package httpclient

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestValidatedTransport_CacheHostValidation(t *testing.T) {
	originalValidate := validateResolvedIP
	defer func() { validateResolvedIP = originalValidate }()

	var validateCalls int32
	validateResolvedIP = func(host string) error {
		atomic.AddInt32(&validateCalls, 1)
		require.Equal(t, "api.openai.com", host)
		return nil
	}

	var baseCalls int32
	base := roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		atomic.AddInt32(&baseCalls, 1)
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       io.NopCloser(strings.NewReader(`{}`)),
			Header:     make(http.Header),
		}, nil
	})

	now := time.Unix(1730000000, 0)
	transport := newValidatedTransport(base)
	transport.now = func() time.Time { return now }

	req, err := http.NewRequest(http.MethodGet, "https://api.openai.com/v1/responses", nil)
	require.NoError(t, err)

	_, err = transport.RoundTrip(req)
	require.NoError(t, err)
	_, err = transport.RoundTrip(req)
	require.NoError(t, err)

	require.Equal(t, int32(1), atomic.LoadInt32(&validateCalls))
	require.Equal(t, int32(2), atomic.LoadInt32(&baseCalls))
}

func TestValidatedTransport_ExpiredCacheTriggersRevalidation(t *testing.T) {
	originalValidate := validateResolvedIP
	defer func() { validateResolvedIP = originalValidate }()

	var validateCalls int32
	validateResolvedIP = func(_ string) error {
		atomic.AddInt32(&validateCalls, 1)
		return nil
	}

	base := roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       io.NopCloser(strings.NewReader(`{}`)),
			Header:     make(http.Header),
		}, nil
	})

	now := time.Unix(1730001000, 0)
	transport := newValidatedTransport(base)
	transport.now = func() time.Time { return now }

	req, err := http.NewRequest(http.MethodGet, "https://api.openai.com/v1/responses", nil)
	require.NoError(t, err)

	_, err = transport.RoundTrip(req)
	require.NoError(t, err)

	now = now.Add(validatedHostTTL + time.Second)
	_, err = transport.RoundTrip(req)
	require.NoError(t, err)

	require.Equal(t, int32(2), atomic.LoadInt32(&validateCalls))
}

func TestValidatedTransport_ValidationErrorStopsRoundTrip(t *testing.T) {
	originalValidate := validateResolvedIP
	defer func() { validateResolvedIP = originalValidate }()

	expectedErr := errors.New("dns rebinding rejected")
	validateResolvedIP = func(_ string) error {
		return expectedErr
	}

	var baseCalls int32
	base := roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		atomic.AddInt32(&baseCalls, 1)
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	})

	transport := newValidatedTransport(base)
	req, err := http.NewRequest(http.MethodGet, "https://api.openai.com/v1/responses", nil)
	require.NoError(t, err)

	_, err = transport.RoundTrip(req)
	require.ErrorIs(t, err, expectedErr)
	require.Equal(t, int32(0), atomic.LoadInt32(&baseCalls))
}

// TestBuildTransportWithCustomDialKeepsHTTP2Disabled 固化普通共享客户端的协议事实：
// buildTransport 设置自定义 DialContext，但没有开启 ForceAttemptHTTP2。按 net/http
// 契约，即使服务端支持 h2，这类 Transport 也只会协商 HTTP/1.1。
//
// Codex 用量探针、PAT whoami 与 Agent Identity 注册当前都复用该客户端；此测试只记录
// 变更集 0 发现的现状，不表示该协议选择符合对应 persona。
func TestBuildTransportWithCustomDialKeepsHTTP2Disabled(t *testing.T) {
	protocol := ""
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		protocol = r.Proto
		w.WriteHeader(http.StatusOK)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	t.Cleanup(server.Close)

	transport, err := buildTransport(Options{})
	require.NoError(t, err)
	require.NotNil(t, transport.DialContext)
	require.False(t, transport.ForceAttemptHTTP2)

	// 本地测试服务使用自签证书；只在这个新建的测试 Transport 上跳过校验。
	transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec
	client := &http.Client{Transport: transport}
	response, err := client.Get(server.URL)
	require.NoError(t, err)
	t.Cleanup(func() { _ = response.Body.Close() })

	require.Equal(t, "HTTP/1.1", protocol)
}

type sharedPoolWireFact struct {
	method     string
	requestURI string
	body       string
	header     http.Header
}

