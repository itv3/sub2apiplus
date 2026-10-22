package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// WS→HTTP 单轮桥接路径上的契约捕获：input 契约来自完整原文，身份与小字段来自入口已归一化的版本。入站 input 前缀里带标记的
// 基础指令消息也属于 input 契约，必须随完整原文那一半带过去，否则大帧走桥接时官方客户端的基础指令消息会丢失标记来源。
func TestOfficialEgressBridgeContractKeepsInboundBaseInstructions(t *testing.T) {
	const officialText = "Official base instructions.\n"
	tagged := map[string]any{
		"type": "message", "role": "developer",
		"content": []any{map[string]any{"type": "input_text", "text": officialText}},
		"internal_chat_message_metadata_passthrough": map[string]any{
			"content_item_kinds": []any{"model.base_instructions"},
		},
	}
	base := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	full := mutateOfficialOpenAIHTTPTestBody(t, base, func(payload map[string]any, _ map[string]any, _ map[string]any) {
		input, ok := payload["input"].([]any)
		require.True(t, ok)
		payload["input"] = append([]any{tagged}, input...)
	})
	// 入口已归一化的小字段版本：与完整原文是同一逻辑正文，但没有 input 历史。
	header := mutateOfficialOpenAIHTTPTestBody(t, base, func(payload map[string]any, _ map[string]any, _ map[string]any) {
		payload["input"] = []any{}
	})
	headerIndex, err := buildOfficialJSONRawIndexForDecode(header)
	require.NoError(t, err)
	workspace := &officialForwardHTTPBody{
		bridge:    &openAIWSHTTPBridgeBody{source: full, headerIndex: headerIndex},
		wsIngress: &openAIWSDeferredIngressBody{},
	}
	c := newOfficialOpenAIHTTPTestContext(full, "/v1/responses")

	bridged, err := workspace.captureContract(c, full)
	require.NoError(t, err)
	text, found := bridged.inboundTaggedMessageText("model.base_instructions")
	require.True(t, found, "桥接路径的契约必须带着入站带标记的基础指令消息")
	require.Equal(t, officialText, text)

	// 与不走桥接的契约一致：input 契约（additional_tools、call_id）同样来自完整原文。
	plain, err := captureOfficialOpenAIHTTPBodyContract(full)
	require.NoError(t, err)
	plainText, plainFound := plain.inboundTaggedMessageText("model.base_instructions")
	require.True(t, plainFound)
	require.Equal(t, plainText, text)
	require.Equal(t, plain.additionalTools, bridged.additionalTools)
	require.Equal(t, plain.callIDs, bridged.callIDs)
}
