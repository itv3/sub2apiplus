"""冻结 Codex CLI 0.149.1 沙箱 HOME 修复后继 transition。"""

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


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "cb846068a9b3f2504f7e6bdb8506af7a260a3753"
TRANSITION_PATH = (
    ROOT / "docs/egress/maintenance/codex-0.149.1-shell-home-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-direct-readiness-repair-transition.json"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/capturelib/scenarios.py",
    "tools/official_client_capture/tests/test_codex_01491_shell_home_transition.py",
    "tools/official_client_capture/tests/test_commands.py",
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def commit_blob(path: str) -> bytes | None:
    """读取沙箱 HOME 修复基线提交中的文件；新增文件返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放沙箱 HOME 修复的身份、闭集和服务器隔离边界。"""

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
        raise ValueError("沙箱 HOME transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-shell-home-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-shell-home-repair"
        or document["framework_stage"] != "VC-0/P0-RUNTIME-REPAIR-SUCCESSOR"
        or document["result"] != "shell_home_repair_ready_for_p0"
    ):
        raise ValueError("沙箱 HOME transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("沙箱 HOME transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("沙箱 HOME transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "direct 就绪 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("沙箱 HOME transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-direct-readiness-repair-transition/v1"
        or predecessor.get("result")
        != "direct_readiness_repair_ready_for_runtime_rebuild"
    ):
        raise ValueError("沙箱 HOME transition 前序身份非法")

    if document["boundaries"] != {
        "failed_campaign_preserved_read_only": True,
        "oauth_codex_home_unchanged": True,
        "production_selector_changed": False,
        "shell_home_outside_root": True,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("沙箱 HOME transition 能力边界非法")
    if document["execution_facts"] != {
        "campaign_id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T014757Z-cb846068a"
        ),
        "failed_attempt_id": "20260826T015035Z-7a1a062177c07b42",
        "live_official_requests_sent": True,
        "restoration_report_passed": True,
        "runtime_image_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("沙箱 HOME transition 执行事实非法")
    if set(document["verification"]) != {
        "command_toml_parse_passed",
        "predecessor_transition_replayed",
        "targeted_tests_passed",
    } or not all(document["verification"].values()):
        raise ValueError("沙箱 HOME transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("沙箱 HOME transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("沙箱 HOME transition 条目字段非法")
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
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"沙箱 HOME transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"沙箱 HOME transition 命中历史只读路径：{path}")


class Codex01491ShellHomeTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "沙箱 HOME transition"))

    def test_transition_拒绝身份与服务器事实篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "沙箱 HOME transition")
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "沙箱 HOME transition")
        document["execution_facts"]["restoration_report_passed"] = False
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "执行事实"):
            validate_transition(document)


if __name__ == "__main__":
    unittest.main()
