package service

import (
	"context"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func BenchmarkOfficialEgressWSFrameFinalizer(b *testing.B) {
	payload := []byte(`{"type":"response.create","model":"gpt-5.6-luna","tool_choice":"auto","parallel_tool_calls":false,"reasoning":{"effort":"high","summary":"auto"},"store":false,"stream":true,"include":["reasoning.encrypted_content"],"client_metadata":{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"` +
		strings.Repeat("官方出口性能样本", 8192) +
		`"}]}]}`)
	enabledContext := newOfficialEgressBenchmarkWSContext(b)

	b.Run("无画像上下文", func(b *testing.B) {
		b.ReportAllocs()
		for range b.N {
			_, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(
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
			_, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(
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

// BenchmarkOfficialCodexVersionProfileResolve 守护画像解析的只读单例契约。
// 改造前每次解析都要重新解码 48 KB 快照、执行全量结构校验并重算 SHA-256，
// 实测约 449 μs / 239 KB / 1790 次分配，而单条 HTTP 定型链路会重复触发十余次。
// 命中编译缓存后这里应当退化为常数级查表，不再产生任何分配。
func BenchmarkOfficialCodexVersionProfileResolve(b *testing.B) {
	b.ReportAllocs()
	for range b.N {
		if _, err := resolveCodexVersionProfile(officialCodexVersion0145); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkOfficialCodexEndpointResolve 覆盖端点级深拷贝。端点是执行器唯一会
// 就地改写的画像数据，必须继续返回可安全改写的副本；这里守护的是该副本不再
// 经由 JSON 编解码产生。
func BenchmarkOfficialCodexEndpointResolve(b *testing.B) {
	b.ReportAllocs()
	for range b.N {
		if _, err := resolveCodexEndpoint(
			officialCodexVersion0145,
			codexEndpointID(officialCodexEndpointResponsesHTTP),
		); err != nil {
			b.Fatal(err)
		}
	}
}
