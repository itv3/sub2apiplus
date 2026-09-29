package service

import (
	"encoding/json"
	"math/rand"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M2-a：官方出站 HTTP 转发主干不再整程持有对象树与重编码正文。
//   - 重编码按成员产出，字节与 marshalOfficialJSONObjectPreservingOrderAndRaw 完全相同（含错误）；与调用方
//     原始正文逐字节相同的大成员引用原始正文，其余引用改写前正文的成员值复制成小段；
//   - Finalizer 的成员按来源回指后字节不变，且不再引用 Forward 的整段正文；
//   - 上游 attempt 期间放下整段正文，恢复后逐字节相同；对象树在定型前放下；
//   - 整链上，上游 attempt 期间的存活堆比改造前少约两倍正文（对象树与重编码正文）。

func forwardMembersInBody(value, body []byte) bool {
	return officialForwardOffsetWithin(value, body) >= 0
}

// forwardMembersRandomDocument 生成含一个大 input 的请求：input 由随机项组成（含长加密内容），另有若干
// 小的顶层字段。
func forwardMembersRandomDocument(rng *rand.Rand, largeItems int) map[string]any {
	input := make([]any, 0, largeItems+4)
	for i := 0; i < largeItems; i++ {
		input = append(input, map[string]any{
			"type": "reasoning", "summary": []any{},
			"encrypted_content": "gAAAAAB" + strings.Repeat(string(rune('A'+rng.Intn(26))), 2048+rng.Intn(4096)),
		})
		input = append(input, map[string]any{"type": "message", "role": "user", "content": "turn-" + strconv.Itoa(i)})
	}
	document := map[string]any{
		"model": "gpt-5.6-luna", "stream": true, "input": input,
		"client_metadata": map[string]any{"session_id": "s-" + strconv.Itoa(rng.Intn(1000))},
		"reasoning":       map[string]any{"effort": "high"},
		"big":             json.Number("9007199254740993"),
	}
	for i, n := 0, rng.Intn(4); i < n; i++ {
		document[spliceRandomKey(rng)] = spliceRandomValue(rng, 1)
	}
	return document
}

// forwardMembersMutatePayload 按 Forward 业务改写的典型形态修改对象树：改写小字段、增删顶层键，
// 有时改动 input（删项或追加项）。
func forwardMembersMutatePayload(rng *rand.Rand, payload map[string]any) {
	payload["client_metadata"] = map[string]any{"session_id": "rewritten", "x-codex-installation-id": "i"}
	if rng.Intn(2) == 0 {
		payload["include"] = []any{"reasoning.encrypted_content"}
	}
	if rng.Intn(3) == 0 {
		delete(payload, "big")
	}
	if rng.Intn(4) == 0 {
		if input, ok := payload["input"].([]any); ok && len(input) > 2 {
			payload["input"] = append([]any{}, input[1:]...)
		}
	}
	if rng.Intn(5) == 0 {
		payload["bad"] = make(chan int)
	}
}

func TestOfficialForwardHTTPBodyReencodeMatchesMarshal(t *testing.T) {
	rng := rand.New(rand.NewSource(20261003))
	located, copied, compared := 0, 0, 0
	for round := 0; round < 160; round++ {
		name := "round#" + strconv.Itoa(round)
		document := forwardMembersRandomDocument(rng, 8+rng.Intn(24))
		ingress, err := marshalOpenAIUpstreamJSON(document)
		require.NoError(t, err)
		// base 模拟 Lite 归一化等前置改写产生的中间正文：与原始正文只差小的顶层字段；一半轮次直接用原始正文。
		base := ingress
		if rng.Intn(2) == 0 {
			rewritten, rewriteErr := decodeOfficialJSONObjectUseNumber(ingress)
			require.NoError(t, rewriteErr)
			rewritten["parallel_tool_calls"] = false
			rewritten["reasoning"] = map[string]any{"effort": "high", "context": "all_turns"}
			base, err = marshalOfficialJSONObjectPreservingOrderAndRaw(rewritten, ingress)
			require.NoError(t, err)
		}
		payload, err := decodeOfficialJSONObjectUseNumber(base)
		require.NoError(t, err)
		forwardMembersMutatePayload(rng, payload)

		want, wantErr := marshalOfficialJSONObjectPreservingOrderAndRaw(membersHandoffCopy(t, payload), base)
		_, workspace := newOfficialForwardHTTPBody(t.Context(), ingress)
		got := base
		view := newOpenAIRequestView(base)
		reqBody := payload
		returned, gotErr := workspace.reencodeRequestBody(membersHandoffCopy(t, payload), &got, &view, &reqBody)
		if decodeSharingErrorParity(t, name, wantErr, gotErr) {
			require.Equal(t, wantErr.Error(), gotErr.Error(), name)
			require.Nil(t, got, "%s：出错时正文与改造前一样为 nil", name)
			require.Nil(t, returned, "%s：出错时返回的正文同样为 nil", name)
			continue
		}
		compared++
		require.Equal(t, string(want), string(got), "%s：重编码字节必须一致", name)
		require.True(t, officialForwardSameBody(returned, got), "%s：返回的正文就是写回 *body 的正文", name)
		require.Equal(t, string(officialegress.AppendJSONObjectMembers(nil, workspace.members)), string(got), name)
		require.True(t, officialForwardSameBody(got, workspace.rebuilt), name)
		require.Nil(t, reqBody, "%s：物化前放下对象树", name)
		require.Nil(t, view.body, "%s：物化前放下请求视图里的旧正文", name)
		require.Nil(t, workspace.index, "%s：物化前放下旧正文的索引", name)
		sameAsIngress := officialForwardSameBody(base, ingress)
		for _, member := range workspace.members {
			if forwardMembersInBody(member.Value, ingress) {
				located++
				continue
			}
			if !sameAsIngress && forwardMembersInBody(member.Value, base) {
				require.GreaterOrEqual(t, len(member.Value), officialForwardDetachLocateMinBytes,
					"%s：成员 %s 不得以小段引用改写前正文", name, member.Name)
				require.NotContains(t, string(ingress), string(member.Value),
					"%s：大成员 %s 能在原始正文找到时必须引用原始正文", name, member.Name)
				continue
			}
			copied++
		}
		for i, span := range workspace.spans {
			require.Equal(t, string(workspace.members[i].Value), string(got[span.start:span.end]), name)
		}
	}
	require.Greater(t, compared, 100)
	require.Greater(t, located, 50, "夹具必须覆盖大成员回指原始正文")
	require.Greater(t, copied, 100, "夹具必须覆盖小成员复制与新编码成员")
}

func TestOfficialForwardHTTPBodyRebasesFinalizerMembers(t *testing.T) {
	rng := rand.New(rand.NewSource(20261004))
	for round := 0; round < 80; round++ {
		name := "round#" + strconv.Itoa(round)
		document := forwardMembersRandomDocument(rng, 16)
		ingress, err := marshalOpenAIUpstreamJSON(document)
		require.NoError(t, err)
		payload, err := decodeOfficialJSONObjectUseNumber(ingress)
		require.NoError(t, err)
		payload["client_metadata"] = map[string]any{"session_id": "forward"}
		_, workspace := newOfficialForwardHTTPBody(t.Context(), ingress)
		rebuilt := ingress
		view := newOpenAIRequestView(ingress)
		reqBody := payload
		_, err = workspace.reencodeRequestBody(payload, &rebuilt, &view, &reqBody)
		require.NoError(t, err)

		// 模拟 Finalizer：在重编码正文上解码、按画像字段序定型并按成员产出。
		finalPayload, index, err := decodeOfficialJSONObjectSharingBody(rebuilt)
		require.NoError(t, err)
		delete(finalPayload, "client_metadata")
		finalPayload["store"] = false
		if rng.Intn(2) == 0 {
			finalPayload["moved_input"] = finalPayload["input"]
		}
		order := []string{"model", "input", "store", "stream"}
		members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(finalPayload, order, rebuilt, index)
		require.NoError(t, err)
		want := string(officialegress.AppendJSONObjectMembers(nil, members))
		referencedRebuilt := false
		for _, member := range members {
			referencedRebuilt = referencedRebuilt || forwardMembersInBody(member.Value, rebuilt)
		}
		require.True(t, referencedRebuilt, "%s：夹具中 Finalizer 成员应引用重编码正文", name)

		// 定型输入不是重编码物化的那份正文（内容相同但另一份字节）时不回指，成员保持原引用。
		workspace.rebaseFinalMembers(append([]byte(nil), rebuilt...), members)
		stillReferenced := false
		for _, member := range members {
			stillReferenced = stillReferenced || forwardMembersInBody(member.Value, rebuilt)
		}
		require.True(t, stillReferenced, "%s：不是同一份正文时不得回指", name)
		workspace.rebaseFinalMembers(rebuilt, members)
		require.Equal(t, want, string(officialegress.AppendJSONObjectMembers(nil, members)), "%s：回指后字节必须一致", name)
		for _, member := range members {
			require.False(t, forwardMembersInBody(member.Value, rebuilt), "%s：成员 %s 不得再引用重编码正文", name, member.Name)
		}
	}
	var nilWorkspace *officialForwardHTTPBody
	nilWorkspace.rebaseFinalMembers([]byte(`{}`), nil)
	require.Nil(t, nilWorkspace.membersFor([]byte(`{}`)))
}

func TestOfficialForwardHTTPBodyParkRestoresIdenticalBody(t *testing.T) {
	document := forwardMembersRandomDocument(rand.New(rand.NewSource(20261005)), 8)
	ingress, err := marshalOpenAIUpstreamJSON(document)
	require.NoError(t, err)
	payload, err := decodeOfficialJSONObjectUseNumber(ingress)
	require.NoError(t, err)
	payload["service_tier"] = "priority"
	_, workspace := newOfficialForwardHTTPBody(t.Context(), ingress)
	body := ingress
	view := newOpenAIRequestView(ingress)
	reqBody := payload
	_, err = workspace.reencodeRequestBody(payload, &body, &view, &reqBody)
	require.NoError(t, err)
	view = newOpenAIRequestView(body)
	expected := string(body)
	expectedView := view
	lineage := body
	require.NotNil(t, workspace.indexFor(body))
	require.NotEmpty(t, workspace.membersFor(body), "Finalizer 不改写时可按成员装配")
	require.Nil(t, workspace.membersFor(append([]byte(nil), body...)), "不是同一份正文时不提供成员")

	require.Equal(t, "priority", *workspace.serviceTier(body), "未暂存时按正文读 service_tier")

	workspace.park(&body, &view, &lineage)
	require.Nil(t, body, "attempt 期间放下正文")
	require.Nil(t, lineage, "attempt 期间放下 lineage 基准")
	require.Nil(t, view.body, "attempt 期间放下请求视图")
	require.Nil(t, workspace.index, "attempt 期间放下正文索引")
	require.Nil(t, workspace.rebuilt)
	require.Equal(t, "priority", *workspace.serviceTier(body), "暂存期间从成员读出与正文相同的 service_tier")
	workspace.park(&body, &view, &lineage)
	require.True(t, workspace.parked, "已暂存时重复暂存不生效")
	workspace.restore()
	require.Equal(t, expected, string(body), "恢复后正文逐字节相同")
	require.True(t, officialForwardSameBody(body, lineage), "lineage 基准恢复为同一份正文")
	require.True(t, officialForwardSameBody(body, view.body), "请求视图恢复为同一份正文")
	require.Equal(t, expectedView.Model, view.Model)
	require.Equal(t, expectedView.ServiceTier, view.ServiceTier)
	require.Equal(t, expectedView.PromptCacheKey, view.PromptCacheKey)
	require.True(t, officialForwardSameBody(body, workspace.rebuilt), "恢复后仍可按来源回指")
	restored := body
	workspace.restore()
	require.True(t, officialForwardSameBody(restored, body), "重复恢复不再物化")
	require.Nil(t, workspace.parkedBody, "恢复后不再持有 Forward 变量地址")

	other := append([]byte(nil), body...)
	otherView := newOpenAIRequestView(other)
	otherLineage := body
	workspace.park(&other, &otherView, &otherLineage)
	require.False(t, workspace.parked, "不是重编码正文时不放下")
	require.NotNil(t, other)
	require.NotNil(t, otherLineage)

	var nilWorkspace *officialForwardHTTPBody
	nilWorkspace.park(&other, &otherView, &otherLineage)
	nilWorkspace.restore()
	require.NotNil(t, other)
	require.Nil(t, nilWorkspace.serviceTier([]byte(`{"a":1}`)))
	require.Equal(t, "flex", *nilWorkspace.serviceTier([]byte(`{"service_tier":"flex"}`)))

	noTier, err := decodeOfficialJSONObjectUseNumber(ingress)
	require.NoError(t, err)
	delete(noTier, "service_tier")
	_, tierless := newOfficialForwardHTTPBody(t.Context(), ingress)
	tierBody, tierView, tierReq := ingress, newOpenAIRequestView(ingress), noTier
	_, err = tierless.reencodeRequestBody(noTier, &tierBody, &tierView, &tierReq)
	require.NoError(t, err)
	tierLineage := tierBody
	tierless.park(&tierBody, &tierView, &tierLineage)
	require.True(t, tierless.parked)
	require.Nil(t, tierless.serviceTier(tierBody), "没有 service_tier 时与按正文读一致为 nil")

	mapped := map[string]any{"a": 1}
	workspace.releaseRequestMap(&mapped, newOpenAIRequestView(other), body)
	require.NotNil(t, mapped, "请求视图与正文不是同一版本时保留对象树")
	workspace.releaseRequestMap(&mapped, otherView, other)
	require.Nil(t, mapped, "同一版本时放下对象树")
	mapped = map[string]any{"a": 1}
	nilWorkspace.releaseRequestMap(&mapped, otherView, other)
	require.NotNil(t, mapped, "nil 工作区不放下对象树")
}

// TestOfficialForwardHTTPBodyReleasesCopiesDuringUpstreamAttempt 在完整 Forward 上测量上游 attempt 期间
// （编译与压缩已完成、请求正在发送）的存活堆：改造前 Forward 此时仍持有整段对象树与重编码正文，改造后
// 只剩压缩输出。
func TestOfficialForwardHTTPBodyReleasesCopiesDuringUpstreamAttempt(t *testing.T) {
	gin.SetMode(gin.TestMode)
	source := buildOfficialEgressMemoryProfileBody(t, 8<<20)
	measure := func(disabled bool) (baseline uint64, during uint64) {
		tc := officialForwardBodyCase{
			name: "live", body: func(*testing.T) []byte { return source },
			context: func(_ *testing.T, body []byte) *gin.Context {
				return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			},
			onBusiness: func(*http.Request) {
				runtime.GC()
				var stats runtime.MemStats
				runtime.ReadMemStats(&stats)
				during = stats.HeapAlloc
			},
		}
		runtime.GC()
		var stats runtime.MemStats
		runtime.ReadMemStats(&stats)
		baseline = stats.HeapAlloc
		outcome := officialForwardBodyRun(t, tc, source, disabled)
		require.NoError(t, outcome.err)
		return baseline, during
	}
	legacyBase, legacyDuring := measure(true)
	currentBase, currentDuring := measure(false)
	legacyExtra := float64(legacyDuring) - float64(legacyBase)
	currentExtra := float64(currentDuring) - float64(currentBase)
	size := float64(len(source))
	t.Logf("正文 %.1f MiB：上游 attempt 期间存活增量 %.2f → %.2f 倍正文", size/(1<<20), legacyExtra/size, currentExtra/size)
	require.Less(t, currentExtra, 1.3*size, "attempt 期间只应剩压缩输出与少量结构")
	require.Greater(t, legacyExtra-currentExtra, 1.8*size, "改造后应少掉对象树与重编码正文约两倍正文")
}
