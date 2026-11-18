# Python → Cypher extractor (`ast` module)

A command-line application that parses Python source code, builds its
standard-library AST, and emits a graph representation of the code as
**Cypher** statements suitable for import into [Neo4j](https://neo4j.com/).

| Parser / Library | Application | Node kinds come from |
| ----------------------------------------- | ---------------------- | ----------------------------------------------- |
| Python standard library [`ast`](https://docs.python.org/3/library/ast.html) + [`tokenize`](https://docs.python.org/3/library/tokenize.html) | `python/cypher_extractor` | The official Python grammar (reference/grammar.html) |

## CLI contract

* positional `PATH` arguments — files **or** directories (scanned recursively);
* `--verbose` / `-v` — print progress information to **stderr**;
* `--ast` / `-a` — print the AST before generating Cypher, to **stderr**;
* `--cfg` — additionally emit control-flow-graph basic blocks, see
  [Control flow graphs](#control-flow-graphs) below;
* `--souffle` — emit a **Souffle datalog** program for the control-flow graphs
  on **stdout** instead of Cypher, see [Souffle datalog](#souffle-datalog)
  below;
* `--format cypher|csv` — output format, see [Output formats](#output-formats)
  below (default `cypher`);
* `--output-dir DIR` — directory for the CSV files (`--format csv` only,
  default `neo4j-import`);
* `--database NAME` — database name for the generated import command
  (`--format csv` only, default `neo4j`);
* `--help` / `-h` — usage help.

**stdout carries only one format** so it can be piped straight into a consumer
(`cypher-shell` for Cypher, `souffle -D-` for datalog):

```bash
python -m cypher_extractor -v --ast python/samples/sample.py | cypher-shell
```

---

## Quick example

Feed a small Python function through the extractor:

```python
# greet.py
def greet(name: str) -> str:
    if not name:
        return "Hello!"
    return "Hello " + name
```

```bash
python -m cypher_extractor --cfg greet.py   # any file or directory works
```

Every construct becomes a node with coordinates, every containment a
relationship (trimmed here for brevity):

```cypher
CREATE (p1:PythonModule {file: 'greet.py', name: 'greet.py', qualname: 'greet', id: 'p1'});
CREATE (p2:PythonFunctionDefinition:PythonStatement {name: 'greet', isAsync: false, qualname: 'greet.greet', startLine: 1, startColumn: 0, endLine: 4, endColumn: 26, id: 'p2'});
CREATE (p6:PythonIfStatement:PythonStatement {startLine: 2, startColumn: 4, endLine: 3, endColumn: 23, id: 'p6'});
CREATE (p9:PythonReturnStatement:PythonStatement {startLine: 3, startColumn: 8, endLine: 3, endColumn: 23, id: 'p9'});
MATCH (a {id: 'p2'}), (b {id: 'p6'}) CREATE (a)-[:CONTAINS]->(b);
MATCH (a {id: 'p6'}), (b {id: 'p7'}) CREATE (a)-[:HAS_CONDITION]->(b);
MATCH (a {id: 'p6'}), (b {id: 'p9'}) CREATE (a)-[:CONTAINS]->(b);
```

Relationship statements look their endpoints up by the `id` property rather
than reusing the `CREATE`d variable name, because each statement is its own
autocommitted query when piped through `cypher-shell` — variables bound in
one `CREATE` are not visible in the next one.

With `--cfg` you also get the control-flow graph; a branch edge carries the
condition that selects it (the false branch is the negated condition —
`not x < 10` simplifies to `x >= 10` for a comparison, or just drops a
leading `not`; see [Control flow graphs](#control-flow-graphs)):

```cypher
CREATE (p17:PythonBasicBlock {scope: 'greet.greet', ordinal: 0, blockId: 0, isEntry: true, kind: 'If', startLine: 2, endLine: 3, id: 'p17'});
MATCH (a {id: 'p17'}), (b {id: 'p19'}) CREATE (a)-[:FOLLOWS {condition: 'not name'}]->(b);
MATCH (a {id: 'p17'}), (b {id: 'p20'}) CREATE (a)-[:FOLLOWS {condition: 'name'}]->(b);
MATCH (a {id: 'p17'}), (b {id: 'p6'}) CREATE (a)-[:CONTAINS {ordinal: 0}]->(b);
```

Import it into Neo4j, then query the graph:

```bash
python -m cypher_extractor --cfg greet.py | cypher-shell
```

```cypher
-- which functions return early out of an if?
MATCH (f:PythonFunctionDefinition)-[:CONTAINS]->(i:PythonIfStatement)
      -[:CONTAINS]->(:PythonReturnStatement)
RETURN f.name AS function, i.startLine AS line;

-- every basic block of the greet function and what it executes
MATCH (b:PythonBasicBlock {scope: 'greet.greet'})-[:CONTAINS]->(s:PythonStatement)
RETURN b.blockId, labels(s) AS statement, s.startLine AS line
ORDER BY b.blockId;
```

---

## Output formats

`--format cypher` (default) streams `CREATE`/`MATCH` statements to stdout, as
above — each one is its own autocommitted transaction, which doesn't scale to
a large codebase (many small transactions, or one huge one if you batch it
yourself).

`--format csv` instead writes
[`neo4j-admin database import`](https://neo4j.com/docs/operations-manual/current/import/)
CSV files under `--output-dir`, and prints the ready-to-run import command on
stdout instead of graph data:

```bash
python -m cypher_extractor --format csv --output-dir out python/samples/sample.py
# neo4j-admin database import full --multiline-fields=true --nodes=out/nodes/PythonClassDefinition_PythonStatement.csv --nodes=... --relationships=CONTAINS=out/relationships/CONTAINS.csv --relationships=... neo4j

# stop the target dbms first, then run the printed command (an example):
neo4j-admin database import full --multiline-fields=true --nodes=out/nodes/PythonClassDefinition_PythonStatement.csv --relationships=CONTAINS=out/relationships/CONTAINS.csv neo4j
```

One CSV file is written per **node label set** (`out/nodes/`) and per
**relationship type** (`out/relationships/`), since each has its own property
schema; node files carry a `:ID`/`:LABEL` header, relationship files a
`:START_ID`/`:END_ID` header, and property columns are typed
(`prop:long`/`:double`/`:boolean`/`:string[]`, or untyped for `string`,
inferred from the first non-null value seen for that column). This is a bulk
loader for an **empty** database, not an incremental import — unlike Cypher
mode, it can't add to an existing graph.

Both formats are implemented against the same writer interface
(`cypher_extractor/writers.py`): `CypherStreamWriter` prints each file's
statements as they're built, while `Neo4jAdminImportWriter` buffers
nodes/relationships across the whole run (needed to group same-schema rows
into one file each) and writes everything out in `finish()`.

---

## Requirements

* Python 3.10+ (3.12 recommended). Only the standard library is used —
  including the `--cfg` control-flow pass, which this extractor computes
  itself (see [Control flow graphs](#control-flow-graphs)).

## Run

```bash
python -m cypher_extractor --verbose --ast python/samples/sample.py
python -m cypher_extractor python/samples
```

Start with the bundled `python/samples/sample.py` (it exercises classes,
inheritance, async functions, `try`/`except`, comprehensions, walrus and
`match`). Point `PATH` at a directory to process every `.py` file in it.

## How it works

`ast.parse(source)` produces an `ast.AST` tree whose node classes mirror the
official Python grammar (`classdef`, `funcdef`, `while_stmt`, `if_stmt`,
`for_stmt`, `expr_stmt`, `parameters`, …). `GraphBuilder` walks the tree
generically via each node's `_fields`, mapping classes to labels and fields to
edges. Comments are recovered with `tokenize` and attached to the nearest
preceding statement as trivia.

Node ids are `p1`, `p2`, … Every statement carries `PythonStatement` and every
expression carries `PythonExpression` as an extra label for querying.

**Qualified names**: `PythonModule`, `PythonClassDefinition` and
`PythonFunctionDefinition` (including async) nodes carry a package-qualified
`qualname`, unique across the run, next to their plain `name`:

| Node | `qualname` |
| ---- | ---------- |
| module | the dotted module name, e.g. `pkg.sub.mod` |
| class / function | the enclosing definition's `qualname` + `.` + its name, e.g. `pkg.sub.mod.Class.method`, `pkg.sub.mod.Class.method.inner` |

Nesting goes through classes and functions only (no `<locals>`): a `def`
inside an `if` or `for` is named after the nearest enclosing definition.

The module name comes from the `PATH` argument the file was found under:

* under a directory argument, it is the path relative to that directory,
  without `.py` and with separators turned into dots, so running on `src`
  names `src/pkg/sub/mod.py` `pkg.sub.mod`, and two `utils.py` files in
  `src/x` and `src/y` become `x.utils` and `y.utils`;
* a package's `__init__.py` is named after the package (`src/pkg/__init__.py`
  → `pkg`);
* a file passed directly is named by its stem (`python -m cypher_extractor
  src/pkg/tool.py` → `tool`), so pass the source root as the argument to get
  package-qualified names.

CFG `scope`s (below) use exactly the same names, so a function's blocks are
`MATCH (f:PythonFunctionDefinition), (b:PythonBasicBlock) WHERE b.scope = f.qualname`.

**Order**: statements in a body are chained with `FOLLOWS` in source order.
Wherever else order is semantically meaningful — binary/comparison operand
order, call/decorator arguments, parameter lists (in calling-convention
order: positional-only, positional, `*args`, keyword-only, `**kwargs`),
`except`/`match`-case order (first-match-wins), and which statement runs
first inside one CFG basic block — the relationship carries an integer
`ordinal` property instead: `MATCH (n)-[r:HAS_ARGUMENT]->(a) RETURN a ORDER
BY r.ordinal`. This applies even when a single edge type comes from more
than one AST field, e.g. `BinOp`'s separate `left`/`right` fields (both
`HAS_OPERAND`) still get ordinals `0`/`1` in source order.

## Node labels (a selection)

| Label | Grammar production / ast class |
| ----- | ------------------------------- |
| `PythonModule` | `file_input` (the module) |
| `PythonClassDefinition` | `classdef` |
| `PythonFunctionDefinition`, `PythonAsyncFunctionDefinition` | `funcdef`, `async_funcdef` |
| `PythonParameter` | `parameters` / `typedargslist` / `arg` |
| `PythonType` | type annotations (`annotation`, `returns`, class `bases`) |
| `PythonWhileStatement`, `PythonIfStatement`, `PythonForStatement`, `PythonAsyncForStatement`, `PythonReturnStatement`, `PythonExpressionStatement`, `PythonAssignmentStatement`, `PythonAugmentedAssignmentStatement`, `PythonAnnotatedAssignmentStatement`, `PythonWithStatement`, `PythonTryStatement`, `PythonRaiseStatement`, `PythonAssertStatement`, `PythonImportStatement`, `PythonImportFromStatement`, `PythonMatchStatement`, `PythonPassStatement`, `PythonBreakStatement`, `PythonContinueStatement`, `PythonDeleteStatement`, `PythonGlobalStatement`, `PythonNonlocalStatement` | `while_stmt`, `if_stmt`, `for_stmt`, `return_stmt`, `expr_stmt`, … |
| `PythonDocString` | a docstring (first string expression of a module/class/function body) |
| `PythonExceptHandler` | `except_clause` |
| `PythonCall`, `PythonBinaryOperation`, `PythonBooleanOperation`, `PythonComparison`, `PythonName`, `PythonConstant`, `PythonAttribute`, `PythonSubscript`, `PythonLambda`, `PythonConditionalExpression`, `PythonListComprehension`, `PythonDictComprehension`, `PythonGeneratorExpression`, `PythonJoinedString`, … | the expression classes |
| `PythonCommentTrivia` | `#` comments |

## Relationships (a selection)

```cypher
(:PythonClassDefinition)-[:DERIVED_FROM]->(:PythonType)     -- bases
(:PythonClassDefinition)-[:DECLARES]->(:PythonFunctionDefinition)
(:PythonClassDefinition)-[:CONTAINS]->(:PythonStatement)    -- suite
(:PythonFunctionDefinition)-[:HAS_PARAMETER]->(:PythonParameter)
(:PythonParameter)-[:OF_TYPE]->(:PythonType)                -- annotation
(:PythonFunctionDefinition)-[:RETURNS]->(:PythonType)       -- return annotation
(:PythonWhileStatement)-[:HAS_CONDITION]->(:PythonExpression) -- test
(:PythonWhileStatement)-[:CONTAINS]->(:PythonStatement)       -- body (suite)
(:PythonWhileStatement)-[:HAS_ELSE]->(:PythonStatement)       -- orelse
(:PythonIfStatement)-[:HAS_CONDITION]->(:PythonExpression)
(:PythonForStatement)-[:HAS_TARGET]->(:PythonExpression)
(:PythonForStatement)-[:HAS_ITERABLE]->(:PythonExpression)
(:PythonTryStatement)-[:HAS_HANDLER]->(:PythonExceptHandler)
(:PythonStatement)-[:HAS_TRIVIA]->(:PythonCommentTrivia)
(caller)-[:CALLS {count}]->(:PythonFunctionDefinition)       -- best effort, see below
```

### `CALLS`

**Never guess.** Python is dynamic, so a call is tied to a definition only
when the code leaves no doubt. A missing edge is acceptable; a wrong edge is
not. Everything not listed below gets no edge.

The **caller** is the innermost enclosing function (`def` / `async def`).
Top-level code has the `PythonModule` node as caller, and code in a class
body outside any method has its `PythonClassDefinition`. Calls in a `def`'s
decorators or default values run in the enclosing scope and are attributed
to it. One `CALLS` edge is emitted per (caller, callee) pair, with `count` =
the number of call sites. Calls are resolved after every file is built, so
they connect across files.

| Call form | Resolves to |
| --- | --- |
| `f()` where `f` is a `def` at module level in the same module, not shadowed in the calling function | that function |
| `f()` where `f` is a nested `def` in the calling or an enclosing function | that nested function |
| `self.m()` / `cls.m()` in a method of class `C` (through its first parameter), where `C`'s own body defines `m` | `C.m` |
| `C.m()` where `C` is a class defined in the same module and its body defines `m` | `C.m` |
| `C()` where `C` is a class in the same module whose body defines `__init__` | `C.__init__` |
| `f()` after `from pkg.mod import f` (or `… import f as g` → `g()`), where `pkg.mod` is in the same run and defines `f` at module level | that function |
| `mod.f()` after `import pkg.mod as mod`, or `pkg.mod.f()` after `import pkg.mod`, same conditions | that function |

Relative imports (`from .mod import f`) resolve against the importing
module's package (the module itself for an `__init__.py`). Module names are
the `qualname`s described above, so pass the source root as `PATH` for
imports between files to resolve. An imported class works like a local one
(`C()`, `C.m()`).

A name resolves only when its scope binds it **exactly once**, by a `def`,
`class` or `import` placed directly in that scope's body, and only when the
call comes after that binding in the same scope. Name lookup follows
Python's rules: local, then enclosing functions (class bodies are only
visible to their own code), then the module, honouring `global` and
`nonlocal`.

**Not resolved** (no edge), explicitly:

* builtins (`print()`, `len()`, `getattr(...)`) and anything from a module
  outside the run (the standard library, third-party packages);
* calls on parameters or other unknown objects (`obj.m()`), and calls
  through variables, containers or call results (`table['f']()`,
  `make()()`);
* inherited methods: `self.m()` where `m` is only defined on a base class;
* names that are conditional (a `def` or `import` inside `if`/`try`/…),
  redefined, rebound by assignment, a parameter, a loop target, or rebound
  through `global`/`nonlocal` from another function;
* decorated targets: functions, methods and classes with any decorator
  other than the builtin `staticmethod` / `classmethod`, and classes with
  keywords such as `metaclass=`;
* star imports: a module with `from x import *` resolves no module-level
  names;
* a class that defines `__new__`, or a constructor call on a class without
  its own `__init__`;
* a method (including `__init__`) whose name is assigned as an attribute
  anywhere in the run (`obj.m = ...`, `setattr(obj, 'm', ...)`), and every
  method after a `setattr` with a computed name.

Over-approximation only ever drops edges. For example, names bound inside a
lambda or comprehension count as bindings of the enclosing function, and two
files with the same module name make imports of that name unresolvable.
Monkey-patching that is not a direct attribute assignment or `setattr`
(through `__dict__`, say) is not detected.

```cypher
// who calls compute(), and how often
MATCH (caller)-[c:CALLS]->(f:PythonFunctionDefinition {qualname: 'sample.compute'})
RETURN caller.qualname, c.count
```

---

## Control flow graphs

The extractor models the **control flow graph** of the module and every
function/method/class body, opt-in via `--cfg`, computed entirely with the
standard library — no third-party dependency. (Earlier versions delegated
this to the third-party [`py2cfg`](https://pypi.org/project/py2cfg/) package;
it was dropped because it's unmaintained and has no support at all for
Python's `match` statement, silently collapsing one to a single block. What
this extractor actually needs from a CFG — block partitioning,
branch-condition edges, one entry/exit per scope — turned out to be a small
enough surface that a purpose-built builder was simpler than continuing to
carry that dependency risk.)

Every scope gets exactly one entry block and one exit block (mirroring how
the C# extractor's Roslyn-based CFG always has a single Entry/Exit), with
further blocks created only at actual branch points: `if`/`elif`/`else`,
`while`/`for` (+ `else`, `break`, `continue`), `match`/`case` (**one block per
case**, unlike the old dependency), and `try`/`except`/`else`/`finally`.
Nested `def`/`class` bodies get their own independent CFG, scoped by dotted
qualname (`module.Class.method`), the same `qualname` the definition node
carries.

Each basic block becomes a `PythonBasicBlock` node with `ordinal` (position
within its scope), `isEntry`/`isFinal`/`isReachable`, `scope`, `kind` (the
class name of its first statement), and source line span. Successor edges
are `FOLLOWS` relationships; a real boolean branch (`if`/`while`, a `match`
pattern test) carries a `condition` property with the text that selects it.
Every block links to the AST statement nodes it executes via `CONTAINS`
(with an `ordinal` for execution order within the block — see
[Order](#how-it-works) above).

```cypher
(:PythonBasicBlock)-[:FOLLOWS {condition: 'x < 10'}]->(:PythonBasicBlock)
(:PythonBasicBlock)-[:CONTAINS {ordinal: 0}]->(:PythonStatement)
```

**Negation**: the false branch's `condition` is a genuine negation of the
test, simplified where cheap — a comparison flips its operator (`x < 10`
negates to `x >= 10`), `not x` drops the `not` (double-negation
elimination) — and falls back to `not (...)` wrapping the whole test
otherwise (chained comparisons, `and`/`or`, calls, ...).

**`match`**: each `case` gets its own block, reached by a `condition` edge
carrying the pattern text (plus ` if <guard>` when present, e.g.
`"int() as i if i > 0"`) chained like an `elif` ladder; a case whose pattern
is an unconditional capture/wildcard (`case _:`, `case x:`, no guard) is
treated as exhaustive, so there's no "didn't match" edge out of it. If no
case is exhaustive, `match` can fall through having matched nothing, exactly
like Python does at runtime — the CFG's "after" block is reachable from the
last case's failing test too.

**Known limitations** (both about `try`/`except`/`finally`, and both because
this extractor works at statement/block granularity rather than modeling
exactly which statement could raise, matching how the C# side's Roslyn-based
CFG also doesn't add edges for implicit exceptions):

* Every statement in a `try` body is treated as equally able to raise, so a
  handler is reached by a `condition` edge (naming the exception type, or no
  condition for a bare `except:`) from the block containing the `try`
  itself, not from the specific statement that actually raised.
* `finally` always runs in real Python, including right before an early
  `return`/`raise`/`break`/`continue` inside the `try` body or a handler —
  that early-exit path is not routed through `finally` in the CFG, only the
  normal-completion path is. The `finally` body is still fully present in
  the graph (via `HAS_FINALLY` on the `Try` statement) either way; only the
  CFG edge for that specific early-exit timing is the gap.

`for` loops don't carry a `condition` on their branch edges — unlike `if`/
`while`, there's no boolean test in the source to show (the branch is driven
by the iterator, not an expression), so showing one would mean fabricating
text that isn't really there. `with`/`async with` don't branch at all, so
they don't start a new block; the statement and its body share whatever
block precedes them.

## Souffle datalog

Pass `--souffle` to emit a [Souffle](https://souffle-lang.github.io/) datalog
program on **stdout** instead of Cypher. It models the control-flow graph:
one fact per basic block and successor edge, scoped by the enclosing
function/method, plus a couple of standard analysis rules.

```bash
python -m cypher_extractor --souffle greet.py | souffle -D-
```

The program defines three input relations and two derived relations:

| Relation | Tuple |
| -------- | ----- |
| `basic_block(id, function, ordinal, isEntry, isFinal)` | one row per basic block; ids are run-global `b1`, `b2`, … so all input files share one program |
| `control_flow_edge(source, target, function, condition)` | one row per `FOLLOWS` edge; `condition` carries the branch condition |
| `block_statement(block, statement, statement_kind, function)` | links a block to each AST statement it executes |
| `reachable(id, function)` | blocks reachable from the function's entry block |
| `path(source, target, function)` | transitive closure of `control_flow_edge` |

Facts and rules are emitted inline, and `.output reachable` / `.output path`
print the two derived relations when the program runs.

```datalog
basic_block("b1", "greet.greet", 0, 1, 0).
control_flow_edge("b1", "b3", "greet.greet", "not name").
block_statement("b1", "s1", "PythonIfStatement", "greet.greet").

reachable(Id, Function) :- basic_block(Id, Function, _, 1, _).
reachable(Target, Function) :- reachable(Source, Function),
                               control_flow_edge(Source, Target, Function, _).
```

Python scopes use the definitions' `qualname`: `module.function` /
`module.Class.method`.

---

## Notes

* **Idempotency**: the tool emits plain `CREATE` statements with unique ids
  (`p1`, `p2`, …) for the whole run, even across multiple input files.
  Re-importing duplicates the graph; clear the database first or use
  `MATCH (n) DETACH DELETE n` between runs.
* **Performance**: relationship statements `MATCH` both endpoints by their
  `id` property before `CREATE`ing the edge (ids aren't visible across
  separate `cypher-shell` statements otherwise). For large codebases, create
  an index per node label on `id` first (e.g.
  `CREATE INDEX FOR (n:PythonStatement) ON (n.id);`) so those lookups aren't
  full label scans.
* **Trivia**: comments are attached to the statement that begins on or before
  their line.
* **Expressions** are deliberately shallow "for now": operators are stored as
  properties and operands linked with `HAS_OPERAND`; sub-expression detail can
  be deepened later without changing the overall shape.
* **Unparseable files** (e.g. a `.py` with syntax newer than the interpreter)
  are reported on stderr and skipped; processing continues with the next file.
* **Troubleshooting**: re-running an import duplicates the graph — clear it
  first with `cypher-shell "MATCH (n) DETACH DELETE n;"` (or
  `cypher-shell --database neo4j "MATCH (n) DETACH DELETE n;"` if you target a
  named database). If `cypher-shell` isn't on `PATH`, use the browser at
  `http://localhost:7474` and paste the statements into the query bar instead.
