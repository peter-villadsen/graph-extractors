# Source Code → Neo4j Cypher Extractors

Command-line applications that parse source code, build the language's AST,
and emit a graph representation of the code as **Cypher** statements suitable
for import into [Neo4j](https://neo4j.com/).

| Language | Parser / Library                          | Application            | Node kinds come from                            | Docs |
| -------- | ----------------------------------------- | ---------------------- | ----------------------------------------------- | ---- |
| C#       | [Roslyn](https://github.com/dotnet/roslyn) (`Microsoft.CodeAnalysis.CSharp`) | `csharp/CSharpToCypher` | The C# language specification (ECMA-334) and Roslyn `SyntaxKind` | [csharp/README.md](csharp/README.md) |
| Python   | Python standard library [`ast`](https://docs.python.org/3/library/ast.html) + [`tokenize`](https://docs.python.org/3/library/tokenize.html) | `python/cypher_extractor` | The official Python grammar (reference/grammar.html) | [python/README.md](python/README.md) |

Each extractor has its own README with requirements, CLI usage, node/
relationship labels, control-flow-graph details, and Souffle datalog output.
This file only covers what's shared between them.

## Shared CLI contract

* positional `PATH` arguments — files **or** directories (scanned recursively);
* `--verbose` / `-v` — print progress information to **stderr**;
* `--ast` / `-a` — print the AST before generating Cypher, to **stderr**;
* `--souffle` — emit a **Souffle datalog** program for the control-flow graphs
  on **stdout** instead of Cypher (see each project's README);
* `--help` / `-h` — usage help.

The Python extractor additionally has `--cfg` to opt in to control-flow-graph
basic blocks; the C# extractor emits its CFG unconditionally.

**stdout carries only one format** so it can be piped straight into a consumer
(`cypher-shell` for Cypher, `souffle -D-` for datalog):

```bash
python -m cypher_extractor -v --ast python/samples/sample.py | cypher-shell
dotnet run --project csharp/CSharpToCypher -- csharp/samples/sample.cs | cypher-shell
```

## Shared notes

* **Idempotency**: the tools emit plain `CREATE` statements with unique,
  per-run ids. Re-importing duplicates the graph; clear the database first or
  use `MATCH (n) DETACH DELETE n` between runs.
* **Troubleshooting**: re-running an import duplicates the graph — clear it
  first with `cypher-shell "MATCH (n) DETACH DELETE n;"` (or
  `cypher-shell --database neo4j "MATCH (n) DETACH DELETE n;"` if you target a
  named database). If `cypher-shell` isn't on `PATH`, use the browser at
  `http://localhost:7474` and paste the statements into the query bar instead.

See [csharp/README.md](csharp/README.md) and [python/README.md](python/README.md)
for everything else: setup, node/relationship labels, control-flow graphs,
Souffle datalog, and per-language known limitations.
