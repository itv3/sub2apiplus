package service

import (
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 回放字段保真：官方客户端回放历史时，input 项带着 status、annotations: []、logprobs: []、encrypted_content、summary: []
// 等字段。基础指令载体只在前缀插入或去掉一条消息，其余历史项不得被改动：
//   - HTTP：未改写的历史项逐字节原样（连内部空白与键序都在）；
//   - WS：历史项会按规则补逐项 turn_id（这是旧逻辑就有的改写），但原有字段一个不少、取值逐字节相同；
// 节缺省的 Active 槽位同样成立（对照）。

// replayFidelityItems 是一组回放历史项；第二项刻意带不规则空白与非字典序键序，使“逐字节原样”可观测。
var replayFidelityItems = []string{
	baseInstructionsUserItem,
	`{ "id" : "msg_replay_1" , "type":"message","role":"assistant","status":"completed",` +
		`"content":[{"type":"output_text","text":"好的 <ok> & done","annotations":[],"logprobs":[]}] }`,
	`{"type":"reasoning","id":"rs_replay_1","summary":[],"encrypted_content":"gAAAAAB-encrypted_payload=="}`,
	`{"type":"function_call","id":"fc_replay_1","call_id":"call_replay_1","name":"read","arguments":"{\"path\":\"a\"}","status":"completed"}`,
	`{"type":"function_call_output","call_id":"call_replay_1","output":"done"}`,
	`{"type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}`,
}

func replayFidelityInput() string {
	return `[` + strings.Join(replayFidelityItems, ",") + `]`
}

func TestOfficialEgressHTTPBodyKeepsReplayHistoryByteForByte(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	body := buildBaseInstructionsHTTPBody(baseInstructionsHTTPVariant{"replay", `"You are a helper."`, replayFidelityInput()})
	for _, mode := range []string{officialClientProfileModePrevious, officialClientProfileModeActive} {
		for _, lite := range []bool{false, true} {
			label := mode + "/lite=" + strconv.FormatBool(lite)
			final, _, _, err := finalizeBaseInstructionsHTTP(t, mode, lite, "entry", body)
			require.NoError(t, err, label)
			input := gjson.GetBytes(final, "input").Array()
			for _, item := range replayFidelityItems {
				// 逐项核对：原文片段整段出现在定型后的 input 里（同一份原文不会出现在别处）。
				require.Contains(t, gjson.GetBytes(final, "input").Raw, item, "%s：历史项必须逐字节原样：%s", label, item)
			}
			if mode == officialClientProfileModePrevious {
				require.Equal(t, "model.base_instructions",
					input[0].Get("internal_chat_message_metadata_passthrough.content_item_kinds.0").String(), label)
				require.Len(t, input, len(replayFidelityItems)+1, label)
			}
		}
	}
}

func TestOpenAIWSFramesKeepReplayHistoryFields(t *testing.T) {
	withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	payload := buildWSLegacyDerivedPayload(wsLegacyVariant{
		name: "replay", instructions: `"You are a helper."`, input: replayFidelityInput(),
	})
	// requireReplayFields 要求输出帧里的每个历史项保留原有的全部字段，取值逐字节相同；允许多出逐项 turn_id。
	requireReplayFields := func(t *testing.T, frame []byte, label string) {
		t.Helper()
		var outputs []gjson.Result
		for _, item := range gjson.GetBytes(frame, "input").Array() {
			// 前缀项不属于回放历史：additional_tools、官方形态的基础指令消息，以及节缺省时旧逻辑迁入的指令 developer 消息。
			if item.Get("internal_chat_message_metadata_passthrough.content_item_kinds").Exists() ||
				item.Get("type").String() == "additional_tools" || item.Get("role").String() == "developer" {
				continue
			}
			outputs = append(outputs, item)
		}
		require.Len(t, outputs, len(replayFidelityItems), label)
		for index, original := range replayFidelityItems {
			output := outputs[index]
			gjson.Parse(original).ForEach(func(key, value gjson.Result) bool {
				require.Equal(t, value.Raw, output.Get(strings.ReplaceAll(key.String(), ".", `\.`)).Raw,
					"%s：input 第 %d 项的字段 %s 必须原样保留", label, index, key.String())
				return true
			})
		}
	}
	for _, mode := range []string{officialClientProfileModePrevious, officialClientProfileModeActive} {
		for _, lite := range []bool{false, true} {
			label := mode + "/lite=" + strconv.FormatBool(lite)
			ctx := newWSLegacyDerivedContext(t, mode, lite, payload)
			first, _, err := prepareOpenAIOfficialEgressSemanticWSFrame(ctx, payload, payload, "", false)
			require.NoError(t, err, label)
			requireReplayFields(t, first, label+"/first")
			if mode != officialClientProfileModePrevious {
				continue
			}
			// 载体启用时，链帧同样保留全部历史项（预热帧只取前缀，不含历史）。
			chain, err := chainDerivedOpenAIOfficialEgressWSBusinessFrame(ctx, first, "resp_replay_prewarm")
			require.NoError(t, err, label)
			requireReplayFields(t, chain, label+"/chain")
		}
	}
}
