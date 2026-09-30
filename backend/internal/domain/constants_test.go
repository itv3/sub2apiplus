package domain

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"
)

func TestDefaultAntigravityModelMapping_ContainsOnlyOfficialModels(t *testing.T) {
	t.Parallel()

	cases := antigravity.OfficialModelMapping()

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

func TestAntigravityCompatibilityModelMapping_MigratesLegacySonnet45Aliases(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"claude-sonnet-4-5":          "claude-sonnet-4-6",
		"claude-sonnet-4-5-thinking": "claude-sonnet-4-6",
		"claude-sonnet-4-5-20250929": "claude-sonnet-4-6",
	}
	for model, want := range cases {
		if got := AntigravityCompatibilityModelMapping[model]; got != want {
			t.Fatalf("expected model %q to map to %q, got %q", model, want, got)
		}
	}
}

func TestAntigravityCompatibilityModelMapping_Gemini36FlashModels(t *testing.T) {
	for _, model := range []string{"gemini-3.6-flash", "gemini-3.6-flash-high", "gemini-3.6-flash-low", "gemini-3.6-flash-medium", "gemini-3.6-flash-tiered"} {
		if got := AntigravityCompatibilityModelMapping[model]; got != model {
			t.Fatalf("expected %s to map to itself, got %q", model, got)
		}
	}
}

func TestAntigravityCompatibilityModelMapping_Gemini37FlashModels(t *testing.T) {
	for _, model := range []string{"gemini-3.7-flash", "gemini-3.7-flash-high", "gemini-3.7-flash-low", "gemini-3.7-flash-medium", "gemini-3.7-flash-tiered"} {
		if got := AntigravityCompatibilityModelMapping[model]; got != model {
			t.Fatalf("expected %s to map to itself, got %q", model, got)
		}
	}
}

func TestAntigravityCompatibilityModelMapping_Gemini38FlashModels(t *testing.T) {
	for _, model := range []string{"gemini-3.8-flash", "gemini-3.8-flash-high", "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-tiered"} {
		if got := AntigravityCompatibilityModelMapping[model]; got != model {
			t.Fatalf("expected %s to map to itself, got %q", model, got)
		}
	}
}

func TestDefaultBedrockModelMapping_ContainsNewClaudeModels(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"claude-fable-5-1":  "anthropic.claude-fable-5-1",
		"claude-fable-5":    "anthropic.claude-fable-5",
		"claude-opus-4-8":   "us.anthropic.claude-opus-4-8-v1",
		"claude-sonnet-5-5": "global.anthropic.claude-sonnet-5-5",
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
