"""冻结 Codex CLI 0.149.1 r9 环境污染恢复后继 transition。"""

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

from tools.official_client_capture.tests.test_codex_01491_r15_formal_classification_transition import (
    r15_supersedes,
)

from tools.official_client_capture.tests.test_codex_01491_r11a_harness_transition import (
    load_validated_transition as load_r11a_harness_transition,
    transition_supersedes as r11a_harness_transition_supersedes,
)



ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "620c3c8d1f7e7dd6734cb961cdb2b2904799974e"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r9-contamination-recovery-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-model-catalog-h1-successor-transition.json"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/codex_01491_r4_catalog_successor_transition_test.go",
        "backend/internal/officialegress/codex_01491_r9_contamination_recovery_transition_test.go",
        "backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go",
        "backend/internal/service/codex_01491_r9_contamination_recovery_transition_test.go",
        "tools/official_client_capture/run_sub2api_direct_matrix.sh",
        "tools/official_client_capture/tests/test_codex_01491_candidate_gate_successor_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_r4_catalog_successor_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_r9_contamination_recovery_transition.py",
        "tools/official_client_capture/tests/test_direct_matrix_account_selection.py",
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


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会覆盖恢复事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r9 污染恢复 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取普通 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return document


def load_transition() -> dict[str, Any]:
    """读取 r9 环境污染恢复后继 transition。"""

    return load_document(TRANSITION_PATH, "r9 污染恢复 transition")


def load_h1_transition() -> dict[str, Any]:
    """通过冻结文件摘要与自摘要读取模型目录 H1 前序。"""

    document = load_document(PREDECESSOR_PATH, "模型目录 H1 后继 transition")
    if (
        sha256(PREDECESSOR_PATH.read_bytes())
        != "e97b35438e82f4ee7adb905eadbb5483c035cd387b144e00d39c63321988bba6"
        or document.get("schema_version")
        != "official-client-codex-0.149.1-model-catalog-h1-successor-transition/v1"
        or document.get("scope") != "codex-0.149.1-model-catalog-h1-successor"
        or document.get("identity_sha256")
        != "688aa12024280bda592e54694a539ee32ac79a1db393a69032e212c8fc1b3217"
        or document.get("identity_sha256") != canonical_identity(document)
    ):
        raise ValueError("模型目录 H1 后继 transition 身份非法")
    return document


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


@lru_cache(maxsize=None)
def commit_blob(path: str) -> bytes | None:
    """读取恢复基线提交中的文件；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放污染根因、账号闭集、追加式文件闭集与生产边界。"""

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
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r9 污染恢复 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r9-contamination-recovery-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r9-contamination-recovery"
        or document["framework_stage"] != "VC-0/P0-R9-CONTAMINATION-RECOVERY"
        or document["result"] != "r10_campaign_required_with_repaired_tooling"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r9 污染恢复 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r9 污染恢复 transition 时间非法") from error

    predecessor = load_h1_transition()
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r9 污染恢复 transition 前序绑定非法")

    if document["failed_candidate_attempt"] != {
        "campaign_id": "codex-01491-r9",
        "candidate_id": "codex-01491-r9-93d179469-arm64-21",
        "attempt_id": "20260826T185433Z-081e83190732044e",
        "status": "environment_contaminated",
        "reuse_forbidden": True,
    }:
        raise ValueError("r9 污染恢复 transition 失败 Attempt 事实非法")
    if document["boundaries"] != {
        "api_key_ref": "#4",
        "capture_account_ref": "#21",
        "capture_account_20_allowed": False,
        "capture_account_20_used": False,
        "codex_only_selected_account_ids": [21],
        "new_campaign_required": True,
        "production_selector_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("r9 污染恢复 transition 账号或生产边界非法")
    if document["repair_facts"] != {
        "capture_cli_candidate_network_required": "sub2apiplus_sub2api-network",
        "capture_cli_dns_failure_detected": True,
        "capture_cli_network_repair_performed": False,
        "codex_only_matrix_touches_claude_account": False,
        "dynamic_account_state_restoration": True,
        "historical_evidence_preserved_read_only": True,
        "schedulable_state_order_normalized": True,
    }:
        raise ValueError("r9 污染恢复 transition 修复事实非法")
    if document["verification"] != {
        "bash_syntax_passed": True,
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "h1_successor_chain_replayed": True,
        "targeted_tests_passed": True,
    }:
        raise ValueError("r9 污染恢复 transition 门禁未闭合")
    if document["safety"] != {
        "active_remained_0_147_0": True,
        "arm64_deployment_performed": False,
        "historical_artifacts_modified": False,
        "previous_remained_0_149_1": True,
        "production_activation_receipt_created": False,
        "production_selector_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("r9 污染恢复 transition 安全边界非法")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r9 污染恢复 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r9 污染恢复 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        current = ROOT / path
        current_digest = (
            sha256(current.read_bytes())
            if current.is_file() and not current.is_symlink()
            else ""
        )
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != current_digest
                and not r11a_harness_transition_supersedes(
                    path,
                    entry["to_sha256"],
                    current_digest,
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r9 污染恢复 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"r9 污染恢复 transition 命中历史只读路径：{path}")


def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r9 污染恢复收据登记的精确追加边。"""

    if r15_supersedes(path, prior_digest, current_digest):
        return True

    try:
        document = load_validated_transition()
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry.get("path") == path
        and prior_digest in entry.get("predecessor_sha256s", [])
        and entry.get("to_sha256") == current_digest
        for entry in document["transitions"]
    )


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """验证 r9 收据，并向统一摘要链追加已验证的 r11a 后继边。"""

    document = load_transition()
    validate_transition(document)
    successor = load_r11a_harness_transition()
    replay = copy.deepcopy(document)
    replay["transitions"] = [
        *document["transitions"],
        *successor["transitions"],
    ]
    return replay


class Codex01491R9ContaminationRecoveryTransitionTest(unittest.TestCase):
    def test_transition_身份账号与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝账号20与生产边界篡改(self) -> None:
        document = load_transition()
        account_mutation = copy.deepcopy(document)
        account_mutation["boundaries"]["capture_account_ref"] = "#20"
        account_mutation["identity_sha256"] = canonical_identity(account_mutation)
        with self.assertRaisesRegex(ValueError, "账号或生产边界"):
            validate_transition(account_mutation)

        vircs_mutation = copy.deepcopy(document)
        vircs_mutation["safety"]["vircs_accessed"] = True
        vircs_mutation["identity_sha256"] = canonical_identity(vircs_mutation)
        with self.assertRaisesRegex(ValueError, "安全边界"):
            validate_transition(vircs_mutation)


if __name__ == "__main__":
    unittest.main()
