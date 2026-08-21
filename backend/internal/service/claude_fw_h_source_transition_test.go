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

// claudeFWHSourceTransitionSupersedesService 只消费由 officialegress 包完整
// 验证过的不可变 FW-H 收据，并只接受精确 path/from/to 后继关系。
func claudeFWHSourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
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
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}
