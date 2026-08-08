# Source Code → Neo4j Cypher Extractors

This repo turns source code into a **property graph**. Each extractor parses
source with the language's own parser, walks the resulting AST, and
represents the code as nodes and relationships — a function is a node, its
parameters are nodes linked by `HAS_PARAMETER`, a class's base type is linked
by `DERIVED_FROM`, a branch is a `FOLLOWS` edge between two control-flow basic
blocks, and so on. The graph is emitted as **Cypher** `CREATE` statements
ready to pipe into [Neo4j](https://neo4j.com/), or — for control-flow graphs
specifically — as [Souffle](https://souffle-lang.github.io/) datalog facts.

## Why a graph

Most interesting questions about a codebase are graph questions: "what calls
this method", "what's reachable from this branch", "which classes implement
this interface", "which basic blocks execute this statement". Cypher answers
those directly against the emitted graph instead of requiring a hand-rolled
AST walk for every question, and Souffle's fixpoint evaluation is a natural
fit for reachability-style analysis over the control-flow graph.

## Pipeline

Each extractor follows the same shape:

1. **Parse** — build the language's native AST (Roslyn for C#, the `ast`
   module for Python).
2. **Walk** — map every AST node to a graph node and every structural field
   (body, condition, parameter, base type, …) to a relationship.
3. **Control flow** — build a control-flow graph per function/method: basic
   blocks as nodes, `FOLLOWS` as branch/fallthrough edges.
4. **Emit** — print Cypher (default) or Souffle datalog (`--souffle`) on
   stdout, so it pipes straight into `cypher-shell` or `souffle -D-`.

## Languages

| Language | Parser / Library                          | Application            | Docs |
| -------- | ------------------------------------------ | ----------------------- | ---- |
| C#       | [Roslyn](https://github.com/dotnet/roslyn) (`Microsoft.CodeAnalysis.CSharp`) | `csharp/CSharpToCypher` | [csharp/README.md](csharp/README.md) |
| Python   | Python standard library [`ast`](https://docs.python.org/3/library/ast.html) + [`tokenize`](https://docs.python.org/3/library/tokenize.html) | `python/cypher_extractor` | [python/README.md](python/README.md) |

Each language's README has the full reference: requirements, CLI flags and
what they mean, node/relationship label tables, control-flow-graph details,
and Souffle datalog output.
