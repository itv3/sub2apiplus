"""冻结 Codex CLI 0.149.1 DOC-PRE 工具后继 transition。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_formal_attempt_repair_transition import (
    load_transition as load_formal_attempt_repair_transition,
    transition_supersedes as formal_attempt_repair_transition_supersedes,
)
from tools.official_client_capture.tests.test_codex_01491_egress_gate_chain_repair_transition import (
    load_transition as load_egress_gate_chain_repair_transition,
    transition_supersedes as egress_gate_chain_repair_transition_supersedes,
)
from tools.official_client_capture.tests.test_codex_01491_target_scenario_binding_transition import (
    load_transition as load_target_scenario_binding_transition,
)
from tools.official_client_capture.tests.test_codex_01491_candidate_gate_successor_transition import (
    transition_chain_supersedes as candidate_gate_transition_chain_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "3d6082f44f289ec80e0e29eb2643cda78113eef0"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-doc-pre-tooling-transition.json"
)
HARDENING_BASE_COMMIT = "f0ec0ea0cb235d4a6845558f11d74ed067919fd2"
P0_TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-p0-transition-chain-repair.json"
)
HARDENING_TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-campaign-boundary-hardening-transition.json"
)
RECOVERY_BASE_COMMIT = "d9d4db88fb9a8dfdcba21ab9612b2eb4b6a0d7a3"
RECOVERY_TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-failed-evidence-recovery-transition.json"
)
EXPECTED_PATHS = {
    ".gitignore",
    "Makefile",
    "docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "tools/check_spec_refs.py",
    "tools/official_client_capture/candidate_rule_expectation_overrides_0_149_1.json",
    "tools/official_client_capture/candidate_rule_expectations_0_149_1.json",
    "tools/official_client_capture/capturelib/model.py",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/codex_upgrade_campaign.schema.json",
    "tools/official_client_capture/codex_upgrade_rules_0_147_0.json",
    "tools/official_client_capture/codex_upgrade_rules_0_149_1.json",
    "tools/official_client_capture/codex_upgrade_scenarios_0_147_0.json",
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/extract_compaction_reason.py",
    "tools/official_client_capture/h1_wire_probe.py",
    "tools/official_client_capture/run_candidate_aux_capture.sh",
    "tools/official_client_capture/run_candidate_core_capture.sh",
    "tools/official_client_capture/scrub_raw_bytes.py",
    "tools/official_client_capture/tests/test_candidate_aux_capture.py",
    "tools/official_client_capture/tests/test_candidate_rule_assertion.py",
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
    "tools/official_client_capture/tests/test_main_track_models.py",
    "tools/official_client_capture/tests/test_scenario_receipt.py",
    "tools/official_client_capture/tests/test_upstream_byte_relay.py",
    "tools/spec_ref_anchors_0_149_1.json",
    "tools/spec_source_deps/README.md",
    "tools/spec_source_deps/h2-0.4.16/Cargo.toml",
    "tools/spec_source_deps/h2-0.4.16/LICENSE",
    "tools/spec_source_deps/h2-0.4.16/src/frame/headers.rs",
    "tools/spec_source_deps/h2-0.4.16/src/frame/settings.rs",
    "tools/spec_source_deps/h2-0.4.16/src/hpack/encoder.rs",
    "tools/spec_source_deps/manifest_0_149_1.json",
    "tools/update_spec_ref_anchors.py",
}
HARDENING_EXPECTED_PATHS = {
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
    "backend/internal/officialegress/upstream_merge_framework_transition_test.go",
    "backend/internal/officialegress/upstream_v0180_source_transition_test.go",
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/codex_upgrade_campaign.schema.json",
    "tools/official_client_capture/codex_upgrade_capture_attempt.schema.json",
    "tools/official_client_capture/codex_upgrade_capture_reservation.schema.json",
    "tools/official_client_capture/codex_upgrade_gate_receipt.py",
    "tools/official_client_capture/codex_upgrade_gate_receipt.schema.json",
    "tools/official_client_capture/codex_upgrade_seal_failure.schema.json",
    "tools/official_client_capture/codex_upgrade_seal_preview.schema.json",
    "tools/official_client_capture/codex_upgrade_stage_result.schema.json",
    "tools/official_client_capture/production_activation_receipt.py",
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
    "tools/official_client_capture/tests/test_codex_upgrade_capture_lifecycle.py",
    "tools/official_client_capture/tests/test_codex_upgrade_gate_receipt.py",
    "tools/official_client_capture/tests/test_production_activation_receipt.py",
}
RECOVERY_EXPECTED_PATHS = {
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
    "tools/official_client_capture/tests/test_job_retry_within_attempt.py",
}
FORBIDDEN_TRANSITION_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """严格拒绝会覆盖 transition 事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_transition() -> dict[str, Any]:
    """读取非符号链接、无重复键的 transition。"""

    if TRANSITION_PATH.is_symlink() or not TRANSITION_PATH.is_file():
        raise ValueError("DOC-PRE transition 必须是普通文件")
    value = json.loads(
        TRANSITION_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("DOC-PRE transition 顶层必须是对象")
    return value


def load_json_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取 transition 链上的普通 JSON 文件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return value


def commit_blob(path: str, commit: str = BASE_COMMIT) -> bytes | None:
    """读取基线提交中的文件；不存在时返回 None。"""

    completed = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    missing = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if missing.returncode != 0:
        return None
    raise ValueError(f"无法读取基线文件：{path}")


def load_hardening_transition() -> dict[str, Any]:
    """读取并完整重放 B1+B2 Campaign 边界后继 transition。"""

    document = load_json_document(HARDENING_TRANSITION_PATH, "Campaign 边界 transition")
    expected_keys = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }
    if set(document) != expected_keys:
        raise ValueError("Campaign 边界 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-campaign-boundary-hardening-transition/v1"
        or document["base_commit"] != HARDENING_BASE_COMMIT
        or document["scope"] != "codex-0.149.1-campaign-boundary-hardening"
        or document["framework_stage"] != "VC-0/P0-B1-B2"
        or document["result"] != "campaign_boundary_hardening_complete"
    ):
        raise ValueError("Campaign 边界 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("Campaign 边界 transition 时间非法") from error

    identity = dict(document)
    recorded_identity = identity.pop("identity_sha256")
    canonical = (
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if recorded_identity != sha256(canonical):
        raise ValueError("Campaign 边界 transition 自摘要不一致")

    predecessor = load_json_document(P0_TRANSITION_PATH, "P0 链修复 transition")
    expected_predecessor = {
        "path": P0_TRANSITION_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(P0_TRANSITION_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }
    if document["predecessor_transition"] != expected_predecessor:
        raise ValueError("Campaign 边界 transition 前序绑定非法")

    if document["boundaries"] != {
        "accepted_not_activated_enforced": True,
        "candidate_purpose_frozen": True,
        "formal_mode_required_for_live_stages": True,
        "preflight_only_plan_status_only": True,
    }:
        raise ValueError("Campaign 边界 transition 能力事实非法")
    if set(document["verification"]) != {
        "capture_tool_tests_passed",
        "egress_spec_passed",
        "schema_validation_passed",
        "targeted_tests_passed",
        "transition_chain_replayed",
    } or not all(document["verification"].values()):
        raise ValueError("Campaign 边界 transition 门禁未闭合")
    if set(document["safety"]) != {
        "active_previous_changed",
        "catalog_promoted",
        "deployment_performed",
        "formal_campaign_created",
        "historical_receipts_modified",
        "live_request_sent",
        "production_selector_changed",
        "server_accessed",
    } or any(document["safety"].values()):
        raise ValueError("Campaign 边界 transition 安全边界非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(HARDENING_EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("Campaign 边界 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("Campaign 边界 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path, HARDENING_BASE_COMMIT)
        current = ROOT / path
        if (
            before is None
            or entry["change"] != "modified"
            or entry["predecessor_sha256s"] != [sha256(before)]
            or not current.is_file()
            or current.is_symlink()
            or (
                entry["to_sha256"] != sha256(current.read_bytes())
                and not maintenance_transition_chain_supersedes(
                    document,
                    load_recovery_transition(),
                    path,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"Campaign 边界 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_TRANSITION_PREFIXES):
            raise ValueError(f"Campaign 边界 transition 命中历史只读路径：{path}")
    return document


def hardening_transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 B1+B2 receipt 中登记的精确三元组。"""

    return any(
        entry["path"] == path
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document["transitions"]
    )


def load_recovery_transition() -> dict[str, Any]:
    """读取并完整重放最终失败证据恢复后继 transition。"""

    document = load_json_document(RECOVERY_TRANSITION_PATH, "失败证据恢复 transition")
    expected_keys = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }
    if set(document) != expected_keys:
        raise ValueError("失败证据恢复 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-failed-evidence-recovery-transition/v1"
        or document["base_commit"] != RECOVERY_BASE_COMMIT
        or document["scope"] != "codex-0.149.1-failed-evidence-recovery"
        or document["framework_stage"] != "VC-0/P0-RECOVERY"
        or document["result"] != "failed_evidence_recovery_complete"
    ):
        raise ValueError("失败证据恢复 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("失败证据恢复 transition 时间非法") from error

    identity = dict(document)
    recorded_identity = identity.pop("identity_sha256")
    canonical = (
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if recorded_identity != sha256(canonical):
        raise ValueError("失败证据恢复 transition 自摘要不一致")

    predecessor = load_json_document(HARDENING_TRANSITION_PATH, "Campaign 边界 transition")
    expected_predecessor = {
        "path": HARDENING_TRANSITION_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(HARDENING_TRANSITION_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }
    if document["predecessor_transition"] != expected_predecessor:
        raise ValueError("失败证据恢复 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-campaign-boundary-hardening-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-campaign-boundary-hardening"
        or predecessor.get("result") != "campaign_boundary_hardening_complete"
    ):
        raise ValueError("失败证据恢复 transition 前序身份非法")

    if document["boundaries"] != {
        "failed_receipt_paths_rebased": True,
        "final_failure_evidence_archived": True,
        "fixed_evidence_root_released": True,
        "historical_failure_evidence_replayable": True,
    }:
        raise ValueError("失败证据恢复 transition 能力事实非法")
    if set(document["verification"]) != {
        "capture_tool_tests_passed",
        "egress_spec_passed",
        "targeted_tests_passed",
        "transition_chain_replayed",
    } or not all(document["verification"].values()):
        raise ValueError("失败证据恢复 transition 门禁未闭合")
    if set(document["safety"]) != {
        "active_previous_changed",
        "catalog_promoted",
        "deployment_performed",
        "formal_campaign_created",
        "historical_receipts_modified",
        "live_request_sent",
        "production_selector_changed",
        "server_accessed",
    } or any(document["safety"].values()):
        raise ValueError("失败证据恢复 transition 安全边界非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(RECOVERY_EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("失败证据恢复 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("失败证据恢复 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path, RECOVERY_BASE_COMMIT)
        current = ROOT / path
        if (
            before is None
            or entry["change"] != "modified"
            or entry["predecessor_sha256s"] != [sha256(before)]
            or not current.is_file()
            or current.is_symlink()
            or (
                entry["to_sha256"] != sha256(current.read_bytes())
                and not formal_attempt_repair_chain_supersedes(
                    path,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"失败证据恢复 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_TRANSITION_PREFIXES):
            raise ValueError(f"失败证据恢复 transition 命中历史只读路径：{path}")
    return document


def recovery_transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认恢复 transition 中登记的精确三元组。"""

    return any(
        entry["path"] == path
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document["transitions"]
    )


def formal_attempt_repair_chain_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """重放 Formal Attempt 修复及其出站门禁链后继。"""

    formal = load_formal_attempt_repair_transition()
    egress_gate = load_egress_gate_chain_repair_transition()
    if formal_attempt_repair_transition_supersedes(
        formal,
        path,
        prior_digest,
        current_digest,
    ) or egress_gate_chain_repair_transition_supersedes(
        egress_gate,
        path,
        prior_digest,
        current_digest,
    ):
        return True
    for entry in formal["transitions"]:
        if entry["path"] != path or prior_digest not in entry["predecessor_sha256s"]:
            continue
        if egress_gate_chain_repair_transition_supersedes(
            egress_gate,
            path,
            entry["to_sha256"],
            current_digest,
        ):
            return True
    return False


def maintenance_transition_chain_supersedes(
    hardening: dict[str, Any],
    recovery: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """重放 Campaign 边界、失败恢复与 Formal Attempt 修复的传递闭包。"""

    if candidate_gate_transition_chain_supersedes(
        path,
        prior_digest,
        current_digest,
    ):
        return True

    edges: dict[str, list[str]] = {}
    formal_attempt_repair = load_formal_attempt_repair_transition()
    egress_gate_chain_repair = load_egress_gate_chain_repair_transition()
    target_scenario_binding = load_target_scenario_binding_transition()
    for document in (
        hardening,
        recovery,
        formal_attempt_repair,
        egress_gate_chain_repair,
        target_scenario_binding,
    ):
        for entry in document["transitions"]:
            if entry["path"] != path:
                continue
            for predecessor in entry["predecessor_sha256s"]:
                edges.setdefault(predecessor, []).append(entry["to_sha256"])

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


class Codex01491DocPreTransitionTest(unittest.TestCase):
    def test_transition_identity_and_file_closure_are_frozen(self) -> None:
        document = load_transition()
        hardening = load_hardening_transition()
        recovery = load_recovery_transition()
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "issued_at_utc",
                "base_commit",
                "scope",
                "framework_stage",
                "baseline",
                "target",
                "private_archive",
                "historical_read_only_bindings",
                "transitions",
                "verification",
                "safety",
                "result",
                "identity_sha256",
            },
        )
        self.assertEqual(
            document["schema_version"],
            "official-client-codex-0.149.1-doc-pre-tooling-transition/v1",
        )
        self.assertEqual(document["base_commit"], BASE_COMMIT)
        self.assertEqual(document["scope"], "codex-0.149.1-doc-pre-tooling")
        self.assertEqual(document["framework_stage"], "VC-0/DOC-PRE")
        self.assertEqual(document["result"], "ready_for_clean_head_p0")

        identity = dict(document)
        recorded_identity = identity.pop("identity_sha256")
        canonical = (
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        self.assertEqual(recorded_identity, sha256(canonical))

        entries = document["transitions"]
        self.assertIsInstance(entries, list)
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(EXPECTED_PATHS))
        self.assertEqual(len(paths), len(set(paths)))
        for entry in entries:
            self.assertEqual(
                set(entry),
                {"path", "change", "from_sha256", "to_sha256", "reason"},
            )
            path = entry["path"]
            self.assertFalse(path.startswith(FORBIDDEN_TRANSITION_PREFIXES))
            before = commit_blob(path)
            expected_change = "added" if before is None else "modified"
            expected_before = None if before is None else sha256(before)
            self.assertEqual(entry["change"], expected_change, path)
            self.assertEqual(entry["from_sha256"], expected_before, path)
            current = ROOT / path
            self.assertTrue(current.is_file() and not current.is_symlink(), path)
            current_digest = sha256(current.read_bytes())
            self.assertTrue(
                entry["to_sha256"] == current_digest
                or maintenance_transition_chain_supersedes(
                    hardening,
                    recovery,
                    path,
                    entry["to_sha256"],
                    current_digest,
                ),
                path,
            )
            self.assertNotEqual(entry["from_sha256"], entry["to_sha256"], path)
            self.assertIsInstance(entry["reason"], str)
            self.assertTrue(entry["reason"].strip(), path)

    def test_active_previous_and_historical_receipts_remain_read_only(self) -> None:
        document = load_transition()
        self.assertEqual(
            document["baseline"],
            {
                "sub2api_version": "0.1.180",
                "production_active": "0.147.0",
                "production_previous": "0.145.0",
            },
        )
        bindings = document["historical_read_only_bindings"]
        paths = [binding["path"] for binding in bindings]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        transition_paths = {entry["path"] for entry in document["transitions"]}
        for binding in bindings:
            self.assertEqual(set(binding), {"path", "sha256", "role"})
            path = ROOT / binding["path"]
            self.assertTrue(path.is_file() and not path.is_symlink())
            current_digest = sha256(path.read_bytes())
            self.assertTrue(
                binding["sha256"] == current_digest
                or candidate_gate_transition_chain_supersedes(
                    binding["path"],
                    binding["sha256"],
                    current_digest,
                ),
                binding["path"],
            )
            self.assertNotIn(binding["path"], transition_paths)

        catalog_path = (
            ROOT / "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
        )
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        graph_path = ROOT / "backend/internal/officialegress" / catalog["release_graph"]["path"]
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {node["build"]["version"] for node in graph["nodes"] if node["mode"] == "active"},
            {"0.147.0"},
        )
        self.assertEqual(
            {node["build"]["version"] for node in graph["nodes"] if node["mode"] == "previous"},
            {"0.149.1"},
        )

    def test_archive_is_legacy_input_not_current_acceptance(self) -> None:
        document = load_transition()
        archive = document["private_archive"]
        self.assertEqual(archive["source_base_commit"], "cd13abbc8364a90ddeabddf86a68de7c6057dd2a")
        self.assertEqual(archive["source_backend_version"], "0.1.179-2")
        self.assertEqual(
            archive["reuse_policy"],
            "可作目标源码、官方抓包和旧部署事实的只读输入；不得充当 Sub2API 0.1.180 的 Campaign、AcceptanceFact、Catalog promotion 或 production activation 收据。",
        )
        self.assertEqual(
            document["target"],
            {
                "codex_version": "0.149.1",
                "formal_campaign_created": False,
                "campaign_purpose": "unassigned_until_formal_campaign",
            },
        )
        self.assertEqual(
            document["safety"],
            {
                "active_previous_changed": False,
                "catalog_promoted": False,
                "deployment_performed": False,
                "formal_campaign_created": False,
                "historical_artifacts_modified": False,
                "live_request_sent": False,
                "production_activation_receipt_created": False,
                "production_selector_changed": False,
                "server_required_for_this_transition": False,
            },
        )

    def test_campaign_boundary_hardening_transition_is_frozen(self) -> None:
        document = load_hardening_transition()
        entry = document["transitions"][0]
        self.assertTrue(
            hardening_transition_supersedes(
                document,
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            hardening_transition_supersedes(
                document,
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )

    def test_failed_evidence_recovery_transition_is_frozen(self) -> None:
        document = load_recovery_transition()
        entry = document["transitions"][0]
        self.assertTrue(
            recovery_transition_supersedes(
                document,
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            recovery_transition_supersedes(
                document,
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )


if __name__ == "__main__":
    unittest.main()
