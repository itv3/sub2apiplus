#!/usr/bin/env python3
"""恢复链一条命令（第 18 项，``recovery-plan.sh``）：读账本与 attempt 状态，判定恢复链每一步做没做，再按序补做。

背景：一次采集失败后的恢复链此前由人逐条串联——``campaign-resume`` → 工具演进登记 → 对账 → 恢复预览 → 批准／授权 → 续跑，
0.163 的 VC-1 恢复链连做了 7 次，每次都要人工回忆"上一次做到哪了、这次从哪接"。本模块在这些**既有受管命令**之上做一个
可重复执行的恢复计划：

* **计划预览（``--plan``，缺省）**：只读。从受管账本接口、工具演进状态、Campaign 目录里的对账收据／恢复预览／批准收据、
  attempt 与后继 attempt、续跑标记判定每一步的状态（已完成／待执行／等待前序／阻塞／不适用），并明确列出
  **保留**（预览里只读复用的作业）、**重验**（需要重新校验的预览／批准／演进登记／环境隔离绑定）、
  **重采**（预览里要重新执行的作业及原因）。同一现场重复预览得到逐字相同的计划摘要 ``plan_sha256``。
* **执行（``--apply``）**：先落盘计划，再按序执行；**每一步开始前重新判定**，已完成的跳过，未完成的调用既有受管命令
  （与 ``fix-and-continue.sh`` 同一套命令行、同一套对账判定函数），执行后再判定一次确认确实推进。中途被打断（kill -9、断电）后
  再执行同一条命令：判定只看现场（账本状态、收据、预览、批准、后继 attempt、续跑标记），结果与不打断相同；唯一会产生
  真实采集请求的"续跑派发"另有不可覆盖的占位与启动记录，已派发过绝不重派。

分工与边界（与 ``fix-and-continue`` 一致，不越权）：

* 不重写任何受管逻辑：判定用受管只读接口（``effective_deadlines``、``tool-evolution-status``、监督器扫描）与磁盘收据；
  动作只调受管 CLI（``campaign-resume``、``tool-evolution``、``reconcile-supervisor-run``、``reconcile-attempt``）和既有续跑脚本
  （候选采集 ``vc5-recover.sh``；官方首批 ``entry.sh --approve-sha256``）。
* 写账本的动作（campaign-resume 批准、工具演进登记、批准恢复预览、授权、官方首批续跑）各自消费一份**人工提交的专项批准**
  （``fix_safety.require_approval``，批准文件绑定操作、对象摘要、参数与文件摘要，有效期内才认）；缺失时只写批准意向并停下，
  **从不代签**。批准预览与授权共用同一个预览摘要，停下之前把两份意向一次写全。
* 从不做：accounting-resolve、environment-isolate、request-budget-extend、deadline-extend、record-root-cause-repair、任何 ``--force``、
  重装守护。对账判这些暂停时停下，打印要执行的受管命令与续跑的同一条命令。
* 只读模式不写任何文件、不改任何账本；执行模式只写 ``$RUNROOT/recovery-plan/<轮次>/``（步骤记录、原始输出、批准意向、
  续跑占位）和受管命令自己的产物。

退出码：0 计划完成（续跑已派发／在跑／已结束）或预览成功；1 失败；2 参数或用法错误；3 本轮已有实例在跑；4 需要人工处理
（缺批准、对账暂停、阻塞）。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 驱动目录与数据根都不得出现 __pycache__（清单闭合、工具身份）。

import argparse  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402

DRV = Path(__file__).resolve().parent
if str(DRV) not in sys.path:
    sys.path.insert(0, str(DRV))
import fix_and_continue as FC  # noqa: E402  —— 复用对账判定、监督器扫描与参数校验，不重写
import fix_safety  # noqa: E402  —— 不可覆盖凭证与专项人工批准
import parse_env  # noqa: E402  —— 与驱动参数文件同一安全解析

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PLAN_SCHEMA = "arm64-recovery-plan/v1"
STEP_SCHEMA = "arm64-recovery-plan-step/v1"
LAUNCH_SCHEMA = "arm64-recovery-plan-launch/v1"

# 步骤顺序。resume 在最前（账本停线时其它步骤都被它挡住）；reconcile-runs 与 reconcile 之间没有额外约束；
# approve／authorize 对候选采集是两个独立的受管动作，对官方首批由 entry.sh 一次完成（见 PHASE_PROFILES）。
STEPS = ("resume", "evolution", "reconcile-runs", "reconcile", "approve", "authorize", "continue")
STEP_TITLES = {
    "resume": "campaign-resume（账本停线后凭修复证据恢复）",
    "evolution": "工具演进登记（tool-evolution）",
    "reconcile-runs": "对账链尾失败的父 run（reconcile-supervisor-run／reconcile-attempt）",
    "reconcile": "目标 attempt 对账并生成恢复预览（reconcile-attempt）",
    "approve": "批准恢复预览（--approve-recovery-sha256）",
    "authorize": "授权恢复预览（--authorize-recovery-preview）",
    "continue": "续跑派发",
}
DONE, PENDING, WAITING, BLOCKED, SKIPPED = "done", "pending", "waiting", "blocked", "skipped"

# 本计划各动作消费的专项批准操作名（批准文件绑定它）。
OPERATIONS = frozenset({"campaign-resume", "tool-evolution", "recovery-approve", "recovery-authorize", "recovery-continue"})
APPROVAL_SUBDIR = "recovery-plan-approvals"

# 两种 attempt 所处阶段的形状：候选采集（VC-5 补跑，续跑脚本 vc5-recover.sh，批准与授权是两个受管动作）；
# 官方首批（VC-0 收口之后的 VC-1，续跑就是 entry.sh --approve-sha256：收口模块在一次调用里批准、授权并派发恢复批次）。
PHASE_PROFILES: Mapping[str, Mapping[str, Any]] = {
    "candidate": {"label": "候选采集（VC-5）", "separate_approve_authorize": True},
    "official": {"label": "官方首批（VC-1）", "separate_approve_authorize": False},
}

STOPPED_LEDGER_STATES = frozenset({"stopped", "stop_required"})
CAMPAIGN_STOP_MARKER = FC.CAMPAIGN_ROOT_CAUSE_STOP
STALE_MARKER = "必须重新对账"
# 对账判暂停时本计划不代办的处置（与 fix-and-continue 的 PAUSE_HINTS 同一立场），续跑一律是"重新执行同一条命令"。
PAUSE_HINTS = {
    "accounting": "账务暂停：按总账未决 operation 执行 accounting-resolve preview/apply（无法核清时需人给出估计上界与来源审计文件）",
    "environment": "环境污染暂停：修复环境、取得晚于污染的干净环境复核与环境修复记录后执行 environment-isolate preview/apply",
    "request_budget": "请求预算耗尽：由人批准新预算后执行 request-budget-extend preview/apply",
    "deadline": "预算截止暂停：由人批准 deadline-extend preview/apply（项目／Campaign／阶段分别批准；阶段层加 --scope stage --phase VC-n）",
}

REQUIRED_KEYS = ("ROUND", "D", "RUNROOT", "VC_ENV", "VC_STATE_DIR", "CAMPAIGN", "ATTEMPT", "CANDIDATE", "APPROVER")
OPTIONAL_KEYS = (
    "FIX_COMMIT", "REGRESSION_RECEIPT", "RESUME_REASON", "EVOLUTION_REASON", "APPROVALS_DIR", "DRIVER_TARGET",
    "CLI_TIMEOUT_SECONDS",
)
DEFAULTS = {"DRIVER_TARGET": "/root/arm64-capture-driver", "CLI_TIMEOUT_SECONDS": "3600"}
PATH_KEYS = ("D", "RUNROOT", "VC_ENV", "VC_STATE_DIR", "APPROVALS_DIR", "DRIVER_TARGET", "REGRESSION_RECEIPT")
MAX_ITERATIONS = 24  # 执行循环上限：每轮至少推进一步，超过即判异常，防止判定与动作互相抵消。

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_OPERATOR = 0, 1, 2, 3, 4

PREVIEW_RE = re.compile(r"^recovery-preview-(\d{2,})\.json$")
APPROVAL_RE = re.compile(r"^recovery-approval-(\d{2,})\.json$")


class RecoveryPlanError(RuntimeError):
    """判定所需的前提不成立（转为失败停下）。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_json(path: Path, label: str) -> Any:
    """读 JSON；不存在返回 None，存在但不可信（符号链接、坏 JSON）失败关闭。"""

    path = Path(path)
    if path.is_symlink():
        raise RecoveryPlanError(f"{label}是符号链接，拒绝读取：{path}")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryPlanError(f"{label}不是合法 JSON：{path}：{error}") from error


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def load_params(path: Path) -> dict[str, str]:
    """安全解析恢复计划参数文件并与驱动参数文件（VC_ENV）交叉核对。失败抛 ``parse_env.EnvFileError``。

    与 fix-and-continue 的轮次参数文件同一词法层（``parse_env.parse_assignments``，绝不 source），键集合更小：只要定位
    Campaign／attempt 的坐标与批准人；修复提交、回归收据与理由只在 resume／evolution 真要执行时才必填（缺了在计划里标阻塞）。
    """

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise parse_env.EnvFileError(f"参数文件不存在或不可信：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise parse_env.EnvFileError(f"参数文件无法读取：{error}") from error
    values = parse_env.parse_assignments(text, (*REQUIRED_KEYS, *OPTIONAL_KEYS))
    missing = [key for key in REQUIRED_KEYS if key not in values]
    if missing:
        raise parse_env.EnvFileError(f"参数文件缺少键：{missing}")
    for key, value in DEFAULTS.items():
        values.setdefault(key, value)
    for key in PATH_KEYS:
        if key in values and not FC._is_clean_path(values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是规范的绝对路径（不含空白、.. 或重复分隔符）：{values[key]!r}")
    for key in ("ROUND", "CAMPAIGN", "ATTEMPT", "CANDIDATE"):
        if not FC.ID_RE.fullmatch(values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是单层安全标识")
    if "FIX_COMMIT" in values and not FC.SHA1_RE.fullmatch(values["FIX_COMMIT"]):
        raise parse_env.EnvFileError("FIX_COMMIT 必须是完整 40 位小写提交号")
    if not re.fullmatch(r"[1-9][0-9]*", values["CLI_TIMEOUT_SECONDS"]):
        raise parse_env.EnvFileError("CLI_TIMEOUT_SECONDS 必须是正整数")
    env_path = Path(values["VC_ENV"])
    if env_path.is_symlink() or not env_path.is_file():
        raise parse_env.EnvFileError(f"VC_ENV 不存在或不可信：{env_path}")
    driver_values = parse_env.parse(env_path.read_text(encoding="utf-8"))
    for key, driver_key in (("D", "D"), ("RUNROOT", "RUNROOT"), ("CAMPAIGN", "NEW"), ("CANDIDATE", "CAND")):
        if values[key] != driver_values[driver_key]:
            raise parse_env.EnvFileError(
                f"{key}={values[key]!r} 与驱动参数文件不一致（{env_path} 的 {driver_key}={driver_values[driver_key]!r}）"
            )
    client = FC._driver_client(values)
    if client != "codex":
        raise parse_env.EnvFileError(f"恢复计划目前只支持 Codex 升级（驱动参数文件的 CLIENT={client}）")
    values["OUT"] = f"{values['RUNROOT']}/recovery-plan/{values['ROUND']}"
    values["C"] = f"{values['D']}/evidence/campaigns/{values['CAMPAIGN']}"
    values["PARAMS_PATH"] = str(path.resolve())
    values["PARAMS_SHA256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return values


# ---------------------------------------------------------------------------
# 磁盘读取（只读；字段形状取自 0.163 真实 Campaign 记录）
# ---------------------------------------------------------------------------


def locate_attempt(params: Mapping[str, str]) -> tuple[str | None, Path | None]:
    """目标 attempt 在官方还是候选目录下；两处都没有返回 (None, None)，两处都有（不应出现）失败关闭。"""

    campaign = Path(params["C"])
    found = []
    for phase, root in (
        ("official", campaign / "official" / "attempts" / params["ATTEMPT"]),
        ("candidate", campaign / "candidates" / params["CANDIDATE"] / "attempts" / params["ATTEMPT"]),
    ):
        if root.is_dir() and not root.is_symlink():
            found.append((phase, root))
    if len(found) > 1:
        raise RecoveryPlanError(f"attempt {params['ATTEMPT']} 同时出现在官方与候选目录下：{[str(item[1]) for item in found]}")
    return found[0] if found else (None, None)


def sibling_attempts(params: Mapping[str, str], phase: str) -> list[str]:
    """同阶段（官方或同一候选）下全部 attempt 目录名，按名字（时间戳前缀）升序。"""

    campaign = Path(params["C"])
    base = campaign / "official" / "attempts" if phase == "official" else campaign / "candidates" / params["CANDIDATE"] / "attempts"
    if not base.is_dir():
        return []
    return sorted(item.name for item in base.iterdir() if item.is_dir() and not item.is_symlink() and FC.ID_RE.fullmatch(item.name))


def read_attempt(root: Path) -> dict[str, Any]:
    """attempt.json 的摘要：状态与各作业状态。孤儿 attempt（采集被截停、没有 attempt.json）返回 exists=False。"""

    data = _load_json(root / "attempt.json", "attempt.json")
    if not isinstance(data, dict):
        return {"exists": False, "status": None, "jobs": {}}
    jobs: dict[str, list[str]] = {}
    for item in data.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            jobs.setdefault(str(item.get("status") or "unknown"), []).append(item["id"])
    return {
        "exists": True,
        "status": data.get("status"),
        "jobs": {status: sorted(ids) for status, ids in sorted(jobs.items())},
        "started_at_utc": data.get("started_at_utc"),
        "completed_at_utc": data.get("completed_at_utc"),
    }


def _job_list(value: Any) -> list[str]:
    return sorted(str(item) for item in value) if isinstance(value, list) else []


def _preview_summary(path: Path, data: Mapping[str, Any], index: int) -> dict[str, Any]:
    evolution = data.get("tool_evolution") if isinstance(data.get("tool_evolution"), dict) else {}
    head = data.get("campaign_ledger_head") if isinstance(data.get("campaign_ledger_head"), dict) else {}
    identity = data.get("tool_identity") if isinstance(data.get("tool_identity"), dict) else {}
    return {
        "index": index,
        "path": str(path),
        "review_sha256": str(data.get("review_sha256") or ""),
        "created_at_utc": data.get("created_at_utc"),
        "phase": data.get("phase"),
        "execute_job_ids": _job_list(data.get("execute_job_ids")),
        "reuse_job_ids": _job_list(data.get("reuse_job_ids")),
        "complete_job_ids": _job_list(data.get("complete_job_ids")),
        "failed_job_ids": _job_list(data.get("failed_job_ids")),
        "indeterminate_job_ids": _job_list(data.get("indeterminate_job_ids")),
        "pending_job_ids": _job_list(data.get("pending_job_ids")),
        "evolution_indexes": sorted(int(i) for i in evolution.get("evolution_indexes") or [] if isinstance(i, int)),
        "evolution_invalidated_job_ids": _job_list(evolution.get("invalidated_job_ids")),
        "reuse_basis": data.get("reuse_basis"),
        "ledger_head_sequence": head.get("sequence"),
        "ledger_head_status": head.get("status"),
        "tool_files_sha256": identity.get("files_sha256"),
        "recovery_revision": data.get("recovery_revision"),
    }


def read_recovery_records(params: Mapping[str, str]) -> dict[str, Any]:
    """目标 attempt 的对账目录：对账收据、全部恢复预览与批准收据（形状取自真实 0.163 记录）。"""

    directory = Path(params["C"]) / "control" / "reconciliation" / f"attempt-{params['ATTEMPT']}"
    result: dict[str, Any] = {"directory": str(directory), "receipt": None, "previews": [], "approvals": []}
    if not directory.is_dir() or directory.is_symlink():
        return result
    receipt = _load_json(directory / "attempt-reconciliation.json", "对账收据")
    if isinstance(receipt, dict):
        environment = receipt.get("environment") if isinstance(receipt.get("environment"), dict) else {}
        ledger = receipt.get("campaign_ledger") if isinstance(receipt.get("campaign_ledger"), dict) else {}
        result["receipt"] = {
            "attempt_status": receipt.get("attempt_status"),
            "ledger_status": ledger.get("status"),
            "ledger_head_sequence": ledger.get("head_sequence"),
            "recovery_root_cause_id": ledger.get("recovery_root_cause_id"),
            "environment_status": environment.get("status"),
            "environment_decision": environment.get("decision_status"),
            "contamination_records": len(environment.get("contamination_records") or []),
        }
    for item in sorted(directory.iterdir()):
        match = PREVIEW_RE.match(item.name)
        if match:
            data = _load_json(item, "恢复预览")
            if isinstance(data, dict):
                result["previews"].append(_preview_summary(item, data, int(match.group(1))))
            continue
        match = APPROVAL_RE.match(item.name)
        if match:
            data = _load_json(item, "恢复批准收据")
            if isinstance(data, dict):
                result["approvals"].append({
                    "index": int(match.group(1)),
                    "preview_index": data.get("preview_index"),
                    "approved_sha256": data.get("approved_sha256"),
                    "approved_at_utc": data.get("approved_at_utc"),
                })
    result["previews"].sort(key=lambda row: row["index"])
    result["approvals"].sort(key=lambda row: row["index"])
    return result


def read_resume_receipts(params: Mapping[str, str]) -> list[dict[str, Any]]:
    directory = Path(params["C"]) / "control" / "resume"
    rows: list[dict[str, Any]] = []
    if not directory.is_dir() or directory.is_symlink():
        return rows
    for item in sorted(directory.glob("resume-*.json")):
        data = _load_json(item, "campaign-resume 批准收据")
        if isinstance(data, dict):
            rows.append({"approved_sha256": data.get("approved_sha256"), "approved_by": data.get("approved_by"),
                         "approved_at_utc": data.get("approved_at_utc"), "root_cause_id": data.get("root_cause_id")})
    return rows


def read_isolations(params: Mapping[str, str]) -> list[dict[str, Any]]:
    """环境隔离收据里涉及目标 attempt 的条目（作废的 attempt 不再是可复用来源）。"""

    directory = Path(params["C"]) / "control" / "environment"
    rows: list[dict[str, Any]] = []
    if not directory.is_dir() or directory.is_symlink():
        return rows
    for item in sorted(directory.glob("isolation-*.json")):
        data = _load_json(item, "环境隔离收据")
        if not isinstance(data, dict):
            continue
        for entry in data.get("invalidated_attempts") or []:
            if isinstance(entry, dict) and entry.get("attempt_id") == params["ATTEMPT"]:
                rows.append({"index": data.get("index"), "path": item.name})
    return rows


# ---------------------------------------------------------------------------
# 后端：受管工具（CLI 与受管只读接口）。测试用同一份代码跑在假数据根的受管工具替身上。
# ---------------------------------------------------------------------------


@dataclass
class CliResult:
    rc: int
    payload: Any
    stdout: str
    stderr: str
    raw_prefix: str | None = None


class ManagedBackend:
    """数据根里的受管工具：CLI（与 fix-and-continue.sh 的 ``managed()`` 同一命令行）与只读接口。"""

    def __init__(self, params: Mapping[str, str], *, raw_dir: Path | None) -> None:
        self.params = params
        self.raw_dir = raw_dir  # 只读预览为 None：原始输出不落盘
        self.stamp = time.strftime("%Y%m%dt%H%M%Sz", time.gmtime()) + f"-{os.getpid()}"
        self._sequence = 0
        row = FC._client_row(params)
        self.orchestrator = FC._short_module(row["orchestrator_module"])
        self.row = row

    # --- CLI ----------------------------------------------------------------
    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        data = self.params["D"]
        env["PYTHONPATH"] = data
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        parent = str(Path(data).parent)
        env["CODEX_UPGRADE_IDENTITY_MEMO"] = f"{parent}/identity-memo"
        if Path(parent, "pycache-managed").is_dir():
            env["PYTHONPYCACHEPREFIX"] = f"{parent}/pycache-managed"
        return env

    def cli(self, step: str, label: str, args: list[str]) -> CliResult:
        self._sequence += 1
        command = [sys.executable, "-B", "-m", f"tools.official_client_capture.{self.orchestrator}", *args]
        try:
            done = subprocess.run(command, cwd=self.params["D"], env=self.environment(), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, errors="replace",
                                  timeout=int(self.params["CLI_TIMEOUT_SECONDS"]))
            rc, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as error:
            rc, stdout = 124, (error.stdout or b"").decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = f"受管命令超过 {self.params['CLI_TIMEOUT_SECONDS']} 秒仍未结束，已终止"
        try:
            payload = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            payload = None
        prefix = None
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            prefix = str(self.raw_dir / f"{self.stamp}-{self._sequence:02d}-{step}-{label}")
            Path(prefix + ".out").write_text(stdout, encoding="utf-8")
            Path(prefix + ".err").write_text(stderr, encoding="utf-8")
            Path(prefix + ".rc").write_text(f"{rc}\n", encoding="utf-8")
            for suffix in (".out", ".err", ".rc"):
                os.chmod(prefix + suffix, 0o600)
        return CliResult(rc, payload, stdout, stderr, prefix)

    # --- 只读接口 -------------------------------------------------------------
    def ledger(self) -> dict[str, Any]:
        """计时账本的有效状态（受管 ``effective_deadlines``，修好接着跑的 accepted／extend 步骤同一接口）。"""

        try:
            artifacts = FC._managed_from_table(self.params, "vc_artifacts_module")
            value = dict(artifacts.effective_deadlines(Path(self.params["C"]), project_ledger_optional=True))
        except Exception as error:  # 受管校验失败即失败关闭，原因进计划
            return {"error": f"{type(error).__name__}: {error}"}
        return {
            "status": value.get("status_before_pause"),
            "paused_scopes": sorted(value.get("paused_scopes") or []),
            "phase": value.get("phase"),
            "stage_deadline_at_utc": value.get("stage_deadline_at_utc"),
        }

    def evolution(self) -> dict[str, Any]:
        result = self.cli("evolution", "status", ["tool-evolution-status", "--campaign-dir", self.params["C"]])
        payload = result.payload
        if result.rc != 0 or not isinstance(payload, dict):
            return {"error": f"tool-evolution-status 失败（rc={result.rc}）：{FC._tail(result.stderr)}"}
        drift = payload.get("unregistered_drift") or []
        return {
            "status": payload.get("status"),
            "registered": payload.get("status") == "registered" and not drift,
            "effective_index": payload.get("effective_index"),
            "unregistered_drift": drift,
        }

    def tail(self, *, exclude: tuple[str, ...] = (), revisit: tuple[Mapping[str, str], ...] = ()) -> dict[str, Any]:
        try:
            supervisor = FC._managed_from_table(self.params, "supervisor_module")
            return FC.scan_reconciliation_targets(
                Path(self.params["C"]), Path(self.params["VC_STATE_DIR"]), campaign_id=self.params["CAMPAIGN"],
                target_attempt=self.params["ATTEMPT"], supervisor=supervisor, exclude_runs=exclude, revisit=revisit)
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}

    def active_runs(self) -> list[str]:
        return FC.active_runs(Path(self.params["VC_STATE_DIR"]))


# ---------------------------------------------------------------------------
# 现场事实（一次只读采集）与步骤判定（纯函数）
# ---------------------------------------------------------------------------


@dataclass
class Step:
    id: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    command: str = ""

    @property
    def title(self) -> str:
        return STEP_TITLES[self.id]

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail,
                "evidence": {k: v for k, v in self.evidence.items() if not k.startswith("_")}, "command": self.command}


def _launch_dir(params: Mapping[str, str]) -> Path:
    return Path(params["OUT"]) / "continue"


def _process_alive(pid: Any) -> bool:
    return isinstance(pid, int) and FC._alive(pid)


def _tail_lines(path: Path, count: int = 3) -> list[str]:
    """只读文件尾部（续跑日志可能很大）：最后 ``count`` 行。"""

    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-count:]


def launch_records(params: Mapping[str, str]) -> list[dict[str, Any]]:
    """本轮对目标 attempt 做过的全部续跑派发（占位＋启动记录），按占位文件时间升序。

    派发记录按预览摘要命名，但"这个 attempt 已经派发过"与派发的是哪份预览无关：对账之后又出了新预览，也不能再派一次。
    """

    directory = _launch_dir(params)
    rows: list[dict[str, Any]] = []
    if not directory.is_dir() or directory.is_symlink():
        return rows
    for inflight_path in sorted(directory.glob("*.inflight.json"), key=lambda item: (item.stat().st_mtime, item.name)):
        inflight = _load_json(inflight_path, "续跑占位")
        if not isinstance(inflight, dict) or inflight.get("attempt") != params["ATTEMPT"]:
            continue
        review = inflight_path.name[: -len(".inflight.json")]
        started = _load_json(directory / f"{review}.started.json", "续跑启动记录")
        rows.append({"review": review, "inflight": inflight, "started": started if isinstance(started, dict) else None})
    return rows


def continuation_state(params: Mapping[str, str], siblings: list[str]) -> dict[str, Any]:
    """续跑派发的现场状态（不依赖任何内存状态）。

    判定顺序：本计划自己的派发占位／启动记录（取最近一次）→ 后继 attempt 是否已存在（别的途径或上一轮已派发过）。
    占位存在但没有启动记录、或启动后进程消失又没有结束标记，都是"结果不确定"，一律不自动重派（与 fix-and-continue 的
    recover.inflight.json 同一立场）。
    """

    state: dict[str, Any] = {"state": "none"}
    successors = [name for name in siblings if name > params["ATTEMPT"]]
    if successors:
        state["successors"] = successors
    records = launch_records(params)
    if records:
        last = records[-1]
        state.update(state="inflight", dispatched_review=last["review"], dispatches=len(records))
        started = last["started"]
        if started is not None:
            log = Path(str(started.get("log") or ""))
            exit_line = next((line for line in reversed(_tail_lines(log)) if line.startswith("exit=")), None)
            pid = started.get("pid")
            if exit_line is not None:
                code = exit_line.split("=", 1)[1].strip()
                state.update(state="finished-ok" if code == "0" else "finished-failed", exit_code=code, log=str(log), pid=pid)
            elif _process_alive(pid):
                state.update(state="running", pid=pid, log=str(log))
            else:
                state.update(state="lost", pid=pid, log=str(log))
    if state["state"] == "none" and successors:
        state["state"] = "successor-exists"
    return state


def prior_revisit(params: Mapping[str, str]) -> list[dict[str, str]]:
    """上一次 reconcile-runs 停下时判暂停的对象：受管对账器先写收据再判定，暂停对象的收据按监督器判据核验是通过的，
    不能据此当作已对账跳过（与 fix-and-continue 的 revisit 同一原因），续跑时重新对账。"""

    path = Path(params["OUT"]) / "steps" / "reconcile-runs.json"
    data = _load_json(path, "reconcile-runs 步骤记录") if path.exists() else None
    items = ((data or {}).get("summary") or {}).get("revisit") if isinstance(data, dict) else None
    keys: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("kind") in FC.REVISIT_KINDS and isinstance(item.get("subject"), str):
            keys.append({"kind": str(item["kind"]), "subject": str(item["subject"])})
    return keys


def gather_facts(params: Mapping[str, str], backend: ManagedBackend, *, force_reconcile: bool = False,
                 revisit: tuple[Mapping[str, str], ...] = (), exclude_runs: tuple[str, ...] = ()) -> dict[str, Any]:
    """一次只读采集：账本、演进、链尾父 run、目标 attempt 与对账目录、续跑状态。"""

    phase, root = locate_attempt(params)
    facts: dict[str, Any] = {
        "phase": phase,
        "ledger": backend.ledger(),
        "evolution": backend.evolution(),
        "tail": backend.tail(exclude=exclude_runs, revisit=revisit),
        "attempt": read_attempt(root) if root is not None else {"exists": False, "status": None, "jobs": {}},
        "attempt_dir": str(root) if root is not None else None,
        "records": read_recovery_records(params),
        "resume_receipts": read_resume_receipts(params),
        "isolations": read_isolations(params),
        "active_runs": backend.active_runs(),
        "force_reconcile": force_reconcile,
    }
    siblings = sibling_attempts(params, phase) if phase else []
    facts["continuation"] = continuation_state(params, siblings)
    facts["siblings"] = siblings
    return facts


def current_preview(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    previews = facts["records"]["previews"]
    return previews[-1] if previews else None


def approval_for(facts: Mapping[str, Any], preview: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if preview is None:
        return None
    matches = [row for row in facts["records"]["approvals"] if row.get("approved_sha256") == preview["review_sha256"]]
    return matches[-1] if matches else None


def _missing_inputs(params: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    return [key for key in keys if not params.get(key)]


def derive_steps(params: Mapping[str, str], facts: Mapping[str, Any]) -> list[Step]:
    """从现场事实推出每一步的状态（纯函数：同一份事实得到同一份结果）。"""

    phase = facts["phase"]
    profile = PHASE_PROFILES.get(phase or "", PHASE_PROFILES["candidate"])
    ledger, evolution, tail, attempt = facts["ledger"], facts["evolution"], facts["tail"], facts["attempt"]
    preview = current_preview(facts)
    approval = approval_for(facts, preview)
    cont = facts["continuation"]
    steps: dict[str, Step] = {}

    # --- resume ------------------------------------------------------------
    if "error" in ledger:
        steps["resume"] = Step("resume", BLOCKED, f"无法读取计时账本：{ledger['error']}")
    elif ledger["status"] in STOPPED_LEDGER_STATES:
        missing = _missing_inputs(params, ("FIX_COMMIT", "REGRESSION_RECEIPT", "RESUME_REASON"))
        if missing:
            steps["resume"] = Step("resume", BLOCKED, f"账本状态 {ledger['status']}（停线），需要 campaign-resume，但参数文件缺 {missing}",
                                   {"ledger_status": ledger["status"]})
        else:
            steps["resume"] = Step("resume", PENDING, f"账本状态 {ledger['status']}（停线）：凭修复提交与回归收据恢复",
                                   {"ledger_status": ledger["status"], "resume_receipts": len(facts["resume_receipts"])},
                                   "campaign-resume 预览 → 专项批准 → --approve-sha256")
    else:
        steps["resume"] = Step("resume", DONE, f"账本状态 {ledger['status']}，不是停线",
                               {"ledger_status": ledger["status"], "resume_receipts": len(facts["resume_receipts"])})

    # --- evolution ---------------------------------------------------------
    if "error" in evolution:
        steps["evolution"] = Step("evolution", BLOCKED, evolution["error"])
    elif evolution["registered"]:
        steps["evolution"] = Step("evolution", DONE, f"受管工具已登记（effective_index={evolution['effective_index']}），无未登记漂移",
                                  {"effective_index": evolution["effective_index"]})
    else:
        missing = _missing_inputs(params, ("FIX_COMMIT", "EVOLUTION_REASON"))
        if missing:
            steps["evolution"] = Step("evolution", BLOCKED, f"有未登记的工具漂移，需要 tool-evolution，但参数文件缺 {missing}",
                                      {"unregistered_drift": evolution["unregistered_drift"]})
        else:
            steps["evolution"] = Step("evolution", PENDING, "受管工具有未登记漂移：预览 → 专项批准 → 登记",
                                      {"unregistered_drift": evolution["unregistered_drift"],
                                       "effective_index": evolution["effective_index"]},
                                      "tool-evolution 预览 → 专项批准 → --approve-sha256")

    # --- reconcile-runs ----------------------------------------------------
    if "error" in tail:
        steps["reconcile-runs"] = Step("reconcile-runs", BLOCKED, f"无法扫描监督器状态目录：{tail['error']}")
    elif tail.get("problems") or tail.get("unsupported"):
        steps["reconcile-runs"] = Step("reconcile-runs", BLOCKED, "链尾父 run 有无法自动处理的情形",
                                       {"problems": tail.get("problems"), "unsupported": tail.get("unsupported")})
    elif facts["active_runs"]:
        steps["reconcile-runs"] = Step("reconcile-runs", BLOCKED, f"有运行中的父 run：{facts['active_runs']}（等其结束）",
                                       {"active_runs": facts["active_runs"]})
    elif tail.get("pending"):
        steps["reconcile-runs"] = Step("reconcile-runs", PENDING, f"链尾有 {len(tail['pending'])} 个失败父 run／attempt 缺对账",
                                       {"pending": [item.get("run_id") or item.get("attempt_id") for item in tail["pending"]]},
                                       "reconcile-supervisor-run／reconcile-attempt")
    else:
        steps["reconcile-runs"] = Step("reconcile-runs", DONE, "链尾失败的父 run 都已对账（或没有）",
                                       {"tail": len(tail.get("tail") or []), "reconciled": len(tail.get("reconciled") or [])})

    # --- reconcile（目标 attempt）---------------------------------------------
    if phase is None:
        steps["reconcile"] = Step("reconcile", BLOCKED, f"找不到目标 attempt {params['ATTEMPT']}（官方与候选目录下都没有）")
    elif attempt.get("status") == "complete":
        steps["reconcile"] = Step("reconcile", SKIPPED, "目标 attempt 已 complete，无需恢复", {"attempt_status": "complete"})
    elif preview is None or facts["force_reconcile"]:
        why = "预览被判过期，需重新对账" if facts["force_reconcile"] else "尚无恢复预览"
        steps["reconcile"] = Step("reconcile", PENDING, f"{why}：对账目标 attempt 并生成恢复预览",
                                  {"attempt_status": attempt.get("status"), "receipt": facts["records"]["receipt"]},
                                  "reconcile-attempt（零请求，幂等）")
    else:
        steps["reconcile"] = Step("reconcile", DONE, f"已有恢复预览 #{preview['index']}（摘要 {preview['review_sha256'][:16]}…）",
                                  {"preview_index": preview["index"], "review_sha256": preview["review_sha256"]})

    # --- approve / authorize / continue ----------------------------------------
    separate = bool(profile["separate_approve_authorize"])
    if steps["reconcile"].status == SKIPPED:
        for name in ("approve", "authorize", "continue"):
            steps[name] = Step(name, SKIPPED, "目标 attempt 已 complete，无需恢复")
    else:
        if not separate:
            steps["approve"] = Step("approve", SKIPPED, "官方首批：批准由 entry.sh 的收口模块在续跑一次调用里完成")
            steps["authorize"] = Step("authorize", SKIPPED, "官方首批：授权由 entry.sh 的收口模块在续跑一次调用里完成")
        else:
            if preview is None:
                steps["approve"] = Step("approve", WAITING, "等恢复预览")
            elif approval is not None:
                steps["approve"] = Step("approve", DONE, f"预览 #{preview['index']} 已批准（批准收据 #{approval['index']}）",
                                        {"approval_index": approval["index"]})
            else:
                steps["approve"] = Step("approve", PENDING, f"预览 #{preview['index']} 待批准", {"review_sha256": preview["review_sha256"]},
                                        "reconcile-attempt --approve-recovery-sha256")
            ledger_status = ledger.get("status")
            if approval is None:
                steps["authorize"] = Step("authorize", WAITING, "等恢复预览批准")
            elif ledger_status in FC.AUTHORIZABLE_LEDGER_STATES:
                steps["authorize"] = Step("authorize", PENDING, f"账本 {ledger_status}：授权已批准的预览",
                                          {"ledger_status": ledger_status}, "reconcile-attempt --authorize-recovery-preview")
            else:
                steps["authorize"] = Step("authorize", DONE, f"账本 {ledger_status}，授权已生效", {"ledger_status": ledger_status})
        state = cont["state"]
        if state in {"running", "finished-ok"}:
            steps["continue"] = Step("continue", DONE, "续跑已派发" + ("（仍在运行）" if state == "running" else "并正常结束"),
                                     {"state": state, "_pid": cont.get("pid")})
        elif state == "successor-exists":
            steps["continue"] = Step("continue", DONE, f"目标 attempt 之后已有后继 attempt {cont['successors']}：续跑已由别处派发，不再派发",
                                     {"state": state, "successors": cont["successors"]})
        elif state in {"inflight", "lost", "finished-failed"}:
            reasons = {"inflight": "派发占位存在但没有启动记录（执行结果不确定）",
                       "lost": "续跑进程已消失且没有结束标记（被杀？）",
                       "finished-failed": f"续跑进程以 {cont.get('exit_code')} 结束"}
            steps["continue"] = Step("continue", BLOCKED, reasons[state] + "：禁止自动重派，人工核对后换新轮次", {"state": state})
        elif preview is None or (separate and steps["authorize"].status != DONE) or steps["reconcile"].status != DONE:
            steps["continue"] = Step("continue", WAITING, "等前序步骤")
        else:
            steps["continue"] = Step("continue", PENDING, f"按预览 #{preview['index']} 派发续跑（{profile['label']}）",
                                     {"review_sha256": preview["review_sha256"]}, _launch_description(params, phase, preview))

    ordered = [steps[name] for name in STEPS]
    # 第一个未完成步骤之后的"待执行"降为"等待前序"（它们此刻不可单独执行；执行时逐步重新判定）。
    gate_seen = False
    for step in ordered:
        if gate_seen and step.status == PENDING:
            step.status = WAITING
        if step.status in {PENDING, BLOCKED}:
            gate_seen = True
    return ordered


def objects_view(params: Mapping[str, str], facts: Mapping[str, Any]) -> dict[str, Any]:
    """保留／重验／重采：预览有就以预览为准；没有预览时只列 attempt 当前各作业状态，最终清单待对账预览产出。"""

    preview = current_preview(facts)
    approval = approval_for(facts, preview)
    evolution, ledger = facts["evolution"], facts["ledger"]
    reverify: list[dict[str, Any]] = []
    if preview is None:
        reverify.append({"object": "恢复预览", "state": "missing", "why": "尚无预览，执行 reconcile 步骤后产出"})
    else:
        indexes = preview["evolution_indexes"]
        effective = evolution.get("effective_index")
        if indexes and isinstance(effective, int):
            state, why = (("valid", f"预览基于演进 #{max(indexes)}，当前有效演进 #{effective}") if max(indexes) == effective
                          else ("stale", f"预览基于演进 #{max(indexes)}，当前有效演进 #{effective}：需重新对账"))
        else:
            state, why = "unknown", "预览不含演进信息或当前演进序号不可读；执行时由受管对账确认"
        reverify.append({"object": f"恢复预览 #{preview['index']}", "state": state, "why": why})
        if bool(PHASE_PROFILES.get(facts["phase"] or "", PHASE_PROFILES["candidate"])["separate_approve_authorize"]):
            reverify.append({"object": f"恢复批准 #{approval['index']}" if approval else "恢复批准",
                             "state": "valid" if approval else "missing",
                             "why": "批准收据绑定预览摘要" if approval else "尚未批准"})
    if "error" not in evolution:
        reverify.append({"object": "工具演进登记", "state": "valid" if evolution["registered"] else "stale",
                         "why": "无未登记漂移" if evolution["registered"] else f"未登记漂移 {len(evolution['unregistered_drift'])} 项"})
    if "error" not in ledger:
        reverify.append({"object": "计时账本停线状态", "state": "stale" if ledger["status"] in STOPPED_LEDGER_STATES else "valid",
                         "why": f"账本 {ledger['status']}"})
    receipt = facts["records"]["receipt"]
    if receipt is not None:
        isolated = bool(facts["isolations"])
        unrestored = receipt.get("environment_status") not in (None, "restored")
        reverify.append({"object": "环境连续性（before／after 探针）",
                         "state": "stale" if unrestored and not isolated else "valid",
                         "why": f"对账收据环境状态 {receipt.get('environment_status')}，" + ("已隔离作废" if isolated else "未隔离")})
    keep: list[str] = []
    recollect: list[dict[str, str]] = []
    source = "unknown"
    if preview is not None:
        source = "preview"
        keep = list(preview["reuse_job_ids"])
        reasons = _recollect_reasons(preview)
        recollect = [{"job": job, "reason": reasons.get(job, "预览执行集合")} for job in preview["execute_job_ids"]]
    elif facts["attempt"].get("exists"):
        source = "attempt"
        jobs = facts["attempt"]["jobs"]
        keep = list(jobs.get("complete", []))
        recollect = [{"job": job, "reason": f"attempt 状态 {status}"} for status, ids in jobs.items() if status != "complete" for job in ids]
    return {"source": source, "keep": keep, "reverify": reverify, "recollect": recollect}


def _recollect_reasons(preview: Mapping[str, Any]) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for key, text in (("pending_job_ids", "尚未执行"), ("indeterminate_job_ids", "结果不确定"),
                      ("failed_job_ids", "已失败"), ("evolution_invalidated_job_ids", "被工具演进作废")):
        for job in preview[key]:
            reasons[job] = text
    for job in preview["execute_job_ids"]:
        if job not in reasons:
            basis = preview.get("reuse_basis") or "预览执行集合"
            reasons[job] = f"已完成但不可复用（{basis}）" if job in preview["complete_job_ids"] else str(basis)
    return reasons


def _launch_description(params: Mapping[str, str], phase: str | None, preview: Mapping[str, Any]) -> str:
    if phase == "official":
        return f"{params['DRIVER_TARGET']}/driver/entry.sh --approve-sha256 {preview['review_sha256']} --approved-by <批准人>"
    return f"{params['DRIVER_TARGET']}/driver/{FC._client_row(params)['vc5_recover_script']} {preview['path']}"


def build_plan(params: Mapping[str, str], facts: Mapping[str, Any]) -> dict[str, Any]:
    """计划对象。``plan_sha256`` 只覆盖同一现场必然相同的字段（不含时间戳、pid、原始输出路径）。"""

    steps = derive_steps(params, facts)
    objects = objects_view(params, facts)
    statuses = [step.status for step in steps]
    if any(status == BLOCKED for status in statuses):
        overall = "blocked"
    elif all(status in {DONE, SKIPPED} for status in statuses):
        overall = "complete"
    else:
        overall = "pending"
    nxt = next((step for step in steps if step.status in {PENDING, BLOCKED}), None)
    warnings = []
    by_id = {step.id: step for step in steps}
    if facts["records"]["receipt"] is None and by_id["evolution"].status != DONE and facts["attempt"].get("exists"):
        warnings.append("目标 attempt 还没有对账收据，而受管工具已有未登记漂移（修复已部署）：同根因若已达上限，限额事件的时间以第一次对账为准，"
                        "晚于部署会让 campaign-resume 因“部署收据早于停线”被拒；以后失败后先执行本计划（先对账入账）再部署修复")
    core = {
        "schema_version": PLAN_SCHEMA,
        "round": params["ROUND"],
        "campaign": params["CAMPAIGN"],
        "attempt": params["ATTEMPT"],
        "candidate": params["CANDIDATE"],
        "phase": facts["phase"],
        "attempt_status": facts["attempt"].get("status"),
        "ledger_status": facts["ledger"].get("status"),
        "overall": overall,
        "steps": [step.public() for step in steps],
        "objects": objects,
        "warnings": warnings,
        "next": None if nxt is None else {"step": nxt.id, "status": nxt.status, "detail": nxt.detail},
    }
    return {**core, "plan_sha256": _digest(core)}


def render_plan(plan: Mapping[str, Any], *, mode: str) -> str:
    phase = PHASE_PROFILES.get(plan["phase"] or "", {}).get("label", plan["phase"] or "未定位")
    lines = [
        f"==== 恢复计划（{mode}）：轮次 {plan['round']}  Campaign {plan['campaign']}",
        f"目标 attempt {plan['attempt']}（{phase}，状态 {plan['attempt_status']}）  账本 {plan['ledger_status']}  总体 {plan['overall']}",
        "步骤：",
    ]
    marks = {DONE: "已完成", PENDING: "待执行", WAITING: "等前序", BLOCKED: "阻塞", SKIPPED: "不适用"}
    for index, step in enumerate(plan["steps"], 1):
        lines.append(f"  {index}. {step['id']:<15s}[{marks[step['status']]}] {step['detail']}")
        if step["status"] in {PENDING, WAITING} and step["command"]:
            lines.append(f"       命令：{step['command']}")
    objects = plan["objects"]
    source = {"preview": "恢复预览", "attempt": "attempt 当前状态（尚无预览，最终清单待对账）", "unknown": "尚无依据"}[objects["source"]]
    lines.append(f"保留（{len(objects['keep'])}，来源：{source}）：" + ("、".join(objects["keep"]) or "无"))
    lines.append(f"重验（{len(objects['reverify'])}）：")
    for row in objects["reverify"]:
        lines.append(f"  - {row['object']}：{row['state']}（{row['why']}）")
    lines.append(f"重采（{len(objects['recollect'])}）：")
    for row in objects["recollect"]:
        lines.append(f"  - {row['job']}：{row['reason']}")
    for warning in plan["warnings"]:
        lines.append(f"警告：{warning}")
    following = "无，计划已完成"
    if plan["next"]:
        following = f"{plan['next']['step']}（{plan['next']['status']}）：{plan['next']['detail']}"
    lines.append(f"下一步：{following}")
    lines.append(f"plan_sha256={plan['plan_sha256']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def judge(result: CliResult, step: str) -> tuple[str, str, str]:
    """对账结果归类（受管输出的判定直接用 ``fix_and_continue.judge_reconciliation``），提示换成本计划的续跑口径。"""

    category, reason, _hint, _resume = FC.judge_reconciliation(result.payload, result.rc, result.stderr, step=step)
    payload = result.payload
    upstream = str(payload.get("next_command") or "") if isinstance(payload, dict) else ""
    suffix = f"；受管工具提示：{upstream}" if upstream else ""
    if category == "paused":
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        reasons = "；".join(str(item) for item in decision.get("reasons") or [])
        actions = []
        for kind in FC.pause_kinds(payload):
            if kind == "root_cause_repair":
                actions.append(
                    "Campaign 账本同根因重试达上限：在参数文件填 FIX_COMMIT／REGRESSION_RECEIPT／RESUME_REASON 后重新执行（resume 步骤处理）"
                    if CAMPAIGN_STOP_MARKER in reasons else
                    "项目总账根因重试达上限：由人执行 record-root-cause-repair（修复提交、回归收据、部署收据三项绑定）")
            else:
                actions.append(PAUSE_HINTS.get(kind, f"暂停种类 {kind}：按受管工具提示处理"))
        return category, reason, "；".join(actions) + "；处理后重新执行同一条命令" + suffix
    if category == "stop":
        return category, reason, "永久停线或需审核由人裁定" + suffix
    return category, reason, "按报错修复后重新执行同一条命令" + suffix


class Stop(Exception):
    """执行停下：status 为 failed／needs-operator。"""

    def __init__(self, status: str, reason: str, next_hint: str, *, resume: str | None = None) -> None:
        super().__init__(reason)
        self.status, self.reason, self.next_hint, self.resume = status, reason, next_hint, resume


class Runner:
    """执行模式：持有参数、后端、运行状态；每一步动作返回 summary 或抛 Stop。"""

    def __init__(self, params: dict[str, str], backend: ManagedBackend) -> None:
        self.params = params
        self.backend = backend
        self.out = Path(params["OUT"])
        self.force_reconcile = False
        self.rewinds = 0
        self.revisit: list[dict[str, str]] = prior_revisit(params)
        self.exclude_runs: list[str] = []
        self.crash_after = os.environ.get("RECOVERY_PLAN_CRASH_AFTER")  # 只供验收注入打断，默认空

    # --- 步骤记录 ---------------------------------------------------------------
    def record(self, step: str, status: str, *, reason: str | None = None, next_hint: str | None = None,
               summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "schema_version": STEP_SCHEMA, "round": self.params["ROUND"], "step": step, "status": status,
            "params_sha256": self.params["PARAMS_SHA256"], "recorded_at_utc": _utc_now(),
            "reason": reason, "next": next_hint, "summary": dict(summary or {}),
        }
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        fix_safety.write_once(self.out / "receipts" / (fix_safety.digest(record) + ".json"), record)
        FC._write_private_json(self.out / "steps" / f"{step}.json", record)
        with (self.out / "history.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(self.out / "history.ndjson", 0o600)
        return record

    def maybe_crash(self, step: str) -> None:
        """验收用打断注入：动作做完、步骤记录写入之前结束进程（等价 kill -9）。"""

        if self.crash_after == step:
            os._exit(137)

    # --- 批准 ------------------------------------------------------------------
    def _approval_params(self) -> dict[str, str]:
        return dict(self.params)

    def require(self, operation: str, subject: str, *, extra_files: tuple[str, ...] = ()) -> dict[str, Any]:
        keys = ("VC_ENV", *extra_files)
        try:
            return fix_safety.require_approval(self._approval_params(), operation, subject, operations=OPERATIONS,
                                               file_keys=keys, subdir=APPROVAL_SUBDIR)
        except fix_safety.SafetyError as error:
            _key, intent, path, _binding = fix_safety.approval_intent(self._approval_params(), operation, subject,
                                                                       operations=OPERATIONS, file_keys=keys, subdir=APPROVAL_SUBDIR)
            raise Stop("needs-operator", str(error),
                       f"由人审阅批准意向 {intent} 后提交批准文件 {path}（schema=arm64-fix-operation-approval/v1，0600，"
                       "approved_by 须等于参数 APPROVER），再执行同一条命令") from error

    def write_intents(self, operation: str, subject: str, *, extra_files: tuple[str, ...] = ()) -> Path:
        _key, intent, _path, _binding = fix_safety.approval_intent(
            self._approval_params(), operation, subject, operations=OPERATIONS, file_keys=("VC_ENV", *extra_files),
            subdir=APPROVAL_SUBDIR)
        return intent

    # --- 受管命令 -------------------------------------------------------------------
    def cli(self, step: str, label: str, args: list[str]) -> CliResult:
        result = self.backend.cli(step, label, args)
        print(f"  [{step}] {args[0]} → rc={result.rc}", file=sys.stderr)
        return result

    # --- 动作 ----------------------------------------------------------------------
    def act_resume(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        base = ["campaign-resume", "--campaign-dir", self.params["C"], "--fix-commit", self.params["FIX_COMMIT"],
                "--regression-receipt", self.params["REGRESSION_RECEIPT"], "--reason", self.params["RESUME_REASON"]]
        preview = self.cli("resume", "preview", base)
        payload = preview.payload
        review = str(payload.get("review_sha256") or "") if isinstance(payload, dict) else ""
        if preview.rc != 0 or not isinstance(payload, dict) or payload.get("status") != "approval_required" or not FC.SHA256_RE.fullmatch(review):
            raise Stop("failed", f"campaign-resume 预览失败（rc={preview.rc}）：{FC._tail(preview.stderr)}",
                       "按报错处理（未决账务先 accounting-resolve、环境污染先 environment-isolate、请求预算先 request-budget-extend）"
                       "后重新执行同一条命令")
        self.require("campaign-resume", review, extra_files=("REGRESSION_RECEIPT",))
        applied = self.cli("resume", "apply", [*base, "--approve-sha256", review, "--approved-by", self.params["APPROVER"]])
        if applied.rc != 0 or not isinstance(applied.payload, dict) or applied.payload.get("status") != "resumed":
            raise Stop("failed", f"campaign-resume 写入失败（rc={applied.rc}）：{FC._tail(applied.stderr)}",
                       "以同一批准重跑即续接（受管工具保证幂等）：重新执行同一条命令")
        return {"review_sha256": review, "receipt_path": applied.payload.get("receipt_path"),
                "timing_status": applied.payload.get("timing_status")}

    def act_evolution(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        base = ["tool-evolution", "--campaign-dir", self.params["C"], "--fix-commit", self.params["FIX_COMMIT"],
                "--reason", self.params["EVOLUTION_REASON"]]
        preview = self.cli("evolution", "preview", base)
        payload = preview.payload
        if preview.rc != 0 or not isinstance(payload, dict):
            needs = any(marker in preview.stderr for marker in ("策略演进", "已封存", "accounting-resolve", "blocked"))
            raise Stop("needs-operator" if needs else "failed", f"工具演进预览失败（rc={preview.rc}）：{FC._tail(preview.stderr)}",
                       "由人按报错裁定（策略演进须给兼容收据与激活认证；影响已封存作业须走评估恢复；账务未决先补账）"
                       if needs else "按报错处理（Campaign 未静默时等批次结束）")
        review = str(payload.get("review_sha256") or "")
        changes = payload.get("changes") or {}
        problems = []
        if payload.get("status") != "approval_required" or not FC.SHA256_RE.fullmatch(review):
            problems.append(f"预览状态 {payload.get('status')!r} 或 review_sha256 非法")
        if (payload.get("bindings") or {}).get("fix_commit") != self.params["FIX_COMMIT"]:
            problems.append("预览绑定的修复提交不是 FIX_COMMIT")
        if changes.get("wire_closure_changed") is not False:
            problems.append(f"wire_closure_changed={changes.get('wire_closure_changed')!r}（wire 闭包变化会让失败 attempt 对账判 identity_changed 永久停线）")
        if payload.get("policy_transition"):
            problems.append("本次是策略演进（需兼容收据与激活认证）")
        if problems:
            raise Stop("needs-operator", "工具演进预览不满足自动登记的条件：" + "；".join(problems),
                       "由人按 Formal 后继流程或策略演进流程处理，本计划不代办")
        self.require("tool-evolution", review)
        applied = self.cli("evolution", "apply", [*base, "--approve-sha256", review, "--approved-by", self.params["APPROVER"]])
        if applied.rc != 0 or not isinstance(applied.payload, dict) or applied.payload.get("status") != "evolution_applied":
            raise Stop("failed", f"工具演进登记失败（rc={applied.rc}）：{FC._tail(applied.stderr)}", "按报错处理后重新执行同一条命令")
        return {"review_sha256": review, "receipt_sha256": applied.payload.get("receipt_sha256")}

    def _reconcile_object(self, label: str, kind: str, subject: str, args: list[str]) -> CliResult:
        result = self.cli("reconcile-runs", label, args)
        category, reason, hint = judge(result, "reconcile-runs")
        if kind == "supervisor-run" and category == "failed" and FC.parse_redirect(result.stderr) is not None:
            return result  # 改走 attempt 对账：调用方处理
        if category != "recoverable":
            if category == "paused":
                # 受管对账器先写收据再判定：暂停对象的收据已落盘，续跑必须重新对账（记进停下记录的 revisit）。
                self.revisit = FC._merge_keys(self.revisit, [{"kind": kind, "subject": subject}])
            raise Stop("failed" if category == "failed" else "needs-operator", f"{kind} {subject}：{reason}", hint)
        self.revisit = [item for item in self.revisit if item != {"kind": kind, "subject": subject}]
        return result

    def act_reconcile_runs(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        tail = facts["tail"]
        done: list[str] = []
        redirected: list[str] = []
        for entry in tail.get("pending") or []:
            if entry["kind"] == "supervisor-run":
                result = self._reconcile_object(f"run-{entry['run_id']}", "supervisor-run", entry["run_id"],
                                                ["reconcile-supervisor-run", "--run-dir", entry["run_dir"], "--campaign-dir", self.params["C"]])
                redirect = FC.parse_redirect(result.stderr) if result.rc != 0 else None
                if redirect is not None:
                    redirected.append(entry["run_id"])
                    for attempt_id, revision in redirect:
                        if attempt_id == self.params["ATTEMPT"] and revision is None:
                            continue
                        args = ["reconcile-attempt", "--campaign-dir", self.params["C"], "--attempt-id", attempt_id]
                        subject = attempt_id
                        if revision:
                            args += ["--recovery-revision", revision]
                            subject = f"{attempt_id}:{revision}"
                        self._reconcile_object(f"attempt-{attempt_id}", "attempt", subject, args)
                        done.append(subject)
                    continue
                done.append(entry["run_id"])
            else:
                if entry.get("attempt_id") == self.params["ATTEMPT"] and not entry.get("recovery_revision"):
                    continue
                args = ["reconcile-attempt", "--campaign-dir", self.params["C"], "--attempt-id", entry["attempt_id"]]
                subject = entry["attempt_id"]
                if entry.get("recovery_revision"):
                    args += ["--recovery-revision", entry["recovery_revision"]]
                    subject = f"{subject}:{entry['recovery_revision']}"
                self._reconcile_object(f"attempt-{entry['attempt_id']}", "attempt", subject, args)
                done.append(subject)
        self.exclude_runs = sorted(set(self.exclude_runs) | set(redirected))
        return {"reconciled": done, "redirected_runs": redirected, "revisit": self.revisit}

    def _reconcile_target(self, step: str, label: str, extra: list[str]) -> CliResult:
        return self.cli(step, label, ["reconcile-attempt", "--campaign-dir", self.params["C"], "--attempt-id", self.params["ATTEMPT"], *extra])

    def act_reconcile(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        result = self._reconcile_target("reconcile", "reconcile", [])
        category, reason, hint = judge(result, "reconcile")
        if category != "recoverable":
            raise Stop("failed" if category == "failed" else "needs-operator", f"目标 attempt 对账：{reason}", hint)
        payload = result.payload
        check = payload.get("resume_reuse_check") or {}
        if check.get("status") == "inconsistent":
            raise Stop("needs-operator", f"恢复预览与 resume 复用判定不一致：{check.get('reason')}",
                       "按原因修复后重新执行同一条命令（不一致的预览不可批准）")
        review, path, problems = FC._preview_facts(self.params, payload)
        if problems:
            raise Stop("failed", "；".join(problems), "人工核对对账输出")
        self.force_reconcile = False
        # 批准与授权共用同一个预览摘要：停在批准之前就把两份意向一次写全，人只需一次提交。
        intents = []
        if PHASE_PROFILES[facts["phase"]]["separate_approve_authorize"]:
            intents = [str(self.write_intents("recovery-approve", review)), str(self.write_intents("recovery-authorize", review))]
        else:
            intents = [str(self.write_intents("recovery-continue", review))]
        return {"review_sha256": review, "preview_path": path, "approval_intents": intents,
                "execute_job_ids": (payload.get("recovery_preview") or {}).get("execute_job_ids"),
                "reuse_count": len((payload.get("recovery_preview") or {}).get("reuse_job_ids") or [])}

    def act_approve(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        preview = current_preview(facts)
        assert preview is not None
        review = preview["review_sha256"]
        self.write_intents("recovery-authorize", review)
        self.require("recovery-approve", review)
        result = self._reconcile_target("approve", "approve", ["--approve-recovery-sha256", review])
        category, reason, hint = judge(result, "approve")
        if category != "recoverable":
            if STALE_MARKER in result.stderr and self.rewinds < 1:
                self.rewinds += 1
                self.force_reconcile = True
                return {"rewind": "approve 报预览过期，回到 reconcile 重新生成预览", "stderr": FC._tail(result.stderr)}
            raise Stop("failed" if category == "failed" else "needs-operator", f"批准恢复预览失败：{reason}", hint)
        approval = result.payload.get("recovery_approval") if isinstance(result.payload, dict) else None
        if not isinstance(approval, dict) or approval.get("approved_sha256") not in (None, review):
            raise Stop("failed", "输出没有与该摘要一致的 recovery_approval", "人工核对恢复预览与批准收据")
        return {"review_sha256": review, "approval_index": (approval or {}).get("index")}

    def act_authorize(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        preview = current_preview(facts)
        assert preview is not None
        review = preview["review_sha256"]
        self.require("recovery-authorize", review)
        result = self._reconcile_target("authorize", "authorize", ["--authorize-recovery-preview", preview["path"]])
        payload = result.payload
        if result.rc != 0 or not isinstance(payload, dict) or payload.get("status") != "authorized":
            if STALE_MARKER in result.stderr and self.rewinds < 1:
                self.rewinds += 1
                self.force_reconcile = True
                return {"rewind": "authorize 报账本 head 已推进，回到 reconcile 重新生成预览", "stderr": FC._tail(result.stderr)}
            raise Stop("failed", f"授权恢复预览失败（rc={result.rc}）：{FC._tail(result.stderr)}",
                       "账本 head 在批准后推进时重新执行同一条命令（会重新对账、批准）；其它按报错处理")
        return {"review_sha256": review, "timing_recovery_event": payload.get("timing_recovery_event")}

    def act_continue(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        """续跑派发：唯一会产生真实采集请求的动作。占位先于启动、启动记录后于派发，均只写一次。"""

        phase = facts["phase"]
        preview = current_preview(facts)
        assert preview is not None
        review = preview["review_sha256"]
        if phase == "official":
            self.require("recovery-continue", review)
        if facts["active_runs"] or FC.driver_processes(self.params):
            raise Stop("needs-operator", f"已有运行中的父 run {facts['active_runs']} 或驱动进程", "等其结束后再执行同一条命令")
        driver = Path(self.params["DRIVER_TARGET"]) / "driver"
        runroot = Path(self.params["RUNROOT"])
        env = {**os.environ, "ARM64_VC_ENV": self.params["VC_ENV"], "PYTHONDONTWRITEBYTECODE": "1"}
        if phase == "official":
            script = driver / "entry.sh"
            command = ["bash", str(script), "--approve-sha256", review, "--approved-by", self.params["APPROVER"]]
            log = runroot / "recovery-plan-entry.out"
        else:
            script = driver / str(FC._client_row(self.params)["vc5_recover_script"])
            command = ["bash", str(script), preview["path"]]
            log = runroot / "vc5-recover.out"
            env["VC_STATE_DIR"] = self.params["VC_STATE_DIR"]
        if not script.is_file():
            raise Stop("failed", f"驱动安装目标缺少 {script}", "先重装驱动（fix-and-continue.sh --from postdeploy 或 install.py install）")
        launch_dir = _launch_dir(self.params)
        launch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fix_safety.write_once(launch_dir / f"{review}.inflight.json", {
            "schema_version": LAUNCH_SCHEMA, "status": "dispatch_pending", "round": self.params["ROUND"],
            "campaign": self.params["CAMPAIGN"], "attempt": self.params["ATTEMPT"], "phase": phase,
            "preview": fix_safety.file_binding(preview["path"]), "command": command, "log": str(log)})
        self.maybe_crash("continue-inflight")
        for name in ("vc5-recover.out", "vc5-run-batch.out"):
            old = runroot / name
            if phase != "official" and old.exists():
                old.rename(runroot / f"{name}.pre-{self.params['ROUND']}-{self.backend.stamp}")
        wrapper = 'cd "$1" && shift && "$@"; code=$?; echo "exit=$code"; exit "$code"'
        with open(log, "w", encoding="utf-8") as handle:
            os.chmod(log, 0o600)
            process = subprocess.Popen(["bash", "-c", wrapper, "recovery-plan-launch", self.params["D"], *command], stdin=subprocess.DEVNULL,
                                       stdout=handle, stderr=subprocess.STDOUT, env=env, start_new_session=True, close_fds=True)
        self.maybe_crash("continue-spawned")
        fix_safety.write_once(launch_dir / f"{review}.started.json", {
            "schema_version": LAUNCH_SCHEMA, "status": "started", "pid": process.pid, "log": str(log),
            "started_at_utc": _utc_now()})
        return {"review_sha256": review, "pid": process.pid, "log": str(log), "command": command}

    ACTIONS = {
        "resume": act_resume, "evolution": act_evolution, "reconcile-runs": act_reconcile_runs, "reconcile": act_reconcile,
        "approve": act_approve, "authorize": act_authorize, "continue": act_continue,
    }

    # --- 主循环 --------------------------------------------------------------------
    def facts(self) -> dict[str, Any]:
        return gather_facts(self.params, self.backend, force_reconcile=self.force_reconcile,
                            revisit=tuple(self.revisit), exclude_runs=tuple(self.exclude_runs))

    def run(self) -> int:
        plan = build_plan(self.params, self.facts())
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        fix_safety.write_once(self.out / "plans" / f"{plan['plan_sha256']}.json", plan)
        print(render_plan(plan, mode="执行前计划"), file=sys.stderr)
        progressed: dict[str, int] = {}
        for _ in range(MAX_ITERATIONS):
            facts = self.facts()
            steps = derive_steps(self.params, facts)
            current = next((step for step in steps if step.status in {PENDING, BLOCKED}), None)
            if current is None:
                return self.finish(facts, steps)
            if current.status == BLOCKED:
                return self.stop(current.id, Stop("needs-operator", current.detail, "排除阻塞后重新执行同一条命令"))
            progressed[current.id] = progressed.get(current.id, 0) + 1
            if progressed[current.id] > 3:
                return self.stop(current.id, Stop("failed", f"{current.id} 连续 {progressed[current.id] - 1} 次执行后现场仍判待执行",
                                                  "人工核对该步骤的原始输出与现场（判定与动作互相抵消）"))
            print(f"==== [{_utc_now()}] 步骤 {current.id}：{current.detail}", file=sys.stderr)
            try:
                summary = self.ACTIONS[current.id](self, facts)
            except Stop as stop:
                return self.stop(current.id, stop)
            except (fix_safety.SafetyError, RecoveryPlanError, OSError, subprocess.SubprocessError) as error:
                return self.stop(current.id, Stop("failed", f"{type(error).__name__}: {error}", "按报错处理后重新执行同一条命令"))
            self.maybe_crash(current.id)
            if current.id in {"resume", "evolution", "reconcile-runs"}:
                self.force_reconcile = True  # 账本／工具身份／对账链变了，已有预览必然过期
            self.record(current.id, "passed", summary=summary)
        return self.stop("loop", Stop("failed", f"执行循环超过 {MAX_ITERATIONS} 轮仍未收敛", "人工核对"))

    def stop(self, step: str, stop: Stop) -> int:
        summary = {"revisit": self.revisit} if step == "reconcile-runs" else None
        self.record(step, stop.status, reason=stop.reason, next_hint=stop.next_hint, summary=summary)
        label = "需要人工处理（本计划不越权代办）" if stop.status == "needs-operator" else "失败"
        print("\n".join([
            f"==== 恢复计划停在「{step}」：{label}", f"原因：{stop.reason}", f"下一步：{stop.next_hint}",
            f"续跑：bash {DRV / 'recovery-plan.sh'} {self.params['PARAMS_PATH']} --apply（重复执行安全，已完成的步骤会被跳过）",
            f"RECOVERY_PLAN_STOPPED step={step} status={stop.status}"]), file=sys.stderr)
        return EXIT_OPERATOR if stop.status == "needs-operator" else EXIT_FAILED

    def finish(self, facts: Mapping[str, Any], steps: list[Step]) -> int:
        plan = build_plan(self.params, facts)
        self.record("done", "passed", summary={"plan_sha256": plan["plan_sha256"], "overall": plan["overall"]})
        print(render_plan(plan, mode="执行后现场"), file=sys.stderr)
        print(f"RECOVERY_PLAN_DONE round={self.params['ROUND']} out={self.out}", file=sys.stderr)
        return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_or_exit(path: str) -> dict[str, str]:
    try:
        return load_params(Path(path))
    except (parse_env.EnvFileError, FC.FixAndContinueError, OSError) as error:
        print(f"恢复计划参数文件无效：{error}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from error


def acquire_lock(params: Mapping[str, str]) -> int:
    """同一轮次只允许一个实例（执行模式）；锁随进程退出释放，锁文件保留。"""

    out = Path(params["OUT"])
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(out / ".round.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("本轮已有恢复计划实例在运行，拒绝并发", file=sys.stderr)
        raise SystemExit(EXIT_LOCKED)
    return handle


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("params", help="恢复计划参数文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="只读计划预览（缺省）")
    mode.add_argument("--apply", action="store_true", help="按序执行；每步前重新判定，已完成的跳过")
    parser.add_argument("--json", action="store_true", help="计划预览输出 JSON")
    args = parser.parse_args(argv)
    params = _load_or_exit(args.params)
    try:
        if not args.apply:
            backend = ManagedBackend(params, raw_dir=None)
            plan = build_plan(params, gather_facts(params, backend, revisit=tuple(prior_revisit(params))))
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) if args.json else render_plan(plan, mode="只读预览"))
            return EXIT_OK
        lock = acquire_lock(params)
        try:
            backend = ManagedBackend(params, raw_dir=Path(params["OUT"]) / "raw")
            return Runner(params, backend).run()
        finally:
            os.close(lock)
    except (RecoveryPlanError, FC.FixAndContinueError, fix_safety.SafetyError, OSError) as error:
        print(f"恢复计划失败：{error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
