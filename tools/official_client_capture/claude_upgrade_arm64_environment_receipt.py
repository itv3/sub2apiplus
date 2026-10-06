#!/usr/bin/env python3
"""生成并重放 Claude Code 升级所需的 ARM64 网络、磁盘与客户端二进制硬门禁收据（Claude 升级 B1-10）。

一、定位与子命令
================
本模块是 Claude Code 升级控制面的 ARM64 环境 producer，与 Codex 的
``codex_upgrade_arm64_environment_receipt.py``（下称"Codex 模块"）同构，提供四个子命令：

* ``collect``：在 ARM64 宿主机上只读采集原始环境事实（facts），写入证据根；
* ``finalize``：用当前 producer 校验 facts 并封存收据（receipt）；
* ``replay``：按收据记录的 producer 逐字重建收据，任何字节不一致都失败；
* ``egress-check``：出口守护实时准入，直接调用 Codex 模块的 ``require_runtime_egress``（两客户端共用同一份
  root 专有出口策略、持续守护状态与内核租期，历史收据不能替代实时准入）。

编排器使用的 Python 接口与 Codex 模块同名同义：``collect``、``finalize``、``replay``、``build_receipt``、
``validate_facts``、``receipts_equivalent``、``receipt_equivalence_sha256``、``PRODUCER_VERSION`` 与
``Arm64EnvironmentReceiptError``（即 Codex 模块的同一个异常类，调用方捕获一个类即可覆盖两边抛出的错误）。

二、三层合同
============
* facts：``official-client-arm64-environment-facts/v1``，字段闭集见 ``FACT_FIELDS``。除 Codex v8 facts 的通用字段
  （phase、subject_id、observed_at_utc、contract_sha256、host、root_filesystem、runtime_egress、containers、collector）
  外，带 ``client: "claude"``、``client_binary{path, sha256, version}`` 与 ``client_binary_probe``（版本探针的容器、
  隔离 HOME、退出码、版本原文与耗时）；没有 Codex 的 ``rust_tls_readiness``。``containers[*].tls_readiness`` 与 Codex
  同形（name、url、expected_http_status、required_successes、attempts），内容是 Claude 的探针集合。
* receipt：``official-client-arm64-environment-receipt/v1``，在 Codex v8 收据字段之外带 ``client`` 与
  ``client_binary{path, sha256, version}``（取代 Codex 的 ``rust_tls_probe``）。
* producer：``official-client-arm64-environment-producer/v1``，``PRODUCER_VERSION = "1"``；producer 对象与 Codex 同为
  四字段 ``{schema_version, tool, tool_sha256, version}``，身份是本文件的精确字节摘要加规范相对坐标（工作树根可迁移）。

三、与 Codex 模块的同构关系：调用什么、自实现什么
================================================
Codex 模块一字不改（它有 77 个符号在 Codex 评估读侧闭包内，改动会破坏 Codex 身份守恒）。本模块只调用它：

* 出口守护：``require_runtime_egress``、``_checked_runtime_egress``（采集前后两次准入，后一次的校验时刻即
  ``observed_at_utc``）、``validate_egress_policy``、``egress_policy_sha256``、``validate_egress_status``；
* 容器合同：``_validate_container``（名称、容器与镜像身份、固定网络、绑定排序、默认路由、公网出口、原始摘要；
  调用前剥离 Claude 的 ``tls_readiness``，见 ``CODEX_SHARED_CONTAINER_VALIDATION_VERSION``）、``_parse_default_route``；
* 环境等价：``environment_equivalence_projection``（同一宿主与容器的 Codex v8 收据与本收据等价摘要相同）；
* 受 deadline 约束的命令执行与 TLS 瞬态重试：``_run``、``_run_completed``、``_tls_transient_summary``、
  ``_tls_transient_retry``（内部经 ``_transient_backoff`` 在 attempt 共享 deadline 内退避）、``_log_tls_transient``；
* 证据根与文件：``_private_root``、``_relative``、``_load_json``、``_write_once``、``_canonical``、``_sha256_bytes``、
  ``_sha256_file``、``_expect``、``_safe_id``、``_rfc3339``；
* 常量：``PHASES``、``CONTAINER_CONTRACTS``、``SHA256_RE``、``CONTAINER_ID_RE``、``IMAGE_ID_RE``、根盘水位与降级后缀、
  ``TLS_READINESS_ATTEMPTS``、``TLS_READINESS_TRANSIENT_CURL_EXIT_CODES``、抓包容器名与探针运行根的宿主／容器映射、
  x.y.z 版本正则。

下列逻辑在 Codex 模块里内联在别的函数中、或绑定了 Codex 专属常量，不能单独调用，本模块按同一算法自实现：

* ``_container_observation``：Codex 版本写死调用 Codex 的 ChatGPT／OpenAI TLS 探针，不能只取通用部分；
* ``_tls_readiness_observation`` 与 ``_validate_tls_readiness``：Codex 版本遍历 Codex 的探针表，本模块遍历
  ``TLS_READINESS_PROBES``，连续成功次数、瞬态退出码（6／7／28）、退避与总时限全部沿用 Codex 常量与重试函数；
* 根盘采集与资源门禁（``_root_filesystem_observation``、``_resource_gate``）：Codex 内联在 ``_collect_facts`` 与
  ``validate_facts`` 中，算法与 v8 一致（准入阶段硬门禁，``*_after`` 收尾阶段低于水位只记 degraded）；
* 探针隔离运行根校验（``_probe_runtime_root``）：Codex 内联在 Rust TLS 探针中；
* producer 身份（``_current_producer``、``_producer_tool_coordinate``、``_validated_producer_version``）与收据封存／
  重放（``_build_receipt``、``replay``、``receipt_equivalence_sha256``）：Codex 版本绑定 Codex 的文件坐标、版本与登记表。

WireGuard：Codex v8 的 ``collect`` 已不再读取 wg1 配置（``_wireguard_observation`` 只服务 v3～v7 历史合同），隧道、
防火墙、路由与远端守护的健康由出口守护状态 ``shared_protection.checks`` 承担，经 ``validate_egress_status`` 校验。
本模块与 v8 同构，同样经 ``validate_egress_status`` 覆盖，不另行采集 wg1。

四、合同摘要与 Codex v8 网络合同的关系
======================================
``contract_sha256()`` 绑定两部分：

1. 与客户端无关的部分（``shared_with_codex``）：容器合同、出口策略／守护状态／等价投影的 schema、根盘水位与降级
   后缀、TLS 连续成功次数、架构。每一项都在调用时读取 Codex 模块常量，不复制值；Codex 修改其中任何一项时，两边的
   合同摘要同时变化，Claude 历史 facts 按"合同摘要漂移"失败关闭，必须升 ``PRODUCER_VERSION`` 并为旧版本冻结合同。
2. Claude 专属部分：TLS 就绪探针（方法、URL、期望状态）与客户端二进制探针（容器、路径模板、版本参数、期望输出、
   退出码、隔离 HOME、摘要来源）。

不直接嵌入 Codex 的 ``contract_sha256()``：Codex v8 合同还含 Codex 专属的 ChatGPT／OpenAI 端点与 Rust TLS 探针二进制
模板，嵌入会让 Codex 专属探针的调整误改 Claude 合同。与 Codex 一样，瞬态重试参数是采集行为而非校验规则，不进入合同。

五、对设计的偏离与已知缺口
==========================
* 设计原文要求"TLS 就绪探针用 ``/opt/claude-<v>/claude`` 对 ``api.anthropic.com:443`` 做无凭据握手"。实测
  （Claude Code 2.1.197，Bun 单文件可执行）该握手不可观察：二进制不认 ``BUN_BE_BUN=1``（``-e`` 被当成 Claude 自己的
  未知参数）；空 HOME 无凭据运行 ``claude -p hi`` 时，Bun 对 base URL 发一个 ``HEAD`` 预检后输出
  ``Not logged in · Please run /login`` 并以退出码 1 结束，网络通与不通时输出、退出码与 ``--debug`` 日志完全一样。
  因此 TLS 就绪改为在两个容器内用 curl 对 ``https://api.anthropic.com/api/hello`` 发无凭据 ``HEAD``、期望 HTTP 200——
  这正是 Claude 启动预检本身发出的请求（2.1.226 complete-v21 录制中 82 次全部 200，请求不带任何凭据）。
* 客户端二进制身份由两件事证明：容器内 ``/usr/bin/env -i HOME=<隔离空目录> PATH=/usr/bin:/bin /opt/claude-<v>/claude
  --version`` 的输出恰为 ``<v> (Claude Code)`` 加换行且退出码 0（实测不联网、不写 HOME）；容器内
  ``/usr/bin/sha256sum`` 在版本探针前后各读一次、两次必须一致。摘要取容器内而不是宿主 ``/opt/claude-<v>/claude``：
  探针与采集实际执行的是容器内经数据根只读挂载的那一份，宿主实体目录是另一处安装（设计 G3 要求两处同一摘要，
  由入口预检 ``target-client`` 核对），哈希宿主文件证明不了容器内执行的字节。
* 已知缺口：Bun TLS 栈对真实出口的就绪性（即 Claude 二进制自己的 TLS 实现能否经当前出口完成握手）没有可观察的
  无凭据手段，本收据不伪造这一探针；curl 证明的是容器网络、DNS、出口与 Anthropic 端点的 TLS 路径。该缺口由 B5a 在
  ARM64 实测后另定方案。
* 二进制身份不进入连续性／等价身份（与 Codex 的 ``rust_tls_probe`` 同构）：两者都只投影宿主、镜像与抓包拓扑，
  需要核对二进制的消费方读取收据的 ``client_binary``。

六、只读重放登记
================
``REGISTERED_REPLAY_PRODUCER_HASHES`` 是按 ``PRODUCER_VERSION`` 分组的历史 producer 摘要登记表，初始为空（Claude 还
没有历史收据）。本文件部署后再修改时，必须把修改前已部署的摘要登记到它生成收据时的版本号下（写成
``"1": frozenset({"<旧摘要>"})``），只允许只读重放，不允许生成新 facts。``tests/producer_replay_registration.py`` 的
生成器只读重放登记门禁覆盖本文件，会在提交阶段拦下漏登记。

七、跨模块依赖的边界
====================
本 producer 的校验结果还依赖 Codex 模块共享函数的行为，而 producer 身份只覆盖本文件字节。Codex 模块受 Codex 身份
守恒与其自身重放登记门禁约束，改动这些共享函数时须保持对既有 v8 收据（以及本收据）的只读重放兼容；共用常量的变化
由合同摘要直接暴露（见第四节）。
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import platform
import re
import socket
import stat
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

if __package__ in {None, ""}:
    import codex_upgrade_arm64_environment_receipt as codex_arm
else:
    from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as codex_arm


CLIENT = "claude"
FACTS_SCHEMA = "official-client-arm64-environment-facts/v1"
RECEIPT_SCHEMA = "official-client-arm64-environment-receipt/v1"
PRODUCER_SCHEMA = "official-client-arm64-environment-producer/v1"
PRODUCER_VERSION = "1"
PRODUCER_TOOL_RELATIVE = (
    "tools/official_client_capture/claude_upgrade_arm64_environment_receipt.py"
)
NETWORK_CONTRACT_SCHEMA = "official-client-arm64-network-contract/v1"
# 历史 producer 摘要登记表：按 PRODUCER_VERSION 分组，只允许只读重放已封存收据，不允许生成新 facts。
# 初始为空——Claude 还没有任何历史收据。本文件部署后再修改时，把修改前已部署的摘要登记到它生成收据时的
# 版本号下，例如 "1": frozenset({"<旧摘要>"})，并在注释写明它生成过哪些收据（生成器只读重放登记门禁会检查）。
REGISTERED_REPLAY_PRODUCER_HASHES: dict[str, frozenset[str]] = {}

# 与 Codex 模块共用同一个异常类：调用方捕获一个类即可覆盖本模块与被调用的 Codex 函数抛出的错误。
Arm64EnvironmentReceiptError = codex_arm.Arm64EnvironmentReceiptError
# 两客户端共用的 phase 闭集（P0、attempt、Kilo、门禁、canary、部署的前后阶段）。
PHASES = codex_arm.PHASES

# facts 顶层字段闭集。与 Codex v8 相比：多 client、client_binary、client_binary_probe，少 rust_tls_readiness。
FACT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "phase",
        "subject_id",
        "observed_at_utc",
        "contract_sha256",
        "host",
        "root_filesystem",
        "runtime_egress",
        "containers",
        "client_binary",
        "client_binary_probe",
        "collector",
    }
)

# Claude 启动前实际依赖的 TLS 路径：Claude Code 启动时对 base URL 的无凭据 HEAD 预检。每项为
# （探针名, URL, 期望 HTTP 状态），与 Codex 的 TLS_READINESS_PROBES 同形；请求方法单独冻结在 TLS_READINESS_METHOD。
TLS_READINESS_METHOD = "HEAD"
TLS_READINESS_PROBES = (
    ("anthropic-api-hello", "https://api.anthropic.com/api/hello", 200),
)
# 单次 TLS 握手耗时上限（秒），与 Codex 探针的 curl --max-time 与校验上限一致。
TLS_READINESS_MAX_SECONDS = 15

# 客户端二进制探针：在抓包容器 capture-cli 内执行本轮目标版本的二进制。路径按固定模板派生，版本由 collect 参数
# 给出，工具不随客户端版本修改（与 Codex v8 的 Rust TLS 探针目标同法）。
CLIENT_BINARY_PROBE_CONTAINER = codex_arm.RUST_TLS_PROBE_CONTAINER
CLIENT_BINARY_TEMPLATE = "/opt/claude-{version}/claude"
CLIENT_VERSION_RE = codex_arm.CODEX_VERSION_RE  # x.y.z，三段都是不带前导零的十进制整数
CLIENT_VERSION_ARGV = ("--version",)
CLIENT_VERSION_STDOUT_TEMPLATE = "{version} (Claude Code)\n"
CLIENT_BINARY_PROBE_TIMEOUT_SECONDS = 30
# 隔离 HOME 的宿主运行根与它在容器内的映射，与 Codex Rust TLS 探针共用同一对目录（宿主运行根须 0700、
# 归当前执行用户）。在调用时读取本模块常量，离线测试可把宿主运行根重定向到私有临时目录。
CLIENT_PROBE_HOST_RUNTIME_ROOT = codex_arm.RUST_TLS_PROBE_HOST_RUNTIME_ROOT
CLIENT_PROBE_CONTAINER_RUNTIME_ROOT = codex_arm.RUST_TLS_PROBE_CONTAINER_RUNTIME_ROOT
# GNU coreutils sha256sum 的单文件输出：64 位小写十六进制、一个空格、文本模式空格或二进制模式星号、文件名、换行。
SHA256SUM_LINE_RE = re.compile(r"(?P<sha256>[0-9a-f]{64}) [ *](?P<path>[^\n]+)\n")

# Codex ``_validate_container`` 只在 producer_version 属于 {"5"} ∪ RUST_TLS_READINESS_PRODUCER_VERSIONS 时把
# tls_readiness 计入容器字段闭集，并按 Codex 的 ChatGPT／OpenAI 端点逐项校验。Claude 容器的 tls_readiness 是 Claude
# 自己的探针集合，由本模块按 Claude 合同校验；调用 Codex 校验前先剥离该字段，并传入这个不属于上述集合的 Claude 版本
# 标记，使 Codex 只校验两客户端共用的容器字段（运行时出口策略分支与版本号无关）。测试锁定该标记不落入 Codex 的
# TLS 版本集合。
CODEX_SHARED_CONTAINER_VALIDATION_VERSION = "claude/1"
# 传给 Codex ``_validate_container`` 的出口说明：存在运行时策略时 Codex 只按策略的 probe_urls 与 allowed_public_ipv4
# 校验公网出口，不使用这个参数；取值与 Codex v8 一致。
RUNTIME_POLICY_EGRESS_LABEL = "运行时授权策略"


def _current_producer() -> dict[str, str]:
    """当前 producer 身份：本文件规范绝对路径与精确字节摘要。"""

    producer = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(producer),
        "tool_sha256": codex_arm._sha256_file(producer),
        "version": PRODUCER_VERSION,
    }


def _producer_tool_coordinate(value: Any) -> tuple[str, ...] | None:
    """提取 producer 的规范相对坐标，忽略可迁移的工作树绝对根（与 Codex 同一算法，坐标换成本文件）。"""

    if not isinstance(value, str) or not value or not value.startswith("/"):
        return None
    try:
        parsed = PurePosixPath(value)
        parts = parsed.parts
        relative = tuple(PurePosixPath(PRODUCER_TOOL_RELATIVE).parts)
    except (TypeError, ValueError):
        return None
    if (
        str(parsed) != value
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) < len(relative)
        or parts[-len(relative) :] != relative
    ):
        return None
    return relative


def _validated_producer_version(value: Any, *, allow_legacy_replay: bool) -> str:
    """验证当前 producer，或只读承接已登记的历史 producer；返回 facts 所属的 producer 版本。

    同一字节 producer 在不同工作树根生成的收据可以直接承接：路径只需落在相同的受管相对坐标，不能作为身份本身。
    摘要不等于当前时，只有 ``allow_legacy_replay`` 且摘要登记在 ``REGISTERED_REPLAY_PRODUCER_HASHES[version]`` 中才放行。
    """

    producer = codex_arm._expect(
        value,
        {"schema_version", "tool", "tool_sha256", "version"},
        "producer",
    )
    current = _current_producer()
    if (
        producer.get("schema_version") == current["schema_version"]
        and producer.get("version") == current["version"]
        and producer.get("tool_sha256") == current["tool_sha256"]
        and _producer_tool_coordinate(producer.get("tool")) is not None
        and _producer_tool_coordinate(current.get("tool")) is not None
    ):
        return PRODUCER_VERSION
    version = producer.get("version")
    if (
        allow_legacy_replay
        and producer.get("schema_version") == PRODUCER_SCHEMA
        and isinstance(version, str)
        and _producer_tool_coordinate(producer.get("tool")) is not None
        and _producer_tool_coordinate(current.get("tool")) is not None
        and producer.get("tool_sha256")
        in REGISTERED_REPLAY_PRODUCER_HASHES.get(version, frozenset())
    ):
        return version
    raise Arm64EnvironmentReceiptError("Claude ARM64 事实采集器身份漂移")


def contract_sha256() -> str:
    """Claude v1 合同摘要：与客户端无关的部分逐项读取 Codex 模块常量，再加 Claude 专属探针合同（见模块文档第四节）。"""

    return codex_arm._sha256_bytes(
        codex_arm._canonical(
            {
                "schema_version": NETWORK_CONTRACT_SCHEMA,
                "client": CLIENT,
                "shared_with_codex": {
                    "containers": codex_arm.CONTAINER_CONTRACTS,
                    "runtime_policy_schema": codex_arm.EGRESS_POLICY_SCHEMA,
                    "runtime_status_schema": codex_arm.EGRESS_STATUS_SCHEMA,
                    "equivalence_schema": codex_arm.EGRESS_EQUIVALENCE_SCHEMA,
                    "root_max_used_percent": codex_arm.ROOT_MAX_USED_PERCENT,
                    "root_min_available_bytes": codex_arm.ROOT_MIN_AVAILABLE_BYTES,
                    "resource_gate_degradable_phase_suffix": (
                        codex_arm.RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX
                    ),
                    "tls_readiness_attempts": codex_arm.TLS_READINESS_ATTEMPTS,
                    "architecture": "linux/arm64",
                },
                "tls_readiness": {
                    "method": TLS_READINESS_METHOD,
                    "probes": TLS_READINESS_PROBES,
                    "max_tls_seconds": TLS_READINESS_MAX_SECONDS,
                },
                "client_binary": {
                    "container": CLIENT_BINARY_PROBE_CONTAINER,
                    "path_template": CLIENT_BINARY_TEMPLATE,
                    "version_source": "collect 参数：本轮目标版本",
                    "version_argv": list(CLIENT_VERSION_ARGV),
                    "expected_version_stdout_template": CLIENT_VERSION_STDOUT_TEMPLATE,
                    "expected_process_exit_code": 0,
                    "isolated_empty_home": True,
                    "timeout_seconds": CLIENT_BINARY_PROBE_TIMEOUT_SECONDS,
                    "sha256_source": "容器内 /usr/bin/sha256sum，版本探针前后各读一次且必须一致",
                },
            }
        )
    )


def client_binary_target(claude_version: str) -> dict[str, str]:
    """本轮目标版本与按固定模板派生的容器内二进制路径；版本非法时在任何宿主读取之前拒绝。"""

    if not isinstance(claude_version, str) or not CLIENT_VERSION_RE.fullmatch(claude_version):
        raise Arm64EnvironmentReceiptError(
            "Claude 客户端二进制探针目标版本必须是 x.y.z 形式的本轮目标版本"
        )
    return {
        "container": CLIENT_BINARY_PROBE_CONTAINER,
        "path": CLIENT_BINARY_TEMPLATE.format(version=claude_version),
        "version": claude_version,
    }


def _expected_version_stdout(claude_version: str) -> str:
    """``--version`` 的冻结输出：版本号、空格、``(Claude Code)``、换行。"""

    return CLIENT_VERSION_STDOUT_TEMPLATE.format(version=claude_version)


def require_runtime_egress() -> dict[str, Any]:
    """出口守护实时准入：调用 Codex 模块同名函数（两客户端共用同一份策略、守护状态与内核租期）。"""

    return codex_arm.require_runtime_egress()


@contextlib.contextmanager
def _shared_deadline_scope(deadline: Any | None, heartbeat: Any | None) -> Iterator[None]:
    """让本次采集经 Codex 模块执行的全部命令与瞬态退避共享同一条 attempt deadline 与心跳。

    Codex 模块用模块级 ``_ACTIVE_DEADLINE``／``_ACTIVE_HEARTBEAT`` 表达"当前采集作用域"（其 ``collect_facts`` 同法）：
    ``_run_completed`` 在没有显式 deadline 时读取它们，``_transient_backoff`` 只读取它们。这里在作用域内设置、退出时
    原样恢复（异常时同样恢复），不改 Codex 源码。
    """

    previous_deadline = codex_arm._ACTIVE_DEADLINE
    previous_heartbeat = codex_arm._ACTIVE_HEARTBEAT
    codex_arm._ACTIVE_DEADLINE = deadline
    codex_arm._ACTIVE_HEARTBEAT = heartbeat
    try:
        yield
    finally:
        codex_arm._ACTIVE_DEADLINE = previous_deadline
        codex_arm._ACTIVE_HEARTBEAT = previous_heartbeat


def _root_filesystem_observation() -> dict[str, Any]:
    """根文件系统容量事实；算法与 Codex v8 ``_collect_facts`` 内联的计算逐字一致（使用率向上取整）。"""

    filesystem = os.statvfs("/")
    block_size = filesystem.f_frsize or filesystem.f_bsize
    total_bytes = filesystem.f_blocks * block_size
    available_bytes = filesystem.f_bavail * block_size
    used_bytes = (filesystem.f_blocks - filesystem.f_bfree) * block_size
    denominator = used_bytes + available_bytes
    used_percent = (
        (used_bytes * 100 + denominator - 1) // denominator if denominator else 100
    )
    return {
        "mountpoint": "/",
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "available_bytes": available_bytes,
        "used_percent": used_percent,
    }


def _tls_readiness_observation(container: str) -> list[dict[str, Any]]:
    """在容器内连续验证 Claude 启动前实际依赖的 Anthropic TLS 路径（无凭据 HEAD 预检）。

    与 Codex ``_tls_readiness_observation`` 同构：每个探针须连续 ``TLS_READINESS_ATTEMPTS`` 次成功；curl 以
    ``TLS_READINESS_TRANSIENT_CURL_EXIT_CODES``（6 无法解析主机、7 无法连接、28 超时）失败时属于拿到 HTTP 响应之前的
    网络瞬态，清零连续计数、经 Codex ``_tls_transient_retry`` 在共享 deadline 内退避后重新验证（次数、退避序列与
    总时限都是 Codex 常量）；其余退出码由 ``_run_completed`` 立即失败，HTTP 状态不符或响应非法也立即失败。瞬态只写
    ARM64 环境收据日志（进程标准错误），facts 只记录最后连续成功的那几次，编号 1..N。

    curl 只带 ``--disable``（不读 .curlrc）、限定 HTTPS 与 TLS 1.2 以上，不带任何头部、凭据或请求体。
    """

    observations: list[dict[str, Any]] = []
    for probe_name, url, expected_status in TLS_READINESS_PROBES:
        attempts: list[dict[str, Any]] = []
        # 每次网络瞬态：发生时的连续序号、curl 退出码与原文摘要，用于日志与超限报错。
        transients: list[dict[str, Any]] = []
        started = time.monotonic()
        while len(attempts) < codex_arm.TLS_READINESS_ATTEMPTS:
            attempt_index = len(attempts) + 1
            completed = codex_arm._run_completed(
                [
                    "docker",
                    "exec",
                    container,
                    "/usr/bin/curl",
                    "--disable",
                    "--proto",
                    "=https",
                    "--tlsv1.2",
                    "--silent",
                    "--show-error",
                    "--head",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}\t%{remote_ip}\t%{time_appconnect}\n",
                    "--connect-timeout",
                    "6",
                    "--max-time",
                    str(TLS_READINESS_MAX_SECONDS),
                    url,
                ],
                f"{container} {probe_name} TLS 就绪探针第 {attempt_index} 次",
                timeout=20,
                operation=f"arm64:tls-ready:{container}:{probe_name}:{attempt_index}",
                # 瞬态退出码放行到这里再判定；其余非零退出码由 _run_completed 按原文立即失败。
                allowed_returncodes=(
                    frozenset({0}) | codex_arm.TLS_READINESS_TRANSIENT_CURL_EXIT_CODES
                ),
            )
            if completed.returncode != 0:
                transients.append(
                    {
                        "attempt": attempt_index,
                        "exit_code": completed.returncode,
                        "summary": codex_arm._tls_transient_summary(completed.stderr),
                    }
                )
                # 连续验证从头开始：瞬态之前的成功不计入最终的连续成功次数。
                attempts = []
                codex_arm._tls_transient_retry(container, probe_name, transients, started)
                continue
            raw = completed.stdout
            try:
                fields = raw.decode("ascii").strip().split("\t")
                if len(fields) != 3:
                    raise ValueError("字段数不一致")
                http_status = int(fields[0])
                remote_ip = str(ipaddress.ip_address(fields[1]))
                tls_seconds = float(fields[2])
            except (UnicodeError, ValueError) as error:
                raise Arm64EnvironmentReceiptError(
                    f"{container} {probe_name} TLS 就绪探针响应非法"
                ) from error
            if http_status != expected_status or not 0 < tls_seconds <= TLS_READINESS_MAX_SECONDS:
                raise Arm64EnvironmentReceiptError(
                    f"{container} {probe_name} TLS 就绪探针未通过"
                )
            attempts.append(
                {
                    "attempt": attempt_index,
                    "http_status": http_status,
                    "remote_ip": remote_ip,
                    "tls_seconds": tls_seconds,
                    "response_sha256": codex_arm._sha256_bytes(raw),
                }
            )
        if transients:
            # 审计收尾：本探针经历过瞬态，最终仍是连续成功通过。
            codex_arm._log_tls_transient(
                f"{container} {probe_name} TLS 就绪探针在 {len(transients)} 次网络瞬态后连续 "
                f"{codex_arm.TLS_READINESS_ATTEMPTS} 次通过（瞬态退出码："
                + "、".join(str(item["exit_code"]) for item in transients)
                + "）"
            )
        observations.append(
            {
                "name": probe_name,
                "url": url,
                "expected_http_status": expected_status,
                "required_successes": codex_arm.TLS_READINESS_ATTEMPTS,
                "attempts": attempts,
            }
        )
    return observations


def _container_observation(name: str, *, runtime_egress: dict[str, Any]) -> dict[str, Any]:
    """采集一个固定容器的身份、网络、默认路由、守护登记的公网出口与 Claude TLS 就绪事实。

    除 ``tls_readiness`` 外与 Codex v8 ``_container_observation`` 逐字同构；Codex 版本写死调用 Codex 端点的 TLS 探针，
    不能只取通用部分，所以在这里自实现，命令执行与路由解析仍调用 Codex 函数。
    """

    inspect_raw = codex_arm._run(
        ["docker", "inspect", name],
        f"{name} docker inspect",
        operation=f"arm64:docker-inspect:{name}",
    )
    try:
        inspected = json.loads(inspect_raw)
    except json.JSONDecodeError as error:
        raise Arm64EnvironmentReceiptError(f"{name} inspect 不是合法 JSON") from error
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise Arm64EnvironmentReceiptError(f"{name} inspect 结果格式非法")
    item = inspected[0]
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    network_settings = (
        item.get("NetworkSettings") if isinstance(item.get("NetworkSettings"), dict) else {}
    )
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings.get("Networks"), dict)
        else {}
    )
    container_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", ""))
    if state.get("Running") is not True:
        raise Arm64EnvironmentReceiptError(f"{name} 当前未运行")
    if not codex_arm.CONTAINER_ID_RE.fullmatch(container_id) or not codex_arm.IMAGE_ID_RE.fullmatch(image_id):
        raise Arm64EnvironmentReceiptError(f"{name} 容器或镜像身份非法")
    normalized_networks = [
        {
            "name": network_name,
            "network_id": str(network.get("NetworkID", "")),
            "endpoint_id": str(network.get("EndpointID", "")),
            "ipv4_address": str(network.get("IPAddress", "")),
            "gateway": str(network.get("Gateway", "")),
        }
        for network_name, network in sorted(networks.items())
        if isinstance(network, dict)
    ]
    expected = codex_arm.CONTAINER_CONTRACTS[name]
    selected = next(
        (network for network in normalized_networks if network["name"] == expected["network"]),
        None,
    )
    if selected is None:
        raise Arm64EnvironmentReceiptError(f"{name} 缺少固定网络 {expected['network']}")
    route_raw = codex_arm._run(
        ["docker", "exec", name, "cat", "/proc/net/route"],
        f"{name} 默认路由读取",
        operation=f"arm64:default-route:{name}",
    )
    default_route = codex_arm._parse_default_route(route_raw, name)
    service = runtime_egress["runtime"]["services"][name]
    if service["container_id"] != container_id:
        raise Arm64EnvironmentReceiptError("容器在实时准入与环境采集之间发生重建，须重新验证")
    observed = next(item for item in service["observations"] if item["status"] == "passed")
    public_egress = {key: observed[key] for key in ("url", "ip_address", "response_sha256")}
    return {
        "name": name,
        "container_id": container_id,
        "image_id": image_id,
        "selected_network": selected,
        "network_bindings": normalized_networks,
        "default_route": default_route,
        "public_egress": public_egress,
        "tls_readiness": _tls_readiness_observation(name),
        "raw_sha256": {
            "docker_inspect": codex_arm._sha256_bytes(inspect_raw),
            "proc_net_route": codex_arm._sha256_bytes(route_raw),
        },
    }


def _probe_runtime_root() -> Path:
    """校验隔离 HOME 所在的宿主运行根：非符号链接目录、归当前执行用户、权限 0700（与 Codex Rust TLS 探针同一约束）。"""

    runtime_root = CLIENT_PROBE_HOST_RUNTIME_ROOT
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise Arm64EnvironmentReceiptError("Claude 客户端探针临时运行根不可信")
    metadata = runtime_root.stat()
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise Arm64EnvironmentReceiptError(
            "Claude 客户端探针临时运行根必须由当前执行用户拥有且权限为 0700"
        )
    return runtime_root


def _client_binary_digest(target: dict[str, str], *, stage: str) -> str:
    """在容器内用 ``/usr/bin/sha256sum`` 读取目标二进制摘要；输出必须是该路径的单行标准格式。"""

    raw = codex_arm._run(
        ["docker", "exec", target["container"], "/usr/bin/sha256sum", target["path"]],
        f"{target['container']} Claude {target['version']} 客户端二进制摘要读取",
        operation=f"arm64:client-binary:sha256:{stage}",
    )
    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise Arm64EnvironmentReceiptError("Claude 客户端二进制摘要输出编码非法") from error
    match = SHA256SUM_LINE_RE.fullmatch(text)
    if match is None or match.group("path") != target["path"]:
        raise Arm64EnvironmentReceiptError("Claude 客户端二进制摘要输出格式非法或路径不符")
    return match.group("sha256")


def _client_binary_observation(target: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """证明容器内本轮目标二进制的身份：版本自报恰为冻结输出，且探针前后摘要一致。

    版本探针以 ``/usr/bin/env -i`` 清空环境，只给隔离空目录作 HOME 与最小 PATH，不传任何凭据；隔离目录建在宿主
    运行根下（0700），映射为容器内路径，探针结束即删除。实测 ``--version`` 不联网、不写 HOME。
    返回 ``(client_binary, client_binary_probe)``。
    """

    runtime_root = _probe_runtime_root()
    digest_before = _client_binary_digest(target, stage="before")
    with tempfile.TemporaryDirectory(prefix="claude-version-", dir=runtime_root) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        if temporary.is_symlink() or any(temporary.iterdir()):
            raise Arm64EnvironmentReceiptError("Claude 客户端二进制探针 HOME 不是隔离空目录")
        container_home = str(CLIENT_PROBE_CONTAINER_RUNTIME_ROOT / temporary.name)
        started = time.monotonic()
        completed = codex_arm._run_completed(
            [
                "docker",
                "exec",
                target["container"],
                "/usr/bin/env",
                "-i",
                f"HOME={container_home}",
                "PATH=/usr/bin:/bin",
                target["path"],
                *CLIENT_VERSION_ARGV,
            ],
            f"{target['container']} Claude {target['version']} 客户端二进制版本探针",
            timeout=CLIENT_BINARY_PROBE_TIMEOUT_SECONDS,
            operation="arm64:client-binary:version",
            allowed_returncodes=frozenset({0}),
        )
        duration_seconds = round(time.monotonic() - started, 3)
    digest_after = _client_binary_digest(target, stage="after")
    if digest_before != digest_after:
        raise Arm64EnvironmentReceiptError("探针期间 Claude 客户端二进制发生变化，须重新采集")
    expected_stdout = _expected_version_stdout(target["version"])
    if completed.stdout != expected_stdout.encode("ascii"):
        # 原文摘要只用于诊断（控制字符换成空格、截断），复用 Codex 的单行摘要函数。
        raise Arm64EnvironmentReceiptError(
            f"Claude 客户端二进制自报版本与本轮目标 {target['version']} 不一致："
            f"「{codex_arm._tls_transient_summary(completed.stdout)}」"
        )
    if not 0 < duration_seconds <= CLIENT_BINARY_PROBE_TIMEOUT_SECONDS:
        raise Arm64EnvironmentReceiptError("Claude 客户端二进制版本探针耗时与冻结合同不一致")
    return (
        {
            "path": target["path"],
            "sha256": digest_after,
            "version": target["version"],
        },
        {
            "container": target["container"],
            "isolated_empty_home": True,
            "process_exit_code": completed.returncode,
            "version_stdout": expected_stdout,
            "duration_seconds": duration_seconds,
        },
    )


def _collect_facts(*, phase: str, subject_id: str, claude_version: str) -> dict[str, Any]:
    """只读采集真实抓包拓扑、资源与客户端二进制，并在前后核验当前授权出口策略（与 Codex v8 同序）。

    客户端二进制目标先于任何宿主读取完成校验。
    """

    if phase not in PHASES:
        raise Arm64EnvironmentReceiptError(f"phase 必须属于 {sorted(PHASES)}")
    codex_arm._safe_id(subject_id, "subject_id")
    target = client_binary_target(claude_version)
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        raise Arm64EnvironmentReceiptError("本门禁只能在 ARM64 宿主机执行")
    root_filesystem = _root_filesystem_observation()
    before = codex_arm.require_runtime_egress()
    if not set(codex_arm.CONTAINER_CONTRACTS).issubset(before["policy"]["services"]):
        raise Arm64EnvironmentReceiptError("运行时出口策略未保护升级所需的两个容器")
    containers = [
        _container_observation(name, runtime_egress=before)
        for name in sorted(codex_arm.CONTAINER_CONTRACTS)
    ]
    client_binary, client_binary_probe = _client_binary_observation(target)
    after, checked_at = codex_arm._checked_runtime_egress()
    if before["policy_sha256"] != after["policy_sha256"] or any(
        item["container_id"] != after["runtime"]["services"][item["name"]]["container_id"]
        for item in containers
    ):
        raise Arm64EnvironmentReceiptError("环境采集期间策略或容器身份变化，须重新准入")
    return {
        "schema_version": FACTS_SCHEMA,
        "client": CLIENT,
        "phase": phase,
        "subject_id": subject_id,
        "observed_at_utc": checked_at.isoformat(timespec="microseconds"),
        "contract_sha256": contract_sha256(),
        "host": {
            "hostname": socket.gethostname(),
            "architecture": "linux/arm64",
        },
        "root_filesystem": root_filesystem,
        "runtime_egress": after,
        "containers": containers,
        "client_binary": client_binary,
        "client_binary_probe": client_binary_probe,
        "collector": _current_producer(),
    }


def collect_facts(
    *,
    phase: str,
    subject_id: str,
    claude_version: str,
    deadline: codex_arm.incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """采集 Claude ARM64 事实，全部容器命令与瞬态退避共享受管 deadline。"""

    with _shared_deadline_scope(deadline, heartbeat):
        return _collect_facts(phase=phase, subject_id=subject_id, claude_version=claude_version)


def _validate_tls_readiness(value: Any, container_name: str) -> None:
    """按 Claude 合同校验一个容器的 TLS 就绪事实（与 Codex 内联在 ``_validate_container`` 中的校验同构）。"""

    if not isinstance(value, list) or len(value) != len(TLS_READINESS_PROBES):
        raise Arm64EnvironmentReceiptError(
            f"{container_name} Claude TLS 就绪探针没有完整覆盖冻结端点"
        )
    for observed, (probe_name, url, expected_status) in zip(
        value, TLS_READINESS_PROBES, strict=True
    ):
        probe = codex_arm._expect(
            observed,
            {"name", "url", "expected_http_status", "required_successes", "attempts"},
            f"{container_name}.tls_readiness.{probe_name}",
        )
        attempts = probe.get("attempts")
        if (
            probe.get("name") != probe_name
            or probe.get("url") != url
            or probe.get("expected_http_status") != expected_status
            or probe.get("required_successes") != codex_arm.TLS_READINESS_ATTEMPTS
            or not isinstance(attempts, list)
            or len(attempts) != codex_arm.TLS_READINESS_ATTEMPTS
        ):
            raise Arm64EnvironmentReceiptError(
                f"{container_name} {probe_name} TLS 连续成功次数不足"
            )
        for attempt_index, observed_attempt in enumerate(attempts, 1):
            attempt = codex_arm._expect(
                observed_attempt,
                {"attempt", "http_status", "remote_ip", "tls_seconds", "response_sha256"},
                f"{container_name}.{probe_name}.attempt-{attempt_index}",
            )
            try:
                ipaddress.ip_address(str(attempt.get("remote_ip", "")))
            except ValueError as error:
                raise Arm64EnvironmentReceiptError(
                    f"{container_name} {probe_name} 远端 IP 非法"
                ) from error
            tls_seconds = attempt.get("tls_seconds")
            if (
                attempt.get("attempt") != attempt_index
                or attempt.get("http_status") != expected_status
                or isinstance(tls_seconds, bool)
                or not isinstance(tls_seconds, (int, float))
                or not 0 < tls_seconds <= TLS_READINESS_MAX_SECONDS
                or not codex_arm.SHA256_RE.fullmatch(str(attempt.get("response_sha256", "")))
            ):
                raise Arm64EnvironmentReceiptError(
                    f"{container_name} {probe_name} TLS 第 {attempt_index} 次未通过"
                )


def _validate_client_binary(binary_value: Any, probe_value: Any) -> None:
    """校验客户端二进制身份与版本探针事实：路径由版本按模板派生，版本原文恰为冻结输出。"""

    binary = codex_arm._expect(binary_value, {"path", "sha256", "version"}, "client_binary")
    target = client_binary_target(binary.get("version"))
    if binary.get("path") != target["path"]:
        raise Arm64EnvironmentReceiptError("client_binary.path 与本轮目标版本的路径模板不一致")
    if not codex_arm.SHA256_RE.fullmatch(str(binary.get("sha256", ""))):
        raise Arm64EnvironmentReceiptError("client_binary.sha256 不是 64 位小写十六进制摘要")
    probe = codex_arm._expect(
        probe_value,
        {"container", "isolated_empty_home", "process_exit_code", "version_stdout", "duration_seconds"},
        "client_binary_probe",
    )
    exit_code = probe.get("process_exit_code")
    duration = probe.get("duration_seconds")
    if (
        probe.get("container") != target["container"]
        or probe.get("isolated_empty_home") is not True
        or type(exit_code) is not int
        or exit_code != 0
        or probe.get("version_stdout") != _expected_version_stdout(target["version"])
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 0 < float(duration) <= CLIENT_BINARY_PROBE_TIMEOUT_SECONDS
    ):
        raise Arm64EnvironmentReceiptError("Claude 客户端二进制探针事实与冻结合同不一致")


def _resource_gate(value: Any, phase: str) -> dict[str, Any]:
    """根盘硬门禁（与 Codex v8 同一语义）：准入阶段低于水位即失败，``*_after`` 收尾阶段只记 degraded。"""

    filesystem = codex_arm._expect(
        value,
        {"mountpoint", "total_bytes", "used_bytes", "available_bytes", "used_percent"},
        "facts.root_filesystem",
    )
    if filesystem.get("mountpoint") != "/":
        raise Arm64EnvironmentReceiptError("根文件系统挂载点非法")
    for key in ("total_bytes", "used_bytes", "available_bytes", "used_percent"):
        if not isinstance(filesystem.get(key), int) or isinstance(filesystem.get(key), bool):
            raise Arm64EnvironmentReceiptError(f"根文件系统 {key} 非整数")
    if (
        filesystem["total_bytes"] <= 0
        or filesystem["used_bytes"] < 0
        or filesystem["available_bytes"] < 0
        or not 0 <= filesystem["used_percent"] <= 100
    ):
        raise Arm64EnvironmentReceiptError("根文件系统容量事实非法")
    watermark_reached = (
        filesystem["available_bytes"] < codex_arm.ROOT_MIN_AVAILABLE_BYTES
        or filesystem["used_percent"] > codex_arm.ROOT_MAX_USED_PERCENT
    )
    if watermark_reached and not phase.endswith(codex_arm.RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX):
        raise Arm64EnvironmentReceiptError(
            "ARM64 根文件系统达到停线水位（使用率须低于 "
            f"{codex_arm.ROOT_MAX_USED_PERCENT + 1}%，可用空间须不少于 "
            f"{codex_arm.ROOT_MIN_AVAILABLE_BYTES // 1024**3} GiB）"
        )
    gate: dict[str, Any] = {
        "used_percent": filesystem["used_percent"],
        "available_bytes": filesystem["available_bytes"],
        "passed": not watermark_reached,
    }
    if watermark_reached:
        gate["degraded"] = True
    return gate


def _validate_facts(facts: Any, *, allow_legacy_replay: bool) -> dict[str, Any]:
    """严格校验 Claude facts，返回 producer 版本、连续性／等价身份与资源门禁。"""

    if not isinstance(facts, dict):
        raise Arm64EnvironmentReceiptError("facts 必须是对象")
    producer_version = _validated_producer_version(
        facts.get("collector"), allow_legacy_replay=allow_legacy_replay
    )
    if producer_version != PRODUCER_VERSION:
        # v1 是首个版本。以后升 PRODUCER_VERSION 时，旧版本 facts 必须按它生成时的字段闭集与合同摘要只读重放：
        # 在这里按版本分支冻结历史合同（与 Codex validate_facts 的 v1～v7 分支同法），不能用当前合同重写结论。
        raise Arm64EnvironmentReceiptError(
            f"Claude ARM64 环境 facts 版本 {producer_version} 没有登记事实合同"
        )
    codex_arm._expect(facts, set(FACT_FIELDS), "facts")
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise Arm64EnvironmentReceiptError("facts.schema_version 不匹配")
    if facts.get("client") != CLIENT:
        raise Arm64EnvironmentReceiptError("facts.client 必须是 claude")
    if facts.get("phase") not in PHASES:
        raise Arm64EnvironmentReceiptError("facts.phase 非法")
    codex_arm._safe_id(facts.get("subject_id"), "facts.subject_id")
    codex_arm._rfc3339(facts.get("observed_at_utc"), "facts.observed_at_utc")
    if facts.get("contract_sha256") != contract_sha256():
        raise Arm64EnvironmentReceiptError("固定网络、资源或 Claude 探针合同摘要漂移")
    host = codex_arm._expect(facts.get("host"), {"hostname", "architecture"}, "facts.host")
    if (
        host.get("architecture") != "linux/arm64"
        or not isinstance(host.get("hostname"), str)
        or not host["hostname"]
    ):
        raise Arm64EnvironmentReceiptError("宿主机不是可信 ARM64 身份")
    resource_gate = _resource_gate(facts.get("root_filesystem"), str(facts["phase"]))

    containers = facts.get("containers")
    expected_names = sorted(codex_arm.CONTAINER_CONTRACTS)
    if (
        not isinstance(containers, list)
        or len(containers) != len(expected_names)
        or any(not isinstance(item, dict) for item in containers)
        or [item.get("name") for item in containers] != expected_names
    ):
        raise Arm64EnvironmentReceiptError("容器事实必须唯一且完整覆盖固定双容器")
    runtime = codex_arm._expect(
        facts.get("runtime_egress"), {"policy", "policy_sha256", "runtime"}, "运行时出口事实"
    )
    runtime_policy = codex_arm.validate_egress_policy(runtime["policy"])
    if runtime["policy_sha256"] != codex_arm.egress_policy_sha256(runtime_policy):
        raise Arm64EnvironmentReceiptError("运行时出口策略摘要不一致")
    for item, name in zip(containers, expected_names, strict=True):
        if "tls_readiness" not in item:
            raise Arm64EnvironmentReceiptError(f"{name} 缺少 Claude TLS 就绪事实")
        # 两客户端共用的容器字段交给 Codex 校验；Claude 的 TLS 就绪事实按本模块合同单独校验。
        shared_fields = {key: value for key, value in item.items() if key != "tls_readiness"}
        codex_arm._validate_container(
            shared_fields,
            name,
            expected_public_egress=RUNTIME_POLICY_EGRESS_LABEL,
            producer_version=CODEX_SHARED_CONTAINER_VALIDATION_VERSION,
            runtime_policy=runtime_policy,
        )
        _validate_tls_readiness(item["tls_readiness"], name)
    # 历史事实按采集时刻（即第二次实时准入的校验时刻）复算守护状态年龄与观测时效。
    observed_epoch = datetime.fromisoformat(
        facts["observed_at_utc"].replace("Z", "+00:00")
    ).timestamp()
    codex_arm.validate_egress_status(runtime_policy, runtime["runtime"], now_epoch=observed_epoch)
    services = runtime["runtime"]["services"]
    if not set(expected_names).issubset(services):
        raise Arm64EnvironmentReceiptError("运行时出口事实未覆盖固定双容器")
    for container in containers:
        service = services[container["name"]]
        addresses = {binding["ipv4_address"] for binding in container["network_bindings"]}
        if container["container_id"] != service["container_id"] or addresses != {
            binding["source_ipv4"] for binding in service["network_bindings"]
        }:
            raise Arm64EnvironmentReceiptError("容器网络事实与出口守护登记不一致")
    _validate_client_binary(facts.get("client_binary"), facts.get("client_binary_probe"))

    # 连续性与等价身份都取 Codex v8 的等价投影（宿主、镜像与抓包拓扑），与 Codex v8 相同；二进制身份见收据
    # client_binary。同一宿主与容器下，本收据与 Codex v8 收据的等价摘要相同。
    identity = codex_arm._sha256_bytes(
        codex_arm._canonical(codex_arm.environment_equivalence_projection(facts))
    )
    return {
        "producer_version": producer_version,
        "continuity_identity_sha256": identity,
        "equivalence_identity_sha256": identity,
        "resource_gate": resource_gate,
    }


def validate_facts(facts: dict[str, Any], *, allow_legacy_replay: bool = False) -> dict[str, Any]:
    """严格校验原始事实并返回用于前后连续性比较的稳定身份；结构异常一律按收据错误失败关闭。"""

    try:
        return _validate_facts(facts, allow_legacy_replay=allow_legacy_replay)
    except Arm64EnvironmentReceiptError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise Arm64EnvironmentReceiptError(
            f"Claude ARM64 环境 facts 结构非法：{type(error).__name__}"
        ) from error


def _build_receipt(
    root: Path,
    facts_relative: str,
    *,
    replay_producer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = codex_arm._private_root(root)
    facts_path = codex_arm._relative(root, facts_relative, "facts")
    facts, raw = codex_arm._load_json(facts_path, "facts")
    validation = validate_facts(facts, allow_legacy_replay=replay_producer is not None)
    producer = _current_producer() if replay_producer is None else replay_producer
    if facts.get("collector") != producer:
        raise Arm64EnvironmentReceiptError("facts 与 receipt producer 身份不一致")
    if validation["producer_version"] != producer.get("version"):
        raise Arm64EnvironmentReceiptError("facts 与 receipt producer 版本不一致")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "client": CLIENT,
        "status": "passed",
        "phase": facts["phase"],
        "subject_id": facts["subject_id"],
        "observed_at_utc": facts["observed_at_utc"],
        "contract_sha256": facts["contract_sha256"],
        "continuity_identity_sha256": validation["continuity_identity_sha256"],
        "resource_gate": validation["resource_gate"],
        "facts": {
            "path": facts_relative,
            "sha256": codex_arm._sha256_bytes(raw),
            "bytes": len(raw),
        },
        "producer": producer,
        "environment_equivalence": {
            "schema_version": codex_arm.EGRESS_EQUIVALENCE_SCHEMA,
            "sha256": validation["equivalence_identity_sha256"],
        },
        "runtime_egress": {
            "policy_sha256": facts["runtime_egress"]["policy_sha256"],
            "status_sha256": codex_arm._sha256_bytes(
                codex_arm._canonical(facts["runtime_egress"]["runtime"])
            ),
        },
        "client_binary": {
            key: facts["client_binary"][key] for key in ("path", "sha256", "version")
        },
    }


def build_receipt(root: Path, facts_relative: str) -> dict[str, Any]:
    """只使用当前 producer 生成新收据。"""

    return _build_receipt(root, facts_relative)


def collect(
    root: Path,
    output_relative: str,
    *,
    phase: str,
    subject_id: str,
    claude_version: str,
    deadline: codex_arm.incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """采集 facts 并一次性写入证据根（0600，禁止覆盖）。"""

    root = codex_arm._private_root(root)
    output = codex_arm._relative(root, output_relative, "facts output")
    facts = collect_facts(
        phase=phase,
        subject_id=subject_id,
        claude_version=claude_version,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    codex_arm._write_once(output, facts)
    return facts


def finalize(root: Path, facts_relative: str, output_relative: str) -> dict[str, Any]:
    """用当前 producer 封存收据并一次性写入证据根。"""

    root = codex_arm._private_root(root)
    output = codex_arm._relative(root, output_relative, "receipt output")
    receipt = build_receipt(root, facts_relative)
    codex_arm._write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    """按收据记录的 producer 重建收据并逐字比较；任何字段或字节不一致都失败。"""

    root = codex_arm._private_root(root)
    path = codex_arm._relative(root, receipt_relative, "receipt")
    receipt, raw = codex_arm._load_json(path, "receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise Arm64EnvironmentReceiptError("receipt.schema_version 不匹配")
    if receipt.get("client") != CLIENT:
        raise Arm64EnvironmentReceiptError("receipt.client 必须是 claude")
    facts = receipt.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("path"), str):
        raise Arm64EnvironmentReceiptError("receipt.facts 缺失")
    producer = receipt.get("producer")
    _validated_producer_version(producer, allow_legacy_replay=True)
    expected = _build_receipt(root, facts["path"], replay_producer=producer)
    if codex_arm._canonical(expected) != raw:
        raise Arm64EnvironmentReceiptError("Claude ARM64 环境收据重放结果不一致")
    return receipt


def receipt_equivalence_sha256(root: Path, receipt: dict[str, Any]) -> str:
    """先按原 producer 逐字重建收据，再投影等价身份；不改写历史收据，也不读取当前出口策略。"""

    root = codex_arm._private_root(root)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("client") != CLIENT
        or not isinstance(receipt.get("facts"), dict)
        or not isinstance(receipt["facts"].get("path"), str)
        or not isinstance(receipt.get("producer"), dict)
    ):
        raise Arm64EnvironmentReceiptError("环境等价比较缺少完整的 facts／producer 收据")
    expected = _build_receipt(root, receipt["facts"]["path"], replay_producer=receipt["producer"])
    if codex_arm._canonical(receipt) != codex_arm._canonical(expected):
        raise Arm64EnvironmentReceiptError("未经原 producer 完整重放的收据不能参与环境等价比较")
    facts, raw = codex_arm._load_json(
        codex_arm._relative(root, receipt["facts"]["path"], "等价投影 facts"),
        "等价投影 facts",
    )
    if codex_arm._sha256_bytes(raw) != receipt["facts"]["sha256"] or len(raw) != receipt["facts"]["bytes"]:
        raise Arm64EnvironmentReceiptError("等价投影读取期间 facts 发生变化")
    return codex_arm._sha256_bytes(
        codex_arm._canonical(codex_arm.environment_equivalence_projection(facts))
    )


def receipts_equivalent(
    before_root: Path,
    before: dict[str, Any],
    after_root: Path,
    after: dict[str, Any],
) -> bool:
    """跨时间环境比较入口；两侧都必须能按原合同完整重放，否则抛出 ``Arm64EnvironmentReceiptError``。"""

    return receipt_equivalence_sha256(before_root, before) == receipt_equivalence_sha256(
        after_root, after
    )


def build_parser() -> argparse.ArgumentParser:
    # 不直接用模块文档串：它很长，且 python -OO 运行时为 None。
    parser = argparse.ArgumentParser(
        description="生成并重放 Claude Code 升级所需的 ARM64 网络、磁盘与客户端二进制硬门禁收据。"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="只读采集 Claude ARM64 原始环境事实")
    collect_parser.add_argument("--evidence-root", type=Path, required=True)
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    collect_parser.add_argument("--subject-id", required=True)
    collect_parser.add_argument(
        "--claude-version",
        required=True,
        help="客户端二进制探针使用的本轮目标 Claude Code 版本（x.y.z）",
    )
    finalize_parser = commands.add_parser("finalize", help="封存 Claude ARM64 环境收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放 Claude ARM64 环境收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    commands.add_parser(
        "egress-check",
        help="核验当前持续守护和逐容器出口（与 Codex 共用）；历史收据不能替代实时准入",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "egress-check":
            result = require_runtime_egress()
            print(json.dumps({"status": "passed", "policy_sha256": result["policy_sha256"]}, sort_keys=True))
            return 0
        if arguments.command == "collect":
            result = collect(
                arguments.evidence_root,
                arguments.output,
                phase=arguments.phase,
                subject_id=arguments.subject_id,
                claude_version=arguments.claude_version,
            )
        elif arguments.command == "finalize":
            result = finalize(arguments.evidence_root, arguments.facts, arguments.output)
        else:
            result = replay(arguments.evidence_root, arguments.receipt)
    except (OSError, Arm64EnvironmentReceiptError) as error:
        print(f"Claude ARM64 环境收据失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": result.get("status", "collected"),
                "client": CLIENT,
                "phase": result["phase"],
                "subject_id": result["subject_id"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
