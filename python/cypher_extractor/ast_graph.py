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
    "TryStar": "PythonTryStatement",
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

# (ast class name, field name) overrides for cases where the generic
# field-name mapping above would be wrong for that specific class — e.g.
# ``Dict.values`` is the dict's value expressions, not boolean operands
# (the generic "values" -> HAS_OPERAND entry exists for ``BoolOp.values``).
_EDGE_BY_CLASS_FIELD = {
    ("Dict", "values"): "HAS_VALUE",
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

# Comparison operators negate by flipping to their complement, e.g. `<` -> `>=`.
_COMPARE_NEGATION = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.GtE: ast.Lt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


def _expr_text(node) -> str:
    """Render an expression (or match-case pattern) node back to source text."""
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node)


def _negate_condition(node) -> str:
    """Textual negation of a boolean test, simplified where cheap.

    A single comparison (``a < b``) negates by flipping the operator
    (``a >= b``); ``not x`` negates by dropping the ``not`` (double-negation
    elimination). Anything else (chained comparisons, ``and``/``or``, calls,
    ...) falls back to wrapping the whole test in ``not (...)`` — still a
    correct negation, just not simplified.
    """
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        negated_type = _COMPARE_NEGATION.get(type(node.ops[0]))
        if negated_type is not None:
            negated = ast.Compare(left=node.left, ops=[negated_type()], comparators=node.comparators)
            return _expr_text(negated)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _expr_text(node.operand)
    return f"not ({_expr_text(node)})"


