"""VC-6 真实批次链测试专用：复用「受管工具清单快照」的计算结果（只在测试里，不进受管树）。

背景（2026-10-25 第 5 步分段计时，T1 补测）
==========================================
``claude_tool_identity_policy._managed_tree_snapshot`` 每次调用都要用 ``rglob`` 遍历两个受管根、逐个读出约 492 个
源文件并算 sha256。``test_claude_upgrade_vc6_batch`` 的整条恢复链里它被调用了 1108 次（测试进程 458、守卫 213、
五个动作子进程合计 437），调用参数和读到的 492 个文件**全程不变**，合计约 33 个 CPU 秒（本机；CI 约放大 3 倍）。

做法与 ``claude_gate_reader_memo`` 同构
--------------------------------------
* 只在 ``Vc6BatchChainTests`` 的 ``setUp`` 里显式开启；动作子进程经环境变量 ``MEMO_ENV`` 由
  ``claude_typed_vc6_fixture.install`` 开启，守卫进程由测试进程 ``fork`` 而来、沿用已装好的接缝。
  其他所有测试（含专测身份策略的 ``test_claude_tool_identity_policy``）完全不受影响，仍每次真实计算。
* **每个进程对每种输入的第一次调用仍然真实计算**（缓存键里带进程号：守卫经 ``fork`` 继承父进程内存，
  不带进程号会把父进程算过的结果当成自己的）；同一进程之后遇到相同输入，才在核对文件状态后复用。
  因此“测试进程、守卫、动作子进程各自在自己的环境里真实读一遍受管树”这条覆盖保留，总共只真实计算十来次。
* 复用前核对快照涉及的全部状态：受管根与其下每个**未被排除的目录**（目录里新增、删除、改名条目都会改变目录的
  mtime／ctime）、以及快照里每个文件的 ``(路径, 类型, 大小, mtime_ns, ctime_ns, inode, 设备号)``。任一项不同，
  或者核对过程中出现任何异常，一律丢弃缓存、真实计算（并以新结果替换缓存）。ctime 不能被用户态程序伪造，
  所以“改内容后把 mtime 复原、大小不变”也能识别。
* 文件状态太新（mtime／ctime 距建立指纹不足 ``RACY_NS``）时不缓存，也不复用：同一个时钟刻度内的二次修改
  可能不改变 mtime，属于经典的“racy”情形，直接走真实计算最稳妥。
* 受管树里出现符号链接（目录或文件）时不缓存：真实实现对符号链接的处理在不同 Python 版本间有差异，
  不在缓存能证明的范围内。
* 建立指纹前后各枚举一次，两次一致才缓存，避免“读到一半文件被改”留下半新半旧的快照。
* 复用时返回浅拷贝（字典是新的，键值都是不可变的 str／bytes），调用方改返回值不会污染缓存。

为什么不落盘共享
----------------
读取器结果（``claude_gate_reader_memo``）要落盘，是因为它由独立进程算出、同一进程只在首次之后才读缓存；本快照是
纯进程内计算，既然“每个进程首次真实计算”，落盘共享的命中永远只可能来自本进程自己的上一次结果，所以直接放内存即可，
也就没有“缓存文件损坏”这一类失效模式；仍保留“核对异常即回退真实计算”的兜底。统计文件
（目录由 ``MEMO_ENV`` 给出）只记命中与真实计算次数，供事后核对，不参与任何断言。
"""

from __future__ import annotations

import contextlib
import os
import stat as stat_module
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as identity

# 统计目录经环境变量传给 VC-6 动作子进程（子进程里由 claude_typed_vc6_fixture.install 开启）。
MEMO_ENV = "CLAUDE_VC6_TOOL_SNAPSHOT_MEMO_DIR"
# 统计文件：每次复用追加 "hit"，每次真实计算追加 "run"（含进程号），只供事后核对，不参与任何断言。
STATS_FILE = "tool-snapshot-stats.log"
# 距今不足这么久的文件状态不可信（racy），不缓存也不复用。
RACY_NS = 3 * 10**9

_lock = threading.Lock()
# 键：(进程号, 根目录, 受管根配置, 后缀集合) → (状态指纹, 摘要字典, 源字节字典)。
_cache: dict[tuple[Any, ...], tuple[tuple[Any, ...], dict[str, str], dict[str, bytes]]] = {}


def _reset_after_fork() -> None:
    """守卫进程由测试进程 fork 而来：子进程里换一把新锁，避免继承到 fork 瞬间被别的线程持有的锁而死等。"""

    global _lock
    _lock = threading.Lock()


os.register_at_fork(after_in_child=_reset_after_fork)


def _entry_state(path: str) -> tuple[Any, ...]:
    """单个路径的状态；用 ``lstat``，不跟随符号链接。"""

    info = os.lstat(path)
    return (path, stat_module.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)


