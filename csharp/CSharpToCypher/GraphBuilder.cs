using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.FlowAnalysis;

namespace CSharpToCypher;

/// <summary>
/// Walks a Roslyn syntax tree and produces a Cypher graph.
///
/// Node labels use the grammar production names from the official C# language
/// specification (ECMA-334 / https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/language-specification),
/// e.g. <c>class_declaration</c>, <c>interface_declaration</c>,
/// <c>method_declaration</c>, <c>while_statement</c>, <c>for_statement</c>,
/// <c>embedded_statement</c>. Comments and preprocessing directives are modeled
/// as trivia connected with <c>HAS_TRIVIA</c>, mirroring Roslyn's
/// <c>SyntaxTrivia</c>.
/// </summary>
public sealed class GraphBuilder
{
    private readonly CypherDocument _document = new();
    private readonly string _file;
    private readonly SemanticModel? _model;
    private readonly Dictionary<SyntaxNode, string> _nodeIds = new();
    private readonly Dictionary<ISymbol, string> _symbolNodeIds = new(SymbolEqualityComparer.Default);
    private readonly Stack<string> _methodStack = new();
    private int _counter;

    /// <summary>Includes containing type/namespace so symbol names are unambiguous.</summary>
    private static readonly SymbolDisplayFormat QualifiedFormat = new(
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        memberOptions: SymbolDisplayMemberOptions.IncludeContainingType | SymbolDisplayMemberOptions.IncludeParameters,
        parameterOptions: SymbolDisplayParameterOptions.IncludeType,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes | SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers);

    public GraphBuilder(string file, SemanticModel? model = null, int startId = 0)
    {
        _file = file;
        _model = model;
        _counter = startId;
    }

    /// <summary>The id counter after the last <see cref="Build"/> call, so the next file in a multi-file run can continue it and keep node ids unique across the whole run.</summary>
    public int Counter => _counter;

    public CypherDocument Build(CompilationUnitSyntax root)
    {
        BuildNode(root, null, null);
        AttachTrivia(root);
        return _document;
    }

    // -- helpers -------------------------------------------------------

    private string NewId() => $"n{++_counter}";

    private static KeyValuePair<string, object?> KV(string key, object? value) => new(key, value);

    private string AddNode(IReadOnlyList<string> labels, IEnumerable<KeyValuePair<string, object?>> props)
    {
        var node = new CypherNode { Id = NewId(), Labels = labels };
        foreach (var kv in props)
        {
            node.Properties[kv.Key] = kv.Value;
        }
        _document.Nodes.Add(node);
        return node.Id;
    }

    private void AddRel(string? source, string? type, string? target, IEnumerable<KeyValuePair<string, object?>>? properties = null)
    {
        if (source is null || type is null || target is null)
        {
            return;
        }
        var rel = new CypherRelationship
        {
            Source = source,
            Type = type,
            Target = target,
            Properties = properties?.ToDictionary(kv => kv.Key, kv => kv.Value),
        };
        _document.Relationships.Add(rel);
    }

    /// <summary>Builds consecutive sibling statements, chaining them with :FOLLOWS (source order).</summary>
    private void AddFollowsChain(SyntaxList<StatementSyntax> statements, string ownerId)
    {
        string? previous = null;
        foreach (var stmt in statements)
        {
            var id = BuildNode(stmt, "CONTAINS", ownerId);
            if (id is not null)
            {
                if (previous is not null)
                {
                    AddRel(previous, "FOLLOWS", id);
                }
                previous = id;
            }
        }
    }

    /// <summary>
    /// Emits Roslyn's <see cref="ControlFlowGraph"/> for a declaration with a body:
    /// a :CSharpBasicBlock node per basic block, :FOLLOWS edges for the successor
    /// graph (with branch semantics), and :CONTAINS links from each block to the
    /// AST statements it executes. Correctness comes from Roslyn, not this walker.
    /// </summary>
    private void BuildCfg(SyntaxNode declaration)
    {
        if (_model is null)
        {
            return;
        }
        ControlFlowGraph? cfg;
        try
        {
            cfg = ControlFlowGraph.Create(declaration, _model, CancellationToken.None);
        }
        catch (ArgumentException)
        {
            return; // not a CFG-bearing node
        }
        if (cfg is null)
        {
            return;
        }

        var scope = CfgScope(declaration);
        var blockIds = new Dictionary<BasicBlock, string>();
        foreach (var block in cfg.Blocks)
        {
            var blockId = AddNode(new[] { "CSharpBasicBlock" }, new[]
            {
                KV("scope", scope),
                KV("ordinal", block.Ordinal),
                KV("kind", block.Kind.ToString()),
                KV("isReachable", block.IsReachable),
                KV("isEntry", block.Kind == BasicBlockKind.Entry),
                KV("isFinal", block.Kind == BasicBlockKind.Exit),
            });
            blockIds[block] = blockId;

            var linked = new HashSet<string>();
            var statementOrdinal = 0;
            foreach (var op in block.Operations)
            {
                var syntaxId = NodeIdOrNull(op.Syntax);
                if (syntaxId is not null && linked.Add(syntaxId))
                {
                    AddRel(blockId, "CONTAINS", syntaxId, new[] { KV("ordinal", statementOrdinal++) });
                }
            }
        }

        foreach (var block in cfg.Blocks)
        {
            var seen = new HashSet<BasicBlock>();
            foreach (var branch in new[] { block.FallThroughSuccessor, block.ConditionalSuccessor })
            {
                if (branch is null)
                {
                    continue;
                }
                var dest = branch.Destination;
                if (dest is null || !seen.Add(dest))
                {
                    continue;
                }
                var props = new List<KeyValuePair<string, object?>>();
                if (branch.Semantics != ControlFlowBranchSemantics.None)
                {
                    props.Add(KV("semantics", branch.Semantics.ToString()));
                }
                if (branch.IsConditionalSuccessor)
                {
                    props.Add(KV("whenTrue", true));
                }
                AddRel(blockIds[block], "FOLLOWS", blockIds[dest], props);
            }
        }
    }

    /// <summary>
    /// A stable, human-readable name for the CFG scope of a declaration, used to
    /// group basic blocks in querying. Prefers the resolved symbol so overloads
    /// and same-named members stay distinct.
    /// </summary>
    private string CfgScope(SyntaxNode declaration)
    {
        // An expression-bodied property/indexer's CFG is rooted at its
        // ArrowExpressionClauseSyntax (the only node shape Roslyn's CFG API
        // recognizes there), but the declared symbol lives one level up.
        var symbolTarget = declaration is ArrowExpressionClauseSyntax { Parent: { } parent } ? parent : declaration;
        if (_model is not null)
        {
            var symbol = _model.GetDeclaredSymbol(symbolTarget);
            if (symbol is not null)
            {
                return symbol.ToDisplayString(QualifiedFormat);
            }
        }
        return symbolTarget switch
        {
            MethodDeclarationSyntax m => m.Identifier.ValueText,
            ConstructorDeclarationSyntax c => c.Identifier.ValueText,
            DestructorDeclarationSyntax d => d.Identifier.ValueText,
            LocalFunctionStatementSyntax lf => lf.Identifier.ValueText,
            OperatorDeclarationSyntax op => $"operator {op.OperatorToken.Text}",
            ConversionOperatorDeclarationSyntax co => $"{co.ImplicitOrExplicitKeyword.Text} operator {co.Type}",
            PropertyDeclarationSyntax p => p.Identifier.ValueText,
            IndexerDeclarationSyntax => "this[]",
            AccessorDeclarationSyntax a => a.Parent switch
            {
                PropertyDeclarationSyntax p => $"{p.Identifier.ValueText}.{a.Keyword.ValueText}",
                EventDeclarationSyntax e => $"{e.Identifier.ValueText}.{a.Keyword.ValueText}",
                _ => a.Keyword.ValueText,
            },
            _ => symbolTarget.Kind().ToString(),
        };
    }

    private string? NodeIdOrNull(SyntaxNode? node) =>
        node is not null && _nodeIds.TryGetValue(node, out var id) ? id : null;

    private string CreateNode(SyntaxNode syntax, IReadOnlyList<string> labels, IEnumerable<KeyValuePair<string, object?>> props)
    {
        var id = AddNode(labels, props.Concat(LocationProps(syntax)));
        _nodeIds[syntax] = id;
        return id;
    }

