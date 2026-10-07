package service

import (
	"regexp"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// Claude 候选激活事实（升级控制面 B4-06a）：FW-G 候选 Runtime 生效时，事实按候选
// Runtime 填写画像链摘要，profile_mode 为 validation_candidate，批准摘要恒为空。

var claudeActivationDigestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func storeClaudeActivationRuntime(t *testing.T, runtimeState *OfficialEgressTransitionRuntime) {
	t.Helper()
	previous := processOfficialEgressRuntime.Load()
	processOfficialEgressRuntime.Store(runtimeState)
	t.Cleanup(func() { processOfficialEgressRuntime.Store(previous) })
}

func TestClaudeCandidateActivationFactReportsCandidateRuntime(t *testing.T) {
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, &claudeFWGServiceUpstream{})
	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "legacy"
	storeClaudeActivationRuntime(t, runtimeState)
	candidate := runtimeState.ClaudeCandidate

	fact, err := resolveClaudeOfficialEgressActivationFact(
		cfg, time.Unix(1791374400, 0), activationEnv(map[string]string{
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST": candidate.ProfileDigest(),
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST": candidate.ReleaseDigest(),
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_BUILD_ID":       "build-candidate",
		}),
	)
	require.NoError(t, err)
	require.Equal(t, "claude-egress-activation-fact/v1", fact.SchemaVersion)
	require.Equal(t, "claude_profile_observed", fact.EventType)
	require.Equal(t, "validation_candidate", fact.ProfileMode)
	require.Equal(t, candidate.ReleaseVersion(), fact.ClaudeVersion)
	require.NotEmpty(t, fact.ClaudeVersion)
	for name, digest := range map[string]string{
		"profile": fact.ProfileDigest, "wire": fact.WireDigest,
		"release": fact.ReleaseDigest, "bundle": fact.BundleDigest,
	} {
		require.Regexp(t, claudeActivationDigestPattern, digest, name)
	}
	require.Equal(t, candidate.WireDigest(), fact.WireDigest)
	require.Equal(t, candidate.BundleDigest(), fact.BundleDigest)
	require.Empty(t, fact.ApprovalDigest, "候选 Runtime 没有生产批准")
	require.Equal(t, "build-candidate", fact.BuildID)
	require.Equal(t, claudeOfficialEgressActivationEventID(fact), fact.EventID)
	require.Len(t, fact.EventID, 64)
}

func TestClaudeCandidateActivationFactFailsClosed(t *testing.T) {
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, &claudeFWGServiceUpstream{})
	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "legacy"
	storeClaudeActivationRuntime(t, runtimeState)
	for name, env := range map[string]map[string]string{
		"声明批准摘要": {
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_APPROVAL_DIGEST": "5555555555555555555555555555555555555555555555555555555555555555",
		},
		"profile 摘要漂移": {
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST": "0000000000000000000000000000000000000000000000000000000000000000",
		},
		"release 摘要漂移": {
			"GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST": "0000000000000000000000000000000000000000000000000000000000000000",
		},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := resolveClaudeOfficialEgressActivationFact(cfg, time.Unix(1791374400, 0), activationEnv(env))
			require.Error(t, err)
		})
	}

	// 候选开关打开，但进程里装配的是正式 Runtime（没有候选 Runtime）：拒绝落盘。
	productionState, _ := newClaudeFWHServiceRuntime(t)
	storeClaudeActivationRuntime(t, productionState)
	_, err := resolveClaudeOfficialEgressActivationFact(cfg, time.Unix(1791374400, 0), activationEnv(nil))
	require.ErrorContains(t, err, "候选 Runtime 未生效")
}

func TestClaudeLegacyActivationFactWithoutCandidateUnchanged(t *testing.T) {
	runtimeState, cfg := newClaudeFWGServiceRuntime(t, &claudeFWGServiceUpstream{})
	storeClaudeActivationRuntime(t, runtimeState)
	cfg.Gateway.ClaudeFWGCandidateEnabled = false
	cfg.Gateway.ClaudeOfficialClientProfiles.Mode = "legacy"
	fact, err := resolveClaudeOfficialEgressActivationFact(cfg, time.Unix(1791374400, 0), activationEnv(nil))
	require.NoError(t, err)
	require.Equal(t, "legacy", fact.ProfileMode)
	for _, value := range []string{
		fact.ClaudeVersion, fact.ProfileDigest, fact.WireDigest,
		fact.ReleaseDigest, fact.BundleDigest, fact.ApprovalDigest,
	} {
		require.Empty(t, value)
	}
}

// TestClaudeCandidateActivationEventIDMatchesPythonAudit 与 Python 运行审计
// （tools/official_client_capture/tests/test_claude_build_runtime_audit.py 的
// CANDIDATE_FACT）共用同一份样例：两边按各自的序列化复算出同一 event_id。
func TestClaudeCandidateActivationEventIDMatchesPythonAudit(t *testing.T) {
	fact := ClaudeOfficialEgressActivationFact{
		SchemaVersion:    "claude-egress-activation-fact/v1",
		Source:           "sub2api-runtime",
		EventType:        "claude_profile_observed",
		ObservedAtUTC:    "2026-10-07T12:00:00Z",
		ProfileMode:      "validation_candidate",
		ClaudeVersion:    "2.1.285",
		ProfileDigest:    "1111111111111111111111111111111111111111111111111111111111111111",
		WireDigest:       "2222222222222222222222222222222222222222222222222222222222222222",
		ReleaseDigest:    "3333333333333333333333333333333333333333333333333333333333333333",
		BundleDigest:     "4444444444444444444444444444444444444444444444444444444444444444",
		ApprovalDigest:   "",
		ImageID:          "sha256:6666666666666666666666666666666666666666666666666666666666666666",
		ImageReference:   "ghcr.io/itv3/sub2apiplus@sha256:6666666666666666666666666666666666666666666666666666666666666666",
		SourceTreeSHA256: "7777777777777777777777777777777777777777777777777777777777777777",
		BuildID:          "build-candidate-20261007",
		DeployedVersion:  "0.2.13-1-candidate",
	}
	require.Equal(t,
		"97c0648ed625ac16968f356be8d938058bcdcc58c675edcfca4a5f123971a3da",
		claudeOfficialEgressActivationEventID(fact),
	)
}
