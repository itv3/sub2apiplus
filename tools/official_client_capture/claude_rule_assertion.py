#!/usr/bin/env python3
"""Claude 官方／候选证据的逐规则独立断言（Claude 升级控制面设计 B4-03 的 checker）。

结构与 Codex 的 ``candidate_rule_assertion.py`` 对齐（那份在 Claude 身份策略里被忽略，这里不导入，按 Claude 的证据形态
另写），只读取证据，不导入候选 Go 画像，不联网。

一、断言画像（``claude_rule_expectations_<版本>.json``，``claude-candidate-rule-expectations/v1``）
==================================================================================================
``rules[{rule_id, description, atomic_assertion_ids, scenario_ids, checks[{id, description, atomic_assertion_id,
select{record_type, where[], scenario_ids?}, assertion{operator, ...}}]}]`` 加 ``scenarios``（场景的
``required_artifact_kinds``）与 ``generated_from``（生成输入的摘要）。画像不手写：子命令 ``expectations`` 由 VC-2 的
AtomicAssertionLedger（规则 → 原子断言；也接受基线 RequiredRules 清单）、场景清单（规则 → 场景，取场景的 ``covers``）
与判据模板库（按原子断言 ID 组织的 check 模板）确定性生成；模板里的期望值可以写成 ``{"$release": "wire:/指针"}`` 或
``{"$release": "profile:/指针"}``，生成时从目标 Release 的 Wire／Profile 制品取值。每条原子断言至少有一个 check，缺模板即
失败关闭。

二、评估用 manifest（``claude-evaluation-capture-manifest/v1``）
================================================================
VC-1／候选封存的 capture manifest（``claude-upgrade-capture-manifest/v1``）本身不带逐文件摘要，摘要在 EvidencePackage 的
外部证据绑定里。``bind_capture_manifest`` 把两者合成评估用 manifest：每个 artifact 保留 ``probe_id``、``path``、``kind``、
``parser``、``labels``、``scenario_ids``（与可选的 ``connection``、``derive``、``job_id``），再加 ``sha256`` 与 ``bytes``。
checker 读任何证据文件前都按它核对路径（证据根内、不跟符号链接）、字节数与摘要。

三、记录类型（与验收契约 ``claude_acceptance_contract`` 同一闭集）
===================================================================
* ``claude_http_request``：中继请求字节（``relay_binary``，``derive.parser == h1_request_stream``）逐请求解析的 HTTP/1.1
  请求：请求行、头名顺序与取值、Content-Length、内容编码（gzip／deflate 解码后再解析 JSON）、Body 顶层键序与完整 Body。
  秘密头（Authorization、Cookie 等）只保留形态（scheme、长度、是否为等长脱敏占位），取值一律替换为 ``<redacted>``。解析器
  在本文件内实现、不导入 control 层模块，免得解析逻辑变了 checker 摘要不变。
* ``relay_connection``：每条有效连接一条：连接序号、连接号、连接上的请求行序列、同目录干预记录里该连接的合成动作与时刻。
* ``claude_http_response``：中继按合成计划给出的响应（``synthetic_intervention``，``intervention.jsonl`` 逐行）：连接号、
  动作名、计划、请求行、消息序号、延迟与时刻。真实上游响应字节当前没有进证据标签，不产出这类记录。
* ``tls_client_hello``：P 通道 pcap 逐包解析（evidence 层 ``pcap_clienthello``）：SNI、扩展类型序列、cipher 序列、ALPN。
* ``runtime_receipt``：中继清单（``relay_manifest``）：probe、模型、合成计划、客户端版本、注入环境、本地错误结果、
  中继完整性等便捷字段，另带完整清单对象（``data.manifest``）。

四、断言引擎
============
``select`` 按 ``record_type``、场景（缺省取规则的场景；记录属于 artifact 的全部场景）与 ``where`` 条件选记录，``where``
的 8 个 operator 与 21 个断言 operator 的语义与 Codex 相同。每条规则的结果首项固定是 ``scenario-artifact-coverage``：规则
引用的每个场景都必须有规定类型的已绑定证据。给出 ``--side`` 时跳过验收契约登记为本侧不适用的 check。

五、投影、命令与结果
====================
``project_capture_manifest`` 把评估用 manifest 缩到与规则场景相交的 artifact（顶层字段与条目逐字保留、按原顺序），
``canonical_projection_bytes`` 是其规范字节，``projection_sha256`` 是规范字节摘要；``--capture-manifest-projection`` 给出时
先用完整 manifest 重算投影并逐字比对，再只用投影解析证据。``build_assertion_command`` 是命令的唯一构造器（builder 与
accept 用它复算命令摘要）；结果文档 ``claude-candidate-rule-assertion/v1`` 带 checker 自身的整文件摘要与命令摘要，
全部 check 通过为 pass（退出码 0），否则 fail（退出码 1）；输入不成立时写出一份只含 ``input-validation`` 失败 check 的
文档。

本文件在 Claude 身份策略里归 evidence 层（整文件计入），是评估器四项摘要里的 checker；Codex 身份策略忽略它。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

if __package__ in {None, ""}:
    # 支持 builder 与 accept 使用的显式文件命令：python3 tools/official_client_capture/claude_rule_assertion.py ...
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.claude_acceptance_contract import (  # noqa: E402
    COVERAGE_CHECK_ID,
    KNOWN_RECORD_TYPES,
    check_applies_to_side,
    derive_side_restricted_checks,
)
from tools.official_client_capture.pcap_clienthello import (  # noqa: E402
    iter_packets,
    parse_client_hello,
    tcp_payload,
)

PROFILE_SCHEMA_VERSION = "claude-candidate-rule-expectations/v1"
TEMPLATES_SCHEMA_VERSION = "claude-rule-expectation-templates/v1"
CAPTURE_MANIFEST_SCHEMA_VERSION = "claude-upgrade-capture-manifest/v1"
EVALUATION_MANIFEST_SCHEMA_VERSION = "claude-evaluation-capture-manifest/v1"
ASSERTION_SCHEMA_VERSION = "claude-candidate-rule-assertion/v1"
ATOMIC_LEDGER_SCHEMA = "official-client-atomic-assertion-ledger/v1"
REQUIRED_RULES_SCHEMA = "claude-code-required-rule-manifest/v1"
RULE_MANIFEST_SCHEMA = "claude-upgrade-rule-manifest/v1"
SCENARIO_MANIFEST_SCHEMA = "claude-upgrade-scenarios/v1"
CHECKER_RELATIVE_PATH = "tools/official_client_capture/claude_rule_assertion.py"
PROJECTION_FLAG = "--capture-manifest-projection"
EXPECTATIONS_COMMAND = "expectations"

# 仓库基线画像（claude_rule_expectations_<版本>.json）的冻结摘要，按版本登记；只用于仓库基线的冻结核对，compare／accept
# 用批准画像的精确摘要（``--expected-profile-sha256``）。
FROZEN_PROFILE_SHA256S: dict[str, str] = {}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RULE_ID_RE = re.compile(r"^SPEC-[A-Z0-9]+-[0-9]{3}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
SCENARIO_ID_RE = re.compile(r"^(?:transport|entrypoint|agency|header|body|tool|failure|aux|privacy|condition)\.[a-z0-9_]+$")
CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TEMPLATE_CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
REQUEST_LINE_RE = re.compile(r"^([A-Z]+) (\S+) (HTTP/1\.[01])$")

# 评估用 manifest 的 kind／parser 闭集（与 Claude 证据标签 ``claude_upgrade_plan.LABEL_KIND_PARSERS`` 同一组合）。
ARTIFACT_KIND_PARSERS: dict[str, frozenset[str]] = {
    "relay_binary": frozenset({"opaque_bound_source"}),
    "relay_manifest": frozenset({"claude_relay_manifest"}),
    "synthetic_intervention": frozenset({"jsonl"}),
    "pcap": frozenset({"pcap_client_hello"}),
    "debug_log": frozenset({"opaque_bound_source"}),
}
DERIVE_PARSERS = frozenset({"h1_request_stream"})
ALLOWED_WHERE_OPERATORS = frozenset({"equal", "not_equal", "present", "absent", "contains", "in", "match", "subset"})
ALLOWED_ASSERTION_OPERATORS = frozenset(
    {
        "all_absent",
        "all_contains",
        "all_equal",
        "all_fields_equal",
        "all_list_all_different",
        "all_list_all_same",
        "all_list_prefix",
        "all_list_suffix",
        "all_lowercase",
        "all_match",
        "all_not_contains",
        "all_ordered_subset_of",
        "all_subset",
        "any_equal",
        "any_subset",
        "count_at_least",
        "count_equal",
        "distinct_count_at_least",
        "distinct_values_equal",
        "none_subset",
        "same_set_distinct_order",
    }
)
# 秘密头：取值替换为占位，只保留形态（scheme、长度、是否为等长脱敏占位）。
SECRET_HEADER_NAMES = frozenset({"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"})
SECRET_SCRUB_MARKER = "<secret>"
REDACTED_VALUE = "<redacted>"
RELEASE_REF_KEY = "$release"
RELEASE_ARTIFACTS = ("profile", "wire")
MISSING = object()


class AssertionConfigurationError(ValueError):
    """画像、manifest 或证据不足以支撑断言。"""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise AssertionConfigurationError(f"JSON 对象键重复：{key}")
        payload[key] = value
    return payload


@dataclass(frozen=True)
class Observation:
    """一条可被 selector 选中的记录；``evidence_paths`` 不进入 selector 可见的映射。"""

    record_id: str
    scenario_ids: tuple[str, ...]
    record_type: str
    artifact_path: str
    evidence_paths: tuple[str, ...]
    labels: Mapping[str, Any]
    data: Mapping[str, Any]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "scenario_ids": list(self.scenario_ids),
            "record_type": self.record_type,
            "artifact_path": self.artifact_path,
            "labels": dict(self.labels),
            "data": dict(self.data),
        }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def command_sha256(command: Sequence[str]) -> str:
    """命令摘要：参数数组的紧凑 JSON（不排序、无末尾换行），与 Codex 同一口径。"""

    return hashlib.sha256(json.dumps(list(command), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_json(path: Path, description: str) -> Any:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AssertionConfigurationError(f"{description} 必须是非符号链接普通文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionConfigurationError(f"{description} 不是合法 JSON：{error}") from error


def _require_exact_keys(value: Any, *, required: set[str], optional: set[str] = frozenset(), description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionConfigurationError(f"{description} 必须是对象")
    keys = set(value)
    missing = sorted(required - keys)
    extra = sorted(keys - required - set(optional))
    if missing or extra:
        raise AssertionConfigurationError(f"{description} 字段不闭合：缺少 {missing}，多出 {extra}")
    return dict(value)


def _relative_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AssertionConfigurationError(f"{description} 必须是非空相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != value:
        raise AssertionConfigurationError(f"{description} 不是规范相对路径：{value}")
    return path


def _resolve_evidence_file(evidence_root: Path, relative: str, *, sha256: str, size: int, description: str) -> Path:
    """证据文件：证据根内、路径上没有符号链接、字节数与摘要一致。"""

    root = Path(evidence_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise AssertionConfigurationError(f"证据根必须是非符号链接的绝对目录：{root}")
    parts = _relative_path(relative, description).parts
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise AssertionConfigurationError(f"{description} 路径含符号链接：{relative}")
    if not current.is_file():
        raise AssertionConfigurationError(f"{description} 不存在或不是普通文件：{relative}")
    if not current.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise AssertionConfigurationError(f"{description} 越出证据根：{relative}")
    if current.stat().st_size != size:
        raise AssertionConfigurationError(f"{description} 字节数与绑定不一致：{relative}")
    if file_sha256(current) != sha256:
        raise AssertionConfigurationError(f"{description} 摘要与绑定不一致：{relative}")
    return current


# ---------------------------------------------------------------------------
# 断言画像：读取与校验
# ---------------------------------------------------------------------------


def _string_list(value: Any, description: str, *, pattern: re.Pattern[str] | None = None, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise AssertionConfigurationError(f"{description} 必须是{'' if allow_empty else '非空'}数组")
    if len(set(map(str, value))) != len(value) or not all(isinstance(item, str) and item for item in value):
        raise AssertionConfigurationError(f"{description} 必须是不重复的非空字符串")
    if pattern is not None:
        bad = [item for item in value if not pattern.fullmatch(item)]
        if bad:
            raise AssertionConfigurationError(f"{description} 含非法取值：{bad}")
    return list(value)


def _validate_select(select: Any, *, rule_scenarios: Sequence[str], description: str) -> dict[str, Any]:
    payload = _require_exact_keys(select, required={"record_type"}, optional={"where", "scenario_ids"}, description=description)
    if payload["record_type"] not in KNOWN_RECORD_TYPES:
        raise AssertionConfigurationError(f"{description} 的 record_type 未登记：{payload['record_type']!r}")
    where = payload.get("where", [])
    if not isinstance(where, list):
        raise AssertionConfigurationError(f"{description}.where 必须是数组")
    for index, condition in enumerate(where, 1):
        condition = _require_exact_keys(
            condition, required={"path", "operator"}, optional={"value"}, description=f"{description}.where[{index}]"
        )
        if not isinstance(condition["path"], str) or not condition["path"]:
            raise AssertionConfigurationError(f"{description}.where[{index}].path 不能为空")
        if condition["operator"] not in ALLOWED_WHERE_OPERATORS:
            raise AssertionConfigurationError(f"{description}.where[{index}] 的 operator 不受支持：{condition['operator']!r}")
        if condition["operator"] not in {"present", "absent"} and "value" not in condition:
            raise AssertionConfigurationError(f"{description}.where[{index}] 缺少 value")
    if "scenario_ids" in payload:
        scenarios = _string_list(payload["scenario_ids"], f"{description}.scenario_ids", pattern=SCENARIO_ID_RE)
        if not set(scenarios) <= set(rule_scenarios):
            raise AssertionConfigurationError(f"{description}.scenario_ids 必须是规则场景的子集")
    return payload


def _validate_assertion(assertion: Any, description: str) -> dict[str, Any]:
    operator = assertion.get("operator") if isinstance(assertion, Mapping) else None
    if operator not in ALLOWED_ASSERTION_OPERATORS:
        raise AssertionConfigurationError(f"{description} 的断言 operator 不受支持：{operator!r}")
    return dict(assertion)


def validate_profile_document(profile: Any, *, expected_claude_version: str) -> dict[str, Any]:
    """断言画像的结构闭合校验（不核摘要）。"""

    payload = _require_exact_keys(
        profile,
        required={"schema_version", "claude_version", "generated_from", "scenarios", "rules"},
        optional={"status"},
        description="断言画像",
    )
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise AssertionConfigurationError("断言画像 schema_version 不受支持")
    if payload["claude_version"] != expected_claude_version or not VERSION_RE.fullmatch(str(payload["claude_version"])):
        raise AssertionConfigurationError(f"断言画像版本 {payload['claude_version']!r} 不是期望的 {expected_claude_version!r}")
    if "status" in payload and payload["status"] != "approved":
        raise AssertionConfigurationError("断言画像 status 只能是 approved")
    if not isinstance(payload["generated_from"], Mapping):
        raise AssertionConfigurationError("断言画像 generated_from 必须是对象")
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise AssertionConfigurationError("断言画像 scenarios 不能为空")
    scenario_ids: list[str] = []
    for index, scenario in enumerate(scenarios, 1):
        scenario = _require_exact_keys(
            scenario,
            required={"scenario_id", "description", "trigger", "preconditions", "required_artifact_kinds"},
            description=f"断言画像场景 {index}",
        )
        if not isinstance(scenario["scenario_id"], str) or not SCENARIO_ID_RE.fullmatch(scenario["scenario_id"]):
            raise AssertionConfigurationError(f"断言画像场景 {index} 的 scenario_id 非法")
        kinds = _string_list(scenario["required_artifact_kinds"], f"{scenario['scenario_id']}.required_artifact_kinds")
        if not set(kinds) <= set(ARTIFACT_KIND_PARSERS):
            raise AssertionConfigurationError(f"{scenario['scenario_id']} 引用未登记的证据类型：{sorted(set(kinds) - set(ARTIFACT_KIND_PARSERS))}")
        scenario_ids.append(scenario["scenario_id"])
    if len(scenario_ids) != len(set(scenario_ids)):
        raise AssertionConfigurationError("断言画像场景 ID 重复")
    rules = payload["rules"]
    if not isinstance(rules, list) or not rules:
        raise AssertionConfigurationError("断言画像 rules 不能为空")
    rule_ids: list[str] = []
    for index, rule in enumerate(rules, 1):
        rule = _require_exact_keys(
            rule,
            required={"rule_id", "description", "atomic_assertion_ids", "scenario_ids", "checks"},
            description=f"断言画像规则 {index}",
        )
        rule_id = rule["rule_id"]
        if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
            raise AssertionConfigurationError(f"断言画像规则 {index} 的 rule_id 非法")
        rule_ids.append(rule_id)
        atomic_ids = _string_list(rule["atomic_assertion_ids"], f"{rule_id}.atomic_assertion_ids", pattern=RULE_ID_RE)
        rule_scenarios = _string_list(rule["scenario_ids"], f"{rule_id}.scenario_ids", pattern=SCENARIO_ID_RE)
        if not set(rule_scenarios) <= set(scenario_ids):
            raise AssertionConfigurationError(f"{rule_id} 引用未定义场景：{sorted(set(rule_scenarios) - set(scenario_ids))}")
        checks = rule["checks"]
        if not isinstance(checks, list) or not checks:
            raise AssertionConfigurationError(f"{rule_id} 没有 check")
        check_ids: set[str] = set()
        covered: set[str] = set()
        for check_index, check in enumerate(checks, 1):
            check = _require_exact_keys(
                check,
                required={"id", "description", "atomic_assertion_id", "select", "assertion"},
                description=f"{rule_id} check {check_index}",
            )
            check_id = check["id"]
            if not isinstance(check_id, str) or not CHECK_ID_RE.fullmatch(check_id) or check_id == COVERAGE_CHECK_ID:
                raise AssertionConfigurationError(f"{rule_id} check {check_index} 的 id 非法")
            if check_id in check_ids:
                raise AssertionConfigurationError(f"{rule_id} check ID 重复：{check_id}")
            check_ids.add(check_id)
            if check["atomic_assertion_id"] not in atomic_ids:
                raise AssertionConfigurationError(f"{rule_id}／{check_id} 的原子断言不属于本规则")
            covered.add(check["atomic_assertion_id"])
            _validate_select(check["select"], rule_scenarios=rule_scenarios, description=f"{rule_id}／{check_id}.select")
            _validate_assertion(check["assertion"], f"{rule_id}／{check_id}")
        if covered != set(atomic_ids):
            raise AssertionConfigurationError(f"{rule_id} 的原子断言没有机器 check：{sorted(set(atomic_ids) - covered)}")
    if len(rule_ids) != len(set(rule_ids)):
        raise AssertionConfigurationError("断言画像规则重复")
    return payload


def _rule_manifest_ids(manifest: Mapping[str, Any]) -> list[str]:
    """规则清单里的规则 ID 序列：RequiredRules 清单（对象数组带 spec_id）或目标规则清单（ID 数组）。"""

    rules = manifest.get("required_rules")
    if not isinstance(rules, list) or not rules:
        raise AssertionConfigurationError("规则清单 required_rules 不能为空")
    ids: list[str] = []
    for item in rules:
        rule_id = item.get("spec_id") if isinstance(item, Mapping) else item
        if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
            raise AssertionConfigurationError(f"规则清单含非法规则：{item!r}")
        ids.append(rule_id)
    return ids


def load_profile(
    profile_path: Path,
    rule_manifest_path: Path | None = None,
    *,
    expected_claude_version: str,
    expected_profile_sha256: str | None = None,
    verify_frozen_digest: bool = False,
) -> dict[str, Any]:
    """读取并校验断言画像。

    两种互斥模式：给出 ``expected_profile_sha256`` 为批准画像模式（只核精确文件摘要）；``verify_frozen_digest`` 为仓库
    基线模式（核该版本登记的冻结摘要）。给出规则清单时，画像规则必须与清单同序逐项相等。
    """

    profile_path = Path(profile_path)
    profile = _load_json(profile_path, "断言画像")
    digest = file_sha256(profile_path)
    if expected_profile_sha256 is not None:
        if not SHA256_RE.fullmatch(str(expected_profile_sha256)):
            raise AssertionConfigurationError("expected_profile_sha256 必须是 SHA-256")
        if digest != expected_profile_sha256:
            raise AssertionConfigurationError("断言画像摘要与批准值不一致")
    if verify_frozen_digest:
        frozen = FROZEN_PROFILE_SHA256S.get(expected_claude_version)
        if frozen is None:
            raise AssertionConfigurationError(f"版本 {expected_claude_version} 没有登记仓库基线画像冻结摘要")
        if digest != frozen:
            raise AssertionConfigurationError("断言画像摘要与仓库冻结值不一致")
    payload = validate_profile_document(profile, expected_claude_version=expected_claude_version)
    if rule_manifest_path is not None:
        manifest = _load_json(Path(rule_manifest_path), "规则清单")
        if not isinstance(manifest, Mapping):
            raise AssertionConfigurationError("规则清单必须是对象")
        if _rule_manifest_ids(manifest) != [rule["rule_id"] for rule in payload["rules"]]:
            raise AssertionConfigurationError("断言画像规则与规则清单不是同序同集合")
    return payload


# ---------------------------------------------------------------------------
# 断言画像：由台账、场景清单、判据模板与目标 Release 确定性生成
# ---------------------------------------------------------------------------


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ledger_rules(ledger: Mapping[str, Any], rule_order: Sequence[str] | None = None) -> list[tuple[str, list[str]]]:
    """台账 → ``[(规则, [原子断言…])]``。

    接受 VC-2 的 AtomicAssertionLedger（取 ``owner_kind == required_rule`` 的断言，按 owner 归组；scenario-only 断言不归任何
    规则）与基线 RequiredRules 清单（``required_rules[].atomic_assertion_ids``）。给出 ``rule_order`` 时按它排序，且规则集合
    必须相等；否则 AtomicAssertionLedger 按规则 ID 排序、RequiredRules 清单按清单顺序。
    """

    schema = ledger.get("schema_version")
    grouped: dict[str, list[str]] = {}
    if schema == ATOMIC_LEDGER_SCHEMA:
        assertions = ledger.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise AssertionConfigurationError("AtomicAssertionLedger 没有断言")
        seen: set[str] = set()
        for item in assertions:
            if not isinstance(item, Mapping):
                raise AssertionConfigurationError("AtomicAssertionLedger 断言必须是对象")
            assertion_id, owner = item.get("assertion_id"), item.get("owner_id")
            if not isinstance(assertion_id, str) or not RULE_ID_RE.fullmatch(assertion_id) or assertion_id in seen:
                raise AssertionConfigurationError(f"AtomicAssertionLedger 断言 ID 非法或重复：{assertion_id!r}")
            seen.add(assertion_id)
            if item.get("owner_kind") != "required_rule":
                continue
            if not isinstance(owner, str) or not RULE_ID_RE.fullmatch(owner):
                raise AssertionConfigurationError(f"AtomicAssertionLedger 断言归属非法：{assertion_id}")
            grouped.setdefault(owner, []).append(assertion_id)
        order = sorted(grouped)
    elif schema == REQUIRED_RULES_SCHEMA:
        for rule in ledger.get("required_rules") or []:
            if not isinstance(rule, Mapping):
                raise AssertionConfigurationError("RequiredRules 清单规则必须是对象")
            rule_id = rule.get("spec_id")
            if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id) or rule_id in grouped:
                raise AssertionConfigurationError(f"RequiredRules 清单规则非法或重复：{rule_id!r}")
            grouped[rule_id] = _string_list(rule.get("atomic_assertion_ids"), f"{rule_id}.atomic_assertion_ids", pattern=RULE_ID_RE)
        order = list(grouped)
    else:
        raise AssertionConfigurationError(f"台账 schema_version 不受支持：{schema!r}")
    if not grouped:
        raise AssertionConfigurationError("台账没有任何 RequiredRule 的原子断言")
    if rule_order is not None:
        if sorted(rule_order) != sorted(grouped) or len(set(rule_order)) != len(rule_order):
            raise AssertionConfigurationError("规则清单与台账的规则集合不一致")
        order = list(rule_order)
    return [(rule_id, list(grouped[rule_id])) for rule_id in order]


def _json_pointer(document: Any, pointer: str, label: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise AssertionConfigurationError(f"{label} 不是 JSON 指针：{pointer!r}")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise AssertionConfigurationError(f"{label} 在目标 Release 制品里取不到值：{pointer}")
    return current


def _resolve_release_refs(value: Any, artifacts: Mapping[str, Any], label: str) -> Any:
    """把模板里的 ``{"$release": "<profile|wire>:<JSON 指针>"}`` 换成目标 Release 制品里的取值。"""

    if isinstance(value, Mapping):
        if set(value) == {RELEASE_REF_KEY}:
            reference = value[RELEASE_REF_KEY]
            if not isinstance(reference, str) or ":" not in reference:
                raise AssertionConfigurationError(f"{label} 的 Release 引用非法：{reference!r}")
            artifact, _, pointer = reference.partition(":")
            if artifact not in RELEASE_ARTIFACTS:
                raise AssertionConfigurationError(f"{label} 的 Release 引用指向未知制品：{artifact!r}")
            if artifacts.get(artifact) is None:
                raise AssertionConfigurationError(f"{label} 引用了目标 Release 的 {artifact}，但生成时没有给出该制品")
            return json.loads(json.dumps(_json_pointer(artifacts[artifact], pointer, label)))
        if RELEASE_REF_KEY in value:
            raise AssertionConfigurationError(f"{label} 的 Release 引用不能带其他字段")
        return {key: _resolve_release_refs(item, artifacts, label) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_release_refs(item, artifacts, label) for item in value]
    return value


def _validate_templates(templates: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    payload = _require_exact_keys(templates, required={"schema_version", "templates"}, optional={"description"}, description="判据模板库")
    if payload["schema_version"] != TEMPLATES_SCHEMA_VERSION:
        raise AssertionConfigurationError("判据模板库 schema_version 不受支持")
    entries = payload["templates"]
    if not isinstance(entries, Mapping) or not entries:
        raise AssertionConfigurationError("判据模板库 templates 不能为空")
    validated: dict[str, dict[str, Any]] = {}
    for assertion_id, template in entries.items():
        if not RULE_ID_RE.fullmatch(str(assertion_id)):
            raise AssertionConfigurationError(f"判据模板的原子断言 ID 非法：{assertion_id!r}")
        template = _require_exact_keys(template, required={"description", "checks"}, description=f"判据模板 {assertion_id}")
        checks = template["checks"]
        if not isinstance(checks, list) or not checks:
            raise AssertionConfigurationError(f"判据模板 {assertion_id} 没有 check")
        ids: set[str] = set()
        for index, check in enumerate(checks, 1):
            check = _require_exact_keys(
                check, required={"id", "description", "select", "assertion"}, description=f"判据模板 {assertion_id} check {index}"
            )
            if not isinstance(check["id"], str) or not TEMPLATE_CHECK_ID_RE.fullmatch(check["id"]) or check["id"] in ids:
                raise AssertionConfigurationError(f"判据模板 {assertion_id} check {index} 的 id 非法或重复")
            ids.add(check["id"])
        validated[str(assertion_id)] = template
    return validated


def build_expectations(
    *,
    claude_version: str,
    ledger: Mapping[str, Any],
    scenarios_payload: Mapping[str, Any],
    templates: Mapping[str, Any],
    release_profile: Any = None,
    release_wire: Any = None,
    rule_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """确定性生成断言画像：规则与原子断言取台账，规则场景取场景清单里 ``covers`` 含该规则的证据场景，check 取模板。"""

    if not VERSION_RE.fullmatch(str(claude_version)):
        raise AssertionConfigurationError(f"版本号非法：{claude_version!r}")
    if scenarios_payload.get("schema_version") != SCENARIO_MANIFEST_SCHEMA:
        raise AssertionConfigurationError("场景清单 schema_version 不受支持")
    template_index = _validate_templates(templates)
    rule_order = _rule_manifest_ids(rule_manifest) if rule_manifest is not None else None
    titles: dict[str, str] = {}
    if rule_manifest is not None:
        for item in rule_manifest.get("required_rules") or []:
            if isinstance(item, Mapping) and isinstance(item.get("title"), str):
                titles[str(item["spec_id"])] = item["title"]
    if ledger.get("schema_version") == REQUIRED_RULES_SCHEMA:
        for item in ledger.get("required_rules") or []:
            if isinstance(item, Mapping) and isinstance(item.get("title"), str):
                titles.setdefault(str(item["spec_id"]), item["title"])
    scenario_defs: dict[str, dict[str, Any]] = {}
    for scenario in scenarios_payload.get("evidence_scenarios") or []:
        if not isinstance(scenario, Mapping) or not isinstance(scenario.get("scenario_id"), str):
            raise AssertionConfigurationError("场景清单的证据场景非法")
        scenario_defs[scenario["scenario_id"]] = dict(scenario)
    artifacts = {"profile": release_profile, "wire": release_wire}
    rules: list[dict[str, Any]] = []
    used_scenarios: set[str] = set()
    for rule_id, atomic_ids in ledger_rules(ledger, rule_order):
        scenario_ids = sorted(sid for sid, scenario in scenario_defs.items() if rule_id in (scenario.get("covers") or []))
        if not scenario_ids:
            raise AssertionConfigurationError(f"{rule_id} 没有任何证据场景覆盖")
        used_scenarios.update(scenario_ids)
        checks: list[dict[str, Any]] = []
        for atomic_id in atomic_ids:
            template = template_index.get(atomic_id)
            if template is None:
                raise AssertionConfigurationError(f"原子断言 {atomic_id}（{rule_id}）没有判据模板")
            for check in template["checks"]:
                label = f"{rule_id}／{atomic_id}／{check['id']}"
                select = _resolve_release_refs(check["select"], artifacts, label)
                assertion = _resolve_release_refs(check["assertion"], artifacts, label)
                checks.append(
                    {
                        "id": f"{atomic_id.lower()}.{check['id']}",
                        "description": check["description"],
                        "atomic_assertion_id": atomic_id,
                        "select": select,
                        "assertion": assertion,
                    }
                )
        rules.append(
            {
                "rule_id": rule_id,
                "description": titles.get(rule_id, rule_id),
                "atomic_assertion_ids": list(atomic_ids),
                "scenario_ids": scenario_ids,
                "checks": checks,
            }
        )
    scenarios = [
        {
            "scenario_id": scenario_id,
            "description": str(scenario_defs[scenario_id].get("description", scenario_id)),
            "trigger": str(scenario_defs[scenario_id].get("trigger", "")),
            "preconditions": list(scenario_defs[scenario_id].get("preconditions") or []),
            "required_artifact_kinds": sorted(set(scenario_defs[scenario_id].get("required_artifact_kinds") or [])),
        }
        for scenario_id in sorted(used_scenarios)
    ]
    document = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "claude_version": claude_version,
        "generated_from": {
            "ledger": {"schema_version": ledger.get("schema_version"), "sha256": _canonical_sha256(ledger)},
            "scenarios": {"claude_version": scenarios_payload.get("claude_version"), "sha256": _canonical_sha256(scenarios_payload)},
            "templates": {"schema_version": templates.get("schema_version"), "sha256": _canonical_sha256(templates)},
            "release": {
                "profile_sha256": _canonical_sha256(release_profile) if release_profile is not None else None,
                "wire_sha256": _canonical_sha256(release_wire) if release_wire is not None else None,
            },
        },
        "scenarios": scenarios,
        "rules": rules,
    }
    return validate_profile_document(json.loads(json.dumps(document)), expected_claude_version=claude_version)


def render_expectations(document: Mapping[str, Any]) -> bytes:
    """断言画像的落盘字节：排序键、缩进 2、末尾换行；同一输入逐字节相同。"""

    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 评估用 manifest
# ---------------------------------------------------------------------------

ARTIFACT_REQUIRED_FIELDS = frozenset({"path", "sha256", "bytes", "kind", "parser", "probe_id", "scenario_ids", "labels"})
ARTIFACT_OPTIONAL_FIELDS = frozenset({"connection", "derive", "job_id"})


def bind_capture_manifest(
    capture_manifest: Mapping[str, Any],
    bindings: Mapping[str, Mapping[str, Any]],
    *,
    capture_id: str,
) -> dict[str, Any]:
    """封存的 capture manifest ＋ EvidencePackage 外部绑定（路径 → sha256／bytes）→ 评估用 manifest。"""

    if capture_manifest.get("schema_version") != CAPTURE_MANIFEST_SCHEMA_VERSION:
        raise AssertionConfigurationError("capture manifest schema_version 不受支持")
    if capture_manifest.get("label_mismatches"):
        raise AssertionConfigurationError("capture manifest 仍有标签不一致，不能用于评估")
    artifacts: list[dict[str, Any]] = []
    for item in capture_manifest.get("artifacts") or []:
        if not isinstance(item, Mapping):
            raise AssertionConfigurationError("capture manifest artifact 必须是对象")
        path = str(item.get("path", ""))
        binding = bindings.get(path)
        if not isinstance(binding, Mapping) or not SHA256_RE.fullmatch(str(binding.get("sha256", ""))):
            raise AssertionConfigurationError(f"证据没有封存绑定：{path}")
        entry = {key: item[key] for key in ("path", "kind", "parser", "probe_id", "scenario_ids", "labels") if key in item}
        for key in ARTIFACT_OPTIONAL_FIELDS:
            if key in item:
                entry[key] = item[key]
        entry["sha256"] = str(binding["sha256"])
        entry["bytes"] = binding.get("bytes")
        artifacts.append(entry)
    manifest = {
        "schema_version": EVALUATION_MANIFEST_SCHEMA_VERSION,
        "claude_version": str(capture_manifest.get("target_version", "")),
        "capture_id": capture_id,
        "status": "complete",
        "artifacts": artifacts,
    }
    return validate_evaluation_manifest(manifest, expected_claude_version=manifest["claude_version"])


def validate_evaluation_manifest(manifest: Any, *, expected_claude_version: str) -> dict[str, Any]:
    payload = _require_exact_keys(
        manifest,
        required={"schema_version", "claude_version", "capture_id", "status", "artifacts"},
        description="评估用 manifest",
    )
    if payload["schema_version"] != EVALUATION_MANIFEST_SCHEMA_VERSION:
        raise AssertionConfigurationError("评估用 manifest schema_version 不受支持")
    if payload["claude_version"] != expected_claude_version:
        raise AssertionConfigurationError(f"评估用 manifest 版本 {payload['claude_version']!r} 不是 {expected_claude_version!r}")
    if payload["status"] != "complete":
        raise AssertionConfigurationError("评估用 manifest status 必须是 complete")
    if not isinstance(payload["capture_id"], str) or not SAFE_ID_RE.fullmatch(payload["capture_id"]):
        raise AssertionConfigurationError("评估用 manifest capture_id 非法")
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise AssertionConfigurationError("评估用 manifest artifacts 不能为空")
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts, 1):
        label = f"评估用 manifest artifact {index}"
        artifact = _require_exact_keys(artifact, required=set(ARTIFACT_REQUIRED_FIELDS), optional=set(ARTIFACT_OPTIONAL_FIELDS), description=label)
        path = _relative_path(artifact["path"], f"{label}.path").as_posix()
        if path in seen:
            raise AssertionConfigurationError(f"评估用 manifest artifact 路径重复：{path}")
        seen.add(path)
        if not SHA256_RE.fullmatch(str(artifact["sha256"])):
            raise AssertionConfigurationError(f"{label} sha256 非法")
        if isinstance(artifact["bytes"], bool) or not isinstance(artifact["bytes"], int) or artifact["bytes"] < 0:
            raise AssertionConfigurationError(f"{label} bytes 非法")
        if artifact["kind"] not in ARTIFACT_KIND_PARSERS or artifact["parser"] not in ARTIFACT_KIND_PARSERS[artifact["kind"]]:
            raise AssertionConfigurationError(f"{label} 的 kind／parser 组合未登记：{artifact['kind']}／{artifact['parser']}")
        if not isinstance(artifact["probe_id"], str) or not SAFE_ID_RE.fullmatch(artifact["probe_id"]):
            raise AssertionConfigurationError(f"{label} probe_id 非法")
        _string_list(artifact["scenario_ids"], f"{label}.scenario_ids", pattern=SCENARIO_ID_RE)
        labels = artifact["labels"]
        if not isinstance(labels, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
            raise AssertionConfigurationError(f"{label} labels 必须是字符串映射")
        connection = artifact.get("connection")
        derive = artifact.get("derive")
        if connection is not None:
            connection = _require_exact_keys(connection, required={"ordinal", "connection_id"}, description=f"{label}.connection")
            if artifact["kind"] != "relay_binary" or not all(
                isinstance(connection[key], int) and not isinstance(connection[key], bool) and connection[key] >= 1
                for key in ("ordinal", "connection_id")
            ):
                raise AssertionConfigurationError(f"{label}.connection 只能出现在中继请求字节上，且为正整数")
        if derive is not None:
            derive = _require_exact_keys(derive, required={"parser", "kind"}, description=f"{label}.derive")
            if connection is None or derive["parser"] not in DERIVE_PARSERS:
                raise AssertionConfigurationError(f"{label}.derive 只能是逐连接中继请求字节的 h1_request_stream")
    return payload


# ---------------------------------------------------------------------------
# 证据解析
# ---------------------------------------------------------------------------


def _header_value_shape(name: str, value: str) -> dict[str, Any]:
    scheme, _, rest = value.partition(" ")
    return {
        "scheme": scheme if rest else None,
        "length": len(value),
        "scrubbed": SECRET_SCRUB_MARKER in value,
    }


def _decode_body(body: bytes, encoding: str) -> bytes | None:
    if not encoding or encoding == "identity":
        return body
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        return None
    return None


def _parse_json_body(decoded: bytes | None) -> tuple[Any, list[str] | None, bool]:
    if not decoded:
        return None, None, False
    try:
        parsed = json.loads(decoded.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, AssertionConfigurationError):
        return None, None, False
    keys = list(parsed) if isinstance(parsed, dict) else None
    return parsed, keys, True


def _dechunk(data: bytes, start: int, label: str) -> tuple[bytes, int]:
    body = bytearray()
    offset = start
    while True:
        line_end = data.find(b"\r\n", offset)
        if line_end < 0:
            raise AssertionConfigurationError(f"{label} chunked 正文不完整")
        size_text = data[offset:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as error:
            raise AssertionConfigurationError(f"{label} chunk 长度非法") from error
        offset = line_end + 2
        if size == 0:
            trailer_end = data.find(b"\r\n\r\n", line_end)
            if trailer_end < 0:
                raise AssertionConfigurationError(f"{label} chunked 结尾不完整")
            return bytes(body), trailer_end + 4
        if offset + size + 2 > len(data) or data[offset + size : offset + size + 2] != b"\r\n":
            raise AssertionConfigurationError(f"{label} chunk 越界或缺少结尾")
        body.extend(data[offset : offset + size])
        offset += size + 2


def parse_h1_requests(data: bytes, label: str) -> list[dict[str, Any]]:
    """把一条连接的客户端→上游字节逐请求解析成结构化 HTTP/1.1 请求；任何残缺都失败关闭。"""

    requests: list[dict[str, Any]] = []
    offset = 0
    while offset < len(data):
        head_end = data.find(b"\r\n\r\n", offset)
        if head_end < 0:
            raise AssertionConfigurationError(f"{label} 第 {len(requests) + 1} 个请求头不完整")
        lines = data[offset:head_end].decode("latin-1").split("\r\n")
        match = REQUEST_LINE_RE.fullmatch(lines[0])
        if match is None:
            raise AssertionConfigurationError(f"{label} 第 {len(requests) + 1} 个请求行非法")
        method, target, protocol = match.groups()
        headers: list[list[str]] = []
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or not name or name != name.strip():
                raise AssertionConfigurationError(f"{label} 请求头行非法")
            headers.append([name, value.strip(" \t")])
        lowered: dict[str, list[str]] = {}
        for name, value in headers:
            lowered.setdefault(name.lower(), []).append(value)
        body_start = head_end + 4
        transfer = ",".join(lowered.get("transfer-encoding", [])).lower()
        lengths = lowered.get("content-length", [])
        if "chunked" in transfer:
            wire_body, end = _dechunk(data, body_start, label)
            content_length = None
        else:
            if len(set(lengths)) > 1:
                raise AssertionConfigurationError(f"{label} 同一请求有多个不同的 Content-Length")
            try:
                content_length = int(lengths[0]) if lengths else None
            except ValueError as error:
                raise AssertionConfigurationError(f"{label} Content-Length 非法") from error
            end = body_start + (content_length or 0)
            if end > len(data):
                raise AssertionConfigurationError(f"{label} 请求正文被截断")
            wire_body = data[body_start:end]
        encoding = ",".join(lowered.get("content-encoding", [])).lower().strip()
        decoded = _decode_body(wire_body, encoding)
        body, body_keys, json_valid = _parse_json_body(decoded)
        split = urlsplit(target)
        secret_headers = {
            name: _header_value_shape(name, values[0]) for name, values in lowered.items() if name in SECRET_HEADER_NAMES and values
        }
        visible_values = {
            name: ([REDACTED_VALUE] * len(values) if name in SECRET_HEADER_NAMES else list(values)) for name, values in lowered.items()
        }
        requests.append(
            {
                "method": method,
                "target": target,
                "path": split.path,
                "query": split.query,
                "query_pairs": [[key, value] for key, value in parse_qsl(split.query, keep_blank_values=True)],
                "protocol": protocol,
                "request_line": f"{method} {split.path}",
                "headers": [[name, REDACTED_VALUE if name.lower() in SECRET_HEADER_NAMES else value] for name, value in headers],
                "header_names_in_order": [name for name, _value in headers],
                "header_names_lower": [name.lower() for name, _value in headers],
                "header_values": visible_values,
                "secret_headers": secret_headers,
                "header_count": len(headers),
                "host": (lowered.get("host") or [None])[0],
                "content_length": content_length,
                "transfer_encoding": transfer or None,
                "content_encoding": encoding or None,
                "wire_body_bytes": len(wire_body),
                "body_bytes": len(decoded) if decoded is not None else None,
                "body_sha256": bytes_sha256(decoded) if decoded is not None else None,
                "body_json": json_valid,
                "body": body,
                "body_keys": body_keys,
                "stream_offset": offset,
                "stream_length": end - offset,
            }
        )
        offset = end
    return requests


def _pcap_hellos(path: Path) -> list[dict[str, Any]]:
    hellos: list[dict[str, Any]] = []
    for index, (link, packet) in enumerate(iter_packets(path), 1):
        parsed = tcp_payload(link, packet)
        if parsed is None:
            continue
        destination, port, payload = parsed
        hello = parse_client_hello(payload)
        if hello is None:
            continue
        sni, extensions, ciphers, alpn = hello
        hellos.append(
            {
                "packet_index": index,
                "order": len(hellos) + 1,
                "destination": destination,
                "destination_port": port,
                "sni": sni,
                "extension_types": list(extensions),
                "cipher_suites": list(ciphers),
                "cipher_suite_count": len(ciphers),
                "alpn_protocols": list(alpn),
                "has_alpn_extension": 16 in extensions,
            }
        )
    return hellos


def _interventions(data: bytes, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, AssertionConfigurationError) as error:
            raise AssertionConfigurationError(f"{label} 第 {line_number} 行不是合法 JSON") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("connection_id"), int) or not isinstance(payload.get("action"), str):
            raise AssertionConfigurationError(f"{label} 第 {line_number} 行缺少连接号或动作")
        request_line = str(payload.get("request_line", ""))
        match = REQUEST_LINE_RE.fullmatch(request_line)
        records.append(
            {
                "line": line_number,
                "connection_id": payload["connection_id"],
                "action": payload["action"],
                "plan": payload.get("plan"),
                "request_line": request_line,
                "request": f"{match.group(1)} {urlsplit(match.group(2)).path}" if match else None,
                "message_ordinal": payload.get("message_ordinal"),
                "delay_seconds": payload.get("delay_seconds"),
                "t_unix_ms": payload.get("t_unix_ms"),
                "production_forwarded": payload.get("production_forwarded"),
            }
        )
    return records


def _runtime_receipt(manifest: Any, label: str) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise AssertionConfigurationError(f"{label} 必须是对象")

    def mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    scenario_result = mapping(manifest.get("scenario_result"))
    inner = mapping(scenario_result.get("inner_result"))
    integrity = mapping(manifest.get("relay_integrity"))
    upstream = mapping(manifest.get("upstream"))
    client = mapping(manifest.get("client"))
    injected = manifest.get("injected_probe_env")
    if not isinstance(injected, Mapping):
        injected = inner.get("injected_probe_env") if isinstance(inner.get("injected_probe_env"), Mapping) else {}
    return {
        "probe_id": manifest.get("probe_id"),
        "relay_probe": manifest.get("fw_f_v5_probe") or manifest.get("fw_f_v4_probe") or manifest.get("probe_id"),
        "model": manifest.get("model"),
        "status": manifest.get("status"),
        "response_plan": manifest.get("response_plan"),
        "client_version": str(client.get("version") or "").split(" ", 1)[0] or None,
        "injected_probe_env": dict(injected),
        "upstream_host": upstream.get("host"),
        "live_requests": upstream.get("live_requests"),
        "relay_integrity_result": integrity.get("result"),
        "messages_request_count": integrity.get("messages_request_count"),
        "hello_request_count": integrity.get("hello_request_count"),
        "excluded_connection_ids": sorted(
            int(item["connection_id"]) for item in integrity.get("excluded_connections") or [] if isinstance(item, Mapping) and isinstance(item.get("connection_id"), int)
        ),
        "message_request_expectation": manifest.get("message_request_expectation"),
        "expected_outcome": scenario_result.get("expected_outcome"),
        "driver": scenario_result.get("driver"),
        "local_error_results": inner.get("local_error_results"),
        "error_results": inner.get("error_results"),
        "return_codes": inner.get("return_codes"),
        "credential_scrubbing_verified": mapping(manifest.get("credential_scrubbing")).get("verified"),
        "manifest": json.loads(json.dumps(manifest)),
    }


def observations_from_manifest(manifest: Mapping[str, Any], evidence_root: Path) -> list[Observation]:
    """评估用 manifest（已校验）→ 记录；每个证据文件读前核对绑定。"""

    artifacts = list(manifest["artifacts"])
    contents: dict[str, bytes] = {}
    files: dict[str, Path] = {}
    for artifact in artifacts:
        path = _resolve_evidence_file(
            Path(evidence_root), artifact["path"], sha256=artifact["sha256"], size=artifact["bytes"], description=f"证据 {artifact['path']}"
        )
        files[artifact["path"]] = path
        if artifact["kind"] != "pcap":
            contents[artifact["path"]] = path.read_bytes()
    interventions_by_directory: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    observations: list[Observation] = []

    def add(record_id: str, artifact: Mapping[str, Any], record_type: str, data: Mapping[str, Any], evidence: Sequence[str]) -> None:
        labels = dict(artifact["labels"])
        labels.setdefault("probe", str(artifact["probe_id"]))
        observations.append(
            Observation(
                record_id=record_id,
                scenario_ids=tuple(artifact["scenario_ids"]),
                record_type=record_type,
                artifact_path=str(artifact["path"]),
                evidence_paths=tuple(sorted(set(evidence))),
                labels=labels,
                data=data,
            )
        )

    for artifact in artifacts:
        if artifact["kind"] == "synthetic_intervention":
            directory = PurePosixPath(artifact["path"]).parent.as_posix()
            for record in _interventions(contents[artifact["path"]], f"干预记录 {artifact['path']}"):
                interventions_by_directory.setdefault(directory, []).append((artifact["path"], record))
                add(f"{artifact['path']}#line-{record['line']}", artifact, "claude_http_response", record, [artifact["path"]])
    for artifact in artifacts:
        path = artifact["path"]
        kind = artifact["kind"]
        if kind == "relay_binary" and artifact.get("derive", {}).get("parser") == "h1_request_stream":
            connection = artifact["connection"]
            requests = parse_h1_requests(contents[path], f"中继请求字节 {path}")
            directory = PurePosixPath(path).parent.as_posix()
            related = [
                (source, record)
                for source, record in interventions_by_directory.get(directory, [])
                if record["connection_id"] == connection["connection_id"]
            ]
            for index, request in enumerate(requests, 1):
                add(
                    f"{path}#request-{index}",
                    artifact,
                    "claude_http_request",
                    {**request, "connection_id": connection["connection_id"], "connection_ordinal": connection["ordinal"], "request_index": index},
                    [path],
                )
            add(
                f"{path}#connection",
                artifact,
                "relay_connection",
                {
                    "connection_id": connection["connection_id"],
                    "connection_ordinal": connection["ordinal"],
                    "request_count": len(requests),
                    "request_lines": [request["request_line"] for request in requests],
                    "interventions": [record for _source, record in related],
                    "intervention_actions": [record["action"] for _source, record in related],
                    "first_intervention_t_unix_ms": min(
                        (record["t_unix_ms"] for _source, record in related if isinstance(record.get("t_unix_ms"), int)), default=None
                    ),
                },
                [path, *(source for source, _record in related)],
            )
        elif kind == "relay_manifest":
            try:
                payload = json.loads(contents[path].decode("utf-8"), object_pairs_hook=_unique_json_object)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AssertionConfigurationError(f"中继清单 {path} 不是合法 JSON") from error
            add(f"{path}#receipt", artifact, "runtime_receipt", _runtime_receipt(payload, f"中继清单 {path}"), [path])
        elif kind == "pcap":
            for hello in _pcap_hellos(files[path]):
                add(f"{path}#packet-{hello['packet_index']}", artifact, "tls_client_hello", hello, [path])
    record_ids = [item.record_id for item in observations]
    if len(record_ids) != len(set(record_ids)):
        raise AssertionConfigurationError("记录 ID 重复")
    return observations


def load_observations(manifest_path: Path, evidence_root: Path, expected_claude_version: str) -> tuple[dict[str, Any], list[Observation]]:
    manifest = validate_evaluation_manifest(_load_json(Path(manifest_path), "评估用 manifest"), expected_claude_version=expected_claude_version)
    return manifest, observations_from_manifest(manifest, evidence_root)


# ---------------------------------------------------------------------------
# 逐规则投影
# ---------------------------------------------------------------------------


def canonical_projection_bytes(projection: Mapping[str, Any]) -> bytes:
    """投影的规范字节：排序键、紧凑分隔、无末尾换行。"""

    return json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def projection_sha256(projection: Mapping[str, Any]) -> str:
    return bytes_sha256(canonical_projection_bytes(projection))


def _profile_rule(profile: Mapping[str, Any], rule_id: str) -> Mapping[str, Any]:
    for rule in profile["rules"]:
        if rule["rule_id"] == rule_id:
            return rule
    raise AssertionConfigurationError(f"断言画像不包含规则：{rule_id}")


def project_capture_manifest(
    profile: Mapping[str, Any],
    rule_id: str,
    manifest: Mapping[str, Any],
    evidence_root: Path,
    expected_claude_version: str,
) -> dict[str, Any]:
    """per-rule 投影：只留与规则场景相交的 artifact（条目逐字、按原顺序），并逐个核对绑定。"""

    del evidence_root  # 投影只按 manifest 条目缩小；证据核对在 checker 解析投影时做（与 Codex 同一分工）
    payload = validate_evaluation_manifest(dict(manifest), expected_claude_version=expected_claude_version)
    rule = _profile_rule(profile, rule_id)
    scenarios = set(rule["scenario_ids"])
    artifacts = [dict(item) for item in payload["artifacts"] if scenarios & set(item["scenario_ids"])]
    for scenario_id in scenarios:
        full = {item["kind"] for item in payload["artifacts"] if scenario_id in item["scenario_ids"]}
        projected = {item["kind"] for item in artifacts if scenario_id in item["scenario_ids"]}
        if full != projected:
            raise AssertionConfigurationError(f"{rule_id} 的投影丢失了场景 {scenario_id} 的证据类型")
    return {
        "schema_version": payload["schema_version"],
        "claude_version": payload["claude_version"],
        "capture_id": payload["capture_id"],
        "status": payload["status"],
        "artifacts": artifacts,
    }


def verify_capture_manifest_projection(
    profile: Mapping[str, Any],
    rule_id: str,
    capture_manifest_path: Path,
    projection_path: Path,
    evidence_root: Path,
    expected_claude_version: str,
) -> tuple[dict[str, Any], str, str]:
    """用完整 manifest 重算期望投影，投影文件字节必须逐字相等；返回 (投影, 投影文件摘要, 完整 manifest 摘要)。"""

    full = _load_json(Path(capture_manifest_path), "评估用 manifest")
    expected = project_capture_manifest(profile, rule_id, full, evidence_root, expected_claude_version)
    raw = Path(projection_path).read_bytes() if Path(projection_path).is_file() and not Path(projection_path).is_symlink() else None
    if raw is None or raw != canonical_projection_bytes(expected):
        raise AssertionConfigurationError("projection-mismatch：投影文件与完整 manifest 重算结果不一致")
    return expected, bytes_sha256(raw), file_sha256(Path(capture_manifest_path))


# ---------------------------------------------------------------------------
# 断言引擎（where 与断言 operator 的语义与 Codex 相同）
# ---------------------------------------------------------------------------


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    if not isinstance(path, str) or not path:
        return MISSING
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(key in actual and _is_subset(value, actual[key]) for key, value in expected.items())
    return expected == actual


def _where_matches(observation: Observation, conditions: Any) -> bool:
    if conditions is None:
        return True
    if not isinstance(conditions, list):
        raise AssertionConfigurationError("select.where 必须是数组")
    mapping = observation.as_mapping()
    for condition in conditions:
        condition = _require_exact_keys(condition, required={"path", "operator"}, optional={"value"}, description="where 条件")
        actual = _resolve_path(mapping, condition["path"])
        operator = condition["operator"]
        expected = condition.get("value")
        if operator == "equal":
            matched = actual is not MISSING and actual == expected
        elif operator == "not_equal":
            matched = actual is not MISSING and actual != expected
        elif operator == "present":
            matched = actual is not MISSING
        elif operator == "absent":
            matched = actual is MISSING
        elif operator == "contains":
            try:
                matched = actual is not MISSING and actual is not None and expected in actual
            except TypeError:
                matched = False
        elif operator == "in":
            matched = actual is not MISSING and isinstance(expected, list) and actual in expected
        elif operator == "match":
            matched = isinstance(actual, str) and isinstance(expected, str) and re.search(expected, actual) is not None
        elif operator == "subset":
            matched = actual is not MISSING and _is_subset(expected, actual)
        else:
            raise AssertionConfigurationError(f"不支持的 where operator：{operator!r}")
        if not matched:
            return False
    return True


def _select_observations(observations: Sequence[Observation], selector: Mapping[str, Any], default_scenarios: Sequence[str]) -> list[Observation]:
    record_type = selector.get("record_type")
    if not isinstance(record_type, str) or not record_type:
        raise AssertionConfigurationError("select.record_type 不能为空")
    scenario_ids = selector.get("scenario_ids", list(default_scenarios))
    if not isinstance(scenario_ids, list) or not scenario_ids:
        raise AssertionConfigurationError("select.scenario_ids 必须是非空数组")
    wanted = set(scenario_ids)
    return [
        item
        for item in observations
        if item.record_type == record_type and wanted & set(item.scenario_ids) and _where_matches(item, selector.get("where"))
    ]


def _json_safe(value: Any) -> Any:
    return "<absent>" if value is MISSING else value


def _serialized(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)


def _evaluate_assertion(matched: Sequence[Observation], assertion: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    operator = assertion.get("operator")
    if operator not in ALLOWED_ASSERTION_OPERATORS:
        raise AssertionConfigurationError(f"不支持的断言 operator：{operator!r}")
    mappings = [item.as_mapping() for item in matched]
    path = assertion.get("path")
    values = [_resolve_path(item, path) for item in mappings] if isinstance(path, str) else []
    shown = {"values": [_json_safe(value) for value in values]}

    if operator == "count_at_least":
        expected = assertion.get("value")
        passed = isinstance(expected, int) and not isinstance(expected, bool) and len(matched) >= expected
        return passed, {"matched_count": len(matched)}
    if operator == "count_equal":
        return len(matched) == assertion.get("value"), {"matched_count": len(matched)}
    if operator == "all_equal":
        expected = assertion.get("value")
        return bool(values) and all(value == expected for value in values), shown
    if operator == "any_equal":
        expected = assertion.get("value")
        return any(value == expected for value in values), shown
    if operator == "all_absent":
        return bool(mappings) and all(value is MISSING for value in values), shown
    if operator in {"all_contains", "all_not_contains"}:
        expected = assertion.get("value")
        contained = []
        for value in values:
            try:
                contained.append(value is not MISSING and value is not None and expected in value)
            except TypeError:
                contained.append(False)
        passed = bool(contained) and (all(contained) if operator == "all_contains" else not any(contained))
        return passed, shown
    if operator == "all_lowercase":
        passed = bool(values) and all(
            isinstance(value, list) and bool(value) and all(isinstance(item, str) and item == item.lower() for item in value) for value in values
        )
        return passed, shown
    if operator in {"all_list_prefix", "all_list_suffix"}:
        expected = assertion.get("value")
        if not isinstance(expected, list):
            raise AssertionConfigurationError(f"{operator}.value 必须是数组")
        comparisons = []
        for value in values:
            if not isinstance(value, list):
                comparisons.append(False)
            elif operator == "all_list_prefix":
                comparisons.append(value[: len(expected)] == expected)
            else:
                comparisons.append(value[-len(expected) :] == expected if expected else True)
        return bool(comparisons) and all(comparisons), shown
    if operator == "all_match":
        pattern = assertion.get("value")
        if not isinstance(pattern, str):
            raise AssertionConfigurationError("all_match.value 必须是正则字符串")
        passed = bool(values) and all(isinstance(value, str) and re.fullmatch(pattern, value) is not None for value in values)
        return passed, shown
    if operator == "all_ordered_subset_of":
        allowed = assertion.get("allowed")
        required = assertion.get("required", [])
        if not isinstance(allowed, list) or not isinstance(required, list):
            raise AssertionConfigurationError("all_ordered_subset_of 需要 allowed/required 数组")
        positions = {item: index for index, item in enumerate(allowed)}
        results: list[bool] = []
        for value in values:
            if not isinstance(value, list) or any(item not in positions for item in value):
                results.append(False)
                continue
            order = [positions[item] for item in value]
            results.append(order == sorted(order) and len(set(value)) == len(value) and all(item in value for item in required))
        return bool(results) and all(results), shown
    if operator in {"all_subset", "any_subset", "none_subset"}:
        expected = assertion.get("value")
        results = [value is not MISSING and _is_subset(expected, value) for value in values]
        if operator == "all_subset":
            passed = bool(results) and all(results)
        elif operator == "any_subset":
            passed = any(results)
        else:
            passed = bool(mappings) and not any(results)
        return passed, shown
    if operator == "distinct_count_at_least":
        expected = assertion.get("value")
        distinct = {_serialized(value) for value in values}
        passed = isinstance(expected, int) and not isinstance(expected, bool) and len(distinct) >= expected
        return passed, {"distinct_count": len(distinct), **shown}
    if operator == "distinct_values_equal":
        expected = assertion.get("value")
        if not isinstance(expected, list):
            raise AssertionConfigurationError("distinct_values_equal.value 必须是数组")
        actual = {_serialized(value) for value in values}
        wanted = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in expected}
        return bool(values) and actual == wanted, {"distinct_values": [_json_safe(value) for value in values]}
    if operator == "same_set_distinct_order":
        minimum_records = assertion.get("minimum_records")
        minimum_artifacts = assertion.get("minimum_artifacts")
        minimum_orders = assertion.get("minimum_distinct_orders")
        if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (minimum_records, minimum_artifacts, minimum_orders)):
            raise AssertionConfigurationError("same_set_distinct_order 的三个 minimum_* 必须是正整数")
        groups: dict[str, dict[str, Any]] = {}
        invalid: list[str] = []
        for observation, sequence in zip(matched, values, strict=True):
            if not isinstance(sequence, list):
                invalid.append(observation.record_id)
                continue
            key = json.dumps(sorted({_serialized(item) for item in sequence}), ensure_ascii=False, separators=(",", ":"))
            group = groups.setdefault(key, {"values": [], "artifacts": set(), "orders": set(), "record_ids": []})
            group["values"].append(sequence)
            group["artifacts"].add(observation.artifact_path)
            group["orders"].add(json.dumps(_json_safe(sequence), ensure_ascii=False, separators=(",", ":")))
            group["record_ids"].append(observation.record_id)
        summaries = [
            {
                "extension_set": json.loads(key),
                "record_count": len(group["values"]),
                "artifact_count": len(group["artifacts"]),
                "distinct_order_count": len(group["orders"]),
                "record_ids": group["record_ids"],
                "values": group["values"],
            }
            for key, group in sorted(groups.items())
        ]
        qualifying = [
            item
            for item in summaries
            if item["record_count"] >= minimum_records and item["artifact_count"] >= minimum_artifacts and item["distinct_order_count"] >= minimum_orders
        ]
        return bool(qualifying) and not invalid, {
            "matching_group_count": len(qualifying),
            "selected_group": qualifying[0] if qualifying else None,
            "groups": summaries,
            "invalid_record_ids": invalid,
        }
    if operator == "all_fields_equal":
        left_path, right_path = assertion.get("left_path"), assertion.get("right_path")
        if not isinstance(left_path, str) or not isinstance(right_path, str):
            raise AssertionConfigurationError("all_fields_equal 需要 left_path/right_path")
        pairs = [(_resolve_path(item, left_path), _resolve_path(item, right_path)) for item in mappings]
        passed = bool(pairs) and all(left is not MISSING and right is not MISSING and left == right for left, right in pairs)
        return passed, {"pairs": [{"left": _json_safe(left), "right": _json_safe(right)} for left, right in pairs]}
    if operator in {"all_list_all_same", "all_list_all_different"}:
        results = []
        for value in values:
            if not isinstance(value, list) or len(value) < 2:
                results.append(False)
                continue
            serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            results.append(len(set(serialized)) == 1 if operator == "all_list_all_same" else len(set(serialized)) == len(serialized))
        return bool(results) and all(results), shown
    raise AssertionConfigurationError(f"断言 operator 未实现：{operator}")


def _side_restriction_contract(profile: Mapping[str, Any]) -> dict[str, Any]:
    """只取侧别限定投影，不构造完整契约（契约冻结校验不归本模块）。"""

    return {"side_restricted_checks": derive_side_restricted_checks(profile)}


def evaluate_rule(
    profile: Mapping[str, Any],
    rule_id: str,
    observations: Sequence[Observation],
    capture_manifest: Mapping[str, Any],
    side: str | None = None,
) -> list[dict[str, Any]]:
    """执行一条规则的全部 check：首项为场景证据覆盖；给出 ``side`` 时跳过契约登记为本侧不适用的 check。"""

    rule = _profile_rule(profile, rule_id)
    scenario_kinds = {scenario["scenario_id"]: scenario["required_artifact_kinds"] for scenario in profile["scenarios"]}
    artifacts = capture_manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise AssertionConfigurationError("评估用 manifest artifacts 缺失")
    coverage_actual: dict[str, list[str]] = {}
    coverage_expected: dict[str, list[str]] = {}
    coverage_paths: set[str] = set()
    coverage_passed = True
    for scenario_id in rule["scenario_ids"]:
        required = sorted(set(scenario_kinds[scenario_id]))
        present = [artifact for artifact in artifacts if scenario_id in artifact["scenario_ids"]]
        actual = sorted({artifact["kind"] for artifact in present})
        coverage_paths.update(artifact["path"] for artifact in present)
        coverage_expected[scenario_id] = required
        coverage_actual[scenario_id] = actual
        if not set(required) <= set(actual):
            coverage_passed = False
    results: list[dict[str, Any]] = [
        {
            "id": COVERAGE_CHECK_ID,
            "description": "本规则所需证据场景均具有规定类型的已绑定原始证据",
            "passed": coverage_passed,
            "expected": coverage_expected,
            "actual": coverage_actual,
            "evidence_paths": sorted(coverage_paths),
        }
    ]
    contract = _side_restriction_contract(profile) if side else None
    for check in rule["checks"]:
        if contract is not None and not check_applies_to_side(contract, rule_id, check["id"], side):
            continue
        matched = _select_observations(observations, check["select"], rule["scenario_ids"])
        passed, actual = _evaluate_assertion(matched, check["assertion"])
        evidence_paths = sorted({path for item in matched for path in item.evidence_paths})
        if not evidence_paths:
            wanted = set(check["select"].get("scenario_ids", rule["scenario_ids"]))
            evidence_paths = sorted({path for item in observations if wanted & set(item.scenario_ids) for path in item.evidence_paths})
        results.append(
            {
                "id": check["id"],
                "description": check["description"],
                "passed": passed,
                "expected": dict(check["assertion"]),
                "actual": actual,
                "evidence_paths": evidence_paths,
            }
        )
    return results


# ---------------------------------------------------------------------------
# 命令与结果
# ---------------------------------------------------------------------------


def build_assertion_command(
    *,
    rule_id: str,
    capture_manifest: str,
    evidence_root: str,
    output: str,
    profile: str,
    rule_manifest: str,
    expected_claude_version: str | None = None,
    expected_profile_sha256: str | None = None,
    side: str | None = None,
    capture_manifest_projection: str | None = None,
) -> list[str]:
    """checker 命令的唯一构造器：参数顺序固定，builder 与 accept 用它复算命令摘要。"""

    command = [
        "python3",
        CHECKER_RELATIVE_PATH,
        "--rule-id",
        rule_id,
        "--capture-manifest",
        capture_manifest,
        "--evidence-root",
        evidence_root,
        "--profile",
        profile,
        "--rule-manifest",
        rule_manifest,
    ]
    if expected_claude_version is not None:
        command.extend(["--expected-claude-version", expected_claude_version])
    if expected_profile_sha256 is not None:
        command.extend(["--expected-profile-sha256", expected_profile_sha256])
    if side is not None:
        command.extend(["--side", side])
    if capture_manifest_projection is not None:
        command.extend([PROJECTION_FLAG, capture_manifest_projection])
    command.extend(["--output", output])
    return command


def build_assertion_result(
    *,
    rule_id: str,
    checks: Sequence[Mapping[str, Any]],
    command: Sequence[str],
    started_at: str,
    finished_at: str,
    projection_sha256: str | None = None,
    capture_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """单规则断言结果；投影模式另带投影文件摘要与完整 manifest 摘要（只作审计绑定，不进依赖摘要）。"""

    passed = bool(checks) and all(check.get("passed") is True for check in checks)
    result: dict[str, Any] = {
        "schema_version": ASSERTION_SCHEMA_VERSION,
        "rule_id": rule_id,
        "status": "pass" if passed else "fail",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": 0 if passed else 1,
        "checker_sha256": file_sha256(Path(__file__)),
        "command_sha256": command_sha256(command),
        "checks": [dict(check) for check in checks],
    }
    if (projection_sha256 is None) != (capture_manifest_sha256 is None):
        raise AssertionConfigurationError("投影模式必须同时给出投影摘要与完整 manifest 摘要")
    if projection_sha256 is not None:
        result["projection_sha256"] = projection_sha256
        result["capture_manifest_sha256"] = capture_manifest_sha256
    return result


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    from tools.official_client_capture.capturelib.security import secure_write_json

    secure_write_json(path, payload)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按批准断言画像对 Claude 官方／候选证据执行单规则断言")
    parser.add_argument("--rule-id", required=True)
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--rule-manifest", type=Path, required=True)
    parser.add_argument("--expected-claude-version", required=True)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--side", choices=("official", "candidate"), help="验收侧；给出时跳过契约登记为本侧不适用的 check")
    parser.add_argument(
        PROJECTION_FLAG,
        dest="capture_manifest_projection",
        type=Path,
        help="per-rule 投影 manifest；给出时以完整 manifest 重算并逐字比对，再只用投影解析证据",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _build_expectations_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{Path(__file__).name} {EXPECTATIONS_COMMAND}", description="由台账、场景清单与判据模板生成 Claude 断言画像")
    parser.add_argument("--claude-version", required=True)
    parser.add_argument("--ledger", type=Path, required=True, help="AtomicAssertionLedger 或基线 RequiredRules 清单")
    parser.add_argument("--scenarios", type=Path, required=True, help="目标版本场景清单 claude_upgrade_scenarios_<版本>.json")
    parser.add_argument("--templates", type=Path, required=True, help="判据模板库")
    parser.add_argument("--release-profile", type=Path, help="目标 Release 的 Profile 制品")
    parser.add_argument("--release-wire", type=Path, help="目标 Release 的 Wire 制品")
    parser.add_argument("--rule-manifest", type=Path, help="目标规则清单（给出时规则顺序随它）")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def expectations_main(argv: Sequence[str]) -> int:
    arguments = _build_expectations_parser().parse_args(list(argv))
    try:
        if arguments.output.exists() or arguments.output.is_symlink():
            raise AssertionConfigurationError(f"输出已存在，拒绝覆盖：{arguments.output}")
        document = build_expectations(
            claude_version=arguments.claude_version,
            ledger=_load_json(arguments.ledger, "台账"),
            scenarios_payload=_load_json(arguments.scenarios, "场景清单"),
            templates=_load_json(arguments.templates, "判据模板库"),
            release_profile=_load_json(arguments.release_profile, "Release Profile") if arguments.release_profile else None,
            release_wire=_load_json(arguments.release_wire, "Release Wire") if arguments.release_wire else None,
            rule_manifest=_load_json(arguments.rule_manifest, "规则清单") if arguments.rule_manifest else None,
        )
    except AssertionConfigurationError as error:
        print(f"断言画像生成失败：{error}", file=sys.stderr)
        return 1
    raw = render_expectations(document)
    arguments.output.write_bytes(raw)
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "sha256": bytes_sha256(raw),
                "rule_count": len(document["rules"]),
                "check_count": sum(len(rule["checks"]) for rule in document["rules"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    if arguments_list[:1] == [EXPECTATIONS_COMMAND]:
        return expectations_main(arguments_list[1:])
    args = _build_parser().parse_args(arguments_list)
    started_at = utc_now()
    projection_digest: str | None = None
    manifest_digest: str | None = None
    try:
        profile = load_profile(
            args.profile,
            args.rule_manifest,
            expected_claude_version=args.expected_claude_version,
            expected_profile_sha256=args.expected_profile_sha256,
            verify_frozen_digest=args.expected_profile_sha256 is None,
        )
        manifest_path = args.capture_manifest
        if args.capture_manifest_projection is not None:
            _projection, projection_digest, manifest_digest = verify_capture_manifest_projection(
                profile, args.rule_id, args.capture_manifest, args.capture_manifest_projection, args.evidence_root, args.expected_claude_version
            )
            manifest_path = args.capture_manifest_projection
        manifest, observations = load_observations(manifest_path, args.evidence_root, args.expected_claude_version)
        checks = evaluate_rule(profile, args.rule_id, observations, manifest, args.side)
    except (AssertionConfigurationError, OSError, ValueError) as error:
        # 投影已核对通过时保留两项摘要（与 Codex 同一口径）：builder 据此把这次失败记进 checkpoint，而不是当成投影错位。
        checks = [
            {
                "id": "input-validation",
                "description": "断言画像、评估用 manifest 与原始证据必须完整且相互绑定",
                "passed": False,
                "expected": {"input_valid": True},
                "actual": {"input_valid": False, "error": str(error)},
                "evidence_paths": [str(args.capture_manifest)],
            }
        ]
    command = build_assertion_command(
        rule_id=args.rule_id,
        capture_manifest=str(args.capture_manifest),
        evidence_root=str(args.evidence_root),
        output=str(args.output),
        profile=str(args.profile),
        rule_manifest=str(args.rule_manifest),
        expected_claude_version=args.expected_claude_version,
        expected_profile_sha256=args.expected_profile_sha256,
        side=args.side,
        capture_manifest_projection=str(args.capture_manifest_projection) if args.capture_manifest_projection is not None else None,
    )
    result = build_assertion_result(
        rule_id=args.rule_id,
        checks=checks,
        command=command,
        started_at=started_at,
        finished_at=utc_now(),
        projection_sha256=projection_digest,
        capture_manifest_sha256=manifest_digest,
    )
    _write_json(args.output, result)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
