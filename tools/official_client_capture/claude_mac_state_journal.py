"""有界私有文件状态的真实 journal；本模块不启动产品或授予正式来源权。

首次 before、冻结清单和 WAL 永不覆盖。受管变更须先持久化完整意图，再执行或
确认真实文件系统结果。恢复只接受已知状态；未记录的产品／外部写入不猜测来源，
保留现场并返回异常。原子替换的 stage/trash 留在私有 journal 根内，供同 nonce
中断恢复与审核使用，不把 before 文件复制成 after 原件。
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence


SCHEMA = "claude-private-state-journal/v1"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
LOCK_NAME = ".claude-private-state.lock"


class StateJournalError(ValueError):
    """内部状态合同不满足；不能转为产品成功或零请求。"""


class StateConflict(StateJournalError):
    """状态或来源未知，保留现场，不覆盖为 before。"""


class StateLockBusy(StateJournalError):
    """同一私有根已有持锁者，不等待或启动另一个产品。"""


@dataclass(frozen=True)
class SnapshotLimits:
    """限制目录备份范围；不提供无限制或整个 HOME 的快捷路径。"""

    max_paths: int = 32
    max_depth: int = 8
    max_entries: int = 512
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_wal_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        caps = (64, 16, 4096, 8 * 1024 * 1024, 32 * 1024 * 1024, 128 * 1024 * 1024)
        for (name, value), cap in zip(asdict(self).items(), caps):
            if type(value) is not int or not 1 <= value <= cap:
                raise StateJournalError(f"{name} 超出有界范围")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StateJournalError(message)


def _relative(value: str) -> str:
    _require(isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 1024, "状态相对路径非法")
    parts = value.split("/")
    _require(not value.startswith("/") and all(p not in {"", ".", ".."} for p in parts), "状态路径越界或未规范化")
    _require(all(not any(ord(c) < 32 for c in p) and len(p.encode("utf-8")) <= 255 for p in parts), "状态路径分量非法")
    _require(parts[0] != LOCK_NAME, "锁文件不属于产品状态清单")
    return PurePosixPath(value).as_posix()


def _plain_directory(path: Path) -> os.stat_result:
    _require(path.is_absolute() and ".." not in path.parts, "私有根必须为无父跳转的绝对路径")
    for current in reversed((path, *path.parents)):
        info = current.lstat()
        _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "私有根祖先不是普通目录或含 symlink")
    info = path.lstat()
    _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0, "私有根须归当前用户且禁止组与其他用户访问")
    return info


def _identity(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"task_sha256", "task_nonce", "producer_run_id", "deadline_utc"}
    _require(isinstance(value, Mapping) and set(value) == fields, "状态身份必须冻结任务、nonce、producer 和原 deadline")
    result = dict(value)
    _require(isinstance(result["task_sha256"], str) and bool(SHA_RE.fullmatch(result["task_sha256"])), "任务摘要非法")
    for key in ("task_nonce", "producer_run_id"):
        _require(isinstance(result[key], str) and bool(ID_RE.fullmatch(result[key])), f"{key} 非法")
    _require(isinstance(result["deadline_utc"], str), "原 deadline 非法")
    try:
        moment = datetime.fromisoformat(result["deadline_utc"].replace("Z", "+00:00"))
    except ValueError as error:
        raise StateJournalError("原 deadline 非法") from error
    _require(moment.tzinfo is not None and moment.utcoffset().total_seconds() == 0, "原 deadline 必须带 UTC 时区")
    return result


def file_state(data: bytes, *, mode: int = 0o600, mtime_ns: int | None = None) -> dict[str, Any]:
    """受管写入的目标状态；它只是意图，不能当作已观测的 after。"""
    _require(isinstance(data, bytes), "文件意图须提供 bytes")
    return {"type": "file", "mode": mode, "mtime_ns": time.time_ns() if mtime_ns is None else mtime_ns,
            "bytes": len(data), "sha256": _sha(data), "data_base64": base64.b64encode(data).decode("ascii")}


def missing_state() -> dict[str, Any]:
    """明确缺失的目标状态，不用空文件代替不存在。"""
    return {"type": "missing"}


class StateJournal:
    """一个私有根、一个冻结身份、一个全周期排他锁。

    frozen_paths 必须不重叠，父目录已经存在且无 symlink。symlink_targets 是冻结
    的叶 symlink 白名单（相对状态路径→相对目标字面）；不跟随链接读取或恢复。
    record_intent 的完整目标来自调用方的受管操作。任意未知产品写入没有来源证明，
    本模块不会自动把它收成“已知 after”。产品停止、drain 和正式封存由上层负责。
    """

    def __init__(self, identity: Mapping[str, Any], journal_root: Path, private_state_root: Path,
                 frozen_paths: Sequence[str], *, symlink_targets: Mapping[str, str] | None = None,
                 limits: SnapshotLimits | None = None) -> None:
        self.identity = _identity(identity)
        self.root, self.state_root = Path(journal_root), Path(private_state_root)
        self.limits = limits or SnapshotLimits()
        self.limits.validate()
        _require(not isinstance(frozen_paths, (str, bytes)) and bool(frozen_paths), "状态清单不能为空")
        self.paths = tuple(sorted(_relative(p) for p in frozen_paths))
        _require(len(self.paths) <= self.limits.max_paths and len(set(self.paths)) == len(self.paths), "状态清单过长或重复")
        for i, path in enumerate(self.paths):
            _require(len(PurePosixPath(path).parts) <= self.limits.max_depth, "状态路径嵌套过深")
            _require(not any(other.startswith(path + "/") for other in self.paths[i+1:]), "状态路径不能重叠")
        self.symlinks = {_relative(k): v for k, v in (symlink_targets or {}).items()}
        for key, target in self.symlinks.items():
            self._check_symlink(key, target)
        self._lock_fd: int | None = None
        self._state_fd: int | None = None
        self._root_info: os.stat_result | None = None
        self._journal_info: os.stat_result | None = None
        self._frozen_config: dict[str, Any] | None = None
        self._rows: list[dict[str, Any]] = []
        self._prepared = False
        self._product_recorder: Any = None

    def _check_symlink(self, logical: str, target: Any) -> None:
        _require(isinstance(target, str) and bool(target) and len(target.encode("utf-8")) <= 1024, "symlink 目标非法")
        _require(not target.startswith("/") and not any(ord(c) < 32 for c in target), "symlink 目标须为受控相对路径")
        parts = list(PurePosixPath(logical).parent.parts)
        for piece in target.split("/"):
            _require(piece not in {"", "."}, "symlink 目标未规范化")
            if piece == "..":
                _require(bool(parts), "symlink 目标越出私有根")
                parts.pop()
            else:
                _require(len(piece.encode("utf-8")) <= 255, "symlink 目标分量过长")
                parts.append(piece)
        _require(bool(parts) and parts[0] != LOCK_NAME, "symlink 不能指向私有根或锁")
        _require(self.symlinks.get(logical) == target, "symlink 未在冻结白名单中")
        # 字面在根内仍可能经另一条 symlink 越界；只接受普通目录组成的目标链。
        if getattr(self, "_state_fd", None) is not None:
            fd = os.dup(self._state_fd)
            try:
                for index, piece in enumerate(parts):
                    try:
                        info = os.stat(piece, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        break
                    _require(not stat.S_ISLNK(info.st_mode) and info.st_dev == self._root_info.st_dev,
                             "symlink 目标经链接或挂载边界越出受控路径")
                    if index != len(parts) - 1:
                        next_fd = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        os.close(fd)
                        fd = next_fd
                    else:
                        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 or stat.S_ISDIR(info.st_mode),
                                 "symlink 目标不是普通单链接文件或目录")
            finally:
                os.close(fd)

    def __enter__(self) -> StateJournal:
        _require(self._lock_fd is None, "同一 journal 实例不能重复持锁")
        self._prepared, self._rows = False, []
        info = _plain_directory(self.state_root)
        _require(not self.root.is_relative_to(self.state_root) and not self.state_root.is_relative_to(self.root), "journal 与产品私有根须分离")
        if not self.root.exists():
            _plain_directory(self.root.parent)
            self.root.mkdir(mode=0o700)
        journal_info = _plain_directory(self.root)
        _require(info.st_dev == journal_info.st_dev, "journal 与私有状态须在同一文件系统以支持原子恢复")
        self._state_fd = os.open(self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._root_info = info
        self._journal_info = journal_info
        try:
            self._lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._state_fd)
            lock_info = os.fstat(self._lock_fd)
            _require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_nlink == 1 and lock_info.st_uid == os.geteuid()
                     and stat.S_IMODE(lock_info.st_mode) == 0o600, "状态锁不是私有普通单链接文件")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StateLockBusy("私有状态锁已被占用") from error
            os.fsync(self._state_fd)
            raw = os.read(self._lock_fd, 8193)
            _require(len(raw) <= 8192, "状态锁标记过大")
            if raw:
                marker = json.loads(raw)
                if marker.get("identity") != self.identity or marker.get("journal_root") != str(self.root):
                    raise StateConflict("私有状态已绑定其他任务／nonce／journal，不能覆盖")
            else:
                marker = _canonical({"identity": self.identity, "journal_root": str(self.root)})
                self._write_all(self._lock_fd, marker)
                os.fsync(self._lock_fd)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args: Any) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._state_fd is not None:
            os.close(self._state_fd)
            self._state_fd = None

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        while raw:
            count = os.write(fd, raw)
            if count <= 0:
                raise OSError("文件写入没有前进")
            raw = raw[count:]

    def _active(self) -> None:
        _require(self._lock_fd is not None and self._state_fd is not None, "状态操作必须持有全周期排他锁")
        current = _plain_directory(self.state_root)
        _require((current.st_dev, current.st_ino) == (self._root_info.st_dev, self._root_info.st_ino), "私有根已被替换")
        journal = _plain_directory(self.root)
        _require((journal.st_dev, journal.st_ino) == (self._journal_info.st_dev, self._journal_info.st_ino), "journal 根已被替换")
        lock_path = os.stat(LOCK_NAME, dir_fd=self._state_fd, follow_symlinks=False)
        held = os.fstat(self._lock_fd)
        _require((held.st_dev, held.st_ino) == (lock_path.st_dev, lock_path.st_ino)
                 and all(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                         and stat.S_IMODE(info.st_mode) == 0o600 for info in (held, lock_path)),
                 "持锁期间状态锁被替换、改权或不再是私有普通单链接文件")

    @contextmanager
    def _parent(self, relative: str) -> Iterator[tuple[int, str]]:
        self._active()
        fd = os.dup(self._state_fd)
        try:
            parts = _relative(relative).split("/")
            for piece in parts[:-1]:
                next_fd = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    _require(os.fstat(next_fd).st_dev == self._root_info.st_dev, "状态路径穿过挂载边界")
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def _read_node(self, parent_fd: int, name: str, logical: str, depth: int, budget: dict[str, int]) -> dict[str, Any]:
        _require(depth <= self.limits.max_depth, "状态目录嵌套过深")
        budget["entries"] += 1
        _require(budget["entries"] <= self.limits.max_entries, "状态条目数超限")
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return missing_state()
        _require(info.st_uid == os.geteuid() and info.st_dev == self._root_info.st_dev, "状态对象所有者或文件系统不符")
        mode = stat.S_IMODE(info.st_mode)
        _require(mode <= 0o777, "状态对象包含特殊权限位")
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=parent_fd)
            self._check_symlink(logical, target)
            result = {"type": "symlink", "target": target, "mode": mode, "mtime_ns": info.st_mtime_ns}
        elif stat.S_ISREG(info.st_mode):
            _require(info.st_nlink == 1 and info.st_size <= self.limits.max_file_bytes, "状态文件是硬链接或超过单文件上限")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                _require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino) and stat.S_ISREG(opened.st_mode), "状态文件读取期间被替换")
                chunks, total = [], 0
                while True:
                    chunk = os.read(fd, min(65536, self.limits.max_file_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    _require(total <= self.limits.max_file_bytes, "状态文件读取期间超限")
                    chunks.append(chunk)
                ended = os.fstat(fd)
                _require((opened.st_size, opened.st_mtime_ns, opened.st_mode) == (ended.st_size, ended.st_mtime_ns, ended.st_mode), "状态文件读取期间发生变化")
                raw = b"".join(chunks)
            finally:
                os.close(fd)
            budget["bytes"] += len(raw)
            _require(budget["bytes"] <= self.limits.max_total_bytes, "状态原件总字节数超限")
            result = file_state(raw, mode=mode, mtime_ns=info.st_mtime_ns)
        elif stat.S_ISDIR(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                _require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino), "状态目录读取期间被替换")
                children = {}
                for child in sorted(os.listdir(fd)):
                    _relative(child)
                    children[child] = self._read_node(fd, child, logical + "/" + child, depth + 1, budget)
                ended = os.fstat(fd)
                _require(opened.st_mtime_ns == ended.st_mtime_ns, "状态目录读取期间发生变化")
            finally:
                os.close(fd)
            result = {"type": "directory", "mode": mode, "mtime_ns": info.st_mtime_ns, "children": children}
        else:
            raise StateJournalError("私有状态包含 FIFO、socket 或设备等特殊对象")
        again = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require((info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns) ==
                 (again.st_dev, again.st_ino, again.st_mode, again.st_mtime_ns), "状态对象在观测后被改变")
        return result

    def _snapshot(self, paths: Sequence[str] | None = None) -> dict[str, Any]:
        self._active()
        result, budget = {}, {"entries": 0, "bytes": 0}
        for path in paths or self.paths:
            with self._parent(path) as (fd, leaf):
                result[path] = self._read_node(fd, leaf, path, len(PurePosixPath(path).parts), budget)
        return result

    def _observe(self, paths: Sequence[str] | None = None) -> dict[str, Any]:
        """运行期无法取得完整实际状态即未知；不把异常转为空目录或缺失。"""
        try:
            return self._snapshot(paths)
        except (StateJournalError, OSError) as error:
            self._append("unknown", {"reason": "实际状态不符合冻结约束或无法完整读取", "error_type": type(error).__name__})
            raise StateConflict("实际私有状态未知，保留现场") from error

    def _binding(self, path: Path) -> dict[str, Any]:
        raw = self._read_private(path)
        return {"path": path.relative_to(self.root).as_posix(), "sha256": _sha(raw), "bytes": len(raw)}

    def _read_private(self, path: Path) -> bytes:
        for parent in path.parents:
            if parent == self.root:
                break
            _plain_directory(parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                     and stat.S_IMODE(info.st_mode) == 0o600, "journal 原件不是私有普通单链接文件")
            _require(info.st_size <= self.limits.max_wal_bytes, "journal 原件超限")
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
                _require(len(raw) <= self.limits.max_wal_bytes, "journal 原件读取超限")
            ended = os.fstat(fd)
            _require((info.st_size, info.st_mtime_ns) == (ended.st_size, ended.st_mtime_ns), "journal 原件读取期间变化")
            return raw
        finally:
            os.close(fd)

    def _exclusive_write(self, path: Path, raw: bytes) -> None:
        _require(len(raw) <= self.limits.max_wal_bytes, "journal 材料超限")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self._write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _material(self, value: Any) -> dict[str, Any]:
        raw = _canonical(value)
        directory = self.root / "materials"
        if not directory.exists():
            directory.mkdir(mode=0o700)
        _plain_directory(directory)
        path = directory / (_sha(raw) + ".json")
        if path.exists() or path.is_symlink():
            _require(self._read_private(path) == raw, "不可变材料已被修改")
        else:
            self._exclusive_write(path, raw)
        return self._binding(path)

    def _load(self, ref: Mapping[str, Any]) -> Any:
        _require(isinstance(ref, Mapping) and set(ref) == {"path", "sha256", "bytes"}, "内部原件绑定非法")
        path = self.root / _relative(ref["path"])
        raw = self._read_private(path)
        _require(_sha(raw) == ref["sha256"] and len(raw) == ref["bytes"], "内部原件摘要不符")
        return json.loads(raw)

    def _append(self, event: str, payload: Any) -> None:
        body = {"sequence": len(self._rows) + 1, "previous_sha256": self._rows[-1]["sha256"] if self._rows else "0" * 64,
                "identity": self.identity, "event": event, "payload": payload,
                "observed_at_utc": datetime.now(timezone.utc).isoformat()}
        row = {**body, "sha256": _sha(_canonical(body))}
        raw = _canonical(row) + b"\n"
        path = self.root / "journal.jsonl"
        current_size = path.lstat().st_size if path.exists() else 0
        _require(current_size + len(raw) <= self.limits.max_wal_bytes, "WAL 字节上限不足，禁止继续修改状态")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                     and stat.S_IMODE(info.st_mode) == 0o600, "WAL 文件不安全")
            self._write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self._rows.append(row)

    def _replay(self) -> None:
        path = self.root / "journal.jsonl"
        self._rows = []
        if not path.exists() and not path.is_symlink():
            return
        raw = self._read_private(path)
        _require(not raw or raw.endswith(b"\n"), "WAL 尾部截断；保留原件，不截短或补造事件")
        for line in raw.splitlines():
            row = json.loads(line)
            expected = {"sequence", "previous_sha256", "identity", "event", "payload", "observed_at_utc", "sha256"}
            _require(isinstance(row, dict) and set(row) == expected, "WAL 行字段非法")
            _require(row["identity"] == self.identity and row["sequence"] == len(self._rows) + 1, "WAL 身份或序号非法")
            _require(row["previous_sha256"] == (self._rows[-1]["sha256"] if self._rows else "0" * 64), "WAL 前件断链")
            _require(row["sha256"] == _sha(_canonical({k: v for k, v in row.items() if k != "sha256"})), "WAL 内容摘要不符")
            self._rows.append(row)

    def prepare(self) -> dict[str, Any]:
        """真实读 before；同一冻结身份幂等，首次原件绝不重写。"""
        self._active()
        config = {"schema_version": SCHEMA, "identity": self.identity, "state_root": str(self.state_root),
                  "root_instance": {"dev": self._root_info.st_dev, "ino": self._root_info.st_ino},
                  "paths": list(self.paths), "symlink_targets": self.symlinks, "limits": asdict(self.limits)}
        manifest = self.root / "frozen-state.json"
        if manifest.exists() or manifest.is_symlink():
            _require(json.loads(self._read_private(manifest)) == config, "状态清单、身份或根实例与首次冻结不同")
        else:
            self._exclusive_write(manifest, _canonical(config))
        self._frozen_config = config
        self._replay()
        before = self.root / "state-before.bin"
        if before.exists() or before.is_symlink():
            original = json.loads(self._read_private(before))
            _require(set(original) == {"schema_version", "identity", "states"} and original["schema_version"] == SCHEMA
                     and original["identity"] == self.identity and set(original["states"]) == set(self.paths), "before 冻结身份或清单非法")
            prepared = [r for r in self._rows if r["event"] == "prepared"]
            if prepared:
                _require(len(prepared) == 1 and prepared[0]["payload"]["before"] == self._binding(before), "首次 before 原件被修改")
            else:
                _require(not self._rows and self._snapshot() == original["states"], "before 尚未入 WAL 且状态已变化，不能猜测未执行")
                self._append("prepared", {"before": self._binding(before)})
        else:
            _require(not self._rows, "已有 WAL 缺首次 before，不能重新采集")
            states = self._snapshot()
            _require(states == self._snapshot(), "采集 before 期间状态不稳定")
            self._exclusive_write(before, _canonical({"schema_version": SCHEMA, "identity": self.identity, "states": states}))
            self._append("prepared", {"before": self._binding(before)})
        self._prepared = True
        return self.evidence()

    def _ready(self) -> None:
        self._active()
        _require(self._prepared, "必须先准备并验证首次 before")
        _require(json.loads(self._read_private(self.root / "frozen-state.json")) == self._frozen_config,
                 "活动冻结清单已被修改")
        # 活动期间不能在篡改后的 WAL 或 before 上继续写状态；重放仍须原件完整。
        expected_wal = b"".join(_canonical(row) + b"\n" for row in self._rows)
        _require(self._read_private(self.root / "journal.jsonl") == expected_wal, "活动 WAL 被修改，禁止继续操作")
        prepared = [r for r in self._rows if r["event"] == "prepared"]
        _require(len(prepared) == 1 and prepared[0]["payload"]["before"] == self._binding(self.root / "state-before.bin"),
                 "首次 before 原件已被修改")
        if any(r["event"] == "unknown" for r in self._rows):
            raise StateConflict("原 journal 已记录未知状态，须由上层解决，不能自动覆盖")

    def _validate_node(self, node: Any, logical: str, depth: int, budget: dict[str, int]) -> None:
        _require(isinstance(node, dict) and "type" in node, "受管目标状态非法")
        budget["entries"] += 1
        _require(depth <= self.limits.max_depth and budget["entries"] <= self.limits.max_entries, "目标状态深度或条目超限")
        kind = node["type"]
        if kind == "missing":
            _require(set(node) == {"type"}, "缺失状态不能混入文件字段")
            return
        _require(type(node.get("mode")) is int and 0 <= node["mode"] <= 0o777 and type(node.get("mtime_ns")) is int
                 and node["mtime_ns"] >= 0, "目标状态权限或时间非法")
        if kind == "file":
            _require(set(node) == {"type", "mode", "mtime_ns", "bytes", "sha256", "data_base64"}, "目标文件字段非法")
            try:
                _require(isinstance(node["data_base64"], str) and len(node["data_base64"]) <= ((self.limits.max_file_bytes + 2) // 3) * 4,
                         "目标文件编码超过单文件上限")
                raw = base64.b64decode(node["data_base64"], validate=True)
            except (ValueError, TypeError) as error:
                raise StateJournalError("目标文件字节非法") from error
            _require(type(node["bytes"]) is int and node["bytes"] == len(raw) and node["sha256"] == _sha(raw)
                     and len(raw) <= self.limits.max_file_bytes, "目标文件摘要或大小非法")
            budget["bytes"] += len(raw)
            _require(budget["bytes"] <= self.limits.max_total_bytes, "目标状态总字节超限")
        elif kind == "directory":
            _require(set(node) == {"type", "mode", "mtime_ns", "children"} and isinstance(node["children"], dict), "目标目录字段非法")
            for name, child in node["children"].items():
                _require("/" not in _relative(name), "目标目录子名称非法")
                _require(isinstance(child, dict) and child.get("type") != "missing", "目录清单不能包含缺失或非法子项")
                self._validate_node(child, logical + "/" + name, depth + 1, budget)
        elif kind == "symlink":
            _require(set(node) == {"type", "mode", "mtime_ns", "target"}, "目标 symlink 字段非法")
            self._check_symlink(logical, node["target"])
        else:
            raise StateJournalError("目标状态类型非法")

    def record_intent(self, operation_id: str, expected_after: Mapping[str, Any]) -> str:
        """在受管操作前 fsync 完整意图；不接受任意路径或未解释的实际写入。"""
        self._ready()
        _require(not any(row["event"] == "product_scope_intent" for row in self._rows),
                 "产品 scope 已冻结，不能继续增加受管设置意图")
        _require(isinstance(operation_id, str) and bool(ID_RE.fullmatch(operation_id)), "操作 ID 非法")
        _require(isinstance(expected_after, Mapping) and bool(expected_after) and set(expected_after) <= set(self.paths), "操作路径不在冻结清单")
        desired = json.loads(_canonical(dict(expected_after)))
        budget = {"entries": 0, "bytes": 0}
        for path, node in desired.items():
            self._validate_node(node, path, len(PurePosixPath(path).parts), budget)
        _require(not any(r["event"] == "restored" for r in self._rows), "已恢复任务不能重新执行变更")
        _require(not any(r["event"] == "after_read_intent" for r in self._rows), "after 观测开始后不能执行变更")
        existing = [r for r in self._rows if r["event"] == "operation_intent" and r["payload"]["operation_id"] == operation_id]
        if existing:
            _require(self._load(existing[0]["payload"]["after"]) == desired, "同操作 ID 不能修改冻结意图")
            return operation_id
        complete = {r["payload"]["operation_id"] for r in self._rows if r["event"] in {"operation_observed", "operation_abandoned"}}
        _require(all(r["payload"]["operation_id"] in complete for r in self._rows if r["event"] == "operation_intent"), "前一操作尚未确认，不能叠加意图")
        baseline = json.loads(self._read_private(self.root / "state-before.bin"))["states"]
        for row in self._rows:
            if row["event"] == "operation_observed":
                baseline.update(self._load(row["payload"]["actual"]))
        if self._observe() != baseline:
            self._unknown("记录新意图前已有未解释的外部写入")
        current = self._observe(sorted(desired))
        self._append("operation_intent", {"operation_id": operation_id, "before": self._material(current), "after": self._material(desired)})
        return operation_id

    def bind_product_recorder(self, recorder: Any) -> None:
        """只绑定固定正式产品工厂的原实例，供历史原件只恢复重放使用。"""
        self._active()
        from tools.official_client_capture import claude_mac_product_state as product
        product.scope_context(recorder, self, authorize=False)
        _require(self._product_recorder is None or self._product_recorder is recorder, "原产品恢复实例不能替换")
        self._product_recorder = recorder

    def _known_operations(self) -> dict[str, Any]:
        baseline = json.loads(self._read_private(self.root / "state-before.bin"))["states"]
        for row in self._rows:
            if row["event"] == "operation_observed":
                baseline.update(self._load(row["payload"]["actual"]))
        return baseline

    def authorize_product_scope(self, recorder: Any) -> None:
        """产品派发前 fsync 精确 scope；after 不会被转换为受管操作意图。"""
        self._ready()
        self.bind_product_recorder(recorder)
        _require(not any(row["event"] in {"product_scope_intent", "restored", "after_read_intent"}
                         for row in self._rows), "产品 scope 只允许在恢复和独立 after 之前冻结一次")
        complete = {row["payload"]["operation_id"] for row in self._rows if row["event"] == "operation_observed"}
        _require(all(row["payload"]["operation_id"] in complete for row in self._rows
                     if row["event"] == "operation_intent"), "产品派发前的受管设置仍未确认")
        baseline = self._known_operations()
        if self._observe() != baseline:
            self._unknown("冻结产品 scope 前已有未知外部写入")
        from tools.official_client_capture import claude_mac_product_state as product
        scope = product.scope_context(recorder, self, authorize=True)
        self._append("product_scope_intent", {"scope": self._material(scope), "baseline": self._material(baseline)})
        # 固定 native 原件随后签准确 scope 摘要；不是可注入的动态验证 callback。
        ack = recorder._provider._monitor.authorize_state_scope(_sha(_canonical(scope)))
        product.verify_scope_ack(scope, ack)
        self._append("product_scope_ack", {"scope_sha256": _sha(_canonical(scope)), "native_ack": ack})

    def observe_product_writes(self) -> dict[str, Any]:
        """停止后核签名完整变化链与全集，持久化观察材料；缺来源保留未知。"""
        self._ready()
        intents = [row["payload"] for row in self._rows if row["event"] == "product_scope_intent"]
        _require(len(intents) == 1 and self._product_recorder is not None, "没有原产品事前 scope 与固定来源实例")
        scope, baseline = self._load(intents[0]["scope"]), self._load(intents[0]["baseline"])
        _require(baseline == self._known_operations(), "产品 scope 的初始状态不是准确已知操作结果")
        actual = self._observe()
        from tools.official_client_capture import claude_mac_product_state as product
        try:
            proof = product.verify_observation(self._product_recorder, self, scope, baseline, actual)
            existing = [row["payload"] for row in self._rows if row["event"] == "product_state_observed"]
            if existing:
                _require(len(existing) == 1 and self._load(existing[0]["actual"]) == actual
                         and existing[0]["proof"] == proof, "原产品观察材料被替换或实际状态已变化")
            else:
                self._append("product_state_observed", {"actual": self._material(actual), "proof": proof})
            return actual
        except StateConflict as error:
            self._unknown(str(error))

    def abandon_product_scope(self) -> None:
        """只在固定实例原 WAL 确认无派发且全集仍等于事前已知状态时撤销 scope。"""
        self._ready()
        _require(self._product_recorder is not None, "没有原固定产品实例，不能判断无派发")
        from tools.official_client_capture import claude_mac_product_state as product
        product.scope_context(self._product_recorder, self, authorize=False)
        _require(not any(row["event"] == "launch_intent" for row in self._product_recorder._rows()),
                 "已有启动意图，不能推断 no-launch")
        intents = [row["payload"] for row in self._rows if row["event"] == "product_scope_intent"]
        _require(len(intents) == 1 and not any(row["event"] == "product_state_observed" for row in self._rows),
                 "没有唯一尚未执行的产品 scope")
        baseline = self._load(intents[0]["baseline"])
        _require(baseline == self._known_operations() and self._observe() == baseline,
                 "未派发产品的状态仍有未知变化，禁止自动覆盖")
        payload = {"scope_sha256": _sha(_canonical(self._load(intents[0]["scope"]))), "reason": "durable_no_launch_and_unchanged_state"}
        if not any(row["event"] == "product_scope_abandoned" for row in self._rows):
            self._append("product_scope_abandoned", payload)

    def _product_known_state(self) -> dict[str, Any] | None:
        intents = [row["payload"] for row in self._rows if row["event"] == "product_scope_intent"]
        observed = [row["payload"] for row in self._rows if row["event"] == "product_state_observed"]
        if not intents:
            _require(not observed, "产品观察材料缺事前 scope")
            return None
        abandoned = [row["payload"] for row in self._rows if row["event"] == "product_scope_abandoned"]
        if abandoned:
            _require(len(intents) == len(abandoned) == 1 and not observed and self._product_recorder is not None,
                     "撤销产品 scope 原件不闭合")
            from tools.official_client_capture import claude_mac_product_state as product
            product.scope_context(self._product_recorder, self, authorize=False)
            _require(not any(row["event"] == "launch_intent" for row in self._product_recorder._rows())
                     and abandoned[0] == {"scope_sha256": _sha(_canonical(self._load(intents[0]["scope"]))),
                         "reason": "durable_no_launch_and_unchanged_state"}, "撤销 scope 缺原无派发证明")
            return self._known_operations()
        _require(len(intents) == len(observed) == 1 and self._product_recorder is not None,
                 "真实产品变化缺完整固定来源观察材料，不能自动覆盖")
        scope, baseline = self._load(intents[0]["scope"]), self._load(intents[0]["baseline"])
        actual = self._load(observed[0]["actual"])
        _require(baseline == self._known_operations() and set(actual) == set(self.paths), "产品恢复材料状态全集非法")
        budget = {"entries": 0, "bytes": 0}
        for path, node in actual.items():
            self._validate_node(node, path, len(PurePosixPath(path).parts), budget)
        from tools.official_client_capture import claude_mac_product_state as product
        _require(product.verify_observation(self._product_recorder, self, scope, baseline, actual)
                 == observed[0]["proof"], "历史产品恢复来源重放不符")
        return actual

    def _operation(self, operation_id: str) -> dict[str, Any]:
        matches = [r["payload"] for r in self._rows if r["event"] == "operation_intent" and r["payload"]["operation_id"] == operation_id]
        _require(len(matches) == 1, "缺唯一受管操作意图")
        return matches[0]

    def confirm_intent(self, operation_id: str) -> dict[str, Any]:
        """独立实读结果须匹配预先意图；调用方布尔自报没有确认效力。"""
        self._ready()
        _require(not any(row["event"] == "product_scope_intent" for row in self._rows),
                 "产品 scope 已冻结，不能事后确认受管设置")
        _require(not any(r["event"] == "restored" for r in self._rows), "已恢复任务不能重新确认执行")
        operation = self._operation(operation_id)
        desired = self._load(operation["after"])
        actual = self._observe(sorted(desired))
        if actual != desired:
            self._unknown("变更后的实际状态不符合原意图")
        existing = [r for r in self._rows if r["event"] == "operation_observed" and r["payload"]["operation_id"] == operation_id]
        if not existing:
            self._append("operation_observed", {"operation_id": operation_id, "actual": self._material(actual)})
        else:
            _require(self._load(existing[0]["payload"]["actual"]) == actual, "已确认操作结果已变化")
        return actual

    def _make_node(self, parent_fd: int, name: str, node: Mapping[str, Any], logical: str) -> None:
        kind = node["type"]
        if kind == "missing":
            return
        if kind == "file":
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            try:
                self._write_all(fd, base64.b64decode(node["data_base64"], validate=True))
                os.fchmod(fd, node["mode"])
                os.utime(fd, ns=(node["mtime_ns"], node["mtime_ns"]))
                os.fsync(fd)
            finally:
                os.close(fd)
        elif kind == "directory":
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                for child, value in sorted(node["children"].items()):
                    self._make_node(fd, child, value, logical + "/" + child)
                os.fchmod(fd, node["mode"])
                os.utime(fd, ns=(node["mtime_ns"], node["mtime_ns"]))
                os.fsync(fd)
            finally:
                os.close(fd)
        elif kind == "symlink":
            self._check_symlink(logical, node["target"])
            os.symlink(node["target"], name, dir_fd=parent_fd)
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _require(stat.S_IMODE(info.st_mode) == node["mode"], "当前平台不能无损生成该 symlink 权限，保留原状态")
            os.utime(name, ns=(node["mtime_ns"], node["mtime_ns"]), dir_fd=parent_fd, follow_symlinks=False)
        else:
            raise StateJournalError("不能创建非法状态类型")
        os.fsync(parent_fd)

    def _tx_state(self, fd: int, name: str, logical: str) -> dict[str, Any]:
        return self._read_node(fd, name, logical, len(PurePosixPath(logical).parts), {"entries": 0, "bytes": 0})

    def _execute_replacement(self, payload: Mapping[str, Any]) -> None:
        path = payload["path"]
        original, desired = self._load(payload["before"]), self._load(payload["after"])
        tx = self.root / _relative(payload["transaction"])
        if not tx.exists():
            tx.mkdir(mode=0o700)
        _plain_directory(tx)
        tx_fd = os.open(tx, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with self._parent(path) as (parent_fd, leaf):
                current = self._tx_state(parent_fd, leaf, path)
                stage, trash = self._tx_state(tx_fd, "stage", path), self._tx_state(tx_fd, "trash", path)
                if current == desired and (trash == original or original == missing_state()) and stage == missing_state():
                    pass
                elif current == original and trash == missing_state():
                    if desired != missing_state() and stage == missing_state():
                        self._make_node(tx_fd, "stage", desired, path)
                        stage = self._tx_state(tx_fd, "stage", path)
                    _require(stage == desired, "恢复 stage 截断或与持久化意图不符")
                    if original != missing_state():
                        os.rename(leaf, "trash", src_dir_fd=parent_fd, dst_dir_fd=tx_fd)
                        os.fsync(parent_fd)
                        os.fsync(tx_fd)
                        _require(self._tx_state(tx_fd, "trash", path) == original, "移动前状态被外部改变，保留 trash")
                    if desired != missing_state():
                        os.rename("stage", leaf, src_dir_fd=tx_fd, dst_dir_fd=parent_fd)
                        os.fsync(tx_fd)
                        os.fsync(parent_fd)
                elif current == missing_state() and trash == original and stage == desired:
                    if desired != missing_state():
                        os.rename("stage", leaf, src_dir_fd=tx_fd, dst_dir_fd=parent_fd)
                        os.fsync(tx_fd)
                        os.fsync(parent_fd)
                else:
                    raise StateConflict("原子替换的实际状态未知，禁止覆盖")
                _require(self._tx_state(parent_fd, leaf, path) == desired, "替换结果没有独立实读命中意图")
            self._append("replacement_complete", {"replacement_id": payload["replacement_id"]})
        finally:
            os.close(tx_fd)

    def _replace(self, path: str, current: Mapping[str, Any], desired: Mapping[str, Any], *, phase: str) -> None:
        if current == desired:
            return
        directory = self.root / "transactions"
        if not directory.exists():
            directory.mkdir(mode=0o700)
        _plain_directory(directory)
        replacement_id = uuid.uuid4().hex
        payload = {"replacement_id": replacement_id, "path": path, "before": self._material(current),
                   "after": self._material(desired), "transaction": "transactions/" + replacement_id, "phase": phase}
        self._append("replacement_intent", payload)
        self._execute_replacement(payload)

    def _recover_replacements(self) -> None:
        complete = {r["payload"]["replacement_id"] for r in self._rows if r["event"] == "replacement_complete"}
        for row in list(self._rows):
            if row["event"] == "replacement_intent" and row["payload"]["replacement_id"] not in complete:
                self._execute_replacement(row["payload"])

    def apply_intent(self, operation_id: str) -> dict[str, Any]:
        """执行真实、已冻结的私有文件变更；不操作或重启任何产品。"""
        self._ready()
        _require(not any(row["event"] == "product_scope_intent" for row in self._rows),
                 "产品 scope 已冻结，不能继续执行受管设置")
        _require(not any(r["event"] == "restored" for r in self._rows), "已恢复任务不能重新执行变更")
        _require(not any(r["event"] == "after_read_intent" for r in self._rows), "after 观测开始后不能执行变更")
        self._recover_replacements()
        operation = self._operation(operation_id)
        original, desired = self._load(operation["before"]), self._load(operation["after"])
        current = self._observe(sorted(desired))
        if any(current[p] not in (original[p], desired[p]) for p in desired):
            self._unknown("受管操作执行前出现未知写入")
        try:
            for path in sorted(desired):
                self._replace(path, current[path], desired[path], phase="change")
            return self.confirm_intent(operation_id)
        except Exception as error:
            self._failure(error, "apply_intent")
            raise

    def _unknown(self, message: str) -> None:
        self._append("unknown", {"reason": message})
        raise StateConflict(message)

    def _failure(self, error: Exception, operation: str) -> None:
        raw = _canonical({"schema_version": SCHEMA, "identity": self.identity, "operation": operation,
                          "error_type": type(error).__name__, "source_scope": "internal_state_only"})
        path = self.root / ("failure-" + uuid.uuid4().hex + ".json")
        try:
            if isinstance(error, StateConflict) and not any(r["event"] == "unknown" for r in self._rows):
                self._append("unknown", {"reason": "实际状态或恢复来源未知，保留现场"})
            self._exclusive_write(path, raw)
            self._append("failure", {"failure": self._binding(path)})
        except Exception as recording_error:
            raise error from recording_error

    def restore(self) -> dict[str, Any]:
        """真实恢复首次 before；冲突先全部检查，不能覆盖未知现场。"""
        self._ready()
        try:
            before = json.loads(self._read_private(self.root / "state-before.bin"))["states"]
            known = dict(before)
            pending: dict[str, tuple[Any, Any]] = {}
            observed = {r["payload"]["operation_id"]: r for r in self._rows if r["event"] == "operation_observed"}
            for row in self._rows:
                if row["event"] != "operation_intent":
                    continue
                operation = row["payload"]
                old, new = self._load(operation["before"]), self._load(operation["after"])
                if operation["operation_id"] in observed:
                    actual = self._load(observed[operation["operation_id"]]["payload"]["actual"])
                    _require(actual == new, "操作实际结果原件与意图不符")
                    known.update(actual)
                else:
                    pending.update({p: (old[p], new[p]) for p in new})
            product_known = self._product_known_state()
            if product_known is not None:
                _require(not pending, "产品 scope 不能越过未完成的 v1 受管操作")
                known.update(product_known)
            current = self._observe()
            restored_paths = {r["payload"]["path"] for r in self._rows if r["event"] == "restored_path"}
            completed_replacements = {r["payload"]["replacement_id"] for r in self._rows if r["event"] == "replacement_complete"}
            restoring: dict[str, dict[str, Any]] = {}
            for row in self._rows:
                if row["event"] == "replacement_intent" and row["payload"]["phase"] == "restore":
                    restoring[row["payload"]["path"]] = row["payload"]
                    if row["payload"]["replacement_id"] in completed_replacements:
                        restored_paths.add(row["payload"]["path"])
            unfinished = {r["payload"]["path"]: r["payload"] for r in self._rows if r["event"] == "replacement_intent"
                          and r["payload"]["replacement_id"] not in completed_replacements}
            for path in self.paths:
                allowed = pending.get(path, (known[path],))
                if path in restored_paths:
                    allowed = (before[path],)
                elif path in restoring:
                    allowed = (*allowed, before[path])
                if path in unfinished and current[path] == missing_state():
                    transaction = self.root / _relative(unfinished[path]["transaction"])
                    _plain_directory(transaction)
                    fd = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        _require(self._tx_state(fd, "trash", path) == self._load(unfinished[path]["before"]),
                                 "中断替换缺准确的原状态 trash")
                        _require(self._tx_state(fd, "stage", path) == self._load(unfinished[path]["after"]),
                                 "中断替换缺完整目标 stage")
                        allowed = (*allowed, missing_state())
                    finally:
                        os.close(fd)
                if current[path] not in allowed:
                    self._unknown("恢复前出现未解释的状态／权限变更；保留现场")
            # 全集先检查再恢复已记录原子步骤，不能因一个未知路径而先覆盖其他路径。
            self._recover_replacements()
            current = self._observe()
            for row in self._rows:
                if row["event"] == "replacement_intent" and row["payload"]["phase"] == "restore":
                    restored_paths.add(row["payload"]["path"])
            for path in self.paths:
                if path in restored_paths:
                    continue
                self._replace(path, current[path], before[path], phase="restore")
                self._append("restored_path", {"path": path})
            self.read_after()
            if not any(r["event"] == "restored" for r in self._rows):
                self._append("restored", {"after": self._binding(self.root / "state-after.bin")})
            return self.evidence()
        except Exception as error:
            self._failure(error, "restore")
            raise

    def recover(self) -> dict[str, Any]:
        """同 nonce 恢复已知文件意图；不新建任务、延长 deadline 或重启产品。"""
        self.prepare()
        return self.restore()

    def read_after(self) -> dict[str, Any]:
        """从真实文件系统独立读 after；已有 after 被篡改或状态再变均拒绝。"""
        self._ready()
        path = self.root / "state-after.bin"
        read_intent = [r for r in self._rows if r["event"] == "after_read_intent"]
        if not read_intent:
            _require(not path.exists() and not path.is_symlink(), "after 不是本 journal 的独立读取产物")
            self._append("after_read_intent", {"origin": "independent_filesystem_read"})
        states = self._observe()
        _require(states == self._observe(), "after 读取期间状态不稳定")
        raw = _canonical({"schema_version": SCHEMA, "identity": self.identity, "states": states})
        existing = [r for r in self._rows if r["event"] == "after_observed"]
        if path.exists() or path.is_symlink():
            _require(self._read_private(path) == raw, "after 原件或实际文件系统已变化")
            if existing:
                _require(len(existing) == 1 and existing[0]["payload"]["after"] == self._binding(path), "after 绑定已被修改")
        else:
            _require(not existing, "after 原件丢失，禁止重新签一个 after")
            self._exclusive_write(path, raw)
        if not existing:
            self._append("after_observed", {"after": self._binding(path), "origin": "independent_filesystem_read"})
        return states

    def evidence(self) -> dict[str, Any]:
        """内部状态原件清单；既非 v2 成功收据，也非已认证来源证明。"""
        self._active()
        status = "restored" if any(r["event"] == "restored" for r in self._rows) else "prepared" if self._prepared else "unprepared"
        if any(r["event"] == "unknown" for r in self._rows):
            status = "unknown"
        elif self._rows and self._rows[-1]["event"] == "failure":
            status = "failed"
        bindings = {}
        for name in ("frozen-state.json", "state-before.bin", "state-after.bin", "journal.jsonl"):
            path = self.root / name
            if path.exists() or path.is_symlink():
                bindings[name] = self._binding(path)
        return {"schema_version": SCHEMA, "identity": self.identity, "status": status,
                "source_scope": "internal_state_only", "formal_ready": False, "launch_allowed": False,
                "files": bindings, "failures": [self._binding(p) for p in sorted(self.root.glob("failure-*.json"))]}