def _tree_paths(repository_root: Path, policy: Mapping[str, Any]) -> list[str] | None:
    """受管根与其下未被排除的全部目录、文件路径（排序）；出现符号链接返回 None（不缓存）。

    排除规则与真实实现一致：路径任一级的目录名落在 ``EXCLUDED_PARTS`` 内的整棵子树都不参与快照，
    因此既不遍历，也不记录；它们里面的变化不会影响快照结果。
    """

    paths: list[str] = []
    for relative_root, _name in identity._root_names(policy):
        root = os.path.join(str(repository_root), relative_root)
        stack = [root]
        paths.append(root)
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        return None
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in identity.EXCLUDED_PARTS:
                            continue
                        paths.append(entry.path)
                        stack.append(entry.path)
                    else:
                        paths.append(entry.path)
    return sorted(paths)


def _fingerprint(repository_root: Path, policy: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...] | None:
    """全树状态指纹；不可缓存（符号链接、状态太新、枚举失败）返回 None。"""

    try:
        paths = _tree_paths(repository_root, policy)
        if paths is None:
            return None
        states = tuple(_entry_state(path) for path in paths)
    except OSError:
        return None
    newest = max(max(item[3], item[4]) for item in states)
    if time.time_ns() - newest < RACY_NS:
        return None
    return states


def _verify(states: tuple[tuple[Any, ...], ...]) -> bool:
    """逐路径重新取状态并比较；全部相同才返回 True。路径集合由建立时的枚举确定，新增条目会改变其父目录的状态。"""

    try:
        return all(_entry_state(item[0]) == item for item in states)
    except OSError:
        return False


def _key(repository_root: Path, policy: Mapping[str, Any]) -> tuple[Any, ...]:
    return (os.getpid(), str(Path(repository_root)), tuple(str(root) for root in policy["roots"]),
            tuple(sorted(str(suffix) for suffix in policy["suffixes"])))


def _note(directory: Path | None, kind: str) -> None:
    """追加一行统计；统计失败绝不影响测试结果。"""

    if directory is None:
        return
    with contextlib.suppress(OSError):
        with (directory / STATS_FILE).open("a", encoding="utf-8") as stream:
            stream.write(f"{kind} {os.getpid()}\n")


def _memoized(real: Any, directory: Path | None) -> Any:
    """包装 ``_managed_tree_snapshot``：未命中走真实实现，命中且状态未变则返回缓存的浅拷贝。"""

    def snapshot(repository_root: Path, policy: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, bytes]]:
        try:
            key = _key(repository_root, policy)
        except (KeyError, TypeError):
            # 策略形态不对：交给真实实现给出权威的拒绝。
            return real(repository_root, policy)
        with _lock:
            cached = _cache.get(key)
        if cached is not None:
            try:
                reusable = _verify(cached[0])
            except Exception:  # noqa: BLE001 —— 任何核对异常都按“不可复用”处理，回退真实计算
                reusable = False
            if reusable:
                _note(directory, "hit")
                return dict(cached[1]), dict(cached[2])
            with _lock:
                _cache.pop(key, None)
        before = _fingerprint(Path(repository_root), policy)
        digests, sources = real(repository_root, policy)
        _note(directory, "run")
        # 计算前后的枚举一致才缓存：读取期间被改过的树不留缓存。
        if before is not None and _fingerprint(Path(repository_root), policy) == before:
            with _lock:
                _cache[key] = (before, dict(digests), dict(sources))
        return digests, sources

    return snapshot


@contextlib.contextmanager
def tool_snapshot_memo(directory: Path | None = None) -> Iterator[None]:
    """在本进程内启用受管工具清单快照复用；退出即恢复原函数并清空本进程缓存。"""

    stats = None if directory is None else Path(directory)
    if stats is not None:
        stats.mkdir(parents=True, exist_ok=True, mode=0o700)
    real = identity._managed_tree_snapshot
    with mock.patch.object(identity, "_managed_tree_snapshot", _memoized(real, stats)):
        try:
            yield
        finally:
            with _lock:
                _cache.clear()


def read_stats(directory: Path) -> dict[str, int]:
    """事后汇总：复用次数与真实计算次数（供人工核对与回归观察）。"""

    counts = {"hit": 0, "run": 0}
    with contextlib.suppress(OSError):
        for line in (Path(directory) / STATS_FILE).read_text(encoding="utf-8").splitlines():
            kind = line.split(" ", 1)[0]
            if kind in counts:
                counts[kind] += 1
    return counts


def environment(directory: Path) -> dict[str, str]:
    """交给动作子进程的环境变量。"""

    return {MEMO_ENV: str(directory)}


__all__ = ["MEMO_ENV", "STATS_FILE", "environment", "read_stats", "tool_snapshot_memo"]
