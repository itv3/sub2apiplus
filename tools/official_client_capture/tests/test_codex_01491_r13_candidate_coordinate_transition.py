"""冻结 Codex CLI 0.149.1 r13 候选运行坐标恢复 transition。"""

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

from tools.official_client_capture.tests.test_codex_01491_r15_formal_classification_transition import (
    r15_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "66d2c5ce29d8911982c91b738ab623a83c8aea4e"
SOURCE_COMMIT = "244da853d7963fe4bb6a55c228bc94932303e8d8"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r13-candidate-coordinate-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r12e-preconnect-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r13_candidate_coordinate_transition.py"
)
SOURCE_PATHS = {
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/tests/test_codex_upgrade_capture_lifecycle.py",
}
ADAPTER_PATHS = {
    "backend/internal/officialegress/"
    "codex_01491_r12e_preconnect_transition_test.go",
    "backend/internal/officialegress/"
    "codex_01491_r4_catalog_successor_transition_test.go",
    "backend/internal/officialegress/"
    "codex_01491_r13_candidate_coordinate_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r12e_preconnect_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r4_catalog_successor_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r13_candidate_coordinate_transition_test.go",
    "tools/official_client_capture/tests/"
    "test_codex_01491_candidate_gate_successor_transition.py",
    "tools/official_client_capture/tests/"
    "test_codex_01491_r4_catalog_successor_transition.py",
    "tools/official_client_capture/tests/"
    "test_codex_01491_r12e_preconnect_transition.py",
    SELF_PATH,
}
EXPECTED_PATHS = sorted(SOURCE_PATHS | ADAPTER_PATHS)
ADDITIONAL_PREDECESSORS = {
    "backend/internal/officialegress/"
    "codex_01491_r4_catalog_successor_transition_test.go": {
        "1d2cec832db7fc457516a70863431f5207cf31dea6b9cd5e9dabb8f379ac2206"
    },
    "backend/internal/service/"
    "codex_01491_r4_catalog_successor_transition_test.go": {
        "ac51a0742fef4ac120705a91bdbf5267180ce4c6e913314e6311239d0825fd6e"
    },
    "tools/official_client_capture/tests/"
    "test_codex_01491_candidate_gate_successor_transition.py": {
        "96482eee11a088a5c7f41127cd2c7f331e78cf198661fc6ebe00e46eafa81e94"
    },
    "tools/official_client_capture/tests/"
    "test_codex_01491_r4_catalog_successor_transition.py": {
        "c6ddc9a5c89e191a0b6896da681f17696c4055fcc33a23305187b0bd3d3d0874"
    },
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)

EXPECTED_BOUNDARIES = {
    "api_key_ref": "#4",
    "capture_account_ref": "#21",
    "capture_account_20_allowed": False,
    "capture_account_20_used": False,
    "service_private_ip": "172.25.0.3",
    "service_default_gateway": "172.25.0.1",
    "capture_private_ip": "172.30.0.10",
    "capture_default_gateway": "172.30.0.1",
    "required_public_egress": "179.255.100.158",
    "docker_network_changed": False,
    "route_nat_iptables_changed": False,
    "proxy_or_compose_network_changed": False,
    "production_selector_changed": False,
    "historical_evidence_preserved_read_only": True,
    "vircs_accessed": False,
}

EXPECTED_FAILED_ATTEMPT = {
    "campaign_id": (
        "codex-0_149_1-formal-production-replacement-"
        "20260827T053634Z-66d2c5ce2"
    ),
    "candidate_id": "codex-01491-r12e-formal-66d2c5ce2-arm64-21",
    "attempt_id": "20260827T065244Z-1ee3494b9eb7714d",
    "started_at_utc": "2026-08-27T06:52:44.759605Z",
    "finished_at_utc": "2026-08-27T06:59:01.748798Z",
    "duration_seconds": 377,
    "status": "failed",
    "execution_error_type": "KeyboardInterrupt",
    "jobs": [
        {
            "id": "candidate-core-direct",
            "attempt_index": 3,
            "status": "failed",
            "duration_seconds": 22.37,
        },
        {
            "id": "candidate-core-mitm",
            "attempt_index": 1,
            "status": "failed",
            "duration_seconds": 11.744,
        },
        {
            "id": "candidate-h1-wire",
            "attempt_index": 1,
            "status": "complete",
            "duration_seconds": 26.491,
        },
        {
            "id": "candidate-ws-handshake-repeat",
            "attempt_index": 3,
            "status": "failed",
            "duration_seconds": 32.639,
        },
    ],
    "before_probe_sha256": (
        "48b697f20422c78cb914c818b9beef2e1af91ca4718d426ed89894844005d862"
    ),
    "after_probe_sha256": (
        "1fc927c7458f440775e1a6bbce7948c338ca2febee3893b69c329089ce75409d"
    ),
    "restoration_report_sha256": (
        "d38fbba256f74e4c95eedb002e11a4bac7f023fb68aaea539890659720537767"
    ),
    "failed_attempt_preserved_read_only": True,
}

