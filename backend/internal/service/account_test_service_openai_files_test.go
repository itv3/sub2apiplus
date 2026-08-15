package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type officialFilesProbeWireRequest struct {
	method string
	path   string
	body   []byte
}

func TestAccountTestServiceOfficialFilesProbeUsesFixedMemoryPayloadAndHidesSignedURLs(t *testing.T) {
	gin.SetMode(gin.TestMode)

	var sequence atomic.Int32
	var wireMu sync.Mutex
	wires := make([]officialFilesProbeWireRequest, 0, 6)
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			http.Error(writer, "读取测试请求失败", http.StatusInternalServerError)
			return
		}
		wireMu.Lock()
		wires = append(wires, officialFilesProbeWireRequest{
			method: request.Method,
			path:   request.URL.Path,
			body:   append([]byte(nil), body...),
		})
		wireMu.Unlock()

		switch {
		case request.Method == http.MethodPost && request.URL.Path == "/backend-api/files":
			index := sequence.Add(1)
			fileID := fmt.Sprintf("file_probe_%d", index)
			uploadURL := fmt.Sprintf("https://region.oaiusercontent.com/files/%s/raw?sig=upload-secret-%d", fileID, index)
			writer.Header().Set("content-type", "application/json")
			_, _ = fmt.Fprintf(writer, `{"file_id":%q,"upload_url":%q}`, fileID, uploadURL)
		case request.Method == http.MethodPut && strings.HasPrefix(request.URL.Path, "/files/file_probe_"):
			writer.WriteHeader(http.StatusCreated)
		case request.Method == http.MethodPost && strings.HasPrefix(request.URL.Path, "/backend-api/files/file_probe_"):
			writer.Header().Set("content-type", "application/json")
			_, _ = io.WriteString(writer, `{"status":"success","download_url":"https://download.example/file?sig=download-secret","file_name":"server-result.txt","mime_type":"text/plain"}`)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	account := *officialCodexFileTestAccount()
	repo := stubOpenAIAccountRepo{accounts: []Account{account}}
	upstream := newOfficialCodexFileTestUpstream(t, server)
	gateway := &OpenAIGatewayService{accountRepo: repo, httpUpstream: upstream}
	service := &AccountTestService{
		accountRepo:          repo,
		openAIGatewayService: gateway,
	}

	for index := 0; index < 2; index++ {
		recorder := httptest.NewRecorder()
		context, _ := gin.CreateTestContext(recorder)
		context.Request = httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/901/test", bytes.NewReader(nil))

		err := service.TestAccountConnection(
			context,
			account.ID,
			"/Users/admin/private-model-path",
			"/Users/admin/private-file.txt",
			AccountTestModeOfficialFilesProbe,
		)
		require.NoError(t, err)
		require.Contains(t, recorder.Body.String(), `"type":"test_complete"`)
		require.Contains(t, recorder.Body.String(), `"success":true`)
		require.NotContains(t, recorder.Body.String(), "oaiusercontent.com")
		require.NotContains(t, recorder.Body.String(), "download.example")
		require.NotContains(t, recorder.Body.String(), "upload-secret")
		require.NotContains(t, recorder.Body.String(), "download-secret")
		require.NotContains(t, recorder.Body.String(), "file_probe_")
	}

	wireMu.Lock()
	captured := append([]officialFilesProbeWireRequest(nil), wires...)
	wireMu.Unlock()
	require.Len(t, captured, 6)

	type createPayload struct {
		FileName string `json:"file_name"`
		FileSize uint64 `json:"file_size"`
		UseCase  string `json:"use_case"`
	}
	createPayloads := make([]createPayload, 0, 2)
	blobBodies := make([][]byte, 0, 2)
	for _, wire := range captured {
		switch {
		case wire.method == http.MethodPost && wire.path == "/backend-api/files":
			var payload createPayload
			require.NoError(t, json.Unmarshal(wire.body, &payload))
			createPayloads = append(createPayloads, payload)
		case wire.method == http.MethodPut:
			blobBodies = append(blobBodies, wire.body)
		}
	}
	require.Len(t, createPayloads, 2)
	require.Len(t, blobBodies, 2)
	expectedPayload := officialFilesProbePayload(activeOpenAICodexVersionForTest())
	for _, payload := range createPayloads {
		require.True(t, strings.HasPrefix(
			payload.FileName,
			"sub2apiplus-codex-"+activeOpenAICodexVersionForTest()+"-files-probe-",
		))
		require.True(t, strings.HasSuffix(payload.FileName, ".txt"))
		require.Equal(t, uint64(len(expectedPayload)), payload.FileSize)
		require.Equal(t, "codex", payload.UseCase)
		require.NotContains(t, payload.FileName, "/Users/admin")
	}
	require.NotEqual(t, createPayloads[0].FileName, createPayloads[1].FileName)
	for _, body := range blobBodies {
		require.Equal(t, expectedPayload, string(body))
		require.NotContains(t, string(body), "/Users/admin")
	}

	calls := upstream.snapshot()
	require.Len(t, calls, 6)
	require.Equal(t, []string{
		officialCodexEndpointFilesCreate,
		officialCodexEndpointFilesBlobUpload,
		officialCodexEndpointFilesUploaded,
		officialCodexEndpointFilesCreate,
		officialCodexEndpointFilesBlobUpload,
		officialCodexEndpointFilesUploaded,
	}, []string{
		calls[0].EndpointID,
		calls[1].EndpointID,
		calls[2].EndpointID,
		calls[3].EndpointID,
		calls[4].EndpointID,
		calls[5].EndpointID,
	})
}

func TestAccountTestServiceOfficialFilesProbeRejectsNonOAuthAccount(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := Account{
		ID:       902,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"api_key": "test-key",
		},
	}
	repo := stubOpenAIAccountRepo{accounts: []Account{account}}
	service := &AccountTestService{accountRepo: repo}
	recorder := httptest.NewRecorder()
	context, _ := gin.CreateTestContext(recorder)
	context.Request = httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/902/test", bytes.NewReader(nil))

	err := service.TestAccountConnection(context, account.ID, "", "", AccountTestModeOfficialFilesProbe)
	require.ErrorContains(t, err, "只支持 OpenAI OAuth 账号")
	require.Contains(t, recorder.Body.String(), `"type":"error"`)
	require.NotContains(t, recorder.Body.String(), `"type":"test_start"`)
}
