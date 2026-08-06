using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CSharpToCypher;

internal static class Program
{
    private static int Main(string[] args)
    {
        var verbose = false;
        var printAst = false;
        var emitSouffle = false;
        var paths = new List<string>();

        foreach (var arg in args)
        {
            switch (arg)
            {
                case "--verbose" or "-v": verbose = true; break;
                case "--ast" or "-a": printAst = true; break;
                case "--souffle": emitSouffle = true; break;
                case "--help" or "-h":
                    PrintHelp();
                    return 0;
                default:
                    if (arg.StartsWith("-"))
                    {
                        Console.Error.WriteLine($"error: unknown option: {arg}");
                        return 2;
                    }
                    paths.Add(arg);
                    break;
            }
        }

        if (paths.Count == 0)
        {
            PrintHelp();
            return 2;
        }

        var files = CollectFiles(paths, ".cs");
        if (files.Count == 0)
        {
            Console.Error.WriteLine("error: no C# source files found");
            return 2;
        }

        var trees = new List<(string File, CSharpSyntaxTree Tree)>();
        foreach (var file in files)
        {
            if (verbose)
            {
                Console.Error.WriteLine($"[verbose] reading {file}");
            }

            string source;
            try
            {
                source = File.ReadAllText(file);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"error: cannot read {file}: {ex.Message}");
                continue;
            }

            var tree = (CSharpSyntaxTree)CSharpSyntaxTree.ParseText(source, path: file);
            if (verbose)
            {
                Console.Error.WriteLine($"[verbose] parsed {file}");
            }

            if (printAst)
            {
                Console.Error.WriteLine($"[ast] {file}");
                AstPrinter.Print(tree.GetRoot(), Console.Error);
            }

            trees.Add((file, tree));
        }

        if (trees.Count == 0)
        {
            return 2;
        }

        // One compilation for all files so symbols resolve across the run.
        var compilation = CSharpCompilation.Create("Extraction", trees.Select(t => t.Tree), CoreReferences());

        var souffle = new SouffleWriter();
        foreach (var (file, tree) in trees)
        {
            var root = (CompilationUnitSyntax)tree.GetRoot();
            var model = compilation.GetSemanticModel(tree);
            var builder = new GraphBuilder(file, model);
            var document = builder.Build(root);

            if (verbose)
            {
                Console.Error.WriteLine($"[verbose] {file}: {document.Nodes.Count} nodes, {document.Relationships.Count} relationships");
            }

            if (emitSouffle)
            {
                souffle.AddCfg(document);
                continue;
            }

            foreach (var node in document.Nodes)
            {
                Console.WriteLine(node.ToCypher());
            }
            foreach (var relationship in document.Relationships)
            {
                Console.WriteLine(relationship.ToCypher());
            }
        }

        if (emitSouffle)
        {
            Console.Write(souffle.Render());
        }

        return 0;
    }

    /// <summary>Metadata references for the whole shared framework.</summary>
    private static IEnumerable<MetadataReference> CoreReferences()
    {
        var trusted = (string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES");
        if (trusted is null) yield break;
        foreach (var path in trusted.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            {
                continue;
            }
            yield return MetadataReference.CreateFromFile(path);
        }
    }

    private static List<string> CollectFiles(IEnumerable<string> paths, string extension)
    {
        var files = new List<string>();
        foreach (var path in paths)
        {
            if (Directory.Exists(path))
            {
                files.AddRange(Directory.EnumerateFiles(path, "*" + extension, SearchOption.AllDirectories));
            }
            else if (File.Exists(path))
            {
                files.Add(path);
            }
            else
            {
                Console.Error.WriteLine($"warning: no such file or directory: {path}");
            }
        }
        return files.Distinct().OrderBy(f => f, StringComparer.Ordinal).ToList();
    }

    private static void PrintHelp()
    {
        Console.Error.WriteLine(
            """
            Usage: CSharpToCypher [--verbose] [--ast] [--souffle] <path> [<path> ...]

              Parse C# source files (or directories, searched recursively) with
              Roslyn and emit a Cypher graph (Neo4j) representation on stdout.

              --verbose, -v   print progress information to stderr
              --ast, -a       print the parsed syntax tree to stderr first
              --souffle       emit a Souffle datalog program for the control-flow
                              graphs on stdout instead of Cypher
              --help, -h      show this help
            """);
    }
}
