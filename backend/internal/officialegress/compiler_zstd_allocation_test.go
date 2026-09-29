package officialegress

import (
	"bytes"
	"encoding/base64"
	"math/rand"
	"strconv"
	"strings"
	"testing"

	"github.com/klauspost/compress/zstd"
)

// 本文件锁定编译器 zstd 压缩的分配调整：编码器并发度固定为 1（问题四 M1）、输出缓冲按最坏长度的
// 81% 一次预留（问题四 M3-b，此前按最坏长度预留）。压缩输出必须与缺省参数新建编码器逐字节一致（含空
// 正文的 nil 返回），大正文不再按 GOMAXPROCS 预建编码器；可压缩正文不扩容输出缓冲，完全不可压缩的
// 正文最多扩容一次。

// legacyCompressCompiledBodyZstd 逐字保留改造前 compileEndpointBody 的压缩写法，只作差分基准。
func legacyCompressCompiledBodyZstd(level int, compiled []byte) ([]byte, error) {
	encoder, err := zstd.NewWriter(
		nil,
		zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(level)),
	)
	if err != nil {
		return nil, err
	}
	out := encoder.EncodeAll(compiled, nil)
	if err := encoder.Close(); err != nil {
		return nil, err
	}
	return out, nil
}

func zstdTestInputs(large bool) map[string][]byte {
	rng := rand.New(rand.NewSource(20261004))
	random := make([]byte, 1<<20)
	_, _ = rng.Read(random)
	inputs := map[string][]byte{
		"nil":         nil,
		"empty":       {},
		"tiny":        []byte(`{}`),
		"small":       []byte(`{"model":"gpt-5.6-luna","input":"hi"}`),
		"repetitive":  bytes.Repeat([]byte(`{"type":"message","content":"同样的内容 same content"}`), 4000),
		"block":       random[:128*1024],
		"block_plus":  random[:128*1024+1],
		"random_1mib": random,
	}
	if large {
		base64Text := []byte(base64.StdEncoding.EncodeToString(random))
		// 超过 8 MiB 窗口，覆盖历史窗口滑动与多块输出。
		inputs["base64_window"] = bytes.Repeat(base64Text, 7)
		inputs["mixed_window"] = append(bytes.Repeat(random, 5), bytes.Repeat([]byte(`{"a":"b"},`), 400000)...)
	}
	return inputs
}

func TestCompressCompiledBodyZstdMatchesDefaultEncoder(t *testing.T) {
	check := func(level int, name string, input []byte) {
		t.Helper()
		legacy, legacyErr := legacyCompressCompiledBodyZstd(level, input)
		current, currentErr := compressCompiledBodyZstd(level, input)
		label := name + " level=" + strconv.Itoa(level)
		if (legacyErr == nil) != (currentErr == nil) {
			t.Fatalf("%s：错误有无不一致 legacy=%v current=%v", label, legacyErr, currentErr)
		}
		if legacyErr != nil {
			return
		}
		if (legacy == nil) != (current == nil) || !bytes.Equal(legacy, current) {
			t.Fatalf("%s：压缩输出必须与缺省参数编码器逐字节一致（legacy=%d 字节，current=%d 字节）",
				label, len(legacy), len(current))
		}
	}
	for _, level := range []int{1, 3} {
		for name, input := range zstdTestInputs(true) {
			check(level, name, input)
		}
	}
	for _, level := range []int{2, 5, 9, 19, 22} {
		for name, input := range zstdTestInputs(false) {
			check(level, name, input)
		}
	}
}

func TestCompressCompiledBodyZstdDoesNotAmplifyLargeBody(t *testing.T) {
	rng := rand.New(rand.NewSource(20261005))
	random := make([]byte, 6<<20)
	_, _ = rng.Read(random)
	input := []byte(base64.StdEncoding.EncodeToString(random))
	var current []byte
	currentAllocated := sharedBodyMeasureAllocated(func() {
		var err error
		current, err = compressCompiledBodyZstd(3, input)
		if err != nil {
			t.Fatal(err)
		}
	})
	legacyAllocated := sharedBodyMeasureAllocated(func() {
		if _, err := legacyCompressCompiledBodyZstd(3, input); err != nil {
			t.Fatal(err)
		}
	})
	t.Logf("正文 %.1f MiB → 压缩 %.1f MiB：单编码器 + 预留输出 %.1f MiB，改造前 %.1f MiB",
		float64(len(input))/(1<<20), float64(len(current))/(1<<20),
		float64(currentAllocated)/(1<<20), float64(legacyAllocated)/(1<<20))
	// 允许量：一个编码器的历史窗口（8 MiB 窗口对应 16 MiB）与匹配表，加输出缓冲。该输入是没有 JSON 结构
	// 的纯 base64 文本，块按原样存储、压缩比约为 1，而问题四 M3-b 起输出按最坏长度的 81% 预留（这类
	// 非 JSON 内容不在原样存储估计范围内），追加末段会扩容一次：输出缓冲至多为预留加一次扩容后的容量。
	const encoderAllowance = 16<<20 + 4<<20
	if currentAllocated > uint64(len(input))*21/10+encoderAllowance {
		t.Fatalf("压缩分配 %d 字节，超过预留、一次扩容与单个编码器状态之和", currentAllocated)
	}
	if currentAllocated >= legacyAllocated {
		t.Fatalf("压缩分配未减少：%d >= %d", currentAllocated, legacyAllocated)
	}
}

