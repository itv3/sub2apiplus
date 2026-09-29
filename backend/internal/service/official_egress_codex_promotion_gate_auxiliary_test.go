package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"mime"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"slices"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// Codex 辅助端点（alpha-search、images）受影响规则的晋升后门禁。
//
// 请求从官方 Codex 客户端形态的入站请求出发，经生产业务入口（ForwardAlphaSearch、
// ParseOpenAIImagesRequest + ForwardImages）发往按结构事实定位的目标槽位，断言本地
// TLS 终端观测到的真实 wire。账号级 Cookie jar 预先建立，覆盖画像的 cookie 条件槽位。
// ============================================================================

const (
	codexGateAlphaSearchPath       = "/backend-api/codex/alpha/search"
	codexGateImagesGenerationsPath = "/backend-api/codex/images/generations"
	codexGateImagesEditsPath       = "/backend-api/codex/images/edits"
)

// codexGateAlphaSearchBody 构造一次 alpha-search 入站请求体：闭集外字段放在前面并
// 打乱顺序，用来确认出站顶层字段由画像闭集与线序决定。
func codexGateAlphaSearchBody(commands string) []byte {
	return []byte(`{"reasoning":{"effort":"max","context":"all_turns"},"max_output_tokens":2000,` +
		`"settings":{"allowed_callers":["direct"],"external_web_access":true},` +
		`"commands":` + commands + `,` +
		`"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"latest news"}]}],` +
		`"model":"gpt-5.6-sol","id":"search-session","future_field":{"keep":true}}`)
}

// SPEC-EP-015（change）：alpha-search 的 header/body 精确，commands 随阶段变化。
//
// 检查项与网关观测：
//   - search-header-order：同一运行的两次 alpha-search 在真实 wire 上使用同一精确线序
//     version, x-codex-turn-metadata, authorization, chatgpt-account-id, content-type,
//     originator, user-agent, accept, cookie, host, content-length（目标画像把 accept 移到
//     originator、user-agent 之后；cookie 在 jar 建立后出现）；
//   - search-body-order：wire 请求体顶层字段恰为 id, model, input, commands, settings,
//     max_output_tokens（闭集外字段被投影掉，顺序由画像决定）；
//   - search-command-phases：检索阶段由官方客户端产生，网关对应的事实是逐阶段原样
//     透传——两阶段 commands 在 wire 上的哈希不同，且分别等于各自入站的 commands，
//     网关不缓存、不改写阶段性 commands。
func TestCodexAlphaSearchWireReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t,
		"alpha_search 的 accept 位于 originator、user-agent 与 residency 之后",
		func(profile profilecontract.ExecutableProfile) bool {
			return codexGateAcceptAfterIdentity(profile, officialCodexEndpointAlphaSearch)
		})
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	gate.seedCookie("__cf_bm", "promotion-gate")

	phases := []string{
		`{"search_query":[{"q":"OpenAI news","recency":1}]}`,
		`{"open":[{"ref_id":"turn0search0","lineno":1}]}`,
	}
	for _, commands := range phases {
		_, err := gate.alphaSearch(codexGateAlphaSearchBody(commands))
		require.NoError(t, err, "alpha-search 经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
	}
	requests := server.requestsForPath(codexGateAlphaSearchPath)
	require.Len(t, requests, len(phases), "每个检索阶段恰好一次 alpha-search 出站")

	wantOrder := []string{
		"version", "x-codex-turn-metadata", "authorization", "chatgpt-account-id", "content-type",
		"originator", "user-agent", "accept", "cookie", "host", "content-length",
	}
	commandDigests := make(map[string]bool, len(requests))
	for index, request := range requests {
		require.Equal(t, wantOrder, request.lowerHeaderNames(),
			"第 %d 次 alpha-search 的 wire 线序必须精确匹配（accept 位于 originator、user-agent 之后）", index+1)
		require.Equal(t, []string{"*/*"}, request.values("accept"))
		require.Equal(t,
			[]string{"id", "model", "input", "commands", "settings", "max_output_tokens"},
			codexGateJSONFieldOrder(t, request.body),
			"第 %d 次 alpha-search 的 body 顶层字段必须精确匹配", index+1)
		commands := gjson.GetBytes(request.body, "commands").Raw
		require.JSONEq(t, phases[index], commands, "第 %d 阶段的 commands 必须原样透传", index+1)
		sum := sha256.Sum256([]byte(commands))
		commandDigests[hex.EncodeToString(sum[:])] = true
	}
	require.Len(t, commandDigests, len(phases), "同一运行两阶段的 commands 哈希必须不同")
}

