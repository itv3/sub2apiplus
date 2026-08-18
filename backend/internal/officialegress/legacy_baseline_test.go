package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"sort"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/sealcontract"
)

type legacyBaselineManifest struct {
	SchemaVersion        int      `json:"schema_version"`
	BootstrapCommit      string   `json:"bootstrap_commit"`
	Lifecycle            string   `json:"lifecycle"`
	ObservationStartedAt string   `json:"observation_started_at"`
	SealedAt             *string  `json:"sealed_at"`
	SinkIDs              []string `json:"sink_ids"`
}

type supplementLifecycleManifest struct {
	SchemaVersion   int               `json:"schema_version"`
	BootstrapCommit string            `json:"bootstrap_commit"`
	BootstrapTree   string            `json:"bootstrap_tree"`
	Lifecycle       string            `json:"lifecycle"`
	Supplements     []json.RawMessage `json:"supplements"`
}

type legacyCeilingManifest struct {
	SchemaVersion   int      `json:"schema_version"`
	BootstrapCommit string   `json:"bootstrap_commit"`
	Lifecycle       string   `json:"lifecycle"`
	SealedAt        *string  `json:"sealed_at"`
	SinkIDs         []string `json:"sink_ids"`
}

type legacyRemovalManifest struct {
	SchemaVersion   int    `json:"schema_version"`
	BootstrapCommit string `json:"bootstrap_commit"`
	Receipts        []struct {
		Candidate struct {
			RuntimeSinkID string `json:"runtime_sink_id"`
		} `json:"candidate"`
	} `json:"receipts"`
}