EXPECTED_DIAGNOSIS = {
    "root_cause": "projected_runtime_id_exceeded_128_characters",
    "safe_id_max_characters": 128,
    "projected_invalid_lengths": [133, 135, 138, 143, 146, 148, 154],
    "direct_error": "RUN_ID／SUBJECT 格式非法。",
    "mitm_error": "MITM 运行坐标格式非法。",
    "affected_direct_mitm_requests_started": False,
    "h1_probe_completed": True,
    "old_candidate_failure_globally_blocked_new_candidate": True,
    "account_or_quota_root_cause": False,
    "dmit_egress_root_cause": False,
    "network_configuration_root_cause": False,
    "complete_official_recapture_required": False,
    "new_short_formal_campaign_required": True,
}

EXPECTED_REPAIR_FACTS = {
    "reservation_preflight_projects_final_runtime_ids": True,
    "run_id_checked": True,
    "run_id_prefix_setup_checked": True,
    "run_id_prefix_subjects_checked": True,
    "window_id_checked": True,
    "overflow_fails_before_reservation": True,
    "same_candidate_failure_requires_explicit_resume": True,
    "different_candidate_can_append_after_failure": True,
    "global_active_reservation_gate_preserved": True,
    "historical_attempt_mutation_allowed": False,
    "request_or_response_bytes_modified": False,
    "source_commit": SOURCE_COMMIT,
    "source_paths": sorted(SOURCE_PATHS),
}

EXPECTED_VERIFICATION = {
    "python_compile_passed": True,
    "targeted_python_test_count": 102,
    "targeted_python_tests_passed": True,
    "candidate_gate_chain_replayed": True,
    "officialegress_chain_replayed": True,
    "service_chain_replayed": True,
    "capture_tool_tests_passed": True,
    "egress_spec_passed": True,
    "historical_artifacts_unchanged": True,
    "new_campaign_preflight_required": True,
}

