package service

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
	upstreamMergeEgressSnapshotServicePath = "docs/egress/maintenance/upstream-merge-egress-snapshot-source-transition.json"
	upstreamV0179SourceTransitionPath      = "docs/egress/maintenance/upstream-v0.1.179-source-transition.json"
)

type upstreamV0179SourceTransitionReceipt struct {
	SchemaVersion      string                                    `json:"schema_version"`
	Date               string                                    `json:"date"`
	BaseCommit         string                                    `json:"base_commit"`
	PlanIdentitySHA256 string                                    `json:"plan_identity_sha256"`
	UpstreamTag        string                                    `json:"upstream_tag"`
	UpstreamCommit     string                                    `json:"upstream_commit"`
	Classification     string                                    `json:"classification"`
	ActivationStatus   string                                    `json:"activation_status"`
	TargetClients      map[string]string                         `json:"target_clients"`
	Transitions        []upstreamMergeFrameworkServiceTransition `json:"source_transitions"`
	AllowedWireDeltas  []string                                  `json:"allowed_wire_deltas"`
	Verification       []string                                  `json:"required_verification"`
	Result             string                                    `json:"result"`
	IdentitySHA256     string                                    `json:"identity_sha256"`
}

var (
	upstreamMergeEgressSnapshotServiceOnce    sync.Once
	upstreamMergeEgressSnapshotServiceCached  upstreamMergeFrameworkServiceReceipt
	upstreamMergeEgressSnapshotServiceLoadErr error
	upstreamV0179SourceTransitionOnce         sync.Once
	upstreamV0179SourceTransitionCached       upstreamV0179SourceTransitionReceipt
	upstreamV0179SourceTransitionLoadErr      error
)

func loadUpstreamMergeEgressSnapshotServiceReceipt() (
	upstreamMergeFrameworkServiceReceipt,
	error,
) {
	upstreamMergeEgressSnapshotServiceOnce.Do(func() {
		upstreamMergeEgressSnapshotServiceCached,
			upstreamMergeEgressSnapshotServiceLoadErr =
			readUpstreamMergeEgressSnapshotServiceReceipt()
	})
	return upstreamMergeEgressSnapshotServiceCached,
		upstreamMergeEgressSnapshotServiceLoadErr
}

func readUpstreamMergeEgressSnapshotServiceReceipt() (
	upstreamMergeFrameworkServiceReceipt,
	error,
) {
	var receipt upstreamMergeFrameworkServiceReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamMergeEgressSnapshotServicePath))
	if err != nil {
		return receipt, err
	}
	if err := decodeStrictUpstreamSourceTransition(raw, &receipt); err != nil {
		return receipt, err
	}
	if err := validateUpstreamSourceTransitionIdentity(raw, receipt.IdentitySHA256, false); err != nil {
		return receipt, err
	}
	if receipt.SchemaVersion != "official-egress-upstream-merge-egress-snapshot-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "ce9deb3fabe0b99bf1c6d3be1c3ff472007ef9ac" ||
		receipt.Purpose != "egress_snapshot_migration_receipt_repair" ||
		len(receipt.Transitions) == 0 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) < 4 || receipt.Result != "passed" {
		return receipt, errors.New("上游合并 egress snapshot transition 顶层事实非法")
	}
	if err := validateUpstreamServiceTransitionEntries(receipt.Transitions, false); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadUpstreamV0179SourceTransitionServiceReceipt() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	upstreamV0179SourceTransitionOnce.Do(func() {
		upstreamV0179SourceTransitionCached,
			upstreamV0179SourceTransitionLoadErr =
			readUpstreamV0179SourceTransitionServiceReceipt()
	})
	return upstreamV0179SourceTransitionCached, upstreamV0179SourceTransitionLoadErr
}

