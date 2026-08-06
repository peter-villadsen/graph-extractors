"""Emit a Souffle datalog program describing control-flow graphs.

The program contains inline facts for every basic block and control-flow edge
found by the extractor, plus a few standard analysis rules — reachability from
the entry block and transitive path closure — that can be queried with
``.output``. See https://souffle-lang.github.io/.

``SouffleProgram`` accumulates facts across several files (block/statement ids
are remapped to run-global ``b1``/``s1`` ids so the result is one valid
program), while the module-level ``emit_souffle`` helper renders a single CFG.
"""

from __future__ import annotations

_DECLS = (
    ".decl basic_block(id: symbol, function: symbol, ordinal: number, isEntry: number, isFinal: number)",
    ".decl control_flow_edge(source: symbol, target: symbol, function: symbol, condition: symbol)",
    ".decl block_statement(block: symbol, statement: symbol, statement_kind: symbol, function: symbol)",
    ".decl reachable(id: symbol, function: symbol)",
    ".decl path(source: symbol, target: symbol, function: symbol)",
)

_RULES = (
    "reachable(Id, Function) :- basic_block(Id, Function, _, 1, _).",
    "reachable(Target, Function) :- reachable(Source, Function), control_flow_edge(Source, Target, Function, _).",
    "path(Block, Block, Function) :- basic_block(Block, Function, _, _, _).",
    "path(Source, Target, Function) :- path(Source, Mid, Function), control_flow_edge(Mid, Target, Function, _).",
)

_OUTPUTS = (
    ".output reachable",
    ".output path",
)


def _sym(value) -> str:
    """Render a Python value as a Souffle double-quoted symbol literal."""
    text = "" if value is None else str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SouffleProgram:
    """Accumulates CFG facts from one or more modules and renders a program."""

    def __init__(self):
        self._blocks = []
        self._edges = []
        self._block_statements = []
        self._block_counter = 0
        self._statement_counter = 0

    def add_cfg(self, blocks, edges, nodes=None):
        """Add one module's CFG model, remapping ids to run-global values.

        ``blocks`` is a list of dicts with keys ``id``, ``function``,
        ``ordinal``, ``isEntry``, ``isFinal``, ``statementIds``; ``edges`` a
        list of dicts with keys ``source``, ``target``, ``function``,
        ``condition``; ``nodes`` the builder's Cypher nodes (for statement
        kinds).
        """
        node_labels = {}
        if nodes:
            node_labels = {node.id: node.labels[0] for node in nodes if node.labels}

        id_map = {}
        for block in blocks:
            self._block_counter += 1
            datalog_id = f"b{self._block_counter}"
            id_map[block["id"]] = datalog_id
            self._blocks.append(
                (
                    datalog_id,
                    block["function"],
                    block["ordinal"],
                    block["isEntry"],
                    block["isFinal"],
                )
            )

        for block in blocks:
            for statement_id in block["statementIds"]:
                self._statement_counter += 1
                self._block_statements.append(
                    (
                        id_map[block["id"]],
                        f"s{self._statement_counter}",
                        node_labels.get(statement_id, "Statement"),
                        block["function"],
                    )
                )

        for edge in edges:
            if edge["source"] not in id_map or edge["target"] not in id_map:
                continue
            self._edges.append(
                (
                    id_map[edge["source"]],
                    id_map[edge["target"]],
                    edge["function"],
                    edge["condition"],
                )
            )

    def render(self) -> str:
        """Render the accumulated facts as one self-contained datalog program."""
        lines = list(_DECLS) + [""]
        for block_id, function, ordinal, is_entry, is_final in self._blocks:
            lines.append(
                f"basic_block({_sym(block_id)}, {_sym(function)}, {ordinal}, "
                f"{1 if is_entry else 0}, {1 if is_final else 0})."
            )
        for source, target, function, condition in self._edges:
            lines.append(
                f"control_flow_edge({_sym(source)}, {_sym(target)}, {_sym(function)}, "
                f"{_sym(condition)})."
            )
        for block, statement, kind, function in self._block_statements:
            lines.append(
                f"block_statement({_sym(block)}, {_sym(statement)}, {_sym(kind)}, "
                f"{_sym(function)})."
            )
        lines += [""] + list(_RULES) + [""] + list(_OUTPUTS) + [""]
        return "\n".join(lines)


def emit_souffle(blocks, edges, nodes=None) -> str:
    """Render a single CFG model (dicts as for ``SouffleProgram.add_cfg``)."""
    program = SouffleProgram()
    program.add_cfg(blocks, edges, nodes)
    return program.render()
