package officialegress

import (
	"bytes"
	"os"

	"golang.org/x/sys/unix"
)

func requestBodySpoolAllowed(directory string) bool {
	var info unix.Statfs_t
	if unix.Statfs(directory, &info) != nil {
		return false
	}
	name := string(bytes.TrimRight(info.Fstypename[:], "\x00"))
	return name != "tmpfs" && name != "mfs"
}

func configureRequestBodySpool(file *os.File) error {
	raw, err := file.SyscallConn()
	if err != nil {
		return err
	}
	var configureErr error
	if err := raw.Control(func(fd uintptr) {
		_, configureErr = unix.FcntlInt(fd, unix.F_NOCACHE, 1)
	}); err != nil {
		return err
	}
	return configureErr
}

func finishRequestBodySpool(file *os.File) error { return file.Sync() }

// F_NOCACHE 已限制写入缓存，不在每个窗口额外执行一次同步。
func flushRequestBodySpoolWriteCache(_ *os.File, _, _ int64) error { return nil }

func releaseRequestBodySpoolReadCache(_ *os.File, _, _ int64) error { return nil }
