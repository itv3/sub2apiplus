"""Codex 升级控制收据测试使用的纯合成事实构造器。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def create_arm_receipt(
    root: Path,
    *,
    phase: str,
    subject_id: str,
    prefix: str,
    continuity_seed: str = "a",
) -> Path:
    """写入不联网的合成 ARM64 facts，并经正式 finalizer 封存。"""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    containers = []
    for index, name in enumerate(sorted(arm.CONTAINER_CONTRACTS), 1):
        expected = arm.CONTAINER_CONTRACTS[name]
        selected = {
            "name": expected["network"],
            "network_id": continuity_seed * 64,
            "endpoint_id": str(index) * 64,
            "ipv4_address": expected["ipv4_address"],
            "gateway": expected["gateway"],
        }
        containers.append(
            {
                "name": name,
                "container_id": str(index) * 64,
                "image_id": f"sha256:{str(index + 2) * 64}",
                "selected_network": selected,
                "network_bindings": [selected],
                "default_route": {
                    "interface": "eth0",
                    "gateway": expected["gateway"],
                },
                "public_egress": {
                    "url": arm.PUBLIC_EGRESS_URL,
                    "ip_address": arm.EXPECTED_PUBLIC_EGRESS,
                    "response_sha256": "e" * 64,
                },
                "raw_sha256": {
                    "docker_inspect": "f" * 64,
                    "proc_net_route": "0" * 64,
                },
            }
        )
    tool = Path(arm.__file__).resolve()
    facts = {
        "schema_version": arm.FACTS_SCHEMA,
        "phase": phase,
        "subject_id": subject_id,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contract_sha256": arm.contract_sha256(),
        "host": {"hostname": "arm64-test", "architecture": "linux/arm64"},
        "root_filesystem": {
            "mountpoint": "/",
            "total_bytes": 100 * 1024 * 1024 * 1024,
            "used_bytes": 40 * 1024 * 1024 * 1024,
            "available_bytes": 60 * 1024 * 1024 * 1024,
            "used_percent": 40,
        },
        "wireguard": {
            "interface": arm.WIREGUARD_INTERFACE,
            "configured_mtu": arm.EXPECTED_DMIT_WG1_MTU,
            "runtime_mtu": arm.EXPECTED_DMIT_WG1_MTU,
            "expected_dmit_mtu": arm.EXPECTED_DMIT_WG1_MTU,
            "config_path": str(arm.WIREGUARD_CONFIG),
            "config_sha256": "d" * 64,
        },
        "containers": containers,
        "collector": {
            "schema_version": arm.PRODUCER_SCHEMA,
            "tool": str(tool),
            "tool_sha256": hashlib.sha256(tool.read_bytes()).hexdigest(),
            "version": arm.PRODUCER_VERSION,
        },
    }
    facts_name = f"{prefix}-facts.json"
    receipt_name = f"{prefix}-receipt.json"
    _write(root / facts_name, facts)
    arm.finalize(root, facts_name, receipt_name)
    return root / receipt_name


def create_timing_checkpoint(
    root: Path,
    *,
    upgrade_id: str,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
) -> Path:
    """创建处于 VC-0 active 的合成 UpgradeTimingLedger checkpoint。"""

    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.parent.chmod(0o700)
    timing.create_ledger(
        root,
        upgrade_id=upgrade_id,
        baseline_version=baseline_version,
        target_version=target_version,
        campaign_purpose=campaign_purpose,
        evidence_decision="recapture",
    )
    timing.checkpoint(root, "receipts/p0.json")
    return root / "receipts" / "p0.json"


def create_job_rehearsal_receipt(
    root: Path,
    *,
    contract: dict[str, object],
    preflight_campaign_id: str,
    preflight_campaign_dir: Path | None = None,
    preflight_manifest_sha256: str | None = None,
) -> Path:
    """写入不运行命令的合成全量 Job facts，并经正式 finalizer 封存。"""

    from tools.official_client_capture import codex_upgrade

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    rehearsal.validate_execution_contract(contract)
    tool_identity = codex_upgrade._tool_identity()
    if tool_identity["files_sha256"] != contract["tool_files_sha256"]:
        raise AssertionError("测试合同与当前工具树摘要不一致")
    entries = tool_identity["entries"]
    trees = {
        name: {
            "root": str((root / name).resolve()),
            "entry_count": len(entries),
            "files_sha256": tool_identity["files_sha256"],
            "entries": entries,
        }
        for name in ("managed_host", "execution_host", "execution_container")
    }

    def dependencies(names: tuple[str, ...], prefix: str) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "path": f"/{prefix}/{name}",
                "sha256": hashlib.sha256(f"{prefix}:{name}".encode()).hexdigest(),
                "status": "passed",
            }
            for name in names
        ]

    configuration = contract["configuration"]
    container_names = sorted(
        {
            str(configuration[field])
            for field in (
                "capture_container",
                "service_container",
                "keeper_container",
                "postgres_container",
                "redis_container",
            )
        }
    )
    containers = [
        {
            "name": name,
            "container_id": f"{index:x}" * 64,
            "image_id": f"sha256:{index + 5:x}" + f"{index + 5:x}" * 63,
            "running": True,
        }
        for index, name in enumerate(container_names, 1)
    ]
    jobs: list[dict[str, object]] = []
    for job_id in contract["job_ids"]:
        step_count = contract["step_counts"][job_id]
        jobs.append(
            {
                "id": job_id,
                "phase": contract["job_phases"][job_id],
                "status": "passed",
                "job_contract_sha256": hashlib.sha256(job_id.encode()).hexdigest(),
                "step_count": step_count,
                "steps": [
                    {
                        "index": index,
                        "status": "passed",
                        "launcher": "host",
                        "argv_sha256": hashlib.sha256(
                            f"{job_id}:argv:{index}".encode()
                        ).hexdigest(),
                        "environment_sha256": hashlib.sha256(
                            f"{job_id}:env:{index}".encode()
                        ).hexdigest(),
                        "timeout_seconds": 60,
                        "path_checks": [
                            {
                                "kind": "host_script",
                                "path": f"/fixture/{job_id}-{index}.sh",
                                "sha256": hashlib.sha256(
                                    f"{job_id}:path:{index}".encode()
                                ).hexdigest(),
                            }
                        ],
                        "dependencies": ["host:bash"],
                    }
                    for index in range(1, step_count + 1)
                ],
                "c2pa_identity": contract["c2pa_job_identities"].get(job_id),
            }
        )
    job_set_sha256 = rehearsal._fingerprint(
        [
            {
                "id": item["id"],
                "phase": item["phase"],
                "job_contract_sha256": item["job_contract_sha256"],
            }
            for item in jobs
        ]
    )
    binary_path = str(configuration["capture_codex_bin"])
    helper_path = str(configuration["capture_code_mode_host_bin"])
    facts: dict[str, object] = {
        "schema_version": rehearsal.FACTS_SCHEMA,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preflight_campaign": {
            "path": str(
                (
                    preflight_campaign_dir
                    if preflight_campaign_dir is not None
                    else root / "preflight-campaign"
                ).resolve()
            ),
            "campaign_id": preflight_campaign_id,
            "manifest_sha256": preflight_manifest_sha256 or "9" * 64,
            "campaign_mode": "preflight_only",
        },
        "execution_contract": contract,
        "execution_contract_sha256": rehearsal.execution_contract_sha256(contract),
        "host": {"architecture": "linux/arm64", "machine": "aarch64"},
        "containers": containers,
        "tool_trees": trees,
        "dependencies": {
            "host": dependencies(rehearsal.HOST_REQUIRED_COMMANDS, "host"),
            "capture_container": dependencies(
                rehearsal.CAPTURE_CONTAINER_REQUIRED_COMMANDS, "container"
            ),
        },
        "binary_verification": {
            "passed": True,
            "expected_version": contract["target_version"],
            "expected_sha256": contract["target_sha256"],
            "runtime_image_reference": configuration["runtime_image"],
            "runtime_image_id": f"sha256:{'8' * 64}",
            "identities": [
                {
                    "label": label,
                    "path": binary_path,
                    "version": contract["target_version"],
                    "version_output": f"codex-cli {contract['target_version']}",
                    "sha256": contract["target_sha256"],
                }
                for label in (
                    "container:capture_codex_bin",
                    "container:relay_codex_bin",
                    "host:relay_codex_bin",
                )
            ],
            "package": {
                "asset_sha256": contract["target_package_sha256"],
                "code_mode_host_sha256": contract[
                    "target_code_mode_host_sha256"
                ],
            },
            "helpers": [
                {
                    "label": label,
                    "path": helper_path,
                    "sha256": contract["target_code_mode_host_sha256"],
                }
                for label in (
                    "container:capture_code_mode_host_bin",
                    "container:relay_code_mode_host_bin",
                    "host:relay_code_mode_host_bin",
                )
            ],
        },
        "probes": {
            "bubblewrap": {
                "status": "passed",
                "version": "bubblewrap fixture",
                "network_isolated": True,
            },
            "zstd": {
                "status": "passed",
                "input_sha256": hashlib.sha256(rehearsal.ZSTD_FRAME).hexdigest(),
                "output_sha256": hashlib.sha256(rehearsal.ZSTD_OUTPUT).hexdigest(),
                "output": rehearsal.ZSTD_OUTPUT.decode("ascii"),
            },
        },
        "jobs": jobs,
        "summary": {
            "job_count": contract["job_count"],
            "passed_job_count": contract["job_count"],
            "phase_counts": contract["phase_counts"],
            "job_set_sha256": job_set_sha256,
            "live_requests_sent": False,
            "status": "passed",
        },
        "runtime_identity_sha256": "",
        "collector": rehearsal._producer(),
    }
    facts["runtime_identity_sha256"] = rehearsal._runtime_identity(facts)
    _write(root / "facts.json", facts)
    rehearsal.finalize(root, "facts.json", "receipt.json")
    return root / "receipt.json"


def create_p0_gate_receipt(
    root: Path,
    *,
    upgrade_id: str,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
    job_rehearsal_receipt: Path,
) -> Path:
    """创建不运行命令、但结构与正式 P0 完全一致的合成门禁收据。"""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    evidence = []
    for role in sorted(
        {
            "campaign_run_rehearsal",
            "check_egress_spec",
            "job_rehearsal",
            "rollback",
            "test_capture_tools",
        }
    ):
        relative = f"evidence/{role}.log"
        _write(root / relative, {"role": role, "status": "passed"})
        evidence.append({"role": role, "path": relative})
    facts = {
        "schema_version": vc_receipt.FACTS_SCHEMA,
        "kind": "p0_gate",
        "subject": {
            "upgrade_id": upgrade_id,
            "campaign_id": None,
            "campaign_purpose": campaign_purpose,
            "baseline_version": baseline_version,
            "target_version": target_version,
            "candidate_id": None,
            "attempt_id": None,
        },
        "assertions": {
            "offline_gates": [
                {
                    "gate_id": "check-egress-spec",
                    "kind": "public",
                    "command": ["make", "check-egress-spec"],
                    "exit_code": 0,
                    "passed": 1,
                    "failed": 0,
                    "approved_skip": 0,
                    "unexpected_skip": 0,
                },
                {
                    "gate_id": "test-capture-tools",
                    "kind": "public",
                    "command": ["make", "test-capture-tools"],
                    "exit_code": 0,
                    "passed": 1,
                    "failed": 0,
                    "approved_skip": 0,
                    "unexpected_skip": 0,
                },
            ],
            "tool_blockers": [],
            "campaign_run_rehearsal": {
                "multi_batch_passed": True,
                "original_deadline_inherited": True,
                "frozen_jobs_passed": True,
                "live_request_count": 0,
            },
            "rollback_ready": True,
            "job_rehearsal_sha256": hashlib.sha256(
                job_rehearsal_receipt.read_bytes()
            ).hexdigest(),
        },
        "evidence": evidence,
    }
    facts_path = _write(root / "p0-facts.json", facts)
    vc_receipt.finalize(root.resolve(), facts_path.name, "p0-receipt.json")
    return root / "p0-receipt.json"
