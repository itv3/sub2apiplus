package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
	"weak"

	"github.com/klauspost/compress/zstd"
)

func requestBodySpoolTestDirectory(t *testing.T) string {
	t.Helper()
	directory := t.TempDir()
	if !requestBodySpoolAllowed(directory) {
		t.Skip("测试临时目录不支持普通文件正文暂存")
	}
	t.Setenv("TMPDIR", directory)
	return directory
}

func newRequestBodySpoolTestBody(t *testing.T, ctx context.Context, input []byte) RequestBody {
	t.Helper()
	writer := newRequestBodyOutputWriter(ctx)
	defer writer.abort()
	for offset := 0; offset < len(input); {
		end := min(len(input), offset+segmentedBodyBlockSize-3)
		if _, err := writer.Write(input[offset:end]); err != nil {
			t.Fatal(err)
		}
		offset = end
	}
	body, err := writer.finish()
	if err != nil {
		t.Fatal(err)
	}
	if len(input) > requestBodySpoolThreshold && body.state.spooled == nil {
		t.Fatal("大正文未切换为普通文件")
	}
	return body
}

func TestRequestBodySpoolPreservesReplayAndGuardWithoutMaterializing(t *testing.T) {
	directory := requestBodySpoolTestDirectory(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	want := bytes.Repeat([]byte("独立只读正文0123456789"), requestBodySpoolThreshold/10)
	body := newRequestBodySpoolTestBody(t, ctx, want)
	content := body.state.spooled
	if content.length != int64(len(want)) || body.ContentLength() != int64(len(want)) {
		t.Fatal("文件正文长度错误")
	}
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 0 {
		t.Fatalf("正文文件未立即解除目录链接：entries=%v err=%v", entries, err)
	}
	request := newSegmentedTestRequest(t, body)
	plain := newSegmentedTestRequest(t, NewSharedReplayableRequestBody(want))
	normalization := WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}
	expected, err := requestDigest(plain, normalization, WireProtocolHTTP)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := requestDigest(request, normalization, WireProtocolHTTP)
	if err != nil || digest != expected {
		t.Fatalf("暂存改变了请求摘要：%v", err)
	}
	var wait sync.WaitGroup
	wantHash := sha256.Sum256(want)
	for range 8 {
		wait.Go(func() {
			reader, err := request.GetBody()
			if err != nil {
				t.Error(err)
				return
			}
			defer reader.Close()
			hash := sha256.New()
			n, err := io.CopyBuffer(hash, reader, make([]byte, 7919))
			if err != nil || n != int64(len(want)) || !bytes.Equal(hash.Sum(nil), wantHash[:]) {
				t.Errorf("并发文件重放发生串扰：n=%d err=%v", n, err)
			}
		})
	}
	wait.Wait()
	if content.bytes != nil || body.state.segmented != nil {
		t.Fatal("摘要或并发重放物化了完整压缩正文")
	}
	assertReaderBytes(t, request.Body, want)
	copyBody, ok := body.ReplayableBytes()
	if !ok || !bytes.Equal(copyBody, want) {
		t.Fatal("公开正文读取失败")
	}
	clear(copyBody)
	reader, _, err := body.clone().openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	assertReaderBytes(t, reader, want)
	if content.bytes != nil {
		t.Fatal("公开副本读取额外缓存了另一份完整正文")
	}
}

