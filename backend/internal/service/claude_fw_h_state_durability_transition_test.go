package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
)

const (
	claudeFWHStateDurabilityServiceSourceSHA256 = "4d529a58d06a8a597da17b477c17fb5ee99dc6e2b6728e8adeaf3afb98f0fa60"
	claudeFWHStateDurabilityServiceTestSHA256   = "5ab18f3424aa6b2300ae865999019c586814896a0d805c6fed6a48eaa7647d40"
)

func claudeFWHStateDurabilityTransitionSupersedesService(
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
			"../../../docs/egress/maintenance/claude-fw-h-state-durability-source-transition.json",
			claudeFWHStateDurabilityServiceSourceSHA256,
			"official-egress-claude-fw-h-state-durability-source-transition/v1",
		},
		{
			"../../../docs/egress/maintenance/claude-fw-h-state-durability-test-transition.json",
			claudeFWHStateDurabilityServiceTestSHA256,
			"official-egress-claude-fw-h-state-durability-test-transition/v1",
		},
	} {
		raw, err := os.ReadFile(item.path)
		if err != nil || compatibilityClosureDigest(raw) != item.digest {
			return false
		}
		var receipt struct {
			SchemaVersion string                                 `json:"schema_version"`
			Date          string                                 `json:"date"`
			Phase         string                                 `json:"phase"`
			BaseCommit    string                                 `json:"base_commit"`
			Scope         string                                 `json:"scope"`
			Transitions   []compatibilityClosureSourceTransition `json:"transitions"`
			Result        string                                 `json:"result"`
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		if err := decoder.Decode(&receipt); err != nil {
			return false
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
			receipt.SchemaVersion != item.schema || receipt.Date != "2026-08-22" ||
			receipt.Phase != "FW-H" ||
			receipt.BaseCommit != "571370f3be67df3ccca594cd3e3b1eecf15e4421" ||
			receipt.Scope != "production_state_durability_correction" ||
			receipt.Result != "passed" {
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
