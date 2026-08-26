"""冻结 Codex CLI 0.149.1 预热目录绑定修复后继 transition。"""

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
BASE_COMMIT = "0a09b60d33cfbeb139aff75e605f3bb2d4aabdfa"
TARGET_COMMIT = "9da0cc904b338bfc5a3c2ac1980d2fdc0f66a6bd"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-model-catalog-binding-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT / "docs/egress/maintenance/codex-0.149.1-model-catalog-repair-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_model_catalog_binding_transition.py"
)
EXPECTED_PATHS = {
    "tools/official_client_capture/model_condition_receipts.py",
    "tools/official_client_capture/run_official_relay_scenario.sh",
    "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
    "tools/official_client_capture/tests/test_model_condition_receipt.py",
    SELF_PATH,
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def commit_blob(commit: str, path: str) -> bytes | None:
    """读取指定提交中的文件；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_transition(document: dict[str, Any]) -> None:
    """重放预热目录绑定修复的身份、闭集、恢复事实与生产边界。"""

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
        raise ValueError("模型目录绑定 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-model-catalog-binding-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["target_commit"] != TARGET_COMMIT
        or document["scope"] != "codex-0.149.1-model-catalog-binding-repair"
        or document["framework_stage"]
        != "VC-0/P0-MODEL-CATALOG-BINDING-REPAIR-SUCCESSOR"
        or document["result"]
        != "prewarm_catalog_binding_repair_ready_for_p0"
    ):
        raise ValueError("模型目录绑定 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("模型目录绑定 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("模型目录绑定 transition 自摘要不一致")

    predecessor = load_json(PREDECESSOR_PATH, "模型目录修复 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("模型目录绑定 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-model-catalog-repair-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-model-catalog-repair"
        or predecessor.get("result")
        != "model_catalog_and_compaction_repair_ready_for_p0"
    ):
        raise ValueError("模型目录绑定 transition 前序身份非法")

    if document["boundaries"] != {
        "complete_models_http_200_required": True,
        "failed_campaign_preserved_read_only": True,
        "historical_artifacts_modified": False,
        "prewarm_capture_coordinate_bound": True,
        "production_selector_changed": False,
        "scrubbed_manifest_integrity_required": True,
        "sub2api_candidate_deployed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("模型目录绑定 transition 能力边界非法")
    if document["execution_facts"] != {
        "failed_attempt_id": "20260826T033217Z-7dbe49d6e95db310",
        "failed_attempt_status": "failed",
        "failed_campaign_id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T033113Z-0a09b60d3"
        ),
        "failed_sample_replay": {
            "job_id": "official-relay-http-response-plain",
            "model_id": "gpt-5.5",
            "models_response_sha256": (
                "2ba6c2c23c0a3186a1e790ad010788563418063c1cb64f80ae7cc2b82b1fa4d1"
            ),
            "status": "success",
            "use_responses_lite": False,
        },
        "observed_model_conditions": {
            "gpt-5.4-mini": False,
            "gpt-5.5": False,
            "gpt-5.6-luna": True,
        },
        "post_repair_capture_started": False,
        "restoration_report_sha256": (
            "7eade97b7b20991f34fe016afb2e32a7c659a439570f0e9eabf92d68e668aa9c"
        ),
        "restoration_status": "restored",
        "vircs_accessed": False,
    }:
        raise ValueError("模型目录绑定 transition 执行事实非法")
    if set(document["verification"]) != {
        "actual_failed_sample_replay_passed",
        "arm64_restoration_verified",
        "bash_syntax_passed",
        "capture_tool_tests_passed",
        "egress_spec_ci_passed",
        "predecessor_transition_replayed",
        "targeted_tests_passed",
    } or not all(document["verification"].values()):
        raise ValueError("模型目录绑定 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(EXPECTED_PATHS) or len(paths) != len(set(paths)):
        raise ValueError("模型目录绑定 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("模型目录绑定 transition 条目字段非法")
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
            raise ValueError(f"模型目录绑定 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"模型目录绑定 transition 命中历史只读路径：{path}")


class Codex01491ModelCatalogBindingTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_json(TRANSITION_PATH, "模型目录绑定 transition"))

    def test_transition_拒绝身份与生产边界篡改(self) -> None:
        document = load_json(TRANSITION_PATH, "模型目录绑定 transition")
        document["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要"):
            validate_transition(document)

        document = load_json(TRANSITION_PATH, "模型目录绑定 transition")
        document["boundaries"]["vircs_accessed"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "能力边界"):
            validate_transition(document)


if __name__ == "__main__":
    unittest.main()
