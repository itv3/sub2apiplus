package service

import (
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

func TestOfficialCodex0145EngineUsesProfileDrivenImageToolPresentation(t *testing.T) {
	profile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	require.False(t, profile.ToolPresentation.HostedImageGenerationAllowed)
	require.Equal(t, "image_generation", profile.ToolPresentation.HostedImageGenerationType)
	require.Equal(t, "image_gen", profile.ToolPresentation.NamespaceName)
	require.Equal(t, "imagegen", profile.ToolPresentation.FunctionName)

	hostedPayload := map[string]any{
		"tools": []any{
			map[string]any{"type": "function", "name": "shell"},
			map[string]any{"type": "image_generation", "output_format": "png"},
		},
	}
	changed, err := officialCodexNormalizeDerivedToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		hostedPayload,
	)
	require.False(t, changed)
	require.ErrorContains(t, err, "必须由客户端提供完整 image_gen/imagegen")

	body := []byte(`{"tools":[{"type":"namespace","name":"image_gen","description":"生成图片","tools":[{"type":"function","name":"imagegen","description":"生成一张图片","strict":false,"parameters":{"type":"object"}}]}],"input":[]}`)
	require.NoError(t, officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		body,
		false,
	))
}

func TestOfficialCodex0145EngineRejectsHostedImageToolAndWrongLiteCarrier(t *testing.T) {
	hosted := []byte(`{"tools":[{"type":"image_generation"}],"input":[]}`)
	err := officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		hosted,
		false,
	)
	require.ErrorContains(t, err, "禁止 hosted")

	namespace := []byte(`{"tools":[{"type":"namespace","name":"image_gen","description":"生成图片","tools":[{"type":"function","name":"imagegen","description":"生成一张图片","strict":false,"parameters":{"type":"object"}}]}],"input":[]}`)
	err = officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
		namespace,
		true,
	)
	require.ErrorContains(t, err, "错误载体")

	lite := []byte(`{"input":[{"type":"additional_tools","tools":[{"type":"namespace","name":"image_gen","description":"生成图片","tools":[{"type":"function","name":"imagegen","description":"生成一张图片","strict":false,"parameters":{"type":"object"}}]}]}]}`)
	require.NoError(t, officialCodexValidateToolPresentation(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesWS),
		lite,
		true,
	))
}

func TestOfficialCodex0145EngineTLSProfileComesFromImmutableSnapshot(t *testing.T) {
	httpProfile, err := officialCodexResolveTLSProfile(
		officialCodexVersion0145,
		officialCodexTransportHTTPDefault,
	)
	require.NoError(t, err)
	require.Len(t, httpProfile.CipherSuites, 30)
	require.Equal(t, uint16(0x1302), httpProfile.CipherSuites[0])
	require.Empty(t, httpProfile.ALPNProtocols)
	require.True(t, httpProfile.Transport.DisableCompression)
	require.True(t, httpProfile.Transport.LowercaseHeaders)
	require.True(t, httpProfile.Transport.StrictH1Wire)
	require.NotEmpty(t, httpProfile.Transport.H1HeaderOrders)

	versionProfile, err := resolveCodexVersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	httpEndpointCount := 0
	for _, endpoint := range versionProfile.Endpoints {
		if endpoint.TransportID == officialCodexTransportHTTPDefault {
			httpEndpointCount++
		}
	}
	require.Len(t, httpProfile.Transport.H1HeaderOrders, httpEndpointCount)
	for _, rule := range httpProfile.Transport.H1HeaderOrders {
		require.NotEmpty(t, rule.Method)
		require.NotEmpty(t, rule.Path)
		require.Empty(t, rule.PathContains)
		require.True(t, rule.RejectUnlisted)
		require.Equal(t, tlsfingerprint.H1HeaderOrderModeStatic, rule.Mode)
	}

	// 返回值是独占深拷贝，任何调用点误改都不能污染下一次画像解析。
	httpProfile.CipherSuites[0] = 0
	httpProfile.Transport.H1HeaderOrders[0].Order[0] = "mutated"
	fresh, err := officialCodexResolveTLSProfile(
		officialCodexVersion0145,
		officialCodexTransportHTTPDefault,
	)
	require.NoError(t, err)
	require.Equal(t, uint16(0x1302), fresh.CipherSuites[0])
	require.NotEqual(t, "mutated", fresh.Transport.H1HeaderOrders[0].Order[0])
}

