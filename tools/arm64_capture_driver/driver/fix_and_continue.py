#!/usr/bin/env python3
"""修好接着跑一条命令（第 35 项，``fix-and-continue.sh``）的 Python 辅助：参数安全解析、逐步判定与步骤记录。

背景：此前每轮"修复 → 部署 → 登记 → 对账 → 批准 → 重派"由人按 sed 复制改写一套轮次脚本（upload-rN 的
deploy／postdeploy／resume 与 repair-rN 的实测／回归收据／根因修复登记），十几步逐步执行、每步人工核对，
约 15～30 分钟人工间隙且容易漏改。本辅助把这些轮次脚本里**每轮都一样的判定**收进驱动（随驱动清单受管），
**每轮不同的值**全部来自一份轮次参数文件（KEY=VALUE，经 ``parse_env.parse_assignments`` 同一词法层解析，
绝不 source、绝不执行）与两份 JSON（期望摘要 ``EXPECT``、入口断言 ``ENTRY_GREPS``）。

分工：``fix-and-continue.sh`` 负责调度与执行外部命令（git、setsid、受管工具 CLI、部署与驱动安装），并把每次
受管命令的 stdout／stderr／退出码原样落盘；本辅助只读这些落盘结果与现场事实做判定，输出可 ``eval`` 的赋值
（``shlex.quote`` 引号化、键名固定），并写 ``$RUNROOT/fix-and-continue/<轮次>/<步骤>.json`` 步骤记录。

判定退出码（``decide`` 子命令）：0 继续；10 本步骤幂等跳过（已写 skipped 记录）；1 失败；4 需要人工处理
（账务暂停、环境污染、永久停线、需要人给出估计上界或证据文件、wire 闭包变化等——本脚本不越权代办）；
5 驱动自身已被本轮重装更新，须用新驱动续跑。停下时本辅助写记录并在 stderr 打印原因、下一步与 ``--from`` 续跑命令。

绝不做的事：不调用 accounting-resolve、environment-isolate、campaign-resume、request-budget-extend；不传任何
``--force`` 类参数；不重装出口守护；不修改受管工具源码；不生成 docs/egress/maintenance 下的收据。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 驱动目录与数据根都不得出现 __pycache__（清单闭合、工具身份）。

import argparse  # noqa: E402
import difflib  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Iterable, Mapping  # noqa: E402

DRV = Path(__file__).resolve().parent
if str(DRV) not in sys.path:
    sys.path.insert(0, str(DRV))
import parse_env  # noqa: E402  —— 与驱动参数文件同一安全解析（parse_assignments）

# ---------------------------------------------------------------------------
# 步骤与参数
# ---------------------------------------------------------------------------

# 步骤顺序（--from 按名续跑）。相对设计稿的偏离：阶段延期拆成对账前的 pre-extend 与授权后的 extend 两处，
# 不放在"批准与授权之间"——延期 apply 会在 Campaign 计时账本追加 deadline_extended 事件，而授权消费预览时
# 要求账本 head 仍是预览冻结的 head（否则"恢复批准消费前 Campaign 账本 head 已推进，必须重新对账"）；
# 阶段截止在对账前已过时，对账本身就判预算暂停、拒绝批准，必须先延期（见 fix-and-continue.sh 头注释）。
STEPS = (
    "deploy",
    "postdeploy",
    "item-tests",
    "evolution",
    "pre-extend",
    "reconcile-runs",
    "reconcile-attempt",
    "repair",
    "approve",
    "authorize",
    "extend",
    "accepted",
    "recover",
)

REQUIRED_KEYS = (
    "ROUND",  # 轮次标识（输出目录 $RUNROOT/fix-and-continue/<ROUND>/）
    "D",  # 数据根（受管工具生产树所在）
    "RUNROOT",  # 驱动参数文件里的 RUNROOT（vc5-recover.out 等写这里）
    "VC_ENV",  # 驱动参数文件（ARM64_VC_ENV，vc5-recover.sh 用）
    "VC_STATE_DIR",  # 监督器状态目录（父 run 的 run-*/）
    "CAMPAIGN",  # Campaign ID
    "ATTEMPT",  # 目标 attempt（本轮要对账、批准恢复预览、续跑的那一个）
    "CANDIDATE",  # 候选 ID
    "SRC",  # staging 源仓库（bundle fetch 进来的仓库）
    "STAGING_TREE",  # 部署用 staging 树（受监督部署的 --staging-root）
    "BUNDLE",  # 本轮上传的 git bundle
    "BUNDLE_BRANCH",  # bundle 里的分支
    "HEAD_COMMIT",  # 部署提交（= bundle head）
    "FIX_COMMIT",  # 工具演进登记的修复提交
    "EXPECT",  # 部署后期望摘要 JSON（arm64-fix-and-continue-expect/v1）
    "ENTRY_GREPS",  # 入口断言列表 JSON（arm64-fix-and-continue-entry-assertions/v1）
    "ITEM_TESTS",  # 实测：staging 树上整跑的 unittest 目标（空格分隔）
    "EVOLUTION_REASON",  # 工具演进理由
    "APPROVER",  # 批准人（如实记录）
)
OPTIONAL_KEYS = (
    "ITEM_TESTS_K",  # 实测第二段：-k 模式（空格分隔），与 ITEM_TESTS_K_MODULES 同时给
    "ITEM_TESTS_K_MODULES",
    "ITEM_TESTS_MAX_SECONDS",
    "DEPLOY_MAX_SECONDS",
    "DRIVER_TARGET",  # 驱动安装目标（默认 /root/arm64-capture-driver）
    "UPLOAD_SHA256",  # 上传材料 sha256 清单（"<sha256>  <文件名>"，文件名相对清单所在目录）
    "EXTEND_DEADLINE",  # 阶段新截止（UTC，YYYY-MM-DDTHH:MM:SSZ）；不给则不延期
    "EXTEND_PHASE",  # 默认 VC-5
    "EXTEND_REASON",
    "REPAIR_ROOT_CAUSES",  # 根因修复登记：根因 ID（空格分隔）；不给则跳过登记
    "REPAIR_FIX_COMMIT",  # 默认 FIX_COMMIT
    "REPAIR_SEQ",  # 对账入账的总账序号（只用于 note 引用）
    "REPAIR_NOTE",
    "REGRESSION_RECEIPT",  # 回归收据（arm64-code-regression-receipt/v1）；已存在即直接使用
    "REGRESSION_DRAFT",  # 回归收据草稿：回归收据不存在时由草稿补本轮实测日志与部署收据后写一次
)
DEFAULTS = {
    "DRIVER_TARGET": "/root/arm64-capture-driver",
    "EXTEND_PHASE": "VC-5",
    "ITEM_TESTS_MAX_SECONDS": "7200",
    "DEPLOY_MAX_SECONDS": "1800",
}
PATH_KEYS = (
    "D", "RUNROOT", "VC_ENV", "VC_STATE_DIR", "SRC", "STAGING_TREE", "BUNDLE", "EXPECT", "ENTRY_GREPS",
    "DRIVER_TARGET", "UPLOAD_SHA256", "REGRESSION_RECEIPT", "REGRESSION_DRAFT",
)
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ROOT_CAUSE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PATH_RE = re.compile(r"/[A-Za-z0-9_./@+=,:-]+")
MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
K_PATTERN_RE = re.compile(r"[A-Za-z0-9_*.-]+")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PHASE_RE = re.compile(r"VC-[0-9]")

STEP_SCHEMA = "arm64-fix-and-continue-step/v1"
EXPECT_SCHEMA = "arm64-fix-and-continue-expect/v1"
ENTRY_SCHEMA = "arm64-fix-and-continue-entry-assertions/v1"
REGRESSION_SCHEMA = "arm64-code-regression-receipt/v1"
DEPLOY_RECEIPT_KEYS = (
    "status", "architecture", "tool_files_sha256", "policy_version", "policy_sha256", "wire_producer_sha256",
    "evidence_semantics_sha256", "control_sha256", "supervisor_sha256",
)
DEFAULT_GUARD_STATUS = "/run/sub2api-egress/status.json"
AUTHORIZABLE_LEDGER_STATES = frozenset({"recovery_required", "stage_review_required", "candidate_review_required"})
CANDIDATE_RECOVER_SCRIPT = "vc5-recover.sh"
# 第 59 项：受管对账器 _decide 两类"根因重试达上限"暂停原因的原文片段。Campaign 账本 stop_required 只能由人执行
# campaign-resume（本脚本不代办）；项目总账根因达上限由本脚本按参数给的材料执行 record-root-cause-repair 后重新对账。
CAMPAIGN_ROOT_CAUSE_STOP = "Campaign 账本同根因重试已达上限"
PROJECT_ROOT_CAUSE_LIMIT = "累计失败已达上限"
# 在步骤内登记根因修复的步骤：reconcile-runs 在 repair 步骤之前，停下后 --from repair 过不了前序核对，只能在步骤内登记。
INLINE_REPAIR_STEPS = frozenset({"reconcile-runs"})
# reconcile-runs 续跑时要重新对账的对象类别（与扫描结果、run-verdict 的 kind 一致）。
REVISIT_KINDS = frozenset({"supervisor-run", "attempt"})

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_OPERATOR = 4
EXIT_RESTART = 5
EXIT_SKIP = 10


class FixAndContinueError(RuntimeError):
    """判定所需的前提不成立（转为失败停下）。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise FixAndContinueError(f"{label} 不是合法时间：{value!r}") from error
    if parsed.tzinfo is None:
        raise FixAndContinueError(f"{label} 缺少时区：{value!r}")
    return parsed


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _read_json(path: Path, label: str) -> Any:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FixAndContinueError(f"{label}不存在或不可信：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixAndContinueError(f"{label}不是合法 JSON：{path}：{error}") from error


def _write_private_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    """原子写入：同目录临时文件 → chmod → rename。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def _is_clean_path(value: str) -> bool:
    return bool(PATH_RE.fullmatch(value)) and os.path.normpath(value) == value and ".." not in Path(value).parts


def load_params(path: Path) -> dict[str, str]:
    """安全解析轮次参数文件并逐键校验；与驱动参数文件（VC_ENV）交叉核对。失败抛 parse_env.EnvFileError。"""

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
    values.setdefault("REPAIR_FIX_COMMIT", values["FIX_COMMIT"])
    for key in PATH_KEYS:
        if key in values and not _is_clean_path(values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是规范的绝对路径（不含空白、.. 或重复分隔符）：{values[key]!r}")
    for key in ("ROUND", "CAMPAIGN", "ATTEMPT", "CANDIDATE"):
        if not ID_RE.fullmatch(values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是单层安全标识")
    for key in ("HEAD_COMMIT", "FIX_COMMIT", "REPAIR_FIX_COMMIT"):
        if not SHA1_RE.fullmatch(values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是完整 40 位小写提交号")
    branch = values["BUNDLE_BRANCH"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch or branch.endswith((".", "/", ".lock")):
        raise parse_env.EnvFileError("BUNDLE_BRANCH 不是安全的分支名")
    for key in ("ITEM_TESTS", "ITEM_TESTS_K_MODULES"):
        if key in values and not all(MODULE_RE.fullmatch(token) for token in values[key].split()):
            raise parse_env.EnvFileError(f"{key} 只能是空格分隔的 unittest 目标（点分标识）")
    if not values["ITEM_TESTS"].split():
        raise parse_env.EnvFileError("ITEM_TESTS 不能为空")
    if ("ITEM_TESTS_K" in values) != ("ITEM_TESTS_K_MODULES" in values):
        raise parse_env.EnvFileError("ITEM_TESTS_K 与 ITEM_TESTS_K_MODULES 必须同时给出")
    if "ITEM_TESTS_K" in values and not all(K_PATTERN_RE.fullmatch(token) for token in values["ITEM_TESTS_K"].split()):
        raise parse_env.EnvFileError("ITEM_TESTS_K 只能是空格分隔的 -k 模式（字母数字与 _ * . -）")
    for key in ("ITEM_TESTS_MAX_SECONDS", "DEPLOY_MAX_SECONDS", "REPAIR_SEQ"):
        if key in values and not re.fullmatch(r"[1-9][0-9]*", values[key]):
            raise parse_env.EnvFileError(f"{key} 必须是正整数")
    if not PHASE_RE.fullmatch(values["EXTEND_PHASE"]):
        raise parse_env.EnvFileError("EXTEND_PHASE 必须形如 VC-5")
    if "EXTEND_DEADLINE" in values:
        if not UTC_RE.fullmatch(values["EXTEND_DEADLINE"]):
            raise parse_env.EnvFileError("EXTEND_DEADLINE 必须形如 2026-09-28T16:00:00Z")
        try:
            _parse_utc(values["EXTEND_DEADLINE"], "EXTEND_DEADLINE")
        except FixAndContinueError as error:
            raise parse_env.EnvFileError(str(error)) from error
        if "EXTEND_REASON" not in values:
            raise parse_env.EnvFileError("给了 EXTEND_DEADLINE 必须同时给 EXTEND_REASON")
    if "REPAIR_ROOT_CAUSES" in values:
        causes = values["REPAIR_ROOT_CAUSES"].split()
        if not causes or not all(ROOT_CAUSE_RE.fullmatch(cause) for cause in causes) or len(set(causes)) != len(causes):
            raise parse_env.EnvFileError("REPAIR_ROOT_CAUSES 只能是空格分隔、互不重复的根因 ID")
        if "REPAIR_NOTE" not in values:
            raise parse_env.EnvFileError("给了 REPAIR_ROOT_CAUSES 必须同时给 REPAIR_NOTE")
    if "REGRESSION_DRAFT" in values and "REGRESSION_RECEIPT" not in values:
        raise parse_env.EnvFileError("给了 REGRESSION_DRAFT 必须同时给 REGRESSION_RECEIPT（收据写入位置）")
    # 与驱动参数文件交叉核对：vc5-recover.sh 读的是它，两者对不上就会续跑到别的 Campaign／候选。
    env_path = Path(values["VC_ENV"])
    if env_path.is_symlink() or not env_path.is_file():
        raise parse_env.EnvFileError(f"VC_ENV 不存在或不可信：{env_path}")
    driver_values = parse_env.parse(env_path.read_text(encoding="utf-8"))
    for key, driver_key in (("D", "D"), ("RUNROOT", "RUNROOT"), ("CAMPAIGN", "NEW"), ("CANDIDATE", "CAND")):
        if values[key] != driver_values[driver_key]:
            raise parse_env.EnvFileError(
                f"{key}={values[key]!r} 与驱动参数文件不一致（{env_path} 的 {driver_key}={driver_values[driver_key]!r}）"
            )
    values["OUT"] = f"{values['RUNROOT']}/fix-and-continue/{values['ROUND']}"
    values["C"] = f"{values['D']}/evidence/campaigns/{values['CAMPAIGN']}"
    values["PARAMS_PATH"] = str(path.resolve())
    values["PARAMS_SHA256"] = _sha256_bytes(text.encode("utf-8"))
    return values


def identity(params: Mapping[str, str]) -> dict[str, str]:
    """轮次身份：--from 续跑时前序步骤记录必须同一身份（参数里可调的延期／根因修复参数不在其内）。"""

    return {key.lower(): params[key] for key in ("ROUND", "HEAD_COMMIT", "FIX_COMMIT", "CAMPAIGN", "ATTEMPT", "CANDIDATE")}


# ---------------------------------------------------------------------------
# 步骤记录与停下
# ---------------------------------------------------------------------------


def _out(params: Mapping[str, str]) -> Path:
    return Path(params["OUT"])


def _partial_path(params: Mapping[str, str], step: str) -> Path:
    return _out(params) / f".partial-{step}.json"


def note(params: Mapping[str, str], step: str, **values: Any) -> None:
    """累积本步骤的摘要字段，步骤结束时并入记录。"""

    path = _partial_path(params, step)
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    current.update(values)
    _write_private_json(path, current)


def _partial(params: Mapping[str, str], step: str) -> dict[str, Any]:
    """本步骤已累积的摘要字段（fix-and-continue.sh 每步开始时清空）。"""

    path = _partial_path(params, step)
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return payload if isinstance(payload, dict) else {}


def read_step(params: Mapping[str, str], step: str) -> dict[str, Any] | None:
    path = _out(params) / f"{step}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def write_step(
    params: Mapping[str, str],
    step: str,
    status: str,
    *,
    run_stamp: str | None,
    reason: str | None = None,
    next_hint: str | None = None,
    resume_from: str | None = None,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    partial_path = _partial_path(params, step)
    merged: dict[str, Any] = json.loads(partial_path.read_text(encoding="utf-8")) if partial_path.is_file() else {}
    merged.update(summary or {})
    record = {
        "schema_version": STEP_SCHEMA,
        "round": params["ROUND"],
        "step": step,
        "status": status,
        "identity": identity(params),
        "params_path": params["PARAMS_PATH"],
        "params_sha256": params["PARAMS_SHA256"],
        "run_stamp": run_stamp,
        "recorded_at_utc": _utc_now(),
        "summary": merged,
        "reason": reason,
        "next": next_hint,
        "resume_from": resume_from,
    }
    out = _out(params)
    _write_private_json(out / f"{step}.json", record)
    with (out / "history.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(out / "history.ndjson", 0o600)
    if partial_path.exists():
        partial_path.unlink()
    return record


def _script() -> Path:
    return DRV / "fix-and-continue.sh"


def stop(
    params: Mapping[str, str],
    step: str,
    status: str,
    reason: str,
    next_hint: str,
    *,
    run_stamp: str | None,
    resume_from: str | None = None,
    summary: Mapping[str, Any] | None = None,
) -> int:
    """写停下记录并打印"下一步该跑哪一步"。status：failed／needs-operator。"""

    resume = resume_from or (step if step in STEPS else STEPS[0])
    write_step(params, step, status, run_stamp=run_stamp, reason=reason, next_hint=next_hint, resume_from=resume,
               summary=summary)
    label = "需要人工处理（本脚本不越权代办）" if status == "needs-operator" else "失败"
    lines = [
        f"==== 修好接着跑停在「{step}」：{label}",
        f"原因：{reason}",
        f"下一步：{next_hint}",
        f"续跑：bash {_script()} {params['PARAMS_PATH']} --from {resume}",
        f"步骤记录：{_out(params) / (step + '.json')}",
        f"FIX_AND_CONTINUE_STOPPED step={step} status={status} resume_from={resume}",
    ]
    print("\n".join(lines), file=sys.stderr)
    return EXIT_OPERATOR if status == "needs-operator" else EXIT_FAILED


def skip(params: Mapping[str, str], step: str, reason: str, *, run_stamp: str | None,
         summary: Mapping[str, Any] | None = None) -> int:
    write_step(params, step, "skipped", run_stamp=run_stamp, reason=reason, summary=summary)
    print(f"  [{step}] 幂等跳过：{reason}", file=sys.stderr)
    return EXIT_SKIP


def passed(params: Mapping[str, str], step: str, *, run_stamp: str | None, summary: Mapping[str, Any] | None = None) -> int:
    write_step(params, step, "passed", run_stamp=run_stamp, summary=summary)
    return EXIT_OK


def check_from(params: Mapping[str, str], step: str) -> list[str]:
    """--from 续跑：之前每个步骤在本轮都必须有 passed／skipped 记录且轮次身份一致（从不跳过核对）。"""

    problems: list[str] = []
    for prior in STEPS[: STEPS.index(step)]:
        record = read_step(params, prior)
        if record is None:
            problems.append(f"前序步骤 {prior} 在本轮没有记录；先执行 --from {prior}")
            break
        if record.get("status") not in {"passed", "skipped"}:
            problems.append(f"前序步骤 {prior} 的状态是 {record.get('status')}；先执行 --from {prior}")
            break
        if record.get("identity") != identity(params):
            problems.append(f"前序步骤 {prior} 的轮次身份与当前参数不一致（HEAD／修复提交／Campaign／attempt／候选变了就是新一轮）")
            break
    return problems


# ---------------------------------------------------------------------------
# 期望摘要、入口断言、部署收据、守护
# ---------------------------------------------------------------------------


def load_expect(path: Path) -> dict[str, Any]:
    payload = _read_json(path, "期望摘要 EXPECT ")
    allowed = {"schema_version", "deploy_receipt", "wire_closure_sha256", "guard", "evolution", "note"}
    if not isinstance(payload, dict) or payload.get("schema_version") != EXPECT_SCHEMA or set(payload) - allowed:
        raise FixAndContinueError(f"EXPECT schema 必须是 {EXPECT_SCHEMA}，且只允许键 {sorted(allowed)}")
    receipt = payload.get("deploy_receipt")
    if not isinstance(receipt, dict) or set(receipt) != set(DEPLOY_RECEIPT_KEYS):
        raise FixAndContinueError(f"EXPECT.deploy_receipt 必须恰好包含 {list(DEPLOY_RECEIPT_KEYS)}")
    if receipt["status"] != "passed":
        raise FixAndContinueError("EXPECT.deploy_receipt.status 只能是 passed")
    for key in ("tool_files_sha256", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256",
                "supervisor_sha256"):
        if not isinstance(receipt[key], str) or not SHA256_RE.fullmatch(receipt[key]):
            raise FixAndContinueError(f"EXPECT.deploy_receipt.{key} 必须是 64 位小写十六进制")
    if isinstance(receipt["policy_version"], bool) or not isinstance(receipt["policy_version"], int):
        raise FixAndContinueError("EXPECT.deploy_receipt.policy_version 必须是整数")
    closure = payload.get("wire_closure_sha256")
    if closure is not None and (not isinstance(closure, str) or not SHA256_RE.fullmatch(closure)):
        raise FixAndContinueError("EXPECT.wire_closure_sha256 必须是 64 位小写十六进制")
    guard = payload.get("guard")
    guard_keys = {"service", "baseline_file", "diff_removed", "diff_added", "status_path"}
    if not isinstance(guard, dict) or set(guard) - guard_keys or not {"service", "baseline_file", "diff_removed", "diff_added"} <= set(guard):
        raise FixAndContinueError(f"EXPECT.guard 必须包含 service／baseline_file／diff_removed／diff_added（可选 status_path）")
    if not isinstance(guard["service"], str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", guard["service"]):
        raise FixAndContinueError("EXPECT.guard.service 不是安全的 systemd 单元名")
    for key in ("baseline_file", "status_path"):
        if key in guard and (not isinstance(guard[key], str) or not _is_clean_path(guard[key])):
            raise FixAndContinueError(f"EXPECT.guard.{key} 必须是规范绝对路径")
    for key in ("diff_removed", "diff_added"):
        if not isinstance(guard[key], list) or not all(isinstance(item, str) for item in guard[key]):
            raise FixAndContinueError(f"EXPECT.guard.{key} 必须是字符串列表")
    evolution = payload.get("evolution")
    if evolution is not None:
        if not isinstance(evolution, dict) or set(evolution) - {"wire_closure_changed", "evidence_closure_changed"} or not all(
            isinstance(value, bool) for value in evolution.values()
        ):
            raise FixAndContinueError("EXPECT.evolution 只允许布尔键 wire_closure_changed／evidence_closure_changed")
    return payload


def load_entries(path: Path) -> list[dict[str, str]]:
    payload = _read_json(path, "入口断言 ENTRY_GREPS ")
    if not isinstance(payload, dict) or payload.get("schema_version") != ENTRY_SCHEMA or set(payload) - {"schema_version", "assertions", "note"}:
        raise FixAndContinueError(f"ENTRY_GREPS schema 必须是 {ENTRY_SCHEMA}")
    assertions = payload.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise FixAndContinueError("ENTRY_GREPS.assertions 必须是非空列表")
    checked: list[dict[str, str]] = []
    for index, item in enumerate(assertions, 1):
        if not isinstance(item, dict):
            raise FixAndContinueError(f"入口断言第 {index} 条不是对象")
        keys = set(item) - {"note"}
        if keys == {"exists"}:
            target = item["exists"]
        elif keys in ({"path", "regex"}, {"path", "fixed"}):
            target = item["path"]
            if "regex" in item:
                try:
                    re.compile(item["regex"])
                except (re.error, TypeError) as error:
                    raise FixAndContinueError(f"入口断言第 {index} 条正则非法：{error}") from error
            elif not isinstance(item["fixed"], str) or not item["fixed"] or "\n" in item["fixed"]:
                raise FixAndContinueError(f"入口断言第 {index} 条 fixed 必须是非空单行字符串")
        else:
            raise FixAndContinueError(f"入口断言第 {index} 条只能是 {{exists}} 或 {{path, regex|fixed}}")
        if not isinstance(target, str) or not target or ".." in Path(target).parts or (
            target.startswith("/") and not _is_clean_path(target)
        ):
            raise FixAndContinueError(f"入口断言第 {index} 条路径非法：{target!r}")
        checked.append(dict(item))
    return checked


def entry_problems(params: Mapping[str, str], entries: Iterable[Mapping[str, str]]) -> list[str]:
    problems: list[str] = []
    for item in entries:
        raw = item.get("exists") or item["path"]
        path = Path(raw) if raw.startswith("/") else Path(params["D"]) / raw
        if "exists" in item:
            if not path.is_file():
                problems.append(f"缺少文件：{path}")
            continue
        if path.is_symlink() or not path.is_file():
            problems.append(f"断言文件不存在：{path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "regex" in item:
            if re.search(item["regex"], text, re.M) is None:
                problems.append(f"{path} 不含正则 {item['regex']!r}")
        elif item["fixed"] not in text:
            problems.append(f"{path} 不含 {item['fixed']!r}")
    return problems


def _install_module() -> Any:
    """驱动 install.py（上一级目录）：最新部署收据的判定与驱动 verify 同一函数。"""

    spec = importlib.util.spec_from_file_location("arm64_capture_driver_install_for_fix", DRV.parent / "install.py")
    if spec is None or spec.loader is None:
        raise FixAndContinueError("无法加载驱动 install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def latest_deploy_receipt(params: Mapping[str, str]) -> tuple[Path, dict[str, Any]]:
    install = _install_module()
    try:
        return install.latest_deploy_receipt(Path(params["D"]) / "control")
    except install.DriverError as error:
        raise FixAndContinueError(str(error)) from error


def deploy_receipt_problems(receipt: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    problems = [f"{key} 期望 {value!r} 实际 {receipt.get(key)!r}" for key, value in expected.items() if receipt.get(key) != value]
    if receipt.get("rehearsal") is not None:
        problems.append("最新部署收据是演练收据")
    return problems


def code_diff(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
    """非注释、非空行的差异（与 r20 postdeploy 守护核对同一口径）。"""

    removed: list[str] = []
    added: list[str] = []
    for line in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        body = line[1:].strip()
        if not body or body.startswith("#"):
            continue
        (removed if line.startswith("-") else added).append(body)
    return removed, added


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def staging_state(params: Mapping[str, str]) -> tuple[str | None, bool]:
    tree = Path(params["STAGING_TREE"])
    if not tree.is_dir():
        return None, False
    head = _git(tree, "rev-parse", "HEAD")
    status = _git(tree, "status", "--porcelain")
    if head.returncode != 0 or status.returncode != 0:
        return None, False
    return head.stdout.strip(), status.stdout.strip() == ""


def _alive(pid: int) -> bool:
    if pid < 2:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def _pid_from(path: Path) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _cmdline(pid: int) -> str:
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.is_file():
        try:
            return proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return result.stdout.strip()


def active_runs(state_dir: Path) -> list[str]:
    """只读 state.json 的 state 字段：运行中／已 prepared 的父 run（批次间隙之外禁止部署、对账与重派）。"""

    found: list[str] = []
    if not state_dir.is_dir():
        return found
    for state_path in sorted(state_dir.glob("run-*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(state, dict) and state.get("state") in {"running", "prepared"}:
            found.append(state_path.parent.name)
    return found


def driver_processes(params: Mapping[str, str]) -> list[str]:
    """vc5-recover／vc5-start 写的 PID 文件指向仍活着的驱动进程。"""

    pid = _pid_from(Path(params["RUNROOT"]) / "vc5-run-batch.pid")
    if pid is not None and _alive(pid) and "vc5-" in _cmdline(pid):
        return [f"PID {pid}（{_cmdline(pid)[:160]}）"]
    return []


def _managed(params: Mapping[str, str], name: str) -> Any:
    """导入数据根里的受管模块（只读调用）；PYTHONDONTWRITEBYTECODE 与 sys.dont_write_bytecode 保证不留 __pycache__。"""

    data = params["D"]
    if sys.path[:1] != [data]:
        sys.path.insert(0, data)
    return importlib.import_module(f"tools.official_client_capture.{name}")


# ---------------------------------------------------------------------------
# 对账扫描（监督器同一判据）
# ---------------------------------------------------------------------------


def scan_reconciliation_targets(
    campaign_dir: Path,
    state_dir: Path,
    *,
    campaign_id: str,
    target_attempt: str,
    supervisor: Any,
    exclude_runs: Iterable[str] = (),
    revisit: Iterable[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """扫描监督器状态目录，列出链尾失败父 run 还缺的对账（只读）。

    * 链尾：本 Campaign 的父 run 按 ``started_at_epoch`` 排序，最后一个正常结束（stopped）之后的全部 run。
      更早的失败 run 在当时已由后继协议承接，重新对账只会把早已处理过的根因再计一次数（根因全局累计，
      计数到上限即暂停），所以不碰；
    * 链尾里 failed／watchdog-aborted 的 run：run 期间（``started_at_utc`` 不早于父 run 开始）有未收口预约的，
      按监督器 ``_reservations_in_run_window``（与 reconciler 父 run 对账同判据）改走 attempt 对账——目标
      attempt 留给 reconcile-attempt 步骤；没有预约的走 reconcile-supervisor-run；
    * 已有对账收据的，按监督器 ``verify_*_reconciliation_binding`` 完整核验（收据与项目总账绑定），核验不过记为
      problems（失败关闭，不重复对账）；
    * 链尾里其它终态（audit-incomplete、aborted_prepared）不在本脚本处理范围，记为 unsupported；
      运行中／prepared 的 run 记为 active；
    * 第 59／62 项 ``revisit``（``{"kind", "subject"}``，来自上一次 reconcile-runs 停下记录）：受管对账器先写对账收据、
      入总账，再判定——判暂停（任何暂停种类）的对象，收据按监督器判据核验是通过的。这些对象核验通过后仍列为待对账
      （条目带 ``revisit: true``，并记入 ``result["revisit"]``），不能据收据当作已对账跳过；核验不过照旧记 problems；
      不在链尾的不再处理。
    """

    campaign_dir = Path(campaign_dir)
    state_dir = Path(state_dir)
    excluded = set(exclude_runs)
    wanted = {(str(item.get("kind")), str(item.get("subject"))) for item in revisit}
    result: dict[str, Any] = {
        "active": [], "unsupported": [], "foreign": [], "tail": [], "pending": [], "reconciled": [],
        "problems": [], "excluded": [], "target_in_window": False, "revisit": [],
    }

    def settle(entry: dict[str, Any], kind: str, subject: str) -> None:
        """对账收据核验通过：前次判定暂停（revisit）的仍待重新对账，其余记为已对账。"""

        if (kind, subject) in wanted:
            result["pending"].append({**entry, "revisit": True})
            result["revisit"].append({"kind": kind, "subject": subject})
        else:
            result["reconciled"].append(entry)

    if state_dir.is_symlink() or not state_dir.is_dir():
        result["problems"].append(f"监督器状态目录不存在或不可信：{state_dir}")
        return result
    runs: list[tuple[float, Path, Mapping[str, Any]]] = []
    for run_dir in sorted(state_dir.iterdir()):
        if not run_dir.name.startswith("run-"):
            continue
        if run_dir.is_symlink() or not run_dir.is_dir():
            result["problems"].append(f"父 run 目录不可信：{run_dir}")
            continue
        try:
            state = supervisor._read_state(run_dir)
        except Exception as error:  # 受管校验失败即失败关闭
            result["problems"].append(f"父 run {run_dir.name} 的 state 无法校验：{error}")
            continue
        runs.append((float(state["started_at_epoch"]), run_dir, state))
    runs.sort(key=lambda row: (row[0], row[1].name))
    own: list[tuple[float, Path, Mapping[str, Any]]] = []
    for row in runs:
        if row[2].get("campaign_id") != campaign_id:
            result["foreign"].append(row[1].name)
            continue
        if row[2].get("state") in supervisor.ACTIVE_STATES:
            result["active"].append(row[1].name)
        own.append(row)
    last_ok = max((index for index, row in enumerate(own) if row[2].get("state") == "stopped"), default=-1)
    seen: set[str] = set()
    label = "fix-and-continue 对账扫描"
    for started, run_dir, state in own[last_ok + 1:]:
        name = run_dir.name
        result["tail"].append(name)
        status = state.get("state")
        if status in supervisor.ACTIVE_STATES:
            continue
        if name in excluded:
            result["excluded"].append(name)
            continue
        if status not in {"failed", "watchdog-aborted"}:
            result["unsupported"].append({"run_id": name, "state": status})
            continue
        try:
            reservations = supervisor._reservations_in_run_window(campaign_dir, started)
        except Exception as error:  # 预约收据读不了（权限、格式）即失败关闭，并指明是哪个 run
            result["problems"].append(f"父 run {name} 的窗口内预约无法按监督器判据读取：{error}")
            continue
        if reservations:
            for candidate_id, subject, root in reservations:
                attempt_id, _separator, revision = str(subject).partition(":")
                if attempt_id == target_attempt and not revision:
                    result["target_in_window"] = True
                    continue
                if subject in seen:
                    continue
                seen.add(subject)
                receipt = campaign_dir / "control" / "reconciliation" / f"attempt-{str(subject).replace(':', '-')}" / "attempt-reconciliation.json"
                entry = {"kind": "attempt", "run_id": name, "attempt_id": attempt_id,
                         "recovery_revision": revision or None, "candidate_id": candidate_id}
                if receipt.is_file() and not receipt.is_symlink():
                    try:
                        supervisor.verify_attempt_reconciliation_binding(
                            campaign_dir, campaign_id=campaign_id, candidate_id=candidate_id, attempt_root=Path(root), label=label
                        )
                    except Exception as error:
                        result["problems"].append(f"attempt {subject} 的对账收据核验不过：{error}")
                    else:
                        settle(entry, "attempt", str(subject))
                else:
                    result["pending"].append(entry)
            continue
        receipt = campaign_dir / "control" / "reconciliation" / f"run-{name}" / "supervisor-run-reconciliation.json"
        entry = {"kind": "supervisor-run", "run_id": name, "run_dir": str(run_dir)}
        if receipt.is_file() and not receipt.is_symlink():
            manifest_path = run_dir / "campaign-run-manifest.json"
            manifest: Mapping[str, Any] = {}
            if manifest_path.is_file():
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                inner = loaded.get("manifest") if isinstance(loaded, dict) else None
                manifest = inner if isinstance(inner, dict) else {}
            try:
                supervisor.verify_supervisor_run_reconciliation_binding(
                    campaign_dir, campaign_id=campaign_id, run_id=name, phase=state.get("phase"),
                    batch_sequence=manifest.get("batch_sequence"), batch_sha256=manifest.get("batch_sha256"), label=label,
                )
            except Exception as error:
                result["problems"].append(f"父 run {name} 的对账收据核验不过：{error}")
            else:
                settle(entry, "supervisor-run", name)
        else:
            result["pending"].append(entry)
    return result


def prior_revisit(params: Mapping[str, str]) -> list[dict[str, str]]:
    """上一次 reconcile-runs 记录里判暂停、还没重新对账到 recoverable 的对象（第 59 项起，第 62 项推广到全部暂停种类）。

    受管对账器先写对账收据、入总账再判定，暂停对象的收据按监督器判据核验是通过的；扫描若据此当作已对账，续跑时暂停
    判定就被悄悄丢掉（根因上限形同虚设、延期或补账后也拿不到 reconcile-run-passed 账本事件）。停下记录带上这些对象，
    续跑扫描时重新对账。
    """

    record = read_step(params, "reconcile-runs") or {}
    if record.get("schema_version") != STEP_SCHEMA:
        return []
    items = (record.get("summary") or {}).get("revisit")
    keys: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("kind") in REVISIT_KINDS and isinstance(item.get("subject"), str):
            key = {"kind": str(item["kind"]), "subject": str(item["subject"])}
            if key not in keys:
                keys.append(key)
    return keys


def _merge_keys(*groups: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for group in groups:
        for item in group:
            key = {"kind": str(item["kind"]), "subject": str(item["subject"])}
            if key not in merged:
                merged.append(key)
    return merged


# ---------------------------------------------------------------------------
# 受管命令输出的读取与对账结果判定
# ---------------------------------------------------------------------------


def read_raw(prefix: str) -> tuple[int, Any, str]:
    """读 fix-and-continue.sh 落盘的受管命令原始输出：(退出码, stdout JSON 或 None, stderr)。"""

    base = Path(prefix)
    rc_text = Path(f"{base}.rc").read_text(encoding="utf-8").strip() if Path(f"{base}.rc").is_file() else "255"
    stdout = Path(f"{base}.out").read_text(encoding="utf-8", errors="replace") if Path(f"{base}.out").is_file() else ""
    stderr = Path(f"{base}.err").read_text(encoding="utf-8", errors="replace") if Path(f"{base}.err").is_file() else ""
    try:
        payload = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    return int(rc_text) if rc_text.lstrip("-").isdigit() else 255, payload, stderr


def _tail(text: str, lines: int = 12) -> str:
    return " ⏎ ".join(line for line in text.strip().splitlines()[-lines:])


PAUSE_HINTS = {
    "accounting": "账务暂停：按总账未决 operation 执行 accounting-resolve preview/apply（无法核清时需人给出估计上界与来源审计文件）",
    "environment": "环境污染暂停：修复环境、取得晚于污染的干净环境复核与环境修复记录后执行 environment-isolate preview/apply",
    "request_budget": "请求预算耗尽：由人批准新预算后执行 request-budget-extend preview/apply",
    # 第 62 项：deadline 的续跑步骤由 pause_resume_step 定为 pre-extend，提示里不再各自写 --from（统一在末尾给一个）。
    "deadline": ("预算截止暂停：阶段层在参数文件填 EXTEND_DEADLINE／EXTEND_REASON（pre-extend 先于对账执行阶段延期）；"
                 "Campaign／项目层由人批准 deadline-extend"),
}


def pause_kinds(payload: Any) -> list[str]:
    """对账暂停判定的种类；受管输出缺 pause_kinds 时按 deadline（与受管 _paused_next_command 同一口径）。"""

    decision = payload.get("decision") if isinstance(payload, dict) and isinstance(payload.get("decision"), dict) else {}
    return [str(kind) for kind in decision.get("pause_kinds") or ["deadline"]]


def pause_resume_step(kinds: Iterable[str], step: str) -> str:
    """对账判暂停而停下时的续跑步骤（第 62 项），``step`` 是停下的步骤。

    含 deadline：pre-extend——阶段延期（EXTEND_DEADLINE）只在对账之前的 pre-extend 执行，从对账步骤续跑会绕过延期、
    再次暂停；Campaign／项目层由人延期后从 pre-extend 续跑同样可行（不给 EXTEND_DEADLINE 时它幂等跳过）。
    其余种类（accounting／environment／request_budget／root_cause_repair／受管将来新增的种类）：由人按受管提示处理
    或由本脚本在步骤内登记根因修复，之后从停下的步骤续跑。两种续跑步骤都在停下步骤之前或就是它，前序记录齐全，
    一定能通过 check_from。
    """

    return "pre-extend" if "deadline" in kinds else step


def root_cause_actions(reasons: str) -> str:
    """根因重试达上限暂停要做的事（第 59 项；续跑步骤由调用方统一加在提示末尾）。

    Campaign 账本 stop_required 只能由人执行 campaign-resume（本脚本不代办）；项目总账根因达上限由本脚本按参数给的材料
    执行 record-root-cause-repair（reconcile-runs 在步骤内、reconcile-attempt 交给 repair 步骤，同一判定、命令与核对）后
    重新对账。
    """

    parts: list[str] = []
    if CAMPAIGN_ROOT_CAUSE_STOP in reasons:
        parts.append("Campaign 账本同根因重试达上限（stop_required）：须由人执行 campaign-resume（本脚本不代办）")
    if PROJECT_ROOT_CAUSE_LIMIT in reasons or not parts:
        parts.append(
            "项目总账根因重试达上限：在参数文件填写 REPAIR_ROOT_CAUSES／REPAIR_NOTE 与回归收据 REGRESSION_RECEIPT（或草稿 "
            "REGRESSION_DRAFT），由本脚本执行 record-root-cause-repair 登记（与 repair 步骤同一判定、命令与核对）后重新对账"
        )
    return "；".join(parts)


def pause_hint(kinds: Iterable[str], reasons: str, resume_step: str) -> str:
    """暂停提示：逐个种类写要做的事，末尾恰好一个"之后 --from <续跑步骤>"（第 59／62 项）。

    此前 accounting／environment／request_budget 的提示没有 --from，deadline 写 --from pre-extend 而停下记录的续跑行是
    对账步骤，两者对不上；现在提示与续跑行是同一个步骤，照做一定能通过 check_from。
    """

    actions = [
        root_cause_actions(reasons) if kind == "root_cause_repair"
        else PAUSE_HINTS.get(kind, f"暂停种类 {kind}：按受管工具提示处理")
        for kind in kinds
    ]
    return "；".join(actions) + f"；之后 --from {resume_step}"


def judge_reconciliation(payload: Any, rc: int, stderr: str, *, step: str) -> tuple[str, str, str, str]:
    """把对账（reconcile-supervisor-run／reconcile-attempt）结果归为 (类别, 原因, 下一步, 续跑步骤)。

    类别：recoverable；paused；stop（永久停线／需审核）；failed（命令本身失败或输出不可解析）。``step`` 是判定所在
    的续跑步骤：暂停时续跑步骤按 ``pause_resume_step``（含 deadline 回到 pre-extend），暂停提示里的 ``--from`` 与它一致；
    其余类别的续跑步骤就是 ``step``（第 59／62 项）。
    """

    if not isinstance(payload, dict):
        return "failed", f"对账命令失败（rc={rc}）：{_tail(stderr) or '无输出'}", "按报错修复后重跑本步骤", step
    status = payload.get("status")
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    upstream = str(payload.get("next_command") or "")
    reasons = "；".join(str(item) for item in decision.get("reasons") or [])
    if status == "recoverable" and rc == 0:
        return "recoverable", "", upstream, step
    if status == "paused":
        kinds = pause_kinds(payload)
        resume = pause_resume_step(kinds, step)
        return ("paused", f"对账判定暂停（{'、'.join(kinds)}）：{reasons}",
                f"{pause_hint(kinds, reasons, resume)}。受管工具提示：{upstream}", resume)
    if status == "permanent_stop":
        terminal = decision.get("terminal_reason")
        return "stop", f"对账判定永久停线（{terminal}）：{reasons}", f"永久停线由人裁定；受管工具提示：{upstream}", step
    if status in {"stage_review_required", "review_required"}:
        return "stop", f"对账判定需审核（{status}）：{reasons}", f"由人审核后处理；受管工具提示：{upstream}", step
    return ("failed", f"对账结果不可识别（status={status!r}，rc={rc}）：{_tail(stderr)}",
            f"人工核对原始输出；受管工具提示：{upstream}", step)


REDIRECT_RE = re.compile(r"属于 attempt 中断；请改用 reconcile-attempt[^：]*：(?P<items>.+)$", re.M)


def parse_redirect(stderr: str) -> list[tuple[str, str | None]] | None:
    """reconcile-supervisor-run 拒绝"run 期间已有预约"时给出的 attempt 列表（恢复段为 <id>:ar<k>）。"""

    match = REDIRECT_RE.search(stderr)
    if match is None:
        return None
    items: list[tuple[str, str | None]] = []
    for raw in match.group("items").strip().split("、"):
        attempt_id, _separator, revision = raw.strip().partition(":")
        if not ID_RE.fullmatch(attempt_id) or (revision and not re.fullmatch(r"ar[1-9][0-9]*", revision)):
            return None
        items.append((attempt_id, revision or None))
    return items or None


# ---------------------------------------------------------------------------
# 实测日志与回归收据
# ---------------------------------------------------------------------------


def test_signature(params: Mapping[str, str]) -> str:
    spec = {"modules": params["ITEM_TESTS"].split(), "k": params.get("ITEM_TESTS_K", "").split(),
            "k_modules": params.get("ITEM_TESTS_K_MODULES", "").split()}
    return _sha256_bytes(json.dumps(spec, sort_keys=True).encode("utf-8"))[:16]


def parse_test_log(text: str) -> dict[str, Any]:
    fields = dict(re.findall(r"(?m)^(head|tests|segments|staging-clean|exit)=(\S*)$", text))
    return {
        "head": fields.get("head"),
        "tests": fields.get("tests"),
        "segments": int(fields["segments"]) if fields.get("segments", "").isdigit() else None,
        "staging_clean": fields.get("staging-clean"),
        "exit": int(fields["exit"]) if fields.get("exit", "").lstrip("-").isdigit() else None,
        "ok": len(re.findall(r"(?m)^OK(?: \(.*\))?$", text)),
        "ran": [int(value) for value in re.findall(r"(?m)^Ran (\d+) tests? in ", text)],
        "failed": bool(re.search(r"(?m)^FAILED \(", text)),
    }


def test_log_problems(params: Mapping[str, str], text: str) -> list[str]:
    parsed = parse_test_log(text)
    expected_segments = 2 if params.get("ITEM_TESTS_K") else 1
    problems: list[str] = []
    if parsed["head"] != params["HEAD_COMMIT"]:
        problems.append(f"日志的 head={parsed['head']} 不是部署 HEAD")
    if parsed["tests"] != test_signature(params):
        problems.append("日志的实测用例签名与当前参数不一致")
    if parsed["exit"] != 0:
        problems.append(f"exit={parsed['exit']}")
    if parsed["staging_clean"] != "yes":
        problems.append(f"staging-clean={parsed['staging_clean']}")
    if parsed["segments"] != expected_segments or len(parsed["ran"]) != expected_segments or parsed["ok"] != expected_segments:
        problems.append(f"应有 {expected_segments} 段各自 OK，实际段 {parsed['segments']}／Ran {len(parsed['ran'])}／OK {parsed['ok']}")
    if parsed["failed"]:
        problems.append("日志含 FAILED")
    return problems


def _log_marker(path: Path, pattern: str) -> bool:
    if not path.is_file():
        return False
    return re.search(pattern, path.read_text(encoding="utf-8", errors="replace"), re.M) is not None


def regression_problems(payload: Any, params: Mapping[str, str], causes: Iterable[str]) -> list[str]:
    causes = set(causes)
    if not isinstance(payload, dict):
        return ["回归收据不是 JSON 对象"]
    problems: list[str] = []
    if payload.get("schema_version") != REGRESSION_SCHEMA:
        problems.append(f"schema_version 不是 {REGRESSION_SCHEMA}")
    if payload.get("fix_commit_sha") != params["REPAIR_FIX_COMMIT"]:
        problems.append(f"fix_commit_sha={payload.get('fix_commit_sha')!r} 不是 REPAIR_FIX_COMMIT")
    declared = [payload["root_cause_id"]] if "root_cause_id" in payload else list(payload.get("root_cause_ids") or [])
    if declared and not set(declared) <= causes:
        problems.append(f"收据的根因 {declared} 不在 REPAIR_ROOT_CAUSES 内")
    tests = ((payload.get("targeted_regression") or {}).get("arm64_real_check") or {}).get("tests")
    if isinstance(tests, dict) and tests.get("path"):
        log = Path(str(tests["path"]))
        if not log.is_file():
            problems.append(f"收据引用的实测日志不存在：{log}")
        elif tests.get("sha256") != _sha256_file(log):
            problems.append(f"收据引用的实测日志摘要不符：{log}")
    return problems


def build_regression_receipt(params: Mapping[str, str], draft: Mapping[str, Any]) -> dict[str, Any]:
    """草稿 + 本轮客观事实（实测日志、最新部署收据、记录时间）→ 回归收据（与 r20 repair 脚本同构）。"""

    log = _out(params) / "item-tests.log"
    record = read_step(params, "item-tests")
    if record is None or record.get("status") not in {"passed", "skipped"} or not log.is_file():
        raise FixAndContinueError("本轮实测没有通过记录，不能生成回归收据")
    text = log.read_text(encoding="utf-8", errors="replace")
    if test_log_problems(params, text):
        raise FixAndContinueError("本轮实测日志未通过核对，不能生成回归收据")
    ran = parse_test_log(text)["ran"]
    deploy_path, _receipt = latest_deploy_receipt(params)
    receipt = json.loads(json.dumps(draft))
    targeted = receipt.setdefault("targeted_regression", {})
    real = targeted.setdefault("arm64_real_check", {})
    tests = real.get("tests") if isinstance(real.get("tests"), dict) else {}
    real["tests"] = {
        **tests,
        "path": str(log),
        "sha256": _sha256_file(log),
        "ran": ran,
        "result": tests.get("result") or (
            f"部署用 staging 树（{params['HEAD_COMMIT']}）上实测 {'＋'.join(str(n) for n in ran)} 条 OK，staging 树前后干净"
        ),
    }
    deployment = targeted.get("deployment_receipt") if isinstance(targeted.get("deployment_receipt"), dict) else {}
    targeted["deployment_receipt"] = {**deployment, "path": str(deploy_path), "sha256": _sha256_file(deploy_path)}
    receipt["recorded_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return receipt


def registered_repairs(params: Mapping[str, str]) -> tuple[Path, dict[str, set[str]]]:
    """项目总账里已登记的 root_cause_repaired：{根因 ID: {修复提交…}}（只读，受管 _load_events 带链校验）。"""

    ledger = _managed(params, "codex_upgrade_project_ledger")
    root = ledger.find_project_ledger(Path(params["C"]))
    if root is None:
        raise FixAndContinueError("找不到 Campaign 所属的项目总账")
    registered: dict[str, set[str]] = {}
    for event in ledger._load_events(Path(root)):
        if event.get("event_type") != "root_cause_repaired":
            continue
        payload = event.get("payload") or {}
        causes = [payload["root_cause_id"]] if "root_cause_id" in payload else list(payload.get("root_cause_ids") or [])
        fix = (payload.get("bindings") or {}).get("fix_commit_sha")
        for cause in causes:
            registered.setdefault(str(cause), set()).add(str(fix))
    return Path(root), registered


# ---------------------------------------------------------------------------
# decide：逐步判定
# ---------------------------------------------------------------------------

Decider = Callable[[dict[str, str], argparse.Namespace], "tuple[int, dict[str, str]]"]
DECIDERS: dict[str, Decider] = {}


def decider(name: str) -> Callable[[Decider], Decider]:
    def register(function: Decider) -> Decider:
        DECIDERS[name] = function
        return function

    return register


def _ok(**exports: Any) -> tuple[int, dict[str, str]]:
    return EXIT_OK, {key: str(value) for key, value in exports.items()}


@decider("preflight")
def _preflight(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "preflight", args.run_stamp
    resume = args.resume_from or STEPS[0]
    out = _out(params)
    (out / "raw").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out, 0o700)
    os.chmod(out / "raw", 0o700)
    active = active_runs(Path(params["VC_STATE_DIR"]))
    if active:
        return stop(params, step, "needs-operator", f"监督器状态目录有运行中／prepared 的父 run：{active}",
                     "修好接着跑只能在批次间隙执行：等该批次结束（或按其终态对账）后再运行", run_stamp=stamp,
                     resume_from=resume), {}
    processes = driver_processes(params)
    if processes:
        return stop(params, step, "needs-operator", f"候选续跑驱动仍在运行：{processes}",
                    "等 vc5-recover／vc5-start 结束后再运行", run_stamp=stamp, resume_from=resume), {}
    try:
        load_expect(Path(params["EXPECT"]))
        load_entries(Path(params["ENTRY_GREPS"]))
    except FixAndContinueError as error:
        return stop(params, step, "failed", str(error), "修正本地生成的期望摘要／入口断言文件并重新上传", run_stamp=stamp,
                    resume_from=resume), {}
    if "UPLOAD_SHA256" in params:
        manifest = Path(params["UPLOAD_SHA256"])
        problems: list[str] = []
        if manifest.is_symlink() or not manifest.is_file():
            problems.append(f"上传清单不存在：{manifest}")
        else:
            for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                match = re.fullmatch(r"([0-9a-f]{64})\s+\*?([A-Za-z0-9_.@+=,:-][A-Za-z0-9_./@+=,:-]*)", line.strip())
                if match is None or ".." in Path(match.group(2)).parts:
                    problems.append(f"上传清单第 {number} 行格式非法")
                    continue
                target = manifest.parent / match.group(2)
                if not target.is_file() or _sha256_file(target) != match.group(1):
                    problems.append(f"{target} 与上传清单摘要不符")
        if problems:
            return stop(params, step, "failed", "；".join(problems), "重新上传本轮材料并核对 sha256 清单", run_stamp=stamp,
                        resume_from=resume), {}
    return _ok()


@decider("deploy-state")
def _deploy_state(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "deploy", args.run_stamp
    out = _out(params)
    inflight_path = out / "deploy.inflight.json"
    expect = load_expect(Path(params["EXPECT"]))
    if inflight_path.is_file():
        inflight = json.loads(inflight_path.read_text(encoding="utf-8"))
        log, pid_file = Path(inflight["log"]), Path(inflight["pid_file"])
        if _log_marker(log, r"^exit="):
            return _ok(ACTION="verify", LOG=log, PIDF=pid_file)
        pid = _pid_from(pid_file)
        if pid is not None and _alive(pid):
            return _ok(ACTION="wait", LOG=log, PIDF=pid_file)
        return stop(params, step, "needs-operator",
                    f"上一次受监督部署没有 exit 标记且进程已不在（日志 {log}）：部署中途被打断",
                    f"人工核对 {log}、最新部署收据与回滚备份；确认生产工具树一致后删除 {inflight_path} 再 --from deploy",
                    run_stamp=stamp), {}
    head, clean = staging_state(params)
    try:
        receipt_path, receipt = latest_deploy_receipt(params)
        receipt_ok = not deploy_receipt_problems(receipt, expect["deploy_receipt"])
    except FixAndContinueError:
        receipt_path, receipt_ok = None, False
    if head == params["HEAD_COMMIT"] and clean and receipt_ok and receipt_path is not None:
        return skip(params, step, f"staging 已是 {head[:12]} 的干净检出且最新部署收据 passed、摘要与期望一致", run_stamp=stamp,
                    summary={"receipt": str(receipt_path), "receipt_sha256": _sha256_file(receipt_path)}), {}
    reuse = "1" if head == params["HEAD_COMMIT"] and clean else "0"
    note(params, step, staging_head_before=head, staging_clean_before=clean, reuse_staging=reuse == "1")
    return _ok(ACTION="deploy", REUSE_STAGING=reuse,
               LOG=out / "raw" / f"{stamp}-deploy.log", PIDF=out / "raw" / f"{stamp}-deploy.pid")


@decider("deploy-launch")
def _deploy_launch(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    _write_private_json(_out(params) / "deploy.inflight.json",
                        {"log": args.log, "pid_file": args.pid_file, "started_at_utc": _utc_now()})
    return _ok()


@decider("deploy-verify")
def _deploy_verify(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "deploy", args.run_stamp
    log = Path(args.log)
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    exits = re.findall(r"(?m)^exit=(-?\d+)$", text)
    inflight = _out(params) / "deploy.inflight.json"
    if inflight.exists():
        inflight.unlink()
    if not exits or exits[-1] != "0":
        return stop(params, step, "failed", f"受监督部署失败（{exits[-1] if exits else '无 exit 标记'}）：{_tail(text, 20)}",
                    f"查看 {log}：修复后重新生成 bundle 与期望摘要，或确认失败原因后 --from deploy", run_stamp=stamp), {}
    expect = load_expect(Path(params["EXPECT"]))
    try:
        receipt_path, receipt = latest_deploy_receipt(params)
    except FixAndContinueError as error:
        return stop(params, step, "failed", str(error), "人工核对 control/ 下的部署收据", run_stamp=stamp), {}
    problems = deploy_receipt_problems(receipt, expect["deploy_receipt"])
    if problems:
        return stop(params, step, "failed", f"最新部署收据 {receipt_path.name} 与期望摘要不符：{problems}",
                    "核对本地按部署 HEAD 预算的期望摘要（EXPECT）与部署收据；不一致说明部署内容不是本轮提交", run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={"log": str(log), "receipt": str(receipt_path),
                                                            "receipt_sha256": _sha256_file(receipt_path)}), {}


@decider("postdeploy-receipt")
def _postdeploy_receipt(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    head, clean = staging_state(params)
    if head != params["HEAD_COMMIT"] or not clean:
        return stop(params, step, "failed", f"staging 树 HEAD={head} clean={clean}，不是部署 HEAD 的干净检出",
                    "先 --from deploy", run_stamp=stamp, resume_from="deploy"), {}
    expect = load_expect(Path(params["EXPECT"]))
    try:
        receipt_path, receipt = latest_deploy_receipt(params)
    except FixAndContinueError as error:
        return stop(params, step, "failed", str(error), "人工核对 control/ 下的部署收据", run_stamp=stamp), {}
    problems = deploy_receipt_problems(receipt, expect["deploy_receipt"])
    supervisor_file = Path(params["D"]) / "tools" / "official_client_capture" / "codex_upgrade_supervisor.py"
    actual = _sha256_file(supervisor_file) if supervisor_file.is_file() else None
    if actual != expect["deploy_receipt"]["supervisor_sha256"]:
        problems.append(f"数据根监督器 sha256={actual} 不是期望的 {expect['deploy_receipt']['supervisor_sha256']}")
    closure = None
    if expect.get("wire_closure_sha256"):
        try:
            identity_payload = _managed(params, "codex_upgrade")._tool_identity(include_git=False)
            closure = identity_payload["orchestrator_closures"]["wire_producer"]["closure_sha256"]
        except Exception as error:  # 受管计算失败即失败关闭
            problems.append(f"数据根 wire 闭包无法计算：{error}")
        else:
            if closure != expect["wire_closure_sha256"]:
                problems.append(f"数据根 wire 闭包 {closure} 不是期望的 {expect['wire_closure_sha256']}")
    if problems:
        return stop(params, step, "failed", "；".join(problems),
                    "部署后事实与期望摘要不符：核对 EXPECT 是否按部署 HEAD 预算、部署是否完整", run_stamp=stamp), {}
    note(params, step, receipt=str(receipt_path), receipt_sha256=_sha256_file(receipt_path), wire_closure_sha256=closure)
    return _ok(GUARD_SERVICE=expect["guard"]["service"])


@decider("sync-deploy-script")
def _sync_deploy_script(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    expect = load_expect(Path(params["EXPECT"]))
    source = Path(params["STAGING_TREE"]) / "tools" / "arm64_supervised_deploy.py"
    target = Path(params["D"]) / "tools" / "arm64_supervised_deploy.py"
    if not source.is_file():
        return stop(params, step, "failed", f"staging 树缺少部署脚本：{source}", "先 --from deploy", run_stamp=stamp,
                    resume_from="deploy"), {}
    data = source.read_bytes()
    synced = False
    if not target.is_file() or target.read_bytes() != data:
        backup = Path(params["RUNROOT"]) / "stale-tools-backup" / f"arm64_supervised_deploy.py.pre-{params['ROUND']}"
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_file() and not backup.exists():
            backup.write_bytes(target.read_bytes())
            backup.chmod(0o600)
        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        temporary.write_bytes(data)
        temporary.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(temporary, 0, 0)
        os.replace(temporary, target)
        synced = True
    if target.read_bytes() != data:
        return stop(params, step, "failed", "数据根部署脚本副本同步后与 staging 不一致", "人工核对", run_stamp=stamp), {}
    digest = expect["deploy_receipt"]["supervisor_sha256"]
    if f'"{digest}"' not in data.decode("utf-8", "replace"):
        return stop(params, step, "failed", f"部署脚本不含新监督器摘要常量 {digest}",
                    "部署脚本的 DEFAULT_SUPERVISOR_DIGEST 与期望监督器摘要不一致：核对本轮提交", run_stamp=stamp), {}
    note(params, step, deploy_script_synced=synced, deploy_script_sha256=_sha256_bytes(data))
    return _ok()


@decider("guard-check")
def _guard_check(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    guard = load_expect(Path(params["EXPECT"]))["guard"]
    unit = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file and Path(args.file).is_file() else ""
    exec_lines = [line[len("ExecStart="):] for line in unit.splitlines() if line.startswith("ExecStart=")]
    problems: list[str] = []
    script: Path | None = None
    if not exec_lines or len(exec_lines[0].split()) < 2:
        problems.append(f"systemctl cat {guard['service']} 没有可解析的 ExecStart")
    else:
        script = Path(exec_lines[0].split()[1])
    baseline = Path(guard["baseline_file"])
    if script is not None:
        if not script.is_file() or not baseline.is_file():
            problems.append(f"守护脚本 {script} 或基线 {baseline} 不存在")
        elif _sha256_file(script) != _sha256_file(baseline):
            problems.append(f"守护脚本 {script} 与基线 {baseline} 不同（守护代码已变）")
        else:
            staging = Path(params["STAGING_TREE"]) / "tools" / "arm64_supervised_deploy.py"
            removed, added = code_diff(script.read_text(encoding="utf-8"), staging.read_text(encoding="utf-8"))
            if removed != guard["diff_removed"] or added != guard["diff_added"]:
                problems.append(f"守护所用部署脚本与新部署脚本的非注释差异不符期望：删 {removed} 增 {added}")
    status_path = Path(guard.get("status_path", DEFAULT_GUARD_STATUS))
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        ready = status["shared_protection"]["status"] == "compliant" and all(
            service["status"] == "compliant" and service["admission_state"] == "ready" for service in status["services"].values()
        )
        services = {name: (service.get("status"), service.get("admission_state")) for name, service in status["services"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        ready, services = False, {"error": str(error)}
    if not ready:
        problems.append(f"守护状态未就绪：{services}")
    if problems:
        return stop(params, step, "failed", "守护核对不过：" + "；".join(problems),
                    "本脚本不重装出口守护：守护逻辑有变时按守护变更流程人工处理；状态未就绪时等守护恢复。处理后 --from postdeploy",
                    run_stamp=stamp), {}
    note(params, step, guard_script=str(script), guard_services=services)
    return _ok()


@decider("driver-state")
def _driver_state(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    target = Path(params["DRIVER_TARGET"])
    staging_manifest = Path(params["STAGING_TREE"]) / "tools" / "arm64_capture_driver" / "manifest.json"
    installed_manifest = target / "manifest.json"
    same = False
    try:
        same = (json.loads(staging_manifest.read_text(encoding="utf-8")).get("manifest_sha256")
                == json.loads(installed_manifest.read_text(encoding="utf-8")).get("manifest_sha256"))
    except (OSError, ValueError, AttributeError):
        same = False
    if args.verify_rc == 0 and same:
        note(params, "postdeploy", driver="already-installed")
        return _ok(ACTION="keep")
    return _ok(ACTION="install")


@decider("driver-after")
def _driver_after(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    if args.install_rc != 0 or args.verify_rc != 0:
        return stop(params, step, "failed", f"驱动重装或复验失败（install rc={args.install_rc}，verify rc={args.verify_rc}）",
                    "查看本步骤 raw 输出；驱动安装收据必须绑定最新部署收据", run_stamp=stamp), {}
    note(params, step, driver="reinstalled")
    return _ok()


@decider("entry-assertions")
def _entry_assertions(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    entries = load_entries(Path(params["ENTRY_GREPS"]))
    problems = entry_problems(params, entries)
    if problems:
        return stop(params, step, "failed", "入口断言不过：" + "；".join(problems),
                    "数据根或驱动缺少本轮入口：核对部署 HEAD 与入口断言文件", run_stamp=stamp), {}
    note(params, step, entry_assertions=len(entries))
    return _ok()


def self_digest() -> str:
    """本脚本自身（入口、辅助与共用解析／等待模块）的摘要：驱动被本轮重装更新后须用新驱动续跑。"""

    parts = []
    for name in ("fix-and-continue.sh", "fix_and_continue.py", "parse_env.py", "wait_state.py"):
        path = DRV / name
        parts.append(f"{name}:{_sha256_file(path) if path.is_file() else '-'}")
    return _sha256_bytes("\n".join(parts).encode("utf-8"))


@decider("postdeploy-final")
def _postdeploy_final(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "postdeploy", args.run_stamp
    installed = (Path(params["DRIVER_TARGET"]) / "driver").resolve()
    passed(params, step, run_stamp=stamp)
    if DRV == installed and args.self_sha and self_digest() != args.self_sha:
        print("==== 驱动已被本轮重装更新（本脚本自身变化）：请用新驱动续跑\n"
              f"续跑：bash {_script()} {params['PARAMS_PATH']} --from item-tests\n"
              "FIX_AND_CONTINUE_RESTART resume_from=item-tests", file=sys.stderr)
        return EXIT_RESTART, {}
    return _ok()


@decider("tests-state")
def _tests_state(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "item-tests", args.run_stamp
    head, clean = staging_state(params)
    if head != params["HEAD_COMMIT"] or not clean:
        return stop(params, step, "failed", f"staging 树 HEAD={head} clean={clean}，不是部署 HEAD 的干净检出",
                    "先 --from deploy", run_stamp=stamp, resume_from="deploy"), {}
    out = _out(params)
    log, pid_file = out / "item-tests.log", out / "item-tests.pid"
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        if parse_test_log(text)["exit"] is not None:
            problems = test_log_problems(params, text)
            if not problems:
                return skip(params, step, f"本轮实测日志已通过（{log}）", run_stamp=stamp,
                            summary={"log": str(log), "sha256": _sha256_file(log), "ran": parse_test_log(text)["ran"]}), {}
            parsed = parse_test_log(text)
            suffix = "stale" if parsed["head"] != params["HEAD_COMMIT"] or parsed["tests"] != test_signature(params) else "failed"
            log.rename(out / f"item-tests.log.{suffix}-{stamp}")
        else:
            pid = _pid_from(pid_file)
            if pid is not None and _alive(pid):
                return _ok(ACTION="wait", LOG=log, PIDF=pid_file)
            log.rename(out / f"item-tests.log.interrupted-{stamp}")
    if pid_file.exists():
        pid_file.unlink()
    return _ok(ACTION="run", LOG=log, PIDF=pid_file)


@decider("tests-verdict")
def _tests_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "item-tests", args.run_stamp
    log = Path(args.log)
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    problems = test_log_problems(params, text) if text else ["实测日志不存在"]
    if problems:
        return stop(params, step, "failed", f"实测未通过：{'；'.join(problems)}（日志 {log}）",
                    f"查看 {log}：工具缺陷则修复后开新一轮；环境偶发则 --from item-tests 重跑（失败日志会改名留档）",
                    run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={"log": str(log), "sha256": _sha256_file(log),
                                                           "ran": parse_test_log(text)["ran"]}), {}


@decider("evolution-status")
def _evolution_status(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "evolution", args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    if rc != 0 or not isinstance(payload, dict):
        return stop(params, step, "failed", f"tool-evolution-status 失败（rc={rc}）：{_tail(stderr)}", "按报错处理",
                    run_stamp=stamp), {}
    drift = payload.get("unregistered_drift") or []
    if args.mode == "final":
        if drift or payload.get("status") != "registered":
            return stop(params, step, "failed", f"登记后仍有未登记漂移：{drift}", "人工核对演进链", run_stamp=stamp), {}
        return passed(params, step, run_stamp=stamp, summary={"effective_index": payload.get("effective_index")}), {}
    if not drift and payload.get("status") == "registered":
        return skip(params, step, f"当前受管工具已登记（effective_index={payload.get('effective_index')}），无未登记漂移",
                    run_stamp=stamp, summary={"effective_index": payload.get("effective_index")}), {}
    note(params, step, unregistered_drift=drift, effective_index_before=payload.get("effective_index"))
    return _ok()


@decider("evolution-preview")
def _evolution_preview(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "evolution", args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    if rc != 0 or not isinstance(payload, dict):
        # 策略演进（要人给兼容收据与激活认证）、影响已封存作业、总账未决账务：都要人裁定，本脚本不代办。
        needs = any(marker in stderr for marker in ("策略演进", "已封存", "accounting-resolve", "blocked"))
        return stop(params, step, "needs-operator" if needs else "failed", f"工具演进预览失败（rc={rc}）：{_tail(stderr)}",
                    "由人按报错裁定（策略演进须给兼容收据与激活认证；影响已封存作业须走评估恢复；账务未决先补账）后 --from evolution"
                    if needs else "按报错处理（Campaign 未静默时等批次结束）后 --from evolution", run_stamp=stamp), {}
    expect = load_expect(Path(params["EXPECT"]))
    wanted = {"wire_closure_changed": False, **(expect.get("evolution") or {})}
    changes = payload.get("changes") or {}
    review = str(payload.get("review_sha256") or "")
    problems: list[str] = []
    if payload.get("status") != "approval_required" or not SHA256_RE.fullmatch(review):
        problems.append(f"预览状态 {payload.get('status')!r} 或 review_sha256 非法")
    if (payload.get("bindings") or {}).get("fix_commit") != params["FIX_COMMIT"]:
        problems.append("预览绑定的修复提交不是 FIX_COMMIT")
    for key, value in wanted.items():
        if changes.get(key) is not value:
            problems.append(f"{key}={changes.get(key)!r}，期望 {value!r}")
    if payload.get("policy_transition"):
        problems.append("本次是策略演进（需兼容收据与激活认证）")
    summary = {
        "preview_index": payload.get("index"),
        "impact_paths": changes.get("impact_paths"),
        "unmapped_paths": changes.get("unmapped_paths"),
        "wire_closure_changed": changes.get("wire_closure_changed"),
        "evidence_closure_changed": changes.get("evidence_closure_changed"),
        "official_affected": ((payload.get("impact") or {}).get("official") or {}).get("affected_job_ids"),
        "candidates": {name: part.get("affected_job_ids") for name, part in ((payload.get("impact") or {}).get("candidates") or {}).items()},
        "evaluator_changed": (payload.get("evaluator") or {}).get("changed_fields"),
        "review_sha256": review,
    }
    note(params, step, preview=summary)
    print(f"  [evolution] 预览：{json.dumps(summary, ensure_ascii=False)}", file=sys.stderr)
    if problems:
        return stop(params, step, "needs-operator", "工具演进预览与本轮期望不符：" + "；".join(problems),
                    "wire 闭包变化会让失败 attempt 的对账判 identity_changed 永久停线，须按 Formal 后继流程由人处理；"
                    "期望本就如此时在 EXPECT.evolution 写明后 --from evolution", run_stamp=stamp), {}
    return _ok(REVIEW_SHA256=review)


@decider("evolution-apply")
def _evolution_apply(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "evolution", args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    if rc != 0 or not isinstance(payload, dict) or payload.get("status") != "evolution_applied":
        return stop(params, step, "failed", f"工具演进登记失败（rc={rc}）：{_tail(stderr)}", "按报错处理后 --from evolution",
                    run_stamp=stamp), {}
    note(params, step, applied={"path": payload.get("path"), "receipt_sha256": payload.get("receipt_sha256")})
    return _ok()


def _deadlines(params: Mapping[str, str]) -> dict[str, Any]:
    artifacts = _managed(params, "codex_upgrade_vc_artifacts")
    return dict(artifacts.effective_deadlines(Path(params["C"]), project_ledger_optional=True))


@decider("extend-state")
def _extend_state(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = ("pre-extend" if args.mode == "pre" else "extend"), args.run_stamp
    if "EXTEND_DEADLINE" not in params:
        return skip(params, step, "参数文件未给 EXTEND_DEADLINE，不做阶段延期", run_stamp=stamp), {}
    try:
        deadlines = _deadlines(params)
    except Exception as error:
        return stop(params, step, "failed", f"读取有效截止失败：{error}", "人工核对计时账本与项目总账", run_stamp=stamp), {}
    wanted = _parse_utc(params["EXTEND_DEADLINE"], "EXTEND_DEADLINE")
    current_raw = deadlines.get("stage_deadline_at_utc")
    phase = deadlines.get("phase")
    summary = {"phase": phase, "stage_deadline_before": current_raw, "extend_deadline": params["EXTEND_DEADLINE"],
               "status_before_pause": deadlines.get("status_before_pause"), "paused_scopes": deadlines.get("paused_scopes")}
    if current_raw is not None and _parse_utc(str(current_raw), "阶段截止") >= wanted:
        return skip(params, step, f"阶段截止 {current_raw} 已不早于 EXTEND_DEADLINE", run_stamp=stamp, summary=summary), {}
    if phase != params["EXTEND_PHASE"] or current_raw is None:
        if args.mode == "pre":
            return skip(params, step, f"当前阶段 {phase!r} 不是 {params['EXTEND_PHASE']}（候选审核等状态下阶段未开）："
                                      "授权重开阶段后由 extend 步骤延期", run_stamp=stamp, summary=summary), {}
        return stop(params, step, "needs-operator", f"授权后当前阶段是 {phase!r}（阶段截止 {current_raw}），不是 {params['EXTEND_PHASE']}",
                    "人工核对计时账本状态后决定延期层级", run_stamp=stamp, summary=summary), {}
    if wanted <= datetime.now(timezone.utc):
        return stop(params, step, "needs-operator", f"EXTEND_DEADLINE={params['EXTEND_DEADLINE']} 已不晚于当前时间",
                    "由人给出新的阶段截止写入参数文件后续跑", run_stamp=stamp, summary=summary), {}
    note(params, step, **summary)
    return _ok(ACTION="extend")


@decider("extend-preview")
def _extend_preview(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = ("pre-extend" if args.mode == "pre" else "extend"), args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    review = str((payload or {}).get("review_sha256") or "") if isinstance(payload, dict) else ""
    preview = str((payload or {}).get("preview_path") or "") if isinstance(payload, dict) else ""
    if rc != 0 or not isinstance(payload, dict) or payload.get("status") != "preview" or not SHA256_RE.fullmatch(review) \
            or not preview.startswith(params["C"] + "/") or not Path(preview).is_file():
        return stop(params, step, "failed", f"延期预览失败（rc={rc}）：{_tail(stderr)}", "按报错处理", run_stamp=stamp), {}
    if payload.get("new_deadline_at_utc") not in (None, params["EXTEND_DEADLINE"]):
        return stop(params, step, "failed", "延期预览的新截止与参数不一致", "人工核对", run_stamp=stamp), {}
    note(params, step, extend_preview=preview, extend_review_sha256=review)
    return _ok(EXT_PREVIEW=preview, EXT_SHA256=review)


@decider("extend-apply")
def _extend_apply(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = ("pre-extend" if args.mode == "pre" else "extend"), args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    if rc != 0 or not isinstance(payload, dict) or payload.get("status") != "extended":
        return stop(params, step, "failed", f"延期写入失败（rc={rc}）：{_tail(stderr)}",
                    "按报错处理（延期 apply 可按同一预览重跑补齐两本账）", run_stamp=stamp), {}
    deadlines = _deadlines(params)
    current = deadlines.get("stage_deadline_at_utc")
    if current is None or _parse_utc(str(current), "阶段截止") < _parse_utc(params["EXTEND_DEADLINE"], "EXTEND_DEADLINE"):
        return stop(params, step, "failed", f"延期写入后阶段截止仍是 {current}", "人工核对两本账", run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={"stage_deadline_after": current,
                                                           "receipt_path": payload.get("receipt_path")}), {}


@decider("scan-runs")
def _scan_runs(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "reconcile-runs", args.run_stamp
    final = args.mode == "final"
    # 第 59／62 项：上一次停下时判暂停（任何种类）的对象，续跑扫描时重新对账（收据虽已写，判定没有通过）。收尾扫描不带入：
    # 本次运行里这些对象要么已重新对账到 recoverable（从 revisit 移除），要么已让本步骤停下。
    carried = [] if final else prior_revisit(params)
    try:
        supervisor = _managed(params, "codex_upgrade_supervisor")
        result = scan_reconciliation_targets(Path(params["C"]), Path(params["VC_STATE_DIR"]), campaign_id=params["CAMPAIGN"],
                                             target_attempt=params["ATTEMPT"], supervisor=supervisor,
                                             exclude_runs=args.exclude_run or (), revisit=carried)
    except Exception as error:
        return stop(params, step, "failed", f"对账扫描失败：{error}", "人工核对监督器状态目录与受管监督器版本", run_stamp=stamp,
                    summary={"revisit": carried} if carried else None), {}
    summary = {key: result[key] for key in ("tail", "foreign", "reconciled", "excluded", "target_in_window")}
    summary["redirected_runs"] = list(args.exclude_run or [])
    if not final:
        # 扫描没走完就停下时照旧带上前次的暂停对象（下次续跑仍重新对账）；走完则以本次在链尾找到的为准。
        blocked = bool(result["active"] or result["unsupported"] or result["problems"])
        summary["revisit"] = _merge_keys(carried, result["revisit"]) if blocked else result["revisit"]
    if result["active"]:
        return stop(params, step, "needs-operator", f"有运行中／prepared 的父 run：{result['active']}", "等批次结束后再运行",
                    run_stamp=stamp, summary=summary), {}
    if result["unsupported"]:
        return stop(params, step, "needs-operator", f"链尾父 run 的终态不在本脚本处理范围：{result['unsupported']}",
                    "audit-incomplete／aborted_prepared 由人按受管工具提示处理后再续跑", run_stamp=stamp, summary=summary), {}
    if result["problems"]:
        return stop(params, step, "failed", "对账扫描发现不可信事实：" + "；".join(result["problems"]),
                    "已有对账收据核验不过属完整性问题，人工核对，不重复对账", run_stamp=stamp, summary=summary), {}
    if final:
        leftover = _partial(params, step).get("revisit") or []
        if leftover:
            return stop(params, step, "failed", f"前次判定暂停的对象本次没有重新对账到 recoverable：{leftover}",
                        "人工核对本步骤记录与 raw 输出后 --from reconcile-runs", run_stamp=stamp, summary=summary), {}
    if not result["pending"]:
        if final:
            return passed(params, step, run_stamp=stamp, summary=summary), {}
        return skip(params, step, f"链尾失败父 run 均已对账（链尾 {result['tail']}）", run_stamp=stamp, summary=summary), {}
    if final:
        return stop(params, step, "failed", f"对账后仍有待对账：{result['pending']}",
                    "扫描判据与对账器不一致：人工核对后再续跑", run_stamp=stamp, summary=summary), {}
    pending_file = _out(params) / "raw" / f"{stamp}-reconcile-runs-pending.tsv"
    rows = []
    for item in result["pending"]:
        if item["kind"] == "attempt":
            rows.append("\t".join(["attempt", item["run_id"], item["attempt_id"], item["recovery_revision"] or "-"]))
        else:
            rows.append("\t".join(["supervisor-run", item["run_id"], item["run_dir"], "-"]))
    pending_file.write_text("\n".join(rows) + "\n", encoding="utf-8")
    pending_file.chmod(0o600)
    note(params, step, pending=result["pending"], **summary)
    return _ok(PENDING_FILE=pending_file)


@decider("run-verdict")
def _run_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    """reconcile-runs 里单个对账的判定；父 run 对账被拒且提示改走 attempt 时输出 REDIRECT。

    第 59 项：暂停只因项目总账根因达上限、且参数给了登记材料（与 reconcile-attempt 旁路同一判据 ``_paused_needs_repair``）
    时输出 ACTION=repair——fix-and-continue.sh 在本步骤内按 repair 步骤同一路径登记（``root_cause_repair inline``），再以
    ``--mode after-repair`` 对该对象重新对账一次；重新对账仍暂停即停下（不再登记，不循环）。
    第 62 项：任何暂停种类（deadline／accounting／environment／request_budget／root_cause_repair／未知种类）停下的对象都
    记入 ``revisit``——受管对账器先写收据、入总账再判定，停下后续跑扫描时据此重新对账，而不是当作已对账跳过。暂停的续跑
    步骤按 ``pause_resume_step``（含 deadline 是 pre-extend，否则是本步骤），提示里的 ``--from`` 与之一致；永久停线、需审核
    与命令失败不进 revisit，续跑步骤是本步骤（行为不变）。
    """

    step, stamp = "reconcile-runs", args.run_stamp
    after_repair = args.mode == "after-repair"
    rc, payload, stderr = read_raw(args.raw)
    partial = _partial(params, step)
    key = {"kind": args.kind, "subject": args.subject}
    revisit = [item for item in partial.get("revisit") or [] if item != key]
    if args.kind == "supervisor-run" and rc != 0 and payload is None:
        redirect = parse_redirect(stderr)
        if redirect is not None:
            # 父 run 的对账归到 attempt 对账（收尾扫描排除该 run），它本身不再需要重新对账。
            note(params, step, revisit=revisit)
            return _ok(ACTION="redirect", REDIRECT=" ".join(f"{a}:{r or '-'}" for a, r in redirect))
    category, reason, hint, resume = judge_reconciliation(payload, rc, stderr, step=step)
    if category == "recoverable":
        done = list(partial.get("done") or [])
        entry = {"kind": args.kind, "subject": args.subject, "next_command": hint}
        if after_repair:
            entry["after_root_cause_repair"] = True
        done.append(entry)
        note(params, step, done=done, revisit=revisit)
        return _ok(ACTION="done")
    if category == "paused":
        # 第 62 项：任何暂停都记入 revisit（收据虽已写，判定没有通过；续跑时重新对账）。
        revisit.append(key)
        if not after_repair and _paused_needs_repair(params, payload):
            decision = payload.get("decision") or {}
            repairs = list(partial.get("root_cause_repairs") or [])
            repairs.append({"kind": args.kind, "object": args.subject, "action": "planned", "pause_kinds": pause_kinds(payload),
                            "reasons": decision.get("reasons"),
                            "root_cause": (payload.get("root_cause") or {}).get("root_cause_id")})
            note(params, step, revisit=revisit, root_cause_repairs=repairs)
            print(f"  [{step}] {args.kind} {args.subject}：项目总账根因达上限暂停，参数已给登记材料——本步骤内登记根因修复后重新对账",
                  file=sys.stderr)
            return _ok(ACTION="repair")
        note(params, step, revisit=revisit)
        if after_repair:
            reason = f"登记根因修复后重新对账仍暂停——{reason}"
            hint = (f"本步骤已按参数登记根因修复（REPAIR_ROOT_CAUSES={params.get('REPAIR_ROOT_CAUSES')}，修复提交 "
                    f"{params['REPAIR_FIX_COMMIT'][:12]}），重新对账仍判暂停：核对暂停原因里的根因是否都在 REPAIR_ROOT_CAUSES 内、"
                    f"修复是否对症（需要新的修复提交就开新一轮）；{hint}")
    status = "failed" if category == "failed" else "needs-operator"
    return stop(params, step, status, f"{args.kind} {args.subject}：{reason}", hint, run_stamp=stamp, resume_from=resume), {}


def _paused_needs_repair(params: Mapping[str, str], payload: Mapping[str, Any]) -> bool:
    """暂停只因项目总账根因达上限、且参数给了根因修复登记材料：按根因修复登记路径登记后再对账。

    reconcile-attempt 与 reconcile-runs 两处同一判据（第 59 项）：reconcile-attempt 记 passed（needs_repair）交给 repair
    步骤登记、approve 重新对账；reconcile-runs 在 repair 之前，在本步骤内登记后对该对象重新对账。Campaign 账本
    stop_required（原因含"Campaign 账本同根因重试已达上限"）不走此路径，只能由人 campaign-resume。
    """

    decision = payload.get("decision") or {}
    kinds = set(decision.get("pause_kinds") or [])
    reasons = " ".join(str(item) for item in decision.get("reasons") or [])
    material = "REGRESSION_RECEIPT" in params and (Path(params["REGRESSION_RECEIPT"]).is_file() or "REGRESSION_DRAFT" in params)
    return (kinds == {"root_cause_repair"} and CAMPAIGN_ROOT_CAUSE_STOP not in reasons
            and bool(params.get("REPAIR_ROOT_CAUSES")) and material)


def _preview_facts(params: Mapping[str, str], payload: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    preview = payload.get("recovery_preview") if isinstance(payload.get("recovery_preview"), dict) else {}
    review = str(preview.get("review_sha256") or "")
    path = str(payload.get("recovery_preview_path") or "")
    problems: list[str] = []
    if not SHA256_RE.fullmatch(review):
        problems.append("恢复预览 review_sha256 非法")
    expected_dir = f"{params['C']}/control/reconciliation/attempt-{params['ATTEMPT']}/"
    if not path.startswith(expected_dir) or not Path(path).is_file():
        problems.append(f"恢复预览路径 {path!r} 不在 {expected_dir} 下或不存在")
    if payload.get("attempt_id") not in (None, params["ATTEMPT"]):
        problems.append("对账输出的 attempt 不是目标 ATTEMPT")
    return review, path, problems


@decider("attempt-verdict")
def _attempt_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step = "reconcile-attempt" if args.mode == "reconcile" else "approve"
    stamp = args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    # 暂停时两种模式都从 reconcile-attempt 续跑（含 deadline 时从 pre-extend，第 62 项），见本函数末尾 stop 的 resume_from。
    # 目标 attempt 在 reconcile-attempt／approve 每次都重新对账（不经扫描、不看收据），不存在"暂停后续跑被跳过"。
    category, reason, hint, resume = judge_reconciliation(payload, rc, stderr, step="reconcile-attempt")
    if category == "recoverable":
        check = (payload.get("resume_reuse_check") or {}) if isinstance(payload, dict) else {}
        if check.get("status") == "inconsistent":
            return stop(params, step, "needs-operator", f"恢复预览与 resume 复用判定不一致：{check.get('reason')}",
                        "按原因修复后 --from reconcile-attempt（不一致的预览不可批准）", run_stamp=stamp,
                        resume_from="reconcile-attempt"), {}
        review, path, problems = _preview_facts(params, payload)
        if problems:
            return stop(params, step, "failed", "；".join(problems), "人工核对对账输出", run_stamp=stamp), {}
        summary = {"decision": "recoverable", "review_sha256": review, "preview_path": path,
                   "root_cause": (payload.get("root_cause") or {}).get("root_cause_id"),
                   "root_cause_counts": (payload.get("project_head") or {}).get("root_cause_counts"),
                   "execute_job_ids": (payload.get("recovery_preview") or {}).get("execute_job_ids"),
                   "reuse_count": len((payload.get("recovery_preview") or {}).get("reuse_job_ids") or []),
                   "next_command": payload.get("next_command")}
        if args.mode == "reconcile":
            return passed(params, step, run_stamp=stamp, summary=summary), {}
        note(params, step, **summary)
        return _ok(REVIEW_SHA256=review, PREVIEW_PATH=path)
    if category == "paused" and args.mode == "reconcile" and _paused_needs_repair(params, payload):
        return passed(params, step, run_stamp=stamp, summary={
            "decision": "paused", "needs_repair": True, "pause_kinds": (payload.get("decision") or {}).get("pause_kinds"),
            "reasons": (payload.get("decision") or {}).get("reasons"),
            "root_cause": (payload.get("root_cause") or {}).get("root_cause_id")}), {}
    status = "failed" if category == "failed" else "needs-operator"
    return stop(params, step, status, reason, hint, run_stamp=stamp,
                resume_from=resume if category == "paused" else None), {}


def plan_root_cause_repair(params: Mapping[str, str], *, on_written: Callable[[Path], None]) -> dict[str, Any]:
    """根因修复登记计划：repair 步骤与 reconcile-runs 步骤内登记共用的同一判定（第 59 项从 repair 步骤原样抽出）。

    只读判定＋回归收据生成与校验，按原顺序返回 ``outcome`` 之一：
      no_causes       参数没给 REPAIR_ROOT_CAUSES；
      ledger_error    读项目总账失败（error）；
      already         根因都已登记同一修复提交——幂等跳过，不再执行登记命令（causes、fix）；
      no_receipt      没给回归收据，或收据不存在又没给草稿（causes、evidence_hint）；
      material_error  草稿／回归收据不合格或读不到最新部署收据（error、evidence_hint）；
      ready           可以登记（todo、ledger_root、receipt_path、deploy_path）。
    回归收据不存在时由草稿补本轮实测日志与最新部署收据后 O_EXCL 写一次（写后回调 ``on_written``），随后与已存在的收据
    一样逐项校验（schema、fix_commit_sha、根因集合、引用的实测日志摘要）。
    """

    causes = params.get("REPAIR_ROOT_CAUSES", "").split()
    if not causes:
        return {"outcome": "no_causes"}
    try:
        ledger_root, registered = registered_repairs(params)
    except Exception as error:
        return {"outcome": "ledger_error", "error": str(error)}
    fix = params["REPAIR_FIX_COMMIT"]
    todo = [cause for cause in causes if fix not in registered.get(cause, set())]
    if not todo:
        return {"outcome": "already", "causes": causes, "fix": fix}
    receipt_path = Path(params["REGRESSION_RECEIPT"]) if "REGRESSION_RECEIPT" in params else None
    log = _out(params) / "item-tests.log"
    evidence_hint = f"本轮实测日志 {log}（sha256 {_sha256_file(log) if log.is_file() else '无'}）"
    if receipt_path is None or (not receipt_path.is_file() and "REGRESSION_DRAFT" not in params):
        return {"outcome": "no_receipt", "causes": causes, "evidence_hint": evidence_hint}
    try:
        if not receipt_path.is_file():
            draft = _read_json(Path(params["REGRESSION_DRAFT"]), "回归收据草稿 ")
            draft_problems = regression_problems({**draft, "targeted_regression": {}}, params, causes) if isinstance(draft, dict) else ["草稿不是对象"]
            if draft_problems:
                raise FixAndContinueError("回归收据草稿不合格：" + "；".join(draft_problems))
            payload = build_regression_receipt(params, draft)
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            on_written(receipt_path)
        payload = _read_json(receipt_path, "回归收据 ")
        problems = regression_problems(payload, params, causes)
        if problems:
            raise FixAndContinueError("回归收据不合格：" + "；".join(problems))
        deploy_path, _deploy = latest_deploy_receipt(params)
    except (FixAndContinueError, OSError) as error:
        return {"outcome": "material_error", "error": str(error), "evidence_hint": evidence_hint}
    return {"outcome": "ready", "causes": causes, "fix": fix, "todo": todo, "ledger_root": ledger_root,
            "receipt_path": receipt_path, "deploy_path": deploy_path}


def _inline_repair_entry(params: Mapping[str, str], step: str, args: argparse.Namespace,
                         expected: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """步骤内登记的当前条目：必须是本步骤对账判定 ACTION=repair 时记下的同一对象、处在期望阶段，否则失败关闭。"""

    repairs = list(_partial(params, step).get("root_cause_repairs") or [])
    entry = repairs[-1] if repairs and isinstance(repairs[-1], dict) else {}
    if (entry.get("kind"), entry.get("object"), entry.get("action")) != (args.kind, args.object, expected):
        raise FixAndContinueError(
            f"步骤内根因修复登记与本步骤的对账暂停记录对不上（期望 {args.kind} {args.object} 处于 {expected}）：{entry or '无记录'}"
        )
    return repairs, entry


def _repair_plan_inline(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    """reconcile-runs 步骤内登记（第 59 项）：同一判定，结果并入本步骤记录，停下的续跑步骤是本步骤。"""

    step, stamp = args.step, args.run_stamp
    if step not in INLINE_REPAIR_STEPS:
        raise FixAndContinueError(f"步骤内根因修复登记只用于 {sorted(INLINE_REPAIR_STEPS)}，不能在 {step} 里执行")
    repairs, entry = _inline_repair_entry(params, step, args, "planned")

    def written(path: Path) -> None:
        entry["regression_receipt_written"] = str(path)
        note(params, step, root_cause_repairs=repairs)

    plan = plan_root_cause_repair(params, on_written=written)
    outcome = plan["outcome"]
    if outcome in {"no_causes", "no_receipt"}:
        # 对账判定已按 _paused_needs_repair 核过材料，到这里说明判定之后材料没了：失败关闭。
        return stop(params, step, "needs-operator", "对账判定根因达上限暂停，但登记材料不全（REPAIR_ROOT_CAUSES 或回归收据／草稿不存在）",
                    f"填写 REPAIR_ROOT_CAUSES／REPAIR_NOTE 与回归收据（或草稿 REGRESSION_DRAFT）后 --from {step}",
                    run_stamp=stamp, resume_from=step), {}
    if outcome == "ledger_error":
        return stop(params, step, "failed", f"读取项目总账失败：{plan['error']}", f"人工核对项目总账后 --from {step}",
                    run_stamp=stamp, resume_from=step), {}
    if outcome == "material_error":
        return stop(params, step, "needs-operator", plan["error"],
                    f"修正回归收据后 --from {step}（可引用{plan['evidence_hint']}）", run_stamp=stamp, resume_from=step), {}
    if outcome == "already":
        entry.update(action="already", root_causes=plan["causes"], fix_commit_sha=plan["fix"])
        note(params, step, root_cause_repairs=repairs)
        print(f"  [{step}] 根因 {plan['causes']} 已登记同一修复提交 {plan['fix'][:12]}：不重复登记，重新对账", file=sys.stderr)
        return _ok(REPAIR_ACTION="already")
    receipt_path, deploy_path = plan["receipt_path"], plan["deploy_path"]
    entry.update(action="recording", todo=plan["todo"], fix_commit_sha=plan["fix"], regression_receipt=str(receipt_path),
                 regression_receipt_sha256=_sha256_file(receipt_path), deployment_receipt=str(deploy_path))
    note(params, step, root_cause_repairs=repairs)
    return _ok(REPAIR_ACTION="record", TODO_RCS=" ".join(plan["todo"]), LEDGER_DIR=plan["ledger_root"],
               REG_SHA256=_sha256_file(receipt_path), DEPLOY_RECEIPT_SHA256=_sha256_file(deploy_path))


@decider("repair-plan")
def _repair_plan(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    """根因修复登记判定。``--mode inline``：reconcile-runs 步骤内登记；其余（``step``）：repair 步骤，记录与提示同第 35 项。"""

    if args.mode == "inline":
        return _repair_plan_inline(params, args)
    step, stamp = "repair", args.run_stamp
    reconcile = read_step(params, "reconcile-attempt") or {}
    needs = bool((reconcile.get("summary") or {}).get("needs_repair"))
    plan = plan_root_cause_repair(params, on_written=lambda path: note(params, step, regression_receipt_written=str(path)))
    outcome = plan["outcome"]
    if outcome == "no_causes":
        if needs:
            return stop(params, step, "needs-operator", "对账判定根因达上限暂停，但参数文件没有根因修复登记材料",
                        "填写 REPAIR_ROOT_CAUSES／REPAIR_NOTE 与回归收据（或草稿）后 --from reconcile-attempt", run_stamp=stamp,
                        resume_from="reconcile-attempt"), {}
        return skip(params, step, "参数文件未给 REPAIR_ROOT_CAUSES，不登记根因修复", run_stamp=stamp), {}
    if outcome == "ledger_error":
        return stop(params, step, "failed", f"读取项目总账失败：{plan['error']}", "人工核对项目总账", run_stamp=stamp), {}
    if outcome == "already":
        causes, fix = plan["causes"], plan["fix"]
        return skip(params, step, f"根因 {causes} 已登记同一修复提交 {fix[:12]}", run_stamp=stamp,
                    summary={"root_causes": causes, "fix_commit_sha": fix}), {}
    if outcome == "no_receipt":
        if needs:
            return stop(params, step, "needs-operator", "登记根因修复需要回归收据（证据文件），参数文件未给或文件不存在",
                        f"按 arm64-code-regression-receipt/v1 生成回归收据（或提供草稿 REGRESSION_DRAFT），可引用{plan['evidence_hint']}；"
                        "然后 --from repair（record-root-cause-repair 由本脚本执行）", run_stamp=stamp), {}
        return skip(params, step, "未给回归收据：只做实测不登记（设计：REGRESSION_RECEIPT 可空）", run_stamp=stamp,
                    summary={"root_causes": plan["causes"]}), {}
    if outcome == "material_error":
        return stop(params, step, "needs-operator", plan["error"], f"修正回归收据后 --from repair（可引用{plan['evidence_hint']}）",
                    run_stamp=stamp), {}
    receipt_path, deploy_path = plan["receipt_path"], plan["deploy_path"]
    note(params, step, todo=plan["todo"], regression_receipt=str(receipt_path), regression_receipt_sha256=_sha256_file(receipt_path),
         deployment_receipt=str(deploy_path))
    return _ok(TODO_RCS=" ".join(plan["todo"]), LEDGER_DIR=plan["ledger_root"], REG_SHA256=_sha256_file(receipt_path),
               DEPLOY_RECEIPT_SHA256=_sha256_file(deploy_path))


@decider("repair-verdict")
def _repair_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    """登记命令的核对（两种模式同一核对）：命令成功且回显覆盖待登记根因，登记后总账里必须有该根因以同一修复提交的修复事件。"""

    inline = args.mode == "inline"
    step, stamp = (args.step if inline else "repair"), args.run_stamp
    resume = step if inline else None
    rc, payload, stderr = read_raw(args.raw)
    todo = set(args.subject.split()) if args.subject else set()
    repairs: list[dict[str, Any]] = []
    entry: dict[str, Any] = {}
    if inline:
        if step not in INLINE_REPAIR_STEPS:
            raise FixAndContinueError(f"步骤内根因修复登记只用于 {sorted(INLINE_REPAIR_STEPS)}，不能在 {step} 里执行")
        repairs, entry = _inline_repair_entry(params, step, args, "recording")
    if rc != 0 or not isinstance(payload, dict) or not todo <= set(payload.get("root_cause_ids") or []):
        return stop(params, step, "failed", f"record-root-cause-repair 失败（rc={rc}）：{_tail(stderr)}", f"按报错处理后 --from {step}",
                    run_stamp=stamp, resume_from=resume), {}
    _root, registered = registered_repairs(params)
    missing = [cause for cause in todo if params["REPAIR_FIX_COMMIT"] not in registered.get(cause, set())]
    if missing:
        return stop(params, step, "failed", f"登记后总账里仍没有 {missing} 的修复事件",
                    "人工核对项目总账" + (f"后 --from {step}" if inline else ""), run_stamp=stamp, resume_from=resume), {}
    facts = {"operation_id": payload.get("operation_id"), "receipt_sha256": payload.get("receipt_sha256"),
             "head_sequence": payload.get("head_sequence")}
    if inline:
        entry.update(action="recorded", **facts)
        note(params, step, root_cause_repairs=repairs)
        return _ok()
    return passed(params, step, run_stamp=stamp, summary=facts), {}


@decider("approve-verdict")
def _approve_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "approve", args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    category, reason, hint, resume = judge_reconciliation(payload, rc, stderr, step=step)
    if category != "recoverable":
        # 暂停时续跑步骤与提示一致（含 deadline 回到 pre-extend，第 62 项）；其余仍从本步骤续跑。
        return stop(params, step, "failed" if category == "failed" else "needs-operator", f"批准恢复预览失败：{reason}", hint,
                    run_stamp=stamp, resume_from=resume if category == "paused" else None), {}
    review, path, problems = _preview_facts(params, payload)
    approval = payload.get("recovery_approval") if isinstance(payload.get("recovery_approval"), dict) else None
    if review != args.subject:
        problems.append("批准后的预览摘要与本次运行预览输出的 review_sha256 不一致")
    if approval is None or approval.get("approved_sha256") not in (None, args.subject):
        problems.append("输出没有与该摘要一致的 recovery_approval")
    if Path(path).resolve() != Path(args.preview).resolve():
        problems.append("批准输出的预览路径与本次运行预览输出不一致")
    if problems:
        return stop(params, step, "failed", "；".join(problems), "人工核对恢复预览与批准收据", run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={"review_sha256": review, "preview_path": path,
                                                           "approval_index": approval.get("index")}), {}


@decider("need-preview")
def _need_preview(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    record = read_step(params, "approve") or {}
    summary = record.get("summary") or {}
    path, review = str(summary.get("preview_path") or ""), str(summary.get("review_sha256") or "")
    if record.get("status") != "passed" or not Path(path).is_file() or not SHA256_RE.fullmatch(review):
        return stop(params, args.step, "failed", "本轮 approve 步骤没有可用的已批准恢复预览", "先 --from approve",
                    run_stamp=args.run_stamp, resume_from="approve"), {}
    return _ok(PREVIEW=path, REVIEW_SHA256=review)


@decider("authorize-verdict")
def _authorize_verdict(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "authorize", args.run_stamp
    rc, payload, stderr = read_raw(args.raw)
    if rc != 0 or not isinstance(payload, dict) or payload.get("status") != "authorized":
        return stop(params, step, "failed", f"授权恢复预览失败（rc={rc}）：{_tail(stderr)}",
                    "账本 head 在批准后推进时须 --from approve 重新对账批准；其它按报错处理", run_stamp=stamp), {}
    problems = []
    if Path(str(payload.get("preview_path") or "")).resolve() != Path(args.preview).resolve():
        problems.append("授权的预览路径与批准步骤不一致")
    if payload.get("review_sha256") not in (None, args.subject):
        problems.append("授权的预览摘要与批准步骤不一致")
    if problems:
        return stop(params, step, "failed", "；".join(problems), "人工核对", run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={"timing_recovery_event": payload.get("timing_recovery_event"),
                                                           "next_command": payload.get("next_command")}), {}


@decider("accepted")
def _accepted(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "accepted", args.run_stamp
    try:
        deadlines = _deadlines(params)
    except Exception as error:
        return stop(params, step, "failed", f"读取计时账本失败：{error}", "人工核对", run_stamp=stamp), {}
    if deadlines.get("status_before_pause") in AUTHORIZABLE_LEDGER_STATES:
        return stop(params, step, "failed", f"账本仍是 {deadlines.get('status_before_pause')}：授权未生效",
                    "先 --from authorize（接受检查在可授权状态下会顺带写授权事件，本脚本不这样做）", run_stamp=stamp,
                    resume_from="authorize"), {}
    try:
        reconciler = _managed(params, "codex_upgrade_reconciler")
        consumed = reconciler.load_approved_recovery_preview(Path(params["C"]), Path(args.preview), phase="candidate",
                                                             candidate_id=params["CANDIDATE"])
    except Exception as error:
        return stop(params, step, "failed", f"已批准预览不再被接受：{error}", "按报错处理（通常须 --from approve 重新对账批准）",
                    run_stamp=stamp), {}
    event = consumed.get("timing_recovery_event")
    if isinstance(event, dict) and event.get("appended"):
        return stop(params, step, "needs-operator", "接受检查写入了授权事件（授权步骤未生效）", "人工核对计时账本", run_stamp=stamp), {}
    return passed(params, step, run_stamp=stamp, summary={
        "execute_job_ids": consumed.get("execute_job_ids"), "reuse_count": len(consumed.get("reuse_job_ids") or []),
        "stage_deadline_at_utc": deadlines.get("stage_deadline_at_utc")}), {}


@decider("recover-state")
def _recover_state(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    step, stamp = "recover", args.run_stamp
    record = read_step(params, step) or {}
    summary = record.get("summary") or {}
    if record.get("status") in {"passed", "skipped"}:
        # 同一轮次只派发一次：批次结束后再空跑时恢复预览可能已换成新索引，也不能据此再派一次（重派须开新轮次）。
        changed = "" if summary.get("preview_path") == args.preview else f"；当前批准预览已是 {args.preview}，如需按它重派请开新轮次"
        return skip(params, step, f"本轮已启动过 {CANDIDATE_RECOVER_SCRIPT}（日志 {summary.get('log')}），不重复派发{changed}",
                    run_stamp=stamp, summary=summary), {}
    active = active_runs(Path(params["VC_STATE_DIR"]))
    processes = driver_processes(params)
    if active or processes:
        return stop(params, step, "needs-operator", f"已有运行中的父 run {active} 或驱动进程 {processes}",
                    "等其结束后再续跑", run_stamp=stamp), {}
    script = Path(params["DRIVER_TARGET"]) / "driver" / CANDIDATE_RECOVER_SCRIPT
    if not script.is_file():
        return stop(params, step, "failed", f"驱动安装目标缺少 {script}", "先 --from postdeploy 重装驱动", run_stamp=stamp,
                    resume_from="postdeploy"), {}
    return _ok(ACTION="start", RECOVER_SCRIPT=script, RECOVER_LOG=Path(params["RUNROOT"]) / "vc5-recover.out")


@decider("recover-started")
def _recover_started(params: dict[str, str], args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    return passed(params, "recover", run_stamp=args.run_stamp, summary={
        "preview_path": args.preview, "log": args.log, "started_at_utc": _utc_now()}), {}


# ---------------------------------------------------------------------------
# run-tests：staging 树上的实测（由 setsid -f 启动）
# ---------------------------------------------------------------------------


def run_tests(params: Mapping[str, str], log: Path, pid_file: Path) -> int:
    """在部署用 staging 树上跑实测，写日志（头部 head／tests，尾部 segments／staging-clean／exit）。

    SIGHUP 先恢复默认处置：nohup 启动会让子进程继承 SIG_IGN，监督器的 hangup 用例就会必红
    （ARM64 已踩过）；字节码缓存放树外、不写 PYTHONPATH，结束后核对 staging 树仍干净。
    """

    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    tree = Path(params["STAGING_TREE"])
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(_out(params) / ".pycache-item-tests")
    commands = [[sys.executable, "-m", "unittest", *params["ITEM_TESTS"].split()]]
    if params.get("ITEM_TESTS_K"):
        second = [sys.executable, "-m", "unittest"]
        for pattern in params["ITEM_TESTS_K"].split():
            second.extend(["-k", pattern])
        commands.append(second + params["ITEM_TESTS_K_MODULES"].split())
    rc = 0
    segments = 0
    with log.open("w", encoding="utf-8") as handle:
        handle.write(f"head={params['HEAD_COMMIT']}\ntests={test_signature(params)}\nstarted_at_utc={_utc_now()}\n")
        handle.flush()
        for command in commands:
            segments += 1
            handle.write(f"== 第 {segments} 段：{' '.join(command[1:])}\n")
            handle.flush()
            rc = subprocess.run(command, cwd=tree, env=environment, stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL).returncode
            if rc != 0:
                break
        clean = _git(tree, "status", "--porcelain")
        handle.write(f"segments={segments}\n")
        handle.write(f"staging-clean={'yes' if clean.returncode == 0 and not clean.stdout.strip() else 'no'}\n")
        handle.write(f"finished_at_utc={_utc_now()}\n")
        handle.write(f"exit={rc}\n")
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _export_lines(values: Mapping[str, Any]) -> str:
    return "".join(f"export {key}={shlex.quote(str(value))}\n" for key, value in values.items())


def _assignment_lines(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def _load_or_exit(path: str) -> dict[str, str]:
    try:
        return load_params(Path(path))
    except (parse_env.EnvFileError, OSError, UnicodeDecodeError) as error:
        print(f"参数文件拒绝加载：{error}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("steps", help="按顺序列出步骤名")
    load = sub.add_parser("load-params", help="安全解析轮次参数文件，输出 export 赋值")
    load.add_argument("params")
    frm = sub.add_parser("check-from", help="--from 续跑前核对前序步骤记录")
    frm.add_argument("--params", required=True)
    frm.add_argument("--step", required=True)
    lst = sub.add_parser("list", help="列出本轮各步骤记录")
    lst.add_argument("--params", required=True)
    sub.add_parser("self-digest", help="本脚本自身摘要")
    st = sub.add_parser("stop", help="写停下记录并打印下一步")
    st.add_argument("--params", required=True)
    st.add_argument("--step", required=True)
    st.add_argument("--run-stamp")
    st.add_argument("--status", choices=("failed", "needs-operator"), required=True)
    st.add_argument("--reason", required=True)
    st.add_argument("--next", required=True)
    st.add_argument("--resume-from")
    fin = sub.add_parser("finish", help="写 passed 记录")
    fin.add_argument("--params", required=True)
    fin.add_argument("--step", required=True)
    fin.add_argument("--run-stamp")
    rt = sub.add_parser("run-tests", help="staging 树上的实测（由 setsid -f 启动）")
    rt.add_argument("--params", required=True)
    rt.add_argument("--log", required=True)
    rt.add_argument("--pid-file", required=True)
    dec = sub.add_parser("decide", help="逐步判定（退出码 0 继续／10 跳过／1 失败／4 需人工／5 驱动已更新）")
    dec.add_argument("name", choices=sorted(DECIDERS))
    dec.add_argument("--params", required=True)
    dec.add_argument("--step", required=True)
    dec.add_argument("--run-stamp")
    dec.add_argument("--resume-from")
    dec.add_argument("--raw")
    dec.add_argument("--mode")
    dec.add_argument("--kind")
    dec.add_argument("--subject")
    dec.add_argument("--object", help="步骤内根因修复登记所属的对账对象（第 59 项）")
    dec.add_argument("--log")
    dec.add_argument("--pid-file")
    dec.add_argument("--file")
    dec.add_argument("--preview")
    dec.add_argument("--self-sha")
    dec.add_argument("--verify-rc", type=int, default=0)
    dec.add_argument("--install-rc", type=int, default=0)
    dec.add_argument("--exclude-run", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "steps":
        print("\n".join(STEPS))
        return EXIT_OK
    if args.command == "self-digest":
        print(self_digest())
        return EXIT_OK
    if args.command == "load-params":
        values = _load_or_exit(args.params)
        sys.stdout.write(_export_lines(values))
        return EXIT_OK
    params = _load_or_exit(args.params)
    if args.command == "check-from":
        if args.step not in STEPS:
            print(f"未知步骤：{args.step}（可选：{' '.join(STEPS)}）", file=sys.stderr)
            return EXIT_USAGE
        problems = check_from(params, args.step)
        if problems:
            print("；".join(problems), file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    if args.command == "list":
        for step in STEPS:
            record = read_step(params, step)
            state = "（无记录）" if record is None else f"{record.get('status')}  {record.get('recorded_at_utc')}  {record.get('reason') or ''}"
            print(f"{step:18s} {state}")
        return EXIT_OK
    if args.command == "stop":
        return stop(params, args.step, args.status, args.reason, args.next, run_stamp=args.run_stamp,
                    resume_from=args.resume_from)
    if args.command == "finish":
        return passed(params, args.step, run_stamp=args.run_stamp)
    if args.command == "run-tests":
        return run_tests(params, Path(args.log), Path(args.pid_file))
    try:
        code, exports = DECIDERS[args.name](params, args)
    except FixAndContinueError as error:
        return stop(params, args.step, "failed", str(error), "按报错处理", run_stamp=args.run_stamp)
    except Exception as error:  # 判定自身异常也要留下步骤记录与续跑提示，不能只丢 traceback
        return stop(params, args.step, "failed", f"判定 {args.name} 异常：{type(error).__name__}: {error}",
                    "人工核对本步骤 raw 输出与现场事实", run_stamp=args.run_stamp)
    if code in (EXIT_OK, EXIT_SKIP) and exports:
        sys.stdout.write(_assignment_lines(exports))
    return code


if __name__ == "__main__":
    sys.exit(main())
