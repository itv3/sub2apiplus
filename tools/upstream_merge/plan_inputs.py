"""计划目录 ``inputs/`` 的公共约定（UM-9）。

标准布局下每个 Plan 的私有目录是 ``<计划目录>/{inputs,evidence,worktree}``（见
``request_template_v2.json`` 的 ``{plan_root}``）。人工决策稿与签名输入放在 ``inputs/``，不可变阶段
制品放在 ``evidence/``。UM-9 的重放与轮次命令替人生成机械字段，把草稿与签名输入写到 ``inputs/``，
文件名沿用 v0.2.10 合并实际使用的命名，便于人工对照。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import ensure_private_directory, pretty_bytes, write_once
from .contracts import LoadedPlan
from .errors import UpstreamMergeError

# 命令停在需要人工补全的输入上：已完成的步骤保留，补完后按提示续跑；CLI 以退出码 4 返回。
AWAITING_MANUAL_INPUT = "awaiting_manual_input"


def plan_inputs_root(plan: LoadedPlan) -> Path:
    """返回与 evidence/ 并列的 inputs/（0700）；非标准布局时拒绝猜测位置。"""

    if plan.evidence_root.name != "evidence":
        raise UpstreamMergeError(
            f"evidence root 不在标准布局 <计划目录>/evidence 下，无法确定 inputs 目录：{plan.evidence_root}"
        )
    return ensure_private_directory(plan.evidence_root.parent / "inputs", create=True)


def write_json_input(path: Path, document: Any, label: str) -> str:
    """写入工具生成的输入文件；同内容已存在时复用（便于中断后重跑），内容不同一律拒绝覆盖。

    返回 ``written`` 或 ``reused``。
    """

    raw = pretty_bytes(document)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise UpstreamMergeError(f"{label} 不是可信普通文件：{path}")
        if path.read_bytes() == raw:
            return "reused"
        raise UpstreamMergeError(
            f"{label} 已存在且内容不同，禁止覆盖：{path}；确认旧文件作废后改名留档再重跑"
        )
    write_once(path, raw)
    return "written"
