package service

import (
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

type officialCodexFileTestRequest struct {
	Method         string
	URL            string
	Host           string
	Header         http.Header
	ContentLength  int64
	EndpointID     string
	InvocationID   string
	ConnectionPool string
	HeaderOrder    []string
	DeadlineRemain time.Duration
	ProxyURL       string
	AccountID      int64
	Concurrency    int
}

type officialCodexFileTestUpstream struct {
	client *http.Client
	mu     sync.Mutex
	calls  []officialCodexFileTestRequest
}

func newOfficialCodexFileTestUpstream(t *testing.T, server *httptest.Server) *officialCodexFileTestUpstream {
	t.Helper()
	baseTransport, ok := server.Client().Transport.(*http.Transport)
	require.True(t, ok)
	transport := baseTransport.Clone()
	transport.DisableCompression = true
	transport.ForceAttemptHTTP2 = false
	transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // 仅测试本地 TLS server。
	serverAddress := server.Listener.Addr().String()
	transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, serverAddress)
	}
	t.Cleanup(transport.CloseIdleConnections)
	return &officialCodexFileTestUpstream{client: &http.Client{Transport: transport}}
}

func (u *officialCodexFileTestUpstream) Do(
	request *http.Request,
	proxyURL string,
	accountID int64,
	accountConcurrency int,
) (*http.Response, error) {
	return u.DoWithTLS(request, proxyURL, accountID, accountConcurrency, nil)
}

func (u *officialCodexFileTestUpstream) DoWithTLS(
	request *http.Request,
	proxyURL string,
	accountID int64,
	accountConcurrency int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	record := officialCodexFileTestRequest{
		Method:         request.Method,
		URL:            request.URL.String(),
		Host:           request.Host,
		Header:         request.Header.Clone(),
		ContentLength:  request.ContentLength,
		ProxyURL:       proxyURL,
		AccountID:      accountID,
		Concurrency:    accountConcurrency,
		DeadlineRemain: -1,
	}
	if deadline, ok := request.Context().Deadline(); ok {
		record.DeadlineRemain = time.Until(deadline)
	}
	if egressContext, ok := OfficialEgressContextFromContext(request.Context()); ok {
		record.EndpointID = egressContext.CodexEndpointProfileID()
		record.InvocationID = egressContext.InvocationID()
		record.ConnectionPool = egressContext.ConnectionPoolID()
	}
	if identity, ok := officialegress.AttemptIdentityFromContext(request.Context()); ok {
		record.EndpointID = identity.EndpointID
		record.InvocationID = identity.InvocationID
		record.ConnectionPool = identity.ConnectionPoolDigest
	}
	if profile != nil {
		for _, rule := range profile.Transport.H1HeaderOrders {
			if rule.Method == request.Method && rule.Path == request.URL.EscapedPath() {
				record.HeaderOrder = append([]string(nil), rule.Order...)
				break
			}
		}
	}
	u.mu.Lock()
	u.calls = append(u.calls, record)
	u.mu.Unlock()
	return u.client.Do(request)
}

func (u *officialCodexFileTestUpstream) snapshot() []officialCodexFileTestRequest {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]officialCodexFileTestRequest(nil), u.calls...)
}

type officialCodexFileWireRequest struct {
	Method        string
	RequestURI    string
	Host          string
	Header        http.Header
	ContentLength int64
	Body          []byte
}

