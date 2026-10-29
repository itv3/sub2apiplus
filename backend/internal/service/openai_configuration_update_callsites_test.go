//go:build unit

package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 本文件补的是“调用点接线”的端到端覆盖（0.165 独立验收的存活变异）：
//
//   - 定型函数的闸门、折叠函数本身都有单测，但调用点有没有把闸门／折叠接上，此前只有函数级用例，
//     把调用点的接线去掉或改成恒真，全部既有测试仍然通过；
//   - 这里每条用例都走真实入口（Forward／ForwardAsResponses），断言上游实际收到的字节。
//
// 对应变异：
//
//	N12  official_egress_openai_http.go 把 AllowConfigurationUpdate 改成恒真（记忆合并线程也合成）
//	N13  openai_gateway_responses_chat_fallback.go 去掉折叠
//	N14  antigravity_gateway_compat.go 去掉折叠
//	N15  gateway_forward_as_responses.go 去掉折叠

const (
	cuCallsiteUpdateHigh   = `{"type":"configuration_update","reasoning":{"effort":"high"}}`
	cuCallsiteUpdateMedium = `{"type":"configuration_update","reasoning":{"effort":"medium"}}`
	cuCallsiteUpdateXHigh  = `{"type":"configuration_update","reasoning":{"effort":"xhigh"}}`
	cuCallsiteUserHello    = `{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}`
	cuCallsiteUserAgain    = `{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}`
	cuCallsiteAssistant    = `{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}`
)

// cuCallsiteBody 拼一个 Responses 请求体：请求级 effort 取 baseline，input 依次放 items。
func cuCallsiteBody(t *testing.T, model string, stream bool, baseline string, items ...string) []byte {
	t.Helper()
	body := fmt.Sprintf(`{"model":%q,"stream":%t,"reasoning":{"effort":%q},"input":[%s]}`, model, stream, baseline, strings.Join(items, ","))
	require.True(t, json.Valid([]byte(body)), body)
	return []byte(body)
}

// ---- N12：记忆合并线程不合成 configuration_update（Forward 端到端）----

