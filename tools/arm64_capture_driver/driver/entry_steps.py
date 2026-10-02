#!/usr/bin/env python3
"""入口步骤的输入摘要与失效判定（E2-05）。

为什么要有：续跑时「上次的结果还能不能用」原来主要看部署收据和工具身份。工具身份不含测试目录，真实链要读两份不进
身份的指南，后端测试直接读 ``tools/`` 和 ``docs/``，只看身份会把该重做的漏掉。这里给入口的每个步骤登记一份输入清单，
逐项算摘要写进步骤记录；续跑时逐步重算，全部一致且上次通过才沿用，任一项变了这一步失效并写明是哪一项，失效再沿
依赖关系传给下游。

输入分七类（方案 E2-05）：参数（本步骤实际读的键和值，不是整份参数文件）、受管工具（五摘要、具体文件或整树）、测试与
夹具、文档、命令与驱动（命令字面量、本步骤用到的驱动文件）、上游产物（前序步骤记录里的产物摘要、最新部署收据、
其他前序产物）、环境（内核、Python、Go、node、容器镜像、二进制、录制数据、出口策略）。输入范围拿不准的一律从宽
（方案 D3），由读集审计（E3-04）收窄。

判定（``evaluate``）按步骤表的依赖顺序逐步进行：

* 实时类步骤（入口便宜检查、客户端启动探测）每次执行：出口租期、磁盘水位、总账状态、容器状态不是文件内容，摘要
  没变也可能已经失效；
* Formal Campaign 建成后，创建链上的步骤一律冻结，输入再变也不重做（方案 D13），之后的修复走 Campaign 自己的
  工具演进与恢复路径；
* 没有记录、记录自摘要不符、记录来自快照、上次没通过、步骤的输入声明变了、产物缺失或被改动、产物内容不合格（Job
  演练收据不是通过）、任一输入变了、上游要重做：都判重做，并把全部原因写出来（一次报全）；
* 账本已建就绝不重建：输入变了判为阻塞，交给人决定（换新一轮）。

步骤记录（``entry-step-record/v1``）放在 ``--steps-dir``（默认 ``$RUNROOT/entry-steps``）：``<步骤>.json`` 是当前记录，
``history/`` 里留每一次的不可变副本；记录带自摘要，被改动即不沿用。快照记录（``snapshot`` 子命令，只供验收建立基准）
标明来源，默认不被接受（防止把没执行过的步骤当成通过）。

子命令：

* ``inputs --step <步骤>``：打印这一步当前的输入明细；
* ``begin``／``finish``：执行前算输入，执行后写记录（产物路径用 ``--product 名称=路径`` 给出或覆盖默认坐标）；
  ``record`` 两步合一；
* ``evaluate``：全部步骤的判定与原因，``--json`` 另存机读结果；
* ``snapshot``：把全部非实时步骤按当前输入记成通过（来源标为快照），验收逐类改动时作基准；
* ``tree-digest``：目录内容摘要；
* ``deploy-consistency --tree <测试树> --data-root <数据根>``：入口门禁在跑之前核对数据根部署的就是测试树这一份——受管整树
  （含测试，不计字节码）、两份指南、部署脚本副本逐项比内容摘要（整树身份不含测试目录，只比它会放过「测试改了、还没
  重新部署」）；一致退出 0，不一致退出 1 并列出不一致的项。

参数取自环境变量（驱动 ``source lib.sh`` 后由 ``parse_env.py`` 导出）；入口门禁的源码提交取参数 ``ENTRY_COMMIT``。
退出码：0 全部沿用或冻结（实时类不计）；1 有步骤要重做；2 用法或配置错误；3 有步骤被阻塞。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
RECORD_SCHEMA = "entry-step-record/v1"
EVALUATION_SCHEMA = "entry-step-evaluation/v1"
MISSING = "missing"
IDENTITY_FIELDS = ("policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256",
                   "control_sha256", "tool_files_sha256")
CATEGORIES = {"param": "参数", "managed": "受管工具", "tests": "测试与夹具", "docs": "文档", "command": "命令与驱动",
              "upstream": "上游产物", "environment": "环境"}
SEGMENTS = {"cheap": "便宜检查", "pre_ledger": "建账本之前", "post_ledger": "建账本之后"}
# 目录摘要的排除集合：pycache 只排除字节码（受管树、文档目录、录制数据）；no_tests 另把测试目录单列成一项输入，失效
# 原因能分清是受管代码还是测试变了；source 排除源码树的构建产物与版本库。
TREE_EXCLUDES = {
    "pycache": frozenset({"__pycache__"}),
    "no_tests": frozenset({"__pycache__", "tests"}),
    "source": frozenset({".git", "target", "node_modules", "__pycache__"}),
}
DEPLOY_RECEIPT_GLOB = "codex-*-supervisor-enable-*.json"   # 与驱动 install.py 的 latest_deploy_receipt 同一规则
TEMPLATE = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")


class EntryStepsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Input:
    """一项输入声明：``category`` 是七类之一，``kind`` 决定怎么取值，``arg``／``option`` 是取值参数（路径可引用参数键）。"""

    category: str
    kind: str
    arg: str = ""
    option: str = ""

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.arg}" + (f"#{self.option}" if self.option else "")

    def as_json(self) -> dict[str, str]:
        return {"category": self.category, "kind": self.kind, "arg": self.arg, "option": self.option}


@dataclass(frozen=True)
class Product:
    """步骤产物：``path`` 是默认坐标模板（空则必须由调用方给出）；``content`` 是沿用前的内容核对；``optional`` 可以没有。"""

    name: str
    path: str = ""
    content: str = ""
    optional: bool = False

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "content": self.content, "optional": self.optional}


@dataclass(frozen=True)
class Step:
    step_id: str
    title: str
    segment: str
    inputs: tuple[Input, ...]
    upstream: tuple[str, ...] = ()
    products: tuple[Product, ...] = ()
    live: bool = False            # 实时类：每次执行，不沿用
    ledger: bool = False          # 计时账本：已建就绝不重建，输入变了判阻塞
    creation_chain: bool = True   # Formal 建成后冻结（D13）

    def all_inputs(self) -> tuple[Input, ...]:
        """声明的输入加上游产物：上游每一份产物的摘要都是本步骤的一项输入（上游重做后摘要变了，本步骤自然失效）。"""

        derived = tuple(Input("upstream", "upstream", step_id, product.name)
                        for step_id in self.upstream for product in STEP_BY_ID[step_id].products)
        return self.inputs + derived

    def spec(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "segment": self.segment, "inputs": [item.as_json() for item in self.inputs],
                "upstream": list(self.upstream), "products": [item.as_json() for item in self.products],
                "live": self.live, "ledger": self.ledger, "creation_chain": self.creation_chain}

    def spec_sha256(self) -> str:
        return _sha256_bytes(_canonical(self.spec()))


OCC = "{D}/tools/official_client_capture"


def _params(*keys: str) -> tuple[Input, ...]:
    return tuple(Input("param", "param", key) for key in keys)


def _drivers(*names: str) -> tuple[Input, ...]:
    return tuple(Input("command", "driver", name) for name in names)


def _env(*facts: str) -> tuple[Input, ...]:
    return tuple(Input("environment", "env", fact) for fact in facts)


def _containers(*names: str) -> tuple[Input, ...]:
    return tuple(Input("environment", "container_image", name) for name in names)


def _managed_files(*names: str) -> tuple[Input, ...]:
    return tuple(Input("managed", "file", f"{OCC}/{name}") for name in names)


IDENTITY = tuple(Input("managed", "identity", name) for name in IDENTITY_FIELDS)
MANAGED_TREE = Input("managed", "tree", OCC, "no_tests")
TESTS_TREE = Input("tests", "tree", f"{OCC}/tests", "pycache")
GUIDES = (Input("docs", "file", "{D}/docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"),
          Input("docs", "file", "{D}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md"))
MAINTENANCE = Input("docs", "tree", "{D}/docs/egress/maintenance", "pycache")
# pre-A3 场景读、但测试树里没有或不保证与测试树一致的数据根内容：冻结台账目录、录制数据（含录制配置引用的源码树、画像与
# 安装包）、alpine 镜像（E2-05 的三项）；断言打包脚本（vc1 录制回放链按生产布局调用数据根这一份）与项目总账的两处探测
# 位置（场景从临时根往上最多六层找 upgrade-project-ledger，会探到 staging 与数据根顶层；那里出现总账时场景行为会变，现在
# 都不存在，记为 missing）——后两类是 E3-04 读集审计实测补的。pre-A3 步骤与入口门禁的场景单元
# （entry_gates.pre_a3_data_root_inputs）用同一份。
PRE_A3_DATA_ROOT = (
    MAINTENANCE, Input("environment", "recorded"), Input("environment", "image_ref", "alpine:3.21"),
    Input("managed", "file", "{D}/tools/prepare_assertion_bundle.sh"),
    Input("environment", "tree", "{D}/staging/upgrade-project-ledger", "pycache"),
    Input("environment", "tree", "{D}/upgrade-project-ledger", "pycache"),
)
# Codex 指南第二部分：预检 plan、Job 演练与收口只读这一节（按目标场景清单 source_spec 的锚点、受管模块同一算法算摘要）。
GUIDE_PART2 = Input("docs", "section", "{SCENARIOS_JSON}")
DEPLOY_RECEIPT = Input("upstream", "deploy_file")
DEPLOY_TOOL_FILES = Input("upstream", "deploy_field", "tool_files_sha256")
COMMON_DRIVER = _drivers("lib.sh", "parse_env.py")
JOB_CONTAINERS = ("capture-cli", "sub2apiplus", "sub2apiplus-keeper", "sub2apiplus-postgres", "sub2apiplus-redis")
# 入口便宜检查「后续阶段要用的身份参数」一项逐个核对的键（entry-preflight.sh 的 required_parameters）。
PREFLIGHT_REQUIRED = ("TARGET_CODE_MODE_HOST_SHA256", "CAPTURE_RUNTIME_IMAGE", "CODEX_BIN", "CODEX_BIN_SHA256",
                      "OFFICIAL_ASSET_SHA256", "TARGET_PACKAGE", "TARGET_SOURCE", "BASELINE_SOURCE", "ACTIVE_PROFILE",
                      "MAIN_MODEL", "LITE_MODEL", "CODEX_ACCOUNT_ID", "API_KEY_ID", "COMPOSE_DIR", "PROJECT_DEADLINE_UTC",
                      "STAGE_BUDGETS", "POLICY_COMPAT_RECEIPT", "POLICY_ACTIVATION", "PRE_A3_CERTIFICATION")
PLAN_PARAMS = ("BASELINE_VERSION", "TARGET_VERSION", "BASELINE_SOURCE", "TARGET_SOURCE", "ACTIVE_PROFILE", "CODEX_BIN",
               "CODEX_BIN_SHA256", "TARGET_PACKAGE", "OFFICIAL_ASSET_SHA256", "TARGET_CODE_MODE_HOST_SHA256",
               "CAPTURE_RUNTIME_IMAGE", "MAIN_MODEL", "LITE_MODEL", "CODEX_ACCOUNT_ID", "API_KEY_ID", "COMPOSE_DIR",
               "CAMPAIGN_PREFIX", "UP")

# 步骤表：顺序即依赖顺序（上游一律排在前面，导入时核对）。依据见各步骤的注释与方案 E2-05、E2-06。
STEPS: tuple[Step, ...] = (
    # 便宜检查七项合成一个实时步骤：每次几十秒内重跑，记下输入只供复盘。
    Step("entry-preflight", "入口便宜检查", "cheap", live=True, creation_chain=False, inputs=(
        *_params("ROUND", "STAMP", "UP", "BASELINE_VERSION", "TARGET_VERSION", "MIN_FREE_GIB", *PREFLIGHT_REQUIRED),
        *IDENTITY, DEPLOY_RECEIPT, GUIDE_PART2,
        *_drivers("entry-preflight.sh", "guard.sh", "../install.py", "bytecode_cache.py"), *COMMON_DRIVER,
        *_env("kernel", "python"), Input("environment", "file", "{CODEX_BIN}"), Input("environment", "file", "{TARGET_PACKAGE}"),
    )),
    # 兼容收据的有效性只取决于两份策略文件（激活认证只核它的当前策略字段）；取整树会在策略没变时被拒签而卡死。
    Step("policy-compatibility", "策略兼容收据", "cheap", inputs=(
        *_params("PREVIOUS_POLICY", "POLICY_COMPAT_RECEIPT"),
        *_managed_files("tool_identity_policy_v2.json"), Input("managed", "file", "{PREVIOUS_POLICY}"),
        *COMMON_DRIVER, Input("command", "command", "codex_upgrade_policy_certification compatibility"),
    ), products=(Product("compatibility", "{POLICY_COMPAT_RECEIPT}"),)),
    # 激活认证绑定部署收据的路径与摘要（指南要求每次部署签一次）：换部署收据即失效。
    Step("policy-activation", "策略激活认证", "cheap", upstream=("policy-compatibility",), inputs=(
        *_params("POLICY_ACTIVATION"), *IDENTITY, DEPLOY_RECEIPT,
        *COMMON_DRIVER, Input("command", "command", "codex_upgrade_policy_certification activation"),
    ), products=(Product("activation", "{POLICY_ACTIVATION}"),)),
    # 入口门禁（测试树部分）：源码提交确定测试树的全部内容（源码、测试、文档、Makefile、CI 定义、权重表与耗时表）；
    # 前端依赖、历史门禁源码、工具链和执行器另列。部署收据只取整树摘要字段（核对数据根已部署本提交）。两份 P0 证据
    # 可以没有：全集通过模式承接了单元时写 v2（E3-03），记录库里没有已发布的清单、测试树五摘要算不出来时不写，P0 收据
    # 步骤据总摘要报原因。
    Step("entry-gates", "入口门禁", "pre_ledger", inputs=(
        Input("tests", "source_commit", "ENTRY_COMMIT"),
        *_params("HISTORY_TEST_TREE", "HISTORICAL_SOURCE_ROOT"), DEPLOY_TOOL_FILES,
        *_drivers("entry-gates.sh", "entry_gates.py", "unit_executor.py", "unit_executor.json", "bytecode_cache.py"),
        *COMMON_DRIVER,
        *_env("kernel", "python", "go", "node", "golangci_lint", "docker"),
        Input("environment", "tree", "{HISTORICAL_SOURCE_ROOT}", "pycache"),
        Input("environment", "file", "{HISTORY_TEST_TREE}/frontend/pnpm-lock.yaml"),
        Input("environment", "file", "{HISTORY_TEST_TREE}/frontend/node_modules/typescript/lib/typescript.js"),
    ), products=(Product("summary"), Product("p0-test-capture-tools", optional=True), Product("p0-check-egress-spec", optional=True))),
    # pre-A3：场景在数据根生产布局里跑，读受管整树（含测试、指纹代理、运行时镜像定义）、部署脚本副本、两份指南（真实链
    # 副本树复制它们），以及 PRE_A3_DATA_ROOT 那几项（冻结台账目录、录制数据、断言打包脚本、项目总账探测位置等）。部署收据
    # 不取文件摘要、激活认证只取策略摘要字段（第 19 项：工具五摘要与策略没变时跨部署复用）。
    Step("pre-a3", "pre-A3 路径认证", "pre_ledger", inputs=(
        *_params("PRE_A3_CERTIFICATION"), *IDENTITY, MANAGED_TREE, TESTS_TREE,
        Input("managed", "file", "{D}/tools/arm64_supervised_deploy.py"), *GUIDES, *PRE_A3_DATA_ROOT,
        Input("upstream", "json_field", "{POLICY_ACTIVATION}", "policy_sha256"),
        *_drivers("entry-gates.sh", "entry_gates.py", "unit_executor.py", "unit_executor.json", "pre-a3.sh"), *COMMON_DRIVER,
        *_env("kernel", "python"),
    ), products=(Product("certification", "{PRE_A3_CERTIFICATION}"), Product("reuse-receipt", optional=True))),
    # 零请求 smoke：经 provenance 传递导入编排器但只导入不调用（control 层），运行时导入测试模块与夹具。
    Step("zero-request-smoke", "零请求 smoke", "pre_ledger", inputs=(
        Input("managed", "identity", "control_sha256"), TESTS_TREE,
        *_drivers("stage1.sh"), *COMMON_DRIVER, *_env("python"),
    ), products=(Product("receipt", "{D}/audit/zero-request-smoke-{STAMP}.json"),)),
    # atomic-double 在 capture-cli 容器里跑：导入链没有逐一核实，受管工具按整树摘要从宽。
    Step("atomic-double", "atomic-double 收据", "pre_ledger", inputs=(
        Input("managed", "identity", "tool_files_sha256"),
        *_drivers("stage1-finish.sh"), *COMMON_DRIVER,
        *_containers("capture-cli"), Input("environment", "container_python", "capture-cli"),
    ), products=(Product("receipt"),)),
    # 计时账本：只认决定「这本账属于哪次升级」的参数。截止时间只在建账本时折算总预算，之后的延期记在账本事件里
    # （deadline-extend），参数文件里的截止时间改了不影响已建账本，所以不列。工具改了不影响已建账本（生成器摘要漂移
    # 由账本自己的承接收据处理）。
    Step("ledger", "计时账本", "post_ledger", ledger=True, inputs=(
        *_params("UP", "BASELINE_VERSION", "TARGET_VERSION", "EVIDENCE_DECISION", "STAGE_BUDGETS"),
    ), products=(Product("ledger", "{ENTRY_ROOT}/control/{UP}-timing-ledger/ledger.json"),)),
    # 环境收据：采集器与增量恢复两个受管文件；环境取稳定的部分（主机、架构、两个容器的镜像、出口策略、目标客户端）。
    Step("environment-p0", "ARM64 环境收据（p0）", "post_ledger", inputs=(
        *_params("UP", "TARGET_VERSION"),
        *_managed_files("codex_upgrade_arm64_environment_receipt.py", "incremental_recovery.py"),
        *_drivers("stage1.sh"), *COMMON_DRIVER,
        *_env("hostname", "arch"), *_containers("capture-cli", "sub2apiplus"),
        Input("environment", "file", "/etc/sub2api-egress/policy.json"),
        Input("environment", "file", "/opt/codex-{TARGET_VERSION}/bin/codex"),
    ), products=(Product("receipt"),)),
    Step("ledger-checkpoint", "账本 checkpoint", "post_ledger", upstream=("ledger",), inputs=(
        *_managed_files("codex_upgrade_timing_ledger.py"), *_drivers("stage1.sh"), *COMMON_DRIVER,
    ), products=(Product("checkpoint"),)),
    # 预检 plan 冻结完整工具身份；读指南第二部分、两棵官方源码树、官方包、Active 画像、compose 文件与采集镜像。
    Step("preflight-plan", "预检 plan", "post_ledger", upstream=("ledger", "ledger-checkpoint", "environment-p0"), inputs=(
        *_params(*PLAN_PARAMS), *IDENTITY, GUIDE_PART2, DEPLOY_TOOL_FILES,
        *_drivers("stage1.sh"), *COMMON_DRIVER,
        Input("environment", "tree", "{BASELINE_SOURCE}", "source"), Input("environment", "tree", "{TARGET_SOURCE}", "source"),
        Input("environment", "file", "{TARGET_PACKAGE}"), Input("upstream", "file", "{ACTIVE_PROFILE}"),
        Input("environment", "file", "{COMPOSE_DIR}/docker-compose.yml"),
        Input("environment", "image_ref", "{CAPTURE_RUNTIME_IMAGE}"),
    ), products=(Product("campaign"),)),
    # Job 演练：五个容器的镜像、目标客户端二进制、采集镜像；收据可能是失败的，沿用前要核对状态是通过。
    Step("job-rehearsal", "Job 演练收据", "post_ledger", upstream=("preflight-plan",), inputs=(
        *IDENTITY, GUIDE_PART2, *_params("CODEX_BIN", "TARGET_CODE_MODE_HOST_SHA256", "CAPTURE_RUNTIME_IMAGE"),
        *_drivers("stage1-finish.sh"), *COMMON_DRIVER,
        *_containers(*JOB_CONTAINERS), Input("environment", "file", "{CODEX_BIN}"),
        Input("environment", "image_ref", "{CAPTURE_RUNTIME_IMAGE}"),
    ), products=(Product("receipt", content="status_passed"),)),
    # 启动探测按指南每次重跑（反映修复后的最新环境）；报告登记进记录，收口前预检按它再核验一次（E2-07）。
    Step("client-launch-probe", "客户端启动探测", "post_ledger", live=True, creation_chain=False, upstream=("preflight-plan",), inputs=(
        *IDENTITY, *_drivers("client_launch_probe.py", "client_launch_probe_runner.py", "stage1-finish.sh"), *COMMON_DRIVER,
        *_containers("capture-cli"),
    ), products=(Product("report"),)),
    # 发布认证：导入期经 pre-A3 认证模块读测试夹具，测试目录从宽整体列入；绑定部署收据、pre-A3、激活认证、Job 演练、atomic。
    Step("release-certification", "发布认证", "post_ledger",
         upstream=("pre-a3", "policy-activation", "job-rehearsal", "atomic-double"), inputs=(
        *_params("RELEASE_CERTIFICATION"), *IDENTITY, TESTS_TREE, DEPLOY_RECEIPT,
        *_drivers("stage2.sh"), *COMMON_DRIVER, *_env("arch", "go", "docker"),
    ), products=(Product("certification", "{RELEASE_CERTIFICATION}"),)),
    # P0 收据：收据模块只依赖标准库；证据取自入口门禁那一次运行，绑定发布认证、回退依据与账本的升级 ID。入口门禁承接了
    # 单元时证据是 v2（E3-03），签发时按证据登记的记录库逐条重验——记录库是入口门禁步骤的产物，随上游一起失效。
    Step("p0-receipt", "P0 收据", "post_ledger", upstream=("entry-gates", "release-certification", "ledger"), inputs=(
        *_params("UP", "BASELINE_VERSION", "TARGET_VERSION", "P0_ROLLBACK_EVIDENCE"), *_managed_files("codex_upgrade_vc_receipt.py"),
        Input("upstream", "file", "{P0_ROLLBACK_EVIDENCE}"), *_drivers("entry_orchestrator.py"),
    ), products=(Product("receipt"),)),
    # VC-0 收口（E2-07）：Formal 建成前后都要执行（建成后是续作：补阶段事件派发首批，或交给 VC-1 对账恢复链），所以不随
    # 创建链冻结；收口完成（记录通过）且输入没变就沿用。建成之后修工具登记演进，工具身份一变这一步就重做（续作）。
    Step("vc0-closeout", "VC-0 收口", "post_ledger", creation_chain=False,
         upstream=("preflight-plan", "ledger", "ledger-checkpoint", "environment-p0", "release-certification",
                   "job-rehearsal", "p0-receipt"), inputs=(
        *_params("NEW", "RELEASE_CERTIFICATION"), *IDENTITY, TESTS_TREE, GUIDE_PART2, *GUIDES, DEPLOY_RECEIPT,
        Input("managed", "file", "{D}/tools/prepare_assertion_bundle.sh"), *_drivers("entry_orchestrator.py"), *_env("arch"),
    ), products=(Product("receipt"),)),
)
STEP_BY_ID: dict[str, Step] = {step.step_id: step for step in STEPS}

# 入口门禁每个门禁项的输入清单（方案 E2-05「每个门禁项都登记一份输入清单」；E3-01 的单元承接按它判断）。测试树里的门禁项
# 共用：源码提交（测试树的全部内容）、驱动里的入口门禁脚本与执行器、内核；再按门禁项加工具链与前端依赖。pre-A3 同上面的
# 步骤。范围从宽，由读集审计（E3-04）收窄。
_GATE_COMMON = (Input("tests", "source_commit", "ENTRY_COMMIT"),
                *_drivers("entry-gates.sh", "entry_gates.py", "unit_executor.py", "unit_executor.json"), *_env("kernel"))
_FRONTEND = (*_env("node"), Input("environment", "file", "{HISTORY_TEST_TREE}/frontend/pnpm-lock.yaml"))
_TYPESCRIPT = Input("environment", "file", "{HISTORY_TEST_TREE}/frontend/node_modules/typescript/lib/typescript.js")
GATE_INPUTS: dict[str, tuple[Input, ...]] = {
    "backend-go-test": (*_GATE_COMMON, *_env("go")),
    "backend-unit": (*_GATE_COMMON, *_env("go")),
    "backend-integration": (*_GATE_COMMON, *_env("go", "docker")),
    "backend-lint": (*_GATE_COMMON, *_env("go", "golangci_lint")),
    "lint-unit": (*_GATE_COMMON, *_env("go", "golangci_lint")),
    "lint-integration": (*_GATE_COMMON, *_env("go", "golangci_lint")),
    "frontend-lint": (*_GATE_COMMON, *_FRONTEND),
    "frontend-typecheck": (*_GATE_COMMON, *_FRONTEND),
    "frontend-critical": (*_GATE_COMMON, *_FRONTEND),
    "test-capture-tools": (*_GATE_COMMON, *_env("python", "node"), _TYPESCRIPT),
    "test-official-client-control": (*_GATE_COMMON, *_env("python")),
    "check-egress-spec": (*_GATE_COMMON, *_env("python", "go"), *_params("HISTORICAL_SOURCE_ROOT"),
                          Input("environment", "tree", "{HISTORICAL_SOURCE_ROOT}", "pycache")),
    "deploy-scripts": (*_GATE_COMMON, *_env("bash")),
    "pre-a3": STEP_BY_ID["pre-a3"].inputs,
}


def gate_inputs(gate_id: str, ctx: "Context") -> list[dict[str, Any]]:
    """门禁项当前的输入明细（没登记的门禁项按测试树门禁项的共用部分从宽）。"""

    return sorted((resolve(item, ctx) for item in GATE_INPUTS.get(gate_id, _GATE_COMMON)), key=lambda entry: entry["name"])


def _check_step_table() -> None:
    seen: set[str] = set()
    for step in STEPS:
        if step.step_id in seen:
            raise EntryStepsError(f"步骤重复：{step.step_id}")
        if step.segment not in SEGMENTS:
            raise EntryStepsError(f"步骤 {step.step_id} 的段不认识：{step.segment}")
        for upstream in step.upstream:
            if upstream not in seen:
                raise EntryStepsError(f"步骤 {step.step_id} 的上游 {upstream} 没有排在它前面")
            if STEP_BY_ID[upstream].live:
                raise EntryStepsError(f"实时类步骤 {upstream} 不能当上游（没有可沿用的产物）")
        for item in step.inputs:
            if item.category not in CATEGORIES:
                raise EntryStepsError(f"步骤 {step.step_id} 的输入类别不认识：{item.category}")
        names = [item.name for item in step.all_inputs()]
        if len(set(names)) != len(names):
            raise EntryStepsError(f"步骤 {step.step_id} 的输入重复")
        seen.add(step.step_id)


_check_step_table()


# ---------------------------------------------------------------- 取值

Runner = Callable[[Sequence[str], "Mapping[str, str] | None", "Path | None"], "tuple[int, str]"]


def default_runner(argv: Sequence[str], env: Mapping[str, str] | None = None, cwd: Path | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, env=dict(env) if env is not None else None,
                                   cwd=str(cwd) if cwd is not None else None, stdin=subprocess.DEVNULL, timeout=900)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, f"{type(error).__name__}: {error}"
    return completed.returncode, completed.stdout


# 受管事实在数据根里算（PYTHONPATH=.，用受管模块自己的算法）：五摘要、指南章节摘要，以及录制回放链实际读的录制数据
# 根目录（录制 Campaign 目录、最后一次 attempt 各作业的证据根、guardian 的失败归档，规则同 vc1_recorded_replay 的
# build_snapshot；录制 Campaign 的编号与位置也由它给出，驱动里不写死），另给录制配置里 ``recorded_fields`` 各项的值
# （回放链 plan 步骤按录制配置读的数据根内容，见 ``RECORDED_SOURCE_FIELDS``）。
_MANAGED_FACTS = r"""
import json, sys
from pathlib import Path, PurePosixPath
request = json.loads(sys.argv[1])
out = {}
if request.get("identity"):
    from tools.official_client_capture import codex_upgrade_policy_certification as pc
    out["identity"] = pc.current_identity()
