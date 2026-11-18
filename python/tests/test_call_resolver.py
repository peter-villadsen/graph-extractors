"""Tests for best-effort (never guess) CALLS resolution."""

import ast
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cypher_extractor import cli
from cypher_extractor.ast_graph import GraphBuilder
from cypher_extractor.call_resolver import CallResolver


def resolve(*modules):
    """Build each ``(module_name, source[, is_package])`` like the CLI does,
    sharing one resolver, and return the CALLS edges as sorted
    ``(caller qualname, callee qualname, count)`` triples."""
    resolver = CallResolver()
    nodes = {}
    next_id = 0
    for module in modules:
        name, source = module[0], module[1]
        is_package = module[2] if len(module) > 2 else False
        tree = ast.parse(source)
        builder = GraphBuilder(source, name.replace(".", "/") + ".py", next_id, module_name=name)
        builder.build(tree)
        next_id = builder._counter
        nodes.update({n.id: n for n in builder._nodes})
        resolver.add_module(name, is_package, tree, builder._node_ids)
    edges = []
    for rel in resolver.relationships():
        assert rel.type == "CALLS"
        edges.append(
            (
                nodes[rel.source].properties["qualname"],
                nodes[rel.target].properties["qualname"],
                rel.properties["count"],
            )
        )
    return sorted(edges)


class ResolvedCallTests(unittest.TestCase):
    """One test per row of the resolution table."""

    def test_module_level_def(self):
        source = "def f():\n    pass\n\ndef g():\n    f()\n"
        self.assertEqual(resolve(("m", source)), [("m.g", "m.f", 1)])

    def test_nested_def_in_enclosing_function(self):
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        pass\n"
            "    def sibling():\n"
            "        inner()\n"
            "    inner()\n"
        )
        self.assertEqual(
            resolve(("m", source)),
            [("m.outer", "m.outer.inner", 1), ("m.outer.sibling", "m.outer.inner", 1)],
        )

    def test_self_method_defined_in_own_class(self):
        source = "class C:\n    def m(self):\n        pass\n    def n(self):\n        self.m()\n"
        self.assertEqual(resolve(("m", source)), [("m.C.n", "m.C.m", 1)])

    def test_cls_method_in_classmethod(self):
        source = (
            "class C:\n"
            "    @classmethod\n"
            "    def m(cls):\n"
            "        pass\n"
            "    @classmethod\n"
            "    def n(cls):\n"
            "        cls.m()\n"
        )
        self.assertEqual(resolve(("m", source)), [("m.C.n", "m.C.m", 1)])

    def test_class_dot_method(self):
        source = "class C:\n    @staticmethod\n    def m():\n        pass\n\ndef g():\n    C.m()\n"
        self.assertEqual(resolve(("m", source)), [("m.g", "m.C.m", 1)])

    def test_constructor_call_targets_init(self):
        source = "class C:\n    def __init__(self):\n        pass\n\ndef g():\n    C()\n"
        self.assertEqual(resolve(("m", source)), [("m.g", "m.C.__init__", 1)])

    def test_from_import(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg.b", "from pkg.a import f\n\ndef g():\n    f()\n"),
            ),
            [("pkg.b.g", "pkg.a.f", 1)],
        )

    def test_from_import_as(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg.b", "from pkg.a import f as h\n\ndef g():\n    h()\n"),
            ),
            [("pkg.b.g", "pkg.a.f", 1)],
        )

    def test_relative_from_import(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg.b", "from .a import f\n\ndef g():\n    f()\n"),
            ),
            [("pkg.b.g", "pkg.a.f", 1)],
        )

    def test_relative_import_from_package_init(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg", "from .a import f\n\ndef g():\n    f()\n", True),
            ),
            [("pkg.g", "pkg.a.f", 1)],
        )

    def test_import_module_as_alias(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg.b", "import pkg.a as mod\n\ndef g():\n    mod.f()\n"),
            ),
            [("pkg.b.g", "pkg.a.f", 1)],
        )

    def test_import_dotted_module(self):
        self.assertEqual(
            resolve(
                ("pkg.a", "def f():\n    pass\n"),
                ("pkg.b", "import pkg.a\n\ndef g():\n    pkg.a.f()\n"),
            ),
            [("pkg.b.g", "pkg.a.f", 1)],
        )


class CallerTests(unittest.TestCase):
    def test_module_level_call_has_the_module_as_caller(self):
        resolver = CallResolver()
        source = "def f():\n    pass\n\nf()\n"
        tree = ast.parse(source)
        builder = GraphBuilder(source, "m.py", module_name="m")
        builder.build(tree)
        resolver.add_module("m", False, tree, builder._node_ids)
        rel = resolver.relationships()[0]
        caller = next(n for n in builder._nodes if n.id == rel.source)
        self.assertEqual(caller.labels, ["PythonModule"])
        self.assertEqual(resolve(("m", source)), [("m", "m.f", 1)])

    def test_class_body_call_has_the_class_as_caller(self):
        source = "def f():\n    return 1\n\nclass C:\n    x = f()\n"
        self.assertEqual(resolve(("m", source)), [("m.C", "m.f", 1)])

    def test_same_call_twice_is_one_edge_with_count(self):
        source = "def f():\n    pass\n\ndef g():\n    f()\n    f()\n"
        self.assertEqual(resolve(("m", source)), [("m.g", "m.f", 2)])