func TestUploadOfficialCodexFileExecutesProfileDrivenThreeStepFlow(t *testing.T) {
	var wireMu sync.Mutex
	wires := make([]officialCodexFileWireRequest, 0, 4)
	var finalizeAttempts atomic.Int32
	const returnedUploadURL = "https://sdmntprwestus3.oaiusercontent.com/files/blob-1/raw?se=2026-07-30T12%3A00%3A00Z&sp=w&sig=a%2Fb"

	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, readErr := io.ReadAll(request.Body)
		if readErr != nil {
			http.Error(writer, readErr.Error(), http.StatusInternalServerError)
			return
		}
		wireMu.Lock()
		wires = append(wires, officialCodexFileWireRequest{
			Method:        request.Method,
			RequestURI:    request.RequestURI,
			Host:          request.Host,
			Header:        request.Header.Clone(),
			ContentLength: request.ContentLength,
			Body:          body,
		})
		wireMu.Unlock()

		switch {
		case request.Method == http.MethodPost && request.URL.Path == "/backend-api/files":
			writer.Header().Set("content-type", "application/json")
			_, _ = fmt.Fprintf(writer, `{"file_id":"file_123","upload_url":%q}`, returnedUploadURL)
		case request.Method == http.MethodPut && request.URL.Path == "/files/blob-1/raw":
			writer.Header().Set("x-ms-request-id", "azure-request-1")
			writer.WriteHeader(http.StatusCreated)
		case request.Method == http.MethodPost && request.URL.Path == "/backend-api/files/file_123/uploaded":
			writer.Header().Set("content-type", "application/json")
			if finalizeAttempts.Add(1) == 1 {
				_, _ = io.WriteString(writer, `{"status":"retry"}`)
				return
			}
			_, _ = io.WriteString(writer, `{"status":"success","download_url":"https://download.example/file_123","file_name":"hello.txt","mime_type":"text/plain","file_size_bytes":7}`)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	upstream := newOfficialCodexFileTestUpstream(t, server)
	service := &OpenAIGatewayService{httpUpstream: upstream}
	account := officialCodexFileTestAccount()
	startedAt := time.Now()
	uploaded, err := service.UploadOfficialCodexFile(
		context.Background(),
		account,
		OfficialCodexFileUploadInput{
			FileName:      "hello.txt",
			FileSizeBytes: 5,
			Contents:      bytes.NewReader([]byte("hello")),
		},
	)
	require.NoError(t, err)
	require.GreaterOrEqual(t, time.Since(startedAt), 200*time.Millisecond)
	require.Equal(t, &OfficialCodexUploadedFile{
		FileID:        "file_123",
		URI:           "sediment://file_123",
		DownloadURL:   "https://download.example/file_123",
		FileName:      "hello.txt",
		FileSizeBytes: 7,
		MIMEType:      officialCodexFileStringPointer("text/plain"),
	}, uploaded)

	calls := upstream.snapshot()
	require.Len(t, calls, 4)
	require.Equal(t, []string{
		officialCodexEndpointFilesCreate,
		officialCodexEndpointFilesBlobUpload,
		officialCodexEndpointFilesUploaded,
		officialCodexEndpointFilesUploaded,
	}, []string{calls[0].EndpointID, calls[1].EndpointID, calls[2].EndpointID, calls[3].EndpointID})
	for _, call := range calls {
		require.NotEmpty(t, call.InvocationID)
		require.Equal(t, calls[0].InvocationID, call.InvocationID)
		require.Equal(t, account.ID, call.AccountID)
		require.Equal(t, account.Concurrency, call.Concurrency)
		require.Empty(t, call.ProxyURL)
		require.Greater(t, call.DeadlineRemain, 55*time.Second)
		require.LessOrEqual(t, call.DeadlineRemain, 60*time.Second)
	}
	require.NotEqual(t, calls[0].ConnectionPool, calls[1].ConnectionPool)
	require.Equal(t, calls[2].ConnectionPool, calls[3].ConnectionPool)
	require.Equal(t, []string{
		"authorization", "chatgpt-account-id", "x-openai-fedramp", "content-type", "accept", "host", "content-length",
	}, calls[0].HeaderOrder)
	require.Equal(t, []string{
		"x-ms-blob-type", "x-ms-client-request-id", "content-length", "accept", "host",
	}, calls[1].HeaderOrder)
	require.Equal(t, calls[0].HeaderOrder, calls[2].HeaderOrder)

	require.Equal(t, []string{"accept", "authorization", "chatgpt-account-id", "content-type"}, officialCodexFileHeaderNames(calls[0].Header))
	require.Equal(t, []string{"accept", "x-ms-blob-type", "x-ms-client-request-id"}, officialCodexFileHeaderNames(calls[1].Header))
	require.Equal(t, calls[0].Header, calls[2].Header)
	require.Equal(t, "Bearer token-1", calls[0].Header.Get("authorization"))
	require.Equal(t, "account-1", calls[0].Header.Get("chatgpt-account-id"))
	require.Empty(t, calls[0].Header.Get("version"))
	require.Empty(t, calls[0].Header.Get("originator"))
	require.Empty(t, calls[0].Header.Get("user-agent"))
	require.Empty(t, calls[0].Header.Get("cookie"))
	require.Empty(t, calls[1].Header.Get("authorization"))
	require.Equal(t, "BlockBlob", calls[1].Header.Get("x-ms-blob-type"))
	require.Equal(t, "*/*", calls[1].Header.Get("accept"))
	_, err = uuid.Parse(calls[1].Header.Get("x-ms-client-request-id"))
	require.NoError(t, err)
	require.Equal(t, returnedUploadURL, calls[1].URL)

	wireMu.Lock()
	actualWires := append([]officialCodexFileWireRequest(nil), wires...)
	wireMu.Unlock()
	require.Len(t, actualWires, 4)
	require.Equal(t, `{"file_name":"hello.txt","file_size":5,"use_case":"codex"}`, string(actualWires[0].Body))
	require.Equal(t, int64(len(actualWires[0].Body)), actualWires[0].ContentLength)
	require.Equal(t, "chatgpt.com", actualWires[0].Host)
	require.Equal(t, returnedUploadURL[strings.Index(returnedUploadURL, "/files/"):], actualWires[1].RequestURI)
	require.Equal(t, "sdmntprwestus3.oaiusercontent.com", actualWires[1].Host)
	require.Equal(t, []byte("hello"), actualWires[1].Body)
	require.Equal(t, int64(5), actualWires[1].ContentLength)
	require.Equal(t, []byte("{}"), actualWires[2].Body)
	require.Equal(t, []byte("{}"), actualWires[3].Body)
	require.Equal(t, int64(2), actualWires[2].ContentLength)
	require.Equal(t, int32(2), finalizeAttempts.Load())
	createUploadURLSHA256 := candidateTraceSHA256(returnedUploadURL)
	putURLSHA256 := candidateTraceSHA256(calls[1].URL)
	require.Equal(t, createUploadURLSHA256, putURLSHA256)
	candidateTraceLogFact(t, "a14.file-upload-url-chain", "A14", "file_upload_chain", map[string]any{
		"create_upload_url_sha256": createUploadURLSHA256,
		"put_url_sha256":           putURLSHA256,
	})
}

func TestUploadOfficialCodexHostedFileIncludesContextAndFallsBackToRequestSize(t *testing.T) {
	var wireMu sync.Mutex
	wires := make([]officialCodexFileWireRequest, 0, 3)
	const returnedUploadURL = "https://region.oaiusercontent.com/files/file_hosted/raw?sig=hosted"

	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, readErr := io.ReadAll(request.Body)
		if readErr != nil {
			http.Error(writer, readErr.Error(), http.StatusInternalServerError)
			return
		}
		wireMu.Lock()
		wires = append(wires, officialCodexFileWireRequest{
			Method: request.Method, RequestURI: request.RequestURI,
			Host: request.Host, Header: request.Header.Clone(),
			ContentLength: request.ContentLength, Body: body,
		})
		wireMu.Unlock()

		switch {
		case request.Method == http.MethodPost && request.URL.Path == "/backend-api/files":
			writer.Header().Set("content-type", "application/json")
			_, _ = fmt.Fprintf(writer, `{"file_id":"file_hosted","upload_url":%q}`, returnedUploadURL)
		case request.Method == http.MethodPut && request.URL.Path == "/files/file_hosted/raw":
			writer.WriteHeader(http.StatusCreated)
		case request.Method == http.MethodPost && request.URL.Path == "/backend-api/files/file_hosted/uploaded":
			writer.Header().Set("content-type", "application/json")
			_, _ = io.WriteString(writer, `{"status":"success","download_url":"https://download.example/file_hosted","file_name":"report.pdf","mime_type":"application/pdf"}`)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	upstream := newOfficialCodexFileTestUpstream(t, server)
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		officialegress.DefaultGuard(),
		upstream,
		officialegress.ExecutorID(t.Name()),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	service := &OpenAIGatewayService{httpUpstream: upstream, officialEgress: runtimeState}
	uploaded, err := service.UploadOfficialCodexFile(
		context.Background(),
		officialCodexFileTestAccount(),
		OfficialCodexFileUploadInput{
			FileName: "report.pdf", FileSizeBytes: 8,
			Contents: bytes.NewReader([]byte("%PDF-1.4")),
			HostedUpload: &OfficialCodexHostedFileUploadContext{
				ConnectorID: "library", ActionName: "create_library_file", Model: "gpt-work",
			},
		},
	)
	require.NoError(t, err)
	require.Equal(t, uint64(8), uploaded.FileSizeBytes, "旧服务缺少 file_size_bytes 时必须回退请求大小")

	wireMu.Lock()
	actualWires := append([]officialCodexFileWireRequest(nil), wires...)
	wireMu.Unlock()
	require.Len(t, actualWires, 3)
	require.Equal(
		t,
		`{"file_name":"report.pdf","file_size":8,"use_case":"codex","codex_connector_id":"library","codex_action_name":"create_library_file","codex_model":"gpt-work"}`,
		string(actualWires[0].Body),
	)
}

func TestUploadOfficialCodexHostedFileRejectsIncompleteContextBeforeNetwork(t *testing.T) {
	testCases := []OfficialCodexHostedFileUploadContext{
		{ActionName: "action", Model: "model"},
		{ConnectorID: "connector", Model: "model"},
		{ConnectorID: "connector", ActionName: "action"},
		{ConnectorID: " ", ActionName: "action", Model: "model"},
	}
	for _, hostedUpload := range testCases {
		upstream := &officialCodexFileTestUpstream{}
		service := &OpenAIGatewayService{httpUpstream: upstream}
		_, err := service.UploadOfficialCodexFile(
			context.Background(),
			officialCodexFileTestAccount(),
			OfficialCodexFileUploadInput{HostedUpload: &hostedUpload},
		)
		require.ErrorContains(t, err, "三个字段必须同时为非空字符串")
		require.Empty(t, upstream.snapshot())
	}
}

func TestUploadOfficialCodexFileRejectsOversizeBeforeNetwork(t *testing.T) {
	upstream := &officialCodexFileTestUpstream{}
	service := &OpenAIGatewayService{httpUpstream: upstream}
	profile, err := resolveCodexVersionProfileForMode(officialClientProfileModeActive)
	require.NoError(t, err)
	_, err = service.UploadOfficialCodexFile(
		context.Background(),
		officialCodexFileTestAccount(),
		OfficialCodexFileUploadInput{
			FileName:      "too-large.bin",
			FileSizeBytes: profile.Files.UploadLimitBytes + 1,
		},
	)
	require.ErrorContains(t, err, "超过上传上限")
	require.Empty(t, upstream.snapshot())
}

func TestUploadOfficialCodexFileRejectsReturnedNonOAIHostWithoutLeakingSignature(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = io.WriteString(writer, `{"file_id":"file_123","upload_url":"https://evil.example/upload?sig=top-secret"}`)
	}))
	defer server.Close()
	upstream := newOfficialCodexFileTestUpstream(t, server)
	service := &OpenAIGatewayService{httpUpstream: upstream}
	_, err := service.UploadOfficialCodexFile(
		context.Background(),
		officialCodexFileTestAccount(),
		OfficialCodexFileUploadInput{
			FileName:      "hello.txt",
			FileSizeBytes: 5,
			Contents:      bytes.NewReader([]byte("hello")),
		},
	)
	require.ErrorContains(t, err, "不符合版本画像")
	require.NotContains(t, err.Error(), "top-secret")
	require.Len(t, upstream.snapshot(), 1)
}

