#!/usr/bin/env python3
"""在 ARM64 上受独立监督器保护地启用 Codex 0.160.0 工具和文档。

本脚本只负责受管工具和活动文档的同一部署事务，不执行官方请求，也不读取证据。
所有外部命令都经由 ``codex_upgrade_supervisor.SupervisorClient``，文件操作前后均
写入事件账本。发布后的任一校验失败时，会同时恢复旧工具和旧文档，并保留失败
的新版本以便审计。

Claude 升级 B5a-01 起，一次部署是一个多根事务：``--managed-roots`` 选择要交换的受管根
（缺省只有 ``tools/official_client_capture``），``--client`` 选择要签收据的客户端（缺省只有
codex，Codex 收据的字段、文件名与摘要口径逐字不变）。交换任何对象之前先把完整意图写入
``control/managed-deploy-<stamp>.journal`` 并 fsync；中断后再次启动时按生产路径的实际内容
判定每个对象处在哪一步，把它恢复到部署前（或在收据正文已入日志时补齐收据），从不依据
「进行到哪一步」的记忆。日志存在期间驱动 ``install.py verify`` 与 ``guard.sh`` 拒绝派发。
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import fcntl
import fnmatch
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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
from contextlib import contextmanager, nullcontext
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
# 2026-09-25（0.156.1）：新增 0.156.1 目标版本清单与 0.154.0→0.156.1 升级对，目标场景清单切到 0.156.1，
# 部署命名改为 01561。受管工具树摘要随之变化；监督器与断言预处理器未变。
# 2026-09-25（画像规则补丁 v2）：validate_profile_derivation 新增 v2（键控寻址、显式增删、多规则归属），
# 只改 control 层（wire／evidence／policy 摘要不变）。受管工具树摘要随之变化；监督器与断言预处理器未变。
# 2026-09-25（VC-1 超时清理后继）：监督器的普通恢复预览后继额外接纳 capture-official 动作执行截止后
# 在清理宽限内自行封口的父失败（control 层）。受管工具树与监督器摘要随之变化；断言预处理器未变。
# 2026-09-25（seal 断言门禁画像权威）：官方 seal 预检把目标版本整体删除的端点（select 以 data.path 钉死、
# 全部官方观测零出现）登记为延后项交 VC-2 裁决；候选 seal 预检改用本 Campaign 批准断言画像。evidence 与
# control 层变化（wire／policy 摘要不变，seal 前须追加 evaluation epoch）；受管工具树随之变化；监督器与
# 断言预处理器未变。
# 2026-09-25（VC-1 恢复复用与预览重派）：历史结果复用的逐文件依赖校验豁免低风险组件（编排器、评估器、
# 控制面、环境面，只核冻结摘要）；监督器新增“失败的零请求恢复预览以 N+1 逐字重派”后继协议。control 层
# 变化（wire／evidence／policy 摘要不变）；受管工具树与监督器摘要随之变化；断言预处理器未变。
# 2026-09-25（classify 证据语义判定）：classify 以已封存官方 attempt 的 evaluation epoch 链判定 evidence
# semantics（control 层，wire／evidence／policy 摘要不变）；受管工具树随之变化；监督器与断言预处理器未变。
# 2026-09-25（VC-1 补跑失败后继）：监督器新增“按预览真实补跑失败 → N+1 普通零请求预览”后继协议，两个重派
# 协议共用父 run 失败核验（control 层，wire／evidence／policy 摘要不变）；受管工具树与监督器摘要随之变化；
# 断言预处理器未变。
# 2026-09-25（VC-1 再次承接）：已被承接过的结果再次承接时，沿同一 Campaign 的承接链按原始执行结果验证
# 元数据（control 层，wire／evidence／policy 摘要不变）；受管工具树随之变化；监督器与断言预处理器未变。
# 2026-09-25（R16 逐作业显式依赖）：场景清单可逐作业声明 tool_dependencies，声明后依赖只按清单取摘要；
# 首个 VC-1 批次动作超时按官方作业数估算。wire 与 evidence 层变化（计划外的 wire 变化，发布认证须重签），
# control 随之变化，policy 不变；受管工具树随之变化；监督器与断言预处理器未变。
# 2026-09-25（R21 弃用标签值延后）：官方 seal 断言门禁按基线与目标两份标签声明算弃用取值，零命中且
# 限定取值全部弃用的 check 登记延后交 VC-2 裁决。evidence 与 control 层变化（wire／policy 不变）；受管
# 工具树随之变化；监督器与断言预处理器未变。
# 2026-09-26（R17 恢复预览复用复算）：reconcile-attempt 的 official 恢复预览按 resume 同一复用判定只读
# 复算，不一致不可批准。仅 control 层变化（wire／evidence／policy 不变）；受管工具树随之变化；监督器与
# 断言预处理器未变。
# 2026-09-26（R18 VC-4 修好接着跑与后段注入链）：候选审核下 VC-4 的 plan-candidate-gates／record-candidate-build
# 凭对账写入的阶段幂等重派证明在同一 revision 重开并逐字重派（编排器、对账器、计时账本、证明 schema）；监督器的
# 阶段审核后继协议放开到 VC-4；pre-A3 登记后段父失败注入链。control 层变化（wire／evidence／policy 摘要不变）；
# 受管工具树与监督器摘要随之变化；断言预处理器未变。
# 2026-09-26（R18 VC-1 录制回放链）：pre-A3 登记 vc-chain.vc1-capture 与 vc-chain.vc1-recovery-chain（0.156.1 录制官方证据
# 零请求回放，测试夹具在 tests/）。仅 control 层（pre-A3 场景表）变化，wire／evidence／policy 不变；受管工具树随之变化；
# 监督器与断言预处理器未变。
# 2026-09-26（F6：目标改为 0.157.0，放弃 0.156.1）：新增 0.157.0 目标版本清单（场景清单含 A17 daemon 路径作业，
# 全部作业声明 R16 逐作业依赖）与 0.154.0→0.157.0 升级对，目标场景清单切到 0.157.0，部署命名改为 01570（与驱动
# 按目标版本推导的前缀一致）；中继脚本新增 daemon-tui 场景、daemon 残留清扫与 drive_codex_daemon.py。wire 层
# 变化；受管工具树随之变化；监督器与断言预处理器未变。
# 2026-09-26（0.157.0 VC-1 中断暴露的两处缺陷）：字节中继为每条连接的处理任务保留强引用（修复客户端断开后挂起
# 任务被循环 GC 销毁、并在别的连接任务里误取消该连接）；本脚本 egress-guard 按租期末尾挑选与判定出口观测（修复
# 签发后租期内过期导致的采集暂停，需随出口安装包重装生效）。wire 层变化；受管工具树随之变化；监督器与断言
# 预处理器未变。
# 2026-09-26（0.157.0 VC-5 批次 9）：监督器受控维护的重新准入只采用观测起点晚于维护命令结束的守护状态（修复重启
# 后读到命令开始前发布的陈旧就绪、提前撤销维护声明，致父监督器判出口异常暂停）。control 层变化；受管工具树与
# 监督器摘要随之变化；断言预处理器未变；本脚本 egress-guard 逻辑未变，出口守护无需重装。
# 2026-09-26（0.157.0 VC-5 批次 10 编译拒绝）：reservation 前逐字重派在 b0 下只核评估器 checker／builder 摘要（修复上一
# 修复改变 compare／accept 读侧闭包后，环境前提失败无法重派的缺口）。control 层变化；受管工具树与监督器摘要随之变化；
# 断言预处理器与 egress-guard 逻辑未变。
# 2026-09-27（修好接着跑第一批）：根因上限按 Campaign 目标版本判定；VC-5 候选采集（及续跑预览／补跑）失败进入
# recovery_required，新增候选采集续跑三条后继协议（采集失败→零请求预览、预览逐字重派、补跑失败→新预览）；编排器
# 新增工具演进登记与续跑闭集，计时账本允许候选审核下 VC-5 恢复授权。control 层与 evidence 层（checker）变化，
# wire 层随 A15 采集脚本修复变化；受管工具树与监督器摘要随之变化；断言预处理器与 egress-guard 逻辑未变。
# 2026-09-27（修好接着跑第二批）：对账把预算到期、请求预算耗尽、账务无法核清、未隔离的环境污染一律判暂停并给出
# 下一步（deadline-extend／request-budget-extend／accounting-resolve／environment-isolate），campaign-resume 凭修复证据
# 恢复停线的 Campaign；监督器新增续跑后继（工具演进或环境隔离作废 attempt 的零请求恢复预览、VC-1 官方 seal 链零请求
# 失败的重派、执行前评估器漂移改为可恢复 tool-evolution-required），永久条件去掉账务 blocked 与请求预算耗尽；评估器
# 读侧口径换为 evaluator-reader-closure/v2。control 层与 evidence 层（污染判定）变化，wire 闭包不变；受管工具树与监督器
# 摘要随之变化；断言预处理器与 egress-guard 逻辑未变。
# 2026-09-27（修好接着跑第 24 项）：续跑重跑来源已完成的作业前把被其它 attempt 登记占据的固定证据根取代归档（先写取代
# 收据），旧 attempt 按收据映射重放收口；新增 evidence-conflict-quarantine 隔离已被破坏的 attempt（受损只读留档、其余
# 作废，全部重跑）；监督器作废对账核验接受证据根冲突隔离。control 层与 evidence 层变化，wire 闭包与 wire producer 不变；
# 受管工具树与监督器摘要随之变化；断言预处理器与 egress-guard 逻辑未变。
# 2026-09-27（修好接着跑第 25 项）：看门狗每轮不持锁重放三层预算，撞上动作进程写总账／账本的中间态会瞬时失败；改为自首次
# 失败起持续 15 秒才判预算状态无效，容忍期间沿用已收紧的截止。control 层变化，wire／evidence 不变；监督器摘要随之变化。
# 2026-09-27（修好接着跑第三批：R1／R3／R5 与第 9、10、12、19、22 项）：策略变化经策略演进在原 Campaign 承接；已封存证据只有
# mtime／ctime／inode 漂移可 rebind 继续，环境隔离按污染发生时刻保留污染前作业复用；评估器／证据层修好后 reevaluate 开
# tool-evolution 基线，selector 修正开 approval-revision 基线，VC-5 重开后的 checkpoint 落 reopen-b<K>；候选零请求后处理失败
# 与批次中途评估器漂移可恢复；同根因达上限只暂停、门禁只挡本次根因；总账 blocked 按 Campaign 归属；零请求预览先采探针查
# 连续性并写漂移收据；重派按批次身份而非命令全文；pre-A3 认证按工具身份复用并登记复用收据。control 层、evidence 层
# （闭包 54dbc763→3aa27c1e）与计时账本生产者变化，wire 闭包 b1e4c251 不变；受管工具树与监督器摘要随之变化；断言预处理器
# 与 egress-guard 逻辑未变，出口守护无需重装。
# 2026-09-28（修好接着跑第 26 项，B3-15）：被看门狗中止并已对账（无 reservation、零请求、总账绑定）的父 run 是可信终态，批次链
# 审计按失败终态走后继协议，续跑协议透传 Campaign 目录。control 层变化，wire／evidence 不变；监督器摘要随之变化。
# 2026-09-28（修好接着跑第 30 项，B3-19）：恢复链协议核验父动作诊断不再按错误类型白名单，改为失败种类＋execution-failure 类别，
# 显式排除清理／中断异常；补跑动作因任何工具缺陷抛出的异常修好后都能用 N+1 零请求预览承接。control 层变化，监督器摘要随之变化。
# 2026-09-28（B4-1 后继协议层失败矩阵，含第 31 项）：失败父 run 共享事实、已对账的看门狗中止等同 failed、恢复链协议要求失败父 run
# 已对账、候选后处理 execution-failure 与 canonical 动作识别、协议 8 判据、environment-prerequisite、拒因收集。control 层变化，
# 监督器摘要随之变化。
# 2026-09-28（草表 D-07／D-10、第 39／43 项）：看门狗中止却留有动作诊断按 failed 同口径承接；VC-1 官方 seal 链零请求后处理的
# 阶段幂等重派合同；断言包已发布后 seal 预览续派的门禁与协议；看门狗形态阶段审核类失败的收账补做。control 层变化，监督器摘要随之变化。
# 2026-09-28（第 46 项）：父监督器收账遇同根因重试上限按 B3-9 只暂停、不停线（永久失败类与已停线／已完成账本照旧停线）。
# control 层变化，监督器摘要随之变化。
# 2026-09-28（第 51 项）：staging 提交步骤失败时监督器先把异常类型、原文与签名写进 staging-commit-failure.json 再封存。
# control 层变化，监督器摘要随之变化。
# 2026-09-28（第 49 项）：恢复链截止类失败（WallClockTimeoutError／CampaignCleanupRequested 的 deadline-expired）由协议承接，
# VC-5 续跑链截止失败收账进 recovery_required。control 层变化，监督器摘要随之变化。
# 2026-09-28（第 48 项）：恢复段在段预约之后以截止类失败收口时与执行失败一样进 recovery_required，父收账按账本里段是否
# 仍在运行判定；候选审核下的段不再提示开后继段。control 层变化，监督器摘要随之变化。
# 2026-09-28（第 53 项）：恢复段在段预约之前失败（执行失败或截止类）收为 recovery_required，经 reconcile-supervisor-run
# 入账后由协议 15 承接同一段号重派；已预约的段仍按第 48 项。control 层变化，监督器摘要随之变化。
# 2026-09-28（第 60 项）：动作诊断升 v4，handled-error／unexpected-error 由写入方按异常原文落 es1 归一化拒因签名，
# 对账器据此编成 campaign-run.action-error（维度 phase、failure_kind、error_type、error_signature）；v1～v3 照旧读、旧 ID 不变。
# control 层变化，监督器摘要随之变化。
# 2026-10-01（0.159.2 版本登记）：新增 0.159.2 目标版本清单与 0.157.0→0.159.2 升级对，目标场景清单切到 0.159.2，
# 部署命名改为 01592（与驱动按目标版本推导的前缀一致）；场景清单按审核补充生图透明背景与 file_id 编辑、数字推理等级、
# 流内 flex_unavailable／interrupted、空闲断连与 Retry-After 定向样本。wire 层变化；受管工具树随之变化。
# 2026-10-03（E5-01，0.160.0 版本登记）：0.159.2 从未生效，六份目标版本清单改名为 0.160.0 并替换版本号与三处源码锚点，
# 升级对改为 0.157.0→0.160.0，目标场景清单切到 0.160.0，部署命名改为 01600。wire 层变化；受管工具树随之变化。
# 2026-10-03（E5 VC-5）：环境探针把服务端写回的 codex_credits_snapshot 列为托管键（control 层评估侧，监督器不变）；
# 监督器"父 run 期间预约"窗口只在回头核验历史后继链时，以该段后继 run 的开始时刻封顶（_historical_window_end），
# 后继 run 自己的预约不再算回失败前序；链尾一段与单独调用的对账入口保持原判定。control 层变化，监督器摘要随之变化。
# 2026-10-03（E5 VC-5 评估基线）：评估基线后继协议加非失败承接分支（_validate_non_failure_baseline_bridge）——失败评估批次
# 之后由 reevaluate／approval-revision 开的基线沿 previous_baseline 链承接，不再被当成跳号或缺失败诊断而拒绝；失败类基线
# 原路径不变。control 层变化，监督器摘要随之变化。
# 2026-10-05（A-07.4）：分钟账本保留 epoch 原精度，防止亚微秒首桶被取整成零长度。
# 2026-10-07（Claude 升级 B1-02）：监督器不再导入编排器，编排器符号、编排器 CLI 名、禁止子命令、评估 builder 名、
# 断言包取证清单版本字段与根因码表经客户端适配器取（Codex 取值逐字不变）；新增 Claude 清单合同
# official-client-campaign-run/v1（v2 结构加 client），后继协议与批次历史核对前后清单同属一个客户端。control 层变化，
# 监督器摘要随之变化；Codex 的 wire、evidence 与评估器摘要不变。
# 2026-10-07（Claude 升级 B5a-01）：部署改为多根事务——交换任何对象之前写意图日志（control/managed-deploy-<stamp>.journal），
# 中断后按生产路径的实际内容恢复到部署前或补齐收据；--managed-roots 选择要交换的受管根、--client 选择签收据的客户端，
# 交换 official_client_capture 根影响两个客户端、official_client_control 根只影响 Claude。Codex 收据
# codex-arm64-supervisor-enable/v1 的 22 个字段、文件名与单根三后缀摘要口径不变；Claude 另签
# official-client-managed-deploy/v1（身份由 v3 引擎按 Claude 策略计算）。受管树只有 codex_upgrade_wire_transition.py 的
# main 改经适配器取 _tool_identity（control 层，Codex 取值不变）；监督器与断言预处理器未变。
# 2026-10-07（staging 派发门禁偶发的产品侧修复）：stop 等 monitor 收口时，monitor 还没写出首条 watchdog 心跳（冷启动）
# 就最多等到父 run 起动后 30 秒，已写出的仍按 2 秒判卡住、到时处理不变（原先 no-op 批次在高负载下被误封
# audit-incomplete，引导首批成为不可信前序）；campaign-run 返回值以落盘终态为准，不一致时附 requested_terminal。
# control 层变化，监督器摘要随之变化；Codex 的 wire、evidence 与评估器摘要不变。
# 2026-10-07（修好接着跑：Claude VC-1 续跑路径）：VC-1 post-run-tooling 判据（_official_post_run_tooling_facts）与 seal 链
# 续派协议（_validate_batched_seal_chain_successor）的阶段项闭集、不绑定 attempt 的零请求后处理动作改经适配器按清单归属
# 客户端取（Codex 取原常量、判定恒为假，判定逐字不变；Claude 认封存批次三个动作，静态分析按形态精确认定）。control 层变化，
# 监督器摘要随之变化；Codex 的 wire、evidence 与评估器摘要不变。
# 2026-10-08（Claude 升级 B4-07 收尾）：候选级零请求后处理判定（candidate_post_run_recovery_action）在共享闭集之外按清单
# 客户端经适配器 candidate_post_run_extra_action 额外放行（Codex 恒为假，判定逐字不变；Claude 认 VC-6 两个交付动作）。
# control 层变化，监督器摘要随之变化；Codex 的 wire、evidence 与评估器摘要不变。
# 2026-10-08（m11 满载收尾误封修复）：stop 等 monitor 收口时，有新落盘进展就续期 2 秒；monitor 已写出本 owner 的
# stop-requested 事件时允许单步慢写，两种续期都受 stop 起算 30 秒的等待上限约束。没有进展且未接手停止请求的 monitor
# 仍沿用原等待窗口，超时封存与终止逻辑不变；本次只改 control 层，Codex 的 wire、evidence 与评估器摘要不变。
# 2026-10-08（Claude 升级 B4-05b-1）：候选续跑命令的身份参数经适配器 candidate_recovery_identity_flags 提供；Codex 保留
# 原十项及其顺序，Claude 只传 --candidate-id、候选身份由 VC-4 构建收据重放。解析、预览／补跑命令与后继协议使用同一组
# 参数；只改 control 层并登记身份守恒，wire、evidence 与评估器摘要不变。
# 2026-10-08（Claude 首批清单确定性编译）：共享清单结构规范化拆出唯一纯核心，Claude 首个 VC-1 批次的
# 内容生成与重放只走纯核心；真实派发仍经原读取入口核绑定文件和 VC-1／VC-5 门禁。Codex 继续走严格入口，
# 仅 control 层与监督器摘要变化，wire、evidence、编排器闭包和四项评估器摘要均保持原值。
# 2026-10-11（Claude 候选批准续跑）：Codex 专属 seal 预演门禁仅适用于 Codex v2 清单；
# Claude 沿原闭合、批准摘要与证据绑定校验继续执行。仅控制层与整树变化，四条读侧及采集／证据身份不变。
# 2026-10-12（Claude 原批次续作，342b19b78）：监督器新增首次候选采集预约前失败的逐字重派承接，监督器摘要变为 4c91077a；
# 部署时一直用 --expected-supervisor-digest 显式钉它，默认值没有同步。
# 2026-10-13（Claude 升级收尾）：上述承接限定为 Claude 新合同，并要求后继与前序批次身份相同（否则让位给后面的协议）；
# Codex 候选采集动作的 execution-failure 仍不走逐字重派协议，行为与此前逐字相同。仅控制层变化，监督器摘要随之变化；
# wire、evidence 与四项评估器摘要不变。默认值同步为当前树的监督器。
DEFAULT_SUPERVISOR_DIGEST = (
    "6997337257e9956f6bb83c54122ae8c4b5dbacfeedb6aaa7d1226864750a6b1d"
)
DEFAULT_ASSERTION_PREPARER_DIGEST = (
    "c8020cadd3ee08f67236313a0913dbc3730805b46c3f6b720cdf7ace77f9fec1"
)
RENAME_EXCHANGE = 2
# renameat2 的不覆盖标志：目标已存在时内核返回 EEXIST，用于「原路径不存在」的切入与回滚移回（B5a-01）。
RENAME_NOREPLACE = 1
AT_FDCWD = -100
# Codex 部署收据的整树摘要口径（单根、三后缀）。Claude 的摘要口径由 Claude 策略自带（含 .mjs、两个根），
# 不经这个常量；改它会改变 Codex 收据的 tool_files_sha256，所以 B5a-01 起也只服务 Codex。
SAFE_SUFFIXES = {".py", ".sh", ".json"}
# Codex 的活动文档。签 Codex 收据的事务交换这两份（含 repository-docs 归档副本）；Codex 收据的
# switched_archived_documents 只列它们，VC-0 收口按这份清单逐项核对（codex_upgrade_vc0_closeout 的
# EXPECTED_MANAGED_DOCUMENTS），因此不能把其他客户端的文档混进这个元组。
MANAGED_DOCUMENTS = (
    "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
)
# Claude 的活动文档（B5a-01）：Claude 指南与两客户端共用控制面的实现文档（设计 D10）。只有签 Claude 收据的事务
# 才交换它们。没有并进 MANAGED_DOCUMENTS 的原因有二：其一，修复轮驱动（fix-and-continue.sh）只为 Codex 的两份文档
# 准备 staging 归档副本，缺省 Codex 部署若要求这两份就会在预检失败；其二，上面所说的 VC-0 收口逐项核对。
# 首次纳管时生产路径不存在，意图记 old_exists=false，回滚恢复为不存在。
CLAUDE_MANAGED_DOCUMENTS = (
    "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
    "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md",
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
    # 2026-09-25：阶段 1 的 R2／R4／R8 与复审修正改过计时账本，其承接收据已登记进计时账本来源链，
    # 但此前漏登这里，部署后重放历史账本会因找不到收据而阻断；按时间顺序补齐。
    "egress/maintenance/upstream-codex-01561-r2-review-fix-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r4-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r4-review-fix-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r8-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r8-final-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-r8-review-fix-20260924-freeze-successor.json",
    "egress/maintenance/upstream-codex-01561-review2-fix-20260924-freeze-successor.json",
    # 2026-09-26：R18 改过计时账本，其承接收据登记进计时账本来源链，须随工具树部署。
    "egress/maintenance/upstream-codex-0157-r18-candidate-stage-replay-20260926-freeze-successor.json",
    "egress/maintenance/upstream-codex-0157-batch1-tool-evolution-20260927-freeze-successor.json",
    "egress/maintenance/upstream-codex-0157-batch2-fix-and-continue-20260927-freeze-successor.json",
    "egress/maintenance/upstream-codex-0157-batch3-fix-and-continue-20260927-freeze-successor.json",
    # 2026-09-27：第三批 R3 改过 codex_upgrade_evidence_manifest.py（0.151 评估恢复 addition 闭集），台账链重签的承接收据与本清单
    # 变更的承接收据都要随工具树部署，否则部署后重放历史账本会因找不到收据而阻断（先例 09-22 ledger-resign／deploy-manifest-22）。
    "egress/maintenance/upstream-codex-0157-batch3-ledger-resign-evidence-manifest-20260927-freeze-successor.json",
    "egress/maintenance/upstream-codex-0157-batch3-deploy-manifest-r12-20260927-freeze-successor.json",
    # 2026-10-07（Claude 升级批次 1 收口）：计时账本加客户端维度后的生产者承接收据；同一张收据也承接本清单这次变更。
    "egress/maintenance/upstream-claude-b1-control-plane-20261007-freeze-successor.json",
    # 2026-10-07（Claude 升级 B4-07）：计时账本加四个候选交付事件后的生产者承接收据；同一张收据也承接本清单这次变更。
    "egress/maintenance/upstream-claude-b407-delivery-ledger-20261007-freeze-successor.json",
)
# Claude 的运行时依赖文档（B5a-01 扩展点）。Codex 的这张清单存在，是因为计时账本重放历史 producer 摘要时在运行时
# 读取承接收据；Claude 计时账本的承接表目前为空（B1-05：PRODUCER_FREEZE_SUCCESSORS_BY_CLIENT["claude"] == ()），也没有
# 别的 Claude 工具在运行时从仓库根读台账，所以清单留空。以后 Claude 工具开始在运行时读某份台账时登记在这里，
# 签 Claude 收据的事务会把它与工具树放进同一事务。
CLAUDE_MANAGED_RUNTIME_DOCUMENTS: tuple[str, ...] = ()
MANAGED_ASSERTION_PREPARER = "prepare_assertion_bundle.sh"

# ---------------------------------------------------------------------------
# 多根事务与按客户端签收据（B5a-01）
# ---------------------------------------------------------------------------
# 受管根（相对数据根，与仓库内布局相同）→ 交换它会影响身份的客户端。Codex 的身份只覆盖 official_client_capture；
# Claude 的身份覆盖两个根（Claude 策略 roots）。
CAPTURE_ROOT = "tools/official_client_capture"
CONTROL_ROOT = "tools/official_client_control"
MANAGED_ROOT_CLIENTS: Mapping[str, tuple[str, ...]] = {
    CAPTURE_ROOT: ("codex", "claude"),
    CONTROL_ROOT: ("claude",),
}
DEFAULT_MANAGED_ROOTS = (CAPTURE_ROOT,)
# 签收据的顺序固定按这张表：Claude 收据要记录同一事务 Codex 收据的摘要，所以 Codex 收据必须先定稿。
DEPLOY_CLIENTS = ("codex", "claude")
DEFAULT_CLIENTS = ("codex",)
# 客户端身份覆盖的受管根：签某客户端收据时，这些根在部署后都必须等于暂存树的同名根（未交换的根要求生产现状
# 已与暂存树一致），收据才能如实描述生产身份。
CLIENT_MANAGED_ROOTS: Mapping[str, tuple[str, ...]] = {
    "codex": (CAPTURE_ROOT,),
    "claude": (CAPTURE_ROOT, CONTROL_ROOT),
}
CLIENT_MANAGED_DOCUMENTS: Mapping[str, tuple[str, ...]] = {
    "codex": MANAGED_DOCUMENTS,
    "claude": CLAUDE_MANAGED_DOCUMENTS,
}
CLIENT_MANAGED_RUNTIME_DOCUMENTS: Mapping[str, tuple[str, ...]] = {
    "codex": MANAGED_RUNTIME_DOCUMENTS,
    "claude": CLAUDE_MANAGED_RUNTIME_DOCUMENTS,
}
# 部署收据：合同、文件名模式（与适配器 deploy_receipt_glob 同值，测试核对）与文件名里时间戳之前的前缀。
# Codex 的文件名保留它自己维护的版本标签（codex-01600-…），消费者按 "-supervisor-enable-" 之后的时间戳排序。
CODEX_DEPLOY_RECEIPT_SCHEMA = "codex-arm64-supervisor-enable/v1"
CLAUDE_DEPLOY_RECEIPT_SCHEMA = "official-client-managed-deploy/v1"
DEPLOY_RECEIPT_GLOBS: Mapping[str, str] = {
    "codex": "codex-*-supervisor-enable-*.json",
    "claude": "claude-managed-deploy-*.json",
}
CODEX_RECEIPT_NAME_TEMPLATE = "codex-01600-supervisor-enable-{stamp}.json"
CLAUDE_RECEIPT_NAME_TEMPLATE = "claude-managed-deploy-{stamp}.json"
# Codex 收据的字段闭集（22 项，VC-0 收口与驱动按它核对）；B5a-01 不增不减。
CODEX_DEPLOY_RECEIPT_FIELDS = frozenset({
    "schema_version", "status", "campaign_id", "created_at_utc", "architecture", "production_tool_root",
    "production_doc_root", "tool_files_sha256", "policy_version", "policy_sha256", "wire_producer_sha256",
    "evidence_semantics_sha256", "control_sha256", "supervisor_sha256", "assertion_preparer_sha256", "rollback_backup",
    "assertion_preparer_rollback_backup", "document_rollback_backup", "switched_archived_documents",
    "installed_runtime_documents", "supervisor_run_dir", "project_ledger",
})
# Claude 收据的字段闭集。设计规定的核心是 client、identity、managed_roots、runtime_bindings、switch_journal_sha256；
# 其余是消费者需要的公共事实：status／created_at_utc（按部署顺序取最新、按时间找生效收据）、architecture 与
# supervisor_sha256（修复轮期望摘要的同名键）、documents（Claude 文档的生效摘要与回滚点）、client_binaries 与 pre_a3
# （冒烟探针与场景表的如实记录，没有目标版本或场景集合为空时写明跳过原因）、transaction_receipts（同一事务签出的
# 其它客户端收据，B5a-08／B5a-10 据此认定「双收据」属于同一次部署）、notes（事务事实与人工备注，B5a-02 登记未受管旧副本）。
CLAUDE_DEPLOY_RECEIPT_FIELDS = frozenset({
    "schema_version", "status", "client", "campaign_id", "created_at_utc", "architecture", "production_data_root",
    "production_doc_root", "identity", "managed_roots", "runtime_bindings", "switch_journal_sha256", "supervisor_sha256",
    "supervisor_run_dir", "documents", "client_binaries", "pre_a3", "transaction_receipts", "notes",
})
CLAUDE_IDENTITY_FIELDS = (
    "policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256",
    "tool_files_sha256",
)
# Claude 身份引擎（v3）与策略：随 official_client_capture 根部署，部署时用暂存树里的那一份算预期身份，切换后用
# 生产树里的那一份复算；两份字节相同（同一事务交换），复算结果必须与预期逐字相等。
CLAUDE_IDENTITY_ENGINE = "claude_tool_identity_policy.py"
CLAUDE_IDENTITY_POLICY = "claude_tool_identity_policy_v2.json"
# v3 引擎以脚本方式（无包）加载时按扁平名导入的兄弟模块；加载与调用期间都要从同一棵树解析。
CLAUDE_IDENTITY_SIBLINGS = ("claude_tool_identity_policy", "codex_upgrade_tool_identity_policy", "upgrade_client_adapter")

# 写前意图日志（B5a-01）：一个事务一份，放在 control 目录（与候选、备份、收据同一文件系统）。第一行是意图，之后
# 只追加阶段标记；正常结束删除，回滚结束改名为失败证据（不再匹配活动日志的模式，消费门禁不再拒绝）。
SWITCH_JOURNAL_SCHEMA = "managed-deploy-journal/v1"
SWITCH_JOURNAL_GLOB = "managed-deploy-*.journal"
SWITCH_JOURNAL_NAME_TEMPLATE = "managed-deploy-{stamp}.journal"
SWITCH_JOURNAL_RETIRED_SUFFIX = ".rolled-back"
SWITCH_JOURNAL_MARKERS = (
    "switch_begin", "switch_done", "verified", "receipts_prepared", "rollback_begin", "rollback_done",
)
# 同一 control 目录同时只允许一个受监督部署：启动恢复会按内容回滚它发现的日志，另一进程的进行中事务不能被当成残留。
DEPLOY_LOCK_NAME = "managed-deploy.lock"
# 生产监督器的编译检查只编译、不落盘（E2-02）：py_compile 会无视 PYTHONDONTWRITEBYTECODE 显式写 .pyc，生产受管树原先
# 因此每次部署都多一个 __pycache__。内置 compile() 与 py_compile 是同一个编译器，语法与编码声明错误同样失败。
COMPILE_CHECK_COMMAND = ("python3", "-c", "import sys; compile(open(sys.argv[1], 'rb').read(), sys.argv[1], 'exec', dont_inherit=True)")
TARGET_SCENARIO_MANIFEST = "codex_upgrade_scenarios_0_160_0.json"
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

    def keys(self, name: str, key_size: int) -> list[bytes]:
        """只读枚举已固定表的全部键，供守护按内核中的实际状态对账。"""

        self.library.bpf_map_get_next_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.library.bpf_map_get_next_key.restype = ctypes.c_int
        descriptor = self._descriptor(name)
        try:
            found: list[bytes] = []
            current: Any = None
            while True:
                following = ctypes.create_string_buffer(key_size)
                if self.library.bpf_map_get_next_key(descriptor, current, following) != 0:
                    if ctypes.get_errno() == 2:
                        return found
                    raise DeploymentError(f"出口内核表枚举失败：{name}，errno={ctypes.get_errno()}")
                key = following.raw[:key_size]
                if key in found or len(found) >= 65536:
                    raise DeploymentError(f"出口内核表枚举未收敛：{name}")
                found.append(key)
                current = ctypes.create_string_buffer(key, key_size)
        finally:
            os.close(descriptor)

    def sync_probes(self, addresses: set[str]) -> dict[str, int]:
        """探针目的表与当前解析结果对账：先删除不在当前集合的旧地址，再写入当前地址。

        BPF 表没有 nft 集合那样的元素超时；只写不删会随探针域名 IP 轮换累积到上限，之后每轮写入
        失败、业务永久闭锁。守护重启会接管已固定的旧表，因此以内核中的实际键为准，不依赖进程内记忆。
        """

        desired = {socket.inet_aton(address) for address in addresses}
        stale = [key for key in self.keys("probes", 4) if key not in desired]
        if stale:
            descriptor = self._descriptor("probes")
            try:
                for key in stale:
                    result = self.library.bpf_map_delete_elem(descriptor, ctypes.create_string_buffer(key, 4))
                    if result and ctypes.get_errno() != 2:
                        raise DeploymentError("出口探针表旧地址删除失败")
            finally:
                os.close(descriptor)
        for key in sorted(desired):
            self.update("probes", key, struct.pack("=I", 1))
        return {"removed": len(stale), "current": len(desired)}

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


def egress_container_snapshot() -> list[dict[str, Any]]:
    """列出并 inspect 当前运行中的容器；列出后消失的容器只从本轮快照剔除，不放宽任何判定。

    2026-09-26 c01570 170805z VC-4 批次 8：record-candidate-build 用候选镜像起的临时核验容器存活不到 1 秒，
    恰在 ps 与 inspect 之间销毁，inspect 对消失的 ID 返回非零，守护判共享保护失效闭锁两个受保护容器，父监督器
    随即暂停。

    重试只收缩、不扩大：inspect 失败后重新列出，与本轮快照取交集，只 inspect 仍在运行的容器。交集没有缩小
    （没有容器消失，失败另有原因）立即抛出，由守护按"容器清单不可完整核验"失败关闭；每次重试至少剔除一个
    已消失的 ID，因此在任何短命容器密度下都有限次收敛。首次列出之后才出现的容器不进入本轮快照，下一轮再按
    常规纳入（此前也是如此，纳入前默认拒绝）；受保护容器若恰在此间消失，照常只按"受保护容器尚未运行"闭锁该
    服务。不能改成"整体重新列出再试若干次"：ARM64 实测修前守护在串行 60 个短命容器、22 秒内共享保护失效
    5 次（每轮约一成撞上），整体重列在同等密度下仍有按轮累积的残余失败。
    """

    identifiers = egress_command(["docker", "ps", "-q", "--no-trunc"]).split()
    while identifiers:
        try:
            return json.loads(egress_command(["docker", "inspect", *identifiers]))
        except DeploymentError:
            running = set(egress_command(["docker", "ps", "-q", "--no-trunc"]).split())
            remaining = [identifier for identifier in identifiers if identifier in running]
            if len(remaining) == len(identifiers):
                raise
            identifiers = remaining
    return []


def egress_inventory(policy: dict[str, Any], parents: dict[str, str]) -> dict[str, Any]:
    """逐服务隔离发现错误；仅精确确认的其他容器端口进入 bypass，宿主代理从不放行。"""

    items = egress_container_snapshot()
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
                other = by_name.get(dependency["container"])
                if other is None or not other["State"].get("Running") or other["State"].get("Restarting"):
                    # 依赖容器受控重建、停止或处于 restarting 时没有可放行的地址：只暂缓这一项依赖，
                    # 本服务自身的出口准入、探针与内核租期保持不变。否则另一受保护容器的正常重建会把
                    # 本容器连带判为不合规，维护入口要求的"另一容器持续合规"永远无法成立。
                    continue
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


def egress_lease_view(service: dict[str, Any] | None) -> dict[str, Any]:
    """内核租期只由网卡、cgroup、进程周期与准入状态决定；依赖放行只在 nft 集合中生效。

    依赖容器重建会让本服务的依赖列表暂时收缩，但不能因此撤销本服务的内核租期，
    否则另一受保护容器的受控重建会让本容器短暂断网。
    """

    return {key: value for key, value in (service or {}).items() if key != "dependencies"}


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


# 探针单次请求的建连与总时长上限，以及任一来源失败后的重探间隔。跨洋链路丢包时个别来源会偶发超时，
# 有效期内最近一次成功仍能证明出口；失败来源尽快重探补齐，不等常规刷新周期。
EGRESS_PROBE_CONNECT_SECONDS = 3
EGRESS_PROBE_MAX_SECONDS = 5
EGRESS_PROBE_RETRY_SECONDS = 3


def egress_effective_observations(policy: dict[str, Any], latest: list[dict[str, Any]],
                                  passes: dict[str, dict[str, Any]], *, now_epoch: float) -> list[dict[str, Any]]:
    """逐来源选取用于准入的观测：有效期内最近一次成功优先，否则取最近一次真实结果。

    一轮中个别来源因丢包超时，不应让有效期内的成功观测失效；所有条目都是真实探针结果，不生成替代记录。
    最近一次成功若地址冲突，照样作为该来源的观测参与判定，冲突不会被更早的成功结果掩盖。
    """

    chosen = []
    for item in latest:
        best = passes.get(item["url"])
        if best is not None and 0 <= now_epoch - best["observed_at_epoch"] <= policy["probe_max_age_seconds"]:
            chosen.append(best)
        else:
            chosen.append(item)
    return chosen


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
                                  "--tlsv1.2", "--silent", "--show-error", "--fail", "--connect-timeout", str(EGRESS_PROBE_CONNECT_SECONDS),
                                  "--max-time", str(EGRESS_PROBE_MAX_SECONDS), "--max-filesize", "1024",
                                  "--resolve", f"{urlsplit(url).hostname}:443:{address}", url], timeout=EGRESS_PROBE_MAX_SECONDS + 1)
            ip = str(ipaddress.IPv4Address(raw.strip()))
            observation.update({"status": "passed", "ip_address": ip, "response_sha256": sha256_bytes(raw.encode("ascii"))})
        except (ValueError, DeploymentError):
            pass
        observation["observed_at_epoch"] = time.time()
        return observation
    with ThreadPoolExecutor(max_workers=len(policy["probe_urls"])) as pool:
        return list(pool.map(probe, policy["probe_urls"]))


def egress_remote_status(policy: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """经专用 WireGuard 访问出口守护；不读取本地缓存充当远端当前状态。"""

    import http.client

    address = str(ipaddress.IPv4Interface(policy["nodes"]["exit"]["tunnel_ipv4"]).ip)
    limit = min(0.8, policy["lease_seconds"] / 3) if timeout is None else timeout
    connection = http.client.HTTPConnection(address, policy["control_port"], timeout=limit)
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


# 源端每轮并行发出的出口健康请求数与同时在途上限；跨洋链路偶发丢包时，
# 较慢但成功的请求仍可在后续轮次收取，避免单次超时即判共享失效。
EGRESS_REMOTE_ATTEMPTS_PER_ROUND = 2
EGRESS_REMOTE_INFLIGHT = 8


def egress_remote_attempt(policy: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """后台单次出口健康请求；同时返回请求发出时的单调时钟，信任窗口从这一刻起算。"""

    started = time.monotonic_ns()
    return started, egress_remote_status(policy, timeout=float(policy["lease_seconds"]))


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
        self.passes: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, Any] = {}
        self.next_probe: dict[str, float] = {}
        self.blocked_since: dict[str, float] = {}
        self.last_compliant: dict[str, float] = {}
        self.lease_states: dict[str, str] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.policy["services"])))
        self.resolver_pool = ThreadPoolExecutor(max_workers=1)
        self.resolver_pending: Any | None = None
        self.next_resolution = time.time() + self.policy["probe_refresh_seconds"]
        self.remote_pool = ThreadPoolExecutor(max_workers=EGRESS_REMOTE_INFLIGHT) if role == "origin" else None
        self.remote_pending: list[Any] = []
        self.remote_valid_until_ns = 0
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

    def confirm_remote_guard(self) -> None:
        """源端核对出口守护：只采信出口响应中声明的剩余租期，并从请求发出时刻起算。

        跨洋链路偶发丢包会让单次请求因 TCP 重传超过超时。每轮并行发出两个请求，并继续收取此前仍在
        进行的请求；最近一次合规响应所担保的剩余租期内不判共享失效。窗口不超过出口自身的内核租期：
        出口守护失效时出口端先自行闭锁，源端最迟在窗口结束时闭锁，仍满足"守护失联的业务闭锁上界为租期"。
        """

        for _ in range(EGRESS_REMOTE_ATTEMPTS_PER_ROUND):
            if len(self.remote_pending) < EGRESS_REMOTE_INFLIGHT:
                self.remote_pending.append(self.remote_pool.submit(egress_remote_attempt, self.policy))
        wait_until = time.monotonic() + min(0.8, self.policy["lease_seconds"] / 3)
        while True:
            extended, unfinished = False, []
            for future in self.remote_pending:
                if not future.done():
                    unfinished.append(future)
                    continue
                try:
                    started, value = future.result()
                except Exception:
                    continue
                valid_until = started + int(value["lease_remaining_ms"]) * 10**6
                if valid_until > self.remote_valid_until_ns:
                    self.remote_valid_until_ns, extended = valid_until, True
            self.remote_pending = unfinished
            remaining = wait_until - time.monotonic()
            # 窗口仍有 1 秒以上余量时不等待在途请求，避免拖长本轮核验；首轮或窗口将尽时最多等待到本轮上限。
            if (extended or remaining <= 0 or not unfinished
                    or self.remote_valid_until_ns - time.monotonic_ns() > 10**9):
                break
            wait_futures(unfinished, timeout=remaining, return_when=FIRST_COMPLETED)
        if time.monotonic_ns() >= self.remote_valid_until_ns:
            raise DeploymentError("出口宿主当前保护不可确认")

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
                self.confirm_remote_guard()
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
                self.passes.pop(name, None)
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
                        results = pending[1].result()
                    except Exception:
                        results = None
                    if results is None:
                        self.observations.pop(name, None)
                        self.passes.pop(name, None)
                    else:
                        self.observations[name] = results
                        self.passes.setdefault(name, {}).update(
                            {item["url"]: item for item in results if item["status"] == "passed"})
                    # 任一来源失败即尽快重探；全部成功才按常规周期刷新。
                    complete = results is not None and all(item["status"] == "passed" for item in results)
                    self.next_probe[name] = now + (self.policy["probe_refresh_seconds"] if complete
                                                   else min(EGRESS_PROBE_RETRY_SECONDS, self.policy["probe_refresh_seconds"]))
            # 本轮签发的状态与内核租期要一直用到租期末尾，独立校验方按校验当下的时刻核对观测年龄。
            # 因此按租期末尾挑选与判定：会在租期内过期的旧成功不再沿用，改取最近一次真实结果（失败
            # 也如实计入法定数）。否则发布时 29.8 秒的成功观测会在 0.3 秒后被校验方判过期，触发整轮
            # 采集暂停（2026-09-26 0.157.0 VC-1 即此）。
            horizon = time.time() + self.policy["lease_seconds"]
            observations = egress_effective_observations(self.policy, self.observations.get(name, []),
                                                         self.passes.get(name, {}), now_epoch=horizon)
            compliant = shared and service["valid"] and self.contract.egress_observations_compliant(self.policy, observations, now_epoch=horizon)
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
            if not shared or states.get(name) != self.lease_states.get(name) or egress_lease_view(current) != egress_lease_view(old):
                self.revoke({"services": {name: old}})
        if not shared:
            self.revoke(self.inventory)
        transaction = egress_lease_transaction(self.policy, self.role, self.inventory, states, sorted(set(self.probes.values())), shared=shared)
        egress_command(["nft", "-f", "-"], input_text=transaction)
        if time.monotonic_ns() >= expiry:
            self.revoke(self.inventory)
            raise DeploymentError("出口守护一轮核验超过内核租期，不得签发迟到状态")
        marks = egress_service_marks(self.policy)
        # 探针目的表按当前解析结果对账；共享保护失效时清空（此时全部服务已闭锁）。
        current_probes = set(self.probes.values()) if shared else set()
        for maps in self.maps.values():
            maps.sync_probes(current_probes)
        for name, service in self.inventory.get("services", {}).items():
            if states[name] == "blocked":
                continue
            maps = self.maps[service["parent"]]
            probing = states[name] == "probing"
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
            if self.remote_pool:
                self.remote_pool.shutdown(wait=False, cancel_futures=True)


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
            # bpffs 保留含点号的名称（内核 bpf_lookup 对含 "." 的目录名返回 EPERM），slice 名的 ".slice" 须转写。
            # 策略只允许 [A-Za-z0-9_-] 加唯一的 ".slice" 后缀，把点号换成下划线后与 slice 仍一一对应。
            pin = Path("/sys/fs/bpf/sub2api-egress") / (name.replace(".", "_") + "-" + source_identity[:16])
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


def _module_file_under(module: Any, root: Path) -> bool:
    location = getattr(module, "__file__", None)
    if not isinstance(location, str):
        return False
    try:
        Path(location).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


@contextmanager
def _staged_module_imports(module_root: Path, names: tuple[str, ...]):
    """在一棵受管树里以脚本方式（扁平名）导入并调用模块，结束后不留任何痕迹。

    v3 身份引擎按「无包即扁平导入兄弟模块」加载；部署器要用暂存树（或切换后的生产树）里的那一份，不能混入
    进程内已缓存的同名模块。进入时把 ``module_root`` 放到 ``sys.path`` 首位并暂时移走同名模块；退出时恢复
    ``sys.path``，丢弃块内从这棵树导入的全部模块（含 ``names``），再放回原来的同名模块。加载与调用都在块内
    完成，函数体内的延迟导入也解析到同一棵树（2026-09-25 项目总账延迟导入事故的同类问题）。
    """

    root = module_root.resolve()
    original_sys_path = list(sys.path)
    previous = {name: sys.modules[name] for name in names if name in sys.modules}
    before = set(sys.modules)
    for name in names:
        sys.modules.pop(name, None)
    try:
        sys.path.insert(0, str(root))
        yield
    finally:
        sys.path[:] = original_sys_path
        for name in list(sys.modules):
            if name in names or (name not in before and _module_file_under(sys.modules[name], root)):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


def _fingerprint_entries(entries: list[dict[str, str]]) -> str:
    """与身份引擎 ``_fingerprint({"entries": ...})`` 相同的规范 JSON 摘要。"""

    return sha256_bytes(canonical({"entries": entries}))


def claude_identity(repository_root: Path) -> dict[str, Any]:
    """按这棵仓库视图里 official_client_capture 自带的 v3 引擎与 Claude 策略计算 Claude 工具身份。

    ``repository_root`` 是含 ``tools/official_client_capture`` 与 ``tools/official_client_control`` 的目录：
    预检时是暂存根，切换后复算时是生产数据根。返回五摘要与整树摘要（``tool_files_sha256``）、策略版本、
    按受管根分组的单根摘要（条目键与整树相同，``<根目录名>/<相对路径>``，两组合起来就是整树清单），以及
    策略登记的运行时绑定。按默认层归类的文件非空即失败关闭（B1-08 验收口径），否则身份不可信。
    """

    module_root = repository_root / CAPTURE_ROOT
    engine_path = module_root / CLAUDE_IDENTITY_ENGINE
    policy_path = module_root / CLAUDE_IDENTITY_POLICY
    for path in (engine_path, policy_path):
        if path.is_symlink() or not path.is_file():
            raise DeploymentError(f"受管树缺少 Claude 身份引擎或策略：{path}")
    with _staged_module_imports(module_root, CLAUDE_IDENTITY_SIBLINGS):
        try:
            engine = importlib.import_module("claude_tool_identity_policy")
        except Exception as error:  # noqa: BLE001 - 引擎加载失败必须让部署失败关闭
            raise DeploymentError(f"无法加载 Claude 身份引擎：{error}") from error
        if not _module_file_under(engine, module_root.resolve()):
            raise DeploymentError("Claude 身份引擎没有从目标受管树加载。")
        try:
            policy = engine.load_policy(policy_path)
            identity = engine.compute_identity(policy, repository_root)
            digests = engine.managed_tree_digests(repository_root, policy)
        except Exception as error:  # noqa: BLE001 - 策略失败必须让部署失败关闭
            raise DeploymentError(f"Claude 工具身份计算失败：{error}") from error
    if policy.get("client") != "claude":
        raise DeploymentError("Claude 身份策略的 client 不是 claude。")
    if identity.get("defaulted_paths"):
        raise DeploymentError(f"Claude 身份策略有未归层的受管文件：{identity['defaulted_paths'][:5]}")
    policy_roots = [str(root).rstrip("/") for root in policy["roots"]]
    if sorted(policy_roots) != sorted(CLIENT_MANAGED_ROOTS["claude"]):
        raise DeploymentError(f"Claude 策略的受管根与部署器登记不一致：{policy_roots}")
    roots: dict[str, dict[str, Any]] = {}
    for root in policy_roots:
        prefix = root.rsplit("/", 1)[-1] + "/"
        entries = [{"path": key, "sha256": digests[key]} for key in sorted(digests) if key.startswith(prefix)]
        roots[root] = {"tool_files_sha256": _fingerprint_entries(entries), "entry_count": len(entries)}
    whole = _fingerprint_entries([{"path": key, "sha256": digests[key]} for key in sorted(digests)])
    if whole != identity.get("tool_files_sha256"):
        raise DeploymentError("Claude 整树摘要与身份引擎输出不一致。")
    return {
        "client": "claude",
        **{field: identity[field] for field in CLAUDE_IDENTITY_FIELDS},
        "roots": roots,
        "runtime_bindings": sorted((dict(item) for item in policy["runtime_bindings"]), key=lambda item: item["name"]),
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


def rename_noreplace(source: Path, destination: Path) -> None:
    """不覆盖地改名（renameat2 RENAME_NOREPLACE）：目标已存在时由内核拒绝，不存在先查后改的竞争窗口。

    用于原路径不存在的对象切入生产（意图 ``old_exists=false``），以及回滚时把这类新产物移回候选区。完成后
    fsync 两边的父目录，改名本身随之持久化。
    """

    if source.is_symlink() or destination.is_symlink():
        raise DeploymentError("不覆盖改名的两端不得是符号链接。")
    if not source.exists():
        raise DeploymentError(f"不覆盖改名的源不存在：{source}")
    if destination.exists():
        raise DeploymentError(f"不覆盖改名的目标已存在：{destination}")
    if source.parent.stat().st_dev != destination.parent.stat().st_dev:
        raise DeploymentError("不覆盖改名的两端不在同一文件系统。")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except AttributeError as error:
        raise DeploymentError("ARM64 内核/运行库不提供 renameat2。") from error
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    fsync_directory(source.parent)
    fsync_directory(destination.parent)


def _bytecode_cache(relative: Path) -> bool:
    return "__pycache__" in relative.parts or relative.suffix == ".pyc"


def tree_content_sha256(root: Path) -> str:
    """一棵目录的完整内容摘要：恢复时据此判定生产路径上是旧树、新树还是别的东西。

    逐项按相对路径排序记录目录、普通文件（内容 sha256）与符号链接（指向），包括测试目录与非受管后缀——
    ``tool_digest`` 只覆盖三后缀且排除 tests，新旧两棵树只差测试文件时它给出同一个值，按它判定会把
    「已交换」误认成「未交换」。字节码缓存（``__pycache__``、``.pyc``）不计：部署之后生产树被别的进程
    导入时可能新长出缓存，它不是部署内容，不能因此把已交换的树判成「无法判定」。权限与时间不计。
    """

    if root.is_symlink() or not root.is_dir():
        raise DeploymentError(f"不是可计算内容摘要的目录：{root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _bytecode_cache(relative):
            continue
        name = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + name + b"\0" + os.fsencode(os.readlink(path)) + b"\n")
        elif path.is_dir():
            digest.update(b"D\0" + name + b"\n")
        elif path.is_file():
            digest.update(b"F\0" + name + b"\0" + file_sha256(path).encode("ascii") + b"\n")
        else:
            digest.update(b"O\0" + name + b"\n")
    return digest.hexdigest()


# 实际内容既不是 old 也不是 new 时的占位值：它们不是 64 位摘要，与任何意图摘要都不相等。
OBJECT_SYMLINK = "symlink"
OBJECT_TYPE_MISMATCH = "type-mismatch"


def object_sha256(path: Path, object_type: str) -> str | None:
    """按意图登记的对象类型取摘要：目录取完整内容摘要，文件取文件摘要；不存在为 None。

    符号链接与类型不符分别返回占位值，恢复判定把它们落到「其它」一行（失败关闭）。
    """

    if path.is_symlink():
        return OBJECT_SYMLINK
    if not path.exists():
        return None
    if object_type == "directory":
        return tree_content_sha256(path) if path.is_dir() else OBJECT_TYPE_MISMATCH
    if object_type == "file":
        return file_sha256(path) if path.is_file() else OBJECT_TYPE_MISMATCH
    raise DeploymentError(f"意图对象类型非法：{object_type}")


def _publish_bytes_once(path: Path, data: bytes) -> None:
    """一次性发布一个新文件：先写同目录临时文件并 fsync，再以硬链接落到目标名（目标已存在即失败），最后
    fsync 父目录。读者要么看不到目标，要么看到完整内容；目标一经出现就不再被覆盖（write-once）。"""

    if path.exists() or path.is_symlink():
        raise DeploymentError(f"目标已存在，拒绝覆盖：{path}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    fsync_directory(path.parent)


def receipt_bytes(payload: Mapping[str, Any]) -> bytes:
    """部署收据落盘的确切字节（规范 JSON 加换行，与 ``write_json_atomic`` 历来写出的相同）。"""

    return canonical(payload) + b"\n"


def write_json_once(path: Path, payload: Mapping[str, Any]) -> int:
    """write-once 写出部署收据；返回字节数。内容与 ``write_json_atomic`` 相同，只是不覆盖已有文件。"""

    data = receipt_bytes(payload)
    _publish_bytes_once(path, data)
    return len(data)


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


# 部署后冒烟：枚举容器内全部已安装的 Codex 客户端逐个执行 --version 并记录结果。任一二进制执行异常
# 只记录、不让探针失败；是否放行由 verify_container_codex_binaries 按本轮目标版本判定。
CODEX_BINARY_PROBE = (
    "import glob,json,re,subprocess\n"
    "rows=[]\n"
    "for path in sorted(glob.glob('/opt/codex-*/bin/codex')):\n"
    "    match=re.fullmatch(r'/opt/codex-((?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*))/bin/codex',path)\n"
    "    row={'path':path,'version':match.group(1) if match else None}\n"
    "    try:\n"
    "        done=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=30)\n"
    "        row.update(returncode=done.returncode,stdout=done.stdout.strip()[:200])\n"
    "    except Exception as error:\n"
    "        row.update(returncode=None,stdout='',error=type(error).__name__)\n"
    "    rows.append(row)\n"
    "print(json.dumps(rows))\n"
)
CODEX_VERSION_PATTERN = r"(?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)"


def read_target_codex_version(tool_root: Path) -> str:
    """本轮目标 Codex 版本取自已部署工具树的目标场景清单，部署脚本里不写死版本。"""

    manifest_path = tool_root / TARGET_SCENARIO_MANIFEST
    reject_untrusted_file(manifest_path, label="目标场景清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("无法读取目标场景清单。") from error
    version = manifest.get("codex_version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(CODEX_VERSION_PATTERN, version):
        raise DeploymentError("目标场景清单缺少合法的 codex_version。")
    return version


def verify_container_codex_binaries(output: str, target_version: str) -> list[dict[str, Any]]:
    """本轮目标版本的客户端必须存在、可执行并自报与安装目录一致的版本；其余目录只记录不判定。

    R15 复审修正：原先只要求"至少一个客户端且每个都可用"，目标版本缺失也能通过，非 x.y.z 命名或
    执行失败的其他目录反而拒掉整个部署。清单本身格式非法仍失败关闭。
    """

    try:
        rows = json.loads(output)
    except json.JSONDecodeError as error:
        raise DeploymentError("容器内 Codex 客户端清单无法解析。") from error
    if not isinstance(rows, list):
        raise DeploymentError("容器内 Codex 客户端清单格式非法。")
    target_path = f"/opt/codex-{target_version}/bin/codex"
    recorded: list[dict[str, Any]] = []
    target_verified = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise DeploymentError(f"容器内 Codex 客户端清单行非法：{row}")
        version = row.get("version") if isinstance(row.get("version"), str) else None
        verified = False
        if row["path"] == target_path:
            stdout = row.get("stdout")
            if (version != target_version or row.get("returncode") != 0 or not isinstance(stdout, str)
                    or target_version not in stdout.split()):
                raise DeploymentError(f"本轮目标版本 {target_version} 的 Codex 客户端不可执行或版本不符：{target_path}")
            verified = target_verified = True
        recorded.append({"path": row["path"], "version": version, "verified": verified})
    if not target_verified:
        raise DeploymentError(f"容器内缺少本轮目标版本 {target_version} 的 Codex 客户端：{target_path}")
    return recorded


# Claude 部署后冒烟（B5a-01）：容器内目标客户端装在 /opt/claude-<v>/claude（设计 D3：数据根 runtime/claude-<v> 只读挂载）。
# 每个二进制在隔离的空 HOME 里执行 --version（B1-10 实测不联网、不写 HOME），结束即删除临时 HOME。
CLAUDE_BINARY_PROBE = (
    "import glob,json,re,shutil,subprocess,tempfile\n"
    "rows=[]\n"
    "for path in sorted(glob.glob('/opt/claude-*/claude')):\n"
    "    match=re.fullmatch(r'/opt/claude-((?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*))/claude',path)\n"
    "    row={'path':path,'version':match.group(1) if match else None}\n"
    "    home=tempfile.mkdtemp(prefix='claude-deploy-probe-')\n"
    "    try:\n"
    "        done=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=30,"
    "env={'HOME':home,'PATH':'/usr/local/bin:/usr/bin:/bin'})\n"
    "        row.update(returncode=done.returncode,stdout=done.stdout.strip()[:200])\n"
    "    except Exception as error:\n"
    "        row.update(returncode=None,stdout='',error=type(error).__name__)\n"
    "    finally:\n"
    "        shutil.rmtree(home,ignore_errors=True)\n"
    "    rows.append(row)\n"
    "print(json.dumps(rows))\n"
)
# Claude 的目标版本登记点（扩展点）：Codex 取自已部署工具树的目标场景清单；Claude 的目标场景清单由 B2-05 建立、目标
# 客户端由 B5a-07 安装，此前受管树里没有 Claude 目标版本，探针如实跳过并把原因写进 Claude 收据，不伪造。B2-05 登记
# 目标场景清单时把文件名填在这里，清单需带 claude_version。
CLAUDE_TARGET_SCENARIO_MANIFEST: str | None = None


def read_target_claude_version(tool_root: Path) -> str | None:
    """本轮目标 Claude 版本：取自已部署工具树的 Claude 目标场景清单；尚未登记时为 None（探针跳过）。"""

    if CLAUDE_TARGET_SCENARIO_MANIFEST is None:
        return None
    manifest_path = tool_root / CLAUDE_TARGET_SCENARIO_MANIFEST
    reject_untrusted_file(manifest_path, label="Claude 目标场景清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("无法读取 Claude 目标场景清单。") from error
    version = manifest.get("claude_version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(CODEX_VERSION_PATTERN, version):
        raise DeploymentError("Claude 目标场景清单缺少合法的 claude_version。")
    return version


def verify_container_claude_binaries(output: str, target_version: str) -> list[dict[str, Any]]:
    """与 Codex 同一判定：本轮目标版本的 Claude 必须存在、可执行并自报与安装目录一致的版本；其余目录只记录。"""

    try:
        rows = json.loads(output)
    except json.JSONDecodeError as error:
        raise DeploymentError("容器内 Claude 客户端清单无法解析。") from error
    if not isinstance(rows, list):
        raise DeploymentError("容器内 Claude 客户端清单格式非法。")
    target_path = f"/opt/claude-{target_version}/claude"
    recorded: list[dict[str, Any]] = []
    target_verified = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise DeploymentError(f"容器内 Claude 客户端清单行非法：{row}")
        version = row.get("version") if isinstance(row.get("version"), str) else None
        verified = False
        if row["path"] == target_path:
            stdout = row.get("stdout")
            if (version != target_version or row.get("returncode") != 0 or not isinstance(stdout, str)
                    or target_version not in stdout.split()):
                raise DeploymentError(f"本轮目标版本 {target_version} 的 Claude 客户端不可执行或版本不符：{target_path}")
            verified = target_verified = True
        recorded.append({"path": row["path"], "version": version, "verified": verified})
    if not target_verified:
        raise DeploymentError(f"容器内缺少本轮目标版本 {target_version} 的 Claude 客户端：{target_path}")
    return recorded


def probe_claude_binaries(client: Any, tool_root: Path) -> dict[str, Any]:
    """Claude 部署后冒烟：有目标版本才进容器核对，没有就如实跳过（不执行任何容器命令）。"""

    target_version = read_target_claude_version(tool_root)
    if target_version is None:
        return {
            "status": "skipped",
            "target_version": None,
            "binaries": [],
            "reason": "受管工具树尚未登记 Claude 目标版本（目标场景清单 B2-05、目标客户端安装 B5a-07 之前）",
        }
    binaries = verify_container_claude_binaries(
        run_checked(client, "enable:verify-claude-binaries",
                    ["docker", "exec", "capture-cli", "python3", "-c", CLAUDE_BINARY_PROBE]),
        target_version,
    )
    return {"status": "verified", "target_version": target_version, "binaries": binaries, "reason": None}


PRE_A3_CERTIFICATION_MODULE = "codex_upgrade_pre_a3_certification.py"
MANAGED_TEST_MODULE_PREFIX = "tools.official_client_capture."


def verify_pre_a3_scenario_entries(tool_root: Path) -> dict[str, Any]:
    """发布流程保障：pre-A3 场景表的每个测试入口（含本包登记的真实链）都随暂存树部署。

    pre-A3 在 staging 根下逐个执行这些入口；入口文件缺失、测试类或方法改名会让认证在 ARM64
    上失败，登记链还会被记为未认证。这里只按 AST 核对模块文件、测试类与方法并记录文件摘要，
    不导入测试模块、不执行任何测试；属主与写权限由调用方的整树检查负责。
    """

    module = _parse_pre_a3_module(tool_root)
    literals: dict[str, Any] = {}
    for node in module.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in {"SCENARIOS", "REAL_CHAIN_IDS"}:
            try:
                literals[name] = ast.literal_eval(value)
            except ValueError as error:
                raise DeploymentError(f"pre-A3 {name} 不是可静态核对的字面量。") from error
    scenarios, registered_ids = literals.get("SCENARIOS"), literals.get("REAL_CHAIN_IDS")
    if (
        not isinstance(scenarios, tuple)
        or not scenarios
        or any(not isinstance(row, tuple) or len(row) != 5 or not all(isinstance(item, str) and item for item in row)
               for row in scenarios)
        or len({row[0] for row in scenarios}) != len(scenarios)
        or not isinstance(registered_ids, tuple)
        or not registered_ids
        or len(set(registered_ids)) != len(registered_ids)
    ):
        raise DeploymentError("pre-A3 场景表或真实链登记集合非法。")
    return _pre_a3_entry_rows(tool_root, scenarios, registered_ids)


def _parse_pre_a3_module(tool_root: Path) -> ast.Module:
    """读取并解析树内的 pre-A3 认证模块（只做 AST，不导入）。"""

    source_path = tool_root / PRE_A3_CERTIFICATION_MODULE
    if source_path.is_symlink() or not source_path.is_file():
        raise DeploymentError(f"暂存树缺少 pre-A3 认证模块：{source_path}")
    try:
        return ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise DeploymentError("无法解析 pre-A3 认证模块。") from error


def _pre_a3_entry_rows(
    tool_root: Path,
    scenarios: tuple[tuple[str, str, str, str, str], ...],
    registered_ids: tuple[str, ...],
) -> dict[str, Any]:
    """逐个核对场景入口（模块文件、测试类与方法）随树部署，并核对真实链登记项都在场景表里。

    Codex 与 Claude 的场景表共用这一段核对；表本身的读取与形态校验由各自的调用方负责。
    """

    parsed: dict[Path, ast.Module] = {}
    entries: list[dict[str, str]] = []
    for name, _description, module_name, class_name, method in scenarios:
        if not module_name.startswith(MANAGED_TEST_MODULE_PREFIX):
            raise DeploymentError(f"pre-A3 场景入口不在受管工具包内：{name}")
        relative = Path(*module_name[len(MANAGED_TEST_MODULE_PREFIX):].split(".")).with_suffix(".py")
        if relative.parts[0] != "tests" or ".." in relative.parts:
            raise DeploymentError(f"pre-A3 场景入口不是受管测试模块：{name}")
        path = tool_root / relative
        if path.is_symlink() or not path.is_file():
            raise DeploymentError(f"pre-A3 场景入口未随暂存树部署：{name} -> {relative.as_posix()}")
        if path not in parsed:
            try:
                parsed[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeDecodeError, SyntaxError) as error:
                raise DeploymentError(f"无法解析 pre-A3 场景入口：{relative.as_posix()}") from error
        classes = [node for node in parsed[path].body if isinstance(node, ast.ClassDef) and node.name == class_name]
        methods = [node for cls in classes for node in cls.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method]
        if len(classes) != 1 or len(methods) != 1:
            raise DeploymentError(f"pre-A3 场景入口类或方法缺失：{name} -> {class_name}.{method}")
        entries.append({"id": name, "test": f"{module_name}:{class_name}.{method}", "sha256": file_sha256(path)})
    registration = [entry for entry in entries if entry["id"] in registered_ids]
    if sorted(entry["id"] for entry in registration) != sorted(registered_ids):
        raise DeploymentError("pre-A3 真实链登记项在场景表中缺失或重复。")
    return {"scenario_count": len(entries), "real_chain_registration": registration}


def _top_level_assignment(node: ast.AST) -> tuple[str, ast.AST] | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return node.target.id, node.value
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id, node.value
    return None


def _pre_a3_client_table(module: ast.Module, name: str) -> dict[str, Any] | None:
    """读 pre-A3 模块里按客户端分组的表（``{"codex": …, "claude": …}``）；模块里没有这张表时为 None。

    表的值可以直接写字面量，也可以引用模块内另一个字面量常量（例如 ``{"codex": SCENARIOS, …}``）；两种写法
    都只做静态求值，不导入模块。其它写法无法静态核对，失败关闭。
    """

    literals: dict[str, Any] = {}
    deferred: dict[str, ast.AST] = {}
    for node in module.body:
        assignment = _top_level_assignment(node)
        if assignment is None:
            continue
        target, value = assignment
        try:
            literals[target] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            deferred[target] = value
    if name in literals:
        table = literals[name]
    elif name in deferred:
        node = deferred[name]
        if not isinstance(node, ast.Dict):
            raise DeploymentError(f"pre-A3 {name} 不是可静态核对的字面量。")
        table = {}
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise DeploymentError(f"pre-A3 {name} 的键必须是客户端名字符串。")
            if isinstance(value, ast.Name) and value.id in literals:
                table[key.value] = literals[value.id]
                continue
            try:
                table[key.value] = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError, RecursionError) as error:
                raise DeploymentError(f"pre-A3 {name} 不是可静态核对的字面量。") from error
    else:
        return None
    if not isinstance(table, dict):
        raise DeploymentError(f"pre-A3 {name} 必须是按客户端分组的映射。")
    return table


def verify_claude_pre_a3_entries(tool_root: Path) -> dict[str, Any]:
    """Claude 的 pre-A3 场景表与真实链登记（B5a-01 留结构；按客户端分组的表由 B5a-04 建立，Claude 场景由 B5a-05 登记）。

    按客户端分组的表（``SCENARIOS_BY_CLIENT``／``REAL_CHAIN_IDS_BY_CLIENT``）里有 Claude 场景时，与 Codex 用同一段
    入口核对（模块文件、测试类与方法随树部署，真实链登记项在场景表内）；表不存在或 Claude 集合为空时如实记录与原因，
    不伪造认证状态。
    """

    module = _parse_pre_a3_module(tool_root)
    scenario_table = _pre_a3_client_table(module, "SCENARIOS_BY_CLIENT")
    chain_table = _pre_a3_client_table(module, "REAL_CHAIN_IDS_BY_CLIENT")
    scenarios = scenario_table.get("claude") if scenario_table is not None else None
    registered_ids = chain_table.get("claude") if chain_table is not None else None
    if not scenarios:
        return {
            "status": "not-registered",
            "scenario_count": 0,
            "real_chain_registration": [],
            "reason": (
                "pre-A3 认证模块的 Claude 场景集合为空（四条最小恢复演练链由 B5a-05 登记）"
                if scenario_table is not None
                else "pre-A3 认证模块没有按客户端分组的场景表"
            ),
        }
    if (
        not isinstance(scenarios, tuple)
        or any(not isinstance(row, tuple) or len(row) != 5 or not all(isinstance(item, str) and item for item in row)
               for row in scenarios)
        or len({row[0] for row in scenarios}) != len(scenarios)
        or not isinstance(registered_ids, tuple)
        or not registered_ids
        or len(set(registered_ids)) != len(registered_ids)
    ):
        raise DeploymentError("pre-A3 Claude 场景表或真实链登记集合非法。")
    return {"status": "verified", **_pre_a3_entry_rows(tool_root, scenarios, registered_ids), "reason": None}


def project_ledger_summary(supervisor: Any, data_root: Path) -> dict[str, Any] | None:
    """B9：部署收据只读记录生产数据根下项目总账 head 的两层预算事实；没有总账时为 None。"""

    ledger_module = getattr(supervisor, "project_ledger", None)
    if ledger_module is None:
        return None
    # 总账回放遇到延期事件（deadline_extended）时才在函数体内裸名导入 codex_upgrade_vc_artifacts；
    # 2026-09-25 项目总账写入延期事件后，部署预检在这里 ModuleNotFoundError。
    with staging_sibling_imports(supervisor):
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


# 监督器同目录直接依赖：模块名 → 监督器上的属性名。加载暂存监督器与之后调用这些暂存模块共用同一张表。
SUPERVISOR_SIBLING_MODULES = {
    "codex_upgrade_evidence_permissions": "evidence_permissions",
    "codex_upgrade_project_ledger": "project_ledger",
    "codex_upgrade_root_cause": "root_cause",
    "codex_upgrade_timing_ledger": "timing_ledger",
    "codex_upgrade_vc_artifacts": "vc_artifacts",
}


@contextmanager
def staging_sibling_imports(supervisor: Any):
    """调用暂存监督器的同级模块时，复现 load_supervisor 加载期的导入环境。

    暂存模块在函数体内按“无包即裸名导入”延迟导入同级模块，而 load_supervisor 结束时已恢复 sys.path
    并移除同名模块，直接调用会 ModuleNotFoundError。调用期间把暂存工具目录放回 sys.path 首位，并把
    加载时绑定的同级模块对象按原名放回 sys.modules（不重新从磁盘导入，也不混入进程内的同名旧模块），
    结束后原样恢复。
    """

    module_root = str(Path(supervisor.__file__).resolve().parent)
    original_sys_path = list(sys.path)
    previous_modules = {
        name: sys.modules[name]
        for name in SUPERVISOR_SIBLING_MODULES
        if name in sys.modules
    }
    try:
        sys.path.insert(0, module_root)
        for name, attribute in SUPERVISOR_SIBLING_MODULES.items():
            loaded = getattr(supervisor, attribute, None)
            if loaded is not None:
                sys.modules[name] = loaded
        yield
    finally:
        sys.path[:] = original_sys_path
        for name in SUPERVISOR_SIBLING_MODULES:
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)


def load_supervisor(staging_root: Path) -> Any:
    module_root = staging_root / "tools" / "official_client_capture"
    module_path = module_root / "codex_upgrade_supervisor.py"
    sibling_modules = {
        name: (attribute, module_root / f"{name}.py")
        for name, attribute in SUPERVISOR_SIBLING_MODULES.items()
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
    """把超长数组与过深嵌套压成可复算摘要，避免部署规模增长击穿监督器事件上限。

    监督器事件 metadata 以顶层对象为第 0 层，第 4 层出现任何值都会拒绝。R15 起预检与切换后
    核验都带运行时出口策略的节点（runtime_egress→nodes→角色→endpoint），第 3 层再出现
    非空容器就越界，部署在记录预检事件时失败。这里把第 3 层的非空容器改写成规范 JSON 的
    摘要字符串；完整事实仍原样写入部署收据，原本就能通过的 metadata 保持不变。
    """

    def canonical_sha256(item: Any) -> str:
        return sha256_bytes(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def compact(item: Any, depth: int) -> Any:
        if isinstance(item, (list, Mapping)) and item and depth >= 3:
            return "sha256:" + canonical_sha256(item)
        if isinstance(item, list):
            if len(item) > 32:
                return {
                    "item_count": len(item),
                    "items_sha256": canonical_sha256(item),
                }
            return [compact(child, depth + 1) for child in item]
        if isinstance(item, Mapping):
            return {str(key): compact(child, depth + 1) for key, child in item.items()}
        return item

    return {str(key): compact(child, 1) for key, child in value.items()}


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
        default=Path("/root/docker/capture-cli/data/staging/codex-0.160.0-managed-tools"),
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
    # B5a-01：多根事务与按客户端签收据。缺省只交换 official_client_capture、只签 Codex 收据，与改造前逐字相同。
    parser.add_argument(
        "--client",
        nargs="+",
        choices=DEPLOY_CLIENTS,
        default=None,
        help="要签部署收据的客户端（可多值，缺省 codex）；交换 official_client_capture 影响两个客户端，"
             "official_client_control 只影响 Claude，已纳管的受影响客户端必须一并签收据",
    )
    parser.add_argument(
        "--managed-roots",
        nargs="+",
        default=None,
        help=f"要交换的受管根（相对数据根，可多值，缺省 {CAPTURE_ROOT}）；可选 {', '.join(MANAGED_ROOT_CLIENTS)}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    # 部署进程与它起的子进程一律不写字节码（E2-02）：本进程会按文件导入 staging 树里的受管模块，子进程还会运行
    # 生产树里的监督器；不写就不会有 __pycache__ 跟着 staging 树复制进生产树，也不依赖调用方的 shell 有没有设禁写。
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
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
    # B5a-01：事务范围与 Claude 预期身份。与 Codex 的暂存身份一样在启动监督器之前算好（只读暂存树）。缺省（只交换
    # official_client_capture、只签 Codex）时这里不读任何 Claude 文件；后面的事件序列与改造前相同，只多出写意图、
    # 收据正文入日志与删除日志三步，Codex 收据的字段与取值不变。
    scope = transaction_scope(arguments.managed_roots, arguments.client, control_root)
    signing = scope["clients"]
    production_data_root = production_root.parent.parent
    if scope["roots"] != [CAPTURE_ROOT] or "claude" in signing:
        production_data_root = require_standard_layout(production_root)
    claude_expected = claude_identity(staging_root) if "claude" in signing else None
    supervisor = load_supervisor(staging_root)
    stamp = utc_stamp().lower()
    campaign_id = f"c01600-supervisor-enable-{stamp}-{secrets.token_hex(4)}"
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
    documents, runtime_documents = transaction_documents(signing)
    receipt_paths = {name: control_root / RECEIPT_NAME_TEMPLATES[name].format(stamp=stamp) for name in signing}
    journal_path = control_root / SWITCH_JOURNAL_NAME_TEMPLATE.format(stamp=stamp)
    root_candidates: dict[str, Path] = {}
    root_backups: dict[str, Path] = {}
    assertion_preparer_candidate: Path | None = None
    assertion_preparer_backup: Path | None = None
    transaction_root: Path | None = None
    journal: SwitchJournal | None = None
    recovery: dict[str, Any] | None = None

    def write_switch_journal() -> Mapping[str, Any]:
        """交换任何对象之前写入完整意图：每个对象的新旧摘要按实际内容现算，日志文件与父目录都 fsync。"""

        nonlocal journal
        if transaction_root is None:
            raise DeploymentError("文档事务目录坐标缺失。")
        roots = [
            intent_entry(
                "root",
                root,
                "directory",
                production_root if root == CAPTURE_ROOT else production_data_root / root,
                root_candidates[root],
                root_backups[root],
            )
            for root in scope["roots"]
        ]
        preparers = []
        if assertion_preparer_candidate is not None and assertion_preparer_backup is not None:
            preparers.append(intent_entry(
                "preparer",
                MANAGED_ASSERTION_PREPARER,
                "file",
                production_assertion_preparer,
                assertion_preparer_candidate,
                assertion_preparer_backup,
            ))
        intent = {
            "schema_version": SWITCH_JOURNAL_SCHEMA,
            "stamp": stamp,
            "campaign_id": campaign_id,
            "created_at_utc": _iso_now(),
            "clients": list(signing),
            "affected_clients": list(scope["affected_clients"]),
            "unmanaged_affected_clients": list(scope["unmanaged_affected_clients"]),
            "managed_roots": list(scope["roots"]),
            "control_root": str(control_root),
            "production_data_root": str(production_data_root),
            "production_doc_root": str(production_doc_root),
            "transaction_root": str(transaction_root),
            "receipt_paths": {name: str(path) for name, path in receipt_paths.items()},
            "roots": roots,
            "preparers": preparers,
            "documents": document_intent_entries(transaction_root, production_doc_root, documents, runtime_documents),
        }
        journal = SwitchJournal.create(journal_path, intent)
        return {
            "journal": journal_path.name,
            "intent_sha256": journal.intent_sha256,
            "roots": [{"root": entry["root"], "old_exists": entry["old_exists"]} for entry in roots],
            "object_count": len(roots) + len(preparers) + len(intent["documents"]),
        }

    def active_journal() -> SwitchJournal:
        if journal is None:
            raise DeploymentError("部署事务日志尚未写入。")
        return journal

    def switch_roots() -> Mapping[str, Any]:
        rows = switch_intent_group(active_journal(), "roots")
        result: dict[str, Any] = {"roots": rows}
        if CAPTURE_ROOT in root_backups:
            result["backup"] = root_backups[CAPTURE_ROOT].name
        return result

    def switch_preparers() -> Mapping[str, Any]:
        switch_intent_group(active_journal(), "preparers")
        return {"backup": assertion_preparer_backup.name if assertion_preparer_backup else None}

    def switch_documents() -> Mapping[str, Any]:
        rows = switch_intent_group(active_journal(), "documents")
        active = [row for row in rows if not row["object"].startswith(ARCHIVE_DOCUMENT_PREFIX)]
        archived = [row for row in rows if row["object"].startswith(ARCHIVE_DOCUMENT_PREFIX)]
        return {
            "switched_documents": [row["object"] for row in active if row["result"] == "switched"],
            "installed_documents": [row["object"] for row in active if row["result"] == "installed"],
            "switched_archived_documents": [
                row["object"].removeprefix(ARCHIVE_DOCUMENT_PREFIX) for row in archived if row["result"] == "switched"
            ],
            "installed_archived_documents": [
                row["object"].removeprefix(ARCHIVE_DOCUMENT_PREFIX) for row in archived if row["result"] == "installed"
            ],
            "backup_root": str(Path(str(transaction_root)) / "backups"),
        }

    def verify_transaction() -> Mapping[str, Any]:
        """全局一致性核验：每个对象都已是新版本，再做各签收据客户端的切换后核验。"""

        result: dict[str, Any] = dict(verify_switched_entries(active_journal()))
        if "codex" in signing:
            result.update(
                _post_switch_verify(
                    client,
                    staging_root,
                    production_root,
                    production_doc_root,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                    arguments.expected_assertion_preparer_digest,
                )
            )
        if claude_expected is not None:
            result["claude"] = _post_switch_verify_claude(client, production_data_root, claude_expected)
        return result

    def prepare_receipts(
        verification: Mapping[str, Any],
        ledger_summary: Mapping[str, Any],
        claude_preflight: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """按签收据客户端组装收据正文（Codex 先定稿，Claude 收据记录它的文件名与摘要）。"""

        current = active_journal()
        prepared: list[dict[str, Any]] = []
        if "codex" in signing:
            archived, installed = codex_document_facts(current.intent)
            payload = build_codex_receipt(
                campaign_id=campaign_id,
                production_root=production_root,
                production_doc_root=production_doc_root,
                expected_tool_digest=arguments.expected_tool_digest,
                identity_v2=arguments.identity_v2,
                expected_supervisor_digest=arguments.expected_supervisor_digest,
                expected_assertion_preparer_digest=arguments.expected_assertion_preparer_digest,
                backup=root_backups.get(CAPTURE_ROOT),
                assertion_preparer_backup=assertion_preparer_backup,
                transaction_root=transaction_root,
                switched_archived_documents=archived,
                installed_documents=installed,
                supervisor_run_dir=client.run_dir,
                ledger_summary=ledger_summary,
            )
            prepared.append({
                "client": "codex",
                "path": str(receipt_paths["codex"]),
                "sha256": sha256_bytes(receipt_bytes(payload)),
                "payload": payload,
            })
        if claude_expected is not None:
            payload = build_claude_receipt(
                campaign_id=campaign_id,
                journal=current,
                verification=verification["claude"],
                pre_a3=claude_preflight["pre_a3"],
                expected_supervisor_digest=arguments.expected_supervisor_digest,
                supervisor_run_dir=client.run_dir,
                transaction_receipts=[
                    {"client": item["client"], "receipt": Path(item["path"]).name, "sha256": item["sha256"]}
                    for item in prepared
                ],
            )
            prepared.append({
                "client": "claude",
                "path": str(receipt_paths["claude"]),
                "sha256": sha256_bytes(receipt_bytes(payload)),
                "payload": payload,
            })
        return prepared

    def record_prepared_receipts(prepared: list[dict[str, Any]]) -> Mapping[str, Any]:
        active_journal().append("receipts_prepared", receipts=prepared)
        return {"receipts": [{"client": item["client"], "sha256": item["sha256"]} for item in prepared]}

    def remove_switch_journal() -> Mapping[str, Any]:
        active_journal().remove()
        return {"journal": journal_path.name}

    # campaign-run 子进程复用父监督器；独立调用才创建并结束自己的监督器。部署锁先于监督器取得，回滚也在锁内完成。
    with deploy_lock(control_root), (nullcontext(client) if client.attached else client):
        try:
            if pending_switch_journals(control_root):
                recovery = record_step(
                    client,
                    "enable:recover-switch-journal",
                    lambda: recover_switch_journal(control_root),
                )
                if recovery.get("outcome") == "rolled-back":
                    raise DeploymentError(
                        f"发现未完成的部署事务，已按内容恢复到部署前并把日志改名为 {recovery.get('journal')} 留证；"
                        "本次部署被拒绝，核对后重新执行。"
                    )
            # 「已纳管」按 control 目录里的已写收据判定：启动时那次判定在取锁之前，期间别的部署（或上面补齐的残留收据）
            # 可能刚让某个客户端变成已纳管。锁内、恢复之后按当前状态重算一次，范围变了即拒绝，不按过时的判定签收据。
            if transaction_scope(arguments.managed_roots, arguments.client, control_root) != scope:
                raise DeploymentError("取得部署锁后事务范围变了（期间有部署签出了收据），重新执行以按当前纳管状态签收据。")
            if "codex" in signing:
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
            claude_preflight: dict[str, Any] = {}
            if claude_expected is not None:
                claude_preflight = record_step(
                    client,
                    "enable:preflight-claude",
                    lambda: _preflight_claude(
                        client,
                        staging_root,
                        production_doc_root,
                        production_data_root,
                        scope["roots"],
                        arguments.expected_supervisor_digest,
                        include_environment="codex" not in signing,
                    ),
                )
            for root in scope["roots"]:
                suffix = root_event_suffix(root)
                staging_path = staging_root / root
                candidate_path = control_root / (
                    f"codex-01600-tool-candidate-{stamp}-{secrets.token_hex(4)}"
                    if root == CAPTURE_ROOT
                    else f"{root_name(root)}-candidate-{stamp}-{secrets.token_hex(4)}"
                )
                root_candidates[root] = candidate_path
                record_step(
                    client,
                    f"enable:copy-candidate{suffix}",
                    lambda source=staging_path, target=candidate_path: _copy_candidate(source, target),
                )
                record_step(
                    client,
                    f"enable:normalize-candidate{suffix}",
                    lambda target=candidate_path: {"entry_count": normalize_tree_permissions(target)},
                )
                if root == CAPTURE_ROOT:
                    record_step(
                        client,
                        "enable:verify-candidate",
                        lambda target=candidate_path: _verify_candidate(
                            target,
                            arguments.expected_tool_digest,
                            arguments.expected_supervisor_digest,
                        ),
                    )
                else:
                    record_step(
                        client,
                        f"enable:verify-candidate{suffix}",
                        lambda source=staging_path, target=candidate_path: _verify_root_candidate(target, source),
                    )
            if "codex" in signing:
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
            transaction_root = production_doc_root / transaction_root_name(stamp, secrets.token_hex(4), signing)
            record_step(
                client,
                "enable:prepare-document-candidates",
                lambda: _prepare_transaction_documents(
                    staging_root / "docs",
                    production_doc_root,
                    transaction_root,
                    documents,
                    runtime_documents,
                ),
            )
            for root in scope["roots"]:
                if root == CAPTURE_ROOT:
                    # 生产目录当前可能已经是某个 0.151 工具树；备份名不能把未知
                    # 的旧树伪装成 0.149.1，具体版本由既有启用收据和摘要确定。
                    root_backups[root] = control_root / (
                        f"managed-tools-backup-before-{arguments.expected_tool_digest[:12]}-"
                        f"{stamp}-{secrets.token_hex(4)}"
                    )
                else:
                    root_backups[root] = control_root / (
                        f"{root_name(root)}-backup-before-{tree_content_sha256(root_candidates[root])[:12]}-"
                        f"{stamp}-{secrets.token_hex(4)}"
                    )
            if "codex" in signing:
                assertion_preparer_backup = control_root / (
                    f"assertion-preparer-backup-before-"
                    f"{arguments.expected_assertion_preparer_digest[:12]}-"
                    f"{stamp}-{secrets.token_hex(4)}"
                )
            record_step(client, "enable:write-switch-journal", write_switch_journal)
            active_journal().append("switch_begin")
            record_step(client, "enable:atomic-exchange", switch_roots)
            if assertion_preparer_backup is not None:
                record_step(client, "enable:switch-assertion-preparer", switch_preparers)
            record_step(client, "enable:switch-documents", switch_documents)
            active_journal().append("switch_done")
            verification = record_step(client, "enable:post-switch-verify", verify_transaction)
            active_journal().append("verified")
            ledger_summary = (
                record_step(
                    client,
                    "enable:read-project-ledger",
                    lambda: project_ledger_summary(supervisor, control_root.parent),
                )
                if "codex" in signing
                else {}
            )
            prepared = prepare_receipts(verification, ledger_summary, claude_preflight)
            record_step(client, "enable:prepare-receipts", lambda: record_prepared_receipts(prepared))
            record_step(client, "enable:write-receipt", lambda: write_prepared_receipts(prepared))
            record_step(client, "enable:remove-switch-journal", remove_switch_journal)
        except BaseException as error:
            # 必须在监督器仍运行时记录回滚；不能等上下文退出后再补写。收据正文入日志之后不回滚：收据可能已经写出，
            # 回滚会让它描述一棵不存在的树，留给下次启动按日志正文补齐（设计 B5a-01 残留 ②）。
            failed = journal
            if failed is not None and not failed.receipts_prepared and failed.path.exists():
                reason = type(error).__name__
                try:
                    record_step(
                        client,
                        "enable:rollback",
                        lambda: restore_switch_journal(failed, reason=reason),
                    )
                except BaseException:
                    pass
            raise
    output: dict[str, Any] = {
        "status": "passed",
        "campaign_id": campaign_id,
        "receipt": str(receipt_paths["codex"] if "codex" in receipt_paths else receipt_paths["claude"]),
        "supervisor_run_dir": str(client.run_dir),
        "rollback_backup": str(root_backups[CAPTURE_ROOT]) if CAPTURE_ROOT in root_backups else None,
        "assertion_preparer_rollback_backup": (
            str(assertion_preparer_backup)
            if assertion_preparer_backup
            else None
        ),
    }
    if signing != ["codex"]:
        output["receipts"] = {name: str(path) for name, path in receipt_paths.items()}
    if recovery is not None:
        output["recovered_switch_journal"] = recovery
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
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
    # 出口路径只按受限运维策略与持续守护的实时状态核验，不再比对固定服务商、隧道接口或路由表。
    runtime_egress = verify_runtime_egress()
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
    pre_a3_scenarios = verify_pre_a3_scenario_entries(staging_tool)
    return {
        "staging_file_count": staging_count,
        "staging_tool_sha256": staging_digest,
        "staging_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "capture_ip": capture_network,
        "service_ip": service_network,
        "network_policy": "runtime-egress-policy",
        "runtime_egress": runtime_egress,
        "document_sha256": document_sha256,
        "runtime_document_bindings": runtime_document_bindings,
        "scenario_source_spec": source_spec,
        "pre_a3_scenarios": pre_a3_scenarios,
        "legacy_production_documents": legacy_document_facts,
        "legacy_runtime_documents": legacy_runtime_document_facts,
    }


def _copy_candidate(source: Path, destination: Path) -> Mapping[str, Any]:
    if destination.exists() or destination.is_symlink():
        raise DeploymentError("候选目录已存在，拒绝覆盖。")
    # 字节码缓存不进生产树（E2-02）：staging 树若被别的进程导入过、留下了 __pycache__，也不跟着复制过去。
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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
    runtime_egress = verify_runtime_egress()
    run_checked(
        client,
        "enable:compile-production-supervisor",
        [*COMPILE_CHECK_COMMAND, str(supervisor_path)],
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
    codex_target_version = read_target_codex_version(production)
    codex_binaries = verify_container_codex_binaries(
        run_checked(
            client,
            "enable:verify-codex-binaries",
            ["docker", "exec", "capture-cli", "python3", "-c", CODEX_BINARY_PROBE],
        ),
        codex_target_version,
    )
    return {
        "production_file_count": count,
        "production_tool_sha256": digest,
        "production_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "container_capture_sha256": container_payload.get("sha256"),
        "supervisor_help_bytes": len(version.encode("utf-8")),
        "runtime_egress": runtime_egress,
        "codex_target_version": codex_target_version,
        "codex_binaries": codex_binaries,
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


# ---------------------------------------------------------------------------
# 多根事务：事务范围、写前意图日志、按内容恢复、收据补齐（B5a-01）
# ---------------------------------------------------------------------------
# 上面的 _switch_documents、_exchange_and_backup、_switch_assertion_preparer、_rollback_deployment 是改造前按「进行到
# 哪一步」记忆回滚的单根实现，保留原样供既有测试与历史行为对照；main 的事务改由意图条目驱动，切入与恢复都只看
# 生产路径的实际内容。

# 意图里的三组条目：组名 → (条目名称键, 对象类型)。切入按这个顺序（根 → 断言预处理器 → 文档，与改造前相同），
# 恢复逆序进行。每个条目恰好是名称键加下面七项（与设计 B5a-01 的根条目字段逐字相同）。
JOURNAL_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("roots", "root", "directory"),
    ("preparers", "preparer", "file"),
    ("documents", "document", "file"),
)
ENTRY_PATH_FIELDS = ("production_path", "candidate_path", "backup_path")
ENTRY_FIELDS = (*ENTRY_PATH_FIELDS, "old_exists", "old_tree_sha256", "new_tree_sha256")
INTENT_FIELDS = frozenset({
    "schema_version", "stamp", "campaign_id", "created_at_utc", "clients", "affected_clients",
    "unmanaged_affected_clients", "managed_roots", "control_root", "production_data_root", "production_doc_root",
    "transaction_root", "receipt_paths", *(group for group, _key, _object_type in JOURNAL_GROUPS),
})
# 文档条目名是相对生产文档根的路径；repository-docs 下的归档副本单独成条目。
ARCHIVE_DOCUMENT_PREFIX = "repository-docs/"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
# 恢复判定（设计 B5a-01 的判定表）：SETTLED 是「未交换或已回滚」「未切入或已回滚」两行，无动作；BLOCKED 是
# 无法判定、已交换却找不到旧副本、新产物要移回而候选区已被占用——任一条目落在这里，整个恢复一个对象都不动、
# 失败关闭交人工，现场保持发现时的样子便于分析。
RECOVERY_SETTLED = frozenset({"at-old", "absent"})
RECOVERY_BLOCKED = frozenset({"undecidable", "old-copy-missing", "candidate-occupied"})
RECEIPT_NAME_TEMPLATES: Mapping[str, str] = {
    "codex": CODEX_RECEIPT_NAME_TEMPLATE,
    "claude": CLAUDE_RECEIPT_NAME_TEMPLATE,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _within(path: Path, root: Path) -> bool:
    if ".." in path.parts:
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def root_name(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1]


def root_event_suffix(root: str) -> str:
    """监督器事件名后缀：official_client_capture 沿用改造前的事件名，其它根带上根目录名。"""

    return "" if root == CAPTURE_ROOT else f":{root_name(root)}"


def managed_clients(control_root: Path) -> list[str]:
    """已纳管的客户端：control 目录里已经有它的已写部署收据（日志里的收据正文不算）。"""

    return [
        client
        for client in DEPLOY_CLIENTS
        if any(path.is_file() and not path.is_symlink() for path in control_root.glob(DEPLOY_RECEIPT_GLOBS[client]))
    ]


def transaction_scope(
    managed_roots: list[str] | None,
    clients: list[str] | None,
    control_root: Path,
) -> dict[str, list[str]]:
    """确定本次事务交换哪些根、为哪些客户端签收据，并拒绝会让某份收据失真的组合。

    * 受影响的客户端由被交换的根推出（``MANAGED_ROOT_CLIENTS``）；签收据的客户端必须受影响（不能为身份没变的
      客户端签收据），每个被交换的根也必须至少有一个受它影响的客户端签收据（否则这次交换没有任何收据记录）。
    * 已纳管（已有部署收据）的受影响客户端必须一并签收据：否则它在 control 目录里「最新」的收据描述的是已被换掉
      的树，按客户端取最新收据的消费者会把过期身份当作生产现状。尚未纳管的受影响客户端（例如 B5a-02 之前的
      Claude）不签，只记进意图的 ``unmanaged_affected_clients``——它还没有任何收据可被误用。
    """

    roots = list(dict.fromkeys(managed_roots or DEFAULT_MANAGED_ROOTS))
    unknown = [root for root in roots if root not in MANAGED_ROOT_CLIENTS]
    if unknown:
        raise DeploymentError(f"不认识的受管根：{unknown}（可选：{list(MANAGED_ROOT_CLIENTS)}）")
    order = list(MANAGED_ROOT_CLIENTS)
    roots.sort(key=order.index)
    wanted = set(clients or DEFAULT_CLIENTS)
    signing = [client for client in DEPLOY_CLIENTS if client in wanted]
    if wanted - set(DEPLOY_CLIENTS):
        raise DeploymentError(f"不认识的客户端：{sorted(wanted - set(DEPLOY_CLIENTS))}")
    affected = [client for client in DEPLOY_CLIENTS if any(client in MANAGED_ROOT_CLIENTS[root] for root in roots)]
    not_affected = [client for client in signing if client not in affected]
    if not_affected:
        raise DeploymentError(f"本次交换的根不影响客户端 {not_affected}，不能为它签部署收据：{roots}")
    unsigned_roots = [root for root in roots if not set(MANAGED_ROOT_CLIENTS[root]) & set(signing)]
    if unsigned_roots:
        raise DeploymentError(f"交换 {unsigned_roots} 却没有为受它影响的客户端签部署收据，加上对应的 --client。")
    missing = [client for client in affected if client in managed_clients(control_root) and client not in signing]
    if missing:
        raise DeploymentError(
            f"本次交换影响已纳管的客户端 {missing}：必须同时为它签部署收据（--client {' '.join([*signing, *missing])}），"
            "否则它的最新部署收据会描述已被替换的工具树。"
        )
    return {
        "roots": roots,
        "clients": signing,
        "affected_clients": affected,
        "unmanaged_affected_clients": [client for client in affected if client not in signing],
    }


def require_standard_layout(production_root: Path) -> Path:
    """多根事务按数据根定位各根：``--production-root`` 必须是 ``<数据根>/tools/official_client_capture``。"""

    if production_root.name != root_name(CAPTURE_ROOT) or production_root.parent.name != "tools":
        raise DeploymentError(f"多根部署要求生产工具根位于 <数据根>/{CAPTURE_ROOT}：{production_root}")
    return production_root.parent.parent


def transaction_documents(clients: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """签收据客户端的活动文档与运行时依赖文档的并集（按客户端顺序去重）；只签 Codex 时与改造前完全相同。"""

    documents: list[str] = []
    runtime_documents: list[str] = []
    for client in DEPLOY_CLIENTS:
        if client not in clients:
            continue
        documents.extend(name for name in CLIENT_MANAGED_DOCUMENTS[client] if name not in documents)
        runtime_documents.extend(name for name in CLIENT_MANAGED_RUNTIME_DOCUMENTS[client] if name not in runtime_documents)
    return tuple(documents), tuple(runtime_documents)


def transaction_root_name(stamp: str, token: str, clients: list[str]) -> str:
    """文档事务目录名：签 Codex 收据时沿用改造前的名字（Codex 收据的 document_rollback_backup 指向它）。"""

    return f".codex-01600-deploy-{stamp}-{token}" if "codex" in clients else f".managed-deploy-{stamp}-{token}"


@contextmanager
def deploy_lock(control_root: Path):
    """同一 control 目录同时只允许一个部署进程（flock，进程退出即释放）。

    启动时的恢复会按内容回滚它发现的事务日志；没有这把锁，另一个仍在进行的部署会被当成中断残留回滚掉。
    """

    descriptor = os.open(control_root / DEPLOY_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("另一个受监督部署正在使用同一 control 目录，拒绝并发执行。") from error
        yield
    finally:
        os.close(descriptor)


def _journal_entries(intent: Mapping[str, Any]) -> list[tuple[str, str, str, Mapping[str, Any]]]:
    """意图里的全部条目，按切入顺序：(组名, 条目名, 对象类型, 条目)。"""

    rows: list[tuple[str, str, str, Mapping[str, Any]]] = []
    for group, key, object_type in JOURNAL_GROUPS:
        for entry in intent[group]:
            rows.append((group, str(entry[key]), object_type, entry))
    return rows


def _validate_intent(intent: Any) -> None:
    """意图结构与路径边界的完整校验：恢复只按日志里的路径动作，被改动或越界的意图一律失败关闭。"""

    if not isinstance(intent, dict) or set(intent) != INTENT_FIELDS or intent.get("schema_version") != SWITCH_JOURNAL_SCHEMA:
        raise DeploymentError("部署事务意图字段不闭合或 schema 非法。")
    clients = intent["clients"]
    if (
        not isinstance(clients, list)
        or not clients
        or any(client not in DEPLOY_CLIENTS for client in clients)
        or len(set(clients)) != len(clients)
    ):
        raise DeploymentError("部署事务意图的签收据客户端非法。")
    roots = [Path(str(intent[key])) for key in ("control_root", "production_data_root", "production_doc_root")]
    if any(not path.is_absolute() or ".." in path.parts for path in roots):
        raise DeploymentError("部署事务意图的目录坐标必须是绝对路径。")
    control_root, data_root, doc_root = roots
    transaction_root = intent["transaction_root"]
    receipt_paths = intent["receipt_paths"]
    if not isinstance(receipt_paths, dict) or set(receipt_paths) != set(clients):
        raise DeploymentError("部署事务意图的收据坐标与签收据客户端不一致。")
    for client, raw in receipt_paths.items():
        path = Path(str(raw))
        if path.parent != control_root or not fnmatch.fnmatchcase(path.name, DEPLOY_RECEIPT_GLOBS[client]):
            raise DeploymentError(f"部署事务意图的收据坐标非法：{raw}")
    seen: set[tuple[str, str]] = set()
    for group, key, _object_type in JOURNAL_GROUPS:
        entries = intent[group]
        if not isinstance(entries, list):
            raise DeploymentError(f"部署事务意图的 {group} 必须是列表。")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {key, *ENTRY_FIELDS}
                or not isinstance(entry[key], str)
                or not entry[key]
                or (group, entry[key]) in seen
            ):
                raise DeploymentError(f"部署事务意图的 {group} 条目字段不闭合或重复。")
            seen.add((group, entry[key]))
            production, candidate, backup = (Path(str(entry[field])) for field in ENTRY_PATH_FIELDS)
            if any(not path.is_absolute() or ".." in path.parts for path in (production, candidate, backup)):
                raise DeploymentError(f"部署事务意图的路径必须是规范绝对路径：{entry[key]}")
            old = entry["old_tree_sha256"]
            if (
                not isinstance(entry["old_exists"], bool)
                or entry["old_exists"] != (old is not None)
                or (old is not None and not SHA256_PATTERN.fullmatch(str(old)))
                or not SHA256_PATTERN.fullmatch(str(entry["new_tree_sha256"]))
            ):
                raise DeploymentError(f"部署事务意图的新旧摘要非法：{entry[key]}")
            if group == "documents":
                if (
                    not isinstance(transaction_root, str)
                    or not _within(production, doc_root)
                    or not _within(candidate, Path(transaction_root))
                    or not _within(backup, Path(transaction_root))
                ):
                    raise DeploymentError(f"部署事务意图的文档路径越界：{entry[key]}")
            elif candidate.parent != control_root or backup.parent != control_root or not _within(production, data_root):
                raise DeploymentError(f"部署事务意图的 {group} 路径越界：{entry[key]}")


def _validate_prepared_receipts(receipts: Any, intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """日志里的收据正文：客户端与意图逐一对应，坐标与意图相同，摘要等于正文落盘字节的摘要。"""

    if not isinstance(receipts, list) or [
        item.get("client") if isinstance(item, dict) else None for item in receipts
    ] != list(intent["clients"]):
        raise DeploymentError("日志里的收据正文与签收据的客户端不一致。")
    for item in receipts:
        if (
            set(item) != {"client", "path", "sha256", "payload"}
            or item["path"] != intent["receipt_paths"][item["client"]]
            or not isinstance(item["payload"], dict)
            or item["sha256"] != sha256_bytes(receipt_bytes(item["payload"]))
        ):
            raise DeploymentError(f"日志里的 {item.get('client')} 收据正文被改动或坐标不符。")
    return [dict(item) for item in receipts]


class SwitchJournal:
    """一份写前意图日志（JSON Lines）：第一行是完整意图，之后只追加阶段标记。

    意图在交换任何对象之前以一次性发布写入（临时文件 fsync → 硬链接到日志名 → fsync 目录），日志一旦出现就带着
    完整意图；标记行以 O_APPEND 追加并 fsync。每个标记行都带意图摘要，拼进别的事务的行会被识别。只有最后一行
    允许不完整（追加时进程中断），等同这个标记没有写成；``receipts_prepared`` 之外的标记缺失都不影响恢复判断。
    """

    def __init__(
        self,
        path: Path,
        intent: dict[str, Any],
        intent_sha256: str,
        markers: list[str],
        receipts: list[dict[str, Any]] | None,
    ) -> None:
        self.path = path
        self.intent = intent
        self.intent_sha256 = intent_sha256
        self.markers = markers
        self.receipts = receipts

    @property
    def receipts_prepared(self) -> bool:
        return self.receipts is not None

    @classmethod
    def create(cls, path: Path, intent: dict[str, Any]) -> "SwitchJournal":
        _validate_intent(intent)
        intent_sha256 = sha256_bytes(canonical(intent))
        record = {"record": "intent", "intent": intent, "intent_sha256": intent_sha256}
        _publish_bytes_once(path, canonical(record) + b"\n")
        return cls(path, intent, intent_sha256, [], None)

    @classmethod
    def load(cls, path: Path) -> "SwitchJournal":
        if path.is_symlink() or not path.is_file():
            raise DeploymentError(f"部署事务日志不是可信普通文件：{path}")
        lines = path.read_bytes().split(b"\n")
        records: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            last = index == len(lines) - 1
            if not line:
                if last:
                    continue
                raise DeploymentError(f"部署事务日志含空行：{path.name}")
            try:
                record = json.loads(line)
            except ValueError as error:
                if last and records:
                    break
                raise DeploymentError(f"部署事务日志损坏：{path.name} 第 {index + 1} 行") from error
            if not isinstance(record, dict):
                raise DeploymentError(f"部署事务日志行不是对象：{path.name} 第 {index + 1} 行")
            records.append(record)
        if not records or records[0].get("record") != "intent" or set(records[0]) != {"record", "intent", "intent_sha256"}:
            raise DeploymentError(f"部署事务日志第一行不是意图：{path.name}")
        intent = records[0]["intent"]
        _validate_intent(intent)
        intent_sha256 = sha256_bytes(canonical(intent))
        if records[0]["intent_sha256"] != intent_sha256:
            raise DeploymentError(f"部署事务意图摘要不符（意图被改动）：{path.name}")
        markers: list[str] = []
        receipts: list[dict[str, Any]] | None = None
        for record in records[1:]:
            if (
                record.get("record") != "marker"
                or record.get("intent_sha256") != intent_sha256
                or record.get("marker") not in SWITCH_JOURNAL_MARKERS
            ):
                raise DeploymentError(f"部署事务日志的阶段标记非法或不属于这份意图：{path.name}")
            markers.append(str(record["marker"]))
            if record["marker"] == "receipts_prepared":
                if receipts is not None:
                    raise DeploymentError(f"部署事务日志重复登记收据正文：{path.name}")
                receipts = _validate_prepared_receipts(record.get("receipts"), intent)
        return cls(path, intent, intent_sha256, markers, receipts)

    def append(self, marker: str, **fields: Any) -> None:
        if marker not in SWITCH_JOURNAL_MARKERS:
            raise DeploymentError(f"未知的阶段标记：{marker}")
        record = {"record": "marker", "marker": marker, "intent_sha256": self.intent_sha256, "at_utc": _iso_now(), **fields}
        if marker == "receipts_prepared":
            _validate_prepared_receipts(fields.get("receipts"), self.intent)
        line = canonical(record) + b"\n"
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            offset = 0
            while offset < len(line):
                offset += os.write(descriptor, line[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.markers.append(marker)
        if marker == "receipts_prepared":
            self.receipts = [dict(item) for item in fields["receipts"]]

    def remove(self) -> None:
        os.unlink(self.path)
        fsync_directory(self.path.parent)

    def retire(self) -> Path:
        """回滚完成后改名留作失败证据；新名字不再匹配活动日志模式，消费门禁随之放行。"""

        target = self.path.with_name(self.path.name + SWITCH_JOURNAL_RETIRED_SUFFIX)
        if target.exists() or target.is_symlink():
            target = self.path.with_name(f"{self.path.name}{SWITCH_JOURNAL_RETIRED_SUFFIX}-{secrets.token_hex(4)}")
        # 目标不存在已核对；control 目录由部署锁独占，没有并发写者。
        os.rename(self.path, target)
        fsync_directory(self.path.parent)
        return target


def intent_entry(key: str, name: str, object_type: str, production: Path, candidate: Path, backup: Path) -> dict[str, Any]:
    """按生产与候选的实际内容生成一个意图条目（写意图之前调用；候选此时已准备并核验）。"""

    if production.is_symlink():
        raise DeploymentError(f"生产路径是符号链接：{production}")
    old_exists = production.exists()
    old = object_sha256(production, object_type) if old_exists else None
    if old in {OBJECT_SYMLINK, OBJECT_TYPE_MISMATCH}:
        raise DeploymentError(f"生产对象类型与候选不一致：{production}")
    new = object_sha256(candidate, object_type)
    if new is None or new in {OBJECT_SYMLINK, OBJECT_TYPE_MISMATCH}:
        raise DeploymentError(f"候选对象缺失或类型非法：{candidate}")
    if backup.exists() or backup.is_symlink():
        raise DeploymentError(f"回滚备份路径已存在，拒绝覆盖：{backup}")
    return {
        key: name,
        "production_path": str(production),
        "candidate_path": str(candidate),
        "backup_path": str(backup),
        "old_exists": old_exists,
        "old_tree_sha256": old,
        "new_tree_sha256": new,
    }


def document_intent_entries(
    transaction_root: Path,
    production_doc_root: Path,
    documents: tuple[str, ...],
    runtime_documents: tuple[str, ...],
) -> list[dict[str, Any]]:
    """活动文档、运行时依赖文档与 repository-docs 归档副本的意图条目（改造前 _switch_documents 的同一组对象）。

    Codex 的两份活动文档与其归档副本必须已在生产存在（改造前同样要求，缺失时部署失败）；其它文档允许首次纳管。
    生产没有 repository-docs 目录时不处理归档副本，与改造前相同。
    """

    candidate_root = transaction_root / "candidates"
    backup_root = transaction_root / "backups"
    entries: list[dict[str, Any]] = []
    for name in (*documents, *runtime_documents):
        production = managed_document_path(production_doc_root, name, label="生产文档", allow_missing=True)
        candidate = managed_document_path(candidate_root, name, label="文档候选", allow_missing=False)
        backup = managed_document_path(backup_root, name, label="文档回滚副本", allow_missing=True)
        entry = intent_entry("document", name, "file", production, candidate, backup)
        if name in MANAGED_DOCUMENTS and not entry["old_exists"]:
            raise DeploymentError(f"生产活动文档缺失：{name}")
        entries.append(entry)
    production_archive_root = production_doc_root / "repository-docs"
    candidate_archive_root = candidate_root / "repository-docs"
    if production_archive_root.is_dir() and not production_archive_root.is_symlink() and candidate_archive_root.is_dir():
        for name in documents:
            entry = intent_entry(
                "document",
                ARCHIVE_DOCUMENT_PREFIX + name,
                "file",
                production_archive_root / name,
                candidate_archive_root / name,
                backup_root / "repository-docs" / name,
            )
            if name in MANAGED_DOCUMENTS and not entry["old_exists"]:
                raise DeploymentError(f"生产归档文档缺失：{name}")
            entries.append(entry)
    return entries


def _prepare_switch_parents(entry: Mapping[str, Any]) -> None:
    """切入一个文档前准备父目录：活动与运行时文档的生产父目录、全部文档的回滚父目录（root、0700，同改造前）。"""

    parents = [Path(str(entry["backup_path"])).parent]
    if not str(entry["document"]).startswith(ARCHIVE_DOCUMENT_PREFIX):
        parents.insert(0, Path(str(entry["production_path"])).parent)
    for parent in parents:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(parent, 0, 0)
        os.chmod(parent, 0o700)


def switch_intent_entry(group: str, entry: Mapping[str, Any]) -> str:
    """按意图切入一个对象，返回 switched（交换并备份旧对象）或 installed（原路径不存在，不覆盖地移入）。

    ``old_exists=true`` 沿用改造前的两步：先原子交换候选与生产，再把换到候选位置的旧对象移到回滚位置；两步之间
    旧对象在候选区、备份还不存在，恢复按摘要在候选区找到它。``old_exists=false`` 用不覆盖改名移入生产。
    回滚副本的属主与权限处理与改造前相同：工具树与断言预处理器去掉 group/other 写，归档文档 0644，活动文档不动。
    """

    production, candidate, backup = (Path(str(entry[field])) for field in ENTRY_PATH_FIELDS)
    if not entry["old_exists"]:
        rename_noreplace(candidate, production)
        return "installed"
    if backup.exists() or backup.is_symlink():
        raise DeploymentError(f"回滚备份路径已存在，拒绝覆盖：{backup}")
    atomic_exchange(candidate, production)
    fsync_directory(production.parent)
    os.rename(candidate, backup)
    if group != "documents":
        os.chown(backup, 0, 0)
        os.chmod(backup, stat.S_IMODE(backup.stat().st_mode) & ~0o022)
    elif str(entry["document"]).startswith(ARCHIVE_DOCUMENT_PREFIX):
        os.chown(backup, 0, 0)
        os.chmod(backup, 0o644)
    fsync_directory(candidate.parent)
    fsync_directory(backup.parent)
    return "switched"


def switch_intent_group(journal: SwitchJournal, group: str) -> list[dict[str, str]]:
    key = next(name_key for name, name_key, _object_type in JOURNAL_GROUPS if name == group)
    rows: list[dict[str, str]] = []
    for entry in journal.intent[group]:
        if group == "documents":
            _prepare_switch_parents(entry)
        rows.append({"object": str(entry[key]), "result": switch_intent_entry(group, entry)})
    return rows


def entry_decision(object_type: str, entry: Mapping[str, Any]) -> dict[str, str | None]:
    """按生产路径的实际内容判定一个条目处在哪一步（设计 B5a-01 判定表），只读。"""

    production, candidate, backup = (Path(str(entry[field])) for field in ENTRY_PATH_FIELDS)
    actual = object_sha256(production, object_type)
    old, new = entry["old_tree_sha256"], entry["new_tree_sha256"]
    if entry["old_exists"]:
        if actual is not None and actual == old:
            return {"decision": "at-old", "action": None}
        if actual == new:
            # 已交换：按摘要定位旧副本，先看候选区（交换完成、备份尚未移入时中断），再看备份位置。
            if object_sha256(candidate, object_type) == old:
                return {"decision": "switched", "action": "exchange-candidate"}
            if object_sha256(backup, object_type) == old:
                return {"decision": "switched", "action": "exchange-backup"}
            return {"decision": "old-copy-missing", "action": None}
        return {"decision": "undecidable", "action": None}
    if actual is None:
        return {"decision": "absent", "action": None}
    if actual == new:
        if candidate.exists() or candidate.is_symlink():
            return {"decision": "candidate-occupied", "action": None}
        return {"decision": "installed", "action": "move-back"}
    return {"decision": "undecidable", "action": None}


def _apply_recovery_action(entry: Mapping[str, Any], action: str) -> None:
    production, candidate, backup = (Path(str(entry[field])) for field in ENTRY_PATH_FIELDS)
    if action == "exchange-candidate":
        atomic_exchange(candidate, production)
        fsync_directory(production.parent)
    elif action == "exchange-backup":
        atomic_exchange(backup, production)
        fsync_directory(production.parent)
    elif action == "move-back":
        rename_noreplace(production, candidate)
    else:
        raise DeploymentError(f"未知的恢复动作：{action}")


def restore_switch_journal(journal: SwitchJournal, *, reason: str) -> dict[str, Any]:
    """把意图里的全部对象按实际内容恢复到部署前；幂等，新产物留在候选区或备份位置，不删除。

    先对全部条目只读判定：任一条目无法判定或找不到旧副本即失败关闭，一个对象都不动、日志保持活动名交人工。
    之后追加 ``rollback_begin``，逆序执行各条目的回滚动作（单项失败不阻止其余对象），再复判一次，全部落在
    「无动作」两行才追加 ``rollback_done`` 并把日志改名留证。中途中断时日志仍是活动名，再次恢复时已回滚的对象
    按内容落到「无动作」，不会重复交换。
    """

    plan = [
        (name, object_type, entry, entry_decision(object_type, entry))
        for _group, name, object_type, entry in reversed(_journal_entries(journal.intent))
    ]
    blocked = [f"{name}（{decision['decision']}）" for name, _type, _entry, decision in plan
               if decision["decision"] in RECOVERY_BLOCKED]
    if blocked:
        raise DeploymentError("部署事务无法按内容判定，失败关闭、未动任何对象，交人工：" + "；".join(blocked[:20]))
    journal.append("rollback_begin", reason=reason)
    failures: list[str] = []
    for name, _object_type, entry, decision in plan:
        if decision["action"] is None:
            continue
        try:
            _apply_recovery_action(entry, str(decision["action"]))
        except BaseException as error:  # noqa: BLE001 - 单项失败不得阻止其余对象回滚
            failures.append(f"{name}: {type(error).__name__}")
    if failures:
        raise DeploymentError("部署回滚不完整：" + "; ".join(failures))
    unsettled = [name for name, object_type, entry, _decision in plan
                 if entry_decision(object_type, entry)["decision"] not in RECOVERY_SETTLED]
    if unsettled:
        raise DeploymentError("回滚后仍有对象不在部署前状态：" + "；".join(unsettled[:20]))
    restored = [{"object": name, "action": decision["action"]} for name, _type, _entry, decision in plan
                if decision["action"] is not None]
    journal.append("rollback_done", restored=restored)
    retired = journal.retire()
    return {
        "rollback": "completed",
        "journal": retired.name,
        "restored": [item["object"] for item in restored],
        "unchanged_count": len(plan) - len(restored),
    }


def complete_switch_journal(journal: SwitchJournal) -> dict[str, Any]:
    """收据正文已入日志的残留：核对每个对象都是新版本，按日志正文补齐缺失的收据、核对已有收据，再删日志。

    任一对象不是新版本、或已写出的收据与日志正文摘要不同，即失败关闭（不回滚：收据可能已经写出，回滚会让
    收据描述一棵不存在的树）。
    """

    mismatched = [
        name
        for _group, name, object_type, entry in _journal_entries(journal.intent)
        if object_sha256(Path(str(entry["production_path"])), object_type) != entry["new_tree_sha256"]
    ]
    if mismatched:
        raise DeploymentError(
            "收据正文已入日志，但生产内容不是本次事务的新版本，失败关闭交人工：" + "；".join(mismatched[:20])
        )
    written: list[str] = []
    verified: list[str] = []
    for item in journal.receipts or []:
        path = Path(str(item["path"]))
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or file_sha256(path) != item["sha256"]:
                raise DeploymentError(f"已写出的部署收据与日志正文不一致，失败关闭交人工：{path.name}")
            verified.append(path.name)
            continue
        write_json_once(path, item["payload"])
        if file_sha256(path) != item["sha256"]:
            raise DeploymentError(f"补写的部署收据摘要不符：{path.name}")
        written.append(path.name)
    journal.remove()
    return {
        "outcome": "receipts-completed",
        "journal": journal.path.name,
        "written_receipts": written,
        "verified_receipts": verified,
    }


def pending_switch_journals(control_root: Path) -> list[Path]:
    return sorted(control_root.glob(SWITCH_JOURNAL_GLOB))


def recover_switch_journal(control_root: Path) -> dict[str, Any]:
    """启动时处理残留的事务日志（设计 B5a-01 三种残留）。

    ① 有日志、没有 ``receipts_prepared``：按内容把全部对象恢复到部署前，日志改名留证（调用方随后拒绝本次部署）；
    ② 有日志、有 ``receipts_prepared``：核对新版本、补齐收据、删日志，不回滚；③ 没有日志：无事可做。
    多份活动日志说明有人绕过了部署锁，失败关闭交人工。
    """

    journals = pending_switch_journals(control_root)
    if not journals:
        return {"outcome": "none"}
    if len(journals) > 1:
        raise DeploymentError("control 目录有多份未完成的部署事务日志，失败关闭交人工：" + "、".join(path.name for path in journals))
    journal = SwitchJournal.load(journals[0])
    if journal.receipts_prepared:
        return complete_switch_journal(journal)
    return {"outcome": "rolled-back", **restore_switch_journal(journal, reason="startup-recovery")}


def verify_switched_entries(journal: SwitchJournal) -> dict[str, Any]:
    """全局一致性核验的第一项：意图里的每个对象在生产路径上都已是新版本。"""

    mismatched = [
        name
        for _group, name, object_type, entry in _journal_entries(journal.intent)
        if object_sha256(Path(str(entry["production_path"])), object_type) != entry["new_tree_sha256"]
    ]
    if mismatched:
        raise DeploymentError("切换后的生产内容与意图的新版本不一致：" + "；".join(mismatched[:20]))
    return {"switched_objects": len(_journal_entries(journal.intent))}


def _verify_root_candidate(candidate: Path, staging_path: Path) -> Mapping[str, Any]:
    """非 Codex 根的候选核验：内容与暂存根逐项相同（不计字节码），属主与写权限可信。"""

    digest = tree_content_sha256(candidate)
    if digest != tree_content_sha256(staging_path):
        raise DeploymentError(f"受管根候选与暂存树内容不一致：{candidate.name}")
    reject_untrusted_tree(candidate)
    return {"candidate_tree_sha256": digest}


def _prepare_transaction_documents(
    staging_doc_root: Path,
    production_doc_root: Path,
    transaction_root: Path,
    documents: tuple[str, ...],
    runtime_documents: tuple[str, ...],
) -> Mapping[str, Any]:
    """按事务的文档集合准备候选与回滚目录（_prepare_document_candidates 的按客户端推广）。

    只签 Codex 时文档集合就是 MANAGED_DOCUMENTS 与 MANAGED_RUNTIME_DOCUMENTS，做法与返回值同改造前；Codex 文档要求
    生产已存在（按旧文档规则冻结事实），其它活动文档首次纳管时允许不存在，存在则同样核对写权限。
    """

    if transaction_root.exists() or transaction_root.is_symlink():
        raise DeploymentError("文档事务目录已存在，拒绝覆盖。")
    candidate_root = transaction_root / "candidates"
    candidate_archive_root = candidate_root / "repository-docs"
    backup_root = transaction_root / "backups"
    candidate_root.mkdir(mode=0o700, parents=True)
    candidate_archive_root.mkdir(mode=0o700)
    backup_root.mkdir(mode=0o700)
    for directory in (transaction_root, candidate_root, candidate_archive_root, backup_root):
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o700)
    digests: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    for name in documents:
        source = staging_doc_root / name
        production = production_doc_root / name
        reject_untrusted_file(source, label="暂存活动文档")
        if name in MANAGED_DOCUMENTS or production.exists() or production.is_symlink():
            legacy_production_document_facts(production)
        destination = candidate_root / name
        archive_destination = candidate_archive_root / name
        for target in (destination, archive_destination):
            shutil.copyfile(source, target)
            os.chown(target, 0, 0)
            os.chmod(target, 0o644)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
        if file_sha256(destination) != file_sha256(source):
            raise DeploymentError(f"文档候选复制后摘要漂移：{name}")
        if file_sha256(archive_destination) != file_sha256(source):
            raise DeploymentError(f"文档归档候选复制后摘要漂移：{name}")
        digests[name] = file_sha256(destination)
    for name in runtime_documents:
        source = managed_document_path(staging_doc_root, name, label="暂存运行时依赖文档", allow_missing=False)
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
        runtime_document_bindings.append({"path": name, "sha256": file_sha256(destination)})
    for directory in (candidate_archive_root, candidate_root, backup_root, transaction_root, production_doc_root):
        fsync_directory(directory)
    return {
        "transaction_root": transaction_root.name,
        "document_sha256": digests,
        "runtime_document_bindings": runtime_document_bindings,
    }


def _preflight_claude(
    client: Any,
    staging_root: Path,
    production_doc_root: Path,
    production_data_root: Path,
    managed_roots: list[str],
    expected_supervisor_digest: str,
    *,
    include_environment: bool,
) -> dict[str, Any]:
    """签 Claude 收据前的预检：Claude 文档、两个受管根、pre-A3 Claude 场景表；只签 Claude 时另做环境前提。

    未在本次交换的 Claude 受管根必须已在生产存在且与暂存树逐项相同，否则暂存树算出的身份不等于部署后的生产
    身份，收据会失真——这时要把它加进 ``--managed-roots`` 一起交换。
    """

    if production_doc_root.is_symlink() or not production_doc_root.is_dir():
        raise DeploymentError("生产活动文档目录不存在或不可信。")
    if production_doc_root.stat().st_uid != 0 or production_doc_root.stat().st_mode & 0o022:
        raise DeploymentError("生产活动文档目录属主或写权限不安全。")
    repository_docs = staging_root / "docs" / "repository-docs"
    if repository_docs.is_symlink() or not repository_docs.is_dir():
        raise DeploymentError("暂存文档归档目录不存在或不可信。")
    document_sha256: dict[str, str] = {}
    legacy_documents: dict[str, Any] = {}
    for name in CLAUDE_MANAGED_DOCUMENTS:
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
        production = production_doc_root / name
        legacy_documents[name] = (
            {"status": "present", **legacy_production_document_facts(production)}
            if production.exists() or production.is_symlink()
            else {"status": "absent"}
        )
    runtime_document_bindings: list[dict[str, str]] = []
    for name in CLAUDE_MANAGED_RUNTIME_DOCUMENTS:
        runtime_document = managed_document_path(staging_root / "docs", name, label="暂存运行时依赖文档", allow_missing=False)
        reject_untrusted_file(runtime_document, label="暂存运行时依赖文档")
        runtime_document_bindings.append({"path": name, "sha256": file_sha256(runtime_document)})
    roots: dict[str, str] = {}
    for root in CLIENT_MANAGED_ROOTS["claude"]:
        staging_path = staging_root / root
        reject_untrusted_tree(staging_path)
        production_path = production_data_root / root
        if root in managed_roots:
            if production_path.exists() or production_path.is_symlink():
                reject_untrusted_tree(production_path)
            roots[root] = "switched"
            continue
        if production_path.is_symlink() or not production_path.is_dir():
            raise DeploymentError(f"Claude 受管根 {root} 不在本次交换范围、生产上也不存在：把它加入 --managed-roots。")
        reject_untrusted_tree(production_path)
        if tree_content_sha256(production_path) != tree_content_sha256(staging_path):
            raise DeploymentError(f"Claude 受管根 {root} 的生产现状与暂存树不一致：把它加入 --managed-roots 一起交换。")
        roots[root] = "unchanged"
    result: dict[str, Any] = {
        "document_sha256": document_sha256,
        "legacy_documents": legacy_documents,
        "runtime_document_bindings": runtime_document_bindings,
        "roots": roots,
        "pre_a3": verify_claude_pre_a3_entries(staging_root / CAPTURE_ROOT),
    }
    if include_environment:
        # 只签 Claude 时 Codex 预检不运行，暂存监督器摘要与运行环境前提在这里核对（与 Codex 预检同一组命令）。
        if file_sha256(staging_root / CAPTURE_ROOT / "codex_upgrade_supervisor.py") != expected_supervisor_digest:
            raise DeploymentError("暂存监督器摘要不符。")
        result["capture_ip"] = parse_container_network(
            run_checked(client, "enable:inspect-capture-network",
                        ["docker", "inspect", "capture-cli", "--format", "{{json .NetworkSettings.Networks}}"]),
            "capture-cli",
        )
        result["service_ip"] = parse_container_network(
            run_checked(client, "enable:inspect-service-network",
                        ["docker", "inspect", "sub2apiplus", "--format", "{{json .NetworkSettings.Networks}}"]),
            "sub2apiplus",
        )
        result["runtime_egress"] = verify_runtime_egress()
    return result


def observe_runtime_bindings(data_root: Path, bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude 策略登记的受管运行时（相对数据根）在生产上的观测：不存在如实记 absent（B5a-03 安装之前）；
    存在却与冻结摘要不同即失败关闭——control_sha256 已把冻结值算进身份，运行时不符时身份不再如实。"""

    rows: list[dict[str, Any]] = []
    for item in bindings:
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise DeploymentError(f"Claude 运行时绑定路径非法：{item['path']}")
        path = data_root / relative
        if path.is_symlink() or not path.exists():
            rows.append({**item, "observed_sha256": None, "status": "absent"})
            continue
        if not path.is_file():
            raise DeploymentError(f"Claude 运行时绑定不是普通文件：{path}")
        observed = file_sha256(path)
        if observed != item["sha256"]:
            raise DeploymentError(f"Claude 运行时绑定摘要与策略冻结值不一致：{item['name']}")
        rows.append({**item, "observed_sha256": observed, "status": "matched"})
    return rows


