"""冻结 Codex CLI 0.149.1 r22 候选 Catalog 追加式制品。"""

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
BASE_COMMIT = "b37e16dd03fe92a5f0e32fd2e0291a5bcb9d3de8"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r22-candidate-catalog-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r21-classification-fact-correction-transition.json"
)
PROFILE_DIGEST = (
    "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3"
)
HISTORICAL_PROFILE_DIGEST = (
    "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60"
)
CLASSIFICATION_SHA256 = (
    "62dd45d20c0fded5441b987fef0b913898fca98986b8705f3f42e0f83e221aca"
)
RELEASE_GRAPH_SHA256 = (
    "071362d48ff01553ba4ffb44371cf04c88f6224361090e62ee5e6ff03a619cfc"
)
SNAPSHOT_CATALOG_SHA256 = (
    "33c7840f95d32677e4189c57d8ee8d3b18fce040ed91c1a707f120e76ce6f905"
)
EXPECTED_PATHS = [
    (
        "backend/internal/officialegress/catalogdata/runtime/profiles/0.149.1/"
        f"{PROFILE_DIGEST}.json"
    ),
    "backend/internal/officialegress/catalogdata/runtime/release-catalog.json",
    (
        "backend/internal/officialegress/catalogdata/runtime/release-graphs/"
        f"{RELEASE_GRAPH_SHA256}.json"
    ),
    (
        "backend/internal/officialegress/catalogdata/runtime/snapshot-catalogs/"
        f"{SNAPSHOT_CATALOG_SHA256}.json"
    ),
    (
        "backend/internal/officialegress/"
        "codex_01491_r21_classification_fact_correction_transition_test.go"
    ),
    (
        "backend/internal/officialegress/"
        "codex_01491_r22_candidate_catalog_transition_test.go"
    ),
    (
        "backend/internal/officialegress/profilecontract/testdata/"
        "snapshot-catalog.json"
    ),
    (
        "backend/internal/officialegress/profilecontract/testdata/snapshots/"
        f"0.149.1/{PROFILE_DIGEST}.json"
    ),
    (
        "backend/internal/officialegress/releasecontract/testdata/"
        "release-graph.json"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r21_classification_fact_correction_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r22_candidate_catalog_transition.py"
    ),
]


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会遮蔽受管事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r22 候选 Catalog transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取非符号链接 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return value


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
    """读取 r22 基准提交中的普通 Git blob。"""

    completed = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def expected_campaign() -> dict[str, Any]:
    """返回冻结的分类批准坐标。"""

    return {
        "id": "c1491-r22-s8",
        "predecessor_campaign_id": "c1491-r20-s6",
        "purpose": "production_replacement",
        "target_version": "0.149.1",
        "classification_sha256": CLASSIFICATION_SHA256,
        "target_profile_id": "codex-0.149.1-official-r1491-v2",
        "target_profile_digest": PROFILE_DIGEST,
        "profile_payload_sha256": (
            "61d1db4b41ba97b9667678cb3cf219326d9ccb47c58c9ea2cae9e0e6160000d8"
        ),
        "required_rule_count": 42,
        "discovery_count": 2101,
        "unclassified_count": 0,
        "codex_account_ref": "#22",
        "api_key_ref": "#4",
    }


def expected_catalog_stage() -> dict[str, Any]:
    """返回机器 stage-profile 收据的冻结事实。"""

    return {
        "receipt_schema": "official-egress-catalog-stage/v1",
        "receipt_sha256": (
            "30374ad355cee7e79100172ef577ee9536dbdb861649c17ca1eb0a804443a0fb"
        ),
        "inventory_sha256": (
            "55d5493bd02073cbc69d4854e6821d64ad72ec5b132038858f96373dce3fb2a5"
        ),
        "active_version": "0.147.0",
        "active_profile_digest": (
            "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc"
        ),
        "active_release_digest": (
            "caa1948405136feaf159cbfdf3c164c056c1ea38cac6f87a007cfe69ead38707"
        ),
        "active_unchanged": True,
        "production_selector_changed": False,
        "candidate_release_mode": "previous",
        "candidate_release_digest": (
            "ebf5947e7630e4b70efe48d5b52435a1a8b54c4c8675ebc134519b22a9175e2f"
        ),
        "release_graph_sha256": RELEASE_GRAPH_SHA256,
        "snapshot_catalog_sha256": SNAPSHOT_CATALOG_SHA256,
        "runtime_profile_file_sha256": (
            "a8ec3ccf43748bd225a9bd753fb2a240d3f5667da76ccd344e370294178ec2df"
        ),
        "contract_snapshot_catalog_sha256": (
            "f2a38b220a5edb0ee3c2e4f734da7a9d4d91bf80d0d5c2f7805f8dbfb5d87c37"
        ),
    }


