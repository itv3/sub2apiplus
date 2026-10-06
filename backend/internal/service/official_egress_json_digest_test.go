package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"math"
	"math/rand"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOfficialJSONStreamingDigestMatchesEncoder(t *testing.T) {
	allBytes := make([]byte, 256)
	for i := range allBytes {
		allBytes[i] = byte(i)
	}
	values := []any{
		nil, true, false, string(allBytes), "<>&\u2028\u2029中文😀",
		map[string]any{"\xff": "bad", "\xfe": "bad2", "a": "a"},
		[]any{}, []any(nil), map[string]any{}, map[string]any(nil),
		json.Number("9007199254740993"), json.Number("-0.00e+2"), json.Number("broken"),
		1.25, math.NaN(), json.RawMessage(` {"x":"<&"} `), make(chan int),
		struct{ Value string }{Value: "兼容结构体"},
	}
	rng := rand.New(rand.NewSource(202610062))
	for i := 0; i < 600; i++ {
		value, err := decodeOfficialJSONValueUseNumber([]byte(decodeRandomJSON(rng, 0)))
		require.NoError(t, err)
		values = append(values, value)
	}
	deep := any("deep")
	for range 270 {
		deep = []any{deep}
	}
	values = append(values, deep)
	cycle := map[string]any{}
	cycle["self"] = cycle
	values = append(values, cycle)
	for i, value := range values {
		var encoded bytes.Buffer
		encoder := json.NewEncoder(&encoded)
		encoder.SetEscapeHTML(false)
		wantErr := encoder.Encode(value)
		got, gotErr := digestOfficialJSONValue(value)
		if wantErr != nil {
			require.EqualError(t, gotErr, wantErr.Error(), "样本 %d", i)
		} else {
			require.NoError(t, gotErr)
			require.Equal(t, officialContentDigest(sha256.Sum256(encoded.Bytes())), got, "样本 %d", i)
		}
	}
}
