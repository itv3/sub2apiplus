//go:build unit

package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 上游原本复用 openai_astra_pro_mode_test.go 里的 Astra 辅助；本分支把该文件放到 astra 构建标签之后
// （Astra 不在当前 Codex 仿真范围），这里改为自带同等的最小替身，保证 unit 构建下该用例仍然执行。
const webSearchHistoryCodexResponsesURL = "https://chatgpt.com/backend-api/codex/responses"

type webSearchHistoryForwardSetup struct {
	upstream *httpUpstreamRecorder
	svc      *OpenAIGatewayService
	c        *gin.Context
	rec      *httptest.ResponseRecorder
	account  *Account
}

// newWebSearchHistoryOAuthSetup 构造最小的 OpenAI Codex 协议账号转发环境；passthrough 为真时走透传分支。
// accountType 取 AccountTypeOAuth（经官方出站定型器定型）或 AccountTypeSetupToken（不经官方出站定型）。
func newWebSearchHistoryOAuthSetup(t *testing.T, passthrough bool, accountType string) *webSearchHistoryForwardSetup {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "codex_cli_rs/0.144.1")
	upstream := &httpUpstreamRecorder{}
	svc := &OpenAIGatewayService{
		cfg:          &config.Config{Gateway: config.GatewayConfig{ForceCodexCLI: false}},
		httpUpstream: upstream,
	}
	account := &Account{
		ID:             888,
		Name:           "oauth-web-search-history",
		Platform:       PlatformOpenAI,
		Type:           accountType,
		Concurrency:    1,
		Credentials:    map[string]any{"access_token": "oauth-token", "chatgpt_account_id": "chatgpt-acc"},
		Extra:          map[string]any{"openai_passthrough": passthrough},
		Status:         StatusActive,
		Schedulable:    true,
		RateMultiplier: f64p(1),
	}
	return &webSearchHistoryForwardSetup{upstream: upstream, svc: svc, c: c, rec: rec, account: account}
}

func webSearchHistoryCompletedSSE(inner string) string {
	return "data: {\"type\":\"response.completed\",\"response\":" + inner + "}\n\ndata: [DONE]\n\n"
}

// TestForward_OAuthWebSearchHistoryDeclaresTool drives the Codex compaction
// shape (web_search_call history, tools:[]) through Forward.
//
// 本分支的出站约束：经官方出站定型器定型的 Codex OAuth 请求，出站字节里不得出现网关代注入的
// web_search 工具（顶层或 additional_tools），也不得把 tool_choice 钉成 none；只有不经官方出站定型的
// 账号（OpenAI SetupToken）才沿用上游语义——顶层声明 web_search，Lite 则放进 input additional_tools（#7927）。
func TestForward_OAuthWebSearchHistoryDeclaresTool(t *testing.T) {
	for _, tc := range []struct {
		name        string
		accountType string
		passthrough bool
		lite        bool
		inject      bool
	}{
		{"official OAuth transform", AccountTypeOAuth, false, false, false},
		{"official OAuth passthrough", AccountTypeOAuth, true, false, false},
		{"official OAuth transform lite", AccountTypeOAuth, false, true, false},
		{"official OAuth passthrough lite", AccountTypeOAuth, true, true, false},
		{"non-official setup-token transform", AccountTypeSetupToken, false, false, true},
		{"non-official setup-token passthrough", AccountTypeSetupToken, true, false, true},
		{"non-official setup-token transform lite", AccountTypeSetupToken, false, true, true},
		{"non-official setup-token passthrough lite", AccountTypeSetupToken, true, true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gin.SetMode(gin.TestMode)
			s := newWebSearchHistoryOAuthSetup(t, tc.passthrough, tc.accountType)
			if !tc.inject {
				// 官方出站分支断言旧画像的 input 固定下标；非官方出站（SetupToken）不属于 Codex 画像，不受槽位影响。
				useOfficialCodexLegacyProfile(t, s.svc)
			}
			if tc.lite {
				s.c.Request.Header.Set(responsesLiteHeader, "true")
			}
			inner := `{"id":"resp_test","model":"gpt-5.5","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}`
			s.upstream.resp = &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
				Body:       io.NopCloser(strings.NewReader(webSearchHistoryCompletedSSE(inner))),
			}
			body, err := sjson.SetRawBytes([]byte(openAIWebSearchHistoryCompactionBody), "input.-1", []byte(`{"type":"compaction_trigger"}`))
			require.NoError(t, err)

			result, err := s.svc.Forward(context.Background(), s.c, s.account, body)
			require.NoError(t, err)
			require.NotNil(t, result)
			require.NotNil(t, s.upstream.lastReq)
			require.Equal(t, webSearchHistoryCodexResponsesURL, s.upstream.lastReq.URL.String())

			forwarded := s.upstream.lastBody
			require.Equal(t, "web_search_call", gjson.GetBytes(forwarded, "input.1.type").String())
			items := gjson.GetBytes(forwarded, "input").Array()
			require.Equal(t, "compaction_trigger", items[len(items)-1].Get("type").String())
			if !tc.inject {
				// 官方出站：不代注入顶层或 additional_tools 里的 web_search，也不把 tool_choice 钉成 none。
				require.False(t, gjsonToolsContainWebSearch(gjson.GetBytes(forwarded, "tools")))
				for _, item := range items {
					if item.Get("type").String() == "additional_tools" {
						require.False(t, gjsonToolsContainWebSearch(item.Get("tools")), "official egress must not carry injected web_search")
					}
				}
				require.NotEqual(t, "none", gjson.GetBytes(forwarded, "tool_choice").String())
				return
			}
			require.Equal(t, "none", gjson.GetBytes(forwarded, "tool_choice").String())
			if !tc.lite {
				tools := gjson.GetBytes(forwarded, "tools").Array()
				require.Len(t, tools, 1)
				require.Equal(t, "web_search", tools[0].Get("type").String())
				return
			}
			require.False(t, gjsonToolsContainWebSearch(gjson.GetBytes(forwarded, "tools")))
			additional := items[len(items)-2]
			require.Equal(t, "additional_tools", additional.Get("type").String())
			require.Equal(t, "web_search", additional.Get("tools.0.type").String())
		})
	}
}
