package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// officialCodexFileControlResponseLimitBytes 只是防止异常控制面响应耗尽内存的
// 本地安全上限，不属于官方 wire 契约；所有可见协议参数均来自版本画像。
const officialCodexFileControlResponseLimitBytes int64 = 1 << 20

// OfficialCodexFileUploadInput 是一次 Codex 文件上传的生产服务输入。
// Contents 只读取一次且不会整体缓冲；调用结束前调用方必须保持其有效。
type OfficialCodexFileUploadInput struct {
	FileName      string
	FileSizeBytes uint64
	Contents      io.Reader
}

// OfficialCodexUploadedFile 对应官方 codex-api 的 UploadedOpenAiFile。
type OfficialCodexUploadedFile struct {
	FileID        string
	URI           string
	DownloadURL   string
	FileName      string
	FileSizeBytes uint64
	MIMEType      *string
}

type officialCodexFileCreateResponse struct {
	FileID    *string `json:"file_id"`
	UploadURL *string `json:"upload_url"`
}

type officialCodexFileUploadedResponse struct {
	Status       *string `json:"status"`
	DownloadURL  *string `json:"download_url"`
	FileName     *string `json:"file_name"`
	MIMEType     *string `json:"mime_type"`
	ErrorMessage *string `json:"error_message"`
}

// officialCodexFileUploadCall 固定一次三段式调用使用的账号、代理、版本快照和
// invocation ID。uploaded 的轮询只重建请求，不得重新选择账号或创建上层调用。
type officialCodexFileUploadCall struct {
	service           *OpenAIGatewayService
	profile           *officialCodexVersionProfile
	routeAccount      *Account
	credentialAccount *Account
	proxyURL          string
	invocationID      string
	ctx               context.Context
	now               func() time.Time
	sleep             func(context.Context, time.Duration) error
}

// UploadOfficialCodexFile 按 Codex CLI 0.145.0 版本画像执行 create、区域 blob
// PUT、uploaded 三步。它是独立的生产 service API，不自行暴露公网路由。
func (s *OpenAIGatewayService) UploadOfficialCodexFile(
	ctx context.Context,
	account *Account,
	input OfficialCodexFileUploadInput,
) (*OfficialCodexUploadedFile, error) {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	if err != nil {
		return nil, err
	}
	if input.FileSizeBytes > profile.Files.UploadLimitBytes {
		return nil, fmt.Errorf(
			"Codex 文件 %q 超过上传上限：%d > %d 字节",
			input.FileName,
			input.FileSizeBytes,
			profile.Files.UploadLimitBytes,
		)
	}
	if input.FileSizeBytes > 0 && input.Contents == nil {
		return nil, errors.New("Codex 文件上传内容为空")
	}

	call, err := newOfficialCodexFileUploadCall(ctx, s, account, profile)
	if err != nil {
		return nil, err
	}
	created, err := call.create(input.FileName, input.FileSizeBytes)
	if err != nil {
		return nil, err
	}
	if err := call.uploadBlob(*created.UploadURL, input.FileSizeBytes, input.Contents); err != nil {
		return nil, err
	}
	return call.finalize(*created.FileID, input.FileName, input.FileSizeBytes)
}