    /// <summary>
    /// Exact source coordinates for a syntax node: 1-based lines, 0-based
    /// columns, plus absolute character offsets into the file.
    /// </summary>
    private static IEnumerable<KeyValuePair<string, object?>> LocationProps(SyntaxNode syntax)
    {
        var span = syntax.GetLocation().GetLineSpan().Span;
        yield return new KeyValuePair<string, object?>("startLine", span.Start.Line + 1);
        yield return new KeyValuePair<string, object?>("startColumn", span.Start.Character);
        yield return new KeyValuePair<string, object?>("endLine", span.End.Line + 1);
        yield return new KeyValuePair<string, object?>("endColumn", span.End.Character);
        yield return new KeyValuePair<string, object?>("startOffset", syntax.SpanStart);
        yield return new KeyValuePair<string, object?>("endOffset", syntax.Span.End);
    }

    private string CreateDeclarationNode(SyntaxNode syntax, string specific, IEnumerable<KeyValuePair<string, object?>> props)
        => CreateNode(syntax, new[] { specific, "CSharpDeclaration" }, props);

    private string CreateStatementNode(SyntaxNode syntax, string specific, IEnumerable<KeyValuePair<string, object?>> props)
        => CreateNode(syntax, new[] { specific, "CSharpStatement" }, props);

    private string CreateExpressionNode(SyntaxNode syntax, string specific, IEnumerable<KeyValuePair<string, object?>> props)
        => CreateNode(syntax, new[] { specific, "CSharpExpression" }, props);

    private string AddTypeNode(string name, string? kind = null)
    {
        var props = new List<KeyValuePair<string, object?>> { KV("name", name) };
        if (kind is not null)
        {
            props.Add(KV("kind", kind));
        }
        return AddNode(new[] { "CSharpType" }, props);
    }

    /// <summary>Like <see cref="AddTypeNode(string, string)"/> but resolves the type symbol.</summary>
    private string AddTypeNode(SyntaxNode syntax, string? kind = null)
    {
        var props = new List<KeyValuePair<string, object?>> { KV("name", syntax.ToString()) };
        if (kind is not null)
        {
            props.Add(KV("kind", kind));
        }
        props.AddRange(SymbolProps(syntax));
        var id = AddNode(new[] { "CSharpType" }, props);
        _nodeIds[syntax] = id;
        return id;
    }

    /// <summary>
    /// Records a call edge from the enclosing method to the resolved callee
    /// symbol, when semantic information is available.
    /// </summary>
    private void AddCallEdge(SyntaxNode calleeSyntax)
    {
        if (_model is null || _methodStack.Count == 0)
        {
            return;
        }
        var info = _model.GetSymbolInfo(calleeSyntax);
        var symbol = info.Symbol ?? info.CandidateSymbols.FirstOrDefault();
        if (symbol is null)
        {
            return;
        }
        if (!_symbolNodeIds.TryGetValue(symbol, out var callee))
        {
            callee = AddNode(new[] { "CSharpSymbol" }, new[]
            {
                KV("symbolKind", symbol.Kind.ToString()),
                KV("symbolName", symbol.ToDisplayString(QualifiedFormat)),
            });
            _symbolNodeIds[symbol] = callee;
        }
        AddRel(_methodStack.Peek(), "CALLS", callee);
    }

    /// <summary>
    /// Attaches every meaningful trivia (comments, preprocessor directives) to the
    /// node that directly owns its token, exactly once. Walking tokens avoids the
    /// duplicates that come from nested nodes sharing a boundary token, and picks
    /// up comments attached to interior tokens (e.g. after an opening brace).
    /// </summary>
    private void AttachTrivia(SyntaxNode root)
    {
        foreach (var token in root.DescendantTokens(descendIntoTrivia: true))
        {
            var owner = token.Parent;
            if (owner is null || !_nodeIds.TryGetValue(owner, out var ownerId))
            {
                continue;
            }
            foreach (var trivia in token.LeadingTrivia.Concat(token.TrailingTrivia))
            {
                if (!IsMeaningfulTrivia(trivia))
                {
                    continue;
                }
                var id = AddNode(new[] { "CSharpTrivia" }, new[]
                {
                    KV("kind", trivia.Kind().ToString()),
                    KV("text", trivia.ToFullString().Trim()),
                    KV("startLine", trivia.GetLocation().GetLineSpan().Span.Start.Line + 1),
                });
                AddRel(ownerId, "HAS_TRIVIA", id);
            }
        }
    }

    /// <summary>Resolves a syntax node to its symbol; records kind, full name and type.</summary>
    private IEnumerable<KeyValuePair<string, object?>> SymbolProps(SyntaxNode node)
    {
        if (_model is null)
        {
            yield break;
        }
        var info = _model.GetSymbolInfo(node);
        var symbol = info.Symbol ?? info.CandidateSymbols.FirstOrDefault();
        if (symbol is null)
        {
            yield break;
        }
        yield return new KeyValuePair<string, object?>("symbolKind", symbol.Kind.ToString());
        yield return new KeyValuePair<string, object?>("symbolName", symbol.ToDisplayString(QualifiedFormat));
        var type = SymbolType(symbol);
        if (type is not null)
        {
            yield return new KeyValuePair<string, object?>("symbolType", type);
        }
    }

    private static string? SymbolType(ISymbol symbol) => symbol switch
    {
        IMethodSymbol m => m.ReturnType?.ToDisplayString(QualifiedFormat),
        IPropertySymbol p => p.Type?.ToDisplayString(QualifiedFormat),
        IFieldSymbol f => f.Type?.ToDisplayString(QualifiedFormat),
        ILocalSymbol l => l.Type?.ToDisplayString(QualifiedFormat),
        IParameterSymbol pa => pa.Type?.ToDisplayString(QualifiedFormat),
        IEventSymbol e => e.Type?.ToDisplayString(QualifiedFormat),
        ITypeSymbol t => t.ToDisplayString(QualifiedFormat),
        _ => null,
    };

    private static bool IsMeaningfulTrivia(SyntaxTrivia trivia) =>
        trivia.IsDirective
        || trivia.IsKind(SyntaxKind.SingleLineCommentTrivia)
        || trivia.IsKind(SyntaxKind.MultiLineCommentTrivia)
        || trivia.IsKind(SyntaxKind.DocumentationCommentExteriorTrivia)
        || trivia.IsKind(SyntaxKind.SingleLineDocumentationCommentTrivia)
        || trivia.IsKind(SyntaxKind.MultiLineDocumentationCommentTrivia);

    private static string Accessibility(SyntaxTokenList modifiers)
    {
        if (modifiers.Any(SyntaxKind.PublicKeyword))
        {
            return "public";
        }
        if (modifiers.Any(SyntaxKind.PrivateKeyword))
        {
            return "private";
        }
        if (modifiers.Any(SyntaxKind.ProtectedKeyword) && modifiers.Any(SyntaxKind.InternalKeyword))
        {
            return "protected internal";
        }
        if (modifiers.Any(SyntaxKind.ProtectedKeyword))
        {
            return "protected";
        }
        if (modifiers.Any(SyntaxKind.InternalKeyword))
        {
            return "internal";
        }
        return "private";
    }

    private void BuildBaseList(BaseListSyntax? baseList, string ownerId, bool canHaveBaseClass = true)
    {
        if (baseList is null)
        {
            return;
        }
        var types = baseList.Types.ToList();
        for (var i = 0; i < types.Count; i++)
        {
            if (i == 0 && canHaveBaseClass && IsBaseClassCandidate(types[i].Type))
            {
                AddRel(ownerId, "DERIVED_FROM", AddTypeNode(types[i].Type, "class"));
            }
            else
            {
                AddRel(ownerId, "IMPLEMENTS", AddTypeNode(types[i].Type, "interface"));
            }
        }
    }

    /// <summary>
    /// Whether the first entry in a base list should be treated as a base class
    /// rather than an interface. Resolves the symbol when semantic info is
    /// available; otherwise falls back to the C# rule that a base class, when
    /// present, must be listed first.
    /// </summary>
    private bool IsBaseClassCandidate(TypeSyntax type)
    {
        if (_model is null)
        {
            return true;
        }
        var symbol = _model.GetTypeInfo(type).Type;
        return symbol is null || symbol.TypeKind == TypeKind.Class;
    }

