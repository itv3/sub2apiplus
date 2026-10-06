package httputil

import (
	"bytes"
	"compress/gzip"
	"compress/zlib"
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"

	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

func admittedTestEncode(t *testing.T, body []byte, encoding string) []byte {
	t.Helper()
	var output bytes.Buffer
	var writer io.WriteCloser
	switch encoding {
	case "zstd":
		encoder, err := zstd.NewWriter(&output, zstd.WithEncoderConcurrency(1))
		require.NoError(t, err)
		writer = encoder
	case "gzip":
		writer = gzip.NewWriter(&output)
	case "deflate":
		writer = zlib.NewWriter(&output)
	default:
		return body
	}
	_, err := writer.Write(body)
	require.NoError(t, err)
	require.NoError(t, writer.Close())
	return output.Bytes()
}

func TestReadAdmittedBodyLimitsAndDecoding(t *testing.T) {
	body := []byte(`{"input":"` + strings.Repeat("测试", 1200) + `"}`)
	for _, encoding := range []string{"", "identity", "zstd", "gzip", "deflate"} {
		t.Run(encoding, func(t *testing.T) {
			wire := admittedTestEncode(t, body, encoding)
			req := newRequestWithBody(t, wire, encoding)
			got, err := ReadAdmittedLenientJSONRequestBody(req, int64(len(body)))
			require.NoError(t, err)
			require.Equal(t, body, got)
			require.Equal(t, int64(len(body)), req.ContentLength)
			if encoding != "" && encoding != "identity" {
				require.Empty(t, req.Header.Get("Content-Encoding"))
			}
			_, err = ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, wire, encoding), int64(len(body)-1))
			var tooLarge *http.MaxBytesError
			require.ErrorAs(t, err, &tooLarge)
			require.Equal(t, int64(len(body)-1), tooLarge.Limit)
		})
	}
}

func TestReadAdmittedBodyKnownAndUnknownLengths(t *testing.T) {
	body := []byte(`{"input":"` + strings.Repeat("a", 2<<20) + `"}`)
	for _, length := range []int64{int64(len(body)), -1, 0} {
		req := newRequestWithBody(t, body, "")
		req.ContentLength = length
		got, err := ReadAdmittedLenientJSONRequestBody(req, int64(len(body)))
		require.NoError(t, err)
		require.Equal(t, body, got)
		require.Equal(t, len(got), cap(got), "大正文不能通过扩容保留多余容量")
	}
	for _, length := range []int64{int64(len(body) - 1), int64(len(body) + 1)} {
		req := newRequestWithBody(t, body, "")
		req.ContentLength = length
		_, err := ReadAdmittedLenientJSONRequestBody(req, int64(len(body)+10))
		require.Error(t, err, "上传长度必须符合已准入的 Content-Length")
	}
}

func TestReadAdmittedBodyCorruptionAndNormalization(t *testing.T) {
	for _, encoding := range []string{"zstd", "gzip", "deflate", "br"} {
		_, err := ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, []byte("损坏的数据"), encoding), 1024)
		require.Error(t, err)
	}
	body := []byte("{\"input\":\"a\nb\"}")
	_, err := ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, body, ""), int64(len(body)))
	var tooLarge *http.MaxBytesError
	require.True(t, errors.As(err, &tooLarge))
	req := newRequestWithBody(t, nil, "")
	req.Body = NewPrereadBody([]byte(samplePayload))
	got, err := ReadAdmittedLenientJSONRequestBody(req, 1024)
	require.NoError(t, err)
	require.Equal(t, samplePayload, string(got))
}

func TestAdmittedNormalizationReservesBeforeAllocating(t *testing.T) {
	body := []byte("{\"input\":\"" + strings.Repeat("\n", 64<<10) + "\"}")
	want := []byte(`{"input":"` + strings.Repeat(`\u000a`, 64<<10) + `"}`)
	rejected := errors.New("测试预算不足")
	var got []byte
	var err error
	var reservedBytes int
	reserve := func(size int) error {
		reservedBytes = size
		return rejected
	}
	allocations := testing.AllocsPerRun(3, func() {
		got, err = normalizeLenientJSONRequestBody(body, int64(len(want)), reserve)
	})
	require.ErrorIs(t, err, rejected)
	require.Nil(t, got)
	require.Equal(t, len(want), reservedBytes)
	require.Zero(t, allocations, "预算拒绝前不能分配规范化输出")

	got, err = NormalizeLenientJSONRequestBody(body, int64(len(want)))
	require.NoError(t, err)
	require.Equal(t, want, got)
	require.Equal(t, len(got), cap(got), "规范化只分配一次精确大小输出")

	for _, encoding := range []string{"identity", "gzip", "deflate", "zstd", "preread"} {
		t.Run(encoding, func(t *testing.T) {
			var req *http.Request
			if encoding == "preread" {
				req = newRequestWithBody(t, nil, "")
				req.Body = NewPrereadBody(body)
			} else {
				req = newRequestWithBody(t, admittedTestEncode(t, body, encoding), encoding)
			}
			calls := 0
			got, err := ReadAdmittedLenientJSONRequestBodyWithReservation(req, int64(len(want)), func(size int) error {
				calls++
				require.Equal(t, len(want), size)
				return rejected
			})
			require.ErrorIs(t, err, rejected)
			require.Nil(t, got)
			require.Equal(t, 1, calls)
		})
	}
}

