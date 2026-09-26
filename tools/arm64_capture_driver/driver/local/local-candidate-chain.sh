#!/bin/bash
# 本机候选提交链：核验本轮 VC-3 Catalog、门禁需求与显式映射 → A（资产）→ C（冻结指针）→ D（承接）。
# 用法：ARM64_VC_ENV=<本轮参数> bash local-candidate-chain.sh <ROUND> <STAMP> <工作目录> <DATE_TAG YYYYMMDD> <BASE_COMMIT> <A 段提交说明文件>
# 环境：REPO（默认 ~/Developer/sub2apiplus）；输出 <工作目录>/<ROUND>-commits.txt 与 <ROUND>.bundle。
set -Eeuo pipefail
REQUESTED_ROUND="$1"; REQUESTED_STAMP="$2"; SP="$3"; DATE_TAG="$4"; BASE="$5"; A_MESSAGE="$6"
REPO=${REPO:-$HOME/Developer/sub2apiplus}
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${ARM64_VC_ENV:?必须提供本轮参数文件}"
EXPORTS=$(python3 "$DRV/parse_env.py" "$ARM64_VC_ENV") || exit 2
eval "$EXPORTS"; unset EXPORTS
# 本机不加载 lib.sh，避免在本机创建 ARM64 的 RUNROOT。
test "$ROUND" = "$REQUESTED_ROUND"; test "$STAMP" = "$REQUESTED_STAMP"
: "${GATE_MAPPING_INPUT:?必须提供本轮已绑定 VC-3 需求的门禁映射}"
test -f "$A_MESSAGE"; test -f "$GATE_MAPPING_INPUT"
test -z "$(git -C "$REPO" status --porcelain)"
test "$(git -C "$REPO" branch --show-current)" = "$BUNDLE_BRANCH"
CAT_NAME="${CAMPAIGN_PREFIX}-vc3-candidate-catalog-$ROUND-$STAMP"
mkdir -p "$SP/$ROUND-catalog"
# %q 将本轮参数作为远端 shell 的单一参数传入，不拼接可执行文本。
printf -v REMOTE_COMMAND 'cd %q && tar -cf - %q' "$D/control" "$CAT_NAME"
ssh -n -o ConnectTimeout=20 ARM64 "$REMOTE_COMMAND" > "$SP/$ROUND-catalog.tar"
tar -xf "$SP/$ROUND-catalog.tar" -C "$SP/$ROUND-catalog"
printf -v REMOTE_COMMAND 'cd %q && PYTHONPATH=. python3 - %q' "$D" "$D/evidence/campaigns/$NEW"
ssh -o ConnectTimeout=20 ARM64 "$REMOTE_COMMAND" > "$SP/$ROUND-gate-requirements.json" <<'PY'
import sys, json
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
campaign=Path(sys.argv[1]); manifest=cu._require_formal_campaign(campaign)
_,_,requirements=cu._load_vc3_gate_requirements(campaign,manifest,cu._load_stage_result(campaign,'classify'))
print(json.dumps(requirements,ensure_ascii=False))
PY
cd "$REPO"
PYTHONPATH="$REPO" python3 "$DRV/catalog_chain.py" "$SP/$ROUND-catalog/$CAT_NAME" "$REPO" "$LIFECYCLE_DIR" "$SP/$ROUND-gate-requirements.json" "$GATE_MAPPING_INPUT"
# A 包括所有新增 blob 与测试快照索引，两份冻结指针只在 C 中提交。
git add backend/internal/officialegress/catalogdata/runtime/profiles/ backend/internal/officialegress/catalogdata/runtime/release-graphs/ backend/internal/officialegress/catalogdata/runtime/snapshot-catalogs/ backend/internal/officialegress/profilecontract/testdata/ "$LIFECYCLE_DIR/"
git commit -q -F "$A_MESSAGE"
git add backend/internal/officialegress/catalogdata/runtime/release-catalog.json backend/internal/officialegress/releasecontract/testdata/release-graph.json
git commit -q -m "feat($TARGET_TAG): 切换候选 RuntimeCatalog 指向 $ROUND 候选 release graph"
A=$(git rev-parse HEAD~1); C=$(git rev-parse HEAD)
TAG="$TARGET_TAG-candidate-$ROUND-$DATE_TAG"; RECEIPT="docs/egress/maintenance/upstream-$TAG-freeze-successor.json"
python3 -m tools.upstream_merge freeze-successor-generate --before "$A" --after "$C" --tag "$TAG" --output "$REPO/$RECEIPT" --reason "按最终候选提交登记冻结路径后继摘要；逐字保留旧收据。"
python3 - "$RECEIPT" "$C" <<'PY'
import json, sys
receipt=json.load(open(sys.argv[1]))
assert receipt['result']=='passed_local_evidence_successor' and receipt['unregistered_path_count']==0 and receipt['current_commit']==sys.argv[2], receipt
print('冻结承接通过', receipt['changed_path_count'])
PY
git add "$RECEIPT"
git commit -q -m "chore($TARGET_TAG): 登记 $ROUND 候选 RuntimeCatalog 的冻结承接摘要"
DC=$(git rev-parse HEAD)
echo "$C $DC" > "$SP/$ROUND-commits.txt"
echo "A=$A C=$C DC=$DC RECEIPT=$RECEIPT"
git bundle create "$SP/$ROUND.bundle" "$BASE..$BUNDLE_BRANCH"
git log --oneline -4
echo CHAIN_DONE
