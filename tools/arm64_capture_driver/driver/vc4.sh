#!/bin/bash
# VC-4（ARM64 端，构建完成后）：实现测试收据 → 批次 plan-candidate-gates（预演+派发）→ 批次 record-candidate-build（预演+派发）。
# 用法：bash vc4.sh <E 证据根>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
E="$1"; C9=${C:0:9}
if [ ! -f "$E/receipt.json" ]; then
  test -f "$E/logs/check-egress-spec.log"; test -f "$E/logs/implementation.log"
fi
TREE=$(python3 -c "
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path('$B/source')))"); echo "TREE=$TREE"
echo "=== 实现测试收据"; if [ -f "$E/receipt.json" ]; then echo "receipt 已存在，跳过 facts/finalize"; else bash "$DRV/vc4-facts.sh" "$E" "$TREE" 2>&1 | tail -n 2; fi; test -f "$E/receipt.json"
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt replay --evidence-root "$E" --receipt receipt.json > "$RUNROOT/vc4-replay.json"; python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("replay:", d.get("kind"), d.get("status"))' "$RUNROOT/vc4-replay.json"
python3 "$DRV/vc4_resume.py" check --evidence-root "$E" --mode full || exit 3
echo "=== 批次 plan-candidate-gates：计划 + 预演 + 派发"
python3 - "$W/action-plan-vc4-plan-gates.json" "$NEW" "$CAND" "$B" "$D" <<'PY'
import json, sys, os
out, NEWID, CAND, B, D = sys.argv[1:]
NEW = f"{D}/evidence/campaigns/{NEWID}"
plan = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["plan-candidate-gates"], "reuse_item_ids": [], "actions": [{"action_id": "plan-candidate-gates", "operation": "VC-4:plan-candidate-gates", "timeout_seconds": 900, "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "plan-candidate-gates", "--campaign-dir", NEW, "--candidate-id", CAND, "--candidate-source", f"{B}/plan-source", "--mapping", f"{B}/plan-source/{os.environ['LIFECYCLE_DIR']}/gate-mapping.json", "--output", f"{B}/plan-source/{os.environ['LIFECYCLE_DIR']}/gate-plan-dispatch.json"], "item_ids": ["plan-candidate-gates"]}]}
json.dump(plan, open(out, "w"), ensure_ascii=False, indent=2); print("plan-candidate-gates 计划 ->", out)
PY
chmod 600 "$W"/*.json
rm -f "$B/plan-source/$LIFECYCLE_DIR/gate-plan-rehearsal.json" "$B/plan-source/$LIFECYCLE_DIR/gate-plan-dispatch.json"
python3 - "$B" "$NEWDIR" "$CAND" > "$RUNROOT/vc4-plan-rehearsal.out" 2>&1 <<'PY'
import argparse, json, sys, os
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
B, NEW, CAND = sys.argv[1:]
args = argparse.Namespace(campaign_dir=Path(NEW), candidate_id=CAND, candidate_source=Path(B)/"plan-source", mapping=Path(B)/"plan-source"/os.environ["LIFECYCLE_DIR"]/"gate-mapping.json", output=Path(B)/"plan-source"/os.environ["LIFECYCLE_DIR"]/"gate-plan-rehearsal.json")
print(json.dumps(cu.plan_candidate_gates(args), ensure_ascii=False))
PY
tail -c 300 "$RUNROOT/vc4-plan-rehearsal.out"; echo
cmp "$B/plan-source/$LIFECYCLE_DIR/gate-plan-rehearsal.json" "$B/source/$LIFECYCLE_DIR/gate-plan.json" && echo "预演 plan 与提交内 gate-plan.json 逐字一致"; rm -f "$B/plan-source/$LIFECYCLE_DIR/gate-plan-rehearsal.json"
SEQ=$(next_seq); echo "批次序号=$SEQ"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-4 "$SEQ" VC-3 action-plan-vc4-plan-gates.json | grep -v "^$"
cmp "$B/plan-source/$LIFECYCLE_DIR/gate-plan-dispatch.json" "$B/source/$LIFECYCLE_DIR/gate-plan.json" && echo "派发产物 plan 与提交内 gate-plan.json 逐字一致"
echo "=== 批次 record-candidate-build：计划 + 预演 + 派发"
IMAGE_ID=$(python3 -c "import json; print(json.load(open('$B/artifacts/build-parameters.json'))['docker_build']['image_id'])")
BUILD_ID="$CAND-$C9-$(python3 -c "import sys; print(sys.argv[1].strip().replace('-','').replace(':','').lower())" "$(cat $B/artifacts/built-at-utc.txt)")"
echo "IMAGE_ID=$IMAGE_ID BUILD_ID=$BUILD_ID"
rm -f "$W/action-plan-vc4-record-build.json"; python3 "$DRV/gen_vc4_record_plan.py" "$IMAGE_ID" "$BUILD_ID" "$E" "$W/action-plan-vc4-record-build.json" "$NEW" "$CAND" "$B" | cut -c1-100; chmod 600 "$W"/*.json
set +e; python3 - "$W/action-plan-vc4-record-build.json" > "$RUNROOT/vc4-record-rehearsal.out" 2>&1 <<'PY'
import json, sys, argparse, shutil
from pathlib import Path
from unittest import mock
from tools.official_client_capture import codex_upgrade as cu
plan = json.load(open(sys.argv[1])); argv = plan["actions"][0]["command"][3:]
parser = argparse.ArgumentParser()
for name in ("--campaign-dir","--candidate-source","--candidate-binary","--build-parameters","--build-tree","--docker-context","--frontend-dist-source","--catalog-stage-dir","--source-transition","--gate-plan","--implementation-test-root","--implementation-test-receipt"):
    parser.add_argument(name, type=Path, required=True)
for name in ("--candidate-id","--candidate-purpose","--runtime-image","--candidate-image-id","--build-id","--deployed-version","--target-architecture"):
    parser.add_argument(name, required=True)
args = parser.parse_args(argv)
# 预演产物放在 Campaign 候选目录下的临时子目录（machine receipts 路径需相对 Campaign 目录），完成后整体删除
rehearsal_dir = args.campaign_dir / "candidates" / args.candidate_id / "rehearsal-tmp"
# v14r 教训（2026-09-21）：改造 2 后 record-candidate-build 还会写 control/vc/revisions/r<N>/seal.json（revision-seal，
# write-once + 内容核对，绑定构建收据摘要）；预演若不隔离它，正式派发必因摘要不一致失败并把候选级阶段推进
# candidate_review_required。预演因此 mock 全部三个 Campaign 写点，并在前后对 Campaign 写域做文件快照比对。
import hashlib, os
def snapshot(root):
    rows = {}
    for sub in ("candidates", "control/vc"):
        base = root / sub
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            for name in filenames:
                fp = Path(dirpath) / name
                rows[str(fp.relative_to(root))] = hashlib.sha256(fp.read_bytes()).hexdigest() if fp.is_file() else "?"
    return rows
before = snapshot(args.campaign_dir)
try:
    with mock.patch.object(cu, "_candidate_build_receipt_path", lambda campaign_dir, candidate_id: rehearsal_dir / "build-receipt.json"), \
         mock.patch.object(cu, "_complete_vc_phase", lambda *a, **k: {"rehearsal": True}), \
         mock.patch.object(cu, "_seal_candidate_revision", lambda *a, **k: {"rehearsal": True}):
        result = cu.record_candidate_build(args)
finally:
    shutil.rmtree(rehearsal_dir, ignore_errors=True)
after = snapshot(args.campaign_dir)
diff = sorted(set(before.items()) ^ set(after.items()))
if diff:
    print("预演在 Campaign 写域留下了变更：", diff[:10])
    sys.exit(3)
print("预演零副作用：Campaign 写域快照前后一致，文件数", len(after))
print("预演结果:", json.dumps({k: result.get(k) for k in ("status","candidate_id","source_tree_sha256","image_reference","live_request_count")}, ensure_ascii=False))
PY
RC=$?; set -e; tail -n 2 "$RUNROOT/vc4-record-rehearsal.out" | cut -c1-400; test ! -e "$NEWDIR/candidates/$CAND/rehearsal-tmp"; test "$RC" = 0; grep -q "\"status\": \"complete\"" "$RUNROOT/vc4-record-rehearsal.out"
SEQ=$(next_seq); echo "批次序号=$SEQ"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-4 "$SEQ" VC-3 action-plan-vc4-record-build.json | grep -v "^$"
python3 -c "
import json
import glob
rev=sorted(glob.glob('$NEWDIR/control/vc/revisions/r*/vc-4-checkpoint.json'))
c=json.load(open(rev[-1] if rev else '$NEWDIR/control/vc/vc-4-checkpoint.json')); print('VC-4 checkpoint:', {k:c.get(k) for k in ('phase','status','completed_at_utc')})
r=json.load(open('$NEWDIR/candidates/$CAND/build-receipt.json')); print('build-receipt:', {k:r.get(k) for k in ('candidate_id','receipt_digest')}, 'image:', r['image']['image_id'], '| source:', r['source']['git_commit'][:9], '| build_id:', r['build']['build_id'])"
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence')})"
echo "IMAGE_ID=$IMAGE_ID BUILD_ID=$BUILD_ID TREE=$TREE E=$E"; echo "VC4_DONE"
