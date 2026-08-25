"""冻结 Codex CLI 0.149.1 DOC-PRE 工具后继 transition。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "3d6082f44f289ec80e0e29eb2643cda78113eef0"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-doc-pre-tooling-transition.json"
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


def commit_blob(path: str) -> bytes | None:
    """读取基线提交中的文件；不存在时返回 None。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    missing = subprocess.run(
        ["git", "cat-file", "-e", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if missing.returncode != 0:
        return None
    raise ValueError(f"无法读取基线文件：{path}")


class Codex01491DocPreTransitionTest(unittest.TestCase):
    def test_transition_identity_and_file_closure_are_frozen(self) -> None:
        document = load_transition()
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
            self.assertEqual(entry["to_sha256"], sha256(current.read_bytes()), path)
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
            self.assertEqual(binding["sha256"], sha256(path.read_bytes()))
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
            {"0.145.0"},
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


if __name__ == "__main__":
    unittest.main()
