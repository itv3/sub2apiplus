package repository

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

type reqGuardWireFact struct {
	method     string
	requestURI string
	body       string
	header     http.Header
}

// TestReqProfileGuardPreservesOutOfScopeWireAndResult 是 req/v3 栈的 1A
// before/after 对照；两次都走 req.Transport 的真实 RoundTrip 链。
func TestReqProfileGuardPreservesOutOfScopeWireAndResult(t *testing.T) {
	facts := make(chan reqGuardWireFact, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		require.NoError(t, err)
		facts <- reqGuardWireFact{
			method: request.Method, requestURI: request.RequestURI,
			body: string(body), header: request.Header.Clone(),
		}
		w.Header().Set("X-Test-Result", "same")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte("same-response"))
	}))
	t.Cleanup(server.Close)

	send := func(client *req.Client) string {
		response, err := client.R().
			SetHeader("X-Test-Header", "same-value").
			SetBodyString("same-body").
			Post(server.URL + "/third-party/messages?mode=exact")
		require.NoError(t, err)
		return response.Status + "|" + response.Header.Get("X-Test-Result") + "|" + response.String()
	}

	before := send(req.C())
	recorder := officialegress.NewBoundedGuardRecorder(16, slog.New(slog.NewTextHandler(io.Discard, nil)))
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{},
		officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(),
		recorder,
	)
	require.NoError(t, err)
	after := send(instrumentReqClientWithGuard(req.C(), guard, nil))

	require.Equal(t, before, after)
	first, second := <-facts, <-facts
	require.Equal(t, first.method, second.method)
	require.Equal(t, first.requestURI, second.requestURI)
	require.Equal(t, first.body, second.body)
	require.Equal(t, first.header, second.header)
	metrics := recorder.Snapshot()
	require.Len(t, metrics, 1)
	require.Equal(t, officialegress.ReasonOutOfScopePassthrough, metrics[0].Reason)
}

func TestReqProfileGuardPreservesErrorCancellationAndRedirect(t *testing.T) {
	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{}, officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), nil,
	)
	require.NoError(t, err)
	sentinel := errors.New("合成 req transport 错误")
	newFailureClient := func(guarded bool) *req.Client {
		client := req.C()
		client.GetTransport().WrapRoundTripFunc(func(http.RoundTripper) req.HttpRoundTripFunc {
			return func(*http.Request) (*http.Response, error) { return nil, sentinel }
		})
		if guarded {
			client = instrumentReqClientWithGuard(client, guard, nil)
		}
		return client
	}
	for _, guarded := range []bool{false, true} {
		_, sendErr := newFailureClient(guarded).R().Get("https://example.com/failure")
		require.ErrorIs(t, sendErr, sentinel)
		var urlError *url.Error
		require.ErrorAs(t, sendErr, &urlError)
	}

	slow := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		time.Sleep(100 * time.Millisecond)
	}))
	t.Cleanup(slow.Close)
	for _, guarded := range []bool{false, true} {
		client := req.C()
		if guarded {
			client = instrumentReqClientWithGuard(client, guard, nil)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
		_, sendErr := client.R().SetContext(ctx).Get(slow.URL)
		cancel()
		require.ErrorIs(t, sendErr, context.DeadlineExceeded)
	}

	final := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte("redirected"))
	}))
	t.Cleanup(final.Close)
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		http.Redirect(w, request, final.URL, http.StatusFound)
	}))
	t.Cleanup(redirect.Close)
	results := make([]string, 0, 2)
	for _, guarded := range []bool{false, true} {
		client := req.C()
		if guarded {
			client = instrumentReqClientWithGuard(client, guard, nil)
		}
		response, sendErr := client.R().Get(redirect.URL)
		require.NoError(t, sendErr)
		results = append(results, response.Status+"|"+response.String())
	}
	require.Equal(t, results[0], results[1])
}
