"""冻结 Codex CLI 0.149.1 模型目录 H1 补采后继 transition。"""

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
BASE_COMMIT = "93d179469b0e6f4643242a1340b7f2aff6e7113c"
TARGET_COMMIT = "edaa717252a2e7eceb47529e0fa34f9bd0d9f922"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-model-catalog-h1-successor-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r4-catalog-successor-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_model_catalog_h1_successor_transition.py"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
    "tools/official_client_capture/drive_codex_model_catalog.py",
    "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
    SELF_PATH,
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def commit_blob(commit: str, path: str) -> bytes | None:
    """读取指定提交中的文件；该文件不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放补采根因、ARM64 实测结果、文件闭集与生产边界。"""

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
        raise ValueError("模型目录 H1 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-model-catalog-h1-successor-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["target_commit"] != TARGET_COMMIT
        or document["scope"] != "codex-0.149.1-model-catalog-h1-successor"
        or document["framework_stage"] != "VC-0/P0-MODEL-CATALOG-H1-SUCCESSOR"
        or document["result"] != "model_catalog_h1_successor_ready_for_p0"
    ):
        raise ValueError("模型目录 H1 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("模型目录 H1 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("模型目录 H1 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "r4 Catalog 后继 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("模型目录 H1 transition 前序绑定非法")
    if document["boundaries"] != {
        "api_key_ref": "#4",
        "capture_account_ref": "#21",
        "historical_evidence_preserved_read_only": True,
        "official_request_origin": "codex-cli-0.149.1",
        "production_selector_changed": False,
        "proxy_http2_disabled_for_models_only": True,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("模型目录 H1 transition 能力边界非法")

    facts = document["execution_facts"]
    if (
        facts.get("root_cause")
        != "mitm_http2_models_response_reset_before_complete_http_200"
        or facts.get("diagnostic", {}).get("driver_status") != 0
        or facts.get("diagnostic", {}).get("valid_models_http_200") != 1
        or facts.get("diagnostic", {}).get("model_id") != "gpt-5.5"
        or facts.get("diagnostic", {}).get("use_responses_lite") is not False
        or [item.get("campaign_id") for item in facts.get("failed_campaigns", [])]
        != ["codex-01491-r6", "codex-01491-r7"]
        or not all(
            item.get("status") == "failed"
            and item.get("restoration_status") == "restored"
            and item.get("restoration_all_checks_passed") is True
            for item in facts.get("failed_campaigns", [])
        )
    ):
        raise ValueError("模型目录 H1 transition 执行事实非法")
    if set(document["verification"]) != {
        "arm64_h1_diagnostic_passed",
        "campaign_restoration_passed",
        "json_schema_load_passed",
        "targeted_python_tests_passed",
    } or not all(document["verification"].values()):
        raise ValueError("模型目录 H1 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("模型目录 H1 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("模型目录 H1 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(BASE_COMMIT, path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        target = (
            (ROOT / path).read_bytes()
            if path == SELF_PATH
            else commit_blob(TARGET_COMMIT, path)
        )
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or target is None
            or entry["to_sha256"] != sha256(target)
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"模型目录 H1 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"模型目录 H1 transition 命中历史只读路径：{path}")


class Codex01491ModelCatalogH1SuccessorTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "模型目录 H1 transition"))

    def test_transition_拒绝账号或生产边界篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "模型目录 H1 transition")
        document["boundaries"]["capture_account_ref"] = "#20"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "能力边界"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "模型目录 H1 transition")
        document["boundaries"]["production_selector_changed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "能力边界"):
            validate_transition(document)


if __name__ == "__main__":
    unittest.main()
