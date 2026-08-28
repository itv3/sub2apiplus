"""冻结 Codex CLI 0.149.1 r25 EP-014 Cookie 条件事实后继。"""

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

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.candidate_rule_assertion import (
    source_spec_section_sha256,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "1369078cecee21296978bae23a151206d250acfb"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r25-ep014-cookie-condition-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r24-selector-lite-coordinate-transition.json"
)
TOOLING_CLOSURE_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-service-successor-replay-transition.json"
)
EXPECTED_PATHS = sorted(
    [
        (
            "backend/internal/officialegress/"
            "codex_01491_r25_ep014_cookie_condition_transition_test.go"
        ),
        (
            "backend/internal/officialegress/"
            "codex_01491_service_successor_replay_transition_test.go"
        ),
        (
            "backend/internal/service/"
            "codex_01491_r25_ep014_cookie_condition_transition_test.go"
        ),
        (
            "backend/internal/service/"
            "codex_01491_service_successor_replay_transition_test.go"
        ),
        "docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md",
        (
            "tools/official_client_capture/"
            "candidate_rule_expectation_overrides_0_149_1.json"
        ),
        (
            "tools/official_client_capture/"
            "candidate_rule_expectations_0_149_1.json"
        ),
        "tools/official_client_capture/codex_upgrade.py",
        (
            "tools/official_client_capture/"
            "codex_upgrade_evidence_labels_0_149_1.json"
        ),
        (
            "tools/official_client_capture/"
            "codex_upgrade_scenarios_0_149_1.json"
        ),
        (
            "tools/official_client_capture/tests/"
            "test_candidate_rule_assertion.py"
        ),
        (
            "tools/official_client_capture/tests/"
            "test_codex_01491_candidate_gate_successor_transition.py"
        ),
        (
            "tools/official_client_capture/tests/"
            "test_codex_01491_r25_ep014_cookie_condition_transition.py"
        ),
        (
            "tools/official_client_capture/tests/"
            "test_codex_01491_service_successor_replay_transition.py"
        ),
        "tools/official_client_capture/tests/test_codex_upgrade.py",
    ]
)


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会遮蔽后继事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r25 EP-014 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取非符号链接 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return document


def canonical_identity(document: dict[str, Any]) -> str:
    """复算排除自摘要字段后的规范身份。"""

    identity = copy.deepcopy(document)
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


