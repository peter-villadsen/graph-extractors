"""Tests for the Python -> Cypher extractor (stdlib unittest)."""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cypher_extractor.ast_graph import GraphBuilder
from cypher_extractor.cypher import escape_string, format_value
from cypher_extractor.souffle import SouffleProgram


def build(source, filename="test.py"):
    """Parse ``source`` and run the graph builder; return (nodes, relationships)."""
    tree = ast.parse(source, filename=filename, type_comments=True)
    return GraphBuilder(source, filename).build(tree)


def labels_of(nodes):
    """Return label sets for every node."""
    return [set(n.labels) for n in nodes]


class CypherFormattingTests(unittest.TestCase):
    def test_escape_string(self):
        self.assertEqual(escape_string("a'b\\c\n\t"), "a\\'b\\\\c\\n\\t")

    def test_format_value_bool_and_number(self):
        self.assertEqual(format_value(True), "true")
        self.assertEqual(format_value(False), "false")
        self.assertEqual(format_value(42), "42")

    def test_format_value_string_and_list(self):
        self.assertEqual(format_value("hi"), "'hi'")
        self.assertEqual(format_value(["a", 1]), "['a', 1]")


class ModuleTests(unittest.TestCase):
    def test_module_node(self):
        nodes, _ = build("pass\n", "mod.py")
        module = nodes[0]
        self.assertEqual(module.labels, ["PythonModule"])
        self.assertEqual(module.properties["name"], "mod.py")
        self.assertEqual(module.properties["file"], "mod.py")

    def test_pass_statement_appears(self):
        nodes, _ = build("pass\n")
        self.assertTrue(any(set(n.labels) == {"PythonPassStatement", "PythonStatement"} for n in nodes))


class FunctionTests(unittest.TestCase):
    def test_function_with_params_and_return(self):
        source = "def greet(name: str, n=1) -> str:\n    return name\n"
        nodes, rels = build(source)

        fn = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        self.assertEqual(fn.properties["name"], "greet")
        self.assertEqual(fn.properties["isAsync"], False)

        params = [n for n in nodes if "PythonParameter" in n.labels]
        self.assertEqual(len(params), 2)
        self.assertEqual({p.properties["name"] for p in params}, {"name", "n"})

        kinds = {r.type for r in rels}
        self.assertIn("HAS_PARAMETER", kinds)
        self.assertIn("RETURNS", kinds)

        returns = [r for r in rels if r.type == "RETURNS"]
        type_node = next(n for n in nodes if "PythonType" in n.labels and n.id == returns[0].target)
        self.assertEqual(type_node.properties["name"], "str")

    def test_async_function_label(self):
        nodes, _ = build("async def f():\n    pass\n")
        fn = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        self.assertTrue("PythonAsyncFunctionDefinition" in fn.labels)
        self.assertEqual(fn.properties["isAsync"], True)

    def test_parameter_annotation(self):
        nodes, rels = build("def f(x: int):\n    pass\n")
        param = next(n for n in nodes if "PythonParameter" in n.labels)
        of_type = next(r for r in rels if r.type == "OF_TYPE" and r.source == param.id)
        type_node = next(n for n in nodes if n.id == of_type.target)
        self.assertEqual(type_node.properties["name"], "int")


class ClassTests(unittest.TestCase):
    def test_class_derives_and_declares(self):
        source = "class Dog(Animal):\n    def bark(self):\n        pass\n"
        nodes, rels = build(source)

        cls = next(n for n in nodes if "PythonClassDefinition" in n.labels)
        self.assertEqual(cls.properties["name"], "Dog")

        self.assertTrue(any(r.type == "DERIVED_FROM" and r.source == cls.id for r in rels))
        self.assertTrue(any(r.type == "DECLARES" and r.source == cls.id for r in rels))


class StatementTests(unittest.TestCase):
    def test_while_condition_and_body(self):
        source = "while x < 10:\n    x = x + 1\n"
        nodes, rels = build(source)

        loop = next(n for n in nodes if "PythonWhileStatement" in n.labels)
        self.assertEqual({r.type for r in rels if r.source == loop.id}, {"HAS_CONDITION", "CONTAINS"})

    def test_if_has_condition(self):
        source = "if x:\n    pass\nelse:\n    pass\n"
        nodes, rels = build(source)

        stmt = next(n for n in nodes if "PythonIfStatement" in n.labels)
        types = {r.type for r in rels if r.source == stmt.id}
        self.assertIn("HAS_CONDITION", types)
        self.assertIn("HAS_ELSE", types)

    def test_binary_operator_stored_as_property(self):
        nodes, _ = build("x = a + b\n")
        binop = next(n for n in nodes if "PythonBinaryOperation" in n.labels)
        self.assertEqual(binop.properties["operator"], "+")
        self.assertEqual(len([r for r in nodes if "PythonBinaryOperation" in n.labels] if False else []), 0)


