package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
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

const upstreamMergeFrameworkTransitionPath = "docs/egress/maintenance/upstream-merge-framework-v2-source-transition.json"

type upstreamMergeFrameworkTransitionEntry struct {
	Path               string   `json:"path"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type upstreamMergeFrameworkTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	BaseCommit    string `json:"base_commit"`
	Purpose       string `json:"purpose"`
	TargetClients struct {
		Claude string `json:"claude"`
		Codex  string `json:"codex"`
	} `json:"target_clients"`
	Transitions       []upstreamMergeFrameworkTransitionEntry `json:"transitions"`
	AllowedWireDeltas []string                                `json:"allowed_wire_deltas"`
	Verification      []string                                `json:"verification"`
	Result            string                                  `json:"result"`
	IdentitySHA256    string                                  `json:"identity_sha256"`
}

func upstreamMergeFrameworkDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

var (
	upstreamMergeFrameworkTransitionOnce    sync.Once
	upstreamMergeFrameworkTransitionCached  upstreamMergeFrameworkTransitionReceipt
	upstreamMergeFrameworkTransitionLoadErr error
)

func loadUpstreamMergeFrameworkTransition() (
	upstreamMergeFrameworkTransitionReceipt,
	error,
) {
	upstreamMergeFrameworkTransitionOnce.Do(func() {
		upstreamMergeFrameworkTransitionCached, upstreamMergeFrameworkTransitionLoadErr =
			readUpstreamMergeFrameworkTransition()
	})
	return upstreamMergeFrameworkTransitionCached, upstreamMergeFrameworkTransitionLoadErr
}

func readUpstreamMergeFrameworkTransition() (
	upstreamMergeFrameworkTransitionReceipt,
	error,
) {
	var receipt upstreamMergeFrameworkTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamMergeFrameworkTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游合并框架 transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游合并框架 transition 自摘要不一致")
	}
	if receipt.SchemaVersion != "official-egress-upstream-merge-framework-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "5d99bb5b44013ffe0dd21174842f30fe9453a99f" ||
		receipt.Purpose != "framework_prerequisite" ||
		receipt.TargetClients.Claude != "2.1.226" ||
		receipt.TargetClients.Codex != "0.147.0" ||
		len(receipt.Transitions) == 0 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) < 4 || receipt.Result != "passed" {
		return receipt, errors.New("上游合并框架 transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return receipt, errors.New("上游合并框架 transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return receipt, errors.New("上游合并框架 transition 前序摘要非法")
			}
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!compositeModelProtocolSourceTransitionSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			) &&
			!upstreamMergeRouteScopeTransitionSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			) && !upstreamV0179SourceTransitionSupersedes(
			transition.Path,
			transition.ToSHA256,
			currentDigest,
		)) {
			return receipt, errors.New("上游合并框架 transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("上游合并框架 transition 路径未严格排序")
	}
	return receipt, nil
}

// upstreamMergeFrameworkTransitionSupersedes 只接受前置变更集显式登记的
// path／前序摘要／当前摘要三元组，历史收据仍保持不可变。
func upstreamMergeFrameworkTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if compositeModelProtocolSourceTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if upstreamV0179SourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamMergeEgressSnapshotTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadUpstreamMergeFrameworkTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && slices.Contains(transition.PredecessorSHA256s, priorDigest) &&
			(transition.ToSHA256 == currentDigest ||
				compositeModelProtocolSourceTransitionSupersedes(
					path,
					transition.ToSHA256,
					currentDigest,
				) ||
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

func TestUpstreamMergeFrameworkTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamMergeFrameworkTransition(); err != nil {
		t.Fatal(err)
	}
}
