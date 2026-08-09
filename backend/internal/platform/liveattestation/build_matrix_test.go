package liveattestation

import (
	"os/exec"
	"testing"
)

// candidatecapture 是候选抓包专用构建，平时没有任何构建或测试会触碰它——
// attestation_candidate_capture.go 曾经引用了一个不存在的
// candidateCaptureScopeFromContext，直到真正去构建候选镜像时才暴露。
// 这里把两种 Linux 构建都编一遍，防止该分支再次悄悄腐烂。
func TestLinuxBuildMatrixCompiles(t *testing.T) {
	if testing.Short() {
		t.Skip("短模式跳过交叉编译")
	}
	for _, tags := range []string{"", "candidatecapture"} {
		name := tags
		if name == "" {
			name = "default"
		}
		t.Run(name, func(t *testing.T) {
			args := []string{"build", "-o", t.TempDir() + "/out"}
			if tags != "" {
				args = append(args, "-tags", tags)
			}
			args = append(args, "./")
			cmd := exec.Command("go", args...)
			cmd.Env = append(cmd.Environ(), "GOOS=linux", "GOARCH=amd64", "CGO_ENABLED=0")
			if output, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("linux/%s 构建失败：%v\n%s", name, err, output)
			}
		})
	}
}
