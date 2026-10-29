#!/bin/bash
# VC-6 §4.6.3 post-promotion 门禁：在 production tree 上逐字执行 VC-4 批准的门禁计划（项数以计划为准），
# 前后各采一份 ARM64 环境收据；facts v4、finalize 与 replay 由后续步骤完成。
# 只禁写字节码、不设缓存前缀（第 67 项：空前缀会让标准库 .pyc 也读不到）；本计划没有计时敏感的 Python 子进程用例。
set -Eeuo pipefail; umask 077
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1; export PATH=/usr/local/go/bin:$PATH
unset PYTHONPYCACHEPREFIX
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
# 入口坐标是入口编排器的参数，门禁里的测试不该继承（见 lib.sh isolated_run 的说明）；vc6-env.sh 已用完它们。
unset ENTRY_COMMIT ENTRY_BUNDLE ENTRY_BRANCH
N=$NEWDIR
B=$D/candidates/$CAND
P=$B/production-tree
W=$V6
R=$(cat "$W/vc6-root.txt"); ATT=$(cat "$W/vc6-gates-attempt.txt")
mkdir -p "$R/gates/logs" "$R/gates/environment"; chmod 700 "$R" "$R/gates" "$R/gates/logs" "$R/gates/environment"
# 输入副本（acceptance／promotion／gate plan 原字节）——激活收据要在同一证据根内重放门禁收据
# 验收结果取当前评估基线的规范读源（与 vc5-completion.json 的 acceptance_fact 同一份；本轮 approval-revision 后在 b2）。
ACC=$(cd "$D" && PYTHONPATH="$D" PYTHONDONTWRITEBYTECODE=1 python3 -B -c "import sys; from pathlib import Path; from tools.official_client_capture import codex_upgrade as cu; c = Path(sys.argv[1]); b, _ = cu._current_evaluation_baseline(c, sys.argv[2]); print(cu._stage_read_source(c, sys.argv[2], b, 'accept')['path'])" "$N" "$CAND")
test -s "$ACC"; cp -f "$ACC" "$R/acceptance-result.json"
cp -f "$B/production-catalog-$STAMP/catalog-promotion-receipt.json" "$R/catalog-promotion-receipt.json"
cp -f "$B/source/$LIFECYCLE_DIR/gate-plan.json" "$R/gates/gate-plan.json"
chmod 600 "$R/acceptance-result.json" "$R/catalog-promotion-receipt.json" "$R/gates/gate-plan.json"
echo "=== gate_before 环境收据 $(date -u +%H:%M:%SZ)"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$R" --output "gates/environment/$ATT-before-facts.json" --phase gate_before --subject-id "$ATT" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-140
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$R" --facts "gates/environment/$ATT-before-facts.json" --output "gates/environment/$ATT-before.json" | cut -c1-140
echo "=== 逐字执行门禁（production tree）$(date -u +%H:%M:%SZ)"
export CODEX_0_149_1_SOURCE_ROOT=$D/official/historical-gate-source
# 与 VC-4 门禁（driver lib.sh 的 isolated_run）一致，规范源码基线指向本轮目标版本的源码树副本；go test 用例不读它，加上无副作用
export CODEX_SPEC_SOURCE_ROOT=$HISTORICAL_SOURCE_ROOT
python3 - "$R" "$P" "$ATT" <<'PY'
import json, subprocess, socket, datetime, sys
from pathlib import Path
R = Path(sys.argv[1]); P = Path(sys.argv[2]); ATT = sys.argv[3]
plan = json.load(open(R/"gates/gate-plan.json"))
rows = []
def now(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for gate in plan["gates"]:
    gid = gate["gate_id"]; wd = P / gate["working_directory"]; cmd = list(gate["command"])
    start = now()
    proc = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True, errors="replace")
    end = now()
    out = R/"gates/logs"/f"{gid}.stdout.log"; err = R/"gates/logs"/f"{gid}.stderr.log"
    out.write_text(proc.stdout, encoding="utf-8"); err.write_text(proc.stderr, encoding="utf-8")
    out.chmod(0o600); err.chmod(0o600)
    meta = {"gate_id": gid, "test_id": gate["test_id"], "working_directory": gate["working_directory"], "command": cmd,
            "host": socket.gethostname(), "architecture": "linux/arm64", "started_at_utc": start, "completed_at_utc": end,
            "exit_code": proc.returncode, "tree": str(P)}
    gj = R/"gates/logs"/f"{gid}.gate.json"; gj.write_text(json.dumps(meta, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); gj.chmod(0o600)
    rows.append((gid, proc.returncode, start, end))
    print(gid, "exit_code=", proc.returncode, start, "->", end, flush=True)
print("ALL_EXIT_ZERO" if all(r[1] == 0 for r in rows) else "GATE_FAILED", len(rows), "项")
PY
echo "=== gate_after 环境收据 $(date -u +%H:%M:%SZ)"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$R" --output "gates/environment/$ATT-after-facts.json" --phase gate_after --subject-id "$ATT" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-140
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$R" --facts "gates/environment/$ATT-after-facts.json" --output "gates/environment/$ATT-after.json" | cut -c1-140
echo "=== production tree 仍干净"; git -C "$P" status --porcelain --untracked-files=all | grep -v "catalogdata/runtime\|releasecontract/testdata" | head -5 || true
echo "GATES_RUN_DONE $(date -u +%H:%M:%SZ)"
