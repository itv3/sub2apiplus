package service

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"
)

// 发现与过滤：源快照指纹、专用模型排除、白名单、协议与能力快照收窄。

func TestCodexThirdPartyCandidatesAreExactlyTheGroupDefaultModelList(t *testing.T) {
	// 账号的源快照里有大量上游型号（中转站点同步来的），但分组默认启用的模型只有 kimi-k3：
	// 候选只能是名单里的模型，快照里的名字（含专用媒体、通配符、旧快照）一概不进入候选。
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	settings := &cxtpSettings{snapshots: map[int64]CodexCatalogSourceSnapshot{
		5: {AccountID: 5, Models: []string{"kimi-k3", "kimi-other-1", "kimi-other-2", "kimi-embedding-1"}, Status: CodexCatalogSourceComplete,
			SourceFingerprint: CodexCatalogSourceFingerprint(&kimi)},
	}}
	svc := newCXTPService(t, settings, []Account{kimi}, nil)
	var asked []int64
	svc.groupModels = func(_ context.Context, group *Group) ([]string, error) {
		asked = append(asked, group.ID)
		// 名单里带空白、重复、通配符和专用媒体模型：空白与重复被整理，通配符与媒体模型不作为候选。
		return []string{" kimi-k3 ", "kimi-k3", "kimi-*", "kimi-video-x", "kimi-embedding-1", ""}, nil
	}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	var slugs []string
	for _, c := range eval.Candidates {
		slugs = append(slugs, c.Slug)
	}
	require.Equal(t, []string{"kimi-k3"}, slugs)
	require.Equal(t, []string{"group_models"}, eval.Candidates[0].Sources)
	require.Equal(t, CodexModelStatusConfigurable, eval.Candidates[0].Status)
	require.Equal(t, []int64{8}, asked, "一次评估只生成一次名单")
}

func TestCodexThirdPartyEvaluationFailsWhenGroupModelListUnavailable(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	svc := newCXTPService(t, &cxtpSettings{}, []Account{kimi}, nil)
	group := &Group{ID: 8, Platform: PlatformKimi}

	svc.groupModels = func(context.Context, *Group) ([]string, error) { return nil, errors.New("boom") }
	_, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.ErrorIs(t, err, ErrCodexThirdPartyGroupModelsUnavailable)

	svc.groupModels = nil
	_, err = svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.ErrorIs(t, err, ErrCodexThirdPartyGroupModelsUnavailable)
}

func TestCodexThirdPartyRuntimePublishesOnlyModelsInGroupDefaultList(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	kimi.Credentials["model_mapping"] = map[string]any{"kimi-k3": "kimi-k3"}
	group := &Group{ID: 8, Platform: PlatformKimi}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, []Account{kimi}, nil)

	// 名单里有：发布。
	got, err := svc.ApplyToBody(context.Background(), group, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.Equal(t, []string{"kimi-k3"}, cxtpSlugs(t, got))

	// 名单里没有（例如账号被停用、映射被改掉）：停发，配置保留。
	svc.groupModels = func(context.Context, *Group) ([]string, error) { return []string{"other-model"}, nil }
	got, err = svc.ApplyToBody(context.Background(), group, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got))

	// 名单生成失败：第三方阶段降级为只发原生条目，不报错、不发布第三方。
	svc.groupModels = func(context.Context, *Group) ([]string, error) { return nil, errors.New("boom") }
	got, err = svc.ApplyToBody(context.Background(), group, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got))
}

func TestCodexThirdPartyRuntimeSkipsListLookupWhenNothingEnabled(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	svc := newCXTPService(t, &cxtpSettings{}, []Account{kimi}, nil)
	svc.groupModels = func(context.Context, *Group) ([]string, error) {
		t.Fatal("没有开启项时不应生成分组模型名单")
		return nil, nil
	}
	_, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, []byte(`{"models":[]}`))
	require.NoError(t, err)
}

func TestCodexThirdPartyEnabledModelMissingFromListShowsAsClosableRow(t *testing.T) {
	kimi := cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")
	svc := newCXTPService(t, &cxtpSettings{}, []Account{kimi}, nil)
	svc.groupModels = func(context.Context, *Group) ([]string, error) { return []string{"kimi-k3"}, nil }
	cfg := NewCodexModelVisibilityConfig()
	cfg.Models["gone-model"] = CodexModelVisibilityEntry{Enabled: true}
	group := &Group{ID: 8, Platform: PlatformKimi}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cfg)
	require.NoError(t, err)
	bySlug := map[string]CodexThirdPartyCandidate{}
	for _, c := range eval.Candidates {
		bySlug[c.Slug] = c
	}
	require.Contains(t, bySlug, "kimi-k3")
	gone := bySlug["gone-model"]
	require.True(t, gone.Enabled)
	require.False(t, gone.CanEnable)
	require.False(t, gone.Published)
	require.Equal(t, CodexModelStatusUnavailable, gone.Status)

	// 遗留行依赖开关，不能影响输入指纹（预览与保存时指纹必须一致）。
	clean, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, NewCodexModelVisibilityConfig())
	require.NoError(t, err)
	require.Equal(t, clean.InputFingerprint, eval.InputFingerprint)
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
