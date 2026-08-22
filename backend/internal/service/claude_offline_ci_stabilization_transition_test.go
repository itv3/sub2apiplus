package service

import (
	"encoding/json"
	"os"
)

const (
	claudeOfflineCIStabilizationServicePath = "../../../docs/egress/maintenance/claude-offline-ci-stabilization-transition.json"
	claudeOfflineCIStabilizationServiceSHA  = "f86194ed0167485c2ff91efca6b4b834ab5c000b3768a35ad9924f84818b47d5"
)

type claudeOfflineCIStabilizationServiceReceipt struct {
	SchemaVersion string                                 `json:"schema_version"`
	IssuedAtUTC   string                                 `json:"issued_at_utc"`
	BaseCommit    string                                 `json:"base_commit"`
	Scope         string                                 `json:"scope"`
	Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
	Safety        struct {
		LiveAccountUsed           bool   `json:"live_account_used"`
		OnlineAcceptancePerformed bool   `json:"online_acceptance_performed"`
		ReleaseReadinessChanged   bool   `json:"release_readiness_changed"`
		ReleaseState              string `json:"release_state"`
		VircsAccessed             bool   `json:"vircs_accessed"`
		VircsChanged              bool   `json:"vircs_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

// claudeOfflineCIStabilizationTransitionSupersedesService 只消费由
// officialegress 测试包完整验证的固定收据，并只接受精确 path/from/to。
func claudeOfflineCIStabilizationTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	raw, err := os.ReadFile(claudeOfflineCIStabilizationServicePath)
	if err != nil || compatibilityClosureDigest(raw) != claudeOfflineCIStabilizationServiceSHA {
		return false
	}
	var receipt claudeOfflineCIStabilizationServiceReceipt
	if json.Unmarshal(raw, &receipt) != nil ||
		receipt.SchemaVersion !=
			"official-egress-claude-offline-ci-stabilization-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T16:05:00Z" ||
		receipt.BaseCommit != "7a0c4bdc199bfda81e3cdd64c4ad4237c4ffe5d9" ||
		receipt.Scope != "claude-offline-ci-stabilization" ||
		receipt.Result != "passed_offline_ci_stabilization" ||
		len(receipt.Transitions) != 27 {
		return false
	}
	safety := receipt.Safety
	if safety.LiveAccountUsed || safety.OnlineAcceptancePerformed ||
		safety.ReleaseReadinessChanged || safety.VircsAccessed || safety.VircsChanged ||
		safety.ReleaseState != "candidate_deployed/not_ready_for_operator_release" {
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
