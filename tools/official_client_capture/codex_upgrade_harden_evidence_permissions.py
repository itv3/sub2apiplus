"""两步式证据权限收口 ``harden-evidence-permissions``（方案 A3a）。

``preview`` 只读：枚举 attempt 全部证据根（attempt 目录内证据与 Job 证据根，容器路径
按冻结 CAPTURE_ROOT 映射到宿主），记录每个条目的当前 mode、目标 mode（目录 0700、
文件 0600）、大小与逐文件内容摘要，输出变更摘要与 ``review_sha256``，写入 Campaign 的
``control/evidence-permissions/<attempt_id>/preview-NN.json``。

``apply --approve-sha256``：持 Campaign 排他锁；重新快照并要求逐文件内容摘要与预览
逐字相等（证据在预览后被改过即失败关闭）；只对 mode 不等于目标的条目做 chmod，不做
任何内容写入与网络访问；再次快照证明内容未变且全部达到目标 mode；收据写入同一独立
目录，不回写 ``attempt.json``。中断后重跑幂等：已全部达标且收据存在即返回既有收据。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_evidence_permissions as evidence_permissions
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout

PREVIEW_SCHEMA = "evidence-permission-hardening-preview/v1"
APPLY_SCHEMA = "evidence-permission-hardening-apply/v1"
RECEIPT_DIR = Path("control") / "evidence-permissions"
PREVIEW_RE = re.compile(r"^preview-(\d{2})\.json$")
APPLY_RE = re.compile(r"^apply-(\d{2})\.json$")
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class HardenError(RuntimeError):
    """证据根不可信、内容漂移或批准摘要不符。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise HardenError(f"收据已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HardenError(f"{label}不是可信普通文件：{path}")
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise HardenError(f"{label}顶层必须是对象")
    return payload


def _evidence_roots(campaign_dir: Path, attempt_id: str) -> tuple[Path, list[Path], dict[str, Any]]:
    campaign = closeout._trusted_directory(Path(campaign_dir), "Campaign")
    manifest, _raw = closeout._load_json(campaign / "campaign.json", "Campaign 清单")
    if not closeout.SAFE_ID_RE.fullmatch(attempt_id):
        raise HardenError("attempt_id 非法")
    attempt_root = closeout._trusted_directory(campaign / "official" / "attempts" / attempt_id, "official attempt")
    attempt, _attempt_raw = closeout._load_json(attempt_root / "attempt.json", "attempt")
    configuration = manifest.get("configuration") or {}
    capture_root = Path(str(configuration.get("capture_root", "")))
    host_data_root = closeout._formal_host_data_root(campaign)
    roots: list[Path] = []
    for value in attempt.get("evidence_roots", []):
        candidate = Path(str(value))
        if candidate.is_absolute() and (candidate == host_data_root or host_data_root in candidate.parents):
            mapped = candidate
        else:
            mapped = closeout._map_container_evidence_root(value, capture_root=capture_root, host_data_root=host_data_root)
        if mapped.is_symlink() or not mapped.is_dir():
            raise HardenError(f"证据根缺失或不可信：{mapped}")
        if mapped not in roots:
            roots.append(mapped)
    if not roots:
        raise HardenError("attempt 没有证据根")
    return attempt_root, roots, attempt


def _snapshot(roots: list[Path], *, with_content: bool) -> list[dict[str, Any]]:
    """枚举证据根下全部条目；符号链接、特殊文件、硬链接与非法属主一律失败关闭。

    属主边界与 seal 侧 ``codex_upgrade_evidence_permissions`` 完全相同：目录与普通文件必须归当前
    用户；只有 capture-cli 内 tcpdump 固定身份写出的 ``traffic.pcap``／``egress.pcap`` 允许保留
    ``100:102``。uid／gid 进入条目并参与 mode 摘要，收口前后二次核验属主未变。
    """

    entries: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted([root, *root.rglob("*")], key=lambda item: str(item)):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise HardenError(f"证据含符号链接：{path}")
            if stat.S_ISDIR(metadata.st_mode):
                kind, target = "directory", DIRECTORY_MODE
            elif stat.S_ISREG(metadata.st_mode):
                kind, target = "file", FILE_MODE
                if metadata.st_nlink != 1:
                    raise HardenError(f"证据文件存在边界外硬链接：{path}")
            else:
                raise HardenError(f"证据含特殊文件：{path}")
            if not evidence_permissions._owner_allowed(path, kind, metadata):
                raise HardenError(f"证据属主不是当前用户，也不在 tcpdump 固定边界内：{path}")
            record: dict[str, Any] = {
                "path": str(path),
                "kind": kind,
                "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                "target_mode": format(target, "04o"),
                "bytes": metadata.st_size if kind == "file" else 0,
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
            }
            if with_content and kind == "file":
                record["sha256"] = _file_sha256(path)
            entries.append(record)
    return entries


def _content_digest(entries: list[dict[str, Any]]) -> str:
    return _fingerprint([{"path": e["path"], "bytes": e["bytes"], "sha256": e.get("sha256")} for e in entries if e["kind"] == "file"])


def _mode_digest(entries: list[dict[str, Any]]) -> str:
    return _fingerprint(
        [{"path": e["path"], "kind": e["kind"], "mode": e["mode"], "uid": e.get("uid"), "gid": e.get("gid")} for e in entries]
    )


def _owner_digest(entries: list[dict[str, Any]]) -> str:
    return _fingerprint([{"path": e["path"], "uid": e.get("uid"), "gid": e.get("gid")} for e in entries])


def _receipt_dir(campaign_dir: Path, attempt_id: str) -> Path:
    return Path(campaign_dir) / RECEIPT_DIR / attempt_id


def _latest(directory: Path, pattern: re.Pattern[str]) -> tuple[int, Path | None]:
    if not directory.is_dir():
        return 0, None
    best = 0
    best_path: Path | None = None
    for child in directory.iterdir():
        match = pattern.fullmatch(child.name)
        if match and int(match.group(1)) > best:
            best, best_path = int(match.group(1)), child
    return best, best_path


def preview(campaign_dir: Path, attempt_id: str) -> dict[str, Any]:
    attempt_root, roots, attempt = _evidence_roots(campaign_dir, attempt_id)
    entries = _snapshot(roots, with_content=True)
    changes = [e for e in entries if e["mode"] != e["target_mode"]]
    receipt_dir = _receipt_dir(campaign_dir, attempt_id)
    index, _previous = _latest(receipt_dir, PREVIEW_RE)
    payload: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA,
        "index": index + 1,
        "campaign_id": attempt.get("campaign_id"),
        "attempt_id": attempt_id,
        "attempt_sha256": _file_sha256(attempt_root / "attempt.json"),
        "evidence_roots": [str(r) for r in roots],
        "entry_count": len(entries),
        "change_count": len(changes),
        "change_summary": {
            "directories": sum(1 for e in changes if e["kind"] == "directory"),
            "files": sum(1 for e in changes if e["kind"] == "file"),
        },
        "before_mode_sha256": _mode_digest(entries),
        "content_sha256": _content_digest(entries),
        "entries": entries,
        "created_at_utc": _utc_now(),
    }
    payload["review_sha256"] = _fingerprint({k: v for k, v in payload.items() if k != "created_at_utc"})
    _write_once(receipt_dir / f"preview-{payload['index']:02d}.json", payload)
    return payload


