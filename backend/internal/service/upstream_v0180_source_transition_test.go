package service

import (
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"
)

const (
	upstreamV0180SourceTransitionPath  = "docs/egress/maintenance/upstream-v0.1.180-source-transition.json"
	upstreamV0180SourceTransitionCount = 112
)

var (
	upstreamV0180SourceTransitionOnce    sync.Once
	upstreamV0180SourceTransitionCached  upstreamV0179SourceTransitionReceipt
	upstreamV0180SourceTransitionLoadErr error
)

func loadUpstreamV0180SourceTransitionServiceReceipt() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	upstreamV0180SourceTransitionOnce.Do(func() {
		upstreamV0180SourceTransitionCached,
			upstreamV0180SourceTransitionLoadErr =
			readUpstreamV0180SourceTransitionServiceReceipt()
	})
	return upstreamV0180SourceTransitionCached, upstreamV0180SourceTransitionLoadErr
}

func readUpstreamV0180SourceTransitionServiceReceipt() (
	upstreamV0179SourceTransitionReceipt,
	error,
) {
	var receipt upstreamV0179SourceTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", upstreamV0180SourceTransitionPath))
	if err != nil {
		return receipt, err
	}
	if err := decodeStrictUpstreamSourceTransition(raw, &receipt); err != nil {
		return receipt, err
	}
	if err := validateUpstreamSourceTransitionIdentity(raw, receipt.IdentitySHA256, true); err != nil {
		return receipt, err
	}
	if receipt.SchemaVersion != "official-egress-upstream-source-transition/v2" ||
		receipt.Date != "2026-08-25" ||
		receipt.BaseCommit != "2dbbe53a3c85cfae2737bc7de85328c42c9fa16e" ||
		receipt.PlanIdentitySHA256 != "0e3d358fed57a721cf5637dc1609d2a1c7e0adbf0d08230ff7ae6b66746ac954" ||
		receipt.UpstreamTag != "v0.1.180" ||
		receipt.UpstreamCommit != "c40edb4070a9274e8c23f161b4ed552051b14698" ||
		receipt.Classification != "implementation_and_validation" ||
		receipt.ActivationStatus != "accepted_not_activated" ||
		receipt.TargetClients["claude"] != "2.1.226" ||
		receipt.TargetClients["codex"] != "0.147.0" ||
		len(receipt.Transitions) != upstreamV0180SourceTransitionCount ||
		len(receipt.AllowedWireDeltas) != 0 || len(receipt.Verification) < 5 ||
		receipt.Result != "passed" {
		return receipt, os.ErrInvalid
	}
	// 最新 transition 在加载期间不能通过历史校验器回调自身，否则 sync.Once
	// 会等待同一次加载完成而死锁。先复用结构校验，再对当前文件做精确摘要校验。
	if err := validateUpstreamServiceTransitionEntries(receipt.Transitions, false); err != nil {
		return receipt, err
	}
	for _, transition := range receipt.Transitions {
		current, readErr := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(transition.Path),
		))
		currentDigest := upstreamMergeFrameworkServiceDigest(current)
		if readErr != nil || (currentDigest != transition.ToSHA256 &&
			!codex01491TerminalStateSupersedesService(
				transition.Path, transition.ToSHA256, currentDigest,
			)) {
			return receipt, os.ErrInvalid
		}
	}
	return receipt, nil
}

// upstreamV0180SourceTransitionSupersedesService 只接受本次合并固定的
// path／predecessor／to 三元组，不扩大官方 OAuth 发送权限。
func upstreamV0180SourceTransitionSupersedesService(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if codex01491TerminalStateSupersedesService(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadUpstreamV0180SourceTransitionServiceReceipt()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path &&
			slices.Contains(transition.PredecessorSHA256s, priorDigest) {
			return transition.ToSHA256 == currentDigest ||
				codex01491TerminalStateSupersedesService(
					path, transition.ToSHA256, currentDigest,
				)
		}
	}
	return false
}

func TestUpstreamV0180SourceTransitionIsFrozen(t *testing.T) {
	if _, err := loadUpstreamV0180SourceTransitionServiceReceipt(); err != nil {
		t.Fatal(err)
	}
}
