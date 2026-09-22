#!/bin/bash
# VC-4 实现测试：参数 <证据根>。在候选 commit 的干净副本（gate-tree：无 vendor、无前端 dist）上跑四个公共门禁
# 与 affected-spec-ep-019 门禁；依赖经 GOPROXY 下载到模块缓存，不进入源码树。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
E="$1"
mkdir -p "$E/logs"; chmod 700 "$E"
cd "$D"
TREE=$(python3 -c "
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path('$B/gate-tree')))")
{
  echo "# VC-4 四个公共门禁 + affected-spec-ep-019（ARM64 linux/arm64）"
  echo "commit=$C"; echo "candidate_source=$B/source"
  echo "gate_tree=$B/gate-tree（候选 commit 的干净副本：无 vendor、无前端 dist；Go 依赖经 GOPROXY 下载到 GOMODCACHE，不进入源码树）"
  echo "gate_tree_sha256=$TREE"; echo "gate_tree_git_status=[$(git -C $B/gate-tree status --porcelain --untracked-files=all)]"
  utc_now; uname -srm; go version; python3 --version; echo "GOPROXY=$(go env GOPROXY) GOMODCACHE=$(go env GOMODCACHE) GOFLAGS=$(go env GOFLAGS)"
  cd "$B/gate-tree/backend"
  set +e
  echo; echo "## gate affected-spec-ep-019 (working_directory=backend)"; echo '$ go test ./internal/service -run ^TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease$ -count=1'
  go test ./internal/service -run '^TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease$' -count=1 -v 2>&1 | grep -v official_egress_guard; echo "exit_code=${PIPESTATUS[0]}"
  echo; echo "## gate catalog-projection (working_directory=backend)"; echo '$ go test ./internal/service -run ^TestOfficialCodexProjectionUsesFormalReleaseCatalog$ -count=1'
  go test ./internal/service -run '^TestOfficialCodexProjectionUsesFormalReleaseCatalog$' -count=1 -v 2>&1 | grep -v official_egress_guard; echo "exit_code=${PIPESTATUS[0]}"
  echo; echo "## gate official-egress-version-leak-ast (working_directory=backend)"; echo '$ go test ./internal/service -run ^TestOfficialEgressVersionLeakAST$ -count=1'
  go test ./internal/service -run '^TestOfficialEgressVersionLeakAST$' -count=1 -v 2>&1 | grep -v official_egress_guard; echo "exit_code=${PIPESTATUS[0]}"
  cd "$B/gate-tree"
  echo; echo "## gate version-leak (working_directory=.)"; echo '$ python3 tools/check_version_leak.py'
  python3 tools/check_version_leak.py; echo "exit_code=$?"
  echo; echo "## gate version-leak-self-test (working_directory=.)"; echo '$ python3 tools/check_version_leak.py --self-test'
  python3 tools/check_version_leak.py --self-test; echo "exit_code=$?"
  set -e
  echo; echo "## 门禁树洁净度复核"; echo "gate_tree_git_status=[$(git -C $B/gate-tree status --porcelain --untracked-files=all)]"
  cd "$D"
  echo "gate_tree_sha256_after=$(python3 -c "
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path('$B/gate-tree')))")"
  echo "GATES_DONE $(utc_now)"
} > "$E/logs/implementation.log" 2>&1
grep -E "^exit_code=|GATES_DONE|gate_tree_sha256" "$E/logs/implementation.log"
