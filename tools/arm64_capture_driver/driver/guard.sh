#!/bin/bash
# 派发前检查：先复验驱动安装，再检查磁盘、总账和 Campaign；任一步失败自动停止。
#   journal   受监督部署的写前意图日志（control/managed-deploy-*.journal）存在即停止：部署进行中或中断后尚未恢复，生产树
#             可能处在半交换状态，只认已写出的部署收据（Claude 升级 B5a-01；install.py verify 同样拒绝，这里先给出明确原因）
#   driver    以安装目标内的 install.py verify 复验驱动脚本清单：逐文件 SHA／权限／属主与安装收据绑定的部署收据
#             必须是 control/ 下本轮客户端当前最新的部署收据（Codex 是 codex-*-supervisor-enable；工具重新部署后驱动必须重新安装确认）
#   disk      根盘：used ≤ 69% 且 available ≥ 30 GiB（候选就绪门禁策略 root-69-percent-and-30-gib/v1）且 available ≥ 操作员阈值
#             操作员阈值固定最低 40 GiB，参数文件 MIN_FREE_GIB 只能把它设得更高
#             仅显式工程 75% 参数且私有 staging 根完整 fixture_only 总账复验通过时改 used 上限；与日常维护共用判据
#   project   项目总账：blocked=false、root_causes_at_limit=[]；remaining_live_requests 为 null（本项目无固定请求上限）合法，非空且 ≤0 拒绝
#   campaign  （pre-vc4／pre-vc5）以受管工具从 Campaign 清单解析并验证精确绑定的 UpgradeTimingLedger，要求 status=active
#             （pre-vc5）再以受管工具 _current_candidate_revision_record + _replay_vc_checkpoint 完整重放当前 revision 的 VC-4 checkpoint
#   pre-vc4／pre-vc5 先清可再生的 Go build cache 再做磁盘检查
# 按客户端（Claude 升级 B5a-06）：驱动复验绑定的部署收据、出口实时准入的环境收据模块、项目总账的模块与目录、Campaign 检查的
#   编排器与计时账本模块取客户端表（lib.sh 给出 CT_ 变量）；Codex 的取值与改造前逐字相同。
# 用法：bash guard.sh pre-plan | pre-vc4 <campaign_dir> | pre-vc5 <campaign_dir>
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
MODE="${1:-}"; CAMPAIGN="${2:-}"
case "$MODE" in pre-plan|pre-vc4|pre-vc5) ;; *) echo "guard: MODE 只能是 pre-plan|pre-vc4|pre-vc5（收到 '$MODE'）"; exit 2;; esac
if [ "$MODE" != pre-plan ] && [ -z "$CAMPAIGN" ]; then echo "guard: $MODE 需要 <campaign_dir>"; exit 2; fi
if compgen -G "$D/control/managed-deploy-*.journal" > /dev/null; then
  echo "guard/deploy-journal: FAIL — control 下有未完成的受监督部署事务日志，重新执行受监督部署完成恢复后再派发"; exit 1
fi
python3 "$DRV/../install.py" verify --target "$(dirname "$DRV")" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} || { echo "guard/driver: FAIL — 驱动安装复验未通过，停止"; exit 1; }
# 出口准入必须来自持续守护的当前状态；旧环境收据或发布认证不能替代它。
python3 -m "$CT_ENVIRONMENT_RECEIPT_MODULE" egress-check || { echo "guard/egress: FAIL — 指定出口未通过实时准入，停止派发"; exit 1; }
MIN_FREE_GIB=$(python3 -c "
import sys
raw = sys.argv[1]
try:
    value = float(raw)
except ValueError:
    sys.exit('guard: MIN_FREE_GIB 必须是数字')
print(max(40.0, value))" "${MIN_FREE_GIB:-40}")
if [ "$MODE" = pre-vc4 ] || [ "$MODE" = pre-vc5 ]; then
  # 老板 2026-09-22 口径：候选构建后、VC-4 与 VC-5 检查前先清可再生的 Go build cache，再做磁盘检查
  BEFORE=0; if [ -d /root/.cache/go-build ]; then BEFORE=$(du -sm /root/.cache/go-build | cut -f1); fi
  go clean -cache; echo "guard/go-clean-cache: released ${BEFORE} MiB"
fi
python3 -B - "$MIN_FREE_GIB" "$DRV" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[2])
import entry_housekeeping as housekeeping
try:
    disk = housekeeping.disk_status(min_free_gib=float(sys.argv[1]), environment=os.environ)
except housekeeping.HousekeepingError as error:
    sys.exit(f"guard/disk: FAIL — {error}")
print(f"guard/disk: used={disk['used_percent']:.1f}% free={disk['free_gib']:.1f}GiB "
      f"(policy ≤{disk['max_used_percent']:g}% & ≥30GiB; operator ≥{disk['min_free_gib']:g}GiB, floor 40)")
if disk.get("engineering_policy"):
    print("guard/disk-engineering:", disk["engineering_policy"])
if not disk["ok"]:
    sys.exit("guard/disk: FAIL — 根盘不满足派发条件，停止")
