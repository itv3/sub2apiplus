package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAccountSupportsCompositeModelListProtocol(t *testing.T) {
	t.Parallel()

	testCases := []struct {
		name          string
		account       *Account
		wantOpenAI    bool
		wantAnthropic bool
	}{
		{
			name:          "Anthropic 平台",
			account:       &Account{Platform: PlatformAnthropic},
			wantAnthropic: true,
		},
		{
			name:       "OpenAI 平台",
			account:    &Account{Platform: PlatformOpenAI},
			wantOpenAI: true,
		},
		{
			name:       "Grok 平台",
			account:    &Account{Platform: PlatformGrok},
			wantOpenAI: true,
		},
		{
			name: "智谱 Anthropic 协议",
			account: &Account{
				Platform:    PlatformZhipu,
				Credentials: map[string]any{"api_protocol": APIProtocolAnthropic},
			},
			wantAnthropic: true,
		},
		{
			name: "Kimi Chat Completions 协议",
			account: &Account{
				Platform:    PlatformKimi,
				Credentials: map[string]any{"api_protocol": APIProtocolChatCompletions},
			},
			wantOpenAI: true,
		},
		{
			name: "DeepSeek Responses 协议",
			account: &Account{
				Platform:    PlatformDeepseek,
				Credentials: map[string]any{"api_protocol": APIProtocolResponses},
			},
			wantOpenAI: true,
		},
		{
			name: "国产供应商 Adaptive 协议",
			account: &Account{
				Platform:    PlatformZhipu,
				Credentials: map[string]any{"api_protocol": APIProtocolAdaptive},
			},
			wantOpenAI:    true,
			wantAnthropic: true,
		},
		{
			name:       "国产供应商缺省协议",
			account:    &Account{Platform: PlatformKimi},
			wantOpenAI: true,
		},
		{
			name:    "Gemini 平台不进入两类目录",
			account: &Account{Platform: PlatformGemini},
		},
		{
			name:    "Antigravity 平台不进入两类目录",
			account: &Account{Platform: PlatformAntigravity},
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			require.Equal(t, testCase.wantOpenAI, accountSupportsCompositeModelListProtocol(testCase.account, CompositeModelListProtocolOpenAI))
			require.Equal(t, testCase.wantAnthropic, accountSupportsCompositeModelListProtocol(testCase.account, CompositeModelListProtocolAnthropic))
		})
	}
}
