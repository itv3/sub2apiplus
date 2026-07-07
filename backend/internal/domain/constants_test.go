package domain

import "testing"

func TestDefaultAntigravityModelMapping_ContainsOnlyOfficialModels(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"claude-opus-4-6-thinking":       "claude-opus-4-6-thinking",
		"claude-sonnet-4-6":              "claude-sonnet-4-6",
		"gemini-3-flash-agent":           "gemini-3-flash-agent",
		"gemini-3.1-pro-low":             "gemini-3.1-pro-low",
		AntigravityGemini31ProAgentModel: AntigravityGemini31ProAgentModel,
		"gemini-3.5-flash-extra-low":     "gemini-3.5-flash-extra-low",
		"gemini-3.5-flash-low":           "gemini-3.5-flash-low",
		"gpt-oss-120b-medium":            "gpt-oss-120b-medium",
	}

	if len(DefaultAntigravityModelMapping) != len(cases) {
		t.Fatalf("default mapping size = %d, want %d", len(DefaultAntigravityModelMapping), len(cases))
	}
	for from, want := range cases {
		got, ok := DefaultAntigravityModelMapping[from]
		if !ok {
			t.Fatalf("expected mapping for %q to exist", from)
		}
		if got != want {
			t.Fatalf("unexpected mapping for %q: got %q want %q", from, got, want)
		}
	}
}

func TestAntigravityCompatibilityModelMapping_KeepsHistoricalAliases(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"claude-fable-5":                 "claude-sonnet-4-6",
		"claude-opus-4-8":                "claude-opus-4-6-thinking",
		"gemini-2.5-pro":                 AntigravityGemini31ProAgentModel,
		"gemini-3.1-pro":                 AntigravityGemini31ProAgentModel,
		"gemini-3.1-pro-high":            AntigravityGemini31ProAgentModel,
		"gemini-3.1-pro-preview":         AntigravityGemini31ProAgentModel,
		"gemini-3.1-flash-image-preview": "gemini-3-flash-agent",
		"tab_flash_lite_preview":         "gemini-3.5-flash-extra-low",
	}

	for from, want := range cases {
		got, ok := AntigravityCompatibilityModelMapping[from]
		if !ok {
			t.Fatalf("expected mapping for %q to exist", from)
		}
		if got != want {
			t.Fatalf("unexpected mapping for %q: got %q want %q", from, got, want)
		}
	}
}

func TestDefaultBedrockModelMapping_ContainsNewClaudeModels(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"claude-fable-5":  "anthropic.claude-fable-5",
		"claude-opus-4-8": "us.anthropic.claude-opus-4-8-v1",
	}
	for from, want := range cases {
		got, ok := DefaultBedrockModelMapping[from]
		if !ok {
			t.Fatalf("expected Bedrock mapping for %q to exist", from)
		}
		if got != want {
			t.Fatalf("unexpected Bedrock mapping for %q: got %q want %q", from, got, want)
		}
	}
}
