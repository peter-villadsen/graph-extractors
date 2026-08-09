using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Xunit;

namespace CSharpToCypher.Tests;

public class CypherFormattingTests
{
    [Fact]
    public void EscapeString_escapes_quotes_backslashes_and_control_chars()
    {
        Assert.Equal("a\\'b\\\\c\\n\\t", Cypher.EscapeString("a'b\\c\n\t"));
    }

    [Fact]
    public void FormatValue_renders_primitives()
    {
        Assert.Equal("true", Cypher.FormatValue(true));
        Assert.Equal("42", Cypher.FormatValue(42));
        Assert.Equal("'hi'", Cypher.FormatValue("hi"));
        Assert.Equal("['a', 1]", Cypher.FormatValue(new object?[] { "a", 1 }));
        Assert.Equal("null", Cypher.FormatValue(null));
    }
}

public class GraphBuilderTests
{
    private static CypherDocument Build(string source, string file = "test.cs")
    {
        var root = (CompilationUnitSyntax)CSharpSyntaxTree.ParseText(source).GetRoot();
        return new GraphBuilder(file).Build(root);
    }

    private static CypherDocument BuildWithModel(string source, string file = "test.cs")
    {
        var tree = (CSharpSyntaxTree)CSharpSyntaxTree.ParseText(source, path: file);
        var references = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES"))
            ?.Split(Path.PathSeparator)
            .Where(p => !string.IsNullOrEmpty(p) && File.Exists(p))
            .Select(p => (MetadataReference)MetadataReference.CreateFromFile(p))
            .ToArray() ?? Array.Empty<MetadataReference>();
        var compilation = CSharpCompilation.Create("Test", new[] { tree }, references);
        var model = compilation.GetSemanticModel(tree);
        return new GraphBuilder(file, model).Build((CompilationUnitSyntax)tree.GetRoot());
    }

    [Fact]
    public void CompilationUnit_node_emitted_first()
    {
        var doc = Build("class A { }");
        var root = doc.Nodes.First();
        Assert.Contains("CSharpCompilationUnit", root.Labels);
        Assert.Equal("test.cs", root.Properties["name"]);
    }

