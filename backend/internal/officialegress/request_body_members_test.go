package officialegress

import (
	"bytes"
	"context"
	"encoding/json"
	"math/rand"
	"strconv"
	"strings"
	"testing"
)

// 本文件锁定问题四 M1 第三项在 officialegress 侧的等价性：按顶层成员装配的 Body 与用其物化
// 字节构造的 Body 在读取、语义准备（含错误文本）与编译输出上逐项一致，且语义准备与编译全程
// 不物化整段正文。

// membersFromDocument 把合法顶层对象拆成成员，键按 encoding/json 规则重新编码（与 service
// 拼接编码器一致）。
func membersFromDocument(t *testing.T, body []byte) []JSONObjectMember {
	t.Helper()
	document, err := newOrderedJSONDocument(body)
	if err != nil {
		t.Fatalf("拆分成员失败：%v", err)
	}
	members := make([]JSONObjectMember, 0, len(document.fields))
	for _, field := range document.fields {
		quoted, err := json.Marshal(field.name)
		if err != nil {
			t.Fatal(err)
		}
		members = append(members, JSONObjectMember{Name: field.name, QuotedName: quoted, Value: field.value})
	}
	return members
}

// membersRandomList 生成随机成员列表：大多数合法，也刻意混入扫描器会拒绝或与物化字节不一致
// 的形态（非法键编码、键与 Name 不符、值前后空白、非法值、空值、重复键），用来验证退回路径。
func membersRandomList(rng *rand.Rand) []JSONObjectMember {
	names := []string{"model", "input", "client_metadata", "prompt_cache_key", "type", "a", "中", "é", "a<b", "q\"uote", ""}
	count := rng.Intn(6)
	members := make([]JSONObjectMember, 0, count)
	for i := 0; i < count; i++ {
		name := names[rng.Intn(len(names))]
		quoted, _ := json.Marshal(name)
		value := []byte(scanRandomJSON(rng, 1))
		switch rng.Intn(14) {
		case 0:
			quoted = []byte(`"` + name)
		case 1:
			quoted = []byte(`"other"`)
		case 2:
			value = append([]byte(" "), value...)
		case 3:
			value = append(value, ' ')
		case 4:
			value = []byte(`{"a":`)
		case 5:
			value = nil
		case 6:
			if len(members) > 0 {
				name = members[0].Name
				quoted = members[0].QuotedName
			}
		case 7:
			if name == "model" {
				value = []byte(`"gpt-5.6-luna"`)
			}
		}
		members = append(members, JSONObjectMember{Name: name, QuotedName: quoted, Value: value})
	}
	return members
}

func TestJSONObjectMembersBodyMatchesMaterializedBody(t *testing.T) {
	endpoints := []string{
		"responses_http", "responses_compact", "responses_ws", "oauth_refresh", "files_create", "models",
	}
	rng := rand.New(rand.NewSource(20261001))
	cases := map[string][]JSONObjectMember{"empty": nil}
	for i, fixture := range scanFixtures {
		if document, err := newOrderedJSONDocument([]byte(fixture)); err == nil && len(document.fields) >= 0 {
			cases["fixture#"+strconv.Itoa(i)] = membersFromDocument(t, []byte(fixture))
		}
	}
	for round := 0; round < 600; round++ {
		cases["random#"+strconv.Itoa(round)] = membersRandomList(rng)
	}
	for name, members := range cases {
		materialized := AppendJSONObjectMembers(nil, members)
		if JSONObjectMembersLength(members) != len(materialized) {
			t.Fatalf("%s：成员长度计算与物化字节不一致", name)
		}
		membersBody := NewSharedReplayableJSONObjectRequestBody(members)
		bytesBody := NewSharedReplayableRequestBody(materialized)
		membersBytes, membersOK := membersBody.ReplayableBytes()
		plainBytes, plainOK := bytesBody.ReplayableBytes()
		if membersOK != plainOK || !bytes.Equal(membersBytes, plainBytes) ||
			membersBody.ContentLength() != bytesBody.ContentLength() || membersBody.Mode() != bytesBody.Mode() {
			t.Fatalf("%s：成员 Body 的读取结果与物化字节不一致", name)
		}
		for _, endpointID := range endpoints {
			label := name + "@" + endpointID
			legacyBody, legacyFields, legacyErr := PrepareOfficialCodexAttemptRequestBody(endpointID, bytesBody)
			freshMembers := NewSharedReplayableJSONObjectRequestBody(members)
			currentBody, currentFields, currentErr := PrepareOfficialCodexAttemptRequestBody(endpointID, freshMembers)
			sharedBodyComparePrepared(t, label, legacyBody, legacyFields, legacyErr, currentBody, currentFields, currentErr)
			if currentErr != nil || !endpointExtractsCompilerOwnedBody(endpointID) {
				continue
			}
			if _, valid := orderedJSONDocumentFromMembers(freshMembers.jsonObjectMembers().members); valid &&
				freshMembers.jsonObjectMembers().bytes != nil {
				t.Fatalf("%s：成员可直接建 document 时语义准备不得物化整段正文", label)
			}
		}
	}
}

