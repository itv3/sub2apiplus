package service

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func TestOpenAIWSDeferredIngressMaterializationMatchesLegacy(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)
	input := `[{"type":"message","role":"user","content":"` + strings.Repeat("只读历史", 8192) + `"}]`
	for i, prefix := range []string{
		`{ "client_metadata" : {"session_id":"tiny","thread_id":"tiny","number":9007199254740993}, "prompt_cache_key" : "tiny", "input" : `,
		`{"type":"response.create","reasoning":{"summary":"auto","effort":"high"},"parallel_tool_calls":true,"input":`,
		`{"type":"response.create","reasoning":null,"parallel_tool_calls":false,"input":`,
		`{"type":"response.create","reasoning":{"summary":"auto","context":"all_turns"},"parallel_tool_calls":false,"input":`,
		`{"client_metadata":{"session_id":"quote\"slash\\"},"prompt_cache_key":"quote\"slash\\","input":`,
	} {
		for _, lite := range []bool{false, true} {
			body := []byte(prefix + input + ` }`)
			original := bytes.Clone(body)
			deferred := newOpenAIWSDeferredIngressBody(body, lite)
			require.NotNil(t, deferred, "fixture=%d lite=%v", i, lite)
			want, _, err := applyCodexAccountIdentityClientMetadataRaw(body, account, 77)
			require.NoError(t, err)
			deferred.headers, _, err = applyCodexAccountIdentityClientMetadataRaw(deferred.headers, account, 77)
			require.NoError(t, err)
			if lite {
				want, _, err = normalizeOpenAIResponsesLitePayloadForAccount(want, account)
				require.NoError(t, err)
				deferred.headers, _, err = normalizeOpenAIResponsesLitePayloadForAccount(deferred.headers, account)
				require.NoError(t, err)
			}
			require.Equal(t, want, deferred.materialize(), "物化须逐字节恢复旧身份隔离/Lite 结果")
			require.Equal(t, len(want), deferred.payloadBytes(), "路由使用归一化后的精确长度")
			require.Equal(t, original, body)
		}
	}
}

func TestOpenAIWSDeferredIngressComplexLayoutsFallBack(t *testing.T) {
	large := strings.Repeat("x", 70<<10)
	for _, body := range []string{
		`{"input":[{"reasoning":{ "context" : "all_turns" },"text":"` + large + `"}]}`,
		`{"tools":[{"type":"namespace","name":"n","tools":[]}],"input":"` + large + `"}`,
		`{"input":[{"type":"reasoning","content":["text"],"data":"` + large + `"}]}`,
		`{"input":[{"type":"compaction_trigger","data":"` + large + `"}]}`,
		`{"input":[{"data":"` + large + `","key":1,"key":2}]}`,
		`{"input":[{"data":"` + large + `\xff"}]}`,
	} {
		if strings.HasSuffix(body, `\xff"}]}`) {
			body = strings.Replace(body, `\xff`, "\xff", 1)
		}
		require.Nil(t, newOpenAIWSDeferredIngressBody([]byte(body), true))
	}
}

func TestOpenAIWSDeferredIngressCanonicalPoolIncludesInput(t *testing.T) {
	source := []byte(`{"reasoning":{"context":"all_turns","summary":"auto"},"input":[{"context":"all_turns","effort":"high","summary":"auto"}]}`)
	inputIndex, err := buildOfficialJSONRawIndex(source)
	require.NoError(t, err)
	headerIndex, err := buildOfficialJSONRawIndex([]byte(`{"input":[],"reasoning":{"context":"all_turns","summary":"auto"}}`))
	require.NoError(t, err)
	value := map[string]any{"context": "all_turns", "effort": "high", "summary": "auto"}
	prepared, err := prepareOpenAIWSHTTPBridgeBody(newOfficialOpenAIHTTPTestAccount(94), source)
	require.NoError(t, err)
	preparedIndex, err := buildOfficialJSONRawIndex(prepared)
	require.NoError(t, err)
	want, err := officialJSONAppendValue(preparedIndex, nil, value, preparedIndex.memberNode(preparedIndex.root, "reasoning"))
	require.NoError(t, err)
	writer := openAIWSHTTPBridgeJSONSegments{inputIndex: inputIndex, headerIndex: headerIndex}
	require.NoError(t, writer.appendValue(headerIndex, value, headerIndex.memberNode(headerIndex.root, "reasoning")))
	writer.flush()
	require.Equal(t, want, bytes.Join(writer.segments, nil), "新增字段仍须在逻辑原文的完整复合值池中查找")
}

