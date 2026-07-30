//go:build !darwin

package liveattestation

import "context"

// unsupportedProvider 保留非 macOS 普通构建的既有行为。
// candidatecapture 构建在双门禁未同时开启时也回落到这里，避免验收镜像误用。
type unsupportedProvider struct{}

func (unsupportedProvider) Check(context.Context) error {
	return ErrUnsupportedPlatform
}

func (unsupportedProvider) Generate(context.Context) (string, error) {
	return "", ErrUnsupportedPlatform
}
