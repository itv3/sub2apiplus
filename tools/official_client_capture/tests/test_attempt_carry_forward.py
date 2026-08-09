"""承接上一轮已完成任务，前提是两轮之间环境没有漂移。

原本 `--rerun-failed` 收集完上一轮的成功任务后直接丢弃、整轮重采。实测单轮 17 项全绿
概率只有约三成，整轮重采让收敛极慢。承接是合理的——但必须证明「上一轮 after」到「本轮
before」之间环境没变，否则被承接的证据描述的是另一套环境。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.official_client_capture import codex_upgrade as cu


def _manifest(**digests) -> dict:
    base = {
        "service": "a" * 64,
        "containers": "b" * 64,
        "account": "c" * 64,
        "configuration": "d" * 64,
        "database": "e" * 64,
    }
    base.update(digests)
    return {
        "snapshots": [
            {"kind": kind, "sha256": value} for kind, value in base.items()
        ]
    }


class AttemptCarryForwardTest(unittest.TestCase):
    def _campaign(self, root: Path, after: dict) -> Path:
        attempt_dir = root / "official" / "attempts" / "A1" / "evidence" / "environment" / "after"
        attempt_dir.mkdir(parents=True)
        path = attempt_dir / "probe-manifest.json"
        path.write_text(json.dumps(after), encoding="utf-8")
        return root

    def test_环境一致时承接成立(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._campaign(Path(temporary), _manifest())
            receipt = cu._verify_environment_continuity(
                root, Path("official"), {"A1"}, _manifest()
            )
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["source_attempt_id"], "A1")
            self.assertEqual(
                receipt["compared_kinds"], list(cu.CONTINUITY_PROBE_KINDS)
            )

    def test_数据库增长不阻断承接(self) -> None:
        """usage_logs 等水位表必然随采集增长，由 restoration 的 before_subset 覆盖。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = self._campaign(Path(temporary), _manifest())
            receipt = cu._verify_environment_continuity(
                root, Path("official"), {"A1"}, _manifest(database="f" * 64)
            )
            self.assertIsNotNone(receipt)

    def test_环境漂移必须拒绝承接(self) -> None:
        """被承接的证据描述的是旧环境；环境变了就不能再算数。"""

        for kind in cu.CONTINUITY_PROBE_KINDS:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = self._campaign(Path(temporary), _manifest())
                    with self.assertRaises(cu.ConfigurationError) as caught:
                        cu._verify_environment_continuity(
                            root,
                            Path("official"),
                            {"A1"},
                            _manifest(**{kind: "9" * 64}),
                        )
                    self.assertIn(kind, str(caught.exception))

    def test_没有承接时不做校验(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(
                cu._verify_environment_continuity(
                    Path(temporary), Path("official"), set(), _manifest()
                )
            )

    def test_承接来源必须唯一(self) -> None:
        """混用多个 attempt 的结果才是真正的拼接，必须拒绝。"""

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(cu.ConfigurationError):
                cu._verify_environment_continuity(
                    Path(temporary), Path("official"), {"A1", "A2"}, _manifest()
                )

    def test_缺少来源探针时拒绝承接(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(cu.ConfigurationError):
                cu._verify_environment_continuity(
                    Path(temporary), Path("official"), {"A1"}, _manifest()
                )


if __name__ == "__main__":
    unittest.main()
