using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;

namespace RoslynParser
{
    class Program
    {
        static async Task Main(string[] args)
        {
            if (args.Length < 1)
            {
                Console.WriteLine("Usage: RoslynParser <path-to-sln-or-csproj>");
                return;
            }

            var path = args[0];
            if (!File.Exists(path))
            {
                Console.WriteLine($"File not found: {path}");
                return;
            }

            MSBuildLocator.RegisterDefaults();

            using var workspace = MSBuildWorkspace.Create();
            workspace.LoadMetadataForReferencedProjects = true;

            Solution solution;
            if (path.EndsWith(".sln"))
            {
                solution = await workspace.OpenSolutionAsync(path);
            }
            else
            {
                var project = await workspace.OpenProjectAsync(path);
                solution = project.Solution;
            }

            var symbols = new List<object>();
            var edges = new List<object>();

            int currentId = 1;
            var symbolIdMap = new Dictionary<ISymbol, int>(SymbolEqualityComparer.Default);

            int GetSymbolId(ISymbol sym)
            {
                if (!symbolIdMap.TryGetValue(sym, out int id))
                {
                    id = currentId++;
                    symbolIdMap[sym] = id;
                }
                return id;
            }

            foreach (var project in solution.Projects)
            {
                var compilation = await project.GetCompilationAsync();
                if (compilation == null) continue;

                foreach (var document in project.Documents)
                {
                    if (document.FilePath == null) continue;
                    
                    var model = await document.GetSemanticModelAsync();
                    if (model == null) continue;

                    var root = await document.GetSyntaxRootAsync();
                    if (root == null) continue;

                    var fileId = document.FilePath; 

                    var declaredNodes = root.DescendantNodes().OfType<MemberDeclarationSyntax>();
                    
                    foreach (var node in declaredNodes)
                    {
                        var declaredSymbol = model.GetDeclaredSymbol(node);
                        if (declaredSymbol == null) continue;

                        if (declaredSymbol is INamedTypeSymbol typeSymbol)
                        {
                            var id = GetSymbolId(typeSymbol);
                            var location = node.GetLocation().GetLineSpan();
                            
                            string kind = typeSymbol.TypeKind.ToString().ToLower();
                            var sourceSpan = node.Span;
                            string body = root.GetText().GetSubText(sourceSpan).ToString();

                            symbols.Add(new {
                                ast_node_id = id,
                                file_path = fileId,
                                name = typeSymbol.Name,
                                qualified_name = typeSymbol.ToDisplayString(),
                                kind = kind,
                                signature = typeSymbol.ToDisplayString(),
                                language = "c_sharp",
                                body = body,
                                line_start = location.StartLinePosition.Line + 1,
                                line_end = location.EndLinePosition.Line + 1
                            });

                            if (typeSymbol.BaseType != null && typeSymbol.BaseType.SpecialType != SpecialType.System_Object)
                            {
                                edges.Add(new {
                                    source_ast_id = id,
                                    target_qualified_name = typeSymbol.BaseType.ToDisplayString(),
                                    kind = "INHERITS"
                                });
                            }

                            foreach (var iface in typeSymbol.Interfaces)
                            {
                                edges.Add(new {
                                    source_ast_id = id,
                                    target_qualified_name = iface.ToDisplayString(),
                                    kind = "IMPLEMENTS"
                                });
                            }
                        }
                        else if (declaredSymbol is IMethodSymbol methodSymbol)
                        {
                            var id = GetSymbolId(methodSymbol);
                            var location = node.GetLocation().GetLineSpan();
                            var sourceSpan = node.Span;
                            string body = root.GetText().GetSubText(sourceSpan).ToString();

                            symbols.Add(new {
                                ast_node_id = id,
                                file_path = fileId,
                                name = methodSymbol.Name,
                                qualified_name = methodSymbol.ToDisplayString(),
                                kind = "method",
                                signature = methodSymbol.ToDisplayString(),
                                language = "c_sharp",
                                body = body,
                                line_start = location.StartLinePosition.Line + 1,
                                line_end = location.EndLinePosition.Line + 1
                            });

                            // Find calls
                            var invocations = node.DescendantNodes().OfType<InvocationExpressionSyntax>();
                            foreach (var inv in invocations)
                            {
                                var symInfo = model.GetSymbolInfo(inv);
                                if (symInfo.Symbol is IMethodSymbol targetMethod)
                                {
                                    edges.Add(new {
                                        source_ast_id = id,
                                        target_qualified_name = targetMethod.ToDisplayString(),
                                        kind = "CALLS"
                                    });
                                }
                            }
                        }
                    }
                }
            }

            var output = new
            {
                symbols = symbols,
                edges = edges
            };

            var json = JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true });
            Console.WriteLine(json);
        }
    }
}
