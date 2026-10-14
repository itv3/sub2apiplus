#!/usr/bin/env python3
"""入口编排器（E2-06）：一条命令按依赖关系执行入口各步骤，每步写步骤记录，重新执行同一条命令按记录续跑。

由驱动 ``entry.sh`` 调用（它先 ``source lib.sh`` 加载本轮参数，再执行本模块）。步骤、输入清单与「沿用还是重做」的判定
都来自同目录的 ``entry_steps.py``（E2-05）；本模块负责按判定执行、给只写一次的产物分配坐标、写步骤记录、汇总。

执行顺序与规则：

* 建账本之前（便宜检查、策略兼容与激活认证、入口门禁与 pre-A3、零请求 smoke、atomic-double）**一次报全**：一个失败
  不影响与它无关的步骤继续跑，依赖失败步骤的标为「被阻塞」（便宜检查的部署绑定一项没过时，激活认证、入口门禁与
  pre-A3 都被阻塞），最后一起汇总。只有这一段全部通过才建账本。
* 建账本之后（建账本、环境收据、checkpoint、预检 plan、Job 演练、启动探测、发布认证、P0 收据、VC-0 收口）
  按顺序执行，失败即停。重新执行同一条命令：判定为沿用的跳过，从失败的那一步接着做；账本已建就沿用，绝不重建。
* 沿用核步骤记录（产物存在不等于可用）：没有记录的旧产物通常换带序号的新坐标（``-r2``、``-r3`` …）。Claude 逐单元
  承接入口已有独立 pre-A3 认证时，先由原验证器和当前输入复核，再登记真实消费；无效来源失败，不降级重跑九场景。
  实际坐标写进步骤记录，下游从记录取。
* Job 演练上一次失败且留下了收据时，新的一次带上它只重跑失败的作业（工具已有的 ``--rerun-failed``）。
* VC-0 收口（E2-07）调用收口模块：它先做只读现场判定，再新建、续作或交给 VC-1 的对账恢复链；同一 Formal ID、同一账本，
  重新执行同一条命令即续作。需要批准（上限后的恢复、首批恢复预览）时这一步标「阻塞」并打印下一条命令，批准后带
  ``--approve-sha256``／``--approved-by``（上限后的恢复另带 ``--reason``）重新执行同一条命令。
* Formal Campaign 建成后（收口模块判定：可重放且总账注册已提交，半成品不算），创建链上的步骤一律冻结，只剩收口这一步
  按判定续作。

产物坐标：建账本之后的产物都在产物根 ``ENTRY_ROOT``（默认数据根）下——计时账本、项目总账（``evidence/campaigns/
upgrade-project-ledger``）、环境收据、预检 Campaign、Job 演练、启动探测；atomic-double 与零请求 smoke 照旧在数据根
``staging`` 下（容器经 ``/capture`` 只看得到数据根）。验收演练把 ``ENTRY_ROOT`` 设在数据根 ``staging`` 下的独立目录，
配一本演练总账，生产项目总账与正式坐标一律不写。

选项：``--plan`` 只判定不执行；``--from``／``--to`` 指定起止步骤（起点之前有要重做的步骤时拒绝）；``--inject-mask
步骤=目录`` 只供验收：执行这一步的命令时在私有挂载命名空间里用只读空 tmpfs 遮住这个目录，制造一次真实的失败；
``--approve-sha256``／``--approved-by``／``--reason`` 只交给 VC-0 收口这一步（批准对象由收口模块按现场判定）；
``--reexecute-gates``：入口门禁重新执行全集（方案 D12：升级开工的入口空跑、一致性验收、收尾合入前用），这一步不沿用
记录、单元一个都不承接；承接模式由显式请求决定，没有请求时重新执行。``--audit-reads``：入口门禁检查实际读集；
建立可逐单元承接的测试记录时，同时带 ``--audit-strict --audit-policy <登记策略>``，把完整审计和精确输入合同传到实际
执行器。登记策略与审计选项进入入口门禁、pre-A3 的步骤输入；续跑保持这些选项，只执行失效或缺少有效记录的单元。
运行锁 ``$RUNROOT/.entry.lock``；每步日志与运行汇总在 ``$RUNROOT/entry-runs/<UTC>/``。

按客户端（Claude 升级 B5a-06）：执行顺序与续跑规则两客户端相同；各步骤调用的模块、合同、目录与追加给共享模块的
``--client`` 取客户端表（``client_table.py``，本轮客户端取参数 ``CLIENT``，缺省 codex），Codex 的命令行与改造前逐字相同。
客户端表登记为尚未接入的步骤不执行，记失败并写明落实它的工作项（``--plan`` 也列出），不拿 Codex 的实现顶替（B5a-09 续落实
Claude 的 atomic-double 之后，两客户端都没有待接入的入口步骤）。预检 plan 的参数形状按客户端表的模板展开（B2-11 第二部分：
Claude 的 ``claude_upgrade plan`` 参数；Codex 的模板为空，沿用下面写死的 Codex 参数形状）；atomic-double 给共享模块追加
``--client``（Claude 签 ``official-client-atomic-vc0-vc1-rehearsal/v1``）；VC-0 收口按客户端表的收口模块执行（两客户端同一组
参数）。
逐单元承接续跑：主请求 ``--unit-inherit-request`` 供首轮实际 entry 组合使用，可从首轮同时固定提供另一份已独立批准的
``--unit-inherit-full-gates-request``。pre-A3 按步骤记录沿用时，门禁仍实时执行，实际组合是 full-gates，选用配套请求。
两份请求始终同时进入步骤输入，不改写批准或来源，不把首轮新记录自动加入第二轮来源；没有配套请求保持原合同的拒绝与回退。
声明级承接的采样方式（步骤 5 WP3b）：``--declared-sample off|always|every:N``（缺省 off）原样交给入口门禁与 pre-A3 两步的执行器，决定声明级命令
单元的读集证据来自登记表还是每次执行自己采样；非法值在这里就拒绝，非 off 的取值进入这两步的输入（同一提交下换了采样方式不沿用旧步骤记录），缺省时
命令行、参数与步骤输入与改造前逐字相同。
退出码：0 全部完成或沿用；1 有步骤失败；2 用法或配置错误；3 被阻塞（账本输入不一致、收口需要批准或人工处置等）或已有
编排在运行。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
RUN_SCHEMA = "entry-orchestrator-run/v1"
# VC-0 收口派发 VC-1 首批前申请整机，等调度器批准的上限（秒；后台验证在跑的重单元要先结束）。
MACHINE_ACQUIRE_TIMEOUT_SECONDS = 3600
# 收口模块的退出码 3：需要批准或人工处置（不是失败），这一步标「阻塞」。
CLOSEOUT_NEEDS_OPERATOR = 3


def _entry_steps_module() -> Any:
    """同目录的 entry_steps.py（仓库 tools/ci 与驱动副本里都和本文件同目录）：按路径加载，不受当前目录与 PYTHONPATH 影响。"""

    name = "entry_steps_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "entry_steps.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"找不到同目录的 entry_steps.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


ES = _entry_steps_module()
# 客户端表（B5a-06）：与入口步骤同一个模块对象。各步骤按本轮客户端（参数 CLIENT，缺省 codex）取模块名、合同名、目录与
# 追加给共享模块命令行的 --client（Codex 为空，命令行与改造前逐字相同）；Claude 尚未接入的步骤（表里的 pending）不执行，
# 记失败并写明落实它的工作项，不拿 Codex 的实现顶替。
CT = ES.CT
# 以下是 Codex 的取值（只读视图，沿用改造前的名字）。
PRE_A3_MODULE = CT.spec("codex")["pre_a3_module"]
POLICY_MODULE = CT.spec("codex")["policy_certification_module"]
CLOSEOUT_MODULE = CT.spec("codex")["closeout_module"]
VC_RECEIPT_MODULE = CT.spec("codex")["p0_receipt_module"]
P0_FACTS_SCHEMA = CT.spec("codex")["p0_facts_schema"]

# E2-06 的执行顺序，末尾两步（P0 收据、VC-0 收口）由 E2-07 接上。
ORDER = ("entry-preflight", "policy-compatibility", "policy-activation", "entry-gates", "pre-a3", "zero-request-smoke",
         "atomic-double", "ledger", "environment-p0", "ledger-checkpoint", "preflight-plan", "job-rehearsal",
         "client-launch-probe", "release-certification", "p0-receipt", "vc0-closeout")
PRE_LEDGER = frozenset(step for step in ORDER if ES.STEP_BY_ID[step].segment in {"cheap", "pre_ledger"})
# 执行上的依赖（步骤表的上游之外）：入口门禁与 pre-A3 都要用激活认证。
EXEC_DEPS: dict[str, tuple[str, ...]] = {"entry-gates": ("policy-activation",), "pre-a3": ("policy-activation",)}
# 便宜检查某一项没过时被它挡住的步骤：部署绑定没过，认证与入口门禁都依赖当前部署。
PREFLIGHT_BLOCKS: dict[str, tuple[str, ...]] = {"deployment-identity": ("policy-activation", "entry-gates", "pre-a3")}


class OrchestratorError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh(base: Path) -> Path:
    """只写一次的坐标：空着就用它，被占用（上次失败的残留或没有记录的旧产物）就换 ``-r2``、``-r3`` …。"""

    if not os.path.lexists(base):
        return base
    for serial in range(2, 1000):
        candidate = (base.with_name(f"{base.stem}-r{serial}{base.suffix}") if base.suffix == ".json"
                     else base.with_name(f"{base.name}-r{serial}"))
        if not os.path.lexists(candidate):
            return candidate
    raise OrchestratorError(f"坐标序号用尽：{base}")


def mask_argv(argv: Sequence[str], directory: str) -> list[str]:
    """验收注入：私有挂载命名空间里用只读空 tmpfs 遮住目录，再执行原命令（与入口门禁遮生产别名同一做法）。"""

    script = f"mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs {shlex.quote(directory)} && exec \"$@\""
    return ["unshare", "-m", "--propagation", "private", "bash", "-c", script, "entry-inject", *argv]


Runner = Callable[[Sequence[str], Path, Mapping[str, str], Path], int]


def default_runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {shlex.join(argv)}\n")
        handle.flush()
        try:
            completed = subprocess.run(list(argv), cwd=str(cwd), env=dict(env), stdout=handle, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
        except OSError as error:
            handle.write(f"启动失败：{error}\n")
            return 127
        handle.write(f"[退出码 {completed.returncode}]\n")
        return completed.returncode


@dataclass
class Outcome:
    step_id: str
    decision: str
    action: str = "未执行"      # 执行／沿用／冻结／被阻塞／阻塞／范围外／未到达
    status: str = ""            # passed／failed（执行过的）
    reasons: list[str] = field(default_factory=list)
    products: dict[str, str] = field(default_factory=dict)
    log: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "decision": self.decision, "action": self.action, "status": self.status,
                "reasons": self.reasons, "products": self.products, "log": self.log}


class Orchestrator:
    def __init__(self, params: Mapping[str, str], *, driver_dir: Path, run_dir: Path, steps_dir: Path,
                 runner: Runner = default_runner, es_runner: Any = None, masks: Mapping[str, str] | None = None,
                 approval: Mapping[str, str] | None = None, reexecute_gates: bool = False, audit_reads: bool = False,
                 full_set_request: Path | None = None, full_set_work: Path | None = None,
                 unit_inherit_request: Path | None = None, gate_parallel: int | None = None,
                 unit_inherit_full_gates_request: Path | None = None,
                 audit_strict: bool = False, audit_policy: Path | None = None, declared_sample: str = "off") -> None:
        self.params = dict(params)
        if audit_strict and not audit_reads:
            raise OrchestratorError("--audit-strict 必须同时启用 --audit-reads")
        if audit_policy is not None:
            if not audit_reads or not audit_strict:
                raise OrchestratorError("--audit-policy 必须同时启用 --audit-reads --audit-strict")
            if (not audit_policy.is_absolute() or ".." in audit_policy.parts
                    or any(path.is_symlink() for path in (audit_policy, *audit_policy.parents)) or not audit_policy.is_file()):
                raise OrchestratorError("--audit-policy 必须是无符号链接的现有普通文件，使用规范绝对路径")
        self.audit_strict, self.audit_policy = audit_strict, audit_policy
        # 只有本次显式选项能改变审计规格；策略路径和文件字节分别绑定，不能换内容却沿用旧步骤。
        for key in (ES.AUDIT_READS_PARAM, ES.AUDIT_STRICT_PARAM, ES.AUDIT_POLICY_PARAM):
            self.params.pop(key, None)
        if audit_reads:
            self.params[ES.AUDIT_READS_PARAM] = "true"
        if audit_strict:
            self.params[ES.AUDIT_STRICT_PARAM] = "true"
        if audit_policy is not None:
            self.params[ES.AUDIT_POLICY_PARAM] = str(audit_policy)
        # 声明级承接（步骤 5 WP3b）：命令单元读集证据的采样方式（--declared-sample）。与审计开关同一规则：只从本次显式选项生成步骤输入，环境里的
        # 同名键不能暗中改变；缺省 off 不写参数、不改命令行，步骤输入与改造前逐字相同。
        try:
            self.declared_sample = _declared_sample_mode(declared_sample)
        except argparse.ArgumentTypeError as error:
            raise OrchestratorError(str(error)) from error
        self.params.pop(ES.DECLARED_SAMPLE_PARAM, None)
        if self.declared_sample != "off":
            self.params[ES.DECLARED_SAMPLE_PARAM] = self.declared_sample
        if gate_parallel is not None and (type(gate_parallel) is not int or gate_parallel <= 0):
            raise OrchestratorError("--gate-parallel 必须是正整数")
        self.gate_parallel = gate_parallel
        # 只从本次显式选项生成步骤输入，防止继承环境中同名键与实际命令不一致；缺省规格保持原样。
        self.params.pop(ES.GATE_PARALLEL_PARAM, None)
        if gate_parallel is not None:
            self.params[ES.GATE_PARALLEL_PARAM] = str(gate_parallel)
        # B5a-08：逐单元承接请求（与 B-09 全集请求、重新执行全集互斥）。放进参数 UNIT_INHERIT_REQUEST：入口步骤据此给入口门禁与
        # pre-A3 两步登记请求文件这项输入（请求一换两步都失效）；不给时参数、步骤表与命令行都与改造前相同。
        self.unit_inherit_request = unit_inherit_request
        if unit_inherit_request is not None:
            self.params["UNIT_INHERIT_REQUEST"] = str(unit_inherit_request)
        # 同一命令续跑时，pre-A3 从真跑变为沿用，实际门禁组合由 entry 变为 full-gates。两份分别批准的请求从首轮就固定
        # 登记，不能到第二轮才替换主请求，也不能改写请求的上下文或来源集合。未提供配套请求时保持原合同的拒绝与回退。
        if unit_inherit_full_gates_request is not None and (unit_inherit_request is None or full_set_request or reexecute_gates):
            raise OrchestratorError("full-gates 逐单元承接请求须与主请求一起提供，且不能与全集请求或重新执行全集同用")
        self.unit_inherit_full_gates_request = unit_inherit_full_gates_request
        self.params.pop(ES.UNIT_INHERIT_FULL_GATES_PARAM, None)
        if unit_inherit_full_gates_request is not None:
            self.params[ES.UNIT_INHERIT_FULL_GATES_PARAM] = str(unit_inherit_full_gates_request)
        self.driver_dir, self.run_dir, self.steps_dir = driver_dir, run_dir, steps_dir
        self.runner, self.masks = runner, dict(masks or {})
        # 入口门禁的模式（D12）：重新执行全集时这一步强制重做、单元一个都不承接。
        self.reexecute_gates = reexecute_gates
        self.full_set_request = full_set_request
        self.full_set_work = full_set_work
        # 外层同名参数不能暗中改变步骤输入；仅显式逐单元承接工作区参与这两步的输入比较。
        self.params.pop(ES.GATE_WORK_PARAM, None)
        if unit_inherit_request is not None and full_set_work is not None:
            _validate_source_work(full_set_work)
            self.params[ES.GATE_WORK_PARAM] = str(full_set_work)
        # 承接的单元重验原轨迹；本次真正执行的单元继续产出相同严格合同的记录。
        self.audit_reads = audit_reads
        # 只交给 VC-0 收口这一步：approve_sha256／approved_by／reason。
        self.approval = {key: value for key, value in (approval or {}).items() if value}
        context_args = {"params": self.params, "driver_dir": driver_dir, "steps_dir": steps_dir}
        self.ctx = ES.Context(**context_args, runner=es_runner) if es_runner else ES.Context(**context_args)
        self.outcomes: dict[str, Outcome] = {}
        self.preflight_failed: list[str] = []
        self.operator_needed: dict[str, str] = {}

    # ---------------------------------------------------------------- 坐标
    def p(self, key: str) -> str:
        value = self.params.get(key)
        if not value:
            raise OrchestratorError(f"参数里没有 {key}")
        return value

    @property
    def data(self) -> Path:
        return Path(self.p("D"))

    @property
    def root(self) -> Path:
        return Path(self.params.get("ENTRY_ROOT") or self.p("D"))

    @property
    def ledger_dir(self) -> Path:
        return self.root / "control" / f"{self.p('UP')}-timing-ledger"

    @property
    def project_ledger(self) -> Path:
        return self.root / "evidence" / "campaigns" / self.spec["project_ledger_dir_name"]

    @property
    def spec(self) -> Mapping[str, Any]:
        """本轮客户端在客户端表里的整行（B5a-06）。"""

        return self.ctx.client_spec

    @property
    def client_argv(self) -> list[str]:
        """追加给共享模块命令行的客户端参数：Codex 为空（与改造前逐字相同），Claude 是 --client claude。"""

        return list(self.spec["client_argv"])

    def record_product(self, step_id: str, name: str) -> Path:
        record = ES.read_record(self.steps_dir, step_id)
        for product in (record or {}).get("products", []):
            if product.get("name") == name and product.get("sha256") not in {None, ES.MISSING}:
                return Path(str(product["path"]))
        raise OrchestratorError(f"步骤 {step_id} 没有可用的产物 {name}（先让这一步通过）")

    def latest_deploy(self) -> Path:
        receipt = self.ctx.latest_deploy_receipt()
        if receipt is None:
            raise OrchestratorError("数据根 control 里没有受管工具部署收据")
        return receipt

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**os.environ, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})}

    def gate_env(self) -> dict[str, str]:
        """只有显式逐单元承接门禁使用后台三缓存；其它步骤和 B-09 仍用原环境。"""
        if self.unit_inherit_request is not None and self.full_set_work is not None:
            work = self.full_set_work
            _validate_source_work(work)
            return self.env({"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"),
                             "GOTMPDIR": str(work / "go-tmp")})
        return self.env()

    def run(self, step_id: str, argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        if step_id in self.masks:
            argv = mask_argv(argv, self.masks[step_id])
        return self.runner(list(argv), cwd or self.data, env or self.env(), self.run_dir / f"{step_id}.log")

    def py(self, module: str, *arguments: str) -> list[str]:
        return [sys.executable, "-B", "-m", module, *arguments]

    def template_argv(self, template: Sequence[str]) -> list[str]:
        """客户端表的参数模板：以 ``$`` 开头的项代入同名参数（没有即报「参数里没有 X」），其余照抄。"""

        return [self.p(item[1:]) if item.startswith("$") else item for item in template]

    def run_json(self, step_id: str, label: str, argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
        """执行一条输出 JSON 的子命令（单独一份日志，不加验收注入），取日志里最后一个 JSON 行。"""

        log = self.run_dir / f"{step_id}.{label}.log"
        rc = self.runner(list(argv), self.data, self.env(), log)
        for line in reversed(log.read_text(encoding="utf-8").splitlines() if log.is_file() else []):
            if line.strip().startswith("{"):
                try:
                    return rc, dict(json.loads(line))
                except ValueError:
                    continue
        return rc, {}

    # ---------------------------------------------------------------- 各步骤
    def step_entry_preflight(self) -> tuple[bool, dict[str, str]]:
        out = self.run_dir / "entry-preflight"
        rc = self.run("entry-preflight", ["bash", str(self.driver_dir / "entry-preflight.sh")],
                      env=self.env({"ENTRY_PREFLIGHT_OUT": str(out)}))
        try:
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.preflight_failed = [str(name) for name in summary.get("failed", [])]
        except (OSError, ValueError):
            self.preflight_failed = ["（便宜检查没有写出汇总）"]
        return rc == 0 and not self.preflight_failed, {}

    def step_policy_compatibility(self) -> tuple[bool, dict[str, str]]:
        out = fresh(Path(self.p("POLICY_COMPAT_RECEIPT")))
        rc = self.run("policy-compatibility", self.py(self.spec["policy_certification_module"], "compatibility", "--previous-policy",
                                                      self.p("PREVIOUS_POLICY"), "--output", str(out), *self.client_argv))
        return rc == 0 and out.is_file(), {"compatibility": str(out)}

    def step_policy_activation(self) -> tuple[bool, dict[str, str]]:
        out = fresh(Path(self.p("POLICY_ACTIVATION")))
        rc = self.run("policy-activation", self.py(self.spec["policy_certification_module"], "activation", "--deployment-receipt",
                                                   str(self.latest_deploy()),
                                                   "--compatibility-receipt", str(self.record_product("policy-compatibility", "compatibility")),
                                                   "--output", str(out), *self.client_argv))
        return rc == 0 and out.is_file(), {"activation": str(out)}

    def _gates_mode(self) -> str:
        """入口门禁的模式：B-09 全集请求（且不是重新执行全集）为 full-set-pass，B5a-08 逐单元承接请求为 unit-inherit，否则
        re-execute（三者互斥，命令行入口已核对）。"""

        if self.full_set_request and not self.reexecute_gates:
            return "full-set-pass"
        return "unit-inherit" if self.unit_inherit_request is not None and not self.reexecute_gates else "re-execute"

    def _unit_inherit_request_for(self, profile: str) -> Path | None:
        """只按本次实际门禁组合选择原文件；授权、来源与实时复核仍由 unit-inherit 合同核对。"""

        if profile == "full-gates" and self.unit_inherit_full_gates_request is not None:
            return self.unit_inherit_full_gates_request
        return self.unit_inherit_request

    def _recorded_bridge_enabled(self) -> bool:
        keys = ("CLAUDE_ENTRY_FULL79_SPEC", "CLAUDE_ENTRY_FULL79_SPEC_SHA256")
        configured = any(self.params.get(key) for key in keys)
        if configured and (self.ctx.client != "claude" or not all(self.params.get(key) for key in keys)):
            raise OrchestratorError("完整79接线只允许Claude，文件与外持SHA必须成对提供")
        return configured

    def _recorded_reuse_products(self, out: Path, certification: Path, *,
                                 result: dict[str, Any] | None = None) -> dict[str, str]:
        """仅登记正式脚本调用原record-reuse的真实输出；同部署not_needed合法，缺证明不能写通过。"""

        if result is None:
            result = json.loads((out / "pre-a3-reuse.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise OrchestratorError("原复用消费端输出必须是对象")
        if result.get("reused_certification") != str(certification):
            raise OrchestratorError("原复用消费端返回的认证坐标不符")
        status, receipt = result.get("status"), result.get("reuse_receipt")
        if status == "not_needed" and receipt is None:
            return {}
        if status not in {"recorded", "existing"} or not isinstance(receipt, str):
            raise OrchestratorError("原复用消费端没有返回有效复用状态")
        path = Path(receipt)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise OrchestratorError("原复用收据缺失或不是普通绝对路径文件")
        return {"reuse-receipt": str(path)}

    def _ordinary_pre_a3_inputs(self, path: Path) -> str:
        """普通认证沿原场景声明重算当前输入；不引入完整79的 carry 规格或承接合同。

        独立九场景的记录必须包含全部当前仓库输入和六项来源；入口额外数据根输入只复核原记录实际声明过的项。
        返回源原件摘要，调用前后都核验，不能用文件仍在代替有效性证明。
        """
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification

        if (not path.is_absolute() or ".." in path.parts
                or any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file()):
            raise OrchestratorError("普通 pre-A3 源认证必须是无链接的规范绝对路径文件")
        raw = path.read_bytes()
        name = "entry_orchestrator_gates_sibling"
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, HERE / "entry_gates.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        gates = sys.modules[name]
        records = gates._unit_records_module()
        tree = (self.full_set_work or self.data.parent / "entry-gates-work") / "test-tree"
        if any(item.is_symlink() for item in (tree, *tree.parents)):
            raise OrchestratorError("普通 pre-A3 当前测试树不得经符号链接转向")
        repo = records.RepoIndex.load(tree)
        if repo.root != tree.resolve() or not repo.clean or repo.head != self.p("ENTRY_COMMIT"):
            raise OrchestratorError("普通 pre-A3 当前测试树不是本轮干净提交")
        try:
            payload = certification.verify_certification(path, client="claude")
            store = certification._RecordStore(Path(payload["record_store"]))
            manifest = store.read(store.manifest_path(payload["unit_manifest"]["run_id"]))
            values = {key: self.params.get(key, "") for key in self.spec["pre_a3_fixture_keys"]}
            fixtures = gates.pre_a3_fixture_resolved("claude", values)
            optional = {item["name"]: item for item in gates.pre_a3_data_root_inputs(self.data, "claude", pre_a3_env=values)}
            cache: dict[Path, Any] = {}
            for scenario in certification.scenario_names("claude"):
                unit_id = certification.scenario_unit_prefix("claude") + scenario
                entry = next(item for item in manifest["units"] if item["unit_id"] == unit_id)
                record = store.read(store.record_path(unit_id, entry["record_sha256"]))
                test_file = certification.scenario_test_module(scenario, "claude").replace(".", "/") + ".py"
                saved = {item["name"]: item for item in record["inputs"] if item["name"] != "reexecute-only-policy"}
                expected = {item["name"]: item for item in records.declared_inputs(
                    repo, gates.pre_a3_inputs(test_file, fixtures, "claude"), cache)}
                expected.update({key: optional[key] for key in saved if key in optional})
                if set(saved) != set(expected) or any(saved[key]["sha256"] != expected[key]["sha256"] for key in expected):
                    raise OrchestratorError("普通 pre-A3 认证的当前输入已变化或声明不完整：" + unit_id)
            if path.read_bytes() != raw or any(item.is_symlink() for item in (path, *path.parents)):
                raise OrchestratorError("普通 pre-A3 源认证在复核期间变化")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, StopIteration) as error:
            raise OrchestratorError(f"普通 pre-A3 认证输入复核失败：{error}") from error
        return hashlib.sha256(raw).hexdigest()

    def entry_gates_and_pre_a3(self, gates_run: bool, pre_a3_run: bool) -> None:
        """入口门禁与 pre-A3 同一次运行（E2-04）：pre-A3 要做就纳入，否则复核并沿用记录里的认证；入口门禁沿用、只有
        pre-A3 要做时，用入口门禁的 pre-a3 组合单独签（E3-02：同一测试树、同一部署一致性核对、同一记录库，场景可承接，
        认证从单元执行记录组装）。"""

        # 已有步骤记录的桥接续跑也登记本次原消费端验证与真实复用收据，不修改旧九单元记录。
        ordinary = (self.ctx.client == "claude" and self.unit_inherit_request is not None and not self.reexecute_gates
                    and not any(self.params.get(key) for key in ("CLAUDE_ENTRY_FULL79_SPEC", "CLAUDE_ENTRY_FULL79_SPEC_SHA256")))
        refresh_reuse = gates_run and (ordinary or (self.ctx.client == "claude" and bool(self.params.get("CLAUDE_ENTRY_FULL79_SPEC"))))
        steps = [step for step, needed in (("entry-gates", gates_run), ("pre-a3", pre_a3_run or refresh_reuse)) if needed]
        audit_args = (["--audit-reads"] if self.audit_reads else []) + (["--audit-strict"] if self.audit_strict else [])
        if self.audit_policy is not None:
            audit_args += ["--audit-policy", str(self.audit_policy)]
        if self.declared_sample != "off":
            # 声明级承接的采样方式随审计参数一并交给入口门禁（entry 与 pre-A3 两次调用共用 audit_args）；缺省 off 不追加。
            audit_args += ["--declared-sample", self.declared_sample]
        for step in steps:
            ES.begin(self.ctx.step(step), self.ctx)
        try:
            activation = self.record_product("policy-activation", "activation")
            bridge = self._recorded_bridge_enabled()
            # 显式接线时已有的独立九认证交给原消费端验真，不能因缺入口步骤记录就换坐标再跑九场景。
            external_present = (bridge or ordinary) and pre_a3_run and os.path.lexists(self.p("PRE_A3_CERTIFICATION"))
            run_scenarios = pre_a3_run and not external_present
            certification = (fresh(Path(self.p("PRE_A3_CERTIFICATION"))) if run_scenarios
                             else Path(self.p("PRE_A3_CERTIFICATION")) if external_present
                             else self.record_product("pre-a3", "certification"))
            source = (self.p("ENTRY_BUNDLE"), self.p("ENTRY_BRANCH"), self.p("ENTRY_COMMIT"))
            self.latest_deploy()
            ordinary_reuse = ordinary and not run_scenarios
            if ordinary_reuse:
                if (not certification.is_absolute() or ".." in certification.parts
                        or any(item.is_symlink() for item in (certification, *certification.parents))
                        or not certification.is_file()):
                    raise OrchestratorError("普通 pre-A3 源认证必须是无链接的规范绝对路径文件")
                source_digest = hashlib.sha256(certification.read_bytes()).hexdigest()
                rc = self.run("pre-a3", self.py(self.spec["pre_a3_module"], "find-reusable", "--certification", str(certification),
                              "--deployment-receipt", str(self.latest_deploy()), "--policy-activation", str(activation), *self.client_argv))
                if rc != 0:
                    raise OrchestratorError("普通 pre-A3 源认证未通过原 find-reusable；拒绝降级重跑九场景")
                if (hashlib.sha256(certification.read_bytes()).hexdigest() != source_digest
                        or any(item.is_symlink() for item in (certification, *certification.parents))):
                    raise OrchestratorError("普通 pre-A3 源认证在原 find-reusable 期间变化")
                # 当前测试树由下面的原入口脚本准备；首次 WORK 为空或旧 HEAD 时不能在此提前要求它存在。
        except (RuntimeError, ES.EntryStepsError, OSError, ValueError) as error:
            for step in steps:
                self.outcomes[step].reasons.append(f"执行前准备失败：{error}")
                self._finish(step, False, {})
            return
        if gates_run:
            out = self.run_dir / "entry-gates"
            inherit_request = self._unit_inherit_request_for("entry" if run_scenarios else "full-gates")
            rc = self.run("entry-gates", ["bash", str(self.driver_dir / "entry-gates.sh"), "--profile", "entry", "--out", str(out),
                                          "--policy-activation", str(activation), "--pre-a3-certification", str(certification),
                                          "--pre-a3-mode", "run" if run_scenarios else "present",
                                          "--mode", self._gates_mode(),
                                          *(["--full-set-request", str(self.full_set_request)] if self.full_set_request else []),
                                          *(["--unit-inherit-request", str(inherit_request)] if inherit_request else []),
                                          *(["--work", str(self.full_set_work)] if self.full_set_work else []),
                                          *(["--parallel", str(self.gate_parallel)] if self.gate_parallel is not None else []),
                                          *audit_args, *source], env=self.gate_env())
            summary = out / "entry-gates.json"
            try:
                status = json.loads(summary.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError):
                status = None
            # 退出码 1 有两种来源：门禁项没过（或门禁后测试树不干净），或者门禁都过了只是 pre-A3 没签成。
            pre_a3_ok = (not pre_a3_run) or certification.is_file()
            gates_passed = status == "passed" and (rc == 0 or (rc == 1 and not pre_a3_ok))
            if rc == 3:
                self.outcomes["entry-gates"].reasons.append("入口门禁准备或执行失败（退出码 3），没有门禁结论；见日志")
            self._finish("entry-gates", gates_passed, {
                "summary": str(summary), "p0-test-capture-tools": str(out / "p0" / "test-capture-tools.json"),
                "p0-check-egress-spec": str(out / "p0" / "check-egress-spec.json")})
        else:
            out = self.run_dir / "pre-a3-gates"
            rc = self.run("pre-a3", ["bash", str(self.driver_dir / "entry-gates.sh"), "--profile", "pre-a3", "--out", str(out),
                                "--policy-activation", str(activation), "--pre-a3-certification", str(certification),
                                "--pre-a3-mode", "run" if run_scenarios else "present", "--mode", self._gates_mode(),
                                          *(["--full-set-request", str(self.full_set_request)] if self.full_set_request else []),
                                *(["--unit-inherit-request", str(self.unit_inherit_request)] if self.unit_inherit_request else []),
                                *(["--work", str(self.full_set_work)] if self.full_set_work else []),
                                *(["--parallel", str(self.gate_parallel)] if self.gate_parallel is not None else []),
                                *audit_args, *source], env=self.gate_env())
        if pre_a3_run or refresh_reuse:
            reuse_products: dict[str, str] = {}
            pre_a3_passed = certification.is_file()
            if bridge:
                try:
                    if rc not in {0, 1}:
                        raise OrchestratorError("正式入口未完成认证与复用消费，不能凭已有文件记通过")
                    reuse_products = self._recorded_reuse_products(out, certification)
                except (OrchestratorError, OSError, ValueError, TypeError) as error:
                    pre_a3_passed = False
                    self.outcomes["pre-a3"].reasons.append(f"完整79接线复核失败：{error}")
            elif ordinary_reuse:
                try:
                    if rc not in {0, 1}:
                        raise OrchestratorError("正式入口未完成普通认证消费")
                    if self._ordinary_pre_a3_inputs(certification) != source_digest:
                        raise OrchestratorError("普通 pre-A3 源认证在入口运行期间变化")
                    reuse_rc, result = self.run_json("pre-a3", "reuse", self.py(
                        self.spec["pre_a3_module"], "record-reuse", "--certification", str(certification),
                        "--deployment-receipt", str(self.latest_deploy()), "--policy-activation", str(activation),
                        "--receipt-root", str(certification.parent), *self.client_argv))
                    if (reuse_rc != 0 or hashlib.sha256(certification.read_bytes()).hexdigest() != source_digest
                            or any(item.is_symlink() for item in (certification, *certification.parents))):
                        raise OrchestratorError("普通 pre-A3 原复用消费失败或源原件发生变化")
                    reuse_products = self._recorded_reuse_products(out, certification, result=result)
                except (RuntimeError, OSError, ValueError, TypeError) as error:
                    pre_a3_passed = False
                    self.outcomes["pre-a3"].reasons.append(f"普通 pre-A3 复核失败：{error}")
            if not certification.is_file():
                self.outcomes["pre-a3"].reasons.append(
                    f"pre-A3 认证没有签发（没通过的认证只写旁路文件 {certification.parent}/*.failed-*.json）")
            self._finish("pre-a3", pre_a3_passed, {"certification": str(certification), **reuse_products})

    def step_zero_request_smoke(self) -> tuple[bool, dict[str, str]]:
        stamp = self.p("STAMP")
        for serial in range(1, 1000):
            suffix = "" if serial == 1 else f"-r{serial}"
            staging = self.data / "staging" / f"zero-request-smoke-{stamp}{suffix}"
            output = self.data / "audit" / f"zero-request-smoke-{stamp}{suffix}.json"
            if not os.path.lexists(staging) and not os.path.lexists(output):
                break
        rc = self.run("zero-request-smoke", self.py(self.spec["zero_request_smoke_module"],
                                                    "--staging-root", str(staging), "--output", str(output)))
        return rc == 0 and output.is_file(), {"receipt": str(output)}

    def step_atomic_double(self) -> tuple[bool, dict[str, str]]:
        root = fresh(self.data / "staging" / f"{self.spec['atomic_root_prefix']}{self.p('ROUND')}-{self.p('STAMP')}")
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        # 共享收据模块按 --client 取客户端画像（B5a-09 续；Codex 为空，命令行与改造前相同）。
        rc = self.run("atomic-double", ["docker", "exec", "--env", "PYTHONPATH=/capture", "--env", "PYTHONDONTWRITEBYTECODE=1",
                                        "--workdir", "/capture", "capture-cli", "python3", "-m",
                                        self.spec["atomic_rehearsal_module"],
                                        "atomic-double-collect", "--evidence-root", f"/capture/staging/{root.name}",
                                        "--output", "receipt.json", *self.client_argv])
        receipt = root / "receipt.json"
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    def step_ledger(self) -> tuple[bool, dict[str, str]]:
        deadline = datetime.strptime(self.p("PROJECT_DEADLINE_UTC"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        total = int((deadline - datetime.now(timezone.utc)).total_seconds() // 60) - 5   # 留 5 分钟余量（同 stage1.sh）
        budgets = [argument for item in self.params.get("STAGE_BUDGETS", "").split() for argument in ("--stage-budget-minutes", item)]
        self.ledger_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Campaign 用途（B5b-01）：客户端表登记了用途参数键的客户端（Claude）按参数取（parse_env 派生缺省值），预检 plan 的模板
        # 代入同一参数；Codex 没有这个键，固定 production_replacement，命令行与改造前相同。
        purpose_key = self.spec["campaign_purpose_key"]
        purpose = self.p(purpose_key) if purpose_key else "production_replacement"
        rc = self.run("ledger", self.py(self.spec["timing_ledger_module"], "create",
                                        "--ledger-dir", str(self.ledger_dir), "--upgrade-id", self.p("UP"),
                                        "--baseline-version", self.p("BASELINE_VERSION"), "--target-version", self.p("TARGET_VERSION"),
                                        "--campaign-purpose", purpose,
                                        "--evidence-decision", self.params.get("EVIDENCE_DECISION") or "recapture",
                                        "--project-ledger-dir", str(self.project_ledger), "--total-budget-minutes", str(total), *budgets,
                                        *self.client_argv))
        ledger = self.ledger_dir / "ledger.json"
        return rc == 0 and ledger.is_file(), {"ledger": str(ledger)}

    def step_environment_p0(self) -> tuple[bool, dict[str, str]]:
        root = fresh(self.root / "environment" / f"{self.p('UP')}-p0")
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        module = self.spec["environment_receipt_module"]
        rc = self.run("environment-p0", self.py(module, "collect", "--evidence-root", str(root), "--output", "facts.json",
                                                "--phase", "p0", "--subject-id", self.p("UP"),
                                                self.spec["environment_probe_version_option"], self.p("TARGET_VERSION")))
        if rc == 0:
            rc = self.run("environment-p0", self.py(module, "finalize", "--evidence-root", str(root), "--facts", "facts.json",
                                                    "--output", "receipt.json"))
        receipt = root / "receipt.json"
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    def step_ledger_checkpoint(self) -> tuple[bool, dict[str, str]]:
        ledger_dir = self.record_product("ledger", "ledger").parent
        checkpoint = fresh(ledger_dir / "receipts" / f"vc0-input-{self.p('ROUND')}-preflight-{self.p('STAMP')}.json")
        rc = self.run("ledger-checkpoint", self.py(self.spec["timing_ledger_module"], "checkpoint",
                                                   "--ledger-dir", str(ledger_dir),
                                                   "--output", checkpoint.relative_to(ledger_dir).as_posix()))
        return rc == 0 and checkpoint.is_file(), {"checkpoint": str(checkpoint)}

    def step_preflight_plan(self) -> tuple[bool, dict[str, str]]:
        ledger_dir = self.record_product("ledger", "ledger").parent
        checkpoint = self.record_product("ledger-checkpoint", "checkpoint")
        environment = self.record_product("environment-p0", "receipt")
        receipt = json.loads(self.latest_deploy().read_text(encoding="utf-8"))
        container = self.spec["deploy_receipt_identity_key"]
        headsha = str((receipt if container is None else receipt[container])["tool_files_sha256"])[:9]
        campaigns = self.root / "evidence" / "campaigns"
        campaigns.mkdir(parents=True, exist_ok=True, mode=0o700)
        pre = fresh(campaigns / f"{self.p('CAMPAIGN_PREFIX')}-preflight-vc5-{self.p('ROUND')}-{headsha}-{self.p('STAMP')}")
        p = self.p
        template = tuple(self.spec["preflight_plan_argv"])
        if template:
            # 客户端表给出了模板的客户端（Claude，B2-11 第二部分）：账本、checkpoint、环境收据与作业坐标由这里按前序步骤
            # 记录给出，其余参数按模板代入；证据决定为 reuse 时追加只读复用的前序。
            argv = self.py(self.spec["orchestrator_module"], "plan",
                           "--campaign-dir", str(pre), "--campaign-id", pre.name, "--timing-ledger-dir", str(ledger_dir),
                           "--timing-receipt", checkpoint.relative_to(ledger_dir).as_posix(),
                           "--arm64-environment-root", str(environment.parent), "--arm64-environment-receipt", environment.name,
                           *self.template_argv(template))
            if (self.params.get("EVIDENCE_DECISION") or "recapture") == "reuse":
                argv += self.template_argv(self.spec["preflight_plan_reuse_argv"])
            rc = self.run("preflight-plan", argv)
            manifest = pre / "campaign.json"
            return rc == 0 and manifest.is_file(), {"campaign": str(manifest)}
        # 下面是 Codex 编排器 plan 的参数形状（code-mode-host、capture／relay 两处 codex 二进制、Codex 账号）；Codex 的模板为空，
        # 命令行与改造前逐字相同。
        argv = self.py(self.spec["orchestrator_module"], "plan",
                       "--campaign-dir", str(pre), "--campaign-id", pre.name, "--baseline-version", p("BASELINE_VERSION"),
                       "--target-version", p("TARGET_VERSION"), "--campaign-mode", "preflight_only",
                       "--campaign-purpose", "production_replacement", "--timing-ledger-dir", str(ledger_dir),
                       "--timing-receipt", checkpoint.relative_to(ledger_dir).as_posix(),
                       "--arm64-environment-root", str(environment.parent), "--arm64-environment-receipt", environment.name,
                       "--baseline-source", p("BASELINE_SOURCE"), "--target-source", p("TARGET_SOURCE"),
                       "--baseline-evidence", p("ACTIVE_PROFILE"), "--target-sha256", p("CODEX_BIN_SHA256"),
                       "--target-package", p("TARGET_PACKAGE"), "--target-package-sha256", p("OFFICIAL_ASSET_SHA256"),
                       "--target-code-mode-host-sha256", p("TARGET_CODE_MODE_HOST_SHA256"), "--runtime-image", p("CAPTURE_RUNTIME_IMAGE"),
                       "--rule-manifest", p("BASELINE_RULES_JSON"), "--scenario-manifest", p("BASELINE_SCENARIOS_JSON"),
                       "--target-scenario-manifest", p("SCENARIOS_JSON"), "--capture-codex-bin", p("CODEX_BIN"),
                       "--relay-codex-bin", p("CODEX_BIN"), "--suite", "full", "--model", p("MAIN_MODEL"), "--lite-model", p("LITE_MODEL"),
                       "--codex-account-id", p("CODEX_ACCOUNT_ID"), "--api-key-id", p("API_KEY_ID"),
                       "--live-attestation-compose-dir", p("COMPOSE_DIR"),
                       "--live-attestation-compose-files", f"{p('COMPOSE_DIR')}/docker-compose.yml")
        rc = self.run("preflight-plan", argv)
        manifest = pre / "campaign.json"
        return rc == 0 and manifest.is_file(), {"campaign": str(manifest)}

    def step_job_rehearsal(self) -> tuple[bool, dict[str, str]]:
        campaign = self.record_product("preflight-plan", "campaign").parent
        base = self.root / "control" / f"{self.p('CAMPAIGN_PREFIX')}-job-rehearsal-vc5-{self.p('ROUND')}-{self.p('STAMP')}"
        previous: list[str] = []
        record = ES.read_record(self.steps_dir, "job-rehearsal")
        if record and record.get("status") == "failed":
            receipt = next((Path(str(item["path"])) for item in record.get("products", []) if item.get("name") == "receipt"), None)
            if receipt is not None and receipt.is_file():
                # 工具已有的「只重跑失败项」：上一份失败收据所在的证据根作为前序，通过的作业按结果键复用。
                previous = ["--previous-receipt", receipt.name, "--previous-receipt-root", str(receipt.parent), "--rerun-failed"]
        root = fresh(base)
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        module = self.spec["job_rehearsal_module"]
        # 共享收据模块按 --client 取客户端适配器（B2-10；Codex 为空，命令行与改造前相同）。
        rc = self.run("job-rehearsal", self.py(module, "collect", "--campaign-dir", str(campaign), "--evidence-root", str(root),
                                               "--output", "facts.json", *previous, *self.client_argv))
        if rc == 0:
            rc = self.run("job-rehearsal", self.py(module, "finalize", "--evidence-root", str(root), "--facts", "facts.json",
                                                   "--output", "receipt.json", *self.client_argv))
        receipt = root / "receipt.json"
        # 有作业失败时工具照样写收据并退出 0：结论按收据里的状态（沿用前同样核对，E2-05）。
        try:
            passed = json.loads(receipt.read_text(encoding="utf-8")).get("status") == "passed"
        except (OSError, ValueError):
            passed = False
        if rc == 0 and receipt.is_file() and not passed:
            self.outcomes["job-rehearsal"].reasons.append("Job 演练收据不是通过（有作业失败），下次执行只重跑失败的作业")
        return rc == 0 and passed, {"receipt": str(receipt)}

    def step_client_launch_probe(self) -> tuple[bool, dict[str, str]]:
        campaign = self.record_product("preflight-plan", "campaign").parent
        probe = self.root / "control" / f"{self.p('CAMPAIGN_PREFIX')}-client-launch-probe-{self.p('ROUND')}-{self.p('STAMP')}"
        script = self.driver_dir / "client_launch_probe.py"
        rc = self.run("client-launch-probe", [sys.executable, "-B", str(script), "run", "--campaign-dir", str(campaign),
                                              "--output-dir", str(probe), "--data-root", str(self.data), *self.client_argv])
        if rc == 0:
            rc = self.run("client-launch-probe", [sys.executable, "-B", str(script), "verify", "--output-dir", str(probe),
                                                  "--campaign-dir", str(campaign), *self.client_argv])
        # 报告登记进步骤记录（E2-07）：收口前预检按记录里的这份报告再核验一次。
        return rc == 0, {"report": str(probe / "report.json")}

    def step_release_certification(self) -> tuple[bool, dict[str, str]]:
        deploy, activation = self.latest_deploy(), self.record_product("policy-activation", "activation")
        pre_a3 = self.record_product("pre-a3", "certification")
        job = self.record_product("job-rehearsal", "receipt")
        atomic = self.record_product("atomic-double", "receipt")
        out = fresh(Path(self.p("RELEASE_CERTIFICATION")))
        # pre-A3 认证是跨部署复用来的时，发布认证要绑定复用收据（同 stage2.sh：record-reuse 幂等，本次部署下签发的为空）。
        rc, reused = self.run_json("release-certification", "reuse", self.py(
            self.spec["pre_a3_module"], "record-reuse", "--certification", str(pre_a3), "--deployment-receipt", str(deploy),
            "--policy-activation", str(activation), "--receipt-root", str(pre_a3.parent), *self.client_argv))
        if rc != 0:
            self.outcomes["release-certification"].reasons.append("pre-A3 复用收据登记失败，见日志")
            return False, {"certification": str(out)}
        reuse = str(reused.get("reuse_receipt") or "")
        arguments = ["issue", "--deployment-receipt", str(deploy), "--pre-a3-certification", str(pre_a3),
                     *(["--pre-a3-reuse-receipt", reuse] if reuse else []), "--policy-activation", str(activation),
                     "--job-rehearsal-root", str(job.parent), "--job-rehearsal-receipt", job.name,
                     "--atomic-rehearsal-root", str(atomic.parent), "--atomic-rehearsal-receipt", atomic.name,
                     "--atomic-container", "capture-cli", "--data-root", str(self.data), "--output", str(out), *self.client_argv]
        rc = self.run("release-certification", self.py(self.spec["release_certification_module"], *arguments))
        if rc == 0:
            rc = self.run("release-certification", self.py(self.spec["release_certification_module"], "verify",
                                                           "--certification", str(out), *self.client_argv))
        return rc == 0 and out.is_file(), {"certification": str(out)}

    def step_p0_receipt(self) -> tuple[bool, dict[str, str]]:
        """P0 门禁收据（E2-07 固定步骤，原来每轮手写）：两份离线门禁证据取自入口门禁那一次运行，加发布认证与回退依据；
        subject 取计时账本计划（升级 ID、用途、两个版本），签发后立即重放。"""

        # 入口门禁在全集通过模式下承接了单元时，两份证据是以运行清单为证据的 v2（E3-03），签发时收据模块按记录库逐条
        # 重验；记录库或已发布的清单缺失、测试树五摘要算不出来时入口门禁不写证据：照总摘要报原因。
        entry = json.loads(self.record_product("entry-gates", "summary").read_text(encoding="utf-8"))
        withheld = entry.get("p0_evidence_withheld") or {}
        if withheld:
            for gate_id, why in sorted(withheld.items()):
                self.outcomes["p0-receipt"].reasons.append(f"入口门禁的 {gate_id} 没有可签 P0 收据的证据：{why}"
                                                           f"（编排器带 --reexecute-gates 即重新执行全集）")
            return False, {}
        evidence_sources = {
            "test-capture-tools.json": self.record_product("entry-gates", "p0-test-capture-tools"),
            "check-egress-spec.json": self.record_product("entry-gates", "p0-check-egress-spec"),
            "release-certification.json": self.record_product("release-certification", "certification"),
        }
        rollback = Path(self.p("P0_ROLLBACK_EVIDENCE"))
        if not rollback.is_file() or rollback.is_symlink():
            raise OrchestratorError(f"回退依据（P0_ROLLBACK_EVIDENCE）不是普通文件：{rollback}")
        evidence_sources[f"rollback-{rollback.name}"] = rollback
        plan = json.loads(self.record_product("ledger", "ledger").read_text(encoding="utf-8"))
        root = fresh(self.root / "control" / f"{self.p('CAMPAIGN_PREFIX')}-p0-gate-{self.p('ROUND')}-{self.p('STAMP')}")
        (root / "evidence").mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        for name, source in evidence_sources.items():
            target = root / "evidence" / name
            shutil.copyfile(source, target)
            target.chmod(0o600)
        gates: dict[str, dict[str, Any]] = {}
        for gate_id in ("check-egress-spec", "test-capture-tools"):
            evidence = json.loads((root / "evidence" / f"{gate_id}.json").read_text(encoding="utf-8"))
            if (evidence.get("gate_id") != gate_id or evidence.get("status") != "passed" or evidence.get("exit_code") != 0
                    or evidence.get("failed") != 0 or evidence.get("unexpected_skip") != 0):
                self.outcomes["p0-receipt"].reasons.append(f"入口门禁的 {gate_id} 证据不是通过，不能签 P0 收据")
                return False, {}
            gates[gate_id] = {"gate_id": gate_id, "kind": "public", "command": ["make", gate_id], "exit_code": 0,
                              "passed": evidence["passed"], "failed": 0, "approved_skip": evidence["approved_skip"],
                              "unexpected_skip": 0}
        release_sha256 = hashlib.sha256((root / "evidence" / "release-certification.json").read_bytes()).hexdigest()
        facts = {
            "schema_version": self.spec["p0_facts_schema"],
            "kind": "p0_gate",
            "subject": {"upgrade_id": plan["upgrade_id"], "campaign_id": None, "campaign_purpose": plan["campaign_purpose"],
                        "baseline_version": plan["baseline_version"], "target_version": plan["target_version"],
                        "candidate_id": None, "attempt_id": None},
            "assertions": {"offline_gates": [gates["check-egress-spec"], gates["test-capture-tools"]], "tool_blockers": [],
                           "rollback_ready": True, "release_certification_sha256": release_sha256},
            "evidence": [
                {"path": "evidence/check-egress-spec.json", "role": "check_egress_spec"},
                {"path": "evidence/release-certification.json", "role": "release_certification"},
                {"path": f"evidence/rollback-{rollback.name}", "role": "rollback"},
                {"path": "evidence/test-capture-tools.json", "role": "test_capture_tools"},
            ],
        }
        facts_path = root / "p0-facts.json"
        facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        facts_path.chmod(0o600)
        rc = self.run("p0-receipt", self.py(self.spec["p0_receipt_module"], "finalize", "--evidence-root", str(root),
                                            "--facts", "p0-facts.json", "--output", "p0-receipt.json"))
        if rc == 0:
            rc = self.run("p0-receipt", self.py(self.spec["p0_receipt_module"], "replay", "--evidence-root", str(root),
                                                "--receipt", "p0-receipt.json"))
        receipt = root / "p0-receipt.json"
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    def closeout_paths(self) -> dict[str, Path]:
        """收口的固定坐标：Formal 与监督器状态目录在产物根下（状态目录放在控制根下一层，VC-1 对账才找得到首批父 run）。"""

        new = self.p("NEW")
        return {"formal": self.root / "evidence" / "campaigns" / new, "state": self.root / "control" / f"{new}-supervisor"}

    def step_vc0_closeout(self) -> tuple[bool, dict[str, str]]:
        """VC-0 收口（E2-07）：Formal 未建时先复核启动探测报告，收口模块自己做其余三份输入与工具身份的只读预检；
        然后由它按现场判定新建、续作或交给 VC-1 对账恢复链。参数全部来自本轮参数文件与前序步骤记录。"""

        if self.root.resolve() != self.data.resolve():
            # 演练根（数据根 staging 下）不做 VC-0 收口：首批是真实官方取证，会发正式请求；收口的续作由录制回放链验收。
            raise OrchestratorError("产物根是验收演练目录（ENTRY_ROOT 不是数据根），不做 VC-0 收口：首批会发正式请求；用 --to p0-receipt")
        paths = self.closeout_paths()
        preflight = self.record_product("preflight-plan", "campaign").parent
        if not ES.formal_built(self.ctx):
            report = self.record_product("client-launch-probe", "report")
            rc = self.run("vc0-closeout", [sys.executable, "-B", str(self.driver_dir / "client_launch_probe.py"), "verify",
                                           "--output-dir", str(report.parent), "--campaign-dir", str(preflight), *self.client_argv])
            if rc != 0:
                self.outcomes["vc0-closeout"].reasons.append("收口前预检：启动探测报告复核没过")
                return False, {}
        p0 = self.record_product("p0-receipt", "receipt")
        audit_root = self.root / "audit"
        audit_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit = fresh(audit_root / f"{self.p('CAMPAIGN_PREFIX')}-vc0-closeout-{self.p('ROUND')}-{self.p('STAMP')}")
        paths["formal"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        arguments = [
            "--preflight-campaign-dir", str(preflight), "--formal-campaign-dir", str(paths["formal"]),
            "--formal-campaign-id", self.p("NEW"), "--p0-gate-root", str(p0.parent), "--p0-gate-receipt", p0.name,
            "--managed-tool-deploy-receipt", str(self.latest_deploy()),
            "--release-certification", str(self.record_product("release-certification", "certification")),
            "--supervisor-state-dir", str(paths["state"]), "--audit-dir", str(audit),
            "--control-root", str(self.data / "control"),
            # 上限后的恢复用的修复证据：修复提交是本轮部署的源码提交，离线回归收据是本次部署下入口门禁那一次运行的汇总。
            "--fix-commit", self.p("ENTRY_COMMIT"),
            "--regression-receipt", str(self.record_product("entry-gates", "summary")),
        ]
        for key, option in (("approve_sha256", "--approve-sha256"), ("approved_by", "--approved-by"), ("reason", "--reason")):
            if self.approval.get(key):
                arguments += [option, self.approval[key]]
        # 收口模块同步派发 VC-1 首批（真实采集）：开始前向统一调度执行器申请整机（后台验证停派、等在跑单元结束才批准），
        # 收口返回后归还（E4-01，与 vc-batch.sh 同一做法）。
        owner = f"vc0-closeout-{self.p('NEW')}"
        executor = [sys.executable, "-B", str(self.driver_dir / "unit_executor.py")]
        rc, grant = self.run_json("vc0-closeout", "acquire", [*executor, "acquire", "--owner", owner, "--owner-pid", str(os.getpid()),
                                                              "--timeout", str(MACHINE_ACQUIRE_TIMEOUT_SECONDS)])
        if rc != 0 or not grant.get("granted_at_utc"):
            self.outcomes["vc0-closeout"].reasons.append("向统一调度执行器申请整机没有批准（超时或已有其它采集占用），没有收口")
            return False, {}
        try:
            rc, result = self.run_json("vc0-closeout", "closeout", self.py(self.spec["closeout_module"], *arguments))
        finally:
            self.run_json("vc0-closeout", "release", [*executor, "release", "--owner", owner])
        receipt = audit / "receipt.json"
        if rc == CLOSEOUT_NEEDS_OPERATOR:
            message = str(result.get("message") or "收口需要人工处理，见日志")
            self.outcomes["vc0-closeout"].reasons.append(message)
            if result.get("next_command"):
                self.outcomes["vc0-closeout"].reasons.append(f"下一步：{result['next_command']}")
            self.operator_needed["vc0-closeout"] = str(result.get("kind") or "operator")
            return False, {}
        if rc != 0:
            self.outcomes["vc0-closeout"].reasons.append(f"收口失败，诊断见 {audit}/failure.json")
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    # ---------------------------------------------------------------- 编排
    def _finish(self, step_id: str, passed: bool, products: Mapping[str, str]) -> None:
        outcome = self.outcomes[step_id]
        outcome.action, outcome.status = "执行", "passed" if passed else "failed"
        outcome.products = dict(products)
        outcome.log = str(self.run_dir / f"{step_id}.log")
        if not passed and not outcome.reasons:
            outcome.reasons.append("执行失败，见日志")
        step = self.ctx.step(step_id)
        record_products = {name: path for name, path in products.items() if not passed or Path(path).exists()}
        try:
            ES.finish(step, self.ctx, status=outcome.status, products=record_products)
        except ES.EntryStepsError as error:
            # 记成通过的条件没满足（产物缺失或内容不合格）：改记失败，原因写明。
            outcome.status = "failed"
            outcome.reasons.append(str(error))
            ES.finish(step, self.ctx, status="failed", products=record_products)

    def execute(self, step_id: str) -> None:
        step = self.ctx.step(step_id)
        ES.begin(step, self.ctx)
        waiting = CT.pending(self.ctx.client, f"step:{step_id}")
        if waiting:
            # 客户端表登记为尚未接入（B5a-06）：不执行，记失败并写明落实它的工作项；接入后从表里删去即照常执行。
            self.outcomes[step_id].reasons.append(f"本轮客户端（{self.ctx.client}）的这一步尚未接入：{waiting}")
            self._finish(step_id, False, {})
            return
        handler = getattr(self, "step_" + step_id.replace("-", "_"))
        try:
            passed, products = handler()
        except (OrchestratorError, ES.EntryStepsError, OSError, ValueError, KeyError) as error:
            self.outcomes[step_id].reasons.append(f"执行前准备失败：{error}")
            passed, products = False, {}
        self._finish(step_id, passed, products)

    def blocked_by(self, step_id: str) -> list[str]:
        deps = list(self.ctx.step(step_id).upstream) + list(EXEC_DEPS.get(step_id, ()))
        blockers = [dep for dep in deps if dep in self.outcomes and
                    (self.outcomes[dep].status == "failed" or self.outcomes[dep].action in {"被阻塞", "阻塞"})]
        blockers += [f"便宜检查 {check}" for check, steps in PREFLIGHT_BLOCKS.items()
                     if check in self.preflight_failed and step_id in steps]
        return blockers

    def orchestrate(self, *, plan_only: bool = False, from_step: str | None = None, to_step: str | None = None,
                    formal: bool | None = None) -> dict[str, Any]:
        is_formal = ES.formal_built(self.ctx) if formal is None else formal
        force = None if is_formal else {"entry-gates": "B-09：每次开工重新核对全集承接身份、时效与撤销；不得沿用旧步骤缓存"}
        evaluation = ES.evaluate(self.ctx, is_formal_built=is_formal, force=force)
        decisions = {step["step_id"]: step for step in evaluation["steps"]}
        start = ORDER.index(from_step) if from_step else 0
        stop = ORDER.index(to_step) if to_step else len(ORDER) - 1
        if start > stop:
            raise OrchestratorError("起点在终点之后")
        for index, step_id in enumerate(ORDER):
            decision = decisions[step_id]
            self.outcomes[step_id] = Outcome(step_id, decision["decision"], reasons=list(decision["reasons"]))
            waiting = CT.pending(self.ctx.client, f"step:{step_id}")
            if waiting and decision["decision"] in {"run", "live"}:
                self.outcomes[step_id].reasons.append(f"本轮客户端（{self.ctx.client}）的这一步尚未接入：{waiting}")
            if index < start and decision["decision"] in {"run", "blocked"}:
                raise OrchestratorError(f"起点 {from_step} 之前的步骤 {step_id} 要重做（{'；'.join(decision['reasons'])}），不能从这里开始")
        result = {"schema_version": RUN_SCHEMA, "started_at_utc": _now(), "formal_built": is_formal, "plan_only": plan_only,
                  "from": from_step, "to": to_step, "run_dir": str(self.run_dir), "steps_dir": str(self.steps_dir)}
        if plan_only:
            for outcome in self.outcomes.values():
                outcome.action = "只判定"
            return self._summary(result, 0)
        if is_formal:
            # Formal 建成后：创建链冻结，实时检查也不再做；只剩 VC-0 收口按判定续作（E2-07：补阶段事件后派发首批，
            # 或首批已派发时交给 VC-1 对账恢复链），收口已完成就沿用。
            for step_id, outcome in self.outcomes.items():
                if step_id != "vc0-closeout":
                    outcome.action = "冻结"
            closing = self.outcomes["vc0-closeout"]
            if closing.decision == "reuse":
                closing.action = "沿用"
                result["stopped_because"] = "Formal Campaign 已建，收口已完成；入口结束"
                return self._summary(result, 0)
            self.execute("vc0-closeout")
            return self._after_closeout(result)
        in_range = set(ORDER[start:stop + 1])
        for step_id in ORDER:
            if step_id not in in_range:
                self.outcomes[step_id].action = "范围外"
        # 建账本之前：一次报全。入口门禁与 pre-A3 成对处理（同一次运行）。
        for step_id in ORDER:
            if step_id not in PRE_LEDGER or step_id not in in_range or step_id == "pre-a3":
                continue
            if step_id == "entry-gates":
                self._entry_pair(in_range)
                continue
            outcome = self.outcomes[step_id]
            if outcome.decision in {"reuse", "frozen"}:
                outcome.action = "沿用" if outcome.decision == "reuse" else "冻结"
                continue
            blockers = self.blocked_by(step_id)
            if blockers:
                outcome.action = "被阻塞"
                outcome.reasons.append(f"依赖没通过：{', '.join(blockers)}")
                continue
            self.execute(step_id)
        pre_failures = [step for step in ORDER if step in PRE_LEDGER and step in in_range and
                        (self.outcomes[step].status == "failed" or self.outcomes[step].action == "被阻塞")]
        if pre_failures:
            for step_id in ORDER:
                if step_id not in PRE_LEDGER and step_id in in_range:
                    self.outcomes[step_id].action = "未到达"
            result["stopped_because"] = f"建账本之前有步骤没通过：{', '.join(pre_failures)}；全部通过才建账本"
            return self._summary(result, 1)
        # 建账本之后：按序，失败即停。
        for position, step_id in enumerate(ORDER):
            if step_id in PRE_LEDGER or step_id not in in_range:
                continue
            outcome = self.outcomes[step_id]
            if outcome.decision in {"reuse", "frozen"}:
                outcome.action = "沿用" if outcome.decision == "reuse" else "冻结"
                continue
            if outcome.decision == "blocked":
                outcome.action = "阻塞"
                self._mark_rest(position)
                result["stopped_because"] = f"{step_id} 被阻塞：{'；'.join(outcome.reasons)}"
                return self._summary(result, 3)
            self.execute(step_id)
            if step_id == "vc0-closeout":
                return self._after_closeout(result)
            if outcome.status == "failed":
                self._mark_rest(position)
                result["stopped_because"] = f"{step_id} 没通过；修好后重新执行同一条命令，从这一步续跑"
                return self._summary(result, 1)
        return self._summary(result, 0)

    def _after_closeout(self, result: dict[str, Any]) -> dict[str, Any]:
        """收口这一步之后：需要批准或人工处置时标「阻塞」（退出码 3），失败为 1，通过为 0。"""

        outcome = self.outcomes["vc0-closeout"]
        if "vc0-closeout" in self.operator_needed:
            # 步骤记录里记成没通过（下次执行这一步要重做）；汇总里显示「阻塞」，不是失败。
            outcome.action, outcome.status = "阻塞", ""
            result["stopped_because"] = (f"VC-0 收口需要处理（{self.operator_needed['vc0-closeout']}）："
                                         + "；".join(outcome.reasons))
            return self._summary(result, 3)
        if outcome.status == "failed":
            result["stopped_because"] = "VC-0 收口没通过；修好后重新执行同一条命令续作（同一 Formal ID、同一账本）"
            return self._summary(result, 1)
        return self._summary(result, 0)

    def _entry_pair(self, in_range: set[str]) -> None:
        gates, pre_a3 = self.outcomes["entry-gates"], self.outcomes["pre-a3"]
        needed = {"entry-gates": "entry-gates" in in_range and gates.decision == "run",
                  "pre-a3": "pre-a3" in in_range and pre_a3.decision == "run"}
        for step_id, outcome in (("entry-gates", gates), ("pre-a3", pre_a3)):
            if step_id in in_range and not needed[step_id]:
                outcome.action = "沿用" if outcome.decision == "reuse" else "冻结"
        if not any(needed.values()):
            return
        blockers: list[str] = []
        for step_id in ("entry-gates", "pre-a3"):
            if needed[step_id]:
                blockers += [item for item in self.blocked_by(step_id) if item not in blockers]
        if blockers:
            for step_id, outcome in (("entry-gates", gates), ("pre-a3", pre_a3)):
                if needed[step_id]:
                    outcome.action = "被阻塞"
                    outcome.reasons.append(f"依赖没通过：{', '.join(blockers)}")
            return
        self.entry_gates_and_pre_a3(needed["entry-gates"], needed["pre-a3"])

    def _mark_rest(self, position: int) -> None:
        for step_id in ORDER[position + 1:]:
            if self.outcomes[step_id].action == "未执行":
                self.outcomes[step_id].action = "未到达"

    def _summary(self, result: dict[str, Any], code: int) -> dict[str, Any]:
        result.update({"completed_at_utc": _now(), "exit_code": code, "steps": [self.outcomes[step].as_json() for step in ORDER],
                       "preflight_failed": self.preflight_failed})
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.run_dir / "run.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return result


def print_summary(result: Mapping[str, Any]) -> None:
    print(f"入口编排（{'只判定' if result['plan_only'] else '执行'}）：{len(result['steps'])} 步" +
          ("；Formal Campaign 已建，创建链冻结" if result["formal_built"] else ""))
    for step in result["steps"]:
        status = {"passed": "通过", "failed": "失败"}.get(step["status"], "")
        reasons = "；".join(step["reasons"])
        print(f"  {step['action']}{('／' + status) if status else ''}  {step['step_id']}" + (f"：{reasons}" if reasons else ""))
    if result.get("preflight_failed"):
        print(f"便宜检查没过的项：{', '.join(result['preflight_failed'])}")
    if result.get("stopped_because"):
        print(f"停在：{result['stopped_because']}")
    print(f"ENTRY_RUN_DONE exit={result['exit_code']} run={result['run_dir']}")


def _positive_parallel(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--gate-parallel 必须是正整数")
    return int(value)


def _declared_sample_mode(value: str) -> str:
    """``--declared-sample`` 的取值：``off``（缺省）、``always`` 或 ``every:N``（N 为正整数）；非法在最外层就拒绝。
    与执行器 ``unit_executor.declared_sample_mode`` 以及入口脚本 ``entry-gates.sh`` 的正则 ``^(off|always|every:[1-9][0-9]*)$`` 同一规则（测试核对
    各层一致），免得编排层放过一个执行器才报错的值：**不去首尾空白**、不认换行、前导零与大小写变体、不是字符串一律拒绝；合法的取值就是规范写法，原样返回。"""

    if isinstance(value, str):
        if value in ("off", "always") or re.fullmatch(r"every:[1-9][0-9]*", value):
            return value
    raise argparse.ArgumentTypeError(f"--declared-sample 只接受 off、always 或 every:N（N 为正整数）：{value!r}")


def _validate_source_work(work: Path) -> None:
    """承接工作区坐标不能经链接或上级回退悄悄指向另一份来源。"""
    roots = (work, work / "go-build-cache", work / "go-mod-cache", work / "go-tmp")
    if (not work.is_absolute() or ".." in work.parts
            or any(path.is_symlink() for root in roots for path in (root, *root.parents))):
        raise OrchestratorError("逐单元承接来源工作区须为无符号链接的规范绝对路径")


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="入口编排器（E2-06）")
    parser.add_argument("--plan", action="store_true", help="只判定每一步沿用还是重做，不执行")
    parser.add_argument("--from", dest="from_step", choices=ORDER, help="从这一步开始（之前的步骤必须都可沿用）")
    parser.add_argument("--to", dest="to_step", choices=ORDER, help="做到这一步为止")
    parser.add_argument("--inject-mask", action="append", default=[], metavar="步骤=目录",
                        help="只供验收：执行这一步的命令时用只读空 tmpfs 遮住目录，制造一次真实的失败")
    parser.add_argument("--steps-dir", help="步骤记录目录（默认 $RUNROOT/entry-steps）")
    parser.add_argument("--approve-sha256", help="只交给 VC-0 收口：批准当前现场待批准事项的 review_sha256")
    parser.add_argument("--approved-by", help="只交给 VC-0 收口：批准人")
    parser.add_argument("--reason", help="只交给 VC-0 收口：上限后的恢复里，失败原因已如何消除")
    parser.add_argument("--reexecute-gates", action="store_true",
                        help="入口门禁重新执行全集（D12：升级开工的入口空跑、一致性验收、收尾合入前用）；不带时用全集通过模式")
    parser.add_argument("--full-set-request", type=Path, help="B-09 全集承接请求；仍须入口实时核对全部条件")
    parser.add_argument("--full-set-work", type=Path, help="来源后台验证的 work 目录；保持严格输入和命令路径一致")
    parser.add_argument("--gate-parallel", type=_positive_parallel, help="入口门禁和 pre-A3 单元并发数；缺省沿用执行器配置")
    parser.add_argument("--unit-inherit-request", type=Path,
                        help="B5a-08 逐单元承接请求（入口门禁 --mode unit-inherit）；与 --full-set-request、--reexecute-gates 互斥")
    parser.add_argument("--unit-inherit-full-gates-request", type=Path,
                        help="独立批准的 full-gates 请求；与主请求从首轮一起固定提供，pre-A3 沿用时按实际门禁组合选用")
    parser.add_argument("--audit-reads", action="store_true",
                        help="入口门禁检查实际读集；与 --reexecute-gates 或 --unit-inherit-request 一起使用")
    parser.add_argument("--audit-strict", action="store_true", help="同时启用 --audit-reads，完整观察宿主读取并核对精确输入")
    parser.add_argument("--audit-policy", type=Path, help="测试与命令的精确输入登记策略；同时启用 --audit-reads --audit-strict")
    parser.add_argument("--declared-sample", type=_declared_sample_mode, default="off", metavar="off|always|every:N",
                        help="声明级承接命令单元的读集证据来源：off（缺省）用登记表证据，always 每次执行都带 strace 采样，every:N 每 N 次采样一次；"
                             "原样交给入口门禁与 pre-A3 两步的执行器，并进入这两步的输入")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.full_set_work and args.unit_inherit_request is None and (
            not args.full_set_request or not args.full_set_work.is_absolute() or ".." in args.full_set_work.parts):
        print("B-09 来源工作区须为规范绝对路径并与全集请求一起提供", file=sys.stderr)
        return 2
    if args.full_set_work is not None and args.unit_inherit_request is not None:
        try:
            _validate_source_work(args.full_set_work)
        except OrchestratorError as error:
            print(f"入口编排：{error}", file=sys.stderr)
            return 2
    if args.unit_inherit_request is not None and (args.full_set_request or args.reexecute_gates
                                                  or not args.unit_inherit_request.is_absolute()):
        print("入口编排：逐单元承接请求须为绝对路径，且不能与 --full-set-request、--reexecute-gates 同用", file=sys.stderr)
        return 2
    if args.unit_inherit_full_gates_request is not None and (
            args.unit_inherit_request is None or not args.unit_inherit_full_gates_request.is_absolute()):
        print("入口编排：full-gates 逐单元承接请求须为绝对路径，并与 --unit-inherit-request 一起提供", file=sys.stderr)
        return 2
    if args.audit_reads and not args.reexecute_gates and args.unit_inherit_request is None:
        print("入口编排：读集审计要真跑才有读集，--audit-reads 要和 --reexecute-gates 一起带", file=sys.stderr)
        return 2
    if args.audit_strict and not args.audit_reads:
        print("入口编排：--audit-strict 必须同时启用 --audit-reads", file=sys.stderr)
        return 2
    if args.audit_policy is not None:
        if not args.audit_reads or not args.audit_strict:
            print("入口编排：--audit-policy 必须同时启用 --audit-reads --audit-strict", file=sys.stderr)
            return 2
        if (not args.audit_policy.is_absolute() or ".." in args.audit_policy.parts
                or any(path.is_symlink() for path in (args.audit_policy, *args.audit_policy.parents)) or not args.audit_policy.is_file()):
            print("入口编排：--audit-policy 必须是无符号链接的现有普通文件，使用规范绝对路径", file=sys.stderr)
            return 2
    params = dict(os.environ)
    if not params.get("D") or not params.get("RUNROOT"):
        print("入口编排：参数里没有 D 或 RUNROOT（由 entry.sh 先 source 驱动 lib.sh 加载本轮参数文件）", file=sys.stderr)
        return 2
    masks: dict[str, str] = {}
    for item in args.inject_mask:
        step, _, directory = item.partition("=")
        if step not in ORDER or not directory.startswith("/"):
            print(f"入口编排：--inject-mask 要写成 步骤=绝对目录：{item}", file=sys.stderr)
            return 2
        masks[step] = directory
    runroot = Path(params["RUNROOT"])
    runroot.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (runroot / ".entry.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("入口编排：已有编排在运行（$RUNROOT/.entry.lock），拒绝并发", file=sys.stderr)
        return 3
    run_dir = runroot / "entry-runs" / datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{run_dir.name}-{os.getpid()}")
    steps_dir = Path(args.steps_dir or params.get("ENTRY_STEPS_DIR") or runroot / "entry-steps")
    orchestrator = Orchestrator(params, driver_dir=Path(params.get("DRV") or HERE), run_dir=run_dir, steps_dir=steps_dir,
                                masks=masks, approval={"approve_sha256": args.approve_sha256 or "",
                                                       "approved_by": args.approved_by or "", "reason": args.reason or ""},
                                reexecute_gates=args.reexecute_gates, audit_reads=args.audit_reads, full_set_request=args.full_set_request,
                                full_set_work=args.full_set_work, unit_inherit_request=args.unit_inherit_request, gate_parallel=args.gate_parallel,
                                unit_inherit_full_gates_request=args.unit_inherit_full_gates_request,
                                audit_strict=args.audit_strict, audit_policy=args.audit_policy, declared_sample=args.declared_sample)
    try:
        result = orchestrator.orchestrate(plan_only=args.plan, from_step=args.from_step, to_step=args.to_step)
    except OrchestratorError as error:
        print(f"入口编排：{error}", file=sys.stderr)
        return 2
    print_summary(result)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
