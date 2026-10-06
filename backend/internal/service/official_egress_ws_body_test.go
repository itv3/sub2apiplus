package service

import (
	"bytes"
	"encoding/json"
	"math/rand"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOfficialWSHistoryComparisonMatchesLegacy(t *testing.T) {
	inputs := []string{
		`{}`, `{"input":null}`, `{"input":[]}`, `{"input":" a \n b "}`,
		`{"input":1}`, `{"input":{}}`, `{"input":[null,1,true,"text",[]]}`,
		`{"input":[{"role":"user","content":" a \n b "}]}`,
		`{"input":[{"type":"message","role":"user","content":[{"text":" a "},{"input_text":" b "}]}]}`,
		`{"input":[{"type":"additional_tools","tools":[1]},{"role":"user","content":"x","id":"item_bad","call_id":"orphan"}]}`,
		`{"input":[{"type":"message","role":"user","content":"x"}]}`,
		`{"input":[{"type":"function_call","call_id":"c1","arguments":"{}","extra":1.200e+3}]}`,
		`{"input":[{"type":"function_call","call_id":"c2","arguments":"{}","extra":1200}]}`,
		`{"input":[{"x":1,"x":2,"n":9007199254740993,"empty":{}}]}`,
	}
	rng := rand.New(rand.NewSource(202610061))
	for i := 0; i < 100; i++ {
		inputs = append(inputs, `{"input":[`+decodeRandomJSON(rng, 1)+`]}`)
	}
	for i, original := range inputs {
		for j, candidate := range inputs {
			left, _, err := (*officialWSFrameBody)(nil).decode([]byte(original))
			require.NoError(t, err)
			right, _, err := (*officialWSFrameBody)(nil).decode([]byte(candidate))
			require.NoError(t, err)
			leftBefore, _ := json.Marshal(left)
			rightBefore, _ := json.Marshal(right)
			got, gotErr := equalOfficialOpenAIWSBusinessHistory(left, right)
			leftAfter, _ := json.Marshal(left)
			rightAfter, _ := json.Marshal(right)
			require.Equal(t, leftBefore, leftAfter, "不能修改原帧对象树")
			require.Equal(t, rightBefore, rightAfter, "不能修改候选对象树")
			wantLeft, wantErr := legacyOfficialOpenAIWSBusinessHistory(left)
			var wantRight []byte
			if wantErr == nil {
				wantRight, wantErr = legacyOfficialOpenAIWSBusinessHistory(right)
			}
			if wantErr != nil {
				require.EqualError(t, gotErr, wantErr.Error(), "%d/%d", i, j)
			} else {
				require.NoError(t, gotErr)
				require.Equal(t, bytes.Equal(wantLeft, wantRight), got, "%d/%d", i, j)
			}
		}
	}
}

func TestOfficialWSFrameBodyOwnership(t *testing.T) {
	body := &officialWSFrameBody{}
	source := []byte(`{"input":[{"role":"user","content":"` + strings.Repeat("x", 4096) + `"}]}`)
	first, index, err := body.decode(source)
	require.NoError(t, err)
	second, secondIndex, err := body.decode(source)
	require.NoError(t, err)
	require.Same(t, index, secondIndex, "同一版本正文应共享扫描结果")
	first["input"].([]any)[0].(map[string]any)["role"] = "assistant"
	require.Equal(t, "user", second["input"].([]any)[0].(map[string]any)["role"], "可写对象树不能共享")
	_, nextIndex, err := body.decode(bytes.Clone(source))
	require.NoError(t, err)
	require.NotSame(t, index, nextIndex, "换正文后不能误用旧索引")
	body.clear()
	require.Nil(t, body.index, "轮末不得通过索引保活整帧")
}

func TestOpenAIWSStrictStateSmallDecodeMatchesLegacy(t *testing.T) {
	inputs := []string{
		`null`, `[]`, `{} {}`, `{"bad":`,
		`{"input":[{"content":"` + strings.Repeat("x", 1<<20) + `"}],"previous_response_id":"r","model":"m"}`,
		`{"input":1,"model":"first","model":"last","n":9007199254740993,"previous_response_id":null}`,
	}
	for _, input := range inputs {
		var decoded map[string]any
		wantErr := decodeOpenAIJSONUseNumber([]byte(input), &decoded)
		var want []byte
		if wantErr == nil {
			delete(decoded, "input")
			delete(decoded, "previous_response_id")
			want, wantErr = json.Marshal(decoded)
		}
		got, gotErr := normalizeOpenAIWSPayloadWithoutInputAndPreviousResponseID([]byte(input))
		if wantErr != nil {
			require.EqualError(t, gotErr, wantErr.Error())
		} else {
			require.NoError(t, gotErr)
			require.Equal(t, string(want), string(got))
		}
	}
}

func TestOfficialWSFrameMembersMatchLegacyMarshal(t *testing.T) {
	rng := rand.New(rand.NewSource(202610063))
	for i := 0; i < 100; i++ {
		source := []byte(`{"type":"response.create","input":[` + decodeRandomJSON(rng, 1) + `],"model":"m"}`)
		body := &officialWSFrameBody{}
		payload, index, err := body.decode(source)
		require.NoError(t, err)
		payload["client_metadata"] = map[string]any{"turn_id": "new", "key": "<>&"}
		payload["input"] = append(payload["input"].([]any), map[string]any{"role": "user", "content": "next"})
		want, wantErr := marshalOfficialOpenAIWSJSONPreservingRaw(officialClientProfileModeActive, payload, source)
		got, gotErr := marshalOfficialWSFrameBody(officialClientProfileModeActive, payload, source, index)
		if wantErr != nil {
			require.EqualError(t, gotErr, wantErr.Error())
		} else {
			require.NoError(t, gotErr)
			require.Equal(t, string(want), string(got))
		}
	}
}

// 两个重试分支不得通过共享头数组相互覆盖，且新状态不能保留旧容量中的无关项。
func TestOpenAIWSReplayStateAppendOwnsHeaders(t *testing.T) {
	items := make([]json.RawMessage, 1, 8)
	items[0] = json.RawMessage(`{"type":"message","content":"history"}`)
	original := newOpenAIWSReplayInputState(items, true)
	firstDelta := []json.RawMessage{json.RawMessage(`{"type":"message","content":"first"}`)}
	secondDelta := []json.RawMessage{json.RawMessage(`{"type":"message","content":"second"}`)}
	first, err := appendOpenAIWSReplayInputState(original, firstDelta, defaultOpenAIWSReplayInputLimits())
	require.NoError(t, err)
	_, err = appendOpenAIWSReplayInputState(original, secondDelta, defaultOpenAIWSReplayInputLimits())
	require.NoError(t, err)
	require.Equal(t, firstDelta[0], first.items[1])
	require.Equal(t, len(first.items), cap(first.items))
	first, err = buildOpenAIWSReplayInputState(original, firstDelta, true, true, defaultOpenAIWSReplayInputLimits())
	require.NoError(t, err)
	_, err = buildOpenAIWSReplayInputState(original, secondDelta, true, true, defaultOpenAIWSReplayInputLimits())
	require.NoError(t, err)
	require.Equal(t, firstDelta[0], first.items[1])
}
