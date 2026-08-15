package service

import (
	"context"
	"net/http"
	"testing"

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
	require.Equal(t, activeOpenAICodexUserAgentForTest(), upstream.lastReq.Header.Get("User-Agent"))
	require.Equal(t, activeOpenAICodexProfileForTest().Build.Originator, upstream.lastReq.Header.Get("originator"))
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
	require.Equal(t, activeOpenAICodexUserAgentForTest(), upstream.lastReq.Header.Get("User-Agent"))
}

// 入站未声明压缩时，HTTP Responses 仍使用 active 画像默认压缩。
func TestOfficialEgressResponsesUsesProfileCompressionDefault(t *testing.T) {
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
	require.Equal(t, "zstd", upstream.lastReq.Header.Get("Content-Encoding"))
}

// 官方进程默认开启压缩时，入站 zstd feature 状态必须在终态重新编码后保留。
func TestOfficialEgressResponsesRespectsCompressionFeatureOn(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	c.Request.Header.Set("Content-Encoding", "zstd")
	// 生产 body reader 会在解压后删除 Content-Encoding；画像必须消费此前冻结的
	// 调用级 feature 快照，而不是在 Finalizer 阶段重新读取已变化的 header。
	c.Request = c.Request.WithContext(
		WithOfficialCodexIngressRuntime(c.Request.Context(), c),
	)
	c.Request.Header.Del("Content-Encoding")
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_compress_probe_on"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).Forward(
		context.Background(), c, newOfficialOpenAIHTTPTestAccount(94), body,
	)
	require.NoError(t, err)
	require.NotNil(t, upstream.lastReq)
	require.Equal(t, "zstd", upstream.lastReq.Header.Get("Content-Encoding"))
}
