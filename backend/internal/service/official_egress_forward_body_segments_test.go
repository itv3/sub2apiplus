package service

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestOfficialForwardHTTPBodyDefersValidatedSmallFields(t *testing.T) {
	body := buildOfficialEgressMemoryProfileBody(t, 128<<10)
	_, workspace := newOfficialForwardHTTPBody(context.Background(), body)
	account := newOfficialOpenAIHTTPTestAccount(94)
	normalized, changed, err := workspace.normalizeResponsesLitePayloadForAccount(body, account)
	require.NoError(t, err)
	require.False(t, changed)
	require.True(t, officialForwardSameBody(body, normalized), "只补 Lite 小字段时应保留原正文")

	view := newOpenAIRequestView(body)
	payload, err := workspace.decodeRequestView(nil, view)
	require.NoError(t, err)
	payload["include"] = append(payload["include"].([]any), "额外字段")
	payload["client_metadata"].(map[string]any)["session_id"] = "new-session"
	want, err := marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)
	require.NoError(t, err)
	got := body
	requestBody := payload
	_, err = workspace.reencodeRequestBody(payload, &got, &view, &requestBody)
	require.NoError(t, err)
	require.True(t, officialForwardSameBody(body, got), "小字段覆盖层不能物化完整历史正文")
	require.Nil(t, workspace.rebuilt)
	require.NotNil(t, requestBody, "底层正文未变时保留本次对象树供 Finalizer 接收")

	restored, err := workspace.decodeRequestView(nil, newOpenAIRequestView(got))
	require.NoError(t, err)
	encoded, err := marshalOfficialJSONObjectPreservingOrderAndRaw(restored, body)
	require.NoError(t, err)
	require.Equal(t, want, encoded, "后续对象树读取必须还原旧重编码路径的全部修改")
	// 覆盖层里的小值需要保持不可变；一次读取后的 map 修改不能污染重试得到的下一棵树。
	restored["client_metadata"].(map[string]any)["session_id"] = "temporary"
	again, err := workspace.decodeRequestView(nil, newOpenAIRequestView(got))
	require.NoError(t, err)
	require.Equal(t, "new-session", again["client_metadata"].(map[string]any)["session_id"])
}

func TestOfficialForwardHTTPBodyMaterializesChangedBusinessField(t *testing.T) {
	body := []byte(`{"model":"m","input":[{"role":"user","content":"旧文本"}],"reasoning":{}}`)
	_, workspace := newOfficialForwardHTTPBody(context.Background(), body)
	_, _, err := workspace.normalizeResponsesLitePayloadForAccount(body, newOfficialOpenAIHTTPTestAccount(94))
	require.NoError(t, err)
	view := newOpenAIRequestView(body)
	payload, err := workspace.decodeRequestView(nil, view)
	require.NoError(t, err)
	payload["input"].([]any)[0].(map[string]any)["content"] = "新文本"
	want, err := marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)
	require.NoError(t, err)
	requestBody := payload
	got, err := workspace.reencodeRequestBody(payload, &body, &view, &requestBody)
	require.NoError(t, err)
	require.Equal(t, want, got, "历史实际变化时必须保持原重建语义")
	require.NotNil(t, workspace.rebuilt)
}

func TestOfficialJSONChangedInputKeepsOriginalSegments(t *testing.T) {
	original := []byte(`{"input":[ {"role":"user","content":"\u0061","n":9007199254740993,"x":1e3}, {"type":"reasoning","encrypted_content":"gAAAAAB-unmodified"} ],"instructions":"入口指令"}`)
	payload, index, err := decodeOfficialJSONObjectSharingBody(original)
	require.NoError(t, err)
	_, err = moveOfficialOpenAIHTTPInstructionsToInput(payload, payload["instructions"])
	require.NoError(t, err)
	want, err := marshalOfficialJSONObjectPreservingOrderAndRawWithIndex(payload, original, index)
	require.NoError(t, err)
	members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, nil, original, index)
	require.NoError(t, err)
	require.Equal(t, want, officialegress.AppendJSONObjectMembers(nil, members))
	var input officialegress.JSONObjectMember
	for _, member := range members {
		if member.Name == "input" {
			input = member
		}
	}
	require.Nil(t, input.Value)
	require.NotEmpty(t, input.ValueSegments)
	shared := 0
	for _, segment := range input.ValueSegments {
		if officialForwardOffsetWithin(segment, original) >= 0 {
			shared++
		}
	}
	require.Equal(t, 2, shared, "两个未修改历史元素应直接引用原文字节")
	require.True(t, bytes.Contains(want, []byte(`"content":"\u0061","n":9007199254740993,"x":1e3`)))
}

