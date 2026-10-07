"""B5a-08 受控的逐单元承接合同（``tools/ci/unit_inherit.py``）：授权、逐单元判定、三个复核点、决策收据、清单自检与消费端
重验，按设计验收逐条核对（合成世界见 ``unit_inherit_fixture.py``，时间全是替身时刻，不真等）。

* 只重跑按输入声明失效或没有有效记录的单元；请求缺失、校时过期、任一批准失效、上下文变化时整次重新执行；
* 任一来源运行被撤销只重跑指向它的单元；五种变异（记录篡改、规格漂移、环境漂移、读集未闭合、超期）各自只让对应单元重跑；
* 锁后复核只缩不扩，发布前复核失败清单不发布（已执行单元的记录保留）；
* 混合执行超过 300 秒：按复核点刷新凭证后照常发布，原凭证已过期，历史重放按形成期快照与原判定时间通过；
* 消费端对缺决策收据、决策收据被改、凭证过期的清单拒签。
执行器真跑见 ``test_ci_unit_inherit_executor.py``，P0 与 pre-A3 的签发见 ``test_unit_inherit_consumers.py``。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import unit_executor as ue
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import unit_inherit_fixture as fx

# 与被测模块同一份 full_set_receipt：ContractError 是同一个类。
fs = ui.fs
T0 = fx.T0


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.worlds = 0

    def world(self, **options: object) -> fx.World:
        self.worlds += 1
        return fx.World(self.base / f"world-{self.worlds}", **options)

    @staticmethod
    def inherited(result: fx.RunResult) -> set[str]:
        return {entry["unit_id"] for entry in result.manifest["units"] if entry["disposition"] == "inherited"}

    @staticmethod
    def executed(result: fx.RunResult) -> set[str]:
        return {entry["unit_id"] for entry in result.manifest["units"] if entry["disposition"] == "executed"}

    def assertReexecutedAll(self, result: fx.RunResult, reason: str) -> None:
        self.assertEqual(result.manifest["mode"], "re-execute", result.session.checkpoints)
        self.assertEqual(self.inherited(result), set())
        self.assertEqual(result.problems, [])
        self.assertEqual(result.session.action(), "reexecute-all")
        reasons = "；".join(reason for item in result.session.checkpoints for reason in item["reasons"])
        self.assertIn(reason, reasons + "；".join(result.session.reasons))
        self.assertNotIn("unit_inherit_decision", result.manifest, "整次重新执行的清单保持 re-execute 的原形状")


class AuthorizedInheritanceTests(_Base):
    def test_all_valid_units_are_inherited_and_the_decision_is_published_with_the_manifest(self) -> None:
        world = self.world()
        request = world.request()
        evidence_before = set((ui.inherit_root(world.store.root) / "evidence").glob("*.json"))
        result = world.run(request)
        self.assertEqual(result.problems, [])
        self.assertEqual(result.manifest["mode"], "unit-inherit")
        self.assertEqual(self.inherited(result), set(world.units), "来源记录全部有效：全部承接")
        self.assertEqual(result.session.action(), "inherit-matching-units")
        self.assertEqual([item["checkpoint"] for item in result.session.checkpoints], list(ui.CHECKPOINTS))
        decision = json.loads(world.store.decision_path(result.manifest["run_id"]).read_text(encoding="utf-8"))
        self.assertTrue(ur.seal_ok(decision, "decision_sha256"))
        self.assertEqual(result.manifest["unit_inherit_decision"]["decision_sha256"], decision["decision_sha256"])
        self.assertEqual(decision["source_run_ids"], [fx.SOURCE_RUN])
        self.assertEqual(ur.verify_manifest(result.manifest, store=world.store), [])
        self.assertEqual(world.store.manifest(result.manifest["run_id"]), result.manifest, "清单自检通过才发布")
        # 每个复核点一份新的校时与撤销快照（新文件、新摘要），请求自带的初始凭证原样留着。
        clocks = [item["consumer_clock"]["sha256"] for item in decision["checkpoints"]]
        revocations = [item["revocation"]["sha256"] for item in decision["checkpoints"]]
        self.assertEqual(len(set(clocks)), 3)
        self.assertEqual(len(set(revocations)), 3)
        evidence_after = set((ui.inherit_root(world.store.root) / "evidence").glob("*.json"))
        self.assertTrue(evidence_before <= evidence_after)
        original = json.loads(request.read_text(encoding="utf-8"))
        self.assertEqual(fs.replay_ref(original["consumer_clock"])["sampled_at_utc"], fs.timestamp(T0), "初始凭证没有被覆盖")

    def test_only_units_without_a_valid_record_rerun(self) -> None:
        """来源集合只登记了一部分单元：其余单元「没有有效记录」，各自执行；登记了的照常承接。"""

        world = self.world()
        listed = ("test_alpha", "egress-spec:check-a", "pre-a3:fake.alpha")
        result = world.run(world.request(sources=world.sources(listed)))
        self.assertEqual(result.problems, [])
        self.assertEqual(self.inherited(result), set(listed))
        self.assertEqual(self.executed(result), set(world.units) - set(listed))
        reasons = {entry["unit_id"]: entry.get("reasons") for entry in result.manifest["units"] if entry["disposition"] == "executed"}
        self.assertTrue(all("来源集合没有登记这个单元" in " ".join(item) for item in reasons.values()), reasons)


class WholeRunFallbackTests(_Base):
    """请求缺失、校时过期、任一批准失效、当前上下文与请求不同：整次重新执行（不混用部分承接）。"""

    def test_missing_or_malformed_request_reexecutes_everything(self) -> None:
        world = self.world()
        self.assertReexecutedAll(world.run(world.root / "no-such-request.json"), "No such file")
        for field in ("consumer_clock", "revocation", "approvals", "context", "sources", "reuse_enabled"):
            with self.subTest(missing=field):
                self.assertReexecutedAll(world.run(world.request(drop=(field,))), "缺少")

    def test_expired_or_unconfirmed_clock_reexecutes_everything(self) -> None:
        cases = {
            "样本过旧（超过 300 秒）": ({"sampled_at_utc": fs.timestamp(T0 - 301)}, "校时凭证不在本次判定的有效窗口内"),
            "偏差超过 60 秒": ({"offset_seconds": 61}, "时钟偏差不可确认或超过 60 秒"),
            "没有同步": ({"status": "unsynchronized"}, "时钟偏差不可确认或超过 60 秒"),
        }
        for label, (change, reason) in cases.items():
            with self.subTest(label):
                world = self.world()
                original = world.sample
                world.sample = lambda moment, _c=change, _o=original, **kw: _o(moment, **{**_c, **kw})  # type: ignore[method-assign]
                self.assertReexecutedAll(world.run(world.request()), reason)
        world = self.world()
        stale = world.request(at=T0 - 86400 - 10)
        self.assertReexecutedAll(world.run(stale), "批准有效期")

    def test_any_invalid_approval_reexecutes_everything(self) -> None:
        cases = {
            "已过期": {"expires_at_utc": fs.timestamp(T0)},
            "撤回": {"status": "withdrawn"},
            "错绑": {"binding_sha256": "0" * 64},
            "范围不对": {"scope": "full-set-inheritance/v1"},
            "没有账号": {"account": ""},
        }
        for label, change in cases.items():
            with self.subTest(label):
                world = self.world()
                context, sources = world.context(), world.sources()
                binding = ui.binding_sha256(context, sources)
                approvals = [world.approval("change_approver", binding, **change), world.approval("third_party_reviewer", binding)]
                self.assertReexecutedAll(world.run(world.request(context=context, sources=sources, approvals=approvals)), "批准")
        world = self.world()
        binding = ui.binding_sha256(world.context(), world.sources())
        self.assertReexecutedAll(world.run(world.request(approvals=[world.approval("change_approver", binding)])), "缺少三方审核")

    def test_context_change_reexecutes_everything(self) -> None:
        changes = {
            "commit": {"commit": "d" * 40},
            "environment_sha256": {"environment_sha256": "1" * 64},
            "policy_sha256": {"policy_sha256": "2" * 64},
            "executor_sha256": {"executor_sha256": "3" * 64},
            "stage": {"stage": "post-ledger"},
            "client": {"client": "claude"},
            "gates": {"gates": ["check-egress-spec"]},
        }
        for field, change in changes.items():
            with self.subTest(field):
                world = self.world()
                self.assertReexecutedAll(world.run(world.request(), context=lambda name, c=change: world.context(**c)), field)
        world = self.world()
        request = world.request()
        newer = world.control / "codex-fixture-supervisor-enable-20261005t010000z.json"
        newer.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.assertReexecutedAll(world.run(request), "deployment_receipts")

    def test_closed_switch_records_eligibility_without_inheriting(self) -> None:
        world = self.world()
        result = world.run(world.request(reuse_enabled=False))
        self.assertReexecutedAll(result, "开关关闭")
        queue = result.session.checkpoints[0]
        self.assertEqual(set(queue["inherited"]), set(world.units), "开关关闭仍记录可承接判定")


class UnitLevelRerunTests(_Base):
    def test_revoked_source_run_reruns_only_units_pointing_to_it(self) -> None:
        world = self.world()
        second = "20261005t113000z-3-second"
        moved = ("test_beta", "egress-spec:check-b")
        records = dict(world.records)
        rows = {}
        for unit_id in moved:
            records[unit_id] = world.source_record(unit_id, run_id=second, completed=T0 - 1800)
            rows[unit_id] = (records[unit_id], "executed")
        world.publish_run(second, "re-execute", rows, decided=T0 - 1800)
        sources = world.sources(records=records)
        self.assertEqual(sorted(sources["runs"]), sorted([fx.SOURCE_RUN, second]), "来源集合跨两次运行")
        request = world.request(sources=sources)
        ui.revoke(world.store.root, second, reason="示例：撤销第二次来源运行", actor="替身操作员", now=T0 - 10)
        result = world.run(request)
        self.assertEqual(result.problems, [])
        self.assertEqual(self.executed(result), set(moved), "只重跑指向被撤销运行的单元")
        self.assertEqual(self.inherited(result), set(world.units) - set(moved))
        self.assertEqual(result.decision["checkpoints"][0]["revoked_run_ids"], [second])
        with self.assertRaises(FileExistsError):
            ui.revoke(world.store.root, second, reason="重复撤销", actor="替身操作员")

    def test_each_mutation_reruns_only_its_unit(self) -> None:
        """五种变异各自只让对应单元重跑：记录篡改、规格漂移、环境漂移、读集未闭合、超期。"""

        def tampered(world: fx.World) -> tuple[dict, dict]:
            record = world.records["test_beta"]
            path = world.store.record_path("test_beta", record["record_sha256"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps({**payload, "seconds": 9.0}, ensure_ascii=False), encoding="utf-8")
            return {}, {}

        def drifted_spec(world: fx.World) -> tuple[dict, dict]:
            return {}, {"egress-spec:check-b": world.current("egress-spec:check-b", spec={"unit_id": "egress-spec:check-b", "argv": ["改了"]})}

        def isolated(world: fx.World, unit_id: str, run_id: str, **overrides: object) -> tuple[dict, dict]:
            record = world.source_record(unit_id, run_id=run_id, **overrides)
            world.publish_run(run_id, "re-execute", {unit_id: (record, "executed")}, decided=T0 - 3600)
            return {unit_id: record}, {}

        mutations = {
            "记录篡改": ("test_beta", tampered, "自摘要不符"),
            "规格漂移": ("egress-spec:check-b", drifted_spec, "单元规格变了"),
            "环境漂移": ("capture:prerequisites", lambda world: isolated(
                world, "capture:prerequisites", "20261005t100000z-9-env",
                environment=[ur.value_entry("environment", "executor:arch", "x86_64")],
                environment_sha256=ur.entries_sha256([ur.value_entry("environment", "executor:arch", "x86_64")])), "环境变了"),
            "读集未闭合": ("pre-a3:fake.beta", lambda world: isolated(
                world, "pre-a3:fake.beta", "20261005t100000z-9-audit", read_audit={"status": "passed", "coverage_complete": False}),
                "读集覆盖未闭合"),
            "超期": ("test_alpha", lambda world: isolated(world, "test_alpha", "20261005t100000z-9-old", completed=T0 - 169 * 3600),
                     "超过承接期限"),
        }
        for label, (unit_id, mutate, reason) in mutations.items():
            with self.subTest(label):
                world = self.world()
                replaced, currents = mutate(world)
                sources = world.sources(records={**world.records, **replaced})
                result = world.run(world.request(sources=sources), currents={**{u: world.current(u) for u in world.units}, **currents})
                self.assertEqual(result.problems, [])
                self.assertEqual(self.executed(result), {unit_id})
                self.assertEqual(self.inherited(result), set(world.units) - {unit_id})
                row = next(entry for entry in result.manifest["units"] if entry["unit_id"] == unit_id)
                self.assertIn(reason, " ".join(row["reasons"]))

    def test_control_root_change_reruns_claude_scenarios_and_inherits_codex_scenarios(self) -> None:
        """只改 tools/official_client_control 下的文件：Claude 场景单元的输入声明失效重跑，Codex 场景单元照常承接（两客户端
        场景单元的输入声明来自入口门禁 pre_a3_inputs，展开算法与执行器相同，见 B5a-06 的同类测试）。"""

        from tools.ci import entry_gates as eg
        from tools.official_client_capture.tests.test_driver_client_table import EntryGatesClaudeTests

        scenario_test = EntryGatesClaudeTests.SCENARIO_TEST
        data_inputs = [{"category": "environment", "name": "tree:fixed", "sha256": "0" * 64, "detail": {}}]
        declarations = {"pre-a3:fx": eg.pre_a3_inputs(scenario_test, data_inputs, "codex"),
                        "pre-a3:claude:fx": eg.pre_a3_inputs(scenario_test, data_inputs, "claude")}
        cache: dict = {}

        def declared(repo: object) -> dict[str, list]:
            return {unit_id: ur.declared_inputs(repo, declaration, cache) for unit_id, declaration in declarations.items()}

        before = declared(EntryGatesClaudeTests._repo(self, {}))  # type: ignore[arg-type]
        after = declared(EntryGatesClaudeTests._repo(self, {"tools/official_client_control/store.py": "1" * 64}))  # type: ignore[arg-type]
        world = self.world(scenarios=(), extra_units=tuple(declarations), inputs=before)
        currents = {unit_id: world.current(unit_id) for unit_id in world.units}
        currents.update({unit_id: world.current(unit_id, inputs=inputs) for unit_id, inputs in after.items()})
        result = world.run(world.request(), currents=currents)
        self.assertEqual(result.problems, [])
        self.assertEqual(self.executed(result), {"pre-a3:claude:fx"})
        self.assertIn("pre-a3:fx", self.inherited(result))
        row = next(entry for entry in result.manifest["units"] if entry["unit_id"] == "pre-a3:claude:fx")
        self.assertIn("repo:official_client_control", " ".join(row["reasons"]))

    def test_unpublished_or_replaced_source_manifest_reruns_its_units(self) -> None:
        world = self.world()
        sources = world.sources()
        sources["runs"][fx.SOURCE_RUN] = "0" * 64
        result = world.run(world.request(sources=sources))
        self.assertReexecutedAll(result, "没有可承接的单元")
        self.assertIn("清单没有发布进记录库，或与来源集合登记的摘要不同",
                      " ".join(reason for decision in result.decisions.values() for reason in decision.reasons))


class CheckpointTests(_Base):
    def test_lock_checkpoint_only_narrows_the_inherited_set(self) -> None:
        world = self.world()
        second = "20261005t113000z-3-second"
        records = dict(world.records)
        records["test_beta"] = world.source_record("test_beta", run_id=second, completed=T0 - 1800)
        world.publish_run(second, "re-execute", {"test_beta": (records["test_beta"], "executed")}, decided=T0 - 1800)

        def revoke_before_lock(name: str, moment: float) -> None:
            if name == "lock":
                ui.revoke(world.store.root, second, reason="排队期间撤销", actor="替身操作员", now=moment)

        result = world.run(world.request(sources=world.sources(records=records)), before=revoke_before_lock)
        self.assertEqual(result.problems, [])
        queue, lock, publish = result.decision["checkpoints"]
        self.assertIn("test_beta", queue["inherited"])
        self.assertNotIn("test_beta", lock["inherited"])
        self.assertEqual(self.executed(result), {"test_beta"})

    def test_publish_refusal_withholds_decision_and_manifest_but_keeps_executed_records(self) -> None:
        world = self.world()
        listed = tuple(unit for unit in world.units if unit != "egress-spec:check-b")

        def revoke_before_publish(name: str, moment: float) -> None:
            if name == "publish":
                ui.revoke(world.store.root, fx.SOURCE_RUN, reason="执行期间撤销", actor="替身操作员", now=moment)

        result = world.run(world.request(sources=world.sources(listed)), before=revoke_before_publish)
        self.assertEqual(result.session.action(), "publish-refused")
        self.assertTrue(any("发布前复核不再可承接" in problem for problem in result.problems), result.problems)
        self.assertFalse(world.store.decision_path(result.manifest["run_id"]).exists(), "决策收据不发布")
        self.assertIsNone(world.store.manifest(result.manifest["run_id"]), "清单不发布")
        executed = result.executed_records["egress-spec:check-b"]
        self.assertTrue(world.store.record_path("egress-spec:check-b", executed["record_sha256"]).is_file(), "已执行的记录保留")

    def test_request_changed_or_withdrawn_during_the_run_is_refused(self) -> None:
        world = self.world()
        request = world.request()

        def edit(name: str, moment: float) -> None:
            if name == "lock":
                payload = json.loads(request.read_text(encoding="utf-8"))
                payload["max_age_hours"] = 24
                request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        self.assertReexecutedAll(world.run(request, before=edit), "运行期间被改动")
        world = self.world()
        request = world.request()
        result = world.run(request, before=lambda name, moment: request.unlink() if name == "publish" else None)
        self.assertEqual(result.session.action(), "publish-refused")
        self.assertIsNone(world.store.manifest(result.manifest["run_id"]))

    def test_approval_withdrawn_during_the_run_is_refused(self) -> None:
        world = self.world()
        request = world.request()
        approval = Path(json.loads(request.read_text(encoding="utf-8"))["approvals"][0]["path"])

        def withdraw(name: str, moment: float) -> None:
            if name == "lock":
                payload = json.loads(approval.read_text(encoding="utf-8"))
                approval.write_text(json.dumps({**payload, "status": "withdrawn"}, ensure_ascii=False), encoding="utf-8")

        self.assertReexecutedAll(world.run(request, before=withdraw), "证据文件摘要发生变化")

    def test_checkpoint_order_is_enforced(self) -> None:
        world = self.world()
        session = ui.Session(world.request(), store_root=world.store.root, run_id="20261005t120000z-9-order")
        decisions = session.checkpoint("lock", world.currents, world.facts(T0), world.context(), now=T0)
        self.assertFalse(session.authorized)
        self.assertIn("复核点顺序非法", session.checkpoints[0]["reasons"][0])
        self.assertFalse(any(decision.inherit for decision in decisions.values()))


class LongMixedRunTests(_Base):
    def test_mixed_run_longer_than_300_seconds_refreshes_credentials_and_still_publishes(self) -> None:
        world = self.world()
        request = world.request(at=T0)
        times = (T0, T0 + 200, T0 + 400)
        result = world.run(request, times=times, currents={**{u: world.current(u) for u in world.units}})
        self.assertEqual(result.problems, [])
        self.assertEqual(result.session.action(), "inherit-matching-units")
        initial = json.loads(request.read_text(encoding="utf-8"))["consumer_clock"]
        with self.assertRaisesRegex(fs.ContractError, "校时凭证不在本次判定的有效窗口内"):
            fs.clock_check(initial, T0 + 400)
        sampled = [fs.replay_ref(item["consumer_clock"])["sampled_at_utc"] for item in result.decision["checkpoints"]]
        self.assertEqual(sampled, [fs.timestamp(moment) for moment in times], "每个复核点按自己的时刻刷新")
        # 消费端签发时（T0+450）再刷新一次；十天后批准早已过期，只有历史重放仍按形成期快照与原判定时间通过。
        world.sample(T0 + 450)
        with mock.patch.object(ui, "current_time", return_value=T0 + 450):
            proof = ui.verify_published(result.manifest, world.store.root, purpose="acceptance")
        self.assertEqual((proof["inherited"], proof["executed"]), (len(world.units), 0))
        later = T0 + 10 * 86400
        world.sample(later)
        with mock.patch.object(ui, "current_time", return_value=later):
            with self.assertRaisesRegex(fs.ContractError, "批准"):
                ui.verify_published(result.manifest, world.store.root, purpose="acceptance")
            replayed = ui.verify_published(result.manifest, world.store.root, purpose="acceptance", historical=True,
                                           consumption=proof["consumption"])
        self.assertEqual(replayed["decision_sha256"], proof["decision_sha256"])


class ConsumerTests(_Base):
    def _published(self) -> tuple[fx.World, fx.RunResult]:
        world = self.world()
        result = world.run(world.request())
        self.assertEqual(result.problems, [])
        world.sample(T0 + 150)
        return world, result

    def consume(self, world: fx.World, manifest: dict, moment: float = T0 + 150) -> dict:
        with mock.patch.object(ui, "current_time", return_value=moment):
            return ui.verify_published(manifest, world.store.root, purpose="acceptance")

    def test_consumption_records_fresh_credentials_and_a_consumption_receipt(self) -> None:
        world, result = self._published()
        proof = self.consume(world, result.manifest)
        consumption = fs.replay_ref(proof["consumption"])
        self.assertEqual(consumption["schema_version"], ui.CONSUMPTION_SCHEMA)
        self.assertEqual(consumption["decision_sha256"], result.decision["decision_sha256"])
        self.assertEqual(fs.replay_ref(consumption["consumer_clock"])["sampled_at_utc"], fs.timestamp(T0 + 150))
        self.assertEqual(set(proof["verifier"]), set(ui.VERIFIER_FILES))

    def test_missing_or_tampered_decision_is_refused(self) -> None:
        world, result = self._published()
        path = world.store.decision_path(result.manifest["run_id"])
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(fs.ContractError, "决策收据"):
            self.consume(world, result.manifest)
        payload = json.loads(original)
        payload["units"]["test_beta"]["disposition"] = "executed"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(fs.ContractError, "决策收据"):
            self.consume(world, result.manifest)
        resealed = ur.seal({key: value for key, value in payload.items() if key != "decision_sha256"}, "decision_sha256")
        path.write_text(json.dumps(resealed, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(fs.ContractError, "决策收据"):
            self.consume(world, result.manifest)
        path.write_bytes(original)
        self.consume(world, result.manifest)

    def test_tampered_sealed_evidence_is_refused(self) -> None:
        world, result = self._published()
        sealed = Path(result.decision["sealed_request"]["path"])
        original = sealed.read_bytes()
        sealed.write_bytes(original.replace(b'"reuse_enabled": true', b'"reuse_enabled": false'))
        with self.assertRaisesRegex(fs.ContractError, "摘要"):
            self.consume(world, result.manifest)

    def test_expired_credentials_at_consumption_are_refused(self) -> None:
        world, result = self._published()
        world.sample(T0 + 150 - 400)
        with self.assertRaisesRegex(fs.ContractError, "校时"):
            self.consume(world, result.manifest)
        world.sample(T0 + 3 * 86400)
        with self.assertRaisesRegex(fs.ContractError, "批准"):
            self.consume(world, result.manifest, T0 + 3 * 86400)
        world.sample(T0 + 150)
        ui.revoke(world.store.root, fx.SOURCE_RUN, reason="签发前撤销", actor="替身操作员", now=T0 + 100)
        with self.assertRaisesRegex(fs.ContractError, "已撤销"):
            self.consume(world, result.manifest)

    def test_withdrawn_or_removed_approval_at_consumption_is_refused(self) -> None:
        world, result = self._published()
        request = json.loads(Path(result.decision["request"]["path"]).read_text(encoding="utf-8"))
        approval = Path(request["approvals"][1]["path"])
        original = approval.read_bytes()
        approval.write_text(json.dumps({**json.loads(original), "status": "withdrawn"}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(fs.ContractError, "证据文件摘要发生变化"):
            self.consume(world, result.manifest)
        approval.unlink()
        with self.assertRaises((fs.ContractError, OSError)):
            self.consume(world, result.manifest)
        approval.write_bytes(original)
        self.consume(world, result.manifest)
        # 历史重放只用封存副本：原批准文件撤回之后，签发时的证明照常可重放。
        approval.unlink()
        ui.verify_published(result.manifest, world.store.root, purpose="acceptance", historical=True)

    def test_unpublished_or_non_inheriting_manifest_is_refused(self) -> None:
        world, result = self._published()
        with self.assertRaisesRegex(fs.ContractError, "不是逐单元承接"):
            ui.verify_published(world.source_manifest, world.store.root, purpose="acceptance")
        edited = ur.seal({**{k: v for k, v in result.manifest.items() if k != "manifest_sha256"}, "out_dir": "/elsewhere"}, "manifest_sha256")
        with self.assertRaisesRegex(fs.ContractError, "没有发布进记录库"):
            self.consume(world, edited)


class FormalStageTests(_Base):
    def test_formal_stage_requires_an_active_campaign(self) -> None:
        world = self.world(stage="formal:claude-test-campaign")
        status = world.root / "campaign-status.json"

        def write_status(value: str):
            def before(name: str, moment: float) -> None:
                status.write_text(json.dumps({"schema_version": ui.CAMPAIGN_STATUS_SCHEMA, "campaign_id": "claude-test-campaign",
                                              "status": value, "observed_at_utc": fs.timestamp(moment)}), encoding="utf-8")
            return before

        write_status("active")("queue", T0)
        self.assertReexecutedAll(world.run(world.request()), "Campaign")
        request = world.request(campaign_status_source=f"file:{status}")
        result = world.run(request, before=write_status("active"))
        self.assertEqual(result.problems, [])
        self.assertEqual(self.inherited(result), set(world.units))
        write_status("closed")("queue", T0)
        self.assertReexecutedAll(world.run(world.request(campaign_status_source=f"file:{status}"), before=write_status("closed")),
                                 "Campaign 不再 active")


class ProposalAndCommandLineTests(_Base):
    def test_proposal_lists_the_newest_inheritable_record_and_assembles_a_valid_request(self) -> None:
        world = self.world()
        newer = world.source_record("test_alpha", run_id="20261005t113000z-4-newer", completed=T0 - 600)
        world.publish_run("20261005t113000z-4-newer", "re-execute", {"test_alpha": (newer, "executed")}, decided=T0 - 600)
        open_audit = world.source_record("test_beta", run_id="20261005t114000z-5-open", completed=T0 - 300,
                                         read_audit={"status": "passed", "coverage_complete": False})
        world.publish_run("20261005t114000z-5-open", "re-execute", {"test_beta": (open_audit, "executed")}, decided=T0 - 300)
        currents = {unit_id: world.current(unit_id) for unit_id in world.units}
        before = {unit_id: current.inputs_sha256 for unit_id, current in currents.items()}
        sources = ui.propose(world.store.root, currents, world.facts(T0))
        self.assertEqual({unit_id: current.inputs_sha256 for unit_id, current in currents.items()}, before, "提案不改动当前事实")
        self.assertEqual(sources["units"]["test_alpha"]["record_sha256"], newer["record_sha256"], "取完成时间最新的可承接记录")
        self.assertEqual(sources["units"]["test_beta"]["record_sha256"], world.records["test_beta"]["record_sha256"],
                         "读集未闭合的新记录不入选，退回仍可承接的旧记录")
        summary = ui.write_proposal(world.root / "proposal", context=world.context(), sources=sources)
        self.assertEqual(summary["binding_sha256"], ui.binding_sha256(world.context(), sources))
        clock = world.root / "clock.json"
        clock.write_text(json.dumps({"status": "synchronized", "source": "替身", "offset_seconds": 0, "sampled_at_utc": fs.timestamp(T0)}))
        revocation = world.root / "revocation.json"
        revocation.write_text(json.dumps(ui.query_revocation(world.store.root, sorted(sources["runs"]), world.stage, T0)), encoding="utf-8")
        approvals = [Path(world.approval(role, summary["binding_sha256"])["path"]) for role in sorted(fs.APPROVAL_ROLES)]
        output = world.root / "assembled-request.json"
        ui.assemble_request(world.root / "proposal", clock=clock, revocation=revocation, approvals=approvals, reuse_enabled=True,
                            output=output, clock_source=f"file:{world.clock_file}")
        result = world.run(output)
        self.assertEqual(result.problems, [])
        self.assertEqual(self.inherited(result), set(world.units))

    def test_command_line_subcommands(self) -> None:
        world = self.world()
        result = world.run(world.request())
        world.sample(T0 + 150)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(ui, "current_time", return_value=T0 + 150):
            self.assertEqual(ui.main(["sample-clock", "--source", f"file:{world.clock_file}", "--output", str(world.root / "s.json")]), 0)
            self.assertEqual(ui.main(["query-revocation", "--store", str(world.store.root), "--sources", str(world.root / "s.json"),
                                      "--stage", "pre-ledger", "--output", str(world.root / "q.json")]), 3)
            request = sorted((world.root / "inputs").glob("*-request.json"))[-1]
            self.assertEqual(ui.main(["query-revocation", "--store", str(world.store.root), "--sources", str(request),
                                      "--stage", "pre-ledger", "--output", str(world.root / "q.json")]), 0)
            self.assertEqual(ui.main(["verify", "--manifest", str(result.manifest_path), "--store", str(world.store.root)]), 0)
            self.assertEqual(ui.main(["verify", "--manifest", str(result.manifest_path), "--store", str(world.store.root),
                                      "--historical"]), 0)
            self.assertEqual(ui.main(["revoke", "--store", str(world.store.root), "--run-id", fx.SOURCE_RUN, "--reason", "示例",
                                      "--actor", "替身"]), 0)
            self.assertEqual(ui.main(["verify", "--manifest", str(result.manifest_path), "--store", str(world.store.root)]), 3)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line["status"] for line in lines], ["ok", "refused", "ok", "ok", "ok", "ok", "refused"])
        self.assertIn("已撤销", lines[-1]["reason"])


class ReadAuditorTests(unittest.TestCase):
    """逐单元承接下执行的单元照常取证；承接的单元不执行、没有轨迹，读集覆盖只按本次执行的单元判（没有承接时与改造前相同）。"""

    def report(self, inherited: frozenset[str], results: dict[str, dict]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            currents = {unit_id: ur.Current(unit_id, "command", {}, "s", [], ur.entries_sha256([]), True) for unit_id in ("a", "b")}
            auditor = ue.ReadAuditor(out_dir=root, repo_root=root, data_root=None, currents=currents, strict=True)
            auditor.inherited = inherited
            auditor.results = dict(results)
            summary = auditor.report()
            written = json.loads((root / "audit" / "read-audit.json").read_text(encoding="utf-8"))
        return {"summary": summary, "written": written}

    def test_audit_coverage_counts_only_executed_units_when_inheriting(self) -> None:
        complete = {"coverage_complete": True, "status": "passed", "undeclared_count": 0}
        codex = self.report(frozenset(), {"a": complete})
        self.assertEqual((codex["summary"]["status"], codex["summary"]["missing_units"]), ("failed", ["b"]), "没有承接：与改造前相同")
        self.assertNotIn("inherited_units", codex["written"])
        self.assertEqual(self.report(frozenset(), {"a": complete, "b": complete})["summary"]["coverage_complete"], True)
        mixed = self.report(frozenset({"b"}), {"a": complete})
        self.assertEqual((mixed["summary"]["status"], mixed["summary"]["coverage_complete"], mixed["summary"]["missing_units"]),
                         ("passed", True, []))
        self.assertEqual(mixed["written"]["inherited_units"], ["b"])
        everything = self.report(frozenset({"a", "b"}), {})
        self.assertEqual((everything["summary"]["status"], everything["summary"]["coverage_complete"]), ("passed", True))
        self.assertEqual(self.report(frozenset({"b"}), {})["summary"]["missing_units"], ["a"], "执行的单元没取证照样算缺")


class ClockSamplingTests(unittest.TestCase):
    def test_chronyc_tracking_is_parsed_or_fails_closed(self) -> None:
        line = "A9FEA97B,169.254.169.123,4,1696680000.123456789,-0.000012345,0.000001,0.000023,-12.3,0.001,0.012,0.000345,0.012345,64.5,Normal\n"
        completed = subprocess.CompletedProcess(["chronyc"], 0, stdout=line, stderr="")
        with mock.patch.object(ui.subprocess, "run", return_value=completed):
            sample = ui.sample_clock("chronyc", T0)
        self.assertEqual((sample["status"], sample["offset_seconds"], sample["sampled_at_utc"]), ("synchronized", -0.000012345, fs.timestamp(T0)))
        unsynced = subprocess.CompletedProcess(["chronyc"], 0, stdout=line.replace("Normal", "Not synchronised"), stderr="")
        with mock.patch.object(ui.subprocess, "run", return_value=unsynced):
            self.assertEqual(ui.sample_clock("chronyc", T0)["status"], "unsynchronized")
        with mock.patch.object(ui.subprocess, "run", side_effect=FileNotFoundError("chronyc")):
            sample = ui.sample_clock("chronyc", T0)
        self.assertEqual(sample["status"], "unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "sample.json"
            path.write_text(json.dumps(sample), encoding="utf-8")
            with self.assertRaisesRegex(fs.ContractError, "时钟偏差不可确认"):
                fs.clock_check(fs.reference(path), T0)
        for source in ("file:relative.json", "ntp://example", "file:/a/../b.json"):
            with self.subTest(source), self.assertRaises(fs.ContractError):
                ui.sample_clock(source, T0)


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        self.assertEqual((repo / "tools/ci/unit_inherit.py").read_bytes(),
                         (repo / "tools/arm64_capture_driver/driver/unit_inherit.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
