"""加载并验证 Claude 版本专属、内容冻结的 generation policy。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "claude-fw-g-generation-policy/v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class GenerationPolicyError(ValueError):
    """generation policy 不是完整且不可变的批准身份。"""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GenerationPolicyError(f"generation policy 包含重复字段：{key}")
        result[key] = value
    return result


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GenerationPolicyError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise GenerationPolicyError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise GenerationPolicyError(f"{label}不是小写 SHA-256")
    return value


def _positive(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GenerationPolicyError(f"{label}必须是正整数")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise GenerationPolicyError(f"{label}不是安全标识")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or value != list(dict.fromkeys(value))
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise GenerationPolicyError(f"{label}为空、重复或非法")
    return value


def _integer_vector(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or value != list(dict.fromkeys(value))
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in value
        )
    ):
        raise GenerationPolicyError(f"{label}为空、重复或非法")
    return value


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GenerationPolicyError(f"{label}不是相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise GenerationPolicyError(f"{label}越出仓库")
    if path.as_posix() != value:
        raise GenerationPolicyError(f"{label}不是规范 POSIX 相对路径")
    return value


def policy_identity(document: dict[str, Any]) -> str:
    """复算排除自摘要后的 generation policy 身份。"""

    canonical = json.dumps(
        {key: value for key, value in document.items() if key != "identity_sha256"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_generation_policy(path: Path) -> dict[str, Any]:
    """严格加载策略；空值、旧身份、摘要漂移和结构扩张均失败关闭。"""

    if path.is_symlink() or not path.is_file():
        raise GenerationPolicyError(f"generation policy 不是可信普通文件：{path}")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GenerationPolicyError(f"无法读取 generation policy：{error}") from error
    policy = _expect(
        document,
        {
            "schema_version",
            "target",
            "frozen_inputs",
            "previous_release",
            "tls",
            "model_capability",
            "official_finalize",
            "acceptance",
            "identity_sha256",
        },
        "generation policy",
    )
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise GenerationPolicyError("generation policy schema_version 非法")
    identity = _sha256(policy.get("identity_sha256"), "identity_sha256")
    if policy_identity(policy) != identity:
        raise GenerationPolicyError("generation policy identity_sha256 漂移")

    target = _expect(
        policy.get("target"),
        {
            "version",
            "required_rule_count",
            "profile_atomic_assertion_count",
            "total_atomic_assertion_count",
            "strict_endpoint_count",
            "models",
            "efforts",
        },
        "target",
    )
    version = target.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise GenerationPolicyError("target.version 不是三段式版本号")
    rule_count = _positive(target.get("required_rule_count"), "target.required_rule_count")
    profile_count = _positive(
        target.get("profile_atomic_assertion_count"),
        "target.profile_atomic_assertion_count",
    )
    total_count = _positive(
        target.get("total_atomic_assertion_count"),
        "target.total_atomic_assertion_count",
    )
    strict_count = _positive(
        target.get("strict_endpoint_count"), "target.strict_endpoint_count"
    )
    if profile_count >= total_count:
        raise GenerationPolicyError("总断言数必须大于画像原子断言数")
    for field in ("models", "efforts"):
        _string_list(target.get(field), f"target.{field}")

    frozen = _expect(
        policy.get("frozen_inputs"),
        {
            "profile_sha256",
            "required_rules_manifest_sha256",
            "atomic_ledger_sha256",
            "snapshot_sha256",
            "evidence_package_sha256",
            "support_envelope_sha256",
            "messages_evidence_sha256",
            "messages_body_sha256",
            "tls_pcap_sha256",
            "thinking_display_request_sha256",
            "system_text_sha256",
        },
        "frozen_inputs",
    )
    for field, value in frozen.items():
        if field == "thinking_display_request_sha256":
            thinking = _expect(value, {"summarized", "omitted"}, f"frozen_inputs.{field}")
            for key, digest in thinking.items():
                _sha256(digest, f"frozen_inputs.{field}.{key}")
        elif field == "system_text_sha256":
            if not isinstance(value, list) or not value:
                raise GenerationPolicyError("frozen_inputs.system_text_sha256 不能为空")
            for index, digest in enumerate(value):
                _sha256(digest, f"frozen_inputs.system_text_sha256[{index}]")
        else:
            _sha256(value, f"frozen_inputs.{field}")

    previous = _expect(
        policy.get("previous_release"),
        {"wire_sha256", "release_sha256", "bundle_sha256"},
        "previous_release",
    )
    for field, value in previous.items():
        _sha256(value, f"previous_release.{field}")

    tls = _expect(
        policy.get("tls"),
        {
            "cipher_suites",
            "supported_groups",
            "point_formats",
            "signature_algorithms",
            "supported_versions",
            "key_share_groups",
            "psk_modes",
            "with_alpn_extensions",
            "without_alpn_extensions",
        },
        "tls",
    )
    for field, value in tls.items():
        _integer_vector(value, f"tls.{field}")

    model = _expect(
        policy.get("model_capability"),
        {
            "successful_attempts",
            "historical_failed_attempt_ids",
            "base_commit",
            "prior_transitions",
            "source_transitions",
        },
        "model_capability",
    )
    _positive(model.get("successful_attempts"), "model_capability.successful_attempts")
    _string_list(
        model.get("historical_failed_attempt_ids"),
        "model_capability.historical_failed_attempt_ids",
    )
    if not isinstance(model.get("base_commit"), str) or not GIT_OBJECT_RE.fullmatch(
        model["base_commit"]
    ):
        raise GenerationPolicyError("model_capability.base_commit 非法")
    prior_transitions = model.get("prior_transitions")
    if not isinstance(prior_transitions, list) or not prior_transitions:
        raise GenerationPolicyError("model_capability.prior_transitions 不能为空")
    prior_paths: list[str] = []
    for index, item in enumerate(prior_transitions):
        transition = _expect(
            item,
            {"path", "sha256"},
            f"model_capability.prior_transitions[{index}]",
        )
        prior_paths.append(
            _relative_path(
                transition.get("path"),
                f"model_capability.prior_transitions[{index}].path",
            )
        )
        _sha256(
            transition.get("sha256"),
            f"model_capability.prior_transitions[{index}].sha256",
        )
    if len(prior_paths) != len(set(prior_paths)):
        raise GenerationPolicyError("model_capability.prior_transitions 路径重复")
    source_transitions = model.get("source_transitions")
    if not isinstance(source_transitions, list) or not source_transitions:
        raise GenerationPolicyError("model_capability.source_transitions 不能为空")
    source_paths: list[str] = []
    for index, item in enumerate(source_transitions):
        transition = _expect(
            item,
            {"path", "from_sha256", "reason"},
            f"model_capability.source_transitions[{index}]",
        )
        source_paths.append(
            _relative_path(
                transition.get("path"),
                f"model_capability.source_transitions[{index}].path",
            )
        )
        _sha256(
            transition.get("from_sha256"),
            f"model_capability.source_transitions[{index}].from_sha256",
        )
        if not isinstance(transition.get("reason"), str) or not transition["reason"]:
            raise GenerationPolicyError(
                f"model_capability.source_transitions[{index}].reason 为空"
            )
    if len(source_paths) != len(set(source_paths)):
        raise GenerationPolicyError("model_capability.source_transitions 路径重复")

    official = _expect(
        policy.get("official_finalize"),
        {
            "campaign_id",
            "campaign_sha256",
            "source_bundle_sha256",
            "target_binary_sha256",
            "implementation_base_commit",
            "probe_count",
            "candidate_count",
            "dimension_count",
            "request_occurrence_count",
            "endpoint_counts",
        },
        "official_finalize",
    )
    _safe_id(official.get("campaign_id"), "official_finalize.campaign_id")
    for field in ("campaign_sha256", "source_bundle_sha256", "target_binary_sha256"):
        _sha256(official.get(field), f"official_finalize.{field}")
    if not isinstance(official.get("implementation_base_commit"), str) or not GIT_OBJECT_RE.fullmatch(
        official["implementation_base_commit"]
    ):
        raise GenerationPolicyError("official_finalize.implementation_base_commit 非法")
    for field in (
        "probe_count",
        "candidate_count",
        "dimension_count",
        "request_occurrence_count",
    ):
        _positive(official.get(field), f"official_finalize.{field}")
    endpoint_counts = official.get("endpoint_counts")
    if (
        not isinstance(endpoint_counts, dict)
        or not endpoint_counts
        or not all(isinstance(key, str) and key for key in endpoint_counts)
    ):
        raise GenerationPolicyError("official_finalize.endpoint_counts 非法")
    for key, value in endpoint_counts.items():
        _positive(value, f"official_finalize.endpoint_counts.{key}")
    if sum(endpoint_counts.values()) != official["request_occurrence_count"]:
        raise GenerationPolicyError("official_finalize endpoint 计数总和不一致")

    acceptance = _expect(
        policy.get("acceptance"),
        {
            "campaign_id",
            "candidate_id",
            "candidate_commit",
            "candidate_tree",
            "candidate_image_digest",
            "candidate_profile_sha256",
            "candidate_wire_sha256",
            "candidate_release_sha256",
            "candidate_release_bundle_sha256",
            "candidate_source_tree_sha256",
            "candidate_test_tree_sha256",
            "candidate_dependency_lock_sha256",
            "official_probe_count",
            "official_request_count",
            "official_candidate_count",
            "official_dimension_count",
            "reviewer",
            "review_ref",
            "response_compatibility_fact_id",
        },
        "acceptance",
    )
    for field in (
        "campaign_id",
        "candidate_id",
        "reviewer",
        "review_ref",
        "response_compatibility_fact_id",
    ):
        _safe_id(acceptance.get(field), f"acceptance.{field}")
    for field in ("candidate_commit", "candidate_tree"):
        value = acceptance.get(field)
        if not isinstance(value, str) or not GIT_OBJECT_RE.fullmatch(value):
            raise GenerationPolicyError(f"acceptance.{field} 非法")
    image = acceptance.get("candidate_image_digest")
    if not isinstance(image, str) or not IMAGE_ID_RE.fullmatch(image):
        raise GenerationPolicyError("acceptance.candidate_image_digest 非法")
    for field in (
        "candidate_profile_sha256",
        "candidate_wire_sha256",
        "candidate_release_sha256",
        "candidate_release_bundle_sha256",
        "candidate_source_tree_sha256",
        "candidate_test_tree_sha256",
        "candidate_dependency_lock_sha256",
    ):
        _sha256(acceptance.get(field), f"acceptance.{field}")
    for field in (
        "official_probe_count",
        "official_request_count",
        "official_candidate_count",
        "official_dimension_count",
    ):
        _positive(acceptance.get(field), f"acceptance.{field}")
    if (
        acceptance["official_probe_count"] != official["probe_count"]
        or acceptance["official_request_count"]
        != official["request_occurrence_count"]
        or acceptance["official_candidate_count"] != official["candidate_count"]
        or acceptance["official_dimension_count"] != official["dimension_count"]
        or acceptance["candidate_profile_sha256"] != frozen["profile_sha256"]
        or acceptance["candidate_release_sha256"] != previous["release_sha256"]
        or acceptance["candidate_release_bundle_sha256"]
        != previous["bundle_sha256"]
    ):
        raise GenerationPolicyError("acceptance 与目标冻结输入或官方 Campaign 计数不一致")
    if rule_count <= 0 or strict_count <= 0:
        raise GenerationPolicyError("目标规则或 strict 端点计数非法")
    return policy


def policy_binding(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    """返回可写入批准事实的策略绑定。"""

    raw = path.read_bytes()
    if policy_identity(document) != document.get("identity_sha256"):
        raise GenerationPolicyError("generation policy 在绑定前发生漂移")
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "identity_sha256": document["identity_sha256"],
    }
