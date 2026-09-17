"""结构化根因编码：让同一缺陷在不同 Campaign 里得到同一个根因 ID。

背景：0.154 升级期间同一收口步骤失败了 5 次，账本里的
``same_root_cause_retry_limit = 2`` 从未触发，因为旧的根因 ID 由
``sha256(campaign_id + step)`` 生成，每个新 Campaign 都会得到一个新 ID。
本模块把根因身份收敛为四项稳定输入：

* ``component``：根因生产者，必须与枚举表登记的一致；
* ``stable_error_code``：``root_cause_codes.json`` 里登记的稳定错误码；
* ``failed_step``：失败步骤或动作的稳定名称；
* ``stable_dimensions``：该错误码白名单里的维度键值。

诊断全文、时间戳、Campaign ID、attempt ID、路径、nonce 一律不进主键。
任何看起来像这些波动值的输入都会被拒绝，而不是被剥离；剥离会让不同
故障因为剥掉了同一段而被合并，拒绝则迫使调用方只传稳定维度。

项目总账会冻结枚举表摘要与算法版本；生产者和消费者在生成或比较根因前
先调用 :func:`assert_codes_identity`，避免后续控制面更新静默改变 ID。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "codex-upgrade-root-cause-codes/v1"
ALGORITHM_VERSION = "structured-root-cause/v1"
ROOT_CAUSE_ID_PREFIX = "rc1-"
ROOT_CAUSE_ID_RE = re.compile(r"^rc1-[0-9a-f]{20}$")
CODE_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DIMENSION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FAILURE_OBSERVATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_VALUE_LENGTH = 128
DEFAULT_CODES_PATH = Path(__file__).resolve().parent / "root_cause_codes.json"

# 六类波动值：出现在稳定输入里就拒绝，而不是剥离。
_VOLATILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Campaign ID", re.compile(r"c\d{3,4}-[a-z0-9-]*\d{8}t\d{4,6}z")),
    ("attempt ID", re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{16}")),
    ("supervisor run ID", re.compile(r"run-[0-9a-f]{64}")),
    ("ISO 时间戳", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")),
    ("绝对路径", re.compile(r"(?:^|\s)/[^\s]*")),
    ("长十六进制摘要", re.compile(r"[0-9a-f]{32,}")),
)


class RootCauseError(ValueError):
    """根因输入不稳定、未登记或枚举表身份不一致。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_volatile(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise RootCauseError(f"{label}必须是字符串")
    if len(value) > MAX_VALUE_LENGTH or "\n" in value or "\r" in value:
        raise RootCauseError(f"{label}过长或含换行")
    for name, pattern in _VOLATILE_PATTERNS:
        if pattern.search(value):
            raise RootCauseError(f"{label}含{name}，不是稳定根因输入：{value!r}")


def load_codes(path: Path | None = None) -> dict[str, Any]:
    """读取并校验枚举表，返回含文件摘要的只读视图。"""

    source = Path(path) if path is not None else DEFAULT_CODES_PATH
    if source.is_symlink() or not source.is_file():
        raise RootCauseError(f"根因枚举表不是普通文件：{source}")
    raw = source.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RootCauseError("根因枚举表不是合法 JSON") from error
    if not isinstance(payload, Mapping):
        raise RootCauseError("根因枚举表顶层必须是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RootCauseError("根因枚举表 schema_version 非预期")
    if payload.get("algorithm_version") != ALGORITHM_VERSION:
        raise RootCauseError("根因枚举表 algorithm_version 与本模块不一致")
    codes = payload.get("codes")
    if not isinstance(codes, Mapping) or not codes:
        raise RootCauseError("根因枚举表缺少 codes")
    normalized: dict[str, dict[str, Any]] = {}
    for code, entry in codes.items():
        if not isinstance(code, str) or not CODE_RE.fullmatch(code):
            raise RootCauseError(f"根因错误码非法：{code!r}")
        if not isinstance(entry, Mapping):
            raise RootCauseError(f"根因错误码 {code} 的登记项必须是对象")
        component = entry.get("component")
        if not isinstance(component, str) or not COMPONENT_RE.fullmatch(component):
            raise RootCauseError(f"根因错误码 {code} 的 component 非法")
        dimensions = entry.get("stable_dimensions")
        if not isinstance(dimensions, list) or any(
            not isinstance(item, str) or not DIMENSION_KEY_RE.fullmatch(item)
            for item in dimensions
        ):
            raise RootCauseError(f"根因错误码 {code} 的 stable_dimensions 非法")
        if len(set(dimensions)) != len(dimensions):
            raise RootCauseError(f"根因错误码 {code} 的 stable_dimensions 重复")
        legacy = entry.get("legacy", False)
        if not isinstance(legacy, bool):
            raise RootCauseError(f"根因错误码 {code} 的 legacy 必须是布尔值")
        normalized[code] = {
            "component": component,
            "stable_dimensions": tuple(dimensions),
            "legacy": legacy,
        }
    return {
        "path": str(source),
        "codes_sha256": _sha256(raw),
        "algorithm_version": ALGORITHM_VERSION,
        "codes": normalized,
    }


def describe_root_cause(
    *,
    component: str,
    stable_error_code: str,
    failed_step: str,
    stable_dimensions: Mapping[str, str] | None = None,
    codes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """校验四项稳定输入并返回可审计的根因描述（含 ID）。"""

    table = codes if codes is not None else load_codes()
    entries = table["codes"]
    if not isinstance(stable_error_code, str) or stable_error_code not in entries:
        raise RootCauseError(f"根因错误码未登记：{stable_error_code!r}")
    entry = entries[stable_error_code]
    if component != entry["component"]:
        raise RootCauseError(
            f"根因错误码 {stable_error_code} 属于 {entry['component']}，"
            f"不能由 {component!r} 生产"
        )
    _reject_volatile(failed_step, "failed_step")
    provided = dict(stable_dimensions or {})
    expected = set(entry["stable_dimensions"])
    if set(provided) != expected:
        raise RootCauseError(
            f"根因错误码 {stable_error_code} 的维度键必须恰好是 "
            f"{sorted(expected)}，实际 {sorted(provided)}"
        )
    for key, value in provided.items():
        _reject_volatile(value, f"维度 {key}")
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "component": component,
        "stable_error_code": stable_error_code,
        "failed_step": failed_step,
        "stable_dimensions": {key: provided[key] for key in sorted(provided)},
    }
    payload["root_cause_id"] = ROOT_CAUSE_ID_PREFIX + _sha256(_canonical(payload))[:20]
    payload["codes_sha256"] = table["codes_sha256"]
    return payload


def structured_root_cause(
    *,
    component: str,
    stable_error_code: str,
    failed_step: str,
    stable_dimensions: Mapping[str, str] | None = None,
    codes: Mapping[str, Any] | None = None,
) -> str:
    """返回跨 Campaign 稳定的根因 ID。"""

    return describe_root_cause(
        component=component,
        stable_error_code=stable_error_code,
        failed_step=failed_step,
        stable_dimensions=stable_dimensions,
        codes=codes,
    )["root_cause_id"]


def describe_failure_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    component: str,
    stable_error_code: str,
    stable_dimensions: Mapping[str, str],
    codes: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """把枚举失败观测归一化为一一对应的稳定根因。

    一次 attempt／动作内，相同 ``check_id + failure_code`` 只保留一项；
    ``failed_step`` 使用该稳定二元组的摘要，不包含 Campaign、attempt、重试次数、
    时间或诊断正文，因此同一检查在不同 Campaign 中得到相同根因 ID。
    """

    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes, bytearray))
        or len(observations) > 64
    ):
        raise RootCauseError("failure_observations 必须是至多 64 项的数组")
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping):
            raise RootCauseError(f"failure_observations[{index}] 必须是对象")
        check_id = item.get("check_id")
        failure_code = item.get("failure_code")
        if (
            not isinstance(check_id, str)
            or FAILURE_OBSERVATION_ID_RE.fullmatch(check_id) is None
            or not isinstance(failure_code, str)
            or FAILURE_OBSERVATION_ID_RE.fullmatch(failure_code) is None
        ):
            raise RootCauseError(
                f"failure_observations[{index}] 的 check_id 或 failure_code 非法"
            )
        by_key[(check_id, failure_code)] = {
            "check_id": check_id,
            "failure_code": failure_code,
        }

    normalized: list[dict[str, str]] = []
    causes: list[dict[str, Any]] = []
    table = codes if codes is not None else load_codes()
    for key in sorted(by_key):
        observation = by_key[key]
        failed_step = "failure-observation-" + _sha256(_canonical(observation))[:20]
        cause = describe_root_cause(
            component=component,
            stable_error_code=stable_error_code,
            failed_step=failed_step,
            stable_dimensions=stable_dimensions,
            codes=table,
        )
        normalized.append(
            {
                **observation,
                "root_cause_id": str(cause["root_cause_id"]),
            }
        )
        causes.append(
            {
                **cause,
                "check_id": observation["check_id"],
                "failure_code": observation["failure_code"],
            }
        )
    return normalized, causes