// 真实 WS 入口差分覆盖身份隔离、Lite 默认值、fast policy、同连接两轮、重放及上游错误重试。
// 阈值只在测试中降至 1 字节；128 KiB 正文足以走延迟路径，不属于大正文峰值测量。
func TestOpenAIWSDeferredIngressMatchesLegacyWire(t *testing.T) {
	for _, fixture := range []struct {
		name     string
		explicit bool
		retry    bool
		replay   bool
		failover bool
		mutate   func([]byte) ([]byte, error)
	}{
		{name: "native"},
		{name: "explicit", explicit: true},
		{name: "reasoning_defaults", mutate: func(body []byte) ([]byte, error) { return sjson.DeleteBytes(body, "reasoning.context") }},
		{name: "metadata_float", mutate: func(body []byte) ([]byte, error) {
			return sjson.SetRawBytes(body, "client_metadata.number", []byte(`9007199254740993`))
		}},
		{name: "fast_normalize", mutate: func(body []byte) ([]byte, error) { return sjson.SetBytes(body, "service_tier", "fast") }},
		{name: "rejected_status", retry: true},
		{name: "history_replay", replay: true},
		{name: "later_429", failover: true},
		{name: "namespace", mutate: func(body []byte) ([]byte, error) {
			return sjson.SetRawBytes(body, "tools", []byte(`[{"type":"namespace","name":"n","tools":[{"type":"function","name":"f","parameters":{"type":"object"}}]}]`))
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			body := newOfficialOpenAIHTTPTestBody(t, true, fixture.explicit, true)
			body, err := sjson.SetBytes(body, "input.2.content.0.text", strings.Repeat("只读正文", 16384))
			require.NoError(t, err)
			if fixture.mutate != nil {
				body, err = fixture.mutate(body)
				require.NoError(t, err)
			}
			first := officialEgressWSHTTPBridgePayload(t, body)
			second, err := sjson.SetBytes(first, "input.2.content.0.text", strings.Repeat("后续正文", 16384))
			require.NoError(t, err)
			if fixture.replay {
				second, err = sjson.SetBytes(second, "previous_response_id", "resp_deferred")
				require.NoError(t, err)
			}
			run := func(disabled bool) []officialForwardBodyWireCall {
				recorder := &officialForwardBodyRecorder{fallback: officialForwardBodySSE("resp_deferred")}
				deferredCalls := 0
				recorder.onBusiness = func(req *http.Request) {
					if workspace := officialForwardHTTPBodyFromContext(req.Context()); workspace != nil && workspace.wsIngress != nil {
						deferredCalls++
					}
				}
				if fixture.retry {
					recorder.responses = []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
						`{"error":{"code":"unsupported_parameter","message":"Unsupported parameter: input[3].status","param":"input[3].status","type":"invalid_request_error"}}`)}
				}
				if fixture.failover {
					recorder.responses = []func() *http.Response{
						officialForwardBodySSE("resp_deferred"),
						officialForwardBodyJSON(http.StatusTooManyRequests, `{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached"}}`),
					}
				}
				service := officialEgressWSHTTPBridgeTestService(recorder)
				cfg := newOpenAIWSExecutionScopeTestConfig()
				cfg.Gateway.OpenAIWS.HTTPBridgeEnabled = true
				cfg.Gateway.OpenAIWS.HTTPBridgeThresholdBytes = 1
				service.cfg = cfg
				service.cache = &stubGatewayCache{}
				service.openaiWSResolver = NewOpenAIWSProtocolResolver(cfg)
				service.toolCorrector = NewCodexToolCorrector()
				account := newOfficialOpenAIHTTPTestAccount(94)
				account.Extra["responses_websockets_v2_enabled"] = true
				service.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`))
				serverErrors := make(chan error, 1)
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					conn, acceptErr := coderws.Accept(w, r, nil)
					if acceptErr != nil {
						serverErrors <- acceptErr
						return
					}
					defer conn.CloseNow()
					conn.SetReadLimit(1 << 20)
					_, frame, readErr := conn.Read(r.Context())
					if readErr != nil {
						serverErrors <- readErr
						return
					}
					ctx, release := openAIWSMemoryTestContext(t, r.Context(), frame)
					defer release()
					if disabled {
						ctx = withOfficialForwardHTTPBodyDisabled(ctx)
					}
					c := newOfficialOpenAIHTTPTestContext(nil, "/v1/responses")
					c.Request = c.Request.WithContext(ctx)
					c.Set(officialEgressInvocationGinKey, "deferred-ingress-"+fixture.name)
					applyOfficialOpenAIWSIngressHeadersForTest(c, frame)
					proxyErr := service.ProxyResponsesWebSocketFromClient(ctx, c, conn, account, "oauth-token", frame, nil)
					if fixture.failover {
						retry, currentTurn := OpenAIWSCurrentTurnRetryPayload(proxyErr)
						if !currentTurn || len(retry) == 0 {
							serverErrors <- fmt.Errorf("缺少后续轮换号正文：%w", proxyErr)
							return
						}
						if gjson.GetBytes(retry, "client_metadata.session_id").String() != gjson.GetBytes(second, "client_metadata.session_id").String() {
							serverErrors <- fmt.Errorf("换号正文保留了上一账号的隔离身份")
							return
						}
						next := newOfficialOpenAIHTTPTestAccount(95)
						next.Credentials["chatgpt_account_id"] = "replacement-account"
						next.Extra["responses_websockets_v2_enabled"] = true
						service.openaiModelCapabilities.replaceFromManifest(next.ID, []byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`))
						if beginErr := openAIWSRequestMemoryFromContext(ctx).BeginAttempt(retry); beginErr != nil {
							serverErrors <- beginErr
							return
						}
						proxyErr = service.ProxyResponsesWebSocketFromClient(ctx, c, conn, next, "replacement-token", retry, nil)
					}
					serverErrors <- proxyErr
				}))
				defer server.Close()
				ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
				defer cancel()
				client, _, dialErr := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), nil)
				require.NoError(t, dialErr)
				defer client.CloseNow()
				for _, frame := range [][]byte{first, second} {
					require.NoError(t, client.Write(ctx, coderws.MessageText, frame))
					for {
						_, message, readErr := client.Read(ctx)
						if readErr != nil {
							select {
							case proxyErr := <-serverErrors:
								t.Fatalf("WS 入口转发失败：%v", proxyErr)
							default:
							}
							break
						}
						if gjson.GetBytes(message, "type").String() == "response.completed" {
							break
						}
					}
				}
				_ = client.Close(coderws.StatusNormalClosure, "done")
				select {
				case proxyErr := <-serverErrors:
					require.NoError(t, proxyErr)
				case <-ctx.Done():
					t.Fatal("WS 入口未按时结束")
				}
				if !disabled && fixture.name != "namespace" {
					require.Positive(t, deferredCalls, "完整入口必须实际启用延迟路径")
				}
				return recorder.calls
			}
			legacy, current := run(true), run(false)
			require.NotEmpty(t, current)
			require.Len(t, current, len(legacy))
			for i := range legacy {
				require.Equal(t, legacy[i], current[i], fmt.Sprintf("完整 WS 入口第 %d 次上游请求", i+1))
			}
		})
	}
}

