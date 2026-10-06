//go:build !linux && !darwin

package httputil

// 其他平台保留内存分块读取，避免未经验证地依赖打开文件删除或文件系统识别。
func requestBodySpoolAllowed(string) bool { return false }
