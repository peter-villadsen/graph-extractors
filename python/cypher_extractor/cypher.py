"""Cypher emission: escaping helpers plus the in-memory graph model.

The graph model is intentionally language-agnostic: a ``Node`` carries one or
more labels (the language-endemic construct names) and a property map; a
``Relationship`` carries the relationship type and optional properties. Both
know how to render themselves as Neo4j Cypher ``CREATE`` statements.
"""

from __future__ import annotations


def escape_string(value: str) -> str:
    """Escape a string for use inside a single-quoted Cypher string literal."""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def format_value(value):
    """Render a Python value as a Cypher literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + escape_string(value) + "'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    return "'" + escape_string(str(value)) + "'"


class Node:
    """A Cypher node. ``labels`` may be a single string or an iterable of them."""

    __slots__ = ("id", "labels", "properties")

    def __init__(self, node_id, labels, properties=None):
        if isinstance(labels, str):
            labels = [labels]
        self.id = node_id
        self.labels = list(labels)
        self.properties = dict(properties or {})

    def to_cypher(self) -> str:
        props = dict(self.properties)
        props["id"] = self.id
        label_text = ":".join(self.labels)
        prop_text = ", ".join(f"{k}: {format_value(v)}" for k, v in props.items())
        return f"CREATE ({self.id}:{label_text} {{{prop_text}}});"


class Relationship:
    """A directed Cypher relationship between two node ids."""

    __slots__ = ("source", "type", "target", "properties")

    def __init__(self, source, type_, target, properties=None):
        self.source = source
        self.type = type_
        self.target = target
        self.properties = dict(properties or {})

    def to_cypher(self) -> str:
        if self.properties:
            prop_text = (
                " {"
                + ", ".join(f"{k}: {format_value(v)}" for k, v in self.properties.items())
                + "}"
            )
        else:
            prop_text = ""
        # Node and relationship statements are separate, independently
        # autocommitted CREATE statements (see Node.to_cypher), so a bare
        # variable like ``p1`` is not bound here - it would silently create a
        # fresh, disconnected node instead of referencing the one created
        # earlier. Look both endpoints up by their ``id`` property instead.
        source_id = format_value(self.source)
        target_id = format_value(self.target)
        return (
            f"MATCH (a {{id: {source_id}}}), (b {{id: {target_id}}}) "
            f"CREATE (a)-[:{self.type}{prop_text}]->(b);"
        )
