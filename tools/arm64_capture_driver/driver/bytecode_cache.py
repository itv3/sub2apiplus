#!/usr/bin/env python3
"""为禁写字节码的测试运行准备树外只读字节码缓存（修好接着跑第 67 项）。

用法：python3 bytecode_cache.py <缓存目录（绝对路径，名字含 pycache）> <源码目录>...

背景：驱动全局 PYTHONDONTWRITEBYTECODE=1（测试树、数据根、staging 树都不许留 __pycache__）。
* 若再设一个空的 PYTHONPYCACHEPREFIX，解释器改到前缀下查找全部 .pyc（含标准库自带的）而全部落空，
  每个 Python 子进程都从源码重编标准库与受管模块：ARM64 实测监督器 CLI 启动 584 毫秒，候选树监督器
  4 条计时用例在目标平台门禁里确定性失败；
* 不设前缀时标准库能读自带的 .pyc，但受管模块仍每次从源码编译：启动约 323 毫秒，心跳间隔用例
  （0.15 秒超时、0.35 秒后断言）只剩约 20 毫秒余量，ARM64 实测 16 次失败 2 次。

做法：先清空并重建缓存目录，把解释器标准库和给定源码目录预编译进去（缓存目录按源文件的绝对路径
镜像），调用方随后设 PYTHONPYCACHEPREFIX 指向它、保持禁写，测试期间只读使用——ARM64 实测启动约
187 毫秒，心跳用例 15 次零失败。源码目录下不产生任何 __pycache__。

* 缓存目录必须是绝对路径、名字含 pycache，且与源码目录、标准库互不包含（清空缓存目录前的防呆）；
* 源码目录按物理路径编译：测试进程按 os.getcwd()（物理路径）拼模块路径，缓存路径必须与之一致；
* 标准库按解释器实际导入时的路径写法编译（sysconfig 报告的写法不同时两份都编译），跳过其中的 site-packages／
  dist-packages 与自带测试集；个别文件编译失败只会让该文件回落为源码编译，不影响语义，只记入输出；
* 源码目录必须全部编译成功、且不得出现 __pycache__，否则退出 1（失败关闭，调用方不得带着残缺缓存继续）；
* 最后一行输出 JSON 摘要（status／prefix／标准库与各源码目录的 .pyc 数）。
"""

from __future__ import annotations

import compileall
import json
import os
import re
import shutil
import sys
import sysconfig
from pathlib import Path

# 标准库里不属于解释器启动与受管工具依赖的部分：第三方包目录与 Python 自带测试集。
STDLIB_SKIP = re.compile(r"[/\\](site-packages|dist-packages|test|tests|idlelib|turtledemo)[/\\]")


def _mirror(prefix: Path, source: Path) -> Path:
    """与 importlib.util.cache_from_source 同一规则：前缀 + 源文件目录的绝对路径（去掉根）。"""

    return prefix / source.relative_to(source.anchor)


def _count_pyc(prefix: Path, source: Path) -> int:
    mirror = _mirror(prefix, source)
    return sum(1 for _ in mirror.rglob("*.pyc")) if mirror.is_dir() else 0


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def prepare(prefix: Path, sources: list[Path]) -> dict[str, object]:
    if not prefix.is_absolute() or "pycache" not in prefix.name:
        raise ValueError(f"缓存目录必须是绝对路径且名字含 pycache：{prefix}")
    if prefix.is_symlink() or (prefix.exists() and not prefix.is_dir()):
        raise ValueError(f"缓存目录不是普通目录：{prefix}")
    physical_sources = []
    for source in sources:
        if not source.is_absolute() or not source.is_dir():
            raise ValueError(f"源码目录必须是已存在的绝对路径：{source}")
        physical_sources.append(source.resolve(strict=True))
    # 标准库按解释器实际导入时用的路径写法编译（json 包所在目录的上一级，与 sys.path 条目一致）；sysconfig 报告的
    # 写法不同时（例如 Homebrew 的 opt 符号链接与 Cellar 物理路径）两份都编译，保证按哪种写法查找都能命中。
    stdlib = Path(json.__file__).parents[1]
    stdlib_dirs = [stdlib]
    reported = Path(sysconfig.get_paths()["stdlib"])
    if reported != stdlib:
        stdlib_dirs.append(reported)
    resolved_prefix = prefix.resolve()
    for guarded in [*physical_sources, *stdlib_dirs, stdlib.resolve()]:
        if _overlaps(resolved_prefix, guarded) or _overlaps(prefix, guarded):
            raise ValueError(f"缓存目录与源码目录／标准库互相包含：{prefix} ↔ {guarded}")
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(mode=0o700, parents=True)
    # compileall 按 importlib.util.cache_from_source 定位 .pyc，它读 sys.pycache_prefix；并行 worker 由新起的
    # forkserver／spawn 解释器执行，只能从环境变量拿到前缀，两处同时设置。禁写字节码不影响 compileall 的显式写入。
    sys.pycache_prefix = str(prefix)
    os.environ["PYTHONPYCACHEPREFIX"] = str(prefix)
    stdlib_ok = all([compileall.compile_dir(str(item), quiet=1, workers=4, rx=STDLIB_SKIP) for item in stdlib_dirs])
    source_counts: dict[str, int] = {}
    failed: list[str] = []
    for source in physical_sources:
        if not compileall.compile_dir(str(source), quiet=1, workers=4):
            failed.append(str(source))
        source_counts[str(source)] = _count_pyc(prefix, source)
        if source_counts[str(source)] == 0:
            failed.append(str(source))
    leaked = sorted(str(path) for source in physical_sources for path in source.rglob("__pycache__"))
    return {
        "status": "ready" if not failed and not leaked else "failed",
        "prefix": str(prefix),
        "stdlib": str(stdlib),
        "stdlib_pyc": _count_pyc(prefix, stdlib),
        "stdlib_all_compiled": bool(stdlib_ok),
        "sources_pyc": source_counts,
        "failed_sources": sorted(set(failed)),
        "leaked_pycache": leaked[:5],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2:
        print("用法：python3 bytecode_cache.py <缓存目录（绝对路径，名字含 pycache）> <源码目录>...", file=sys.stderr)
        return 2
    try:
        summary = prepare(Path(arguments[0]), [Path(item) for item in arguments[1:]])
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    # 不排序键：status 在最前，调用方截断日志行时结论不丢。
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