type admittedBodyErrorReader struct{ err error }

func (r admittedBodyErrorReader) Read([]byte) (int, error) { return 0, r.err }

func TestReadAdmittedBodyCancelledBeforeReading(t *testing.T) {
	for _, encoding := range []string{"", "zstd", "gzip", "deflate"} {
		t.Run(encoding, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			req := newRequestWithBody(t, nil, encoding).WithContext(ctx)
			req.ContentLength = 32 << 20
			req.Body = io.NopCloser(admittedBodyErrorReader{err: errors.New("已取消的请求不应读取正文")})
			body, err := ReadAdmittedLenientJSONRequestBody(req, 64<<20)
			require.ErrorIs(t, err, context.Canceled)
			require.Nil(t, body)
		})
	}
}

type admittedBodyCancellingReader struct {
	reader io.Reader
	cancel context.CancelFunc
	reads  int
}

func (r *admittedBodyCancellingReader) Read(p []byte) (int, error) {
	r.reads++
	if r.reads == 3 {
		r.cancel()
	}
	return r.reader.Read(p)
}

func TestReadAdmittedUnknownBodyReleasesTemporaryFile(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	if !requestBodySpoolAllowed(directory) {
		t.Skip("当前临时目录是内存文件系统或平台不支持磁盘暂存")
	}
	body := bytes.Repeat([]byte{'a'}, 3<<20)
	uploadErr := errors.New("模拟上传中断")
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	for _, test := range []struct {
		name   string
		ctx    context.Context
		reader io.Reader
		err    error
	}{
		{name: "正常完成", ctx: t.Context(), reader: bytes.NewReader(body)},
		{name: "上传中断", ctx: t.Context(), reader: io.MultiReader(bytes.NewReader(body), admittedBodyErrorReader{err: uploadErr}), err: uploadErr},
		{name: "上传取消", ctx: ctx, reader: &admittedBodyCancellingReader{reader: bytes.NewReader(body), cancel: cancel}, err: context.Canceled},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := readAdmittedUnknownBody(test.ctx, test.reader)
			if test.err == nil {
				require.NoError(t, err)
				require.Equal(t, body, got)
			} else {
				require.ErrorIs(t, err, test.err)
				require.Nil(t, got)
			}
			entries, err := os.ReadDir(directory)
			require.NoError(t, err)
			require.Empty(t, entries, "正文临时文件必须在完成或失败后清理")
		})
	}
}

func TestReadAdmittedCompressedBodyLimitAfterSpilling(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	body := []byte(`{"input":"` + strings.Repeat("a", 3<<20) + `"}`)
	wire := admittedTestEncode(t, body, "gzip")
	_, err := ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, wire, "gzip"), 2<<20)
	var tooLarge *http.MaxBytesError
	require.ErrorAs(t, err, &tooLarge)
	entries, err := os.ReadDir(directory)
	require.NoError(t, err)
	require.Empty(t, entries)
}

func TestReadAdmittedCompressedBodyBoundsWindowAndWireTail(t *testing.T) {
	var wire bytes.Buffer
	encoder, err := zstd.NewWriter(&wire, zstd.WithEncoderConcurrency(1), zstd.WithWindowSize(32<<20))
	require.NoError(t, err)
	body := []byte(`{"input":"` + strings.Repeat("a", 256<<10) + `"}`)
	_, err = encoder.Write(body)
	require.NoError(t, err)
	require.NoError(t, encoder.Close())
	_, err = ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, wire.Bytes(), "zstd"), int64(len(body)))
	require.Error(t, err, "小请求不能通过声明巨大的解码窗口绕过内存准入")

	deflated := admittedTestEncode(t, []byte(samplePayload), "deflate")
	deflated = append(deflated, bytes.Repeat([]byte{'x'}, 2048)...)
	_, err = ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, deflated, "deflate"), 1024)
	var tooLarge *http.MaxBytesError
	require.ErrorAs(t, err, &tooLarge, "解码结束后仍要检查压缩传输尾部的字节上限")
}
