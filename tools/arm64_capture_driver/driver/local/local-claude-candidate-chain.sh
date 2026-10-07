#!/bin/bash
# 本机候选提交链（Claude；Claude 升级 B3-04a，设计 B3-04「Claude 版候选链驱动」）：只读取回 VC-3 暂存 Release 与门禁需求预览
#   → A（资产：画像与 Wire、投影策略、lifecycle 四个文件）→ C（只有 Claude 的 release-catalog.json）→ D（A→C 的冻结承接收据）。
# 前提：采集主机已跑 vc4-requirements.sh（revision 已登记、$RUNROOT/vc4-gate-requirements.json 已写）；GATE_MAPPING_INPUT 是按
#   这份需求预览逐项绑定的门禁映射；仓库在 BUNDLE_BRANCH、工作树干净，且当前提交的 Claude release-catalog.json 就是 VC-3 冻结的
#   Active 目录（r≥2 的候选也从不含候选目录改动的提交开始，内容寻址文件逐字复用）。
# 采集主机只读：一条 ssh tar 取回 Campaign 总计划、profile 下的 VC-3 产物与需求预览，不在采集主机执行任何 Python。取料按摘要链
#   核验，装配与验收见 claude_catalog_chain.py（与 record-candidate-build 同一组落点常量与验收函数）。
# 用法：ARM64_VC_ENV=<本轮参数> bash local-claude-candidate-chain.sh <ROUND> <STAMP> <工作目录> <DATE_TAG YYYYMMDD> <BASE_COMMIT> <A 段提交说明文件>
# 环境：REPO（默认 ~/Developer/sub2apiplus）；输出 <工作目录>/<ROUND>-commits.txt（C 与 DC）与 <ROUND>.bundle（BASE..分支）。
set -Eeuo pipefail
[ "$#" = 6 ] || { echo '用法：ARM64_VC_ENV=… bash local-claude-candidate-chain.sh <ROUND> <STAMP> <工作目录> <DATE_TAG> <BASE_COMMIT> <A 段提交说明文件>'; exit 2; }
REQUESTED_ROUND="$1"; REQUESTED_STAMP="$2"; SP="$3"; DATE_TAG="$4"; BASE="$5"; A_MESSAGE="$6"
REPO=${REPO:-$HOME/Developer/sub2apiplus}
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${ARM64_VC_ENV:?必须提供本轮参数文件}"
EXPORTS=$(python3 "$DRV/parse_env.py" "$ARM64_VC_ENV") || exit 2
eval "$EXPORTS"; unset EXPORTS
# 本机不加载 lib.sh（不在本机建 RUNROOT）；客户端表同样只赋值、不导出。
CLIENT_TABLE_ASSIGNMENTS=$(python3 -B "$DRV/client_table.py" shell) || exit 2
eval "$CLIENT_TABLE_ASSIGNMENTS"; unset CLIENT_TABLE_ASSIGNMENTS
if [ "$CT_VC4_REQUIREMENTS_SOURCE" != arm64-preview ]; then
  echo "本轮客户端（${CT_CLIENT}）的门禁需求由本机现算，用 local-candidate-chain.sh"; exit 2
fi
test "$ROUND" = "$REQUESTED_ROUND"; test "$STAMP" = "$REQUESTED_STAMP"
: "${GATE_MAPPING_INPUT:?必须提供按本轮门禁需求预览绑定的门禁映射}"
test -f "$A_MESSAGE"; test -f "$GATE_MAPPING_INPUT"
test -z "$(git -C "$REPO" status --porcelain --untracked-files=all)"
test "$(git -C "$REPO" branch --show-current)" = "$BUNDLE_BRANCH"
git -C "$REPO" merge-base --is-ancestor "$BASE" HEAD
TAKE="$SP/$ROUND-claude-take"
mkdir -p "$SP"; rm -rf "$TAKE"; mkdir -m 700 "$TAKE"
# %q 把本轮路径作为远端 shell 的单一参数传入，不拼接可执行文本；tar 的 -C 让需求预览以文件名落在取料根下。
printf -v REMOTE_COMMAND 'cd %q && tar -cf - campaign.json campaign.sha256 profile/result.json profile/prepare/results profile/prepare/approved profile/release profile/stage -C %q vc4-gate-requirements.json' "$NEWDIR" "$RUNROOT"
ssh -n -o ConnectTimeout=20 ARM64 "$REMOTE_COMMAND" > "$SP/$ROUND-claude-take.tar"
tar -xf "$SP/$ROUND-claude-take.tar" -C "$TAKE"
cd "$REPO"
CHAIN=(env PYTHONPATH="$REPO" PYTHONDONTWRITEBYTECODE=1 python3 -B "$DRV/claude_catalog_chain.py")
TAKE_ARGS=(--take "$TAKE" --repository "$REPO" --lifecycle "$LIFECYCLE_DIR" --campaign-id "$NEW" --candidate-id "$CAND" --target-version "$TARGET_VERSION")
"${CHAIN[@]}" assets "${TAKE_ARGS[@]}" --mapping "$GATE_MAPPING_INPUT" > "$SP/$ROUND-claude-assets.json"
# A：只暂存装配器写入的路径与 lifecycle 目录；不能有装配器之外的改动或未跟踪文件。
python3 -c 'import json,sys; print("\0".join(json.load(open(sys.argv[1]))["written"]), end="")' "$SP/$ROUND-claude-assets.json" | xargs -0 git add --
git add -A -- "$LIFECYCLE_DIR"
test -z "$(git status --porcelain --untracked-files=all | grep -v '^[AMD]  ' || true)"
git commit -q -F "$A_MESSAGE"
A=$(git rev-parse HEAD)
test -z "$(git status --porcelain --untracked-files=all)"
# C：只有 release-catalog.json（Go 加载核对与 verify_candidate_catalog 验收不过即还原，脚本停下）。
"${CHAIN[@]}" catalog "${TAKE_ARGS[@]}" > "$SP/$ROUND-claude-catalog.json"
CATALOG=backend/internal/officialegress/catalogdata/claude/release-catalog.json
git add -- "$CATALOG"
test "$(git status --porcelain --untracked-files=all)" = "M  $CATALOG"
git commit -q -m "feat($TARGET_TAG): 切换 Claude validation_candidate 指向 $ROUND 暂存 Release"
C=$(git rev-parse HEAD)
TAG="$TARGET_TAG-candidate-$ROUND-$DATE_TAG"; RECEIPT="docs/egress/maintenance/upstream-$TAG-freeze-successor.json"
PYTHONDONTWRITEBYTECODE=1 python3 -B -m tools.upstream_merge freeze-successor-generate --before "$A" --after "$C" --tag "$TAG" --output "$REPO/$RECEIPT" --reason "按最终候选提交登记冻结路径后继摘要；逐字保留旧收据。"
"${CHAIN[@]}" transition --receipt "$REPO/$RECEIPT" --repository "$REPO" --commit "$C"
git add -- "$RECEIPT"
git commit -q -m "chore($TARGET_TAG): 登记 $ROUND 候选 Claude 发布目录的冻结承接摘要"
DC=$(git rev-parse HEAD)
echo "$C $DC" > "$SP/$ROUND-commits.txt"
echo "A=$A C=$C DC=$DC RECEIPT=$RECEIPT"
git bundle create "$SP/$ROUND.bundle" "$BASE..$BUNDLE_BRANCH"
git log --oneline -4
echo CHAIN_DONE
