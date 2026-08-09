using System.Globalization;
using System.Text;

namespace CSharpToCypher;

/// <summary>
/// Consumes one parsed file's graph at a time. <see cref="Finish"/> runs once
/// after every file has been written, for writers that need to buffer across
/// the whole run (e.g. to group rows by label before writing CSV).
/// </summary>
public interface IGraphWriter
{
    void Write(CypherDocument document);
    void Finish();
}

/// <summary>Streams Cypher <c>CREATE</c>/<c>MATCH</c> statements as each file is built — the original, transaction-per-statement output mode.</summary>
public sealed class CypherStreamWriter : IGraphWriter
{
    private readonly TextWriter _output;

    public CypherStreamWriter(TextWriter output) => _output = output;

    public void Write(CypherDocument document)
    {
        foreach (var node in document.Nodes)
        {
            _output.WriteLine(node.ToCypher());
        }
        foreach (var relationship in document.Relationships)
        {
            _output.WriteLine(relationship.ToCypher());
        }
    }

    public void Finish()
    {
    }
}

/// <summary>
/// Buffers nodes/relationships across every input file, grouped by label set
/// (nodes) or relationship type, and at <see cref="Finish"/> writes one
/// <c>neo4j-admin database import</c> CSV file per group under
/// <c>outputDir</c>, then prints the ready-to-run import command that loads
/// them all into an empty database in one bulk pass — the alternative to
/// streaming individual <c>CREATE</c> statements, which doesn't scale to
/// large codebases (many small transactions, or one huge one).
/// </summary>
public sealed class Neo4jAdminImportWriter : IGraphWriter
{
    private const string ArrayDelimiter = ";";

    private readonly string _outputDir;
    private readonly string _database;
    private readonly TextWriter _commandOutput;
    private readonly Dictionary<string, List<CypherNode>> _nodesByGroup = new();
    private readonly List<string> _nodeGroupOrder = new();
    private readonly Dictionary<string, List<CypherRelationship>> _relationshipsByType = new();
    private readonly List<string> _relationshipTypeOrder = new();

    public Neo4jAdminImportWriter(string outputDir, string database, TextWriter commandOutput)
    {
        _outputDir = outputDir;
        _database = database;
        _commandOutput = commandOutput;
    }

    public void Write(CypherDocument document)
    {
        foreach (var node in document.Nodes)
        {
            var key = string.Join(":", node.Labels);
            if (!_nodesByGroup.TryGetValue(key, out var list))
            {
                list = new List<CypherNode>();
                _nodesByGroup[key] = list;
                _nodeGroupOrder.Add(key);
            }
            list.Add(node);
        }
        foreach (var relationship in document.Relationships)
        {
            if (!_relationshipsByType.TryGetValue(relationship.Type, out var list))
            {
                list = new List<CypherRelationship>();
                _relationshipsByType[relationship.Type] = list;
                _relationshipTypeOrder.Add(relationship.Type);
            }
            list.Add(relationship);
        }
    }

    public void Finish()
    {
        Directory.CreateDirectory(_outputDir);

        var nodePaths = _nodeGroupOrder.Select(key => WriteNodeFile(key, _nodesByGroup[key])).ToList();
        var relationshipPaths = _relationshipTypeOrder
            .Select(type => (Type: type, Path: WriteRelationshipFile(type, _relationshipsByType[type])))
            .ToList();

        _commandOutput.WriteLine(BuildCommand(nodePaths, relationshipPaths));
    }

    private string WriteNodeFile(string groupKey, List<CypherNode> nodes)
    {
        var (columns, types) = CollectSchema(nodes.Select(n => n.Properties));

        var directory = Path.Combine(_outputDir, "nodes");
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, SanitizeFileName(groupKey) + ".csv");

