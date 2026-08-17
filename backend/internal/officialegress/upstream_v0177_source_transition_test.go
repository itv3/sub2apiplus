package officialegress

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	upstreamV0177SourceTransitionSHA256      = "ecb95c725ac58b3fa270eeb413e5887f83a13692c153fe0da50e820d34046ec2"
	runtimeReliabilityRepairTransitionSHA256 = "8f8a2d5d0d763bf72834eac0441d4baeb76442fec9fb415b7f338da90bcc13f2"
)

type upstreamV0177SourceTransitionReceipt struct {
	SchemaVersion    string `json:"schema_version"`
	Date             string `json:"date"`
	BaseCommit       string `json:"base_commit"`
	UpstreamTag      string `json:"upstream_tag"`
	UpstreamCommit   string `json:"upstream_commit"`
	Classification   string `json:"classification"`
	ActivationStatus string `json:"activation_status"`
	FrozenRelease    struct {
		ActiveVersion         string `json:"active_version"`
		ActiveProfileSHA256   string `json:"active_profile_sha256"`
		PreviousVersion       string `json:"previous_version"`
		PreviousProfileSHA256 string `json:"previous_profile_sha256"`
		ReleaseGraphSHA256    string `json:"release_graph_sha256"`
	} `json:"frozen_release_state"`
	SourceTransitions []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	AllowedWireDeltas    []string `json:"allowed_wire_deltas"`
	RequiredVerification []string `json:"required_verification"`
	Result               string   `json:"result"`
}

func TestUpstreamV0177SourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := loadUpstreamV0177SourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if got := sha256Hex(raw); got != upstreamV0177SourceTransitionSHA256 {
		t.Fatalf("v0.1.177 上游源码 transition 摘要漂移：got=%s want=%s", got, upstreamV0177SourceTransitionSHA256)
	}
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v1" ||
		receipt.Date != "2026-08-16" ||
		receipt.BaseCommit != "234fdf8d53734f16ae84e45470bc91583d473684" ||
		receipt.UpstreamTag != "v0.1.177" ||
		receipt.UpstreamCommit != "073e92d17178a1ccdb0a27017f572f10c9c7ab62" ||
		receipt.Classification != "validation_only" ||
		receipt.ActivationStatus != "accepted_not_activated" ||
		receipt.FrozenRelease.ActiveVersion != "0.147.0" ||
		receipt.FrozenRelease.ActiveProfileSHA256 != "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc" ||
		receipt.FrozenRelease.PreviousVersion != "0.145.0" ||
		receipt.FrozenRelease.PreviousProfileSHA256 != "e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b" ||
		receipt.FrozenRelease.ReleaseGraphSHA256 != "d8a7634c9af52159c912b539df74b9fa3b92664c0b90cc0defd6789cd07c846d" ||
		len(receipt.SourceTransitions) != 12 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.RequiredVerification) != 5 || receipt.Result != "passed" {
		t.Fatalf("v0.1.177 上游源码 transition 顶层事实非法：%+v", receipt)
	}

	paths := make([]string, 0, len(receipt.SourceTransitions))
	for _, transition := range receipt.SourceTransitions {
		if strings.TrimSpace(transition.Path) == "" || strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" || strings.TrimSpace(transition.Reason) == "" {
			t.Fatalf("v0.1.177 上游源码 transition 条目不完整：%+v", transition)
		}
		paths = append(paths, transition.Path)
		source, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := sha256Hex(source); got != transition.ToSHA256 &&
			!runtimeReliabilityRepairTransitionSupersedes(transition.Path, transition.ToSHA256, got) {
			t.Fatalf("v0.1.177 上游源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		t.Fatalf("v0.1.177 上游源码 transition 路径未严格排序或存在重复：%v", paths)
	}
}

