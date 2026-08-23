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

const upstreamMergeEgressSnapshotTransitionPath = "docs/egress/maintenance/upstream-merge-egress-snapshot-source-transition.json"

type upstreamMergeEgressSnapshotTransitionEntry struct {
	Path               string   `json:"path"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type upstreamMergeEgressSnapshotTransitionReceipt struct {
	SchemaVersion     string                                       `json:"schema_version"`
	Date              string                                       `json:"date"`
	BaseCommit        string                                       `json:"base_commit"`
	Purpose           string                                       `json:"purpose"`
	Transitions       []upstreamMergeEgressSnapshotTransitionEntry `json:"transitions"`
	AllowedWireDeltas []string                                     `json:"allowed_wire_deltas"`
	Verification      []string                                     `json:"verification"`
	Result            string                                       `json:"result"`
	IdentitySHA256    string                                       `json:"identity_sha256"`
}

var (
	upstreamMergeEgressSnapshotTransitionOnce    sync.Once
	upstreamMergeEgressSnapshotTransitionCached  upstreamMergeEgressSnapshotTransitionReceipt
	upstreamMergeEgressSnapshotTransitionLoadErr error
)

func loadUpstreamMergeEgressSnapshotTransition() (
	upstreamMergeEgressSnapshotTransitionReceipt,
	error,
) {
	upstreamMergeEgressSnapshotTransitionOnce.Do(func() {
		upstreamMergeEgressSnapshotTransitionCached,
			upstreamMergeEgressSnapshotTransitionLoadErr =
			readUpstreamMergeEgressSnapshotTransition()
	})
	return upstreamMergeEgressSnapshotTransitionCached,
		upstreamMergeEgressSnapshotTransitionLoadErr
}

func readUpstreamMergeEgressSnapshotTransition() (
	upstreamMergeEgressSnapshotTransitionReceipt,
	error,
) {
	var receipt upstreamMergeEgressSnapshotTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamMergeEgressSnapshotTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游合并 egress snapshot transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游合并 egress snapshot transition 自摘要不一致")
	}
	if receipt.SchemaVersion != "official-egress-upstream-merge-egress-snapshot-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "ce9deb3fabe0b99bf1c6d3be1c3ff472007ef9ac" ||
		receipt.Purpose != "egress_snapshot_migration_receipt_repair" ||
		len(receipt.Transitions) == 0 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) < 4 || receipt.Result != "passed" {
		return receipt, errors.New("上游合并 egress snapshot transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return receipt, errors.New("上游合并 egress snapshot transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return receipt, errors.New("上游合并 egress snapshot transition 前序摘要非法")
			}
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!upstreamV0179SourceTransitionSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			)) {
			return receipt, errors.New("上游合并 egress snapshot transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("上游合并 egress snapshot transition 路径未严格排序")
	}
	return receipt, nil
}

// upstreamMergeEgressSnapshotTransitionSupersedes 只接受本次工具修复显式登记的
// path／前序摘要／当前摘要三元组，既有框架和路由作用域收据保持不可变。
func upstreamMergeEgressSnapshotTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadUpstreamMergeEgressSnapshotTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && slices.Contains(transition.PredecessorSHA256s, priorDigest) &&
			(transition.ToSHA256 == currentDigest ||
				upstreamV0179SourceTransitionSupersedes(
					path,
					transition.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestUpstreamMergeEgressSnapshotTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamMergeEgressSnapshotTransition(); err != nil {
		t.Fatal(err)
	}
}
