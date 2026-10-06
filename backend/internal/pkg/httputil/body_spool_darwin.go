package httputil

import (
	"bytes"

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
