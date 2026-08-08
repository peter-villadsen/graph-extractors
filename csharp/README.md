# C# → Cypher extractor (Roslyn)

A command-line application that parses C# source code, builds its Roslyn AST,
and emits a graph representation of the code as **Cypher** statements suitable
for import into [Neo4j](https://neo4j.com/).

| Parser / Library | Application | Node kinds come from |
| ----------------------------------------- | ---------------------- | ----------------------------------------------- |
| [Roslyn](https://github.com/dotnet/roslyn) (`Microsoft.CodeAnalysis.CSharp`) | `csharp/CSharpToCypher` | The C# language specification (ECMA-334) and Roslyn `SyntaxKind` |

## CLI contract

* positional `PATH` arguments — files **or** directories (scanned recursively);
* `--verbose` / `-v` — print progress information to **stderr**;
* `--ast` / `-a` — print the AST before generating Cypher, to **stderr**;
* `--souffle` — emit a **Souffle datalog** program for the control-flow graphs
  on **stdout** instead of Cypher, see [Souffle datalog](#souffle-datalog)
  below;
* `--help` / `-h` — usage help.

The CFG is emitted unconditionally (there is no `--cfg` flag, unlike the
Python extractor).

**stdout carries only one format** so it can be piped straight into
`cypher-shell`:

```bash
dotnet run --project csharp/CSharpToCypher -- csharp/samples/sample.cs | cypher-shell
```

---

## Requirements

* .NET SDK 8.0 (`dotnet` on PATH). The Roslyn `Microsoft.CodeAnalysis.CSharp`
  package (4.11.0) is pulled from NuGet on first restore.

## Run

```bash
dotnet run --project csharp/CSharpToCypher -- --verbose csharp/samples/sample.cs
# or the built binary
dotnet build csharp/CSharpToCypher
csharp/CSharpToCypher/bin/Debug/net8.0/CSharpToCypher.exe --ast csharp/samples/sample.cs
```

Start with the bundled `csharp/samples/sample.cs` (it exercises classes,
inheritance, control flow, exceptions and trivia). Point `PATH` at a directory
to process every `.cs` file in it.

## How it works

`CSharpSyntaxTree.ParseText(source)` produces a Roslyn syntax tree.
`GraphBuilder` (a recursive walker) maps `SyntaxNode`/`SyntaxKind` to nodes and
edges. Comments and preprocessor directives are `SyntaxTrivia`; they become
`CSharpTrivia` nodes linked with `HAS_TRIVIA`.

Node ids are `n1`, `n2`, … The generic labels `CSharpDeclaration`,
`CSharpStatement` and `CSharpExpression` are added so
`MATCH (s:CSharpStatement)` matches any statement kind.

## Node labels (a selection)

| Label | Source construct |
| ----- | ---------------- |
| `CSharpCompilationUnit` | the parsed file |
| `CSharpNamespaceDeclaration` | namespace |
| `CSharpUsingDirective` | using / using static / using-alias |
| `CSharpClassDeclaration`, `CSharpRecordDeclaration`, `CSharpRecordStructDeclaration`, `CSharpStructDeclaration`, `CSharpInterfaceDeclaration`, `CSharpEnumDeclaration` | `class_declaration`, `record_declaration`, … |
| `CSharpMethodDeclaration`, `CSharpConstructorDeclaration`, `CSharpDestructorDeclaration`, `CSharpPropertyDeclaration`, `CSharpIndexerDeclaration`, `CSharpFieldDeclaration`, `CSharpEventDeclaration`, `CSharpDelegateDeclaration`, `CSharpEnumMember` | member declarations |
| `CSharpParameter`, `CSharpTypeParameter`, `CSharpVariableDeclarator` | parameters and variables |
| `CSharpType` | referenced type names (base class, interfaces, parameter/variable/return types) |
| `CSharpBlock` | a statement block (`block`) |
| `CSharpIfStatement`, `CSharpWhileStatement`, `CSharpDoStatement`, `CSharpForStatement`, `CSharpForEachStatement`, `CSharpReturnStatement`, `CSharpThrowStatement`, `CSharpSwitchStatement`, `CSharpTryStatement`, `CSharpUsingStatement`, `CSharpLockStatement`, `CSharpLocalDeclarationStatement`, `CSharpExpressionStatement`, `CSharpYieldStatement`, `CSharpBreakStatement`, `CSharpContinueStatement`, `CSharpGotoStatement` | the corresponding statement productions |
| `CSharpCatchClause`, `CSharpSwitchSection`, `CSharpCaseLabel` | try/catch and switch parts |
| `CSharpExpression`, `CSharpBinaryExpression`, `CSharpInvocationExpression`, `CSharpMemberAccessExpression`, `CSharpLiteralExpression`, `CSharpIdentifierName`, `CSharpGenericName`, `CSharpObjectCreationExpression`, `CSharpAssignmentExpression`, `CSharpConditionalExpression`, `CSharpLambdaExpression`, … | expressions |
| `CSharpTrivia` | comments and preprocessing directives |

## Relationships (a selection)

```cypher
(:CSharpClassDeclaration)-[:DERIVED_FROM]->(:CSharpType)       -- base class
(:CSharpClassDeclaration)-[:IMPLEMENTS]->(:CSharpType)         -- interface
(:CSharpClassDeclaration)-[:DECLARES]->(:CSharpMethodDeclaration)
(:CSharpMethodDeclaration)-[:HAS_PARAMETER]->(:CSharpParameter)
(:CSharpParameter)-[:OF_TYPE]->(:CSharpType)
(:CSharpMethodDeclaration)-[:RETURNS]->(:CSharpType)
(:CSharpMethodDeclaration)-[:HAS_BODY]->(:CSharpBlock)
(:CSharpBlock)-[:CONTAINS]->(:CSharpWhileStatement)
(:CSharpWhileStatement)-[:HAS_CONDITION]->(:CSharpExpression) -- boolean_expression
(:CSharpWhileStatement)-[:HAS_BODY]->(:CSharpStatement)        -- embedded_statement
(:CSharpIfStatement)-[:HAS_CONDITION]->(:CSharpExpression)
(:CSharpIfStatement)-[:HAS_BODY]->(:CSharpStatement)
(:CSharpIfStatement)-[:HAS_ELSE]->(:CSharpStatement)
(:CSharpForStatement)-[:HAS_CONDITION]->(:CSharpExpression)    -- for_condition
(:CSharpForStatement)-[:HAS_INCREMENTOR]->(:CSharpExpression)
(:CSharpSwitchStatement)-[:HAS_CASE]->(:CSharpSwitchSection)
(:CSharpTryStatement)-[:HAS_CATCH]->(:CSharpCatchClause)
(:CSharpNode)-[:HAS_TRIVIA]->(:CSharpTrivia)
```

---

## Control flow graphs

The extractor models the **control flow graph** of every method, always
emitted, using Roslyn's
[`ControlFlowGraph`](https://learn.microsoft.com/dotnet/api/microsoft.codeanalysis.flowanalysis.controlflowgraph)
(`ControlFlowGraph.Create(declaration, model, …)`), the same analyzer that
powers Roslyn code-fixers and analyzers.

Each basic block becomes a `CSharpBasicBlock` node with `ordinal`,
`isEntry`/`isReachable`/`isFinal`, `scope` (the enclosing method), and source
line span. Successor edges are `FOLLOWS` relationships; branch edges carry a
`semantics` property. Every block links to the AST statement nodes it
executes via `CONTAINS`.

```cypher
(:CSharpBasicBlock)-[:FOLLOWS {semantics: 'Regular', whenTrue: true}]->(:CSharpBasicBlock)
(:CSharpBasicBlock)-[:CONTAINS]->(:CSharpStatement)
```

---

## Souffle datalog

Pass `--souffle` to emit a [Souffle](https://souffle-lang.github.io/) datalog
program on **stdout** instead of Cypher. It models the control-flow graph:
one fact per basic block and successor edge, scoped by the enclosing method,
plus a couple of standard analysis rules.

```bash
dotnet run --project csharp/CSharpToCypher -- --souffle csharp/samples/sample.cs | souffle -D-
```

The program defines three input relations and two derived relations:

| Relation | Tuple |
| -------- | ----- |
| `basic_block(id, function, ordinal, isEntry, isFinal)` | one row per basic block; ids are run-global `b1`, `b2`, … so all input files share one program |
| `control_flow_edge(source, target, function, condition)` | one row per `FOLLOWS` edge; `condition` carries the Roslyn branch semantics |
| `block_statement(block, statement, statement_kind, function)` | links a block to each AST statement it executes |
| `reachable(id, function)` | blocks reachable from the function's entry block |
| `path(source, target, function)` | transitive closure of `control_flow_edge` |

Facts and rules are emitted inline, and `.output reachable` / `.output path`
print the two derived relations when the program runs.

C# function scopes use the resolved symbol (e.g. `SampleApp.Models.Dog.Fly()`).

---

## Notes

* **Idempotency**: the tool emits plain `CREATE` statements with unique,
  per-run ids (`n1`, `n2`, …). Re-importing duplicates the graph; clear the
  database first or use `MATCH (n) DETACH DELETE n` between runs.
* **Performance**: relationship statements `MATCH` both endpoints by their
  `id` property before `CREATE`ing the edge, rather than referencing the
  `CREATE`-time variable name — each statement is its own autocommitted query
  when pasted into the Neo4j Browser or piped through `cypher-shell`, so a
  variable bound by an earlier `CREATE` isn't visible to a later one; without
  the `MATCH`, the edge would silently create fresh, unlabeled, disconnected
  nodes instead of linking the real ones. For large codebases, create an
  index per node label on `id` first (e.g.
  `CREATE INDEX FOR (n:CSharpDeclaration) ON (n.id);`) so those lookups
  aren't full label scans.
* **Trivia**: `SyntaxTrivia` includes whitespace and end-of-line trivia, which
  is filtered out; only comments and preprocessor directives are emitted.
* **Expressions** are deliberately shallow "for now": operators are stored as
  properties and operands linked with `HAS_OPERAND`; sub-expression detail can
  be deepened later without changing the overall shape.
* **Troubleshooting**: re-running an import duplicates the graph — clear it
  first with `cypher-shell "MATCH (n) DETACH DELETE n;"` (or
  `cypher-shell --database neo4j "MATCH (n) DETACH DELETE n;"` if you target a
  named database). If `cypher-shell` isn't on `PATH`, use the browser at
  `http://localhost:7474` and paste the statements into the query bar instead.