func membersCompileBody(
	t *testing.T,
	bundle ReleaseBundle,
	plan ResolvedEndpointPlan,
	prepared RequestBody,
	fields CompilerOwnedBodyFields,
) (CompiledExecution, []byte) {
	t.Helper()
	egressPlan := staticClosureEgressPlan(t, bundle, plan, staticClosureLegalTarget(plan.template), "members-compile")
	egressPlan.Body = prepared
	egressPlan.RoutingHint = fields.RoutingHint
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译失败：%v", err)
	}
	raw, ok := execution.request.body.replayableView()
	if !ok {
		t.Fatal("编译产物缺少可重放 Body")
	}
	return execution, raw
}

func membersCompileVariants(t *testing.T, base []byte) map[string][]byte {
	t.Helper()
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(base, &decoded); err != nil {
		t.Fatal(err)
	}
	withInput := func(input string) []byte {
		variant := make(map[string]json.RawMessage, len(decoded)+1)
		for key, value := range decoded {
			variant[key] = value
		}
		variant["input"] = json.RawMessage(input)
		out, err := json.Marshal(variant)
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	large := `[` + strings.Repeat(`{"type":"reasoning","summary":[],"encrypted_content":"`+strings.Repeat("QUJD", 4096)+`"},`, 16) +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]`
	return map[string][]byte{
		"base":       base,
		"large":      withInput(large),
		"breakpoint": withInput(`[{"type":"message","role":"user","content":[{"type":"input_text","text":"x","prompt_cache_breakpoint":{"ttl":"5m"}}]}]`),
	}
}

func TestCompileJSONObjectMembersBodyMatchesBytesBody(t *testing.T) {
	bundle, plan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	base := staticClosureSemanticBody(t, plan.template.endpoint)
	for name, body := range membersCompileVariants(t, base) {
		members := membersFromDocument(t, body)
		materialized := AppendJSONObjectMembers(nil, members)
		bytesPrepared, bytesFields, err := PrepareOfficialCodexAttemptRequestBody(
			"responses_http", NewSharedReplayableRequestBody(materialized),
		)
		if err != nil {
			t.Fatalf("%s：字节句柄准备失败：%v", name, err)
		}
		membersInput := NewSharedReplayableJSONObjectRequestBody(members)
		membersPrepared, membersFields, err := PrepareOfficialCodexAttemptRequestBody("responses_http", membersInput)
		if err != nil {
			t.Fatalf("%s：成员句柄准备失败：%v", name, err)
		}
		bytesExecution, bytesWire := membersCompileBody(t, bundle, plan, bytesPrepared, bytesFields)
		membersExecution, membersWire := membersCompileBody(t, bundle, plan, membersPrepared, membersFields)
		if !bytes.Equal(bytesWire, membersWire) {
			t.Fatalf("%s：成员句柄编译出的 wire 正文与字节句柄不一致\nbytes=%q\nmembers=%q", name, bytesWire, membersWire)
		}
		if bytesExecution.CompiledDigest() != membersExecution.CompiledDigest() {
			t.Fatalf("%s：成员句柄与字节句柄的编译摘要不一致", name)
		}
		if membersInput.jsonObjectMembers().bytes != nil {
			t.Fatalf("%s：语义准备与编译期间不得物化整段正文", name)
		}
	}
}

func TestJSONObjectMembersBodyPrepareDoesNotCopyLargeBody(t *testing.T) {
	bundle, plan := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_http")
	base := staticClosureSemanticBody(t, plan.template.endpoint)
	body := membersCompileVariants(t, base)["large"]
	// 放大 input：成员值直接引用这段大字节。
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatal(err)
	}
	decoded["input"] = json.RawMessage(`[` + strings.Repeat(`{"type":"reasoning","summary":[],"encrypted_content":"`+strings.Repeat("QUJD", 32*1024)+`"},`, 63) +
		`{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]`)
	large, err := json.Marshal(decoded)
	if err != nil {
		t.Fatal(err)
	}
	members := membersFromDocument(t, large)
	input := NewSharedReplayableJSONObjectRequestBody(members)
	var prepared RequestBody
	var fields CompilerOwnedBodyFields
	allocated := sharedBodyMeasureAllocated(func() {
		prepared, fields, err = PrepareOfficialCodexAttemptRequestBody("responses_http", input)
		if err != nil {
			t.Fatal(err)
		}
	})
	_, wire := membersCompileBody(t, bundle, plan, prepared, fields)
	t.Logf("正文 %.1f MiB：成员句柄语义准备 %.1f KiB，编译产出 %.1f MiB",
		float64(len(large))/(1<<20), float64(allocated)/1024, float64(len(wire))/(1<<20))
	if allocated > uint64(len(large))/64 {
		t.Fatalf("成员句柄语义准备分配 %d 字节，不得按正文体积（%d 字节）复制或物化", allocated, len(large))
	}
	if input.jsonObjectMembers().bytes != nil {
		t.Fatalf("成员句柄在语义准备与编译期间被物化")
	}
}
