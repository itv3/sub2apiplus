"""验证离线来源接缝的进程边界，独立检查器不继承外层动作的重复安装。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture.tests import claude_vc5_chain_fixture as fixture


class SeamInjectionBoundaryTests(unittest.TestCase):
    def _invoke(self, program: str, action: str) -> list[str]:
        # 在真实新解释器中执行同一份启动代码；安装替身只记录调用，不替换规则校验器。
        script = '''
import json
import os
import sys
from tools.official_client_capture.tests import claude_vc5_chain_fixture as fixture
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
program, action = sys.argv[1:]
os.environ[supervisor.CAMPAIGN_RUN_ACTION_ID_ENV] = action
os.environ[fixture.SEAM_ACTIONS_ENV] = "compare,assert-rules,acceptance"
os.environ[fixture.SEAM_REPOSITORY_ENV] = str(fixture.REPOSITORY_ROOT)
os.environ[fixture.SEAM_CONFIG_ENV] = "frozen-fixture-config.json"
calls = []
fixture.install = calls.append
sys.argv = [program]
exec(compile(fixture.USERCUSTOMIZE, "usercustomize.py", "exec"), {"__name__": "usercustomize"})
print(json.dumps(calls))
'''
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for key in (fixture.SEAM_ACTIONS_ENV, fixture.SEAM_CONFIG_ENV, fixture.SEAM_REPOSITORY_ENV):
            environment.pop(key, None)
        result = subprocess.run([sys.executable, "-B", "-c", script, program, action],
                                cwd=fixture.REPOSITORY_ROOT, env=environment,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_exact_checker_does_not_install_parent_source_seam(self) -> None:
        checker = fixture.REPOSITORY_ROOT / assertion.CHECKER_RELATIVE_PATH
        self.assertEqual(self._invoke(str(checker), "assert-rules"), [])

    def test_relative_checker_uses_the_same_exact_boundary(self) -> None:
        self.assertEqual(self._invoke(assertion.CHECKER_RELATIVE_PATH, "assert-rules"), [])

    def test_outer_actions_keep_the_original_installation(self) -> None:
        for action in ("compare", "assert-rules", "acceptance"):
            with self.subTest(action=action):
                self.assertEqual(self._invoke("-m", action), ["frozen-fixture-config.json"])

    def test_another_directory_with_the_same_filename_is_not_excluded(self) -> None:
        other = fixture.REPOSITORY_ROOT / "tests" / Path(assertion.CHECKER_RELATIVE_PATH).name
        self.assertEqual(self._invoke(str(other), "assert-rules"), ["frozen-fixture-config.json"])

    def test_unselected_action_does_not_install_sources(self) -> None:
        self.assertEqual(self._invoke("-m", "vc3-prepare-draft"), [])


if __name__ == "__main__":
    unittest.main()
