package httputil

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"testing"

	"github.com/stretchr/testify/require"
)

func ownedTestMappedBytes(size int) int64 {
	if size < ownedRequestBodyMapThreshold || (runtime.GOOS != "linux" && runtime.GOOS != "darwin") {
		return 0
	}
	pageSize := os.Getpagesize()
	return int64((size + pageSize - 1) / pageSize * pageSize)
}

func TestOwnedRequestBodyLeaseLifecycle(t *testing.T) {
	for _, size := range []int{0, ownedRequestBodyMapThreshold - 1, ownedRequestBodyMapThreshold, ownedRequestBodyMapThreshold + 1} {
		baseline := ActiveOwnedRequestBodyBytes()
		unmaps := 0
		owner, err := newOwnedRequestBodyWithMapping(size, mapOwnedRequestBody, func(buffer []byte) error {
			unmaps++
			return unmapOwnedRequestBody(buffer)
		})
		require.NoError(t, err)
		t.Cleanup(func() { require.NoError(t, owner.Close()) })
		require.Len(t, owner.Bytes(), size)
		require.Equal(t, size, cap(owner.Bytes()))
		mappedBytes := ownedTestMappedBytes(size)
		require.Equal(t, mappedBytes, owner.MappedBytes())
		require.Equal(t, mappedBytes > 0, owner.IsMapped())
		require.Equal(t, baseline+mappedBytes, ActiveOwnedRequestBodyBytes())
		if size > 0 {
			owner.Bytes()[0], owner.Bytes()[size-1] = 'a', 'z'
			buffers := owner.RetainedBuffers()
			require.Len(t, buffers, 1)
			require.Len(t, buffers[0], max(size, int(mappedBytes)), "完整底层区间必须包含映射的页对齐尾部")
		} else {
			require.Empty(t, owner.RetainedBuffers())
		}
		lease, err := owner.Retain()
		require.NoError(t, err)
		t.Cleanup(func() { require.NoError(t, lease.Close()) })
		require.Equal(t, baseline+mappedBytes, ActiveOwnedRequestBodyBytes(), "多个租约不能重复计数")
		require.NoError(t, owner.Close())
		require.NoError(t, owner.Close())
		require.Nil(t, owner.Bytes())
		require.Empty(t, owner.RetainedBuffers())
		require.Zero(t, owner.MappedBytes())
		_, err = owner.Retain()
		require.ErrorIs(t, err, ErrOwnedRequestBodyClosed)
		require.Zero(t, unmaps, "其他读者结束前不能释放真实映射")
		require.Equal(t, baseline+mappedBytes, ActiveOwnedRequestBodyBytes())
		if size > 0 {
			require.Equal(t, byte('a'), lease.Bytes()[0])
			require.Equal(t, byte('z'), lease.Bytes()[size-1])
		}
		require.NoError(t, lease.Close())
		require.NoError(t, lease.Close())
		require.Equal(t, baseline, ActiveOwnedRequestBodyBytes(), "最后一个租约必须同步完成真实释放")
		if mappedBytes > 0 {
			require.Equal(t, 1, unmaps)
		} else {
			require.Zero(t, unmaps)
		}
	}
	var empty *OwnedRequestBody
	require.Nil(t, empty.Bytes())
	require.Empty(t, empty.RetainedBuffers())
	require.Zero(t, empty.MappedBytes())
	require.NoError(t, empty.Close())
	_, err := empty.Retain()
	require.ErrorIs(t, err, ErrOwnedRequestBodyClosed)
}

func TestOwnedRequestBodyConcurrentLeases(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	owner, err := newOwnedRequestBody(ownedRequestBodyMapThreshold + 1)
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, owner.Close()) })
	copy(owner.Bytes(), "所有读者结束后释放")
	var workers sync.WaitGroup
	for range 12 {
		workers.Go(func() {
			for range 20 {
				lease, err := owner.Retain()
				if err != nil {
					t.Error(err)
					return
				}
				if !bytes.HasPrefix(lease.Bytes(), []byte("所有读者结束后释放")) {
					t.Error("有效租约的正文发生改变")
				}
				if err := lease.Close(); err != nil {
					t.Error(err)
				}
				if err := lease.Close(); err != nil {
					t.Error(err)
				}
			}
		})
	}
	workers.Wait()
	require.Equal(t, baseline+owner.MappedBytes(), ActiveOwnedRequestBodyBytes())
	for range 12 {
		workers.Go(func() {
			if err := owner.Close(); err != nil {
				t.Error(err)
			}
		})
	}
	workers.Wait()
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
}