func TestRequestBodySpoolThresholdAndMemoryFallback(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	for _, size := range []int{0, requestBodySpoolThreshold, requestBodySpoolThreshold + 1} {
		writer := newRequestBodyOutputWriter(context.Background())
		if _, err := writer.Write(bytes.Repeat([]byte("x"), size)); err != nil {
			t.Fatal(err)
		}
		body, err := writer.finish()
		if err != nil || (body.state.spooled != nil) != (size > requestBodySpoolThreshold) {
			t.Fatalf("正文存储阈值错误：size=%d err=%v", size, err)
		}
		body.closeOwnedReplayableStorage()
		writer.abort()
		if _, err := writer.Write([]byte("late")); !errors.Is(err, io.ErrClosedPipe) {
			t.Fatalf("完成后仍能修改正文：%v", err)
		}
	}
	writer := newRequestBodyOutputWriter(context.Background())
	writer.disabled = true
	defer writer.abort()
	if _, err := writer.Write(bytes.Repeat([]byte("x"), requestBodySpoolThreshold+1)); err != nil {
		t.Fatal(err)
	}
	body, err := writer.finish()
	if err != nil || body.state.spooled != nil || body.state.segmented == nil {
		t.Fatalf("不支持的文件系统未回退分段正文：%v", err)
	}
}

func TestRequestBodySpoolCancellationClosesWriterAndReplay(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	want := bytes.Repeat([]byte("c"), requestBodySpoolThreshold+1)
	ctx, cancel := context.WithCancel(context.Background())
	writer := newRequestBodyOutputWriter(ctx)
	defer writer.abort()
	if _, err := writer.Write(want); err != nil {
		t.Fatal(err)
	}
	resource := writer.resource
	cancel()
	awaitRequestBodySpoolClosed(t, resource)
	if _, err := writer.finish(); !errors.Is(err, context.Canceled) {
		t.Fatalf("编译中取消未传播 context 错误：%v", err)
	}
	ctx, cancel = context.WithCancel(context.Background())
	body := newRequestBodySpoolTestBody(t, ctx, want)
	request := newSegmentedTestRequest(t, body)
	reader, err := request.GetBody()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	cancel()
	awaitRequestBodySpoolClosed(t, body.state.spooled.resource)
	if _, err := request.GetBody(); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消后 GetBody 未返回原始 context 错误：%v", err)
	}
	if _, err := reader.Read(make([]byte, 1)); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消后活动 reader 未停止：%v", err)
	}
	if body, ok := body.ReplayableBytes(); ok || body != nil {
		t.Fatal("取消后公开读取把存储失败伪装成成功正文")
	}
}

func TestRequestBodySpoolReaderAndContextDoNotRetainAbandonedOwner(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var resource *requestBodySpoolResource
	var reader io.ReadCloser
	var owner weak.Pointer[requestBodySpoolContent]
	func() {
		body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("k"), requestBodySpoolThreshold+1))
		resource = body.state.spooled.resource
		owner = weak.Make(body.state.spooled)
		var err error
		reader, _, err = body.openReplayable()
		if err != nil {
			t.Fatal(err)
		}
	}()
	// GC 只用于证明所有权，不进入正式编译、发送或测量达标逻辑。
	runtime.GC()
	if resource.closed.Load() || owner.Value() == nil {
		t.Fatal("活动 reader 未保活正文 owner")
	}
	if _, err := reader.Read(make([]byte, 17)); err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && !resource.closed.Load() {
		runtime.GC()
		runtime.Gosched()
		time.Sleep(time.Millisecond)
	}
	if !resource.closed.Load() || owner.Value() != nil {
		t.Fatal("未取消的 context 回调或 cleanup 参数仍强持有已丢弃 owner")
	}
	if ctx.Err() != nil {
		t.Fatal("兜底清理不应取消调用方 context")
	}
	runtime.KeepAlive(reader)
}

func TestRequestBodySpoolConcurrentReadCloseAndCancel(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("r"), requestBodySpoolThreshold+17))
	var wait sync.WaitGroup
	for range 8 {
		reader, _, err := body.openReplayable()
		if err != nil {
			t.Fatal(err)
		}
		wait.Go(func() {
			buffer := make([]byte, 37)
			for {
				if _, err := reader.Read(buffer); err != nil {
					if !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrClosedPipe) && !errors.Is(err, context.Canceled) {
						t.Errorf("并发关闭返回意外错误：%v", err)
					}
					return
				}
			}
		})
		wait.Go(func() { _ = reader.Close() })
	}
	cancel()
	wait.Wait()
}

