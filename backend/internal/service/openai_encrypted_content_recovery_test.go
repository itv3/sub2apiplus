package service

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const encryptedRecoveryTestInput = `[{"type":"message","role":"user","content":"任务背景"},{"type":"reasoning","id":"rs_history","encrypted_content":"stale-cipher","summary":[{"type":"summary_text","text":"保留摘要"}]},{"type":"function_call","call_id":"call_history","name":"lookup","arguments":"{\"value\":9007199254740993}"},{"type":"function_call_output","call_id":"call_history","output":"工具结果"},{"type":"message","role":"user","content":"继续任务"}]`

func encryptedRecoveryTestEvent(payload string) string {
	return "event: " + gjson.Get(payload, "type").String() + "\ndata: " + payload + "\n\n"
}

func encryptedRecoveryTestFailure() string {
	return encryptedRecoveryTestEvent(`{"type":"response.failed","response":{"id":"resp_rejected","status":"failed","output":[],"error":{"code":"invalid_encrypted_content","type":"invalid_request_error","message":"The encrypted content could not be verified."}}}`)
}

func encryptedRecoveryTestSuccess() string {
	return encryptedRecoveryTestEvent(`{"type":"response.created","response":{"id":"resp_recovered"}}`) +
		encryptedRecoveryTestEvent(`{"type":"response.output_text.delta","delta":"恢复成功"}`) +
		encryptedRecoveryTestEvent(`{"type":"response.completed","response":{"id":"resp_recovered","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"恢复成功"}]}],"usage":{"input_tokens":12,"output_tokens":3}}}`)
}

func encryptedRecoveryTestResponse(status int, body, requestID string) *http.Response {
	contentType := "text/event-stream"
	if status >= http.StatusBadRequest {
		contentType = "application/json"
	}
	return &http.Response{
		StatusCode: status,
		Header: http.Header{
			"Content-Type": []string{contentType},
			"X-Request-Id": []string{requestID},
		},
		Body: io.NopCloser(strings.NewReader(body)),
	}
}

func newEncryptedRecoveryForwardTest(t *testing.T, responses ...*http.Response) (*OpenAIGatewayService, *httpUpstreamSequenceRecorder, *gin.Context, *httptest.ResponseRecorder, *Account) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "custom-client/1.0")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamSequenceRecorder{responses: responses}
	svc := &OpenAIGatewayService{
		cfg:           &config.Config{Gateway: config.GatewayConfig{MaxLineSize: defaultMaxLineSize}},
		httpUpstream:  upstream,
		toolCorrector: NewCodexToolCorrector(),
	}
	account := &Account{
		ID: 901, Name: "encrypted-recovery-test", Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Status: StatusActive, Schedulable: true, Concurrency: 1,
		Credentials: map[string]any{"api_key": "sk-test"},
	}
	return svc, upstream, c, rec, account
}

// 覆盖生产故障的 HTTP 200 流内拒绝，以及兼容提供方的另外两种错误信封。
func TestOpenAIEncryptedRecoveryForwardSSE(t *testing.T) {
	for _, failed := range []struct {
		name string
		body string
	}{
		{"失败终态", encryptedRecoveryTestFailure()},
		{"顶层错误", encryptedRecoveryTestEvent(`{"type":"error","code":"invalid_encrypted_content","message":"The encrypted content could not be verified."}`)},
		{"嵌套错误", encryptedRecoveryTestEvent(`{"type":"error","error":{"code":"invalid_encrypted_content","type":"invalid_request_error","message":"The encrypted content could not be verified."}}`)},
		{"包装错误", encryptedRecoveryTestEvent(`{"type":"response.failed","response":{"status":"failed","error":{"code":null,"message":"{\"error\":{\"code\":\"invalid_encrypted_content\"}}（traceid: test）"}}}`)},
	} {
		t.Run(failed.name, func(t *testing.T) {
			created := encryptedRecoveryTestEvent(`{"type":"response.created","response":{"id":"resp_rejected"}}`)
			svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, created+failed.body, "rid-rejected"),
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-recovered"),
			)
			body := []byte(`{"model":"gpt-6.1-sol","stream":true,"previous_response_id":"resp_history","input":` + encryptedRecoveryTestInput + `}`)
			result, err := svc.Forward(context.Background(), c, account, body)

			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, 2, upstream.callCount)
			require.Equal(t, "resp_recovered", result.ResponseID)
			require.Equal(t, 12, result.Usage.InputTokens)
			require.Equal(t, "rid-recovered", rec.Header().Get("X-Request-Id"))
			require.NotContains(t, rec.Body.String(), "resp_rejected", "第一次尝试的元数据不得提交给客户端")
			require.NotContains(t, rec.Body.String(), "invalid_encrypted_content")
			require.Equal(t, 1, strings.Count(rec.Body.String(), "event: response.created"))
			require.Equal(t, 1, strings.Count(rec.Body.String(), "event: response.completed"))
			first, second := upstream.bodies[0], upstream.bodies[1]
			require.Equal(t, "stale-cipher", gjson.GetBytes(first, "input.1.encrypted_content").String())
			require.False(t, gjson.GetBytes(second, "input.1.encrypted_content").Exists())
			require.Equal(t, "保留摘要", gjson.GetBytes(second, "input.1.summary.0.text").String())
			require.Equal(t, "resp_history", gjson.GetBytes(second, "previous_response_id").String())
			for _, index := range []int{0, 2, 3, 4} {
				require.JSONEq(t, gjson.GetBytes(first, "input").Array()[index].Raw, gjson.GetBytes(second, "input").Array()[index].Raw)
			}
		})
	}
}

