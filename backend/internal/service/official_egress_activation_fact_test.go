package service

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func activationConfig(mode string) *config.Config {
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = mode
	return cfg
}

func activationEnv(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func TestActivationFactReportsRuntimeResolvedProfile(t *testing.T) {
	for _, mode := range []string{"active", "previous"} {
		t.Run(mode, func(t *testing.T) {
			release, err := officialegress.DefaultReleaseCatalog().Resolve(
				officialegress.ReleaseMode(mode),
			)
			require.NoError(t, err)

			fact, err := resolveOfficialEgressActivationFact(
				activationConfig(mode),
				time.Unix(1765000000, 0),
				activationEnv(nil),
			)
			require.NoError(t, err)
			require.Equal(t, "codex-egress-activation-fact/v1", fact.SchemaVersion)
			require.Equal(t, "sub2api-runtime", fact.Source)
			require.Equal(t, "profile_activated", fact.EventType)
			require.Equal(t, mode, fact.ProfileMode)
			// 画像事实只能来自运行时解析，不接受外部声明。
			require.Equal(t, release.ProfileDigest(), fact.ProfileDigest)
			require.Equal(t, release.ReleaseDigest(), fact.ReleaseDigest)
			require.Equal(t, release.Version(), fact.CodexVersion)
			require.Len(t, fact.EventID, 64)
		})
	}
}

func TestActivationFactRejectsMismatchedDeclaredDigest(t *testing.T) {
	_, err := resolveOfficialEgressActivationFact(
		activationConfig("active"),
		time.Unix(1765000000, 0),
		activationEnv(map[string]string{
			"GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST": "0000000000000000000000000000000000000000000000000000000000000000",
		}),
	)
	require.Error(t, err)
	require.Contains(t, err.Error(), "不一致")
}

func TestActivationFactAcceptsMatchingDeclaredIdentity(t *testing.T) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)

	fact, err := resolveOfficialEgressActivationFact(
		activationConfig("active"),
		time.Unix(1765000000, 0),
		activationEnv(map[string]string{
			"GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST":     release.ProfileDigest(),
			"GATEWAY_EGRESS_ACTIVATION_PROFILE_ID":         "codex-profile-under-test",
			"GATEWAY_EGRESS_ACTIVATION_IMAGE_ID":           "sha256:" + string(make([]byte, 0)) + "abc",
			"GATEWAY_EGRESS_ACTIVATION_BUILD_ID":           "build-under-test",
			"GATEWAY_EGRESS_ACTIVATION_DEPLOYED_VERSION":   "0.147.0",
			"GATEWAY_EGRESS_ACTIVATION_SOURCE_TREE_SHA256": "tree-under-test",
		}),
	)
	require.NoError(t, err)
	require.Equal(t, "codex-profile-under-test", fact.ProfileID)
	require.Equal(t, "build-under-test", fact.BuildID)
	require.Equal(t, "0.147.0", fact.DeployedVersion)
}

func TestActivationEventIDIsContentAddressedAndStable(t *testing.T) {
	first, err := resolveOfficialEgressActivationFact(
		activationConfig("active"), time.Unix(1765000000, 0), activationEnv(nil),
	)
	require.NoError(t, err)
	second, err := resolveOfficialEgressActivationFact(
		activationConfig("active"), time.Unix(1765000000, 0), activationEnv(nil),
	)
	require.NoError(t, err)
	require.Equal(t, first.EventID, second.EventID)

	later, err := resolveOfficialEgressActivationFact(
		activationConfig("active"), time.Unix(1765000001, 0), activationEnv(nil),
	)
	require.NoError(t, err)
	require.NotEqual(t, first.EventID, later.EventID)
}

func TestActivationFactWriteIsPrivateAndAbsoluteOnly(t *testing.T) {
	fact, err := resolveOfficialEgressActivationFact(
		activationConfig("active"), time.Unix(1765000000, 0), activationEnv(nil),
	)
	require.NoError(t, err)

	require.Error(t, writeOfficialEgressActivationFact("relative/path.json", fact))

	target := filepath.Join(t.TempDir(), "nested", "activation-fact.json")
	require.NoError(t, writeOfficialEgressActivationFact(target, fact))
	info, err := os.Stat(target)
	require.NoError(t, err)
	require.Equal(t, os.FileMode(0o600), info.Mode().Perm())

	var decoded OfficialEgressActivationFact
	raw, err := os.ReadFile(target)
	require.NoError(t, err)
	require.NoError(t, json.Unmarshal(raw, &decoded))
	require.Equal(t, fact, decoded)
}
