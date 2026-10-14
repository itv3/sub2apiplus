"""统一调度执行器的单元级超时（``tools/ci/unit_executor.json`` 的 ``unit_timeouts``，步骤 7）。

为什么要有这一项：严格读集审计会把重单元放大 4～17 倍（10-14 实测：evaluation_recovery 233 秒→950 秒，candidate_seal_evidence
不审计 209 秒、审计下被 3600 秒统一超时截断），统一 3600 秒会让它们必超时。超时随单元规格（spec）进入记录与输入比对，
所以必须在生成正式来源之前定好、来源与后继之间不得再改；这里的测试把这条纪律和配置校验固定下来。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.ci import unit_executor as ue

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tools" / "official_client_capture" / "tests"


def _write_config(root: Path, **overrides: object) -> Path:
    payload = {
        "schema_version": ue.CONFIG_SCHEMA,
        "default_parallelism": 2,
        "default_quota": {"cores": 1, "memory_mb": 128},
        "quotas": {},
        "splits": {},
        "exclusive": [],
        "unit_timeout_seconds": 120,
        "orphan_grace_seconds": 1,
        **overrides,
    }
    path = root / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class UnitTimeoutConfigTests(unittest.TestCase):
    def test_valid_table_is_loaded_and_empty_table_keeps_the_old_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = ue.load_config(_write_config(root, unit_timeouts={"test_a": 500, "test_b#2": 14400}, unit_timeouts_basis="依据：测试"))
            self.assertEqual(config.unit_timeouts, {"test_a": 500.0, "test_b#2": 14400.0})
            plain = ue.load_config(_write_config(root))
            self.assertEqual(plain.unit_timeouts, {}, "不登记单元超时时行为与改动前相同")

    def test_invalid_tables_are_refused_with_a_reason(self) -> None:
        cases = {
            "缺依据": {"unit_timeouts": {"test_a": 500}},
            "依据为空白": {"unit_timeouts": {"test_a": 500}, "unit_timeouts_basis": "   "},
            "不是对象": {"unit_timeouts": [["test_a", 500]], "unit_timeouts_basis": "x"},
            "单元 ID 为空": {"unit_timeouts": {"": 500}, "unit_timeouts_basis": "x"},
            "值是布尔": {"unit_timeouts": {"test_a": True}, "unit_timeouts_basis": "x"},
            "值不是数字": {"unit_timeouts": {"test_a": "500"}, "unit_timeouts_basis": "x"},
            "值等于默认（只放宽不缩短）": {"unit_timeouts": {"test_a": 120}, "unit_timeouts_basis": "x"},
            "值小于默认": {"unit_timeouts": {"test_a": 60}, "unit_timeouts_basis": "x"},
            "值超过上限": {"unit_timeouts": {"test_a": 14401}, "unit_timeouts_basis": "x"},
            "值是 NaN": {"unit_timeouts": {"test_a": float("nan")}, "unit_timeouts_basis": "x"},
            "值是无穷": {"unit_timeouts": {"test_a": float("inf")}, "unit_timeouts_basis": "x"},
        }
        for label, overrides in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as raw:
                with self.assertRaises(ue.ExecutorError):
                    ue.load_config(_write_config(Path(raw), **overrides))


class UnitTimeoutPlanningTests(unittest.TestCase):
    def _plan(self, timeouts: dict[str, float], grouped: dict[str, list[str]], *, full_set: bool = True) -> list[ue.Unit]:
        with tempfile.TemporaryDirectory() as raw:
            overrides: dict[str, object] = {"splits": {"test_big": {"chunks": 2, "reason": "测试拆块"}}}
            if timeouts:
                overrides.update(unit_timeouts=timeouts, unit_timeouts_basis="依据：测试")
            config = ue.load_config(_write_config(Path(raw), **overrides))
        return ue.plan_units(grouped, config, {}, {}, machine_cores=2, full_set=full_set)

    GROUPED = {
        "test_big": ["test_big.A.test_1", "test_big.A.test_2", "test_big.A.test_3"],
        "test_small": ["test_small.A.test_1"],
    }

    def test_registered_units_get_their_own_timeout_and_the_rest_keep_the_default(self) -> None:
        units = {unit.unit_id: unit for unit in self._plan({"test_big#2": 900, "test_small": 500}, self.GROUPED)}
        self.assertEqual(sorted(units), ["test_big#1", "test_big#2", "test_small"])
        self.assertEqual(units["test_big#2"].timeout_seconds, 900.0)
        self.assertEqual(units["test_small"].timeout_seconds, 500.0)
        self.assertIsNone(units["test_big#1"].timeout_seconds, "没有登记的单元沿用全局默认超时")

    def test_without_a_table_the_plan_is_exactly_what_it_was_before(self) -> None:
        self.assertTrue(all(unit.timeout_seconds is None for unit in self._plan({}, self.GROUPED)))

    def test_a_registered_unit_that_no_longer_exists_fails_a_full_plan_but_not_a_partial_one(self) -> None:
        with self.assertRaises(ue.ExecutorError) as raised:
            self._plan({"test_renamed#1": 900}, self.GROUPED)
        self.assertIn("test_renamed#1", str(raised.exception))
        # 只跑一部分模块时，本次集合之外的登记项不参与规划，也不报错（与拆块、独占名单同一纪律）。
        partial = self._plan({"test_renamed#1": 900}, {"test_small": ["test_small.A.test_1"]}, full_set=False)
        self.assertEqual([unit.unit_id for unit in partial], ["test_small"])

    def test_the_timeout_enters_the_unit_spec_so_changing_it_invalidates_old_records(self) -> None:
        before = {unit.unit_id: unit for unit in self._plan({"test_small": 500}, self.GROUPED)}["test_small"]
        after = {unit.unit_id: unit for unit in self._plan({"test_small": 600}, self.GROUPED)}["test_small"]
        plain = {unit.unit_id: unit for unit in self._plan({}, self.GROUPED)}["test_small"]
        spec = lambda unit: ue.unit_spec(unit, start=Path("tests"), pattern="test_*.py", timeout_seconds=120.0)
        self.assertEqual(spec(before)["timeout_seconds"], 500.0)
        self.assertEqual(spec(plain)["timeout_seconds"], 120.0)
        self.assertNotEqual(spec(before), spec(after), "超时进了规格：来源与后继之间改超时，旧记录就对不上、无法承接")


class RepositoryUnitTimeoutTableTests(unittest.TestCase):
    """仓库里真实的 unit_executor.json：表本身要自洽，依据要写明，键要能对上真实存在的测试模块。"""

    CONFIG = ue.load_config(REPO_ROOT / ue.DEFAULT_CONFIG)

    def test_the_repository_table_is_registered_with_a_basis_and_within_range(self) -> None:
        table = self.CONFIG.unit_timeouts
        self.assertGreaterEqual(len(table), 10, "审计放大后必超时的重单元都要登记")
        basis = self.CONFIG.raw.get("unit_timeouts_basis")
        self.assertTrue(isinstance(basis, str) and "10×" in basis and "2.5×" in basis, "依据要写明公式")
        for unit_id, seconds in table.items():
            self.assertGreater(seconds, self.CONFIG.unit_timeout_seconds, unit_id)
            self.assertLessEqual(seconds, ue.MAX_UNIT_TIMEOUT_SECONDS, unit_id)
            self.assertEqual(seconds % 100, 0, f"{unit_id} 的超时按 100 秒取整")

    def test_every_key_names_a_real_test_module_and_the_two_known_audit_timeouts_get_the_cap(self) -> None:
        for unit_id in self.CONFIG.unit_timeouts:
            match = re.fullmatch(r"(test_[A-Za-z0-9_]+)(?:#[1-9][0-9]*|!exclusive)?", unit_id)
            self.assertIsNotNone(match, f"单元 ID 形态不对：{unit_id}")
            self.assertTrue(list(TESTS_ROOT.rglob(match.group(1) + ".py")), f"{unit_id} 对不上任何测试模块")
        # 这两个单元在 10-14 的严格审计下被 3600 秒统一超时截断，真实耗时未知，所以直接取上限。
        for unit_id in ("test_claude_upgrade_compare_accept", "test_claude_upgrade_candidate_seal_evidence"):
            self.assertEqual(self.CONFIG.unit_timeouts[unit_id], ue.MAX_UNIT_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
