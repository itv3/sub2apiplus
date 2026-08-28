"""冻结 Codex CLI 0.149.1 候选门禁后继 transition。"""

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

from tools import check_ledger_completeness as ledger
from tools.official_client_capture.tests import (
    test_codex_01491_r4_catalog_successor_transition as r4_catalog,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "580ac615c759170cfb745e7b71fa02a9e1c3f12e"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-candidate-gate-successor-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/codex-0.149.1-candidate-source-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_candidate_gate_successor_transition.py"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/codex_01491_candidate_source_transition_test.go",
        "backend/internal/officialegress/routing_hint.go",
        "backend/internal/service/codex_01491_candidate_source_transition_test.go",
        "backend/internal/service/official_egress_changeset5_final_wire_test.go",
        "backend/internal/service/official_egress_codex_0145_profile.go",
        "backend/internal/service/official_egress_codex_0145_profile_test.go",
        "backend/internal/service/openai_forward_plan_test.go",
        SELF_PATH,
        "tools/official_client_capture/tests/test_codex_01491_capture_runtime_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_direct_readiness_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_doc_pre_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_egress_gate_chain_repair_transition.py",
        "tools/official_client_capture/tests/test_codex_01491_target_scenario_binding_transition.py",
    }
)
FORBIDDEN_PREFIXES = (
    "backend/internal/officialegress/catalogdata/runtime/profiles/0.145.0/",
    "backend/internal/officialegress/catalogdata/runtime/release-graphs/",
    "backend/internal/officialegress/catalogdata/runtime/snapshot-catalogs/",
    "docs/egress/lifecycle/migration-artifacts/",
)


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会覆盖冻结事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取普通 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return value


def load_transition() -> dict[str, Any]:
    """读取候选门禁后继 transition。"""

    return load_document(TRANSITION_PATH, "候选门禁后继 transition")


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


