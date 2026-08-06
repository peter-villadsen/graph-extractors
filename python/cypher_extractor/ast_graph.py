"""Walk a Python ``ast`` tree and produce a Cypher graph.

Node labels and relationship types use the names of the official Python
grammar (see https://docs.python.org/3/reference/grammar.html):

* ``classdef``      -> ``PythonClassDefinition``
* ``funcdef``/``async_funcdef`` -> ``PythonFunctionDefinition``
* ``while_stmt``    -> ``PythonWhileStatement``, ``if_stmt`` -> ``PythonIfStatement``
* ``for_stmt``      -> ``PythonForStatement``, ``return_stmt`` -> ``PythonReturnStatement``
* ``parameters``    -> ``PythonParameter``, etc.

The generic labels ``PythonStatement`` and ``PythonExpression`` are added so a
query such as ``MATCH (s:PythonStatement)`` matches every statement regardless
of its specific kind.
"""

from __future__ import annotations

import ast
import bisect
import io
import os
import tokenize

from .cypher import Node, Relationship

# Mapping from ast node class name to the primary, language-endemic label.
LABEL_BY_CLASS = {
    # module / declarations
    "Module": "PythonModule",
    "ClassDef": "PythonClassDefinition",
    "FunctionDef": "PythonFunctionDefinition",
    "AsyncFunctionDef": "PythonAsyncFunctionDefinition",
    "TypeAlias": "PythonTypeAlias",
    # statements
    "Return": "PythonReturnStatement",
    "Delete": "PythonDeleteStatement",
    "Assign": "PythonAssignmentStatement",
    "AugAssign": "PythonAugmentedAssignmentStatement",
    "AnnAssign": "PythonAnnotatedAssignmentStatement",
    "For": "PythonForStatement",
    "AsyncFor": "PythonAsyncForStatement",
    "While": "PythonWhileStatement",
    "If": "PythonIfStatement",
    "With": "PythonWithStatement",
    "AsyncWith": "PythonAsyncWithStatement",
    "Raise": "PythonRaiseStatement",
    "Try": "PythonTryStatement",
    "Assert": "PythonAssertStatement",
    "Import": "PythonImportStatement",
    "ImportFrom": "PythonImportFromStatement",
    "Global": "PythonGlobalStatement",
    "Nonlocal": "PythonNonlocalStatement",
    "Pass": "PythonPassStatement",
    "Break": "PythonBreakStatement",
    "Continue": "PythonContinueStatement",
    "Match": "PythonMatchStatement",
    "Expr": "PythonExpressionStatement",
    "ExceptHandler": "PythonExceptHandler",
    # parameters / misc grammar productions
    "arg": "PythonParameter",
    "alias": "PythonImportAlias",
    "keyword": "PythonKeywordArgument",
    "withitem": "PythonWithItem",
    "comprehension": "PythonComprehension",
    "match_case": "PythonMatchCase",
    # expressions
    "Constant": "PythonConstant",
    "Name": "PythonName",
    "Attribute": "PythonAttribute",
    "Subscript": "PythonSubscript",
    "Starred": "PythonStarredExpression",
    "Tuple": "PythonTuple",
    "List": "PythonList",
    "Set": "PythonSet",
    "Dict": "PythonDictionary",
    "BinOp": "PythonBinaryOperation",
    "UnaryOp": "PythonUnaryOperation",
    "BoolOp": "PythonBooleanOperation",
    "Compare": "PythonComparison",
    "IfExp": "PythonConditionalExpression",
    "Call": "PythonCall",
    "Lambda": "PythonLambda",
    "Yield": "PythonYield",
    "YieldFrom": "PythonYield",
    "Await": "PythonAwait",
    "Slice": "PythonSlice",
    "NamedExpr": "PythonNamedExpression",
    "JoinedStr": "PythonJoinedString",
    "FormattedValue": "PythonFormattedValue",
    "GeneratorExp": "PythonGeneratorExpression",
    "ListComp": "PythonListComprehension",
    "SetComp": "PythonSetComprehension",
    "DictComp": "PythonDictComprehension",
}

# Field names that must never be traversed as child nodes.
_SKIP_FIELDS = {"ctx", "type_ignores"}

