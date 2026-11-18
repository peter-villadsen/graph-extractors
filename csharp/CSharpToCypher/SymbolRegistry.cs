using Microsoft.CodeAnalysis;

namespace CSharpToCypher;

/// <summary>
/// Run-wide symbol bookkeeping that lets <c>CALLS</c> edges reach across files.
/// One instance is shared by every <see cref="GraphBuilder"/> in a run: each
/// builder registers the symbols its declaration nodes introduce and records the
/// call sites it sees, and once all files are built
/// <see cref="BuildCallDocument"/> resolves every call site to the callee's
/// declaration node, or to a shared <c>CSharpSymbol</c> node when the callee
/// isn't declared in the input.
///
/// Symbols are only ever compared with <see cref="SymbolEqualityComparer.Default"/>
/// after <see cref="Normalize"/>, never by name, so overloads and same-named
/// members in different types stay distinct.
/// </summary>
public sealed class SymbolRegistry
{
    private readonly Dictionary<ISymbol, string> _declarations = new(SymbolEqualityComparer.Default);
    private readonly Dictionary<CallKey, int> _callCounts = new();
    private readonly List<CallKey> _callOrder = new();

    /// <summary>
    /// Maps a symbol to the definition every reference to it should land on:
    /// a reduced extension method to its static declaration, a constructed
    /// generic to its definition, and a partial method to its implementation
    /// part (when there is one).
    /// </summary>
    public static ISymbol Normalize(ISymbol symbol)
    {
        if (symbol is IMethodSymbol method)
        {
            method = method.ReducedFrom ?? method;
            method = method.OriginalDefinition;
            return method.PartialImplementationPart ?? method;
        }
        return symbol.OriginalDefinition;
    }

    /// <summary>
    /// Records that <paramref name="nodeId"/> declares <paramref name="symbol"/>.
    /// When several nodes declare one symbol (the parts of a partial type, or
    /// the definition and implementation of a partial method) the first one
    /// wins, except that a partial method's implementation part replaces its
    /// definition part, since that is where the body lives.
    /// </summary>
    public void RegisterDeclaration(ISymbol symbol, string nodeId)
    {
        var key = Normalize(symbol);
        var isPartialImplementation = symbol is IMethodSymbol { PartialDefinitionPart: not null };
        if (isPartialImplementation)
        {
            _declarations[key] = nodeId;
        }
        else
        {
            _declarations.TryAdd(key, nodeId);
        }
    }

    /// <summary>Records one call site from the node <paramref name="callerId"/> to <paramref name="callee"/>. No edge is emitted until <see cref="BuildCallDocument"/>.</summary>
    public void RecordCall(string callerId, ISymbol callee)
    {
        var key = new CallKey(callerId, Normalize(callee));
        if (_callCounts.TryGetValue(key, out var count))
        {
            _callCounts[key] = count + 1;
        }
        else
        {
            _callCounts[key] = 1;
            _callOrder.Add(key);
        }
    }

    /// <summary>
    /// Builds the document that completes the run: one <c>CSharpSymbol</c> node
    /// per callee not declared in the input (deduplicated across the whole run),
    /// and one <c>CALLS</c> edge per distinct (caller, callee) pair, in first-seen
    /// order, carrying the number of call sites as <c>count</c>. New node ids
    /// continue from <paramref name="startId"/>; <paramref name="counter"/>
    /// returns the counter after the last one.
    /// </summary>
    public CypherDocument BuildCallDocument(int startId, out int counter)
    {
        var document = new CypherDocument();
        var externals = new Dictionary<ISymbol, string>(SymbolEqualityComparer.Default);
        counter = startId;
        foreach (var key in _callOrder)
        {
            if (!_declarations.TryGetValue(key.Callee, out var target)
                && !externals.TryGetValue(key.Callee, out target))
            {
                target = $"n{++counter}";
                var node = new CypherNode { Id = target, Labels = new[] { "CSharpSymbol" } };
                node.Properties["symbolKind"] = key.Callee.Kind.ToString();
                node.Properties["symbolName"] = key.Callee.ToDisplayString(GraphBuilder.QualifiedFormat);
                document.Nodes.Add(node);
                externals[key.Callee] = target;
            }
            document.Relationships.Add(new CypherRelationship
            {
                Source = key.CallerId,
                Type = "CALLS",
                Target = target,
                Properties = new Dictionary<string, object?> { ["count"] = _callCounts[key] },
            });
        }
        return document;
    }

    /// <summary>A (caller node, normalized callee) pair, compared by node id and symbol identity.</summary>
    private readonly record struct CallKey(string CallerId, ISymbol Callee)
    {
        public bool Equals(CallKey other) =>
            CallerId == other.CallerId && SymbolEqualityComparer.Default.Equals(Callee, other.Callee);

        public override int GetHashCode() =>
            HashCode.Combine(CallerId, SymbolEqualityComparer.Default.GetHashCode(Callee));
    }
}