    [Fact]
    public void Class_derives_and_declares_method()
    {
        var doc = Build("class Dog : Animal { void Bark() { } }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration"));
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "DERIVED_FROM");
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "DECLARES");
    }

    [Fact]
    public void Class_with_interface_only_base_list_does_not_derive()
    {
        var doc = BuildWithModel("interface IDisposable2 { } class Logger : IDisposable2 { }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration") && n.Properties["name"] as string == "Logger");
        Assert.DoesNotContain(doc.Relationships, r => r.Source == cls.Id && r.Type == "DERIVED_FROM");
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "IMPLEMENTS");
    }

    [Fact]
    public void Class_with_real_base_class_still_derives()
    {
        var doc = BuildWithModel("class Animal { } class Dog : Animal { }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration") && n.Properties["name"] as string == "Dog");
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "DERIVED_FROM");
    }

    [Fact]
    public void Class_base_list_without_semantic_model_falls_back_to_positional_heuristic()
    {
        var doc = Build("class Dog : Animal { }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration"));
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "DERIVED_FROM");
    }

    [Fact]
    public void Interface_base_list_never_derives()
    {
        var doc = BuildWithModel("interface IBar { } interface IFoo : IBar { }");
        var iface = doc.Nodes.First(n => n.Labels.Contains("CSharpInterfaceDeclaration") && n.Properties["name"] as string == "IFoo");
        Assert.DoesNotContain(doc.Relationships, r => r.Source == iface.Id && r.Type == "DERIVED_FROM");
        Assert.Contains(doc.Relationships, r => r.Source == iface.Id && r.Type == "IMPLEMENTS");
    }

    [Fact]
    public void Struct_base_list_never_derives()
    {
        var doc = BuildWithModel("interface IFoo { } struct S : IFoo { }");
        var s = doc.Nodes.First(n => n.Labels.Contains("CSharpStructDeclaration"));
        Assert.DoesNotContain(doc.Relationships, r => r.Source == s.Id && r.Type == "DERIVED_FROM");
        Assert.Contains(doc.Relationships, r => r.Source == s.Id && r.Type == "IMPLEMENTS");
    }

    [Fact]
    public void Method_has_parameter_return_and_body()
    {
        var doc = Build("class C { int Add(int a, int b) { return a + b; } }");
        var method = doc.Nodes.First(n => n.Labels.Contains("CSharpMethodDeclaration"));
        Assert.Equal("Add", method.Properties["name"]);

        var types = doc.Relationships.Where(r => r.Source == method.Id).Select(r => r.Type);
        Assert.Contains("HAS_PARAMETER", types);
        Assert.Contains("RETURNS", types);
        Assert.Contains("HAS_BODY", types);

        var paramsCount = doc.Relationships
            .Where(r => r.Type == "HAS_PARAMETER" && r.Source == method.Id)
            .Count();
        Assert.Equal(2, paramsCount);
    }

    [Fact]
    public void While_statement_has_condition_and_body()
    {
        var doc = Build("class C { void M() { while (x < 10) { x++; } } }");
        var loop = doc.Nodes.First(n => n.Labels.Contains("CSharpWhileStatement"));
        var types = doc.Relationships.Where(r => r.Source == loop.Id).Select(r => r.Type).Distinct();
        Assert.Contains("HAS_CONDITION", types);
        Assert.Contains("HAS_BODY", types);
    }

    [Fact]
    public void Comment_becomes_trivia()
    {
        var doc = Build("// hello\nclass A { }");
        var trivia = doc.Nodes.First(n => n.Labels.Contains("CSharpTrivia"));
        Assert.Contains(doc.Relationships, r => r.Type == "HAS_TRIVIA" && r.Target == trivia.Id);
    }

    [Fact]
    public void Goto_and_yield_constructs_do_not_throw()
    {
        var source = "class C { int M() { goto Done; Done: return 1; } object N() { yield return 1; } }";
        var doc = Build(source); // must not throw
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpGotoStatement"));
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpYieldStatement"));
    }

    [Fact]
    public void Consecutive_statements_are_chained_with_follows()
    {
        var doc = Build("class C { void M() { int a = 1; int b = 2; int c = 3; } }");
        var statements = doc.Nodes
            .Where(n => n.Labels.Contains("CSharpLocalDeclarationStatement"))
            .ToList();
        Assert.Equal(3, statements.Count);

        var follows = doc.Relationships.Where(r => r.Type == "FOLLOWS").ToList();
        Assert.Equal(2, follows.Count);
        Assert.Equal(follows[0].Source, statements[0].Id);
        Assert.Equal(follows[0].Target, statements[1].Id);
        Assert.Equal(follows[1].Source, statements[1].Id);
        Assert.Equal(follows[1].Target, statements[2].Id);
    }

    [Fact]
    public void Follows_edges_do_not_cross_block_boundaries()
    {
        var doc = Build("class C { void M() { if (a) { b(); c(); } d(); } }");
        var follows = doc.Relationships.Where(r => r.Type == "FOLLOWS").ToList();
        Assert.Equal(2, follows.Count);

        // Inner if-body block: b() -> c()
        var ifStmt = doc.Nodes.First(n => n.Labels.Contains("CSharpIfStatement"));
        var innerBlock = doc.Relationships
            .First(r => r.Type == "HAS_BODY" && r.Source == ifStmt.Id)
            .Target;
        var innerStatements = doc.Relationships
            .Where(r => r.Type == "CONTAINS" && r.Source == innerBlock)
            .Select(r => r.Target)
            .ToList();
        Assert.Equal(2, innerStatements.Count);
        Assert.Contains(follows, f => f.Source == innerStatements[0] && f.Target == innerStatements[1]);

        // Outer block: if statement -> d()
        Assert.Contains(follows, f => f.Source == ifStmt.Id);
    }

    [Fact]
    public void Nodes_carry_source_coordinates()
    {
        var doc = Build("class C {\n    void M() { return; }\n}");
        var method = doc.Nodes.First(n => n.Labels.Contains("CSharpMethodDeclaration"));
        Assert.Equal(2, method.Properties["startLine"]);
        Assert.Equal(4, method.Properties["startColumn"]);
        Assert.Equal(2, method.Properties["endLine"]);
        Assert.Equal(24, method.Properties["endColumn"]);
        Assert.True(method.Properties.ContainsKey("startOffset"));
        Assert.True(method.Properties.ContainsKey("endOffset"));
    }

    [Fact]
    public void Symbol_references_record_kind_and_type()
    {
        var doc = BuildWithModel("class C { int Field; void M() { int x = Field; System.Console.WriteLine(x); } }");
        var field = doc.Nodes.First(n =>
            n.Labels.Contains("CSharpIdentifierName") && n.Properties["name"] as string == "Field");
        Assert.Equal("Field", field.Properties["symbolKind"]);
        Assert.Equal("int", field.Properties["symbolType"]);

        var local = doc.Nodes.First(n =>
            n.Labels.Contains("CSharpIdentifierName") && n.Properties["name"] as string == "x");
        Assert.Equal("Local", local.Properties["symbolKind"]);
        Assert.Equal("int", local.Properties["symbolType"]);
    }

    [Fact]
    public void Invocation_resolves_to_method_symbol()
    {
        var doc = BuildWithModel("class C { void M() { System.Console.WriteLine(1); } }");
        var call = doc.Nodes.First(n => n.Labels.Contains("CSharpInvocationExpression"));
        Assert.Equal("Method", call.Properties["symbolKind"]);
        Assert.Equal("void", call.Properties["symbolType"]);
    }

    [Fact]
    public void Calls_edge_from_enclosing_method_to_callee()
    {
        var doc = BuildWithModel("class C { void A() { B(); } void B() { } }");
        var methodA = doc.Nodes.First(n =>
            n.Labels.Contains("CSharpMethodDeclaration") && n.Properties["name"] as string == "A");

        var calls = doc.Relationships.Where(r => r.Type == "CALLS").ToList();
        Assert.Single(calls);
        Assert.Equal(methodA.Id, calls[0].Source);

        var callee = doc.Nodes.First(n => n.Id == calls[0].Target);
        Assert.Equal("CSharpSymbol", callee.Labels[0]);
        Assert.Equal("Method", callee.Properties["symbolKind"]);
        Assert.Equal("C.B()", callee.Properties["symbolName"]);
    }

    [Fact]
    public void Repeated_calls_to_same_symbol_share_one_callee_node()
    {
        var doc = BuildWithModel("class C { void A() { B(); B(); B(); } void B() { } }");
        var calls = doc.Relationships.Where(r => r.Type == "CALLS").ToList();
        Assert.Equal(3, calls.Count);
        Assert.Single(calls.Select(c => c.Target).Distinct());

        var calleeNodes = doc.Nodes.Where(n => n.Labels.Contains("CSharpSymbol")).ToList();
        Assert.Single(calleeNodes);
    }

    [Fact]
    public void Argument_ref_kind_is_recorded_as_booleans()
    {
        var doc = Build("class C { void M(ref int a, out int b, in int c, int d) { M(ref a, out b, in c, d); } }");
        var args = doc.Nodes.Where(n => n.Labels.Contains("CSharpArgument")).ToList();
        Assert.Equal(4, args.Count);

        Assert.True((bool)args[0].Properties["isRef"]!);
        Assert.False((bool)args[0].Properties["isOut"]!);
        Assert.False((bool)args[0].Properties["isIn"]!);

        Assert.False((bool)args[1].Properties["isRef"]!);
        Assert.True((bool)args[1].Properties["isOut"]!);
        Assert.False((bool)args[1].Properties["isIn"]!);

        Assert.False((bool)args[2].Properties["isRef"]!);
        Assert.False((bool)args[2].Properties["isOut"]!);
        Assert.True((bool)args[2].Properties["isIn"]!);

        Assert.False((bool)args[3].Properties["isRef"]!);
        Assert.False((bool)args[3].Properties["isOut"]!);
        Assert.False((bool)args[3].Properties["isIn"]!);
    }

    [Fact]
    public void Interpolated_string_holes_are_traversed()
    {
        var doc = Build("class C { void M(string name, int count) { var s = $\"Hello {name}, you have {count} items\"; } }");
        var interpolated = doc.Nodes.First(n => n.Labels.Contains("CSharpInterpolatedStringExpression"));
        var holes = doc.Relationships
            .Where(r => r.Source == interpolated.Id && r.Type == "HAS_OPERAND")
            .Select(r => doc.Nodes.First(n => n.Id == r.Target))
            .ToList();
        Assert.Equal(2, holes.Count);
        Assert.Contains(holes, h => h.Properties.TryGetValue("name", out var n) && n as string == "name");
        Assert.Contains(holes, h => h.Properties.TryGetValue("name", out var n) && n as string == "count");
    }

    [Fact]
    public void Top_level_statement_is_not_dropped()
    {
        var root = (CompilationUnitSyntax)CSharpSyntaxTree.ParseText("System.Console.WriteLine(\"hi\");").GetRoot();
        var doc = new GraphBuilder("test.cs").Build(root);
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpExpressionStatement"));
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpInvocationExpression"));
    }

    [Fact]
    public void Comment_attached_exactly_once()
    {
        var doc = Build("// hello\nclass C { }\n");
        var triviaCount = doc.Nodes.Count(n => n.Labels.Contains("CSharpTrivia"));
        Assert.Equal(1, triviaCount);
        var classNode = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration"));
        Assert.Contains(doc.Relationships, r => r.Type == "HAS_TRIVIA" && r.Source == classNode.Id);
    }

    [Fact]
    public void Comment_after_opening_brace_is_captured()
    {
        var doc = Build("class C { void M() { // inner\n } }");
        var trivia = doc.Nodes.Where(n => n.Labels.Contains("CSharpTrivia")).ToList();
        Assert.Single(trivia);
        var block = doc.Nodes.First(n => n.Labels.Contains("CSharpBlock"));
        Assert.Contains(doc.Relationships, r => r.Type == "HAS_TRIVIA" && r.Source == block.Id);
    }

    [Fact]
    public void Full_cfg_emits_basic_blocks_and_conditional_branches()
    {
        var doc = BuildWithModel("class C { void M() { while (x < 10) { y++; } z(); } }");
        var blocks = doc.Nodes.Where(n => n.Labels.Contains("CSharpBasicBlock")).ToList();
        Assert.True(blocks.Count >= 3);

        var blockEdges = doc.Relationships.Where(r => r.Type == "FOLLOWS"
            && blocks.Any(b => b.Id == r.Source)
            && blocks.Any(b => b.Id == r.Target)).ToList();
        Assert.True(blockEdges.Count >= 2);

        // A conditional block has a whenTrue successor.
        Assert.Contains(blockEdges, e => e.Properties?.ContainsKey("whenTrue") == true);
    }

    [Fact]
    public void Cfg_blocks_link_to_contained_statements()
    {
        var doc = BuildWithModel("class C { void M() { int a = 1; return; } }");
        var blocks = doc.Nodes.Where(n => n.Labels.Contains("CSharpBasicBlock")).ToList();
        var linked = doc.Relationships.Where(r =>
            r.Type == "CONTAINS" && blocks.Any(b => b.Id == r.Source)).ToList();
        Assert.True(linked.Count > 0);
    }

    [Fact]
    public void Class_attribute_is_captured()
    {
        var doc = Build("[System.Serializable] class C { }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration"));
        var attr = doc.Nodes.First(n => n.Labels.Contains("CSharpAttribute"));
        Assert.Equal("System.Serializable", attr.Properties["name"]);
        Assert.Contains(doc.Relationships, r => r.Source == cls.Id && r.Type == "HAS_ATTRIBUTE" && r.Target == attr.Id);
    }

    [Fact]
    public void Attribute_target_and_named_argument_are_captured()
    {
        var doc = Build("class C { [return: My(Value = 1)] int M() => 1; }");
        var attr = doc.Nodes.First(n => n.Labels.Contains("CSharpAttribute"));
        Assert.Equal("return", attr.Properties["target"]);
        var arg = doc.Nodes.First(n => n.Labels.Contains("CSharpAttributeArgument"));
        Assert.Equal("Value", arg.Properties["name"]);
        Assert.Contains(doc.Relationships, r => r.Source == attr.Id && r.Type == "HAS_ARGUMENT" && r.Target == arg.Id);
    }

    [Fact]
    public void Parameter_attribute_is_captured()
    {
        var doc = Build("class C { void M([My] int x) { } }");
        var param = doc.Nodes.First(n => n.Labels.Contains("CSharpParameter"));
        Assert.Contains(doc.Relationships, r => r.Source == param.Id && r.Type == "HAS_ATTRIBUTE");
    }

    [Fact]
    public void Operator_overload_is_captured_with_cfg()
    {
        var doc = BuildWithModel("class Vec { public static Vec operator +(Vec a, Vec b) { if (a == null) { return b; } return a; } }");
        var op = doc.Nodes.First(n => n.Labels.Contains("CSharpOperatorDeclaration"));
        Assert.Equal("+", op.Properties["operator"]);
        Assert.Contains(doc.Relationships, r => r.Source == op.Id && r.Type == "RETURNS");
        Assert.Equal(2, doc.Relationships.Count(r => r.Source == op.Id && r.Type == "HAS_PARAMETER"));
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpBasicBlock"));
    }

    [Fact]
    public void Conversion_operator_is_captured_with_cfg()
    {
        var doc = BuildWithModel("class Meters { public static implicit operator double(Meters m) { return 1.0; } }");
        var co = doc.Nodes.First(n => n.Labels.Contains("CSharpConversionOperatorDeclaration"));
        Assert.Equal("implicit", co.Properties["kind"]);
        Assert.Contains(doc.Relationships, r => r.Source == co.Id && r.Type == "RETURNS");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpBasicBlock"));
    }

    [Fact]
    public void Generic_constraints_are_captured()
    {
        var doc = Build("class C<T> where T : System.IDisposable, new() { }");
        var cls = doc.Nodes.First(n => n.Labels.Contains("CSharpClassDeclaration"));
        var constraintEdges = doc.Relationships.Where(r => r.Source == cls.Id && r.Type == "HAS_CONSTRAINT").ToList();
        Assert.Equal(2, constraintEdges.Count);
        Assert.Contains(constraintEdges, e => e.Properties != null
            && e.Properties.TryGetValue("typeParameter", out var tp) && (string?)tp == "T");
        var ctorConstraint = doc.Nodes.First(n => n.Labels.Contains("CSharpConstraint"));
        Assert.Equal("new()", ctorConstraint.Properties["kind"]);
    }

    [Fact]
    public void Switch_expression_and_arms_are_captured()
    {
        var doc = Build("class C { int M(int x) => x switch { 0 => 1, _ => 2 }; }");
        var swe = doc.Nodes.First(n => n.Labels.Contains("CSharpSwitchExpression"));
        var arms = doc.Nodes.Where(n => n.Labels.Contains("CSharpSwitchExpressionArm")).ToList();
        Assert.Equal(2, arms.Count);
        Assert.Equal(2, doc.Relationships.Count(r => r.Source == swe.Id && r.Type == "HAS_ARM"));
    }

    [Fact]
    public void Anonymous_method_body_is_walked()
    {
        var doc = Build("class C { void M() { System.Action a = delegate() { int y = 1; }; } }");
        var am = doc.Nodes.First(n => n.Labels.Contains("CSharpAnonymousMethodExpression"));
        Assert.Contains(doc.Relationships, r => r.Source == am.Id && r.Type == "HAS_BODY");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpLocalDeclarationStatement"));
    }

    [Fact]
    public void Query_expression_clauses_are_captured()
    {
        var doc = Build("class C { void M() { var q = from x in xs where x > 0 orderby x select x; } }");
        var qe = doc.Nodes.First(n => n.Labels.Contains("CSharpQueryExpression"));
        var kinds = doc.Nodes.Where(n => n.Labels.Contains("CSharpQueryClause"))
            .Select(n => n.Properties["kind"] as string)
            .ToList();
        Assert.Contains("from", kinds);
        Assert.Contains("where", kinds);
        Assert.Contains("orderby", kinds);
        Assert.Contains("select", kinds);
        Assert.Contains(doc.Relationships, r => r.Source == qe.Id && r.Type == "HAS_CLAUSE");
    }

    [Fact]
    public void Lambda_body_gets_cfg_when_it_has_branches()
    {
        var doc = BuildWithModel("class C { void M() { System.Func<int, int> f = x => { if (x > 0) { return x; } return -x; }; } }");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpBasicBlock"));
    }

    [Fact]
    public void Expression_bodied_property_attributes_calls_to_itself()
    {
        var doc = BuildWithModel("class C { int Compute() => 1; int X => Compute(); }");
        var prop = doc.Nodes.First(n => n.Labels.Contains("CSharpPropertyDeclaration") && n.Properties["name"] as string == "X");
        Assert.Contains(doc.Relationships, r => r.Source == prop.Id && r.Type == "CALLS");
    }

    [Fact]
    public void Expression_bodied_property_gets_cfg()
    {
        var doc = BuildWithModel("class C { int X => 1; }");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpBasicBlock") && n.Properties["scope"] as string == "C.X");
    }

    [Fact]
    public void Expression_bodied_indexer_gets_cfg()
    {
        var doc = BuildWithModel("class C { int this[int i] => i; }");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpBasicBlock"));
    }

    [Fact]
    public void Record_positional_parameters_are_captured()
    {
        var doc = Build("record Dog(string Name, int Age);");
        var rec = doc.Nodes.First(n => n.Labels.Contains("CSharpRecordDeclaration"));
        var paramEdges = doc.Relationships.Where(r => r.Source == rec.Id && r.Type == "HAS_PARAMETER").ToList();
        Assert.Equal(2, paramEdges.Count);
    }

    [Fact]
    public void Namespace_using_directive_is_captured()
    {
        var doc = Build("namespace Foo { using System; class C { } }");
        var ns = doc.Nodes.First(n => n.Labels.Contains("CSharpNamespaceDeclaration"));
        Assert.Contains(doc.Relationships, r => r.Source == ns.Id && r.Type == "USES");
    }

    [Fact]
    public void Extern_alias_directive_is_captured()
    {
        var doc = Build("extern alias Foo;\nclass C { }");
        Assert.Contains(doc.Nodes, n => n.Labels.Contains("CSharpExternAliasDirective") && n.Properties["identifier"] as string == "Foo");
    }

    [Fact]
    public void Binary_expression_operands_carry_ordinal()
    {
        var doc = Build("class C { void M() { var x = a - b; } }");
        var bin = doc.Nodes.First(n => n.Labels.Contains("CSharpBinaryExpression"));
        var operands = doc.Relationships.Where(r => r.Source == bin.Id && r.Type == "HAS_OPERAND")
            .OrderBy(r => r.Properties!["ordinal"])
            .ToList();
        Assert.Equal(2, operands.Count);
        Assert.Equal(0, operands[0].Properties!["ordinal"]);
        Assert.Equal(1, operands[1].Properties!["ordinal"]);
        var left = doc.Nodes.First(n => n.Id == operands[0].Target);
        var right = doc.Nodes.First(n => n.Id == operands[1].Target);
        Assert.Equal("a", left.Properties["name"]);
        Assert.Equal("b", right.Properties["name"]);
    }

    [Fact]
    public void Argument_ordinal_matches_call_position()
    {
        var doc = Build("class C { void M() { F(x, y, z); } }");
        var inv = doc.Nodes.First(n => n.Labels.Contains("CSharpInvocationExpression"));
        var argsByOrdinal = doc.Relationships.Where(r => r.Source == inv.Id && r.Type == "HAS_ARGUMENT")
            .OrderBy(r => (int)r.Properties!["ordinal"]!)
            .Select(r => doc.Nodes.First(n => n.Id == r.Target))
            .ToList();
        Assert.Equal(3, argsByOrdinal.Count);
        var names = argsByOrdinal
            .Select(a => doc.Relationships.First(r => r.Source == a.Id && r.Type == "HAS_EXPRESSION").Target)
            .Select(id => doc.Nodes.First(n => n.Id == id).Properties["name"] as string)
            .ToList();
        Assert.Equal(new[] { "x", "y", "z" }, names);
    }

    [Fact]
    public void Parameter_list_carries_ordinal()
    {
        var doc = Build("class C { void M(int a, int b, int c) { } }");
        var method = doc.Nodes.First(n => n.Labels.Contains("CSharpMethodDeclaration"));
        var ordinals = doc.Relationships.Where(r => r.Source == method.Id && r.Type == "HAS_PARAMETER")
            .Select(r => (int)r.Properties!["ordinal"]!)
            .OrderBy(o => o)
            .ToList();
        Assert.Equal(new[] { 0, 1, 2 }, ordinals);
    }

    [Fact]
    public void Cfg_block_statements_carry_ordinal()
    {
        var doc = BuildWithModel("class C { void M() { int a = 1; int b = 2; int c = 3; } }");
        var block = doc.Nodes.First(n => n.Labels.Contains("CSharpBasicBlock") && n.Properties["kind"] as string == "Block");
        var ordinals = doc.Relationships.Where(r => r.Source == block.Id && r.Type == "CONTAINS")
            .Select(r => (int)r.Properties!["ordinal"]!)
            .OrderBy(o => o)
            .ToList();
        Assert.Equal(new[] { 0, 1, 2 }, ordinals);
    }
}

