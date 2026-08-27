"""冻结 Codex CLI 0.149.1 r11b relay 完整响应等待后继 transition。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "7fa29213ef50e7d2efb81efe1aeeabcdcd749426"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11b-relay-completion-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11a-harness-repair-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r11b_relay_completion_transition.py"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "tools/official_client_capture/drive_codex_model_catalog.py",
        "tools/official_client_capture/tests/"
        "test_codex_01491_r11a_harness_transition.py",
        SELF_PATH,
        "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
    }
)
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


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
    """拒绝会覆盖 relay 修复事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r11b relay transition 包含重复字段：{key}")
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
    """读取 r11b 基线提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """读取 r11b relay 完整响应等待 transition。"""

    return load_document(TRANSITION_PATH, "r11b relay transition")


def validate_transition(document: dict[str, Any]) -> None:
    """重放失败响应、固定网络、离线依赖与文件摘要闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "failed_official_attempt",
        "boundaries",
        "failure_facts",
        "repair_facts",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r11b relay transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r11b-relay-completion-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r11b-relay-completion"
        or document["framework_stage"] != "VC-0/P0-R11B-RELAY-COMPLETION"
        or document["result"]
        != "r11b_campaign_required_with_repaired_relay_completion"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r11b relay transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r11b relay transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r11a 脚手架 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": "c348b081fdf4357a2dd87aab6e23b571d8578e62d710a9bdd85623c5cd2bda11",
        "identity_sha256": "1104fbc1742b2a8e0985e96e0cb2902bc5a9e28fed914546c74b37abe1917469",
    }:
        raise ValueError("r11b relay transition 前序绑定非法")
    if predecessor.get("identity_sha256") != document["predecessor_transition"][
        "identity_sha256"
    ]:
        raise ValueError("r11b relay transition 前序自摘要非法")

    if document["failed_official_attempt"] != {
        "campaign_id": "codex-01491-r11a",
        "attempt_id": "20260827T000102Z-a792726deea258ed",
        "status": "failed",
        "restoration_status": "restored",
        "reuse_forbidden": True,
    }:
        raise ValueError("r11b relay transition 失败 Attempt 事实非法")
    if document["boundaries"] != {
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
    }:
        raise ValueError("r11b relay transition 账号或网络边界非法")
    if document["failure_facts"] != {
        "relay_attempt_count": 3,
        "models_http_status": 200,
        "models_content_length": 360785,
        "captured_response_bytes_each_attempt": [1369, 1369, 1369],
        "app_server_closed_before_response_complete": True,
        "host_python_version": "3.12.3",
        "host_python_zstandard_missing": True,
        "offline_zstandard_runtime": (
            "/root/oauth-capture/runtime/"
            "codex-upgrade-pydeps-zstandard-0.22.0"
        ),
    }:
        raise ValueError("r11b relay transition 失败根因事实非法")
    if document["repair_facts"] != {
        "wait_for_complete_relay_response_inside_app_server_lifetime": True,
        "relay_poll_interval_seconds": 0.05,
        "incomplete_response_fails_closed": True,
        "system_package_installed": False,
        "network_configuration_changed": False,
        "new_campaign_required": True,
    }:
        raise ValueError("r11b relay transition 修复事实非法")
    if document["verification"] != {
        "failed_attempt_restoration_verified": True,
        "targeted_python_tests_passed": True,
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "network_gate_passed": True,
    }:
        raise ValueError("r11b relay transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r11b relay transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r11b relay transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        current = ROOT / path
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r11b relay transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"r11b relay transition 命中历史只读路径：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """加载并完整重放 r11b relay transition。"""

    document = load_transition()
    validate_transition(document)
    return document


def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r11b transition 登记的精确摘要边。"""

    try:
        document = load_validated_transition()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and current_digest == entry["to_sha256"]
        for entry in document["transitions"]
    )


class Codex01491R11BRelayCompletionTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝账号网络与不完整响应篡改(self) -> None:
        document = load_transition()
        document["boundaries"]["capture_account_ref"] = "#20"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["boundaries"]["route_nat_iptables_changed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["repair_facts"]["incomplete_response_fails_closed"] = False
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "修复事实"):
            validate_transition(document)

    def test_transition_只承认精确后继边(self) -> None:
        entry = load_transition()["transitions"][0]
        self.assertTrue(
            transition_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            transition_supersedes(
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )


if __name__ == "__main__":
    unittest.main()
