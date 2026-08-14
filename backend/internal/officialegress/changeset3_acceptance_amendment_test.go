package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"slices"
	"sort"
	"strings"
	"testing"
)

const changeset3AcceptanceAmendmentSHA256 = "eaed3bb95bdd90659bbe0e6f9bc2094eb31d82c6df96f6f5c02484c401496a9b"

type changeset3AcceptanceAmendment struct {
	SchemaVersion string `json:"schema_version"`
	Status        string `json:"status"`
	ApprovalRef   string `json:"approval_ref"`
	OriginalFacts struct {
		AcceptanceMatrix changeset3AmendmentFrozenFact `json:"acceptance_matrix"`
		BindingFreeze    changeset3AmendmentFrozenFact `json:"codex_binding_freeze"`
		CatalogAmendment changeset3AmendmentFrozenFact `json:"catalog_amendments"`
	} `json:"original_facts"`
	ScopeExclusions []changeset3AmendmentScopeExclusion `json:"scope_exclusions"`
	EffectiveTarget struct {
		NewMigrationSinkCount       int    `json:"new_migration_sink_count"`
		NewMigrationRouteCount      int    `json:"new_migration_route_count"`
		ExistingEnforcedAnchorCount int    `json:"existing_enforced_anchor_count"`
		RuntimeSinkCount            int    `json:"runtime_sink_count"`
		RuntimeRouteCount           int    `json:"runtime_route_count"`
		RuntimeLegacyCount          int    `json:"codex_profile_runtime_legacy_count"`
		RuntimeEnforcedCount        int    `json:"codex_profile_runtime_enforced_count"`
		ScopeExclusionCount         int    `json:"scope_exclusion_count"`
		TransportOnlyException      string `json:"transport_only_exception"`
	} `json:"effective_target"`
	CorrectionReason string `json:"correction_reason"`
}

type changeset3AmendmentFrozenFact struct {
	Path                  string `json:"path"`
	SHA256                string `json:"sha256"`
	PendingMigrationCount int    `json:"pending_migration_count,omitempty"`
	RuntimeEnforcedTarget int    `json:"runtime_enforced_target,omitempty"`
}

type changeset3AmendmentScopeExclusion struct {
	SinkID           string `json:"sink_id"`
	Kind             string `json:"kind"`
	ASTFingerprint   string `json:"ast_fingerprint"`
	SourceBlobSHA256 string `json:"source_blob_sha256"`
}

type changeset3HistoricalAcceptanceMatrix struct {
	SchemaVersion          string   `json:"schema_version"`
	Status                 string   `json:"status"`
	AlreadyEnforcedAnchors []string `json:"already_enforced_anchors"`
	PendingMigration       []string `json:"pending_migration"`
	FacadeEvidence         []string `json:"facade_evidence"`
	TransportOnlyException struct {
		SinkID            string `json:"sink_id"`
		EndpointEvidence  string `json:"endpoint_evidence"`
		EndpointID        string `json:"endpoint_id"`
		ReleaseSelection  string `json:"release_selection"`
		MigrationReceipt  string `json:"migration_receipt"`
		FinalizationToken string `json:"finalization_token"`
		EnforcementState  string `json:"enforcement_state"`
	} `json:"transport_only_exception"`
	FinalAcceptance struct {
		RuntimeLegacyCount   int      `json:"codex_profile_runtime_legacy_count"`
		RuntimeEnforcedCount int      `json:"codex_profile_runtime_enforced_count"`
		TransportOnlyLegacy  []string `json:"codex_transport_only_legacy"`
		FacadeRuntimeCount   int      `json:"codex_facade_runtime_binding_count"`
	} `json:"final_acceptance"`
	NonRegression struct {
		ExecutorID                           string `json:"executor_id"`
		ExistingReceiptsMustRemainIdentical  bool   `json:"existing_receipts_must_remain_byte_identical"`
		ExistingBindingDigestsMustRemainSame bool   `json:"existing_binding_digests_must_remain_unchanged"`
		ExistingWireFixturesMustRemainSame   bool   `json:"existing_wire_fixtures_must_remain_unchanged"`
	} `json:"non_regression"`
}

func TestChangeset3AcceptanceAmendmentIsImmutableAndClosesHistoricalContradiction(t *testing.T) {
	amendmentRaw := mustReadChangeset3AcceptanceFile(t, "../../../docs/egress/migration/acceptance-amendment.json")
	if changeset3AcceptanceSHA256(amendmentRaw) != changeset3AcceptanceAmendmentSHA256 {
		t.Fatal("变更集 3 Acceptance Amendment 发生未审核漂移")
	}
	if err := validateChangeset3AcceptanceAmendment(amendmentRaw); err != nil {
		t.Fatal(err)
	}
}

