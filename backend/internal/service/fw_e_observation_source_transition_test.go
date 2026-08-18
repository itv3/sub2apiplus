package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
)

const fwEObservationSourceTransitionSHA256 = "dce3acb964556fdf6b6c1338463598d9256784831fa2e2667b143224da7dd36b"

type fwEObservationSourceTransitionReceipt struct {
	SchemaVersion     string `json:"schema_version"`
	Date              string `json:"date"`
	Stage             string `json:"stage"`
	BaseCommit        string `json:"base_commit"`
	SourceTransitions []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	Result string `json:"result"`
}

func loadFWEObservationSourceTransition() (
	fwEObservationSourceTransitionReceipt,
	bool,
) {
	var receipt fwEObservationSourceTransitionReceipt
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/fw-e-observation-source-transition.json",
	)
	if err != nil {
		return receipt, false
	}
	digest := sha256.Sum256(raw)
	if hex.EncodeToString(digest[:]) != fwEObservationSourceTransitionSHA256 ||
		json.Unmarshal(raw, &receipt) != nil ||
		receipt.SchemaVersion != "official-client-fw-e-observation-source-transition/v1" ||
		receipt.Date != "2026-08-18" || receipt.Stage != "FW-E" ||
		receipt.BaseCommit != "7cbbb76e37118479a4618702357b62a95e9c88ec" ||
		receipt.Result != "passed" || len(receipt.SourceTransitions) == 0 {
		return receipt, false
	}
	return receipt, true
}

func fwEObservationSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, ok := loadFWEObservationSourceTransition()
	if !ok {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest && transition.Reason != "" {
			return true
		}
	}
	return false
}

func fwEObservationSourceTransitionPrior(path string, currentDigest string) (string, bool) {
	receipt, ok := loadFWEObservationSourceTransition()
	if !ok {
		return "", false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest {
			return transition.FromSHA256, true
		}
	}
	return "", false
}