func TestOwnedRequestBodyMappingFailures(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	allocationErr := errors.New("模拟匿名映射分配失败")
	owner, err := newOwnedRequestBodyWithMapping(ownedRequestBodyMapThreshold, func(int) ([]byte, bool, error) {
		return nil, false, allocationErr
	}, unmapOwnedRequestBody)
	require.Nil(t, owner)
	var storageErr *RequestBodyStorageError
	require.ErrorAs(t, err, &storageErr)
	require.ErrorIs(t, err, allocationErr)
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
	if ownedTestMappedBytes(ownedRequestBodyMapThreshold) == 0 {
		return
	}
	releaseErr := errors.New("模拟匿名映射释放失败")
	calls := 0
	owner, err = newOwnedRequestBodyWithMapping(ownedRequestBodyMapThreshold, mapOwnedRequestBody, func(buffer []byte) error {
		calls++
		if calls == 1 {
			return releaseErr
		}
		return unmapOwnedRequestBody(buffer)
	})
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, owner.Close()) })
	owner.Bytes()[0] = 'x'
	err = owner.Close()
	require.ErrorAs(t, err, &storageErr)
	require.ErrorIs(t, err, releaseErr)
	require.Equal(t, baseline+owner.MappedBytes(), ActiveOwnedRequestBodyBytes(), "释放失败不能提前扣减系统内存")
	require.Equal(t, byte('x'), owner.Bytes()[0], "释放失败保留有效租约，允许调用方重试")
	lease, err := owner.Retain()
	require.NoError(t, err)
	require.NoError(t, lease.Close())
	require.NoError(t, owner.Close())
	require.Equal(t, 2, calls)
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
}

func TestReadOwnedAdmittedHTTPBodies(t *testing.T) {
	body := []byte(`{"input":"` + strings.Repeat("x", ownedRequestBodyMapThreshold) + `"}`)
	for _, encoding := range []string{"identity", "zstd", "gzip", "deflate"} {
		for _, unknown := range []bool{false, true} {
			name := encoding
			if unknown {
				name += "_未知长度"
			}
			t.Run(name, func(t *testing.T) {
				baseline := ActiveOwnedRequestBodyBytes()
				req := newRequestWithBody(t, admittedTestEncode(t, body, encoding), encoding)
				if unknown {
					req.ContentLength = -1
				}
				reservations := 0
				owner, err := ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(req, int64(len(body)), func(size int) error {
					reservations++
					require.Equal(t, len(body), size)
					return nil
				})
				require.NoError(t, err)
				t.Cleanup(func() { require.NoError(t, owner.Close()) })
				require.Equal(t, body, owner.Bytes())
				require.Equal(t, 1, reservations)
				require.Equal(t, baseline+ownedTestMappedBytes(len(body)), ActiveOwnedRequestBodyBytes())
				if encoding != "identity" {
					require.Empty(t, req.Header.Get("Content-Encoding"))
					require.EqualValues(t, len(body), req.ContentLength)
				}
				require.NoError(t, owner.Close())
				require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
			})
		}
	}
}

func TestReadOwnedAdmittedNormalizationAndPreread(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	body := []byte("\xef\xbb\xbf{\"input\":\"" + strings.Repeat("x", ownedRequestBodyMapThreshold) + "\n\"}")
	want := []byte(`{"input":"` + strings.Repeat("x", ownedRequestBodyMapThreshold) + `\u000a"}`)
	var allocations []*OwnedRequestBody
	owner, err := readOwnedAdmittedLenientJSONRequestBody(newRequestWithBody(t, body, ""), int64(len(want)), nil, func(size int) (*OwnedRequestBody, error) {
		owner, err := newOwnedRequestBody(size)
		if err == nil {
			allocations = append(allocations, owner)
		}
		return owner, err
	})
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, owner.Close()) })
	require.Equal(t, want, owner.Bytes())
	require.Len(t, allocations, 2)
	require.Nil(t, allocations[0].Bytes(), "规范化成功后立即释放中间原文")
	require.Equal(t, baseline+ownedTestMappedBytes(len(want)), ActiveOwnedRequestBodyBytes())
	require.NoError(t, owner.Close())
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())

	for _, preread := range []bool{false, true} {
		body := append([]byte{0xef, 0xbb, 0xbf}, want...)
		req := newRequestWithBody(t, body, "")
		if preread {
			req.Body = NewPrereadBody(body)
		}
		owner, err := ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(req, int64(len(body)), nil)
		require.NoError(t, err)
		t.Cleanup(func() { require.NoError(t, owner.Close()) })
		require.Equal(t, want, owner.Bytes())
		require.NotSame(t, &body[jsonUTF8BOMLen], &owner.Bytes()[0], "预读正文属于调用方，不能借用其数组冒充所有权")
		retainedSize := len(body)
		if preread {
			retainedSize = len(want)
		}
		require.Equal(t, ownedTestMappedBytes(retainedSize), owner.MappedBytes())
		require.Len(t, owner.RetainedBuffers()[0], max(retainedSize, int(owner.MappedBytes())))
		require.NoError(t, owner.Close())
		require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
	}
	for _, req := range []*http.Request{nil, {}, newRequestWithBody(t, nil, "")} {
		owner, err := ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(req, 0, nil)
		require.NoError(t, err)
		require.Empty(t, owner.Bytes())
		require.False(t, owner.IsMapped())
		require.NoError(t, owner.Close())
	}
}

