package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
)

const (
	upstreamV0177SourceTransitionSHA256       = "ecb95c725ac58b3fa270eeb413e5887f83a13692c153fe0da50e820d34046ec2"
	runtimeReliabilityRepairTransitionSHA256  = "8f8a2d5d0d763bf72834eac0441d4baeb76442fec9fb415b7f338da90bcc13f2"
	multiPersonaControlSourceTransitionSHA256 = "139f4844085942b709a68b61d2b51f863189ada780d361ace91cad8a6ae86bb2"
	multiPersonaControlSourceV2SHA256         = "354d32c903a14cd287c1a9fc9269f20af1e307b589947bbc29a8d883b0963872"
)

// upstreamV0177SourceTransitionSupersedes 验证历史摘要是否由本次上游合并的
// 固定 transition 精确承接。旧退休收据保持不可变，路径和摘要均不得模糊匹配。
func upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if codex01491CandidateSourceTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if upstreamMergeEgressSnapshotTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if upstreamV0179SourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGTestTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if fwEObservationSourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if fwEPrior, ok := fwEObservationSourceTransitionPrior(path, currentDigest); ok {
		return upstreamV0177SourceTransitionBeforeFWE(path, priorDigest, fwEPrior)
	}
	return upstreamV0177SourceTransitionBeforeFWE(path, priorDigest, currentDigest)
}

func upstreamV0177SourceTransitionBeforeFWE(path, priorDigest, currentDigest string) bool {
	if multiPersonaControlSourceTransitionV2Supersedes(path, priorDigest, currentDigest) {
		return true
	}
	if runtimeReliabilityRepairTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	raw, err := os.ReadFile("../../../docs/egress/maintenance/upstream-v0.1.177-source-transition.json")
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != upstreamV0177SourceTransitionSHA256 {
		return false
	}
	var receipt struct {
		SchemaVersion     string `json:"schema_version"`
		SourceTransitions []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
		} `json:"source_transitions"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil ||
		receipt.SchemaVersion != "official-egress-upstream-source-transition/v1" {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest {
			if transition.ToSHA256 == currentDigest ||
				runtimeReliabilityRepairTransitionSupersedes(path, transition.ToSHA256, currentDigest) ||
				upstreamV0179SourceTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				) {
				return true
			}
		}
	}
	return false
}

// runtimeReliabilityRepairTransitionSupersedes 验证本次可靠性修复收据中的精确
// path/from/to 承接关系，旧收据原文保持不变。
func runtimeReliabilityRepairTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if claudeFWGTestTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGSourceTransitionSupersedesService(path, priorDigest, currentDigest) {
		return true
	}
	if fwEObservationSourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if multiPersonaControlSourceTransitionV2Supersedes(path, priorDigest, currentDigest) {
		return true
	}
	raw, err := os.ReadFile("../../../docs/egress/maintenance/runtime-reliability-repair-source-transition.json")
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != runtimeReliabilityRepairTransitionSHA256 {
		return false
	}
	var receipt struct {
		SchemaVersion string `json:"schema_version"`
		Transitions   []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
		} `json:"transitions"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil ||
		receipt.SchemaVersion != "official-egress-runtime-reliability-repair-source-transition/v1" {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest {
			return transition.ToSHA256 == currentDigest ||
				multiPersonaControlSourceTransitionV2Supersedes(
					path, transition.ToSHA256, currentDigest,
				)
		}
	}
	return false
}

func multiPersonaControlSourceTransitionV2Supersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/multi-persona-control-source-transition-v2.json",
	)
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != multiPersonaControlSourceV2SHA256 {
		return false
	}
	var receipt struct {
		SchemaVersion         string `json:"schema_version"`
		PriorTransition       string `json:"prior_transition"`
		PriorTransitionSHA256 string `json:"prior_transition_sha256"`
		AdditionalPrior       []struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"additional_prior_transitions"`
		Transitions []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
			Reason     string `json:"reason"`
		} `json:"transitions"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil ||
		receipt.SchemaVersion != "official-egress-multi-persona-control-source-transition/v2" ||
		receipt.PriorTransition !=
			"docs/egress/maintenance/multi-persona-control-source-transition.json" ||
		receipt.PriorTransitionSHA256 != multiPersonaControlSourceTransitionSHA256 ||
		len(receipt.AdditionalPrior) != 1 ||
		receipt.AdditionalPrior[0].Path !=
			"docs/egress/maintenance/runtime-reliability-repair-source-transition.json" ||
		receipt.AdditionalPrior[0].SHA256 != runtimeReliabilityRepairTransitionSHA256 ||
		receipt.Result != "passed" || len(receipt.Transitions) != 3 {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest && transition.Reason != "" {
			return true
		}
	}
	return false
}
