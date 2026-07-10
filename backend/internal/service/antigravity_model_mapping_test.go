//go:build unit

package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAntigravityGatewayService_GetMappedModel(t *testing.T) {
	svc := &AntigravityGatewayService{}

	tests := []struct {
		name           string
		requestedModel string
		accountMapping map[string]string
		expected       string
	}{
		// 1. 账户级映射优先
		{
			name:           "账户映射优先",
			requestedModel: "claude-3-5-sonnet-20241022",
			accountMapping: map[string]string{
				"claude-3-5-sonnet-20241022": "custom-model",
				"custom-model":               "custom-model",
			},
			expected: "custom-model",
		},
		{
			name:           "账户映射 - 可覆盖默认映射的模型",
			requestedModel: "claude-sonnet-4-5",
			accountMapping: map[string]string{
				"claude-sonnet-4-5": "my-custom-sonnet",
				"my-custom-sonnet":  "my-custom-sonnet",
			},
			expected: "my-custom-sonnet",
		},
		{
			name:           "账户映射 - 可覆盖未知模型",
			requestedModel: "claude-opus-4",
			accountMapping: map[string]string{
				"claude-opus-4": "my-opus",
				"my-opus":       "my-opus",
			},
			expected: "my-opus",
		},
		{
			name:           "账户映射 - 目标未被白名单允许时拒绝",
			requestedModel: "claude-3-5-sonnet-20241022",
			accountMapping: map[string]string{"claude-3-5-sonnet-20241022": "custom-model"},
			expected:       "",
		},

		// 2. 默认映射（DefaultAntigravityModelMapping）
		{
			name:           "默认映射 - claude-opus-4-6 → claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-6",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "默认映射 - claude-opus-4-5-20251101 → claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-5-20251101",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "默认映射 - claude-opus-4-5-thinking → claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-5-thinking",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "默认映射 - claude-haiku-4-5 → claude-sonnet-4-6",
			requestedModel: "claude-haiku-4-5",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "默认映射 - claude-haiku-4-5-20251001 → claude-sonnet-4-6",
			requestedModel: "claude-haiku-4-5-20251001",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "默认映射 - claude-sonnet-4-5-20250929 → claude-sonnet-4-6",
			requestedModel: "claude-sonnet-4-5-20250929",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},

		// 3. 官方模型透传 + 历史入口兼容映射
		{
			name:           "兼容映射 - claude-fable-5 → claude-sonnet-4-6",
			requestedModel: "claude-fable-5",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "默认映射透传 - claude-sonnet-4-6",
			requestedModel: "claude-sonnet-4-6",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "兼容映射 - claude-sonnet-4-5 → claude-sonnet-4-6",
			requestedModel: "claude-sonnet-4-5",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "兼容映射 - claude-opus-4-8 → claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-8",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "兼容映射 - claude-opus-4-7 → claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-7",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "默认映射透传 - claude-opus-4-6-thinking",
			requestedModel: "claude-opus-4-6-thinking",
			accountMapping: nil,
			expected:       "claude-opus-4-6-thinking",
		},
		{
			name:           "兼容映射 - claude-sonnet-4-5-thinking → claude-sonnet-4-6",
			requestedModel: "claude-sonnet-4-5-thinking",
			accountMapping: nil,
			expected:       "claude-sonnet-4-6",
		},
		{
			name:           "兼容映射 - gemini-2.5-flash → gemini-3.5-flash-low",
			requestedModel: "gemini-2.5-flash",
			accountMapping: nil,
			expected:       "gemini-3.5-flash-low",
		},
		{
			name:           "兼容映射 - gemini-2.5-pro → gemini-pro-agent",
			requestedModel: "gemini-2.5-pro",
			accountMapping: nil,
			expected:       "gemini-pro-agent",
		},
		{
			name:           "兼容映射 - gemini-3-flash → gemini-3-flash-agent",
			requestedModel: "gemini-3-flash",
			accountMapping: nil,
			expected:       "gemini-3-flash-agent",
		},
		{
			name:           "默认映射透传 - gemini-3.5-flash-extra-low",
			requestedModel: "gemini-3.5-flash-extra-low",
			accountMapping: nil,
			expected:       "gemini-3.5-flash-extra-low",
		},
		{
			name:           "默认映射透传 - gemini-3.5-flash-low",
			requestedModel: "gemini-3.5-flash-low",
			accountMapping: nil,
			expected:       "gemini-3.5-flash-low",
		},
		{
			name:           "默认映射透传 - gemini-3-flash-agent",
			requestedModel: "gemini-3-flash-agent",
			accountMapping: nil,
			expected:       "gemini-3-flash-agent",
		},
		{
			name:           "默认映射透传 - gpt-oss-120b-medium",
			requestedModel: "gpt-oss-120b-medium",
			accountMapping: nil,
			expected:       "gpt-oss-120b-medium",
		},

		// 4. 未在默认映射中的模型返回空字符串（不支持）
		{
			name:           "未知模型 - claude-unknown 返回空",
			requestedModel: "claude-unknown",
			accountMapping: nil,
			expected:       "",
		},
		{
			name:           "未知模型 - claude-3-5-sonnet-20241022 返回空（未在默认映射）",
			requestedModel: "claude-3-5-sonnet-20241022",
			accountMapping: nil,
			expected:       "",
		},
		{
			name:           "未知模型 - claude-3-opus-20240229 返回空",
			requestedModel: "claude-3-opus-20240229",
			accountMapping: nil,
			expected:       "",
		},
		{
			name:           "未知模型 - claude-opus-4 返回空",
			requestedModel: "claude-opus-4",
			accountMapping: nil,
			expected:       "",
		},
		{
			name:           "未知模型 - gemini-future-model 返回空",
			requestedModel: "gemini-future-model",
			accountMapping: nil,
			expected:       "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			account := &Account{
				Platform: PlatformAntigravity,
			}
			if tt.accountMapping != nil {
				// GetModelMapping 期望 model_mapping 是 map[string]any 格式
				mappingAny := make(map[string]any)
				for k, v := range tt.accountMapping {
					mappingAny[k] = v
				}
				account.Credentials = map[string]any{
					"model_mapping": mappingAny,
				}
			}

			got := svc.getMappedModel(account, tt.requestedModel)
			require.Equal(t, tt.expected, got, "model: %s", tt.requestedModel)
		})
	}
}

