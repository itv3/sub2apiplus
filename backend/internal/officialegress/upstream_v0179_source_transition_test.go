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

const upstreamV0179SourceTransitionPath = "docs/egress/maintenance/upstream-v0.1.179-source-transition.json"

type upstreamV0179SourceTransitionReceipt struct {
	SchemaVersion      string                                  `json:"schema_version"`
	Date               string                                  `json:"date"`
	BaseCommit         string                                  `json:"base_commit"`
	PlanIdentitySHA256 string                                  `json:"plan_identity_sha256"`
	UpstreamTag        string                                  `json:"upstream_tag"`
	UpstreamCommit     string                                  `json:"upstream_commit"`
	Classification     string                                  `json:"classification"`
	ActivationStatus   string                                  `json:"activation_status"`
	TargetClients      map[string]string                       `json:"target_clients"`
	Transitions        []upstreamMergeFrameworkTransitionEntry `json:"source_transitions"`
	AllowedWireDeltas  []string                                `json:"allowed_wire_deltas"`
	Verification       []string                                `json:"required_verification"`
	Result             string                                  `json:"result"`
	IdentitySHA256     string                                  `json:"identity_sha256"`
}

var (
	upstreamV0179SourceTransitionOnce    sync.Once
	upstreamV0179SourceTransitionCached  upstreamV0179SourceTransitionReceipt
	upstreamV0179SourceTransitionLoadErr error
)

func loadUpstreamV0179SourceTransition() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	upstreamV0179SourceTransitionOnce.Do(func() {
		upstreamV0179SourceTransitionCached,
			upstreamV0179SourceTransitionLoadErr = readUpstreamV0179SourceTransition()
	})
	return upstreamV0179SourceTransitionCached, upstreamV0179SourceTransitionLoadErr
}

func readUpstreamV0179SourceTransition() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	var receipt upstreamV0179SourceTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamV0179SourceTransitionPath))
	if err != nil {
		return receipt, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游 v0.1.179 source transition 尾部存在额外 JSON")
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
		return receipt, errors.New("上游 v0.1.179 source transition 自摘要不一致")
	}
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v2" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "3743f5f62d8cf40bdccc8f8add717cda809ce767" ||
		receipt.PlanIdentitySHA256 != "c2eed1886c938bc1d3dbe23d0b810e865ecbc7b2197ca378810cb553c1be87ef" ||
		receipt.UpstreamTag != "v0.1.179" ||
		receipt.UpstreamCommit != "75f88be5f75c27771836b586f7de1503afa0e3bc" ||
		receipt.Classification != "implementation_and_validation" ||
		receipt.ActivationStatus != "accepted_not_activated" ||
		receipt.TargetClients["claude"] != "2.1.226" ||
		receipt.TargetClients["codex"] != "0.147.0" ||
		len(receipt.Transitions) != 60 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) < 5 || receipt.Result != "passed" {
		return receipt, errors.New("上游 v0.1.179 source transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return receipt, errors.New("上游 v0.1.179 source transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return receipt, errors.New("上游 v0.1.179 source transition 前序摘要非法")
			}
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		if readErr != nil {
			return receipt, errors.New("上游 v0.1.179 source transition 当前源码不可读：" + transition.Path)
		}
		currentDigest := upstreamMergeFrameworkDigest(current)
		if currentDigest != transition.ToSHA256 &&
			!upstreamV0179ReleaseCIRepairTransitionSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) && !upstreamV0180SourceTransitionSupersedes(
			transition.Path, transition.ToSHA256, currentDigest,
		) {
			return receipt, errors.New("上游 v0.1.179 source transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return receipt, errors.New("上游 v0.1.179 source transition 路径未严格排序")
	}
	return receipt, nil
}

// upstreamV0179SourceTransitionSupersedes 只消费本次上游合并固定的
// path／predecessor／to 三元组；历史 transition 原文和既有选择权保持不变。
func upstreamV0179SourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if upstreamV0180SourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadUpstreamV0179SourceTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path ||
			!slices.Contains(transition.PredecessorSHA256s, priorDigest) {
			continue
		}
		if transition.ToSHA256 == currentDigest ||
			upstreamV0179ReleaseCIRepairTransitionSupersedes(
				path, transition.ToSHA256, currentDigest,
			) || upstreamV0180SourceTransitionSupersedes(
			path, transition.ToSHA256, currentDigest,
		) {
			return true
		}
	}
	return false
}

func TestUpstreamV0179SourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV0179SourceTransition(); err != nil {
		t.Fatal(err)
	}
}
