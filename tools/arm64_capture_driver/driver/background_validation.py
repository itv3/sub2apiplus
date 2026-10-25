#!/usr/bin/env python3
"""修复轮的后台验证（E4-01，指南「修好接着跑」）：修复提交部署之后，由同一个统一调度执行器在后台跑全量门禁，结论按
修复提交与当时的部署收据落盘；批次驱动在每个批次边界读这份结论。

为什么：修复轮原先以全量门禁通过为部署前提（串行约 95 分钟），而工具本来只认修复提交、定向回归与部署收据。现在改为
定向回归（入口门禁 ``regression`` 组合）通过即部署接着跑，全量门禁挪到后台：入口门禁 ``full-gates`` 组合、重新执行
全集模式（B-09 来源必须是同一次真实完整执行）、``nice`` 降优先级、``--require-deployed``（门禁前核对数据根
部署的就是这个提交）。采集批次开始前向调度器申请整机（``unit_executor.py acquire``）：调度器停派、等在跑单元结束才
批准，批次结束归还后接着跑，不和采集抢核。

结论文件 ``<RUNROOT>/background-validation/<提交前 12 位>-<部署收据摘要前 12 位>.json``（``background-validation/v1``）：
提交、分支、bundle、绑定的部署收据（文件名、sha256、工具身份）、组合、状态、后台进程号、起止时间、入口门禁输出目录与
日志、没通过的门禁项、原因。状态：

* ``running``：在跑（后台进程活着才算；进程已经不在而没写结论的，读出来是 ``vanished``）；
* ``passed``／``failed``：入口门禁的结论（门禁项全部通过／有门禁项没通过）；
* ``aborted``：没有门禁结论（被信号终止、准备失败、部署与提交不一致、部署在验证途中换了）；
* ``superseded``：被更新的修复轮停下（上一轮的提交已作废）。注意：被停下的运行没有发布运行清单，它执行过的单元记录
  不能被承接（E3-01 承接要追到原运行清单），下一次验证会重新执行它们。

同一提交＋同一部署只有一份当前状态；重开前保存旧终态。A-04 起工作树与全部 Go 缓存位于
本轮 background-validation/work 的独立目录，输入合同变化不能继续复用旧结论，启动以文件锁去重。显式 ``--work`` 也可放在
数据根外的 ``entry-gates-source-work/<完整提交>-<部署绑定摘要>/``，与后台状态和输出分开；来源绑定前须一次准备好目录与缓存。

B-09 可提供当前上下文与结束时校时凭证，覆盖完整才签发全集收据；到期自动重跑，撤销后可用 ``--rerun-full-set``。

已有后台失败后，显式 ``start --unit-inherit-request <当前请求>`` 可在同一 WORK 续作：保留旧终态，只通过现有逐单元合同
补跑不能承接的项。请求必须绑定本客户端的 full-gates／pre-ledger、当前提交、部署集合及记录库；与 B-09 签发参数互斥。
新后台通过前实际消费已发布的决策收据；批次边界按该次消费凭证重放，不能只信 passed 标签。默认路径仍重新执行全集。

跨提交、跨部署的后继（旧后台失败 → 修复 → 新提交新部署续作）：``start --unit-inherit-request <新请求> --unit-inherit-source-result
<旧后台结论>`` 把一次已经结束的旧后台运行当作「来源」，在**新运行**（新结论文件、新运行目录、新运行 ID）里按新提交、新部署
与新批准承接旧运行留下的合法记录；旧运行只读，不在它的目录上续作。允许变化的只有提交、部署、bundle、分支与入口门禁摘要；
必须相同的是客户端、组合（仅 full-gates）、物理 WORK（与入口线的原位后继同口径，测试树和缓存沿用）、记录库与调度并发参数，
且必须显式提供审计策略与承接请求。来源结论须是 passed 或 failed、进程组已退出，留有入口门禁摘要与已发布进记录库的运行清单
（执行器中途终止的运行不发布清单，其成果不能承接），旧提交还须能验证是新提交的祖先（在临时对象库里用 WORK 测试树的完整历史
加新 bundle 验证，不动测试树；真正的非祖先永远拒绝，只有「无法验证」才可用 ``--allow-unverified-ancestry`` 退化，并在结论里
标明 unverified）。这一接线**不削弱任何承接条件**：单元记录能否承接（声明输入摘要、读集审计、记录有效期、环境指纹、两类批准有效
且未过期、范围匹配、来源运行未撤销）仍全部由原逐单元合同（``unit_inherit.py`` 与 ``unit_records.py``）在三个复核点逐条判定，
来源或批准失效、记录被篡改或过期、审计未通过的单元不承接、只重跑自己，原因写进清单与本结论。新结论另登记（只在使用来源
选项的运行里出现，其余结论的形状不变）：``unit_inherit_source``（来源结论、其执行器运行 ID、提交、祖先验证方式与冻结副本）、
``inherited_units``（每个承接项的单元、来源运行 ID、来源记录摘要、来源提交、是否来自来源后台那一轮实际执行的新记录）、
``inherit_counts``（四个数分开统计，是一个漏斗：登记数 registered＝本次计划单元里登记为可承接的单元数（清单项 inheritable
为真，即已登记读集审计且没有别的阻断原因）；审计通过数 audit_passed＝其中承接请求的来源集合里有来源记录、且这条记录自身读集
审计通过并自摘要有效的；消费者接受数 consumer_accepted＝承接消费者 ``verify_published`` 实际重验接受的承接项数；实际承接数
inherited＝运行清单里处置为承接的单元数；通过的结论要求 inherited＝consumer_accepted≤audit_passed≤registered）、``rerun_units``
（每个重跑单元的原因类别、原文、前序状态、变化的输入名与耗时）与 ``inherit_summary``（含请求来源单元数 request_source_units）。
没有任何单元被承接（整套重新执行）时 ``outcome`` 明确写 ``full_rerun``，不会假装承接。``check-boundary``／``check`` 的语义与
退出码不变：当前部署没有后台记录时仍只提示先起后台；通过的跨提交后继另重放上述登记与清单、决策、消费凭证一致。

两客户端都已纳管时（B5a-10；数据根 ``control`` 里两客户端都有部署收据）：仓库全量门禁两客户端只跑一次（测试集合已含 Claude
测试），结论按「提交＋部署收据集合」绑定——集合是每个已纳管客户端最新的那一份（交换共享根的一次部署即该事务签出的每一份，
B5a-01 的双收据），写进结论的 ``deployments``，文件名的部署摘要换成集合摘要；入口门禁带 ``--require-deployed-all``，本轮
客户端之外每个已纳管客户端的部署也核对是本提交，门禁结束时集合必须没变，否则判中止。两客户端的轮次各有自己的 RUNROOT，
所以另在数据根 ``control/background-validation/`` 登记「提交＋集合 → 结论文件」：另一客户端（另一 RUNROOT）的 start 查到
同一提交与集合已有在跑或已有结论就不再起，只把自己的入口空跑登记上（``.peer-<客户端>.json``），门禁通过后两客户端的入口
空跑都接着起（已经通过的当场接上）；起新的一次时，登记里其它提交或集合的在跑验证一并停下（部署已经换了，它们不再有用）。
搭上的轮次在自己的运行根 ``background-validation/attached/`` 留一个指向那份结论的指针（``background-validation-attached/v1``），
``stop`` 与 ``status`` 也覆盖它：哪个客户端的修复轮跑定向回归前 ``stop``，都停得下本轮正在用的那一次验证。
``check-boundary``／``require-passed`` 按 ``--client`` 取该客户端当前部署收据所绑定的结论：先找本 RUNROOT，再找数据根的登记。
只有一个客户端纳管时结论、文件名与命令行与改造前逐字相同（不写 ``deployments``，不登记）。

失败单元补跑（Codex 升级工具优化第 2 项）：执行器在调度配置打开补跑时，对“执行失败”类的失败单元在机器腾空后单独补跑一次，通过则算入门禁结论并标注
“补跑通过”。结论文件因此多一个键 ``retry_passed_units``（只在有补跑通过的单元时出现：单元、首次失败日志、补跑日志、累计窗口），``check-boundary``／
``require-passed``（VC-5 统一准入 ``vc5_admission.state`` 也经 ``check`` 取同一份判定）的提示单列这些单元，并在判定时按执行器汇总重放标注（含两次
日志的摘要）——标注被改写、被藏起来、日志缺失，结论就不是通过，不静默放行。补跑关闭或没有补跑时结论与改造前逐字相同。

起跑前置检查（Codex 升级工具优化第 4 项，``background_preflight.py``）：后台验证一轮约 136 分钟，一次 Codex 升级里白跑过两次都是起跑顺序错（先于冻结承接收据、先于重部署）。
``start --preflight on`` 在起跑前逐项核对冻结收据、基线重封、重部署、准入绑定，任一项不过就拒绝起跑（不启动后台进程、不写结论、不停掉已在跑的验证），
报告留档在 ``<运行根>/background-validation/preflight/``；``preflight`` 子命令只查不起（退出码 0 通过、3 不通过）。``--preflight`` 缺省 on（验收修正建议 12：Python 命令行与
驱动入口 ``background-validate.sh start`` 同一缺省，直接调 Python 也不会悄悄绕过前置检查），``--preflight off``（驱动入口 ``--no-preflight``）显式跳过。

同树重部署沿用测试结论（Codex 升级工具优化第 5 项）：同一棵受管树重新部署（部署收据只换时间戳）也要重跑约 145 分钟验证——测试结论与部署时间戳无关，只与“测什么、怎么测、
在什么环境测”有关。通过的结论因此带一份沿用绑定 ``reuse_binding``：工具身份五摘要（每个已纳管客户端）、提交、门禁集合摘要、执行器摘要、调度策略摘要、输入清单摘要、
平台环境摘要（后四项和门禁集合来自执行器写的 ``executor/binding.json``），并带有效期（缺省 24 小时，自来源完成时起算，沿用不延长）。``reuse`` 子命令（或 ``start --reuse auto``，
驱动入口缺省带）在新部署之后用 ``entry-gates.sh --bind-only`` 在来源的同一个工作目录里现算同样的绑定——它走和真实运行一样的树准备、部署绑定核对（整树摘要与部署一致性各部分）、
清单与输入计算，但不执行任何测试——逐项比对，全部相同才把来源的通过结论沿用到新部署上，写一份绑定新部署收据的结论（``reuse`` 键登记来源结论、冻结副本、逐项比对结果）；
任一项变了或过了有效期就拒绝并逐项写明原因，要求重验。``check``（批次边界、VC-5 前核对与统一准入）对沿用的结论重放：来源冻结副本的摘要与自摘要、来源仍是通过、绑定未过期、
新部署的工具身份与来源一致，不能只信 passed 标签。沿用成功之后仍接部署后空跑（验收修正必修 6）：沿用只省掉测试，不省掉新部署上的入口空跑——``--then-dryrun``
与非沿用路径同一个语义（``start`` 由驱动入口缺省带、``--no-dryrun`` 才关；``reuse`` 子命令同样缺省带），沿用结论写好后立刻按原逻辑接着起入口空跑（两客户端都纳管时
另一客户端的空跑一并接上），空跑状态记进结论的 ``dryrun``、``peer_dryruns``，``start``／``reuse`` 的输出里也带出。

核数偏差报告（Codex 升级工具优化第 6 项）：执行器每轮结束写 ``core-deviation.json``（声明核数与实测核数偏差超过 30% 的单元，整轮实测／声明总比值）；后台验证结束时
（不论通过与否）把它复制进结论目录——``<提交前 12 位>-<部署摘要前 12 位>.core-deviation.json``，与结论文件并排——并在结论里登记 ``core_deviation``
（路径、摘要、偏差单元数、总比值）。报告只供调优额度参考，不影响任何结论。

子命令：

* ``start --runroot --data-root --bundle --branch --commit [--profile] [--record-store] [--work] [--vc-env] [--then-dryrun]
  [--audit-policy <策略>] [--unit-inherit-request <请求> [--unit-inherit-source-result <旧后台结论> [--allow-unverified-ancestry]]]``：
  绑定数据根最新部署收据；同一提交＋同一部署已有在跑或已有 passed／failed 结论就不重复起，打印现有结论；在跑的其它后台
  验证先停下、标 superseded；然后在新会话里起 ``run``。``--then-dryrun``（E4-02 日常化）：通过之后接着起入口空跑
  （``entry_dryrun.py start --to pre-a3``，同一份记录库；门禁结论仍须满足 B-09 完整合同，缺失时重新执行；
  有采集在跑就让路），结果记在本结论的 ``dryrun`` 字段。
  本次带 ``--audit-policy`` 时记为 skipped：策略来源需要当前逐单元批准，操作员应显式调用另一个脚本——入口空跑
  ``entry-dryrun.sh start ... --unit-inherit-request <请求> --audit-policy <策略>``，不自动补批准或重新执行全集。
  跨提交后继另加 ``--unit-inherit-source-result <旧后台结论>``（只与 ``--unit-inherit-request`` 同用，``--work`` 须是旧结论的
  WORK）与可选的 ``--allow-unverified-ancestry``（见上文）。**这两个选项只有本脚本的 ``start`` 支持**，经
  ``background-validate.sh start <bundle> <分支> <提交> …`` 原样透传（该脚本把三个位置参数之后的选项交给本脚本）；入口空跑
  ``entry_dryrun.py``／``entry-dryrun.sh`` 没有这两个选项，不要加到空跑命令上（上一段的 ``entry-dryrun.sh start`` 命令行里
  只有 ``--unit-inherit-request`` 与 ``--audit-policy`` 是它自己的选项）。
  声明级承接的采样方式（步骤 5 WP3b）：``start --declared-sample off|always|every:N``（缺省 off）原样交给后台起的入口门禁（``entry-gates.sh``），再到执行器，
  决定声明级命令单元的读集证据来自登记表还是每次执行自己采样（正式验证轮显式带 ``always``）；非法值在这里就拒绝。非 off 的取值进输入合同与结论
  （同 ``--parallel``：同一提交同一部署已有不同取值的结论时须先显式停止并重新验证；逐单元续作须保持原取值），缺省时输入合同、结论与命令行与改造前
  逐字相同；``--then-dryrun`` 接着的入口空跑不带它（要采样的空跑另用 ``entry-dryrun.sh start … --declared-sample …``，或本命令不接空跑）。
* ``run``（内部）：跑入口门禁，按结论写状态。收到 SIGTERM 时把它转给入口门禁（执行器会终止全部单元会话）。
* ``stop --runroot [--reason]``：停下在跑的后台验证并标 superseded。修复轮跑定向回归之前用：同一台机器同一时间只能有
  一个调度器，上一轮的验证也已作废。两客户端都纳管时连本运行根搭上的（别的运行根发起的）那一次一起停。
* ``check-boundary --runroot --data-root [--client]``：批次边界——取该客户端（缺省 codex）最新部署收据绑定的结论：failed／
  aborted 退出 3（拒绝派发下一批，打印没通过的门禁与日志位置）；passed、running 退出 0；没有结论（没起、被停下或进程已经
  不在）退出 0 并提示先 start。
* ``require-passed --runroot --data-root [--client]``：VC-5 验收前——该客户端最新部署绑定的结论是 passed 才退出 0，否则退出 3。
* ``status --runroot``：打印全部结论（JSON）。

退出码：0 通过或放行；2 用法或环境错误；3 拒绝（有失败结论、要求通过而没有通过结论）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "background-validation/v1"
DIRECTORY = "background-validation"
DEFAULT_PROFILE = "full-gates"
# 按客户端筛选（Codex 升级工具优化第 9 项）：full 是全集；client-plan 是某个客户端计划的子集（归属表选择器按基点..提交的改动算出）。
GATE_SCOPE_FULL = "full"
GATE_SCOPE_CLIENT_PLAN = "client-plan"
HERE = Path(__file__).resolve().parent


def _client_table() -> Any:
    """同目录的客户端表 client_table.py（B5a-06）：按路径加载，与驱动其它模块共用一个模块对象（client_table_sibling）。"""

    name = "client_table_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "client_table.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"找不到同目录的 client_table.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


_CT = _client_table()
# 与驱动 install.py 的 latest_deploy_receipt 同一规则（按文件名里的时间戳取最新）。
DEPLOY_RECEIPT_GLOB = _CT.spec("codex")["deploy_receipt_glob"]
# B5a-01：按客户端（--client，缺省 codex）选部署收据，规则与驱动 install.py 相同；Claude 收据的工具身份在 identity
# 对象里。Codex 的结论文件逐字不变（不写 client 字段），Claude 的结论写 client，run 与边界判定据此取同一客户端的收据。
# B5a-06 起取值来自客户端表，这几个名字保留作只读视图。
DEPLOY_RECEIPT_GLOBS = {client: _CT.spec(client)["deploy_receipt_glob"] for client in _CT.CLIENTS}
DEPLOY_RECEIPT_STAMP_SEPARATORS = {client: _CT.spec(client)["deploy_receipt_stamp_separator"] for client in _CT.CLIENTS}
DEPLOY_RECEIPT_IDENTITY_KEYS = {client: _CT.spec(client)["deploy_receipt_identity_key"] for client in _CT.CLIENTS}
DEPLOY_CLIENTS = tuple(_CT.CLIENTS)
IDENTITY_FIELDS = ("policy_version", "policy_sha256", "tool_files_sha256", "wire_producer_sha256", "evidence_semantics_sha256",
                   "control_sha256")
NICENESS = 10
# 停下后台验证时给入口门禁与执行器的清理时间（执行器收到 SIGTERM 终止全部单元会话），超时再 SIGKILL 整组。
STOP_GRACE_SECONDS = 60.0
# 同一后台工作目录的既有进程尚在收尾时，入口锁最多等这么久。
WAIT_LOCK_SECONDS = 7200
# 两客户端都纳管时（B5a-10）：数据根 control 下的跨轮次登记目录与登记文件合同。
REGISTRY_DIRECTORY = "background-validation"
REGISTRY_SCHEMA = "background-validation-registry/v1"
PEER_SCHEMA = "background-validation-peer/v1"
# 另一客户端搭同一次验证时只比这几项输入合同：参数文件、运行目录与是否接空跑本来就随客户端与轮次不同。
CLIENT_SPECIFIC_INPUTS = frozenset({"vc_env_sha256", "work", "then_dryrun"})
# 搭上的轮次在自己运行根留的指针（子目录，不混进本运行根的结论列表）。
ATTACHED_DIRECTORY = "attached"
ATTACHED_SCHEMA = "background-validation-attached/v1"
# 跨提交、跨部署的后继（旧后台结论作来源，新运行承接它留下的合法记录）：来源登记与承接登记的合同版本。
INHERIT_SOURCE_SCHEMA = "background-inherit-source/v1"
INHERIT_REPORT_SCHEMA = "background-inherit-report/v1"
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
# 验证「旧提交是新提交的祖先」时每个 git 子进程的时限（秒）：只在临时对象库里取一次 bundle、查一次 merge-base，正常几秒内完成。
GIT_TIMEOUT_SECONDS = 180
# 重跑原因类别：主类别取下面顺序里第一个命中的，命中的全部类别都登记在 reason_categories。需求点名的四类原因是失败（failed）、
# 输入变化（inputs_changed）、未审计需实时重跑（unaudited_realtime）、无来源记录（no_source）；其余几类（来源记录不可用、读集审计
# 未通过、复核点拒绝、其它）是来源或批准失效时「不承接并给出明确原因」所需。
RERUN_CATEGORIES = {
    "failed": "前序后台轮次里该单元失败",
    "source_invalid": "来源记录不可用（自摘要不符被改过、超过承接期限、清单或日志缺失、来源运行已撤销）",
    "unaudited_realtime": "未登记读集审计或属实时检查，只验证本次执行，必须实时重跑",
    "audit_not_passed": "来源记录的读集审计未通过或覆盖未闭合",
    "inputs_changed": "输入、单元规格、环境、执行器或调度策略相对前序轮次已变化",
    "no_source": "没有来源记录（来源集合未登记，记录库里也没有可承接的）",
    "session_refused": "逐单元承接的复核点拒绝了本次承接，整次重新执行",
    "other": "其它原因（见 reasons 原文）",
}
# 承接登记里计数与耗时字段的口径，原样写进 inherit_summary.definitions：结论文件自带说明，读的人不必回头翻源码。
INHERIT_DEFINITIONS = {
    "registered": "登记数：本次计划单元里登记为可承接的单元数（运行清单项 inheritable 为真：已登记读集审计，且没有别的阻断原因）",
    "audit_passed": "审计通过数：其中承接请求的来源集合里有来源记录、且这条记录自身读集审计证据成立（自摘要有效、正式执行、审计通过、覆盖闭合）的单元数",
    "consumer_accepted": "消费者接受数：承接消费者 verify_published 实际重验接受的承接项数（失败或中止的结论没有消费，写 null）",
    "inherited": "实际承接数：运行清单里处置为承接的单元数",
    "executed_audit_passed": "本次重跑的单元里，新执行记录的读集审计证据成立的单元数",
    "audit_ready_after_run": "本次运行之后有读集审计通过的记录可供下一轮承接的单元数＝实际承接数＋重跑且审计通过的单元数",
    "rerun_seconds": "各重跑单元自身耗时之和（单元并行执行，不是墙钟时间）",
}


class ValidationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA else None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: Any) -> bool:
    """进程组里还有进程（含组长已退出、组员还在清理的情形）。"""

    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def latest_deployment(data_root: Path, client: str = "codex") -> dict[str, Any]:
    """数据根该客户端最新的受管工具部署收据：文件名、sha256、工具身份。"""

    if client not in DEPLOY_RECEIPT_GLOBS:
        raise ValidationError(f"不认识的客户端：{client!r}（可选：{list(DEPLOY_CLIENTS)}）")
    control = Path(data_root) / "control"
    separator = DEPLOY_RECEIPT_STAMP_SEPARATORS[client]
    candidates = sorted((path for path in control.glob(DEPLOY_RECEIPT_GLOBS[client]) if path.is_file() and not path.is_symlink()),
                        key=lambda path: path.name.split(separator, 1)[1].lower())
    if not candidates:
        raise ValidationError(f"数据根没有受管工具部署收据：{control}")
    path = candidates[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValidationError(f"部署收据不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, dict) or payload.get("status") != "passed":
        raise ValidationError(f"最新部署收据不是 passed：{path}")
    key = DEPLOY_RECEIPT_IDENTITY_KEYS[client]
    identity = payload if key is None else payload.get(key)
    if not isinstance(identity, dict):
        raise ValidationError(f"部署收据缺少工具身份：{path}")
    return {"receipt": path.name, "sha256": _sha256_file(path), "identity": {field: identity.get(field) for field in IDENTITY_FIELDS}}


def managed_clients(data_root: Path) -> list[str]:
    """已纳管的客户端：数据根 control 里已有它的部署收据（与驱动 install.py、受管部署器同一判据），按客户端表顺序。"""

    control = Path(data_root) / "control"
    return [client for client in DEPLOY_CLIENTS
            if any(path.is_file() and not path.is_symlink() for path in control.glob(DEPLOY_RECEIPT_GLOBS[client]))]


def deployment_set(data_root: Path) -> dict[str, dict[str, Any]] | None:
    """两个及以上客户端已纳管时，每个已纳管客户端最新的部署收据（B5a-10，见模块说明）；只有一个客户端纳管时返回 None。"""

    clients = managed_clients(data_root)
    if len(clients) < 2:
        return None
    return {client: latest_deployment(data_root, client) for client in clients}


def set_sha256(deployments: Mapping[str, Mapping[str, Any]]) -> str:
    """部署收据集合的摘要：客户端 → 收据摘要，按客户端排序后的规范 JSON。"""

    canonical = json.dumps({client: str(item["sha256"]) for client, item in sorted(deployments.items())}, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def binding_key(deployment: Mapping[str, Any], deployments: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """结论文件名与后台工作目录用的部署摘要：只有一个客户端纳管时是那份部署收据的摘要（与改造前相同），否则是集合摘要。"""

    return set_sha256(deployments) if deployments else str(deployment["sha256"])


def result_path(runroot: Path, commit: str, deployment: Mapping[str, Any],
                deployments: Mapping[str, Mapping[str, Any]] | None = None) -> Path:
    return Path(runroot) / DIRECTORY / f"{commit[:12]}-{binding_key(deployment, deployments)[:12]}.json"


def registry_dir(data_root: Path) -> Path:
    return Path(data_root) / "control" / REGISTRY_DIRECTORY


def registry_path(data_root: Path, commit: str, deployments: Mapping[str, Mapping[str, Any]]) -> Path:
    return registry_dir(data_root) / f"{commit[:12]}-{set_sha256(deployments)[:12]}.json"


def _read_kind(path: Path, schema: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == schema else None


def registrations(data_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """数据根登记的全部「提交＋集合 → 结论文件」。"""

    found = []
    directory = registry_dir(data_root)
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.glob("*.json")):
            if ".peer-" in path.name:
                continue
            payload = _read_kind(path, REGISTRY_SCHEMA)
            if payload is not None:
                found.append((path, payload))
    return found


def peers(registration: Path) -> list[dict[str, Any]]:
    """搭这次验证的其它客户端的入口空跑登记（``<登记名>.peer-<客户端>.json``）。"""

    found = []
    for path in sorted(Path(registration).parent.glob(f"{Path(registration).stem}.peer-*.json")):
        payload = _read_kind(path, PEER_SCHEMA)
        if payload is not None:
            found.append(payload)
    return found


def attached(runroot: Path) -> list[tuple[Path, dict[str, Any]]]:
    """本运行根搭上的、别的运行根发起的后台验证结论（指针在 ``attached/`` 下；只有两客户端都纳管时才会有）。"""

    found = []
    directory = Path(runroot) / DIRECTORY / ATTACHED_DIRECTORY
    if directory.is_dir() and not directory.is_symlink():
        for pointer in sorted(directory.glob("*.json")):
            entry = _read_kind(pointer, ATTACHED_SCHEMA)
            target = Path(str((entry or {}).get("result") or ""))
            payload = _read(target) if entry is not None and target.is_absolute() else None
            if payload is not None:
                found.append((target, payload))
    return found


def results(runroot: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted((Path(runroot) / DIRECTORY).glob("*.json")):
        payload = _read(path)
        if payload is not None:
            found.append((path, payload))
    return found


def effective_status(payload: Mapping[str, Any]) -> str:
    """running 只有后台进程还活着才算；进程已经不在而没写结论的是 ``vanished``。"""

    status = str(payload.get("status"))
    if status == "running" and not _pid_alive(payload.get("pid")):
        return "vanished"
    return status


def _binds(payload: Mapping[str, Any], deployment: Mapping[str, Any], client: str | None) -> bool:
    """结论是否绑定了这份部署收据：本身发起时绑定的那份，或两客户端都纳管时集合里该客户端的那份（B5a-10）。"""

    if (payload.get("deployment") or {}).get("sha256") == deployment["sha256"]:
        return True
    bound = (payload.get("deployments") or {}).get(client) if client else None
    return isinstance(bound, Mapping) and bound.get("sha256") == deployment["sha256"]


def for_deployment(runroot: Path, deployment: Mapping[str, Any], *, client: str | None = None,
                   data_root: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    """绑定这次部署的结论（同一部署验证过多个提交时取最后开始的一份）。给了 ``client`` 时另认集合里该客户端的那份；本 RUNROOT
    找不到而给了 ``data_root`` 时再按数据根的登记找（另一客户端的轮次发起的同一次验证，B5a-10）。"""

    bound = [(path, payload) for path, payload in results(runroot) if _binds(payload, deployment, client)]
    if not bound and data_root is not None and client is not None:
        for _registration, entry in registrations(data_root):
            target = Path(str(entry.get("result") or ""))
            payload = _read(target) if target.is_absolute() else None
            if payload is not None and _binds(payload, deployment, client):
                bound.append((target, payload))
    return max(bound, key=lambda item: (str(item[1].get("started_at_utc")), item[0].name)) if bound else None


# ---------------------------------------------------------------- 启动、停止与后台运行

# ---------------------------------------------------------------- 进程树快照（WP-R5）
#
# 为什么：``_terminate`` 原来只管进程组。执行器给每个单元起的是新会话（不在组里），单元里的测试还可能再起嵌套的
# 另起会话的进程（``test_claude_real_delivery_integration`` 的嵌套 ``unit_executor.py run-unit``）：执行器没来得及
# abort 它们就被 SIGKILL（或根本没响应 SIGTERM）时，这些进程成了 PPID=1 的孤儿继续持锁运行（10-14、10-15 实测）。
# 做法：**在发任何信号之前**按 ppid 取 run 进程的整棵后代树（含进程组成员），宽限期内每轮并入新出现的后代，最终对快照里
# 仍存活的全部进程（不只进程组）先冻结再 SIGKILL。快照记 pid＋启动时间，发信号前核对一致，防 pid 复用误杀。
#
# 这里自带一份进程表，不去加载 ``unit_executor.py``：stop 路径不依赖 3800 行的执行器模块能否导入。两份实现的一致性由
# 测试核对（``test_ci_unit_executor_descendant_cleanup``）。
#
# 失败面（R7b 加固）：新逻辑引入的任何一步（取快照、并入新后代、冻结后的身份复核）出错，都不得妨碍后面的信号——
# stop 已经先把结论标成 superseded，信号发不出去就等于什么都没停。所以：快照出错回落到「killpg SIGTERM→宽限→killpg SIGKILL」
# 的旧做法，原因写进 stop 的输出；冻结后的复核瞬时失败要重试，仍失败就解冻（SIGCONT）并告警，绝不留下停住的进程；
# run 进程的 pid 动手前先核对启动标识（``pid_start``），对不上就是 pid 被无关进程复用，不动。

FREEZE_ROUNDS = 8
# 冻结之后、SIGKILL 之前要再核对一次「pid＋启动时间」。读进程信息偶发失败（fd 耗尽、内存紧张、读到乱码、ps 超时）不等于「进程没了」：
# 在这个次数内重试；仍核对不了就对它发 SIGCONT 解冻并告警，不把进程留在 STOPPED 里。
KILL_VERIFY_ATTEMPTS = 5
KILL_VERIFY_RETRY_SECONDS = 0.1
# 进程文件系统根（测试用假的 /proc 树覆盖 Linux 分支；生产永远是 /proc）。
PROC_ROOT = Path("/proc")


def _note(notes: list[str] | None, text: str) -> None:
    """把一条说明／告警记进 ``notes``（同一句只记一次），同时写到标准错误：后台自己起的 stop（``start`` 取代上一轮）也有日志可查。"""

    if notes is not None:
        if text in notes:
            return
        notes.append(text)
    print(f"background_validation: {text}", file=sys.stderr, flush=True)


def _ps_table_strict() -> dict[int, tuple[int, int, str, str]]:
    """没有 /proc 时（macOS）的后备。``ps`` 起不来、超时或没有输出就报错，不把「读不到」伪装成「一个进程都没有」。"""

    completed = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,stat=,lstart="], capture_output=True, text=True,
                               env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}, stdin=subprocess.DEVNULL, timeout=30)
    if completed.returncode != 0 and not completed.stdout.strip():
        raise OSError(f"ps 失败（退出码 {completed.returncode}）：{completed.stderr.strip()[:200]}")
    table: dict[int, tuple[int, int, str, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5:
            try:
                table[int(parts[0])] = (int(parts[1]), int(parts[2]), " ".join(parts[4].split()), parts[3])
            except ValueError:
                continue
    return table


def _process_table() -> dict[int, tuple[int, int, str, str]]:
    """全部进程：pid → (ppid, pgid, 启动时间标识, 状态)。Linux 读 /proc；没有 /proc 时（macOS）用 ``ps``。``ps`` 读不到时返回空表；
    ``/proc`` 目录本身读不了（fd 耗尽等）会抛 OSError，由调用方（``_terminate`` 的各处保护）接住。"""

    table: dict[int, tuple[int, int, str, str]] = {}
    if (PROC_ROOT / "self" / "stat").is_file():
        for entry in PROC_ROOT.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
                table[int(entry.name)] = (int(fields[1]), int(fields[2]), fields[19], fields[0])
            except (OSError, ValueError, IndexError):
                continue
        return table
    try:
        return _ps_table_strict()
    except (OSError, subprocess.SubprocessError):
        return table


def _probe(pid: int) -> tuple[int, int, str, str] | None:
    """单个进程现在的 (ppid, pgid, 启动时间标识, 状态)。进程已经不在返回 ``None``；读不了（fd 耗尽、内存紧张、读到乱码或截断的
    stat、``ps`` 失败／超时）抛 OSError／ValueError／SubprocessError——调用方据此区分「确实没了」和「暂时核对不了」，
    故障注入测试也从这里下手。"""

    if (PROC_ROOT / "self" / "stat").is_file():
        try:
            text = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return None   # 进程已退出（ENOENT／ESRCH），不是读故障
        fields = text.rsplit(")", 1)[-1].split()
        if len(fields) <= 19:
            raise ValueError(f"/proc/{pid}/stat 字段不足：{text[:80]!r}")
        return int(fields[1]), int(fields[2]), fields[19], fields[0]
    return _ps_table_strict().get(pid)


def process_identity(pid: int) -> str | None:
    """这个 pid 现在对应进程的启动标识（Linux：/proc stat 的 starttime 刻度；macOS：ps 的 lstart 文本），读不到返回 ``None``。
    后台 run 启动时记进结论的 ``pid_start``；``stop`` 动手前据此核对 pid 没有被无关进程复用。"""

    try:
        row = _probe(pid)
    except Exception:   # noqa: BLE001 - 尽力而为：读不到就不记，stop 会退回按进程组处理（见 ``_run_mode``），不能因此拦住 start
        return None
    return None if row is None else row[2]


def _identity(pid: int, start: str) -> str:
    """pid 现在和快照里那个进程的关系：``same``（启动时间一致、没成僵尸）、``gone``（已不在或已成僵尸）、``other``（pid 已被
    另一个进程复用）、``unknown``（读不了，暂时核对不了）。"""

    try:
        row = _probe(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"
    if row is None or row[3][:1] in ("Z", "X"):
        return "gone"
    return "same" if row[2] == start else "other"


def _tree_snapshot(root: int | None, previous: Mapping[int, str] | None = None,
                   table: Mapping[int, tuple[int, int, str, str]] | None = None) -> dict[int, str]:
    """``root`` 进程的整棵后代树加它的进程组成员（另起会话的后代也在内，按 ppid 找），再并上 ``previous`` 里仍是同一个进程
    （pid＋启动时间一致）的那些及其后代；结果 pid → 启动时间标识。绝不含调用方自己和它的祖先，也不含已成僵尸的。
    ``table`` 给了就用它（调用方已经读过进程表），否则现读。"""

    table = _process_table() if table is None else table
    protected = {os.getpid()}
    pid = os.getpid()
    while pid in table and table[pid][0] > 0 and table[pid][0] not in protected:
        pid = table[pid][0]
        protected.add(pid)
    seeds = {pid for pid, row in table.items() if root is not None and (pid == root or row[1] == root)}
    seeds |= {pid for pid, start in (previous or {}).items() if pid in table and table[pid][2] == start}
    children: dict[int, list[int]] = {}
    for pid, row in table.items():
        children.setdefault(row[0], []).append(pid)
    seen: set[int] = set()
    stack = list(seeds)
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack.extend(children.get(pid, ()))
    return {pid: table[pid][2] for pid in seen if pid not in protected and table[pid][3][:1] not in ("Z", "X")}


def _refresh_snapshot(root: int | None, previous: Mapping[int, str], notes: list[str] | None) -> dict[int, str]:
    """重新取一次快照并并入 ``previous``；**取不到就沿用 ``previous``**（不丢掉已经快照到的进程），原因记进 ``notes``。
    进程表为空也算取不到：真实系统里进程表至少有调用方自己，空表只可能是 ``ps`` 失败。"""

    try:
        table = _process_table()
    except Exception as error:   # noqa: BLE001 - 见函数说明：任何读进程表的故障都不得妨碍后面的信号
        _note(notes, f"读进程表失败（{error!r}）：沿用上一次的快照，仍会对进程组发 SIGTERM／SIGKILL")
        return dict(previous)
    if not table:
        _note(notes, "进程表读不到（为空）：沿用上一次的快照，仍会对进程组发 SIGTERM／SIGKILL")
        return dict(previous)
    try:
        return _tree_snapshot(root, previous, table)
    except Exception as error:   # noqa: BLE001
        _note(notes, f"解析进程树失败（{error!r}）：沿用上一次的快照，仍会对进程组发 SIGTERM／SIGKILL")
        return dict(previous)


def _signal_if_same(pid: int, start: str, sig: int) -> bool:
    """pid 现在还是快照里那个进程（启动时间一致）才发信号；pid 已被复用、进程已走或暂时核对不了就什么也不做。"""

    if _identity(pid, start) != "same":
        return False
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _unfreeze(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGCONT)


def _kill_frozen(frozen: Mapping[int, str], notes: list[str] | None) -> list[int]:
    """杀掉已经 SIGSTOP 冻结的进程，保证**不会有进程留在 STOPPED 里**。返回实际发过 SIGKILL 的 pid。

    杀之前再核对一次身份。读进程信息偶发失败（``unknown``）不能当成「进程没了」：在 ``KILL_VERIFY_ATTEMPTS`` 轮内重试；仍核对不了的
    对它发 SIGCONT 解冻并告警——不在这里盲杀，因为核对不了身份时按「pid＋启动时间」才发信号的保护就失效了；
    解冻后它仍在快照里，后面的扫荡轮次还会再核对、再杀。核对出 ``other``（冻结的瞬间 pid 已被复用）说明被冻住的是无辜进程，
    同样发 SIGCONT 撤销冻结。"""

    killed: list[int] = []
    interrupted: BaseException | None = None
    pending = dict(frozen)
    for _ in range(KILL_VERIFY_ATTEMPTS):
        retry: dict[int, str] = {}
        for pid, start in pending.items():
            try:
                state = _identity(pid, start)
                if state == "same":
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed.append(pid)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        _note(notes, f"进程 {pid} 已冻结但无权发 SIGKILL（权限不足），仍处于停住状态")
                elif state == "other":
                    _unfreeze(pid)
                    _note(notes, f"冻结的 pid {pid} 在核对时已是另一个进程（pid 被复用），已发 SIGCONT 撤销冻结")
                elif state == "unknown":
                    retry[pid] = start
            except BaseException as error:   # noqa: BLE001 - 中断先记下，处理完其余进程再抛（已发出的 SIGKILL 不丢，也不留停住的进程）
                interrupted = interrupted or error
                retry[pid] = start
        pending = retry
        if not pending:
            break
        try:
            time.sleep(KILL_VERIFY_RETRY_SECONDS)
        except BaseException as error:   # noqa: BLE001
            interrupted = interrupted or error
    for pid in pending:
        _unfreeze(pid)
        _note(notes, f"进程 {pid} 冻结后连续 {KILL_VERIFY_ATTEMPTS} 次核对不了身份，已发 SIGCONT 解冻（没有杀；请核对它是否是遗留进程）")
    if interrupted is not None:
        raise interrupted
    return killed


def _kill_snapshot(snapshot: Mapping[int, str], root: int | None = None, notes: list[str] | None = None) -> list[int]:
    """终止快照里仍存活的全部进程（以及它们此后新生的后代、``root`` 的进程组成员）：先反复冻结（SIGSTOP）直到不再出现新进程，
    防止它们在被杀的过程中继续 fork，再统一 SIGKILL（``_kill_frozen``，不留停住的进程）；最后对进程组补一次 SIGKILL。
    返回实际发过 SIGKILL 的 pid。取快照出错时沿用已有快照，不让异常妨碍终止；``root`` 的进程组 SIGKILL 一定会发。"""

    frozen: dict[int, str] = {}
    killed: list[int] = []
    try:
        try:
            for _ in range(FREEZE_ROUNDS):
                fresh = {pid: start for pid, start in _refresh_snapshot(root, {**snapshot, **frozen}, notes).items() if pid not in frozen}
                if not fresh:
                    break
                for pid, start in fresh.items():
                    if _signal_if_same(pid, start, signal.SIGSTOP):
                        frozen[pid] = start
        finally:
            # 无论冻结阶段出什么错，冻住的都要杀掉（或至少解冻），不能把停住的进程留在后台。
            killed = _kill_frozen(frozen, notes)
    finally:
        # 进程组的 SIGKILL 是旧做法留下的底线：上面任何一步出错也要走到。
        if root is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(int(root), signal.SIGKILL)
    sent = set(killed)
    for _ in range(3):
        stray = {pid: start for pid, start in _refresh_snapshot(root, {**snapshot, **frozen}, notes).items() if pid not in sent}
        if not stray:
            break
        for pid, start in stray.items():
            if _signal_if_same(pid, start, signal.SIGKILL):
                sent.add(pid)
                killed.append(pid)
        time.sleep(0.05)
    return sorted(killed)


def _run_mode(payload: Mapping[str, Any], root: int, notes: list[str] | None) -> str:
    """动手前核对 payload 里的 run 进程 pid 还是不是那个 run 进程，决定清理到什么范围：

    * ``tree``：整棵后代树（含另起会话的）——只有核对过 ``pid_start`` 才做；
    * ``group``：只按进程组 SIGTERM→宽限→SIGKILL（旧做法）——结论里没有 ``pid_start``（旧结论、空跑结论），或读不到 pid 的启动标识；
    * ``skip``：什么都不做——pid 现在是另一个进程（启动标识对不上＝run 已经退出、pid 被无关进程复用）。

    调用前已确认进程组 ``root`` 里还有进程。run 进程（组长）已经退出而组里仍有成员时，``pid`` 不可能被无关进程占用（内核在进程组
    还存在时不会复用这个号），所以放行整棵子树清理：这正是 10-14 事故的形态（执行器已走，单元孤儿还在）。"""

    expected = payload.get("pid_start")
    if not isinstance(expected, str) or not expected:
        _note(notes, f"结论里没有 run 进程 {root} 的启动标识（pid_start，旧结论或入口空跑）：只按进程组 SIGTERM→SIGKILL 处理，不做整棵子树清理")
        return "group"
    try:
        row = _probe(root)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _note(notes, f"读不到 pid {root} 的启动标识（{error!r}），核对不了身份：按旧做法只处理进程组")
        return "group"
    if row is None or row[2] == expected:
        return "tree"
    _note(notes, f"pid {root} 现在是另一个进程（启动标识 {row[2]!r}，结论里记的是 {expected!r}）：run 进程早已退出、pid 被无关进程复用，不动手")
    return "skip"


def _terminate(payload: Mapping[str, Any], notes: list[str] | None = None) -> list[str]:
    """终止后台运行的整棵进程树：先取快照，再 SIGTERM 整组（执行器收到后终止全部单元），等整组退出、超过宽限再 SIGKILL，
    最终对**快照里仍存活的全部进程**（不只进程组）冻结后 SIGKILL。返回说明／告警列表（也记进 ``notes``、写到标准错误）。

    要等整组而不是只等组长：组长（``run`` 进程）收到 SIGTERM 立即退出，原来据此马上对整组发 SIGKILL，执行器正在逐个
    终止单元会话就被杀掉，排在后面的单元（各自成会话、不在本组里）留在后台继续跑（E4-02 验收实测）。

    快照必须在发信号之前取（WP-R5）：执行器没来得及 abort 就退出或被杀时，单元（各自成会话）与它们里面另起会话的嵌套进程
    都成了孤儿，ppid 链已断，事后找不到；``test_claude_real_delivery_integration`` 的嵌套 ``run-unit`` 就是这样留下来持锁运行的
    （10-14、10-15 实测）。宽限期内每轮把新出现的后代并进快照（执行器还在启停单元）；整组一退出就动手清剩下的，不空等宽限。

    取快照本身出错不能挡住信号（stop 已经先把结论标成 superseded，信号发不出去后台就还在跑）：出错的一步回落到旧做法，
    SIGTERM 一定先发，最后的进程组 SIGKILL 在 ``finally`` 里，中断也跳不过。动手前先核对 run 进程的启动标识（见 ``_run_mode``）。"""

    notes = [] if notes is None else notes
    pid = payload.get("pid")
    if not _group_alive(pid):
        return notes
    root = int(pid)
    mode = _run_mode(payload, root, notes)
    if mode == "skip":
        return notes
    snapshot: dict[int, str] = {}
    group_killed = False
    try:
        if mode == "tree":
            snapshot = _refresh_snapshot(root, {}, notes)   # 先于任何信号：执行器一死，单元的 ppid 链就断了
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(root, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while _group_alive(root) and time.monotonic() < deadline:
            time.sleep(0.2)
            if mode == "tree":
                snapshot = _refresh_snapshot(root, snapshot, notes)
        if mode == "tree":
            try:
                _kill_snapshot(snapshot, root, notes)
                group_killed = True
            except Exception as error:   # noqa: BLE001 - 按快照终止出了意外：回落到对进程组 SIGKILL（下面的 finally），原因记下来
                _note(notes, f"按快照终止时出错（{error!r}），回落到对进程组 SIGKILL")
    finally:
        if not group_killed:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(root, signal.SIGKILL)
    return notes


def stop(runroot: Path, reason: str, *, keep: Path | None = None, with_dryruns: bool = True,
         notes: list[str] | None = None) -> list[str]:
    """停下在跑的后台验证（``keep`` 除外），标 superseded；``with_dryruns`` 时连后台验证接上的入口空跑一起停（E4-02：空跑与
    前台入口门禁共用调度器和入口门禁工作目录，不停会挡住修复轮的定向回归）。两客户端都纳管时，本运行根搭上的（别的运行根
    发起的）那一次也停（B5a-10）。终止过程中的说明和告警（取快照出错而回落到旧做法、冻结后核对不了身份而改发 SIGCONT、
    pid 已被复用而没动手……）记进 ``notes``；某一个后台的终止出了意外也只记一条，不妨碍停其余的。"""

    stopped = []
    if with_dryruns and (HERE / "entry_dryrun.py").is_file():
        spec = importlib.util.spec_from_file_location("entry_dryrun_sibling", HERE / "entry_dryrun.py")
        if spec is not None and spec.loader is not None:
            module = sys.modules.get("entry_dryrun_sibling") or importlib.util.module_from_spec(spec)
            if "entry_dryrun_sibling" not in sys.modules:
                sys.modules["entry_dryrun_sibling"] = module
                spec.loader.exec_module(module)
            stopped += [f"entry-dryrun/{name}" for name in module.stop(runroot, reason)]
    for path, _payload in [*results(runroot), *attached(runroot)]:
        current = _read(path)   # 紧挨着写之前再读一次：刚写完结论的不再改
        if path == keep or current is None or current.get("status") != "running":
            continue
        # 先标 superseded 再终止：后台 ``run`` 收尾时看到状态已不是 running 就不再写（否则会写成 aborted）。
        _write(path, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        _terminate_reporting(current, path.name, notes)
        stopped.append(path.name)
    return stopped


def _terminate_reporting(current: Mapping[str, Any], name: str, notes: list[str] | None) -> None:
    """``_terminate`` 加一层兜底：它自己已把各步故障接住，这里再防意料之外的异常，让 stop 照常停其余的后台，并把原因记下来。"""

    try:
        _terminate(current, notes=notes)
    except Exception as error:   # noqa: BLE001
        _note(notes, f"终止 {name} 的后台进程时出错：{error!r}；该后台可能仍在运行，请人工核对")


def start(args: argparse.Namespace) -> dict[str, Any]:
    """同一运行根串行登记启动，避免两个入口同时读到空结论而重复派发。两客户端都纳管时另取数据根登记目录的锁（先运行根、
    后登记目录，顺序固定）：两个客户端的轮次各在自己的运行根里 start，也只起一次。"""
    root = Path(args.runroot) / DIRECTORY
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValidationError("后台验证运行根不得含符号链接")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        deployments = deployment_set(args.data_root)
        if deployments is None:
            return _start_locked(args)
        registry = registry_dir(args.data_root)
        if any(path.is_symlink() for path in (registry, *registry.parents)):
            raise ValidationError("后台验证登记目录不得含符号链接")
        registry.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared = os.open(registry / ".start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(shared, fcntl.LOCK_EX)
            return _start_locked(args, deployments=deployments)
        finally:
            os.close(shared)
    finally:
        os.close(fd)


REUSE_BINDING_SCHEMA = "background-reuse-binding/v1"
REUSE_SCHEMA = "background-reuse/v1"
REUSE_VALIDITY_HOURS = 24.0
REUSE_BIND_TIMEOUT_SECONDS = 3600
# 绑定的各项（键 → 说明文字）：逐项比对，哪项变了就报哪项。
REUSE_COMPONENTS = (("tool_identity", "工具身份五摘要"), ("commit", "提交"), ("gate_set_sha256", "门禁集合摘要"), ("executor_sha256", "执行器摘要"),
                    ("policy_sha256", "调度策略摘要"), ("inputs_manifest_sha256", "输入清单摘要"), ("environment_sha256", "平台环境摘要"))


def _identities(payload: Mapping[str, Any], deployments: Mapping[str, Mapping[str, Any]] | None = None,
                deployment: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """结论（或给定的部署）绑定的工具身份五摘要：每个已纳管客户端一份。"""

    bound = deployments if deployments is not None else payload.get("deployments")
    if bound:
        return {client: dict(item["identity"]) for client, item in sorted(bound.items())}
    one = deployment if deployment is not None else payload.get("deployment")
    return {str(payload.get("client") or "codex"): dict((one or {})["identity"])}


def executor_binding(out: Path | str) -> dict[str, Any] | None:
    """执行器在结束时写的 ``binding.json``（入口门禁总摘要指向的执行器汇总所在目录）；没有就返回 None。"""

    entry = _entry_summary(Path(out))
    if entry is None or not entry.get("executor_summary"):
        return None
    try:
        document = json.loads((Path(str(entry["executor_summary"])).parent / "binding.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    required = {"schema_version", "policy_sha256", "environment_sha256", "executor_sha256", "gate_set_sha256", "inputs_manifest_sha256"}
    return document if isinstance(document, dict) and required <= set(document) else None


def core_deviation_info(out: Path | str, destination: Path) -> dict[str, Any] | None:
    """执行器的核数偏差报告（入口门禁总摘要指向的执行器汇总所在目录）复制到结论目录 ``destination``；没有报告返回 None。"""

    entry = _entry_summary(Path(out))
    if entry is None or not entry.get("executor_summary"):
        return None
    source = Path(str(entry["executor_summary"])).parent / "core-deviation.json"
    try:
        report = json.loads(source.read_text(encoding="utf-8"))
        data = source.read_bytes()
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or "units_flagged" not in report:
        return None
    destination.write_bytes(data)
    os.chmod(destination, 0o600)
    return {"report": str(destination), "sha256": hashlib.sha256(data).hexdigest(), "threshold": report.get("threshold"),
            "units_measured": report.get("units_measured"), "units_flagged": report.get("units_flagged"), "overall_ratio": report.get("overall_ratio")}


def build_reuse_binding(payload: Mapping[str, Any], binding: Mapping[str, Any], *, completed_at_utc: str,
                        validity_hours: float = REUSE_VALIDITY_HOURS) -> dict[str, Any]:
    """通过的结论的沿用绑定：各项绑定值加有效期，自带摘要。"""

    components = {"tool_identity": _identities(payload), "commit": payload["commit"],
                  **{key: binding[key] for key, _label in REUSE_COMPONENTS if key not in ("tool_identity", "commit")}}
    completed = datetime.fromisoformat(completed_at_utc.replace("Z", "+00:00")).timestamp()
    body = {"schema_version": REUSE_BINDING_SCHEMA, "components": components, "completed_at_utc": completed_at_utc,
            "validity_hours": validity_hours,
            "valid_until_utc": datetime.fromtimestamp(completed + validity_hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {**body, "binding_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}


def reuse_binding_problems(binding: Any, *, now: float | None = None) -> list[str]:
    """沿用绑定的自检（格式、自摘要、各项齐全）与有效期；空列表＝可用。"""

    if not isinstance(binding, Mapping) or binding.get("schema_version") != REUSE_BINDING_SCHEMA:
        return ["结论没有沿用绑定（reuse_binding）或格式版本不对"]
    body = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if binding.get("binding_sha256") != hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest():
        return ["沿用绑定的自摘要不符（被改过）"]
    components = binding.get("components")
    if not isinstance(components, Mapping) or any(key not in components for key, _label in REUSE_COMPONENTS):
        return ["沿用绑定缺少绑定项"]
    try:
        until = datetime.fromisoformat(str(binding["valid_until_utc"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return ["沿用绑定的有效期读不出"]
    if (time.time() if now is None else now) > until:
        return [f"沿用的绑定已过有效期（{binding['valid_until_utc']}）：要求重验"]
    return []


def component_differences(source: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """逐项比对两份绑定值，返回变了的项（中文说明，工具身份写明哪个客户端的哪个摘要变了）。"""

    problems: list[str] = []
    for key, label in REUSE_COMPONENTS:
        if key == "tool_identity":
            before, after = source.get(key) or {}, current.get(key) or {}
            for client in sorted(set(before) | set(after)):
                for field in sorted(set(before.get(client) or {}) | set(after.get(client) or {})):
                    if (before.get(client) or {}).get(field) != (after.get(client) or {}).get(field):
                        problems.append(f"{label}变了：{client} 的 {field}")
                if client not in before or client not in after:
                    problems.append(f"{label}变了：已纳管客户端集合不同（{client}）")
        elif source.get(key) != current.get(key):
            problems.append(f"{label}变了：{str(source.get(key))[:16]} → {str(current.get(key))[:16]}")
    return problems


def _reuse_problems(payload: Mapping[str, Any]) -> list[str]:
    """``check`` 对沿用结论的重放：来源冻结副本的摘要、来源仍是通过、绑定未过期、本次比对记录与来源绑定一致、新部署的工具身份与来源一致。"""

    reuse = payload.get("reuse")
    if not isinstance(reuse, Mapping) or reuse.get("schema_version") != REUSE_SCHEMA:
        return ["沿用登记缺失或格式版本不对"]
    frozen = reuse.get("source_frozen") if isinstance(reuse.get("source_frozen"), Mapping) else {}
    try:
        if _sha256_file(Path(str(frozen.get("path")))) != frozen.get("sha256"):
            return ["沿用的来源结论冻结副本摘要不符（被改过或不在了）"]
    except OSError:
        return ["沿用的来源结论冻结副本读不到"]
    source = _read(Path(str(frozen["path"])))
    if source is None or source.get("status") != "passed" or source.get("commit") != payload.get("commit"):
        return ["沿用的来源结论不是同一提交的通过结论"]
    problems = reuse_binding_problems(source.get("reuse_binding"))
    if problems:
        return problems
    binding = source["reuse_binding"]
    if payload.get("reuse_binding") != binding or reuse.get("source_binding_sha256") != binding["binding_sha256"]:
        return ["沿用结论登记的绑定与来源不一致"]
    differences = component_differences(binding["components"], reuse.get("current_components") or {})
    differences += component_differences({"tool_identity": binding["components"]["tool_identity"]},
                                         {"tool_identity": _identities(payload)})
    return [f"沿用时的比对记录与来源绑定不符：{'；'.join(differences)}"] if differences else []


def _preflight_module() -> Any:
    """同目录的 background_preflight.py（起跑前置检查，优化第 4 项）：按路径加载。"""

    name = "background_preflight_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "background_preflight.py")
        if spec is None or spec.loader is None:
            raise ValidationError(f"找不到同目录的 background_preflight.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _preflight_gate(args: argparse.Namespace, *, client: str, deployment: Mapping[str, Any],
                    deployments: Mapping[str, Mapping[str, Any]] | None) -> None:
    """``--preflight on``（命令行缺省）时起跑前逐项核对冻结收据、基线重封、重部署、准入绑定；不通过抛 ``ValidationError``（拒绝起跑）。
    不是命令行解析出来的参数对象（没有 ``preflight`` 属性）什么都不做。"""

    if getattr(args, "preflight", "off") != "on":
        return
    module = _preflight_module()
    scratch = Path(args.runroot) / DIRECTORY / "preflight"
    try:
        report = module.preflight_with_bundle(bundle=Path(args.bundle), branch=args.branch, commit=args.commit, data_root=Path(args.data_root),
                                              runroot=Path(args.runroot), client=client, deployment=deployment, deployments=deployments,
                                              scratch_parent=scratch)
    except module.PreflightError as error:
        raise ValidationError(f"起跑前置检查做不了，拒绝起跑后台验证：{error}") from error
    _write(scratch / f"{args.commit[:12]}-{time.strftime('%Y%m%dt%H%M%Sz', time.gmtime())}.json", report)
    if not report["passed"]:
        raise ValidationError(module.format_failures(report))


def _reuse_candidates(args: argparse.Namespace, client: str) -> list[tuple[Path, dict[str, Any]]]:
    """同一提交、同一客户端、同一组合下可作沿用来源的通过结论（全集重新执行、有沿用绑定），完成时间新的在前。"""

    found = [(path, payload) for path, payload in results(args.runroot)
             if payload.get("commit") == args.commit and (payload.get("client") or "codex") == client and effective_status(payload) == "passed"
             and payload.get("mode") == "re-execute" and not payload.get("unit_inherit_request") and not payload.get("audit_policy")
             and payload.get("profile") == args.profile and isinstance(payload.get("reuse_binding"), dict)]
    return sorted(found, key=lambda item: str(item[1].get("completed_at_utc")), reverse=True)


def _current_binding(args: argparse.Namespace, source: Mapping[str, Any], *, deployments: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """现算新部署上的绑定：``entry-gates.sh --bind-only`` 在来源的同一个工作目录里走真实运行的树准备、部署绑定核对与清单／输入计算，不执行任何测试，
    只让执行器写 ``binding.json``。部署绑定核对不过（整树摘要或部署一致性不符）时入口门禁自己就报错退出，沿用随之拒绝。"""

    out = Path(args.runroot) / DIRECTORY / "reuse" / f"{args.commit[:12]}-{time.time_ns()}"
    require = "--require-deployed-all" if deployments else "--require-deployed"
    argv = ["bash", str(args.entry_gates), "--profile", str(source["profile"]), "--mode", "re-execute", require, "--wait-lock", str(WAIT_LOCK_SECONDS),
            "--out", str(out), "--bind-only"]
    for flag, key in (("--record-store", "record_store"), ("--work", "work")):
        if source.get(key):
            argv += [flag, str(source[key])]
    if "parallel" in source:
        argv += ["--parallel", str(source["parallel"])]
    if "declared_sample" in source:
        argv += ["--declared-sample", str(source["declared_sample"])]
    argv += [str(args.bundle), args.branch, args.commit]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **dict(source.get("cache_roots") or {}),
                   **({"ARM64_VC_ENV": str(args.vc_env)} if args.vc_env else {})}
    try:
        completed = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=REUSE_BIND_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValidationError(f"沿用核对的入口门禁没跑起来：{error}") from error
    binding_path = out / "executor" / "binding.json"
    try:
        document = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = "\n".join((completed.stdout + completed.stderr).strip().splitlines()[-4:])
        raise ValidationError(f"沿用核对的入口门禁没有产出绑定（退出码 {completed.returncode}；多半是部署与测试树不一致）：{tail[-400:]}") from None
    if completed.returncode != 0 or not isinstance(document, dict):
        raise ValidationError(f"沿用核对的入口门禁退出码 {completed.returncode}")
    return document


def _attempt_reuse(args: argparse.Namespace, *, client: str, deployment: Mapping[str, Any], deployments: Mapping[str, Mapping[str, Any]] | None,
                   path: Path, inputs: Mapping[str, Any], work: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """同树重部署沿用（优化第 5 项）。成功返回（起跑结果, []）；拒绝返回（None, 原因列表）。拒绝时不写任何结论，调用方照常起完整验证（auto）或报告（only）。"""

    candidates = _reuse_candidates(args, client)
    if not candidates:
        return None, ["同一提交没有带沿用绑定的通过结论可沿用"]
    refusals: list[str] = []
    for source_path, source in candidates:
        binding = source["reuse_binding"]
        problems = reuse_binding_problems(binding)
        current_identities = _identities(source, deployments, deployment)
        problems += component_differences({"tool_identity": binding["components"]["tool_identity"]}, {"tool_identity": current_identities}) \
            if not problems else []
        if problems:
            refusals.append(f"{source_path.name}：{'；'.join(problems)}")
            continue
        try:
            document = _current_binding(args, source, deployments=deployments)
        except ValidationError as error:
            refusals.append(f"{source_path.name}：{error}")
            continue
        current_components = {"tool_identity": current_identities, "commit": args.commit,
                              **{key: document.get(key) for key, _label in REUSE_COMPONENTS if key not in ("tool_identity", "commit")}}
        differences = component_differences(binding["components"], current_components)
        if differences:
            refusals.append(f"{source_path.name}：{'；'.join(differences)}")
            continue
        now = _utc_now()
        frozen_text = json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        frozen = path.parent / "history" / (hashlib.sha256(frozen_text.encode()).hexdigest() + ".json")
        if not frozen.exists():
            _write(frozen, source)
        reused = {key: value for key, value in source.items() if key not in ("pid", "pid_start", "deployments", "predecessor")}
        reused.update(
            deployment=dict(deployment), status="passed", started_at_utc=now, completed_at_utc=now, reason=None, returncode=0,
            then_dryrun=bool(args.then_dryrun), dryrun=None, input_contract=dict(inputs), work=str(work),
            # 接部署后空跑要用本次调用的坐标（参数文件、运行根、数据根、bundle、分支），不是来源结论里记的那一份。
            vc_env=str(args.vc_env) if args.vc_env else None, runroot=str(args.runroot), data_root=str(args.data_root),
            bundle=str(args.bundle), branch=args.branch, entry_gates=str(args.entry_gates),
            **({"deployments": dict(deployments)} if deployments is not None else {}),
            reuse={"schema_version": REUSE_SCHEMA, "source_result": str(source_path), "source_deployment": (source.get("deployment") or {}).get("receipt"),
                   "source_frozen": {"path": str(frozen), "sha256": _sha256_file(frozen)}, "source_binding_sha256": binding["binding_sha256"],
                   "current_components": current_components, "verified_at_utc": now,
                   "deployment_check": "entry-gates.sh --bind-only 在来源工作目录里完成部署绑定核对（整树摘要与部署一致性各部分逐项相同）"})
        _write(path, reused)
        started = {"action": "reused", "result": str(path), "source": str(source_path), "components": [label for _key, label in REUSE_COMPONENTS]}
        started.update(_dryrun_after_reuse(path, reused))
        return started, []
    return None, refusals


def _dryrun_after_reuse(path: Path, reused: Mapping[str, Any]) -> dict[str, Any]:
    """沿用成功之后接部署后空跑（验收修正必修 6），与 ``run`` 末尾“通过之后接空跑”同一逻辑：``then_dryrun`` 打开就接着起入口空跑到 pre-A3；
    两客户端都纳管时，搭这次验证的其它客户端的入口空跑一并接上。空跑状态记进结论（``dryrun``、``peer_dryruns``）并随起跑结果返回。
    沿用只省掉测试，不能顺带省掉新部署上的入口空跑。"""

    outcome: dict[str, Any] = {}
    if reused.get("then_dryrun"):
        outcome["dryrun"] = _chain_dryrun(reused)
        _write(path, {**(_read(path) or reused), "dryrun": outcome["dryrun"]})
    if reused.get("deployments"):
        chained = {}
        for peer in peers(registry_path(Path(reused["data_root"]), reused["commit"], reused["deployments"])):
            if peer.get("client") != (reused.get("client") or "codex"):
                chained[str(peer.get("client"))] = _chain_dryrun({**reused, "vc_env": peer.get("vc_env"), "runroot": peer.get("runroot"),
                                                                  "data_root": peer.get("data_root")})
        if chained:
            outcome["peer_dryruns"] = chained
            _write(path, {**(_read(path) or reused), "peer_dryruns": chained})
    return outcome


def _shared_contract(inputs: Any) -> dict[str, Any]:
    return {key: value for key, value in (inputs or {}).items() if key not in CLIENT_SPECIFIC_INPUTS}


def _register_peer(args: argparse.Namespace, registration: Path, client: str) -> dict[str, Any] | None:
    """另一客户端搭这次验证：登记它的入口空跑（参数文件与运行根），门禁通过后据此接着空跑；不接空跑时不登记。"""

    if not (args.vc_env and args.then_dryrun):
        return None
    peer = {"schema_version": PEER_SCHEMA, "client": client, "vc_env": str(args.vc_env), "runroot": str(args.runroot),
            "data_root": str(args.data_root), "registered_at_utc": _utc_now()}
    _write(registration.with_name(f"{registration.stem}.peer-{client}.json"), peer)
    return peer


def _attach(args: argparse.Namespace, registration: Path, target: Path, existing: Mapping[str, Any], client: str) -> dict[str, Any]:
    """同一提交与集合已有另一客户端发起的在跑验证或结论：不再起，只登记本客户端的入口空跑；已经通过的当场接上。"""

    peer = _register_peer(args, registration, client)
    own = Path(args.runroot) / DIRECTORY
    if target.parent != own:
        # 本运行根留指针：本轮的修复轮跑定向回归前 stop 要停得下它，status 也列得出它。
        _write(own / ATTACHED_DIRECTORY / f"{target.stem}.json",
               {"schema_version": ATTACHED_SCHEMA, "result": str(target), "registration": str(registration), "client": client,
                "attached_at_utc": _utc_now()})
    # 先登记、再重读状态：后台 run 写下 passed 之后才读登记，这里登记之后才判状态，两边总有一边接上空跑（同一份空跑
    # 已有就不重复起，两边都接也无妨）。
    current = _read(target) or existing
    status = effective_status(current)
    outcome: dict[str, Any] = {"action": "exists", "result": str(target), "status": status, "attached": client}
    if peer is not None and status == "passed":
        outcome["dryrun"] = _chain_dryrun({**current, "vc_env": peer["vc_env"], "runroot": peer["runroot"],
                                           "data_root": peer["data_root"]})
    return outcome


def stop_registered(data_root: Path, reason: str, *, keep: Path, notes: list[str] | None = None) -> list[str]:
    """两客户端都纳管时：数据根登记里其它提交或集合的在跑验证（在别的运行根）一并停下，标 superseded。"""

    stopped = []
    for _registration, entry in registrations(data_root):
        target = Path(str(entry.get("result") or ""))
        if not target.is_absolute() or target == keep:
            continue
        current = _read(target)
        if current is None or current.get("status") != "running":
            continue
        _write(target, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        _terminate_reporting(current, target.name, notes)
        stopped.append(str(target))
    return stopped


def _bound_work_roots(runroot: Path, data_root: Path, commit: str, deployment: Mapping[str, Any],
                      deployments: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[Path, Path]:
    """旧默认坐标保留；显式来源工作区按同一提交与部署绑定，避免后台发布变动它的精确目录输入。"""

    binding = f"{commit}-{binding_key(deployment, deployments)}"
    return (Path(runroot) / DIRECTORY / "work" / binding,
            Path(data_root).parent / "entry-gates-source-work" / binding)


def _unit_inherit() -> Any:
    """加载原逐单元合同，后台不另写批准、来源或决策验证规则。"""
    name = "background_unit_inherit_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "unit_inherit.py")
        if spec is None or spec.loader is None:
            raise ValidationError("后台逐单元承接验证器缺失")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _inherit_reference(request: Path, scope: Mapping[str, Any], *, check_approval: bool = True) -> dict[str, str]:
    """启动与派发前只核固定授权对象；实时读集与三次复核仍由原入口执行器完成。"""
    module = _unit_inherit()
    if not request.is_absolute() or any(path.is_symlink() for path in (request, *request.parents)):
        raise ValidationError("后台逐单元请求须为无符号链接的绝对路径")
    reference, payload = module._read_request(request)
    context = module.context_check(module.fs.replay_ref(payload["context"]))
    sources = module.sources_check(payload["sources"])
    expected = {"client": scope.get("client") or "codex", "stage": "pre-ledger", "commit": scope["commit"],
                "profile": DEFAULT_PROFILE, "record_store": str(Path(str(scope["record_store"])).resolve()),
                "deployment_receipts": sorted(module.deployment_set(Path(str(scope["data_root"])) / "control"),
                                              key=lambda item: item["client"])}
    if not payload["reuse_enabled"] or any(context.get(key) != value for key, value in expected.items()):
        raise ValidationError("后台逐单元请求未开启承接，或客户端、提交、部署集合、阶段、组合、记录库错绑")
    if "input_contract" in scope:
        contract = scope["input_contract"]
        if (_sha256_file(Path(str(scope["audit_policy"]))) != contract["full_set_files"]["audit_policy"]
                or _sha256_file(Path(str(scope["entry_gates"]))) != contract["entry_gates_sha256"]):
            raise ValidationError("后台逐单元策略或门禁入口在登记后变化")
    approvals = [module.fs.replay_ref(ref) for ref in payload["approvals"]]
    if check_approval:
        module.approvals_check(approvals, module.binding_sha256(context, sources), module.current_time())
    return reference


def _inherit_reexecuted(module: Any, manifest: Mapping[str, Any], store: Any, decision: Mapping[str, Any], fixed: Any,
                        consumption: Mapping[str, Any] | None) -> dict[str, Any]:
    """只有已通过的复核点把承接集合缩为空，才接受原执行器的全执行回退；授权失败回退仍拒绝。"""
    records = module.records()
    checkpoints = decision.get("checkpoints") or []
    if (decision.get("schema_version") != records.UNIT_INHERIT_DECISION_SCHEMA or decision.get("contract") != module.CONTRACT
            or not records.seal_ok(decision, "decision_sha256") or decision.get("action") != "reexecute-all"
            or decision.get("run_id") != manifest["run_id"] or decision.get("record_store") != str(store.root)
            or tuple(item.get("checkpoint") for item in checkpoints) not in (("queue",), ("queue", "lock"))
            or checkpoints[-1].get("inherited") != {} or not fixed.reuse_enabled
            or set(decision.get("units") or {}) != {row["unit_id"] for row in manifest["units"]}
            or any(row.get("disposition") != "executed" for row in manifest["units"])
            or any(row.get("disposition") != "executed" or row.get("source") is not None for row in decision["units"].values())
            or store.manifest(manifest["run_id"]) != manifest or records.verify_manifest(manifest, store=store, historical=True)
            or manifest.get("environment_sha256") != fixed.context["environment_sha256"]
            or manifest.get("policy_sha256") != fixed.context["policy_sha256"]
            or (manifest.get("executor") or {}).get("sha256") != fixed.context["executor_sha256"]):
        raise ValidationError("后台全执行回退缺少完整原件，或不是已授权的空承接集合")
    for checkpoint in checkpoints:
        module._checkpoint_proof(checkpoint, fixed, set())
    if consumption is None:
        moment = module.current_time()
        clock = module.put_evidence(store.root, module.sample_clock(fixed.clock_source, moment))
        revoked = module.put_evidence(store.root, module.query_revocation(store.root, sorted(fixed.sources["runs"]),
                                      fixed.context["stage"], moment, fixed.campaign_status_source))
        body = records.seal({"schema_version": module.CONSUMPTION_SCHEMA, "run_id": manifest["run_id"],
                             "decision_sha256": decision["decision_sha256"], "purpose": "background-validation",
                             "consumed_at_utc": module.fs.timestamp(moment), "consumer_clock": clock, "revocation": revoked,
                             "verifier": module.verifier_files()}, "consumption_sha256")
        module._consumption_proof(body, decision, fixed, set())
        consumption = module.put_evidence(store.root, body)
    else:
        module._consumption_proof(module.fs.replay_ref(dict(consumption)), decision, fixed, set())
    return {"run_id": manifest["run_id"], "decision_sha256": decision["decision_sha256"], "inherited": 0,
            "executed": len(manifest["units"]), "consumption": dict(consumption), "verifier": module.verifier_files()}


def _inherit_completion(payload: Mapping[str, Any], *, historical: bool = False) -> dict[str, Any]:
    """新结论实际消费原决策；边界像认证核验一样重放形成期及签发时消费证明，不重签或改写旧结论。"""
    module = _unit_inherit()
    request = payload["unit_inherit_request"]
    if (_inherit_reference(Path(request["path"]), payload, check_approval=not historical) != request
            or payload["input_contract"].get("unit_inherit_request") != request):
        raise ValidationError("后台逐单元请求在登记后变化")
    out = Path(str(payload["out"]))
    summary_path = out / "entry-gates.json"
    manifest_path = out / "executor" / "unit-manifest.json"
    plan_path = out / "gates-manifest.json"
    summary = module.fs.read(summary_path)
    manifest = module.fs.read(manifest_path)
    plan = module.fs.read(plan_path)
    reference = summary.get("unit_manifest") or {}
    if (summary.get("status") != "passed" or summary.get("mode") != manifest.get("mode")
            or manifest.get("mode") not in (module.MODE, "re-execute")
            or summary.get("profile") != DEFAULT_PROFILE or plan.get("profile") != DEFAULT_PROFILE
            or summary.get("record_store") != payload["record_store"]
            or summary.get("test_tree") != str(Path(payload["work"]) / "test-tree")
            or (summary.get("source") or {}).get("commit") != payload["commit"]
            or reference.get("path") != str(manifest_path) or reference.get("self_check") != "passed"
            or reference.get("manifest_sha256") != manifest.get("manifest_sha256")
            or manifest.get("out_dir") != str(out / "executor")
            or not manifest.get("units") or any(row.get("passed") is not True for row in manifest["units"])):
        raise ValidationError("后台通过结论未绑定本次完整逐单元清单")
    store = module.records().RecordStore(Path(str(payload["record_store"])))
    decision_path = (store.decision_path(manifest["run_id"]) if manifest["mode"] == module.MODE
                     else out / "executor" / "unit-inherit-decision.json")
    decision = module.fs.read(decision_path)
    fixed = module.replay_fixed(decision)
    if (fixed.request_ref != request or fixed.context["profile"] != DEFAULT_PROFILE
            or fixed.context["gates"] != sorted(row["gate_id"] for row in plan["gates"])
            or fixed.context["gates"] != sorted(row["gate_id"] for row in summary["gates"])
            or any(row.get("status") != "passed" for row in summary["gates"])):
        raise ValidationError("后台门禁全集、当前请求与决策收据不一致")
    references = {"summary": module.fs.reference(summary_path), "manifest": module.fs.reference(manifest_path),
                  "plan": module.fs.reference(plan_path), "decision": module.fs.reference(decision_path)}
    previous = payload.get("unit_inherit_consumption") if historical else None
    if historical and (not isinstance(previous, dict) or previous.get("references") != references):
        raise ValidationError("后台逐单元消费凭证缺失，或签发后的原件发生变化")
    consumption = previous["proof"]["consumption"] if historical else None
    if historical and (not isinstance(consumption, dict) or set(consumption) != {"path", "sha256"}):
        raise ValidationError("后台签发时的实际消费凭证缺失")
    proof = (module.verify_published(manifest, store.root, purpose="background-validation", historical=historical,
                                     consumption=consumption) if manifest["mode"] == module.MODE
             else _inherit_reexecuted(module, manifest, store, decision, fixed, consumption))
    if historical and any(proof[key] != previous["proof"].get(key) for key in ("run_id", "decision_sha256", "inherited", "executed")):
        raise ValidationError("后台逐单元消费凭证与实际决策不一致")
    return {"references": references, "proof": proof}


# ---------------------------------------------------------------- 跨提交、跨部署的逐单元后继（来源校验与承接登记）
#
# 为什么要有：H7 里故意让一个测试失败，后台形成 failed 结论并拒派；H8 修好、换了提交与部署之后，需要只重跑失败与受影响
# 的单元，其余单元承接 H7 那一轮实际执行成功的记录。原来的续作要求同提交、同部署的原结论，H8 的提交与部署都变了，
# 找不到原结论。这里新增的只有两件事：
#   1. 启动时校验「来源后台结论」（旧运行只作来源，新运行是新结论、新运行目录、新运行 ID）；
#   2. 结束时把每一条承接项的来源运行 ID 与记录摘要、四个分开的计数和每个重跑单元的原因与耗时登记进新结论。
# 单元记录能否承接，仍然完全由原逐单元合同（unit_inherit.py 的复核点与消费者）判定，这里不放宽任何一条。


class _AncestryUndetermined(Exception):
    """祖先关系没能判定（环境或材料不足）。与「判定为不是祖先」不同：后者永远拒绝，前者才可经 ``--allow-unverified-ancestry`` 退化。"""


def _git_environment() -> dict[str, str]:
    """git 子进程的环境：去掉继承来的 ``GIT_*``（例如从 git 钩子里运行时的 GIT_DIR），免得验证落到别的仓库上；不弹交互。

    同时屏蔽系统与全局 git 配置（``GIT_CONFIG_NOSYSTEM=1``、``GIT_CONFIG_GLOBAL=/dev/null``）：祖先验证只在一个刚建的临时
    裸库里验 bundle、取新提交、查 merge-base，不需要、也不该受运行账号 ``~/.gitconfig`` 或 XDG 配置的影响——那里的
    ``core.hooksPath`` 会让 ``fetch`` 更新引用时执行钩子（``reference-transaction``），``init.templateDir`` 会把模板里的钩子
    复制进临时库。``GIT_CONFIG_GLOBAL`` 需要 git 2.32 及以上，更老的 git 不认它，所以 ``_git`` 的命令行上另有
    ``-c core.hooksPath=/dev/null`` 作第二道防线。"""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", LC_ALL="C")
    return environment


def _git(repo: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    # ``-c core.hooksPath=/dev/null`` 放在子命令之前，对所有子命令一律生效：不论钩子来自全局配置、模板还是临时库自己的
    # hooks 目录，都不会被执行（git 到 /dev/null 下找不到钩子文件）。主要防的是 ``fetch`` 更新引用时的 reference-transaction。
    argv = ["git", "--no-optional-locks", *(["-C", str(repo)] if repo is not None else []), "-c", "core.hooksPath=/dev/null", *args]
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
                          env=_git_environment())


def _one_line(text: str, limit: int = 240) -> str:
    """git 的多行输出折成一行、取最后 ``limit`` 个字符，放进一条中文原因里。"""

    return " ".join(str(text).split())[-limit:]


def _ancestry_attempt(old: str, new: str, *, bundle: Path, branch: str, work: Path, repo: Path) -> str:
    """在临时对象库 ``repo`` 里判定 ``old`` 是不是 ``new`` 的祖先，返回验证方式的说明。

    历史从哪来：增量 bundle（``git bundle create <BASE>..<分支>``）只带新提交，旧历史要靠别处。WORK 里的 ``test-tree`` 是
    完整历史克隆（入口门禁的 ``verify_history_tree`` 要求提交数过万且非浅克隆，入口线的原位后继同样靠它做祖先核对），所以把它的
    对象库登记为临时库的备用对象库（只读引用，不改它的任何文件、索引或引用），再把新 bundle 取进临时库。没有测试树时只剩 bundle
    自带的历史，增量 bundle 会在前置提交校验处失败，按「无法验证」处理。
    材料不足抛 ``_AncestryUndetermined``；确定不是祖先抛 ``ValidationError``。"""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return _git(repo, *args)
        except (OSError, subprocess.SubprocessError) as error:
            raise _AncestryUndetermined(f"git 执行失败：{error}") from error

    try:
        created = _git(None, "init", "--bare", "-q", str(repo))
    except (OSError, subprocess.SubprocessError) as error:
        raise _AncestryUndetermined(f"找不到可用的 git：{error}") from error
    if created.returncode != 0:
        raise _AncestryUndetermined("临时对象库创建失败：" + _one_line(created.stderr))
    tree = Path(work) / "test-tree"
    objects = tree / ".git" / "objects"
    borrowed = objects.is_dir() and not any(item.is_symlink() for item in (Path(work), tree, tree / ".git", objects))
    if borrowed:
        alternates = repo / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(objects) + "\n", encoding="utf-8")
    verified = run("bundle", "verify", str(bundle))
    if verified.returncode != 0:
        raise _AncestryUndetermined("bundle 校验失败（它的前置提交不在可用历史里，或不是合法 bundle）："
                                    + _one_line(verified.stderr or verified.stdout))
    heads = run("bundle", "list-heads", str(bundle))
    refs = [line.split() for line in heads.stdout.splitlines() if line.strip()]
    wanted = [ref[1] for ref in refs if len(ref) >= 2 and ref[0] == new and ref[1] in {branch, "refs/heads/" + branch}]
    if heads.returncode != 0 or not wanted:
        # 这是参数自相矛盾（入口门禁也会拒绝），不是「无法验证」：不可用 --allow-unverified-ancestry 绕过。
        raise ValidationError(f"bundle 里没有分支 {branch} 指向 {new[:12]}，核对不了祖先关系：请核对 --bundle、--branch 与 --commit")
    fetched = run("-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "-q", "--no-tags", str(bundle),
                  f"+{wanted[0]}:refs/heads/ancestry-new")
    if fetched.returncode != 0:
        raise _AncestryUndetermined("把新 bundle 取进临时对象库失败：" + _one_line(fetched.stderr))
    for label, commit in (("新", new), ("旧", old)):
        if run("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            raise _AncestryUndetermined(f"{label}提交 {commit[:12]} 不在可用历史里（WORK 测试树的对象库加新 bundle）")
    answer = run("merge-base", "--is-ancestor", old, new)
    if answer.returncode == 0:
        return "git merge-base --is-ancestor（临时对象库：" + ("WORK 测试树对象库作备用，加新 bundle）" if borrowed else "仅新 bundle 自带历史）")
    if answer.returncode == 1:
        raise ValidationError(f"旧提交 {old[:12]} 不是新提交 {new[:12]} 的祖先：来源后台属于另一条历史线，拒绝承接它的成果")
    raise _AncestryUndetermined("merge-base 判定失败：" + _one_line(answer.stderr))


def _verify_ancestry(old: str, new: str, *, bundle: Path, branch: str, work: Path, allow_unverified: bool = False) -> dict[str, Any]:
    """旧提交是新提交的祖先（附加护栏，不替代任何承接条件）。

    建议与理由：要。入口线的原位后继本来就要求「测试树旧 HEAD 是新提交的祖先」（``prepare_scope_successor``），后台线与入口线共用
    同一个物理 WORK，来源后台留下的记录又带着该 WORK 里旧提交的读集与快照；若来源属于另一条历史线，记录的输入摘要即便恰好相同，
    也不是「这个提交的前序」，应当在启动时就明确拒绝，而不是等入口门禁在运行中才失败。ARM64 的 WORK 具备验证条件（测试树是
    完整历史克隆，增量 bundle 的前置提交就在其中），所以默认必须验证成功，否则拒绝。
    返回登记用的说明：``verified``（含验证方式），或 ``unverified``（仅 ``allow_unverified`` 且材料不足时，如实写明原因）。"""

    base = {"old_commit": old, "new_commit": new}
    if old == new:
        return {**base, "status": "verified", "method": "same-commit"}
    scratch: Path | None = None
    try:
        scratch = Path(tempfile.mkdtemp(prefix="background-validation-ancestry-"))
        method = _ancestry_attempt(old, new, bundle=Path(bundle), branch=branch, work=Path(work), repo=scratch / "history.git")
    except (OSError, _AncestryUndetermined) as undetermined:   # 临时目录建不出来之类的环境问题同属「无法验证」
        if not allow_unverified:
            raise ValidationError(f"无法验证旧提交 {old[:12]} 是新提交 {new[:12]} 的祖先：{undetermined}。真正的非祖先永远拒绝；"
                                  "只有像这样「无法验证」时，才可显式加 --allow-unverified-ancestry 退化为在结论里标明 unverified") from undetermined
        return {**base, "status": "unverified", "method": "none", "reason": str(undetermined)}
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return {**base, "status": "verified", "method": method}


def _inherit_source_roots(runroot: Path, data_root: Path, source: Mapping[str, Any]) -> tuple[Path, Path]:
    """来源结论自己的提交与部署绑定的两个 WORK 根（与它启动时 ``_bound_work_roots`` 同一算法）。"""

    commit, deployment, deployments = source.get("commit"), source.get("deployment"), source.get("deployments")
    if (not isinstance(commit, str) or not _COMMIT_PATTERN.fullmatch(commit) or not isinstance(deployment, dict)
            or not deployment.get("sha256") or (deployments is not None and not isinstance(deployments, dict))):
        raise ValidationError("来源后台结论缺少提交或部署绑定，确定不了它的 WORK 坐标")
    try:
        return _bound_work_roots(Path(runroot), Path(data_root), commit, deployment, deployments)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValidationError(f"来源后台结论的部署绑定不完整：{error}") from error


def _inherit_source_load(args: argparse.Namespace, source_path: Path) -> tuple[dict[str, Any], str]:
    """读来源后台结论：只认本运行根的结论（或其 history 子目录里保存的旧终态），同一次读取得到内容与摘要。"""

    path = Path(source_path)
    root = Path(args.runroot) / DIRECTORY
    if not path.is_absolute() or ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValidationError("来源后台结论须是无符号链接的规范绝对路径")
    if path.parent not in (root, root / "history"):
        raise ValidationError(f"来源后台结论必须是本运行根的后台结论（在 {root} 或其 history 子目录下）：{path}")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValidationError(f"来源后台结论不可读或不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise ValidationError(f"来源后台结论不是 {SCHEMA}：{path}")
    return payload, hashlib.sha256(raw).hexdigest()


# 来源结论的状态不能作来源时的说明（只有 passed 与 failed 且进程组已退出的结论留有可承接的成果）。
_SOURCE_STATUS_REASONS = {
    "running": "来源后台仍在运行，拒绝承接（running 一律拒绝）：请等它结束成 passed 或 failed 再起后继",
    "vanished": "来源后台进程已不在而没有写结论，没有可承接的成果",
    "aborted": "来源后台中止、没有门禁结论，不能作为承接来源",
    "superseded": "来源后台被停下（superseded）：被停下的运行没有发布运行清单，它执行过的单元记录不能承接",
}


def _inherit_source_check(args: argparse.Namespace, source_path: Path, loaded: tuple[dict[str, Any], str], *, client: str,
                          work: Path, inputs: Mapping[str, Any], result_path: Path) -> dict[str, Any]:
    """校验来源后台结论并返回登记用的来源信息；任一项不成立抛 ``ValidationError``（中文原因）。

    只校验「这个旧后台能不能当来源」，不判单元记录能不能承接：后者是原逐单元合同在三个复核点的事，一条都不放宽。"""

    source, digest = loaded
    if Path(source_path) == Path(result_path):
        raise ValidationError("来源后台结论就是本提交与部署的当前结论：同提交、同部署的续作不要带 --unit-inherit-source-result")
    status = effective_status(source)
    if status not in ("passed", "failed"):
        raise ValidationError(_SOURCE_STATUS_REASONS.get(status, f"来源后台状态 {status} 不能作为承接来源（只接受 passed、failed）"))
    if _group_alive(source.get("pid")):
        raise ValidationError("来源后台进程组尚未退出，拒绝在同 WORK 启动后继")
    # 允许变化的只有提交、部署、bundle、分支与入口门禁摘要；其余与入口线的原位后继同口径，必须相同。
    differing = []
    if (source.get("client") or "codex") != client:
        differing.append(f"客户端（来源 {source.get('client') or 'codex'}，本次 {client}）")
    if source.get("profile") != args.profile or args.profile != DEFAULT_PROFILE:
        differing.append(f"组合（来源 {source.get('profile')}，本次 {args.profile}，仅限 {DEFAULT_PROFILE}）")
    if source.get("work") != str(work):
        differing.append(f"WORK（来源 {source.get('work')}，本次 {work}）")
    if source.get("record_store") != inputs.get("record_store"):
        differing.append(f"记录库（来源 {source.get('record_store')}，本次 {inputs.get('record_store')}）")
    if (source.get("input_contract") or {}).get("parallel") != inputs.get("parallel"):
        differing.append(f"调度并发参数（来源 {(source.get('input_contract') or {}).get('parallel')}，本次 {inputs.get('parallel')}）")
    if differing:
        raise ValidationError("来源后台结论必须与本次后继同客户端、同组合、同物理 WORK、同记录库、同调度并发参数，不一致："
                              + "；".join(differing))
    runroot, data_root = Path(args.runroot), Path(args.data_root)
    roots = _work_lineage(runroot, data_root, source)
    source_work = Path(str(source["work"]))
    if not source_work.is_absolute() or ".." in source_work.parts or not any(source_work.is_relative_to(root) for root in roots):
        raise ValidationError("来源后台结论的 WORK 没有绑定它自己的提交与部署，疑似被改动")
    # 来源留下的成果：入口门禁摘要与逐单元清单（执行器中途终止的运行不发布清单，其单元记录不能承接）。
    module = _unit_inherit()
    records = module.records()
    out = Path(str(source.get("out") or ""))
    try:
        summary = module.fs.read(out / "entry-gates.json")
        manifest = module.fs.read(out / "executor" / "unit-manifest.json")
    except (OSError, ValueError) as error:
        raise ValidationError(f"来源后台没有留下入口门禁摘要或逐单元清单（执行器中途终止的运行不发布清单），其成果不能承接：{error}") from error
    reference = summary.get("unit_manifest") or {}
    if (manifest.get("schema_version") != records.MANIFEST_SCHEMA or not records.seal_ok(manifest, "manifest_sha256")
            or reference.get("path") != str(out / "executor" / "unit-manifest.json") or reference.get("self_check") != "passed"
            or reference.get("manifest_sha256") != manifest.get("manifest_sha256")):
        raise ValidationError("来源后台的逐单元清单自摘要不符，或与入口门禁摘要登记的不是同一份（自检不通过的清单不会发布）")
    summary_commit = (summary.get("source") or {}).get("commit")
    if summary_commit is not None and summary_commit != source.get("commit"):
        raise ValidationError(f"来源后台结论的提交（{str(source.get('commit'))[:12]}）与它入口门禁摘要里的提交（{str(summary_commit)[:12]}）不一致")
    run_id = str(manifest.get("run_id") or "")
    store = records.RecordStore(Path(str(source["record_store"])))
    if manifest.get("record_store") != str(store.root) or store.manifest(run_id) != manifest:
        raise ValidationError(f"来源后台的运行清单（{run_id}）没有发布进本记录库，或与发布的不是同一份：其单元记录不能承接")
    ancestry = _verify_ancestry(str(source["commit"]), str(args.commit), bundle=Path(args.bundle), branch=str(args.branch),
                                work=source_work, allow_unverified=bool(getattr(args, "allow_unverified_ancestry", False)))
    return {
        "schema": INHERIT_SOURCE_SCHEMA, "result": {"path": str(source_path), "sha256": digest}, "status": status,
        "commit": source["commit"], "branch": source.get("branch"), "deployment": source["deployment"],
        "deployments": source.get("deployments"), "work": str(source_work), "record_store": source["record_store"],
        "client": client, "out": str(out), "executor_run_id": run_id, "executor_manifest_sha256": manifest["manifest_sha256"],
        "executor_mode": manifest.get("mode"), "ancestry": ancestry, "validated_at_utc": _utc_now(),
    }


def _request_units_from(request: Path, run_id: str) -> int:
    """承接请求的来源集合里有多少个单元指向 ``run_id`` 那一轮的执行记录（启动时的提示：为 0 说明来源后台对这次承接没有贡献）。"""

    module = _unit_inherit()
    sources = module.sources_check(module._read_request(Path(request))[1]["sources"])
    return sum(1 for item in sources["units"].values() if item["run_id"] == run_id)


def _inherit_source_contract(source: Mapping[str, Any]) -> dict[str, Any]:
    """写进输入合同的来源指纹（稳定字段）：重复执行同一条 start 命令能认出是同一次，换了来源则判输入合同不同。"""

    return {"result": dict(source["result"]), "executor_run_id": source["executor_run_id"], "commit": source["commit"]}


def _frozen_source_payload(runroot: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    """来源登记里的冻结副本（内容寻址存在本运行根的 history 目录，登记着摘要）：不在 history 目录、摘要不符、缺失或不可读都抛
    ``ValidationError``。"""

    frozen = source.get("frozen") or {}
    path = Path(str(frozen.get("path") or ""))
    if (not path.is_absolute() or path.parent != Path(runroot) / DIRECTORY / "history" or path.is_symlink() or not path.is_file()
            or _sha256_file(path) != frozen.get("sha256")):
        raise ValidationError("后台跨提交承接的来源结论冻结副本缺失或被改动")
    payload = _read(path)
    if payload is None:
        raise ValidationError("后台跨提交承接的来源结论冻结副本不可读")
    return payload


# 跨提交后继可以一棵接一棵地接下去（H7 → H8 → H9 …），物理 WORK 始终是第一棵留下的那个；链太长多半是登记被改成了环。
MAX_LINEAGE_DEPTH = 32


def _work_lineage(runroot: Path, data_root: Path, payload: Mapping[str, Any], *, depth: int = 0) -> tuple[Path, ...]:
    """一份后台结论允许使用的 WORK 根：它自己的提交与部署绑定的两个根；它本身也是跨提交后继时，再加上它的来源（沿冻结副本逐级
    向上）允许的根——后继沿用来源的物理 WORK，而那个 WORK 绑定的是最早一棵的提交与部署。"""

    roots = _inherit_source_roots(runroot, data_root, payload)
    source = payload.get("unit_inherit_source")
    if source is None:
        return roots
    if depth >= MAX_LINEAGE_DEPTH:
        raise ValidationError("后台跨提交后继链过长，疑似登记成了环")
    return (*roots, *_work_lineage(runroot, data_root, _frozen_source_payload(runroot, source), depth=depth + 1))


def _inherit_source_work_roots(payload: Mapping[str, Any]) -> tuple[Path, ...]:
    """后继结论登记的来源允许使用的 WORK 根（含来源链上更早的）；没有来源登记的结论返回空（运行期的 WORK 校验据此放宽到来源的
    物理 WORK）。以冻结副本为准，不信任结论文件里可被改动的来源坐标。"""

    source = payload.get("unit_inherit_source")
    if source is None:
        return ()
    runroot = Path(payload["runroot"])
    return _work_lineage(runroot, Path(payload["data_root"]), _frozen_source_payload(runroot, source))


def _inherit_source_recheck(payload: Mapping[str, Any]) -> None:
    """运行期再核对来源登记：与输入合同一致、冻结副本未被改动、WORK 仍是来源的物理 WORK。不一致抛 ``ValidationError``。"""

    source = payload["unit_inherit_source"]
    if (not isinstance(source, dict) or source.get("schema") != INHERIT_SOURCE_SCHEMA or payload.get("mode") != "unit-inherit"
            or payload["input_contract"].get("unit_inherit_source") != _inherit_source_contract(source)
            or source.get("work") != payload.get("work") or source.get("record_store") != payload.get("record_store")):
        raise ValidationError("后台跨提交承接的来源登记与输入合同不一致")
    roots = _inherit_source_work_roots(payload)   # 内含冻结副本的存在与摘要核对
    if not any(Path(str(source["work"])).is_relative_to(root) for root in roots):
        raise ValidationError("后台跨提交承接登记的 WORK 没有绑定来源自己的提交与部署")


# 声明级承接（declared-inputs/v1）记录的读集覆盖口径名与范围条目名，与 unit_records.DECLARED_COVERAGE_SCOPE、DECLARED_SCOPE_ENTRY 同值（测试核对
# 各处一致）；本模块不导入记录层，只在判断「审计证据是否成立」时认这两个名字。
DECLARED_COVERAGE_SCOPE = "declared-inputs/v1"
DECLARED_SCOPE_ENTRY = "declared-inputs-scope"
# 声明级记录的 read_audit.sample_origin 只有这两种：registry＝登记表里的证据文件，run＝本次执行自己采样出的证据。
DECLARED_SAMPLE_ORIGINS = ("registry", "run")

_AUDIT_DIGEST = re.compile(r"[0-9a-f]{64}")


def _exact_zero(value: Any) -> bool:
    """类型恰为 ``int`` 且等于 0。``False == 0``、``0.0 == 0`` 在 Python 里都成立，但封存物里的未声明数是 ``False`` 或 ``0.0`` 说明它不是执行器写的：
    不能冒充「未声明数为零」（独立验收 #5 一般 4）。``type(x) is int`` 同时排除 bool 与 int 的任何子类。"""

    return type(value) is int and value == 0


