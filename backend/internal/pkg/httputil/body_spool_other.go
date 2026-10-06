//go:build !linux && !darwin

package httputil

import "os"

// 其他平台保留内存分块读取，避免未经验证地依赖打开文件删除或文件系统识别。
func requestBodySpoolAllowed(string) bool { return false }

func requestBodySpoolPrepare(*os.File) error                   { return nil }
func requestBodySpoolFlush(*os.File, int64, int64) error       { return nil }
func requestBodySpoolDiscardRead(*os.File, int64, int64) error { return nil }
