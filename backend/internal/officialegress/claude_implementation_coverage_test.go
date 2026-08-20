package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const claudeFWGImplementationCoverageSHA256 = "79c208e3775b3dfdc3f62ea95548550801bf2aea96e67d5010b390f894ec008c"

type claudeFWGSourceAnchor struct {
	Path   string `json:"path"`
	Symbol string `json:"symbol"`
}

type claudeFWGImplementationCoverageEntry struct {
	SpecID                string                  `json:"spec_id"`
	ImplementationAnchors []claudeFWGSourceAnchor `json:"implementation_anchors"`
	TestAnchors           []claudeFWGSourceAnchor `json:"test_anchors"`
}

// TestClaudeFWGRequiredRulesHaveExecutableImplementationCoverage 证明 FW-F 的
// 40 条 RequiredRules 均恰好一次绑定到真实实现符号和会被 go test 执行的测试。
func TestClaudeFWGRequiredRulesHaveExecutableImplementationCoverage(t *testing.T) {
	repositoryRoot := filepath.Clean("../../..")
	manifestPath := filepath.Join(
		repositoryRoot,
		"tools/official_client_capture/claude_required_rules_2_1_226.json",
	)
	coveragePath := filepath.Join(
		repositoryRoot,
		"tools/official_client_capture/claude_fw_g_implementation_coverage_2_1_226.json",
	)
	manifestRaw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	coverageRaw, err := os.ReadFile(coveragePath)
	if err != nil {
		t.Fatal(err)
	}
	coverageDigest := sha256.Sum256(coverageRaw)
	if got := hex.EncodeToString(coverageDigest[:]); got != claudeFWGImplementationCoverageSHA256 {
		t.Fatalf(
			"Claude FW-G 实现覆盖矩阵摘要漂移：got=%s want=%s",
			got, claudeFWGImplementationCoverageSHA256,
		)
	}

	var manifest struct {
		TargetVersion string `json:"target_version"`
		RequiredRules []struct {
			SpecID string `json:"spec_id"`
		} `json:"required_rules"`
	}
	if err := json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatal(err)
	}
	var coverage struct {
		SchemaVersion               string                                 `json:"schema_version"`
		TargetVersion               string                                 `json:"target_version"`
		RequiredRulesManifestSHA256 string                                 `json:"required_rules_manifest_sha256"`
		ImplementationBaseCommit    string                                 `json:"implementation_base_commit"`
		RequiredRuleCount           int                                    `json:"required_rule_count"`
		Entries                     []claudeFWGImplementationCoverageEntry `json:"entries"`
	}
	if err := json.Unmarshal(coverageRaw, &coverage); err != nil {
		t.Fatal(err)
	}
	manifestDigest := sha256.Sum256(manifestRaw)
	if coverage.SchemaVersion != "claude-code-fw-g-implementation-coverage/v1" ||
		coverage.TargetVersion != ClaudeFWGVersion ||
		coverage.TargetVersion != manifest.TargetVersion ||
		coverage.RequiredRulesManifestSHA256 != hex.EncodeToString(manifestDigest[:]) ||
		len(coverage.ImplementationBaseCommit) != sha256.Size*2-24 ||
		coverage.RequiredRuleCount != 40 || len(coverage.Entries) != 40 {
		t.Fatalf("Claude FW-G 实现覆盖矩阵顶层事实非法：%+v", coverage)
	}

	manifestIDs := make([]string, 0, len(manifest.RequiredRules))
	for _, rule := range manifest.RequiredRules {
		manifestIDs = append(manifestIDs, rule.SpecID)
	}
	slices.Sort(manifestIDs)
	coverageIDs := make([]string, 0, len(coverage.Entries))
	seen := make(map[string]struct{}, len(coverage.Entries))
	parsedFiles := make(map[string]map[string]bool)
	for _, entry := range coverage.Entries {
		if _, duplicate := seen[entry.SpecID]; duplicate {
			t.Fatalf("Claude RequiredRule 在实现覆盖矩阵重复：%s", entry.SpecID)
		}
		seen[entry.SpecID] = struct{}{}
		coverageIDs = append(coverageIDs, entry.SpecID)
		if len(entry.ImplementationAnchors) == 0 || len(entry.TestAnchors) == 0 {
			t.Fatalf("Claude RequiredRule 缺少实现或测试锚点：%s", entry.SpecID)
		}
		validateClaudeFWGSourceAnchors(
			t, repositoryRoot, entry.SpecID, entry.ImplementationAnchors, false, parsedFiles,
		)
		validateClaudeFWGSourceAnchors(
			t, repositoryRoot, entry.SpecID, entry.TestAnchors, true, parsedFiles,
		)
	}
	slices.Sort(coverageIDs)
	if !slices.Equal(coverageIDs, manifestIDs) {
		t.Fatalf(
			"Claude FW-G 实现覆盖集合不等于 RequiredRules：got=%v want=%v",
			coverageIDs, manifestIDs,
		)
	}
}

func validateClaudeFWGSourceAnchors(
	t *testing.T,
	repositoryRoot string,
	specID string,
	anchors []claudeFWGSourceAnchor,
	testAnchor bool,
	parsedFiles map[string]map[string]bool,
) {
	t.Helper()
	seen := make(map[string]struct{}, len(anchors))
	for _, anchor := range anchors {
		key := anchor.Path + "#" + anchor.Symbol
		if _, duplicate := seen[key]; duplicate {
			t.Fatalf("%s 的源码锚点重复：%s", specID, key)
		}
		seen[key] = struct{}{}
		if !strings.HasPrefix(anchor.Path, "backend/internal/") ||
			!strings.HasSuffix(anchor.Path, ".go") ||
			strings.TrimSpace(anchor.Symbol) == "" {
			t.Fatalf("%s 的源码锚点非法：%+v", specID, anchor)
		}
		if testAnchor != strings.HasSuffix(anchor.Path, "_test.go") ||
			(testAnchor && !strings.HasPrefix(anchor.Symbol, "TestClaudeFWG")) {
			t.Fatalf("%s 的实现／测试锚点分类错误：%+v", specID, anchor)
		}
		path := filepath.Join(repositoryRoot, filepath.FromSlash(anchor.Path))
		symbols, ok := parsedFiles[path]
		if !ok {
			file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
			if err != nil {
				t.Fatalf("解析 %s 失败：%v", anchor.Path, err)
			}
			symbols = make(map[string]bool)
			for _, declaration := range file.Decls {
				function, ok := declaration.(*ast.FuncDecl)
				if ok {
					symbols[function.Name.Name] = function.Recv != nil
				}
			}
			parsedFiles[path] = symbols
		}
		if _, exists := symbols[anchor.Symbol]; !exists {
			t.Fatalf("%s 引用的源码符号不存在：%s", specID, key)
		}
	}
}