func TestNormalizeOpenAIResponsesLiteHeaderFieldsMatchesFullNormalization(t *testing.T) {
	fixtures := []string{
		`{}`, `{"parallel_tool_calls":true}`, `{"reasoning":{"context":"current_turn"}}`,
		`{"parallel_tool_calls":"invalid","tools":[{"type":"unsupported"}]}`,
		`{"reasoning":4}`, `{"reasoning":null,"parallel_tool_calls":false}`,
		`{"tools":["custom",{"type":"function","name":"f"}]}`,
		`{"tools":[{"type":"namespace","name":"n","tools":[]}]}`,
		`{"reasoning":3,"reasoning":{"context":"all_turns"},"parallel_tool_calls":false}`,
		`{"reasoning":`, `null`, `[]`,
	}
	for _, fixture := range fixtures {
		fields, handled, gotErr := normalizeOpenAIResponsesLiteHeaderFields([]byte(fixture))
		if !handled {
			continue
		}
		var payload map[string]any
		require.NoError(t, json.Unmarshal([]byte(fixture), &payload))
		_, wantErr := normalizeOpenAIResponsesLiteTools(payload)
		if wantErr != nil {
			require.EqualError(t, gotErr, wantErr.Error(), fixture)
			continue
		}
		require.NoError(t, gotErr, fixture)
		for _, name := range []string{"tools", "reasoning", "parallel_tool_calls"} {
			require.Equal(t, payload[name], fields[name], fixture)
		}
	}
}

func TestNormalizeOpenAIResponsesLiteHeaderFieldsCanonicalFallback(t *testing.T) {
	for _, fixture := range []string{
		`{"input":[{"type":"compaction_trigger"}]}`,
		`{"input":[{"type":"reasoning","content":[{"text":"visible"}]}]}`,
		`{"input":[{"type":"message","type":"reasoning","content":[],"content":[{"text":"visible"}]}]}`,
		`{"input":[],"input":[{"type":"compaction_trigger"}]}`,
	} {
		_, handled, err := normalizeOpenAIResponsesLiteHeaderFields([]byte(fixture))
		require.NoError(t, err)
		require.False(t, handled, "后续会整体排序的形态应立即归一化：%s", fixture)
	}
}

func TestOfficialForwardHTTPBodyFinalizerHandoffDoesNotLeakIntoRetry(t *testing.T) {
	body := []byte(`{"model":"m","input":[{"role":"user","content":"原始文本"}],"reasoning":{"effort":"high"},"include":[]}`)
	_, workspace := newOfficialForwardHTTPBody(context.Background(), body)
	_, _, err := workspace.normalizeResponsesLitePayloadForAccount(body, newOfficialOpenAIHTTPTestAccount(94))
	require.NoError(t, err)
	view := newOpenAIRequestView(body)
	payload, err := workspace.decodeRequestView(nil, view)
	require.NoError(t, err)
	payload["include"] = []any{"reasoning.encrypted_content"}
	requestBody := payload
	_, err = workspace.reencodeRequestBody(payload, &body, &view, &requestBody)
	require.NoError(t, err)
	workspace.releaseRequestMap(&requestBody, view, body)
	require.Nil(t, requestBody)

	finalized, _, err := workspace.decodeFinalizerPayload(body)
	require.NoError(t, err)
	require.Nil(t, workspace.finalizerPayload, "对象树只允许移交一次")
	require.Nil(t, workspace.finalizerBody)
	finalized["input"].([]any)[0].(map[string]any)["content"] = "定型期间改写"
	require.Equal(t, "定型期间改写", payload["input"].([]any)[0].(map[string]any)["content"], "第一次应直接接收已有对象树")
	finalized["reasoning"].(map[string]any)["context"] = "current_turn"
	delete(finalized, "include")

	retry, _, err := workspace.decodeFinalizerPayload(body)
	require.NoError(t, err)
	require.True(t, workspace.applyDeferredFields(retry, body))
	require.Equal(t, "原始文本", retry["input"].([]any)[0].(map[string]any)["content"])
	require.Equal(t, "all_turns", retry["reasoning"].(map[string]any)["context"])
	require.Equal(t, []any{"reasoning.encrypted_content"}, retry["include"])
}

