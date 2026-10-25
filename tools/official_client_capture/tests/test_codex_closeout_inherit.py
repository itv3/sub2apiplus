"""收尾门禁的固定测试树坐标（第 10a 项 D1）与逐单元承接（第 17 项）。

借 ``test_codex_closeout.CloseoutTests`` 的世界（真实 Git 候选、本地推送、隔离的指南与终态材料），另用 ``unit_inherit_fixture.World`` 造一次真实发布进
记录库的逐单元承接运行（运行清单、决策收据、批准、校时与撤销快照），再把它接成收尾门禁的输出文件。覆盖：

* 配置校验：不写新键时与改造前逐字相同；固定坐标必须是 ``entry-gates-source-work/fixed/<客户端>``；承接须同时给坐标、记录库和请求；
* 命令行：承接配置下门禁以 ``--mode unit-inherit`` 运行并带固定坐标、记录库、承接请求与阶段；没配置时仍是 ``re-execute`` 与 ``<work_root>/gate-work``；
* 校验：承接单元与运行清单逐一相符，运行清单经 ``verify_published`` 重放通过，决策收据缺失、清单被改、承接集合对不上、测试树坐标不是固定坐标一律拒绝；
  没配置承接时承接单元仍被拒绝（旧口径）；
* 收据：逐项列出承接单元（来源运行、来源记录摘要、视图摘要）与被重跑的单元和原因；
* 窄化来源记录的真实流程（不桩 ``verify_published``）：承接项带 ``inherit_check``，收尾校验全部真实运行；
* 一票否决（已知缺口 R2）：来源提交到候选提交之间有“同名文件变目录”之类的类型翻转时，收尾拒绝承接——跑门禁之前先拦、门禁结果出来后再独立复核一次；
  来源提交解析不出来同样失败关闭。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tools import codex_closeout as closeout
from tools.ci import entry_gates
from tools.ci import read_audit as ra
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_codex_closeout as base
from tools.official_client_capture.tests import unit_inherit_fixture as fx

T0 = fx.T0
write_json = base.write_json


class NarrowedFixtureWorld(fx.World):
    """来源记录是窄化记录（带 ``host_path_modes`` 与承接视图摘要，宿主输入是 v3 快照）的承接世界：承接项带 ``inherit_check``，
    当前事实带测试树坐标——与真实执行器写出的形状一致。"""

    def record_inputs(self, unit_id):
        return ur._unique([*self.base_inputs(unit_id), ra.host_snapshot_v3(str(self.host_input)),
                           ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])

    def audit_block(self, inputs):
        block = super().audit_block(inputs)
        document = json.loads((self.root / "trace.json").read_text(encoding="utf-8"))
        modes = ra.host_path_modes(document, list(inputs))
        block.update(host_path_modes=modes, inherit_view_schema=ra.INHERIT_VIEW_SCHEMA,
                     inherit_inputs_sha256=ra.inherit_view_digest(ra.inherit_view(list(inputs), modes, roots=(str(self.root), None))))
        return block

    def current(self, unit_id, **overrides):
        current = super().current(unit_id, **overrides)
        current.inherit_repo_root, current.inherit_data_root = str(self.root), None
        return current

    def entry(self, record, disposition, reasons=None):
        row = super().entry(record, disposition, reasons)
        if disposition == "inherited" and ur._narrowed_source(record):
            row["inherit_check"] = {"schema_version": ra.INHERIT_VIEW_SCHEMA, "repo_root": str(self.root), "data_root": None,
                                    "inherit_inputs_sha256": record["read_audit"]["inherit_inputs_sha256"]}
        return row


class CloseoutInheritBase(unittest.TestCase):
    def setUp(self) -> None:
        self.case = base.CloseoutTests("test_dry_run_does_not_create_work_or_modify_repository")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.fixed = self.root / "entry-gates-source-work" / "fixed" / "codex"
        # 来源运行的提交：默认是权威仓库的基线提交（收尾候选的祖先）；入口门禁摘要不在测试夹具里，所以换成固定值。用例可以改 ``self.source_commit``。
        self.source_commit = self.case.base
        patcher = mock.patch.object(closeout._inherit_predict(), "source_run_commit", side_effect=lambda _store, _run_id: self.source_commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inherit_config(self, world: fx.World, request: Path) -> dict:
        config = deepcopy(self.case.config)
        config["gate"].update(work=str(self.fixed), record_store=str(world.store.root),
                              unit_inherit={"request": str(request), "stage": "pre-ledger"})
        return config

    def world_run(self, *, rerun: str | None = None, factory=fx.World, root: Path | None = None):
        world = factory(root or self.root / "inherit-world")
        world.sample(T0)
        request = world.request(at=T0)
        currents = None
        if rerun is not None:
            currents = {unit_id: world.current(unit_id) for unit_id in world.units}
            changed = {**world.spec(rerun), "argv": ["fixture", "已改变的命令"]}
            currents[rerun] = world.current(rerun, spec=changed)
        result = world.run(request, times=(T0, T0 + 60, T0 + 120), currents=currents)
        self.assertEqual(result.problems, [])
        return world, request, result

    def write_gate_files(self, plan: dict, world: fx.World, result, *, mode: str = "unit-inherit", mutate=None) -> Path:
        """把一次真实发布的逐单元承接运行接成收尾门禁的输出文件（按真实导出的字段）。"""
        root = self.case.work / "gates"
        names = list(entry_gates.profile_gates("full-gates"))
        manifest_path = root / "unit-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(manifest_path, result.manifest)
        by_unit = {entry["unit_id"]: entry for entry in result.manifest["units"]}
        gate_units: dict[str, list[str]] = {names[0]: list(world.units), **{name: [name] for name in names[1:]}}
        summary_units, rows, manifest_rows = [], [], []
        for name in names:
            command, directory = entry_gates.GATE_COMMANDS[name]
            inherited = [unit for unit in gate_units[name] if by_unit.get(unit, {}).get("disposition") == "inherited"]
            write_json(root / "logs" / (name + ".gate.json"), {
                "gate_id": name, "command": command, "working_directory": directory, "status": "passed", "exit_code": 0,
                "tree_head": plan["candidate_commit"], "mode": mode, "failed_units": [], "inherited_units": inherited,
                "started_at_utc": "2026-10-05T00:00:00Z", "completed_at_utc": "2026-10-05T00:01:00Z"})
            rows.append({"gate_id": name, "status": "passed", "exit_code": 0, "gate_json": "logs/" + name + ".gate.json"})
            manifest_rows.append({"gate_id": name, "units": gate_units[name]})
            for unit in gate_units[name]:
                log = root / "executor" / (unit.replace(":", "-") + ".log")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("隔离结果夹具\n")
                summary_units.append({"unit_id": unit, "passed": True, "exit_code": 0, "orphans": 0, "log": str(log),
                                      "disposition": by_unit[unit]["disposition"] if unit in by_unit else "executed"})
        write_json(root / "entry-gates.json", {
            "schema_version": entry_gates.ENTRY_SUMMARY_SCHEMA, "status": "passed", "profile": "full-gates", "mode": mode,
            "source": {"commit": plan["candidate_commit"], "tree_head": plan["candidate_commit"]}, "gates": rows,
            "executor_summary": str(root / "executor/summary.json")})
        write_json(root / "gates-manifest.json", {"schema_version": entry_gates.GATES_SCHEMA, "profile": "full-gates", "gates": manifest_rows,
                                                   "units": [{"unit_id": row["unit_id"]} for row in summary_units]})
        summary = {"schema_version": entry_gates.GATES_SUMMARY_SCHEMA, "status": "passed", "mode": mode, "units": summary_units,
                   "units_not_run": [], "test_groups": {}, "run_id": result.manifest["run_id"], "record_store": str(world.store.root),
                   "unit_manifest": {"path": str(manifest_path), "manifest_sha256": result.manifest["manifest_sha256"]}}
        if mutate is not None:
            mutate(summary, manifest_path)
        write_json(root / "executor/summary.json", summary)
        return root

    def prepared(self, world: fx.World, request: Path) -> dict:
        self.case.config = self.inherit_config(world, request)
        plan = self.case.prepare()
        closeout.journal(plan, "gates", "started", {"fixture": True})
        return plan


class ConfigurationTests(CloseoutInheritBase):
    def test_configs_without_the_new_keys_are_unchanged(self) -> None:
        self.assertEqual(set(self.case.config["gate"]), {"driver", "vc_env", "node_modules"})
        plan = self.case.prepare()
        self.assertIsNone(closeout.gate_inherit_summary(plan), "没配置承接：没有承接汇总")

    def test_the_fixed_coordinate_must_be_the_shared_one(self) -> None:
        world, request, _ = self.world_run()
        for label, work, fragment in (
                ("在收尾工作根里", str(self.case.work / "gate-work"), "之外"),
                ("不是固定坐标的命名", str(self.root / "somewhere" / "tree"), "entry-gates-source-work/fixed"),
                ("客户端不对", str(self.root / "entry-gates-source-work" / "fixed" / "claude"), "entry-gates-source-work/fixed"),
                ("在权威仓库里", str(self.case.repo / "entry-gates-source-work" / "fixed" / "codex"), "之外")):
            with self.subTest(case=label):
                config = self.inherit_config(world, request)
                config["gate"]["work"] = work
                with self.assertRaisesRegex(closeout.CloseoutError, fragment):
                    closeout.validate_config(config)

    def test_inheritance_needs_the_coordinate_the_store_and_a_well_formed_request(self) -> None:
        world, request, _ = self.world_run()
        for label, mutate in (("缺固定坐标", lambda gate: gate.pop("work")), ("缺记录库", lambda gate: gate.pop("record_store")),
                              ("阶段非法", lambda gate: gate["unit_inherit"].__setitem__("stage", "closeout")),
                              ("多余键", lambda gate: gate["unit_inherit"].__setitem__("mode", "x")),
                              ("请求文件不存在", lambda gate: gate["unit_inherit"].__setitem__("request", str(self.root / "no-such.json")))):
            with self.subTest(case=label):
                config = self.inherit_config(world, request)
                mutate(config["gate"])
                with self.assertRaises(closeout.CloseoutError if label != "请求文件不存在" else closeout.safety.SafetyError):
                    closeout.validate_config(config)
        config = self.inherit_config(world, request)
        config["gate"].pop("unit_inherit")                       # 只给记录库不给承接：拒绝
        with self.assertRaisesRegex(closeout.CloseoutError, "只与逐单元承接同用"):
            closeout.validate_config(config)
        config = self.inherit_config(world, request)
        config["gate"]["surprise"] = 1
        with self.assertRaises(closeout.CloseoutError):
            closeout.validate_config(config)

    def test_the_assembled_request_is_bound_into_the_plan(self) -> None:
        world, request, _ = self.world_run()
        plan = self.prepared(world, request)
        self.assertIn(str(request), plan["basis"]["input_files"], "承接请求进输入绑定，换了请求就是另一份方案")


class RunGatesCommandTests(CloseoutInheritBase):
    def started_argv(self, plan: dict) -> list[str]:
        real = closeout.subprocess.run

        def fake(argv, *args, **kwargs):
            # 只替换门禁驱动那一次调用（bash entry-gates.sh …）：收尾里版本库命令也经 subprocess.run，不能一起换掉。
            return mock.Mock(returncode=1) if argv and argv[0] == "bash" else real(argv, *args, **kwargs)

        with mock.patch.object(closeout.subprocess, "run", side_effect=fake):
            with self.assertRaisesRegex(closeout.CloseoutError, "收尾全量门禁失败"):
                closeout.run_gates(self.case.plan_path)
        return closeout.journal_read(plan, "gates", "started")["argv"]

    def test_inherit_runs_the_gate_in_unit_inherit_mode_at_the_fixed_coordinate(self) -> None:
        world, request, _ = self.world_run()
        self.case.config = self.inherit_config(world, request)
        plan = self.case.prepare()
        argv = self.started_argv(plan)
        self.assertEqual(argv[argv.index("--mode") + 1], "unit-inherit")
        self.assertEqual(argv[argv.index("--work") + 1], str(self.fixed))
        self.assertEqual(argv[argv.index("--record-store") + 1], str(world.store.root))
        self.assertEqual(argv[argv.index("--unit-inherit-request") + 1], str(request))
        self.assertEqual(argv[argv.index("--unit-inherit-stage") + 1], "pre-ledger")

    def test_without_inherit_the_command_line_is_the_old_one(self) -> None:
        plan = self.case.prepare()
        argv = self.started_argv(plan)
        self.assertEqual(argv[argv.index("--mode") + 1], "re-execute")
        self.assertEqual(argv[argv.index("--work") + 1], str(self.case.work / "gate-work"))
        self.assertEqual(argv[argv.index("--record-store") + 1], str(self.case.work / "gate-records"))
        self.assertNotIn("--unit-inherit-request", argv)


class CheckInheritedGateTests(CloseoutInheritBase):
    def test_a_real_inherit_run_is_accepted_and_every_unit_is_listed(self) -> None:
        world, request, result = self.world_run(rerun="test_beta")
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        files = closeout.check_gate_result(plan)
        self.assertIn(str(self.case.work / "gates" / "unit-manifest.json"), files)
        self.assertIn(str(world.store.decision_path(result.manifest["run_id"])), files, "决策收据进不可变证据索引")
        summary = closeout.gate_inherit_summary(plan)
        self.assertEqual({item["unit_id"] for item in summary["inherited"]}, set(world.units) - {"test_beta"})
        self.assertEqual([item["unit_id"] for item in summary["rerun"]], ["test_beta"])
        self.assertTrue(summary["rerun"][0]["reasons"], "被重跑的单元写明原因")
        self.assertTrue(all(item["source_run_id"] == fx.SOURCE_RUN and len(item["source_record_sha256"]) == 64 for item in summary["inherited"]))

    def test_done_journal_records_the_consumption_and_the_itemised_summary(self) -> None:
        world, request, result = self.world_run()
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        verifier = closeout._unit_inherit()
        world.sample(T0 + 150)
        with mock.patch.object(verifier, "current_time", return_value=T0 + 150):
            done = closeout.run_gates(self.case.plan_path)
        self.assertEqual(done["status"], "passed")
        self.assertEqual(len(done["unit_inherit"]["inherited"]), len(world.units))
        self.assertIsNotNone(done["unit_inherit"]["consumption"], "刚跑完做过一次真消费，留了消费登记")

    def test_mode_and_inherited_units_follow_the_configuration(self) -> None:
        world, request, result = self.world_run()
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result, mode="re-execute")
        with self.assertRaisesRegex(closeout.CloseoutError, "未通过|不一致"):
            closeout.check_gate_result(plan)

    def test_without_inherit_configuration_any_inherited_unit_is_refused_as_before(self) -> None:
        world, request, result = self.world_run()
        plan = self.case.prepare()
        closeout.journal(plan, "gates", "started", {"fixture": True})
        self.write_gate_files(plan, world, result, mode="re-execute")
        with self.assertRaisesRegex(closeout.CloseoutError, "不一致|承接"):
            closeout.check_gate_result(plan)

    def test_tampered_evidence_is_refused(self) -> None:
        world, request, result = self.world_run()
        plan = self.prepared(world, request)

        def hide_one(summary, _path):
            row = next(item for item in summary["units"] if item["unit_id"] == "test_beta")
            row["disposition"] = "executed"
            for gate in (self.case.work / "gates" / "logs").glob("*.gate.json"):
                value = json.loads(gate.read_text())
                value["inherited_units"] = [unit for unit in value["inherited_units"] if unit != "test_beta"]
                write_json(gate, value)

        def wrong_hash(summary, _path):
            summary["unit_manifest"]["manifest_sha256"] = "0" * 64

        def foreign_store(summary, _path):
            summary["record_store"] = str(self.root / "elsewhere")

        for label, mutate, fragment in (("把承接单元说成执行", hide_one, "承接单元集合不同"),
                                         ("清单摘要对不上", wrong_hash, "不一致"),
                                         ("记录库不是配置的那个", foreign_store, "不一致")):
            with self.subTest(case=label):
                self.write_gate_files(plan, world, result, mutate=mutate)
                with self.assertRaisesRegex(closeout.CloseoutError, fragment):
                    closeout.check_gate_result(plan)
        self.write_gate_files(plan, world, result)
        decision = world.store.decision_path(result.manifest["run_id"])
        original = decision.read_bytes()
        decision.unlink()
        with self.assertRaisesRegex(closeout.CloseoutError, "决策收据"):
            closeout.check_gate_result(plan)
        decision.write_bytes(original)
        closeout.check_gate_result(plan)

    def test_the_test_tree_of_an_inherited_unit_must_be_the_fixed_coordinate(self) -> None:
        world, request, result = self.world_run()
        plan = self.prepared(world, request)
        forged = deepcopy(result.manifest)
        forged["units"][0]["inherit_check"] = {"schema_version": "unit-inherit-view/v1", "inherit_inputs_sha256": "a" * 64,
                                               "repo_root": "/some/commit-bound/test-tree", "data_root": None}
        unit_id = forged["units"][0]["unit_id"]
        forged = ur.seal({key: value for key, value in forged.items() if key != "manifest_sha256"}, "manifest_sha256")
        forged_result = mock.Mock(manifest=forged)
        self.write_gate_files(plan, world, forged_result)
        verifier = closeout._unit_inherit()
        with mock.patch.object(verifier, "verify_published", return_value={}):
            with self.assertRaisesRegex(closeout.CloseoutError, f"{unit_id}.*不是固定坐标"):
                closeout.check_gate_result(plan)
            forged["units"][0]["inherit_check"]["repo_root"] = str(self.fixed / "test-tree")
            forged = ur.seal({key: value for key, value in forged.items() if key != "manifest_sha256"}, "manifest_sha256")
            self.write_gate_files(plan, world, mock.Mock(manifest=forged))
            closeout.check_gate_result(plan)


class NarrowedCloseoutTests(CloseoutInheritBase):
    """窄化来源记录的真实收尾流程：``verify_published``、``check_record`` 的窄化分支、承接项 ``inherit_check`` 全部真实运行，不桩。"""

    def narrowed_run(self, *, rerun: str | None = None):
        return self.world_run(rerun=rerun, factory=NarrowedFixtureWorld, root=self.fixed / "test-tree")

    def commit_flip(self) -> None:
        """在权威仓库里让 ``docs/flip.txt`` 由文件变成同名目录（git 里是一条删除加一条新增，不是类型变化 T），并推到远端、更新预期的远端头。
        来源提交取“文件还是文件”的那个提交——来源看到的是文件，候选里是目录。"""
        repo = self.case.repo
        flip = repo / "docs" / "flip.txt"
        flip.parent.mkdir(exist_ok=True)
        flip.write_text("先是文件\n")
        self.case.git("add", "-A")
        self.case.git("commit", "-m", "加文件")
        self.source_commit = self.case.git("rev-parse", "HEAD")
        flip.unlink()
        flip.mkdir()
        (flip / "inside.txt").write_text("后来变成目录\n")
        self.case.git("add", "-A")
        self.case.git("commit", "-m", "文件变目录")
        self.case.git("push", "origin", "main")
        self.case.config["push"]["expected_tip"] = self.case.git("rev-parse", "HEAD")

    def test_a_real_narrowed_inherit_run_passes_every_closeout_check(self) -> None:
        world, request, result = self.narrowed_run(rerun="test_beta")
        inherited = [entry for entry in result.manifest["units"] if entry["disposition"] == "inherited"]
        self.assertTrue(inherited and all("inherit_check" in entry for entry in inherited), "（对照）承接项是窄化承接，带 inherit_check")
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        files = closeout.check_gate_result(plan)
        self.assertIn(str(world.store.decision_path(result.manifest["run_id"])), files)
        dispositions = {entry["disposition"] for entry in result.manifest["units"]}
        self.assertEqual(dispositions, {"executed", "inherited"}, "S1：承接模式下执行与承接两种处置都被允许")
        summary = closeout.gate_inherit_summary(plan)
        self.assertTrue(all(len(item["view_digest"] or "") == 64 for item in summary["inherited"]))

    def test_the_gate_record_cannot_claim_more_inherited_units_than_the_executor_summary(self) -> None:
        """S2：门禁分项记录登记的承接单元必须是执行器汇总承接单元的子集。"""
        world, request, result = self.narrowed_run(rerun="test_beta")
        plan = self.prepared(world, request)

        def claim_more(_summary, _path):
            for gate in (self.case.work / "gates" / "logs").glob("*.gate.json"):
                value = json.loads(gate.read_text())
                if value["inherited_units"]:
                    value["inherited_units"] = [*value["inherited_units"], "test_beta"]       # test_beta 在汇总里是“执行”
                    write_json(gate, value)

        self.write_gate_files(plan, world, result, mutate=claim_more)
        with self.assertRaisesRegex(closeout.CloseoutError, "门禁记录登记的承接单元不在执行器汇总"):
            closeout.check_gate_result(plan)

    def test_a_same_name_file_to_directory_flip_vetoes_the_inheritance_after_the_gate(self) -> None:
        """T10 在权威路径上的补丁：承接视图发现不了的“同名文件变目录”由 git 变更集一票否决——门禁结果被接受之前独立复核，拒绝。"""
        self.commit_flip()
        world, request, result = self.narrowed_run()
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        with self.assertRaisesRegex(closeout.CloseoutError, "一票否决.*docs/flip.txt|docs/flip.txt.*同名文件变成了目录"):
            closeout.check_gate_result(plan)

    def test_the_veto_also_blocks_the_gate_before_it_is_launched(self) -> None:
        self.commit_flip()
        world, request, _ = self.narrowed_run()
        self.case.config = self.inherit_config(world, request)
        plan = self.case.prepare()
        real = closeout.subprocess.run

        def forbid_the_driver(argv, *args, **kwargs):
            if argv and argv[0] == "bash":
                raise AssertionError("被否决后不应启动门禁")
            return real(argv, *args, **kwargs)

        with mock.patch.object(closeout.subprocess, "run", side_effect=forbid_the_driver):
            with self.assertRaisesRegex(closeout.CloseoutError, "一票否决.*docs/flip.txt：同名文件变成了目录"):
                closeout.run_gates(self.case.plan_path)
        self.assertIsNone(closeout.journal_read(plan, "gates", "started"), "没跑就拦下，不留“已启动”记录")

    def test_an_unresolvable_source_commit_fails_closed(self) -> None:
        world, request, result = self.narrowed_run()
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        self.source_commit = None
        with self.assertRaisesRegex(closeout.CloseoutError, "解析不出来"):
            closeout.check_gate_result(plan)
        self.source_commit = "1" * 40                            # 提交不在权威仓库的对象库里：读不出变更集
        with self.assertRaisesRegex(closeout.CloseoutError, "读不出"):
            closeout.check_gate_result(plan)
        self.source_commit = self.case.base
        closeout.check_gate_result(plan)

    def test_ordinary_edits_between_source_and_candidate_do_not_veto(self) -> None:
        self.case.repo.joinpath("docs").mkdir(exist_ok=True)
        (self.case.repo / "docs" / "ordinary.txt").write_text("普通新增\n")
        self.case.git("add", "-A")
        self.case.git("commit", "-m", "普通新增")
        self.case.git("push", "origin", "main")
        self.case.config["push"]["expected_tip"] = self.case.git("rev-parse", "HEAD")
        world, request, result = self.narrowed_run()
        plan = self.prepared(world, request)
        self.write_gate_files(plan, world, result)
        closeout.check_gate_result(plan)


if __name__ == "__main__":
    unittest.main()
