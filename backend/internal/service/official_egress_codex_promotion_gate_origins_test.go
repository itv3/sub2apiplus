package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// ============================================================================
// SPEC-EP-002 的晋升后门禁：业务、刷新、realtime 与文件上传使用各自域名来源。
//
// SNI 在网关里没有单独字段：三类后端（HTTPUpstream、ReqProfile、WS）都以 Compiler 冻结
// 的最终 URL 主机名拨号，uTLS 以之作为 ServerName。本地终端在握手时记录 ClientHello
// 的 ServerName，并按连接号与该连接上的请求对应，因此“某个请求用了哪个 SNI”是真实观测。
// ============================================================================

// codexGateRegionalUploadHost 是 files create 返回的区域上传主机。
const codexGateRegionalUploadHost = "sdmntprwestus3.oaiusercontent.com"

// codexGateFileFlow 描述一次文件上传在终端上的预期应答：是否预约 C2PA、finalize 是否
// 先返回 retry。
type codexGateFileFlow struct {
	fileID      string
	reservation bool
	retryFirst  bool
}

// codexGateFileResponder 按 create 请求的文件名分派三跳应答：create 返回区域上传 URL
// （可带 C2PA 预约），PUT 返回 201，uploaded 按需先返回一次 retry。
type codexGateFileResponder struct {
	mu        sync.Mutex
	flows     map[string]codexGateFileFlow
	finalized map[string]int
}

func (r *codexGateFileResponder) uploadURL(fileID string) string {
	return "https://" + codexGateRegionalUploadHost + "/files/" + fileID +
		"/raw?se=2026-07-30T12%3A00%3A00Z&sp=w&sig=a%2Fb"
}

func (r *codexGateFileResponder) respond(request codexGateWireRequest) codexGateWireResponse {
	r.mu.Lock()
	defer r.mu.Unlock()
	switch {
	case request.method == http.MethodPost && request.path == "/backend-api/files":
		var create struct {
			FileName string `json:"file_name"`
		}
		_ = json.Unmarshal(request.body, &create)
		flow := r.flows[create.FileName]
		return codexGateJSONResponse(fmt.Sprintf(`{"file_id":%q,"upload_url":%q,"pdf_c2pa_reservation":%t}`,
			flow.fileID, r.uploadURL(flow.fileID), flow.reservation))
	case request.method == http.MethodPut && strings.HasPrefix(request.path, "/files/"):
		return codexGateWireResponse{status: http.StatusCreated}
	case request.method == http.MethodPost && strings.HasSuffix(request.path, "/uploaded"):
		fileID := strings.TrimSuffix(strings.TrimPrefix(request.path, "/backend-api/files/"), "/uploaded")
		r.finalized[fileID]++
		for _, flow := range r.flows {
			if flow.fileID == fileID && flow.retryFirst && r.finalized[fileID] == 1 {
				return codexGateJSONResponse(`{"status":"retry"}`)
			}
		}
		return codexGateJSONResponse(`{"status":"success","download_url":"https://download.example/` + fileID + `"}`)
	default:
		return codexGateRealtimeResponse(request)
	}
}

// codexGateHelloFor 返回承载指定请求的连接上的 ClientHello。
func codexGateHelloFor(t *testing.T, server *codexGateWireServer, request codexGateWireRequest) codexGateClientHello {
	t.Helper()
	for _, hello := range server.clientHellos() {
		if hello.connection == request.connection {
			return hello
		}
	}
	t.Fatalf("终端没有记录连接 %d 的 ClientHello", request.connection)
	return codexGateClientHello{}
}

