"""冻结 Codex CLI 0.149.1 direct 就绪竞态修复后继 transition。"""

from __future__ import annotations

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
BASE_COMMIT = "ecd794c2fa13881db7be0ac8c0d728a2d8ab9490"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-direct-readiness-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-capture-runtime-repair-transition.json"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/runtime_scripts/start_direct.sh",
    "tools/official_client_capture/tests/test_capture_runtime_scripts.py",
    "tools/official_client_capture/tests/test_codex_01491_capture_runtime_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_direct_readiness_transition.py",
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


def commit_blob(path: str) -> bytes | None:
    """读取 direct 修复基线提交中的文件；新增文件返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
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
    """完整重放 direct 就绪修复的前序、文件闭集与只读边界。"""

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
        raise ValueError("direct 就绪 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-direct-readiness-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-direct-readiness-repair"
        or document["framework_stage"] != "VC-0/P0-RUNTIME-REPAIR-SUCCESSOR"
        or document["result"]
        != "direct_readiness_repair_ready_for_runtime_rebuild"
    ):
        raise ValueError("direct 就绪 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("direct 就绪 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("direct 就绪 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "抓包运行时修复 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("direct 就绪 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-capture-runtime-repair-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-capture-runtime-repair"
        or predecessor.get("result") != "capture_runtime_repair_ready_for_p0"
    ):
        raise ValueError("direct 就绪 transition 前序身份非法")

    if document["boundaries"] != {
        "direct_ready_requires_tcpdump_listening": True,
        "failed_offline_validation_preserved_read_only": True,
        "historical_transitions_preserved_read_only": True,
        "production_selector_changed": False,
        "runtime_rebuild_required": True,
        "vircs_accessed": False,
    }:
        raise ValueError("direct 就绪 transition 能力边界非法")
    if document["execution_facts"] != {
        "arm64_capture_image_rebuilt": False,
        "failed_validation_run_id": (
            "codex-0_149_1-runtime-offline-20260826T013200Z-ecd794c2f"
        ),
        "failed_validation_runtime_restored": True,
        "live_official_requests_sent": False,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("direct 就绪 transition 执行事实非法")
    if set(document["verification"]) != {
        "bash_syntax_passed",
        "failed_run_cleanup_verified",
        "predecessor_transition_replayed",
        "targeted_tests_passed",
    } or not all(document["verification"].values()):
        raise ValueError("direct 就绪 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("direct 就绪 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("direct 就绪 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        current = ROOT / path
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or not current.is_file()
            or current.is_symlink()
            or (
                entry["to_sha256"] != sha256(current.read_bytes())
                and not candidate_gate_transition_chain_supersedes(
                    path,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"direct 就绪 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"direct 就绪 transition 命中历史只读路径：{path}")


class Codex01491DirectReadinessTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "direct 就绪 transition"))

    def test_transition_拒绝身份与执行事实篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "direct 就绪 transition")
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "direct 就绪 transition")
        document["execution_facts"]["live_official_requests_sent"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "执行事实"):
            validate_transition(document)


if __name__ == "__main__":
    unittest.main()
