package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
)

const upstreamMergeRouteScopeTransitionPath = "docs/egress/maintenance/upstream-merge-route-scope-source-transition.json"

type upstreamMergeRouteScopeTransitionEntry struct {
	Path               string   `json:"path"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type upstreamMergeRouteScopeTransitionReceipt struct {
	SchemaVersion  string                                   `json:"schema_version"`
	Date           string                                   `json:"date"`
	BaseCommit     string                                   `json:"base_commit"`
	Purpose        string                                   `json:"purpose"`
	Transitions    []upstreamMergeRouteScopeTransitionEntry `json:"transitions"`
	Verification   []string                                 `json:"verification"`
	Result         string                                   `json:"result"`
	IdentitySHA256 string                                   `json:"identity_sha256"`
}

var (
	upstreamMergeRouteScopeTransitionOnce    sync.Once
	upstreamMergeRouteScopeTransitionCached  upstreamMergeRouteScopeTransitionReceipt
	upstreamMergeRouteScopeTransitionLoadErr error
)

func loadUpstreamMergeRouteScopeTransition() (
	upstreamMergeRouteScopeTransitionReceipt,
	error,
) {
	upstreamMergeRouteScopeTransitionOnce.Do(func() {
		upstreamMergeRouteScopeTransitionCached, upstreamMergeRouteScopeTransitionLoadErr =
			readUpstreamMergeRouteScopeTransition()
	})
	return upstreamMergeRouteScopeTransitionCached, upstreamMergeRouteScopeTransitionLoadErr
}

func readUpstreamMergeRouteScopeTransition() (
	upstreamMergeRouteScopeTransitionReceipt,
	error,
) {
	var receipt upstreamMergeRouteScopeTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamMergeRouteScopeTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游合并路由作用域 transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游合并路由作用域 transition 自摘要不一致")
	}
	if receipt.SchemaVersion != "official-egress-upstream-merge-route-scope-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "7d0d6c98f9f291aa96eeb1b968d2e0896f421be1" ||
		receipt.Purpose != "route_scope_identity_repair" ||
		len(receipt.Transitions) == 0 || len(receipt.Verification) < 3 ||
		receipt.Result != "passed" {
		return receipt, errors.New("上游合并路由作用域 transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return receipt, errors.New("上游合并路由作用域 transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return receipt, errors.New("上游合并路由作用域 transition 前序摘要非法")
			}
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		if readErr != nil || upstreamMergeFrameworkDigest(current) != transition.ToSHA256 {
			return receipt, errors.New("上游合并路由作用域 transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("上游合并路由作用域 transition 路径未严格排序")
	}
	return receipt, nil
}

// upstreamMergeRouteScopeTransitionSupersedes 只接受本次工具修复显式登记的
// path／前序摘要／当前摘要三元组，原框架收据保持不可变。
func upstreamMergeRouteScopeTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadUpstreamMergeRouteScopeTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest &&
			slices.Contains(transition.PredecessorSHA256s, priorDigest) {
			return true
		}
	}
	return false
}

func TestUpstreamMergeRouteScopeTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamMergeRouteScopeTransition(); err != nil {
		t.Fatal(err)
	}
}