def base_blob(path: str) -> bytes | None:
    """读取 r25 基准提交中的普通 Git blob；不存在时返回 None。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def expected_contract() -> dict[str, Any]:
    """返回只读承接官方阶段并重跑候选的后继合同。"""

    return {
        "reason": "classification_fact_correction",
        "predecessor_import_schema": "codex-upgrade-predecessor-import/v3",
        "import_mode": "official_only_reclassification",
        "official_recapture_required": False,
        "official_evidence_replay_required": True,
        "approved_classification_imported": False,
        "classification_reapproval_required": True,
        "approved_scenario_rebind_required": True,
        "candidate_recapture_required": True,
        "kilo_revalidation_required": True,
    }


def expected_headers() -> tuple[list[str], list[str]]:
    """返回 EP-014 允许全集与 Cookie 之外的必选集合。"""

    allowed = [
        "version",
        "x-codex-installation-id",
        "x-codex-window-id",
        "x-codex-turn-metadata",
        "session-id",
        "thread-id",
        "x-codex-routing-hint",
        "x-openai-internal-codex-responses-lite",
        "authorization",
        "chatgpt-account-id",
        "content-type",
        "accept",
        "originator",
        "user-agent",
        "cookie",
        "host",
        "content-length",
    ]
    return allowed, [header for header in allowed if header != "cookie"]


def expected_corrected_fact() -> dict[str, Any]:
    """返回由两份既有官方 R 证据联合证明的条件事实。"""

    allowed, required = expected_headers()
    return {
        "rule_id": "SPEC-EP-014",
        "check_id": "legacy-default-headers",
        "selector": {
            "path": "/backend-api/codex/responses/compact",
            "variant": "default",
            "track": "lite",
        },
        "official_evidence": [
            {
                "path": (
                    "c1491-r14-f-lite-legacy-compact-default/relay/"
                    "conn006.client_to_upstream.bin"
                ),
                "track": "lite",
                "lite_header_present": True,
                "cookie_present": False,
            },
            {
                "path": (
                    "c1491-r14-f-official-legacy-compact-default/relay/"
                    "conn007.client_to_upstream.bin"
                ),
                "track": "main",
                "lite_header_present": False,
                "cookie_present": True,
            },
        ],
        "assertion": {
            "allowed": allowed,
            "operator": "all_ordered_subset_of",
            "path": "data.header_names_in_order",
            "required": required,
        },
        "cookie_condition": {
            "present_when": "cookie_jar_established",
            "absent_when": "cold_start_without_cookie_jar",
            "slot_after": "user-agent",
            "slot_before": "host",
        },
    }


def expected_model_coordinates() -> dict[str, Any]:
    """冻结本后继仍维持 main／Lite 双轨，不扩大为全 Luna。"""

    return {
        "main_track": "main",
        "main_model": "gpt-5.5",
        "lite_track": "lite",
        "lite_model": "gpt-5.6-luna",
        "whole_main_switched_to_luna": False,
    }


def validate_semantics(fact: dict[str, Any]) -> None:
    """验证覆盖清单、生成画像、标签和文档共同落实同一条件事实。"""

    override_path = (
        ROOT
        / "tools/official_client_capture/"
        "candidate_rule_expectation_overrides_0_149_1.json"
    )
    overrides = load_document(override_path, "0.149.1 断言覆盖清单")
    if overrides.get("schema_version") != (
        "codex-candidate-rule-expectation-overrides/v2"
    ):
        raise ValueError("r25 断言覆盖清单未升级为完整断言替换")
    operations = [
        operation
        for operation in overrides.get("operations", [])
        if operation.get("rule_id") == fact["rule_id"]
        and operation.get("check_id") == fact["check_id"]
    ]
    if len(operations) != 1 or operations[0].get("after") != fact["assertion"]:
        raise ValueError("r25 EP-014 覆盖断言未落实条件事实")

    profile_path = (
        ROOT
        / "tools/official_client_capture/"
        "candidate_rule_expectations_0_149_1.json"
    )
    profile = load_document(profile_path, "0.149.1 候选断言画像")
    checks = [
        check
        for rule in profile.get("rules", [])
        if rule.get("rule_id") == fact["rule_id"]
        for check in rule.get("checks", [])
        if check.get("id") == fact["check_id"]
    ]
    expected_where = [
        {
            "operator": "equal",
            "path": "data.path",
            "value": fact["selector"]["path"],
        },
        {
            "operator": "equal",
            "path": "labels.variant",
            "value": fact["selector"]["variant"],
        },
        {
            "operator": "equal",
            "path": "labels.track",
            "value": fact["selector"]["track"],
        },
    ]
    if (
        len(checks) != 1
        or checks[0].get("assertion") != fact["assertion"]
        or checks[0].get("select", {}).get("where") != expected_where
    ):
        raise ValueError("r25 EP-014 生成画像与批准选择器不一致")

    base_path = (
        ROOT
        / "tools/official_client_capture/"
        "candidate_rule_expectations_0_147_0.json"
    )
    base_profile = load_document(base_path, "0.147 基线断言画像")
    generated, count = codex_upgrade._apply_assertion_profile_overrides(
        base_profile,
        target_version="0.149.1",
        base_profile_path=base_path,
    )
    generated_check = next(
        check
        for rule in generated["rules"]
        if rule["rule_id"] == fact["rule_id"]
        for check in rule["checks"]
        if check["id"] == fact["check_id"]
    )
    if count != 3 or generated_check["assertion"] != fact["assertion"]:
        raise ValueError("r25 classify 无法确定性生成纠正后的 EP-014")

    labels_path = (
        ROOT
        / "tools/official_client_capture/"
        "codex_upgrade_evidence_labels_0_149_1.json"
    )
    labels = load_document(labels_path, "0.149.1 证据标签声明")

    def cookie_state(job_id: str, glob: str | None = None) -> str | None:
        jobs = [
            entry
            for entry in labels.get("entries", [])
            if entry.get("job_id") == job_id
        ]
        if len(jobs) != 1:
            return None
        rules = jobs[0].get("rules", [])
        if glob is not None:
            rules = [rule for rule in rules if rule.get("glob") == glob]
        states = {
            rule.get("labels", {}).get("cookie_state") for rule in rules
        }
        return states.pop() if len(states) == 1 else None

    if (
        cookie_state("official-lite-legacy-compact-default") != "absent"
        or cookie_state("official-relay-legacy-compact-default") != "present"
        or cookie_state(
            "candidate-frozen-aux",
            "scenarios/A09/relay/conn002.client_to_upstream.bin",
        )
        != "absent"
        or cookie_state(
            "candidate-frozen-aux",
            "scenarios/A09/relay/conn003.client_to_upstream.bin",
        )
        != "present"
    ):
        raise ValueError("r25 EP-014 冷启动／暖会话标签未闭合")

    spec_path = ROOT / "docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md"
    spec_sha = source_spec_section_sha256(spec_path, "第二章")
    scenario = load_document(
        ROOT
        / "tools/official_client_capture/"
        "codex_upgrade_scenarios_0_149_1.json",
        "0.149.1 场景清单",
    )
    if (
        profile.get("source_spec_sha256") != spec_sha
        or scenario.get("source_spec", {}).get("sha256") != spec_sha
    ):
        raise ValueError("r25 EP-014 规则正文摘要未同步")
    text = spec_path.read_text(encoding="utf-8")
    if (
        "Cookie jar" not in text
        or "不得把 Cookie 错误提升为 Lite 请求必选头" not in text
    ):
        raise ValueError("r25 EP-014 版本专属文档条件事实缺失")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 r25 身份、前序、条件事实、文件闭集与安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "tooling_closure",
        "correction_contract",
        "corrected_fact",
        "model_coordinates",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r25 EP-014 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r25-ep014-cookie-condition-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r25-ep014-cookie-condition"
        or document["framework_stage"]
        != "VC-3/VC-4/SAME-VERSION-SUCCESSOR"
        or document["result"]
        != "ep014_cookie_condition_successor_tooling_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r25 EP-014 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r25 EP-014 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r24 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r25 EP-014 transition 前序绑定非法")
    closure = load_document(TOOLING_CLOSURE_PATH, "service 后继闭合收据")
    if document["tooling_closure"] != {
        "path": TOOLING_CLOSURE_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(TOOLING_CLOSURE_PATH.read_bytes()),
        "identity_sha256": closure.get("identity_sha256"),
    }:
        raise ValueError("r25 EP-014 transition 工具闭合绑定非法")
    if document["correction_contract"] != expected_contract():
        raise ValueError("r25 EP-014 后继合同非法")
    fact = document["corrected_fact"]
    if fact != expected_corrected_fact():
        raise ValueError("r25 EP-014 条件事实非法")
    if document["model_coordinates"] != expected_model_coordinates():
        raise ValueError("r25 EP-014 模型坐标非法")
    if document["verification"] != {
        "official_raw_headers_replayed": True,
        "cold_and_warm_cookie_conditions_tested": True,
        "override_generation_tested": True,
        "mutation_tests_required": True,
    }:
        raise ValueError("r25 EP-014 验证事实非法")
    if document["safety"] != {
        "historical_artifacts_overwritten": False,
        "historical_receipts_modified": False,
        "official_recapture_performed": False,
        "candidate_capture_performed": False,
        "deployment_performed": False,
        "network_configuration_changed": False,
        "production_selector_changed": False,
        "production_activated": False,
        "arm64_read_only_evidence_accessed": True,
        "vircs_accessed": False,
        "dmit_server_accessed": False,
    }:
        raise ValueError("r25 EP-014 安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_PATHS):
        raise ValueError("r25 EP-014 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r25 EP-014 transition 路径未排序或重复")
    path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document["path_set_sha256"] != path_set:
        raise ValueError("r25 EP-014 transition 路径摘要非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r25 EP-014 transition 条目字段非法")
        path = entry["path"]
        previous = base_blob(path)
        expected_predecessors = [] if previous is None else [sha256(previous)]
        expected_change = "added" if previous is None else "modified"
        current = ROOT / path
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
            or path.startswith("docs/egress/maintenance/")
        ):
            raise ValueError(f"r25 EP-014 transition 条目非法：{path}")
    validate_semantics(fact)


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r25 transition。"""

    document = load_document(TRANSITION_PATH, "r25 EP-014 transition")
    validate_transition(document)
    return document