func TestRequestBodySpoolStorageFailuresRemainTyped(t *testing.T) {
	directory := requestBodySpoolTestDirectory(t)
	filePath := filepath.Join(directory, "not-a-directory")
	if err := os.WriteFile(filePath, []byte("x"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TMPDIR", filePath)
	writer := newRequestBodyOutputWriter(context.Background())
	defer writer.abort()
	_, err := writer.Write(bytes.Repeat([]byte("f"), requestBodySpoolThreshold+1))
	var storageErr *RequestBodyStorageError
	if !errors.As(err, &storageErr) || storageErr.Operation != "create" {
		t.Fatalf("创建文件失败丢失存储错误类型：%v", err)
	}
	t.Setenv("TMPDIR", directory)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("f"), requestBodySpoolThreshold+1))
	request := newSegmentedTestRequest(t, body)
	if err := body.state.spooled.resource.file.Truncate(0); err != nil {
		t.Fatal(err)
	}
	_, err = requestDigest(request, WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}, WireProtocolHTTP)
	if !errors.As(err, &storageErr) || !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("摘要把损坏文件伪装成成功的空正文：%v", err)
	}
	metadata := attemptMetadata{Token: &FinalizationToken{payload: tokenPayload{
		Normalization: WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve},
	}}}
	guard := &Guard{}
	_, _, cause := guard.finalizationWireReasons(request, metadata, WireProtocolHTTP)
	rejection := &GuardRejectionError{cause: cause}
	if !errors.As(rejection, &storageErr) || !errors.Is(rejection, ErrGuardRejected) {
		t.Fatalf("Guard 拒绝丢失原始存储故障或既有拒绝标识：%v", rejection)
	}
}

func TestRequestBodySpoolCompressionRunsOnce(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	input := make([]byte, 2*requestBodySpoolThreshold)
	_, _ = rand.New(rand.NewSource(20261006)).Read(input)
	writes := 0
	body, err := compressRequestBodyZstdContext(ctx, 3, int64(len(input)), func(writer io.Writer) error {
		writes++
		return writeJSONDocumentPart(writer, input)
	})
	if err != nil || body.state.spooled == nil {
		t.Fatalf("流式压缩未采用文件正文：%v", err)
	}
	request := newSegmentedTestRequest(t, body)
	if _, err := requestDigest(request, WireNormalizationPlan{HeaderMode: HeaderNormalizationPreserve}, WireProtocolHTTP); err != nil {
		t.Fatal(err)
	}
	decoder, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	for range 3 {
		reader, err := request.GetBody()
		if err != nil {
			t.Fatal(err)
		}
		wire, err := io.ReadAll(reader)
		_ = reader.Close()
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := decoder.DecodeAll(wire, nil)
		if err != nil || !bytes.Equal(decoded, input) {
			t.Fatalf("文件重放改变了压缩帧：%v", err)
		}
		requireSingleStreamingZstdFrame(t, wire)
	}
	if writes != 1 {
		t.Fatalf("摘要或重放触发重复压缩：writes=%d", writes)
	}
}

func TestRequestBodySpoolExecuteReturnKeepsAsyncUploadReadable(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	want := bytes.Repeat([]byte("a"), requestBodySpoolThreshold+17)
	body := newRequestBodySpoolTestBody(t, ctx, want)
	executor, sinks, _, bundle := newExecutorForBodyTest(t, testExecutionPolicy())
	ctx, err := sinks.StartAttemptContext(ctx, SinkCodexResponsesForward)
	if err != nil {
		t.Fatal(err)
	}
	input := requestBodySpoolExecutorInput(bundle, body)
	result, err := executeSingleExecutorTestAttempt(ctx, executor, input)
	if err != nil {
		t.Fatal(err)
	}
	response := result.HTTPResponse()
	defer response.Body.Close()
	if body.state.spooled.resource.closed.Load() {
		t.Fatal("Execute 返回响应后提前关闭了仍可能上传的正文")
	}
	assertReaderBytes(t, response.Request.Body, want)
	reader, err := response.Request.GetBody()
	if err != nil {
		t.Fatalf("Execute 成功后提前撤销重放能力：%v", err)
	}
	assertReaderBytes(t, reader, want)
	cancel()
	awaitRequestBodySpoolClosed(t, body.state.spooled.resource)
}

