#!/usr/bin/env python3
"""Candidate seal 端到端预演：在隔离副本上把零请求后处理链跑到最终决策。

背景（2026-09-17／18 事故）：Candidate Job 全部完成后，seal 检查点、assertion
bundle、seal preview 等零请求后处理动作因评估／控制工具缺陷接连失败，每次都在
正式 Campaign 上暴露下一层缺口，七个后继 Campaign 全部消耗在同一段链路上。
Framework §5.1.2 要求"修复必须先在冻结的最小历史夹具上完整跑通从故障点到最终
阶段，再允许恢复；禁止把正式 Campaign 当作工具集成测试环境"。本模块把这条
原则落成机器门禁：

1. ``rehearse``：父进程做前置检查并对正式目录做不变性快照，然后在新的 mount
   namespace 里把宿主数据根（及其 bind 别名）整体覆盖为 OverlayFS，upper 目录
   落在数据根之外；driver 在 namespace 内按正式动作清单逐字执行 seal 链（跳过
   已在正式流程完成的 live 动作，approve 动作自动取 seal-preview 的
   ``review_sha256``），最后执行 ``status`` 取最终决策。namespace 退出即丢弃全部
   写入，正式目录逐字节不变。
2. 父进程把 driver 输出与不变性比对写成收据，绑定 Campaign／candidate／attempt
   身份、``attempt.json`` 摘要、当前工具身份五摘要、归一化后的动作序列摘要与
   每个动作的产物清单。
3. ``verify_rehearsal_for_batch``：正式 seal 批次派发前，父监督器按同一归一化
   规则复算当前批次的动作序列，要求存在 TTL 内、身份与工具摘要完全匹配且
   ``passed`` 的收据；否则拒绝派发。

预演动作的零请求性质由动作集合保证：只有 ``candidate-seal``／``compare``／
``assert-*``／``acceptance`` 阶段项可以进入预演，且必须跳过会发 Kilo 请求的
``candidate-seal-checkpoint``（它属于正式流程，只能真实执行一次）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

# 包名优先：测试与 reconciler 都以 tools.official_client_capture 包名导入，
# 若这里退回 bare 模块名会得到第二份模块对象，常量与 mock 都会分叉。
try:
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor
except ImportError:  # pragma: no cover - driver 模式直接以文件执行时的兜底
    import codex_upgrade_supervisor as supervisor  # type: ignore[no-redef]


SCHEMA_VERSION = "codex-upgrade-candidate-seal-rehearsal/v1"
DRIVER_SCHEMA_VERSION = "codex-upgrade-candidate-seal-rehearsal-driver/v1"
RECEIPT_DIRECTORY = "control/seal-rehearsal"
# namespace 内动作的预演上下文标记：codex_upgrade 的派发门禁只在数据根确为 OverlayFS
# 隔离副本时接受它，代替 campaign-run 父监督器上下文（预演没有父 run，也不得 attach）。
REHEARSAL_CONTEXT_ENV = "CODEX_UPGRADE_SEAL_REHEARSAL_ACTIVE"
REVIEW_PLACEHOLDER = "@REVIEW_SHA256@"
APPROVE_FLAG = "--approve-seal-sha256"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
# 已在正式流程真实执行、含 Kilo live 请求的动作，预演必须跳过。
DEFAULT_SKIP_ACTION_IDS = ("candidate-seal-checkpoint",)
MAX_OUTPUT_TAIL = 2000
MAX_UPPER_INVENTORY = 4096
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
# 不变性快照覆盖的正式目录：只记录元数据（大小与 mtime_ns），避免对 GB 级证据
# 复算内容摘要；OverlayFS 保证 lower 不被写，快照只是第二道独立证明。
LOWER_SNAPSHOT_MAX_ENTRIES = 200_000


class SealRehearsalError(RuntimeError):
    """预演前置条件、隔离执行或收据校验失败。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SealRehearsalError(f"{label}必须是 RFC3339 UTC 时间。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SealRehearsalError(f"{label}时间非法。") from error
    return parsed


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SealRehearsalError(f"{label}不存在或不可信：{path}")
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise SealRehearsalError(f"{label}超过大小上限：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SealRehearsalError(f"{label}不是有效 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise SealRehearsalError(f"{label}顶层必须是对象：{path}")
    return payload


# ---------------------------------------------------------------------------
# 动作清单归一化：预演与门禁共用同一规则
# ---------------------------------------------------------------------------


def _item_allowed(item_id: Any) -> bool:
    return supervisor._post_run_tooling_item_allowed(item_id)


def _normalize_command(command: Sequence[str]) -> list[str]:
    """把 approve 动作里的 review 摘要替换为占位符，其余逐字保留。"""

    normalized = list(command)
    for index, token in enumerate(normalized):
        if token == APPROVE_FLAG and index + 1 < len(normalized):
            normalized[index + 1] = REVIEW_PLACEHOLDER
        elif token.startswith(APPROVE_FLAG + "="):
            normalized[index] = f"{APPROVE_FLAG}={REVIEW_PLACEHOLDER}"
    return normalized


def _is_approve_action(command: Sequence[str]) -> bool:
    return any(
        token == APPROVE_FLAG or token.startswith(APPROVE_FLAG + "=")
        for token in command
    )


def normalize_rehearsal_actions(
    actions: Iterable[Mapping[str, Any]],
    *,
    skip_action_ids: Iterable[str] = DEFAULT_SKIP_ACTION_IDS,
) -> list[dict[str, Any]]:
    """过滤 live 动作、校验阶段项闭集，返回归一化后的预演动作序列。

    每项保留 ``action_id``、``operation``、``timeout_seconds``、``item_ids``、
    ``command``（原样，供执行）与 ``normalized_command``（占位后，供摘要）。
    """

    skip = set(skip_action_ids)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, action in enumerate(actions, 1):
        if not isinstance(action, Mapping):
            raise SealRehearsalError(f"动作第 {index} 项必须是对象。")
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise SealRehearsalError(f"动作第 {index} 项缺少 action_id。")
        if action_id in seen:
            raise SealRehearsalError(f"动作重复：{action_id}")
        seen.add(action_id)
        if action_id in skip:
            continue
        command = action.get("command")
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
            or not all(isinstance(token, str) and token for token in command)
        ):
            raise SealRehearsalError(f"动作 {action_id} 的 command 非法。")
        item_ids = action.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids or not all(
            _item_allowed(item) for item in item_ids
        ):
            raise SealRehearsalError(
                f"动作 {action_id} 的 item_ids 不在零请求后处理阶段项闭集内。"
            )
        timeout = action.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise SealRehearsalError(f"动作 {action_id} 的 timeout_seconds 非法。")
        normalized.append(
            {
                "action_id": action_id,
                "operation": str(action.get("operation", "")),
                "timeout_seconds": float(timeout),
                "item_ids": sorted(str(item) for item in item_ids),
                "command": list(command),
                "normalized_command": _normalize_command(command),
                "approve": _is_approve_action(command),
            }
        )
    if not normalized:
        raise SealRehearsalError("过滤 live 动作后没有可预演的动作。")
    return normalized


def actions_sha256(actions: Sequence[Mapping[str, Any]]) -> str:
    """动作序列摘要：只绑定 action_id、item_ids 与占位后的命令。"""

    core = [
        {
            "action_id": item["action_id"],
            "item_ids": list(item["item_ids"]),
            "normalized_command": list(item["normalized_command"]),
        }
        for item in actions
    ]
    return _sha256(_canonical(core))


# ---------------------------------------------------------------------------
# 不变性快照与 upper 清单
# ---------------------------------------------------------------------------


def snapshot_tree(root: Path, *, max_entries: int = LOWER_SNAPSHOT_MAX_ENTRIES) -> dict[str, Any]:
    """记录目录树的相对路径、大小与 mtime_ns，返回条目数与摘要。"""

    root = Path(root)
    if not root.is_dir():
        return {"root": str(root), "entry_count": 0, "sha256": _sha256(b"[]\n"), "missing": True}
    entries: list[list[Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = Path(current) / name
            try:
                metadata = path.lstat()
            except OSError:
                continue
            entries.append(
                [
                    path.relative_to(root).as_posix(),
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                    int(metadata.st_mode & 0o7777),
                ]
            )
            if len(entries) > max_entries:
                raise SealRehearsalError(f"不变性快照条目超过上限：{root}")
    return {
        "root": str(root),
        "entry_count": len(entries),
        "sha256": _sha256(_canonical(entries)),
        "missing": False,
    }


def upper_inventory(upper_root: Path, *, max_entries: int = MAX_UPPER_INVENTORY) -> list[dict[str, Any]]:
    """列出 overlay upper 目录里的新增／修改文件（含内容摘要）。"""

    inventory: list[dict[str, Any]] = []
    upper_root = Path(upper_root)
    if not upper_root.is_dir():
        return inventory
    for current, directories, files in os.walk(upper_root, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = Path(current) / name
            metadata = path.lstat()
            entry: dict[str, Any] = {
                "path": path.relative_to(upper_root).as_posix(),
                "size": int(metadata.st_size),
                "mode": int(metadata.st_mode & 0o7777),
            }
            if os.path.islink(path):
                entry["kind"] = "symlink"
            elif os.path.isfile(path) and (metadata.st_mode & 0o170000) == 0o100000:
                entry["kind"] = "file"
                entry["sha256"] = _file_sha256(path)
            else:
                # overlay 的 whiteout（字符设备 0:0）等特殊项只记类型。
                entry["kind"] = "special"
            inventory.append(entry)
            if len(inventory) > max_entries:
                raise SealRehearsalError("upper 清单条目超过上限。")
    return inventory


# ---------------------------------------------------------------------------
# driver：在 mount namespace 内执行
# ---------------------------------------------------------------------------


def _mount_overlay(target: Path, upper: Path, work: Path) -> None:
    upper.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    options = f"lowerdir={target},upperdir={upper},workdir={work}"
    completed = subprocess.run(
        ["mount", "-t", "overlay", "overlay", "-o", options, str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SealRehearsalError(
            f"OverlayFS 挂载失败：{target}：{completed.stderr.strip()[:400]}"
        )


def _tail(text: str) -> str:
    return text[-MAX_OUTPUT_TAIL:] if len(text) > MAX_OUTPUT_TAIL else text


def _bind_alias(source: Path, alias: Path) -> None:
    completed = subprocess.run(
        ["mount", "--bind", str(source), str(alias)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SealRehearsalError(
            f"bind 别名重挂失败：{alias}：{completed.stderr.strip()[:400]}"
        )


LEGAL_STOP_STATUSES = frozenset({"approval_required"})


def _approval_stop_status(stdout_tail: str) -> str | None:
    """从动作 stdout 末尾的 JSON 里取合法停靠状态（approval_required），否则 None。"""

    text = stdout_tail.strip()
    if not text.endswith("}"):
        return None
    start = text.rfind("\n{")
    candidate = text[start + 1 :] if start >= 0 else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    status = payload.get("status") if isinstance(payload, Mapping) else None
    return status if status in LEGAL_STOP_STATUSES else None


def run_driver(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """namespace 内主体：挂 overlay、逐动作执行、取最终 status。

    只对数据根做一次 OverlayFS；其余 bind 别名重新 bind 到 overlay 后的数据根，
    保证正式工具经两条路径（如 ``/root/oauth-capture`` 与
    ``/root/docker/capture-cli/data``）看到的是同一份合并视图，与生产同构。
    """

    roots = [Path(item) for item in arguments["overlay_roots"]]
    upper_root = Path(arguments["upper_root"])
    _mount_overlay(roots[0], upper_root / "upper", upper_root / "work")
    for alias in roots[1:]:
        _bind_alias(roots[0], alias)
    attempt_root = Path(arguments["attempt_root"])
    actions = list(arguments["actions"])
    results: list[dict[str, Any]] = []
    status = "passed"
    environment = dict(os.environ)
    environment.update(dict(arguments.get("environment", {})))
    environment[REHEARSAL_CONTEXT_ENV] = "1"
    environment.pop(supervisor.CAMPAIGN_RUN_CONTEXT_ENV, None)
    for action in actions:
        command = list(action["command"])
        if action.get("approve"):
            preview = attempt_root / "seal-preview.json"
            if not preview.is_file():
                results.append(
                    {
                        "action_id": action["action_id"],
                        "status": "failed",
                        "returncode": None,
                        "duration_seconds": 0.0,
                        "stderr_tail": "预演到 approve 时 seal-preview.json 不存在。",
                    }
                )
                status = "failed"
                break
            review = json.loads(preview.read_text(encoding="utf-8")).get("review_sha256")
            command = [
                (str(review) if token == REVIEW_PLACEHOLDER else token)
                for token in _normalize_command(command)
            ]
            command = [
                (f"{APPROVE_FLAG}={review}" if token == f"{APPROVE_FLAG}={REVIEW_PLACEHOLDER}" else token)
                for token in command
            ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=float(action["timeout_seconds"]),
                env=environment,
            )
            returncode: int | None = int(completed.returncode)
            stdout_tail = _tail(completed.stdout)
            stderr_tail = _tail(completed.stderr)
        except subprocess.TimeoutExpired as error:
            returncode = None
            stdout_tail = _tail(error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else str(error.stdout or ""))
            stderr_tail = "动作超时。" + _tail(error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else str(error.stderr or ""))
        duration = time.monotonic() - started
        passed = returncode == 0
        legal_stop: str | None = None
        if returncode == 2 and not action.get("approve"):
            # seal 预览与正式监督器一样，以退出码 2 + status=approval_required 停靠：
            # 预览已写出 seal-preview.json 与 review_sha256，等待批准，不是失败。
            legal_stop = _approval_stop_status(stdout_tail)
            if legal_stop is not None and (attempt_root / "seal-preview.json").is_file():
                passed = True
        record = {
            "action_id": action["action_id"],
            "status": "passed" if passed else "failed",
            "returncode": returncode,
            "duration_seconds": round(duration, 3),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "upper_inventory": upper_inventory(upper_root),
        }
        if legal_stop is not None and passed:
            record["legal_stop"] = legal_stop
        results.append(record)
        if not passed:
            status = "failed"
            break
    final: dict[str, Any] | None = None
    if status == "passed" and arguments.get("status_command"):
        completed = subprocess.run(
            list(arguments["status_command"]),
            capture_output=True,
            text=True,
            timeout=float(arguments.get("status_timeout_seconds", 600.0)),
            env=environment,
        )
        try:
            parsed = json.loads(completed.stdout) if completed.returncode == 0 else None
        except json.JSONDecodeError:
            parsed = None
        final = {
            "returncode": int(completed.returncode),
            "status": parsed.get("status") if isinstance(parsed, Mapping) else None,
            "next_command": parsed.get("next_command") if isinstance(parsed, Mapping) else None,
            "stderr_tail": _tail(completed.stderr),
        }
        if completed.returncode != 0 or not isinstance(parsed, Mapping):
            status = "failed"
    return {
        "schema_version": DRIVER_SCHEMA_VERSION,
        "status": status,
        "results": results,
        "final_status": final,
        "upper_inventory": upper_inventory(upper_root),
    }


# ---------------------------------------------------------------------------
# 父进程：前置检查、隔离执行、收据
# ---------------------------------------------------------------------------


def _same_directory(left: Path, right: Path) -> bool:
    try:
        first = left.stat()
        second = right.stat()
    except OSError:
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def preflight(*, data_root: Path, alias_roots: Sequence[Path], upper_root: Path) -> None:
    """只在 Linux root 且工具可用时允许真实 OverlayFS 预演。"""

    if platform.system() != "Linux":
        raise SealRehearsalError("OverlayFS 预演只能在 Linux 上执行。")
    if os.geteuid() != 0:
        raise SealRehearsalError("OverlayFS 预演需要 root。")
    for binary in ("unshare", "mount"):
        if shutil.which(binary) is None:
            raise SealRehearsalError(f"缺少 {binary}。")
    if not data_root.is_absolute() or data_root.is_symlink() or not data_root.is_dir():
        raise SealRehearsalError("数据根必须是绝对、非符号链接的目录。")
    for alias in alias_roots:
        if not alias.is_absolute() or not alias.is_dir():
            raise SealRehearsalError(f"bind 别名不是目录：{alias}")
        if not _same_directory(data_root, alias):
            raise SealRehearsalError(f"bind 别名与数据根不是同一目录：{alias}")
    upper_root = Path(upper_root)
    for covered in (data_root, *alias_roots):
        if upper_root == covered or covered in upper_root.parents:
            raise SealRehearsalError("upper 目录不得位于被覆盖的数据根内。")


def _tool_identity() -> dict[str, Any]:
    # 延迟导入：codex_upgrade 会导入 supervisor，而 supervisor 的门禁又需要本模块。
    try:
        from tools.official_client_capture import codex_upgrade
    except ImportError:  # pragma: no cover
        import codex_upgrade  # type: ignore[import-not-found,no-redef]
    identity = codex_upgrade._tool_identity(include_git=False)
    return {
        key: identity.get(key)
        for key in (
            "files_sha256",
            "policy_version",
            "policy_sha256",
            "wire_producer_sha256",
            "evidence_semantics_sha256",
            "control_sha256",
        )
    }


def _attempt_binding(campaign_dir: Path, candidate_id: str, attempt_id: str) -> tuple[Path, dict[str, Any]]:
    attempt_root = campaign_dir / "candidates" / candidate_id / "attempts" / attempt_id
    attempt_path = attempt_root / "attempt.json"
    attempt = _read_json(attempt_path, "候选 attempt")
    if attempt.get("attempt_id") != attempt_id or attempt.get("candidate_id") != candidate_id:
        raise SealRehearsalError("attempt.json 身份与目录不一致。")
    return attempt_root, {
        "path": attempt_path.relative_to(campaign_dir).as_posix(),
        "sha256": _file_sha256(attempt_path),
        "status": attempt.get("status"),
    }


def _snapshot_roots(campaign_dir: Path, attempt_root: Path, extra_roots: Sequence[Path]) -> dict[str, dict[str, Any]]:
    roots = {
        "attempt": attempt_root,
        "campaign_ledger": campaign_dir / "ledger",
        "campaign_control": campaign_dir / "control",
    }
    for index, extra in enumerate(extra_roots):
        roots[f"extra-{index}"] = Path(extra)
    return {label: snapshot_tree(path) for label, path in roots.items()}


def rehearse(
    *,
    campaign_dir: Path,
    candidate_id: str,
    attempt_id: str,
    actions: Sequence[Mapping[str, Any]],
    data_root: Path,
    alias_roots: Sequence[Path],
    upper_root: Path,
    status_command: Sequence[str],
    snapshot_extra_roots: Sequence[Path] = (),
    skip_action_ids: Iterable[str] = DEFAULT_SKIP_ACTION_IDS,
    environment: Mapping[str, str] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
    driver_runner: Any = None,
) -> dict[str, Any]:
    """执行一次完整预演并把收据写入正式 Campaign 目录。

    ``driver_runner`` 只供测试注入：默认通过 ``unshare -m`` 启动本模块的
    ``--driver`` 模式；注入的替身必须返回与 driver 相同结构的结果。
    """

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    campaign = _read_json(campaign_dir / "campaign.json", "Campaign 清单")
    campaign_id = str(campaign.get("campaign_id", ""))
    if not campaign_id:
        raise SealRehearsalError("Campaign 清单缺少 campaign_id。")
    normalized = normalize_rehearsal_actions(actions, skip_action_ids=skip_action_ids)
    attempt_root, attempt_binding = _attempt_binding(campaign_dir, candidate_id, attempt_id)
    if attempt_binding["status"] != "awaiting_receipts":
        raise SealRehearsalError("只有 awaiting_receipts 的 attempt 才能预演 seal。")
    identity = dict(tool_identity) if tool_identity is not None else _tool_identity()
    before = _snapshot_roots(campaign_dir, attempt_root, snapshot_extra_roots)
    driver_arguments = {
        "overlay_roots": [str(data_root), *[str(item) for item in alias_roots]],
        "upper_root": str(upper_root),
        "attempt_root": str(attempt_root),
        "actions": normalized,
        "status_command": list(status_command),
        "environment": dict(environment or {}),
    }
    started_at = _utc_now()
    if driver_runner is None:
        preflight(data_root=Path(data_root), alias_roots=[Path(item) for item in alias_roots], upper_root=Path(upper_root))
        driver_result = _spawn_driver(driver_arguments)
    else:
        driver_result = driver_runner(driver_arguments)
    after = _snapshot_roots(campaign_dir, attempt_root, snapshot_extra_roots)
    lower_unchanged = before == after
    status = driver_result.get("status")
    final = driver_result.get("final_status")
    passed = (
        status == "passed"
        and lower_unchanged
        and isinstance(final, Mapping)
        and final.get("returncode") == 0
        and isinstance(final.get("status"), str)
    )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "attempt": attempt_binding,
        "tool_identity": identity,
        "actions": [
            {
                "action_id": item["action_id"],
                "item_ids": item["item_ids"],
                "normalized_command": item["normalized_command"],
            }
            for item in normalized
        ],
        "actions_sha256": actions_sha256(normalized),
        "skipped_action_ids": sorted(set(skip_action_ids)),
        "overlay_roots": driver_arguments["overlay_roots"],
        "results": [
            {key: value for key, value in item.items() if key != "upper_inventory"}
            | {
                "upper_inventory_count": len(item.get("upper_inventory", [])),
                "upper_inventory_sha256": _sha256(_canonical(item.get("upper_inventory", []))),
            }
            for item in driver_result.get("results", [])
        ],
        "final_status": final,
        "upper_inventory_count": len(driver_result.get("upper_inventory", [])),
        "upper_inventory_sha256": _sha256(_canonical(driver_result.get("upper_inventory", []))),
        "lower_snapshot_before": before,
        "lower_snapshot_after": after,
        "lower_unchanged": lower_unchanged,
        "live_request_count": 0,
        "started_at_utc": started_at,
        "recorded_at_utc": _utc_now(),
        "status": "passed" if passed else "failed",
    }
    receipt["receipt_sha256"] = _sha256(_canonical(receipt))
    receipt_dir = campaign_dir / RECEIPT_DIRECTORY / attempt_id
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = receipt["recorded_at_utc"].replace(":", "").replace("-", "").replace(".", "")
    path = receipt_dir / f"rehearsal-{stamp}-{receipt['receipt_sha256'][:12]}.json"
    if path.exists():
        raise SealRehearsalError("预演收据路径已存在。")
    descriptor, temporary = tempfile.mkstemp(dir=receipt_dir, prefix=".rehearsal-", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"receipt_path": str(path), **receipt}


def _spawn_driver(driver_arguments: Mapping[str, Any]) -> dict[str, Any]:
    """通过 unshare 建立私有 mount namespace 并运行本模块的 driver 模式。"""

    descriptor, arguments_path = tempfile.mkstemp(prefix=".seal-rehearsal-args-", suffix=".json")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(driver_arguments))
        completed = subprocess.run(
            [
                "unshare",
                "--mount",
                "--propagation",
                "private",
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "--driver",
                arguments_path,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        Path(arguments_path).unlink(missing_ok=True)
    if completed.returncode != 0:
        raise SealRehearsalError(f"预演 driver 失败：{completed.stderr.strip()[-1200:]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SealRehearsalError("预演 driver 输出不是 JSON。") from error
    if not isinstance(payload, Mapping) or payload.get("schema_version") != DRIVER_SCHEMA_VERSION:
        raise SealRehearsalError("预演 driver 输出 schema 非法。")
    return dict(payload)


# ---------------------------------------------------------------------------
# 门禁：正式 seal 批次派发前校验收据
# ---------------------------------------------------------------------------


def load_receipt(path: Path) -> dict[str, Any]:
    payload = _read_json(path, "seal 预演收据")
    unsigned = dict(payload)
    digest = unsigned.pop("receipt_sha256", None)
    if payload.get("schema_version") != SCHEMA_VERSION or digest != _sha256(_canonical(unsigned)):
        raise SealRehearsalError(f"seal 预演收据 schema 或摘要非法：{path.name}")
    return payload


def verify_rehearsal_for_batch(
    campaign_dir: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    attempt_id: str,
    actions: Sequence[Mapping[str, Any]],
    tool_identity: Mapping[str, Any] | None = None,
    now: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    skip_action_ids: Iterable[str] = DEFAULT_SKIP_ACTION_IDS,
) -> dict[str, Any]:
    """返回与当前批次、attempt 与工具身份完全匹配且未过期的通过收据。"""

    campaign_dir = Path(campaign_dir)
    if tool_identity is None:
        tool_identity = _tool_identity()
    normalized = normalize_rehearsal_actions(actions, skip_action_ids=skip_action_ids)
    expected_actions = actions_sha256(normalized)
    _attempt_root, attempt_binding = _attempt_binding(campaign_dir, candidate_id, attempt_id)
    receipt_dir = campaign_dir / RECEIPT_DIRECTORY / attempt_id
    if receipt_dir.is_symlink() or not receipt_dir.is_dir():
        raise SealRehearsalError(
            "正式 seal 批次派发前必须先用 rehearse-candidate-seal 在隔离副本上跑通整条链；"
            "当前 attempt 没有任何预演收据。"
        )
    current = _parse_utc(now or _utc_now(), "当前时间")
    rejections: list[str] = []
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in sorted(receipt_dir.glob("rehearsal-*.json")):
        payload = load_receipt(path)
        candidates.append((_parse_utc(payload.get("recorded_at_utc"), "预演收据时间"), path, payload))
    for recorded, path, payload in sorted(candidates, key=lambda item: item[0], reverse=True):
        problems: list[str] = []
        if payload.get("status") != "passed":
            problems.append("status 不是 passed")
        if payload.get("campaign_id") != campaign_id or payload.get("candidate_id") != candidate_id or payload.get("attempt_id") != attempt_id:
            problems.append("Campaign／candidate／attempt 身份不匹配")
        if (payload.get("attempt") or {}).get("sha256") != attempt_binding["sha256"]:
            problems.append("attempt.json 摘要已变化")
        recorded_identity = payload.get("tool_identity") or {}
        for key in ("policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "files_sha256"):
            if recorded_identity.get(key) != tool_identity.get(key):
                problems.append(f"工具身份 {key} 与预演时不同")
        if payload.get("actions_sha256") != expected_actions:
            problems.append("动作序列与预演不同")
        if payload.get("lower_unchanged") is not True:
            problems.append("预演期间正式目录发生变化")
        if payload.get("live_request_count") != 0:
            problems.append("预演声明了 live 请求")
        if (current - recorded).total_seconds() > ttl_seconds:
            problems.append("预演收据已超过有效期")
        if not problems:
            return {"path": str(path), "sha256": _file_sha256(path), **payload}
        rejections.append(f"{path.name}: " + "；".join(problems))
    raise SealRehearsalError(
        "没有与当前批次匹配的通过预演收据：" + " | ".join(rejections[:4])
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Candidate seal 端到端隔离预演")
    parser.add_argument("--driver", type=Path, help="内部：namespace 内 driver 参数文件")
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--candidate-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--action-plan", type=Path, help="codex-upgrade-vc-action-plan/v1，与正式批次同一份")
    parser.add_argument("--data-root", type=Path, default=Path("/root/docker/capture-cli/data"))
    parser.add_argument("--alias-root", type=Path, action="append", default=[])
    parser.add_argument("--upper-root", type=Path)
    parser.add_argument("--skip-action", action="append", default=list(DEFAULT_SKIP_ACTION_IDS))
    parser.add_argument("--status-timeout-seconds", type=float, default=600.0)
    return parser


def _load_action_plan(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path, "VC 动作计划")
    actions = payload.get("actions")
    if payload.get("schema_version") != "codex-upgrade-vc-action-plan/v1" or not isinstance(actions, list):
        raise SealRehearsalError("动作计划 schema 非法或缺少 actions。")
    return [dict(item) for item in actions]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.driver is not None:
        driver_arguments = _read_json(Path(arguments.driver), "driver 参数")
        result = run_driver(driver_arguments)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    required = ("campaign_dir", "candidate_id", "attempt_id", "action_plan", "upper_root")
    missing = [name for name in required if getattr(arguments, name) is None]
    if missing:
        raise SystemExit(f"缺少参数：{', '.join('--' + name.replace('_', '-') for name in missing)}")
    tool_root = Path(__file__).resolve().parent
    status_command = [
        sys.executable,
        str(tool_root / "codex_upgrade.py"),
        "status",
        "--campaign-dir",
        str(Path(arguments.campaign_dir).resolve()),
    ]
    try:
        result = rehearse(
            campaign_dir=arguments.campaign_dir,
            candidate_id=arguments.candidate_id,
            attempt_id=arguments.attempt_id,
            actions=_load_action_plan(arguments.action_plan),
            data_root=Path(arguments.data_root).resolve(),
            alias_roots=[Path(item).resolve() for item in arguments.alias_root],
            upper_root=Path(arguments.upper_root).resolve(),
            status_command=status_command,
            skip_action_ids=arguments.skip_action,
        )
    except SealRehearsalError as error:
        sys.stderr.write(f"seal-rehearsal: fail: {error}\n")
        return 1
    sys.stdout.write(json.dumps({k: v for k, v in result.items() if k not in {"lower_snapshot_before", "lower_snapshot_after"}}, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