func TestReadOwnedAdmittedHTTPErrorsReleaseAllocations(t *testing.T) {
	body := []byte("{\"input\":\"" + strings.Repeat("x", ownedRequestBodyMapThreshold) + "\n\"}")
	rejected := errors.New("模拟准入失败")
	allocationErr := errors.New("模拟正文内存不足")
	for _, phase := range []string{"分配前取消", "分配后取消", "预读复制后取消", "正文短读", "超出声明长度", "规范化超限", "规范化拒绝", "第二次分配失败"} {
		t.Run(phase, func(t *testing.T) {
			baseline := ActiveOwnedRequestBodyBytes()
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			req := newRequestWithBody(t, body, "").WithContext(ctx)
			limit := int64(len(body) + 5)
			var reserve func(int) error
			var wantErr error
			switch phase {
			case "分配前取消":
				cancel()
				wantErr = context.Canceled
			case "分配后取消":
				wantErr = context.Canceled
			case "预读复制后取消":
				preread := append([]byte(nil), body...)
				preread[len(preread)-3] = 'x'
				req.Body = NewPrereadBody(preread)
				wantErr = context.Canceled
			case "正文短读":
				req.ContentLength++
				wantErr = io.ErrUnexpectedEOF
			case "超出声明长度":
				req.ContentLength--
			case "规范化超限":
				limit = int64(len(body))
			case "规范化拒绝":
				reserve = func(int) error { return rejected }
				wantErr = rejected
			case "第二次分配失败":
				wantErr = allocationErr
			}
			var allocations []*OwnedRequestBody
			owner, err := readOwnedAdmittedLenientJSONRequestBody(req, limit, reserve, func(size int) (*OwnedRequestBody, error) {
				if phase == "第二次分配失败" && len(allocations) == 1 {
					return newOwnedRequestBodyWithMapping(size, func(int) ([]byte, bool, error) {
						return nil, false, allocationErr
					}, unmapOwnedRequestBody)
				}
				allocated, err := newOwnedRequestBody(size)
				if err == nil {
					allocations = append(allocations, allocated)
				}
				if phase == "分配后取消" || phase == "预读复制后取消" {
					cancel()
				}
				return allocated, err
			})
			require.Error(t, err)
			if wantErr != nil {
				require.ErrorIs(t, err, wantErr)
			}
			if phase == "规范化超限" {
				var tooLarge *http.MaxBytesError
				require.ErrorAs(t, err, &tooLarge)
			}
			if phase == "第二次分配失败" {
				var storageErr *RequestBodyStorageError
				require.ErrorAs(t, err, &storageErr)
			}
			require.Nil(t, owner)
			if phase == "分配前取消" {
				require.Empty(t, allocations)
			} else {
				require.Len(t, allocations, 1)
			}
			for _, allocated := range allocations {
				require.Nil(t, allocated.Bytes(), "所有错误路径必须关闭已分配正文")
			}
			require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
		})
	}
}

func TestReadOwnedAdmittedDecodedAllocationFailure(t *testing.T) {
	body := []byte(`{"input":"` + strings.Repeat("x", ownedRequestBodyMapThreshold) + `"}`)
	allocationErr := errors.New("模拟解码后正文分配失败")
	for _, encoding := range []string{"identity", "zstd"} {
		for _, rejectReserve := range []bool{false, true} {
			baseline := ActiveOwnedRequestBodyBytes()
			directory := t.TempDir()
			t.Setenv("TMPDIR", directory)
			req := newRequestWithBody(t, admittedTestEncode(t, body, encoding), encoding)
			req.ContentLength = -1
			var allocated *OwnedRequestBody
			owner, err := readOwnedAdmittedLenientJSONRequestBody(req, int64(len(body)), func(int) error {
				if rejectReserve {
					return allocationErr
				}
				return nil
			}, func(size int) (*OwnedRequestBody, error) {
				if !rejectReserve {
					return newOwnedRequestBodyWithMapping(size, func(int) ([]byte, bool, error) {
						return nil, false, allocationErr
					}, unmapOwnedRequestBody)
				}
				var err error
				allocated, err = newOwnedRequestBody(size)
				return allocated, err
			})
			require.Nil(t, owner)
			require.ErrorIs(t, err, allocationErr)
			if rejectReserve {
				require.NotNil(t, allocated)
				require.Nil(t, allocated.Bytes(), "解码成功后的准入拒绝必须释放完整正文")
			} else {
				var storageErr *RequestBodyStorageError
				require.ErrorAs(t, err, &storageErr)
			}
			require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
			entries, err := os.ReadDir(directory)
			require.NoError(t, err)
			require.Empty(t, entries, "未知长度或解压后的分配失败也要关闭暂存文件")
		}
	}
}

