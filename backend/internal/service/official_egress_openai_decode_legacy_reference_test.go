package service

// 本文件是“减少解码副本”变更集改造前各阶段实现的逐字副本，只作为差分测试的参照，
// 证明预检/零拷贝/共享解码后的输出与旧实现逐字节一致。生产代码不得引用这里的符号。

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/klauspost/compress/zstd"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func legacyCaptureOfficialOpenAIHTTPBodyContract(body []byte) (*officialOpenAIHTTPBodyContract, error) {
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return nil, fmt.Errorf("OpenAI official egress requires valid JSON body: %w", err)
	}
	contract := &officialOpenAIHTTPBodyContract{}
	contract.instructions, contract.instructionsPresent = payload["instructions"]
	contract.include, contract.includePresent = payload["include"]
	contract.parallelToolCalls, contract.parallelPresent = payload["parallel_tool_calls"]

	if promptCacheKey, ok := payload["prompt_cache_key"].(string); ok &&
		strings.TrimSpace(promptCacheKey) != "" {
		contract.promptCacheKeySet = true
		contract.promptCacheKey = strings.TrimSpace(promptCacheKey)
	}

	if clientMetadata, ok := payload["client_metadata"].(map[string]any); ok {
		contract.clientMetadataSet = true
		contract.clientMetadata = cloneOfficialOpenAIMap(clientMetadata)
	}
	contract.additionalTools = collectOfficialOpenAIAdditionalTools(payload)
	contract.callIDs = collectOfficialOpenAICallIDs(payload)
	return contract, nil
}

func legacyNormalizeOpenAIResponsesLegacyIngress(body []byte) ([]byte, bool, error) {
	if len(body) == 0 {
		return body, false, nil
	}

	var request map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &request); err != nil {
		return body, false, fmt.Errorf("normalize legacy Responses ingress: %w", err)
	}

	changed := false
	messagesValue, hasMessages := request["messages"]
	if hasMessages {
		messages, messagesAreArray := messagesValue.([]any)
		if messagesAreArray && len(messages) > 0 {
			legacy, err := convertLegacyResponsesMessages(nil, body)
			if err != nil {
				return body, false, err
			}

			input, hasInput := request["input"]
			hasNativeInput := hasInput && input != nil
			if !hasNativeInput {
				request["input"] = legacy.input
				applyLegacyResponsesTopLevelFields(request, legacy)
				delete(request, "previous_response_id")
			}
		}
		// messages is not a Responses field. When native input is present but the
		// two histories are ambiguous, retain input and drop only the legacy alias.
		delete(request, "messages")
		changed = true
	}

	if prompt, hasPrompt := request["prompt"]; hasPrompt {
		// Only the legacy string alias is unambiguously equivalent to Responses
		// input. Objects are native reusable prompt templates and must remain in
		// prompt; arrays and other shapes are left for upstream validation rather
		// than being relabeled as a structurally different input value.
		if promptText, isLegacyString := prompt.(string); isLegacyString {
			if input, hasInput := request["input"]; !hasInput || input == nil {
				request["input"] = promptText
			}
			delete(request, "prompt")
			changed = true
		}
	}
	if _, hasCommands := request["commands"]; hasCommands {
		delete(request, "commands")
		changed = true
	}

	if !changed {
		return body, false, nil
	}
	normalized, err := marshalOpenAIUpstreamJSON(request)
	if err != nil {
		return body, false, fmt.Errorf("serialize legacy Responses ingress: %w", err)
	}
	return normalized, true, nil
}

func legacyNormalizeCompactionTriggerInputOrder(body []byte) ([]byte, bool, error) {
	if len(body) == 0 {
		return body, false, nil
	}
	var payload map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &payload); err != nil {
		return body, false, err
	}
	input, ok := payload["input"].([]any)
	if !ok || len(input) == 0 {
		return body, false, nil
	}
	triggerCount := 0
	normalized := make([]any, 0, len(input))
	for _, raw := range input {
		item, itemOK := raw.(map[string]any)
		if itemOK && item["type"] == "compaction_trigger" {
			triggerCount++
			continue
		}
		normalized = append(normalized, raw)
	}
	if triggerCount == 0 {
		return body, false, nil
	}
	if triggerCount == 1 {
		if last, ok := input[len(input)-1].(map[string]any); ok && last["type"] == "compaction_trigger" {
			return body, false, nil
		}
	}
	normalized = append(normalized, map[string]any{"type": "compaction_trigger"})
	payload["input"] = normalized
	encoded, err := marshalOpenAIUpstreamJSON(payload)
	if err != nil {
		return body, false, err
	}
	return encoded, true, nil
}

func legacySanitizeOpenAIResponsesInputItemIDs(body []byte) ([]byte, bool, error) {
	input := gjson.GetBytes(body, "input")
	if !input.IsArray() {
		return body, false, nil
	}

	type inputItem struct {
		body        []byte
		stripID     bool
		stripCallID bool
	}

	items := make([]inputItem, 0)
	input.ForEach(func(_, item gjson.Result) bool {
		parsed := inputItem{body: []byte(item.Raw)}
		if item.IsObject() {
			itemType := item.Get("type")
			id := item.Get("id")
			trimmedItemType := strings.TrimSpace(itemType.String())
			parsed.stripCallID = item.Get("call_id").Exists() && shouldStripOpenAIResponsesNonPairCallID(trimmedItemType)
			if id.Type == gjson.String {
				parsed.stripID = shouldStripOpenAIResponsesInputItemID(trimmedItemType, id.String())
			}
		}
		items = append(items, parsed)
		return true
	})
	hasSanitization := false
	for _, item := range items {
		if item.stripID || item.stripCallID {
			hasSanitization = true
			break
		}
	}
	if !hasSanitization {
		return body, false, nil
	}

	rebuiltItems := make([][]byte, 0, len(items))
	for index, item := range items {
		itemBody := item.body
		if item.stripID {
			var err error
			itemBody, err = sjson.DeleteBytes(itemBody, "id")
			if err != nil {
				return nil, false, fmt.Errorf("delete input.%d.id: %w", index, err)
			}
		}
		if item.stripCallID {
			var err error
			itemBody, err = sjson.DeleteBytes(itemBody, "call_id")
			if err != nil {
				return nil, false, fmt.Errorf("delete input.%d.call_id: %w", index, err)
			}
		}
		rebuiltItems = append(rebuiltItems, itemBody)
	}

	rebuiltInput := make([]byte, 0, len(input.Raw))
	rebuiltInput = append(rebuiltInput, '[')
	for i, item := range rebuiltItems {
		if i > 0 {
			rebuiltInput = append(rebuiltInput, ',')
		}
		rebuiltInput = append(rebuiltInput, item...)
	}
	rebuiltInput = append(rebuiltInput, ']')

	sanitized, err := sjson.SetRawBytes(body, "input", rebuiltInput)
	if err != nil {
		return nil, false, fmt.Errorf("replace sanitized input: %w", err)
	}
	return sanitized, true, nil
}

func legacyCompressOfficialOpenAIHTTPRequest(req *http.Request, body []byte, level int) error {
	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(level)))
	if err != nil {
		return fmt.Errorf("create OpenAI official zstd encoder: %w", err)
	}
	compressed := encoder.EncodeAll(body, nil)
	if err := encoder.Close(); err != nil {
		return fmt.Errorf("close OpenAI official zstd encoder: %w", err)
	}
	resetOfficialEgressRequestBody(req, compressed)
	req.Header.Set("Content-Encoding", "zstd")
	return nil
}