def r25_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r25 收据登记的精确 path/from/to 三元组。"""

    try:
        document = load_validated_transition()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and entry["to_sha256"] == current_digest
        for entry in document["transitions"]
    )


class Codex01491R25EP014CookieConditionTransitionTest(unittest.TestCase):
    def test_transition_身份条件事实和文件闭集可独立重放(self) -> None:
        validate_transition(load_document(TRANSITION_PATH, "r25 transition"))

    def test_transition_拒绝把_cookie_或_luna_扩大为无条件主线事实(self) -> None:
        document = load_document(TRANSITION_PATH, "r25 transition")
        cookie_mutation = copy.deepcopy(document)
        cookie_mutation["corrected_fact"]["assertion"]["required"].append(
            "cookie"
        )
        cookie_mutation["identity_sha256"] = canonical_identity(cookie_mutation)
        with self.assertRaisesRegex(ValueError, "条件事实非法"):
            validate_transition(cookie_mutation)

        model_mutation = copy.deepcopy(document)
        model_mutation["model_coordinates"]["main_model"] = "gpt-5.6-luna"
        model_mutation["model_coordinates"]["whole_main_switched_to_luna"] = True
        model_mutation["identity_sha256"] = canonical_identity(model_mutation)
        with self.assertRaisesRegex(ValueError, "模型坐标非法"):
            validate_transition(model_mutation)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_validated_transition()
        entry = next(row for row in document["transitions"] if row["change"] == "modified")
        self.assertTrue(
            r25_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            r25_supersedes(entry["path"], "0" * 64, entry["to_sha256"])
        )


if __name__ == "__main__":
    unittest.main()
