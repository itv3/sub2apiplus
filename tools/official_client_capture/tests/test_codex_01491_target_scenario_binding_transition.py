"""冻结 Codex CLI 0.149.1 target 场景绑定修复 transition。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_candidate_gate_successor_transition import (
    transition_chain_supersedes as candidate_gate_transition_chain_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "0566654268ba060f3549869169ef21e9d7828bc7"
SOURCE_COMMIT = "b8de982d727b9a82f5429916c63ac663c5dd81f9"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-target-scenario-binding-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-egress-gate-chain-repair-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_target_scenario_binding_transition.py"
)
SOURCE_PATHS = {
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "tools/official_client_capture/codex_upgrade.py",
    "tools/official_client_capture/codex_upgrade_campaign.schema.json",
    "tools/official_client_capture/tests/test_codex_upgrade.py",
}
ADAPTER_PATHS = {
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_egress_gate_chain_repair_transition.py",
    SELF_PATH,
}
EXPECTED_PATHS = sorted(SOURCE_PATHS | ADAPTER_PATHS)
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
            raise ValueError(f"transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path) -> dict[str, Any]:
    """严格读取普通 JSON 文件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"transition 不是普通文件：{path}")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("transition 顶层必须是对象")
    return value


def load_transition() -> dict[str, Any]:
    """读取 target 场景绑定修复 transition。"""

    return load_document(TRANSITION_PATH)


def identity_sha256(document: dict[str, Any]) -> str:
    """按 Go json.Marshal 等价形式复算 transition 自摘要。"""

    identity = dict(document)
    identity.pop("identity_sha256", None)
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    return sha256(canonical)


def transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认本后继登记的精确 path/from/to 三元组。"""

    return any(
        entry["path"] == path
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document["transitions"]
    ) or candidate_gate_transition_chain_supersedes(
        path,
        prior_digest,
        current_digest,
    )


def commit_blob(commit: str, path: str) -> bytes | None:
    """读取提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放前序、源码提交、文件闭集、失败 Attempt 与安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "failed_attempt",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("target 场景绑定 transition 顶层字段闭集非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-target-scenario-binding-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-target-scenario-binding-repair"
        or document["framework_stage"]
        != "VC-0/P0-TARGET-SCENARIO-BINDING-REPAIR"
        or document["result"]
        != "target_scenario_binding_repair_ready_for_new_campaign"
    ):
        raise ValueError("target 场景绑定 transition 顶层事实非法")
    datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    if identity_sha256(document) != document["identity_sha256"]:
        raise ValueError("target 场景绑定 transition 自摘要不一致")

    predecessor = load_document(PREDECESSOR_PATH)
    if identity_sha256(predecessor) != predecessor.get("identity_sha256"):
        raise ValueError("出站门禁链前序 transition 自摘要不一致")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor["identity_sha256"],
    }:
        raise ValueError("target 场景绑定 transition 前序绑定非法")

    if document["failed_attempt"] != {
        "campaign_id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T062203Z-056665426"
        ),
        "attempt_id": "20260826T062314Z-490f44ec6127782b",
        "attempt_digest": (
            "96bd3faaa1f645fe02dac32f46e0f46b6a1dba94d3f8aa7d31ce79fc522ed1bf"
        ),
        "scenario_version": "0.147.0",
        "target_version": "0.149.1",
        "jobs_total": 28,
        "jobs_complete": 26,
        "jobs_failed": 2,
        "failed_job_ids": [
            "official-relay-realtime-webrtc",
            "official-wham-safe",
        ],
        "status": "failed_frozen_read_only",
    }:
        raise ValueError("target 场景绑定 transition 失败 Attempt 事实非法")
    if document["verification"] != {
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "formal_transition_replayed": True,
        "independent_replay_passed": True,
        "mutation_tests_passed": True,
        "transition_closure_replayed": True,
    }:
        raise ValueError("target 场景绑定 transition 验证事实未闭合")
    if document["safety"] != {
        "active_previous_changed": False,
        "arm64_read_only_diagnostics_performed": True,
        "arm64_tooling_synchronized": False,
        "catalog_promoted": False,
        "deployment_performed": False,
        "formal_campaign_created": False,
        "historical_receipts_modified": False,
        "live_request_sent": False,
        "production_selector_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("target 场景绑定 transition 安全边界非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("target 场景绑定 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("target 场景绑定 transition 条目字段闭集非法")
        path = entry["path"]
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError("target 场景绑定 transition 命中历史只读路径")
        before = commit_blob(BASE_COMMIT, path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        target = (
            commit_blob(SOURCE_COMMIT, path)
            if path in SOURCE_PATHS
            else (ROOT / path).read_bytes()
        )
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or target is None
            or (
                entry["to_sha256"] != sha256(target)
                and not transition_supersedes(
                    document,
                    path,
                    entry["to_sha256"],
                    sha256(target),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"target 场景绑定 transition 条目非法：{path}")

    changed = subprocess.run(
        ["git", "diff", "--name-only", BASE_COMMIT, SOURCE_COMMIT],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    if changed != sorted(SOURCE_PATHS):
        raise ValueError("target 场景绑定源码提交路径闭集非法")


class Codex01491TargetScenarioBindingTransitionTest(unittest.TestCase):
    def test_transition_身份与源码提交可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝身份与安全边界篡改(self) -> None:
        document = load_transition()
        mutated_identity = copy.deepcopy(document)
        mutated_identity["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要不一致"):
            validate_transition(mutated_identity)

        mutated_safety = copy.deepcopy(document)
        mutated_safety["safety"]["vircs_accessed"] = True
        mutated_safety["identity_sha256"] = identity_sha256(mutated_safety)
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