PY
# 缺省沿用正式总账路径和 status 命令；只给已验证的显式工程 fixture 取其私有总账。
# 路径、客户端、plan 摘要在同一项目锁内再次按既有 disk_policy 复核，不把裸 ENTRY_ROOT 当成准入证明。
guard_project_status() {
  if [ "${ENGINEERING_ROOT_MAX_USED_PERCENT+x}" != x ]; then
    python3 -m "$CT_PROJECT_LEDGER_MODULE" status --ledger-dir "$D/evidence/campaigns/$CT_PROJECT_LEDGER_DIR_NAME"
    return
  fi
  python3 -B - "$DRV" "$CT_PROJECT_LEDGER_MODULE" <<'PY'
import importlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import entry_housekeeping as housekeeping
project_ledger = importlib.import_module(sys.argv[2])
os.umask(0o077)
try:
    policy = housekeeping.disk_policy(os.environ)
    if policy is None:
        raise housekeeping.HousekeepingError("显式工程策略未通过复验")
    root = Path(policy["ledger_dir"])
    with project_ledger.project_lock(root):
        locked_policy = housekeeping.disk_policy(os.environ)
        if locked_policy != policy:
            raise housekeeping.HousekeepingError("工程总账路径、客户端或 plan 摘要在取锁后漂移")
        # replay_head 原有项目锁可重入；完整重放和缓存规则均沿用原实现。
        head = project_ledger.replay_head(root)
except (housekeeping.HousekeepingError, project_ledger.ProjectLedgerError,
        project_ledger.root_cause.RootCauseError, OSError) as error:
    print(f"guard/project: FAIL — {error}", file=sys.stderr)
    sys.exit(2)
result = {key: value for key, value in head.items() if key not in {"accounted_identity_index", "operations"}}
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
PY
}
guard_project_status | python3 -c "
import importlib, sys, json
project_ledger = importlib.import_module(sys.argv[2])
d = json.loads(sys.stdin.read()); h = d.get('head', d)
remaining = h.get('remaining_live_requests')
# 根因上限按本轮目标版本取（共同底数 + 该版本入账）；变量缺失时退回全局口径，仍失败关闭。
at_limit = project_ledger.root_causes_at_limit_for(h, sys.argv[1] or None)
print('guard/project:', {'sequence': h.get('sequence'), 'blocked': h.get('blocked'), 'target_version': sys.argv[1] or None,
      'root_causes_at_limit': at_limit, 'remaining_live_requests': remaining})
if h.get('blocked'):
    sys.exit('guard/project: FAIL — 总账 blocked，停止')
if at_limit:
    sys.exit('guard/project: FAIL — 本版本有根因达同根因重试上限，停止')
if remaining is not None and (not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or remaining <= 0):
    sys.exit('guard/project: FAIL — 剩余请求预算非空且 ≤0（或非数值），停止')
" "${TARGET_VERSION:-}" "$CT_PROJECT_LEDGER_MODULE"
if [ "$MODE" = pre-vc4 ] || [ "$MODE" = pre-vc5 ]; then
  python3 - "$CAMPAIGN" "$MODE" "$CT_ORCHESTRATOR_MODULE" "$CT_TIMING_LEDGER_MODULE" <<'PY'
import importlib, sys
from pathlib import Path
# 编排器与计时账本模块取客户端表（Codex：codex_upgrade 与 codex_upgrade_timing_ledger，与改造前相同）。
cu = importlib.import_module(sys.argv[3])
timing_ledger = importlib.import_module(sys.argv[4])
campaign_dir = Path(sys.argv[1]).resolve(strict=True); mode = sys.argv[2]
manifest = cu._require_formal_campaign(campaign_dir)
ledger_dir = cu._campaign_timing_ledger_dir(campaign_dir, manifest)   # 从清单解析并校验 ledger_plan_sha256 绑定
summary = timing_ledger.inspect_ledger(ledger_dir)
print("guard/campaign:", {"campaign_id": manifest.get("campaign_id"), "ledger_dir": ledger_dir.name, **{k: summary.get(k) for k in ("status", "active_phase", "head_sequence", "current_revision")}})
if summary.get("status") != "active":
    sys.exit("guard/campaign: FAIL — Campaign 账本非 active，停止")
if mode == "pre-vc5":
    plan = cu._vc_campaign_plan(campaign_dir, manifest)
    revision, record = cu._current_candidate_revision_record(campaign_dir, manifest)
    if revision is None:
        sys.exit("guard/campaign: FAIL — 没有 active 候选 revision，停止")
    path, checkpoint = cu._replay_vc_checkpoint(campaign_dir, plan, "VC-4", revision=revision)
    print("guard/vc4-checkpoint:", {"revision": revision, "candidate_id": record.get("candidate_id") if record else None, "path": str(path.relative_to(campaign_dir)), "status": checkpoint.get("status"), "checkpoint_sha256": str(checkpoint.get("checkpoint_sha256"))[:16]})
PY
fi
echo "GUARD_PASS $MODE $(utc_now)"
