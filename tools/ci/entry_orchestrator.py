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
* 沿用只看步骤记录（产物存在不等于可用）：没有记录的旧产物一律不沿用；只写一次的正式坐标被占用（上次失败的残留、
  或没有记录的旧产物）时换带序号的新坐标（``-r2``、``-r3`` …），实际坐标写进步骤记录，下游从记录取。
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
``--approve-sha256``／``--approved-by``／``--reason`` 只交给 VC-0 收口这一步（批准对象由收口模块按现场判定）。
运行锁 ``$RUNROOT/.entry.lock``；每步日志与运行汇总在 ``$RUNROOT/entry-runs/<UTC>/``。
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
PRE_A3_MODULE = "tools.official_client_capture.codex_upgrade_pre_a3_certification"
POLICY_MODULE = "tools.official_client_capture.codex_upgrade_policy_certification"
CLOSEOUT_MODULE = "tools.official_client_capture.codex_upgrade_vc0_closeout"
VC_RECEIPT_MODULE = "tools.official_client_capture.codex_upgrade_vc_receipt"
P0_FACTS_SCHEMA = "codex-upgrade-vc-receipt-facts/v1"
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
                 approval: Mapping[str, str] | None = None) -> None:
        self.params = dict(params)
        self.driver_dir, self.run_dir, self.steps_dir = driver_dir, run_dir, steps_dir
        self.runner, self.masks = runner, dict(masks or {})
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
        return self.root / "evidence" / "campaigns" / "upgrade-project-ledger"

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

    def run(self, step_id: str, argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        if step_id in self.masks:
            argv = mask_argv(argv, self.masks[step_id])
        return self.runner(list(argv), cwd or self.data, env or self.env(), self.run_dir / f"{step_id}.log")

    def py(self, module: str, *arguments: str) -> list[str]:
        return [sys.executable, "-B", "-m", module, *arguments]

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
        rc = self.run("policy-compatibility", self.py(POLICY_MODULE, "compatibility", "--previous-policy", self.p("PREVIOUS_POLICY"),
                                                      "--output", str(out)))
        return rc == 0 and out.is_file(), {"compatibility": str(out)}

    def step_policy_activation(self) -> tuple[bool, dict[str, str]]:
        out = fresh(Path(self.p("POLICY_ACTIVATION")))
        rc = self.run("policy-activation", self.py(POLICY_MODULE, "activation", "--deployment-receipt", str(self.latest_deploy()),
                                                   "--compatibility-receipt", str(self.record_product("policy-compatibility", "compatibility")),
                                                   "--output", str(out)))
        return rc == 0 and out.is_file(), {"activation": str(out)}

    def entry_gates_and_pre_a3(self, gates_run: bool, pre_a3_run: bool) -> None:
        """入口门禁与 pre-A3 同一次运行（E2-04）：pre-A3 要做就纳入，否则复核并沿用记录里的认证；入口门禁沿用、只有
        pre-A3 要做时，照 lib.sh 的 issue_pre_a3_certification 同一顺序单独签（plan → 执行器按场景并行 → issue）。"""

        steps = [step for step, needed in (("entry-gates", gates_run), ("pre-a3", pre_a3_run)) if needed]
        for step in steps:
            ES.begin(ES.STEP_BY_ID[step], self.ctx)
        try:
            activation = self.record_product("policy-activation", "activation")
            certification = (fresh(Path(self.p("PRE_A3_CERTIFICATION"))) if pre_a3_run
                             else self.record_product("pre-a3", "certification"))
            if gates_run:
                source = (self.p("ENTRY_BUNDLE"), self.p("ENTRY_BRANCH"), self.p("ENTRY_COMMIT"))
            deploy = self.latest_deploy()
        except (OrchestratorError, OSError) as error:
            for step in steps:
                self.outcomes[step].reasons.append(f"执行前准备失败：{error}")
                self._finish(step, False, {})
            return
        if gates_run:
            out = self.run_dir / "entry-gates"
            rc = self.run("entry-gates", ["bash", str(self.driver_dir / "entry-gates.sh"), "--profile", "entry", "--out", str(out),
                                          "--policy-activation", str(activation), "--pre-a3-certification", str(certification),
                                          "--pre-a3-mode", "run" if pre_a3_run else "present", *source])
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
            units = self.run_dir / "pre-a3-units.json"
            rc, planned = self.run_json("pre-a3", "plan", self.py(PRE_A3_MODULE, "plan", "--staging-root",
                                                                  str(self.data / "staging" / f"pre-a3-certification-{self.p('STAMP')}"),
                                                                  "--output", str(units)))
            root = Path(str(planned.get("staging_root") or ""))
            if rc == 0 and root.is_dir():
                self.run("pre-a3", [sys.executable, "-B", str(self.driver_dir / "unit_executor.py"), "run-commands",
                                    "--manifest", str(units), "--out-dir", str(root / "executor")])
                self.run("pre-a3", self.py(PRE_A3_MODULE, "issue", "--staging-root", str(root),
                                           "--executor-summary", str(root / "executor" / "summary.json"),
                                           "--deployment-receipt", str(deploy), "--policy-activation", str(activation),
                                           "--output", str(certification)))
        if pre_a3_run:
            if not certification.is_file():
                self.outcomes["pre-a3"].reasons.append(
                    f"pre-A3 认证没有签发（没通过的认证只写旁路文件 {certification.parent}/*.failed-*.json）")
            self._finish("pre-a3", certification.is_file(), {"certification": str(certification)})

    def step_zero_request_smoke(self) -> tuple[bool, dict[str, str]]:
        stamp = self.p("STAMP")
        for serial in range(1, 1000):
            suffix = "" if serial == 1 else f"-r{serial}"
            staging = self.data / "staging" / f"zero-request-smoke-{stamp}{suffix}"
            output = self.data / "audit" / f"zero-request-smoke-{stamp}{suffix}.json"
            if not os.path.lexists(staging) and not os.path.lexists(output):
                break
        rc = self.run("zero-request-smoke", self.py("tools.official_client_capture.codex_upgrade_zero_request_smoke",
                                                    "--staging-root", str(staging), "--output", str(output)))
        return rc == 0 and output.is_file(), {"receipt": str(output)}

    def step_atomic_double(self) -> tuple[bool, dict[str, str]]:
        root = fresh(self.data / "staging" / f"codex-atomic-vc0-vc1-{self.p('ROUND')}-{self.p('STAMP')}")
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        rc = self.run("atomic-double", ["docker", "exec", "--env", "PYTHONPATH=/capture", "--env", "PYTHONDONTWRITEBYTECODE=1",
                                        "--workdir", "/capture", "capture-cli", "python3", "-m",
                                        "tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt",
                                        "atomic-double-collect", "--evidence-root", f"/capture/staging/{root.name}",
                                        "--output", "receipt.json"])
        receipt = root / "receipt.json"
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    def step_ledger(self) -> tuple[bool, dict[str, str]]:
        deadline = datetime.strptime(self.p("PROJECT_DEADLINE_UTC"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        total = int((deadline - datetime.now(timezone.utc)).total_seconds() // 60) - 5   # 留 5 分钟余量（同 stage1.sh）
        budgets = [argument for item in self.params.get("STAGE_BUDGETS", "").split() for argument in ("--stage-budget-minutes", item)]
        self.ledger_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rc = self.run("ledger", self.py("tools.official_client_capture.codex_upgrade_timing_ledger", "create",
                                        "--ledger-dir", str(self.ledger_dir), "--upgrade-id", self.p("UP"),
                                        "--baseline-version", self.p("BASELINE_VERSION"), "--target-version", self.p("TARGET_VERSION"),
                                        "--campaign-purpose", "production_replacement",
                                        "--evidence-decision", self.params.get("EVIDENCE_DECISION") or "recapture",
                                        "--project-ledger-dir", str(self.project_ledger), "--total-budget-minutes", str(total), *budgets))
        ledger = self.ledger_dir / "ledger.json"
        return rc == 0 and ledger.is_file(), {"ledger": str(ledger)}

    def step_environment_p0(self) -> tuple[bool, dict[str, str]]:
        root = fresh(self.root / "environment" / f"{self.p('UP')}-p0")
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        module = "tools.official_client_capture.codex_upgrade_arm64_environment_receipt"
        rc = self.run("environment-p0", self.py(module, "collect", "--evidence-root", str(root), "--output", "facts.json",
                                                "--phase", "p0", "--subject-id", self.p("UP"),
                                                "--rust-tls-codex-version", self.p("TARGET_VERSION")))
        if rc == 0:
            rc = self.run("environment-p0", self.py(module, "finalize", "--evidence-root", str(root), "--facts", "facts.json",
                                                    "--output", "receipt.json"))
        receipt = root / "receipt.json"
        return rc == 0 and receipt.is_file(), {"receipt": str(receipt)}

    def step_ledger_checkpoint(self) -> tuple[bool, dict[str, str]]:
        ledger_dir = self.record_product("ledger", "ledger").parent
        checkpoint = fresh(ledger_dir / "receipts" / f"vc0-input-{self.p('ROUND')}-preflight-{self.p('STAMP')}.json")
        rc = self.run("ledger-checkpoint", self.py("tools.official_client_capture.codex_upgrade_timing_ledger", "checkpoint",
                                                   "--ledger-dir", str(ledger_dir),
                                                   "--output", checkpoint.relative_to(ledger_dir).as_posix()))
        return rc == 0 and checkpoint.is_file(), {"checkpoint": str(checkpoint)}

    def step_preflight_plan(self) -> tuple[bool, dict[str, str]]:
        ledger_dir = self.record_product("ledger", "ledger").parent
        checkpoint = self.record_product("ledger-checkpoint", "checkpoint")
        environment = self.record_product("environment-p0", "receipt")
        headsha = str(json.loads(self.latest_deploy().read_text(encoding="utf-8"))["tool_files_sha256"])[:9]
        campaigns = self.root / "evidence" / "campaigns"
        campaigns.mkdir(parents=True, exist_ok=True, mode=0o700)
        pre = fresh(campaigns / f"{self.p('CAMPAIGN_PREFIX')}-preflight-vc5-{self.p('ROUND')}-{headsha}-{self.p('STAMP')}")
        p = self.p
        argv = self.py("tools.official_client_capture.codex_upgrade", "plan",
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
        module = "tools.official_client_capture.codex_upgrade_job_rehearsal_receipt"
        rc = self.run("job-rehearsal", self.py(module, "collect", "--campaign-dir", str(campaign), "--evidence-root", str(root),
                                               "--output", "facts.json", *previous))
        if rc == 0:
            rc = self.run("job-rehearsal", self.py(module, "finalize", "--evidence-root", str(root), "--facts", "facts.json",
                                                   "--output", "receipt.json"))
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
                                              "--output-dir", str(probe), "--data-root", str(self.data)])
        if rc == 0:
            rc = self.run("client-launch-probe", [sys.executable, "-B", str(script), "verify", "--output-dir", str(probe),
                                                  "--campaign-dir", str(campaign)])
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
            PRE_A3_MODULE, "record-reuse", "--certification", str(pre_a3), "--deployment-receipt", str(deploy),
            "--policy-activation", str(activation), "--receipt-root", str(pre_a3.parent)))
        if rc != 0:
            self.outcomes["release-certification"].reasons.append("pre-A3 复用收据登记失败，见日志")
            return False, {"certification": str(out)}
        reuse = str(reused.get("reuse_receipt") or "")
        arguments = ["issue", "--deployment-receipt", str(deploy), "--pre-a3-certification", str(pre_a3),
                     *(["--pre-a3-reuse-receipt", reuse] if reuse else []), "--policy-activation", str(activation),
                     "--job-rehearsal-root", str(job.parent), "--job-rehearsal-receipt", job.name,
                     "--atomic-rehearsal-root", str(atomic.parent), "--atomic-rehearsal-receipt", atomic.name,
                     "--atomic-container", "capture-cli", "--data-root", str(self.data), "--output", str(out)]
        rc = self.run("release-certification", self.py("tools.official_client_capture.certify_release", *arguments))
        if rc == 0:
            rc = self.run("release-certification", self.py("tools.official_client_capture.certify_release", "verify",
                                                           "--certification", str(out)))
        return rc == 0 and out.is_file(), {"certification": str(out)}

    def step_p0_receipt(self) -> tuple[bool, dict[str, str]]:
        """P0 门禁收据（E2-07 固定步骤，原来每轮手写）：两份离线门禁证据取自入口门禁那一次运行，加发布认证与回退依据；
        subject 取计时账本计划（升级 ID、用途、两个版本），签发后立即重放。"""

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
            "schema_version": P0_FACTS_SCHEMA,
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
        rc = self.run("p0-receipt", self.py(VC_RECEIPT_MODULE, "finalize", "--evidence-root", str(root), "--facts", "p0-facts.json",
                                            "--output", "p0-receipt.json"))
        if rc == 0:
            rc = self.run("p0-receipt", self.py(VC_RECEIPT_MODULE, "replay", "--evidence-root", str(root),
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
                                           "--output-dir", str(report.parent), "--campaign-dir", str(preflight)])
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
        rc, result = self.run_json("vc0-closeout", "closeout", self.py(CLOSEOUT_MODULE, *arguments))
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
        step = ES.STEP_BY_ID[step_id]
        record_products = {name: path for name, path in products.items() if not passed or Path(path).exists()}
        try:
            ES.finish(step, self.ctx, status=outcome.status, products=record_products)
        except ES.EntryStepsError as error:
            # 记成通过的条件没满足（产物缺失或内容不合格）：改记失败，原因写明。
            outcome.status = "failed"
            outcome.reasons.append(str(error))
            ES.finish(step, self.ctx, status="failed", products=record_products)

    def execute(self, step_id: str) -> None:
        step = ES.STEP_BY_ID[step_id]
        ES.begin(step, self.ctx)
        handler = getattr(self, "step_" + step_id.replace("-", "_"))
        try:
            passed, products = handler()
        except (OrchestratorError, ES.EntryStepsError, OSError, ValueError, KeyError) as error:
            self.outcomes[step_id].reasons.append(f"执行前准备失败：{error}")
            passed, products = False, {}
        self._finish(step_id, passed, products)

    def blocked_by(self, step_id: str) -> list[str]:
        deps = list(ES.STEP_BY_ID[step_id].upstream) + list(EXEC_DEPS.get(step_id, ()))
        blockers = [dep for dep in deps if dep in self.outcomes and
                    (self.outcomes[dep].status == "failed" or self.outcomes[dep].action in {"被阻塞", "阻塞"})]
        blockers += [f"便宜检查 {check}" for check, steps in PREFLIGHT_BLOCKS.items()
                     if check in self.preflight_failed and step_id in steps]
        return blockers

    def orchestrate(self, *, plan_only: bool = False, from_step: str | None = None, to_step: str | None = None,
                    formal: bool | None = None) -> dict[str, Any]:
        is_formal = ES.formal_built(self.ctx) if formal is None else formal
        evaluation = ES.evaluate(self.ctx, is_formal_built=is_formal)
        decisions = {step["step_id"]: step for step in evaluation["steps"]}
        start = ORDER.index(from_step) if from_step else 0
        stop = ORDER.index(to_step) if to_step else len(ORDER) - 1
        if start > stop:
            raise OrchestratorError("起点在终点之后")
        for index, step_id in enumerate(ORDER):
            decision = decisions[step_id]
            self.outcomes[step_id] = Outcome(step_id, decision["decision"], reasons=list(decision["reasons"]))
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
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
                                                       "approved_by": args.approved_by or "", "reason": args.reason or ""})
    try:
        result = orchestrator.orchestrate(plan_only=args.plan, from_step=args.from_step, to_step=args.to_step)
    except OrchestratorError as error:
        print(f"入口编排：{error}", file=sys.stderr)
        return 2
    print_summary(result)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
