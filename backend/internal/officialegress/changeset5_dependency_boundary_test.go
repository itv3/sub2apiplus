package officialegress

import (
	"bufio"
	"bytes"
	"os/exec"
	"sort"
	"strings"
	"testing"
)

const changeset5ModulePath = "github.com/Wei-Shaw/sub2api"

// changeset5AllowedInternalDependencies 是 officialegress 生产依赖闭包中允许出现的
// 项目内部包。闭集只包含 officialegress 核心及其契约子包；业务、仓储和入站适配层
// 均不得通过直接或传递依赖进入该闭包。
var changeset5AllowedInternalDependencies = map[string]bool{
	changeset5ModulePath + "/internal/officialegress":                     true,
	changeset5ModulePath + "/internal/officialegress/bindingcontract":     true,
	changeset5ModulePath + "/internal/officialegress/compositioncontract": true,
	changeset5ModulePath + "/internal/officialegress/finalwirecapture":    true,
	changeset5ModulePath + "/internal/officialegress/finalwirecontract":   true,
	changeset5ModulePath + "/internal/officialegress/profilecontract":     true,
	changeset5ModulePath + "/internal/officialegress/receiptcontract":     true,
	changeset5ModulePath + "/internal/officialegress/releasecontract":     true,
	changeset5ModulePath + "/internal/officialegress/sealcontract":        true,
}

func TestChangeset5OfficialEgressDependencyClosure(t *testing.T) {
	command := exec.Command(
		"go", "list", "-deps", "-f", "{{if not .Standard}}{{.ImportPath}}{{end}}",
		"./internal/officialegress/...",
	)
	command.Dir = "../.."
	raw, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("go list 重建 officialegress 依赖闭包失败：%v\n%s", err, raw)
	}
	var dependencies []string
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	for scanner.Scan() {
		if dependency := strings.TrimSpace(scanner.Text()); dependency != "" {
			dependencies = append(dependencies, dependency)
		}
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("读取 go list 输出失败：%v", err)
	}
	if violations := changeset5DependencyViolations(dependencies); len(violations) != 0 {
		t.Fatalf("officialegress 依赖闭包越界：%v", violations)
	}
	seen := make(map[string]bool)
	for _, dependency := range dependencies {
		if strings.HasPrefix(dependency, changeset5ModulePath+"/internal/") {
			seen[dependency] = true
		}
	}
	for dependency := range changeset5AllowedInternalDependencies {
		if !seen[dependency] {
			t.Fatalf("officialegress 内部依赖白名单出现陈旧项：%s", dependency)
		}
	}
}

func TestChangeset5OfficialEgressDependencyClosureRejectsForbiddenLayers(t *testing.T) {
	for _, dependency := range []string{
		changeset5ModulePath + "/internal/service",
		changeset5ModulePath + "/internal/service/subpackage",
		changeset5ModulePath + "/internal/repository",
		changeset5ModulePath + "/internal/handler/admin",
	} {
		violations := changeset5DependencyViolations([]string{dependency})
		if len(violations) != 1 || violations[0] != dependency {
			t.Fatalf("依赖闭包负例未被拒绝：dependency=%s violations=%v", dependency, violations)
		}
	}
}

func changeset5DependencyViolations(dependencies []string) []string {
	var violations []string
	for _, dependency := range dependencies {
		if !strings.HasPrefix(dependency, changeset5ModulePath+"/internal/") {
			continue
		}
		if !changeset5AllowedInternalDependencies[dependency] {
			violations = append(violations, dependency)
		}
	}
	sort.Strings(violations)
	return violations
}