# Relationship type to use when descending into an ast field.
_EDGE_BY_FIELD = {
    "body": "CONTAINS",
    "orelse": "HAS_ELSE",
    "finalbody": "HAS_FINALLY",
    "handlers": "HAS_HANDLER",
    "test": "HAS_CONDITION",
    "msg": "HAS_MESSAGE",
    "targets": "HAS_TARGET",
    "target": "HAS_TARGET",
    "value": "HAS_VALUE",
    "values": "HAS_OPERAND",
    "iter": "HAS_ITERABLE",
    "annotation": "OF_TYPE",
    "returns": "RETURNS",
    "left": "HAS_OPERAND",
    "right": "HAS_OPERAND",
    "operand": "HAS_OPERAND",
    "comparators": "HAS_OPERAND",
    "func": "HAS_FUNCTION",
    "args": "HAS_ARGUMENT",
    "keywords": "HAS_KEYWORD_ARGUMENT",
    "elts": "HAS_ELEMENT",
    "keys": "HAS_KEY",
    "elt": "HAS_ELEMENT",
    "generators": "HAS_GENERATOR",
    "ifs": "HAS_IF",
    "slice": "HAS_INDEX",
    "lower": "HAS_LOWER",
    "upper": "HAS_UPPER",
    "step": "HAS_STEP",
    "exc": "HAS_EXCEPTION",
    "cause": "HAS_CAUSE",
    "subject": "HAS_SUBJECT",
    "cases": "HAS_CASE",
    "pattern": "HAS_PATTERN",
    "guard": "HAS_CONDITION",
    "items": "HAS_ITEM",
    "context_expr": "HAS_EXPRESSION",
    "optional_vars": "HAS_AS_TARGET",
    "names": "HAS_ALIAS",
    "format_spec": "HAS_FORMAT_SPEC",
    "type": "OF_TYPE",
}

# Operator classes we never want as nodes; they are recorded as properties.
_OP_SYMBOLS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.MatMult: "@",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.And: "and",
    ast.Or: "or",
    ast.Not: "not",
    ast.USub: "-",
    ast.UAdd: "+",
    ast.Invert: "~",
}
_OP_BY_CLASS = {cls.__name__: symbol for cls, symbol in _OP_SYMBOLS.items()}

# Grammar productions that carry ``_fields`` but are not ``ast.AST`` subclasses.
_PSEUDO_NODES = {
    "arguments",
    "alias",
    "keyword",
    "comprehension",
    "withitem",
    "match_case",
    "TypeIgnore",
}


def _expr_text(node) -> str:
    """Render an expression node back to source text."""
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node)


def _location_props(node) -> dict:
    """Source offsets (1-based lines, 0-based columns) from an ast node."""
    props = {}
    for attr, key in (
        ("lineno", "startLine"),
        ("col_offset", "startColumn"),
        ("end_lineno", "endLine"),
        ("end_col_offset", "endColumn"),
    ):
        value = getattr(node, attr, None)
        if value is not None:
            props[key] = value
    return props