// HTTP 400 与 SSE 拒绝在两种顺序下都共用一次恢复预算，不能跨协议分支重复恢复。
func TestOpenAIEncryptedRecoveryForwardSharesRetryBudget(t *testing.T) {
	for _, scenario := range []struct {
		name       string
		firstHTTP  bool
		secondHTTP bool
	}{
		{"两次流内拒绝", false, false},
		{"HTTP后流内拒绝", true, false},
		{"流内后HTTP拒绝", false, true},
		{"两次HTTP拒绝", true, true},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			httpFailure := `{"error":{"code":"invalid_encrypted_content","type":"invalid_request_error","message":"invalid"}}`
			first := encryptedRecoveryTestResponse(200, encryptedRecoveryTestFailure(), "rid-first")
			if scenario.firstHTTP {
				first = encryptedRecoveryTestResponse(400, httpFailure, "rid-first")
			}
			second := encryptedRecoveryTestResponse(200, encryptedRecoveryTestFailure(), "rid-second")
			if scenario.secondHTTP {
				second = encryptedRecoveryTestResponse(400, httpFailure, "rid-second")
			}
			svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t, first,
				second,
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
			)
			_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":`+encryptedRecoveryTestInput+`}`))
			require.Error(t, err)
			require.Equal(t, 2, upstream.callCount)
			if scenario.secondHTTP {
				require.Equal(t, http.StatusBadRequest, rec.Code)
				require.Contains(t, rec.Header().Get("Content-Type"), "application/json")
				require.True(t, json.Valid(rec.Body.Bytes()))
			} else {
				require.Equal(t, 1, strings.Count(rec.Body.String(), "event: response.failed"))
			}
			require.Contains(t, rec.Body.String(), "invalid_encrypted_content")
			require.Contains(t, rec.Body.String(), "新建会话")
			require.NotContains(t, rec.Body.String(), "恢复成功")
		})
	}
}

// 仅有压缩历史时无法证明早期上下文仍完整；即使混有 reasoning，也不能删除压缩项后重放。
func TestOpenAIEncryptedRecoveryForwardPreservesCompaction(t *testing.T) {
	for _, itemType := range []string{"compaction", "compaction_summary"} {
		for _, status := range []int{200, 400} {
			t.Run(itemType+http.StatusText(status), func(t *testing.T) {
				failure := encryptedRecoveryTestFailure()
				if status == 400 {
					failure = `{"error":{"code":"invalid_encrypted_content","type":"invalid_request_error","message":"invalid"}}`
				}
				svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
					encryptedRecoveryTestResponse(status, failure, "rid-compaction"),
					encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
				)
				body := []byte(`{"model":"gpt-6.1-sol","stream":true,"input":[{"type":"` + itemType + `","encrypted_content":"only-earlier-context"},{"type":"reasoning","encrypted_content":"stale-cipher"},{"role":"user","content":"继续"}]}`)
				_, err := svc.Forward(context.Background(), c, account, body)
				require.Error(t, err)
				require.Equal(t, 1, upstream.callCount)
				require.Equal(t, "only-earlier-context", gjson.GetBytes(upstream.bodies[0], "input.0.encrypted_content").String())
				require.Contains(t, rec.Body.String(), "新建会话")
				require.Empty(t, svc.openaiInvalidEncryptedScopes)
			})
		}
	}
}