// zstdReserveInterleavedBody 生成与测量形态同型的正文：每项约 13 KB 随机 base64 加密内容，项间插入 textBytes
// 字节可匹配的文本（重复的中英文字母表）；textBytes 为 0 时加密内容背靠背，之间只有少量 JSON 结构。
func zstdReserveInterleavedBody(rng *rand.Rand, target int, textBytes int) []byte {
	var b bytes.Buffer
	b.WriteString(`{"model":"gpt-5.6-luna","input":[`)
	raw := make([]byte, 10000)
	text := bytes.Repeat([]byte("abcdefghijklmnopqrstuvwxyz0123456789 执行测试检查输出结果并继续下一步 "), textBytes/60+1)[:textBytes]
	for i := 0; b.Len() < target; i++ {
		if i > 0 {
			b.WriteByte(',')
		}
		_, _ = rng.Read(raw)
		b.WriteString(`{"type":"reasoning","encrypted_content":"gAAAAAB`)
		b.WriteString(base64.StdEncoding.EncodeToString(raw))
		b.WriteString(`"},{"type":"message","content":"` + strconv.Itoa(i))
		b.Write(text)
		b.WriteString(`"}`)
	}
	b.WriteString(`]}`)
	return b.Bytes()
}

// TestCompressCompiledBodyZstdReservesBelowWorstCase 锁定问题四 M3-b 的输出预留：与足量文本交错的加密内容
// （测量形态）按 81% 预留且不扩容；加密内容背靠背与整段图片 data URL 按原样存储计、预留接近最坏长度且
// 不扩容；非 base64 的不可压缩长文本按 81% 预留、恰好扩容一次。全部输出与改造前逐字节一致。
func TestCompressCompiledBodyZstdReservesBelowWorstCase(t *testing.T) {
	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(3)), zstd.WithEncoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = encoder.Close() }()
	rng := rand.New(rand.NewSource(20261006))
	random := make([]byte, 6<<20)
	_, _ = rng.Read(random)
	symbols := []byte("!#$%&'()*,.:;<>?@[]^`{|}~ ")
	noisy := make([]byte, 6<<20)
	for i := range noisy {
		noisy[i] = symbols[rng.Intn(len(symbols))]
	}
	const encoderAllowance = 16<<20 + 4<<20
	cases := []struct {
		name       string
		input      []byte
		minPercent int
		maxPercent int
		growOnce   bool
	}{
		{name: "interleaved_text", input: zstdReserveInterleavedBody(rng, 8<<20, 3000), minPercent: 81, maxPercent: 82},
		{name: "back_to_back_base64", input: zstdReserveInterleavedBody(rng, 8<<20, 0), minPercent: 99, maxPercent: 100},
		{name: "image_data_url", input: []byte(`{"input":[{"type":"input_image","image_url":"data:image/png;base64,` +
			base64.StdEncoding.EncodeToString(random) + `"}]}`), minPercent: 99, maxPercent: 100},
		{name: "incompressible_text", input: []byte(`{"text":"` + string(noisy) + `"}`), minPercent: 81, maxPercent: 82, growOnce: true},
	}
	for _, tc := range cases {
		worst := encoder.MaxEncodedSize(len(tc.input))
		reservation := zstdOutputReservation(worst, len(tc.input), zstdLikelyIncompressibleBytes(tc.input))
		var out []byte
		allocated := sharedBodyMeasureAllocated(func() {
			var compressErr error
			out, compressErr = compressCompiledBodyZstd(3, tc.input)
			if compressErr != nil {
				t.Fatal(compressErr)
			}
		})
		legacy, err := legacyCompressCompiledBodyZstd(3, tc.input)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(legacy, out) {
			t.Fatalf("%s：压缩输出必须与改造前逐字节一致", tc.name)
		}
		t.Logf("%s：正文 %.1f MiB，压缩比 %.2f，最坏长度 %.1f MiB，预留 %.1f MiB（%.1f%%），结果容量 %.1f MiB，分配 %.1f MiB",
			tc.name, float64(len(tc.input))/(1<<20), float64(len(out))/float64(len(tc.input)), float64(worst)/(1<<20),
			float64(reservation)/(1<<20), float64(reservation)*100/float64(worst), float64(cap(out))/(1<<20),
			float64(allocated)/(1<<20))
		if reservation*100 < worst*tc.minPercent || reservation*100 > worst*tc.maxPercent {
			t.Fatalf("%s：预留 %d 字节应在最坏长度 %d 的 %d%%～%d%% 之间", tc.name, reservation, worst, tc.minPercent, tc.maxPercent)
		}
		if !tc.growOnce {
			if cap(out) != reservation {
				t.Fatalf("%s：不得扩容（输出 %d，容量 %d，预留 %d）", tc.name, len(out), cap(out), reservation)
			}
			if allocated > uint64(reservation)+encoderAllowance {
				t.Fatalf("%s：分配 %d 字节，超过一份预留输出加单个编码器状态", tc.name, allocated)
			}
			continue
		}
		// 超出预留只能扩容一次：一次扩容后的容量约为预留的 1.25 倍，两次则约 1.56 倍。
		if len(out) <= reservation || cap(out) > reservation*13/10 {
			t.Fatalf("%s：应恰好扩容一次（输出 %d，容量 %d，预留 %d）", tc.name, len(out), cap(out), reservation)
		}
		if allocated > uint64(reservation)+uint64(cap(out))+encoderAllowance {
			t.Fatalf("%s：分配 %d 字节，超过预留、一次扩容与单个编码器状态之和", tc.name, allocated)
		}
	}
	if zstdOutputReservation(12, 2, 0) != 12 || zstdOutputReservation(100, 90, 0) != 82 ||
		zstdOutputReservation(1000, 900, 0) != 829 || zstdOutputReservation(1000, 900, 900) != 1000 {
		t.Fatalf("预留边界不符合预期")
	}
}

