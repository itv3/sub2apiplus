"""冻结 Codex CLI 0.149.1 r11a 候选脚手架修复后继 transition。"""

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
BASE_COMMIT = "3421eabbaea96dcdba25808281fc9804c87af70e"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11a-harness-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r9-contamination-recovery-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r11a_harness_transition.py"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "tools/official_client_capture/h1_wire_probe.py",
        "tools/official_client_capture/run_h1_wire_probe.sh",
        "tools/official_client_capture/run_images_wire_probe.sh",
        "tools/official_client_capture/tests/test_account_gate_restoration.py",
        "tools/official_client_capture/tests/"
        "test_codex_01491_r9_contamination_recovery_transition.py",
        SELF_PATH,
        "tools/official_client_capture/tests/test_main_track_models.py",
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
    """拒绝会覆盖脚手架修复事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r11a 脚手架 transition 包含重复字段：{key}")
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
    """读取 r11a 基线提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """读取 r11a 候选脚手架修复 transition。"""

    return load_document(TRANSITION_PATH, "r11a 脚手架 transition")


def validate_transition(document: dict[str, Any]) -> None:
    """重放脚手架根因、网络硬边界、预检事实和文件摘要闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "failed_candidate_attempt",
        "boundaries",
        "repair_facts",
        "preflight_evidence",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r11a 脚手架 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r11a-harness-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r11a-harness-repair"
        or document["framework_stage"] != "VC-0/P0-R11A-HARNESS-REPAIR"
        or document["result"] != "r11a_campaign_required_with_repaired_harness"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r11a 脚手架 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r11a 脚手架 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r9 污染恢复 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": "44bcc4c258c28b58c807de49fb23db5e038f981564ae69bee26c8bc29b36c5a1",
        "identity_sha256": "1b2916c441a9bbb405db2c190f8df944d9acc978a54683a04fbb9ff8391592f8",
    }:
        raise ValueError("r11a 脚手架 transition 前序绑定非法")
    if predecessor.get("identity_sha256") != document["predecessor_transition"][
        "identity_sha256"
    ]:
        raise ValueError("r11a 脚手架 transition 前序自摘要非法")

    if document["failed_candidate_attempt"] != {
        "campaign_id": "codex-01491-r10a",
        "candidate_id": "codex-01491-r10a-93d179469-arm64-21",
        "attempt_id": "20260826T204300Z-98b0048acd77b1a4",
        "status": "failed",
        "reuse_forbidden": True,
    }:
        raise ValueError("r11a 脚手架 transition 失败 Attempt 事实非法")
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
        raise ValueError("r11a 脚手架 transition 账号或网络边界非法")
    if document["repair_facts"] != {
        "published_port_accepts_non_loopback_binding": True,
        "shared_probe_address_resolved_from_service_container": True,
        "hosts_written_immediately_after_restart": True,
        "api_key_group_single_account_gate": True,
        "api_key_group_single_key_gate": True,
        "image_permission_temporarily_enabled_and_restored": True,
        "image_probe_filters_startup_models": True,
        "account_gate_restored": True,
        "keeper_hosts_ca_and_model_mapping_restored": True,
        "new_campaign_required": True,
    }:
        raise ValueError("r11a 脚手架 transition 修复事实非法")
    if document["preflight_evidence"] != {
        "h1_run_id": "codex-01491-r10b-preflight-h1-final",
        "h1_request_count": 3,
        "h1_wire_sha256": "8466f16f736ea077d34ae22e8232d04e685551f824175ab8bbb5500c7f59a1da",
        "images_run_id": "codex-01491-r10b-preflight-images-final2",
        "images_request_count": 1,
        "images_wire_sha256": "45243c9d53b41a8cb6b9cd77d2ce2095efe1a8423c1ba056afa5fb24aaadcf28",
        "captured_codex_version": "0.149.1",
        "pre_and_post_egress_verified": True,
        "post_run_restoration_verified": True,
    }:
        raise ValueError("r11a 脚手架 transition 预检证据非法")
    if document["verification"] != {
        "bash_syntax_passed": True,
        "targeted_python_tests_passed": True,
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "arm64_h1_preflight_passed": True,
        "arm64_images_preflight_passed": True,
        "network_gate_passed": True,
    }:
        raise ValueError("r11a 脚手架 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r11a 脚手架 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r11a 脚手架 transition 条目字段非法")
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
            raise ValueError(f"r11a 脚手架 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"r11a 脚手架 transition 命中历史只读路径：{path}")


def load_validated_transition() -> dict[str, Any]:
    """加载并完整重放 r11a 候选脚手架修复 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


@lru_cache(maxsize=None)
def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r11a transition 登记的精确摘要边。"""

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


class Codex01491R11AHarnessTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝账号与网络边界篡改(self) -> None:
        document = load_transition()
        document["boundaries"]["capture_account_ref"] = "#20"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["boundaries"]["docker_network_changed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

    def test_transition_承认_h1_产出侧精确摘要边(self) -> None:
        document = load_transition()
        entry = next(
            item
            for item in document["transitions"]
            if item["path"] == "tools/official_client_capture/h1_wire_probe.py"
        )
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
