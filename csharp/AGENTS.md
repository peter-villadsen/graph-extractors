# Guidance for C# code generation

This file is read before generating or modifying C# code in this repository.

## Statement formatting

- All constituent statements must always be enclosed in braces. This applies
  everywhere a statement is optional or embedded:

  - the body of `if` / `else` / `else if` clauses;
  - the bodies of `while`, `do` / `while`, `for`, and `foreach` loops;
  - `try` / `catch` / `finally` blocks;
  - `using`, `lock`, `switch`, and similar block-bearing statements;
  - the body of a lambda or local function.

  Never omit braces, even when a clause contains a single statement.

  Example — required:

  ```csharp
  if (condition)
  {
      DoSomething();
  }

  while (running)
  {
      Tick();
  }
  ```

  Incorrect — braces omitted:

  ```csharp
  if (condition)
      DoSomething();   // wrong: braces required

  while (running)
      Tick();          // wrong: braces required
  ```