def commit_blob(path: str) -> bytes | None:
    """读取候选基线提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


@lru_cache(maxsize=None)
def transition_chain_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认候选源码与门禁后继登记的可达摘要链。"""

    if r4_catalog.transition_chain_supersedes(
        path,
        prior_digest,
        current_digest,
    ):
        return True

    try:
        from tools.official_client_capture.tests import (
            test_codex_01491_r9_contamination_recovery_transition as r9_recovery,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r13_candidate_coordinate_transition as r13_coordinate,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r16_successor_carry_forward_transition as r16_successor,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r17_kilo_model_contract_transition as r17_successor,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r18_successor_account_transition as r18_successor,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r19_successor_chain_transition as r19_successor,
        )
        from tools.official_client_capture.tests import (
            test_codex_01491_r20_candidate_aux_transition as r20_successor,
        )

        predecessor = load_document(PREDECESSOR_PATH, "候选源码 transition")
        candidate_gate = load_transition()
        surface_successor = load_document(
            ledger.CANDIDATE_SURFACE_SUCCESSOR,
            "候选出站面后继 transition",
        )
        r4_successor = r4_catalog.load_transition()
        r4_catalog.validate_transition(r4_successor)
        h1_successor = r9_recovery.load_h1_transition()
        recovery_successor = r9_recovery.load_validated_transition()
        r13_successor = r13_coordinate.load_validated_transition()
        r16_successor_document = r16_successor.load_validated_transition()
        r17_successor_document = r17_successor.load_validated_transition()
        r18_successor_document = r18_successor.load_validated_transition()
        r19_successor_document = r19_successor.load_validated_transition()
        r20_successor_document = r20_successor.load_validated_transition()
        ledger.validate_candidate_surface_successor(
            surface_successor,
            ledger.INVENTORY.read_bytes(),
            ledger.CHANGESET6_TRANSITION.read_bytes(),
            TRANSITION_PATH.read_bytes(),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    edges: dict[str, list[str]] = {}
    for document, field in (
        (predecessor, "transitions"),
        (candidate_gate, "transitions"),
        (surface_successor, "implementation_transitions"),
        (r4_successor, "transitions"),
        (h1_successor, "transitions"),
        (recovery_successor, "transitions"),
        (r13_successor, "transitions"),
        (r16_successor_document, "transitions"),
        (r17_successor_document, "transitions"),
        (r18_successor_document, "transitions"),
        (r19_successor_document, "transitions"),
        (r20_successor_document, "transitions"),
    ):
        transitions = document.get(field)
        if not isinstance(transitions, list):
            return False
        for entry in transitions:
            if not isinstance(entry, dict) or entry.get("path") != path:
                continue
            target = entry.get("to_sha256")
            predecessors = entry.get("predecessor_sha256s")
            if not isinstance(target, str) or not isinstance(predecessors, list):
                return False
            for predecessor in predecessors:
                if isinstance(predecessor, str):
                    edges.setdefault(predecessor, []).append(target)

    queue = [prior_digest]
    visited: set[str] = set()
    while queue:
        digest = queue.pop(0)
        if digest == current_digest:
            return True
        if digest in visited:
            continue
        visited.add(digest)
        queue.extend(edges.get(digest, []))
    return False


def validate_transition(document: dict[str, Any]) -> None:
    """重放后继身份、前序、路径闭集、候选 Catalog 与安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "campaign",
        "staged_catalog",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("候选门禁后继 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-candidate-gate-successor-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-candidate-gate-successor"
        or document["framework_stage"] != "VC-4/CANDIDATE-GATE-SUCCESSOR"
        or document["result"] != "candidate_gate_successor_frozen"
    ):
        raise ValueError("候选门禁后继 transition 顶层事实非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("候选门禁后继 transition 时间非法") from error
    if document["identity_sha256"] != canonical_identity(document):
        raise ValueError("候选门禁后继 transition 自摘要不一致")

    predecessor = load_document(PREDECESSOR_PATH, "候选源码 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("候选门禁后继 transition 前序绑定非法")
    if (
        predecessor.get("schema_version")
        != "official-client-codex-0.149.1-candidate-source-transition/v1"
        or predecessor.get("scope") != "codex-0.149.1-candidate-source-transition"
        or predecessor.get("result")
        != "candidate_source_transition_frozen_pending_full_gates"
        or predecessor.get("identity_sha256") != canonical_identity(predecessor)
    ):
        raise ValueError("候选门禁后继 transition 前序身份非法")

    if document["campaign"] != {
        "id": "codex-0_149_1-formal-production-replacement-20260826T092125Z-580ac615c",
        "purpose": "production_replacement",
        "target_version": "0.149.1",
        "target_profile_sha256": (
            "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
        ),
    }:
        raise ValueError("候选门禁后继 transition Campaign 身份非法")
    if document["staged_catalog"] != {
        "active_version": "0.147.0",
        "previous_version": "0.149.1",
        "release_catalog_path": (
            "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
        ),
        "release_catalog_sha256": (
            "f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259"
        ),
        "release_graph_path": (
            "backend/internal/officialegress/catalogdata/runtime/release-graphs/"
            "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25.json"
        ),
        "release_graph_sha256": (
            "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25"
        ),
        "contract_release_graph_path": (
            "backend/internal/officialegress/releasecontract/testdata/release-graph.json"
        ),
        "contract_release_graph_sha256": (
            "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25"
        ),
    }:
        raise ValueError("候选门禁后继 transition 暂存 Catalog 事实非法")
    for key in (
        "release_catalog_path",
        "release_graph_path",
        "contract_release_graph_path",
    ):
        path = ROOT / document["staged_catalog"][key]
        digest_key = key.removesuffix("_path") + "_sha256"
        current_digest = sha256(path.read_bytes()) if path.is_file() else ""
        if path.is_symlink() or not path.is_file() or (
            current_digest != document["staged_catalog"][digest_key]
            and not r4_catalog.transition_chain_supersedes(
                path.relative_to(ROOT).as_posix(),
                document["staged_catalog"][digest_key],
                current_digest,
            )
        ):
            raise ValueError(f"候选门禁后继 transition 暂存 Catalog 漂移：{key}")

    if document["verification"] != {
        "candidate_source_transition_replayed": True,
        "historical_transition_chain_replayed": True,
        "mutation_tests_passed": True,
        "staged_catalog_state_verified": True,
        "targeted_tests_passed": True,
    }:
        raise ValueError("候选门禁后继 transition 验证事实非法")
    if document["safety"] != {
        "active_remained_0_147_0": True,
        "arm64_accessed_for_this_transition": False,
        "catalog_promoted": False,
        "deployment_performed": False,
        "historical_content_addressed_artifacts_overwritten": False,
        "historical_receipts_modified": False,
        "historical_transitions_modified": False,
        "live_request_sent": False,
        "previous_staged_0_149_1": True,
        "production_selector_changed": False,
        "vircs_accessed": False,
    }:
        raise ValueError("候选门禁后继 transition 安全边界非法")

    expected_path_set = sha256(
        json.dumps(EXPECTED_PATHS, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document["path_set_sha256"] != expected_path_set:
        raise ValueError("候选门禁后继 transition 路径摘要非法")
    predecessor_entries = {
        entry.get("path"): entry
        for entry in predecessor.get("transitions", [])
        if isinstance(entry, dict)
    }
    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("候选门禁后继 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("候选门禁后继 transition 条目字段非法")
        path_value = entry["path"]
        if path_value.startswith(FORBIDDEN_PREFIXES):
            raise ValueError("候选门禁后继 transition 命中历史只读路径")
        predecessor_entry = predecessor_entries.get(path_value)
        before = commit_blob(path_value)
        if predecessor_entry is not None:
            expected_change = "modified"
            expected_predecessors = [predecessor_entry["to_sha256"]]
        elif before is None:
            expected_change = "added"
            expected_predecessors = []
        else:
            expected_change = "modified"
            expected_predecessors = [sha256(before)]
        current = ROOT / path_value
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != sha256(current.read_bytes())
                and not transition_chain_supersedes(
                    path_value,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"候选门禁后继 transition 条目非法：{path_value}")


class Codex01491CandidateGateSuccessorTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝身份与安全边界篡改(self) -> None:
        document = load_transition()
        mutated_identity = copy.deepcopy(document)
        mutated_identity["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "自摘要不一致"):
            validate_transition(mutated_identity)

        mutated_safety = copy.deepcopy(document)
        mutated_safety["safety"]["vircs_accessed"] = True
        mutated_safety["identity_sha256"] = canonical_identity(mutated_safety)
        with self.assertRaisesRegex(ValueError, "安全边界非法"):
            validate_transition(mutated_safety)

    def test_transition_精确后继摘要链被承认(self) -> None:
        predecessor = load_document(PREDECESSOR_PATH, "候选源码 transition")
        document = load_transition()
        entry = next(
            item
            for item in document["transitions"]
            if item["path"].endswith("test_codex_01491_doc_pre_transition.py")
        )
        source_entry = next(
            item
            for item in predecessor["transitions"]
            if item["path"] == entry["path"]
        )
        self.assertTrue(
            transition_chain_supersedes(
                entry["path"],
                source_entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            transition_chain_supersedes(
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )


if __name__ == "__main__":
    unittest.main()
