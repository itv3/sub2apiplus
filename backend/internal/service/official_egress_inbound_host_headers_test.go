package service

import (
	"context"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openaiidentity"
	"github.com/stretchr/testify/require"
)

// inboundHostHeaderSamples 是真实第三方客户端（VSCode 系 IDE、Stainless SDK）会携带，
// 而官方 Claude Code / Codex CLI 从不发送的宿主环境头。
var inboundHostHeaderSamples = map[string]string{
	"accept-language":           "zh-CN,zh;q=0.9,en-US;q=0.8",
	"sec-fetch-mode":            "cors",
	"x-stainless-helper-method": "stream",
}

func requireNoInboundHostHeaders(t *testing.T, header http.Header) {
	t.Helper()
	for name := range inboundHostHeaderSamples {
		require.Empty(
			t,
			header.Get(name),
			"官方出站不得携带入站宿主环境头 %s（官方客户端从不发送）",
			name,
		)
	}
}

// 第三方客户端入站（derived 路径）：宿主头必须被剥离，官方身份头必须保留。
func TestOfficialEgressOpenAIHTTPStripsInboundHostHeadersFromThirdParty(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":true,
		"messages":[{"role":"user","content":"HOST_HEADER_STRIP_OK"}]
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "host-header-strip-session")
	for name, value := range inboundHostHeaderSamples {
		c.Request.Header.Set(name, value)
	}
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_host_header_strip"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).ForwardAsChatCompletions(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"",
		"gpt-5.6-luna",
	)

	require.NoError(t, err)
	require.NotNil(t, upstream.lastReq)
	requireNoInboundHostHeaders(t, upstream.lastReq.Header)
	require.Equal(t, openaiidentity.CodexUserAgent, upstream.lastReq.Header.Get("User-Agent"))
	require.Equal(t, openaiidentity.CodexOriginator, upstream.lastReq.Header.Get("originator"))
}

// 官方客户端入站（strict 路径）同样要剥离：strict 只跳过 Body 顶层归一化，不豁免 Header 定型。
func TestOfficialEgressOpenAIHTTPStripsInboundHostHeadersFromOfficialClient(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	for name, value := range inboundHostHeaderSamples {
		c.Request.Header.Set(name, value)
	}
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_host_header_strip_official"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
	)

	require.NoError(t, err)
	require.NotNil(t, upstream.lastReq)
	requireNoInboundHostHeaders(t, upstream.lastReq.Header)
	require.Equal(t, openaiidentity.CodexUserAgent, upstream.lastReq.Header.Get("User-Agent"))
}

// WebSocket 握手头与 HTTP 对称：宿主头不得随握手上行。
func TestOfficialEgressOpenAIWSHandshakeStripsInboundHostHeaders(t *testing.T) {
	ctx, _, _, _ := newOfficialOpenAIWSContextForTest(t)
	headers := http.Header{
		"User-Agent": []string{"Kilo-Code/7.4.0"},
		"Originator": []string{"third-party"},
	}
	for name, value := range inboundHostHeaderSamples {
		headers.Set(name, value)
	}

	_, err := finalizeOpenAIOfficialEgressWSHandshakeHeaders(ctx, headers)

	require.NoError(t, err)
	requireNoInboundHostHeaders(t, headers)
	require.Equal(t, openaiidentity.CodexUserAgent, headers.Get("User-Agent"))
	require.Equal(t, openaiidentity.CodexOriginator, headers.Get("originator"))
}

// 官方 compact 请求不压缩：RequestCompression 默认 None，compact 的 execute_with
// 不传压缩选项。普通 Responses 才走 zstd。
func TestOfficialEgressCompactRequestIsNotCompressed(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_compress_probe"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(), c, newOfficialOpenAIHTTPTestAccount(94), body,
	)
	require.NoError(t, err)
	require.NotNil(t, upstream.lastReq)
	require.Equal(t, "zstd", upstream.lastReq.Header.Get("Content-Encoding"),
		"普通 Responses 必须保持 zstd 压缩")
}
