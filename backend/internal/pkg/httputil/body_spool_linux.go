package httputil

import (
	"os"

	"golang.org/x/sys/unix"
)

func requestBodySpoolAllowed(directory string) bool {
	var info unix.Statfs_t
	return unix.Statfs(directory, &info) == nil && info.Type != unix.TMPFS_MAGIC && info.Type != unix.RAMFS_MAGIC
}

func requestBodySpoolPrepare(file *os.File) error {
	// 禁用最终回读的预读放大，已消费部分随即丢弃。
	return unix.Fadvise(int(file.Fd()), 0, 0, unix.FADV_RANDOM)
}

func requestBodySpoolFlush(file *os.File, offset, length int64) error {
	// DONTNEED 不保证丢弃脏页，必须先完成写回。每 1MiB 调用，避免容器
	// memory.current 中的文件页随正文增长；不是依赖系统稍后自行回收。
	if err := unix.Fdatasync(int(file.Fd())); err != nil {
		return err
	}
	return unix.Fadvise(int(file.Fd()), offset, length, unix.FADV_DONTNEED)
}

func requestBodySpoolDiscardRead(file *os.File, offset, length int64) error {
	return unix.Fadvise(int(file.Fd()), offset, length, unix.FADV_DONTNEED)
}
