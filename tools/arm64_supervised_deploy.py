#!/usr/bin/env python3
"""在 ARM64 上受独立监督器保护地启用 Codex 0.154 工具和文档。

本脚本只负责受管工具和活动文档的同一部署事务，不执行官方请求，也不读取证据。
所有外部命令都经由 ``codex_upgrade_supervisor.SupervisorClient``，文件操作前后均
写入事件账本。发布后的任一校验失败时，会同时恢复旧工具和旧文档，并保留失败
的新版本以便审计。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


# 2026-09-09：candidate_test_fact_map_0_151_0.json 与 candidate_test_trace.py 在合并
# Sub2API v0.2.3 后重绑五份生产源码摘要；事实和规则语义不变，受管工具树摘要随之更新。
# 2026-09-11：codex_upgrade.py 新增候选层运行坐标覆盖并把 campaign-run 强制派发泛化到
# 全部未来目标版本，codex_upgrade_supervisor.py 修正 campaign-mark 与派发超时的竞争，
# codex_upgrade_legacy_boundary.py 去掉版本字面量；受管工具树与监督器摘要随之更新。
# 2026-09-11（批次 2）：codex_upgrade.py 新增 reuse-official-evidence 正式命令，把已封存官方
# 阶段只读导入新 Campaign；受管工具树摘要随之更新。
# 2026-09-11（0.154）：新增目标版本清单、Astra Lite 轨模型政策和 0.154 升级对；部署坐标
# 统一迁入 /root/docker/capture-cli/data，受管工具树摘要随之更新。
# 2026-09-12（0.154 补丁绑定修复）：画像补丁清单改为绑定活动画像文件 SHA-256，
# 并增加穿过正式画像派生校验的离线回归；规则语义与网络行为不变。
# 2026-09-12（canonical 生产链修复）：fresh Candidate attempt 可原生初始化
# checkpoint，并由显式版本参数决定旧 Previous 退休项；受管工具摘要随之更新。
# 2026-09-12（VC 制品链闭合）：新增动态门禁需求／计划、Candidate 构建与交付收据，
# 并把 0.154 的画像派生、构建身份和 post-promotion v4 门禁纳入受管工具树。
# 2026-09-13（VC-0～VC-6 最终对齐）：补齐多批次控制、阶段完成收据、两阶段生产
# 交付和对应失败关闭；受管工具树与监督器摘要随之更新。
# 2026-09-13（BWG 出口切换）：将新 P0 的固定公网出口从 DMIT 切换到 BWG，
# 历史 DMIT 收据仍由环境 producer 的兼容分支只读重放。
# 2026-09-13（VC-1 父租约修复）：父监督器 deadline 进入 attempt reservation，
# campaign-run 新增不可覆盖的脱敏动作失败诊断；受管工具树与监督器摘要随之更新。
# 2026-09-13（运行根可写性修复）：P0 对四个精确可写子挂载执行真实跨别名
# 创建、读取、inode 比对与清理探针，并约束冻结 Job 的证据根；受管工具树摘要随之更新。
# 2026-09-13（失败证据归档路由修复）：宿主编排器把两条容器 runs 别名映射到
# 唯一宿主子树后再归档；P0 新增创建、宿主归档、跨别名读取和清理闭环。
# 2026-09-13（VC-0 原子收口）：P0 新增真实 campaign-run／三轮 Job 失败生命周期
# 门禁，并新增唯一 Formal 收口入口、部分失败阶段关闭和直接 Formal plan 拒绝。
# 2026-09-13（campaign-run 演练收据闭环）：新增受管生成／重放工具，真实执行两个
# VC batch 并证明原始 deadline 漂移在动作前失败关闭；受管工具树摘要随之更新。
# 2026-09-13（Job 演练入口闭环）：失败生命周期 worker 支持由父 campaign-run
# 从任意工作目录按绝对路径启动，并在父进程只写 stdout 时保留结构化失败诊断。
# 2026-09-13（Campaign 输入逐字节冻结）：规则与双份场景清单保持原始字节，
# 防止 preflight 重排 JSON 后破坏场景绑定的规则摘要。
# 2026-09-13（BWG TLS 闭环）：P0 新增固定 IPv4 Endpoint、持久／运行时 MSS clamp
# 和双容器重复 TLS 探针；Campaign 对 Cloud Config 全局启动失败立即停线。
# 2026-09-14（BWG 双向 MSS 与 Rust TLS 修复）：环境 producer v6 同时冻结
# 回程 SYN-ACK MSS、隔离无凭据 Codex Doctor Rust TLS 路径和 attempt heartbeat phase。
# 2026-09-14（VC-1 抓包运行根与 MITM 生命周期修复）：宿主 wrapper 分离容器
# 逻辑根与宿主数据根，MITM 启动失败统一回收，并补齐失败请求计数审计。
# 2026-09-14（VC-1 KeyboardInterrupt 孤儿恢复）：新增失败 attempt 零请求封口、
# 唯一 v3 恢复预览和普通 v2 定向补跑的合同闭环；受管工具树摘要随之更新。
# 2026-09-14（VC-1 中断恢复续接）：修正恢复 attempt 的 heartbeat 绑定基准，
# 并新增只承接既有确定性失败 v3 的唯一 v4 零请求预览；不增加产出侧变化。
# 2026-09-14（VC-1 中断恢复收尾）：统一父 campaign-run 清单的换行规范摘要，
# 并新增只承接既有确定性失败 v4 的唯一 v5 零请求收尾；不增加产出侧变化。
# 2026-09-14（VC-1 deadline 孤儿封口）：批次预留 attempt 清理窗口，并新增只消费
# sequence 5 冻结现场、零模型请求且永久停线的直接 finalizer。
# 2026-09-14（0.154 首轮正式取证缺陷闭合）：压缩场景改用当前非 Lite 第二模型，
# WHAM safe 写入登记数据根，并允许零 Responses／零连接 relay 形成失败请求审计；
# 新增只承接该确定性失败的追加式账本闭合入口。
# 2026-09-14（VC-1 seal 权限前检补偿）：监督器新增只承接本次冻结 sequence 2
# 的一次性 v2→v2 权限收口门禁；其他失败仍沿既有 v3/v4/v5 唯一恢复链停线。
# 2026-09-14（VC-1 权限只读别名收口）：新增只承接 sequence 3 EROFS 的
# sequence 4 窄门禁，经逐 inode 校验的宿主可写 runs 别名执行权限收口。
# 2026-09-14（VC-1 权限别名预派发封口）：修正真实 OAuth 两级证据根识别，
# 固定 tcpdump pcap 的非特权属主边界，并新增只承接 sequence 4 在父 run 创建前
# 确定性误拒绝的零请求 sequence 5 窄门禁。
# 2026-09-15（通用原子派发工具闭合）：后继批次改为单一父 campaign-run
# 监督器，新增通用预派发停线与 ARM64 双跑重放，并同步 0.154 模型轨标签。
# 2026-09-15（VC-1 通用证据闭合）：证据权限收口改为 Attempt v3 通用模块，
# 父动作失败强制关闭阶段与计时账本，并将 P0 双轮演练升级为 v2。
# 2026-09-15（计时生产者后继闭合）：历史账本按显式通用 freeze successor
# 链重放，不再把 0.151 初始工作区快照误当成当前工具摘要。
# 2026-09-15（0.154 Main→Astra Lite 恢复）：压缩场景从唯一 Main gpt-5.5
# 显式切换到冻结的 gpt-6-astra Lite，并新增普通 v2 两阶段恢复交接门禁。
# 2026-09-15（通用 v2 恢复与预派发停线修复）：失败的正式 sequence 1
# 可由普通 v2 零请求恢复预览承接，非 no-op batch 可确定性补写停线收据。
# A2-3：不再硬编码整树摘要。暂存树自算摘要即期望值，候选与生产三向互等；
# 收据额外写策略 v2 的五个摘要，供 VC-0 收口与 seal 按 wire 身份比对。
# 2026-09-17（VC-5 失败 Job 定向恢复）：正式入口精确绑定 v7，VC-0～VC-4
# 只读承接，仅允许重跑 candidate-frozen-core；监督器摘要随之更新。
# 2026-09-17（VC-5 管理凭据执行闭集修复）：管理凭据只校验本轮实际执行 Job，
# 已复用的 candidate-frozen-aux 不再误拦截仅执行 core 的恢复批次。
# 2026-09-17（VC-5 路由映射计数兼容修复）：Candidate readiness 使用 PostgreSQL
# 实际支持的对象键集合计数，避免静态路由快照在派发前误失败。
# 2026-09-17（VC-5 对账副本格式兼容修复）：重派门禁分别校验账本副本摘要，
# 并按 JSON 事实比较 Campaign 与 TimingLedger 副本，避免空白格式差异误报漂移。
# 2026-09-17（VC-5 A15 启动身份并发修正）：启动 models 的 suffix 按初始化
# 并发时序允许缺失或为入口规范值；新增冻结后继随受管运行文档一并部署。
# 2026-09-17（VC-5 post-run seal 恢复）：A15 九项 Job 只读承接到新的
# metadata-only attempt，只重新采集 Kilo 后检查点与两条客户端收据。
# 2026-09-17（VC-5 post-run VC-4 来源修复）：metadata-only 后继继续接受
# A15 逐字投影的原始 v7 构建收据绑定，不要求改写不可变历史 Campaign 身份。
# 2026-09-18（VC-5 post-run 场景兼容修复）：只在 A15 唯一来源与九项 Job
# 执行合同完全相同时承接场景说明元数据变化；任何执行字段变化仍失败关闭。
# 2026-09-18（VC-5 post-run 构建投影门禁修复）：允许 metadata-only 后继在
# 新 attempt 前仅保留 VC-4 构建收据；attempt、结果或其他条目仍失败关闭。
# 2026-09-18（VC-5 metadata-only 预览时延修复）：产出路径无变化时跳过
# 空影响图，并复用同一 Job 已计算的增量元数据；所有身份与摘要门禁保持不变。
# 2026-09-18（VC-5 metadata-only 恢复收据状态修复）：按规范的
# status=restored 判定恢复成功，避免误读不存在的 passed 字段。
# 2026-09-18（VC-5 metadata-only 环境投影修复）：attempt 校验器接受生成器按合同
# 写入的五份环境绑定，并继续严格校验固定路径、SHA-256 与字节数。
# 2026-09-18（VC-5 零请求后处理链收口）：post-run-tooling 可恢复分类、
# OverlayFS seal 预演门禁、candidate-trace-test 零请求 Job 与标签 root_suffix
# 通配；工具身份策略升至 v7。
# 2026-09-18（VC-5 最小闭集：Astra Lite 判定）：候选 relay 合成 /models 补 gpt-6-astra
# 并使清单 authoritative、A03 Astra Lite 采集省略 effort/summary/text、fact map 重绑三份
# 源码快照；同步补登记三次 0.151 台账重签的承接边。监督器与断言预处理器未变。
# 2026-09-18（VC-5 最小闭集：VC-2 输入）：EP-019 画像补丁（wham_usage Luna Reserve 与 cookie
# 槽位）与六条规则的 Astra Lite 精确断言；trace 画像冻结摘要同步。监督器与断言预处理器未变。
# 2026-09-19（升级工具改造第 1 批）：ARM64 环境收据 producer 升 v7（*_after 阶段低于根盘
# 水位只记 degraded，v6 收据按显式合同只读重放）；canonical 交接四步进入 post-run-tooling
# 可恢复分类并按冻结映射从命令提取 attempt，批准摘要只散列 approval_projection。
# 监督器随之变化；断言预处理器未变。
# 2026-09-19（升级工具改造第 2 批 M1：批次 staging/WAL）：VC-2～VC-6 批次先写 staging attempt
# 三件套，父 run 以 prepared 起动，账本事件 → 发布 → COMMIT → running 四步在同一锁内提交；
# 序号占用只认 control/vc/commits/ 的 COMMIT；取得执行权前的失败（P1～P4）由入口孤儿
# 对账、monitor 三分类与 reconciler 新分支闭合，不再产生 predispatch-stop/v1；根因编码表
# 新增四条 code（需项目总账 root-cause-code-migration 收据衔接）。监督器随之变化；断言预处理器未变。
# 2026-09-19（第 2 批 M1 审核修正）：classify_prepared_run 只把"同 Campaign／阶段／序号／
# 规范路径、且属于同序号另一 attempt"的 COMMIT 视为 no_commit，其余外来 COMMIT 一律完整性
# 异常；staging ABORT 的 write-once 改为逐字段内容核对。监督器随之变化；断言预处理器未变。
# 2026-09-19（第 2 批 M2：候选级 revision）：campaign-run 清单成对携带 candidate_revision／
# candidate_id（staging 模型必带、legacy 不得带，Campaign 级为 null）；候选级动作失败按三分支
# 收账（可恢复类不变／永久条件停线／其余 stage_abandoned + candidate_review_required）；失败
# 批次新增"候选作废 → 新 revision VC-4 首批"后继协议。监督器随之变化；断言预处理器未变。
# 2026-09-19（第 2 批 M2 审核修正）：候选 revision 后继协议完整重放失败父 run 对账收据与旧候选
# invalidation.json（schema／身份／项目总账摘要绑定）及新 revision 记录、COMMIT 与账本 stage_revision
# 的摘要链；失败摘要与 candidate_review_required 事件 id 的构造／解析集中为函数，供作废前对账
# 绑定本次失败父 run。监督器随之变化；断言预处理器未变。
# 2026-09-19（第 2 批 M2 审核修正二：reservation 分流）：失败父 run 期间为旧候选发布过 reservation 的，
# 后继协议与作废前对账只认 reconcile-attempt 的收据与总账绑定，否则只认 reconcile-supervisor-run；
# 两条分支的收据校验（campaign_run_failure_facts／candidate_reservations_in_run_window／
# verify_attempt_reconciliation_binding／verify_supervisor_run_reconciliation_binding）集中在监督器，
# 供 invalidate-candidate 共用。监督器随之变化；断言预处理器未变。
# 2026-09-20（第 3 批 M1：评估失败局部恢复）：stop-receipt 升 v2（显式 action_outputs_sha256|null，
# v1 只读兼容，全部读点走 read_stop_receipt）；父监督器在动作退出后 write-once 写动作输出绑定
# （诊断 → action-output → post-run-tooling 收据）；monitor 对 running 父 run 的 owner 丢失按 R2
# 四层判定封存为普通 failed／action-failed:<id>（失败身份不完整或绑定不一致仍 watchdog-aborted）；
# commit 步骤在 nonce-mismatch 后加 evaluator-digests（正式 COMMIT 前核对 evaluator 四项摘要）；
# campaign-run 清单成对携带 evaluation_baseline／baseline_commit_sha256／evaluator_digests 与动作
# output_bindings；失败评估批次新增"评估基线后继"协议；reconciler 在锁内对 owner-loss run 复算
# 三项并补写 post-run-tooling 收据。根因编码表新增 evaluation.rule-failed／attempt.job-transient-failure
# （需项目总账 migration 000003）。监督器随之变化；断言预处理器未变。
# 2026-09-20（第 3 批 M1 审核修正）：动作执行前核对 evaluator 四项摘要（不等即动作不执行、父 run
# failed／identity-drift）；动作输出绑定按声明原顺序写出（清单校验已失败关闭，不再归一化）。
# 监督器随之变化；断言预处理器未变。
# 2026-09-20（第 3 批 M2 attempt-recovery）：父 run 窗口扫描识别恢复段预约（键 <attempt>:ar<k>）、
# attempt 对账绑定校验接受恢复段目录；断言预处理器新增 BASELINE=b<K> 模式（证据根改读
# effective-results，bundle 落本基线私有根）。监督器与断言预处理器均随之变化。
# 2026-09-21（第 3 批 M2 T5.18 端到端）：恢复段 run 动作失败归可恢复（recovery_required，不进候选
# review）；第七种后继协议（失败段批次只能由同 attempt 后继段批次承接）；评估基线后继协议按账本
# 事件历史核对基线；断言预处理器的基线私有根改名 baseline-evidence。监督器与断言预处理器均随之变化。
# 2026-09-21（第 3 批 M2 审核修正：2 个 P1）：后继段预览批准范围必须等于权威链（段预约三元组 → COMMIT
# → recovery.json）取得的 J*、reuse 恒空（第七种协议重放）；崩溃矩阵 R2 的 attempt-recovery 变体——
# 正式单动作恢复段 run 已成功、动作输出绑定已写（段摘要 exists=true）、父 run 终态前 owner 丢失时 monitor
# 确定性封存 failed／parent-finalize-lost，新增"父终态化丢失"N+1 逐字重派协议（走既有对账许可绑定）。
# 监督器随之变化；断言预处理器未变。
# 2026-09-21（第 3 批 M2 审核修正三审：2 个 P1）：后继段预览的请求估算（known_by_job ∪ unknown_job_ids == J*、
# 不相交、known_total == Σknown）与范围等式合并为 recovery_preview_scope_violation（CLI 与第七种协议共用）；
# parent-finalize-lost 的段摘要重验改用与幂等重派相同强度的段加载校验并要求结果 Job 集合恰等于权威链 J*。
# 监督器随之变化；断言预处理器未变。
# 2026-09-22（EvidenceManifest 边界漂移分类）：verify_manifest_boundary 的不可变 stat 边界漂移由生产者给出
# failure_class=evidence-integrity 与观测 evidence-manifest.boundary／stat-boundary-drift，codex_upgrade 原样携带到
# 动作诊断，reconciler 固定终态 integrity_mismatch；0.151 评估恢复台账随 evidence_manifest 重签；ARM64 抓包驱动链
# 入库（tools/arm64_capture_driver，非受管目录，组合安装收据绑定本部署收据）。受管工具树与 evidence 层摘要随之
# 变化；监督器与断言预处理器未变。
# 2026-09-22（VC-6 canonical 派发闭包）：冻结映射拆成 VC-5／VC-6 两组，退休项按 retire-<版本> 动态识别并与
# --retire-version 精确匹配；VC-6 三步必须带绝对路径 --step-receipt，批次按组校验且不得混组、次序为生产激活 →
# 回滚验证 → 退休；监督器 post-run-tooling 闭集按冻结判定接纳 VC-6 三项，使退休动作失败仍归可恢复类。
# 受管工具树与监督器随之变化；断言预处理器未变。
DEFAULT_SUPERVISOR_DIGEST = (
    "70338d6335dd5dbd3a0fde9d7589386aa912c19a36dae0200e08635f82874802"
)
DEFAULT_ASSERTION_PREPARER_DIGEST = (
    "c8020cadd3ee08f67236313a0913dbc3730805b46c3f6b720cdf7ace77f9fec1"
)
RENAME_EXCHANGE = 2
AT_FDCWD = -100
SAFE_SUFFIXES = {".py", ".sh", ".json"}
MANAGED_DOCUMENTS = (
    "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
)
# 计时账本会在运行时从仓库根读取这些来源链文档。它们不是普通说明文档，
# 而是历史 producer 摘要承接的直接依赖，必须与工具树放在同一部署事务中。
MANAGED_RUNTIME_DOCUMENTS = (
    "egress/maintenance/codex-cli-0151-model-policy-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-container-path-recovery-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-timing-producer-replay-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-producer-coordinate-decoupling-source-transition.json",
    "egress/maintenance/codex-cli-0151-worktree-successor.json",
    "egress/maintenance/upstream-codex-0154-vc1-general-toolchain-closeout-20260915-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc1-timing-producer-chain-closeout-20260915-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a0a-tool-unlock-20260915-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a2-a3a-policy-reuse-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-b0-reconciler-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a26-a25-policy-certification-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a25-py312-nesting-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-b1-deletion-proof-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-b-cleanup-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-c-release-certification-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a1b-verdict-copy-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-c1-atomic-container-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-c3-historical-p0-compat-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a0b-provenance-campaign-id-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a2-contract-v2-identity-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a3a-harden-tcpdump-owner-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc1-evidence-boundary-v2-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a0b-stop-phase-sealed-accounting-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-a0b-stop-phase-import-sites-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc2-vc6-governance-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc2-classify-import-origin-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc2-draft-legal-stop-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-client-checkpoint-legal-stop-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-a15-models-cache-restart-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-aux-empty-mapping-20260916-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-framework-closure-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-stopped-identity-precedence-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-metadata-bound-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-provenance-pcap-owner-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-reconciliation-producer-registration-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-classification-successor-vc-control-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-failed-job-recovery-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-legacy-build-replay-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-runtime-rebase-component-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-historical-result-replay-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-execution-identity-rebind-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-admin-execution-scope-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-routing-count-compat-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-reconciliation-json-compat-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-a15-startup-identity-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-seal-recovery-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-build-origin-20260917-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-scenario-compat-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-build-projection-gate-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-metadata-preview-latency-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-metadata-restoration-status-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-metadata-environment-projection-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-closure-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-closure-import-fix-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-post-run-closure-runtime-docs-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-label-coverage-compat-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-recovery-scenario-readonly-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-recovery-scenario-snapshot-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-reuse-closure-frozen-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-official-only-reuse-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-official-reuse-contract-fallback-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-official-reuse-scenario-followup-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-official-reuse-import-fields-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-official-reuse-copy-closure-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-a15-witness-originator-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-zero-request-provenance-readiness-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-seal-rehearsal-dispatch-context-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-boundary-device-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-approval-stop-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-manifest-device-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-manifest-device-ledger-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-candidate-evaluation-epoch-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-manifest-diagnostic-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-mount-order-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-manifest-diff-diag-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-rehearsal-manifest-roots-device-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ledger-resign-mount-type-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ledger-resign-diff-diag-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ledger-resign-roots-device-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-minimal-fix-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ep019-patch-assertions-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-2-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-candidate-v9-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-astra-reasoning-default-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-candidate-v10-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-accept-identity-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-3-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-predispatch-stop-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-4-20260918-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch1-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-canonical-guide-457-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-5-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m1-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-6-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m1-fix-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-7-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m2-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-8-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m2-fix-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-9-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m2-fix2-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-10-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m3-guide-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-11-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m3-guide-fix-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-12-20260919-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m1-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-13-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m1-fix-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-14-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m1-fix2-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-15-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m1-fix3-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-16-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-g0-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t515-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t512-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t513-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t516-20260920-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t518-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-17-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ledger-resign-t515-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-18-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-review-fix-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-review-fix2-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m3-guide-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-19-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m3-guide-fix-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-20-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m3-guide-fix2-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-21-20260921-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-evidence-integrity-20260922-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-ledger-resign-evidence-integrity-20260922-freeze-successor.json",
    "egress/maintenance/upstream-codex-0154-vc5-deploy-manifest-22-20260922-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r13-20260923-freeze-successor.json",
    "egress/runtime-egress-operations.md",
    "egress/maintenance/upstream-codex-01561-r15-20260923-freeze-successor.json",
)
MANAGED_ASSERTION_PREPARER = "prepare_assertion_bundle.sh"
TARGET_SCENARIO_MANIFEST = "codex_upgrade_scenarios_0_154_0.json"
SOURCE_SPEC_HEADINGS = {
    "第二章": "# 第二部分 Codex CLI 客户端规则画像",
    "第二部分": "# 第二部分 Codex CLI 客户端规则画像",
    "第二部分-规则": "# 第二部分 Codex CLI 客户端规则画像",
}

# R15：过滤器挂在受保护容器的父 cgroup；容器创建时即继承默认拒绝。
# 放行键同时绑定 cgroup、容器侧接口和源 IPv4，重新创建／加网卡没有首包放行窗口。
# 内核自行检查单调时钟租期；守护退出或被冻结时，无须用户态清理即可停止出网。
EGRESS_FILTER_SOURCE = r'''
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

struct lease_key { __u64 cgroup_id; __u32 ifindex; __u32 source_ipv4; };
struct lease_value { __u64 expires_at_ns; __u32 mark; __u32 probe_only; };
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, struct lease_key);
    __type(value, struct lease_value);
} leases SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, __u32);
} probes SEC(".maps");

SEC("cgroup_skb/egress")
int egress_gate(struct __sk_buff *skb) {
    __u8 version = 0;
    if (bpf_skb_load_bytes(skb, 0, &version, sizeof(version))) return 0;
    if ((version >> 4) == 6) {
        /* 仅保留进程间 IPv6 loopback；业务 IPv6 不得绕过指定 IPv4 通道。 */
        __u32 source[4] = {}, destination[4] = {};
        if (bpf_skb_load_bytes(skb, 8, source, sizeof(source)) ||
            bpf_skb_load_bytes(skb, 24, destination, sizeof(destination))) return 0;
        return source[0] == 0 && source[1] == 0 && source[2] == 0 && source[3] == bpf_htonl(1) &&
               destination[0] == 0 && destination[1] == 0 && destination[2] == 0 && destination[3] == bpf_htonl(1);
    }
    if ((version >> 4) != 4) return 0;
    __u32 source = 0, destination = 0;
    if (bpf_skb_load_bytes(skb, 12, &source, sizeof(source)) ||
        bpf_skb_load_bytes(skb, 16, &destination, sizeof(destination))) return 0;
    if ((bpf_ntohl(source) >> 24) == 127 && (bpf_ntohl(destination) >> 24) == 127) return 1;
    struct lease_key key = { .cgroup_id = bpf_skb_cgroup_id(skb), .ifindex = skb->ifindex, .source_ipv4 = source };
    struct lease_value *lease = bpf_map_lookup_elem(&leases, &key);
    if (!lease || bpf_ktime_get_ns() >= lease->expires_at_ns) return 0;
    if (lease->probe_only && !bpf_map_lookup_elem(&probes, &destination)) return 0;
    skb->mark = lease->mark;
    return 1;
}
char LICENSE[] SEC("license") = "GPL";
'''

EGRESS_FILTER_LOADER = r'''
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* 仅负责首次加载、挂载与 pin；重启不会解除既有保护或复用未核验对象。 */
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    struct bpf_object *object = bpf_object__open_file(argv[1], NULL);
    if (libbpf_get_error(object)) return 3;
    if (bpf_object__load(object)) return 4;
    struct bpf_program *program = bpf_object__find_program_by_name(object, "egress_gate");
    if (!program) return 5;
    int group = open(argv[2], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (group < 0) return 6;
    struct bpf_link *link = bpf_program__attach_cgroup(program, group);
    if (libbpf_get_error(link)) return 7;
    char path[4096];
    snprintf(path, sizeof(path), "%s/link", argv[3]);
    if (bpf_link__pin(link, path)) return 8;
    struct bpf_map *map;
    bpf_object__for_each_map(map, object) {
        if (strcmp(bpf_map__name(map), "leases") && strcmp(bpf_map__name(map), "probes")) continue;
        snprintf(path, sizeof(path), "%s/%s", argv[3], bpf_map__name(map));
        if (bpf_map__pin(map, path)) return 9;
    }
    snprintf(path, sizeof(path), "%s/program", argv[3]);
    if (bpf_obj_pin(bpf_program__fd(program), path)) return 10;
    printf("{\"program_id_fd\":%d,\"status\":\"attached\"}\n", bpf_program__fd(program));
    close(group);
    bpf_object__close(object);
    return 0;
}
'''


def build_egress_filter(output_root: Path) -> dict[str, str]:
    """在隔离目录编译可复算的内核过滤器与最小加载器，不挂载、不放行任何业务。"""

    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    filter_source = output_root / "egress-filter.bpf.c"
    loader_source = output_root / "egress-filter-loader.c"
    for path, source in ((filter_source, EGRESS_FILTER_SOURCE), (loader_source, EGRESS_FILTER_LOADER)):
        if path.exists() and path.read_text(encoding="utf-8") != source:
            raise DeploymentError("出口过滤器构建目录包含其他版本，须使用新目录")
        path.write_text(source, encoding="utf-8")
    architecture = subprocess.check_output(["gcc", "-dumpmachine"], text=True, timeout=10).strip()
    subprocess.run(["clang", "-O2", "-g", "-target", "bpf", "-Wall", "-Werror",
                    "-I", f"/usr/include/{architecture}", "-c", str(filter_source), "-o", str(output_root / "egress-filter.bpf.o")],
                   check=True, timeout=60)
    subprocess.run(["gcc", "-O2", "-Wall", "-Werror", str(loader_source), "-lbpf", "-lelf", "-lz",
                    "-o", str(output_root / "egress-filter-loader")], check=True, timeout=60)
    return {path.name: sha256_bytes(path.read_bytes()) for path in sorted(output_root.iterdir()) if path.is_file()}


class EgressKernelMaps:
    """直接使用 libbpf 访问已 pin 的对象；仅刷新精确键，不执行外部 shell 或扩展范围。"""

    def __init__(self, pin_root: Path):
        self.pin_root = pin_root
        self.library = ctypes.CDLL("libbpf.so.1", use_errno=True)
        self.library.bpf_obj_get.argtypes = [ctypes.c_char_p]
        self.library.bpf_obj_get.restype = ctypes.c_int
        self.library.bpf_map_update_elem.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong]
        self.library.bpf_map_update_elem.restype = ctypes.c_int
        self.library.bpf_map_delete_elem.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self.library.bpf_map_delete_elem.restype = ctypes.c_int

    def _descriptor(self, name: str) -> int:
        descriptor = self.library.bpf_obj_get(os.fsencode(self.pin_root / name))
        if descriptor < 0:
            raise DeploymentError(f"出口内核对象不可读：{name}，errno={ctypes.get_errno()}")
        return descriptor

    def update(self, name: str, key: bytes, value: bytes) -> None:
        descriptor = self._descriptor(name)
        try:
            if self.library.bpf_map_update_elem(descriptor, ctypes.create_string_buffer(key), ctypes.create_string_buffer(value), 0) != 0:
                raise DeploymentError(f"出口内核放行租期写入失败：errno={ctypes.get_errno()}")
        finally:
            os.close(descriptor)

    def lease(self, cgroup_id: int, ifindex: int, source_ipv4: str, *, expires_at_ns: int, mark: int, probe_only: bool) -> None:
        import socket

        self.update("leases", struct.pack("=QI4s", cgroup_id, ifindex, socket.inet_aton(source_ipv4)),
                    struct.pack("=QII", expires_at_ns, mark, int(probe_only)))

    def probe(self, address: str) -> None:
        import socket

        self.update("probes", socket.inet_aton(address), struct.pack("=I", 1))

    def revoke(self, cgroup_id: int, ifindex: int, source_ipv4: str) -> None:
        """故障时立即删除精确放行键；守护消失时仍由内核租期提供兜底。"""

        descriptor = self._descriptor("leases")
        try:
            key = struct.pack("=QI4s", cgroup_id, ifindex, socket.inet_aton(source_ipv4))
            result = self.library.bpf_map_delete_elem(descriptor, ctypes.create_string_buffer(key))
            if result and ctypes.get_errno() != 2:
                raise DeploymentError("出口内核租期撤销失败")
        finally:
            os.close(descriptor)

    def verify_attachment(self, group: Path) -> dict[str, int]:
        """核对 pin 的 link 指向本次父 cgroup，且程序仍在有效挂载列表内。"""

        self.library.bpf_obj_get_info_by_fd.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        self.library.bpf_obj_get_info_by_fd.restype = ctypes.c_int
        def info(name: str) -> bytes:
            descriptor = self._descriptor(name)
            try:
                data = ctypes.create_string_buffer(256)
                size = ctypes.c_uint(len(data))
                if self.library.bpf_obj_get_info_by_fd(descriptor, data, ctypes.byref(size)):
                    raise DeploymentError("出口内核对象身份不可核验")
                return data.raw
            finally:
                os.close(descriptor)
        program_id = struct.unpack_from("=I", info("program"), 4)[0]
        link = info("link")
        if struct.unpack_from("=I", link, 8)[0] != program_id or struct.unpack_from("=Q", link, 16)[0] != group.stat().st_ino:
            raise DeploymentError("出口过滤器的 link、程序或 cgroup 身份不一致")
        self.library.bpf_prog_query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                               ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        self.library.bpf_prog_query.restype = ctypes.c_int
        descriptor = os.open(group, os.O_RDONLY | os.O_DIRECTORY)
        try:
            count, flags = ctypes.c_uint(64), ctypes.c_uint()
            ids = (ctypes.c_uint * 64)()
            if self.library.bpf_prog_query(descriptor, 1, 1, ctypes.byref(flags), ids, ctypes.byref(count)):
                raise DeploymentError("出口 cgroup 的有效程序列表不可核验")
            if program_id not in list(ids)[:count.value]:
                raise DeploymentError("出口内核过滤器已被解除")
        finally:
            os.close(descriptor)
        return {"program_id": program_id, "cgroup_id": group.stat().st_ino}


class DeploymentError(RuntimeError):
    """部署前提或发布后校验失败。"""


# 网络运维入口与工具发布入口共用本文件；策略、私钥和实时租期均留在工具树外。
EGRESS_TABLE = "sub2api_egress"
EGRESS_MARK = 0xCE000000
EGRESS_REPLY_MARK = 0xCF000000
EGRESS_WG_MARK = 0xDA150001
EGRESS_PRIVATE_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3",
)


def egress_contract() -> Any:
    """沿受管包的正常导入路径取得策略合同，不接受环境变量注入跳过检查。"""

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.official_client_capture import codex_upgrade_arm64_environment_receipt

    return codex_upgrade_arm64_environment_receipt


def egress_command(argv: list[str], *, input_text: str | None = None, timeout: float = 3) -> str:
    """网络守护命令全部有界；错误不输出 inspect 原文或 WireGuard 密钥。"""

    try:
        result = subprocess.run(argv, input=input_text, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f"出口命令未完成：{Path(argv[0]).name}") from error
    if result.returncode:
        raise DeploymentError(f"出口命令失败：{Path(argv[0]).name}，退出码 {result.returncode}")
    return result.stdout.strip()


def egress_service_marks(policy: dict[str, Any]) -> dict[str, int]:
    """同一策略中的容器标记固定排序，不使用可能重用的 IP 作为服务身份。"""

    if len(policy["services"]) > 128:
        raise DeploymentError("出口策略的服务数量超过内核登记上限")
    return {name: EGRESS_MARK + index for index, name in enumerate(sorted(policy["services"]), 1)}


def render_egress_firewall(policy: dict[str, Any], role: str) -> str:
    """生成默认闭锁的两端规则；动态身份与放行集只由持续守护按短租期填充。

    源宿主 bridge hook 在 Docker 转发规则之前约束所有桥端口，inet 的末尾 hook
    再约束路由和 SNAT 后的实际路径。出口宿主同样在 NAT 后检查真实公网源地址。
    已建立连接每包仍须经过租期、身份和路径检查，不能以 established 整体放行。
    """

    egress_contract().validate_egress_policy(policy)
    if role not in {"origin", "exit"}:
        raise DeploymentError("出口节点角色非法")
    node = policy["nodes"][role]
    peer = policy["nodes"]["exit" if role == "origin" else "origin"]
    local_ip = str(ipaddress.IPv4Interface(node["tunnel_ipv4"]).ip)
    remote_ip = str(ipaddress.IPv4Interface(peer["tunnel_ipv4"]).ip)
    wg = json.dumps(node["interface"])
    physical = json.dumps(node["public_interface"])
    endpoint = peer["endpoint"]
    prefix = [
        f"table inet {EGRESS_TABLE} {{",
        " set shared { typeof numgen inc mod 1; flags timeout; }",
        " set private4 { type ipv4_addr; flags interval; elements = { " + ", ".join(EGRESS_PRIVATE_NETWORKS) + " }; }",
    ]
    if role == "origin":
        prefix += [
            " set protected_ports { type iface_index; }",
            " set identities { type iface_index . ipv4_addr . mark; flags timeout; }",
            " set dependencies { type mark . ipv4_addr . inet_proto . inet_service; flags timeout; }",
            " set ingress { type mark . inet_service; flags timeout; }",
            " set dns { type mark . ipv4_addr; flags timeout; }",
            " set probes { type ipv4_addr; flags timeout; }",
            " chain check {",
            "  numgen inc mod 1 @shared jump live",
            "  counter drop",
            " }",
            " chain live {",
            "  meta nfproto != ipv4 counter drop",
            "  meta mark . tcp sport @ingress ct direction reply ct state established,related accept",
            "  meta mark . ip daddr . meta l4proto . th dport @dependencies accept",
            "  ip daddr @private4 counter drop",
            "  meta l4proto { tcp, udp } th dport 53 meta mark . ip daddr @dns jump tunnel",
            "  meta l4proto { tcp, udp } th dport 53 counter drop",
            "  meta mark & 0x00010000 != 0 ip daddr @probes tcp dport 443 jump tunnel",
            "  meta mark & 0x00010000 != 0 counter drop",
            "  jump tunnel",
            " }",
            " chain tunnel {",
            f"  oifname {wg} accept",
            "  counter drop",
            " }",
            " chain forward { type filter hook forward priority 300; policy accept;",
            "  meta mark & 0xff000000 == 0xce000000 jump check",
            "  iif @protected_ports counter drop",
            " }",
            " chain input { type filter hook input priority 300; policy accept;",
            "  iif @protected_ports counter drop",
            "  meta mark & 0xff000000 == 0xce000000 counter drop",
            " }",
            " chain prerouting { type filter hook prerouting priority -145; policy accept;",
            "  meta mark & 0xff000000 == 0xce000000 meta mark . tcp sport @ingress ct direction reply ct state established,related meta mark set meta mark | 0x01000000",
            " }",
            " chain translate_src { type nat hook postrouting priority 90; policy accept;",
            f"  meta mark & 0xff000000 == 0xce000000 oifname {wg} snat ip to {local_ip}",
            " }",
            " chain late { type filter hook postrouting priority 310; policy accept;",
            f"  meta mark {EGRESS_WG_MARK} oifname {physical} ip daddr {endpoint['ipv4']} udp dport {endpoint['port']} accept",
            "  ct direction original ct mark & 0xff000000 == 0xce000000 meta mark & 0xff000000 != 0xce000000 counter drop",
            "  ct direction reply ct mark & 0xff000000 == 0xcf000000 meta mark & 0xff000000 != 0xcf000000 counter drop",
            f"  meta mark & 0xff000000 == 0xce000000 oifname {wg} ip saddr != {local_ip} counter drop",
            "  meta mark & 0xff000000 == 0xce000000 jump check",
            "  meta mark & 0xff000000 == 0xcf000000 numgen inc mod 1 @shared ct direction reply ct state established,related accept",
            "  meta mark & 0xff000000 == 0xcf000000 counter drop",
            " }",
        ]
    else:
        allowed = ", ".join(policy["allowed_public_ipv4"])
        prefix += [
            " chain forward { type filter hook forward priority 310; policy accept;",
            f"  iifname {wg} ip saddr {remote_ip} oifname {physical} ip daddr != @private4 numgen inc mod 1 @shared meta mark set {EGRESS_MARK} ct mark set {EGRESS_MARK} accept",
            f"  iifname {wg} counter drop",
            f"  oifname {wg} iifname {physical} ip daddr {remote_ip} ct direction reply ct state established,related numgen inc mod 1 @shared accept",
            f"  oifname {wg} counter drop",
            " }",
            " chain input { type filter hook input priority 310; policy accept;",
            f"  iifname {wg} ip saddr {remote_ip} ip daddr {local_ip} tcp dport {policy['control_port']} accept",
            f"  iifname {wg} counter drop",
            " }",
            " chain translate_src { type nat hook postrouting priority 90; policy accept;",
            f"  iifname {wg} ip saddr {remote_ip} oifname {physical} snat ip to {policy['allowed_public_ipv4'][0]}",
            " }",
            " chain late { type filter hook postrouting priority 310; policy accept;",
            f"  meta mark {EGRESS_WG_MARK} oifname {physical} ip daddr {endpoint['ipv4']} udp dport {endpoint['port']} accept",
            f"  ct direction original ct mark == {EGRESS_MARK} meta mark != {EGRESS_MARK} counter drop",
            f"  meta mark == {EGRESS_MARK} oifname {physical} ip saddr {{ {allowed} }} numgen inc mod 1 @shared accept",
            f"  meta mark == {EGRESS_MARK} counter drop",
            f"  iifname {wg} counter drop",
            " }",
        ]
    # 外层 UDP 与内层 TCP 均在各自的末尾 hook 核验，端点不能随 DNS 或 peer 漫游改变。
    prefix += [
        " chain output { type filter hook output priority 310; policy accept;",
        f"  meta mark {EGRESS_WG_MARK} oifname {physical} ip daddr {endpoint['ipv4']} udp dport {endpoint['port']} accept",
        f"  meta mark {EGRESS_WG_MARK} counter drop",
        f"  udp sport {node['listen_port']} oifname {physical} ip daddr {endpoint['ipv4']} udp dport {endpoint['port']} accept",
        f"  udp sport {node['listen_port']} counter drop",
        " }",
        " chain mss { type filter hook forward priority -140; policy accept;",
        f"  oifname {wg} tcp flags syn tcp option maxseg size > {node['mtu']-40} tcp option maxseg size set {node['mtu']-40}",
        f"  iifname {wg} tcp flags syn tcp option maxseg size > {node['mtu']-40} tcp option maxseg size set {node['mtu']-40}",
        " }",
        "}",
    ]
    if role == "origin":
        prefix += [
            f"table bridge {EGRESS_TABLE} {{",
            " set shared { typeof numgen inc mod 1; flags timeout; }",
            " set bypass { type iface_index; }",
            " map classification { type iface_index . ipv4_addr : mark; flags timeout; }",
            " set identities { type iface_index . ipv4_addr . mark; flags timeout; }",
            " set dependencies { type mark . ipv4_addr . inet_proto . inet_service; flags timeout; }",
            " set ingress { type mark . inet_service; flags timeout; }",
            " set dns { type mark . ipv4_addr; flags timeout; }",
            " set probes { type ipv4_addr; flags timeout; }",
            " set private4 { type ipv4_addr; flags interval; elements = { " + ", ".join(EGRESS_PRIVATE_NETWORKS) + " }; }",
            " chain ingress { type filter hook prerouting priority -150; policy accept;",
            "  ether type arp accept",
            "  meta iif @bypass accept",
            "  numgen inc mod 1 @shared meta mark set meta iif . ip saddr map @classification",
            "  numgen inc mod 1 @shared meta iif . ip saddr . meta mark @identities jump live",
            "  counter drop",
            " }",
            " chain live {",
            "  ether type != ip counter drop",
            "  ct direction original ct mark set meta mark",
            "  ct direction reply ct mark set meta mark | 0x01000000",
            "  meta mark . tcp sport @ingress ct direction reply ct state established,related accept",
            "  meta mark . ip daddr . meta l4proto . th dport @dependencies accept",
            "  ip daddr @private4 counter drop",
            "  meta l4proto { tcp, udp } th dport 53 meta mark . ip daddr @dns accept",
            "  meta l4proto { tcp, udp } th dport 53 counter drop",
            "  meta mark & 0x00010000 != 0 ip daddr @probes tcp dport 443 accept",
            "  meta mark & 0x00010000 != 0 counter drop",
            "  accept",
            " }",
            "}",
        ]
    return "\n".join(prefix) + "\n"


def egress_firewall_identity(role: str) -> str:
    """只排除内核动态计数、句柄与租期元素，规则、集合类型和 hook 次序都进入身份。"""

    records: list[Any] = []
    for family in (["inet", "bridge"] if role == "origin" else ["inet"]):
        value = json.loads(egress_command(["nft", "-j", "list", "table", family, EGRESS_TABLE]))
        for entry in value["nftables"]:
            if "metainfo" in entry:
                continue
            entry = json.loads(json.dumps(entry))
            if "element" in entry and entry["element"].get("name") != "private4":
                continue
            for kind in ("set", "map"):
                if kind in entry and entry[kind].get("name") != "private4":
                    entry[kind].pop("elem", None)
            def project(item: Any) -> Any:
                if isinstance(item, dict):
                    return {key: (None if key == "counter" else project(child))
                            for key, child in item.items() if key != "handle"}
                if isinstance(item, list):
                    return [project(child) for child in item]
                return item
            records.append(project(entry))
    return sha256_bytes(canonical({"records": records}))


def egress_lease_transaction(policy: dict[str, Any], role: str, inventory: dict[str, Any],
                             service_states: dict[str, str], probes: list[str], *, shared: bool) -> str:
    """以单个 nft 事务替换全部动态租期；未知网卡从未进入集合，过期连接不能绕过。"""

    lines: list[str] = []
    lifetime = int(policy["lease_seconds"] * 1000)
    families = ["inet", "bridge"] if role == "origin" else ["inet"]
    marks = egress_service_marks(policy)
    for family in families:
        values: dict[str, list[str]] = {"shared": ["0"] if shared else []}
        classification: list[str] = []
        if role == "origin":
            values.update({key: [] for key in ("identities", "dependencies", "ingress", "dns", "probes")})
            values["probes"] = probes if shared else []
            if family == "bridge":
                # 非保护容器也须重新登记 host ifindex；不按可能复用的 veth 名或整个网段放行。
                values["bypass"] = [str(value) for value in inventory.get("bypass_ifindices", [])]
            else:
                ports = {item["host_ifindex"] for service in inventory.get("services", {}).values()
                         for item in service.get("bindings", [])}
                lines.append(f"flush set {family} {EGRESS_TABLE} protected_ports")
                if ports:
                    lines.append(f"add element {family} {EGRESS_TABLE} protected_ports {{ " + ", ".join(map(str, sorted(ports))) + " }")
            for name, service in inventory.get("services", {}).items():
                state = service_states.get(name, "blocked")
                if not shared or state not in {"compliant", "probing"}:
                    continue
                mark = marks[name] | (0x10000 if state == "probing" else 0)
                classification += [f"{binding['host_ifindex']} . {binding['source_ipv4']} timeout {lifetime}ms : {mark}" for binding in service["bindings"]]
                values["identities"] += [f"{binding['host_ifindex']} . {binding['source_ipv4']} . {mark}" for binding in service["bindings"]]
                values["dns"] += [f"{mark} . {address}" for address in policy["services"][name]["dns_servers"]]
                if state == "compliant":
                    values["dependencies"] += [f"{mark} . {item['ipv4']} . {item['protocol']} . {item['port']}" for item in service["dependencies"]]
                    values["ingress"] += [f"{mark} . {port}" for port in policy["services"][name]["ingress_tcp_ports"]]
        if family == "bridge":
            # veth 跨网络命名空间会清除 skb mark，宿主按同一份短租期身份重新分类。
            # 此处不从源网段推断服务身份；必须精确匹配已核验的 host ifindex 与源地址。
            lines.append(f"flush map {family} {EGRESS_TABLE} classification")
            if classification:
                lines.append(f"add element {family} {EGRESS_TABLE} classification {{ " + ", ".join(sorted(set(classification))) + " }")
        for key, elements in values.items():
            lines.append(f"flush set {family} {EGRESS_TABLE} {key}")
            if elements:
                suffix = "" if key == "bypass" else f" timeout {lifetime}ms"
                lines.append(f"add element {family} {EGRESS_TABLE} {key} {{ " + ", ".join(f"{element}{suffix}" for element in sorted(set(elements))) + " }")
    return "\n".join(lines) + "\n"


def egress_container_bindings(item: dict[str, Any]) -> list[dict[str, Any]]:
    """从真实 netns 读取每块网卡，再与 Docker 登记交叉核对；附加未登记网卡不能获租期。"""

    pid = int(item["State"]["Pid"])
    links = json.loads(egress_command(["nsenter", "-t", str(pid), "-n", "ip", "-j", "addr", "show"]))
    registered = {network["IPAddress"] for network in item["NetworkSettings"]["Networks"].values() if network.get("IPAddress")}
    bindings = []
    for link in links:
        if link["ifname"] == "lo":
            continue
        if link.get("link_type") != "ether" or not isinstance(link.get("link_index"), int):
            raise DeploymentError("受保护容器出现非受管的网络接口")
        addresses = [address["local"] for address in link.get("addr_info", []) if address["family"] == "inet"]
        if not addresses or any(address not in registered for address in addresses):
            raise DeploymentError("受保护容器出现未登记的源 IPv4")
        for address in addresses:
            bindings.append({"ifindex": link["ifindex"], "host_ifindex": link["link_index"], "source_ipv4": address})
    if not bindings or {binding["source_ipv4"] for binding in bindings} != registered:
        raise DeploymentError("容器网络登记与真实网卡不闭合")
    return sorted(bindings, key=lambda item: (item["ifindex"], item["source_ipv4"]))


def egress_inventory(policy: dict[str, Any], parents: dict[str, str]) -> dict[str, Any]:
    """逐服务隔离发现错误；仅精确确认的其他容器端口进入 bypass，宿主代理从不放行。"""

    identifiers = egress_command(["docker", "ps", "-q", "--no-trunc"]).split()
    items = json.loads(egress_command(["docker", "inspect", *identifiers])) if identifiers else []
    by_name = {item["Name"].lstrip("/"): item for item in items}
    result: dict[str, Any] = {"services": {}, "bypass_ifindices": []}
    for name, item in by_name.items():
        if name in policy["services"]:
            continue
        try:
            bindings = egress_container_bindings(item)
            result["bypass_ifindices"].extend(binding["host_ifindex"] for binding in bindings)
        except (OSError, ValueError, KeyError, DeploymentError):
            # host／none 网络没有桥端口；未知接口维持默认拒绝，不能扩大 bypass。
            continue
    for name, settings in policy["services"].items():
        service: dict[str, Any] = {"container_id": "", "bindings": [], "dependencies": [], "reason": "", "valid": False}
        result["services"][name] = service
        try:
            item = by_name[name]
            pid = int(item["State"]["Pid"])
            if not item["State"].get("Running") or pid <= 0:
                raise DeploymentError("受保护容器尚未运行")
            service["container_id"] = item["Id"]
            service["pid"] = pid
            service["started_at_utc"] = item["State"]["StartedAt"]
            service["bindings"] = egress_container_bindings(item)
            if item["HostConfig"].get("CgroupParent") != settings["cgroup_parent"]:
                raise DeploymentError("容器没有继承策略指定的默认闭锁 slice")
            group_lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
            group_relative = next(line[3:] for line in group_lines if line.startswith("0::"))
            parent = Path(parents[settings["cgroup_parent"]])
            group = Path("/sys/fs/cgroup") / group_relative.lstrip("/")
            if not group.is_relative_to(parent) or group == parent:
                raise DeploymentError("容器进程的真实 cgroup 未继承受保护父组")
            service.update({"cgroup_id": group.stat().st_ino, "cgroup_path": str(group), "parent": settings["cgroup_parent"]})
            resolver = Path(f"/proc/{pid}/root/etc/resolv.conf").read_text(encoding="ascii")
            servers = [line.split()[1] for line in resolver.splitlines() if line.strip().startswith("nameserver ")]
            if servers != settings["dns_servers"]:
                raise DeploymentError("容器 DNS 未直接绑定策略服务器，不能使用宿主转发解析")
            for dependency in settings["dependencies"]:
                other = by_name[dependency["container"]]
                shared_networks = set(item["NetworkSettings"]["Networks"]) & set(other["NetworkSettings"]["Networks"])
                if not shared_networks:
                    raise DeploymentError("必要内网依赖没有共同的受管网络")
                for network in sorted(shared_networks):
                    address = other["NetworkSettings"]["Networks"][network]["IPAddress"]
                    if not address or ipaddress.IPv4Address(address).is_global:
                        raise DeploymentError("内网依赖地址不合规")
                    service["dependencies"].extend({"ipv4": address, "protocol": dependency["protocol"], "port": port} for port in dependency["ports"])
            service["valid"] = True
        except (OSError, ValueError, KeyError, StopIteration, DeploymentError) as error:
            # restart 的采样竞争只可降为缺失状态，不能把消失进程的网卡或观测交给新进程。
            if service.get("pid") and not Path(f"/proc/{service['pid']}").is_dir():
                service["container_id"], service["bindings"] = "", []
            service["reason"] = str(error) if isinstance(error, DeploymentError) else "容器或必要依赖身份不可验证"
    protected_ports = {binding["host_ifindex"] for service in result["services"].values() for binding in service["bindings"]}
    result["bypass_ifindices"] = sorted(set(result["bypass_ifindices"]) - protected_ports)
    return result


def egress_service_identity(service: dict[str, Any]) -> str:
    """容器 restart 可能保留 ID／网卡，进程启动周期和 cgroup 也必须使旧探针失效。"""

    return sha256_bytes(canonical({key: service.get(key) for key in
                                  ("container_id", "bindings", "cgroup_id", "pid", "started_at_utc")}))


def egress_wireguard_observation(policy: dict[str, Any], role: str) -> dict[str, Any]:
    """仅查询公开配置；专用通道严格绑定业务 peer，其他监控接口的 peer 不参与判定。"""

    node = policy["nodes"][role]
    peer = policy["nodes"]["exit" if role == "origin" else "origin"]
    interface = node["interface"]
    observed = {field: egress_command(["wg", "show", interface, field]) for field in ("public-key", "peers", "endpoints", "allowed-ips", "listen-port", "fwmark")}
    expected_allowed = "0.0.0.0/0" if role == "origin" else str(ipaddress.IPv4Interface(peer["tunnel_ipv4"]).ip) + "/32"
    if (observed["public-key"] != node["public_key"] or observed["peers"].splitlines() != [peer["public_key"]]
            or observed["endpoints"].split() != [peer["public_key"], f"{peer['endpoint']['ipv4']}:{peer['endpoint']['port']}"]
            or observed["allowed-ips"].split() != [peer["public_key"], expected_allowed]
            or observed["listen-port"] != str(node["listen_port"])
            or int(observed["fwmark"], 0) != EGRESS_WG_MARK):
        raise DeploymentError("专用 WireGuard 公钥、端点、AllowedIPs 或外层标记发生漂移")
    link = json.loads(egress_command(["ip", "-j", "addr", "show", "dev", interface]))[0]
    ipv4 = [f"{item['local']}/{item['prefixlen']}" for item in link.get("addr_info", []) if item["family"] == "inet"]
    if link["mtu"] != node["mtu"] or ipv4 != [node["tunnel_ipv4"]] or "UP" not in link["flags"]:
        raise DeploymentError("专用通道的地址、MTU 或运行状态漂移")
    return {"interface": interface, "mtu": link["mtu"], "public_key": observed["public-key"],
            "peer_public_key": peer["public_key"], "endpoint": peer["endpoint"], "ifindex": link["ifindex"]}


def egress_verify_routes(policy: dict[str, Any], role: str) -> None:
    """分别验证内层与外层查路结果；晚期防火墙负责在两次检查之间拒绝错误路径。"""

    node = policy["nodes"][role]
    peer = policy["nodes"]["exit" if role == "origin" else "origin"]
    outer = json.loads(egress_command(["ip", "-j", "route", "get", peer["endpoint"]["ipv4"], "mark", str(EGRESS_WG_MARK)]))[0]
    if outer.get("dev") != node["public_interface"]:
        raise DeploymentError("WireGuard 外层路由未走策略指定物理接口")
    if role == "origin":
        for mark in egress_service_marks(policy).values():
            route = json.loads(egress_command(["ip", "-j", "route", "get", policy["services"][next(iter(policy["services"]))]["dns_servers"][0], "mark", str(mark)]))[0]
            if route.get("dev") != node["interface"] or str(route.get("table")) != str(policy["route_table"]):
                raise DeploymentError("受保护业务的策略路由未进入专用通道")
    else:
        for address in policy["services"][next(iter(policy["services"]))]["dns_servers"]:
            route = json.loads(egress_command(["ip", "-j", "route", "get", address, "from", str(ipaddress.IPv4Interface(peer["tunnel_ipv4"]).ip), "iif", node["interface"]]))[0]
            if route.get("dev") != node["public_interface"]:
                raise DeploymentError("出口宿主的业务转发路由发生漂移")


def egress_resolve_probes(policy: dict[str, Any]) -> dict[str, str]:
    """解析结果只提供探针目的地址，不作为允许出口的来源；HTTPS 仍验证原主机证书。"""

    resolved: dict[str, str] = {}
    for url in policy["probe_urls"]:
        host = urlsplit(url).hostname
        try:
            lines = egress_command(["getent", "ahostsv4", str(host)], timeout=3).splitlines()
            addresses = [line.split()[0] for line in lines if line.split()]
            resolved[url] = next(address for address in addresses if ipaddress.IPv4Address(address).is_global)
        except (ValueError, StopIteration, DeploymentError):
            continue
    return resolved


def egress_probe_service(policy: dict[str, Any], name: str, resolved: dict[str, str]) -> list[dict[str, Any]]:
    """从每个容器的真实 curl 路径独立观测；无代理、无重定向、不发送官方模型请求。"""

    def probe(url: str) -> dict[str, Any]:
        observation: dict[str, Any] = {"url": url, "status": "failed", "ip_address": None,
                                       "observed_at_epoch": time.time(), "response_sha256": None}
        address = resolved.get(url)
        if not address:
            return observation
        try:
            raw = egress_command(["docker", "exec", name, "/usr/bin/curl", "--noproxy", "*", "--proto", "=https",
                                  "--tlsv1.2", "--silent", "--show-error", "--fail", "--connect-timeout", "2",
                                  "--max-time", "3", "--max-filesize", "1024", "--resolve", f"{urlsplit(url).hostname}:443:{address}", url], timeout=4)
            ip = str(ipaddress.IPv4Address(raw.strip()))
            observation.update({"status": "passed", "ip_address": ip, "response_sha256": sha256_bytes(raw.encode("ascii"))})
        except (ValueError, DeploymentError):
            pass
        observation["observed_at_epoch"] = time.time()
        return observation
    with ThreadPoolExecutor(max_workers=len(policy["probe_urls"])) as pool:
        return list(pool.map(probe, policy["probe_urls"]))


def egress_remote_status(policy: dict[str, Any]) -> dict[str, Any]:
    """经专用 WireGuard 访问出口守护；不读取本地缓存充当远端当前状态。"""

    import http.client

    address = str(ipaddress.IPv4Interface(policy["nodes"]["exit"]["tunnel_ipv4"]).ip)
    connection = http.client.HTTPConnection(address, policy["control_port"], timeout=min(0.8, policy["lease_seconds"] / 3))
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise DeploymentError("出口守护未返回当前合规状态")
        value = json.loads(raw)
        if (value.get("schema_version") != "codex-runtime-egress-exit-health/v1"
                or value.get("policy_sha256") != egress_contract().egress_policy_sha256(policy)
                or value.get("status") != "compliant" or value.get("lease_remaining_ms", 0) <= 0
                or value["lease_remaining_ms"] > policy["lease_seconds"] * 1000):
            raise DeploymentError("出口守护健康响应没有绑定当前策略和有效租期")
        return value
    except (OSError, ValueError, KeyError, http.client.HTTPException) as error:
        raise DeploymentError("出口宿主当前保护不可确认") from error
    finally:
        connection.close()


class EgressGuard:
    """同时维护内核短租期与可审计状态；进程停止不会删除已有保护规则。"""

    def __init__(self, policy_path: Path, role: str, runtime_root: Path):
        self.contract = egress_contract()
        self.policy_path, self.role, self.runtime_root = policy_path, role, runtime_root
        self.policy = self.contract.load_egress_policy(policy_path)
        self.policy_sha256 = self.contract.egress_policy_sha256(self.policy)
        self.manifest = self.contract._read_egress_runtime_json(runtime_root / "installed.json", private=True)
        if self.manifest["policy_sha256"] != self.policy_sha256 or self.manifest["role"] != role:
            raise DeploymentError("出口保护安装记录未绑定当前策略；须显式重新安装")
        self.parents = self.manifest["parents"]
        self.maps = {name: EgressKernelMaps(Path(value)) for name, value in self.manifest["pins"].items()}
        self.inventory: dict[str, Any] = {"services": {}, "bypass_ifindices": []}
        self.probes = egress_resolve_probes(self.policy) if role == "origin" else {}
        self.observations: dict[str, Any] = {}
        self.pending: dict[str, Any] = {}
        self.next_probe: dict[str, float] = {}
        self.blocked_since: dict[str, float] = {}
        self.last_compliant: dict[str, float] = {}
        self.lease_states: dict[str, str] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.policy["services"])))
        self.resolver_pool = ThreadPoolExecutor(max_workers=1)
        self.resolver_pending: Any | None = None
        self.next_resolution = time.time() + self.policy["probe_refresh_seconds"]
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        self.previous_state: str | None = None
        self.health: dict[str, Any] = {}
        self.health_expiry = 0
        self.status_path = Path("/run/sub2api-egress/status.json")
        self.status_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)

    def revoke(self, inventory: dict[str, Any]) -> None:
        for service in inventory.get("services", {}).values():
            if "parent" not in service:
                continue
            for binding in service["bindings"]:
                self.maps[service["parent"]].revoke(service["cgroup_id"], binding["ifindex"], binding["source_ipv4"])

    def shared_checks(self) -> tuple[dict[str, bool], str]:
        checks = {key: False for key in ("kernel_filter", "firewall", "wireguard", "routes", "remote_guard")}
        try:
            current = self.contract.load_egress_policy(self.policy_path)
            if self.contract.egress_policy_sha256(current) != self.policy_sha256:
                raise DeploymentError("授权出口策略发生变更，须重新安装并验证后恢复")
            for name, maps in self.maps.items():
                maps.verify_attachment(Path(self.parents[name]))
            checks["kernel_filter"] = True
            if egress_firewall_identity(self.role) != self.manifest["firewall_sha256"]:
                raise DeploymentError("两端路径保护中的本端规则发生漂移")
            checks["firewall"] = True
            egress_wireguard_observation(self.policy, self.role)
            checks["wireguard"] = True
            egress_verify_routes(self.policy, self.role)
            checks["routes"] = True
            if self.role == "origin":
                egress_remote_status(self.policy)
            checks["remote_guard"] = True
            return checks, ""
        except (OSError, ValueError, KeyError, DeploymentError) as error:
            return checks, str(error)

    def step(self) -> dict[str, Any]:
        started_ns, now = time.monotonic_ns(), time.time()
        expiry = started_ns + int(self.policy["lease_seconds"] * 10**9)
        checks, reason = self.shared_checks()
        shared = all(checks.values())
        if self.role == "origin":
            if self.resolver_pending is not None and self.resolver_pending.done():
                try:
                    self.probes = self.resolver_pending.result()
                except Exception:
                    self.probes = {}
                self.resolver_pending = None
                self.next_resolution = now + self.policy["probe_refresh_seconds"]
            if self.resolver_pending is None and now >= self.next_resolution:
                self.resolver_pending = self.resolver_pool.submit(egress_resolve_probes, self.policy)
        previous = self.inventory
        if self.role == "origin":
            try:
                self.inventory = egress_inventory(self.policy, self.parents)
            except (OSError, ValueError, KeyError, DeploymentError):
                shared, reason = False, "容器清单不可完整核验"
                checks["kernel_filter"] = False
        states: dict[str, str] = {}
        services: dict[str, Any] = {}
        for name, service in self.inventory.get("services", {}).items():
            old = previous.get("services", {}).get(name, {})
            if (not shared or not service["valid"]
                    or egress_service_identity(old) != egress_service_identity(service)):
                self.observations.pop(name, None)
                self.next_probe[name] = 0
                # 旧探针即使随后返回，也不能为新容器或附加网卡签发准入。
                pending = self.pending.pop(name, None)
                if pending:
                    pending[1].cancel()
            pending = self.pending.get(name)
            identity = egress_service_identity(service)
            if pending and pending[1].done():
                self.pending.pop(name)
                if pending[0] == identity:
                    try:
                        self.observations[name] = pending[1].result()
                    except Exception:
                        self.observations.pop(name, None)
                    self.next_probe[name] = now + self.policy["probe_refresh_seconds"]
            observations = self.observations.get(name, [])
            compliant = shared and service["valid"] and self.contract.egress_observations_compliant(self.policy, observations, now_epoch=time.time())
            states[name] = "compliant" if compliant else ("probing" if shared and service["valid"] else "blocked")
            if compliant:
                self.blocked_since.pop(name, None)
                self.last_compliant[name] = now
            else:
                self.blocked_since.setdefault(name, now)
            services[name] = {"status": "compliant" if compliant else "blocked",
                              "admission_state": "ready" if compliant else "probing" if shared and service["valid"] else "missing" if not service["container_id"] else "invalid",
                              "container_id": service["container_id"],
                              "network_bindings": service["bindings"], "observations": observations,
                              "reason": "" if compliant else reason or service["reason"] or "等待独立出口验证",
                              "blocked_at_epoch": self.blocked_since.get(name)}
        # 先撤销受影响的旧 BPF 身份，再原子换 nft 租期，最后才向新身份发放短租期。
        for name, old in previous.get("services", {}).items():
            current = self.inventory.get("services", {}).get(name)
            if not shared or states.get(name) != self.lease_states.get(name) or current != old:
                self.revoke({"services": {name: old}})
        if not shared:
            self.revoke(self.inventory)
        transaction = egress_lease_transaction(self.policy, self.role, self.inventory, states, sorted(set(self.probes.values())), shared=shared)
        egress_command(["nft", "-f", "-"], input_text=transaction)
        if time.monotonic_ns() >= expiry:
            self.revoke(self.inventory)
            raise DeploymentError("出口守护一轮核验超过内核租期，不得签发迟到状态")
        marks = egress_service_marks(self.policy)
        for name, service in self.inventory.get("services", {}).items():
            if states[name] == "blocked":
                continue
            maps = self.maps[service["parent"]]
            probing = states[name] == "probing"
            for address in self.probes.values():
                maps.probe(address)
            for binding in service["bindings"]:
                maps.lease(service["cgroup_id"], binding["ifindex"], binding["source_ipv4"], expires_at_ns=expiry,
                           mark=marks[name] | (0x10000 if probing else 0), probe_only=probing)
            if name not in self.pending and time.time() >= self.next_probe.get(name, 0):
                identity = egress_service_identity(service)
                self.pending[name] = (identity, self.pool.submit(egress_probe_service, self.policy, name, self.probes))
        status = {"schema_version": self.contract.EGRESS_STATUS_SCHEMA, "policy_sha256": self.policy_sha256,
                  "role": self.role, "boot_id": self.boot_id, "observed_at_epoch": time.time(),
                  "observed_at_monotonic_ns": time.monotonic_ns(),
                  "valid_until_monotonic_ns": expiry, "shared_protection": {
                      "status": "compliant" if shared else "blocked", "checks": checks, "reason": reason}, "services": services}
        write_json_atomic(self.status_path, status)
        self.health = {"schema_version": "codex-runtime-egress-exit-health/v1", "policy_sha256": self.policy_sha256,
                       "status": "compliant" if shared else "blocked", "boot_id": self.boot_id}
        self.health_expiry = expiry if shared else 0
        transition = {"shared": status["shared_protection"], "services": {
            name: {key: value[key] for key in ("status", "container_id", "network_bindings", "reason")}
            for name, value in services.items()}}
        digest = sha256_bytes(canonical(transition))
        if digest != self.previous_state:
            record = {"observed_at_utc": utc_stamp(), "observed_at_epoch": now, "policy_sha256": self.policy_sha256,
                      "role": self.role, "last_compliant_at_epoch": self.last_compliant, **transition}
            descriptor = os.open(self.runtime_root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(descriptor, canonical(record) + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.previous_state = digest
        self.lease_states = states
        return status

    def run(self) -> None:
        server = None
        if self.role == "exit":
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            guard = self
            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    remaining = max(0, (guard.health_expiry - time.monotonic_ns()) // 10**6)
                    valid = self.path == "/health" and remaining > 0 and guard.health.get("status") == "compliant"
                    payload = canonical({**guard.health, "lease_remaining_ms": remaining})
                    self.send_response(200 if valid else 503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *_args: Any) -> None:
                    return

            address = str(ipaddress.IPv4Interface(self.policy["nodes"]["exit"]["tunnel_ipv4"]).ip)
            server = ThreadingHTTPServer((address, self.policy["control_port"]), HealthHandler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            while True:
                start = time.monotonic()
                try:
                    self.step()
                except Exception:
                    self.health_expiry = 0
                    self.revoke(self.inventory)
                    # 异常记录不得包含子命令原文；即使用户态再次失败，内核租期仍会自行失效。
                    print("出口守护本轮失败，已闭锁；等待下一轮重新核验", file=sys.stderr, flush=True)
                time.sleep(max(0.05, self.policy["poll_seconds"] - (time.monotonic() - start)))
        finally:
            self.health_expiry = 0
            self.revoke(self.inventory)
            if server:
                server.shutdown()
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.resolver_pool.shutdown(wait=False, cancel_futures=True)


def egress_apply_firewall(policy: dict[str, Any], role: str) -> str:
    """规则替换是单个内核事务；失败保留旧表，成功时新表的动态放行集为空。"""

    tables = json.loads(egress_command(["nft", "-j", "list", "tables"]))["nftables"]
    existing = {(item["table"]["family"], item["table"]["name"]) for item in tables if "table" in item}
    families = ["inet", "bridge"] if role == "origin" else ["inet"]
    deletion = "".join(f"delete table {family} {EGRESS_TABLE}\n" for family in families if (family, EGRESS_TABLE) in existing)
    bypass = ""
    if role == "origin":
        # 首次安装也精确保留无关容器的桥端口，不能等待编译／握手期间切断数据库等已有流量。
        # 开机 bootstrap 先于 Docker；此时不能查询 socket 触发 Docker 启动，形成依赖环。
        active = subprocess.run(["systemctl", "is-active", "--quiet", "docker.service"],
                                capture_output=True, timeout=3).returncode == 0
        ports = egress_inventory(policy, {})["bypass_ifindices"] if active else []
        if ports:
            bypass = f"add element bridge {EGRESS_TABLE} bypass {{ " + ", ".join(map(str, ports)) + " }\n"
    egress_command(["nft", "-f", "-"], input_text=deletion + render_egress_firewall(policy, role) + bypass)
    return egress_firewall_identity(role)


def egress_bootstrap(policy_path: Path, role: str, runtime_root: Path) -> dict[str, Any]:
    """先闭锁路径，再加载父 cgroup 过滤器；此入口绝不添加业务放行租期。"""

    contract = egress_contract()
    policy = contract.load_egress_policy(policy_path)
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    old_manifest = None
    manifest_path = runtime_root / "installed.json"
    if manifest_path.exists():
        old_manifest = contract._read_egress_runtime_json(manifest_path, private=True)
    firewall_sha256 = egress_apply_firewall(policy, role)
    parents: dict[str, str] = {}
    pins: dict[str, str] = {}
    interrupted_pins: list[Path] = []
    artifacts: dict[str, str] = {}
    if role == "origin":
        source_identity = sha256_bytes((EGRESS_FILTER_SOURCE + EGRESS_FILTER_LOADER).encode("utf-8"))
        build_root = runtime_root / ("kernel-" + source_identity[:16])
        artifacts = build_egress_filter(build_root)
        for name in sorted({service["cgroup_parent"] for service in policy["services"].values()}):
            egress_command(["systemctl", "start", name], timeout=10)
            relative = egress_command(["systemctl", "show", "--value", "-p", "ControlGroup", name])
            parent = Path("/sys/fs/cgroup") / relative.lstrip("/")
            if not relative.startswith("/") or parent == Path("/sys/fs/cgroup") or not parent.is_dir():
                raise DeploymentError("出口专用 slice 未取得可信 cgroup")
            pin = Path("/sys/fs/bpf/sub2api-egress") / (name + "-" + source_identity[:16])
            if pin.exists():
                try:
                    EgressKernelMaps(pin).verify_attachment(parent)
                except (OSError, DeploymentError):
                    # 上次可能只 pin 了 link 就中断；先挂新的默认拒绝程序，再清理半成品。
                    interrupted_pins.append(pin)
                    pin = pin.with_name(pin.name + "-repair-" + secrets.token_hex(4))
            if not pin.exists():
                pin.mkdir(mode=0o700, parents=True)
                egress_command([str(build_root / "egress-filter-loader"), str(build_root / "egress-filter.bpf.o"), str(parent), str(pin)])
            EgressKernelMaps(pin).verify_attachment(parent)
            parents[name], pins[name] = str(parent), str(pin)
        # 换内核程序时新程序先挂载并默认拒绝，随后才解除本工具以前登记的旧 link。
        old_paths = {Path(value) for value in (old_manifest or {}).get("pins", {}).values()} | set(interrupted_pins)
        for path in old_paths:
            if str(path) not in pins.values():
                if path.parent != Path("/sys/fs/bpf/sub2api-egress"):
                    raise DeploymentError("历史内核 pin 路径不属于本工具，拒绝清理")
                for entry in ("link", "program", "leases", "probes"):
                    (path / entry).unlink(missing_ok=True)
                if path.exists():
                    path.rmdir()
    manifest = {"schema_version": "codex-runtime-egress-install/v1", "policy_sha256": contract.egress_policy_sha256(policy),
                "role": role, "installed_at_utc": utc_stamp(), "firewall_sha256": firewall_sha256,
                "parents": parents, "pins": pins, "kernel_artifacts": artifacts}
    write_json_atomic(manifest_path, manifest)
    return manifest


def egress_setup_routes(policy: dict[str, Any], role: str) -> None:
    """仅配置专用表和精确 mark 规则；其他出口和现有 WireGuard 监控通道保持独立。"""

    node = policy["nodes"][role]
    if role == "origin":
        table = str(policy["route_table"])
        egress_command(["ip", "route", "replace", "table", table, "default", "dev", node["interface"]])
        for network in EGRESS_PRIVATE_NETWORKS:
            egress_command(["ip", "route", "replace", "throw", network, "table", table])
        rules = json.loads(egress_command(["ip", "-j", "rule", "show"]))
        matches = [rule for rule in rules if rule.get("priority") == policy["rule_priority"]]
        if matches:
            expected = {"fwmark": hex(EGRESS_MARK), "fwmask": "0xff000000", "table": policy["route_table"]}
            if len(matches) != 1 or any(str(matches[0].get(key)) != str(value) for key, value in expected.items()):
                raise DeploymentError("策略路由优先级已被其他规则使用，拒绝覆盖")
        else:
            egress_command(["ip", "rule", "add", "priority", str(policy["rule_priority"]), "fwmark", f"{EGRESS_MARK}/0xff000000", "lookup", table])
    egress_command(["sysctl", "-w", f"net.ipv4.conf.{node['interface']}.rp_filter=0"])


def egress_write_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """先持久化完整新文件再替换；中断只留下完整旧版或新版，不截断私钥配置。"""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DeploymentError("出口配置目标不是普通文件")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def egress_runtime_bundle(runtime_root: Path) -> Path:
    """持久化按内容寻址的最小运行闭包；systemd 不依赖会清理或覆盖的 staging 目录。"""

    source_root = Path(__file__).resolve().parent.parent
    names = ("tools/arm64_supervised_deploy.py",
             "tools/official_client_capture/codex_upgrade_arm64_environment_receipt.py",
             "tools/official_client_capture/incremental_recovery.py")
    sources = {name: (source_root / name).read_bytes() for name in names}
    identity = sha256_bytes(canonical({name: sha256_bytes(data) for name, data in sources.items()}))
    directory = runtime_root / "bundles"
    directory.mkdir(mode=0o700, exist_ok=True)
    bundle = directory / identity
    if bundle.exists():
        if bundle.is_symlink() or any((bundle / name).is_symlink() or (bundle / name).read_bytes() != data
                                     for name, data in sources.items()):
            raise DeploymentError("已登记的出口运行包被修改，拒绝覆盖")
        return bundle / names[0]
    temporary = directory / (".install-" + secrets.token_hex(8))
    try:
        temporary.mkdir(mode=0o700)
        for name, data in sources.items():
            path = temporary / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            egress_write_atomic(path, data, mode=0o400)
        write_json_atomic(temporary / "bundle.json", {"schema_version": "codex-runtime-egress-bundle/v1",
                          "bundle_sha256": identity, "files": {name: sha256_bytes(data) for name, data in sources.items()}})
        os.rename(temporary, bundle)
        fsync_directory(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return bundle / names[0]


def egress_compose_settings(policy: dict[str, Any], name: str, items: list[dict[str, Any]], resolver: Path) -> dict[str, Any]:
    """生成可审核的 compose 服务保护字段；精确补全必要依赖的 hosts，不绑定只读 /etc/hosts。"""

    settings = policy["services"][name]
    by_name = {item["Name"].lstrip("/"): item for item in items}
    item = by_name[name]
    aliases: dict[str, str] = {}
    for dependency in settings["dependencies"]:
        other = by_name[dependency["container"]]
        shared = sorted(set(item["NetworkSettings"]["Networks"]) & set(other["NetworkSettings"]["Networks"]))
        if not shared:
            raise DeploymentError("compose 必要依赖没有共同网络，拒绝猜测地址")
        network = other["NetworkSettings"]["Networks"][shared[0]]
        address = network["IPAddress"]
        if not ipaddress.IPv4Address(address).is_private:
            raise DeploymentError("compose 必要依赖必须使用私网地址")
        for alias in {dependency["container"], *(network.get("Aliases") or [])}:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias):
                raise DeploymentError("compose 依赖别名非法")
            if alias in aliases and aliases[alias] != address:
                raise DeploymentError("compose 依赖别名存在地址冲突")
            aliases[alias] = address
    return {"cgroup_parent": settings["cgroup_parent"], "dns": settings["dns_servers"],
            "volumes": [{"type": "bind", "source": str(resolver), "target": "/etc/resolv.conf", "read_only": True}],
            "extra_hosts": dict(sorted(aliases.items()))}


def egress_prepare_compose(policy: dict[str, Any], output: Path, name: str, compose_service: str) -> dict[str, Any]:
    """只生成保护覆盖文件和摘要，不重建容器；生产 compose 须在维护窗口审核合并。"""

    if name not in policy["services"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", compose_service):
        raise DeploymentError("compose 服务映射必须显式指定受保护容器及合法服务名")
    if not output.is_absolute() or output.exists():
        raise DeploymentError("compose 保护制品必须写入新的绝对路径目录")
    identifiers = egress_command(["docker", "ps", "-a", "-q", "--no-trunc"]).split()
    items = json.loads(egress_command(["docker", "inspect", *identifiers])) if identifiers else []
    resolver = output / "resolv.conf"
    settings = egress_compose_settings(policy, name, items, resolver)
    output.mkdir(mode=0o700, parents=True)
    egress_write_atomic(resolver, ("".join(f"nameserver {address}\n" for address in policy["services"][name]["dns_servers"])
                                  + "options timeout:2 attempts:2\n").encode("ascii"), mode=0o644)
    override = output / "compose.override.json"
    write_json_atomic(override, {"services": {compose_service: settings}})
    result = {"schema_version": "codex-runtime-egress-compose/v1", "policy_sha256": egress_contract().egress_policy_sha256(policy),
              "container": name, "compose_service": compose_service,
              "files": {path.name: file_sha256(path) for path in (resolver, override)}}
    write_json_atomic(output / "manifest.json", result)
    return result


def egress_install(policy_path: Path, role: str, runtime_root: Path, key_path: Path) -> dict[str, Any]:
    """安装持久保护与启动顺序；必须在维护窗口使用，业务恢复由守护逐容器验证决定。"""

    contract = egress_contract()
    policy = contract.load_egress_policy(policy_path)
    node = policy["nodes"][role]
    peer = policy["nodes"]["exit" if role == "origin" else "origin"]
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if key_path.is_symlink() or not key_path.is_file():
        raise DeploymentError("专用通道私钥文件不是可信普通文件")
    metadata = key_path.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DeploymentError("专用通道私钥必须为 root:root 0600")
    private = key_path.read_text(encoding="ascii").strip()
    if egress_command(["wg", "pubkey"], input_text=private + "\n") != node["public_key"]:
        raise DeploymentError("专用通道私钥不对应授权策略公钥")
    script = egress_runtime_bundle(runtime_root)
    # 在生成规则和闭锁之前不触碰已有接口；新安装不能接管未登记的 wg1 等通道。
    interfaces = json.loads(egress_command(["ip", "-j", "link", "show"]))
    if not (runtime_root / "installed.json").exists() and any(link["ifname"] == node["interface"] for link in interfaces):
        raise DeploymentError("策略接口已存在但未登记为本工具专用通道，拒绝接管")
    if (runtime_root / "installed.json").exists():
        egress_command(["systemctl", "stop", "sub2api-egress-guard.service"], timeout=20)
    # 配置替换前闭锁，后续任何写入或服务启动失败也不会沿旧租期继续业务。
    egress_apply_firewall(policy, role)
    parents = sorted({service["cgroup_parent"] for service in policy["services"].values()}) if role == "origin" else []
    for name in parents:
        path = Path("/etc/systemd/system") / name
        egress_write_atomic(path, "[Unit]\nDescription=Sub2API 专用出口受保护容器组\n[Slice]\n".encode("utf-8"), mode=0o644)
    common = f"--policy {shlex.quote(str(policy_path))} --role {role} --runtime-root {shlex.quote(str(runtime_root))}"
    execute = f"/usr/bin/python3 {shlex.quote(str(script))}"
    unit_root = Path("/etc/systemd/system")
    bootstrap = "[Unit]\nDescription=Sub2API 出口启动默认闭锁\nAfter=local-fs.target systemd-modules-load.service\nRequiresMountsFor=/sys/fs/bpf /sys/fs/cgroup\n"
    if parents:
        bootstrap += "Requires=" + " ".join(parents) + "\nAfter=" + " ".join(parents) + "\n"
    bootstrap += f"Before=docker.service wg-quick@{node['interface']}.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={execute} egress-bootstrap {common}\n[Install]\nWantedBy=multi-user.target\n"
    guard = f"[Unit]\nDescription=Sub2API 出口持续核验与短租期守护\nRequires=sub2api-egress-bootstrap.service wg-quick@{node['interface']}.service\nAfter=sub2api-egress-bootstrap.service wg-quick@{node['interface']}.service"
    if role == "origin":
        guard += " docker.service"
    guard += f"\n[Service]\nType=simple\nExecStartPre={execute} egress-routes {common}\nExecStart={execute} egress-guard {common}\nRestart=on-failure\nRestartSec=1\nTimeoutStopSec=8\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n"
    for name, source in (("sub2api-egress-bootstrap.service", bootstrap), ("sub2api-egress-guard.service", guard)):
        egress_write_atomic(unit_root / name, source.encode("utf-8"), mode=0o644)
    dependents = [f"wg-quick@{node['interface']}.service"] + (["docker.service"] if role == "origin" else [])
    for name in dependents:
        directory = unit_root / (name + ".d")
        directory.mkdir(mode=0o755, exist_ok=True)
        egress_write_atomic(directory / "sub2api-egress.conf", b"[Unit]\nRequires=sub2api-egress-bootstrap.service\nAfter=sub2api-egress-bootstrap.service\n", mode=0o644)
    allowed = "0.0.0.0/0" if role == "origin" else str(ipaddress.IPv4Interface(peer["tunnel_ipv4"]).ip) + "/32"
    configuration = (f"[Interface]\nPrivateKey = {private}\nAddress = {node['tunnel_ipv4']}\nListenPort = {node['listen_port']}\n"
                     f"MTU = {node['mtu']}\nFwMark = {EGRESS_WG_MARK}\nTable = off\nSaveConfig = false\n\n[Peer]\nPublicKey = {peer['public_key']}\n"
                     f"Endpoint = {peer['endpoint']['ipv4']}:{peer['endpoint']['port']}\nAllowedIPs = {allowed}\nPersistentKeepalive = 15\n")
    config_path = Path("/etc/wireguard") / (node["interface"] + ".conf")
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    egress_write_atomic(config_path, configuration.encode("ascii"))
    egress_command(["systemctl", "daemon-reload"], timeout=15)
    egress_command(["systemctl", "enable", "sub2api-egress-bootstrap.service", f"wg-quick@{node['interface']}.service", "sub2api-egress-guard.service"], timeout=15)
    active = subprocess.run(["systemctl", "is-active", "--quiet", "sub2api-egress-bootstrap.service"],
                            capture_output=True, timeout=3).returncode == 0
    if active:
        # 不 restart 被 Docker Requires 的 oneshot，避免 systemd 连带停止无关容器。
        egress_bootstrap(policy_path, role, runtime_root)
    else:
        egress_command(["systemctl", "start", "sub2api-egress-bootstrap.service"], timeout=90)
    egress_command(["systemctl", "restart", f"wg-quick@{node['interface']}.service"], timeout=20)
    egress_command(["systemctl", "restart", "sub2api-egress-guard.service"], timeout=20)
    return contract._read_egress_runtime_json(runtime_root / "installed.json", private=True)


def egress_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="R15 通用出口保护运维入口；策略保留在工具树外")
    parser.add_argument("command", choices=("egress-render", "egress-build", "egress-bootstrap", "egress-routes", "egress-guard", "egress-install", "egress-check", "egress-compose"))
    parser.add_argument("--policy", type=Path, default=Path("/etc/sub2api-egress/policy.json"))
    parser.add_argument("--role", choices=("origin", "exit"), default="origin")
    parser.add_argument("--runtime-root", type=Path, default=Path("/var/lib/sub2api-egress"))
    parser.add_argument("--key", type=Path, default=Path("/etc/sub2api-egress/private.key"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--container")
    parser.add_argument("--compose-service")
    arguments = parser.parse_args(argv)
    os.umask(0o077)
    if arguments.command == "egress-build":
        result = build_egress_filter(arguments.runtime_root)
    else:
        if os.geteuid() != 0 or sys.platform != "linux":
            raise DeploymentError("实时出口运维必须在 Linux 宿主由 root 执行")
        contract = egress_contract()
        policy = contract.load_egress_policy(arguments.policy)
        if arguments.command == "egress-render":
            print(render_egress_firewall(policy, arguments.role), end="")
            return 0
        if arguments.command == "egress-compose":
            if arguments.output_dir is None or arguments.container is None or arguments.compose_service is None:
                parser.error("egress-compose 必须指定 --output-dir、--container 和 --compose-service")
            result = egress_prepare_compose(policy, arguments.output_dir, arguments.container, arguments.compose_service)
        elif arguments.command == "egress-bootstrap":
            result = egress_bootstrap(arguments.policy, arguments.role, arguments.runtime_root)
        elif arguments.command == "egress-routes":
            egress_setup_routes(policy, arguments.role)
            result = {"status": "configured"}
        elif arguments.command == "egress-install":
            result = egress_install(arguments.policy, arguments.role, arguments.runtime_root, arguments.key)
        elif arguments.command == "egress-guard":
            EgressGuard(arguments.policy, arguments.role, arguments.runtime_root).run()
            return 0
        else:
            result = contract.require_runtime_egress(arguments.policy)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_entries(root: Path) -> list[dict[str, str]]:
    """按官方编排器相同的边界生成工具文件清单。"""

    if root.is_symlink() or not root.is_dir():
        raise DeploymentError(f"工具树不是可信目录：{root}")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if (
            path.suffix not in SAFE_SUFFIXES
            or "tests" in relative.parts
            or "versions" in relative.parts
            or "__pycache__" in relative.parts
        ):
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": file_sha256(path),
            }
        )
    return entries


def tool_digest(root: Path) -> tuple[str, int]:
    entries = tool_entries(root)
    return sha256_bytes(canonical({"entries": entries})), len(entries)


def staging_identity_v2(staging_tool: Path) -> dict[str, Any]:
    """用暂存树自带的策略文件与策略模块计算五摘要中的四项（整树摘要另算）。"""

    policy_module_path = staging_tool / "codex_upgrade_tool_identity_policy.py"
    policy_path = staging_tool / "tool_identity_policy_v2.json"
    for path in (policy_module_path, policy_path):
        if path.is_symlink() or not path.is_file():
            raise DeploymentError(f"暂存树缺少工具身份策略：{path.name}")
    spec = importlib.util.spec_from_file_location("arm64_staging_tool_identity_policy", policy_module_path)
    if spec is None or spec.loader is None:
        raise DeploymentError("无法加载暂存工具身份策略模块。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        policy = module.load_policy(policy_path)
        identity = module.compute_identity_v2(policy, staging_tool, tool_entries(staging_tool))
    except Exception as error:  # noqa: BLE001 - 策略失败必须让部署失败关闭
        raise DeploymentError(f"暂存工具身份策略计算失败：{error}") from error
    return {
        "policy_version": int(identity["policy_version"]),
        "policy_sha256": str(identity["policy_sha256"]),
        "wire_producer_sha256": str(identity["wire_producer_sha256"]),
        "evidence_semantics_sha256": str(identity["evidence_semantics_sha256"]),
        "control_sha256": str(identity["control_sha256"]),
    }


def reject_untrusted_tree(root: Path) -> int:
    """拒绝符号链接、非 root 属主和 group/other 写权限。"""

    count = 0
    paths = [root, *root.rglob("*")]
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise DeploymentError(f"工具树含符号链接：{path}")
        if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
            count += 1
    if count:
        raise DeploymentError(f"工具树存在 {count} 个属主或写权限不安全项：{root}")
    return len(paths)


def normalize_tree_permissions(root: Path) -> int:
    """把新候选树统一为 root:root，并移除 group/other 写权限。"""

    count = 0
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise DeploymentError(f"候选树含符号链接：{path}")
        os.chown(path, 0, 0)
        os.chmod(path, stat.S_IMODE(metadata.st_mode) & ~0o022)
        count += 1
    return count


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_exchange(first: Path, second: Path) -> None:
    """使用 Linux renameat2(RENAME_EXCHANGE) 交换两个同类型文件系统对象。"""

    if first.is_symlink() or second.is_symlink():
        raise DeploymentError("原子交换目标不得是符号链接。")
    if not first.exists() or not second.exists():
        raise DeploymentError("原子交换目标必须存在。")
    if first.is_dir() != second.is_dir() or first.is_file() != second.is_file():
        raise DeploymentError("原子交换目标类型不一致。")
    if first.parent.stat().st_dev != second.parent.stat().st_dev:
        raise DeploymentError("原子交换目标不在同一文件系统。")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except AttributeError as error:
        raise DeploymentError("ARM64 内核/运行库不提供 renameat2。") from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        AT_FDCWD,
        os.fsencode(first),
        AT_FDCWD,
        os.fsencode(second),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    fsync_directory(first.parent)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = canonical(payload) + b"\n"
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def reject_untrusted_file(path: Path, *, label: str) -> None:
    """拒绝非 root 所有、可被其他用户写入或由符号链接替代的文件。"""

    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"{label}不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
        raise DeploymentError(f"{label}属主或写权限不安全：{path}")


def managed_document_path(
    root: Path,
    relative: str,
    *,
    label: str,
    allow_missing: bool,
) -> Path:
    """解析受管文档坐标，并拒绝任一路径组件被符号链接替代。"""

    parsed = Path(relative)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise DeploymentError(f"{label}不是规范相对路径：{relative}")
    current = root
    for index, part in enumerate(parsed.parts):
        current /= part
        if current.is_symlink():
            raise DeploymentError(f"{label}路径包含符号链接：{current}")
        if current.exists() and index < len(parsed.parts) - 1 and not current.is_dir():
            raise DeploymentError(f"{label}父路径不是目录：{current}")
        if not current.exists() and not allow_missing:
            raise DeploymentError(f"{label}缺失：{current}")
    return current


def legacy_production_document_facts(path: Path) -> dict[str, Any]:
    """冻结首次纳管的旧文档；只豁免属主，不豁免类型或写权限。"""

    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"旧生产活动文档不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_mode & 0o022:
        raise DeploymentError(f"旧生产活动文档可被组或其他用户写入：{path}")
    return {
        "sha256": file_sha256(path),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def source_spec_section_sha256(source_path: Path, fragment: str) -> str:
    """按受管场景清单的章节锚点复算活动文档摘要。"""

    expected_heading = SOURCE_SPEC_HEADINGS.get(fragment)
    if expected_heading is None:
        raise DeploymentError(f"场景清单章节锚点不受支持：{fragment}")
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as error:
        raise DeploymentError("无法读取活动规格章节。") from error
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == expected_heading
        ),
        None,
    )
    if start is None:
        raise DeploymentError(f"活动规格缺少章节：{expected_heading}")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("# ")
        ),
        len(lines),
    )
    return sha256_bytes("".join(lines[start:end]).encode("utf-8"))


def verify_scenario_source_spec(
    runtime_root: Path,
    tool_root: Path,
) -> dict[str, str]:
    """现场验证 0.151 场景清单绑定的活动文档章节。"""

    manifest_path = tool_root / TARGET_SCENARIO_MANIFEST
    reject_untrusted_file(manifest_path, label="目标场景清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("无法读取目标场景清单。") from error
    source_spec = manifest.get("source_spec") if isinstance(manifest, dict) else None
    if not isinstance(source_spec, dict):
        raise DeploymentError("目标场景清单缺少 source_spec。")
    path_text = source_spec.get("path")
    fragment = source_spec.get("fragment")
    expected_sha256 = source_spec.get("sha256")
    if (
        not isinstance(path_text, str)
        or not path_text
        or Path(path_text).is_absolute()
        or ".." in Path(path_text).parts
        or not isinstance(fragment, str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256))
    ):
        raise DeploymentError("目标场景清单 source_spec 非法。")
    source_path = runtime_root / path_text
    reject_untrusted_file(source_path, label="活动规格文档")
    actual_sha256 = source_spec_section_sha256(source_path, fragment)
    if actual_sha256 != expected_sha256:
        raise DeploymentError("活动规格第二章摘要与目标场景清单不一致。")
    return {
        "manifest": str(manifest_path),
        "source_spec": f"{path_text}#{fragment}",
        "source_spec_sha256": actual_sha256,
    }


def project_ledger_summary(supervisor: Any, data_root: Path) -> dict[str, Any] | None:
    """B9：部署收据只读记录生产数据根下项目总账 head 的两层预算事实；没有总账时为 None。"""

    ledger_module = getattr(supervisor, "project_ledger", None)
    if ledger_module is None:
        return None
    root = ledger_module.find_project_ledger(data_root / "evidence" / "campaigns")
    if root is None:
        return None
    head = ledger_module.replay_head(root)
    plan, _raw = ledger_module._load_plan(root)
    return {
        "path": str(root),
        "head_sequence": head["sequence"],
        "head_sha256": head["head_sha256"],
        "blocked": head["blocked"],
        "remaining_live_requests": head["remaining_live_requests"],
        "absolute_deadline_utc": plan["absolute_deadline_utc"],
    }


def load_supervisor(staging_root: Path) -> Any:
    module_root = staging_root / "tools" / "official_client_capture"
    module_path = module_root / "codex_upgrade_supervisor.py"
    sibling_modules = {
        "codex_upgrade_evidence_permissions": (
            "evidence_permissions",
            module_root / "codex_upgrade_evidence_permissions.py",
        ),
        "codex_upgrade_project_ledger": (
            "project_ledger",
            module_root / "codex_upgrade_project_ledger.py",
        ),
        "codex_upgrade_root_cause": (
            "root_cause",
            module_root / "codex_upgrade_root_cause.py",
        ),
        "codex_upgrade_timing_ledger": (
            "timing_ledger",
            module_root / "codex_upgrade_timing_ledger.py",
        ),
        "codex_upgrade_vc_artifacts": (
            "vc_artifacts",
            module_root / "codex_upgrade_vc_artifacts.py",
        ),
    }
    if module_path.is_symlink() or not module_path.is_file():
        raise DeploymentError("暂存监督器文件不存在或不可信。")
    for dependency_path in (item[1] for item in sibling_modules.values()):
        if dependency_path.is_symlink() or not dependency_path.is_file():
            raise DeploymentError(
                f"暂存监督器同目录依赖不存在或不可信：{dependency_path.name}"
            )
    spec = importlib.util.spec_from_file_location("arm64_staging_supervisor", module_path)
    if spec is None or spec.loader is None:
        raise DeploymentError("无法加载暂存监督器。")
    module = importlib.util.module_from_spec(spec)
    # 监督器以脚本方式运行时会从同目录导入直接依赖。部署器用 importlib 加载
    # 暂存副本时也必须精确复现这条搜索路径，同时隔离进程内可能缓存的同名旧模块，
    # 否则会在生产切换前误载旧依赖或直接报 ModuleNotFoundError。
    original_sys_path = list(sys.path)
    previous_modules = {
        name: sys.modules[name]
        for name in sibling_modules
        if name in sys.modules
    }
    for name in sibling_modules:
        sys.modules.pop(name, None)
    try:
        sys.path.insert(0, str(module_root))
        spec.loader.exec_module(module)
        for attribute, dependency_path in sibling_modules.values():
            loaded_dependency = getattr(module, attribute, None)
            loaded_dependency_file = getattr(loaded_dependency, "__file__", None)
            if (
                not isinstance(loaded_dependency_file, str)
                or Path(loaded_dependency_file).resolve()
                != dependency_path.resolve()
            ):
                raise DeploymentError(
                    f"暂存监督器未绑定同目录依赖：{dependency_path.name}"
                )
    finally:
        sys.path[:] = original_sys_path
        for name in sibling_modules:
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
    return module


def run_checked(
    client: Any,
    operation: str,
    argv: list[str],
    *,
    timeout_seconds: float = 120.0,
) -> str:
    result = client.run_command(
        argv,
        operation=operation,
        timeout_seconds=timeout_seconds,
        capture_output=True,
        text=True,
        merge_stderr=True,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        raise DeploymentError(f"{operation} 返回码 {result.returncode}")
    return output


def record_step(
    client: Any,
    operation: str,
    action: Callable[[], Mapping[str, Any] | None],
) -> dict[str, Any]:
    client.event_start(operation)
    try:
        result = dict(action() or {})
    except BaseException as error:
        try:
            client.event_fail(operation, reason=type(error).__name__)
        except BaseException:
            pass
        raise
    client.event_end(operation, metadata=_bounded_event_metadata(result))
    return result


def _bounded_event_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """把超长数组压成可复算摘要，避免部署规模增长击穿监督器事件上限。"""

    def compact(item: Any) -> Any:
        if isinstance(item, list):
            if len(item) > 32:
                return {
                    "item_count": len(item),
                    "items_sha256": sha256_bytes(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                }
            return [compact(child) for child in item]
        if isinstance(item, Mapping):
            return {str(key): compact(child) for key, child in item.items()}
        return item

    return {str(key): compact(child) for key, child in value.items()}


def parse_container_network(output: str, name: str) -> str:
    try:
        networks = json.loads(output)
    except json.JSONDecodeError as error:
        raise DeploymentError(f"无法解析 {name} 容器网络事实。") from error
    if not isinstance(networks, dict):
        raise DeploymentError(f"{name} 容器网络事实不是对象。")
    addresses = {
        str(value.get("IPAddress"))
        for value in networks.values()
        if isinstance(value, dict) and value.get("IPAddress")
    }
    if name == "capture-cli" and "172.30.0.10" not in addresses:
        raise DeploymentError(f"capture-cli 固定 IP 漂移：{sorted(addresses)}")
    if name == "sub2apiplus" and "172.25.0.3" not in addresses:
        raise DeploymentError(f"sub2apiplus 固定 IP 漂移：{sorted(addresses)}")
    return ",".join(sorted(addresses))


def verify_runtime_egress() -> dict[str, Any]:
    """部署前后都读取当前受限策略和持续守护，不冻结服务商、出口地址或链路参数。"""

    value = egress_contract().require_runtime_egress()
    return {"policy_sha256": value["policy_sha256"],
            "status_sha256": sha256_bytes(canonical(value["runtime"])),
            "nodes": value["policy"]["nodes"],
            "services": sorted(value["runtime"]["services"])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/staging/codex-0.154.0-managed-tools"),
    )
    parser.add_argument(
        "--production-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/tools/official_client_capture"),
    )
    parser.add_argument(
        "--production-doc-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/docs"),
    )
    parser.add_argument(
        "--control-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/control"),
    )
    parser.add_argument("--expected-tool-digest", default=None, help="可选；给定时暂存树摘要必须等于它，省略时以暂存树自算摘要为准")
    parser.add_argument("--expected-supervisor-digest", default=DEFAULT_SUPERVISOR_DIGEST)
    parser.add_argument(
        "--expected-assertion-preparer-digest",
        default=DEFAULT_ASSERTION_PREPARER_DIGEST,
    )
    parser.add_argument("--max-wall-seconds", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    selected = list(sys.argv[1:] if argv is None else argv)
    if selected and selected[0].startswith("egress-"):
        return egress_main(selected)
    arguments = build_parser().parse_args(argv)
    staging_root = arguments.staging_root.resolve(strict=True)
    production_root = arguments.production_root.resolve(strict=True)
    production_assertion_preparer = production_root.parent / MANAGED_ASSERTION_PREPARER
    production_doc_root = arguments.production_doc_root.resolve(strict=True)
    control_root = arguments.control_root.resolve(strict=True)
    if os.geteuid() != 0:
        raise DeploymentError("ARM64 启用必须由 root 执行。")
    if platform.machine() != "aarch64":
        raise DeploymentError("启用脚本只能在 ARM64 主机执行。")
    if control_root.is_symlink() or not control_root.is_dir():
        raise DeploymentError("控制目录不可信。")
    if stat.S_IMODE(control_root.stat().st_mode) != 0o700 or control_root.stat().st_uid != 0:
        raise DeploymentError("控制目录必须是 root 拥有的 0700 目录。")
    staging_tool = staging_root / "tools" / "official_client_capture"
    if not staging_tool.is_symlink() and staging_tool.is_dir():
        staging_digest, _staging_count = tool_digest(staging_tool)
        if arguments.expected_tool_digest is None:
            arguments.expected_tool_digest = staging_digest
        elif arguments.expected_tool_digest != staging_digest:
            raise DeploymentError(f"暂存工具摘要与 --expected-tool-digest 不符：{staging_digest}")
        arguments.identity_v2 = staging_identity_v2(staging_tool)
    if staging_tool.is_symlink() or not staging_tool.is_dir():
        raise DeploymentError("暂存工具树不存在或不可信。")
    if production_root.is_symlink() or not production_root.is_dir():
        raise DeploymentError("生产工具树不存在或不可信。")
    supervisor = load_supervisor(staging_root)
    stamp = utc_stamp().lower()
    campaign_id = f"c0154-supervisor-enable-{stamp}-{secrets.token_hex(4)}"
    attached_client = supervisor.SupervisorClient.attach_from_environment()
    client = attached_client or supervisor.SupervisorClient(
        control_root,
        campaign_id=campaign_id,
        phase="bootstrap",
        deadline_at_epoch=time.time() + float(arguments.max_wall_seconds),
        heartbeat_seconds=5,
        watchdog_timeout_seconds=20,
        ledger_interval_seconds=60,
        terminate_owner=False,
    )
    candidate: Path | None = None
    backup: Path | None = None
    assertion_preparer_candidate: Path | None = None
    assertion_preparer_backup: Path | None = None
    transaction_root: Path | None = None
    switched_documents: list[str] = []
    switched_archived_documents: list[str] = []
    installed_documents: list[str] = []
    tool_switched = False
    assertion_preparer_switched = False
    receipt_path = control_root / f"codex-0154-supervisor-enable-{stamp}.json"

    def switch_tool_tree() -> Mapping[str, Any]:
        """在事件结束写入失败时也保留已经发生的交换状态。"""

        nonlocal candidate, tool_switched
        if candidate is None or backup is None:
            raise DeploymentError("工具树候选或回滚坐标缺失。")
        result = _exchange_and_backup(candidate, production_root, backup)
        tool_switched = True
        candidate = None
        return result

    def switch_assertion_preparer() -> Mapping[str, Any]:
        """切换后立即置位，确保后续事件写入失败仍会共同回滚。"""

        nonlocal assertion_preparer_candidate, assertion_preparer_switched
        if assertion_preparer_candidate is None or assertion_preparer_backup is None:
            raise DeploymentError("assertion bundle 入口候选或回滚坐标缺失。")
        result = _switch_assertion_preparer(
            assertion_preparer_candidate,
            production_assertion_preparer,
            assertion_preparer_backup,
        )
        assertion_preparer_switched = True
        assertion_preparer_candidate = None
        return result

    # campaign-run 子进程复用父监督器；独立调用才创建并结束自己的监督器。
    with (nullcontext(client) if client.attached else client):
        try:
            record_step(
                client,
                "enable:preflight",
                lambda: _preflight(
                    client,
                    staging_root,
                    staging_tool,
                    production_root,
                    production_doc_root,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            candidate = control_root / (
                f"codex-0154-tool-candidate-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:copy-candidate",
                lambda: _copy_candidate(staging_tool, candidate),
            )
            record_step(
                client,
                "enable:normalize-candidate",
                lambda: {"entry_count": normalize_tree_permissions(candidate)},
            )
            record_step(
                client,
                "enable:verify-candidate",
                lambda: _verify_candidate(
                    candidate,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                ),
            )
            assertion_preparer_candidate = control_root / (
                f"assertion-preparer-candidate-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:prepare-assertion-preparer",
                lambda: _prepare_assertion_preparer_candidate(
                    staging_root,
                    assertion_preparer_candidate,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            transaction_root = production_doc_root / (
                f".codex-0154-deploy-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:prepare-document-candidates",
                lambda: _prepare_document_candidates(
                    staging_root / "docs",
                    production_doc_root,
                    transaction_root,
                ),
            )
            backup = control_root / (
                # 生产目录当前可能已经是某个 0.151 工具树；备份名不能把未知
                # 的旧树伪装成 0.149.1，具体版本由既有启用收据和摘要确定。
                f"managed-tools-backup-before-{arguments.expected_tool_digest[:12]}-"
                f"{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:atomic-exchange",
                switch_tool_tree,
            )
            assertion_preparer_backup = control_root / (
                f"assertion-preparer-backup-before-"
                f"{arguments.expected_assertion_preparer_digest[:12]}-"
                f"{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:switch-assertion-preparer",
                switch_assertion_preparer,
            )
            record_step(
                client,
                "enable:switch-documents",
                lambda: _switch_documents(
                    transaction_root,
                    production_doc_root,
                    switched_documents,
                    switched_archived_documents,
                    installed_documents=installed_documents,
                ),
            )
            record_step(
                client,
                "enable:post-switch-verify",
                lambda: _post_switch_verify(
                    client,
                    staging_root,
                    production_root,
                    production_doc_root,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            ledger_summary = record_step(
                client,
                "enable:read-project-ledger",
                lambda: project_ledger_summary(supervisor, control_root.parent),
            )
            receipt = {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "campaign_id": campaign_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "architecture": platform.machine(),
                "production_tool_root": str(production_root),
                "production_doc_root": str(production_doc_root),
                "tool_files_sha256": arguments.expected_tool_digest,
                "policy_version": arguments.identity_v2["policy_version"],
                "policy_sha256": arguments.identity_v2["policy_sha256"],
                "wire_producer_sha256": arguments.identity_v2["wire_producer_sha256"],
                "evidence_semantics_sha256": arguments.identity_v2["evidence_semantics_sha256"],
                "control_sha256": arguments.identity_v2["control_sha256"],
                "supervisor_sha256": arguments.expected_supervisor_digest,
                "assertion_preparer_sha256": (
                    arguments.expected_assertion_preparer_digest
                ),
                "rollback_backup": str(backup) if backup else None,
                "assertion_preparer_rollback_backup": (
                    str(assertion_preparer_backup)
                    if assertion_preparer_backup
                    else None
                ),
                "document_rollback_backup": (
                    str(transaction_root / "backups") if transaction_root else None
                ),
                "switched_archived_documents": list(switched_archived_documents),
                "installed_runtime_documents": list(installed_documents),
                "supervisor_run_dir": str(client.run_dir),
                # B9：项目总账 head 摘要；生产数据根下没有总账时记录为 null。
                "project_ledger": ledger_summary or None,
            }
            record_step(
                client,
                "enable:write-receipt",
                lambda: (
                    write_json_atomic(receipt_path, receipt)
                    or {"bytes": receipt_path.stat().st_size}
                ),
            )
        except BaseException:
            # 必须在监督器仍运行时记录回滚；不能等上下文退出后再补写。
            if (
                (
                    tool_switched
                    or assertion_preparer_switched
                    or switched_documents
                    or installed_documents
                )
                and backup is not None
                and backup.is_dir()
                and production_root.is_dir()
            ):
                try:
                    record_step(
                        client,
                        "enable:rollback",
                        lambda: _rollback_deployment(
                            backup,
                            production_root,
                            transaction_root,
                            production_doc_root,
                            switched_documents,
                            switched_archived_documents,
                            installed_documents=installed_documents,
                            auxiliary_backup=assertion_preparer_backup,
                            auxiliary_production=production_assertion_preparer,
                            auxiliary_switched=assertion_preparer_switched,
                        ),
                    )
                except BaseException:
                    pass
            raise
    print(
        json.dumps(
            {
                "status": "passed",
                "campaign_id": campaign_id,
                "receipt": str(receipt_path),
                "supervisor_run_dir": str(client.run_dir),
                "rollback_backup": str(backup) if backup else None,
                "assertion_preparer_rollback_backup": (
                    str(assertion_preparer_backup)
                    if assertion_preparer_backup
                    else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _preflight(
    client: Any,
    staging_root: Path,
    staging_tool: Path,
    production_root: Path,
    production_doc_root: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
    expected_assertion_preparer_digest: str,
) -> dict[str, Any]:
    staging_digest, staging_count = tool_digest(staging_tool)
    if staging_digest != expected_tool_digest:
        raise DeploymentError(f"暂存工具摘要不符：{staging_digest}")
    if file_sha256(staging_tool / "codex_upgrade_supervisor.py") != expected_supervisor_digest:
        raise DeploymentError("暂存监督器摘要不符。")
    reject_untrusted_tree(staging_tool)
    reject_untrusted_tree(production_root)
    staging_assertion_preparer = (
        staging_root / "tools" / MANAGED_ASSERTION_PREPARER
    )
    production_assertion_preparer = (
        production_root.parent / MANAGED_ASSERTION_PREPARER
    )
    reject_untrusted_file(
        staging_assertion_preparer,
        label="暂存 assertion bundle 入口",
    )
    reject_untrusted_file(
        production_assertion_preparer,
        label="生产 assertion bundle 入口",
    )
    if file_sha256(staging_assertion_preparer) != expected_assertion_preparer_digest:
        raise DeploymentError("暂存 assertion bundle 入口摘要不符。")
    if production_doc_root.is_symlink() or not production_doc_root.is_dir():
        raise DeploymentError("生产活动文档目录不存在或不可信。")
    if production_doc_root.stat().st_uid != 0 or production_doc_root.stat().st_mode & 0o022:
        raise DeploymentError("生产活动文档目录属主或写权限不安全。")
    legacy_document_facts = {
        name: legacy_production_document_facts(production_doc_root / name)
        for name in MANAGED_DOCUMENTS
    }
    legacy_runtime_document_facts: list[dict[str, Any]] = []
    for name in MANAGED_RUNTIME_DOCUMENTS:
        production_document = managed_document_path(
            production_doc_root,
            name,
            label="生产运行时依赖文档",
            allow_missing=True,
        )
        legacy_runtime_document_facts.append(
            (
                {
                    "path": name,
                    "status": "present",
                    **legacy_production_document_facts(production_document),
                }
                if production_document.exists()
                else {"path": name, "status": "absent"}
            )
        )
    capture_network = parse_container_network(
        run_checked(
            client,
            "enable:inspect-capture-network",
            ["docker", "inspect", "capture-cli", "--format", "{{json .NetworkSettings.Networks}}"],
        ),
        "capture-cli",
    )
    service_network = parse_container_network(
        run_checked(
            client,
            "enable:inspect-service-network",
            ["docker", "inspect", "sub2apiplus", "--format", "{{json .NetworkSettings.Networks}}"],
        ),
        "sub2apiplus",
    )
    rules = run_checked(client, "enable:inspect-policy", ["ip", "-4", "rule", "show"])
    routes = run_checked(client, "enable:inspect-route", ["ip", "-4", "route", "show", "table", "51830"])
    if "172.30.0.10" not in rules or "172.25.0.3" not in rules or "default dev wg1" not in routes:
        raise DeploymentError("固定 BWG 出口路由策略不完整。")
    wireguard = verify_runtime_egress()
    repository_docs = staging_root / "docs" / "repository-docs"
    if repository_docs.is_symlink() or not repository_docs.is_dir():
        raise DeploymentError("暂存文档归档目录不存在或不可信。")
    document_sha256: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    for name in MANAGED_DOCUMENTS:
        runtime_document = staging_root / "docs" / name
        archived_document = repository_docs / name
        if (
            runtime_document.is_symlink()
            or archived_document.is_symlink()
            or not runtime_document.is_file()
            or not archived_document.is_file()
            or file_sha256(runtime_document) != file_sha256(archived_document)
        ):
            raise DeploymentError(f"暂存文档运行副本与归档副本不一致：{name}")
        reject_untrusted_file(runtime_document, label="暂存活动文档")
        reject_untrusted_file(archived_document, label="暂存归档文档")
        document_sha256[name] = file_sha256(runtime_document)
    for name in MANAGED_RUNTIME_DOCUMENTS:
        runtime_document = managed_document_path(
            staging_root / "docs",
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(runtime_document, label="暂存运行时依赖文档")
        runtime_document_bindings.append(
            {"path": name, "sha256": file_sha256(runtime_document)}
        )
    source_spec = verify_scenario_source_spec(staging_root, staging_tool)
    return {
        "staging_file_count": staging_count,
        "staging_tool_sha256": staging_digest,
        "staging_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "capture_ip": capture_network,
        "service_ip": service_network,
        "network_policy": "fixed-bwg",
        "wireguard": wireguard,
        "document_sha256": document_sha256,
        "runtime_document_bindings": runtime_document_bindings,
        "scenario_source_spec": source_spec,
        "legacy_production_documents": legacy_document_facts,
        "legacy_runtime_documents": legacy_runtime_document_facts,
    }


def _copy_candidate(source: Path, destination: Path) -> Mapping[str, Any]:
    if destination.exists() or destination.is_symlink():
        raise DeploymentError("候选目录已存在，拒绝覆盖。")
    shutil.copytree(source, destination, symlinks=True)
    return {"candidate": destination.name}


def _prepare_assertion_preparer_candidate(
    staging_root: Path,
    candidate: Path,
    expected_digest: str,
) -> Mapping[str, Any]:
    """在 control 文件系统准备主工具树外的 bundle 入口候选。"""

    source = staging_root / "tools" / MANAGED_ASSERTION_PREPARER
    reject_untrusted_file(source, label="暂存 assertion bundle 入口")
    if file_sha256(source) != expected_digest:
        raise DeploymentError("暂存 assertion bundle 入口摘要不符。")
    if candidate.exists() or candidate.is_symlink():
        raise DeploymentError("assertion bundle 入口候选已存在。")
    shutil.copyfile(source, candidate)
    os.chown(candidate, 0, 0)
    os.chmod(candidate, 0o755)
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    fsync_directory(candidate.parent)
    if file_sha256(candidate) != expected_digest:
        raise DeploymentError("assertion bundle 入口候选复制后摘要漂移。")
    return {
        "candidate": candidate.name,
        "sha256": expected_digest,
    }


def _switch_assertion_preparer(
    candidate: Path,
    production: Path,
    backup: Path,
) -> Mapping[str, Any]:
    """原子切换外层 bundle 入口，并保留同文件系统回滚副本。"""

    reject_untrusted_file(candidate, label="assertion bundle 入口候选")
    reject_untrusted_file(production, label="生产 assertion bundle 入口")
    if backup.exists() or backup.is_symlink():
        raise DeploymentError("assertion bundle 入口回滚副本已存在。")
    exchanged = False
    try:
        atomic_exchange(candidate, production)
        exchanged = True
        os.rename(candidate, backup)
        os.chown(backup, 0, 0)
        os.chmod(backup, stat.S_IMODE(backup.stat().st_mode) & ~0o022)
        fsync_directory(backup.parent)
    except BaseException:
        if exchanged and candidate.exists() and production.exists():
            try:
                atomic_exchange(candidate, production)
            except BaseException:
                pass
        raise
    return {"backup": backup.name}


def _verify_candidate(
    candidate: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
) -> Mapping[str, Any]:
    digest, count = tool_digest(candidate)
    if digest != expected_tool_digest:
        raise DeploymentError(f"候选工具摘要不符：{digest}")
    if file_sha256(candidate / "codex_upgrade_supervisor.py") != expected_supervisor_digest:
        raise DeploymentError("候选监督器摘要不符。")
    reject_untrusted_tree(candidate)
    return {"candidate_file_count": count, "candidate_tool_sha256": digest}


def _prepare_document_candidates(
    staging_doc_root: Path,
    production_doc_root: Path,
    transaction_root: Path,
) -> Mapping[str, Any]:
    """在活动文档同一文件系统内准备候选和回滚目录。"""

    if transaction_root.exists() or transaction_root.is_symlink():
        raise DeploymentError("文档事务目录已存在，拒绝覆盖。")
    candidate_root = transaction_root / "candidates"
    candidate_archive_root = candidate_root / "repository-docs"
    backup_root = transaction_root / "backups"
    candidate_root.mkdir(mode=0o700, parents=True)
    candidate_archive_root.mkdir(mode=0o700)
    backup_root.mkdir(mode=0o700)
    os.chown(transaction_root, 0, 0)
    os.chmod(transaction_root, 0o700)
    os.chown(candidate_root, 0, 0)
    os.chmod(candidate_root, 0o700)
    os.chown(candidate_archive_root, 0, 0)
    os.chmod(candidate_archive_root, 0o700)
    os.chown(backup_root, 0, 0)
    os.chmod(backup_root, 0o700)
    digests: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    for name in MANAGED_DOCUMENTS:
        source = staging_doc_root / name
        production = production_doc_root / name
        reject_untrusted_file(source, label="暂存活动文档")
        legacy_production_document_facts(production)
        destination = candidate_root / name
        shutil.copyfile(source, destination)
        archive_destination = candidate_archive_root / name
        shutil.copyfile(source, archive_destination)
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o644)
        os.chown(archive_destination, 0, 0)
        os.chmod(archive_destination, 0o644)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        with archive_destination.open("rb") as stream:
            os.fsync(stream.fileno())
        if file_sha256(destination) != file_sha256(source):
            raise DeploymentError(f"文档候选复制后摘要漂移：{name}")
        if file_sha256(archive_destination) != file_sha256(source):
            raise DeploymentError(f"文档归档候选复制后摘要漂移：{name}")
        digests[name] = file_sha256(destination)
    for name in MANAGED_RUNTIME_DOCUMENTS:
        source = managed_document_path(
            staging_doc_root,
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(source, label="暂存运行时依赖文档")
        destination = candidate_root / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(destination.parent, 0, 0)
        os.chmod(destination.parent, 0o700)
        shutil.copyfile(source, destination)
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o644)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        if file_sha256(destination) != file_sha256(source):
            raise DeploymentError(f"运行时依赖文档候选复制后摘要漂移：{name}")
        runtime_document_bindings.append(
            {"path": name, "sha256": file_sha256(destination)}
        )
    fsync_directory(candidate_archive_root)
    fsync_directory(candidate_root)
    fsync_directory(backup_root)
    fsync_directory(transaction_root)
    fsync_directory(production_doc_root)
    return {
        "transaction_root": transaction_root.name,
        "document_sha256": digests,
        "runtime_document_bindings": runtime_document_bindings,
    }


def _switch_documents(
    transaction_root: Path,
    production_doc_root: Path,
    switched_documents: list[str],
    switched_archived_documents: list[str] | None = None,
    *,
    installed_documents: list[str] | None = None,
) -> Mapping[str, Any]:
    """在同一事务中交换活动文档和归档副本，并保存回滚坐标。"""

    candidate_root = transaction_root / "candidates"
    candidate_archive_root = candidate_root / "repository-docs"
    backup_root = transaction_root / "backups"
    installed = installed_documents if installed_documents is not None else []
    for name in (*MANAGED_DOCUMENTS, *MANAGED_RUNTIME_DOCUMENTS):
        candidate = managed_document_path(
            candidate_root,
            name,
            label="文档候选",
            allow_missing=False,
        )
        production = managed_document_path(
            production_doc_root,
            name,
            label="生产文档",
            allow_missing=True,
        )
        backup = managed_document_path(
            backup_root,
            name,
            label="文档回滚副本",
            allow_missing=True,
        )
        if backup.exists() or backup.is_symlink():
            raise DeploymentError(f"文档回滚副本已存在：{name}")
        production.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(production.parent, 0, 0)
        os.chmod(production.parent, 0o700)
        os.chown(backup.parent, 0, 0)
        os.chmod(backup.parent, 0o700)
        if not production.exists():
            os.rename(candidate, production)
            installed.append(name)
            fsync_directory(candidate.parent)
            fsync_directory(production.parent)
            continue
        exchanged = False
        try:
            atomic_exchange(candidate, production)
            exchanged = True
            os.rename(candidate, backup)
            fsync_directory(candidate_root)
            fsync_directory(backup_root)
            switched_documents.append(name)
        except BaseException:
            if exchanged and candidate.exists() and production.exists():
                try:
                    atomic_exchange(candidate, production)
                except BaseException:
                    pass
            raise
    archived = (
        switched_archived_documents
        if switched_archived_documents is not None
        else []
    )
    production_archive_root = production_doc_root / "repository-docs"
    if production_archive_root.is_dir() and candidate_archive_root.is_dir():
        backup_archive_root = backup_root / "repository-docs"
        backup_archive_root.mkdir(mode=0o700, exist_ok=False)
        os.chown(backup_archive_root, 0, 0)
        os.chmod(backup_archive_root, 0o700)
        for name in MANAGED_DOCUMENTS:
            candidate = candidate_archive_root / name
            production = production_archive_root / name
            backup = backup_archive_root / name
            if backup.exists() or backup.is_symlink():
                raise DeploymentError(f"归档文档回滚副本已存在：{name}")
            exchanged = False
            try:
                atomic_exchange(candidate, production)
                exchanged = True
                os.rename(candidate, backup)
                os.chown(backup, 0, 0)
                os.chmod(backup, 0o644)
                fsync_directory(candidate_archive_root)
                fsync_directory(backup_archive_root)
                archived.append(name)
            except BaseException:
                if exchanged and candidate.exists() and production.exists():
                    try:
                        atomic_exchange(candidate, production)
                    except BaseException:
                        pass
                raise
    return {
        "switched_documents": list(switched_documents),
        "installed_documents": list(installed),
        "switched_archived_documents": list(archived),
        "backup_root": str(backup_root),
    }


def _exchange_and_backup(
    candidate: Path,
    production: Path,
    backup: Path,
) -> Mapping[str, Any]:
    if backup.exists() or backup.is_symlink():
        raise DeploymentError("回滚备份路径已存在，拒绝覆盖。")
    exchanged = False
    backup_ready = False
    try:
        atomic_exchange(candidate, production)
        exchanged = True
        os.rename(candidate, backup)
        backup_ready = True
        os.chown(backup, 0, 0)
        os.chmod(backup, stat.S_IMODE(backup.stat().st_mode) & ~0o022)
        fsync_directory(backup.parent)
    except BaseException:
        # 交换或备份阶段出错时，尽力恢复旧目录；失败树仍保留在可识别路径。
        try:
            if backup_ready and backup.is_dir() and production.is_dir():
                atomic_exchange(backup, production)
            elif exchanged and candidate.is_dir() and production.is_dir():
                atomic_exchange(candidate, production)
        except BaseException:
            pass
        raise
    return {"backup": backup.name}


def _post_switch_verify(
    client: Any,
    staging_root: Path,
    production: Path,
    production_doc_root: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
    expected_assertion_preparer_digest: str,
) -> Mapping[str, Any]:
    digest, count = tool_digest(production)
    if digest != expected_tool_digest:
        raise DeploymentError(f"生产工具摘要不符：{digest}")
    supervisor_path = production / "codex_upgrade_supervisor.py"
    if file_sha256(supervisor_path) != expected_supervisor_digest:
        raise DeploymentError("生产监督器摘要不符。")
    assertion_preparer = production.parent / MANAGED_ASSERTION_PREPARER
    reject_untrusted_file(assertion_preparer, label="生产 assertion bundle 入口")
    if file_sha256(assertion_preparer) != expected_assertion_preparer_digest:
        raise DeploymentError("生产 assertion bundle 入口摘要不符。")
    document_sha256: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    repository_docs = staging_root / "docs" / "repository-docs"
    production_repository_docs = production_doc_root / "repository-docs"
    if not production_repository_docs.is_dir():
        raise DeploymentError("生产文档归档目录不存在或不可信。")
    for name in MANAGED_DOCUMENTS:
        production_document = production_doc_root / name
        staging_document = staging_root / "docs" / name
        archived_document = repository_docs / name
        production_archived_document = production_repository_docs / name
        reject_untrusted_file(production_document, label="生产活动文档")
        reject_untrusted_file(production_archived_document, label="生产归档文档")
        production_sha256 = file_sha256(production_document)
        if (
            production_sha256 != file_sha256(staging_document)
            or production_sha256 != file_sha256(archived_document)
            or production_sha256 != file_sha256(production_archived_document)
        ):
            raise DeploymentError(f"生产活动文档与暂存/归档副本不一致：{name}")
        document_sha256[name] = production_sha256
    for name in MANAGED_RUNTIME_DOCUMENTS:
        production_document = managed_document_path(
            production_doc_root,
            name,
            label="生产运行时依赖文档",
            allow_missing=False,
        )
        staging_document = managed_document_path(
            staging_root / "docs",
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(production_document, label="生产运行时依赖文档")
        production_sha256 = file_sha256(production_document)
        if production_sha256 != file_sha256(staging_document):
            raise DeploymentError(f"生产运行时依赖文档与暂存副本不一致：{name}")
        runtime_document_bindings.append(
            {"path": name, "sha256": production_sha256}
        )
    source_spec = verify_scenario_source_spec(
        production_doc_root.parent,
        production,
    )
    wireguard = verify_runtime_egress()
    run_checked(
        client,
        "enable:compile-production-supervisor",
        ["python3", "-m", "py_compile", str(supervisor_path)],
    )
    run_checked(
        client,
        "enable:check-assertion-preparer",
        ["bash", "-n", str(assertion_preparer)],
    )
    version = run_checked(
        client,
        "enable:supervisor-help",
        ["python3", str(supervisor_path), "--help"],
    )
    container_probe = (
        "import hashlib,json,pathlib,stat;"
        "p=pathlib.Path('/root/oauth-capture/tools/official_client_capture/capture.py');"
        "s=p.stat();"
        "print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
        "'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}))"
    )
    container_output = run_checked(
        client,
        "enable:verify-container-tool",
        ["docker", "exec", "capture-cli", "python3", "-c", container_probe],
    )
    try:
        container_payload = json.loads(container_output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise DeploymentError("容器内工具校验输出无法解析。") from error
    if not isinstance(container_payload, dict) or container_payload.get("uid") != 0 or container_payload.get("gid") != 0 or int(container_payload.get("mode", 0)) & 0o022:
        raise DeploymentError("容器内抓包执行源属主或权限不安全。")
    run_checked(
        client,
        "enable:verify-0154-binary",
        ["docker", "exec", "capture-cli", "/opt/codex-0.154.0/bin/codex", "--version"],
    )
    return {
        "production_file_count": count,
        "production_tool_sha256": digest,
        "production_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "container_capture_sha256": container_payload.get("sha256"),
        "supervisor_help_bytes": len(version.encode("utf-8")),
        "wireguard": wireguard,
        "document_sha256": document_sha256,
        "runtime_document_bindings": runtime_document_bindings,
        "scenario_source_spec": source_spec,
    }


def _rollback_deployment(
    backup: Path,
    production: Path,
    transaction_root: Path | None,
    production_doc_root: Path,
    switched_documents: list[str],
    switched_archived_documents: list[str] | None = None,
    *,
    installed_documents: list[str] | None = None,
    auxiliary_backup: Path | None = None,
    auxiliary_production: Path | None = None,
    auxiliary_switched: bool = False,
) -> Mapping[str, Any]:
    """尽力恢复全部文档和工具；单项失败不得阻止其余回滚。"""

    failures: list[str] = []
    restored_documents: list[str] = []
    removed_installed_documents: list[str] = []
    restored_archived_documents: list[str] = []
    archived = switched_archived_documents or []
    if transaction_root is None and (switched_documents or installed_documents):
        failures.append("缺少文档事务目录")
    elif transaction_root is not None:
        backup_root = transaction_root / "backups"
        for name in reversed(switched_documents):
            try:
                atomic_exchange(backup_root / name, production_doc_root / name)
                restored_documents.append(name)
            except BaseException as error:
                failures.append(f"文档 {name}: {type(error).__name__}")
        candidate_root = transaction_root / "candidates"
        for name in reversed(installed_documents or []):
            try:
                production_document = production_doc_root / name
                candidate_document = candidate_root / name
                candidate_document.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(production_document, candidate_document)
                fsync_directory(production_document.parent)
                fsync_directory(candidate_document.parent)
                removed_installed_documents.append(name)
            except BaseException as error:
                failures.append(f"新增文档 {name}: {type(error).__name__}")
        backup_archive_root = backup_root / "repository-docs"
        production_archive_root = production_doc_root / "repository-docs"
        for name in reversed(archived):
            try:
                atomic_exchange(
                    backup_archive_root / name,
                    production_archive_root / name,
                )
                restored_archived_documents.append(name)
            except BaseException as error:
                failures.append(f"归档文档 {name}: {type(error).__name__}")
    if auxiliary_switched:
        if auxiliary_backup is None or auxiliary_production is None:
            failures.append("缺少 assertion bundle 入口回滚坐标")
        else:
            try:
                atomic_exchange(auxiliary_backup, auxiliary_production)
            except BaseException as error:
                failures.append(f"assertion bundle 入口: {type(error).__name__}")
    try:
        atomic_exchange(backup, production)
    except BaseException as error:
        failures.append(f"工具树: {type(error).__name__}")
    if failures:
        raise DeploymentError("部署回滚不完整：" + "; ".join(failures))
    return {
        "failed_tree": backup.name,
        "restored_documents": restored_documents,
        "removed_installed_documents": removed_installed_documents,
        "restored_archived_documents": restored_archived_documents,
        "restored_assertion_preparer": auxiliary_switched,
        "rollback": "completed",
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        print(f"ARM64 监督部署失败：{error}", file=sys.stderr)
        raise SystemExit(1)
