//go:build darwin || !candidatecapture

package liveattestation

import "testing"

func TestCandidateCaptureCapabilityProbeUnavailableWithoutBuildTag(t *testing.T) {
	result := ProbeCandidateCaptureCapability()
	if result.Available || result.ProviderCheckPassed || result.ProviderGeneratePassed {
		t.Fatalf("普通构建不得宣称 candidatecapture capability 可用：%+v", result)
	}
}