func TestAntigravityGatewayService_GetMappedModel_EdgeCases(t *testing.T) {
	svc := &AntigravityGatewayService{}

	tests := []struct {
		name           string
		requestedModel string
		expected       string
	}{
		// 空字符串和非 claude/gemini 前缀返回空字符串
		{"空字符串", "", ""},
		{"非claude/gemini前缀 - gpt", "gpt-4", ""},
		{"非claude/gemini前缀 - llama", "llama-3", ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			account := &Account{Platform: PlatformAntigravity}
			got := svc.getMappedModel(account, tt.requestedModel)
			require.Equal(t, tt.expected, got)
		})
	}
}

func TestAntigravityGatewayService_IsModelSupported(t *testing.T) {
	svc := &AntigravityGatewayService{}

	tests := []struct {
		name     string
		model    string
		expected bool
	}{
		// 直接支持
		{"直接支持 - claude-fable-5", "claude-fable-5", true},
		{"直接支持 - claude-sonnet-4-5", "claude-sonnet-4-5", true},
		{"直接支持 - gemini-3-flash", "gemini-3-flash", true},
		{"直接支持 - gemini-3-flash-agent", "gemini-3-flash-agent", true},
		{"直接支持 - gpt-oss-120b-medium", "gpt-oss-120b-medium", true},

		// 可映射（有明确前缀映射）
		{"可映射 - claude-opus-4-8", "claude-opus-4-8", true},
		{"可映射 - claude-opus-4-6", "claude-opus-4-6", true},

		// 不支持
		{"不支持 - 未知Gemini", "gemini-unknown", false},
		{"不支持 - 未知Claude", "claude-unknown", false},
		{"不支持 - gpt-4", "gpt-4", false},
		{"不支持 - 空字符串", "", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := svc.IsModelSupported(tt.model)
			require.Equal(t, tt.expected, got)
		})
	}
}

func TestAdvertisedModelMappingForAccount_AntigravityUsesExplicitWhitelistModels(t *testing.T) {
	account := &Account{
		Platform: PlatformAntigravity,
		Credentials: map[string]any{
			"model_mapping": map[string]any{
				"gemini-future-pro":      "gemini-future-pro",
				"Gemini Future Pro":      "gemini-future-pro",
				"gemini-3.1-pro-high":    "gemini-pro-agent",
				"claude-sonnet-4-6":      "claude-sonnet-4-6",
				"  gemini-future-lite  ": "  gemini-future-lite  ",
				"models/gemini-path-pro": "gemini-path-pro",
				"models/gemini-path-max": "models/gemini-path-max",
			},
		},
	}

	mapping := AdvertisedModelMappingForAccount(account)

	require.Equal(t, "gemini-future-pro", mapping["gemini-future-pro"])
	require.Equal(t, "gemini-future-lite", mapping["gemini-future-lite"])
	require.Equal(t, "gemini-path-pro", mapping["gemini-path-pro"])
	require.Equal(t, "gemini-path-max", mapping["gemini-path-max"])
	require.NotContains(t, mapping, "models/gemini-path-pro")
	require.NotContains(t, mapping, "models/gemini-path-max")
	require.NotContains(t, mapping, "Gemini Future Pro")
	require.NotContains(t, mapping, "gemini-3.1-pro-high")
	require.NotContains(t, mapping, "gemini-3.5-flash-low")
}

