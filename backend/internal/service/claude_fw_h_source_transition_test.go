package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
)

const claudeFWHServiceSourceTransitionSHA256 = "b4638d412cfcd34e144dfbcce9b2326d2f6ed4df1987b55cac2c3209d3a5aa5f"

type claudeFWHServiceSourceTransitionReceipt struct {
	SchemaVersion string                                 `json:"schema_version"`
	Date          string                                 `json:"date"`
	Phase         string                                 `json:"phase"`
	BaseCommit    string                                 `json:"base_commit"`
	Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
	Result        string                                 `json:"result"`
}

type claudeOfficialClientOnlyServiceTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	BaseCommit    string `json:"base_commit"`
	Scope         string `json:"scope"`
	Target        struct {
		IngressPolicy string `json:"ingress_policy"`
	} `json:"target"`
	Transitions []compatibilityClosureSourceTransition `json:"transitions"`
	Safety      struct {
		DMITDeployed  bool   `json:"dmit_deployed"`
		DMITState     string `json:"dmit_state"`
		VircsAccessed bool   `json:"vircs_accessed"`
		VircsChanged  bool   `json:"vircs_changed"`
		VircsState    string `json:"vircs_state"`
	} `json:"safety"`
	Result string `json:"result"`
}

// claudeFWHSourceTransitionSupersedesService 只消费由 officialegress 包完整
// 验证过的不可变 FW-H 收据，并只接受精确 path/from/to 后继关系。
func claudeFWHSourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeOfficialClientOnlyTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWHStateDurabilityTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWHLegacyRetirementTransitionSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/claude-fw-h-source-transition.json",
	)
	if err != nil || compatibilityClosureDigest(raw) != claudeFWHServiceSourceTransitionSHA256 {
		return false
	}
	var receipt claudeFWHServiceSourceTransitionReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(&receipt); err != nil {
		return false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
		receipt.SchemaVersion != "official-egress-claude-fw-h-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "619a63818f49ecc0b99bf4a16b7d776f939a452e" ||
		receipt.Result != "passed" || len(receipt.Transitions) == 0 {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			(transition.ToSHA256 == currentDigest ||
				claudeFWHLegacyRetirementTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				) ||
				claudeFWHStateDurabilityTransitionSupersedesService(
					path, transition.ToSHA256, currentDigest,
				)) {
			return true
		}
	}
	return false
}

// claudeOfficialClientOnlyTransitionSupersedesService 消费由 officialegress
// 测试包固定摘要并完整校验的后继收据。这里再次约束顶层身份与安全边界，且只接受
// 同一路径的精确 from→to；历史摘要只能经已冻结的 FW-H 收据到达本轮 from。
func claudeOfficialClientOnlyTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if len(priorDigest) != 64 || len(currentDigest) != 64 ||
		priorDigest == currentDigest {
		return false
	}
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/claude-official-client-only-transition.json",
	)
	if err != nil {
		return false
	}
	var receipt claudeOfficialClientOnlyServiceTransitionReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(&receipt); err != nil {
		return false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
		receipt.SchemaVersion !=
			"official-egress-claude-official-client-only-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T14:00:00Z" ||
		receipt.BaseCommit != "3d5967919" ||
		receipt.Scope != "claude-official-client-only-dmit-candidate" ||
		receipt.Target.IngressPolicy != "official-client-only" ||
		receipt.Result != "sealed_for_dmit_validation" ||
		len(receipt.Transitions) < 15 || receipt.Safety.DMITDeployed ||
		receipt.Safety.DMITState != "not_yet_validated" ||
		receipt.Safety.VircsAccessed || receipt.Safety.VircsChanged ||
		receipt.Safety.VircsState != "operator_managed/unverified/not_touched" {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest ||
			len(transition.FromSHA256) != 64 ||
			transition.FromSHA256 == transition.ToSHA256 {
			continue
		}
		if transition.FromSHA256 == priorDigest ||
			claudeFWHLegacyRetirementTransitionSupersedesService(
				path, priorDigest, transition.FromSHA256,
			) || claudeFWHStateDurabilityTransitionSupersedesService(
			path, priorDigest, transition.FromSHA256,
		) {
			return true
		}
	}
	return false
}
