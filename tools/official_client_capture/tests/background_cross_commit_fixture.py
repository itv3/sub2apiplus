"""后台跨提交、跨部署承接（H7 失败 → H8 修复续作，``tools/ci/background_validation.py``）的合成世界（测试辅助，不是测试模块）。

在原承接夹具（``unit_inherit_fixture.World``）上搭一个「两轮后台」：

* **H7**：提交 C7、部署 D7 的一轮旧后台，在物理 WORK 上跑完整个单元全集（单元执行记录与运行清单都真实入库），其中
  ``test_alpha`` 被故意弄失败，后台结论 ``failed``（批次边界 ``check-boundary`` 退出 3 拒派）；
* **H8**：修好并重新部署（提交 C8、部署 D8，部署收据集合不同）；``test_alpha`` 的修复与 ``test_beta`` 的受影响输入变化、一个
  没有任何来源记录的新单元、一个未登记读集审计只能实时重跑的单元；其余单元输入没变。

H8 的承接提案用原合同的 ``propose`` 现算（不是手填来源集合），所以「提案选的是 H7 轮实际执行的新记录，而不是更早的历史记录」
由真实的选取规则保证。入口门禁用进程内替身走执行器同一流程（三个复核点、决策收据、清单自检，见 ``World.run``），后台
进程用 ``Popen`` 替身，不起真实子进程。再往后还可以接 H9（``deploy``／``gate_result`` 的参数化），验证后继链。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from tools.ci import background_validation as bv
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import unit_inherit_fixture as base

T0 = base.T0
H7_RUN = "20261005t113000z-7-h7"
H8_RUN = "20261005t121500z-8-h8"
H9_RUN = "20261005t131500z-9-h9"
C7 = "7" * 40
C8 = "8" * 40
C9 = "9" * 40
BRANCH = "codex/claude-upgrade-finish"
# 修复轮涉及的单元：H7 里故意失败的、H8 里输入变化的（受影响）、只能实时重跑的、H8 新增而没有来源记录的。
FAILED_UNIT = "test_alpha"
AFFECTED_UNIT = "test_beta"
REALTIME_UNIT = "unaudited:realtime"
NEW_UNIT = "new:unit-without-source"
# 修复不涉及的单元：H8 的提案应当承接 H7 那一轮实际执行成功的记录。
STABLE_UNITS = (base.CAPTURE_COMMAND, *base.EGRESS)
GATE_IDS = sorted(("check-egress-spec", "test-capture-tools"))


class CrossWorld(base.World):
    """原承接夹具 + 两处修正：清单项如实写单元是否通过与是否可承接（原夹具一律写通过、可承接），运行清单的 ``out_dir`` 可按
    运行指定（后台要求它是 ``<out>/executor``）。``not_inheritable`` 里的单元执行记录标 ``inheritable=False``（未登记读集审计）。"""

    def __init__(self, root: Path, *, not_inheritable: Sequence[str] = (), **kwargs: Any) -> None:
        self.not_inheritable = set(not_inheritable)
        self.out_dirs: dict[str, Path] = {}
        # 运行 ID → 在那一轮里执行失败的单元（造「重跑之后仍然失败」）。
        self.failing_in_run: dict[str, set[str]] = {}
        super().__init__(root, **kwargs)

    def source_record(self, unit_id: str, **kwargs: Any) -> dict[str, Any]:
        if unit_id in self.not_inheritable:
            kwargs.setdefault("inheritable", False)
            kwargs.setdefault("not_inheritable_reason", "输入合同未闭合：只验证本次执行结果，禁止承接")
        if unit_id in self.failing_in_run.get(kwargs.get("run_id"), ()):
            tests = base.TESTS.get(unit_id)
            kwargs.update(passed=False, exit_code=1, tests={test_id: {"outcome": "failed"} for test_id in tests} if tests else None)
        return super().source_record(unit_id, **kwargs)

    def entry(self, record: Mapping[str, Any], disposition: str, reasons: Sequence[str] | None = None) -> dict[str, Any]:
        row = super().entry(record, disposition, reasons)
        row["passed"] = record.get("passed") is True
        row["inheritable"] = record.get("inheritable") is True
        return row

    def manifest_body(self, run_id: str, mode: str, rows: Mapping[str, tuple[Mapping[str, Any], str]], *, decided: float,
                      max_age: float = 168.0, reasons: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
        body = super().manifest_body(run_id, mode, rows, decided=decided, max_age=max_age, reasons=reasons)
        out = self.out_dirs.get(run_id)
        if out is None:
            return body
        return ur.seal({**{key: value for key, value in body.items() if key != "manifest_sha256"}, "out_dir": str(out / "executor")},
                       "manifest_sha256")


def _clean_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


class GitHistory:
    """真实的 git 小仓库，用来验证「旧提交是新提交的祖先」：

    ``old``（H7 的提交）在 ``BRANCH`` 上；``unrelated`` 是另一条无关历史线（孤儿分支）上的提交；两者建好之后才克隆出 WORK 里的
    ``test-tree``（所以测试树有旧历史，没有后来的新提交——与 ARM64 上「测试树是完整历史克隆、增量 bundle 只带新提交」同形）；
    然后 ``advance()`` 在两条线上各加新提交，``bundle()`` 打增量 bundle（``<base>..<分支>``，前置提交在测试树里）。"""

    def __init__(self, root: Path) -> None:
        self.source = Path(root).resolve() / "git-source"
        self.source.mkdir(parents=True)
        self.run("init", "-q", "-b", BRANCH)
        self.commit("input.txt", "旧输入\n", "旧提交")
        self.old = self.rev()
        self.run("checkout", "-q", "--orphan", "unrelated")
        self.run("rm", "-q", "-rf", ".")
        self.commit("other.txt", "无关历史线\n", "无关历史线的提交")
        self.unrelated = self.rev()
        self.run("checkout", "-q", BRANCH)
        self.new = self.unrelated_new = ""

    def run(self, *args: str, cwd: Path | None = None) -> str:
        argv = ["git", "-C", str(cwd or self.source), "-c", "user.name=跨提交验收", "-c", "user.email=cross@example.invalid",
                "-c", "commit.gpgsign=false", *args]
        return subprocess.run(argv, check=True, capture_output=True, text=True, env=_clean_environment()).stdout.strip()

    def commit(self, name: str, text: str, message: str) -> None:
        (self.source / name).write_text(text, encoding="utf-8")
        self.run("add", "-A")
        self.run("commit", "-q", "-m", message)

    def rev(self) -> str:
        return self.run("rev-parse", "HEAD")

    def clone_into(self, work: Path) -> Path:
        tree = Path(work) / "test-tree"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.source), str(tree)], check=True, capture_output=True,
                       env=_clean_environment())
        self.run("checkout", "-q", "--detach", self.old, cwd=tree)
        return tree

    def advance(self) -> None:
        self.run("checkout", "-q", BRANCH)
        self.commit("input.txt", "新输入一\n", "新提交一")
        self.commit("input.txt", "新输入二\n", "新提交二")
        self.new = self.rev()
        self.run("checkout", "-q", "unrelated")
        self.commit("other.txt", "无关历史线的后继\n", "无关历史线的新提交")
        self.unrelated_new = self.rev()
        self.run("checkout", "-q", BRANCH)

    def bundle(self, path: Path, base_commit: str | None, branch: str) -> Path:
        self.run("bundle", "create", str(path), f"{base_commit}..{branch}" if base_commit else branch)
        return Path(path)


class Scenario:
    """一场「H7 失败 → H8 修复续作」。测试里按需构造，``case`` 用来在替身派发里做断言。

    ``h7_no_audit``：H7 里这些单元的执行记录没有读集审计（用来造「来源单元审计未通过」）；``client``：本场后台是哪个客户端的
    轮次（两客户端都已纳管，结论按部署收据集合绑定；Claude 轮次的入口门禁摘要核对的是 Claude 的那份收据）；``git``：用真实的
    小仓库（见 ``GitHistory``），提交号是真实的、WORK 里有完整历史克隆的测试树，可以跑真实的祖先验证。"""

    def __init__(self, case: unittest.TestCase, root: Path, *, git: GitHistory | None = None,
                 h7_no_audit: Sequence[str] = (), client: str = "codex") -> None:
        self.case = case
        self.root = Path(root).resolve()
        self.git = git
        self.client = client
        self.h7_no_audit = set(h7_no_audit)
        self.world = CrossWorld(self.root / "world", scenarios=(), extra_units=(REALTIME_UNIT,), not_inheritable=(REALTIME_UNIT,))
        self.data = self.world.root / "data"
        self.runroot = self.root / "run"
        self.claude_runroot = self.root / "claude-run"
        self.claude_d7 = self.world.control / "claude-managed-deploy-20261005t000000z.json"
        self.claude_d7.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "a" * 64}}))
        self.dummy_bundle = self.root / "h7.bundle"
        self.dummy_bundle.write_bytes("H7 的 bundle 替身".encode())
        self.h8_bundle = self.root / "h8.bundle"
        self.h8_bundle.write_bytes("H8 的 bundle 替身".encode())
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text("# 后台测试替身入口；原决策与消费验证不替换\n")
        self.policy = self.root / "policy.json"
        self.policy.write_text('{"schema_version":"unit-audit-policy/v1","units":{}}\n')
        self.process = type("NoProcess", (), {"pid": -1})()
        self.world.sample(T0 + 150)
        self.h7_path: Path | None = None
        self.h7_records: dict[str, dict[str, Any]] = {}
        self.work: Path | None = None
        self.codex_d8 = self.claude_d8 = self.codex_d9 = self.claude_d9 = None

    @property
    def c7(self) -> str:
        return self.git.old if self.git else C7

    @property
    def c8(self) -> str:
        return self.git.new if self.git else C8

    def advance_git(self) -> Path:
        """真实 git 场景：H8 的新提交（在 H7 提交之后）和增量 bundle（前置提交在 WORK 测试树里）。"""

        assert self.git is not None
        self.git.advance()
        self.h8_bundle = self.git.bundle(self.root / "h8-real.bundle", self.git.old, BRANCH)
        return self.h8_bundle

    # ------------------------------------------------------------------ 命令行

    def argv(self, *, commit: str, bundle: Path, request: Path | None = None, source: Path | None = None, extra: Sequence[str] = (),
             work: Path | None = None, record_store: Path | None = None, parallel: str | None = "2", branch: str = BRANCH,
             client: str | None = None) -> list[str]:
        return ["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(bundle), "--branch", branch,
                "--commit", commit, "--work", str(work or self.work), "--record-store", str(record_store or self.world.store.root),
                "--audit-policy", str(self.policy), "--entry-gates", str(self.gates), "--preflight", "off", "--reuse", "off",
                *(["--parallel", parallel] if parallel is not None else []),
                *(["--client", client or self.client] if (client or self.client) != "codex" else []),
                *(["--unit-inherit-request", str(request)] if request else []),
                *(["--unit-inherit-source-result", str(source)] if source else []), *extra]

    def arguments(self, request: Path | None, *extra: str, **kwargs: Any) -> Any:
        kwargs.setdefault("commit", self.c8)
        kwargs.setdefault("bundle", self.h8_bundle)
        kwargs.setdefault("source", self.h7_path)
        return bv._parse(self.argv(request=request, extra=extra, **kwargs))

    # ------------------------------------------------------------------ H7

    def start_h7(self) -> Path:
        """起 H7（Popen 替身）并造出它的全部成果：入口门禁摘要、逐单元清单、全集执行记录；结论 failed。"""

        deployment, deployments = bv.latest_deployment(self.data, self.client), bv.deployment_set(self.data)
        self.deployment7, self.deployments7 = deployment, deployments
        self.work = bv._bound_work_roots(self.runroot, self.data, self.c7, deployment, deployments)[0]
        self.work.mkdir(parents=True)
        self.marker = self.work / "preserve-source"
        self.marker.write_text("必须保留的来源\n")
        if self.git is not None:
            self.git.clone_into(self.work)
        with self.launch_stub():
            started = bv.start(bv._parse(self.argv(commit=self.c7, bundle=self.dummy_bundle)))
        path = Path(started["result"])
        payload = bv._read(path)
        out = Path(payload["out"])
        (out / "executor").mkdir(parents=True)
        self.world.out_dirs[H7_RUN] = out
        rows = {}
        for unit in self.world.units:
            overrides: dict[str, Any] = {"audit": unit not in self.h7_no_audit}
            if unit == FAILED_UNIT:
                overrides.update(passed=False, exit_code=1,
                                 tests={"test_alpha.AlphaTests.test_a": {"outcome": "failed"},
                                        "test_alpha.AlphaTests.test_skipped": {"outcome": "skipped", "reason": "示例跳过"}})
            rows[unit] = (self.world.source_record(unit, run_id=H7_RUN, completed=T0 - 1800, mode="re-execute", **overrides), "executed")
        self.h7_records = {unit: record for unit, (record, _disposition) in rows.items()}
        manifest = self.world.publish_run(H7_RUN, "re-execute", rows, decided=T0 - 1800)
        manifest_path = out / "executor" / "unit-manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        gates = [{"gate_id": gate, "status": "failed" if gate == "test-capture-tools" else "passed"} for gate in GATE_IDS]
        self.h7_out = out
        (out / "entry-gates.json").write_text(json.dumps({
            "status": "failed", "profile": "full-gates", "mode": "re-execute", "gates": gates, "run_id": H7_RUN,
            "source": {"commit": self.c7, **self.receipt_fields(self.world.deploy, self.claude_d7)},
            "record_store": str(self.world.store.root), "test_tree": str(self.work / "test-tree"),
            "unit_manifest": {"path": str(manifest_path), "manifest_sha256": manifest["manifest_sha256"], "self_check": "passed"}}))
        self.h7_manifest = manifest
        self.h7_result = {**payload, "status": "failed", "returncode": 1, "failed_gates": ["test-capture-tools"],
                          "reason": "入口门禁退出码 1", "completed_at_utc": bv._utc_now()}
        bv._write(path, self.h7_result)
        self.h7_path = path
        return path

    def rewrite_h7(self, **changes: Any) -> None:
        """改写 H7 结论文件（造各种坏来源）；``restore_h7`` 写回原样。"""

        bv._write(self.h7_path, {**self.h7_result, **changes})

    def restore_h7(self) -> None:
        bv._write(self.h7_path, self.h7_result)

    def receipt_fields(self, codex: Path, claude: Path) -> dict[str, str]:
        """入口门禁摘要的 source：本轮客户端核对的部署收据，另写其余客户端的（两客户端都纳管时入口门禁要逐一核对）。"""

        own, other_name, other = (codex, "claude", claude) if self.client == "codex" else (claude, "codex", codex)
        return {"deploy_receipt": own.name, "deploy_receipts_others": f"{other_name}={other.name}"}

    # ------------------------------------------------------------------ 后续轮次的部署与变更

    def deploy(self, stamp: str, codex_char: str, claude_char: str) -> tuple[Path, Path]:
        """重新部署：两个客户端各一份新收据（时间戳更晚、摘要不同）→ 部署收据集合与之前不同。"""

        codex = self.world.control / f"codex-fixture-supervisor-enable-{stamp}.json"
        codex.write_text(json.dumps({"status": "passed", "tool_files_sha256": codex_char * 64}), encoding="utf-8")
        claude = self.world.control / f"claude-managed-deploy-{stamp}.json"
        claude.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": claude_char * 64}}),
                          encoding="utf-8")
        return codex, claude

    def deploy_h8(self) -> None:
        self.codex_d8, self.claude_d8 = self.deploy("20261005t120000z", "e", "b")

    def deploy_h9(self) -> None:
        self.codex_d9, self.claude_d9 = self.deploy("20261005t130000z", "d", "c")

    def prepare_h8(self) -> None:
        """H8 的变更：test_alpha 修复、test_beta 受影响；新增一个没有来源记录的单元。"""

        self.world.declared[FAILED_UNIT] = [ur.value_entry("tests", f"file:{FAILED_UNIT}", "修复后的输入")]
        self.world.declared[AFFECTED_UNIT] = [ur.value_entry("tests", f"file:{AFFECTED_UNIT}", "受影响的输入")]
        if NEW_UNIT not in self.world.units:
            self.world.units = (*self.world.units, NEW_UNIT)

    def fresh_currents(self) -> dict[str, Any]:
        currents = {unit: self.world.current(unit) for unit in self.world.units}
        currents[REALTIME_UNIT].inheritable = False
        currents[REALTIME_UNIT].reason = "输入合同未闭合：只验证本次执行结果，禁止承接"
        return currents

    def context(self, **overrides: Any) -> dict[str, Any]:
        overrides.setdefault("commit", self.c8)
        overrides.setdefault("client", self.client)
        return self.world.context(profile="full-gates", gates=GATE_IDS, **overrides)

    def propose(self) -> dict[str, Any]:
        """用原合同的提案规则现算承接来源集合：每个可承接单元取记录库里最新且当前仍能承接的正式记录。"""

        return ui.propose(self.world.store.root, self.fresh_currents(), self.world.facts(T0), client="")

    def explicit_sources(self, records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """手填来源集合：单元 → 指定的来源记录（造混合来源、审计未通过等提案不会选的情形）。"""

        return self.world.sources(units=sorted(records), records=records)

    def request(self, sources: Mapping[str, Any] | None = None, *, commit: str | None = None, client: str | None = None,
                approvals: Any = None, **extra: Any) -> Path:
        context = self.context(**({"commit": commit} if commit else {}), **({"client": client} if client else {}))
        chosen = dict(sources if sources is not None else self.propose())
        if callable(approvals):
            approvals = approvals(ui.binding_sha256(context, chosen))
        return self.world.request(context=context, sources=chosen, approvals=approvals, **extra)

    # ------------------------------------------------------------------ 启动与收尾

    def launch_stub(self) -> Any:
        """只替换「起后台 run 子进程」那一次 Popen，其它子进程（真实 git 的祖先验证）照常执行。"""

        real_popen = subprocess.Popen

        def popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(argv, (list, tuple)) and "run" in argv and "--result" in argv:
                return self.process
            return real_popen(argv, *args, **kwargs)

        return patch.object(bv.subprocess, "Popen", side_effect=popen)

    def verified(self, old: str, new: str, **_kwargs: Any) -> dict[str, Any]:
        return {"old_commit": old, "new_commit": new, "status": "verified", "method": "测试替身"}

    def start_successor(self, request: Path | None, *extra: str, real_ancestry: bool = False, **kwargs: Any) -> Path:
        """起后继（Popen 替身）；默认把祖先验证换成替身（祖先关系另有真实 git 的专项测试），``real_ancestry`` 时走真实验证。"""

        args = self.arguments(request, *extra, **kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.launch_stub())
            if not real_ancestry:
                stack.enter_context(patch.object(bv, "_verify_ancestry", side_effect=self.verified))
            self.last_started = bv.start(args)
        return Path(self.last_started["result"])

    start_h8 = start_successor

    def gate_result(self, path: Path, request: Path, *, run_id: str = H8_RUN, commit: str | None = None,
                    receipts: tuple[Path, Path] | None = None, failing: str | None = None, before: Any = None) -> None:
        """入口门禁替身：按执行器同一流程走完三个复核点、发布决策收据与运行清单，再写入口门禁摘要。
        ``failing`` 是要标失败的门禁项（运行清单照常发布：普通单元失败不影响清单自检）；``before`` 在每个复核点前调用（变异用）。"""

        commit = commit or self.c8
        codex, claude = receipts or (self.codex_d8, self.claude_d8)
        payload = bv._read(path)
        out = Path(payload["out"])
        (out / "executor").mkdir(parents=True)
        self.world.out_dirs[run_id] = out
        context = self.context(commit=commit)
        result = self.world.run(request, run_id=run_id, currents=self.fresh_currents(), context=lambda checkpoint: context, before=before)
        self.case.assertEqual(result.problems, [])
        self.result = result
        manifest = out / "executor" / "unit-manifest.json"
        manifest.write_bytes(result.manifest_path.read_bytes())
        (out / "executor" / "unit-inherit-decision.json").write_text(json.dumps(result.session.build_decision(result.decisions)))
        gates = [{"gate_id": gate, "status": "failed" if failing == gate else "passed"} for gate in context["gates"]]
        summary = {"status": "failed" if failing else "passed", "profile": "full-gates", "mode": result.manifest["mode"], "gates": gates,
                   "run_id": run_id,
                   "source": {"commit": commit, **self.receipt_fields(codex, claude)},
                   "record_store": str(self.world.store.root), "test_tree": str(self.work / "test-tree"),
                   "unit_manifest": {"path": str(manifest), "manifest_sha256": result.manifest["manifest_sha256"], "self_check": "passed"}}
        (out / "entry-gates.json").write_text(json.dumps(summary))
        (out / "gates-manifest.json").write_text(json.dumps({"profile": "full-gates", "gates": gates}))
        self.world.sample(T0 + 150)

    def finish_successor(self, path: Path, request: Path, *, returncode: int = 0, commit: str | None = None,
                         **kwargs: Any) -> dict[str, Any]:
        """跑 ``bv.run``：替身 Popen 先做派发断言（沿用旧 WORK、请求原路径），再按 ``gate_result`` 造出门禁成果。"""

        commit = commit or self.c8

        def dispatch(argv: list[str], **options: Any) -> Any:
            self.case.assertEqual(argv[argv.index("--mode") + 1], "unit-inherit")
            self.case.assertEqual(argv[argv.index("--work") + 1], str(self.work), "跨提交后继沿用来源的物理 WORK")
            self.case.assertEqual(argv[argv.index("--unit-inherit-request") + 1], str(request))
            self.case.assertEqual(argv[-1], commit)
            self.case.assertEqual(options["env"]["GOCACHE"], str(self.work / "go-build-cache"))
            self.gate_result(path, request, commit=commit, **kwargs)
            return type("CompletedGate", (), {"wait": lambda self: returncode})()

        with patch.object(bv.subprocess, "Popen", side_effect=dispatch):
            bv.run(path)
        return bv._read(path)

    finish_h8 = finish_successor