EXPECTED_SAFETY = {
    "active_remained_0_147_0": True,
    "previous_staged_0_149_1": True,
    "catalog_promoted": False,
    "production_activated": False,
    "deployment_performed": False,
    "live_candidate_request_sent": False,
    "historical_content_addressed_artifacts_overwritten": False,
    "historical_receipts_modified": False,
    "historical_transitions_modified": False,
    "network_configuration_changed": False,
    "arm64_evidence_read_only_accessed": True,
    "vircs_accessed": False,
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def canonical_identity(document: dict[str, Any]) -> str:
    """复算排除自摘要字段后的规范身份。"""

    identity = dict(document)
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


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝可覆盖诊断与安全事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r13 候选坐标 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取非符号链接的普通 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return document


@lru_cache(maxsize=None)
def commit_blob(path: str) -> bytes | None:
    """读取 r13 基线提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """读取 r13 候选运行坐标 transition。"""

    return load_document(TRANSITION_PATH, "r13 候选运行坐标 transition")


def transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """按登记路径图重放 r13 及其后继的精确摘要链。"""

    if r15_supersedes(path, prior_digest, current_digest):
        return True

    edges: dict[str, list[str]] = {}
    for entry in document.get("transitions", []):
        if not isinstance(entry, dict) or entry.get("path") != path:
            continue
        for predecessor in entry.get("predecessor_sha256s", []):
            edges.setdefault(predecessor, []).append(entry.get("to_sha256", ""))

    queue = [prior_digest]
    visited: set[str] = set()
    while queue:
        digest = queue.pop(0)
        if digest == current_digest:
            return True
        if digest in visited:
            continue
        visited.add(digest)
        queue.extend(edges.get(digest, []))
    return False


def validate_transition(document: dict[str, Any]) -> None:
    """重放失败 attempt、根因、修复提交、固定边界和路径闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "failed_attempt",
        "diagnosis",
        "repair_facts",
        "timing",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r13 候选坐标 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r13-candidate-coordinate-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r13-candidate-coordinate"
        or document["framework_stage"]
        != "VC-4/P0-R13-CANDIDATE-COORDINATE"
        or document["result"]
        != "r13_candidate_coordinate_repair_verified_new_campaign_required"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r13 候选坐标 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r13 候选坐标 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r12e 预连接 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r13 候选坐标 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r12e-preconnect-transition/v1"
        or predecessor.get("result")
        != "r12e_preconnect_repair_verified_new_campaign_required"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r13 候选坐标 transition 前序身份非法")

    if document["boundaries"] != EXPECTED_BOUNDARIES:
        raise ValueError("r13 候选坐标 transition 账号或网络边界非法")
    if document["failed_attempt"] != EXPECTED_FAILED_ATTEMPT:
        raise ValueError("r13 候选坐标 transition 失败 attempt 事实非法")
    if document["diagnosis"] != EXPECTED_DIAGNOSIS:
        raise ValueError("r13 候选坐标 transition 根因事实非法")
    if document["repair_facts"] != EXPECTED_REPAIR_FACTS:
        raise ValueError("r13 候选坐标 transition 修复事实非法")
    if document["verification"] != EXPECTED_VERIFICATION:
        raise ValueError("r13 候选坐标 transition 门禁未闭合")
    if document["safety"] != EXPECTED_SAFETY:
        raise ValueError("r13 候选坐标 transition 安全边界非法")

    timing = document["timing"]
    if not isinstance(timing, list) or len(timing) < 4:
        raise ValueError("r13 候选坐标 transition 用时记录不完整")
    for item in timing:
        if set(item) != {
            "step",
            "started_at_utc",
            "finished_at_utc",
            "duration_seconds",
            "result",
        }:
            raise ValueError("r13 候选坐标 transition 用时条目字段非法")
        start = datetime.fromisoformat(item["started_at_utc"].replace("Z", "+00:00"))
        finish = datetime.fromisoformat(
            item["finished_at_utc"].replace("Z", "+00:00")
        )
        if finish < start or item["duration_seconds"] < 0:
            raise ValueError("r13 候选坐标 transition 用时条目非法")

    expected_path_set = sha256(
        json.dumps(EXPECTED_PATHS, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document["path_set_sha256"] != expected_path_set:
        raise ValueError("r13 候选坐标 transition 路径摘要非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r13 候选坐标 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r13 候选坐标 transition 条目字段非法")
        path_value = entry["path"]
        before = commit_blob(path_value)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = (
            []
            if before is None
            else sorted(
                {
                    sha256(before),
                    *ADDITIONAL_PREDECESSORS.get(path_value, set()),
                }
            )
        )
        current = ROOT / path_value
        current_digest = (
            sha256(current.read_bytes())
            if current.is_file() and not current.is_symlink()
            else ""
        )
        if (
            path_value.startswith(FORBIDDEN_PREFIXES)
            or entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != current_digest
                and not r14_model_prewarm_supersedes(
                    path_value,
                    entry["to_sha256"],
                    current_digest,
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r13 候选坐标 transition 条目非法：{path_value}")

    changed = subprocess.run(
        ["git", "diff", "--name-only", BASE_COMMIT, SOURCE_COMMIT],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    if changed != sorted(SOURCE_PATHS):
        raise ValueError("r13 候选坐标源码提交路径闭集非法")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r13 与 r14 后继 transition。"""

    document = load_transition()
    validate_transition(document)
    from tools.official_client_capture.tests import (
        test_codex_01491_r14_model_prewarm_transition as r14_prewarm,
    )

    successor = r14_prewarm.load_validated_transition()
    return {
        **document,
        "transitions": [
            *document["transitions"],
            *successor["transitions"],
        ],
    }


def r14_model_prewarm_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """延迟加载 r14，避免维护链模块初始化形成循环依赖。"""

    from tools.official_client_capture.tests import (
        test_codex_01491_r14_model_prewarm_transition as r14_prewarm,
    )

    try:
        successor = r14_prewarm.load_validated_transition()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    return r14_prewarm.transition_supersedes(
        successor,
        path,
        prior_digest,
        current_digest,
    )


class Codex01491R13CandidateCoordinateTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝身份与安全边界篡改(self) -> None:
        document = load_transition()
        mutated_identity = copy.deepcopy(document)
        mutated_identity["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "身份非法"):
            validate_transition(mutated_identity)

        mutated_safety = copy.deepcopy(document)
        mutated_safety["safety"]["network_configuration_changed"] = True
        mutated_safety["identity_sha256"] = canonical_identity(mutated_safety)
        with self.assertRaisesRegex(ValueError, "安全边界非法"):
            validate_transition(mutated_safety)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_transition()
        entry = next(
            item
            for item in document["transitions"]
            if item["path"] == "tools/official_client_capture/codex_upgrade.py"
        )
        self.assertTrue(
            transition_supersedes(
                document,
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            transition_supersedes(
                document,
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )


if __name__ == "__main__":
    unittest.main()
