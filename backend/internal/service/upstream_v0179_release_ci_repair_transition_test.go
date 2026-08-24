package service

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	upstreamV0179ReleaseCIRepairTransitionServicePath = "docs/egress/maintenance/upstream-v0.1.179-release-ci-repair-transition.json"
	upstreamV0179ReleaseCIRepairTransitionServiceSHA  = "e51a96c4cd1b3fc7eca44372c6d2c02556070b387be69ba3482dfd1839e3020b"
	upstreamV0179SourceTransitionServiceFileSHA       = "52a0ab6bde326bc0cbedd87f8623daa035ed1c6438f94cb2b4ae74bfb3787121"
)

type upstreamV0179ReleaseCIRepairServicePredecessor struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type upstreamV0179ReleaseCIRepairServiceTransition struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type upstreamV0179ReleaseCIRepairServiceSafety struct {
	LiveAccountUsed            bool `json:"live_account_used"`
	OnlineAcceptancePerformed  bool `json:"online_acceptance_performed"`
	ReleaseReadinessChanged    bool `json:"release_readiness_changed"`
	UpstreamTagsPushedToOrigin bool `json:"upstream_tags_pushed_to_origin"`
}

type upstreamV0179ReleaseCIRepairServiceReceipt struct {
	SchemaVersion string                                          `json:"schema_version"`
	IssuedAtUTC   string                                          `json:"issued_at_utc"`
	BaseCommit    string                                          `json:"base_commit"`
	Scope         string                                          `json:"scope"`
	Predecessor   upstreamV0179ReleaseCIRepairServicePredecessor  `json:"predecessor"`
	Transitions   []upstreamV0179ReleaseCIRepairServiceTransition `json:"transitions"`
	Verification  []string                                        `json:"verification"`
	Safety        upstreamV0179ReleaseCIRepairServiceSafety       `json:"safety"`
	Result        string                                          `json:"result"`
}

func loadUpstreamV0179ReleaseCIRepairTransitionService() (
	upstreamV0179ReleaseCIRepairServiceReceipt,
	error,
) {
	var receipt upstreamV0179ReleaseCIRepairServiceReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(upstreamV0179ReleaseCIRepairTransitionServicePath),
	))
	if err != nil {
		return receipt, err
	}
	if upstreamMergeFrameworkServiceDigest(raw) != upstreamV0179ReleaseCIRepairTransitionServiceSHA {
		return receipt, errors.New("上游 v0.1.179 发版 CI 修复 transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("上游 v0.1.179 发版 CI 修复 transition 尾部存在额外 JSON")
	}
	if err := validateUpstreamV0179ReleaseCIRepairTransitionService(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validUpstreamV0179ReleaseCIRepairServiceSHA(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validateUpstreamV0179ReleaseCIRepairTransitionService(
	receipt upstreamV0179ReleaseCIRepairServiceReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-upstream-v0.1.179-release-ci-repair-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-24T00:13:42Z" ||
		receipt.BaseCommit != "f6e7bd478a8c5bd1418a8fdb8baa32b33e210a56" ||
		receipt.Scope != "upstream-v0.1.179-release-ci-repair" ||
		receipt.Result != "passed_release_ci_repair" || len(receipt.Transitions) != 4 {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 顶层事实非法")
	}
	if receipt.Predecessor.Kind != "upstream_source_transition" ||
		receipt.Predecessor.Path != upstreamV0179SourceTransitionPath ||
		receipt.Predecessor.SHA256 != upstreamV0179SourceTransitionServiceFileSHA {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 前序非法")
	}
	predecessorRaw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(receipt.Predecessor.Path),
	))
	if err != nil || upstreamMergeFrameworkServiceDigest(predecessorRaw) != receipt.Predecessor.SHA256 {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 前序摘要不一致")
	}
	if !slices.Equal(receipt.Verification, []string{
		"go test -tags=unit ./internal/service -run '^TestBuildCountTokensRequest_StripsCacheControlOnlyFromLiteralDeferredTools$' -count=1",
		"go test -tags=unit ./...",
		"EGRESS_SEAL_BASE_REF=f6e7bd478a8c5bd1418a8fdb8baa32b33e210a56 make check-egress-spec-ci",
	}) {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 验证集合非法")
	}
	if receipt.Safety.LiveAccountUsed || receipt.Safety.OnlineAcceptancePerformed ||
		receipt.Safety.ReleaseReadinessChanged || receipt.Safety.UpstreamTagsPushedToOrigin {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 安全边界非法")
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			!validUpstreamV0179ReleaseCIRepairServiceSHA(transition.FromSHA256) ||
			!validUpstreamV0179ReleaseCIRepairServiceSHA(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 || strings.TrimSpace(transition.Reason) == "" {
			return errors.New("上游 v0.1.179 发版 CI 修复 transition 条目非法")
		}
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		if readErr != nil || upstreamMergeFrameworkServiceDigest(current) != transition.ToSHA256 {
			return errors.New("上游 v0.1.179 发版 CI 修复 transition 当前摘要不一致：" + transition.Path)
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 v0.1.179 发版 CI 修复 transition 路径未严格排序")
	}
	return nil
}

// upstreamV0179ReleaseCIRepairTransitionSupersedesService 只承认本次发版修复
// 收据中的精确 path/from/to，不扩大发版修复对运行时身份或 wire 的影响范围。
func upstreamV0179ReleaseCIRepairTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadUpstreamV0179ReleaseCIRepairTransitionService()
	if err != nil {
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

func TestUpstreamV0179ReleaseCIRepairTransitionServiceIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV0179ReleaseCIRepairTransitionService(); err != nil {
		t.Fatal(err)
	}
}
