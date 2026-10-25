#!/usr/bin/env python3
"""时间敏感用例的抖动压测（独立脚本，暂不接入任何门禁或 CI）。

做什么
======
先烧核把本机负载推到指定水平（缺省 12 个常驻忙循环进程），再把“时间敏感用例清单”里的每个用例各跑多遍，
报告每个用例失败了几遍。清单有两种来源：

* ``--from-scan``（缺省）：调用同目录的 ``time_threshold_lint`` 扫描 tools 与 backend，把所有命中（含已豁免的：
  豁免只说明“这里的绝对时间是被验证对象”，不代表在满载机器上不会抖）所在的测试函数收成清单；
* ``--cases-file FILE``：一个 JSON 数组，每项 ``{"kind":"go","dir":"backend/internal/service","name":"TestX","tags":"unit"}``
  或 ``{"kind":"python","id":"tools.official_client_capture.tests.test_x.Cls.test_y"}``。

怎么跑
======
* Go 用例按（目录, 构建标签）分组，每遍一个新测试进程 ``go test -count=1 -run '^(A|B|C)$' ./目录``（不用 -count=N：同进程重复会撞包级全局状态而误报），编译结果有缓存；
  带 ``integration`` 标签的需要 Docker，**跳过并在报告里列出**。
* Python 用例逐个 ``python3 -B -m unittest 用例ID``，重复 N 遍。
* 烧核进程用 ``nice`` 不降级（和被测用例同优先级，才能真正挤占），负载以 ``os.getloadavg()`` 记录开始与结束值；
  1 分钟平均负载爬升慢，所以先等 ``--ramp-seconds``（缺省 20 秒）再开跑，报告里同时给出开始与结束的
  1 分钟平均负载与烧核进程数。
* 退出时（含 Ctrl-C、异常）一定终止全部烧核进程。

退出码：0 全部通过；1 有失败；2 参数错误。
例：
    python3 -B tools/ci/time_jitter_stress.py --dry-run
    python3 -B tools/ci/time_jitter_stress.py --go-repeat 3 --python-repeat 2 --report /tmp/jitter.json

本脚本是重型命令（烧核加编译 Go），本机请用 ``heavy-run`` 排队，且同一时刻不要再跑别的重活。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import time_threshold_lint as lint  # noqa: E402

BURNER_SOURCE = "while True:\n    pass\n"


@dataclass(frozen=True)
class Case:
    kind: str  # "go" 或 "python"
    name: str  # Go：TestXxx；Python：模块.类.方法
    directory: str = ""  # Go：包目录（相对仓库根）
    tags: str = ""  # Go：构建标签（unit／integration／空）
    source: str = ""  # 来源文件，仅供报告

    @property
    def label(self) -> str:
        return f"{self.directory}::{self.name}" if self.kind == "go" else self.name


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def _go_tags(path: Path) -> str:
    """读 Go 测试文件头部的 ``//go:build`` 约束，取 unit／integration／e2e 标签；没有则为空。"""

    try:
        head = path.read_text(encoding="utf-8").splitlines()[:12]
    except OSError:
        return ""
    for line in head:
        match = re.match(r"//go:build\s+(.*)", line)
        if match:
            for tag in ("integration", "e2e", "unit"):
                if re.search(rf"\b{tag}\b", match.group(1)) and f"!{tag}" not in match.group(1):
                    return tag
    return ""


def _python_module(relative: str) -> str:
    return relative[: -len(".py")].replace("/", ".")


def cases_from_scan(root: Path, paths: list[Path]) -> tuple[list[Case], list[str]]:
    findings = lint.scan(paths or [root / name for name in lint.DEFAULT_ROOTS if (root / name).exists()], root)
    cases: dict[tuple[str, str, str], Case] = {}
    unmapped: list[str] = []
    for item in findings:
        if not item.test:
            unmapped.append(f"{item.path}:{item.line}")
            continue
        if item.path.endswith(".go"):
            directory = str(Path(item.path).parent)
            tags = _go_tags(root / item.path)
            cases[("go", directory + "|" + tags, item.test)] = Case("go", item.test, directory, tags, item.path)
        else:
            cases[("python", "", _python_module(item.path) + "." + item.test)] = Case("python", _python_module(item.path) + "." + item.test, source=item.path)
    return sorted(cases.values(), key=lambda case: (case.kind, case.directory, case.name)), unmapped