// codexGateImageEditMultipart 构造 multipart 形态的图像编辑入站请求（含 n 与 mask），
// 用来确认网关改用 JSON data URL 出站。
func codexGateImageEditMultipart(t *testing.T) ([]byte, string) {
	t.Helper()
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for _, field := range [][2]string{
		{"n", "2"}, {"size", "1024x1024"}, {"quality", "high"}, {"background", "auto"},
		{"prompt", "replace background with aurora"}, {"model", "gpt-image-2"},
	} {
		require.NoError(t, writer.WriteField(field[0], field[1]))
	}
	for _, part := range []struct{ name, file, content string }{
		{"image", "source.png", "png-image-content"},
		{"mask", "mask.png", "png-mask-content"},
	} {
		header := make(textproto.MIMEHeader)
		header.Set("Content-Disposition", `form-data; name="`+part.name+`"; filename="`+part.file+`"`)
		header.Set("Content-Type", "image/png")
		writerPart, err := writer.CreatePart(header)
		require.NoError(t, err)
		_, err = writerPart.Write([]byte(part.content))
		require.NoError(t, err)
	}
	require.NoError(t, writer.Close())
	return body.Bytes(), writer.FormDataContentType()
}

// SPEC-EP-022（change）：images generations/edits 使用 JSON、精确字段与相同 Header 线序。
//
// 检查项与网关观测：
//   - image-header-order：generation 与 edit 在真实 wire 上使用同一精确线序 version,
//     authorization, chatgpt-account-id, content-type, originator, user-agent, accept,
//     cookie, host, content-length（目标画像把 accept 移到 originator、user-agent 之后）；
//   - generation-body-order：generation 请求体顶层字段恰为 prompt, background, model,
//     quality, size（入站的 n、style、output_format 不出站）；
//   - edit-body-order：edit 在首位增加 images、省略 n；
//   - edit-json-data-url：multipart 入站的 edit 以 application/json 出站，images 元素是
//     data: URL，wire 上不存在任何 multipart 分段。
func TestCodexImagesWireReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t,
		"images_generations 与 images_edits 的 accept 位于 originator、user-agent 与 residency 之后",
		func(profile profilecontract.ExecutableProfile) bool {
			return codexGateAcceptAfterIdentity(profile, officialCodexEndpointImagesGenerations) &&
				codexGateAcceptAfterIdentity(profile, officialCodexEndpointImagesEdits)
		})
	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	gate.seedCookie("__cf_bm", "promotion-gate")

	generationBody := []byte(`{"n":3,"style":"vivid","output_format":"png","size":"1024x1024",` +
		`"quality":"high","background":"auto","model":"gpt-image-2","prompt":"draw a cat"}`)
	_, err := gate.images(t, "/v1/images/generations", generationBody, "application/json")
	require.NoError(t, err, "图像生成经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
	editBody, editContentType := codexGateImageEditMultipart(t)
	_, err = gate.images(t, "/v1/images/edits", editBody, editContentType)
	require.NoError(t, err, "图像编辑经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())

	generations := server.requestsForPath(codexGateImagesGenerationsPath)
	edits := server.requestsForPath(codexGateImagesEditsPath)
	require.Len(t, generations, 1)
	require.Len(t, edits, 1)
	wantOrder := []string{
		"version", "authorization", "chatgpt-account-id", "content-type", "originator",
		"user-agent", "accept", "cookie", "host", "content-length",
	}
	require.Equal(t, wantOrder, generations[0].lowerHeaderNames(), "generation 的 wire 线序必须精确匹配")
	require.Equal(t, wantOrder, edits[0].lowerHeaderNames(), "edit 的 wire 线序必须与 generation 一致且精确")

	require.Equal(t, []string{"prompt", "background", "model", "quality", "size"},
		codexGateJSONFieldOrder(t, generations[0].body), "generation 的 body 顶层字段必须精确匹配")
	require.Equal(t, []string{"images", "prompt", "background", "model", "quality", "size"},
		codexGateJSONFieldOrder(t, edits[0].body), "edit 必须在首位增加 images 且省略 n")
	require.False(t, gjson.GetBytes(edits[0].body, "n").Exists(), "edit 不得携带 n")

	mediaType, _, err := mime.ParseMediaType(edits[0].header.Get("Content-Type"))
	require.NoError(t, err)
	require.Equal(t, "application/json", mediaType, "edit 必须以 JSON 出站而不是 multipart")
	require.False(t, strings.HasPrefix(mediaType, "multipart/"), "edit 的 multipart 分段数必须为 0")
	images := gjson.GetBytes(edits[0].body, "images").Array()
	require.Len(t, images, 1, "只有 image 分段转成 data URL，mask 不出站")
	for _, image := range images {
		require.True(t, strings.HasPrefix(image.Get("image_url").String(), "data:"),
			"edit 的 images 元素必须是 data URL：%s", image.Raw)
	}
	require.False(t, gjson.GetBytes(edits[0].body, "mask").Exists())
}

// codexGateDriveAuxiliaryEndpoints 经各自的生产入口依次发出 models、alpha-search、
// images generation/edit、OAuth refresh 与 realtime 第一跳，返回终端上按路径归集的请求。
func codexGateDriveAuxiliaryEndpoints(
	t *testing.T,
	mode string,
	server *codexGateWireServer,
	gate *codexGateService,
) map[string]codexGateWireRequest {
	t.Helper()
	require.NoError(t, gate.models(), "models 经生产入口失败")
	_, err := gate.alphaSearch(codexGateAlphaSearchBody(`{"search_query":[{"q":"news"}]}`))
	require.NoError(t, err, "alpha-search 经生产入口失败")
	_, err = gate.images(t, "/v1/images/generations",
		[]byte(`{"model":"gpt-image-2","prompt":"draw a cat","size":"1024x1024"}`), "application/json")
	require.NoError(t, err, "图像生成经生产入口失败")
	editBody, editContentType := codexGateImageEditMultipart(t)
	_, err = gate.images(t, "/v1/images/edits", editBody, editContentType)
	require.NoError(t, err, "图像编辑经生产入口失败")
	require.NoError(t, gate.oauthRefresh(), "OAuth refresh 经生产入口失败")
	_, _, err = gate.realtimeCall(t, mode)
	require.NoError(t, err, "realtime 第一跳经生产入口失败")
	out := make(map[string]codexGateWireRequest)
	for _, path := range []string{
		codexGateModelsPath, codexGateAlphaSearchPath, codexGateImagesGenerationsPath,
		codexGateImagesEditsPath, "/oauth/token", codexGateRealtimeCallsPath,
	} {
		requests := server.requestsForPath(path)
		require.NotEmpty(t, requests, "终端没有收到 %s", path)
		out[path] = requests[len(requests)-1]
	}
	return out
}

// codexGateAcceptAfterIdentityEndpoints 是目标画像把 accept 移到默认头之后的六个端点。
var codexGateAcceptAfterIdentityEndpoints = []string{
	officialCodexEndpointAlphaSearch, officialCodexEndpointImagesEdits, officialCodexEndpointImagesGenerations,
	officialCodexEndpointModels, officialCodexEndpointOAuthRefresh, officialCodexEndpointRealtimeCalls,
}

// SPEC-HDR-002（change）：Client 默认头为 originator、user-agent 和条件 residency；无显式
// accept 的端点 accept 位于默认头之后。
//
// 检查项与网关观测：
//   - default-identity-headers / default-user-agent：Responses、models、alpha-search、
//     images generation/edit、OAuth refresh、realtime 第一跳的真实 wire 都含 originator 与
//     user-agent；六个无显式 accept 的端点 accept 位于二者之后（目标画像的补丁）；
//   - residency-positive：账号受管配置 residency=us 时，经请求运行态出站的端点（Responses、
//     alpha-search、images、realtime 第一跳）发送恰为 us 的
//     x-openai-internal-codex-residency，且位于 user-agent 之后、accept 之前；
//   - residency-negative：未配置 residency 时全部端点完全不发送该头。
//
// models 清单由缓存刷新在后台上下文拉取、OAuth refresh 以进程级身份编译，二者当前拿不到
// 账号运行态，不携带 residency（生产缺口另行报告），因而正例只对上述四类端点断言；负例
// 对全部端点成立。
func TestCodexClientDefaultHeadersReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, "六个无显式 accept 的端点 accept 位于 originator、user-agent 与 residency 之后",
		func(profile profilecontract.ExecutableProfile) bool {
			for _, endpointID := range codexGateAcceptAfterIdentityEndpoints {
				if !codexGateAcceptAfterIdentity(profile, endpointID) {
					return false
				}
			}
			return true
		})
	const residencyHeader = "x-openai-internal-codex-residency"
	requireIdentityThenAccept := func(request codexGateWireRequest, where string, explicitAccept bool) {
		t.Helper()
		names := request.lowerHeaderNames()
		originator, userAgent, accept := slices.Index(names, "originator"), slices.Index(names, "user-agent"), slices.Index(names, "accept")
		require.GreaterOrEqual(t, originator, 0, "%s 必须包含 originator", where)
		require.GreaterOrEqual(t, userAgent, 0, "%s 必须包含 user-agent", where)
		require.GreaterOrEqual(t, accept, 0, "%s 必须包含 accept", where)
		if explicitAccept {
			return
		}
		require.Greater(t, accept, originator, "%s 的 accept 必须位于 originator 之后", where)
		require.Greater(t, accept, userAgent, "%s 的 accept 必须位于 user-agent 之后", where)
		if residency := slices.Index(names, residencyHeader); residency >= 0 {
			require.Greater(t, residency, userAgent, "%s 的 residency 位于 user-agent 之后", where)
			require.Greater(t, accept, residency, "%s 的 accept 必须位于 residency 之后", where)
		}
	}

	for _, residency := range []string{"", "us"} {
		server := startCodexGateWireServer(t, codexGateRealtimeResponse)
		gate := newCodexGateService(t, mode, server)
		if residency != "" {
			gate.account.Extra[officialCodexResidencyAccountExtraKey] = residency
		}
		body, ingress := codexGateRootIngress(t, nil)
		_, err := gate.forward(ingress, body)
		require.NoError(t, err)
		responses := server.httpRequestsForPath(codexGateResponsesPath)
		require.Len(t, responses, 1)
		auxiliary := codexGateDriveAuxiliaryEndpoints(t, mode, server, gate)

		requireIdentityThenAccept(responses[0], "Responses", true)
		for path, request := range auxiliary {
			requireIdentityThenAccept(request, path, false)
		}
		if residency == "" {
			require.False(t, responses[0].has(residencyHeader), "未配置 residency 时 Responses 不得发送该头")
			for path, request := range auxiliary {
				require.False(t, request.has(residencyHeader), "未配置 residency 时 %s 不得发送该头", path)
			}
			continue
		}
		require.Equal(t, []string{"us"}, responses[0].values(residencyHeader), "受管 residency=us 时 Responses 发送精确值")
		for _, path := range []string{
			codexGateAlphaSearchPath, codexGateImagesGenerationsPath, codexGateImagesEditsPath, codexGateRealtimeCallsPath,
		} {
			require.Equal(t, []string{"us"}, auxiliary[path].values(residencyHeader), "受管 residency=us 时 %s 发送精确值", path)
		}
	}
}

