package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"math/rand"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"unsafe"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M1 第三项：Finalizer 的定型结果按顶层成员交接给编译器，不再拼成整段正文。
// 成员依次写出必须与整段拼接编码逐字节一致（含错误），请求体任何读取方读到的字节与改造前
// 相同，经 Forward attempt 编译后的上游 wire 字节与按字节交接完全一致，且全程不物化整段正文。

// membersHandoffCopy 深拷贝 payload，两种编码各自使用独立副本，互不影响。
func membersHandoffCopy(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	copied, ok := spliceDeepCopy(payload).(map[string]any)
	require.True(t, ok)
	return copied
}

func membersHandoffCompare(t *testing.T, name string, payload map[string]any, order []string, original []byte) {
	t.Helper()
	legacy, legacyErr := marshalOfficialOrderedJSONObjectPreservingRaw(membersHandoffCopy(t, payload), order, original)
	members, currentErr := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(
		membersHandoffCopy(t, payload), order, original, nil,
	)
	if decodeSharingErrorParity(t, name, legacyErr, currentErr) {
		require.Equal(t, legacyErr.Error(), currentErr.Error(), "%s：错误文本必须一致", name)
		return
	}
	require.Equal(t, string(legacy), string(officialegress.AppendJSONObjectMembers(nil, members)),
		"%s：成员依次写出必须与整段拼接编码逐字节一致", name)
	require.Equal(t, len(legacy), officialegress.JSONObjectMembersLength(members), name)
}

func TestMarshalOfficialJSONObjectMembersMatchesByteMarshal(t *testing.T) {
	original := []byte(`{"model":"gpt-5.6-luna","input":[{"type":"message","content":"x"}],"z":{"b":1,"a":2},"a<b":"html","dup":1,"dup":2}`)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	membersHandoffCompare(t, "unchanged", payload, nil, original)
	membersHandoffCompare(t, "ordered", payload, []string{"input", "absent", "model"}, original)
	changed := membersHandoffCopy(t, payload)
	changedInput, ok := changed["input"].([]any)
	require.True(t, ok)
	changed["input"] = append(changedInput, map[string]any{"type": "message", "content": "新"})
	changed["new"] = json.RawMessage(`{"k":1}`)
	changed["num"] = 12.5
	delete(changed, "z")
	membersHandoffCompare(t, "changed", changed, []string{"model", "input"}, original)
	membersHandoffCompare(t, "no-original", changed, []string{"model"}, nil)
	membersHandoffCompare(t, "invalid-original", changed, nil, []byte(`{"model":`))
	membersHandoffCompare(t, "unsupported-value", map[string]any{"a": 1, "bad": make(chan int)}, nil, original)

	rng := rand.New(rand.NewSource(20261002))
	for round := 0; round < 600; round++ {
		size := 1 + rng.Intn(6)
		document := make(map[string]any, size)
		for i := 0; i < size; i++ {
			document[spliceRandomKey(rng)] = spliceRandomValue(rng, 1)
		}
		randomOriginal, err := json.Marshal(document)
		require.NoError(t, err)
		if rng.Intn(3) == 0 {
			var pretty bytes.Buffer
			require.NoError(t, json.Indent(&pretty, randomOriginal, "", "  "))
			randomOriginal = pretty.Bytes()
		}
		randomPayload, err := decodeOfficialJSONObjectUseNumber(randomOriginal)
		require.NoError(t, err)
		spliceRandomMutate(rng, randomPayload)
		var order []string
		if rng.Intn(2) == 0 {
			order = []string{"type", "model", "a", "absent", "b"}
		}
		membersHandoffCompare(t, "random#"+strconv.Itoa(round), randomPayload, order, randomOriginal)
	}
}

