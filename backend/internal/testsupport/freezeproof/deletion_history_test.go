package freezeproof

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// scannerHistoryOptions 描述“删除收据之后扫描器又变更过”场景里需要故意破坏的环节，零值表示全部合法。
type scannerHistoryOptions struct {
	histSource    string // 历史单跳指向的收据路径；为空时指向删除收据本身
	histFrom      string // 历史单跳的 from；为空时等于 Bootstrap 锁摘要
	histTo        string // 历史单跳的 to；为空时等于当时扫描器源码摘要
	curFrom       string // 当前单跳的 from（同时改写当前锁文件）；为空时等于 Bootstrap 锁摘要
	curTo         string // 当前单跳的 to；为空时等于当前扫描器源码摘要
	nextBaseFirst bool   // 后续收据的 base 取删除区间之前的提交，使其不是后代
	nextOffHead   bool   // 后续收据的 current 取不在 HEAD 历史上的提交
	brokenNext    bool   // 后续收据身份摘要损坏
}

// scannerHistoryFixture 保存一个已经走完“删除 -> 扫描器推进到后续收据”的独立 Git 仓库。
type scannerHistoryFixture struct {
	root     string
	path     string
	previous string
	raw      []byte // 删除收据原文
}

const (
	historyDeletionReceiptPath = "docs/egress/maintenance/history-delete-freeze-successor.json"
	historyOtherReceiptPath    = "docs/egress/maintenance/history-other-freeze-successor.json"
	historyNextReceiptPath     = "docs/egress/maintenance/history-next-freeze-successor.json"
	historyScannerPath         = "backend/cmd/egressscan/main.go"
)

// scannerAlgorithmOf 在测试里独立实现扫描器算法摘要合同，避免被测实现自证。
func scannerAlgorithmOf(sources map[string]string) string {
	names := make([]string, 0, len(sources))
	for name := range sources {
		names = append(names, name)
	}
	sort.Strings(names)
	hash := sha256.New()
	for _, name := range names {
		_, _ = hash.Write([]byte(name))
		_, _ = hash.Write([]byte{0})
		_, _ = hash.Write([]byte(sources[name]))
		_, _ = hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil))
}

