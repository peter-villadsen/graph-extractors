using Microsoft.CodeAnalysis;

namespace CSharpToCypher;

/// <summary>How a function uses a field or auto-property; a compound use is both.</summary>
[Flags]
public enum AccessKind
{
    Read = 1,
    Write = 2,
    ReadWrite = Read | Write,
}

/// <summary>
/// Run-wide symbol bookkeeping that lets <c>CALLS</c>, <c>READS</c> and
/// <c>WRITES</c> edges reach across files. One instance is shared by every
/// <see cref="GraphBuilder"/> in a run: each builder registers the symbols its
/// declaration nodes introduce and records the call sites and member accesses
/// it sees, and once all files are built <see cref="BuildDeferredDocument"/>
/// resolves them to declaration nodes.
///
/// Symbols are only ever compared with <see cref="SymbolEqualityComparer.Default"/>
/// after <see cref="Normalize"/>, never by name, so overloads and same-named
/// members in different types stay distinct.
/// </summary>
public sealed class SymbolRegistry
{
    private readonly Dictionary<ISymbol, string> _declarations = new(SymbolEqualityComparer.Default);
    private readonly EdgeTally _calls = new();
    private readonly EdgeTally _accesses = new();

    /// <summary>
    /// Maps a symbol to the definition every reference to it should land on:
    /// a reduced extension method to its static declaration, a constructed
    /// generic (or a member of one) to its definition, and a partial method to
    /// its implementation part (when there is one).
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

    /// <summary>Records one call site from the node <paramref name="callerId"/> to <paramref name="callee"/>. No edge is emitted until <see cref="BuildDeferredDocument"/>.</summary>
    public void RecordCall(string callerId, ISymbol callee) =>
        _calls.Add(new EdgeKey(callerId, Normalize(callee), "CALLS"));

    /// <summary>
    /// Records one access by the node <paramref name="callerId"/> to the field or
    /// auto-property <paramref name="member"/>: a <c>READS</c> and/or a
    /// <c>WRITES</c> occurrence, per <paramref name="kind"/>. No edge is emitted
    /// until <see cref="BuildDeferredDocument"/>, which drops accesses to members
    /// not declared in the input.
    /// </summary>
    public void RecordAccess(string callerId, ISymbol member, AccessKind kind)
    {
        var target = Normalize(member);
        if (kind.HasFlag(AccessKind.Read))
        {
            _accesses.Add(new EdgeKey(callerId, target, "READS"));
        }
        if (kind.HasFlag(AccessKind.Write))
        {
            _accesses.Add(new EdgeKey(callerId, target, "WRITES"));
        }
    }

    /// <summary>
    /// Builds the document that completes the run, from everything recorded:
    /// the <c>CALLS</c> edges (<see cref="EmitCalls"/>) and then the
    /// <c>READS</c>/<c>WRITES</c> edges (<see cref="EmitAccesses"/>). New node
    /// ids continue from <paramref name="startId"/>; <paramref name="counter"/>
    /// returns the counter after the last one.
    /// </summary>
    public CypherDocument BuildDeferredDocument(int startId, out int counter)
    {
        var document = new CypherDocument();
        counter = EmitCalls(document, startId);
        EmitAccesses(document);
        return document;
    }

    /// <summary>
    /// One <c>CALLS</c> edge per distinct (caller, callee) pair, in first-seen
    /// order, carrying the number of call sites as <c>count</c>. It targets the
    /// callee's declaration node, or, for a callee not declared in the input,
    /// one <c>CSharpSymbol</c> node per symbol for the whole run. Returns the id
    /// counter after the last new node.
    /// </summary>
    private int EmitCalls(CypherDocument document, int counter)
    {
        var externals = new Dictionary<ISymbol, string>(SymbolEqualityComparer.Default);
        foreach (var (key, count) in _calls)
        {
            if (!_declarations.TryGetValue(key.Target, out var target)
                && !externals.TryGetValue(key.Target, out target))
            {
                target = $"n{++counter}";
                var node = new CypherNode { Id = target, Labels = new[] { "CSharpSymbol" } };
                node.Properties["symbolKind"] = key.Target.Kind.ToString();
                node.Properties["symbolName"] = key.Target.ToDisplayString(GraphBuilder.QualifiedFormat);
                document.Nodes.Add(node);
                externals[key.Target] = target;
            }
            AddCountedEdge(document, key, target, count);
        }
        return counter;
    }

    /// <summary>
    /// One <c>READS</c> or <c>WRITES</c> edge per distinct (caller, member, type),
    /// in first-seen order, carrying the number of accesses as <c>count</c>.
    /// Accesses to members not declared in the input are dropped: there are no
    /// external <c>READS</c>/<c>WRITES</c> targets.
    /// </summary>
    private void EmitAccesses(CypherDocument document)
    {
        foreach (var (key, count) in _accesses)
        {
            if (_declarations.TryGetValue(key.Target, out var target))
            {
                AddCountedEdge(document, key, target, count);
            }
        }
    }

    private static void AddCountedEdge(CypherDocument document, EdgeKey key, string targetId, int count) =>
        document.Relationships.Add(new CypherRelationship
        {
            Source = key.CallerId,
            Type = key.Type,
            Target = targetId,
            Properties = new Dictionary<string, object?> { ["count"] = count },
        });

    /// <summary>A pending edge: (caller node, normalized target symbol, relationship type), compared by id, symbol identity and type.</summary>
    private readonly record struct EdgeKey(string CallerId, ISymbol Target, string Type)
    {
        public bool Equals(EdgeKey other) =>
            CallerId == other.CallerId && Type == other.Type && SymbolEqualityComparer.Default.Equals(Target, other.Target);

        public override int GetHashCode() =>
            HashCode.Combine(CallerId, Type, SymbolEqualityComparer.Default.GetHashCode(Target));
    }

    /// <summary>Counts occurrences of each pending edge, enumerating them in first-seen order so output is deterministic.</summary>
    private sealed class EdgeTally : IEnumerable<(EdgeKey Key, int Count)>
    {
        private readonly Dictionary<EdgeKey, int> _counts = new();
        private readonly List<EdgeKey> _order = new();

        public void Add(EdgeKey key)
        {
            if (_counts.TryGetValue(key, out var count))
            {
                _counts[key] = count + 1;
            }
            else
            {
                _counts[key] = 1;
                _order.Add(key);
            }
        }

        public IEnumerator<(EdgeKey Key, int Count)> GetEnumerator() =>
            _order.Select(key => (key, _counts[key])).GetEnumerator();

        System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
    }
}
