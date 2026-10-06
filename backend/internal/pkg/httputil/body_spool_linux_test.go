package httputil

import (
	"os"
	"testing"
	"unsafe"

	"github.com/stretchr/testify/require"
	"golang.org/x/sys/unix"
)

func admittedSpoolResidentPages(t *testing.T, file *os.File, size int) int {
	t.Helper()
	mapped, err := unix.Mmap(int(file.Fd()), 0, size, unix.PROT_READ, unix.MAP_SHARED)
	require.NoError(t, err)
	defer func() { require.NoError(t, unix.Munmap(mapped)) }()
	pages := make([]byte, (size+os.Getpagesize()-1)/os.Getpagesize())
	_, _, errno := unix.Syscall(unix.SYS_MINCORE, uintptr(unsafe.Pointer(&mapped[0])), uintptr(len(mapped)), uintptr(unsafe.Pointer(&pages[0])))
	require.Zero(t, errno)
	resident := 0
	for _, page := range pages {
		resident += int(page & 1)
	}
	return resident
}

// 4MiB 的独立文件对照验证缓存策略，不运行请求峰值矩阵，也不通过全局 drop_caches 干扰宿主。
func TestRequestBodySpoolLinuxDiscardsWrittenAndReadPages(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("TMPDIR", directory)
	if !requestBodySpoolAllowed(directory) {
		t.Skip("当前临时目录不是普通磁盘")
	}
	const size = 4 << 20
	plain, err := os.CreateTemp(directory, "cache-control-*")
	require.NoError(t, err)
	defer func() { require.NoError(t, plain.Close()) }()
	spool, err := newRequestBodySpool()
	require.NoError(t, err)
	require.NotNil(t, spool)
	defer func() { require.NoError(t, spool.file.Close()) }()
	chunk := make([]byte, admittedBodyReadChunk)
	for written := 0; written < size; written += len(chunk) {
		_, err := plain.Write(chunk)
		require.NoError(t, err)
		require.NoError(t, spool.write(chunk))
	}
	require.NoError(t, spool.flushCache())
	plainPages := admittedSpoolResidentPages(t, plain, size)
	spoolPages := admittedSpoolResidentPages(t, spool.file, size)
	t.Logf("普通写入驻留页=%d；分批写回丢弃驻留页=%d；页大小=%d", plainPages, spoolPages, os.Getpagesize())
	require.GreaterOrEqual(t, plainPages, size/os.Getpagesize()/2)
	require.Less(t, spoolPages, admittedBodyCacheBatch/os.Getpagesize(), "暂存页缓存不能随正文线性保留")
	_, err = spool.file.ReadAt(chunk, 0)
	require.NoError(t, err)
	readPages := admittedSpoolResidentPages(t, spool.file, size)
	require.NoError(t, requestBodySpoolDiscardRead(spool.file, 0, int64(len(chunk))))
	remainingPages := admittedSpoolResidentPages(t, spool.file, size)
	t.Logf("回读后驻留页=%d；丢弃已读段后驻留页=%d", readPages, remainingPages)
	require.Less(t, remainingPages, readPages)
}

func TestRequestBodySpoolLinuxRefusesMemoryFilesystem(t *testing.T) {
	var stat unix.Statfs_t
	if unix.Statfs("/dev/shm", &stat) != nil || stat.Type != unix.TMPFS_MAGIC {
		t.Skip("没有可核对的 tmpfs")
	}
	require.False(t, requestBodySpoolAllowed("/dev/shm"))
}