def cases_from_file(path: Path) -> list[Case]:
    cases = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row["kind"] == "go":
            cases.append(Case("go", row["name"], row["dir"], row.get("tags", ""), "cases-file"))
        else:
            cases.append(Case("python", row["id"], source="cases-file"))
    return cases


# ---------------------------------------------------------------------------
# 烧核
# ---------------------------------------------------------------------------


class Burners:
    """常驻忙循环进程；退出时一定回收。"""

    def __init__(self, count: int):
        self.count = count
        self.processes: list[subprocess.Popen] = []

    def __enter__(self) -> "Burners":
        for _ in range(self.count):
            self.processes.append(subprocess.Popen([sys.executable, "-c", BURNER_SOURCE], stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return self

    def __exit__(self, *_ignored: object) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def run_go_group_once(root: Path, directory: str, tags: str, names: list[str], timeout: int) -> dict[str, dict]:
    """一遍：``go test -count=1`` 起一个全新的测试进程。

    不用 ``-count=N``：同一进程里重复跑会撞上包级全局状态（限流节流表、最短间隔登记等），第二遍起的失败是重复运行造成的
    假象，不是负载抖动。每遍一个新进程，编译结果有缓存，只付一次。
    """

    pattern = "^(" + "|".join(sorted(set(names))) + ")$"
    command = ["go", "test", "-json", "-count", "1", "-run", pattern, "-p", "1"]
    if tags:
        command += [f"-tags={tags}"]
    command.append("./" + str(Path(directory).relative_to("backend")) if directory.startswith("backend/") else "./" + directory)
    workdir = root / "backend" if directory.startswith("backend/") else root
    results: dict[str, dict] = {name: {"runs": 0, "failures": 0, "status": "未执行"} for name in names}
    try:
        completed = subprocess.run(command, cwd=workdir, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        for name in names:
            results[name]["status"] = f"超时（{timeout} 秒）"
        return results
    output, build_failed = completed.stdout, False
    if completed.returncode != 0 and not output.strip():
        output, build_failed = completed.stderr, True
    messages: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        test = event.get("Test")
        if not test or "/" in test or test not in results:
            continue
        action = event.get("Action")
        if action == "output" and event.get("Output") and event["Output"].strip():
            messages[test].append(event["Output"].rstrip()[:300])
        if action in {"pass", "fail", "skip"}:
            results[test]["runs"] += 1
            if action == "fail":
                results[test]["failures"] += 1
            results[test]["status"] = "已执行"
            if action == "skip":
                results[test]["skips"] = results[test].get("skips", 0) + 1
    if build_failed:
        for name in names:
            results[name]["status"] = "编译或启动失败：" + output.strip()[:200]
    for name, notes in messages.items():
        if results[name]["failures"]:
            interesting = [line for line in notes if "FAIL" in line or "Error" in line or "_test.go" in line]
            results[name]["messages"] = (interesting or notes)[-6:]
    return results


def run_go_group(root: Path, directory: str, tags: str, names: list[str], repeat: int, timeout: int) -> dict[str, dict]:
    """重复 ``repeat`` 遍（每遍一个新进程）并合并计数。"""

    merged: dict[str, dict] = {}
    for _ in range(repeat):
        for name, outcome in run_go_group_once(root, directory, tags, names, timeout).items():
            total = merged.setdefault(name, {"runs": 0, "failures": 0, "status": outcome["status"]})
            total["runs"] += outcome["runs"]
            total["failures"] += outcome["failures"]
            if outcome["status"] != "已执行":
                total["status"] = outcome["status"]
            if outcome.get("messages"):
                total.setdefault("messages", []).extend(outcome["messages"])
                total["messages"] = total["messages"][-6:]
    return merged


def run_python_case(root: Path, case: Case, repeat: int, timeout: int) -> dict:
    result = {"runs": 0, "failures": 0, "status": "已执行"}
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for _ in range(repeat):
        try:
            completed = subprocess.run([sys.executable, "-B", "-m", "unittest", case.name], cwd=root, env=environment,
                                       text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            result["runs"] += 1
            result["failures"] += 1
            result["messages"] = [f"超时（{timeout} 秒）"]
            continue
        result["runs"] += 1
        if completed.returncode != 0:
            result["failures"] += 1
            result.setdefault("messages", []).append((completed.stderr or completed.stdout).strip()[-300:])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="时间敏感用例的抖动压测")
    parser.add_argument("paths", nargs="*", type=Path, help="只在这些文件或目录里取清单（缺省 tools 与 backend）")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--cases-file", type=Path, help="显式清单 JSON；给了就不再扫描")
    parser.add_argument("--load", type=int, default=12, help="烧核进程数（缺省 12，使可运行线程数超过 12）")
    parser.add_argument("--ramp-seconds", type=float, default=20.0, help="烧核后等多久再开跑（缺省 20 秒）")
    parser.add_argument("--go-repeat", type=int, default=3)
    parser.add_argument("--python-repeat", type=int, default=2)
    parser.add_argument("--group-timeout", type=int, default=1800, help="单个 Go 包的 go test 总时限（秒）")
    parser.add_argument("--case-timeout", type=int, default=900, help="单个 Python 用例单遍时限（秒）")
    parser.add_argument("--kind", choices=("go", "python"), help="只跑一种")
    parser.add_argument("--report", type=Path, help="把完整结果写成 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不烧核不运行")
    arguments = parser.parse_args(argv)
    root = (arguments.root or lint._repository_root()).resolve()
    if arguments.cases_file:
        cases, unmapped = cases_from_file(arguments.cases_file), []
    else:
        cases, unmapped = cases_from_scan(root, arguments.paths)
    if arguments.kind:
        cases = [case for case in cases if case.kind == arguments.kind]
    skipped = [case for case in cases if case.kind == "go" and case.tags in {"integration", "e2e"}]
    runnable = [case for case in cases if case not in skipped]
    print(f"清单：{len(cases)} 个用例（Go {sum(c.kind == 'go' for c in cases)}，Python {sum(c.kind == 'python' for c in cases)}）；"
          f"需要 Docker 而跳过 {len(skipped)}；命中不在测试函数里（辅助代码）{len(unmapped)} 处。")
    if arguments.dry_run:
        for case in cases:
            print(("跳过 " if case in skipped else "") + case.kind, case.label)
        return 0
    results: dict[str, dict] = {}
    started = time.time()
    load_start = os.getloadavg()[0]
    with Burners(arguments.load):
        time.sleep(arguments.ramp_seconds)
        load_after_ramp = os.getloadavg()[0]
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for case in runnable:
            if case.kind == "go":
                groups[(case.directory, case.tags)].append(case.name)
        for (directory, tags), names in sorted(groups.items()):
            print(f"[go] {directory} tags={tags or '-'} {len(names)} 个用例 × {arguments.go_repeat} 遍", flush=True)
            for name, outcome in run_go_group(root, directory, tags, names, arguments.go_repeat, arguments.group_timeout).items():
                if name != "__error__":
                    results[f"{directory}::{name}"] = outcome
        for case in runnable:
            if case.kind == "python":
                print(f"[python] {case.name} × {arguments.python_repeat} 遍", flush=True)
                results[case.name] = run_python_case(root, case, arguments.python_repeat, arguments.case_timeout)
        load_end = os.getloadavg()[0]
    failed = {label: outcome for label, outcome in results.items() if outcome.get("failures")}
    not_run = {label: outcome for label, outcome in results.items() if outcome.get("status") != "已执行"}
    summary = {
        "cases": len(cases), "executed": len(results) - len(not_run), "skipped_integration": [case.label for case in skipped],
        "failed_cases": len(failed), "failed_runs": sum(item["failures"] for item in failed.values()),
        "total_runs": sum(item["runs"] for item in results.values()), "not_run": {label: item["status"] for label, item in not_run.items()},
        "load": {"burners": arguments.load, "loadavg_at_start": round(load_start, 2), "loadavg_after_ramp": round(load_after_ramp, 2),
                 "loadavg_at_end": round(load_end, 2), "cpu_count": os.cpu_count()},
        "seconds": round(time.time() - started, 1), "failures": failed, "unmapped": unmapped,
    }
    if arguments.report:
        arguments.report.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key not in {"failures", "unmapped"}}, ensure_ascii=False, indent=1))
    for label, item in failed.items():
        print(f"失败 {item['failures']}/{item['runs']}：{label}", *(item.get("messages") or []), sep="\n    ")
    return 1 if failed or not_run else 0


if __name__ == "__main__":
    def _stop(_signal: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    sys.exit(main())
