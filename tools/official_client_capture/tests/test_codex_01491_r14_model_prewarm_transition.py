"""冻结 Codex CLI 0.149.1 r14 在线模型目录预热后继 transition。"""

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
BASE_COMMIT = "e697ef456ddc9cd54bac79f4126af938ab445be5"
SOURCE_COMMIT = "861fa731f84e0779f606f8f666aaea6a05caef48"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r14-model-prewarm-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r13-candidate-coordinate-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r14_model_prewarm_transition.py"
)
SOURCE_PATHS = {
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/drive_codex_model_catalog.py",
    "tools/official_client_capture/run_official_relay_scenario.sh",
    "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
}
ADAPTER_PATHS = {
    "backend/internal/officialegress/"
    "codex_01491_r12e_preconnect_transition_test.go",
    "backend/internal/officialegress/"
    "codex_01491_r13_candidate_coordinate_transition_test.go",
    "backend/internal/officialegress/"
    "codex_01491_r14_model_prewarm_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r12e_preconnect_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r13_candidate_coordinate_transition_test.go",
    "backend/internal/service/"
    "codex_01491_r14_model_prewarm_transition_test.go",
    "tools/official_client_capture/tests/"
    "test_codex_01491_r12e_preconnect_transition.py",
    "tools/official_client_capture/tests/"
    "test_codex_01491_r13_candidate_coordinate_transition.py",
    SELF_PATH,
}
EXPECTED_PATHS = sorted(SOURCE_PATHS | ADAPTER_PATHS)
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

EXPECTED_FORMAL_FAILURE = {
    "campaign_directory": "/root/oauth-capture/campaigns/c1491-r13-f",
    "attempt_id": "20260827T081742Z-3239096bbd87dada",
    "official_job_count": 28,
    "completed_job_count": 27,
    "failed_job_ids": ["official-core"],
    "primary_capture_complete": True,
    "supplement_attempt_count": 3,
    "missing_artifact": "mitm/codex-http/s1/models-http.jsonl",
    "failed_attempt_preserved_read_only": True,
    "reuse_forbidden": True,
}

EXPECTED_DIAGNOSIS = {
    "debug_models_sent_proxy_request": False,
    "custom_provider_preserved_codex_backend_identity": False,
    "standalone_model_list_triggered_online_refresh": False,
    "app_server_startup_worker_triggers_online_refresh": True,
    "thread_start_required_for_model_refresh": False,
    "thread_start_schedules_unrelated_responses_websocket_prewarm": True,
    "dmit_tls_slow_path_exposed_five_second_client_budget": True,
    "account_or_quota_root_cause": False,
    "network_configuration_root_cause": False,
    "complete_official_recapture_required": False,
    "new_tool_identity_requires_new_campaign": True,
}

EXPECTED_REPAIR_FACTS = {
    "source_commits": [
        "85478e0e99f2456e1da896264cc8706b24001bf1",
        SOURCE_COMMIT,
    ],
    "source_paths": sorted(SOURCE_PATHS),
    "builtin_openai_provider_preserved": True,
    "app_server_initialize_sent": True,
    "thread_start_sent": False,
    "turn_start_sent": False,
    "responses_websocket_prewarm_sent": False,
    "startup_models_refresh_worker_kept_alive": True,
    "plugins_and_apps_disabled": True,
    "home_equals_codex_home": True,
    "per_attempt_timeout_seconds": 20,
    "maximum_attempt_count": 3,
    "formal_attempts_use_independent_temporary_mitm_roots": True,
    "only_validated_models_http_promoted": True,
    "atomic_no_overwrite_install": True,
    "temporary_misc_and_lifecycle_not_promoted": True,
    "request_or_response_bytes_modified": False,
}

EXPECTED_VERIFICATION = {
    "python_compile_passed": True,
    "bash_syntax_passed": True,
    "targeted_model_catalog_test_count": 14,
    "targeted_upgrade_test_count": 78,
    "targeted_python_tests_passed": True,
    "arm64_container_model_catalog_test_count": 14,
    "arm64_tool_hashes_matched": True,
    "arm64_final_attempt_count": 2,
    "arm64_successful_attempt_duration_seconds": 2,
    "arm64_model_count": 9,
    "arm64_protocol_record_count": 1,
    "arm64_responses_http_record_count": 0,
    "arm64_websocket_record_count": 0,
    "arm64_models_http_sha256": (
        "65951e19e96a41c27504061991b1c9edbc1bcf73a0b6314afb807b704bbc8a2e"
    ),
    "arm64_public_egress": "179.255.100.158",
    "capture_tool_tests_passed": True,
    "egress_spec_passed": True,
    "officialegress_go_tests_passed": True,
    "service_go_tests_passed": True,
    "historical_artifacts_unchanged": True,
    "new_campaign_required": True,
}