sections = request.get("sections") or []
if sections:
    from tools.official_client_capture.candidate_rule_assertion import source_spec_section_sha256
    out["sections"] = [source_spec_section_sha256(Path(path), fragment) for path, fragment in sections]
if request.get("recorded"):
    from tools.official_client_capture.tests import vc1_recorded_replay as replay
    campaign = replay.recorded_campaign_dir()
    final = [attempt for _root, attempt in replay._recorded_attempts(campaign) if attempt.get("status") == "awaiting_receipts"]
    if len(final) != 1:
        raise SystemExit("录制 Campaign 应恰好有一个 awaiting_receipts attempt")
    relatives = {replay._runs_relative(str(root)) for result in final[0]["results"] for root in result["evidence_roots"]}
    guardian = [replay._runs_relative(str(root)) for result in final[0]["results"] if result["id"] == replay.GUARDIAN_JOB_ID
                for root in result["evidence_roots"]]
    runs = replay.HOST_DATA_ROOT / "runs"
    for relative in guardian:
        relatives.update(path.name for path in runs.glob(PurePosixPath(relative).name + ".failed-attempt*") if path.is_dir())
    out["recorded"] = [str(campaign), *sorted(str(runs / relative) for relative in relatives)]
    configuration = json.loads((campaign / "campaign.json").read_text(encoding="utf-8")).get("configuration") or {}
    out["recorded_external"] = {field: configuration.get(field) for field in request.get("recorded_fields") or []}
