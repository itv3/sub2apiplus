package service

import (
	"context"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func BenchmarkOfficialEgressWSFrameFinalizer(b *testing.B) {
	payload := []byte(`{"type":"response.create","model":"gpt-5.6-luna","client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"` +
		strings.Repeat("官方出口性能样本", 8192) +
		`"}]}]}`)
	enabledContext := newOfficialEgressBenchmarkWSContext(b)

	b.Run("无画像上下文", func(b *testing.B) {
		b.ReportAllocs()
		for range b.N {
			_, _, err := finalizeOpenAIOfficialEgressWSFrame(
				context.Background(),
				payload,
				payload,
				"",
				false,
			)
			if err != nil {
				b.Fatal(err)
			}
		}
	})
	b.Run("内置画像且帧未变化", func(b *testing.B) {
		b.ReportAllocs()
		for range b.N {
			_, _, err := finalizeOpenAIOfficialEgressWSFrame(
				enabledContext,
				payload,
				payload,
				"",
				false,
			)
			if err != nil {
				b.Fatal(err)
			}
		}
	})
}

func newOfficialEgressBenchmarkWSContext(b *testing.B) context.Context {
	b.Helper()
	account := officialEgressTestAccount(94, PlatformOpenAI)
	egressContext := newOfficialEgressTestContext(
		account,
		OfficialEgressTransportWebSocket,
		"/v1/responses",
		"chatgpt.com",
	)
	profile, err := (DefaultOfficialEgressProfileResolver{}).ResolveWebSocketProfile(
		egressContext,
		account,
		"/v1/responses",
	)
	require.NoError(b, err)
	frozenContext, err := egressContext.Freeze()
	require.NoError(b, err)
	require.NoError(b, ValidateOfficialEgressFinalState(frozenContext, profile))
	return WithOfficialEgressContext(context.Background(), frozenContext)
}
