#!/usr/bin/env python3
"""封存 VC-1 sequence 4 在父 run 创建前的确定性拒绝。

本工具只承接 0.154.0 首轮正式 Campaign 的唯一 sequence 4：批次已经只写一次，
但旧权限 helper 因遗漏冻结 OAuth 目录形态而在监督器只读前检中失败，父 run 和
任何动作均未创建。工具只读取控制文件与证据元数据，不读取证据正文、不改权限、
不发送请求；通过后只写一次零请求封口收据，供唯一 sequence 5 使用。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    import codex_upgrade_vc1_permission_alias_closeout as permission_closeout
else:
    from . import codex_upgrade_vc1_permission_alias_closeout as permission_closeout


SCHEMA_VERSION = "codex-vc1-permission-alias-predispatch-closeout/v1"
CAMPAIGN_ID = "c0154-formal-vc1-bwg-new-window-20260914t100818z"
ATTEMPT_ID = "20260914T102852Z-04996800fbbe4e94"
HOST_DATA_ROOT = Path("/root/docker/capture-cli/data")
CAMPAIGN_DIR = HOST_DATA_ROOT / "evidence/campaigns" / CAMPAIGN_ID
ATTEMPT_PATH = (
    CAMPAIGN_DIR / "official/attempts" / ATTEMPT_ID / "attempt.json"
)
STATE_DIR = HOST_DATA_ROOT / "control" / f"{CAMPAIGN_ID}-supervisor"
ACTION_INPUT_DIR = HOST_DATA_ROOT / "control" / f"{CAMPAIGN_ID}-action-inputs"
RECEIPT_PATH = ACTION_INPUT_DIR / "sequence4-predispatch-closeout-receipt.json"
TOOL_PATH = (
    HOST_DATA_ROOT
    / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_predispatch_closeout.py"
)
SUPERVISOR_PATH = (
    HOST_DATA_ROOT / "tools/official_client_capture/codex_upgrade_supervisor.py"
)
CURRENT_HELPER_PATH = (
    HOST_DATA_ROOT
    / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_closeout.py"
)
SEQUENCE4_BATCH_PATH = CAMPAIGN_DIR / "control/vc/batches/0004-vc-1.json"
SEQUENCE4_MANIFEST_PATH = CAMPAIGN_DIR / "control/vc/run-manifests/0004-vc-1.json"
SEQUENCE4_ACTION_PLAN_PATH = ACTION_INPUT_DIR / "vc1-sequence4-action-plan.json"
FAILED_DEPLOYMENT_PATH = (
    HOST_DATA_ROOT / "control/codex-0154-supervisor-enable-20260914t133412z.json"
)
SEQUENCE4_BATCH_FILE_SHA256 = (
    "02445883a35a1298821a76cfb8eed841b596de2169c22fc34d6d660981ed2cc8"
)
SEQUENCE4_BATCH_SHA256 = (
    "b2455bb609270d25ac257d08405c6eebfd6cfa2d3d207f946e227929030f72fb"
)
SEQUENCE4_MANIFEST_FILE_SHA256 = (
    "6d574a8c785f5ccd0a26033ff43631b62789b27db289590053c210aaedf55e23"
)
SEQUENCE4_ACTION_PLAN_SHA256 = (
    "0daf24b9e369b59735520cfa4ed51d874baa083b1fa396089c36fe8e0b5d8cf1"
)
FAILED_DEPLOYMENT_SHA256 = (
    "7d81a306ec1d71e0eb795ed45d1b2ad71449e60d39f5f2f969e921c0968ca5a6"
)
FAILED_TOOL_FILES_SHA256 = (
    "3bfcc618a6fb94a8c145e747a044803e6b0f148ef5f7004e15f990a91d7d6da9"
)
FAILED_SUPERVISOR_SHA256 = (
    "625a6d86ea28e4025b051ded84a829e8bfd5787db7b5450a41ef2460317d71b9"
)
FAILED_HELPER_SHA256 = (
    "408e9d733b8997e569fbea36ab648f1bd70e45f9669accf3cc9353eb8b2566b1"
)
SEQUENCE4_COMPILED_AT_UTC = "2026-09-14T13:43:34+00:00"
SEQUENCE4_MUST_START_BY_UTC = "2026-09-14T13:44:34+00:00"
EXPECTED_OLD_ERROR = (
    "外部证据根不属于冻结 Campaign："
    "/root/oauth-capture/runs/official-client/oauth/"
    "oauth-c0154-formal-vc1-bwg-new-window-20260914t100818z"
)
EXPECTED_RUN_NAMES = (
    "run-8dbb3b72a10463e8fb98b962a1e427f53b2ffe0ed44e82a743f22a78515f2992",
    "run-c1b188aa7372460a547b4b6f167e539a546bd1ae764c1422cbbf8aa02f0346d4",
    "run-d932716c0dd0fbb60789232cbffad83910a271f58262b67679db75629341769f",
)
SHA256_RE = permission_closeout.SHA256_RE


class PredispatchCloseoutError(RuntimeError):
    """预派发失败现场或当前工具身份不满足唯一恢复合同时抛出。"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _private_file(path: Path, label: str, expected_sha256: str | None = None) -> bytes:
    permission_closeout.reject_symlink_components(path, label)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise PredispatchCloseoutError(f"{label} 类型、属主、权限或硬链接漂移。")
    raw = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PredispatchCloseoutError(f"{label} 摘要漂移。")
    return raw


