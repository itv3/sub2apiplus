"""第 35 项：修好接着跑一条命令（``driver/fix-and-continue.sh``）的离线测试。

覆盖：

* 驱动清单闭合：新脚本、Python 辅助与参数模板被清单登记，bash 语法可解析；
* 轮次参数文件经 ``parse_env.py`` 同一词法层解析（命令替换、反引号、分号、未知键等一律拒绝且不执行），
  并与驱动参数文件（``$ARM64_VC_ENV``）交叉核对数据根、RUNROOT、Campaign 与候选；
* 对账扫描按监督器同一判据（真实 ``codex_upgrade_supervisor``）：只看链尾（最后一个正常结束的父 run 之后）的
  failed／watchdog-aborted 父 run，run 期间有预约的改走 attempt 对账，目标 attempt 留给下一步，已对账的不重复；
* 脚本级（PATH 垫片 + 数据根受管工具替身 + 真实 git bundle）：步骤顺序、再次运行幂等跳过、失败即停并打印
  下一步、``--from`` 续跑与前序记录核对、账务暂停／环境污染／永久停线停下不越权、阶段截止已过时对账前先延期、
  候选审核时授权后再延期、根因修复只登记一次、wire 闭包变化时演进停下、父 run 对账按提示改走 attempt 对账、
  守护代码差异不符与实测失败即停。
* 第 59 项：reconcile-runs 对账遇"项目总账根因达上限"暂停时，与 reconcile-attempt 旁路走同一条登记路径（同一判定、
  同一命令、同一回归收据生成与校验、同一幂等规则），登记后对该对象重新对账一次；没给材料时停下且提示的 ``--from``
  真能通过 check_from；Campaign 账本 stop_required 不走旁路；前次判定暂停的对象续跑时重新对账（受管对账器先写收据、
  入总账再判定，暂停对象的收据按监督器判据核验是通过的，不能据此当作已对账跳过）。

受管工具替身只在 ``FC_TEST_STUB_DIR`` 下读写；PATH 垫片只替换 setsid（同步执行）、systemctl、id、chown。
git、python3、wait_state.py、parse_env.py 都是真的。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import b4_reconciliation_fixtures
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

SCRIPTS = driver_tests.SCRIPTS
DRIVER_ROOT = driver_tests.DRIVER_ROOT
SCRIPT = SCRIPTS / "fix-and-continue.sh"
HELPER = SCRIPTS / "fix_and_continue.py"
ATTEMPT = "20260927T231538Z-1f51c72cdc7dca99"
BRANCH = "codex/codex-01561-upgrade"
EXIT_FAILED = 1
EXIT_OPERATOR = 4


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _write_json(path: Path, payload: object, mode: int = 0o600) -> Path:
    return _write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 受管工具替身（写入假数据根 tools/official_client_capture/）
# ---------------------------------------------------------------------------

_STUB_COMMON = r'''
"""测试替身：模拟受管工具 CLI 与 fix_and_continue.py 用到的只读函数（只在 FC_TEST_STUB_DIR 下读写）。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STUB = Path(os.environ["FC_TEST_STUB_DIR"])
DATA = STUB.parent
PREVIEW_SHA = "a" * 64
EVOLUTION_SHA = "e" * 64
EXTEND_SHA = "d" * 64
AUTHORIZABLE = {"recovery_required", "stage_review_required", "candidate_review_required"}


def _read(name, default):
    path = STUB / name
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def state():
    return _read("state.json", {})


def save_state(value):
    (STUB / "state.json").write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def scenario():
    return _read("scenario.json", {})


