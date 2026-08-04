package service

import (
	"crypto/sha256"
	"encoding/hex"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"testing"
)

var changeset5ExtinctLegacySymbols = map[string]bool{
	"attachOfficialCodex0145EndpointRequest":          true,
	"attachOfficialCodex0145EndpointWebSocketContext": true,
	"officialCodex0145FinalizeEndpointJSONBody":       true,
	"officialCodex0145FinalizeEndpointHeaders":        true,
	"finalizeOpenAIOfficialEgressWSHandshakeHeaders":  true,
}

// TestChangeset5LegacyAttachFinalizerDefinitionsAndCallsAreExtinct 同时约束定义和
// 调用为零，防止旧链以后通过包装函数、别名或“仅保留兼容定义”复活。
func TestChangeset5LegacyAttachFinalizerDefinitionsAndCallsAreExtinct(t *testing.T) {
	const inventorySHA256 = "7be87aeec0dfef8585d8b999becf4977ac726578a9ba85238718268a5dfa2648"
	raw, err := os.ReadFile("../../../docs/changeset5/legacy-symbol-inventory.json")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != inventorySHA256 {
		t.Fatal("变更集 5 旧链开发前库存摘要漂移")
	}

	fset := token.NewFileSet()
	files := parseOfficialEgressPackage(t, fset)
	definitions, calls := changeset5LegacySymbolCounts(files)
	for symbol := range changeset5ExtinctLegacySymbols {
		if definitions[symbol] != 0 || calls[symbol] != 0 {
			t.Fatalf(
				"旧链符号未绝迹：symbol=%s definitions=%d calls=%d",
				symbol, definitions[symbol], calls[symbol],
			)
		}
	}
}

func TestChangeset5LegacyExtinctionGateRejectsDefinitionsAndWrappedCalls(t *testing.T) {
	source := `package service
func officialCodex0145FinalizeEndpointHeaders() {}
func wrapper() { officialCodex0145FinalizeEndpointHeaders() }
`
	file, err := parser.ParseFile(token.NewFileSet(), "negative.go", source, 0)
	if err != nil {
		t.Fatal(err)
	}
	definitions, calls := changeset5LegacySymbolCounts(map[string]*ast.File{"negative.go": file})
	if definitions["officialCodex0145FinalizeEndpointHeaders"] != 1 ||
		calls["officialCodex0145FinalizeEndpointHeaders"] != 1 {
		t.Fatalf("旧链绝迹负例未被完整识别：definitions=%v calls=%v", definitions, calls)
	}
}

func changeset5LegacySymbolCounts(files map[string]*ast.File) (map[string]int, map[string]int) {
	definitions := make(map[string]int)
	calls := make(map[string]int)
	for _, file := range files {
		for _, declaration := range file.Decls {
			if function, ok := declaration.(*ast.FuncDecl); ok &&
				changeset5ExtinctLegacySymbols[function.Name.Name] {
				definitions[function.Name.Name]++
			}
		}
		ast.Inspect(file, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if ok {
				name := calleeName(call)
				if changeset5ExtinctLegacySymbols[name] {
					calls[name]++
				}
			}
			return true
		})
	}
	return definitions, calls
}
