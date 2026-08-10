//go:build !darwin && candidatecapture

package liveattestation

import (
	"context"
	"errors"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	candidateCaptureModeEnv      = "SUB2API_LIVE_ATTESTATION_CAPTURE_MODE"
	candidateCaptureAckEnv       = "SUB2API_LIVE_ATTESTATION_CAPTURE_ACK"
	candidateCaptureAPIKeyEnv    = "SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID"
	candidateCaptureGroupEnv     = "SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID"
	candidateCaptureAccountEnv   = "SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID"
	candidateCaptureProxyNameEnv = "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME"
	candidateCaptureProxyHostEnv = "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST"
	candidateCaptureProxyPortEnv = "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT"
	candidateCaptureExpiryEnv    = "SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX"
	candidateCaptureMode         = "synthetic-only"
	candidateCaptureAck          = "YES_I_ACCEPT_SYNTHETIC_ONLY"
	candidateAttestation         = `{"v":1,"s":0,"t":"candidate-capture-only"}`
	candidateCaptureMaxTTL       = 20 * time.Minute
)

var errCandidateCaptureScopeMismatch = errors.New("Live attestation candidate capture scope does not match")

// candidateCaptureProvider 只为隔离抓包补齐 Linux 无法生成的 DeviceCheck 值。
// 它不替换 Live 调度、首跳、Sideband、TLS 或版本画像路径；普通镜像也不会编译本文件。
type candidateCaptureProvider struct {
	apiKeyID  int64
	groupID   int64
	accountID int64
	proxyName string
	proxyHost string
	proxyPort int
	expiresAt time.Time
}

func NewProvider() Provider {
	if os.Getenv(candidateCaptureModeEnv) != candidateCaptureMode ||
		os.Getenv(candidateCaptureAckEnv) != candidateCaptureAck {
		return unsupportedProvider{}
	}
	apiKeyID, apiKeyErr := strconv.ParseInt(os.Getenv(candidateCaptureAPIKeyEnv), 10, 64)
	groupID, groupErr := strconv.ParseInt(os.Getenv(candidateCaptureGroupEnv), 10, 64)
	accountID, accountErr := strconv.ParseInt(os.Getenv(candidateCaptureAccountEnv), 10, 64)
	proxyName := strings.TrimSpace(os.Getenv(candidateCaptureProxyNameEnv))
	proxyHost := strings.TrimSpace(os.Getenv(candidateCaptureProxyHostEnv))
	proxyPort, proxyPortErr := strconv.Atoi(os.Getenv(candidateCaptureProxyPortEnv))
	expiryUnix, expiryErr := strconv.ParseInt(os.Getenv(candidateCaptureExpiryEnv), 10, 64)
	now := time.Now()
	expiresAt := time.Unix(expiryUnix, 0)
	if apiKeyErr != nil || groupErr != nil || accountErr != nil || proxyPortErr != nil || expiryErr != nil ||
		apiKeyID <= 0 || groupID <= 0 || accountID <= 0 ||
		proxyName == "" || proxyHost == "" || proxyPort <= 0 || proxyPort > 65535 ||
		!expiresAt.After(now) || expiresAt.After(now.Add(candidateCaptureMaxTTL)) {
		return unsupportedProvider{}
	}
	return candidateCaptureProvider{
		apiKeyID:  apiKeyID,
		groupID:   groupID,
		accountID: accountID,
		proxyName: proxyName,
		proxyHost: proxyHost,
		proxyPort: proxyPort,
		expiresAt: expiresAt,
	}
}

func (p candidateCaptureProvider) Check(ctx context.Context) error {
	return p.validateScope(ctx)
}

func (p candidateCaptureProvider) Generate(ctx context.Context) (string, error) {
	if err := p.validateScope(ctx); err != nil {
		return "", err
	}
	return candidateAttestation, nil
}

// candidateCaptureScopeFromContext 读回 WithCandidateCaptureScope 注入的调度身份。
// 与唯一的使用者同处 candidatecapture 构建标签下——放在无标签的 attestation.go 会让
// 默认构建把它算作未使用而被 lint 拦下。
func candidateCaptureScopeFromContext(ctx context.Context) (CandidateCaptureScope, bool) {
	scope, ok := ctx.Value(candidateCaptureScopeContextKey{}).(CandidateCaptureScope)
	return scope, ok
}

func (p candidateCaptureProvider) validateScope(ctx context.Context) error {
	if !time.Now().Before(p.expiresAt) {
		return errCandidateCaptureScopeMismatch
	}
	scope, ok := candidateCaptureScopeFromContext(ctx)
	if !ok || scope.APIKeyID != p.apiKeyID || scope.GroupID != p.groupID ||
		scope.AccountID != p.accountID || scope.ProxyID <= 0 ||
		scope.ProxyName != p.proxyName || scope.ProxyHost != p.proxyHost ||
		scope.ProxyPort != p.proxyPort || !scope.ProxyIsolated {
		return errCandidateCaptureScopeMismatch
	}
	return nil
}