// 第三方入站（Kilo 形态）走官方 OAuth 账号的 HTTP 出站：同一请求体，普通线程合成 update，带记忆合并身份的线程不合成。
// 记忆合并（官方 core 的 reasoning_effort_override 不作用于它）靠调用点把 identity 翻译成 AllowConfigurationUpdate=false；
// 变异把它改成恒真后，记忆合并线程也会被合成 update，下面“记忆合并不合成”的断言就会红。
func TestOfficialForwardHTTPSynthesizesConfigurationUpdateOnlyForOrdinaryThreads(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, cuSynthMutation(t))
	const memorySubagent = "memory_consolidation"
	build := func(memory bool) officialForwardBodyCase {
		// 记忆合并身份要自洽（子会话 ID 与线程 ID 不同、prompt_cache_key 等于会话 ID），否则网关把它当根线程；
		// 下面 context 里再用 resolveDerivedOfficialOpenAIConditionalKind 直接断言身份确实被识别，避免测试静默退化成对照组。
		clientMetadata := `{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","thread_id":"019f9577-d69f-7892-809e-8a3a4198c672",` +
			`"turn_id":"7a46fb58-2930-4d6c-9cca-ea1124fcc871"`
		if memory {
			clientMetadata += `,"x-openai-subagent":"` + memorySubagent + `"`
		}
		clientMetadata += `}`
		body := []byte(`{"model":"gpt-6.1-sol","stream":false,"store":true,"instructions":"Kilo raw responses instruction",` +
			`"prompt_cache_key":"019f9577-d69f-7892-809e-8a3a4198c671",` +
			`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"MEMORY_GATE_OK"}]}],` +
			`"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],` +
			`"reasoning":{"effort":"high"},"text":{"verbosity":"low"},"client_metadata":` + clientMetadata + `}`)
		name := "configuration_update_ordinary_thread"
		if memory {
			name = "configuration_update_memory_consolidation_thread"
		}
		return officialForwardBodyCase{
			name: name,
			body: func(*testing.T) []byte { return append([]byte(nil), body...) },
			context: func(t *testing.T, raw []byte) *gin.Context {
				c := newOfficialOpenAIHTTPKiloContext(raw, "kilo-configuration-update-gate-session")
				c.Request.URL.Path = "/v1/responses"
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				kind := officialOpenAIIdentityKindRoot
				if memory {
					c.Request.Header.Set("x-openai-subagent", memorySubagent)
					c.Request.Header.Set("x-openai-memgen-request", "true")
					c.Request.Header.Set("x-codex-turn-metadata", `{"thread_source":"memory_consolidation"}`)
					kind = officialOpenAIIdentityKindMemoryConsolidation
				}
				contract, err := captureOfficialOpenAIHTTPBodyContract(raw)
				require.NoError(t, err)
				gotKind, gotSubagent, _, gotMemory := resolveDerivedOfficialOpenAIConditionalKind(c, contract)
				require.Equal(t, kind, gotKind, "测试前提：网关必须识别出预期的线程身份")
				require.Equal(t, memory, gotMemory)
				if memory {
					require.Equal(t, memorySubagent, gotSubagent)
				}
				return c
			},
			service: func(upstream HTTPUpstream) *OpenAIGatewayService {
				cfg := &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}}
				cfg.Gateway.OfficialClientProfiles.Mode = officialClientProfileModePrevious
				svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream}
				svc.openaiModelCapabilities.replaceFromManifest(94, []byte(cuSolManifest))
				return svc
			},
		}
	}

	ordinary := officialForwardBodyCompare(t, build(false))
	require.NoError(t, ordinary.err)
	require.Len(t, ordinary.calls, 1)
	require.Equal(t, 1, strings.Count(string(ordinary.calls[0].decoded), `"configuration_update"`),
		"对照组：普通线程必须合成 update，否则下面“记忆合并不合成”什么也证明不了")

	memory := officialForwardBodyCompare(t, build(true))
	require.NoError(t, memory.err)
	require.Len(t, memory.calls, 1)
	require.Contains(t, string(memory.calls[0].decoded), "MEMORY_GATE_OK", "记忆合并请求应已正常转发")
	require.NotContains(t, string(memory.calls[0].decoded), "configuration_update", "记忆合并线程不合成 configuration_update")
}

// ---- N13：Chat Completions 回退路径折叠 ----

// Responses 请求经只支持 Chat Completions 的上游转发：input 里最后一条 configuration_update 的 effort 要折进上游的
// reasoning_effort（Chat 上游不认识 update 项，不折叠的话用户会话内改的档位被窗口基线盖掉）；用量里的 effort 也是升档后的。
func TestForwardResponsesChatFallbackFoldsLastConfigurationUpdateEffort(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			// 基线 low，中途先升 xhigh 再降 high：生效的是最后一条 high。
			body := cuCallsiteBody(t, "gpt-5.4", stream, "low",
				cuCallsiteUserHello, cuCallsiteUpdateXHigh, cuCallsiteAssistant, cuCallsiteUserAgain, cuCallsiteUpdateHigh)
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			responseBody := `{"id":"chatcmpl_cu","object":"chat.completion","model":"gpt-5.4","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`
			contentType := "application/json"
			if stream {
				responseBody = "data: {\"id\":\"chatcmpl_cu\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n" +
					"data: {\"choices\":[],\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}\n\ndata: [DONE]\n\n"
				contentType = "text/event-stream"
			}
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{contentType}}, Body: io.NopCloser(strings.NewReader(responseBody)),
			}}
			svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}

			result, err := svc.Forward(context.Background(), c, forceChatResponsesFallbackAccount(), body)
			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, "/v1/chat/completions", upstream.lastReq.URL.Path)
			require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String(), "上游收到最后一条 update 的 effort，而不是请求级基线 low")
			require.NotContains(t, string(upstream.lastBody), "configuration_update")
			require.Equal(t, "high", optionalStringValue(result.ReasoningEffort), "用量记录的是升档后的 effort")
		})
	}
}