def _campaign_lock(campaign_dir: Path) -> int:
    lock_path = Path(campaign_dir) / ".campaign.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise HardenError("Campaign 锁文件身份不可信")
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def apply(campaign_dir: Path, attempt_id: str, *, approve_sha256: str) -> dict[str, Any]:
    receipt_dir = _receipt_dir(campaign_dir, attempt_id)
    if not receipt_dir.is_dir():
        raise HardenError("没有可批准的预览；先执行 preview")
    matched: tuple[int, Path, dict[str, Any]] | None = None
    for child in sorted(receipt_dir.iterdir()):
        match = PREVIEW_RE.fullmatch(child.name)
        if not match:
            continue
        payload = _read_json(child, "权限预览")
        if payload.get("review_sha256") == approve_sha256:
            matched = (int(match.group(1)), child, payload)
    if matched is None:
        raise HardenError("批准摘要与任何预览都不一致")
    index, preview_path, preview_payload = matched
    apply_index, apply_path = _latest(receipt_dir, APPLY_RE)
    for child in sorted(receipt_dir.iterdir()):
        if APPLY_RE.fullmatch(child.name):
            existing = _read_json(child, "权限收口收据")
            if existing.get("preview_index") == index and existing.get("status") == "applied":
                # 同一预览已收口：幂等返回既有收据，不重复写。
                return existing
    descriptor = _campaign_lock(Path(campaign_dir))
    try:
        _attempt_root, roots, _attempt = _evidence_roots(campaign_dir, attempt_id)
        before = _snapshot(roots, with_content=True)
        if _content_digest(before) != preview_payload["content_sha256"]:
            raise HardenError("证据内容在预览后发生变化，拒绝收口")
        expected_paths = [e["path"] for e in preview_payload["entries"]]
        if [e["path"] for e in before] != expected_paths:
            raise HardenError("证据条目集合在预览后发生变化，拒绝收口")
        changed: list[dict[str, str]] = []
        for entry in before:
            if entry["mode"] != entry["target_mode"]:
                os.chmod(entry["path"], int(entry["target_mode"], 8), follow_symlinks=False)
                changed.append({"path": entry["path"], "from": entry["mode"], "to": entry["target_mode"]})
        after = _snapshot(roots, with_content=True)
        if _content_digest(after) != preview_payload["content_sha256"]:
            raise HardenError("收口后证据内容摘要变化，metadata-only 前提被破坏")
        if _owner_digest(after) != _owner_digest(before):
            raise HardenError("收口后证据属主变化，只允许改 mode")
        if any(e["mode"] != e["target_mode"] for e in after):
            raise HardenError("收口后仍有条目未达到目标权限")
        payload = {
            "schema_version": APPLY_SCHEMA,
            "index": apply_index + 1,
            "preview_index": index,
            "preview_sha256": _file_sha256(preview_path),
            "approved_sha256": approve_sha256,
            "campaign_id": preview_payload.get("campaign_id"),
            "attempt_id": attempt_id,
            "attempt_sha256": preview_payload.get("attempt_sha256"),
            "evidence_roots": preview_payload.get("evidence_roots"),
            "before_mode_sha256": _mode_digest(before),
            "after_mode_sha256": _mode_digest(after),
            "content_sha256_before": _content_digest(before),
            "content_sha256_after": _content_digest(after),
            "content_unchanged": True,
            "changed_count": len(changed),
            "changed": changed,
            "network_access": "none",
            "status": "applied",
            "applied_at_utc": _utc_now(),
        }
        payload["receipt_sha256"] = _fingerprint({k: v for k, v in payload.items() if k != "applied_at_utc"})
        _write_once(receipt_dir / f"apply-{payload['index']:02d}.json", payload)
        return payload
    finally:
        os.close(descriptor)


