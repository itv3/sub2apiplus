//go:build unit

package service

// 自定义多协议平台（custom）的 Responses 输出上限保留回归测试。
//
// 背景：非 Codex 客户端用 /v1/responses 请求 custom 账号并带 max_output_tokens 时，
// 转发器曾在发往上游前把该字段删掉（custom 落入平台 switch 的 default 分支），上游按默认
// 上限输出，客户端设的长度限制不生效。现在 custom 与 openai、deepseek 一样保留该字段，
// Ollama Cloud 出站地址上的 DeepSeek 系模型仍由独立的 clamp 钩子按账号上限夹紧。
//
// 这里用「假上游」（httpUpstreamRecorder）接住真实 Forward 发出的请求并断言上游收到的
// 请求体；不使用连接测试，也不直接调用 clamp 函数。clamp 的判定依赖 ollama.com 主机名，
// 无法指向本机监听的 httptest 服务，所以假上游在 HTTPUpstream 接口层截获请求。

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai_compat"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const (
	// customMaxOutputModel 是 DeepSeek 系模型名：Ollama Cloud 的原生 Responses clamp 只对
	// DeepSeek 系出站模型生效。
	customMaxOutputModel = "deepseek-v4-flash"
	// customMaxOutputOllamaResponsesBase 是实测使用的 Ollama Cloud Responses 地址。
	customMaxOutputOllamaResponsesBase = "https://ollama.com/v1"
	// customMaxOutputRelayResponsesBase 是非 Ollama 的第三方中转地址（不夹紧）。
	customMaxOutputRelayResponsesBase = "http://responses.example/v1"
)

// customMaxOutputResponsesBody 构造非 Codex 客户端的 Responses 请求体；value 为 nil 时不带 max_output_tokens。
func customMaxOutputResponsesBody(value any) []byte {
	body := map[string]any{
		"model":  customMaxOutputModel,
		"input":  "Reply with exactly OK and nothing else.",
		"stream": false,
	}
	if value != nil {
		body["max_output_tokens"] = value
	}
	return customMaxOutputMarshal(body)
}

// customMaxOutputMarshal 序列化测试请求体，失败直接 panic（测试数据是写死的，不应失败）。
func customMaxOutputMarshal(v any) []byte {
	out, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return out
}

// customMaxOutputAccountBuilders 列出所有「保留 max_output_tokens」的平台账号构造器：
// custom（本次新增）与既有的 openai（force_responses）、deepseek（api_protocol=responses）。
// 三者在同一个 Responses 上游地址上必须得到完全一致的转发体。
func customMaxOutputAccountBuilders(responsesBase string) map[string]func(cap any) *Account {
	withCap := func(account *Account, cap any) *Account {
		if cap != nil {
			if account.Extra == nil {
				account.Extra = map[string]any{}
			}
			account.Extra[OllamaCloudMaxTokensCapExtraKey] = cap
		}
		return account
	}
	return map[string]func(cap any) *Account{
		"custom": func(cap any) *Account {
			account := customTestAccount(map[string]any{
				APIProtocolChatCompletions: responsesBase,
				APIProtocolResponses:       responsesBase,
			})
			return withCap(account, cap)
		},
		"deepseek": func(cap any) *Account {
			account := ollamaUpstreamTestAccount(PlatformDeepseek, 931)
			account.Credentials["base_url"] = responsesBase
			account.Credentials["api_protocol"] = APIProtocolResponses
			return withCap(account, cap)
		},
		"openai": func(cap any) *Account {
			account := ollamaUpstreamTestAccount(PlatformOpenAI, 932)
			account.Credentials["base_url"] = responsesBase
			account.Extra[openai_compat.ExtraKeyResponsesMode] = string(openai_compat.ResponsesSupportModeForceResponses)
			return withCap(account, cap)
		},
	}
}

// forwardCustomMaxOutput 经真实 Forward 把 body 打到假上游，返回上游收到的请求体与出站地址。
func forwardCustomMaxOutput(t *testing.T, account *Account, body []byte, prepare func(*gin.Context), forceCodexCLI bool) *httpUpstreamRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	upstream := &httpUpstreamRecorder{err: errors.New("stop after capture")}
	cfg := rawChatCompletionsTestConfig()
	cfg.Gateway.ForceCodexCLI = forceCodexCLI
	svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream}
	c := adaptiveProtocolTestContext("/v1/responses", body)
	if prepare != nil {
		prepare(c)
	}
	_, err := svc.Forward(context.Background(), c, account, body)
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq, "请求应已发往假上游")
	return upstream
}