def _is_irrefutable_pattern(pattern) -> bool:
    """Whether a `match` case pattern always matches (a bare capture/wildcard)."""
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _is_irrefutable_pattern(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(_is_irrefutable_pattern(p) for p in pattern.patterns)
    return False


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


class _Block:
    """One basic block: a straight-line run of statements, in order."""

    __slots__ = ("statements", "reachable")

    def __init__(self):
        self.statements = []
        self.reachable = False


class _CfgScopeBuilder:
    """Builds the control-flow graph for one scope (a module, function, or
    class body) directly from the ``ast``, replacing the third-party
    ``py2cfg`` package this extractor used to depend on.

    Every scope gets exactly one entry block and one exit block (mirroring
    how Roslyn's CFG models the C# side of this project); every other block
    is created only at an actual branch point. Loop targets for `break`/
    `continue` are tracked on a stack so nested loops resolve correctly.
    """

    def __init__(self):
        self.blocks = []
        self.edges = []  # (source_index, target_index, condition_or_None)
        self._loop_stack = []  # (continue_target, break_target)

    def new_block(self) -> int:
        self.blocks.append(_Block())
        return len(self.blocks) - 1

    def add_edge(self, source, target, condition=None):
        if source is None or target is None:
            return
        self.edges.append((source, target, condition))

    def build(self, body):
        """Build the CFG for one statement list. Returns ``(entry, exit)``."""
        entry = self.new_block()
        exit_ = self.new_block()
        tail = self._process_body(body, entry, exit_)
        self.add_edge(tail, exit_)
        self._mark_reachable(entry)
        return entry, exit_

    def _mark_reachable(self, entry):
        adjacency = {}
        for source, target, _condition in self.edges:
            adjacency.setdefault(source, []).append(target)
        seen = set()
        stack = [entry]
        while stack:
            index = stack.pop()
            if index in seen:
                continue
            seen.add(index)
            stack.extend(adjacency.get(index, []))
        for index in seen:
            self.blocks[index].reachable = True

    def _join(self, *exits) -> int | None:
        """Create an ``after`` block and link every reachable exit into it
        unconditionally; returns ``None`` if every path already terminated."""
        live = [e for e in exits if e is not None]
        if not live:
            return None
        after = self.new_block()
        for block in live:
            self.add_edge(block, after)
        return after

    def _process_body(self, stmts, current, scope_exit):
        for stmt in stmts:
            if current is None:
                # Dead code: still gets a block (so it's still represented in
                # the graph), just with no incoming edge, so `isReachable` is
                # correctly false for it.
                current = self.new_block()
            current = self._process_stmt(stmt, current, scope_exit)
        return current

    def _process_stmt(self, stmt, current, scope_exit):
        kind = type(stmt).__name__
        handler = getattr(self, f"_process_{kind}", None)
        if handler is not None:
            return handler(stmt, current, scope_exit)
        # Every other simple statement (Assign, Expr, Pass, Import, nested
        # FunctionDef/ClassDef, ...) just joins the current block; flow
        # continues unchanged.
        self.blocks[current].statements.append(stmt)
        return current

    def _process_If(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)

        then_block = self.new_block()
        self.add_edge(current, then_block, _expr_text(stmt.test))
        then_exit = self._process_body(stmt.body, then_block, scope_exit)

        else_block = self.new_block()
        self.add_edge(current, else_block, _negate_condition(stmt.test))
        else_exit = self._process_body(stmt.orelse, else_block, scope_exit)

        return self._join(then_exit, else_exit)

    def _process_loop(self, stmt, current, scope_exit, *, has_condition):
        """Shared shape for `while` and `for`/`async for`: a test, a body
        that loops back, an optional `else` (runs only without a `break`),
        and a shared `after` block that `break` also targets directly."""
        self.blocks[current].statements.append(stmt)

        body_block = self.new_block()
        else_block = self.new_block()
        if has_condition:
            self.add_edge(current, body_block, _expr_text(stmt.test))
            self.add_edge(current, else_block, _negate_condition(stmt.test))
        else:
            # `for` has no boolean test to show — see README.
            self.add_edge(current, body_block)
            self.add_edge(current, else_block)

        after = self.new_block()
        self._loop_stack.append((current, after))
        body_exit = self._process_body(stmt.body, body_block, scope_exit)
        self._loop_stack.pop()
        self.add_edge(body_exit, current)  # next iteration re-tests

        else_exit = self._process_body(stmt.orelse, else_block, scope_exit)
        self.add_edge(else_exit, after)

        return after

    def _process_While(self, stmt, current, scope_exit):
        return self._process_loop(stmt, current, scope_exit, has_condition=True)

    def _process_For(self, stmt, current, scope_exit):
        return self._process_loop(stmt, current, scope_exit, has_condition=False)

    _process_AsyncFor = _process_For

    def _process_Match(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)
        pending = []
        test_block = current

        for case in stmt.cases:
            case_block = self.new_block()
            condition = _expr_text(case.pattern)
            if case.guard is not None:
                condition += f" if {_expr_text(case.guard)}"
            self.add_edge(test_block, case_block, condition)
            case_exit = self._process_body(case.body, case_block, scope_exit)
            if case_exit is not None:
                pending.append(case_exit)

            if case.guard is None and _is_irrefutable_pattern(case.pattern):
                # This case always matches: there is no "didn't match" edge
                # to the next case, or out of the match entirely.
                test_block = None
                break
            next_test = self.new_block()
            self.add_edge(test_block, next_test, f"not ({condition})")
            test_block = next_test

        if test_block is not None:
            # No case matched (not exhaustive): `match` falls through
            # without running any case body.
            pending.append(test_block)

        return self._join(*pending)

    def _process_try_like(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)

        body_block = self.new_block()
        self.add_edge(current, body_block)
        body_exit = self._process_body(stmt.body, body_block, scope_exit)

        # Coarse exception modeling: any statement in the try body might
        # raise, so each handler is reachable directly from the try's own
        # block rather than from the exact statement that raised — see
        # README for the tradeoff.
        handler_exits = []
        for handler in stmt.handlers:
            handler_block = self.new_block()
            condition = _expr_text(handler.type) if handler.type is not None else None
            self.add_edge(current, handler_block, condition)
            handler_exit = self._process_body(handler.body, handler_block, scope_exit)
            if handler_exit is not None:
                handler_exits.append(handler_exit)

        if stmt.orelse and body_exit is not None:
            else_block = self.new_block()
            self.add_edge(body_exit, else_block)
            body_exit = self._process_body(stmt.orelse, else_block, scope_exit)

        normal_exits = [e for e in [body_exit, *handler_exits] if e is not None]

        if not stmt.finalbody:
            return self._join(*normal_exits)

        # Known limitation: `finally` always runs in real Python, including
        # right before an early `return`/`raise`/`break`/`continue` inside
        # the try body or a handler — that early-exit path is not routed
        # through `finally` here, only the normal-completion path is. See
        # README.
        finally_block = self.new_block()
        for exit_block in normal_exits:
            self.add_edge(exit_block, finally_block)
        return self._process_body(stmt.finalbody, finally_block, scope_exit)

    _process_Try = _process_try_like
    _process_TryStar = _process_try_like

    def _process_With(self, stmt, current, scope_exit):
        # `with`/`async with` don't branch; the statement joins the current
        # block and its body is processed in the same flow (no new block).
        self.blocks[current].statements.append(stmt)
        return self._process_body(stmt.body, current, scope_exit)

    _process_AsyncWith = _process_With

    def _process_Return(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)
        self.add_edge(current, scope_exit)
        return None

    _process_Raise = _process_Return

    def _process_Break(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)
        if self._loop_stack:
            self.add_edge(current, self._loop_stack[-1][1])
        return None

    def _process_Continue(self, stmt, current, scope_exit):
        self.blocks[current].statements.append(stmt)
        if self._loop_stack:
            self.add_edge(current, self._loop_stack[-1][0])
        return None


def _iter_scopes(node, qualname):
    """Yield ``(qualname, body)`` for ``node``'s own body (if it has one) and
    recurse into every nested function/class definition's body, building
    dotted qualnames (``module.Class.method``) as it goes."""
    body = getattr(node, "body", None)
    if body is None:
        return
    yield qualname, body
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _iter_scopes(stmt, f"{qualname}.{stmt.name}")


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

    # -- control flow graph --------------------------------------------

    def build_cfg(self, tree):
        """Build and emit control-flow graphs for the module and every
        nested function/class body: one ``PythonBasicBlock`` per basic
        block, ``FOLLOWS`` edges for the successor graph (with branch
        conditions where the branch is a real boolean test), and
        ``CONTAINS`` links from each block to the statements it executes.

        Computed entirely with the standard library — no third-party
        dependency, unlike the CFG pass this extractor used to hand off to
        the (unmaintained, `match`-statement-unaware) `py2cfg` package.
        """
        module_name = os.path.splitext(os.path.basename(self._filename))[0] or "module"
        self._cfg_blocks = []
        self._cfg_edges = []
        for qualname, body in _iter_scopes(tree, module_name):
            self._emit_cfg_scope(qualname, body)

    def _emit_cfg_scope(self, qualname, body):
        builder = _CfgScopeBuilder()
        entry, exit_ = builder.build(body)

        block_nodes = {}
        for ordinal, block in enumerate(builder.blocks):
            first = block.statements[0] if block.statements else None
            last = block.statements[-1] if block.statements else None
            props = {
                "scope": qualname,
                "ordinal": ordinal,
                "blockId": ordinal,
                "isEntry": ordinal == entry,
                "isFinal": ordinal == exit_,
                "isReachable": block.reachable,
            }
            if first is not None:
                props["kind"] = type(first).__name__
                if getattr(first, "lineno", None) is not None:
                    props["startLine"] = first.lineno
            if last is not None and getattr(last, "end_lineno", None) is not None:
                props["endLine"] = last.end_lineno
            block_nodes[ordinal] = self._add_node("PythonBasicBlock", props)

        for ordinal, block in enumerate(builder.blocks):
            block_id = block_nodes[ordinal]
            statement_ids = []
            for stmt_ordinal, stmt in enumerate(block.statements):
                stmt_id = self._node_ids.get(id(stmt))
                if stmt_id is not None:
                    statement_ids.append(stmt_id)
                    self._rel(block_id, "CONTAINS", stmt_id, {"ordinal": stmt_ordinal})
            self._cfg_blocks.append(
                {
                    "id": block_id,
                    "function": qualname,
                    "ordinal": ordinal,
                    "isEntry": ordinal == entry,
                    "isFinal": ordinal == exit_,
                    "statementIds": statement_ids,
                }
            )

        for source, target, condition in builder.edges:
            properties = {"condition": condition} if condition else None
            self._rel(block_nodes[source], "FOLLOWS", block_nodes[target], properties)
            self._cfg_edges.append(
                {
                    "source": block_nodes[source],
                    "target": block_nodes[target],
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
        class_name = type(node).__name__
        counters = {}
        for field in node._fields:
            if field in _SKIP_FIELDS:
                continue
            value = getattr(node, field, None)
            if value is None:
                continue
            edge = _EDGE_BY_CLASS_FIELD.get((class_name, field)) or _EDGE_BY_FIELD.get(
                field, "HAS_" + field.upper()
            )
            if self._is_node(value):
                self._visit_ordered(value, edge, node_id, counters)
            elif isinstance(value, list):
                for item in value:
                    if self._is_node(item):
                        self._visit_ordered(item, edge, node_id, counters)

    def _visit_ordered(self, child, edge, node_id, counters):
        """Visits ``child`` and links it to ``node_id`` with an ``ordinal``
        tracking its position among same-typed siblings from this parent —
        across all fields sharing that edge type, so e.g. ``BinOp``'s
        separate ``left``/``right`` fields (both HAS_OPERAND) still get 0/1
        in source order. Statement children are additionally chained with
        ``FOLLOWS`` in source order, matching the C# extractor.
        """
        ordinal = counters.get(edge, 0)
        counters[edge] = ordinal + 1
        child_id = self._visit(child, None, None)
        if child_id is None:
            return None
        self._rel(node_id, edge, child_id, {"ordinal": ordinal})
        if isinstance(child, ast.stmt):
            previous_key = (edge, "previous")
            previous_id = counters.get(previous_key)
            if previous_id is not None:
                self._rel(previous_id, "FOLLOWS", child_id)
            counters[previous_key] = child_id
        return child_id

    @staticmethod
    def _is_node(value) -> bool:
        if isinstance(value, ast.AST):
            return True
        return hasattr(value, "_fields") and type(value).__name__ in _PSEUDO_NODES

    def _visit_special(self, node, node_id) -> bool:
        """Handle node types whose children need bespoke edges."""
        class_name = type(node).__name__
        counters = {}

        if class_name in ("FunctionDef", "AsyncFunctionDef"):
            for dec in node.decorator_list:
                self._visit_ordered(dec, "HAS_DECORATOR", node_id, counters)
            self._visit(node.args, "HAS_PARAMETER", node_id)
            if node.returns is not None:
                type_id = self._add_type_node(_expr_text(node.returns))
                self._rel(node_id, "RETURNS", type_id)
            for stmt in node.body:
                self._visit_ordered(stmt, "CONTAINS", node_id, counters)
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
                self._visit_ordered(dec, "HAS_DECORATOR", node_id, counters)
            for stmt in node.body:
                stmt_id = self._visit_ordered(stmt, "CONTAINS", node_id, counters)
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
        """Attach parameters (and their defaults/annotations) to a callable,
        each with an ``ordinal`` reflecting calling-convention order
        (positional-only, positional, ``*args``, keyword-only, ``**kwargs``).
        """
        ordinal = 0

        def visit_arg(arg_node, kind, default=None):
            nonlocal ordinal
            if arg_node is None:
                return None
            props = {"name": arg_node.arg, "kind": kind}
            props.update(_location_props(arg_node))
            node_id = self._add_node("PythonParameter", props)
            self._rel(parent_id, edge, node_id, {"ordinal": ordinal})
            ordinal += 1
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
        index = bisect.bisect_right(lines, (line, "￿"))
        if index > 0:
            return lines[index - 1][1]
        return None
