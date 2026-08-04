package officialegress

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/url"
	"sync"
	"testing"
)

func TestReplayableRequestBodyOwnsImmutableBacking(t *testing.T) {
	source := []byte(`{"model":"gpt-5"}`)
	body := NewReplayableRequestBody(source)
	source[2] = 'X'

	first, ok := body.ReplayableBytes()
	if !ok || string(first) != `{"model":"gpt-5"}` {
		t.Fatalf("构造方字节污染了内部 Body：%q", first)
	}
	first[2] = 'Y'
	second, ok := body.ReplayableBytes()
	if !ok || string(second) != `{"model":"gpt-5"}` {
		t.Fatalf("公开读取副本污染了内部 Body：%q", second)
	}
}

func TestReplayableRequestBodyConcurrentCloneIsReadOnly(t *testing.T) {
	want := bytes.Repeat([]byte("immutable-body-"), 1024)
	body := NewReplayableRequestBody(want)
	var wait sync.WaitGroup
	for range 32 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			for range 100 {
				got, ok := body.clone().ReplayableBytes()
				if !ok || !bytes.Equal(got, want) {
					t.Errorf("并发 clone 返回了不一致 Body")
					return
				}
			}
		}()
	}
	wait.Wait()
}

func TestRequestFromCompiledGetBodyIsRepeatable(t *testing.T) {
	want := []byte(`{"input":[{"role":"user","content":"hello"}]}`)
	target, err := url.Parse("https://example.com/v1/responses")
	if err != nil {
		t.Fatal(err)
	}
	compiled, err := NewCompiledRequest(
		http.MethodPost,
		target,
		http.Header{"Content-Type": []string{"application/json"}},
		NewReplayableRequestBody(want),
	)
	if err != nil {
		t.Fatal(err)
	}
	request, err := requestFromCompiled(context.Background(), compiled)
	if err != nil {
		t.Fatal(err)
	}
	assertReaderBytes(t, request.Body, want)
	for range 3 {
		reader, getErr := request.GetBody()
		if getErr != nil {
			t.Fatal(getErr)
		}
		assertReaderBytes(t, reader, want)
	}
}

func TestSingleUseRequestBodyCloneStillSharesConsumptionCapability(t *testing.T) {
	body, err := NewSingleUseRequestBody(io.NopCloser(bytes.NewReader([]byte("single"))), 6)
	if err != nil {
		t.Fatal(err)
	}
	cloned := body.clone()
	reader, _, _, err := cloned.takeSingleUse()
	if err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	if _, _, _, err := body.takeSingleUse(); err == nil {
		t.Fatal("single-use clone 错误地产生了第二次消费能力")
	}
}

func assertReaderBytes(t *testing.T, reader io.ReadCloser, want []byte) {
	t.Helper()
	defer func() { _ = reader.Close() }()
	got, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("读取 Body 不一致：got=%q want=%q", got, want)
	}
}
