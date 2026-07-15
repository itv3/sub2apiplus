//go:build embed || unit

package web

import (
	"net/http"
	"path"
	"strings"
)

// staticAssetsCacheControl 与 deploy/Caddyfile 中带哈希前端资源的配置保持一致。
// Vite 会在 assets/ 下生成包含八位内容哈希的文件名，因此无需依赖反向代理也能安全使用长期不可变缓存。
const staticAssetsCacheControl = "public, max-age=31536000, immutable"

// isFingerprintedEmbeddedAssetPath 判断清理后的 URL 路径是否指向包含 Vite 默认八位构建哈希的资源。
func isFingerprintedEmbeddedAssetPath(cleanPath string) bool {
	cleanPath = strings.TrimPrefix(cleanPath, "/")
	if !strings.HasPrefix(cleanPath, "assets/") {
		return false
	}

	filename := path.Base(cleanPath)
	extension := path.Ext(filename)
	stem := strings.TrimSuffix(filename, extension)
	const fingerprintLength = 8
	delimiterIndex := len(stem) - fingerprintLength - 1
	if extension == "" || delimiterIndex < 1 || stem[delimiterIndex] != '-' {
		return false
	}

	// Vite 哈希仅使用 URL 安全字符，可稳定用于不可变缓存。
	fingerprint := stem[delimiterIndex+1:]
	for _, char := range fingerprint {
		if (char >= 'a' && char <= 'z') ||
			(char >= 'A' && char <= 'Z') ||
			(char >= '0' && char <= '9') ||
			char == '_' || char == '-' {
			continue
		}
		return false
	}
	return true
}

// applyStaticAssetCacheHeaders 为可长期缓存的静态路径设置 Cache-Control。
// index.html 和 SPA 路由必须保持 no-cache，因此不在此处处理。
func applyStaticAssetCacheHeaders(header http.Header, cleanPath string) {
	if header == nil || !isFingerprintedEmbeddedAssetPath(cleanPath) {
		return
	}
	header.Set("Cache-Control", staticAssetsCacheControl)
}
