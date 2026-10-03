package service

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 较新版本画像新增两节（ReasoningEffort、ImageGeneration）在服务层的消费：
//   - 推理等级：声明 ReasoningEffort 节时，能按 u64 解析的自定义档位以 JSON 整数出站，turn metadata 仍记十进制
//     字符串；未声明时入站整数按十进制字符串出站（旧客户端对同一配置的形态）。
//   - 生图：声明 ImageGeneration 节时 background 只取默认值或透明值，编辑 images[] 按原序还原 image_url 与 file_id
//     引用；未声明时 background 原样透传、file_id 失败关闭。file_id 只在 OAuth（Codex 官方出口）路径按画像判定，
//     API Key 透传等路径以原解析期文案拒绝。
//
// 画像来源都用合成夹具（以正式目录 Active 为底稿追加或去掉节），VC-6 晋升前后同一用例都成立。

// reasoningEffortIntegerMutation 追加目标形态的 ReasoningEffort 节。
func reasoningEffortIntegerMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.ReasoningEffort = syntheticServiceRawSection(t, profilecontract.ReasoningEffortSection{
			CustomNumericSerialization: "u64_integer",
		})
	}
}

// imageGenerationTargetMutation 追加目标形态的 ImageGeneration 节。
func imageGenerationTargetMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		doc.ImageGeneration = syntheticServiceRawSection(t, profilecontract.ImageGenerationSection{
			DefaultBackground: "opaque", TransparentBackground: "transparent",
			EditImageReferences: []string{"file_id", "image_url"},
		})
	}
}

// withOfficialCodexLegacyImageGenerationProfile 提供“画像未声明 ImageGeneration 节”的旧画像对照组。
func withOfficialCodexLegacyImageGenerationProfile(t *testing.T) {
	t.Helper()
	withOfficialCodexLegacySyntheticProfile(t, "ImageGeneration 节",
		func(profile profilecontract.ExecutableProfile) bool { return profile.Optional().ImageGeneration != nil },
		func(doc *profilecontract.SnapshotDoc) { doc.ImageGeneration = nil })
}

func TestNormalizeOfficialOpenAIExplicitReasoningEffortFollowsSerialization(t *testing.T) {
	type outcome struct {
		value   any
		changed bool
	}
	cases := []struct {
		name    string
		raw     any
		integer outcome // 画像声明 ReasoningEffort 节
		legacy  outcome // 画像未声明（旧版本）
	}{
		{"已知档位", "high", outcome{"high", false}, outcome{"high", false}},
		{"ultra 映射 max", "ultra", outcome{"max", true}, outcome{"max", true}},
		{"max 保持", "max", outcome{"max", false}, outcome{"max", false}},
		{"数字字符串", "3", outcome{json.Number("3"), true}, outcome{"3", false}},
		{"前导加号", "+3", outcome{json.Number("3"), true}, outcome{"+3", false}},
		{"前导零", "007", outcome{json.Number("7"), true}, outcome{"007", false}},
		{"u64 上界", "18446744073709551615", outcome{json.Number("18446744073709551615"), true}, outcome{"18446744073709551615", false}},
		{"超出 u64 仍为字符串", "18446744073709551616", outcome{"18446744073709551616", false}, outcome{"18446744073709551616", false}},
		{"含空白仍为字符串", " 3", outcome{" 3", false}, outcome{" 3", false}},
		{"负号仍为字符串", "-3", outcome{"-3", false}, outcome{"-3", false}},
		{"小数仍为字符串", "3.5", outcome{"3.5", false}, outcome{"3.5", false}},
		{"单独加号仍为字符串", "+", outcome{"+", false}, outcome{"+", false}},
		{"JSON 整数", json.Number("3"), outcome{json.Number("3"), false}, outcome{"3", true}},
		{"未启用 UseNumber 的整数", float64(12), outcome{json.Number("12"), true}, outcome{"12", true}},
	}
	for _, item := range cases {
		for _, mode := range []struct {
			integer bool
			want    outcome
		}{{true, item.integer}, {false, item.legacy}} {
			value, changed, err := normalizeOfficialOpenAIExplicitReasoningEffort(item.raw, mode.integer)
			require.NoError(t, err, "%s（整数模式=%v）", item.name, mode.integer)
			require.Equal(t, mode.want.value, value, "%s（整数模式=%v）", item.name, mode.integer)
			require.Equal(t, mode.want.changed, changed, "%s（整数模式=%v）", item.name, mode.integer)
		}
	}

	for name, raw := range map[string]any{
		"空字符串":      "",
		"空白字符串":     "   ",
		"负整数":       json.Number("-1"),
		"JSON 小数":   json.Number("1.5"),
		"超出 u64 的数": json.Number("18446744073709551616"),
		"负浮点":       float64(-1),
		"非整数浮点":     1.5,
		"布尔":        true,
		"缺省 null":   nil,
		"对象":        map[string]any{"level": "high"},
	} {
		for _, integer := range []bool{true, false} {
			_, _, err := normalizeOfficialOpenAIExplicitReasoningEffort(raw, integer)
			require.Error(t, err, "%s（整数模式=%v）必须失败关闭", name, integer)
		}
	}
}