def _same_inputs_digest(audit_digest: Any, record_digest: Any) -> bool:
    """审计绑定的输入摘要就是记录自己的输入摘要：两边都得是 64 位小写十六进制字符串且相等。直接 ``==`` 会让两边都缺（``None == None``）的
    封存物冒充「摘要一致」。"""

    return isinstance(audit_digest, str) and _AUDIT_DIGEST.fullmatch(audit_digest) is not None and audit_digest == record_digest


def _audit_evidence_ok(records: Any, record: Mapping[str, Any] | None, expected_sha256: str) -> bool:
    """一条来源记录自身的读集审计证据是否成立：记录读得出、自摘要有效且就是登记的那一条、是正式执行、审计通过且覆盖闭合。

    只看「审计」这一层（被篡改的记录不算审计通过）；单元执行本身是否通过、输入是否变化、是否过期等其余承接条件另由逐单元合同判定。"""

    if not isinstance(record, Mapping) or not records.seal_ok(record, "record_sha256") or record.get("record_sha256") != expected_sha256:
        return False
    audit = record.get("read_audit")
    if isinstance(audit, Mapping) and audit.get("coverage_scope") == DECLARED_COVERAGE_SCOPE:
        # 声明级承接（declared-inputs/v1，步骤 5 WP3b）的记录：读集覆盖口径不是全路径，而是「采样路径全部落在声明范围内」，同样只认审计这一层——
        # 正式执行、状态 passed、覆盖闭合、未声明数为零、审计绑定的输入摘要就是这条记录的。incomplete（只因证据不完整）与 failed（有未覆盖）的
        # 记录都不算审计通过：认证运行里它们就该重跑（失败关闭）。其余承接条件（声明范围核验、合同凭证、证据重算）由逐单元合同的消费者判定。
        #
        # 只信记录自述（read_audit 里的字段）不够：任何单元的记录都能自述「声明级、通过、覆盖闭合、0 未声明、摘要一致」。所以在自述之外还要求
        # 记录的结构真的像一条声明级记录——这些是执行器写声明级记录时一定带上的、可以只看记录本身核对的事实（统计口径用的轻量检查；真正的放行判据
        # 仍是消费者核验 check_record，它会重算覆盖，这里不替它做、也不改它）：
        # ① 记录输入里恰有一个 declared-inputs-scope 条目（范围条目是声明级单元的输入，缺了或重复了说明它不是按声明范围算的输入）；
        # ② read_audit.trace 是对象且带 64 位十六进制的 sha256（封存的采样证据，不论来源是登记表还是本次采样）；
        # ③ read_audit.sample_binding 是对象，且其 unit_id 就是这条记录的单元（证据绑定在这个单元上，不是别的单元的）；
        # ④ read_audit.sample_origin 是 registry 或 run。
        # 记录是封存物，自摘要有效不等于内容规矩（伪造者可以封存任意形状）：每一项都先核类型再取值，形状不对一律判不成立，不抛异常。
        inputs = record.get("inputs")
        scope_entries = [item for item in (inputs if isinstance(inputs, list) else [])
                         if isinstance(item, Mapping) and item.get("name") == DECLARED_SCOPE_ENTRY]
        trace, binding, unit_id = audit.get("trace"), audit.get("sample_binding"), record.get("unit_id")
        digest = trace.get("sha256") if isinstance(trace, Mapping) else None
        # 判型要严（独立验收 #5 一般 4）：未声明数只认类型恰为 int 且等于 0（False、0.0 不行），输入摘要两边都得是 64 位十六进制且相等（None == None 不行）；
        # kind／status／coverage_scope 是与字符串字面量比较（非字符串永远不等），coverage_complete 本来就是 ``is True``，没有同样的宽松等于问题。
        return (record.get("kind") == "formal" and audit.get("status") == "passed" and audit.get("coverage_complete") is True
                and _exact_zero(audit.get("undeclared_count")) and _same_inputs_digest(audit.get("inputs_sha256"), record.get("inputs_sha256"))
                and len(scope_entries) == 1
                and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
                and isinstance(unit_id, str) and isinstance(binding, Mapping) and binding.get("unit_id") == unit_id
                and audit.get("sample_origin") in DECLARED_SAMPLE_ORIGINS)
    return (record.get("kind") == "formal" and isinstance(audit, Mapping) and audit.get("status") == "passed"
            and audit.get("coverage_complete") is True and audit.get("coverage_scope") == "all-file-paths/v1"
            and _exact_zero(audit.get("undeclared_count")) and _same_inputs_digest(audit.get("inputs_sha256"), record.get("inputs_sha256")))