func TestProvisionalLegacyBaselineMatchesUnfinalizedReachableCatalog(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/egress/lifecycle/legacy-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest legacyBaselineManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("legacy 基线尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 1 || manifest.BootstrapCommit != BootstrapCommit ||
		manifest.ObservationStartedAt == "" {
		t.Fatalf("legacy 基线元数据非法：%+v", manifest)
	}
	switch manifest.Lifecycle {
	case "provisional":
		if manifest.SealedAt != nil {
			t.Fatal("provisional legacy 基线不得设置 sealed_at")
		}
	case "sealed":
		if manifest.SealedAt == nil || *manifest.SealedAt == "" {
			t.Fatal("sealed legacy 基线必须记录 sealed_at")
		}
	default:
		t.Fatalf("legacy lifecycle 非法：%s", manifest.Lifecycle)
	}
	supplementRaw, err := os.ReadFile("../../../docs/egress/lifecycle/pre-bootstrap-supplements.json")
	if err != nil {
		t.Fatal(err)
	}
	var supplementManifest supplementLifecycleManifest
	supplementDecoder := json.NewDecoder(bytes.NewReader(supplementRaw))
	supplementDecoder.DisallowUnknownFields()
	if err := supplementDecoder.Decode(&supplementManifest); err != nil {
		t.Fatal(err)
	}
	if supplementManifest.SchemaVersion != 2 || supplementManifest.BootstrapTree != bootstrapTree ||
		supplementManifest.BootstrapCommit != manifest.BootstrapCommit ||
		supplementManifest.Lifecycle != manifest.Lifecycle {
		t.Fatalf("legacy 基线与 pre-bootstrap 补录清单生命周期不一致：legacy=%+v supplements=%+v",
			manifest, supplementManifest)
	}
	ceilingRaw, err := os.ReadFile("../../../docs/egress/lifecycle/legacy-ceiling.json")
	if err != nil {
		t.Fatal(err)
	}
	var ceiling legacyCeilingManifest
	ceilingDecoder := json.NewDecoder(bytes.NewReader(ceilingRaw))
	ceilingDecoder.DisallowUnknownFields()
	if err := ceilingDecoder.Decode(&ceiling); err != nil {
		t.Fatal(err)
	}
	if ceiling.SchemaVersion != 1 || ceiling.BootstrapCommit != manifest.BootstrapCommit ||
		ceiling.Lifecycle != manifest.Lifecycle {
		t.Fatalf("legacy ceiling 与运行时清单生命周期不一致：legacy=%+v ceiling=%+v", manifest, ceiling)
	}
	sealReceiptRaw, err := os.ReadFile("../../../docs/egress/lifecycle/legacy-seal-receipt.json")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sealcontract.VerifyCurrent(sealcontract.Assets{
		ReceiptRaw: sealReceiptRaw, CeilingRaw: ceilingRaw, SupplementsRaw: supplementRaw,
		BaselineRaw: raw,
	}); err != nil {
		t.Fatalf("legacy seal receipt 与当前封存资产不一致：%v", err)
	}
	ceilingHasDuplicate := false
	for index := 1; index < len(ceiling.SinkIDs); index++ {
		ceilingHasDuplicate = ceilingHasDuplicate || ceiling.SinkIDs[index-1] == ceiling.SinkIDs[index]
	}
	if !sort.StringsAreSorted(ceiling.SinkIDs) || ceilingHasDuplicate {
		t.Fatal("legacy ceiling sink_ids 必须去重并排序")
	}
	if manifest.Lifecycle == "provisional" {
		if ceiling.SealedAt != nil || !slices.Equal(manifest.SinkIDs, ceiling.SinkIDs) {
			t.Fatal("provisional 阶段的 legacy 清单与 ceiling 必须同步，且不得设置 sealed_at")
		}
	} else if ceiling.SealedAt == nil || manifest.SealedAt == nil || *ceiling.SealedAt != *manifest.SealedAt {
		t.Fatal("sealed 阶段的 legacy 清单与 ceiling 必须使用同一 sealed_at")
	}
	if !sort.StringsAreSorted(manifest.SinkIDs) {
		t.Fatal("legacy sink_ids 必须排序")
	}
	for index := 1; index < len(manifest.SinkIDs); index++ {
		if manifest.SinkIDs[index-1] == manifest.SinkIDs[index] {
			t.Fatalf("legacy SinkID 重复：%s", manifest.SinkIDs[index])
		}
	}

	var expected []string
	for _, binding := range DefaultSinkCatalog().Bindings() {
		if !binding.RuntimeBindable() || binding.EnforcementState() == SinkStatePendingRemoval {
			continue
		}
		// FW-E observation-only Sink 是在 bootstrap 之后追加的 Claude 发送面事实，
		// 由独立的 FW-E 嵌入清单与 Inventory 约束，不得倒灌并改写历史 legacy
		// baseline／ceiling。其运行态仍必须保持 unclassified + legacy_observe。
		if binding.MigrationChangeset() == "FW-E" &&
			binding.Persona() == PersonaUnclassified &&
			binding.EndpointEvidence() == EndpointEvidenceExternalPersona {
			if binding.EnforcementState() != SinkStateLegacyObserve ||
				binding.MigrationReceiptDigest() != "" {
				t.Fatalf("FW-E observation-only Sink 越权：%s", binding.ID())
			}
			continue
		}
		if binding.MigrationReceiptDigest() == "" && binding.EnforcementState() != SinkStateLegacyObserve {
			t.Fatalf("无 MigrationReceipt 的可达 Sink %s 必须是 legacy_observe", binding.ID())
		}
		if binding.MigrationReceiptDigest() != "" && binding.EnforcementState() == SinkStateLegacyObserve {
			t.Fatalf("Sink %s 已有 MigrationReceipt 却仍停留在 legacy_observe", binding.ID())
		}
		// provisional 阶段尚未建立受保护 Git 封存锚，baseline 必须
		// 与 ceiling 保持完全一致；已签发 canary/enforced 收据的 Sink
		// 只能等 seal 后再持 MigrationReceipt 从 baseline 单调移除。
		if binding.EnforcementState() == SinkStateLegacyObserve ||
			(manifest.Lifecycle == "provisional" &&
				(binding.EnforcementState() == SinkStateCanaryEnforce ||
					binding.EnforcementState() == SinkStateEnforced)) {
			expected = append(expected, string(binding.ID()))
		}
	}
	sort.Strings(expected)
	if !slices.Equal(manifest.SinkIDs, expected) {
		t.Fatalf("legacy 基线与 Catalog 不一致：manifest=%v catalog=%v", manifest.SinkIDs, expected)
	}
	if manifest.Lifecycle == "sealed" {
		ceilingSet := make(map[string]struct{}, len(ceiling.SinkIDs))
		for _, id := range ceiling.SinkIDs {
			ceilingSet[id] = struct{}{}
		}
		for _, id := range manifest.SinkIDs {
			if _, allowed := ceilingSet[id]; !allowed {
				t.Fatalf("sealed 后 legacy 只能减少，禁止新增：%s", id)
			}
		}
		removalRaw, err := os.ReadFile("../../../docs/egress/lifecycle/removal-receipts.json")
		if err != nil {
			t.Fatal(err)
		}
		var removals legacyRemovalManifest
		if err := json.Unmarshal(removalRaw, &removals); err != nil {
			t.Fatal(err)
		}
		removedSink := make(map[string]struct{})
		for _, receipt := range removals.Receipts {
			removedSink[receipt.Candidate.RuntimeSinkID] = struct{}{}
		}
		for _, id := range ceiling.SinkIDs {
			if slices.Contains(manifest.SinkIDs, id) {
				continue
			}
			binding, exists := DefaultSinkCatalog().Resolve(SinkID(id))
			_, removed := removedSink[id]
			if (!exists || binding.MigrationReceiptDigest() == "") && !removed {
				t.Fatalf("legacy 减少缺少 MigrationReceipt/RemovalReceipt：%s", id)
			}
		}
	}
}
