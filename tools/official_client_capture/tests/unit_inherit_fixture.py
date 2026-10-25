"""B5a-08 逐单元承接的合成世界（测试辅助）：一次严格读集审计过的来源运行、当前事实、请求与两份批准，以及按执行器同一
流程在进程内走完三个复核点、发布决策收据与运行清单的驱动。合同测试与消费端（P0 v2、pre-A3）测试共用。

单元与 P0 两个门禁同构：``capture:prerequisites`` 加测试组 ``capture-tools``（两个测试单元）组成 test-capture-tools，两个
``egress-spec:`` 子检查组成 check-egress-spec；另有 pre-A3 场景单元（日志里一行场景结果）。来源记录的读集轨迹只读一个宿主
文件，记录登记了它的精确快照；当前事实的输入声明不含宿主快照——与真实执行器一样，由承接判定按来源沿用、重新快照。
时间全部是替身时刻（``T0`` 起算），校时样本来自请求登记的 ``file:`` 来源，每个复核点前按该时刻重写（模拟外部校时源刷新）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.ci import read_audit as ra
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur

# 与被测模块同一份 full_set_receipt（unit_inherit 按路径加载的那一个实例）：异常类型一致，测试才能按类型断言。
fs = ui.fs

T0 = fs.utc("2026-10-05T12:00:00Z")
SOURCE_RUN = "20261005t110000z-1-source"
COMMIT = "c" * 40
TESTS: dict[str, dict[str, Any]] = {
    "test_alpha": {"test_alpha.AlphaTests.test_a": {"outcome": "passed"},
                   "test_alpha.AlphaTests.test_skipped": {"outcome": "skipped", "reason": "示例跳过"}},
    "test_beta": {"test_beta.BetaTests.test_b": {"outcome": "passed"}},
}
CAPTURE_COMMAND = "capture:prerequisites"
EGRESS = ("egress-spec:check-a", "egress-spec:check-b")
DEFAULT_SCENARIOS = ("fake.alpha", "fake.beta")
GATES = ("check-egress-spec", "pre-a3", "test-capture-tools")
SCENARIO_RESULT_PREFIX = "PRE_A3_SCENARIO_RESULT "


def stamp(moment: float) -> str:
    """执行记录与清单用的时间写法（与 unit_records.utc_now 相同：到秒、Z 结尾）。"""

    return fs.timestamp(moment).split(".")[0].removesuffix("Z") + "Z"


@dataclass
class RunResult:
    """一次进程内运行（三个复核点＋清单）的结果。"""

    session: Any
    decisions: dict[str, Any]
    manifest: dict[str, Any]
    manifest_path: Path
    problems: list[str]
    decision: dict[str, Any] | None
    executed_records: dict[str, dict[str, Any]] = field(default_factory=dict)


class World:
    def __init__(self, root: Path, *, scenarios: Sequence[str] = DEFAULT_SCENARIOS, stage: str = "pre-ledger",
                 extra_units: Sequence[str] = (), inputs: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = ur.RecordStore(self.root / "store")
        self.control = self.root / "data" / "control"
        self.control.mkdir(parents=True)
        self.deploy = self.control / "codex-fixture-supervisor-enable-20261005t000000z.json"
        self.deploy.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.stage = stage
        self.scenarios = tuple(scenarios)
        self.units = (CAPTURE_COMMAND, *TESTS, *EGRESS, *(f"pre-a3:{name}" for name in self.scenarios), *extra_units)
        # 单元的当前输入声明：缺省是一项合成摘要；给了 inputs 的单元用真实声明展开的明细（例如 pre-A3 场景单元）。
        self.declared = dict(inputs or {})
        self.environment = [ur.value_entry("environment", "executor:arch", "aarch64")]
        files = {"unit_executor.py": "e" * 64}
        self.executor = {"files": files, "sha256": ur.sha256_json(files)}
        self.policy = "5" * 64
        self.host_input = self.root / "host" / "input.txt"
        self.host_input.parent.mkdir()
        self.host_input.write_text("单元实际读到的宿主文件\n", encoding="utf-8")
        trace = ra.filter_stream([f'1 openat(AT_FDCWD<{self.root}>, "{self.host_input}", O_RDONLY) = 3<{self.host_input}>\n'],
                                 ["/"], strict=True)
        trace_path = self.root / "trace.json"
        trace_path.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
        self.trace_sha256 = ur.file_sha256(trace_path)
        self.store.put_log(trace_path, self.trace_sha256)
        self.clock_file = self.root / "clock" / "sample.json"
        self.clock_file.parent.mkdir()
        self.counter = 0
        self.records = {unit_id: self.source_record(unit_id) for unit_id in self.units}
        self.source_manifest = self.publish_run(SOURCE_RUN, "re-execute", {unit_id: (record, "executed") for unit_id, record in self.records.items()},
                                                decided=T0 - 3600)
        self.currents = {unit_id: self.current(unit_id) for unit_id in self.units}

    # ------------------------------------------------------------------ 单元的当前事实与记录

    def unit_type(self, unit_id: str) -> str:
        return "test" if unit_id in TESTS else "command"

    def spec(self, unit_id: str) -> dict[str, Any]:
        return {"unit_id": unit_id, "argv": ["fixture", unit_id]}

    def base_inputs(self, unit_id: str) -> list[dict[str, Any]]:
        """当前声明的输入（执行器 _gate_currents 的形状）：不含宿主快照。"""

        if unit_id in self.declared:
            return list(self.declared[unit_id])
        return [ur.value_entry("tests", f"file:{unit_id}", f"内容-{unit_id}")]

    def record_inputs(self, unit_id: str) -> list[dict[str, Any]]:
        """来源记录登记的输入：当前声明＋精确宿主快照＋要求读集闭合的策略项（严格审计运行的形状）。"""

        return ur._unique([*self.base_inputs(unit_id), ra.host_snapshot(str(self.host_input)),
                           ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])

    def current(self, unit_id: str, **overrides: Any) -> Any:
        spec = overrides.pop("spec", self.spec(unit_id))
        inputs = overrides.pop("inputs", self.base_inputs(unit_id))
        return ur.Current(unit_id=unit_id, unit_type=self.unit_type(unit_id), spec=spec, spec_sha256=ur.sha256_json(spec),
                          inputs=inputs, inputs_sha256=ur.entries_sha256(inputs), inheritable=True, **overrides)

    def facts(self, now: float, **overrides: Any) -> Any:
        values = dict(policy_sha256=self.policy, environment=self.environment, environment_sha256=ur.entries_sha256(self.environment),
                      executor=self.executor, max_age_hours=168.0, now=now)
        values.update(overrides)
        return ur.RunFacts(**values)

    def log(self, text: str) -> dict[str, Any]:
        self.counter += 1
        path = self.root / "logs" / f"{self.counter}.log"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest = ur.file_sha256(path)
        self.store.put_log(path, digest)
        return {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}

    def scenario_log(self, unit_id: str) -> str:
        name = unit_id.removeprefix("pre-a3:")
        result = ur.seal({"schema_version": "pre-a3-scenario-result/v2", "name": name, "kind": "formal",
                          "scenario": {"name": name, "description": "替身", "test": "tools.fake:FakeTests.x", "status": "passed",
                                       "seconds": 0.1},
                          "network_attempts": 0, "finished_at_utc": "2026-10-05T11:00:00.000Z", "staging_root": "/staging/x"},
                         "result_sha256")
        return "场景自己的输出\n" + SCENARIO_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"

    def source_record(self, unit_id: str, *, run_id: str = SOURCE_RUN, completed: float | None = None, audit: bool = True,
                      mode: str = "re-execute", **overrides: Any) -> dict[str, Any]:
        """一条严格读集审计过的正式执行记录（入库）。"""

        inputs = overrides.pop("inputs", self.record_inputs(unit_id) if audit else self.base_inputs(unit_id))
        spec = overrides.pop("spec", self.spec(unit_id))
        tests = TESTS.get(unit_id)
        moment = stamp(T0 - 3600 if completed is None else completed)
        body = {
            "unit_id": unit_id, "unit_type": self.unit_type(unit_id), "kind": "formal",
            "run": {"run_id": run_id, "mode": mode, "out_dir": "/out"}, "executor": self.executor, "policy_sha256": self.policy,
            "environment": self.environment, "environment_sha256": ur.entries_sha256(self.environment),
            "spec": spec, "spec_sha256": ur.sha256_json(spec), "inputs": inputs, "inputs_sha256": ur.entries_sha256(inputs),
            "inheritable": True, "not_inheritable_reason": None, "test_ids": sorted(tests or {}), "tests": tests, "passed": True,
            "exit_code": 0, "signal": None, "timed_out": False, "seconds": 1.0, "cpu_seconds": 0.5, "max_rss_mb": 30.0, "cores": 1,
            "memory_mb": 128, "orphans": [],
            "log": self.log(self.scenario_log(unit_id) if unit_id.startswith("pre-a3:") else f"{unit_id} 的日志\n"),
            "started_at_utc": moment, "completed_at_utc": moment,
        }
        if audit:
            body["read_audit"] = self.audit_block(inputs)
        body.update(overrides)
        record = ur.seal_record(body)
        self.store.put_record(record)
        return record

    def audit_block(self, inputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """来源记录的读集审计块（子类可以改成窄化记录的形状，见 ``test_codex_closeout_inherit``）。"""

        return {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1",
                "undeclared_count": 0, "undeclared": [], "inputs_sha256": ur.entries_sha256(inputs),
                "trace": {"sha256": self.trace_sha256}, "repo_root": str(self.root), "data_root": None}

    def entry(self, record: Mapping[str, Any], disposition: str, reasons: Sequence[str] | None = None) -> dict[str, Any]:
        unit_id = str(record["unit_id"])
        gates = (["test-capture-tools"] if unit_id in TESTS or unit_id == CAPTURE_COMMAND
                 else ["pre-a3"] if unit_id.startswith("pre-a3:") else ["check-egress-spec"])
        entry = {"unit_id": unit_id, "unit_type": record["unit_type"], "gates": gates,
                 "test_group": "capture-tools" if unit_id in TESTS else None, "disposition": disposition,
                 "record_sha256": record["record_sha256"], "record_path": str(self.store.record_path(unit_id, record["record_sha256"])),
                 "passed": True, "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"], "inheritable": True}
        if disposition == "inherited":
            entry["basis"] = {"run_id": record["run"]["run_id"], "completed_at_utc": record["completed_at_utc"]}
        else:
            entry["reasons"] = list(reasons) if reasons is not None else ["示例：本次执行"]
        return entry

    def manifest_body(self, run_id: str, mode: str, rows: Mapping[str, tuple[Mapping[str, Any], str]], *, decided: float,
                      max_age: float = 168.0, reasons: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
        entries = [self.entry(record, disposition, (reasons or {}).get(unit_id))
                   for unit_id, (record, disposition) in sorted(rows.items())]
        return ur.build_manifest(
            run_id=run_id, mode=mode, inheritance_max_age_hours=max_age, record_store=str(self.store.root), out_dir="/out",
            decided_at_utc=stamp(decided), completed_at_utc=stamp(decided), policy_sha256=self.policy, environment=self.environment,
            environment_sha256=ur.entries_sha256(self.environment), executor=self.executor,
            planned_units=[entry["unit_id"] for entry in entries], units=entries, diagnostic=[],
            test_groups={"capture-tools": sorted(test_id for tests in TESTS.values() for test_id in tests)},
            counts={"planned": len(entries), "executed": sum(1 for e in entries if e["disposition"] == "executed"),
                    "inherited": sum(1 for e in entries if e["disposition"] == "inherited"), "inherited_tests": 0})

    def publish_run(self, run_id: str, mode: str, rows: Mapping[str, tuple[Mapping[str, Any], str]], *, decided: float) -> dict[str, Any]:
        manifest = self.manifest_body(run_id, mode, rows, decided=decided)
        self.store.put_manifest(manifest)
        return manifest

    # ------------------------------------------------------------------ 请求与凭证

    def write(self, name: str, payload: Mapping[str, Any]) -> dict[str, str]:
        self.counter += 1
        path = self.root / "inputs" / f"{self.counter:03d}-{name}"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return fs.reference(path)

    def context(self, **overrides: Any) -> dict[str, Any]:
        context = ui.current_context(client="codex", stage=self.stage, commit=COMMIT, deployments=ui.deployment_set(self.control),
                                     environment_sha256=ur.entries_sha256(self.environment), policy_sha256=self.policy,
                                     executor_sha256=self.executor["sha256"], gate_ids=GATES, profile="entry",
                                     record_store=self.store.root)
        context.update(overrides)
        return context

    def sources(self, units: Sequence[str] | None = None, records: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        chosen = {unit_id: (records or self.records)[unit_id] for unit_id in (units if units is not None else self.units)}
        runs: dict[str, str] = {}
        for record in chosen.values():
            run_id = record["run"]["run_id"]
            runs[run_id] = self.store.manifest(run_id)["manifest_sha256"]
        return {"schema_version": ui.SOURCES_SCHEMA, "runs": runs,
                "units": {unit_id: {"run_id": record["run"]["run_id"], "record_sha256": record["record_sha256"]}
                          for unit_id, record in chosen.items()}}

    def sample(self, moment: float, **overrides: Any) -> None:
        """外部校时源写的样本（请求登记的 file: 来源）：每个复核点之前按那个时刻刷新。"""

        payload = {"status": "synchronized", "source": "替身校时源", "offset_seconds": 0, "sampled_at_utc": fs.timestamp(moment)}
        payload.update(overrides)
        self.clock_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def approval(self, role: str, binding: str, **overrides: Any) -> dict[str, str]:
        payload = {"role": role, "status": "approved", "account": "替身审核人", "scope": ui.CONTRACT, "binding_sha256": binding,
                   "approved_at_utc": fs.timestamp(T0 - 86400), "expires_at_utc": fs.timestamp(T0 + 2 * 86400)}
        payload.update(overrides)
        return self.write(f"approval-{role}.json", payload)

    def request(self, *, at: float = T0, reuse_enabled: bool = True, context: Mapping[str, Any] | None = None,
                sources: Mapping[str, Any] | None = None, approvals: list[dict[str, str]] | None = None,
                drop: Sequence[str] = (), **extra: Any) -> Path:
        context = dict(context or self.context())
        sources = dict(sources or self.sources())
        binding = ui.binding_sha256(context, sources)
        request: dict[str, Any] = {
            "schema_version": ui.REQUEST_SCHEMA, "reuse_enabled": reuse_enabled, "context": self.write("context.json", context),
            "sources": sources,
            "consumer_clock": self.write("request-clock.json", {"status": "synchronized", "source": "替身校时源", "offset_seconds": 0,
                                                                "sampled_at_utc": fs.timestamp(at)}),
            "revocation": self.write("request-revocation.json",
                                     ui.query_revocation(self.store.root, sorted(sources["runs"]), self.stage, at,
                                                         extra.get("campaign_status_source"))),
            "approvals": approvals if approvals is not None else [self.approval(role, binding) for role in sorted(fs.APPROVAL_ROLES)],
            "clock_source": f"file:{self.clock_file}",
        }
        request.update(extra)
        for key in drop:
            request.pop(key, None)
        self.counter += 1
        path = self.root / "inputs" / f"{self.counter:03d}-request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ 进程内走一次运行

    def run(self, request: Path, *, run_id: str | None = None, times: Sequence[float] = (T0, T0 + 60, T0 + 120),
            currents: Mapping[str, Any] | None = None, facts: Callable[[float], Any] | None = None,
            context: Callable[[str], Mapping[str, Any] | None] | None = None,
            before: Callable[[str, float], None] | None = None, publish: bool = True) -> RunResult:
        """照执行器 ``_run_gates`` 的顺序走一次：排队前复核 →（有承接时）锁后复核 → 执行其余单元（这里直接写一条本次运行的
        记录）→ 发布前复核 → 决策收据先入库、再封进清单 → 清单自检通过才发布。``before`` 在每个复核点之前调用（变异用）。"""

        self.counter += 1
        run_id = run_id or f"20261005t120000z-{self.counter}-current"
        currents = dict(currents or {unit_id: self.current(unit_id) for unit_id in self.units})
        make_facts = facts or self.facts
        make_context = context or (lambda name: self.context())
        session = ui.Session(request, store_root=self.store.root, run_id=run_id)
        decisions: dict[str, Any] = {}
        mode = ui.MODE
        for name, moment in zip(("queue", "lock"), times[:2]):
            if mode != ui.MODE:
                break
            self.sample(moment)
            if before is not None:
                before(name, moment)
            decisions = session.checkpoint(name, currents, make_facts(moment), make_context(name), now=moment)
            if session.authorized and not any(decision.inherit for decision in decisions.values()):
                session.fallback(f"{name} 复核通过，但没有可承接的单元：整次重新执行")
            if not session.authorized:
                mode = "re-execute"
        executed = {unit_id: self.source_record(unit_id, run_id=run_id, completed=times[1] + 10, mode=mode)
                    for unit_id in self.units if not decisions[unit_id].inherit}
        rows = {unit_id: ((decisions[unit_id].record, "inherited") if decisions[unit_id].inherit else (executed[unit_id], "executed"))
                for unit_id in self.units}
        manifest = self.manifest_body(run_id, mode, rows, decided=times[0],
                                      reasons={unit_id: decision.reasons for unit_id, decision in decisions.items()})
        problems: list[str] = []
        decision = None
        if mode == ui.MODE:
            self.sample(times[2])
            if before is not None:
                before("publish", times[2])
            published = session.checkpoint("publish", {unit_id: self.current(unit_id) for unit_id in self.units},
                                           make_facts(times[2]), make_context("publish"), now=times[2])
            problems = session.publish_problems(published)
            decision = session.build_decision(decisions)
            reference = {"path": str(self.store.decision_path(run_id)), "decision_sha256": decision["decision_sha256"],
                         "context_sha256": decision["context_sha256"], "sources_sha256": decision["sources_sha256"]}
            if not problems:
                reference = session.publish_decision(decision)
            manifest = ur.seal({**{key: value for key, value in manifest.items() if key != "manifest_sha256"},
                                "unit_inherit_decision": reference}, "manifest_sha256")
        problems = [*ur.verify_manifest(manifest, store=self.store), *problems]
        if publish and not problems:
            self.store.put_manifest(manifest)
        path = self.root / f"unit-manifest-{run_id}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return RunResult(session=session, decisions=decisions, manifest=manifest, manifest_path=path, problems=problems,
                         decision=decision, executed_records=executed)

    # ------------------------------------------------------------------ P0 v2 证据

    def p0_evidence(self, manifest: Mapping[str, Any], identity: Mapping[str, str], directory: Path) -> tuple[Path, Path]:
        """照入口门禁 export 的形状写两份 v2 P0 证据（运行清单引用、记录库、门禁项单元与计数、工具五摘要）。"""

        rows = {entry["unit_id"]: entry for entry in manifest["units"]}

        def counts(members: Sequence[str]) -> dict[str, int]:
            dispositions = [rows[unit_id]["disposition"] for unit_id in members]
            return {"executed": dispositions.count("executed"), "inherited": dispositions.count("inherited")}

        common = {"unit_manifest": {"run_id": manifest["run_id"], "manifest_sha256": manifest["manifest_sha256"], "mode": manifest["mode"]},
                  "record_store": str(self.store.root), "tool_identity": dict(identity)}
        base = {"working_directory": "/tree", "git_commit": COMMIT, "status": "passed", "exit_code": 0, "failed": 0,
                "unexpected_skip": 0, "elapsed_seconds": 60.0, "raw_errors": [], "temporary_asset_inventory": [],
                "executed_as": "合成", "executor_summary": "/out/summary.json"}
        capture = {"schema_version": "codex-p0-offline-gate-evidence/v2", "gate_id": "test-capture-tools",
                   "command": ["make", "test-capture-tools"], **base, "passed": 2, "approved_skip": 1, **common,
                   "units": [CAPTURE_COMMAND], "test_group": "capture-tools",
                   "unit_counts": counts([CAPTURE_COMMAND, *TESTS]), "expected_tests": 3, "reported_tests": 3,
                   "skipped": [{"test_id": "test_alpha.AlphaTests.test_skipped", "reason": "示例跳过"}]}
        egress = {"schema_version": "codex-p0-offline-gate-evidence/v2", "gate_id": "check-egress-spec",
                  "command": ["make", "check-egress-spec"], **base, "passed": 2, "approved_skip": 0, **common,
                  "units": list(EGRESS), "test_group": None, "unit_counts": counts(EGRESS),
                  "checks": [{"target": unit_id.removeprefix("egress-spec:"), "passed": True, "exit_code": 0, "seconds": 1.0,
                              "log": "/logs/x.log"} for unit_id in EGRESS]}
        directory.mkdir(parents=True, exist_ok=True)
        capture_path, egress_path = directory / "test-capture-tools.json", directory / "check-egress-spec.json"
        capture_path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
        egress_path.write_text(json.dumps(egress, ensure_ascii=False), encoding="utf-8")
        return capture_path, egress_path
