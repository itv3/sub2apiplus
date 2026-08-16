package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
)

const upstreamV0177SourceTransitionSHA256 = "ecb95c725ac58b3fa270eeb413e5887f83a13692c153fe0da50e820d34046ec2"

// upstreamV0177SourceTransitionSupersedes 验证历史摘要是否由本次上游合并的
// 固定 transition 精确承接。旧退休收据保持不可变，路径和摘要均不得模糊匹配。
func upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	raw, err := os.ReadFile("../../../docs/egress/maintenance/upstream-v0.1.177-source-transition.json")
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != upstreamV0177SourceTransitionSHA256 {
		return false
	}
	var receipt struct {
		SchemaVersion     string `json:"schema_version"`
		SourceTransitions []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
		} `json:"source_transitions"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil ||
		receipt.SchemaVersion != "official-egress-upstream-source-transition/v1" {
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