func TestOpenAIEncryptedRecoveryForwardDoesNotReplayOutput(t *testing.T) {
	for _, output := range []struct {
		name string
		body string
	}{
		{"文字", encryptedRecoveryTestEvent(`{"type":"response.output_text.delta","delta":"已提交文字"}`)},
		{"工具调用", encryptedRecoveryTestEvent(`{"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_live","name":"write_file","arguments":"{}"}}`)},
		{"工具调用骨架", encryptedRecoveryTestEvent(`{"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_live","name":"write_file","arguments":""}}`)},
		{"终态携带输出", ""},
	} {
		t.Run(output.name, func(t *testing.T) {
			failure := encryptedRecoveryTestFailure()
			if output.name == "终态携带输出" {
				failure = encryptedRecoveryTestEvent(`{"type":"response.failed","response":{"status":"failed","output":[{"type":"function_call","call_id":"call_live","name":"write_file","arguments":"{}"}],"error":{"code":"invalid_encrypted_content","type":"invalid_request_error"}}}`)
			}
			svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, output.body+failure, "rid-output"),
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
			)
			_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":`+encryptedRecoveryTestInput+`}`))
			require.Error(t, err)
			require.Equal(t, 1, upstream.callCount)
			require.Equal(t, 1, strings.Count(rec.Body.String(), "event: response.failed"))
			require.Contains(t, rec.Body.String(), "新建会话")
			require.NotContains(t, rec.Body.String(), "恢复成功")
		})
	}
}

func TestOpenAIEncryptedRecoveryForwardNonStreamingSSE(t *testing.T) {
	for _, scenario := range []struct{ name, failure string }{
		{"标准事件", encryptedRecoveryTestFailure()},
		{"事件名来自event字段", "event: response.created\ndata: {\"response\":{\"id\":\"resp_rejected\"}}\n\n" +
			"event: response.failed\ndata: {\n" +
			"data: \"response\":{\"id\":\"resp_rejected\",\"output\":[],\"error\":{\"code\":\"invalid_encrypted_content\"}}}\n\n"},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, scenario.failure, "rid-rejected"),
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-recovered"),
			)
			result, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":false,"input":`+encryptedRecoveryTestInput+`}`))
			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, 2, upstream.callCount)
			require.True(t, json.Valid(rec.Body.Bytes()))
			require.Equal(t, "resp_recovered", gjson.GetBytes(rec.Body.Bytes(), "id").String())
		})
	}
}

func TestOpenAIEncryptedRecoveryForwardNoReasoningAndBufferedOutput(t *testing.T) {
	for _, scenario := range []string{"缺少推理项", "非流式已有输出"} {
		t.Run(scenario, func(t *testing.T) {
			failure := encryptedRecoveryTestFailure()
			stream := "true"
			input := `[{"role":"user","content":"继续"}]`
			if scenario == "非流式已有输出" {
				failure = encryptedRecoveryTestEvent(`{"type":"response.output_text.delta","delta":"已生成文字"}`) + failure
				stream = "false"
				input = encryptedRecoveryTestInput
			}
			svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, failure, "rid-rejected"),
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
			)
			_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":`+stream+`,"input":`+input+`}`))
			require.Error(t, err)
			require.Equal(t, 1, upstream.callCount)
			require.Contains(t, rec.Body.String(), "新建会话")
			require.NotContains(t, rec.Body.String(), "恢复成功")
		})
	}
}

