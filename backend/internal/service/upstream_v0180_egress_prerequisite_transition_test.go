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

const upstreamV0180EgressPrerequisiteServiceTransitionPath = "docs/egress/maintenance/upstream-v0.1.180-egress-prerequisite-transition.json"

type upstreamV0180EgressPrerequisiteServiceReceipt struct {
	SchemaVersion        string                                      `json:"schema_version"`
	IssuedAtUTC          string                                      `json:"issued_at_utc"`
	BaseCommit           string                                      `json:"base_commit"`
	Scope                string                                      `json:"scope"`
	TargetUpstreamTag    string                                      `json:"target_upstream_tag"`
	TargetUpstreamCommit string                                      `json:"target_upstream_commit"`
	Predecessor          compositeModelProtocolTransitionPredecessor `json:"predecessor"`
	Transitions          []compositeModelProtocolTransitionEntry     `json:"transitions"`
	Verification         []string                                    `json:"verification"`
	Safety               struct {
		ScannerAlgorithmChanged   bool `json:"scanner_algorithm_changed"`
		BootstrapSourceChanged    bool `json:"bootstrap_source_changed"`
		BootstrapInventoryChanged bool `json:"bootstrap_inventory_changed"`
		OAuthWireContractChanged  bool `json:"oauth_wire_contract_changed"`
		UpstreamMergePerformed    bool `json:"upstream_merge_performed"`
		LiveAccountUsed           bool `json:"live_account_used"`
		DeploymentPerformed       bool `json:"deployment_performed"`
	} `json:"safety"`
	Result         string `json:"result"`
	IdentitySHA256 string `json:"identity_sha256"`
}

var (
	upstreamV0180EgressPrerequisiteServiceOnce    sync.Once
	upstreamV0180EgressPrerequisiteServiceCached  upstreamV0180EgressPrerequisiteServiceReceipt
	upstreamV0180EgressPrerequisiteServiceLoadErr error
)

func loadUpstreamV0180EgressPrerequisiteServiceTransition() (
	upstreamV0180EgressPrerequisiteServiceReceipt,
	error,
) {
	upstreamV0180EgressPrerequisiteServiceOnce.Do(func() {
		upstreamV0180EgressPrerequisiteServiceCached,
			upstreamV0180EgressPrerequisiteServiceLoadErr =
			readUpstreamV0180EgressPrerequisiteServiceTransition()
	})
	return upstreamV0180EgressPrerequisiteServiceCached,
		upstreamV0180EgressPrerequisiteServiceLoadErr
}

func readUpstreamV0180EgressPrerequisiteServiceTransition() (
	upstreamV0180EgressPrerequisiteServiceReceipt,
	error,
) {
	var receipt upstreamV0180EgressPrerequisiteServiceReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(upstreamV0180EgressPrerequisiteServiceTransitionPath),
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
		return receipt, errors.New("上游 v0.1.180 egress 前置 transition 尾部存在额外 JSON")
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return receipt, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkServiceDigest(canonical) != receipt.IdentitySHA256 {
		return receipt, errors.New("上游 v0.1.180 egress 前置 transition 自摘要不一致")
	}
	if err := validateUpstreamV0180EgressPrerequisiteServiceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateUpstreamV0180EgressPrerequisiteServiceTransition(
	receipt upstreamV0180EgressPrerequisiteServiceReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-upstream-v0.1.180-prerequisite-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-24T15:29:13Z" ||
		receipt.BaseCommit != "cd13abbc8364a90ddeabddf86a68de7c6057dd2a" ||
		receipt.Scope != "upstream-v0.1.180-egress-lifecycle-prerequisite" ||
		receipt.TargetUpstreamTag != "v0.1.180" ||
		receipt.TargetUpstreamCommit != "c40edb4070a9274e8c23f161b4ed552051b14698" ||
		receipt.Result != "passed_local_prerequisite_gates" || len(receipt.Transitions) != 7 {
		return errors.New("上游 v0.1.180 egress 前置 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "composite_model_protocol_source_transition" ||
		receipt.Predecessor.Path != compositeModelProtocolSourceTransitionPath ||
		receipt.Predecessor.SHA256 != compositeModelProtocolSourceTransitionSHA {
		return errors.New("上游 v0.1.180 egress 前置 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(receipt.Predecessor.Path),
	))
	if err != nil || upstreamMergeFrameworkServiceDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("上游 v0.1.180 egress 前置 transition 前序摘要不一致")
	}
	if !slices.Equal(receipt.Verification, []string{
		"go test ./cmd/egressscan -count=1",
		"go test ./internal/officialegress/... -count=1",
		"EGRESS_SEAL_BASE_REF=cd13abbc8364a90ddeabddf86a68de7c6057dd2a make check-egress-spec-ci",
	}) {
		return errors.New("上游 v0.1.180 egress 前置 transition 验证集合非法")
	}
	if !receipt.Safety.ScannerAlgorithmChanged || receipt.Safety.BootstrapSourceChanged ||
		receipt.Safety.BootstrapInventoryChanged || receipt.Safety.OAuthWireContractChanged ||
		receipt.Safety.UpstreamMergePerformed || receipt.Safety.LiveAccountUsed ||
		receipt.Safety.DeploymentPerformed {
		return errors.New("上游 v0.1.180 egress 前置 transition 安全边界非法")
	}
	wantPaths := []string{
		"backend/cmd/egressscan/classify.go",
		"backend/cmd/egressscan/lifecycle_test.go",
		"backend/cmd/egressscan/main.go",
		"backend/internal/officialegress/composite_model_protocol_source_transition_test.go",
		"backend/internal/officialegress/upstream_merge_egress_snapshot_transition_test.go",
		"backend/internal/officialegress/upstream_v0180_egress_prerequisite_transition_test.go",
		"docs/egress/maintenance/bootstrap-inventory-lock.json",
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			!validUpstreamV0179ReleaseCIRepairServiceSHA(transition.FromSHA256) ||
			!validUpstreamV0179ReleaseCIRepairServiceSHA(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 || strings.TrimSpace(transition.Reason) == "" {
			return errors.New("上游 v0.1.180 egress 前置 transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.Equal(paths, wantPaths) {
		return errors.New("上游 v0.1.180 egress 前置 transition 路径闭集非法")
	}
	return nil
}

// upstreamV0180EgressPrerequisiteTransitionSupersedesService 接受前置收据的精确
// path/from/to，或经已冻结 snapshot 抵达该 from 的历史链；不扩大到业务源码和 wire。
func upstreamV0180EgressPrerequisiteTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491R15FormalClassificationSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if codex01491CandidateSourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadUpstreamV0180EgressPrerequisiteServiceTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		if transition.FromSHA256 == priorDigest ||
			upstreamMergeEgressSnapshotTransitionSupersedesService(
				path, priorDigest, transition.FromSHA256,
			) {
			return true
		}
	}
	return false
}

func TestUpstreamV0180EgressPrerequisiteServiceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV0180EgressPrerequisiteServiceTransition(); err != nil {
		t.Fatal(err)
	}
}
