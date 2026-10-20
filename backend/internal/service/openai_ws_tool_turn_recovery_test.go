package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestOpenAIGatewayServiceForwardWSv2TreatsToolHistoryBeforeUserAsHistorical(t *testing.T) {
	gin.SetMode(gin.TestMode)

	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "OpenAI/Python 2.24.0")

	cfg := newOpenAIWSV2TestConfig()
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.QueueLimitPerConn = 8

	captureConn := &openAIWSCaptureConn{events: [][]byte{
		[]byte(`{"type":"response.completed","response":{"id":"resp_tool_history_ok","model":"gpt-5.5","status":"completed","output":[],"usage":{"input_tokens":3,"output_tokens":1}}}`),
	}}
	captureDialer := &openAIWSCaptureDialer{conn: captureConn}
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(captureDialer)
	upstream := &httpUpstreamRecorder{}
	svc := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
		openaiWSPool:     pool,
	}
	useOfficialCodexLegacyProfile(t, svc) // 断言旧画像的 input 固定下标
	account := openAIWSToolTurnRecoveryOAuthAccount(9201)
	svc.openaiModelCapabilities.replaceFromManifest(
		account.ID,
		[]byte(`{"models":[{"slug":"gpt-5.5","use_responses_lite":false}]}`),
	)

	body := []byte(`{
		"model":"gpt-5.5","stream":false,
		"input":[
			{"type":"function_call","id":"fc_old","call_id":"call_old","name":"exec","arguments":"{}"},
			{"type":"function_call_output","call_id":"call_old","output":"ok"},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}
		]
	}`)
	result, err := svc.Forward(context.Background(), c, account, body)
	require.NoError(t, err)
	require.NotNil(t, result)
	require.True(t, result.OpenAIWSMode)
	require.Equal(t, "resp_tool_history_ok", result.RequestID)
	require.Nil(t, upstream.lastReq, "明确的新用户轮次应继续走 WS，不应回退 HTTP")

	requestJSON := requestToJSONString(captureConn.lastWrite)
	historicalCallTurn := gjson.Get(requestJSON, "input.0.internal_chat_message_metadata_passthrough.turn_id").String()
	historicalOutputTurn := gjson.Get(requestJSON, "input.1.internal_chat_message_metadata_passthrough.turn_id").String()
	currentUserTurn := gjson.Get(requestJSON, "input.2.internal_chat_message_metadata_passthrough.turn_id").String()
	currentFrameTurn := gjson.Get(requestJSON, "client_metadata.turn_id").String()
	require.NotEmpty(t, historicalCallTurn)
	require.Equal(t, historicalCallTurn, historicalOutputTurn)
	require.Equal(t, currentFrameTurn, currentUserTurn)
	require.NotEqual(t, historicalCallTurn, currentUserTurn)
}

func TestOpenAIGatewayServiceForwardWSv2AmbiguousToolTurnFallsBackToHTTP(t *testing.T) {
	gin.SetMode(gin.TestMode)

	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "OpenAI/Python 2.24.0")

	cfg := newOpenAIWSV2TestConfig()
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
	cfg.Gateway.OpenAIWS.MinIdlePerAccount = 0
	cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
	cfg.Gateway.OpenAIWS.QueueLimitPerConn = 8

	captureConn := &openAIWSCaptureConn{}
	captureDialer := &openAIWSCaptureDialer{conn: captureConn}
	pool := newOpenAIWSConnPool(cfg)
	pool.setClientDialerForTest(captureDialer)
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
		Body: io.NopCloser(strings.NewReader(
			"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_tool_http_fallback\",\"model\":\"gpt-5.5\",\"status\":\"completed\",\"output\":[],\"usage\":{\"input_tokens\":3,\"output_tokens\":1}}}\n\n",
		)),
	}}
	svc := &OpenAIGatewayService{
		cfg:              cfg,
		httpUpstream:     upstream,
		cache:            &stubGatewayCache{},
		openaiWSResolver: NewOpenAIWSProtocolResolver(cfg),
		toolCorrector:    NewCodexToolCorrector(),
		openaiWSPool:     pool,
	}
	useOfficialCodexLegacyProfile(t, svc) // 断言旧画像的 HTTP 回退正文下标
	account := openAIWSToolTurnRecoveryOAuthAccount(9202)
	svc.openaiModelCapabilities.replaceFromManifest(
		account.ID,
		[]byte(`{"models":[{"slug":"gpt-5.5","use_responses_lite":false}]}`),
	)

	// input_text 没有 role=user，不能把它当成可信的新轮次边界；WS 路径
	// 继续严格拒绝裁剪，但 HTTP 来源请求应保留完整历史回退到 Responses HTTP。
	body := []byte(`{
		"model":"gpt-5.5","stream":false,
		"input":[
			{"type":"function_call","id":"fc_ambiguous","call_id":"call_ambiguous","name":"exec","arguments":"{}"},
			{"type":"function_call_output","call_id":"call_ambiguous","output":"ok"},
			{"type":"input_text","text":"继续"}
		]
	}`)
	result, err := svc.Forward(context.Background(), c, account, body)
	require.NoError(t, err)
	require.NotNil(t, result)
	require.False(t, result.OpenAIWSMode)
	require.Equal(t, "resp_tool_http_fallback", result.ResponseID)
	require.NotNil(t, upstream.lastReq, "轮次不明时应进入同一冻结调用的 HTTP fallback")
	require.Empty(t, captureConn.writes, "WS 定型失败发生在业务请求写入之前")
	require.Equal(t, 1, captureDialer.DialCount())
	require.Len(t, gjson.GetBytes(upstream.lastBody, "input").Array(), 3)
}

func openAIWSToolTurnRecoveryOAuthAccount(id int64) *Account {
	return &Account{
		ID:          id,
		Name:        "openai-tool-turn-recovery",
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-tool-turn-recovery",
		},
		Extra: map[string]any{
			"responses_websockets_v2_enabled": true,
		},
	}
}
