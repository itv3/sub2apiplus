#!/bin/bash
# 写 VC-2 人工审核标记（只在调度者审核签字之后运行）。流水线的 VC2 阶段要求 $W/VC2-INPUTS-REVIEWED 存在，且 JSON 里的
# rule_migration_sha256／target_snapshot_sha256 与 INPUT_RULE_MIGRATION／INPUT_TARGET_SNAPSHOT 两个文件的 sha256 逐一相等、reviewed_by 非空
#（审核后又改过输入，标记就失效，流水线会 STOPPED）。
# 用法：ARM64_VC_ENV=… bash vc2-mark-reviewed.sh --reviewed-by '<审核人及依据>'
#       ARM64_VC_ENV=… bash vc2-mark-reviewed.sh --show          只看现状，不写
# 写标记前会重新过一遍工具的迁移清单校验与 validate 目标 Snapshot 的基本形状（零请求）。
set -Eeuo pipefail; umask 077
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV}"
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
PIPE_HOME=${PIPE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
EXPORTS=$(python3 -B "${PIPE_DRIVER_DIR}/parse_env.py" "$ARM64_VC_ENV"); eval "$EXPORTS"; unset EXPORTS
MK="$W/VC2-INPUTS-REVIEWED"
case "${1:-}" in
  --show)
    ls -l "$INPUT_RULE_MIGRATION" "$INPUT_TARGET_SNAPSHOT" 2>&1; sha256sum "$INPUT_RULE_MIGRATION" "$INPUT_TARGET_SNAPSHOT" 2>&1
    [ -f "$MK" ] && { echo "标记："; cat "$MK"; } || echo "标记不存在：$MK"; exit 0 ;;
  --reviewed-by) BY=${2:-}; [ -n "$BY" ] || { echo "--reviewed-by 不能为空"; exit 2; } ;;
  *) echo "用法：--reviewed-by '<审核人及依据>' | --show"; exit 2 ;;
esac
test -f "$INPUT_RULE_MIGRATION" && test -f "$INPUT_TARGET_SNAPSHOT" || { echo "两份输入还没就位（先按 README 准备两份输入：rule-migration-approved.json、target-snapshot-approved.json）"; exit 3; }
test ! -e "$MK" || { echo "标记已存在：${MK}（要重写请先由调度者删除，避免无意覆盖）"; exit 3; }
CAMP="$D/evidence/campaigns/$NEW"
( cd "$D" && PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -B - "$INPUT_RULE_MIGRATION" "$INPUT_TARGET_SNAPSHOT" "$RULES_JSON" "$CAMP" "$MK" "$BY" "$BASELINE_VERSION" "$TARGET_VERSION" <<'PY'
import datetime, hashlib, json, sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
rm_path, ts_path, rules_path, camp, mk, by, base, target = sys.argv[1:]
migration = json.load(open(rm_path))
rules = tuple(json.load(open(rules_path))["required_rules"])
campaign = Path(camp)
manifest = cu._require_formal_campaign(campaign)
official = cu._load_stage_result(campaign, "capture-official")
origin_dir, _ = (campaign, manifest) if official.get("predecessor_import") is None else cu._official_evidence_origin(campaign, manifest, official)
sd = json.load(open(campaign / manifest["analysis"]["source-diff"]["path"])); od = json.load(open(origin_dir / official["official_diff"]["path"]))
r = cu._validate_migration_manifest(migration, baseline_version=base, target_version=target, baseline_rules=rules, target_rules=rules, source_diff=sd, official_diff=od)
assert not r["blocked"] and r["unclassified_count"] == 0, r
snap = json.load(open(ts_path))
assert snap.get("Digest") == "" and snap.get("Version") == target, ("目标 Snapshot 的 Digest 应置空、Version 应为目标版本", snap.get("Digest"), snap.get("Version"))
marker = {"rule_migration_sha256": hashlib.sha256(open(rm_path, "rb").read()).hexdigest(),
          "target_snapshot_sha256": hashlib.sha256(open(ts_path, "rb").read()).hexdigest(),
          "reviewed_by": by, "reviewed_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "migration_check": {k: r[k] for k in ("entry_count", "discovery_count", "unclassified_count")}}
Path(mk).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
Path(mk).chmod(0o600)
print("已写审核标记：", mk); print(json.dumps(marker, ensure_ascii=False, indent=2))
PY
)