func TestChangeset3ReferenceCompatibilityHelpersRemainReachable(t *testing.T) {
	// 这些函数属于已冻结的迁移参考实现；保留显式消费者，避免普通维护变更
	// 误把它们当作可直接退休的死代码。
	_ = changeset3ReferenceSyntheticHeaders
	_ = changeset3ReferenceReleasePurpose
	if !officialCodexGeneratedHeaderForReference("host") ||
		officialCodexGeneratedHeaderForReference("authorization") {
		t.Fatal("迁移参考实现的生成头分类发生漂移")
	}
}

func upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if runtimeReliabilityRepairTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, raw, err := loadUpstreamV0177SourceTransition()
	if err != nil || sha256Hex(raw) != upstreamV0177SourceTransitionSHA256 {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest {
			if transition.ToSHA256 == currentDigest ||
				runtimeReliabilityRepairTransitionSupersedes(path, transition.ToSHA256, currentDigest) {
				return true
			}
		}
	}
	return false
}

// runtimeReliabilityRepairTransitionSupersedes 只接受本次可靠性修复收据中精确的
// path/from/to 承接关系，使既有冻结证据保持不可变。
func runtimeReliabilityRepairTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	receipt, raw, err := loadRuntimeReliabilityRepairTransition()
	if err != nil || sha256Hex(raw) != runtimeReliabilityRepairTransitionSHA256 {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

type runtimeReliabilityRepairTransitionReceipt struct {
	SchemaVersion         string                            `json:"schema_version"`
	Date                  string                            `json:"date"`
	ReleaseTag            string                            `json:"release_tag"`
	BaseCommit            string                            `json:"base_commit"`
	PriorTransition       string                            `json:"prior_transition"`
	PriorTransitionSHA256 string                            `json:"prior_transition_sha256"`
	Transitions           []changeset4SourceTransitionEntry `json:"transitions"`
	Result                string                            `json:"result"`
}

func TestRuntimeReliabilityRepairSourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := loadRuntimeReliabilityRepairTransition()
	if err != nil {
		t.Fatal(err)
	}
	if got := sha256Hex(raw); got != runtimeReliabilityRepairTransitionSHA256 {
		t.Fatalf("运行时可靠性修复 source transition 摘要漂移：got=%s want=%s", got, runtimeReliabilityRepairTransitionSHA256)
	}
	if receipt.SchemaVersion != "official-egress-runtime-reliability-repair-source-transition/v1" ||
		receipt.Date != "2026-08-17" || receipt.ReleaseTag != "v0.1.177-2" ||
		strings.TrimSpace(receipt.BaseCommit) == "" ||
		receipt.PriorTransition != "docs/egress/maintenance/upstream-v0.1.177-source-transition.json" ||
		receipt.PriorTransitionSHA256 != upstreamV0177SourceTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 5 {
		t.Fatalf("运行时可靠性修复 source transition 顶层事实非法：%+v", receipt)
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" || strings.TrimSpace(transition.Reason) == "" {
			t.Fatalf("运行时可靠性修复 source transition 条目不完整：%+v", transition)
		}
		paths = append(paths, transition.Path)
		source, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := sha256Hex(source); got != transition.ToSHA256 {
			t.Fatalf("运行时可靠性修复源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		t.Fatalf("运行时可靠性修复 source transition 路径未严格排序或存在重复：%v", paths)
	}
}

func loadRuntimeReliabilityRepairTransition() (runtimeReliabilityRepairTransitionReceipt, []byte, error) {
	var receipt runtimeReliabilityRepairTransitionReceipt
	raw, err := os.ReadFile("../../../docs/egress/maintenance/runtime-reliability-repair-source-transition.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return receipt, nil, err
	}
	return receipt, raw, nil
}

func loadUpstreamV0177SourceTransition() (upstreamV0177SourceTransitionReceipt, []byte, error) {
	var receipt upstreamV0177SourceTransitionReceipt
	raw, err := os.ReadFile("../../../docs/egress/maintenance/upstream-v0.1.177-source-transition.json")
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return receipt, nil, err
	}
	return receipt, raw, nil
}
