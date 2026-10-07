"""Claude 2.1.226 断言画像（升级控制面设计 B4-03 第二步）：仓库基线与官方证据验收。

常驻部分只读仓库文件：
* ``claude_rule_expectations_2_1_226.json`` 必须与「判据模板库 ＋ 2.1.226 RequiredRules ＋ 2.1.285 场景清单 ＋ catalogdata 里
  2.1.226 Release 的 Profile／Wire」重新生成的字节逐字相同，且命中 checker 登记的冻结摘要；
* 由它推导的验收契约命中契约模块登记的冻结摘要，40 条规则全部 dual_wire；
* 模板库恰好覆盖 106 条 profile 原子断言，不含 4 条 scenario-only 断言。

设置 ``CLAUDE_2_1_226_RELAY_FIXTURE`` 指向 complete-v21 录制目录时（同级的 thinking.display 补充取证一并纳入，见
``claude_2_1_226_evaluator_fixture``），另用真实 builder 子进程（正式 Campaign 布局、真实 checker 子进程）把同一份官方证据
同时当官方侧与候选侧评估两次：40 条规则全部通过、两次运行的逐规则 ``dependency_projection_sha256`` 逐字相同、results 绑定
冻结的契约摘要。录制目录只读。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_acceptance_contract as contract
from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_tool_identity_policy as policy_module
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import claude_2_1_226_evaluator_fixture as fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDER = REPO_ROOT / "tools" / "official_client_capture" / "claude_build_rule_assertion_results.py"
CANDIDATE = "candidate-2-1-226"
CAMPAIGN_ID = "claude-code-2_1_226-evaluator-acceptance"
AUTHORITY = {"assertion_profile_sha256": "a" * 64, "classification_package_digest": "b" * 64, "review_sha256": "c" * 64}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RepositoryBaselineTests(unittest.TestCase):
    def test_profile_regenerates_byte_for_byte_and_matches_frozen_digest(self) -> None:
        raw = fixture.PROFILE_PATH.read_bytes()
        self.assertEqual(raw, assertion.render_expectations(fixture.build_profile()))
        self.assertEqual(assertion.FROZEN_PROFILE_SHA256S.get(fixture.TARGET_VERSION), hashlib.sha256(raw).hexdigest())
        profile = assertion.load_profile(fixture.PROFILE_PATH, fixture.REQUIRED_RULES_PATH, expected_claude_version=fixture.TARGET_VERSION, verify_frozen_digest=True)
        self.assertEqual(len(profile["rules"]), 40)

    def test_contract_matches_frozen_digest(self) -> None:
        payload = contract.verify_frozen_contract(contract.load_profile(fixture.PROFILE_PATH), fixture.TARGET_VERSION)
        self.assertEqual(payload["rule_counts"], {"dual_wire": 40, "candidate_profile": 0})
        self.assertEqual(payload["side_restricted_checks"], {})

    def test_templates_cover_exactly_the_profile_assertions(self) -> None:
        required = json.loads(fixture.REQUIRED_RULES_PATH.read_text(encoding="utf-8"))
        templates = json.loads(fixture.TEMPLATES_PATH.read_text(encoding="utf-8"))
        profile_ids = {item for rule in required["required_rules"] for item in rule["atomic_assertion_ids"]}
        scenario_only = {item for group in required["scenario_only_groups"] for item in group["atomic_assertion_ids"]}
        self.assertEqual((len(profile_ids), len(scenario_only)), (106, 4))
        self.assertEqual(set(templates["templates"]), profile_ids)
        self.assertFalse(scenario_only & set(templates["templates"]))
        # 画像里每条原子断言都有机器 check，引用的 Release 值在生成时全部解析掉。
        profile = json.loads(fixture.PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual({check["atomic_assertion_id"] for rule in profile["rules"] for check in rule["checks"]}, profile_ids)
        self.assertNotIn(assertion.RELEASE_REF_KEY, fixture.PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(profile["generated_from"]["release"]["profile_sha256"], assertion._canonical_sha256(fixture.release_artifacts()[0]))

    def test_release_selector_resolves_unique_array_items(self) -> None:
        release_profile, _wire = fixture.release_artifacts()
        self.assertEqual(assertion._json_pointer(release_profile, "/headers/facts/[name=User-Agent]/value", "测试"), "claude-cli/2.1.226 (external, sdk-cli)")
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "命中 0 项"):
            assertion._json_pointer(release_profile, "/headers/facts/[name=X-Missing]/value", "测试")
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "命中 2 项"):
            assertion._json_pointer({"items": [{"k": "a"}, {"k": "a"}]}, "/items/[k=a]", "测试")


@unittest.skipUnless(os.environ.get(fixture.FIXTURE_ENV), f"未设置 {fixture.FIXTURE_ENV}（2.1.226 complete-v21 录制目录）")
class OfficialEvidenceAcceptanceTests(unittest.TestCase):
    """真实 builder ＋ 真实 checker 在 2.1.226 官方证据上评估两次（官方侧与候选侧都用同一份录制）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="claude-eval-2-1-226-")
        cls.root = Path(cls._temporary.name).resolve()
        cls.recording = fixture.fixture_root()
        cls.manifest_path = cls.root / "evaluation-manifest.json"
        cls.manifest = fixture.write_evaluation_manifest(cls.recording, cls.manifest_path)
        cls.digests = dict(policy_module.evaluator_dependency_digests())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def run_campaign(self, name: str) -> tuple[subprocess.CompletedProcess, Path]:
        campaign = self.root / name
        assertions_root = campaign / "assertions" / CANDIDATE
        (assertions_root / "machine").mkdir(parents=True)
        run_dir = self.root / f"{name}-run"
        run_dir.mkdir(mode=0o700)
        inner = {
            "schema_version": "codex-upgrade-campaign-run/v2",
            "campaign_id": CAMPAIGN_ID,
            "phase": "VC-5",
            "candidate_revision": 1,
            "candidate_id": CANDIDATE,
            "evaluation_baseline": None,
            "baseline_commit_sha256": None,
            "evaluator_digests": dict(self.digests),
            "actions": [],
        }
        (run_dir / "campaign-run-manifest.json").write_text(
            json.dumps({"schema_version": inner["schema_version"], "manifest_sha256": artifacts.digest(inner), "manifest": inner}, ensure_ascii=False), encoding="utf-8"
        )
        config = {
            "campaign_dir": str(campaign),
            "assertion_profile": str(fixture.PROFILE_PATH),
            "rule_manifest": str(fixture.REQUIRED_RULES_PATH),
            "expected_profile_sha256": _sha256(fixture.PROFILE_PATH),
            "official_evidence_root": str(fixture.evidence_root(self.recording)),
            "candidate_evidence_root": str(fixture.evidence_root(self.recording)),
            "official_capture_manifest": str(self.manifest_path),
            "candidate_capture_manifest": str(self.manifest_path),
            "official_evidence_prefix": "official-2.1.226",
            "candidate_evidence_prefix": "candidate-2.1.226",
            "target_version": fixture.TARGET_VERSION,
            "candidate_id": CANDIDATE,
            "profile_id": "claude-code-2.1.226-evaluator-acceptance",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": dict(AUTHORITY),
            "rules": [rule["spec_id"] for rule in json.loads(fixture.REQUIRED_RULES_PATH.read_text(encoding="utf-8"))["required_rules"]],
        }
        config_path = assertions_root / "builder-config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "CODEX_UPGRADE_CAMPAIGN_RUN_DIR": str(run_dir), "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": "9" * 64}
        command = [
            sys.executable, str(BUILDER),
            "--config", str(config_path),
            "--output", str(assertions_root / "results.json"),
            "--results-dir", str(assertions_root / "machine"),
            "--evaluation-baseline", "0",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL)
        return completed, assertions_root

    def test_all_rules_pass_and_dependency_digests_are_stable(self) -> None:
        # complete-v21 的 77 个 probe 加 thinking.display=omitted 补充 1 个：标签 artifact 与补绑的伴随文件共 898 个。
        self.assertEqual(len(self.manifest["artifacts"]), 898)
        first, first_root = self.run_campaign("campaign-a")
        self.assertEqual(first.returncode, 0, first.stderr[-2000:])
        second, second_root = self.run_campaign("campaign-b")
        self.assertEqual(second.returncode, 0, second.stderr[-2000:])
        rows = []
        for root in (first_root, second_root):
            index = artifacts.validate_evaluation_run(json.loads((root / "evaluation-run.json").read_text(encoding="utf-8")))
            self.assertEqual(len(index["rules"]), 40)
            self.assertTrue(all(row["status"] == "pass" and row["validation_mode"] == "dual_wire" for row in index["rules"]))
            self.assertEqual(index["evaluator"], self.digests)
            rows.append({row["rule"]: row["dependency_projection_sha256"] for row in index["rules"]})
        self.assertEqual(rows[0], rows[1])
        results = json.loads((first_root / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["schema_version"], contract.RESULTS_SCHEMA_V2)
        self.assertEqual(results["acceptance_contract_sha256"], contract.FROZEN_CONTRACT_SHA256S[fixture.TARGET_VERSION])
        self.assertEqual(len(results["rules"]), 40)


if __name__ == "__main__":
    unittest.main()
