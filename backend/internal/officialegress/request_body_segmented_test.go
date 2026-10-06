package officialegress

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"sync"
	"testing"
)

func TestSegmentedBodyWriterOwnsImmutableBoundedBlocks(t *testing.T) {
	for _, size := range []int{0, 1, segmentedBodyBlockSize - 1, segmentedBodyBlockSize, segmentedBodyBlockSize + 1, 3*segmentedBodyBlockSize + 17} {
		t.Run(strconv.Itoa(size), func(t *testing.T) {
			want := bytes.Repeat([]byte("x"), size)
			writer := newSegmentedBodyWriter()
			for offset := 0; offset < len(want); {
				end := min(len(want), offset+segmentedBodyBlockSize+3)
				input := bytes.Clone(want[offset:end])
				if n, err := writer.Write(input); err != nil || n != len(input) {
					t.Fatalf("分块写入失败：n=%d err=%v", n, err)
				}
				clear(input)
				offset = end
			}
			body := writer.finish()
			if body.Mode() != RequestBodyReplayable || body.ContentLength() != int64(size) {
				t.Fatalf("正文模式或长度错误：mode=%s length=%d", body.Mode(), body.ContentLength())
			}
			content := body.state.segmented
			for i, segment := range content.segments {
				if len(segment) == 0 || len(segment) > segmentedBodyBlockSize || cap(segment) != len(segment) {
					t.Fatalf("第 %d 块未保持有界只读容量：len=%d cap=%d", i, len(segment), cap(segment))
				}
			}
			if n, err := writer.Write([]byte("later")); n != 0 || !errors.Is(err, io.ErrClosedPipe) {
				t.Fatalf("finish 后仍可修改已交出的正文：n=%d err=%v", n, err)
			}
			if writer.finish().state.segmented != content || body.clone().state.segmented != content {
				t.Fatal("重复 finish 与 clone 应共享同一份不可变分段")
			}
			first, ok := body.ReplayableBytes()
			if !ok || !bytes.Equal(first, want) {
				t.Fatal("调用方复用写入缓冲污染了分段正文")
			}
			clear(first)
			second, ok := body.clone().ReplayableBytes()
			if !ok || !bytes.Equal(second, want) {
				t.Fatal("公开返回值污染了共享正文")
			}
			if content.bytes != nil {
				t.Fatal("公开副本读取不应额外缓存一份连续正文")
			}
			view, ok := body.replayableView()
			if !ok || !bytes.Equal(view, want) {
				t.Fatal("显式连续视图与分段内容不一致")
			}
		})
	}
}

func TestSegmentedBodyReaderShortReadsAndClose(t *testing.T) {
	want := bytes.Repeat([]byte("0123456789"), segmentedBodyBlockSize/5)
	body := newSegmentedTestBody(t, want)
	reader, ok, err := body.openReplayable()
	if !ok || err != nil {
		t.Fatalf("分段正文没有重放能力：%v", err)
	}
	if n, err := reader.Read(nil); n != 0 || err != nil {
		t.Fatalf("零长度读取改变了 reader：n=%d err=%v", n, err)
	}
	var got bytes.Buffer
	for _, size := range []int{1, segmentedBodyBlockSize - 2, 3, 19} {
		buffer := make([]byte, size)
		if _, err := io.ReadFull(reader, buffer); err != nil {
			t.Fatalf("跨块短读失败：%v", err)
		}
		_, _ = got.Write(buffer)
	}
	if _, err := io.Copy(&got, reader); err != nil || !bytes.Equal(got.Bytes(), want) {
		t.Fatalf("短读后正文不完整：%v", err)
	}
	if n, err := reader.Read(make([]byte, 1)); n != 0 || err != io.EOF {
		t.Fatalf("正文结束未返回 EOF：n=%d err=%v", n, err)
	}
	segmented, ok := reader.(*segmentedBodyReader)
	if !ok {
		t.Fatalf("重放 reader 类型错误：%T", reader)
	}
	if segmented.segments != nil {
		t.Fatal("读取完毕后仍保留块引用")
	}
	if err := reader.Close(); err != nil {
		t.Fatal(err)
	}
	if n, err := reader.Read(make([]byte, 1)); n != 0 || !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("关闭后读取未返回关闭错误：n=%d err=%v", n, err)
	}
	replay, _, _ := body.openReplayable()
	assertReaderBytes(t, replay, want)
	partial, _, _ := body.openReplayable()
	if _, err := partial.Read(make([]byte, 3)); err != nil {
		t.Fatal(err)
	}
	_ = partial.Close()
	partialSegmented, ok := partial.(*segmentedBodyReader)
	if !ok {
		t.Fatalf("部分读取 reader 类型错误：%T", partial)
	}
	if partialSegmented.segments != nil {
		t.Fatal("提前关闭后仍保留块引用")
	}
}

func TestSegmentedBodyReaderAllowsConcurrentClose(t *testing.T) {
	body := newSegmentedTestBody(t, bytes.Repeat([]byte("a"), 3*segmentedBodyBlockSize))
	for range 16 {
		reader, _, _ := body.openReplayable()
		var wait sync.WaitGroup
		wait.Add(2)
		go func() {
			defer wait.Done()
			buffer := make([]byte, 37)
			for {
				if _, err := reader.Read(buffer); err != nil {
					if !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrClosedPipe) {
						t.Errorf("并发关闭时读取错误：%v", err)
					}
					return
				}
			}
		}()
		go func() {
			defer wait.Done()
			_ = reader.Close()
		}()
		wait.Wait()
	}
}

