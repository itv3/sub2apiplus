"""冻结 Codex CLI 0.149.1 r23 Candidate 运行时坐标后继工具。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "f7c3521bc80e350ae529496e9f0656079cdad78c"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r23-runtime-coordinate-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r22-candidate-catalog-transition.json"
)
EXPECTED_PATHS = [
    (
        "backend/internal/officialegress/"
        "codex_01491_r22_candidate_catalog_transition_test.go"
    ),
    (
        "backend/internal/officialegress/"
        "codex_01491_r23_runtime_coordinate_transition_test.go"
    ),
    "tools/official_client_capture/codex_upgrade.py",
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r22_candidate_catalog_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r23_runtime_coordinate_transition.py"
    ),
    "tools/official_client_capture/tests/test_codex_upgrade.py",
]


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会遮蔽运行时过渡事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r23 运行时坐标 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取非符号链接 JSON 对象。"""

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


def base_blob(path: str) -> bytes | None:
    """读取 r23 基准提交中的普通 Git blob。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def expected_runtime_contract() -> dict[str, Any]:
    """返回 v4 后继收据允许改变的最小运行时坐标。"""

    return {
        "receipt_schema": "codex-upgrade-predecessor-import/v4",
        "reason": "candidate_runtime_identity_correction",
        "configuration_fields": [
            "codex_account_id",
            "live_attestation_compose_dir",
            "live_attestation_compose_files",
        ],
        "compose_coordinates_required_together": True,
        "compose_directory_must_be_canonical": True,
        "compose_files_must_exist_and_be_regular": True,
        "unlisted_configuration_changes_denied": True,
        "official_recapture_required": False,
        "classification_reapproval_required": False,
    }


def r24_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """延迟加载 r24，避免前序校验与分类事实后继形成导入环。"""

    try:
        from tools.official_client_capture.tests.test_codex_01491_r24_selector_lite_coordinate_transition import (
            r24_supersedes as successor,
        )
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return successor(path, prior_digest, current_digest)


def validate_transition(document: dict[str, Any]) -> None:
    """重放 r23 身份、前序、运行时合同、文件闭集和安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "runtime_contract",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r23 运行时坐标 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r23-runtime-coordinate-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r23-runtime-coordinate"
        or document["framework_stage"] != "VC-0/RUNTIME-COORDINATE-REBINDING"
        or document["result"] != "runtime_coordinate_successor_tooling_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r23 运行时坐标 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r23 运行时坐标 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r22 候选 Catalog transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r23 运行时坐标 transition 前序绑定非法")
    if document["runtime_contract"] != expected_runtime_contract():
        raise ValueError("r23 运行时坐标合同非法")
    if document["verification"] != {
        "legacy_v1_v2_v3_receipts_replayed": True,
        "v4_positive_path_replayed": True,
        "partial_coordinates_rejected": True,
        "reclassification_rebinding_rejected": True,
        "mutation_tests_required": True,
    }:
        raise ValueError("r23 运行时坐标验证事实非法")
    if document["safety"] != {
        "historical_campaigns_modified": False,
        "historical_compose_files_overwritten": False,
        "official_recapture_performed": False,
        "candidate_capture_performed": False,
        "deployment_performed": False,
        "network_configuration_changed": False,
        "production_selector_changed": False,
        "production_activated": False,
        "vircs_accessed": False,
        "dmit_server_accessed": False,
    }:
        raise ValueError("r23 运行时坐标安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_PATHS):
        raise ValueError("r23 运行时坐标 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if paths != EXPECTED_PATHS or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("r23 运行时坐标 transition 路径未排序或重复")
    path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if document["path_set_sha256"] != path_set:
        raise ValueError("r23 运行时坐标 transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r23 运行时坐标 transition 条目字段非法")
        path = entry["path"]
        previous = base_blob(path)
        expected_predecessors = [] if previous is None else [sha256(previous)]
        expected_change = "added" if previous is None else "modified"
        current = ROOT / path
        current_digest = sha256(current.read_bytes()) if current.is_file() else ""
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != current_digest
                and not r24_supersedes(
                    path,
                    entry["to_sha256"],
                    current_digest,
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r23 运行时坐标 transition 条目非法：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r23 transition。"""

    document = load_document(TRANSITION_PATH, "r23 运行时坐标 transition")
    validate_transition(document)
    return document


def r23_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r23 收据登记的精确 path/from/to 三元组。"""

    if r24_supersedes(path, prior_digest, current_digest):
        return True
    try:
        document = load_validated_transition()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and (
            entry["to_sha256"] == current_digest
            or r24_supersedes(path, entry["to_sha256"], current_digest)
        )
        for entry in document["transitions"]
    )


class Codex01491R23RuntimeCoordinateTransitionTest(unittest.TestCase):
    def test_transition_身份合同和文件闭集可独立重放(self) -> None:
        validate_transition(load_document(TRANSITION_PATH, "r23 transition"))

    def test_transition_拒绝扩大配置字段或伪造历史只读(self) -> None:
        document = load_document(TRANSITION_PATH, "r23 transition")
        contract_mutation = copy.deepcopy(document)
        contract_mutation["runtime_contract"]["configuration_fields"].append(
            "runtime_image"
        )
        contract_mutation["identity_sha256"] = canonical_identity(contract_mutation)
        with self.assertRaisesRegex(ValueError, "合同非法"):
            validate_transition(contract_mutation)

        safety_mutation = copy.deepcopy(document)
        safety_mutation["safety"]["historical_compose_files_overwritten"] = True
        safety_mutation["identity_sha256"] = canonical_identity(safety_mutation)
        with self.assertRaisesRegex(ValueError, "安全边界非法"):
            validate_transition(safety_mutation)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_validated_transition()
        entry = next(row for row in document["transitions"] if row["change"] == "modified")
        self.assertTrue(
            r23_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(r23_supersedes(entry["path"], "0" * 64, entry["to_sha256"]))


if __name__ == "__main__":
    unittest.main()
