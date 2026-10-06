package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 本文件是问题四 M2 的端到端安全网：同一请求分别在“正文工作区开启”与“关闭（改造前的代码路径）”
// 两种状态下走完整的 OpenAIGatewayService.Forward，经真实 Executor 编译、签名后发往上游的每个请求
// 的 URL、Header、ContentLength 与 wire 字节（含 zstd 压缩变体）必须逐字节一致，转发结果与错误文本
// 也必须一致。覆盖官方 Lite 与非 Lite、工具续接、推理内容回放、compaction、guardian、记忆合并、
// 第三方客户端、legacy compact、字段补丁、孤立工具输出、命名空间剥离、上游错误重试与非法正文。

// officialForwardBodyWireCall 是上游收到的一个业务请求。
type officialForwardBodyWireCall struct {
	method        string
	url           string
	host          string
	header        http.Header
	contentLength int64
	wire          []byte
	decoded       []byte
	tlsProfile    string
}

// officialForwardBodyRecorder 记录业务请求的原始 wire 字节与 Header，按队列应答。
type officialForwardBodyRecorder struct {
	mu        sync.Mutex
	calls     []officialForwardBodyWireCall
	responses []func() *http.Response
	fallback  func() *http.Response
	// onBusiness 在读取业务请求正文之前调用（此时上游 attempt 的编译与压缩已完成、请求正在发送），
	// 供测试观测这一时刻的内存与工作区状态。
	onBusiness func(req *http.Request)
}

func (u *officialForwardBodyRecorder) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	return u.DoWithTLS(req, "", 0, 0, nil)
}

func (u *officialForwardBodyRecorder) DoWithTLS(req *http.Request, _ string, _ int64, _ int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	if req != nil && req.URL != nil && strings.Contains(req.URL.Path, "/codex/models") {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(codexModelsRecorderManifest)),
		}, nil
	}
	if u.onBusiness != nil {
		u.onBusiness(req)
	}
	call := officialForwardBodyWireCall{
		method: req.Method, url: req.URL.String(), host: req.Host,
		header: req.Header.Clone(), contentLength: req.ContentLength,
	}
	if profile != nil {
		call.tlsProfile = profile.Name
	}
	if req.Body != nil {
		call.wire, _ = io.ReadAll(req.Body)
		_ = req.Body.Close()
		call.decoded = call.wire
		if strings.EqualFold(req.Header.Get("Content-Encoding"), "zstd") {
			decoder, err := zstd.NewReader(nil)
			if err == nil {
				if decoded, decodeErr := decoder.DecodeAll(call.wire, nil); decodeErr == nil {
					call.decoded = decoded
				}
				decoder.Close()
			}
		}
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.calls = append(u.calls, call)
	if len(u.responses) > 0 {
		next := u.responses[0]
		u.responses = u.responses[1:]
		return next(), nil
	}
	return u.fallback(), nil
}

func officialForwardBodySSE(responseID string) func() *http.Response {
	return func() *http.Response { return newOfficialOpenAIHTTPSSECompletedResponse(responseID) }
}

func officialForwardBodyJSON(status int, body string) func() *http.Response {
	return func() *http.Response { return newOfficialOpenAIHTTPJSONResponse(status, body) }
}

// officialForwardBodyCase 描述一种请求形态；body 与 context 每次运行都重新构造（Forward 会改写 gin 上下文）。
type officialForwardBodyCase struct {
	name    string
	body    func(t *testing.T) []byte
	context func(t *testing.T, body []byte) *gin.Context
	account func() *Account
	// service 为空时使用官方出站测试服务（带 Lite 模型能力清单）。
	service   func(upstream HTTPUpstream) *OpenAIGatewayService
	prepare   func(t *testing.T, svc *OpenAIGatewayService)
	responses []func() *http.Response
	fallback  func() *http.Response
	// onBusiness 见 officialForwardBodyRecorder.onBusiness。
	onBusiness func(req *http.Request)
}

type officialForwardBodyOutcome struct {
	calls  []officialForwardBodyWireCall
	result *OpenAIForwardResult
	err    error
}

