"""Claude 断言结果 builder（升级控制面设计 B4-03）：真实 builder 子进程＋真实 checker 子进程，合成证据。

覆盖共享 VC-5 合同：父监督器绑定、评估器四项摘要互校（Claude 身份引擎）、逐规则逐侧 write-once checkpoint 链、投影输入、
有 fail 先落 evaluation-run.json 再非零退出、同基线零 checker 重入、崩溃续跑只做剩余、b<K> 引用式复用、全 pass 才写 v2
results，以及逐规则依赖摘要 ``dependency_projection_sha256`` 的确定性（同输入跨 Campaign 相等、只随受影响规则变化）。
另核对评估器已在受管树里「建立」：checker／builder 两项整文件摘要、适配器的 builder 文件名与身份策略一致。
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
from tools.official_client_capture import claude_upgrade_client_adapter as claude_adapter
from tools.official_client_capture import claude_upgrade_evolution as evolution
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_evaluator_fixture as fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDER = REPO_ROOT / "tools" / "official_client_capture" / "claude_build_rule_assertion_results.py"
CHECKER = REPO_ROOT / "tools" / "official_client_capture" / "claude_rule_assertion.py"
CANDIDATE = "candidate-eval"
RUN_SCHEMA = "codex-upgrade-campaign-run/v2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ClaudeBuilderFixture:
    """两侧合成证据＋正式 Campaign 布局＋父监督器 run 目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.campaign_dir = root / "campaign"
        (self.campaign_dir / "assertions" / CANDIDATE).mkdir(parents=True)
        self.profile_path = self.campaign_dir / "control" / "assertion-profile.json"
        self.profile_sha256 = fixture.write_profile(self.profile_path)
        self.rule_manifest_path = fixture.write_rule_manifest(self.campaign_dir / "control" / "target-rules.json")
        self.official = fixture.write_side(root / "official", capture_id="eval-official")
        self.candidate = fixture.write_side(root / "candidate", capture_id="eval-candidate", anthropic_version="2023-01-01")
        # 批次冻结值必须等于 builder 运行时用 Claude 身份引擎重算的当前受管树四项。
        self.digests = dict(policy_module.evaluator_dependency_digests())
        self.config_overrides: dict = {}

    def fix_candidate(self, name: str = "candidate-fixed", **variant) -> None:
        """换成正确的候选证据（同一 capture_id）：只有 transport.http 场景的证据字节变化。"""

        self.candidate = fixture.write_side(self.root / name, capture_id="eval-candidate", **variant)

    def run_dir(self, name: str, *, baseline: int, digests: dict | None = None) -> Path:
        run_dir = self.root / name
        run_dir.mkdir(mode=0o700)
        inner = {
            "schema_version": RUN_SCHEMA,
            "campaign_id": fixture.CAMPAIGN_ID,
            "phase": "VC-5",
            "candidate_revision": 1,
            "candidate_id": CANDIDATE,
            "evaluation_baseline": baseline or None,
            "baseline_commit_sha256": ("b" * 64) if baseline else None,
            "evaluator_digests": dict(digests or self.digests),
            "actions": [],
        }
        record = {"schema_version": RUN_SCHEMA, "manifest_sha256": artifacts.digest(inner), "manifest": inner}
        (run_dir / "campaign-run-manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return run_dir

    def assertions_root(self, baseline: int) -> Path:
        root = self.campaign_dir / "assertions" / CANDIDATE
        return root / "revisions" / f"b{baseline}" if baseline else root

    def config(self, baseline: int, *, rules: list[str] | None = None, formal: bool = True) -> Path:
        results_dir = self.assertions_root(baseline) / "machine"
        results_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "assertion_profile": str(self.profile_path),
            "rule_manifest": str(self.rule_manifest_path),
            "expected_profile_sha256": self.profile_sha256,
            "official_evidence_root": str(self.official.evidence_root),
            "candidate_evidence_root": str(self.candidate.evidence_root),
            "official_capture_manifest": str(self.official.manifest_path),
            "candidate_capture_manifest": str(self.candidate.manifest_path),
            "official_evidence_prefix": "official-run",
            "candidate_evidence_prefix": "candidate-run",
            "target_version": fixture.TARGET_VERSION,
            "candidate_id": CANDIDATE,
            "profile_id": "claude-eval-v1",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": dict(fixture.AUTHORITY),
            "rules": list(rules or fixture.RULES),
            **self.config_overrides,
        }
        if formal:
            config["campaign_dir"] = str(self.campaign_dir)
        path = self.assertions_root(baseline) / "builder-config.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return path

    def run_builder(
        self,
        run_dir: Path | None,
        *,
        baseline: int,
        reuse_from: Path | None = None,
        authority: str = "none",
        env_override: dict | None = None,
        rules: list[str] | None = None,
        formal: bool = True,
        results_dir: Path | None = None,
    ) -> subprocess.CompletedProcess:
        command = [
            sys.executable, str(BUILDER),
            "--config", str(self.config(baseline, rules=rules, formal=formal)),
            "--output", str(self.assertions_root(baseline) / "results.json"),
            "--results-dir", str(results_dir or self.assertions_root(baseline) / "machine"),
            "--evaluation-baseline", str(baseline),
            "--reuse-authority", authority,
        ]
        if reuse_from is not None:
            command.extend(["--reuse-from", str(reuse_from)])
        env = dict(os.environ)
        env.pop("CODEX_UPGRADE_CAMPAIGN_RUN_DIR", None)
        env.pop("CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE", None)
        if run_dir is not None:
            env["CODEX_UPGRADE_CAMPAIGN_RUN_DIR"] = str(run_dir)
            env["CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE"] = fixture.OWNER_NONCE
        env.update(env_override or {})
        return subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL)

    def index(self, baseline: int) -> dict:
        return artifacts.validate_evaluation_run(json.loads((self.assertions_root(baseline) / "evaluation-run.json").read_text(encoding="utf-8")))

    def checkpoints(self, baseline: int) -> list[dict]:
        directory = self.assertions_root(baseline) / "checkpoints"
        return [
            artifacts.validate_evaluation_checkpoint(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(p for p in directory.iterdir() if p.suffix == ".json" and not p.name.endswith("-input.json"))
        ]


class ClaudeBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="claude-eval-builder-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.fixture = ClaudeBuilderFixture(self.root)

    def test_b0_writes_checkpoints_and_index_before_nonzero_exit(self) -> None:
        run_dir = self.fixture.run_dir("run-b0", baseline=0)
        bare = self.fixture.run_builder(None, baseline=0)
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("父监督器派发", bare.stderr)
        completed = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        index = self.fixture.index(0)
        self.assertEqual({row["rule"]: row["status"] for row in index["rules"]}, {"SPEC-FAIL-001": "pass", "SPEC-HDR-001": "fail", "SPEC-META-001": "pass", "SPEC-TLS-001": "pass"})
        self.assertEqual({row["rule"]: row["validation_mode"] for row in index["rules"]}, contract.derive_validation_modes(fixture.build_profile()))
        self.assertIsNone(next(row for row in index["rules"] if row["rule"] == "SPEC-META-001")["official_checkpoint"])
        self.assertFalse((self.fixture.assertions_root(0) / "results.json").exists())
        chain = self.fixture.checkpoints(0)
        # 三条 dual_wire 两侧各一条，candidate_profile 只有候选侧：共 7 条，链摘要闭合。
        self.assertEqual(len(chain), 7)
        previous = None
        for checkpoint in chain:
            self.assertEqual(checkpoint["previous_checkpoint_sha256"], previous)
            self.assertEqual(checkpoint["executed_by"], {"builder_sha256": self.fixture.digests["builder_sha256"], "run_dir": str(run_dir), "owner_nonce": fixture.OWNER_NONCE, "run_manifest_sha256": artifacts.digest(json.loads((run_dir / "campaign-run-manifest.json").read_text(encoding="utf-8"))["manifest"])})
            self.assertEqual(checkpoint["checker_sha256"], _sha256(CHECKER))
            self.assertIsNone(checkpoint["reused_from"])
            projection = self.fixture.campaign_dir / checkpoint["input_projection"]["path"]
            self.assertEqual(_sha256(projection), checkpoint["projection_sha256"])
            document = json.loads((self.fixture.campaign_dir / checkpoint["document"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual((document["projection_sha256"], document["checker_sha256"]), (checkpoint["projection_sha256"], checkpoint["checker_sha256"]))
            self.assertEqual(document["schema_version"], assertion.ASSERTION_SCHEMA_VERSION)
            previous = checkpoint["checkpoint_sha256"]
        self.assertEqual(index["checkpoint_head_sha256"], previous)
        self.assertEqual(index["evaluator"], self.fixture.digests)
        failing = [c for c in chain if c["status"] == "fail"]
        self.assertEqual([(c["rule"], c["side"]) for c in failing], [("SPEC-HDR-001", "candidate")])
        # 同基线幂等重入：既有 checkpoint／索引视为完成，零 checker（文档未改写），按既有 fail 退出。
        stamps = {p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("*.json")}
        reentry = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(reentry.returncode, 1)
        self.assertIn('"reentry": true', reentry.stdout)
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("*.json")}, stamps)
        self.assertEqual(len(self.fixture.checkpoints(0)), 7)

    def test_frozen_evaluator_drift_is_refused(self) -> None:
        forged = dict(self.fixture.digests, checker_sha256="e5" * 32)
        run_dir = self.fixture.run_dir("run-forged", baseline=0, digests=forged)
        rejected = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("builder 拒绝执行", rejected.stderr)
        self.assertFalse((self.fixture.assertions_root(0) / "checkpoints").exists())
        # 父监督器清单绑定的候选或基线与 builder 不一致也失败关闭。
        mismatched = self.fixture.run_dir("run-b3", baseline=3)
        refused = self.fixture.run_builder(mismatched, baseline=0)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("评估基线", refused.stderr)

    def test_b1_reuses_unaffected_rules_and_full_pass_writes_results(self) -> None:
        run_b0 = self.fixture.run_dir("run-b0", baseline=0)
        self.assertEqual(self.fixture.run_builder(run_b0, baseline=0).returncode, 1)
        b0_index = self.fixture.assertions_root(0) / "evaluation-run.json"
        b0 = {(c["rule"], c["side"]): c for c in self.fixture.checkpoints(0)}
        self.fixture.fix_candidate()
        self.fixture.config_overrides = {"reuse_candidate_evidence_prefix": "candidate-run-b0"}
        # reuse_authority=none：拒写复用，全部重跑。
        run_b1 = self.fixture.run_dir("run-b1", baseline=1)
        none_run = self.fixture.run_builder(run_b1, baseline=1, reuse_from=b0_index, authority="none")
        self.assertEqual(none_run.returncode, 0, none_run.stderr)
        self.assertTrue(all(c["reused_from"] is None for c in self.fixture.checkpoints(1)))
        # anchored：投影与依赖未变的规则（failure.529、transport.native_tls 场景）引用 b0 checkpoint，transport.http 的两条重跑。
        run_b2 = self.fixture.run_dir("run-b2", baseline=2)
        anchored = self.fixture.run_builder(run_b2, baseline=2, reuse_from=b0_index, authority="anchored")
        self.assertEqual(anchored.returncode, 0, anchored.stderr)
        b2 = {(c["rule"], c["side"]): c for c in self.fixture.checkpoints(2)}
        self.assertEqual(len(b2), 7)
        for key in (("SPEC-FAIL-001", "candidate"), ("SPEC-FAIL-001", "official"), ("SPEC-TLS-001", "candidate"), ("SPEC-TLS-001", "official")):
            with self.subTest(reused=key):
                self.assertEqual(b2[key]["reused_from"], {"baseline": 0, "checkpoint_sha256": b0[key]["checkpoint_sha256"], "document_sha256": b0[key]["document"]["sha256"]})
                self.assertEqual(b2[key]["document"], b0[key]["document"])
        for key in (("SPEC-HDR-001", "candidate"), ("SPEC-HDR-001", "official"), ("SPEC-META-001", "candidate")):
            with self.subTest(rerun=key):
                self.assertIsNone(b2[key]["reused_from"])
        self.assertFalse((self.fixture.assertions_root(2) / "machine" / "candidate" / "SPEC-TLS-001.json").exists())
        index = self.fixture.index(2)
        rows = {row["rule"]: row for row in index["rules"]}
        self.assertEqual(rows["SPEC-TLS-001"]["reused_from"]["baseline"], 0)
        b0_rows = {row["rule"]: row for row in self.fixture.index(0)["rules"]}
        self.assertEqual(rows["SPEC-TLS-001"]["dependency_projection_sha256"], b0_rows["SPEC-TLS-001"]["dependency_projection_sha256"])
        self.assertNotEqual(rows["SPEC-HDR-001"]["dependency_projection_sha256"], b0_rows["SPEC-HDR-001"]["dependency_projection_sha256"])
        # 全部通过：v2 results 绑定验收契约摘要；dual_wire 行两侧证据与机器结果，candidate_profile 行改记官方权威。
        results = json.loads((self.fixture.assertions_root(2) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["schema_version"], contract.RESULTS_SCHEMA_V2)
        profile = fixture.build_profile()
        self.assertEqual(results["acceptance_contract_sha256"], contract.contract_sha256(contract.build_contract_payload(profile)))
        self.assertEqual([row["rule"] for row in results["rules"]], fixture.RULES)
        by_rule = {row["rule"]: row for row in results["rules"]}
        meta = by_rule["SPEC-META-001"]
        self.assertEqual((meta["validation_mode"], meta["official_authority"]), ("candidate_profile", fixture.AUTHORITY))
        self.assertNotIn("official_evidence_refs", meta)
        hdr = by_rule["SPEC-HDR-001"]
        # 证据绑定覆盖全部 check 引用的文件（场景覆盖 check 引用规则场景下的全部 artifact，含补绑的伴随文件），去重按路径排序。
        self.assertEqual(
            hdr["candidate_evidence_refs"],
            [
                {"path": f"candidate-run/{relative}", "sha256": _sha256(self.fixture.candidate.evidence_root / relative)}
                for relative in (
                    "job-main/print/relay-manifest.json",
                    "job-main/print/relay/conn001.client_to_upstream.bin",
                    "job-main/print/relay/conn001.upstream_to_client.bin",
                    "job-main/print/relay/relay.json",
                )
            ],
        )
        self.assertTrue(hdr["candidate_machine_result"]["path"].startswith(f"assertions/{CANDIDATE}/revisions/b2/machine/candidate/"))
        # 复用行：候选证据前缀换成复用前缀，命令按历史 checkpoint 逐字重建。
        tls = by_rule["SPEC-TLS-001"]
        self.assertTrue(all(ref["path"].startswith("candidate-run-b0/") for ref in tls["candidate_evidence_refs"]))
        self.assertEqual(assertion.command_sha256(tls["candidate_command"]), b0[("SPEC-TLS-001", "candidate")]["command_sha256"])
        self.assertEqual(tls["candidate_machine_result"]["path"], b0[("SPEC-TLS-001", "candidate")]["document"]["path"])

    def test_dependency_projection_digest_is_deterministic(self) -> None:
        """同样的证据内容在两个独立 Campaign 里得到同样的逐规则依赖摘要；只改 TLS 证据只动 SPEC-TLS-001。"""

        self.fixture.fix_candidate()
        self.assertEqual(self.fixture.run_builder(self.fixture.run_dir("run-a", baseline=0), baseline=0).returncode, 0)
        first = {row["rule"]: row["dependency_projection_sha256"] for row in self.fixture.index(0)["rules"]}
        other_root = self.root / "second"
        other_root.mkdir()
        second = ClaudeBuilderFixture(other_root)
        second.fix_candidate()
        self.assertEqual(second.run_builder(second.run_dir("run-a", baseline=0), baseline=0).returncode, 0)
        self.assertEqual({row["rule"]: row["dependency_projection_sha256"] for row in second.index(0)["rules"]}, first)
        third_root = self.root / "third"
        third_root.mkdir()
        third = ClaudeBuilderFixture(third_root)
        third.fix_candidate(sni="api.anthropic.com")
        third.official = fixture.write_side(third_root / "official-tls", capture_id="eval-official", sni="api.anthropic.com.")
        completed = third.run_builder(third.run_dir("run-a", baseline=0), baseline=0)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        changed = {row["rule"]: row["dependency_projection_sha256"] for row in third.index(0)["rules"]}
        self.assertEqual(sorted(rule for rule in first if first[rule] != changed[rule]), ["SPEC-TLS-001"])

    def test_crash_resume_only_runs_remaining_rules(self) -> None:
        self.fixture.fix_candidate()
        run_dir = self.fixture.run_dir("run-b0", baseline=0)
        partial = self.fixture.run_builder(run_dir, baseline=0, rules=["SPEC-TLS-001"])
        self.assertEqual(partial.returncode, 0, partial.stderr)
        # 模拟索引落盘前崩溃：撤回索引与 results，保留 checkpoint。
        (self.fixture.assertions_root(0) / "evaluation-run.json").unlink()
        (self.fixture.assertions_root(0) / "results.json").unlink()
        documents = {p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("SPEC-TLS-001.json")}
        self.assertEqual(len(documents), 2)
        resumed = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("SPEC-TLS-001.json")}, documents)
        chain = self.fixture.checkpoints(0)
        self.assertEqual(len(chain), 7)
        self.assertEqual([c["rule"] for c in chain[:2]], ["SPEC-TLS-001", "SPEC-TLS-001"])
        self.assertTrue((self.fixture.assertions_root(0) / "results.json").is_file())

    def test_flat_layout_and_layout_errors(self) -> None:
        self.fixture.fix_candidate()
        flat_dir = self.root / "flat"
        flat = self.fixture.run_builder(None, baseline=0, formal=False, results_dir=flat_dir)
        self.assertEqual(flat.returncode, 0, flat.stderr)
        self.assertTrue((flat_dir / "SPEC-HDR-001.official.json").is_file())
        self.assertFalse((flat_dir / "SPEC-META-001.official.json").exists())
        results = json.loads((self.fixture.assertions_root(0) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(results["rules"]), 4)
        self.assertFalse((self.fixture.assertions_root(0) / "checkpoints").exists())
        refused = self.fixture.run_builder(None, baseline=1, formal=False, results_dir=flat_dir)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("平铺布局不支持", refused.stderr)
        misplaced = self.fixture.run_builder(self.fixture.run_dir("run-b0", baseline=0), baseline=0, results_dir=self.root / "elsewhere")
        self.assertNotEqual(misplaced.returncode, 0)
        self.assertIn("results-dir 必须等于", misplaced.stderr)


class EvaluatorEstablishedTests(unittest.TestCase):
    """B4-03 起 Claude 评估器在受管树里「已建立」：两项整文件摘要、适配器 builder 文件名与身份策略逐项一致。"""

    def test_evaluator_digests_bind_checker_and_builder_files(self) -> None:
        policy = policy_module.load_policy()
        self.assertEqual(policy["evaluator"]["checker"], "official_client_capture/claude_rule_assertion.py")
        self.assertEqual(policy["evaluator"]["builder"], "official_client_capture/claude_build_rule_assertion_results.py")
        self.assertEqual(claude_adapter.EVALUATION_BUILDER_BASENAME, Path(policy["evaluator"]["builder"]).name)
        digests = policy_module.evaluator_dependency_digests()
        self.assertEqual(set(digests), set(artifacts.EVALUATOR_DIGEST_FIELDS))
        self.assertEqual((digests["checker_sha256"], digests["builder_sha256"]), (_sha256(CHECKER), _sha256(BUILDER)))
        for name in ("claude_rule_assertion.py", "claude_build_rule_assertion_results.py", "claude_acceptance_contract.py", "claude_build_runtime_audit.py"):
            with self.subTest(module=name):
                self.assertEqual(policy_module.classify_path(policy, f"official_client_capture/{name}"), "evidence_semantics")

    def test_evolution_sees_established_evaluator(self) -> None:
        policy = policy_module.load_policy()
        digests = policy_module.managed_tree_digests(REPO_ROOT, policy)
        identity = {"entries": [{"path": key, "sha256": value} for key, value in sorted(digests.items())]}
        state = evolution.evaluator_state(identity, policy)
        self.assertEqual(state, {"status": evolution.EVALUATOR_ESTABLISHED, "checker_sha256": _sha256(CHECKER), "builder_sha256": _sha256(BUILDER)})
        without_checker = {"entries": [entry for entry in identity["entries"] if entry["path"] != policy["evaluator"]["checker"]]}
        with self.assertRaisesRegex(ConfigurationError, "只建立了一个"):
            evolution.evaluator_state(without_checker, policy)
        neither = {"entries": [entry for entry in without_checker["entries"] if entry["path"] != policy["evaluator"]["builder"]]}
        self.assertEqual(evolution.evaluator_state(neither, policy)["status"], evolution.EVALUATOR_NOT_ESTABLISHED)


if __name__ == "__main__":
    unittest.main()
