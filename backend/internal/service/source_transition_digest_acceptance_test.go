package service

import (
	"errors"
	"os"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/testsupport/freezeproof"
)

// auditedSourceCurrentStateAcceptedService 区分存在的文件与带证明的删除，读取错误不会被当作删除。
// 缺失文件必须通过共享校验器核对提交、精确路径、删除前摘要和完整删除证明。
func auditedSourceCurrentStateAcceptedService(path, expectedDigest string, current []byte, readErr error) bool {
	if readErr == nil {
		currentDigest := upstreamMergeFrameworkServiceDigest(current)
		return expectedDigest == currentDigest || auditedSourceSuccessorReachesService(path, expectedDigest, currentDigest)
	}
	return errors.Is(readErr, os.ErrNotExist) &&
		freezeproof.DeletedSourceAccepted("../../..", path, expectedDigest, auditedSourceSuccessorReachesService)
}

// 实际退役文件只可由既有精确删除证明承接；改路径、改摘要或其它读取错误都必须拒绝。
func TestAuditedSourceCurrentStateAcceptedServiceRequiresDeletionProof(t *testing.T) {
	const path = "backend/internal/service/account_test_service_keeper.go"
	const deletedDigest = "82b3dd51545f43f59f2ac70b3b518ec1bc1da8a338f945679794881857b20b08"
	const unknownDigest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if !auditedSourceCurrentStateAcceptedService(path, deletedDigest, nil, os.ErrNotExist) {
		t.Fatal("已封存的精确删除证明必须能承接退役文件")
	}
	for name, state := range map[string]struct {
		path    string
		digest  string
		current []byte
		err     error
	}{
		"未知路径":  {path: path + ".unknown", digest: deletedDigest, err: os.ErrNotExist},
		"未知摘要":  {path: path, digest: unknownDigest, err: os.ErrNotExist},
		"权限错误":  {path: path, digest: deletedDigest, err: os.ErrPermission},
		"未登记正文": {path: path, digest: deletedDigest, current: []byte("未经登记的替代正文")},
	} {
		t.Run(name, func(t *testing.T) {
			if auditedSourceCurrentStateAcceptedService(state.path, state.digest, state.current, state.err) {
				t.Fatal("未登记状态不得借用历史删除证明")
			}
		})
	}
}

// codex0151CurrentSourceDigestAcceptedService 只承认已登记的 successor 状态：收据精确目标摘要、
// 当前 0.151 工作区 successor、历史漂移账本，或本次扫描器修复 successor。
// 它不读取目录、不生成新收据，也不会把任意未登记的工作树修改视为合法。
func codex0151CurrentSourceDigestAcceptedService(path, expectedDigest, currentDigest string) bool {
	return expectedDigest == currentDigest ||
		auditedSourceSuccessorReachesService(path, expectedDigest, currentDigest) ||
		upstreamV023PostBootstrapSourceSuccessorSupersedesService(path, expectedDigest, currentDigest) ||
		upstreamMergeFrameworkV3SuccessorSupersedesService(
			path, expectedDigest, currentDigest,
		) ||
		codex0151WorktreeSuccessorEdgeService(path, expectedDigest, currentDigest) ||
		historicalSourceDriftSupersedes(path, expectedDigest, currentDigest) ||
		upstreamV023ScannerSuccessorTransitionSupersedesService(path, expectedDigest, currentDigest) ||
		upstreamV023SourceTransitionSupersedesService(path, expectedDigest, currentDigest)
}
