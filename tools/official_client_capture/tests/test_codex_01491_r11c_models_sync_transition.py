"""冻结 Codex CLI 0.149.1 r11c 在线模型目录修复后继 transition。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "87de87becf648148e02fe44ec5d9d91afdc1a708"
TRANSITION_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11c-models-sync-transition.json"
)
PREDECESSOR_PATH = (
    ROOT
    / "docs/egress/maintenance/"
    "codex-0.149.1-r11b-relay-completion-transition.json"
)
SELF_PATH = (
    "tools/official_client_capture/tests/"
    "test_codex_01491_r11c_models_sync_transition.py"
)
EXPECTED_PATHS = sorted(
    {
        "backend/internal/officialegress/"
        "codex_01491_r11c_models_sync_transition_test.go",
        "backend/internal/officialegress/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r11c_models_sync_transition_test.go",
        "backend/internal/service/"
        "codex_01491_r9_contamination_recovery_transition_test.go",
        "tools/official_client_capture/codex_upgrade_scenarios_0_149_1.json",
        "tools/official_client_capture/drive_codex_model_catalog.py",
        "tools/official_client_capture/run_official_relay_scenario.sh",
        "tools/official_client_capture/tests/"
        "test_codex_01491_r11a_harness_transition.py",
        "tools/official_client_capture/tests/"
        "test_codex_01491_r11b_relay_completion_transition.py",
        SELF_PATH,
        "tools/official_client_capture/tests/test_model_catalog_prewarm.py",
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
    """拒绝会覆盖在线模型目录事实的重复 JSON 字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"r11c 模型目录 transition 包含重复字段：{key}")
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
    """读取 r11c 在线模型目录 transition。"""

    return load_document(TRANSITION_PATH, "r11c 模型目录 transition")