// 索引只可复用到同一只读正文；兼容改写或后续替换必须重新扫描，不能使用旧坐标。
func TestOpenAIWSDeferredIngressReusesOnlyMatchingNormalizedIndex(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)
	body := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"message","role":"user","content":"` + strings.Repeat("x", 70<<10) + `"}]}`)
	var index *officialJSONRawIndex
	normalized, _, err := normalizeOpenAIResponsesWebSocketCompatibilityBodyWithIndex(body, account, true, &index)
	require.NoError(t, err)
	require.NotNil(t, index)
	reused := newOpenAIWSDeferredIngressBodyWithIndex(normalized, true, index)
	require.NotNil(t, reused)
	require.Same(t, index, reused.index)
	require.Equal(t, normalized, reused.materialize())
	changed, err := sjson.SetBytes(normalized, "model", "gpt-5.4")
	require.NoError(t, err)
	replacement := newOpenAIWSDeferredIngressBodyWithIndex(changed, true, index)
	require.NotNil(t, replacement)
	require.NotSame(t, index, replacement.index)
	require.Equal(t, changed, replacement.materialize())
	var unused = index
	_, _, err = normalizeOpenAIResponsesWebSocketCompatibilityBodyWithIndex(body, nil, true, &unused)
	require.NoError(t, err)
	require.Nil(t, unused)
}