// SPEC-HDR-006（condition_change）：accept 按传输和端点变化。
//
// 检查项与网关观测：
//   - responses-http-sse：HTTP Responses 的 wire accept 恰为 text/event-stream；
//   - ws-no-accept：Responses WS 握手的 wire 不发送 accept；
//   - auxiliary-wildcard：辅助端点（models、alpha-search、images generation/edit、OAuth
//     refresh、realtime 第一跳）的 wire accept 恰为 */*。
//
// 条件变化来自 legacy compact 删除：它原本是 accept */* 的 Responses 变体，目标画像中
// 普通 Responses 之外不再有任何 Responses 端点，画像逐端点 accept 与上述一致。
func TestCodexAcceptByTransportReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateTargetReleaseMode(t, codexGateLegacyCompactFeature, codexGateLegacyCompactRemoved)
	profile := codexGateExecutableProfile(t, mode)
	require.True(t, codexGateLegacyCompactRemoved(profile), "条件变化的来源：目标画像不再有 accept */* 的 legacy compact")
	for _, endpoint := range profile.Endpoints() {
		accept, declared := codexGateHeaderSlot(endpoint, "accept")
		switch {
		case endpoint.Upgrade != "":
			require.False(t, declared, "WS 端点 %s 不得声明 accept", endpoint.ID)
		case endpoint.ID == officialCodexEndpointResponsesHTTP:
			require.Equal(t, "text/event-stream", accept.Value)
		default:
			require.True(t, declared, "%s 必须声明 accept", endpoint.ID)
			require.Equal(t, "*/*", accept.Value, "%s 的 accept 必须为 */*", endpoint.ID)
		}
	}

	server := startCodexGateWireServer(t, codexGateRealtimeResponse)
	body, ingress := codexGateRootIngress(t, nil)
	responses := codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	require.Equal(t, []string{"text/event-stream"}, responses.values("accept"), "HTTP Responses 必须使用 text/event-stream")

	gate := newCodexGateService(t, mode, server)
	codexGateUseRecorderManifest(gate.service, gate.account)
	gate.enableWebSocket(t, server)
	handshake, err := gate.forwardViaWebSocket(t, server, codexGateWebSocketBody("gpt-5.6-luna", nil),
		http.Header{"Accept": []string{"text/event-stream"}})
	require.NoError(t, err)
	require.False(t, handshake.has("accept"), "WS 握手不得发送 accept")

	for path, request := range codexGateDriveAuxiliaryEndpoints(t, mode, server, gate) {
		require.Equal(t, []string{"*/*"}, request.values("accept"), "辅助端点 %s 的 accept 必须为 */*", path)
	}
}
