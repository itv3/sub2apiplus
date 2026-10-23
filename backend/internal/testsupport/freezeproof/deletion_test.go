package freezeproof

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// deletionFixture 使用独立 Git 仓库固定“存在→删除”事实，避免测试修改真实源码或审计收据。
type deletionFixture struct {
	root     string
	path     string
	previous string
	document map[string]any
}

func newDeletionFixture(t *testing.T) deletionFixture {
	t.Helper()
	root := t.TempDir()
	git := func(arguments ...string) string {
		t.Helper()
		raw, err := gitOutput(root, arguments...)
		if err != nil {
			t.Fatalf("构造删除证明仓库失败：git %v：%v", arguments, err)
		}
		return strings.TrimSpace(string(raw))
	}
	git("init", "--quiet")
	git("config", "user.name", "冻结证明测试")
	git("config", "user.email", "freeze-proof@example.invalid")
	git("config", "commit.gpgsign", "false")
	git("config", "core.hooksPath", "/dev/null")
	path := "src/retired.go"
	previous := []byte("package retired\n")
	filename := filepath.Join(root, filepath.FromSlash(path))
	if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filename, previous, 0o644); err != nil {
		t.Fatal(err)
	}
	git("add", ".")
	git("commit", "--quiet", "-m", "记录删除前源码")
	base := git("rev-parse", "HEAD")
	git("rm", "--quiet", "--", path)
	git("commit", "--quiet", "-m", "下线测试源码")
	current := git("rev-parse", "HEAD")
	return deletionFixture{
		root: root, path: path, previous: digest(previous),
		document: map[string]any{
			"schema_version": "official-egress-upstream-freeze-successor/v1",
			"mode":           "commit", "base_commit": base, "current_commit": current,
			"result": "passed_with_deletions", "required_manual_actions": []any{},
			"deleted_frozen_paths": []any{path},
			"deletion_proof": map[string]any{
				"algorithm": "deletion-proof/v1", "reason": "经审核下线，完成检查 && 确认 <零引用>。",
				"historical_readers": []any{},
				"deleted_paths": []any{map[string]any{
					"path": path, "last_sha256": digest(previous), "frozen": true,
					"reference_scan": map[string]any{
						"algorithm": "reference-scan/v1", "patterns": []any{path, filepath.Base(path)},
						"references": []any{},
						"scopes":     []any{"python:import-and-attribute", "shell:invocation", "json:command-fields"},
					},
				}},
			},
		},
	}
}

