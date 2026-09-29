package service

import (
	"bytes"
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"reflect"
	"strconv"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M2-c：同一版本的正文在一次转发里只扫描一遍建立索引，各使用方共享。
// 只登记节点的索引补齐结构摘要后必须与现场建立的完整索引逐项相同；各“带索引”入口与原入口
// 结果（含错误文本）完全一致；Lite 归一化、契约捕获、对象树解码、保序重编码、compaction 规整与
// Finalizer 不再为同一正文重复扫描。

// forwardBodyIndexRequireEqual 比较两份索引的全部可观察状态（节点、摘要、复合值索引）。
func forwardBodyIndexRequireEqual(t *testing.T, name string, want, got *officialJSONRawIndex) {
	t.Helper()
	require.Equal(t, want.root, got.root, name)
	require.Equal(t, want.seed, got.seed, name)
	require.Equal(t, want.skipDigest, got.skipDigest, name)
	require.Len(t, got.nodes, len(want.nodes), name)
	for i := range want.nodes {
		w, g := want.nodes[i], got.nodes[i]
		require.Equal(t, w.kind, g.kind, "%s：节点 %d", name, i)
		require.Equal(t, w.slowPath, g.slowPath, "%s：节点 %d", name, i)
		require.Equal(t, w.start, g.start, "%s：节点 %d", name, i)
		require.Equal(t, w.end, g.end, "%s：节点 %d", name, i)
		require.Equal(t, w.hash, g.hash, "%s：节点 %d 摘要", name, i)
		require.Equal(t, w.members, g.members, "%s：节点 %d", name, i)
		require.Equal(t, w.items, g.items, "%s：节点 %d", name, i)
	}
	require.Equal(t, want.byHash, got.byHash, "%s：复合值索引必须一致", name)
}

func forwardBodyIndexEnsureDigestsCompare(t *testing.T, name string, body []byte) {
	t.Helper()
	full, fullErr := buildOfficialJSONRawIndex(body)
	partial, partialErr := buildOfficialJSONRawIndexForDecode(body)
	if fullErr != nil || partialErr != nil {
		require.Error(t, fullErr, name)
		require.Error(t, partialErr, name)
		require.Equal(t, fullErr.Error(), partialErr.Error(), name)
		return
	}
	partial.ensureDigests()
	forwardBodyIndexRequireEqual(t, name, full, partial)
	partial.ensureDigests()
	forwardBodyIndexRequireEqual(t, name+"（重复补齐）", full, partial)
}

func TestOfficialJSONRawIndexEnsureDigestsMatchesFullIndex(t *testing.T) {
	for i, fixture := range officialJSONDecodeFixtures {
		forwardBodyIndexEnsureDigestsCompare(t, "valid#"+strconv.Itoa(i), []byte(fixture))
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		forwardBodyIndexEnsureDigestsCompare(t, "invalid#"+strconv.Itoa(i), []byte(fixture))
	}
	forwardBodyIndexEnsureDigestsCompare(t, "official", newOfficialOpenAIHTTPTestBody(t, true, true, true))
	forwardBodyIndexEnsureDigestsCompare(t, "memory_profile", buildOfficialEgressMemoryProfileBody(t, 256<<10))
	forwardBodyIndexEnsureDigestsCompare(t, "escapes", []byte(`{"aé":"x\"y\\z😀\ud800","b":[1,-0,1e3,true,false,null,{},[]],"aé":{"k":"中"}}`))
	rng := rand.New(rand.NewSource(20260929))
	for round := 0; round < 2000; round++ {
		body := []byte(decodeRandomJSON(rng, 0))
		forwardBodyIndexEnsureDigestsCompare(t, "random#"+strconv.Itoa(round), body)
		forwardBodyIndexEnsureDigestsCompare(t, "mutated#"+strconv.Itoa(round), decodeMutate(rng, body))
		forwardBodyIndexEnsureDigestsCompare(t, "request#"+strconv.Itoa(round), decodeSharingRandomBody(rng))
	}
}

// TestOfficialJSONRawIndexUpgradedIndexSplicesIdentically 锁定补齐后的索引用于拼接编码时与现场建立
// 的完整索引逐字节相同（含成员形态），覆盖按内容查找与数组项匹配。
func TestOfficialJSONRawIndexUpgradedIndexSplicesIdentically(t *testing.T) {
	rng := rand.New(rand.NewSource(20260930))
	compared := 0
	for round := 0; round < 800; round++ {
		original := decodeSharingRandomBody(rng)
		payload, err := decodeOfficialJSONObjectUseNumber(original)
		require.NoError(t, err)
		mutated := membersHandoffCopy(t, payload)
		if input, ok := mutated["input"].([]any); ok && len(input) > 1 {
			mutated["input"] = append([]any{input[len(input)-1]}, input[:len(input)-1]...)
		}
		mutated["moved"] = payload["input"]
		want, wantErr := marshalOfficialJSONObjectPreservingOrderAndRaw(mutated, original)
		index, err := buildOfficialJSONRawIndexForDecode(original)
		require.NoError(t, err)
		index.ensureDigests()
		got, gotErr := marshalOfficialJSONObjectPreservingOrderAndRawWithIndex(mutated, original, index)
		if decodeSharingErrorParity(t, "splice#"+strconv.Itoa(round), wantErr, gotErr) {
			continue
		}
		require.Equal(t, string(want), string(got), "splice#%d", round)
		compared++
	}
	require.Greater(t, compared, 700)
}

func TestOfficialForwardBodyWithIndexEntriesMatchOriginals(t *testing.T) {
	bodies := map[string][]byte{
		"official":        newOfficialOpenAIHTTPTestBody(t, true, true, true),
		"compaction":      []byte(`{"input":[{"type":"compaction_trigger"},{"type":"message","content":"x"}],"model":"m"}`),
		"compaction_done": []byte(`{"input":[{"type":"message","content":"x"},{"type":"compaction_trigger"}]}`),
		"empty_object":    []byte(`{}`),
		"null":            []byte(`null`),
		"array":           []byte(`[1]`),
	}
	rng := rand.New(rand.NewSource(20261001))
	for round := 0; round < 600; round++ {
		bodies["random_"+strconv.Itoa(round)] = decodeSharingRandomBody(rng)
	}
	for i, fixture := range officialJSONDecodeInvalidFixtures {
		bodies["invalid_"+strconv.Itoa(i)] = []byte(fixture)
	}
	for name, body := range bodies {
		index, indexErr := buildOfficialJSONRawIndexForDecode(body)
		if indexErr != nil {
			index = nil
		}

		wantContract, wantErr := captureOfficialOpenAIHTTPBodyContract(body)
		gotContract, gotErr := captureOfficialOpenAIHTTPBodyContractWithIndex(body, index)
		if !decodeSharingErrorParity(t, name, wantErr, gotErr) {
			require.True(t, reflect.DeepEqual(wantContract, gotContract), "%s：契约捕获必须一致", name)
		} else {
			require.Equal(t, wantErr.Error(), gotErr.Error(), name)
		}

		wantBody, wantChanged, wantErr := NormalizeCompactionTriggerInputOrder(body)
		gotBody, gotChanged, gotErr := normalizeCompactionTriggerInputOrderWithIndex(body, index)
		if !decodeSharingErrorParity(t, name, wantErr, gotErr) {
			require.Equal(t, wantChanged, gotChanged, name)
			require.Equal(t, string(wantBody), string(gotBody), name)
		} else {
			require.Equal(t, wantErr.Error(), gotErr.Error(), name)
		}

		wantPayload, wantIndex, wantErr := decodeOfficialJSONObjectSharingBody(body)
		var sharedIndex *officialJSONRawIndex
		if index != nil {
			sharedIndex, _ = buildOfficialJSONRawIndexForDecode(body)
		}
		gotPayload, gotIndex, gotErr := decodeOfficialJSONObjectSharingBodyWithIndex(body, sharedIndex)
		if !decodeSharingErrorParity(t, name, wantErr, gotErr) {
			require.True(t, reflect.DeepEqual(wantPayload, gotPayload), "%s：共享解码结果必须一致", name)
			require.Equal(t, wantIndex == nil, gotIndex == nil, name)
			if wantIndex != nil {
				forwardBodyIndexRequireEqual(t, name, wantIndex, gotIndex)
			}
		} else {
			require.Equal(t, wantErr.Error(), gotErr.Error(), name)
		}
	}
}

// legacyNormalizeOpenAIResponsesLiteToolsPayloadTwoScans 是 M2-c 之前的 Lite 归一化：预检与建树扫描一遍，拼接
// 编码再扫描一遍。更早的整段解码实现见 openai_responses_lite_legacy_reference_test.go。
func legacyNormalizeOpenAIResponsesLiteToolsPayloadTwoScans(body []byte) ([]byte, bool, error) {
	var requestBody map[string]any
	index, indexErr := buildOfficialJSONRawIndexForDecode(body)
	if indexErr == nil && index.nodes[index.root].kind == officialJSONRawKindObject {
		if openAIResponsesLiteAlreadyNormalized(index) {
			return body, false, nil
		}
		requestBody = index.decodeObject(index.root)
	} else {
		var err error
		if requestBody, err = decodeOfficialJSONObjectUseNumber(body); err != nil {
			return body, false, fmt.Errorf("decode responses Lite request body: %w", err)
		}
	}
	changed, err := normalizeOpenAIResponsesLiteTools(requestBody)
	if err != nil || !changed {
		return body, false, err
	}
	rebuilt, err := marshalOfficialJSONObjectPreservingOrderAndRaw(requestBody, body)
	if err != nil {
		return body, false, fmt.Errorf("encode responses Lite request body: %w", err)
	}
	return rebuilt, true, nil
}

func TestNormalizeOpenAIResponsesLiteToolsPayloadSingleScanMatchesLegacy(t *testing.T) {
	bodies := map[string][]byte{
		"official":        newOfficialOpenAIHTTPTestBody(t, true, true, true),
		"memory_profile":  buildOfficialEgressMemoryProfileBody(t, 64<<10),
		"namespace_tools": []byte(`{"tools":[{"type":"namespace","name":"ns","tools":[{"type":"function","name":"f"}]},{"type":"function","name":"g"}],"input":[{"type":"message","content":"x"}],"reasoning":{"effort":"high"}}`),
		"already":         []byte(`{"reasoning":{"context":"all_turns"},"parallel_tool_calls":false,"tools":[{"type":"function","name":"f"}]}`),
		"bad_parallel":    []byte(`{"parallel_tool_calls":"x"}`),
		"bad_tool":        []byte(`{"tools":[{"type":"web_search"}]}`),
		"invalid":         []byte(`{"tools":[`),
		"null":            []byte(`null`),
	}
	rng := rand.New(rand.NewSource(20261002))
	for round := 0; round < 800; round++ {
		bodies["random_"+strconv.Itoa(round)] = decodeSharingRandomBody(rng)
		bodies["mutated_"+strconv.Itoa(round)] = decodeMutate(rng, decodeSharingRandomBody(rng))
	}
	for name, body := range bodies {
		wantBody, wantChanged, wantErr := legacyNormalizeOpenAIResponsesLiteToolsPayloadTwoScans(body)
		gotBody, gotChanged, gotErr := normalizeOpenAIResponsesLiteToolsPayload(body)
		if decodeSharingErrorParity(t, name, wantErr, gotErr) {
			require.Equal(t, wantErr.Error(), gotErr.Error(), name)
			continue
		}
		require.Equal(t, wantChanged, gotChanged, name)
		require.True(t, bytes.Equal(wantBody, gotBody), "%s：Lite 归一化输出必须逐字节一致", name)
	}
}

func TestNormalizeOpenAIResponsesLiteToolsPayloadScansOnce(t *testing.T) {
	body := buildOfficialEgressMemoryProfileBody(t, 8<<20)
	_, changed, err := normalizeOpenAIResponsesLiteToolsPayload(body)
	require.NoError(t, err)
	require.True(t, changed, "夹具需要走改写路径")
	current := testMeasureAllocatedBytes(t, func() {
		_, _, err := normalizeOpenAIResponsesLiteToolsPayload(body)
		require.NoError(t, err)
	})
	legacy := testMeasureAllocatedBytes(t, func() {
		_, _, err := legacyNormalizeOpenAIResponsesLiteToolsPayloadTwoScans(body)
		require.NoError(t, err)
	})
	oneScan := testMeasureAllocatedBytes(t, func() {
		_, err := buildOfficialJSONRawIndexForDecode(body)
		require.NoError(t, err)
	})
	t.Logf("正文 %.1f MiB：Lite 归一化改写路径 %.1f → %.1f MiB（单次扫描约 %.1f MiB）",
		float64(len(body))/(1<<20), float64(legacy)/(1<<20), float64(current)/(1<<20), float64(oneScan)/(1<<20))
	require.Less(t, current+oneScan*3/4, legacy, "改写路径必须少扫描一遍正文")
}

// TestNormalizeOpenAIResponsesLiteToolsPayloadTreeSharesBody 锁定 Lite 归一化改写路径的对象树不再复制长字符串：
// 与两遍扫描的改造前实现相比，至少少分配约 0.8 倍正文（树中字符串的副本）。
func TestNormalizeOpenAIResponsesLiteToolsPayloadTreeSharesBody(t *testing.T) {
	body := buildOfficialEgressMemoryProfileBody(t, 8<<20)
	current := testMeasureAllocatedBytes(t, func() {
		_, changed, err := normalizeOpenAIResponsesLiteToolsPayload(body)
		require.NoError(t, err)
		require.True(t, changed)
	})
	legacy := testMeasureAllocatedBytes(t, func() {
		_, _, err := legacyNormalizeOpenAIResponsesLiteToolsPayloadTwoScans(body)
		require.NoError(t, err)
	})
	t.Logf("正文 %.1f MiB：Lite 归一化改写路径 %.1f → %.1f MiB", float64(len(body))/(1<<20),
		float64(legacy)/(1<<20), float64(current)/(1<<20))
	require.Less(t, current+uint64(len(body))*8/10, legacy, "对象树不得再复制长字符串")
}

func TestOfficialForwardHTTPBodyIndexCacheByBodyIdentity(t *testing.T) {
	var nilBody *officialForwardHTTPBody
	require.Nil(t, nilBody.indexFor([]byte(`{}`)), "nil 工作区不提供索引")

	_, workspace := newOfficialForwardHTTPBody(context.Background(), nil)
	require.NotNil(t, workspace)
	body := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"message","content":"x"}]}`)
	first := workspace.indexFor(body)
	require.NotNil(t, first)
	require.Same(t, first, workspace.indexFor(body), "同一版本正文必须复用索引")
	copied := append([]byte(nil), body...)
	second := workspace.indexFor(copied)
	require.NotSame(t, first, second, "内容相同但不是同一底层数组时按新版本重建")
	require.Same(t, second, workspace.indexFor(copied))
	require.Nil(t, workspace.indexFor([]byte(`{"broken":`)), "非法正文不缓存")
	require.Nil(t, workspace.index, "非法正文要清空旧缓存，不再引用旧正文")

	disabledCtx, disabled := newOfficialForwardHTTPBody(withOfficialForwardHTTPBodyDisabled(context.Background()), nil)
	require.Nil(t, disabled)
	require.Nil(t, officialForwardHTTPBodyFromContext(disabledCtx))
	enabledCtx, enabled := newOfficialForwardHTTPBody(context.Background(), nil)
	require.Same(t, enabled, officialForwardHTTPBodyFromContext(enabledCtx))
	require.Same(t, enabled, officialForwardHTTPBodyFromContext(context.WithoutCancel(enabledCtx)),
		"上游 ctx 分离后仍能取回同一工作区")
}

// TestOfficialForwardHTTPBodyNilWorkspaceDelegatesToOriginals 锁定非官方出站路径：nil 工作区的每个
// 方法都与改造前的函数结果完全一致（含错误文本）。
func TestOfficialForwardHTTPBodyNilWorkspaceDelegatesToOriginals(t *testing.T) {
	var workspace *officialForwardHTTPBody
	bodies := [][]byte{
		newOfficialOpenAIHTTPTestBody(t, true, true, true),
		[]byte(`{"input":[{"type":"compaction_trigger"},{"type":"message","content":"x"}]}`),
		[]byte(`{"broken":`), []byte(`null`), nil,
	}
	for i, body := range bodies {
		name := "body#" + strconv.Itoa(i)
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		wantContract, wantErr := captureOfficialOpenAIHTTPBodyContractForRequest(c, body)
		gotContract, gotErr := workspace.captureContract(c, body)
		require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), name)
		require.True(t, reflect.DeepEqual(wantContract, gotContract), name)

		view := newOpenAIRequestView(body)
		wantMap, wantErr := view.Decode(c)
		gotMap, gotErr := workspace.decodeRequestView(c, view)
		require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), name)
		require.True(t, reflect.DeepEqual(wantMap, gotMap), name)

		wantBody, wantChanged, wantErr := NormalizeCompactionTriggerInputOrder(body)
		gotBody, gotChanged, gotErr := workspace.normalizeCompactionTriggerInputOrder(body)
		require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), name)
		require.Equal(t, wantChanged, gotChanged, name)
		require.Equal(t, string(wantBody), string(gotBody), name)

		if wantMap != nil {
			want, wantErr := marshalOfficialJSONObjectPreservingOrderAndRaw(wantMap, body)
			got := body
			gotView := view
			gotReqBody := gotMap
			returned, gotErr := workspace.reencodeRequestBody(gotMap, &got, &gotView, &gotReqBody)
			require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), name)
			require.Equal(t, string(want), string(got), name)
			require.Equal(t, string(want), string(returned), "%s：nil 工作区返回的正文与写回的正文相同", name)
			require.Equal(t, view.body, gotView.body, "%s：nil 工作区不改动请求视图", name)
			require.NotNil(t, gotReqBody, "%s：nil 工作区不放下对象树", name)
		}
	}
}

// TestOfficialForwardHTTPBodyScansEachBodyVersionOnce 在完整 Forward 上验证同一版本正文只扫描一次：
// 测量形态经 Lite 归一化后的正文 L 供契约捕获、对象树解码与保序重编码共用一次扫描，重编码结果 D 供
// compaction 规整与 Finalizer 共用一次扫描（Lite 归一化自身对入站正文的一次扫描不经工作区）。
func TestOfficialForwardHTTPBodyScansEachBodyVersionOnce(t *testing.T) {
	source := buildOfficialEgressMemoryProfileBody(t, 1<<20)
	var workspace *officialForwardHTTPBody
	tc := officialForwardBodyCase{
		name: "scan_count", body: func(*testing.T) []byte { return source },
		context: func(_ *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		},
		onBusiness: func(req *http.Request) { workspace = officialForwardHTTPBodyFromContext(req.Context()) },
	}
	outcome := officialForwardBodyRun(t, tc, source, false)
	require.NoError(t, outcome.err)
	require.NotNil(t, workspace, "上游请求的 ctx 必须带着本次调用的工作区")
	require.Equal(t, 2, workspace.scans, "L 与 D 两个正文版本各只扫描一次")
}
