"""冻结 Codex CLI 0.149.1 r24 选择器与 Lite 坐标纠正后继。"""

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

from tools.official_client_capture.tests import (
    test_codex_01491_service_successor_replay_transition as service_successor_replay,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "8d8252c519663a7165a0258ee1e97c4159751282"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r24-selector-lite-coordinate-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r23-runtime-coordinate-transition.json"
)
EXPECTED_PATHS = [
    (
        "backend/internal/officialegress/"
        "codex_01491_r23_runtime_coordinate_transition_test.go"
    ),
    (
        "backend/internal/officialegress/"
        "codex_01491_r24_selector_lite_coordinate_transition_test.go"
    ),
    "tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/tests/test_candidate_rule_assertion.py",
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r23_runtime_coordinate_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r24_selector_lite_coordinate_transition.py"
    ),
    "tools/official_client_capture/tests/test_codex_upgrade.py",
]


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会遮蔽后继事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r24 选择器与 Lite 坐标 transition 包含重复字段：{key}")
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
    """读取 r24 基准提交中的普通 Git blob。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def expected_correction_contract() -> dict[str, Any]:
    """返回只读承接官方阶段并重新批准分类的最小合同。"""

    return {
        "reason": "classification_fact_correction",
        "predecessor_import_schema": "codex-upgrade-predecessor-import/v3",
        "import_mode": "official_only_reclassification",
        "official_recapture_required": False,
        "official_evidence_replay_required": True,
        "approved_classification_imported": False,
        "classification_reapproval_required": True,
        "approved_scenario_rebind_required": True,
        "candidate_recapture_required": True,
        "kilo_revalidation_required": True,
    }


def expected_corrected_coordinates() -> dict[str, Any]:
    """返回本阶段允许纠正的两个精确坐标。"""

    return {
        "auxiliary_job_id": "candidate-frozen-aux",
        "auxiliary_track": "lite",
        "auxiliary_model_id": "{lite_model}",
        "auxiliary_expected_use_responses_lite": True,
        "auxiliary_required_model_receipt": False,
        "session_header_rule_id": "SPEC-HDR-007",
        "session_header_check_ids": [
            "responses-session-id",
            "responses-thread-id",
        ],
        "session_header_allowed_paths": [
            "/backend-api/codex/responses",
            "/backend-api/codex/responses/compact",
        ],
        "auxiliary_posts_excluded_from_session_header_scope": True,
    }


def validate_corrected_semantics(coordinates: dict[str, Any]) -> None:
    """验证场景坐标和会话头端点闭集已真实写入受管清单。"""

    scenario = load_document(
        ROOT / "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
        "0.149.1 场景清单",
    )
    jobs = [
        job
        for job in scenario.get("capture_jobs", [])
        if job.get("id") == coordinates["auxiliary_job_id"]
    ]
    if len(jobs) != 1:
        raise ValueError("r24 辅助任务 Lite 坐标数量非法")
    job = jobs[0]
    if (
        job.get("track") != coordinates["auxiliary_track"]
        or job.get("model_id") != coordinates["auxiliary_model_id"]
        or job.get("expected_use_responses_lite") is not True
        or job.get("required_model_receipt") is not False
    ):
        raise ValueError("r24 辅助任务 Lite 坐标未落入场景清单")

    profile = load_document(
        ROOT
        / "tools/official_client_capture/"
        "candidate_rule_expectations_0_149_1.json",
        "0.149.1 候选规则期望",
    )
    rules = [
        rule
        for rule in profile.get("rules", [])
        if rule.get("rule_id") == coordinates["session_header_rule_id"]
    ]
    if len(rules) != 1:
        raise ValueError("r24 会话头规则数量非法")
    checks = {
        check.get("id"): check
        for check in rules[0].get("checks", [])
        if check.get("id") in coordinates["session_header_check_ids"]
    }
    if set(checks) != set(coordinates["session_header_check_ids"]):
        raise ValueError("r24 会话头选择器检查数量非法")
    for check in checks.values():
        path_conditions = [
            condition
            for condition in check.get("select", {}).get("where", [])
            if condition.get("path") == "data.path"
        ]
        if path_conditions != [
            {
                "operator": "in",
                "path": "data.path",
                "value": coordinates["session_header_allowed_paths"],
            }
        ]:
            raise ValueError("r24 会话头选择器路径闭集非法")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 r24 身份、前序、合同、文件闭集和安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "correction_contract",
        "corrected_coordinates",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r24 选择器与 Lite 坐标 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r24-selector-lite-coordinate-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r24-selector-lite-coordinate"
        or document["framework_stage"] != "VC-3/VC-4/SAME-VERSION-SUCCESSOR"
        or document["result"]
        != "selector_lite_coordinate_successor_tooling_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r24 选择器与 Lite 坐标 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r24 选择器与 Lite 坐标 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r23 运行时坐标 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r24 选择器与 Lite 坐标 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r23-runtime-coordinate-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-r23-runtime-coordinate"
        or predecessor.get("result")
        != "runtime_coordinate_successor_tooling_frozen"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r24 选择器与 Lite 坐标 transition 前序身份非法")
    if document["correction_contract"] != expected_correction_contract():
        raise ValueError("r24 选择器与 Lite 坐标纠正合同非法")
    coordinates = document["corrected_coordinates"]
    if coordinates != expected_corrected_coordinates():
        raise ValueError("r24 选择器与 Lite 坐标纠正事实非法")
    if document["verification"] != {
        "scenario_coordinate_tested": True,
        "selector_scope_tested": True,
        "analytics_exclusion_tested": True,
        "mutation_tests_required": True,
    }:
        raise ValueError("r24 选择器与 Lite 坐标验证事实非法")
    if document["safety"] != {
        "historical_artifacts_overwritten": False,
        "historical_receipts_modified": False,
        "official_recapture_performed": False,
        "candidate_capture_performed": False,
        "deployment_performed": False,
        "network_configuration_changed": False,
        "production_selector_changed": False,
        "production_activated": False,
        "vircs_accessed": False,
        "dmit_server_accessed": False,
    }:
        raise ValueError("r24 选择器与 Lite 坐标安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_PATHS):
        raise ValueError("r24 选择器与 Lite 坐标 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if paths != EXPECTED_PATHS or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("r24 选择器与 Lite 坐标 transition 路径未排序或重复")
    path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if document["path_set_sha256"] != path_set:
        raise ValueError("r24 选择器与 Lite 坐标 transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r24 选择器与 Lite 坐标 transition 条目字段非法")
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
                and not service_successor_replay.transition_supersedes(
                    path,
                    entry["to_sha256"],
                    current_digest,
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
            or path.startswith("docs/egress/maintenance/")
        ):
            raise ValueError(f"r24 选择器与 Lite 坐标 transition 条目非法：{path}")
    validate_corrected_semantics(coordinates)


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r24 transition。"""

    document = load_document(TRANSITION_PATH, "r24 选择器与 Lite 坐标 transition")
    validate_transition(document)
    return document


