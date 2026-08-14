"""候选抓包入口的 Campaign 版本权威离线门禁。"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]


class CandidateVersionAuthorityTest(unittest.TestCase):
    def _run_with_version(
        self,
        script_name: str,
        version: str | None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "ENABLE_CANDIDATE_CORE_SYNTHETIC": "YES_I_ACCEPT_SYNTHETIC_ONLY",
            "ENABLE_CANDIDATE_AUX_SYNTHETIC": "YES_I_ACCEPT_SYNTHETIC_ONLY",
            "ACCOUNT_ID": "1",
            "RUN_ID": "candidate-version-unit-test",
        }
        if version is not None:
            environment["CODEX_VERSION"] = version
        return subprocess.run(
            ["bash", str(TOOL_ROOT / script_name)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_candidate_shell_entrypoints_reject_missing_or_invalid_version(self) -> None:
        scripts = (
            "run_sub2api_direct_matrix.sh",
            "run_sub2api_openai_mitm_matrix.sh",
            "run_candidate_core_capture.sh",
            "run_candidate_aux_capture.sh",
        )
        for script in scripts:
            with self.subTest(script=script, mutation="missing"):
                result = self._run_with_version(script, None)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CODEX_VERSION", result.stderr)
            with self.subTest(script=script, mutation="invalid"):
                result = self._run_with_version(script, "not-a-version")
                self.assertEqual(result.returncode, 2)
                self.assertIn("完整的 x.y.z 版本", result.stderr)

    def test_wire_builders_use_runtime_campaign_version(self) -> None:
        core = (TOOL_ROOT / "run_candidate_core_capture.sh").read_text(
            encoding="utf-8"
        )
        auxiliary = (TOOL_ROOT / "run_candidate_aux_capture.sh").read_text(
            encoding="utf-8"
        )
        for source in (core, auxiliary):
            self.assertIn('"Version: $codex_version"', source)
            self.assertIn("client_version=$codex_version", source)
            self.assertNotIn("client_version=0.145.0", source)
            self.assertNotIn("'Version: 0.145.0'", source)
        self.assertIn('--codex-version "$codex_version"', core)
        self.assertIn('--codex-version "$6"', auxiliary)
        self.assertIn('"$codex_version" \\\n', auxiliary)

    def test_direct_and_mitm_default_to_four_tls_scenarios(self) -> None:
        for script_name in (
            "run_sub2api_direct_matrix.sh",
            "run_sub2api_openai_mitm_matrix.sh",
        ):
            source = (TOOL_ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn('scenarios=${SCENARIOS:-"s1 s2 s3 s4"}', source)


if __name__ == "__main__":
    unittest.main()