func TestChangeset3AcceptanceAmendmentRejectsMissingOrMutatedAuthority(t *testing.T) {
	raw := mustReadChangeset3AcceptanceFile(t, "../../../docs/egress/migration/acceptance-amendment.json")
	cases := map[string][]byte{
		"缺少 amendment": nil,
		"父摘要漂移": bytes.Replace(
			raw,
			[]byte("2dc2acb8247bde3a9afadaaa0e7f57e482d2fdcaefd0f08211c11a1b702f9d56"),
			[]byte(strings.Repeat("a", sha256.Size*2)),
			1,
		),
		"有效迁移计数漂移": bytes.Replace(
			raw, []byte(`"new_migration_sink_count": 17`),
			[]byte(`"new_migration_sink_count": 18`), 1,
		),
		"exclusion AST 漂移": bytes.Replace(
			raw, []byte("93418efda9cf"), []byte("93418efda9ce"), 1,
		),
	}
	for name, candidate := range cases {
		t.Run(name, func(t *testing.T) {
			if err := validateChangeset3AcceptanceAmendment(candidate); err == nil {
				t.Fatal("非法 amendment 未被拒绝")
			}
		})
	}
}

func validateChangeset3AcceptanceAmendment(raw []byte) error {
	var amendment changeset3AcceptanceAmendment
	if err := changeset3DecodeAcceptanceStrict(raw, &amendment); err != nil {
		return fmt.Errorf("解析 Acceptance Amendment：%w", err)
	}
	if amendment.SchemaVersion != "changeset3-acceptance-amendment/v1" ||
		amendment.Status != "approved_for_remediation_with_constraints" ||
		strings.TrimSpace(amendment.ApprovalRef) == "" || strings.TrimSpace(amendment.CorrectionReason) == "" {
		return errors.New("Acceptance Amendment 身份不完整")
	}

	wantFacts := map[string]changeset3AmendmentFrozenFact{
		"acceptance": {
			Path:                  "docs/changeset3/acceptance-matrix.json",
			SHA256:                "2dc2acb8247bde3a9afadaaa0e7f57e482d2fdcaefd0f08211c11a1b702f9d56",
			PendingMigrationCount: 19, RuntimeEnforcedTarget: 23,
		},
		"binding": {
			Path:   "docs/changeset3/codex-binding-freeze.json",
			SHA256: "30e7814c10370278ce4940e00e5b2943b2243e763b9537dbb5069c7c0e310304",
		},
		"catalog": {
			Path:   "backend/internal/officialegress/catalogdata/catalog-amendments.json",
			SHA256: "325e7f918147a9876b9ec3fcecc09c96219ba7425b686376767365725d1a46b5",
		},
	}
	gotFacts := map[string]changeset3AmendmentFrozenFact{
		"acceptance": amendment.OriginalFacts.AcceptanceMatrix,
		"binding":    amendment.OriginalFacts.BindingFreeze,
		"catalog":    amendment.OriginalFacts.CatalogAmendment,
	}
	for name, want := range wantFacts {
		got := gotFacts[name]
		if got != want {
			return fmt.Errorf("Acceptance Amendment 父事实漂移: %s", name)
		}
		diskPath := "../../../" + got.Path
		diskPath = strings.Replace(diskPath, "../../../docs/changeset3/", "../../../docs/egress/migration/", 1)
		if name == "catalog" {
			diskPath = "catalogdata/catalog-amendments.json"
		}
		parent, err := os.ReadFile(diskPath)
		if err != nil {
			return err
		}
		if changeset3AcceptanceSHA256(parent) != got.SHA256 {
			return fmt.Errorf("Acceptance Amendment 绑定文件摘要不符: %s", got.Path)
		}
	}

	var historical changeset3HistoricalAcceptanceMatrix
	historicalRaw, err := os.ReadFile("../../../docs/egress/migration/acceptance-matrix.json")
	if err != nil {
		return err
	}
	if err := changeset3DecodeAcceptanceStrict(historicalRaw, &historical); err != nil {
		return err
	}
	if len(historical.PendingMigration) != 19 || historical.FinalAcceptance.RuntimeLegacyCount != 0 ||
		historical.FinalAcceptance.RuntimeEnforcedCount != 23 {
		return errors.New("原始矩阵的 19/23 历史事实未保留")
	}

	wantExclusions := []changeset3AmendmentScopeExclusion{
		{
			SinkID: "codex.admin_test.chat_completions", Kind: catalogAmendmentRuntimeScopeExclusion,
			ASTFingerprint:   "93418efda9cf",
			SourceBlobSHA256: "b932b28fdb708bd8b89d6f4cb7c865e90ffe8930e509d3f1912d6e32898f055e",
		},
		{
			SinkID: "codex.admin_test.keeper", Kind: catalogAmendmentRuntimeScopeExclusion,
			ASTFingerprint:   "d0e7cca6b855",
			SourceBlobSHA256: "689a1fe132daa14beaad88a7ade54c1c8f4f1852ef1015563993392b9d9b85e0",
		},
	}
	if !slices.Equal(amendment.ScopeExclusions, wantExclusions) {
		return errors.New("Acceptance Amendment 没有绑定两组完整 exclusion 证据")
	}
	catalogAmendments, err := loadCatalogAmendments()
	if err != nil {
		return err
	}
	for _, excluded := range wantExclusions {
		matched := false
		for _, catalogEntry := range catalogAmendments.Amendments {
			if catalogEntry.Kind == excluded.Kind && catalogEntry.SinkID == excluded.SinkID &&
				catalogEntry.Source.ASTFingerprint == excluded.ASTFingerprint &&
				catalogEntry.Source.SourceBlobSHA256 == excluded.SourceBlobSHA256 {
				matched = true
				break
			}
		}
		if !matched {
			return fmt.Errorf("Acceptance Amendment 与 Catalog exclusion 不一致: %s", excluded.SinkID)
		}
		binding, ok := DefaultSinkCatalog().Resolve(SinkID(excluded.SinkID))
		if !ok || binding.RuntimeBindable() {
			return fmt.Errorf("scope exclusion 重新成为 RuntimeBindable: %s", excluded.SinkID)
		}
	}

	effectiveMigration := len(historical.PendingMigration) - len(wantExclusions)
	effectiveEnforcedTarget := historical.FinalAcceptance.RuntimeEnforcedCount - len(wantExclusions)
	if effectiveMigration != 17 || effectiveEnforcedTarget != 21 ||
		amendment.EffectiveTarget.NewMigrationSinkCount != effectiveMigration ||
		amendment.EffectiveTarget.NewMigrationRouteCount != 24 ||
		amendment.EffectiveTarget.ExistingEnforcedAnchorCount != 4 ||
		amendment.EffectiveTarget.RuntimeSinkCount != effectiveEnforcedTarget ||
		amendment.EffectiveTarget.RuntimeRouteCount != 28 ||
		amendment.EffectiveTarget.RuntimeLegacyCount != 0 ||
		amendment.EffectiveTarget.RuntimeEnforcedCount != effectiveEnforcedTarget ||
		amendment.EffectiveTarget.ScopeExclusionCount != len(wantExclusions) ||
		amendment.EffectiveTarget.TransportOnlyException != string(SinkCodexOAuthExchange) {
		return errors.New("Acceptance Amendment 未正确推导 19/23→17/21")
	}

	var runtimeEnforced, runtimeLegacy, runtimeRoutes int
	var excludedIDs []string
	for _, binding := range DefaultSinkCatalog().Bindings() {
		if binding.Persona() != PersonaCodexCLI || binding.EndpointEvidence() != EndpointEvidenceCodexProfile {
			continue
		}
		if !binding.RuntimeBindable() {
			excludedIDs = append(excludedIDs, string(binding.ID()))
			continue
		}
		runtimeRoutes += len(binding.Routes())
		switch binding.EnforcementState() {
		case SinkStateEnforced:
			runtimeEnforced++
		case SinkStateLegacyObserve:
			runtimeLegacy++
		}
	}
	sort.Strings(excludedIDs)
	wantExcludedIDs := []string{"codex.admin_test.chat_completions", "codex.admin_test.keeper"}
	versionRoutes, err := loadVersionRouteReceiptManifest()
	if err != nil {
		return err
	}
	// Acceptance Amendment 的 28 条是变更集 3 历史终点；后续版本 route
	// 使用独立追加收据，因此当前运行时总数应单调增加而不改写父事实。
	expectedRuntimeRoutes := 28 + len(versionRoutes.Receipts)
	if runtimeEnforced != 21 || runtimeLegacy != 0 || runtimeRoutes != expectedRuntimeRoutes ||
		!slices.Equal(excludedIDs, wantExcludedIDs) {
		return fmt.Errorf(
			"最终 Runtime 口径不符合 amendment: enforced=%d legacy=%d routes=%d excluded=%v",
			runtimeEnforced, runtimeLegacy, runtimeRoutes, excludedIDs,
		)
	}
	return nil
}

func changeset3DecodeAcceptanceStrict(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("JSON 尾部存在额外对象")
		}
		return err
	}
	return nil
}

func mustReadChangeset3AcceptanceFile(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func changeset3AcceptanceSHA256(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}