// TestSharedPoolGuardPreservesOutOfScopeWireAndResult 是共享 net/http 栈的 1A
// before/after 对照：Guard 接入后请求事实和响应结果不变，且第三方流量不会变成 unknown。
func TestSharedPoolGuardPreservesOutOfScopeWireAndResult(t *testing.T) {
	facts := make(chan sharedPoolWireFact, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		require.NoError(t, err)
		facts <- sharedPoolWireFact{
			method: request.Method, requestURI: request.RequestURI,
			body: string(body), header: request.Header.Clone(),
		}
		w.Header().Set("X-Test-Result", "same")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte("same-response"))
	}))
	t.Cleanup(server.Close)

	newRequest := func() *http.Request {
		request, err := http.NewRequest(
			http.MethodPost,
			server.URL+"/third-party/messages?mode=exact",
			bytes.NewBufferString("same-body"),
		)
		require.NoError(t, err)
		request.Header.Set("X-Test-Header", "same-value")
		return request
	}
	readResult := func(response *http.Response, err error) string {
		require.NoError(t, err)
		defer func() { require.NoError(t, response.Body.Close()) }()
		body, readErr := io.ReadAll(response.Body)
		require.NoError(t, readErr)
		return response.Status + "|" + response.Header.Get("X-Test-Result") + "|" + string(body)
	}

	baseTransport, err := buildTransport(Options{})
	require.NoError(t, err)
	before := readResult((&http.Client{Transport: baseTransport}).Do(newRequest()))

	recorder := officialegress.NewBoundedGuardRecorder(16, slog.New(slog.NewTextHandler(io.Discard, nil)))
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{},
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		recorder,
	)
	require.NoError(t, err)
	guardedClient, err := buildClientWithGuard(Options{}, guard)
	require.NoError(t, err)
	after := readResult(guardedClient.Do(newRequest()))

	require.Equal(t, before, after)
	require.Equal(t, <-facts, <-facts)
	metrics := recorder.Snapshot()
	require.Len(t, metrics, 1)
	require.Equal(t, officialegress.ReasonOutOfScopePassthrough, metrics[0].Reason)
}

func TestSharedPoolGuardPreservesErrorCancellationAndRedirect(t *testing.T) {
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{}, officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), nil,
	)
	require.NoError(t, err)
	sentinel := errors.New("合成 transport 错误")
	base := roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, sentinel })
	request, err := http.NewRequest(http.MethodGet, "https://example.com/failure", nil)
	require.NoError(t, err)
	beforeErr := func() error {
		_, sendErr := (&http.Client{Transport: base}).Do(request.Clone(context.Background()))
		return sendErr
	}()
	afterErr := func() error {
		transport := officialegress.NewGuardedRoundTripper(
			base, guard, officialegress.BackendPlainNetHTTP, officialegress.WireProtocolHTTP,
		)
		_, sendErr := (&http.Client{Transport: transport}).Do(request.Clone(context.Background()))
		return sendErr
	}()
	for _, sendErr := range []error{beforeErr, afterErr} {
		require.ErrorIs(t, sendErr, sentinel)
		var urlError *url.Error
		require.ErrorAs(t, sendErr, &urlError)
		require.Equal(t, "Get", urlError.Op)
	}

	cancelBase := roundTripFunc(func(request *http.Request) (*http.Response, error) {
		<-request.Context().Done()
		return nil, request.Context().Err()
	})
	for _, transport := range []http.RoundTripper{
		cancelBase,
		officialegress.NewGuardedRoundTripper(
			cancelBase, guard, officialegress.BackendPlainNetHTTP, officialegress.WireProtocolHTTP,
		),
	} {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		cancelled := request.Clone(ctx)
		_, cancelErr := (&http.Client{Transport: transport}).Do(cancelled)
		require.ErrorIs(t, cancelErr, context.Canceled)
	}

	final := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
	}))
	t.Cleanup(final.Close)
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		http.Redirect(w, request, final.URL, http.StatusFound)
	}))
	t.Cleanup(redirect.Close)
	beforeResponse, err := http.Get(redirect.URL)
	require.NoError(t, err)
	require.NoError(t, beforeResponse.Body.Close())
	afterClient := &http.Client{Transport: officialegress.NewGuardedRoundTripper(
		http.DefaultTransport, guard, officialegress.BackendPlainNetHTTP, officialegress.WireProtocolHTTP,
	)}
	afterResponse, err := afterClient.Get(redirect.URL)
	require.NoError(t, err)
	require.NoError(t, afterResponse.Body.Close())
	require.Equal(t, beforeResponse.StatusCode, afterResponse.StatusCode)
	require.Equal(t, beforeResponse.Request.URL.String(), afterResponse.Request.URL.String())
}