def validate_catalog_semantics() -> None:
    """验证 Active/Previous、画像并存和历史内容寻址文件。"""

    release_catalog = load_document(
        ROOT
        / "backend/internal/officialegress/catalogdata/runtime/"
        "release-catalog.json",
        "r22 release-catalog",
    )
    if release_catalog != {
        "schema_version": 1,
        "release_graph": {
            "path": (
                "catalogdata/runtime/release-graphs/"
                f"{RELEASE_GRAPH_SHA256}.json"
            ),
            "sha256": RELEASE_GRAPH_SHA256,
        },
        "snapshot_catalog": {
            "path": (
                "catalogdata/runtime/snapshot-catalogs/"
                f"{SNAPSHOT_CATALOG_SHA256}.json"
            ),
            "sha256": SNAPSHOT_CATALOG_SHA256,
        },
        "source": f"campaign:c1491-r22-s8/classification:{CLASSIFICATION_SHA256}",
    }:
        raise ValueError("r22 release-catalog 身份非法")

    release_graph = load_document(
        ROOT
        / "backend/internal/officialegress/releasecontract/testdata/"
        "release-graph.json",
        "r22 release graph",
    )
    nodes = release_graph.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 4:
        raise ValueError("r22 ReleaseGraph 节点闭集非法")
    active = [row for row in nodes if row.get("mode") == "active"]
    previous = [row for row in nodes if row.get("mode") == "previous"]
    if (
        len(active) != 2
        or len(previous) != 2
        or any(row.get("snapshot", {}).get("version") != "0.147.0" for row in active)
        or any(
            row.get("snapshot")
            != {"version": "0.149.1", "digest": PROFILE_DIGEST}
            for row in previous
        )
        or any(
            row.get("build", {}).get("source")
            != f"campaign:c1491-r22-s8/classification:{CLASSIFICATION_SHA256}"
            for row in previous
        )
    ):
        raise ValueError("r22 ReleaseGraph 的 Active/Previous 选择非法")

    snapshot_catalog = load_document(
        ROOT
        / "backend/internal/officialegress/profilecontract/testdata/"
        "snapshot-catalog.json",
        "r22 contract snapshot catalog",
    )
    identities = {
        (row.get("version"), row.get("digest"))
        for row in snapshot_catalog.get("snapshots", [])
        if isinstance(row, dict)
    }
    required = {
        ("0.145.0", "343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431"),
        ("0.145.0", "e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b"),
        ("0.147.0", "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc"),
        ("0.149.1", PROFILE_DIGEST),
        ("0.149.1", HISTORICAL_PROFILE_DIGEST),
    }
    if identities != required:
        raise ValueError("r22 SnapshotCatalog 未精确保留历史画像并追加 v2")

    historical_files = {
        (
            "backend/internal/officialegress/catalogdata/runtime/profiles/"
            f"0.149.1/{HISTORICAL_PROFILE_DIGEST}.json"
        ): "39e29520a4f10dc55c14f1b259c09d0058f3444d56824e5988850e4660e9123a",
        (
            "backend/internal/officialegress/catalogdata/runtime/profiles/"
            "0.145.0/343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431.json"
        ): "ff503d73d7402fb0d6429a67b415aa7d2bc863a3002cf5e5ee9ab3baa3d7529f",
        (
            "backend/internal/officialegress/catalogdata/runtime/profiles/"
            "0.145.0/e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b.json"
        ): "36c6c0e4464e6182347210d05d17ea85f6121e98f70f3c36b6ffc2b4230a5c66",
    }
    for relative, expected in historical_files.items():
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != expected:
            raise ValueError(f"r22 历史画像发生漂移：{relative}")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 r22 身份、Campaign、Catalog、路径闭集和安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "campaign",
        "catalog_stage",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r22 候选 Catalog transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r22-candidate-catalog-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r22-candidate-catalog"
        or document["framework_stage"] != "VC-3/CANDIDATE-CATALOG"
        or document["result"] != "r22_candidate_catalog_staged"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r22 候选 Catalog transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r22 候选 Catalog transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r21 分类事实纠正 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": sha256(PREDECESSOR_PATH.read_bytes()),
        "identity_sha256": predecessor.get("identity_sha256"),
    }:
        raise ValueError("r22 候选 Catalog transition 前序绑定非法")
    if document["campaign"] != expected_campaign():
        raise ValueError("r22 Campaign 或账号身份非法")
    if document["catalog_stage"] != expected_catalog_stage():
        raise ValueError("r22 stage-profile 收据事实非法")

    expected_verification = {
        "official_evidence_replayed": True,
        "classification_approved": True,
        "all_rules_mapped": True,
        "all_discoveries_mapped": True,
        "catalog_inventory_verified": True,
        "historical_profiles_rehashed": True,
        "active_selector_unchanged": True,
        "mutation_tests_required": True,
    }
    if document["verification"] != expected_verification:
        raise ValueError("r22 候选 Catalog 验证事实非法")
    expected_safety = {
        "historical_content_addressed_artifacts_overwritten": False,
        "historical_profile_8e59_preserved": True,
        "historical_0_145_profiles_preserved": True,
        "historical_receipts_modified": False,
        "historical_transitions_modified": False,
        "network_configuration_changed": False,
        "deployment_performed": False,
        "production_selector_changed": False,
        "production_activated": False,
        "official_recapture_performed": False,
        "codex_account_request_sent": False,
        "vircs_accessed": False,
    }
    if document["safety"] != expected_safety:
        raise ValueError("r22 候选 Catalog 安全边界非法")

    entries = document.get("transitions")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_PATHS):
        raise ValueError("r22 候选 Catalog transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if paths != EXPECTED_PATHS or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("r22 候选 Catalog transition 路径未排序或重复")
    path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if document["path_set_sha256"] != path_set:
        raise ValueError("r22 候选 Catalog transition 路径摘要不一致")

    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r22 候选 Catalog transition 条目字段非法")
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
        ):
            raise ValueError(f"r22 候选 Catalog transition 条目非法：{path}")

    validate_catalog_semantics()


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 r22 transition。"""

    document = load_document(TRANSITION_PATH, "r22 候选 Catalog transition")
    validate_transition(document)
    return document


def r22_supersedes(path: str, prior_digest: str, current_digest: str) -> bool:
    """只承认 r22 收据登记的精确 path/from/to 三元组。"""

    try:
        document = load_validated_transition()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and entry["to_sha256"] == current_digest
        and prior_digest in entry["predecessor_sha256s"]
        for entry in document["transitions"]
    )


class Codex01491R22CandidateCatalogTransitionTest(unittest.TestCase):
    def test_transition_身份制品和文件闭集可独立重放(self) -> None:
        validate_transition(load_document(TRANSITION_PATH, "r22 transition"))

    def test_transition_拒绝切换_active或伪造历史保留(self) -> None:
        document = load_document(TRANSITION_PATH, "r22 transition")
        active_mutation = copy.deepcopy(document)
        active_mutation["catalog_stage"]["active_unchanged"] = False
        active_mutation["identity_sha256"] = canonical_identity(active_mutation)
        with self.assertRaisesRegex(ValueError, "stage-profile"):
            validate_transition(active_mutation)

        history_mutation = copy.deepcopy(document)
        history_mutation["safety"]["historical_profile_8e59_preserved"] = False
        history_mutation["identity_sha256"] = canonical_identity(history_mutation)
        with self.assertRaisesRegex(ValueError, "安全边界"):
            validate_transition(history_mutation)

    def test_transition_精确后继三元组被承认(self) -> None:
        document = load_validated_transition()
        entry = next(row for row in document["transitions"] if row["change"] == "modified")
        self.assertTrue(
            r22_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(r22_supersedes(entry["path"], "0" * 64, entry["to_sha256"]))


if __name__ == "__main__":
    unittest.main()
