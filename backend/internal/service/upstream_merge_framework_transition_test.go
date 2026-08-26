package service

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
	"sync"
	"testing"
)

type upstreamMergeFrameworkServiceTransition struct {
	Path               string   `json:"path"`
	PredecessorSHA256s []string `json:"predecessor_sha256s"`
	ToSHA256           string   `json:"to_sha256"`
	Reason             string   `json:"reason"`
}

type upstreamMergeFrameworkServiceReceipt struct {
	SchemaVersion     string                                    `json:"schema_version"`
	Date              string                                    `json:"date"`
	BaseCommit        string                                    `json:"base_commit"`
	Purpose           string                                    `json:"purpose"`
	TargetClients     map[string]string                         `json:"target_clients"`
	Transitions       []upstreamMergeFrameworkServiceTransition `json:"transitions"`
	AllowedWireDeltas []string                                  `json:"allowed_wire_deltas"`
	Verification      []string                                  `json:"verification"`
	Result            string                                    `json:"result"`
	IdentitySHA256    string                                    `json:"identity_sha256"`
}

func upstreamMergeFrameworkServiceDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

var (
	upstreamMergeFrameworkServiceOnce    sync.Once
	upstreamMergeFrameworkServiceCached  upstreamMergeFrameworkServiceReceipt
	upstreamMergeFrameworkServiceLoadErr error
)

func loadUpstreamMergeFrameworkServiceReceipt() (
	upstreamMergeFrameworkServiceReceipt,
	error,
) {
	upstreamMergeFrameworkServiceOnce.Do(func() {
		upstreamMergeFrameworkServiceCached, upstreamMergeFrameworkServiceLoadErr =
			readUpstreamMergeFrameworkServiceReceipt()
	})
	return upstreamMergeFrameworkServiceCached, upstreamMergeFrameworkServiceLoadErr
}

func readUpstreamMergeFrameworkServiceReceipt() (
	upstreamMergeFrameworkServiceReceipt,
	error,
) {
	var receipt upstreamMergeFrameworkServiceReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", "docs/egress/maintenance/upstream-merge-framework-v2-source-transition.json",
	))
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
	if err != nil || upstreamMergeFrameworkServiceDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游合并框架 transition 自摘要不一致")
	}
	if receipt.SchemaVersion != "official-egress-upstream-merge-framework-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "5d99bb5b44013ffe0dd21174842f30fe9453a99f" ||
		receipt.Purpose != "framework_prerequisite" ||
		receipt.TargetClients["claude"] != "2.1.226" ||
		receipt.TargetClients["codex"] != "0.147.0" ||
		len(receipt.Transitions) == 0 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) < 4 || receipt.Result != "passed" {
		return receipt, errors.New("上游合并框架 transition 顶层事实非法")
	}
	return receipt, nil
}

func upstreamMergeFrameworkTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491CandidateSourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if compositeModelProtocolSourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if upstreamMergeEgressSnapshotTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) || upstreamV0179SourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadUpstreamMergeFrameworkServiceReceipt()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path &&
			slices.Contains(transition.PredecessorSHA256s, priorDigest) &&
			(transition.ToSHA256 == currentDigest ||
				compositeModelProtocolSourceTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				) ||
				upstreamV0179SourceTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				)) {
			return true
		}
	}
	return false
}

func TestUpstreamMergeFrameworkServiceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamMergeFrameworkServiceReceipt(); err != nil {
		t.Fatal(err)
	}
}
