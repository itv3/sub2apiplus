package apicompat

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 同时检查转换后的线协议及原始响应，防止修正展示 ID 时影响正文、计费或上游追踪。
func TestResponsesToChatCompletions_WireID(t *testing.T) {
	for _, tc := range []struct {
		name       string
		upstreamID string
		wantID     string
	}{
		{name: "responses", upstreamID: "resp_hub_probe", wantID: "chatcmpl-hub_probe"},
		{name: "chat_completions", upstreamID: "chatcmpl-existing", wantID: "chatcmpl-existing"},
		{name: "other_upstream", upstreamID: "custom_123", wantID: "chatcmpl-custom_123"},
		{name: "surrounding_whitespace", upstreamID: " resp_trimmed ", wantID: "chatcmpl-trimmed"},
		{name: "missing"},
		{name: "empty_responses_suffix", upstreamID: "resp_"},
		{name: "empty_chat_suffix", upstreamID: "chatcmpl-"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			upstream := &ResponsesResponse{
				ID: tc.upstreamID, Status: "completed",
				Output: []ResponsesOutput{
					{Type: "message", Content: []ResponsesContentPart{{Type: "output_text", Text: `{"ok":true}`}}},
					{Type: "function_call", CallID: "call_weather", Name: "get_weather", Arguments: `{"city":"Boston"}`},
				},
				Usage: &ResponsesUsage{InputTokens: 14, OutputTokens: 5, TotalTokens: 19},
			}
			response := ResponsesToChatCompletions(upstream, "gpt-6.1-sol")
			if tc.wantID != "" {
				require.Equal(t, tc.wantID, response.ID)
			} else {
				require.Regexp(t, `^chatcmpl-[0-9a-f]{24}$`, response.ID)
			}
			require.Equal(t, tc.upstreamID, upstream.ID, "原始上游 ID 必须保留")
			require.Equal(t, "call_weather", response.Choices[0].Message.ToolCalls[0].ID)
			require.Equal(t, `{"city":"Boston"}`, response.Choices[0].Message.ToolCalls[0].Function.Arguments)

			body, err := json.Marshal(response)
			require.NoError(t, err)
			var wire map[string]any
			require.NoError(t, json.Unmarshal(body, &wire))
			require.Equal(t, "chat.completion", wire["object"])
			require.NotContains(t, wire, "output")
			require.NotContains(t, wire, "status")
			choices, ok := wire["choices"].([]any)
			require.True(t, ok)
			require.Len(t, choices, 1)
			choice, ok := choices[0].(map[string]any)
			require.True(t, ok)
			require.Equal(t, "tool_calls", choice["finish_reason"])
			message, ok := choice["message"].(map[string]any)
			require.True(t, ok)
			require.Equal(t, `{"ok":true}`, message["content"])
			require.Equal(t, map[string]any{
				"prompt_tokens": float64(14), "completion_tokens": float64(5), "total_tokens": float64(19),
			}, wire["usage"])
		})
	}
}

// 正常、重复、缺失及迟到的创建事件均不能让同一条流中途切换 ID。
func TestResponsesEventToChatChunks_WireIDStable(t *testing.T) {
	created := func(id string) *ResponsesStreamEvent {
		return &ResponsesStreamEvent{Type: "response.created", Response: &ResponsesResponse{ID: id}}
	}
	for _, tc := range []struct {
		name   string
		events []*ResponsesStreamEvent
		wantID string
	}{
		{name: "normal", events: []*ResponsesStreamEvent{created("resp_stream")}, wantID: "chatcmpl-stream"},
		{name: "duplicate", events: []*ResponsesStreamEvent{created("resp_first"), created("resp_second")}, wantID: "chatcmpl-first"},
		{name: "missing"},
		{name: "late", events: []*ResponsesStreamEvent{
			{Type: "response.reasoning_summary_text.delta", Delta: "先检查请求"}, created("resp_late"),
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := NewResponsesEventToChatState()
			state.Model = "gpt-6.1-sol"
			state.IncludeUsage = true
			wantID := tc.wantID
			if wantID == "" {
				wantID = state.ID
			}
			var chunks []ChatCompletionsChunk
			for _, event := range tc.events {
				chunks = append(chunks, ResponsesEventToChatChunks(event, state)...)
			}
			chunks = append(chunks, ResponsesEventToChatChunks(&ResponsesStreamEvent{
				Type: "response.output_text.delta", Delta: "pong",
			}, state)...)
			chunks = append(chunks, ResponsesEventToChatChunks(&ResponsesStreamEvent{
				Type: "response.completed", Response: &ResponsesResponse{
					ID: "resp_terminal", Status: "completed",
					Usage: &ResponsesUsage{InputTokens: 14, OutputTokens: 5, TotalTokens: 19},
				},
			}, state)...)

			var text strings.Builder
			for _, chunk := range chunks {
				require.Equal(t, wantID, chunk.ID)
				sse, err := ChatChunkToSSE(chunk)
				require.NoError(t, err)
				var wire map[string]any
				require.NoError(t, json.Unmarshal([]byte(strings.TrimSpace(strings.TrimPrefix(sse, "data: "))), &wire))
				require.Equal(t, "chat.completion.chunk", wire["object"])
				require.Equal(t, wantID, wire["id"])
				for _, choice := range chunk.Choices {
					if choice.Delta.Content != nil {
						_, _ = text.WriteString(*choice.Delta.Content)
					}
				}
			}
			require.Equal(t, "pong", text.String())
			require.Equal(t, "stop", *chunks[len(chunks)-2].Choices[0].FinishReason)
			require.Equal(t, &ChatUsage{PromptTokens: 14, CompletionTokens: 5, TotalTokens: 19}, chunks[len(chunks)-1].Usage)
		})
	}
}
