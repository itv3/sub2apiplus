package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

const (
	sub2APIRuntimeStabilityTransitionPath = "../../../docs/egress/maintenance/sub2api-runtime-stability-source-transition.json"
	sub2APIRuntimeStabilityTransitionSHA  = "48827266d2c184311f455aed95f6aea70faab025927c1edb999c3a76a0459676"
)

type sub2APIRuntimeStabilitySourceTransition struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type sub2APIRuntimeStabilityTransitionReceipt struct {
	SchemaVersion string                                    `json:"schema_version"`
	Date          string                                    `json:"date"`
	BaseCommit    string                                    `json:"base_commit"`
	ReleaseTag    string                                    `json:"release_tag"`
	Scope         string                                    `json:"scope"`
	Transitions   []sub2APIRuntimeStabilitySourceTransition `json:"transitions"`
	Verification  []string                                  `json:"verification"`
	Result        string                                    `json:"result"`
}

// loadSub2APIRuntimeStabilityTransitionReceipt 只接受固定摘要和完整顶层事实，
// 使历史门禁只能经由本次明确登记的 path/from/to 后继关系到达新源码。
func loadSub2APIRuntimeStabilityTransitionReceipt() (
	sub2APIRuntimeStabilityTransitionReceipt,
	bool,
) {
	raw, err := os.ReadFile(sub2APIRuntimeStabilityTransitionPath)
	if err != nil || sub2APIRuntimeStabilityDigest(raw) != sub2APIRuntimeStabilityTransitionSHA {
		return sub2APIRuntimeStabilityTransitionReceipt{}, false
	}
	var receipt sub2APIRuntimeStabilityTransitionReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return sub2APIRuntimeStabilityTransitionReceipt{}, false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
		receipt.SchemaVersion != "sub2api-runtime-stability-source-transition/v1" ||
		receipt.Date != "2026-08-23" ||
		receipt.BaseCommit != "216bb205be76136358b27b0f0af10b463701522a" ||
		receipt.ReleaseTag != "v0.1.177-6" ||
		receipt.Scope != "turn_state_websocket_pool_and_log_stability" ||
		len(receipt.Transitions) != 7 || len(receipt.Verification) != 6 ||
		receipt.Result != "passed" {
		return sub2APIRuntimeStabilityTransitionReceipt{}, false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == "" || len(transition.FromSHA256) != 64 ||
			len(transition.ToSHA256) != 64 ||
			transition.FromSHA256 == transition.ToSHA256 || transition.Reason == "" {
			return sub2APIRuntimeStabilityTransitionReceipt{}, false
		}
	}
	return receipt, true
}

func sub2APIRuntimeStabilityTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, ok := loadSub2APIRuntimeStabilityTransitionReceipt()
	if !ok {
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

// sub2APIRuntimeStabilityTransitionPrior 返回当前摘要在本次收据中的唯一前驱，
// 供旧的不可变收据链先验证到该前驱，再由本收据承接到当前源码。
func sub2APIRuntimeStabilityTransitionPrior(
	path string,
	currentDigest string,
) (string, bool) {
	receipt, ok := loadSub2APIRuntimeStabilityTransitionReceipt()
	if !ok {
		return "", false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest {
			return transition.FromSHA256, true
		}
	}
	return "", false
}

func TestSub2APIRuntimeStabilityTransitionMatchesCurrentSources(t *testing.T) {
	receipt, ok := loadSub2APIRuntimeStabilityTransitionReceipt()
	if !ok {
		t.Fatal("Sub2API 运行稳定性源码 transition 收据无效")
	}
	repoRoot := filepath.Clean("../../..")
	seen := make(map[string]struct{}, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if _, exists := seen[transition.Path]; exists {
			t.Fatalf("Sub2API 运行稳定性源码 transition 路径重复：%s", transition.Path)
		}
		seen[transition.Path] = struct{}{}
		raw, err := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(transition.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := sub2APIRuntimeStabilityDigest(raw); got != transition.ToSHA256 {
			t.Fatalf(
				"Sub2API 运行稳定性源码摘要漂移：path=%s got=%s want=%s",
				transition.Path,
				got,
				transition.ToSHA256,
			)
		}
	}
}

func sub2APIRuntimeStabilityDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
