package service

import (
	"errors"
	"fmt"
	"net/http"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// ============================================================================
// 请求体序列化类受影响规则（数字推理档位、imagegen 背景与编辑引用）的晋升后门禁。
//
// 两条门禁都以 codexGateDiscriminatingReleaseMode 定位目标槽位：只有目标画像声明对应可选节，
// 另一槽位作对照，证明行为确实由画像节驱动、回退旧画像时不会带出新行为。
// ============================================================================

// SPEC-BODY-006（change）：自定义推理档位能按 u64 解析时，Responses 请求体以 JSON 整数发出。
//
// 检查项与网关观测：
//   - numeric-effort-http-integer：官方客户端 HTTP 入站的 reasoning.effort 为 3（JSON 整数）或 "3"
//     （较旧客户端的字符串形态）时，真实 wire 的 reasoning.effort 都是 JSON 整数 3；x-codex-turn-metadata
//     的 reasoning_effort 记十进制字符串 "3"，与请求体 client_metadata 中的同名值逐字节一致；
//   - numeric-effort-ws-integer：第三方 HTTP 入站经上游 WS 发出时，连接上每一帧 response.create 的
//     reasoning.effort 同样是 JSON 整数 3；
//   - known-level-string：已知档位 high 照旧是字符串。
//
// 对照：另一槽位没有 ReasoningEffort 节，同一入站的 reasoning.effort 在 wire 上是字符串 "3"。
func TestCodexNumericReasoningEffortReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "ReasoningEffort 节把可按 u64 解析的自定义档位定型为 JSON 整数",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().ReasoningEffort
			return section != nil && section.CustomNumericSerialization == "u64_integer"
		})

	forwardHTTP := func(mode string, effort any) codexGateWireRequest {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		body, ingress := codexGateRootIngress(t, func(payload map[string]any, _ map[string]any, _ map[string]any) {
			payload["reasoning"] = map[string]any{"effort": effort, "summary": "auto"}
		})
		return codexGateForwardResponsesWire(t, mode, server, ingress, body, nil)
	}
	for _, effort := range []any{3, "3"} {
		request := forwardHTTP(mode, effort)
		wire := gjson.GetBytes(request.body, "reasoning.effort")
		require.Equal(t, gjson.Number, wire.Type, "入站 effort=%#v 时 wire 必须是 JSON 整数：%s", effort, wire.Raw)
		require.Equal(t, "3", wire.Raw)
		header := request.header.Get("X-Codex-Turn-Metadata")
		require.Equal(t, header, gjson.GetBytes(request.body, "client_metadata.x-codex-turn-metadata").String(),
			"请求体 client_metadata 中的 turn metadata 必须与头逐字节一致")
		if section := codexGateExecutableProfile(t, mode).Optional().TurnMetadata; section != nil &&
			slices.Contains(section.Keys, "reasoning_effort") {
			recorded := gjson.Get(header, "reasoning_effort")
			require.Equal(t, gjson.String, recorded.Type, "turn metadata 记档位的字符串形态：%s", header)
			require.Equal(t, "3", recorded.String())
		}
	}
	known := gjson.GetBytes(forwardHTTP(mode, "high").body, "reasoning.effort")
	require.Equal(t, gjson.String, known.Type)
	require.Equal(t, "high", known.String(), "已知档位照旧发字符串")

	forwardWS := func(mode string) [][]byte {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		codexGateUseRecorderManifest(gate.service, gate.account)
		gate.enableWebSocket(t, server)
		handshake, err := gate.forwardViaWebSocket(t, server,
			codexGateWebSocketBody("gpt-5.6-luna", map[string]any{"reasoning": map[string]any{"effort": 3}}), nil)
		require.NoError(t, err)
		frames := codexGateResponseCreateFrames(handshake)
		require.NotEmpty(t, frames)
		return frames
	}
	for index, frame := range forwardWS(mode) {
		wire := gjson.GetBytes(frame, "reasoning.effort")
		require.Equal(t, gjson.Number, wire.Type, "第 %d 帧 response.create 的 reasoning.effort 必须是 JSON 整数：%s", index+1, wire.Raw)
		require.Equal(t, "3", wire.Raw)
	}

	if other, ok := codexGateControlReleaseMode(t, mode); ok {
		control := gjson.GetBytes(forwardHTTP(other, 3).body, "reasoning.effort")
		require.Equal(t, gjson.String, control.Type, "对照：另一槽位没有该节，HTTP 发字符串")
		require.Equal(t, "3", control.String())
		for index, frame := range forwardWS(other) {
			wire := gjson.GetBytes(frame, "reasoning.effort")
			require.Equal(t, gjson.String, wire.Type, "对照：另一槽位第 %d 帧发字符串", index+1)
		}
	}
}

