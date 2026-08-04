package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func TestWriteRuntimeCatalogIsDeterministicAndAppendOnly(t *testing.T) {
	files, err := officialegress.DefaultReleaseCatalog().RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	output := t.TempDir()
	if err := writeRuntimeCatalog(output, files); err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		raw, readErr := os.ReadFile(filepath.Join(output, filepath.FromSlash(file.Path)))
		if readErr != nil || !bytes.Equal(raw, file.Data) {
			t.Fatalf("导出文件不确定：path=%s err=%v", file.Path, readErr)
		}
	}
	if err := writeRuntimeCatalog(output, files); err != nil {
		t.Fatalf("相同内容重复导出应当幂等：%v", err)
	}

	mutatedBlob := ""
	for _, file := range files {
		if file.Path != "release-catalog.json" {
			mutatedBlob = file.Path
			break
		}
	}
	if mutatedBlob == "" {
		t.Fatal("测试缺少内容寻址 blob")
	}
	if err := os.WriteFile(filepath.Join(output, filepath.FromSlash(mutatedBlob)), []byte("mutated"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writeRuntimeCatalog(output, files); err == nil || !strings.Contains(err.Error(), "字节不同") {
		t.Fatalf("相同路径不同字节未被拒绝：%v", err)
	}
}

func TestWriteRuntimeCatalogSelectorCanMoveWithoutChangingBlobs(t *testing.T) {
	files, err := officialegress.DefaultReleaseCatalog().RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	output := t.TempDir()
	if err := writeRuntimeCatalog(output, files); err != nil {
		t.Fatal(err)
	}
	selector := filepath.Join(output, "release-catalog.json")
	if err := os.WriteFile(selector, []byte("old selector"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writeRuntimeCatalog(output, files); err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		if file.Path != "release-catalog.json" {
			continue
		}
		raw, readErr := os.ReadFile(selector)
		if readErr != nil || !bytes.Equal(raw, file.Data) {
			t.Fatalf("selector 未原子切换：%v", readErr)
		}
	}
}
