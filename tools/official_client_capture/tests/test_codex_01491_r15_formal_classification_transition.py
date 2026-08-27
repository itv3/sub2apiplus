"""冻结 Codex CLI 0.149.1 r15 Formal 分类后继 transition。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_r16_successor_carry_forward_transition import (
    r16_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "165666267c07bafff6a2c04077a82187e157968e"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r15-formal-classification-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r14-model-prewarm-transition.json"
)
EXPECTED_CAMPAIGN = {
    "id": "c1491-r14-f",
    "mode": "formal",
    "purpose": "production_replacement",
    "target_version": "0.149.1",
    "official_attempt_id": "20260827T105419Z-823221b63b08688e",
    "classification_sha256": (
        "97a6cf745c120db22d552b466c342c46d893ce0974521983e4a74ac1cf2654bd"
    ),
    "classification_package_sha256": (
        "caa5d79776bf1765352e24245db8274e6aa6aef408b6129392ea1cf32419e179"
    ),
    "profile_digest": (
        "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
    ),
    "rule_count": 42,
    "discovery_count": 2101,
    "blocked_count": 0,
}
EXPECTED_STAGE_PROFILE = {
    "receipt_sha256": (
        "cc9570a4d8a21bd7a43ca0a3dd870bbd8b9f7e93286d96faf1604c0e4df7ce9a"
    ),
    "inventory_sha256": (
        "9b8f867f0b5d97f595909bbecdfd2519754b74e5a716f09eaef4d0f97af6e798"
    ),
    "active_version": "0.147.0",
    "active_profile_digest": (
        "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc"
    ),
    "previous_version": "0.149.1",
    "previous_profile_digest": (
        "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
    ),
    "release_graph_sha256": (
        "cdab29c8c598356e9cb97958bb80695a9f7d3c61e9af37e4f13da84cb336d08e"
    ),
    "release_catalog_sha256": (
        "24722a44b2716739384c536ede3e92a7c27e3634c42afe2f25ae3e883fb7b5d7"
    ),
    "active_unchanged": True,
    "production_selector_changed": False,
}
EXPECTED_VERIFICATION = {
    "classification_replay_passed": True,
    "stage_inventory_replay_passed": True,
    "targeted_catalog_tests_passed": True,
    "historical_gate_failure_count": 52,
    "repository_wide_gates_pending": True,
}
EXPECTED_SAFETY = {
    "historical_receipts_modified": False,
    "historical_artifacts_overwritten": False,
    "network_configuration_changed": False,
    "production_selector_changed": False,
    "deployment_performed": False,
    "production_activated": False,
    "vircs_accessed": False,
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会覆盖冻结事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r15 Formal 分类 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取普通 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return value


def canonical_identity(document: dict[str, Any]) -> str:
    """复算排除自摘要字段后的规范身份。"""

    identity = copy.deepcopy(document)
    identity.pop("identity_sha256", None)
    raw = (
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return sha256(raw)


def load_transition() -> dict[str, Any]:
    """读取 r15 Formal 分类 transition。"""

    return load_document(TRANSITION_PATH, "r15 Formal 分类 transition")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 Formal Campaign、Catalog、安全边界和精确路径闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "campaign",
        "stage_profile",
        "verification",
        "safety",
        "path_set_sha256",
        "transitions",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r15 Formal 分类 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r15-formal-classification-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r15-formal-classification"
        or document["framework_stage"]
        != "VC-3/VC-4/FORMAL-CLASSIFICATION-STAGE"
        or document["result"]
        != "formal_classification_catalog_staged_transition_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r15 Formal 分类 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r15 Formal 分类 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r14 模型预热 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r15 Formal 分类 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r14-model-prewarm-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-r14-model-prewarm"
        or predecessor.get("result")
        != "r14_model_prewarm_verified_new_campaign_required"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r15 Formal 分类 transition 前序身份非法")

    if document["campaign"] != EXPECTED_CAMPAIGN:
        raise ValueError("r15 Formal 分类 transition Campaign 事实非法")
    if document["stage_profile"] != EXPECTED_STAGE_PROFILE:
        raise ValueError("r15 Formal 分类 transition stage-profile 事实非法")
    if document["verification"] != EXPECTED_VERIFICATION:
        raise ValueError("r15 Formal 分类 transition 验证事实非法")
    if document["safety"] != EXPECTED_SAFETY:
        raise ValueError("r15 Formal 分类 transition 安全边界非法")

    entries = document["transitions"]
    if not isinstance(entries, list) or len(entries) < 4:
        raise ValueError("r15 Formal 分类 transition 路径闭集为空")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if (
        len(paths) != len(entries)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
    ):
        raise ValueError("r15 Formal 分类 transition 路径未排序、重复或缺项")
    expected_path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if document["path_set_sha256"] != expected_path_set:
        raise ValueError("r15 Formal 分类 transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r15 Formal 分类 transition 条目字段非法")
        path = entry["path"]
        predecessors = entry["predecessor_sha256s"]
        current = ROOT / path
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(predecessors, list)
            or predecessors != sorted(predecessors)
            or len(predecessors) != len(set(predecessors))
            or not isinstance(entry["to_sha256"], str)
            or len(entry["to_sha256"]) != 64
            or any(
                not isinstance(predecessor_digest, str)
                or len(predecessor_digest) != 64
                or predecessor_digest == entry["to_sha256"]
                for predecessor_digest in predecessors
            )
            or (
                entry["change"] == "added"
                and predecessors
            )
            or (
                entry["change"] != "added"
                and (entry["change"] != "modified" or not predecessors)
            )
            or (
                path.startswith("docs/egress/maintenance/")
                and path != TRANSITION_PATH.relative_to(ROOT).as_posix()
            )
            or current.is_symlink()
            or not current.is_file()
            or (
                sha256(current.read_bytes()) != entry["to_sha256"]
                and not r16_supersedes(
                    path,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r15 Formal 分类 transition 条目非法：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r15 Formal 分类 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


@lru_cache(maxsize=None)
def r15_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r15 收据登记的精确路径、前序摘要和目标摘要。"""

    try:
        document = load_validated_transition()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if r16_supersedes(path, prior_digest, current_digest):
        return True
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and (
            entry["to_sha256"] == current_digest
            or r16_supersedes(path, entry["to_sha256"], current_digest)
        )
        for entry in document["transitions"]
    )


transition_supersedes = r15_supersedes


class Codex01491R15FormalClassificationTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝身份与安全边界篡改(self) -> None:
        document = load_transition()
        identity_mutation = copy.deepcopy(document)
        identity_mutation["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "身份非法"):
            validate_transition(identity_mutation)

        safety_mutation = copy.deepcopy(document)
        safety_mutation["safety"]["network_configuration_changed"] = True
        safety_mutation["identity_sha256"] = canonical_identity(safety_mutation)
        with self.assertRaisesRegex(ValueError, "安全边界非法"):
            validate_transition(safety_mutation)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_transition()
        entry = next(
            item
            for item in document["transitions"]
            if item["path"]
            == "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
        )
        self.assertTrue(
            r15_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            r15_supersedes(entry["path"], "0" * 64, entry["to_sha256"])
        )


if __name__ == "__main__":
    unittest.main()