public class SouffleWriterTests
{
    private static CypherDocument BuildWithModel(string source, string file = "test.cs")
    {
        var tree = (CSharpSyntaxTree)CSharpSyntaxTree.ParseText(source, path: file);
        var references = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES"))
            ?.Split(Path.PathSeparator)
            .Where(p => !string.IsNullOrEmpty(p) && File.Exists(p))
            .Select(p => (MetadataReference)MetadataReference.CreateFromFile(p))
            .ToArray() ?? Array.Empty<MetadataReference>();
        var compilation = CSharpCompilation.Create("Test", new[] { tree }, references);
        var model = compilation.GetSemanticModel(tree);
        return new GraphBuilder(file, model).Build((CompilationUnitSyntax)tree.GetRoot());
    }

    private static string Render(string source)
    {
        var writer = new SouffleWriter();
        writer.AddCfg(BuildWithModel(source));
        return writer.Render();
    }

    [Fact]
    public void Render_includes_decls_facts_and_rules()
    {
        var text = Render("class C { void M() { while (x < 10) { y++; } z(); } }");
        Assert.Contains(".decl basic_block(", text);
        Assert.Contains(".decl control_flow_edge(", text);
        Assert.Contains(".decl block_statement(", text);
        Assert.Contains("basic_block(", text);
        Assert.Contains("control_flow_edge(", text);
        Assert.Contains("block_statement(", text);
        Assert.Contains("reachable(Id, Function) :- basic_block(Id, Function, _, 1, _).", text);
        Assert.Contains(".output reachable", text);
        Assert.Contains(".output path", text);
    }

