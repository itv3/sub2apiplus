package service

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestOfficialOpenAIReasoningDefaultsFollowModelManifest(t *testing.T) {
	snapshot := &openAIModelCapabilitySnapshot{}
	snapshot.replaceFromManifest(91, []byte(`{
		"models":[{
			"slug":"gpt-profile-test",
			"visibility":"list",
			"use_responses_lite":true,
			"supports_parallel_tool_calls":true,
			"default_reasoning_level":"low",
			"default_reasoning_summary":"none",
			"supports_reasoning_summary_parameter":true
		}]
	}`))

	capabilities, known, _ := snapshot.modelCapabilitiesState(
		91,
		"gpt-profile-test",
		time.Now(),
	)
	require.True(t, known)
	require.True(t, capabilities.ReasoningDefaultsKnown)
	require.Equal(t, "low", capabilities.DefaultReasoningLevel)
	require.Equal(t, "none", capabilities.DefaultReasoningSummary)
	require.True(t, capabilities.SupportsReasoningSummaryParameter)
}

func TestNormalizeDerivedOfficialOpenAIReasoningUsesFrozenDefaults(t *testing.T) {
	tests := []struct {
		name        string
		payload     map[string]any
		defaults    officialOpenAIReasoningDefaults
		wantEffort  string
		wantSummary string
	}{
		{
			name:       "模型默认 effort 且 summary none",
			payload:    map[string]any{},
			defaults:   officialOpenAIReasoningDefaults{Effort: "low", Summary: "none", SupportsSummary: true, Known: true},
			wantEffort: "low",
		},
		{
			name:        "未知模型采用官方 fallback summary",
			payload:     map[string]any{},
			defaults:    officialOpenAIReasoningDefaults{Summary: "auto", SupportsSummary: true, Known: true},
			wantSummary: "auto",
		},
		{
			name:       "ultra 映射为 wire max",
			payload:    map[string]any{"reasoning": map[string]any{"effort": "ultra", "summary": "none"}},
			defaults:   officialOpenAIReasoningDefaults{SupportsSummary: true, Known: true},
			wantEffort: "max",
		},
		{
			name:       "模型不支持 summary 时省略",
			payload:    map[string]any{"reasoning": map[string]any{"effort": "high", "summary": "detailed"}},
			defaults:   officialOpenAIReasoningDefaults{Known: true},
			wantEffort: "high",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			modified, err := normalizeDerivedOfficialOpenAIReasoning(tt.payload, tt.defaults)
			require.NoError(t, err)
			require.True(t, modified)
			reasoning, ok := tt.payload["reasoning"].(map[string]any)
			require.True(t, ok)
			if tt.wantEffort == "" {
				require.NotContains(t, reasoning, "effort")
			} else {
				require.Equal(t, tt.wantEffort, reasoning["effort"])
			}
			if tt.wantSummary == "" {
				require.NotContains(t, reasoning, "summary")
			} else {
				require.Equal(t, tt.wantSummary, reasoning["summary"])
			}
		})
	}
}
