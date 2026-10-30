#!/usr/bin/env python3
"""依赖追踪器（OPT-02）：逐个测试模块跑一遍，用审计钩子（``sys.addaudithook``）记录它实际打开、枚举了仓库里的哪些文件和目录，
结果由 ``unit_ownership.py dependency-table`` 汇成依赖追踪表 ``tools/ci/unit_dependencies.json``。

用法（在被追踪的测试树根目录之外的任何地方都行，追踪不改测试树）::

    python3 -B tools/ci/dependency_trace.py --repo <测试树> --out <结果目录> [--jobs 4] [--modules-file <每行一个模块名>]
    python3 -B tools/ci/unit_ownership.py dependency-table --tree <测试树> --trace-dir <结果目录> --python <版本> --platform <平台>

原理：每个模块一个 ``python -m unittest tools.official_client_capture.tests.<模块>`` 子进程，``PYTHONPATH`` 前面垫一个只含 ``sitecustomize.py`` 的目录，
Python 子进程（包括测试再拉起的 Python）启动时自动装上钩子；退出时把"打开过的仓库内文件、列过的仓库内目录、拉起的子进程参数"写到
``<结果目录>/<模块>/<pid>.json``，运行结果（退出码、耗时）写 ``_result.json``。导入机制（FileFinder 缓存目录等）列目录不算测试在枚举仓库，按调用栈排除。

限制（所以追踪表只是依赖的一部分，另有静态下限与失败关闭兜底）：看不到非 Python 进程（Shell 脚本里的 cat／jq、Go、Node）自己读的文件，
只看得到它们是被谁拉起的；``stat``／``exists`` 没有审计事件；只覆盖这一次运行走到的分支（本机跳过的平台分支不在里面）；退出码非 0 的模块记录不可信，进 ``unobserved``。

重新追踪的时机：版本登记之后、同一 Campaign 的第一次验证之前（追踪提交必须是之后所有基点的祖先；追踪表在改动里时，依赖精度整轮不筛选）。
本机 16 GiB 要走重测试锁（``--jobs`` 别超过 4）；全部 385 个模块约 30～50 分钟。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS_PACKAGE = "tools.official_client_capture.tests"
MODULE_TIMEOUT_SECONDS = 1500

# 写进 PYTHONPATH 目录的 sitecustomize.py：钩子本身只用标准库，任何异常都吞掉（追踪不能改变被测代码的行为）。
SITECUSTOMIZE = r'''
import os, sys, json, atexit
_OUT = os.environ.get("TRACE_OUT")
_ROOT = os.environ.get("TRACE_ROOT")
if _OUT and _ROOT:
    _ROOT = os.path.realpath(_ROOT) + os.sep
    _opened, _listed, _spawned = set(), set(), []

    def _rel(path):
        try:
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if isinstance(path, int) or path is None:
                return None
            absolute = os.path.realpath(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))
            if absolute.startswith(_ROOT):
                return absolute[len(_ROOT):]
            if absolute + os.sep == _ROOT:
                return ""
        except Exception:
            return None
        return None

    def _import_machinery():
        # 导入机制（FileFinder 缓存目录、pkgutil、zipimport）列目录不是测试在枚举仓库目录：按调用栈排除。
        frame = sys._getframe(2)
        depth = 0
        while frame is not None and depth < 14:
            name = frame.f_code.co_filename
            if "importlib" in name or name.endswith(("pkgutil.py", "zipimport.py", "site.py", "linecache.py")):
                return True
            frame = frame.f_back
            depth += 1
        return False

    def _hook(event, args):
        try:
            if event == "open":
                relative = _rel(args[0])
                if relative is not None:
                    _opened.add(relative)
            elif event in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2", "os.walk", "pathlib.Path.glob", "pathlib.Path.rglob"):
                relative = _rel(args[0])
                if relative is not None and not _import_machinery():
                    _listed.add(relative)
            elif event in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.system"):
                _spawned.append([str(item)[:300] for item in (args[:2] if event == "subprocess.Popen" else args[:3])])
        except Exception:
            pass

    sys.addaudithook(_hook)

    def _dump():
        try:
            os.makedirs(_OUT, exist_ok=True)
            with open(os.path.join(_OUT, f"{os.getpid()}.json"), "w") as handle:
                json.dump({"opened": sorted(_opened), "listed": sorted(_listed), "spawned": _spawned[:400], "argv": sys.argv[:6]}, handle)
        except Exception:
            pass

    atexit.register(_dump)
'''


def trace_module(repo: Path, out: Path, site: Path, module: str) -> tuple[str, int, float]:
    """追踪一个测试模块：返回（模块名, 退出码, 耗时秒）。"""

    folder = out / module
    folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TRACE_OUT=str(folder), TRACE_ROOT=str(repo), PYTHONPATH=str(site) + os.pathsep + str(repo), PYTHONDONTWRITEBYTECODE="1")
    started = time.time()
    try:
        # 子进程用 PATH 里的 python3（与门禁入口 ``python3 -B -m unittest`` 同一个解释器路径），不用 sys.executable：框架版 Python 的 sys.executable 是
        # 真实路径而不是符号链接路径，test_arm64_capture_driver 里有一条用例比对的正是后者，用 sys.executable 追踪会让它假红、整个模块进 unobserved。
        completed = subprocess.run([shutil.which("python3") or sys.executable, "-B", "-m", "unittest", f"{TESTS_PACKAGE}.{module}"], cwd=repo, env=env, capture_output=True,
                                   text=True, timeout=MODULE_TIMEOUT_SECONDS, check=False)
        code, tail = completed.returncode, (completed.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        code, tail = -9, "timeout"
    seconds = round(time.time() - started, 1)
    (folder / "_result.json").write_text(json.dumps({"module": module, "rc": code, "seconds": seconds, "tail": tail}), encoding="utf-8")
    return module, code, seconds


def run(repo: Path, out: Path, modules: list[str], jobs: int) -> list[tuple[str, int, float]]:
    repo, out = Path(repo).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        site = Path(directory)
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            return list(pool.map(lambda name: trace_module(repo, out, site, name), modules))


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True, help="被追踪的测试树（建议是干净的、固定在一个提交上的检出）")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--modules-file", type=Path, default=None, help="每行一个模块名；缺省追踪测试树里非另一客户端重模块的全部模块")
    args = parser.parse_args(argv)
    if args.modules_file is not None:
        modules = [line.strip() for line in args.modules_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import unit_ownership as ownership   # 同目录的选择器：用它的归属表判断哪些是重模块

        data = ownership.load_data(args.repo)
        heavy = {name for name, row in data["modules"].items()
                 if ownership.plan_state(row, "codex", cheap=data["cheap_core_seconds"], pinned=name in data["pinned"]) == ownership.SKIP}
        modules = [name for name in ownership.discover_modules(args.repo) if name not in heavy]
    results = run(args.repo, args.out, modules, args.jobs)
    for module, code, seconds in results:
        print(module, code, seconds, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
