"""冻结 Codex CLI 0.149.1 r21 分类事实纠正 transition。"""

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
BASE_COMMIT = "3825f879b39e5f9aeb3e175d59cfc781b3f25ecf"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r21-classification-fact-correction-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r20-candidate-aux-transition.json"
)
EXPECTED_PATHS = {
    "backend/internal/officialegress/codex_01491_r20_candidate_aux_transition_test.go",
    "backend/internal/officialegress/codex_01491_r21_classification_fact_correction_transition_test.go",
    "docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json",
    "tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/codex_upgrade_evidence_labels_0_149_1.json",
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/run_candidate_core_capture.sh",
    "tools/official_client_capture/tests/test_build_evidence_catalog.py",
    "tools/official_client_capture/tests/test_candidate_core_capture.py",
    "tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_r20_candidate_aux_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_r21_classification_fact_correction_transition.py",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
    "tools/spec_ref_anchors_0_149_1.json",
}
EXPECTED_CONTRACT = {
    "reason": "classification_fact_correction",
    "predecessor_import_schema": "codex-upgrade-predecessor-import/v3",
    "import_mode": "official_only_reclassification",
    "official_recapture_required": False,
    "official_evidence_replay_required": True,
    "approved_classification_imported": False,
    "classification_reapproval_required": True,
    "historical_scenario_source_binding_scope": "successor_plan_rebuild_only",
    "approved_scenario_rebind_required": True,
    "candidate_recapture_required": True,
    "kilo_revalidation_required": True,
}
EXPECTED_FACT = {
    "rule_id": "SPEC-H1-004",
    "transport": "responses_http",
    "official_evidence": (
        "c1491-r14-f-lite-http-response/relay/"
        "conn005.client_to_upstream.bin"
    ),
    "cold_start_cookie_present": False,
    "lite_header_slot": 60,
    "routing_hint_slot": 65,
    "required_order": [
        "x-codex-turn-metadata",
        "x-openai-internal-codex-responses-lite",
        "x-codex-routing-hint",
        "x-client-request-id",
    ],
}
EXPECTED_PROFILE_TRANSITION = {
    "target_version": "0.149.1",
    "predecessor_profile_digest": (
        "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
    ),
    "successor_profile_id": "codex-0.149.1-official-r1491-v2",
    "successor_profile_digest": (
        "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3"
    ),
    "historical_profile_overwritten": False,
    "catalog_activation_performed": False,
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
    "docs/egress/maintenance/",
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
            raise ValueError(f"r21 分类事实 transition 包含重复字段：{key}")
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
    """读取 r21 分类事实纠正 transition。"""

    return load_document(TRANSITION_PATH, "r21 分类事实纠正 transition")


def base_blob(path: str) -> bytes | None:
    """读取 r20 最终提交中的普通 Git blob；不存在时返回 None。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放前序、事实纠正合同、安全边界和精确文件闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "classification_fact_correction_contract",
        "corrected_fact",
        "profile_transition",
        "safety",
        "path_set_sha256",
        "transitions",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r21 分类事实纠正 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r21-classification-fact-correction-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"]
        != "codex-0.149.1-r21-classification-fact-correction"
        or document["framework_stage"]
        != "VC-3/VC-4/SAME-VERSION-SUCCESSOR"
        or document["result"]
        != "classification_fact_correction_tooling_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r21 分类事实纠正 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r21 分类事实纠正 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r20 Candidate aux transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r21 分类事实纠正 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r20-candidate-aux-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-r20-candidate-aux"
        or predecessor.get("result") != "candidate_aux_capture_tooling_frozen"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r21 分类事实纠正 transition 前序身份非法")
    if document["classification_fact_correction_contract"] != EXPECTED_CONTRACT:
        raise ValueError("r21 分类事实纠正承接合同非法")
    if document["corrected_fact"] != EXPECTED_FACT:
        raise ValueError("r21 SPEC-H1-004 纠正事实非法")
    if document["profile_transition"] != EXPECTED_PROFILE_TRANSITION:
        raise ValueError("r21 画像追加式过渡非法")
    if document["safety"] != EXPECTED_SAFETY:
        raise ValueError("r21 分类事实纠正安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list):
        raise ValueError("r21 分类事实纠正 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if (
        paths != sorted(EXPECTED_PATHS)
        or len(paths) != len(entries)
        or len(paths) != len(set(paths))
        or any(path.startswith(FORBIDDEN_PREFIXES) for path in paths)
    ):
        raise ValueError("r21 分类事实纠正 transition 路径未排序、重复或越界")
    expected_path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document.get("path_set_sha256") != expected_path_set:
        raise ValueError("r21 分类事实纠正 transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r21 分类事实纠正 transition 条目字段非法")
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
            raise ValueError(f"r21 分类事实纠正 transition 条目非法：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r21 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


@lru_cache(maxsize=None)
def r21_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r21 收据登记的精确 path/from/to 三元组。"""

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


transition_supersedes = r21_supersedes


class Codex01491R21ClassificationFactCorrectionTransitionTest(unittest.TestCase):
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
            r21_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            r21_supersedes(entry["path"], "0" * 64, entry["to_sha256"])
        )

    def test_transition_禁止重复官方取证和继承旧批准(self) -> None:
        document = load_transition()
        contract = document["classification_fact_correction_contract"]
        self.assertFalse(contract["official_recapture_required"])
        self.assertFalse(contract["approved_classification_imported"])
        self.assertTrue(contract["classification_reapproval_required"])


if __name__ == "__main__":
    unittest.main()
