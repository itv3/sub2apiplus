"""开工检查两项（模型可用性、main 上 Security Scan）的“跳过”记录方式（优化第 14 项的验收修正必修 3）。

真实执行 ``entry-preflight.sh`` 里的检查框架（``check``）、跳过判定（``opening_skipped``）、两项检查函数与最后的汇总脚本——只去掉另外七项需要真实 ARM64 环境的检查调用——
用本机 http.server 当中转、假 ``gh`` 当 GitHub，验证行为而不只是文本：

* 显式跳过必须同时写 ``OPENING_CHECKS_SKIP_REASON``；跳过的项在 ``results.txt`` 与 ``summary.json`` 里记为 ``skipped``（带理由），**不是 passed**；
* 列了跳过却没写理由（空、全是空白、没设）：拒绝跳过，这一项按未通过处理，整个便宜检查退出 1；
* 没跳过、没配置：仍然未通过；配置齐全且检查通过：记为 passed，汇总里没有 skipped。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path

from tools.official_client_capture.tests import test_ci_opening_checks as base

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
SCRIPT = DRIVER / "entry-preflight.sh"
OTHER_CHECKS = "required-parameters|guard-pre-plan|deployment-identity|target-client|official-package|plan-audit|environment-probe"

FAKE_GH = """#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
if args[0] == "run":
    print(json.dumps([{"status": "completed", "conclusion": "success", "headSha": "a" * 40,
                       "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600)), "databaseId": 1, "url": "u"}]))
else:
    print("a" * 40)
"""


class PreflightSkipBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.out = self.root / "out"
        self.out.mkdir()
        # 只留 check 框架、跳过判定、两项检查和汇总，去掉另外七项检查的调用（它们要真实的 ARM64 环境）。
        text = SCRIPT.read_text(encoding="utf-8")
        body = text[text.index("check() {"):]
        body = re.sub(rf"(?m)^check ({OTHER_CHECKS}) .*\n", "", body)
        prelude = ("set -Euo pipefail; umask 077\nutc_now() { date -u +%FT%TZ; }\n"
                   f"OUT={self.out}\nFAILED=()\nDRV={DRIVER}\nMAIN_MODEL=gpt-main\nLITE_MODEL=gpt-lite\nROUND=r1\nSTAMP=s1\n")
        self.harness = self.root / "harness.sh"
        self.harness.write_text(prelude + body, encoding="utf-8")
        # 假 gh、假中转与密钥文件。
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH, encoding="utf-8")
        (self.bin / "gh").chmod(0o755)
        base.FakeRelay.models, base.FakeRelay.respond_status, base.FakeRelay.seen = ["gpt-main", "gpt-lite"], 200, []
        self.server = HTTPServer(("127.0.0.1", 0), base.FakeRelay)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.key_file = self.root / "key"
        self.key_file.write_text("sk-test-key\n", encoding="utf-8")
        self.key_file.chmod(0o600)

    def configured(self) -> dict[str, str]:
        return {"MODEL_PROBE_URL": f"http://127.0.0.1:{self.server.server_port}/v1", "MODEL_PROBE_KEY_FILE": str(self.key_file),
                "SECURITY_SCAN_REPO": "itv3/sub2apiplus"}

    def run_checks(self, **environment: str) -> tuple[int, dict, dict[str, str]]:
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith(("MODEL_PROBE", "SECURITY_SCAN", "OPENING_CHECKS"))}
        completed = subprocess.run(["bash", str(self.harness)], capture_output=True, text=True, timeout=120,
                                   env={**clean, "PATH": f"{self.bin}:{clean['PATH']}", "PYTHONDONTWRITEBYTECODE": "1", **environment})
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        results = dict(line.split() for line in (self.out / "results.txt").read_text(encoding="utf-8").splitlines() if line.strip())
        self.completed = completed
        return completed.returncode, summary, results

    def test_a_skip_with_a_reason_is_recorded_as_skipped_never_as_passed(self) -> None:
        code, summary, results = self.run_checks(OPENING_CHECKS_SKIP="model-availability,main-security-scan",
                                                 OPENING_CHECKS_SKIP_REASON="演练：中转与 GitHub 都不可达")
        self.assertEqual(code, 0, self.completed.stdout)
        self.assertEqual(results, {"model-availability": "skipped", "main-security-scan": "skipped"})
        self.assertEqual(summary["checks"], results)
        self.assertEqual(summary["failed"], [])
        self.assertEqual(summary["status"], "passed", "跳过不算失败")
        self.assertEqual(summary["skipped"], {"model-availability": "演练：中转与 GitHub 都不可达", "main-security-scan": "演练：中转与 GitHub 都不可达"})
        self.assertNotIn("passed", results.values())
        self.assertIn("被显式跳过", self.completed.stdout)

    def test_a_skip_without_a_reason_is_refused_and_counts_as_a_failure(self) -> None:
        for label, environment in {"没设理由": {}, "理由为空": {"OPENING_CHECKS_SKIP_REASON": ""},
                                   "理由全是空白": {"OPENING_CHECKS_SKIP_REASON": "  \t "}}.items():
            with self.subTest(label):
                code, summary, results = self.run_checks(OPENING_CHECKS_SKIP="model-availability", **environment)
                self.assertEqual(code, 1, self.completed.stdout)
                self.assertEqual(results["model-availability"], "failed")
                self.assertIn("model-availability", summary["failed"])
                self.assertEqual(summary["skipped"], {})
                self.assertIn("理由必填", (self.out / "model-availability.log").read_text(encoding="utf-8"))
                self.assertFalse((self.out / "model-availability.skipped").exists(), "没有理由就没有跳过凭据")

    def test_only_the_listed_item_is_skipped_the_other_still_has_to_pass(self) -> None:
        code, summary, results = self.run_checks(OPENING_CHECKS_SKIP="model-availability", OPENING_CHECKS_SKIP_REASON="中转维护中",
                                                 **{key: value for key, value in self.configured().items() if key.startswith("SECURITY")})
        self.assertEqual(code, 0, self.completed.stdout)
        self.assertEqual(results, {"model-availability": "skipped", "main-security-scan": "passed"})
        self.assertEqual(list(summary["skipped"]), ["model-availability"])
        # 没跳过的那一项没配置时仍然未通过。
        code, summary, results = self.run_checks(OPENING_CHECKS_SKIP="model-availability", OPENING_CHECKS_SKIP_REASON="中转维护中")
        self.assertEqual((code, results["main-security-scan"]), (1, "failed"))

    def test_unconfigured_and_not_skipped_still_fails(self) -> None:
        code, summary, results = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(results, {"model-availability": "failed", "main-security-scan": "failed"})
        self.assertEqual(sorted(summary["failed"]), ["main-security-scan", "model-availability"])

    def test_configured_and_passing_checks_are_recorded_as_passed_with_nothing_skipped(self) -> None:
        code, summary, results = self.run_checks(**self.configured())
        self.assertEqual(code, 0, self.completed.stdout)
        self.assertEqual(results, {"model-availability": "passed", "main-security-scan": "passed"})
        self.assertEqual(summary["skipped"], {})

    def test_a_reason_without_a_skip_list_skips_nothing(self) -> None:
        code, _summary, results = self.run_checks(OPENING_CHECKS_SKIP_REASON="只写理由不列项目")
        self.assertEqual((code, results["model-availability"]), (1, "failed"))

    def test_a_skipped_item_that_would_have_failed_is_not_run_at_all(self) -> None:
        """跳过就是不检查：中转里没有模型（本会未通过）时，跳过的项不会去拉列表。"""

        base.FakeRelay.models = []
        code, _summary, results = self.run_checks(OPENING_CHECKS_SKIP="model-availability", OPENING_CHECKS_SKIP_REASON="演练",
                                                  **self.configured())
        self.assertEqual((code, results["model-availability"]), (0, "skipped"))
        self.assertEqual(base.FakeRelay.seen, [])


class ParameterTemplateTests(unittest.TestCase):
    def test_the_example_parameter_file_documents_the_opening_check_variables(self) -> None:
        text = (DRIVER / "env.example.sh").read_text(encoding="utf-8")
        for name in ("MODEL_PROBE_URL", "MODEL_PROBE_KEY_FILE", "MODEL_PROBE_MINIMAL", "SECURITY_SCAN_REPO", "OPENING_CHECKS_SKIP", "OPENING_CHECKS_SKIP_REASON"):
            with self.subTest(name):
                self.assertIn(name, text)
        # 这几项是调用环境变量，不是参数文件键：模板里只能以注释出现，参数文件解析不应接受它们当赋值。
        self.assertFalse([line for line in text.splitlines() if re.match(r"(MODEL_PROBE|SECURITY_SCAN|OPENING_CHECKS)\w*=", line)])


if __name__ == "__main__":
    unittest.main()
