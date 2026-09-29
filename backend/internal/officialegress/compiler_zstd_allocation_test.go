package officialegress

import (
	"bytes"
	"encoding/base64"
	"math/rand"
	"strconv"
	"testing"

	"github.com/klauspost/compress/zstd"
)

// 本文件锁定编译器 zstd 压缩的两处分配调整（问题四 M1 实测中发现、方案未列出）：编码器并发度
// 固定为 1、输出缓冲按最坏长度一次预留。压缩输出必须与缺省参数新建编码器逐字节一致（含空
// 正文的 nil 返回），且大正文不再按 GOMAXPROCS 预建编码器、不再反复扩容输出缓冲。

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
	// 允许量：一份按最坏长度预留的输出、一个编码器的历史窗口（8 MiB 窗口对应 16 MiB）与匹配表。
	const encoderAllowance = 16<<20 + 4<<20
	if currentAllocated > uint64(len(input))+encoderAllowance {
		t.Fatalf("压缩分配 %d 字节，超过一份输出加单个编码器状态", currentAllocated)
	}
	if currentAllocated >= legacyAllocated {
		t.Fatalf("压缩分配未减少：%d >= %d", currentAllocated, legacyAllocated)
	}
}
