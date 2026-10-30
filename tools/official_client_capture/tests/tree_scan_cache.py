"""测试侧的受管树扫描缓存（OPT-02 第二轮，整树模块提速）。

背景：受管工具身份 ``codex_upgrade._tool_identity`` 每次都把整棵受管树（约 440 个文件）遍历并逐个算 sha256：
``codex_upgrade._tool_tree_entries`` 一遍，``codex_upgrade_tool_identity_policy._managed_tree_digests`` 再一遍（身份记忆化的键就是它）。
``test_codex_upgrade`` 一个模块里 ``_tool_identity`` 被调了 1954 次，约 180 万次文件摘要、两千万次 pathlib 路径运算，占该模块一半以上的时间；
同一棵树、同一个进程里重复扫描，得到的是同一个纯函数的同一个输出，重复执行不提供任何新信息。

做法（不动受管文件，只在测试进程里包一层）：
1. 每次调用先用 ``os.scandir`` 给树做一份**签名**——被扫描范围内每个条目（文件、目录、符号链接，只跳过原函数本来就忽略的 ``tests``／``versions``／
   ``__pycache__`` 目录）的（相对路径、模式、大小、mtime_ns、ctime_ns、inode）；
2. 签名与上次缓存时相同，就返回上次**原函数**的结果（深拷贝，调用方改它不会污染缓存）；不同或没有缓存，就调**原函数**算，结果再入缓存。
   所以结果永远是原函数在同样输入上算出来的，缓存只省掉重复；原函数的第一次执行仍在每个（进程，树）上发生，原函数本身的任何改坏照样被测试看见；
3. **竞态保护**（与 git 索引同一思路）：签名里最新的 mtime／ctime 离"现在"不到 ``RACY_SLACK_SECONDS`` 秒，就不入缓存，也不命中——文件刚被写过，
   同一个时钟刻度里的再次写入不会改变 mtime，所以此时的签名不可信；等文件"老"过窗口之后第一次算出的结果才入缓存。ctime 无法被用户态伪造，
   写完再 ``utime`` 回原 mtime 也会被 ctime 抓住；
4. 原函数抛异常不缓存；签名取不到（权限、竞争删除）不缓存、直接走原函数。

缓存键含（函数种类，树的真实路径），值含完整签名；任何受管文件内容、数量、名字、权限变化都会让签名不同，必然重算。覆盖的两个函数是纯函数
（输出只取决于被扫描范围内的文件集合与内容），其余函数不动。

激活：测试包 ``__init__`` 调 ``install_lazily()``——两个受管模块被导入时才打补丁（不为不用它们的测试模块多付 1 秒以上的导入代价）。
环境变量 ``TREE_SCAN_CACHE=off`` 关闭（排查时用）。
"""

from __future__ import annotations

import copy
import importlib.abc
import importlib.util
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

RACY_SLACK_SECONDS = 3.0
MAX_CACHED_TREES = 48
# 原函数本来就不看的目录名（路径任意一段等于它们的文件都被忽略）：签名也跳过，免得无关目录里的文件变化让缓存白白失效。
PRUNED_DIRECTORIES = frozenset({"tests", "versions", "__pycache__"})
DISABLE_ENV = "TREE_SCAN_CACHE"
CODEX_MODULE = "tools.official_client_capture.codex_upgrade"
POLICY_MODULE = "tools.official_client_capture.codex_upgrade_tool_identity_policy"

# 测试里注入的时钟（单位 ns）。缺省取真实时间。
clock_ns: Callable[[], int] = time.time_ns

_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, str], tuple[tuple, Any]]" = OrderedDict()
stats = {"calls": 0, "hits": 0, "misses": 0, "racy": 0, "unsignable": 0}


