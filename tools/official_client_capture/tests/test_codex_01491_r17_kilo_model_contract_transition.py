"""冻结 Codex CLI 0.149.1 r17 Kilo 模型合同后继 transition。"""

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
BASE_COMMIT = "c09a88fc4fe82b54995f79fc9e60c5abc0042d9f"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r17-kilo-model-contract-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r16-successor-carry-forward-transition.json"
)
EXPECTED_PATHS = {
    "backend/internal/officialegress/codex_01491_r16_successor_carry_forward_transition_test.go",
    "backend/internal/officialegress/codex_01491_r17_kilo_model_contract_transition_test.go",
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/tests/test_codex_01491_r16_successor_carry_forward_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_r17_kilo_model_contract_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
}
EXPECTED_CONTRACT = {
    "reason": "candidate_runtime_identity_correction",
    "official_recapture_required": False,
    "classification_reapproval_required": False,
    "candidate_recapture_required": True,
    "kilo_revalidation_required": True,
    "kilo_model_source": "lite_model",
    "historical_fallback_model_source": "model",
    "replay_on": ["accept", "compare", "status"],
    "replayed_facts": [
        "approved_classification_five_files",
        "official_evidence_inventory",
        "official_security_gate",
        "official_stage_seal",
        "predecessor_campaign_manifest",
    ],
}
EXPECTED_SAFETY = {
    "historical_artifacts_overwritten": False,
    "historical_receipts_modified": False,
    "network_configuration_changed": False,
    "deployment_performed": False,
    "production_selector_changed": False,
    "production_activated": False,
    "vircs_accessed": False,
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会覆盖冻结事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r17 Kilo 模型 transition 包含重复字段：{key}")
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


def load_transition() -> dict[str, Any]:
    """读取 r17 Kilo 模型合同 transition。"""

    return load_document(TRANSITION_PATH, "r17 Kilo 模型 transition")


def base_blob(path: str) -> bytes | None:
    """读取 r16 最终提交中的普通 Git blob；不存在时返回 None。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放前序、模型合同、安全边界和精确文件闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "carry_forward_contract",
        "safety",
        "path_set_sha256",
        "transitions",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r17 Kilo 模型 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r17-kilo-model-contract-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r17-kilo-model-contract"
        or document["framework_stage"]
        != "VC-3/VC-4/SAME-VERSION-SUCCESSOR"
        or document["result"] != "kilo_model_contract_tooling_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r17 Kilo 模型 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r17 Kilo 模型 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r16 后继承接 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r17 Kilo 模型 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r16-successor-carry-forward-transition/v1"
        or predecessor.get("scope")
        != "codex-0.149.1-r16-successor-carry-forward"
        or predecessor.get("result")
        != "successor_carry_forward_tooling_frozen"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r17 Kilo 模型 transition 前序身份非法")
    if document["carry_forward_contract"] != EXPECTED_CONTRACT:
        raise ValueError("r17 Kilo 模型承接合同非法")
    if document["safety"] != EXPECTED_SAFETY:
        raise ValueError("r17 Kilo 模型安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list):
        raise ValueError("r17 Kilo 模型 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if (
        paths != sorted(EXPECTED_PATHS)
        or len(paths) != len(entries)
        or len(paths) != len(set(paths))
        or any(path.startswith(FORBIDDEN_PREFIXES) for path in paths)
    ):
        raise ValueError("r17 Kilo 模型 transition 路径未排序、重复或越界")
    expected_path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document.get("path_set_sha256") != expected_path_set:
        raise ValueError("r17 Kilo 模型 transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r17 Kilo 模型 transition 条目字段非法")
        path = entry["path"]
        current = ROOT / path
        previous_blob = base_blob(path)
        previous_sha256s = [] if previous_blob is None else [sha256(previous_blob)]
        expected_change = "added" if previous_blob is None else "modified"
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != previous_sha256s
            or current.is_symlink()
            or not current.is_file()
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r17 Kilo 模型 transition 条目非法：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r17 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


@lru_cache(maxsize=None)
def r17_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r17 收据登记的精确 path/from/to 三元组。"""

    try:
        document = load_validated_transition()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and entry["to_sha256"] == current_digest
        for entry in document["transitions"]
    )


transition_supersedes = r17_supersedes


class Codex01491R17KiloModelContractTransitionTest(unittest.TestCase):
    def test_transition_身份合同与文件闭集可独立重放(self) -> None:
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
            if item["path"] == "tools/official_client_capture/codex_upgrade.py"
        )
        self.assertTrue(
            r17_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            r17_supersedes(entry["path"], "0" * 64, entry["to_sha256"])
        )


if __name__ == "__main__":
    unittest.main()