func TestRequestFromCompiledSegmentedBodyReplaysWithoutMaterializing(t *testing.T) {
	for _, size := range []int{0, 1, 2*segmentedBodyBlockSize + 17} {
		t.Run(strconv.Itoa(size), func(t *testing.T) {
			want := bytes.Repeat([]byte("w"), size)
			body := newSegmentedTestBody(t, want)
			request := newSegmentedTestRequest(t, body)
			if request.ContentLength != int64(size) || request.GetBody == nil {
				t.Fatal("HTTP 请求未携带精确长度或独立重放能力")
			}
			if size == 0 && request.Body != http.NoBody {
				t.Fatal("空正文必须使用 http.NoBody，避免触发未知长度传输")
			}
			var wire bytes.Buffer
			if err := request.Write(&wire); err != nil {
				t.Fatal(err)
			}
			wireRequest, err := http.ReadRequest(bufio.NewReader(&wire))
			if err != nil {
				t.Fatal(err)
			}
			if wireRequest.ContentLength != int64(size) || len(wireRequest.TransferEncoding) != 0 {
				t.Fatalf("wire 未保留 Content-Length：length=%d encoding=%v", wireRequest.ContentLength, wireRequest.TransferEncoding)
			}
			assertReaderBytes(t, wireRequest.Body, want)
			wantDigest := sha256.Sum256(want)
			var wait sync.WaitGroup
			for range 16 {
				wait.Add(1)
				go func() {
					defer wait.Done()
					reader, openErr := request.GetBody()
					if openErr != nil {
						t.Errorf("重放打开失败：%v", openErr)
						return
					}
					defer func() { _ = reader.Close() }()
					hash := sha256.New()
					n, readErr := io.CopyBuffer(hash, reader, make([]byte, 127))
					if readErr != nil || n != int64(size) || !bytes.Equal(hash.Sum(nil), wantDigest[:]) {
						t.Errorf("并发重放正文不完整：n=%d err=%v", n, readErr)
					}
				}()
			}
			wait.Wait()
			if body.state.segmented.bytes != nil {
				t.Fatal("HTTP 发送或并发重放物化了连续正文")
			}
		})
	}
}

func TestSegmentedBodyPreservesGuardDigestAndMutationDetection(t *testing.T) {
	want := bytes.Repeat([]byte("guard-body"), segmentedBodyBlockSize/3)
	body := newSegmentedTestBody(t, want)
	request := newSegmentedTestRequest(t, body)
	contiguous := newSegmentedTestRequest(t, NewReplayableRequestBody(want))
	normalization := WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}
	digest, err := requestDigest(contiguous, normalization, WireProtocolHTTP)
	if err != nil {
		t.Fatal(err)
	}
	metadata := attemptMetadata{Token: &FinalizationToken{payload: tokenPayload{
		RequestDigest: digest, Normalization: normalization,
	}}}
	guard := &Guard{}
	if reasons, detail, _ := guard.finalizationWireReasons(request, metadata, WireProtocolHTTP); len(reasons) != 0 {
		t.Fatalf("分段重放改变了 Guard 摘要：reasons=%v detail=%s", reasons, detail)
	}
	assertReaderBytes(t, request.Body, want)
	if body.state.segmented.bytes != nil {
		t.Fatal("Guard 校验物化了连续正文")
	}
	request.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(want[:len(want)-1])), nil
	}
	if reasons, _, _ := guard.finalizationWireReasons(request, metadata, WireProtocolHTTP); !slices.Contains(reasons, ReasonRequestModifiedAfterFinalize) {
		t.Fatalf("重放正文被截断后 Guard 未拒绝：%v", reasons)
	}
}

func TestSingleUseBodyCannotOpenReplayable(t *testing.T) {
	body, err := NewSingleUseRequestBody(io.NopCloser(bytes.NewReader([]byte("single"))), 6)
	if err != nil {
		t.Fatal(err)
	}
	if reader, replayable, err := body.openReplayable(); replayable || reader != nil || err != nil {
		t.Fatal("single-use 正文错误获得重放能力")
	}
	reader, _, _, err := body.takeSingleUse()
	if err != nil {
		t.Fatalf("重放探测错误消耗了 single-use 能力：%v", err)
	}
	assertReaderBytes(t, reader, []byte("single"))
}

func newSegmentedTestBody(t *testing.T, body []byte) RequestBody {
	t.Helper()
	writer := newSegmentedBodyWriter()
	if n, err := writer.Write(body); err != nil || n != len(body) {
		t.Fatalf("构造分段正文失败：n=%d err=%v", n, err)
	}
	return writer.finish()
}

func newSegmentedTestRequest(t *testing.T, body RequestBody) *http.Request {
	t.Helper()
	target, err := url.Parse("https://example.com/v1/responses")
	if err != nil {
		t.Fatal(err)
	}
	compiled, err := NewCompiledRequest(http.MethodPost, target, http.Header{"Content-Type": []string{"application/json"}}, body)
	if err != nil {
		t.Fatal(err)
	}
	request, err := requestFromCompiled(context.Background(), compiled)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = request.Body.Close() })
	return request
}
