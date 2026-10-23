package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// 测试夹具
// ---------------------------------------------------------------------------

type cxtpSettings struct {
	cfg       map[int64]CodexModelVisibilityConfig
	cfgErr    error
	snapshots map[int64]CodexCatalogSourceSnapshot
}

func (s *cxtpSettings) LoadCodexModelVisibility(_ context.Context, groupID int64) (CodexModelVisibilityConfig, bool, error) {
	if s.cfgErr != nil {
		return NewCodexModelVisibilityConfig(), true, s.cfgErr
	}
	cfg, ok := s.cfg[groupID]
	if !ok {
		return NewCodexModelVisibilityConfig(), false, nil
	}
	return cfg, true, nil
}

func (s *cxtpSettings) LoadCodexCatalogSource(_ context.Context, accountID int64) (CodexCatalogSourceSnapshot, bool, error) {
	snap, ok := s.snapshots[accountID]
	return snap, ok, nil
}

type cxtpRepo struct {
	AccountRepository
	accounts []Account
	err      error
}

func (r *cxtpRepo) ListModelAvailabilityCandidates(_ context.Context, _ *int64, platforms []string, _ bool) ([]Account, error) {
	if r.err != nil {
		return nil, r.err
	}
	allowed := map[string]struct{}{}
	for _, platform := range platforms {
		allowed[platform] = struct{}{}
	}
	var out []Account
	for _, account := range r.accounts {
		if _, ok := allowed[account.Platform]; ok {
			out = append(out, account)
		}
	}
	return out, nil
}

type cxtpFetcher struct {
	bodies map[int64][]byte
	errs   map[int64]error
	// merge 模拟运行时映射之后的分组合并；为空表示不改动。
	merge func([]byte) []byte
	// listed 模拟账号能力快照里上游清单已列出的型号。
	listed map[int64]map[string]bool

	// fetched 记录 FetchBase 被调用的账号 ID（并发安全），用来证明哪些账号发起过上游获取。
	mu      sync.Mutex
	fetched []int64
}

// fetchedIDs 返回已排序的 FetchBase 调用账号。
func (f *cxtpFetcher) fetchedIDs() []int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := append([]int64(nil), f.fetched...)
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (f *cxtpFetcher) MergeGroupModels(_ context.Context, _ *Group, body []byte) ([]byte, error) {
	if f.merge == nil {
		return body, nil
	}
	return f.merge(body), nil
}

func (f *cxtpFetcher) UpstreamListsModel(account *Account, model string) bool {
	return f.listed[account.ID][model]
}

func (f *cxtpFetcher) FetchBase(_ context.Context, _ *Group, account *Account, _ string) ([]byte, error) {
	f.mu.Lock()
	f.fetched = append(f.fetched, account.ID)
	f.mu.Unlock()
	if err := f.errs[account.ID]; err != nil {
		return nil, err
	}
	return f.bodies[account.ID], nil
}

func newCXTPService(t *testing.T, settings *cxtpSettings, accounts []Account, fetcher *cxtpFetcher) *CodexThirdPartyCatalogService {
	t.Helper()
	profiles, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	if fetcher == nil {
		fetcher = &cxtpFetcher{}
	}
	svc := &CodexThirdPartyCatalogService{
		settings:    settings,
		accountRepo: &cxtpRepo{accounts: accounts},
		fetcher:     fetcher,
		resolver:    NewCompositeRouteResolver(nil),
		profiles:    profiles,
		now:         func() time.Time { return time.Unix(1_700_000_000, 0).UTC() },
		baseTimeout: time.Second,
	}
	// 默认的分组模型名单替身：账号映射的公开名（与普通模型列表的来源一致）加上测试夹具里
	// 基础目录和源快照出现的名称。需要精确控制名单的用例直接覆盖 svc.groupModels。
	svc.groupModels = func(ctx context.Context, group *Group) ([]string, error) {
		seen := map[string]struct{}{}
		var out []string
		add := func(name string) {
			if _, dup := seen[name]; !dup && strings.TrimSpace(name) != "" {
				seen[name] = struct{}{}
				out = append(out, name)
			}
		}
		for i := range accounts {
			for public := range AdvertisedModelMappingForAccount(&accounts[i]) {
				add(public)
			}
			if snap, ok := settings.snapshots[accounts[i].ID]; ok && snap.Matches(&accounts[i]) {
				for _, model := range snap.Models {
					add(model)
				}
			}
		}
		for _, body := range fetcher.bodies {
			if _, entries, err := parseCodexCatalogBody(body); err == nil {
				for _, entry := range entries {
					add(entry.slug)
				}
			}
		}
		if svc.resolver != nil && group != nil {
			// 普通模型列表也会列出 Composite 精确路由的公开名。
			routed, err := svc.resolver.ListExactPublicModels(ctx, group.ID, "", false)
			if err != nil {
				return nil, err
			}
			for _, model := range routed {
				add(model)
			}
		}
		return out, nil
	}
	return svc
}

func cxtpOAuthAccount(id int64) Account {
	return Account{ID: id, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Credentials: map[string]any{}}
}

// cxtpOpenAIKeyAccount 是 OpenAI 平台 API Key 账号，指向自定义兼容站点。
func cxtpOpenAIKeyAccount(id int64, baseURL string, mapping map[string]any) Account {
	creds := map[string]any{"api_key": "sk-test-secret", "base_url": baseURL}
	if mapping != nil {
		creds["model_mapping"] = mapping
	}
	return Account{ID: id, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: creds}
}

func cxtpCNAccount(id int64, platform, baseURL, protocol string) Account {
	creds := map[string]any{"api_key": "sk-test-secret", "base_url": baseURL}
	if protocol != "" {
		creds["api_protocol"] = protocol
	}
	return Account{ID: id, Platform: platform, Type: AccountTypeAPIKey, Credentials: creds}
}

// cxtpGPTEntry 生成带未知字段和缩进空白的原生 GPT 条目，用来验证原始字节保真。
func cxtpGPTEntry(slug string, pad int) string {
	return fmt.Sprintf(`{"slug":%q,"display_name":%q, "x_unknown_field":{"keep":[1,2,3]},"supported_reasoning_levels":[],"pad":%q}`, slug, strings.ToUpper(slug), strings.Repeat("p", pad))
}

func cxtpBase(entries ...string) []byte {
	return []byte(`{"x_top_level":{"a":1},"models":[` + strings.Join(entries, ",") + `]}`)
}

func cxtpSlugs(t *testing.T, body []byte) []string {
	t.Helper()
	_, entries, err := parseCodexCatalogBody(body)
	require.NoError(t, err)
	out := make([]string, 0, len(entries))
	for _, entry := range entries {
		out = append(out, entry.slug)
	}
	return out
}

func cxtpEntry(t *testing.T, body []byte, slug string) map[string]json.RawMessage {
	t.Helper()
	_, entries, err := parseCodexCatalogBody(body)
	require.NoError(t, err)
	for _, entry := range entries {
		if entry.slug == slug {
			var fields map[string]json.RawMessage
			require.NoError(t, json.Unmarshal(entry.raw, &fields))
			return fields
		}
	}
	t.Fatalf("slug %q not found in %v", slug, cxtpSlugs(t, body))
	return nil
}

func cxtpConfig(revision int64, enabled ...string) map[int64]CodexModelVisibilityConfig {
	cfg := NewCodexModelVisibilityConfig()
	cfg.Revision = revision
	for _, slug := range enabled {
		cfg.Models[slug] = CodexModelVisibilityEntry{Enabled: true}
	}
	return map[int64]CodexModelVisibilityConfig{8: cfg}
}

func cxtpCompositeGroup() *Group { return &Group{ID: 8, Platform: PlatformComposite} }

// ---------------------------------------------------------------------------
// 能力档案
// ---------------------------------------------------------------------------

func TestCodexThirdPartyProfilesLoadAndPinTemplate(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	require.Len(t, set.profiles, 4)
	for _, id := range []string{"deepseek-flash", "deepseek-v4-pro", "kimi-k3", "glm-5.3"} {
		profile := set.match(id)
		require.NotNil(t, profile, id)
		_, digest := codexThirdPartyRuntimeTemplate(profile)
		require.Equal(t, profile.InstructionsTemplateSHA256, digest, "%s 模板校验值必须与运行时一致", id)
	}
	// 档案文件里不得出现三段数字形式的版本号（版本泄漏门禁同样适用于嵌入数据）。
	require.False(t, regexp.MustCompile(`\d+\.\d+\.\d+`).Match(codexThirdPartyProfilesJSON))
}

func TestCodexThirdPartyProfilesRejectLegacyKeys(t *testing.T) {
	for _, key := range codexThirdPartyForbiddenModelInfoKeys {
		var file map[string]any
		require.NoError(t, json.Unmarshal(codexThirdPartyProfilesJSON, &file))
		profiles, ok := file["profiles"].([]any)
		require.True(t, ok, "profiles 应为数组")
		first, ok := profiles[0].(map[string]any)
		require.True(t, ok, "profiles[0] 应为对象")
		info, ok := first["model_info"].(map[string]any)
		require.True(t, ok, "model_info 应为对象")
		info[key] = true
		raw, err := json.Marshal(file)
		require.NoError(t, err)
		_, err = loadCodexThirdPartyProfiles(raw)
		require.Error(t, err, key)
	}
}

func TestCodexThirdPartyProfileEntriesCarryAllRequiredFields(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	required := []string{"slug", "display_name", "supported_reasoning_levels", "shell_type", "visibility",
		"supported_in_api", "priority", "support_verbosity", "truncation_policy", "experimental_supported_tools", "model_messages"}
	for _, profile := range set.profiles {
		entry, template, err := profile.buildEntry(profile.UpstreamModels[0], false)
		require.NoError(t, err)
		var fields map[string]json.RawMessage
		require.NoError(t, json.Unmarshal(entry, &fields))
		for _, key := range required {
			require.Contains(t, fields, key, "%s 缺少必填字段 %s", profile.ID, key)
		}
		var messages struct {
			InstructionsTemplate string `json:"instructions_template"`
		}
		require.NoError(t, json.Unmarshal(fields["model_messages"], &messages))
		require.Equal(t, template, messages.InstructionsTemplate)
		for _, key := range codexThirdPartyForbiddenModelInfoKeys {
			require.NotContains(t, fields, key)
		}
	}
}

func cxtpProfileEntry(t *testing.T, id string) map[string]any {
	t.Helper()
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	entry, _, err := set.match(id).buildEntry(id, false)
	require.NoError(t, err)
	var out map[string]any
	require.NoError(t, json.Unmarshal(entry, &out))
	return out
}

func TestCodexThirdPartyDeepSeekProfilesMatchDesignTable(t *testing.T) {
	flash := cxtpProfileEntry(t, "deepseek-flash")
	pro := cxtpProfileEntry(t, "deepseek-v4-pro")
	// 两者相同的字段（逐键核对方案第 5.3 节）。
	for _, entry := range []map[string]any{flash, pro} {
		require.Equal(t, "shell_command", entry["shell_type"])
		require.Equal(t, "freeform", entry["apply_patch_tool_type"])
		require.Nil(t, entry["tool_mode"])
		require.Equal(t, false, entry["use_responses_lite"])
		require.Equal(t, "high", entry["default_reasoning_level"])
		require.EqualValues(t, 1048576, entry["context_window"])
		require.EqualValues(t, 1048576, entry["max_context_window"])
		require.Equal(t, "text", entry["web_search_tool_type"])
		require.Equal(t, map[string]any{"mode": "tokens", "limit": float64(10000)}, entry["truncation_policy"])
		require.Equal(t, "none", entry["default_reasoning_summary"])
		require.Equal(t, true, entry["supports_reasoning_summary_parameter"])
		require.Equal(t, "3000", entry["comp_hash"])
		require.Equal(t, "v2", entry["multi_agent_version"])
		require.Equal(t, []any{}, entry["service_tiers"])
		require.Nil(t, entry["default_service_tier"])
		require.NotContains(t, entry, "multi_agent_reasoning_effort")
		require.Equal(t, false, entry["support_verbosity"])
		require.EqualValues(t, 95, entry["effective_context_window_percent"])
		require.Equal(t, []any{}, entry["experimental_supported_tools"])
		require.Equal(t, "list", entry["visibility"])
		require.Equal(t, true, entry["supported_in_api"])
		require.EqualValues(t, 50, entry["priority"])
		levels, ok := entry["supported_reasoning_levels"].([]any)
		require.True(t, ok, "supported_reasoning_levels 应为数组")
		require.Len(t, levels, 3)
		want := [][2]string{
			{"low", "Fast responses with lighter reasoning"},
			{"high", "Extra high reasoning depth for complex problems"},
			{"max", "Maximum reasoning depth for the hardest problems"},
		}
		for i, level := range levels {
			lv, ok := level.(map[string]any)
			require.True(t, ok, "推理档位条目应为对象")
			require.Equal(t, want[i][0], lv["effort"])
			require.Equal(t, want[i][1], lv["description"])
		}
	}
	require.Equal(t, "deepseek-flash", flash["slug"])
	require.Equal(t, "deepseek-flash", flash["display_name"])
	require.Equal(t, []any{"text", "image"}, flash["input_modalities"])
	require.Equal(t, true, flash["supports_image_detail_original"])
	require.Equal(t, true, flash["supports_search_tool"])
	require.Equal(t, "deepseek-v4-pro", pro["slug"])
	require.Equal(t, []any{"text"}, pro["input_modalities"])
	require.Equal(t, false, pro["supports_image_detail_original"])
	require.Equal(t, false, pro["supports_search_tool"])
}

func TestCodexThirdPartyKimiAndGLMProfiles(t *testing.T) {
	kimi := cxtpProfileEntry(t, "kimi-k3")
	require.Equal(t, []any{"text", "image"}, kimi["input_modalities"])
	require.Equal(t, "max", kimi["default_reasoning_level"])
	var efforts []string
	kimiLevels, ok := kimi["supported_reasoning_levels"].([]any)
	require.True(t, ok, "supported_reasoning_levels 应为数组")
	for _, level := range kimiLevels {
		lv, ok := level.(map[string]any)
		require.True(t, ok, "推理档位条目应为对象")
		effort, ok := lv["effort"].(string)
		require.True(t, ok, "effort 应为字符串")
		efforts = append(efforts, effort)
	}
	require.Equal(t, []string{"low", "high", "max"}, efforts)
	require.EqualValues(t, 1048576, kimi["context_window"])

	glm := cxtpProfileEntry(t, "glm-5.3")
	require.Equal(t, []any{"text"}, glm["input_modalities"], "GLM 必须显式声明仅文本，省略时客户端默认允许图片")
	require.NotContains(t, glm["input_modalities"], "image")
	require.Equal(t, "max", glm["default_reasoning_level"])
	require.Equal(t, "shell_command", glm["shell_type"])
	require.Equal(t, "freeform", glm["apply_patch_tool_type"])
	require.EqualValues(t, 1048576, glm["max_context_window"])
}

func TestCodexThirdPartyRemoveImageOnlyNarrows(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	entry, _, err := set.match("kimi-k3").buildEntry("kimi-k3", true)
	require.NoError(t, err)
	var fields map[string]any
	require.NoError(t, json.Unmarshal(entry, &fields))
	require.Equal(t, []any{"text"}, fields["input_modalities"])
	require.Equal(t, false, fields["supports_image_detail_original"])
}

func TestCodexThirdPartyProfileTemplateMismatchStopsEntry(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	profile := *set.match("kimi-k3")
	profile.InstructionsTemplateSHA256 = strings.Repeat("0", 64)
	_, _, err = profile.buildEntry("kimi-k3", false)
	require.Error(t, err)
	require.Contains(t, err.Error(), "模板")
}

// ---------------------------------------------------------------------------
// 合并、过滤与降级
// ---------------------------------------------------------------------------

func TestCodexThirdPartyPureOpenAIGroupIsByteIdentical(t *testing.T) {
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("gpt-5.4-mini", 0), cxtpGPTEntry("codex-auto-review", 0), cxtpGPTEntry("unlisted-internal-slug", 0))
	// 即使配置里开启了第三方模型，只有 ChatGPT 账号的分组也不会有任何变化。
	settings := &cxtpSettings{cfg: cxtpConfig(3, "kimi-k3", "deepseek-flash")}
	svc := newCXTPService(t, settings, []Account{cxtpOAuthAccount(1), cxtpOAuthAccount(2)}, nil)
	group := &Group{ID: 8, Platform: PlatformOpenAI}

	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.True(t, bytes.Equal(base, got), "纯 ChatGPT 分组的 GPT 目录必须逐字节不变")

	// 同样的账号组成在 Composite 分组里也不应改变（官方账号不会承接第三方）。
	got, err = svc.ApplyToBody(context.Background(), cxtpCompositeGroup(), base)
	require.NoError(t, err)
	require.True(t, bytes.Equal(base, got))
}

func TestCodexThirdPartyBaseThirdPartyEntriesRemovedWhenDisabled(t *testing.T) {
	accounts := []Account{
		cxtpOAuthAccount(1),
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"kimi-k3": "kimi-k3"}),
	}
	gpt := cxtpGPTEntry("gpt-5.6-sol", 0)
	base := cxtpBase(gpt, cxtpGPTEntry("kimi-k3", 0), cxtpGPTEntry("gpt-5.4", 0))
	svc := newCXTPService(t, &cxtpSettings{}, accounts, nil)
	group := &Group{ID: 8, Platform: PlatformOpenAI}

	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol", "gpt-5.4"}, cxtpSlugs(t, got))
	// 原生条目原始字节与顶层未知字段保持不变。
	require.Contains(t, string(got), `"x_top_level":{"a":1}`)
	require.Contains(t, string(got), gpt)
	require.True(t, strings.HasPrefix(string(got), `{"x_top_level":{"a":1},"models":[`+gpt+`,`))
}

func TestCodexThirdPartySameNamePassthroughReplacesBaseObject(t *testing.T) {
	accounts := []Account{
		cxtpOAuthAccount(1),
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"kimi-k3": "kimi-k3"}),
	}
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("kimi-k3", 0))
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(2, "kimi-k3")}, accounts, nil)
	got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformOpenAI}, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol", "kimi-k3"}, cxtpSlugs(t, got), "替换基础目录同名对象，位置不变")
	entry := cxtpEntry(t, got, "kimi-k3")
	require.NotContains(t, entry, "x_unknown_field", "不得残留基础目录旧对象的字段")
	var name string
	require.NoError(t, json.Unmarshal(entry["display_name"], &name))
	require.Equal(t, "kimi-k3", name)
	var modalities []string
	require.NoError(t, json.Unmarshal(entry["input_modalities"], &modalities))
	require.Equal(t, []string{"text", "image"}, modalities)
}

func TestCodexThirdPartyExplicitAliasUsesTargetProfile(t *testing.T) {
	accounts := []Account{
		cxtpOAuthAccount(1),
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"my-kimi": "kimi-k3"}),
	}
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("my-kimi", 0))
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "my-kimi")}, accounts, nil)
	got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformOpenAI}, base)
	require.NoError(t, err)
	entry := cxtpEntry(t, got, "my-kimi")
	var name string
	require.NoError(t, json.Unmarshal(entry["display_name"], &name))
	require.Equal(t, "my-kimi", name, "别名场景 slug 与显示名都是公开名")
	require.Contains(t, string(entry["supported_reasoning_levels"]), "max")
}

func TestCodexThirdPartyGPTNameMappedToThirdPartyIsNotNative(t *testing.T) {
	// gpt-5.6-sol 在自定义站点账号上被映射为 kimi-k3：不能凭名称前缀当原生。
	accounts := []Account{
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"gpt-5.6-sol": "kimi-k3"}),
	}
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0))
	svc := newCXTPService(t, &cxtpSettings{}, accounts, nil)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got), "关闭时该条目被移除")

	// 开启后按第三方（kimi-k3 档案）发布，slug 仍是公开名 gpt-5.6-sol。
	svc = newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "gpt-5.6-sol")}, accounts, nil)
	got, err = svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, got))
	require.Contains(t, string(cxtpEntry(t, got, "gpt-5.6-sol")["input_modalities"]), "image")

	// 与 ChatGPT 账号并存时归属冲突，停发。
	svc = newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "gpt-5.6-sol")}, append(accounts, cxtpOAuthAccount(1)), nil)
	got, err = svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got))
}

func TestCodexThirdPartyCompositeWithAndWithoutOpenAIAccounts(t *testing.T) {
	deepseek := cxtpCNAccount(3, PlatformDeepseek, "https://ds.example", "responses")
	deepseek.Credentials["model_mapping"] = map[string]any{"deepseek-flash": "deepseek-flash", "deepseek-v4-pro": "deepseek-v4-pro"}
	settings := &cxtpSettings{cfg: cxtpConfig(4, "deepseek-flash")}

	// 有 OpenAI 账号：原生 GPT 保留，开启的第三方追加在后。
	svc := newCXTPService(t, settings, []Account{cxtpOAuthAccount(1), deepseek}, nil)
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("deepseek-v4-pro", 0))
	got, err := svc.ApplyToBody(context.Background(), cxtpCompositeGroup(), base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol", "deepseek-flash"}, cxtpSlugs(t, got))

	// 无 OpenAI 账号：空基础加第三方。
	svc = newCXTPService(t, settings, []Account{deepseek}, nil)
	got, err = svc.BuildThirdPartyOnly(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Equal(t, []string{"deepseek-flash"}, cxtpSlugs(t, got))
	hasBase, err := svc.HasOpenAIBaseSource(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.False(t, hasBase)

	// 全部关闭：有效空目录。
	svc = newCXTPService(t, &cxtpSettings{}, []Account{deepseek}, nil)
	got, err = svc.BuildThirdPartyOnly(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.JSONEq(t, `{"models":[]}`, string(got))
}

func TestCodexThirdPartyCompositeExplicitRouteRewritesUpstream(t *testing.T) {
	// Composite 精确路由把公开名 team-model 改写到 kimi 平台的 kimi-k3。
	kimi := cxtpCNAccount(4, PlatformKimi, "https://kimi.example", "responses")
	repo := cxtpRouteRepo{routes: []CompositeModelRoute{{
		ID: 1, GroupID: 8, PublicModel: "team-model", MatchType: CompositeRouteMatchExact,
		TargetPlatform: PlatformKimi, UpstreamModel: "kimi-k3", Endpoint: CompositeRouteEndpointAny, Enabled: true,
	}}}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "team-model")}, []Account{kimi}, nil)
	svc.resolver = NewCompositeRouteResolver(repo)
	got, err := svc.BuildThirdPartyOnly(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.Equal(t, []string{"team-model"}, cxtpSlugs(t, got))
	require.Contains(t, string(cxtpEntry(t, got, "team-model")["input_modalities"]), "image")
}

type cxtpRouteRepo struct {
	CompositeModelRouteRepository
	routes []CompositeModelRoute
	err    error
}

func (r cxtpRouteRepo) ListByGroup(context.Context, int64, bool) ([]CompositeModelRoute, error) {
	return r.routes, r.err
}

func TestCodexThirdPartyPureCNPlatformGroup(t *testing.T) {
	zhipu := cxtpCNAccount(5, PlatformZhipu, "https://zhipu.example", "")
	zhipu.Credentials["model_mapping"] = map[string]any{"glm-5.3": "glm-5.3"}
	// 通用构建器为纯 CN 分组生成的目录：glm-5.3 带 text+image 的构建器默认值，必须被档案整对象替换。
	manifest, err := BuildCodexModelsManifest([]string{"glm-5.3"})
	require.NoError(t, err)
	group := &Group{ID: 8, Platform: PlatformZhipu}

	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "glm-5.3")}, []Account{zhipu}, nil)
	got, err := svc.ApplyToBody(context.Background(), group, manifest)
	require.NoError(t, err)
	entry := cxtpEntry(t, got, "glm-5.3")
	require.JSONEq(t, `["text"]`, string(entry["input_modalities"]))
	require.JSONEq(t, `"max"`, string(entry["default_reasoning_level"]))

	// 默认关闭：目录为空。
	svc = newCXTPService(t, &cxtpSettings{}, []Account{zhipu}, nil)
	got, err = svc.ApplyToBody(context.Background(), group, manifest)
	require.NoError(t, err)
	require.JSONEq(t, `{"models":[]}`, string(got))
}

func TestCodexThirdPartyOpenAIKeyAccountCarriesThirdPartyModel(t *testing.T) {
	accounts := []Account{cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"glm-5.3": "glm-5.3"})}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "glm-5.3")}, accounts, nil)
	got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformOpenAI}, cxtpBase(cxtpGPTEntry("glm-5.3", 0)))
	require.NoError(t, err)
	require.JSONEq(t, `["text"]`, string(cxtpEntry(t, got, "glm-5.3")["input_modalities"]))
}

func TestCodexThirdPartyModelWithoutProfileIsNeverPublished(t *testing.T) {
	// 没有档案也没有可靠能力快照：状态为待补能力，绝不套用构建器的 272K、none、unified_exec 默认值。
	accounts := []Account{cxtpCNAccount(5, PlatformKimi, "https://kimi.example", "responses")}
	accounts[0].Credentials["model_mapping"] = map[string]any{"kimi-unknown-9": "kimi-unknown-9"}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-unknown-9")}, accounts, nil)
	got, err := svc.ApplyToBody(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.Empty(t, cxtpSlugs(t, got))

	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), &Group{ID: 8, Platform: PlatformKimi}, cxtpConfig(1, "kimi-unknown-9")[8])
	require.NoError(t, err)
	var found bool
	for _, c := range eval.Candidates {
		if c.Slug == "kimi-unknown-9" {
			found = true
			require.Equal(t, CodexModelStatusNeedsCapability, c.Status)
			require.False(t, c.CanEnable)
			require.False(t, c.Published)
		}
	}
	require.True(t, found)
}

// ---------------------------------------------------------------------------
// 体积预算与降级
// ---------------------------------------------------------------------------

func TestCodexThirdPartyCatalogSizeBoundary(t *testing.T) {
	accounts := []Account{
		cxtpOAuthAccount(1),
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"kimi-k3": "kimi-k3"}),
	}
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, accounts, nil)

	// 先量出填充为 0 时的合并体积，再用 pad 精确补到边界。
	probe, err := svc.ApplyToBody(context.Background(), group, cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0)))
	require.NoError(t, err)
	pad := CodexCatalogMaxBytes - len(probe)
	require.Positive(t, pad)

	atLimit, err := svc.ApplyToBody(context.Background(), group, cxtpBase(cxtpGPTEntry("gpt-5.6-sol", pad)))
	require.NoError(t, err)
	require.Equal(t, CodexCatalogMaxBytes, len(atLimit), "1048576 字节必须通过")
	require.Equal(t, []string{"gpt-5.6-sol", "kimi-k3"}, cxtpSlugs(t, atLimit))

	over, err := svc.ApplyToBody(context.Background(), group, cxtpBase(cxtpGPTEntry("gpt-5.6-sol", pad+1)))
	require.NoError(t, err)
	require.LessOrEqual(t, len(over), CodexCatalogMaxBytes)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, over), "1048577 字节降级为只发原生条目")

	// 原生部分自身超限：只有这时才返回目录错误。
	_, err = svc.ApplyToBody(context.Background(), group, cxtpBase(cxtpGPTEntry("gpt-5.6-sol", CodexCatalogMaxBytes)))
	require.ErrorIs(t, err, ErrCodexCatalogUnavailable)
}

func TestCodexThirdPartyDegradesOnConfigAndRouteFailures(t *testing.T) {
	accounts := []Account{
		cxtpOAuthAccount(1),
		cxtpOpenAIKeyAccount(2, "https://relay.example", map[string]any{"kimi-k3": "kimi-k3"}),
	}
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	base := cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("kimi-k3", 0))

	// 配置损坏或读取失败：不返回错误、不泄漏未过滤的第三方条目，只发原生 GPT。
	broken := &cxtpSettings{cfgErr: errors.New("corrupt config")}
	svc := newCXTPService(t, broken, accounts, nil)
	got, err := svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, got))

	// 路由查询失败（账号列表读取失败）：同样降级，纯第三方分组降级为空目录。
	svc = newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, accounts, nil)
	svc.accountRepo = &cxtpRepo{err: errors.New("db down")}
	got, err = svc.ApplyToBody(context.Background(), group, base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, got))
	got, err = svc.BuildThirdPartyOnly(context.Background(), cxtpCompositeGroup())
	require.NoError(t, err)
	require.JSONEq(t, `{"models":[]}`, string(got))

	// Composite 路由读取失败。
	svc = newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "kimi-k3")}, accounts, nil)
	svc.resolver = NewCompositeRouteResolver(cxtpRouteRepo{err: errors.New("route query failed")})
	got, err = svc.ApplyToBody(context.Background(), cxtpCompositeGroup(), base)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-5.6-sol"}, cxtpSlugs(t, got))
}

// ---------------------------------------------------------------------------
// 评估器
// ---------------------------------------------------------------------------

func cxtpEvalFixture(t *testing.T, baseURL string) (*CodexThirdPartyCatalogService, *Group) {
	t.Helper()
	deepseek := cxtpCNAccount(3, PlatformDeepseek, baseURL, "responses")
	deepseek.Credentials["model_mapping"] = map[string]any{"deepseek-flash": "deepseek-flash", "deepseek-v4-pro": "deepseek-v4-pro"}
	kimi := cxtpOpenAIKeyAccount(4, baseURL, map[string]any{"kimi-k3": "kimi-k3"})
	// 账号 4 是自定义站点上的 OpenAI API Key 账号，同样可能提供基础目录。
	fetcher := &cxtpFetcher{bodies: map[int64][]byte{
		1: cxtpBase(cxtpGPTEntry("gpt-5.6-sol", 0), cxtpGPTEntry("kimi-k3", 0)),
		4: cxtpBase(cxtpGPTEntry("kimi-k3", 0)),
	}}
	oauth := cxtpOAuthAccount(1)
	svc := newCXTPService(t, &cxtpSettings{}, []Account{oauth, deepseek, kimi}, fetcher)
	return svc, cxtpCompositeGroup()
}

func TestCodexThirdPartyEvaluatorCandidatesAndBudget(t *testing.T) {
	svc, group := cxtpEvalFixture(t, "https://site.example")
	cfg := cxtpConfig(2, "deepseek-flash")[8]
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cfg)
	require.NoError(t, err)

	bySlug := map[string]CodexThirdPartyCandidate{}
	for _, c := range eval.Candidates {
		bySlug[c.Slug] = c
	}
	require.True(t, bySlug["gpt-5.6-sol"].Native)
	require.Equal(t, CodexModelStatusNative, bySlug["gpt-5.6-sol"].Status)
	flash := bySlug["deepseek-flash"]
	require.True(t, flash.Enabled)
	require.True(t, flash.Published)
	require.Equal(t, CodexModelStatusConfigurable, flash.Status)
	require.Equal(t, "deepseek-flash", flash.ProfileID)
	require.Equal(t, CodexThirdPartyProtocolNative, flash.Protocol)
	require.NotEmpty(t, flash.Fingerprint)
	require.Contains(t, flash.Capabilities.InputModalities, "image")
	pro := bySlug["deepseek-v4-pro"]
	require.False(t, pro.Enabled)
	require.True(t, pro.CanEnable)
	require.NotContains(t, bySlug["deepseek-flash"].Sources, "")
	// 基础目录里已有的 kimi-k3 作为第三方候选出现，默认关闭。
	require.Contains(t, bySlug["kimi-k3"].Sources, "base_catalog")
	require.False(t, bySlug["kimi-k3"].Published)

	require.True(t, eval.Budget.Complete)
	require.Equal(t, CodexCatalogMaxBytes, eval.Budget.LimitBytes)
	require.Positive(t, eval.Budget.WorstCaseBytes)
	require.Equal(t, CodexCatalogMaxBytes-eval.Budget.WorstCaseBytes, eval.Budget.RemainingBytes)
	require.Equal(t, 1, eval.Budget.EnabledThirdPartyCount)
	require.Equal(t, []int64{1}, eval.Budget.BaseAccountIDs, "只有官方账号参与预算，中转账号不取基础目录")
	require.NotEmpty(t, eval.InputFingerprint)
}

func TestCodexThirdPartyEvaluatorFingerprintsAreDeterministic(t *testing.T) {
	svc, group := cxtpEvalFixture(t, "https://site.example")
	off := cxtpConfig(1)[8]
	on := cxtpConfig(9, "deepseek-flash", "kimi-k3")[8]

	first, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, off)
	require.NoError(t, err)
	second, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, off)
	require.NoError(t, err)
	require.Equal(t, first.InputFingerprint, second.InputFingerprint, "相同输入指纹必须相同")

	// 开关与 revision 不属于输入指纹。
	enabled, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, on)
	require.NoError(t, err)
	require.Equal(t, first.InputFingerprint, enabled.InputFingerprint)

	// 换站点：输入指纹与条目指纹都必须变化。
	other, otherGroup := cxtpEvalFixture(t, "https://another-site.example")
	moved, err := other.EvaluateCodexThirdPartyCatalog(context.Background(), otherGroup, off)
	require.NoError(t, err)
	require.NotEqual(t, first.InputFingerprint, moved.InputFingerprint)
	fingerprintOf := func(eval CodexThirdPartyCatalogEvaluation, slug string) string {
		for _, c := range eval.Candidates {
			if c.Slug == slug {
				return c.Fingerprint
			}
		}
		return ""
	}
	require.NotEmpty(t, fingerprintOf(first, "deepseek-flash"))
	require.NotEqual(t, fingerprintOf(first, "deepseek-flash"), fingerprintOf(moved, "deepseek-flash"))
}

func TestCodexThirdPartyEvaluatorVerifiedStatusFollowsFingerprint(t *testing.T) {
	svc, group := cxtpEvalFixture(t, "https://site.example")
	base, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1)[8])
	require.NoError(t, err)
	var fingerprint string
	for _, c := range base.Candidates {
		if c.Slug == "deepseek-flash" {
			fingerprint = c.Fingerprint
		}
	}
	cfg := cxtpConfig(2, "deepseek-flash")[8]
	entry := cfg.Models["deepseek-flash"]
	entry.Verification = &CodexModelVerification{Fingerprint: fingerprint}
	cfg.Models["deepseek-flash"] = entry
	verified, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cfg)
	require.NoError(t, err)
	status := func(eval CodexThirdPartyCatalogEvaluation) CodexThirdPartyModelStatus {
		for _, c := range eval.Candidates {
			if c.Slug == "deepseek-flash" {
				return c.Status
			}
		}
		return ""
	}
	require.Equal(t, CodexModelStatusVerified, status(verified))

	// 换站点后旧验收记录不再匹配，降回配置可用。
	other, otherGroup := cxtpEvalFixture(t, "https://another-site.example")
	moved, err := other.EvaluateCodexThirdPartyCatalog(context.Background(), otherGroup, cfg)
	require.NoError(t, err)
	require.Equal(t, CodexModelStatusConfigurable, status(moved))
}

func TestCodexThirdPartyEvaluatorBudgetIncompleteWhenBaseMissing(t *testing.T) {
	svc, group := cxtpEvalFixture(t, "https://site.example")
	svc.fetcher = &cxtpFetcher{errs: map[int64]error{1: errors.New("upstream down")}}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1)[8])
	require.NoError(t, err)
	require.False(t, eval.Budget.Complete)
	require.NotEmpty(t, eval.Budget.Reason)
}

func TestCodexThirdPartyEvaluatorPureThirdPartyGroupUsesEmptyBase(t *testing.T) {
	zhipu := cxtpCNAccount(5, PlatformZhipu, "https://zhipu.example", "")
	zhipu.Credentials["model_mapping"] = map[string]any{"glm-5.3": "glm-5.3"}
	svc := newCXTPService(t, &cxtpSettings{}, []Account{zhipu}, nil)
	group := &Group{ID: 8, Platform: PlatformZhipu}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "glm-5.3")[8])
	require.NoError(t, err)
	require.True(t, eval.Budget.Complete)
	require.Empty(t, eval.Budget.BaseAccountIDs)
	require.Positive(t, eval.Budget.WorstCaseBytes)
	require.Len(t, eval.Candidates, 1)
	require.Equal(t, CodexThirdPartyProtocolViaChat, eval.Candidates[0].Protocol, "智谱平台账号沿用 Responses 到 Chat 的转换通路")
	require.Equal(t, 1, eval.Budget.EnabledThirdPartyCount)
}

func TestCodexThirdPartyEvaluatorOverBudgetMarksEnabledModels(t *testing.T) {
	svc, group := cxtpEvalFixture(t, "https://site.example")
	svc.fetcher = &cxtpFetcher{bodies: map[int64][]byte{1: cxtpBase(cxtpGPTEntry("gpt-5.6-sol", CodexCatalogMaxBytes-30_000))}}
	eval, err := svc.EvaluateCodexThirdPartyCatalog(context.Background(), group, cxtpConfig(1, "deepseek-flash", "deepseek-v4-pro")[8])
	require.NoError(t, err)
	require.Greater(t, eval.Budget.WorstCaseBytes, CodexCatalogMaxBytes)
	require.Negative(t, eval.Budget.RemainingBytes)
	for _, c := range eval.Candidates {
		if c.Enabled {
			require.Equal(t, CodexModelStatusOverBudget, c.Status)
			require.False(t, c.Published)
		}
	}
}