func TestParseOfficialCodexU64EffortMatchesRustParse(t *testing.T) {
	for raw, want := range map[string]uint64{"0": 0, "3": 3, "+3": 3, "0003": 3, "18446744073709551615": 18446744073709551615} {
		parsed, ok := parseOfficialCodexU64Effort(raw)
		require.True(t, ok, raw)
		require.Equal(t, want, parsed, raw)
	}
	for _, raw := range []string{"", "+", "++3", "-0", " 3", "3 ", "3.0", "1e3", "0x10", "18446744073709551616", "high"} {
		_, ok := parseOfficialCodexU64Effort(raw)
		require.False(t, ok, "%q 不是 u64", raw)
	}
}

func TestOfficialOpenAIReasoningDefaultsFollowReasoningEffortSection(t *testing.T) {
	egressContext := &OfficialEgressContext{profileMode: officialClientProfileModeActive}

	withOfficialCodexSyntheticProfile(t, reasoningEffortIntegerMutation(t))
	require.True(t, officialOpenAIReasoningDefaultsFromContext(egressContext).NumericEffortAsInteger,
		"声明 ReasoningEffort 节的画像启用整数序列化")

	withOfficialCodexLegacySyntheticProfile(t, "ReasoningEffort 节",
		func(profile profilecontract.ExecutableProfile) bool { return profile.Optional().ReasoningEffort != nil },
		func(doc *profilecontract.SnapshotDoc) { doc.ReasoningEffort = nil })
	require.False(t, officialOpenAIReasoningDefaultsFromContext(egressContext).NumericEffortAsInteger,
		"未声明该节的画像一律发字符串")
	require.False(t, officialOpenAIReasoningDefaultsFromContext(nil).NumericEffortAsInteger)
}

func TestNormalizeDerivedOfficialOpenAIReasoningSerializesNumericEffort(t *testing.T) {
	derive := func(effort any, integer bool) any {
		payload := map[string]any{"reasoning": map[string]any{"effort": effort}}
		_, err := normalizeDerivedOfficialOpenAIReasoning(payload, officialOpenAIReasoningDefaults{NumericEffortAsInteger: integer})
		require.NoError(t, err)
		reasoning, ok := payload["reasoning"].(map[string]any)
		require.True(t, ok, "reasoning 必须保持对象形态")
		return reasoning["effort"]
	}
	require.Equal(t, json.Number("3"), derive("3", true))
	require.Equal(t, json.Number("3"), derive(json.Number("3"), true))
	require.Equal(t, "3", derive(json.Number("3"), false))
	require.Equal(t, "3", derive("3", false))
	require.Equal(t, "high", derive("high", true))

	raw, err := json.Marshal(map[string]any{"effort": derive("3", true)})
	require.NoError(t, err)
	require.JSONEq(t, `{"effort":3}`, string(raw), "整数模式在 wire 上是 JSON 数字")

	_, err = normalizeDerivedOfficialOpenAIReasoning(
		map[string]any{"reasoning": map[string]any{"effort": json.Number("-1")}},
		officialOpenAIReasoningDefaults{NumericEffortAsInteger: true},
	)
	require.Error(t, err)
}