def _managed_file(path: Path, label: str, expected_sha256: str) -> bytes:
    """读取不可由其他用户写入且无额外硬链接的受管工具。"""

    permission_closeout.reject_symlink_components(path, label)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
    ):
        raise PredispatchCloseoutError(f"{label} 类型、属主、权限或硬链接漂移。")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PredispatchCloseoutError(f"{label} 摘要漂移。")
    return raw


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PredispatchCloseoutError(f"{label} 不是有效 UTF-8 JSON。") from error
    if not isinstance(payload, dict):
        raise PredispatchCloseoutError(f"{label} 顶层必须是对象。")
    return payload


def _owned_directory(path: Path, label: str, *, exact_mode: int | None = None) -> None:
    metadata = permission_closeout._validate_owned_directory(path, label)
    if metadata.st_mode & 0o022:
        raise PredispatchCloseoutError(f"{label} 可被 group/other 写入。")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise PredispatchCloseoutError(f"{label} 权限必须为 {exact_mode:04o}。")


def _validate_current_deployment(
    path: Path,
    *,
    expected_sha256: str,
    expected_tool_files_sha256: str,
    expected_self_sha256: str,
) -> tuple[dict[str, Any], Path]:
    if (
        path.parent != HOST_DATA_ROOT / "control"
        or not path.name.startswith("codex-0154-supervisor-enable-")
        or not path.name.endswith(".json")
    ):
        raise PredispatchCloseoutError("当前部署收据坐标漂移。")
    raw = _private_file(path, "当前受管部署收据", expected_sha256)
    payload = _json(raw, "当前受管部署收据")
    rollback_text = payload.get("rollback_backup")
    if (
        payload.get("schema_version") != "codex-arm64-supervisor-enable/v1"
        or payload.get("status") != "passed"
        or payload.get("architecture") != "aarch64"
        or payload.get("production_tool_root")
        != str(HOST_DATA_ROOT / "tools/official_client_capture")
        or payload.get("tool_files_sha256") != expected_tool_files_sha256
        or payload.get("supervisor_sha256") != file_sha256(SUPERVISOR_PATH)
        or file_sha256(TOOL_PATH) != expected_self_sha256
        or not isinstance(rollback_text, str)
    ):
        raise PredispatchCloseoutError("当前部署收据没有绑定本次受管工具。")
    rollback = Path(rollback_text)
    if (
        rollback.parent != HOST_DATA_ROOT / "control"
        or not rollback.name.startswith("managed-tools-backup-before-")
    ):
        raise PredispatchCloseoutError("当前部署回滚树坐标漂移。")
    _owned_directory(rollback, "当前部署回滚工具树")
    return payload, rollback


