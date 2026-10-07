"""``tools/generate_claude_fw_g_profile.py`` 的确定性投影（Claude 升级控制面设计 B3-03）。

全部离线：不运行 git、不读本地忽略区。只验证这次改动的四处：

* 版本与时间不写死：Release 身份、迁移收据的平台与日期都由调用方给出；
* 输出里的制品路径一律是仓库相对路径，与传绝对路径还是相对路径无关；路径在仓库外即拒绝；
* 输出只写一次：同内容复用、异内容拒绝；
* 迁移收据的源码 transition 不读工作树：策略登记的源码摘要取自冻结提交读取器，本次生成的 Profile、Wire 取内存摘要。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import generate_claude_fw_g_profile as generator

POLICY = Path(generator.ROOT) / "tools" / "official_client_capture" / "claude_fw_g_generation_policy_2_1_226_v2.json"
PROFILE = Path(generator.ROOT) / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "profiles" / "2.1.285" / ("1" * 64 + ".json")
WIRE = Path(generator.ROOT) / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "wire" / "2.1.285" / ("2" * 64 + ".json")


class DeterministicProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        generator.configure_generation_policy(POLICY)

    def test_write_once_reuses_identical_and_refuses_different(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "out.json"
            self.assertEqual(generator.write_once(path, b"{}\n"), "generated")
            self.assertEqual(generator.write_once(path, b"{}\n"), "reused")
            with self.assertRaisesRegex(SystemExit, "禁止覆盖"):
                generator.write_once(path, b"[]\n")
            link = Path(directory) / "link.json"
            os.symlink(path, link)
            with self.assertRaisesRegex(SystemExit, "符号链接"):
                generator.write_once(link, b"{}\n")

    def test_recorded_paths_are_repository_relative(self) -> None:
        relative = Path("backend") / "x.json"
        with mock.patch.object(Path, "cwd", return_value=Path(generator.ROOT)):
            self.assertEqual(generator.repository_relative(relative), "backend/x.json")
        self.assertEqual(generator.repository_relative(Path(generator.ROOT) / "backend" / "x.json"), "backend/x.json")
        with self.assertRaisesRegex(SystemExit, "不在仓库内"):
            generator.repository_relative(Path("/") / "outside" / "x.json")

    def test_source_transitions_do_not_read_the_worktree(self) -> None:
        reads: list[str] = []

        def frozen(path: str) -> str:
            reads.append(path)
            return "f" * 64

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("读了工作树文件")):
            transitions = generator.build_model_capability_source_transitions(PROFILE, "1" * 64, WIRE, "2" * 64, frozen)
        by_path = {item["path"]: item for item in transitions}
        profile_key = PROFILE.relative_to(generator.ROOT).as_posix()
        wire_key = WIRE.relative_to(generator.ROOT).as_posix()
        self.assertEqual((by_path[profile_key]["from_sha256"], by_path[profile_key]["to_sha256"]), (generator.EMPTY_FILE_SHA256, "1" * 64))
        self.assertEqual(by_path[wire_key]["to_sha256"], "2" * 64)
        self.assertEqual(sorted(reads), sorted(path for path, _before, _reason in generator.MODEL_CAPABILITY_SOURCE_TRANSITIONS))
        self.assertEqual([item["path"] for item in transitions], sorted(by_path))

    def test_receipt_and_release_identity_take_platform_and_date_from_caller(self) -> None:
        catalog = {"evidence": {"campaign_sha256": "3" * 64}, "models": []}
        release, bundle = generator.build_model_capability_release_identity("1" * 64, "2" * 64, "4" * 64, "linux/arm64")
        self.assertNotEqual(release, generator.build_model_capability_release_identity("1" * 64, "2" * 64, "4" * 64, "linux/amd64")[0])
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("读了工作树文件")):
            receipt = generator.build_model_capability_receipt(
                catalog, PROFILE, "1" * 64, WIRE, "2" * 64, release, bundle,
                receipt_date="2026-10-07", platform="linux/arm64", source_digest=lambda path: "f" * 64,
            )
        self.assertEqual((receipt["date"], receipt["phase"], receipt["target"]["platform"]), ("2026-10-07", generator.RECEIPT_PHASE, "linux/arm64"))
        self.assertEqual(receipt["artifacts"]["after"]["profile"], PROFILE.relative_to(generator.ROOT).as_posix())
        self.assertEqual(receipt["artifacts"]["after"]["wire"], WIRE.relative_to(generator.ROOT).as_posix())
        with self.assertRaisesRegex(SystemExit, "YYYY-MM-DD"):
            generator.build_model_capability_receipt(
                catalog, PROFILE, "1" * 64, WIRE, "2" * 64, release, bundle,
                receipt_date="2026/10/07", platform="linux/arm64", source_digest=lambda path: "f" * 64,
            )

    def test_source_commit_must_be_a_full_commit_id(self) -> None:
        for value in ("HEAD", "a" * 39, "G" * 40):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SystemExit, "40 位"):
                    generator.git_blob_digest_reader(value)


if __name__ == "__main__":
    unittest.main()
