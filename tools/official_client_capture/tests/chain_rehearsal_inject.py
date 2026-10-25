"""第 13 项：历史缺陷重放用的“反向补丁”注入器（测试夹具，不进受管身份）。

做法：每个历史缺陷对应一份**修复补丁**（修复提交里非测试文件的 ``git diff``，存在
``tests/fixtures/chain_rehearsal/``）。重放时把它**反向**应用到受管树的副本上——也就是把“修复后”的代码块换回
“修复前”的代码块，缺陷就重新出现了。测试文件不动：它们是修复时新增的回归用例，缺陷回来时必须失败。

为什么不用 ``git apply -R`` 或 ``patch -R``：ARM64 上的受管树副本没有 git 仓库，也不一定装了 patch；纯 Python
实现只依赖补丁文本本身，行为在两台机器上一致。

匹配规则（宁可报错，不能悄悄改错地方）：
* 每个补丁块把“新侧”（上下文＋新增行）逐行精确匹配到文件当前内容，再换成“旧侧”（上下文＋删除行）；
* 找不到 → 上下文已漂移（受管文件在修复之后又被改过），抛 ``InjectionError``，调用方把这个缺陷标“未执行”，
  而不是随便找个相近位置硬改；
* 找到多处 → 取离补丁块标注行号最近的一处；
* 同一文件的多个补丁块按文件内顺序依次应用，后一块的搜索起点按前面块造成的行数变化修正。

只支持修改既有文件的补丁（修复提交里新增／删除文件的部分不进补丁）。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Sequence

FIXTURE_DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "chain_rehearsal"
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class InjectionError(RuntimeError):
    """补丁文本非法，或补丁上下文在目标文件里找不到（受管文件在修复之后又变了）。"""


@dataclasses.dataclass(frozen=True)
class ReversePatch:
    """重新引入一个历史缺陷：把 ``patch`` 指向的修复补丁反向应用到树副本。

    ``patch``：``fixtures/chain_rehearsal/`` 下的补丁文件名；``commit``：修复提交（只用来追溯，不参与应用）。
    """

    patch: str
    commit: str = ""


@dataclasses.dataclass(frozen=True)
class ExactReplacement:
    """精确替换：把 ``path`` 文件里**恰好出现一次**的 ``old`` 换成 ``new``。

    给修复之后又被改写过、整份补丁已经无法反向应用的缺陷用：只钉住缺陷最小的那一处，例如把某个放行分支的条件改成
    恒假。``old`` 出现零次或多次都当“上下文漂移”报错。
    """

    path: str
    old: str
    new: str
    note: str = ""


@dataclasses.dataclass
class _Hunk:
    old_start: int
    new_start: int
    old_lines: list[str]
    new_lines: list[str]


@dataclasses.dataclass
class _FilePatch:
    path: str
    hunks: list[_Hunk]


def parse_patch(text: str) -> list[_FilePatch]:
    """解析统一格式的补丁文本；只认修改既有文件（``--- a/x`` ``+++ b/x``）。"""

    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    lines = text.split("\n")
    # 结尾换行产生的空串不是补丁内容。
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith("diff --git "):
            current, hunk = None, None
            continue
        if line.startswith("--- "):
            if line.strip() == "--- /dev/null":
                raise InjectionError("补丁包含新增文件，注入器只支持修改既有文件")
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                raise InjectionError("补丁包含删除文件，注入器只支持修改既有文件")
            if target.startswith("b/"):
                target = target[2:]
            current = _FilePatch(target, [])
            files.append(current)
            hunk = None
            continue
        match = _HUNK_HEADER.match(line)
        if match:
            if current is None:
                raise InjectionError("补丁块出现在文件头之前")
            hunk = _Hunk(int(match.group(1)), int(match.group(3)), [], [])
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue  # index、mode、提交说明等头部信息
        if line.startswith("\\"):
            continue  # “\ No newline at end of file”
        tag, body = line[:1], line[1:]
        if tag == " ":
            hunk.old_lines.append(body)
            hunk.new_lines.append(body)
        elif tag == "-":
            hunk.old_lines.append(body)
        elif tag == "+":
            hunk.new_lines.append(body)
        elif line == "":
            # 上下文里的空行在某些工具输出里会丢掉前导空格。
            hunk.old_lines.append("")
            hunk.new_lines.append("")
        else:
            raise InjectionError(f"补丁行格式非法：{line[:60]!r}")
    if not files:
        raise InjectionError("补丁里没有任何文件")
    for item in files:
        if not item.hunks:
            raise InjectionError(f"{item.path}：补丁没有任何块")
    return files


def _find_block(lines: Sequence[str], block: Sequence[str], hint: int) -> int:
    """在 ``lines`` 里找 ``block`` 的起点（精确逐行匹配）；多处匹配取离 ``hint`` 最近的；找不到返回 -1。"""

    size = len(block)
    if size == 0:
        return -1
    first = block[0]
    candidates = [index for index in range(0, len(lines) - size + 1)
                  if lines[index] == first and list(lines[index:index + size]) == list(block)]
    if not candidates:
        return -1
    return min(candidates, key=lambda index: abs(index - hint))


def apply_reverse(tree_root: Path, patch_text: str) -> list[str]:
    """把补丁反向应用到 ``tree_root``（仓库根副本）；返回被改动的相对路径。任何一块匹配不上就整体报错、不留半成品。"""

    root = Path(tree_root)
    staged: dict[Path, str] = {}
    for file_patch in parse_patch(patch_text):
        path = root / file_patch.path
        if not path.is_file():
            raise InjectionError(f"目标文件不存在：{file_patch.path}")
        source = path.read_text(encoding="utf-8")
        trailing_newline = source.endswith("\n")
        lines = source.split("\n")
        if trailing_newline:
            lines.pop()
        shift = 0
        for hunk in file_patch.hunks:
            if hunk.new_lines == hunk.old_lines:
                continue
            index = _find_block(lines, hunk.new_lines, hunk.new_start - 1 + shift)
            if index < 0:
                raise InjectionError(f"{file_patch.path}：第 {hunk.new_start} 行附近的修复后代码块在文件里找不到（上下文已漂移）")
            lines[index:index + len(hunk.new_lines)] = hunk.old_lines
            shift += len(hunk.old_lines) - len(hunk.new_lines)
        staged[path] = "\n".join(lines) + ("\n" if trailing_newline else "")
    for path, content in staged.items():
        path.write_text(content, encoding="utf-8")
    return sorted(str(path.relative_to(root)) for path in staged)


def load_patch(name: str) -> str:
    path = FIXTURE_DIRECTORY / name
    if not path.is_file():
        raise InjectionError(f"修复补丁夹具不存在：{name}")
    return path.read_text(encoding="utf-8")


def apply_replacement(tree_root: Path, replacement: ExactReplacement) -> list[str]:
    """精确替换：``old`` 必须在目标文件里恰好出现一次。"""

    path = Path(tree_root) / replacement.path
    if not path.is_file():
        raise InjectionError(f"目标文件不存在：{replacement.path}")
    source = path.read_text(encoding="utf-8")
    count = source.count(replacement.old)
    if count != 1:
        raise InjectionError(f"{replacement.path}：待替换的代码块出现 {count} 次（要求恰好一次，上下文已漂移）")
    path.write_text(source.replace(replacement.old, replacement.new), encoding="utf-8")
    return [replacement.path]


def inject(tree_root: Path, injection: ReversePatch | ExactReplacement) -> list[str]:
    """引擎的注入入口：把一个历史缺陷重新引入树副本。"""

    if isinstance(injection, ReversePatch):
        return apply_reverse(tree_root, load_patch(injection.patch))
    if isinstance(injection, ExactReplacement):
        return apply_replacement(tree_root, injection)
    raise InjectionError(f"不认识的注入器：{injection!r}")
