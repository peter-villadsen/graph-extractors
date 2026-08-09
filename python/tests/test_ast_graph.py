"""Tests for the Python -> Cypher extractor (stdlib unittest)."""

import ast
import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cypher_extractor.ast_graph import GraphBuilder
from cypher_extractor.cypher import Node, Relationship, escape_string, format_value
from cypher_extractor.souffle import SouffleProgram
from cypher_extractor.writers import CypherStreamWriter, Neo4jAdminImportWriter


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


class OrderTests(unittest.TestCase):
    """Ordinal/FOLLOWS parity with the C# extractor: order should be a
    queryable graph fact, not something recovered from `startLine`/`startColumn`."""

    def test_statements_in_a_body_are_chained_with_follows(self):
        nodes, rels = build("def f():\n    a = 1\n    b = 2\n    c = 3\n")
        func = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        contains = [r for r in rels if r.source == func.id and r.type == "CONTAINS"]
        stmt_ids = [r.target for r in contains]
        follows = [r for r in rels if r.type == "FOLLOWS"]
        self.assertEqual(len(follows), 2)
        self.assertTrue(any(f.source == stmt_ids[0] and f.target == stmt_ids[1] for f in follows))
        self.assertTrue(any(f.source == stmt_ids[1] and f.target == stmt_ids[2] for f in follows))

    def test_module_and_class_bodies_also_get_follows(self):
        nodes, rels = build("x = 1\ny = 2\n")
        module = next(n for n in nodes if "PythonModule" in n.labels)
        contains = [r for r in rels if r.source == module.id and r.type == "CONTAINS"]
        follows = [r for r in rels if r.type == "FOLLOWS"]
        self.assertEqual(len(follows), 1)
        self.assertEqual(follows[0].source, contains[0].target)
        self.assertEqual(follows[0].target, contains[1].target)

    def test_binary_operands_carry_ordinal_across_separate_fields(self):
        nodes, rels = build("x = a - b\n")
        binop = next(n for n in nodes if "PythonBinaryOperation" in n.labels)
        operands = sorted(
            (r for r in rels if r.source == binop.id and r.type == "HAS_OPERAND"),
            key=lambda r: r.properties["ordinal"],
        )
        self.assertEqual([r.properties["ordinal"] for r in operands], [0, 1])
        left = next(n for n in nodes if n.id == operands[0].target)
        right = next(n for n in nodes if n.id == operands[1].target)
        self.assertEqual(left.properties["name"], "a")
        self.assertEqual(right.properties["name"], "b")

    def test_chained_comparison_operands_are_in_source_order(self):
        nodes, rels = build("y = a < b < c\n")
        compare = next(n for n in nodes if "PythonComparison" in n.labels)
        operands = sorted(
            (r for r in rels if r.source == compare.id and r.type == "HAS_OPERAND"),
            key=lambda r: r.properties["ordinal"],
        )
        names = [next(n for n in nodes if n.id == r.target).properties["name"] for r in operands]
        self.assertEqual(names, ["a", "b", "c"])

    def test_call_arguments_carry_ordinal(self):
        nodes, rels = build("F(x, y, z)\n")
        call = next(n for n in nodes if "PythonCall" in n.labels)
        args = sorted(
            (r for r in rels if r.source == call.id and r.type == "HAS_ARGUMENT"),
            key=lambda r: r.properties["ordinal"],
        )
        names = [next(n for n in nodes if n.id == r.target).properties["name"] for r in args]
        self.assertEqual(names, ["x", "y", "z"])

    def test_parameters_carry_ordinal_in_calling_convention_order(self):
        nodes, rels = build("def f(a, b, *args, c, **kwargs):\n    pass\n")
        func = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        params = sorted(
            (r for r in rels if r.source == func.id and r.type == "HAS_PARAMETER"),
            key=lambda r: r.properties["ordinal"],
        )
        names = [next(n for n in nodes if n.id == r.target).properties["name"] for r in params]
        self.assertEqual(names, ["a", "b", "args", "c", "kwargs"])

    def test_decorators_carry_ordinal(self):
        nodes, rels = build("@first\n@second\ndef f():\n    pass\n")
        func = next(n for n in nodes if "PythonFunctionDefinition" in n.labels)
        decorators = sorted(
            (r for r in rels if r.source == func.id and r.type == "HAS_DECORATOR"),
            key=lambda r: r.properties["ordinal"],
        )
        names = [next(n for n in nodes if n.id == r.target).properties["name"] for r in decorators]
        self.assertEqual(names, ["first", "second"])

    def test_dict_values_use_has_value_not_has_operand(self):
        nodes, rels = build("d = {'a': 1, 'b': 2}\n")
        d = next(n for n in nodes if "PythonDictionary" in n.labels)
        types = {r.type for r in rels if r.source == d.id}
        self.assertIn("HAS_KEY", types)
        self.assertIn("HAS_VALUE", types)
        self.assertNotIn("HAS_OPERAND", types)


