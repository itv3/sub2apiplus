#!/usr/bin/env python3
"""Claude FW-G 历史制品的生成命令行：从 FW-F 受批 Snapshot 与 2.1.226 原生证据重放 Profile、Wire、模型能力目录与 Release。

投影算法（Wire 投影策略的校验、HTTP/1.1 与 TLS 解析、版本指纹复算、实现策略／工具策略／thinking.display／模型能力目录／
Wire／Release 身份的构造）在受管模块 ``tools/official_client_capture/claude_upgrade_wire_projection.py``，本文件从那里导入。
原因：VC-3 的 ``prepare-profile`` 在进程内调用投影函数，而受监督部署与真实链／pre-A3 副本树只交换两个受管根，根外的本文件
在部署树里不存在，其变化也不进 Claude 工具身份（2026-10-07 起投影函数逐字搬入受管模块，历史命令行与 2.1.226 逐字回归不变）。
本文件只保留历史 FW-G 专用的部分：generation policy 的版本事实、历史布局的证据来源 ``FwfCampaignSource``、追加式迁移收据、
源码 transition、历史 Release 身份与命令行入口。

一、确定性投影（B3-03）
=====================
同一组冻结输入在任何工作目录、任何时间重跑得到逐字相同的输出：

* 版本、平台与时间不写死：平台取已校验画像的 ``identity.platform``，迁移收据的日期由 ``--receipt-date`` 显式给出，
  Release 的 persona（投影模块）与收据阶段（本文件）是合同的具名常量；
* 输出里记录的证据与制品路径一律是仓库相对路径（``repository_relative``），与调用方传绝对路径还是相对路径无关；
* 输出只写一次：目标已存在且内容相同即复用，内容不同即拒绝覆盖；
* 迁移收据里源码 transition 的「之后」摘要取 ``--source-commit`` 指定提交里的 blob，不读工作树源码。

二、两份策略（B3-03 补充项：拆分策略 Schema）
==========================================
* **generation policy**（``claude-fw-g-generation-policy/v2``，历史 FW-G 专用、内容冻结）：冻结输入摘要、前一 Release、
  模型能力 Campaign 计数与迁移收据坐标，以及 official finalize／acceptance 等后续阶段身份。只给历史 CLI 用，指南 §4.3.1
  不许把它复制成新 Campaign 的 VC-3 policy。
* **Wire 投影策略**（``claude-code-wire-projection-policy/v1``，每个版本一份、只含投影所需的版本事实）：模型闭集与角色
  （主模型、TUI 标题模型、fallback 模型、legacy retry fallback、server fallback 锁存）、effort 闭集、版本指纹与 cch
  的 attestation、工具目录选择器（deferred MCP 与 background 工具数、advisor 工具类型、system 块数、background 请求数）、
  TLS 期望向量、实现策略的 identity／headers／retry 三段、thinking.display 取值、模型目录的别名与未知模型策略。
  原先散在代码里的模型名、版本指纹 salt 与 message index 等字面量全部移入这里；2.1.226 的值见
  ``tools/official_client_capture/claude_upgrade_wire_projection_2_1_226.json``。校验与加载在投影模块。

三、证据来源（按证据布局分离）
============================
投影函数只经「证据来源」对象取请求，不认目录布局：

* ``FwfCampaignSource``（本文件）：历史 FW-F／FW-G 布局（``attempts/<probe>/attempt-NNN/relay``、thinking.display 补充
  Campaign、模型能力 Campaign），R 通道按 MeasuredRuleLedger 绑定；只用于重放 2.1.226 历史制品，逐字复算已入库字节。
* Claude 升级的 VC-1 布局由 ``tools/official_client_capture/claude_upgrade_release.py`` 提供同名方法的来源对象
  （capture manifest 的逐连接标签 + EvidencePackage v3 的外部证据绑定）。

来源对象的方法：``messages``（某 probe 的推理请求，按连接序）、``select_messages``、``base_select``（主模型 probe 的
任意请求）、``model_select``／``model_requests``（非主模型 probe）、``thinking_display_request``、
``thinking_production_receipt``、``evidence_ledger``、``catalog_evidence``。非主模型的 probe ID 为
``<模型>-<基础 probe>``（FW-G 模型能力 Campaign 的约定）。

本工具与投影模块都不导入 ``claude_fw_f_profile``（附录 C：只读保留、不调用）：HTTP/1.1 请求流解析在投影模块内，语义与
原解析器逐字相同，由 2.1.226 历史回归（逐字复算已入库 Profile／Wire）锁定。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.official_client_capture.claude_generation_policy import (
    GenerationPolicyError,
    load_generation_policy,
)
# 投影算法（受管，见模块说明）：只导入本文件历史部分实际用到的名字；投影的其余函数与常量直接从受管模块取。
from tools.official_client_capture.claude_upgrade_wire_projection import (
    MESSAGES_TARGET,
    PROBE_THINKING_DISPLAY_PREFIX,
    ProjectionError,
    build_implementation_policy,
    build_model_capability_catalog,
    build_release_identity,
    build_wire_artifact,
    canonical_bytes,
    load_projection_policy,
    parse_http_stream,
    select_request,
    sha256_hex,
)


EXPECTED_PROFILE_DIGEST: str
PREVIOUS_WIRE_DIGEST: str
PREVIOUS_RELEASE_DIGEST: str
PREVIOUS_BUNDLE_DIGEST: str
EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST: str
EXPECTED_ATOMIC_LEDGER_DIGEST: str
EXPECTED_SNAPSHOT_DIGEST: str
EXPECTED_EVIDENCE_PACKAGE_DIGEST: str
EXPECTED_SUPPORT_ENVELOPE_DIGEST: str
EXPECTED_RULE_COUNT: int
EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT: int
EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT: int
EXPECTED_STRICT_ENDPOINT_COUNT: int
EXPECTED_VERSION: str
EXPECTED_MESSAGES_EVIDENCE_DIGEST: str
EXPECTED_MESSAGES_BODY_DIGEST: str
EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS: dict[str, str]
EXPECTED_TLS_PCAP_DIGEST: str
EXPECTED_SYSTEM_TEXT_DIGESTS: list[str]
EXPECTED_CIPHERS: list[int]
EXPECTED_GROUPS: list[int]
EXPECTED_POINT_FORMATS: list[int]
EXPECTED_SIGNATURE_ALGORITHMS: list[int]
EXPECTED_SUPPORTED_VERSIONS: list[int]
EXPECTED_KEY_SHARE_GROUPS: list[int]
EXPECTED_PSK_MODES: list[int]
EXPECTED_WITH_ALPN_EXTENSIONS: list[int]
EXPECTED_WITHOUT_ALPN_EXTENSIONS: list[int]
MODEL_CAPABILITY_MODELS: tuple[str, ...]
MODEL_CAPABILITY_EFFORTS: tuple[str, ...]
MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS: int
MODEL_CAPABILITY_FAILED_ATTEMPTS: set[str]
MODEL_CAPABILITY_BASE_COMMIT: str
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# 本生成器合同的固定身份（不随版本变化）：迁移收据的阶段名（Release bundle 的 persona 在投影模块）。
RECEIPT_PHASE = "FW-G"
RECEIPT_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MODEL_CAPABILITY_PRIOR_TRANSITIONS: tuple[tuple[str, str], ...]
MODEL_CAPABILITY_SOURCE_TRANSITIONS: tuple[tuple[str, str, str], ...]


def configure_generation_policy(policy_path: Path) -> None:
    """原子加载版本事实；生成器控制流不再因普通换版而修改。"""

    global EXPECTED_PROFILE_DIGEST
    global PREVIOUS_WIRE_DIGEST, PREVIOUS_RELEASE_DIGEST, PREVIOUS_BUNDLE_DIGEST
    global EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST, EXPECTED_ATOMIC_LEDGER_DIGEST
    global EXPECTED_SNAPSHOT_DIGEST, EXPECTED_EVIDENCE_PACKAGE_DIGEST
    global EXPECTED_SUPPORT_ENVELOPE_DIGEST, EXPECTED_RULE_COUNT
    global EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT, EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
    global EXPECTED_STRICT_ENDPOINT_COUNT
    global EXPECTED_VERSION, EXPECTED_MESSAGES_EVIDENCE_DIGEST
    global EXPECTED_MESSAGES_BODY_DIGEST, EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS
    global EXPECTED_TLS_PCAP_DIGEST, EXPECTED_SYSTEM_TEXT_DIGESTS
    global EXPECTED_CIPHERS, EXPECTED_GROUPS, EXPECTED_POINT_FORMATS
    global EXPECTED_SIGNATURE_ALGORITHMS, EXPECTED_SUPPORTED_VERSIONS
    global EXPECTED_KEY_SHARE_GROUPS, EXPECTED_PSK_MODES
    global EXPECTED_WITH_ALPN_EXTENSIONS, EXPECTED_WITHOUT_ALPN_EXTENSIONS
    global MODEL_CAPABILITY_MODELS, MODEL_CAPABILITY_EFFORTS
    global MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS, MODEL_CAPABILITY_FAILED_ATTEMPTS
    global MODEL_CAPABILITY_BASE_COMMIT, MODEL_CAPABILITY_PRIOR_TRANSITIONS
    global MODEL_CAPABILITY_SOURCE_TRANSITIONS

    try:
        document = load_generation_policy(policy_path)
        target = document["target"]
        frozen = document["frozen_inputs"]
        previous = document["previous_release"]
        tls = document["tls"]
        model = document["model_capability"]

        EXPECTED_VERSION = str(target["version"])
        EXPECTED_RULE_COUNT = int(target["required_rule_count"])
        EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT = int(
            target["profile_atomic_assertion_count"]
        )
        EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT = int(
            target["total_atomic_assertion_count"]
        )
        EXPECTED_STRICT_ENDPOINT_COUNT = int(target["strict_endpoint_count"])
        MODEL_CAPABILITY_MODELS = tuple(target["models"])
        MODEL_CAPABILITY_EFFORTS = tuple(target["efforts"])

        EXPECTED_PROFILE_DIGEST = frozen["profile_sha256"]
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST = frozen[
            "required_rules_manifest_sha256"
        ]
        EXPECTED_ATOMIC_LEDGER_DIGEST = frozen["atomic_ledger_sha256"]
        EXPECTED_SNAPSHOT_DIGEST = frozen["snapshot_sha256"]
        EXPECTED_EVIDENCE_PACKAGE_DIGEST = frozen["evidence_package_sha256"]
        EXPECTED_SUPPORT_ENVELOPE_DIGEST = frozen["support_envelope_sha256"]
        EXPECTED_MESSAGES_EVIDENCE_DIGEST = frozen["messages_evidence_sha256"]
        EXPECTED_MESSAGES_BODY_DIGEST = frozen["messages_body_sha256"]
        EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS = dict(
            frozen["thinking_display_request_sha256"]
        )
        EXPECTED_TLS_PCAP_DIGEST = frozen["tls_pcap_sha256"]
        EXPECTED_SYSTEM_TEXT_DIGESTS = list(frozen["system_text_sha256"])

        PREVIOUS_WIRE_DIGEST = previous["wire_sha256"]
        PREVIOUS_RELEASE_DIGEST = previous["release_sha256"]
        PREVIOUS_BUNDLE_DIGEST = previous["bundle_sha256"]

        EXPECTED_CIPHERS = list(tls["cipher_suites"])
        EXPECTED_GROUPS = list(tls["supported_groups"])
        EXPECTED_POINT_FORMATS = list(tls["point_formats"])
        EXPECTED_SIGNATURE_ALGORITHMS = list(tls["signature_algorithms"])
        EXPECTED_SUPPORTED_VERSIONS = list(tls["supported_versions"])
        EXPECTED_KEY_SHARE_GROUPS = list(tls["key_share_groups"])
        EXPECTED_PSK_MODES = list(tls["psk_modes"])
        EXPECTED_WITH_ALPN_EXTENSIONS = list(tls["with_alpn_extensions"])
        EXPECTED_WITHOUT_ALPN_EXTENSIONS = list(tls["without_alpn_extensions"])

        MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS = int(model["successful_attempts"])
        MODEL_CAPABILITY_FAILED_ATTEMPTS = set(
            model["historical_failed_attempt_ids"]
        )
        MODEL_CAPABILITY_BASE_COMMIT = model["base_commit"]
        MODEL_CAPABILITY_PRIOR_TRANSITIONS = tuple(
            (item["path"], item["sha256"])
            for item in model["prior_transitions"]
        )
        MODEL_CAPABILITY_SOURCE_TRANSITIONS = tuple(
            (item["path"], item["from_sha256"], item["reason"])
            for item in model["source_transitions"]
        )
    except (GenerationPolicyError, KeyError, TypeError, ValueError) as exc:
        raise ProjectionError(
            f"Claude FW-G generation policy 非法：{policy_path}: {exc}"
        ) from exc

    digest_values = [
        EXPECTED_PROFILE_DIGEST,
        PREVIOUS_WIRE_DIGEST,
        PREVIOUS_RELEASE_DIGEST,
        PREVIOUS_BUNDLE_DIGEST,
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        EXPECTED_ATOMIC_LEDGER_DIGEST,
        EXPECTED_SNAPSHOT_DIGEST,
        EXPECTED_EVIDENCE_PACKAGE_DIGEST,
        EXPECTED_SUPPORT_ENVELOPE_DIGEST,
        EXPECTED_MESSAGES_EVIDENCE_DIGEST,
        EXPECTED_MESSAGES_BODY_DIGEST,
        EXPECTED_TLS_PCAP_DIGEST,
        *EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS.values(),
        *EXPECTED_SYSTEM_TEXT_DIGESTS,
    ]
    if (
        not EXPECTED_VERSION
        or min(
            EXPECTED_RULE_COUNT,
            EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT,
            EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT,
            EXPECTED_STRICT_ENDPOINT_COUNT,
        )
        <= 0
        or not MODEL_CAPABILITY_MODELS
        or not MODEL_CAPABILITY_EFFORTS
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in digest_values
        )
    ):
        raise ProjectionError(f"Claude FW-G generation policy 坐标不完整：{policy_path}")


def check_projection_matches_generation_policy(policy: dict[str, Any]) -> None:
    """历史 CLI：Wire 投影策略与 v2 generation policy 的重叠事实（版本、模型、effort、TLS 向量）必须逐字相同。"""

    tls = {
        "cipher_suites": EXPECTED_CIPHERS,
        "supported_groups": EXPECTED_GROUPS,
        "point_formats": EXPECTED_POINT_FORMATS,
        "signature_algorithms": EXPECTED_SIGNATURE_ALGORITHMS,
        "supported_versions": EXPECTED_SUPPORTED_VERSIONS,
        "key_share_groups": EXPECTED_KEY_SHARE_GROUPS,
        "psk_modes": EXPECTED_PSK_MODES,
        "with_alpn_extensions": EXPECTED_WITH_ALPN_EXTENSIONS,
        "without_alpn_extensions": EXPECTED_WITHOUT_ALPN_EXTENSIONS,
    }
    if (
        policy["version"] != EXPECTED_VERSION
        or tuple(policy["models"]["ordered"]) != MODEL_CAPABILITY_MODELS
        or tuple(policy["efforts"]) != MODEL_CAPABILITY_EFFORTS
        or policy["tls"] != tls
    ):
        raise ProjectionError("Wire 投影策略与 generation policy 的版本、模型、effort 或 TLS 向量不一致")


def repository_relative(path: Path) -> str:
    """输出里记录的路径：一律换算成仓库相对路径，不随调用方的工作目录或传参形式变化。"""

    absolute = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    try:
        return absolute.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ProjectionError(f"制品或证据路径不在仓库内：{absolute}") from exc


def write_once(path: Path, raw: bytes) -> str:
    """只写一次：不存在时独占新建；已存在且逐字相同返回 ``reused``，内容不同即拒绝覆盖。"""

    if path.is_symlink():
        raise ProjectionError(f"输出路径是符号链接，拒绝写入：{path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw:
            raise ProjectionError(f"输出已存在且内容不同，禁止覆盖：{path}")
        return "reused"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "generated"


def git_blob_digest_reader(commit: str) -> Callable[[str], str]:
    """返回「仓库相对路径 → 该提交里 blob 的 SHA-256」的读取器；读 Git 对象库，不读工作树。"""

    if not GIT_COMMIT_RE.fullmatch(commit):
        raise ProjectionError(f"--source-commit 必须是 40 位小写提交号：{commit!r}")

    def digest(path: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "blob", f"{commit}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise ProjectionError(f"模型能力源码 transition 在 {commit} 缺少文件：{path}")
        return sha256_hex(completed.stdout)

    return digest


def build_model_capability_source_transitions(
    profile_path: Path,
    profile_digest: str,
    wire_path: Path,
    wire_digest: str,
    source_digest: Callable[[str], str],
) -> list[dict[str, str]]:
    """迁移收据的 transition：策略登记的源码取冻结提交里的摘要，本次生成的 Profile、Wire 取内存摘要。

    不读工作树：工作树随后续开发变化，读它会让同一组输入在不同时刻得到不同收据。
    """

    generated = {
        repository_relative(profile_path): profile_digest,
        repository_relative(wire_path): wire_digest,
    }
    specs = list(MODEL_CAPABILITY_SOURCE_TRANSITIONS)
    specs.extend(
        [
            (
                repository_relative(profile_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Profile，冻结三模型显式闭集和能力目录摘要。",
            ),
            (
                repository_relative(wire_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Wire，冻结三模型逐场景官方请求与状态差异。",
            ),
        ]
    )
    paths = [path for path, _, _ in specs]
    if len(paths) != len(set(paths)):
        raise ProjectionError("模型能力源码 transition 路径重复")
    transitions: list[dict[str, str]] = []
    for path, before_digest, reason in sorted(specs):
        after_digest = generated[path] if path in generated else source_digest(path)
        if before_digest == after_digest:
            raise ProjectionError(f"模型能力源码 transition 未发生变化：{path}")
        transitions.append(
            {
                "path": path,
                "from_sha256": before_digest,
                "to_sha256": after_digest,
                "reason": reason,
            }
        )
    return transitions


def load_frozen_json(path: Path, expected_digest: str, label: str) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = sha256_hex(raw)
    if digest != expected_digest:
        raise ProjectionError(f"{label} 摘要不一致：{digest}")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ProjectionError(f"{label} 不是 JSON 对象")
    return document


def required_rule_mapping(rules: Any, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(rules, list) or len(rules) != EXPECTED_RULE_COUNT:
        raise ProjectionError(
            f"{label} RequiredRules 数量不是 {EXPECTED_RULE_COUNT}"
        )
    result: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise ProjectionError(f"{label} RequiredRule 不是对象")
        spec_id = rule.get("spec_id")
        atomic_ids = rule.get("atomic_assertion_ids")
        if (
            not isinstance(spec_id, str)
            or not spec_id.startswith("SPEC-")
            or not isinstance(atomic_ids, list)
            or not atomic_ids
            or any(not isinstance(item, str) or not item.startswith("SPEC-") for item in atomic_ids)
            or spec_id in result
        ):
            raise ProjectionError(f"{label} RequiredRule 或原子映射非法：{spec_id}")
        result[spec_id] = tuple(atomic_ids)
    return result


def validate_fw_f_rule_authorities(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    atomic_ledger: dict[str, Any],
    evidence_package: dict[str, Any],
    support_envelope: dict[str, Any],
) -> dict[str, Any]:
    try:
        profile_document = snapshot["payload"]["document"]
        evidence_rules = evidence_package["payload"]["rules"]
        support_spec_ids = support_envelope["payload"]["target_spec_ids"]
    except (KeyError, TypeError) as exc:
        raise ProjectionError("FW-F Snapshot／EvidencePackage／SupportEnvelope 结构不完整") from exc
    if not isinstance(profile_document, dict):
        raise ProjectionError("FW-F Snapshot profile document 非法")

    manifest_mapping = required_rule_mapping(manifest.get("required_rules"), "manifest")
    snapshot_mapping = required_rule_mapping(profile_document.get("rules"), "Snapshot")
    if manifest_mapping != snapshot_mapping:
        raise ProjectionError("RequiredRules manifest 与 Snapshot 的规则／原子映射不一致")

    manifest_profile_atomic = [
        assertion_id
        for assertion_ids in manifest_mapping.values()
        for assertion_id in assertion_ids
    ]
    if (
        len(manifest_profile_atomic) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
        or len(set(manifest_profile_atomic)) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
    ):
        raise ProjectionError(
            f"{EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT} 条画像原子断言"
            "未恰好一次归属 RequiredRules"
        )

    scenario_groups = manifest.get("scenario_only_groups")
    if not isinstance(scenario_groups, list):
        raise ProjectionError("RequiredRules manifest 缺少 scenario-only 分组")
    scenario_atomic = [
        assertion_id
        for group in scenario_groups
        if isinstance(group, dict)
        for assertion_id in group.get("atomic_assertion_ids", [])
    ]
    all_manifest_atomic = manifest_profile_atomic + scenario_atomic
    ledger_entries = atomic_ledger.get("entries")
    if not isinstance(ledger_entries, list):
        raise ProjectionError("AtomicAssertionLedger entries 非法")
    ledger_atomic = [entry.get("spec_id") for entry in ledger_entries if isinstance(entry, dict)]
    if (
        len(all_manifest_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(set(all_manifest_atomic)) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(ledger_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or set(ledger_atomic) != set(all_manifest_atomic)
    ):
        raise ProjectionError(
            f"{EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT} 条 AtomicAssertionLedger "
            "未与画像／scenario-only 映射完全一致"
        )

    evidence_spec_ids = [
        rule.get("spec_id") for rule in evidence_rules if isinstance(rule, dict)
    ]
    required_spec_ids = set(manifest_mapping)
    if (
        len(evidence_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(evidence_spec_ids)) != EXPECTED_RULE_COUNT
        or set(evidence_spec_ids) != required_spec_ids
        or not isinstance(support_spec_ids, list)
        or len(support_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(support_spec_ids)) != EXPECTED_RULE_COUNT
        or set(support_spec_ids) != required_spec_ids
    ):
        raise ProjectionError("EvidencePackage／SupportEnvelope RequiredRule 集合不一致")

    strict_endpoints = profile_document.get("strict_endpoints")
    if (
        not isinstance(strict_endpoints, list)
        or len(strict_endpoints) != EXPECTED_STRICT_ENDPOINT_COUNT
    ):
        raise ProjectionError(
            "Snapshot strict endpoint 数量不是 "
            f"{EXPECTED_STRICT_ENDPOINT_COUNT}"
        )
    endpoint_spec_ids: list[str] = []
    for endpoint in strict_endpoints:
        spec_ids = endpoint.get("spec_ids") if isinstance(endpoint, dict) else None
        if (
            not isinstance(spec_ids, list)
            or not spec_ids
            or len(spec_ids) != len(set(spec_ids))
            or not set(spec_ids).issubset(required_spec_ids)
        ):
            raise ProjectionError("Snapshot strict endpoint 规则引用非法")
        endpoint_spec_ids.extend(spec_ids)
    if set(endpoint_spec_ids) != required_spec_ids:
        raise ProjectionError(
            f"{EXPECTED_STRICT_ENDPOINT_COUNT} 个 strict endpoint 的 SPEC 并集"
            f"未覆盖 {EXPECTED_RULE_COUNT} 条 RequiredRules"
        )
    return profile_document


def collect_measured_raw_sha256s(ledger_path: Path) -> set[str]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if (
        ledger.get("target_version") != EXPECTED_VERSION
        or ledger.get("rule_count") != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
    ):
        raise ProjectionError("MeasuredRuleLedger 身份或原子断言数量不一致")
    raw_sha256s: set[str] = set()
    for entry in ledger.get("entries", []):
        for reference in entry.get("evidence_refs", []):
            raw_sha256s.update(reference.get("raw_request_sha256s", []))
    if not raw_sha256s:
        raise ProjectionError("MeasuredRuleLedger 没有 R 通道请求摘要")
    return raw_sha256s


def load_scenario_messages(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
) -> list[dict[str, Any]]:
    relay_root = campaign_root / "attempts" / scenario / "attempt-001" / "relay"
    requests: list[dict[str, Any]] = []
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            if request.get("request_target") != "/v1/messages?beta=true":
                continue
            raw_sha256 = request.get("raw_sha256", "")
            if raw_sha256 not in allowed_raw_sha256s:
                raise ProjectionError(f"{scenario} 请求未绑定 MeasuredRuleLedger：{raw_sha256}")
            request = dict(request)
            request["evidence_path"] = repository_relative(path)
            requests.append(request)
    if not requests:
        raise ProjectionError(f"{scenario} 没有 messages R 通道")
    return requests


def successful_model_attempts(campaign_root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    attempts_root = campaign_root / "attempts"
    successful: dict[str, Path] = {}
    failed: set[str] = set()
    attempt_receipts: list[dict[str, Any]] = []
    telemetry_markers = ("telemetry", "event_logging", "datadog", "statsig", "analytics")
    for scenario_root in sorted(attempts_root.iterdir()):
        if not scenario_root.is_dir():
            continue
        for attempt_root in sorted(scenario_root.glob("attempt-*")):
            manifest_path = attempt_root / "relay-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            attempt_id = f"{scenario_root.name}/{attempt_root.name}"
            complete = (
                manifest.get("status") == "complete"
                and manifest.get("m_binding", {}).get("complete") is True
                and manifest.get("secret_scan", {}).get("passed") is True
                and manifest.get("dimension_evidence", {}).get("result") == "passed"
                and manifest.get("m_binding", {})
                .get("requirements", {})
                .get("hosts_restored")
                is True
            )
            if not complete:
                failed.add(attempt_id)
                attempt_receipts.append(
                    {
                        "attempt_id": attempt_id,
                        "status": "historical_failed",
                        "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    }
                )
                continue
            for item in manifest.get("dimension_evidence", {}).get(
                "actual_wire_inventory", []
            ):
                identity = " ".join(
                    str(item.get(name, ""))
                    for name in ("request_target", "host", "evidence_file")
                ).lower()
                if any(marker in identity for marker in telemetry_markers):
                    raise ProjectionError(f"模型能力 Campaign 混入遥测／行为请求：{attempt_id}")
            runtime_path = (
                campaign_root
                / "runtime-receipts"
                / f"{scenario_root.name}-{attempt_root.name}.json"
            )
            if not runtime_path.is_file():
                raise ProjectionError(f"模型能力 attempt 缺少 runtime receipt：{attempt_id}")
            successful[scenario_root.name] = attempt_root
            attempt_receipts.append(
                {
                    "attempt_id": attempt_id,
                    "status": "complete",
                    "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    "runtime_receipt_sha256": sha256_hex(runtime_path.read_bytes()),
                }
            )
    complete_count = sum(item["status"] == "complete" for item in attempt_receipts)
    if complete_count != MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS:
        raise ProjectionError(
            "模型能力成功 attempt 数量不是 "
            f"{MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS}：{complete_count}"
        )
    if failed != MODEL_CAPABILITY_FAILED_ATTEMPTS:
        raise ProjectionError(f"模型能力历史失败集合漂移：{sorted(failed)}")

    production_path = campaign_root / "environment" / "production-diff.json"
    production = json.loads(production_path.read_text(encoding="utf-8"))
    if production.get("identical") is not True or production.get("changed_fields") != []:
        raise ProjectionError("模型能力 Campaign 改变了 Vircs 生产服务")
    evidence = {
        "campaign_root": repository_relative(campaign_root),
        "successful_attempts": complete_count,
        "historical_failed_attempts": len(failed),
        "attempts": attempt_receipts,
        "production_diff": {
            "path": repository_relative(production_path),
            "sha256": sha256_hex(production_path.read_bytes()),
            "identical": True,
        },
        "behavior_traffic_disposition": "excluded-from-rules-and-difference-judgement",
    }
    evidence["campaign_sha256"] = sha256_hex(canonical_bytes(evidence))
    return successful, evidence


def load_attempt_requests(attempt_root: Path, scenario: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            request = dict(request)
            request["evidence_path"] = repository_relative(path)
            requests.append(request)
    return requests


def select_base_campaign_request(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt_roots = sorted((campaign_root / "attempts" / scenario).glob("attempt-*"))
    selected_attempt = None
    for attempt_root in attempt_roots:
        manifest_path = attempt_root / "relay-manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") == "complete"
            and manifest.get("m_binding", {}).get("complete") is True
            and manifest.get("secret_scan", {}).get("passed") is True
        ):
            selected_attempt = attempt_root
    if selected_attempt is None:
        raise ProjectionError(f"基础 Campaign 缺少成功场景：{scenario}")
    for request in load_attempt_requests(selected_attempt, scenario):
        if not predicate(request):
            continue
        if request.get("raw_sha256") not in allowed_raw_sha256s:
            raise ProjectionError(f"{scenario} 请求未绑定 MeasuredRuleLedger")
        return request
    raise ProjectionError(f"{scenario} 没有 {description}")


def select_attempt_request(
    attempts: dict[str, Path],
    scenario: str,
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt = attempts.get(scenario)
    if attempt is None:
        raise ProjectionError(f"模型能力 Campaign 缺少成功场景：{scenario}")
    selected = next(
        (request for request in load_attempt_requests(attempt, scenario) if predicate(request)),
        None,
    )
    if selected is None:
        raise ProjectionError(f"{scenario} 没有 {description}")
    return selected


# ---------------------------------------------------------------------------
# 历史 FW-F／FW-G 证据来源（只用于重放 2.1.226 历史制品）
# ---------------------------------------------------------------------------


class FwfCampaignSource:
    """历史 FW-F／FW-G 布局的证据来源。

    基础 Campaign 的推理请求只取 ``attempt-001`` 且必须绑定 MeasuredRuleLedger；主模型的任意请求取最后一个完整 attempt；
    非主模型取模型能力 Campaign 的成功 attempt（生成器历史行为，逐字保留）。thinking.display 取补充 Campaign 的冻结
    attempt 并核对 v2 generation policy 冻结的请求摘要。
    """

    # thinking.display 补充 Campaign 里各取值的成功 attempt 编号（历史布局事实）。
    THINKING_DISPLAY_ATTEMPTS = {"summarized": "002", "omitted": "001"}

    def __init__(self, campaign_root: Path, measured_ledger: Path, thinking_display_root: Path, capability_root: Path) -> None:
        self.campaign_root = campaign_root
        self.measured_ledger = measured_ledger
        self.thinking_display_root = thinking_display_root
        self.capability_root = capability_root
        self._allowed: set[str] | None = None
        self._capability: tuple[dict[str, Path], dict[str, Any]] | None = None

    def allowed(self) -> set[str]:
        if self._allowed is None:
            self._allowed = collect_measured_raw_sha256s(self.measured_ledger)
        return self._allowed

    def messages(self, probe: str) -> list[dict[str, Any]]:
        return load_scenario_messages(self.campaign_root, probe, self.allowed())

    def select_messages(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_request(self.messages(probe), predicate, description, probe)

    def base_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_base_campaign_request(self.campaign_root, probe, self.allowed(), predicate, description)

    def capability(self) -> tuple[dict[str, Path], dict[str, Any]]:
        if self._capability is None:
            self._capability = successful_model_attempts(self.capability_root)
        return self._capability

    def model_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_attempt_request(self.capability()[0], probe, predicate, description)

    def model_requests(self, probe: str) -> list[dict[str, Any]]:
        attempt = self.capability()[0].get(probe)
        if attempt is None:
            raise ProjectionError(f"模型能力 Campaign 缺少成功场景：{probe}")
        return load_attempt_requests(attempt, probe)

    def thinking_display_request(self, display: str) -> dict[str, Any]:
        probe = f"{PROBE_THINKING_DISPLAY_PREFIX}{display}"
        attempt = self.THINKING_DISPLAY_ATTEMPTS.get(display)
        if attempt is None:
            raise ProjectionError(f"thinking.display 补充 Campaign 没有 {display} 的冻结 attempt")
        attempt_root = self.thinking_display_root / "attempts" / probe / f"attempt-{attempt}"
        manifest = json.loads((attempt_root / "relay-manifest.json").read_text(encoding="utf-8"))
        dimension = manifest.get("dimension_evidence") or {}
        if (
            manifest.get("status") != "complete"
            or manifest.get("m_binding", {}).get("complete") is not True
            or manifest.get("secret_scan", {}).get("passed") is not True
            or dimension.get("result") != "passed"
        ):
            raise ProjectionError(f"{probe} R/M 没有完整通过")
        summary = json.loads((attempt_root / "results" / "v4-summary.json").read_text(encoding="utf-8"))
        argv = summary.get("inner_result", {}).get("invocation", {}).get("argv_redacted", [])
        expected_pair = ["--thinking-display", display]
        if summary.get("valid") is not True or not any(argv[index : index + 2] == expected_pair for index in range(len(argv) - 1)):
            raise ProjectionError(f"{probe} 未绑定冻结 CLI 参数")
        requests: list[dict[str, Any]] = []
        for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
            for request in parse_http_stream(path.read_bytes(), probe):
                if request.get("request_target") != MESSAGES_TARGET:
                    continue
                request = dict(request)
                request["evidence_path"] = repository_relative(path)
                requests.append(request)
        if len(requests) != 1:
            raise ProjectionError(f"{probe} messages R 通道数量不是 1")
        if requests[0].get("raw_sha256") != EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS[display]:
            raise ProjectionError(f"{probe} R 通道摘要漂移")
        return requests[0]

    def thinking_production_receipt(self) -> dict[str, str]:
        path = self.thinking_display_root / "environment" / "production-change-receipt.json"
        production = json.loads(path.read_text(encoding="utf-8"))
        if production.get("result") != "passed" or production.get("differences") != []:
            raise ProjectionError("thinking.display 补充 Campaign 影响了生产环境")
        return {"path": repository_relative(path), "sha256": sha256_hex(path.read_bytes())}

    def evidence_ledger(self) -> dict[str, str]:
        return {"path": repository_relative(self.measured_ledger), "sha256": sha256_hex(self.measured_ledger.read_bytes())}

    def catalog_evidence(self) -> dict[str, Any]:
        return self.capability()[1]


def extend_profile_for_model_capabilities(
    base_document: dict[str, Any],
    model_catalog: dict[str, Any],
    required_rule_count: int,
) -> dict[str, Any]:
    """历史 FW-G：把模型能力目录的闭集、别名／未知模型策略与目录摘要写进画像。"""

    document = copy.deepcopy(base_document)
    document["schema_version"] = "claude-code-fw-f-target-profile/v6"
    identity = document.setdefault("identity", {})
    identity["supported_models"] = [model["canonical_model"] for model in model_catalog["models"]]
    identity["model_alias_policy"] = model_catalog["alias_policy"]
    identity["unknown_model_policy"] = model_catalog["unknown_model_policy"]
    identity["model_capability_catalog_sha256"] = sha256_hex(canonical_bytes(model_catalog))
    body = document.setdefault("body", {})
    field_types = body.setdefault("field_types", {})
    field_types["fallbacks"] = ["array"]
    optional = body.setdefault("optional_fields", [])
    if "fallbacks" not in optional:
        optional.append("fallbacks")
    optional.sort()
    if len(document.get("rules", [])) != required_rule_count:
        raise ProjectionError(f"模型能力扩展改变了 {required_rule_count} 条 RequiredRules")
    return document


def build_model_capability_release_identity(
    profile_digest: str,
    wire_digest: str,
    model_catalog_digest: str,
    platform: str,
) -> tuple[str, str]:
    """历史 CLI：Release 身份按 v2 generation policy 的目标坐标计算。"""

    return build_release_identity(
        version=EXPECTED_VERSION,
        platform=platform,
        profile_digest=profile_digest,
        wire_digest=wire_digest,
        model_catalog_digest=model_catalog_digest,
        required_rules_manifest_sha256=EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        required_rule_count=EXPECTED_RULE_COUNT,
    )


def build_model_capability_receipt(
    model_catalog: dict[str, Any],
    profile_path: Path,
    profile_digest: str,
    wire_path: Path,
    wire_digest: str,
    release_digest: str,
    bundle_digest: str,
    *,
    receipt_date: str,
    platform: str,
    source_digest: Callable[[str], str],
) -> dict[str, Any]:
    if not RECEIPT_DATE_RE.fullmatch(receipt_date):
        raise ProjectionError(f"--receipt-date 必须是 YYYY-MM-DD：{receipt_date!r}")
    model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
    return {
        "schema_version": "official-egress-claude-model-capability-source-transition/v1",
        "date": receipt_date,
        "phase": RECEIPT_PHASE,
        "base_commit": MODEL_CAPABILITY_BASE_COMMIT,
        "prior_transitions": [
            {"path": path, "sha256": digest}
            for path, digest in MODEL_CAPABILITY_PRIOR_TRANSITIONS
        ],
        "target": {
            "product": "claude-code",
            "version": EXPECTED_VERSION,
            "platform": platform,
            "models": list(MODEL_CAPABILITY_MODELS),
        },
        "invariants": {
            "required_rule_count_before": EXPECTED_RULE_COUNT,
            "required_rule_count_after": EXPECTED_RULE_COUNT,
            "required_rules_duplicated_per_model": False,
            "unknown_model_policy": "deny",
            "alias_policy": "explicit-only",
            "telemetry_and_behavior_traffic": "excluded",
        },
        "evidence": model_catalog["evidence"],
        "model_capability_catalog_sha256": model_catalog_digest,
        "artifacts": {
            "before": {
                "profile_sha256": EXPECTED_PROFILE_DIGEST,
                "wire_sha256": PREVIOUS_WIRE_DIGEST,
                "release_sha256": PREVIOUS_RELEASE_DIGEST,
                "bundle_sha256": PREVIOUS_BUNDLE_DIGEST,
            },
            "after": {
                "profile": repository_relative(profile_path),
                "profile_sha256": profile_digest,
                "wire": repository_relative(wire_path),
                "wire_sha256": wire_digest,
                "release_sha256": release_digest,
                "bundle_sha256": bundle_digest,
            },
        },
        "transitions": build_model_capability_source_transitions(
            profile_path,
            profile_digest,
            wire_path,
            wire_digest,
            source_digest,
        ),
        "safety": {
            "vircs_production_changed": False,
            "production_selector_changed": False,
            "dmit_deployed": False,
        },
        "result": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--generation-policy",
        type=Path,
        required=True,
        help="冻结目标版本、输入摘要、模型能力 Campaign 计数与迁移收据坐标的历史 FW-G 策略（v2）",
    )
    parser.add_argument(
        "--projection-policy",
        type=Path,
        help="生成模型能力目录与 Wire 时必填：该版本的 Wire 投影策略（模型角色、attestation、选择器、TLS 与实现策略三段）",
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--required-rules-manifest", type=Path, required=True)
    parser.add_argument("--measured-ledger", type=Path, required=True)
    parser.add_argument("--evidence-package", type=Path, required=True)
    parser.add_argument("--support-envelope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--messages-evidence", type=Path)
    parser.add_argument("--tls-pcap", type=Path)
    parser.add_argument("--wire-output", type=Path)
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument("--thinking-display-campaign-root", type=Path)
    parser.add_argument("--model-capability-campaign-root", type=Path)
    parser.add_argument("--model-capability-receipt-output", type=Path)
    parser.add_argument(
        "--receipt-date",
        help="写迁移收据时必填：收据日期（YYYY-MM-DD），由调用方按批准时刻给出，生成器不取当前时间",
    )
    parser.add_argument(
        "--source-commit",
        help="写迁移收据时必填：源码 transition 的「之后」摘要所取的冻结提交（40 位），不读工作树",
    )
    args = parser.parse_args()

    configure_generation_policy(args.generation_policy)

    snapshot = load_frozen_json(args.snapshot, EXPECTED_SNAPSHOT_DIGEST, "Snapshot")
    manifest = load_frozen_json(
        args.required_rules_manifest,
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        "RequiredRules manifest",
    )
    atomic_ledger = load_frozen_json(
        args.measured_ledger,
        EXPECTED_ATOMIC_LEDGER_DIGEST,
        "AtomicAssertionLedger",
    )
    evidence_package = load_frozen_json(
        args.evidence_package,
        EXPECTED_EVIDENCE_PACKAGE_DIGEST,
        "EvidencePackage",
    )
    support_envelope = load_frozen_json(
        args.support_envelope,
        EXPECTED_SUPPORT_ENVELOPE_DIGEST,
        "SupportEnvelope",
    )
    document = validate_fw_f_rule_authorities(
        snapshot,
        manifest,
        atomic_ledger,
        evidence_package,
        support_envelope,
    )
    if document.get("identity", {}).get("version") != EXPECTED_VERSION:
        raise ProjectionError(f"Snapshot 目标版本不是 {EXPECTED_VERSION}")
    platform = document.get("identity", {}).get("platform")
    if not isinstance(platform, str) or not platform:
        raise ProjectionError("Snapshot 缺少 identity.platform")

    model_catalog = None
    source = None
    projection = None
    implementation_policy = None
    if args.model_capability_campaign_root is not None:
        if args.campaign_root is None or args.thinking_display_campaign_root is None:
            raise ProjectionError("模型能力目录需要基础 Campaign 与 thinking.display Campaign")
        if args.projection_policy is None:
            raise ProjectionError("模型能力目录需要 --projection-policy（该版本的 Wire 投影策略）")
        projection = load_projection_policy(args.projection_policy)
        check_projection_matches_generation_policy(projection)
        source = FwfCampaignSource(
            args.campaign_root,
            args.measured_ledger,
            args.thinking_display_campaign_root,
            args.model_capability_campaign_root,
        )
        implementation_policy = build_implementation_policy(source, projection)
        model_catalog = build_model_capability_catalog(
            source,
            projection,
            implementation_policy["scenarios"],
            EXPECTED_RULE_COUNT,
        )
        document = extend_profile_for_model_capabilities(document, model_catalog, EXPECTED_RULE_COUNT)

    raw = canonical_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    if model_catalog is None and digest != EXPECTED_PROFILE_DIGEST:
        raise ProjectionError(f"画像摘要不一致：{digest}")

    state = write_once(args.output, raw)
    print(f"{state} {args.output} sha256={digest} bytes={len(raw)}")
    wire_args = (
        args.messages_evidence,
        args.tls_pcap,
        args.wire_output,
        args.campaign_root,
        args.thinking_display_campaign_root,
        args.model_capability_campaign_root,
        args.model_capability_receipt_output,
    )
    if any(wire_args) and not all(wire_args):
        raise ProjectionError("生成 wire artifact 时必须同时提供五个 wire 参数")
    if all(wire_args):
        if model_catalog is None or projection is None or implementation_policy is None:
            raise ProjectionError("生成新版 wire artifact 时缺少模型能力目录")
        if not args.receipt_date or not args.source_commit:
            raise ProjectionError("写迁移收据时必须提供 --receipt-date 与 --source-commit")
        source_digest = git_blob_digest_reader(args.source_commit)
        wire_document = build_wire_artifact(
            projection,
            implementation_policy,
            model_catalog,
            args.messages_evidence.read_bytes(),
            repository_relative(args.messages_evidence),
            args.tls_pcap.read_bytes(),
            repository_relative(args.tls_pcap),
            digest,
            platform,
            frozen={
                "messages_sha256": EXPECTED_MESSAGES_EVIDENCE_DIGEST,
                "messages_body_sha256": EXPECTED_MESSAGES_BODY_DIGEST,
                "system_text_sha256": EXPECTED_SYSTEM_TEXT_DIGESTS,
                "tls_pcap_sha256": EXPECTED_TLS_PCAP_DIGEST,
            },
        )
        wire_raw = canonical_bytes(wire_document)
        wire_digest = sha256_hex(wire_raw)
        state = write_once(args.wire_output, wire_raw)
        print(
            f"{state} {args.wire_output} sha256={wire_digest} "
            f"bytes={len(wire_raw)}"
        )
        model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
        release_digest, bundle_digest = build_model_capability_release_identity(
            digest, wire_digest, model_catalog_digest, platform
        )
        receipt = build_model_capability_receipt(
            model_catalog,
            args.output,
            digest,
            args.wire_output,
            wire_digest,
            release_digest,
            bundle_digest,
            receipt_date=args.receipt_date,
            platform=platform,
            source_digest=source_digest,
        )
        receipt_raw = canonical_bytes(receipt)
        state = write_once(args.model_capability_receipt_output, receipt_raw)
        print(
            f"{state} {args.model_capability_receipt_output} "
            f"sha256={sha256_hex(receipt_raw)} bytes={len(receipt_raw)}"
        )
        print(f"release_sha256={release_digest} bundle_sha256={bundle_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
