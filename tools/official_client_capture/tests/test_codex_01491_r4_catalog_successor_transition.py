"""冻结 Codex CLI 0.149.1 r4 候选 Catalog 追加式后继。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.official_client_capture.tests.test_codex_01491_r15_formal_classification_transition import (
    r15_supersedes,
)


ROOT = Path(__file__).resolve().parents[3]
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r4-catalog-successor-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-egress-surface-successor-transition.json"
)
EXPECTED_PATHS = [
    "backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
    (
        "backend/internal/officialegress/catalogdata/runtime/release-graphs/"
        "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4.json"
    ),
    "backend/internal/officialegress/codex_01491_candidate_source_transition_test.go",
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go",
    (
        "backend/internal/officialegress/"
        "codex_01491_r4_catalog_successor_transition_test.go"
    ),
    "backend/internal/officialegress/releasecontract/testdata/release-graph.json",
    "backend/internal/service/codex_01491_candidate_source_transition_test.go",
    "backend/internal/service/codex_01491_r4_catalog_successor_transition_test.go",
    "backend/internal/service/official_egress_changeset5_final_wire_test.go",
    "backend/internal/service/official_egress_profile_test.go",
    "tools/check_ledger_completeness.py",
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r4_catalog_successor_transition.py"
    ),
]
EXPECTED_PREDECESSORS = {
    "backend/internal/officialegress/catalogdata/runtime/release-catalog.json": [
        "f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259"
    ],
    "backend/internal/officialegress/codex_01491_candidate_source_transition_test.go": [
        "84df55efad07da2e5efa3b150fe1eb1745817aff12956ee2b6ff3763d3a0dcfe"
    ],
    "backend/internal/officialegress/codex_01491_p0_transition_chain_repair_test.go": [
        "e5983e782e6875937ae82e4271f3406ef745b1dbd0d07985afd9172bcb86475c"
    ],
    "backend/internal/officialegress/releasecontract/testdata/release-graph.json": [
        "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25"
    ],
    "backend/internal/service/codex_01491_candidate_source_transition_test.go": [
        "d870c8d511544e7ea81e0153fa722f00fa8ca1e98b70144bea071f3c95a426fb"
    ],
    "backend/internal/service/official_egress_changeset5_final_wire_test.go": [
        "687a27153325f0d242f8bbd363ac4cf2bde249c92b9396bd98ef35de5b4c05b9"
    ],
    "backend/internal/service/official_egress_profile_test.go": [
        "f1a744dc697603b5983cf7c94c4af900238fe633068701636f0ddfb0ead18591"
    ],
    "tools/check_ledger_completeness.py": [
        "3eea18d00402462c4652f58f9d7d482523acb52c38bf6ed192bc8a53a20d1685"
    ],
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ): ["2c364f6f6221c38a89c06b0b6aaf30d0fd067956e43b2f33575c134410e29696"],
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r4 transition 包含重复字段：{key}")
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
    """读取 r4 候选 Catalog 后继 transition。"""

    return load_document(TRANSITION_PATH, "r4 候选 Catalog 后继 transition")


def canonical_identity(document: dict[str, Any]) -> str:
    """复算排除自摘要后的规范身份。"""

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


def validate_transition(document: dict[str, Any]) -> None:
    """重放 r4 身份、Catalog、账号边界和追加式摘要链。"""

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
        raise ValueError("r4 候选 Catalog 后继 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r4-catalog-successor-transition/v1"
        or document["base_commit"]
        != "6d773d01aa5c81ec949355976568c770d1977207"
        or document["scope"] != "codex-0.149.1-r4-catalog-successor"
        or document["framework_stage"] != "VC-3/CANDIDATE-CATALOG-SUCCESSOR"
        or document["result"] != "r4_candidate_catalog_successor_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r4 候选 Catalog 后继 transition 身份非法")

    predecessor = load_document(PREDECESSOR_PATH, "候选出站面后继 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": (
            "2eb9981fcf6d813bc3bcdd24ca5cb39648d93d40d4445519998270dd5306abd0"
        ),
        "identity_sha256": (
            "3b1f765ced2d288e4bad7eb3eabcd0c093bf653e6cd83c0e00abf788763148a4"
        ),
    } or sha256(PREDECESSOR_PATH.read_bytes()) != document["predecessor_transition"][
        "file_sha256"
    ] or predecessor.get("identity_sha256") != document["predecessor_transition"][
        "identity_sha256"
    ]:
        raise ValueError("r4 候选 Catalog 后继 transition 前序绑定非法")

    if document["campaign"] != {
        "id": (
            "codex-0_149_1-formal-production-replacement-"
            "20260826T140949Z-2c1ab3b9e-r4"
        ),
        "official_attempt_id": "20260826T141046Z-90545b2a079aa94a",
        "purpose": "production_replacement",
        "target_version": "0.149.1",
        "target_profile_sha256": (
            "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
        ),
        "classification_sha256": (
            "058bb9a2d78ba64ecda1a2a8025158b6cf64f0183a931a2fd1b519146838a599"
        ),
        "review_sha256": (
            "a72a22ef4ec9d8d78329d313175c4cbff97e0537b19eafde0aebadbf83f436bd"
        ),
        "required_rule_count": 42,
        "discovery_count": 2090,
        "capture_account_ref": "#21",
        "api_key_ref": "#4",
    }:
        raise ValueError("r4 候选 Catalog 后继 transition Campaign 或账号身份非法")

    expected_catalog = {
        "active_version": "0.147.0",
        "previous_version": "0.149.1",
        "active_unchanged": True,
        "production_selector_changed": False,
        "release_catalog": {
            "path": (
                "backend/internal/officialegress/catalogdata/runtime/"
                "release-catalog.json"
            ),
            "predecessor_sha256": (
                "f7d4c7b6f6ab045c4c4cec1ca87f29928366199c16f1838717dd02dc91a50259"
            ),
            "sha256": (
                "c26b274a5942eee249ae4755aa60c399745f03212077e798fb5d582f0ce6c81e"
            ),
        },
        "release_graph": {
            "path": (
                "backend/internal/officialegress/catalogdata/runtime/release-graphs/"
                "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4.json"
            ),
            "sha256": (
                "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4"
            ),
        },
        "contract_release_graph": {
            "path": (
                "backend/internal/officialegress/releasecontract/"
                "testdata/release-graph.json"
            ),
            "predecessor_sha256": (
                "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25"
            ),
            "sha256": (
                "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4"
            ),
        },
        "inventory_sha256": (
            "f00545f1296175c1a60f51ec770bf050832dbf80bbc61569fbfe9ec78759111b"
        ),
    }
    if document["staged_catalog"] != expected_catalog:
        raise ValueError("r4 候选 Catalog 后继 transition 暂存 Catalog 非法")
    for binding_name in ("release_catalog", "release_graph", "contract_release_graph"):
        binding = expected_catalog[binding_name]
        path = ROOT / binding["path"]
        current_digest = sha256(path.read_bytes()) if path.is_file() else ""
        if (
            path.is_symlink()
            or not path.is_file()
            or (
                current_digest != binding["sha256"]
                and not r15_supersedes(
                    binding["path"],
                    binding["sha256"],
                    current_digest,
                )
            )
        ):
            raise ValueError(f"r4 候选 Catalog 后继 transition 摘要漂移：{binding_name}")

    release_catalog = load_document(
        ROOT / expected_catalog["release_catalog"]["path"],
        "r4 release-catalog",
    )
    legacy_catalog_bound = release_catalog.get("source") == (
        "campaign:codex-0_149_1-formal-production-replacement-"
        "20260826T140949Z-2c1ab3b9e-r4/classification:"
        "058bb9a2d78ba64ecda1a2a8025158b6cf64f0183a931a2fd1b519146838a599"
    ) and release_catalog.get("release_graph") == {
        "path": (
            "catalogdata/runtime/release-graphs/"
            "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4.json"
        ),
        "sha256": (
            "9824eee0200ea1be1136a1a87ea5accc9a8e5a7c48855b4f0c06587eeab17ca4"
        ),
    }
    release_catalog_current_digest = sha256(
        (ROOT / expected_catalog["release_catalog"]["path"]).read_bytes()
    )
    if not legacy_catalog_bound and not r15_supersedes(
        expected_catalog["release_catalog"]["path"],
        expected_catalog["release_catalog"]["sha256"],
        release_catalog_current_digest,
    ):
        raise ValueError("r4 release-catalog 未精确绑定正式分类")

    if document["verification"] != {
        "official_attempt_sealed": True,
        "classification_approved": True,
        "all_rules_mapped": True,
        "all_discoveries_mapped": True,
        "secret_scan_clean": True,
        "catalog_inventory_verified": True,
        "historical_transition_chain_replayed": True,
        "mutation_tests_passed": True,
    }:
        raise ValueError("r4 候选 Catalog 后继 transition 验证事实非法")
    if document["safety"] != {
        "active_remained_0_147_0": True,
        "previous_staged_0_149_1": True,
        "candidate_catalog_staged": True,
        "production_selector_changed": False,
        "historical_content_addressed_artifacts_overwritten": False,
        "historical_receipts_modified": False,
        "historical_transitions_modified": False,
        "deployment_performed": False,
        "live_request_sent": False,
        "arm64_accessed_for_this_transition": False,
        "vircs_accessed": False,
        "capture_account_20_used": False,
        "capture_account_21_used": True,
    }:
        raise ValueError("r4 候选 Catalog 后继 transition 安全边界非法")

    path_set_sha256 = sha256(
        json.dumps(EXPECTED_PATHS, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    if document["path_set_sha256"] != path_set_sha256:
        raise ValueError("r4 候选 Catalog 后继 transition 路径摘要非法")
    entries = document["transitions"]
    if not isinstance(entries, list) or [entry.get("path") for entry in entries] != EXPECTED_PATHS:
        raise ValueError("r4 候选 Catalog 后继 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r4 候选 Catalog 后继 transition 条目字段非法")
        path_value = entry["path"]
        predecessors = EXPECTED_PREDECESSORS.get(path_value, [])
        expected_change = "modified" if predecessors else "added"
        current = ROOT / path_value
        current_digest = sha256(current.read_bytes()) if current.is_file() else ""
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != current_digest
                and not r9_recovery_transition_supersedes(
                    path_value,
                    entry["to_sha256"],
                    current_digest,
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r4 候选 Catalog 后继 transition 条目非法：{path_value}")

    historical_graph = (
        ROOT
        / "backend/internal/officialegress/catalogdata/runtime/release-graphs/"
        "591347d95b380bdb789a0c05f76796e61c8f7e7bdd8f0eb39442c0c4f3716f25.json"
    )
    if sha256(historical_graph.read_bytes()) != historical_graph.stem:
        raise ValueError("r4 候选 Catalog 后继覆盖了历史 content-addressed ReleaseGraph")


def r9_recovery_transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """延迟加载 r9 恢复模块，避免候选门禁模块初始化形成循环依赖。"""

    if r15_supersedes(path, prior_digest, current_digest):
        return True

    from tools.official_client_capture.tests import (
        test_codex_01491_r9_contamination_recovery_transition as r9_recovery,
    )

    if r9_recovery.transition_supersedes(path, prior_digest, current_digest):
        return True

    from tools.official_client_capture.tests import (
        test_codex_01491_r13_candidate_coordinate_transition as r13_coordinate,
    )

    try:
        successor = r13_coordinate.load_validated_transition()
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return r13_coordinate.transition_supersedes(
        successor,
        path,
        prior_digest,
        current_digest,
    )


@lru_cache(maxsize=None)
def transition_chain_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """重放 r4、模型目录 H1 与 r9 污染恢复的追加式摘要链。"""

    if r15_supersedes(path, prior_digest, current_digest):
        return True

    try:
        from tools.official_client_capture.tests import (
            test_codex_01491_r9_contamination_recovery_transition as r9_recovery,
        )

        document = load_transition()
        validate_transition(document)
        h1_document = r9_recovery.load_h1_transition()
        recovery_document = r9_recovery.load_validated_transition()
    except (OSError, ValueError, json.JSONDecodeError):
        return False

    edges: dict[str, list[str]] = {}
    for receipt in (document, h1_document, recovery_document):
        for entry in receipt["transitions"]:
            if entry.get("path") != path:
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


class Codex01491R4CatalogSuccessorTransitionTests(unittest.TestCase):
    def test_r4_身份Catalog与账号边界可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_r4_拒绝账号20与自摘要篡改(self) -> None:
        document = load_transition()
        account_mutation = copy.deepcopy(document)
        account_mutation["campaign"]["capture_account_ref"] = "#20"
        account_mutation["identity_sha256"] = canonical_identity(account_mutation)
        with self.assertRaisesRegex(ValueError, "Campaign 或账号身份非法"):
            validate_transition(account_mutation)

        identity_mutation = copy.deepcopy(document)
        identity_mutation["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "身份非法"):
            validate_transition(identity_mutation)

    def test_r4_精确后继摘要链被承认(self) -> None:
        document = load_transition()
        entry = document["transitions"][0]
        self.assertTrue(
            transition_chain_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
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