func TestOfficialCodex0145EngineCompilesExactHTTPAndWSSwapRemoveRules(t *testing.T) {
	httpProfile, err := officialCodexResolveEndpointTLSProfile(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)
	httpRule := officialCodex0145FindH1Rule(
		t,
		httpProfile.Transport.H1HeaderOrders,
		http.MethodPost,
		"/backend-api/codex/responses",
	)
	require.Contains(t, httpRule.Order, "host")
	require.Contains(t, httpRule.Order, "content-length")
	require.Less(t, indexOfOfficialCodex0145TestHeader(httpRule.Order, "user-agent"), indexOfOfficialCodex0145TestHeader(httpRule.Order, "host"))
	require.Less(t, indexOfOfficialCodex0145TestHeader(httpRule.Order, "host"), indexOfOfficialCodex0145TestHeader(httpRule.Order, "content-length"))

	wsProfile, err := officialCodexResolveEndpointTLSProfile(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesWS),
	)
	require.NoError(t, err)
	require.True(t, wsProfile.Transport.StrictH1Wire)
	require.True(t, wsProfile.Transport.LowercaseHeaders)
	require.Equal(t, []string{
		"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key",
	}, wsProfile.Transport.PreserveHeaderCase)

	wsRule := officialCodex0145FindH1Rule(
		t,
		wsProfile.Transport.H1HeaderOrders,
		http.MethodGet,
		"/backend-api/codex/responses",
	)
	prefix := []string{"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key"}
	require.Equal(t, tlsfingerprint.H1HeaderOrderModeSwapRemove, wsRule.Mode)
	require.Equal(t, prefix, wsRule.PrefixHeaders)
	require.Equal(t, prefix, wsRule.RemoveHeaders)
	require.Equal(t, []string{
		"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key",
		"version", "x-codex-beta-features", "x-client-request-id", "session-id", "thread-id",
		"x-codex-window-id", "x-codex-turn-metadata", "x-codex-parent-thread-id",
		"x-openai-subagent", "x-openai-memgen-request", "openai-beta",
		"x-responsesapi-include-timing-metrics", "originator", "user-agent",
		"x-openai-internal-codex-residency", "authorization", "chatgpt-account-id",
		"x-openai-fedramp",
	}, wsRule.Order)
	require.Equal(t, []string{"sec-websocket-extensions"}, wsRule.AppendHeaders)
}

func TestOfficialCodex0145EngineRejectsUnknownVersionTransportAndEndpoint(t *testing.T) {
	_, err := officialCodexResolveTLSProfile("0.145.1", officialCodexTransportHTTPDefault)
	require.ErrorContains(t, err, "未知")
	_, err = officialCodexResolveTLSProfile(officialCodexVersion0145, "http-default")
	require.ErrorContains(t, err, "不支持传输画像")
	_, err = officialCodexResolveEndpointTLSProfile(
		officialCodexVersion0145,
		codexEndpointID("responses"),
	)
	require.ErrorContains(t, err, "不支持端点画像")
}

func TestOfficialCodex0145EngineBuildsClosedEndpointURL(t *testing.T) {
	modelsURL, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointModels),
		officialCodexEndpointURLInput{},
	)
	require.NoError(t, err)
	require.Equal(t, "https://chatgpt.com/backend-api/codex/models?client_version=0.145.0", modelsURL.String())

	sidebandURL, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
		officialCodexEndpointURLInput{QueryValues: map[string]string{"call_id": "call / 1"}},
	)
	require.NoError(t, err)
	require.Equal(t, "wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=call+%2F+1", sidebandURL.String())

	uploadedURL, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesUploaded),
		officialCodexEndpointURLInput{PathValues: map[string]string{"file_id": "file/a"}},
	)
	require.NoError(t, err)
	require.Equal(t, "https://chatgpt.com/backend-api/files/file%2Fa/uploaded", uploadedURL.String())
	uploadedTLS, err := officialCodexResolveEndpointTLSProfileForURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesUploaded),
		uploadedURL,
	)
	require.NoError(t, err)
	officialCodex0145FindH1Rule(
		t,
		uploadedTLS.Transport.H1HeaderOrders,
		http.MethodPost,
		"/backend-api/files/file%2Fa/uploaded",
	)

	returned := "https://upload.oaiusercontent.com/blob/a?sig=a%2Fb&se=1"
	blobURL, err := officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesBlobUpload),
		officialCodexEndpointURLInput{ReturnedURL: returned},
	)
	require.NoError(t, err)
	require.Equal(t, returned, blobURL.String())
	blobTLS, err := officialCodexResolveEndpointTLSProfileForURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesBlobUpload),
		blobURL,
	)
	require.NoError(t, err)
	officialCodex0145FindH1Rule(t, blobTLS.Transport.H1HeaderOrders, http.MethodPut, "/blob/a")

	_, err = officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
		officialCodexEndpointURLInput{},
	)
	require.ErrorContains(t, err, "缺少 query")
	_, err = officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointModels),
		officialCodexEndpointURLInput{QueryValues: map[string]string{"client_version": "0.145.1"}},
	)
	require.ErrorContains(t, err, "不允许覆盖")
	_, err = officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesBlobUpload),
		officialCodexEndpointURLInput{ReturnedURL: "https://upload.oaiusercontent.com/blob/a"},
	)
	require.ErrorContains(t, err, "缺少必需 query")
	_, err = officialCodexBuildEndpointURL(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointFilesBlobUpload),
		officialCodexEndpointURLInput{ReturnedURL: "https://evil.example/blob?sig=1"},
	)
	require.ErrorContains(t, err, "不匹配画像")
}

