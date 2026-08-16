package officialegress

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func TestAttemptBodyDocumentRejectsAllTopLevelDuplicatesBeforeDirty(t *testing.T) {
	for _, raw := range []string{
		`{"model":1,"model":2}`,
		`{"prompt_cache_key":"a","prompt_cache_key":"b"}`,
		`{"model":1,"\u006dodel":2}`,
	} {
		t.Run(raw, func(t *testing.T) {
			_, _, err := PrepareOfficialCodexAttemptBody("responses_http", []byte(raw))
			if err == nil || !strings.Contains(err.Error(), "字段重复") {
				t.Fatalf("解码后重复字段未在 dirty 前 fail-close：%v", err)
			}
		})
	}
}

func TestAttemptBodyDocumentExtractsOwnedFieldsWithoutRebuildingSource(t *testing.T) {
	source := []byte(`{"client_metadata":{"session_id":" session-1 ","ignored":1},"model":"gpt-5","prompt_cache_key":"caller","input":[]}`)
	body, fields, err := PrepareOfficialCodexAttemptBody("responses_http", source)
	if err != nil {
		t.Fatal(err)
	}
	if fields.Metadata["session_id"] != "session-1" {
		t.Fatalf("client_metadata 未按需解码：%v", fields.Metadata)
	}
	source[2] = 'X'
	semantic, ok := body.ReplayableBytes()
	if !ok || string(semantic) != `{"model":"gpt-5","input":[]}` {
		t.Fatalf("抽取 overlay 或不可变 source 非法：%q", semantic)
	}
}

func TestAttemptBodyDocumentStripsUnsupportedPromptCacheBreakpoints(t *testing.T) {
	tests := []struct {
		name       string
		endpointID string
	}{
		{name: "HTTP", endpointID: "responses_http"},
		{name: "WebSocket", endpointID: "responses_ws"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			source := []byte(`{"type":"response.create","model":"gpt-5.6-luna","input":[{"role":"developer","content":[{"type":"input_text","text":"system","prompt_cache_breakpoint":{"mode":"explicit"}}]},{"role":"user","content":[{"type":"input_text","text":"hi","prompt_cache_breakpoint":{"mode":"explicit"}}]}],"tools":[]}`)
			body, _, err := PrepareOfficialCodexAttemptBody(test.endpointID, source)
			if err != nil {
				t.Fatal(err)
			}
			semantic, ok := body.ReplayableBytes()
			if !ok {
				t.Fatal("未生成可重放的 Responses Body")
			}
			if bytes.Contains(semantic, []byte(`"prompt_cache_breakpoint"`)) {
				t.Fatalf("不支持的缓存断点字段仍存在：%s", semantic)
			}
			if !bytes.Contains(semantic, []byte(`{"type":"input_text","text":"system"}`)) ||
				!bytes.Contains(semantic, []byte(`{"type":"input_text","text":"hi"}`)) {
				t.Fatalf("删除缓存提示时破坏了消息内容：%s", semantic)
			}
		})
	}
}

func TestAttemptBodyDocumentKeepsPromptCacheBreakpointText(t *testing.T) {
	source := []byte(`{"model":"gpt-5.6-luna","input":"请解释 \"prompt_cache_breakpoint\" 字段"}`)
	body, _, err := PrepareOfficialCodexAttemptBody("responses_http", source)
	if err != nil {
		t.Fatal(err)
	}
	semantic, ok := body.ReplayableBytes()
	if !ok || !bytes.Equal(semantic, source) {
		t.Fatalf("普通文本被错误改写：%q", semantic)
	}
}

func TestAttemptBodyDocumentKeepsCompactTextField(t *testing.T) {
	body, _, err := PrepareOfficialCodexAttemptBody(
		"responses_compact",
		[]byte(`{"model":"gpt-5.6-luna","input":[],"parallel_tool_calls":false,"reasoning":{"effort":"medium"},"text":{"verbosity":"low"}}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	semantic, ok := body.ReplayableBytes()
	if !ok || !bytes.Contains(semantic, []byte(`"text":{"verbosity":"low"}`)) {
		t.Fatalf("compact text 字段在语义 Body 抽取阶段被错误删除：%q", semantic)
	}
}

func TestAttemptBodyDocumentCloneAndRetryDoNotShareDirtyOverlay(t *testing.T) {
	raw := []byte(`{"model":"gpt-5","prompt_cache_key":"caller","input":[]}`)
	body, _, err := PrepareOfficialCodexAttemptBody("responses_http", raw)
	if err != nil {
		t.Fatal(err)
	}
	firstPlan := CodexEgressPlan{Body: body}.clone()
	secondPlan := CodexEgressPlan{Body: body}.clone()
	firstPlan.Body.jsonDocument().set("client_metadata", json.RawMessage(`{"session_id":"first"}`))
	firstPlan.Body.jsonDocument().set("prompt_cache_key", json.RawMessage(`"first"`))

	second, ok := secondPlan.Body.ReplayableBytes()
	if !ok || string(second) != `{"model":"gpt-5","input":[]}` {
		t.Fatalf("Plan clone 共享了 dirty overlay：%q", second)
	}
	original, ok := body.ReplayableBytes()
	if !ok || !bytes.Equal(original, second) {
		t.Fatalf("失败 attempt 污染了原 document：original=%q second=%q", original, second)
	}

	retry, _, err := PrepareOfficialCodexAttemptBody("responses_http", raw)
	if err != nil {
		t.Fatal(err)
	}
	retrySemantic, ok := retry.ReplayableBytes()
	if !ok || !bytes.Equal(retrySemantic, second) {
		t.Fatalf("retry 未从同一语义 Body 创建新 document：%q", retrySemantic)
	}
}

func TestAttemptBodyDocumentCompileFailureLeavesOriginalUnchanged(t *testing.T) {
	body, _, err := PrepareOfficialCodexAttemptBody(
		"responses_http",
		[]byte(`{"model":"gpt-5","prompt_cache_key":"caller","input":[]}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	before, _ := body.ReplayableBytes()
	failedAttempt := body.clone()
	failedAttempt.jsonDocument().set("unknown", json.RawMessage(`true`))
	_, err = orderJSONDocumentWithPolicy(
		failedAttempt.jsonDocument(),
		profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyJson,
			Closed:   true,
			Fields: []profilecontract.BodyFieldProfile{
				{Name: "model", Required: true},
				{Name: "input", Required: true},
			},
		},
		profilecontract.FeatureDefaults{},
		CodexRequestConditions{},
		BodyRuntimeConditions{},
		AttemptAuthenticationInput{},
	)
	if err == nil || !strings.Contains(err.Error(), "闭集外字段") {
		t.Fatalf("测试未触发预期编译失败：%v", err)
	}
	after, _ := body.ReplayableBytes()
	if !bytes.Equal(before, after) {
		t.Fatalf("编译失败污染原 document：before=%q after=%q", before, after)
	}
}

func TestAttemptBodyDocumentRejectsDuplicateNestedClientMetadata(t *testing.T) {
	_, _, err := PrepareOfficialCodexAttemptBody(
		"responses_http",
		[]byte(`{"model":"gpt-5","input":[],"client_metadata":{"session_id":"a","session_id":"b"}}`),
	)
	if err == nil || !strings.Contains(err.Error(), "client_metadata") {
		t.Fatalf("嵌套 compiler-owned object 重复字段未 fail-close：%v", err)
	}
}
