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

const (
	upstreamV0180SourceTransitionPath  = "docs/egress/maintenance/upstream-v0.1.180-source-transition.json"
	upstreamV0180SourceTransitionCount = 112
)

var (
	upstreamV0180SourceTransitionOnce    sync.Once
	upstreamV0180SourceTransitionCached  upstreamV0179SourceTransitionReceipt
	upstreamV0180SourceTransitionLoadErr error
)

func loadUpstreamV0180SourceTransition() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	upstreamV0180SourceTransitionOnce.Do(func() {
		upstreamV0180SourceTransitionCached,
			upstreamV0180SourceTransitionLoadErr = readUpstreamV0180SourceTransition()
	})
	return upstreamV0180SourceTransitionCached, upstreamV0180SourceTransitionLoadErr
}

func readUpstreamV0180SourceTransition() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	var receipt upstreamV0179SourceTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamV0180SourceTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游 v0.1.180 source transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil {
		return receipt, err
	}
	canonical = append(canonical, '\n')
	if upstreamMergeFrameworkDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游 v0.1.180 source transition 自摘要不一致")
	}
	if err := validateUpstreamV0180SourceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateUpstreamV0180SourceTransition(
	receipt upstreamV0179SourceTransitionReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v2" ||
		receipt.Date != "2026-08-25" ||
		receipt.BaseCommit != "2dbbe53a3c85cfae2737bc7de85328c42c9fa16e" ||
		receipt.PlanIdentitySHA256 != "0e3d358fed57a721cf5637dc1609d2a1c7e0adbf0d08230ff7ae6b66746ac954" ||
		receipt.UpstreamTag != "v0.1.180" ||
		receipt.UpstreamCommit != "c40edb4070a9274e8c23f161b4ed552051b14698" ||
		receipt.Classification != "implementation_and_validation" ||
		receipt.ActivationStatus != "accepted_not_activated" ||
		receipt.TargetClients["claude"] != "2.1.226" ||
		receipt.TargetClients["codex"] != "0.147.0" ||
		len(receipt.Transitions) != upstreamV0180SourceTransitionCount ||
		len(receipt.AllowedWireDeltas) != 0 || len(receipt.Verification) < 5 ||
		receipt.Result != "passed" {
		return errors.New("上游 v0.1.180 source transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return errors.New("上游 v0.1.180 source transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return errors.New("上游 v0.1.180 source transition 前序摘要非法")
			}
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		currentDigest := upstreamMergeFrameworkDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!codex01491MaintenanceTransitionSupersedes(
				transition.Path,
				transition.ToSHA256,
				currentDigest,
			)) {
			return errors.New("上游 v0.1.180 source transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 v0.1.180 source transition 路径未严格排序")
	}
	return nil
}

// upstreamV0180SourceTransitionSupersedes 只接受本次合并固定的
// path／predecessor／to 三元组，不改变历史收据或生产选择状态。
func upstreamV0180SourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491MaintenanceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadUpstreamV0180SourceTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && slices.Contains(transition.PredecessorSHA256s, priorDigest) &&
			(transition.ToSHA256 == currentDigest ||
				codex01491MaintenanceTransitionSupersedes(
					path,
					transition.ToSHA256,
					currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestUpstreamV0180SourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV0180SourceTransition(); err != nil {
		t.Fatal(err)
	}
}