func TestOfficialCodex0145EngineAppliesHeaderSlotsAndCompactAlternate(t *testing.T) {
	headers := http.Header{
		"X-Codex-Installation-Id": {"installation-1"},
		"X-Codex-Turn-State":      {"state-1"},
		"X-Codex-Window-Id":       {"window-1"},
		"X-Codex-Turn-Metadata":   {`{"turn_id":"turn-1"}`},
		"Session-Id":              {"session-1"},
		"Thread-Id":               {"thread-1"},
		"Authorization":           {"Bearer token"},
		"Chatgpt-Account-Id":      {"account-1"},
		"Originator":              {"codex_exec"},
		"User-Agent":              {"codex_exec/0.145.0"},
	}
	fields, err := officialCodexApplyHeaderContract(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
		headers,
		map[string]bool{
			officialCodexConditionBetaFeatures: true,
			officialCodexConditionTurnState:    true,
		},
	)
	require.NoError(t, err)
	names := make([]string, 0, len(fields))
	for _, field := range fields {
		names = append(names, field.Name)
	}
	require.Equal(t, []string{
		"version", "x-codex-installation-id", "x-codex-turn-state",
		"x-codex-window-id", "x-codex-turn-metadata", "session-id", "thread-id",
		"authorization", "chatgpt-account-id", "content-type", "accept",
		"originator", "user-agent",
	}, names)
	require.Equal(t, officialCodexVersion0145, headers.Get("version"))
	require.Empty(t, headers.Get("x-codex-beta-features"))
	require.Equal(t, "application/json", headers.Get("content-type"))

	invalid := headers.Clone()
	invalid.Set("x-unknown", "leak")
	_, err = officialCodexApplyHeaderContract(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
		invalid,
		nil,
	)
	require.ErrorContains(t, err, "不允许 header")
}

func TestOfficialCodex0145EngineValidatesClosedJSONAndOrdersFields(t *testing.T) {
	body := []byte(`{
		"parallel_tool_calls": true,
		"instructions": "",
		"input": [ { "role": "user" } ],
		"model": "gpt-5",
		"reasoning": null
	}`)
	ordered, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
		body,
		nil,
	)
	require.NoError(t, err)
	require.JSONEq(t, `{"model":"gpt-5","input":[{"role":"user"}],"parallel_tool_calls":true}`, string(ordered))
	require.True(t, strings.HasPrefix(string(ordered), `{"model":"gpt-5","input":`))

	_, err = officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
		[]byte(`{"model":"gpt-5","input":[],"parallel_tool_calls":true,"unknown":1}`),
		nil,
	)
	require.ErrorContains(t, err, "不允许 JSON 字段")
	_, err = officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointResponsesCompact),
		[]byte(`{"model":"gpt-5","input":[]}`),
		nil,
	)
	require.ErrorContains(t, err, "parallel_tool_calls")

	// sideband 是开放事件契约，但 discriminator 仍必须存在且排在画像字段最前。
	sideband, err := officialCodexValidateAndOrderJSONBody(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointRealtimeSideband),
		[]byte(`{"event_id":"evt-1","type":"session.update"}`),
		nil,
	)
	require.NoError(t, err)
	require.Equal(t, `{"type":"session.update","event_id":"evt-1"}`, string(sideband))
}

func officialCodex0145FindH1Rule(
	t *testing.T,
	rules []tlsfingerprint.H1HeaderOrderRule,
	method string,
	path string,
) tlsfingerprint.H1HeaderOrderRule {
	t.Helper()
	for _, rule := range rules {
		if rule.Method == method && rule.Path == path {
			return rule
		}
	}
	t.Fatalf("未找到 H1 规则：%s %s", method, path)
	return tlsfingerprint.H1HeaderOrderRule{}
}

func indexOfOfficialCodex0145TestHeader(headers []string, expected string) int {
	for index, header := range headers {
		if header == expected {
			return index
		}
	}
	return -1
}

func TestOfficialCodex0145EngineHeaderResultIsDeterministic(t *testing.T) {
	endpoint, err := resolveCodexEndpoint(
		officialCodexVersion0145,
		codexEndpointID(officialCodexEndpointModels),
	)
	require.NoError(t, err)
	first := endpoint.OrderedHeaders()
	second := endpoint.OrderedHeaders()
	require.True(t, reflect.DeepEqual(first, second))
}