def replay(campaign_dir: Path, attempt_id: str) -> dict[str, Any]:
    """只读重放：最新 apply 收据绑定的预览存在、内容摘要仍相等、当前全部达标。"""

    receipt_dir = _receipt_dir(campaign_dir, attempt_id)
    _apply_index, apply_path = _latest(receipt_dir, APPLY_RE)
    if apply_path is None:
        raise HardenError("没有权限收口收据")
    applied = _read_json(apply_path, "权限收口收据")
    preview_path = receipt_dir / f"preview-{int(applied['preview_index']):02d}.json"
    if _file_sha256(preview_path) != applied.get("preview_sha256"):
        raise HardenError("权限收口收据绑定的预览漂移")
    _attempt_root, roots, _attempt = _evidence_roots(campaign_dir, attempt_id)
    current = _snapshot(roots, with_content=True)
    problems: list[str] = []
    if _content_digest(current) != applied.get("content_sha256_after"):
        problems.append("证据内容摘要与收口后不一致")
    if any(e["mode"] != e["target_mode"] for e in current):
        problems.append("存在未达标权限条目")
    return {"status": "passed" if not problems else "failed", "problems": problems, "apply_receipt": str(apply_path), "entry_count": len(current)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="两步式证据权限收口：preview 只读，apply 需批准摘要。")
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("preview", "apply", "replay"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--campaign-dir", type=Path, required=True)
        sub.add_argument("--attempt-id", required=True)
        if name == "apply":
            sub.add_argument("--approve-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action == "preview":
            result = preview(arguments.campaign_dir, arguments.attempt_id)
            result = {k: v for k, v in result.items() if k != "entries"}
        elif arguments.action == "apply":
            result = apply(arguments.campaign_dir, arguments.attempt_id, approve_sha256=arguments.approve_sha256)
            result = {k: v for k, v in result.items() if k != "changed"}
        else:
            result = replay(arguments.campaign_dir, arguments.attempt_id)
    except (HardenError, closeout.VC0CloseoutError, OSError, ValueError) as error:
        print(f"证据权限收口失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status", "passed") in {"passed", "applied"} or "review_sha256" in result else 2


if __name__ == "__main__":
    sys.exit(main())