// TestForwardResponsesKeepsMaxOutputTokensForPreservingPlatforms 覆盖验收矩阵：
// 非 Codex 客户端 + Responses 入站 + Ollama Cloud 上的 DeepSeek 模型，max_output_tokens
// 取 128、65535、65536，分别在默认上限、关闭夹紧（ollama_max_tokens_cap=0）、自定义上限
// 下的保留与夹紧结果；custom 必须与 openai、deepseek 逐项一致。
func TestForwardResponsesKeepsMaxOutputTokensForPreservingPlatforms(t *testing.T) {
	cases := []struct {
		name string
		cap  any
		// want 按 value 128 / 65535 / 65536 依次给出上游应收到的 max_output_tokens。
		want [3]int64
	}{
		{"默认上限 65535：65536 被夹到 65535", nil, [3]int64{128, 65535, 65535}},
		{"关闭夹紧（cap=0）：65536 原样保留", 0, [3]int64{128, 65535, 65536}},
		{"自定义上限 100：三个值都夹到 100", 100, [3]int64{100, 100, 100}},
	}
	values := [3]int64{128, 65535, 65536}

	for platform, build := range customMaxOutputAccountBuilders(customMaxOutputOllamaResponsesBase) {
		for _, tc := range cases {
			for i, value := range values {
				name := platform + "/" + tc.name + "/" + strconv.FormatInt(value, 10)
				t.Run(name, func(t *testing.T) {
					upstream := forwardCustomMaxOutput(t, build(tc.cap), customMaxOutputResponsesBody(value), nil, false)
					require.Equal(t, "https://ollama.com/v1/responses", upstream.lastReq.URL.String())
					got := gjson.GetBytes(upstream.lastBody, "max_output_tokens")
					require.True(t, got.Exists(), "max_output_tokens 不得被删除：%s", upstream.lastBody)
					require.Equal(t, tc.want[i], got.Int())
					// 其他字段不受影响。
					require.Equal(t, customMaxOutputModel, gjson.GetBytes(upstream.lastBody, "model").String())
				})
			}
		}
	}
}

// TestForwardResponsesKeepsMaxOutputTokensOnNonOllamaUpstream 非 Ollama 的第三方 Responses
// 上游不做任何夹紧：三个取值与超大值都原样保留，custom 与 openai、deepseek 一致。
func TestForwardResponsesKeepsMaxOutputTokensOnNonOllamaUpstream(t *testing.T) {
	for platform, build := range customMaxOutputAccountBuilders(customMaxOutputRelayResponsesBase) {
		for _, value := range []int64{128, 65535, 65536, 256000} {
			name := platform + "/" + strconv.FormatInt(value, 10)
			t.Run(name, func(t *testing.T) {
				upstream := forwardCustomMaxOutput(t, build(nil), customMaxOutputResponsesBody(value), nil, false)
				require.Equal(t, "http://responses.example/v1/responses", upstream.lastReq.URL.String())
				got := gjson.GetBytes(upstream.lastBody, "max_output_tokens")
				require.True(t, got.Exists(), "max_output_tokens 不得被删除：%s", upstream.lastBody)
				require.Equal(t, value, got.Int())
			})
		}
	}
}

// TestForwardResponsesDoesNotInjectMaxOutputTokens 客户端没带 max_output_tokens 时，
// 转发器不得凭空补一个。
func TestForwardResponsesDoesNotInjectMaxOutputTokens(t *testing.T) {
	for platform, build := range customMaxOutputAccountBuilders(customMaxOutputOllamaResponsesBase) {
		t.Run(platform, func(t *testing.T) {
			upstream := forwardCustomMaxOutput(t, build(nil), customMaxOutputResponsesBody(nil), nil, false)
			require.False(t, gjson.GetBytes(upstream.lastBody, "max_output_tokens").Exists(), string(upstream.lastBody))
		})
	}
}