# 比较两条记录的输入时不计这些：宿主快照与读集合同项每次重新快照，不代表仓库内容变化（逐单元合同按来源沿用并重新快照）。
_VOLATILE_INPUTS = frozenset({"runtime-contract", "require-read-audit", "reexecute-only-policy"})


def _input_changes(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    """两条记录之间变化的输入名，以及变化的单元规格、环境、执行器、调度策略（任一条记录读不出来就返回空：不猜）。"""

    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return [], []

    def digests(record: Mapping[str, Any]) -> dict[str, Any]:
        return {str(item.get("name")): item.get("sha256") for item in record.get("inputs") or []
                if isinstance(item, Mapping) and not str(item.get("name")).startswith("host:") and item.get("name") not in _VOLATILE_INPUTS}

    before, after = digests(old), digests(new)
    names = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    executor = lambda record: (record.get("executor") or {}).get("sha256") if isinstance(record.get("executor"), Mapping) else None  # noqa: E731
    context = [label for label, same in (("单元规格", old.get("spec_sha256") == new.get("spec_sha256")),
                                         ("环境", old.get("environment_sha256") == new.get("environment_sha256")),
                                         ("执行器", executor(old) == executor(new)),
                                         ("调度策略", old.get("policy_sha256") == new.get("policy_sha256"))) if not same]
    return names, context


# 重跑原因里的文字线索（原文来自 unit_inherit.assess、unit_records.check_record 与执行器；拿不准的归 other，原文全部保留）。
_REALTIME_HINTS = ("输入合同未闭合", "禁止承接", "标为不可承接", "本单元不可承接", "尚未闭合：本次执行记录不得承接")
_AUDIT_HINTS = ("读集覆盖未闭合", "读集审计未通过", "完整读集轨迹")
_SOURCE_HINTS = ("记录自摘要不符", "超过承接期限", "完成时间", "原运行的清单缺失", "清单没有发布", "没有把这条记录列为", "日志不在记录库",
                 "已撤销", "宿主输入快照失败", "记录读不出来", "记录格式版本不对", "诊断执行的记录永不承接", "来源记录明确不可承接",
                 "单元 ID 或类型不符", "记录不属于来源集合登记的运行")
_CHANGED_HINTS = ("输入变了", "单元规格变了", "环境变了", "执行器变了", "调度策略版本变了")
_NO_SOURCE_HINTS = ("来源集合没有登记这个单元", "记录库里没有这个单元的记录")


def _classify_rerun(entry: Mapping[str, Any], predecessor: Mapping[str, Any] | None, old_record: Mapping[str, Any] | None,
                    new_record: Mapping[str, Any] | None, *, registered: bool) -> dict[str, Any]:
    """一个被重跑（本次执行）的单元为什么没有承接：类别、中文说明、原文与变化的输入。

    结构化证据优先于文字线索：前序清单里该单元没通过、清单项标明不可承接（未登记读集审计）、前后两条记录的输入或环境摘要不同。
    文字线索只用来认出逐单元合同给出的来源记录问题（被篡改、过期、审计未通过等）。同一个单元可以命中多个类别。"""

    reasons = [str(item) for item in entry.get("reasons") or []]
    text = "；".join(reasons)
    found: set[str] = set()
    if predecessor is not None and predecessor.get("disposition") != "not_run" and predecessor.get("passed") is not True:
        found.add("failed")
    if "该次执行没通过" in text:
        found.add("failed")
    if entry.get("inheritable") is False or any(hint in text for hint in _REALTIME_HINTS):
        found.add("unaudited_realtime")
    if any(hint in text for hint in _AUDIT_HINTS):
        found.add("audit_not_passed")
    if any(hint in text for hint in _SOURCE_HINTS):
        found.add("source_invalid")
    changed_inputs, changed_context = _input_changes(old_record, new_record)
    if changed_inputs or changed_context or any(hint in text for hint in _CHANGED_HINTS):
        found.add("inputs_changed")
    if ("逐单元承接" in text and "拒绝" in text) or "不扩大承接集合" in text:
        found.add("session_refused")
    if not found and (not registered or any(hint in text for hint in _NO_SOURCE_HINTS)):
        found.add("no_source")
    if not found:
        found.add("other")
    ordered = [name for name in RERUN_CATEGORIES if name in found]
    detail = []
    if changed_inputs:
        detail.append("变化的输入：" + "、".join(changed_inputs[:8]) + (" 等" if len(changed_inputs) > 8 else ""))
    if changed_context:
        detail.append("变化的环境因素：" + "、".join(changed_context))
    return {"reason_category": ordered[0], "reason_categories": ordered,
            "reason": "；".join([*(RERUN_CATEGORIES[name] for name in ordered), *detail]), "reasons": reasons,
            "changed_inputs": changed_inputs[:20], "changed_context": changed_context}


def _run_commit(module: Any, store: Any, run_id: str) -> tuple[str | None, str | None]:
    """尽力解析一次来源运行属于哪个提交：读它入口门禁摘要里的提交，且摘要登记的清单就是这次运行的那一份。解析不出就是（None, None）。"""

    try:
        manifest = store.manifest(run_id)
        summary = module.fs.read(Path(str(manifest["out_dir"])).parent / "entry-gates.json")
        commit = (summary.get("source") or {}).get("commit")
        if ((summary.get("unit_manifest") or {}).get("manifest_sha256") == manifest.get("manifest_sha256")
                and isinstance(commit, str) and _COMMIT_PATTERN.fullmatch(commit)):
            return commit, "entry_gates_summary"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None, None


def _inherit_registry(payload: Mapping[str, Any], *, proof: Mapping[str, Any] | None) -> dict[str, Any]:
    """从本次运行已发布的清单、承接请求与记录库现算承接登记（结论文件里的 ``inherited_units`` 等）。

    ``proof`` 是承接消费者重验通过后的消费凭证（只有 passed 的结论有）；没有时（failed 的尽力登记）``consumer_accepted`` 是 None。
    四个计数分开统计，不互相代替（一个漏斗：登记了多少、其中多少有审计过的来源记录、消费者接受了多少、实际承接了多少）：
      * registered：本次计划单元里登记为可承接的单元数（运行清单项 ``inheritable`` 为真：已登记读集审计，且没有别的阻断原因）；
      * audit_passed：其中承接请求的来源集合里有来源记录、且这条记录自身的读集审计证据成立的（见 ``_audit_evidence_ok``）；
      * consumer_accepted：承接消费者 ``verify_published`` 实际重验接受的承接项数；
      * inherited：运行清单里处置为承接的单元数。
    登记了而没有审计过的来源记录的单元（例如只有 4 个测试单元真有可用的审计记录，其余登记单元没有）因此不会被算进后三项。
    通过的结论要求 inherited＝consumer_accepted≤audit_passed≤registered，不满足抛 ``ContractError``（调用方拒绝通过）。"""

    module = _unit_inherit()
    records = module.records()
    source = payload["unit_inherit_source"]
    out = Path(str(payload["out"]))
    manifest = module.fs.read(out / "executor" / "unit-manifest.json")
    store = records.RecordStore(Path(str(payload["record_store"])))
    run_id = str(manifest.get("run_id") or "")
    if store.manifest(run_id) != manifest:
        raise module.ContractError("本次运行清单没有发布进记录库，或与发布的不是同一份")
    request_reference, request = module._read_request(Path(str(payload["unit_inherit_request"]["path"])))
    if request_reference != payload["unit_inherit_request"]:
        raise module.ContractError("承接请求在运行之后发生变化")
    sources = module.sources_check(request["sources"])
    predecessor = store.manifest(str(source["executor_run_id"]))
    if predecessor is None or predecessor.get("manifest_sha256") != source["executor_manifest_sha256"]:
        raise module.ContractError("来源后台的运行清单在记录库里缺失，或与来源登记的摘要不同")
    predecessor_rows = {str(row.get("unit_id")): row for row in predecessor.get("units") or [] if isinstance(row, Mapping)}
    rows = [row for row in manifest.get("units") or [] if isinstance(row, Mapping)]
    decision = records._read_json(store.decision_path(run_id)) if manifest.get("mode") == module.MODE else None
    commits: dict[str, tuple[str | None, str | None]] = {}

    def commit_of(source_run_id: str) -> tuple[str | None, str | None]:
        if source_run_id not in commits:
            commits[source_run_id] = ((str(source["commit"]), "predecessor_result") if source_run_id == source["executor_run_id"]
                                      else _run_commit(module, store, source_run_id))
        return commits[source_run_id]

    inherited_units: list[dict[str, Any]] = []
    for row in sorted((row for row in rows if row.get("disposition") == "inherited"), key=lambda row: str(row.get("unit_id"))):
        unit_id = str(row["unit_id"])
        basis = row.get("basis") if isinstance(row.get("basis"), Mapping) else {}
        origin = {"run_id": str(basis.get("run_id")), "record_sha256": str(row.get("record_sha256"))}
        if manifest.get("mode") != module.MODE or sources["units"].get(unit_id) != origin:
            raise module.ContractError(f"{unit_id}：承接项的来源与承接请求登记的来源不一致")
        if decision is not None and ((decision.get("units") or {}).get(unit_id) or {}).get("source") != origin:
            raise module.ContractError(f"{unit_id}：承接项的来源与决策收据登记的来源不一致")
        commit, basis_of_commit = commit_of(origin["run_id"])
        record = records._read_json(store.record_path(unit_id, origin["record_sha256"]))
        inherited_units.append({
            "unit_id": unit_id, "source_run_id": origin["run_id"], "source_record_sha256": origin["record_sha256"],
            "source_commit": commit, "source_commit_basis": basis_of_commit,
            "source_origin": "predecessor_run" if origin["run_id"] == source["executor_run_id"] else "earlier_run",
            "source_completed_at_utc": basis.get("completed_at_utc"),
            "source_seconds": (record or {}).get("seconds"),
        })
    reruns: list[dict[str, Any]] = []
    not_run: list[str] = []
    for row in sorted(rows, key=lambda row: str(row.get("unit_id"))):
        if row.get("disposition") == "inherited":
            continue
        unit_id = str(row["unit_id"])
        if row.get("disposition") != "executed":
            not_run.append(unit_id)
            continue
        before = predecessor_rows.get(unit_id)
        old_record = records._read_json(store.record_path(unit_id, str(before["record_sha256"]))) if before and before.get("record_sha256") else None
        new_record = records._read_json(store.record_path(unit_id, str(row["record_sha256"]))) if row.get("record_sha256") else None
        before_state = ("absent" if before is None else "not_run" if before.get("disposition") == "not_run"
                        else "passed" if before.get("passed") is True else "failed")
        reruns.append({
            "unit_id": unit_id, **_classify_rerun(row, before, old_record, new_record, registered=unit_id in sources["units"]),
            "predecessor_status": before_state,
            "seconds": (new_record or {}).get("seconds"), "record_sha256": row.get("record_sha256"), "passed": row.get("passed"),
            "audit_passed": _audit_evidence_ok(records, new_record, str(row.get("record_sha256"))),
        })
    eligible = {str(row["unit_id"]) for row in rows if row.get("inheritable") is True}
    registered = len(eligible)
    audit_passed = sum(1 for unit_id, ref in sources["units"].items() if unit_id in eligible
                       and _audit_evidence_ok(records, records._read_json(store.record_path(unit_id, ref["record_sha256"])), ref["record_sha256"]))
    inherited = len(inherited_units)
    consumer_accepted = int(proof["inherited"]) if proof is not None else None
    if proof is not None and (consumer_accepted != inherited or not inherited <= audit_passed <= registered
                              or proof.get("run_id") != run_id or proof.get("executed") != len(reruns)):
        raise module.ContractError(f"承接计数与消费凭证不一致：登记 {registered}、审计通过 {audit_passed}、消费者接受 {consumer_accepted}、"
                                   f"实际承接 {inherited}（要求 实际承接＝消费者接受≤审计通过≤登记）")
    from_predecessor = sum(1 for item in inherited_units if item["source_origin"] == "predecessor_run")
    executed_audit_passed = sum(1 for item in reruns if item["audit_passed"])
    return {
        "inherited_units": inherited_units,
        "inherit_counts": {"registered": registered, "audit_passed": audit_passed, "consumer_accepted": consumer_accepted,
                           "inherited": inherited},
        "rerun_units": reruns,
        "inherit_summary": {
            "schema": INHERIT_REPORT_SCHEMA, "run_id": run_id, "predecessor_run_id": source["executor_run_id"],
            "predecessor_commit": source["commit"], "predecessor_result": source["result"]["path"],
            "predecessor_ancestry": (source.get("ancestry") or {}).get("status"), "planned": len(rows),
            "request_source_units": len(sources["units"]), "executed": len(reruns), "inherited": inherited, "not_run": not_run,
            "rerun_failed_units": [item["unit_id"] for item in reruns if item["passed"] is not True],
            "executed_audit_passed": executed_audit_passed, "audit_ready_after_run": inherited + executed_audit_passed,
            "inherited_from_predecessor_run": from_predecessor, "inherited_from_earlier_runs": inherited - from_predecessor,
            "rerun_seconds": round(sum(float(item["seconds"]) for item in reruns if isinstance(item["seconds"], (int, float))), 3),
            "inherited_source_seconds": round(sum(float(item["source_seconds"]) for item in inherited_units
                                                 if isinstance(item["source_seconds"], (int, float))), 3),
            "outcome": "inherited" if inherited else "full_rerun",
            "note": ("" if inherited else "没有任何单元被承接：本次实际是整套重新执行，不是承接结论（原因见 rerun_units）"),
            "definitions": INHERIT_DEFINITIONS,
        },
    }


def _register_inheritance(payload: Mapping[str, Any], status: str, reason: str | None) -> tuple[dict[str, Any], str, str | None]:
    """结论落盘前登记承接：通过的结论登记失败就不能通过（中止并写明原因）；失败／中止的结论尽力登记，登记不出来只记错误。"""

    proof = (payload.get("unit_inherit_consumption") or {}).get("proof") if status == "passed" else None
    try:
        return {**payload, **_inherit_registry(payload, proof=proof)}, status, reason
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        if status == "passed":
            return dict(payload), "aborted", f"后台承接登记不通过：{error}"
        return {**payload, "inherit_report_error": str(error)}, status, reason


def _verify_inherit_registry(payload: Mapping[str, Any], completion: Mapping[str, Any]) -> None:
    """批次边界重放：结论里登记的承接项、四个计数和重跑类别必须与清单、决策、请求、记录库现算的一致。"""

    fresh = _inherit_registry(payload, proof=completion["proof"])
    module = _unit_inherit()
    triples = lambda items: [(item.get("unit_id"), item.get("source_run_id"), item.get("source_record_sha256")) for item in items or []]  # noqa: E731
    categories = lambda items: [(item.get("unit_id"), item.get("reason_category")) for item in items or []]  # noqa: E731
    # source_commit 来自可能已被清理的入口门禁目录，重放时不比；其余登记逐项比。
    if (triples(payload.get("inherited_units")) != triples(fresh["inherited_units"])
            or payload.get("inherit_counts") != fresh["inherit_counts"]
            or categories(payload.get("rerun_units")) != categories(fresh["rerun_units"])):
        raise module.ContractError("结论里登记的承接项、四个计数或重跑原因与清单、决策、请求现算的不一致")


def _start_locked(args: argparse.Namespace, *, deployments: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    client = getattr(args, "client", None) or "codex"
    parallel = getattr(args, "parallel", None)
    if parallel is not None and (type(parallel) is not int or parallel <= 0):
        raise ValidationError("--parallel 必须是正整数")
    # 声明级承接的采样方式（步骤 5 WP3b）：缺省 off；只有显式非 off 才登记进输入合同与结论（缺省的合同、结论与命令行与改造前逐字相同）。
    # 只有「没给」（属性不存在或为 None）才是缺省；空字符串是非法取值（入口脚本的正则同样拒绝），不能被 ``or`` 悄悄当成 off。
    requested = getattr(args, "declared_sample", None)
    try:
        sample = _declared_sample_mode("off" if requested is None else requested)
    except argparse.ArgumentTypeError as error:
        raise ValidationError(str(error)) from error
    deployment = latest_deployment(args.data_root, client)
    if deployments is not None and deployments.get(client) != deployment:
        raise ValidationError(f"部署收据集合里没有 {client} 当前的部署收据")
    path = result_path(args.runroot, args.commit, deployment, deployments)
    # 后台编译缓存不再依赖操作者的 GOCACHE，也不与前台准入清理共用工作目录。
    bound_roots = _bound_work_roots(args.runroot, args.data_root, args.commit, deployment, deployments)
    # 跨提交、跨部署后继：物理 WORK 沿用来源后台的那一个（测试树与缓存不重建，与入口线的原位后继同口径），它绑定的是来源自己的
    # 提交与部署，所以校验放宽到「本轮坐标或来源坐标」；是否恰好等于来源的 WORK 由随后的来源校验逐字核对。
    source_option = getattr(args, "unit_inherit_source_result", None)
    source_loaded: tuple[dict[str, Any], str] | None = None
    source_info: dict[str, Any] | None = None
    if getattr(args, "allow_unverified_ancestry", False) and source_option is None:
        raise ValidationError("--allow-unverified-ancestry 只与 --unit-inherit-source-result 同用")
    if source_option is not None:
        if getattr(args, "unit_inherit_request", None) is None:
            raise ValidationError("--unit-inherit-source-result 只与 --unit-inherit-request 同用")
        source_loaded = _inherit_source_load(args, Path(source_option))
        try:
            bound_roots = (*bound_roots, *_work_lineage(Path(args.runroot), Path(args.data_root), source_loaded[0]))
        except (KeyError, TypeError, AttributeError) as error:
            raise ValidationError(f"来源后台结论的内容不合法：{error!r}") from error
    work = args.work or bound_roots[0]
    work = Path(work)
    previous_attempt = _read(path) if source_option is None and args.work else None
    if (previous_attempt is not None and previous_attempt.get("unit_inherit_source") is not None
            and not any(work.is_relative_to(root) for root in bound_roots)):
        # 本提交与部署的上一次尝试是跨提交后继，它的物理 WORK 绑定的是来源的坐标；重试时同样要带来源选项，否则这里认不出那个 WORK。
        raise ValidationError("本提交与部署的上一次尝试是跨提交后继（物理 WORK 沿用来源后台的）：重试须带同一个 --unit-inherit-source-result")
    if (not work.is_absolute() or ".." in work.parts or not any(work.is_relative_to(root) for root in bound_roots)
            or any(item.is_symlink() for item in (work, *work.parents))):
        raise ValidationError("后台工作目录必须绑定本轮完整提交和部署摘要，不得共用其它轮次目录")
    if Path(args.bundle).is_symlink() or not Path(args.bundle).is_file():
        raise ValidationError("后台验证 bundle 必须是普通文件")
    inputs = {"execution_contract": "b09-reexecute-full/v1", "bundle_sha256": _sha256_file(args.bundle), "branch": args.branch, "profile": args.profile,
              "work": str(work), "entry_gates_sha256": _sha256_file(args.entry_gates),
              "record_store": str(args.record_store) if args.record_store else None,
              "vc_env_sha256": _sha256_file(args.vc_env) if args.vc_env else None, "then_dryrun": bool(args.then_dryrun)}
    # 只有显式指定才登记；缺省仍由既有执行器配置决定，不改变旧输入合同的字段形状。
    if parallel is not None:
        inputs["parallel"] = parallel
    if sample != "off":
        inputs["declared_sample"] = sample   # 同 parallel：显式指定才登记，换了取值就是另一份输入合同（须先显式停止并重新验证）
    full_set_paths = {key: str(getattr(args, key)) if getattr(args, key, None) else None
                      for key in ("full_set_context", "full_set_clock", "full_set_predecessor", "audit_policy")}
    if bool(full_set_paths["full_set_context"]) != bool(full_set_paths["full_set_clock"]):
        raise ValidationError("B-09 签发须同时提供当前上下文和校时凭证")
    inputs["full_set_files"] = {key: _sha256_file(Path(value)) if value else None for key, value in full_set_paths.items()}
    # 按客户端筛选（优化第 9 项）：只有显式给 client-plan 才登记进输入合同与结论；缺省的合同、结论与命令行与改造前逐字相同。
    gate_scope = getattr(args, "gate_scope", None) or GATE_SCOPE_FULL
    select_base = getattr(args, "select_base", None)
    include_claude = bool(getattr(args, "include_claude", False))
    if gate_scope not in (GATE_SCOPE_FULL, GATE_SCOPE_CLIENT_PLAN):
        raise ValidationError(f"未知的门禁范围：{gate_scope}")
    if gate_scope == GATE_SCOPE_FULL:
        if select_base or include_claude:
            raise ValidationError("--select-base、--include-claude 只在 --gate-scope client-plan 下有意义")
    else:
        if client != "codex":
            raise ValidationError("按客户端筛选本版只启用 codex 计划")
        if not (isinstance(select_base, str) and _COMMIT_PATTERN.fullmatch(select_base)):
            raise ValidationError("--gate-scope client-plan 必须同时给 --select-base（最近一次发版的 40 位小写提交；工具不自动猜）")
        if (args.then_dryrun or getattr(args, "unit_inherit_request", None) is not None or getattr(args, "rerun_full_set", False)
                or any(full_set_paths.values())):
            raise ValidationError("按客户端筛选的验证是子集，不能接入口空跑（空跑要全集结论，加 --no-dryrun），"
                                  "也不能与 B-09 全集收据、读集合同、逐单元承接同用")
        inputs.update(gate_scope=gate_scope, select_base=select_base, select_client=client)
        if include_claude:
            inputs["include_claude"] = True
    inherit_request = getattr(args, "unit_inherit_request", None)
    inherit_reference = None
    if inherit_request is not None:
        if (args.profile != DEFAULT_PROFILE or not args.record_store or not args.work or not args.audit_policy
                or getattr(args, "rerun_full_set", False)
                or any(full_set_paths[key] for key in ("full_set_context", "full_set_clock", "full_set_predecessor"))):
            raise ValidationError("后台逐单元续作须显式提供原 WORK、记录库及策略，仅限 full-gates，不能与 B-09 参数同用")
        try:
            inherit_reference = _inherit_reference(Path(inherit_request), vars(args))
        except _unit_inherit().ContractError as error:
            if source_loaded is None:
                raise   # 同提交、同部署的原续作路径：异常类型与改造前一致
            raise ValidationError(f"后台跨提交承接的请求或批准不成立：{error}") from error
        inputs.update(execution_contract="unit-inherit/v1", unit_inherit_request=inherit_reference)
        if source_loaded is not None:
            try:
                source_info = _inherit_source_check(args, Path(source_option), source_loaded, client=client, work=work, inputs=inputs,
                                                    result_path=path)
            except (KeyError, TypeError, AttributeError) as error:
                raise ValidationError(f"来源后台结论的内容不合法：{error!r}") from error
            source_info["request_units_from_source"] = _request_units_from(Path(inherit_request), source_info["executor_run_id"])
            inputs["unit_inherit_source"] = _inherit_source_contract(source_info)
    existing = _read(path)
    registration = registry_path(args.data_root, args.commit, deployments) if deployments is not None else None
    if registration is not None and (existing is None or effective_status(existing) not in ("running", "passed", "failed")):
        # 本运行根没有这次验证的结论：看另一客户端的轮次（另一运行根）是不是已经起了同一提交与集合的那一次。
        entry = _read_kind(registration, REGISTRY_SCHEMA)
        target = Path(str((entry or {}).get("result") or ""))
        other = _read(target) if entry is not None and target.is_absolute() and target != path else None
        if other is not None and other.get("commit") == args.commit and other.get("deployments") == deployments:
            path, existing = target, other
    rerun = bool(getattr(args, "rerun_full_set", False))
    # 带来源选项的跨提交后继：本提交、本部署还没有结论时，原结论就是来源（已在上面校验）；已有同提交同部署的上一次后继尝试时，
    # 仍按下面原有规则相对它续作（客户端、提交、部署、WORK、记录库、门禁与调度参数逐项不变）。
    if inherit_reference is not None and (existing is not None or source_info is None):
        if existing is None:
            raise ValidationError("后台逐单元续作缺少同提交、同部署的原后台结论")
        unchanged = ("bundle_sha256", "branch", "profile", "work", "entry_gates_sha256", "record_store", "parallel")
        if ((existing.get("client") or "codex") != client or existing.get("commit") != args.commit
                or existing.get("deployment") != deployment or existing.get("deployments") != (dict(deployments) if deployments else None)
                or any((existing.get("input_contract") or {}).get(key) != inputs.get(key) for key in unchanged)):
            raise ValidationError("后台逐单元续作必须保留原客户端、提交、部署、WORK、记录库、门禁及调度参数")
        if (existing.get("input_contract") or {}).get("declared_sample") != inputs.get("declared_sample"):
            # 采样方式也是门禁参数：续作时悄悄换成缺省的 off（或反过来），读集证据的来源就变了，必须显式保持一致。
            raise ValidationError("后台逐单元续作必须保留原采样方式（--declared-sample）")
        if effective_status(existing) not in ("running", "passed"):
            if _group_alive(existing.get("pid")):
                raise ValidationError("原后台进程组尚未退出，拒绝在同 WORK 启动后继")
            rerun = True
    if inherit_reference is not None and existing is not None and existing.get("full_set_receipt"):
        raise ValidationError("已有 B-09 全集收据不能转换成后台逐单元结论")
    if existing is not None and existing.get("full_set_receipt", {}).get("status") == "issued":
        # 到期后必须新执行，不能被“同部署已有 passed”去重挡住；坏来源也先重跑。
        try:
            old_ref = existing["full_set_receipt"]["receipt"]
            old_receipt = json.loads(Path(old_ref["path"]).read_text())
            expires = datetime.fromisoformat(old_receipt["expires_at_utc"].replace("Z", "+00:00")).timestamp()
            rerun = rerun or _sha256_file(Path(old_ref["path"])) != old_ref["sha256"] or time.time() >= expires
            if rerun and not full_set_paths["full_set_predecessor"]:
                full_set_paths["full_set_predecessor"] = old_ref["path"]
                inputs["full_set_files"]["full_set_predecessor"] = _sha256_file(Path(old_ref["path"]))
        except (OSError, ValueError, TypeError, KeyError):
            rerun = True
    if existing is not None and effective_status(existing) == "running" and rerun:
        raise ValidationError("当前全集仍在运行，禁止另起重复执行")
    if (registration is not None and existing is not None and not rerun and (existing.get("client") or "codex") != client
            and effective_status(existing) in ("running", "passed", "failed")):
        # B5a-10：另一客户端发起的同一次验证（同一提交、同一组部署收据）。只比与客户端无关的输入合同，相同即搭上它。
        if _shared_contract(existing.get("input_contract")) != _shared_contract(inputs) or existing.get("commit") != args.commit:
            raise ValidationError(f"同一提交与部署已有另一客户端发起的后台验证（{path}），输入合同不同：须先显式停止并重新验证")
        return _attach(args, registration, path, existing, client)
    if existing is not None and not rerun and effective_status(existing) in ("running", "passed", "failed"):
        if existing.get("input_contract") != inputs or existing.get("commit") != args.commit or existing.get("deployment") != deployment:
            raise ValidationError("后台验证已有记录的输入或缓存隔离合同不同，须先显式停止并重新验证")
        return {"action": "exists", "result": str(path), "status": effective_status(existing)}
    if registration is not None and path.parent != Path(args.runroot) / DIRECTORY:
        # 登记里同一次验证在另一运行根、但没法搭（要重跑全集）：本运行根新起一次，登记改指向新的这一份。
        path = result_path(args.runroot, args.commit, deployment, deployments)
        existing = _read(path)
    # 同树重部署沿用（优化第 5 项）：auto 先试沿用，拒绝（任一绑定项变了、过有效期、核对失败）就照常起完整验证；only 只试沿用。
    reuse_mode, reuse_refused = getattr(args, "reuse", "off"), []
    if gate_scope == GATE_SCOPE_CLIENT_PLAN:
        # 子集结论不沿用，也不拿全集来源冒充本次的筛选验证：auto 直接跳过沿用，only 没有意义。
        if reuse_mode == "only":
            raise ValidationError("按客户端筛选的验证不沿用来源结论（--reuse only 不适用）")
        reuse_mode = "off"
    if reuse_mode in ("auto", "only"):
        reused_result, reuse_refused = _attempt_reuse(args, client=client, deployment=deployment, deployments=deployments, path=path,
                                                      inputs=inputs, work=work)
        if reused_result is not None:
            if reuse_mode == "auto":
                # 驱动的修复轮（fix_and_continue 的 background-verdict）只认 started／exists：自动沿用成功就是“当前部署已有通过的结论”，按 exists 报告并带 reused 标记。
                return {**reused_result, "action": "exists", "status": "passed", "reused": True}
            return reused_result
        if reuse_mode == "only":
            return {"action": "refused", "reasons": reuse_refused}
    # 起跑前置检查（优化第 4 项）：放在停掉其它验证之前——拒绝起跑不能连累已在跑的那一轮。
    _preflight_gate(args, client=client, deployment=deployment, deployments=deployments)
    stop_notes: list[str] = []
    superseded = stop(args.runroot, f"被提交 {args.commit[:12]} 在部署 {deployment['receipt']} 上的后台验证取代", keep=path,
                      notes=stop_notes)
    if registration is not None:
        superseded += stop_registered(args.data_root, f"被提交 {args.commit[:12]} 在部署集合 {registration.stem} 上的后台验证取代",
                                      keep=path, notes=stop_notes)
    out = path.parent / "runs" / f"{path.stem}-{time.time_ns()}"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "commit": args.commit, "branch": args.branch, "bundle": str(args.bundle),
        "deployment": deployment, "profile": args.profile, "mode": "re-execute", "status": "running", "pid": None,
        "started_at_utc": _utc_now(), "completed_at_utc": None, "out": str(out), "log": f"{out}.log",
        "record_store": str(args.record_store) if args.record_store else None, "work": str(work),
        "input_contract": inputs, "cache_roots": {"GOCACHE": str(work / "go-build-cache"),
            "GOMODCACHE": str(work / "go-mod-cache"), "GOTMPDIR": str(work / "go-tmp")},
        "vc_env": str(args.vc_env) if args.vc_env else None, "entry_gates": str(args.entry_gates),
        "data_root": str(args.data_root), "runroot": str(args.runroot), "then_dryrun": bool(args.then_dryrun),
        "failed_gates": [], "reason": None, "returncode": None, "dryrun": None,
        **({"parallel": parallel} if parallel is not None else {}),
        **({"declared_sample": sample} if sample != "off" else {}),
        **full_set_paths,
        # B5a-01：只有非 Codex 的结论记录客户端，Codex 结论的字段逐字不变（读取时缺省按 codex）。
        **({"client": client} if client != "codex" else {}),
        # B5a-10：两客户端都纳管时绑定部署收据集合（只有一个客户端纳管时不写，结论与改造前同形）。
        **({"deployments": dict(deployments)} if deployments is not None else {}),
    }
    if gate_scope == GATE_SCOPE_CLIENT_PLAN:
        payload.update(gate_scope=gate_scope, select_base=select_base, select_client=client, **({"include_claude": True} if include_claude else {}))
    if inherit_reference is not None:
        payload.update(mode="unit-inherit", unit_inherit_request=inherit_reference)
    if existing is not None:
        # 当前状态文件可更新；每次重新开始前保留上一执行的完整终态，便于复核失败恢复。
        saved = json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        history = path.parent / "history" / (hashlib.sha256(saved.encode()).hexdigest() + ".json")
        if not history.exists():
            _write(history, existing)
        if inherit_reference is not None:
            payload["predecessor"] = {"path": str(history), "sha256": _sha256_file(history), "status": existing["status"]}
    if source_info is not None:
        # 来源结论的冻结副本（内容寻址存进 history，与上面保存旧终态同一做法）：之后来源文件即使被同坐标的重跑改写，运行期与
        # 批次边界仍按启动时校验过的这一份核对。没有同提交同部署的上一次尝试时，来源也是这次后继的直接前序。
        frozen_text = json.dumps(source_loaded[0], ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        frozen = path.parent / "history" / (hashlib.sha256(frozen_text.encode()).hexdigest() + ".json")
        if not frozen.exists():
            _write(frozen, source_loaded[0])
        source_info = {**source_info, "frozen": {"path": str(frozen), "sha256": _sha256_file(frozen)}}
        payload["unit_inherit_source"] = source_info
        if existing is None:
            payload["predecessor"] = {"path": str(frozen), "sha256": source_info["frozen"]["sha256"], "status": source_info["status"]}
    _write(path, payload)
    if registration is not None:
        _write(registration, {"schema_version": REGISTRY_SCHEMA, "commit": args.commit, "deployments": dict(deployments),
                              "result": str(path), "client": client, "runroot": str(args.runroot), "registered_at_utc": _utc_now()})
    argv = ["nice", "-n", str(NICENESS), sys.executable, "-B", str(Path(__file__).resolve()), "run", "--result", str(path)]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **({"ARM64_VC_ENV": str(args.vc_env)} if args.vc_env else {})}
    log = Path(payload["log"])
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log.open("ab") as handle:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, env=environment,
                                   start_new_session=True)
    current = _read(path) or payload
    if current.get("status") == "running" and current.get("pid") is None:
        # 同时记下 run 进程的启动标识：之后 stop 动手前据此核对这个 pid 还是不是它（后台被 SIGKILL 或机器重启而结论仍是 running 时，
        # pid 可能被无关进程复用，没有这个标识 stop 就不敢对整棵子树动手，见 ``_run_mode``）。读不到就不写，stop 退回按进程组处理。
        identity = process_identity(process.pid)
        _write(path, {**current, "pid": process.pid, **({"pid_start": identity} if identity else {})})
    started = {"action": "started", "result": str(path), "pid": process.pid, "out": str(out), "superseded": superseded}
    if reuse_refused:
        started["reuse_refused"] = reuse_refused   # auto：沿用被拒绝的原因（已照常起了完整验证）
    if stop_notes:
        started["stop_notes"] = stop_notes
    if source_info is not None:
        started["unit_inherit_source"] = {"commit": source_info["commit"], "executor_run_id": source_info["executor_run_id"],
                                          "status": source_info["status"], "ancestry": source_info["ancestry"]["status"],
                                          "request_units_from_source": source_info["request_units_from_source"]}
        if not source_info["request_units_from_source"]:
            started["unit_inherit_source"]["warning"] = ("承接请求没有登记任何指向来源后台那一轮执行记录的单元：来源后台对这次承接没有贡献，"
                                                         "请核对请求与来源是否配套")
    return started