def _post_switch_verify_claude(client: Any, production_data_root: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """切换后用生产树里的 v3 引擎复算 Claude 身份，必须与预检时暂存树的结果逐字相等；再做 Claude 冒烟。"""

    actual = claude_identity(production_data_root)
    mismatched = [field for field in CLAUDE_IDENTITY_FIELDS if actual[field] != expected[field]]
    if actual["roots"] != expected["roots"]:
        mismatched.append("roots")
    if actual["runtime_bindings"] != expected["runtime_bindings"]:
        mismatched.append("runtime_bindings")
    if mismatched:
        raise DeploymentError(f"切换后生产树的 Claude 身份与暂存树不一致：{mismatched}")
    return {
        "identity": {field: actual[field] for field in CLAUDE_IDENTITY_FIELDS},
        "roots": actual["roots"],
        "client_binaries": probe_claude_binaries(client, production_data_root / CAPTURE_ROOT),
        "runtime_bindings": observe_runtime_bindings(production_data_root, actual["runtime_bindings"]),
    }


def codex_document_facts(intent: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Codex 收据的两项文档事实，只取 Codex 的文档（VC-0 收口按 Codex 清单逐项核对）。

    ``switched_archived_documents``：按 MANAGED_DOCUMENTS 顺序列出交换过的归档副本；``installed_runtime_documents``：
    按改造前的顺序列出首次纳管（部署前不存在）的 Codex 活动与运行时文档。
    """

    by_name = {str(entry["document"]): entry for entry in intent["documents"]}
    archived = [
        name for name in MANAGED_DOCUMENTS
        if bool(by_name.get(ARCHIVE_DOCUMENT_PREFIX + name, {}).get("old_exists"))
    ]
    installed = [
        name for name in (*MANAGED_DOCUMENTS, *MANAGED_RUNTIME_DOCUMENTS)
        if name in by_name and not by_name[name]["old_exists"]
    ]
    return archived, installed


def build_codex_receipt(
    *,
    campaign_id: str,
    production_root: Path,
    production_doc_root: Path,
    expected_tool_digest: str,
    identity_v2: Mapping[str, Any],
    expected_supervisor_digest: str,
    expected_assertion_preparer_digest: str,
    backup: Path | None,
    assertion_preparer_backup: Path | None,
    transaction_root: Path | None,
    switched_archived_documents: list[str],
    installed_documents: list[str],
    supervisor_run_dir: Any,
    ledger_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Codex 部署收据：字段、取值与改造前 main 里的字面量逐项相同（codex-arm64-supervisor-enable/v1，22 项）。"""

    return {
        "schema_version": CODEX_DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "campaign_id": campaign_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "architecture": platform.machine(),
        "production_tool_root": str(production_root),
        "production_doc_root": str(production_doc_root),
        "tool_files_sha256": expected_tool_digest,
        "policy_version": identity_v2["policy_version"],
        "policy_sha256": identity_v2["policy_sha256"],
        "wire_producer_sha256": identity_v2["wire_producer_sha256"],
        "evidence_semantics_sha256": identity_v2["evidence_semantics_sha256"],
        "control_sha256": identity_v2["control_sha256"],
        "supervisor_sha256": expected_supervisor_digest,
        "assertion_preparer_sha256": expected_assertion_preparer_digest,
        "rollback_backup": str(backup) if backup else None,
        "assertion_preparer_rollback_backup": str(assertion_preparer_backup) if assertion_preparer_backup else None,
        "document_rollback_backup": str(transaction_root / "backups") if transaction_root else None,
        "switched_archived_documents": list(switched_archived_documents),
        "installed_runtime_documents": list(installed_documents),
        "supervisor_run_dir": str(supervisor_run_dir),
        # B9：项目总账 head 摘要；生产数据根下没有总账时记录为 null。
        "project_ledger": ledger_summary or None,
    }


def build_claude_receipt(
    *,
    campaign_id: str,
    journal: SwitchJournal,
    verification: Mapping[str, Any],
    pre_a3: Mapping[str, Any],
    expected_supervisor_digest: str,
    supervisor_run_dir: Any,
    transaction_receipts: list[dict[str, str]],
) -> dict[str, Any]:
    """Claude 部署收据（official-client-managed-deploy/v1）：身份取切换后复算的结果（已与暂存树逐字核对）。"""

    intent = journal.intent
    by_root = {str(entry["root"]): entry for entry in intent["roots"]}
    managed_roots = [
        {
            "root": root,
            "tool_files_sha256": verification["roots"][root]["tool_files_sha256"],
            "rollback_backup": (
                str(by_root[root]["backup_path"]) if root in by_root and by_root[root]["old_exists"] else None
            ),
        }
        for root in CLIENT_MANAGED_ROOTS["claude"]
    ]
    claude_documents = {*CLAUDE_MANAGED_DOCUMENTS, *CLAUDE_MANAGED_RUNTIME_DOCUMENTS}
    documents = [
        {
            "document": str(entry["document"]),
            "sha256": entry["new_tree_sha256"],
            "rollback_backup": str(entry["backup_path"]) if entry["old_exists"] else None,
        }
        for entry in intent["documents"]
        if str(entry["document"]).removeprefix(ARCHIVE_DOCUMENT_PREFIX) in claude_documents
    ]
    notes: list[str] = []
    for root in CLIENT_MANAGED_ROOTS["claude"]:
        entry = by_root.get(root)
        if entry is None:
            notes.append(f"{root} 未在本次事务交换：部署前已核对生产现状与暂存树逐项相同")
        elif not entry["old_exists"]:
            notes.append(f"{root} 首次纳管：部署前生产路径不存在，回滚是把新树移回 {entry['candidate_path']}")
    return {
        "schema_version": CLAUDE_DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "client": "claude",
        "campaign_id": campaign_id,
        "created_at_utc": _iso_now(),
        "architecture": platform.machine(),
        "production_data_root": intent["production_data_root"],
        "production_doc_root": intent["production_doc_root"],
        "identity": dict(verification["identity"]),
        "managed_roots": managed_roots,
        "runtime_bindings": list(verification["runtime_bindings"]),
        "switch_journal_sha256": journal.intent_sha256,
        "supervisor_sha256": expected_supervisor_digest,
        "supervisor_run_dir": str(supervisor_run_dir),
        "documents": documents,
        "client_binaries": dict(verification["client_binaries"]),
        "pre_a3": dict(pre_a3),
        "transaction_receipts": list(transaction_receipts),
        "notes": notes,
    }


def write_prepared_receipts(prepared: list[dict[str, Any]]) -> dict[str, Any]:
    """按日志里的正文逐份 write-once 写出收据（正文与摘要已先入日志，中断后下次启动补齐）。"""

    rows = []
    for item in prepared:
        path = Path(str(item["path"]))
        size = write_json_once(path, item["payload"])
        if file_sha256(path) != item["sha256"]:
            raise DeploymentError(f"写出的部署收据摘要与日志正文不符：{path.name}")
        rows.append({"client": item["client"], "name": path.name, "bytes": size})
    return {"bytes": sum(row["bytes"] for row in rows), "receipts": rows}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        print(f"ARM64 监督部署失败：{error}", file=sys.stderr)
        raise SystemExit(1)
