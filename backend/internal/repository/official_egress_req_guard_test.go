package repository

import (
	"context"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
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

// TestReqProfileGuardLowercasesFinalWire 验证 lowercase 计划确实作用到了最终 wire。
//
// 注意这条只证明"到达 wire 的名字是小写"，证明不了 Guard 在链上的位置——链的最内层
// 无论包装顺序如何都在 lowercase 之内。位置由
// TestReqClientGuardWrapsBaseTransportDirectly 锁定。
func TestReqProfileGuardLowercasesFinalWire(t *testing.T) {
	var observed []string
	client := req.C()
	client.GetTransport().WrapRoundTripFunc(func(http.RoundTripper) req.HttpRoundTripFunc {
		return func(request *http.Request) (*http.Response, error) {
			for name := range request.Header {
				observed = append(observed, name)
			}
			return &http.Response{
				StatusCode: http.StatusOK, Body: http.NoBody,
				Header: make(http.Header), Request: request,
			}, nil
		}
	})

	guard, err := officialegress.NewGuard(
		officialegress.GuardConfig{}, officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), nil,
	)
	require.NoError(t, err)
	profile := &tlsfingerprint.Profile{}
	profile.Transport.LowercaseHeaders = true
	profile.Transport.PreserveHeaderCase = []string{"X-Keep-Case"}

	_, sendErr := instrumentReqClientWithGuard(client, guard, profile).R().
		SetHeader("Accept", "text/event-stream").
		SetHeader("X-Keep-Case", "kept").
		Get("https://example.com/anything")
	require.NoError(t, sendErr)

	require.Contains(t, observed, "accept", "Accept 必须在到达 wire 前被小写化")
	require.NotContains(t, observed, "Accept")
	require.Contains(t, observed, "X-Keep-Case", "PreserveHeaderCase 必须原样保留")
}

// TestReqClientGuardWrapsBaseTransportDirectly 锁住 req.Client 链的包装顺序。
//
// 画像声明 lowercase wire 名时，compiler 用 headers.Set() 写入的名字会被 Go 规范化成
// Accept。Guard 校验的是最终 wire，所以小写化必须发生在 Guard 之外——即 Guard 直接包裹
// 原始 transport，lowercase 包在 Guard 之上，与 http_upstream 链的 lowercase(Guard(base))
// 一致。顺序反了 Guard 看到的仍是 Accept，会把画像自己的 lowercase 计划误判成
// request_modified_after_finalize 并拒绝出站——OAuth token 刷新曾因此在生产上完全不可用。
//
// 这个事实无法从链外观测（任何外部探针都落在 lowercase 的同一侧），只能对源码断言。
func TestReqClientGuardWrapsBaseTransportDirectly(t *testing.T) {
	const baseIdent = "rt" // WrapRoundTripFunc 回调收到的原始 transport 形参

	fileSet := token.NewFileSet()
	parsed, err := parser.ParseFile(fileSet, "official_egress_guard.go", nil, 0)
	require.NoError(t, err)

	var target *ast.FuncDecl
	ast.Inspect(parsed, func(node ast.Node) bool {
		if decl, ok := node.(*ast.FuncDecl); ok && decl.Name.Name == "instrumentReqClientWithGuard" {
			target = decl
			return false
		}
		return true
	})
	require.NotNil(t, target, "未找到 instrumentReqClientWithGuard")

	firstArgIdent := func(call *ast.CallExpr) string {
		if len(call.Args) == 0 {
			return ""
		}
		ident, ok := call.Args[0].(*ast.Ident)
		if !ok {
			return ""
		}
		return ident.Name
	}

	var guardedBase, loweredBase string
	ast.Inspect(target, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		switch selector.Sel.Name {
		case "NewGuardedRoundTripper":
			guardedBase = firstArgIdent(call)
		case "NewLowercaseHeaderRoundTripper":
			loweredBase = firstArgIdent(call)
		}
		return true
	})

	require.Equal(t, baseIdent, guardedBase,
		"Guard 必须直接包裹原始 transport；包在 lowercase 之内会让它看不到最终 wire")
	require.NotEmpty(t, loweredBase, "未找到 NewLowercaseHeaderRoundTripper 调用")
	require.NotEqual(t, baseIdent, loweredBase,
		"lowercase 必须包在 Guard 之上，不能直接包裹原始 transport")
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
