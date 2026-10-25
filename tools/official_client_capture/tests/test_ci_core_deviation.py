"""核数偏差报告（优化第 6 项）：执行器每轮结束写 ``core-deviation.json``，偏差超过 30% 的单元逐个列出；后台验证把它放进结论目录并在结论里登记。

* 报告算法：只看本次真正执行的单元，耗时不足 5 秒的不计，边界（29%）不算偏差，偏差按绝对值从大到小，整轮比值为实测核·秒／声明核·秒；
* 真实执行器：报告写在汇总旁边而不是汇总里；睡眠单元（实测远低于声明）被标出；
* 后台验证：报告复制到结论目录（与结论文件并排，不被当成结论），结论里登记路径、摘要、偏差单元数与总比值。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.ci import background_validation as bv
from tools.ci import unit_executor as ue
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase


class CoreDeviationTests(unittest.TestCase):
    """第 6 项：核数偏差报告。"""

    def outcome(self, unit_id: str, declared: float, seconds: float, cpu: float, disposition: str = "executed"):
        unit = ue.Unit(unit_id, unit_id, (), ue.Quota(declared, 128), False, 0.0, command=("true",))
        return ue.Outcome(unit=unit, kind="formal", exit_code=0, signal=None, timed_out=False, seconds=seconds, cpu_seconds=cpu, max_rss_mb=1.0, orphans=[],
                          log_path=Path("/x"), result=None, extra={"disposition": disposition})

    def test_units_beyond_thirty_percent_are_listed_and_the_rest_are_not(self) -> None:
        report = ue.core_deviation_report([
            self.outcome("exact", 1.0, 100, 100), self.outcome("edge", 1.0, 100, 129), self.outcome("over", 1.0, 100, 131),
            self.outcome("under", 2.0, 100, 100), self.outcome("tiny", 1.0, 1.0, 5.0), self.outcome("inherited", 1.0, 100, 500, "inherited"),
        ])
        self.assertEqual([row["unit_id"] for row in report["flagged"]], ["under", "over"], "按偏差绝对值从大到小；边界 29% 不算")
        self.assertEqual((report["units_measured"], report["units_too_short"], report["units_flagged"]), (4, 1, 2))
        under = report["flagged"][0]
        self.assertEqual((under["declared_cores"], under["measured_cores"], under["deviation"]), (2.0, 1.0, -0.5))
        self.assertIn("额度偏大", under["direction"])
        self.assertEqual(report["overall_ratio"], round((100 + 129 + 131 + 100) / (100 + 100 + 100 + 200), 3))
        self.assertEqual(report["threshold"], 0.3)

    def test_empty_run_has_no_ratio(self) -> None:
        report = ue.core_deviation_report([])
        self.assertEqual((report["units_measured"], report["overall_ratio"], report["flagged"]), (0, None, []))


class CoreDeviationRunTests(RetryRunBase):
    def test_the_report_is_written_next_to_the_summary_and_not_into_it(self) -> None:
        units = [self.command_unit("busy:a", ["-c", "import time\nend = time.time() + 6\nwhile time.time() < end: pass"])]
        completed, summary, manifest = self.run_gates(units, retry=None)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
        report = json.loads((self.root / "out" / "core-deviation.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], ue.CORE_DEVIATION_SCHEMA)
        self.assertEqual(report["units_measured"], 1)
        self.assertGreater(report["measured_core_seconds"], 0, "忙等 6 秒的单元有可观的 CPU 用量")
        self.assertNotIn("core_deviation", summary)
        self.assertIn("核数偏差：", completed.stderr)

    def test_an_under_declared_unit_is_flagged(self) -> None:
        units = [self.command_unit("idle:a", ["-c", "import time; time.sleep(6)"])]
        _completed, _summary, _manifest = self.run_gates(units, retry=None)
        report = json.loads((self.root / "out" / "core-deviation.json").read_text(encoding="utf-8"))
        self.assertEqual([row["unit_id"] for row in report["flagged"]], ["idle:a"], "睡 6 秒的单元实测远低于声明的 1 核")
        self.assertIn("额度偏大", report["flagged"][0]["direction"])


class CoreDeviationConclusionTests(unittest.TestCase):
    def test_background_validation_copies_the_report_into_the_conclusion_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "executor").mkdir()
            report = {"schema_version": ue.CORE_DEVIATION_SCHEMA, "threshold": 0.3, "units_measured": 5, "units_flagged": 2, "overall_ratio": 0.71, "flagged": []}
            (root / "executor" / "core-deviation.json").write_text(json.dumps(report), encoding="utf-8")
            (root / "entry-gates.json").write_text(json.dumps({"executor_summary": str(root / "executor" / "summary.json")}), encoding="utf-8")
            conclusions = root / "run" / "background-validation"
            conclusions.mkdir(parents=True)
            destination = conclusions / "abc-def.core-deviation.json"
            info = bv.core_deviation_info(root, destination)
            self.assertEqual((info["units_flagged"], info["overall_ratio"], info["report"]), (2, 0.71, str(destination)))
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), report)
            self.assertEqual(bv.results(root / "run"), [], "报告与结论并排放，但不会被当成结论")
            self.assertIsNone(bv.core_deviation_info(root / "没有", destination))


if __name__ == "__main__":
    unittest.main()
