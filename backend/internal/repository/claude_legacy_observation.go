package repository

import (
	"context"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// bindClaudeLegacyObservationContext 只为精确的官方 Claude OAuth URL 绑定
// FW-E observation-only Sink。测试端点、自定义中转和其他认证产品保持原有范围。
func bindClaudeLegacyObservationContext(
	ctx context.Context,
	rawURL string,
	host string,
	path string,
	sinkID officialegress.SinkID,
) (context.Context, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ctx, err
	}
	port := parsed.Port()
	if !strings.EqualFold(parsed.Scheme, "https") ||
		!strings.EqualFold(parsed.Hostname(), host) ||
		(port != "" && port != "443") || parsed.EscapedPath() != path {
		return ctx, nil
	}
	return officialegress.StartDefaultSinkAttempt(ctx, sinkID)
}
