package officialegress

import (
	"os"

	"golang.org/x/sys/unix"
)

func requestBodySpoolAllowed(directory string) bool {
	var info unix.Statfs_t
	return unix.Statfs(directory, &info) == nil && info.Type != unix.TMPFS_MAGIC && info.Type != unix.RAMFS_MAGIC
}

// 通过 SyscallConn 持有描述符使用权，避免取消关闭文件后对复用的描述符发出提示。
func withRequestBodySpoolDescriptor(file *os.File, action func(int) error) error {
	raw, err := file.SyscallConn()
	if err != nil {
		return err
	}
	var actionErr error
	if err := raw.Control(func(fd uintptr) { actionErr = action(int(fd)) }); err != nil {
		return err
	}
	return actionErr
}

func configureRequestBodySpool(file *os.File) error {
	return withRequestBodySpoolDescriptor(file, func(fd int) error {
		// 同一文件会依次进行摘要、Guard 和发送读取；禁用预读以限制缓存窗口。
		return unix.Fadvise(fd, 0, 0, unix.FADV_RANDOM)
	})
}

func finishRequestBodySpool(file *os.File) error {
	// DONTNEED 不保证回收脏页，必须先 Sync；该延迟需要与内存收益一起测量。
	if err := file.Sync(); err != nil {
		return err
	}
	return withRequestBodySpoolDescriptor(file, func(fd int) error {
		return unix.Fadvise(fd, 0, 0, unix.FADV_DONTNEED)
	})
}

func flushRequestBodySpoolWriteCache(file *os.File, offset, length int64) error {
	if err := file.Sync(); err != nil {
		return err
	}
	return releaseRequestBodySpoolReadCache(file, offset, length)
}

func releaseRequestBodySpoolReadCache(file *os.File, offset, length int64) error {
	return withRequestBodySpoolDescriptor(file, func(fd int) error {
		return unix.Fadvise(fd, offset, length, unix.FADV_DONTNEED)
	})
}