// 不可取消的上游 context 可能携带正文工作区；即使文件 owner 仍在用于重放，
// cleanup 及其 stop 回调也不能把与资源无关的业务值延长到下一次 GC。
func TestRequestBodySpoolDetachedContextDoesNotRetainValues(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	type payloadContextKey struct{}
	type payloadContextValue struct{ body []byte }
	var owner RequestBody
	var original weak.Pointer[payloadContextValue]
	func() {
		value := &payloadContextValue{body: make([]byte, 2<<20)}
		original = weak.Make(value)
		ctx := context.WithValue(context.Background(), payloadContextKey{}, value)
		owner = newRequestBodySpoolTestBody(t, context.WithoutCancel(ctx), bytes.Repeat([]byte("x"), requestBodySpoolThreshold+1))
	}()
	runtime.GC()
	if original.Value() != nil {
		t.Fatal("文件 owner 或 cleanup 仍保活不可取消 context 的业务值")
	}
	reader, _, err := owner.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	owner.state.spooled.close()
	runtime.KeepAlive(owner)
}

func TestRequestBodySpoolPrepareAndExecuteFailureCloseImmediately(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	for _, failure := range []string{"prepare", "execute"} {
		t.Run(failure, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("e"), requestBodySpoolThreshold+1))
			executor, sinks, _, bundle := newExecutorForBodyTest(t, testExecutionPolicy())
			ctx, err := sinks.StartAttemptContext(ctx, SinkCodexResponsesForward)
			if err != nil {
				t.Fatal(err)
			}
			if failure == "prepare" {
				// 第一次摘要读取必须发现文件被截断并立即关闭 owner，不能等 GC 或取消。
				if err := body.state.spooled.resource.file.Truncate(0); err != nil {
					t.Fatal(err)
				}
			} else {
				adapter, err := NewHTTPUpstreamTransportAdapter(requestBodySpoolFailurePort{})
				if err != nil {
					t.Fatal(err)
				}
				executor.registry, err = NewAdapterRegistry(
					[]BackendDescriptor{{Backend: BackendHTTPUpstream, Protocol: WireProtocolHTTP, AdapterID: AdapterHTTPUpstream}},
					[]TransportAdapter{adapter},
				)
				if err != nil {
					t.Fatal(err)
				}
			}
			if _, err := executeSingleExecutorTestAttempt(ctx, executor, requestBodySpoolExecutorInput(bundle, body)); err == nil {
				t.Fatal("预期失败的 attempt 意外成功")
			}
			if !body.state.spooled.resource.closed.Load() || ctx.Err() != nil {
				t.Fatal("失败 attempt 未在 context 仍有效时立即释放文件")
			}
		})
	}
}

func requestBodySpoolExecutorInput(bundle ReleaseBundle, body RequestBody) ExecutorRequest {
	target, _ := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	return ExecutorRequest{
		Bundle: bundle,
		Plan: CodexEgressPlan{
			SinkID: SinkCodexResponsesForward, Purpose: "user_request.responses",
			EndpointID: "responses_http", Mode: ReleaseModeActive,
			Method: http.MethodPost, URL: target, Body: body,
			DeclaredPersona: PersonaCodexCLI, InvocationID: "spool-lifecycle-test",
		},
	}
}

type requestBodySpoolFailurePort struct{}

func (requestBodySpoolFailurePort) SendHTTPUpstream(_ context.Context, request PreparedRequest) (*http.Response, error) {
	_, err := request.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	return nil, errors.New("模拟传输失败")
}

func awaitRequestBodySpoolClosed(t *testing.T, resource *requestBodySpoolResource) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !resource.closed.Load() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if !resource.closed.Load() {
		t.Fatal("取消后暂存文件未及时关闭")
	}
}