    [Fact]
    public void Block_ids_are_remapped_to_global_b_ids()
    {
        var text = Render("class C { void M() { return; } }");
        Assert.Contains("basic_block(\"b1\"", text);
    }

    [Fact]
    public void Blocks_carry_scope_entry_and_final_flags()
    {
        var doc = BuildWithModel("class C { void M() { return; } }");
        var blocks = doc.Nodes.Where(n => n.Labels.Contains("CSharpBasicBlock")).ToList();
        var first = blocks.First();
        Assert.True(first.Properties.ContainsKey("scope"));
        Assert.Equal("C.M()", first.Properties["scope"]);
        Assert.Contains(blocks, b => b.Properties["isEntry"] is true);
        Assert.Contains(blocks, b => b.Properties["isFinal"] is true);
    }

    [Fact]
    public void Edge_condition_records_branch_semantics()
    {
        var text = Render("class C { void M() { if (x) { y(); } z(); } }");
        Assert.Contains("control_flow_edge(", text);
    }

    [Fact]
    public void Symbols_are_escaped()
    {
        var text = Render("class C { void M() { return; } }");
        Assert.Contains("basic_block(\"b1\", \"C.M()\"", text);
    }

    [Fact]
    public void Multiple_files_share_one_program_with_remapped_ids()
    {
        var writer = new SouffleWriter();
        writer.AddCfg(BuildWithModel("class C { void M() { return; } }", "a.cs"));
        writer.AddCfg(BuildWithModel("class D { void N() { return; } }", "b.cs"));
        var text = writer.Render();
        Assert.Contains("basic_block(\"b1\"", text);
        Assert.Contains("basic_block(\"b2\"", text);
        Assert.Contains("\"D.N()\"", text);
    }
}