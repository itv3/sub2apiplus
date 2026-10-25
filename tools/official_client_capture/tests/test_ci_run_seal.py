"""第 16 项：中断运行的封存清单 ``tools/ci/run_seal.py`` 与承接侧对部分完成清单的接受规则。

用 ``unit_inherit_fixture.World`` 的记录库造一次“被 kill -9 停在半路”的运行：运行目录里有计划、事件流和已完成单元的记录，记录库里有记录与日志，没有清单。覆盖：

* 后继封存：只列已落库、自摘要合法、日志匹配、属于该运行的正式执行；``planned_units`` 保持全集；清单经 ``verify_manifest`` 自检通过；
* 进程必须已死：调度器状态目录锁被占用、事件流里有存活的单元进程组、事件流读不出来，一律不封存；已有清单（正常收尾或已封存）时不覆盖，已封存时幂等；
* 承接侧：逐单元承接接受部分清单（已完成的单元被承接、未完成的重跑），B-09／B-10／收尾的全集通道（``allow_partial_origin`` 缺省 False）一律拒绝；
  清单形状被改、列了承接项、列了计划之外的单元都过不了自检；
* 后台验证：aborted／superseded／vanished 的来源在后继封存之后才能当来源，封存不成时仍按原来的说法拒绝。
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.ci import background_validation as bv
from tools.ci import run_seal as seal
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import unit_inherit_fixture as fx

T0 = fx.T0
RUN_ID = "20261005t100000z-1-interrupted"


class InterruptedRunBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.world = fx.World(Path(self.directory.name).resolve() / "world")
        self.store = self.world.store
        self.state = Path(self.directory.name).resolve() / "state"
        self.run_dir = Path(self.directory.name).resolve() / "interrupted-run"
        self.finished = [fx.CAPTURE_COMMAND, "test_alpha", "test_beta"]
        self.records = {}
        (self.run_dir / "units").mkdir(parents=True)
        for index, unit_id in enumerate(self.finished):
            record = self.world.source_record(unit_id, run_id=RUN_ID, completed=T0 - 1800 + index)
            self.records[unit_id] = record
            (self.run_dir / "units" / f"{unit_id.replace(':', '-')}.record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.write_plan()
        self.write_events([])

    def write_plan(self) -> None:
        gates = [{"gate_id": "test-capture-tools", "units": [fx.CAPTURE_COMMAND], "test_groups": ["capture-tools"]},
                 {"gate_id": "check-egress-spec", "units": list(fx.EGRESS)}]
        (self.run_dir / "plan.json").write_text(json.dumps({
            "run_id": RUN_ID, "record_store": str(self.store.root), "gates": gates,
            "units": [{"unit_id": unit_id} for unit_id in self.world.units]}, ensure_ascii=False), encoding="utf-8")

    def write_events(self, rows: list[dict]) -> None:
        (self.run_dir / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def seal(self, state: Path | None = None, origin: str = "aborted") -> dict:
        return seal.seal_interrupted_run(store_root=self.store.root, run_dir=self.run_dir, origin_state=origin, state_dir=state or self.state)


class SealTests(InterruptedRunBase):
    def test_a_dead_run_is_sealed_with_only_its_complete_formal_records(self) -> None:
        receipt = self.seal()
        manifest = self.store.manifest(RUN_ID)
        self.assertEqual((receipt["listed_units"], receipt["planned_units"]), (3, len(self.world.units)))
        self.assertTrue(manifest["partial"] and manifest["sealed_by"] == "successor" and manifest["origin_state"] == "aborted")
        self.assertEqual(sorted(entry["unit_id"] for entry in manifest["units"]), sorted(self.finished))
        self.assertEqual(manifest["planned_units"], list(self.world.units), "planned_units 保持全集")
        self.assertTrue(all(entry["disposition"] == "executed" for entry in manifest["units"]))
        self.assertEqual(ur.verify_manifest(manifest, store=self.store), [])
        self.assertEqual(manifest["manifest_sha256"], receipt["manifest_sha256"])

    def test_incomplete_or_foreign_records_are_not_listed(self) -> None:
        # 一个没有完成时间的记录（单元没跑完）、一个属于别的运行的记录、一个库里那份被改过的记录，都不列。
        unfinished = self.world.source_record("egress-spec:check-a", run_id=RUN_ID, completed=T0 - 1000, completed_at_utc="")
        foreign = self.world.source_record("egress-spec:check-b", run_id="some-other-run", completed=T0 - 1000)
        for name, record in (("unfinished", unfinished), ("foreign", foreign)):
            (self.run_dir / "units" / f"{name}.record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tampered = self.records["test_beta"]
        stored = self.store.record_path("test_beta", tampered["record_sha256"])
        stored.write_text(stored.read_text(encoding="utf-8").replace('"exit_code": 0', '"exit_code": 9'), encoding="utf-8")
        receipt = self.seal()
        listed = {entry["unit_id"] for entry in self.store.manifest(RUN_ID)["units"]}
        self.assertEqual(listed, {fx.CAPTURE_COMMAND, "test_alpha"})
        reasons = {item.get("unit_id"): item["reason"] for item in receipt["skipped_records"]}
        self.assertIn("没有完成时间", reasons["egress-spec:check-a"])
        self.assertIn("不属于这个运行", reasons["egress-spec:check-b"])
        self.assertIn("不是同一份", reasons["test_beta"])

    def test_nothing_to_seal_is_refused(self) -> None:
        for path in (self.run_dir / "units").glob("*.json"):
            path.unlink()
        with self.assertRaisesRegex(seal.SealError, "没有任何已落库且完整"):
            self.seal()
        self.assertIsNone(self.store.manifest(RUN_ID))

    def test_a_live_scheduler_blocks_sealing(self) -> None:
        self.state.mkdir(mode=0o700)
        descriptor = os.open(self.state / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with self.assertRaisesRegex(seal.SealError, "调度器持有状态目录锁"):
            self.seal()
        self.assertFalse(self.store.manifest_path(RUN_ID).exists())

    def test_a_live_unit_process_group_blocks_sealing(self) -> None:
        group = os.getpgrp()
        self.write_events([{"event": "start", "unit": "test_alpha", "pid": group}])
        with self.assertRaisesRegex(seal.SealError, "仍然存活"):
            self.seal()
        self.write_events([{"event": "start", "unit": "test_alpha", "pid": group}, {"event": "exit", "unit": "test_alpha", "pid": group}])
        self.assertTrue(self.seal()["listed_units"])

    def test_unreadable_events_mean_unconfirmed_and_no_seal(self) -> None:
        (self.run_dir / "events.jsonl").write_text("不是 JSON\n", encoding="utf-8")
        with self.assertRaisesRegex(seal.SealError, "读不懂"):
            self.seal()
        (self.run_dir / "events.jsonl").unlink()
        with self.assertRaisesRegex(seal.SealError, "确认不了"):
            self.seal()

    def test_an_existing_manifest_is_never_overwritten_and_resealing_is_idempotent(self) -> None:
        first = self.seal()
        again = self.seal()
        self.assertEqual(first, again, "已封存：幂等地返回同一份收据")
        other = Path(self.directory.name).resolve() / "published-run"
        (other / "units").mkdir(parents=True)
        plan = json.loads((self.run_dir / "plan.json").read_text())
        plan["run_id"] = "20261005t100100z-2-published"
        (other / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (other / "events.jsonl").write_text("", encoding="utf-8")
        self.store.put_manifest(self.world.manifest_body("20261005t100100z-2-published", "re-execute",
                                                         {unit_id: (record, "executed") for unit_id, record in self.records.items()}, decided=T0))
        with self.assertRaisesRegex(seal.SealError, "已有清单"):
            seal.seal_interrupted_run(store_root=self.store.root, run_dir=other, origin_state="aborted", state_dir=self.state)

    def test_origin_state_and_store_are_checked(self) -> None:
        with self.assertRaisesRegex(seal.SealError, "原运行终态"):
            self.seal(origin="running")
        plan = json.loads((self.run_dir / "plan.json").read_text())
        plan["record_store"] = str(Path(self.directory.name) / "elsewhere")
        (self.run_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(seal.SealError, "记录库"):
            self.seal()

    def test_the_command_line_seals_and_refuses_with_the_right_exit_code(self) -> None:
        self.assertEqual(seal.main(["seal", "--store", str(self.store.root), "--run-dir", str(self.run_dir), "--origin-state", "vanished",
                                    "--state-dir", str(self.state)]), 0)
        self.assertEqual(self.store.manifest(RUN_ID)["origin_state"], "vanished")


class AcceptanceTests(InterruptedRunBase):
    def facts(self, **overrides):
        return self.world.facts(T0, require_read_audit=True, **overrides)

    def test_only_the_per_unit_inherit_path_accepts_a_partial_manifest(self) -> None:
        self.seal()
        record = self.records["test_alpha"]
        path = self.store.record_path("test_alpha", record["record_sha256"])
        current = self.world.current("test_alpha")
        ui._carry_host_inputs(current, record)
        self.assertEqual(ur.check_record(self.store, path, record, current, self.facts(allow_partial_origin=True)), [])
        refused = ur.check_record(self.store, path, record, current, self.facts())
        self.assertTrue(any("部分完成清单" in problem and "不接受" in problem for problem in refused), refused)

    def test_finished_units_are_inherited_and_unfinished_ones_rerun_through_the_real_checkpoints(self) -> None:
        self.seal()
        listed = {unit_id: self.records[unit_id] for unit_id in self.finished}
        sources = self.world.sources(units=list(listed), records=listed)
        self.world.sample(T0)
        request = self.world.request(at=T0, sources=sources)
        result = self.world.run(request, times=(T0, T0 + 60, T0 + 120))
        self.assertEqual(result.problems, [])
        inherited = {unit_id for unit_id, decision in result.decisions.items() if decision.inherit}
        self.assertEqual(inherited, set(self.finished), "已完成的单元被承接，没跑完的单元重跑")
        self.assertEqual({entry["basis"]["run_id"] for entry in result.manifest["units"] if entry["disposition"] == "inherited"}, {RUN_ID})
        # 消费端历史重放同样接受（verify_published 的 facts 也放行部分清单）
        proof = ui.verify_published(result.manifest, self.store.root, purpose="test", historical=True)
        self.assertEqual(proof["inherited"], len(self.finished))

    def test_an_unsealed_run_gives_nothing_to_inherit(self) -> None:
        record = self.records["test_alpha"]
        path = self.store.record_path("test_alpha", record["record_sha256"])
        current = self.world.current("test_alpha")
        ui._carry_host_inputs(current, record)
        problems = ur.check_record(self.store, path, record, current, self.facts(allow_partial_origin=True))
        self.assertTrue(any("原运行的清单缺失" in problem for problem in problems), problems)


class PartialManifestShapeTests(InterruptedRunBase):
    def resealed(self, mutate) -> dict:
        self.seal()
        manifest = json.loads(json.dumps(self.store.manifest(RUN_ID)))
        mutate(manifest)
        return ur.seal({key: value for key, value in manifest.items() if key != "manifest_sha256"}, "manifest_sha256")

    def test_shape_and_coverage_rules(self) -> None:
        cases = {
            "封存者不是后继": (lambda m: m.__setitem__("sealed_by", "origin"), "sealed_by"),
            "终态不合法": (lambda m: m.__setitem__("origin_state", "passed"), "原运行终态"),
            "缺封存时间": (lambda m: m.__setitem__("sealed_at_utc", "x"), "封存时间"),
            "列了计划之外的单元": (lambda m: m["units"].append({**m["units"][0], "unit_id": "not-planned"}), "多"),
            "列了承接项": (lambda m: m["units"][0].__setitem__("disposition", "inherited"), "承接"),
        }
        for label, (mutate, fragment) in cases.items():
            with self.subTest(case=label):
                problems = ur.verify_manifest(self.resealed(mutate), store=self.store)
                self.assertTrue(any(fragment in problem for problem in problems), problems)

    def test_a_non_partial_manifest_still_has_to_cover_the_full_set(self) -> None:
        self.seal()
        manifest = json.loads(json.dumps(self.store.manifest(RUN_ID)))
        manifest.pop("partial")
        broken = ur.seal({key: value for key, value in manifest.items() if key != "manifest_sha256"}, "manifest_sha256")
        self.assertTrue(any("执行＋承接≠全集" in problem for problem in ur.verify_manifest(broken, store=self.store)))


class BackgroundSourceTests(InterruptedRunBase):
    """后台验证：被中断的来源在后继封存之后才能当来源。"""

    def setUp(self) -> None:
        super().setUp()
        self.out = Path(self.directory.name).resolve() / "bg-out"
        (self.out / "executor").mkdir(parents=True)
        self.run_dir = self.out / "executor"
        (self.run_dir / "units").mkdir(exist_ok=True)
        for index, unit_id in enumerate(self.finished):
            (self.run_dir / "units" / f"{index}.record.json").write_text(json.dumps(self.records[unit_id], ensure_ascii=False), encoding="utf-8")
        self.write_plan()
        self.write_events([])
        self.source = {"record_store": str(self.store.root), "out": str(self.out)}

    def test_sealing_on_demand_then_the_sealed_manifest_is_the_source(self) -> None:
        from unittest import mock
        executor = seal._executor()
        with mock.patch.object(executor, "default_state_dir", return_value=self.state):
            manifest = bv._partial_source_manifest(self.source, "superseded", self.out)
        self.assertTrue(manifest["partial"])
        self.assertEqual(manifest["origin_state"], "superseded")
        self.assertTrue((self.run_dir / seal.RECEIPT_NAME).is_file())
        again = bv._partial_source_manifest(self.source, "superseded", self.out)
        self.assertEqual(again, manifest)
        with self.assertRaisesRegex(bv.ValidationError, "不符"):
            bv._partial_source_manifest(self.source, "vanished", self.out)

    def test_an_unsealable_source_is_refused_with_the_old_reason(self) -> None:
        self.state.mkdir(mode=0o700)
        descriptor = os.open(self.state / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        from unittest import mock
        with mock.patch.object(seal._executor(), "default_state_dir", return_value=self.state):
            with self.assertRaisesRegex(bv.ValidationError, "被停下的运行没有发布运行清单.*封存部分完成清单没有成功"):
                bv._partial_source_manifest(self.source, "superseded", self.out)


if __name__ == "__main__":
    unittest.main()