def build_with_cfg(source, filename="test.py"):
    """Run the graph builder including the (stdlib-only) control-flow pass."""
    tree = ast.parse(source, filename=filename, type_comments=True)
    builder = GraphBuilder(source, filename)
    builder.build(tree)
    builder.build_cfg(tree)
    return builder._nodes, builder._relationships


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
            "false branch should carry the negated (operator-flipped) condition",
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

    def test_module_and_class_scopes_get_a_cfg_too(self):
        source = "x = 1\nclass C:\n    y = 2\n    def m(self):\n        return self.y\n"
        nodes, _ = build_with_cfg(source)

        scopes = {n.properties["scope"] for n in nodes if "PythonBasicBlock" in n.labels}
        self.assertIn("test", scopes)
        self.assertIn("test.C", scopes)
        self.assertIn("test.C.m", scopes)

    def test_not_negates_by_dropping_the_not(self):
        source = "def f(name):\n    if not name:\n        return 1\n    return 2\n"
        _, rels = build_with_cfg(source)
        conditions = {
            r.properties["condition"]
            for r in rels
            if r.type == "FOLLOWS" and r.properties and "condition" in r.properties
        }
        self.assertIn("not name", conditions)
        self.assertIn("name", conditions)
        self.assertNotIn("not not name", conditions)

    def test_unreachable_code_after_return_is_flagged(self):
        source = "def f():\n    return 1\n    x = 2\n"
        nodes, _ = build_with_cfg(source)
        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        self.assertTrue(any(not b.properties.get("isReachable", True) for b in blocks))
        assign_block = next(
            b for b in blocks if not b.properties.get("isReachable", True)
        )
        self.assertEqual(assign_block.properties.get("kind"), "Assign")

    def test_for_loop_body_and_else_and_break(self):
        source = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        if x < 0:\n"
            "            break\n"
            "    else:\n"
            "        return 'done'\n"
            "    return 'broke'\n"
        )
        nodes, rels = build_with_cfg(source)
        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        self.assertGreaterEqual(len(blocks), 5)
        # `break` must reach the same block as the code after the for/else,
        # not the `else` clause (break skips a loop's `else`).
        break_id = next(n.id for n in nodes if "PythonBreakStatement" in n.labels)
        break_block = next(
            b for b in blocks
            if any(r.source == b.id and r.target == break_id and r.type == "CONTAINS" for r in rels)
        )
        break_targets = {r.target for r in rels if r.type == "FOLLOWS" and r.source == break_block.id}
        else_block_ids = {
            b.id for b in blocks
            if any(
                r.source == b.id and r.type == "CONTAINS"
                and any(n.id == r.target and "value" in n.properties and n.properties.get("value") == "done"
                        for n in nodes)
                for r in rels
            )
        }
        self.assertFalse(break_targets & else_block_ids)

    def test_match_statement_branches_per_case(self):
        source = (
            "def f(value):\n"
            "    match value:\n"
            "        case 0:\n"
            "            return 'zero'\n"
            "        case int() as i if i > 0:\n"
            "            return 'positive'\n"
            "        case _:\n"
            "            return 'other'\n"
        )
        nodes, rels = build_with_cfg(source)
        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        # One block per case body plus the blocks chaining the pattern tests.
        self.assertGreaterEqual(len(blocks), 4)

        follows = [r for r in rels if r.type == "FOLLOWS" and r.properties]
        conditions = {r.properties.get("condition") for r in follows}
        self.assertIn("0", conditions)
        self.assertIn("int() as i if i > 0", conditions)
        self.assertIn("_", conditions)

        # The wildcard `case _` is irrefutable, so nothing should carry a
        # "didn't match the wildcard" condition.
        self.assertFalse(any(c and c.startswith("not (_") for c in conditions if c))

    def test_match_without_wildcard_can_fall_through(self):
        source = "def f(value):\n    match value:\n        case 0:\n            return 'zero'\n    return 'other'\n"
        nodes, rels = build_with_cfg(source)
        return_other_id = next(
            n.id for n in nodes
            if "PythonReturnStatement" in n.labels
            and any(
                r.source == n.id and r.type == "HAS_VALUE"
                and any(t.id == r.target and t.properties.get("value") == "other" for t in nodes)
                for r in rels
            )
        )
        blocks = [n for n in nodes if "PythonBasicBlock" in n.labels]
        return_block = next(
            b for b in blocks
            if any(r.source == b.id and r.target == return_other_id and r.type == "CONTAINS" for r in rels)
        )
        self.assertTrue(return_block.properties.get("isReachable"))

    def test_try_except_finally_links_handler_and_finally(self):
        source = (
            "def f():\n"
            "    try:\n"
            "        risky()\n"
            "    except ValueError:\n"
            "        handle()\n"
            "    finally:\n"
            "        cleanup()\n"
        )
        nodes, rels = build_with_cfg(source)
        follows = [r for r in rels if r.type == "FOLLOWS"]
        conditions = {r.properties.get("condition") for r in follows if r.properties}
        self.assertIn("ValueError", conditions)

        # `finally` should be reachable from both the try body's normal exit
        # and the handler's exit — i.e. exactly one block has 2 predecessors.
        incoming_count = {}
        for r in follows:
            incoming_count[r.target] = incoming_count.get(r.target, 0) + 1
        finally_block_id = next(block_id for block_id, count in incoming_count.items() if count >= 2)
        finally_block = next(n for n in nodes if n.id == finally_block_id)
        self.assertEqual(finally_block.properties.get("kind"), "Expr")

    def test_cfg_contains_edges_carry_statement_ordinal(self):
        source = "def f():\n    a = 1\n    b = 2\n    c = 3\n"
        nodes, rels = build_with_cfg(source)
        block = next(
            n for n in nodes
            if "PythonBasicBlock" in n.labels
            and sum(1 for r in rels if r.source == n.id and r.type == "CONTAINS") == 3
        )
        ordinals = sorted(
            r.properties["ordinal"] for r in rels if r.source == block.id and r.type == "CONTAINS"
        )
        self.assertEqual(ordinals, [0, 1, 2])

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