func TestAdvertisedModelMappingForAccount_AntigravityDefaultsOnlyForMissingOrEmptyMapping(t *testing.T) {
	for _, account := range []*Account{
		{Platform: PlatformAntigravity},
		{Platform: PlatformAntigravity, Credentials: map[string]any{}},
		{Platform: PlatformAntigravity, Credentials: map[string]any{"model_mapping": map[string]any{}}},
	} {
		mapping := AdvertisedModelMappingForAccount(account)
		require.Len(t, mapping, len(defaultAntigravityModelIDs()))
		for _, model := range defaultAntigravityModelIDs() {
			require.Equal(t, model, mapping[model])
		}
	}

	malformed := &Account{
		Platform: PlatformAntigravity,
		Credentials: map[string]any{
			"model_mapping": map[string]any{"gemini-pro-agent": 123},
		},
	}
	require.Empty(t, AdvertisedModelMappingForAccount(malformed))
	require.False(t, isAntigravityAllowedModel(malformed, "gemini-pro-agent"))
}

func TestResolveAntigravityFallbackModelUsesAccountAllowedModels(t *testing.T) {
	defaultAccount := &Account{Platform: PlatformAntigravity}
	require.Equal(t, "gemini-3.5-flash-low", resolveAntigravityFallbackModel(defaultAccount, "gemini-3.5-flash-low", "gemini-pro-agent"))
	require.Empty(t, resolveAntigravityFallbackModel(defaultAccount, "gemini-future-pro", "gemini-pro-agent"))
	require.Empty(t, resolveAntigravityFallbackModel(defaultAccount, "gemini-3.5-flash-low", "gemini-3.5-flash-low"))

	customAccount := &Account{
		Platform: PlatformAntigravity,
		Credentials: map[string]any{
			"model_mapping": map[string]any{
				"gemini-future-pro": "gemini-future-pro",
				"Future Pro":        "gemini-future-pro",
			},
		},
	}
	require.Equal(t, "gemini-future-pro", resolveAntigravityFallbackModel(customAccount, "gemini-future-pro", "gemini-pro-agent"))
	require.Equal(t, "gemini-future-pro", resolveAntigravityFallbackModel(customAccount, "Future Pro", "gemini-pro-agent"))
	require.Empty(t, resolveAntigravityFallbackModel(customAccount, "gemini-3.5-flash-low", "gemini-pro-agent"))
}

func TestMapAntigravityModel_RequiresAllowedTarget(t *testing.T) {
	tests := []struct {
		name           string
		modelMapping   map[string]any
		requestedModel string
		expected       string
	}{
		{
			name:           "wildcard target official model",
			modelMapping:   map[string]any{"claude-*": "claude-sonnet-4-6"},
			requestedModel: "claude-opus-4-6",
			expected:       "",
		},
		{
			name:           "wildcard target requires self map",
			modelMapping:   map[string]any{"gemini-*": "gemini-future-pro"},
			requestedModel: "gemini-future-pro",
			expected:       "",
		},
		{
			name: "wildcard target allowed by self map",
			modelMapping: map[string]any{
				"gemini-*":          "gemini-future-pro",
				"gemini-future-pro": "gemini-future-pro",
			},
			requestedModel: "gemini-future-lite",
			expected:       "gemini-future-pro",
		},
		{
			name:           "legacy alias self map is not injected into allowed set",
			modelMapping:   map[string]any{"gemini-future-pro": "gemini-future-pro"},
			requestedModel: "gemini-3.1-pro-high",
			expected:       "",
		},
		{
			name:           "wildcard target legacy alias rejected without self map",
			modelMapping:   map[string]any{"claude-*": "claude-sonnet-4-5"},
			requestedModel: "claude-opus-4-6",
			expected:       "",
		},
		{
			name:           "wildcard no match",
			modelMapping:   map[string]any{"claude-*": "claude-sonnet-4-5"},
			requestedModel: "gpt-4o",
			expected:       "",
		},
		{
			name:           "explicit passthrough same name",
			modelMapping:   map[string]any{"claude-sonnet-4-5": "claude-sonnet-4-5"},
			requestedModel: "claude-sonnet-4-5",
			expected:       "claude-sonnet-4-5",
		},
		{
			name:           "multiple wildcards target compatibility alias rejected",
			modelMapping:   map[string]any{"claude-*": "claude-sonnet-4-6", "gemini-*": "gemini-2.5-flash"},
			requestedModel: "gemini-2.5-flash",
			expected:       "",
		},
		{
			name:           "customtools alias target official model",
			modelMapping:   map[string]any{"gemini-3.1-pro-preview": "gemini-pro-agent"},
			requestedModel: "gemini-3.1-pro-preview-customtools",
			expected:       "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			account := &Account{
				Platform: PlatformAntigravity,
				Credentials: map[string]any{
					"model_mapping": tt.modelMapping,
				},
			}
			got := mapAntigravityModel(account, tt.requestedModel)
			require.Equal(t, tt.expected, got, "mapAntigravityModel(%q) = %q, want %q", tt.requestedModel, got, tt.expected)
		})
	}
}
