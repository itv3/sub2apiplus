package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestSnapshotLoadsReviewedMigrationRouteEvidence(t *testing.T) {
	output := filepath.Join(t.TempDir(), "snapshot.json")
	receipts := "../../../docs/egress/lifecycle/migration-receipts.json," +
		"../../../docs/egress/migration/migration-receipts.json"
	if code := runSnapshot(output, receipts); code != 0 {
		t.Fatalf("snapshot 模式失败：exit=%d", code)
	}
	raw, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	migrations, err := loadMigrationReceiptIndex(receipts)
	if err != nil {
		t.Fatal(err)
	}
	routes := migrations.codexRoutes()
	baseline, err := decodeBaseline(raw, routes)
	if err != nil {
		t.Fatal(err)
	}
	if len(baseline.Sinks) == 0 {
		t.Fatal("snapshot 未生成发送面候选")
	}
	if !slices.Contains(routes, "GET chatgpt.com/backend-api/codex/models") ||
		!slices.Contains(routes, "GET api.openai.com/v1/realtime (WebSocket)") {
		t.Fatalf("snapshot 未加载预期 Codex route 覆盖证明：%v", routes)
	}
}

func TestSnapshotRejectsMissingMigrationReceipts(t *testing.T) {
	output := filepath.Join(t.TempDir(), "snapshot.json")
	if code := runSnapshot(output, ""); code == 0 {
		t.Fatal("缺少 MigrationReceipt 时 snapshot 未失败关闭")
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatalf("失败的 snapshot 不得产生输出：%v", err)
	}
}
