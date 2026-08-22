package officialegress

import (
	"strings"
	"testing"
)

func TestOpenAIIngressAdaptersBuildLosslessCanonicalRequests(t *testing.T) {
	tests := []struct {
		name     string
		protocol string
		body     string
		stream   bool
		usage    bool
	}{
		{
			name:     "chat-completions",
			protocol: IngressProtocolOpenAIChatCompletions,
			body: `{
				"model":"claude-opus-5",
				"messages":[
					{"role":"system","content":"system one"},
					{"role":"system","content":[{"type":"text","text":"system two"}]},
					{"role":"user","content":[{"type":"text","text":"hello"}]},
					{"role":"assistant","content":"answer"},
					{"role":"user","content":"follow up"}
				],
				"max_completion_tokens":32000,
				"reasoning_effort":"high",
				"stream":true,
				"stream_options":{"include_usage":true},
				"store":false,
				"tools":[],
				"tool_choice":"none",
				"parallel_tool_calls":false,
				"response_format":{"type":"text"}
			}`,
			stream: true,
			usage:  true,
		},
		{
			name:     "responses",
			protocol: IngressProtocolOpenAIResponses,
			body: `{
				"model":"claude-fable-5",
				"instructions":"system one",
				"input":[
					{"type":"message","role":"system","content":[{"type":"input_text","text":"system two"}]},
					{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]},
					{"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}]},
					{"type":"message","role":"user","content":"follow up"}
				],
				"max_output_tokens":32000,
				"reasoning":{"effort":"high"},
				"stream":false,
				"store":false,
				"tools":[],
				"tool_choice":"none",
				"parallel_tool_calls":false,
				"text":{"format":{"type":"text"}},
				"include":[]
			}`,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			canonical, report, err := AdaptIngressProtocol(test.protocol, []byte(test.body))
			if err != nil {
				t.Fatal(err)
			}
			if err := validateCanonicalTranslation(canonical, report); err != nil {
				t.Fatal(err)
			}
			if canonical.IngressProtocol != test.protocol || canonical.Stream != test.stream ||
				canonical.IncludeUsage != test.usage || canonical.MaxOutputTokens != 32000 ||
				canonical.ReasoningEffort != "high" || canonical.FirstUserText != "hello" ||
				len(canonical.System) != 2 || len(canonical.Messages) != 3 || len(report.Fields) < 10 {
				t.Fatalf("CanonicalRequest 或逐字段报告不完整：canonical=%+v report=%+v", canonical, report)
			}
			if canonical.System[0].Text != "system one" || canonical.System[1].Text != "system two" ||
				canonical.Messages[1].Role != "assistant" || canonical.Messages[1].Content[0].Text != "answer" {
				t.Fatalf("消息角色、文本或顺序发生变化：%+v", canonical)
			}
		})
	}
}

func TestOpenAIIngressAdaptersRejectLossyOrUnknownSemantics(t *testing.T) {
	tests := []struct {
		name     string
		protocol string
		body     string
		want     string
	}{
		{name: "chat-unknown-top-level", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"seed":1}`,
			want: "未知字段"},
		{name: "chat-developer-role", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"developer","content":"rules"},{"role":"user","content":"hello"}]}`,
			want: "role 不在无损闭集"},
		{name: "chat-mid-system", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"},{"role":"system","content":"late"}]}`,
			want: "不是前置 system"},
		{name: "chat-image", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.invalid/a.png"}}]}]}`,
			want: "含未批准字段"},
		{name: "chat-tool-catalog", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"tools":[{"type":"function","function":{"name":"shell"}}]}`,
			want: "tools 只允许空数组"},
		{name: "chat-tool-history", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"assistant","content":"call","tool_calls":[]},{"role":"user","content":"hello"}]}`,
			want: "含未批准字段"},
		{name: "chat-json-schema", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"response_format":{"type":"json_schema"}}`,
			want: "默认 text"},
		{name: "chat-sampling", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"temperature":0}`,
			want: "采样或停止控制"},
		{name: "chat-null-stream", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"stream":null}`,
			want: "stream 必须是布尔值"},
		{name: "chat-null-store", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"store":null}`,
			want: "store 只允许 false"},
		{name: "chat-null-tools", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"tools":null}`,
			want: "tools 只允许空数组"},
		{name: "chat-null-include-usage", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}],"stream":true,"stream_options":{"include_usage":null}}`,
			want: "include_usage 必须是布尔值"},
		{name: "chat-duplicate-field", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","model":"claude-opus-5","messages":[{"role":"user","content":"hello"}]}`,
			want: "字段重复"},
		{name: "chat-whitespace-role", protocol: IngressProtocolOpenAIChatCompletions,
			body: `{"model":"claude-sonnet-5","messages":[{"role":" user ","content":"hello"}]}`,
			want: "role 非法"},
		{name: "responses-function-history", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":[{"type":"function_call","call_id":"call_1","name":"shell","arguments":"{}"}]}`,
			want: "含未批准字段"},
		{name: "responses-encrypted-reasoning", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":[{"type":"reasoning","encrypted_content":"secret"},{"role":"user","content":"hello"}]}`,
			want: "含未批准字段"},
		{name: "responses-image", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":[{"role":"user","content":[{"type":"input_image","image_url":"https://example.invalid/a.png"}]}]}`,
			want: "含未批准字段"},
		{name: "responses-json-schema", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":"hello","text":{"format":{"type":"json_schema"}}}`,
			want: "type=text"},
		{name: "responses-stateful", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":"hello","previous_response_id":"resp_1"}`,
			want: "有状态、后台或采样控制"},
		{name: "responses-reasoning-summary", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":"hello","reasoning":{"effort":"high","summary":"auto"}}`,
			want: "含未批准字段"},
		{name: "responses-null-include", protocol: IngressProtocolOpenAIResponses,
			body: `{"model":"claude-sonnet-5","input":"hello","include":null}`,
			want: "include 只允许空数组"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			canonical, report, err := AdaptIngressProtocol(test.protocol, []byte(test.body))
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("应拒绝且包含 %q：canonical=%+v report=%+v err=%v", test.want, canonical, report, err)
			}
			if report.Lossless || report.Compatibility != "rejected" ||
				len(report.RejectionReasons) == 0 ||
				report.Fields[len(report.Fields)-1].Disposition != TranslationDispositionRejected {
				t.Fatalf("拒绝没有进入逐字段 TranslationReport：%+v", report)
			}
		})
	}
}

func TestOpenAIIngressAdaptersPreserveTextWhitespace(t *testing.T) {
	canonical, _, err := AdaptIngressProtocol(
		IngressProtocolOpenAIChatCompletions,
		[]byte(`{"model":"claude-sonnet-5","messages":[{"role":"user","content":[{"type":"text","text":"  hello  "}]}]}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if canonical.Messages[0].Content[0].Text != "  hello  " || canonical.FirstUserText != "  hello  " {
		t.Fatalf("文本空白被改写：%q", canonical.Messages[0].Content[0].Text)
	}
}
