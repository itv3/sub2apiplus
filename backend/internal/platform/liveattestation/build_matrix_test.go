package liveattestation

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
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

// TestProductionDockerfileDoesNotDefaultToCandidateCapture 守住生产构建的默认标签。
//
// candidatecapture 提供的是合成 attestation provider，只能用于隔离抓包；它一旦进入生产
// 镜像，Live 请求就会带上合成值出站。Dockerfile 允许候选采集用 BUILD_TAGS 覆盖，但默认
// 值必须是 embed。
func TestProductionDockerfileDoesNotDefaultToCandidateCapture(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "deploy", "Dockerfile")
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取 Dockerfile 失败：%v", err)
	}
	text := string(content)

	matches := regexp.MustCompile(`(?m)^ARG BUILD_TAGS=(.*)$`).FindStringSubmatch(text)
	if matches == nil {
		t.Fatal("Dockerfile 缺少 ARG BUILD_TAGS 声明")
	}
	if got := strings.TrimSpace(matches[1]); got != "embed" {
		t.Fatalf("BUILD_TAGS 默认值必须是 embed，实际为 %q", got)
	}
	if !strings.Contains(text, `-tags "${BUILD_TAGS}"`) {
		t.Fatal("go build 必须使用 ${BUILD_TAGS}，否则该参数形同虚设")
	}
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "#") {
			continue
		}
		if strings.Contains(trimmed, "candidatecapture") {
			t.Fatalf("Dockerfile 不得在非注释行写入 candidatecapture：%s", trimmed)
		}
	}
}
