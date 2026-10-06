package service

import (
	"fmt"
	"runtime"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOfficialOpenAIWSTurnMetadataFieldsMatchFullDecode(t *testing.T) {
	fixtures := []string{
		`{"model":"old","model":"last","reasoning":{"effort":"ULTRA"},"input":[{"text":"ignored"}]}`,
		`{"model":"\u0067pt","reasoning":{"effort":"low","effort":42}}`,
		`{"model":null,"reasoning":{"effort":18446744073709551615}}`,
		`{"model":42,"reasoning":{"effort":18446744073709551616}}`,
		`{"reasoning":{"effort":1e3},"input":[]}`,
		`{"reasoning":{"effort":""},"reasoning":{"effort":" high "}}`,
		`{"reasoning":{"effort":true},"model":{"unexpected":true}}`,
		`{"reasoning":"invalid","model":[1]}`,
		`{"model":"gpt","input":[`, `{"model":"gpt"}{}`, `null`, `[]`, `42`, ``,
	}
	for _, fixture := range fixtures {
		want, wantErr := decodeOfficialJSONObjectUseNumber([]byte(fixture))
		got, gotErr := decodeOfficialOpenAIWSTurnMetadataFields([]byte(fixture))
		require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr), fixture)
		require.Equal(t, officialOpenAIString(want, "model"), officialOpenAIString(got, "model"), fixture)
		for _, fallback := range []string{"", "ultra", " High "} {
			defaults := officialOpenAIReasoningDefaults{Effort: fallback}
			require.Equal(t, officialOpenAIEffectiveReasoningEffort(want, defaults), officialOpenAIEffectiveReasoningEffort(got, defaults), fixture)
		}
	}
}

func TestOfficialOpenAIWSTurnMetadataDoesNotCopyHistory(t *testing.T) {
	body := []byte(`{"model":"gpt","reasoning":{"effort":"high"},"input":[{"text":"` + strings.Repeat("x", 4<<20) + `"}]}`)
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	payload, err := decodeOfficialOpenAIWSTurnMetadataFields(body)
	runtime.ReadMemStats(&after)
	require.NoError(t, err)
	require.Equal(t, "gpt", payload["model"])
	require.Less(t, after.TotalAlloc-before.TotalAlloc, uint64(256<<10), "握手提取小字段不能复制历史正文")
}
