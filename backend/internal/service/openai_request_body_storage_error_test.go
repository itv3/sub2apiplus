package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"syscall"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func requestBodyStorageTestErrors() map[string]error {
	return map[string]error{
		"inbound":  &pkghttputil.RequestBodyStorageError{Err: syscall.ECONNREFUSED},
		"outbound": &officialegress.RequestBodyStorageError{Operation: "read", Err: syscall.ECONNREFUSED},
	}
}

func TestRequestBodyStorageErrorClassificationAndTransport(t *testing.T) {
	for name, localErr := range requestBodyStorageTestErrors() {
		for _, wrapped := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/wrapped=%v", name, wrapped), func(t *testing.T) {
				err := localErr
				if wrapped {
					err = fmt.Errorf("forward: %w", &url.Error{Op: "Post", URL: "https://example.test", Err: localErr})
				}
				require.True(t, IsRequestBodyStorageError(err))
				// 内部原因故意使用会触发持久网络故障的 ECONNREFUSED，确保类型归因优先。
				repo := &openAIAPIKeyHealthAccountRepo{}
				svc := &OpenAIGatewayService{accountRepo: repo}
				account := openAIHealthPoolAccount()
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
				returned := svc.handleOpenAIUpstreamTransportError(context.Background(), c, account, err, true)
				require.Same(t, err, returned)
				var failoverErr *UpstreamFailoverError
				require.False(t, errors.As(returned, &failoverErr))
				require.Zero(t, repo.setCalls, "本地故障不能临时停用账号")
				require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
				_, _, eligible := classifyOpenAIAPIKeyHealthFailure(returned)
				require.False(t, eligible, "本地故障不能触发账号健康熔断")
				_, recorded := c.Get(OpsUpstreamErrorsKey)
				require.False(t, recorded, "本地故障不能记成上游故障事件")
			})
		}
	}
	require.False(t, IsRequestBodyStorageError(nil))
	require.False(t, IsRequestBodyStorageError(io.ErrUnexpectedEOF))
}

func TestProxyOpenAIWSHTTPBridgeTurnStorageFailureClosesWithoutFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for name, localErr := range requestBodyStorageTestErrors() {
		for _, turn := range []int{1, 2} {
			for _, inStream := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/turn=%d/stream=%v", name, turn, inStream), func(t *testing.T) {
					wrapped := fmt.Errorf("request body: %w", localErr)
					upstream := &httpUpstreamRecorder{err: wrapped}
					if inStream {
						upstream.err = nil
						upstream.resp = &http.Response{StatusCode: http.StatusOK,
							Header: http.Header{"Content-Type": []string{"text/event-stream"}},
							Body:   passthroughErrReadCloser{err: wrapped}}
					}
					repo := &openAIAPIKeyHealthAccountRepo{}
					svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream, accountRepo: repo}
					account := &Account{ID: 8, Name: "healthy", Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Concurrency: 1}
					c, _ := gin.CreateTestContext(httptest.NewRecorder())
					c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil)
					payload := []byte(`{"type":"response.create","model":"gpt-5","input":"hi"}`)
					writes := 0
					_, err := svc.proxyOpenAIWSHTTPBridgeTurn(context.Background(), c, account, "sk-test", payload, len(payload),
						"gpt-5", "", "", "", "", turn, func([]byte) error { writes++; return nil })
					var closeErr *OpenAIWSClientCloseError
					require.ErrorAs(t, err, &closeErr)
					require.Equal(t, coderws.StatusTryAgainLater, closeErr.StatusCode())
					require.True(t, IsRequestBodyStorageError(err), "关闭错误必须保留本地原因")
					var failoverErr *UpstreamFailoverError
					require.False(t, errors.As(err, &failoverErr))
					require.Len(t, upstream.requests, 1, "本地故障不得重发请求")
					require.Zero(t, writes, "不得向 WS 客户端伪造上游 502")
					require.Zero(t, repo.setCalls)
					require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
				})
			}
		}
	}
}