func TestMarshalOfficialJSONObjectMembersReferenceUnchangedValues(t *testing.T) {
	original := officialBodySharingLargeRequestBody(t, 8, 64*1024)
	payload, err := decodeOfficialJSONObjectUseNumber(original)
	require.NoError(t, err)
	delete(payload, "client_metadata")
	payload["parallel_tool_calls"] = true
	members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, nil, original, nil)
	require.NoError(t, err)
	begin := uintptr(unsafe.Pointer(unsafe.SliceData(original)))
	end := begin + uintptr(len(original))
	for _, member := range members {
		if member.Name != "input" {
			continue
		}
		pointer := uintptr(unsafe.Pointer(unsafe.SliceData(member.Value)))
		require.True(t, pointer >= begin && pointer < end, "未改动的 input 必须直接引用原正文区间")
		return
	}
	t.Fatal("成员中缺少 input")
}

func TestFinalizeOfficialOpenAIHTTPBodyMembersMatchesBytes(t *testing.T) {
	rng := rand.New(rand.NewSource(20261003))
	defaultsVariants := []officialOpenAIReasoningDefaults{
		{},
		{Effort: "high", Summary: "auto", SupportsSummary: true, Known: true},
	}
	compared := 0
	for name, body := range decodeSharingBodyFinalizeInputs(t, rng) {
		if _, err := captureOfficialOpenAIHTTPBodyContract(body); err != nil {
			continue
		}
		for _, compact := range []bool{false, true} {
			for _, lite := range []bool{false, true} {
				for defaultsIndex, defaults := range defaultsVariants {
					options := officialOpenAIHTTPBodyOptions{
						IsCompact: compact, UseResponsesLite: lite, SupportsParallelTools: true,
						ProfileMode: officialClientProfileModeActive,
					}
					label := name + " compact=" + strconv.FormatBool(compact) + " lite=" + strconv.FormatBool(lite) +
						" defaults=" + strconv.Itoa(defaultsIndex)
					legacyContract, err := captureOfficialOpenAIHTTPBodyContract(body)
					require.NoError(t, err)
					legacy, legacyModified, legacyErr := finalizeOfficialOpenAIHTTPBody(
						body, legacyContract, officialOpenAIHTTPIdentity{}, defaults, options,
					)
					currentContract, err := captureOfficialOpenAIHTTPBodyContract(body)
					require.NoError(t, err)
					payload, index, err := decodeOfficialJSONObjectSharingBody(body)
					require.NoError(t, err)
					members, currentModified, currentErr := finalizeOfficialOpenAIHTTPBodyPayloadMembers(
						payload, body, index, currentContract, defaults, options,
					)
					if decodeSharingErrorParity(t, label, legacyErr, currentErr) {
						require.Equal(t, legacyErr.Error(), currentErr.Error(), label)
						continue
					}
					require.Equal(t, legacyModified, currentModified, label)
					if !currentModified {
						require.Nil(t, members, label)
						require.Equal(t, string(body), string(legacy), label)
						continue
					}
					require.Equal(t, string(legacy), string(officialegress.AppendJSONObjectMembers(nil, members)),
						"%s：成员写出必须与改造前定型正文逐字节一致", label)
					compared++
				}
			}
		}
	}
	require.Greater(t, compared, 500, "差分样本过少，夹具可能失效")
}

func membersHandoffBuildRequest(t *testing.T, body []byte) *http.Request {
	t.Helper()
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
		openAIUpstreamRequestPlan{
			IsStream: true, PromptCacheKey: testOfficialOpenAISessionID,
			IsCodexCLI: true, OfficialEgressBodyContract: contract,
		},
	)
	require.NoError(t, err)
	return req
}

