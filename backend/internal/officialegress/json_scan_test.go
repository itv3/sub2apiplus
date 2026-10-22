package officialegress

import (
	"bytes"
	"math/rand"
	"strconv"
	"strings"
	"testing"
)

// 本文件锁定零分配顶层扫描器与 Decoder 路径在字段名、值区间、错误值上的逐项一致性。

func scanCompareDocument(t *testing.T, name string, source []byte) {
	t.Helper()
	fast, fastErr := newOrderedJSONDocument(source)
	slow, slowErr := newOrderedJSONDocumentWithDecoder(source)
	if (fastErr == nil) != (slowErr == nil) {
		t.Fatalf("%s：错误有无不一致 fast=%v slow=%v", name, fastErr, slowErr)
	}
	if slowErr != nil {
		if fastErr.Error() != slowErr.Error() {
			t.Fatalf("%s：错误文本不一致\nfast=%v\nslow=%v", name, fastErr, slowErr)
		}
		return
	}
	if len(fast.fields) != len(slow.fields) {
		t.Fatalf("%s：字段数不一致 fast=%d slow=%d", name, len(fast.fields), len(slow.fields))
	}
	for i := range fast.fields {
		if fast.fields[i].name != slow.fields[i].name || !bytes.Equal(fast.fields[i].value, slow.fields[i].value) {
			t.Fatalf("%s：字段 %d 不一致 fast=%q=%q slow=%q=%q", name, i,
				fast.fields[i].name, fast.fields[i].value, slow.fields[i].name, slow.fields[i].value)
		}
		if fast.fieldIndex[fast.fields[i].name] != slow.fieldIndex[slow.fields[i].name] {
			t.Fatalf("%s：fieldIndex 不一致", name)
		}
	}
	if !fast.duplicatesChecked || !slow.duplicatesChecked || !bytes.Equal(fast.source, slow.source) {
		t.Fatalf("%s：document 状态不一致", name)
	}
}

var scanFixtures = []string{
	`{}`, ` { } `, `{"a":1}`, `{"a":{"b":[1,2,{"c":null}]},"d":[],"e":{}}`,
	`{"n":[0,-0,1.5,-1.5e10,1E-5,123456789012345678901234567890]}`,
	`{"s":"a\"b\\c\/d\b\f\n\r\t","u":"é😀","lone":"\ud800x"}`,
	`{"key":1,"k\"q":2,"中文":3,"":4}`,
	"{\"bad\":\"\xff\",\"\xff\":\"key\",\"ok\":\"\xe4\xb8\xad\"}",
	"{\n\t\"a\" : [ 1 , 2 ] ,\r\n \"b\" : { \"c\" : \"d\" , \"c\" : 5 } \n}\n",
	`{"input":[{"type":"message","content":[{"type":"input_text","text":"hi"}]}],"metadata":{"x":"y"}}`,
	`{"a":1,"a":2}`, `{"key":1,"key":2}`, `{"a":1,"b":2,"a":3}`, `{"b":{"a":1,"a":2},"b":1}`,
	``, ` `, `{`, `}`, `{"a":1`, `{"a":1,}`, `{,}`, `{"a"}`, `{"a":}`, `{a:1}`, `{"a":1}x`, `{"a":1} {"b":2}`,
	`{"a":01}`, `{"a":1.}`, `{"a":.5}`, `{"a":+1}`, `{"a":1e}`, `{"a":NaN}`, `{"a":tru}`, `{"a":"\x"}`, `{"a":"\'"}`,
	`{"a":"\u12"}`, "{\"a\":\"tab\there\"}", `{"a":[1,]}`, `{"a":[1 2]}`, "\ufeff{}", `null`, `[1]`, `"s"`, `1`,
	`{"a":"unterminated}`, `{"a":1}}`, `{"a":1]`, `{"a" 1}`, `{"a"::1}`, `{"a":1,,"b":2}`, "{\"a\":1}\x00",
	`{"a":1,"a":}`, `{"a":,"a":1}`, `{"a":1,"b":2,"b":3,"c":x}`,
}

func TestOrderedJSONScannerMatchesDecoderOnFixtures(t *testing.T) {
	for i, fixture := range scanFixtures {
		scanCompareDocument(t, "fixture#"+strconv.Itoa(i), []byte(fixture))
	}
	for _, depth := range []int{9999, 10000, 10001, 10002} {
		body := []byte(`{"a":` + strings.Repeat("[", depth-1) + strings.Repeat("]", depth-1) + `}`)
		scanCompareDocument(t, "depth "+strconv.Itoa(depth), body)
	}
}