func officialForwardBodyRun(t *testing.T, tc officialForwardBodyCase, source []byte, disabled bool) officialForwardBodyOutcome {
	t.Helper()
	// 每次运行各用一份拷贝：同一形态的两次运行必须看到逐字节相同的入站正文（部分夹具含随机内容）。
	body := append([]byte(nil), source...)
	c := tc.context(t, body)
	// 固定 invocation，避免两次运行因随机 ID 产生与本改造无关的差异。
	c.Set(officialEgressInvocationGinKey, "official-forward-body-"+tc.name)
	recorder := &officialForwardBodyRecorder{
		responses:  append([]func() *http.Response(nil), tc.responses...),
		fallback:   tc.fallback,
		onBusiness: tc.onBusiness,
	}
	if recorder.fallback == nil {
		recorder.fallback = officialForwardBodySSE("resp_" + tc.name)
	}
	var svc *OpenAIGatewayService
	if tc.service != nil {
		svc = tc.service(recorder)
	} else {
		svc = &OpenAIGatewayService{
			cfg: &config.Config{Security: config.SecurityConfig{
				URLAllowlist: config.URLAllowlistConfig{Enabled: false},
			}},
			httpUpstream: recorder,
		}
		svc.openaiModelCapabilities.replaceFromManifest(
			94,
			[]byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`),
		)
	}
	if tc.prepare != nil {
		tc.prepare(t, svc)
	}
	account := newOfficialOpenAIHTTPTestAccount(94)
	if tc.account != nil {
		account = tc.account()
	}
	ctx := context.Background()
	if disabled {
		ctx = withOfficialForwardHTTPBodyDisabled(ctx)
	}
	result, err := svc.Forward(ctx, c, account, body)
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	return officialForwardBodyOutcome{calls: append([]officialForwardBodyWireCall(nil), recorder.calls...), result: result, err: err}
}

func officialForwardBodyStringPtr(value *string) string {
	if value == nil {
		return "<nil>"
	}
	return *value
}

func officialForwardBodyCompare(t *testing.T, tc officialForwardBodyCase) officialForwardBodyOutcome {
	t.Helper()
	source := tc.body(t)
	legacy := officialForwardBodyRun(t, tc, source, true)
	current := officialForwardBodyRun(t, tc, source, false)
	if legacy.err != nil || current.err != nil {
		require.Error(t, legacy.err, "%s：新路径报错但改造前没有：%v", tc.name, current.err)
		require.Error(t, current.err, "%s：改造前报错但新路径没有：%v", tc.name, legacy.err)
		require.Equal(t, legacy.err.Error(), current.err.Error(), "%s：错误文本必须一致", tc.name)
	}
	require.Len(t, current.calls, len(legacy.calls), "%s：上游请求次数必须一致", tc.name)
	for i := range legacy.calls {
		want, got := legacy.calls[i], current.calls[i]
		label := tc.name + "#" + strconv.Itoa(i)
		require.Equal(t, want.method, got.method, label)
		require.Equal(t, want.url, got.url, label)
		require.Equal(t, want.host, got.host, label)
		require.Equal(t, want.header, got.header, "%s：上游 Header 必须一致", label)
		if !bytes.Equal(want.decoded, got.decoded) {
			difference := 0
			for difference < len(want.decoded) && difference < len(got.decoded) && want.decoded[difference] == got.decoded[difference] {
				difference++
			}
			start := max(0, difference-80)
			t.Fatalf("%s：解压正文在 %d 字节处不同\n旧=%s\n新=%s", label, difference,
				want.decoded[start:min(len(want.decoded), difference+180)], got.decoded[start:min(len(got.decoded), difference+180)])
		}
		require.Equal(t, want.contentLength, got.contentLength, label)
		require.Equal(t, want.tlsProfile, got.tlsProfile, label)
		require.True(t, bytes.Equal(want.wire, got.wire), "%s：上游 wire 字节必须逐字节一致\n旧=%s\n新=%s", label,
			officialForwardBodyPreview(want.decoded), officialForwardBodyPreview(got.decoded))
	}
	if legacy.result != nil || current.result != nil {
		require.NotNil(t, legacy.result, tc.name)
		require.NotNil(t, current.result, tc.name)
		require.Equal(t, legacy.result.Model, current.result.Model, tc.name)
		require.Equal(t, legacy.result.BillingModel, current.result.BillingModel, tc.name)
		require.Equal(t, legacy.result.UpstreamModel, current.result.UpstreamModel, tc.name)
		require.Equal(t, officialForwardBodyStringPtr(legacy.result.ReasoningEffort), officialForwardBodyStringPtr(current.result.ReasoningEffort), tc.name)
		require.Equal(t, officialForwardBodyStringPtr(legacy.result.ServiceTier), officialForwardBodyStringPtr(current.result.ServiceTier), tc.name)
		require.Equal(t, legacy.result.Stream, current.result.Stream, tc.name)
		require.Equal(t, legacy.result.RequestID, current.result.RequestID, tc.name)
		require.Equal(t, legacy.result.ResponseID, current.result.ResponseID, tc.name)
		require.Equal(t, legacy.result.Usage, current.result.Usage, tc.name)
	}
	return current
}

func officialForwardBodyPreview(body []byte) string {
	if len(body) > 600 {
		return string(body[:600]) + "…"
	}
	return string(body)
}

// officialForwardBodyMutate 在官方测试正文上做 map 级改写后按规范化编码重建正文。
func officialForwardBodyMutate(t *testing.T, body []byte, mutate func(payload map[string]any)) []byte {
	t.Helper()
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	require.NoError(t, err)
	mutate(payload)
	out, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return out
}

func officialForwardBodyAppendInput(t *testing.T, body []byte, items ...any) []byte {
	t.Helper()
	return officialForwardBodyMutate(t, body, func(payload map[string]any) {
		input, _ := payload["input"].([]any)
		payload["input"] = append(input, items...)
	})
}

func officialForwardBodyCases(t *testing.T) []officialForwardBodyCase {
	t.Helper()
	officialContext := func(_ *testing.T, body []byte) *gin.Context {
		return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	}
	kiloBody := []byte(`{
		"model":"gpt-5.6-luna","stream":false,"store":true,"parallel_tool_calls":true,"tool_choice":"required",
		"max_output_tokens":32000,"instructions":"Kilo raw responses instruction",
		"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"KILO_OPENAI_RESPONSES_S1_OK"}]}],
		"tools":[{"type":"function","name":"read_file","description":"读取文件","parameters":{"type":"object"}}],
		"reasoning":{"effort":"medium","context":"none"},"include":["message.output_text.logprobs"],"text":{"verbosity":"high"}
	}`)
	kiloContext := func(_ *testing.T, body []byte) *gin.Context {
		c := newOfficialOpenAIHTTPKiloContext(body, "kilo-forward-body-session")
		c.Request.URL.Path = "/v1/responses"
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		return c
	}
	compactContext := func(t *testing.T, body []byte) *gin.Context {
		turnMetadata := gjson.GetBytes(newOfficialOpenAIHTTPTestBody(t, false, true, true), "client_metadata.x-codex-turn-metadata").String()
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses/compact")
		c.Request.Header.Set("Accept", "application/json")
		c.Request.Header.Set("X-Codex-Installation-ID", testOfficialOpenAIInstallationID)
		c.Request.Header.Set("x-codex-turn-metadata", turnMetadata)
		return c
	}
	cases := []officialForwardBodyCase{
		{name: "lite_tool_continuation", body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, true, false, true) }, context: officialContext},
		{name: "lite_explicit_instructions", body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, true, true, false) }, context: officialContext},
		{
			name: "non_stream_json", body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, false, false, true) }, context: officialContext,
			fallback: officialForwardBodyJSON(http.StatusOK, `{"id":"resp_non_stream","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":1,"output_tokens":2}}`),
		},
		{
			name: "non_lite_model",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, newOfficialOpenAIHTTPTestBody(t, true, true, true), func(payload map[string]any) {
					payload["model"] = "gpt-5.5"
				})
			},
			context: officialContext,
		},
		{
			name: "reasoning_content_replay",
			body: func(t *testing.T) []byte {
				return officialForwardBodyAppendInput(t, newOfficialOpenAIHTTPTestBody(t, true, false, true),
					map[string]any{"type": "reasoning", "summary": []any{}, "encrypted_content": "gAAAAAB" + strings.Repeat("QUJD", 4096),
						"content": []any{map[string]any{"type": "reasoning_text", "text": "visible"}}},
					map[string]any{"type": "message", "role": "user", "content": "继续"},
				)
			},
			context: officialContext,
		},
		{
			name: "compaction_trigger_reorder",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, newOfficialOpenAIHTTPTestBody(t, true, false, true), func(payload map[string]any) {
					input, _ := payload["input"].([]any)
					payload["input"] = append([]any{input[0], map[string]any{"type": "compaction_trigger"}}, input[1:]...)
				})
			},
			context: officialContext,
		},
		{name: "guardian", body: newOfficialOpenAIGuardianHTTPBody, context: func(t *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIGuardianHTTPContext(t, body, "/v1/responses")
		}},
		{name: "memory_consolidation", body: newOfficialOpenAIMemoryConsolidationHTTPBody, context: func(t *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIMemoryConsolidationHTTPContext(t, body, "/v1/responses")
		}},
		{name: "third_party_kilo_lite", body: func(*testing.T) []byte { return append([]byte(nil), kiloBody...) }, context: kiloContext},
		{
			name: "third_party_kilo_patches",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, append([]byte(nil), kiloBody...), func(payload map[string]any) {
					delete(payload, "instructions")
					payload["reasoning"] = map[string]any{"effort": "minimal"}
					payload["service_tier"] = "fast"
					payload["previous_response_id"] = "resp_prev"
					payload["max_tokens"] = json.Number("2048")
					payload["prompt_cache_retention"] = "24h"
				})
			},
			context: kiloContext,
		},
		{
			name: "account_model_mapping",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, append([]byte(nil), kiloBody...), func(payload map[string]any) {
					payload["model"] = "alias-luna"
				})
			},
			context: kiloContext,
			account: func() *Account {
				account := newOfficialOpenAIHTTPTestAccount(94)
				account.Credentials["model_mapping"] = map[string]any{"alias-luna": "gpt-5.6-luna"}
				return account
			},
		},
		{
			name: "compact_legacy",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, newOfficialOpenAIHTTPTestBody(t, false, true, true), func(payload map[string]any) {
					delete(payload, "client_metadata")
				})
			},
			context: compactContext,
			prepare: func(t *testing.T, svc *OpenAIGatewayService) { withOfficialCodexLegacyCompactRelease(t, svc) },
			fallback: officialForwardBodyJSON(http.StatusOK,
				`{"id":"resp_compact","model":"gpt-5.6-luna","output":[],"usage":{"input_tokens":2,"output_tokens":1}}`),
		},
		{
			name: "namespace_strip",
			body: func(t *testing.T) []byte {
				return officialForwardBodyMutate(t, newOfficialOpenAIHTTPTestBody(t, false, false, true), func(payload map[string]any) {
					input, _ := payload["input"].([]any)
					item, _ := input[3].(map[string]any)
					item["namespace"] = "remove-before-forward"
				})
			},
			context: officialContext,
		},
		{
			name: "orphan_tool_output",
			body: func(t *testing.T) []byte {
				return officialForwardBodyAppendInput(t, newOfficialOpenAIHTTPTestBody(t, true, false, true),
					map[string]any{"type": "custom_tool_call_output", "call_id": "call_orphan", "output": "孤立输出"},
				)
			},
			context: officialContext,
		},
		{
			name:    "large_memory_profile_shape",
			body:    func(t *testing.T) []byte { return buildOfficialEgressMemoryProfileBody(t, 1<<20) },
			context: officialContext,
		},
		{
			name: "invalid_encrypted_retry",
			body: func(t *testing.T) []byte {
				return officialForwardBodyAppendInput(t, newOfficialOpenAIHTTPTestBody(t, true, false, true),
					map[string]any{"type": "reasoning", "summary": []any{}, "encrypted_content": "gAAAAAB" + strings.Repeat("WFla", 512)},
					map[string]any{"type": "message", "role": "user", "content": "继续"},
				)
			},
			context: officialContext,
			responses: []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
				`{"error":{"message":"The encrypted content could not be verified.","type":"invalid_request_error","code":"invalid_encrypted_content"}}`)},
		},
		{
			name: "upstream_error_passthrough",
			body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, true, false, true) }, context: officialContext,
			fallback: officialForwardBodyJSON(http.StatusBadRequest, `{"error":{"message":"bad request","type":"invalid_request_error"}}`),
		},
		{name: "invalid_json", body: func(*testing.T) []byte { return []byte(`{"model":"gpt-5.6-luna","input":[`) }, context: officialContext},
		{
			// 原生 compaction v2：首个 attempt 返回 200 但 SSE 里 response.failed（上下文超限），按 compact
			// 回退模型重试一次，覆盖非流式 compact 回退分支。
			name: "compaction_v2_fallback_retry",
			body: func(*testing.T) []byte {
				return []byte(`{"model":"gpt-5.5","stream":false,"instructions":"compact-test","input":[{"type":"message","role":"user","content":"hello"},{"type":"compaction_trigger"}]}`)
			},
			context: func(_ *testing.T, body []byte) *gin.Context {
				c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
				c.Request.Header = http.Header{"Content-Type": []string{"application/json"}}
				MarkOpenAINativeCompactionV2(c)
				// 直接走官方出站 HTTP（不先尝试 WS），覆盖正文工作区所在的路径。
				setOfficialCodexForceHTTPFallback(c, true)
				return c
			},
			account: newOpenAIOAuthNamespaceTestAccount,
			service: func(upstream HTTPUpstream) *OpenAIGatewayService {
				svc := &OpenAIGatewayService{
					cfg: &config.Config{
						Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}},
						Gateway:  config.GatewayConfig{OpenAICompactModel: "gpt-5.4"},
					},
					httpUpstream: upstream,
				}
				// 预载模型清单：否则异步能力刷新可能在重试前后任意时刻完成，推理默认值是否已知随之
				// 摇摆，两次运行的 turn metadata 会因时序而不同，与正文处理无关。
				svc.openaiModelCapabilities.replaceFromManifest(5108, []byte(codexModelsRecorderManifest))
				return svc
			},
			responses: []func() *http.Response{func() *http.Response {
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": []string{"text/event-stream"}, "X-Request-Id": []string{"rid_compact_1"}},
					Body: io.NopCloser(strings.NewReader("event: response.failed\n" +
						`data: {"type":"response.failed","response":{"status":"failed","error":{"code":"context_length_exceeded","message":"context window exceeded"}}}` + "\n\n")),
				}
			}},
			fallback: officialForwardBodyJSON(http.StatusOK,
				`{"id":"resp_compact","status":"completed","model":"gpt-5.4","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`),
		},
		{
			name: "third_party_curl_oauth",
			body: func(*testing.T) []byte {
				return []byte(`{"model":"gpt-5.5","stream":false,"instructions":"test","parallel_tool_calls":false,"reasoning":{"effort":"medium","summary":"auto"},"input":[{"type":"message","role":"user","namespace":"remove","content":[{"type":"input_text","text":"hello","namespace":"nested-keep"}]}]}`)
			},
			context: func(_ *testing.T, body []byte) *gin.Context { return newOpenAIRejectedFieldTestContext(body) },
			account: newOpenAIOAuthNamespaceTestAccount,
			fallback: officialForwardBodyJSON(http.StatusOK,
				`{"id":"resp_curl_ok","output":[],"usage":{"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}`),
		},
	}
	return cases
}

// TestOfficialForwardHTTPBodyNonOfficialPathsUnchanged 锁定非官方出站路径：API Key 与 OAuth 透传不创建
// 正文工作区（上游请求的 ctx 里没有工作区），两种状态下发往上游的字节、Header 与结果完全一致。
func TestOfficialForwardHTTPBodyNonOfficialPathsUnchanged(t *testing.T) {
	gin.SetMode(gin.TestMode)
	apiKeyService := func(upstream HTTPUpstream) *OpenAIGatewayService {
		return &OpenAIGatewayService{
			cfg: &config.Config{Security: config.SecurityConfig{
				URLAllowlist: config.URLAllowlistConfig{Enabled: false},
			}},
			httpUpstream: upstream,
		}
	}
	apiKeyContext := func(_ *testing.T, body []byte) *gin.Context { return newOpenAIRejectedFieldTestContext(body) }
	okJSON := officialForwardBodyJSON(http.StatusOK,
		`{"output":[],"usage":{"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}`)
	sawWorkspace := false
	detect := func(req *http.Request) {
		if officialForwardHTTPBodyFromContext(req.Context()) != nil {
			sawWorkspace = true
		}
	}
	cases := []officialForwardBodyCase{
		{
			name: "apikey_responses",
			body: func(*testing.T) []byte {
				return []byte(`{"model":"gpt-5.5","stream":false,"store":true,"input":[{"type":"message","role":"user","content":"one"},` +
					`{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{}","namespace":"drop"},{"type":"function_call_output","call_id":"call_1","output":"ok"},` +
					`{"type":"reasoning","summary":[{"type":"summary_text","text":"keep"}],"content":[{"type":"reasoning_text","text":"remove"}]}]}`)
			},
			context: apiKeyContext, account: newOpenAIRejectedFieldTestAccount, service: apiKeyService, fallback: okJSON, onBusiness: detect,
		},
		{
			name: "apikey_rejected_field_retry",
			body: func(*testing.T) []byte {
				return []byte(`{"model":"gpt-5.5","stream":false,"max_output_tokens":4096,"input":[{"type":"message","role":"user","content":{"max_output_tokens":"keep"}}]}`)
			},
			context: apiKeyContext, account: newOpenAIRejectedFieldTestAccount, service: apiKeyService,
			responses: []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
				`{"error":{"code":"unsupported_parameter","message":"Unsupported parameter: max_output_tokens","param":"max_output_tokens","type":"invalid_request_error"}}`)},
			fallback: okJSON, onBusiness: detect,
		},
		{
			name: "oauth_passthrough",
			body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, false, false, true) },
			context: func(_ *testing.T, body []byte) *gin.Context {
				return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			},
			account: func() *Account {
				account := newOfficialOpenAIHTTPTestAccount(94)
				account.Extra["openai_passthrough"] = true
				return account
			},
			onBusiness: detect,
		},
	}
	for _, tc := range cases {
		outcome := officialForwardBodyCompare(t, tc)
		require.NoError(t, outcome.err, tc.name)
		require.NotEmpty(t, outcome.calls, tc.name)
	}
	require.False(t, sawWorkspace, "非官方出站路径不得创建正文工作区")
}

func TestOfficialForwardHTTPBodyMatchesLegacyOnWire(t *testing.T) {
	gin.SetMode(gin.TestMode)
	sawZstd, sawPlain, sawRetry := false, false, false
	for _, tc := range officialForwardBodyCases(t) {
		outcome := officialForwardBodyCompare(t, tc)
		encodings := make([]string, 0, len(outcome.calls))
		for _, call := range outcome.calls {
			encodings = append(encodings, call.header.Get("Content-Encoding")+"/"+strconv.Itoa(len(call.decoded)))
		}
		t.Logf("%s：上游请求 %d 次 %v，错误=%v", tc.name, len(outcome.calls), encodings, outcome.err)
		for _, call := range outcome.calls {
			if strings.EqualFold(call.header.Get("Content-Encoding"), "zstd") {
				sawZstd = true
			} else if len(call.wire) > 0 {
				sawPlain = true
			}
		}
		if len(outcome.calls) > 1 {
			sawRetry = true
		}
	}
	require.True(t, sawZstd, "夹具必须覆盖 zstd 压缩出站")
	require.True(t, sawPlain, "夹具必须覆盖未压缩出站")
	require.True(t, sawRetry, "夹具必须覆盖上游错误后的重试出站")
}
