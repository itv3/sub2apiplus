"""抓包 driver 与 Claude 采集路径的绑定，必须挡住「改数字让门禁过」。

driver（`capturelib/scenarios.py`）由 Claude 与 Codex 共用，Codex 侧改动会让整文件
摘要漂移却不影响 Claude 采集逻辑。绑定因此分两层：Claude 入口的可达闭包摘要每次实算，
是硬门禁；整文件摘要只是当前工具树声明，其漂移必须逐跳登记提交号与审阅结论，链条从
证据基线连续接到当前文件。

campaign 的产出侧工具树同理：common_scope 顶层那对摘要不等于全部 run 的统一绑定，
实际分组由各 run 的 manifest 复算，登记与实际不符即失败。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.claude_21220 import check_coverage as checker


DRIVER_SOURCE = '''\
"""合成 driver。"""

SHARED_PROMPT = "shared"
CODEX_RETRY_LIMIT = 3


def _shared_helper(value):
    return SHARED_PROMPT + str(value)


def _claude_only(value):
    return _shared_helper(value)


def _codex_only(value):
    return _shared_helper(value) * CODEX_RETRY_LIMIT


def run_claude_scenario(value):
    return _claude_only(value)


def run_codex_scenario(value):
    return _codex_only(value)
'''


def _closure(source: str = DRIVER_SOURCE, entry: str = "run_claude_scenario"):
    return checker.driver_entry_closure(source, entry)


class DriverEntryClosureTest(unittest.TestCase):
    def test_闭包只收可达符号(self) -> None:
        reachable, _ = _closure()
        self.assertEqual(
            reachable,
            {"run_claude_scenario", "_claude_only", "_shared_helper", "SHARED_PROMPT"},
        )

    def test_入口不存在时返回空集(self) -> None:
        reachable, _ = _closure(entry="run_missing_scenario")
        self.assertEqual(reachable, set())

    def test_改_Codex_独有符号不影响_Claude_闭包(self) -> None:
        _, before = _closure()
        mutated = DRIVER_SOURCE.replace(
            "return _shared_helper(value) * CODEX_RETRY_LIMIT",
            "return _shared_helper(value) * CODEX_RETRY_LIMIT + 1",
        )
        self.assertNotEqual(mutated, DRIVER_SOURCE)
        _, after = _closure(mutated)
        self.assertEqual(before, after)

    def test_改共享符号会改变_Claude_闭包(self) -> None:
        _, before = _closure()
        mutated = DRIVER_SOURCE.replace(
            "return SHARED_PROMPT + str(value)",
            "return SHARED_PROMPT + str(value) + '!'",
        )
        _, after = _closure(mutated)
        self.assertNotEqual(before, after)

    def test_闭包摘要与定义顺序无关(self) -> None:
        _, before = _closure()
        reordered = DRIVER_SOURCE.replace(
            "def _codex_only(value):\n    return _shared_helper(value) * CODEX_RETRY_LIMIT\n\n\n",
            "",
        )
        reordered += (
            "\n\ndef _codex_only(value):\n"
            "    return _shared_helper(value) * CODEX_RETRY_LIMIT\n"
        )
        _, after = _closure(reordered)
        self.assertEqual(before, after)


class DriverBindingValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.driver_path = Path(self._temporary.name) / "scenarios.py"
        self.driver_path.write_text(DRIVER_SOURCE, encoding="utf-8")
        self.file_sha256 = hashlib.sha256(DRIVER_SOURCE.encode()).hexdigest()
        _, self.closure_sha256 = _closure()
        self.addCleanup(self._temporary.cleanup)

    def _common_scope(self, **overrides) -> dict:
        scope = {
            "current_driver_claude_entry": "run_claude_scenario",
            "current_driver_claude_closure_sha256": self.closure_sha256,
            "current_driver_evidence_baseline_sha256": self.file_sha256,
            "current_driver_reviewed_drift": [],
            "current_driver_source_sha256": self.file_sha256,
        }
        scope.update(overrides)
        return scope

    def _validate(self, **overrides) -> list[str]:
        errors: list[str] = []
        checker.validate_driver_binding(
            self._common_scope(**overrides), self.driver_path, errors
        )
        return errors

    def test_基线一致时通过(self) -> None:
        self.assertEqual(self._validate(), [])

    def test_Claude_闭包变化直接失败(self) -> None:
        errors = self._validate(current_driver_claude_closure_sha256="0" * 64)
        self.assertTrue(any("闭包摘要变化" in error for error in errors))

    def test_入口缺失时失败(self) -> None:
        errors = self._validate(current_driver_claude_entry="run_missing_scenario")
        self.assertTrue(any("缺少 Claude 采集入口" in error for error in errors))

    def test_未声明入口时失败(self) -> None:
        errors = self._validate(current_driver_claude_entry="")
        self.assertTrue(any("未声明 driver 的 Claude 采集入口" in error for error in errors))

    def test_只改登记摘要而不登记审阅结论会失败(self) -> None:
        # 模拟「为让门禁通过直接改数字」：文件确实变了，台账也跟着改成新值，
        # 但没有留下任何审阅记录，漂移链仍停在基线。
        self.driver_path.write_text(DRIVER_SOURCE + "\n# drift\n", encoding="utf-8")
        drifted = hashlib.sha256(self.driver_path.read_bytes()).hexdigest()
        errors = self._validate(current_driver_source_sha256=drifted)
        self.assertTrue(any("未登记审阅结论" in error for error in errors))

    def test_登记完整的_Codex_漂移可以放行(self) -> None:
        mutated = DRIVER_SOURCE.replace(
            "return _shared_helper(value) * CODEX_RETRY_LIMIT",
            "return _shared_helper(value) * CODEX_RETRY_LIMIT + 1",
        )
        self.driver_path.write_text(mutated, encoding="utf-8")
        drifted = hashlib.sha256(self.driver_path.read_bytes()).hexdigest()
        errors = self._validate(
            current_driver_source_sha256=drifted,
            current_driver_reviewed_drift=[
                {
                    "commit": "a" * 40,
                    "from_sha256": self.file_sha256,
                    "to_sha256": drifted,
                    "claude_closure_sha256": self.closure_sha256,
                    "claude_closure_unchanged": True,
                    "conclusion": "仅 Codex 独有符号改动",
                }
            ],
        )
        self.assertEqual(errors, [])

    def test_漂移链断裂会失败(self) -> None:
        errors = self._validate(
            current_driver_reviewed_drift=[
                {
                    "commit": "a" * 40,
                    "from_sha256": "9" * 64,
                    "to_sha256": self.file_sha256,
                    "conclusion": "断链",
                }
            ],
        )
        self.assertTrue(any("不接续" in error for error in errors))

    def test_漂移条目缺字段会失败(self) -> None:
        errors = self._validate(
            current_driver_reviewed_drift=[
                {"commit": "a" * 40, "from_sha256": self.file_sha256}
            ],
        )
        self.assertTrue(any("缺少 to_sha256" in error for error in errors))

    def test_谎报_Claude_路径未变会失败(self) -> None:
        errors = self._validate(
            current_driver_reviewed_drift=[
                {
                    "commit": "a" * 40,
                    "from_sha256": self.file_sha256,
                    "to_sha256": self.file_sha256,
                    "claude_closure_sha256": "0" * 64,
                    "claude_closure_unchanged": True,
                    "conclusion": "谎报",
                }
            ],
        )
        self.assertTrue(any("声明 Claude 路径未变" in error for error in errors))

    def test_缺少证据基线摘要会失败(self) -> None:
        errors = self._validate(current_driver_evidence_baseline_sha256=None)
        self.assertTrue(any("未记录 driver 的证据基线摘要" in error for error in errors))


class CaptureToolchainConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.capture_root = Path(self._temporary.name) / "campaign"
        self.addCleanup(self._temporary.cleanup)

    def _write_runs(self, layout: dict[str, tuple[str, str]]) -> None:
        for run_id, (driver_sha256, addon_sha256) in layout.items():
            manifest = self.capture_root / "runs" / run_id / "manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "capture_tools": {
                                "execution_sources": {
                                    "files": [
                                        {
                                            "path": "capturelib/scenarios.py",
                                            "sha256": driver_sha256,
                                        },
                                        {
                                            "path": "addons/mitm_capture.py",
                                            "sha256": addon_sha256,
                                        },
                                    ]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

    def _validate(self, registered: dict) -> list[str]:
        errors: list[str] = []
        with mock.patch.object(checker, "PENDING_CAPTURE_ROOT", self.capture_root):
            checker.validate_capture_toolchain_consistency(
                {"capture_toolchain_consistency": registered}, errors
            )
        return errors

    def test_一致的工具树登记通过(self) -> None:
        self._write_runs({"r1": ("d" * 64, "a" * 64), "r2": ("d" * 64, "a" * 64)})
        errors = self._validate(
            {
                "consistent": True,
                "driver_variants": [{"sha256": "d" * 64, "run_count": 2}],
                "addon_variants": [{"sha256": "a" * 64, "run_count": 2}],
                "unreproducible_driver_runs": 0,
            }
        )
        self.assertEqual(errors, [])

    def test_把多版本工具树谎报成一致会失败(self) -> None:
        # 真实 campaign 的形态：采集途中换过 driver，登记却只留下最后那一版。
        self._write_runs({"r1": ("d" * 64, "a" * 64), "r2": ("e" * 64, "a" * 64)})
        errors = self._validate(
            {
                "consistent": True,
                "driver_variants": [{"sha256": "e" * 64, "run_count": 1}],
                "addon_variants": [{"sha256": "a" * 64, "run_count": 2}],
                "unreproducible_driver_runs": 0,
            }
        )
        self.assertTrue(any("driver_variants 与各 run manifest 不一致" in e for e in errors))
        self.assertTrue(any("consistent 应为 False" in e for e in errors))

    def test_runs_明细与实际不符会失败(self) -> None:
        self._write_runs({"r1": ("d" * 64, "a" * 64), "r2": ("d" * 64, "a" * 64)})
        errors = self._validate(
            {
                "consistent": True,
                "driver_variants": [
                    {"sha256": "d" * 64, "run_count": 2, "runs": ["r1", "r3"]}
                ],
                "addon_variants": [{"sha256": "a" * 64, "run_count": 2}],
                "unreproducible_driver_runs": 0,
            }
        )
        self.assertTrue(any("runs 明细与实际不符" in error for error in errors))

    def test_不可复算计数必须与登记一致(self) -> None:
        self._write_runs({"r1": ("d" * 64, "a" * 64), "r2": ("e" * 64, "a" * 64)})
        errors = self._validate(
            {
                "consistent": False,
                "driver_variants": [
                    {
                        "sha256": "d" * 64,
                        "run_count": 1,
                        "runs": ["r1"],
                        "source_recoverable": False,
                    },
                    {
                        "sha256": "e" * 64,
                        "run_count": 1,
                        "runs": ["r2"],
                        "source_recoverable": True,
                    },
                ],
                "addon_variants": [{"sha256": "a" * 64, "run_count": 2}],
                "unreproducible_driver_runs": 0,
            }
        )
        self.assertTrue(
            any("unreproducible_driver_runs 应为 1" in error for error in errors)
        )

    def test_缺少登记会失败(self) -> None:
        self._write_runs({"r1": ("d" * 64, "a" * 64)})
        errors: list[str] = []
        with mock.patch.object(checker, "PENDING_CAPTURE_ROOT", self.capture_root):
            checker.validate_capture_toolchain_consistency({}, errors)
        self.assertTrue(any("缺少 capture_toolchain_consistency" in e for e in errors))

    def test_run_未记录执行源会失败(self) -> None:
        manifest = self.capture_root / "runs" / "r1" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"runtime": {}}), encoding="utf-8")
        errors = self._validate(
            {
                "consistent": True,
                "driver_variants": [],
                "addon_variants": [],
                "unreproducible_driver_runs": 0,
            }
        )
        self.assertTrue(any("未记录 capture_tools 执行源" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
