package officialegress

import (
	"bytes"
	"errors"
	"io"
	"math/rand"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/klauspost/compress/zstd"
)

// 流式压缩会改变多块帧的编码组织，因此本文件分别验证原文逐字节保真、帧参数、
// 单帧边界、校验和与长度。不能仅把解压结果转成 JSON 对象比较。
func TestStreamingBodyZstdPreservesBytesAndFrameContract(t *testing.T) {
	decoder, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	for name, input := range zstdTestInputs(true) {
		t.Run(name, func(t *testing.T) {
			body, err := compressRequestBodyZstd(3, int64(len(input)), func(writer io.Writer) error {
				// 刻意使写入边界与 128 KiB 压缩块不对齐，防止字段边界影响正文。
				for rest := input; len(rest) > 0; {
					n := min(len(rest), 7919)
					if err := writeJSONDocumentPart(writer, rest[:n]); err != nil {
						return err
					}
					rest = rest[n:]
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			wire, ok := body.ReplayableBytes()
			if !ok || body.ContentLength() != int64(len(wire)) {
				t.Fatalf("压缩结果不可重放或长度不符：length=%d actual=%d", body.ContentLength(), len(wire))
			}
			legacy, err := compressCompiledBodyZstd(3, input)
			if err != nil {
				t.Fatal(err)
			}
			if len(input) == 0 {
				if len(wire) != 0 || !bytes.Equal(wire, legacy) {
					t.Fatal("空正文必须保持旧编码器的空输出")
				}
				return
			}
			decoded, err := decoder.DecodeAll(wire, nil)
			if err != nil || !bytes.Equal(decoded, input) {
				t.Fatalf("解压后原始字节不一致：%v", err)
			}
			var previous zstd.Header
			if err := previous.Decode(legacy); err != nil {
				t.Fatal(err)
			}
			current := requireSingleStreamingZstdFrame(t, wire)
			if current.HasFCS != previous.HasFCS || current.FrameContentSize != previous.FrameContentSize ||
				current.HasCheckSum != previous.HasCheckSum || current.DictionaryID != previous.DictionaryID {
				t.Fatalf("流式帧改变了原文长度、校验和或字典：previous=%+v current=%+v", previous, current)
			}
			if len(input) < zstdRawBlockBytes {
				if !bytes.Equal(wire, legacy) {
					t.Fatal("单块小正文的压缩字节发生变化")
				}
			} else {
				if current.SingleSegment {
					t.Fatal("多块流式帧不应声明 SingleSegment")
				}
				// 大正文必须精确声明 512KiB；较小正文允许编码器按长度缩小帧窗口。
				minimumWindow := min(uint64(len(input)), uint64(512<<10))
				if current.WindowSize < minimumWindow || current.WindowSize > 512<<10 {
					t.Fatalf("正式流式帧窗口不满足 512KiB 上限与正文长度：window=%d length=%d", current.WindowSize, len(input))
				}
				// EncodeAll 对照保留旧默认窗口，不得通过同步改动两侧绕过窗口兼容性检查。
				if !previous.SingleSegment && previous.WindowSize != 8<<20 {
					t.Fatalf("旧 EncodeAll 对照不再使用 8MiB 窗口：window=%d", previous.WindowSize)
				}
			}
			// 既检查声明了校验和，也检查损坏的校验和确实会导致解码失败。
			if current.HasCheckSum {
				wire[len(wire)-1] ^= 1
				if _, err := decoder.DecodeAll(wire, nil); err == nil {
					t.Fatal("损坏的帧校验和未被拒绝")
				}
			}
		})
	}
}

func TestStreamingBodyZstdReducedWindowAllocation(t *testing.T) {
	input := make([]byte, 4<<20)
	_, _ = rand.New(rand.NewSource(20261006)).Read(input)
	var previous, current RequestBody
	previousAllocated := sharedBodyMeasureAllocated(func() {
		output := newSegmentedBodyWriter()
		encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault),
			zstd.WithEncoderConcurrency(1), zstd.WithLowerEncoderMem(true), zstd.WithWindowSize(8<<20))
		if err != nil {
			t.Fatal(err)
		}
		encoder.ResetContentSize(output, int64(len(input)))
		if _, err := encoder.Write(input); err != nil {
			t.Fatal(err)
		}
		if err := encoder.Close(); err != nil {
			t.Fatal(err)
		}
		previous = output.finish()
	})
	currentAllocated := sharedBodyMeasureAllocated(func() {
		var err error
		current, err = compressRequestBodyZstdWithCache(3, int64(len(input)), func(writer io.Writer) error {
			return writeJSONDocumentPart(writer, input)
		}, nil)
		if err != nil {
			t.Fatal(err)
		}
	})
	previousWire, _ := previous.ReplayableBytes()
	currentWire, _ := current.ReplayableBytes()
	previousHeader := requireSingleStreamingZstdFrame(t, previousWire)
	currentHeader := requireSingleStreamingZstdFrame(t, currentWire)
	if previousHeader.WindowSize != 8<<20 || currentHeader.WindowSize != 512<<10 {
		t.Fatalf("窗口对照不符：previous=%d current=%d", previousHeader.WindowSize, currentHeader.WindowSize)
	}
	t.Logf("4MiB 难压缩正文：8MiB 窗口累计分配 %.2fMiB，正式 512KiB 窗口无缓存 %.2fMiB",
		float64(previousAllocated)/(1<<20), float64(currentAllocated)/(1<<20))
	if currentAllocated+(7<<20) >= previousAllocated {
		t.Fatal("512KiB 窗口未减少预期的编码器固定分配")
	}
}

// requireSingleStreamingZstdFrame 按块头计算首帧的结束位置，防止输出中混入第二个帧、
// 可跳过帧、被截断的块或尾随字节；实际块内容与 CRC 交给解码器验证。
func requireSingleStreamingZstdFrame(t *testing.T, wire []byte) zstd.Header {
	t.Helper()
	var header zstd.Header
	if err := header.Decode(wire); err != nil || header.Skippable || !header.FirstBlock.OK {
		t.Fatalf("zstd 首帧非法：header=%+v error=%v", header, err)
	}
	position := header.HeaderSize
	for {
		if position+3 > len(wire) {
			t.Fatal("zstd 块头被截断")
		}
		block := uint32(wire[position]) | uint32(wire[position+1])<<8 | uint32(wire[position+2])<<16
		position += 3
		kind, size := (block>>1)&3, int(block>>3)
		if kind == 3 {
			t.Fatal("zstd 块类型使用了保留值")
		}
		if kind == 1 {
			size = 1
		}
		position += size
		if position > len(wire) {
			t.Fatal("zstd 块内容被截断")
		}
		if block&1 == 1 {
			break
		}
	}
	if header.HasCheckSum {
		position += 4
	}
	if position != len(wire) {
		t.Fatalf("压缩结果必须恰好包含一个完整帧：frame=%d wire=%d", position, len(wire))
	}
	return header
}

func TestStreamingEndpointBodyMatchesUncompressedWireBytes(t *testing.T) {
	endpoint := profilecontract.ExecutableEndpointProfile{
		ID: "streaming_test", Compression: profilecontract.CompressionZstdWhenFeatureEnabled,
		Body: profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyJson, Closed: true,
			Fields: []profilecontract.BodyFieldProfile{
				{Name: "model", Required: true},
				{Name: "instructions", OmitWhen: profilecontract.OmitEmptyString},
				{Name: "input", Required: true},
				{Name: "number"},
			},
		},
	}
	input := []byte(`{ "number":1.2300e+04,"input":[{"text":"` + strings.Repeat(`原始\u0061\/\"内容`, 16000) +
		`", "nested": { "z" : 1, "a" : true }}],"instructions":"","model":"gpt" }`)
	features := profilecontract.FeatureDefaults{EnableRequestCompression: true, RequestCompressionLevel: 3}
	compile := func(headers http.Header) RequestBody {
		body, err := compileEndpointBody(endpoint, features, profilecontract.OptionalSections{}, headers,
			NewSharedReplayableRequestBody(input), BodyRuntimeConditions{}, AttemptAuthenticationInput{}, CodexIdentityFacts{})
		if err != nil {
			t.Fatal(err)
		}
		return body
	}
	plain, ok := compile(nil).ReplayableBytes()
	if !ok {
		t.Fatal("未压缩正文不可重放")
	}
	compressed := compile(http.Header{"Content-Encoding": []string{"zstd"}})
	wire, ok := compressed.ReplayableBytes()
	if !ok {
		t.Fatal("流式压缩正文不可重放")
	}
	decoder, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	decoded, err := decoder.DecodeAll(wire, nil)
	if err != nil || !bytes.Equal(plain, decoded) {
		t.Fatalf("正式压缩路径改变了 JSON 线序、数值或转义字节：%v", err)
	}
	if !bytes.HasPrefix(decoded, []byte(`{"model":"gpt","input":`)) ||
		!bytes.HasSuffix(decoded, []byte(`,"number":1.2300e+04}`)) || bytes.Contains(decoded, []byte(`"instructions"`)) {
		t.Fatal("流式路径未应用画像排序和省略规则")
	}
	header := requireSingleStreamingZstdFrame(t, wire)
	if !header.HasFCS || header.FrameContentSize != uint64(len(plain)) {
		t.Fatal("帧原文长度不是完整定型 JSON 的准确长度")
	}
}