print(json.dumps(out, ensure_ascii=False))
"""
# 录制回放链 plan 步骤按录制配置读的数据根内容（E3-04 读集审计实测，原来没算进录制数据）：基线与目标两棵官方源码树
# （plan 记源码身份：目录摘要、出站清单与 git 提交号）、基线画像、目标安装包。源码树按 source 口径算目录摘要（不含版本库与
# 构建产物，覆盖 plan 自己跳过的那几类之外的全部内容），另加 ``git rev-parse HEAD`` 的值；明细登记源码树与它的 git 目录，
# 读集审计据此核对 git 仓库发现读到的引用与配置。
RECORDED_SOURCE_FIELDS = ("baseline_source", "target_source")
RECORDED_FILE_FIELDS = ("baseline_evidence", "target_package")


class MissingParameter(LookupError):
    pass


class FactUnavailable(EntryStepsError):
    """受管事实（五摘要、指南章节摘要）在数据根里算不出来：这一项记为取不到，依赖它的步骤判重做，判定照常报全。"""


@dataclass
class Context:
    params: Mapping[str, str]
    driver_dir: Path
    steps_dir: Path
    runner: Runner = default_runner

    def __post_init__(self) -> None:
        self.cache: dict[Any, Any] = {}
        if not self.params.get("ENTRY_ROOT") and self.params.get("D"):
            # 产物根缺省是数据根（与 parse_env.py 的派生规则一致；参数里没导出这个键时也按它算）。
            self.params = {**self.params, "ENTRY_ROOT": self.params["D"]}

    @property
    def data_root(self) -> Path:
        value = self.params.get("D")
        if not value:
            raise MissingParameter("D")
        return Path(value)

    def expand(self, template: str) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = self.params.get(key)
            if value is None or value == "":
                raise MissingParameter(key)
            return value

        return TEMPLATE.sub(replace, template)

    def run(self, argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> tuple[int, str]:
        key = ("run", tuple(argv), str(cwd))
        if key not in self.cache:
            self.cache[key] = self.runner(argv, env, cwd)
        return self.cache[key]

    def managed_facts(self, request: Mapping[str, Any]) -> dict[str, Any]:
        environment = {**os.environ, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"}
        rc, out = self.run([sys.executable, "-B", "-c", _MANAGED_FACTS, json.dumps(request, ensure_ascii=False, sort_keys=True)],
                           cwd=self.data_root, env=environment)
        if rc != 0:
            raise FactUnavailable(f"在数据根算受管事实失败（退出码 {rc}）：{out.strip()[-300:]}")
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise FactUnavailable(f"受管事实输出不是 JSON：{out.strip()[-300:]}") from error

    def identity(self) -> dict[str, Any]:
        if "identity" not in self.cache:
            self.cache["identity"] = self.managed_facts({"identity": True})["identity"]
        return self.cache["identity"]

    def recorded_facts(self) -> dict[str, Any]:
        if "recorded" not in self.cache:
            self.cache["recorded"] = self.managed_facts({"recorded": True,
                                                         "recorded_fields": [*RECORDED_SOURCE_FIELDS, *RECORDED_FILE_FIELDS]})
        return self.cache["recorded"]

    def recorded_roots(self) -> list[str]:
        return list(self.recorded_facts()["recorded"])

    def recorded_external(self) -> dict[str, Any]:
        external = self.recorded_facts().get("recorded_external")
        if not isinstance(external, dict):
            raise FactUnavailable("受管事实没有给出录制配置（recorded_external）")
        return dict(external)

    def section(self, guide: Path, fragment: str) -> str:
        key = ("section", str(guide), fragment)
        if key not in self.cache:
            self.cache[key] = self.managed_facts({"sections": [[str(guide), fragment]]})["sections"][0]
        return self.cache[key]

    def latest_deploy_receipt(self) -> Path | None:
        if "deploy" not in self.cache:
            control = self.data_root / "control"
            candidates = sorted((path for path in control.glob(DEPLOY_RECEIPT_GLOB) if path.is_file() and not path.is_symlink()),
                                key=lambda path: path.name.split("-supervisor-enable-", 1)[1].lower())
            self.cache["deploy"] = candidates[-1] if candidates else None
        return self.cache["deploy"]

    def tree(self, root: Path, exclude: str) -> tuple[str, int]:
        key = ("tree", str(root), exclude)
        if key not in self.cache:
            self.cache[key] = tree_digest(root, TREE_EXCLUDES[exclude])
        return self.cache[key]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path, exclude: frozenset[str]) -> tuple[str, int]:
    """目录内容摘要：按相对路径排序，逐个文件记「路径＋内容摘要」（符号链接记指向）；不计权限与时间。"""

    total, count = hashlib.sha256(), 0
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        kept = []
        for name in sorted(dirnames):
            if name in exclude:
                continue
            if (base / name).is_symlink():
                filenames.append(name)   # 指向目录的符号链接按链接记，不跟进去
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name in exclude:
                continue
            path = base / name
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if path.is_symlink():
                total.update(b"L\0" + relative + b"\0" + os.readlink(path).encode("utf-8") + b"\n")
            elif path.is_file():
                total.update(b"F\0" + relative + b"\0" + file_sha256(path).encode("ascii") + b"\n")
            else:
                continue
            count += 1
    return total.hexdigest(), count


# 入口门禁在测试树里验、pre-A3 在数据根里跑的那几份：必须是同一份内容。
DEPLOY_CONSISTENCY_PARTS = ("tools/official_client_capture", "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
                            "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "tools/arm64_supervised_deploy.py")


def deploy_consistency(tree: Path, data_root: Path) -> list[str]:
    """测试树与数据根在 ``DEPLOY_CONSISTENCY_PARTS`` 上逐项比内容，返回不一致（或任一边缺失）的项。"""

    def digest(path: Path) -> str | None:
        if path.is_symlink():
            return None
        if path.is_dir():
            return tree_digest(path, TREE_EXCLUDES["pycache"])[0]
        return file_sha256(path) if path.is_file() else None

    mismatch = []
    for part in DEPLOY_CONSISTENCY_PARTS:
        left, right = digest(tree / part), digest(data_root / part)
        if left is None or left != right:
            mismatch.append(part)
    return mismatch


def _value_entry(item: Input, value: str, **detail: Any) -> dict[str, Any]:
    return {"category": item.category, "name": item.name, "sha256": _sha256_text(f"{item.name}={value}"),
            "detail": {"value": value[:300], **detail}}


def _missing(item: Input, **detail: Any) -> dict[str, Any]:
    return {"category": item.category, "name": item.name, "sha256": MISSING, "detail": detail}


def _file_entry(item: Input, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return _missing(item, path=str(path))
    return {"category": item.category, "name": item.name, "sha256": file_sha256(path), "detail": {"path": str(path)}}


def _command_value(ctx: Context, argv: Sequence[str]) -> str:
    rc, out = ctx.run(argv)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0 or not lines:
        return f"unavailable(rc={rc})"
    return lines[0]


ENV_FACTS: dict[str, Callable[[Context], str]] = {
    "kernel": lambda ctx: platform.release(),
    "python": lambda ctx: f"{platform.python_implementation()} {platform.python_version()}",
    "arch": lambda ctx: platform.machine(),
    "hostname": lambda ctx: socket.gethostname(),
    "go": lambda ctx: _command_value(ctx, ["go", "version"]),
    "node": lambda ctx: _command_value(ctx, ["node", "--version"]),
    "pnpm": lambda ctx: _command_value(ctx, ["pnpm", "--version"]),
    "golangci_lint": lambda ctx: _command_value(ctx, ["golangci-lint", "--version"]),
    "docker": lambda ctx: _command_value(ctx, ["docker", "info", "--format", "{{.ServerVersion}}"]),
    "bash": lambda ctx: _command_value(ctx, ["bash", "--version"]),
}


def resolve(item: Input, ctx: Context) -> dict[str, Any]:
    """一项输入的当前值：``{category, name, sha256, detail}``；取不到的值记为 ``missing``（与有值不相等，变化可见）。"""

    try:
        return _resolve(item, ctx)
    except MissingParameter as error:
        return _missing(item, missing_param=str(error))
    except FactUnavailable as error:
        return _missing(item, error=str(error)[:300])


def _resolve(item: Input, ctx: Context) -> dict[str, Any]:
    kind = item.kind
    if kind == "param":
        value = ctx.params.get(item.arg)
        return _missing(item, key=item.arg) if value is None else _value_entry(item, value, key=item.arg)
    if kind == "identity":
        return _value_entry(item, str(ctx.identity()[item.arg]))
    if kind == "file":
        return _file_entry(item, Path(ctx.expand(item.arg)))
    if kind == "tree":
        root = Path(ctx.expand(item.arg))
        if root.is_symlink() or not root.is_dir():
            return _missing(item, path=str(root))
        digest, count = ctx.tree(root, item.option)
        return {"category": item.category, "name": item.name, "sha256": digest, "detail": {"path": str(root), "files": count}}
    if kind == "recorded":
        total, files, roots = hashlib.sha256(), 0, ctx.recorded_roots()
        for root in roots:
            path = Path(root)
            if path.is_symlink() or not path.is_dir():
                return _missing(item, path=root)
            digest, count = ctx.tree(path, "pycache")
            total.update(f"{root}\0{digest}\n".encode("utf-8"))
            files += count
        paths, external = list(roots), ctx.recorded_external()
        for field in (*RECORDED_SOURCE_FIELDS, *RECORDED_FILE_FIELDS):
            value = external.get(field)
            if not isinstance(value, str) or not value.startswith("/"):
                return _missing(item, field=field)
            path = Path(value)
            if field in RECORDED_SOURCE_FIELDS:
                if path.is_symlink() or not path.is_dir():
                    return _missing(item, path=value)
                digest, count = ctx.tree(path, "source")
                commit = _command_value(ctx, ["git", "-C", value, "rev-parse", "HEAD"])
                git_dir = _command_value(ctx, ["git", "-C", value, "rev-parse", "--absolute-git-dir"])
                total.update(f"{field}\0{value}\0{digest}\0{commit}\n".encode("utf-8"))
                files += count
                paths += [value, *([git_dir] if git_dir.startswith("/") else [])]
            else:
                if path.is_symlink() or not path.is_file():
                    return _missing(item, path=value)
                total.update(f"{field}\0{value}\0{file_sha256(path)}\n".encode("utf-8"))
                files += 1
                paths.append(value)
        # 明细列出全部根目录与文件（读集审计按绝对路径核对数据根读取，E3-04）；摘要只由内容决定，明细变化不影响失效判定。
        return {"category": item.category, "name": item.name, "sha256": total.hexdigest(),
                "detail": {"roots": len(roots), "campaign": roots[0] if roots else None, "files": files, "paths": paths}}
    if kind == "section":
        manifest = Path(ctx.expand(item.arg))
        try:
            source = json.loads(manifest.read_text(encoding="utf-8"))["source_spec"]
            guide, fragment = ctx.data_root / str(source["path"]), str(source["fragment"])
        except (OSError, ValueError, KeyError, TypeError):
            return _missing(item, manifest=str(manifest))
        if guide.is_symlink() or not guide.is_file():
            return _missing(item, guide=str(guide))
        return _value_entry(item, ctx.section(guide, fragment), guide=str(guide), fragment=fragment)
    if kind == "deploy_file":
        receipt = ctx.latest_deploy_receipt()
        return _missing(item) if receipt is None else _file_entry(item, receipt)
    if kind == "deploy_field":
        receipt = ctx.latest_deploy_receipt()
        if receipt is None:
            return _missing(item)
        return _json_field(item, receipt, item.arg)
    if kind == "json_field":
        return _json_field(item, Path(ctx.expand(item.arg)), item.option)
    if kind == "driver":
        return _file_entry(item, (ctx.driver_dir / item.arg).resolve())
    if kind == "command":
        return _value_entry(item, item.arg)
    if kind == "upstream":
        return _upstream(item, ctx)
    if kind == "source_commit":
        value = ctx.params.get(item.arg, "")
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            return _missing(item, key=item.arg)
        return _value_entry(item, value, key=item.arg)
    if kind == "env":
        return _value_entry(item, ENV_FACTS[item.arg](ctx))
    if kind == "container_image":
        return _value_entry(item, _command_value(ctx, ["docker", "inspect", "--format", "{{.Image}}", item.arg]))
    if kind == "image_ref":
        reference = ctx.expand(item.arg)
        return _value_entry(item, _command_value(ctx, ["docker", "image", "inspect", "--format", "{{.Id}}", reference]),
                            ref=reference)
    if kind == "container_python":
        return _value_entry(item, _command_value(ctx, ["docker", "exec", item.arg, "python3", "--version"]))
    raise EntryStepsError(f"输入种类不认识：{kind}")


def _json_field(item: Input, path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))[field]
    except (OSError, ValueError, KeyError, TypeError):
        return _missing(item, path=str(path), field=field)
    return _value_entry(item, json.dumps(value, ensure_ascii=False, sort_keys=True), path=str(path), field=field)


def _upstream(item: Input, ctx: Context) -> dict[str, Any]:
    record = read_record(ctx.steps_dir, item.arg)
    if record is None:
        return _missing(item, step=item.arg, product=item.option)
    for product in record.get("products", []):
        if product.get("name") == item.option:
            return {"category": item.category, "name": item.name, "sha256": str(product.get("sha256") or MISSING),
                    "detail": {"step": item.arg, "product": item.option, "path": product.get("path")}}
    return _missing(item, step=item.arg, product=item.option)


def resolve_inputs(step: Step, ctx: Context) -> list[dict[str, Any]]:
    return sorted((resolve(item, ctx) for item in step.all_inputs()), key=lambda entry: entry["name"])


def inputs_sha256(entries: Iterable[Mapping[str, Any]]) -> str:
    return _sha256_bytes(_canonical(sorted([str(entry["name"]), str(entry["sha256"])] for entry in entries)))


# ---------------------------------------------------------------- 记录

def record_path(steps_dir: Path, step_id: str) -> Path:
    return steps_dir / f"{step_id}.json"


def pending_path(steps_dir: Path, step_id: str) -> Path:
    return steps_dir / "pending" / f"{step_id}.json"


def seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key != "record_sha256"}
    return {**body, "record_sha256": _sha256_bytes(_canonical(body))}


def seal_ok(payload: Mapping[str, Any]) -> bool:
    body = {key: value for key, value in payload.items() if key != "record_sha256"}
    return payload.get("record_sha256") == _sha256_bytes(_canonical(body))


def read_record(steps_dir: Path, step_id: str) -> dict[str, Any] | None:
    path = record_path(steps_dir, step_id)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": "unreadable"}
    return payload if isinstance(payload, dict) else {"schema_version": "unreadable"}


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _write_history(steps_dir: Path, payload: Mapping[str, Any]) -> Path:
    history = steps_dir / "history"
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%fz")
    for serial in range(1, 1000):
        path = history / f"{payload['step_id']}-{stamp}-{payload['status']}-{serial}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return path
    raise EntryStepsError("历史记录序号用尽")


def _product_paths(step: Step, ctx: Context, overrides: Mapping[str, str], *, strict: bool = True) -> dict[str, str]:
    """产物坐标：调用方给的优先，其次默认模板。记通过时（strict）每份必需产物都要有坐标；记失败时拿不到坐标的就不记。"""

    unknown = sorted(set(overrides) - {product.name for product in step.products})
    if unknown:
        raise EntryStepsError(f"步骤 {step.step_id} 没有这些产物：{unknown}")
    paths: dict[str, str] = {}
    for product in step.products:
        if product.name in overrides:
            paths[product.name] = overrides[product.name]
        elif product.path:
            try:
                paths[product.name] = ctx.expand(product.path)
            except MissingParameter as error:
                if strict:
                    raise EntryStepsError(f"产物 {product.name} 的默认坐标要用参数 {error}，参数里没有") from error
        elif not product.optional and strict:
            raise EntryStepsError(f"产物 {product.name} 没有默认坐标，用 --product {product.name}=路径 给出")
    return paths


def _content_problem(product: Product, path: Path) -> str | None:
    if product.content != "status_passed":
        return None
    try:
        status = json.loads(path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return "不是合法 JSON"
    return None if status == "passed" else f"status={status}"


def _measure_products(step: Step, paths: Mapping[str, str]) -> list[dict[str, Any]]:
    measured = []
    for product in step.products:
        if product.name not in paths:
            continue
        path = Path(paths[product.name])
        digest = file_sha256(path) if path.is_file() and not path.is_symlink() else MISSING
        measured.append({"name": product.name, "path": str(path), "sha256": digest})
    return measured


def begin(step: Step, ctx: Context) -> dict[str, Any]:
    pending = {"schema_version": RECORD_SCHEMA, "step_id": step.step_id, "started_at_utc": _now(),
               "spec_sha256": step.spec_sha256(), "inputs": resolve_inputs(step, ctx)}
    _write_json_atomic(pending_path(ctx.steps_dir, step.step_id), seal(pending))
    return pending


def finish(step: Step, ctx: Context, *, status: str, products: Mapping[str, str], origin: str = "execution") -> dict[str, Any]:
    if status not in {"passed", "failed"}:
        raise EntryStepsError(f"状态只能是 passed 或 failed：{status}")
    pending_file = pending_path(ctx.steps_dir, step.step_id)
    try:
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EntryStepsError(f"步骤 {step.step_id} 没有执行前的输入记录（先 begin）：{pending_file}") from error
    if not seal_ok(pending) or pending.get("spec_sha256") != step.spec_sha256():
        raise EntryStepsError(f"步骤 {step.step_id} 的执行前输入记录被改动或与当前步骤声明不符：{pending_file}")
    measured = _measure_products(step, _product_paths(step, ctx, products, strict=status == "passed"))
    if status == "passed":
        for product in step.products:
            entry = next((item for item in measured if item["name"] == product.name), None)
            if entry is None or entry["sha256"] == MISSING:
                if product.optional:
                    continue
                raise EntryStepsError(f"步骤 {step.step_id} 记为通过，但产物 {product.name} 不存在")
            problem = _content_problem(product, Path(entry["path"]))
            if problem:
                raise EntryStepsError(f"步骤 {step.step_id} 记为通过，但产物 {product.name} 内容不合格：{problem}")
        measured = [entry for entry in measured if entry["sha256"] != MISSING]
    record = seal({
        "schema_version": RECORD_SCHEMA, "step_id": step.step_id, "title": step.title, "segment": step.segment,
        "origin": origin, "status": status, "started_at_utc": pending["started_at_utc"], "completed_at_utc": _now(),
        "spec_sha256": step.spec_sha256(), "inputs": pending["inputs"], "inputs_sha256": inputs_sha256(pending["inputs"]),
        "products": measured, "tool": {"entry_steps_sha256": file_sha256(Path(__file__).resolve())},
    })
    _write_history(ctx.steps_dir, record)
    _write_json_atomic(record_path(ctx.steps_dir, step.step_id), record)
    pending_file.unlink(missing_ok=True)
    return record


# ---------------------------------------------------------------- 判定

def formal_built(ctx: Context) -> bool:
    """Formal Campaign 已建（E2-07）：由收口模块的只读现场判定给出——Formal 可重放且总账注册批次已提交；只有目录或
    注册写了一半的半成品不算（收口续作会把它归档后重建）。判定不了但 Formal 可重放时按已建算（创建链冻结，由收口这一步
    报出不一致）；判定命令本身失败时同样按已建算，宁可冻结也不在 Formal 可能已建时重做创建链。"""

    new = ctx.params.get("NEW")
    root = Path(ctx.params.get("ENTRY_ROOT") or ctx.data_root)
    formal = root / "evidence" / "campaigns" / str(new)
    if not new or not (formal / "campaign.json").is_file():
        return False
    key = ("formal-built", str(formal))
    if key not in ctx.cache:
        argv = [sys.executable, "-B", "-m", "tools.official_client_capture.codex_upgrade_vc0_closeout", "inspect",
                "--timing-ledger-dir", str(root / "control" / f"{ctx.params.get('UP')}-timing-ledger"),
                "--formal-campaign-dir", str(formal), "--formal-campaign-id", str(new),
                "--supervisor-state-dir", str(root / "control" / f"{new}-supervisor")]
        environment = {**os.environ, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"}
        rc, out = ctx.run(argv, cwd=ctx.data_root, env=environment)
        built = True
        if rc == 0:
            try:
                site = json.loads(out.strip().splitlines()[-1])
                kind = site.get("site")
                built = kind in {"formal-built", "dispatched"} or (
                    kind == "inconsistent" and bool((site.get("formal") or {}).get("replayable"))
                )
            except (ValueError, IndexError, AttributeError):
                built = True
        ctx.cache[key] = built
    return bool(ctx.cache[key])


def _label(entry: Mapping[str, Any]) -> str:
    return f"{CATEGORIES.get(str(entry.get('category')), entry.get('category'))} {entry.get('name')}"


def _diff_inputs(recorded: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    before = {str(entry["name"]): entry for entry in recorded}
    after = {str(entry["name"]): entry for entry in current}
    changed = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is None or new is None or old.get("sha256") != new.get("sha256"):
            reference = new or old or {}
            changed.append({"name": name, "category": reference.get("category"),
                            "before": None if old is None else old.get("sha256"),
                            "after": None if new is None else new.get("sha256"),
                            "detail": (new or {}).get("detail") or (old or {}).get("detail")})
    return changed


def _product_problems(step: Step, record: Mapping[str, Any]) -> list[str]:
    problems = []
    recorded = {str(entry.get("name")): entry for entry in record.get("products", []) if isinstance(entry, Mapping)}
    for product in step.products:
        entry = recorded.get(product.name)
        if entry is None:
            if not product.optional:
                problems.append(f"记录里没有产物 {product.name}")
            continue
        path = Path(str(entry.get("path")))
        if path.is_symlink() or not path.is_file():
            problems.append(f"产物 {product.name} 不在了（{path}）")
        elif file_sha256(path) != entry.get("sha256"):
            problems.append(f"产物 {product.name} 被改动（{path}）")
        else:
            problem = _content_problem(product, path)
            if problem:
                problems.append(f"产物 {product.name} 内容不合格：{problem}")
    return problems


def _product_occupied(step: Step, ctx: Context) -> list[str]:
    occupied = []
    for product in step.products:
        if not product.path:
            continue
        try:
            path = Path(ctx.expand(product.path))
        except MissingParameter:
            continue
        if path.exists():
            occupied.append(str(path))
    return occupied


def evaluate_step(step: Step, ctx: Context, decisions: Mapping[str, Mapping[str, Any]], *, is_formal_built: bool,
                  accept_snapshot: bool, force: Mapping[str, str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"step_id": step.step_id, "title": step.title, "segment": step.segment,
                              "reasons": [], "changed_inputs": [], "upstream_rerun": []}

    def done(decision: str) -> dict[str, Any]:
        result["decision"] = decision
        return result

    if step.live:
        result["reasons"].append("实时类步骤，每次执行")
        return done("live")
    if is_formal_built and step.creation_chain:
        result["reasons"].append("Formal Campaign 已建，创建链冻结（D13），之后的修复走工具演进与恢复路径")
        return done("frozen")
    if force and step.step_id in force and not step.ledger:
        # 调用方要求这一步重做（如入口门禁重新执行全集，D12）：不看记录，下游照常按上游重做传播。
        result["reasons"].append(force[step.step_id])
        return done("run")
    record = read_record(ctx.steps_dir, step.step_id)
    if record is None:
        occupied = _product_occupied(step, ctx)
        if step.ledger and occupied:
            result["reasons"].append(f"账本已存在但没有步骤记录，绝不重建：{occupied[0]}")
            return done("blocked")
        result["reasons"].append("没有步骤记录" + (f"（正式坐标已被占用：{', '.join(occupied)}，重做要换新坐标）" if occupied else ""))
        return done("run")
    if record.get("schema_version") != RECORD_SCHEMA or record.get("step_id") != step.step_id or not seal_ok(record):
        result["reasons"].append("步骤记录自摘要不符或格式不对（被改动或损坏），不沿用")
        return done("blocked" if step.ledger else "run")
    origin = record.get("origin")
    if origin != "execution" and not (origin == "snapshot" and accept_snapshot):
        result["reasons"].append("步骤记录来自快照（只供验收建立基准），不是执行结果，不沿用" if origin == "snapshot"
                                 else f"步骤记录的来源不认识（{origin}），不沿用")
        return done("blocked" if step.ledger else "run")
    if record.get("status") != "passed":
        result["reasons"].append(f"上次执行没有通过（status={record.get('status')}）")
        return done("blocked" if step.ledger else "run")
    reasons = result["reasons"]
    current = resolve_inputs(step, ctx)
    changed = _diff_inputs(record.get("inputs", []), current)
    if step.ledger:
        # 账本只看声明的参数有没有变；声明本身随工具升级变化时，只比两边都有的项。
        changed = [entry for entry in changed if entry["before"] is not None and entry["after"] is not None]
    elif record.get("spec_sha256") != step.spec_sha256():
        reasons.append("步骤的输入声明变了（工具升级）")
    result["changed_inputs"] = changed
    for entry in changed:
        reasons.append(f"输入变了：{_label(entry)}")
    reasons.extend(_product_problems(step, record))
    upstream_rerun = [upstream for upstream in step.upstream if decisions[upstream]["decision"] in {"run", "blocked"}]
    if upstream_rerun:
        result["upstream_rerun"] = upstream_rerun
        reasons.append(f"上游要重做：{', '.join(upstream_rerun)}")
    if not reasons:
        return done("reuse")
    if step.ledger:
        reasons.insert(0, "账本已建，绝不重建；输入与已建账本不一致，交给人决定（换新一轮）")
        return done("blocked")
    return done("run")


def evaluate(ctx: Context, *, is_formal_built: bool, accept_snapshot: bool = False,
             force: Mapping[str, str] | None = None) -> dict[str, Any]:
    """全部步骤的判定。``force``：步骤 → 原因，要求这些步骤重做（账本步骤不受影响；Formal 建成后创建链照样冻结）。"""

    decisions: dict[str, dict[str, Any]] = {}
    for step in STEPS:
        decisions[step.step_id] = evaluate_step(step, ctx, decisions, is_formal_built=is_formal_built,
                                                accept_snapshot=accept_snapshot, force=force)
    counts: dict[str, int] = {}
    for decision in decisions.values():
        counts[decision["decision"]] = counts.get(decision["decision"], 0) + 1
    return {"schema_version": EVALUATION_SCHEMA, "evaluated_at_utc": _now(), "formal_built": is_formal_built,
            "accept_snapshot": accept_snapshot, "steps_dir": str(ctx.steps_dir), "counts": counts,
            "steps": list(decisions.values())}


DECISION_LABELS = {"reuse": "沿用", "run": "重做", "frozen": "冻结", "blocked": "阻塞", "live": "实时"}


def print_evaluation(result: Mapping[str, Any]) -> None:
    counts = result["counts"]
    summary = "、".join(f"{DECISION_LABELS[key]} {counts[key]}" for key in DECISION_LABELS if counts.get(key))
    print(f"入口步骤判定（{len(result['steps'])} 步）：{summary}" + ("；Formal Campaign 已建" if result["formal_built"] else ""))
    for step in result["steps"]:
        reasons = "；".join(step["reasons"])
        print(f"  {DECISION_LABELS[step['decision']]}  {step['step_id']}（{step['title']}）" + (f"：{reasons}" if reasons else ""))


def exit_code(result: Mapping[str, Any]) -> int:
    decisions = {step["decision"] for step in result["steps"]}
    if "blocked" in decisions:
        return 3
    return 1 if "run" in decisions else 0


# ---------------------------------------------------------------- 命令行

def _context(args: argparse.Namespace) -> Context:
    params = dict(os.environ)
    if not params.get("D"):
        raise EntryStepsError("参数里没有数据根 D（先 source 驱动 lib.sh 加载本轮参数文件）")
    steps_dir = args.steps_dir or params.get("ENTRY_STEPS_DIR") or (f"{params['RUNROOT']}/entry-steps" if params.get("RUNROOT") else "")
    if not steps_dir:
        raise EntryStepsError("没有步骤记录目录：给 --steps-dir，或先 source 驱动 lib.sh（取 $RUNROOT/entry-steps）")
    driver_dir = args.driver_dir or params.get("DRV") or str(HERE)
    return Context(params=params, driver_dir=Path(driver_dir), steps_dir=Path(steps_dir))


def _step(step_id: str) -> Step:
    if step_id not in STEP_BY_ID:
        raise EntryStepsError(f"步骤不认识：{step_id}（可选：{', '.join(STEP_BY_ID)}）")
    return STEP_BY_ID[step_id]


def _product_overrides(values: Sequence[str], *, prefixed: bool = False) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for value in values:
        key, separator, path = value.partition("=")
        if not separator or not path:
            raise EntryStepsError(f"--product 要写成 名称=路径：{value}")
        step_id, _, product = key.partition(":") if prefixed else ("", "", key)
        if prefixed and not (step_id and product):
            raise EntryStepsError(f"--product 要写成 步骤:名称=路径：{value}")
        result.setdefault(step_id, {})[product] = path
    return result


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="入口步骤的输入摘要与失效判定（E2-05）")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command.add_argument("--steps-dir", help="步骤记录目录（默认 $ENTRY_STEPS_DIR 或 $RUNROOT/entry-steps）")
        command.add_argument("--driver-dir", help="驱动目录（默认 $DRV 或本文件所在目录）")
        return command

    common(sub.add_parser("inputs", help="打印某一步当前的输入明细")).add_argument("--step", required=True)
    common(sub.add_parser("begin", help="执行前算输入")).add_argument("--step", required=True)
    for name, text in (("finish", "执行后写记录"), ("record", "执行前后两步合一")):
        command = common(sub.add_parser(name, help=text))
        command.add_argument("--step", required=True)
        command.add_argument("--status", required=True, choices=("passed", "failed"))
        command.add_argument("--product", action="append", default=[], help="名称=路径（覆盖或给出产物坐标）")
    command = common(sub.add_parser("evaluate", help="全部步骤的判定与原因"))
    command.add_argument("--json", help="机读结果另存到这个文件")
    command.add_argument("--accept-snapshot", action="store_true", help="接受快照记录（只供验收）")
    command.add_argument("--formal-built", choices=("auto", "yes", "no"), default="auto")
    command = common(sub.add_parser("snapshot", help="把全部非实时步骤按当前输入记成通过（来源标为快照，只供验收）"))
    command.add_argument("--product", action="append", default=[], help="步骤:名称=路径")
    command = sub.add_parser("tree-digest", help="目录内容摘要")
    command.add_argument("--root", required=True)
    command.add_argument("--exclude", choices=sorted(TREE_EXCLUDES), default="pycache")
    command = sub.add_parser("deploy-consistency", help="核对数据根部署的就是测试树这一份（受管整树含测试、指南、部署脚本副本）")
    command.add_argument("--tree", required=True)
    command.add_argument("--data-root", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "tree-digest":
            root = Path(args.root)
            if root.is_symlink() or not root.is_dir():
                raise EntryStepsError(f"不是目录：{root}")
            digest, count = tree_digest(root, TREE_EXCLUDES[args.exclude])
            print(json.dumps({"root": str(root), "exclude": args.exclude, "sha256": digest, "files": count}, ensure_ascii=False))
            return 0
        if args.command == "deploy-consistency":
            mismatch = deploy_consistency(Path(args.tree), Path(args.data_root))
            print(" ".join(mismatch) if mismatch else "一致：" + " ".join(DEPLOY_CONSISTENCY_PARTS))
            return 1 if mismatch else 0
        ctx = _context(args)
        if args.command == "inputs":
            print(json.dumps(resolve_inputs(_step(args.step), ctx), ensure_ascii=False, indent=2))
            return 0
        if args.command == "begin":
            pending = begin(_step(args.step), ctx)
            print(f"ENTRY_STEP_BEGIN {args.step} 输入 {len(pending['inputs'])} 项")
            return 0
        if args.command in {"finish", "record"}:
            step = _step(args.step)
            if args.command == "record":
                begin(step, ctx)
            record = finish(step, ctx, status=args.status, products=_product_overrides(args.product).get("", {}))
            print(f"ENTRY_STEP_RECORDED {args.step} {record['status']} 输入摘要 {record['inputs_sha256'][:12]}")
            return 0
        if args.command == "evaluate":
            built = formal_built(ctx) if args.formal_built == "auto" else args.formal_built == "yes"
            result = evaluate(ctx, is_formal_built=built, accept_snapshot=args.accept_snapshot)
            if args.json:
                _write_json_atomic(Path(args.json), result)
            print_evaluation(result)
            return exit_code(result)
        if args.command == "snapshot":
            overrides = _product_overrides(args.product, prefixed=True)
            unknown = sorted(set(overrides) - set(STEP_BY_ID))
            if unknown:
                raise EntryStepsError(f"--product 里的步骤不认识：{unknown}")
            for step in STEPS:
                if step.live:
                    continue
                begin(step, ctx)
                record = finish(step, ctx, status="passed", products=overrides.get(step.step_id, {}), origin="snapshot")
                print(f"ENTRY_STEP_SNAPSHOT {step.step_id} 输入 {len(record['inputs'])} 项、产物 {len(record['products'])} 份")
            return 0
    except EntryStepsError as error:
        print(f"入口步骤：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
