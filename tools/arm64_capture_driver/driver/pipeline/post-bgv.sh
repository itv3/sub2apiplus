#!/bin/bash
# 后台验证结束后的取证摘要（只读）：用法 post-bgv.sh <结论 JSON> <运行目录>
RES="$1"; RUN="$2"
python3 -B - "${RES}" "${RUN}" <<'PY'
import json, os, sys
res, run = sys.argv[1:3]
d = json.load(open(res))
print("结论:", d.get("status"), "|", d.get("reason"), "| 开始", d.get("started_at_utc"), "结束", d.get("completed_at_utc"))
print("筛选:", json.dumps(d.get("gate_scope_resolved"), ensure_ascii=False))
print("核数偏差:", json.dumps(d.get("core_deviation"), ensure_ascii=False))
for key in ("retry", "retry_report", "retry_passed_units", "failed_gates", "reuse_binding", "dryrun"):
    value = d.get(key)
    if value is not None:
        print(key, "=", json.dumps(value, ensure_ascii=False)[:700])
es = json.load(open(os.path.join(run, "executor", "summary.json")))
print("执行器:", es.get("status"), "用时", es.get("elapsed_seconds"), "最大核", es.get("max_cores_in_use"), "失败单元", es.get("failed_units"))
retry = es.get("retry") or {}
print("补跑汇总:", {k: retry.get(k) for k in ("retry_passed", "retry_failed", "retry_denied", "not_retriable")})
for unit, info in (retry.get("units") or {}).items():
    second = info.get("retry_attempt") or {}
    first = info.get("first_attempt") or {}
    print(" -", unit, info.get("status"), info.get("failure_class"), "|", info.get("reason"), "|", second.get("note"),
          "| 首轮用时", first.get("seconds"), "补跑用时", second.get("seconds"))
gates = json.load(open(os.path.join(run, "entry-gates.json")))
print("入口门禁用时:", gates.get("elapsed_seconds"))
PY
echo "--- kind=retry 事件"
grep '"kind": "retry"' "${RUN}/executor/events.jsonl" 2>/dev/null | python3 -B -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    print(e.get('event'), e.get('unit'), e.get('exit_code'), e.get('seconds'), e.get('running'))"
echo "--- 补跑日志头"
for f in "${RUN}"/executor/logs/*.retry.log; do [ -f "$f" ] && { echo "## $f"; head -8 "$f" | cut -c1-240; }; done
echo "--- 账本目录"
ls -la /root/docker/capture-cli/unit-records/retry-ledger 2>/dev/null | head -8
