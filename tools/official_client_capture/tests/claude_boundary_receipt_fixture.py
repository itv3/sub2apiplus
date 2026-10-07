"""attempt 级边界核对收据的测试夹具（Claude 升级 B4-01 f 段）。

按 ``claude_upgrade_boundary_checks.verify_boundary_receipt`` 的合同，在候选 attempt 根下造一份合法的边界核对收据：
``client-evidence/<check>/runs/<固定名>/`` 里每个登记的证据角色各一份占位文件、一份 pass 的 ``verdict.json``，再写
``client-evidence/<check>.json``。执行与签发时刻都取 attempt 的完成时刻（校验入口要求不早于它）。供 seal 侧的测试使用，
真实执行路径由 ``test_claude_upgrade_boundary_checks`` 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from tools.official_client_capture import claude_upgrade_boundary_checks as boundary_checks
from tools.official_client_capture.capturelib.security import file_sha256

FIXTURE_RUN_NAME = "20990101T000000Z-fixture0"
FIXTURE_RUN_ID = "fixture-boundary-run"
FIXTURE_RUN_NONCE = "f" * 64


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _entry(role: str, attempt_root: Path, path: Path) -> dict[str, Any]:
    return {
        "role": role,
        "base": "attempt",
        "path": path.relative_to(attempt_root).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def write_boundary_receipt(
    attempt_root: Path,
    check_id: str,
    *,
    live_count: int = 0,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """写一份合法的边界核对收据并返回其路径；``mutate`` 在写出前改收据（造非法样本用）。"""

    attempt_root = Path(attempt_root)
    attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
    identity = attempt.get("identity") if isinstance(attempt.get("identity"), dict) else {}
    run_relative = PurePosixPath(boundary_checks.CLIENT_EVIDENCE_DIRNAME) / check_id / boundary_checks.RUNS_DIRNAME / FIXTURE_RUN_NAME
    run_root = attempt_root.joinpath(*run_relative.parts)
    evidence: list[dict[str, Any]] = []
    roles = sorted(boundary_checks.EVIDENCE_ROLES[check_id] - {"verdict"})
    roles += sorted(f"{prefix}fixture" for prefix in boundary_checks.EVIDENCE_ROLE_PREFIXES[check_id])
    for role in roles:
        path = run_root / f"{role.replace(':', '-')}.json"
        _write(path, {"role": role, "fixture": True})
        evidence.append(_entry(role, attempt_root, path))
    live = {
        "count": live_count,
        "exact": True,
        "counting_rule": "fixture",
        "by_endpoint": {"POST /fixture": live_count} if live_count else {},
    }
    verdict_path = run_root / boundary_checks.VERDICT_FILENAME
    _write(
        verdict_path,
        {
            "schema_version": boundary_checks.RUN_VERDICT_SCHEMA,
            "check_id": check_id,
            "attempt_id": attempt_root.name,
            "boundary_run_id": FIXTURE_RUN_ID,
            "boundary_run_nonce": FIXTURE_RUN_NONCE,
            "result": "pass",
            "live_requests": live,
        },
    )
    evidence.append(_entry("verdict", attempt_root, verdict_path))
    moment = attempt["completed_at_utc"]
    receipt: dict[str, Any] = {
        "schema_version": boundary_checks.RECEIPT_SCHEMA,
        "check_id": check_id,
        "campaign_id": attempt.get("campaign_id"),
        "candidate_id": attempt_root.parents[1].name,
        "attempt_id": attempt_root.name,
        "run_nonce": attempt.get("run_nonce"),
        "result": "pass",
        "conclusion_scope": boundary_checks.CONCLUSION_SCOPES[check_id],
        "candidate": {name: identity.get(name) for name in boundary_checks.CANDIDATE_FACT_FIELDS},
        "boundary_run": {
            "run_id": FIXTURE_RUN_ID,
            "run_nonce": FIXTURE_RUN_NONCE,
            "path": run_relative.as_posix(),
            "started_at_utc": moment,
            "ended_at_utc": moment,
        },
        "evidence": sorted(evidence, key=lambda item: item["role"]),
        "live_requests": live,
        "facts": {"fixture": True},
        "issued_at_utc": moment,
    }
    if mutate is not None:
        mutate(receipt)
    path = boundary_checks.receipt_path(attempt_root, check_id)
    _write(path, receipt)
    return path
