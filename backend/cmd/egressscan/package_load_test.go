package main

import (
	"go/ast"
	"go/types"
	"strings"
	"testing"

	"golang.org/x/tools/go/packages"
)

func TestValidateLoadedPackagesRejectsPatternErrorWithoutFiles(t *testing.T) {
	loaded := []*packages.Package{{
		ID: "./...", PkgPath: "./...",
		Types:     types.NewPackage("./...", "pattern"),
		TypesInfo: &types.Info{},
		Errors: []packages.Error{{
			Msg: "pattern ./...: open cache-index: permission denied",
		}},
	}}
	err := validateLoadedPackages(loaded, "linux/amd64")
	if err == nil || !strings.Contains(err.Error(), "permission denied") ||
		!strings.Contains(err.Error(), "linux/amd64") {
		t.Fatalf("无源码 pattern error 未按构建上下文失败关闭：%v", err)
	}
}

func TestValidateLoadedPackagesKeepsAuditableTypecheckFallback(t *testing.T) {
	loaded := []*packages.Package{{
		ID: "sample", PkgPath: "sample",
		GoFiles:   []string{"/workspace/sample.go"},
		Syntax:    []*ast.File{{}},
		Types:     types.NewPackage("sample", "sample"),
		TypesInfo: &types.Info{},
		Errors:    []packages.Error{{Msg: "undefined: sampleSymbol"}},
	}}
	if err := validateLoadedPackages(loaded, "linux/amd64"); err != nil {
		t.Fatalf("仍有源码和局部类型信息的普通类型错误被拒绝：%v", err)
	}
	fallback := collectTypecheckFallback(loaded, "linux/amd64")
	if len(fallback) != 1 || !strings.HasSuffix(fallback[0], "sample.go") {
		t.Fatalf("普通类型错误没有进入 SyntaxFallback：%v", fallback)
	}
}

func TestValidateLoadedPackagesRejectsRepositoryPackageWithoutTypesInfo(t *testing.T) {
	loaded := []*packages.Package{{
		ID:      modulePackagePrefix + "internal/service",
		PkgPath: modulePackagePrefix + "internal/service",
		GoFiles: []string{"/workspace/internal/service/send.go"},
		Syntax:  []*ast.File{{}},
		Types:   types.NewPackage(modulePackagePrefix+"internal/service", "service"),
	}}
	err := validateLoadedPackages(loaded, "darwin/arm64")
	if err == nil || !strings.Contains(err.Error(), "类型或语法信息不完整") {
		t.Fatalf("仓库 package 缺失 TypesInfo 时未失败关闭：%v", err)
	}
}
