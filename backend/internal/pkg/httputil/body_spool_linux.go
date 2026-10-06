package httputil

import "golang.org/x/sys/unix"

func requestBodySpoolAllowed(directory string) bool {
	var info unix.Statfs_t
	return unix.Statfs(directory, &info) == nil && info.Type != unix.TMPFS_MAGIC && info.Type != unix.RAMFS_MAGIC
}
