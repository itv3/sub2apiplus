//go:build linux || darwin

package httputil

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

func mapOwnedRequestBody(size int) ([]byte, bool, error) {
	if size < ownedRequestBodyMapThreshold {
		return make([]byte, size), false, nil
	}
	pageSize := os.Getpagesize()
	if size > int(^uint(0)>>1)-(pageSize-1) {
		return nil, false, errors.New("request body mapping size overflows")
	}
	aligned := (size + pageSize - 1) / pageSize * pageSize
	// 匿名私有映射是进程系统内存，不是 tmpfs，也不作为磁盘暂存收益计入。
	buffer, err := unix.Mmap(-1, 0, aligned, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_PRIVATE|unix.MAP_ANON)
	return buffer, err == nil, err
}

func unmapOwnedRequestBody(buffer []byte) error { return unix.Munmap(buffer) }