func TestOfficialForwardHTTPBodyCacheKeyOverlayPreservesSessionHash(t *testing.T) {
	for _, key := range []any{"new-session", "", "  ", nil} {
		body := []byte(`{"model":"m","prompt_cache_key":"old-session","input":[{"role":"user","content":"文本"}]}`)
		_, workspace := newOfficialForwardHTTPBody(context.Background(), body)
		_, _, err := workspace.normalizeResponsesLitePayloadForAccount(body, newOfficialOpenAIHTTPTestAccount(94))
		require.NoError(t, err)
		view := newOpenAIRequestView(body)
		payload, err := workspace.decodeRequestView(nil, view)
		require.NoError(t, err)
		if key == nil {
			delete(payload, "prompt_cache_key")
		} else {
			payload["prompt_cache_key"] = key
		}
		want, err := marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)
		require.NoError(t, err)
		requestBody := payload
		_, err = workspace.reencodeRequestBody(payload, &body, &view, &requestBody)
		require.NoError(t, err)
		svc := &OpenAIGatewayService{}
		c := newOpenAIRejectedFieldTestContext(body)
		require.Equal(t, svc.GenerateSessionHash(c, want), svc.GenerateSessionHash(c, workspace.sessionHashBody(body)), "缓存键=%v", key)
		if key == "new-session" {
			require.Nil(t, workspace.rebuilt)
			require.Equal(t, "new-session", gjson.GetBytes(workspace.sessionHashBody(body), "prompt_cache_key").String())
		} else {
			require.NotNil(t, workspace.rebuilt, "空键/删除必须物化，才能使用正确的内容哈希回退")
		}
	}
}

func TestOfficialForwardHTTPBodyDeferredFieldsSurviveRejectedStatusRetry(t *testing.T) {
	tc := officialForwardBodyCase{
		name: "deferred_status_retry",
		body: func(t *testing.T) []byte { return newOfficialOpenAIHTTPTestBody(t, true, false, true) },
		context: func(_ *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		},
		responses: []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
			`{"error":{"code":"unsupported_parameter","message":"Unsupported parameter: input[3].status","param":"input[3].status","type":"invalid_request_error"}}`)},
	}
	outcome := officialForwardBodyCompare(t, tc)
	require.NoError(t, outcome.err)
	require.Len(t, outcome.calls, 2)
	for _, call := range outcome.calls {
		require.Equal(t, "all_turns", gjson.GetBytes(call.decoded, "reasoning.context").String())
	}
	require.False(t, gjson.GetBytes(outcome.calls[1].decoded, "input.3.status").Exists())
}

func TestOfficialForwardHTTPBodyWritesLiteDefaultsWhenFinalizerOtherwiseUnchanged(t *testing.T) {
	seed := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	contract, err := captureOfficialOpenAIHTTPBodyContract(seed)
	require.NoError(t, err)
	payload, err := decodeOfficialJSONObjectUseNumber(seed)
	require.NoError(t, err)
	options := officialOpenAIHTTPBodyOptions{UseResponsesLite: true, ProfileMode: officialClientProfileModeActive}
	_, err = finalizeOfficialOpenAIHTTPBodyPayloadInPlace(payload, contract, officialOpenAIReasoningDefaults{}, options)
	require.NoError(t, err)
	changed, err := finalizeOfficialOpenAIHTTPBodyPayloadInPlace(payload, contract, officialOpenAIReasoningDefaults{}, options)
	require.NoError(t, err)
	require.False(t, changed, "夹具除缺少 Lite 默认值之外已经满足最终定型")
	delete(payload["reasoning"].(map[string]any), "context")
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	account := newOfficialOpenAIHTTPTestAccount(94)
	legacyBody, changed, err := normalizeOpenAIResponsesLitePayloadForAccount(body, account)
	require.NoError(t, err)
	require.True(t, changed)
	ctx, workspace := newOfficialForwardHTTPBody(context.Background(), body)
	_, _, err = workspace.normalizeResponsesLitePayloadForAccount(body, account)
	require.NoError(t, err)
	view := newOpenAIRequestView(body)
	payload, err = workspace.decodeRequestView(nil, view)
	require.NoError(t, err)
	workspace.releaseRequestMap(&payload, view, body)
	require.Empty(t, workspace.deferredFields, "只在对象树内补 Lite 默认值，不依赖一次额外重编码建立覆盖层")
	svc := &OpenAIGatewayService{}
	plan := openAIUpstreamRequestPlan{IsStream: true, IsCodexCLI: true, OfficialEgressBodyContract: contract}
	legacy, err := svc.buildUpstreamRequest(context.Background(), newOfficialOpenAIHTTPTestContext(seed, "/v1/responses"), account, legacyBody, "oauth-token", plan)
	require.NoError(t, err)
	current, err := svc.buildUpstreamRequest(ctx, newOfficialOpenAIHTTPTestContext(seed, "/v1/responses"), account, body, "oauth-token", plan)
	require.NoError(t, err)
	want, got := mustReadRequestBody(t, legacy), mustReadRequestBody(t, current)
	require.JSONEq(t, string(want), string(got))
	require.Equal(t, "all_turns", gjson.GetBytes(got, "reasoning.context").String())
}