// ---- N14：Antigravity 的 Responses 兼容入口折叠 ----

// Antigravity 用 effort 推出的 thinking 预算挑选 Gemini 思考变体（budget>8192 → -high，1024<budget≤8192 → -medium）：
// 基线 high、中途降到 medium 的请求，折叠后应走 -medium 变体；不折叠就会按基线走 -high。
// （effort=low 在转换器里不开 thinking，档位按“未指定”兜底成 high，所以这里用 medium 与 high 对比。）
func TestAntigravityCompatResponsesFoldsConfigurationUpdateEffortIntoThinkingVariant(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := newAntigravityCompatAccount(AccountTypeOAuth)
	account.Credentials["model_mapping"] = map[string]any{
		"gemini-3.1-pro-high":   "gemini-3.1-pro-high",
		"gemini-3.1-pro-medium": "gemini-3.1-pro-medium",
	}
	modelSentUpstream := func(t *testing.T, body []byte) string {
		t.Helper()
		upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{antigravityCompatSuccessResponse()}}
		svc := newAntigravityCompatService(config.GatewayConfig{MaxLineSize: defaultMaxLineSize}, upstream)
		c, _ := newAntigravityCompatContext(http.MethodPost, "/v1/responses", body)
		result, err := svc.ForwardAsResponses(context.Background(), c, account, body, nil)
		require.NoError(t, err)
		require.NotNil(t, result)
		require.Len(t, upstream.requestBodies, 1)
		return gjson.GetBytes(upstream.requestBodies[0], "model").String()
	}

	// 对照组：请求级 effort 就是 medium、没有 update，走 -medium；基线 high 没有 update，走 -high。证明两个变体确实随 effort 变化。
	require.Equal(t, "gemini-3.1-pro-medium", modelSentUpstream(t, cuCallsiteBody(t, "gemini-3.1-pro", false, "medium", cuCallsiteUserHello)))
	require.Equal(t, "gemini-3.1-pro-high", modelSentUpstream(t, cuCallsiteBody(t, "gemini-3.1-pro", false, "high", cuCallsiteUserHello)))
	// 折叠：基线 high，input 末尾 update 降到 medium → -medium。
	folded := cuCallsiteBody(t, "gemini-3.1-pro", false, "high", cuCallsiteUserHello, cuCallsiteUpdateMedium)
	require.Equal(t, "gemini-3.1-pro-medium", modelSentUpstream(t, folded), "configuration_update 的 effort 要折进请求级 effort 再选思考变体")
}

// ---- N15：Anthropic 账号的 Responses 入口（GatewayService.ForwardAsResponses）折叠 ----

func TestGatewayForwardAsResponsesFoldsConfigurationUpdateEffortForAnthropic(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := cuCallsiteBody(t, "public-claude", false, "low",
		cuCallsiteUserHello, cuCallsiteUpdateXHigh, cuCallsiteAssistant, cuCallsiteUserAgain, cuCallsiteUpdateHigh)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	upstream := &anthropicHTTPUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(namespaceToolAnthropicStream()))}}
	svc := &GatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{ID: 1, Platform: PlatformAnthropic, Type: AccountTypeAPIKey,
		Credentials: map[string]any{"api_key": "fixture-key", "model_mapping": map[string]any{"public-claude": "claude-opus-5-5"}}}

	result, err := svc.ForwardAsResponses(context.Background(), c, account, body, nil)
	require.NoError(t, err)
	require.NotNil(t, result)
	require.NotNil(t, upstream.lastReq)
	require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "output_config.effort").String(), "上游收到最后一条 update 的 effort，而不是请求级基线 low")
	require.NotContains(t, string(upstream.lastBody), "configuration_update")
}