func newOfficialCodexFileUploadCall(
	ctx context.Context,
	service *OpenAIGatewayService,
	account *Account,
	profile *officialCodexVersionProfile,
) (*officialCodexFileUploadCall, error) {
	if service == nil || service.httpUpstream == nil {
		return nil, errors.New("Codex 文件上传 HTTP 服务不可用")
	}
	if account == nil || !account.IsOpenAIOAuth() {
		return nil, errors.New("Codex 文件上传只支持 OpenAI OAuth 账号")
	}
	if profile == nil {
		return nil, errors.New("Codex 文件上传缺少版本画像")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	credentialAccount, err := resolveCredentialAccount(ctx, service.accountRepo, account)
	if err != nil {
		return nil, fmt.Errorf("解析 Codex 文件上传凭据账号：%w", err)
	}
	if credentialAccount == nil || !credentialAccount.IsOpenAIOAuth() {
		return nil, errors.New("Codex 文件上传凭据账号不是 OpenAI OAuth")
	}
	proxyURL := ""
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	return &officialCodexFileUploadCall{
		service:           service,
		profile:           profile,
		routeAccount:      account,
		credentialAccount: credentialAccount,
		proxyURL:          proxyURL,
		invocationID:      uuid.NewString(),
		ctx:               ctx,
		now:               time.Now,
		sleep:             officialCodexFileSleep,
	}, nil
}

func (c *officialCodexFileUploadCall) create(
	fileName string,
	fileSizeBytes uint64,
) (*officialCodexFileCreateResponse, error) {
	body := map[string]any{
		"file_name": fileName,
		"file_size": fileSizeBytes,
		"use_case":  c.profile.Files.UseCase,
	}
	responseBody, err := c.executeJSON(
		codex0145EndpointID(c.profile.Files.CreateEndpointID),
		officialCodex0145EndpointURLInput{},
		body,
	)
	if err != nil {
		return nil, err
	}
	var payload officialCodexFileCreateResponse
	if err := json.Unmarshal(responseBody, &payload); err != nil {
		return nil, fmt.Errorf("解析 Codex 文件 create 响应：%w", err)
	}
	if payload.FileID == nil || payload.UploadURL == nil {
		return nil, errors.New("Codex 文件 create 响应缺少 file_id 或 upload_url")
	}
	return &payload, nil
}

func (c *officialCodexFileUploadCall) uploadBlob(
	returnedURL string,
	fileSizeBytes uint64,
	contents io.Reader,
) error {
	endpointID := codex0145EndpointID(c.profile.Files.BlobUploadEndpointID)
	endpoint, err := c.profile.ResolveEndpoint(string(endpointID))
	if err != nil {
		return err
	}
	if endpoint.Body.Encoding != "raw_bytes" {
		return fmt.Errorf("Codex 文件端点 %s 不是 raw_bytes", endpoint.ID)
	}
	target, err := officialCodex0145BuildEndpointURL(
		c.profile.Version,
		endpointID,
		officialCodex0145EndpointURLInput{ReturnedURL: returnedURL},
	)
	if err != nil {
		// 签名 query 不能进入普通错误文本。
		return errors.New("Codex 文件 blob 上传地址不符合版本画像")
	}

	requestContext, cancel := context.WithTimeout(
		c.ctx,
		time.Duration(c.profile.Files.RequestTimeoutMillis)*time.Millisecond,
	)
	defer cancel()
	var body io.Reader = contents
	if fileSizeBytes == 0 {
		body = http.NoBody
	}
	request, err := http.NewRequestWithContext(requestContext, endpoint.Method, target.String(), body)
	if err != nil {
		return fmt.Errorf("构造 Codex 文件 blob 请求：%w", err)
	}
	request = request.WithContext(WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI))
	request.Host = target.Host
	request.ContentLength = int64(fileSizeBytes)
	// 文件流不可重放；即便调用方传入 bytes.Reader，也不允许传输层据此重试 PUT。
	request.GetBody = nil
	request.Header.Set("x-ms-client-request-id", uuid.NewString())
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(
		request,
		c.routeAccount,
		endpointID,
		c.invocationID,
	)
	if err != nil {
		return fmt.Errorf("绑定 Codex 文件 blob 画像：%w", err)
	}
	if _, err := officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil); err != nil {
		return fmt.Errorf("定型 Codex 文件 blob headers：%w", err)
	}
	tlsProfile, err := officialCodex0145ResolveEndpointTLSProfileForURL(
		c.profile.Version,
		endpointID,
		request.URL,
	)
	if err != nil {
		return fmt.Errorf("编译 Codex 文件 blob TLS 画像：%w", err)
	}
	response, err := c.service.httpUpstream.DoWithTLS(
		request,
		c.proxyURL,
		c.routeAccount.ID,
		c.routeAccount.Concurrency,
		tlsProfile,
	)
	if err != nil {
		return &officialCodexFileBlobTransportError{host: target.Hostname(), cause: err}
	}
	if response == nil || response.Body == nil {
		return errors.New("Codex 文件 blob 上游返回空响应")
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return &officialCodexFileBlobStatusError{
			host:                 target.Hostname(),
			statusCode:           response.StatusCode,
			azureClientRequestID: request.Header.Get("x-ms-client-request-id"),
			azureRequestID:       response.Header.Get("x-ms-request-id"),
			azureErrorCode:       response.Header.Get("x-ms-error-code"),
		}
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64<<10))
	return nil
}

