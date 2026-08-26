"""冻结 Codex CLI 0.149.1 模型目录与压缩场景修复后继 transition。"""

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
BASE_COMMIT = "ae9d7e10a52ff792cea0b881dc4248af2749d417"
TARGET_COMMIT = "650a15d7c16b6518bb07c97a25d13598489281b9"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-model-catalog-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT / "docs/egress/maintenance/codex-0.149.1-shell-home-repair-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_model_catalog_transition.py"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/capturelib/identity.py",
    "tools/official_client_capture/drive_codex_model_catalog.py",
    "tools/official_client_capture/model_condition_receipts.py",
    "tools/official_client_capture/run_official_relay_scenario.sh",
    "tools/official_client_capture/tests/test_compaction_scenario_models.py",
    SELF_PATH,
    "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
    "tools/official_client_capture/tests/test_model_condition_receipt.py",
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def commit_blob(commit: str, path: str) -> bytes | None:
    """读取指定提交中的文件；该提交不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放模型目录修复的前序身份、文件闭集和服务器安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "target_commit",
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
        raise ValueError("模型目录 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-model-catalog-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["target_commit"] != TARGET_COMMIT
        or document["scope"] != "codex-0.149.1-model-catalog-repair"
        or document["framework_stage"]
        != "VC-0/P0-MODEL-CONDITION-REPAIR-SUCCESSOR"
        or document["result"]
        != "model_catalog_and_compaction_repair_ready_for_p0"
    ):
        raise ValueError("模型目录 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("模型目录 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("模型目录 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "沙箱 HOME transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("模型目录 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-shell-home-repair-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-shell-home-repair"
        or predecessor.get("result") != "shell_home_repair_ready_for_p0"
    ):
        raise ValueError("模型目录 transition 前序身份非法")

    if document["boundaries"] != {
        "compaction_primary_model_campaign_bound": True,
        "historical_campaigns_preserved_read_only": True,
        "model_receipt_requires_complete_http_200": True,
        "production_selector_changed": False,
        "request_only_exclusion_manifest_bound": True,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("模型目录 transition 能力边界非法")
    if document["execution_facts"] != {
        "interrupted_attempt_id": "20260826T021733Z-f35fd27a31f17b7c",
        "interrupted_attempt_status": "failed",
        "interrupted_campaign_id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T021655Z-ae9d7e10a"
        ),
        "new_capture_account_login_verified": True,
        "post_switch_capture_started": False,
        "restoration_report_sha256": (
            "3fa92e3eb8a32fc052861c6781163e0752fc8b7f6dc8fc98161911d4b114aed9"
        ),
        "restoration_status": "restored",
        "runtime_image_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("模型目录 transition 执行事实非法")
    if set(document["verification"]) != {
        "arm64_source_reference_gate_passed",
        "bash_syntax_passed",
        "capture_tool_tests_passed",
        "egress_spec_ci_passed",
        "predecessor_transition_replayed",
        "targeted_tests_passed",
    } or not all(document["verification"].values()):
        raise ValueError("模型目录 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("模型目录 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("模型目录 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(BASE_COMMIT, path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        if path == SELF_PATH:
            current = ROOT / path
            target = (
                current.read_bytes()
                if current.is_file() and not current.is_symlink()
                else None
            )
        else:
            target = commit_blob(TARGET_COMMIT, path)
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or target is None
            or entry["to_sha256"] != sha256(target)
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"模型目录 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"模型目录 transition 命中历史只读路径：{path}")


class Codex01491ModelCatalogTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "模型目录 transition"))

    def test_transition_拒绝身份与服务器事实篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "模型目录 transition")
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "模型目录 transition")
        document["execution_facts"]["post_switch_capture_started"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "执行事实"):
            validate_transition(document)


if __name__ == "__main__":
    unittest.main()
