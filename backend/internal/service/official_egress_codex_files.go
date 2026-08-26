package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
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
	HostedUpload  *OfficialCodexHostedFileUploadContext
}

// OfficialCodexHostedFileUploadContext 是 hosted connector 发起文件上传时
// 由 Codex CLI 附加的三元上下文；三个字段必须同时提供且均为非空字符串。
type OfficialCodexHostedFileUploadContext struct {
	ConnectorID string
	ActionName  string
	Model       string
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
	Status        *string `json:"status"`
	DownloadURL   *string `json:"download_url"`
	FileName      *string `json:"file_name"`
	MIMEType      *string `json:"mime_type"`
	ErrorMessage  *string `json:"error_message"`
	FileSizeBytes *uint64 `json:"file_size_bytes"`
}

// officialCodexFileUploadCall 固定一次三段式调用使用的账号、代理、版本快照和
// invocation ID。uploaded 的轮询只重建请求，不得重新选择账号或创建上层调用。
type officialCodexFileUploadCall struct {
	service            *OpenAIGatewayService
	runtime            *OfficialEgressTransitionRuntime
	createInvocation   *officialCodexHTTPInvocation
	finalizeInvocation *officialCodexHTTPInvocation
	blobInvocation     *officialCodexHTTPInvocation
	profile            *officialCodexVersionProfile
	routeAccount       *Account
	credentialAccount  *Account
	proxyURL           string
	invocationID       string
	ctx                context.Context
	now                func() time.Time
	sleep              func(context.Context, time.Duration) error
}

type officialCodexFileCreateSemanticBody struct {
	UseCase          string `json:"use_case"`
	FileSize         uint64 `json:"file_size"`
	FileName         string `json:"file_name"`
	CodexConnectorID string `json:"codex_connector_id,omitempty"`
	CodexActionName  string `json:"codex_action_name,omitempty"`
	CodexModel       string `json:"codex_model,omitempty"`
}

func marshalOfficialCodexFileSemanticBody(payload any) ([]byte, error) {
	return json.Marshal(payload)
}

func newOfficialCodexBlobUploadSemanticRequest(
	ctx context.Context,
	target *url.URL,
	fileSizeBytes uint64,
	contents io.Reader,
) (*http.Request, error) {
	if target == nil || target.Hostname() == "" {
		return nil, errors.New("Codex blob 上传 target 非法")
	}
	body := contents
	if fileSizeBytes == 0 {
		body = http.NoBody
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, target.String(), body)
	if err != nil {
		return nil, err
	}
	request = request.WithContext(WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI))
	request.Host = target.Host
	request.ContentLength = int64(fileSizeBytes)
	// raw stream 是单次能力；即使来源可重放，也不得向 transport 暴露 GetBody。
	request.GetBody = nil
	return request, nil
}

