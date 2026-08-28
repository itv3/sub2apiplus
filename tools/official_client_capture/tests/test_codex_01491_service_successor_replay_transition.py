"""冻结 Codex CLI 0.149.1 service 校验器后继重放闭合收据。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "cd6adf31b510ab47e08386cedfa19b6eaad978f0"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-service-successor-replay-transition.json"
)
R15_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r15-formal-classification-transition.json"
)
R15_FILE_SHA256 = "206b3a7e6e25bf408d6b3c4fbdbaf6e420d1cf500cb132a8e830d5163a274048"
R15_IDENTITY_SHA256 = (
    "79996daa098909ace8b2332fd6c0bb5803087ff2585e6c482ae6a1ae7ac8ee36"
)
EXPECTED_CHAIN = [
    (
        "docs/egress/maintenance/codex-0.149.1-r16-successor-carry-forward-transition.json",
        "97286f9cfec8fe2e0c4c60b7ea962bfa7e98bdaa3b083fe736628e68719ef222",
        "bc8dd8444a5ba65ff30d0439a3e8937c4bf4ea9e2f0362a12b540789af1670c5",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r17-kilo-model-contract-transition.json",
        "67b78fc1f4db41dad476e3de3c83ea2ebabf3c33ff676502d1dda293ab8a3555",
        "b7b7534f89f9d79e66970d54a5124b82e795693e75b32c26f9bd7a926169396c",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r18-successor-account-transition.json",
        "0d3fe228ab2fe418e07a31e2f60e8fcf0a72d23b3b2033e6b248117d694de361",
        "39ab82fad57031021db3dd7e4a8dfb355317935cd4aac069b17554df593bdef8",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r19-successor-chain-transition.json",
        "34a6ced196f49fd38a940969662f8292d3417dbad5da6a15c5cc38f1421e0226",
        "6bf51f5a27d288833ba862ef6f91782aba17a2d6e6afdc8da200678b240ef14e",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r20-candidate-aux-transition.json",
        "d35fda7dd6a9250286821b5ce96a741ff650ded48a6005a78209008615db3d2d",
        "6b928fec7aebf5424b48b86bda21aa6e1f5fd5429f41c09bb7d5b72b2d370c06",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r21-classification-fact-correction-transition.json",
        "6d4195f248c12538363df71aefcd9ac4c1b98220effaa1024ff3c3c46515abc9",
        "aa67d69ff36b6bf8ff01fcbbf36779fdb18d28d2e882ae82ee332498f3e4be33",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r22-candidate-catalog-transition.json",
        "e24ad2b2b956bfaa985ead30850623e99428a0ea0a5b84a3e90b84843c94a292",
        "777fc13ced7f8a1ebb66d44864147dd4a86511bf4f0f76bd8fe2c336cbf05ad3",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r23-runtime-coordinate-transition.json",
        "7b2b3cc09dd56d095230243c0e77c3f38c97b256bbaca43fb50bd61d51fdb1b9",
        "c07a935f50e022a2469e234c71bf76d3e59272ac121698df7f33e6bf70106356",
    ),
    (
        "docs/egress/maintenance/codex-0.149.1-r24-selector-lite-coordinate-transition.json",
        "9d78ecb8851935f40be42866f46f83d097b974a8deba0abe44ab46d242fc39b4",
        "3fd50b27cd48f8a9df0b61e697cdf09f36673f765aa9ef81aed611c09fbed96d",
    ),
]
EXPECTED_PATHS = [
    (
        "backend/internal/officialegress/"
        "codex_01491_r24_selector_lite_coordinate_transition_test.go"
    ),
    (
        "backend/internal/officialegress/"
        "codex_01491_service_successor_replay_transition_test.go"
    ),
    "backend/internal/service/codex_01491_candidate_source_transition_test.go",
    (
        "backend/internal/service/"
        "codex_01491_r15_formal_classification_transition_test.go"
    ),
    (
        "backend/internal/service/"
        "codex_01491_service_successor_replay_transition_test.go"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_candidate_gate_successor_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_r24_selector_lite_coordinate_transition.py"
    ),
    (
        "tools/official_client_capture/tests/"
        "test_codex_01491_service_successor_replay_transition.py"
    ),
]
EXPECTED_PREDECESSORS = {
    EXPECTED_PATHS[0]: [
        "ab35846c004138b441e9e1b2a1045e78f09755c8ce0089f6eb4f5c222c62a4a9"
    ],
    EXPECTED_PATHS[2]: [
        "14adcc0c3ab4826dab7f0edf13bb94e4a2f6661651a802fe5c27a8770365cb54"
    ],
    EXPECTED_PATHS[3]: [
        "198e4ee6c30020868887a793dacea60649c6ea23f0816c7adf197e618a042a65"
    ],
    EXPECTED_PATHS[5]: [
        "23fe94c453166c079e5c1f1df8bfde7b0beba7e7bfec0c47118795b004c28948"
    ],
    EXPECTED_PATHS[6]: [
        "7f995f92d901d0f9d587242ca4ae7fabc2d6b0eaf937f06b6f4bcd68cad54099"
    ],
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会遮蔽后继事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"service 后继重放 transition 包含重复字段：{key}")
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


def validate_transition(document: dict[str, Any]) -> list[dict[str, Any]]:
    """重放收据身份、r15-r24 连续前序、文件闭集和安全边界。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "successor_chain",
        "path_set_sha256",
        "transitions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }:
        raise ValueError("service 后继重放 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-service-successor-replay-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-service-successor-replay"
        or document["framework_stage"]
        != "VC-3/VC-4/HISTORICAL-VALIDATOR-CLOSURE"
        or document["result"] != "service_successor_replay_transition_frozen"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("service 后继重放 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("service 后继重放 transition 时间非法") from error
    if document["verification"] != {
        "r15_receipt_identity_verified": True,
        "successor_receipt_identity_verified": True,
        "predecessor_chain_verified": True,
        "transition_graph_replayed": True,
        "multi_hop_replay_tested": True,
        "mutation_tests_required": True,
    }:
        raise ValueError("service 后继重放 transition 验证事实非法")
    if document["safety"] != {
        "historical_artifacts_overwritten": False,
        "historical_receipts_modified": False,
        "official_recapture_performed": False,
        "candidate_capture_performed": False,
        "deployment_performed": False,
        "network_configuration_changed": False,
        "production_selector_changed": False,
        "production_activated": False,
        "arm64_accessed": False,
        "vircs_accessed": False,
        "dmit_server_accessed": False,
    }:
        raise ValueError("service 后继重放 transition 安全边界非法")

    r15 = load_document(R15_PATH, "r15 Formal 分类 transition")
    if (
        sha256(R15_PATH.read_bytes()) != R15_FILE_SHA256
        or r15.get("identity_sha256") != R15_IDENTITY_SHA256
        or canonical_identity(r15) != R15_IDENTITY_SHA256
    ):
        raise ValueError("service 后继重放 r15 历史收据非法")
    chain = document.get("successor_chain")
    if not isinstance(chain, list) or len(chain) != len(EXPECTED_CHAIN):
        raise ValueError("service 后继重放 r16-r24 绑定数量非法")
    predecessor = {
        "path": R15_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": R15_FILE_SHA256,
        "identity_sha256": R15_IDENTITY_SHA256,
    }
    historical_documents = [r15]
    for binding, (path_value, file_digest, identity_digest) in zip(
        chain,
        EXPECTED_CHAIN,
        strict=True,
    ):
        path = ROOT / path_value
        historical = load_document(path, f"后继收据 {path_value}")
        if (
            binding.get("path") != path_value
            or binding.get("file_sha256") != file_digest
            or binding.get("identity_sha256") != identity_digest
            or sha256(path.read_bytes()) != file_digest
            or historical.get("identity_sha256") != identity_digest
            or canonical_identity(historical) != identity_digest
            or binding.get("schema_version") != historical.get("schema_version")
            or binding.get("scope") != historical.get("scope")
            or binding.get("result") != historical.get("result")
            or historical.get("predecessor_transition") != predecessor
        ):
            raise ValueError(f"service 后继重放连续前序非法：{path_value}")
        predecessor = {
            "path": path_value,
            "file_sha256": file_digest,
            "identity_sha256": identity_digest,
        }
        historical_documents.append(historical)

    entries = document.get("transitions")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_PATHS):
        raise ValueError("service 后继重放 transition 文件闭集非法")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if paths != EXPECTED_PATHS or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("service 后继重放 transition 路径未排序或重复")
    path_set = sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if document["path_set_sha256"] != path_set:
        raise ValueError("service 后继重放 transition 路径摘要非法")
    for entry in entries:
        path_value = entry["path"]
        expected_predecessors = EXPECTED_PREDECESSORS.get(path_value, [])
        expected_change = "modified" if expected_predecessors else "added"
        current = ROOT / path_value
        if (
            set(entry)
            != {
                "path",
                "change",
                "predecessor_sha256s",
                "to_sha256",
                "reason",
            }
            or entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"service 后继重放 transition 条目非法：{path_value}")
    return [*historical_documents, document]


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """读取并完整重放 service 后继闭合收据。"""

    document = load_document(TRANSITION_PATH, "service 后继重放 transition")
    validate_transition(document)
    return document


@lru_cache(maxsize=None)
def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r15-r24 与本次闭合收据固定的精确摘要可达图。"""

    if prior_digest == current_digest:
        return False
    try:
        document = load_validated_transition()
        historical_documents = validate_transition(document)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    edges: dict[str, list[str]] = {}
    for receipt in historical_documents:
        entries = receipt.get("transitions")
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("path") != path:
                continue
            target = entry.get("to_sha256")
            predecessors = entry.get("predecessor_sha256s")
            if not isinstance(target, str) or not isinstance(predecessors, list):
                return False
            for predecessor_digest in predecessors:
                if isinstance(predecessor_digest, str):
                    edges.setdefault(predecessor_digest, []).append(target)
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


class Codex01491ServiceSuccessorReplayTransitionTest(unittest.TestCase):
    def test_transition_身份链与文件闭集可独立重放(self) -> None:
        validate_transition(load_document(TRANSITION_PATH, "service 后继收据"))

    def test_transition_拒绝安全边界和未知摘要(self) -> None:
        document = load_document(TRANSITION_PATH, "service 后继收据")
        safety_mutation = copy.deepcopy(document)
        safety_mutation["safety"]["network_configuration_changed"] = True
        safety_mutation["identity_sha256"] = canonical_identity(safety_mutation)
        with self.assertRaisesRegex(ValueError, "安全边界非法"):
            validate_transition(safety_mutation)

        entry = next(row for row in document["transitions"] if row["change"] == "modified")
        self.assertTrue(
            transition_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            transition_supersedes(entry["path"], "0" * 64, entry["to_sha256"])
        )


if __name__ == "__main__":
    unittest.main()