def tree_signature(root: str | Path) -> tuple | None:
    """树的签名；取不到或还在竞态窗口内返回 ``None``（调用方不缓存、不命中）。"""

    root = os.fspath(root)
    now = clock_ns()
    horizon = now - int(RACY_SLACK_SECONDS * 1_000_000_000)
    rows: list[tuple] = []
    newest = 0
    try:
        top = os.lstat(root)
        rows.append(("", top.st_mode, top.st_size, top.st_mtime_ns, top.st_ctime_ns, top.st_ino))
        newest = max(top.st_mtime_ns, top.st_ctime_ns)
        stack: list[tuple[str, str]] = [(root, "")]
        while stack:
            directory, prefix = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    relative = f"{prefix}{entry.name}"
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if is_directory and entry.name in PRUNED_DIRECTORIES:
                        continue
                    rows.append((relative, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
                    newest = max(newest, info.st_mtime_ns, info.st_ctime_ns)
                    if is_directory:
                        stack.append((entry.path, relative + "/"))
    except OSError:
        stats["unsignable"] += 1
        return None
    if newest > horizon:
        stats["racy"] += 1
        return None
    rows.sort()
    return tuple(rows)


def cached(original: Callable[[Any], Any], kind: str) -> Callable[[Any], Any]:
    """给"单参数＝树根目录"的纯函数包一层缓存（语义见模块说明）。包装函数带 ``__wrapped__`` 指向原函数。"""

    def wrapper(tool_root: Any) -> Any:
        stats["calls"] += 1
        if os.environ.get(DISABLE_ENV) == "off":
            return original(tool_root)
        try:
            real = os.path.realpath(os.fspath(tool_root))
        except (OSError, TypeError, ValueError):
            return original(tool_root)
        signature = tree_signature(real)
        key = (kind, real)
        if signature is not None:
            with _lock:
                hit = _cache.get(key)
                if hit is not None and hit[0] == signature:
                    _cache.move_to_end(key)
                    stats["hits"] += 1
                    return copy.deepcopy(hit[1])
        stats["misses"] += 1
        result = original(tool_root)
        if signature is not None:
            with _lock:
                _cache[key] = (signature, copy.deepcopy(result))
                _cache.move_to_end(key)
                while len(_cache) > MAX_CACHED_TREES:
                    _cache.popitem(last=False)
        return result

    wrapper.__wrapped__ = original   # type: ignore[attr-defined]
    wrapper.__name__ = getattr(original, "__name__", "wrapped")
    wrapper.__doc__ = getattr(original, "__doc__", None)
    return wrapper


def clear() -> None:
    with _lock:
        _cache.clear()
    for key in stats:
        stats[key] = 0


def _patch(module: Any) -> None:
    """给已导入的受管模块里的两个整树函数打补丁（幂等）。"""

    for attribute, kind in (("_tool_tree_entries", "entries"), ("_managed_tree_digests", "digests")):
        function = getattr(module, attribute, None)
        if function is not None and not hasattr(function, "__wrapped__"):
            setattr(module, attribute, cached(function, kind))


def installed(module: Any) -> bool:
    return any(hasattr(getattr(module, attribute, None), "__wrapped__") for attribute in ("_tool_tree_entries", "_managed_tree_digests"))


class _PostImportPatcher(importlib.abc.MetaPathFinder):
    """目标模块导入完成后立刻打补丁。通过包一层加载器的 ``exec_module`` 实现，不改变任何模块的导入结果。"""

    targets = {CODEX_MODULE: "_tool_tree_entries", POLICY_MODULE: "_managed_tree_digests"}

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        if name not in self.targets:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        loader = spec.loader
        original_exec = loader.exec_module

        def exec_module(module: Any) -> None:
            original_exec(module)
            _patch(module)

        loader.exec_module = exec_module   # type: ignore[method-assign]
        return spec


def install_lazily() -> None:
    """测试包导入时调用：已导入的受管模块立刻补丁，没导入的等它被导入时补丁。"""

    if os.environ.get(DISABLE_ENV) == "off":
        return
    for name in (CODEX_MODULE, POLICY_MODULE):
        module = sys.modules.get(name)
        if module is not None:
            _patch(module)
    if not any(isinstance(item, _PostImportPatcher) for item in sys.meta_path):
        sys.meta_path.insert(0, _PostImportPatcher())