func TestZstdLikelyIncompressibleBytes(t *testing.T) {
	long := strings.Repeat("QUJD", zstdIncompressibleStringMinBytes/4)
	medium := strings.Repeat("QUJD", zstdBase64StringMinBytes/4+100)
	backToBack := func(separator string) string {
		var b strings.Builder
		b.WriteString(`[`)
		for i := 0; i < 40; i++ {
			if i > 0 {
				b.WriteString(`,`)
			}
			b.WriteString(`{"e":"` + medium + `"` + separator + `}`)
		}
		b.WriteString(`]`)
		return b.String()
	}
	tight := backToBack("")
	loose := backToBack(`,"t":"` + strings.Repeat("文本 text ", 40) + `"`)
	textRich := `{"history":"` + strings.Repeat("可匹配的文本 matchable text ", 40000) + `","image":"` + long + `"}`
	whole := func(input string) int { return len(input) }
	cases := []struct {
		name  string
		input string
		want  int
	}{
		{name: "short", input: `{"a":"QUJD"}`, want: 0},
		{name: "long_base64", input: `{"a":"` + long + `"}`, want: whole(`{"a":"` + long + `"}`)},
		{name: "data_url", input: `{"a":"data:image/png;base64,` + long + `"}`, want: whole(`{"a":"data:image/png;base64,` + long + `"}`)},
		{name: "mostly_text", input: `{"a":"` + strings.Repeat("QUJD 中文", zstdIncompressibleStringMinBytes/8) + `"}`, want: 0},
		{name: "two_long_strings", input: `{"a":"` + long + `","b":"x","c":"` + long + `"}`, want: whole(`{"a":"` + long + `","b":"x","c":"` + long + `"}`)},
		{name: "escaped_quote_before", input: `{"t":"say \\"hi\\"","a":"` + long + `"}`, want: whole(`{"t":"say \\"hi\\"","a":"` + long + `"}`)},
		{name: "escaped_backslash_end", input: `{"t":"path\\\\","a":"` + long + `"}`, want: whole(`{"t":"path\\\\","a":"` + long + `"}`)},
		{name: "back_to_back_medium", input: tight, want: len(tight)},
		{name: "medium_with_text", input: loose, want: 0},
		{name: "long_in_text", input: textRich, want: len(long) + 2*zstdRawBlockBytes},
		{name: "unterminated", input: `{"a":"` + long, want: 0},
		{name: "empty", input: ``, want: 0},
	}
	for _, tc := range cases {
		if got := zstdLikelyIncompressibleBytes([]byte(tc.input)); got != tc.want {
			t.Fatalf("%s：估计 %d 字节，期望 %d", tc.name, got, tc.want)
		}
	}
}