func TestReadOwnedAdmittedBodyStreamJoinLifecycle(t *testing.T) {
	body := bytes.Repeat([]byte{'x'}, admittedBodySpoolThreshold+1)
	for _, disk := range []bool{false, true} {
		for _, phase := range []string{"完成", "准入拒绝", "分配失败", "分配后取消", "回读失败"} {
			if !disk && phase == "回读失败" {
				continue
			}
			name := "内存回退_" + phase
			if disk {
				name = "磁盘暂存_" + phase
			}
			t.Run(name, func(t *testing.T) {
				baseline := ActiveOwnedRequestBodyBytes()
				directory := t.TempDir()
				t.Setenv("TMPDIR", directory)
				if disk && !requestBodySpoolAllowed(directory) {
					t.Skip("当前目录不支持普通磁盘暂存")
				}
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
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
				rejected := errors.New("模拟最终正文准入失败")
				var allocated *OwnedRequestBody
				finished := false
				owner, err := readOwnedAdmittedBodyStream(ctx, bytes.NewReader(body), int64(len(body)), AdmittedBodyReadHooks{
					ReadDone: func() error { finished = true; return nil },
					Reserve: func(_, joining int64) error {
						if joining > 0 && phase == "准入拒绝" {
							return rejected
						}
						return nil
					},
				}, factory, func(size int) (*OwnedRequestBody, error) {
					require.True(t, finished, "读者必须在最终分配前结束")
					if phase == "分配失败" {
						return nil, &RequestBodyStorageError{Err: errors.New("模拟内存分配失败")}
					}
					var err error
					allocated, err = newOwnedRequestBody(size)
					if phase == "分配后取消" {
						cancel()
					}
					if phase == "回读失败" {
						require.NoError(t, file.Close())
					}
					return allocated, err
				})
				if phase == "完成" {
					require.NoError(t, err)
					require.Equal(t, body, owner.Bytes())
					require.Equal(t, baseline+owner.MappedBytes(), ActiveOwnedRequestBodyBytes())
					require.NoError(t, owner.Close())
				} else {
					require.Error(t, err)
					require.Nil(t, owner)
					if phase == "准入拒绝" {
						require.ErrorIs(t, err, rejected)
					}
					if phase == "分配后取消" {
						require.ErrorIs(t, err, context.Canceled)
					}
					if phase == "回读失败" || phase == "分配失败" {
						var storageErr *RequestBodyStorageError
						require.ErrorAs(t, err, &storageErr)
					}
					if allocated != nil {
						require.Nil(t, allocated.Bytes(), "最终分配后的错误必须释放匿名映射")
					}
				}
				require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
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
}

func TestLegacyAdmittedBodyAPIsKeepHeapOwnership(t *testing.T) {
	baseline := ActiveOwnedRequestBodyBytes()
	body := []byte(`{"input":"` + strings.Repeat("x", ownedRequestBodyMapThreshold) + `"}`)
	for _, encoding := range []string{"identity", "zstd"} {
		got, err := ReadAdmittedLenientJSONRequestBodyWithReservation(newRequestWithBody(t, admittedTestEncode(t, body, encoding), encoding), int64(len(body)), func(int) error {
			require.Equal(t, baseline, ActiveOwnedRequestBodyBytes(), "旧裸切片 API 不能借用需显式释放的映射")
			return nil
		})
		require.NoError(t, err)
		require.Equal(t, body, got)
		require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
	}
	got, err := ReadAdmittedBodyStream(t.Context(), bytes.NewReader(body), int64(len(body)), AdmittedBodyReadHooks{})
	require.NoError(t, err)
	require.Equal(t, body, got)
	require.Equal(t, baseline, ActiveOwnedRequestBodyBytes())
}
