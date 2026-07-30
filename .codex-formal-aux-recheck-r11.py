#!/usr/bin/env python3
"""复核正式 aux 的外层恢复；保留原失败回执并证明仅为挂载数组排列差异。"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def run(*args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"命令失败：{args[0]} {args[1] if len(args) > 1 else ''}")
    return completed.stdout.strip()


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def inspect(container: str, template: str) -> str:
    return run("docker", "inspect", "-f", template, container)


def mount_recheck(container: str, baseline_value: str) -> dict[str, Any]:
    mounts = json.loads(inspect(container, "{{json .Mounts}}"))
    count_text, expected_digest = baseline_value.split("|", 1)
    matches: list[int] = []
    for index, permutation in enumerate(itertools.permutations(mounts)):
        encoded = json.dumps(
            list(permutation), sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(encoded).hexdigest() == expected_digest:
            matches.append(index)
    canonical = sorted(
        mounts,
        key=lambda item: (
            str(item.get("Destination", "")),
            str(item.get("Source", "")),
            str(item.get("Type", "")),
        ),
    )
    canonical_digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "container_id": inspect(container, "{{.Id}}"),
        "mount_count": len(mounts),
        "baseline_mount_count": int(count_text),
        "baseline_order_sensitive_sha256": expected_digest,
        "baseline_is_permutation_of_current": bool(matches),
        "matching_permutation_indexes": matches,
        "current_canonical_sha256": canonical_digest,
    }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("用法：脚本 BASELINE_JSON RUN_ROOT EVIDENCE_ROOT")
    baseline_path = Path(sys.argv[1]).resolve()
    run_root = Path(sys.argv[2]).resolve()
    evidence_root = Path(sys.argv[3]).resolve()
    output = evidence_root / "restoration/formal-aux-restoration-recheck.json"
    if output.exists() or output.is_symlink():
        raise SystemExit("复核输出已存在，拒绝覆盖")

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summary_path = run_root / "run-summary.json"
    wire_path = run_root / "formal-wire-validation.json"
    original_receipt_path = run_root / "formal-wrapper-receipt.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    wire = json.loads(wire_path.read_text(encoding="utf-8"))
    original_receipt = json.loads(original_receipt_path.read_text(encoding="utf-8"))

    postgres = "sub2apiplus-postgres"
    redis = "sub2apiplus-redis"
    keeper = "sub2apiplus-keeper"
    service = "sub2apiplus"
    env_lines = inspect(postgres, "{{range .Config.Env}}{{println .}}{{end}}").splitlines()
    db_user = next(item.split("=", 1)[1] for item in env_lines if item.startswith("POSTGRES_USER="))
    db_name = next(item.split("=", 1)[1] for item in env_lines if item.startswith("POSTGRES_DB="))

    def query(sql: str) -> str:
        return run(
            "docker", "exec", postgres, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", db_user, "-d", db_name, "-qAtc", sql,
        )

    group = query(
        "select platform||'|'||require_oauth_only::text||'|'||allow_live::text||'|'||"
        "allow_image_generation::text from groups where id=9"
    )
    proxy = query(
        "select coalesce(proxy_id::text,'NULL')||'|'||"
        "coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id=99"
    )
    mapping = query(
        "select case when credentials ? 'model_mapping' then 'present:'||"
        "encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex') "
        "else 'missing:' end from accounts where id=99"
    )
    credentials_sha = query(
        "select encode(sha256(convert_to(credentials::text,'UTF8')),'hex') "
        "from accounts where id=99"
    )
    counts = query(
        "select (select count(*) from users)::text||'|'||"
        "(select count(*) from groups)::text||'|'||"
        "(select count(*) from accounts)::text||'|'||"
        "(select count(*) from api_keys)::text||'|'||"
        "(select count(*) from account_groups)::text||'|'||"
        "(select count(*) from proxies)::text"
    )
    temp_proxy_count = query(
        "select count(*) from proxies where name='candidate-aux-formal-r11-aux-"
        "codex0145-20260730T204352Z-r11'"
    )
    auth_digest = query(
        "select encode(sha256(convert_to(key,'UTF8')),'hex') from api_keys "
        "where id=15 and group_id=9 and status='active'"
    )
    cache_exists = run(
        "docker", "exec", "-i", redis, "redis-cli", "--raw", "-x", "EXISTS",
        input_text=f"apikey:auth:{auth_digest}",
    )

    hostname = run("docker", "exec", service, "hostname")
    hosts_lines = []
    for raw in run("docker", "exec", service, "cat", "/etc/hosts").splitlines():
        value = " ".join(raw.split())
        if value and not value.startswith("#") and hostname not in value:
            hosts_lines.append(value)
    hosts_sha = hashlib.sha256(
        ("\n".join(sorted(set(hosts_lines))) + "\n").encode()
    ).hexdigest()
    ca_sha = run(
        "docker", "exec", service, "sha256sum", "/etc/ssl/certs/ca-certificates.crt"
    ).split()[0]
    service_env = inspect(service, "{{range .Config.Env}}{{println .}}{{end}}").splitlines()
    capture_env_count = sum(
        item.startswith("SUB2API_LIVE_ATTESTATION_CAPTURE_") for item in service_env
    )
    hosts_text = run("docker", "exec", service, "cat", "/etc/hosts").lower()
    forbidden_hosts = (
        "chatgpt.com", "api.openai.com", "auth.openai.com",
        "region-candidate-0145.oaiusercontent.com",
    )
    custom_ca_absent = (
        run(
            "docker", "exec", service, "sh", "-c",
            "test ! -e /usr/local/share/ca-certificates/candidate-core-capture.crt "
            "-a ! -e /usr/local/share/ca-certificates/candidate-aux-capture.crt; echo $?",
        )
        == "0"
    )

    mount_checks = {
        "postgres": mount_recheck(postgres, baseline["postgres_mount"]),
        "redis": mount_recheck(redis, baseline["redis_mount"]),
        "keeper": mount_recheck(keeper, baseline["keeper_mount"]),
    }
    checks = {
        "group_equal": group == baseline["group"],
        "account_proxy_equal": proxy == baseline["proxy"],
        "model_mapping_equal": mapping == baseline["model_mapping"],
        "credentials_sha256_equal": credentials_sha == baseline["credentials_sha256"],
        "protected_table_counts_equal": counts == baseline["protected_table_counts"],
        "temporary_proxy_absent": temp_proxy_count == "0",
        "auth_cache_absent": cache_exists == "0",
        "postgres_id_equal": inspect(postgres, "{{.Id}}") == baseline["postgres_id"],
        "redis_id_equal": inspect(redis, "{{.Id}}") == baseline["redis_id"],
        "keeper_id_equal": inspect(keeper, "{{.Id}}") == baseline["keeper_id"],
        "all_mount_sets_equal_ignoring_order": all(
            item["baseline_is_permutation_of_current"] for item in mount_checks.values()
        ),
        "keeper_running": inspect(keeper, "{{.State.Running}}") == "true",
        "normal_image_restored": inspect(service, "{{.Image}}")
        == "sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14",
        "normal_reference_restored": inspect(service, "{{.Config.Image}}")
        == "sub2apiplus:codex0145-20260730T195700Z-39e579acb066-r11",
        "service_healthy": inspect(
            service,
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        ) == "healthy",
        "attestation_environment_absent": capture_env_count == 0,
        "ca_bundle_equal": ca_sha == baseline["ca_sha256"],
        "semantic_hosts_equal": hosts_sha == baseline["hosts_semantic_sha256"],
        "capture_hosts_absent": not any(host in hosts_text for host in forbidden_hosts),
        "custom_capture_ca_absent": custom_ca_absent,
        "run_summary_complete": summary.get("status") == "complete"
        and summary.get("exit_code") == 0,
        "inner_restoration_complete": all(summary.get("restoration", {}).values()),
        "wire_validation_accepted": wire.get("accepted") is True,
        "original_outer_receipt_preserved": original_receipt.get("status")
        == "restoration_failed",
    }
    accepted = all(checks.values())
    payload = {
        "schema_version": "formal-r11-aux-restoration-recheck/v1",
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accepted": accepted,
        "cause": (
            "原外层门禁对 Docker Mounts 数组做顺序敏感哈希；"
            "容器未重建但返回顺序可变化。逐排列复算证明挂载集合一致。"
        ),
        "checks": checks,
        "mount_checks": mount_checks,
        "preserved_sources": {
            "outer_baseline": {
                "path": str(baseline_path), "sha256": sha256(baseline_path),
            },
            "original_wrapper_receipt": {
                "path": str(original_receipt_path), "sha256": sha256(original_receipt_path),
            },
            "run_summary": {"path": str(summary_path), "sha256": sha256(summary_path)},
            "wire_validation": {"path": str(wire_path), "sha256": sha256(wire_path)},
        },
    }
    if not accepted:
        failed = [name for name, value in checks.items() if not value]
        raise SystemExit(f"正式 aux 恢复复核失败：{failed}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(output, 0o600)
    print(json.dumps({"accepted": True, "checks": len(checks)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