// sealDeletionDocument 与 Python 收据合同一致地保留非 ASCII 与 HTML 字符，且不加入尾换行。
func sealDeletionDocument(t *testing.T, document map[string]any) []byte {
	t.Helper()
	delete(document, "identity_sha256")
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(document); err != nil {
		t.Fatal(err)
	}
	document["identity_sha256"] = digest(bytes.TrimSuffix(buffer.Bytes(), []byte("\n")))
	raw, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func cloneDeletionDocument(t *testing.T, document map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	var cloned map[string]any
	if err := json.Unmarshal(raw, &cloned); err != nil {
		t.Fatal(err)
	}
	return cloned
}

// fixtureValue 在夹具结构意外变化时明确终止测试，避免负例构造使用未检查的断言。
func fixtureValue[T any](t *testing.T, value any) T {
	t.Helper()
	result, ok := value.(T)
	if !ok {
		t.Fatalf("删除证明测试夹具类型不符：%T", value)
	}
	return result
}

func proofEntry(t *testing.T, document map[string]any) map[string]any {
	t.Helper()
	proof := fixtureValue[map[string]any](t, document["deletion_proof"])
	entries := fixtureValue[[]any](t, proof["deleted_paths"])
	if len(entries) == 0 {
		t.Fatal("删除证明测试夹具缺少源码条目")
	}
	return fixtureValue[map[string]any](t, entries[0])
}

func TestDeletedSourceRequiresCompleteEvidence(t *testing.T) {
	fixture := newDeletionFixture(t)
	raw := sealDeletionDocument(t, fixture.document)
	if !deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("提交中已证明的精确删除未获接受")
	}
	cases := []struct {
		name   string
		mutate func(*testing.T, map[string]any)
	}{
		{"缺少证明", func(t *testing.T, doc map[string]any) { delete(doc, "deletion_proof") }},
		{"缺少原因", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, doc["deletion_proof"])["reason"] = "  "
		}},
		{"证明算法错误", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, doc["deletion_proof"])["algorithm"] = "其他算法"
		}},
		{"非冻结名单", func(t *testing.T, doc map[string]any) { doc["deleted_frozen_paths"] = []any{} }},
		{"冻结标志错误", func(t *testing.T, doc map[string]any) { proofEntry(t, doc)["frozen"] = false }},
		{"重复路径", func(t *testing.T, doc map[string]any) {
			proof := fixtureValue[map[string]any](t, doc["deletion_proof"])
			proof["deleted_paths"] = append(fixtureValue[[]any](t, proof["deleted_paths"]), proofEntry(t, doc))
		}},
		{"删除前摘要错误", func(t *testing.T, doc map[string]any) { proofEntry(t, doc)["last_sha256"] = strings.Repeat("a", 64) }},
		{"扫描算法错误", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, proofEntry(t, doc)["reference_scan"])["algorithm"] = "其他算法"
		}},
		{"遗漏引用范围", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, proofEntry(t, doc)["reference_scan"])["scopes"] = []any{}
		}},
		{"遗漏引用模式", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, proofEntry(t, doc)["reference_scan"])["patterns"] = []any{fixture.path}
		}},
		{"引用结果未提供", func(t *testing.T, doc map[string]any) {
			delete(fixtureValue[map[string]any](t, proofEntry(t, doc)["reference_scan"]), "references")
		}},
		{"仍有引用", func(t *testing.T, doc map[string]any) {
			fixtureValue[map[string]any](t, proofEntry(t, doc)["reference_scan"])["references"] = []any{"仍有调用"}
		}},
		{"非法路径", func(t *testing.T, doc map[string]any) { proofEntry(t, doc)["path"] = "../retired.go" }},
		{"无效提交", func(t *testing.T, doc map[string]any) { doc["current_commit"] = strings.Repeat("a", 40) }},
		{"当前提交仍有源码", func(t *testing.T, doc map[string]any) {
			doc["base_commit"], doc["current_commit"] = doc["current_commit"], doc["base_commit"]
		}},
		{"未完成待办", func(t *testing.T, doc map[string]any) {
			doc["result"] = "manual_actions_required"
			doc["required_manual_actions"] = []any{map[string]any{"action_kind": "manual_required"}}
		}},
		{"结果错误", func(t *testing.T, doc map[string]any) { doc["result"] = "passed" }},
		{"通过状态仍有待办", func(t *testing.T, doc map[string]any) {
			doc["required_manual_actions"] = []any{map[string]any{"action_kind": "single_hop_file"}}
		}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			document := cloneDeletionDocument(t, fixture.document)
			test.mutate(t, document)
			if deletionDocumentAccepted(fixture.root, sealDeletionDocument(t, document), fixture.path, fixture.previous, nil) {
				t.Fatal("不完整或冲突的删除证明被接受")
			}
		})
	}
	t.Run("身份摘要损坏", func(t *testing.T) {
		document := cloneDeletionDocument(t, fixture.document)
		document["identity_sha256"] = strings.Repeat("0", 64)
		broken, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		if deletionDocumentAccepted(fixture.root, broken, fixture.path, fixture.previous, nil) {
			t.Fatal("身份摘要损坏的删除证明被接受")
		}
	})
	unknown := strings.Repeat("b", 64)
	if deletionDocumentAccepted(fixture.root, raw, fixture.path, unknown, nil) {
		t.Fatal("未知历史摘要被接受")
	}
	if !deletionDocumentAccepted(fixture.root, raw, fixture.path, unknown, func(path, from, to string) bool {
		return path == fixture.path && from == unknown && to == fixture.previous
	}) {
		t.Fatal("已登记历史摘要未沿审计图承接到删除前摘要")
	}
}