// TestForwardResponsesCodexClientMaxOutputTokensUnchangedForCustom Codex 客户端本来就不进入
// 非 Codex 的字段归一化块：custom 账号上 128、65535 原样、65536 仍由 Ollama clamp 夹到 65535，
// 关闭夹紧时原样保留。三种 Codex 判定（User-Agent、originator、ForceCodexCLI）结果一致。
func TestForwardResponsesCodexClientMaxOutputTokensUnchangedForCustom(t *testing.T) {
	codexUA := func(c *gin.Context) {
		c.Request.Header.Set("User-Agent", "codex_cli_rs/0.55.0 (Ubuntu 24.04; x86_64)")
	}
	codexOriginator := func(c *gin.Context) { c.Request.Header.Set("originator", "codex-tui") }
	clients := []struct {
		name          string
		prepare       func(*gin.Context)
		forceCodexCLI bool
	}{
		{"codex UA", codexUA, false},
		{"codex originator", codexOriginator, false},
		{"ForceCodexCLI", nil, true},
	}
	build := customMaxOutputAccountBuilders(customMaxOutputOllamaResponsesBase)["custom"]
	for _, client := range clients {
		for _, tc := range []struct {
			name string
			cap  any
			want [3]int64
		}{
			{"默认上限", nil, [3]int64{128, 65535, 65535}},
			{"关闭夹紧", 0, [3]int64{128, 65535, 65536}},
		} {
			for i, value := range [3]int64{128, 65535, 65536} {
				name := client.name + "/" + tc.name + "/" + strconv.FormatInt(value, 10)
				t.Run(name, func(t *testing.T) {
					upstream := forwardCustomMaxOutput(t, build(tc.cap), customMaxOutputResponsesBody(value), client.prepare, client.forceCodexCLI)
					require.Equal(t, "https://ollama.com/v1/responses", upstream.lastReq.URL.String())
					got := gjson.GetBytes(upstream.lastBody, "max_output_tokens")
					require.True(t, got.Exists(), string(upstream.lastBody))
					require.Equal(t, tc.want[i], got.Int())
				})
			}
		}
	}
}

// TestForwardChatCompletionsMaxTokensUnchangedForCustom Chat Completions 入站的 max_tokens
// 不受本次改动影响：custom 账号走 Chat Completions 出站，128、65535 原样，65536 在
// Ollama Cloud 出站地址上夹到 65535，关闭夹紧（cap=0）时原样保留。
func TestForwardChatCompletionsMaxTokensUnchangedForCustom(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name string
		cap  any
		want [3]int64
	}{
		{"默认上限", nil, [3]int64{128, 65535, 65535}},
		{"关闭夹紧", 0, [3]int64{128, 65535, 65536}},
	} {
		for i, value := range [3]int64{128, 65535, 65536} {
			name := tc.name + "/" + strconv.FormatInt(value, 10)
			t.Run(name, func(t *testing.T) {
				account := customTestAccount(map[string]any{
					APIProtocolChatCompletions: customMaxOutputOllamaResponsesBase,
					APIProtocolResponses:       customMaxOutputOllamaResponsesBase,
				})
				if tc.cap != nil {
					account.Extra = map[string]any{OllamaCloudMaxTokensCapExtraKey: tc.cap}
				}
				body := customMaxOutputMarshal(map[string]any{
					"model":      customMaxOutputModel,
					"max_tokens": value,
					"messages":   []map[string]any{{"role": "user", "content": "hi"}},
					"stream":     false,
				})
				upstream := &httpUpstreamRecorder{err: errors.New("stop after capture")}
				svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
				_, err := svc.ForwardAsChatCompletions(context.Background(), adaptiveProtocolTestContext("/v1/chat/completions", body), account, body, "", "")
				require.Error(t, err)
				require.Equal(t, "https://ollama.com/v1/chat/completions", upstream.lastReq.URL.String())
				require.Equal(t, tc.want[i], gjson.GetBytes(upstream.lastBody, "max_tokens").Int())
			})
		}
	}
}

// TestForwardResponsesViaChatCompletionsMaxOutputTokensUnchangedForCustom 未填 Responses 地址的
// custom 账号把 Responses 请求协议转换到 Chat Completions：这是修复前文档推荐的规避办法，
// max_output_tokens 经转换变成 max_completion_tokens，行为必须保持不变。
func TestForwardResponsesViaChatCompletionsMaxOutputTokensUnchangedForCustom(t *testing.T) {
	for i, value := range [3]int64{128, 65535, 65536} {
		want := [3]int64{128, 65535, 65535}[i]
		t.Run(strconv.FormatInt(value, 10), func(t *testing.T) {
			account := customTestAccount(map[string]any{APIProtocolChatCompletions: customMaxOutputOllamaResponsesBase})
			upstream := forwardCustomMaxOutput(t, account, customMaxOutputResponsesBody(value), nil, false)
			require.Equal(t, "https://ollama.com/v1/chat/completions", upstream.lastReq.URL.String())
			require.Equal(t, want, gjson.GetBytes(upstream.lastBody, "max_completion_tokens").Int(), string(upstream.lastBody))
			require.False(t, gjson.GetBytes(upstream.lastBody, "max_output_tokens").Exists(), string(upstream.lastBody))
		})
	}
}