func (c *officialCodexFileUploadCall) finalize(
	fileID string,
	fallbackFileName string,
	fileSizeBytes uint64,
) (*OfficialCodexUploadedFile, error) {
	startedAt := c.now()
	for {
		responseBody, err := c.executeJSON(
			codex0145EndpointID(c.profile.Files.UploadedEndpointID),
			officialCodex0145EndpointURLInput{PathValues: map[string]string{"file_id": fileID}},
			map[string]any{},
		)
		if err != nil {
			return nil, err
		}
		var payload officialCodexFileUploadedResponse
		if err := json.Unmarshal(responseBody, &payload); err != nil {
			return nil, fmt.Errorf("解析 Codex 文件 uploaded 响应：%w", err)
		}
		if payload.Status == nil {
			return nil, errors.New("Codex 文件 uploaded 响应缺少 status")
		}
		switch *payload.Status {
		case c.profile.Files.FinalizeSuccessStatus:
			if payload.DownloadURL == nil {
				return nil, fmt.Errorf("Codex 文件 %q 完成但缺少 download_url", fileID)
			}
			fileName := fallbackFileName
			if payload.FileName != nil {
				fileName = *payload.FileName
			}
			return &OfficialCodexUploadedFile{
				FileID:        fileID,
				URI:           c.profile.Files.URIPrefix + fileID,
				DownloadURL:   *payload.DownloadURL,
				FileName:      fileName,
				FileSizeBytes: fileSizeBytes,
				MIMEType:      payload.MIMEType,
			}, nil
		case c.profile.Files.FinalizeRetryStatus:
			finalizeTimeout := time.Duration(c.profile.Files.FinalizeTimeoutMillis) * time.Millisecond
			if c.now().Sub(startedAt) >= finalizeTimeout {
				return nil, fmt.Errorf("Codex 文件 %q 在 %s 内尚未就绪", fileID, finalizeTimeout)
			}
			retryDelay := time.Duration(c.profile.Files.FinalizeRetryDelayMillis) * time.Millisecond
			if err := c.sleep(c.ctx, retryDelay); err != nil {
				return nil, fmt.Errorf("等待 Codex 文件 %q 完成：%w", fileID, err)
			}
		default:
			message := "上传定型返回错误"
			if payload.ErrorMessage != nil && strings.TrimSpace(*payload.ErrorMessage) != "" {
				message = strings.TrimSpace(*payload.ErrorMessage)
			}
			return nil, fmt.Errorf("Codex 文件 %q 上传失败：%s", fileID, message)
		}
	}
}

