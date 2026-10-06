package officialegress

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func TestJSONObjectValueSegmentsCompileWithoutMaterializingHistory(t *testing.T) {
	for _, tokenBoundaries := range []bool{false, true} {
		name := "whole_items"
		if tokenBoundaries {
			name = "token_boundaries"
		}
		t.Run(name, func(t *testing.T) {
			testJSONObjectValueSegmentsCompileWithoutMaterializingHistory(t, tokenBoundaries)
		})
	}
}

func testJSONObjectValueSegmentsCompileWithoutMaterializingHistory(t *testing.T, tokenBoundaries bool) {
	bundle, endpoint := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	base := staticClosureSemanticBody(t, endpoint.template.endpoint)
	members := membersFromDocument(t, base)
	var changed bool
	for i := range members {
		if members[i].Name != "input" {
			continue
		}
		members[i].Value = []byte(`"被分段值覆盖"`)
		members[i].ValueSegments = [][]byte{
			[]byte("["),
			[]byte(`{"role":"developer","content":[{"type":"input_text","text":"显式指令"}]}`),
			[]byte(","),
			[]byte(`{"type":"reasoning","summary":[],"encrypted_content":"` + strings.Repeat("QUJD", 64*1024) + `"}`),
			[]byte(","),
			[]byte(`{"role":"user", "content":[{"type":"input_text","text":"原始\u0061\/内容"}]}`),
			[]byte("]"),
		}
		if tokenBoundaries {
			members[i].ValueSegments = [][]byte{
				[]byte("["),
				[]byte(`{"role":`), []byte(`"developer"`), []byte(`,"content":[{"type":"input_text","text":`),
				[]byte(`"显式指令"`), []byte(`}]},`),
				[]byte(`{"type":"reasoning","summary":[],"encrypted_content":`),
				[]byte(`"` + strings.Repeat("QUJD", 64*1024) + `"`), []byte(`},`),
				[]byte(`{"role":"user", "content":[{"type":"input_text","text":`),
				[]byte(`"原始\u0061\/内容"`), []byte(`}]}]`),
			}
		}
		changed = true
	}
	if !changed {
		t.Fatal("测试画像缺少 input 字段")
	}
	wantRaw := AppendJSONObjectMembers(nil, members)
	if len(wantRaw) != JSONObjectMembersLength(members) || !json.Valid(wantRaw) {
		t.Fatal("成员片段长度计算或编码无效")
	}
	input := NewSharedReplayableJSONObjectRequestBody(members)
	prepared, fields, err := PrepareOfficialCodexAttemptRequestBody("responses_http", input)
	if err != nil {
		t.Fatal(err)
	}
	inputField, ok := prepared.jsonDocument().field("input")
	if !ok || inputField.segments == nil || inputField.segments.bytes != nil {
		t.Fatal("按完整元素或 token 交接的 input 未保持分段")
	}
	wantPrepared, wantFields, err := PrepareOfficialCodexAttemptRequestBody("responses_http", NewSharedReplayableRequestBody(wantRaw))
	if err != nil {
		t.Fatal(err)
	}
	compile := func(body RequestBody, fields CompilerOwnedBodyFields) (CompiledExecution, []byte) {
		egressPlan := staticClosureEgressPlan(t, bundle, endpoint, staticClosureLegalTarget(endpoint.template), "members-compile")
		egressPlan.Body = body
		egressPlan.RoutingHint = fields.RoutingHint
		egressPlan.IdentityFacts.Conditions.CompressionEligible = true
		execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
		if err != nil {
			t.Fatal(err)
		}
		wire, _ := execution.request.body.ReplayableBytes()
		return execution, wire
	}
	wantExecution, wantWire := compile(wantPrepared, wantFields)
	gotExecution, gotWire := compile(prepared, fields)
	if !bytes.Equal(gotWire, wantWire) || gotExecution.CompiledDigest() != wantExecution.CompiledDigest() {
		t.Fatal("分段 input 改变了压缩正文或编译摘要")
	}
	if input.jsonObjectMembers().bytes != nil || inputField.segments.bytes != nil {
		t.Fatal("语义准备、编译或摘要期间不得物化整段 input 或正文")
	}
	if gotExecution.request.body.state.segmented == nil || gotExecution.request.body.state.segmented.bytes != nil {
		t.Fatal("正式压缩结果未保留分段，或在摘要计算时被物化")
	}
}

