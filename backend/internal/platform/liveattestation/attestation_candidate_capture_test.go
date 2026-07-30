//go:build !darwin && candidatecapture

package liveattestation

import (
	"context"
	"errors"
	"strconv"
	"testing"
	"time"
)

func TestCandidateCaptureProviderRequiresBothExplicitGates(t *testing.T) {
	t.Setenv(candidateCaptureModeEnv, candidateCaptureMode)
	t.Setenv(candidateCaptureAckEnv, "")
	provider := NewProvider()
	if err := provider.Check(context.Background()); !errors.Is(err, ErrUnsupportedPlatform) {
		t.Fatalf("仅开启单门禁时 Check() error = %v，期望 ErrUnsupportedPlatform", err)
	}
}

func enableCandidateCaptureProvider(t *testing.T) {
	t.Helper()
	t.Setenv(candidateCaptureModeEnv, candidateCaptureMode)
	t.Setenv(candidateCaptureAckEnv, candidateCaptureAck)
	t.Setenv(candidateCaptureAPIKeyEnv, "15")
	t.Setenv(candidateCaptureGroupEnv, "9")
	t.Setenv(candidateCaptureAccountEnv, "99")
	t.Setenv(candidateCaptureProxyNameEnv, "candidate-aux-test")
	t.Setenv(candidateCaptureProxyHostEnv, "capture-cli")
	t.Setenv(candidateCaptureProxyPortEnv, "18443")
	t.Setenv(candidateCaptureExpiryEnv, strconv.FormatInt(time.Now().Add(5*time.Minute).Unix(), 10))
}

func TestCandidateCaptureProviderRejectsUnboundRequestContext(t *testing.T) {
	enableCandidateCaptureProvider(t)
	provider := NewProvider()
	if err := provider.Check(context.Background()); !errors.Is(err, errCandidateCaptureScopeMismatch) {
		t.Fatalf("未绑定验收身份时 Check() error = %v，期望 scope mismatch", err)
	}
}

func validCandidateCaptureScope() CandidateCaptureScope {
	return CandidateCaptureScope{
		APIKeyID:      15,
		GroupID:       9,
		AccountID:     99,
		ProxyID:       123,
		ProxyName:     "candidate-aux-test",
		ProxyHost:     "capture-cli",
		ProxyPort:     18443,
		ProxyIsolated: true,
	}
}

func TestCandidateCaptureProviderRejectsEveryScopeMismatch(t *testing.T) {
	enableCandidateCaptureProvider(t)
	provider := NewProvider()
	tests := map[string]func(*CandidateCaptureScope){
		"api key":         func(scope *CandidateCaptureScope) { scope.APIKeyID++ },
		"group":           func(scope *CandidateCaptureScope) { scope.GroupID++ },
		"account":         func(scope *CandidateCaptureScope) { scope.AccountID++ },
		"proxy id":        func(scope *CandidateCaptureScope) { scope.ProxyID = 0 },
		"proxy name":      func(scope *CandidateCaptureScope) { scope.ProxyName += "-other" },
		"proxy host":      func(scope *CandidateCaptureScope) { scope.ProxyHost = "other" },
		"proxy port":      func(scope *CandidateCaptureScope) { scope.ProxyPort++ },
		"proxy isolation": func(scope *CandidateCaptureScope) { scope.ProxyIsolated = false },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			scope := validCandidateCaptureScope()
			mutate(&scope)
			ctx := WithCandidateCaptureScope(context.Background(), scope)
			if _, err := provider.Generate(ctx); !errors.Is(err, errCandidateCaptureScopeMismatch) {
				t.Fatalf("Generate() error = %v，期望 scope mismatch", err)
			}
		})
	}
}

func TestCandidateCaptureProviderRejectsExpiredFixture(t *testing.T) {
	provider := candidateCaptureProvider{
		apiKeyID:  15,
		groupID:   9,
		accountID: 99,
		proxyName: "candidate-aux-test",
		proxyHost: "capture-cli",
		proxyPort: 18443,
		expiresAt: time.Now().Add(-time.Second),
	}
	ctx := WithCandidateCaptureScope(context.Background(), validCandidateCaptureScope())
	if _, err := provider.Generate(ctx); !errors.Is(err, errCandidateCaptureScopeMismatch) {
		t.Fatalf("过期后 Generate() error = %v，期望 scope mismatch", err)
	}
}

func TestCandidateCaptureProviderReturnsFixedNonSecretAttestation(t *testing.T) {
	enableCandidateCaptureProvider(t)
	provider := NewProvider()
	ctx := WithCandidateCaptureScope(context.Background(), validCandidateCaptureScope())
	if err := provider.Check(ctx); err != nil {
		t.Fatalf("双门禁开启后 Check() error = %v", err)
	}
	got, err := provider.Generate(ctx)
	if err != nil {
		t.Fatalf("Generate() error = %v", err)
	}
	if got != candidateAttestation {
		t.Fatalf("Generate() = %q，期望固定验收值", got)
	}
}
