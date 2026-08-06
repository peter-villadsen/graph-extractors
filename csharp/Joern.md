# Analysing this codebase with Joern (Docker)

This document explains how to analyse the C# sources in this repository
(`CSharpToCypher`, `CSharpToCypher.Tests`, `samples`) with the
[Joern](https://joern.io) static-analysis platform, running entirely inside a
Docker container, and provides ready-to-run query examples.

## What Joern is, and why use it here

Joern is an open-source code-analysis platform built on the **Code Property
Graph (CPG)** — a single graph that combines an Abstract Syntax Tree, a
Control Flow Graph, a Program Dependence Graph, call-graph edges, and more.

Note how this differs from the `CSharpToCypher` tool in this repo:

| | `CSharpToCypher` (this repo) | Joern |
| -------------------------------- | --------------------------- | --------------------------- |
| Parser | Roslyn (`Microsoft.CodeAnalysis.CSharp`) | Roslyn via bundled `dotnetastgen` binary |
| Graph format | Cypher statements for **Neo4j** | Binary **CPG** (own storage, overlaydb/overflowdb) |
| Query language | Cypher | **CPGQL**, a Scala DSL in an interactive REPL |
| CFG/DDG built in | CFG only (plus Souffle datalog) | CFG, PDG, DDG, call graph, taint engine |

Joern ships a **C# frontend** (`csharpsrc2cpg`, language arg `CSHARPSRC`). We
use it to build a CPG over this repo's own C# source files and then query it.

## Requirements

- Docker Desktop with ~4–8 GB of RAM free and ~3 GB of disk for the image.
- A CPU with SSE4.2 support (any modern CPU). If you are inside a `kvm64`
  virtual machine, use the `joern-alma8` image variant instead — see
  [Troubleshooting](#troubleshooting).
- Nothing else: the image bundles a JDK, Joern and all frontends. No .NET or
  Java installation is needed on the host. The C# frontend's Roslyn-based
  `dotnetastgen` binary is self-contained and ships inside the image.

All commands below assume PowerShell on Windows; the only host-side step is a
single `docker run`.

## 1. Pull the image

```powershell
docker pull ghcr.io/joernio/joern
```

## 2. Start the Joern REPL with this repo mounted

From the repository root (`C:\Users\peter\source\repos\extractors\csharp`):

```powershell
docker run --rm -it -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern joern -J-Xmx8g
```

What each part does:

- `--rm -it` — remove the container on exit and run interactively.
- `-v "${PWD}:/app:rw"` — mount this repo (writeable) at `/app` inside the
  container. `$PWD` expands to the current directory in PowerShell.
- `-w /app` — start in the mounted directory.
- `joern` — the command to run in the container (the Joern REPL).
- `-J-Xmx8g` — give the Joern JVM 8 GB (raise it for bigger codebases).

You should be greeted by a `joern>` prompt. If you are, try
`cpg` — it is not imported yet, so start with the next step.

> If Docker Desktop asks, you must share your `C:` drive so the mount works.

## 3. Import the code into a CPG

At the `joern>` prompt:

```scala
importCode.csharp("/app", "csharp-extractors")
```

or let Joern auto-detect the language (it picks the frontend with the most
supported files — here that is C#):

```scala
importCode("/app", "csharp-extractors")
```

This runs the C# frontend in a separate JVM, builds the CPG, stores it in the
Joern workspace (under `workspace/csharp-extractors` inside the container),
and augments it with the default overlays (CFG, PDG, etc.). Joern's default
excludes skip build output (`bin/`, `obj/`, …) and VCS directories, so the
generated `AssemblyInfo.cs` files under `obj/` are not imported.

### Alternative: parse once, open many times

Importing in the REPL re-parses every time. If you want a reusable CPG file
that survives container restarts (it lives on the mounted volume), parse it
first, then load it:

```powershell
docker run --rm -it -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern `
  joern-parse /app --language CSHARPSRC --output /app/cpg.bin
docker run --rm -it -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern joern -J-Xmx8g
```

```scala
importCpg("/app/cpg.bin")
```

`--language CSHARPSRC` forces the C# frontend explicitly; omit it and
`joern-parse` auto-detects by file type.

## 4. First sanity checks

```scala
cpg.method.name.l
cpg.typeDecl.name.l
cpg.file.name.l
```

## 5. Example queries

All of the following are typed at the `joern>` prompt. `.l` evaluates and
prints the results as a list. Use TAB-completion on `cpg.` to discover steps.

### Files, types and methods

```scala
// Every file that was imported
cpg.file.name.l

// Every type declaration (class/interface/enum/struct)
cpg.typeDecl.name.l

// Every method, with its full name (namespace.type.method:returnType(...))
cpg.method.fullName.l

// Just the extractor's own classes
cpg.typeDecl.name(".*CSharpToCypher.*").name.l

// Where do the classes live? -> file per type
cpg.typeDecl.map(t => (t.name, t.filename)).l
```

### Calls and the call graph

```scala
// All distinct call targets used anywhere in the code
cpg.call.nameNot("<operator>.*").name.sorted.distinct.l

// Roslyn entry points used by the extractor
cpg.call.name("ParseText|CreateFromFile|GetSemanticModel").code.l

// Console output sites
cpg.call.name("WriteLine").code.l

// What does Program.Main call? (direct callees)
cpg.method.name("Main").call.callee.fullName.l

// Who calls AddNode / AddRel inside GraphBuilder?
cpg.call.name("AddNode|AddRel").method.fullName.l

// Calls to external (framework) methods only
cpg.call.nameNot("<operator>.*").callee.isExternal.name.sorted.distinct.l
```

### Control flow

```scala
// Basic-block (CFG) nodes of a method: label + source code
cpg.method.name("CollectFiles").cfgNode.map(n => (n.label, n.code)).l

// All control structures in the codebase, grouped by kind
cpg.controlStructure.controlStructureType.groupCount.toList

// Loops inside Dog.Fly() (sample.cs) — their conditions
cpg.method.name("Fly").controlStructure.where(_.controlStructureType("WHILE|FOR|DO")).code.l
```

### AST queries

```scala
// Every identifier used in Program.Main
cpg.method.name("Main").ast.isIdentifier.name.l

// Every literal (strings, numbers) used in GraphBuilder
cpg.method.name("GraphBuilder.*").ast.isLiteral.code.l

// All assignment statements
cpg.assignment.code.l

// Field accesses (e.g. ex.Message, Console.Error)
cpg.fieldAccess.code.l
```

### Data flow and taint (grounded in this code)

The classic question: *does attacker-controlled input reach a sensitive sink?*
Here the CLI argument `args` flows through `CollectFiles` into
`File.ReadAllText`, then into `CSharpSyntaxTree.ParseText`. Joern's
data-flow engine is interprocedural:

```scala
def source = cpg.method.name("Main").parameter.name("args")
def sink   = cpg.call.name("ReadAllText").argument

sink.reachableByFlows(source).p
```

`.p` prints the full interprocedural paths found; `.l` lists them. If the
result is empty, it usually means the C# frontend could not resolve a
collection/alias hop (see [C# frontend caveats](#c-frontend-caveats)), not
that there is no flow — narrow the query to a single method to check:

```scala
// Within CollectFiles, does the "paths" parameter reach EnumerateFiles?
cpg.method.name("CollectFiles").parameter.name("paths")
  .reachableBy(cpg.method.name("CollectFiles").call.name("EnumerateFiles|Exists").argument).l
```

More taint patterns:

```scala
// Anything reaching Console output
cpg.call.name("WriteLine").argument.reachableByFlows(cpg.method.parameter).p

// Methods that both read the filesystem and write to console (potential smell)
cpg.method.name("ReadAllText|WriteLine").map(_.method.fullName).toSet.l
```

### Metrics and navigation

```scala
// Longest methods in the codebase
cpg.method.map(m => (m.fullName, m.numberOfLines))
  .sortBy(_._2).reverse.take(5).l

// Methods with the most call sites
cpg.call.callee.filterNot(_.isExternal).map(_.name).groupCount.toList.sortBy(_._2).reverse.take(10)

// Parameters that are never used inside their method
cpg.method.parameter.nameNot(
  cpg.method.parameter.method.ast.isIdentifier.name.dedup.toSet.toSeq* ).l
```

(The last one is deliberately fiddly — a reminder that Joern queries are real
Scala; you can use `filter`, `map`, `groupCount`, `sortBy`, `toSet`, etc.)

### Exporting results

```scala
// Dump a full CPG (or a subset) for a given node's neighbourhood
cpg.method.name("Main").dotCfg
```

And, outside the REPL, from the container:

```powershell
docker run --rm -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern `
  joern-export /app/cpg.bin --repr=all --out=/app/export
```

## 6. Running queries non-interactively

Put queries in a script file, e.g. `analysis.sc` in the repo root:

```scala
importCpg("/app/cpg.bin")
cpg.method.name("Main").call.callee.fullName.sorted.l
cpg.typeDecl.name("CSharp.*").name.l
```

Then run it in one shot:

```powershell
docker run --rm -it -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern joern --script /app/analysis.sc
```

## 7. Persisting your work

The workspace (and any `cpg.bin` you wrote to `/app`) lives on the mounted
volume, so it survives between runs. Inside the REPL:

```scala
save    // persist the current CPG back to disk
close   // unload the current project
workspace  // list projects / reload them
```

## Troubleshooting

### JVM / out-of-memory

`importCode` spawns a second JVM for the frontend; memory usage roughly
doubles. Raise both: start the REPL with `joern -J-Xmx16g` and, for very large
imports, run the frontend directly:

```bash
/joern/joern-cli/frontends/csharpsrc2cpg/csharpsrc2cpg -J-Xmx16g /app --output /app/cpg.bin
```

then `importCpg("/app/cpg.bin")` in the REPL.

### "Illegal instruction" or instant crash on startup

Your CPU lacks SSE4.2 (common on `kvm64` VMs). Use the AlmaLinux-8 build:

```powershell
docker run --rm -it -v "${PWD}:/app:rw" -w /app ghcr.io/joernio/joern-alma8 joern
```

### Wrong or no frontend selected

Force C# explicitly with `importCode.csharp("/app", "...")` or
`joern-parse /app --language CSHARPSRC`.

### C# frontend caveats

- The frontend is under active development (funded/contributed via
  [joernio/joern](https://github.com/joernio/joern) + `joernio/DotNetAstGen`).
- `fieldAccess` nodes sometimes lack type info (polymorphism); to recover
  possible types use `fieldAccess.argument(1).evalType.baseTypeDeclTransitive`
  instead of relying on the stored type.
- Data flow across collections (`List`, `foreach`, aliasing) may be
  incomplete; a missing flow is frequently a frontend resolution gap, not a
  bug in your query.

### `dotnetastgen` errors

The C# frontend invokes a self-contained Roslyn-based binary. If it reports a
"failed to run" error, check the image arch matches your CPU, or pin the
version to match your host (`dotnetastgen_version` is configured in
`csharpsrc2cpg`'s `application.conf`).

### Re-importing after editing sources

`importCode` rebuilds from scratch, so edits to `.cs` files only take effect
after a re-import (`importCode.csharp("/app", "csharp-extractors")` again, or
delete the old workspace entry with `rmProject`/reparse).
