"""Output writers: render the extracted graph as either streamed Cypher
``CREATE``/``MATCH`` statements, or as a set of ``neo4j-admin`` bulk-import
CSV files plus the ready-to-run import command.

Streaming Cypher text scales poorly on large codebases: splitting the run
into many small transactions risks a half-imported graph, and one huge
transaction can take hours. ``neo4j-admin database import`` instead reads a
set of CSV files (one per node label set / relationship type, linked by
id columns) directly into an empty database in seconds. Both writers accept
one module's ``(nodes, relationships)`` at a time via :meth:`write`, keeping
call sites in ``cli.py`` identical regardless of format; :meth:`finish` runs
once after every file, which is where the CSV writer groups and renders.
"""

from __future__ import annotations

import csv
import os

_ARRAY_DELIMITER = ";"


class CypherStreamWriter:
    """Streams Cypher CREATE/MATCH statements to ``output`` as each module is built — the original, transaction-per-statement output mode."""

    def __init__(self, output):
        self._output = output

    def write(self, nodes, relationships) -> None:
        for node in nodes:
            print(node.to_cypher(), file=self._output)
        for relationship in relationships:
            print(relationship.to_cypher(), file=self._output)

    def finish(self) -> None:
        pass


def _infer_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, (list, tuple)):
        return "string[]"
    return "string"


def _format_csv_value(value) -> str:
    """Render by the value's own type, independent of the column's declared
    (possibly downgraded-to-string) type -- a "string" column still needs
    "true"/"false" for a bool, not str()'s "True"/"False".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return _ARRAY_DELIMITER.join("" if v is None else str(v) for v in value)
    return str(value)


def _collect_schema(rows):
    """Union of property keys across every row in a group, in first-seen order,
    with a Neo4j type per key inferred from its values. A property whose type
    varies from row to row (e.g. ``ast.Constant.value``, which is sometimes an
    int, sometimes a string) falls back to "string" -- the one type every
    value can be rendered as -- rather than emitting a column whose declared
    type doesn't match every row, which neo4j-admin rejects outright.
    """
    columns = []
    seen = set()
    types = {}
    for properties in rows:
        for key, value in properties.items():
            if value is None:
                continue
            if key not in seen:
                seen.add(key)
                columns.append(key)
            inferred = _infer_type(value)
            existing = types.get(key)
            if existing is None:
                types[key] = inferred
            elif existing != inferred:
                types[key] = "string"
    return columns, types


def _header_names(columns, types):
    return [key if types[key] == "string" else f"{key}:{types[key]}" for key in columns]


def _sanitize_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)


def _quote_arg(path: str) -> str:
    return f'"{path}"' if " " in path else path


class Neo4jAdminImportWriter:
    """Buffers nodes/relationships across every input file, grouped by label
    set (nodes) or relationship type, and at :meth:`finish` writes one
    ``neo4j-admin database import`` CSV file per group under ``output_dir``,
    then prints the ready-to-run import command that loads them all into an
    empty database in one bulk pass.
    """

    def __init__(self, output_dir, database, command_output):
        self._output_dir = output_dir
        self._database = database
        self._command_output = command_output
        self._nodes_by_group = {}
        self._node_group_order = []
        self._relationships_by_type = {}
        self._relationship_type_order = []

    def write(self, nodes, relationships) -> None:
        for node in nodes:
            key = ":".join(node.labels)
            if key not in self._nodes_by_group:
                self._nodes_by_group[key] = []
                self._node_group_order.append(key)
            self._nodes_by_group[key].append(node)
        for relationship in relationships:
            if relationship.type not in self._relationships_by_type:
                self._relationships_by_type[relationship.type] = []
                self._relationship_type_order.append(relationship.type)
            self._relationships_by_type[relationship.type].append(relationship)

    def finish(self) -> None:
        os.makedirs(self._output_dir, exist_ok=True)

        node_paths = [
            self._write_node_file(key, self._nodes_by_group[key]) for key in self._node_group_order
        ]
        relationship_paths = [
            (rel_type, self._write_relationship_file(rel_type, self._relationships_by_type[rel_type]))
            for rel_type in self._relationship_type_order
        ]

        print(self._build_command(node_paths, relationship_paths), file=self._command_output)

    def _write_node_file(self, group_key, nodes) -> str:
        columns, types = _collect_schema(node.properties for node in nodes)

        directory = os.path.join(self._output_dir, "nodes")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, _sanitize_filename(group_key) + ".csv")

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([":ID", ":LABEL"] + _header_names(columns, types))
            for node in nodes:
                row = [node.id, _ARRAY_DELIMITER.join(node.labels)]
                row += [_format_csv_value(node.properties.get(key)) for key in columns]
                writer.writerow(row)
        return path

    def _write_relationship_file(self, rel_type, relationships) -> str:
        columns, types = _collect_schema(relationship.properties for relationship in relationships)

        directory = os.path.join(self._output_dir, "relationships")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, _sanitize_filename(rel_type) + ".csv")

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([":START_ID", ":END_ID"] + _header_names(columns, types))
            for relationship in relationships:
                row = [relationship.source, relationship.target]
                row += [_format_csv_value(relationship.properties.get(key)) for key in columns]
                writer.writerow(row)
        return path

    def _build_command(self, node_paths, relationship_paths) -> str:
        # <database> must come right after 'full', not at the end (even though
        # that's the order --help shows): neo4j-admin's --nodes/--relationships
        # are unbounded multi-valued options, so a trailing bare "neo4j" gets
        # parsed as one more file in the last --relationships list instead of
        # the positional database argument, and the import fails outright.
        parts = ["neo4j-admin", "database", "import", "full", self._database, "--multiline-fields=true"]
        parts += [f"--nodes={_quote_arg(path)}" for path in node_paths]
        parts += [f"--relationships={rel_type}={_quote_arg(path)}" for rel_type, path in relationship_paths]
        return " ".join(parts)