EXPECTED_SAFETY = {
    "active_remained_0_147_0": True,
    "previous_staged_0_149_1": True,
    "catalog_promoted": False,
    "production_activated": False,
    "sub2api_deployment_performed": False,
    "capture_tool_sync_performed": True,
    "live_models_requests_sent": True,
    "live_responses_or_turn_requests_sent": False,
    "historical_content_addressed_artifacts_overwritten": False,
    "historical_receipts_modified": False,
    "historical_transitions_modified": False,
    "network_configuration_changed": False,
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
    """拒绝可覆盖诊断、安全或在线验证事实的重复字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r14 模型预热 transition 包含重复字段：{key}")
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
    """读取 r13 后继提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """读取 r14 在线模型目录预热 transition。"""

    return load_document(TRANSITION_PATH, "r14 模型预热 transition")


def transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r14 收据登记的精确追加边。"""

    return any(
        entry.get("path") == path
        and entry.get("to_sha256") == current_digest
        and prior_digest in entry.get("predecessor_sha256s", [])
        for entry in document.get("transitions", [])
        if isinstance(entry, dict)
    )


def validate_transition(document: dict[str, Any]) -> None:
    """重放失败事实、根因、ARM64 验证、安全边界与文件闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "formal_failure",
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
        raise ValueError("r14 模型预热 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r14-model-prewarm-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r14-model-prewarm"
        or document["framework_stage"] != "VC-4/P0-R14-MODEL-PREWARM"
        or document["result"]
        != "r14_model_prewarm_verified_new_campaign_required"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r14 模型预热 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r14 模型预热 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r13 候选坐标 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r14 模型预热 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-r13-candidate-coordinate-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-r13-candidate-coordinate"
        or predecessor.get("result")
        != "r13_candidate_coordinate_repair_verified_new_campaign_required"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("r14 模型预热 transition 前序身份非法")

    if document["boundaries"] != EXPECTED_BOUNDARIES:
        raise ValueError("r14 模型预热 transition 账号或网络边界非法")
    if document["formal_failure"] != EXPECTED_FORMAL_FAILURE:
        raise ValueError("r14 模型预热 transition Formal 失败事实非法")
    if document["diagnosis"] != EXPECTED_DIAGNOSIS:
        raise ValueError("r14 模型预热 transition 根因事实非法")
    if document["repair_facts"] != EXPECTED_REPAIR_FACTS:
        raise ValueError("r14 模型预热 transition 修复事实非法")
    if document["verification"] != EXPECTED_VERIFICATION:
        raise ValueError("r14 模型预热 transition 门禁未闭合")
    if document["safety"] != EXPECTED_SAFETY:
        raise ValueError("r14 模型预热 transition 安全边界非法")

    timing = document["timing"]
    if not isinstance(timing, list) or len(timing) < 6:
        raise ValueError("r14 模型预热 transition 用时记录不完整")
    for item in timing:
        if set(item) != {
            "step",
            "started_at_utc",
            "finished_at_utc",
            "duration_seconds",
            "result",
        }:
            raise ValueError("r14 模型预热 transition 用时条目字段非法")
        start = datetime.fromisoformat(item["started_at_utc"].replace("Z", "+00:00"))
        finish = datetime.fromisoformat(
            item["finished_at_utc"].replace("Z", "+00:00")
        )
        if finish < start or item["duration_seconds"] < 0:
            raise ValueError("r14 模型预热 transition 用时条目非法")

    expected_path_set = sha256(
        json.dumps(EXPECTED_PATHS, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document["path_set_sha256"] != expected_path_set:
        raise ValueError("r14 模型预热 transition 路径摘要非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r14 模型预热 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r14 模型预热 transition 条目字段非法")
        path_value = entry["path"]
        before = commit_blob(path_value)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        current = ROOT / path_value
        if (
            path_value.startswith(FORBIDDEN_PREFIXES)
            or entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r14 模型预热 transition 条目非法：{path_value}")

    changed = subprocess.run(
        ["git", "diff", "--name-only", BASE_COMMIT, SOURCE_COMMIT],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    if changed != sorted(SOURCE_PATHS):
        raise ValueError("r14 模型预热源码提交路径闭集非法")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r14 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


class Codex01491R14ModelPrewarmTransitionTest(unittest.TestCase):
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
            == "tools/official_client_capture/drive_codex_model_catalog.py"
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