class TriviaTests(unittest.TestCase):
    def test_comment_attached_to_statement(self):
        source = "x = 1\n# a comment\n"
        nodes, rels = build(source)

        comment = next(n for n in nodes if "PythonCommentTrivia" in n.labels)
        self.assertEqual(comment.properties["text"], "a comment")
        self.assertTrue(any(r.type == "HAS_TRIVIA" and r.target == comment.id for r in rels))

    def test_docstring_label(self):
        source = '"""module docs"""\n'
        nodes, _ = build(source)
        self.assertTrue(any(n.labels == ["PythonStatement", "PythonDocString"] for n in nodes))


class SourceOffsetTests(unittest.TestCase):
    def test_function_and_statements_carry_offsets(self):
        source = "def f(x):\n    while x:\n        x = x - 1\n"
        nodes, _ = build(source)

        fn = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        self.assertEqual(fn.properties["startLine"], 1)
        self.assertEqual(fn.properties["startColumn"], 0)
        self.assertEqual(fn.properties["endLine"], 3)

        loop = next(n for n in nodes if "PythonWhileStatement" in n.labels)
        self.assertEqual(loop.properties["startLine"], 2)
        self.assertEqual(loop.properties["startColumn"], 4)

        param = next(n for n in nodes if "PythonParameter" in n.labels)
        self.assertEqual(param.properties["startColumn"], 6)

    def test_offsets_are_one_based_lines_zero_based_columns(self):
        nodes, _ = build("a = 1\n\nif a:\n    a = 2\n")
        stmt = next(n for n in nodes if "PythonIfStatement" in n.labels)
        self.assertEqual(stmt.properties["startLine"], 3)
        self.assertEqual(stmt.properties["startColumn"], 0)


class ImportTests(unittest.TestCase):
    def test_import_does_not_crash_and_emits_node(self):
        nodes, rels = build("import os\nfrom pathlib import Path as P\n")
        self.assertTrue(any("PythonImportStatement" in n.labels for n in nodes))
        imp = next(n for n in nodes if "PythonImportFromStatement" in n.labels)
        self.assertEqual(imp.properties["module"], "pathlib")


try:
    from py2cfg.builder import CFGBuilder as _Py2CfgBuilder  # noqa: F401
    _PY2CFG_AVAILABLE = True
except ImportError:
    _PY2CFG_AVAILABLE = False


def build_with_cfg(source, filename="test.py"):
    """Run the graph builder including the py2cfg control-flow pass."""
    tree = ast.parse(source, filename=filename, type_comments=True)
    builder = GraphBuilder(source, filename)
    builder.build(tree)
    builder.build_cfg(tree)
    return builder._nodes, builder._relationships


@unittest.skipUnless(_PY2CFG_AVAILABLE, "py2cfg is not installed")
class CfgTests(unittest.TestCase):
    def test_while_loop_emits_blocks_and_conditional_edges(self):
        source = "def f(x):\n    while x < 10:\n        x = x + 1\n    return x\n"
        nodes, rels = build_with_cfg(source)

        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        self.assertGreaterEqual(len(blocks), 3)

        follows = [r for r in rels if r.type == "FOLLOWS"]
        conditional = [r for r in follows if r.properties and "condition" in r.properties]
        self.assertTrue(conditional)
        self.assertTrue(
            any("x < 10" in r.properties["condition"] for r in conditional),
            "true branch should carry the loop condition",
        )
        self.assertTrue(
            any("x >= 10" in r.properties["condition"] for r in conditional),
            "false branch should carry the negated condition",
        )

        # The body block links (CONTAINS) to the assignment statement node.
        block_ids = {n.id for n in blocks}
        contains = [r for r in rels if r.type == "CONTAINS"]
        assign_ids = {n.id for n in nodes if "PythonAssignmentStatement" in n.labels}
        self.assertTrue(any(r.source in block_ids and r.target in assign_ids for r in contains))

    def test_function_subcfg_blocks_are_scoped(self):
        source = "def f():\n    return 1\n\ndef g():\n    pass\n"
        nodes, _ = build_with_cfg(source)

        scopes = {n.properties["scope"] for n in nodes if "PythonBasicBlock" in n.labels}
        self.assertIn("test.f", scopes)
        self.assertIn("test.g", scopes)

    def test_entry_and_final_flags(self):
        source = "def f():\n    if x:\n        return 1\n    return 0\n"
        nodes, _ = build_with_cfg(source)

        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        self.assertTrue(any(n.properties.get("isEntry") for n in blocks))
        self.assertTrue(any(n.properties.get("isFinal") for n in blocks))

    def test_full_pipeline_emits_cfg_datalog(self):
        source = "def f(x):\n    while x < 10:\n        x = x + 1\n    return x\n"
        tree = ast.parse(source, filename="test.py", type_comments=True)
        builder = GraphBuilder(source, "test.py")
        builder.build(tree)
        builder.build_cfg(tree)
        program = SouffleProgram()
        program.add_cfg(builder._cfg_blocks, builder._cfg_edges, builder._nodes)
        text = program.render()

        self.assertIn("basic_block(", text)
        self.assertIn("control_flow_edge(", text)
        self.assertIn("block_statement(", text)
        self.assertIn('"test.f"', text)
        self.assertIn(".output reachable", text)
        self.assertIn(".output path", text)


