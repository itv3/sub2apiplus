"""试验区快线的失败归因：把“冻结摘要类”失败和功能失败分开，归因不清一律按功能失败处理。

背景：v0.2.17 试验区全量 64 分 29 秒，查出的失败里，出站规格线只有 1 条、go 测试线 47 条，全部是冻结摘要类
（试验区还没生成台账，冻结测试本来就会红），没有一条功能问题。快线不能被这类已知的待办挡住，但也绝不能
把真正的功能失败误当成“待台账闭合”放过。所以归因规则是保守的：

* 只有**全部**失败项都能证明属于冻结摘要类才记 ``frozen_digest_pending_ledger``；其余任何情况
  （其他单元失败、日志读不到或解析不了、编译失败、panic、超时、信号、失败用例不在冻结测试包里、
  用例名或失败信息不像摘要比对）一律记 ``functional``。
* 出站规格线（``make -k check-egress-spec``）只认 ``egress-spec-go-test`` 一个单元。仓库里其余子检查有的也是
  摘要比对（transition／maintenance 系列 Python 检查），但试验区真实日志里它们从未成为失败来源，没有证据
  就不放进白名单（宁可按功能失败阻断，也不放过）。需要加入时，必须在 ``EGRESS_SPEC_FROZEN_UNITS`` 里逐个写明依据。
* go test 的失败判定同时要求三件事：失败包在冻结测试所在的包里、每个顶层失败用例的名字带冻结类字样、
  有“这是冻结台账比对”的证据——失败信息带摘要比对类字样，或失败信息指出的测试源文件读取 ``docs/egress`` 台账。
  仓库里 ``internal/service`` 等包还有名字里带 Transition／Retirement 的功能测试（例如 ``TestGPT55Retirement*``、
  ``TestCanTransitionBatchImageJob``，它们的测试文件不读台账），只靠名字会把功能失败放过；只靠失败信息又会漏掉
  v0.2.17 试验区里真实出现的 ``TestUpstreamV0180SourceTransitionIsFrozen``（失败信息只有 ``invalid argument``，但它的
  测试文件读 ``docs/egress``）。

本模块只依赖标准库，由 ``gate_runner`` 在步骤结束后调用；归因只是记录，不改变步骤的通过／失败状态。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CLASS_FUNCTIONAL = "functional"
CLASS_FROZEN_PENDING = "frozen_digest_pending_ledger"
CLASSIFICATIONS = (CLASS_FUNCTIONAL, CLASS_FROZEN_PENDING)

# 仓库里冻结测试的命名（v0.2.17 试验区 47 条失败全部匹配，见 test_fast_attribution 的真实日志夹具）：
# TestXxxTransitionIsFrozen／XxxSuccessorRejectsMutation／XxxRetirementClosure／XxxSourceDrift 等。
FROZEN_TEST_NAME_RE = re.compile(r"Frozen|Transition|Successor|Drift|Retirement|RejectsMutation")
# 冻结测试的失败信息一律是摘要比对：「当前摘要不一致」「…漂移：path=… got=… want=…」「transition 条目非法」
# 「提交绑定不一致」「没有形成追加式 transition」。功能测试的失败信息不会带这些字样。
FROZEN_MESSAGE_RE = re.compile(r"摘要|漂移|绑定不一致|追加式|条目非法")
# 冻结测试所在的包（相对模块根）。仓库里 handler／repository 等包里同样有名字带 Transition 的功能测试，
# 不在此列的包失败一律按功能失败处理。
MODULE_PREFIX = "github.com/Wei-Shaw/sub2api/"
FROZEN_TEST_PACKAGES = ("internal/officialegress", "internal/service")
# 出站规格线里允许记为冻结摘要类的单元（去掉 egress-spec: 前缀后的目标名）。
EGRESS_SPEC_UNIT_PREFIX = "egress-spec:"
EGRESS_SPEC_FROZEN_UNITS = ("egress-spec-go-test",)

# 失败信息首行形如 ``upstream_v0180_source_transition_test.go:120: …``，取出测试源文件名。
_FAIL_SOURCE_RE = re.compile(r"^(?P<file>[\w.-]+_test\.go):\d+:")
FROZEN_SOURCE_MARKER = "docs/egress"
_PACKAGE_RESULT_RE = re.compile(r"^(?P<verdict>ok|FAIL|\?)\s+(?P<package>\S+)(?P<rest>.*)$")
_TOP_LEVEL_FAIL_RE = re.compile(r"^--- FAIL: (?P<name>\S+) \(")
# 任何一条都说明失败不是“断言不相等”这类可归因的用例失败，按功能失败处理。
_CRASH_MARKER_RE = re.compile(
    r"^(?:panic: |fatal error: |SIGQUIT|panic\(|signal: |test timed out after|FAIL\s+.*\[(?:build|setup) failed\]"
    r"|# [^\s]+|.*\[build failed\]|.*\[setup failed\])",
    re.MULTILINE,
)


@dataclass
class Attribution:
    """一次归因的结论与依据；``failed_tests`` 与 ``packages`` 供状态文件追溯。"""

    classification: str
    reason: str
    failed_tests: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        return {"failed_tests": self.failed_tests, "packages": self.packages}


def _functional(reason: str, **kwargs: Any) -> Attribution:
    return Attribution(CLASS_FUNCTIONAL, reason, **kwargs)


@dataclass
class _Segment:
    package: str
    verdict: str
    rest: str
    lines: list[str]
    failures: list[tuple[str, list[str]]] = field(default_factory=list)


def _split_failures(lines: list[str]) -> list[tuple[str, list[str]]]:
    """一个包的输出里，顶层 ``--- FAIL:`` 用例及紧随其后的缩进信息行。

    go test 把用例自己的输出缩进 4 格接在 ``--- FAIL`` 行之后；子测试的 ``--- FAIL`` 行本身也缩进，不算顶层。
    被测代码直接写到标准错误的日志不缩进，会截断信息行——这时信息行为空或不足，下游按功能失败处理。
    """

    failures: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        match = _TOP_LEVEL_FAIL_RE.match(lines[index])
        index += 1
        if match is None:
            continue
        message: list[str] = []
        while index < len(lines) and lines[index][:1] in (" ", "\t"):
            message.append(lines[index].strip())
            index += 1
        failures.append((match.group("name"), message))
    return failures


def parse_go_test_output(text: str) -> tuple[list[_Segment], list[str]]:
    """把 ``go test`` 的多包输出切成“每包一段”，返回 (各包段落, 最后一个包结果行之后的残余行)。

    ``go test`` 对多个包按包缓冲输出，一个包的全部输出连续出现、以 ``ok／FAIL／?  <包>`` 结果行收尾，
    所以结果行之前累积的行就是该包的输出。
    """

    segments: list[_Segment] = []
    buffer: list[str] = []
    for line in text.splitlines():
        match = _PACKAGE_RESULT_RE.match(line)
        # “FAIL”单独成行是汇总行；带包名的才是包结果行。
        if match is not None and match.group("package") not in ("", "FAIL"):
            segment = _Segment(match.group("package"), match.group("verdict"), match.group("rest").strip(), buffer)
            segment.failures = _split_failures(buffer)
            segments.append(segment)
            buffer = []
        else:
            buffer.append(line)
    return segments, buffer


def _relative_package(package: str) -> str | None:
    if package.startswith(MODULE_PREFIX):
        return package[len(MODULE_PREFIX) :]
    return None


def _frozen_evidence(message: list[str], package: str, source_root: Path | None) -> bool:
    """失败用例是冻结台账比对的证据：失败信息带摘要字样，或其测试源文件读取 docs/egress 台账。"""

    if FROZEN_MESSAGE_RE.search(" ".join(message)):
        return True
    if source_root is None or not message:
        return False
    match = _FAIL_SOURCE_RE.match(message[0])
    if match is None:
        return False
    try:
        text = (source_root / package / match.group("file")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return FROZEN_SOURCE_MARKER in text


def classify_go_test_output(text: str, *, source_root: Path | None = None) -> Attribution:
    """对一段 go test 输出做保守归因：能证明全部失败都是冻结摘要比对才记 frozen_digest_pending_ledger。

    ``source_root`` 是 ``backend`` 目录；给出时，失败信息里没有摘要字样的用例可以凭“测试源文件读取 docs/egress”取得证据。
    """

    if not text.strip():
        return _functional("go test 输出为空，无法归因")
    segments, leftover = parse_go_test_output(text)
    failing = [segment for segment in segments if segment.verdict == "FAIL"]
    if not failing:
        return _functional("没有从 go test 输出里解析出失败的包（输出不完整或不是 go test 的格式）")
    if _CRASH_MARKER_RE.search("\n".join(leftover)):
        return _functional("go test 输出的包结果行之外出现 panic、信号、超时或编译错误迹象")
    failed_tests: list[str] = []
    packages: list[str] = []
    for segment in failing:
        relative = _relative_package(segment.package)
        packages.append(relative or segment.package)
        if "[build failed]" in segment.rest or "[setup failed]" in segment.rest:
            return _functional(f"包 {segment.package} 编译或装配失败", packages=packages)
        if _CRASH_MARKER_RE.search("\n".join(segment.lines)):
            return _functional(f"包 {segment.package} 的输出里有 panic、信号、超时或编译错误迹象", packages=packages)
        if relative not in FROZEN_TEST_PACKAGES:
            return _functional(
                f"包 {segment.package} 不在冻结测试所在的包 {list(FROZEN_TEST_PACKAGES)} 里", packages=packages
            )
        if not segment.failures:
            return _functional(f"包 {segment.package} 失败，但没有解析出顶层失败用例", packages=packages)
        for name, message in segment.failures:
            if not FROZEN_TEST_NAME_RE.search(name):
                return _functional(f"失败用例 {name} 的名字不像冻结测试", failed_tests=failed_tests + [name], packages=packages)
            if not _frozen_evidence(message, relative, source_root):
                return _functional(
                    f"失败用例 {name} 既没有摘要比对的失败信息、测试源文件也不读取 docs/egress 台账：{' '.join(message)[:120] or '（无信息）'}",
                    failed_tests=failed_tests + [name],
                    packages=packages,
                )
            failed_tests.append(name)
    return Attribution(
        CLASS_FROZEN_PENDING,
        f"{len(failed_tests)} 个失败用例全部是冻结摘要比对（待台账闭合）：包 {', '.join(packages)}",
        failed_tests=failed_tests,
        packages=packages,
    )


def classify_egress_spec_summary(summary_path: Path, *, source_root: Path | None = None) -> Attribution:
    """出站规格线（统一调度执行器汇总 ``summary.json``）的归因：只认 ``egress-spec-go-test`` 一个单元。"""

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        units = summary["units"]
        failed = [unit for unit in units if not unit["passed"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _functional(f"统一调度执行器汇总不可读：{summary_path}（{error}）")
    if not failed:
        return _functional("步骤失败，但执行器汇总里没有失败单元（执行器自身或 make 在启动单元之前失败）")
    if summary.get("units_not_run"):
        return _functional(f"执行器汇总里有未执行的单元：{summary['units_not_run'][:5]}，检查没有完整跑完")
    names: list[str] = []
    for unit in failed:
        unit_id = str(unit.get("unit_id", ""))
        name = unit_id[len(EGRESS_SPEC_UNIT_PREFIX) :] if unit_id.startswith(EGRESS_SPEC_UNIT_PREFIX) else unit_id
        names.append(name)
    others = [name for name in names if name not in EGRESS_SPEC_FROZEN_UNITS]
    if others:
        return _functional(f"失败单元里有不属于冻结摘要类白名单 {list(EGRESS_SPEC_FROZEN_UNITS)} 的：{others}", packages=names)
    combined: list[str] = []
    for unit in failed:
        if unit.get("signal") or unit.get("timed_out"):
            return _functional(f"单元 {unit.get('unit_id')} 被信号终止或超时", packages=names)
        try:
            combined.append(Path(unit["log"]).read_text(encoding="utf-8", errors="replace"))
        except (OSError, KeyError, TypeError) as error:
            return _functional(f"单元 {unit.get('unit_id')} 的日志不可读（{error}）", packages=names)
    result = classify_go_test_output("\n".join(combined), source_root=source_root)
    result.packages = result.packages or names
    return result
