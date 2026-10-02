#!/usr/bin/env python3
"""入口日常维护（E4-02）：入口空跑（每次部署后接在后台验证后面的那次、升级开工的那次）开跑之前先做四件事——清 Go 编译
缓存里 6 小时以上没用过的条目、清理单元执行记录库、只留最近几次空跑的演练根与产物、查根盘余量。根盘越过停线（与派发前
守卫 ``guard.sh`` 同一条：已用超过 69%，或可用少于 40 GiB 与参数文件 ``MIN_FREE_GIB`` 中较大的那个）时空跑不起，结论写明原因。

为什么（方案第 8 节「ARM64 磁盘越过停线」、E3-01 发现的问题第 2 条）：

* 根盘越过停线，环境收据拒签、派发前守卫停线。Go 编译缓存是最大的可再生占用（10-03 ARM64 实测 8.9 GiB），按「6 小时以上
  没用过」清旧条目：Go 每用到一个条目，就把它修改时间刷新到一小时以内，刚编过的留着，入口门禁不至于全冷。
* 单元执行记录库只增不删：每次运行都写进本次执行的记录、日志与运行清单；日常化之后每次部署都有一次后台验证和一次空跑。
* 每次空跑建一个演练根（数据根 ``staging/entry-dryrun-<UTC>/``）和一份空跑产物（``<RUNROOT>/entry-dryrun/run-<UTC>/``）。

记录库清理（``gc_record_store``）：

* 保留期（默认 30 天，不得短于承接期限 7 天另加 1 天余量）：保留期内写入的运行清单、执行记录与日志都留着。
* 被引用的运行：在引用根（默认是数据根的 ``control``、``evidence``、``staging``：认证坐标、P0 收据证据根与演练根都在这几处）
  里找登记了本记录库的文档——JSON 对象顶层 ``record_store`` 是本库、带 ``unit_manifest.run_id``，即 pre-A3 v2 认证与 P0 v2
  证据，连同复用时复制的认证与 P0 收据证据根里的副本。入口门禁在 RUNROOT 里导出的 P0 证据只是签发收据的原料，不算引用
  （否则每次运行都被自己的产物钉住，记录库永远清不动）。它引用的运行清单与保留期内的运行清单一样整份保留：清单列出的全部记录（本次执行、承接、诊断）。
  保留下来的每条记录，它的日志和它所在运行（承接项的原运行）的清单也留着。pre-A3 v2 认证复核与复用查找、发布认证、P0 收据
  签发与 VC-0 收口要读的文件因此都在原位（E3-02、E3-03 发现的问题）。
* 其余写入时间早于保留期的清单、记录与日志删掉；发布中断留下的临时文件（``.*.tmp``）超过一天删掉。目录一律不删：发布方先建
  目录再写文件，删目录会和发布抢。
* 与发布并发：同样的日志内容寻址到同一个文件，发布方遇到已有的同名文件时刷新它的修改时间（``unit_records._publish``）。清理先
  把文件改名移走，再看修改时间，移走前后被刷新过的放回原处，没有的才删；上次清理中断留下的移走文件，原名空着就放回、交给
  本次重新判定。两次清理不同时跑（记录库下的 ``.gc.lock``）。

空跑留存（``prune_dryruns``）：按 UTC 时间戳只留最近 5 次的演练根与空跑产物，在跑的空跑不动。

子命令：``run --data-root --runroot [--record-store] [--reference-root …] [--retention-days] [--keep-dryruns]
[--go-cache auto|off|<目录>] [--min-free-gib] [--dry-run]``：做一遍并打印报告（JSON）；退出码 0 根盘在停线以内，5 越过停线，
2 用法错误。
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

SCHEMA = "entry-housekeeping/v1"
HERE = Path(__file__).resolve().parent
GO_UNUSED_HOURS = 6.0
DEFAULT_RETENTION_DAYS = 30.0
GRACE_HOURS = 24.0
KEEP_DRYRUNS = 5
TEMP_MAX_AGE_SECONDS = 86400.0
# 根盘停线：与 guard.sh 的派发前检查同一组数（已用 ≤69%、可用 ≥30 GiB、可用 ≥ 操作员阈值且不低于 40 GiB）。
MAX_USED_PERCENT = 69.0
MIN_FREE_GIB = 40.0
POLICY_FREE_GIB = 30.0
MAX_REFERENCE_BYTES = 16 << 20
GC_LOCK = ".gc.lock"
TRASH_MARK = ".gc-trash-"
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[0-9A-Za-z._-]+")
GO_ENTRY_DIR = re.compile(r"[0-9a-f]{2}")
DRYRUN_ROOT = re.compile(r"entry-dryrun-(\d{8}t\d{6}z)")
DRYRUN_OUTPUT = re.compile(r"run-(\d{8}t\d{6}z)")
GO_CANDIDATES = ("/usr/local/go/bin/go",)
# 扫引用文档时不进的目录：部署留下的受管工具树副本与字节码缓存（只有仓库文件，没有认证与收据）、版本库与前端依赖。
SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__"})
SKIP_DIR_PATTERN = re.compile(r".*-managed-tools(?:\.superseded-.*)?|pycache-.*")


class HousekeepingError(RuntimeError):
    pass


def _records() -> Any:
    """同目录的 unit_records.py（驱动目录与 tools/ci 都是平铺的一组文件，按路径加载）：路径规则与承接期限都取自它。"""

    name = "unit_records_sibling"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "unit_records.py")
    if spec is None or spec.loader is None:
        raise HousekeepingError("找不到同目录的 unit_records.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # dataclass 按模块名回查，先登记再执行
    spec.loader.exec_module(module)
    return module


def _mtime(path: Path) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _size(path: Path) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _plain_files(directory: Path, pattern: str) -> list[Path]:
    """目录下按模式匹配的普通文件（不跟符号链接，不含点开头的临时文件与移走文件）。"""

    if not directory.is_dir() or directory.is_symlink():
        return []
    found = []
    for path in directory.glob(pattern):
        if path.name.startswith("."):
            continue
        with contextlib.suppress(OSError):
            if stat.S_ISREG(os.lstat(path).st_mode):
                found.append(path)
    return sorted(found)


def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            total += _size(Path(dirpath) / name)
    return total


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[bool]:
    """非阻塞的排他锁：拿到给 True，已有人持有给 False（不等）。"""

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------- Go 编译缓存

def go_cache_dir(setting: str = "auto", environ: Mapping[str, str] | None = None) -> Path | None:
    """Go 编译缓存目录：``off`` 不清；给目录就用它；``auto`` 先看环境变量 ``GOCACHE``（入口门禁的白名单环境放行 GO* 变量，
    与它看到的是同一个），没设再取 ``go env GOCACHE``（与入口门禁同一个 HOME）。找不到 go 或缓存被关掉时返回 None。"""

    if setting == "off":
        return None
    if setting != "auto":
        return Path(setting)
    environ = dict(os.environ if environ is None else environ)
    explicit = environ.get("GOCACHE")
    if explicit:
        return None if explicit == "off" else Path(explicit)
    go = shutil.which("go", path=environ.get("PATH")) or next((item for item in GO_CANDIDATES if Path(item).is_file()), None)
    if go is None:
        return None
    try:
        completed = subprocess.run([go, "env", "GOCACHE"], capture_output=True, text=True, timeout=60, env=environ,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return Path(value) if completed.returncode == 0 and value and value != "off" else None


def trim_go_cache(cache: Path | None, *, unused_hours: float = GO_UNUSED_HOURS, now: float | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """删掉 Go 编译缓存里超过 ``unused_hours`` 没用过的条目（两位十六进制子目录下修改时间更早的文件；顶层文件与 fuzz 语料
    不动）。Go 自己的定期修剪也是这样并发删的：被删的条目再被用到只是缓存未命中、重新编译。"""

    report: dict[str, Any] = {"cache": str(cache) if cache else None, "unused_hours": unused_hours, "dry_run": dry_run}
    if cache is None or not Path(cache).is_dir() or Path(cache).is_symlink():
        return {**report, "status": "absent", "removed_files": 0, "removed_bytes": 0, "kept_files": 0, "kept_bytes": 0}
    cutoff = (time.time() if now is None else now) - unused_hours * 3600
    removed = removed_bytes = kept = kept_bytes = 0
    for sub in sorted(Path(cache).iterdir()):
        if not GO_ENTRY_DIR.fullmatch(sub.name) or sub.is_symlink() or not sub.is_dir():
            continue
        with os.scandir(sub) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime >= cutoff:
                    kept += 1
                    kept_bytes += info.st_size
                    continue
                if not dry_run:
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        continue
                removed += 1
                removed_bytes += info.st_size
    return {**report, "status": "done", "removed_files": removed, "removed_bytes": removed_bytes, "kept_files": kept,
            "kept_bytes": kept_bytes}


# ---------------------------------------------------------------- 记录库清理

def _names_store(value: Any, store: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value == str(store):
        return True
    try:
        return Path(value).resolve() == store
    except OSError:
        return False


def find_references(roots: Iterable[Path], store: Path) -> dict[str, list[str]]:
    """引用根里登记了本记录库的文档：``run_id`` → 文档路径。只认 JSON 对象顶层的 ``record_store`` 与 ``unit_manifest.run_id``
    （pre-A3 v2 认证与 P0 v2 证据的形状）；不跟符号链接，记录库本身、超过 16 MiB 的文件与 ``SKIP_DIR_*`` 目录不看。"""

    store = Path(store).resolve()
    marker = b'"record_store"'
    found: dict[str, set[str]] = {}
    for root in roots:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root.resolve()):
            current = Path(dirpath)
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES and not SKIP_DIR_PATTERN.fullmatch(name)
                                 and current / name != store)
            for name in filenames:
                if not name.endswith(".json"):
                    continue
                path = current / name
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REFERENCE_BYTES:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if marker not in data:
                    continue
                try:
                    document = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(document, dict) or not _names_store(document.get("record_store"), store):
                    continue
                reference = document.get("unit_manifest")
                run_id = reference.get("run_id") if isinstance(reference, Mapping) else None
                if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
                    found.setdefault(run_id, set()).add(str(path))
    return {run_id: sorted(paths) for run_id, paths in sorted(found.items())}


def _restore_trash(root: Path) -> int:
    """上次清理中断留下的移走文件：原名空着就放回（交给本次重新判定），原名已有（发布方重写了）就删掉移走的那份。"""

    restored = 0
    for directory in [root / "runs", root / "logs", *sorted((root / "records").glob("*"))]:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.iterdir():
            if not path.name.startswith(".") or TRASH_MARK not in path.name:
                continue
            original = directory / path.name[1:path.name.index(TRASH_MARK)]
            try:
                os.link(path, original)
                restored += 1
            except FileExistsError:
                pass
            except OSError:
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    return restored


def _remove(path: Path, cutoff: float) -> tuple[str, int]:
    """移走再删：改名后修改时间仍早于保留期才删；移走前后被发布方续期过的放回原处。返回（结果，字节数）。"""

    trash = path.with_name(f".{path.name}{TRASH_MARK}{os.getpid()}")
    try:
        os.rename(path, trash)
    except FileNotFoundError:
        return "gone", 0
    info = os.lstat(trash)
    if info.st_mtime >= cutoff:
        with contextlib.suppress(FileExistsError):
            os.link(trash, path)
        os.unlink(trash)
        return "renewed", 0
    os.unlink(trash)
    return "removed", info.st_size


def gc_record_store(store: Path, *, reference_roots: Iterable[Path], retention_hours: float = DEFAULT_RETENTION_DAYS * 24,
                    now: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """按模块说明的规则清理一个记录库，返回报告。``dry_run`` 只算不删。"""

    records = _records()
    minimum = float(records.MAX_AGE_HOURS) + GRACE_HOURS
    if retention_hours < minimum:
        raise HousekeepingError(f"保留期不得短于承接期限另加 1 天（{minimum:g} 小时），收到 {retention_hours:g} 小时")
    report: dict[str, Any] = {"store": str(store), "retention_hours": retention_hours, "dry_run": dry_run}
    root = Path(store)
    if root.is_symlink() or not root.is_dir():
        return {**report, "status": "absent"}
    root = root.resolve()
    now = time.time() if now is None else now
    cutoff = now - retention_hours * 3600
    with _exclusive(root / GC_LOCK) as acquired:
        if not acquired:
            return {**report, "status": "busy", "reason": "另一次清理正在进行"}
        restored = 0 if dry_run else _restore_trash(root)
        api = records.RecordStore(root)
        runs = {path.stem: path for path in _plain_files(root / "runs", "*.json")}
        record_files = _plain_files(root / "records", "*/*.json")
        log_files = _plain_files(root / "logs", "*.log")
        referenced = find_references(reference_roots, root)
        recent = {run_id for run_id, path in runs.items() if (_mtime(path) or 0.0) >= cutoff}
        keep: set[Path] = set()
        missing: list[str] = []

        def keep_record(path: Path) -> None:
            keep.add(path)
            record = records._read_json(path)
            if record is None:
                return
            log = record.get("log") if isinstance(record.get("log"), Mapping) else {}
            digest = log.get("sha256")
            if isinstance(digest, str) and SHA256.fullmatch(digest):
                keep.add(api.log_path(digest))
            run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
            origin = run.get("run_id")
            if isinstance(origin, str) and origin in runs:
                keep.add(runs[origin])

        for run_id in sorted(recent | set(referenced)):
            path = runs.get(run_id)
            if path is None:
                continue
            keep.add(path)
            manifest = records._read_json(path) or {}
            for entry in [*(manifest.get("units") or []), *(manifest.get("diagnostic") or [])]:
                if not isinstance(entry, Mapping):
                    continue
                unit_id, digest = entry.get("unit_id"), entry.get("record_sha256")
                if not isinstance(unit_id, str) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
                    continue
                record_path = api.record_path(unit_id, digest)
                if record_path.is_file():
                    keep_record(record_path)
                else:
                    missing.append(f"{run_id}:{unit_id}")
        for path in record_files:
            if (_mtime(path) or 0.0) >= cutoff:
                keep_record(path)

        counts: dict[str, dict[str, int]] = {}
        renewed = 0
        for kind, files in (("runs", sorted(runs.values())), ("records", record_files), ("logs", log_files)):
            row = {"total": len(files), "kept": 0, "removed": 0, "removed_bytes": 0}
            for path in files:
                if path in keep or (_mtime(path) or 0.0) >= cutoff:
                    row["kept"] += 1
                    continue
                if dry_run:
                    row["removed"] += 1
                    row["removed_bytes"] += _size(path)
                    continue
                outcome, size = _remove(path, cutoff)
                if outcome == "removed":
                    row["removed"] += 1
                    row["removed_bytes"] += size
                elif outcome == "renewed":
                    row["kept"] += 1
                    renewed += 1
            counts[kind] = row
        temporary = 0
        for directory in [root / "runs", root / "logs", *sorted((root / "records").glob("*"))]:
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if path.name.startswith(".") and path.name.endswith(".tmp") and (_mtime(path) or now) < now - TEMP_MAX_AGE_SECONDS:
                    if not dry_run:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(path)
                    temporary += 1
    dangling = sorted(run_id for run_id in referenced if run_id not in runs)
    return {
        **report, "status": "done", "store": str(root), "cutoff_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)),
        "counts": counts, "recent_runs": len(recent), "referenced_runs": len(referenced),
        "references": {run_id: paths[:3] for run_id, paths in list(referenced.items())[:20]},
        "dangling_references": {run_id: referenced[run_id][:3] for run_id in dangling[:10]},
        "kept_runs_missing_records": missing[:10], "renewed_during_gc": renewed, "restored_from_trash": restored,
        "temporary_removed": temporary,
    }


# ---------------------------------------------------------------- 空跑留存

def prune_dryruns(data_root: Path, runroot: Path, *, keep: int = KEEP_DRYRUNS, busy: Iterable[str] = (),
                  dry_run: bool = False) -> dict[str, Any]:
    """只留最近 ``keep`` 次空跑（按演练根与空跑产物目录名里的 UTC 时间戳）；``busy`` 里的时间戳（在跑的空跑）不动。"""

    if keep < 1:
        raise HousekeepingError("至少要留 1 次空跑")
    places: dict[str, list[Path]] = {}
    for directory, pattern in ((Path(data_root) / "staging", DRYRUN_ROOT), (Path(runroot) / "entry-dryrun", DRYRUN_OUTPUT)):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            matched = pattern.fullmatch(path.name)
            if matched and path.is_dir() and not path.is_symlink():
                places.setdefault(matched.group(1), []).append(path)
    busy = set(busy)
    stamps = sorted(places, reverse=True)
    doomed = [stamp for stamp in stamps[keep:] if stamp not in busy]
    removed: list[str] = []
    removed_bytes = 0
    for stamp in doomed:
        for path in sorted(places[stamp]):
            removed_bytes += _tree_bytes(path)
            if not dry_run:
                shutil.rmtree(path)
            removed.append(str(path))
    return {"status": "done", "keep": keep, "dry_run": dry_run, "dryruns": len(stamps),
            "kept": [stamp for stamp in stamps if stamp not in doomed], "removed": removed, "removed_bytes": removed_bytes}


# ---------------------------------------------------------------- 根盘

def disk_status(path: Path = Path("/"), *, min_free_gib: float = MIN_FREE_GIB) -> dict[str, Any]:
    """根盘余量与停线判定（与 guard.sh 同一条：已用 ≤69%、可用 ≥30 GiB、可用 ≥ 操作员阈值且不低于 40 GiB）。"""

    usage = shutil.disk_usage(path)
    used_percent = usage.used * 100 / usage.total
    free_gib = usage.free / 2 ** 30
    need = max(MIN_FREE_GIB, float(min_free_gib))
    ok = used_percent <= MAX_USED_PERCENT and free_gib >= POLICY_FREE_GIB and free_gib >= need
    return {"path": str(path), "used_percent": round(used_percent, 1), "free_gib": round(free_gib, 1),
            "max_used_percent": MAX_USED_PERCENT, "min_free_gib": need, "ok": ok}


def disk_reason(disk: Mapping[str, Any]) -> str:
    return (f"根盘越过停线：已用 {disk['used_percent']}%（上限 {disk['max_used_percent']:g}%），可用 {disk['free_gib']} GiB"
            f"（下限 {disk['min_free_gib']:g} GiB）；先腾空间（日常维护已清过 Go 编译缓存旧条目、记录库与旧空跑）")


def run(*, data_root: Path, runroot: Path, record_store: Path | None = None, reference_roots: Iterable[Path] | None = None,
        retention_days: float = DEFAULT_RETENTION_DAYS, keep_dryruns: int = KEEP_DRYRUNS, go_cache: str = "auto",
        min_free_gib: float = MIN_FREE_GIB, busy_dryruns: Iterable[str] = (), dry_run: bool = False,
        disk_path: Path = Path("/"), store_now: float | None = None) -> dict[str, Any]:
    """按顺序做一遍日常维护：Go 编译缓存 → 记录库 → 空跑留存 → 根盘。某一项出错只记在报告里，不挡后面几项。
    ``store_now`` 只给记录库清理换一个判断新旧的时刻（验收与演练用），Go 编译缓存始终按墙钟。"""

    data_root, runroot = Path(data_root), Path(runroot)
    store = Path(record_store) if record_store is not None else data_root.parent / "unit-records"
    roots = list(reference_roots) if reference_roots is not None else [data_root / "control", data_root / "evidence", data_root / "staging"]
    report: dict[str, Any] = {"schema_version": SCHEMA, "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "dry_run": dry_run}
    steps = (
        ("go_cache", lambda: trim_go_cache(go_cache_dir(go_cache), dry_run=dry_run)),
        ("record_store", lambda: gc_record_store(store, reference_roots=roots, retention_hours=retention_days * 24, now=store_now,
                                                 dry_run=dry_run)),
        ("dryruns", lambda: prune_dryruns(data_root, runroot, keep=keep_dryruns, busy=busy_dryruns, dry_run=dry_run)),
    )
    for name, action in steps:
        began = time.monotonic()
        try:
            report[name] = {**action(), "seconds": round(time.monotonic() - began, 1)}
        except (HousekeepingError, OSError, subprocess.SubprocessError) as error:
            report[name] = {"status": "error", "error": f"{type(error).__name__}: {error}", "seconds": round(time.monotonic() - began, 1)}
    report["disk"] = disk_status(disk_path, min_free_gib=min_free_gib)
    report["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return report


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="做一遍日常维护并打印报告")
    p_run.add_argument("--data-root", type=Path, required=True)
    p_run.add_argument("--runroot", type=Path, required=True)
    p_run.add_argument("--record-store", type=Path, default=None, help="单元执行记录库（默认数据根上一级的 unit-records，与入口门禁相同）")
    p_run.add_argument("--reference-root", type=Path, action="append", default=None,
                       help="找引用文档的目录，可重复（默认数据根的 control、evidence、staging）")
    p_run.add_argument("--retention-days", type=float, default=DEFAULT_RETENTION_DAYS)
    p_run.add_argument("--keep-dryruns", type=int, default=KEEP_DRYRUNS)
    p_run.add_argument("--go-cache", default="auto", help="auto（go env GOCACHE）、off 或缓存目录")
    p_run.add_argument("--min-free-gib", type=float, default=MIN_FREE_GIB)
    p_run.add_argument("--dry-run", action="store_true", help="只算不删")
    p_run.add_argument("--store-now-utc", default=None,
                       help="验收与演练用：记录库清理按这个时刻（如 2026-11-15T00:00:00Z）判断新旧；Go 编译缓存始终按墙钟")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    for name in ("data_root", "runroot"):
        if not getattr(args, name).is_absolute():
            print(f"日常维护：--{name.replace('_', '-')} 必须是绝对路径", file=sys.stderr)
            return 2
    store_now = None
    if args.store_now_utc is not None:
        try:
            store_now = float(calendar.timegm(time.strptime(args.store_now_utc, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            print(f"日常维护：--store-now-utc 要 YYYY-MM-DDTHH:MM:SSZ：{args.store_now_utc}", file=sys.stderr)
            return 2
    report = run(data_root=args.data_root, runroot=args.runroot, record_store=args.record_store, reference_roots=args.reference_root,
                 retention_days=args.retention_days, keep_dryruns=args.keep_dryruns, go_cache=args.go_cache,
                 min_free_gib=args.min_free_gib, dry_run=args.dry_run, store_now=store_now)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["disk"]["ok"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
