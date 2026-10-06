package officialegress

import (
	"bytes"
	"context"
	"errors"
	"io"
	"runtime"
	"sync"
	"testing"
	"weak"
)

func TestRequestBodyStorageScopeReleasesUnusedAttemptsAndKeepsActiveUpload(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, release := WithRequestBodyStorageScope(context.Background())
	defer release()
	want := bytes.Repeat([]byte("r"), requestBodySpoolThreshold+13)
	first := newRequestBodySpoolTestBody(t, ctx, want)
	second := newRequestBodySpoolTestBody(t, ctx, want)
	firstRequest := newSegmentedTestRequest(t, first)
	secondRequest := newSegmentedTestRequest(t, second)
	reader, err := firstRequest.GetBody()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if _, err := reader.Read(make([]byte, 37)); err != nil {
		t.Fatal(err)
	}
	assertReaderBytes(t, secondRequest.Body, want)
	// 响应头或一次 attempt 的 reader 关闭不能关闭同一整轮内的其他正文。
	if first.state.spooled.resource.closed.Load() || second.state.spooled.resource.closed.Load() {
		t.Fatal("整轮结束之前提前关闭了重放文件")
	}
	release()
	if first.state.spooled.resource.closed.Load() || !second.state.spooled.resource.closed.Load() {
		t.Fatal("正常结束必须同步释放无读者文件，并保留尚未完成的上传")
	}
	lateReader, err := firstRequest.GetBody()
	if err != nil {
		t.Fatalf("早响应后的活动上传无法新增 GetBody 读者：%v", err)
	}
	defer lateReader.Close()
	// 初始 Body 尚未读取，同样属于有效租约，不能只保护已经读过前缀的 reader。
	assertReaderBytes(t, firstRequest.Body, want)
	assertReaderBytes(t, reader, want[37:])
	if first.state.spooled.resource.closed.Load() {
		t.Fatal("关闭原上传与旧重放读者时提前释放了新 GetBody 仍持有的文件")
	}
	assertReaderBytes(t, lateReader, want)
	if !first.state.spooled.resource.closed.Load() {
		t.Fatal("最后一个 reader.Close 未同步释放文件")
	}
	if _, err := firstRequest.GetBody(); !errors.Is(err, context.Canceled) {
		t.Fatalf("最后租约结束后的 GetBody 未报告已关闭：%v", err)
	}
	if _, err := secondRequest.GetBody(); !errors.Is(err, context.Canceled) {
		t.Fatalf("范围结束后第二次 attempt 仍可重放：%v", err)
	}
}

func TestRequestBodyStorageScopeKeepsEOFReaderUntilClose(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, release := WithRequestBodyStorageScope(context.Background())
	defer release()
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("e"), requestBodySpoolThreshold+1))
	reader, _, err := body.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}
	release()
	if body.state.spooled.resource.closed.Load() {
		t.Fatal("EOF 不代表 transport 已关闭请求，不能提前撤销重试所需租约")
	}
	if err := reader.Close(); err != nil || !body.state.spooled.resource.closed.Load() {
		t.Fatalf("Close 后未同步释放 EOF 读者的文件：%v", err)
	}
}

func TestRequestBodyStorageScopeCancellationClosesActiveReadersImmediately(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	parent, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx, release := WithRequestBodyStorageScope(parent)
	defer release()
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("c"), requestBodySpoolThreshold+1))
	request := newSegmentedTestRequest(t, body)
	defer request.Body.Close()
	cancel()
	release()
	if !body.state.spooled.resource.closed.Load() {
		t.Fatal("取消退出必须同步关闭，不能等待尚未结束的上传租约")
	}
	if _, err := request.Body.Read(make([]byte, 1)); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消退出后活动读者仍可继续发送：%v", err)
	}
	if _, err := request.GetBody(); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消退出后仍可新建重放读者：%v", err)
	}
}

func TestRequestBodyStorageScopeDoesNotRetainCancelableBusinessContext(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	parent, cancel := context.WithCancel(context.Background())
	defer cancel()
	type businessKey struct{}
	type businessValue struct{ body []byte }
	var body RequestBody
	var release func()
	var reference weak.Pointer[businessValue]
	func() {
		value := &businessValue{body: bytes.Repeat([]byte("v"), requestBodySpoolThreshold+1)}
		reference = weak.Make(value)
		ctx, finish := WithRequestBodyStorageScope(context.WithValue(parent, businessKey{}, value))
		release = finish
		body = newRequestBodySpoolTestBody(t, ctx, value.body)
	}()
	defer release()
	for attempt := 0; attempt < 5 && reference.Value() != nil; attempt++ {
		runtime.GC()
	}
	if reference.Value() != nil {
		t.Fatal("存活正文或 cleanup 仍强持有可取消业务 context 的值")
	}
	reader, _, err := body.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	release()
	if !body.state.spooled.resource.closed.Load() {
		t.Fatal("独立范围结束后文件未关闭")
	}
	runtime.KeepAlive(parent)
	runtime.KeepAlive(body)
}

func TestRequestBodyStorageScopePreservesDetachedUpstreamCancellation(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	parent, cancel := context.WithCancel(context.Background())
	ctx, release := WithRequestBodyStorageScope(parent)
	defer release()
	ctx = context.WithoutCancel(ctx)
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("d"), requestBodySpoolThreshold+1))
	cancel()
	if ctx.Err() != nil || body.state.spooled.resource.closed.Load() {
		t.Fatal("存储范围重新连接了原本已解耦的入站取消")
	}
	reader, _, err := body.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	release()
	if !body.state.spooled.resource.closed.Load() {
		t.Fatal("整轮退出未关闭已解耦请求的暂存文件")
	}
}

func TestRequestBodyStorageScopeConcurrentReleaseAndReaderClose(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	ctx, release := WithRequestBodyStorageScope(context.Background())
	defer release()
	body := newRequestBodySpoolTestBody(t, ctx, bytes.Repeat([]byte("c"), requestBodySpoolThreshold+1))
	reader, _, err := body.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	group.Go(func() { _, _ = io.Copy(io.Discard, reader) })
	group.Go(func() { _ = reader.Close() })
	for range 4 {
		group.Go(func() {
			release()
		})
	}
	group.Wait()
	if !body.state.spooled.resource.closed.Load() {
		t.Error("scope 与最后读者都结束后文件未同步关闭")
	}
}

func TestRequestBodySpoolWriteCacheWindowIsBoundedBeforeFinish(t *testing.T) {
	requestBodySpoolTestDirectory(t)
	writer := newRequestBodyOutputWriter(context.Background())
	defer writer.abort()
	want := bytes.Repeat([]byte("s"), 3*requestBodySpoolThreshold+31)
	// 前缀迁移与一次超大 Write 都必须经过同一个有界写入窗口。
	if _, err := writer.Write(want[:requestBodySpoolThreshold-7]); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write(want[requestBodySpoolThreshold-7:]); err != nil {
		t.Fatal(err)
	}
	if writer.spoolWritten != int64(len(want)) || writer.spoolReleased != 3*requestBodySpoolThreshold {
		t.Fatalf("finish 之前未按窗口处理写缓存：written=%d released=%d", writer.spoolWritten, writer.spoolReleased)
	}
	body, err := writer.finish()
	if err != nil {
		t.Fatal(err)
	}
	defer body.closeOwnedReplayableStorage()
	reader, _, err := body.openReplayable()
	if err != nil {
		t.Fatal(err)
	}
	assertReaderBytes(t, reader, want)
}