func readUpstreamV0179SourceTransitionServiceReceipt() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	var receipt upstreamV0179SourceTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamV0179SourceTransitionPath))
	if err != nil {
		return receipt, err
	}
	if err := decodeStrictUpstreamSourceTransition(raw, &receipt); err != nil {
		return receipt, err
	}
	if err := validateUpstreamSourceTransitionIdentity(raw, receipt.IdentitySHA256, true); err != nil {
		return receipt, err
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
	if err := validateUpstreamServiceTransitionEntries(receipt.Transitions, true); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func decodeStrictUpstreamSourceTransition(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("上游 source transition 尾部存在额外 JSON")
	}
	return nil
}

func validateUpstreamSourceTransitionIdentity(
	raw []byte,
	expected string,
	withTrailingNewline bool,
) error {
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil {
		return err
	}
	if withTrailingNewline {
		canonical = append(canonical, '\n')
	}
	if upstreamMergeFrameworkServiceDigest(canonical) != expected {
		return errors.New("上游 source transition 自摘要不一致")
	}
	return nil
}

func validateUpstreamServiceTransitionEntries(
	transitions []upstreamMergeFrameworkServiceTransition,
	checkCurrent bool,
) error {
	paths := make([]string, 0, len(transitions))
	for _, transition := range transitions {
		if strings.TrimSpace(transition.Path) == "" || len(transition.ToSHA256) != 64 ||
			strings.TrimSpace(transition.Reason) == "" || len(transition.PredecessorSHA256s) == 0 ||
			!slices.IsSorted(transition.PredecessorSHA256s) ||
			len(transition.PredecessorSHA256s) != len(slices.Compact(
				append([]string(nil), transition.PredecessorSHA256s...),
			)) {
			return errors.New("上游 source transition 条目非法")
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if len(predecessor) != 64 || predecessor == transition.ToSHA256 {
				return errors.New("上游 source transition 前序摘要非法")
			}
		}
		if checkCurrent {
			current, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
			if err != nil || upstreamMergeFrameworkServiceDigest(current) != transition.ToSHA256 {
				return errors.New("上游 source transition 当前摘要不一致：" + transition.Path)
			}
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 source transition 路径未严格排序")
	}
	return nil
}

// upstreamMergeEgressSnapshotTransitionSupersedesService 只接受已固定
// snapshot 收据的精确路径、前序与当前摘要，并通过 FW-H 追加收据
// 承接其直接前序，不修改任何历史事实。
func upstreamMergeEgressSnapshotTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadUpstreamMergeEgressSnapshotServiceReceipt()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if predecessor == priorDigest ||
				claudeFWHSourceTransitionSupersedesService(
					path, priorDigest, predecessor,
				) || upstreamV0177SourceTransitionSupersedes(
				path, priorDigest, predecessor,
			) {
				return true
			}
		}
	}
	return false
}

// upstreamV0179SourceTransitionSupersedesService 仅消费本次上游合并固定的
// path／predecessor／to 三元组。当前收据作为最新单向后继，可以通过已经
// 完整校验的历史链抵达 predecessor；历史收据原文保持不变。
func upstreamV0179SourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadUpstreamV0179SourceTransitionServiceReceipt()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		for _, predecessor := range transition.PredecessorSHA256s {
			if predecessor == priorDigest ||
				upstreamMergeEgressSnapshotTransitionSupersedesService(
					path, priorDigest, predecessor,
				) || upstreamMergeFrameworkTransitionSupersedesService(
				path, priorDigest, predecessor,
			) || claudeFWHSourceTransitionSupersedesService(
				path, priorDigest, predecessor,
			) || claudeFWGSourceTransitionSupersedesService(
				path, priorDigest, predecessor,
			) || claudeFWGTestTransitionSupersedesService(
				path, priorDigest, predecessor,
			) || upstreamV0177SourceTransitionSupersedes(
				path, priorDigest, predecessor,
			) {
				return true
			}
		}
	}
	return false
}

func TestUpstreamV0179SourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamMergeEgressSnapshotServiceReceipt(); err != nil {
		t.Fatal(err)
	}
	if _, err := loadUpstreamV0179SourceTransitionServiceReceipt(); err != nil {
		t.Fatal(err)
	}
}
