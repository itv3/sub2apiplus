package officialegress

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

func TestTransportReceiptTransitionBindsTransportPerReleaseDigest(t *testing.T) {
	catalog := syntheticChangeset2MixedVersionReleaseCatalog(t)
	active, err := catalog.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := catalog.Resolve(ReleaseModePrevious)
	if err != nil {
		t.Fatal(err)
	}
	endpointID := "wham_usage"
	findTransport := func(release ResolvedCodexRelease) string {
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if endpoint.ID == endpointID {
				return endpoint.TransportID
			}
		}
		t.Fatalf("画像缺少 endpoint：%s", endpointID)
		return ""
	}
	activeTransport := findTransport(active)
	previousTransport := findTransport(previous)
	if activeTransport == previousTransport {
		t.Fatal("异版本合成画像未形成 transport ID 差异")
	}
	route := CatalogRoute{
		Key: RouteKey{
			Method: "GET", Host: "chatgpt.com", Path: "/backend-api/wham/usage",
			Purpose: Purpose("quota_query"),
		},
		Protocol: WireProtocolHTTP,
	}
	inputs := []SinkBindingInput{{
		ID: SinkCodexQuotaWHAM, Persona: PersonaCodexCLI,
		EnforcementState: SinkStateEnforced,
		migrationReceipt: &MigrationReceipt{routeClaims: []migrationRouteClaim{{
			route: route, evidenceID: endpointID,
			transportID: "codex-0.145.0-http-ubuntu24-native",
		}}},
	}}
	entry := transportReceiptTransition{
		SinkID:              string(SinkCodexQuotaWHAM),
		Route:               receiptRouteIdentityForTransportTest(route),
		EvidenceID:          endpointID,
		PreviousTransportID: "codex-0.145.0-http-ubuntu24-native",
		CurrentTransportID:  previousTransport,
		CurrentLifecycle:    profilecontract.LifecycleBackendClientLongLived,
		Reason:              "异版本 transport 绑定门禁自测",
	}
	if err := applyTransportReceiptTransitionEntry(
		inputs,
		map[SinkID]int{SinkCodexQuotaWHAM: 0},
		entry,
		map[string]bool{},
		catalog,
	); err != nil {
		t.Fatal(err)
	}
	inputs, err = bindMigrationReceiptTransports(inputs, catalog)
	if err != nil {
		t.Fatal(err)
	}
	claim := inputs[0].migrationReceipt.routeClaims[0]
	if !claim.matchesTransport(active.ReleaseDigest(), activeTransport) ||
		!claim.matchesTransport(previous.ReleaseDigest(), previousTransport) {
		t.Fatal("transport 未按 ReleaseDigest 精确绑定")
	}
	if claim.matchesTransport(active.ReleaseDigest(), previousTransport) ||
		claim.matchesTransport(previous.ReleaseDigest(), activeTransport) {
		t.Fatal("transport 可跨 ReleaseDigest 混用")
	}
}

func TestEveryCodexMigrationClaimHasReleaseScopedTransport(t *testing.T) {
	for _, binding := range DefaultSinkCatalog().Bindings() {
		if binding.Persona() != PersonaCodexCLI || binding.migrationReceipt == nil {
			continue
		}
		for _, claim := range binding.migrationReceipt.routeClaims {
			if len(claim.transportIDsByRelease) != 2 {
				t.Fatalf(
					"Codex MigrationReceipt 未绑定 Active/Previous transport：%s/%s",
					binding.ID(), claim.evidenceID,
				)
			}
		}
	}
}

func receiptRouteIdentityForTransportTest(route CatalogRoute) receiptcontract.RouteIdentity {
	return receiptcontract.RouteIdentity{
		Method:   route.Key.Method,
		Host:     route.Key.Host,
		Path:     route.Key.Path,
		Purpose:  string(route.Key.Purpose),
		Protocol: string(route.Protocol),
	}
}
