#!/usr/bin/env python3
"""测试里的“绝对时间阈值”静态检查（独立脚本，暂不接入任何门禁或 CI）。

为什么要有它
============
测试里一旦写死“耗时必须小于某个常数”“超时只给几百毫秒”“分配次数不得超过某个贴着正常值的数”，在满载
机器（ARM64 执行器按 4 核跑满、CI 的 4 vCPU 分片并发）上就会抖红：同一份代码、同一个提交，换个时间
重跑就绿了。0.163 升级时因满载抖红而逐个修掉的用例就是这一类（token refresh 起跑间隔、共享截止时间、
节点表预分配上限、WS 桥接整秒边界）。本脚本把这类写法找出来，要求每一处要么改成不依赖绝对时间的写法，
要么用显式豁免标记说明“这里本身就在验证超时语义”，不做一刀切禁止。

检查的四类写法（规则编号出现在输出里）
======================================
* ``elapsed-bound``   ：把“实测已用时间”和一个数字常量做**上界**比较，例如 ``assertLess(elapsed, 0.5)``、
  ``assert time.monotonic() - start < 3``、Go 的 ``require.Less(t, elapsed, 200*time.Millisecond)``、
  ``if elapsed > 2*time.Second { t.Fatal }``。只报上界：下界（“至少等了多久”）在机器变慢时只会更容易满足，
  不会抖红，所以不报。常数 0（“非负”）不报；常数不小于 ``--loose-seconds``（缺省 5 秒）的视为看门狗式的宽裕上限，
  不报。
* ``short-timeout``   ：小于 1 秒的超时、截止或等待上限，例如 ``join(timeout=0.2)``、``settimeout(0.2)``、
  ``context.WithTimeout(ctx, 50*time.Millisecond)``、``select { case <-time.After(100*time.Millisecond): }``、
  ``require.Eventually(t, f, 300*time.Millisecond, ...)``、``require.Never(...)``。
* ``alloc-bound``     ：Go 里把 ``testing.AllocsPerRun`` 的结果和一个非零常数比较，或者把
  ``runtime.MemStats`` 的字段差值和常数上限比较（上限贴着正常值，换 Go 版本或开竞态检测就越界）；
  比较对象是 0（“零分配”）的不报。
* ``clock-deadline``  ：Python 里 ``deadline = time.monotonic() + 常数`` 且常数小于 1 秒。

豁免标记
========
在命中行的行尾、命中行的上一行（整行注释）、或命中所在测试函数（Go 的 ``func``、Python 的 ``def``）的定义行行尾 / 其上一行（整行注释）写：

    # time-threshold: allow <原因>       （Python）
    // time-threshold: allow <原因>      （Go）

原因必须非空，说明“为什么这里的绝对时间是被验证对象的一部分”（例如：被测对象就是超时语义；或这是
“窗口内不应发生”的反向检查，机器变慢只会让它更宽松）。整个文件都在验证超时语义时，可在文件前 40 行写
``time-threshold: allow-file <原因>``。没有原因的标记视为无效，仍然报告。

用法
====
    python3 -B tools/ci/time_threshold_lint.py                        # 扫 tools 与 backend，列出未豁免命中，有命中退出 1
    python3 -B tools/ci/time_threshold_lint.py --list-all             # 连已豁免、基线内的一起列
    python3 -B tools/ci/time_threshold_lint.py --format json
    python3 -B tools/ci/time_threshold_lint.py --baseline FILE        # 基线里的命中不算新增（过渡用）
    python3 -B tools/ci/time_threshold_lint.py --write-baseline FILE  # 把当前未豁免命中写成基线
    python3 -B tools/ci/time_threshold_lint.py path/to/x_test.go ...  # 只扫指定文件或目录
    python3 -B tools/ci/time_threshold_lint.py --self-test            # 内置样例自检

退出码：0 无新增未豁免命中；1 有；2 参数或读取错误。
基线条目的键是“文件相对路径＋规则＋命中行去空白后的文本”，不含行号，所以无关改动移动行号不会让基线失效。

实现说明：Python 用 ``ast``；Go 不做类型分析，用括号配平的正则，宁可多报交给人判。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

ALLOW_RE = re.compile(r"time-threshold:\s*allow(?!-file)\b[ \t]*(?P<reason>[^\n]*)")
ALLOW_FILE_RE = re.compile(r"time-threshold:\s*allow-file\b[ \t]*(?P<reason>[^\n]*)")

# 默认扫描的根（相对仓库根）与要跳过的目录名。
DEFAULT_ROOTS = ("tools", "backend")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "vendor", "ent", ".claude", "local-analysis", "dist", "build"}
# 上界常数不小于这么多秒的，视为看门狗式的宽裕上限，不报告。
DEFAULT_LOOSE_SECONDS = 5.0

RULES = {
    "elapsed-bound": "实测已用时间与数字常量做上界比较",
    "short-timeout": "小于 1 秒的超时、截止或等待上限",
    "alloc-bound": "分配次数或内存上限贴着非零常数",
    "clock-deadline": "时钟加不足 1 秒常数的截止时间",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str
    detail: str
    exempt_reason: str | None = None
    # 命中所在的测试：Python 为 ``类名.方法名``，Go 为 ``TestXxx``；不在测试函数里（辅助代码）为 None。抖动压测用它定位用例。
    test: str | None = None

    def key(self) -> str:
        return f"{self.path}\t{self.rule}\t{' '.join(self.text.split())}"


# ---------------------------------------------------------------------------
# 豁免标记
# ---------------------------------------------------------------------------


def _allow_reason(line: str) -> str | None:
    match = ALLOW_RE.search(line)
    if not match:
        return None
    reason = match.group("reason").strip()
    return reason or None


def _file_allow(lines: list[str]) -> str | None:
    for line in lines[:40]:
        match = ALLOW_FILE_RE.search(line)
        if match and match.group("reason").strip():
            return match.group("reason").strip()
    return None


def _exemption(lines: list[str], index: int, scope_start: int | None) -> str | None:
    """命中行（0 起）的豁免原因。

    * 命中行本身：行尾标记。
    * 命中行的上一行、所在函数定义行的上一行：只认**整行注释**里的标记（行尾带标记的代码行只管它自己，
      不会把豁免“传染”给下一行）。
    * 所在函数的定义行：行尾标记。
    """

    def comment_only(position: int) -> bool:
        return 0 <= position < len(lines) and lines[position].lstrip().startswith(("#", "//"))

    candidates = [index, index - 1 if comment_only(index - 1) else -1]
    if scope_start is not None:
        candidates += [scope_start, scope_start - 1 if comment_only(scope_start - 1) else -1]
    for item in candidates:
        if 0 <= item < len(lines):
            reason = _allow_reason(lines[item])
            if reason:
                return reason
    return None


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_ELAPSED_NAME = re.compile(r"elapsed|took|latency|waited|spent|runtime|wall_?time|duration_seconds", re.I)
_CLOCK_CALLS = {"monotonic", "perf_counter", "monotonic_ns", "perf_counter_ns", "time", "time_ns", "process_time"}


def _const_number(node: ast.AST) -> float | None:
    """纯数字常量（含负号与常量算术）求值；含名字则返回 None。"""

    try:
        value = ast.literal_eval(node)
    except Exception:  # noqa: BLE001 —— 不是字面量就继续尝试常量算术
        value = None
    else:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if isinstance(node, ast.BinOp):
        left, right = _const_number(node.left), _const_number(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        except ZeroDivisionError:
            return None
    return None


def _has_clock_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in _CLOCK_CALLS:
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                if func.attr != "time" or owner == "time":
                    return True
            if isinstance(func, ast.Name) and func.id in _CLOCK_CALLS - {"time"}:
                return True
    return False


def _looks_elapsed(node: ast.AST) -> bool:
    """表达式像“实测已用时间”：名字里带 elapsed／took 等，或是时钟读数相减。"""

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and (_has_clock_call(node.left) or _has_clock_call(node.right)):
        return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return bool(_ELAPSED_NAME.search(node.id if isinstance(node, ast.Name) else node.attr))
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return bool(_ELAPSED_NAME.search(node.slice.value))
    return False


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, loose: float):
        self.loose = loose
        self.hits: list[tuple[int, str, str, int | None, str | None]] = []
        self.scope: list[int] = []
        self.classes: list[str] = []
        self.test_names: list[str] = []
        # 单独成句的 ``event.wait(0.15)``：返回值被丢弃，是“睡一拍”的轮询节拍，不是等待上限。
        self.sleep_calls: set[int] = set()

    def visit_Expr(self, node: ast.Expr) -> None:  # noqa: N802
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "wait":
            self.sleep_calls.add(id(node.value))
        self.generic_visit(node)

    def _record(self, node: ast.AST, rule: str, detail: str) -> None:
        test = self.test_names[0] if self.test_names else None
        self.hits.append((getattr(node, "lineno", 1), rule, detail, self.scope[-1] if self.scope else None, test))

    def _upper(self, expression: ast.AST, limit: float, node: ast.AST, how: str) -> None:
        if limit > 0 and limit < self.loose and _looks_elapsed(expression):
            self._record(node, "elapsed-bound", f"{how}：已用时间上界 {limit:g} 秒")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.scope.append(min([node.lineno, *[d.lineno for d in node.decorator_list]]))
        is_test = not self.test_names and node.name.startswith("test")
        if is_test:
            self.test_names.append(".".join([*self.classes, node.name]))
        self.generic_visit(node)
        if is_test:
            self.test_names.pop()
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        operands = [node.left, *node.comparators]
        for left, op, right in zip(operands, node.ops, operands[1:]):
            # 上界：expression < C、expression <= C、C > expression、C >= expression
            if isinstance(op, (ast.Lt, ast.LtE)):
                limit = _const_number(right)
                if limit is not None:
                    self._upper(left, limit, node, "比较")
            elif isinstance(op, (ast.Gt, ast.GtE)):
                limit = _const_number(left)
                if limit is not None:
                    self._upper(right, limit, node, "比较")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name in {"assertLess", "assertLessEqual"} and len(node.args) >= 2:
            limit = _const_number(node.args[1])
            if limit is not None:
                self._upper(node.args[0], limit, node, name)
        elif name in {"assertGreater", "assertGreaterEqual"} and len(node.args) >= 2:
            limit = _const_number(node.args[0])
            if limit is not None:
                self._upper(node.args[1], limit, node, name)
        # 小于 1 秒的超时：关键字实参，或常见阻塞调用的第一个位置实参。
        for keyword in node.keywords:
            if keyword.arg and re.search(r"timeout|deadline|grace|wait_?seconds", keyword.arg, re.I):
                limit = _const_number(keyword.value)
                if limit is not None and 0 < limit < 1:
                    self._record(node, "short-timeout", f"{keyword.arg}={limit:g} 秒")
        if name in {"join", "wait", "settimeout", "result"} and node.args and id(node) not in self.sleep_calls:
            limit = _const_number(node.args[0])
            if limit is not None and 0 < limit < 1:
                self._record(node, "short-timeout", f"{name}({limit:g}) 秒")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        value = node.value
        targets = " ".join(ast.unparse(target) for target in node.targets)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            for clock, number in ((value.left, value.right), (value.right, value.left)):
                limit = _const_number(number)
                if limit is not None and 0 < limit < 1 and _has_clock_call(clock):
                    self._record(node, "clock-deadline", f"{targets} = 时钟 + {limit:g} 秒")
        limit = _const_number(value)
        if limit is not None and 0 < limit < 1 and re.search(r"timeout|deadline|grace", targets, re.I):
            self._record(node, "short-timeout", f"{targets} = {limit:g} 秒")
        self.generic_visit(node)


def scan_python(relative: str, text: str, loose: float) -> list[Finding]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError:
        return []
    visitor = _PythonVisitor(loose)
    visitor.visit(tree)
    file_reason = _file_allow(lines)
    findings: dict[tuple[int, str], Finding] = {}
    for line, rule, detail, scope, test in visitor.hits:
        reason = file_reason or _exemption(lines, line - 1, None if scope is None else scope - 1)
        findings[(line, rule)] = Finding(relative, line, rule, lines[line - 1].strip() if 0 < line <= len(lines) else "", detail, reason, test)
    return [findings[key] for key in sorted(findings)]


# ---------------------------------------------------------------------------
# Go（括号配平加正则；不做类型分析）
# ---------------------------------------------------------------------------

_GO_UNITS = {"Nanosecond": 1e-9, "Microsecond": 1e-6, "Millisecond": 1e-3, "Second": 1.0, "Minute": 60.0, "Hour": 3600.0}
_UNIT = "Nanosecond|Microsecond|Millisecond|Second|Minute|Hour"
# 40*time.Millisecond、time.Millisecond*40、time.Duration(40)*time.Millisecond、20 * time.Second、time.Second。
_GO_DURATION = re.compile(
    rf"(?P<n1>\d[\d_]*(?:\.\d+)?)\s*\*\s*(?:time\.)?(?P<u1>{_UNIT})\b"
    rf"|(?:time\.)?(?P<u2>{_UNIT})\s*\*\s*(?P<n2>\d[\d_]*(?:\.\d+)?)\b"
    rf"|time\.Duration\((?P<n3>\d[\d_]*)\)\s*\*\s*time\.(?P<u3>{_UNIT})\b"
    rf"|(?<![\w.])time\.(?P<u4>{_UNIT})\b"
)
_GO_TIMEOUT_CALL = re.compile(
    r"\bcontext\.WithTimeout\(|\bcontext\.WithDeadline\(|\btime\.After\(|\btime\.NewTimer\(|\btime\.AfterFunc\(|"
    r"\.SetDeadline\(|\.SetReadDeadline\(|\.SetWriteDeadline\(|\bTimeout:\s|"
    r"\b(?:require|assert)\.(?:Eventually|Eventuallyf|EventuallyWithT|Never|Neverf)\("
)
_GO_ELAPSED = re.compile(r"\belapsed\w*|\btook\w*|\blatency\w*|\bwaited\w*|\bspent\w*|time\.Since\(|time\.Now\(\)\.Sub\(", re.I)
_GO_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")
_GO_ARGS_CMP = {"Less": "upper", "LessOrEqual": "upper", "Lessf": "upper", "LessOrEqualf": "upper",
                "Greater": "lower", "GreaterOrEqual": "lower", "Greaterf": "lower", "GreaterOrEqualf": "lower"}


def _go_durations(segment: str) -> list[float]:
    seconds: list[float] = []
    for match in _GO_DURATION.finditer(segment):
        if match.group("n1"):
            seconds.append(float(match.group("n1").replace("_", "")) * _GO_UNITS[match.group("u1")])
        elif match.group("n2"):
            seconds.append(float(match.group("n2").replace("_", "")) * _GO_UNITS[match.group("u2")])
        elif match.group("n3"):
            seconds.append(float(match.group("n3").replace("_", "")) * _GO_UNITS[match.group("u3")])
        else:
            seconds.append(_GO_UNITS[match.group("u4")])
    return seconds


def _call_arguments(text: str, start: int) -> list[str]:
    """从 ``start``（左括号之后）起按括号配平切出实参文本。"""

    depth, current, arguments, in_string = 1, [], [], None
    index = start
    while index < len(text) and depth > 0:
        char = text[index]
        if in_string:
            current.append(char)
            if char == "\\" and in_string != "`":
                index += 1
                if index < len(text):
                    current.append(text[index])
            elif char == in_string:
                in_string = None
        elif char in "\"`'":
            in_string = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            if depth > 0:
                current.append(char)
        elif char == "," and depth == 1:
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    arguments.append("".join(current).strip())
    return arguments


def scan_go(relative: str, text: str, loose: float) -> list[Finding]:
    lines = text.splitlines()
    file_reason = _file_allow(lines)
    scopes: list[int | None] = []
    current: int | None = None
    for number, line in enumerate(lines):
        if _GO_FUNC.match(line):
            current = number
        scopes.append(current)
    offsets, running = [], 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1
    findings: dict[tuple[int, str], Finding] = {}

    def add(index: int, rule: str, detail: str) -> None:
        reason = file_reason or _exemption(lines, index, scopes[index])
        owner = None
        if scopes[index] is not None:
            matched = re.match(r"func\s+(Test\w+)\s*\(\s*\w+\s+\*testing\.T\s*\)", lines[scopes[index]])
            owner = matched.group(1) if matched else None
        findings[(index + 1, rule)] = Finding(relative, index + 1, rule, lines[index].strip(), detail, reason, owner)

    def line_of(offset: int) -> int:
        low, high = 0, len(offsets) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if offsets[middle] <= offset:
                low = middle
            else:
                high = middle - 1
        return low

    # --- 短超时 ---------------------------------------------------------------------------------
    for match in _GO_TIMEOUT_CALL.finditer(text):
        index = line_of(match.start())
        if lines[index].lstrip().startswith("//"):
            continue
        token = match.group(0)
        if token.endswith("("):
            arguments = _call_arguments(text, match.end())
            name = token[:-1]
            if "Eventually" in name:
                candidates = arguments[2:3]  # Eventually(t, cond, waitFor, tick)
            elif "Never" in name:
                candidates = arguments[2:3]  # Never(t, cond, waitFor, tick)：窗口内不应发生
            elif name in {"context.WithTimeout", "context.WithDeadline"}:
                candidates = arguments[1:2]
            else:
                candidates = arguments[:1]
        else:
            name = "Timeout"
            candidates = [lines[index][lines[index].find("Timeout:") + len("Timeout:"):]]
        for argument in candidates:
            durations = _go_durations(argument)
            if durations and 0 < min(durations) < 1.0:
                add(index, "short-timeout", f"{name} 的等待上限 {min(durations):g} 秒")
                break

    # --- 实测耗时上界与分配上限 -----------------------------------------------------------------
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        code = stripped.split("//", 1)[0]
        function = re.search(r"\b(?:require|assert)\.(Less|LessOrEqual|Lessf|LessOrEqualf|Greater|GreaterOrEqual|Greaterf|GreaterOrEqualf)\(", code)
        if function:
            arguments = _call_arguments(code, function.end())
            if len(arguments) >= 3:
                direction = _GO_ARGS_CMP[function.group(1)]
                first, second = arguments[1], arguments[2]
                measured, bound = (first, second) if direction == "upper" else (second, first)
                durations = _go_durations(bound)
                if _GO_ELAPSED.search(measured) and durations and 0 < sum(durations) < loose and not _GO_ELAPSED.search(bound):
                    add(index, "elapsed-bound", f"{function.group(1)}：已用时间上界 {sum(durations):g} 秒")
        else:
            condition = re.match(r"(?:if|for|\}\s*else\s+if)\b(.*?)\{?\s*$", code) or re.search(r";\s*(.*?)\{\s*$", code)
            comparison = re.search(r"(?P<l>[^<>=!&|]+?)\s*(?P<op><=|>=|<|>)\s*(?P<r>[^<>=!&|{]+)", condition.group(1) if condition else "")
            if comparison:
                left, op, right = comparison.group("l"), comparison.group("op"), comparison.group("r")
                is_loop = code.startswith("for")
                for measured, bound, measured_on_left in ((left, right, True), (right, left, False)):
                    durations = _go_durations(bound)
                    if not (_GO_ELAPSED.search(measured) and durations and 0 < sum(durations) < loose and not _GO_ELAPSED.search(bound)):
                        continue
                    # 上界：“耗时 > 常数”“常数 < 耗时”会让用例失败；循环里的“耗时 < 常数”是等待上限，也算上界。
                    # “if 耗时 < 常数 {Fatal}”是下界，不报。
                    greater = (op in {">", ">="}) == measured_on_left
                    if greater or (is_loop and measured_on_left and op in {"<", "<="}):
                        add(index, "elapsed-bound", f"比较：已用时间上界 {sum(durations):g} 秒")
                        break
        allocs = re.search(r"(\w+)\s*:?=\s*testing\.AllocsPerRun\(", code)
        if allocs:
            variable = re.escape(allocs.group(1))
            window = "\n".join(lines[index + 1:index + 14])
            for pattern in (
                rf"(?:Less|LessOrEqual|Lessf|LessOrEqualf)\(\s*\w+\s*,\s*{variable}\s*,\s*(?:float64\()?([\d.]+)",
                rf"(?:Greater|GreaterOrEqual)\(\s*\w+\s*,\s*(?:float64\()?([\d.]+)\)?\s*,\s*{variable}\b",
                rf"\b{variable}\s*(?:>|>=)\s*(?:float64\()?([\d.]+)",
            ):
                bound = re.search(pattern, window)
                if bound and float(bound.group(1)) != 0:
                    add(index, "alloc-bound", f"AllocsPerRun 结果的上限 {bound.group(1)}")
                    break
        if re.match(r"(?:if\b|\}?\s*else if\b)", code) and re.search(r"\.(?:Mallocs|HeapAlloc|TotalAlloc|HeapObjects|Alloc)\b[^\n]*(?:<=|<)\s*(?:uint64\()?\d", code) or \
                re.search(r"(?:Less|LessOrEqual)\([^\n]*\.(?:Mallocs|HeapAlloc|TotalAlloc|HeapObjects|Alloc)\b[^\n]*,\s*(?:u?int(?:64)?\()?\d", code):
            add(index, "alloc-bound", "MemStats 字段与常数上限比较")
    return [findings[key] for key in sorted(findings)]


# ---------------------------------------------------------------------------
# 遍历与输出
# ---------------------------------------------------------------------------


def _is_python_test(path: Path) -> bool:
    return path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py") or "tests" in path.parts)


def _is_go_test(path: Path) -> bool:
    return path.name.endswith("_test.go")


def iter_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for directory, names, files in os.walk(root):
            names[:] = sorted(name for name in names if name not in SKIP_DIRS)
            for file in sorted(files):
                path = Path(directory) / file
                if _is_python_test(path) or _is_go_test(path):
                    yield path


def scan(paths: Iterable[Path], repository_root: Path, loose: float = DEFAULT_LOOSE_SECONDS) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_files(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            relative = path.resolve().relative_to(repository_root.resolve()).as_posix()
        except ValueError:
            relative = path.as_posix()
        if path.suffix == ".py":
            findings.extend(scan_python(relative, text, loose))
        elif path.suffix == ".go":
            findings.extend(scan_go(relative, text, loose))
    return findings


_SELF_TEST_PY = '''\
import time, unittest

class T(unittest.TestCase):
    def test_a(self):
        start = time.monotonic()
        self.assertLess(time.monotonic() - start, 0.5)            # 命中 elapsed-bound
        self.assertGreaterEqual(time.monotonic() - start, 0.5)    # 下界，不报
        self.assertLess(time.monotonic() - start, 60)             # 宽裕，不报
        self.assertLess(time.monotonic() - start, 0.5)  # time-threshold: allow 验证超时语义本身
        start = 0
        thread.join(timeout=0.2)                                  # 命中 short-timeout
        deadline = time.monotonic() + 0.3                         # 命中 clock-deadline

    # time-threshold: allow 整个用例在验证超时
    def test_b(self):
        elapsed = 1
        self.assertLess(elapsed, 0.1)
'''

_SELF_TEST_GO = '''\
package x

func TestA(t *testing.T) {
	require.Less(t, time.Since(start), 50*time.Millisecond)      // 命中 elapsed-bound
	require.GreaterOrEqual(t, elapsed, 40*time.Millisecond)      // 下界，不报
	if elapsed > 2*time.Second {                                 // 命中 elapsed-bound
		t.Fatal("x")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)  // 命中 short-timeout
	select {
	case <-time.After(100 * time.Millisecond): // 命中 short-timeout
	}
	require.Eventually(t, f, 5*time.Second, 10*time.Millisecond) // 等待上限 5 秒，不报
	allocs := testing.AllocsPerRun(10, f)
	require.LessOrEqual(t, allocs, float64(3))                    // 命中 alloc-bound
}

// time-threshold: allow 本用例就是验证超时语义
func TestB(t *testing.T) {
	require.Less(t, time.Since(start), 50*time.Millisecond)
}
'''


def self_test() -> int:
    """内置样例：每条规则至少命中一次、下界与宽裕上界不报、豁免标记生效。"""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "test_sample.py").write_text(_SELF_TEST_PY, encoding="utf-8")
        (root / "sample_test.go").write_text(_SELF_TEST_GO, encoding="utf-8")
        findings = scan([root], root)
    got = sorted((item.path, item.line, item.rule, item.exempt_reason is not None) for item in findings)
    want = sorted([
        ("test_sample.py", 6, "elapsed-bound", False),
        ("test_sample.py", 9, "elapsed-bound", True),
        ("test_sample.py", 11, "short-timeout", False),
        ("test_sample.py", 12, "clock-deadline", False),
        ("test_sample.py", 17, "elapsed-bound", True),
        ("sample_test.go", 4, "elapsed-bound", False),
        ("sample_test.go", 6, "elapsed-bound", False),
        ("sample_test.go", 9, "short-timeout", False),
        ("sample_test.go", 11, "short-timeout", False),
        ("sample_test.go", 14, "alloc-bound", False),
        ("sample_test.go", 20, "elapsed-bound", True),
    ])
    if got != want:
        print("自检失败\n期望：", *want, "\n实际：", *got, sep="\n", file=sys.stderr)
        return 1
    print("自检通过")
    return 0


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试里的绝对时间阈值静态检查")
    parser.add_argument("paths", nargs="*", type=Path, help="要扫的文件或目录；缺省扫仓库的 tools 与 backend")
    parser.add_argument("--root", type=Path, default=None, help="仓库根（缺省取脚本所在仓库）")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--list-all", action="store_true", help="连已豁免、基线内的命中一起列出")
    parser.add_argument("--rule", action="append", choices=sorted(RULES), help="只看指定规则，可重复")
    parser.add_argument("--loose-seconds", type=float, default=DEFAULT_LOOSE_SECONDS, help="耗时上界不小于这个秒数的不报（缺省 5）")
    parser.add_argument("--baseline", type=Path, help="基线文件；基线内的命中不算新增")
    parser.add_argument("--write-baseline", type=Path, help="把当前未豁免命中写成基线后退出 0")
    parser.add_argument("--self-test", action="store_true", help="跑内置样例自检")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        return self_test()
    root = (arguments.root or _repository_root()).resolve()
    targets = arguments.paths or [root / name for name in DEFAULT_ROOTS if (root / name).exists()]
    findings = scan(targets, root, arguments.loose_seconds)
    if arguments.rule:
        findings = [item for item in findings if item.rule in set(arguments.rule)]
    baseline: set[str] = set()
    if arguments.baseline:
        try:
            baseline = set(json.loads(arguments.baseline.read_text(encoding="utf-8"))["entries"])
        except (OSError, ValueError, KeyError) as error:
            print(f"基线读取失败：{error}", file=sys.stderr)
            return 2
    pending = [item for item in findings if item.exempt_reason is None and item.key() not in baseline]
    if arguments.write_baseline:
        entries = sorted({item.key() for item in findings if item.exempt_reason is None})
        arguments.write_baseline.write_text(
            json.dumps({"schema_version": "time-threshold-baseline/v1", "entries": entries}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"基线已写入 {arguments.write_baseline}（{len(entries)} 条）")
        return 0
    shown = findings if arguments.list_all else pending
    if arguments.format == "json":
        print(json.dumps([asdict(item) for item in shown], ensure_ascii=False, indent=1))
    else:
        for item in shown:
            state = f" [已豁免：{item.exempt_reason}]" if item.exempt_reason else (" [基线内]" if item.key() in baseline else "")
            print(f"{item.path}:{item.line}: {item.rule}（{item.detail}）{state}\n    {item.text}")
    exempt = sum(1 for item in findings if item.exempt_reason is not None)
    in_baseline = sum(1 for item in findings if item.exempt_reason is None and item.key() in baseline)
    print(f"合计命中 {len(findings)} 处；已豁免 {exempt}；基线内 {in_baseline}；未豁免新增 {len(pending)}。", file=sys.stderr)
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
