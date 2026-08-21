package service

import (
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestBufferClaudeFWGSSEToJSONPreservesApprovedContent(t *testing.T) {
	source := strings.Join([]string{
		`event: message_start`,
		`data: {"type":"message_start","message":{"id":"msg_buffer","type":"message","role":"assistant","model":"claude-sonnet-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":7,"output_tokens":0,"cache_read_input_tokens":2}}}`,
		``,
		`event: content_block_start`,
		`data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reason"}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig"}}`,
		``,
		`event: content_block_stop`,
		`data: {"type":"content_block_stop","index":0}`,
		``,
		`event: content_block_start`,
		`data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":"","citations":[]}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":1,"delta":{"type":"citations_delta","citation":{"type":"char_location","cited_text":"source","document_index":0,"document_title":"doc","start_char_index":0,"end_char_index":6}}}`,
		``,
		`event: content_block_stop`,
		`data: {"type":"content_block_stop","index":1}`,
		``,
		`event: content_block_start`,
		`data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"Read","input":{}}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"file_"}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"path\":\"/tmp/a\"}"}}`,
		``,
		`event: content_block_stop`,
		`data: {"type":"content_block_stop","index":2}`,
		``,
		`event: message_delta`,
		`data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":9}}`,
		``,
		`event: message_stop`,
		`data: {"type":"message_stop"}`,
		``,
	}, "\n")
	resp := &http.Response{Body: io.NopCloser(strings.NewReader(source))}

	got, err := (&GatewayService{}).bufferClaudeFWGSSEToJSON(resp)
	require.NoError(t, err)
	require.JSONEq(t, `{
		"id":"msg_buffer",
		"type":"message",
		"role":"assistant",
		"model":"claude-sonnet-5",
		"content":[
			{"type":"thinking","thinking":"reason","signature":"sig"},
			{"type":"text","text":"answer","citations":[{"type":"char_location","cited_text":"source","document_index":0,"document_title":"doc","start_char_index":0,"end_char_index":6}]},
			{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/tmp/a"}}
		],
		"stop_reason":"tool_use",
		"stop_sequence":null,
		"usage":{"input_tokens":7,"output_tokens":9,"cache_read_input_tokens":2}
	}`, string(got))
}

func TestBufferClaudeFWGSSEToJSONRejectsIncompleteOrUnknownStreams(t *testing.T) {
	start := "event: message_start\n" +
		`data: {"type":"message_start","message":{"id":"msg_bad","type":"message","role":"assistant","model":"claude-sonnet-5","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}` + "\n\n"
	stop := "event: message_stop\n" + `data: {"type":"message_stop"}` + "\n\n"
	tests := []struct {
		name   string
		source string
	}{
		{name: "缺少终止事件", source: start},
		{
			name: "工具输入不是完整对象",
			source: start + "event: content_block_start\n" +
				`data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_bad","name":"Read","input":{}}}` + "\n\n" +
				"event: content_block_delta\n" +
				`data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"[1]"}}` + "\n\n" +
				"event: content_block_stop\n" + `data: {"type":"content_block_stop","index":0}` + "\n\n" + stop,
		},
		{
			name: "未知事件",
			source: start + "event: future_event\n" +
				`data: {"type":"future_event"}` + "\n\n" + stop,
		},
		{
			name:   "终止后仍有事件",
			source: start + stop + "event: ping\n" + `data: {"type":"ping"}` + "\n\n",
		},
		{
			name: "错误事件",
			source: start + "event: error\n" +
				`data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}` + "\n\n",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp := &http.Response{Body: io.NopCloser(strings.NewReader(tt.source))}
			_, err := (&GatewayService{}).bufferClaudeFWGSSEToJSON(resp)
			require.Error(t, err)
		})
	}
}
