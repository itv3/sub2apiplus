"""验证 Claude generation policy 的封闭结构、自摘要和交叉约束。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_generation_policy import (
    GenerationPolicyError,
    load_generation_policy,
    policy_identity,
)


ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = (
    ROOT
    / "tools/official_client_capture/claude_fw_g_generation_policy_2_1_226_v2.json"
)


class ClaudeGenerationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def _write(self, directory: str, document: dict, *, reseal: bool = True) -> Path:
        candidate = copy.deepcopy(document)
        if reseal:
            candidate["identity_sha256"] = policy_identity(candidate)
        path = Path(directory) / "generation-policy.json"
        path.write_text(
            json.dumps(candidate, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def test_current_policy_is_valid(self) -> None:
        loaded = load_generation_policy(POLICY_PATH)
        self.assertEqual(loaded["identity_sha256"], policy_identity(loaded))

    def test_rejects_empty_target_version_even_when_resealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = copy.deepcopy(self.document)
            candidate["target"]["version"] = ""
            path = self._write(directory, candidate)
            with self.assertRaisesRegex(GenerationPolicyError, "target.version"):
                load_generation_policy(path)

    def test_rejects_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = copy.deepcopy(self.document)
            candidate["target"]["version"] = "9.9.9"
            path = self._write(directory, candidate, reseal=False)
            with self.assertRaisesRegex(GenerationPolicyError, "identity_sha256 漂移"):
                load_generation_policy(path)

    def test_rejects_official_acceptance_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = copy.deepcopy(self.document)
            candidate["acceptance"]["official_probe_count"] += 1
            path = self._write(directory, candidate)
            with self.assertRaisesRegex(GenerationPolicyError, "计数不一致"):
                load_generation_policy(path)

    def test_rejects_unknown_top_level_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = copy.deepcopy(self.document)
            candidate["unapproved_extension"] = True
            path = self._write(directory, candidate)
            with self.assertRaisesRegex(GenerationPolicyError, "字段不闭合"):
                load_generation_policy(path)


if __name__ == "__main__":
    unittest.main()
