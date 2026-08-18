package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const fwEObservationSourceTransitionSHA256 = "dce3acb964556fdf6b6c1338463598d9256784831fa2e2667b143224da7dd36b"

type fwEObservationSourceTransitionReceipt struct {
	SchemaVersion     string `json:"schema_version"`
	Date              string `json:"date"`
	Stage             string `json:"stage"`
	BaseCommit        string `json:"base_commit"`
	Scope             string `json:"scope"`
	SourceTransitions []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	AllowedWireDeltas []string `json:"allowed_wire_deltas"`
	Result            string   `json:"result"`
}

func readFWEObservationSourceTransition() (
	fwEObservationSourceTransitionReceipt,
	[]byte,
	error,
) {
	var receipt fwEObservationSourceTransitionReceipt
	raw, err := os.ReadFile(
		"../../../docs/egress/maintenance/fw-e-observation-source-transition.json",
	)
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("FW-E observation source transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateFWEObservationSourceTransition(
	receipt fwEObservationSourceTransitionReceipt,
	raw []byte,
) error {
	if sha256Hex(raw) != fwEObservationSourceTransitionSHA256 ||
		receipt.SchemaVersion != "official-client-fw-e-observation-source-transition/v1" ||
		receipt.Date != "2026-08-18" || receipt.Stage != "FW-E" ||
		receipt.BaseCommit != "7cbbb76e37118479a4618702357b62a95e9c88ec" ||
		strings.TrimSpace(receipt.Scope) == "" || len(receipt.SourceTransitions) == 0 ||
		len(receipt.AllowedWireDeltas) != 0 || receipt.Result != "passed" {
		return errors.New("FW-E observation source transition 顶层事实非法")
	}
	paths := make([]string, 0, len(receipt.SourceTransitions))
	for _, transition := range receipt.SourceTransitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("FW-E observation source transition 条目不完整")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("FW-E observation source transition 路径未排序或重复")
	}
	return nil
}

// fwEObservationSourceTransitionSupersedes 只接受本阶段冻结收据中的精确
// path/from/to 后继关系；历史收据继续保持不可变。
func fwEObservationSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, raw, err := readFWEObservationSourceTransition()
	if err != nil || validateFWEObservationSourceTransition(receipt, raw) != nil {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func fwEObservationSourceTransitionPrior(path string, currentDigest string) (string, bool) {
	receipt, raw, err := readFWEObservationSourceTransition()
	if err != nil || validateFWEObservationSourceTransition(receipt, raw) != nil {
		return "", false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest {
			return transition.FromSHA256, true
		}
	}
	return "", false
}

func TestFWEObservationSourceTransitionIsFrozen(t *testing.T) {
	receipt, raw, err := readFWEObservationSourceTransition()
	if err != nil {
		t.Fatal(err)
	}
	if err := validateFWEObservationSourceTransition(receipt, raw); err != nil {
		t.Fatal(err)
	}
	for _, transition := range receipt.SourceTransitions {
		source, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := sha256Hex(source); got != transition.ToSHA256 {
			t.Fatalf(
				"FW-E observation source transition 漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}