// newScannerHistoryFixture 依次构造四步历史：删除前状态、删除（单跳指向删除收据）、登记删除收据、
// 扫描器变更（单跳推进到后续收据）、登记后续收据；options 负责在其中一环故意破坏。
func newScannerHistoryFixture(t *testing.T, options scannerHistoryOptions) scannerHistoryFixture {
	t.Helper()
	root := t.TempDir()
	git := func(arguments ...string) string {
		t.Helper()
		raw, err := gitOutput(root, arguments...)
		if err != nil {
			t.Fatalf("构造扫描器历史仓库失败：git %v：%v", arguments, err)
		}
		return strings.TrimSpace(string(raw))
	}
	write := func(path string, raw []byte) {
		t.Helper()
		filename := filepath.Join(root, filepath.FromSlash(path))
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
	commit := func(message string) string {
		t.Helper()
		git("add", "-A")
		git("commit", "--quiet", "-m", message)
		return git("rev-parse", "HEAD")
	}
	pick := func(value, fallback string) string {
		if value != "" {
			return value
		}
		return fallback
	}
	git("init", "--quiet")
	git("config", "user.name", "冻结证明测试")
	git("config", "user.email", "freeze-proof@example.invalid")
	git("config", "commit.gpgsign", "false")
	git("config", "core.hooksPath", "/dev/null")

	path := "src/retired.go"
	previous := "package retired\n"
	bootstrap := strings.Repeat("a", 64)
	scannerV1 := map[string]string{"main.go": "package main\n", "removals.go": "package main\n// 第一版\n"}
	scannerV2 := map[string]string{"main.go": "package main\n// 第二版\n", "removals.go": "package main\n// 第一版\n"}

	// 提交一：删除前状态，含待删源码、Bootstrap 锁与第一版扫描器。
	write(path, []byte(previous))
	for name, content := range scannerV1 {
		write(scannerSourceDir+name, []byte(content))
	}
	writeJSON(scannerLockFile, map[string]any{"scanner_algorithm_sha256": bootstrap})
	first := commit("记录删除前状态")

	// 提交二：删除源码，同时把单跳文件写成指向删除收据（收据本身要晚一个提交才能入库）。
	git("rm", "--quiet", "--", path)
	writeJSON(scannerSuccessorFile, map[string]any{
		"schema_version": "official-egress-scanner-algorithm-successor/v1",
		"from_sha256":    pick(options.histFrom, bootstrap), "to_sha256": pick(options.histTo, scannerAlgorithmOf(scannerV1)),
		"source_transition": pick(options.histSource, historyDeletionReceiptPath),
		"reviewed_by":       "冻结证明测试", "reason": "删除当时的扫描器单跳",
	})
	deleted := commit("下线测试源码")

	deletion := map[string]any{
		"schema_version": freezeReceiptSchema, "mode": "commit", "base_commit": first, "current_commit": deleted,
		"result": "manual_actions_required",
		"required_manual_actions": []any{map[string]any{
			"action_kind": "single_hop_file", "rule_id": "scanner-algorithm-successor",
			"file": scannerSuccessorFile, "path": historyScannerPath,
		}},
		"deleted_frozen_paths": []any{path},
		"deletion_proof": map[string]any{
			"algorithm": "deletion-proof/v1", "reason": "经审核下线，扫描器待办随后完成。",
			"historical_readers": []any{},
			"deleted_paths": []any{map[string]any{
				"path": path, "last_sha256": digest([]byte(previous)), "frozen": true,
				"reference_scan": map[string]any{
					"algorithm": "reference-scan/v1", "patterns": []any{path, filepath.Base(path)},
					"references": []any{},
					"scopes":     []any{"python:import-and-attribute", "shell:invocation", "json:command-fields"},
				},
			}},
		},
	}
	deletionRaw := sealDeletionDocument(t, deletion)
	write(historyDeletionReceiptPath, deletionRaw)
	// 另一份区间不同的收据，用来构造“历史单跳指向别的收据”。
	otherReceipt := map[string]any{
		"schema_version": freezeReceiptSchema, "mode": "commit", "base_commit": first, "current_commit": first,
		"result": "passed",
	}
	write(historyOtherReceiptPath, sealDeletionDocument(t, otherReceipt))
	receiptCommit := commit("登记删除收据")

	// 扫描器推进到第二版，单跳文件改指向后续收据。
	for name, content := range scannerV2 {
		write(scannerSourceDir+name, []byte(content))
	}
	currentFrom := pick(options.curFrom, bootstrap)
	writeJSON(scannerLockFile, map[string]any{"scanner_algorithm_sha256": currentFrom})
	writeJSON(scannerSuccessorFile, map[string]any{
		"schema_version": "official-egress-scanner-algorithm-successor/v1",
		"from_sha256":    currentFrom, "to_sha256": pick(options.curTo, scannerAlgorithmOf(scannerV2)),
		"source_transition": historyNextReceiptPath,
		"reviewed_by":       "冻结证明测试", "reason": "删除之后的扫描器变更",
	})
	advanced := commit("扫描器变更")

	nextBase, nextCurrent := receiptCommit, advanced
	if options.nextBaseFirst {
		nextBase = first
	}
	if options.nextOffHead {
		tree := git("rev-parse", "HEAD^{tree}")
		nextCurrent = git("commit-tree", tree, "-p", receiptCommit, "-m", "不在 HEAD 历史上的提交")
	}
	next := map[string]any{
		"schema_version": freezeReceiptSchema, "mode": "commit", "base_commit": nextBase, "current_commit": nextCurrent,
		"result": "manual_actions_required",
	}
	nextRaw := sealDeletionDocument(t, next)
	if options.brokenNext {
		next["identity_sha256"] = strings.Repeat("0", 64)
		var err error
		if nextRaw, err = json.Marshal(next); err != nil {
			t.Fatal(err)
		}
	}
	write(historyNextReceiptPath, nextRaw)
	commit("登记后续收据")
	return scannerHistoryFixture{root: root, path: path, previous: digest([]byte(previous)), raw: deletionRaw}
}

func TestDeletedSourceAcceptsScannerActionAfterSingleHopAdvanced(t *testing.T) {
	fixture := newScannerHistoryFixture(t, scannerHistoryOptions{})
	if !deletionDocumentAccepted(fixture.root, fixture.raw, fixture.path, fixture.previous, nil) {
		t.Fatal("单跳已推进到后续合法收据后，旧删除证明被拒绝")
	}
	// 公开入口读取封存目录里的删除收据，结论必须一致。
	if !DeletedSourceAccepted(fixture.root, fixture.path, fixture.previous, nil) {
		t.Fatal("公开入口未接受单跳推进之后的旧删除收据")
	}
	// 逐条删除证明不因历史判据放松：未知摘要、未登记路径仍被拒绝。
	if deletionDocumentAccepted(fixture.root, fixture.raw, fixture.path, strings.Repeat("b", 64), nil) {
		t.Fatal("未知历史摘要被接受")
	}
	if deletionDocumentAccepted(fixture.root, fixture.raw, "src/unregistered.go", fixture.previous, nil) {
		t.Fatal("未登记的路径被接受")
	}
}

func TestDeletedSourceRejectsBrokenScannerHistory(t *testing.T) {
	other := strings.Repeat("c", 64)
	cases := []struct {
		name    string
		options scannerHistoryOptions
	}{
		{"历史单跳指向别的收据", scannerHistoryOptions{histSource: historyOtherReceiptPath}},
		{"历史单跳指向不存在的收据", scannerHistoryOptions{histSource: "docs/egress/maintenance/history-missing.json"}},
		{"历史单跳 from 与当时锁不符", scannerHistoryOptions{histFrom: other, curFrom: other}},
		{"历史单跳 to 与当时扫描器不符", scannerHistoryOptions{histTo: other}},
		{"当前单跳换了 bootstrap", scannerHistoryOptions{curFrom: other}},
		{"后续收据 base 不是后代", scannerHistoryOptions{nextBaseFirst: true}},
		{"后续收据 current 不在 HEAD 历史", scannerHistoryOptions{nextOffHead: true}},
		{"后续收据身份损坏", scannerHistoryOptions{brokenNext: true}},
		{"当前单跳 to 与当前扫描器不符", scannerHistoryOptions{curTo: other}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			fixture := newScannerHistoryFixture(t, test.options)
			if deletionDocumentAccepted(fixture.root, fixture.raw, fixture.path, fixture.previous, nil) {
				t.Fatal("扫描器历史不完整的删除证明被接受")
			}
		})
	}
	t.Run("浅克隆取不到历史对象", func(t *testing.T) {
		fixture := newScannerHistoryFixture(t, scannerHistoryOptions{})
		shallow := filepath.Join(t.TempDir(), "shallow")
		if _, err := gitOutput(fixture.root, "clone", "--quiet", "--depth", "1", "file://"+fixture.root, shallow); err != nil {
			t.Fatalf("构造浅克隆失败：%v", err)
		}
		if deletionDocumentAccepted(shallow, fixture.raw, fixture.path, fixture.previous, nil) {
			t.Fatal("取不到历史对象时删除证明未失败关闭")
		}
	})
	t.Run("删除收据提交在仓库中不存在", func(t *testing.T) {
		fixture := newScannerHistoryFixture(t, scannerHistoryOptions{})
		var receipt deletionReceipt
		if err := json.Unmarshal(fixture.raw, &receipt); err != nil {
			t.Fatal(err)
		}
		receipt.CurrentCommit = strings.Repeat("e", 40)
		if manualActionsAccepted(fixture.root, receipt) {
			t.Fatal("引用不存在的提交时扫描器待办被接受")
		}
	})
}