def r24_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r24 收据登记的精确 path/from/to 三元组。"""

    if service_successor_replay.transition_supersedes(
        path,
        prior_digest,
        current_digest,
    ):
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
            or service_successor_replay.transition_supersedes(
                path,
                entry["to_sha256"],
                current_digest,
            )
        )
        for entry in document["transitions"]
    )


class Codex01491R24SelectorLiteCoordinateTransitionTest(unittest.TestCase):
    def test_transition_身份合同语义和文件闭集可独立重放(self) -> None:
        validate_transition(load_document(TRANSITION_PATH, "r24 transition"))

    def test_transition_拒绝重复取证或扩大选择器范围(self) -> None:
        document = load_document(TRANSITION_PATH, "r24 transition")
        contract_mutation = copy.deepcopy(document)
        contract_mutation["correction_contract"]["official_recapture_required"] = True
        contract_mutation["identity_sha256"] = canonical_identity(contract_mutation)
        with self.assertRaisesRegex(ValueError, "合同非法"):
            validate_transition(contract_mutation)

        coordinate_mutation = copy.deepcopy(document)
        coordinate_mutation["corrected_coordinates"][
            "session_header_allowed_paths"
        ].append("/backend-api/wham/settings/user")
        coordinate_mutation["identity_sha256"] = canonical_identity(
            coordinate_mutation
        )
        with self.assertRaisesRegex(ValueError, "纠正事实非法"):
            validate_transition(coordinate_mutation)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_validated_transition()
        entry = next(row for row in document["transitions"] if row["change"] == "modified")
        self.assertTrue(
            r24_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(r24_supersedes(entry["path"], "0" * 64, entry["to_sha256"]))


if __name__ == "__main__":
    unittest.main()