def _load_historical_helper(path: Path) -> Any:
    _managed_file(path, "历史失败 helper", FAILED_HELPER_SHA256)
    spec = importlib.util.spec_from_file_location(
        "codex_vc1_historical_permission_alias_helper",
        path,
    )
    if spec is None or spec.loader is None:
        raise PredispatchCloseoutError("无法加载历史失败 helper。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_sequence4_artifacts() -> tuple[dict[str, Any], dict[str, Any]]:
    batch = _json(
        _private_file(
            SEQUENCE4_BATCH_PATH,
            "sequence 4 batch",
            SEQUENCE4_BATCH_FILE_SHA256,
        ),
        "sequence 4 batch",
    )
    manifest = _json(
        _private_file(
            SEQUENCE4_MANIFEST_PATH,
            "sequence 4 run manifest",
            SEQUENCE4_MANIFEST_FILE_SHA256,
        ),
        "sequence 4 run manifest",
    )
    _private_file(
        SEQUENCE4_ACTION_PLAN_PATH,
        "sequence 4 action plan",
        SEQUENCE4_ACTION_PLAN_SHA256,
    )
    deployment = _json(
        _private_file(
            FAILED_DEPLOYMENT_PATH,
            "sequence 4 部署收据",
            FAILED_DEPLOYMENT_SHA256,
        ),
        "sequence 4 部署收据",
    )
    if (
        batch.get("sequence") != 4
        or batch.get("batch_id") != "vc-1-0004"
        or batch.get("batch_sha256") != SEQUENCE4_BATCH_SHA256
        or batch.get("compiled_at_utc") != SEQUENCE4_COMPILED_AT_UTC
        or batch.get("must_start_by_utc") != SEQUENCE4_MUST_START_BY_UTC
        or manifest.get("schema_version") != "codex-upgrade-campaign-run/v2"
        or manifest.get("campaign_id") != CAMPAIGN_ID
        or manifest.get("batch_sequence") != 4
        or manifest.get("batch_sha256") != SEQUENCE4_BATCH_SHA256
        or deployment.get("schema_version") != "codex-arm64-supervisor-enable/v1"
        or deployment.get("status") != "passed"
        or deployment.get("tool_files_sha256") != FAILED_TOOL_FILES_SHA256
        or deployment.get("supervisor_sha256") != FAILED_SUPERVISOR_SHA256
    ):
        raise PredispatchCloseoutError("sequence 4 批次或旧部署身份漂移。")
    must_start = datetime.fromisoformat(SEQUENCE4_MUST_START_BY_UTC).timestamp()
    if time.time() <= must_start:
        raise PredispatchCloseoutError("sequence 4 启动窗口尚未过期，拒绝提前封口。")
    return batch, manifest


def _validate_no_sequence4_run(lock_descriptor: int) -> list[dict[str, Any]]:
    del lock_descriptor
    _owned_directory(STATE_DIR, "原 campaign-run state-dir", exact_mode=0o700)
    run_dirs = sorted(STATE_DIR.glob("run-*"))
    if tuple(path.name for path in run_dirs) != EXPECTED_RUN_NAMES:
        raise PredispatchCloseoutError("campaign-run 历史不再精确为 sequence 1～3。")
    facts: list[dict[str, Any]] = []
    for expected_sequence, run_dir in enumerate(run_dirs, start=1):
        _owned_directory(run_dir, f"sequence {expected_sequence} run", exact_mode=0o700)
        record_path = run_dir / "campaign-run-manifest.json"
        raw = _private_file(record_path, f"sequence {expected_sequence} run manifest")
        record = _json(raw, f"sequence {expected_sequence} run manifest")
        effective = record.get("manifest")
        if (
            not isinstance(effective, Mapping)
            or effective.get("campaign_id") != CAMPAIGN_ID
            or effective.get("batch_sequence") != expected_sequence
        ):
            raise PredispatchCloseoutError("campaign-run 历史序号或身份漂移。")
        facts.append(
            {
                "batch_sequence": expected_sequence,
                "run_name": run_dir.name,
                "manifest_file_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return facts


def _assert_no_outputs() -> None:
    attempt_root = ATTEMPT_PATH.parent
    candidates = (
        ACTION_INPUT_DIR / "sequence4-permission-alias-closeout-receipt.json",
        ACTION_INPUT_DIR / "sequence5-permission-alias-closeout-receipt.json",
        attempt_root / "evidence-manifest.json",
        attempt_root / "seal-draft.json",
        attempt_root / "seal-preview.json",
    )
    if any(path.exists() or path.is_symlink() for path in candidates):
        raise PredispatchCloseoutError("sequence 4 前已出现权限收据或 seal 制品。")


def close_predispatch(
    *,
    current_deployment_receipt: Path,
    current_deployment_receipt_sha256: str,
    tool_files_sha256: str,
    self_sha256: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """锁定父队列并封存确定性预派发失败。"""

    _validate_current_deployment(
        current_deployment_receipt,
        expected_sha256=current_deployment_receipt_sha256,
        expected_tool_files_sha256=tool_files_sha256,
        expected_self_sha256=self_sha256,
    )
    current_deployment = _json(
        _private_file(current_deployment_receipt, "当前受管部署收据"),
        "当前受管部署收据",
    )
    rollback = Path(str(current_deployment["rollback_backup"]))
    old_helper_path = rollback / "codex_upgrade_vc1_permission_alias_closeout.py"
    permission_closeout.reject_symlink_components(old_helper_path, "历史失败 helper")
    historical_helper = _load_historical_helper(old_helper_path)
    batch, manifest = _validate_sequence4_artifacts()

    lock_path = STATE_DIR / ".campaign-run.lock"
    lock_raw = _private_file(lock_path, "campaign-run 锁文件")
    del lock_raw
    lock_descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PredispatchCloseoutError("campaign-run 仍在运行，禁止封口。") from error
        run_facts = _validate_no_sequence4_run(lock_descriptor)
        _assert_no_outputs()
        try:
            historical_helper.inspect_permission_boundary(
                attempt_path=ATTEMPT_PATH,
                campaign_id=CAMPAIGN_ID,
                attempt_id=ATTEMPT_ID,
                attempt_sha256=permission_closeout.ATTEMPT_SHA256,
                roots_sha256=permission_closeout.ROOTS_SHA256,
                readonly_runs_root=permission_closeout.READONLY_RUNS_ROOT,
                writable_runs_root=permission_closeout.WRITABLE_RUNS_ROOT,
                require_mount_modes=True,
                expected_entry_count=permission_closeout.EXPECTED_ENTRY_COUNT,
                expected_gap_count=permission_closeout.EXPECTED_GAP_COUNT,
                expected_gap_sha256=permission_closeout.EXPECTED_GAP_SHA256,
            )
        except historical_helper.PermissionAliasCloseoutError as error:
            if str(error) != EXPECTED_OLD_ERROR:
                raise PredispatchCloseoutError(
                    "历史 helper 没有复现唯一冻结的 OAuth 根误拒绝。"
                ) from error
        else:
            raise PredispatchCloseoutError("历史 helper 意外通过，拒绝伪造预派发失败。")

        boundary = permission_closeout.inspect_permission_boundary(
            attempt_path=ATTEMPT_PATH,
            campaign_id=CAMPAIGN_ID,
            attempt_id=ATTEMPT_ID,
            attempt_sha256=permission_closeout.ATTEMPT_SHA256,
            roots_sha256=permission_closeout.ROOTS_SHA256,
            readonly_runs_root=permission_closeout.READONLY_RUNS_ROOT,
            writable_runs_root=permission_closeout.WRITABLE_RUNS_ROOT,
            require_mount_modes=True,
            expected_entry_count=permission_closeout.EXPECTED_ENTRY_COUNT,
            expected_gap_count=permission_closeout.EXPECTED_GAP_COUNT,
            expected_gap_sha256=permission_closeout.EXPECTED_GAP_SHA256,
        )
        core = {
            "schema_version": SCHEMA_VERSION,
            "status": "passed",
            "campaign_id": CAMPAIGN_ID,
            "attempt_id": ATTEMPT_ID,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_sequence": 4,
            "source_batch_file_sha256": SEQUENCE4_BATCH_FILE_SHA256,
            "source_batch_sha256": SEQUENCE4_BATCH_SHA256,
            "source_manifest_file_sha256": SEQUENCE4_MANIFEST_FILE_SHA256,
            "source_action_plan_sha256": SEQUENCE4_ACTION_PLAN_SHA256,
            "source_failed_deployment_sha256": FAILED_DEPLOYMENT_SHA256,
            "source_compiled_at_utc": batch["compiled_at_utc"],
            "source_must_start_by_utc": batch["must_start_by_utc"],
            "source_actions": [
                str(item.get("action_id"))
                for item in manifest.get("actions", [])
                if isinstance(item, Mapping)
            ],
            "source_run_created": False,
            "source_run_history": run_facts,
            "deterministic_error_type": "PermissionAliasCloseoutError",
            "deterministic_error": EXPECTED_OLD_ERROR,
            "current_deployment_receipt": str(current_deployment_receipt),
            "current_deployment_receipt_sha256": current_deployment_receipt_sha256,
            "current_tool_files_sha256": tool_files_sha256,
            "closeout_tool_sha256": self_sha256,
            "current_helper_sha256": file_sha256(CURRENT_HELPER_PATH),
            "boundary": {
                "entry_count": len(boundary.entries),
                "gap_count": len(boundary.gaps),
                "gap_sha256": boundary.gap_sha256,
                "stable_boundary_sha256": boundary.boundary_sha256,
            },
            "scanned_bytes": 0,
            "live_request_count": 0,
        }
        receipt = {
            **core,
            "receipt_sha256": hashlib.sha256(canonical_bytes(core)).hexdigest(),
        }
        permission_closeout.secure_write_receipt(receipt_path, receipt)
        return receipt
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-deployment-receipt", required=True, type=Path)
    parser.add_argument("--current-deployment-receipt-sha256", required=True)
    parser.add_argument("--tool-files-sha256", required=True)
    parser.add_argument("--self-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    self_path = Path(__file__).absolute()
    metadata = self_path.lstat()
    if (
        self_path != TOOL_PATH
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or not SHA256_RE.fullmatch(arguments.self_sha256)
        or file_sha256(self_path) != arguments.self_sha256
        or not SHA256_RE.fullmatch(arguments.current_deployment_receipt_sha256)
        or not SHA256_RE.fullmatch(arguments.tool_files_sha256)
        or arguments.receipt != RECEIPT_PATH
    ):
        raise PredispatchCloseoutError("预派发封口工具身份或命令坐标漂移。")
    receipt = close_predispatch(
        current_deployment_receipt=arguments.current_deployment_receipt,
        current_deployment_receipt_sha256=arguments.current_deployment_receipt_sha256,
        tool_files_sha256=arguments.tool_files_sha256,
        self_sha256=arguments.self_sha256,
        receipt_path=arguments.receipt,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PredispatchCloseoutError as error:
        print(f"VC-1 sequence 4 预派发封口失败：{error}", file=sys.stderr)
        raise SystemExit(1)
