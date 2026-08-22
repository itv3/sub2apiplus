package officialegress

import (
	"errors"
	"slices"
	"strings"
	"testing"
)

const (
	claudeFWHBareChatRouteSourceSHA256 = "bc4f57df92768a71b5210737121861d9c2dd8234d56e56c84e1a256bd31d60ec"
	claudeFWHBareChatRouteTestSHA256   = "d3b0312e4f1f2032b36e136f04d60653698f2e1de071c68f4f6c94b4a19fae18"
)

func loadClaudeFWHBareChatRouteTransition(
	path string,
) (claudeFWHThirdPartyStrictTransitionReceipt, []byte, error) {
	receipt, raw, err := readClaudeFWHThirdPartyStrictTransition(path)
	if err != nil {
		return receipt, nil, err
	}
	isSource := receipt.SourceTransition == nil
	wantSchema := "claude-fw-h-bare-chat-route-test-transition/v1"
	wantDigest := claudeFWHBareChatRouteTestSHA256
	wantTransitions := 5
	if isSource {
		wantSchema = "claude-fw-h-bare-chat-route-source-transition/v1"
		wantDigest = claudeFWHBareChatRouteSourceSHA256
		wantTransitions = 1
	}
	if claudeFWHSourceDigest(raw) != wantDigest ||
		receipt.SchemaVersion != wantSchema || receipt.Date != "2026-08-22" ||
		receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "0e0d8960621dad12ea57bfd66fec9380de901fc9" ||
		receipt.Scope != "candidate_route_repair" ||
		receipt.Predecessor.Path !=
			"docs/egress/maintenance/claude-fw-h-third-party-strict-candidate-approval.json" ||
		receipt.Predecessor.SHA256 != claudeFWHThirdPartyStrictApprovalSHA256 ||
		!slices.Equal(receipt.LogicalIngressIDs, []string{"chat-completions-oauth"}) ||
		len(receipt.Transitions) != wantTransitions || receipt.Result != "passed" {
		return receipt, nil, errors.New("Claude FW-H 裸 Chat 路由 transition 顶层事实非法")
	}
	if !isSource && (receipt.SourceTransition.Path !=
		"docs/egress/maintenance/claude-fw-h-bare-chat-route-source-transition.json" ||
		receipt.SourceTransition.SHA256 != claudeFWHBareChatRouteSourceSHA256 ||
		len(receipt.Gates) != 5) {
		return receipt, nil, errors.New("Claude FW-H 裸 Chat 路由测试引用非法")
	}
	if err := validateClaudeFWHBareChatRouteTransitionEntries(receipt.Transitions); err != nil {
		return receipt, nil, err
	}
	if err := validateClaudeFWHThirdPartyStrictSafety(receipt); err != nil {
		return receipt, nil, err
	}
	return receipt, raw, nil
}

// 裸 Chat 后继在装载时只校验自身条目结构；目标摘要由本文件的冻结测试校验。
// 这里不能调用第三方 strict 的当前目标校验，否则两条后继链会相互递归。
func validateClaudeFWHBareChatRouteTransitionEntries(
	transitions []changeset4SourceTransitionEntry,
) error {
	paths := make([]string, 0, len(transitions))
	for _, transition := range transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			len(transition.FromSHA256) != 64 || len(transition.ToSHA256) != 64 ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-H 裸 Chat 路由 transition 条目非法")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude FW-H 裸 Chat 路由 transition 路径未排序或重复")
	}
	return nil
}

func claudeFWHBareChatRouteTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHLegacyRetirementTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	for _, receiptPath := range []string{
		"docs/egress/maintenance/claude-fw-h-bare-chat-route-source-transition.json",
		"docs/egress/maintenance/claude-fw-h-bare-chat-route-test-transition.json",
	} {
		receipt, _, err := loadClaudeFWHBareChatRouteTransition(receiptPath)
		if err != nil {
			return false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path == path &&
				(transition.ToSHA256 == currentDigest ||
					claudeFWHLegacyRetirementTransitionSupersedes(
						path, transition.ToSHA256, currentDigest,
					)) &&
				(transition.FromSHA256 == priorDigest ||
					claudeFWGThinkingDisplayTransitionSupersedes(
						path, priorDigest, transition.FromSHA256,
					)) {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWHBareChatRouteTransitionsAreFrozen(t *testing.T) {
	for _, receiptPath := range []string{
		"docs/egress/maintenance/claude-fw-h-bare-chat-route-source-transition.json",
		"docs/egress/maintenance/claude-fw-h-bare-chat-route-test-transition.json",
	} {
		if _, _, err := loadClaudeFWHBareChatRouteTransition(receiptPath); err != nil {
			t.Fatal(err)
		}
	}
}
