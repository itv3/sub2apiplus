"""Claude ``plan --audit-only``（Claude 升级控制面设计 B5a-09）：建计时账本之前的计划输入审计。

* 全部通过：参数、官方包（按真实冻结流程产出的 B2-01 冻结目录逐项复核）、规则／场景清单／证据标签、Job 展开与工具身份
  五项逐项通过，输出与 Codex 的审计同一组顶层字段，零写入（Campaign 目录不存在、冻结目录逐字不变）；
* 一项失败其余照查：参数、官方包、清单、工具身份各自失败时只有该项（及依赖它的 Job 展开）不通过，退出码 1；
* 编排器只做注册与转调：审计实现在 ``claude_upgrade_plan``，审计代码不进 Claude 的 wire／evidence 闭包；
  ``--campaign-mode formal`` 也只审计、不建 Campaign（plan 本身对 formal 照旧报 B2-11 待接入）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture.tests import test_claude_upgrade_freeze as freeze_tests
from tools.official_client_control import receipts as control_receipts

TARGET = "2.1.285"
BASELINE = "2.1.226"
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
CHECKS = ["arguments", "official-package", "manifests", "jobs", "tool-identity"]
# 审计新增的符号：都不得进入 Claude 编排器的 wire／evidence 闭包（改审计代码不改变两层身份）。
AUDIT_SYMBOLS = {"plan_audit", "default_manifest_paths", "PLAN_AUDIT_SCHEMA", "PLAN_AUDIT_UNCHECKED", "TOOL_IDENTITY_FIELD_PAIRS"}


def _run(argv: list[str]) -> tuple[int, dict[str, Any] | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


def _statuses(result: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["status"] for item in result["checks"]}


class PlanAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = freeze_tests._Signer(Path(cls._keys.name))
        cls._frozen = tempfile.TemporaryDirectory()
        cls.binary = freeze_tests._elf(freeze_tests.GLIBC, payload=b"claude-2.1.285")
        cls.source = freeze_tests.freeze_fixture(
            Path(cls._frozen.name).resolve() / f"claude-code-{TARGET}", signer=cls.signer, binary=cls.binary, version=TARGET
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._frozen.cleanup()
        cls._keys.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.official = self.base / "official" / f"claude-code-{TARGET}"
        shutil.copytree(self.source.parent, self.official, symlinks=True)
        self.freeze = self.official / "freeze.json"
        self.campaign_dir = self.base / "staging" / "entry-preflight-x" / "campaign"

    def _argv(self, *extra: str, **overrides: str) -> list[str]:
        values = {
            "--campaign-mode": "preflight_only",
            "--campaign-purpose": "production_replacement",
            "--baseline-version": BASELINE,
            "--target-version": TARGET,
            "--target-sha256": hashlib.sha256(self.binary).hexdigest(),
            "--target-package": str(self.freeze),
            "--target-package-sha256": hashlib.sha256(self.freeze.read_bytes()).hexdigest(),
            "--runtime-image": IMAGE,
            "--model": "claude-sonnet-5",
            "--upstream-ip": "160.79.104.10",
            "--campaign-id": "claude-code-2_1_285-plan-audit",
        }
        values.update({f"--{key.replace('_', '-')}": value for key, value in overrides.items()})
        argv = ["plan", "--audit-only", "--campaign-dir", str(self.campaign_dir)]
        for flag, value in values.items():
            if value is not None:
                argv += [flag, value]
        return [*argv, *extra]

    @staticmethod
    def _tree(root: Path) -> dict[str, str]:
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*")) if path.is_file()}

    def _fixed_identity(self) -> tuple[mock._patch, mock._patch]:
        """失败用例里工具身份不是被测对象：编排器与 Store 各取同一份固定身份，免得每次都重算两遍 v3 身份。"""

        identity = {"policy_version": 2, "policy_sha256": "1" * 64, "wire_producer_sha256": "2" * 64,
                    "evidence_semantics_sha256": "3" * 64, "control_sha256": "4" * 64, "files_sha256": "5" * 64}
        store = {**{key: value for key, value in identity.items() if key != "files_sha256"}, "tool_files_sha256": "5" * 64,
                 "client": "claude"}
        return (mock.patch.object(claude_upgrade, "_tool_identity", return_value=identity),
                mock.patch.object(control_receipts, "current_tool_identity", return_value=store))

    def test_passing_audit_checks_every_item_without_writes(self) -> None:
        before = self._tree(self.official)
        code, result, stderr = _run(self._argv())
        self.assertEqual(code, 0, stderr or result)
        assert result is not None
        self.assertEqual((result["schema_version"], result["status"]), (plan.PLAN_AUDIT_SCHEMA, "passed"))
        self.assertEqual([item["name"] for item in result["checks"]], CHECKS)
        self.assertEqual(set(_statuses(result).values()), {"passed"}, result["checks"])
        self.assertEqual(result["unchecked"], list(plan.PLAN_AUDIT_UNCHECKED))
        self.assertEqual((result["files_written"], result["live_request_count"], result["failed_checks"]), (0, 0, []))
        self.assertEqual((result["baseline_version"], result["target_version"], result["campaign_mode"]),
                         (BASELINE, TARGET, "preflight_only"))
        details = {item["name"]: item["detail"] for item in result["checks"]}
        self.assertEqual(details["official-package"]["binary_sha256"], hashlib.sha256(self.binary).hexdigest())
        self.assertEqual(details["official-package"]["verified_file_count"], 5)
        # 12 个主模型 Job 加 2 个非主模型的模型能力取证 Job（B2-07 补充项）。
        self.assertEqual(details["manifests"]["capture_job_count"], 14)
        self.assertEqual((details["jobs"]["job_count"], details["jobs"]["official_unmapped"]), (14, []))
        # 工具身份按值等于编排器总计划冻结的那一份（files_sha256 ↔ Store 的 tool_files_sha256）。
        identity = claude_upgrade._tool_identity(include_git=False)
        self.assertEqual(details["tool-identity"], {key: identity[key] for key, _ in plan.TOOL_IDENTITY_FIELD_PAIRS})
        self.assertEqual(details["tool-identity"]["files_sha256"], control_receipts.current_tool_identity("claude")["tool_files_sha256"])
        # 零写入：Campaign 目录及其上级都没有被创建，冻结目录逐字不变。
        self.assertFalse((self.base / "staging").exists())
        self.assertEqual(self._tree(self.official), before)

    def test_each_item_fails_independently(self) -> None:
        tampered_labels = self.base / "labels.json"
        labels = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))
        labels["claude_version"] = "2.1.284"
        tampered_labels.write_text(json.dumps(labels), encoding="utf-8")
        cases: dict[str, tuple[list[str], dict[str, str]]] = {
            # Campaign 变量 upstream_ip 必需却为空：参数项不过，依赖它的 Job 展开被阻塞，其余照查。
            "参数": (self._argv(upstream_ip=""), {"arguments": "failed", "jobs": "blocked"}),
            "官方包摘要": (self._argv(target_package_sha256="0" * 64), {"official-package": "failed"}),
            "目标二进制": (self._argv(target_sha256="0" * 64), {"official-package": "failed"}),
            "缺官方包": (self._argv(target_package=None, target_package_sha256=None), {"official-package": "failed"}),
            "证据标签": (self._argv("--evidence-labels", str(tampered_labels)), {"manifests": "failed", "jobs": "blocked"}),
        }
        identity, store = self._fixed_identity()
        with identity, store:
            for label, (argv, expected) in cases.items():
                with self.subTest(label=label):
                    code, result, stderr = _run(argv)
                    self.assertEqual(code, 1, stderr)
                    assert result is not None
                    statuses = _statuses(result)
                    self.assertEqual(list(statuses), CHECKS)
                    self.assertEqual({name: status for name, status in statuses.items() if status != "passed"}, expected,
                                     result["checks"])
                    self.assertEqual(result["failed_checks"], [name for name in CHECKS if name in expected])
                    self.assertFalse((self.base / "staging").exists())
        drift = {"policy_version": 2, "policy_sha256": "1" * 64, "wire_producer_sha256": "2" * 64,
                 "evidence_semantics_sha256": "3" * 64, "control_sha256": "4" * 64, "tool_files_sha256": "6" * 64}
        with self._fixed_identity()[0], mock.patch.object(control_receipts, "current_tool_identity", return_value=drift):
            code, result, _stderr = _run(self._argv())
        self.assertEqual(code, 1)
        assert result is not None
        self.assertEqual({name: status for name, status in _statuses(result).items() if status != "passed"},
                         {"tool-identity": "failed"})
        self.assertIn("files_sha256↔tool_files_sha256", next(item["error"] for item in result["checks"] if item["name"] == "tool-identity"))

    def test_formal_mode_is_only_audited(self) -> None:
        identity, store = self._fixed_identity()
        with identity, store:
            code, result, stderr = _run(self._argv(campaign_mode="formal"))
        self.assertEqual(code, 0, stderr)
        assert result is not None
        self.assertEqual((result["status"], result["campaign_mode"]), ("passed", "formal"))
        self.assertFalse((self.base / "staging").exists())
        argv = [item for item in self._argv(campaign_mode="formal") if item != "--audit-only"]
        code, result, stderr = _run(argv)
        self.assertEqual((code, result), (1, None))
        self.assertIn("B2-11", stderr)

    def test_audit_code_stays_outside_wire_and_evidence_closures(self) -> None:
        policy = claude_identity.load_policy()
        root = Path(claude_upgrade.__file__).resolve().parents[2]
        digests = claude_identity.managed_tree_digests(root, policy)
        index = claude_identity._module_index(policy, digests)
        module = plan.__name__
        for layer, names in (("wire_producer", "wire_roots"), ("evidence_semantics", "evidence_roots")):
            with self.subTest(layer=layer):
                roots = claude_identity._orchestrator_roots(policy, index, policy["orchestrator"][names])
                closure = claude_identity.closure(policy, root, digests, roots, layer=layer)
                reached = {item["name"] for item in closure["symbols"] if item["module"] == module}
                self.assertFalse(reached & AUDIT_SYMBOLS)
                self.assertNotIn("_plan_audit_command", {item["name"] for item in closure["symbols"] if item["module"] == claude_upgrade.__name__})


if __name__ == "__main__":
    unittest.main()