// TestOfficialEgressHTTPRequestMembersBodyReadsFinalBytes 锁定按成员装配的请求体对任何读取方
// （Body 直读、GetBody 重放、readOfficialEgressRequestBodyBytes）都给出与改造前定型正文相同的
// 字节，ContentLength 一致。
func TestOfficialEgressHTTPRequestMembersBodyReadsFinalBytes(t *testing.T) {
	bodies := map[string][]byte{"large": officialBodySharingLargeRequestBody(t, 8, 8*1024)}
	for _, explicit := range []bool{false, true} {
		for _, tool := range []bool{false, true} {
			bodies["official explicit="+strconv.FormatBool(explicit)+" tool="+strconv.FormatBool(tool)] =
				newOfficialOpenAIHTTPTestBody(t, true, explicit, tool)
		}
	}
	for name, body := range bodies {
		req := membersHandoffBuildRequest(t, body)
		content := officialEgressJSONMembersFromRequest(req)
		require.NotNil(t, content, "%s：官方正文被定型改写后必须按成员装配", name)
		egressContext, ok := OfficialEgressContextFromContext(req.Context())
		require.True(t, ok)
		contract, err := captureOfficialOpenAIHTTPBodyContract(body)
		require.NoError(t, err)
		expected, modified, err := finalizeOfficialOpenAIHTTPBody(
			body, contract, officialOpenAIHTTPIdentity{},
			officialOpenAIReasoningDefaultsFromContext(egressContext),
			officialOpenAIHTTPBodyOptions{
				UseResponsesLite:      egressContext.responsesLite,
				SupportsParallelTools: egressContext.parallelTools,
				ProfileMode:           egressContext.ProfileMode(),
			},
		)
		require.NoError(t, err)
		require.True(t, modified, name)
		direct, err := io.ReadAll(req.Body)
		require.NoError(t, err)
		replayed := mustReadRequestBody(t, req)
		read, err := readOfficialEgressRequestBodyBytes(req)
		require.NoError(t, err)
		require.Equal(t, string(expected), string(direct), "%s：Body 直读必须得到改造前定型正文", name)
		require.Equal(t, string(expected), string(replayed), "%s：GetBody 重放必须得到改造前定型正文", name)
		require.Equal(t, string(expected), string(read), name)
		require.Equal(t, int64(len(expected)), req.ContentLength, name)
	}
}

// membersHandoffExecute 用独立的 invocation 执行一次 Forward HTTP attempt，返回上游收到的请求与正文。
func membersHandoffExecute(t *testing.T, request *http.Request) (*http.Request, []byte) {
	t.Helper()
	upstream := &openAIForwardPlanUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		newOpenAIForwardPlanGuard(t), upstream, officialegress.ExecutorID("members-handoff"),
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)
	account := &Account{
		ID: 716, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"chatgpt_account_id": "acct-members-handoff"},
	}
	plan, err := newOfficialCodexResponseForwardPlan(context.Background(), officialCodexResponseForwardPlanInput{
		Runtime: runtimeState, Account: account,
		PrimarySinkID: officialEgressSinkResponsesForward,
		InvocationID:  "members-handoff-invocation",
		PolicyID:      "members-handoff", PolicySource: "test",
		AttemptBudget: 1,
	})
	require.NoError(t, err)
	response, err := plan.ExecuteHTTPRequest(context.Background(), request, officialCodexEndpointResponsesHTTP)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	requests := upstream.snapshot()
	require.Len(t, requests, 1)
	upstreamRequest := requests[0]
	require.NotNil(t, upstreamRequest.GetBody)
	reader, err := upstreamRequest.GetBody()
	require.NoError(t, err)
	wire, err := io.ReadAll(reader)
	require.NoError(t, err)
	return upstreamRequest, wire
}

