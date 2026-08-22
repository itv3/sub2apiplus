package officialegress

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
)

//go:embed catalogdata/fw-e-legacy-observation-sinks.json
var embeddedLegacyObservationSinks []byte

type legacyObservationManifest struct {
	SchemaVersion int                      `json:"schema_version"`
	CampaignStage string                   `json:"campaign_stage"`
	Entries       []legacyObservationEntry `json:"entries"`
}

type legacyObservationEntry struct {
	SinkID           string                   `json:"sink_id"`
	Purpose          string                   `json:"purpose"`
	SourceRef        string                   `json:"source_ref"`
	EndpointEvidence string                   `json:"endpoint_evidence"`
	Routes           []legacyObservationRoute `json:"routes"`
	TargetBackend    string                   `json:"target_backend"`
	LegacyBackends   []string                 `json:"legacy_backends"`
	Owner            string                   `json:"owner"`
	ExpiryCondition  string                   `json:"expiry_condition"`
}

type legacyObservationRoute struct {
	Method   string `json:"method"`
	Host     string `json:"host"`
	Path     string `json:"path"`
	Protocol string `json:"protocol"`
}

// applyLegacyObservationSinks 只追加 FW-E 已冻结遗留调用点的 observation-only
// binding。它不登记 Claude Persona、画像、Release 或生产 selector，且状态被固定为
// legacy_observe；后续状态提升仍必须经过正式 MigrationReceipt。
func applyLegacyObservationSinks(inputs []SinkBindingInput) ([]SinkBindingInput, error) {
	var manifest legacyObservationManifest
	decoder := json.NewDecoder(bytes.NewReader(embeddedLegacyObservationSinks))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("解析 FW-E 遗留观察 Sink：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("FW-E 遗留观察 Sink 清单尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 1 || manifest.CampaignStage != "FW-E" || len(manifest.Entries) == 0 {
		return nil, errors.New("FW-E 遗留观察 Sink 清单元数据非法")
	}

	existing := make(map[SinkID]struct{}, len(inputs)+len(manifest.Entries))
	for _, input := range inputs {
		existing[input.ID] = struct{}{}
	}
	result := append([]SinkBindingInput(nil), inputs...)
	previousID := ""
	for _, entry := range manifest.Entries {
		if strings.TrimSpace(entry.SinkID) == "" || previousID >= entry.SinkID {
			return nil, errors.New("FW-E 遗留观察 Sink 必须按 SinkID 严格排序")
		}
		previousID = entry.SinkID
		if strings.TrimSpace(entry.Purpose) == "" || strings.TrimSpace(entry.SourceRef) == "" ||
			entry.EndpointEvidence != string(EndpointEvidenceExternalPersona) ||
			strings.TrimSpace(entry.Owner) == "" || strings.TrimSpace(entry.ExpiryCondition) == "" ||
			len(entry.Routes) == 0 || len(entry.LegacyBackends) == 0 {
			return nil, fmt.Errorf("FW-E 遗留观察 Sink 字段不完整：%s", entry.SinkID)
		}
		sinkID := SinkID(entry.SinkID)
		if _, duplicate := existing[sinkID]; duplicate {
			return nil, fmt.Errorf("FW-E 遗留观察 SinkID 重复：%s", entry.SinkID)
		}
		existing[sinkID] = struct{}{}

		routes := make([]CatalogRoute, 0, len(entry.Routes))
		previousRoute := ""
		for _, item := range entry.Routes {
			route := CatalogRoute{Key: RouteKey{
				Method: strings.ToUpper(strings.TrimSpace(item.Method)),
				Host:   normalizeRouteHost(item.Host), Path: strings.TrimSpace(item.Path),
				Purpose: Purpose(entry.Purpose),
			}, Protocol: WireProtocol(item.Protocol)}
			identity := catalogRouteIdentity(route)
			if err := route.Validate(); err != nil || previousRoute >= identity {
				return nil, fmt.Errorf("FW-E 遗留观察 route 非法或未排序：%s", entry.SinkID)
			}
			previousRoute = identity
			routes = append(routes, route)
		}

		legacyBackends := make([]BackendKind, 0, len(entry.LegacyBackends))
		for _, raw := range entry.LegacyBackends {
			backend := BackendKind(raw)
			if !backend.Valid() {
				return nil, fmt.Errorf("FW-E 遗留观察 backend 非法：%s/%s", entry.SinkID, raw)
			}
			legacyBackends = append(legacyBackends, backend)
		}
		if !sort.SliceIsSorted(legacyBackends, func(i, j int) bool {
			return legacyBackends[i] < legacyBackends[j]
		}) {
			return nil, fmt.Errorf("FW-E 遗留观察 backend 未排序：%s", entry.SinkID)
		}
		targetBackend := BackendKind(entry.TargetBackend)
		if !targetBackend.Valid() {
			return nil, fmt.Errorf("FW-E 遗留观察目标 backend 非法：%s", entry.SinkID)
		}
		result = append(result, SinkBindingInput{
			ID: sinkID, Purpose: Purpose(entry.Purpose), Persona: PersonaUnclassified,
			EndpointEvidence: EndpointEvidenceExternalPersona, Routes: routes,
			TargetBackend: targetBackend, LegacyBackends: legacyBackends,
			EnforcementState: SinkStateLegacyObserve, Owner: entry.Owner,
			MigrationChangeset: manifest.CampaignStage,
			ExpiryCondition:    entry.ExpiryCondition,
			RuntimeBindable:    true,
		})
	}
	// Setup Token 不属于 Claude OAuth Persona，但推理与 token_count 是必须保留的
	// 产品能力。它们作为 post-bootstrap 的 non_persona_managed Sink 直接进入默认
	// Catalog，不得倒灌 sealed legacy baseline，也不受 Claude Persona selector 控制。
	for sinkID, definition := range claudeSetupTokenManagedBindings {
		if _, duplicate := existing[sinkID]; duplicate {
			return nil, fmt.Errorf("Claude Setup Token 受管 SinkID 重复：%s", sinkID)
		}
		existing[sinkID] = struct{}{}
		policy := completeClaudeFWGManagedPolicy(definition.policy)
		result = append(result, SinkBindingInput{
			ID: sinkID, Purpose: definition.purpose, Persona: PersonaUnclassified,
			EndpointEvidence: EndpointEvidenceExternalPersona,
			Routes:           []CatalogRoute{definition.route}, TargetBackend: BackendHTTPUpstream,
			LegacyBackends:   []BackendKind{BackendHTTPUpstream},
			EnforcementState: SinkStateEnforced,
			Owner:            "official-client-fw-h/setup-token", MigrationChangeset: "FW-H",
			ExpiryCondition: "Setup Token 产品能力被明确迁移或退休",
			RuntimeBindable: true, ManagedPolicy: &policy,
		})
	}
	return result, nil
}
