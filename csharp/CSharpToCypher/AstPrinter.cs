using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CSharpToCypher;

/// <summary>Prints a compact structural view of the Roslyn syntax tree.</summary>
public static class AstPrinter
{
    public static void Print(SyntaxNode root, TextWriter writer)
    {
        PrintNode(root, writer, 0);
    }

    private static void PrintNode(SyntaxNode node, TextWriter writer, int depth)
    {
        writer.WriteLine($"{new string(' ', depth * 2)}{Title(node)}");
        foreach (var child in node.ChildNodes())
        {
            PrintNode(child, writer, depth + 1);
        }
    }

    private static string Title(SyntaxNode node)
    {
        var kind = node.Kind().ToString();
        return node switch
        {
            BaseTypeDeclarationSyntax b => $"{kind} {b.Identifier.ValueText}",
            DelegateDeclarationSyntax d => $"{kind} {d.Identifier.ValueText}",
            MethodDeclarationSyntax m => $"{kind} {m.Identifier.ValueText}",
            LocalFunctionStatementSyntax lf => $"{kind} {lf.Identifier.ValueText}",
            PropertyDeclarationSyntax p => $"{kind} {p.Identifier.ValueText}",
            VariableDeclaratorSyntax v => $"{kind} {v.Identifier.ValueText}",
            ParameterSyntax p2 => $"{kind} {p2.Identifier.ValueText}",
            ForEachStatementSyntax fe => $"{kind} {fe.Identifier.ValueText}",
            LabeledStatementSyntax lb => $"{kind} {lb.Identifier.ValueText}",
            _ => kind,
        };
    }
}