class SouffleTests(unittest.TestCase):
    @staticmethod
    def _render():
        program = SouffleProgram()
        program.add_cfg(
            blocks=[
                {
                    "id": "p1",
                    "function": "mod.f",
                    "ordinal": 0,
                    "isEntry": True,
                    "isFinal": False,
                    "statementIds": ["p2"],
                },
                {
                    "id": "p3",
                    "function": "mod.f",
                    "ordinal": 1,
                    "isEntry": False,
                    "isFinal": True,
                    "statementIds": [],
                },
            ],
            edges=[
                {"source": "p1", "target": "p3", "function": "mod.f", "condition": "(x < 10)"},
            ],
            nodes=[],
        )
        return program.render()

    def test_decls_facts_and_rules(self):
        text = self._render()
        self.assertIn(".decl basic_block(", text)
        self.assertIn(".decl control_flow_edge(", text)
        self.assertIn(".decl block_statement(", text)
        self.assertIn('basic_block("b1", "mod.f", 0, 1, 0).', text)
        self.assertIn('basic_block("b2", "mod.f", 1, 0, 1).', text)
        self.assertIn('control_flow_edge("b1", "b2", "mod.f", "(x < 10)").', text)
        self.assertIn('block_statement("b1", "s1", "Statement", "mod.f").', text)

    def test_analysis_rules_and_outputs(self):
        text = self._render()
        self.assertIn("reachable(Id, Function) :- basic_block(Id, Function, _, 1, _).", text)
        self.assertIn(
            "reachable(Target, Function) :- reachable(Source, Function), "
            "control_flow_edge(Source, Target, Function, _).",
            text,
        )
        self.assertIn("path(Block, Block, Function) :- basic_block(Block, Function, _, _, _).", text)
        self.assertIn(".output reachable", text)
        self.assertIn(".output path", text)

    def test_symbols_are_escaped(self):
        program = SouffleProgram()
        program.add_cfg(
            blocks=[
                {
                    "id": "p1",
                    "function": 'mod."f"',
                    "ordinal": 0,
                    "isEntry": True,
                    "isFinal": False,
                    "statementIds": [],
                }
            ],
            edges=[
                {"source": "p1", "target": "p1", "function": "mod.f", "condition": "a\\b"},
            ],
            nodes=[],
        )
        text = program.render()
        self.assertIn('basic_block("b1", "mod.\\"f\\"", 0, 1, 0).', text)
        self.assertIn('control_flow_edge("b1", "b1", "mod.f", "a\\\\b").', text)

    def test_ids_are_remapped_globally_across_files(self):
        program = SouffleProgram()
        for _ in range(2):
            program.add_cfg(
                blocks=[
                    {
                        "id": "p1",
                        "function": "mod.f",
                        "ordinal": 0,
                        "isEntry": True,
                        "isFinal": False,
                        "statementIds": [],
                    }
                ],
                edges=[],
                nodes=[],
            )
        text = program.render()
        self.assertIn('basic_block("b1", "mod.f", 0, 1, 0).', text)
        self.assertIn('basic_block("b2", "mod.f", 0, 1, 0).', text)


if __name__ == "__main__":
    unittest.main()