// executeJSON 是 create 与 uploaded 共用的版本画像执行器。URL、方法、body
// 闭集、header 闭集、TLS/H1 和请求超时均在这里一次定型。
func (c *officialCodexFileUploadCall) executeJSON(
	endpointID codex0145EndpointID,
	urlInput officialCodex0145EndpointURLInput,
	payload map[string]any,
) ([]byte, error) {
	endpoint, err := c.profile.ResolveEndpoint(string(endpointID))
	if err != nil {
		return nil, err
	}
	orderedBody, err := officialCodex0145ProjectEndpointJSONBody(
		c.profile.Version,
		endpointID,
		payload,
		nil,
		nil,
	)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件端点 %s body：%w", endpoint.ID, err)
	}
	target, err := officialCodex0145BuildEndpointURL(c.profile.Version, endpointID, urlInput)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件端点 %s URL：%w", endpoint.ID, err)
	}

	requestContext, cancel := context.WithTimeout(
		c.ctx,
		time.Duration(c.profile.Files.RequestTimeoutMillis)*time.Millisecond,
	)
	defer cancel()
	request, err := http.NewRequestWithContext(requestContext, endpoint.Method, target.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件端点 %s 请求：%w", endpoint.ID, err)
	}
	request = request.WithContext(WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI))
	request.Host = target.Host
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(
		request,
		c.routeAccount,
		endpointID,
		c.invocationID,
	)
	if err != nil {
		return nil, fmt.Errorf("绑定 Codex 文件端点 %s 画像：%w", endpoint.ID, err)
	}
	orderedBody, err = officialCodex0145FinalizeEndpointJSONBody(egressContext, orderedBody, nil)
	if err != nil {
		return nil, fmt.Errorf("定型 Codex 文件端点 %s body：%w", endpoint.ID, err)
	}
	resetOfficialEgressRequestBody(request, orderedBody)

	token, _, err := c.service.GetAccessToken(requestContext, c.credentialAccount)
	if err != nil {
		return nil, fmt.Errorf("获取 Codex 文件上传凭据：%w", err)
	}
	authHeaders, err := c.service.buildOpenAIAuthenticationHeaders(
		requestContext,
		c.credentialAccount,
		token,
	)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件上传认证头：%w", err)
	}
	for name, values := range authHeaders {
		for _, value := range values {
			request.Header.Add(name, value)
		}
	}
	setOpenAIChatGPTAccountHeaders(request.Header, c.credentialAccount)
	if _, err := officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil); err != nil {
		return nil, fmt.Errorf("定型 Codex 文件端点 %s headers：%w", endpoint.ID, err)
	}
	tlsProfile, err := officialCodex0145ResolveEndpointTLSProfileForURL(
		c.profile.Version,
		endpointID,
		request.URL,
	)
	if err != nil {
		return nil, fmt.Errorf("编译 Codex 文件端点 %s TLS 画像：%w", endpoint.ID, err)
	}
	response, err := c.service.httpUpstream.DoWithTLS(
		request,
		c.proxyURL,
		c.routeAccount.ID,
		c.routeAccount.Concurrency,
		tlsProfile,
	)
	if err != nil {
		return nil, fmt.Errorf("请求 Codex 文件端点 %s：%w", endpoint.ID, err)
	}
	return readOfficialCodexFileControlResponse(endpoint.ID, response)
}

func readOfficialCodexFileControlResponse(endpointID string, response *http.Response) ([]byte, error) {
	if response == nil || response.Body == nil {
		return nil, fmt.Errorf("Codex 文件端点 %s 返回空响应", endpointID)
	}
	defer func() { _ = response.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(response.Body, officialCodexFileControlResponseLimitBytes+1))
	if err != nil {
		return nil, fmt.Errorf("读取 Codex 文件端点 %s 响应：%w", endpointID, err)
	}
	if int64(len(body)) > officialCodexFileControlResponseLimitBytes {
		return nil, fmt.Errorf("Codex 文件端点 %s 响应超过本地安全上限", endpointID)
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		message := strings.TrimSpace(string(body))
		if len(message) > 512 {
			message = message[:512]
		}
		return nil, fmt.Errorf("Codex 文件端点 %s 返回 HTTP %d：%s", endpointID, response.StatusCode, message)
	}
	return body, nil
}

func officialCodexFileSleep(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

type officialCodexFileBlobTransportError struct {
	host  string
	cause error
}

func (e *officialCodexFileBlobTransportError) Error() string {
	return fmt.Sprintf("Codex 文件 blob 上传到 %s 失败", e.host)
}

func (e *officialCodexFileBlobTransportError) Unwrap() error {
	return e.cause
}

type officialCodexFileBlobStatusError struct {
	host                 string
	statusCode           int
	azureClientRequestID string
	azureRequestID       string
	azureErrorCode       string
}

func (e *officialCodexFileBlobStatusError) Error() string {
	return fmt.Sprintf(
		"Codex 文件 blob 上传到 %s 返回 HTTP %d（azure_client_request_id=%s, azure_request_id=%s, azure_error_code=%s）",
		e.host,
		e.statusCode,
		emptyOfficialCodexFileDiagnostic(e.azureClientRequestID),
		emptyOfficialCodexFileDiagnostic(e.azureRequestID),
		emptyOfficialCodexFileDiagnostic(e.azureErrorCode),
	)
}

func emptyOfficialCodexFileDiagnostic(value string) string {
	if strings.TrimSpace(value) == "" {
		return "missing"
	}
	return strings.TrimSpace(value)
}