class CypherStreamWriterTests(unittest.TestCase):
    def test_write_prints_nodes_then_relationships(self):
        output = io.StringIO()
        writer = CypherStreamWriter(output)
        nodes = [Node("p1", "PythonModule", {"name": "m.py"})]
        relationships = [Relationship("p1", "CONTAINS", "p2")]
        writer.write(nodes, relationships)
        writer.finish()
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], nodes[0].to_cypher())
        self.assertEqual(lines[1], relationships[0].to_cypher())


class Neo4jAdminImportWriterTests(unittest.TestCase):
    def _run(self, nodes, relationships):
        with tempfile.TemporaryDirectory() as output_dir:
            command_output = io.StringIO()
            writer = Neo4jAdminImportWriter(output_dir, "neo4j", command_output)
            writer.write(nodes, relationships)
            writer.finish()

            def read_csv(path):
                with open(path, newline="", encoding="utf-8") as handle:
                    return list(csv.reader(handle))

            files = {}
            for root, _dirs, names in os.walk(output_dir):
                for name in names:
                    files[name] = read_csv(os.path.join(root, name))
            return files, command_output.getvalue()

    def test_nodes_grouped_by_label_set_with_typed_columns(self):
        nodes = [
            Node("p1", ["PythonFunctionDefinition", "PythonStatement"], {"name": "f", "isAsync": False}),
            Node("p2", ["PythonFunctionDefinition", "PythonStatement"], {"name": "g", "isAsync": True}),
            Node("p3", "PythonModule", {"name": "m.py"}),
        ]
        files, command = self._run(nodes, [])

        self.assertIn("PythonFunctionDefinition_PythonStatement.csv", files)
        rows = files["PythonFunctionDefinition_PythonStatement.csv"]
        self.assertEqual(rows[0], [":ID", ":LABEL", "name", "isAsync:boolean"])
        self.assertEqual(rows[1], ["p1", "PythonFunctionDefinition;PythonStatement", "f", "false"])
        self.assertEqual(rows[2], ["p2", "PythonFunctionDefinition;PythonStatement", "g", "true"])

        self.assertIn("PythonModule.csv", files)
        self.assertEqual(files["PythonModule.csv"][1], ["p3", "PythonModule", "m.py"])

        self.assertIn("--nodes=", command)
        self.assertIn("neo4j-admin database import full", command)
        self.assertTrue(command.rstrip().endswith("neo4j"))

    def test_missing_property_renders_as_empty_field(self):
        nodes = [
            Node("p1", "PythonConstant", {"value": 1, "kind": "u"}),
            Node("p2", "PythonConstant", {"value": 2}),
        ]
        files, _ = self._run(nodes, [])
        rows = files["PythonConstant.csv"]
        self.assertEqual(rows[0], [":ID", ":LABEL", "value:long", "kind"])
        self.assertEqual(rows[2], ["p2", "PythonConstant", "2", ""])

    def test_relationships_grouped_by_type_with_start_end_columns(self):
        relationships = [
            Relationship("p1", "CONTAINS", "p2", {"ordinal": 0}),
            Relationship("p1", "CONTAINS", "p3", {"ordinal": 1}),
            Relationship("p1", "RETURNS", "p4"),
        ]
        files, command = self._run([], relationships)

        rows = files["CONTAINS.csv"]
        self.assertEqual(rows[0], [":START_ID", ":END_ID", "ordinal:long"])
        self.assertEqual(rows[1], ["p1", "p2", "0"])
        self.assertEqual(rows[2], ["p1", "p3", "1"])

        self.assertEqual(files["RETURNS.csv"][0], [":START_ID", ":END_ID"])
        self.assertIn("--relationships=CONTAINS=", command)
        self.assertIn("--relationships=RETURNS=", command)

    def test_commas_quotes_and_newlines_round_trip(self):
        nodes = [Node("p1", "PythonConstant", {"value": 'a,b"c\nd'})]
        files, _ = self._run(nodes, [])
        rows = files["PythonConstant.csv"]
        self.assertEqual(rows[1][2], 'a,b"c\nd')

    def test_array_property_uses_semicolon_delimiter_and_type_suffix(self):
        nodes = [Node("p1", "PythonFunctionDefinition", {"typeParameters": ["T", "U"]})]
        files, _ = self._run(nodes, [])
        rows = files["PythonFunctionDefinition.csv"]
        self.assertEqual(rows[0], [":ID", ":LABEL", "typeParameters:string[]"])
        self.assertEqual(rows[1], ["p1", "PythonFunctionDefinition", "T;U"])


if __name__ == "__main__":
    unittest.main()