// TestOfficialForwardHTTPAttemptMembersMatchesBytesOnWire 端到端锁定：同一终态正文按成员交接与
// 按整段字节交接，经 Executor 编译、签名后发往上游的正文与 Header 逐字节一致。
func TestOfficialForwardHTTPAttemptMembersMatchesBytesOnWire(t *testing.T) {
	large := strings.Repeat(`{"type":"reasoning","summary":[],"encrypted_content":"`+strings.Repeat("QUJD", 2048)+`"},`, 32)
	base := `{"model":"gpt-5.6-luna","input":[` + large + `{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],` +
		`"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`
	for name, body := range map[string][]byte{
		"large":      []byte(base),
		"breakpoint": []byte(strings.Replace(base, `"text":"hi"`, `"text":"hi","prompt_cache_breakpoint":{"ttl":"5m"}`, 1)),
	} {
		payload, err := decodeOfficialJSONObjectUseNumber(body)
		require.NoError(t, err)
		members, err := marshalOfficialOrderedJSONObjectMembersPreservingRawWithIndex(payload, nil, body, nil)
		require.NoError(t, err)
		materialized := officialegress.AppendJSONObjectMembers(nil, members)

		for _, compressed := range []bool{false, true} {
			label := name + " zstd=" + strconv.FormatBool(compressed)
			membersRequest, err := http.NewRequest(http.MethodPost, chatgptCodexURL, nil)
			require.NoError(t, err)
			resetOfficialEgressRequestBodyMembers(membersRequest, members)
			membersRequest.Header.Set("Authorization", "Bearer members-handoff-token")
			bytesRequest, err := http.NewRequest(http.MethodPost, chatgptCodexURL, nil)
			require.NoError(t, err)
			resetOfficialEgressRequestBody(bytesRequest, materialized)
			bytesRequest.Header.Set("Authorization", "Bearer members-handoff-token")
			if compressed {
				// 语义请求声明 zstd 时编译器按画像压缩定型正文，覆盖压缩路径。
				membersRequest.Header.Set("Content-Encoding", "zstd")
				bytesRequest.Header.Set("Content-Encoding", "zstd")
			}

			membersUpstream, membersWire := membersHandoffExecute(t, membersRequest)
			bytesUpstream, bytesWire := membersHandoffExecute(t, bytesRequest)
			require.Equal(t, bytesWire, membersWire, "%s：上游收到的正文必须逐字节一致", label)
			require.Equal(t, bytesUpstream.ContentLength, membersUpstream.ContentLength, label)
			require.Equal(t, bytesUpstream.Header, membersUpstream.Header, "%s：上游 Header 必须一致", label)
			if compressed {
				require.True(t, strings.EqualFold(membersUpstream.Header.Get("Content-Encoding"), "zstd"),
					"%s：压缩变体必须真正走到 zstd 分支", label)
			}
			content := officialEgressJSONMembersFromRequest(membersRequest)
			require.NotNil(t, content)
			require.Nil(t, content.bytes, "%s：Forward attempt 全程不得物化按成员装配的终态正文", label)
		}
	}
}

func TestOfficialEgressHTTPFinalizerHandsOffMembersWithoutMaterializing(t *testing.T) {
	body := officialBodySharingLargeRequestBody(t, 64, 128*1024)
	require.Greater(t, len(body), 8*1024*1024)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	var req *http.Request
	allocated := testMeasureAllocatedBytes(t, func() {
		c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		req, err = (&OpenAIGatewayService{}).buildUpstreamRequest(
			c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
			openAIUpstreamRequestPlan{
				IsStream: true, PromptCacheKey: testOfficialOpenAISessionID,
				IsCodexCLI: true, OfficialEgressBodyContract: contract,
			},
		)
		require.NoError(t, err)
	})
	runtime.KeepAlive(req)
	content := officialEgressJSONMembersFromRequest(req)
	require.NotNil(t, content)
	require.Nil(t, content.bytes, "终态修正不得物化整段正文")
	t.Logf("正文 %.1f MiB：终态修正整体分配 %.2f MiB（%.3f 倍正文）",
		float64(len(body))/(1<<20), float64(allocated)/(1<<20), float64(allocated)/float64(len(body)))
	require.Less(t, allocated, uint64(len(body))/4,
		"终态修正分配 %d 字节，不得再拼出整段正文（%d 字节）", allocated, len(body))
}
