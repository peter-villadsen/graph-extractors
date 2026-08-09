"""Command line entry point for the Python -> Cypher extractor."""

from __future__ import annotations

import argparse
import ast
import os
import sys

from .ast_graph import GraphBuilder
from .souffle import SouffleProgram
from .writers import CypherStreamWriter, Neo4jAdminImportWriter

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
    parser.add_argument(
        "--format",
        choices=["cypher", "csv"],
        default="cypher",
        help=(
            "'cypher' (default): stream CREATE/MATCH statements to stdout, one "
            "autocommitted transaction per statement. 'csv': write neo4j-admin "
            "bulk-import CSV files under --output-dir (one per node label set / "
            "relationship type) and print the ready-to-run 'neo4j-admin database "
            "import' command to stdout -- loads a large codebase into an empty "
            "database in seconds instead of many small (or one huge) transactions"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="neo4j-import",
        help="directory for the CSV files (--format csv only, default: neo4j-import)",
    )
    parser.add_argument(
        "--database",
        default="neo4j",
        help="database name for the generated import command (--format csv only, default: neo4j)",
    )
    args = parser.parse_args(argv)

    files = collect_files(args.paths, EXTENSIONS)
    if not files:
        print("error: no Python source files found", file=sys.stderr)
        return 2

    souffle = SouffleProgram() if args.emit_souffle else None
    if args.format == "csv":
        writer = Neo4jAdminImportWriter(args.output_dir, args.database, sys.stdout)
    else:
        writer = CypherStreamWriter(sys.stdout)
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

        writer.write(builder._nodes, builder._relationships)

    if souffle is not None:
        print(souffle.render(), end="")
    else:
        writer.finish()

    return 0
