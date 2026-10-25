"""恢复计划（第 18 项）离线测试的"采集主机"夹具：假数据根里放受管工具替身，现场文件按真实 0.163 记录的形状落盘。

替身（写进假数据根 ``tools/official_client_capture/``）：

* ``codex_upgrade.py`` CLI：``tool-evolution-status``、``tool-evolution`` 预览／登记、``campaign-resume`` 预览／批准、
  ``reconcile-supervisor-run``、``reconcile-attempt``（对账／批准／授权）。账本状态、演进登记、预览序号都存在 ``state.json``，
  每次调用追加 ``calls.jsonl``。对账预览按"账本序号＋有效演进序号"取键：键不变返回同一份预览（幂等），键变了才出新序号，
  批准／授权遇到旧键报"必须重新对账"，与受管对账器同一口径。
* ``codex_upgrade_vc_artifacts.effective_deadlines`` 与 ``codex_upgrade_supervisor``：恢复计划进程内调用的只读接口。

现场文件（预览、批准收据、对账收据、campaign-resume 收据、attempt.json）字段名与取值形状取自
``sub2apiplus-evidence/codex-cli/0.163.0`` 的真实 Campaign 归档；替身只负责在对应命令执行时写出它们。

续跑脚本替身（``vc5-recover.sh``、``entry.sh``）记录调用，并按 ``dispatch.mode`` 决定立即结束或长睡；结束时按真实形状
新建后继 attempt 目录（等价真实补跑批次开始时预约新 attempt）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

SCRIPTS = driver_tests.SCRIPTS
HELPER = SCRIPTS / "recovery_plan.py"
SCRIPT = SCRIPTS / "recovery-plan.sh"
APPROVAL_SCHEMA = "arm64-fix-operation-approval/v1"
APPROVER = "测试批准人"
RESUME_SHA = "a" * 64
EVOLUTION_SHA = "e" * 64
FIX_COMMIT = "1" * 40


def write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def write_json(path: Path, payload: object, mode: int = 0o600) -> Path:
    return write(path, json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", mode)


def tree_digest(root: Path, *, skip: tuple[str, ...] = ()) -> str:
    """目录树内容摘要（路径＋字节），用来证明只读预览没有改动任何文件。"""

    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in skip):
            continue
        digest.update(relative.encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


_STUB_COMMON = r'''
"""恢复计划测试的受管工具替身公共部分（只在 RP_STUB_DIR 下读写状态，只在假数据根下写 Campaign 文件）。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STUB = Path(os.environ["RP_STUB_DIR"])
DATA = STUB.parent
RESUME_SHA = "a" * 64
EVOLUTION_SHA = "e" * 64
AUTHORIZABLE = {"recovery_required", "stage_review_required", "candidate_review_required"}


def _read(name, default):
    path = STUB / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


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


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def emit(stdout=None, rc=0, stderr=""):
    if stdout is not None:
        print(json.dumps(stdout, ensure_ascii=False, indent=2, sort_keys=True))
    if stderr:
        print(stderr, file=sys.stderr)
    return rc