func TestOpenAIEncryptedRecoveryForwardKeepsCachedCompactionAndLegacyLineage(t *testing.T) {
	svc, upstream, c, _, account := newEncryptedRecoveryForwardTest(t,
		encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-compaction"),
	)
	body := []byte(`{"model":"gpt-6.1-sol","stream":true,"previous_response_id":"resp_history","input":[{"type":"compaction","encrypted_content":"compaction-cipher"},{"type":"reasoning","encrypted_content":"stale-cipher","summary":[]},{"role":"user","content":"继续"}]}`)
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(body, &decoded))
	scope, ok := buildOpenAIInvalidEncryptedScope(account, "gpt-6.1-sol", OpenAIUpstreamTransportHTTPSSE, false, false, decoded, "")
	require.True(t, ok)
	svc.bindOpenAIInvalidEncryptedScope(account.ID, scope, collectOpenAIEncryptedReasoningDigests(decoded))
	sessionHash := svc.GenerateSessionHash(c, body)
	svc.markOpenAIWSInvalidEncryptedContentLineage(0, sessionHash, collectOpenAIEncryptedContentDigestsRaw(body))
	_, err := svc.Forward(context.Background(), c, account, body)
	require.NoError(t, err)
	require.Equal(t, "compaction-cipher", gjson.GetBytes(upstream.bodies[0], "input.0.encrypted_content").String())
	require.Equal(t, "stale-cipher", gjson.GetBytes(upstream.bodies[0], "input.1.encrypted_content").String())
}

func TestOpenAIEncryptedRecoveryForwardOAuth(t *testing.T) {
	svc, _, c, rec, account := newEncryptedRecoveryForwardTest(t)
	// 官方 OAuth 请求可能压缩出站，使用能解压 wire 正文的夹具检查实际历史内容。
	upstream := &officialForwardBodyRecorder{responses: []func() *http.Response{
		func() *http.Response {
			return encryptedRecoveryTestResponse(200, encryptedRecoveryTestEvent(`{"type":"response.created","response":{"id":"resp_rejected"}}`)+encryptedRecoveryTestFailure(), "rid-rejected")
		},
		func() *http.Response {
			return encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-recovered")
		},
	}}
	svc.httpUpstream = upstream
	account.Type = AccountTypeOAuth
	account.Credentials = map[string]any{"access_token": "test-token", "chatgpt_account_id": "test-account"}
	setOfficialCodexForceHTTPFallback(c, true)
	svc.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(`{"models":[{"slug":"gpt-6.1-sol","use_responses_lite":true}]}`))
	result, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":`+encryptedRecoveryTestInput+`}`))
	require.NoError(t, err)
	require.NotNil(t, result)
	require.Len(t, upstream.calls, 2)
	first, second := upstream.calls[0].decoded, upstream.calls[1].decoded
	require.True(t, gjson.ValidBytes(first))
	require.True(t, gjson.ValidBytes(second))
	firstReasoning := gjson.GetBytes(first, `input.#(type=="reasoning")`)
	secondReasoning := gjson.GetBytes(second, `input.#(type=="reasoning")`)
	require.Equal(t, "stale-cipher", firstReasoning.Get("encrypted_content").String())
	require.False(t, secondReasoning.Get("encrypted_content").Exists())
	require.Equal(t, "保留摘要", secondReasoning.Get("summary.0.text").String())
	firstInput, secondInput := gjson.GetBytes(first, "input").Array(), gjson.GetBytes(second, "input").Array()
	require.Len(t, secondInput, len(firstInput))
	for index, item := range firstInput {
		if item.Get("type").String() != "reasoning" {
			require.JSONEq(t, item.Raw, secondInput[index].Raw)
		}
	}
	require.NotContains(t, rec.Body.String(), "resp_rejected")
	require.Equal(t, "rid-recovered", rec.Header().Get("X-Request-Id"))
}

func TestOpenAIEncryptedRecoveryForwardOrdinaryError(t *testing.T) {
	svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
		encryptedRecoveryTestResponse(200, encryptedRecoveryTestEvent(`{"type":"response.failed","response":{"status":"failed","error":{"code":"bad_argument","type":"invalid_request_error","message":"ordinary error"}}}`), "rid-ordinary"),
		encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
	)
	_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":`+encryptedRecoveryTestInput+`}`))
	require.Error(t, err)
	require.Equal(t, 1, upstream.callCount)
	require.Equal(t, "stale-cipher", gjson.GetBytes(upstream.bodies[0], "input.1.encrypted_content").String())
	require.Contains(t, rec.Body.String(), "bad_argument")
	require.False(t, c.GetBool(openAIEncryptedContentRecoveryTriedKey))
}