func TestStreamingBodyZstdRejectsWriteFailureAndLengthMismatch(t *testing.T) {
	wantErr := errors.New("正文来源中断")
	if body, err := compressRequestBodyZstd(3, 3, func(writer io.Writer) error {
		_, _ = writer.Write([]byte("abc"))
		return wantErr
	}); !errors.Is(err, wantErr) || body.state != nil {
		t.Fatalf("正文写入失败后不应产生可发送结果：%v", err)
	}
	if body, err := compressRequestBodyZstd(3, 10, func(writer io.Writer) error {
		return writeJSONDocumentPart(writer, []byte("abc"))
	}); err == nil || body.state != nil {
		t.Fatal("原文长度与声明不匹配时必须失败关闭")
	}
}

func TestStreamingBodyZstdLowMemoryKeepsFrameAndReducesFixedAllocation(t *testing.T) {
	compress := func(input []byte, lowMemory bool) RequestBody {
		output := newSegmentedBodyWriter()
		encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault),
			zstd.WithEncoderConcurrency(1), zstd.WithLowerEncoderMem(lowMemory))
		if err != nil {
			t.Fatal(err)
		}
		encoder.ResetContentSize(output, int64(len(input)))
		if _, err := encoder.Write(input); err != nil {
			t.Fatal(err)
		}
		if err := encoder.Close(); err != nil {
			t.Fatal(err)
		}
		return output.finish()
	}
	random := make([]byte, 4<<20)
	_, _ = rand.New(rand.NewSource(20261006)).Read(random)
	for name, input := range map[string][]byte{"small": []byte(`{"model":"gpt","input":[]}`), "random_4mib": random} {
		t.Run(name, func(t *testing.T) {
			var normal, reduced RequestBody
			normalAllocated := sharedBodyMeasureAllocated(func() { normal = compress(input, false) })
			reducedAllocated := sharedBodyMeasureAllocated(func() { reduced = compress(input, true) })
			normalWire, _ := normal.ReplayableBytes()
			reducedWire, _ := reduced.ReplayableBytes()
			if !bytes.Equal(normalWire, reducedWire) {
				t.Fatal("低内存选项改变了同一流式编码器的压缩字节")
			}
			normalHeader := requireSingleStreamingZstdFrame(t, normalWire)
			reducedHeader := requireSingleStreamingZstdFrame(t, reducedWire)
			if normalHeader != reducedHeader {
				t.Fatal("低内存选项改变了帧参数")
			}
			t.Logf("正文=%d 字节，普通流式累计分配=%.2f MiB，低内存流式=%.2f MiB，窗口=%d，原文长度=%d，压缩字节相同",
				len(input), float64(normalAllocated)/(1<<20), float64(reducedAllocated)/(1<<20),
				reducedHeader.WindowSize, reducedHeader.FrameContentSize)
			if name == "random_4mib" && reducedAllocated+(7<<20) >= normalAllocated {
				t.Fatal("低内存模式未减少预期的历史窗口冗余缓冲")
			}
		})
	}
}

type shortJSONDocumentWriter struct{}

func (shortJSONDocumentWriter) Write(part []byte) (int, error) { return len(part) - 1, nil }

func TestOrderedJSONDocumentStreamingMatchesExistingEncoder(t *testing.T) {
	document, err := newOrderedJSONDocument([]byte(`{"<name>":"\u4e2d\u6587","number":1.2300e+04,"array":[ 1, {"z":true, "a":null} ],"removed":false}`))
	if err != nil {
		t.Fatal(err)
	}
	document.omit("removed")
	document.set("新\n字段", []byte(`{"original":"\/\u003c"}`))
	names := []string{"missing", "number", "removed", "新\n字段", "array", "<name>"}
	var output bytes.Buffer
	if err := document.writeNames(&output, names); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(output.Bytes(), legacyEncodeNames(document, names)) || output.Len() != document.encodedNamesLength(names) {
		t.Fatal("分段 JSON 输出与旧编码器不一致或长度计算错误")
	}
	if err := document.writeNames(shortJSONDocumentWriter{}, names); !errors.Is(err, io.ErrShortWrite) {
		t.Fatalf("短写未失败关闭：%v", err)
	}
}
