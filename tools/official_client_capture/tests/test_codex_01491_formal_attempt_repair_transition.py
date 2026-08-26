"""冻结 Codex CLI 0.149.1 首次 Formal Attempt 修复后继 transition。"""

from __future__ import annotations

import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_direct_readiness_transition import (
    canonical_identity,
    load_json,
    sha256,
)
from tools.official_client_capture.tests.test_codex_01491_egress_gate_chain_repair_transition import (
    load_transition as load_egress_gate_chain_repair_transition,
    transition_supersedes as egress_gate_chain_repair_transition_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "4080277cef28515fc737cc7f3dcde072efd50936"
TARGET_COMMIT = "032366cc8a696480c61f079b7fa2fed992810a7a"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-formal-attempt-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-model-catalog-binding-repair-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_formal_attempt_repair_transition.py"
)
PYTHON_CHAIN_ADAPTER = (
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py"
)
GO_CHAIN_ADAPTER = (
    "backend/internal/officialegress/"
    "codex_01491_p0_transition_chain_repair_test.go"
)
REPAIR_PATHS = {
    "tools/official_client_capture/build_scenario_facts.py",
    "tools/official_client_capture/codex_upgrade_capture_attempt.schema.json",
    "tools/official_client_capture/codex_upgrade_scenario_receipt.schema.json",
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/run_official_relay_scenario.sh",
    "tools/official_client_capture/scenario_receipts.py",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
    "tools/official_client_capture/tests/test_compaction_scenario_models.py",
    "tools/official_client_capture/tests/test_scenario_receipt.py",
    "tools/official_client_capture/tests/test_upstream_byte_relay.py",
    "tools/official_client_capture/upstream_byte_relay.py",
}
TRANSITION_TOOLING_PATHS = {GO_CHAIN_ADAPTER, PYTHON_CHAIN_ADAPTER, SELF_PATH}
EXPECTED_PATHS = REPAIR_PATHS | TRANSITION_TOOLING_PATHS
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def commit_blob(commit: str, file_name: str) -> bytes | None:
    """读取指定提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{file_name}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """严格读取本次后继 transition。"""

    return load_json(TRANSITION_PATH, "Formal Attempt 修复 transition")


def transition_supersedes(
    document: dict[str, Any],
    file_name: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 transition 中登记的精确 path/from/to 三元组。"""

    return any(
        entry["path"] == file_name
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document.get("transitions", [])
    )