def _entry_summary(out: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((Path(out) / "entry-gates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _retry_policy() -> Any:
    """同目录的 retry_policy.py（失败单元补跑策略，优化第 2 项）：按路径加载，纯标准库。"""

    name = "retry_policy_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "retry_policy.py")
        if spec is None or spec.loader is None:
            raise ValidationError(f"找不到同目录的 retry_policy.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def retry_report(out: Path | str) -> dict[str, Any]:
    """后台验证一次运行里的补跑情况（优化第 2 项）：读入口门禁总摘要指向的执行器汇总，自检补跑标注（含两次日志在盘且摘要相符）。

    返回 ``{"enabled", "passed_units", "failed_units", "problems"}``。``passed_units`` 每项带单元、首次失败日志、补跑日志与累计窗口，
    结论文件据此单列；``problems`` 非空时调用方不得把结论标成通过（标注被改写、被藏起来、日志缺失都算）。补跑关闭或没有补跑的运行
    ``passed_units`` 为空，不产生任何问题。"""

    entry = _entry_summary(Path(out))
    summary = None
    if entry is None or not entry.get("executor_summary"):
        # 入口门禁总摘要没有指向执行器汇总（不是真实导出的总摘要，如测试替身）：没有补跑信息可核，行为与改造前相同。
        return {"enabled": False, "passed_units": [], "failed_units": [], "problems": []}
    try:
        summary = json.loads(Path(str(entry["executor_summary"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if not isinstance(summary, dict):
        return {"enabled": False, "passed_units": [], "failed_units": [], "problems": ["读不到执行器汇总，无法核对补跑标注"]}
    policy = _retry_policy()
    problems = policy.summary_problems(summary, require_files=True)
    block = summary.get("retry") if isinstance(summary.get("retry"), dict) else {}
    units = block.get("units") if isinstance(block.get("units"), dict) else {}
    passed = [{"unit_id": unit_id, "first_attempt_log": units.get(unit_id, {}).get("first_attempt", {}).get("log"),
               "retry_attempt_log": (units.get(unit_id, {}).get("retry_attempt") or {}).get("log"),
               "window_rounds": block.get("window_rounds"), "pass_limit": block.get("pass_limit")}
              for unit_id in block.get("retry_passed", [])]
    return {"enabled": bool(block.get("enabled")), "passed_units": passed, "failed_units": list(block.get("retry_failed", [])),
            "problems": problems}


def run(result: Path) -> int:
    path = Path(result)
    payload = _read(path)
    if payload is None:
        raise ValidationError(f"后台验证结论文件不可读：{path}")
    # 工作树和全部 Go 缓存使用本次后台验证的独立坐标；承接判断仍交给输入合同，不强行沿用前台记录。两客户端都纳管时
    # （B5a-10）入口门禁核对每个已纳管客户端的部署（--require-deployed-all），只有一个客户端纳管时命令行与改造前相同。
    require = "--require-deployed-all" if payload.get("deployments") else "--require-deployed"
    mode = "unit-inherit" if payload.get("unit_inherit_request") else "re-execute"
    argv = ["bash", payload["entry_gates"], "--profile", payload["profile"], "--mode", mode, require,
            "--wait-lock", str(WAIT_LOCK_SECONDS), "--out", payload["out"]]
    for flag, key in (("--record-store", "record_store"), ("--work", "work")):
        if payload.get(key):
            argv += [flag, payload[key]]
    if "parallel" in payload:
        argv += ["--parallel", str(payload["parallel"])]
    if "declared_sample" in payload:
        # 声明级承接的采样方式（步骤 5 WP3b）：原样交给入口门禁再到执行器；运行期下面还会核对它与输入合同一致，缺省（结论里没有）不追加。
        argv += ["--declared-sample", str(payload["declared_sample"])]
    if payload.get("audit_policy"):
        argv += ["--audit-reads", "--audit-strict", "--audit-policy", payload["audit_policy"]]
    if payload.get("gate_scope") == GATE_SCOPE_CLIENT_PLAN:
        # 按客户端筛选（优化第 9 项）：原样交给入口门禁；运行期下面还会核对它与输入合同一致。
        argv += ["--gate-scope", GATE_SCOPE_CLIENT_PLAN, "--select-base", str(payload.get("select_base"))]
        if payload.get("include_claude"):
            argv += ["--include-claude"]
    argv += [payload["bundle"], payload["branch"], payload["commit"]]
    received: list[int] = []

    def forward(signum: int, _frame: Any) -> None:
        received.append(signum)

    signal.signal(signal.SIGTERM, forward)
    environment = dict(os.environ)
    work = Path(payload["work"])
    expected_caches = {"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"), "GOTMPDIR": str(work / "go-tmp")}
    try:
        if (payload.get("mode") == "unit-inherit" or payload.get("unit_inherit_request")
                or payload.get("input_contract", {}).get("execution_contract") == "unit-inherit/v1"):
            if (payload.get("mode") != "unit-inherit" or payload["input_contract"].get("execution_contract") != "unit-inherit/v1"
                    or payload["input_contract"].get("unit_inherit_request") != payload.get("unit_inherit_request")
                    or _inherit_reference(Path(payload["unit_inherit_request"]["path"]), payload) != payload["unit_inherit_request"]):
                raise ValidationError("后台逐单元运行的模式、授权与输入合同不一致")
            if payload.get("unit_inherit_source") is not None:
                _inherit_source_recheck(payload)   # 跨提交后继：来源登记、冻结副本与 WORK 绑定在运行期没有漂移
            elif payload["input_contract"].get("unit_inherit_source") is not None:
                raise ValidationError("后台逐单元运行的输入合同带来源指纹，结论里却没有来源登记")
            argv[2:2] = ["--unit-inherit-request", payload["unit_inherit_request"]["path"], "--unit-inherit-stage", "pre-ledger"]
        # 运行期再核对已登记的资源值；修改结果或输入合同不能悄悄改变门禁的调度策略。
        parallel = payload.get("parallel")
        contract = payload.get("input_contract", {})
        if "parallel" in payload or "parallel" in contract:
            if (type(parallel) is not int or parallel <= 0 or type(contract.get("parallel")) is not int
                    or contract["parallel"] != parallel):
                raise ValidationError("后台并发参数缺失或发生漂移")
        if "declared_sample" in payload or "declared_sample" in contract:
            # 采样方式同并发参数：结论里的取值必须是合法的规范写法、且与输入合同逐字一致，改结论不能悄悄换读集证据的来源。
            sample = payload.get("declared_sample")
            try:
                canonical = _declared_sample_mode(sample) if type(sample) is str else None
            except argparse.ArgumentTypeError:
                canonical = None
            if canonical is None or canonical == "off" or canonical != sample or contract.get("declared_sample") != sample:
                raise ValidationError("后台采样方式参数缺失或发生漂移")
        if "gate_scope" in payload or "gate_scope" in contract:
            # 筛选参数同并发参数：结论里的取值必须与输入合同逐字一致，改结论不能悄悄换成别的基点或范围。
            if (payload.get("gate_scope") != GATE_SCOPE_CLIENT_PLAN or contract.get("gate_scope") != GATE_SCOPE_CLIENT_PLAN
                    or not _COMMIT_PATTERN.fullmatch(str(payload.get("select_base"))) or contract.get("select_base") != payload.get("select_base")
                    or contract.get("select_client") != payload.get("select_client")
                    or bool(contract.get("include_claude")) != bool(payload.get("include_claude"))):
                raise ValidationError("后台筛选参数缺失或发生漂移")
        bound_roots = _bound_work_roots(Path(payload["runroot"]), Path(payload["data_root"]), payload["commit"],
                                       payload["deployment"], payload.get("deployments"))
        # 跨提交后继沿用来源的物理 WORK：它绑定的是来源自己的提交与部署（来源登记在启动时已校验、上面刚复核过）。
        bound_roots = (*bound_roots, *_inherit_source_work_roots(payload))
        if (payload.get("cache_roots") != expected_caches or payload.get("input_contract", {}).get("work") != str(work)
                or not work.is_absolute() or not any(work.is_relative_to(root) for root in bound_roots) or ".." in work.parts):
            raise ValidationError("后台缓存合同缺失或发生漂移")
        for value in expected_caches.values():
            directory = Path(value)
            if any(item.is_symlink() for item in (directory, *directory.parents)):
                raise ValidationError("后台缓存目录不得含符号链接")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        _write(path, {**payload, "status": "aborted", "completed_at_utc": _utc_now(), "reason": str(error), "returncode": 3})
        return 3
    environment.update(expected_caches)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, env=environment)
    while True:
        try:
            returncode = process.wait()
            break
        except InterruptedError:   # pragma: no cover - PEP 475 之后 wait 自动重试，这里只是保险
            continue
    current = _read(path) or payload
    if current.get("status") != "running":
        return 0   # 已被 stop 标为 superseded
    summary = _entry_summary(Path(payload["out"]))
    bound = str(((payload.get("deployment") or {}).get("receipt")) or "")
    checked = Path(str(((summary or {}).get("source") or {}).get("deploy_receipt") or "")).name
    failed = [str(gate.get("gate_id")) for gate in (summary or {}).get("gates") or [] if gate.get("status") != "passed"]
    try:
        deployment_matches = latest_deployment(Path(payload["data_root"]), payload.get("client") or "codex") == payload["deployment"]
        if payload.get("deployments"):
            deployment_matches = deployment_matches and deployment_set(Path(payload["data_root"])) == payload["deployments"]
    except (ValidationError, OSError, ValueError):
        deployment_matches = False
    others_problem = _others_checked(payload, summary) if payload.get("deployments") and summary is not None else None
    if received:
        status, reason = "aborted", f"收到信号 {received[0]}"
    elif summary is None:
        status, reason = "aborted", f"入口门禁没有结论（退出码 {returncode}），日志 {payload.get('log')}"
    elif checked != bound or not deployment_matches:
        status, reason = "aborted", f"入口门禁核对的部署收据（{checked or '没有核对'}）不是开始时绑定的 {bound}：部署在验证途中换了"
    elif others_problem:
        status, reason = "aborted", others_problem
    elif returncode == 0 and not failed and summary.get("status") == "passed":
        status, reason = "passed", None
    else:
        status, reason = "failed", f"入口门禁退出码 {returncode}"
    echoed = (summary or {}).get("client_selection")
    if isinstance(echoed, Mapping):
        # 按客户端筛选（优化第 9 项）：入口门禁总摘要回显的筛选摘要登记进结论——这份结论是哪个客户端计划的子集，防串用据此判定。
        current = {**current, "selection": dict(echoed)}
    if status == "passed" and payload.get("gate_scope") == GATE_SCOPE_CLIENT_PLAN and not isinstance(echoed, Mapping):
        status, reason = "failed", "登记了按客户端筛选，入口门禁总摘要却没有筛选回显：筛选没有生效，这不是可信的子集结论"
    if status == "passed":
        # 失败单元补跑（优化第 2 项）：补跑通过的单元算入结论，但必须单列在结论里；标注自检不通过（被改写、被藏起来、日志缺失）
        # 就不是通过——结论不静默放行。补跑关闭或没有补跑时结论与改造前逐字相同。
        retried = retry_report(payload["out"])
        if retried["problems"]:
            status, reason = "failed", "补跑标注自检不通过：" + "；".join(retried["problems"][:5])
        elif retried["passed_units"]:
            current = {**current, "retry_passed_units": retried["passed_units"]}
    if status == "passed" and not current.get("unit_inherit_request") and not current.get("audit_policy"):
        # 同树重部署沿用（优化第 5 项）：通过的全集重新执行结论带上沿用绑定（取执行器写的 binding.json）；拿不到绑定就不带，之后只能重跑，不会误沿用。
        binding_doc = executor_binding(payload["out"])
        if binding_doc is not None:
            current = {**current, "reuse_binding": build_reuse_binding(current, binding_doc, completed_at_utc=_utc_now())}
    if (current.get("selection") or {}).get("gate_scope") == GATE_SCOPE_CLIENT_PLAN and "reuse_binding" in current:
        current = {key: value for key, value in current.items() if key != "reuse_binding"}   # 子集结论不沿用
    deviation = core_deviation_info(payload["out"], path.with_name(f"{path.stem}.core-deviation.json"))
    if deviation is not None:
        current = {**current, "core_deviation": deviation}   # 核数偏差报告（优化第 6 项）：放进结论目录并登记，不论通过与否
    if status == "passed" and current.get("unit_inherit_request"):
        try:
            current = {**current, "unit_inherit_consumption": _inherit_completion(current)}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            status, reason = "aborted", f"后台逐单元消费不通过：{error}"
    if current.get("unit_inherit_source") is not None:
        # 跨提交后继：登记每条承接项的来源与四个分开的计数、每个重跑单元的原因与耗时；通过的结论登记不出来就不能通过。
        current, status, reason = _register_inheritance(current, status, reason)
    _write(path, {**current, "status": status, "completed_at_utc": _utc_now(), "reason": reason, "returncode": returncode,
                  "failed_gates": failed})
    if status == "passed" and current.get("full_set_context"):
        # 功能通过与可承接签发分开；覆盖不完整时保留通过结论，明确拒签，不生成假绿收据。
        issued = _issue_full_set(current)
        _write(path, {**(_read(path) or current), "full_set_receipt": issued})
    if status == "passed" and current.get("then_dryrun"):
        dryrun = _chain_dryrun(current)
        _write(path, {**(_read(path) or current), "dryrun": dryrun})
    if status == "passed" and current.get("deployments"):
        # B5a-10：搭这次验证的其它客户端的入口空跑一并接上（各用自己的参数文件与运行根；同一份空跑已有就不重复）。
        chained = {}
        for peer in peers(registry_path(Path(current["data_root"]), current["commit"], current["deployments"])):
            if peer.get("client") != (current.get("client") or "codex"):
                chained[str(peer.get("client"))] = _chain_dryrun({**current, "vc_env": peer.get("vc_env"), "runroot": peer.get("runroot"),
                                                                  "data_root": peer.get("data_root")})
        if chained:
            _write(path, {**(_read(path) or current), "peer_dryruns": chained})
    return 0


def _others_checked(payload: Mapping[str, Any], summary: Mapping[str, Any]) -> str | None:
    """两客户端都纳管时，入口门禁必须逐一核对过本轮客户端之外每个绑定的部署收据（总摘要来源 deploy_receipts_others）。"""

    own = payload.get("client") or "codex"
    expected = {client: str(item.get("receipt")) for client, item in (payload.get("deployments") or {}).items() if client != own}
    raw = str(((summary.get("source") or {}).get("deploy_receipts_others")) or "")
    checked = dict(item.split("=", 1) for item in raw.split() if "=" in item)
    if checked != expected:
        return f"入口门禁没有逐一核对其余客户端绑定的部署收据（核对了 {checked or '没有'}，绑定的是 {expected}）"
    return None


def _issue_full_set(payload: Mapping[str, Any]) -> dict[str, Any]:
    if GATE_SCOPE_CLIENT_PLAN in (payload.get("gate_scope"), (payload.get("selection") or {}).get("gate_scope")):
        # B-09 的全集收据不能由子集签出（按客户端筛选的运行只是某个客户端计划的子集）。
        return {"status": "refused", "reason": "按客户端筛选的运行是子集，不能签发全集收据", "reuse_enabled": False}
    spec = importlib.util.spec_from_file_location("background_full_set", HERE / "full_set_receipt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        for key in ("full_set_context", "full_set_predecessor", "audit_policy"):
            if payload.get(key) and _sha256_file(Path(payload[key])) != payload["input_contract"]["full_set_files"][key]:
                raise ValidationError("B-09 签发的静态上下文在后台运行期间变化")
        out = Path(payload["out"])
        summary = _entry_summary(out)
        source = {"entry": module.reference(out / "entry-gates.json"), "summary": module.reference(summary["executor_summary"]),
                  "manifest": module.reference(summary["unit_manifest"]["path"]), "store": summary["record_store"]}
        context = module.read(payload["full_set_context"])
        receipt = module.issue(source, context, module.reference(payload["full_set_clock"]),
            predecessor=module.reference(payload["full_set_predecessor"]) if payload.get("full_set_predecessor") else None)
        output = out / "full-set-receipt.json"
        with output.open("x", encoding="utf-8") as handle:
            os.chmod(output, 0o600)
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return {"status": "issued", "receipt": module.reference(output), "reuse_enabled": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
        return {"status": "refused", "reason": str(error), "reuse_enabled": False}


# 通过之后接着空跑入口（E4-02）只到 pre-A3：便宜检查、策略认证、入口门禁与 pre-A3 场景都受统一调度（采集预约会让它停派）；
# 零请求 smoke、atomic-double 与建账本之后的步骤要用采集容器，只在明确的空跑（entry-dryrun.sh）与升级开工的空跑里跑。
CHAINED_DRYRUN_TO = "pre-a3"


def _chain_dryrun(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("audit_policy"):
        # 后台的同次全集来源不是当前逐单元批准，不能暗中补造批准或丢掉策略再自动执行一遍全集。
        return {"action": "skipped", "reason": "后台已绑定读集策略；请显式 entry-dryrun.sh start ... "
                "--unit-inherit-request <当前批准请求> --audit-policy <原策略>，不自动接空跑"}
    if not payload.get("vc_env") or not payload.get("data_root") or not payload.get("runroot"):
        return {"action": "skipped", "reason": "缺少驱动参数文件、数据根或 RUNROOT，没有接着空跑"}
    argv = [sys.executable, "-B", str(HERE / "entry_dryrun.py"), "start", "--runroot", str(payload["runroot"]),
            "--data-root", str(payload["data_root"]), "--vc-env", str(payload["vc_env"]), "--bundle", str(payload["bundle"]),
            "--branch", str(payload["branch"]), "--commit", str(payload["commit"]), "--to", CHAINED_DRYRUN_TO]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=600, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as error:
        return {"action": "error", "reason": str(error)[:300]}
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    with contextlib.suppress(ValueError, IndexError):
        return {**json.loads(lines[-1]), "returncode": completed.returncode}
    return {"action": "error", "returncode": completed.returncode, "reason": (completed.stderr or completed.stdout)[-300:]}


# ---------------------------------------------------------------- 批次边界与 VC-5 前核对


def _ancestry_note(source: Any) -> str:
    """跨提交后继通过提示里的祖先验证状态说明。

    已验证（``verified``）不追加任何文字，提示与过去逐字相同。``unverified``（用 ``--allow-unverified-ancestry`` 放行的）
    和结论里看不出状态的情形都显眼地写出来：这个状态过去只留在结论文件的 ``unit_inherit_source.ancestry`` 里，
    ``check-boundary``／``require-passed`` 的提示看起来与已验证的后继没有区别，读提示的人注意不到祖先关系其实没验证过。"""

    ancestry = source.get("ancestry") if isinstance(source, Mapping) else None
    status = ancestry.get("status") if isinstance(ancestry, Mapping) else None
    if status == "verified":
        return ""
    if status == "unverified":
        old = str(ancestry.get("old_commit") or "")[:12]
        return (f"；【警告：祖先关系未验证】旧提交 {old} 是否为本提交的祖先没有验证成功（{ancestry.get('reason')}），"
                "本后继是用 --allow-unverified-ancestry 放行的：请人工确认两个提交在同一条历史线上")
    return "；【警告：结论里没有可识别的祖先验证状态（应为 verified 或 unverified）】"


def scope_problem(payload: Mapping[str, Any], client: str) -> str | None:
    """防串用（优化第 9 项）：这份结论的筛选范围能不能当 ``client`` 的准入依据；空＝可以。

    Codex 计划的子集结论没跑 Claude 的重单元，Claude 的 VC-5 准入不能把它当成全集通过（两个客户端共用同一份后台结论）。判定取
    三处互相印证：结论里登记的请求（``gate_scope``）、结论里登记的筛选摘要（``selection``）、入口门禁总摘要的筛选回显（``client_selection``，
    在结论的输出目录里）——任何两处对不上，结论就不可信。没有筛选的结论（三处都空）按全集，与改造前相同。"""

    def scope_of(item: Any) -> Any:
        return item.get("gate_scope") if isinstance(item, Mapping) else None

    requested, recorded = payload.get("gate_scope"), payload.get("selection")
    entry = _entry_summary(Path(str(payload["out"]))) if payload.get("out") else None
    echoed = (entry or {}).get("client_selection")
    if requested is None and recorded is None and echoed is None:
        return None
    if scope_of(recorded) != scope_of(echoed):
        return "结论登记的筛选范围与入口门禁总摘要的回显不一致（结论被改过，或入口门禁没有按登记的范围运行）"
    if requested == GATE_SCOPE_CLIENT_PLAN and echoed is None:
        return "结论登记了按客户端筛选，入口门禁总摘要却没有筛选回显（筛选没有生效）"
    if scope_of(echoed) != GATE_SCOPE_CLIENT_PLAN:
        return None   # 全集（含筛选时归属表自己在改动里、整轮回退成全集的情形）
    if requested != GATE_SCOPE_CLIENT_PLAN:
        return "入口门禁总摘要是按客户端筛选的子集，结论里却没有登记筛选"
    if recorded.get("selection_sha256") != echoed.get("selection_sha256") or recorded.get("client") != echoed.get("client"):
        return "结论登记的筛选摘要与入口门禁总摘要的回显不是同一份"
    if echoed.get("client") != client:
        return f"这是 {echoed.get('client')} 计划的子集结论（没跑另一客户端的重单元），不能当作 {client} 的全集通过——防串用，须另跑 {client} 自己的验证"
    return None


def check(runroot: Path, data_root: Path, *, require_passed: bool, client: str = "codex") -> tuple[int, str]:
    """批次边界（``require_passed`` 为假）或 VC-5 验收前（为真）的判定：（退出码，说明）。"""

    deployment = latest_deployment(data_root, client)
    found = for_deployment(runroot, deployment, client=client, data_root=data_root)
    if found is None:
        message = f"当前部署（{deployment['receipt']}）还没有后台验证：先 background-validate.sh start <bundle> <分支> <提交>"
        return (3, message) if require_passed else (0, "提示：" + message)
    path, payload = found
    status = effective_status(payload)
    where = f"提交 {str(payload.get('commit'))[:12]}，结论 {path}，日志 {payload.get('log')}"
    if status == "passed":
        # 防串用（优化第 9 项）：子集结论只能给它自己的客户端准入用，其余客户端要自己的全集或自己的计划。
        problem = scope_problem(payload, client)
        if problem is not None:
            return 3, f"后台验证结论的筛选范围不满足本客户端准入（{where}）：{problem}"
        if (payload.get("selection") or {}).get("gate_scope") == GATE_SCOPE_CLIENT_PLAN:
            where += f"；按客户端筛选（{client} 计划的子集，不是全集）"
    if status == "passed":
        # 补跑标注无条件重放（优化第 2 项，验收修正建议 8）：不论结论文件里有没有 retry_passed_units 这个键、也不论是哪种执行模式，都按执行器
        # 汇总现算一遍——结论文件被抹掉登记、或汇总里藏了没有标注的补跑尝试，都不能靠“结论里没写”绕过去。执行记录目录（out）不在时无从核对，
        # 与改造前相同（有登记却对不上＝拒绝）。
        registered = sorted(str(item.get("unit_id")) for item in (payload.get("retry_passed_units") or []) if isinstance(item, Mapping))
        replay = retry_report(payload["out"]) if payload.get("out") else {"problems": [], "passed_units": []}
        replayed = sorted(item["unit_id"] for item in replay["passed_units"])
        if replay["problems"] or replayed != registered:
            reasons = replay["problems"] or [f"结论登记的补跑通过名单 {registered} 与执行器汇总现算的 {replayed} 不一致"]
            return 3, f"后台验证的补跑标注不能重放（{where}）：{'；'.join(reasons[:5])}"
        if (payload.get("mode") == "unit-inherit" or payload.get("unit_inherit_request")
                or payload.get("input_contract", {}).get("execution_contract") == "unit-inherit/v1"):
            try:
                if payload.get("mode") != "unit-inherit" or not payload.get("unit_inherit_request"):
                    raise ValidationError("后台逐单元结论缺少模式或请求")
                completion = _inherit_completion(payload, historical=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                return 3, f"后台逐单元消费证明不能重放（{where}）：{error}"
            if payload.get("unit_inherit_source") is None and (payload.get("input_contract") or {}).get("unit_inherit_source") is not None:
                return 3, f"后台跨提交承接登记不能重放（{where}）：输入合同带来源指纹，结论里却没有来源登记"
            if payload.get("unit_inherit_source") is not None:
                # 跨提交后继：登记的承接项、四个计数与重跑原因也要与清单、决策、请求现算的一致，不能只信结论文件里的登记。
                try:
                    _verify_inherit_registry(payload, completion)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
                    return 3, f"后台跨提交承接登记不能重放（{where}）：{error}"
                summary = payload.get("inherit_summary") or {}
                return 0, (f"后台验证通过（{where}）；跨提交后继：承接 {summary.get('inherited')} 个、重跑 {summary.get('executed')} 个"
                           + ("（没有任何单元被承接，实际是整套重新执行）" if summary.get("outcome") == "full_rerun" else "")
                           + _ancestry_note(payload.get("unit_inherit_source")))
        reused_note = ""
        if payload.get("reuse") is not None:
            # 沿用的结论（优化第 5 项）：不能只信 passed 标签，重放来源冻结副本、绑定有效期与工具身份。
            reasons = _reuse_problems(payload)
            if reasons:
                return 3, f"后台验证的沿用不能重放（{where}）：{'；'.join(reasons[:5])}"
            reused_note = f"；沿用自提交 {str(payload.get('commit'))[:12]} 在部署 {payload['reuse'].get('source_deployment')} 上的通过结论（同树重部署，测试结论不重跑）"
        if registered:
            # 结论带补跑通过的单元：重放已在上面做过（标注、两次日志的摘要都对得上），这里只补提示。
            return 0, (f"后台验证通过（{where}）；其中补跑通过的单元 {len(registered)} 个（首次失败后单独补跑一次通过，已单列）：{'、'.join(registered[:8])}"
                       + reused_note)
        return 0, f"后台验证通过（{where}）{reused_note}"
    if status == "failed":
        return 3, f"后台验证失败（没通过的门禁项 {payload.get('failed_gates')}；{where}）：拒绝派发下一批，修好、定向回归、部署后重新 start"
    if status == "aborted":
        return 3, f"后台验证中止、没有结论（{payload.get('reason')}；{where}）：拒绝派发下一批，排查后重新 start"
    if status == "running":
        if require_passed:
            return 3, f"后台验证还在跑（{where}）：VC-5 验收要等它通过"
        return 0, f"后台验证还在跑（{where}），允许继续"
    message = f"当前部署的后台验证{'被停下' if status == 'superseded' else '进程已经不在'}、没有结论（{where}）：重新 background-validate.sh start"
    return (3, message) if require_passed else (0, "提示：" + message)


def _positive_parallel(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--parallel 必须是正整数")
    return int(value)


def _declared_sample_mode(value: str) -> str:
    """``--declared-sample`` 的取值：``off``（缺省）、``always`` 或 ``every:N``（N 为正整数）；非法在最外层就拒绝。
    与执行器 ``unit_executor.declared_sample_mode`` 以及入口脚本 ``entry-gates.sh`` 的正则 ``^(off|always|every:[1-9][0-9]*)$`` 同一规则（测试核对
    各层一致），免得后台起了才在入口门禁里报错：**不去首尾空白**、不认换行、前导零与大小写变体、不是字符串一律拒绝；合法的取值就是规范写法，原样返回。"""

    if isinstance(value, str):
        if value in ("off", "always") or re.fullmatch(r"every:[1-9][0-9]*", value):
            return value
    raise argparse.ArgumentTypeError(f"--declared-sample 只接受 off、always 或 every:N（N 为正整数）：{value!r}")


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="绑定最新部署，后台起全量门禁的全集通过")
    p_start.add_argument("--runroot", type=Path, required=True)
    p_start.add_argument("--data-root", type=Path, required=True)
    p_start.add_argument("--bundle", type=Path, required=True)
    p_start.add_argument("--branch", required=True)
    p_start.add_argument("--commit", required=True)
    p_start.add_argument("--profile", default=DEFAULT_PROFILE)
    p_start.add_argument("--record-store", type=Path, default=None)
    p_start.add_argument("--work", type=Path, default=None,
                         help="只允许本轮完整提交及部署摘要目录或其子目录；缺省自动派生；带 --unit-inherit-source-result 时须是来源后台的那个 WORK")
    p_start.add_argument("--parallel", type=_positive_parallel, default=None, help="门禁单元并发数；缺省沿用执行器配置")
    p_start.add_argument("--declared-sample", type=_declared_sample_mode, default="off", metavar="off|always|every:N",
                         help="声明级承接命令单元的读集证据来源：off（缺省）用登记表证据，always 每次执行都带 strace 采样，every:N 每 N 次采样一次；"
                              "原样交给后台起的入口门禁再到执行器，非 off 的取值进输入合同与结论")
    p_start.add_argument("--vc-env", type=Path, default=None, help="驱动参数文件（ARM64_VC_ENV），后台运行的入口门禁要用")
    p_start.add_argument("--entry-gates", type=Path, default=HERE / "entry-gates.sh")
    p_start.add_argument("--then-dryrun", action="store_true", help="通过之后接着空跑入口到 pre-A3（E4-02）")
    p_start.add_argument("--full-set-context", type=Path, help="B-09 全集绑定及外部证据上下文")
    p_start.add_argument("--full-set-clock", type=Path, help="全集结束时仍有效的校时凭证；不自动补造")
    p_start.add_argument("--full-set-predecessor", type=Path, help="失效的旧收据，只读关联")
    p_start.add_argument("--audit-policy", type=Path, help="B-11 已登记的严格读集策略")
    p_start.add_argument("--rerun-full-set", action="store_true", help="撤销或错绑后显式重跑全集，保留旧终态并关联旧收据")
    p_start.add_argument("--unit-inherit-request", type=Path, help="原后台结束后在同 WORK 按当前批准请求续作，保留旧失败并重验决策")
    p_start.add_argument("--unit-inherit-source-result", type=Path,
                         help="跨提交、跨部署后继：旧后台的结论文件（passed／failed、进程组已退出），在同一物理 WORK 的新运行里承接它的成果；"
                              "只与 --unit-inherit-request 同用")
    p_start.add_argument("--allow-unverified-ancestry", action="store_true",
                         help="旧提交是新提交祖先的验证因材料不足做不了时，退化为在结论里标明 unverified（确定不是祖先永远拒绝）；"
                              "只与 --unit-inherit-source-result 同用")
    p_start.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex", help="绑定哪个客户端的部署收据（缺省 codex）")
    p_start.add_argument("--preflight", choices=("on", "off"), default="on",
                         help="起跑前置检查（冻结收据、基线重封、重部署、准入绑定），不通过就拒绝起跑；缺省 on（与驱动入口 background-validate.sh 一致），off 显式跳过")
    p_start.add_argument("--reuse", choices=("auto", "only", "off"), default="auto",
                         help="同树重部署沿用测试结论：auto 先试沿用、拒绝就照常起完整验证；only 只试沿用（reuse 子命令）；缺省 auto（与驱动入口 background-validate.sh 一致），off 显式不沿用")
    p_start.add_argument("--gate-scope", choices=(GATE_SCOPE_FULL, GATE_SCOPE_CLIENT_PLAN), default=GATE_SCOPE_FULL,
                         help="采集工具测试组怎么选：full（缺省，全集）或 client-plan（按客户端归属筛选，改动涉及的重模块自动并入；结论是子集，"
                              "只给本客户端准入用，不接入口空跑，需配 --no-dryrun；优化第 9 项）")
    p_start.add_argument("--select-base", default=None, help="client-plan 的基点：最近一次发版的 40 位提交（候选提交取测试树 HEAD）")
    p_start.add_argument("--include-claude", action="store_true", help="client-plan 手动开关：并入全部另一客户端的重模块（仍记为 client-plan）")
    p_pre = sub.add_parser("preflight", help="只做起跑前置检查，不起后台验证（退出码 0 通过、3 不通过）")
    p_pre.add_argument("--runroot", type=Path, required=True)
    p_pre.add_argument("--data-root", type=Path, required=True)
    p_pre.add_argument("--bundle", type=Path, required=True)
    p_pre.add_argument("--branch", required=True)
    p_pre.add_argument("--commit", required=True)
    p_pre.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex")
    p_pre.add_argument("--only", default=None, help="只跑指定检查项，逗号分隔（freeze-receipts、baseline-reseal、redeploy、admission-binding）")
    p_run = sub.add_parser("run", help="（内部）跑入口门禁并写结论")
    p_run.add_argument("--result", type=Path, required=True)
    p_reuse = sub.add_parser("reuse", help="同树重部署后沿用已通过的测试结论（退出码 0 沿用成功、3 被拒绝要求重验）")
    p_reuse.add_argument("--runroot", type=Path, required=True)
    p_reuse.add_argument("--data-root", type=Path, required=True)
    p_reuse.add_argument("--bundle", type=Path, required=True)
    p_reuse.add_argument("--branch", required=True)
    p_reuse.add_argument("--commit", required=True)
    p_reuse.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex")
    p_reuse.add_argument("--vc-env", type=Path, default=None)
    p_reuse.add_argument("--entry-gates", type=Path, default=HERE / "entry-gates.sh")
    p_reuse.add_argument("--then-dryrun", action="store_true", help="沿用成功之后接着空跑入口到 pre-A3（与 start --then-dryrun 同一语义；驱动入口缺省带）")
    p_stop = sub.add_parser("stop", help="停下在跑的后台验证，标 superseded")
    p_stop.add_argument("--runroot", type=Path, required=True)
    p_stop.add_argument("--reason", default="修复轮跑定向回归前停下（上一轮提交的验证已作废）")
    for name in ("check-boundary", "require-passed"):
        p = sub.add_parser(name)
        p.add_argument("--runroot", type=Path, required=True)
        p.add_argument("--data-root", type=Path, required=True)
        p.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex", help="取哪个客户端的部署收据（缺省 codex）")
    p_status = sub.add_parser("status")
    p_status.add_argument("--runroot", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "start":
            if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
                raise ValidationError("--commit 要完整的 40 位小写提交号")
            for name in ("runroot", "data_root", "bundle"):
                if not getattr(args, name).is_absolute():
                    raise ValidationError(f"--{name.replace('_', '-')} 必须是绝对路径")
            print(json.dumps(start(args), ensure_ascii=False))
            return 0
        if args.command == "preflight":
            if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
                raise ValidationError("--commit 要完整的 40 位小写提交号")
            module = _preflight_module()
            client_deployment = latest_deployment(args.data_root, args.client)
            try:
                report = module.preflight_with_bundle(
                    bundle=args.bundle, branch=args.branch, commit=args.commit, data_root=args.data_root, runroot=args.runroot, client=args.client,
                    deployment=client_deployment, deployments=deployment_set(args.data_root),
                    scratch_parent=Path(args.runroot) / DIRECTORY / "preflight", only=[item for item in (args.only or "").split(",") if item] or None)
            except module.PreflightError as error:
                raise ValidationError(f"起跑前置检查做不了：{error}") from error
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["passed"]:
                print(module.format_failures(report), file=sys.stderr)
            return 0 if report["passed"] else 3
        if args.command == "reuse":
            if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
                raise ValidationError("--commit 要完整的 40 位小写提交号")
            started = start(_parse(["start", "--runroot", str(args.runroot), "--data-root", str(args.data_root), "--bundle", str(args.bundle),
                                    "--branch", args.branch, "--commit", args.commit, "--client", args.client, "--reuse", "only",
                                    "--entry-gates", str(args.entry_gates), *(["--vc-env", str(args.vc_env)] if args.vc_env else []),
                                    *(["--then-dryrun"] if args.then_dryrun else [])]))
            print(json.dumps(started, ensure_ascii=False))
            return 3 if started.get("action") == "refused" else 0
        if args.command == "run":
            return run(args.result)
        if args.command == "stop":
            notes: list[str] = []
            stopped = stop(args.runroot, args.reason, notes=notes)
            print(json.dumps({"stopped": stopped, **({"notes": notes} if notes else {})}, ensure_ascii=False))
            return 0
        if args.command in ("check-boundary", "require-passed"):
            code, message = check(args.runroot, args.data_root, require_passed=args.command == "require-passed",
                                  client=args.client)
            print(message, flush=True)
            return code
        listed = [{**payload, "effective_status": effective_status(payload), "path": str(path)} for path, payload in results(args.runroot)]
        # 两客户端都纳管时另列本运行根搭上的那一次（标 attached；只有一个客户端纳管时没有，输出与改造前相同）。
        listed += [{**payload, "effective_status": effective_status(payload), "path": str(path), "attached": True}
                   for path, payload in attached(args.runroot)]
        print(json.dumps(listed, ensure_ascii=False, indent=2))
        return 0
    except ValidationError as error:
        print(f"后台验证：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
