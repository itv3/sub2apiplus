package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

// 发现与过滤：源快照指纹、专用模型排除、白名单、协议与能力快照收窄。

func TestCodexThirdPartyDiscoveryUsesOnlyMatchingSnapshotsAndFiltersMedia(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	stale := cxtpCNAccount(6, PlatformKimi, "https://old.example", "responses")
	settings := &cxtpSettings{snapshots: map[int64]CodexCatalogSourceSnapshot{
		5: {AccountID: 5, Models: []string{"kimi-k3", "kimi-embedding-1", "kimi-video-x", "kimi-*"}, Status: CodexCatalogSourceComplete,
			SourceFingerprint: CodexCatalogSourceFingerprint(&kimi)},
		// 源指纹对不上（站点已换）的旧快照不得参与发现。
		6: {AccountID: 6, Models: []string{"kimi-stale-model"}, Status: CodexCatalogSourceComplete, SourceFingerprint: "outdated"},
	}}
	svc := newCXTPService(t, settings, []Account{kimi, stale}, nil)
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	var slugs []string
	for _, c := range eval.Candidates {
		slugs = append(slugs, c.Slug)
	}
	require.Equal(t, []string{"kimi-k3"}, slugs)
	require.Equal(t, []string{"upstream_sync"}, eval.Candidates[0].Sources)
	require.Equal(t, CodexModelStatusConfigurable, eval.Candidates[0].Status)
}

func TestCodexThirdPartyGroupAllowlistBlocksPublication(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	kimi.Credentials["model_mapping"] = map[string]any{"kimi-k3": "kimi-k3"}
	group := &Group{ID: 8, Platform: PlatformKimi, ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: []string{"other-model"}}}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, []Account{kimi}, nil)
	got, err := svc.ApplyToBody(context.Background(), group, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got))

	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "kimi-k3")[8])
	require.NoError(t, err)
	require.Equal(t, CodexModelStatusUnavailable, eval.Candidates[0].Status)
	require.False(t, eval.Candidates[0].CanEnable)
}

func TestCodexThirdPartyProtocolFollowsAccountPath(t *testing.T) {
	// 智谱平台账号没有原生 Responses 端点，沿用 Responses 到 Chat 的转换；DeepSeek 显式 responses 为原生。
	zhipu := cxtpCNAccount(5, PlatformZhipu, "https://zhipu.example", "")
	deepseek := cxtpCNAccount(6, PlatformDeepseek, "https://ds.example", "responses")
	require.Equal(t, CodexThirdPartyProtocolViaChat, codexTPAccountProtocol(&zhipu))
	require.Equal(t, CodexThirdPartyProtocolNative, codexTPAccountProtocol(&deepseek))
	oauth := cxtpOAuthAccount(1)
	require.Equal(t, CodexThirdPartyProtocolNative, codexTPAccountProtocol(&oauth))
}

func TestCodexThirdPartyAccountSnapshotCanOnlyNarrowImageInput(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	kimi.Credentials["model_mapping"] = map[string]any{"kimi-k3": "kimi-k3"}
	kimi.Extra = map[string]any{UpstreamModelMetadataExtraKey: map[string]any{
		"source": "test", "synced_at": "2026-10-23T00:00:00Z",
		"models": map[string]any{"kimi-k3": map[string]any{"id": "kimi-k3", "input_modalities": []any{"text"}}},
	}}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, []Account{kimi}, nil)
	got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.JSONEq(t, `["text"]`, string(cxtpEntry(t, got, "kimi-k3")["input_modalities"]))
}
