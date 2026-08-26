"""冻结 Codex CLI 0.149.1 抓包运行时修复后继 transition。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "20158443533827c0d809c269267135ce3b7d9678"
TARGET_COMMIT = "ecd794c2fa13881db7be0ac8c0d728a2d8ab9490"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-capture-runtime-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-failed-evidence-recovery-transition.json"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/capturelib/identity.py",
    "tools/official_client_capture/model_condition_receipts.py",
    "tools/official_client_capture/run_official_codex_compact_capture.sh",
    "tools/official_client_capture/run_official_relay_scenario.sh",
    "tools/official_client_capture/run_sub2api_direct_matrix.sh",
    "tools/official_client_capture/run_sub2api_openai_mitm_matrix.sh",
    "tools/official_client_capture/runtime_image/Dockerfile",
    "tools/official_client_capture/runtime_image/README.md",
    "tools/official_client_capture/runtime_image/capture-entrypoint",
    "tools/official_client_capture/runtime_scripts/start_direct.sh",
    "tools/official_client_capture/runtime_scripts/start_mitm.sh",
    "tools/official_client_capture/runtime_scripts/stop_direct.sh",
    "tools/official_client_capture/runtime_scripts/stop_mitm.sh",
    "tools/official_client_capture/tests/test_capture_runtime_scripts.py",
    "tools/official_client_capture/tests/test_codex_01491_capture_runtime_transition.py",
    "tools/official_client_capture/tests/test_model_condition_receipt.py",
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
    """严格拒绝可覆盖 transition 事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> dict[str, Any]:
    """读取非符号链接、无重复键的 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是可信普通文件")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return value


def commit_blob(commit: str, path: str) -> bytes | None:
    """读取指定提交中的文件；该提交不存在此文件时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def canonical_identity(document: dict[str, Any]) -> str:
    """复算 transition 排除自摘要字段后的规范身份。"""

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


def validate_transition(document: dict[str, Any]) -> None:
    """完整重放抓包运行时修复的身份、闭集与安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "execution_facts",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("抓包运行时 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-capture-runtime-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-capture-runtime-repair"
        or document["framework_stage"] != "VC-0/P0-RUNTIME-REPAIR"
        or document["result"] != "capture_runtime_repair_ready_for_p0"
    ):
        raise ValueError("抓包运行时 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("抓包运行时 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("抓包运行时 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "失败证据恢复 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("抓包运行时 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-failed-evidence-recovery-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-failed-evidence-recovery"
        or predecessor.get("result") != "failed_evidence_recovery_complete"
    ):
        raise ValueError("抓包运行时 transition 前序身份非法")

    if document["boundaries"] != {
        "empty_connections_fail_closed_unless_manifest_zero_byte": True,
        "failed_campaign_preserved_read_only": True,
        "model_receipts_use_frozen_runtime": True,
        "official_binary_exec_root_outside_root": True,
        "sidecar_lifecycle_state_bound": True,
        "zstd_dependency_frozen": True,
    }:
        raise ValueError("抓包运行时 transition 能力事实非法")
    if document["execution_facts"] != {
        "active_previous_changed": False,
        "catalog_promoted": False,
        "capture_image_deployed": False,
        "historical_artifacts_modified": False,
        "production_selector_changed": False,
        "server_diagnostics_performed_on_arm64": True,
        "vircs_accessed": False,
    }:
        raise ValueError("抓包运行时 transition 执行边界非法")
    if set(document["verification"]) != {
        "bash_syntax_passed",
        "capture_tool_tests_passed",
        "egress_spec_passed",
        "targeted_tests_passed",
        "transition_chain_replayed",
    } or not all(document["verification"].values()):
        raise ValueError("抓包运行时 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("抓包运行时 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("抓包运行时 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(BASE_COMMIT, path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        target = commit_blob(TARGET_COMMIT, path)
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or target is None
            or entry["to_sha256"] != sha256(target)
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"抓包运行时 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"抓包运行时 transition 命中历史只读路径：{path}")


class Codex01491CaptureRuntimeTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "抓包运行时 transition"))

    def test_transition_拒绝身份与安全事实篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "抓包运行时 transition")
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "抓包运行时 transition")
        document["execution_facts"]["vircs_accessed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "执行边界"):
            validate_transition(document)

    def test_candidate_catalog_保持_0147_active_并暂存_01491_previous(self) -> None:
        catalog_path = (
            ROOT
            / "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
        )
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        graph_path = (
            ROOT
            / "backend/internal/officialegress"
            / catalog["release_graph"]["path"]
        )
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        active = {
            node["build"]["version"]
            for node in graph["nodes"]
            if node["mode"] == "active"
        }
        previous = {
            node["build"]["version"]
            for node in graph["nodes"]
            if node["mode"] == "previous"
        }
        self.assertEqual(active, {"0.147.0"})
        self.assertEqual(previous, {"0.149.1"})


if __name__ == "__main__":
    unittest.main()
