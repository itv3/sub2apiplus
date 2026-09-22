//go:build darwin || !candidatecapture

package liveattestation

// ProbeCandidateCaptureCapability 对普通发布构建明确返回不可用，防止仅凭命令行
// 字符串或构建参数冒充 candidatecapture provider 已编入镜像。
func ProbeCandidateCaptureCapability() CandidateCaptureCapabilityProbeResult {
	return CandidateCaptureCapabilityProbeResult{}
}
