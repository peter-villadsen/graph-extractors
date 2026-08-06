using System.Globalization;
using System.Text;

namespace CSharpToCypher;

/// <summary>Cypher emission: escaping, value formatting and the graph model.</summary>
public static class Cypher
{
    public static string EscapeString(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (var ch in value)
        {
            switch (ch)
            {
                case '\\': sb.Append("\\\\"); break;
                case '\'': sb.Append("\\'"); break;
                case '"': sb.Append("\\\""); break;
                case '\r': sb.Append("\\r"); break;
                case '\n': sb.Append("\\n"); break;
                case '\t': sb.Append("\\t"); break;
                default: sb.Append(ch); break;
            }
        }
        return sb.ToString();
    }

    public static string FormatValue(object? value)
    {
        return value switch
        {
            null => "null",
            bool b => b ? "true" : "false",
            int i => i.ToString(CultureInfo.InvariantCulture),
            long l => l.ToString(CultureInfo.InvariantCulture),
            double d => d.ToString(CultureInfo.InvariantCulture),
            string s => "'" + EscapeString(s) + "'",
            IEnumerable<object?> list => "[" + string.Join(", ", list.Select(FormatValue)) + "]",
            _ => "'" + EscapeString(value.ToString() ?? "") + "'",
        };
    }
}

public sealed class CypherNode
{
    public required string Id { get; init; }
    public required IReadOnlyList<string> Labels { get; init; }
    public Dictionary<string, object?> Properties { get; } = new();

    public string ToCypher()
    {
        var props = new Dictionary<string, object?>(Properties) { ["id"] = Id };
        var propText = string.Join(", ", props
            .Where(kv => kv.Value is not null)
            .Select(kv => $"{kv.Key}: {Cypher.FormatValue(kv.Value)}"));
        return $"CREATE ({Id}:{string.Join(":", Labels)} {{{propText}}});";
    }
}

public sealed class CypherRelationship
{
    public required string Source { get; init; }
    public required string Type { get; init; }
    public required string Target { get; init; }
    public Dictionary<string, object?>? Properties { get; init; }

    public string ToCypher()
    {
        var props = Properties;
        var propText = props is { Count: > 0 }
            ? " {" + string.Join(", ", props.Select(kv => $"{kv.Key}: {Cypher.FormatValue(kv.Value)}")) + "}"
            : "";
        return $"CREATE ({Source})-[:{Type}{propText}]->({Target});";
    }
}

public sealed class CypherDocument
{
    public List<CypherNode> Nodes { get; } = new();
    public List<CypherRelationship> Relationships { get; } = new();
}
