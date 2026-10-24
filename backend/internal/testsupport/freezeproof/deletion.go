// Package freezeproof 为历史冻结测试复核带证明的源码删除，不参与应用运行路径。
package freezeproof

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
)

type deletionEntry struct {
	Path       string `json:"path"`
	LastSHA256 string `json:"last_sha256"`
	Frozen     bool   `json:"frozen"`
	Scan       struct {
		Algorithm  string            `json:"algorithm"`
		Patterns   []string          `json:"patterns"`
		Scopes     []string          `json:"scopes"`
		References []json.RawMessage `json:"references"`
	} `json:"reference_scan"`
}

type deletionReceipt struct {
	SchemaVersion string   `json:"schema_version"`
	BaseCommit    string   `json:"base_commit"`
	CurrentCommit string   `json:"current_commit"`
	Mode          string   `json:"mode"`
	Result        string   `json:"result"`
	FrozenPaths   []string `json:"deleted_frozen_paths"`
	Proof         struct {
		Algorithm string          `json:"algorithm"`
		Reason    string          `json:"reason"`
		Entries   []deletionEntry `json:"deleted_paths"`
		Readers   []struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"historical_readers"`
	} `json:"deletion_proof"`
	ManualActions []struct {
		Kind string `json:"action_kind"`
		File string `json:"file"`
		Path string `json:"path"`
		Rule string `json:"rule_id"`
	} `json:"required_manual_actions"`
}

var deletionDocuments sync.Map

// DeletedSourceAccepted 只接受已封存提交中的精确删除事实。历史摘要必须相等，
// 或由调用方提供的只读审计图明确承接到删除前摘要；缺文件本身不构成授权。
func DeletedSourceAccepted(root, path, priorDigest string, reaches func(string, string, string) bool) bool {
	if !safePath(path) || !digestValid(priorDigest) || !missingWorktreePath(root, path) {
		return false
	}
	key, err := filepath.Abs(root)
	if err != nil {
		return false
	}
	stored, loaded := deletionDocuments.Load(key)
	if !loaded {
		var documents [][]byte
		files, _ := filepath.Glob(filepath.Join(root, "docs/egress/maintenance/*.json"))
		for _, filename := range files {
			raw, err := os.ReadFile(filename)
			if err != nil {
				continue
			}
			var probe struct {
				Schema string          `json:"schema_version"`
				Proof  json.RawMessage `json:"deletion_proof"`
			}
			if json.Unmarshal(raw, &probe) == nil && probe.Schema == "official-egress-upstream-freeze-successor/v1" && len(probe.Proof) > 0 {
				documents = append(documents, raw)
			}
		}
		stored, _ = deletionDocuments.LoadOrStore(key, documents)
	}
	documents, ok := stored.([][]byte)
	if !ok {
		return false
	}
	for _, raw := range documents {
		if deletionDocumentAccepted(root, raw, path, priorDigest, reaches) {
			return true
		}
	}
	return false
}

func deletionDocumentAccepted(root string, raw []byte, path, priorDigest string, reaches func(string, string, string) bool) bool {
	var receipt deletionReceipt
	if json.Unmarshal(raw, &receipt) != nil || !identityValid(raw) ||
		receipt.SchemaVersion != "official-egress-upstream-freeze-successor/v1" || receipt.Mode != "commit" ||
		!commitValid(receipt.BaseCommit) || !commitValid(receipt.CurrentCommit) || receipt.BaseCommit == receipt.CurrentCommit ||
		receipt.Proof.Algorithm != "deletion-proof/v1" || strings.TrimSpace(receipt.Proof.Reason) == "" ||
		!safePath(path) || !digestValid(priorDigest) || !missingWorktreePath(root, path) ||
		!slices.Contains(receipt.FrozenPaths, path) || !manualActionsAccepted(root, receipt) {
		return false
	}
	var selected *deletionEntry
	seen := make(map[string]bool)
	frozen := make([]string, 0)
	hasPythonDeletion := false
	for index := range receipt.Proof.Entries {
		entry := &receipt.Proof.Entries[index]
		if !safePath(entry.Path) || !digestValid(entry.LastSHA256) || seen[entry.Path] ||
			entry.Scan.Algorithm != "reference-scan/v1" || entry.Scan.References == nil || len(entry.Scan.References) != 0 ||
			!slices.Contains(entry.Scan.Patterns, entry.Path) || !slices.Contains(entry.Scan.Patterns, filepath.Base(entry.Path)) ||
			!slices.Equal(entry.Scan.Scopes, []string{"python:import-and-attribute", "shell:invocation", "json:command-fields"}) {
			return false
		}
		seen[entry.Path] = true
		hasPythonDeletion = hasPythonDeletion || strings.HasSuffix(entry.Path, ".py")
		if entry.Frozen {
			frozen = append(frozen, entry.Path)
		}
		if entry.Path == path && entry.Frozen {
			selected = entry
		}
	}
	slices.Sort(frozen)
	if selected == nil || !slices.Equal(frozen, receipt.FrozenPaths) ||
		(priorDigest != selected.LastSHA256 && (reaches == nil || !reaches(path, priorDigest, selected.LastSHA256))) ||
		!gitSucceeds(root, "merge-base", "--is-ancestor", receipt.BaseCommit, receipt.CurrentCommit) ||
		!gitSucceeds(root, "merge-base", "--is-ancestor", receipt.CurrentCommit, "HEAD") {
		return false
	}
	before, err := gitOutput(root, "show", receipt.BaseCommit+":"+path)
	if err != nil || digest(before) != selected.LastSHA256 ||
		!missingAtCommit(root, receipt.CurrentCommit, path) || !missingAtCommit(root, "HEAD", path) {
		return false
	}
	if hasPythonDeletion && len(receipt.Proof.Readers) == 0 {
		return false
	}
	for _, reader := range receipt.Proof.Readers {
		if !safePath(reader.Path) || !strings.HasSuffix(reader.Path, ".py") || seen[reader.Path] || !digestValid(reader.SHA256) {
			return false
		}
		atCommit, err := gitOutput(root, "show", receipt.CurrentCommit+":"+reader.Path)
		current, readErr := os.ReadFile(filepath.Join(root, filepath.FromSlash(reader.Path)))
		if err != nil || digest(atCommit) != reader.SHA256 || readErr != nil ||
			(digest(current) != reader.SHA256 && (reaches == nil || !reaches(reader.Path, reader.SHA256, digest(current)))) {
			return false
		}
	}
	return true
}

func identityValid(raw []byte) bool {
	var document map[string]any
	if json.Unmarshal(raw, &document) != nil || document == nil {
		return false
	}
	expected, _ := document["identity_sha256"].(string)
	delete(document, "identity_sha256")
	// Python 的收据合同保留中文以及 <、>、&，因此不能使用默认 HTML 转义。
	var canonical bytes.Buffer
	encoder := json.NewEncoder(&canonical)
	encoder.SetEscapeHTML(false)
	err := encoder.Encode(document)
	return err == nil && digestValid(expected) && digest(bytes.TrimSuffix(canonical.Bytes(), []byte("\n"))) == expected
}

func manualActionsAccepted(root string, receipt deletionReceipt) bool {
	if receipt.Result == "passed_with_deletions" {
		return len(receipt.ManualActions) == 0
	}
	// 人工待办只允许与删除证明无关、且实际已经完成的扫描器单跳承接。
	if receipt.Result != "manual_actions_required" || len(receipt.ManualActions) == 0 {
		return false
	}
	for _, action := range receipt.ManualActions {
		if action.Kind != "single_hop_file" || action.File != scannerSuccessorFile ||
			action.Rule != "scanner-algorithm-successor" || !safePath(action.Path) ||
			!strings.HasPrefix(action.Path, scannerSourceDir) || !strings.HasSuffix(action.Path, ".go") {
			return false
		}
	}
	raw, err := os.ReadFile(filepath.Join(root, scannerSuccessorFile))
	if err != nil {
		return false
	}
	current, ok := parseScannerSuccessor(raw)
	if !ok {
		return false
	}
	lockRaw, err := os.ReadFile(filepath.Join(root, scannerLockFile))
	if err != nil {
		return false
	}
	if lock, ok := lockAlgorithmDigest(lockRaw); !ok || lock != current.From {
		return false
	}
	// 当前单跳必须仍与工作树里的扫描器源码一致，待办名相同不等于已完成。
	if digest, ok := scannerDigestInWorktree(root); !ok || digest != current.To {
		return false
	}
	source, ok := readIdentityReceipt(root, current.Source)
	if !ok {
		return false
	}
	// 判据一：单跳仍直接指向这份删除收据，即删除之后扫描器没有再变过。
	if source.BaseCommit == receipt.BaseCommit && source.CurrentCommit == receipt.CurrentCommit {
		return true
	}
	// 判据二：扫描器每次变更都必须把同一个单跳文件推进到新收据，所以单跳早晚会离开删除收据。
	// 此时改按 Git 历史核对“删除当时它确实指向这份收据”，并确认后续推进来自同一 Bootstrap。
	return historicalSingleHopAccepted(root, receipt, current, source)
}

// 扫描器算法单跳文件、Bootstrap 锁文件与扫描器源码目录的仓库内路径，以及封存收据的架构名。
const (
	scannerSuccessorFile = "docs/egress/maintenance/scanner-algorithm-successor.json"
	scannerLockFile      = "docs/egress/maintenance/bootstrap-inventory-lock.json"
	scannerSourceDir     = "backend/cmd/egressscan/"
	freezeReceiptSchema  = "official-egress-upstream-freeze-successor/v1"
)

// scannerSuccessor 是扫描器算法单跳文件的结构；同一文件任何时刻只能表达“Bootstrap 摘要 -> 当前摘要”这一跳。
type scannerSuccessor struct {
	Schema   string `json:"schema_version"`
	From     string `json:"from_sha256"`
	To       string `json:"to_sha256"`
	Source   string `json:"source_transition"`
	Reviewer string `json:"reviewed_by"`
	Reason   string `json:"reason"`
}

// parseScannerSuccessor 只做结构校验，不判断它与哪份收据绑定。
func parseScannerSuccessor(raw []byte) (scannerSuccessor, bool) {
	var successor scannerSuccessor
	if json.Unmarshal(raw, &successor) != nil || successor.Schema != "official-egress-scanner-algorithm-successor/v1" ||
		!digestValid(successor.From) || !digestValid(successor.To) || !safePath(successor.Source) ||
		!strings.HasPrefix(successor.Source, "docs/egress/maintenance/") ||
		strings.TrimSpace(successor.Reviewer) == "" || strings.TrimSpace(successor.Reason) == "" {
		return scannerSuccessor{}, false
	}
	return successor, true
}

// lockAlgorithmDigest 从 Bootstrap 锁文件原文取出扫描器算法摘要。
func lockAlgorithmDigest(raw []byte) (string, bool) {
	var lock struct {
		Algorithm string `json:"scanner_algorithm_sha256"`
	}
	if json.Unmarshal(raw, &lock) != nil || !digestValid(lock.Algorithm) {
		return "", false
	}
	return lock.Algorithm, true
}

// readIdentityReceipt 读取单跳指向的收据文件，要求身份摘要自洽并能解析出提交区间。
func readIdentityReceipt(root, relative string) (deletionReceipt, bool) {
	raw, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(relative)))
	var source deletionReceipt
	if err != nil || json.Unmarshal(raw, &source) != nil || !identityValid(raw) {
		return deletionReceipt{}, false
	}
	return source, true
}

// scannerDigestOfFiles 与扫描器自己的算法摘要合同一致：按文件名排序，只覆盖非测试 Go 源码，
// 每个文件依次写入“文件名、0 字节、内容、0 字节”。
func scannerDigestOfFiles(names []string, read func(string) ([]byte, error)) (string, bool) {
	hash := sha256.New()
	count := 0
	for _, name := range names {
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		content, err := read(name)
		if err != nil {
			return "", false
		}
		_, _ = hash.Write([]byte(name))
		_, _ = hash.Write([]byte{0})
		_, _ = hash.Write(content)
		_, _ = hash.Write([]byte{0})
		count++
	}
	return hex.EncodeToString(hash.Sum(nil)), count > 0
}

// scannerDigestInWorktree 计算工作树里当前扫描器源码的算法摘要。
func scannerDigestInWorktree(root string) (string, bool) {
	files, err := filepath.Glob(filepath.Join(root, scannerSourceDir+"*.go"))
	if err != nil || len(files) == 0 {
		return "", false
	}
	names := make([]string, 0, len(files))
	for _, file := range files {
		names = append(names, filepath.Base(file))
	}
	return scannerDigestOfFiles(names, func(name string) ([]byte, error) {
		return os.ReadFile(filepath.Join(root, scannerSourceDir, name))
	})
}

// scannerDigestAtCommit 按同一合同计算某个历史提交上的扫描器源码摘要；Git 取不到对象即判失败。
func scannerDigestAtCommit(root, commit string) (string, bool) {
	listing, err := gitOutput(root, "ls-tree", "-z", commit, "--", scannerSourceDir)
	if err != nil {
		return "", false
	}
	var names []string
	for _, entry := range bytes.Split(listing, []byte{0}) {
		if len(entry) == 0 {
			continue
		}
		// 每项形如“<mode> blob <sha>TAB<path>”，只接受目录下直接的普通文件，拒绝符号链接与子模块。
		meta, path, found := strings.Cut(string(entry), "\t")
		fields := strings.Fields(meta)
		if !found || len(fields) != 3 || fields[1] != "blob" || (fields[0] != "100644" && fields[0] != "100755") ||
			!strings.HasPrefix(path, scannerSourceDir) || strings.Contains(strings.TrimPrefix(path, scannerSourceDir), "/") {
			continue
		}
		names = append(names, strings.TrimPrefix(path, scannerSourceDir))
	}
	slices.Sort(names)
	return scannerDigestOfFiles(names, func(name string) ([]byte, error) {
		return gitOutput(root, "show", commit+":"+scannerSourceDir+name)
	})
}

// historicalSingleHopAccepted 在当前单跳已推进到后续收据时，证明删除收据对应的扫描器待办在当时已经完成：
//  1. 删除收据的 current_commit 上，单跳文件结构合法，from 等于当时 Bootstrap 锁的算法摘要，
//     to 等于当时的扫描器源码摘要，并指向一份提交区间与删除收据完全一致的收据；
//  2. 当前单跳与历史单跳从同一个 Bootstrap 摘要出发；
//  3. 当前单跳指向的后续收据同样是合法的封存收据，其 base 提交是删除收据 current_commit 的后代，
//     且整个区间都在当前 HEAD 的历史之内。
//
// 任何 Git 命令失败（对象缺失、浅克隆）都判不接受，不会放松。
func historicalSingleHopAccepted(root string, receipt deletionReceipt, current scannerSuccessor, next deletionReceipt) bool {
	if !commitValid(receipt.BaseCommit) || !commitValid(receipt.CurrentCommit) ||
		next.SchemaVersion != freezeReceiptSchema || next.Mode != "commit" ||
		!commitValid(next.BaseCommit) || !commitValid(next.CurrentCommit) || next.BaseCommit == next.CurrentCommit {
		return false
	}
	historicalRaw, err := gitOutput(root, "show", receipt.CurrentCommit+":"+scannerSuccessorFile)
	if err != nil {
		return false
	}
	historical, ok := parseScannerSuccessor(historicalRaw)
	if !ok || historical.From != current.From {
		return false
	}
	historicalLock, err := gitOutput(root, "show", receipt.CurrentCommit+":"+scannerLockFile)
	if err != nil {
		return false
	}
	if lock, ok := lockAlgorithmDigest(historicalLock); !ok || lock != historical.From {
		return false
	}
	if digest, ok := scannerDigestAtCommit(root, receipt.CurrentCommit); !ok || digest != historical.To {
		return false
	}
	// 历史单跳指向的收据从当前工作树读取，其提交区间必须与删除收据一致。
	bound, ok := readIdentityReceipt(root, historical.Source)
	if !ok || bound.BaseCommit != receipt.BaseCommit || bound.CurrentCommit != receipt.CurrentCommit {
		return false
	}
	return gitSucceeds(root, "merge-base", "--is-ancestor", receipt.CurrentCommit, next.BaseCommit) &&
		gitSucceeds(root, "merge-base", "--is-ancestor", next.BaseCommit, next.CurrentCommit) &&
		gitSucceeds(root, "merge-base", "--is-ancestor", next.CurrentCommit, "HEAD")
}

func safePath(path string) bool {
	return path != "" && path != "." && !strings.ContainsAny(path, "\\\x00") &&
		!filepath.IsAbs(path) && filepath.ToSlash(filepath.Clean(path)) == path &&
		!strings.HasPrefix(path, "../") && path != ".."
}

func missingWorktreePath(root, path string) bool {
	if !safePath(path) {
		return false
	}
	current := root
	for _, part := range strings.Split(path, "/") {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if errors.Is(err, os.ErrNotExist) {
			return true
		}
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			return false
		}
	}
	return false
}

func missingAtCommit(root, commit, path string) bool {
	raw, err := gitOutput(root, "ls-tree", "-z", commit, "--", path)
	return err == nil && len(raw) == 0
}

func gitOutput(root string, arguments ...string) ([]byte, error) {
	command := exec.Command("git", arguments...)
	command.Dir = root
	return command.Output()
}

func gitSucceeds(root string, arguments ...string) bool {
	_, err := gitOutput(root, arguments...)
	return err == nil
}

func commitValid(value string) bool {
	return len(value) == 40 && strings.Trim(value, "0123456789abcdef") == ""
}
func digestValid(value string) bool {
	return len(value) == 64 && strings.Trim(value, "0123456789abcdef") == ""
}
func digest(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }
