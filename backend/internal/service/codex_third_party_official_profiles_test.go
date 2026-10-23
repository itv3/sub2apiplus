package service

// deepseek-flash 与 deepseek-v4-pro 的条目以 DeepSeek 官方 Codex 教程的 models.json 为准：
// 网关最终下发的条目与官方条目逐键逐值一致，只有下面列出的例外。

import (
	"context"
	_ "embed"
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// cxtpOfficialDeepSeekModelsJSON 是官方 models.json 的测试副本（与生产嵌入文件分开保存，
// 生产文件被改动时这里仍然是官方原文）。
//
//go:embed testdata/codex_deepseek_official_models.json
var cxtpOfficialDeepSeekModelsJSON []byte

// cxtpOfficialException 是网关条目与官方条目允许不同的键：对外名以上游叫法为准，所以 slug 与
// display_name 取公开名而不是官方的 "DeepSeek-Flash" 这类写法。
var cxtpOfficialExceptions = []string{"slug", "display_name"}

func cxtpOfficialEntry(t *testing.T, slug string) map[string]any {
	t.Helper()
	var file struct {
		Models []map[string]any `json:"models"`
	}
	require.NoError(t, json.Unmarshal(cxtpOfficialDeepSeekModelsJSON, &file))
	for _, entry := range file.Models {
		if entry["slug"] == slug {
			return entry
		}
	}
	t.Fatalf("官方条目 %s 不存在", slug)
	return nil
}

func cxtpGatewayEntry(t *testing.T, entry []byte) map[string]any {
	t.Helper()
	var out map[string]any
	require.NoError(t, json.Unmarshal(entry, &out))
	return out
}

// cxtpAssertEqualsOfficial 断言网关条目去掉例外键后与官方条目键集、取值完全一致。
func cxtpAssertEqualsOfficial(t *testing.T, label string, gateway, official map[string]any, publicName string, extraExceptions ...string) {
	t.Helper()
	exceptions := append(append([]string(nil), cxtpOfficialExceptions...), extraExceptions...)
	require.Equal(t, publicName, gateway["slug"], label)
	require.Equal(t, publicName, gateway["display_name"], label+"：显示名等于对外名")
	strip := func(m map[string]any) map[string]any {
		out := map[string]any{}
		for k, v := range m {
			out[k] = v
		}
		for _, k := range exceptions {
			delete(out, k)
		}
		return out
	}
	// 键集相同：官方没有的键不出现，官方写了的键不缺。
	gatewayKeys, officialKeys := make([]string, 0, len(gateway)), make([]string, 0, len(official))
	for k := range gateway {
		gatewayKeys = append(gatewayKeys, k)
	}
	for k := range official {
		officialKeys = append(officialKeys, k)
	}
	require.ElementsMatch(t, officialKeys, gatewayKeys, label+"：键集必须与官方一致")
	require.Equal(t, strip(official), strip(gateway), label+"：除例外键外逐键逐值一致")
}

func TestCodexThirdPartyDeepSeekEntriesEqualOfficialTutorial(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	for _, tc := range []struct{ profile, slug string }{
		{"deepseek-flash", "deepseek-flash"},
		{"deepseek-v4-pro", "deepseek-v4-pro"},
	} {
		official := cxtpOfficialEntry(t, tc.slug)
		profile := set.match(tc.profile)
		require.NotNil(t, profile, tc.profile)
		require.True(t, profile.Official, tc.profile)

		// 对外名等于官方 slug，或是别的写法（例如大小写、带前缀的站点叫法）时都一样。
		for _, publicName := range []string{tc.slug, "My-" + tc.slug} {
			raw, _, err := profile.buildEntry(publicName, false)
			require.NoError(t, err)
			cxtpAssertEqualsOfficial(t, tc.profile+"/"+publicName, cxtpGatewayEntry(t, raw), official, publicName)
		}
	}
}

func TestCodexThirdPartyDeepSeekOfficialKeyFacts(t *testing.T) {
	// 逐项钉住 ARM64 实测与官方不一致的那些键：以后任何人改回通用构造器的值都会在这里报错。
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	for _, id := range []string{"deepseek-flash", "deepseek-v4-pro"} {
		raw, _, err := set.match(id).buildEntry(id, false)
		require.NoError(t, err)
		entry := cxtpGatewayEntry(t, raw)
		require.Equal(t, true, entry["support_verbosity"], id)
		require.Equal(t, "low", entry["default_verbosity"], id)
		require.Equal(t, "freeform", entry["apply_patch_tool_type"], id, "不因中转不支持而降级")
		require.Equal(t, "0.144.0", entry["minimal_client_version"], id)
		for _, key := range []string{
			"include_apps_usage_instructions", "include_plugin_usage_instructions", "additional_speed_tiers", "service_tiers",
			"model_specialty", "node_repl_auto_review_required", "node_repl_disabled", "supports_reasoning_summary_parameter",
		} {
			require.NotContains(t, entry, key, "%s：官方没有的键不能出现（%s）", id, key)
		}
		for _, key := range []string{"prefer_websockets", "reasoning_summary_format", "supports_parallel_tool_calls", "supports_reasoning_summaries", "base_instructions"} {
			require.Contains(t, entry, key, "%s：官方写了的键必须保留（%s）", id, key)
		}
	}
}

func TestCodexThirdPartyDeepSeekOfficialPriorityAndInstructions(t *testing.T) {
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	flashRaw, _, err := set.match("deepseek-flash").buildEntry("deepseek-flash", false)
	require.NoError(t, err)
	proRaw, _, err := set.match("deepseek-v4-pro").buildEntry("deepseek-v4-pro", false)
	require.NoError(t, err)
	flash, pro := cxtpGatewayEntry(t, flashRaw), cxtpGatewayEntry(t, proRaw)
	require.EqualValues(t, 1, flash["priority"])
	require.EqualValues(t, 2, pro["priority"])
	require.Equal(t, "Latest frontier agentic coding model with image input.", flash["description"])
	require.Equal(t, "Most capable frontier agentic coding model.", pro["description"])
	for _, entry := range []map[string]any{flash, pro} {
		messages, ok := entry["model_messages"].(map[string]any)
		require.True(t, ok)
		template, ok := messages["instructions_template"].(string)
		require.True(t, ok)
		require.Len(t, []rune(template), 17730, "指令模板就是官方文本")
		require.Equal(t, template, entry["base_instructions"], "官方 base_instructions 与模板相同")
		require.Contains(t, messages, "approvals")
		require.Nil(t, messages["approvals"])
		require.Equal(t, map[string]any{"personality_default": "", "personality_friendly": "", "personality_pragmatic": ""}, messages["instructions_variables"])
	}
	require.Equal(t, []any{"text", "image"}, flash["input_modalities"])
	require.Equal(t, []any{"text"}, pro["input_modalities"])
}

func TestCodexThirdPartyDeepSeekOfficialEntryOnlyNarrowsImageFromSnapshots(t *testing.T) {
	// 现有的账号能力快照调整规则保留：承接账号明确不含图片输入时，移除图片输入与 original 图片细节，其余仍与官方一致。
	set, err := loadCodexThirdPartyProfiles(codexThirdPartyProfilesJSON)
	require.NoError(t, err)
	raw, _, err := set.match("deepseek-flash").buildEntry("deepseek-flash", true)
	require.NoError(t, err)
	cxtpAssertEqualsOfficial(t, "flash/去图片", cxtpGatewayEntry(t, raw), cxtpOfficialEntry(t, "deepseek-flash"), "deepseek-flash",
		"input_modalities", "supports_image_detail_original")
	entry := cxtpGatewayEntry(t, raw)
	require.Equal(t, []any{"text"}, entry["input_modalities"])
	require.Equal(t, false, entry["supports_image_detail_original"])
}

func TestCodexThirdPartyDeepSeekOfficialProfilesLoaderGuards(t *testing.T) {
	load := func(mutate func(profile map[string]any)) error {
		var file map[string]any
		require.NoError(t, json.Unmarshal(codexThirdPartyProfilesJSON, &file))
		profiles, ok := file["profiles"].([]any)
		require.True(t, ok)
		for _, item := range profiles {
			if profile, isMap := item.(map[string]any); isMap && profile["id"] == "deepseek-flash" {
				mutate(profile)
			}
		}
		raw, err := json.Marshal(file)
		require.NoError(t, err)
		_, err = loadCodexThirdPartyProfiles(raw)
		return err
	}
	require.NoError(t, load(func(map[string]any) {}))
	// 官方条目和自写 model_info 不能混用。
	err := load(func(profile map[string]any) { profile["model_info"] = map[string]any{"priority": 50} })
	require.ErrorContains(t, err, "must not write model_info")
	// 指向不存在的官方条目。
	err = load(func(profile map[string]any) { profile["official_entry"] = "no-such-model" })
	require.ErrorContains(t, err, "not found")
	// 指令模板摘要与固定值不一致。
	err = load(func(profile map[string]any) { profile["instructions_template_sha256"] = strings.Repeat("0", 64) })
	require.ErrorContains(t, err, "pinned digest")
}

func TestCodexThirdPartyDeepSeekEntriesPublishedByCatalogEqualOfficial(t *testing.T) {
	// 端到端：经路由、能力档案和目录合并之后，目录里下发的条目与官方一致（例外只有 slug 与 display_name）。
	flashAcct := cxtpOpenAIKeyAccount(105, "https://hub.example.com/v1", map[string]any{"deepseek-flash": "deepseek-flash", "deepseek-v4-pro": "deepseek-v4-pro"})
	svc := newCXTPService(t, &cxtpSettings{cfg: cxtpConfig(1, "deepseek-flash", "deepseek-v4-pro")}, []Account{flashAcct}, nil)
	group := &Group{ID: 8, Platform: PlatformOpenAI}
	body, err := svc.ApplyToBody(context.Background(), group, []byte(`{"models":[]}`))
	require.NoError(t, err)
	require.ElementsMatch(t, []string{"deepseek-flash", "deepseek-v4-pro"}, cxtpSlugs(t, body))
	for _, slug := range []string{"deepseek-flash", "deepseek-v4-pro"} {
		_, entries, perr := parseCodexCatalogBody(body)
		require.NoError(t, perr)
		for _, entry := range entries {
			if entry.slug == slug {
				cxtpAssertEqualsOfficial(t, "目录/"+slug, cxtpGatewayEntry(t, entry.raw), cxtpOfficialEntry(t, slug), slug)
			}
		}
	}
}
