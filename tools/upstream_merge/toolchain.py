"""工具链版本的单一来源与核对：期望值取该树自己的声明，实际值取本机真正会用到的工具。

v0.2.17 合并时，本机 Go 与 golangci-lint 被升级成与主干声明不一致的版本，基线误用了候选的版本，
CI 里 lint 步骤的版本又散落在三处字面量里，上游改一处就会有一处漏改。本模块把这类问题前移到开工检查：

* ``backend-ci.yml`` 的 golangci-lint 版本只在 ``GOLANGCI_LINT_VERSION`` 里定义一次，默认、unit、integration
  三个 lint 步骤都引用它（``parse_ci_workflow``／``workflow_findings`` 用文本规则解析，不依赖 PyYAML）；
* CI 里 ``Verify Go version`` 校验的 Go 版本必须与 ``backend/go.mod`` 的 ``go``／``toolchain`` 指令一致；
* ``check_toolchain`` 读取一棵树的期望值，再在该树 ``backend/`` 下取 ``go env GOVERSION`` 与 lint 二进制的
  版本，输出期望值、实际值和不匹配项（Go 版本、lint 版本不等，或 lint 的编译 Go 主次版本低于期望 Go，都阻断）。

基线取主干声明的版本、候选取合并后声明的版本：对不同的树各调一次即可，本模块不记忆任何全局状态。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .errors import UpstreamMergeError

# backend-ci.yml 的仓库相对路径；U-4 的 CI 证据与开工检查都以它为准（workflow.py 再导出同名常量）。
CI_WORKFLOW_PATH = ".github/workflows/backend-ci.yml"
GO_MOD_PATH = "backend/go.mod"
TOOLCHAIN_SCHEMA = "official-egress-upstream-toolchain-check/v1"

# CI 里 golangci-lint 版本的唯一定义点，以及步骤引用它的写法。
LINT_VERSION_VARIABLE = "GOLANGCI_LINT_VERSION"
LINT_VERSION_REFERENCE = "${{ env." + LINT_VERSION_VARIABLE + " }}"
LINT_ACTION = "golangci/golangci-lint-action"
# 三个 lint 步骤的 --build-tags 取值：空串代表默认标签；与 gate_runner 的 BUILD_TAGS 同一组。
REQUIRED_LINT_TAGS = ("", "integration", "unit")
# 本机 lint 二进制的显式指定（绝对路径）；未设置时取 PATH 里的 golangci-lint。gate_runner 的 lint 步骤也读它。
LINT_BINARY_ENV = "UPSTREAM_GATE_GOLANGCI_LINT"

# 探测命令的返回：(退出码, stdout, stderr)；测试注入假实现，生产用 run_probe。
Probe = Callable[[Sequence[str], Path], "tuple[int, str, str]"]

_JOBS_KEY_RE = re.compile(r"^jobs:\s*(?:#.*)?$")
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_KEY_VALUE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:\s+(?P<value>.*))?$")
_GO_GREP_RE = re.compile(r"grep\s+(?:-[A-Za-z]+\s+)*['\"]?(go[0-9]+\.[0-9]+(?:\.[0-9]+)?)['\"]?")
_BUILD_TAGS_RE = re.compile(r"--build-tags(?:=|\s+)(?P<value>['\"]?[^\s'\"]*['\"]?)")
_LINT_VERSION_VALUE_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_LINT_OUTPUT_RE = re.compile(r"has version\s+(?P<version>\S+)\s+built with\s+(?P<go>go\S+)")
_GO_VERSION_RE = re.compile(r"^go([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")


@dataclass(frozen=True)
class WorkflowStep:
    """工作流里的一个步骤：只保留核对用得到的字段。"""

    job: str
    line: int
    name: str | None
    uses: str | None
    run: str | None
    inputs: Mapping[str, str]
    env: Mapping[str, str]


@dataclass
class WorkflowDocument:
    """按文本规则解析出的工作流：顶层 env、每个作业的 env 与步骤。"""

    workflow_env: dict[str, str] = field(default_factory=dict)
    job_env: dict[str, dict[str, str]] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)
    # 所有名为 GOLANGCI_LINT_VERSION 的定义点（任意层级的 env 块）：(作用域, 值, 行号)。
    lint_definitions: list[tuple[str, str, int]] = field(default_factory=list)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(value: str | None) -> str:
    """取 YAML 标量的字面值：去行尾注释与成对引号。"""

    if value is None:
        return ""
    text = value.strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        if end != -1:
            return text[1:end]
        return text[1:]
    comment = text.find(" #")
    if comment != -1:
        text = text[:comment]
    return text.strip()


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _parse_mapping(lines: Sequence[tuple[int, str]], indent: int) -> dict[str, str]:
    """解析缩进恰为 indent 的 ``key: value`` 映射；更深缩进的行（块标量、嵌套）忽略。"""

    result: dict[str, str] = {}
    for _number, line in lines:
        if _is_noise(line) or _indent(line) != indent:
            continue
        match = _KEY_VALUE_RE.match(line.strip())
        if match is not None:
            result[match.group("key")] = _scalar(match.group("value"))
    return result


def _block_after(lines: Sequence[tuple[int, str]], start: int, key_indent: int) -> list[tuple[int, str]]:
    """取 lines[start] 之后所有缩进大于 key_indent 的行（空行与注释行并入块内）。"""

    block: list[tuple[int, str]] = []
    for number, line in lines[start + 1 :]:
        if not _is_noise(line) and _indent(line) <= key_indent:
            break
        block.append((number, line))
    return block


def _parse_step(job: str, first_number: int, raw: Sequence[tuple[int, str]]) -> WorkflowStep:
    """解析一个步骤。首行形如 ``      - key: value``：其后的键缩进比破折号多 2 格。"""

    first_number_value, first_line = raw[0]
    dash_indent = _indent(first_line)
    key_indent = dash_indent + 2
    # 把破折号换成空格，使首行与后续键处于同一缩进，统一按映射解析。
    lines: list[tuple[int, str]] = [(first_number_value, " " * key_indent + first_line.strip()[2:])]
    lines.extend(raw[1:])
    name: str | None = None
    uses: str | None = None
    run: str | None = None
    inputs: dict[str, str] = {}
    env: dict[str, str] = {}
    for index, (_number, line) in enumerate(lines):
        if _is_noise(line) or _indent(line) != key_indent:
            continue
        match = _KEY_VALUE_RE.match(line.strip())
        if match is None:
            continue
        key, value = match.group("key"), match.group("value")
        if key == "name":
            name = _scalar(value)
        elif key == "uses":
            uses = _scalar(value)
        elif key == "run":
            text = (value or "").strip()
            if text[:1] in ("|", ">"):
                body = [item for _n, item in _block_after(lines, index, key_indent)]
                run = textwrap.dedent("\n".join(body)).strip()
            else:
                run = _scalar(value)
        elif key in ("with", "env"):
            nested = _block_after(lines, index, key_indent)
            parsed = _parse_mapping(nested, key_indent + 2)
            (inputs if key == "with" else env).update(parsed)
    return WorkflowStep(
        job=job,
        line=first_number,
        name=name,
        uses=uses,
        run=run,
        inputs=inputs,
        env=env,
    )


def parse_ci_workflow(text: str) -> WorkflowDocument:
    """按缩进结构解析 backend-ci.yml 一类的 GitHub Actions 工作流（不依赖 PyYAML）。

    只覆盖核对需要的结构：顶层 ``env``、``jobs`` 下每个作业的 ``env`` 与 ``steps``（步骤的
    ``name``／``uses``／``run``／``with``／``env``）。结构看不懂的部分一律当作不存在，由调用方的
    ``workflow_findings`` 报缺失，不会静默放行。
    """

    document = WorkflowDocument()
    numbered = list(enumerate(text.splitlines(), start=1))

    # 顶层 env 与 jobs 块
    top_env: list[tuple[int, str]] = []
    jobs_lines: list[tuple[int, str]] = []
    section: str | None = None
    for number, line in numbered:
        if line and not line.startswith((" ", "#")):
            match = _TOP_KEY_RE.match(line)
            section = match.group(1) if match else None
            continue
        if section == "env":
            top_env.append((number, line))
        elif section == "jobs":
            jobs_lines.append((number, line))
    document.workflow_env = _parse_mapping(top_env, 2)
    for number, line in top_env:
        match = re.match(rf"^\s+{LINT_VERSION_VARIABLE}:\s*(.*)$", line)
        if match:
            document.lint_definitions.append(("workflow", _scalar(match.group(1)), number))

    # 作业块
    blocks: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for number, line in jobs_lines:
        match = _JOB_KEY_RE.match(line)
        if match:
            current = match.group(1)
            if current in blocks:
                raise UpstreamMergeError(f"工作流作业重复定义：{current}")
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append((number, line))

    for job, block in blocks.items():
        job_env: dict[str, str] = {}
        index = 0
        while index < len(block):
            number, line = block[index]
            if _is_noise(line) or _indent(line) != 4:
                index += 1
                continue
            match = _KEY_VALUE_RE.match(line.strip())
            if match is None:
                index += 1
                continue
            key = match.group("key")
            nested = _block_after(block, index, 4)
            if key == "env":
                job_env.update(_parse_mapping(nested, 6))
                for env_number, env_line in nested:
                    definition = re.match(rf"^\s+{LINT_VERSION_VARIABLE}:\s*(.*)$", env_line)
                    if definition:
                        document.lint_definitions.append((f"job:{job}", _scalar(definition.group(1)), env_number))
            elif key == "steps":
                steps: list[list[tuple[int, str]]] = []
                for step_number, step_line in nested:
                    if re.match(r"^      - ", step_line):
                        steps.append([(step_number, step_line)])
                    elif steps:
                        steps[-1].append((step_number, step_line))
                for raw in steps:
                    step = _parse_step(job, raw[0][0], raw)
                    document.steps.append(step)
                    for env_key, env_value in step.env.items():
                        if env_key == LINT_VERSION_VARIABLE:
                            document.lint_definitions.append((f"step:{job}", env_value, step.line))
            index += 1 + len(nested)
        document.job_env[job] = job_env
    return document


def _step_label(step: WorkflowStep) -> str:
    return f"{step.job} 作业的步骤 {step.name or '(未命名)'}（第 {step.line} 行）"


def workflow_findings(text: str) -> tuple[list[str], dict[str, Any]]:
    """核对 backend-ci.yml 的 lint 版本单一来源与 Go 校验版本；返回 (违规清单, 解析出的事实)。

    违规清单为空才算通过。事实用于报告和后续与 ``go.mod`` 的比对：``lint_version``（唯一定义的值）、
    ``lint_steps``（每个步骤的标签与版本写法）、``go_versions``（CI 校验的 Go 版本）。
    """

    findings: list[str] = []
    document = parse_ci_workflow(text)
    lint_steps = [step for step in document.steps if step.uses and step.uses.startswith(f"{LINT_ACTION}@")]
    facts: dict[str, Any] = {
        "lint_version": None,
        "lint_version_source": None,
        "lint_steps": [],
        "go_versions": [],
    }

    # 1. GOLANGCI_LINT_VERSION 恰好定义一次
    definitions = document.lint_definitions
    if len(definitions) != 1:
        findings.append(
            f"{LINT_VERSION_VARIABLE} 必须在 {CI_WORKFLOW_PATH} 恰好定义一次，实际 {len(definitions)} 次"
        )
    definition_scope = None
    if len(definitions) == 1:
        definition_scope, value, line = definitions[0]
        if not _LINT_VERSION_VALUE_RE.fullmatch(value):
            findings.append(f"{LINT_VERSION_VARIABLE} 的值必须形如 v2.14.0，实际 {value!r}（第 {line} 行）")
        else:
            facts["lint_version"] = value

    # 2. 全部 lint 步骤引用它，且能看见这处定义；标签集合恰为默认／unit／integration
    if not lint_steps:
        findings.append(f"{CI_WORKFLOW_PATH} 没有 {LINT_ACTION} 步骤")
    tags: list[str] = []
    for step in lint_steps:
        written = step.inputs.get("version")
        if written is None or "".join(written.split()) != "".join(LINT_VERSION_REFERENCE.split()):
            findings.append(
                f"{_step_label(step)}的 version 必须写 {LINT_VERSION_REFERENCE}，实际 {written!r}（字面版本残留或缺失）"
            )
        if definition_scope is not None and definition_scope not in ("workflow", f"job:{step.job}"):
            findings.append(
                f"{_step_label(step)}看不到 {LINT_VERSION_VARIABLE} 的定义（定义在 {definition_scope}）；"
                "应写在该作业的 env 或工作流顶层 env"
            )
        match = _BUILD_TAGS_RE.search(step.inputs.get("args", ""))
        tag = _scalar(match.group("value")) if match else ""
        tags.append(tag)
        facts["lint_steps"].append(
            {"job": step.job, "name": step.name, "line": step.line, "version": written, "build_tags": tag}
        )
    # 旧写法的树（lint 步骤各写字面版本、没有单一来源）仍要给出期望值，开工检查才能照常比对本机 lint；
    # 违规本身已记入上面的 findings，这里只在字面版本唯一时把它当作期望值。
    literals = sorted(
        {
            _scalar(item)
            for item in (step.inputs.get("version") for step in lint_steps)
            if item and _LINT_VERSION_VALUE_RE.fullmatch(_scalar(item))
        }
    )
    facts["lint_version_source"] = "variable" if facts["lint_version"] else None
    if facts["lint_version"] is None and len(literals) == 1:
        facts["lint_version"] = literals[0]
        facts["lint_version_source"] = "literal"
    if lint_steps and sorted(tags) != list(REQUIRED_LINT_TAGS):
        findings.append(
            "lint 步骤的 --build-tags 必须恰好覆盖 默认／unit／integration 三种标签各一次，"
            f"实际 {sorted(tags)!r}（空串代表默认标签）"
        )

    # 3. Verify Go version 步骤校验的 Go 版本
    for step in document.steps:
        if step.run is None:
            continue
        match = _GO_GREP_RE.search(step.run)
        if match is not None and "go version" in step.run:
            facts["go_versions"].append({"job": step.job, "line": step.line, "version": match.group(1)})
    versions = sorted({item["version"] for item in facts["go_versions"]})
    if not versions:
        findings.append(f"{CI_WORKFLOW_PATH} 没有 Verify Go version 校验（go version | grep -q 'goX.Y.Z'）")
    elif len(versions) > 1:
        findings.append(f"{CI_WORKFLOW_PATH} 里的 Go 校验版本不一致：{versions}")
    return findings, facts


def parse_go_mod(text: str) -> dict[str, str | None]:
    """取 go.mod 的 ``go`` 与 ``toolchain`` 指令；缺失记 None。"""

    result: dict[str, str | None] = {"go": None, "toolchain": None}
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        match = re.match(r"^(go|toolchain)\s+(\S+)$", line)
        if match and result[match.group(1)] is None:
            result[match.group(1)] = match.group(2)
    return result


def _version_tuple(version: str) -> tuple[int, int, int | None] | None:
    match = _GO_VERSION_RE.match(version)
    if match is None:
        return None
    patch = match.group(3)
    return int(match.group(1)), int(match.group(2)), int(patch) if patch is not None else None


def go_versions_agree(expected: str, actual: str) -> bool:
    """期望带补丁号时必须逐位相等；只写到主次版本（go1.27）时比较主次版本。"""

    wanted, got = _version_tuple(expected), _version_tuple(actual)
    if wanted is None or got is None:
        return expected == actual
    if wanted[2] is None:
        return wanted[:2] == got[:2]
    return wanted == got


def declared_toolchain(root: Path) -> dict[str, Any]:
    """读取一棵树自己声明的期望值，并核对声明本身是否自洽（lint 单一来源、CI 与 go.mod 的 Go 版本一致）。"""

    findings: list[str] = []
    go_mod_path = root / GO_MOD_PATH
    workflow_path = root / CI_WORKFLOW_PATH
    go_mod: dict[str, str | None] = {"go": None, "toolchain": None}
    facts: dict[str, Any] = {"lint_version": None, "lint_version_source": None, "lint_steps": [], "go_versions": []}
    if go_mod_path.is_file():
        go_mod = parse_go_mod(go_mod_path.read_text(encoding="utf-8"))
    else:
        findings.append(f"缺少 {GO_MOD_PATH}，无法确定期望的 Go 版本")
    if workflow_path.is_file():
        workflow_found, facts = workflow_findings(workflow_path.read_text(encoding="utf-8"))
        findings.extend(workflow_found)
    else:
        findings.append(f"缺少 {CI_WORKFLOW_PATH}，无法确定期望的 lint 版本")

    # setup-go 的 go-version-file 在有 toolchain 指令时取 toolchain，否则取 go 指令
    directive = go_mod["toolchain"] or (f"go{go_mod['go']}" if go_mod["go"] else None)
    if go_mod["toolchain"] and not str(go_mod["toolchain"]).startswith("go"):
        findings.append(f"{GO_MOD_PATH} 的 toolchain 指令格式非法：{go_mod['toolchain']}")
    expected_go = directive
    ci_versions = sorted({item["version"] for item in facts["go_versions"]})
    if expected_go and ci_versions:
        for version in ci_versions:
            if not go_versions_agree(expected_go, version):
                findings.append(
                    f"CI Verify Go version 校验 {version}，与 {GO_MOD_PATH} 声明的 {expected_go} 不一致"
                )
    return {
        "go": {
            "version": expected_go,
            "source": GO_MOD_PATH,
            "go_directive": go_mod["go"],
            "toolchain_directive": go_mod["toolchain"],
            "ci_verify_versions": ci_versions,
        },
        "golangci_lint": {
            "version": facts["lint_version"],
            "source": (
                f"{CI_WORKFLOW_PATH} 的 {LINT_VERSION_VARIABLE}"
                if facts["lint_version_source"] != "literal"
                else f"{CI_WORKFLOW_PATH} 的 lint 步骤字面版本（未采用单一来源）"
            ),
            "build_tags": sorted(item["build_tags"] for item in facts["lint_steps"]),
        },
        "declaration_findings": findings,
    }


def run_probe(argv: Sequence[str], cwd: Path, timeout: int = 900) -> tuple[int, str, str]:
    """执行一条只读探测命令：禁用 shell，沿用当前环境（含 GOTOOLCHAIN）；任何启动失败都折成非零退出码。"""

    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return 127, "", f"命令不存在：{error}"
    except subprocess.TimeoutExpired:
        return 124, "", f"探测命令超时（{timeout} 秒）"
    except OSError as error:
        return 126, "", f"无法执行：{error}"
    return completed.returncode, completed.stdout, completed.stderr


def resolve_lint_binary(environ: Mapping[str, str] | None = None) -> str | None:
    """lint 二进制：显式环境变量优先（须为绝对路径），否则 PATH 里的 golangci-lint。"""

    environ = os.environ if environ is None else environ
    explicit = environ.get(LINT_BINARY_ENV, "").strip()
    if explicit:
        return explicit
    return shutil.which("golangci-lint", path=environ.get("PATH"))


def _normalize_lint_version(version: str | None) -> str | None:
    if version is None:
        return None
    return version[1:] if version.startswith("v") else version


def probe_actual(root: Path, probe: Probe, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """在该树 ``backend/`` 下取本机真正会用到的 Go 与 lint 版本。"""

    environ = os.environ if environ is None else environ
    backend = root / "backend"
    cwd = backend if backend.is_dir() else root
    code, stdout, stderr = probe(["go", "env", "GOVERSION"], cwd)
    go_actual: dict[str, Any] = {
        "version": stdout.strip() if code == 0 and stdout.strip() else None,
        "command": "go env GOVERSION",
        "cwd": str(cwd),
        "gotoolchain": environ.get("GOTOOLCHAIN"),
        "error": None if code == 0 else (stderr.strip() or f"退出码 {code}")[-500:],
    }
    lint_path = resolve_lint_binary(environ)
    lint_actual: dict[str, Any] = {
        "path": lint_path,
        "version": None,
        "built_with_go": None,
        "raw": None,
        "error": None,
    }
    if lint_path is None:
        lint_actual["error"] = f"找不到 golangci-lint（PATH 中没有，且未设置 {LINT_BINARY_ENV}）"
    elif not os.path.isabs(lint_path):
        lint_actual["error"] = f"{LINT_BINARY_ENV} 必须是绝对路径：{lint_path}"
    else:
        code, stdout, stderr = probe([lint_path, "--version"], cwd)
        text = (stdout or stderr).strip()
        lint_actual["raw"] = text[:300]
        match = _LINT_OUTPUT_RE.search(text) if code == 0 else None
        if match is None:
            lint_actual["error"] = f"无法从 --version 输出解析版本（退出码 {code}）：{text[:200]}"
        else:
            lint_actual["version"] = match.group("version")
            lint_actual["built_with_go"] = match.group("go")
    return {"go": go_actual, "golangci_lint": lint_actual}


def compare_toolchain(expected: dict[str, Any], actual: dict[str, Any], role: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """逐项比对期望与实际，返回 (不匹配项, 修正提示)。"""

    mismatches: list[dict[str, Any]] = []
    hints: list[str] = []
    label = {"baseline": "基线（主干声明）", "candidate": "候选（合并后声明）"}.get(role or "", "该树")

    for finding in expected["declaration_findings"]:
        mismatches.append(
            {"item": "declaration", "expected": "声明自洽", "actual": finding, "message": f"{label}的工具链声明有问题：{finding}"}
        )

    expected_go = expected["go"]["version"]
    actual_go = actual["go"]["version"]
    if expected_go is None:
        pass  # 已在 declaration_findings 里报缺失
    elif actual_go is None:
        mismatches.append(
            {
                "item": "go.version",
                "expected": expected_go,
                "actual": None,
                "message": f"无法取得本机 Go 版本：{actual['go']['error']}",
            }
        )
    elif not go_versions_agree(expected_go, actual_go):
        mismatches.append(
            {
                "item": "go.version",
                "expected": expected_go,
                "actual": actual_go,
                "message": f"{label}期望 {expected_go}，本机 backend/ 下实际是 {actual_go}",
            }
        )
        hints.append(
            f"让该树使用 {expected_go}：在命令前设置 GOTOOLCHAIN={expected_go}（不要升级或降级本机全局 Go，"
            "否则会影响另一棵树）；基线必须用主干声明的版本，不得沿用候选的版本"
        )

    expected_lint = expected["golangci_lint"]["version"]
    actual_lint = actual["golangci_lint"]
    if expected_lint is not None:
        if actual_lint["version"] is None:
            mismatches.append(
                {
                    "item": "golangci_lint.version",
                    "expected": expected_lint,
                    "actual": None,
                    "message": f"无法取得本机 golangci-lint 版本：{actual_lint['error']}",
                }
            )
            hints.append(f"准备 golangci-lint {expected_lint}，并用 {LINT_BINARY_ENV}=<绝对路径> 指向它")
        elif _normalize_lint_version(actual_lint["version"]) != _normalize_lint_version(expected_lint):
            mismatches.append(
                {
                    "item": "golangci_lint.version",
                    "expected": expected_lint,
                    "actual": actual_lint["version"],
                    "message": (
                        f"{label}期望 golangci-lint {expected_lint}，本机 {actual_lint['path']} 实际是 {actual_lint['version']}"
                    ),
                }
            )
            hints.append(
                f"需要 golangci-lint {expected_lint}：另装一份到独立目录，并用 {LINT_BINARY_ENV}=<该二进制绝对路径> 指向它，"
                "不要覆盖本机全局安装；该二进制须用不低于期望 Go 主次版本的工具链编译"
            )
        built_with = actual_lint["built_with_go"]
        wanted_tuple = _version_tuple(expected_go) if expected_go else None
        built_tuple = _version_tuple(built_with) if built_with else None
        if wanted_tuple is not None and built_with is not None:
            if built_tuple is None:
                mismatches.append(
                    {
                        "item": "golangci_lint.built_with_go",
                        "expected": f">= go{wanted_tuple[0]}.{wanted_tuple[1]}",
                        "actual": built_with,
                        "message": f"无法解析 lint 的编译 Go 版本：{built_with}",
                    }
                )
            elif built_tuple[:2] < wanted_tuple[:2]:
                mismatches.append(
                    {
                        "item": "golangci_lint.built_with_go",
                        "expected": f">= go{wanted_tuple[0]}.{wanted_tuple[1]}",
                        "actual": built_with,
                        "message": (
                            f"lint 由 {built_with} 编译，主次版本低于期望的 {expected_go}，读不了新版 Go 的导出数据"
                        ),
                    }
                )
                hints.append(f"用 {expected_go} 重新编译 lint（例如 GOTOOLCHAIN={expected_go} go install 后指定 GOBIN），并用 {LINT_BINARY_ENV} 指向")
    return mismatches, hints


def check_toolchain(
    root: Path,
    role: str | None = None,
    *,
    probe: Probe = run_probe,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """核对一棵树的工具链：输出期望值、实际值和不匹配项；``result`` 为 ``passed`` 或 ``blocked``。"""

    if role not in (None, "baseline", "candidate"):
        raise UpstreamMergeError(f"role 非法：{role}，允许 baseline／candidate")
    if not root.is_dir():
        raise UpstreamMergeError(f"仓库目录不存在：{root}")
    expected = declared_toolchain(root)
    actual = probe_actual(root, probe, environ)
    mismatches, hints = compare_toolchain(expected, actual, role)
    return {
        "schema_version": TOOLCHAIN_SCHEMA,
        "role": role,
        "repository": str(root),
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
        "hints": list(dict.fromkeys(hints)),
        "result": "blocked" if mismatches else "passed",
    }


def describe_mismatches(report: dict[str, Any]) -> list[str]:
    """把不匹配项写成一行行的阻断原因（含修正提示），供启动前检查的错误信息使用。"""

    lines = [item["message"] for item in report["mismatches"]]
    lines.extend(f"修正提示：{hint}" for hint in report["hints"])
    return lines