def validate_transition(document: dict[str, Any]) -> None:
    """重放 TLS 根因、双模型条件、固定网络与文件摘要闭集。"""

    if set(document) != {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "framework_stage",
        "predecessor_transition",
        "boundaries",
        "failure_facts",
        "repair_facts",
        "online_verification",
        "restoration",
        "transitions",
        "verification",
        "result",
        "identity_sha256",
    }:
        raise ValueError("r11c 模型目录 transition 顶层字段非法")
    if (
        document["schema_version"]
        != "official-client-codex-0.149.1-r11c-models-sync-transition/v1"
        or document["base_commit"] != BASE_COMMIT
        or document["scope"] != "codex-0.149.1-r11c-models-sync"
        or document["framework_stage"] != "VC-0/P0-R11C-MODELS-SYNC"
        or document["result"] != "r11c_models_sync_repair_verified"
        or document["identity_sha256"] != canonical_identity(document)
    ):
        raise ValueError("r11c 模型目录 transition 身份非法")
    try:
        datetime.fromisoformat(document["issued_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("r11c 模型目录 transition 时间非法") from error

    predecessor = load_document(PREDECESSOR_PATH, "r11b relay transition")
    if document["predecessor_transition"] != {
        "path": PREDECESSOR_PATH.relative_to(ROOT).as_posix(),
        "file_sha256": (
            "207bc45953007446a24ddd385fd3533102a2fa60ef95e3c31ce1238a885222d6"
        ),
        "identity_sha256": (
            "f52ba06b86b756b6a3e4c89e043e41f1210e8c43614859af9fb8ecf39d5c49b6"
        ),
    }:
        raise ValueError("r11c 模型目录 transition 前序绑定非法")
    if predecessor.get("identity_sha256") != document["predecessor_transition"][
        "identity_sha256"
    ]:
        raise ValueError("r11c 模型目录 transition 前序自摘要非法")

    if document["boundaries"] != {
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
        "proxy_or_compose_network_changed": False,
        "production_selector_changed": False,
        "historical_evidence_preserved_read_only": True,
        "vircs_accessed": False,
    }:
        raise ValueError("r11c 模型目录 transition 账号或网络边界非法")
    if document["failure_facts"] != {
        "mitmproxy_upstream_tls_handshake_failed": True,
        "direct_tls_control_passed": True,
        "mitm_http1_and_http2_both_failed": True,
        "failure_segment": "mitmproxy_to_chatgpt.com",
        "client_to_mitm_tls_failed": False,
        "complete_recapture_required": False,
    }:
        raise ValueError("r11c 模型目录 transition TLS 根因事实非法")
    if document["repair_facts"] != {
        "official_debug_models_used": True,
        "isolated_codex_home_used": True,
        "bundled_catalog_forbidden": True,
        "built_in_openai_provider_preserved": True,
        "raw_models_http_200_required": True,
        "model_catalog_only_mode": True,
        "responses_requests_sent": False,
        "byte_relay_upstream_tls_used": True,
        "incomplete_response_fails_closed": True,
        "system_package_installed": False,
        "network_configuration_changed": False,
    }:
        raise ValueError("r11c 模型目录 transition 修复事实非法")

    online = document["online_verification"]
    expected_online = {
        "run_id": "codex-01491-r11c-models-sync-20260827T025700Z",
        "started_at_utc": "2026-08-27T02:56:38Z",
        "finished_at_utc": "2026-08-27T02:58:47Z",
        "duration_seconds": 129,
        "attempt_count": 2,
        "first_attempt_response_bytes": 1369,
        "successful_connection_id": 2,
        "request_method": "GET",
        "request_path": "/backend-api/codex/models?client_version=0.149.1",
        "response_status": 200,
        "response_content_length": 360785,
        "response_total_bytes": 362553,
        "catalog_model_count": 9,
        "model_conditions": [
            {"model": "gpt-5.5", "use_responses_lite": False},
            {"model": "gpt-5.6-luna", "use_responses_lite": True},
        ],
        "relay_connection_count": 2,
        "relay_valid_connection_count": 2,
        "credential_replacement_count": 16,
        "residual_credential_count": 0,
        "artifacts": [
            {
                "role": "model_catalog_prewarm",
                "path": (
                    "/root/oauth-capture/runs/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/"
                    "model-catalog-prewarm.json"
                ),
                "sha256": (
                    "9b2b195ca303e04070088e482087bb0206fd71f620edcdd32c2021457a3553d4"
                ),
                "bytes": 594,
            },
            {
                "role": "relay_manifest",
                "path": (
                    "/root/oauth-capture/runs/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/relay/relay.json"
                ),
                "sha256": (
                    "a0f6784afee87abb41299ce822a6819cf15832a4902b3a5ea23e2a1e01dd626f"
                ),
                "bytes": 13864,
            },
            {
                "role": "models_request_scrubbed",
                "path": (
                    "/root/oauth-capture/runs/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/relay/"
                    "conn002.client_to_upstream.bin"
                ),
                "sha256": (
                    "65b4cf4bf030ad023bbbe51b623f31a803c5b24ad4ad8133ce555c22a85acb6c"
                ),
                "bytes": 1967,
            },
            {
                "role": "models_response_scrubbed",
                "path": (
                    "/root/oauth-capture/runs/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/relay/"
                    "conn002.upstream_to_client.bin"
                ),
                "sha256": (
                    "468faa78f994964ddaa5ad7b427f8501d5183d9f42bc6d25c0d3eeaea0845788"
                ),
                "bytes": 362553,
            },
            {
                "role": "run_log",
                "path": (
                    "/root/oauth-capture/diagnostics/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/run.log"
                ),
                "sha256": (
                    "981da85f19fb81f54f6f6c475d7c0d5ee902fddd1ef1d7ae806dd61d6e1ef57e"
                ),
                "bytes": 2952,
            },
            {
                "role": "timing_log",
                "path": (
                    "/root/oauth-capture/diagnostics/"
                    "codex-01491-r11c-models-sync-20260827T025700Z/timing.log"
                ),
                "sha256": (
                    "7504dcffe3708c417fec9e9e29cf265fda1571ee82338b299ae55ac7b21805a1"
                ),
                "bytes": 85,
            },
        ],
    }
    if online != expected_online:
        raise ValueError("r11c 模型目录 transition 在线验证事实非法")

    if document["restoration"] != {
        "capture_hosts_sha256_before": (
            "658a0f13912d524aaf3e682eedfbd2fe1e75f10e0edd2c3292468429ad2bfd23"
        ),
        "capture_hosts_sha256_after": (
            "658a0f13912d524aaf3e682eedfbd2fe1e75f10e0edd2c3292468429ad2bfd23"
        ),
        "capture_ca_sha256_before": (
            "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd"
        ),
        "capture_ca_sha256_after": (
            "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd"
        ),
        "capture_public_egress_after": "179.255.100.158",
        "service_public_egress_after": "179.255.100.158",
        "temporary_ca_absent": True,
        "chatgpt_hosts_override_absent": True,
        "relay_process_absent": True,
        "relay_port_443_free": True,
        "login_status_preserved": True,
    }:
        raise ValueError("r11c 模型目录 transition 环境恢复事实非法")
    if document["verification"] != {
        "targeted_python_tests_passed": True,
        "capture_tool_tests_passed": True,
        "egress_spec_passed": True,
        "go_transition_tests_passed": True,
        "network_gate_passed": True,
        "online_models_http_200_passed": True,
        "historical_artifacts_unchanged": True,
    }:
        raise ValueError("r11c 模型目录 transition 门禁未闭合")

    entries = document["transitions"]
    paths = [entry.get("path") for entry in entries]
    if paths != EXPECTED_PATHS or len(paths) != len(set(paths)):
        raise ValueError("r11c 模型目录 transition 路径闭集非法")
    for entry in entries:
        if set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise ValueError("r11c 模型目录 transition 条目字段非法")
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
            or entry["to_sha256"] != sha256(current.read_bytes())
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError(f"r11c 模型目录 transition 条目非法：{path}")
        if path.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"r11c 模型目录 transition 命中历史只读路径：{path}")


@lru_cache(maxsize=1)
def load_validated_transition() -> dict[str, Any]:
    """加载并完整重放 r11c 在线模型目录 transition。"""

    document = load_transition()
    validate_transition(document)
    return document


def transition_supersedes(
    path: str,
    prior_digest: str,
    current_digest: str,
) -> bool:
    """只承认 r11c transition 登记的精确摘要边。"""

    try:
        document = load_validated_transition()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        entry["path"] == path
        and prior_digest in entry["predecessor_sha256s"]
        and current_digest == entry["to_sha256"]
        for entry in document["transitions"]
    )


class Codex01491R11CModelsSyncTransitionTest(unittest.TestCase):
    def test_transition_身份与文件闭集可独立重放(self) -> None:
        validate_transition(load_transition())

    def test_transition_拒绝账号网络与伪在线成功篡改(self) -> None:
        document = load_transition()
        document["boundaries"]["capture_account_ref"] = "#20"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["boundaries"]["required_public_egress"] = "127.0.0.1"
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "账号或网络边界"):
            validate_transition(document)

        document = load_transition()
        document["repair_facts"]["raw_models_http_200_required"] = False
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "修复事实"):
            validate_transition(document)

        document = load_transition()
        document["online_verification"]["model_conditions"][1][
            "use_responses_lite"
        ] = False
        document["identity_sha256"] = canonical_identity(document)
        with self.assertRaisesRegex(ValueError, "在线验证事实"):
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