        using var writer = new StreamWriter(path, append: false, new UTF8Encoding(false));
        WriteRow(writer, new[] { ":ID", ":LABEL" }.Concat(HeaderNames(columns, types)));
        foreach (var node in nodes)
        {
            var fields = new List<string> { node.Id, string.Join(ArrayDelimiter, node.Labels) };
            fields.AddRange(columns.Select(c => FormatCsvValue(node.Properties.GetValueOrDefault(c))));
            WriteRow(writer, fields);
        }
        return path;
    }

    private string WriteRelationshipFile(string type, List<CypherRelationship> relationships)
    {
        var (columns, types) = CollectSchema(relationships.Select(r => (IReadOnlyDictionary<string, object?>?)r.Properties ?? new Dictionary<string, object?>()));

        var directory = Path.Combine(_outputDir, "relationships");
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, SanitizeFileName(type) + ".csv");

        using var writer = new StreamWriter(path, append: false, new UTF8Encoding(false));
        WriteRow(writer, new[] { ":START_ID", ":END_ID" }.Concat(HeaderNames(columns, types)));
        foreach (var relationship in relationships)
        {
            var fields = new List<string> { relationship.Source, relationship.Target };
            fields.AddRange(columns.Select(c =>
                FormatCsvValue(relationship.Properties is not null && relationship.Properties.TryGetValue(c, out var v) ? v : null)));
            WriteRow(writer, fields);
        }
        return path;
    }

    /// <summary>
    /// Union of property keys across every row in a group, in first-seen order,
    /// with a Neo4j type per key inferred from its values. A property whose CLR
    /// type varies from row to row (e.g. a Python-style "value" property that's
    /// sometimes an int, sometimes a string) falls back to "string" — the one
    /// type every value can be rendered as — rather than emitting a column whose
    /// declared type doesn't match every row, which neo4j-admin rejects outright.
    /// </summary>
    private static (List<string> Columns, Dictionary<string, string> Types) CollectSchema(IEnumerable<IReadOnlyDictionary<string, object?>> rows)
    {
        var columns = new List<string>();
        var seen = new HashSet<string>();
        var types = new Dictionary<string, string>();
        foreach (var row in rows)
        {
            foreach (var (key, value) in row)
            {
                if (value is null)
                {
                    continue;
                }
                if (seen.Add(key))
                {
                    columns.Add(key);
                }
                var inferred = InferType(value);
                if (types.TryGetValue(key, out var existing))
                {
                    if (existing != inferred)
                    {
                        types[key] = "string";
                    }
                }
                else
                {
                    types[key] = inferred;
                }
            }
        }
        return (columns, types);
    }

    private static IEnumerable<string> HeaderNames(List<string> columns, Dictionary<string, string> types) =>
        columns.Select(c => types[c] == "string" ? c : $"{c}:{types[c]}");

    private static string InferType(object value) => value switch
    {
        bool => "boolean",
        int or long => "long",
        float or double => "double",
        IEnumerable<object?> => "string[]",
        _ => "string",
    };

    /// <summary>Formats by the value's own CLR type, independent of the column's declared (possibly downgraded-to-string) type — a "string" column still needs "true"/"false" for a bool, not <c>ToString()</c>'s "True"/"False".</summary>
    private static string FormatCsvValue(object? value) => value switch
    {
        null => "",
        bool b => b ? "true" : "false",
        int or long => Convert.ToInt64(value, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture),
        float or double => Convert.ToDouble(value, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture),
        IEnumerable<object?> list => string.Join(ArrayDelimiter, list.Select(v => v?.ToString() ?? "")),
        _ => value.ToString() ?? "",
    };

    private static void WriteRow(TextWriter writer, IEnumerable<string> fields) =>
        writer.WriteLine(string.Join(",", fields.Select(CsvEscape)));

    private static string CsvEscape(string field) =>
        field.IndexOfAny(new[] { ',', '"', '\r', '\n' }) < 0 ? field : "\"" + field.Replace("\"", "\"\"") + "\"";

    private static string SanitizeFileName(string name)
    {
        var sb = new StringBuilder(name.Length);
        foreach (var ch in name)
        {
            sb.Append(char.IsLetterOrDigit(ch) || ch == '_' ? ch : '_');
        }
        return sb.ToString();
    }

    private string BuildCommand(List<string> nodePaths, List<(string Type, string Path)> relationshipPaths)
    {
        // <database> must come right after 'full', not at the end (even though
        // that's the order `--help` shows): neo4j-admin's --nodes/--relationships
        // are unbounded multi-valued options, so a trailing bare "neo4j" gets
        // parsed as one more file in the last --relationships list instead of
        // the positional database argument, and the import fails outright.
        var sb = new StringBuilder("neo4j-admin database import full ").Append(_database).Append(" --multiline-fields=true");
        foreach (var path in nodePaths)
        {
            sb.Append(" --nodes=").Append(QuoteArg(path));
        }
        foreach (var (type, path) in relationshipPaths)
        {
            sb.Append(" --relationships=").Append(type).Append('=').Append(QuoteArg(path));
        }
        return sb.ToString();
    }

    private static string QuoteArg(string path) => path.Contains(' ') ? $"\"{path}\"" : path;
}
