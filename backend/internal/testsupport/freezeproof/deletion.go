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
		if action.Kind != "single_hop_file" || action.File != "docs/egress/maintenance/scanner-algorithm-successor.json" ||
			action.Rule != "scanner-algorithm-successor" || !safePath(action.Path) ||
			!strings.HasPrefix(action.Path, "backend/cmd/egressscan/") || !strings.HasSuffix(action.Path, ".go") {
			return false
		}
	}
	raw, err := os.ReadFile(filepath.Join(root, "docs/egress/maintenance/scanner-algorithm-successor.json"))
	var successor struct {
		Schema   string `json:"schema_version"`
		From     string `json:"from_sha256"`
		To       string `json:"to_sha256"`
		Source   string `json:"source_transition"`
		Reviewer string `json:"reviewed_by"`
		Reason   string `json:"reason"`
	}
	if err != nil || json.Unmarshal(raw, &successor) != nil || successor.Schema != "official-egress-scanner-algorithm-successor/v1" ||
		!digestValid(successor.From) || !digestValid(successor.To) || !safePath(successor.Source) ||
		!strings.HasPrefix(successor.Source, "docs/egress/maintenance/") || strings.TrimSpace(successor.Reviewer) == "" || strings.TrimSpace(successor.Reason) == "" {
		return false
	}
	lockRaw, err := os.ReadFile(filepath.Join(root, "docs/egress/maintenance/bootstrap-inventory-lock.json"))
	var lock struct {
		Algorithm string `json:"scanner_algorithm_sha256"`
	}
	if err != nil || json.Unmarshal(lockRaw, &lock) != nil || lock.Algorithm != successor.From {
		return false
	}
	sourceRaw, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(successor.Source)))
	var source deletionReceipt
	if err != nil || json.Unmarshal(sourceRaw, &source) != nil || !identityValid(sourceRaw) || source.BaseCommit != receipt.BaseCommit || source.CurrentCommit != receipt.CurrentCommit {
		return false
	}
	// 与扫描器自己的算法摘要合同一致：按文件名排序，只覆盖非测试 Go 源码。
	files, err := filepath.Glob(filepath.Join(root, "backend/cmd/egressscan/*.go"))
	if err != nil || len(files) == 0 {
		return false
	}
	hash := sha256.New()
	for _, file := range files {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		content, err := os.ReadFile(file)
		if err != nil {
			return false
		}
		_, _ = hash.Write([]byte(filepath.Base(file)))
		_, _ = hash.Write([]byte{0})
		_, _ = hash.Write(content)
		_, _ = hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil)) == successor.To
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