def campaign_dir():
    return DATA / "evidence" / "campaigns" / scenario()["campaign"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


PAUSE_TEXT = {
    "accounting": "总账 blocked：['reconcile-attempt:x']（暂停：accounting-resolve 补账后继续）",
    "environment": "存在未隔离的环境污染（暂停：修复环境、取得干净环境复核后以 environment-isolate 隔离继续）",
    "request_budget": "项目请求预算已耗尽（暂停：批准请求预算延长后继续）",
    "deadline": "Campaign 计时预算已暂停",
}


def pause_for(st, subject):
    kinds = [kind for kind in (scenario().get("pauses") or {}).get(subject, []) if not st.get(f"{kind}_resolved")]
    if not kinds:
        return None
    return {"status": "paused", "attempt_id": subject,
            "decision": {"decision": "paused", "pause_kinds": kinds, "reasons": [PAUSE_TEXT.get(kind, kind) for kind in kinds]},
            "next_command": "处理后重新对账"}


def preview_key(st):
    return [st.get("ledger_seq", 10), st.get("effective_index", 7)]


def attempt_dir(attempt_id):
    return campaign_dir() / "control" / "reconciliation" / f"attempt-{attempt_id}"


def previews(attempt_id):
    directory = attempt_dir(attempt_id)
    return sorted(directory.glob("recovery-preview-*.json")) if directory.is_dir() else []


def latest_preview(attempt_id):
    items = previews(attempt_id)
    return (items[-1], json.loads(items[-1].read_text(encoding="utf-8"))) if items else (None, None)


def build_preview(st, attempt_id, index):
    sc = scenario()
    jobs = sc["jobs"]
    complete = list(jobs.get("complete", []))
    reuse = complete if sc.get("reuse_complete") else []
    execute = sorted(set(jobs.get("failed", [])) | set(jobs.get("indeterminate", [])) | set(jobs.get("pending", []))
                     | (set(complete) - set(reuse)))
    key = preview_key(st)
    body = {
        "campaign_id": sc["campaign"], "candidate_id": sc.get("candidate") if sc["phase"] == "candidate" else None,
        "campaign_ledger_head": {"sequence": st.get("ledger_seq", 10), "sha256": sha(["head", key]), "status": st["ledger_status"]},
        "complete_job_ids": sorted(complete), "execute_job_ids": execute, "failed_job_ids": sorted(jobs.get("failed", [])),
        "indeterminate_job_ids": sorted(jobs.get("indeterminate", [])), "pending_job_ids": sorted(jobs.get("pending", [])),
        "planned_job_ids": sorted(set(complete) | set(execute)), "created_at_utc": now(), "index": index,
        "live_request_count": 0, "phase": sc["phase"], "recovery_revision": None, "reservation_exists": False,
        "reuse_basis": "source attempt 环境已恢复，complete Job 只读复用；工具演进受影响的已完成 Job 移入执行集合",
        "reuse_job_ids": sorted(reuse), "schema_version": "recovery-preview/v1", "source_attempt_id": attempt_id,
        "tool_identity": {"files_sha256": sha(["files", key]), "policy_sha256": "p" * 64, "wire_producer_sha256": "w" * 64},
    }
    if st.get("effective_index", 7) > scenario().get("base_index", 7):
        body["tool_evolution"] = {"evolution_indexes": list(range(scenario().get("base_index", 7) + 1, st["effective_index"] + 1)),
                                  "invalidated_job_ids": [], "source_index": scenario().get("base_index", 7)}
    body["review_sha256"] = sha([body["source_attempt_id"], index, key])
    return body


def reconcile(st, attempt_id, argv):
    directory = attempt_dir(attempt_id)
    receipt = directory / "attempt-reconciliation.json"
    if not receipt.exists():
        write_json(receipt, {"attempt_id": attempt_id, "attempt_status": scenario().get("attempt_status", "failed"),
                             "campaign_ledger": {"status": st["ledger_status"], "head_sequence": st.get("ledger_seq", 10)},
                             "environment": {"status": "restored", "decision_status": "restored", "contamination_records": []}})
    paused = pause_for(st, attempt_id)
    if paused is not None:
        return emit(paused, 3)
    path, latest = latest_preview(attempt_id)
    key = preview_key(st)
    fresh = latest is not None and latest["review_sha256"] == sha([attempt_id, latest["index"], key])
    if "--approve-recovery-sha256" in argv or "--authorize-recovery-preview" in argv:
        pass
    elif not fresh:
        index = (latest["index"] if latest else 0) + 1
        path = directory / f"recovery-preview-{index:02d}.json"
        latest = build_preview(st, attempt_id, index)
        write_json(path, latest)
    payload = {"status": "recoverable", "attempt_id": attempt_id, "root_cause": {"root_cause_id": "rc1-fixture"},
               "recovery_preview": {k: latest[k] for k in ("index", "review_sha256", "execute_job_ids", "reuse_job_ids", "live_request_count")}
               if latest else None,
               "recovery_preview_path": str(path) if path else None, "resume_reuse_check": {"status": "consistent"},
               "next_command": "reconcile-attempt --approve-recovery-sha256 <review_sha256>"}
    return payload


def cli(argv):
    argv = list(argv)
    record({"argv": argv})
    st = state()
    sc = scenario()
    command = argv[0]
    if command == "tool-evolution-status":
        registered = bool(st.get("evolution_registered"))
        return emit({"status": "registered" if registered else "evolution_required", "effective_index": st.get("effective_index", 7),
                     "unregistered_drift": [] if registered else ["control:tools/official_client_capture/codex_upgrade_supervisor.py"],
                     "live_request_count": 0})
    if command == "tool-evolution":
        if "--approve-sha256" not in argv:
            return emit({"status": "approval_required", "index": st.get("effective_index", 7) + 1, "review_sha256": EVOLUTION_SHA,
                         "changes": {"wire_closure_changed": bool(sc.get("wire_changed")), "impact_paths": [], "unmapped_paths": [],
                                     "evidence_closure_changed": False},
                         "bindings": {"fix_commit": option(argv, "--fix-commit")}, "live_request_count": 0})
        if option(argv, "--approve-sha256") != EVOLUTION_SHA or not option(argv, "--approved-by"):
            return emit(None, 1, "升级审计失败：批准摘要与重算的工具演进预览不一致；重新预览后再批准。")
        st["evolution_registered"] = True
        st["effective_index"] = st.get("effective_index", 7) + 1
        save_state(st)
        return emit({"status": "evolution_applied", "path": "evolution.json", "receipt_sha256": "f" * 64, "index": st["effective_index"]})
    if command == "campaign-resume":
        if st["ledger_status"] not in ("stopped", "stop_required"):
            return emit(None, 1, f"升级审计失败：Campaign 计时账本状态为 {st['ledger_status']}，不是停线，无需 campaign-resume。")
        approve = option(argv, "--approve-sha256")
        if approve is None:
            return emit({"status": "approval_required", "review_sha256": RESUME_SHA, "root_cause_id": "rc1-fixture",
                         "next_command": "campaign-resume --approve-sha256 <review_sha256> --approved-by <批准人>"})
        if approve != RESUME_SHA or not option(argv, "--approved-by"):
            return emit(None, 1, "升级审计失败：批准摘要与重算的 campaign-resume 预览不一致；重新预览后再批准。")
        receipt = campaign_dir() / "control" / "resume" / f"resume-{RESUME_SHA}.json"
        write_json(receipt, {"approved_sha256": RESUME_SHA, "approved_by": option(argv, "--approved-by"), "approved_at_utc": now(),
                             "root_cause_id": "rc1-fixture", "preview": {"review_sha256": RESUME_SHA}})
        st["ledger_status"] = "recovery_required"
        st["ledger_seq"] = st.get("ledger_seq", 10) + 1
        save_state(st)
        return emit({"status": "resumed", "receipt_path": str(receipt), "receipt_sha256": "r" * 64, "timing_status": "recovery_required"})
    if command == "reconcile-supervisor-run":
        run_dir = Path(option(argv, "--run-dir"))
        write_json(campaign_dir() / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json",
                   {"run_id": run_dir.name})
        paused = pause_for(st, run_dir.name)
        if paused is not None:
            return emit({**paused, "run_id": run_dir.name}, 3)
        return emit({"status": "recoverable", "run_id": run_dir.name, "next_command": "重新派发同一批次"})
    if command == "reconcile-attempt":
        attempt_id = option(argv, "--attempt-id")
        approve = option(argv, "--approve-recovery-sha256")
        authorize = option(argv, "--authorize-recovery-preview")
        if authorize is not None:
            data = json.loads(Path(authorize).read_text(encoding="utf-8"))
            if sc.get("stale_authorize_once") and not st.get("stale_authorize_done"):
                st["stale_authorize_done"] = True
                st["ledger_seq"] = st.get("ledger_seq", 10) + 1
                save_state(st)
                return emit(None, 1, "升级审计失败：恢复批准消费前 Campaign 账本 head 已推进，必须重新对账")
            if data["review_sha256"] != sha([attempt_id, data["index"], preview_key(st)]):
                return emit(None, 1, "升级审计失败：恢复预览生成后工具身份已变化，必须重新对账生成新预览")
            if not (attempt_dir(attempt_id) / f"recovery-approval-{data['index']:02d}.json").exists():
                return emit(None, 1, "升级审计失败：恢复预览尚未批准")
            if st["ledger_status"] in AUTHORIZABLE:
                st["ledger_status"] = "active"
                st["ledger_seq"] = st.get("ledger_seq", 10) + 1
                save_state(st)
            return emit({"status": "authorized", "attempt_id": attempt_id, "preview_path": authorize, "review_sha256": data["review_sha256"],
                         "timing_recovery_event": {"event_id": "recovery-authorized-01"}, "live_request_count": 0,
                         "next_command": f"resume --rerun-failed --recovery-preview {authorize}"})
        payload = reconcile(st, attempt_id, argv)
        if isinstance(payload, int):
            return payload
        if approve is not None:
            path, latest = latest_preview(attempt_id)
            if latest is None or approve != latest["review_sha256"]:
                return emit(None, 1, "升级审计失败：批准摘要与任何恢复预览都不一致")
            if latest["review_sha256"] != sha([attempt_id, latest["index"], preview_key(st)]):
                return emit(None, 1, "升级审计失败：恢复预览批准后工具身份已变化，必须重新对账")
            write_json(attempt_dir(attempt_id) / f"recovery-approval-{latest['index']:02d}.json",
                       {"approved_sha256": approve, "preview_index": latest["index"], "index": latest["index"],
                        "preview_sha256": "x" * 64, "approved_at_utc": now(), "schema_version": "recovery-approval/v1"})
            payload["recovery_approval"] = {"index": latest["index"], "approved_sha256": approve, "preview_index": latest["index"]}
        return emit(payload, 0)
    return emit(None, 99, f"替身不认识的调用：{argv}")
'''

_STUB_MODULES = {
    "__init__.py": "",
    "_stub.py": _STUB_COMMON,
    "codex_upgrade.py": textwrap.dedent('''
        """恢复计划测试替身：codex_upgrade CLI。"""
        from tools.official_client_capture import _stub

        if __name__ == "__main__":
            import sys
            raise SystemExit(_stub.cli(sys.argv[1:]))
    '''),
    "codex_upgrade_vc_artifacts.py": textwrap.dedent('''
        """恢复计划测试替身：进程内只读接口 effective_deadlines。"""
        from pathlib import Path
        from tools.official_client_capture import _stub


        def effective_deadlines(campaign_dir, *, project_ledger_optional=False, **_):
            st = _stub.state()
            if st.get("ledger_error"):
                raise RuntimeError(st["ledger_error"])
            return {"status_before_pause": st["ledger_status"], "paused_scopes": [], "phase": "VC-5",
                    "stage_deadline_at_utc": "2099-01-01T00:00:00Z", "paused_hours": 0.0}
    '''),
    "codex_upgrade_supervisor.py": textwrap.dedent('''
        """恢复计划测试替身：监督器状态目录读取与对账收据核验。"""
        import json
        from pathlib import Path

        ACTIVE_STATES = {"running", "prepared"}


        def _read_state(run_dir):
            return json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))


        def _reservations_in_run_window(campaign_dir, started):
            return []


        def verify_attempt_reconciliation_binding(*args, **kwargs):
            return None


        def verify_supervisor_run_reconciliation_binding(*args, **kwargs):
            return None
    '''),
}

_STUB_DISPATCH = r'''#!/bin/bash
# 续跑脚本替身（{name}）：记录调用；dispatch.mode=sleep 时长睡（模拟仍在跑）；结束时新建后继 attempt 目录。
echo "{name} $*" >> "{world}/dispatch.log"
if [ -f "{world}/dispatch.mode" ] && [ "$(cat "{world}/dispatch.mode")" = sleep ]; then sleep 60; fi
if [ -f "{world}/dispatch.mode" ] && [ "$(cat "{world}/dispatch.mode")" = fail ]; then echo "dispatch failed"; exit 7; fi
mkdir -p "{successor_root}/20990101T000000Z-successor0000000000"
echo '{{"status": "awaiting_receipts", "results": []}}' > "{successor_root}/20990101T000000Z-successor0000000000/attempt.json"
echo "RUN_BATCH_DONE" > "{runroot}/vc5-run-batch.out"
echo "{name} done"
'''


class World:
    """一个假采集主机：数据根（受管工具替身）、驱动目标（续跑脚本替身）、驱动参数文件与恢复计划参数文件。"""

    def __init__(self, root: Path, *, phase: str = "candidate", ledger_status: str = "recovery_required",
                 evolution_registered: bool = True, jobs: dict[str, list[str]] | None = None,
                 reuse_complete: bool = False, scenario: dict[str, Any] | None = None, attempt_status: str = "failed",
                 params_extra: dict[str, str] | None = None) -> None:
        self.root = root
        self.phase = phase
        self.fixture = driver_tests._DriverFixture(root)
        self.data = self.fixture.data_root
        self.runroot = self.fixture.runroot
        self.campaign = self.fixture.new
        self.candidate = self.fixture.cand
        self.campaign_dir = self.fixture.newdir
        self.attempt = "20261021T135838Z-70aa3c8b45478aec"
        self.stub = self.data / ".stub"
        self.stub.mkdir(mode=0o700)
        (self.stub / "calls.jsonl").touch()
        self.driver_target = root / "arm64-capture-driver"
        self.state_dir = self.data / "control" / f"{self.campaign}-supervisor"
        self.state_dir.mkdir(mode=0o700)
        tools = self.data / "tools"
        write(tools / "__init__.py", "")
        for name, source in _STUB_MODULES.items():
            write(tools / "official_client_capture" / name, source)
        jobs = jobs or {"complete": ["job-done"], "failed": ["job-failed"], "pending": ["job-p1", "job-p2"]}
        base = {"campaign": self.campaign, "candidate": self.candidate, "attempt": self.attempt, "phase": phase,
                "jobs": jobs, "reuse_complete": reuse_complete, "attempt_status": attempt_status, "base_index": 7}
        base.update(scenario or {})
        write_json(self.stub / "scenario.json", base)
        write_json(self.stub / "state.json", {"ledger_status": ledger_status, "ledger_seq": 10, "effective_index": 7,
                                              "evolution_registered": evolution_registered})
        self._install_attempt(jobs, attempt_status)
        self._install_driver()
        self.regression = write_json(self.runroot / "regression-receipt.json", {"schema_version": "arm64-code-regression-receipt/v1"})
        self.params_path = self.root / "recovery-params.env"
        self.params_extra = dict(params_extra or {})
        self.write_params()

    # --- 现场 ---------------------------------------------------------------------
    def attempts_dir(self) -> Path:
        if self.phase == "official":
            return self.campaign_dir / "official" / "attempts"
        return self.campaign_dir / "candidates" / self.candidate / "attempts"

    def _install_attempt(self, jobs: dict[str, list[str]], status: str) -> None:
        directory = self.attempts_dir() / self.attempt
        results = [{"id": job, "status": state} for state, ids in jobs.items() for job in ids if state in {"complete", "failed"}]
        write_json(directory / "attempt.json", {"attempt_id": self.attempt, "phase": self.phase, "status": status, "results": results,
                                                "started_at_utc": "2026-10-21T13:58:38.000Z"})

    def add_attempt(self, name: str, status: str = "failed") -> None:
        write_json(self.attempts_dir() / name / "attempt.json", {"attempt_id": name, "phase": self.phase, "status": status, "results": []})

    def _install_driver(self) -> None:
        driver = self.driver_target / "driver"
        driver.mkdir(parents=True)
        for name in ("vc5-recover.sh", "entry.sh"):
            write(driver / name, _STUB_DISPATCH.format(name=name, world=self.root, runroot=self.runroot,
                                                       successor_root=self.attempts_dir()), 0o700)

    def set_state(self, **values: Any) -> None:
        state = json.loads((self.stub / "state.json").read_text(encoding="utf-8"))
        state.update(values)
        write_json(self.stub / "state.json", state)

    def state(self) -> dict[str, Any]:
        return json.loads((self.stub / "state.json").read_text(encoding="utf-8"))

    def set_scenario(self, **values: Any) -> None:
        data = json.loads((self.stub / "scenario.json").read_text(encoding="utf-8"))
        data.update(values)
        write_json(self.stub / "scenario.json", data)

    def add_tail_run(self, name: str, state: str, started: float) -> Path:
        directory = self.state_dir / name
        write_json(directory / "state.json", {"state": state, "campaign_id": self.campaign, "started_at_epoch": started})
        return directory

    # --- 参数与批准 -------------------------------------------------------------------
    def write_params(self, **override: str) -> Path:
        values = {
            "ROUND": "r1", "D": str(self.data), "RUNROOT": str(self.runroot), "VC_ENV": str(self.fixture.env_file),
            "VC_STATE_DIR": str(self.state_dir), "CAMPAIGN": self.campaign, "ATTEMPT": self.attempt, "CANDIDATE": self.candidate,
            "APPROVER": APPROVER, "DRIVER_TARGET": str(self.driver_target),
            "FIX_COMMIT": FIX_COMMIT, "REGRESSION_RECEIPT": str(self.regression),
            "RESUME_REASON": "失败原因已消除（测试）", "EVOLUTION_REASON": "修了受管监督器（测试）",
        }
        values.update(self.params_extra)
        values.update(override)
        text = "".join(f'{key}="{value}"\n' for key, value in values.items() if value is not None)
        write(self.params_path, text)
        return self.params_path

    def helper(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("recovery_plan_under_test", HELPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclass 需要按模块名找到类所在模块
        sys.path.insert(0, str(SCRIPTS))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(SCRIPTS))
        return module

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "RP_STUB_DIR": str(self.stub), **extra}

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(HELPER), str(self.params_path), *args], capture_output=True, text=True,
                              env=self.env(**extra_env), stdin=subprocess.DEVNULL)

    def plan(self) -> dict[str, Any]:
        result = self.run("--plan", "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def calls(self) -> list[list[str]]:
        return [json.loads(line)["argv"] for line in (self.stub / "calls.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    def write_calls(self) -> list[str]:
        """写动作的调用（计划只会因为它们改变现场）：命令名＋子动作。"""

        names = []
        for argv in self.calls():
            if argv[0] == "campaign-resume" and "--approve-sha256" in argv:
                names.append("campaign-resume:apply")
            elif argv[0] == "tool-evolution" and "--approve-sha256" in argv:
                names.append("tool-evolution:apply")
            elif argv[0] == "reconcile-attempt" and "--approve-recovery-sha256" in argv:
                names.append("reconcile-attempt:approve")
            elif argv[0] == "reconcile-attempt" and "--authorize-recovery-preview" in argv:
                names.append("reconcile-attempt:authorize")
            elif argv[0] == "reconcile-supervisor-run":
                names.append("reconcile-supervisor-run")
        return names

    def dispatches(self) -> list[str]:
        path = self.root / "dispatch.log"
        return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []

    def pre_approve(self, operation: str, subject: str, *, approver: str = APPROVER, extra_files: tuple[str, ...] = ()) -> Path:
        """人工提交一份专项批准（测试里代替"人读完批准意向后写批准文件"这一步；程序本身从不代签）。"""

        helper = self.helper()
        params = helper.load_params(self.params_path)
        keys = ("VC_ENV", *extra_files)
        key, _intent, path, binding = helper.fix_safety.approval_intent(
            params, operation, subject, operations=helper.OPERATIONS, file_keys=keys, subdir=helper.APPROVAL_SUBDIR)
        now = datetime.now(timezone.utc)
        write_json(path, {
            "schema_version": APPROVAL_SCHEMA, "status": "approved", "binding": binding, "review_sha256": key,
            "approved_by": approver, "approved_at_utc": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), "proof_ref": "unit-test"}, 0o600)
        return path

    def approve_from_intents(self) -> list[Path]:
        """把 approval-intents 里的每份意向转成批准文件（测试代人签）。"""

        out = self.runroot / "recovery-plan" / "r1" / "approval-intents"
        paths = []
        for intent in sorted(out.glob("*.json")) if out.is_dir() else []:
            data = json.loads(intent.read_text(encoding="utf-8"))
            target = Path(data["required_approval_path"])
            if target.exists():
                continue
            now = datetime.now(timezone.utc)
            write_json(target, {
                "schema_version": APPROVAL_SCHEMA, "status": "approved", "binding": data["binding"], "review_sha256": data["review_sha256"],
                "approved_by": APPROVER, "approved_at_utc": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at_utc": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), "proof_ref": "unit-test"}, 0o600)
            paths.append(target)
        return paths

    def out_dir(self) -> Path:
        return self.runroot / "recovery-plan" / "r1"

    def expected_preview_sha(self, index: int, seq: int, effective: int) -> str:
        """替身在账本序号 seq、有效演进序号 effective 时给第 index 份预览的摘要（预先签批准用）。"""

        return hashlib.sha256(json.dumps([self.attempt, index, [seq, effective]], sort_keys=True).encode()).hexdigest()

    def pre_sign_recovery(self, *, seq: int, effective: int, index: int = 1) -> None:
        """批准恢复预览＋授权（候选采集）或官方首批续跑的批准。"""

        sha = self.expected_preview_sha(index, seq, effective)
        if self.phase == "official":
            self.pre_approve("recovery-continue", sha)
        else:
            self.pre_approve("recovery-approve", sha)
            self.pre_approve("recovery-authorize", sha)

    def wait_continue_finished(self, timeout: float = 15.0) -> dict[str, Any]:
        """等续跑替身进程的结束标记写进日志（它是后台进程，主命令返回时可能还没结束）。"""

        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            plan = self.plan()
            step = next(item for item in plan["steps"] if item["id"] == "continue")
            if "已派发" in step["detail"] and "正常结束" in step["detail"] or "后继 attempt" in step["detail"]:
                return plan
            time.sleep(0.2)
        return self.plan()

    def preview_sha(self, index: int = 1) -> str:
        """替身对当前账本序号／演进序号下第 index 份预览给出的摘要（用来预先签批准）。"""

        state = self.state()
        key = [state.get("ledger_seq", 10), state.get("effective_index", 7)]
        digest = hashlib.sha256(json.dumps([self.attempt, index, key], sort_keys=True).encode()).hexdigest()
        return digest