// UploadOfficialCodexFile 按当前 Codex CLI Release 画像执行 create、区域 blob
// PUT、uploaded 三步。它是独立的生产 service API，不自行暴露公网路由。
func (s *OpenAIGatewayService) UploadOfficialCodexFile(
	ctx context.Context,
	account *Account,
	input OfficialCodexFileUploadInput,
) (*OfficialCodexUploadedFile, error) {
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return nil, err
	}
	profile, err := resolveCodexVersionProfileForMode(string(runtimeState.CodexReleaseMode))
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
	if input.HostedUpload != nil && (strings.TrimSpace(input.HostedUpload.ConnectorID) == "" ||
		strings.TrimSpace(input.HostedUpload.ActionName) == "" ||
		strings.TrimSpace(input.HostedUpload.Model) == "") {
		return nil, errors.New("Codex hosted 文件上传上下文三个字段必须同时为非空字符串")
	}

	call, err := newOfficialCodexFileUploadCall(ctx, s, runtimeState, account, profile)
	if err != nil {
		return nil, err
	}
	created, err := call.create(input.FileName, input.FileSizeBytes, input.HostedUpload)
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
	runtimeState *OfficialEgressTransitionRuntime,
	account *Account,
	profile *officialCodexVersionProfile,
) (*officialCodexFileUploadCall, error) {
	if service == nil || service.httpUpstream == nil || runtimeState == nil {
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
		runtime:           runtimeState,
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
	hostedUpload *OfficialCodexHostedFileUploadContext,
) (*officialCodexFileCreateResponse, error) {
	// 结构体顺序是业务构造顺序，并刻意不等同于 Profile wire 顺序；最终
	// file_name/file_size/use_case 线序只能由 officialegress compiler 决定。
	body := officialCodexFileCreateSemanticBody{
		UseCase: c.profile.Files.UseCase, FileSize: fileSizeBytes, FileName: fileName,
	}
	if hostedUpload != nil {
		body.CodexConnectorID = hostedUpload.ConnectorID
		body.CodexActionName = hostedUpload.ActionName
		body.CodexModel = hostedUpload.Model
	}
	responseBody, err := c.executeJSON(
		codexEndpointID(c.profile.Files.CreateEndpointID),
		officialCodexEndpointURLInput{},
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
	endpointID := codexEndpointID(c.profile.Files.BlobUploadEndpointID)
	endpoint, err := c.profile.ResolveEndpoint(string(endpointID))
	if err != nil {
		return err
	}
	if endpoint.Body.Encoding != "raw_bytes" {
		return fmt.Errorf("Codex 文件端点 %s 不是 raw_bytes", endpoint.ID)
	}
	target, err := officialCodexBuildEndpointURL(
		c.profile.Version,
		endpointID,
		officialCodexEndpointURLInput{ReturnedURL: returnedURL},
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
	requestContext, err = bindOfficialEgressSink(requestContext, officialEgressSinkFilesBlobUpload)
	if err != nil {
		return fmt.Errorf("绑定 Codex 文件 blob SinkID：%w", err)
	}
	request, err := newOfficialCodexBlobUploadSemanticRequest(
		requestContext, target, fileSizeBytes, contents,
	)
	if err != nil {
		return fmt.Errorf("构造 Codex 文件 blob 请求：%w", err)
	}
	if c.blobInvocation == nil {
		c.blobInvocation, err = newOfficialCodexHTTPInvocation(
			requestContext,
			officialCodexHTTPInvocationInput{
				Runtime: c.runtime, Account: c.routeAccount,
				SinkID:       officialegress.SinkCodexFilesBlobUpload,
				InvocationID: c.invocationID, ProxyURL: c.proxyURL,
				PolicyID:      "changeset3.files.blob_upload",
				PolicySource:  "service.UploadOfficialCodexFile",
				AttemptBudget: 1, SingleUseBody: true,
			},
		)
		if err != nil {
			return &officialCodexFileBlobTransportError{host: target.Hostname(), cause: err}
		}
	}
	response, err := c.blobInvocation.Execute(
		requestContext,
		officialCodexHTTPAttemptInput{
			EndpointID: string(endpointID), Request: request,
			DynamicInputs: officialegress.EndpointDynamicInputs{ReturnedURL: target},
		},
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
			codexEndpointID(c.profile.Files.UploadedEndpointID),
			officialCodexEndpointURLInput{PathValues: map[string]string{"file_id": fileID}},
			struct{}{},
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
			resolvedFileSizeBytes := fileSizeBytes
			if payload.FileSizeBytes != nil {
				resolvedFileSizeBytes = *payload.FileSizeBytes
			}
			return &OfficialCodexUploadedFile{
				FileID:        fileID,
				URI:           c.profile.Files.URIPrefix + fileID,
				DownloadURL:   *payload.DownloadURL,
				FileName:      fileName,
				FileSizeBytes: resolvedFileSizeBytes,
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
	endpointID codexEndpointID,
	urlInput officialCodexEndpointURLInput,
	payload any,
) ([]byte, error) {
	endpoint, err := c.profile.ResolveEndpoint(string(endpointID))
	if err != nil {
		return nil, err
	}
	semanticBody, err := marshalOfficialCodexFileSemanticBody(payload)
	if err != nil {
		return nil, fmt.Errorf("编码 Codex 文件端点 %s 业务 body：%w", endpoint.ID, err)
	}
	target, err := officialCodexBuildEndpointURL(c.profile.Version, endpointID, urlInput)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件端点 %s URL：%w", endpoint.ID, err)
	}

	requestContext, cancel := context.WithTimeout(
		c.ctx,
		time.Duration(c.profile.Files.RequestTimeoutMillis)*time.Millisecond,
	)
	defer cancel()
	requestContext, err = bindOfficialEgressSink(requestContext, officialEgressSinkFilesRegister)
	if err != nil {
		return nil, fmt.Errorf("绑定 Codex 文件登记 SinkID：%w", err)
	}
	request, err := http.NewRequestWithContext(requestContext, endpoint.Method, target.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 文件端点 %s 请求：%w", endpoint.ID, err)
	}
	request = request.WithContext(WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI))
	request.Host = target.Host
	resetOfficialEgressRequestBody(request, semanticBody)

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
	// files_create 的 hosted_file_upload 条件属于该 attempt 的 Persona
	// attestation，而 files_uploaded 的空 Body 不具备该条件。二者必须各自
	// 冻结 invocation；uploaded 的多次轮询仍复用同一个 finalize invocation。
	var invocation **officialCodexHTTPInvocation
	switch endpointID {
	case codexEndpointID(c.profile.Files.CreateEndpointID):
		invocation = &c.createInvocation
	case codexEndpointID(c.profile.Files.UploadedEndpointID):
		invocation = &c.finalizeInvocation
	default:
		return nil, fmt.Errorf("Codex 文件登记端点不受支持：%s", endpoint.ID)
	}
	if *invocation == nil {
		*invocation, err = newOfficialCodexHTTPInvocation(
			requestContext,
			officialCodexHTTPInvocationInput{
				Runtime: c.runtime, Account: c.routeAccount,
				SinkID:       officialegress.SinkCodexFilesRegister,
				InvocationID: c.invocationID, ProxyURL: c.proxyURL,
				PolicyID:      "changeset3.files.register",
				PolicySource:  "service.UploadOfficialCodexFile",
				AttemptBudget: c.registerAttemptBudget(),
			},
		)
		if err != nil {
			return nil, fmt.Errorf("创建 Codex 文件 register invocation：%w", err)
		}
	}
	response, err := (*invocation).Execute(
		requestContext,
		officialCodexHTTPAttemptInput{EndpointID: string(endpointID), Request: request},
	)
	if err != nil {
		return nil, fmt.Errorf("请求 Codex 文件端点 %s：%w", endpoint.ID, err)
	}
	return readOfficialCodexFileControlResponse(endpoint.ID, response)
}

// registerAttemptBudget 覆盖 create、uploaded 首次检查及整个画像轮询窗口。
func (c *officialCodexFileUploadCall) registerAttemptBudget() int {
	if c == nil || c.profile == nil || c.profile.Files.FinalizeRetryDelayMillis <= 0 ||
		c.profile.Files.FinalizeTimeoutMillis <= 0 {
		return 2
	}
	polls := c.profile.Files.FinalizeTimeoutMillis / c.profile.Files.FinalizeRetryDelayMillis
	return int(polls) + 2
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