func TestOfficialOpenAIEffectiveReasoningEffortRecordsNumericAsString(t *testing.T) {
	effective := func(effort any) string {
		return officialOpenAIEffectiveReasoningEffort(
			map[string]any{"reasoning": map[string]any{"effort": effort}},
			officialOpenAIReasoningDefaults{Effort: "medium"},
		)
	}
	require.Equal(t, "3", effective(json.Number("3")), "turn metadata 记档位的十进制字符串")
	require.Equal(t, "3", effective("3"))
	require.Equal(t, "max", effective("ultra"))
	require.Equal(t, "medium", effective(json.Number("-1")), "非法数字不进入 turn metadata，回落到默认档位")
}

func TestParseOpenAIImagesRequestKeepsFileIDReferencesInOrder(t *testing.T) {
	parse := func(body string) (*OpenAIImagesRequest, error) {
		gin.SetMode(gin.TestMode)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/images/edits", nil)
		c.Request.Header.Set("Content-Type", "application/json")
		return (&OpenAIGatewayService{}).ParseOpenAIImagesRequest(c, []byte(body))
	}
	parsed, err := parse(`{"model":"gpt-image-2","prompt":"make it blue","images":[` +
		`{"file_id":"file-a"},{"image_url":"data:image/png;base64,QUFB"},{"file_id":" file-b "}]}`)
	require.NoError(t, err)
	require.Equal(t, []string{"file-a", "file-b"}, parsed.InputImageFileIDs)
	require.Equal(t, []string{"data:image/png;base64,QUFB"}, parsed.InputImageURLs)
	require.Equal(t, []OpenAIImageInputRef{
		{FileID: "file-a"}, {ImageURL: "data:image/png;base64,QUFB"}, {FileID: "file-b"},
	}, parsed.InputImageRefs)
	require.NotContains(t, string(parsed.ModerationBody()), "file-a", "file_id 图片内容不在网关，不进入审核正文")

	parsed, err = parse(`{"model":"gpt-image-2","prompt":"edit","images":[{"file_id":"file-only"}]}`)
	require.NoError(t, err, "只有 file_id 引用的编辑请求在解析层合法")
	require.Equal(t, []string{"file-only"}, parsed.InputImageFileIDs)

	for _, body := range []string{
		`{"model":"gpt-image-2","prompt":"edit","images":[{"file_id":""}]}`,
		`{"model":"gpt-image-2","prompt":"edit","images":[{"file_id":42}]}`,
		`{"model":"gpt-image-2","prompt":"edit","images":[{"file_id":null}]}`,
	} {
		_, err := parse(body)
		require.EqualError(t, err, "images[].file_id must be a non-empty string", body)
	}
	_, err = parse(`{"model":"gpt-image-2","prompt":"edit","images":[{"file_id":"file-a"}],"mask":{"file_id":"file-m"}}`)
	require.EqualError(t, err, "mask.file_id is not supported (use mask.image_url instead)", "mask 仍不接受 file_id")
}

