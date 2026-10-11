"""Codex 身份守恒门禁（Claude 升级控制面设计 D7、B1-07）。

Claude 升级控制面改造的 wire 层、evidence 层、两个编排器闭包和 checker／builder 摘要必须与改造开工前
的基线逐字相等；control 层、策略文件与整树摘要的变化必须逐文件落在 ``REGISTERED_CONTROL_CHANGES`` 登记的修改
清单里，且不得出现按默认层归类的文件；迁入共享模块的函数与常量不得属于任何身份闭包。

读侧保留 B5a-08 两个钉住常量的原登记；其后已批准的 D4 单点静态证明、r2 后继顺序元数据和原设计恢复接口，另用
SHA 钉死的过渡原件登记逐符号及整文件前后值。B5a-09／10 测试单元读集接线再次改变两个钉住常量，只由独立后继
精确登记；先逆向回到不变的 10-08 原图，再逐步逆向回到 B5a-08 的原图并还原两个旧常量，
最终与原始基线图逐字段、逐摘要相同。D4 恢复的 23／35 项是旧证明依赖，不是可删除的兼容例外；未来未登记变化照常拒绝。

基线由 B1-01 开工前的 main（见基线的 ``source_commit``）算出，固化在
``fixtures/codex_identity_conservation_baseline.json``；本项目各批不得改动基线，文件摘要钉在 ``BASELINE_SHA256``。
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

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
                                     " read_audit.py 两个钉住值，并登记上一已部署生成器只读重放摘要；两个常量的新后值由独立精确后继证明",
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


def _restore_registered_reader(closure: dict[str, Any], field: str, transition: dict[str, Any],
                               successor: dict[str, Any] | None = None) -> dict[str, Any]:
    """当前真实图 → 两常量后继 → 恢复后继 → r2 → D4 → B5a-08 → 原始基线；逐步核实际摘要。"""

    if field not in {item for item, _roots in READER_FIELDS}:
        raise ValueError("未知读侧摘要字段")
    closure = _restore_pinned_reader_successor(closure, field, successor or _load_reader_successor(), transition)
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
        cls.readers = {
            field: tip.evaluator_reader_closure(cls.policy, TOOL_ROOT, list(roots), digests)
            for field, roots in READER_FIELDS
        }

    def test_baseline_is_frozen(self) -> None:
        self.assertEqual(self.baseline_sha256, BASELINE_SHA256, "Codex 身份守恒基线被改动：本项目各批不得改基线")
        self.assertEqual(self.baseline["schema_version"], "codex-identity-conservation-baseline/v1")

    def test_identity_sensitive_digests_match_baseline(self) -> None:
        """① 产出身份守恒；读侧摘要取已审核过渡的后值，并由独立测试逐项逆向证明。"""

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
            expected[field] = self.successor["after_readers"][field]["closure_sha256"]
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
        """② control、策略与整树摘要的变化逐文件对应登记清单；不得有按默认层归类的文件。"""

        before = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        after = {entry["path"]: entry["sha256"] for entry in self.entries}
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        for path in changed:
            layer = tip.classify_path(self.policy, path)
            if layer is None:
                continue
            self.assertIn(path, REGISTERED_CONTROL_CHANGES, f"Codex 受管文件变化未登记：{path}（{layer} 层）")
            self.assertEqual(layer, "control", f"Codex 受管文件变化不在 control 层：{path}（{layer} 层）")
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
                self.assertEqual(self.readers[field], self.successor["after_readers"][field])
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
            "codex_upgrade_gate_receipt.py": hashlib.sha256((TOOL_ROOT / "codex_upgrade_gate_receipt.py").read_bytes()).hexdigest()})
        self.assertEqual(tuple(step["phase"] for step in successor["steps"]), (READER_SUCCESSOR_PHASE,))
        for field, _roots in READER_FIELDS:
            with self.subTest(field=field):
                before, after = successor["before_readers"][field], successor["after_readers"][field]
                self.assertEqual(after, self.readers[field])
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
                    _restore_pinned_reader_successor(self.readers[field], field, damaged, self.transition)
        with tempfile.TemporaryDirectory(prefix="codex-pin-successor-", dir="/private/tmp") as temporary:
            path = Path(temporary) / READER_SUCCESSOR_PATH.name
            path.write_bytes(READER_SUCCESSOR_PATH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "摘要与钉住值不符"):
                _load_reader_successor(path)

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
