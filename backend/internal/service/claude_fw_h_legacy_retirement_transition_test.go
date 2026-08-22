package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
)

const (
	claudeFWHLegacyRetirementServiceSourceSHA256 = "13918c77227fcfff5f44daa77d0382630e37998fd926bab2334cb9392bb7eabd"
	claudeFWHLegacyRetirementServiceTestSHA256   = "6eae1f11f3abc5050ead6a31a7eb79ad5ef7e7066915fc2666fa45dcba8304e0"
)

type claudeFWHLegacyRetirementServiceReceipt struct {
	SchemaVersion string                                 `json:"schema_version"`
	Date          string                                 `json:"date"`
	Phase         string                                 `json:"phase"`
	BaseCommit    string                                 `json:"base_commit"`
	Scope         string                                 `json:"scope"`
	Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
	Result        string                                 `json:"result"`
}

func claudeFWHLegacyRetirementTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	for _, item := range []struct {
		path   string
		digest string
		schema string
	}{
		{
			"../../../docs/egress/maintenance/claude-fw-h-legacy-retirement-source-transition.json",
			claudeFWHLegacyRetirementServiceSourceSHA256,
			"official-egress-claude-fw-h-legacy-retirement-source-transition/v1",
		},
		{
			"../../../docs/egress/maintenance/claude-fw-h-legacy-retirement-test-transition.json",
			claudeFWHLegacyRetirementServiceTestSHA256,
			"official-egress-claude-fw-h-legacy-retirement-test-transition/v1",
		},
	} {
		raw, err := os.ReadFile(item.path)
		if err != nil || compatibilityClosureDigest(raw) != item.digest {
			return false
		}
		var receipt claudeFWHLegacyRetirementServiceReceipt
		decoder := json.NewDecoder(bytes.NewReader(raw))
		if err := decoder.Decode(&receipt); err != nil {
			return false
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
			receipt.SchemaVersion != item.schema || receipt.Date != "2026-08-22" ||
			receipt.Phase != "FW-H" ||
			receipt.BaseCommit != "c51fb5d2cf052f47db9f27926d8340c2c218e2af" ||
			receipt.Scope != "legacy_retirement" || receipt.Result != "passed" {
			return false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				transition.ToSHA256 == currentDigest {
				return true
			}
		}
	}
	return false
}
