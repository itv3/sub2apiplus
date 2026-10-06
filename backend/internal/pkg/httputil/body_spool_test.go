package httputil

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAdmittedBodyStreamReservesAndFinishesBeforeMaterializing(t *testing.T) {
	for _, disk := range []bool{false, true} {
		t.Run(map[bool]string{false: "内存回退", true: "磁盘暂存"}[disk], func(t *testing.T) {
			directory := t.TempDir()
			t.Setenv("TMPDIR", directory)
			if disk && !requestBodySpoolAllowed(directory) {
				t.Skip("当前临时目录不支持普通磁盘暂存")
			}
			body := bytes.Repeat([]byte{'x'}, 2<<20)
			reader := bytes.NewReader(body)
			var file *os.File
			factory := func() (*requestBodySpool, error) {
				if !disk {
					return nil, nil
				}
				spool, err := newRequestBodySpool()
				require.NoError(t, err)
				file = spool.file
				return spool, nil
			}
			finished := false
			rejected := errors.New("分配最终正文之前拒绝预算")
			got, err := readAdmittedBodyStream(t.Context(), reader, int64(len(body)), AdmittedBodyReadHooks{
				ReadDone: func() error {
					require.Zero(t, reader.Len())
					finished = true
					return nil
				},
				Reserve: func(reading, joining int64) error {
					require.GreaterOrEqual(t, reading, int64(len(body)-reader.Len()))
					if joining > 0 {
						require.True(t, finished, "必须先结束解码器生命周期，再申请输出分配")
						require.EqualValues(t, len(body), joining)
						return rejected
					}
					return nil
				},
			}, factory)
			require.ErrorIs(t, err, rejected)
			require.Nil(t, got)
			if file != nil {
				_, err := file.Stat()
				require.ErrorIs(t, err, os.ErrClosed)
			}
			entries, err := os.ReadDir(directory)
			require.NoError(t, err)
			require.Empty(t, entries)
		})
	}
}

func TestAdmittedBodyStreamStorageFailuresCloseFile(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	if !requestBodySpoolAllowed(directory) {
		t.Skip("当前临时目录不支持普通磁盘暂存")
	}
	body := bytes.Repeat([]byte{'x'}, admittedBodySpoolThreshold+1)
	for _, phase := range []string{"创建", "写入", "定位", "回读"} {
		t.Run(phase, func(t *testing.T) {
			var file *os.File
			factory := func() (*requestBodySpool, error) {
				if phase == "创建" {
					return nil, &RequestBodyStorageError{Err: errors.New("模拟磁盘写满")}
				}
				if phase == "写入" {
					var err error
					file, err = os.Open(os.DevNull)
					require.NoError(t, err)
					return &requestBodySpool{file: file}, nil
				}
				spool, err := newRequestBodySpool()
				require.NoError(t, err)
				file = spool.file
				return spool, nil
			}
			got, err := readAdmittedBodyStream(t.Context(), bytes.NewReader(body), int64(len(body)), AdmittedBodyReadHooks{
				ReadDone: func() error {
					if phase == "定位" {
						require.NoError(t, file.Close())
					}
					return nil
				},
				Reserve: func(_, joining int64) error {
					if phase == "回读" && joining > 0 {
						require.NoError(t, file.Close())
					}
					return nil
				},
			}, factory)
			var storageErr *RequestBodyStorageError
			require.ErrorAs(t, err, &storageErr)
			require.Nil(t, got)
			if file != nil {
				_, err := file.Stat()
				require.ErrorIs(t, err, os.ErrClosed)
			}
			entries, err := os.ReadDir(directory)
			require.NoError(t, err)
			require.Empty(t, entries)
		})
	}
}

func TestAdmittedBodyStreamCancelBeforeFinalAllocation(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	body := bytes.Repeat([]byte{'x'}, 2<<20)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	got, err := ReadAdmittedBodyStream(ctx, bytes.NewReader(body), int64(len(body)), AdmittedBodyReadHooks{
		Reserve: func(_, joining int64) error {
			if joining > 0 {
				cancel()
			}
			return nil
		},
	})
	require.ErrorIs(t, err, context.Canceled)
	require.Nil(t, got)
	entries, err := os.ReadDir(directory)
	require.NoError(t, err)
	require.Empty(t, entries)
}

type admittedEmptyReader struct{}

func (admittedEmptyReader) Read([]byte) (int, error) { return 0, nil }

func TestAdmittedBodyStreamKeepsInputErrors(t *testing.T) {
	for _, reader := range []io.Reader{admittedBodyErrorReader{err: io.ErrUnexpectedEOF}, admittedEmptyReader{}} {
		_, err := ReadAdmittedBodyStream(t.Context(), reader, 1024, AdmittedBodyReadHooks{})
		require.Error(t, err)
	}
	for _, encoding := range []string{"gzip", "deflate", "zstd"} {
		body := bytes.Repeat([]byte{'x'}, 2<<20)
		wire := admittedTestEncode(t, body, encoding)
		_, err := ReadAdmittedLenientJSONRequestBody(newRequestWithBody(t, wire[:len(wire)-4], encoding), int64(len(body)))
		require.Error(t, err, "压缩流截断不能作为正常 EOF 接收")
	}
}