func TestOpenAIEncryptedRecoveryForwardCacheIsolation(t *testing.T) {
	for _, scenario := range []string{"同一链路", "切换模型", "切换账号", "切换会话", "缓存过期", "没有链路标识"} {
		t.Run(scenario, func(t *testing.T) {
			svc, upstream, c, _, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestFailure(), "rid-rejected"),
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-recovered"),
			)
			chain := `,"previous_response_id":"resp_history"`
			if scenario == "没有链路标识" {
				chain = ""
			}
			body := []byte(`{"model":"gpt-6.1-sol","stream":true` + chain + `,"input":` + encryptedRecoveryTestInput + `}`)
			_, err := svc.Forward(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, 2, upstream.callCount)
			// HTTP 不再向缺少账号、模型维度的旧 WS 会话摘要表写入失效状态。
			require.False(t, svc.getOpenAIWSStateStore().HasAnySessionInvalidEncryptedContent())
			model := "gpt-6.1-sol"
			if scenario == "切换模型" {
				model = "gpt-6-astra"
			}
			if scenario == "切换账号" {
				account.ID++
			}
			if scenario == "切换会话" {
				chain = `,"previous_response_id":"resp_other_history"`
			}
			if scenario == "缓存过期" {
				for key, binding := range svc.openaiInvalidEncryptedScopes {
					binding.expiresAt = time.Now().Add(-time.Second)
					svc.openaiInvalidEncryptedScopes[key] = binding
				}
			}
			_, nextUpstream, nextContext, _, _ := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-next"),
			)
			svc.httpUpstream = nextUpstream
			nextBody := []byte(`{"model":"` + model + `","stream":true` + chain + `,"input":[{"type":"reasoning","id":"rs_history","encrypted_content":"stale-cipher","summary":[]},{"type":"reasoning","encrypted_content":"fresh-cipher","summary":[]},{"role":"user","content":"下一轮"}]}`)
			_, err = svc.Forward(context.Background(), nextContext, account, nextBody)
			require.NoError(t, err)
			require.Equal(t, 1, nextUpstream.callCount)
			stalePresent := gjson.GetBytes(nextUpstream.bodies[0], "input.0.encrypted_content").Exists()
			require.Equal(t, scenario != "同一链路", stalePresent, "只有同一账号、模型和链路中的未过期命中项允许预清洗")
			require.Equal(t, "fresh-cipher", gjson.GetBytes(nextUpstream.bodies[0], "input.1.encrypted_content").String())
		})
	}
}

func TestOpenAIEncryptedRecoveryForwardBudgetSurvivesForwardReentry(t *testing.T) {
	svc, upstream, c, rec, account := newEncryptedRecoveryForwardTest(t,
		encryptedRecoveryTestResponse(200, encryptedRecoveryTestFailure(), "rid-rejected"),
		encryptedRecoveryTestResponse(200, encryptedRecoveryTestSuccess(), "rid-never"),
	)
	// 模拟外层调度器已经在同一入站请求的前一个 Forward attempt 中用过恢复额度。
	c.Set(openAIEncryptedContentRecoveryTriedKey, true)
	_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":`+encryptedRecoveryTestInput+`}`))
	require.Error(t, err)
	require.Equal(t, 1, upstream.callCount)
	require.Contains(t, rec.Body.String(), "新建会话")
}

func TestOpenAIEncryptedRecoveryWindowAndKeepalive(t *testing.T) {
	for _, scenario := range []string{"正常窗口", "窗口过期", "仅发送心跳", "已经发送事件"} {
		t.Run(scenario, func(t *testing.T) {
			svc, _, c, rec, account := newEncryptedRecoveryForwardTest(t)
			started := time.Now()
			if scenario == "窗口过期" {
				started = started.Add(-openAIEncryptedContentRecoveryWindow - time.Second)
			}
			if scenario == "仅发送心跳" {
				n, err := c.Writer.Write([]byte(":\n\n"))
				require.NoError(t, err)
				recordOpenAIStreamKeepaliveBytes(c, n)
				c.Writer.Flush()
			}
			if scenario == "已经发送事件" {
				_, err := c.Writer.WriteString(encryptedRecoveryTestEvent(`{"type":"response.created","response":{"id":"resp_committed"}}`))
				require.NoError(t, err)
				c.Writer.Flush()
			}
			_, err := svc.handleStreamingResponse(context.Background(), encryptedRecoveryTestResponse(200, encryptedRecoveryTestFailure(), "rid-window"), c, account, started, "gpt-6.1-sol", "gpt-6.1-sol")
			require.Error(t, err)
			signal, ok := asOpenAIEncryptedContentRecoverySignal(err)
			if scenario == "已经发送事件" {
				require.False(t, ok)
				require.Contains(t, rec.Body.String(), "新建会话")
			} else {
				require.True(t, ok)
				require.Equal(t, scenario != "窗口过期", signal.safeToRetry)
				require.NotContains(t, rec.Body.String(), "response.failed", "决定恢复前不得提交失败事件")
			}
		})
	}
}

