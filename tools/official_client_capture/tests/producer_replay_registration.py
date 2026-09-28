"""生成器只读重放身份登记门禁（只读实现，供 ``test_producer_replay_registration_gate`` 与人工排查共用）。

一、为什么需要这道门禁
======================
部分收据生成器把"自身文件的 sha256"写进收据，作为生成器（producer）身份。已经生成的历史收据
在新工具部署后仍会被状态、恢复、对账等命令读取；此时收据里记录的是旧文件摘要，只有生成器
自己的"只读重放登记"承认这个旧摘要，历史收据才能继续被重放。

2026-09-28 修好接着跑第 36 项的事故：提交 4cf336fbf 修改了
``codex_upgrade_arm64_environment_receipt.py``，却没有把修改前的摘要 9e10bd0f… 登记为版本 8 的
只读重放身份。本机八项门禁与 CI 全部通过，部署到 ARM64 后状态命令才报"ARM64 事实采集器身份
漂移"——现场 0.157 194249z 的 P0 与 10 份 attempt 环境收据都是旧摘要生成的。补丁 f9727c765
才补上登记。本门禁把这类遗漏前移到提交阶段：只要生成器在某个部署边界之后被改动，而边界处
的旧摘要既不等于当前摘要、也不被生成器自己的只读重放逻辑接受，就失败，并写明要把哪个摘要
登记到哪里。

二、盘点结论：哪些生成器以"文件摘要即身份"且需要按旧身份重放
============================================================
下面五个生成器同时满足"把自身文件摘要写进收据"与"历史收据须跨工具版本按旧身份重放"，
并且各自带有旧身份登记机制（``PRODUCERS``；第 4、5 个分别由修好接着跑第 40、42 项补上登记机制后
纳入）：

1. ``codex_upgrade_arm64_environment_receipt.py``（ARM64 环境收据，P0／attempt 等 12 个 phase）：
   写入 ``producer.tool_sha256``；重放时 ``_validated_producer_version`` 要求等于当前摘要，否则必须
   在 ``REGISTERED_REPLAY_PRODUCER_HASHES[version]`` 中——**按 PRODUCER_VERSION 分组的显式摘要集合**，
   旧摘要必须登记在它生成收据时的版本号下。
2. ``codex_upgrade_receipt_finalizer.py``（restoration／observed-profile／kilo-binding／scenario 收据）：
   写入 ``producer.tool.sha256``；重放时不等于当前摘要就必须在 ``LEGACY_REPLAY_PRODUCER_HASHES``
   中——**不分版本的扁平摘要集合**。
3. ``codex_upgrade_timing_ledger.py``（Campaign 计时账本）：写入 ``producer.tool_sha256``；重放时
   ``_producer_identity_matches`` 沿 ``PRODUCER_SUCCESSOR_TRANSITIONS``、
   ``codex-cli-0151-worktree-successor.json`` 与 ``PRODUCER_FREEZE_SUCCESSORS`` 登记的承接收据
   摘要边，从旧摘要逐跳走到当前摘要——**承接收据链**，登记方式是把本次变更集的 freeze successor
   描述追加进 ``PRODUCER_FREEZE_SUCCESSORS`` 并生成该收据。
4. ``codex_upgrade_gate_receipt.py``（候选外部门禁与 post-promotion 门禁收据，accept 与生产激活时
   重放）：写入 ``producer.tool_sha256``；重放时 ``_replay_producer_identity`` 对 v4 收据要求摘要等于
   当前摘要，否则必须在 ``REGISTERED_REPLAY_PRODUCER_HASHES[PRODUCER_SCHEMA]`` 中——**按 producer
   schema 分组的显式摘要集合**；v3 历史收据原样承接旧身份，v1／v2 收据格式已退役。
5. ``production_activation_receipt.py``（生产激活收据，VC-6 的 production-activation 与
   rollback-verification 两步先后重放同一份收据，重放时还会连带重放 post-promotion 门禁收据）：写入
   ``producer.tool_sha256``；重放时 ``_replay_producer_identity`` 对 v2 收据要求摘要等于当前摘要，
   否则必须在 ``REGISTERED_REPLAY_PRODUCER_HASHES[PRODUCER_SCHEMA]`` 中——同样**按 producer schema
   分组**；v1 收据格式已退役。

覆盖自检有两道（见第五节）：任何模块新增名字含 ``REPLAY_PRODUCER``／``PRODUCER_*SUCCESSOR`` 的
登记常量，都必须先纳入 ``PRODUCERS``；任何模块只要"计算自身文件摘要、且重放／校验路径上会重新计算
它"（即要求摘要等于当前），就必须纳入 ``PRODUCERS``，或在 ``STRICT_SELF_DIGEST_WITHOUT_REGISTRY``
写明不跨工具版本重放的依据。其余只写不校验的生成器（Claude 台账钉值、运行时授权链等）不在范围内。

三、"部署边界"的定义
====================
部署边界取自仓库自己的冻结承接图，与 ``docs/egress/maintenance/freeze-registry.json`` 中
``policy.generic_graph`` 的抽边口径一致：

* 收据范围：``docs/egress/maintenance/*.json``（只看顶层，不进子目录）中 ``schema_version`` 含
  ``successor``／``transition``／``ledger``／``receipt`` 的收据；
* (A) 显式节点：收据中 ``path`` 等于该生成器、且 ``reason`` 非空的对象里登记的前序摘要
  （``predecessor_sha256s``／``predecessor_sha256``／``from_sha256``／``before.sha256``）与后继摘要
  （``to_sha256``／``current_sha256``／``head_sha256``／``after.sha256``）——覆盖 0.151 时期从工作区
  快照登记、不在 git 历史里的版本；
* (B) 变更集前后提交：每份收据记录的 ``base_commit``／``current_commit`` 处该生成器文件的实际摘要
  （只读读取 git 对象库，不联网）——覆盖承接收据没有为该生成器写显式边的变更集（例如收据终结器）。

同一变更集内部的中间提交既不是收据记录的前后提交，也不是显式节点，因此**不会被要求登记**；
承接收据引用、但仓库里已不存在的提交（0.151 时期的两份工作区基线）只跳过 (B)，其显式节点仍计入。
冻结承接图把新增文件的前序记为空内容摘要（``EMPTY_FILE_SHA256``），它表示"文件尚不存在"，不是
生成器版本，不计为边界。生成器在某个历史形态里还没有只读重放判定时（``judge_attributes`` 中的
函数或登记常量缺失），门禁在该形态上跳过它并记入报告；当前工作树由测试保证不得跳过。

四、检查规则与历史豁免
======================
对每个生成器，当前摘要记为 C；每个部署边界摘要 B ≠ C 时，B 必须被该生成器**自身的只读重放
逻辑**接受（直接调用生成器模块里的判定函数或登记常量，不另写一套判定）。不接受即违规。

门禁引入前已经人工处理过的历史边界——确认从未生成需重放收据，或其收据格式早已按设计退役——
登记在 ``HISTORICAL_EXEMPTIONS``（逐条写明依据）。豁免只允许覆盖 ``GATE_BASELINE_ISSUED_AT_UTC`` 之前签发的收据引入的边界，且
永远不覆盖基线时已部署的版本 ``GATE_BASELINE_CURRENT_SHA256``；此后出现的新边界不提供豁免
通道——即便某个变更集终点确实没部署过，把它登记为只读重放身份也无害（只读重放不允许生成
新事实）。

五、覆盖自检：静态发现"要求摘要严格等于当前"的生成器（第 42 项）
============================================================
第 36、40、42 项暴露的是同一类遗漏：生成器把自身文件摘要写进收据，重放时又用当前文件摘要重建
收据逐字比较，却没有旧版本登记。``find_self_digest_producers`` 静态分析
``tools/official_client_capture``（不含 tests／versions）：函数里对 ``Path(__file__)``（可带 resolve）
或其本地别名求摘要，即为"计算自身摘要"；函数名含 replay／verify／validate／matches／reconstruct
或以 check 开头的单词（不含 checkpoint），且能在模块内调用图上到达计算自身摘要的函数，即为"校验
路径要求等于当前"。
这类模块必须满足以下二者之一，否则覆盖自检失败：

* 纳入 ``PRODUCERS``（有只读重放登记）；
* 列入 ``STRICT_SELF_DIGEST_WITHOUT_REGISTRY``，并逐条写明它的收据为什么不会跨工具版本重放。

这是保守的静态近似：静态调用图看不到运行时分支，会把"重放时放宽生成器身份"的模块也算进来，
这类模块在分类表里说明即可；它也看不到跨模块钉值（例如 Claude 台账钉住别的工具的摘要），
这类情形不属于本门禁范围。

本模块只读：不写仓库、不改生成器、不生成收据、不访问网络。第 40、42 项起它同时覆盖门禁收据与
生产激活收据生成器（登记机制由同一变更集分别加入两个生成器），本模块自身仍不改变任何生成器。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# 覆盖的生成器与登记方式
# ---------------------------------------------------------------------------

MECHANISM_VERSIONED_HASH_SET = "versioned_hash_set"
MECHANISM_FLAT_HASH_SET = "flat_hash_set"
MECHANISM_SUCCESSOR_CHAIN = "successor_chain"
MECHANISM_SCHEMA_KEYED_HASH_SET = "schema_keyed_hash_set"
MECHANISMS = frozenset(
    {
        MECHANISM_VERSIONED_HASH_SET,
        MECHANISM_FLAT_HASH_SET,
        MECHANISM_SUCCESSOR_CHAIN,
        MECHANISM_SCHEMA_KEYED_HASH_SET,
    }
)

ENVIRONMENT_RECEIPT_PRODUCER = (
    "tools/official_client_capture/codex_upgrade_arm64_environment_receipt.py"
)
RECEIPT_FINALIZER_PRODUCER = "tools/official_client_capture/codex_upgrade_receipt_finalizer.py"
TIMING_LEDGER_PRODUCER = "tools/official_client_capture/codex_upgrade_timing_ledger.py"
GATE_RECEIPT_PRODUCER = "tools/official_client_capture/codex_upgrade_gate_receipt.py"
PRODUCTION_ACTIVATION_PRODUCER = "tools/official_client_capture/production_activation_receipt.py"


@dataclass(frozen=True)
class ProducerSpec:
    """一个受本门禁覆盖的生成器。

    * ``path``：仓库相对路径（POSIX）；
    * ``mechanism``：旧身份登记方式（见模块文档第二节）；
    * ``registry``：登记所在的模块级常量名，只用于失败信息与覆盖自检；
    * ``judge_attributes``：门禁判定要调用的生成器函数与常量。历史形态里缺任何一个，说明那时
      还没有这套只读重放判定，门禁在该形态上跳过该生成器（当前工作树由测试保证不会跳过）。
    """

    path: str
    mechanism: str
    registry: str
    judge_attributes: tuple[str, ...] = ()


PRODUCERS: tuple[ProducerSpec, ...] = (
    ProducerSpec(
        path=ENVIRONMENT_RECEIPT_PRODUCER,
        mechanism=MECHANISM_VERSIONED_HASH_SET,
        registry="REGISTERED_REPLAY_PRODUCER_HASHES",
        judge_attributes=("REGISTERED_REPLAY_PRODUCER_HASHES", "_validated_producer_version"),
    ),
    ProducerSpec(
        path=RECEIPT_FINALIZER_PRODUCER,
        mechanism=MECHANISM_FLAT_HASH_SET,
        registry="LEGACY_REPLAY_PRODUCER_HASHES",
        judge_attributes=("LEGACY_REPLAY_PRODUCER_HASHES",),
    ),
    ProducerSpec(
        path=TIMING_LEDGER_PRODUCER,
        mechanism=MECHANISM_SUCCESSOR_CHAIN,
        registry="PRODUCER_FREEZE_SUCCESSORS",
        # 计时账本早于 PRODUCER_FREEZE_SUCCESSORS 就有承接判定（0.151 的 PRODUCER_SUCCESSOR_TRANSITIONS），
        # 历史形态以判定函数是否存在为准，不以当前的登记常量为准。
        judge_attributes=("_producer", "_producer_identity_matches"),
    ),
    ProducerSpec(
        path=GATE_RECEIPT_PRODUCER,
        mechanism=MECHANISM_SCHEMA_KEYED_HASH_SET,
        registry="REGISTERED_REPLAY_PRODUCER_HASHES",
        judge_attributes=("REGISTERED_REPLAY_PRODUCER_HASHES", "_replay_producer_identity"),
    ),
    ProducerSpec(
        path=PRODUCTION_ACTIVATION_PRODUCER,
        mechanism=MECHANISM_SCHEMA_KEYED_HASH_SET,
        registry="REGISTERED_REPLAY_PRODUCER_HASHES",
        judge_attributes=("REGISTERED_REPLAY_PRODUCER_HASHES", "_replay_producer_identity"),
    ),
)

# 覆盖自检用的登记常量命名规则：模块级常量名命中即视为"带只读重放登记机制的生成器"，
# 必须出现在 PRODUCERS 中。命中示例：REGISTERED_REPLAY_PRODUCER_HASHES、
# LEGACY_REPLAY_PRODUCER_HASHES、PRODUCER_FREEZE_SUCCESSORS、PRODUCER_SUCCESSOR_TRANSITIONS。
REGISTRY_CONSTANT_PATTERN = re.compile(
    r"REPLAY_PRODUCER|PRODUCER_[A-Z0-9_]*SUCCESSOR|PRODUCER[A-Z0-9_]*REPLAY"
)


# ---------------------------------------------------------------------------
# 历史豁免（门禁引入前一次性审计；此后新边界不得豁免）
# ---------------------------------------------------------------------------

# 门禁基线：引入本门禁时仓库中最新一份承接收据的签发时间。
GATE_BASELINE_RECEIPT = "upstream-codex-0157-item37-b4-1-successor-matrix-20260928-freeze-successor.json"
GATE_BASELINE_ISSUED_AT_UTC = "2026-09-28T02:13:00Z"
# 基线收据 current_commit 处各生成器的摘要，即门禁引入时已部署的版本。它们是基线之后第一次修改
# 时"必须登记的上一部署边界"，虽然也出现在基线前的收据里，但永远不得豁免——否则改生成器时把
# 当前摘要塞进豁免表就能绕过门禁。
GATE_BASELINE_CURRENT_SHA256: Mapping[str, str] = {
    ENVIRONMENT_RECEIPT_PRODUCER: "1b62b096cc543350d0060c0eea5f97e2f833e47a95b22c346fdffaa27fe66968",
    RECEIPT_FINALIZER_PRODUCER: "02bc3d7e4d7b8df11a0c8ce3289dd270275d748c67cc19d83e27b00c3c3a1e78",
    TIMING_LEDGER_PRODUCER: "bb799f9e817bb3e30e41c1792ee1c4fe61fc51e96685abadc78605e0218ff564",
    # 第 40 项修改前的门禁收据生成器；第 40 项本身改了该文件，因此它必须登记为只读重放身份。
    GATE_RECEIPT_PRODUCER: "034331e58aa96dad7b8368c231fd2ead38a826ffce18c4d76daf4b16b8e14020",
    # 第 42 项修改前的生产激活收据生成器；第 42 项本身改了该文件，因此它必须登记为只读重放身份。
    PRODUCTION_ACTIVATION_PRODUCER: "3b4ddbf874a9164654e8496fb6f2a88fbd50b22557f16b5285f0aa0aca7d1292",
}

HISTORICAL_EXEMPTIONS: Mapping[str, Mapping[str, str]] = {
    ENVIRONMENT_RECEIPT_PRODUCER: {
        # 来源：codex-cli-0151-arm64-environment-producer-coordinate-decoupling-source-transition.json
        # 的 base_commit 77e338de5。
        "a55967eeb5ea8c55c301779595f1aa132df81755a588f5d88070b1fb903dc1d7": (
            "0.151 时期提交 77e338de5 的中间版本，15 分钟后即被 e90e15b9a（317ea2c8，已登记）取代；"
            "同一份承接收据显式登记的前序是工作区快照 a62a269e（已登记）。2026-09-28 已在 ARM64 现场"
            "核实：没有任何收据由它生成，它只出现在承接收据备份副本中"
        ),
        # 来源：upstream-codex-01561-r15-20260923-freeze-successor.json 的 to_sha256／current_commit
        # e30ed6f82，以及 r15-review-fix 收据的 predecessor_sha256s。
        "532bbe60a63b3b4c36b56ca1b0c0d593b58b1aa412c674be68ac5f31d9cdc583": (
            "R15 首版（0a888dadc），次日即被审核修正 5d218b931（9e10bd0f，已登记）取代。2026-09-28 已在"
            " ARM64 现场核实：没有任何收据由它生成，它只出现在承接收据备份副本中"
        ),
        # 来源：upstream-codex-0157-item36-maintenance-wait-invalid-transient-20260928-freeze-successor.json
        # 的 to_sha256／current_commit 4cf336fbf，以及 item36b 收据的 predecessor_sha256s。
        "48304c4c7c8028b7ac613d862bff88ec7abf8d98fa21cc87fe6d46eb337a015a": (
            "第 36 项事故版本（4cf336fbf）：部署到 ARM64 后状态命令即报 P0 环境收据无法重放，补登记"
            "提交 f9727c765 只登记了 9e10bd0f。2026-09-28 已在 ARM64 现场核实：没有任何收据由它生成，"
            "它只出现在承接收据备份副本中。本门禁正是为在提交阶段拦下这一形态而设"
        ),
    },
    GATE_RECEIPT_PRODUCER: {
        # 来源：historical-source-drift-successor.json 等收据的前序与前后提交（7d0d6c98f 起的版本）。
        "c60b3c4992b5a6081e678eb6c3b23e308ca6ea82e8be395e66a9705c01375abb": (
            "v1 门禁收据格式（codex-upgrade-external-gate-producer/v1）已随 v3 升级（d691afcb6）退役："
            "replay 只接受 v3／v4 收据，该版本生成的收据按设计不再重放，对应升级均已收口"
        ),
        # 来源：codex-cli-0151-tool-readiness-source-transition.json 的 from_sha256 等（bd638c411 版本）。
        "861d07d5d6574c0e953df930907bfc8d5a2a64f34072b2f204992362f22327d8": (
            "v2 门禁收据格式（codex-upgrade-external-gate-producer/v2）已随 v3 升级（d691afcb6）退役："
            "replay 只接受 v3／v4 收据，该版本生成的收据按设计不再重放，对应升级均已收口"
        ),
        # 来源：upstream-codex-01561-r15-20260923-freeze-successor.json 的 to_sha256／current_commit
        # e30ed6f82，以及 r15-review-fix 收据的 predecessor_sha256s。
        "72c1020ec44b43b9efa305a70b3a1d4853cccee9375e0329cde7469abc691dc1": (
            "R15 首版（0a888dadc），次日即被 5d218b931（034331e5，已登记）取代。门禁收据必须绑定同一"
            " attempt 的 gate_before／gate_after 环境收据，而同一版本的环境收据生成器 532bbe60 已于"
            " 2026-09-28 在 ARM64 现场核实没有生成任何收据，因此该版本不可能生成过门禁收据"
        ),
    },
    PRODUCTION_ACTIVATION_PRODUCER: {
        # 来源：upstream-merge-framework-v2-source-transition.json 的前序等（51b81c577 版本）。
        "09fdd6afeaa003ca4f35a7b5281392780ed60bd37e9fad732527c9adc4976202": (
            "v1 生产激活收据格式（codex-production-activation-producer/v1）已退役：replay 只接受 v2 收据。"
            "该版本生成的 0.145→0.147 K83 收据（CODEX_CLI_0145_TO_0147_K83_PRODUCTION_ACTIVATION_RECEIPT.json）"
            "按设计不再重放，对应升级已收口"
        ),
    },
}

# 要求自身摘要等于当前、但有意不设只读重放登记的生成器（第 42 项覆盖自检的分类表）。每条都要写明
# 它的收据为什么不会跨工具版本重放；新增这类生成器时，默认应加登记并纳入 PRODUCERS，而不是写进这里。
STRICT_SELF_DIGEST_WITHOUT_REGISTRY: Mapping[str, str] = {
    "tools/official_client_capture/codex_upgrade_campaign_run_rehearsal_receipt.py": (
        "原子演练收据按设计必须由当前工具树重做：pre-A3 认证（_bind_rehearsal_receipt）与发布认证"
        "都要求其工具身份等于当前树，工具一变就重做演练，不承接旧版本"
    ),
    "tools/official_client_capture/codex_upgrade_job_rehearsal_receipt.py": (
        "静态调用图的保守误报：replay 以 allow_collector_drift=True 调用 validate_facts，承接 facts 中"
        "记录的生成器身份，不要求等于当前摘要"
    ),
    "tools/official_client_capture/codex_upgrade_official_asset_receipt.py": (
        "官方 Release 下载前生成并立即离线重放，生成与重放在同一工具版本内完成；仓库内没有跨工具"
        "版本重放它的调用方"
    ),
}


# ---------------------------------------------------------------------------
# 冻结承接图口径（与 freeze-registry.json policy.generic_graph 一致）
# ---------------------------------------------------------------------------

MAINTENANCE_RELATIVE = "docs/egress/maintenance"
RECEIPT_SCHEMA_KEYWORDS = ("successor", "transition", "ledger", "receipt")
PREDECESSOR_FIELDS = ("predecessor_sha256s", "predecessor_sha256", "from_sha256", "before")
SUCCESSOR_FIELDS = ("to_sha256", "current_sha256", "head_sha256", "after")
COMMIT_FIELDS = ("base_commit", "current_commit")
# 冻结承接图用空内容摘要作新增文件的前序，表示"文件尚不存在"，不是生成器版本。
EMPTY_FILE_SHA256 = hashlib.sha256(b"").hexdigest()
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PRODUCER_VERSION_RE = re.compile(rb'^PRODUCER_VERSION\s*=\s*"([^"\n]+)"', re.MULTILINE)
PRODUCER_SCHEMA_RE = re.compile(rb'^PRODUCER_SCHEMA\s*=\s*"([^"\n]+)"', re.MULTILINE)


class GateUnavailable(RuntimeError):
    """当前环境无法判定部署边界（例如不是 git 工作树），调用方可据此跳过。"""


class GateError(RuntimeError):
    """门禁自身无法可靠判定，必须失败关闭（例如浅克隆、承接收据不是合法 JSON）。"""


@dataclass(frozen=True)
class BoundarySource:
    """部署边界的一处出处：哪份承接收据、以什么方式（显式字段或前后提交）指向该摘要。"""

    receipt: str
    kind: str
    commit: str | None = None
    issued_at_utc: str | None = None

    def describe(self) -> str:
        commit = f"@{self.commit[:9]}" if self.commit else ""
        return f"{self.receipt} {self.kind}{commit}"


@dataclass
class Boundary:
    """一个部署边界摘要及其全部出处；能从 git 读到时附带该版本文件内容（用于读取历史版本号）。"""

    sha256: str
    sources: list[BoundarySource] = field(default_factory=list)
    content: bytes | None = None


@dataclass(frozen=True)
class Violation:
    """部署边界处的旧摘要未被生成器只读重放逻辑接受。

    ``registry_key`` 是该摘要应登记的分组键：环境收据为它生成收据时的 ``PRODUCER_VERSION``，
    门禁收据为它生成收据时的 ``PRODUCER_SCHEMA``；扁平集合与承接链只作参考（其 PRODUCER_VERSION）。
    ``registrable`` 为假表示该分组已不受生成器只读重放支持（例如已退役的收据格式），登记无效。
    """

    producer: ProducerSpec
    current_sha256: str
    boundary_sha256: str
    registry_key: str | None
    sources: tuple[BoundarySource, ...]
    detail: str
    registrable: bool = True

    def key_label(self) -> str:
        if self.registry_key is None:
            return ""
        name = "PRODUCER_SCHEMA" if self.producer.mechanism == MECHANISM_SCHEMA_KEYED_HASH_SET else "PRODUCER_VERSION"
        return f"（该版本 {name}=\"{self.registry_key}\"）"

    def remedy(self) -> str:
        """给出要把哪个摘要登记到哪里。"""

        name = Path(self.producer.path).name
        if self.producer.mechanism == MECHANISM_VERSIONED_HASH_SET:
            version = self.registry_key or "<该摘要生成收据时的 PRODUCER_VERSION>"
            return (
                f"把 {self.boundary_sha256} 加入 {name} 的 {self.producer.registry}[\"{version}\"]"
                "（只读重放身份，不允许生成新 facts），并在注释写明该版本生成过哪些收据"
            )
        if self.producer.mechanism == MECHANISM_SCHEMA_KEYED_HASH_SET:
            schema = self.registry_key or "<该摘要生成收据时的 PRODUCER_SCHEMA>"
            if not self.registrable:
                return (
                    f"该摘要生成的是 {schema} 格式的收据，{name} 已不支持该格式的只读重放，登记无效；"
                    "若仍有此格式的收据需要重放，须在生成器中恢复该格式的只读重放"
                )
            return (
                f"把 {self.boundary_sha256} 加入 {name} 的 {self.producer.registry}[\"{schema}\"]"
                "（只读重放身份，新收据仍只由当前生成器生成），并在注释写明该版本生成过哪些收据"
            )
        if self.producer.mechanism == MECHANISM_FLAT_HASH_SET:
            return (
                f"把 {self.boundary_sha256} 加入 {name} 的 {self.producer.registry}"
                "（只读重放身份），并在注释写明该版本生成过哪些收据"
            )
        return (
            f"在 {name} 的 {self.producer.registry} 追加本次变更集的通用 freeze successor 描述"
            "（path／base_commit／scope／result），并用 freeze-successor-generate 生成该收据，使旧摘要 "
            f"{self.boundary_sha256} 能沿登记的承接边逐跳走到当前摘要 {self.current_sha256}"
        )


@dataclass
class GateReport:
    """一次门禁运行的结果。"""

    tree_root: Path
    violations: list[Violation] = field(default_factory=list)
    exempted: list[tuple[ProducerSpec, str, str]] = field(default_factory=list)
    boundaries: dict[str, dict[str, Boundary]] = field(default_factory=dict)
    current: dict[str, str] = field(default_factory=dict)
    unreachable_commits: dict[str, list[str]] = field(default_factory=dict)
    # 该形态下还没有登记常量（登记机制尚未引入）而跳过的生成器及原因；当前工作树必须为空。
    skipped: list[tuple[ProducerSpec, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# 承接收据与部署边界
# ---------------------------------------------------------------------------


def load_receipt_scope(tree_root: Path) -> list[tuple[str, dict[str, Any]]]:
    """按冻结承接图口径读取 docs/egress/maintenance 顶层收据；坏 JSON 失败关闭。"""

    maintenance = Path(tree_root) / MAINTENANCE_RELATIVE
    if not maintenance.is_dir():
        raise GateUnavailable(f"缺少承接收据目录：{maintenance}")
    receipts: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(maintenance.glob("*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_bytes())
        except (UnicodeError, json.JSONDecodeError) as error:
            raise GateError(f"承接收据不是合法 JSON，无法判定部署边界：{path.name}：{error}") from error
        if not isinstance(payload, dict):
            continue
        schema = payload.get("schema_version")
        if not isinstance(schema, str) or not any(word in schema for word in RECEIPT_SCHEMA_KEYWORDS):
            continue
        receipts.append((path.name, payload))
    if not receipts:
        raise GateUnavailable(f"承接收据目录中没有任何冻结承接图收据：{maintenance}")
    return receipts


def _field_digests(node: Mapping[str, Any], name: str) -> list[str]:
    """按冻结承接图字段语义取出摘要：before／after 取 .sha256，predecessor_sha256s 为数组。"""

    value = node.get(name)
    if name in {"before", "after"}:
        value = value.get("sha256") if isinstance(value, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and HEX64_RE.fullmatch(item)]


def _explicit_nodes(payload: Any, producer_path: str) -> list[tuple[str, str]]:
    """递归找出 path 等于生成器、reason 非空的对象，返回 (字段名, 摘要)。"""

    found: list[tuple[str, str]] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            reason = node.get("reason")
            if node.get("path") == producer_path and isinstance(reason, str) and reason.strip():
                for name in (*PREDECESSOR_FIELDS, *SUCCESSOR_FIELDS):
                    found.extend((name, digest) for digest in _field_digests(node, name))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _git(git_root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(git_root), *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise GateUnavailable("找不到 git 可执行文件，无法读取部署边界处的历史版本") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"git {' '.join(args[:2])} 失败：{message}")
    return completed.stdout


def ensure_full_history(git_root: Path) -> None:
    """确认 git_root 是完整历史的 git 仓库：非仓库可跳过，浅克隆必须失败。"""

    try:
        subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--git-dir"],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as error:
        raise GateUnavailable("找不到 git 可执行文件，无法读取部署边界处的历史版本") from error
    except subprocess.CalledProcessError as error:
        raise GateUnavailable(f"不是 git 仓库，无法读取部署边界处的历史版本：{git_root}") from error
    shallow = _git(git_root, "rev-parse", "--is-shallow-repository").decode().strip()
    if shallow == "true":
        raise GateError(
            "git 仓库是浅克隆，承接收据引用的前后提交可能缺失、部署边界无法判定；"
            "CI 须用 fetch-depth: 0 检出，本地须在完整历史的工作树上运行"
        )


def _batch_check(git_root: Path, specs: Sequence[str]) -> list[str | None]:
    """批量查询对象：返回对象 id，缺失时为 None。"""

    if not specs:
        return []
    raw = _git(git_root, "cat-file", "--batch-check", stdin=("\n".join(specs) + "\n").encode())
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(lines) != len(specs):
        raise GateError("git cat-file --batch-check 输出行数与查询不一致")
    result: list[str | None] = []
    for line in lines:
        parts = line.split()
        result.append(None if parts[-1:] == ["missing"] or len(parts) < 3 else parts[0])
    return result


def _read_blobs(git_root: Path, object_ids: Iterable[str]) -> dict[str, bytes]:
    """用一次 git cat-file --batch 读取多个 blob 的原始字节。"""

    ids = sorted(set(object_ids))
    if not ids:
        return {}
    raw = _git(git_root, "cat-file", "--batch", stdin=("\n".join(ids) + "\n").encode())
    contents: dict[str, bytes] = {}
    offset = 0
    for expected in ids:
        header_end = raw.index(b"\n", offset)
        header = raw[offset:header_end].decode("utf-8", "replace").split()
        if len(header) != 3 or header[0] != expected or header[1] != "blob":
            raise GateError(f"git cat-file --batch 输出异常：{header}")
        size = int(header[2])
        start = header_end + 1
        contents[expected] = raw[start : start + size]
        offset = start + size + 1
    return contents


def collect_boundaries(
    receipts: Sequence[tuple[str, Mapping[str, Any]]],
    producer_path: str,
    *,
    git_root: Path,
    commit_exists: Mapping[str, bool],
    unreachable: dict[str, list[str]] | None = None,
) -> dict[str, Boundary]:
    """收集一个生成器的全部部署边界摘要（显式节点 ∪ 变更集前后提交处的实际摘要）。"""

    boundaries: dict[str, Boundary] = {}

    def add(digest: str, source: BoundarySource, content: bytes | None = None) -> None:
        if digest == EMPTY_FILE_SHA256:
            return
        boundary = boundaries.setdefault(digest, Boundary(sha256=digest))
        boundary.sources.append(source)
        if content is not None and boundary.content is None:
            boundary.content = content

    commit_uses: dict[str, list[tuple[str, str, str | None]]] = {}
    for name, payload in receipts:
        issued = payload.get("issued_at_utc") if isinstance(payload.get("issued_at_utc"), str) else None
        for field_name, digest in _explicit_nodes(payload, producer_path):
            add(digest, BoundarySource(name, f"显式 {field_name}", None, issued))
        for field_name in COMMIT_FIELDS:
            commit = payload.get(field_name)
            if isinstance(commit, str) and HEX40_RE.fullmatch(commit):
                commit_uses.setdefault(commit, []).append((name, field_name, issued))

    present = [commit for commit in commit_uses if commit_exists.get(commit)]
    for commit in commit_uses:
        if not commit_exists.get(commit) and unreachable is not None:
            unreachable.setdefault(commit, sorted({use[0] for use in commit_uses[commit]}))
    object_ids = _batch_check(git_root, [f"{commit}:{producer_path}" for commit in present])
    contents = _read_blobs(git_root, [oid for oid in object_ids if oid])
    digest_of = {oid: hashlib.sha256(data).hexdigest() for oid, data in contents.items()}
    for commit, oid in zip(present, object_ids):
        if oid is None:
            # 该提交时生成器文件尚不存在：不是它的部署边界。
            continue
        for name, field_name, issued in commit_uses[commit]:
            add(digest_of[oid], BoundarySource(name, field_name, commit, issued), contents[oid])
    return boundaries


def commit_presence(git_root: Path, receipts: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, bool]:
    """批量判断收据引用的前后提交是否存在于本地对象库。"""

    commits = sorted(
        {
            payload[name]
            for _, payload in receipts
            for name in COMMIT_FIELDS
            if isinstance(payload.get(name), str) and HEX40_RE.fullmatch(payload[name])
        }
    )
    found = _batch_check(git_root, [f"{commit}^{{commit}}" for commit in commits])
    return {commit: oid is not None for commit, oid in zip(commits, found)}


# ---------------------------------------------------------------------------
# 生成器模块与只读重放判定（直接复用生成器自己的判定逻辑）
# ---------------------------------------------------------------------------


def load_producer_module(tree_root: Path, spec: ProducerSpec) -> ModuleType:
    """从 tree_root 加载生成器模块的独立副本。

    模块名挂在 ``tools.official_client_capture`` 包下，使生成器内部
    ``from tools.official_client_capture import …`` 的依赖照常解析；加载后立即从 ``sys.modules``
    移除，副本不会影响其他测试导入的同名模块。
    """

    path = Path(tree_root) / spec.path
    if not path.is_file():
        raise GateError(f"生成器文件不存在：{path}")
    name = f"tools.official_client_capture._replay_gate_probe_{uuid.uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(name, path)
    if module_spec is None or module_spec.loader is None:
        raise GateError(f"无法加载生成器模块：{path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def historical_producer_constants(content: bytes | None) -> tuple[str | None, str | None]:
    """从边界版本的文件内容读出当时的 PRODUCER_SCHEMA 与 PRODUCER_VERSION。"""

    if content is None:
        return None, None
    schema = PRODUCER_SCHEMA_RE.search(content)
    version = PRODUCER_VERSION_RE.search(content)
    return (
        schema.group(1).decode("utf-8") if schema else None,
        version.group(1).decode("utf-8") if version else None,
    )


@dataclass(frozen=True)
class Verdict:
    """一个旧摘要的只读重放判定：是否接受、判定说明、登记分组键、该分组是否仍可登记。"""

    accepted: bool
    detail: str
    registry_key: str | None
    registrable: bool = True


def replay_verdict(
    spec: ProducerSpec,
    module: ModuleType,
    boundary: Boundary,
) -> Verdict:
    """用生成器自身的只读重放逻辑判定旧摘要是否可重放。"""

    schema, version = historical_producer_constants(boundary.content)
    if spec.mechanism == MECHANISM_SCHEMA_KEYED_HASH_SET:
        # 与 replay() 相同的生成器身份承接入口 _replay_producer_identity。历史内容读不到时
        # （只在显式节点出现、不在 git 历史里）只按当前 producer schema 判定，不会因旧格式的
        # 宽松承接（v3）而被放行。
        candidate = schema or module.PRODUCER_SCHEMA
        producer = {
            "schema_version": candidate,
            "tool": str(Path(module.__file__).resolve()),
            "tool_sha256": boundary.sha256,
        }
        # 生成器只对当前 producer schema 查登记；其它旧格式要么整体承接（v3），要么已退役。
        registrable = candidate == module.PRODUCER_SCHEMA
        try:
            module._replay_producer_identity(producer)
        except Exception as error:  # noqa: BLE001 - 生成器以异常表达拒绝
            return Verdict(False, f"producer schema={candidate}：{error}", candidate, registrable)
        return Verdict(True, f"可按 {candidate} 只读重放", candidate)
    if spec.mechanism == MECHANISM_VERSIONED_HASH_SET:
        # 与 _build_receipt(replay_producer=…) → validate_facts 相同的只读重放入口：allow_legacy_replay=True。
        tool = str(Path(module.__file__).resolve())
        registry = getattr(module, spec.registry)
        candidates = [version] if version else sorted({*registry, module.PRODUCER_VERSION})
        reasons: list[str] = []
        for candidate in candidates:
            producer = {
                "schema_version": schema or module.PRODUCER_SCHEMA,
                "tool": tool,
                "tool_sha256": boundary.sha256,
                "version": candidate,
            }
            try:
                accepted = module._validated_producer_version(producer, allow_legacy_replay=True)
            except Exception as error:  # noqa: BLE001 - 生成器以异常表达拒绝
                reasons.append(f"version={candidate}：{error}")
                continue
            if accepted == candidate:
                return Verdict(True, f"已登记为版本 {candidate} 的只读重放身份", version or candidate)
            reasons.append(f"version={candidate}：判定为版本 {accepted}")
        return Verdict(False, "；".join(reasons) or "无可尝试的版本号", version)
    if spec.mechanism == MECHANISM_FLAT_HASH_SET:
        # 与 _validate_replay_producer 相同：不等于当前摘要时必须在扁平登记集合中。
        registry = getattr(module, spec.registry)
        if boundary.sha256 in registry:
            return Verdict(True, f"已登记在 {spec.registry}", version)
        return Verdict(False, f"不在 {spec.registry} 中", version)
    if spec.mechanism == MECHANISM_SUCCESSOR_CHAIN:
        current = module._producer()
        frozen = dict(current)
        frozen["tool_sha256"] = boundary.sha256
        if schema:
            frozen["schema_version"] = schema
        if version:
            frozen["version"] = version
        try:
            accepted = module._producer_identity_matches(frozen, current)
        except Exception as error:  # noqa: BLE001 - 登记描述或承接收据本身不合法
            return Verdict(False, f"承接链加载失败：{type(error).__name__}：{error}", version)
        if accepted:
            return Verdict(True, "沿登记的承接边可达当前摘要", version)
        return Verdict(False, "沿登记的承接边无法到达当前摘要（链断裂或版本不一致）", version)
    raise GateError(f"未知登记方式：{spec.mechanism}")


def _memoize_successor_edges(module: ModuleType) -> None:
    """计时账本判定会为每个旧摘要重读全部登记收据；门禁私有副本上按描述缓存摘要边。

    被缓存的是纯函数（同一仓库根、同一描述 → 同一条边或同一异常），异常不缓存，语义不变；
    只替换本门禁加载的独立模块副本，不影响生产模块。
    """

    for name in ("_load_freeze_successor_edge", "_load_producer_successor_edge"):
        original = getattr(module, name, None)
        if original is None:
            continue
        cache: dict[tuple[str, str], Any] = {}

        def cached(repository_root: Path, descriptor: Mapping[str, str], *, _original=original, _cache=cache):
            key = (str(repository_root), json.dumps(descriptor, sort_keys=True, ensure_ascii=False))
            if key not in _cache:
                _cache[key] = _original(repository_root, descriptor)
            return _cache[key]

        setattr(module, name, cached)


# ---------------------------------------------------------------------------
# 覆盖自检：静态发现"自身文件摘要即身份、且校验路径要求等于当前"的生成器（第 42 项）
# ---------------------------------------------------------------------------

# 校验入口的函数名特征；check 只认作单词开头（排除 checkpoint 之类的名词）。
VERIFICATION_FUNCTION_RE = re.compile(
    r"replay|verify|validate|matches|reconstruct|(?:^|_)check(?!point)", re.IGNORECASE
)
DIGEST_CALL_RE = re.compile(r"sha256|digest", re.IGNORECASE)
TOOL_RELATIVE_ROOT = "tools/official_client_capture"


@dataclass(frozen=True)
class SelfDigestFinding:
    """一个计算自身文件摘要的模块：哪些函数计算它，哪些校验入口会在调用图上到达这些函数。"""

    digest_functions: tuple[str, ...]
    verification_functions: tuple[str, ...]

    @property
    def strict(self) -> bool:
        """校验路径上会重新计算自身摘要，即重放／校验要求摘要等于当前文件。"""

        return bool(self.verification_functions)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_self_path(node: ast.AST, aliases: set[str]) -> bool:
    """``Path(__file__)``、``pathlib.Path(__file__)``、其 ``.resolve(...)``／``.absolute()``，或已知别名。

    ``.with_name``／``.parent`` 等指向别的文件，不算本文件。
    """

    if isinstance(node, ast.Name):
        return node.id in aliases
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {"resolve", "absolute"}:
        return _is_self_path(func.value, aliases)
    is_path_constructor = (isinstance(func, ast.Name) and func.id == "Path") or (
        isinstance(func, ast.Attribute) and func.attr == "Path"
    )
    return (
        is_path_constructor
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__file__"
    )


def _digests_self(node: ast.AST, aliases: set[str]) -> bool:
    """node 是否是对本文件求摘要的调用：摘要函数的参数是本文件路径，或本文件的 ``read_bytes()``。"""

    if not isinstance(node, ast.Call) or not DIGEST_CALL_RE.search(_call_name(node)):
        return False
    for argument in node.args:
        if _is_self_path(argument, aliases):
            return True
        if (
            isinstance(argument, ast.Call)
            and _call_name(argument) == "read_bytes"
            and isinstance(argument.func, ast.Attribute)
            and _is_self_path(argument.func.value, aliases)
        ):
            return True
    return False


def analyze_self_digest_source(source: bytes | str) -> SelfDigestFinding | None:
    """静态分析一个模块的源码；不计算自身摘要时返回 None。"""

    tree = ast.parse(source)
    module_aliases = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and _is_self_path(node.value, set())
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    digest_functions: set[str] = set()
    calls: dict[str, set[str]] = {}
    for name, function in functions.items():
        aliases = set(module_aliases)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and _is_self_path(node.value, aliases):
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if any(_digests_self(node, aliases) for node in ast.walk(function)):
            digest_functions.add(name)
        calls[name] = {
            _call_name(node)
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
    if not digest_functions:
        return None

    def reaches_digest(start: str) -> bool:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in digest_functions:
                return True
            stack.extend(name for name in calls.get(current, ()) if name in functions)
        return False

    verification_functions = sorted(
        name for name in functions if VERIFICATION_FUNCTION_RE.search(name) and reaches_digest(name)
    )
    return SelfDigestFinding(tuple(sorted(digest_functions)), tuple(verification_functions))


def find_self_digest_producers(repository_root: Path) -> dict[str, SelfDigestFinding]:
    """扫描 tools/official_client_capture（不含 tests／versions）中计算自身文件摘要的模块。"""

    tool_root = Path(repository_root) / TOOL_RELATIVE_ROOT
    findings: dict[str, SelfDigestFinding] = {}
    for path in sorted(tool_root.rglob("*.py")):
        relative = path.relative_to(tool_root)
        if {"tests", "versions", "__pycache__"} & set(relative.parts):
            continue
        source = path.read_bytes()
        if b"__file__" not in source:
            continue
        finding = analyze_self_digest_source(source)
        if finding is not None:
            findings[f"{TOOL_RELATIVE_ROOT}/{relative.as_posix()}"] = finding
    return findings


def unclassified_strict_producers(
    findings: Mapping[str, SelfDigestFinding],
    *,
    producers: Sequence[ProducerSpec] | None = None,
    allowed: Mapping[str, str] | None = None,
) -> dict[str, SelfDigestFinding]:
    """要求摘要等于当前、却既不在 PRODUCERS 也不在分类表里的生成器（覆盖自检的失败对象）。"""

    covered = {spec.path for spec in (PRODUCERS if producers is None else producers)}
    allowed = STRICT_SELF_DIGEST_WITHOUT_REGISTRY if allowed is None else allowed
    return {
        path: finding
        for path, finding in findings.items()
        if finding.strict and path not in covered and path not in allowed
    }


# ---------------------------------------------------------------------------
# 门禁主流程
# ---------------------------------------------------------------------------


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def is_pre_baseline(boundary: Boundary, baseline: str | None = None) -> bool:
    """边界是否至少有一处出处来自门禁基线之前签发（或无签发时间的旧格式）收据。"""

    limit = _parse_utc(GATE_BASELINE_ISSUED_AT_UTC if baseline is None else baseline)
    for source in boundary.sources:
        issued = _parse_utc(source.issued_at_utc)
        if issued is None or (limit is not None and issued <= limit):
            return True
    return False


def check_registration(
    tree_root: Path,
    *,
    git_root: Path | None = None,
    producers: Sequence[ProducerSpec] | None = None,
    exemptions: Mapping[str, Mapping[str, str]] | None = None,
    baseline: str | None = None,
) -> GateReport:
    """对 tree_root（提供承接收据与当前生成器文件）运行门禁；git_root 提供历史对象库。

    ``producers``／``exemptions``／``baseline`` 缺省时取模块常量（在调用时解析，便于反证替换）。
    """

    producers = PRODUCERS if producers is None else producers
    exemptions = HISTORICAL_EXEMPTIONS if exemptions is None else exemptions
    baseline = GATE_BASELINE_ISSUED_AT_UTC if baseline is None else baseline
    tree_root = Path(tree_root).resolve()
    git_root = Path(git_root).resolve() if git_root is not None else tree_root
    ensure_full_history(git_root)
    receipts = load_receipt_scope(tree_root)
    presence = commit_presence(git_root, receipts)
    report = GateReport(tree_root=tree_root)
    for spec in producers:
        if spec.mechanism not in MECHANISMS:
            raise GateError(f"未知登记方式：{spec.mechanism}")
        current_bytes = (tree_root / spec.path).read_bytes()
        current_sha256 = hashlib.sha256(current_bytes).hexdigest()
        report.current[spec.path] = current_sha256
        boundaries = collect_boundaries(
            receipts,
            spec.path,
            git_root=git_root,
            commit_exists=presence,
            unreachable=report.unreachable_commits,
        )
        report.boundaries[spec.path] = boundaries
        module = load_producer_module(tree_root, spec)
        missing = [name for name in (spec.judge_attributes or (spec.registry,)) if not hasattr(module, name)]
        if missing:
            # 该形态下生成器还没有只读重放判定（历史形态）；当前工作树由测试保证不会走到这里。
            report.skipped.append((spec, f"生成器尚无只读重放判定所需的 {'、'.join(missing)}"))
            continue
        if spec.mechanism == MECHANISM_SUCCESSOR_CHAIN:
            _memoize_successor_edges(module)
        exempt = exemptions.get(spec.path, {})
        baseline_current = GATE_BASELINE_CURRENT_SHA256.get(spec.path)
        for digest in sorted(boundaries):
            if digest == current_sha256:
                continue
            boundary = boundaries[digest]
            verdict = replay_verdict(spec, module, boundary)
            if verdict.accepted:
                continue
            detail = verdict.detail
            if digest in exempt:
                if digest == baseline_current:
                    detail += "；该摘要是门禁基线时已部署的版本，修改生成器后必须登记，不得豁免"
                elif not is_pre_baseline(boundary, baseline):
                    detail += "；该摘要在历史豁免表中，但出处全部晚于门禁基线，不得豁免"
                else:
                    report.exempted.append((spec, digest, exempt[digest]))
                    continue
            report.violations.append(
                Violation(
                    producer=spec,
                    current_sha256=current_sha256,
                    boundary_sha256=digest,
                    registry_key=verdict.registry_key,
                    sources=tuple(boundary.sources),
                    detail=detail,
                    registrable=verdict.registrable,
                )
            )
    return report


def format_report(report: GateReport, *, max_sources: int = 4) -> str:
    """把门禁结果写成可直接照做的中文说明。"""

    lines: list[str] = []
    if report.passed:
        lines.append("生成器只读重放身份登记门禁通过。")
    else:
        lines.append(
            "生成器只读重放身份登记门禁失败：以下部署边界处的生成器旧摘要既不等于当前摘要，"
            "也未被生成器自身的只读重放逻辑接受。部署后，这些旧版本已生成的历史收据将无法重放"
            "（例如状态命令报\"ARM64 事实采集器身份漂移\"）。"
        )
        for index, violation in enumerate(report.violations, 1):
            sources = list(violation.sources)
            shown = "；".join(source.describe() for source in sources[:max_sources])
            more = f"；等共 {len(sources)} 处" if len(sources) > max_sources else ""
            lines.extend(
                [
                    f"[{index}] 生成器：{violation.producer.path}",
                    f"    当前摘要：{violation.current_sha256}",
                    f"    未登记的旧摘要：{violation.boundary_sha256}{violation.key_label()}",
                    f"    部署边界出处：{shown}{more}",
                    f"    重放判定：{violation.detail}",
                    f"    处置：{violation.remedy()}",
                ]
            )
        lines.append(
            "部署边界取自 docs/egress/maintenance 冻结承接图收据的显式摘要节点与 base_commit／current_commit；"
            "变更集内部的中间提交不要求登记。门禁基线之后出现的新边界不提供豁免："
            "即使该版本确实未部署，登记为只读重放身份也无害。"
        )
    for spec, digest, reason in report.exempted:
        lines.append(f"历史豁免：{Path(spec.path).name} {digest[:12]}…：{reason}")
    for spec, reason in report.skipped:
        lines.append(f"跳过：{Path(spec.path).name}：{reason}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成器只读重放身份登记门禁（只读）。")
    parser.add_argument("--tree-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--git-root", type=Path, default=None)
    arguments = parser.parse_args(argv)
    try:
        report = check_registration(arguments.tree_root, git_root=arguments.git_root)
    except GateUnavailable as error:
        print(f"门禁无法判定（跳过）：{error}", file=sys.stderr)
        return 2
    except GateError as error:
        print(f"门禁失败关闭：{error}", file=sys.stderr)
        return 1
    print(format_report(report))
    for path, boundaries in report.boundaries.items():
        print(f"{Path(path).name}：当前 {report.current[path][:12]}…，部署边界 {len(boundaries)} 个")
    for commit, receipts in sorted(report.unreachable_commits.items()):
        print(f"信息：收据引用的提交 {commit[:12]} 不在本地对象库（只跳过其前后提交边界）：{'、'.join(receipts)}")
    unclassified = unclassified_strict_producers(find_self_digest_producers(arguments.tree_root))
    for path, finding in sorted(unclassified.items()):
        print(
            f"覆盖自检失败：{path} 要求自身摘要等于当前（校验入口 {'、'.join(finding.verification_functions)}），"
            "却既没有只读重放登记，也没有写明不跨工具版本重放的依据"
        )
    return 0 if report.passed and not unclassified else 1


if __name__ == "__main__":
    # 直接按文件路径执行时，把仓库根放进 sys.path，使生成器内部的包内导入可解析。
    _repository_root = str(Path(__file__).resolve().parents[3])
    if _repository_root not in sys.path:
        sys.path.insert(0, _repository_root)
    sys.exit(main())
