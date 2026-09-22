//go:build !darwin && candidatecapture

package liveattestation

import (
	"context"
	"time"
)

// ProbeCandidateCaptureCapability 在进程内实际执行候选 provider 的 Check 与
// Generate。固定夹具不含凭据，且整个路径没有网络调用。
func ProbeCandidateCaptureCapability() CandidateCaptureCapabilityProbeResult {
	provider := candidateCaptureProvider{
		apiKeyID:  15,
		groupID:   9,
		accountID: 99,
		proxyName: "candidate-build-probe",
		proxyHost: "127.0.0.1",
		proxyPort: 18443,
		expiresAt: time.Now().Add(time.Minute),
	}
	contextWithScope := WithCandidateCaptureScope(context.Background(), CandidateCaptureScope{
		APIKeyID:      15,
		GroupID:       9,
		AccountID:     99,
		ProxyID:       123,
		ProxyName:     "candidate-build-probe",
		ProxyHost:     "127.0.0.1",
		ProxyPort:     18443,
		ProxyIsolated: true,
	})
	checkPassed := provider.Check(contextWithScope) == nil
	generated, generateError := provider.Generate(contextWithScope)
	generatePassed := generateError == nil && generated == candidateAttestation
	return CandidateCaptureCapabilityProbeResult{
		Available:              checkPassed && generatePassed,
		ProviderCheckPassed:    checkPassed,
		ProviderGeneratePassed: generatePassed,
	}
}
