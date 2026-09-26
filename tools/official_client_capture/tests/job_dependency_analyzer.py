"""R16：逐作业工具依赖的完整性分析（测试与清单生成共用，不进受管工具身份）。

匹配规则与运行时旧算法（``codex_upgrade._job_tool_dependency_files`` 的引用扫描）一致：
完整相对路径、唯一文件名、``tools.official_client_capture.`` 限定模块名，加 Python AST import。
区别只在于扫描前先剔除注释：shell 注释（引号外、位于词首的 ``#`` 到行尾）与 Python 的注释、
文档字符串都不算依赖；shell heredoc 里交给 Python 执行的代码按 Python 规则剔除。场景清单里
``tool_dependencies`` 的声明必须与本分析结果逐项相等（见 test_job_dependency_declarations）。

用法（生成或核对某份场景清单的逐作业依赖）：
  python3 -m tools.official_client_capture.tests.job_dependency_analyzer --scenario <清单> [--compare-legacy]
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade

TOOL_ROOT = Path(codex_upgrade.__file__).resolve().parent
# ``#`` 只有在词首才开始注释：行首或紧跟空白、``;``、``&``、``|``、``(``、``)``。
_SHELL_WORD_BOUNDARY = " \t;&|()"
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_python_comments(text: str) -> str:
    """去掉 Python 注释与文档字符串；无法解析的片段只去整行注释。"""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "".join(
            "\n" if line.lstrip().startswith("#") else line
            for line in text.splitlines(keepends=True)
        )
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_lines.update(
                    range(first.lineno, (first.end_lineno or first.lineno) + 1)
                )
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start[0] in docstring_lines:
            continue
        kept.append(token)
    return tokenize.untokenize(kept)


def _strip_shell_line(line: str, quote: str | None) -> tuple[str, str | None]:
    """剔除单行的 shell 注释，返回剩余文本与行末仍未闭合的引号。"""

    result: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            result.append(char)
            if quote == '"' and char == "\\" and index + 1 < len(line):
                result.append(line[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(line):
            result.extend((char, line[index + 1]))
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1] in _SHELL_WORD_BOUNDARY):
            if line.endswith("\n"):
                result.append("\n")
            break
        result.append(char)
        index += 1
    return "".join(result), quote


def strip_shell_comments(text: str) -> str:
    """去掉 shell 注释；heredoc 正文不参与引号状态，交给 Python 的按 Python 规则剔除。"""

    output: list[str] = []
    quote: str | None = None
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        stripped, quote = _strip_shell_line(lines[index], quote)
        output.append(stripped)
        index += 1
        marker = _HEREDOC_RE.search(stripped) if quote is None else None
        if marker is None:
            continue
        delimiter = marker.group(2)
        allow_tabs = stripped[marker.start() : marker.start() + 3] == "<<-"
        body: list[str] = []
        while index < len(lines):
            candidate = lines[index].rstrip("\n")
            if (candidate.lstrip("\t") if allow_tabs else candidate) == delimiter:
                break
            body.append(lines[index])
            index += 1
        prefix = stripped[: marker.start()]
        body_text = "".join(body)
        if "python" in prefix or delimiter.upper().startswith("PY"):
            body_text = strip_python_comments(body_text)
        output.append(body_text)
        if index < len(lines):
            output.append(lines[index])
            index += 1
    return "".join(output)


class _ReferenceIndex:
    """与运行时旧算法相同的引用匹配：完整路径、唯一文件名、限定模块名。

    旧算法对每个 token 单独跑一次带边界的正则（约 900 次／文件）。这里把全部 token 合成一个
    零宽前瞻交替正则一次扫完，长 token 在前；同一起点只取一个 token，因此若某个 token 是另一个
    token 在非单词字符处截断的前缀（两者可能在同一位置同时成立），这些 token 退回逐个匹配，
    保证与逐 token 语义逐项相等。
    """

    def __init__(self, index: Mapping[str, str]) -> None:
        self.index = dict(index)
        self.basenames: dict[str, list[str]] = {}
        self.modules: dict[str, str] = {}
        for path in self.index:
            self.basenames.setdefault(PurePosixPath(path).name, []).append(path)
            if path.endswith(".py"):
                module = path[:-3].replace("/", ".")
                self.modules[module] = path
                self.modules[f"tools.official_client_capture.{module}"] = path
        tokens: dict[str, str] = {}

        def register(token: str, path: str) -> None:
            if tokens.get(token, path) != path:
                raise ValueError(f"引用 token 同时指向两个文件：{token}")
            tokens[token] = path

        for path in self.index:
            register(path, path)
        for basename, paths in self.basenames.items():
            if len(paths) == 1:
                register(basename, paths[0])
        for module, path in self.modules.items():
            if "." in module:
                register(module, path)
        self.tokens = tokens
        ordered = sorted(tokens, key=len, reverse=True)
        overlapping = {
            shorter
            for shorter in ordered
            for longer in ordered
            if len(longer) > len(shorter)
            and longer.startswith(shorter)
            and not re.match(r"[A-Za-z0-9_]", longer[len(shorter)])
        }
        self._slow_tokens = sorted(overlapping)
        fast = [token for token in ordered if token not in overlapping]
        self._pattern = (
            re.compile(
                r"(?=(?<![A-Za-z0-9_])("
                + "|".join(re.escape(token) for token in fast)
                + r")(?![A-Za-z0-9_]))"
            )
            if fast
            else None
        )

    @staticmethod
    def _contains(text: str, token: str) -> bool:
        return (
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text
            )
            is not None
        )

    def references(self, text: str) -> set[str]:
        found: set[str] = set()
        if self._pattern is not None:
            found.update(self.tokens[match.group(1)] for match in self._pattern.finditer(text))
        found.update(
            self.tokens[token] for token in self._slow_tokens if self._contains(text, token)
        )
        return found

    def references_slow(self, text: str) -> set[str]:
        """逐 token 的旧语义，只供等价性核对。"""

        found = {path for path in self.index if self._contains(text, path)}
        for basename, paths in self.basenames.items():
            if len(paths) == 1 and self._contains(text, basename):
                found.add(paths[0])
        for module, path in self.modules.items():
            if "." in module and self._contains(text, module):
                found.add(path)
        return found


class DependencyAnalyzer:
    """逐作业依赖分析；同一文件的剔除注释、引用与 import 结果只算一次，供整份清单共用。"""

    def __init__(self, index: Mapping[str, str], tool_root: Path = TOOL_ROOT) -> None:
        self.references = _ReferenceIndex(index)
        self.tool_root = tool_root
        self._edges: dict[str, frozenset[str]] = {}

    def file_edges(self, path: str) -> frozenset[str]:
        cached = self._edges.get(path)
        if cached is not None:
            return cached
        edges: set[str] = set()
        source = self.tool_root / path
        if not source.is_symlink() and source.is_file() and source.suffix in {".py", ".sh"}:
            content = source.read_text(encoding="utf-8")
            cleaned = (
                strip_python_comments(content)
                if source.suffix == ".py"
                else strip_shell_comments(content)
            )
            edges.update(self.references.references(cleaned))
            if source.suffix == ".py":
                for node in ast.walk(ast.parse(content, filename=path)):
                    names: list[str] = []
                    if isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    elif isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    for name in names:
                        matched = self.references.modules.get(name)
                        if matched is not None:
                            edges.add(matched)
        result = frozenset(edges)
        self._edges[path] = result
        return result

    def analyze_steps(self, steps: Iterable[Mapping[str, Any]]) -> list[str]:
        """按剔除注释后的引用与 import 计算一个作业的真实工具依赖（排序去重）。"""

        seed = json.dumps(
            [
                {
                    "argv": step.get("argv", []),
                    "environment": step.get("environment", {}),
                }
                for step in steps
                if isinstance(step, Mapping)
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        pending = list(self.references.references(seed))
        dependencies: set[str] = set()
        while pending:
            path = pending.pop()
            if path in dependencies:
                continue
            dependencies.add(path)
            pending.extend(self.file_edges(path) - dependencies)
        return sorted(dependencies)


def analyze_steps(
    steps: Iterable[Mapping[str, Any]],
    index: Mapping[str, str],
    tool_root: Path = TOOL_ROOT,
) -> list[str]:
    """单个作业的便捷入口；整份清单请复用同一个 DependencyAnalyzer。"""

    return DependencyAnalyzer(index, tool_root).analyze_steps(steps)


def current_tool_index() -> dict[str, str]:
    """当前受管工具树的逐文件摘要（与部署、身份计算同一口径）。"""

    return codex_upgrade._tool_entry_digest_map(
        codex_upgrade._tool_identity(include_git=False)
    )


def analyze_manifest(path: Path, index: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """对场景清单的全部作业计算真实依赖；键为作业 id。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    analyzer = DependencyAnalyzer(dict(index) if index is not None else current_tool_index())
    return {
        str(job["id"]): analyzer.analyze_steps(job.get("steps", []))
        for job in payload.get("capture_jobs", [])
        if isinstance(job, Mapping)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="同时给出运行时旧算法（含注释引用）的依赖数与多出的文件",
    )
    arguments = parser.parse_args(argv)
    identity = codex_upgrade._tool_identity(include_git=False)
    index = codex_upgrade._tool_entry_digest_map(identity)
    analyzed = analyze_manifest(arguments.scenario, index)
    if not arguments.compare_legacy:
        print(json.dumps(analyzed, ensure_ascii=False, indent=2))
        return 0
    payload = json.loads(arguments.scenario.read_text(encoding="utf-8"))
    report = {}
    for job in payload.get("capture_jobs", []):
        legacy_job = codex_upgrade.Job(
            job_id=str(job["id"]),
            phase=str(job["phase"]),
            suites=tuple(job.get("suites", [])),
            description=str(job.get("description", "")),
            steps=tuple(
                {"argv": step.get("argv", []), "environment": step.get("environment", {})}
                for step in job.get("steps", [])
            ),
            evidence_roots=tuple(job.get("evidence_roots", [])),
            covers=tuple(job.get("covers", [])),
        )
        legacy = set(codex_upgrade._job_tool_dependency_files(legacy_job, identity))
        current = set(analyzed[str(job["id"])])
        report[str(job["id"])] = {
            "legacy": len(legacy),
            "analyzed": len(current),
            "legacy_only": sorted(legacy - current),
            "analyzed_only": sorted(current - legacy),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
