//go:build embed || unit

package web

import (
	"net/http"
	"strings"
)

// staticAssetsCacheControl 与 deploy/Caddyfile 中带哈希前端资源的配置保持一致。
// Vite 会在 assets/ 下生成包含内容哈希的文件名，因此无需依赖反向代理也能安全使用长期不可变缓存。
const staticAssetsCacheControl = "public, max-age=31536000, immutable"

// isLongCacheStaticPath 判断清理后的 URL 路径（不含前导斜杠）是否应获得长期 Cache-Control 响应头。
// 此规则与 deploy/Caddyfile 保持一致。
func isLongCacheStaticPath(cleanPath string) bool {
	cleanPath = strings.TrimPrefix(cleanPath, "/")
	return strings.HasPrefix(cleanPath, "assets/") ||
		cleanPath == "logo.png" ||
		cleanPath == "favicon.ico"
}

// applyStaticAssetCacheHeaders 为可长期缓存的静态路径设置 Cache-Control。
// index.html 和 SPA 路由必须保持 no-cache，因此不在此处处理。
func applyStaticAssetCacheHeaders(header http.Header, cleanPath string) {
	if header == nil || !isLongCacheStaticPath(cleanPath) {
		return
	}
	header.Set("Cache-Control", staticAssetsCacheControl)
}
