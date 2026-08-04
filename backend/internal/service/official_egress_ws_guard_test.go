package service

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

type wsGuardCaptureRoundTripper struct {
	request *http.Request
}

func (c *wsGuardCaptureRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	c.request = request.Clone(request.Context())
	c.request.Header = request.Header.Clone()
	return &http.Response{
		StatusCode: http.StatusSwitchingProtocols,
		Status:     "101 Switching Protocols",
		Header:     http.Header{"X-Test-Result": []string{"same"}},
		Body:       http.NoBody,
		Request:    request,
	}, nil
}

// TestWebSocketHandshakeGuardPreservesWireAndResult 是 WebSocket 握手栈的 1A
// scope-0 before/after 对照。Codex Runtime Sink 在变更集 3 后必须持 Token；这里用
// 第三方 WS 证明 Guard 对范围外发送仍保持最终请求与响应完全一致。
func TestWebSocketHandshakeGuardPreservesWireAndResult(t *testing.T) {
	newRequest := func() *http.Request {
		request, requestErr := http.NewRequestWithContext(
			t.Context(),
			http.MethodGet,
			"https://example.com/ws",
			nil,
		)
		require.NoError(t, requestErr)
		request.Header.Set("Upgrade", "websocket")
		request.Header.Set("Connection", "Upgrade")
		request.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate")
		request.Header.Set("X-Test-Header", "same-value")
		return request
	}
	readResult := func(response *http.Response, err error) string {
		require.NoError(t, err)
		defer func() { _ = response.Body.Close() }()
		return response.Status + "|" + response.Header.Get("X-Test-Result")
	}

	beforeCapture := &wsGuardCaptureRoundTripper{}
	before := readResult((&officialEgressWebSocketRoundTripper{base: beforeCapture}).RoundTrip(newRequest()))

	recorder := officialegress.NewBoundedGuardRecorder(16, slog.New(slog.NewTextHandler(io.Discard, nil)))
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{},
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		recorder,
	)
	require.NoError(t, err)
	afterCapture := &wsGuardCaptureRoundTripper{}
	afterTransport := &officialEgressWebSocketRoundTripper{base: officialegress.NewGuardedRoundTripper(
		afterCapture,
		guard,
		officialegress.BackendWebSocket,
		officialegress.WireProtocolWebSocket,
	)}
	after := readResult(afterTransport.RoundTrip(newRequest()))

	require.Equal(t, before, after)
	require.Equal(t, beforeCapture.request.Method, afterCapture.request.Method)
	require.Equal(t, beforeCapture.request.URL.String(), afterCapture.request.URL.String())
	require.Equal(t, beforeCapture.request.Header, afterCapture.request.Header)
	require.Equal(t, officialOpenAIWSCompressionOffer,
		afterCapture.request.Header.Get("Sec-WebSocket-Extensions"))

	reasons := make(map[officialegress.GuardReason]bool)
	for _, metric := range recorder.Snapshot() {
		reasons[metric.Reason] = true
	}
	require.True(t, reasons[officialegress.ReasonOutOfScopePassthrough])
	require.False(t, reasons[officialegress.ReasonMissingFinalizationToken])
	require.False(t, reasons[officialegress.ReasonUnknownRoute])
}

type wsGuardErrorRoundTripper struct {
	err error
}

func (r wsGuardErrorRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	if r.err != nil {
		return nil, r.err
	}
	<-request.Context().Done()
	return nil, request.Context().Err()
}

func TestWebSocketHandshakeGuardPreservesErrorAndCancellation(t *testing.T) {
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{}, officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), nil,
	)
	require.NoError(t, err)
	newRequest := func(ctx context.Context) *http.Request {
		request, requestErr := http.NewRequestWithContext(
			ctx, http.MethodGet, "https://example.com/ws", nil,
		)
		require.NoError(t, requestErr)
		request.Header.Set("Upgrade", "websocket")
		request.Header.Set("Sec-WebSocket-Extensions", "permessage-deflate")
		return request
	}
	wrap := func(base http.RoundTripper, guarded bool) http.RoundTripper {
		if guarded {
			base = officialegress.NewGuardedRoundTripper(
				base, guard, officialegress.BackendWebSocket, officialegress.WireProtocolWebSocket,
			)
		}
		return &officialEgressWebSocketRoundTripper{base: base}
	}
	sentinel := errors.New("合成 WebSocket handshake 错误")
	for _, guarded := range []bool{false, true} {
		_, sendErr := wrap(wsGuardErrorRoundTripper{err: sentinel}, guarded).RoundTrip(
			newRequest(t.Context()),
		)
		require.ErrorIs(t, sendErr, sentinel)

		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		_, cancelErr := wrap(wsGuardErrorRoundTripper{}, guarded).RoundTrip(newRequest(ctx))
		require.ErrorIs(t, cancelErr, context.Canceled)
	}
}