func TestOpenAIEncryptedRecoveryFailureStopsCompactKeepalive(t *testing.T) {
	for _, started := range []bool{false, true} {
		t.Run(map[bool]string{false: "心跳未提交", true: "心跳已提交"}[started], func(t *testing.T) {
			c, rec := newCompactBridgeTestContext(t, true)
			interval := time.Hour
			if started {
				interval = keepaliveTestInterval
			}
			stop := StartOpenAICompactSSEKeepalive(c, interval)
			defer stop()
			if started {
				waitForKeepaliveBeats()
			}
			err := writeOpenAIEncryptedContentFailure(c, encryptedRecoveryTestResponse(400, "", "rid-keepalive"), false, "gpt-6.1-sol", "resp_failed")
			require.Error(t, err)
			if started {
				require.Equal(t, http.StatusOK, rec.Code)
				require.Equal(t, 1, strings.Count(rec.Body.String(), "event: response.failed"))
				require.Contains(t, rec.Header().Get("Content-Type"), "text/event-stream")
			} else {
				require.Equal(t, http.StatusBadRequest, rec.Code)
				require.True(t, json.Valid(rec.Body.Bytes()))
				require.Contains(t, rec.Header().Get("Content-Type"), "application/json")
			}
			length := rec.Body.Len()
			waitForKeepaliveBeats()
			require.Equal(t, length, rec.Body.Len(), "终止错误后不得继续追加心跳")
		})
	}
}

func TestOpenAIEncryptedRecoveryRedactsDiagnostics(t *testing.T) {
	logSink, restore := captureStructuredLog(t)
	defer restore()
	sensitive := "sensitive-cipher-token-and-chat-body"
	errorJSON := `{"code":"invalid_encrypted_content","message":"` + sensitive + `","debug":"` + sensitive + `"}`
	for _, failure := range []struct {
		status int
		body   string
	}{
		{200, encryptedRecoveryTestEvent(`{"type":"response.failed","response":{"status":"failed","output":[],"error":` + errorJSON + `}}`)},
		{200, encryptedRecoveryTestEvent(`{"type":"error","sequence_number":7,"code":"invalid_encrypted_content","message":"` + sensitive + `","debug":"` + sensitive + `"}`)},
		{200, encryptedRecoveryTestEvent(`{"type":"error","error":` + errorJSON + `}`)},
		{400, `{"error":` + errorJSON + `}`},
	} {
		for _, committed := range []bool{false, true} {
			svc, _, c, rec, account := newEncryptedRecoveryForwardTest(t,
				encryptedRecoveryTestResponse(failure.status, failure.body, "rid-sensitive"),
			)
			svc.cfg.Gateway.LogUpstreamErrorBody = true
			if committed {
				_, err := c.Writer.WriteString(encryptedRecoveryTestEvent(`{"type":"response.output_text.delta","delta":"已提交"}`))
				require.NoError(t, err)
				c.Writer.Flush()
			}
			_, err := svc.Forward(context.Background(), c, account, []byte(`{"model":"gpt-6.1-sol","stream":true,"input":[{"role":"user","content":"继续"}]}`))
			require.Error(t, err)
			require.NotContains(t, rec.Body.String(), sensitive)
			events, _ := c.Get(OpsUpstreamErrorsKey)
			encoded, marshalErr := json.Marshal(events)
			require.NoError(t, marshalErr)
			require.NotContains(t, string(encoded), sensitive)
			require.Contains(t, string(encoded), "invalid_encrypted_content")
		}
	}
	require.False(t, logSink.ContainsMessage(sensitive))
}