func scanRandomJSON(rng *rand.Rand, depth int) string {
	space := func() string { return []string{"", " ", "\n", "\t ", "\r\n"}[rng.Intn(5)] }
	str := func() string {
		var b strings.Builder
		_ = b.WriteByte('"')
		for n := rng.Intn(6); n > 0; n-- {
			switch rng.Intn(9) {
			case 0:
				_, _ = b.WriteString(`\"`)
			case 1:
				_, _ = b.WriteString(`\\`)
			case 2:
				_, _ = b.WriteString(`\n`)
			case 3:
				_, _ = b.WriteString(`é`)
			case 4:
				_, _ = b.WriteString(`😀`)
			case 5:
				_, _ = b.WriteString(`\ud800`)
			case 6:
				_, _ = b.WriteString("\xff")
			case 7:
				_, _ = b.WriteString("中")
			default:
				_ = b.WriteByte(byte('a' + rng.Intn(26)))
			}
		}
		_ = b.WriteByte('"')
		return b.String()
	}
	if depth > 4 {
		return []string{"1", `"leaf"`, "null", "true", "-0.5e3"}[rng.Intn(5)]
	}
	switch rng.Intn(8) {
	case 0:
		return "null"
	case 1:
		return "true"
	case 2:
		return "false"
	case 3:
		return []string{"0", "-0", "12", "-7.25", "1e3", "1E+2", "0.001"}[rng.Intn(7)]
	case 4:
		return str()
	case 5:
		var b strings.Builder
		_, _ = b.WriteString("[")
		n := rng.Intn(4)
		for i := 0; i < n; i++ {
			if i > 0 {
				_, _ = b.WriteString(",")
			}
			_, _ = b.WriteString(space() + scanRandomJSON(rng, depth+1) + space())
		}
		_, _ = b.WriteString("]")
		return b.String()
	default:
		var b strings.Builder
		_, _ = b.WriteString("{")
		n := rng.Intn(4)
		keys := []string{`"a"`, `"b"`, `"key"`, `"key"`, `"中"`, "\"\xff\"", `""`}
		for i := 0; i < n; i++ {
			if i > 0 {
				_, _ = b.WriteString(",")
			}
			_, _ = b.WriteString(space() + keys[rng.Intn(len(keys))] + space() + ":" + space() + scanRandomJSON(rng, depth+1) + space())
		}
		_, _ = b.WriteString("}")
		return b.String()
	}
}

func scanMutate(rng *rand.Rand, body []byte) []byte {
	out := append([]byte(nil), body...)
	alphabet := []byte("{}[]\",:\\ 0123456789abtfnu-+.eE\n\x00\xff'")
	for n := 1 + rng.Intn(3); n > 0; n-- {
		if len(out) == 0 {
			return out
		}
		pos := rng.Intn(len(out))
		switch rng.Intn(3) {
		case 0:
			out = append(out[:pos], out[pos+1:]...)
		case 1:
			out = append(out[:pos], append([]byte{alphabet[rng.Intn(len(alphabet))]}, out[pos:]...)...)
		default:
			out[pos] = alphabet[rng.Intn(len(alphabet))]
		}
	}
	return out
}

func TestOrderedJSONScannerMatchesDecoderOnRandomDocuments(t *testing.T) {
	rng := rand.New(rand.NewSource(20260908))
	for round := 0; round < 3000; round++ {
		body := []byte(scanRandomJSON(rng, 0))
		scanCompareDocument(t, "random#"+strconv.Itoa(round), body)
		scanCompareDocument(t, "mutated#"+strconv.Itoa(round), scanMutate(rng, body))
	}
}

func TestOrderedJSONScannerDoesNotAmplifyLargeBody(t *testing.T) {
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.6-codex","instructions":"`)
	_, _ = b.WriteString(strings.Repeat("i", 64*1024))
	_, _ = b.WriteString(`","input":[`)
	for i := 0; i < 32; i++ {
		if i > 0 {
			_, _ = b.WriteString(",")
		}
		_, _ = b.WriteString(`{"type":"message","role":"user","content":[{"type":"input_text","text":"`)
		_, _ = b.WriteString(strings.Repeat("x", 128*1024))
		_, _ = b.WriteString(`"}]}`)
	}
	_, _ = b.WriteString(`],"metadata":{"a":"b"}}`)
	source := []byte(b.String())
	// 满载时其它协程的分配会混进单次窗口（上限只有 64 KiB），改用 sharedBodyMeasureAllocated：窗口内只留一个 P、取多次最小值。
	measure := func(run func()) float64 {
		return float64(sharedBodyMeasureAllocated(run))
	}
	fast := measure(func() {
		if _, err := newOrderedJSONDocument(source); err != nil {
			t.Fatal(err)
		}
	})
	slow := measure(func() {
		if _, err := newOrderedJSONDocumentWithDecoder(source); err != nil {
			t.Fatal(err)
		}
	})
	t.Logf("正文 %.1f MiB：扫描器 %.3f MiB，Decoder %.1f MiB", float64(len(source))/1024/1024, fast/1024/1024, slow/1024/1024)
	if fast > 64*1024 {
		t.Fatalf("扫描器不应随正文放大分配：%.3f MiB", fast/1024/1024)
	}
}