def legacy_root_cause_id(code: str, *, codes: Mapping[str, Any] | None = None) -> str:
    """把历史账本里的字面量根因映射为结构化 ID。

    只接受枚举表里标记为 ``legacy`` 的错误码；映射结果与历史字面量的
    对应关系由项目总账的映射收据固定下来，本函数只负责生成。
    """

    table = codes if codes is not None else load_codes()
    entry = table["codes"].get(code)
    if entry is None or not entry["legacy"]:
        raise RootCauseError(f"不是登记的历史字面量根因：{code!r}")
    return structured_root_cause(
        component=entry["component"],
        stable_error_code=code,
        failed_step="",
        stable_dimensions={},
        codes=table,
    )


def is_structured(value: Any) -> bool:
    return isinstance(value, str) and ROOT_CAUSE_ID_RE.fullmatch(value) is not None


def assert_codes_identity(
    *,
    expected_codes_sha256: str,
    expected_algorithm_version: str,
    codes: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """校验当前枚举表与算法版本等于总账冻结值，否则失败关闭。"""

    table = codes if codes is not None else load_codes()
    if table["codes_sha256"] != expected_codes_sha256:
        raise RootCauseError("根因枚举表摘要与冻结值不一致，禁止生成或比较根因")
    if table["algorithm_version"] != expected_algorithm_version:
        raise RootCauseError("根因算法版本与冻结值不一致，禁止生成或比较根因")
    return table