class GraphBuilder:
    """Builds nodes/relationships for one parsed module."""

    def __init__(self, source: str, filename: str, start_id: int = 0):
        self._source = source
        self._filename = filename
        self._nodes = []
        self._relationships = []
        self._counter = start_id
        self._module_id = None
        # lineno -> node id, for anchoring comments (statements only).
        self._statement_lines = []
        # id(ast node) -> node id, for linking CFG blocks back to statements.
        self._node_ids = {}
        # CFG model (collected by build_cfg, consumed by the Cypher and
        # Souffle emitters): blocks and control-flow edges.
        self._cfg_blocks = []
        self._cfg_edges = []

    # -- construction -------------------------------------------------

    def build(self, tree):
        self._visit(tree, None, None)
        self._attach_comments()
        return self._nodes, self._relationships

    # -- control flow graph (py2cfg) ---------------------------------

    def build_cfg(self, tree):
        """Emit basic blocks and control-flow edges computed by ``py2cfg``.

        The CFG is calculated by the ``py2cfg`` package (a third-party AST
        walker), not by this extractor; we only render its blocks and links
        into the graph. ``py2cfg`` is an optional dependency and is imported
        lazily so the extractor stays stdlib-only without ``--cfg``.
        """
        try:
            from py2cfg.builder import CFGBuilder
        except ImportError:
            raise RuntimeError("the '--cfg' flag requires the 'py2cfg' package (pip install py2cfg)")

        module_name = os.path.splitext(os.path.basename(self._filename))[0] or "module"
        try:
            cfg = CFGBuilder().build(module_name, tree)
            self._cfg_blocks = []
            self._cfg_edges = []
            self._emit_cfg_tree(cfg, module_name)
        except Exception as exc:
            # py2cfg is a third-party AST walker with its own edge cases
            # (e.g. it has no visitor for `match`); let a single file's CFG
            # failure be reported and skipped rather than crashing the whole
            # run.
            raise RuntimeError(f"py2cfg failed to build a control-flow graph: {exc}") from exc

    def _emit_cfg_tree(self, cfg, qualname):
        """Emit one CFG and recurse into function/class sub-CFGs."""
        self._emit_cfg(cfg, qualname)
        for child_name, sub in cfg.functioncfgs.items():
            self._emit_cfg_tree(sub, f"{qualname}.{child_name}")
        for child_name, sub in cfg.classcfgs.items():
            self._emit_cfg_tree(sub, f"{qualname}.{child_name}")

    def _emit_cfg(self, cfg, qualname):
        """Collect one py2cfg graph into the model; emit Cypher nodes/edges."""
        blocks = list(cfg.own_blocks())
        block_nodes = {}
        for ordinal, block in enumerate(blocks):
            first = block.statements[0] if block.statements else None
            props = {
                "scope": qualname,
                "ordinal": ordinal,
                "blockId": block.id,
                "isEntry": block is cfg.entryblock,
                "isFinal": block in cfg.finalblocks,
            }
            if first is not None:
                props["kind"] = type(first).__name__
            start, end = block.at(), block.end()
            if start >= 0:
                props["startLine"] = start
            if end >= 0:
                props["endLine"] = end
            block_nodes[block.id] = self._add_node("PythonBasicBlock", props)

            statement_ids = []
            for stmt in block.statements:
                stmt_id = self._node_ids.get(id(stmt))
                if stmt_id is not None:
                    statement_ids.append(stmt_id)
                    self._rel(block_nodes[block.id], "CONTAINS", stmt_id)
            self._cfg_blocks.append(
                {
                    "id": block_nodes[block.id],
                    "function": qualname,
                    "ordinal": ordinal,
                    "isEntry": block is cfg.entryblock,
                    "isFinal": block in cfg.finalblocks,
                    "statementIds": statement_ids,
                }
            )

        for block in blocks:
            source = block_nodes[block.id]
            for link in block.exits:
                target = block_nodes.get(link.target.id)
                if target is None:
                    continue
                properties = None
                condition = link.get_exitcase().strip()
                if condition:
                    properties = {"condition": condition}
                self._rel(source, "FOLLOWS", target, properties)
                self._cfg_edges.append(
                    {
                        "source": source,
                        "target": target,
                        "function": qualname,
                        "condition": condition,
                    }
                )

    def _new_id(self) -> str:
        self._counter += 1
        return f"p{self._counter}"

    def _add_node(self, labels, properties=None, lineno=None, record=False) -> str:
        node_id = self._new_id()
        self._nodes.append(Node(node_id, labels, properties))
        if record and lineno is not None:
            self._statement_lines.append((lineno, node_id))
        return node_id

    def _add_type_node(self, name: str, extra=None) -> str:
        props = {"name": name}
        if extra:
            props.update(extra)
        return self._add_node("PythonType", props)

    def _rel(self, source, type_, target, properties=None):
        if source is None or target is None:
            return
        self._relationships.append(Relationship(source, type_, target, properties))

    # -- node classification ------------------------------------------

    def _labels_for(self, class_name: str, node):
        if class_name in ("FunctionDef", "AsyncFunctionDef"):
            labels = ["PythonFunctionDefinition", LABEL_BY_CLASS.get(class_name)]
        elif class_name == "YieldFrom":
            labels = [LABEL_BY_CLASS.get(class_name), "PythonYieldFrom"]
        else:
            labels = [LABEL_BY_CLASS.get(class_name, "Python" + class_name)]
        if isinstance(node, ast.stmt) and "PythonStatement" not in labels:
            labels.append("PythonStatement")
        if isinstance(node, ast.expr) and "PythonExpression" not in labels:
            labels.append("PythonExpression")
        seen = []
        for label in labels:
            if label and label not in seen:
                seen.append(label)
        return seen

    def _props_for(self, class_name: str, node) -> dict:
        props = {}
        name = getattr(node, "name", None)
        if name is not None:
            props["name"] = name

        if class_name == "Module":
            props["file"] = self._filename
            props["name"] = os.path.basename(self._filename)
        elif class_name in ("FunctionDef", "AsyncFunctionDef"):
            props["isAsync"] = class_name == "AsyncFunctionDef"
            props["isDecorated"] = bool(node.decorator_list)
        elif class_name == "ClassDef":
            props["isDecorated"] = bool(node.decorator_list)
        elif class_name == "Name":
            props["name"] = node.id
        elif class_name in ("BinOp", "UnaryOp", "BoolOp", "AugAssign"):
            op = getattr(node, "op", None)
            if op is not None:
                props["operator"] = _OP_BY_CLASS.get(type(op).__name__, type(op).__name__)
        elif class_name == "Compare":
            props["operators"] = [
                _OP_BY_CLASS.get(type(o).__name__, type(o).__name__) for o in node.ops
            ]
        elif class_name == "Constant":
            value = node.value
            if isinstance(value, (str, int, float, bool)) or value is None:
                props["value"] = value
            else:
                props["value"] = repr(value)
            if node.kind:
                props["kind"] = node.kind
        elif class_name == "Attribute":
            props["attribute"] = node.attr
        elif class_name in ("For", "AsyncFor"):
            props["isAsync"] = class_name == "AsyncFor"
        elif class_name in ("With", "AsyncWith"):
            props["isAsync"] = class_name == "AsyncWith"
        elif class_name == "Import":
            props["aliases"] = self._alias_texts(node)
        elif class_name == "ImportFrom":
            props["module"] = node.module or ""
            props["level"] = node.level
            props["aliases"] = self._alias_texts(node)
        elif class_name in ("Global", "Nonlocal"):
            props["names"] = list(node.names)
        elif class_name == "ExceptHandler":
            if node.type is not None:
                props["exceptionType"] = _expr_text(node.type)
            if node.name:
                props["name"] = node.name
        elif class_name == "AnnAssign":
            props["simple"] = bool(node.simple)
        elif class_name == "keyword":
            if node.arg:
                props["name"] = node.arg
        elif class_name == "alias":
            if node.asname:
                props["asname"] = node.asname

        return {k: v for k, v in props.items() if v is not None}

    @staticmethod
    def _alias_texts(node) -> list:
        return [
            alias.name + (f" as {alias.asname}" if alias.asname else "")
            for alias in node.names
        ]

    # -- tree walk -----------------------------------------------------

    def _visit(self, node, edge, parent_id):
        class_name = type(node).__name__

        # Operators and the expression-context pseudo nodes are never modeled.
        if class_name in _OP_BY_CLASS or class_name in ("Load", "Store", "Del"):
            return None

        lineno = getattr(node, "lineno", None)

        if class_name == "arguments":
            self._visit_arguments(node, edge, parent_id)
            return None

        if class_name == "Expr" and isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, str
        ):
            node_id = self._add_node(
                ["PythonStatement", "PythonDocString"],
                {"text": node.value.value, **_location_props(node)},
                lineno,
                record=True,
            )
            self._node_ids[id(node)] = node_id
            self._rel(parent_id, edge, node_id)
            return node_id

        labels = self._labels_for(class_name, node)
        props = self._props_for(class_name, node)
        props.update(_location_props(node))
        is_stmt = isinstance(node, ast.stmt)
        node_id = self._add_node(labels, props, lineno, record=is_stmt)
        self._node_ids[id(node)] = node_id
        self._rel(parent_id, edge, node_id)
        self._visit_children(node, node_id)
        return node_id

    def _visit_children(self, node, node_id):
        if self._visit_special(node, node_id):
            return
        for field in node._fields:
            if field in _SKIP_FIELDS:
                continue
            value = getattr(node, field, None)
            if value is None:
                continue
            edge = _EDGE_BY_FIELD.get(field, "HAS_" + field.upper())
            if self._is_node(value):
                self._visit(value, edge, node_id)
            elif isinstance(value, list):
                for item in value:
                    if self._is_node(item):
                        self._visit(item, edge, node_id)

    @staticmethod
    def _is_node(value) -> bool:
        if isinstance(value, ast.AST):
            return True
        return hasattr(value, "_fields") and type(value).__name__ in _PSEUDO_NODES

    def _visit_special(self, node, node_id) -> bool:
        """Handle node types whose children need bespoke edges."""
        class_name = type(node).__name__

        if class_name in ("FunctionDef", "AsyncFunctionDef"):
            for dec in node.decorator_list:
                self._visit(dec, "HAS_DECORATOR", node_id)
            self._visit(node.args, "HAS_PARAMETER", node_id)
            if node.returns is not None:
                type_id = self._add_type_node(_expr_text(node.returns))
                self._rel(node_id, "RETURNS", type_id)
            for stmt in node.body:
                self._visit(stmt, "CONTAINS", node_id)
            return True

        if class_name == "Lambda":
            self._visit(node.args, "HAS_PARAMETER", node_id)
            self._visit(node.body, "HAS_BODY", node_id)
            return True

        if class_name == "ClassDef":
            for base in node.bases:
                type_id = self._add_type_node(_expr_text(base), {"kind": "class"})
                self._rel(node_id, "DERIVED_FROM", type_id)
            for kw in node.keywords:
                kw_id = self._add_node("PythonKeywordArgument", {"name": kw.arg})
                self._rel(node_id, "HAS_KEYWORD_ARGUMENT", kw_id)
                if kw.value is not None:
                    self._visit(kw.value, "HAS_VALUE", kw_id)
            for dec in node.decorator_list:
                self._visit(dec, "HAS_DECORATOR", node_id)
            for stmt in node.body:
                stmt_id = self._visit(stmt, "CONTAINS", node_id)
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self._rel(node_id, "DECLARES", stmt_id)
            return True

        if class_name == "IfExp":
            self._visit(node.test, "HAS_CONDITION", node_id)
            self._visit(node.body, "HAS_BODY", node_id)
            self._visit(node.orelse, "HAS_ELSE", node_id)
            return True

        return False

    def _visit_arguments(self, arguments, edge, parent_id):
        """Attach parameters (and their defaults/annotations) to a callable."""

        def visit_arg(arg_node, kind, default=None):
            if arg_node is None:
                return None
            props = {"name": arg_node.arg, "kind": kind}
            props.update(_location_props(arg_node))
            node_id = self._add_node("PythonParameter", props)
            self._rel(parent_id, edge, node_id)
            if default is not None:
                default_id = self._visit(default, None, None)
                self._rel(node_id, "HAS_DEFAULT", default_id)
            if arg_node.annotation is not None:
                type_id = self._add_type_node(_expr_text(arg_node.annotation))
                self._rel(node_id, "OF_TYPE", type_id)
            return node_id

        posonly = list(arguments.posonlyargs)
        positional = posonly + list(arguments.args)
        defaults = list(arguments.defaults)
        kw_defaults = list(arguments.kw_defaults)
        pad = len(positional) - len(defaults)

        for index, param in enumerate(positional):
            kind = "positional-only" if index < len(posonly) else "positional"
            default = defaults[index - pad] if index >= pad else None
            visit_arg(param, kind, default)

        visit_arg(arguments.vararg, "vararg")
        for index, param in enumerate(arguments.kwonlyargs):
            default = kw_defaults[index] if index < len(kw_defaults) else None
            visit_arg(param, "keyword-only", default)
        visit_arg(arguments.kwarg, "kwarg")

    # -- comments (trivia) --------------------------------------------

    def _attach_comments(self):
        """Attach ``#`` comments to the nearest preceding statement."""
        lines = sorted(self._statement_lines)
        try:
            tokens = tokenize.generate_tokens(io.StringIO(self._source).readline)
        except Exception:
            return
        comments = [
            (tok.start[0], tok.string)
            for tok in tokens
            if tok.type == tokenize.COMMENT
        ]
        for line, text in comments:
            target = self._nearest_statement(line, lines)
            if target is None:
                continue
            comment_id = self._add_node(
                "PythonCommentTrivia",
                {"text": text[1:].strip(), "line": line},
            )
            self._rel(target, "HAS_TRIVIA", comment_id)

    @staticmethod
    def _nearest_statement(line, lines):
        if not lines:
            return None
        index = bisect.bisect_right(lines, (line, "\uffff"))
        if index > 0:
            return lines[index - 1][1]
        return None