func TestJSONObjectValueSegmentsTokenScannerValidationAndAllocation(t *testing.T) {
	cases := map[string][][]byte{
		"nested_values": {[]byte(`[{"a":`), []byte(`1.2300e+04`), []byte(`,"b":[true,`), []byte(`false,null,{},`), []byte(`[]]}]`)},
		"empty_parts":   {nil, []byte(`[`), nil, []byte(`{"a":`), []byte(`"原文"`), []byte(`}`), nil, []byte(`]`), nil},
		"missing_colon": {[]byte(`[{"a"`), []byte(`1}]`)},
		"missing_comma": {[]byte(`[{"a":1`), []byte(`"b":2}]`)},
		"array_comma":   {[]byte(`[{"a":[1,`), []byte(`]}]`)},
		"object_comma":  {[]byte(`[{"a":1,`), []byte(`}]`)},
		"split_string":  {[]byte(`[{"a":"原`), []byte(`文"}]`)},
		"split_number":  {[]byte(`[{"a":1.2e`), []byte(`+04}]`)},
		"extra_value":   {[]byte(`[{}]`), []byte(`[]`)},
		"leading_space": {[]byte(` [`), []byte(`{}]`)},
	}
	for name, parts := range cases {
		t.Run(name, func(t *testing.T) {
			raw := bytes.Join(parts, nil)
			segments, accepted := newOrderedJSONArraySegments(parts)
			wantFastPath := name == "nested_values" || name == "empty_parts"
			if accepted != wantFastPath {
				t.Fatalf("token 边界或语法校验与预期不符：accepted=%t raw=%s", accepted, raw)
			}
			if accepted && (!json.Valid(raw) || segments.length != len(raw) || segments.bytes != nil) {
				t.Fatal("分段扫描接受非法 JSON、错误长度或物化了整个数组")
			}
			members := []JSONObjectMember{{Name: "input", QuotedName: []byte(`"input"`), ValueSegments: parts}}
			want, _, wantErr := PrepareOfficialCodexAttemptRequestBody("responses_http", NewSharedReplayableRequestBody(AppendJSONObjectMembers(nil, members)))
			got, _, gotErr := PrepareOfficialCodexAttemptRequestBody("responses_http", NewSharedReplayableJSONObjectRequestBody(members))
			if (gotErr == nil) != (wantErr == nil) {
				t.Fatalf("分段路径改变了字节入口语法判断：want=%v got=%v", wantErr, gotErr)
			}
			if gotErr == nil {
				wantBytes, _ := want.ReplayableBytes()
				gotBytes, _ := got.ReplayableBytes()
				if !bytes.Equal(wantBytes, gotBytes) {
					t.Fatal("分段路径改变了原文字节")
				}
			}
		})
	}
	parts := [][]byte{[]byte(`[{"encrypted_content":`), []byte(`"` + strings.Repeat("QUJD", 1<<18) + `"`), []byte(`}]`)}
	allocated := sharedBodyMeasureAllocated(func() {
		segments, ok := newOrderedJSONArraySegments(parts)
		if !ok || segments.bytes != nil {
			t.Fatal("长字符串 token 未保持只读分段")
		}
	})
	if allocated > 4<<10 {
		t.Fatalf("跨片段验证分配 %d 字节，疑似重新拼接了大数组", allocated)
	}
}

func TestJSONObjectValueSegmentsFallbackKeepsParserValidation(t *testing.T) {
	cases := map[string][][]byte{
		"empty_array":     {[]byte("["), []byte("]")},
		"split_string":    {[]byte(`["原`), []byte(`文"]`)},
		"outer_spaces":    {[]byte(" ["), []byte("1"), []byte("] ")},
		"trailing_comma":  {[]byte("["), []byte("1"), []byte(","), []byte("]")},
		"invalid_element": {[]byte("["), []byte(`{"broken":}`), []byte("]")},
		"extra_value":     {[]byte("["), []byte("1 2"), []byte("]")},
		"empty_segments":  {},
	}
	for name, segments := range cases {
		t.Run(name, func(t *testing.T) {
			members := []JSONObjectMember{{Name: "input", QuotedName: []byte(`"input"`), ValueSegments: segments}}
			raw := AppendJSONObjectMembers(nil, members)
			want, _, wantErr := PrepareOfficialCodexAttemptRequestBody("responses_http", NewSharedReplayableRequestBody(raw))
			got, _, gotErr := PrepareOfficialCodexAttemptRequestBody("responses_http", NewSharedReplayableJSONObjectRequestBody(members))
			if (gotErr == nil) != (wantErr == nil) {
				t.Fatalf("片段入口改变了 JSON 语法验证：raw=%q want=%v got=%v", raw, wantErr, gotErr)
			}
			if gotErr != nil {
				if gotErr.Error() != wantErr.Error() {
					t.Fatalf("片段入口未返回原扫描错误：want=%v got=%v", wantErr, gotErr)
				}
				return
			}
			wantBytes, _ := want.ReplayableBytes()
			gotBytes, _ := got.ReplayableBytes()
			if !bytes.Equal(gotBytes, wantBytes) {
				t.Fatal("合法片段退回路径改变了原始 JSON 字节")
			}
		})
	}
}

func TestJSONObjectValueSegmentsKeepOwnershipAndUnsupportedFieldCleanup(t *testing.T) {
	parts := [][]byte{
		[]byte("["),
		[]byte(`{"role":"user","content":[{"type":"input_text","text":"hi","prompt_cache_breakpoint":{"ttl":"5m"}}]}`),
		[]byte("]"),
	}
	body := NewSharedReplayableJSONObjectRequestBody([]JSONObjectMember{{
		Name: "input", QuotedName: []byte(`"input"`), ValueSegments: parts,
	}})
	// 调用方可替换自己持有的描述；共享构造器必须独立复制片段列表。
	parts[1] = []byte("null")
	prepared, _, err := PrepareOfficialCodexAttemptRequestBody("responses_http", body)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := prepared.ReplayableBytes()
	if !bytes.Equal(got, []byte(`{"input":[{"role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)) {
		t.Fatalf("片段列表共享或缓存字段清理行为错误：%s", got)
	}
}
