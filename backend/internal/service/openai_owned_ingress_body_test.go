package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 兼容账号的 Transport 可以先返回完整响应，再读取剩余上传或重放正文。
// 用真实匿名映射作为入口，释放后再读出站，防止仅凭 Go 堆仍未回收而误判安全。
func TestOpenAIBorrowedIngressCompatibilityOutlivesOwner(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		t.Run(fmt.Sprintf("passthrough_%t", passthrough), func(t *testing.T) {
			text := strings.Repeat("请求生命周期", 128<<10)
			raw := []byte(`{"model":"gpt-5.4","stream":false,"input":[{"type":"message","role":"user","content":"` + text + `"}]}`)
			before := pkghttputil.ActiveOwnedRequestBodyBytes()
			request := httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(raw))
			owner, err := pkghttputil.ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(request, int64(len(raw)), nil)
			require.NoError(t, err)
			defer owner.Close()
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Request.Header.Set("User-Agent", "curl/8.0")
			upstream := &ownedIngressEarlyResponseUpstream{}
			service := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			account := &Account{ID: 456, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Concurrency: 1,
				Credentials: map[string]any{"api_key": "test-key", "base_url": "https://api.openai.com"},
				Extra:       map[string]any{"openai_passthrough": passthrough}, Status: StatusActive, Schedulable: true}
			result, err := service.Forward(WithOpenAIBorrowedIngressBody(context.Background()), c, account, owner.Bytes())
			require.NoError(t, err)
			require.NotNil(t, result)
			require.NotNil(t, upstream.request)
			require.NoError(t, owner.Close())
			require.Equal(t, before, pkghttputil.ActiveOwnedRequestBodyBytes())
			wire, err := io.ReadAll(upstream.request.Body)
			require.NoError(t, err)
			require.NoError(t, upstream.request.Body.Close())
			require.Equal(t, text, gjson.GetBytes(wire, "input.0.content").String())
			require.NotNil(t, upstream.request.GetBody)
			replay, err := upstream.request.GetBody()
			require.NoError(t, err)
			defer replay.Close()
			digest := sha256.New()
			_, err = io.Copy(digest, replay)
			require.NoError(t, err)
			expected := sha256.Sum256(wire)
			require.Equal(t, expected[:], digest.Sum(nil), "入口释放之后 GetBody 必须仍能重放完整出站")
		})
	}
}

type ownedIngressEarlyResponseUpstream struct{ request *http.Request }

func (u *ownedIngressEarlyResponseUpstream) Do(request *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	u.request = request
	return &http.Response{StatusCode: http.StatusOK,
		Header: http.Header{"Content-Type": []string{"application/json"}},
		Body:   io.NopCloser(strings.NewReader(`{"id":"resp-owned","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`)),
	}, nil
}

func (u *ownedIngressEarlyResponseUpstream) DoWithTLS(request *http.Request, proxyURL string, accountID int64, concurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return u.Do(request, proxyURL, accountID, concurrency)
}