// SPEC-EP-002（condition_change）：业务、刷新、realtime 与文件上传使用各自域名来源。
//
// 检查项与网关观测：
//   - chatgpt-sni：常规业务（Responses）的 ClientHello ServerName 为 chatgpt.com；
//   - auth-sni：OAuth refresh（ReqProfile 后端）的 ServerName 为 auth.openai.com；
//   - api-sni：realtime sideband（生产 dialLiveSideband，WS 后端）的 ServerName 为
//     api.openai.com，第一跳仍是 chatgpt.com；
//   - regional-file-sni：文件 PUT 的 ServerName 是 create 返回的区域 *.oaiusercontent.com 主机；
//   - file-url-chain：实际 PUT 的 URL 与 create 返回的 upload_url 逐字相同（摘要一致）；
//   - file-c2pa-negative-body：未预约 C2PA 时 uploaded 请求体为空对象 {}；
//   - file-c2pa-positive-body：预约 C2PA 时 uploaded 请求体只嵌入本次 create 的请求体；
//   - file-c2pa-retry-body：uploaded 返回 retry 时，同一 finalize invocation 以序号 2、原因
//     retry 重发逐字节相同的请求体（Bundle 与连接池摘要不变）。
//
// 条件变化来自目标画像新增的 WorkspaceRouting 节：默认 origin 与 Responses 两端点主机
// 一致；发现结果为默认（或尚未发现）时 Responses 照常发往 chatgpt.com，判定为非默认时
// 受路由端点（HTTP 与 WS）在建立任何连接前失败关闭，不受路由的文件上传照常进行。
func TestCodexEndpointHostOriginsReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "画像声明 WorkspaceRouting 节",
		func(profile profilecontract.ExecutableProfile) bool {
			return profile.Optional().WorkspaceRouting != nil
		})
	profile := codexGateExecutableProfile(t, mode)
	section := profile.Optional().WorkspaceRouting
	require.NotNil(t, section, "目标画像必须声明 WorkspaceRouting 节")
	defaultOrigin, err := url.Parse(section.DefaultOrigin)
	require.NoError(t, err)
	require.Equal(t, "chatgpt.com", defaultOrigin.Host)
	for _, endpointID := range section.RoutedEndpointIDs {
		require.Equal(t, defaultOrigin.Host, codexGateMustEndpoint(t, profile, endpointID).Host,
			"受路由端点 %s 的主机必须等于默认 origin", endpointID)
	}
	require.Equal(t, []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS}, section.RoutedEndpointIDs)
	require.Equal(t, defaultOrigin.Host, codexGateMustEndpoint(t, profile, section.DiscoveryEndpointID).Host)
	require.Equal(t, "auth.openai.com", codexGateMustEndpoint(t, profile, officialCodexEndpointOAuthRefresh).Host)
	require.Equal(t, "api.openai.com", codexGateMustEndpoint(t, profile, officialCodexEndpointRealtimeSideband).Host)
	blob := codexGateMustEndpoint(t, profile, officialCodexEndpointFilesBlobUpload)
	require.Equal(t, "*.oaiusercontent.com", blob.Host)
	require.True(t, blob.HostFromResponse, "文件 PUT 的主机必须取自服务端响应")
	require.NoError(t, officialCodexWorkspaceRoutingGate(
		codexGateOtherReleaseMode(mode), newOfficialOpenAIHTTPTestAccount(codexGateForwardAccountID),
		officialCodexEndpointResponsesHTTP,
	), "对照：另一槽位没有 WorkspaceRouting 节，闸门恒放行")
	resetOfficialCodexWorkspaceRoutingResults(t)

	files := &codexGateFileResponder{
		flows: map[string]codexGateFileFlow{
			"notes.txt":   {fileID: "file_gate_notes"},
			"report.pdf":  {fileID: "file_gate_report", reservation: true, retryFirst: true},
			"summary.pdf": {fileID: "file_gate_summary", reservation: true},
		},
		finalized: map[string]int{},
	}
	server := startCodexGateWireServer(t, files.respond)
	gate := newCodexGateService(t, mode, server)

	// chatgpt-sni：尚未发现工作区路由，Responses 放行。
	body, ingress := codexGateRootIngress(t, nil)
	_, err = gate.forward(ingress, body)
	require.NoError(t, err)
	responses := server.httpRequestsForPath(codexGateResponsesPath)
	require.Len(t, responses, 1)
	require.Equal(t, "chatgpt.com", codexGateHelloFor(t, server, responses[0]).serverName)

	// auth-sni。
	require.NoError(t, gate.oauthRefresh())
	refresh := server.requestsForPath("/oauth/token")
	require.Len(t, refresh, 1)
	require.Equal(t, "auth.openai.com", codexGateHelloFor(t, server, refresh[0]).serverName)

	// api-sni：realtime 第一跳 chatgpt.com，sideband api.openai.com。
	created, record, err := gate.realtimeCall(t, mode)
	require.NoError(t, err)
	require.Equal(t, codexGateRealtimeCallID, created.CallID)
	calls := server.requestsForPath(codexGateRealtimeCallsPath)
	require.Len(t, calls, 1)
	require.Equal(t, "chatgpt.com", codexGateHelloFor(t, server, calls[0]).serverName)
	require.NoError(t, gate.realtimeSideband(t, server, record))
	sideband := server.requestsForPath("/v1/realtime")
	require.Len(t, sideband, 1)
	require.True(t, sideband[0].webSocket)
	require.Equal(t, "api.openai.com", codexGateHelloFor(t, server, sideband[0]).serverName)
	require.Contains(t, sideband[0].target, "call_id="+created.CallID, "sideband 的 call_id 取自第一跳响应")

	// 文件三跳：负例、带 retry 的正例、不带 retry 的正例。
	uploadAttempts := map[string][]officialegress.AttemptIdentity{}
	for _, name := range []string{"notes.txt", "report.pdf", "summary.pdf"} {
		before := len(gate.upstream.attemptIdentities())
		content := []byte("%PDF-1.4 promotion gate")
		_, err := gate.service.UploadOfficialCodexFile(context.Background(), gate.account, OfficialCodexFileUploadInput{
			FileName: name, FileSizeBytes: uint64(len(content)), Contents: bytes.NewReader(content),
		})
		require.NoError(t, err, "%s 上传经生产入口失败", name)
		uploadAttempts[name] = gate.upstream.attemptIdentities()[before:]
	}
	regional := regexp.MustCompile(`^[a-z0-9.-]+\.oaiusercontent\.com$`)
	creates := server.requestsForPath("/backend-api/files")
	require.Len(t, creates, 3)
	for index, name := range []string{"notes.txt", "report.pdf", "summary.pdf"} {
		flow := files.flows[name]
		puts := server.requestsForPath("/files/" + flow.fileID + "/raw")
		require.Len(t, puts, 1, "%s 恰好一次 PUT", name)
		hello := codexGateHelloFor(t, server, puts[0])
		require.Regexp(t, regional, hello.serverName, "%s 的 PUT 必须使用区域 oaiusercontent 主机", name)
		require.Equal(t, codexGateRegionalUploadHost, hello.serverName)
		putURL := "https://" + puts[0].header.Get("Host") + puts[0].target
		require.Equal(t, files.uploadURL(flow.fileID), putURL, "%s 的 PUT URL 必须与 create 返回的 upload_url 一致", name)
		require.Equal(t, sha256.Sum256([]byte(files.uploadURL(flow.fileID))), sha256.Sum256([]byte(putURL)))

		uploaded := server.requestsForPath("/backend-api/files/" + flow.fileID + "/uploaded")
		switch name {
		case "notes.txt":
			require.Len(t, uploaded, 1)
			require.Equal(t, []byte(`{}`), uploaded[0].body, "未预约 C2PA 时 uploaded 必须发送空对象")
		default:
			embedded := []byte(`{"pdf_c2pa_create_request":` + string(creates[index].body) + `}`)
			for _, finalize := range uploaded {
				require.Equal(t, embedded, finalize.body, "%s 的 uploaded 只嵌入本次 create 请求", name)
			}
		}
		if name == "report.pdf" {
			require.Len(t, uploaded, 2, "uploaded 返回 retry 后必须重发一次")
			require.Equal(t, uploaded[0].body, uploaded[1].body, "retry 必须复用同一请求体")
			var finalizeAttempts []officialegress.AttemptIdentity
			for _, attempt := range uploadAttempts[name] {
				if attempt.EndpointID == officialCodexEndpointFilesUploaded {
					finalizeAttempts = append(finalizeAttempts, attempt)
				}
			}
			require.Len(t, finalizeAttempts, 2)
			require.Equal(t, []uint32{1, 2}, []uint32{finalizeAttempts[0].AttemptOrdinal, finalizeAttempts[1].AttemptOrdinal})
			require.Equal(t, string(officialegress.AttemptReasonRetry), finalizeAttempts[1].AttemptReason,
				"retry 必须复用同一 finalize invocation")
			require.Equal(t, finalizeAttempts[0].BundleDigest, finalizeAttempts[1].BundleDigest)
			require.Equal(t, finalizeAttempts[0].ConnectionPoolDigest, finalizeAttempts[1].ConnectionPoolDigest)
		}
	}
	require.NotEqual(t, creates[1].body, creates[2].body, "两次 create 的请求体不同，嵌入才有判别力")

	// 条件变化：默认判定放行。
	accountKey := officialCodexWorkspaceRoutingAccountKey(gate.account)
	defaultDecision := decideOfficialCodexWorkspaceRouting(section, accountKey, []byte(
		`{"accounts":[{"id":"`+accountKey+`","workspace_backend_origin":"NO_CONSTRAINT","account_routing_override":"NO_CONSTRAINT"}]}`))
	require.True(t, defaultDecision.Default)
	recordOfficialCodexWorkspaceRouting(defaultDecision)
	defaultBody, defaultIngress := codexGateRootIngress(t, nil)
	_, err = gate.forward(defaultIngress, defaultBody)
	require.NoError(t, err, "默认工作区路由下 Responses 必须照常出站")
	defaultResponses := server.httpRequestsForPath(codexGateResponsesPath)
	require.Equal(t, "chatgpt.com", codexGateHelloFor(t, server, defaultResponses[len(defaultResponses)-1]).serverName)

	// 条件变化：非默认判定失败关闭（HTTP 与 WS），文件上传不受影响。
	nonDefault := decideOfficialCodexWorkspaceRouting(section, accountKey, []byte(
		`{"accounts":[{"id":"`+accountKey+`","workspace_backend_origin":"https://us.chatgpt.com","account_routing_override":"NO_CONSTRAINT"}]}`))
	require.False(t, nonDefault.Default)
	recordOfficialCodexWorkspaceRouting(nonDefault)
	hellosBefore, requestsBefore := len(server.clientHellos()), len(server.wireRequests())
	blockedBody, blockedIngress := codexGateRootIngress(t, nil)
	_, err = gate.forward(blockedIngress, blockedBody)
	require.ErrorIs(t, err, ErrOfficialCodexWorkspaceRoutingNonDefault, "非默认路由下 HTTP Responses 必须失败关闭")
	wsBody, wsIngress := codexGateRootIngress(t, nil)
	_, _, err = gate.dialResponsesWebSocket(t, wsIngress, codexGateResponseCreateFrame(t, wsBody), gate.webSocketDialer(server))
	require.ErrorIs(t, err, ErrOfficialCodexWorkspaceRoutingNonDefault, "非默认路由下 WS 必须失败关闭")
	require.Len(t, server.clientHellos(), hellosBefore, "失败关闭发生在建立任何连接之前")
	require.Len(t, server.wireRequests(), requestsBefore, "非默认路由下受路由端点不得出站")
	files.flows["late.txt"] = codexGateFileFlow{fileID: "file_gate_late"}
	late := []byte("late")
	_, err = gate.service.UploadOfficialCodexFile(context.Background(), gate.account, OfficialCodexFileUploadInput{
		FileName: "late.txt", FileSizeBytes: uint64(len(late)), Contents: bytes.NewReader(late),
	})
	require.NoError(t, err, "文件上传不属于受路由端点，非默认路由下照常进行")
}