// codexGateImageFileIDEditBody 是引用会话 file-backed 图片的编辑入站：file_id 在前、data URL 在后。
const codexGateImageFileIDEditBody = `{"model":"gpt-image-2","prompt":"make it blue","images":[` +
	`{"file_id":"file-promotion-gate"},{"image_url":"data:image/png;base64,cG5nLWltYWdl"}]}`

// SPEC-EP-022（change）：imagegen 的 background 只取 opaque／transparent，编辑可引用会话 file_id。
//
// 检查项与网关观测：
//   - generation-background-default-opaque：入站未给 background 或给 auto／opaque 时，真实 wire 的
//     background 都是 opaque，顶层字段序不变（prompt, background, model, quality, size）；
//   - generation-background-transparent：入站要求透明时 wire 为 transparent；
//   - edit-file-id-reference / edit-file-id-without-url：编辑 images 按入站原序还原，file_id 项恰为
//     {"file_id"}、不附 image_url，data URL 项照旧为 {"image_url"}。
//
// 对照：另一槽位没有 ImageGeneration 节，background 原样透传（auto 仍是 auto、缺省不补），file_id 编辑在
// 出站前以 400 拒绝、wire 上没有 edits 请求。
func TestCodexImagesBackgroundAndFileReferenceReplaysApprovedSemanticsOnTargetRelease(t *testing.T) {
	mode := codexGateDiscriminatingReleaseMode(t, "ImageGeneration 节声明 opaque 默认背景与 file_id 编辑引用",
		func(profile profilecontract.ExecutableProfile) bool {
			section := profile.Optional().ImageGeneration
			return section != nil && section.DefaultBackground == "opaque" &&
				section.TransparentBackground == "transparent" && slices.Contains(section.EditImageReferences, "file_id")
		})

	generate := func(mode string, background string) codexGateWireRequest {
		t.Helper()
		server := startCodexGateWireServer(t, nil)
		gate := newCodexGateService(t, mode, server)
		field := ""
		if background != "" {
			field = fmt.Sprintf(`"background":%q,`, background)
		}
		_, err := gate.images(t, "/v1/images/generations",
			[]byte(`{`+field+`"quality":"auto","size":"auto","model":"gpt-image-2","prompt":"draw a cat"}`), "application/json")
		require.NoError(t, err, "图像生成经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
		requests := server.requestsForPath(codexGateImagesGenerationsPath)
		require.Len(t, requests, 1)
		return requests[0]
	}
	for inbound, want := range map[string]string{"": "opaque", "auto": "opaque", "opaque": "opaque", "transparent": "transparent"} {
		request := generate(mode, inbound)
		require.Equal(t, want, gjson.GetBytes(request.body, "background").String(), "入站 background=%q", inbound)
		require.Equal(t, []string{"prompt", "background", "model", "quality", "size"},
			codexGateJSONFieldOrder(t, request.body), "generation 的 body 顶层字段必须精确匹配")
	}

	server := startCodexGateWireServer(t, nil)
	gate := newCodexGateService(t, mode, server)
	_, err := gate.images(t, "/v1/images/edits", []byte(codexGateImageFileIDEditBody), "application/json")
	require.NoError(t, err, "file_id 编辑经生产入口发往 %s 槽位失败（上游错误 %v）", mode, gate.upstream.failureList())
	edits := server.requestsForPath(codexGateImagesEditsPath)
	require.Len(t, edits, 1)
	require.JSONEq(t, `[{"file_id":"file-promotion-gate"},{"image_url":"data:image/png;base64,cG5nLWltYWdl"}]`,
		gjson.GetBytes(edits[0].body, "images").Raw, "编辑 images 按入站原序还原，file_id 项不附 image_url")
	require.Equal(t, []string{"images", "prompt", "background", "model"}, codexGateJSONFieldOrder(t, edits[0].body))
	require.Equal(t, "opaque", gjson.GetBytes(edits[0].body, "background").String())

	if other, ok := codexGateControlReleaseMode(t, mode); ok {
		require.Equal(t, "auto", gjson.GetBytes(generate(other, "auto").body, "background").String(),
			"对照：另一槽位原样透传 background")
		require.False(t, gjson.GetBytes(generate(other, "").body, "background").Exists(), "对照：另一槽位缺省不补 background")

		controlServer := startCodexGateWireServer(t, nil)
		controlGate := newCodexGateService(t, other, controlServer)
		_, err := controlGate.images(t, "/v1/images/edits", []byte(codexGateImageFileIDEditBody), "application/json")
		var rejection *OpenAIImagesUpstreamError
		require.True(t, errors.As(err, &rejection), "对照：另一槽位不接受 file_id 引用（%v）", err)
		require.Equal(t, http.StatusBadRequest, rejection.StatusCode)
		require.Empty(t, controlServer.requestsForPath(codexGateImagesEditsPath), "对照：被拒绝的 file_id 编辑不得出站")
	}
}
