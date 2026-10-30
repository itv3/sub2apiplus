#!/usr/bin/env python3
"""入口日常维护（E4-02）：入口空跑（每次部署后接在后台验证后面的那次、升级开工的那次）开跑之前先做五件事——清 Go 编译
缓存里 6 小时以上没用过的条目、清理单元执行记录库、只留最近几次空跑的演练根与产物、部署留下的旧暂存树每组只留最近两份
（E4-03）、查根盘余量。根盘越过停线（与派发前
守卫 ``guard.sh`` 同一条：已用超过 69%，或可用少于 40 GiB 与参数文件 ``MIN_FREE_GIB`` 中较大的那个）时空跑不起，结论写明原因。

为什么（方案第 8 节「ARM64 磁盘越过停线」、E3-01 发现的问题第 2 条）：

* 根盘越过停线，环境收据拒签、派发前守卫停线。Go 编译缓存是最大的可再生占用（10-03 ARM64 实测 8.9 GiB），按「6 小时以上
  没用过」清旧条目：Go 每用到一个条目，就把它修改时间刷新到一小时以内，刚编过的留着，入口门禁不至于全冷。
* 单元执行记录库只增不删：每次运行都写进本次执行的记录、日志与运行清单；日常化之后每次部署都有一次后台验证和一次空跑。
* 每次空跑建一个演练根（数据根 ``staging/entry-dryrun-<UTC>/``）和一份空跑产物（``<RUNROOT>/entry-dryrun/run-<UTC>/``）。

记录库清理（``gc_record_store``）：

* 保留期（默认 30 天，不得短于承接期限 7 天另加 1 天余量）：保留期内写入的运行清单、执行记录与日志都留着。
* 被引用的运行：在引用根（默认是数据根的 ``control``、``evidence``、``staging``：认证坐标、P0 收据证据根与演练根都在这几处）
  里找登记了本记录库的文档——JSON 对象顶层 ``record_store`` 是本库、带 ``unit_manifest.run_id``，即 Codex 的 pre-A3 v2 认证、
  Claude 的 pre-A3 v3 认证（B5a-10：两客户端同一形状，认证坐标都在 ``control/policy-certification``，演练根里的在 ``staging``）、
  两客户端的 P0 v2 证据，以及从逐单元承接运行签出的认证（``unit_manifest`` 另带承接证明），连同复用时复制的认证与 P0 收据
  证据根里的副本。入口门禁在 RUNROOT 里导出的 P0 证据只是签发收据的原料，不算引用
  （否则每次运行都被自己的产物钉住，记录库永远清不动）。它引用的运行清单与保留期内的运行清单一样整份保留：清单列出的全部记录（本次执行、承接、诊断）。
  保留下来的每条记录，它的日志和它所在运行（承接项的原运行）的清单也留着。pre-A3 v2 认证复核与复用查找、发布认证、P0 收据
  签发与 VC-0 收口要读的文件因此都在原位（E3-02、E3-03 发现的问题）。
* 逐单元承接目录（``unit-inherit/``，B5a-08 合同，B5a-10 纳入清理）：保留下来的运行，它的决策收据
  （``decisions/<run_id>.json``）、撤销登记（``revocations/<run_id>.json``）与消费登记（``consumptions/<run_id>/``）都留着；保留下来的
  决策收据与消费登记引用的证据快照（``evidence/<sha256>.json``，连同快照里再引用的快照，逐层追到底）也留着——消费端按
  决策收据重验形成期证明、按消费登记重放签发时的快照，都要读它们。保留期内写入的同样留着；其余删掉。证据快照与日志一样按
  内容寻址、发布方遇到同名文件刷新修改时间，清理同样先移走再复查修改时间。
* 其余写入时间早于保留期的清单、记录与日志删掉；发布中断留下的临时文件（``.*.tmp``）超过一天删掉。目录一律不删：发布方先建
  目录再写文件，删目录会和发布抢。
* 与发布并发：同样的日志内容寻址到同一个文件，发布方遇到已有的同名文件时刷新它的修改时间（``unit_records._publish``）。清理先
  把文件改名移走，再看修改时间，移走前后被刷新过的放回原处，没有的才删；上次清理中断留下的移走文件，原名空着就放回、交给
  本次重新判定。两次清理不同时跑（记录库下的 ``.gc.lock``）。

空跑留存（``prune_dryruns``）：按 UTC 时间戳只留最近 5 次的演练根与空跑产物，在跑的空跑不动。

旧暂存树（``prune_superseded_staging``，E4-03）：部署脚本每次部署前把上一份暂存树（数据根 ``staging/<名>-managed-tools``）改名为
``<名>.superseded-<UTC>[-<进程号>]`` 留下，攒到 10-03 共 97 份、11.8 GiB。部署只用最新那份，部署收据只绑定数据根受管树的
摘要，收据、账本与驱动安装记录都不引用旧副本（10-03 只读核实），内容可从部署源仓库按提交重新检出。按名字分组、按时间戳
每组只留最近 2 份（对比最近两次部署用）；同名的当前暂存树不在时整组不动（布局异常，留给人看）。

部署备份与事务目录（``prune_deploy_backups``，磁盘水位保护）：每次受监督部署在数据根 ``control/`` 留一份
``<根名>-backup-before-<摘要前缀>-<UTC>-<随机串>``（受管工具、断言准备器等的回滚备份，约 35～40 MiB），在 ``docs/`` 留一份文档事务目录
``.codex-<版本号>-deploy-<UTC>-<随机串>`` 或 ``.managed-deploy-…``。部署脚本没有回收，上一版一轮累计 95 个备份 3.7 GiB、133 个事务目录 625 MiB，
根盘清理后 53%、VC-6 构建后涨到 66%，收尾门禁期间一度 67%（距停线约 4 GB），靠人工手顺腾盘。本函数把手顺固化：

* 范围：只认 ``control/`` 下的**目录**。根名段取自受管根的目录名，可含下划线（如 ``official_client_control``，一份约 1.6 MiB）；
  受管工具根的备份固定叫 ``managed-tools``。断言准备器的备份 ``assertion-preparer-backup-before-…`` 是单个小文件（每份约 14 KB，
  实测 347 份合计约 5 MiB），不是目录，不在回收范围：占用可忽略，且“普通文件一律不动”是本函数的安全前提（有测试钉住）；
* 保留期（默认 14 天，不得短于 7 天，``--deploy-backup-retention-days`` 可调）内的一律不动；每组（备份按根名分组，事务目录合成一组）
  另外保留最近 8 个（``--keep-deploy-backups``），部署刚做完回滚还要用；
* 保留期之外的，若引用扫描发现保留期内修改过的文件（``control``、``evidence`` 下的收据、证据、账本）仍写着它的名字，也不动
  （指南的身份裁定会读“attempt 开始后第一份部署”的回滚备份，这类引用靠名字扫描保护）；其余才回收；
* 回收先改名成 ``.gc-trash-<原名>`` 再删，中断后下次先清掉这类遗留；``--dry-run`` 只算不删。

``disk-watch``（磁盘水位保护）：根盘离停线还有余量时给提前提示，不做任何删除。预警线是停线前 5 个百分点（缺省，即已用 ≥ 64%）
或可用不足 45 GiB（停线 40 GiB 加 5 GiB），打印距停线的余量和**可回收清单**（各项 dry-run 的结果：Go 编译缓存旧条目、旧空跑、旧暂存树、
部署备份与事务目录各能腾出多少）。``vc-pipeline.sh`` 每个阶段起跑前的磁盘检查通过后调用它。``go-cache-check``：本机 Go 编译缓存
超过阈值（缺省 30 GiB）时警告（10-29 本机数据盘因 Go 编译缓存涨到 109G、只剩 7.5G；只警告，退出码恒为 0）。

子命令：``run --data-root --runroot [--record-store] [--reference-root …] [--retention-days] [--keep-dryruns]
[--keep-superseded] [--deploy-backup-retention-days] [--keep-deploy-backups] [--no-deploy-backups] [--go-cache auto|off|<目录>]
[--go-cache-warn-gib] [--min-free-gib] [--dry-run]``：做一遍并打印报告（JSON）；退出码 0 根盘在停线以内，5 越过停线，2 用法错误。
``disk-watch --data-root --runroot [--json] [同上的留存参数]``：只读，打印水位与可回收清单，退出码同上。
``go-cache-check [--go-cache …] [--warn-gib]``：只读，打印缓存大小与警告，退出码 0。
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import importlib
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
KEEP_SUPERSEDED = 2
# 部署备份与事务目录：保留期缺省 14 天（一个版本周期约 10 天，再留余量）、下限 7 天，每组另留最近 8 个。
DEPLOY_BACKUP_RETENTION_DAYS = 14.0
DEPLOY_BACKUP_MIN_RETENTION_DAYS = 7.0
KEEP_DEPLOY_BACKUPS = 8
# 引用扫描里单个文件的大小上限（超过的不看；收据、账本与证据都远小于它）。
DEPLOY_REFERENCE_MAX_BYTES = 4 << 20
# 本机 Go 编译缓存告警阈值（GiB）。
GO_CACHE_WARN_GIB = 30.0
# 根盘预警线：停线前多少个百分点、可用空间比下限多出多少 GiB 时开始提前提示。
WATCH_MARGIN_PERCENT = 5.0
WATCH_MARGIN_GIB = 5.0
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
# 部署留下的回滚备份目录（control/）与文档事务目录（docs/）；时间戳是部署脚本的 utc_stamp().lower()。
# 备份名的根名段取自受管根的目录名（部署脚本的 root_name），目录名里可以有下划线（如 official_client_control），所以允许 ``_``。
DEPLOY_BACKUP = re.compile(r"(?P<kind>[a-z0-9][a-z0-9_-]*)-backup-before-[0-9a-f]{12}-(?P<stamp>\d{8}t\d{6}z)-[0-9a-f]{8}")
DEPLOY_TRANSACTION = re.compile(r"\.(?:codex-\d+|managed)-deploy-(?P<stamp>\d{8}t\d{6}z)-[0-9a-f]{8}")
SUPERSEDED_STAGING = re.compile(r"(?P<base>.+-managed-tools)\.superseded-(?P<stamp>\d{8}t\d{6}z)(?:-\d+)?")
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


def _store_directories(root: Path) -> list[Path]:
    """记录库里放文件的目录：运行清单、日志、记录分桶，以及逐单元承接的证据、决策、撤销与按运行分的消费登记（B5a-10）。"""

    inherit = root / _records().UNIT_INHERIT_DIR
    return [root / "runs", root / "logs", *sorted((root / "records").glob("*")), inherit / "evidence", inherit / "decisions",
            inherit / "revocations", *sorted((inherit / "consumptions").glob("*"))]


def _evidence_refs(value: Any) -> set[str]:
    """JSON 里引用的证据快照摘要：形如 ``{path, sha256}``、路径指向 ``evidence/<sha256>.json`` 的引用（逐单元承接合同的证据索引）。"""

    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            digest, location = item.get("sha256"), item.get("path")
            if (set(item) == {"path", "sha256"} and isinstance(digest, str) and SHA256.fullmatch(digest) and isinstance(location, str)
                    and Path(location).name == f"{digest}.json" and Path(location).parent.name == "evidence"):
                found.add(digest)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _restore_trash(root: Path) -> int:
    """上次清理中断留下的移走文件：原名空着就放回（交给本次重新判定），原名已有（发布方重写了）就删掉移走的那份。"""

    restored = 0
    for directory in _store_directories(root):
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
            # 补跑尝试（优化第 2 项）：补跑记录与它的首次失败记录都留着（补跑仍红的单元，清单 units 里引用的是首次失败那一条）。
            retry_entries = [item for entry in (manifest.get("retries") or []) if isinstance(entry, Mapping)
                             for item in (entry, {"unit_id": entry.get("unit_id"), "record_sha256": entry.get("of"),
                                                  "record_path": entry.get("of_record_path")})]
            for entry in [*(manifest.get("units") or []), *(manifest.get("diagnostic") or []), *retry_entries]:
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

        # 逐单元承接目录（B5a-10）：保留下来的运行的决策、撤销与消费登记，以及它们逐层引用的证据快照。
        inherit = root / records.UNIT_INHERIT_DIR
        kept_runs = {run_id for run_id, path in runs.items() if path in keep or (_mtime(path) or 0.0) >= cutoff}
        decision_files = _plain_files(inherit / "decisions", "*.json")
        revocation_files = _plain_files(inherit / "revocations", "*.json")
        consumption_files = _plain_files(inherit / "consumptions", "*/*.json")
        evidence_files = {path.stem: path for path in _plain_files(inherit / "evidence", "*.json")}
        for path in (*decision_files, *revocation_files):
            if path.stem in kept_runs:
                keep.add(path)
        for path in consumption_files:
            if path.parent.name in kept_runs:
                keep.add(path)
        pending_refs: set[str] = set()
        for path in (*decision_files, *consumption_files):
            if path in keep or (_mtime(path) or 0.0) >= cutoff:
                pending_refs |= _evidence_refs(records._read_json(path))
        kept_evidence: set[str] = set()
        while pending_refs:
            digest = pending_refs.pop()
            if digest in kept_evidence or digest not in evidence_files:
                continue
            kept_evidence.add(digest)
            keep.add(evidence_files[digest])
            pending_refs |= _evidence_refs(records._read_json(evidence_files[digest])) - kept_evidence

        counts: dict[str, dict[str, int]] = {}
        renewed = 0
        for kind, files in (("runs", sorted(runs.values())), ("records", record_files), ("logs", log_files),
                            ("unit_inherit_decisions", decision_files), ("unit_inherit_revocations", revocation_files),
                            ("unit_inherit_consumptions", consumption_files), ("unit_inherit_evidence", sorted(evidence_files.values()))):
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
        for directory in _store_directories(root):
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


# ---------------------------------------------------------------- 部署留下的旧暂存树

def prune_superseded_staging(data_root: Path, *, keep: int = KEEP_SUPERSEDED, dry_run: bool = False) -> dict[str, Any]:
    """部署留下的旧暂存树按名字分组、按时间戳每组只留最近 ``keep`` 份（见模块说明）。同名的当前暂存树不在时整组不动；
    符号链接、普通文件与名字对不上的都不动。"""

    if keep < 0:
        raise HousekeepingError("旧暂存树的留存份数不能是负数")
    staging = Path(data_root) / "staging"
    if not staging.is_dir():
        return {"status": "absent", "keep": keep, "dry_run": dry_run, "groups": {}, "skipped_without_current_tree": [],
                "removed": [], "removed_bytes": 0}
    groups: dict[str, list[tuple[str, Path]]] = {}
    for path in staging.iterdir():
        matched = SUPERSEDED_STAGING.fullmatch(path.name)
        if matched and path.is_dir() and not path.is_symlink():
            groups.setdefault(matched.group("base"), []).append((matched.group("stamp"), path))
    summary: dict[str, dict[str, int]] = {}
    skipped: list[str] = []
    removed: list[str] = []
    removed_bytes = 0
    for base, items in sorted(groups.items()):
        items.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        current = (staging / base).is_dir()
        if not current:
            skipped.append(base)
        doomed = items[keep:] if current else []
        for _stamp, path in doomed:
            removed_bytes += _tree_bytes(path)
            if not dry_run:
                shutil.rmtree(path)
            removed.append(path.name)
        summary[base] = {"total": len(items), "kept": len(items) - len(doomed), "removed": len(doomed)}
    return {"status": "done", "keep": keep, "dry_run": dry_run, "groups": summary, "skipped_without_current_tree": skipped,
            "removed": sorted(removed), "removed_bytes": removed_bytes}


# ---------------------------------------------------------------- 部署备份与事务目录

def _stamp_seconds(stamp: str) -> float:
    """部署名字里的 UTC 时间戳（如 20261029t143005z）→ 秒。"""

    return float(calendar.timegm(time.strptime(stamp.upper(), "%Y%m%dT%H%M%SZ")))


def _deploy_items(data_root: Path) -> list[tuple[str, float, Path]]:
    """数据根里的部署备份（``control/``）与文档事务目录（``docs/``）：``(组名, 部署时间秒, 路径)``。
    符号链接、普通文件与名字对不上的都不算；备份按根名分组，事务目录（不分版本号前缀）合成一组。"""

    found: list[tuple[str, float, Path]] = []
    for sub, pattern in (("control", DEPLOY_BACKUP), ("docs", DEPLOY_TRANSACTION)):
        directory = Path(data_root) / sub
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            matched = pattern.fullmatch(path.name)
            if matched is None or path.is_symlink() or not path.is_dir():
                continue
            group = matched.group("kind") if pattern is DEPLOY_BACKUP else "document-transaction"
            found.append((group, _stamp_seconds(matched.group("stamp")), path))
    return found


def _recent_references(roots: Iterable[Path], names: list[str], cutoff: float) -> dict[str, str]:
    """保留期内（修改时间不早于 ``cutoff``）的文件里还写着哪些候选名字：``名字 → 第一个命中的文件``。
    只看不超过 4 MiB 的普通文件，不跟符号链接，不进 ``SKIP_DIR_*``、部署备份与事务目录自身。"""

    if not names:
        return {}
    pattern = re.compile(b"|".join(re.escape(name.encode()) for name in names))
    found: dict[str, str] = {}
    for root in roots:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root.resolve()):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES and not SKIP_DIR_PATTERN.fullmatch(name)
                           and not DEPLOY_BACKUP.fullmatch(name) and not DEPLOY_TRANSACTION.fullmatch(name)
                           and not name.startswith(TRASH_MARK)]
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime < cutoff or info.st_size > DEPLOY_REFERENCE_MAX_BYTES:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                for match in pattern.finditer(data):
                    found.setdefault(match.group(0).decode(), str(path))
    return found


def prune_deploy_backups(data_root: Path, *, reference_roots: Iterable[Path] | None = None,
                         retention_days: float = DEPLOY_BACKUP_RETENTION_DAYS, keep_latest: int = KEEP_DEPLOY_BACKUPS,
                         now: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """回收保留期之外的部署回滚备份与文档事务目录（见模块说明）。三道闸，缺一不回收：①不在每组最近 ``keep_latest`` 个里；
    ②部署时间早于保留期；③保留期内修改过的 ``reference_roots`` 文件里没有写着它的名字。"""

    if retention_days < DEPLOY_BACKUP_MIN_RETENTION_DAYS:
        raise HousekeepingError(f"部署备份的保留期不得短于 {DEPLOY_BACKUP_MIN_RETENTION_DAYS:g} 天（回滚与身份裁定要读）")
    if keep_latest < 1:
        raise HousekeepingError("每组至少留 1 个部署备份")
    data_root = Path(data_root)
    moment = time.time() if now is None else now
    cutoff = moment - retention_days * 86400
    roots = list(reference_roots) if reference_roots is not None else [data_root / "control", data_root / "evidence"]
    # 上次中断留下的移走目录先收拾掉（原名已不在，内容就是要删的）。
    leftovers = 0
    for sub, pattern in (("control", DEPLOY_BACKUP), ("docs", DEPLOY_TRANSACTION)):
        directory = data_root / sub
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.name.startswith(TRASH_MARK) and pattern.fullmatch(path.name[len(TRASH_MARK):]) and path.is_dir() and not path.is_symlink():
                leftovers += 1
                if not dry_run:
                    shutil.rmtree(path)
    groups: dict[str, list[tuple[float, Path]]] = {}
    for group, seconds, path in _deploy_items(data_root):
        groups.setdefault(group, []).append((seconds, path))
    old: list[tuple[str, Path]] = []
    summary: dict[str, dict[str, int]] = {}
    for group, items in sorted(groups.items()):
        items.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        beyond = [path for seconds, path in items[keep_latest:] if seconds < cutoff]
        summary[group] = {"total": len(items), "newest_kept": min(keep_latest, len(items)), "within_retention": len(items) - min(keep_latest, len(items)) - len(beyond),
                          "candidates": len(beyond), "referenced": 0, "removed": 0}
        old.extend((group, path) for path in beyond)
    references = _recent_references(roots, [path.name for _group, path in old], cutoff)
    protected: dict[str, str] = {}
    removed: list[str] = []
    removed_bytes = 0
    for group, path in sorted(old, key=lambda item: item[1].name):
        if path.name in references:
            summary[group]["referenced"] += 1
            protected[path.name] = references[path.name]
            continue
        removed_bytes += _tree_bytes(path)
        if not dry_run:
            trash = path.with_name(TRASH_MARK + path.name)
            os.rename(path, trash)
            try:
                shutil.rmtree(trash)
            except OSError:
                if not path.exists() and trash.exists():
                    os.rename(trash, path)   # 删不动就放回原名，下次重新判定
                raise
        summary[group]["removed"] += 1
        removed.append(path.name)
    return {"status": "done" if groups else "absent", "retention_days": retention_days, "keep_latest": keep_latest, "dry_run": dry_run,
            "groups": summary, "removed": removed, "removed_bytes": removed_bytes, "protected_by_recent_reference": protected,
            "trash_leftovers_removed": leftovers}


# ---------------------------------------------------------------- 本机 Go 编译缓存告警与根盘预警

def go_cache_check(cache: Path | None, *, warn_gib: float = GO_CACHE_WARN_GIB) -> dict[str, Any]:
    """Go 编译缓存的大小与告警（只读）。10-29 本机数据盘因 Go 编译缓存涨到 109G、只剩 7.5G（99%），清完才回 73%。"""

    if cache is None or not Path(cache).is_dir() or Path(cache).is_symlink():
        return {"status": "absent", "cache": str(cache) if cache else None, "warn_gib": warn_gib, "over": False, "size_bytes": 0,
                "warning": None}
    size = _tree_bytes(Path(cache))
    over = size / 2 ** 30 > warn_gib
    warning = (f"Go 编译缓存 {size / 2 ** 30:.1f} GiB 超过告警阈值 {warn_gib:g} GiB：{cache}；"
               f"门禁开跑前先清（go clean -cache，只是下次编译变慢），免得中途盘满") if over else None
    return {"status": "done", "cache": str(cache), "warn_gib": warn_gib, "over": over, "size_bytes": size, "warning": warning}


def disk_watch(*, data_root: Path, runroot: Path, go_cache: str = "auto", min_free_gib: float = MIN_FREE_GIB,
               retention_days: float = DEPLOY_BACKUP_RETENTION_DAYS, keep_latest: int = KEEP_DEPLOY_BACKUPS,
               keep_dryruns: int = KEEP_DRYRUNS, keep_superseded: int = KEEP_SUPERSEDED, warn_percent: float = WATCH_MARGIN_PERCENT,
               warn_gib: float = WATCH_MARGIN_GIB, disk_path: Path = Path("/"), environment: Mapping[str, str] | None = None,
               reference_roots: Iterable[Path] | None = None) -> dict[str, Any]:
    """根盘水位预警与可回收清单（只读：各项都按 dry-run 算，不删任何东西）。预警线＝停线前 ``warn_percent`` 个百分点，或可用空间
    不足「下限 + ``warn_gib``」。清单列出日常维护（``run``）会回收的各项各能腾出多少，让起跑前的提示能直接说“先腾哪些”。"""

    disk = disk_status(disk_path, min_free_gib=min_free_gib, environment=environment)
    margin_percent = round(disk["max_used_percent"] - disk["used_percent"], 1)
    margin_gib = round(disk["free_gib"] - disk["min_free_gib"], 1)
    over_stop = not disk["ok"]
    early = over_stop or margin_percent <= warn_percent or margin_gib <= warn_gib
    items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    data_root, runroot = Path(data_root), Path(runroot)

    def add(name: str, how: str, action) -> None:
        try:
            result = action()
        except (HousekeepingError, OSError, subprocess.SubprocessError) as error:
            errors[name] = f"{type(error).__name__}: {error}"
            return
        count = len(result.get("removed") or []) if "removed" in result else int(result.get("removed_files", 0))
        items.append({"item": name, "how": how, "count": count, "bytes": int(result.get("removed_bytes", 0))})

    if early:
        add("Go 编译缓存旧条目（6 小时以上没用过）", "housekeeping 自动清",
            lambda: trim_go_cache(go_cache_dir(go_cache), dry_run=True))
        add("旧空跑演练根与产物", "housekeeping 自动清", lambda: prune_dryruns(data_root, runroot, keep=keep_dryruns, dry_run=True))
        add("部署留下的旧暂存树", "housekeeping 自动清", lambda: prune_superseded_staging(data_root, keep=keep_superseded, dry_run=True))
        add(f"部署回滚备份与文档事务目录（早于 {retention_days:g} 天、每组最近 {keep_latest} 个之外、无近期引用）", "housekeeping 自动清",
            lambda: prune_deploy_backups(data_root, reference_roots=reference_roots, retention_days=retention_days, keep_latest=keep_latest,
                                         dry_run=True))
    total = sum(item["bytes"] for item in items)
    warnings: list[str] = []
    if over_stop:
        warnings.append(disk_reason(disk))
    elif early:
        warnings.append(f"根盘已到预警线：已用 {disk['used_percent']}%（停线 {disk['max_used_percent']:g}%，还差 {margin_percent} 个百分点），"
                        f"可用 {disk['free_gib']} GiB（下限 {disk['min_free_gib']:g} GiB，余量 {margin_gib} GiB）")
    return {"schema_version": "disk-watch/v1", "disk": disk, "margin_percent": margin_percent, "margin_gib": margin_gib,
            "warn_percent": warn_percent, "warn_gib": warn_gib, "early_warning": early, "over_stop": over_stop, "warnings": warnings,
            "reclaimable": items, "reclaimable_bytes": total, "errors": errors}


def disk_watch_text(report: Mapping[str, Any]) -> str:
    """``disk_watch`` 的人读文字：没到预警线只有一行，到了再列可回收清单。"""

    disk = report["disk"]
    head = (f"根盘水位：已用 {disk['used_percent']}%（停线 {disk['max_used_percent']:g}%），可用 {disk['free_gib']} GiB"
            f"（下限 {disk['min_free_gib']:g} GiB）；" + ("已越过停线" if report["over_stop"] else "已到预警线" if report["early_warning"] else "余量充足"))
    if not report["early_warning"]:
        return head
    lines = [head, *(f"提示：{warning}" for warning in report["warnings"]), "可回收清单（按 dry-run 估算，未删除任何东西）："]
    for item in report["reclaimable"]:
        lines.append(f"  - {item['item']}：{item['count']} 项，约 {item['bytes'] / 2 ** 20:.0f} MiB（{item['how']}）")
    for name, error in report["errors"].items():
        lines.append(f"  - {name}：估算失败（{error}）")
    lines.append(f"合计约 {report['reclaimable_bytes'] / 2 ** 30:.2f} GiB；执行：bash entry-dryrun.sh housekeeping（记录库另按保留期与引用判定，不在上面的估算里）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 根盘

def _parse_env() -> Any:
    """安全参数解析器；仓库原件与已安装驱动都取同一实现。"""

    name = "parse_env_sibling"
    if name in sys.modules:
        return sys.modules[name]
    for path in (HERE / "parse_env.py", HERE.parent / "arm64_capture_driver" / "driver" / "parse_env.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                return module
    raise HousekeepingError("找不到驱动的安全参数解析器")


def disk_policy(environment: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """唯一工程策略判据：明确 75%＋私有 staging 根＋完整、同客户端的 fixture_only 总账。

    缺省不读总账、不改变正式 69%／30 GiB；工程策略只改变 used 上限，40 GiB 操作员余量仍是硬下限。
    """

    values = dict(environment or {})
    if "ENGINEERING_ROOT_MAX_USED_PERCENT" not in values:
        return None
    try:
        parser = _parse_env()
        root = parser.engineering_root(values)
        spec = parser.client_spec(values)
        ledger_root = parser.plain_path(str(root / "evidence" / "campaigns" / spec["project_ledger_dir_name"]), "工程总账")
        # 受管消费端在已确认的数据根运行；这里只复用其现有总账读侧，不另造合同字段或弱化校验。
        data_path = str(Path(values["D"]))
        sys.path.insert(0, data_path)
        try:
            ledger = importlib.import_module(spec["project_ledger_module"])
        finally:
            sys.path.remove(data_path)
        for directory in (root, root / "evidence", root / "evidence" / "campaigns", ledger_root):
            ledger._private_dir(directory, "工程总账受管目录")
        plan, _raw = ledger._load_plan(ledger_root)
        ledger._require_plan_client(plan, spec["client"], "工程磁盘策略")
        if plan["fixture_only"] is not True:
            raise ValueError("工程磁盘上限要求总账 plan.fixture_only is True")
    except (ImportError, KeyError, OSError, ValueError) as error:
        raise HousekeepingError(f"工程磁盘策略拒绝：{error}") from error
    return {"name": "fixture-only-private-staging/v1", "max_used_percent": 75.0, "entry_root": str(root),
            "client": spec["client"], "ledger_dir": str(ledger_root), "plan_sha256": plan["plan_sha256"]}


def disk_status(path: Path = Path("/"), *, min_free_gib: float = MIN_FREE_GIB,
                environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """根盘余量与停线判定；显式工程策略与 guard 共用完整总账复验，最低余量始终不低于 40 GiB。"""

    policy = disk_policy(environment)
    maximum = MAX_USED_PERCENT if policy is None else policy["max_used_percent"]
    usage = shutil.disk_usage(path)
    used_percent = usage.used * 100 / usage.total
    free_gib = usage.free / 2 ** 30
    need = max(MIN_FREE_GIB, float(min_free_gib))
    ok = used_percent <= maximum and free_gib >= POLICY_FREE_GIB and free_gib >= need
    report = {"path": str(path), "used_percent": round(used_percent, 1), "free_gib": round(free_gib, 1),
              "max_used_percent": maximum, "min_free_gib": need, "ok": ok}
    if policy is not None:
        report["engineering_policy"] = policy
    return report


def disk_reason(disk: Mapping[str, Any]) -> str:
    return (f"根盘越过停线：已用 {disk['used_percent']}%（上限 {disk['max_used_percent']:g}%），可用 {disk['free_gib']} GiB"
            f"（下限 {disk['min_free_gib']:g} GiB）；先腾空间（日常维护已清过 Go 编译缓存旧条目、记录库、旧空跑与部署留下的旧暂存树）")


def run(*, data_root: Path, runroot: Path, record_store: Path | None = None, reference_roots: Iterable[Path] | None = None,
        retention_days: float = DEFAULT_RETENTION_DAYS, keep_dryruns: int = KEEP_DRYRUNS, keep_superseded: int = KEEP_SUPERSEDED,
        go_cache: str = "auto", min_free_gib: float = MIN_FREE_GIB, busy_dryruns: Iterable[str] = (), dry_run: bool = False,
        disk_path: Path = Path("/"), store_now: float | None = None, environment: Mapping[str, str] | None = None,
        deploy_retention_days: float | None = DEPLOY_BACKUP_RETENTION_DAYS, keep_deploy_backups: int = KEEP_DEPLOY_BACKUPS,
        go_cache_warn_gib: float = GO_CACHE_WARN_GIB) -> dict[str, Any]:
    """按顺序做一遍日常维护：Go 编译缓存 → Go 缓存大小告警 → 记录库 → 空跑留存 → 旧暂存树 → 部署备份与事务目录 → 根盘。
    某一项出错只记在报告里，不挡后面几项。``deploy_retention_days`` 为 None 时不回收部署备份与事务目录。
    ``store_now`` 只给记录库清理换一个判断新旧的时刻（验收与演练用），Go 编译缓存始终按墙钟。"""

    data_root, runroot = Path(data_root), Path(runroot)
    disk_policy(environment)   # 资格不合法时先失败，不进行任何清理。
    store = Path(record_store) if record_store is not None else data_root.parent / "unit-records"
    roots = list(reference_roots) if reference_roots is not None else [data_root / "control", data_root / "evidence", data_root / "staging"]
    report: dict[str, Any] = {"schema_version": SCHEMA, "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "dry_run": dry_run}
    steps = (
        ("go_cache", lambda: trim_go_cache(go_cache_dir(go_cache), dry_run=dry_run)),
        ("go_cache_size", lambda: go_cache_check(go_cache_dir(go_cache), warn_gib=go_cache_warn_gib)),
        ("record_store", lambda: gc_record_store(store, reference_roots=roots, retention_hours=retention_days * 24, now=store_now,
                                                 dry_run=dry_run)),
        ("dryruns", lambda: prune_dryruns(data_root, runroot, keep=keep_dryruns, busy=busy_dryruns, dry_run=dry_run)),
        ("superseded_staging", lambda: prune_superseded_staging(data_root, keep=keep_superseded, dry_run=dry_run)),
        *((("deploy_backups", lambda: prune_deploy_backups(data_root, reference_roots=[data_root / "control", data_root / "evidence"],
                                                           retention_days=deploy_retention_days, keep_latest=keep_deploy_backups,
                                                           dry_run=dry_run)),)
          if deploy_retention_days is not None else ()),
    )
    for name, action in steps:
        began = time.monotonic()
        try:
            report[name] = {**action(), "seconds": round(time.monotonic() - began, 1)}
        except (HousekeepingError, OSError, subprocess.SubprocessError) as error:
            report[name] = {"status": "error", "error": f"{type(error).__name__}: {error}", "seconds": round(time.monotonic() - began, 1)}
    report["disk"] = disk_status(disk_path, min_free_gib=min_free_gib, environment=environment)
    # 告警汇总：Go 编译缓存超阈值（只警告，不挡）。
    report["warnings"] = [warning for warning in (report.get("go_cache_size", {}).get("warning"),) if warning]
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
    p_run.add_argument("--keep-superseded", type=int, default=KEEP_SUPERSEDED, help="部署留下的旧暂存树每组留几份（默认 2）")
    p_run.add_argument("--go-cache", default="auto", help="auto（go env GOCACHE）、off 或缓存目录")
    p_run.add_argument("--go-cache-warn-gib", type=float, default=GO_CACHE_WARN_GIB, help="Go 编译缓存超过多少 GiB 就警告（默认 30）")
    p_run.add_argument("--deploy-backup-retention-days", type=float, default=DEPLOY_BACKUP_RETENTION_DAYS,
                       help="部署回滚备份与文档事务目录的保留期（天，默认 14，不得短于 7）")
    p_run.add_argument("--keep-deploy-backups", type=int, default=KEEP_DEPLOY_BACKUPS, help="部署备份每组另外保留最近几个（默认 8）")
    p_run.add_argument("--no-deploy-backups", action="store_true", help="不回收部署回滚备份与文档事务目录")
    p_run.add_argument("--min-free-gib", type=float, default=MIN_FREE_GIB)
    p_run.add_argument("--vc-env", type=Path, default=None, help="本轮安全参数文件；显式工程上限须完整复验私有 fixture_only 总账")
    p_run.add_argument("--dry-run", action="store_true", help="只算不删")
    p_run.add_argument("--store-now-utc", default=None,
                       help="验收与演练用：记录库清理按这个时刻（如 2026-11-15T00:00:00Z）判断新旧；Go 编译缓存始终按墙钟")
    p_watch = sub.add_parser("disk-watch", help="根盘水位预警与可回收清单（只读，不删任何东西）")
    p_watch.add_argument("--data-root", type=Path, required=True)
    p_watch.add_argument("--runroot", type=Path, required=True)
    p_watch.add_argument("--go-cache", default="auto")
    p_watch.add_argument("--min-free-gib", type=float, default=MIN_FREE_GIB)
    p_watch.add_argument("--deploy-backup-retention-days", type=float, default=DEPLOY_BACKUP_RETENTION_DAYS)
    p_watch.add_argument("--keep-deploy-backups", type=int, default=KEEP_DEPLOY_BACKUPS)
    p_watch.add_argument("--keep-dryruns", type=int, default=KEEP_DRYRUNS)
    p_watch.add_argument("--keep-superseded", type=int, default=KEEP_SUPERSEDED)
    p_watch.add_argument("--warn-percent", type=float, default=WATCH_MARGIN_PERCENT, help="停线前多少个百分点开始预警（默认 5）")
    p_watch.add_argument("--warn-gib", type=float, default=WATCH_MARGIN_GIB, help="可用空间比下限多出不足多少 GiB 开始预警（默认 5）")
    p_watch.add_argument("--disk-path", type=Path, default=Path("/"))
    p_watch.add_argument("--vc-env", type=Path, default=None)
    p_watch.add_argument("--json", action="store_true", help="输出 JSON 而不是人读文字")
    p_go = sub.add_parser("go-cache-check", help="本机 Go 编译缓存超阈值时警告（只读，退出码恒为 0）")
    p_go.add_argument("--go-cache", default="auto", help="auto（go env GOCACHE）或缓存目录")
    p_go.add_argument("--warn-gib", type=float, default=GO_CACHE_WARN_GIB)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command == "go-cache-check":
        report = go_cache_check(go_cache_dir(args.go_cache), warn_gib=args.warn_gib)
        print(report["warning"] or (f"Go 编译缓存 {report['size_bytes'] / 2 ** 30:.1f} GiB，未超过 {args.warn_gib:g} GiB"
                                    if report["status"] == "done" else "Go 编译缓存不存在或未启用，不检查"))
        return 0
    for name in ("data_root", "runroot"):
        if not getattr(args, name).is_absolute():
            print(f"日常维护：--{name.replace('_', '-')} 必须是绝对路径", file=sys.stderr)
            return 2
    if args.command == "disk-watch":
        try:
            values = _parse_env().parse(args.vc_env.read_text(encoding="utf-8")) if args.vc_env is not None else None
            watch = disk_watch(data_root=args.data_root, runroot=args.runroot, go_cache=args.go_cache, min_free_gib=args.min_free_gib,
                               retention_days=args.deploy_backup_retention_days, keep_latest=args.keep_deploy_backups,
                               keep_dryruns=args.keep_dryruns, keep_superseded=args.keep_superseded, warn_percent=args.warn_percent,
                               warn_gib=args.warn_gib, disk_path=args.disk_path, environment=values)
        except (HousekeepingError, OSError, ValueError) as error:
            print(f"磁盘水位检查拒绝：{error}", file=sys.stderr)
            return 2
        print(json.dumps(watch, ensure_ascii=False, indent=1) if args.json else disk_watch_text(watch))
        return 5 if watch["over_stop"] else 0
    store_now = None
    if args.store_now_utc is not None:
        try:
            store_now = float(calendar.timegm(time.strptime(args.store_now_utc, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            print(f"日常维护：--store-now-utc 要 YYYY-MM-DDTHH:MM:SSZ：{args.store_now_utc}", file=sys.stderr)
            return 2
    try:
        values = _parse_env().parse(args.vc_env.read_text(encoding="utf-8")) if args.vc_env is not None else None
        report = run(data_root=args.data_root, runroot=args.runroot, record_store=args.record_store, reference_roots=args.reference_root,
                     retention_days=args.retention_days, keep_dryruns=args.keep_dryruns, keep_superseded=args.keep_superseded,
                     go_cache=args.go_cache, environment=values,
                     min_free_gib=args.min_free_gib, dry_run=args.dry_run, store_now=store_now,
                     deploy_retention_days=None if args.no_deploy_backups else args.deploy_backup_retention_days,
                     keep_deploy_backups=args.keep_deploy_backups, go_cache_warn_gib=args.go_cache_warn_gib)
    except (HousekeepingError, OSError, ValueError) as error:
        print(f"日常维护拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["disk"]["ok"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
