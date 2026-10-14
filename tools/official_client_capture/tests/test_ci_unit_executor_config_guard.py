"""统一调度执行器配置校验的两处加固（步骤 5 WP3b 的 G3，独立验收发现；``tools/ci/unit_executor.py`` 的 ``load_config``）。

``unit_timeouts``（单元 ID → 秒）进单元规格、进记录与输入比对，登记错了必须响亮失败，不能无声失效：

* 键写了就必须是对象：``[]``、``0``、``false``、``null`` 过去会被 ``payload.get("unit_timeouts") or {}`` 悄悄当成空表接受，登记过的超时表
  就这样无声失效（只有缺省——没有这个键——才是空表）；
* 超大整数（如 ``10**400``）转不成 float，``math.isfinite`` 会抛 ``OverflowError`` 而不是 ``ExecutorError``：它们本来就超过上限，要按范围拒绝。

既有 ``test_ci_unit_executor_timeouts.py`` 零改动通过（本文件只补它没覆盖的两条）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.ci import unit_executor as ue

REPO_ROOT = Path(__file__).resolve().parents[3]


def write_config(root: Path, **overrides: Any) -> Path:
    payload = {"schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 2, "default_quota": {"cores": 1, "memory_mb": 128}, "quotas": {},
               "splits": {}, "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 1, **overrides}
    path = root / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class UnitTimeoutsContainerTests(unittest.TestCase):
    def load(self, **overrides: Any) -> ue.ExecutorConfig:
        with tempfile.TemporaryDirectory() as raw:
            return ue.load_config(write_config(Path(raw), **overrides))

    def test_a_present_key_must_be_an_object(self) -> None:
        values = {"空数组": [], "零": 0, "false": False, "null": None, "空字符串": "", "零点零": 0.0, "非空数组": [["test_a", 500]], "数组里一个数": [1],
                  "字符串": "test_a", "数": 5, "true": True}
        for label, value in values.items():
            for basis in ({}, {"unit_timeouts_basis": "依据：测试"}):
                with self.subTest(label=label, basis=bool(basis)):
                    with self.assertRaisesRegex(ue.ExecutorError, "unit_timeouts 必须是对象"):
                        self.load(unit_timeouts=value, **basis)

    def test_an_absent_key_and_an_empty_object_are_the_empty_table(self) -> None:
        self.assertEqual(self.load().unit_timeouts, {}, "缺省（没有这个键）：空表，与改动前相同")
        self.assertEqual(self.load(unit_timeouts={}).unit_timeouts, {}, "写了空对象：空表（不需要依据）")
        self.assertEqual(self.load(unit_timeouts={}, unit_timeouts_basis="依据：测试").unit_timeouts, {})

    def test_a_valid_table_still_loads(self) -> None:
        config = self.load(unit_timeouts={"test_a": 500, "test_b#2": 14400}, unit_timeouts_basis="依据：测试")
        self.assertEqual(config.unit_timeouts, {"test_a": 500.0, "test_b#2": 14400.0})

    def test_the_repository_configuration_still_loads(self) -> None:
        config = ue.load_config(REPO_ROOT / ue.DEFAULT_CONFIG)
        self.assertIsInstance(config.raw.get("unit_timeouts"), dict, "仓库自己的配置写了对象，加固后照常加载")
        self.assertTrue(config.unit_timeouts)


class UnitTimeoutsHugeIntegerTests(unittest.TestCase):
    def load(self, value: Any) -> ue.ExecutorConfig:
        with tempfile.TemporaryDirectory() as raw:
            return ue.load_config(write_config(Path(raw), unit_timeouts={"test_a": value}, unit_timeouts_basis="依据：测试"))

    def test_huge_integers_are_executor_errors_never_overflow_errors(self) -> None:
        huge = {"10**400": 10**400, "-10**400": -(10**400), "10**309": 10**309, "2**1024": 2**1024, "-2**1024": -(2**1024),
                "刚好超出 float 的整数": 2**1024 - 2**970 + 1, "能转 float 的大整数": 10**20, "上限加一": int(ue.MAX_UNIT_TIMEOUT_SECONDS) + 1}
        for label, value in huge.items():
            with self.subTest(label):
                with self.assertRaises(ue.ExecutorError) as raised:
                    self.load(value)
                self.assertNotIsInstance(raised.exception, OverflowError)
                self.assertIn("test_a", str(raised.exception), "报错要指明是哪个单元")
                self.assertIn(f"不超过 {ue.MAX_UNIT_TIMEOUT_SECONDS:.0f} 秒", str(raised.exception))

    def test_a_huge_default_timeout_does_not_turn_the_range_error_into_an_overflow(self) -> None:
        """默认超时本身也写成超大整数（病态配置）时，单元超时的超范围报错不能再因为格式化默认超时而抛 OverflowError。"""

        with tempfile.TemporaryDirectory() as raw:
            path = write_config(Path(raw), unit_timeout_seconds=10**400, unit_timeouts={"test_a": 10**400}, unit_timeouts_basis="依据：测试")
            with self.assertRaises(ue.ExecutorError) as raised:
                ue.load_config(path)
        self.assertNotIsInstance(raised.exception, OverflowError)
        self.assertIn("test_a", str(raised.exception))

    def test_the_boundaries_are_unchanged(self) -> None:
        self.assertEqual(self.load(int(ue.MAX_UNIT_TIMEOUT_SECONDS)).unit_timeouts, {"test_a": ue.MAX_UNIT_TIMEOUT_SECONDS}, "上限本身可以")
        self.assertEqual(self.load(121).unit_timeouts, {"test_a": 121.0}, "比默认大一秒可以")
        for label, value in {"等于默认": 120, "小于默认": 60, "零": 0, "负数": -5, "上限加一": 14401, "负的大整数": -(10**20)}.items():
            with self.subTest(label), self.assertRaises(ue.ExecutorError):
                self.load(value)

    def test_the_other_invalid_values_are_still_refused_with_their_old_reasons(self) -> None:
        for label, value in {"布尔": True, "字符串": "500", "NaN": float("nan"), "无穷": float("inf"), "负无穷": float("-inf"), "空": None}.items():
            with self.subTest(label), self.assertRaises(ue.ExecutorError):
                self.load(value)


if __name__ == "__main__":
    unittest.main()
