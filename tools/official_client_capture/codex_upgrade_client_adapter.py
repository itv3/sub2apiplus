"""Codex 客户端适配器：Codex 专属的适配器成员取值（Claude 升级控制面设计 D4、B1-01）。

Codex 的编排器服务面就是按包路径导入的 ``codex_upgrade`` 模块本身，原样导出、不包装；本模块只承载监督器、对账器
等共享模块原先写死的 Codex 名字与小函数，取值与改造前逐字相同。其余成员直接绑定到原模块的原符号（见
``upgrade_client_adapter.ADAPTER_BINDINGS["codex"]``）。共享代码经 ``adapter_for("codex")`` 访问，不直接导入本模块。
"""

from __future__ import annotations

from typing import Mapping

# 监督器 ``_campaign_run_manifest`` 对编排器 CLI 预声明动作拒绝的基础子命令（原写死在该函数里的 15 项）。
# 按清单版本追加 ``recover-vc1-interruption``、对监督器 CLI 追加的 7 项与客户端无关，仍留在监督器。
FORBIDDEN_SUBCOMMANDS = frozenset({
    "successor",
    "recover-candidate-failed-jobs",
    "control-epoch",
    "evaluation-transition",
    "terminal-transition-preflight",
    "plan",
    "reuse-official-evidence",
    "harden-evidence-permissions",
    "reconcile-supervisor-run",
    "reconcile-attempt",
    "compile-vc-batch",
    "compile-vc-interrupted-recovery-batch",
    "compile-vc-interrupted-recovery-continuation",
    "continue-vc1-interruption",
    "finalize-vc1-deadline-orphan",
})
# 断言包取证清单（``<attempt>/evidence/assertion-bundle/capture-manifest.json``）里记录目标版本的字段名；
# Campaign 一侧的字段始终叫 ``target_version``。
TARGET_VERSION_FIELD = "codex_version"


def reject_volatile_dimensions(dimensions: Mapping[str, str]) -> None:
    """Codex 的易变形态已全部由根因模块的六类模式覆盖，没有附加检查。"""

    del dimensions
