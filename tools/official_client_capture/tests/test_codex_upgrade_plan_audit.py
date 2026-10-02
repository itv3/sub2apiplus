"""plan 只审计模式（E1-02）：建账本之前把清单类与输入类错误一次查全，不读账本、不写任何文件。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade as cu

TOOL_ROOT = Path(__file__).resolve().parents[1]

# 已收尾的历史升级对：清单在后续收尾中又演进过（指南第二章摘要、批准规则全集），现行审计下失败是已知的
# 历史漂移，不会再用它们建 Campaign，这里只登记原因。某一对改为通过、或换成别的失败原因，都必须同步本表。
HISTORICAL_PAIR_DRIFT = {
    ("0.145.0", "0.147.0"): "场景清单规格第二章摘要不一致",
    ("0.154.0", "0.157.0"): "target 场景清单的规则数量与当前升级规则全集不一致",
}


def _suffix(version: str) -> str:
    return version.replace(".", "_")


def _plan_argv(baseline: str, target: str, *, campaign_dir: Path, scenario_manifest: Path | None = None) -> list[str]:
    """plan --audit-only 的参数：清单取受管树里登记的版本；官方包、源码与证据坐标按需由调用方替换。"""

    return [
        "plan", "--audit-only", "--campaign-dir", str(campaign_dir),
        "--baseline-version", baseline, "--target-version", target,
        "--campaign-mode", "preflight_only", "--campaign-purpose", "production_replacement",
        "--baseline-source", str(campaign_dir.parent / "baseline-source"),
        "--target-source", str(campaign_dir.parent / "target-source"),
        "--baseline-evidence", str(campaign_dir.parent / "baseline-evidence"),
        "--target-sha256", "a" * 64, "--target-package", str(campaign_dir.parent / "package.tar.gz"),
        "--target-package-sha256", "b" * 64, "--target-code-mode-host-sha256", "c" * 64,
        "--runtime-image", "registry.example/capture@sha256:" + "d" * 64,
        "--rule-manifest", str(TOOL_ROOT / f"codex_upgrade_rules_{_suffix(baseline)}.json"),
        "--scenario-manifest", str(scenario_manifest or TOOL_ROOT / f"codex_upgrade_scenarios_{_suffix(baseline)}.json"),
        "--target-scenario-manifest", str(TOOL_ROOT / f"codex_upgrade_scenarios_{_suffix(target)}.json"),
        "--model", cu.track_models_for_version(target, "main")[0],
        "--lite-model", cu.track_models_for_version(target, "lite")[0],
    ]


def _manifest_arguments(baseline: str, target: str, *, scenario_manifest: Path | None = None) -> argparse.Namespace:
    arguments = cu._build_parser().parse_args(
        _plan_argv(baseline, target, campaign_dir=Path("/nonexistent/plan-audit/campaign"), scenario_manifest=scenario_manifest)
    )
    # 清单层审计只需要作业上下文坐标；正式路径由 _validate_arguments 设定，这里按同一规则补齐。
    arguments.output = arguments.campaign_dir
    arguments.campaign_id = "plan-audit-test"
    return arguments


class PlanAuditTests(unittest.TestCase):
    def test_every_registered_upgrade_pair_passes_manifest_audit(self) -> None:
        """CI 对每个登记升级对跑与入口同一套清单审计；历史漂移只认登记的原因。"""

        self.assertTrue(set(HISTORICAL_PAIR_DRIFT) <= set(cu.SUPPORTED_UPGRADE_PAIRS))
        for baseline, target in sorted(cu.SUPPORTED_UPGRADE_PAIRS):
            with self.subTest(pair=f"{baseline}→{target}"):
                arguments = _manifest_arguments(baseline, target)
                known = HISTORICAL_PAIR_DRIFT.get((baseline, target))
                if known is None:
                    result = cu._plan_manifest_audit(arguments)
                    self.assertGreater(result["job_count"], 0)
                    self.assertEqual(result["planned_job_count"], result["job_count"])
                else:
                    with self.assertRaisesRegex(cu.ConfigurationError, known):
                        cu._plan_manifest_audit(arguments)

    def test_covers_outside_rule_manifest_fail_manifest_audit(self) -> None:
        """0.159.2 轮 stage1 暴露的缺陷：场景 covers 引用规则清单外编号，清单层审计必须当场报出。"""

        source = TOOL_ROOT / "codex_upgrade_scenarios_0_157_0.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        # 当时 09-29 收尾删掉了 SPEC-EP-007 等规则，场景与作业的 covers 都还引用着它：两处一起加回，按原样复现。
        target_job = next(job for job in payload["capture_jobs"] if job.get("covers") and job.get("scenario_ids"))
        target_job["covers"] = [*target_job["covers"], "SPEC-EP-007"]
        scenario = next(item for item in payload["evidence_scenarios"] if item["scenario_id"] == target_job["scenario_ids"][0])
        scenario["covers"] = [*scenario["covers"], "SPEC-EP-007"]
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / source.name
            broken.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            arguments = _manifest_arguments("0.157.0", "0.160.0", scenario_manifest=broken)
            with self.assertRaisesRegex(cu.ConfigurationError, "任务引用规则清单外编号：\\['SPEC-EP-007'\\]"):
                cu._plan_manifest_audit(arguments)

    def test_audit_only_reports_every_check_and_writes_nothing(self) -> None:
        """只审计模式：参数校验失败不挡清单层，依赖失败项标为被阻塞，不建目录、不写任何文件。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = sorted(str(path) for path in root.rglob("*"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cu.main(_plan_argv("0.157.0", "0.160.0", campaign_dir=root / "campaign"))
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["schema_version"], cu.PLAN_AUDIT_SCHEMA)
            status = {item["name"]: item["status"] for item in result["checks"]}
            self.assertEqual(status["arguments"], "failed")
            self.assertEqual(status["manifests"], "passed")
            self.assertEqual(status["baseline-source"], "failed")
            self.assertEqual(status["source-diff"], "blocked")
            self.assertIn("timing-ledger-and-checkpoint", result["unchecked"])
            self.assertEqual(sorted(str(path) for path in root.rglob("*")), before)

    def test_plan_without_audit_still_requires_ledger_arguments(self) -> None:
        """正式建 Campaign 的 plan 仍在解析阶段要求账本与环境参数，报错方式与原先相同。"""

        argv = [item for item in _plan_argv("0.157.0", "0.160.0", campaign_dir=Path("/nonexistent/campaign")) if item != "--audit-only"]
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            cu._build_parser().parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        message = errors.getvalue()
        for flag in ("--timing-ledger-dir", "--timing-receipt", "--arm64-environment-root", "--arm64-environment-receipt"):
            self.assertIn(flag, message)


if __name__ == "__main__":
    unittest.main()
