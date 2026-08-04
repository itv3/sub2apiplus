package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGenEnumsMergesAllCatalogSnapshots(t *testing.T) {
	dir := t.TempDir()
	active := `{"Endpoints":[{"Compression":"none","ClientLifecycle":"active_only","HeaderOrderMode":"ordered","Query":[],"Headers":[],"Body":{"Encoding":"json","Fields":[]}}]}`
	previous := `{"Endpoints":[{"Compression":"previous_only","ClientLifecycle":"active_only","HeaderOrderMode":"ordered","Query":[],"Headers":[],"Body":{"Encoding":"json","Fields":[]}}]}`
	if err := os.WriteFile(filepath.Join(dir, "active.json"), []byte(active), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "previous.json"), []byte(previous), 0o600); err != nil {
		t.Fatal(err)
	}
	catalog := `{"snapshots":[{"file":"active.json"},{"file":"previous.json"}]}`
	catalogPath := filepath.Join(dir, "catalog.json")
	if err := os.WriteFile(catalogPath, []byte(catalog), 0o600); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "enums.go")
	if err := genEnums(catalogPath, out, "profilecontract"); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if !strings.Contains(text, `"active_only"`) || !strings.Contains(text, `"previous_only"`) {
		t.Fatal("生成器没有合并 active/previous 快照的观测值")
	}
}

func TestGenEnumsRejectsInvalidPackageName(t *testing.T) {
	if err := genEnums("unused", "unused", "bad;package"); err == nil {
		t.Fatal("非法包名必须在读取或写入文件前被拒绝")
	}
}
