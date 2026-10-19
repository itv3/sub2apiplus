"""Codex 身份守恒门禁（Claude 升级控制面设计 D7、B1-07）。

Claude 升级控制面改造的 wire 层、evidence 层、两个编排器闭包和 checker／builder 摘要必须与改造开工前
的基线逐字相等；control 层、策略文件与整树摘要的变化必须逐文件落在 ``REGISTERED_CONTROL_CHANGES`` 登记的修改
清单里，且不得出现按默认层归类的文件；迁入共享模块的函数与常量不得属于任何身份闭包。

读侧保留 B5a-08 两个钉住常量的原登记；其后已批准的 D4 单点静态证明、r2 后继顺序元数据和原设计恢复接口，另用
SHA 钉死的过渡原件登记逐符号及整文件前后值。B5a-09／10 测试单元读集接线再次改变两个钉住常量，只由独立后继
精确登记；先逆向回到不变的 10-08 原图，再逐步逆向回到 B5a-08 的原图并还原两个旧常量，
最终与原始基线图逐字段、逐摘要相同。D4 恢复的 23／35 项是旧证明依赖，不是可删除的兼容例外；未来未登记变化照常拒绝。
其后三次门禁收据生成器的登记（H3 收尾的只读重放登记、步骤 8 的三个钉住值与旧生成器登记、步骤 10 的 read_audit／read_audit_runtime 两个钉住值与旧生成器登记）
各由一份独立后继精确登记，逆向链依次回到前一份的后图。

evidence 层另有一份独立的过渡登记（``fixtures/codex_identity_conservation_evidence_transition_20261019.json``，SHA 钉死）：
Codex 候选测试事实映射 ``candidate_test_fact_map_0_160_0.json`` 里 ``openai_gateway_forward.go`` 的源码快照先后被主干修复与
上游 v0.2.17 合并重绑，映射摘要常量 ``FROZEN_MAPPING_SHA256`` 所在的 ``candidate_test_trace.py`` 同步变化，两个文件都归
evidence_semantics 层，基线的 ``evidence_semantics_sha256`` 因此改变。登记只放“基线→当前”的累计替换，测试做三件事：
字节级逆向（把当前文件里每处登记的后值文本换回前值，重算的文件摘要必须等于基线记录的文件摘要）；结构证明（事实映射除登记的
源码快照 sha256 字段外逐叶不变，映射摘要常量文件除注释外只有一条随映射文件摘要同步的赋值）；摘要复算（两个文件换回基线摘要后
重算的 ``evidence_semantics_sha256`` 必须等于基线）。该登记只放行这两个文件，绝不写进 ``REGISTERED_CONTROL_CHANGES`` 冒充
control 层；缺一条、多一条、改一条或文件另有改动都拒绝，再次重绑必须重新生成登记并另行审核。

基线由 B1-01 开工前的 main（见基线的 ``source_commit``）算出，固化在
``fixtures/codex_identity_conservation_baseline.json``；本项目各批不得改动基线，文件摘要钉在 ``BASELINE_SHA256``。
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "codex_identity_conservation_baseline.json"
BASELINE_SHA256 = "e4310febbaeae49e74bbbe32b0093e7866d3c2302ead04db5d35c309d37814a1"
SHARED_CONTROL_PLANE = "upgrade_control_plane.py"

# 本项目登记的 Codex 受管树变化：受管树相对路径 → 工作项与理由。新增、删除、修改都要登记，且只能落在
# control 层。被 Codex 策略忽略的文件（``claude_`` 前缀等 Claude 专属文件）不影响 Codex 身份，无须登记。
REGISTERED_CONTROL_CHANGES: dict[str, str] = {
    "tool_identity_policy_v2.json": "B1-07 策略 v8：三个共享新文件登记进 control 层，先于文件本身进入受管树",
    "upgrade_client_adapter.py": "B1-01 客户端适配接口、编排器服务面与注册表；B2-08 新增三个适配器成员（attempt 环境已恢复口径、部署收据五摘要"
                                 "所在对象、有效 wire 身份），Codex 绑定到原写法或原函数；B2-10 新增 Job 演练的四个适配器成员（画像模块、"
                                 "执行合同构造、二进制表、合同重放），Codex 绑定到 Job 演练收据模块本身与它的原函数体；B2-11 新增"
                                 "收口画像成员 closeout_profile（模块型），Codex 绑定到 VC-0 收口模块本身；B5a-09 续新增原子双跑画像成员 "
                                 "atomic_rehearsal_profile（模块型），Codex 绑定到原子双跑收据模块本身；VC-1 续跑路径新增两个成员"
                                 "（official_post_run_tooling_item_ids 值型、official_attempt_free_post_run_action 可调用型），Codex "
                                 "绑定到监督器原常量 OFFICIAL_POST_RUN_TOOLING_ITEM_IDS 与恒为假的新函数；B5b-01 新增 VC-5 准入的两个"
                                 "可调用成员（catalog_stage_profile、vc5_official_campaign）与它们共用的静态错误码异常和路径／JSON 读取"
                                 "小工具，Codex 绑定到 codex_upgrade_client_adapter 里原样搬入的驱动函数体；B4-07 收尾新增可调用成员 "
                                 "candidate_post_run_extra_action（候选级共享闭集之外的零请求后处理动作），Codex 绑定到恒为假的新函数；"
                                 "B4-05b-1 新增值型成员 "
                                 "candidate_recovery_identity_flags（候选续跑命令的身份参数），Codex 绑定到监督器原常量 "
                                 "CANDIDATE_RECOVERY_IDENTITY_FLAGS；r2 增加 candidate_successor_protocol_order 元数据，Codex 返回原 tuple；"
                                 "B4-05／B5b-03 后继增加候选成功前段承接和新基线 compare 绑定元数据，Codex 前段承接恒 False、compare 条件保持原行为；"
                                 "注册表两常量及整文件的实际读侧变化见 SHA 钉死的精确过渡原件",
    "codex_upgrade_vc0_closeout.py": "B2-11 收口流程加客户端作用域（adapter_scope，缺省 Codex）：共享流程里 12 个编排器引用经服务面"
                                     "（Codex 的 orchestrator 就是 codex_upgrade 模块本身，测试对它的替身照常生效），五个合同名与两个画像函数"
                                     "（validate_inputs、recover_formal_plan_arguments）经收口画像（Codex 画像是本模块，取值与原值逐字相同），"
                                     "两处项目总账查找显式给出客户端（Codex 给 codex，与缺省相同），新增命令提示用的 ORCHESTRATOR_CLI_MODULE；"
                                     "Codex 专属的输入校验、Formal 参数恢复与两个历史修复子命令原样不动。本模块没有符号在评估读侧闭包内",
    "codex_upgrade_campaign_run_rehearsal_receipt.py": "B5a-09 续 原子双跑加客户端作用域（client_scope，缺省 Codex）：7 处编排器"
                                                       "引用经服务面（Codex 服务面就是 codex_upgrade 模块本身，不再直接导入），其余"
                                                       "客户端差异经画像（Codex 画像是本模块，常量与两个画像函数取值是改造前写死的"
                                                       "原值），计时账本与测试用总账显式给出客户端（Codex 给 codex，与缺省相同）；"
                                                       "原子双跑与 worker 命令行加 --client（缺省 codex），分批演练不变。本模块没有"
                                                       "符号在评估读侧闭包内；Codex 的收据、清单、inventory 与报错逐字不变（"
                                                       "test_codex_upgrade_campaign_run_rehearsal_receipt 照常通过）",
    "codex_upgrade_job_rehearsal_receipt.py": "B2-10 Job 演练收据模块加适配器参数：13 个编排器引用经服务面，执行合同构造、二进制表与合同"
                                              "重放经适配器成员，其余客户端差异经画像（Codex 画像就是本模块，各项取值是改造前写死的原值），"
                                              "四个依赖与容器探针加缺省为原值的参数；facts、收据与合同按合同名认客户端，Claude 签 "
                                              "official-client-job-rehearsal-*；命令行加 --client（缺省 codex）。本模块没有符号在评估读侧闭包内；"
                                              "Codex 的合同、facts 校验、收据、重放与报错逐字不变（test_claude_upgrade_job_rehearsal 的 Codex 对照）；"
                                              "5a 收口只改 assert_formal_compatible 的 docstring（两客户端都要求存储路由与失败生命周期两项探针摘要），"
                                              "代码不变",
    "codex_upgrade_client_adapter.py": "B1-01 Codex 适配器成员取值（逐字等于改造前写死的名字）；B2-08 新增两个成员函数，取值逐字等于对账器原写法"
                                       "（after 探针加恢复收据、部署收据顶层五摘要）；VC-1 续跑路径新增 official_attempt_free_post_run_action"
                                       "（恒为假：Codex 的 VC-1 seal 链动作都指向同一个官方 attempt）；B5b-01 搬入驱动 vc5_admission 原有的"
                                       "两段 Codex 逻辑（ManagedFacts.catalog 的画像与 VC-3／VC-4 收据重放、Admission.inspect 的 reuse 分支），"
                                       "候选用途照旧写死、错误码不变，不被 codex_upgrade 引用，不进编排器闭包与评估读侧闭包；B4-07 收尾新增"
                                       "恒为假的 candidate_post_run_extra_action（Codex 没有共享闭集之外的候选级零请求后处理动作）；"
                                       "r2 的协议顺序缺省元数据为 None，监督器返回原 tuple 对象；B4-05 恢复前段承接恒 False，"
                                       "COMPARISON_IS_BASELINE_BOUND 为 False，Codex 原 compare 复用条件不变",
    "codex_upgrade_supervisor.py": "B1-02 监督器解耦：编排器符号与客户端名字经适配器，Claude 清单合同；B3-06 阶段审核后继协议"
                                   "（_validate_batched_stage_review_successor，读侧闭包外）只对正要派发的直接后继复算实物，已执行过的"
                                   "历史后继只重放冻结许可（原先阶段内还有后续批次时，成功的后继不写账本事件，复算看到后继自己的产物"
                                   "误判漂移，本阶段后续批次全被拒；直接后继的判定与报错不变）；10-07 staging 派发门禁偶发的产品侧修复："
                                   "stop 等 monitor 收口时，monitor 还没写出首条 watchdog 心跳（冷启动）就最多等到父 run 起动后 30 秒"
                                   "（DEFAULT_MONITOR_STARTUP_GRACE_SECONDS、_monitor_watchdog_started），已写出的仍按 2 秒判卡住、到时处理不变；"
                                   "campaign-run 返回值以落盘终态为准，与请求不一致时附 requested_terminal（_sealed_terminal_reason）。"
                                   "VC-1 续跑路径：_official_post_run_tooling_facts 与 _validate_batched_seal_chain_successor 的阶段项闭集、"
                                   "不绑定 attempt 的零请求后处理动作改经适配器按清单归属客户端取（Codex 取原常量、判定恒为假，判定、"
                                   "事实结构与原因文案逐字不变）；常量 OFFICIAL_POST_RUN_TOOLING_ITEM_IDS 原位不动。"
                                   "B4-07 收尾：candidate_post_run_recovery_action 在共享闭集判定之后，按清单客户端经适配器"
                                   " candidate_post_run_extra_action 额外放行（新增 _candidate_post_run_extra_items；Codex 认定恒为假、额外项为空，"
                                   "判定逐字不变，见 test_claude_upgrade_vc6_batch 的 Codex 对照）；_post_run_tooling_item_allowed 与五判据不变。"
                                   "10-08 m11 满载收尾误封修复：_wait_for_monitor_stop 在 _monitor_stop_progress 检出新落盘进展时"
                                   "续期 2 秒，_monitor_acknowledged_stop 确认本 owner 的 stop-requested 事件后允许单步慢写；"
                                   "两种续期都受 DEFAULT_MONITOR_STOP_FINALIZE_SECONDS（stop 起算 30 秒）的等待上限约束，"
                                   "无进展且未接手请求时沿用原等待窗口，超时封存与终止逻辑不变。"
                                   "B4-05b-1 候选采集续跑：续跑命令逐字携带的身份参数改经新函数 candidate_recovery_identity_flags 按清单"
                                   "归属客户端的适配器取（未给适配器即原常量 CANDIDATE_RECOVERY_IDENTITY_FLAGS，原位不动）；"
                                   "candidate_capture_run_identity、_candidate_identity_tokens、两个续跑命令构造函数与"
                                   "_candidate_recovery_command_identity 加缺省为 Codex 的适配器参数，各调用点传清单归属客户端的适配器"
                                   "（Codex 清单得到的就是原常量，判定、命令与报错逐字不变，见 test_driver_client_table_codex_parity）。"
                                   "改动与新增的符号都在 wire、evidence 与评估读侧闭包外；10-08 首批清单编译拆分：_normalize_campaign_run_manifest 与纯批次编译器共用唯一规范化核心，严格读取入口保留绑定文件、VC-1／VC-5 门禁，Codex 运行期与历史重放仍走严格入口；"
                                   "r2 按客户端只读元数据校验完整后继排列，Codex 返回原 _SUCCESSOR_PROTOCOLS tuple 对象，原校验器不改；"
                                   "B5b-03 原 revision 中断恢复仅新增读侧闭包外 finalize_orphaned_running_run：完整 COMMIT、唯一已启动动作"
                                   "且 owner/monitor 双死亡时，在派发锁内幂等补 watchdog 中止与分钟尾部；monitor、reader、schema、后继顺序不改；"
                                   "B5b-02 在既有 environment_redispatch 增加单动作首次候选采集的预约前 execution-failure 分支，"
                                   "复核零预约零请求、逐字动作身份、正式收据及项目总账绑定，保留原失败诊断与协议顺序",
    "codex_upgrade_reconciler.py": "B1-03 对账器判定流程经适配器；原读侧函数除单独批准的 D4 目标函数外保留；B2-08 读侧闭包外的 _environment_facts、"
                                   "_deployment_receipt、_identity_facts 三处改经适配器成员（Codex 取值逐字等价）；两类对账收据的易变字段加上 "
                                   "campaign_deadline_at_utc（批准延期后同一对象能再对账，原先在这里失败关闭）；B4-05b-2 的"
                                   " validate_segment_reuse_preview 曾改成适配调用；D4 单点修复恢复 Codex 对原 segment_reuse_proofs 的静态调用，"
                                   "Claude 仍沿自身适配器。原集合核对、拒绝及统计保留，23／35 项旧依赖恢复；目标函数和由它到达的"
                                   "适配服务面依赖按过渡原件精确登记，不修改其他旧证明符号、算法、策略屏障或忽略表；"
                                   "B5b-03 对账写入口在工具身份准入后调用孤儿 running 收口，新调用宿主不属于读侧闭包",
    "codex_upgrade_project_ledger.py": "B1-04 项目总账客户端维度：Claude v2 plan 带 client，码表、目录名与计量类别经适配器；Codex v1 逐字不变",
    "codex_upgrade_timing_ledger.py": (
        "B1-05 计时账本客户端维度：Claude v2 计划带 client，默认预算、承接表与总账目录名按客户端；Codex v1 逐字不变；"
        "B4-07 交付恢复段与阶段失效四个事件（单列 DELIVERY_EVENT_TYPES，只对 Claude 计划放行，三个交付字段只写进 Claude "
        "交付事件，摘要键集合不变），Codex v1 账本的事件字节、判定与报错不变"
    ),
    "codex_upgrade.py": "B1-09 迁出 108 个不在任何身份闭包内的共享流程函数，原位留同名薄壳；随迁常量留别名",
    "upgrade_control_plane.py": "B1-09 新增共享控制流程：迁入函数经 adapter.orchestrator 访问编排器；B3-06 adapter_scope 同时设定对账器的"
                                "客户端作用域（本模块直接调用的对账器内部函数随同一客户端，跨客户端即拒绝；Codex 设定的就是对账器缺省值，"
                                "行为不变）；10-07 staging 派发门禁偶发的产品侧修复：no-op 引导首批派发后核对首批父 run 的落盘终态必须是"
                                " stopped，不是就当场失败并点名落盘终态（_bootstrap_noop_first_batch，读侧闭包外）；B5b-03 补账预览"
                                "（_accounting_resolve_preview，读侧闭包外）的来源核算改经 adapter.provenance 取模块：Claude 作用域下"
                                "原先误用 Codex 计量规则、与对账结论相反；Codex 下 adapter.provenance 就是 reconciler.provenance 同一个"
                                "模块对象，行为不变；B4-05 新基线 compare 的决定经 comparison_is_baseline_bound 值成员取，"
                                "Claude 必须当前基线重签，Codex 原 compare 复用条件不变",
    "codex_upgrade_wire_transition.py": "B5a-01 只改 main：当前工具身份经适配器取 _tool_identity（Codex 取值不变）；裁定函数与 DEPLOY_RECEIPT_GLOB 原位不动",
    "codex_upgrade_pre_a3_certification.py": "B5a-04 pre-A3 按客户端分组：场景表与真实链按客户端、单元前缀 pre-a3:claude:、Claude 签 v3；编排器引用经适配器（Codex 单元、命令与收据逐字不变）；"
                                             "B5a-08 签发与核验新增逐单元承接（unit-inherit）清单的形成期证明分支（新增函数），逐场景核验多一个缺省为全集通过的"
                                             " inheriting_mode 参数（Codex 的 v1／v2 签发与核验逐字不变，见 test_unit_inherit_codex_parity）；"
                                             "B5a-05 只填 Claude 的两张字面量登记表 CLAUDE_REAL_CHAIN_IDS、CLAUDE_SCENARIOS（最小恢复演练四条链），"
                                             "两者不在 Codex 评估读侧闭包内，Codex 场景表、单元与收据不变；"
                                             "B5a-05 收尾给派发秒数表 SCENARIO_SECONDS 追加 Claude 四条真实链的 ARM64 实测秒数（取整到秒，只决定"
                                             "执行器派发顺序；Codex 七项不变，见 test_pre_a3_codex_parity），并改写 Claude 登记表上方已过时的「等 B2-08」注释；"
                                             "5a 收口：模块级不再导入测试目录，FIXTURE_ONLY_ENV 改写为同值字面量（测试锁定两边同值）；REHEARSAL_BINDERS "
                                             "注释与 Claude 分支的拒绝报错改为当前事实（campaign-run 分批演练绑定只属于 Codex，Claude 不接受），Codex 分支"
                                             "与输出不变；B5b-03 在 Claude 两张字面量登记表追加故障恢复链 vc-chain.claude-recovery-chain，"
                                             "SCENARIO_SECONDS 追加它的派发秒数（暂按本机实测，ARM64 预跑后替换；只决定派发顺序），"
                                             "Codex 场景表、单元、秒数与收据不变；B5b-03 时间合同 v2 新增 Claude 三条故障链的方法预算表与计算函数，"
                                             "plan_scenario_commands 仅对这三条 Claude 链覆盖外层时限；B5b-02 两条 Claude 录制链也采用独立一小时与收尾余量，"
                                             "其余单元与旧 Codex planner 逐字不变",
    "codex_upgrade_policy_certification.py": "B5a-04 策略认证按客户端：Claude 按 v3 引擎算身份、认 Claude 部署收据；编排器引用经适配器（Codex 合同与输出逐字不变）",
    "certify_release.py": "B5a-04 发布认证按客户端：Claude 签 tool-release-certification/v2（加 client）；编排器引用经适配器（Codex v1 逐字不变）；"
                          "B2-11 第二部分 Job 演练按认证的客户端重放（Codex 传 codex：合法收据的结果与报错不变，另一客户端的收据被拒），"
                          "atomic-double 门槛单列；B5a-09 续 atomic-double 按认证的客户端重放（本机与容器内；Codex 的命令行、合同名与"
                          "工具身份核对逐字不变，Claude 容器内重放追加 --client claude），去掉 Claude 的失败关闭门槛，Claude 不接受 Codex "
                          "专属的分批演练；B5b-04 为 Claude 签发与核验补八条发布必需链的完整覆盖门槛，"
                          "Codex 未登记额外门槛，v1 字段、输出及历史校验行为不变",
    "codex_upgrade_vc_receipt.py": "B5a-08 P0 v2 重验新增逐单元承接（unit-inherit）清单的形成期证明分支（新增函数，验证器只从仓库 tools/ci 或"
                                   "已安装驱动加载）；逐条核验与门禁重验多一个缺省为全集通过的 inheriting_mode 参数；读侧闭包内的符号原位不动"
                                   "（Codex 两种模式的结果与报错逐字不变，见 test_unit_inherit_codex_parity）",
    "codex_upgrade_gate_receipt.py": "B5a-08 只改钉住常量 TARGET_VERIFIER_SHA256S 里 unit_records.py 的摘要：常量在评估器读侧闭包内，按"
                                     " REGISTERED_EVALUATOR_READER_CHANGES 单独登记；B5a-09／10 测试单元读集接线同步 unit_records.py 与"
                                     " read_audit.py 两个钉住值，并登记上一已部署生成器只读重放摘要；两个常量的新后值由独立精确后继证明；"
                                     "声明级承接（步骤 8）同步 unit_records／read_audit／read_audit_runtime 三个钉住值并登记 77b57e7c 的只读重放摘要，"
                                     "两个常量的新后值由第三份独立精确后继证明；"
                                     "审计器加固（步骤 10）同步 read_audit／read_audit_runtime 两个钉住值并登记 fa764324 的只读重放摘要，"
                                     "两个常量的新后值由第四份独立精确后继证明",
    "codex_upgrade_vc_artifacts.py": "B3-04 只在文件末尾新增客户端中立候选构建收据合同 official-client-upgrade-candidate-build-receipt/v1"
                                     "（两个函数与合同常量，只由 Claude 编排器签发）；Codex 的 build_candidate_build_receipt 与读侧闭包内的"
                                     " validate_candidate_build_receipt 等既有符号原位不动，新增符号不被 Codex 任何身份闭包引用",
}

# 本项目登记的 Codex 评估器读侧变化（见模块说明）：键是工作项，值登记变化的符号（「模块:符号」→ 前后符号摘要）、两项读侧
# 摘要的前后值与理由。B5a-08 的形成期登记原样保留；后续批准范围在独立冻结原件中精确登记，不能覆盖这两个旧值。
REGISTERED_EVALUATOR_READER_CHANGES: dict[str, dict[str, Any]] = {
    "B5a-08": {
        "symbols": {
            # 钉住常量本身：unit_records.py 的摘要换成 B5a-01 与 B5a-08 改过之后的那份。
            "codex_upgrade_gate_receipt:TARGET_VERIFIER_SHA256S": {
                "before": "02825ab58147e9071ae19395590c2be90c58aefd6023a2013646a7605c7e37e8",
                "after": "b5abc7579c03d34b35801d13a3ff8f4cdc0714d15a174396788e46971cf6ae93"},
            # 改钉住值就是改门禁收据生成器：生成器只读重放登记门禁（test_producer_replay_registration_gate）要求同时登记修改前
            # 版本 ce9e3b79 的摘要，否则该版本已生成的 v4 门禁收据部署后无法重放。只新增这一项，已有登记一项不动。
            "codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES": {
                "before": "49e52aebf07af8149431fe658080b241cc0b8cb43d52a04f58a15c3ded6e774a",
                "after": "47a709cdee5443dd31397f06c5a3f23077699a3175039a4506690c5843758860"},
        },
        "compare_reader_sha256": {"before": "2a4ed24c04a1fee36be282fe2b57b4884f760b165e462c2f32cecaa806ebf62b",
                                  "after": "f49bc6a6ff1205b30cef7edf42466b7b68bdf74cc240eb0e48cf83918e86b992"},
        "accept_reader_sha256": {"before": "20773be4fb1fe1a103f874d9b4cbc6a1c78d0ca2830b0693aef8a882f074aff9",
                                 "after": "135ded5e4f6b46a96bfee2de0941c3c480d5ddaf323c00ba27305e433a85986c"},
        "reasons": (
            "B-10 目标平台单元承接验证器的依赖文件 tools/ci/unit_records.py 被 B5a-01 与 B5a-08 合法修改，钉住摘要随之更新"
            "（同组另外四个依赖 target_platform_gate.py、full_set_receipt.py、read_audit.py、read_audit_runtime.py 未改，钉住值不变）",
            "B-10 的 unit_execution 开关是关闭的：门禁收据项不带 unit_execution 时不加载这个验证器，Codex 现行门禁收据不受影响",
            "没有进行中的 Codex Campaign：冻结了评估器摘要的 Campaign 不会遇到新值；Codex 发布重认证随 5a 收口统一做",
            "只读重放登记那一项是改钉住值的连带：按既有门禁登记修改前生成器的摘要，只服务旧收据重放，不改变新收据的判定",
        ),
    },
}
READER_FIELDS = (("compare_reader_sha256", tip.EVALUATOR_COMPARE_READER_ROOTS), ("accept_reader_sha256", tip.EVALUATOR_ACCEPT_READER_ROOTS))
READER_TRANSITION_PATH = BASELINE_PATH.with_name("codex_identity_conservation_reader_transition_20261008.json")
READER_TRANSITION_SHA256 = "6d62b8d3837302ab664732d8a329442221d7b02f9b4c59483cd69c5aac125301"
READER_TRANSITION_PHASES = ("B4-05-D4", "r2-protocol-order", "B4-05-recovery")
READER_SUCCESSOR_PATH = BASELINE_PATH.with_name("codex_identity_conservation_reader_transition_20261011.json")
READER_SUCCESSOR_SHA256 = "2b073cbe3c5efccba31336228b099f2a599c646e21a9902780ca314cd81c9f1d"
READER_SUCCESSOR_PHASE = "B5a09-test-read-pins"
# 收尾：门禁收据生成器只读重放登记补登两个已部署版本摘要，第二份后继单独钉住，前两份原件不改写。
READER_SUCCESSOR2_PATH = BASELINE_PATH.with_name("codex_identity_conservation_reader_transition_20261013.json")
READER_SUCCESSOR2_SHA256 = "e1aea18e2a88074c380cee5850f4b3d50ac3c10718c472a44f7f23093eaaaa65"
READER_SUCCESSOR2_PHASE = "H3-closeout-replay-registry"
# 步骤 8：声明级承接（步骤 1～7）改了 unit_records／read_audit／read_audit_runtime，门禁收据生成器的三个验证器钉住值同步，
# 修改前已部署的生成器 77b57e7c 登记进只读重放登记——两个既有常量同时变化；第三份后继单独钉住，前三份原件不改写。
READER_SUCCESSOR3_PATH = BASELINE_PATH.with_name("codex_identity_conservation_reader_transition_20261015.json")
READER_SUCCESSOR3_SHA256 = "06f36b43ba6dd9a7cb382030b6c26f40c37ae8c360af0f81deeab0b219fd7cbb"
READER_SUCCESSOR3_PHASE = "step8-declared-inputs-pins"
# 步骤 10：审计器加固（开发循环 R2～R16）改了 read_audit／read_audit_runtime，门禁收据生成器的这两个验证器钉住值同步，
# 修改前已部署的生成器 fa764324 登记进只读重放登记——两个既有常量同时变化；第四份后继单独钉住，前四份原件不改写。
READER_SUCCESSOR4_PATH = BASELINE_PATH.with_name("codex_identity_conservation_reader_transition_20261016.json")
READER_SUCCESSOR4_SHA256 = "ceb5e13e1a0f5723c3fb36a094edbd07b7b81109c518b15162b6e845d36239ab"
READER_SUCCESSOR4_PHASE = "step10-audit-hardening-pins"
REGISTERED_REPLAY_SUCCESSOR_SYMBOLS = frozenset({"codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES"})
REGISTERED_PIN_SUCCESSOR_SYMBOLS = frozenset({
    "codex_upgrade_gate_receipt:TARGET_VERIFIER_SHA256S",
    "codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES",
})
# 相对 B5a-08 原图，D4 静态分支只新增这 16 个既有受管适配依赖。显式清单防止把整模块名字当作任意符号豁免。
REGISTERED_ADAPTER_DEPENDENCIES = frozenset({
    "codex_upgrade_reconciler:_ADAPTER", "codex_upgrade_reconciler:_adapter",
    *{f"upgrade_client_adapter:{name}" for name in (
        "ADAPTER_BINDINGS", "ADAPTER_MEMBERS", "CandidateCoordinates", "ClientAdapter", "ClientAdapterError",
        "ORCHESTRATOR_SURFACE", "PACKAGE", "RecoveryContext", "_ADAPTERS", "_ADAPTERS_LOCK",
        "_import_managed", "_resolve", "_surface_bindings", "adapter_for",
    )},
})
REGISTERED_METADATA_SYMBOLS = frozenset({"upgrade_client_adapter:ADAPTER_BINDINGS", "upgrade_client_adapter:ADAPTER_MEMBERS"})
REGISTERED_D4_SYMBOL = "codex_upgrade_reconciler:validate_segment_reuse_preview"

# evidence 层过渡登记（见模块说明）：事实映射的源码快照重绑，以及映射摘要常量文件的同步变化。登记文件摘要钉死，改它必须另行审核
# 并更新钉住值；再次重绑时重新生成“基线→最新”的累计登记，不能原地改文件后沿用旧钉住值。登记只放行下面两个 evidence 层文件，
# 放行路径只有这份过渡登记，绝不进入 ``REGISTERED_CONTROL_CHANGES``。
EVIDENCE_TRANSITION_PATH = BASELINE_PATH.with_name("codex_identity_conservation_evidence_transition_20261019.json")
EVIDENCE_TRANSITION_SHA256 = "56708829d59a293e0ddd20202d62391865b6476725da27e5407bd11e5fd74d74"
EVIDENCE_TRANSITION_ID = "evidence-source-snapshot-rebind-20261019"
EVIDENCE_FACT_MAP = "candidate_test_fact_map_0_160_0.json"
EVIDENCE_TRACE = "candidate_test_trace.py"
EVIDENCE_TRANSITION_PATHS = frozenset({EVIDENCE_FACT_MAP, EVIDENCE_TRACE})
HEX64 = re.compile(r"[0-9a-f]{64}")


def _reader_payload(closure: dict[str, Any]) -> dict[str, Any]:
    """保留正式图的所有元数据；只摘除派生摘要，不删除根、屏障或整文件记录。"""

    return copy.deepcopy({key: value for key, value in closure.items() if key != "closure_sha256"})


def _load_reader_transition(path: Path = READER_TRANSITION_PATH) -> dict[str, Any]:
    """加载独立冻结过渡原件。新登记必须另行审核，不能原地改图后继续使用旧钉住值。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("读侧过渡原件必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != READER_TRANSITION_SHA256:
        raise ValueError("读侧过渡原件摘要与钉住值不符")
    transition = json.loads(raw)
    if (transition.get("schema_version") != "codex-identity-conservation-reader-transition/v1"
            or transition.get("transition_id") != "D4-r2-recovery-20261008"
            or transition.get("baseline_sha256") != BASELINE_SHA256):
        raise ValueError("读侧过渡原件合同或原始基线绑定不符")
    return transition


def _inverse_reader_records(payload: dict[str, Any], collection: str, changes: list[dict[str, Any]]) -> None:
    """按完整前后记录精确逆向；未登记项原样保留，最终摘要会拒绝任何残留变化。"""

    def key(record: dict[str, Any]) -> str:
        return f"{record['module']}:{record['name']}" if collection == "symbols" else record["module"]

    records = {key(record): record for record in payload[collection]}
    if len(records) != len(payload[collection]) or len({change["key"] for change in changes}) != len(changes):
        raise ValueError("读侧记录或过渡键重复")
    for change in changes:
        name = change["key"]
        after, before = change["after"], change["before"]
        if after is None or key(after) != name or records.get(name) != after:
            raise ValueError(f"当前读侧记录不是已登记的完整后值：{name}")
        if before is None:
            del records[name]
        else:
            if key(before) != name:
                raise ValueError(f"读侧过渡前值的键不符：{name}")
            records[name] = copy.deepcopy(before)
    payload[collection] = [records[name] for name in sorted(records)]


def _load_reader_successor(path: Path = READER_SUCCESSOR_PATH) -> dict[str, Any]:
    """新两常量后继单独钉住；不改写已封存的 10-08 过渡原件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("读侧后继原件必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != READER_SUCCESSOR_SHA256:
        raise ValueError("读侧后继原件摘要与钉住值不符")
    successor = json.loads(raw)
    if (successor.get("schema_version") != "codex-identity-conservation-reader-transition/v1"
            or successor.get("transition_id") != "B5a09-test-read-pins-20261011"
            or successor.get("baseline_sha256") != BASELINE_SHA256
            or successor.get("predecessor_transition") != {
                "path": READER_TRANSITION_PATH.name, "sha256": READER_TRANSITION_SHA256}):
        raise ValueError("读侧后继合同或前序原件绑定不符")
    return successor


def _restore_pinned_reader_successor(closure: dict[str, Any], field: str, successor: dict[str, Any],
                                    transition: dict[str, Any]) -> dict[str, Any]:
    """只逆向本轮两个常量；完整图必须逐字段回到原过渡的后图。"""

    if tuple(step["phase"] for step in successor["steps"]) != (READER_SUCCESSOR_PHASE,):
        raise ValueError("读侧后继阶段次序或集合不符")
    if successor["before_readers"][field] != transition["after_readers"][field]:
        raise ValueError("读侧后继前图不是原过渡的精确后图")
    change = successor["steps"][0]["readers"][field]
    if (change["modules"] != []
            or {row["key"] for row in change["symbols"]} != REGISTERED_PIN_SUCCESSOR_SYMBOLS
            or any(row["before"] is None or row["after"] is None or not row["reason"] for row in change["symbols"])
            or change["before_sha256"] != successor["before_readers"][field]["closure_sha256"]
            or change["after_sha256"] != successor["after_readers"][field]["closure_sha256"]):
        raise ValueError("读侧后继必须精确登记两个既有常量且不含整文件变化")
    if closure != successor["after_readers"][field]:
        raise ValueError("当前读侧图不是后继登记的完整后图")
    payload = _reader_payload(closure)
    if tip._fingerprint(payload) != change["after_sha256"]:
        raise ValueError("当前读侧图与后继摘要不符")
    _inverse_reader_records(payload, "symbols", change["symbols"])
    if (tip._fingerprint(payload) != change["before_sha256"]
            or payload != _reader_payload(transition["after_readers"][field])):
        raise ValueError("两个常量逆向没有回到原过渡的完整后图")
    return {**payload, "closure_sha256": change["before_sha256"]}


def _load_reader_successor2(path: Path = READER_SUCCESSOR2_PATH) -> dict[str, Any]:
    """生成器只读重放登记的第二后继单独钉住；不改写前两份已封存的过渡原件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("读侧第二后继原件必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != READER_SUCCESSOR2_SHA256:
        raise ValueError("读侧第二后继原件摘要与钉住值不符")
    successor = json.loads(raw)
    if (successor.get("schema_version") != "codex-identity-conservation-reader-transition/v1"
            or successor.get("transition_id") != "H3-closeout-replay-registry-20261013"
            or successor.get("baseline_sha256") != BASELINE_SHA256
            or successor.get("predecessor_transition") != {
                "path": READER_SUCCESSOR_PATH.name, "sha256": READER_SUCCESSOR_SHA256}):
        raise ValueError("读侧第二后继合同或前序原件绑定不符")
    return successor


def _restore_pinned_reader_successor2(closure: dict[str, Any], field: str, successor2: dict[str, Any],
                                      successor: dict[str, Any]) -> dict[str, Any]:
    """只逆向本轮一个常量；完整图必须逐字段回到第一后继的后图。"""

    if tuple(step["phase"] for step in successor2["steps"]) != (READER_SUCCESSOR2_PHASE,):
        raise ValueError("读侧第二后继阶段次序或集合不符")
    if successor2["before_readers"][field] != successor["after_readers"][field]:
        raise ValueError("读侧第二后继前图不是第一后继的精确后图")
    change = successor2["steps"][0]["readers"][field]
    if (change["modules"] != []
            or {row["key"] for row in change["symbols"]} != REGISTERED_REPLAY_SUCCESSOR_SYMBOLS
            or any(row["before"] is None or row["after"] is None or not row["reason"] for row in change["symbols"])
            or change["before_sha256"] != successor2["before_readers"][field]["closure_sha256"]
            or change["after_sha256"] != successor2["after_readers"][field]["closure_sha256"]):
        raise ValueError("读侧第二后继必须精确登记一个既有常量且不含整文件变化")
    if closure != successor2["after_readers"][field]:
        raise ValueError("当前读侧图不是第二后继登记的完整后图")
    payload = _reader_payload(closure)
    if tip._fingerprint(payload) != change["after_sha256"]:
        raise ValueError("当前读侧图与第二后继摘要不符")
    _inverse_reader_records(payload, "symbols", change["symbols"])
    if (tip._fingerprint(payload) != change["before_sha256"]
            or payload != _reader_payload(successor["after_readers"][field])):
        raise ValueError("一个常量逆向没有回到第一后继的完整后图")
    return {**payload, "closure_sha256": change["before_sha256"]}


def _load_reader_successor3(path: Path = READER_SUCCESSOR3_PATH) -> dict[str, Any]:
    """步骤 8 的第三后继（三个钉住值＋只读重放登记）单独钉住；不改写前三份已封存的过渡原件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("读侧第三后继原件必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != READER_SUCCESSOR3_SHA256:
        raise ValueError("读侧第三后继原件摘要与钉住值不符")
    successor = json.loads(raw)
    if (successor.get("schema_version") != "codex-identity-conservation-reader-transition/v1"
            or successor.get("transition_id") != "step8-declared-inputs-pins-20261015"
            or successor.get("baseline_sha256") != BASELINE_SHA256
            or successor.get("predecessor_transition") != {
                "path": READER_SUCCESSOR2_PATH.name, "sha256": READER_SUCCESSOR2_SHA256}):
        raise ValueError("读侧第三后继合同或前序原件绑定不符")
    return successor


def _restore_pinned_reader_successor3(closure: dict[str, Any], field: str, successor3: dict[str, Any],
                                      successor2: dict[str, Any]) -> dict[str, Any]:
    """只逆向本轮两个常量；完整图必须逐字段回到第二后继的后图。"""

    if tuple(step["phase"] for step in successor3["steps"]) != (READER_SUCCESSOR3_PHASE,):
        raise ValueError("读侧第三后继阶段次序或集合不符")
    if successor3["before_readers"][field] != successor2["after_readers"][field]:
        raise ValueError("读侧第三后继前图不是第二后继的精确后图")
    change = successor3["steps"][0]["readers"][field]
    if (change["modules"] != []
            or {row["key"] for row in change["symbols"]} != REGISTERED_PIN_SUCCESSOR_SYMBOLS
            or any(row["before"] is None or row["after"] is None or not row["reason"] for row in change["symbols"])
            or change["before_sha256"] != successor3["before_readers"][field]["closure_sha256"]
            or change["after_sha256"] != successor3["after_readers"][field]["closure_sha256"]):
        raise ValueError("读侧第三后继必须精确登记两个既有常量且不含整文件变化")
    if closure != successor3["after_readers"][field]:
        raise ValueError("当前读侧图不是第三后继登记的完整后图")
    payload = _reader_payload(closure)
    if tip._fingerprint(payload) != change["after_sha256"]:
        raise ValueError("当前读侧图与第三后继摘要不符")
    _inverse_reader_records(payload, "symbols", change["symbols"])
    if (tip._fingerprint(payload) != change["before_sha256"]
            or payload != _reader_payload(successor2["after_readers"][field])):
        raise ValueError("两个常量逆向没有回到第二后继的完整后图")
    return {**payload, "closure_sha256": change["before_sha256"]}


def _load_reader_successor4(path: Path = READER_SUCCESSOR4_PATH) -> dict[str, Any]:
    """步骤 10 的第四后继（两个验证器钉住值＋只读重放登记）单独钉住；不改写前四份已封存的过渡原件。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("读侧第四后继原件必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != READER_SUCCESSOR4_SHA256:
        raise ValueError("读侧第四后继原件摘要与钉住值不符")
    successor = json.loads(raw)
    if (successor.get("schema_version") != "codex-identity-conservation-reader-transition/v1"
            or successor.get("transition_id") != "step10-audit-hardening-pins-20261016"
            or successor.get("baseline_sha256") != BASELINE_SHA256
            or successor.get("predecessor_transition") != {
                "path": READER_SUCCESSOR3_PATH.name, "sha256": READER_SUCCESSOR3_SHA256}):
        raise ValueError("读侧第四后继合同或前序原件绑定不符")
    return successor


def _restore_pinned_reader_successor4(closure: dict[str, Any], field: str, successor4: dict[str, Any],
                                      successor3: dict[str, Any]) -> dict[str, Any]:
    """只逆向本轮两个常量；完整图必须逐字段回到第三后继的后图。"""

    if tuple(step["phase"] for step in successor4["steps"]) != (READER_SUCCESSOR4_PHASE,):
        raise ValueError("读侧第四后继阶段次序或集合不符")
    if successor4["before_readers"][field] != successor3["after_readers"][field]:
        raise ValueError("读侧第四后继前图不是第三后继的精确后图")
    change = successor4["steps"][0]["readers"][field]
    if (change["modules"] != []
            or {row["key"] for row in change["symbols"]} != REGISTERED_PIN_SUCCESSOR_SYMBOLS
            or any(row["before"] is None or row["after"] is None or not row["reason"] for row in change["symbols"])
            or change["before_sha256"] != successor4["before_readers"][field]["closure_sha256"]
            or change["after_sha256"] != successor4["after_readers"][field]["closure_sha256"]):
        raise ValueError("读侧第四后继必须精确登记两个既有常量且不含整文件变化")
    if closure != successor4["after_readers"][field]:
        raise ValueError("当前读侧图不是第四后继登记的完整后图")
    payload = _reader_payload(closure)
    if tip._fingerprint(payload) != change["after_sha256"]:
        raise ValueError("当前读侧图与第四后继摘要不符")
    _inverse_reader_records(payload, "symbols", change["symbols"])
    if (tip._fingerprint(payload) != change["before_sha256"]
            or payload != _reader_payload(successor3["after_readers"][field])):
        raise ValueError("两个常量逆向没有回到第三后继的完整后图")
    return {**payload, "closure_sha256": change["before_sha256"]}


def _restore_registered_reader(closure: dict[str, Any], field: str, transition: dict[str, Any],
                               successor: dict[str, Any] | None = None,
                               successor2: dict[str, Any] | None = None,
                               successor3: dict[str, Any] | None = None,
                               successor4: dict[str, Any] | None = None) -> dict[str, Any]:
    """当前真实图 → 第四后继 → 第三后继 → 第二后继 → 第一后继 → 恢复后继 → r2 → D4 → B5a-08 → 原始基线；逐步核实际摘要。"""

    if field not in {item for item, _roots in READER_FIELDS}:
        raise ValueError("未知读侧摘要字段")
    successor = successor or _load_reader_successor()
    successor2 = successor2 or _load_reader_successor2()
    successor3 = successor3 or _load_reader_successor3()
    closure = _restore_pinned_reader_successor4(closure, field, successor4 or _load_reader_successor4(), successor3)
    closure = _restore_pinned_reader_successor3(closure, field, successor3, successor2)
    closure = _restore_pinned_reader_successor2(closure, field, successor2, successor)
    closure = _restore_pinned_reader_successor(closure, field, successor, transition)
    payload = _reader_payload(closure)
    if tip._fingerprint(payload) != closure["closure_sha256"]:
        raise ValueError("当前读侧图与其摘要不符")
    if tuple(step["phase"] for step in transition["steps"]) != READER_TRANSITION_PHASES:
        raise ValueError("读侧过渡阶段次序或集合不符")
    for step in reversed(transition["steps"]):
        change = step["readers"][field]
        if tip._fingerprint(payload) != change["after_sha256"]:
            raise ValueError(f"读侧出现未登记的变化：{step['phase']}／{field}")
        for collection in ("symbols", "modules"):
            _inverse_reader_records(payload, collection, change[collection])
        if tip._fingerprint(payload) != change["before_sha256"]:
            raise ValueError(f"读侧精确逆向没有回到前图：{step['phase']}／{field}")
    legacy = REGISTERED_EVALUATOR_READER_CHANGES["B5a-08"]
    if tip._fingerprint(payload) != legacy[field]["after"]:
        raise ValueError("后继逆向未回到 B5a-08 的钉住图")
    for name, digests in legacy["symbols"].items():
        module, symbol = name.split(":", 1)
        matches = [record for record in payload["symbols"] if (record["module"], record["name"]) == (module, symbol)]
        if len(matches) != 1 or matches[0]["sha256"] != digests["after"]:
            raise ValueError(f"B5a-08 常量后值不符：{name}")
        matches[0]["sha256"] = digests["before"]
    if tip._fingerprint(payload) != legacy[field]["before"]:
        raise ValueError("常量逆向未回到原始基线摘要")
    if payload != _reader_payload(transition["before_readers"][field]):
        raise ValueError("逆向图与冻结的原始基线图逐字段不符")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON 对象出现重复键就拒绝，避免后一个值悄悄盖掉前一个。"""

    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON 对象键重复")
    return result


def _load_evidence_transition(path: Path = EVIDENCE_TRANSITION_PATH) -> dict[str, Any]:
    """加载 evidence 层过渡登记：必须是普通文件、摘要等于钉住值，并绑定原始基线。

    改写登记不能获得新的期望：摘要对不上钉住值立即失败，新登记必须另行审核并更新钉住值。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence 层过渡登记必须是普通文件")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EVIDENCE_TRANSITION_SHA256:
        raise ValueError("evidence 层过渡登记摘要与钉住值不符")
    transition = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    if (transition.get("schema_version") != "codex-identity-conservation-evidence-transition/v1"
            or transition.get("transition_id") != EVIDENCE_TRANSITION_ID
            or transition.get("baseline_sha256") != BASELINE_SHA256):
        raise ValueError("evidence 层过渡登记合同或原始基线绑定不符")
    return transition


def _evidence_registrations(transition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按路径索引逐文件登记；文件集合必须正好是事实映射与映射摘要常量两个 evidence 层文件，不重复、不缺少、不多出。"""

    files = transition["files"]
    registrations = {item["path"]: item for item in files}
    if len(registrations) != len(files) or set(registrations) != EVIDENCE_TRANSITION_PATHS:
        raise ValueError("evidence 层过渡登记的文件集合必须正好是事实映射与映射摘要常量两个文件")
    return registrations


def _read_evidence_file(path: str) -> bytes:
    """读取受管树里的 evidence 层文件；符号链接或非普通文件不接受。"""

    target = TOOL_ROOT / path
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"evidence 层文件必须是受管树里的普通文件：{path}")
    return target.read_bytes()


def _occurrence_starts(raw: bytes, needle: bytes) -> list[int]:
    """needle 在 raw 里的全部起点；允许重叠计数，防止自重叠文本被低估成一次。"""

    starts: list[int] = []
    position = raw.find(needle)
    while position != -1:
        starts.append(position)
        position = raw.find(needle, position + 1)
    return starts


def _restore_registered_evidence_file(raw: bytes, registration: dict[str, Any]) -> bytes:
    """逆向证明（字节级）：把当前文件里每处登记的后值文本换回前值，重算的摘要必须等于基线里该文件的摘要。

    先核当前字节的摘要就是登记的后值；每处后值文本必须在当前字节里恰好出现一次、各处位置互不重叠；换回前值后的字节摘要
    必须等于登记的前值（即基线条目）。少登记、多登记、改前后值或文件另有改动，都到不了基线摘要。"""

    path = registration["path"]
    if hashlib.sha256(raw).hexdigest() != registration["after_sha256"]:
        raise ValueError(f"evidence 层文件当前摘要不是登记的后值：{path}")
    spans: list[tuple[int, int, bytes]] = []
    for replacement in registration["replacements"]:
        after, before = replacement["after"].encode("utf-8"), replacement["before"].encode("utf-8")
        if not after or after == before:
            raise ValueError(f"登记的替换必须有非空后值且前后不同：{path}")
        starts = _occurrence_starts(raw, after)
        if len(starts) != 1:
            raise ValueError(f"登记的后值文本必须在当前文件里恰好出现一次（实际 {len(starts)} 次）：{path}")
        spans.append((starts[0], starts[0] + len(after), before))
    spans.sort(key=lambda item: item[0])
    if any(first[1] > second[0] for first, second in zip(spans, spans[1:])):
        raise ValueError(f"登记的替换位置不得重叠：{path}")
    pieces: list[bytes] = []
    cursor = 0
    for start, end, before in spans:
        pieces.extend((raw[cursor:start], before))
        cursor = end
    pieces.append(raw[cursor:])
    restored = b"".join(pieces)
    if hashlib.sha256(restored).hexdigest() != registration["before_sha256"]:
        raise ValueError(f"逆向替换没有回到基线里的文件摘要：{path}")
    return restored


def _json_differences(current: Any, original: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """逐叶比较两份 JSON 值，返回内容不同的叶子路径；类型、键集合或数组长度不同时整个节点记一处。"""

    if isinstance(current, dict) and isinstance(original, dict):
        if current.keys() != original.keys():
            return [path]
        return [difference for key in current for difference in _json_differences(current[key], original[key], (*path, key))]
    if isinstance(current, list) and isinstance(original, list):
        if len(current) != len(original):
            return [path]
        return [difference for index, (left, right) in enumerate(zip(current, original))
                for difference in _json_differences(left, right, (*path, index))]
    return [] if type(current) is type(original) and current == original else [path]


def _prove_fact_map_structure(raw: bytes, restored: bytes, registration: dict[str, Any]) -> None:
    """结构证明：事实映射与逆向还原后的文件相比，只有登记的源码快照 sha256 字段不同。

    测试清单、事实、规则、``test_file_sha256`` 与顶层字段逐叶相同；登记的定位必须唯一命中一个 ``source_files`` 条目，该条目
    当前值等于登记后值、还原值等于登记前值。"""

    current = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    original = json.loads(restored, object_pairs_hook=_reject_duplicate_keys)
    expected: list[tuple[Any, ...]] = []
    for replacement in registration["replacements"]:
        locator = replacement["locator"]
        hits = [(index, position) for index, test in enumerate(current["tests"]) if test["name"] == locator["test"]
                for position, source in enumerate(test["source_files"]) if source["path"] == locator["source_file"]]
        if len(hits) != 1:
            raise ValueError("登记的定位必须唯一命中一个源码快照条目")
        index, position = hits[0]
        if (current["tests"][index]["source_files"][position]["sha256"] != replacement["after"]
                or original["tests"][index]["source_files"][position]["sha256"] != replacement["before"]):
            raise ValueError("登记的源码快照前后值与文件内容不符")
        expected.append(("tests", index, "source_files", position, "sha256"))
    if sorted(map(repr, _json_differences(current, original))) != sorted(map(repr, expected)):
        raise ValueError("事实映射在登记的源码快照 sha256 字段之外还有变化")


def _prove_frozen_mapping_constant(registration: dict[str, Any], fact_map: dict[str, Any]) -> None:
    """结构证明：映射摘要常量文件的登记只有一处连续文本，文本里除注释行外只有一条 ``FROZEN_MAPPING_SHA256`` 字符串赋值，
    赋值的前后值正好是事实映射文件摘要的前后值——常量随映射文件摘要同步，别的代码一概不动。"""

    replacements = registration["replacements"]
    if len(replacements) != 1 or replacements[0]["locator"] != {"constant": "FROZEN_MAPPING_SHA256"}:
        raise ValueError("映射摘要常量文件必须恰好登记一处 FROZEN_MAPPING_SHA256 替换")
    values: dict[str, str] = {}
    for side in ("before", "after"):
        code = "\n".join(line for line in replacements[0][side].splitlines() if not line.lstrip().startswith("#"))
        try:
            body = ast.parse(code).body
        except SyntaxError as error:
            raise ValueError("映射摘要常量登记文本除注释外必须是合法的 Python 赋值语句") from error
        if (len(body) != 1 or not isinstance(body[0], ast.Assign) or len(body[0].targets) != 1
                or not isinstance(body[0].targets[0], ast.Name) or body[0].targets[0].id != "FROZEN_MAPPING_SHA256"
                or not isinstance(body[0].value, ast.Constant) or not isinstance(body[0].value.value, str)):
            raise ValueError("映射摘要常量登记文本除注释外必须只有一条 FROZEN_MAPPING_SHA256 字符串赋值")
        values[side] = body[0].value.value
    if (values["before"], values["after"]) != (fact_map["before_sha256"], fact_map["after_sha256"]):
        raise ValueError("映射摘要常量没有随事实映射文件摘要同步")


def _restore_registered_evidence(transition: dict[str, Any], *, baseline: dict[str, Any], policy: dict[str, Any],
                                 entries: list[dict[str, str]], evidence_closure_sha256: str,
                                 read_file: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """当前 evidence 层 → 逐文件字节逆向（含结构证明）→ 重算 evidence 层摘要，必须回到基线；任一环节对不上就抛 ValueError。

    ``evidence_semantics_sha256`` 是 ``{"entries": evidence 层每个文件的（路径，文件字节摘要）, "orchestrator_closure_sha256":
    evidence 编排器闭包摘要}`` 的指纹，所以文件字节的任何变化都会改它。这里把登记的两个文件换回基线摘要（摘要来自字节逆向，不是
    照抄登记），其余 evidence 层文件必须本来就等于基线，再用同一个闭包摘要重算。"""

    read = read_file or _read_evidence_file
    registrations = _evidence_registrations(transition)
    before = {entry["path"]: entry["sha256"] for entry in baseline["entries"]}
    after = {entry["path"]: entry["sha256"] for entry in entries}
    if evidence_closure_sha256 != baseline["orchestrator_closure_sha256"]["evidence_semantics"]:
        raise ValueError("evidence 层编排器闭包摘要与基线不符：过渡登记只覆盖文件内容的变化")
    raws: dict[str, bytes] = {}
    restored: dict[str, bytes] = {}
    for path, registration in sorted(registrations.items()):
        if tip.classify_path(policy, path) != "evidence_semantics":
            raise ValueError(f"登记的文件不在 evidence 层：{path}")
        if before.get(path) != registration["before_sha256"]:
            raise ValueError(f"登记的前值不是基线里的文件摘要：{path}")
        if after.get(path) != registration["after_sha256"]:
            raise ValueError(f"登记的后值不是当前受管树里的文件摘要：{path}")
        raws[path] = read(path)
        restored[path] = _restore_registered_evidence_file(raws[path], registration)
    _prove_fact_map_structure(raws[EVIDENCE_FACT_MAP], restored[EVIDENCE_FACT_MAP], registrations[EVIDENCE_FACT_MAP])
    _prove_frozen_mapping_constant(registrations[EVIDENCE_TRACE], registrations[EVIDENCE_FACT_MAP])
    restored_entries: list[dict[str, str]] = []
    for entry in tip.layer_entries(policy, entries)["evidence_semantics"]:
        path = entry["path"]
        digest = hashlib.sha256(restored[path]).hexdigest() if path in restored else entry["sha256"]
        if digest != before.get(path):
            raise ValueError(f"evidence 层出现未登记的文件变化：{path}")
        restored_entries.append({"path": path, "sha256": digest})
    digest = tip._fingerprint({"entries": restored_entries, "orchestrator_closure_sha256": evidence_closure_sha256})
    if digest != baseline["evidence_semantics_sha256"]:
        raise ValueError("逆向后重算的 evidence 层摘要不等于基线")
    return {"evidence_semantics_sha256": digest, "files": {path: hashlib.sha256(data).hexdigest() for path, data in restored.items()}}


def _evidence_change_problems(policy: dict[str, Any], before: dict[str, str], after: dict[str, str],
                              registrations: dict[str, dict[str, Any]]) -> list[str]:
    """test ② 的 evidence 层分支：只有过渡登记里前后摘要都精确匹配的 evidence 层文件才允许变化，登记的文件必须真的变了。

    放行只经由 evidence 层过渡登记，不能写进 ``REGISTERED_CONTROL_CHANGES`` 冒充 control 层；其余层（wire、control）的
    变化仍由 test ② 原有的逐文件 control 登记检查。返回问题清单，空表示通过。"""

    problems: list[str] = []
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    changed_evidence = [path for path in changed if tip.classify_path(policy, path) == "evidence_semantics"]
    for path in changed_evidence:
        registration = registrations.get(path)
        if registration is None:
            problems.append(f"Codex evidence 层文件变化未登记：{path}")
        elif (before.get(path), after.get(path)) != (registration["before_sha256"], registration["after_sha256"]):
            problems.append(f"Codex evidence 层文件前后摘要与过渡登记不符：{path}")
    for path in sorted(registrations):
        if path not in changed_evidence:
            problems.append(f"过渡登记的文件没有变化或不在 evidence 层：{path}")
    for path in sorted(set(registrations) | set(changed_evidence)):
        if path in REGISTERED_CONTROL_CHANGES:
            problems.append(f"evidence 层文件不得登记进 REGISTERED_CONTROL_CHANGES 冒充 control 层：{path}")
    return problems


class CodexIdentityConservationTest(unittest.TestCase):
    """基线固定；身份敏感摘要逐字相等；control 层变化逐文件登记；迁入符号不在闭包内。"""

    @classmethod
    def setUpClass(cls) -> None:
        raw = BASELINE_PATH.read_bytes()
        cls.baseline_sha256 = hashlib.sha256(raw).hexdigest()
        cls.baseline = json.loads(raw)
        cls.policy = tip.load_policy()
        digests = tip._managed_tree_digests(TOOL_ROOT)
        cls.entries = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
        cls.identity = tip.compute_identity_v2(cls.policy, TOOL_ROOT, cls.entries)
        cls.evaluator = tip.evaluator_dependency_digests(TOOL_ROOT, cls.policy)
        cls.transition = _load_reader_transition()
        cls.successor = _load_reader_successor()
        cls.successor2 = _load_reader_successor2()
        cls.successor3 = _load_reader_successor3()
        cls.successor4 = _load_reader_successor4()
        cls.evidence_transition = _load_evidence_transition()
        cls.evidence_registrations = _evidence_registrations(cls.evidence_transition)
        cls.readers = {
            field: tip.evaluator_reader_closure(cls.policy, TOOL_ROOT, list(roots), digests)
            for field, roots in READER_FIELDS
        }

    def test_baseline_is_frozen(self) -> None:
        self.assertEqual(self.baseline_sha256, BASELINE_SHA256, "Codex 身份守恒基线被改动：本项目各批不得改基线")
        self.assertEqual(self.baseline["schema_version"], "codex-identity-conservation-baseline/v1")

    def test_identity_sensitive_digests_match_baseline(self) -> None:
        """① 产出身份守恒；读侧摘要取已审核过渡的后值，evidence 层摘要取 evidence 层过渡登记的后值，并由独立测试逐项逆向证明。"""

        current = {
            "wire_producer_sha256": self.identity["wire_producer_sha256"],
            "evidence_semantics_sha256": self.identity["evidence_semantics_sha256"],
            **{field: self.evaluator[field] for field in tip.EVALUATOR_DIGEST_FIELDS},
        }
        expected = {field: self.baseline[field] for field in current}
        for change in REGISTERED_EVALUATOR_READER_CHANGES.values():
            for field, _roots in READER_FIELDS:
                self.assertEqual(change[field]["before"], self.baseline[field], f"登记的变化前摘要必须就是基线：{field}")
                expected[field] = change[field]["after"]
        for field, _roots in READER_FIELDS:
            expected[field] = self.successor4["after_readers"][field]["closure_sha256"]
        # evidence 层摘要被事实映射源码快照重绑与映射摘要常量同步改变：登记的前值必须就是基线，期望取登记的后值；
        # 从后值逐字节逆向回基线由 test_evidence_transition_reverse_proof_restores_baseline_digests 独立证明。
        evidence = self.evidence_transition["sensitive_identity"]
        self.assertEqual(evidence["before"]["evidence_semantics_sha256"], self.baseline["evidence_semantics_sha256"],
                         "登记的变化前摘要必须就是基线：evidence_semantics_sha256")
        expected["evidence_semantics_sha256"] = evidence["after"]["evidence_semantics_sha256"]
        for field, value in current.items():
            self.assertEqual(value, expected[field], f"Codex 身份敏感摘要变化：{field}")
        for layer, expected in self.baseline["orchestrator_closure_sha256"].items():
            self.assertEqual(
                self.identity["orchestrator_closures"][layer]["closure_sha256"],
                expected,
                f"Codex 编排器 {layer} 闭包变化",
            )

    def test_wire_closure_unchanged_for_tool_evolution(self) -> None:
        """④ tool-evolution 预览按编排器 wire 闭包摘要判定 ``wire_closure_changed``，改造前后必须为 false。"""

        self.assertEqual(
            self.identity["orchestrator_closures"]["wire_producer"]["closure_sha256"],
            self.baseline["orchestrator_closure_sha256"]["wire_producer"],
        )

    def test_tree_changes_are_registered_control_changes(self) -> None:
        """② control、策略与整树摘要的变化逐文件对应登记清单；不得有按默认层归类的文件。

        evidence 层文件的变化不走 control 登记：只有 evidence 层过渡登记里前后摘要都精确匹配的两个文件放行，
        其余 evidence 层变化与 wire 层变化照常因“未登记”失败。"""

        before = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        after = {entry["path"]: entry["sha256"] for entry in self.entries}
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        for path in changed:
            layer = tip.classify_path(self.policy, path)
            if layer is None:
                continue
            if layer == "evidence_semantics":
                continue  # evidence 层只经过渡登记放行，由下面的 _evidence_change_problems 逐文件核对
            self.assertIn(path, REGISTERED_CONTROL_CHANGES, f"Codex 受管文件变化未登记：{path}（{layer} 层）")
            self.assertEqual(layer, "control", f"Codex 受管文件变化不在 control 层：{path}（{layer} 层）")
        self.assertEqual(_evidence_change_problems(self.policy, before, after, self.evidence_registrations), [],
                         "Codex evidence 层文件变化必须与 evidence 层过渡登记逐文件精确一致")
        self.assertEqual(self.identity["defaulted_paths"], [], "新增受管文件必须在 Codex 策略里登记层级")
        self.assertEqual(
            self.identity["policy_sha256"] != self.baseline["policy_sha256"],
            tip.POLICY_FILENAME in changed,
            "策略摘要变化必须对应策略文件的登记变化",
        )

    def test_evaluator_reader_change_is_only_the_registered_symbols(self) -> None:
        """⑤ 逐符号、整文件三步逆向，再还原 B5a-08 原两常量，完整图严格回到不变基线。"""

        self.assertEqual(sorted(REGISTERED_EVALUATOR_READER_CHANGES), ["B5a-08"], "B5a-08 的形成期登记不得原地覆盖")
        change = REGISTERED_EVALUATOR_READER_CHANGES["B5a-08"]
        self.assertEqual(sorted(change["symbols"]), ["codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES",
                                                     "codex_upgrade_gate_receipt:TARGET_VERIFIER_SHA256S"])
        self.assertEqual(len(change["reasons"]), 4)
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(self.readers[field], self.successor4["after_readers"][field])
                restored = _restore_registered_reader(self.readers[field], field, self.transition)
                self.assertEqual(tip._fingerprint(restored), self.baseline[field])
                self.assertEqual(restored, _reader_payload(self.transition["before_readers"][field]))

    def test_reader_transition_is_frozen_and_scope_is_exact(self) -> None:
        """冻结完整原图和三步精确登记；不允许通配符、删除旧依赖或借元数据注册别的函数。"""

        transition = self.transition
        self.assertEqual(transition["baseline_sha256"], BASELINE_SHA256)
        self.assertEqual(transition["source_commits"]["baseline"], self.baseline["source_commit"])
        self.assertEqual(transition["source_commits"]["after"], "58dcc47ab6ae27bbd034ec515bc4b74e4ed4f727")
        self.assertEqual(transition["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(transition["sensitive_identity"]["before"], transition["sensitive_identity"]["after"])
        self.assertEqual(tuple(step["phase"] for step in transition["steps"]), READER_TRANSITION_PHASES)
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = transition["before_readers"][field], transition["after_readers"][field]
                self.assertEqual(tip._fingerprint(_reader_payload(before)), self.baseline[field])
                self.assertEqual(tip._fingerprint(_reader_payload(after)), after["closure_sha256"])
                for key in ("schema_version", "roots", "barriers", "module_barriers"):
                    self.assertEqual(before[key], after[key], f"不能改变原图元数据：{key}")
                first = transition["steps"][0]["readers"][field]
                self.assertEqual(first["before_sha256"], REGISTERED_EVALUATOR_READER_CHANGES["B5a-08"][field]["after"])
                self.assertEqual({record["key"] for record in first["symbols"] if record["before"] is None},
                                 REGISTERED_ADAPTER_DEPENDENCIES)
                self.assertEqual({record["key"] for record in first["symbols"] if record["before"] is not None},
                                 {REGISTERED_D4_SYMBOL})
                for index, step in enumerate(transition["steps"]):
                    records = step["readers"][field]
                    self.assertTrue(all(record["after"] is not None and record["reason"] for record in records["symbols"]))
                    self.assertEqual([record["key"] for record in records["modules"]], ["upgrade_client_adapter"])
                    if index:
                        self.assertEqual(records["before_sha256"], transition["steps"][index - 1]["readers"][field]["after_sha256"])
                        self.assertEqual({record["key"] for record in records["symbols"]}, REGISTERED_METADATA_SYMBOLS)
                        self.assertTrue(all(record["before"] is not None for record in records["symbols"] + records["modules"]))
                    else:
                        self.assertIsNone(records["modules"][0]["before"])
                self.assertEqual(transition["steps"][-1]["readers"][field]["after_sha256"], after["closure_sha256"])

    def test_pin_successor_is_frozen_and_scope_is_exact(self) -> None:
        """本轮只连两常量后继，不覆盖旧登记、不改原图或身份敏感产出。"""

        successor = self.successor
        self.assertEqual(successor["before_readers"], self.transition["after_readers"])
        self.assertEqual(successor["sensitive_identity"]["before"], self.transition["sensitive_identity"]["after"])
        self.assertEqual(successor["sensitive_identity"]["after"], successor["sensitive_identity"]["before"])
        self.assertEqual(successor["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(successor["source_commits"], {
            "before": "f99137c82d44adfab8d95b53005b65f5b8de401c",
            "after": "35b89b49945c685683e2ea8e38279667d221b9ac",
        })
        self.assertEqual(successor["source_file_sha256"]["before"], {
            "codex_upgrade_gate_receipt.py": "48cc60ff1419ba51b1d812e3ef30332e473b186a602c43831bd7ee4ad91a62de"})
        self.assertEqual(successor["source_file_sha256"]["after"], {
            "codex_upgrade_gate_receipt.py": "4545caf5da2d4ca6e090239c08bfde463a56dfc36e324f0afa802a81fd954bf0"})
        self.assertEqual(tuple(step["phase"] for step in successor["steps"]), (READER_SUCCESSOR_PHASE,))
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = successor["before_readers"][field], successor["after_readers"][field]
                self.assertEqual(after, self.successor2["before_readers"][field])
                for key in ("schema_version", "roots", "barriers", "module_barriers", "modules"):
                    self.assertEqual(before[key], after[key], f"两常量后继不能改变原图字段：{key}")
                old = {f"{row['module']}:{row['name']}": row for row in before["symbols"]}
                new = {f"{row['module']}:{row['name']}": row for row in after["symbols"]}
                self.assertEqual({key for key in old.keys() | new.keys() if old.get(key) != new.get(key)},
                                 REGISTERED_PIN_SUCCESSOR_SYMBOLS)
                self.assertEqual(_restore_pinned_reader_successor(after, field, successor, self.transition), before)

    def test_pin_successor_missing_extra_or_changed_record_is_rejected(self) -> None:
        """精确后继少一项、夹带符号、改变后值或原件均不能通过。"""

        for field, _roots in READER_FIELDS:
            for case in ("缺常量", "额外常量", "改变后值", "改变前图"):
                damaged = copy.deepcopy(self.successor)
                records = damaged["steps"][0]["readers"][field]["symbols"]
                if case == "缺常量":
                    records.pop()
                elif case == "额外常量":
                    records.append({**copy.deepcopy(records[0]), "key": "unregistered:extra"})
                elif case == "改变后值":
                    records[0]["after"]["sha256"] = "0" * 64
                else:
                    damaged["before_readers"][field]["barriers"].append("unregistered")
                with self.subTest(field=field, case=case), self.assertRaises(ValueError):
                    _restore_pinned_reader_successor(self.successor2["before_readers"][field], field, damaged, self.transition)
        with tempfile.TemporaryDirectory(prefix="codex-pin-successor-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_SUCCESSOR_PATH.name
            path.write_bytes(READER_SUCCESSOR_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                _load_reader_successor(path)

    def test_replay_registry_successor_is_frozen_and_scope_is_exact(self) -> None:
        """第二后继只连一个常量，不覆盖前两份登记、不改原图或身份敏感产出。"""

        successor2 = self.successor2
        self.assertEqual(successor2["before_readers"], self.successor["after_readers"])
        self.assertEqual(successor2["sensitive_identity"]["before"], self.successor["sensitive_identity"]["after"])
        self.assertEqual(successor2["sensitive_identity"]["after"], successor2["sensitive_identity"]["before"])
        self.assertEqual(successor2["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(successor2["source_commits"], {
            "before": "41082f2ee850c6a2056a317f839231b3e5584b73",
            "after": "859876955b446f5beff435dbec05e0f6430af16a",
        })
        self.assertEqual(successor2["source_file_sha256"]["before"], {
            "codex_upgrade_gate_receipt.py": "4545caf5da2d4ca6e090239c08bfde463a56dfc36e324f0afa802a81fd954bf0"})
        self.assertEqual(successor2["source_file_sha256"]["after"], {
            "codex_upgrade_gate_receipt.py": "77b57e7c677cbbe4b137a8fb5ac95554bbdd5e8fab0a92883335cb50e7ad4a38"})
        self.assertEqual(tuple(step["phase"] for step in successor2["steps"]), (READER_SUCCESSOR2_PHASE,))
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = successor2["before_readers"][field], successor2["after_readers"][field]
                self.assertEqual(after, self.successor3["before_readers"][field])
                for key in ("schema_version", "roots", "barriers", "module_barriers", "modules"):
                    self.assertEqual(before[key], after[key], f"第二后继不能改变原图字段：{key}")
                old = {f"{row['module']}:{row['name']}": row for row in before["symbols"]}
                new = {f"{row['module']}:{row['name']}": row for row in after["symbols"]}
                self.assertEqual({key for key in old.keys() | new.keys() if old.get(key) != new.get(key)},
                                 REGISTERED_REPLAY_SUCCESSOR_SYMBOLS)
                self.assertEqual(_restore_pinned_reader_successor2(after, field, successor2, self.successor), before)

    def test_replay_registry_successor_missing_extra_or_changed_record_is_rejected(self) -> None:
        """第二后继少一项、夹带符号、改变后值、改前图或改原件均不能通过。"""

        for field, _roots in READER_FIELDS:
            for case in ("缺常量", "额外常量", "改变后值", "改变前图"):
                damaged = copy.deepcopy(self.successor2)
                records = damaged["steps"][0]["readers"][field]["symbols"]
                if case == "缺常量":
                    records.pop()
                elif case == "额外常量":
                    records.append({**copy.deepcopy(records[0]), "key": "unregistered:extra"})
                elif case == "改变后值":
                    records[0]["after"]["sha256"] = "0" * 64
                else:
                    damaged["before_readers"][field]["barriers"].append("unregistered")
                with self.subTest(field=field, case=case), self.assertRaises(ValueError):
                    _restore_pinned_reader_successor2(self.successor3["before_readers"][field], field, damaged, self.successor)
        with tempfile.TemporaryDirectory(prefix="codex-replay-registry-successor-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_SUCCESSOR2_PATH.name
            path.write_bytes(READER_SUCCESSOR2_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                _load_reader_successor2(path)

    def test_step8_pin_successor_is_frozen_and_scope_is_exact(self) -> None:
        """第三后继只连两个既有常量（三个验证器钉住值＋只读重放登记），不覆盖前三份登记、不改原图或身份敏感产出。"""

        successor3 = self.successor3
        self.assertEqual(successor3["before_readers"], self.successor2["after_readers"])
        self.assertEqual(successor3["sensitive_identity"]["before"], self.successor2["sensitive_identity"]["after"])
        self.assertEqual(successor3["sensitive_identity"]["after"], successor3["sensitive_identity"]["before"])
        self.assertEqual(successor3["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(successor3["source_commits"], {
            "before": "e9c31b016a09090ea5c426c4676617b3e3cf1f24",
            "after": "0a779bb72255cebd7a66b90b05a9c42dda6bacdb",
        })
        self.assertEqual(successor3["source_file_sha256"]["before"], {
            "codex_upgrade_gate_receipt.py": "77b57e7c677cbbe4b137a8fb5ac95554bbdd5e8fab0a92883335cb50e7ad4a38"})
        self.assertEqual(successor3["source_file_sha256"]["after"], {
            "codex_upgrade_gate_receipt.py": "fa764324c944ed716fa838b7260fb40540095140f14bbc918b8ac9beacbe5cb6"})
        self.assertEqual(tuple(step["phase"] for step in successor3["steps"]), (READER_SUCCESSOR3_PHASE,))
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = successor3["before_readers"][field], successor3["after_readers"][field]
                self.assertEqual(after, self.successor4["before_readers"][field])
                for key in ("schema_version", "roots", "barriers", "module_barriers", "modules"):
                    self.assertEqual(before[key], after[key], f"第三后继不能改变原图字段：{key}")
                old = {f"{row['module']}:{row['name']}": row for row in before["symbols"]}
                new = {f"{row['module']}:{row['name']}": row for row in after["symbols"]}
                self.assertEqual({key for key in old.keys() | new.keys() if old.get(key) != new.get(key)},
                                 REGISTERED_PIN_SUCCESSOR_SYMBOLS)
                self.assertEqual(_restore_pinned_reader_successor3(after, field, successor3, self.successor2), before)

    def test_step8_pin_successor_missing_extra_or_changed_record_is_rejected(self) -> None:
        """第三后继少一项、夹带符号、改变后值、改前图或改原件均不能通过。"""

        for field, _roots in READER_FIELDS:
            for case in ("缺常量", "额外常量", "改变后值", "改变前图"):
                damaged = copy.deepcopy(self.successor3)
                records = damaged["steps"][0]["readers"][field]["symbols"]
                if case == "缺常量":
                    records.pop()
                elif case == "额外常量":
                    records.append({**copy.deepcopy(records[0]), "key": "unregistered:extra"})
                elif case == "改变后值":
                    records[0]["after"]["sha256"] = "0" * 64
                else:
                    damaged["before_readers"][field]["barriers"].append("unregistered")
                with self.subTest(field=field, case=case), self.assertRaises(ValueError):
                    _restore_pinned_reader_successor3(self.successor4["before_readers"][field], field, damaged, self.successor2)
        with tempfile.TemporaryDirectory(prefix="codex-step8-pin-successor-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_SUCCESSOR3_PATH.name
            path.write_bytes(READER_SUCCESSOR3_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                _load_reader_successor3(path)

    def test_step10_pin_successor_is_frozen_and_scope_is_exact(self) -> None:
        """第四后继只连两个既有常量（read_audit／read_audit_runtime 两个验证器钉住值＋只读重放登记），不覆盖前四份登记、不改原图或身份敏感产出。"""

        successor4 = self.successor4
        self.assertEqual(successor4["before_readers"], self.successor3["after_readers"])
        self.assertEqual(successor4["sensitive_identity"]["before"], self.successor3["sensitive_identity"]["after"])
        self.assertEqual(successor4["sensitive_identity"]["after"], successor4["sensitive_identity"]["before"])
        self.assertEqual(successor4["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(successor4["source_commits"], {
            "before": "b5cceb46f455b1e4aa9d27c791e55899fcfb85e9",
            "after": "248f4312519054e85fa9a43e3f9dc0e2d3ee68f8",
        })
        self.assertEqual(successor4["source_file_sha256"]["before"], {
            "codex_upgrade_gate_receipt.py": "fa764324c944ed716fa838b7260fb40540095140f14bbc918b8ac9beacbe5cb6"})
        self.assertEqual(successor4["source_file_sha256"]["after"], {
            "codex_upgrade_gate_receipt.py": hashlib.sha256((TOOL_ROOT / "codex_upgrade_gate_receipt.py").read_bytes()).hexdigest()})
        self.assertEqual(tuple(step["phase"] for step in successor4["steps"]), (READER_SUCCESSOR4_PHASE,))
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = successor4["before_readers"][field], successor4["after_readers"][field]
                self.assertEqual(after, self.readers[field])
                for key in ("schema_version", "roots", "barriers", "module_barriers", "modules"):
                    self.assertEqual(before[key], after[key], f"第四后继不能改变原图字段：{key}")
                old = {f"{row['module']}:{row['name']}": row for row in before["symbols"]}
                new = {f"{row['module']}:{row['name']}": row for row in after["symbols"]}
                self.assertEqual({key for key in old.keys() | new.keys() if old.get(key) != new.get(key)},
                                 REGISTERED_PIN_SUCCESSOR_SYMBOLS)
                self.assertEqual(_restore_pinned_reader_successor4(after, field, successor4, self.successor3), before)

    def test_step10_pin_successor_missing_extra_or_changed_record_is_rejected(self) -> None:
        """第四后继少一项、夹带符号、改变后值、改前图或改原件均不能通过。"""

        for field, _roots in READER_FIELDS:
            for case in ("缺常量", "额外常量", "改变后值", "改变前图"):
                damaged = copy.deepcopy(self.successor4)
                records = damaged["steps"][0]["readers"][field]["symbols"]
                if case == "缺常量":
                    records.pop()
                elif case == "额外常量":
                    records.append({**copy.deepcopy(records[0]), "key": "unregistered:extra"})
                elif case == "改变后值":
                    records[0]["after"]["sha256"] = "0" * 64
                else:
                    damaged["before_readers"][field]["barriers"].append("unregistered")
                with self.subTest(field=field, case=case), self.assertRaises(ValueError):
                    _restore_pinned_reader_successor4(self.readers[field], field, damaged, self.successor3)
        with tempfile.TemporaryDirectory(prefix="codex-step10-pin-successor-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_SUCCESSOR4_PATH.name
            path.write_bytes(READER_SUCCESSOR4_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                _load_reader_successor4(path)

    def test_d4_restored_dependencies_are_original_and_not_an_exemption(self) -> None:
        """D4 恢复旧 23／35 项，没有把缺陷时期的丢读集当成可接受的基线。"""

        legacy = REGISTERED_EVALUATOR_READER_CHANGES["B5a-08"]
        for field, count in (("compare_reader_sha256", 23), ("accept_reader_sha256", 35)):
            with self.subTest(field=field):
                original = {f"{row['module']}:{row['name']}": row for row in self.transition["before_readers"][field]["symbols"]}
                current = {f"{row['module']}:{row['name']}": row for row in self.readers[field]["symbols"]}
                restored = self.transition["d4_restored_original_dependencies"]["readers"][field]
                self.assertEqual(len(restored["restored_old_symbols"]), count)
                self.assertTrue(restored["whole_modules_unchanged"])
                self.assertEqual([record["key"] for record in restored["changed_symbols"]], [REGISTERED_D4_SYMBOL])
                for row in restored["restored_old_symbols"]:
                    key = f"{row['module']}:{row['name']}"
                    self.assertIn(key, original)
                    expected = copy.deepcopy(original[key])
                    if key in legacy["symbols"]:
                        expected["sha256"] = legacy["symbols"][key]["after"]
                    self.assertEqual(row, expected)
                    self.assertEqual(current[key], row, f"D4 已恢复的旧依赖不得再次丢失或被修改：{key}")

    def test_unregistered_graph_changes_are_rejected(self) -> None:
        """原符号、已登记符号、整文件、额外依赖与屏障漂移都拒绝，重新计算损坏图摘要也不能放过。"""

        for field, _roots in READER_FIELDS:
            cases: list[tuple[str, dict[str, Any]]] = []
            original_key = next(row for row in self.readers[field]["symbols"] if row["module"] == "codex_upgrade_reconciler"
                                and row["name"] == "segment_reuse_proofs")
            for label, predicate in (
                ("未登记原证明符号", lambda row: row == original_key),
                ("已登记 D4 函数新变化", lambda row: f"{row['module']}:{row['name']}" == REGISTERED_D4_SYMBOL),
                ("已登记适配常量新变化", lambda row: f"{row['module']}:{row['name']}" in REGISTERED_METADATA_SYMBOLS),
            ):
                damaged = copy.deepcopy(self.readers[field])
                next(row for row in damaged["symbols"] if predicate(row))["sha256"] = "0" * 64
                cases.append((label, damaged))
            for label, module in (("既有整文件新变化", self.readers[field]["modules"][0]["module"]),
                                  ("适配器整文件新变化", "upgrade_client_adapter")):
                damaged = copy.deepcopy(self.readers[field])
                next(row for row in damaged["modules"] if row["module"] == module)["sha256"] = "0" * 64
                cases.append((label, damaged))
            damaged = copy.deepcopy(self.readers[field]); damaged["symbols"].remove(original_key)
            cases.append(("缺失旧依赖", damaged))
            damaged = copy.deepcopy(self.readers[field])
            damaged["symbols"].append({"module": "codex_upgrade_reconciler", "name": "unregistered_proof", "kind": "function", "sha256": "0" * 64})
            cases.append(("额外符号", damaged))
            damaged = copy.deepcopy(self.readers[field])
            damaged["modules"].append({"module": "unregistered_module", "path": "unregistered_module.py", "sha256": "0" * 64})
            cases.append(("额外整文件", damaged))
            for key in ("roots", "barriers", "module_barriers"):
                damaged = copy.deepcopy(self.readers[field]); damaged[key].append("unregistered")
                cases.append((f"原图元数据变化 {key}", damaged))
            for label, damaged in cases:
                with self.subTest(field=field, case=label):
                    damaged["closure_sha256"] = tip._fingerprint(_reader_payload(damaged))
                    with self.assertRaises(ValueError):
                        _restore_registered_reader(damaged, field, self.transition)

    def test_transition_steps_cannot_drop_records_or_change_order(self) -> None:
        """登记缺一项、漏掉整文件逆向或交换阶段都不能重新拼出原图。"""

        for field, _roots in READER_FIELDS:
            for collection in ("symbols", "modules"):
                damaged = copy.deepcopy(self.transition)
                damaged["steps"][0]["readers"][field][collection].pop()
                with self.subTest(field=field, missing=collection), self.assertRaises(ValueError):
                    _restore_registered_reader(self.readers[field], field, damaged)
            damaged = copy.deepcopy(self.transition)
            damaged["steps"][0], damaged["steps"][1] = damaged["steps"][1], damaged["steps"][0]
            with self.subTest(field=field, case="次序"), self.assertRaises(ValueError):
                _restore_registered_reader(self.readers[field], field, damaged)

    def test_modified_transition_original_is_rejected(self) -> None:
        """改过渡原件不能获得一个新期望；SHA 不符立即失败。"""

        with tempfile.TemporaryDirectory(prefix="codex-reader-transition-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_TRANSITION_PATH.name
            path.write_bytes(READER_TRANSITION_PATH.read_bytes() + b"\n")
            with self.assertRaises(ValueError):
                _load_reader_transition(path)

    def test_real_source_proof_mutation_and_whole_file_drift_are_rejected(self) -> None:
        """正式算法在副本源码上翻转旧证明比较／增加适配器文件注释，门禁拒绝对应未登记变化。"""

        entries = {entry["path"]: entry["sha256"] for entry in self.entries}
        for case in ("原证明判据", "适配器整文件"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="codex-reader-source-", dir="/private/tmp") as temporary:
                root = Path(temporary)
                for relative in entries:
                    if relative.endswith(".py") and not relative.startswith("tests/"):
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(TOOL_ROOT / relative, target)
                if case == "原证明判据":
                    relative = "codex_upgrade_reconciler.py"
                    path = root / relative
                    source = path.read_text(encoding="utf-8")
                    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                                and node.name == "segment_reuse_proofs")
                    segment = ast.get_source_segment(source, node)
                    self.assertIn(" != ", segment)
                    path.write_text(source.replace(segment, segment.replace(" != ", " == ", 1), 1), encoding="utf-8")
                else:
                    relative = "upgrade_client_adapter.py"
                    path = root / relative
                    path.write_bytes(path.read_bytes() + "\n# 未登记的整文件读集变化。\n".encode("utf-8"))
                digests = {**entries, relative: hashlib.sha256(path.read_bytes()).hexdigest()}
                for field, roots in READER_FIELDS:
                    closure = tip.evaluator_reader_closure(self.policy, root, list(roots), digests)
                    self.assertNotEqual(closure["closure_sha256"], self.readers[field]["closure_sha256"])
                    with self.assertRaises(ValueError):
                        _restore_registered_reader(closure, field, self.transition)

    # ------------------------------------------------------------------
    # evidence 层过渡登记：事实映射的源码快照重绑与映射摘要常量同步
    # ------------------------------------------------------------------

    @staticmethod
    def _unique_hex_token(raw: bytes, excluded: set[str]) -> str:
        """文件里恰好出现一次、且不在 excluded 里的一个 64 位十六进制串；用来构造“登记外”的替换。"""

        tokens = HEX64.findall(raw.decode("utf-8"))
        for token in tokens:
            if tokens.count(token) == 1 and token not in excluded:
                return token
        raise AssertionError("文件里没有可用的唯一十六进制串")

    @staticmethod
    def _flip_last_character(text: str) -> str:
        """改掉文本最后一个字符，得到一个必然不同、长度不变的文本。"""

        return text[:-1] + ("0" if text[-1] != "0" else "1")

    def _restore_evidence(self, transition: dict[str, Any] | None = None, *, entries: list[dict[str, str]] | None = None,
                          read_file: Callable[[str], bytes] | None = None, closure: str | None = None,
                          policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """用真实的基线、策略与当前受管树调用整条 evidence 层逆向证明；各参数用来换成被篡改的输入。"""

        return _restore_registered_evidence(
            transition or self.evidence_transition, baseline=self.baseline, policy=policy or self.policy,
            entries=entries or self.entries,
            evidence_closure_sha256=closure or self.identity["orchestrator_closures"]["evidence_semantics"]["closure_sha256"],
            read_file=read_file)

    def _tampered_world(self, path: str, mutate: Callable[[bytes], bytes]) -> tuple[bytes, list[dict[str, str]], Callable[[str], bytes]]:
        """把一个 evidence 层文件改成 mutate(原字节) 的世界：返回新字节、该文件摘要同步的受管树清单和读文件函数。"""

        original = _read_evidence_file(path)
        tampered = mutate(original)
        self.assertNotEqual(tampered, original, "构造的篡改必须真的改了文件")
        digest = hashlib.sha256(tampered).hexdigest()
        entries = [{**entry, "sha256": digest} if entry["path"] == path else entry for entry in self.entries]
        return tampered, entries, lambda requested: tampered if requested == path else _read_evidence_file(requested)

    def test_evidence_transition_is_frozen_and_scope_is_exact(self) -> None:
        """evidence 层过渡登记：原件钉死，前值就是基线，后值就是当前真实值，范围只含事实映射与映射摘要常量两个 evidence 层文件。"""

        transition = self.evidence_transition
        self.assertEqual(transition["baseline_sha256"], BASELINE_SHA256)
        self.assertEqual(transition["source_commits"], {"baseline": self.baseline["source_commit"]})
        self.assertEqual(transition["algorithm_sha256"], hashlib.sha256(Path(tip.__file__).read_bytes()).hexdigest())
        self.assertEqual(transition["layer"], "evidence_semantics")
        # 敏感身份：前值逐字等于基线，后值逐字等于当前真实计算值，两者只差 evidence 层摘要一项。
        before, after = transition["sensitive_identity"]["before"], transition["sensitive_identity"]["after"]
        self.assertEqual(before, {
            "builder_sha256": self.baseline["builder_sha256"],
            "checker_sha256": self.baseline["checker_sha256"],
            "evidence_semantics_sha256": self.baseline["evidence_semantics_sha256"],
            "orchestrator_closure_sha256": self.baseline["orchestrator_closure_sha256"],
            "wire_producer_sha256": self.baseline["wire_producer_sha256"],
        })
        self.assertEqual(after, {
            "builder_sha256": self.evaluator["builder_sha256"],
            "checker_sha256": self.evaluator["checker_sha256"],
            "evidence_semantics_sha256": self.identity["evidence_semantics_sha256"],
            "orchestrator_closure_sha256": {
                layer: self.identity["orchestrator_closures"][layer]["closure_sha256"]
                for layer in ("evidence_semantics", "wire_producer")},
            "wire_producer_sha256": self.identity["wire_producer_sha256"],
        })
        self.assertEqual({key for key in before if before[key] != after[key]}, {"evidence_semantics_sha256"})
        # 文件范围：正好两个 evidence 层文件，前后摘要钉在测试里；它们不在 control 登记清单里。
        registrations = self.evidence_registrations
        self.assertEqual(set(registrations), EVIDENCE_TRANSITION_PATHS)
        self.assertEqual(set(registrations) & set(REGISTERED_CONTROL_CHANGES), set(), "evidence 层文件不得冒充 control 层登记")
        baseline_files = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        current_files = {entry["path"]: entry["sha256"] for entry in self.entries}
        pinned = {
            EVIDENCE_FACT_MAP: ("0126f35add457cee871808eed63f7be7d94f5dd2361bf1b33ef282c712409370",
                                "5d4c2b7a7ee6c5e8c28235cfd6458805aeddac403057346357b3c57d70c2a957"),
            EVIDENCE_TRACE: ("b8e53183cd572b1623a5c8fb90b93b08bed1976b1480b2b1ba79f5e4db6303c0",
                             "4c44c20a41c32f4b9163c26aea37bda392487e173f01b77df7bf1fa589c308a9"),
        }
        for path, (old, new) in pinned.items():
            with self.subTest(path=path):
                registration = registrations[path]
                self.assertEqual(tip.classify_path(self.policy, path), "evidence_semantics")
                self.assertEqual((registration["before_sha256"], registration["after_sha256"]), (old, new))
                self.assertEqual((baseline_files[path], current_files[path]), (old, new))
                self.assertEqual(len(registration["replacements"]), 1, "累计替换只登记一处")
        fact_map = registrations[EVIDENCE_FACT_MAP]
        self.assertEqual(fact_map["kind"], "fact-map-source-snapshot-rebind")
        (snapshot,) = fact_map["replacements"]
        self.assertEqual(snapshot["locator"], {
            "test": "TestCandidateTraceCodex0145OAuthFallbackSameInvocation",
            "source_file": "backend/internal/service/openai_gateway_forward.go",
        })
        self.assertEqual((snapshot["before"], snapshot["after"]), (
            "89300984a3d93e7389fe6577337f589648ee83618f2be1fcb1c5155df3551ce2",
            "3d41a73eeaef7107163154a2e2c833d6a09296ec41d2cb3cc211f0613b6c2fca",
        ))
        self.assertEqual(registrations[EVIDENCE_TRACE]["kind"], "frozen-mapping-sha256-constant")
        # 两次重绑（主干修复、上游 v0.2.17 合并）的历史链与累计替换首尾相接，映射文件摘要同理。
        history = transition["history"]
        self.assertEqual(len(history), 2)
        self.assertRegex(history[0]["commit"], r"^654b62efb[0-9a-f]{31}$")
        snapshots, digests = [step["source_snapshot"] for step in history], [step["fact_map_sha256"] for step in history]
        self.assertEqual(snapshots[0]["before"], snapshot["before"])
        self.assertEqual(snapshots[0]["after"], snapshots[1]["before"])
        self.assertEqual(snapshots[1]["after"], snapshot["after"])
        self.assertEqual(digests[0]["before"], fact_map["before_sha256"])
        self.assertEqual(digests[0]["after"], digests[1]["before"])
        self.assertEqual(digests[1]["after"], fact_map["after_sha256"])

    def test_evidence_transition_reverse_proof_restores_baseline_digests(self) -> None:
        """逆向证明：当前两个 evidence 层文件逐处换回登记的前值，文件摘要回到基线条目，重算的 evidence 层摘要回到基线。"""

        restored = self._restore_evidence()
        baseline_files = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        self.assertEqual(restored["files"], {path: baseline_files[path] for path in EVIDENCE_TRANSITION_PATHS})
        self.assertEqual(restored["evidence_semantics_sha256"], self.baseline["evidence_semantics_sha256"])
        # 当前 evidence 层摘要正是登记的后值，与逆向回到的基线不同：变化确实存在，并被登记完整解释。
        after = self.evidence_transition["sensitive_identity"]["after"]["evidence_semantics_sha256"]
        self.assertEqual(self.identity["evidence_semantics_sha256"], after)
        self.assertNotEqual(after, restored["evidence_semantics_sha256"])
        # 事实映射：后值在当前文件里恰好一次、前值一次也没有；逆向后正好相反，长度不变（纯 64 位摘要重绑）。
        registration = self.evidence_registrations[EVIDENCE_FACT_MAP]
        (snapshot,) = registration["replacements"]
        raw = _read_evidence_file(EVIDENCE_FACT_MAP)
        original = _restore_registered_evidence_file(raw, registration)
        after_text, before_text = snapshot["after"].encode(), snapshot["before"].encode()
        self.assertEqual((raw.count(after_text), raw.count(before_text)), (1, 0))
        self.assertEqual((original.count(after_text), original.count(before_text)), (0, 1))
        self.assertEqual(len(raw), len(original))

    def test_evidence_transition_changes_only_registered_snapshot_fields(self) -> None:
        """结构证明：事实映射与逆向还原后只差登记的那一个源码快照 sha256；映射摘要常量文件除登记的注释与常量赋值外语法树逐节点相同。"""

        fact_registration = self.evidence_registrations[EVIDENCE_FACT_MAP]
        raw = _read_evidence_file(EVIDENCE_FACT_MAP)
        original = _restore_registered_evidence_file(raw, fact_registration)
        _prove_fact_map_structure(raw, original, fact_registration)
        current, before = json.loads(raw), json.loads(original)
        differences = _json_differences(current, before)
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0][0::2], ("tests", "source_files", "sha256"))
        test = current["tests"][differences[0][1]]
        self.assertEqual((test["name"], test["source_files"][differences[0][3]]["path"]),
                         ("TestCandidateTraceCodex0145OAuthFallbackSameInvocation",
                          "backend/internal/service/openai_gateway_forward.go"))
        # 其余逐项相同：顶层字段、测试清单（包、名字、测试文件及其摘要）、事实、源码快照的路径清单。
        self.assertEqual({key: value for key, value in current.items() if key != "tests"},
                         {key: value for key, value in before.items() if key != "tests"})
        keys = ("package", "name", "test_file", "test_file_sha256", "facts")
        self.assertEqual([[item[key] for key in keys] for item in current["tests"]],
                         [[item[key] for key in keys] for item in before["tests"]])
        self.assertEqual([[source["path"] for source in item["source_files"]] for item in current["tests"]],
                         [[source["path"] for source in item["source_files"]] for item in before["tests"]])

        trace_registration = self.evidence_registrations[EVIDENCE_TRACE]
        trace_raw = _read_evidence_file(EVIDENCE_TRACE)
        trace_original = _restore_registered_evidence_file(trace_raw, trace_registration)
        _prove_frozen_mapping_constant(trace_registration, fact_registration)

        def mapping_constant(source: bytes) -> str:
            node = next(node for node in ast.parse(source.decode("utf-8")).body if isinstance(node, ast.Assign)
                        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "FROZEN_MAPPING_SHA256")
            return node.value.value

        def code_without_mapping_constant(source: bytes) -> str:
            tree = ast.parse(source.decode("utf-8"))
            for node in tree.body:
                if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "FROZEN_MAPPING_SHA256"):
                    node.value = ast.Constant("")
            return ast.dump(tree)

        # 常量在当前文件里就是当前事实映射文件的摘要，还原后就是基线事实映射的摘要；代码除这一个常量外逐节点相同。
        self.assertEqual(mapping_constant(trace_raw), hashlib.sha256(raw).hexdigest())
        self.assertEqual(mapping_constant(trace_original), fact_registration["before_sha256"])
        self.assertEqual(code_without_mapping_constant(trace_raw), code_without_mapping_constant(trace_original))

    def test_evidence_transition_missing_extra_or_changed_record_is_rejected(self) -> None:
        """登记少一条、多一条、改一条前后值或改文件摘要，字节逆向都回不到基线；文件集合缺／多、前后值不符、闭包变化也拒绝。"""

        for path in sorted(EVIDENCE_TRANSITION_PATHS):
            raw = _read_evidence_file(path)
            registration = self.evidence_registrations[path]
            registered_text = " ".join(item[side] for item in registration["replacements"] for side in ("before", "after"))
            token = self._unique_hex_token(raw, set(HEX64.findall(registered_text)))
            self.assertGreater(raw.count(b"sha256"), 1, "构造“后值文本出现多次”用的文本必须在文件里重复")
            for case, message in (
                ("缺一条替换", "没有回到基线"),
                ("多一条凭空替换", "恰好出现一次（实际 0 次）"),
                ("多一条后值出现多次的替换", r"恰好出现一次（实际 (?:[2-9]|[1-9]\d+) 次）"),
                ("重复登记同一条", "不得重叠"),
                ("多一条无关替换", "没有回到基线"),
                ("改后值", "恰好出现一次（实际 0 次）"),
                ("改前值", "没有回到基线"),
                ("改文件前值", "没有回到基线"),
                ("改文件后值", "不是登记的后值"),
            ):
                damaged = copy.deepcopy(registration)
                replacements = damaged["replacements"]
                if case == "缺一条替换":
                    replacements.pop()
                elif case == "多一条凭空替换":
                    replacements.append({**copy.deepcopy(replacements[0]), "after": "登记里凭空多出的后值文本", "before": "x"})
                elif case == "多一条后值出现多次的替换":
                    replacements.append({**copy.deepcopy(replacements[0]), "after": "sha256", "before": "SHA256"})
                elif case == "重复登记同一条":
                    replacements.append(copy.deepcopy(replacements[0]))
                elif case == "多一条无关替换":
                    replacements.append({**copy.deepcopy(replacements[0]), "after": token, "before": "0" * 64})
                elif case == "改后值":
                    replacements[0]["after"] = self._flip_last_character(replacements[0]["after"])
                elif case == "改前值":
                    replacements[0]["before"] = self._flip_last_character(replacements[0]["before"])
                elif case == "改文件前值":
                    damaged["before_sha256"] = self._flip_last_character(damaged["before_sha256"])
                else:
                    damaged["after_sha256"] = self._flip_last_character(damaged["after_sha256"])
                with self.subTest(path=path, case=case), self.assertRaisesRegex(ValueError, message):
                    _restore_registered_evidence_file(raw, damaged)

        # 整条证明：登记文件集合缺一个、多一个、重复一个，登记的前后值不是基线／当前值，evidence 编排器闭包变化，
        # 登记之外的 evidence 层文件有变化，都拒绝。
        for case, message in (
            ("缺一个文件", "文件集合"),
            ("多一个文件", "文件集合"),
            ("重复一个文件", "文件集合"),
            ("登记前值不是基线", "前值不是基线"),
            ("登记后值不是当前值", "后值不是当前受管树"),
            ("闭包变化", "闭包摘要与基线不符"),
            ("别的 evidence 层文件有变化", "出现未登记的文件变化：candidate_rule_assertion.py"),
            ("别的 evidence 层文件被删除", "重算的 evidence 层摘要不等于基线"),
            ("登记的文件被策略改归别层", "不在 evidence 层"),
        ):
            damaged, closure, entries, policy = copy.deepcopy(self.evidence_transition), None, None, None
            if case == "缺一个文件":
                damaged["files"].pop()
            elif case == "多一个文件":
                damaged["files"].append({**copy.deepcopy(damaged["files"][0]), "path": "candidate_rule_assertion.py"})
            elif case == "重复一个文件":
                damaged["files"].append(copy.deepcopy(damaged["files"][0]))
            elif case == "登记前值不是基线":
                damaged["files"][0]["before_sha256"] = "0" * 64
            elif case == "登记后值不是当前值":
                damaged["files"][0]["after_sha256"] = "0" * 64
            elif case == "闭包变化":
                closure = "0" * 64
            elif case == "别的 evidence 层文件有变化":
                entries = [{**entry, "sha256": "0" * 64} if entry["path"] == "candidate_rule_assertion.py" else entry
                           for entry in self.entries]
            elif case == "别的 evidence 层文件被删除":
                entries = [entry for entry in self.entries if entry["path"] != "candidate_rule_assertion.py"]
            else:
                policy = copy.deepcopy(self.policy)
                policy["layers"]["evidence_semantics"]["prefixes"].remove("candidate_test_fact_map_")
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, message):
                self._restore_evidence(damaged, entries=entries, closure=closure, policy=policy)

        # 结构证明：定位不到条目、定位冒用别的条目、还原结果另有结构或内容差异，都拒绝。
        fact_registration = self.evidence_registrations[EVIDENCE_FACT_MAP]
        fact_raw = _read_evidence_file(EVIDENCE_FACT_MAP)
        fact_original = _restore_registered_evidence_file(fact_raw, fact_registration)
        for case, message in (("定位不到条目", "唯一命中"), ("定位冒用别的条目", "前后值与文件内容不符")):
            damaged = copy.deepcopy(fact_registration)
            locator = damaged["replacements"][0]["locator"]
            if case == "定位不到条目":
                locator["test"] = "TestNoSuchCandidateTrace"
            else:
                locator["source_file"] = "backend/internal/service/official_egress_integration.go"
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, message):
                _prove_fact_map_structure(fact_raw, fact_original, damaged)
        for case, old, new in (
            ("还原结果多一处事实差异", b'"fact_id": "a08.connection-lifecycle"', b'"fact_id": "a08.connection-lifecycle-x"'),
            ("还原结果多一个键", b'"codex_version": "0.160.0",', b'"codex_version": "0.160.0",\n  "unregistered_key": 1,'),
        ):
            altered = fact_original.replace(old, new, 1)
            self.assertNotEqual(altered, fact_original)
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, "之外还有变化"):
                _prove_fact_map_structure(fact_raw, altered, fact_registration)
        # 定位命中多处（同名测试出现两次）同样拒绝，不能靠含糊的定位蒙混。
        located = fact_registration["replacements"][0]["locator"]["test"]
        doubled, doubled_original = json.loads(fact_raw), json.loads(fact_original)
        for data in (doubled, doubled_original):
            data["tests"].append(copy.deepcopy(next(item for item in data["tests"] if item["name"] == located)))
        with self.subTest(case="定位命中多处"), self.assertRaisesRegex(ValueError, "唯一命中"):
            _prove_fact_map_structure(json.dumps(doubled).encode("utf-8"), json.dumps(doubled_original).encode("utf-8"),
                                      fact_registration)

        # 映射摘要常量：夹带（合法或不合法的）代码、常量没随映射文件摘要同步、登记两处、定位改名，都拒绝。
        trace_registration = self.evidence_registrations[EVIDENCE_TRACE]
        hunk_after = trace_registration["replacements"][0]["after"]
        for case, message in (
            ("夹带代码", "只有一条"),
            ("夹带不合法的代码", "合法的 Python"),
            ("常量没随映射摘要同步", "没有随事实映射文件摘要同步"),
            ("登记两处", "恰好登记一处"),
            ("定位改名", "恰好登记一处"),
        ):
            damaged = copy.deepcopy(trace_registration)
            replacement = damaged["replacements"][0]
            if case == "夹带代码":
                replacement["after"] = hunk_after + "import os\n"
            elif case == "夹带不合法的代码":
                replacement["after"] = hunk_after + "((\n"
            elif case == "常量没随映射摘要同步":
                replacement["after"] = hunk_after.replace(fact_registration["after_sha256"], "0" * 64)
            elif case == "登记两处":
                damaged["replacements"].append(copy.deepcopy(replacement))
            else:
                replacement["locator"] = {"constant": "FROZEN_PROFILE_SHA256"}
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, message):
                _prove_frozen_mapping_constant(damaged, fact_registration)

    def test_evidence_transition_unregistered_file_change_is_rejected(self) -> None:
        """事实映射或映射摘要常量文件出现登记外的改动：只改文件、同步改登记的后值、甚至把改动也登记成替换，都拒绝。"""

        fact_raw = _read_evidence_file(EVIDENCE_FACT_MAP)
        last_test_file_sha256 = json.loads(fact_raw)["tests"][-1]["test_file_sha256"].encode()
        mutations = {
            EVIDENCE_FACT_MAP: (
                ("改一条事实", b'"fact_id": "a08.connection-lifecycle"', b'"fact_id": "a08.connection-lifecycle-x"', "前后值与文件内容不符"),
                ("改测试文件摘要", last_test_file_sha256, b"0" * 64, "前后值与文件内容不符"),
                ("改版本号", b'"codex_version": "0.160.0"', b'"codex_version": "0.160.1"', "前后值与文件内容不符"),
            ),
            EVIDENCE_TRACE: (
                ("改导入行", b"from __future__ import annotations",
                 "from __future__ import annotations  # 登记外改动".encode("utf-8"), "恰好登记一处"),
            ),
        }
        for path, cases in mutations.items():
            for case, old, new, structure_message in cases:
                tampered, entries, read_file = self._tampered_world(path, lambda raw, old=old, new=new: raw.replace(old, new, 1))
                # 只改文件：受管树里该文件的摘要已不是登记的后值。
                with self.subTest(path=path, case=case, how="只改文件"), self.assertRaisesRegex(ValueError, "后值不是当前受管树"):
                    self._restore_evidence(entries=entries, read_file=read_file)
                # 同步改登记的后值：字节逆向到不了基线。
                synced = copy.deepcopy(self.evidence_transition)
                item = next(item for item in synced["files"] if item["path"] == path)
                item["after_sha256"] = hashlib.sha256(tampered).hexdigest()
                with self.subTest(path=path, case=case, how="同步改登记后值"), self.assertRaisesRegex(ValueError, "没有回到基线"):
                    self._restore_evidence(synced, entries=entries, read_file=read_file)
                # 把改动也登记成一条替换：字节逆向能回到基线，结构证明拒绝。
                item["replacements"].append({"locator": copy.deepcopy(item["replacements"][0]["locator"]),
                                             "before": old.decode("utf-8"), "after": new.decode("utf-8"), "reason": "冒充登记"})
                with self.subTest(path=path, case=case, how="把改动登记成替换"), self.assertRaisesRegex(ValueError, structure_message):
                    self._restore_evidence(synced, entries=entries, read_file=read_file)

    def test_evidence_tree_change_registration_is_exact_and_not_a_control_change(self) -> None:
        """② 的 evidence 层分支：登记的两个文件前后摘要精确匹配且真的变了；别的 evidence 层变化、登记缺失、再次重绑、
        冒充 control 层登记一律产生问题，REGISTERED_CONTROL_CHANGES 里没有这两个文件。"""

        before = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        after = {entry["path"]: entry["sha256"] for entry in self.entries}
        registrations = self.evidence_registrations
        self.assertEqual(_evidence_change_problems(self.policy, before, after, registrations), [])
        without_trace = {path: item for path, item in registrations.items() if path != EVIDENCE_TRACE}
        for case, new, registered, message in (
            ("事实映射再次重绑", {**after, EVIDENCE_FACT_MAP: "0" * 64}, registrations,
             f"前后摘要与过渡登记不符：{EVIDENCE_FACT_MAP}"),
            ("映射摘要常量文件再次变化", {**after, EVIDENCE_TRACE: "0" * 64}, registrations,
             f"前后摘要与过渡登记不符：{EVIDENCE_TRACE}"),
            ("映射摘要常量文件登记缺失", after, without_trace, f"变化未登记：{EVIDENCE_TRACE}"),
            ("登记的事实映射其实没变", {**after, EVIDENCE_FACT_MAP: before[EVIDENCE_FACT_MAP]}, registrations,
             f"没有变化或不在 evidence 层：{EVIDENCE_FACT_MAP}"),
            ("别的 evidence 层文件变化", {**after, "candidate_rule_assertion.py": "0" * 64}, registrations,
             "变化未登记：candidate_rule_assertion.py"),
            ("新增 evidence 层文件", {**after, "candidate_test_fact_map_0_999_0.json": "0" * 64}, registrations,
             "变化未登记：candidate_test_fact_map_0_999_0.json"),
            ("删除 evidence 层文件", {path: digest for path, digest in after.items() if path != "candidate_rule_assertion.py"},
             registrations, "变化未登记：candidate_rule_assertion.py"),
        ):
            with self.subTest(case=case):
                problems = _evidence_change_problems(self.policy, before, new, registered)
                self.assertTrue(any(message in problem for problem in problems), problems)
        # 策略把事实映射改归别的层：登记不再放行它。
        reclassified = copy.deepcopy(self.policy)
        reclassified["layers"]["evidence_semantics"]["prefixes"].remove("candidate_test_fact_map_")
        problems = _evidence_change_problems(reclassified, before, after, registrations)
        self.assertTrue(any(f"没有变化或不在 evidence 层：{EVIDENCE_FACT_MAP}" in problem for problem in problems), problems)
        # 登记的文件被塞进 control 登记清单冒充 control 层：同样产生问题。
        with mock.patch.dict(REGISTERED_CONTROL_CHANGES, {EVIDENCE_FACT_MAP: "冒充 control 层登记"}):
            problems = _evidence_change_problems(self.policy, before, after, registrations)
        self.assertTrue(any("冒充 control 层" in problem for problem in problems), problems)
        self.assertNotIn(EVIDENCE_FACT_MAP, REGISTERED_CONTROL_CHANGES)
        self.assertNotIn(EVIDENCE_TRACE, REGISTERED_CONTROL_CHANGES)

    def test_tree_change_check_rejects_unregistered_changes_in_every_layer(self) -> None:
        """② 本身在被篡改的受管树清单上运行：evidence 层只放行登记的两个文件，wire 层与 control 层的未登记变化照常拒绝。"""

        name = "test_tree_changes_are_registered_control_changes"
        baseline_files = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        unregistered_control = next(path for path in sorted(baseline_files) if path not in REGISTERED_CONTROL_CHANGES
                                    and tip.classify_path(self.policy, path) == "control")
        getattr(type(self)(name), name)()  # 对照：受管树未被篡改时 ② 通过。
        for case, changes, expected in (
            ("事实映射再次重绑", {EVIDENCE_FACT_MAP: "0" * 64}, f"前后摘要与过渡登记不符：{EVIDENCE_FACT_MAP}"),
            ("映射摘要常量文件再次变化", {EVIDENCE_TRACE: "0" * 64}, f"前后摘要与过渡登记不符：{EVIDENCE_TRACE}"),
            ("别的 evidence 层文件变化", {"candidate_rule_assertion.py": "0" * 64}, "变化未登记：candidate_rule_assertion.py"),
            ("新增 evidence 层文件", {"candidate_test_fact_map_0_999_0.json": "0" * 64},
             "变化未登记：candidate_test_fact_map_0_999_0.json"),
            ("新增 wire 层文件", {"drive_unregistered_probe.py": "0" * 64}, "未登记：drive_unregistered_probe.py（wire_producer 层）"),
            ("control 层文件未登记变化", {unregistered_control: "0" * 64}, f"未登记：{unregistered_control}（control 层）"),
        ):
            digests = {entry["path"]: entry["sha256"] for entry in self.entries}
            digests.update(changes)
            tampered = type(self)(name)
            tampered.entries = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
            with self.subTest(case=case), self.assertRaisesRegex(AssertionError, re.escape(expected)):
                getattr(tampered, name)()

    def test_modified_evidence_transition_original_is_rejected(self) -> None:
        """改 evidence 层过渡登记不能获得新的期望：多一个字节、改一个字符、路径不是普通文件都立即失败。"""

        raw = EVIDENCE_TRANSITION_PATH.read_bytes()
        with tempfile.TemporaryDirectory(prefix="codex-evidence-transition-", dir="/private/tmp") as temporary:
            path = Path(temporary) / EVIDENCE_TRANSITION_PATH.name
            for case, damaged in (("多一个字节", raw + b"\n"), ("改一个字符", raw.replace(b"89300984", b"89300985", 1))):
                self.assertNotEqual(damaged, raw)
                path.write_bytes(damaged)
                with self.subTest(case=case), self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                    _load_evidence_transition(path)
            with self.subTest(case="路径是目录"), self.assertRaisesRegex(ValueError, "普通文件"):
                _load_evidence_transition(Path(temporary))
            with self.subTest(case="路径不存在"), self.assertRaisesRegex(ValueError, "普通文件"):
                _load_evidence_transition(Path(temporary) / "不存在的登记.json")
        self.assertEqual(_load_evidence_transition(), self.evidence_transition)

    def test_migrated_symbols_stay_outside_identity_closures(self) -> None:
        """③ 迁入共享模块的函数与常量不得属于 wire、evidence 或评估读侧闭包。"""

        path = TOOL_ROOT / SHARED_CONTROL_PLANE
        defined: set[str] = set()
        if path.is_file():
            functions, constants, _imports = tip._module_symbols(ast.parse(path.read_text(encoding="utf-8")))
            defined = set(functions) | set(constants)
        closure: set[str] = set()
        for kinds in self.baseline["closure_symbols"].values():
            for names in kinds.values():
                closure.update(names)
        self.assertEqual(sorted(defined & closure), [], "迁入共享模块的符号落在 Codex 身份闭包内")


if __name__ == "__main__":
    unittest.main()