def record(entry):
    with (STUB / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def options(argv, name):
    return [argv[i + 1] for i, item in enumerate(argv) if item == name and i + 1 < len(argv)]


def parse_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stage_expired(st):
    deadline = st.get("stage_deadline")
    return st.get("phase") == "VC-5" and bool(deadline) and parse_utc(deadline) <= datetime.now(timezone.utc)


# 第 59 项：根因达上限暂停的替身口径与受管对账器 _decide／_paused_next_command 同文。
ROOT_CAUSE_REASON = "根因 {causes} 累计失败已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）"
CAMPAIGN_STOP_REASON = "Campaign 账本同根因重试已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）"
ROOT_CAUSE_NEXT = ("登记根因修复证据：本 Campaign 账本已 stop_required 用 campaign-resume preview/apply（绑定修复提交、离线回归与部署收据）；"
                   "只是项目总账根因达上限用 codex_upgrade_project_ledger record-root-cause-repair（code：修复提交／回归收据／部署收据）；"
                   "随后重新执行本对账；批准后从原对账 checkpoint 继续")


def root_cause_pause(st, subject):
    """场景 root_cause_limits {对象: 根因}：该根因在总账还没有修复事件时，对账判"项目总账根因达上限"暂停；
    场景 campaign_stop_required [对象]：Campaign 账本 stop_required，人工 campaign-resume（状态 campaign_resumed）前一直暂停。
    与受管对账器同：暂停只是判定——对账收据与总账入账在判定之前已经写下（调用方先写收据再调本函数）。"""
    sc = scenario()
    reasons = []
    cause = (sc.get("root_cause_limits") or {}).get(subject)
    if subject in (sc.get("campaign_stop_required") or []) and not st.get("campaign_resumed"):
        reasons.append(CAMPAIGN_STOP_REASON)
    if cause is not None and not any(cause in repair["root_cause_ids"] for repair in st.get("repairs", [])):
        reasons.append(ROOT_CAUSE_REASON.format(causes=[cause]))
    if not reasons:
        return None
    return {"status": "paused", "root_cause": {"root_cause_id": cause or "rc1-fixture-campaign"},
            "decision": {"decision": "paused", "pause_kinds": ["root_cause_repair"], "reasons": reasons},
            "next_command": ROOT_CAUSE_NEXT}


def campaign_dir():
    return DATA / "evidence" / "campaigns" / scenario()["campaign"]


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    path.chmod(0o600)


def emit(stdout=None, rc=0, stderr=""):
    if stdout is not None:
        print(json.dumps(stdout, ensure_ascii=False, indent=2, sort_keys=True))
    if stderr:
        print(stderr, file=sys.stderr)
    return rc


def _key(module, argv):
    command = argv[0] if argv else ""
    if module == "codex_upgrade":
        if command == "tool-evolution":
            return "tool-evolution:apply" if "--approve-sha256" in argv else "tool-evolution:preview"
        if command == "reconcile-attempt":
            if "--authorize-recovery-preview" in argv:
                return "reconcile-attempt:authorize"
            if "--approve-recovery-sha256" in argv:
                return "reconcile-attempt:approve"
            return "reconcile-attempt"
        if command == "deadline-extend":
            return "deadline-extend:" + (argv[1] if len(argv) > 1 else "")
    return command


def _override(key):
    responses = (scenario().get("responses") or {}).get(key)
    if not responses:
        return None
    st = state()
    counts = st.setdefault("counts", {})
    index = counts.get(key, 0)
    counts[key] = index + 1
    save_state(st)
    if index >= len(responses):
        return None
    return responses[index]


def _attempt(st, attempt_id, *, approve=None):
    if stage_expired(st):
        if approve is not None:
            return emit(None, 1, "升级审计失败：预算暂停期间不接受恢复批准；必须先批准延期")
        return emit({"status": "paused", "attempt_id": attempt_id,
                     "decision": {"decision": "paused", "pause_kinds": ["deadline"], "reasons": ["Campaign 计时预算已暂停"]},
                     "next_command": "deadline-extend preview/apply；批准后从原对账 checkpoint 继续"}, 3)
    directory = campaign_dir() / "control" / "reconciliation" / f"attempt-{attempt_id}"
    write_json(directory / "attempt-reconciliation.json", {"attempt_id": attempt_id})
    # 与受管对账器同：对账收据先于判定写下，根因达上限暂停时收据也已存在（不生成恢复预览）。
    paused = root_cause_pause(st, attempt_id)
    if paused is not None:
        return emit({**paused, "attempt_id": attempt_id}, 3)
    preview = directory / "recovery-preview-01.json"
    # 与真实预览同形：冻结生成时的 Campaign 计时账本 head（授权消费时核对 head 未推进）。
    write_json(preview, {"review_sha256": PREVIEW_SHA, "source_attempt_id": attempt_id,
                         "campaign_ledger_head": {"sequence": st.get("ledger_seq", 10), "status": st.get("status_before_pause")}})
    payload = {"status": "recoverable", "attempt_id": attempt_id, "root_cause": {"root_cause_id": "rc1-fixture"},
               "recovery_preview": {"index": 1, "review_sha256": PREVIEW_SHA, "execute_job_ids": ["job-1"],
                                    "reuse_job_ids": ["job-2"], "live_request_count": 0},
               "recovery_preview_path": str(preview), "resume_reuse_check": {"status": "consistent"},
               "next_command": f"reconcile-attempt --approve-recovery-sha256 {PREVIEW_SHA} 后 resume --rerun-failed --recovery-preview <preview path>"}
    if approve is not None:
        if approve != PREVIEW_SHA:
            return emit(None, 1, "升级审计失败：批准摘要与任何恢复预览都不一致")
        write_json(directory / "recovery-approval-01.json", {"approved_sha256": approve})
        payload["recovery_approval"] = {"index": 1, "approved_sha256": approve, "preview_index": 1}
        payload["next_command"] = f"resume --rerun-failed --recovery-preview {preview}"
    return emit(payload, 0)


def cli(module, argv):
    argv = list(argv)
    record({"module": module, "argv": argv})
    key = _key(module, argv)
    override = _override(key)
    if override is not None:
        return emit(override.get("stdout"), int(override.get("rc", 0)), override.get("stderr", ""))
    st = state()
    sc = scenario()
    if module == "codex_upgrade" and key == "tool-evolution-status":
        registered = bool(st.get("evolution_registered"))
        return emit({"status": "registered" if registered else "evolution_required",
                     "effective_index": 11 if registered else 10,
                     "unregistered_drift": [] if registered else ["control:tools/official_client_capture/codex_upgrade_supervisor.py"],
                     "live_request_count": 0})
    if module == "codex_upgrade" and key == "tool-evolution:preview":
        return emit({"status": "approval_required", "index": 11, "review_sha256": EVOLUTION_SHA,
                     "changes": {"impact_paths": [], "evaluation_paths": [], "unmapped_paths": [],
                                 "wire_closure_changed": bool(sc.get("wire_changed")), "evidence_closure_changed": False},
                     "impact": {"official": {"affected_job_ids": [], "sealed": True},
                                "candidates": {sc["candidate"]: {"affected_job_ids": [], "sealed": False}},
                                "inactive_candidates": []},
                     "evaluator": {"changed_fields": [], "candidates": {}},
                     "bindings": {"fix_commit": option(argv, "--fix-commit")}, "live_request_count": 0})
    if module == "codex_upgrade" and key == "tool-evolution:apply":
        if option(argv, "--approve-sha256") != EVOLUTION_SHA or not option(argv, "--approved-by"):
            return emit(None, 1, "升级审计失败：批准摘要与重算的工具演进预览不一致；重新预览后再批准。")
        st["evolution_registered"] = True
        save_state(st)
        return emit({"status": "evolution_applied", "path": str(campaign_dir() / "control" / "evolution-11.json"),
                     "receipt_sha256": "f" * 64, "index": 11})
    if module == "codex_upgrade" and key == "reconcile-supervisor-run":
        run_dir = Path(option(argv, "--run-dir"))
        redirect = (sc.get("redirect_runs") or {}).get(run_dir.name)
        if redirect:
            return emit(None, 1, "升级审计失败：该 run 期间已产生 reservation，属于 attempt 中断；请改用 reconcile-attempt"
                                 "（恢复段加 --recovery-revision）：" + "、".join(redirect))
        # 与受管 reconcile_supervisor_run 同：收据写入与总账入账在判定之前（暂停时收据也已存在，扫描会按"已对账"核验通过）。
        write_json(campaign_dir() / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json",
                   {"run_id": run_dir.name})
        paused = root_cause_pause(st, run_dir.name)
        if paused is not None:
            return emit({**paused, "run_id": run_dir.name}, 3)
        return emit({"status": "recoverable", "run_id": run_dir.name,
                     "next_command": "phase 保持 active：以 compile-and-run-vc-batch 重新派发同一批次"})
    if module == "codex_upgrade" and key == "reconcile-attempt":
        return _attempt(st, option(argv, "--attempt-id"))
    if module == "codex_upgrade" and key == "reconcile-attempt:approve":
        return _attempt(st, option(argv, "--attempt-id"), approve=option(argv, "--approve-recovery-sha256"))
    if module == "codex_upgrade" and key == "reconcile-attempt:authorize":
        frozen = json.loads(Path(option(argv, "--authorize-recovery-preview")).read_text(encoding="utf-8"))["campaign_ledger_head"]
        if (frozen["status"] in AUTHORIZABLE and st.get("status_before_pause") in AUTHORIZABLE
                and frozen["sequence"] != st.get("ledger_seq", 10)):
            # 与 reconciler.load_approved_recovery_preview 同一拒绝：批准后账本又追加了事件（如延期）。
            return emit(None, 1, "升级审计失败：恢复批准消费前 Campaign 账本 head 已推进，必须重新对账")
        st["authorized"] = True
        st["status_before_pause"] = "active"
        st["phase"] = "VC-5"
        save_state(st)
        preview = option(argv, "--authorize-recovery-preview")
        return emit({"status": "authorized", "attempt_id": option(argv, "--attempt-id"), "preview_path": preview,
                     "review_sha256": PREVIEW_SHA, "live_request_count": 0,
                     "timing_recovery_event": {"event_id": "recovery-authorized-01", "appended": True},
                     "next_command": f"resume --rerun-failed --recovery-preview {preview}"})
    if module == "codex_upgrade" and key == "deadline-extend:preview":
        if st.get("phase") != option(argv, "--phase"):
            return emit(None, 1, "升级审计失败：延期层级非法或阶段不是当前阶段")
        preview = campaign_dir() / "control" / "deadlines" / f"preview-{EXTEND_SHA}.json"
        write_json(preview, {"new_deadline_at_utc": option(argv, "--new-deadline-at-utc")})
        return emit({"status": "preview", "preview_path": str(preview), "review_sha256": EXTEND_SHA,
                     "new_deadline_at_utc": option(argv, "--new-deadline-at-utc"),
                     "scope": option(argv, "--scope"), "phase": option(argv, "--phase")})
    if module == "codex_upgrade" and key == "deadline-extend:apply":
        if option(argv, "--approve-sha256") != EXTEND_SHA or not option(argv, "--approved-by"):
            return emit(None, 1, "升级审计失败：延期批准摘要、Campaign、项目或批准人不一致")
        preview = json.loads(Path(option(argv, "--preview")).read_text(encoding="utf-8"))
        st["stage_deadline"] = preview["new_deadline_at_utc"]
        # 延期写入 Campaign 计时账本 deadline_extended 事件：账本 head 推进。
        st["ledger_seq"] = st.get("ledger_seq", 10) + 1
        save_state(st)
        return emit({"status": "extended",
                     "receipt_path": str(campaign_dir() / "control" / "deadlines" / f"extension-{EXTEND_SHA}.json")})
    if module == "codex_upgrade_project_ledger" and key == "record-root-cause-repair":
        bindings = dict(item.split("=", 1) for item in options(argv, "--binding"))
        causes = options(argv, "--root-cause-id")
        if sc.get("repair_cli_fail"):
            # 与受管 CLI 同：失败只在 stderr 给原因、退出码 2、零写入。
            return emit(None, 2, f"项目总账失败：根因 {causes} 未在总账出现过，无从修复")
        if not sc.get("repair_not_in_ledger"):
            # repair_not_in_ledger：CLI 报成功但总账里没有修复事件（驱动必须按总账复核失败关闭）。
            st.setdefault("repairs", []).append({"root_cause_ids": causes, "bindings": bindings, "note": option(argv, "--note")})
            save_state(st)
        return emit({"operation_id": f"repair:{causes[0]}:0000", "root_cause_ids": causes,
                     "receipt_sha256": "c" * 64, "head_sequence": 294})
    return emit(None, 99, f"替身不认识的调用：{module} {argv}")
'''

_STUB_MODULES = {
    "__init__.py": "",
    "_stub.py": _STUB_COMMON,
    "codex_upgrade.py": textwrap.dedent('''
        """测试替身：codex_upgrade（CLI 与 _tool_identity）。"""
        from tools.official_client_capture import _stub


        def _tool_identity(include_git=True):
            closure = _stub.scenario().get("wire_closure", "b" * 64)
            return {"orchestrator_closures": {"wire_producer": {"closure_sha256": closure}}}


        if __name__ == "__main__":
            import sys
            raise SystemExit(_stub.cli("codex_upgrade", sys.argv[1:]))
    '''),
    "codex_upgrade_project_ledger.py": textwrap.dedent('''
        """测试替身：项目总账（CLI record-root-cause-repair 与只读事件）。"""
        from tools.official_client_capture import _stub


        class ProjectLedgerError(RuntimeError):
            pass


        def find_project_ledger(start):
            return _stub.DATA / "evidence" / "campaigns" / "upgrade-project-ledger"


        def _load_events(root):
            events = []
            for index, repair in enumerate(_stub.state().get("repairs", []), 1):
                causes = repair["root_cause_ids"]
                payload = {"kind": "code", "bindings": repair["bindings"]}
                if len(causes) == 1:
                    payload["root_cause_id"] = causes[0]
                else:
                    payload["root_cause_ids"] = causes
                events.append({"sequence": index, "event_type": "root_cause_repaired", "operation_id": f"repair:{index}",
                               "payload": payload})
            return events


        if __name__ == "__main__":
            import sys
            raise SystemExit(_stub.cli("codex_upgrade_project_ledger", sys.argv[1:]))
    '''),
    "codex_upgrade_supervisor.py": textwrap.dedent('''
        """测试替身：监督器只读判据（状态读取、预约窗口、对账收据核验）。"""
        import json
        from pathlib import Path

        from tools.official_client_capture import _stub

        ACTIVE_STATES = frozenset({"running", "prepared"})
        TERMINAL_STATES = frozenset({"stopped", "failed", "audit-incomplete", "watchdog-aborted", "aborted_prepared"})


        class SupervisorError(RuntimeError):
            pass


        def _read_state(run_dir):
            return json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))


        def _reservations_in_run_window(campaign_dir, started_at_epoch):
            found = []
            for item in _stub.scenario().get("reservations", []):
                if float(item["started_at_epoch"]) >= float(started_at_epoch):
                    root = Path(campaign_dir) / "candidates" / item["candidate_id"] / "attempts" / item["subject"]
                    found.append((item["candidate_id"], item["subject"], root))
            return found


        def verify_attempt_reconciliation_binding(campaign_dir, *, campaign_id, candidate_id, attempt_root, label):
            path = Path(campaign_dir) / "control" / "reconciliation" / f"attempt-{attempt_root.name}" / "attempt-reconciliation.json"
            if not path.is_file():
                raise SupervisorError(f"{label}：attempt {attempt_root.name} 尚未对账")
            return {"attempt_id": attempt_root.name}


        def verify_supervisor_run_reconciliation_binding(campaign_dir, *, campaign_id, run_id, phase, batch_sequence,
                                                         batch_sha256, label):
            path = Path(campaign_dir) / "control" / "reconciliation" / f"run-{run_id}" / "supervisor-run-reconciliation.json"
            if not path.is_file():
                raise SupervisorError(f"{label}：失败父 run {run_id} 尚未对账")
            return {"run_id": run_id}
    '''),
    "codex_upgrade_reconciler.py": textwrap.dedent('''
        """测试替身：对账器只读入口 load_approved_recovery_preview。"""
        from tools.official_client_capture import _stub


        class ReconcilerError(RuntimeError):
            pass


        def load_approved_recovery_preview(campaign_dir, preview_path, *, phase, candidate_id, recovery_revision=None):
            _stub.record({"module": "codex_upgrade_reconciler",
                          "argv": ["load_approved_recovery_preview", str(preview_path), phase, candidate_id]})
            if not preview_path.is_file():
                raise ReconcilerError("恢复预览不存在或不可信")
            authorized = bool(_stub.state().get("authorized"))
            return {"execute_job_ids": ["job-1"], "reuse_job_ids": ["job-2"], "preview_path": str(preview_path),
                    "timing_recovery_event": None if authorized else {"event_id": "x", "appended": True}}
    '''),
    "codex_upgrade_vc_artifacts.py": textwrap.dedent('''
        """测试替身：三层有效截止（只读）。"""
        from tools.official_client_capture import _stub


        def effective_deadlines(campaign_dir, **kwargs):
            st = _stub.state()
            return {"phase": st.get("phase"), "stage_deadline_at_utc": st.get("stage_deadline"),
                    "status_before_pause": st.get("status_before_pause"),
                    "paused_scopes": ["stage"] if _stub.stage_expired(st) else []}
    '''),
}

_STUB_DEPLOY = '''#!/usr/bin/env python3
"""测试替身：受监督部署（写一份部署收据）。"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SUPERVISOR_DIGEST = (
    "{digest}"
)


def main(argv):
    args = dict(zip(argv[::2], argv[1::2]))
    stub = Path(os.environ["FC_TEST_STUB_DIR"])
    with (stub / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"module": "arm64_supervised_deploy", "argv": argv}}) + "\\n")
    if (stub / "deploy-fail").exists():
        print("部署失败（替身）", file=sys.stderr)
        return 1
    control = Path(args["--control-root"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%fz")
    receipt = json.loads((stub / "deploy-receipt.json").read_text(encoding="utf-8"))
    receipt["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    path = control / f"codex-01570-supervisor-enable-{{stamp}}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps({{"status": "passed", "receipt": str(path)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

_STUB_INSTALL = '''#!/usr/bin/env python3
"""测试替身：驱动安装与复验（安装收据绑定最新部署收据）。"""
import json
import os
import shutil
import sys
from pathlib import Path


def latest(control):
    names = sorted(path.name for path in control.glob("codex-*-supervisor-enable-*.json"))
    return names[-1] if names else None


def main(argv):
    command, rest = argv[0], argv[1:]
    args = dict(zip(rest[::2], rest[1::2]))
    stub = Path(os.environ["FC_TEST_STUB_DIR"])
    with (stub / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"module": "install.py", "argv": argv}) + "\\n")
    data = Path(args["--data-root"])
    target = Path(args["--target"])
    marker = stub / "driver-installed.json"
    if command == "install":
        shutil.copytree(args["--source"], target, dirs_exist_ok=True)
        marker.write_text(json.dumps({"deploy": latest(data / "control")}), encoding="utf-8")
        print(json.dumps({"status": "installed"}))
        return 0
    if command == "verify":
        ok = (marker.is_file() and json.loads(marker.read_text(encoding="utf-8"))["deploy"] == latest(data / "control")
              and (target / "manifest.json").is_file())
        print(json.dumps({"status": "verified" if ok else "stale"}))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

_STUB_VC5_RECOVER = '''#!/bin/bash
# 测试替身：只记录调用与环境
python3 -c 'import json,os,sys; open(os.environ["FC_TEST_STUB_DIR"] + "/calls.jsonl", "a").write(json.dumps({"module": "vc5-recover.sh", "argv": sys.argv[1:], "ARM64_VC_ENV": os.environ.get("ARM64_VC_ENV"), "VC_STATE_DIR": os.environ.get("VC_STATE_DIR")}) + "\\n")' "$@"
echo "VC5_RECOVER_STUB $*"
'''

_ITEM_TEST_MODULE = '''import os
import unittest


class ItemTests(unittest.TestCase):
    def test_item(self):
        if os.environ.get("FC_ITEM_DIRTY") == "1":
            with open("stray-output.txt", "w", encoding="utf-8") as handle:
                handle.write("实测往 staging 树里写了文件")
        self.assertNotEqual(os.environ.get("FC_ITEM_FAIL"), "1", "替身实测：按场景失败")

    def test_accounting_pause_then_resolve_continues(self):
        self.assertTrue(True)
'''


class _Round:
    """一轮"修好接着跑"的完整离线夹具：数据根（受管工具替身）、驱动参数、监督器状态目录、git bundle、期望文件。"""

    def __init__(self, root: Path, *, runs: list[tuple[str, str, float]] | None = None, scenario: dict | None = None,
                 state: dict | None = None, params: dict[str, str] | None = None) -> None:
        self.root = root
        self.fixture = driver_tests._DriverFixture(root)
        self.data = self.fixture.data_root
        self.runroot = self.fixture.runroot
        self.campaign = self.fixture.new
        self.candidate = self.fixture.cand
        self.campaign_dir = self.fixture.newdir
        self.stub = self.data / ".stub"
        self.stub.mkdir(mode=0o700)
        self.calls_path = self.stub / "calls.jsonl"
        self.calls_path.touch()
        self.state_dir = self.data / "control" / f"{self.campaign}-supervisor"
        self.state_dir.mkdir(mode=0o700)
        self.driver_target = root / "arm64-capture-driver"
        self.out = self.runroot / "fix-and-continue" / "r99"
        self._install_stub_package()
        self._install_shims()
        self._build_repo()
        self._guard_fixture()
        self._write_expect()
        for name, status, started in runs if runs is not None else [("run-a", "stopped", 100.0), ("run-b", "failed", 200.0)]:
            self.add_run(name, status, started)
        base_scenario = {"campaign": self.campaign, "candidate": self.candidate, "reservations": [], "wire_closure": "b" * 64}
        base_scenario.update(scenario or {})
        _write_json(self.stub / "scenario.json", base_scenario)
        base_state = {"evolution_registered": False, "stage_deadline": "2099-01-01T00:00:00Z", "phase": "VC-5",
                      "status_before_pause": "recovery_required", "authorized": False, "repairs": []}
        base_state.update(state or {})
        _write_json(self.stub / "state.json", base_state)
        self.params_path = self.root / "upload" / "r99-params.env"
        self.write_params(params or {})

    # --- 夹具构造 ---------------------------------------------------------------

    def _install_stub_package(self) -> None:
        tools = self.data / "tools"
        _write(tools / "__init__.py", "")
        for name, source in _STUB_MODULES.items():
            _write(tools / "official_client_capture" / name, source)
        self.supervisor_sha = _sha256(tools / "official_client_capture" / "codex_upgrade_supervisor.py")

    def _install_shims(self) -> None:
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        shims = {
            "setsid": '#!/bin/bash\nif [ "${1:-}" = "-f" ]; then shift; fi\n"$@" || true\nexit 0\n',
            "id": '#!/bin/bash\nif [ "${1:-}" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n',
            "chown": '#!/bin/bash\necho "chown $*" >> "$FC_TEST_STUB_DIR/chown.log"\nexit 0\n',
            "systemctl": f'#!/bin/bash\nif [ "${{1:-}}" = cat ]; then cat "{self.root}/guard/unit.service"; exit 0; fi\nexit 1\n',
        }
        for name, body in shims.items():
            _write(self.bin / name, body, 0o700)

    def _build_repo(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        _git("init", "-q", "-b", BRANCH, str(repo))
        self.deploy_script_new = _STUB_DEPLOY.format(digest=self.supervisor_sha)
        files = {
            ".gitignore": "docs/repository-docs/\n",
            "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md": "# 框架\n",
            "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md": "# 指南\n",
            "tools/__init__.py": "",
            "tools/arm64_supervised_deploy.py": self.deploy_script_new,
            "tools/official_client_capture/__init__.py": "",
            "tools/official_client_capture/tests/__init__.py": "",
            "tools/official_client_capture/tests/test_fc_item.py": _ITEM_TEST_MODULE,
            "tools/arm64_capture_driver/install.py": _STUB_INSTALL,
            "tools/arm64_capture_driver/manifest.json": json.dumps({"manifest_sha256": "9" * 64}) + "\n",
            "tools/arm64_capture_driver/driver/vc5-recover.sh": _STUB_VC5_RECOVER,
        }
        for relative, text in files.items():
            _write(repo / relative, text, 0o755 if relative.endswith((".py", ".sh")) else 0o644)
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "fixture", cwd=repo)
        self.head = _git("rev-parse", "HEAD", cwd=repo)
        upload = self.root / "upload"
        upload.mkdir(exist_ok=True)
        self.bundle = upload / "deploy.bundle"
        _git("bundle", "create", str(self.bundle), BRANCH, cwd=repo)
        self.src = self.data / "staging" / "src"
        _git("init", "-q", str(self.src))
        self.staging_tree = self.data / "staging" / "managed-tools"
        # 采集主机上已安装着上一轮的驱动：安装收据绑定的是旧部署，本轮部署后 verify 必然失败、需要重装。
        shutil.copytree(repo / "tools" / "arm64_capture_driver", self.driver_target)
        _write_json(self.driver_target / "manifest.json", {"manifest_sha256": "8" * 64})

    def _guard_fixture(self) -> None:
        old_digest = "0" * 64
        old_script = _STUB_DEPLOY.format(digest=old_digest)
        guard = self.root / "guard"
        self.guard_script = _write(guard / "arm64_supervised_deploy.py", old_script)
        self.guard_baseline = _write(self.runroot / "stale-tools-backup" / "arm64_supervised_deploy.py.pre-r8", old_script)
        _write(self.data / "tools" / "arm64_supervised_deploy.py", old_script, 0o700)
        _write(guard / "unit.service", f"# /etc/systemd/system/sub2api-egress-guard.service\n[Service]\n"
                                       f"ExecStart=/usr/bin/python3 {self.guard_script} egress-guard --policy /etc/x\n")
        self.guard_status = _write_json(guard / "status.json", {
            "shared_protection": {"status": "compliant"},
            "services": {"sub2apiplus": {"status": "compliant", "admission_state": "ready"},
                         "capture-cli": {"status": "compliant", "admission_state": "ready"}},
        })
        self.guard_removed = [f'"{old_digest}"']
        self.guard_added = [f'"{self.supervisor_sha}"']

    def _write_expect(self) -> None:
        self.receipt_fields = {
            "status": "passed", "architecture": "aarch64", "tool_files_sha256": "1" * 64, "policy_version": 7,
            "policy_sha256": "2" * 64, "wire_producer_sha256": "3" * 64, "evidence_semantics_sha256": "4" * 64,
            "control_sha256": "5" * 64, "supervisor_sha256": self.supervisor_sha,
        }
        _write_json(self.stub / "deploy-receipt.json", {"schema_version": "codex-arm64-supervisor-enable/v1", **self.receipt_fields})
        self.expect_path = self.root / "upload" / "expect.json"
        self.write_expect()
        self.entry_path = _write_json(self.root / "upload" / "entry.json", {
            "schema_version": "arm64-fix-and-continue-entry-assertions/v1",
            "assertions": [
                {"path": "tools/official_client_capture/codex_upgrade_supervisor.py", "regex": "^ACTIVE_STATES = "},
                {"path": "tools/official_client_capture/codex_upgrade.py", "fixed": "def _tool_identity("},
                {"exists": "tools/official_client_capture/codex_upgrade_reconciler.py"},
                {"path": str(self.driver_target / "driver" / "vc5-recover.sh"), "fixed": "VC5_RECOVER_STUB"},
            ],
        })

    def write_expect(self, **guard_overrides: object) -> None:
        guard = {"service": "sub2api-egress-guard.service", "baseline_file": str(self.guard_baseline),
                 "diff_removed": self.guard_removed, "diff_added": self.guard_added, "status_path": str(self.guard_status)}
        guard.update(guard_overrides)
        _write_json(self.expect_path, {
            "schema_version": "arm64-fix-and-continue-expect/v1",
            "deploy_receipt": self.receipt_fields,
            "wire_closure_sha256": "b" * 64,
            "guard": guard,
        })

    def write_params(self, overrides: dict[str, str]) -> None:
        values = {
            "ROUND": "r99",
            "D": str(self.data),
            "RUNROOT": str(self.runroot),
            "VC_ENV": "$RUNROOT/env.sh",
            "VC_STATE_DIR": str(self.state_dir),
            "CAMPAIGN": self.campaign,
            "ATTEMPT": ATTEMPT,
            "CANDIDATE": self.candidate,
            "SRC": str(self.src),
            "STAGING_TREE": str(self.staging_tree),
            "BUNDLE": str(self.bundle),
            "BUNDLE_BRANCH": BRANCH,
            "HEAD_COMMIT": self.head,
            "FIX_COMMIT": self.head,
            "EXPECT": str(self.expect_path),
            "ENTRY_GREPS": str(self.entry_path),
            "ITEM_TESTS": "tools.official_client_capture.tests.test_fc_item",
            "ITEM_TESTS_K": "accounting_pause",
            "ITEM_TESTS_K_MODULES": "tools.official_client_capture.tests.test_fc_item",
            "EVOLUTION_REASON": "修好接着跑：测试轮次（全角括号示例）；受管树只有 control 层变化",
            "APPROVER": "测试批准人（老板授权“修好接着跑”）",
            "DRIVER_TARGET": str(self.driver_target),
        }
        values.update(overrides)
        lines = ["# 测试轮次参数（第 35 项）"]
        for key, value in values.items():
            if value is None:
                continue
            lines.append(f'{key}="{value}"')
        _write(self.params_path, "\n".join(lines) + "\n")

    def add_run(self, name: str, status: str, started: float, *, campaign_id: str | None = None) -> Path:
        run = self.state_dir / name
        run.mkdir(mode=0o700)
        _write_json(run / "state.json", {"campaign_id": campaign_id or self.campaign, "state": status, "phase": "VC-5",
                                         "started_at_epoch": started, "owner_pid": 4242, "owner_nonce": name})
        return run

    def update_scenario(self, **changes: object) -> None:
        path = self.stub / "scenario.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(changes)
        _write_json(path, payload)

    # --- 运行与观察 ---------------------------------------------------------------

    def run(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "FC_TEST_STUB_DIR": str(self.stub),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", "/tmp/pyc-agent-item35"),
        }
        merged.pop("PYTHONPATH", None)
        merged.pop("FC_ITEM_FAIL", None)
        merged.pop("FC_ITEM_DIRTY", None)
        merged.update(env or {})
        return subprocess.run(["bash", str(SCRIPT), str(self.params_path), *extra], capture_output=True, text=True,
                              errors="replace", env=merged, cwd=str(self.root), timeout=600)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def call_keys(self) -> list[str]:
        keys: list[str] = []
        for call in self.calls():
            module, argv = call["module"], call["argv"]
            if module == "codex_upgrade":
                command = argv[0]
                if command == "tool-evolution":
                    keys.append("tool-evolution:apply" if "--approve-sha256" in argv else "tool-evolution:preview")
                elif command == "reconcile-attempt":
                    attempt = argv[argv.index("--attempt-id") + 1]
                    suffix = ("authorize" if "--authorize-recovery-preview" in argv
                              else "approve" if "--approve-recovery-sha256" in argv else "plain")
                    keys.append(f"reconcile-attempt:{suffix}:{attempt}")
                elif command == "deadline-extend":
                    keys.append(f"deadline-extend:{argv[1]}")
                elif command == "reconcile-supervisor-run":
                    keys.append(f"reconcile-supervisor-run:{Path(argv[argv.index('--run-dir') + 1]).name}")
                else:
                    keys.append(command)
            elif module == "codex_upgrade_project_ledger":
                keys.append(argv[0])
            elif module == "install.py":
                keys.append(f"install.py:{argv[0]}")
            else:
                keys.append(module if module != "codex_upgrade_reconciler" else "load_approved_recovery_preview")
        return keys

    def step(self, name: str) -> dict:
        return json.loads((self.out / f"{name}.json").read_text(encoding="utf-8"))

    def stub_state(self) -> dict:
        return json.loads((self.stub / "state.json").read_text(encoding="utf-8"))

    def set_stub_state(self, **changes: object) -> None:
        """模拟编排之外的人工操作（如人工 campaign-resume）改变受管现场。"""

        payload = self.stub_state()
        payload.update(changes)
        _write_json(self.stub / "state.json", payload)

    def check_from(self, step: str) -> subprocess.CompletedProcess[str]:
        """与 fix-and-continue.sh 续跑前同一核对（check-from 子命令）。"""

        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", "/tmp/pyc-agent-item35")}
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, str(HELPER), "check-from", "--params", str(self.params_path), "--step", step],
                              capture_output=True, text=True, env=env)


def load_helper():
    return driver_tests.load_script("fix_and_continue")


def _repair_params(root: Path, *, causes: str = "rc1-fixture", fix: str = "4" * 40,
                   draft: dict | None = None) -> tuple[dict[str, str], Path]:
    """根因修复登记材料（与 repair 步骤用例同形的回归收据草稿）：返回 (参数覆盖, 回归收据写入位置)。"""

    payload = {
        "schema_version": "arm64-code-regression-receipt/v1",
        "fix_commit_sha": fix,
        "fix_commits": [fix],
        "root_cause_id": causes.split()[0],
        "defect": "测试缺陷描述",
        "triggering_failure": {"campaign_id": "c", "batch_sequence": 18},
        "targeted_regression": {"local_unit_tests": ["t"], "arm64_real_check": {"summary": "测试"},
                                "deployment_receipt": {"summary": "测试部署"}},
        "operator": "测试",
    }
    payload.update(draft or {})
    draft_path = _write_json(root / "upload" / "regression-draft.json", payload)
    receipt = root / "runroot-receipts" / "regression.json"
    return {
        "REPAIR_ROOT_CAUSES": causes, "REPAIR_FIX_COMMIT": fix, "REPAIR_SEQ": "293",
        "REGRESSION_DRAFT": str(draft_path), "REGRESSION_RECEIPT": str(receipt),
        "REPAIR_NOTE": "测试登记（总账序号 $REPAIR_SEQ，修复 $REPAIR_FIX_COMMIT）",
    }, receipt


def _stop_hints(output: str) -> tuple[list[str], str | None]:
    """停下输出里"下一步"行给出的全部 --from 步骤，与"续跑"行的 --from 步骤。"""

    lines = output.splitlines()
    hint = next((line for line in lines if line.startswith("下一步：")), "")
    resume = next((line for line in lines if line.startswith("续跑：")), "")
    resumed = re.findall(r"--from ([a-z-]+)", resume)
    return re.findall(r"--from ([a-z-]+)", hint), (resumed[0] if resumed else None)


FULL_ORDER = [
    "arm64_supervised_deploy",
    "install.py:verify",
    "install.py:install",
    "install.py:verify",
    "tool-evolution-status",
    "tool-evolution:preview",
    "tool-evolution:apply",
    "tool-evolution-status",
    "reconcile-supervisor-run:run-b",
    f"reconcile-attempt:plain:{ATTEMPT}",
    f"reconcile-attempt:plain:{ATTEMPT}",
    f"reconcile-attempt:approve:{ATTEMPT}",
    f"reconcile-attempt:authorize:{ATTEMPT}",
    "load_approved_recovery_preview",
    "vc5-recover.sh",
]


class DriverManifestFixAndContinueTests(unittest.TestCase):
    def test_new_driver_files_are_in_manifest_and_parse(self) -> None:
        manifest = driver_tests.driver.load_manifest(DRIVER_ROOT)
        self.assertEqual(manifest, driver_tests.driver.build_manifest(DRIVER_ROOT))
        paths = {row["path"]: row for row in manifest["files"]}
        for relative, mode in (("driver/fix-and-continue.sh", "0700"), ("driver/fix_and_continue.py", "0700"),
                               ("driver/fix-and-continue.example.params", "0600")):
            self.assertIn(relative, paths)
            self.assertEqual(paths[relative]["mode"], mode)
            self.assertEqual(paths[relative]["sha256"], _sha256(DRIVER_ROOT / relative))
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = subprocess.run([sys.executable, str(HELPER), "steps"], capture_output=True, text=True,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(steps.returncode, 0, steps.stderr)
        self.assertEqual(steps.stdout.split(), [
            "deploy", "postdeploy", "item-tests", "evolution", "pre-extend", "reconcile-runs", "reconcile-attempt",
            "repair", "approve", "authorize", "extend", "accepted", "recover",
        ])


class RoundParamsTests(unittest.TestCase):
    def _fixture(self, root: Path) -> _Round:
        return _Round(root)

    def _load(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HELPER), "load-params", str(path)], capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_round_params_parse_with_shared_safe_parser(self) -> None:
        helper = load_helper()
        # 同一安全解析：辅助脚本导入的就是驱动目录里的 parse_env.py，并经它的词法层读取轮次参数文件。
        self.assertEqual(Path(helper.parse_env.__file__).resolve(), (SCRIPTS / "parse_env.py").resolve())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = self._fixture(root)
            with mock.patch.object(helper.parse_env, "parse_assignments", wraps=helper.parse_env.parse_assignments) as spy:
                values = helper.load_params(fixture.params_path)
            self.assertTrue(any("HEAD_COMMIT" in call.args[1] for call in spy.call_args_list), spy.call_args_list)
            self.assertEqual(values["HEAD_COMMIT"], fixture.head)
            result = self._load(fixture.params_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            exported = dict(line[len("export "):].split("=", 1) for line in result.stdout.splitlines())
            for line in result.stdout.splitlines():
                self.assertRegex(line, r"^export [A-Z_][A-Z0-9_]*=('[^']*'|[A-Za-z0-9_./:@%+=,-]+)$")
            self.assertEqual(exported["VC_ENV"], str(fixture.runroot / "env.sh"))
            self.assertEqual(exported["OUT"], str(fixture.out))
            self.assertEqual(exported["C"], str(fixture.campaign_dir))
            self.assertEqual(exported["EXTEND_PHASE"], "VC-5")
            self.assertEqual(exported["REPAIR_FIX_COMMIT"], fixture.head)

    def test_example_template_parses_with_shared_lexer(self) -> None:
        """仓库模板（.params 后缀，避开 .gitignore 的 *.env）经同一词法层可解析，且列全了必填键。"""

        helper = load_helper()
        text = (SCRIPTS / "fix-and-continue.example.params").read_text(encoding="utf-8")
        values = helper.parse_env.parse_assignments(text, (*helper.REQUIRED_KEYS, *helper.OPTIONAL_KEYS))
        self.assertEqual(sorted(set(helper.REQUIRED_KEYS) - set(values)), [])
        self.assertEqual(values["VC_ENV"], "/root/vc-rounds/REPLACE_RUNROOT/env.sh")

    def test_round_params_reject_commands_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = self._fixture(root)
            marker = root / "marker"
            template = fixture.params_path.read_text(encoding="utf-8")
            cases = {
                "命令替换": template.replace('ROUND="r99"', f'ROUND="$(touch {marker})"'),
                "反引号": template.replace('ROUND="r99"', f"ROUND=`touch {marker}`"),
                "分号": template.replace('ROUND="r99"', f"ROUND=r99; touch {marker}"),
                "未知键": template + f"EXTRA=$(touch {marker})\n",
                "缺必填键": re.sub(r'^APPROVER=.*\n', "", template, flags=re.M),
                "非法提交": template.replace(f'HEAD_COMMIT="{fixture.head}"', 'HEAD_COMMIT="abc"'),
                "相对路径": template.replace(f'EXPECT="{fixture.expect_path}"', 'EXPECT="upload/expect.json"'),
                "上跳路径": template.replace(f'EXPECT="{fixture.expect_path}"', 'EXPECT="/tmp/../etc/passwd"'),
                "非法测试名": template.replace('ITEM_TESTS="tools.official_client_capture.tests.test_fc_item"', 'ITEM_TESTS="a b-c"'),
                "非法截止": template + 'EXTEND_DEADLINE="明天"\nEXTEND_REASON="x"\n',
                "延期缺理由": template + 'EXTEND_DEADLINE="2099-01-01T00:00:00Z"\n',
                "只给 K": template.replace('ITEM_TESTS_K_MODULES="tools.official_client_capture.tests.test_fc_item"\n', ""),
                "根因缺说明": template + 'REPAIR_ROOT_CAUSES="rc1-a"\n',
            }
            for name, text in cases.items():
                path = _write(root / "cases" / f"{len(name)}-{abs(hash(name))}.env", text)
                result = self._load(path)
                self.assertEqual(result.returncode, 2, f"{name} 应被拒绝：{result.stdout}")
                self.assertIn("参数文件拒绝加载", result.stderr, name)
                self.assertFalse(marker.exists(), f"{name} 不得执行参数文件内容")

    def test_round_params_must_match_driver_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = self._fixture(root)
            for key, value in (("CAMPAIGN", "other-campaign"), ("CANDIDATE", "other-candidate"),
                               ("D", str(root / "other-data"))):
                fixture.write_params({key: value})
                result = self._load(fixture.params_path)
                self.assertEqual(result.returncode, 2, key)
                self.assertIn("与驱动参数文件不一致", result.stderr, key)


class ReconciliationScanTests(unittest.TestCase):
    """扫描用真实监督器判据：状态读取、预约窗口与已对账收据核验都是受管函数。"""

    def _state(self, campaign_id: str, name: str, status: str, started: float) -> dict:
        return {
            "schema_version": supervisor.STATE_SCHEMA, "supervisor_schema_version": supervisor.SCHEMA_VERSION,
            "campaign_id": campaign_id, "phase": "VC-5", "owner_pid": 4242, "owner_nonce": name.removeprefix("run-"),
            "started_at_utc": "2026-09-28T00:00:00Z", "started_at_epoch": started, "started_monotonic_ns": 1,
            "deadline_at_epoch": started + 3600, "deadline_monotonic_ns": 2, "state": status,
        }

    def _run(self, state_dir: Path, campaign_id: str, name: str, status: str, started: float) -> Path:
        run = state_dir / name
        run.mkdir(mode=0o700)
        b4_reconciliation_fixtures.write_private_json(run / "state.json", self._state(campaign_id, name, status, started))
        manifest = {"campaign_id": campaign_id, "phase": "VC-5", "batch_id": f"vc-5-{int(started)}",
                    "batch_sequence": int(started), "batch_sha256": "9" * 64, "execute_items": ["candidate-run"],
                    "reuse_items": []}
        b4_reconciliation_fixtures.write_private_json(run / "campaign-run-manifest.json", {"manifest": manifest})
        return run

    def _reservation(self, campaign_dir: Path, candidate: str, attempt: str, started_utc: str, status: str | None = None) -> None:
        root = campaign_dir / "candidates" / candidate / "attempts" / attempt
        b4_reconciliation_fixtures.write_private_json(root / "reservation.json", {"started_at_utc": started_utc})
        if status is not None:
            b4_reconciliation_fixtures.write_private_json(root / "attempt.json", {"status": status, "results": []})

    def test_scan_follows_supervisor_window_and_tail(self) -> None:
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_id = "c-scan"
            campaign_dir = root / "evidence" / "campaigns" / campaign_id
            campaign_dir.mkdir(parents=True)
            b4_reconciliation_fixtures.write_private_json(campaign_dir / "campaign.json", {"campaign_id": campaign_id})
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            t0 = 1_790_553_600.0  # 2026-09-28T00:00:00Z
            # 链头：早已失败但其后有正常结束的 run —— 不在链尾，不处理。
            self._run(state_dir, campaign_id, "run-old-failed", "failed", t0 - 3000)
            self._run(state_dir, campaign_id, "run-ok", "stopped", t0 - 2000)
            # 链尾：无预约的失败 run（派发前失败）→ supervisor-run 对账。
            no_reservation = self._run(state_dir, campaign_id, "run-tail-a", "failed", t0 - 1000)
            # 链尾：看门狗中止，窗口内有候选 attempt 预约 → attempt 对账；目标 attempt 留给下一步。
            self._run(state_dir, campaign_id, "run-tail-b", "watchdog-aborted", t0)
            self._run(state_dir, "other-campaign", "run-foreign", "failed", t0 + 10)
            self._reservation(campaign_dir, "cand-1", "att-target", "2026-09-28T00:00:05Z")
            self._reservation(campaign_dir, "cand-1", "att-other", "2026-09-28T00:00:06Z")
            # 采集已完整收口的 attempt 不是中断，不计入窗口。
            self._reservation(campaign_dir, "cand-1", "att-settled", "2026-09-28T00:00:07Z", status="awaiting_receipts")
            result = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                        target_attempt="att-target", supervisor=supervisor)
            self.assertEqual(result["active"], [])
            self.assertEqual(result["unsupported"], [])
            self.assertEqual(result["tail"], ["run-tail-a", "run-tail-b"])
            self.assertEqual(result["foreign"], ["run-foreign"])
            pending = [(item["kind"], item.get("run_id"), item.get("attempt_id")) for item in result["pending"]]
            # run-tail-a 的窗口同样覆盖两个预约（窗口只有起点）——与 reconciler／监督器同判据，改走 attempt 对账。
            self.assertEqual(pending, [("attempt", "run-tail-a", "att-other")])
            self.assertEqual(result["target_in_window"], True)
            # 预约移除后 run-tail-a 回到 supervisor-run 对账；写好收据与总账绑定后不再 pending。
            shutil.rmtree(campaign_dir / "candidates")
            result = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                        target_attempt="att-target", supervisor=supervisor)
            self.assertEqual([(item["kind"], item["run_id"]) for item in result["pending"]],
                             [("supervisor-run", "run-tail-a"), ("supervisor-run", "run-tail-b")])
            state = json.loads((no_reservation / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((no_reservation / "campaign-run-manifest.json").read_text(encoding="utf-8"))["manifest"]
            b4_reconciliation_fixtures.bind_supervisor_run_reconciliation(campaign_dir, no_reservation, state, manifest)
            result = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                        target_attempt="att-target", supervisor=supervisor)
            self.assertEqual([item["run_id"] for item in result["pending"]], ["run-tail-b"])
            self.assertEqual([item["run_id"] for item in result["reconciled"]], ["run-tail-a"])

    def test_scan_reports_active_and_unsupported_runs(self) -> None:
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir = root / "campaign"
            campaign_dir.mkdir()
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            self._run(state_dir, "c1", "run-a", "stopped", 1_790_000_000.0)
            self._run(state_dir, "c1", "run-b", "audit-incomplete", 1_790_000_100.0)
            self._run(state_dir, "c1", "run-c", "running", 1_790_000_200.0)
            result = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id="c1",
                                                        target_attempt="att", supervisor=supervisor)
            self.assertEqual(result["active"], ["run-c"])
            self.assertEqual(result["unsupported"], [{"run_id": "run-b", "state": "audit-incomplete"}])

    def test_scan_revisits_objects_whose_last_verdict_paused(self) -> None:
        """第 59 项：受管对账器先写收据、入总账，再判定——判"根因达上限"暂停的父 run 收据与绑定都已完整，
        按监督器判据核验通过。前次对账停在暂停的对象（revisit）核验通过后仍列为待对账，不能当作已对账跳过；
        核验不过照旧失败关闭，不因 revisit 改走对账。"""

        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_id = "c-revisit"
            campaign_dir = root / "evidence" / "campaigns" / campaign_id
            campaign_dir.mkdir(parents=True)
            b4_reconciliation_fixtures.write_private_json(campaign_dir / "campaign.json", {"campaign_id": campaign_id})
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            t0 = 1_790_553_600.0
            self._run(state_dir, campaign_id, "run-ok", "stopped", t0 - 2000)
            paused = self._run(state_dir, campaign_id, "run-paused", "failed", t0 - 1000)
            state = json.loads((paused / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((paused / "campaign-run-manifest.json").read_text(encoding="utf-8"))["manifest"]
            receipt = b4_reconciliation_fixtures.bind_supervisor_run_reconciliation(campaign_dir, paused, state, manifest)
            revisit = [{"kind": "supervisor-run", "subject": "run-paused"}]
            plain = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                       target_attempt="att", supervisor=supervisor)
            self.assertEqual([item["run_id"] for item in plain["reconciled"]], ["run-paused"])
            self.assertEqual(plain["pending"], [])
            again = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                       target_attempt="att", supervisor=supervisor, revisit=revisit)
            self.assertEqual(again["reconciled"], [])
            self.assertEqual([(item["kind"], item["run_id"], item["run_dir"], item.get("revisit")) for item in again["pending"]],
                             [("supervisor-run", "run-paused", str(paused), True)])
            self.assertEqual(again["revisit"], revisit)
            self.assertEqual(again["problems"], [])
            # 不在链尾的对象（其后已有正常结束的 run）不再重新对账：revisit 只作用于链尾。
            self._run(state_dir, campaign_id, "run-later-ok", "stopped", t0)
            beyond = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                        target_attempt="att", supervisor=supervisor, revisit=revisit)
            self.assertEqual((beyond["pending"], beyond["revisit"], beyond["tail"]), ([], [], []))
            shutil.rmtree(state_dir / "run-later-ok")
            receipt.write_text(receipt.read_text(encoding="utf-8").replace("b4-fixture-01", "b4-fixture-02"), encoding="utf-8")
            broken = helper.scan_reconciliation_targets(campaign_dir, state_dir, campaign_id=campaign_id,
                                                        target_attempt="att", supervisor=supervisor, revisit=revisit)
            self.assertEqual((broken["pending"], broken["reconciled"], broken["revisit"]), ([], [], []))
            self.assertEqual(len(broken["problems"]), 1)
            self.assertIn("run-paused", broken["problems"][0])


class FixAndContinueScriptTests(unittest.TestCase):
    """脚本级：PATH 垫片 + 数据根受管工具替身 + 真实 git bundle。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-6000:])
        self.assertIn("FIX_AND_CONTINUE_DONE", result.stdout)

    def test_full_round_runs_steps_in_order(self) -> None:
        fixture = _Round(self.root)
        result = fixture.run()
        self._assert_ok(result)
        self.assertEqual(fixture.call_keys(), FULL_ORDER)
        statuses = {name: fixture.step(name)["status"] for name in (
            "deploy", "postdeploy", "item-tests", "evolution", "pre-extend", "reconcile-runs", "reconcile-attempt",
            "repair", "approve", "authorize", "extend", "accepted", "recover")}
        self.assertEqual(statuses, {
            "deploy": "passed", "postdeploy": "passed", "item-tests": "passed", "evolution": "passed",
            "pre-extend": "skipped", "reconcile-runs": "passed", "reconcile-attempt": "passed", "repair": "skipped",
            "approve": "passed", "authorize": "passed", "extend": "skipped", "accepted": "passed", "recover": "passed",
        })
        # 部署：staging 是 HEAD 的干净检出，仓库文档已复制，属主收口走 chown。
        self.assertEqual(_git("rev-parse", "HEAD", cwd=fixture.staging_tree), fixture.head)
        self.assertTrue((fixture.staging_tree / "docs" / "repository-docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md").is_file())
        self.assertIn(f"chown -R root:root {fixture.staging_tree}", (fixture.stub / "chown.log").read_text(encoding="utf-8"))
        # 部署后：数据根部署脚本副本已同步、旧副本留档。
        self.assertEqual((fixture.data / "tools" / "arm64_supervised_deploy.py").read_text(encoding="utf-8"), fixture.deploy_script_new)
        self.assertTrue((fixture.runroot / "stale-tools-backup" / "arm64_supervised_deploy.py.pre-r99").is_file())
        # 实测：日志 exit=0、staging 干净、两段各有 OK。
        log = (fixture.out / "item-tests.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"(?m)^staging-clean=yes$")
        self.assertRegex(log, r"(?m)^exit=0$")
        self.assertEqual(len(re.findall(r"(?m)^OK", log)), 2)
        # 批准摘要取自同一次运行的恢复预览；授权与后台续跑用批准步骤记录的预览路径。
        approve = fixture.step("approve")
        preview = fixture.campaign_dir / "control" / "reconciliation" / f"attempt-{ATTEMPT}" / "recovery-preview-01.json"
        self.assertEqual(approve["summary"]["review_sha256"], "a" * 64)
        self.assertEqual(approve["summary"]["preview_path"], str(preview))
        approve_call = [c for c in fixture.calls() if "--approve-recovery-sha256" in c["argv"]][0]["argv"]
        self.assertEqual(approve_call[approve_call.index("--approve-recovery-sha256") + 1], "a" * 64)
        recover_call = [c for c in fixture.calls() if c["module"] == "vc5-recover.sh"][0]
        self.assertEqual(recover_call["argv"], [str(preview)])
        self.assertEqual(recover_call["ARM64_VC_ENV"], str(fixture.runroot / "env.sh"))
        self.assertEqual(recover_call["VC_STATE_DIR"], str(fixture.state_dir))
        self.assertIn("VC5_RECOVER_STUB", (fixture.runroot / "vc5-recover.out").read_text(encoding="utf-8"))
        # 演进批准用预览的 review_sha256 与参数里的批准人；每步原始输出留档。
        apply = [c for c in fixture.calls() if c["argv"][:1] == ["tool-evolution"] and "--approve-sha256" in c["argv"]][0]["argv"]
        self.assertEqual(apply[apply.index("--approve-sha256") + 1], "e" * 64)
        self.assertEqual(apply[apply.index("--approved-by") + 1], "测试批准人（老板授权“修好接着跑”）")
        self.assertTrue(any((fixture.out / "raw").glob("*-evolution-preview.out")))
        self.assertIn("vc5-all.sh", result.stdout)

    def test_second_run_skips_idempotently(self) -> None:
        fixture = _Round(self.root)
        self._assert_ok(fixture.run())
        before = fixture.call_keys()
        second = fixture.run()
        self._assert_ok(second)
        added = fixture.call_keys()[len(before):]
        # 再次运行：部署、实测、演进、父 run 对账、后台续跑全部幂等跳过；对账／批准／授权是受管工具自身幂等的重复调用。
        self.assertEqual(added, [
            "install.py:verify",
            "tool-evolution-status",
            f"reconcile-attempt:plain:{ATTEMPT}",
            f"reconcile-attempt:plain:{ATTEMPT}",
            f"reconcile-attempt:approve:{ATTEMPT}",
            f"reconcile-attempt:authorize:{ATTEMPT}",
            "load_approved_recovery_preview",
        ])
        for name in ("deploy", "item-tests", "evolution", "reconcile-runs", "recover"):
            self.assertEqual(fixture.step(name)["status"], "skipped", name)
        self.assertEqual(sorted(p.name for p in fixture.out.glob("item-tests.log*")), ["item-tests.log"])

    def test_item_test_failure_stops_and_from_resumes(self) -> None:
        fixture = _Round(self.root)
        failed = fixture.run(env={"FC_ITEM_FAIL": "1"})
        self.assertEqual(failed.returncode, EXIT_FAILED, failed.stdout + failed.stderr)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=item-tests", failed.stdout + failed.stderr)
        self.assertIn(f"--from item-tests", failed.stdout + failed.stderr)
        self.assertIn("下一步", failed.stdout + failed.stderr)
        self.assertEqual(fixture.step("item-tests")["status"], "failed")
        self.assertNotIn("tool-evolution-status", fixture.call_keys())
        resumed = fixture.run("--from", "item-tests")
        self._assert_ok(resumed)
        # 失败日志改名留档后重跑；部署不重做。
        self.assertEqual(len(list(fixture.out.glob("item-tests.log.failed-*"))), 1)
        self.assertEqual(fixture.call_keys().count("arm64_supervised_deploy"), 1)
        self.assertEqual(fixture.call_keys()[-1], "vc5-recover.sh")

    def test_item_test_dirtying_staging_stops(self) -> None:
        """实测用例全绿但往 staging 树里写了文件：staging-clean=no 即停，不进入演进登记。"""

        fixture = _Round(self.root)
        result = fixture.run(env={"FC_ITEM_DIRTY": "1"})
        self.assertEqual(result.returncode, EXIT_FAILED, result.stdout + result.stderr)
        self.assertIn("staging-clean=no", result.stdout + result.stderr)
        self.assertEqual(fixture.step("item-tests")["status"], "failed")
        self.assertNotIn("tool-evolution-status", fixture.call_keys())

    def test_from_requires_passed_predecessors(self) -> None:
        fixture = _Round(self.root)
        result = fixture.run("--from", "approve")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("前序步骤 deploy 在本轮没有记录", result.stdout + result.stderr)
        self.assertEqual(fixture.calls(), [])
        unknown = fixture.run("--from", "nope")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("未知步骤", unknown.stdout + unknown.stderr)

    def test_guard_diff_mismatch_stops_and_from_postdeploy_resumes(self) -> None:
        fixture = _Round(self.root)
        fixture.write_expect(diff_added=['"' + "7" * 64 + '"'])
        failed = fixture.run()
        self.assertEqual(failed.returncode, EXIT_FAILED, failed.stdout + failed.stderr)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=postdeploy", failed.stdout + failed.stderr)
        self.assertIn("守护", failed.stdout + failed.stderr)
        self.assertNotIn("install.py:install", fixture.call_keys())
        fixture.write_expect()
        self._assert_ok(fixture.run("--from", "postdeploy"))
        self.assertEqual(fixture.call_keys().count("arm64_supervised_deploy"), 1)

    def test_accounting_pause_stops_without_overreach(self) -> None:
        paused = {"stdout": {"status": "paused", "attempt_id": ATTEMPT,
                             "decision": {"decision": "paused", "pause_kinds": ["accounting"],
                                          "reasons": ["总账 blocked：['reconcile-attempt:x']（暂停：accounting-resolve 补账后继续）"]},
                             "project_head": {"blocked": True},
                             "next_command": "accounting-resolve preview/apply（为未决 operation 补账）；批准后从原对账 checkpoint 继续"},
                  "rc": 3}
        fixture = _Round(self.root, scenario={"responses": {"reconcile-attempt": [paused]}})
        result = fixture.run()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, EXIT_OPERATOR, output)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=reconcile-attempt", output)
        self.assertIn("accounting-resolve", output)
        self.assertIn("--from reconcile-attempt", output)
        self.assertEqual(fixture.step("reconcile-attempt")["status"], "needs-operator")
        keys = fixture.call_keys()
        self.assertEqual(keys[-1], f"reconcile-attempt:plain:{ATTEMPT}")
        for forbidden in ("accounting-resolve", "environment-isolate", "record-root-cause-repair", "vc5-recover.sh"):
            self.assertNotIn(forbidden, keys)
        self.assertFalse(any(key.startswith(("reconcile-attempt:approve", "reconcile-attempt:authorize")) for key in keys))
        self.assertFalse(any("--force" in arg for call in fixture.calls() for arg in call["argv"]))

    def test_environment_pause_and_permanent_stop_stop(self) -> None:
        for decision, expected in (
            ({"status": "paused", "decision": {"decision": "paused", "pause_kinds": ["environment"], "reasons": ["存在未隔离的环境污染"]},
              "next_command": "修复环境并取得晚于污染的干净环境复核后 environment-isolate preview/apply（隔离污染 attempt）"},
             "environment-isolate"),
            ({"status": "permanent_stop", "decision": {"decision": "permanent_stop", "terminal_reason": "identity_changed",
                                                        "reasons": ["当前有效 wire 身份或策略摘要已变化"]},
              "next_command": "stop-the-line"}, "永久停线"),
        ):
            with self.subTest(expected=expected):
                directory = tempfile.TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                root = Path(directory.name).resolve()
                root.chmod(0o700)
                fixture = _Round(root, scenario={"responses": {"reconcile-attempt": [{"stdout": decision, "rc": 3}]}})
                result = fixture.run()
                self.assertEqual(result.returncode, EXIT_OPERATOR, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertNotIn("vc5-recover.sh", fixture.call_keys())
                self.assertFalse(any(key.startswith("reconcile-attempt:approve") for key in fixture.call_keys()))

    def test_expired_stage_is_extended_before_reconcile(self) -> None:
        fixture = _Round(self.root, state={"stage_deadline": "2020-01-01T00:00:00Z"},
                         params={"EXTEND_DEADLINE": "2099-06-01T00:00:00Z",
                                 "EXTEND_REASON": "修好接着跑：测试轮次续跑（阶段延期）"})
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        self.assertLess(keys.index("deadline-extend:apply"), keys.index(f"reconcile-attempt:plain:{ATTEMPT}"))
        self.assertEqual(keys.count("deadline-extend:apply"), 1)
        self.assertEqual(fixture.step("pre-extend")["status"], "passed")
        self.assertEqual(fixture.step("extend")["status"], "skipped")
        extend = [c for c in fixture.calls() if c["argv"][:2] == ["deadline-extend", "apply"]][0]["argv"]
        self.assertEqual(extend[extend.index("--approve-sha256") + 1], "d" * 64)
        self.assertEqual(fixture.stub_state()["stage_deadline"], "2099-06-01T00:00:00Z")

    def test_extend_never_sits_between_approve_and_authorize(self) -> None:
        """账本 recovery_required、阶段未过期但给了更晚的延期截止：对账前 pre-extend 延期，预览冻结延期后的账本 head，
        授权时 head 未推进。若延期夹在批准与授权之间（设计稿原顺序），授权会被"账本 head 已推进"拒绝。"""

        fixture = _Round(self.root, state={"stage_deadline": "2099-01-01T00:00:00Z"},
                         params={"EXTEND_DEADLINE": "2099-06-01T00:00:00Z",
                                 "EXTEND_REASON": "修好接着跑：测试轮次续跑（预留补跑时间）"})
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        self.assertEqual(fixture.step("pre-extend")["status"], "passed")
        self.assertEqual(fixture.step("extend")["status"], "skipped")
        self.assertLess(keys.index("deadline-extend:apply"), keys.index(f"reconcile-attempt:plain:{ATTEMPT}"))
        self.assertLess(keys.index(f"reconcile-attempt:approve:{ATTEMPT}"), keys.index(f"reconcile-attempt:authorize:{ATTEMPT}"))
        self.assertTrue(fixture.stub_state()["authorized"])

    def test_candidate_review_extends_after_authorize(self) -> None:
        fixture = _Round(self.root, state={"stage_deadline": "2020-01-01T00:00:00Z", "phase": None,
                                           "status_before_pause": "candidate_review_required"},
                         params={"EXTEND_DEADLINE": "2099-06-01T00:00:00Z",
                                 "EXTEND_REASON": "修好接着跑：测试轮次续跑（阶段延期）"})
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        self.assertEqual(fixture.step("pre-extend")["status"], "skipped")
        self.assertEqual(fixture.step("extend")["status"], "passed")
        self.assertLess(keys.index(f"reconcile-attempt:authorize:{ATTEMPT}"), keys.index("deadline-extend:preview"))
        self.assertLess(keys.index("deadline-extend:apply"), keys.index("load_approved_recovery_preview"))

    def test_repair_registers_once_from_draft_and_skips_same_fix(self) -> None:
        draft = _write_json(self.root / "upload" / "regression-draft.json", {
            "schema_version": "arm64-code-regression-receipt/v1",
            "fix_commit_sha": "4" * 40,
            "fix_commits": ["4" * 40],
            "root_cause_id": "rc1-fixture",
            "defect": "测试缺陷描述",
            "triggering_failure": {"campaign_id": "c", "batch_sequence": 18},
            "targeted_regression": {"local_unit_tests": ["t"], "arm64_real_check": {"summary": "测试"},
                                    "deployment_receipt": {"summary": "测试部署"}},
            "operator": "测试",
        })
        receipt = self.root / "runroot-receipts" / "regression.json"
        fixture = _Round(self.root, params={
            "REPAIR_ROOT_CAUSES": "rc1-fixture", "REPAIR_FIX_COMMIT": "4" * 40, "REPAIR_SEQ": "293",
            "REGRESSION_DRAFT": str(draft), "REGRESSION_RECEIPT": str(receipt),
            "REPAIR_NOTE": "测试登记（总账序号 $REPAIR_SEQ，修复 $REPAIR_FIX_COMMIT）",
        })
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        self.assertLess(keys.index(f"reconcile-attempt:plain:{ATTEMPT}"), keys.index("record-root-cause-repair"))
        self.assertLess(keys.index("record-root-cause-repair"), keys.index(f"reconcile-attempt:approve:{ATTEMPT}"))
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        tests = payload["targeted_regression"]["arm64_real_check"]["tests"]
        self.assertEqual(tests["sha256"], _sha256(fixture.out / "item-tests.log"))
        self.assertEqual(payload["defect"], "测试缺陷描述")
        repair = fixture.stub_state()["repairs"][0]
        self.assertEqual(repair["root_cause_ids"], ["rc1-fixture"])
        self.assertEqual(repair["bindings"]["fix_commit_sha"], "4" * 40)
        self.assertEqual(repair["bindings"]["regression_receipt_sha256"], _sha256(receipt))
        deploy_receipt = sorted((fixture.data / "control").glob("codex-*-supervisor-enable-*.json"))[-1]
        self.assertEqual(repair["bindings"]["deployment_receipt_sha256"], _sha256(deploy_receipt))
        self.assertEqual(repair["note"], f"测试登记（总账序号 293，修复 {'4' * 40}）")
        self._assert_ok(fixture.run())
        self.assertEqual(fixture.call_keys().count("record-root-cause-repair"), 1)
        self.assertEqual(fixture.step("repair")["status"], "skipped")

    def test_repair_without_receipt_stops_when_reconcile_needs_it(self) -> None:
        paused = {"stdout": {"status": "paused", "attempt_id": ATTEMPT, "root_cause": {"root_cause_id": "rc1-fixture"},
                             "decision": {"decision": "paused", "pause_kinds": ["root_cause_repair"],
                                          "reasons": ["根因 ['rc1-fixture'] 累计失败已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）"]},
                             "next_command": "登记根因修复证据；随后重新执行本对账"}, "rc": 3}
        fixture = _Round(self.root, scenario={"responses": {"reconcile-attempt": [paused]}})
        result = fixture.run()
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stdout + result.stderr)
        self.assertIn("record-root-cause-repair", result.stdout + result.stderr)
        self.assertNotIn("record-root-cause-repair", fixture.call_keys())

    def test_wire_change_in_evolution_preview_stops(self) -> None:
        fixture = _Round(self.root, scenario={"wire_changed": True})
        result = fixture.run()
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stdout + result.stderr)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=evolution", result.stdout + result.stderr)
        self.assertIn("wire", result.stdout + result.stderr)
        self.assertNotIn("tool-evolution:apply", fixture.call_keys())

    def test_supervisor_run_reconciliation_redirects_to_attempt(self) -> None:
        fixture = _Round(self.root, scenario={"redirect_runs": {"run-b": ["20260928T000000Z-aaaaaaaaaaaaaaaa"]}})
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        start = keys.index("reconcile-supervisor-run:run-b")
        self.assertEqual(keys[start:start + 3], [
            "reconcile-supervisor-run:run-b",
            "reconcile-attempt:plain:20260928T000000Z-aaaaaaaaaaaaaaaa",
            f"reconcile-attempt:plain:{ATTEMPT}",
        ])
        self.assertEqual(fixture.step("reconcile-runs")["summary"]["redirected_runs"], ["run-b"])

    def test_active_run_blocks_before_any_step(self) -> None:
        fixture = _Round(self.root, runs=[("run-a", "stopped", 100.0), ("run-live", "running", 200.0)])
        result = fixture.run()
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stdout + result.stderr)
        self.assertIn("run-live", result.stdout + result.stderr)
        self.assertEqual(fixture.calls(), [])