func TestUploadOfficialCodexFileStopsAfterBlobFailureAndRedactsSignedURL(t *testing.T) {
	const returnedUploadURL = "https://region.oaiusercontent.com/upload/file_123?sig=top-secret"
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.Method {
		case http.MethodPost:
			writer.Header().Set("content-type", "application/json")
			_, _ = fmt.Fprintf(writer, `{"file_id":"file_123","upload_url":%q}`, returnedUploadURL)
		case http.MethodPut:
			writer.Header().Set("x-ms-request-id", "azure-request")
			writer.Header().Set("x-ms-error-code", "ServerBusy")
			writer.WriteHeader(http.StatusServiceUnavailable)
			_, _ = io.WriteString(writer, "response body must not leak")
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()
	upstream := newOfficialCodexFileTestUpstream(t, server)
	service := &OpenAIGatewayService{httpUpstream: upstream}
	_, err := service.UploadOfficialCodexFile(
		context.Background(),
		officialCodexFileTestAccount(),
		OfficialCodexFileUploadInput{
			FileName:      "hello.txt",
			FileSizeBytes: 5,
			Contents:      bytes.NewReader([]byte("hello")),
		},
	)
	require.ErrorContains(t, err, "HTTP 503")
	require.ErrorContains(t, err, "azure_request_id=azure-request")
	require.ErrorContains(t, err, "azure_error_code=ServerBusy")
	require.NotContains(t, err.Error(), "top-secret")
	require.NotContains(t, err.Error(), "response body must not leak")
	calls := upstream.snapshot()
	require.Len(t, calls, 2)
	require.Equal(t, officialCodexEndpointFilesBlobUpload, calls[1].EndpointID)
}

func TestOfficialCodexFileFinalizeStopsAtProfileTimeout(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("content-type", "application/json")
		_, _ = io.WriteString(writer, `{"status":"retry"}`)
	}))
	defer server.Close()
	upstream := newOfficialCodexFileTestUpstream(t, server)
	service := &OpenAIGatewayService{httpUpstream: upstream}
	profile, err := resolveCodexVersionProfileForMode(officialClientProfileModeActive)
	require.NoError(t, err)
	runtimeState, err := resolveOfficialEgressRuntime(nil, upstream)
	require.NoError(t, err)
	call, err := newOfficialCodexFileUploadCall(
		context.Background(),
		service,
		runtimeState,
		officialCodexFileTestAccount(),
		profile,
	)
	require.NoError(t, err)
	startedAt := time.Unix(1_700_000_000, 0)
	var nowCalls atomic.Int32
	call.now = func() time.Time {
		if nowCalls.Add(1) == 1 {
			return startedAt
		}
		return startedAt.Add(time.Duration(profile.Files.FinalizeTimeoutMillis) * time.Millisecond)
	}
	call.sleep = func(context.Context, time.Duration) error {
		t.Fatal("达到画像总超时后不应继续 sleep")
		return nil
	}
	_, err = call.finalize("file_123", "hello.txt", 5)
	require.ErrorContains(t, err, "30s 内尚未就绪")
	require.Len(t, upstream.snapshot(), 1)
}

func officialCodexFileTestAccount() *Account {
	return &Account{
		ID:          901,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeOAuth,
		Concurrency: 3,
		Credentials: map[string]any{
			"access_token":       "token-1",
			"chatgpt_account_id": "account-1",
		},
		Extra: map[string]any{},
	}
}

func officialCodexFileHeaderNames(headers http.Header) []string {
	names := make([]string, 0, len(headers))
	for name, values := range headers {
		if len(values) == 1 && values[0] == "" {
			continue
		}
		names = append(names, strings.ToLower(name))
	}
	sort.Strings(names)
	return names
}

func officialCodexFileStringPointer(value string) *string {
	return &value
}
