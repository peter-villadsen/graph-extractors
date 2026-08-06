using System.Text;

namespace CSharpToCypher;

/// <summary>
/// Renders control-flow graphs as a Souffle datalog program: one fact per
/// basic block and CFG edge, plus standard reachability and transitive-path
/// rules. Block/statement ids are remapped to run-global b1/s1 ids so several
/// files can share a single program. See https://souffle-lang.github.io/.
/// </summary>
public sealed class SouffleWriter
{
    private readonly List<(string Id, string Function, int Ordinal, bool IsEntry, bool IsFinal)> _blocks = new();
    private readonly List<(string Source, string Target, string Function, string Condition)> _edges = new();
    private readonly List<(string Block, string Statement, string Kind, string Function)> _blockStatements = new();
    private int _blockCounter;
    private int _statementCounter;

    /// <summary>Adds one file's CFG (from the Cypher document) to the program.</summary>
    public void AddCfg(CypherDocument document)
    {
        var blockNodes = document.Nodes.Where(n => n.Labels.Contains("CSharpBasicBlock")).ToList();
        if (blockNodes.Count == 0)
        {
            return;
        }

        var labelsById = document.Nodes
            .Where(n => n.Labels.Count > 0)
            .ToDictionary(n => n.Id, n => n.Labels[0]);
        var scopeById = blockNodes.ToDictionary(
            n => n.Id,
            n => n.Properties.TryGetValue("scope", out var scope) ? scope as string ?? "" : "");

        var idMap = new Dictionary<string, string>();
        foreach (var node in blockNodes)
        {
            var datalogId = $"b{++_blockCounter}";
            idMap[node.Id] = datalogId;
            var ordinal = node.Properties.TryGetValue("ordinal", out var o) && o is int oi ? oi : 0;
            var isEntry = node.Properties.TryGetValue("isEntry", out var e) && e is bool eb && eb;
            var isFinal = node.Properties.TryGetValue("isFinal", out var f) && f is bool fb && fb;
            _blocks.Add((datalogId, scopeById[node.Id], ordinal, isEntry, isFinal));
        }

        foreach (var node in blockNodes)
        {
            var function = scopeById[node.Id];
            foreach (var rel in document.Relationships.Where(r => r.Type == "CONTAINS" && r.Source == node.Id))
            {
                var statementId = $"s{++_statementCounter}";
                var kind = labelsById.TryGetValue(rel.Target, out var k) ? k : "CSharpStatement";
                _blockStatements.Add((idMap[node.Id], statementId, kind, function));
            }
        }

        foreach (var rel in document.Relationships.Where(r => r.Type == "FOLLOWS"))
        {
            if (!idMap.TryGetValue(rel.Source, out var source) || !idMap.TryGetValue(rel.Target, out var target))
            {
                continue;
            }
            var condition = "";
            if (rel.Properties is not null && rel.Properties.TryGetValue("semantics", out var semantics))
            {
                condition = semantics as string ?? "";
            }
            _edges.Add((source, target, scopeById[rel.Source], condition));
        }
    }

    /// <summary>Renders the accumulated facts as one self-contained datalog program.</summary>
    public string Render()
    {
        var sb = new StringBuilder();
        sb.AppendLine(".decl basic_block(id: symbol, function: symbol, ordinal: number, isEntry: number, isFinal: number)");
        sb.AppendLine(".decl control_flow_edge(source: symbol, target: symbol, function: symbol, condition: symbol)");
        sb.AppendLine(".decl block_statement(block: symbol, statement: symbol, statement_kind: symbol, function: symbol)");
        sb.AppendLine(".decl reachable(id: symbol, function: symbol)");
        sb.AppendLine(".decl path(source: symbol, target: symbol, function: symbol)");
        sb.AppendLine();

        foreach (var (id, function, ordinal, isEntry, isFinal) in _blocks)
        {
            sb.AppendLine($"basic_block({Sym(id)}, {Sym(function)}, {ordinal}, {(isEntry ? 1 : 0)}, {(isFinal ? 1 : 0)}).");
        }

        foreach (var (source, target, function, condition) in _edges)
        {
            sb.AppendLine($"control_flow_edge({Sym(source)}, {Sym(target)}, {Sym(function)}, {Sym(condition)}).");
        }

        foreach (var (block, statement, kind, function) in _blockStatements)
        {
            sb.AppendLine($"block_statement({Sym(block)}, {Sym(statement)}, {Sym(kind)}, {Sym(function)}).");
        }

        sb.AppendLine();
        sb.AppendLine("reachable(Id, Function) :- basic_block(Id, Function, _, 1, _).");
        sb.AppendLine("reachable(Target, Function) :- reachable(Source, Function), control_flow_edge(Source, Target, Function, _).");
        sb.AppendLine();
        sb.AppendLine("path(Block, Block, Function) :- basic_block(Block, Function, _, _, _).");
        sb.AppendLine("path(Source, Target, Function) :- path(Source, Mid, Function), control_flow_edge(Mid, Target, Function, _).");
        sb.AppendLine();
        sb.AppendLine(".output reachable");
        sb.AppendLine(".output path");

        return sb.ToString();
    }

    private static string Sym(string value)
    {
        var sb = new StringBuilder(value.Length + 2);
        sb.Append('"');
        foreach (var ch in value)
        {
            if (ch is '\\' or '"')
            {
                sb.Append('\\');
            }
            sb.Append(ch);
        }
        sb.Append('"');
        return sb.ToString();
    }
}
