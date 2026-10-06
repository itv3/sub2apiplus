package officialegress

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"runtime"
	"sync"
	"testing"
	"weak"

	"github.com/klauspost/compress/zstd"
)

func compressZstdCacheTestInput(input []byte, cache *requestBodyZstdEncoderCache) (RequestBody, error) {
	return compressRequestBodyZstdWithCache(3, int64(len(input)), func(writer io.Writer) error {
		return writeJSONDocumentPart(writer, input)
	}, cache)
}

func cachedZstdTestEncoder(cache *requestBodyZstdEncoderCache) *zstd.Encoder {
	cache.mu.Lock()
	defer cache.mu.Unlock()
	return cache.idle.Value()
}

func TestRequestBodyZstdEncoderCacheMatchesFreshFrames(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	inputs := zstdTestInputs(false)
	inputs["multiple_windows"] = bytes.Repeat([]byte(`{"text":"跨窗口重复内容"}`), 90000)
	for round := 0; round < 3; round++ {
		for name, input := range inputs {
			fresh, err := compressZstdCacheTestInput(input, nil)
			if err != nil {
				t.Fatal(err)
			}
			reused, err := compressZstdCacheTestInput(input, cache)
			if err != nil {
				t.Fatal(err)
			}
			freshWire, _ := fresh.ReplayableBytes()
			reusedWire, _ := reused.ReplayableBytes()
			if !bytes.Equal(freshWire, reusedWire) {
				t.Fatalf("第 %d 轮 %s 复用编码器的压缩字节与新建编码器不同", round, name)
			}
		}
	}
}

func TestRequestBodyZstdEncoderCacheBoundAndLevelIsolation(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	first, err := cache.take(3)
	if err != nil {
		t.Fatal(err)
	}
	second, err := cache.take(3)
	if err != nil {
		t.Fatal(err)
	}
	cache.put(3, first)
	cache.put(3, second)
	if cachedZstdTestEncoder(cache) != first {
		t.Fatal("第二次归还不应替换或扩大已有空闲槽")
	}
	other, err := cache.take(1)
	if err != nil {
		t.Fatal(err)
	}
	if other == first || other == second {
		t.Fatal("不同压缩等级复用了同一个编码器")
	}
	cache.put(1, other)
	got, err := cache.take(3)
	if err != nil || got != first {
		t.Fatalf("其他等级改变了等级 3 的空闲槽：%v", err)
	}
}

func TestRequestBodyZstdEncoderCacheDiscardsFailedEncoder(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	if _, err := compressZstdCacheTestInput([]byte("warm"), cache); err != nil {
		t.Fatal(err)
	}
	wantErr := errors.New("来源写入失败")
	if _, err := compressRequestBodyZstdWithCache(3, 3, func(writer io.Writer) error {
		_, _ = writer.Write([]byte("abc"))
		return wantErr
	}, cache); !errors.Is(err, wantErr) {
		t.Fatalf("写入失败没有返回原始错误：%v", err)
	}
	if cachedZstdTestEncoder(cache) != nil {
		t.Fatal("写入失败的编码器不应归还缓存")
	}
	if _, err := compressRequestBodyZstdWithCache(3, 10, func(writer io.Writer) error {
		return writeJSONDocumentPart(writer, []byte("abc"))
	}, cache); err == nil || cachedZstdTestEncoder(cache) != nil {
		t.Fatal("长度校验失败的编码器不应归还缓存")
	}
	if _, err := compressZstdCacheTestInput([]byte("recovered"), cache); err != nil {
		t.Fatalf("失败之后的正常请求未恢复：%v", err)
	}
}

func TestRequestBodyZstdEncoderCacheClearsOnGC(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	func() {
		encoder, err := cache.take(3)
		if err != nil {
			t.Fatal(err)
		}
		cache.put(3, encoder)
		if cachedZstdTestEncoder(cache) != encoder {
			t.Fatal("正常编码器未进入空闲弱引用槽")
		}
		runtime.KeepAlive(encoder)
	}()
	runtime.GC()
	if cachedZstdTestEncoder(cache) != nil {
		t.Fatal("GC 后空闲编码器仍被缓存强引用")
	}
	if _, err := compressZstdCacheTestInput([]byte("after gc"), cache); err != nil {
		t.Fatalf("GC 清空缓存之后未能新建编码器：%v", err)
	}
}

func TestRequestBodyZstdEncoderCacheReleasesPreviousWriter(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	encoder, err := cache.take(3)
	if err != nil {
		t.Fatal(err)
	}
	reference := func() weak.Pointer[bytes.Buffer] {
		output := new(bytes.Buffer)
		reference := weak.Make(output)
		encoder.ResetContentSize(output, 3)
		if _, err := encoder.Write([]byte("abc")); err != nil {
			t.Fatal(err)
		}
		if err := encoder.Close(); err != nil {
			t.Fatal(err)
		}
		cache.put(3, encoder)
		return reference
	}()
	// 此处 GC 仅用于验证不可达引用；正式压缩与复用路径不主动触发 GC。
	for attempt := 0; attempt < 5; attempt++ {
		runtime.GC()
		if reference.Value() == nil {
			runtime.KeepAlive(encoder)
			runtime.KeepAlive(cache)
			return
		}
	}
	runtime.KeepAlive(encoder)
	runtime.KeepAlive(cache)
	t.Fatal("空闲编码器仍持有上一请求的输出 writer")
}

func TestRequestBodyZstdEncoderCacheConcurrentIsolation(t *testing.T) {
	cache := newRequestBodyZstdEncoderCache()
	const workers = 12
	inputs := make([][]byte, workers)
	expected := make([][]byte, workers)
	for i := range inputs {
		inputs[i] = bytes.Repeat([]byte(fmt.Sprintf("独立请求 %d 的正文", i)), 8000)
		fresh, err := compressZstdCacheTestInput(inputs[i], nil)
		if err != nil {
			t.Fatal(err)
		}
		expected[i], _ = fresh.ReplayableBytes()
	}
	start := make(chan struct{})
	errors := make(chan error, workers)
	var group sync.WaitGroup
	for i := range inputs {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			<-start
			for round := 0; round < 3; round++ {
				body, err := compressZstdCacheTestInput(inputs[index], cache)
				if err != nil {
					errors <- err
					return
				}
				wire, _ := body.ReplayableBytes()
				if !bytes.Equal(wire, expected[index]) {
					errors <- fmt.Errorf("并发请求 %d 第 %d 轮压缩字节串扰", index, round)
					return
				}
			}
		}(i)
	}
	close(start)
	group.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
}

// 基准保留无缓存对照；每次结果由不可变分段持有，编码器自身不保留结果。
func BenchmarkRequestBodyZstdEncoderCache(b *testing.B) {
	input := zstdReserveInterleavedBody(rand.New(rand.NewSource(20261006)), 4<<20, 2000)
	for _, reuse := range []bool{false, true} {
		b.Run(fmt.Sprintf("reuse_%t", reuse), func(b *testing.B) {
			var cache *requestBodyZstdEncoderCache
			if reuse {
				cache = newRequestBodyZstdEncoderCache()
				if _, err := compressZstdCacheTestInput(input, cache); err != nil {
					b.Fatal(err)
				}
			}
			b.ReportAllocs()
			b.SetBytes(int64(len(input)))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				body, err := compressZstdCacheTestInput(input, cache)
				if err != nil {
					b.Fatal(err)
				}
				runtime.KeepAlive(body)
			}
		})
	}
}
