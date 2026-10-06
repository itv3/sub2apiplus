package httputil

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

func requestBodySpoolPrepare(file *os.File) error {
	_, err := unix.FcntlInt(file.Fd(), unix.F_NOCACHE, 1)
	return err
}

func requestBodySpoolFlush(*os.File, int64, int64) error { return nil }

func requestBodySpoolDiscardRead(*os.File, int64, int64) error { return nil }
