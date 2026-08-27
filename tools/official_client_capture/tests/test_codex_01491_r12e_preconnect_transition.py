"""冻结 Codex CLI 0.149.1 r12e 上游 TLS 预连接修复后继 transition。"""

from __future__ import annotations

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


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "48f851d415442b59d53d507e7a8a23218572439a"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r12e-preconnect-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11c-models-sync-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r12e_preconnect_transition.py"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/"
        "codex_01491_r11c_models_sync_transition_test.go",
        "backend/internal/officialegress/"
        "codex_01491_r12e_preconnect_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r11c_models_sync_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r12e_preconnect_transition_test.go",
        "tools/official_client_capture/run_official_relay_scenario.sh",
        "tools/official_client_capture/upstream_byte_relay.py",
        "tools/official_client_capture/tests/"
        "test_codex_01491_r11c_models_sync_transition.py",
        SELF_PATH,
        "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
        "tools/official_client_capture/tests/test_upstream_byte_relay.py",
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


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝可覆盖诊断或在线验证事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r12e 预连接 transition 包含重复字段：{key}")
        result[key] = value
    return result


def load_document(path: Path, label: str) -> dict[str, Any]:
    """严格读取非符号链接的普通 JSON 对象。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return document


@lru_cache(maxsize=None)
def commit_blob(path: str) -> bytes | None:
    """读取 r11c 基线提交中的普通 Git blob；不存在时返回 None。"""

    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_transition() -> dict[str, Any]:
    """读取 r12e 预连接 transition。"""

    return load_document(TRANSITION_PATH, "r12e 预连接 transition")


def _expected_boundaries() -> dict[str, Any]:
    return {
        "api_key_ref": "#4",
        "capture_account_ref": "#21",
        "capture_account_20_allowed": False,
        "capture_account_20_used": False,
        "service_private_ip": "172.25.0.3",
        "service_default_gateway": "172.25.0.1",
        "capture_private_ip": "172.30.0.10",
        "capture_default_gateway": "172.30.0.1",
        "required_public_egress": "179.255.100.158",
        "docker_network_changed": False,
        "route_nat_iptables_changed": False,
        "hosts_baseline_changed": False,
        "ca_baseline_changed": False,
        "proxy_or_compose_network_changed": False,
        "production_selector_changed": False,
        "historical_evidence_preserved_read_only": True,
        "vircs_accessed": False,
    }


def _validate_failed_validation(document: dict[str, Any]) -> None:
    failed = document["failed_validation"]
    if (
        failed["run_id"]
        != "codex-01491-r12e-model-only-main-20260827T043514Z"
        or failed["started_at_utc"] != "2026-08-27T04:35:14Z"
        or failed["finished_at_utc"] != "2026-08-27T04:38:06Z"
        or failed["duration_seconds"] != 172
        or failed["preconnect_duration_ms"] != 4890.012
        or failed["upstream_map_ip"] != "104.18.32.47"
        or failed["default_upstream_ip"] != "172.64.155.209"
        or failed["upstream_preconnected"] is not False
        or failed["started_attempt_count"] != 2
        or failed["captured_response_bytes"] != [1369, 1369]
        or failed["model_catalog_completed"] is not False
        or failed["remaining_retries_stopped"] is not True
        or failed["environment_restored"] is not True
        or failed["raw_evidence_scrubbed"] is not True
        or failed["evidence_reuse_forbidden"] is not True
    ):
        raise ValueError("r12e 预连接 transition 失败验证事实非法")
    expected_artifacts = [
        (
            "failed_relay_manifest",
            "bcc2ffb11492bcab3bf066a4a7fab2c4f92ce72b58ac0d1816292a88b27a3a79",
            2618,
        ),
        (
            "failed_models_response_1_scrubbed",
            "7e607c11a93eb0c802722838e8e9cba8ca876b77140b9a089045d483231f7227",
            1369,
        ),
        (
            "failed_models_response_2_scrubbed",
            "99bf4522007d1db70e2aff0e9409b4e1514cab6c9a2a012b294b4f4da3e104db",
            1369,
        ),
    ]
    observed = [
        (entry["role"], entry["sha256"], entry["bytes"])
        for entry in failed["artifacts"]
    ]
    if observed != expected_artifacts:
        raise ValueError("r12e 预连接 transition 失败证据绑定非法")


def _validate_online(document: dict[str, Any]) -> None:
    online = document["online_verification"]
    if online.get("all_tracks_passed") is not True:
        raise ValueError("r12e 预连接 transition 在线总门禁非法")
    tracks = online.get("tracks")
    if not isinstance(tracks, list) or len(tracks) != 2:
        raise ValueError("r12e 预连接 transition 在线轨道数量非法")
    expected = [
        {
            "track": "main",
            "run_id": "codex-01491-r12e2-model-only-main-20260827T044114Z",
            "started_at_utc": "2026-08-27T04:41:14Z",
            "finished_at_utc": "2026-08-27T04:41:24Z",
            "duration_seconds": 10,
            "model": "gpt-5.5",
            "use_responses_lite": False,
            "preconnect_duration_ms": 5483.464,
            "response_total_bytes": 362549,
            "catalog_bytes": 594,
            "catalog_sha256": (
                "5c24a07219cb6cb64fff100efb89cf157a5842022078fc39e8e0898e254f4084"
            ),
            "relay_bytes": 9515,
            "relay_sha256": (
                "0ccb102b10ffde77ec66815367d93b18eb331ad2affc2ca589985b57a4f2e5e4"
            ),
            "response_sha256": (
                "d24c08fa568fbfe0983987fc1e2ae2c3a582a0b87b6b9992d593f7b58b554784"
            ),
        },
        {
            "track": "lite",
            "run_id": "codex-01491-r12e2-model-only-lite-20260827T044255Z",
            "started_at_utc": "2026-08-27T04:42:55Z",
            "finished_at_utc": "2026-08-27T04:43:06Z",
            "duration_seconds": 11,
            "model": "gpt-5.6-luna",
            "use_responses_lite": True,
            "preconnect_duration_ms": 5148.836,
            "response_total_bytes": 362547,
            "catalog_bytes": 597,
            "catalog_sha256": (
                "f72573759756bf88b31632937ab3fcbeb4490fd9938d885d6bf6e42de149a9fc"
            ),
            "relay_bytes": 9818,
            "relay_sha256": (
                "995e02e09ac3a8e8fd0d1d67acda230107d3a48e6b8a27b6d94a6f3040467228"
            ),
            "response_sha256": (
                "80d6875a319358f42515e40b1bef3d6f0930bee9b43bf5cb30173f3c925a8307"
            ),
        },
    ]
    for track, facts in zip(tracks, expected, strict=True):
        if (
            track["track"] != facts["track"]
            or track["run_id"] != facts["run_id"]
            or track["started_at_utc"] != facts["started_at_utc"]
            or track["finished_at_utc"] != facts["finished_at_utc"]
            or track["duration_seconds"] != facts["duration_seconds"]
            or track["model"] != facts["model"]
            or track["use_responses_lite"] is not facts["use_responses_lite"]
            or track["preconnect_duration_ms"] != facts["preconnect_duration_ms"]
            or track["upstream_preconnected"] is not True
            or track["request_method"] != "GET"
            or track["request_path"]
            != "/backend-api/codex/models?client_version=0.149.1"
            or track["response_status"] != 200
            or track["response_content_length"] != 360785
            or track["response_total_bytes"] != facts["response_total_bytes"]
            or track["catalog_model_count"] != 9
            or track["models_request_count"] != 1
            or track["responses_request_count"] != 0
            or track["credential_replacement_count"] != 9
            or track["residual_credential_count"] != 0
        ):
            raise ValueError("r12e 预连接 transition 在线轨道事实非法")
        artifacts = {entry["role"]: entry for entry in track["artifacts"]}
        if (
            artifacts["model_catalog_prewarm"]["sha256"]
            != facts["catalog_sha256"]
            or artifacts["model_catalog_prewarm"]["bytes"]
            != facts["catalog_bytes"]
            or artifacts["relay_manifest"]["sha256"] != facts["relay_sha256"]
            or artifacts["relay_manifest"]["bytes"] != facts["relay_bytes"]
            or artifacts["models_request_scrubbed"]["sha256"]
            != "65b4cf4bf030ad023bbbe51b623f31a803c5b24ad4ad8133ce555c22a85acb6c"
            or artifacts["models_request_scrubbed"]["bytes"] != 1967
            or artifacts["models_response_scrubbed"]["sha256"]
            != facts["response_sha256"]
            or artifacts["models_response_scrubbed"]["bytes"]
            != facts["response_total_bytes"]
        ):
            raise ValueError("r12e 预连接 transition 在线证据绑定非法")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 DNS 选址根因、双轨慢 TLS 成功、恢复事实和文件摘要闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "failed_validation",
        "diagnosis",
        "repair_facts",
        "online_verification",
        "restoration",
        "timing",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r12e 预连接 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r12e-preconnect-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r12e-preconnect"
        or document["framework_stage"] != "VC-0/P0-R12E-PRECONNECT"
        or document["result"]
        != "r12e_preconnect_repair_verified_new_campaign_required"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r12e 预连接 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r12e 预连接 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r11c 模型目录 transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": (
            "ce4f55368f4821278971ffd23321ef2ef1dac8a95e09b27a6db6cc1158f3ca5f"
        ),
        "identity_sha256": (
            "916041cf4e953b21a44a693b5941681046265dd849294daecb6801a3ea6e0245"
        ),
    }:
        raise ValueError("r12e 预连接 transition 前序绑定非法")
    if (
        predecessor.get("identity_sha256")
        != document["predecessor_transition"]["identity_sha256"]
    ):
        raise ValueError("r12e 预连接 transition 前序自摘要非法")
    if document["boundaries"] != _expected_boundaries():
        raise ValueError("r12e 预连接 transition 账号或网络边界非法")

    _validate_failed_validation(document)
    if document["diagnosis"] != {
        "codex_models_total_timeout_seconds": 5,
        "dmit_tls_slow_path_observed": True,
        "dmit_tls_slowest_observed_ms": 5483.464,
        "duplicate_dns_resolution_returned_different_ips": True,
        "exact_route_ip_mismatch": True,
        "preconnect_silently_fell_back": True,
        "account_or_quota_root_cause": False,
        "network_configuration_root_cause": False,
        "dmit_egress_latency_contributed": True,
        "complete_recapture_required": False,
        "archived_capture_sha256": (
            "b76b8033b2ac71e3beda404c57b52d006bd7bf8901fc2791b1031cb2e5b705d7"
        ),
    }:
        raise ValueError("r12e 预连接 transition 根因事实非法")
    if document["repair_facts"] != {
        "relay_preconnect_before_codex_timer": True,
        "default_upstream_ip_source": "--upstream-ip",
        "preconnect_and_client_route_use_same_ip": True,
        "exact_route_match_required": True,
        "preconnected_connection_consumed_once": True,
        "request_or_response_bytes_modified": False,
        "model_catalog_only_mode": True,
        "responses_requests_sent": False,
        "official_debug_models_used": True,
        "raw_models_http_200_required": True,
        "incomplete_response_fails_closed": True,
        "network_configuration_changed": False,
        "source_commit": "e5b5a0e6ad494ffe301a8c88e875ae185fee83c5",
        "new_campaign_required": True,
    }:
        raise ValueError("r12e 预连接 transition 修复事实非法")
    _validate_online(document)

    restoration = document["restoration"]
    if (
        restoration["capture_hosts_sha256_before"]
        != "658a0f13912d524aaf3e682eedfbd2fe1e75f10e0edd2c3292468429ad2bfd23"
        or restoration["capture_hosts_sha256_after"]
        != restoration["capture_hosts_sha256_before"]
        or restoration["capture_ca_sha256_before"]
        != "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd"
        or restoration["capture_ca_sha256_after"]
        != restoration["capture_ca_sha256_before"]
        or restoration["capture_auth_sha256_before"]
        != "8a3ca59c39a59be0e211b4fbee14096b0bb87515c8ae84b704f01fd6a57ab5cf"
        or restoration["capture_auth_sha256_after"]
        != restoration["capture_auth_sha256_before"]
        or restoration["capture_public_egress_after"] != "179.255.100.158"
        or restoration["service_public_egress_after"] != "179.255.100.158"
        or restoration["temporary_ca_absent"] is not True
        or restoration["chatgpt_hosts_override_absent"] is not True
        or restoration["relay_process_absent"] is not True
        or restoration["relay_port_443_free"] is not True
        or restoration["login_status_preserved"] is not True
    ):
        raise ValueError("r12e 预连接 transition 环境恢复事实非法")
    timing = document["timing"]
    if (
        not isinstance(timing, list)
        or len(timing) != 14
        or sum(entry["duration_seconds"] for entry in timing) != 996
        or timing[3]["result"] != "failed_route_mismatch"
        or timing[10]["result"]
        != "failed_transition_validation_repetition_timeout"
        or timing[-1]["result"] != "passed"
    ):
        raise ValueError("r12e 预连接 transition 用时记录非法")
    if document["verification"] != {
        "python_compile_passed": True,
        "bash_syntax_passed": True,
        "targeted_python_test_count": 63,
        "targeted_python_tests_passed": True,
        "network_gate_passed": True,
        "main_online_models_passed": True,
        "lite_online_models_passed": True,
        "failed_evidence_scrubbed": True,
        "historical_artifacts_unchanged": True,
        "new_formal_campaign_required": True,
    }:
        raise ValueError("r12e 预连接 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r12e 预连接 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r12e 预连接 transition 条目字段非法")
        path = entry["path"]
        before = commit_blob(path)
        expected_change = "added" if before is None else "modified"
        expected_predecessors = [] if before is None else [sha256(before)]
        current = ROOT / path
        if (
            entry["change"] != expected_change
            or entry["predecessor_sha256s"] != expected_predecessors
            or current.is_symlink()
            or not current.is_file()
            or (
                entry["to_sha256"] != sha256(current.read_bytes())
                and not r13_candidate_coordinate_supersedes(
                    path,
                    entry["to_sha256"],
                    sha256(current.read_bytes()),
                )
            )
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r12e 预连接 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"r12e 预连接 transition 命中历史只读路径：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """加载并完整重放 r12e 与 r13 后继 transition。"""

    document = load_transition()
    validate_transition(document)
    from tools.official_client_capture.tests import (
        test_codex_01491_r13_candidate_coordinate_transition as r13_coordinate,
    )

    successor = r13_coordinate.load_validated_transition()
    return {
        **document,
        "transitions": [
            *document["transitions"],
            *successor["transitions"],
        ],
    }


def r13_candidate_coordinate_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """延迟加载 r13，避免维护链模块初始化形成循环依赖。"""

    from tools.official_client_capture.tests import (
        test_codex_01491_r13_candidate_coordinate_transition as r13_coordinate,
    )

    try:
        successor = r13_coordinate.load_validated_transition()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    return r13_coordinate.transition_supersedes(
        successor,
        path,
        prior_digest,
        current_digest,
    )


def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """按登记路径图重放 r12e 及其后继的精确摘要链。"""

    if r15_supersedes(path, prior_digest, current_digest):
        return True

    try:
        document = load_validated_transition()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    edges: dict[str, list[str]] = {}
    for entry in document["transitions"]:
        if entry["path"] != path:
            continue
        for predecessor in entry["predecessor_sha256s"]:
            edges.setdefault(predecessor, []).append(entry["to_sha256"])

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


class Codex01491R12EPreconnectTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝账号网络与伪预连接篡改(self) -> None:
        document = load_transition()
        document["boundaries"]["capture_account_ref"] = "#20"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["online_verification"]["tracks"][0][
            "upstream_preconnected"
        ] = False
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "在线轨道事实"):
            validate_transition(document)

        document = load_transition()
        document["repair_facts"]["request_or_response_bytes_modified"] = True
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "修复事实"):
            validate_transition(document)

    def test_transition_只承认精确后继边(self) -> None:
        entry = next(
            item
            for item in load_transition()["transitions"]
            if item["predecessor_sha256s"]
        )
        self.assertTrue(
            transition_supersedes(
                entry["path"],
                entry["predecessor_sha256s"][0],
                entry["to_sha256"],
            )
        )
        self.assertFalse(
            transition_supersedes(
                entry["path"],
                "0" * 64,
                entry["to_sha256"],
            )
        )


if __name__ == "__main__":
    unittest.main()