ROOT_CAUSE_REASON_FIXTURE = "根因 ['rc1-fixture'] 累计失败已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）"
STEP_RECORD_KEYS = {"schema_version", "round", "step", "status", "identity", "params_path", "params_sha256", "run_stamp",
                    "recorded_at_utc", "summary", "reason", "next", "resume_from"}


class ReconcileRunsRootCauseRepairTests(unittest.TestCase):
    """第 59 项：reconcile-runs 对账遇"项目总账根因达上限"暂停时，与 reconcile-attempt 旁路走同一条登记路径。

    缺陷（2026-09-28 r24 真实续跑暴露）：reconcile-runs 的判定对暂停一律停下，提示"填参数后 --from reconcile-attempt"，
    但 reconcile-runs 不是 passed，check_from 拒绝；--from repair 同样被拒；而受管对账器在判定之前就写下了对账收据与总账
    入账，扫描续跑时会把暂停的父 run 当作"已对账"跳过、暂停判定被悄悄丢掉。只能绕开编排手工登记。
    替身与受管对账器同：暂停时对账收据已存在。
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _new_root(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root.chmod(0o700)
        return root

    def _assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-6000:])
        self.assertIn("FIX_AND_CONTINUE_DONE", result.stdout)

    def _assert_hints_resumable(self, fixture: _Round, output: str, expected: str = "reconcile-runs") -> None:
        """停下提示里的每个 --from 与"续跑"行一致，且都真的能通过 check_from。"""

        hinted, resumed = _stop_hints(output)
        self.assertEqual(resumed, expected, output[-4000:])
        self.assertTrue(hinted, f"下一步里应给出 --from：{output[-4000:]}")
        self.assertEqual(set(hinted), {expected}, output[-4000:])
        for step in {*hinted, resumed}:
            check = fixture.check_from(step)
            self.assertEqual(check.returncode, 0, f"提示的 --from {step} 通不过 check_from：{check.stderr}")

    def test_runs_pause_registers_inline_and_reconciles_again(self) -> None:
        """目标 1：参数已给登记材料——同一次运行内登记（同一判定、同一命令、同一回归收据生成与校验），
        登记后对该父 run 重新对账一次，接着跑到底；repair 步骤按"已登记同一修复提交"跳过。"""

        repair, receipt = _repair_params(self.root)
        fixture = _Round(self.root, scenario={"root_cause_limits": {"run-b": "rc1-fixture"}}, params=repair)
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        start = keys.index("reconcile-supervisor-run:run-b")
        self.assertEqual(keys[start:start + 4], [
            "reconcile-supervisor-run:run-b", "record-root-cause-repair", "reconcile-supervisor-run:run-b",
            f"reconcile-attempt:plain:{ATTEMPT}",
        ])
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        # 同一命令：与 repair 步骤逐字相同的 record-root-cause-repair（总账目录、根因、三项绑定、note）。
        call = [c["argv"] for c in fixture.calls() if c["argv"][:1] == ["record-root-cause-repair"]][0]
        deploy_receipt = sorted((fixture.data / "control").glob("codex-*-supervisor-enable-*.json"))[-1]
        self.assertEqual(call, [
            "record-root-cause-repair", "--ledger-dir", str(fixture.data / "evidence" / "campaigns" / "upgrade-project-ledger"),
            "--root-cause-id", "rc1-fixture", "--kind", "code",
            "--binding", f"fix_commit_sha={'4' * 40}", "--binding", f"regression_receipt_sha256={_sha256(receipt)}",
            "--binding", f"deployment_receipt_sha256={_sha256(deploy_receipt)}",
            "--note", f"测试登记（总账序号 293，修复 {'4' * 40}）",
        ])
        # 同一回归收据生成：草稿 + 本轮实测日志摘要 + 最新部署收据。
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["targeted_regression"]["arm64_real_check"]["tests"]["sha256"],
                         _sha256(fixture.out / "item-tests.log"))
        self.assertEqual(payload["targeted_regression"]["deployment_receipt"]["sha256"], _sha256(deploy_receipt))
        self.assertEqual(payload["defect"], "测试缺陷描述")
        runs = fixture.step("reconcile-runs")
        self.assertEqual(runs["status"], "passed")
        repairs = runs["summary"]["root_cause_repairs"]
        self.assertEqual([(item["kind"], item["object"], item["action"]) for item in repairs], [("supervisor-run", "run-b", "recorded")])
        self.assertEqual(repairs[0]["todo"], ["rc1-fixture"])
        self.assertEqual(repairs[0]["pause_kinds"], ["root_cause_repair"])
        self.assertEqual(repairs[0]["operation_id"], "repair:rc1-fixture:0000")
        self.assertEqual(repairs[0]["regression_receipt_sha256"], _sha256(receipt))
        self.assertEqual(runs["summary"]["revisit"], [])
        self.assertEqual([(item["subject"], item.get("after_root_cause_repair")) for item in runs["summary"]["done"]],
                         [("run-b", True)])
        # 同一幂等规则：repair 步骤见同一修复提交已登记即跳过；再跑一整轮不重复登记、不重复对账。
        self.assertEqual(fixture.step("repair")["status"], "skipped")
        self.assertIn("已登记同一修复提交", fixture.step("repair")["reason"])
        self._assert_ok(fixture.run())
        self.assertEqual(fixture.call_keys().count("record-root-cause-repair"), 1)
        self.assertEqual(fixture.call_keys().count("reconcile-supervisor-run:run-b"), 2)
        self.assertEqual(fixture.step("reconcile-runs")["status"], "skipped")

    def test_runs_pause_without_material_hint_resumes_from_reconcile_runs(self) -> None:
        """目标 2：没给材料时停下，提示的 --from 能通过 check_from；照提示补材料续跑，登记后跑到底。"""

        fixture = _Round(self.root, scenario={"root_cause_limits": {"run-b": "rc1-fixture"}})
        stopped = fixture.run()
        output = stopped.stdout + stopped.stderr
        self.assertEqual(stopped.returncode, EXIT_OPERATOR, output)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=reconcile-runs status=needs-operator resume_from=reconcile-runs", output)
        self.assertIn("REPAIR_ROOT_CAUSES", output)
        self.assertIn("record-root-cause-repair", output)
        self.assertNotIn("--from reconcile-attempt", output)
        self._assert_hints_resumable(fixture, output)
        self.assertNotIn("record-root-cause-repair", fixture.call_keys())
        self.assertEqual(fixture.step("reconcile-runs")["summary"]["revisit"], [{"kind": "supervisor-run", "subject": "run-b"}])
        # 照提示：参数文件补登记材料，按提示的 --from 续跑。
        repair, _receipt = _repair_params(self.root)
        fixture.write_params(repair)
        _hinted, resume = _stop_hints(output)
        self._assert_ok(fixture.run("--from", resume))
        keys = fixture.call_keys()
        # 暂停 → 续跑重新对账（收据虽已写，前次判定是暂停）→ 登记后重新对账 recoverable。
        self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 3)
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        self.assertLess(keys.index("record-root-cause-repair"), keys.index(f"reconcile-attempt:plain:{ATTEMPT}"))
        for name in ("reconcile-runs", "reconcile-attempt", "approve", "authorize", "accepted", "recover"):
            self.assertEqual(fixture.step(name)["status"], "passed", name)
        self.assertEqual(fixture.step("repair")["status"], "skipped")

    def test_runs_campaign_stop_required_stops_without_registering(self) -> None:
        """目标 3：Campaign 账本 stop_required 不走本旁路——给了登记材料也不登记，停下等人工 campaign-resume；
        人工恢复后按提示续跑，前次暂停的父 run 重新对账，登记仍由 repair 步骤按原规则做。"""

        repair, receipt = _repair_params(self.root)
        fixture = _Round(self.root, scenario={"campaign_stop_required": ["run-b"]}, params=repair)
        stopped = fixture.run()
        output = stopped.stdout + stopped.stderr
        self.assertEqual(stopped.returncode, EXIT_OPERATOR, output)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=reconcile-runs status=needs-operator", output)
        self.assertIn("campaign-resume", output)
        keys = fixture.call_keys()
        self.assertNotIn("record-root-cause-repair", keys)
        self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 1)
        self.assertFalse(receipt.exists(), "不走旁路：不得生成回归收据")
        self.assertFalse(any("campaign-resume" in arg for call in fixture.calls() for arg in call["argv"]))
        self._assert_hints_resumable(fixture, output)
        self.assertNotIn("root_cause_repairs", fixture.step("reconcile-runs")["summary"])
        fixture.set_stub_state(campaign_resumed=True)
        self._assert_ok(fixture.run("--from", "reconcile-runs"))
        keys = fixture.call_keys()
        self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 2)
        self.assertEqual(fixture.step("reconcile-runs")["status"], "passed")
        self.assertNotIn("root_cause_repairs", fixture.step("reconcile-runs")["summary"])
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        self.assertLess(keys.index(f"reconcile-attempt:plain:{ATTEMPT}"), keys.index("record-root-cause-repair"))
        self.assertEqual(fixture.step("repair")["status"], "passed")

    def test_runs_pause_on_redirected_attempt_registers_inline(self) -> None:
        """目标 1（attempt 对象）：父 run 按对账器提示改走 attempt 对账，该 attempt 判根因达上限暂停，同样内联登记后重新对账。"""

        other = "20260928T000000Z-aaaaaaaaaaaaaaaa"
        repair, _receipt = _repair_params(self.root)
        fixture = _Round(self.root, scenario={"redirect_runs": {"run-b": [other]}, "root_cause_limits": {other: "rc1-fixture"}},
                         params=repair)
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        start = keys.index("reconcile-supervisor-run:run-b")
        self.assertEqual(keys[start:start + 5], [
            "reconcile-supervisor-run:run-b", f"reconcile-attempt:plain:{other}", "record-root-cause-repair",
            f"reconcile-attempt:plain:{other}", f"reconcile-attempt:plain:{ATTEMPT}",
        ])
        record = fixture.step("reconcile-runs")
        self.assertEqual([(item["kind"], item["object"], item["action"]) for item in record["summary"]["root_cause_repairs"]],
                         [("attempt", other, "recorded")])
        self.assertEqual(record["summary"]["redirected_runs"], ["run-b"])
        self.assertEqual(fixture.step("repair")["status"], "skipped")

    def test_runs_still_paused_after_repair_stops_and_never_registers_twice(self) -> None:
        """目标 1：登记后重新对账仍暂停就停下；照提示续跑时同一修复提交已登记即不再执行登记命令。"""

        # 参数里登记的根因不是暂停的根因（人填错）：登记一次、重新对账一次仍暂停。
        repair, _receipt = _repair_params(self.root, causes="rc1-fixture")
        fixture = _Round(self.root, scenario={"root_cause_limits": {"run-b": "rc1-other"}}, params=repair)
        stopped = fixture.run()
        output = stopped.stdout + stopped.stderr
        self.assertEqual(stopped.returncode, EXIT_OPERATOR, output)
        self.assertIn("登记根因修复后重新对账仍暂停", output)
        self.assertIn("rc1-other", output)
        keys = fixture.call_keys()
        self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 2)
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        self.assertNotIn(f"reconcile-attempt:plain:{ATTEMPT}", keys)
        self._assert_hints_resumable(fixture, output)
        record = fixture.step("reconcile-runs")
        self.assertEqual((record["status"], record["resume_from"]), ("needs-operator", "reconcile-runs"))
        self.assertEqual(record["summary"]["revisit"], [{"kind": "supervisor-run", "subject": "run-b"}])
        self.assertEqual([item["action"] for item in record["summary"]["root_cause_repairs"]], ["recorded"])
        again = fixture.run("--from", "reconcile-runs")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stdout + again.stderr)
        keys = fixture.call_keys()
        self.assertEqual(keys.count("record-root-cause-repair"), 1)
        self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 4)
        self.assertEqual([item["action"] for item in fixture.step("reconcile-runs")["summary"]["root_cause_repairs"]], ["already"])
        self.assertNotIn("vc5-recover.sh", keys)

    def test_runs_inline_repair_keeps_every_check(self) -> None:
        """目标 4：内联登记不削弱任何核对——回归收据字段、fix_commit_sha、根因集合、实测日志摘要、登记命令结果、
        登记后总账里必须有修复事件；任何一项不过都停在 reconcile-runs（续跑步骤仍是它），不重新对账、不往下走。"""

        cases = {
            "草稿修复提交不符": ({"draft": {"fix_commit_sha": "5" * 40}}, {}, EXIT_OPERATOR, "fix_commit_sha", 0),
            "草稿根因不在参数内": ({"draft": {"root_cause_id": "rc1-foreign"}}, {}, EXIT_OPERATOR, "不在 REPAIR_ROOT_CAUSES 内", 0),
            "收据实测日志摘要不符": (None, {}, EXIT_OPERATOR, "实测日志摘要不符", 0),
            "登记命令失败": ({}, {"repair_cli_fail": True}, EXIT_FAILED, "record-root-cause-repair 失败", 1),
            "登记后总账无修复事件": ({}, {"repair_not_in_ledger": True}, EXIT_FAILED, "登记后总账里仍没有", 1),
        }
        for name, (options, scenario, code, needle, records) in cases.items():
            with self.subTest(name=name):
                root = self._new_root()
                if options is None:
                    # 已存在的回归收据（不走草稿）引用的实测日志与摘要对不上。
                    log = _write(root / "upload" / "other-tests.log", "别的实测日志\n")
                    receipt = _write_json(root / "runroot-receipts" / "regression.json", {
                        "schema_version": "arm64-code-regression-receipt/v1", "fix_commit_sha": "4" * 40,
                        "root_cause_id": "rc1-fixture",
                        "targeted_regression": {"arm64_real_check": {"tests": {"path": str(log), "sha256": "0" * 64}}},
                    })
                    params = {"REPAIR_ROOT_CAUSES": "rc1-fixture", "REPAIR_FIX_COMMIT": "4" * 40,
                              "REGRESSION_RECEIPT": str(receipt), "REPAIR_NOTE": "测试登记"}
                else:
                    params, receipt = _repair_params(root, **options)
                fixture = _Round(root, scenario={"root_cause_limits": {"run-b": "rc1-fixture"}, **scenario}, params=params)
                result = fixture.run()
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, code, output[-4000:])
                self.assertIn("FIX_AND_CONTINUE_STOPPED step=reconcile-runs", output)
                self.assertIn(needle, output)
                self._assert_hints_resumable(fixture, output)
                keys = fixture.call_keys()
                self.assertEqual(keys.count("record-root-cause-repair"), records)
                self.assertEqual(keys.count("reconcile-supervisor-run:run-b"), 1)
                self.assertNotIn(f"reconcile-attempt:plain:{ATTEMPT}", keys)
                self.assertNotIn("vc5-recover.sh", keys)
                record = fixture.step("reconcile-runs")
                self.assertEqual(record["resume_from"], "reconcile-runs")
                self.assertEqual(record["summary"]["revisit"], [{"kind": "supervisor-run", "subject": "run-b"}])
                if name.startswith("草稿"):
                    self.assertFalse(receipt.exists(), "草稿不合格不得写回归收据")

    def test_attempt_pause_repair_step_record_format_unchanged(self) -> None:
        """目标 5：reconcile-attempt 旁路（passed needs_repair → repair 步骤登记 → approve 重新对账）行为与步骤记录格式不变，
        --list 仍每步一行可读。"""

        repair, _receipt = _repair_params(self.root)
        fixture = _Round(self.root, scenario={"root_cause_limits": {ATTEMPT: "rc1-fixture"}}, params=repair)
        self._assert_ok(fixture.run())
        keys = fixture.call_keys()
        start = keys.index(f"reconcile-attempt:plain:{ATTEMPT}")
        self.assertEqual(keys[start:start + 4], [
            f"reconcile-attempt:plain:{ATTEMPT}", "record-root-cause-repair", f"reconcile-attempt:plain:{ATTEMPT}",
            f"reconcile-attempt:approve:{ATTEMPT}",
        ])
        attempt = fixture.step("reconcile-attempt")
        self.assertEqual(set(attempt), STEP_RECORD_KEYS)
        self.assertEqual((attempt["status"], attempt["summary"]), ("passed", {
            "decision": "paused", "needs_repair": True, "pause_kinds": ["root_cause_repair"],
            "reasons": [ROOT_CAUSE_REASON_FIXTURE], "root_cause": "rc1-fixture"}))
        record = fixture.step("repair")
        self.assertEqual(set(record), STEP_RECORD_KEYS)
        self.assertEqual((record["status"], record["reason"], record["next"], record["resume_from"]), ("passed", None, None, None))
        self.assertEqual(set(record["summary"]), {"todo", "regression_receipt", "regression_receipt_sha256",
                                                  "regression_receipt_written", "deployment_receipt", "operation_id",
                                                  "receipt_sha256", "head_sequence"})
        self.assertEqual(record["summary"]["todo"], ["rc1-fixture"])
        self.assertEqual(len(list((fixture.out / "raw").glob("*-repair-repair.out"))), 1)
        self.assertNotIn("root_cause_repairs", fixture.step("reconcile-runs")["summary"])
        listing = fixture.run("--list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        lines = listing.stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], [
            "deploy", "postdeploy", "item-tests", "evolution", "pre-extend", "reconcile-runs", "reconcile-attempt",
            "repair", "approve", "authorize", "extend", "accepted", "recover",
        ])
        for line in lines:
            self.assertRegex(line, r"^[a-z-]+ +(passed|skipped)  \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_attempt_pause_without_material_hint_passes_check_from(self) -> None:
        """目标 2（reconcile-attempt 一侧）：没给材料时停在 reconcile-attempt，提示 --from 能通过 check_from，照做续跑到底。"""

        fixture = _Round(self.root, scenario={"root_cause_limits": {ATTEMPT: "rc1-fixture"}})
        stopped = fixture.run()
        output = stopped.stdout + stopped.stderr
        self.assertEqual(stopped.returncode, EXIT_OPERATOR, output)
        self.assertIn("FIX_AND_CONTINUE_STOPPED step=reconcile-attempt status=needs-operator resume_from=reconcile-attempt", output)
        self._assert_hints_resumable(fixture, output, expected="reconcile-attempt")
        repair, _receipt = _repair_params(self.root)
        fixture.write_params(repair)
        self._assert_ok(fixture.run("--from", "reconcile-attempt"))
        self.assertEqual(fixture.call_keys().count("record-root-cause-repair"), 1)
        self.assertEqual(fixture.step("repair")["status"], "passed")


if __name__ == "__main__":
    unittest.main()
