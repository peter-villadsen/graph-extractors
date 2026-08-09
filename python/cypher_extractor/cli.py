"""Command line entry point for the Python -> Cypher extractor."""

from __future__ import annotations

import argparse
import ast
import os
import sys

from .ast_graph import GraphBuilder
from .souffle import SouffleProgram

EXTENSIONS = (".py",)


def collect_files(paths, extensions):
    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                for name in sorted(names):
                    if name.lower().endswith(extensions):
                        files.append(os.path.join(root, name))
        elif os.path.isfile(path):
            files.append(path)
        else:
            print(f"warning: no such file or directory: {path}", file=sys.stderr)
    return sorted(set(files))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cypher_extractor",
        description=(
            "Parse Python source with the standard library `ast` module and emit a "
            "Cypher graph (Neo4j) representation of the code on stdout."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Python source files or directories to scan",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="print progress information to stderr",
    )
    parser.add_argument(
        "--ast",
        "-a",
        dest="print_ast",
        action="store_true",
        help="print the parsed AST to stderr before generating Cypher",
    )
    parser.add_argument(
        "--cfg",
        dest="emit_cfg",
        action="store_true",
        help="also emit control-flow-graph basic blocks",
    )
    parser.add_argument(
        "--souffle",
        dest="emit_souffle",
        action="store_true",
        help=(
            "emit a Souffle datalog program for the control-flow graphs on stdout "
            "instead of Cypher (implies --cfg)"
        ),
    )
    args = parser.parse_args(argv)

    files = collect_files(args.paths, EXTENSIONS)
    if not files:
        print("error: no Python source files found", file=sys.stderr)
        return 2

    souffle = SouffleProgram() if args.emit_souffle else None
    next_id = 0
    for path in files:
        if args.verbose:
            print(f"[verbose] reading {path}", file=sys.stderr)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            continue

        try:
            tree = ast.parse(source, filename=path, type_comments=True)
        except SyntaxError as exc:
            print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
            continue

        if args.print_ast:
            print(f"[ast] {path}", file=sys.stderr)
            print(ast.dump(tree, indent=2, include_attributes=False), file=sys.stderr)

        builder = GraphBuilder(source, path, next_id)
        builder.build(tree)
        next_id = builder._counter

        if args.emit_cfg or args.emit_souffle:
            try:
                builder.build_cfg(tree)
            except RuntimeError as exc:
                print(f"error: {path}: {exc}", file=sys.stderr)
                continue
            next_id = builder._counter

        if args.emit_souffle:
            souffle.add_cfg(builder._cfg_blocks, builder._cfg_edges, builder._nodes)
            continue

        if args.verbose:
            print(
                f"[verbose] {path}: {len(builder._nodes)} nodes, "
                f"{len(builder._relationships)} relationships",
                file=sys.stderr,
            )

        for node in builder._nodes:
            print(node.to_cypher())
        for relationship in builder._relationships:
            print(relationship.to_cypher())

    if souffle is not None:
        print(souffle.render(), end="")

    return 0
