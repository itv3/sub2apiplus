#!/usr/bin/env python3
"""失败单元补跑策略（Codex 升级工具优化第 2 项）：失败分类、累计账本与“补跑通过”标注的自检。

为什么要有：执行器原来“一红整轮作废”——哪怕只是满载引起的一次偶发，整轮结论 failed、同提交同部署不可重启（一次 Codex 升级的后台验证
起了 16 次，因单元失败而重跑的 3 次，红的全是偶发或顺序错误）。补跑把代价从整轮约 2.4 小时降到几分钟，但它改变了“什么算
通过”，所以这里把限制条件写成可单独测试、可被结论与收尾校验重放的纯函数，不在执行器里零散判断。

* **只补跑“执行失败”**：失败按四类分类——``execution``（执行失败：命令正常退出但退出码非 0，或测试单元报告了失败／错误）、
  ``timeout``（超时被杀）、``read-set``（读集审计未闭合，单元本身执行成功）、``environment``（环境失败：被信号终止、命令找不到、
  测试单元没写出结果文件、整机预约中止前已登记失败）。只有 ``execution`` 可补跑；其余三类保持原来的行为（只做不改结论的
  诊断重跑）。拿不准的一律归 ``environment``，宁可不补跑。
* **补跑条件**（全部满足才补跑，由执行器判定，原因逐条写进标注）：配置里打开（缺省关）；有记录库可累计；单元是“可从头重跑”
  的（测试单元，或登记了 ``reservation_restartable`` 的命令单元——pre-A3 认证场景有副作用、不在其中）；本次运行没有对它做读集
  审计（审计下补跑得不到完整读集，不能顶替）；累计规则放行。
* **累计规则**（N＝5）：同一单元最近 5 轮（它被真正执行过的最近 5 次运行）里，补跑通过只许出现 1 次；窗口里已有一次补跑通过，
  这一次再红直接判红、不再补跑。按单元 ID 累计，账本写在记录库的 ``retry-ledger/`` 下（``RetryLedger``）。账本读不出来时按
  “不许补跑”处理，不会因为账本坏了而放宽。
* **标注**（``unit-executor-retry/v1``）：补跑过或被拒绝补跑的单元，汇总行与运行清单里都带一份标注，写明失败类别、两次尝试
  各自的记录摘要与日志摘要、窗口内的累计情况。补跑通过的单元算入结论，但结论里单列（``summary["retry"]``），两次日志都留。
  ``marker_problems`` 是标注的静态自检，结论（后台验证）与收尾门禁校验都用它，不静默放行。

本模块只依赖标准库，不导入执行器与记录模块（它们在受管工具的钉住摘要里，不能因为本策略而改）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

MARKER_SCHEMA = "unit-executor-retry/v1"
SUMMARY_SCHEMA = "unit-executor-retry-summary/v1"
LEDGER_SCHEMA = "unit-executor-retry-ledger/v1"
LEDGER_DIRECTORY = "retry-ledger"
# 累计窗口与放行上限的缺省值（方案第 2 项：N 取 5，窗口内补跑通过只许 1 次）。
DEFAULT_WINDOW_ROUNDS = 5
DEFAULT_PASS_LIMIT = 1
# 规则的底线（验收修正必修 2）：窗口至少 5 轮、窗口内补跑通过至多 1 次。配置与校验都钉死在这里——汇总（以及它带的标注）只能声明
# **更严**的值（窗口更长），声明更宽（窗口更短、上限更高）一律判问题。否则一份被改写的汇总写上 pass_limit=99 就能自证“还有补跑额度”。
POLICY_MIN_WINDOW_ROUNDS = DEFAULT_WINDOW_ROUNDS
POLICY_MAX_PASS_LIMIT = DEFAULT_PASS_LIMIT

CLASS_EXECUTION = "execution"
CLASS_TIMEOUT = "timeout"
CLASS_READ_SET = "read-set"
CLASS_ENVIRONMENT = "environment"
FAILURE_CLASSES = (CLASS_EXECUTION, CLASS_TIMEOUT, CLASS_READ_SET, CLASS_ENVIRONMENT)
CLASS_LABELS = {CLASS_EXECUTION: "执行失败", CLASS_TIMEOUT: "超时被杀", CLASS_READ_SET: "读集未闭合", CLASS_ENVIRONMENT: "环境失败"}

STATUS_PASSED = "retry-passed"
STATUS_FAILED = "retry-failed"
STATUS_DENIED = "retry-denied"
STATUS_NOT_RETRIABLE = "not-retriable"
STATUSES = (STATUS_PASSED, STATUS_FAILED, STATUS_DENIED, STATUS_NOT_RETRIABLE)

# 账本里一次运行对一个单元的结局。
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOMES = (OUTCOME_PASSED, OUTCOME_FAILED, STATUS_PASSED, STATUS_FAILED, STATUS_DENIED)

_SHA256 = re.compile(r"[0-9a-f]{64}")
# 命令单元的下列退出码说明“没有真正跑到被测内容”，属环境问题，不补跑（验收修正建议 9）：
# 124（timeout 类包装器超时）、125（包装器自己出错，如 go list 失败）、126／127（不可执行／找不到命令，含 go 不在 PATH 里）、
# 129～159（shell 约定的 128＋信号：被信号杀，如 137＝SIGKILL、143＝SIGTERM；包装器里 go 子进程被信号杀时也按这个约定退出）。
ENVIRONMENT_EXIT_CODES = frozenset({124, 125, 126, 127}) | frozenset(range(129, 160))


class RetryPolicyError(RuntimeError):
    """配置非法（执行器报“配置错误”，退出码 2）。"""


class LedgerError(RuntimeError):
    """账本读不出来或内容不合法：补跑一律拒绝（宁可红，不放宽）。"""


@dataclass(frozen=True)
class RetryConfig:
    enabled: bool = False
    window_rounds: int = DEFAULT_WINDOW_ROUNDS
    pass_limit: int = DEFAULT_PASS_LIMIT


def parse_config(raw: Any) -> RetryConfig:
    """调度配置里的 ``retry`` 段。没有这一段＝关闭（与改造前逐字相同）；写了就必须完整合法。"""

    if raw is None:
        return RetryConfig()
    if not isinstance(raw, Mapping) or set(raw) - {"enabled", "window_rounds", "pass_limit"} or "enabled" not in raw:
        raise RetryPolicyError("retry 配置只能含 enabled、window_rounds、pass_limit，且必须写 enabled")
    enabled = raw["enabled"]
    window = raw.get("window_rounds", DEFAULT_WINDOW_ROUNDS)
    limit = raw.get("pass_limit", DEFAULT_PASS_LIMIT)
    if not isinstance(enabled, bool):
        raise RetryPolicyError("retry.enabled 必须是布尔值")
    for name, value in (("window_rounds", window), ("pass_limit", limit)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RetryPolicyError(f"retry.{name} 必须是正整数")
    if limit > window:
        raise RetryPolicyError("retry.pass_limit 不能大于 window_rounds")
    if window < POLICY_MIN_WINDOW_ROUNDS or limit > POLICY_MAX_PASS_LIMIT:
        raise RetryPolicyError(f"retry 配置不得比规定宽松：window_rounds 至少 {POLICY_MIN_WINDOW_ROUNDS}、pass_limit 至多 {POLICY_MAX_PASS_LIMIT}")
    return RetryConfig(enabled=enabled, window_rounds=window, pass_limit=limit)


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------


def classify_failure(*, is_command: bool, exit_code: int | None, signal: int | None, timed_out: bool, has_result: bool,
                     result_successful: bool, audit_failed: bool, observed_failure: bool = False) -> str | None:
    """把一次未通过的正式执行归入四类之一；通过的执行返回 None。

    ``audit_failed``：单元本身执行成功，只因读集审计要求没满足而判未通过。``observed_failure``：整机预约中止前已登记了测试失败。
    判定顺序保证“越像环境问题越先归环境”，拿不准归 ``environment``（不补跑）。"""

    execution_ok = exit_code == 0 and signal is None and not timed_out and (is_command or (has_result and result_successful))
    if execution_ok and not audit_failed:
        return None
    if timed_out:
        return CLASS_TIMEOUT
    if execution_ok and audit_failed:
        return CLASS_READ_SET
    if observed_failure or signal is not None:
        return CLASS_ENVIRONMENT
    if exit_code in ENVIRONMENT_EXIT_CODES:
        return CLASS_ENVIRONMENT
    if is_command:
        return CLASS_ENVIRONMENT if exit_code is None else CLASS_EXECUTION
    # 测试单元：必须写出了结果文件，才能说是“测试报告了失败”；没写结果（进程崩在加载阶段等）归环境。
    if not has_result or exit_code is None:
        return CLASS_ENVIRONMENT
    return CLASS_EXECUTION


# ---------------------------------------------------------------------------
# 累计账本
# ---------------------------------------------------------------------------


class RetryLedger:
    """按单元 ID 累计的补跑账本：``<记录库>/retry-ledger/<单元 ID 摘要前 16 位>.jsonl``，每行一次运行对一个单元的结局。

    只追加（``O_APPEND`` 单次 ``write``，一行远小于 ``PIPE_BUF``），同一台机器同一时间只有一个调度器写（调度锁）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, unit_id: str) -> Path:
        return self.root / (hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:16] + ".jsonl")

    def entries(self, unit_id: str) -> list[dict[str, Any]]:
        path = self.path_for(unit_id)
        if path.is_symlink():
            raise LedgerError(f"补跑账本是符号链接：{path}")
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise LedgerError(f"补跑账本读不出来：{path}：{error}") from error
        found: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise LedgerError(f"补跑账本第 {number} 行不是合法 JSON：{path}") from error
            if (not isinstance(entry, dict) or entry.get("schema_version") != LEDGER_SCHEMA or entry.get("unit_id") != unit_id
                    or not isinstance(entry.get("run_id"), str) or not entry["run_id"] or entry.get("outcome") not in OUTCOMES):
                raise LedgerError(f"补跑账本第 {number} 行格式不对或不属于该单元：{path}")
            found.append(entry)
        return found

    def recent(self, unit_id: str, rounds: int) -> list[dict[str, Any]]:
        """该单元最近 ``rounds`` 轮（按运行 ID 去重，同一运行取最后一条）的结局，旧→新。"""

        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for entry in self.entries(unit_id):
            if entry["run_id"] in latest:
                order.remove(entry["run_id"])
            latest[entry["run_id"]] = entry
            order.append(entry["run_id"])
        return [latest[run_id] for run_id in order[-rounds:]]

    def allowance(self, unit_id: str, *, window_rounds: int = DEFAULT_WINDOW_ROUNDS, pass_limit: int = DEFAULT_PASS_LIMIT) -> dict[str, Any]:
        """累计规则：窗口内补跑通过已达上限就不再补跑。账本坏了抛 ``LedgerError``（调用方按“不许补跑”处理）。"""

        recent = self.recent(unit_id, window_rounds)
        prior = sum(1 for entry in recent if entry["outcome"] == STATUS_PASSED)
        return {"window_rounds": window_rounds, "pass_limit": pass_limit, "prior_retry_passed": prior,
                "recent": [{"run_id": entry["run_id"], "outcome": entry["outcome"]} for entry in recent],
                "allowed": prior < pass_limit}

    def append(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """追加结局。``entries`` 每项要有 ``unit_id``、``run_id``、``outcome``（其余字段原样保留）。"""

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in entries:
            entry = {"schema_version": LEDGER_SCHEMA, **item}
            if entry.get("outcome") not in OUTCOMES or not entry.get("unit_id") or not entry.get("run_id"):
                raise LedgerError(f"补跑账本条目不合法：{entry!r}")
            line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            descriptor = os.open(self.path_for(str(entry["unit_id"])), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)


def default_ledger_root(record_store_root: Path) -> Path:
    """账本目录：环境变量 ``UNIT_EXECUTOR_RETRY_LEDGER`` 优先（收尾门禁的记录库按方案独立，可指向共享账本），否则在记录库下。"""

    override = os.environ.get("UNIT_EXECUTOR_RETRY_LEDGER")
    return Path(override) if override else Path(record_store_root) / LEDGER_DIRECTORY


# ---------------------------------------------------------------------------
# 标注
# ---------------------------------------------------------------------------


def attempt_ref(*, kind: str, passed: bool, exit_code: int | None, signal: int | None, timed_out: bool, seconds: float,
                record_sha256: str | None, record_path: str | None, log_path: str, log_sha256: str | None) -> dict[str, Any]:
    return {"kind": kind, "passed": bool(passed), "exit_code": exit_code, "signal": signal, "timed_out": bool(timed_out),
            "seconds": seconds, "record_sha256": record_sha256, "record_path": record_path, "log": log_path, "log_sha256": log_sha256}


def build_marker(*, status: str, failure_class: str, reason: str, first_attempt: Mapping[str, Any],
                 retry_attempt: Mapping[str, Any] | None, ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    if status not in STATUSES or failure_class not in FAILURE_CLASSES:
        raise RetryPolicyError(f"标注的状态或失败类别不认识：{status}／{failure_class}")
    return {"schema_version": MARKER_SCHEMA, "status": status, "failure_class": failure_class,
            "failure_class_label": CLASS_LABELS[failure_class], "reason": reason, "first_attempt": dict(first_attempt),
            "retry_attempt": dict(retry_attempt) if retry_attempt is not None else None,
            "ledger": dict(ledger) if ledger is not None else None}


def _attempt_problems(label: str, attempt: Any, *, expect_passed: bool) -> list[str]:
    if not isinstance(attempt, Mapping):
        return [f"{label}缺失"]
    problems: list[str] = []
    for key in ("record_sha256", "log_sha256"):
        if not isinstance(attempt.get(key), str) or not _SHA256.fullmatch(attempt[key]):
            problems.append(f"{label}的 {key} 不是 64 位十六进制摘要")
    if not isinstance(attempt.get("log"), str) or not attempt["log"]:
        problems.append(f"{label}没有日志路径")
    if attempt.get("passed") is not expect_passed:
        problems.append(f"{label}的通过标志应为 {expect_passed}")
    if expect_passed and (attempt.get("exit_code") != 0 or attempt.get("signal") is not None or attempt.get("timed_out")):
        problems.append(f"{label}标为通过，退出状态却不是干净的 0")
    return problems


def marker_problems(marker: Any, *, unit_id: str = "", passed: bool | None = None,
                    window_rounds: int = DEFAULT_WINDOW_ROUNDS, pass_limit: int = DEFAULT_PASS_LIMIT) -> list[str]:
    """标注的静态自检（不读文件系统）；空列表＝自洽。``passed`` 给出时核对汇总行的通过标志与标注状态一致。"""

    if not isinstance(marker, Mapping) or marker.get("schema_version") != MARKER_SCHEMA:
        return [f"{unit_id or '单元'}：补跑标注缺失或格式版本不对"]
    problems: list[str] = []
    status, failure_class = marker.get("status"), marker.get("failure_class")
    if status not in STATUSES:
        problems.append(f"补跑状态不认识：{status!r}")
    if failure_class not in FAILURE_CLASSES:
        problems.append(f"失败类别不认识：{failure_class!r}")
    if status in (STATUS_PASSED, STATUS_FAILED) and failure_class != CLASS_EXECUTION:
        problems.append(f"只有执行失败可补跑，标注却是 {failure_class!r}")
    if status == STATUS_NOT_RETRIABLE and failure_class == CLASS_EXECUTION:
        problems.append("执行失败不应标为不可补跑（应是补跑被拒绝并写明原因）")
    if passed is not None and (status == STATUS_PASSED) != passed:
        problems.append("汇总行的通过标志与补跑状态不一致（只有补跑通过的单元可以是通过）")
    first = marker.get("first_attempt")
    if status in STATUSES:
        problems.extend(_attempt_problems("首次尝试", first, expect_passed=False))
    if status in (STATUS_PASSED, STATUS_FAILED):
        problems.extend(_attempt_problems("补跑尝试", marker.get("retry_attempt"), expect_passed=status == STATUS_PASSED))
        if isinstance(first, Mapping) and isinstance(marker.get("retry_attempt"), Mapping):
            if first.get("record_sha256") == marker["retry_attempt"].get("record_sha256"):
                problems.append("两次尝试引用了同一条记录")
        ledger = marker.get("ledger")
        if not isinstance(ledger, Mapping) or ledger.get("allowed") is not True:
            problems.append("补跑没有累计账本放行的依据")
        else:
            if ledger.get("window_rounds") != window_rounds or ledger.get("pass_limit") != pass_limit:
                problems.append(f"账本窗口或上限与规定不符（应为 {window_rounds} 轮、{pass_limit} 次）")
            # 底线（必修 2）：不论调用方传什么，账本声明的窗口都不得短于 5 轮、上限都不得高于 1 次。
            declared_window, declared_limit = ledger.get("window_rounds"), ledger.get("pass_limit")
            if (isinstance(declared_window, bool) or not isinstance(declared_window, int) or declared_window < POLICY_MIN_WINDOW_ROUNDS
                    or isinstance(declared_limit, bool) or not isinstance(declared_limit, int) or declared_limit > POLICY_MAX_PASS_LIMIT):
                problems.append(f"账本声明的窗口或上限比规定宽松（窗口至少 {POLICY_MIN_WINDOW_ROUNDS} 轮、补跑通过至多 {POLICY_MAX_PASS_LIMIT} 次）")
            prior = ledger.get("prior_retry_passed")
            if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0 or prior >= min(pass_limit, POLICY_MAX_PASS_LIMIT):
                problems.append(f"窗口内此前已有 {prior!r} 次补跑通过，不应再放行补跑")
            recent = ledger.get("recent")
            if isinstance(recent, list):
                counted = sum(1 for item in recent if isinstance(item, Mapping) and item.get("outcome") == STATUS_PASSED)
                if counted != prior or len(recent) > max(window_rounds, POLICY_MIN_WINDOW_ROUNDS):
                    problems.append("账本引用的最近几轮结局与累计数对不上")
    elif status == STATUS_PASSED and marker.get("retry_attempt") is None:
        problems.append("补跑通过却没有补跑尝试")
    if status == STATUS_DENIED and not str(marker.get("reason") or "").strip():
        problems.append("补跑被拒绝却没有写明原因")
    return [f"{unit_id}：{problem}" if unit_id else problem for problem in problems]


def marker_file_problems(marker: Mapping[str, Any], *, unit_id: str = "") -> list[str]:
    """标注引用的两份日志必须在盘上、摘要相符（补跑通过要求两次日志都留）。"""

    problems: list[str] = []
    for label, key in (("首次尝试", "first_attempt"), ("补跑尝试", "retry_attempt")):
        attempt = marker.get(key)
        if not isinstance(attempt, Mapping):
            continue
        path = Path(str(attempt.get("log") or ""))
        try:
            if not path.is_file() or path.is_symlink():
                problems.append(f"{label}的日志不在：{path}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != attempt.get("log_sha256"):
                problems.append(f"{label}的日志摘要与标注不符：{path}")
        except OSError as error:
            problems.append(f"{label}的日志读不出来：{path}：{error}")
    return [f"{unit_id}：{problem}" if unit_id else problem for problem in problems]


def summarize_markers(rows: Iterable[Mapping[str, Any]], *, config: RetryConfig) -> dict[str, Any]:
    """汇总里的 ``retry`` 段：开关、规则、补跑通过／失败／被拒绝／不可补跑的单元。行里没有标注的单元不出现。"""

    buckets: dict[str, list[str]] = {status: [] for status in STATUSES}
    markers: dict[str, Any] = {}
    for row in rows:
        marker = row.get("retry")
        if isinstance(marker, Mapping) and marker.get("status") in buckets:
            buckets[marker["status"]].append(str(row["unit_id"]))
            markers[str(row["unit_id"])] = marker
    return {"schema_version": SUMMARY_SCHEMA, "enabled": config.enabled, "window_rounds": config.window_rounds,
            "pass_limit": config.pass_limit, "retry_passed": sorted(buckets[STATUS_PASSED]),
            "retry_failed": sorted(buckets[STATUS_FAILED]), "retry_denied": sorted(buckets[STATUS_DENIED]),
            "not_retriable": sorted(buckets[STATUS_NOT_RETRIABLE]), "units": markers}


def summary_problems(summary: Any, rows: Iterable[Mapping[str, Any]] | None = None, *, require_files: bool = False) -> list[str]:
    """结论与收尾门禁用的整体自检：``summary["retry"]`` 与逐行标注一致、每条补跑通过的标注自洽（可选：日志在盘且摘要相符）。

    没有 ``retry`` 段（补跑关闭）时返回空；有补跑通过的行却没有对应的汇总段，视为被藏起来，报问题。"""

    row_list = list(rows) if rows is not None else list((summary or {}).get("units") or [])
    block = (summary or {}).get("retry") if isinstance(summary, Mapping) else None
    marked = {str(row.get("unit_id")): row for row in row_list if isinstance(row, Mapping) and row.get("retry") is not None}
    # 补跑尝试的行（kind=retry）必须带补跑标注：没有标注的补跑尝试等于把“补跑通过”伪装成一次普通通过（验收修正建议 8）。
    unmarked_retry = [f"{row.get('unit_id')}：汇总行是补跑尝试却没有补跑标注（补跑被伪装成普通通过）"
                      for row in row_list if isinstance(row, Mapping) and row.get("kind") == "retry" and row.get("retry") is None]
    if block is None:
        return [f"{unit_id}：汇总行带补跑标注，汇总却没有 retry 段（补跑被藏起来了）" for unit_id in sorted(marked)] + unmarked_retry
    if not isinstance(block, Mapping) or block.get("schema_version") != SUMMARY_SCHEMA:
        return ["汇总的 retry 段格式版本不对"]
    window, limit = block.get("window_rounds"), block.get("pass_limit")
    if isinstance(window, bool) or not isinstance(window, int) or isinstance(limit, bool) or not isinstance(limit, int):
        return ["汇总的 retry 段缺少窗口或上限"]
    problems: list[str] = []
    # 窗口和上限钉死（必修 2）：汇总只能声明更严的值；声明更宽（窗口短于 5 轮、上限高于 1 次）一律判问题，不信汇总自己说的额度。
    if window < POLICY_MIN_WINDOW_ROUNDS or limit > POLICY_MAX_PASS_LIMIT or limit < 1:
        problems.append(f"汇总声明的补跑窗口 {window} 轮／上限 {limit} 次比规定宽松（窗口至少 {POLICY_MIN_WINDOW_ROUNDS} 轮、补跑通过至多 {POLICY_MAX_PASS_LIMIT} 次）")
    problems.extend(unmarked_retry)
    for unit_id, row in sorted(marked.items()):
        problems.extend(marker_problems(row["retry"], unit_id=unit_id, passed=row.get("passed"), window_rounds=window, pass_limit=limit))
        if require_files and row["retry"].get("status") in (STATUS_PASSED, STATUS_FAILED):
            problems.extend(marker_file_problems(row["retry"], unit_id=unit_id))
    expected = summarize_markers(row_list, config=RetryConfig(enabled=bool(block.get("enabled")), window_rounds=window, pass_limit=limit))
    for key in ("retry_passed", "retry_failed", "retry_denied", "not_retriable"):
        if sorted(block.get(key) or []) != expected[key]:
            problems.append(f"汇总 retry.{key} 与逐行标注不一致")
    return problems
