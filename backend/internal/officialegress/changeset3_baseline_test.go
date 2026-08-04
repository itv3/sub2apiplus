package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"
)

type changeset3BindingFreeze struct {
	SchemaVersion string `json:"schema_version"`
	Totals        struct {
		CodexCLI            int `json:"codex_cli"`
		CodexProfile        int `json:"codex_profile"`
		AlreadyEnforced     int `json:"already_enforced"`
		PendingMigration    int `json:"pending_migration"`
		FacadeNotApplicable int `json:"facade_not_applicable"`
		TransportOnly       int `json:"transport_only"`
	} `json:"totals"`
	Bindings []struct {
		SinkID           string `json:"sink_id"`
		EndpointEvidence string `json:"endpoint_evidence"`
		EffectiveState   string `json:"effective_state"`
		RuntimeBinding   bool   `json:"runtime_binding"`
	} `json:"bindings"`
}

type changeset3AcceptanceMatrix struct {
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
}

func TestChangeset3BaselineArtifactsAreFrozen(t *testing.T) {
	artifacts := map[string]string{
		"../../../docs/changeset3/workspace-status.txt":      "48d68ea2eb30649a621e3db9091dd339e5a103304798dcfb541b1dcceaa33b2d",
		"../../../docs/changeset3/untracked-files.sha256":    "3cfcc8d787a476227b74858eddaa8d783a90107d5418e181c16bc2e87c0c525a",
		"../../../docs/changeset3/codex-binding-freeze.json": "30e7814c10370278ce4940e00e5b2943b2243e763b9537dbb5069c7c0e310304",
		"../../../docs/changeset3/acceptance-matrix.json":    "2dc2acb8247bde3a9afadaaa0e7f57e482d2fdcaefd0f08211c11a1b702f9d56",
		"catalogdata/migration-receipts.json":                "94ff0e21cded16b08ba5b9ed4cabd9950a367d0c1fba2924d8d2e1313a6ba4a7",
		"../../../docs/changeset1a/legacy-baseline.json":     "42c49c9000221d30671398b9870297c80d0d6c2b84dfc1e3918056cb7c4c68db",
		"../../../docs/changeset1a/legacy-ceiling.json":      "f744ab46c8f509577ee4a88050e426bc9c04152b39bc5f6f0d0877d4b99cc58d",
		"../../../docs/changeset1a/removal-receipts.json":    "7d9d57cd30bcde5e1b7eacb35d8fcd2b65c80b8799955eb5537e91829bc90282",
		"../../../docs/changeset2/removal-receipts.json":     "c9e0edee6deaa742ca4be85addc02b9c2e2dcd31f15302e676b7830c8e1f8d10",
	}
	for path, want := range artifacts {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("读取变更集 3 冻结产物 %s：%v", path, err)
		}
		sum := sha256.Sum256(raw)
		if hex.EncodeToString(sum[:]) != want {
			t.Fatalf("变更集 3 冻结产物发生未审核漂移：%s", path)
		}
	}
}

func TestChangeset3BindingAndAcceptanceMatrixAreComplete(t *testing.T) {
	var freeze changeset3BindingFreeze
	readChangeset3JSON(t, "../../../docs/changeset3/codex-binding-freeze.json", &freeze)
	if freeze.SchemaVersion != "changeset3-codex-binding-freeze/v1" ||
		freeze.Totals.CodexCLI != 27 || freeze.Totals.CodexProfile != 23 ||
		freeze.Totals.AlreadyEnforced != 4 || freeze.Totals.PendingMigration != 19 ||
		freeze.Totals.FacadeNotApplicable != 3 || freeze.Totals.TransportOnly != 1 ||
		len(freeze.Bindings) != 27 {
		t.Fatalf("变更集 3 binding 冻结计数非法：%+v", freeze.Totals)
	}

	var matrix changeset3AcceptanceMatrix
	readChangeset3JSON(t, "../../../docs/changeset3/acceptance-matrix.json", &matrix)
	anchors := []string{
		"codex.admin_test.compact",
		"codex.admin_test.responses",
		"codex.alpha_search.pat_fallback",
		"codex.usage.probe",
	}
	if matrix.SchemaVersion != "changeset3-acceptance-matrix/v1" ||
		matrix.Status != "approved_for_implementation_with_constraints" ||
		!slices.Equal(matrix.AlreadyEnforcedAnchors, anchors) ||
		len(matrix.PendingMigration) != 19 || len(matrix.FacadeEvidence) != 3 {
		t.Fatal("变更集 3 验收矩阵不完整")
	}
	for _, required := range []string{
		"codex.admin_test.chat_completions",
		"codex.admin_test.keeper",
		"codex.oauth.refresh",
		"codex.responses.forward",
		"codex.responses.ws_v2_passthrough",
	} {
		if !slices.Contains(matrix.PendingMigration, required) {
			t.Fatalf("19 个迁移目标缺少 %s", required)
		}
	}

	exchange := matrix.TransportOnlyException
	if exchange.SinkID != string(SinkCodexOAuthExchange) ||
		exchange.EndpointEvidence != string(EndpointEvidenceTransportOnly) ||
		exchange.EndpointID != "" || exchange.ReleaseSelection != "unavailable" ||
		exchange.MigrationReceipt != "forbidden" || exchange.FinalizationToken != "unavailable" ||
		exchange.EnforcementState != string(SinkStateLegacyObserve) {
		t.Fatalf("OAuth exchange 的 transport-only 例外发生漂移：%+v", exchange)
	}

	catalog := DefaultSinkCatalog()
	for _, sinkID := range anchors {
		binding, ok := catalog.Resolve(SinkID(sinkID))
		if !ok || binding.EnforcementState() != SinkStateEnforced ||
			binding.Persona() != PersonaCodexCLI ||
			binding.EndpointEvidence() != EndpointEvidenceCodexProfile {
			t.Fatalf("既有 enforced 锚点发生回归：%s", sinkID)
		}
	}
	binding, ok := catalog.Resolve(SinkCodexOAuthExchange)
	if !ok || binding.EnforcementState() != SinkStateLegacyObserve ||
		binding.EndpointEvidence() != EndpointEvidenceTransportOnly {
		t.Fatal("生产 Catalog 中 OAuth exchange 不再是 transport-only legacy 例外")
	}
}

func TestChangeset3ExecutorAuthorityRemainsCompatible(t *testing.T) {
	raw, err := os.ReadFile("../service/official_egress_1b_executor.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `officialegress.ExecutorID("codex.executor.changeset1b")`) {
		t.Fatal("变更集 3 不得更换既有 Codex Executor authority ID")
	}
}

func readChangeset3JSON(t *testing.T, path string, target any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, target); err != nil {
		t.Fatal(err)
	}
}