func TestBuildOpenAICodexImagesRequestBodyFollowsImageGenerationSection(t *testing.T) {
	generation := func(background string) []byte {
		body, err := buildOpenAICodexImagesRequestBody(&OpenAIImagesRequest{
			Endpoint: openAIImagesGenerationsEndpoint, Model: "gpt-image-2", Prompt: "draw a cat",
			Background: background, Quality: "auto", Size: "auto",
		}, "gpt-image-2", officialClientProfileModeActive)
		require.NoError(t, err)
		return body
	}
	mixedEdit := &OpenAIImagesRequest{
		Endpoint: openAIImagesEditsEndpoint, Model: "gpt-image-2", Prompt: "make it blue",
		InputImageURLs:    []string{"data:image/png;base64,QUFB"},
		InputImageFileIDs: []string{"file-a"},
		InputImageRefs:    []OpenAIImageInputRef{{FileID: "file-a"}, {ImageURL: "data:image/png;base64,QUFB"}},
	}

	withOfficialCodexSyntheticProfile(t, imageGenerationTargetMutation(t))
	for inbound, want := range map[string]string{
		"": "opaque", "auto": "opaque", "opaque": "opaque", "transparent": "transparent", "Transparent": "transparent",
	} {
		body := generation(inbound)
		require.Equal(t, want, gjson.GetBytes(body, "background").String(), "入站 background=%q", inbound)
		require.Equal(t, []string{"prompt", "background", "model", "quality", "size"}, codexGateJSONFieldOrder(t, body))
	}
	body, err := buildOpenAICodexImagesRequestBody(mixedEdit, "gpt-image-2", officialClientProfileModeActive)
	require.NoError(t, err)
	require.JSONEq(t, `[{"file_id":"file-a"},{"image_url":"data:image/png;base64,QUFB"}]`,
		gjson.GetBytes(body, "images").Raw, "编辑按入站原序还原 file_id 与 image_url 引用，file_id 项不附 image_url")
	require.Equal(t, "opaque", gjson.GetBytes(body, "background").String())

	withOfficialCodexLegacyImageGenerationProfile(t)
	require.Equal(t, "auto", gjson.GetBytes(generation("auto"), "background").String(), "旧画像原样透传 background")
	require.False(t, gjson.GetBytes(generation(""), "background").Exists(), "旧画像缺省时不补 background")
	_, err = buildOpenAICodexImagesRequestBody(mixedEdit, "gpt-image-2", officialClientProfileModeActive)
	require.EqualError(t, err, openAIImagesFileIDUnsupportedMessage, "旧画像不接受 file_id 引用")
}

func TestBuildOpenAIImagesResponsesRequestRejectsFileIDReferences(t *testing.T) {
	_, err := buildOpenAIImagesResponsesRequest(&OpenAIImagesRequest{
		Endpoint: openAIImagesEditsEndpoint, Model: "gpt-image-2", Prompt: "edit",
		InputImageFileIDs: []string{"file-a"}, InputImageRefs: []OpenAIImageInputRef{{FileID: "file-a"}},
	}, "gpt-image-2")
	require.Error(t, err, "Responses 图像工具路径不能表达 file_id 引用，不得静默丢弃")
}

func TestForwardImagesRejectsFileIDReferencesOutsideCodexOAuth(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/images/edits", nil)
	parsed := &OpenAIImagesRequest{
		Endpoint: openAIImagesEditsEndpoint, Model: "gpt-image-2", Prompt: "edit",
		InputImageFileIDs: []string{"file-a"}, InputImageRefs: []OpenAIImageInputRef{{FileID: "file-a"}},
	}
	account := &Account{ID: 9, Platform: PlatformOpenAI, Type: AccountTypeAPIKey}

	result, err := (&OpenAIGatewayService{}).ForwardImages(c.Request.Context(), c, account, []byte(`{}`), parsed, "")
	require.Nil(t, result)
	var rejection *OpenAIImagesUpstreamError
	require.True(t, errors.As(err, &rejection), "按用户错误收尾，处理器不记账号失败、不切号")
	require.Equal(t, http.StatusBadRequest, rejection.StatusCode)
	require.False(t, IsOpenAIImagesRetryableUpstreamError(rejection))
	require.Equal(t, http.StatusBadRequest, recorder.Code)
	require.Equal(t, "invalid_request_error", gjson.Get(recorder.Body.String(), "error.type").String())
	require.Equal(t, openAIImagesFileIDUnsupportedMessage, gjson.Get(recorder.Body.String(), "error.message").String(),
		"沿用原解析期的拒绝文案")
}
