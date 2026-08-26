"""独立重放 Codex CLI 0.149.1 出站门禁链修复 transition。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_target_scenario_binding_transition import (
    load_transition as load_target_scenario_binding_transition,
    transition_supersedes as target_scenario_binding_transition_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "540fd460bf68d936ac8039d403a1035d21919897"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-egress-gate-chain-repair-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-formal-attempt-repair-transition.json"
)
EXPECTED_PATHS = [
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
    "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_egress_gate_chain_repair_transition.py",
    "tools/official_client_capture/tests/test_codex_01491_formal_attempt_repair_transition.py",
]
EXPECTED_PREDECESSOR_IDENTITY = (
    "699d42ad2b95794fbce04e6722710816a614942f45df5f1a5dedf41f1397edac"
)
EXPECTED_GO_PREDECESSOR = (
    "5ed34ff83310270ea18233dce7cde2e4184aefa59fe672bf08dc1bd566eccef7"
)
EXPECTED_MODIFIED_PREDECESSORS = {
    EXPECTED_PATHS[0]: EXPECTED_GO_PREDECESSOR,
    EXPECTED_PATHS[1]: "a0a8726f97d2a4fda8632c717a36b4fcfed386ca014344aad73b7614aec82781",
    EXPECTED_PATHS[3]: "eceae05e02a11aac51c6de750633a434fc44a36e2f2b035e5f9669d9f23f65d8",
}
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/",
    "backend/internal/officialegress/profilecontract/testdata/",
    "backend/internal/officialegress/releasecontract/testdata/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def sha256(content: bytes) -> str:
    """计算字节串的 SHA-256。"""

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
    """读取本次出站门禁链修复 transition。"""

    return load_document(TRANSITION_PATH)


def transition_supersedes(
    document: dict[str, Any],
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """重放出站门禁修复及 target 场景绑定后继的传递边。"""

    if any(
        entry["path"] == path
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document["transitions"]
    ):
        return True
    successor = load_target_scenario_binding_transition()
    if target_scenario_binding_transition_supersedes(
        successor,
        path,
        prior_digest,
        current_digest,
    ):
        return True
    for entry in document["transitions"]:
        if entry["path"] != path or prior_digest not in entry["predecessor_sha256s"]:
            continue
        if target_scenario_binding_transition_supersedes(
            successor,
            path,
            entry["to_sha256"],
            current_digest,
        ):
            return True
    return False


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


def validate_transition(document: dict[str, Any]) -> None:
    """重放身份、前序、文件闭集与安全边界。"""

    expected_keys = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }
    if set(document) != expected_keys:
        raise ValueError("出站门禁链修复 transition 顶层字段闭集非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-egress-gate-chain-repair-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-egress-gate-chain-repair"
        or document["framework_stage"] != "VC-0/P0-GATE-REPAIR"
        or document["result"] != "egress_gate_chain_repair_complete"
    ):
        raise ValueError("出站门禁链修复 transition 顶层事实非法")
    datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    if identity_sha256(document) != document["identity_sha256"]:
        raise ValueError("出站门禁链修复 transition 自摘要不一致")

    predecessor = load_document(PREDECESSOR_PATH)
    if identity_sha256(predecessor) != predecessor["identity_sha256"]:
        raise ValueError("Formal Attempt 前序 transition 自摘要不一致")
    expected_predecessor = {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": EXPECTED_PREDECESSOR_IDENTITY,
    }
    if document["predecessor_transition"] != expected_predecessor:
        raise ValueError("出站门禁链修复前序绑定非法")

    if document["verification"] != {
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "formal_transition_replayed": True,
        "independent_replay_passed": True,
        "mutation_tests_passed": True,
        "transition_closure_replayed": True,
    }:
        raise ValueError("出站门禁链修复验证事实未闭合")
    if document["safety"] != {
        "active_previous_changed": False,
        "arm64_read_only_diagnostics_performed": True,
        "arm64_tooling_synchronized": True,
        "catalog_promoted": False,
        "deployment_performed": False,
        "formal_campaign_created": False,
        "historical_receipts_modified": False,
        "live_request_sent": False,
        "production_selector_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("出站门禁链修复安全边界非法")

    transitions = document["transitions"]
    paths = [entry["path"] for entry in transitions]
    if paths != EXPECTED_PATHS:
        raise ValueError("出站门禁链修复路径闭集非法")
    for index, entry in enumerate(transitions):
        path = entry["path"]
        if any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise ValueError("出站门禁链修复命中历史只读路径")
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("出站门禁链修复条目字段闭集非法")
        predecessors = entry["predecessor_sha256s"]
        if predecessors != sorted(set(predecessors)) or not entry["reason"].strip():
            raise ValueError("出站门禁链修复条目非法")
        if path in EXPECTED_MODIFIED_PREDECESSORS:
            if entry["change"] != "modified" or predecessors != [
                EXPECTED_MODIFIED_PREDECESSORS[path]
            ]:
                raise ValueError("链适配修改条目前序非法")
        elif entry["change"] != "added" or predecessors:
            raise ValueError("独立重放测试新增条目非法")
        current = ROOT / path
        if current.is_symlink() or not current.is_file():
            raise ValueError(f"出站门禁链修复文件不可读：{path}")
        current_digest = sha256(current.read_bytes())
        if (
            current_digest != entry["to_sha256"]
            and not transition_supersedes(
                document,
                path,
                entry["to_sha256"],
                current_digest,
            )
        ):
            raise ValueError(f"出站门禁链修复当前摘要不一致：{path}")


class Codex01491EgressGateChainRepairTransitionTest(unittest.TestCase):
    def test_transition_is_independently_replayable(self) -> None:
        document = load_transition()
        validate_transition(document)

    def test_transition_rejects_identity_and_predecessor_mutations(self) -> None:
        document = load_transition()

        mutated_identity = copy.deepcopy(document)
        mutated_identity["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要不一致"):
            validate_transition(mutated_identity)

        mutated_predecessor = copy.deepcopy(document)
        mutated_predecessor["predecessor_transition"]["identity_sha256"] = "f" * 64
        mutated_predecessor["identity_sha256"] = identity_sha256(mutated_predecessor)
        with self.assertRaisesRegex(ValueError, "前序绑定非法"):
            validate_transition(mutated_predecessor)


if __name__ == "__main__":
    unittest.main()
