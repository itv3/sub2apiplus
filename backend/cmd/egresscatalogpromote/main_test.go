package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func TestPromoteCatalogWritesImmutableProductionDirectory(t *testing.T) {
	base := officialegress.DefaultReleaseCatalog()
	active, err := base.Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := base.Resolve(officialegress.ReleaseModePrevious)
	if err != nil {
		t.Fatal(err)
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(root, "promotion")
	receipt, err := promoteCatalog(officialegress.CatalogPromotionInput{
		CampaignID:            "codex-0_147_0-20260815T055433Z",
		AcceptanceSHA256:      strings.Repeat("a", 64),
		TargetVersion:         previous.Version(),
		TargetProfileDigest:   previous.ProfileDigest(),
		RollbackVersion:       active.Version(),
		RollbackProfileDigest: active.ProfileDigest(),
	}, output)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["production_selector_changed"] != true || receipt["inventory_sha256"] == "" {
		t.Fatalf("生产提升收据不完整：%v", receipt)
	}
	for _, relative := range []string{
		"catalog-promotion-receipt.json",
		"catalogdata/runtime/release-catalog.json",
		"releasecontract/testdata/release-graph.json",
	} {
		info, statErr := os.Stat(filepath.Join(output, relative))
		if statErr != nil || !info.Mode().IsRegular() {
			t.Fatalf("生产提升缺少文件 %s：%v", relative, statErr)
		}
	}
	manifestRaw, err := os.ReadFile(filepath.Join(output, "catalogdata/runtime/release-catalog.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest map[string]any
	if err := json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(manifest["source"].(string), "/acceptance:") {
		t.Fatalf("生产目录未绑定 acceptance：%v", manifest)
	}
	if _, err := promoteCatalog(officialegress.CatalogPromotionInput{}, output); err == nil {
		t.Fatal("重复输出覆盖生产提升目录时未失败关闭")
	}
}