func TestDeletedSourceRejectsExistingOrSymlinkPath(t *testing.T) {
	fixture := newDeletionFixture(t)
	raw := sealDeletionDocument(t, fixture.document)
	filename := filepath.Join(fixture.root, filepath.FromSlash(fixture.path))
	if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filename, []byte("仍存在的源码"), 0o644); err != nil {
		t.Fatal(err)
	}
	if deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("仍在工作树的源码被当作删除")
	}
	if err := os.RemoveAll(filepath.Dir(filename)); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Dir(filename)); err != nil {
		t.Fatal(err)
	}
	if deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("符号链接父目录造成的缺失被当作删除")
	}
}

func TestDeletedSourceAcceptsOnlyCompletedScannerAction(t *testing.T) {
	fixture := newDeletionFixture(t)
	write := func(path string, raw []byte) {
		t.Helper()
		filename := filepath.Join(fixture.root, filepath.FromSlash(path))
		if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filename, raw, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeJSON := func(path string, document map[string]any) {
		t.Helper()
		raw, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		write(path, raw)
	}
	scannerPath := "backend/cmd/egressscan/main.go"
	scanner := []byte("package main\n")
	write(scannerPath, scanner)
	algorithm := sha256.New()
	_, _ = algorithm.Write([]byte("main.go\x00"))
	_, _ = algorithm.Write(scanner)
	_, _ = algorithm.Write([]byte{0})
	from := strings.Repeat("a", 64)
	writeJSON("docs/egress/maintenance/bootstrap-inventory-lock.json", map[string]any{"scanner_algorithm_sha256": from})
	sourcePath := "docs/egress/maintenance/fixture-source-transition.json"
	successor := map[string]any{
		"schema_version": "official-egress-scanner-algorithm-successor/v1",
		"from_sha256":    from, "to_sha256": hex.EncodeToString(algorithm.Sum(nil)),
		"source_transition": sourcePath, "reviewed_by": "冻结证明测试", "reason": "扫描器变更已经审阅完成",
	}
	writeJSON("docs/egress/maintenance/scanner-algorithm-successor.json", successor)
	fixture.document["result"] = "manual_actions_required"
	fixture.document["required_manual_actions"] = []any{map[string]any{
		"action_kind": "single_hop_file", "rule_id": "scanner-algorithm-successor",
		"file": "docs/egress/maintenance/scanner-algorithm-successor.json", "path": scannerPath,
	}}
	raw := sealDeletionDocument(t, fixture.document)
	write(sourcePath, raw)
	if !deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("已完成、精确绑定的扫描器待办未获接受")
	}
	// 待办名相同仍不够：当前算法必须仍与已审核的一跳目标完全一致。
	write(scannerPath, []byte("package changed\n"))
	if deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("当前扫描器偏离已审核算法时删除证明仍被接受")
	}
	write(scannerPath, scanner)
	successor["from_sha256"] = strings.Repeat("b", 64)
	writeJSON("docs/egress/maintenance/scanner-algorithm-successor.json", successor)
	if deletionDocumentAccepted(fixture.root, raw, fixture.path, fixture.previous, nil) {
		t.Fatal("扫描器单跳未绑定原始算法锁时删除证明仍被接受")
	}
	// 同时验证公开入口读取封存收据，而不是仅测试内部解析函数。
	successor["from_sha256"] = from
	writeJSON("docs/egress/maintenance/scanner-algorithm-successor.json", successor)
	if !DeletedSourceAccepted(fixture.root, fixture.path, fixture.previous, nil) {
		t.Fatal("公开入口未接受可验证的封存删除收据")
	}
}
