#!/usr/bin/env python3
"""后端 ``go test`` 按包运行与按包补跑（Codex 升级工具优化第 3 项）。

为什么要有：后端三套 Go 测试（``backend:go-test``、``backend:unit``、``backend:integration``）各跑 9～19 分钟，满载时偶发抖红一次，
整个单元就红了；执行器的补跑（第 2 项）如果再整套重跑，补跑一次又是 9～19 分钟。这里把补跑缩到“失败的包”：

* **首轮**（执行器的正式执行，环境变量 ``UNIT_EXECUTOR_KIND`` 不是 ``retry``）：先 ``go list`` 得到期望包集合，再 ``go test -json -count=1``
  跑全部包，逐个核对：每个期望包都必须有包级终态事件（pass／fail／skip），**没跑到的包（崩溃、提前退出、被杀）按失败处理，不能当通过**；
  ``go test`` 自己的退出码也必须一致。结构化结果写进执行器下发的旁路文件（``UNIT_EXECUTOR_SIDECAR``），日志仍是可读的 go test 文本输出。
* **补跑**（执行器腾空机器后的补跑尝试，``UNIT_EXECUTOR_KIND=retry``）：读首轮旁路文件（``UNIT_EXECUTOR_FIRST_SIDECAR``），只对失败的包与没跑到
  的包重新 ``go test -json -count=1``（沿用入口门禁缓存合同：``-count=1``，补跑不会撞上测试结果缓存）。**只有该包的失败用例能独立运行时才缩到
  ``-run`` 单个用例**：包里没有 ``TestMain``、没有 ``init``、失败的都是顶层用例、且首轮里该包的每个顶层用例都有终态（没有被崩溃截断）、失败用例不多；
  拿不准一律整包重跑。缩到用例时，被点名的用例必须真的跑到并通过（``-run`` 没匹配到任何用例会让 go test 悄悄通过，这里按失败处理）。
  **缩到用例补跑通过后，对该包再整包补跑一次，整包通过才算这个包补跑通过**（标注写“缩到用例＋整包确认”）。
  最终通过＝首轮通过的包＋补跑通过的包恰好覆盖期望包集合、没有任何失败。
* **退出码**（验收修正）：测试失败退出 1（可补跑）；PATH 里找不到 go 退出 127、包装器自己出错（go list 失败）退出 125、go 子进程被信号杀退出 128＋信号
  （137、143 等）——这几种是环境失败，执行器不补跑。
* **失败关闭**：首轮旁路文件缺失、不合法、与本次命令的标签不符，补跑退回整套重跑（仍只此一次）；任何环节拿不准都不判通过。

用法（由门禁清单生成，工作目录是 ``backend``）：``python3 go_test_packages.py [--tags unit|integration] [--pattern ./...] [-- <附加 go test 参数>]``。
环境变量 ``GO_TEST_PACKAGES_NARROW=off`` 关闭“缩到用例”（补跑一律整包）。本模块只依赖标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

SCHEMA = "go-test-packages/v1"
# 缩到 -run 的失败用例数上限：失败很多说明不是单点偶发，整包重跑。
MAX_NARROW_TESTS = 20
_TOP_LEVEL_TEST = re.compile(r"Test[A-Za-z0-9_]*\Z")
_TERMINAL = ("pass", "fail", "skip")
# 首轮包级输出里出现这些标记，说明测试进程可能崩溃、超时或被竞态检测打断，状态不可信：整包重跑，不缩到用例。
_UNSAFE_OUTPUT = ("panic:", "fatal error:", "DATA RACE", "test timed out", "signal: killed", "signal: ")


# 包装器自己的退出码约定（与执行器的失败分类一致，见 ``retry_policy.ENVIRONMENT_EXIT_CODES``，验收修正建议 9）：这几种都说明“没有真正跑到测试”，
# 属环境失败，执行器不补跑——127＝PATH 里找不到 go；125＝包装器自己出错（go list 失败等）；128＋信号＝go 子进程被信号杀（137＝SIGKILL、143＝SIGTERM）。
# 测试本身失败仍退出 1（可补跑）。
EXIT_TEST_FAILED = 1
EXIT_WRAPPER_ERROR = 125
EXIT_GO_NOT_FOUND = 127


_COUNT_FLAG = re.compile(r"--?count(?:=(.*))?")


def count_conflicts(words: Sequence[str]) -> list[str]:
    """附加参数里取值不是 1 的 ``-count``（Go 以最后一次出现为准，附加参数排在包装器固定的 ``-count=1`` 之后，会把缓存合同悄悄改掉）。"""

    values: list[str] = []
    for index, word in enumerate(words):
        match = _COUNT_FLAG.fullmatch(word)
        if match is not None:
            values.append(match.group(1) if match.group(1) is not None else (words[index + 1] if index + 1 < len(words) else ""))
    return [value for value in values if value != "1"]


_TIMEOUT_FLAG = re.compile(r"--?(?:test\.)?timeout(?:=.*)?")
DEFAULT_GO_TEST_TIMEOUT = "30m"


def with_default_timeout(extra: Sequence[str], configured: str | None) -> list[str]:
    """附加参数里没有 ``-timeout`` 时补一个默认的 30 分钟。

    ``go test`` 单个包缺省 10 分钟；ARM64 全集并发下 ``internal/service`` 包要 8～10 分钟（10-26 入口门禁 601.7 秒、同一包在
    后台验证里 494.5 秒），贴着线被杀，而超时被杀按规矩不补跑，整轮就红。``configured`` 取自环境变量
    ``GO_TEST_PACKAGES_TIMEOUT``：空＝默认 30m，``off``＝不补，其它值原样当 ``-timeout`` 取值。
    """

    words = list(extra)
    if configured == "off" or any(_TIMEOUT_FLAG.fullmatch(word) for word in words):
        return words
    return [*words, f"-timeout={configured or DEFAULT_GO_TEST_TIMEOUT}"]


def signal_exit_code(returncode: int) -> int:
    """``Popen.wait()`` 返回负数＝被该信号杀；按 shell 约定折成 128＋信号。"""

    return 128 - returncode


class GoRunError(RuntimeError):
    exit_code = EXIT_WRAPPER_ERROR


class GoNotFoundError(GoRunError):
    exit_code = EXIT_GO_NOT_FOUND


# ---------------------------------------------------------------------------
# go 命令
# ---------------------------------------------------------------------------


def go_executable() -> str:
    found = shutil.which("go")
    if found is None:
        raise GoNotFoundError("PATH 里找不到 go")
    return found


def tag_args(tags: str) -> list[str]:
    return [f"-tags={tags}"] if tags else []


def run_capture(argv: Sequence[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)


def list_packages(tags: str, pattern: str) -> list[str]:
    """期望包集合：``go list`` 列出的全部包（含没有测试文件的包，它们在 go test 里报 skip）。"""

    completed = run_capture([go_executable(), "list", *tag_args(tags), "-f", "{{.ImportPath}}", pattern])
    if completed.returncode != 0:
        raise GoRunError(f"go list 失败（退出码 {completed.returncode}）：{completed.stderr.strip()[-600:]}")
    packages = sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})
    if not packages:
        raise GoRunError("go list 没有列出任何包")
    return packages


def stream_go_test(args: Sequence[str], *, echo: Callable[[str], None]) -> tuple[int, list[str]]:
    """运行 ``go test -json …``，边读边回显可读文本；返回（退出码, 解析用的原始行）。

    ``-json`` 等同于 ``-v``，每个用例都有 RUN／PASS 输出；回显只保留非详细模式下 go test 本来会打印的内容：包级输出（``ok  pkg``、``FAIL pkg``、崩溃栈）、
    编译输出、失败用例的输出；通过或跳过的用例输出丢弃，没有终态的用例（崩溃截断）输出在结束时补打。解析用的原始行不存普通用例输出，只留含崩溃标记的。"""

    process = subprocess.Popen([go_executable(), "test", "-json", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               stdin=subprocess.DEVNULL, bufsize=1)
    lines: list[str] = []
    pending: dict[tuple[str, str], list[str]] = {}
    assert process.stdout is not None
    for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            lines.append(line)
            echo(line)
            continue
        if not isinstance(event, dict):
            lines.append(line)
            echo(line)
            continue
        action, package, test, text = event.get("Action"), event.get("Package"), event.get("Test"), event.get("Output")
        if action == "output" and isinstance(text, str) and test:
            pending.setdefault((str(package), str(test)), []).append(text)
            if any(marker in text for marker in _UNSAFE_OUTPUT):
                lines.append(line)
            continue
        lines.append(line)
        if action in ("output", "build-output") and isinstance(text, str):
            echo(text)
        elif action in _TERMINAL and test:
            outputs = pending.pop((str(package), str(test)), [])
            if action == "fail":
                echo("".join(outputs))
    for outputs in pending.values():
        echo("".join(outputs))
    return process.wait(), lines


def render_line(line: str) -> str:
    """JSON 事件里的 Output 原样回显（和 go test 文本输出一致）；非 JSON 行（编译器输出等）原样保留。"""

    try:
        event = json.loads(line)
    except ValueError:
        return line
    if isinstance(event, dict) and event.get("Action") in ("output", "build-output") and isinstance(event.get("Output"), str):
        return event["Output"]
    return ""


# ---------------------------------------------------------------------------
# 事件解析（纯函数）
# ---------------------------------------------------------------------------


def parse_events(lines: Iterable[str]) -> dict[str, Any]:
    """把 ``go test -json`` 的事件流归纳成逐包结果。

    返回 ``{"packages": {包: {"action": 终态|None, "tests": {顶层用例: 终态}, "started": [顶层用例], "failed_tests": [...],
    "unsafe_output": bool}}, "build_failed": [包], "other_lines": 非 JSON 行数}``。"""

    packages: dict[str, dict[str, Any]] = {}
    build_failed: set[str] = set()
    other = 0

    def entry(name: str) -> dict[str, Any]:
        return packages.setdefault(name, {"action": None, "tests": {}, "started": [], "failed_tests": [], "unsafe_output": False})

    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            other += 1
            continue
        if not isinstance(event, dict):
            other += 1
            continue
        action, package, test = event.get("Action"), event.get("Package") or event.get("ImportPath"), event.get("Test")
        if not isinstance(package, str) or not package:
            continue
        package = package.split(" [", 1)[0]   # 构建事件的 ImportPath 形如 "pkg [pkg.test]"
        if action == "build-fail":
            build_failed.add(package)
            entry(package)
            continue
        if action == "build-output":
            continue
        item = entry(package)
        if action == "output" and isinstance(event.get("Output"), str):
            if any(marker in event["Output"] for marker in _UNSAFE_OUTPUT):
                item["unsafe_output"] = True
            continue
        if test is None:
            if action in _TERMINAL:
                item["action"] = action
            continue
        top = str(test).split("/", 1)[0]
        if action == "run" and top not in item["started"]:
            item["started"].append(top)
        elif action in _TERMINAL and "/" not in str(test):
            item["tests"][top] = action
            if action == "fail" and top not in item["failed_tests"]:
                item["failed_tests"].append(top)
        elif action == "fail" and top not in item["failed_tests"]:
            item["failed_tests"].append(top)   # 子测试失败：顶层用例也会报 fail，这里兜底
    return {"packages": packages, "build_failed": sorted(build_failed), "other_lines": other}


def judge(expected: Sequence[str], parsed: dict[str, Any], *, go_exit_code: int) -> dict[str, Any]:
    """首轮（或一次补跑调用）的结论：每个期望包必须有包级终态；没有终态的按“没跑到”处理。"""

    packages = parsed["packages"]
    passed = sorted(name for name in expected if packages.get(name, {}).get("action") in ("pass", "skip"))
    failed = sorted(name for name in expected if packages.get(name, {}).get("action") == "fail" or name in parsed["build_failed"])
    missing = sorted(name for name in expected if packages.get(name, {}).get("action") is None and name not in parsed["build_failed"])
    # go test 退出码与归纳结果不一致（退出码非 0 却没发现任何失败或缺失）：拿不准，按整体失败处理，补跑时退回整套重跑。
    inconsistent = go_exit_code != 0 and not failed and not missing
    ok = go_exit_code == 0 and not failed and not missing and not inconsistent
    return {"passed_packages": passed, "failed_packages": failed, "missing_packages": missing, "inconsistent": inconsistent, "ok": ok}


def package_record(parsed: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    return {name: {key: parsed["packages"].get(name, {}).get(key) for key in ("action", "tests", "started", "failed_tests", "unsafe_output")}
            for name in names}


# ---------------------------------------------------------------------------
# 旁路结果
# ---------------------------------------------------------------------------


def write_sidecar(path: str | None, document: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"schema_version": SCHEMA, **document}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def load_first_sidecar(path: str | None, *, tags: str, pattern: str) -> dict[str, Any] | None:
    """首轮旁路文件；缺失、不合法、与本次命令的标签／模式不符都返回 None（补跑退回整套重跑）。"""

    if not path or not Path(path).is_file():
        return None
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(document, dict) or document.get("schema_version") != SCHEMA or document.get("mode") != "first"
            or document.get("tags") != tags or document.get("pattern") != pattern):
        return None
    expected = document.get("expected_packages")
    packages = document.get("packages")
    if (not isinstance(expected, list) or not expected or not all(isinstance(item, str) for item in expected)
            or not isinstance(packages, dict) or not isinstance(document.get("failed_packages"), list)
            or not isinstance(document.get("missing_packages"), list) or document.get("expected_ok") is not True):
        return None
    return document


# ---------------------------------------------------------------------------
# 补跑计划
# ---------------------------------------------------------------------------


def inspect_package(tags: str, package: str) -> dict[str, Any]:
    """包的测试文件清单与顶层用例清单（``go list -json``、``go test -list``）。"""

    listed = run_capture([go_executable(), "list", *tag_args(tags), "-json", package])
    if listed.returncode != 0:
        raise GoRunError(f"go list -json {package} 失败：{listed.stderr.strip()[-400:]}")
    info = json.loads(listed.stdout)
    names = run_capture([go_executable(), "test", *tag_args(tags), "-list", "^Test", package])
    if names.returncode != 0:
        raise GoRunError(f"go test -list {package} 失败：{names.stderr.strip()[-400:]}")
    tests = sorted(line.strip() for line in names.stdout.splitlines() if _TOP_LEVEL_TEST.match(line.strip()))
    files = [Path(info["Dir"]) / name for name in [*(info.get("TestGoFiles") or []), *(info.get("XTestGoFiles") or [])]]
    return {"files": files, "tests": tests}


def source_blocks_narrowing(files: Iterable[Path]) -> str | None:
    """测试文件里有共享状态入口（``TestMain``、``init``）时不缩到用例。读不出来也不缩。"""

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return f"读不出测试文件 {path.name}"
        if re.search(r"^func\s+TestMain\s*\(", text, re.M):
            return "包里有 TestMain（共享测试进程状态）"
        if re.search(r"^func\s+init\s*\(", text, re.M):
            return "测试文件里有 init 函数（包级夹具）"
    return None


def decide_scope(package: str, first_record: dict[str, Any], *, narrow_enabled: bool,
                 inspector: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """一个失败包补跑时缩到哪里：``{"scope": "package"}`` 或 ``{"scope": "tests", "tests": [...]}``，并写明为什么不缩。"""

    if not narrow_enabled:
        return {"scope": "package", "why": "已用 GO_TEST_PACKAGES_NARROW=off 关闭缩到用例"}
    failed = list(first_record.get("failed_tests") or [])
    if first_record.get("action") != "fail":
        return {"scope": "package", "why": "该包首轮没有包级失败终态（没跑到或编译失败）"}
    if not failed:
        return {"scope": "package", "why": "没有识别到失败的顶层用例"}
    if len(failed) > MAX_NARROW_TESTS:
        return {"scope": "package", "why": f"失败用例多于 {MAX_NARROW_TESTS} 个，不像单点偶发"}
    if not all(_TOP_LEVEL_TEST.match(name) for name in failed):
        return {"scope": "package", "why": "失败的不是普通顶层用例"}
    if first_record.get("unsafe_output"):
        return {"scope": "package", "why": "首轮输出里有崩溃、超时或竞态标记，状态不可信"}
    try:
        inspected = inspector(package)
    except (GoRunError, ValueError, KeyError, OSError) as error:
        return {"scope": "package", "why": f"取不到包的测试清单：{error}"}
    blocked = source_blocks_narrowing(inspected["files"])
    if blocked:
        return {"scope": "package", "why": blocked}
    tests = first_record.get("tests") or {}
    unfinished = [name for name in inspected["tests"] if name not in tests]
    if unfinished:
        return {"scope": "package", "why": f"首轮有顶层用例没有终态（可能被崩溃截断）：{unfinished[:3]}"}
    return {"scope": "tests", "tests": sorted(failed), "why": "包内无 TestMain／init，失败用例可独立运行，且首轮每个顶层用例都有终态"}


def run_pattern(tests: Sequence[str]) -> str:
    return "^(" + "|".join(re.escape(name) for name in tests) + ")$"


# ---------------------------------------------------------------------------
# 首轮与补跑
# ---------------------------------------------------------------------------


def run_first(tags: str, pattern: str, extra: Sequence[str], *, sidecar: str | None, echo: Callable[[str], None]) -> int:
    started = time.monotonic()
    try:
        expected = list_packages(tags, pattern)
    except GoRunError as error:
        echo(f"go_test_packages：{error}\n")
        write_sidecar(sidecar, {"mode": "first", "tags": tags, "pattern": pattern, "expected_ok": False, "error": str(error)})
        return error.exit_code
    echo(f"go_test_packages 首轮：期望包 {len(expected)} 个（go list {pattern} tags={tags or '无'}）\n")
    code, lines = stream_go_test([*tag_args(tags), "-count=1", *extra, pattern], echo=echo)
    if code < 0:
        # go 子进程被信号杀（OOM、外部 kill、超时清理）：不是测试失败，是环境失败；按 128＋信号退出，执行器不补跑。
        echo(f"go_test_packages：go test 被信号 {-code} 杀掉，按环境失败处理（退出码 {signal_exit_code(code)}）\n")
        write_sidecar(sidecar, {"mode": "first", "tags": tags, "pattern": pattern, "expected_ok": False, "error": f"go test 被信号 {-code} 杀掉"})
        return signal_exit_code(code)
    parsed = parse_events(lines)
    verdict = judge(expected, parsed, go_exit_code=code)
    write_sidecar(sidecar, {
        "mode": "first", "tags": tags, "pattern": pattern, "extra": list(extra), "expected_ok": True, "expected_packages": expected,
        "go_exit_code": code, "seconds": round(time.monotonic() - started, 3),
        "packages": package_record(parsed, sorted(set(expected) | set(parsed["packages"]))), "build_failed": parsed["build_failed"],
        "failed_packages": verdict["failed_packages"], "missing_packages": verdict["missing_packages"],
        "inconsistent": verdict["inconsistent"], "ok": verdict["ok"]})
    summary = (f"go_test_packages 首轮结论：通过 {len(verdict['passed_packages'])}／{len(expected)} 个包，失败 {verdict['failed_packages']}，"
               f"没跑到 {verdict['missing_packages']}，go test 退出码 {code}\n")
    echo(summary)
    return 0 if verdict["ok"] else 1


def retry_note(results: Sequence[dict[str, Any]]) -> str:
    """补跑范围的一句话说明，写进旁路文件的 ``retry_note``；执行器把它抄进补跑标注，结论里就看得出这次补跑是怎么通过的。"""

    narrowed = [f"{item['package']} {item.get('tests')}" for item in results if item["scope"] == "tests+package"]
    narrow_failed = [f"{item['package']} {item.get('tests')}" for item in results if item["scope"] == "tests"]
    whole = [item["package"] for item in results if item["scope"] == "package"]
    parts = []
    if narrowed:
        parts.append("缩到用例＋整包确认：" + "；".join(narrowed))
    if narrow_failed:
        parts.append("缩到用例未通过（未做整包确认）：" + "；".join(narrow_failed))
    if whole:
        parts.append("整包补跑：" + "、".join(whole))
    return "；".join(parts) or "没有补跑范围说明"


def _killed_in_retry(sidecar: str | None, tags: str, pattern: str, code: int, echo: Callable[[str], None]) -> int:
    echo(f"go_test_packages：补跑中 go test 被信号 {-code} 杀掉，补跑不算通过（退出码 {signal_exit_code(code)}）\n")
    write_sidecar(sidecar, {"mode": "retry", "tags": tags, "pattern": pattern, "scope": "packages", "ok": False, "error": f"go test 被信号 {-code} 杀掉"})
    return signal_exit_code(code)


def run_retry(tags: str, pattern: str, extra: Sequence[str], *, sidecar: str | None, first_sidecar: str | None, narrow_enabled: bool,
              echo: Callable[[str], None], inspector: Callable[[str], dict[str, Any]] | None = None) -> int:
    started = time.monotonic()
    first = load_first_sidecar(first_sidecar, tags=tags, pattern=pattern)
    if first is None or first.get("inconsistent") or first.get("extra", []) != list(extra):
        echo("go_test_packages 补跑：首轮旁路结果缺失、不合法或与本次命令不符，退回整套重跑（仍只此一次）\n")
        code = run_first(tags, pattern, extra, sidecar=None, echo=echo)
        write_sidecar(sidecar, {"mode": "retry", "tags": tags, "pattern": pattern, "scope": "full", "ok": code == 0})
        return code
    expected = list(first["expected_packages"])
    failed_first, missing_first = list(first["failed_packages"]), list(first["missing_packages"])
    redo = sorted(set(failed_first) | set(missing_first))
    if not redo:
        echo("go_test_packages 补跑：首轮没有失败或缺失的包却判了失败，退回整套重跑\n")
        code = run_first(tags, pattern, extra, sidecar=None, echo=echo)
        write_sidecar(sidecar, {"mode": "retry", "tags": tags, "pattern": pattern, "scope": "full", "ok": code == 0})
        return code
    inspector = inspector or (lambda package: inspect_package(tags, package))
    echo(f"go_test_packages 补跑：首轮失败 {failed_first}，没跑到 {missing_first}；只补跑这 {len(redo)} 个包\n")
    plans = {package: decide_scope(package, first["packages"].get(package, {}), narrow_enabled=narrow_enabled, inspector=inspector)
             for package in redo}
    results: list[dict[str, Any]] = []
    still_ok = True
    whole = [package for package in redo if plans[package]["scope"] == "package"]
    narrow = [package for package in redo if plans[package]["scope"] == "tests"]
    if whole:
        echo(f"-- 整包补跑：{whole}\n")
        code, lines = stream_go_test([*tag_args(tags), "-count=1", *extra, *whole], echo=echo)
        if code < 0:
            return _killed_in_retry(sidecar, tags, pattern, code, echo)
        verdict = judge(whole, parse_events(lines), go_exit_code=code)
        for package in whole:
            results.append({"package": package, "scope": "package", "why": plans[package]["why"], "ok": package in verdict["passed_packages"]})
        still_ok = still_ok and verdict["ok"]
    for package in narrow:
        tests = plans[package]["tests"]
        echo(f"-- 缩到用例补跑：{package} {tests}\n")
        code, lines = stream_go_test([*tag_args(tags), "-count=1", *extra, "-run", run_pattern(tests), package], echo=echo)
        if code < 0:
            return _killed_in_retry(sidecar, tags, pattern, code, echo)
        parsed = parse_events(lines)
        verdict = judge([package], parsed, go_exit_code=code)
        ran = parsed["packages"].get(package, {}).get("tests", {})
        # -run 没匹配到用例时 go test 会悄悄通过：被点名的用例必须真的跑到并通过。
        missing_named = [name for name in tests if ran.get(name) != "pass"]
        ok = verdict["ok"] and not missing_named
        results.append({"package": package, "scope": "tests", "tests": tests, "why": plans[package]["why"], "ok": ok,
                        "named_tests_not_passed": missing_named, "narrow_ok": ok})
        still_ok = still_ok and ok
    # 缩到用例补跑通过的包，再整包补跑一次，整包通过才算这个包补跑通过（验收修正必修 5）：缩到用例只证明“失败的那几个用例单独跑能过”，
    # 同包里其它用例有没有受牵连、包级的 init／共享状态有没有问题，它说明不了；整包确认把这个口子补上。缩到用例没通过的包不用确认（已经红了）。
    confirm = [item["package"] for item in results if item["scope"] == "tests" and item["ok"]]
    if confirm:
        echo(f"-- 整包确认（缩到用例已通过，整包再跑一次）：{confirm}\n")
        code, lines = stream_go_test([*tag_args(tags), "-count=1", *extra, *confirm], echo=echo)
        if code < 0:
            return _killed_in_retry(sidecar, tags, pattern, code, echo)
        verdict = judge(confirm, parse_events(lines), go_exit_code=code)
        for item in results:
            if item["package"] in confirm:
                item["scope"] = "tests+package"
                item["confirm_ok"] = item["package"] in verdict["passed_packages"]
                item["ok"] = item["confirm_ok"]
        still_ok = still_ok and verdict["ok"]
    # 最终通过＝首轮通过（或跳过）的包＋补跑通过的包恰好覆盖期望包集合，且补跑的每一项都通过；“不在失败名单里”不等于通过。
    first_unproblematic = {name for name in expected if name not in redo and first["packages"].get(name, {}).get("action") in ("pass", "skip")}
    retried_ok = {item["package"] for item in results if item["ok"]}
    final_ok = still_ok and (first_unproblematic | retried_ok) == set(expected) and set(redo) <= retried_ok
    write_sidecar(sidecar, {
        "mode": "retry", "tags": tags, "pattern": pattern, "extra": list(extra), "scope": "packages", "expected_packages": expected,
        "first_sidecar_sha256": hashlib.sha256(Path(first_sidecar).read_bytes()).hexdigest() if first_sidecar else None,
        "retried": results, "retry_note": retry_note(results), "seconds": round(time.monotonic() - started, 3), "ok": final_ok})
    echo(f"go_test_packages 补跑结论：{'通过' if final_ok else '仍失败'}；补跑 {len(redo)} 个包，首轮已通过的 {len(first_unproblematic)} 个包沿用\n")
    return 0 if final_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--tags", default="", help="go 构建标签（如 unit、integration）；缺省不带")
    parser.add_argument("--pattern", default="./...", help="包模式，缺省 ./...")
    parser.add_argument("extra", nargs="*", help="附加的 go test 参数（放在 -- 之后）")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    sidecar, first_sidecar = os.environ.get("UNIT_EXECUTOR_SIDECAR"), os.environ.get("UNIT_EXECUTOR_FIRST_SIDECAR")
    kind = os.environ.get("UNIT_EXECUTOR_KIND", "formal")
    narrow_enabled = os.environ.get("GO_TEST_PACKAGES_NARROW", "on") != "off"

    def echo(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    conflicts = count_conflicts(args.extra)
    if conflicts:
        # 缓存合同（-count=1，防测试结果缓存成为隐藏输入）由包装器固定；附加参数不许把它改掉。
        echo(f"go_test_packages：附加参数里的 -count 取值 {conflicts} 与缓存合同要求的 -count=1 矛盾，拒绝运行\n")
        return EXIT_WRAPPER_ERROR
    extra = with_default_timeout(args.extra, os.environ.get("GO_TEST_PACKAGES_TIMEOUT"))
    try:
        if kind == "retry":
            return run_retry(args.tags, args.pattern, extra, sidecar=sidecar, first_sidecar=first_sidecar, narrow_enabled=narrow_enabled, echo=echo)
        return run_first(args.tags, args.pattern, extra, sidecar=sidecar, echo=echo)
    except GoRunError as error:
        echo(f"go_test_packages：{error}\n")
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