// 用真实仓库数据验证：10-23 Antigravity 保活下线收据的扫描器待办，在单跳文件推进之后依然被接受，
// 其逐条删除证明（路径、删除前摘要、引用扫描）仍然完整执行。
func TestDeletedSourceAcceptsRealKeeperRemovalAfterScannerAdvance(t *testing.T) {
	const root = "../../../.."
	const keeperReceipt = "docs/egress/maintenance/remove-antigravity-keeper-20261023-freeze-successor.json"
	raw, err := os.ReadFile(filepath.Join(root, keeperReceipt))
	if err != nil {
		t.Skipf("仓库中没有该收据：%v", err)
	}
	var receipt deletionReceipt
	if err := json.Unmarshal(raw, &receipt); err != nil {
		t.Fatal(err)
	}
	if !manualActionsAccepted(root, receipt) {
		t.Fatal("真实保活下线收据的扫描器待办未被接受（需要完整 Git 历史，CI 为 fetch-depth: 0）")
	}
	// 单跳必须确实已经离开这份收据，否则本用例没有覆盖历史判据。
	successorRaw, err := os.ReadFile(filepath.Join(root, scannerSuccessorFile))
	if err != nil {
		t.Fatal(err)
	}
	successor, ok := parseScannerSuccessor(successorRaw)
	if !ok {
		t.Fatal("当前扫描器单跳文件结构非法")
	}
	if successor.Source == keeperReceipt {
		t.Skip("单跳仍指向保活下线收据，历史判据暂无覆盖对象")
	}
	checked := 0
	for _, entry := range receipt.Proof.Entries {
		if !entry.Frozen || !strings.HasSuffix(entry.Path, ".go") || !missingWorktreePath(root, entry.Path) {
			continue
		}
		if !DeletedSourceAccepted(root, entry.Path, entry.LastSHA256, nil) {
			t.Fatalf("真实删除证明未被接受：%s", entry.Path)
		}
		// 摘要不是删除前摘要，且没有审计图承接，必须拒绝。
		if DeletedSourceAccepted(root, entry.Path, strings.Repeat("d", 64), nil) {
			t.Fatalf("未知摘要被接受：%s", entry.Path)
		}
		checked++
	}
	if checked == 0 {
		t.Fatal("保活下线收据里没有可核对的 Go 删除条目")
	}
}