def validate_transition(document: dict[str, Any]) -> None:
    """重放修复提交、失败 Attempt、前序链和禁止触碰的生产边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "target_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "failed_attempt",
        "repairs",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("Formal Attempt 修复 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-formal-attempt-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["target_commit"] != TARGET_COMMIT
        or document["scope"] != "codex-0.149.1-formal-attempt-repair"
        or document["framework_stage"]
        != "VC-0/P0-FORMAL-ATTEMPT-REPAIR-SUCCESSOR"
        or document["result"]
        != "formal_attempt_repairs_frozen_pending_new_campaign"
    ):
        raise ValueError("Formal Attempt 修复 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("Formal Attempt 修复 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("Formal Attempt 修复 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "模型目录绑定 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("Formal Attempt 修复 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-model-catalog-binding-repair-transition/v1"
        or predecessor.get("scope")
        != "codex-0.149.1-model-catalog-binding-repair"
        or predecessor.get("result")
        != "prewarm_catalog_binding_repair_ready_for_p0"
    ):
        raise ValueError("Formal Attempt 修复 transition 前序身份非法")

    if document["boundaries"] != {
        "arm64_read_only_diagnostics_performed": True,
        "failed_attempt_preserved_read_only": True,
        "historical_artifacts_modified": False,
        "new_campaign_required": True,
        "production_selector_changed": False,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("Formal Attempt 修复 transition 安全边界非法")
    if document["failed_attempt"] != {
        "attempt_digest": (
            "4b2b87ee7506629d318cbf275585e8b0a3b82473f42d4ba7120374586006eab4"
        ),
        "attempt_id": "20260826T040807Z-b1f8789e6e8bd937",
        "campaign_id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T040733Z-4080277ce"
        ),
        "failed_job_ids": [
            "official-relay-model-downshift",
            "official-relay-realtime-webrtc",
            "official-wham-safe",
        ],
        "jobs_complete": 25,
        "jobs_failed": 3,
        "jobs_total": 28,
        "restoration_report": {
            "path": (
                "/root/oauth-capture/campaigns/"
                "codex-0_149_1-formal-production-replacement-"
                "20260826T040733Z-4080277ce/official/attempts/"
                "20260826T040807Z-b1f8789e6e8bd937/evidence/receipts/"
                "restoration-report.json"
            ),
            "sha256": (
                "5c443e1d8d03571f3f193f7cf315fa925c8f3816e66d4f5c4c49d3e345e7a0f6"
            ),
            "status": "restored",
        },
        "status": "failed_frozen",
    }:
        raise ValueError("Formal Attempt 修复 transition 失败 Attempt 事实非法")
    if document["repairs"] != {
        "a11": {
            "controlled_final_state": (
                "sideband_branch_verified_with_live_regression"
            ),
            "controlled_intervention": (
                "synthesize_realtime_call_after_live_failure"
            ),
            "live_failure_message_sha256": (
                "6361dfb1ded718a34bf4c83d728df084d6f68be022e7a25ef34c3ff7fc35bb51"
            ),
            "live_failure_status": 400,
            "observation_modes": [
                "live_failure_plus_controlled_branch",
                "live_success",
            ],
            "post_switch_diagnostic_run": (
                "/root/oauth-capture/runs/"
                "codex-0_149_1-post-switch-a11-diag-20260826T051415Z"
            ),
        },
        "model_downshift": {
            "first_context_window": 272000,
            "first_token_limit": 16000,
            "same_comp_hash_required": True,
            "second_context_window": 128000,
            "second_token_limit": 8000,
        },
        "relay_timeout_seconds": 1800,
        "wham_safe": {
            "container_entrypoint": "python3",
            "network_mode": "none",
            "request_path": (
                "/backend-api/wham/rate-limit-reset-credits/consume"
            ),
        },
    }:
        raise ValueError("Formal Attempt 修复 transition 修复事实非法")
    if document["verification"] != {
        "bash_syntax_passed": True,
        "capture_tool_tests_passed": True,
        "global_egress_spec_ci_blocker": (
            "pre_existing_repository_wide_transition_digest_drift"
        ),
        "global_egress_spec_ci_blocker_recorded": True,
        "global_egress_spec_ci_passed": False,
        "repair_commit_replayed": True,
        "schema_json_parse_passed": True,
        "targeted_tests_passed": True,
        "transition_chain_replayed": True,
    }:
        raise ValueError("Formal Attempt 修复 transition 门禁事实非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("Formal Attempt 修复 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("Formal Attempt 修复 transition 条目字段非法")
        file_name = entry["path"]
        before = commit_blob(BASE_COMMIT, file_name)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        if file_name in TRANSITION_TOOLING_PATHS:
            current = ROOT / file_name
            target = (
                current.read_bytes()
                if current.is_file() and not current.is_symlink()
                else None
            )
        else:
            target = commit_blob(TARGET_COMMIT, file_name)
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or target is None
            or (
                entry["to_sha256"] != sha256(target)
                and not egress_gate_chain_repair_transition_supersedes(
                    load_egress_gate_chain_repair_transition(),
                    file_name,
                    entry["to_sha256"],
                    sha256(target),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"Formal Attempt 修复 transition 条目非法：{file_name}")
        if file_name.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(
                f"Formal Attempt 修复 transition 命中历史只读路径：{file_name}"
            )

    changed = subprocess.run(
        ["git", "diff", "--name-only", BASE_COMMIT, TARGET_COMMIT],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    if changed != sorted(REPAIR_PATHS):
        raise ValueError("Formal Attempt 修复提交路径闭集非法")


class Codex01491FormalAttemptRepairTransitionTest(unittest.TestCase):
    def test_transition_身份与修复提交可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝身份与生产边界篡改(self) -> None:
        document = load_transition()
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_transition()
        document["boundaries"]["vircs_accessed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "安全边界"):
            validate_transition(document)

    def test_attempt_schema_精确后继三元组被承认(self) -> None:
        document = load_transition()
        entry = next(
            item
            for item in document["transitions"]
            if item["path"].endswith("codex_upgrade_capture_attempt.schema.json")
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