class UnresolvedCallTests(unittest.TestCase):
    """Never guess: each of these must produce no edge at all."""

    def assertNoEdges(self, *modules):
        self.assertEqual(resolve(*modules), [])

    def test_builtins(self):
        self.assertNoEdges(("m", "def g(xs):\n    print(len(xs))\n"))

    def test_method_call_on_a_parameter(self):
        self.assertNoEdges(("m", "class C:\n    def m(self):\n        pass\n\ndef g(obj):\n    obj.m()\n"))

    def test_inherited_method_only_on_base_class(self):
        source = (
            "class B:\n"
            "    def m(self):\n"
            "        pass\n"
            "class C(B):\n"
            "    def n(self):\n"
            "        self.m()\n"
        )
        self.assertNoEdges(("m", source))

    def test_name_shadowed_by_a_local_assignment(self):
        self.assertNoEdges(("m", "def f():\n    pass\n\ndef g():\n    f = lambda: 1\n    f()\n"))

    def test_name_shadowed_by_a_parameter(self):
        self.assertNoEdges(("m", "def f():\n    pass\n\ndef g(f):\n    f()\n"))

    def test_getattr(self):
        self.assertNoEdges(("m", "class C:\n    def m(self):\n        pass\n\ndef g(c):\n    getattr(c, 'm')()\n"))

    def test_conditional_definition(self):
        self.assertNoEdges(("m", "import sys\nif sys.argv:\n    def f():\n        pass\n\ndef g():\n    f()\n"))

    def test_redefined_name(self):
        self.assertNoEdges(("m", "def f():\n    pass\n\ndef f():\n    pass\n\ndef g():\n    f()\n"))

    def test_name_rebound_through_global(self):
        source = "def f():\n    pass\n\ndef h():\n    global f\n    f = None\n\ndef g():\n    f()\n"
        self.assertNoEdges(("m", source))

    def test_decorated_function(self):
        self.assertNoEdges(("m", "def deco(fn):\n    return fn\n\n@deco\ndef f():\n    pass\n\ndef g():\n    f()\n"))

    def test_call_through_a_container(self):
        self.assertNoEdges(("m", "def f():\n    pass\n\nTABLE = {'f': f}\n\ndef g():\n    TABLE['f']()\n"))

    def test_star_import(self):
        self.assertNoEdges(
            ("pkg.a", "def f():\n    pass\n"),
            ("pkg.b", "from pkg.a import *\n\ndef g():\n    f()\n"),
        )

    def test_star_import_may_replace_a_local_def(self):
        self.assertNoEdges(
            ("pkg.a", "def f():\n    pass\n"),
            ("pkg.b", "def f():\n    pass\n\nfrom pkg.a import *\n\ndef g():\n    f()\n"),
        )

    def test_module_level_call_before_the_def(self):
        self.assertNoEdges(("m", "f()\n\ndef f():\n    pass\n"))

    def test_import_from_a_module_outside_the_run(self):
        self.assertNoEdges(("m", "from os.path import join\n\ndef g():\n    join('a', 'b')\n"))

    def test_class_without_own_init(self):
        self.assertNoEdges(("m", "class C:\n    pass\n\ndef g():\n    C()\n"))

    def test_method_replaced_by_attribute_assignment(self):
        source = (
            "class C:\n"
            "    def m(self):\n"
            "        pass\n"
            "    def n(self):\n"
            "        self.m()\n"
            "\n"
            "C.m = print\n"
        )
        self.assertNoEdges(("m", source))


def cli_calls(root):
    """Run the CLI (Cypher mode) on ``root`` and return its CALLS edges as
    sorted ``(caller qualname, callee qualname)`` pairs."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = cli.main([root])
    if status != 0:
        raise AssertionError(f"cli exited with {status}")
    text = out.getvalue()
    qualname_by_id = {}
    for line in text.splitlines():
        if line.startswith("CREATE ("):
            qualname = re.search(r"qualname: '([^']*)'", line)
            node_id = re.search(r"\bid: '([^']*)'\}\);$", line)
            if qualname and node_id:
                qualname_by_id[node_id.group(1)] = qualname.group(1)
    calls = re.findall(r"MATCH \(a \{id: '(\w+)'\}\), \(b \{id: '(\w+)'\}\) CREATE \(a\)-\[:CALLS ", text)
    return sorted((qualname_by_id[a], qualname_by_id[b]) for a, b in calls)


class CliCallTests(unittest.TestCase):
    def _write(self, root, relative, text):
        path = os.path.join(root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_call_across_files_through_the_cli(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "pkg/a.py", "def f():\n    pass\n")
            self._write(root, "pkg/b.py", "from pkg.a import f\n\ndef g():\n    f()\n")
            self.assertEqual(cli_calls(root), [("pkg.b.g", "pkg.a.f")])

    def test_relative_import_across_files_through_the_cli(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "pkg/__init__.py", "")
            self._write(root, "pkg/a.py", "def f():\n    pass\n")
            self._write(root, "pkg/b.py", "from .a import f\n\ndef g():\n    f()\n")
            self.assertEqual(cli_calls(root), [("pkg.b.g", "pkg.a.f")])


if __name__ == "__main__":
    unittest.main()