    /// <summary>Attaches every attribute in every attribute list to <paramref name="ownerId"/> via :HAS_ATTRIBUTE.</summary>
    private void BuildAttributeLists(SyntaxList<AttributeListSyntax> attributeLists, string ownerId)
    {
        foreach (var list in attributeLists)
        {
            var target = list.Target?.Identifier.ValueText;
            foreach (var attribute in list.Attributes)
            {
                var props = new List<KeyValuePair<string, object?>>
                {
                    KV("name", attribute.Name.ToString()),
                    KV("target", target),
                    KV("text", attribute.ToString()),
                };
                props.AddRange(SymbolProps(attribute));
                var id = CreateNode(attribute, new[] { "CSharpAttribute" }, props);
                AddRel(ownerId, "HAS_ATTRIBUTE", id);
                if (attribute.ArgumentList is not null)
                {
                    for (var i = 0; i < attribute.ArgumentList.Arguments.Count; i++)
                    {
                        AddRel(id, "HAS_ARGUMENT", BuildNode(attribute.ArgumentList.Arguments[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
            }
        }
    }

    /// <summary>Attaches every `where T : ...` constraint to <paramref name="ownerId"/> via :HAS_CONSTRAINT.</summary>
    private void BuildConstraintClauses(SyntaxList<TypeParameterConstraintClauseSyntax> clauses, string ownerId)
    {
        foreach (var clause in clauses)
        {
            var typeParameter = clause.Name.Identifier.ValueText;
            foreach (var constraint in clause.Constraints)
            {
                switch (constraint)
                {
                    case TypeConstraintSyntax tc:
                        AddRel(ownerId, "HAS_CONSTRAINT", AddTypeNode(tc.Type), new[] { KV("typeParameter", (object?)typeParameter) });
                        break;
                    case ClassOrStructConstraintSyntax css:
                    {
                        var id = CreateNode(css, new[] { "CSharpConstraint" }, new[]
                        {
                            KV("typeParameter", typeParameter),
                            KV("kind", css.ClassOrStructKeyword.Text),
                        });
                        AddRel(ownerId, "HAS_CONSTRAINT", id);
                        break;
                    }
                    case ConstructorConstraintSyntax ctorConstraint:
                    {
                        var id = CreateNode(ctorConstraint, new[] { "CSharpConstraint" }, new[]
                        {
                            KV("typeParameter", typeParameter),
                            KV("kind", "new()"),
                        });
                        AddRel(ownerId, "HAS_CONSTRAINT", id);
                        break;
                    }
                    case DefaultConstraintSyntax defaultConstraint:
                    {
                        var id = CreateNode(defaultConstraint, new[] { "CSharpConstraint" }, new[]
                        {
                            KV("typeParameter", typeParameter),
                            KV("kind", "default"),
                        });
                        AddRel(ownerId, "HAS_CONSTRAINT", id);
                        break;
                    }
                }
            }
        }
    }

    /// <summary>
    /// Walks a LINQ query's clause pipeline in source order — an optional leading
    /// `from` (absent when recursing into an `into` continuation), the body's
    /// from/let/where/join/orderby clauses, its final select/group clause, and
    /// any further continuation — attaching each to <paramref name="ownerId"/>
    /// via :HAS_CLAUSE with an `ordinal` reflecting pipeline position.
    /// </summary>
    private void BuildQueryBody(FromClauseSyntax? fromClause, QueryBodySyntax body, string ownerId)
    {
        var ordinal = 0;
        if (fromClause is not null)
        {
            AddRel(ownerId, "HAS_CLAUSE", BuildNode(fromClause, null, null), new[] { KV("ordinal", ordinal++) });
        }
        foreach (var clause in body.Clauses)
        {
            AddRel(ownerId, "HAS_CLAUSE", BuildNode(clause, null, null), new[] { KV("ordinal", ordinal++) });
        }
        AddRel(ownerId, "HAS_CLAUSE", BuildNode(body.SelectOrGroup, null, null), new[] { KV("ordinal", ordinal++) });
        if (body.Continuation is not null)
        {
            var continuationId = CreateNode(body.Continuation, new[] { "CSharpQueryContinuation" }, new[]
            {
                KV("identifier", body.Continuation.Identifier.ValueText),
            });
            AddRel(ownerId, "HAS_CONTINUATION", continuationId);
            BuildQueryBody(null, body.Continuation.Body, continuationId);
        }
    }

    // -- tree walk -----------------------------------------------------

    private string? BuildNode(SyntaxNode node, string? parentEdge, string? parentId)
    {
        var countBefore = _document.Nodes.Count;
        switch (node)
        {
            case CompilationUnitSyntax cu:
            {
                var id = CreateNode(cu, new[] { "CSharpCompilationUnit" }, new[]
                {
                    KV("file", _file),
                    KV("name", Path.GetFileName(_file)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(cu.AttributeLists, id);
                foreach (var ext in cu.Externs)
                {
                    BuildNode(ext, "USES", id);
                }
                foreach (var u in cu.Usings)
                {
                    BuildNode(u, "USES", id);
                }
                foreach (var m in cu.Members)
                {
                    BuildNode(m, "CONTAINS", id);
                }
                break;
            }
            case GlobalStatementSyntax gs:
                return BuildNode(gs.Statement, parentEdge, parentId);
            case ExternAliasDirectiveSyntax ext:
            {
                var id = CreateNode(ext, new[] { "CSharpExternAliasDirective" }, new[] { KV("identifier", ext.Identifier.ValueText) });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case UsingDirectiveSyntax u:
            {
                var name = u.Name?.ToString() ?? u.Alias?.Name.Identifier.ValueText ?? "";
                var id = CreateNode(u, new[] { "CSharpUsingDirective" }, new[]
                {
                    KV("name", name),
                    KV("isStatic", u.StaticKeyword != default),
                    KV("isAlias", u.Alias is not null),
                });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case NamespaceDeclarationSyntax ns:
            {
                var id = CreateDeclarationNode(ns, "CSharpNamespaceDeclaration", new[] { KV("name", ns.Name.ToString()) });
                AddRel(parentId, parentEdge, id);
                foreach (var ext in ns.Externs)
                {
                    BuildNode(ext, "USES", id);
                }
                foreach (var u in ns.Usings)
                {
                    BuildNode(u, "USES", id);
                }
                foreach (var member in ns.Members)
                {
                    BuildNode(member, "CONTAINS", id);
                }
                break;
            }
            case FileScopedNamespaceDeclarationSyntax fs:
            {
                var id = CreateDeclarationNode(fs, "CSharpNamespaceDeclaration", new[] { KV("name", fs.Name.ToString()) });
                AddRel(parentId, parentEdge, id);
                foreach (var ext in fs.Externs)
                {
                    BuildNode(ext, "USES", id);
                }
                foreach (var u in fs.Usings)
                {
                    BuildNode(u, "USES", id);
                }
                foreach (var member in fs.Members)
                {
                    BuildNode(member, "CONTAINS", id);
                }
                break;
            }
            case ClassDeclarationSyntax c:
            {
                var id = CreateDeclarationNode(c, "CSharpClassDeclaration", new[]
                {
                    KV("name", c.Identifier.ValueText),
                    KV("accessibility", Accessibility(c.Modifiers)),
                    KV("isPartial", c.Modifiers.Any(SyntaxKind.PartialKeyword)),
                    KV("isAbstract", c.Modifiers.Any(SyntaxKind.AbstractKeyword)),
                    KV("isSealed", c.Modifiers.Any(SyntaxKind.SealedKeyword)),
                    KV("isStatic", c.Modifiers.Any(SyntaxKind.StaticKeyword)),
                    KV("typeParameters", c.TypeParameterList?.Parameters.Select(tp => tp.Identifier.ValueText).ToArray()),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(c.AttributeLists, id);
                BuildBaseList(c.BaseList, id);
                BuildConstraintClauses(c.ConstraintClauses, id);
                foreach (var member in c.Members)
                {
                    BuildNode(member, "DECLARES", id);
                }
                break;
            }
            case RecordDeclarationSyntax r:
            {
                var isStruct = r.Kind() == SyntaxKind.RecordStructDeclaration;
                var id = CreateDeclarationNode(r, isStruct ? "CSharpRecordStructDeclaration" : "CSharpRecordDeclaration", new[]
                {
                    KV("name", r.Identifier.ValueText),
                    KV("accessibility", Accessibility(r.Modifiers)),
                    KV("isPartial", r.Modifiers.Any(SyntaxKind.PartialKeyword)),
                    KV("isAbstract", r.Modifiers.Any(SyntaxKind.AbstractKeyword)),
                    KV("isSealed", r.Modifiers.Any(SyntaxKind.SealedKeyword)),
                    KV("typeParameters", r.TypeParameterList?.Parameters.Select(tp => tp.Identifier.ValueText).ToArray()),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(r.AttributeLists, id);
                BuildBaseList(r.BaseList, id, canHaveBaseClass: !isStruct);
                BuildConstraintClauses(r.ConstraintClauses, id);
                if (r.ParameterList is not null)
                {
                    for (var i = 0; i < r.ParameterList.Parameters.Count; i++)
                    {
                        AddRel(id, "HAS_PARAMETER", BuildNode(r.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                foreach (var member in r.Members)
                {
                    BuildNode(member, "DECLARES", id);
                }
                break;
            }
            case StructDeclarationSyntax s:
            {
                var id = CreateDeclarationNode(s, "CSharpStructDeclaration", new[]
                {
                    KV("name", s.Identifier.ValueText),
                    KV("accessibility", Accessibility(s.Modifiers)),
                    KV("isPartial", s.Modifiers.Any(SyntaxKind.PartialKeyword)),
                    KV("isReadOnly", s.Modifiers.Any(SyntaxKind.ReadOnlyKeyword)),
                    KV("isRecord", false),
                    KV("typeParameters", s.TypeParameterList?.Parameters.Select(tp => tp.Identifier.ValueText).ToArray()),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(s.AttributeLists, id);
                BuildBaseList(s.BaseList, id, canHaveBaseClass: false);
                BuildConstraintClauses(s.ConstraintClauses, id);
                foreach (var member in s.Members)
                {
                    BuildNode(member, "DECLARES", id);
                }
                break;
            }
            case InterfaceDeclarationSyntax i:
            {
                var id = CreateDeclarationNode(i, "CSharpInterfaceDeclaration", new[]
                {
                    KV("name", i.Identifier.ValueText),
                    KV("accessibility", Accessibility(i.Modifiers)),
                    KV("typeParameters", i.TypeParameterList?.Parameters.Select(tp => tp.Identifier.ValueText).ToArray()),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(i.AttributeLists, id);
                BuildBaseList(i.BaseList, id, canHaveBaseClass: false);
                BuildConstraintClauses(i.ConstraintClauses, id);
                foreach (var member in i.Members)
                {
                    BuildNode(member, "DECLARES", id);
                }
                break;
            }
            case EnumDeclarationSyntax e:
            {
                var id = CreateDeclarationNode(e, "CSharpEnumDeclaration", new[]
                {
                    KV("name", e.Identifier.ValueText),
                    KV("accessibility", Accessibility(e.Modifiers)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(e.AttributeLists, id);
                if (e.BaseList is not null)
                {
                    foreach (var bt in e.BaseList.Types)
                    {
                        AddRel(id, "OF_TYPE", AddTypeNode(bt.Type));
                    }
                }
                foreach (var member in e.Members)
                {
                    BuildNode(member, "DECLARES", id);
                }
                break;
            }
            case EnumMemberDeclarationSyntax em:
            {
                var id = CreateDeclarationNode(em, "CSharpEnumMember", new[]
                {
                    KV("name", em.Identifier.ValueText),
                    KV("value", em.EqualsValue?.Value?.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(em.AttributeLists, id);
                break;
            }
            case DelegateDeclarationSyntax del:
            {
                var id = CreateDeclarationNode(del, "CSharpDelegateDeclaration", new[]
                {
                    KV("name", del.Identifier.ValueText),
                    KV("returnType", del.ReturnType.ToString()),
                    KV("accessibility", Accessibility(del.Modifiers)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(del.AttributeLists, id);
                AddRel(id, "RETURNS", AddTypeNode(del.ReturnType));
                BuildConstraintClauses(del.ConstraintClauses, id);
                for (var i = 0; i < del.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(del.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case MethodDeclarationSyntax m:
            {
                var id = CreateDeclarationNode(m, "CSharpMethodDeclaration", new[]
                {
                    KV("name", m.Identifier.ValueText),
                    KV("returnType", m.ReturnType.ToString()),
                    KV("accessibility", Accessibility(m.Modifiers)),
                    KV("isStatic", m.Modifiers.Any(SyntaxKind.StaticKeyword)),
                    KV("isAsync", m.Modifiers.Any(SyntaxKind.AsyncKeyword)),
                    KV("isAbstract", m.Modifiers.Any(SyntaxKind.AbstractKeyword)),
                    KV("isVirtual", m.Modifiers.Any(SyntaxKind.VirtualKeyword)),
                    KV("isOverride", m.Modifiers.Any(SyntaxKind.OverrideKeyword)),
                    KV("isPartial", m.Modifiers.Any(SyntaxKind.PartialKeyword)),
                    KV("typeParameters", m.TypeParameterList?.Parameters.Select(tp => tp.Identifier.ValueText).ToArray()),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(m.AttributeLists, id);
                AddRel(id, "RETURNS", AddTypeNode(m.ReturnType));
                BuildConstraintClauses(m.ConstraintClauses, id);
                if (m.TypeParameterList is not null)
                {
                    for (var i = 0; i < m.TypeParameterList.Parameters.Count; i++)
                    {
                        AddRel(id, "HAS_TYPE_PARAMETER", BuildNode(m.TypeParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                for (var i = 0; i < m.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(m.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (m.Body is not null)
                {
                    BuildNode(m.Body, "HAS_BODY", id);
                }
                else if (m.ExpressionBody is not null)
                {
                    BuildNode(m.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(m);
                break;
            }
            case ConstructorDeclarationSyntax ctor:
            {
                var id = CreateDeclarationNode(ctor, "CSharpConstructorDeclaration", new[]
                {
                    KV("name", ctor.Identifier.ValueText),
                    KV("accessibility", Accessibility(ctor.Modifiers)),
                    KV("isStatic", ctor.Modifiers.Any(SyntaxKind.StaticKeyword)),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(ctor.AttributeLists, id);
                for (var i = 0; i < ctor.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(ctor.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (ctor.Initializer is not null)
                {
                    BuildNode(ctor.Initializer, "INITIALIZER", id);
                }
                if (ctor.Body is not null)
                {
                    BuildNode(ctor.Body, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(ctor);
                break;
            }
            case DestructorDeclarationSyntax dtor:
            {
                var id = CreateDeclarationNode(dtor, "CSharpDestructorDeclaration", new[]
                {
                    KV("name", dtor.Identifier.ValueText),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(dtor.AttributeLists, id);
                if (dtor.Body is not null)
                {
                    BuildNode(dtor.Body, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(dtor);
                break;
            }
            case OperatorDeclarationSyntax op:
            {
                var id = CreateDeclarationNode(op, "CSharpOperatorDeclaration", new[]
                {
                    KV("operator", op.OperatorToken.Text),
                    KV("returnType", op.ReturnType.ToString()),
                    KV("accessibility", Accessibility(op.Modifiers)),
                    KV("isStatic", op.Modifiers.Any(SyntaxKind.StaticKeyword)),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(op.AttributeLists, id);
                AddRel(id, "RETURNS", AddTypeNode(op.ReturnType));
                for (var i = 0; i < op.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(op.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (op.Body is not null)
                {
                    BuildNode(op.Body, "HAS_BODY", id);
                }
                else if (op.ExpressionBody is not null)
                {
                    BuildNode(op.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(op);
                break;
            }
            case ConversionOperatorDeclarationSyntax co:
            {
                var id = CreateDeclarationNode(co, "CSharpConversionOperatorDeclaration", new[]
                {
                    KV("kind", co.ImplicitOrExplicitKeyword.Text),
                    KV("returnType", co.Type.ToString()),
                    KV("accessibility", Accessibility(co.Modifiers)),
                    KV("isStatic", co.Modifiers.Any(SyntaxKind.StaticKeyword)),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(co.AttributeLists, id);
                AddRel(id, "RETURNS", AddTypeNode(co.Type));
                for (var i = 0; i < co.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(co.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (co.Body is not null)
                {
                    BuildNode(co.Body, "HAS_BODY", id);
                }
                else if (co.ExpressionBody is not null)
                {
                    BuildNode(co.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(co);
                break;
            }
            case PropertyDeclarationSyntax pr:
            {
                var id = CreateDeclarationNode(pr, "CSharpPropertyDeclaration", new[]
                {
                    KV("name", pr.Identifier.ValueText),
                    KV("type", pr.Type.ToString()),
                    KV("accessibility", Accessibility(pr.Modifiers)),
                    KV("isStatic", pr.Modifiers.Any(SyntaxKind.StaticKeyword)),
                    KV("isAbstract", pr.Modifiers.Any(SyntaxKind.AbstractKeyword)),
                    KV("isVirtual", pr.Modifiers.Any(SyntaxKind.VirtualKeyword)),
                    KV("isOverride", pr.Modifiers.Any(SyntaxKind.OverrideKeyword)),
                    KV("hasGetter", pr.AccessorList?.Accessors.Any(a => a.Kind() == SyntaxKind.GetAccessorDeclaration) ?? false),
                    KV("hasSetter", pr.AccessorList?.Accessors.Any(a => a.Kind() == SyntaxKind.SetAccessorDeclaration) ?? false),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(pr.AttributeLists, id);
                AddRel(id, "OF_TYPE", AddTypeNode(pr.Type));
                if (pr.AccessorList is not null)
                {
                    foreach (var a in pr.AccessorList.Accessors)
                    {
                        BuildNode(a, "HAS_ACCESSOR", id);
                    }
                }
                if (pr.ExpressionBody is not null)
                {
                    BuildNode(pr.ExpressionBody, "HAS_BODY", id);
                }
                if (pr.Initializer is not null)
                {
                    BuildNode(pr.Initializer, "INITIALIZER", id);
                }
                _methodStack.Pop();
                if (pr.ExpressionBody is not null)
                {
                    BuildCfg(pr.ExpressionBody);
                }
                break;
            }
            case IndexerDeclarationSyntax idx:
            {
                var id = CreateDeclarationNode(idx, "CSharpIndexerDeclaration", new[]
                {
                    KV("type", idx.Type.ToString()),
                    KV("accessibility", Accessibility(idx.Modifiers)),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(idx.AttributeLists, id);
                for (var i = 0; i < idx.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(idx.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (idx.AccessorList is not null)
                {
                    foreach (var a in idx.AccessorList.Accessors)
                    {
                        BuildNode(a, "HAS_ACCESSOR", id);
                    }
                }
                if (idx.ExpressionBody is not null)
                {
                    BuildNode(idx.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                if (idx.ExpressionBody is not null)
                {
                    BuildCfg(idx.ExpressionBody);
                }
                break;
            }
            case AccessorDeclarationSyntax acc:
            {
                var id = CreateNode(acc, new[] { "CSharpAccessor" }, new[] { KV("kind", acc.Keyword.ValueText) });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(acc.AttributeLists, id);
                if (acc.Body is not null)
                {
                    BuildNode(acc.Body, "HAS_BODY", id);
                }
                else if (acc.ExpressionBody is not null)
                {
                    BuildNode(acc.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(acc);
                break;
            }
            case FieldDeclarationSyntax f:
            {
                var id = CreateDeclarationNode(f, "CSharpFieldDeclaration", new[]
                {
                    KV("type", f.Declaration.Type.ToString()),
                    KV("accessibility", Accessibility(f.Modifiers)),
                    KV("isStatic", f.Modifiers.Any(SyntaxKind.StaticKeyword)),
                    KV("isConst", f.Modifiers.Any(SyntaxKind.ConstKeyword)),
                    KV("isReadOnly", f.Modifiers.Any(SyntaxKind.ReadOnlyKeyword)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(f.AttributeLists, id);
                AddRel(id, "OF_TYPE", AddTypeNode(f.Declaration.Type));
                foreach (var v in f.Declaration.Variables)
                {
                    BuildNode(v, "DECLARES", id);
                }
                break;
            }
            case EventFieldDeclarationSyntax ef:
            {
                var id = CreateDeclarationNode(ef, "CSharpEventFieldDeclaration", new[]
                {
                    KV("type", ef.Declaration.Type.ToString()),
                    KV("accessibility", Accessibility(ef.Modifiers)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(ef.AttributeLists, id);
                foreach (var v in ef.Declaration.Variables)
                {
                    BuildNode(v, "DECLARES", id);
                }
                break;
            }
            case EventDeclarationSyntax ev:
            {
                var id = CreateDeclarationNode(ev, "CSharpEventDeclaration", new[]
                {
                    KV("name", ev.Identifier.ValueText),
                    KV("type", ev.Type.ToString()),
                    KV("accessibility", Accessibility(ev.Modifiers)),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(ev.AttributeLists, id);
                if (ev.AccessorList is not null)
                {
                    foreach (var a in ev.AccessorList.Accessors)
                    {
                        BuildNode(a, "HAS_ACCESSOR", id);
                    }
                }
                break;
            }
            case VariableDeclaratorSyntax vd:
            {
                var id = CreateDeclarationNode(vd, "CSharpVariableDeclarator", new[]
                {
                    KV("name", vd.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                if (vd.Initializer is not null)
                {
                    BuildNode(vd.Initializer, "INITIALIZER", id);
                }
                break;
            }
            case VariableDeclarationSyntax vdecl:
            {
                var id = CreateNode(vdecl, new[] { "CSharpVariableDeclaration", "CSharpDeclaration" }, new[]
                {
                    KV("type", vdecl.Type.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                AddRel(id, "OF_TYPE", AddTypeNode(vdecl.Type));
                foreach (var v in vdecl.Variables)
                {
                    BuildNode(v, "DECLARES", id);
                }
                break;
            }
            case ParameterSyntax p:
            {
                var id = CreateNode(p, new[] { "CSharpParameter" }, new[]
                {
                    KV("name", p.Identifier.ValueText),
                    KV("type", p.Type?.ToString() ?? ""),
                    KV("isRef", p.Modifiers.Any(SyntaxKind.RefKeyword)),
                    KV("isOut", p.Modifiers.Any(SyntaxKind.OutKeyword)),
                    KV("isIn", p.Modifiers.Any(SyntaxKind.InKeyword)),
                    KV("isParams", p.Modifiers.Any(SyntaxKind.ParamsKeyword)),
                    KV("isThis", p.Modifiers.Any(SyntaxKind.ThisKeyword)),
                    KV("hasDefault", p.Default is not null),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(p.AttributeLists, id);
                if (p.Type is not null)
                {
                    AddRel(id, "OF_TYPE", AddTypeNode(p.Type));
                }
                if (p.Default is not null)
                {
                    BuildNode(p.Default, "HAS_DEFAULT", id);
                }
                break;
            }
            case TypeParameterSyntax tp:
            {
                var id = CreateDeclarationNode(tp, "CSharpTypeParameter", new[]
                {
                    KV("name", tp.Identifier.ValueText),
                    KV("variance", tp.VarianceKeyword.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(tp.AttributeLists, id);
                break;
            }

            // -- statements -------------------------------------------
            case BlockSyntax b:
            {
                var id = CreateNode(b, new[] { "CSharpBlock" }, Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                AddFollowsChain(b.Statements, id);
                break;
            }
            case LocalDeclarationStatementSyntax lds:
            {
                var id = CreateStatementNode(lds, "CSharpLocalDeclarationStatement", new[]
                {
                    KV("isConst", lds.Modifiers.Any(SyntaxKind.ConstKeyword)),
                    KV("isUsing", lds.UsingKeyword != default),
                });
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(lds.AttributeLists, id);
                BuildNode(lds.Declaration, "DECLARES", id);
                break;
            }
            case ExpressionStatementSyntax es:
            {
                var id = CreateStatementNode(es, "CSharpExpressionStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(es.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case IfStatementSyntax iff:
            {
                var id = CreateStatementNode(iff, "CSharpIfStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(iff.Condition, "HAS_CONDITION", id);
                BuildNode(iff.Statement, "HAS_BODY", id);
                if (iff.Else is not null)
                {
                    BuildNode(iff.Else, "HAS_ELSE", id);
                }
                break;
            }
            case ElseClauseSyntax ec:
                BuildNode(ec.Statement, parentEdge, parentId);
                break;
            case WhileStatementSyntax w:
            {
                var id = CreateStatementNode(w, "CSharpWhileStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(w.Condition, "HAS_CONDITION", id);
                BuildNode(w.Statement, "HAS_BODY", id);
                break;
            }
            case DoStatementSyntax dw:
            {
                var id = CreateStatementNode(dw, "CSharpDoStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(dw.Condition, "HAS_CONDITION", id);
                BuildNode(dw.Statement, "HAS_BODY", id);
                break;
            }
            case ForStatementSyntax f:
            {
                var id = CreateStatementNode(f, "CSharpForStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                if (f.Declaration is not null)
                {
                    BuildNode(f.Declaration, "INITIALIZER", id);
                }
                foreach (var init in f.Initializers)
                {
                    BuildNode(init, "INITIALIZER", id);
                }
                if (f.Condition is not null)
                {
                    BuildNode(f.Condition, "HAS_CONDITION", id);
                }
                foreach (var inc in f.Incrementors)
                {
                    BuildNode(inc, "HAS_INCREMENTOR", id);
                }
                BuildNode(f.Statement, "HAS_BODY", id);
                break;
            }
            case ForEachStatementSyntax fe:
            {
                var id = CreateStatementNode(fe, "CSharpForEachStatement", new[]
                {
                    KV("identifier", fe.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                AddRel(id, "OF_TYPE", AddTypeNode(fe.Type));
                BuildNode(fe.Expression, "HAS_EXPRESSION", id);
                BuildNode(fe.Statement, "HAS_BODY", id);
                break;
            }
            case ReturnStatementSyntax r:
            {
                var id = CreateStatementNode(r, "CSharpReturnStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                if (r.Expression is not null)
                {
                    BuildNode(r.Expression, "HAS_EXPRESSION", id);
                }
                break;
            }
            case ThrowStatementSyntax th:
            {
                var id = CreateStatementNode(th, "CSharpThrowStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                if (th.Expression is not null)
                {
                    BuildNode(th.Expression, "HAS_EXPRESSION", id);
                }
                break;
            }
            case SwitchStatementSyntax sw:
            {
                var id = CreateStatementNode(sw, "CSharpSwitchStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(sw.Expression, "HAS_EXPRESSION", id);
                for (var i = 0; i < sw.Sections.Count; i++)
                {
                    AddRel(id, "HAS_CASE", BuildNode(sw.Sections[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case SwitchSectionSyntax sec:
            {
                var id = CreateNode(sec, new[] { "CSharpSwitchSection", "CSharpStatement" }, Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                foreach (var label in sec.Labels)
                {
                    BuildNode(label, "HAS_LABEL", id);
                }
                AddFollowsChain(sec.Statements, id);
                break;
            }
            case CaseSwitchLabelSyntax csl:
            {
                var id = CreateNode(csl, new[] { "CSharpCaseLabel", "CSharpStatement" }, new[] { KV("value", csl.Value.ToString()) });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case CasePatternSwitchLabelSyntax cpsl:
            {
                var id = CreateNode(cpsl, new[] { "CSharpCaseLabel", "CSharpStatement" }, new[] { KV("pattern", cpsl.Pattern.ToString()) });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case DefaultSwitchLabelSyntax dsl:
            {
                var id = CreateNode(dsl, new[] { "CSharpCaseLabel", "CSharpStatement" }, new[] { KV("value", "default") });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case BreakStatementSyntax brk:
            {
                var id = CreateStatementNode(brk, "CSharpBreakStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                break;
            }
            case ContinueStatementSyntax cont:
            {
                var id = CreateStatementNode(cont, "CSharpContinueStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                break;
            }
            case GotoStatementSyntax go:
            {
                var id = CreateStatementNode(go, "CSharpGotoStatement", new[]
                {
                    KV("caseOrDefault", go.CaseOrDefaultKeyword.ValueText),
                    KV("target", go.Expression?.ToString() ?? string.Empty),
                });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case YieldStatementSyntax y:
            {
                var id = CreateStatementNode(y, "CSharpYieldStatement", new[]
                {
                    KV("isReturn", y.ReturnOrBreakKeyword.IsKind(SyntaxKind.ReturnKeyword)),
                });
                AddRel(parentId, parentEdge, id);
                if (y.Expression is not null)
                {
                    BuildNode(y.Expression, "HAS_EXPRESSION", id);
                }
                break;
            }
            case TryStatementSyntax t:
            {
                var id = CreateStatementNode(t, "CSharpTryStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(t.Block, "HAS_BODY", id);
                for (var i = 0; i < t.Catches.Count; i++)
                {
                    AddRel(id, "HAS_CATCH", BuildNode(t.Catches[i], null, null), new[] { KV("ordinal", i) });
                }
                if (t.Finally is not null)
                {
                    BuildNode(t.Finally, "HAS_FINALLY", id);
                }
                break;
            }
            case CatchClauseSyntax cc:
            {
                var id = CreateStatementNode(cc, "CSharpCatchClause", new[]
                {
                    KV("exceptionType", cc.Declaration?.Type?.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                if (cc.Declaration is not null)
                {
                    BuildNode(cc.Declaration, "HAS_EXCEPTION", id);
                }
                BuildNode(cc.Block, "HAS_BODY", id);
                break;
            }
            case CatchDeclarationSyntax cd:
            {
                var id = CreateNode(cd, new[] { "CSharpCatchDeclaration", "CSharpStatement" }, new[]
                {
                    KV("name", cd.Identifier.ValueText),
                    KV("type", cd.Type.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case FinallyClauseSyntax fc:
                BuildNode(fc.Block, parentEdge, parentId);
                break;
            case UsingStatementSyntax us:
            {
                var id = CreateStatementNode(us, "CSharpUsingStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                if (us.Declaration is not null)
                {
                    BuildNode(us.Declaration, "DECLARES", id);
                }
                if (us.Expression is not null)
                {
                    BuildNode(us.Expression, "HAS_EXPRESSION", id);
                }
                BuildNode(us.Statement, "HAS_BODY", id);
                break;
            }
            case LockStatementSyntax lk:
            {
                var id = CreateStatementNode(lk, "CSharpLockStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                BuildNode(lk.Expression, "HAS_EXPRESSION", id);
                BuildNode(lk.Statement, "HAS_BODY", id);
                break;
            }
            case CheckedStatementSyntax chk:
                BuildNode(chk.Block, parentEdge, parentId);
                break;
            case LocalFunctionStatementSyntax lf:
            {
                var id = CreateStatementNode(lf, "CSharpLocalFunctionDeclaration", new[]
                {
                    KV("name", lf.Identifier.ValueText),
                    KV("returnType", lf.ReturnType.ToString()),
                    KV("isStatic", lf.Modifiers.Any(SyntaxKind.StaticKeyword)),
                    KV("isAsync", lf.Modifiers.Any(SyntaxKind.AsyncKeyword)),
                });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildAttributeLists(lf.AttributeLists, id);
                AddRel(id, "RETURNS", AddTypeNode(lf.ReturnType));
                BuildConstraintClauses(lf.ConstraintClauses, id);
                for (var i = 0; i < lf.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(lf.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                if (lf.Body is not null)
                {
                    BuildNode(lf.Body, "HAS_BODY", id);
                }
                else if (lf.ExpressionBody is not null)
                {
                    BuildNode(lf.ExpressionBody, "HAS_BODY", id);
                }
                _methodStack.Pop();
                BuildCfg(lf);
                break;
            }
            case LabeledStatementSyntax lb:
            {
                var id = CreateStatementNode(lb, "CSharpLabeledStatement", new[] { KV("label", lb.Identifier.ValueText) });
                AddRel(parentId, parentEdge, id);
                BuildNode(lb.Statement, "HAS_BODY", id);
                break;
            }
            case EmptyStatementSyntax empty:
            {
                var id = CreateStatementNode(empty, "CSharpEmptyStatement", Array.Empty<KeyValuePair<string, object?>>());
                AddRel(parentId, parentEdge, id);
                break;
            }

            // -- expressions ------------------------------------------
            case ParenthesizedExpressionSyntax pe:
                BuildNode(pe.Expression, parentEdge, parentId);
                break;
            case IdentifierNameSyntax idn:
            {
                var props = new List<KeyValuePair<string, object?>>
                {
                    KV("name", idn.Identifier.ValueText),
                    KV("text", idn.ToString()),
                };
                props.AddRange(SymbolProps(idn));
                var id = CreateExpressionNode(idn, "CSharpIdentifierName", props);
                AddRel(parentId, parentEdge, id);
                break;
            }
            case GenericNameSyntax gn:
            {
                var props = new List<KeyValuePair<string, object?>>
                {
                    KV("name", gn.Identifier.ValueText),
                    KV("text", gn.ToString()),
                };
                props.AddRange(SymbolProps(gn));
                var id = CreateExpressionNode(gn, "CSharpGenericName", props);
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < gn.TypeArgumentList.Arguments.Count; i++)
                {
                    AddRel(id, "HAS_TYPE_ARGUMENT", AddTypeNode(gn.TypeArgumentList.Arguments[i]), new[] { KV("ordinal", i) });
                }
                break;
            }
            case QualifiedNameSyntax qn:
            {
                var id = CreateExpressionNode(qn, "CSharpQualifiedName", new[] { KV("text", qn.ToString()) });
                AddRel(parentId, parentEdge, id);
                BuildNode(qn.Left, "HAS_EXPRESSION", id);
                BuildNode(qn.Right, "HAS_EXPRESSION", id);
                break;
            }
            case LiteralExpressionSyntax lit:
            {
                var id = CreateExpressionNode(lit, "CSharpLiteralExpression", new[]
                {
                    KV("kind", lit.Kind().ToString()),
                    KV("value", lit.Token.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                break;
            }
            case MemberAccessExpressionSyntax ma:
            {
                var props = new List<KeyValuePair<string, object?>>
                {
                    KV("name", ma.Name.Identifier.ValueText),
                    KV("text", ma.ToString()),
                };
                props.AddRange(SymbolProps(ma));
                var id = CreateExpressionNode(ma, "CSharpMemberAccessExpression", props);
                AddRel(parentId, parentEdge, id);
                BuildNode(ma.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case InvocationExpressionSyntax inv:
            {
                var props = new List<KeyValuePair<string, object?>> { KV("text", inv.ToString()) };
                props.AddRange(SymbolProps(inv));
                var id = CreateExpressionNode(inv, "CSharpInvocationExpression", props);
                AddRel(parentId, parentEdge, id);
                AddCallEdge(inv);
                BuildNode(inv.Expression, "HAS_FUNCTION", id);
                if (inv.ArgumentList is not null)
                {
                    for (var i = 0; i < inv.ArgumentList.Arguments.Count; i++)
                    {
                        AddRel(id, "HAS_ARGUMENT", BuildNode(inv.ArgumentList.Arguments[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                break;
            }
            case ObjectCreationExpressionSyntax oc:
            {
                var props = new List<KeyValuePair<string, object?>>
                {
                    KV("type", oc.Type.ToString()),
                    KV("text", oc.ToString()),
                };
                props.AddRange(SymbolProps(oc));
                var id = CreateExpressionNode(oc, "CSharpObjectCreationExpression", props);
                AddRel(parentId, parentEdge, id);
                AddCallEdge(oc);
                if (oc.ArgumentList is not null)
                {
                    for (var i = 0; i < oc.ArgumentList.Arguments.Count; i++)
                    {
                        AddRel(id, "HAS_ARGUMENT", BuildNode(oc.ArgumentList.Arguments[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                if (oc.Initializer is not null)
                {
                    BuildNode(oc.Initializer, "INITIALIZER", id);
                }
                break;
            }
            case ArgumentSyntax arg:
            {
                var id = CreateNode(arg, new[] { "CSharpArgument", "CSharpExpression" }, new[]
                {
                    KV("name", arg.NameColon?.Name.Identifier.ValueText),
                    KV("isRef", arg.RefKindKeyword.IsKind(SyntaxKind.RefKeyword)),
                    KV("isOut", arg.RefKindKeyword.IsKind(SyntaxKind.OutKeyword)),
                    KV("isIn", arg.RefKindKeyword.IsKind(SyntaxKind.InKeyword)),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(arg.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case AttributeArgumentSyntax attrArg:
            {
                var id = CreateNode(attrArg, new[] { "CSharpAttributeArgument", "CSharpExpression" }, new[]
                {
                    KV("name", attrArg.NameEquals?.Name.Identifier.ValueText ?? attrArg.NameColon?.Name.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(attrArg.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case ArgumentListSyntax al:
                foreach (var a in al.Arguments)
                {
                    BuildNode(a, parentEdge, parentId);
                }
                break;
            case BracketedArgumentListSyntax bal:
                foreach (var a in bal.Arguments)
                {
                    BuildNode(a, parentEdge, parentId);
                }
                break;
            case BinaryExpressionSyntax be:
            {
                var id = CreateExpressionNode(be, "CSharpBinaryExpression", new[]
                {
                    KV("operator", be.OperatorToken.Text),
                    KV("text", be.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                AddRel(id, "HAS_OPERAND", BuildNode(be.Left, null, null), new[] { KV("ordinal", 0) });
                AddRel(id, "HAS_OPERAND", BuildNode(be.Right, null, null), new[] { KV("ordinal", 1) });
                break;
            }
            case AssignmentExpressionSyntax ae:
            {
                var id = CreateExpressionNode(ae, "CSharpAssignmentExpression", new[]
                {
                    KV("operator", ae.OperatorToken.Text),
                    KV("text", ae.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(ae.Left, "HAS_TARGET", id);
                BuildNode(ae.Right, "HAS_VALUE", id);
                break;
            }
            case PrefixUnaryExpressionSyntax pu:
            {
                var id = CreateExpressionNode(pu, "CSharpUnaryExpression", new[]
                {
                    KV("operator", pu.OperatorToken.Text),
                    KV("text", pu.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(pu.Operand, "HAS_OPERAND", id);
                break;
            }
            case PostfixUnaryExpressionSyntax po:
            {
                var id = CreateExpressionNode(po, "CSharpUnaryExpression", new[]
                {
                    KV("operator", po.OperatorToken.Text),
                    KV("text", po.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(po.Operand, "HAS_OPERAND", id);
                break;
            }
            case ConditionalExpressionSyntax cond:
            {
                var id = CreateExpressionNode(cond, "CSharpConditionalExpression", new[] { KV("text", cond.ToString()) });
                AddRel(parentId, parentEdge, id);
                BuildNode(cond.Condition, "HAS_CONDITION", id);
                BuildNode(cond.WhenTrue, "HAS_BODY", id);
                BuildNode(cond.WhenFalse, "HAS_ELSE", id);
                break;
            }
            case SwitchExpressionSyntax swe:
            {
                var id = CreateExpressionNode(swe, "CSharpSwitchExpression", new[] { KV("text", swe.ToString()) });
                AddRel(parentId, parentEdge, id);
                BuildNode(swe.GoverningExpression, "HAS_EXPRESSION", id);
                for (var i = 0; i < swe.Arms.Count; i++)
                {
                    AddRel(id, "HAS_ARM", BuildNode(swe.Arms[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case SwitchExpressionArmSyntax arm:
            {
                var id = CreateNode(arm, new[] { "CSharpSwitchExpressionArm" }, new[]
                {
                    KV("pattern", arm.Pattern.ToString()),
                    KV("whenCondition", arm.WhenClause?.Condition.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(arm.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case CastExpressionSyntax cast:
            {
                var id = CreateExpressionNode(cast, "CSharpCastExpression", new[]
                {
                    KV("type", cast.Type.ToString()),
                    KV("text", cast.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(cast.Expression, "HAS_OPERAND", id);
                break;
            }
            case ElementAccessExpressionSyntax ea:
            {
                var id = CreateExpressionNode(ea, "CSharpElementAccessExpression", new[] { KV("text", ea.ToString()) });
                AddRel(parentId, parentEdge, id);
                BuildNode(ea.Expression, "HAS_EXPRESSION", id);
                if (ea.ArgumentList is not null)
                {
                    for (var i = 0; i < ea.ArgumentList.Arguments.Count; i++)
                    {
                        AddRel(id, "HAS_ARGUMENT", BuildNode(ea.ArgumentList.Arguments[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                break;
            }
            case SimpleLambdaExpressionSyntax sl:
            {
                var id = CreateExpressionNode(sl, "CSharpLambdaExpression", new[] { KV("text", sl.ToString()) });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                BuildNode(sl.Parameter, "HAS_PARAMETER", id);
                BuildNode(sl.Body, "HAS_BODY", id);
                _methodStack.Pop();
                BuildCfg(sl);
                break;
            }
            case ParenthesizedLambdaExpressionSyntax pl:
            {
                var id = CreateExpressionNode(pl, "CSharpLambdaExpression", new[] { KV("text", pl.ToString()) });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < pl.ParameterList.Parameters.Count; i++)
                {
                    AddRel(id, "HAS_PARAMETER", BuildNode(pl.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                }
                BuildNode(pl.Body, "HAS_BODY", id);
                _methodStack.Pop();
                BuildCfg(pl);
                break;
            }
            case AnonymousMethodExpressionSyntax am:
            {
                var id = CreateExpressionNode(am, "CSharpAnonymousMethodExpression", new[] { KV("text", am.ToString()) });
                _methodStack.Push(id);
                AddRel(parentId, parentEdge, id);
                if (am.ParameterList is not null)
                {
                    for (var i = 0; i < am.ParameterList.Parameters.Count; i++)
                    {
                        AddRel(id, "HAS_PARAMETER", BuildNode(am.ParameterList.Parameters[i], null, null), new[] { KV("ordinal", i) });
                    }
                }
                BuildNode(am.Block, "HAS_BODY", id);
                _methodStack.Pop();
                BuildCfg(am);
                break;
            }
            case TupleExpressionSyntax te:
            {
                var id = CreateExpressionNode(te, "CSharpTupleExpression", new[] { KV("text", te.ToString()) });
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < te.Arguments.Count; i++)
                {
                    AddRel(id, "HAS_ARGUMENT", BuildNode(te.Arguments[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case InterpolatedStringExpressionSyntax istr:
            {
                var id = CreateExpressionNode(istr, "CSharpInterpolatedStringExpression", new[] { KV("text", istr.ToString()) });
                AddRel(parentId, parentEdge, id);
                var holeOrdinal = 0;
                foreach (var content in istr.Contents)
                {
                    if (content is InterpolationSyntax interp)
                    {
                        AddRel(id, "HAS_OPERAND", BuildNode(interp.Expression, null, null), new[] { KV("ordinal", holeOrdinal++) });
                    }
                }
                break;
            }
            case InitializerExpressionSyntax init:
            {
                var id = CreateExpressionNode(init, "CSharpInitializerExpression", new[]
                {
                    KV("kind", init.Kind().ToString()),
                    KV("text", init.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < init.Expressions.Count; i++)
                {
                    AddRel(id, "HAS_ELEMENT", BuildNode(init.Expressions[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case ConstructorInitializerSyntax ci:
            {
                var id = CreateExpressionNode(ci, "CSharpConstructorInitializer", new[]
                {
                    KV("kind", ci.Kind() == SyntaxKind.BaseConstructorInitializer ? "base" : "this"),
                });
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < ci.ArgumentList.Arguments.Count; i++)
                {
                    AddRel(id, "HAS_ARGUMENT", BuildNode(ci.ArgumentList.Arguments[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case QueryExpressionSyntax qe:
            {
                var id = CreateExpressionNode(qe, "CSharpQueryExpression", new[] { KV("text", qe.ToString()) });
                AddRel(parentId, parentEdge, id);
                BuildQueryBody(qe.FromClause, qe.Body, id);
                break;
            }
            case FromClauseSyntax fc:
            {
                var id = CreateNode(fc, new[] { "CSharpQueryClause" }, new[]
                {
                    KV("kind", "from"),
                    KV("identifier", fc.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                if (fc.Type is not null)
                {
                    AddRel(id, "OF_TYPE", AddTypeNode(fc.Type));
                }
                BuildNode(fc.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case LetClauseSyntax lc:
            {
                var id = CreateNode(lc, new[] { "CSharpQueryClause" }, new[]
                {
                    KV("kind", "let"),
                    KV("identifier", lc.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(lc.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case WhereClauseSyntax wc:
            {
                var id = CreateNode(wc, new[] { "CSharpQueryClause" }, new[] { KV("kind", "where") });
                AddRel(parentId, parentEdge, id);
                BuildNode(wc.Condition, "HAS_CONDITION", id);
                break;
            }
            case JoinClauseSyntax jc:
            {
                var id = CreateNode(jc, new[] { "CSharpQueryClause" }, new[]
                {
                    KV("kind", "join"),
                    KV("identifier", jc.Identifier.ValueText),
                    KV("into", jc.Into?.Identifier.ValueText),
                });
                AddRel(parentId, parentEdge, id);
                if (jc.Type is not null)
                {
                    AddRel(id, "OF_TYPE", AddTypeNode(jc.Type));
                }
                BuildNode(jc.InExpression, "HAS_EXPRESSION", id);
                BuildNode(jc.LeftExpression, "HAS_CONDITION", id);
                BuildNode(jc.RightExpression, "HAS_CONDITION", id);
                break;
            }
            case OrderByClauseSyntax oc:
            {
                var id = CreateNode(oc, new[] { "CSharpQueryClause" }, new[] { KV("kind", "orderby") });
                AddRel(parentId, parentEdge, id);
                for (var i = 0; i < oc.Orderings.Count; i++)
                {
                    AddRel(id, "HAS_ORDERING", BuildNode(oc.Orderings[i], null, null), new[] { KV("ordinal", i) });
                }
                break;
            }
            case OrderingSyntax ord:
            {
                var id = CreateNode(ord, new[] { "CSharpOrdering" }, new[]
                {
                    KV("direction", ord.Kind() == SyntaxKind.DescendingOrdering ? "descending" : "ascending"),
                });
                AddRel(parentId, parentEdge, id);
                BuildNode(ord.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case SelectClauseSyntax sc:
            {
                var id = CreateNode(sc, new[] { "CSharpQueryClause" }, new[] { KV("kind", "select") });
                AddRel(parentId, parentEdge, id);
                BuildNode(sc.Expression, "HAS_EXPRESSION", id);
                break;
            }
            case GroupClauseSyntax gc:
            {
                var id = CreateNode(gc, new[] { "CSharpQueryClause" }, new[] { KV("kind", "group") });
                AddRel(parentId, parentEdge, id);
                BuildNode(gc.GroupExpression, "HAS_EXPRESSION", id);
                BuildNode(gc.ByExpression, "HAS_KEY", id);
                break;
            }
            case ArrowExpressionClauseSyntax aec:
                return BuildNode(aec.Expression, parentEdge, parentId);
            case EqualsValueClauseSyntax evc:
                return BuildNode(evc.Value, parentEdge, parentId);

            // A bare type used in an expression position becomes a type node.
            case TypeSyntax ts:
            {
                var id = AddTypeNode(ts);
                AddRel(parentId, parentEdge, id);
                break;
            }

            // Fallbacks for any statement / expression kind not enumerated above.
            case StatementSyntax stmt:
            {
                var id = CreateNode(stmt, new[] { "CSharpStatement" }, new[]
                {
                    KV("kind", stmt.Kind().ToString()),
                    KV("text", stmt.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                foreach (var child in stmt.ChildNodes())
                {
                    BuildNode(child, "CONTAINS", id);
                }
                break;
            }
            case ExpressionSyntax expr:
            {
                var id = CreateNode(expr, new[] { "CSharpExpression" }, new[]
                {
                    KV("kind", expr.Kind().ToString()),
                    KV("text", expr.ToString()),
                });
                AddRel(parentId, parentEdge, id);
                var fallbackOrdinal = 0;
                foreach (var child in expr.ChildNodes().OfType<ExpressionSyntax>())
                {
                    AddRel(id, "HAS_OPERAND", BuildNode(child, null, null), new[] { KV("ordinal", fallbackOrdinal++) });
                }
                break;
            }

            default:
                break;
        }
        return _document.Nodes.Count > countBefore ? _document.Nodes[countBefore].Id : null;
    }
}
