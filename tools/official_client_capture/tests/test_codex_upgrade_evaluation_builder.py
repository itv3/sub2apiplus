"""改造 5（评估失败局部恢复）M1：断言 builder 的 checkpoint／索引／复用合同（T5.6，真实 checker 子进程）。

在正式 Campaign 布局（config 声明 campaign_dir／candidate_id）与父监督器环境变量下运行真实
builder：逐规则逐侧 write-once checkpoint、投影输入、有 fail 先落 evaluation-run.json 再非零退出、
同基线幂等重入零 checker、崩溃续跑只做剩余、b<K> 复用命中／未命中、reuse_authority=none 拒写。
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

from unittest import mock

from tools.official_client_capture import assertion_gate as gate
from tools.official_client_capture import build_assertion_bundle as bundle
from tools.official_client_capture import candidate_rule_assertion as assertion
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy_module
from tools.official_client_capture import derive_official_observations as derive
from tools.official_client_capture.tests import managed_tree_copy
from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDER = REPO_ROOT / "tools" / "official_client_capture" / "build_rule_assertion_results.py"
CHECKER = REPO_ROOT / "tools" / "official_client_capture" / "candidate_rule_assertion.py"
CANDIDATE = "candidate-eval"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvaluationBuilderFixture:
    """两侧 bundle（真实派生）+ Campaign 布局 + 父 run 目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.campaign_dir = root / "campaign"
        (self.campaign_dir / "assertions" / CANDIDATE).mkdir(parents=True)
        (self.campaign_dir / "control").mkdir()
        self.profile_path = self.campaign_dir / "control" / "assertion-profile.json"
        self.profile_path.write_text(json.dumps(e2e.PROFILE, ensure_ascii=False), encoding="utf-8")
        self.rule_manifest_path = self.campaign_dir / "control" / "target-rules.json"
        self.rule_manifest_path.write_text(json.dumps(e2e.RULE_MANIFEST, ensure_ascii=False), encoding="utf-8")
        self.profile_sha256 = _file_sha256(self.profile_path)
        self.official = self._side("official", surface="codex")
        self.candidate = self._side("candidate", surface="other")
        # 批次冻结值必须等于 builder 运行时重算的当前受管树四项（builder 互校，三方 P1-1）；
        # 夹具取仓库树的真实值，checker 变化的用例改在副本树上运行。
        self.digests = dict(policy_module.evaluator_dependency_digests())

    def _side(self, side: str, *, surface: str) -> Path:
        candidate_side = side.startswith("candidate")
        side_dir = self.root / side
        source_root = side_dir / "run"
        (source_root / "relay").mkdir(parents=True)
        (source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(e2e.H1_STREAM)
        entries = [{"root": "run", "path": "relay/conn001.client_to_upstream.bin", "target": "run/relay/conn001.client_to_upstream.bin"}]
        if candidate_side:
            (source_root / "traces").mkdir()
            record = dict(e2e.INTERNAL_RECORD, data={"surface": surface})
            (source_root / "traces" / "surface.observation.jsonl").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            entries.append({"root": "run", "path": "traces/surface.observation.jsonl", "target": "run/traces/surface.observation.jsonl"})
        plan_path = side_dir / "bundle-plan.json"
        plan_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        bundle_dir = side_dir / gate.BUNDLE_DIR_NAME
        bundle.build_bundle({"run": source_root}, bundle.load_plan(plan_path), bundle_dir)
        derive_plan = side_dir / "derive-plan.json"
        derive_plan.write_text(
            json.dumps({"entries": [{"source": "run/relay/conn001.client_to_upstream.bin", "parser": "h1_request_stream", "scenario_id": "A03", "kind": "process_trace", "target": "derived/A03/conn001.observation.jsonl", "connection_id": "conn001"}]}),
            encoding="utf-8",
        )
        derive.derive_observations(bundle_dir, derive.load_derivation_plan(derive_plan))
        artifacts_list = [
            self._artifact(bundle_dir, "run/relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source"),
            self._artifact(bundle_dir, "derived/A03/conn001.observation.jsonl", "process_trace", "observation_jsonl"),
        ]
        if candidate_side:
            artifacts_list.append(self._artifact(bundle_dir, "run/traces/surface.observation.jsonl", "process_trace", "observation_jsonl"))
        manifest = {
            "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
            "codex_version": e2e.TARGET_VERSION,
            "capture_id": f"eval-{side}",
            "status": "complete",
            "artifacts": artifacts_list,
        }
        (bundle_dir / gate.MANIFEST_FILENAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return bundle_dir

    @staticmethod
    def _artifact(bundle_dir: Path, path: str, kind: str, parser: str) -> dict:
        return {"path": path, "sha256": _file_sha256(bundle_dir / path), "kind": kind, "parser": parser, "scenario_ids": ["A03"], "labels": {"transport": "http"}}

    def fix_candidate(self) -> None:
        """修正候选证据（surface=codex），并重写 bundle 与 manifest：投影随之变化。"""

        self.candidate = self._side("candidate-fixed", surface="codex")

    def use_passing_candidate(self) -> None:
        """从一开始就用正确的候选证据（全 pass 的 b0）。"""

        self.candidate = self._side("candidate-pass", surface="codex")

    def run_dir(self, name: str, *, baseline: int, digests: dict | None = None) -> Path:
        run_dir = self.root / name
        run_dir.mkdir(mode=0o700)
        inner = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": "campaign-eval",
            "phase": "VC-5",
            "candidate_revision": 1,
            "candidate_id": CANDIDATE,
            "evaluation_baseline": baseline or None,
            "baseline_commit_sha256": ("b" * 64) if baseline else None,
            "evaluator_digests": dict(digests or self.digests),
            "actions": [],
        }
        supervisor._write_json(
            run_dir / "campaign-run-manifest.json",
            {"schema_version": inner["schema_version"], "manifest_sha256": supervisor._sha256(supervisor._canonical(inner)), "manifest": inner},
            replace=False,
        )
        return run_dir

    def assertions_root(self, baseline: int) -> Path:
        root = self.campaign_dir / "assertions" / CANDIDATE
        return root / "revisions" / f"b{baseline}" if baseline else root

    def config(self, baseline: int) -> Path:
        results_dir = self.assertions_root(baseline) / "machine"
        results_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "campaign_dir": str(self.campaign_dir),
            "assertion_profile": str(self.profile_path),
            "rule_manifest": str(self.rule_manifest_path),
            "expected_profile_sha256": self.profile_sha256,
            "official_evidence_root": str(self.official),
            "candidate_evidence_root": str(self.candidate),
            "official_capture_manifest": str(self.official / gate.MANIFEST_FILENAME),
            "candidate_capture_manifest": str(self.candidate / gate.MANIFEST_FILENAME),
            "official_evidence_prefix": "official-run",
            "candidate_evidence_prefix": "candidate-run",
            "target_version": e2e.TARGET_VERSION,
            "candidate_id": CANDIDATE,
            "profile_id": "codex-eval-v1",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": e2e.AUTHORITY,
            "rules": ["SPEC-H1-001", "SPEC-EP-006"],
        }
        path = self.assertions_root(baseline) / "builder-config.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return path

    def run_builder(self, run_dir: Path, *, baseline: int, reuse_from: Path | None = None, authority: str = "none", env_override: dict | None = None, tree_root: Path | None = None) -> subprocess.CompletedProcess:
        """运行真实 builder 子进程；``tree_root`` 给出时在该副本受管树上运行（PYTHONPATH／cwd／脚本路径都指向副本）。"""

        builder = BUILDER if tree_root is None else managed_tree_copy.tool_root(tree_root) / managed_tree_copy.BUILDER_RELATIVE
        command = [
            sys.executable, str(builder),
            "--config", str(self.config(baseline)),
            "--output", str(self.assertions_root(baseline) / "results.json"),
            "--results-dir", str(self.assertions_root(baseline) / "machine"),
            "--evaluation-baseline", str(baseline),
            "--reuse-authority", authority,
        ]
        if reuse_from is not None:
            command.extend(["--reuse-from", str(reuse_from)])
        env = dict(os.environ) if tree_root is None else managed_tree_copy.subprocess_env(tree_root)
        env["CODEX_UPGRADE_CAMPAIGN_RUN_DIR"] = str(run_dir)
        env["CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE"] = "8" * 64
        env.update(env_override or {})
        return subprocess.run(command, capture_output=True, text=True, cwd=str(REPO_ROOT if tree_root is None else tree_root), env=env)

    def index(self, baseline: int) -> dict:
        return json.loads((self.assertions_root(baseline) / "evaluation-run.json").read_text(encoding="utf-8"))

    def checkpoints(self, baseline: int) -> list[Path]:
        directory = self.assertions_root(baseline) / "checkpoints"
        return sorted(p for p in directory.iterdir() if p.suffix == ".json" and not p.name.endswith("-input.json"))


class EvaluationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="eval-builder-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.fixture = EvaluationBuilderFixture(self.root)

    def test_b0_writes_checkpoints_and_index_before_nonzero_exit(self) -> None:
        run_dir = self.fixture.run_dir("run-b0", baseline=0)
        # 缺父监督器环境：失败关闭。
        bare = self.fixture.run_builder(run_dir, baseline=0, env_override={"CODEX_UPGRADE_CAMPAIGN_RUN_DIR": "", "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": ""})
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("父监督器派发", bare.stderr)
        completed = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        index = artifacts.validate_evaluation_run(self.fixture.index(0))
        statuses = {row["rule"]: row["status"] for row in index["rules"]}
        self.assertEqual(statuses, {"SPEC-EP-006": "fail", "SPEC-H1-001": "pass"})
        self.assertFalse((self.fixture.assertions_root(0) / "results.json").exists())
        chain = self.fixture.checkpoints(0)
        # dual_wire 两侧 + candidate_profile 一侧 = 3 条 checkpoint，链摘要闭合。
        self.assertEqual(len(chain), 3)
        previous = None
        for path in chain:
            checkpoint = artifacts.validate_evaluation_checkpoint(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(checkpoint["previous_checkpoint_sha256"], previous)
            self.assertEqual(checkpoint["executed_by"]["run_dir"], str(run_dir))
            self.assertEqual(checkpoint["executed_by"]["owner_nonce"], "8" * 64)
            self.assertIsNone(checkpoint["reused_from"])
            projection = self.fixture.campaign_dir / checkpoint["input_projection"]["path"]
            self.assertEqual(_file_sha256(projection), checkpoint["projection_sha256"])
            document = json.loads((self.fixture.campaign_dir / checkpoint["document"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(document["projection_sha256"], checkpoint["projection_sha256"])
            previous = checkpoint["checkpoint_sha256"]
        self.assertEqual(index["checkpoint_head_sha256"], previous)
        self.assertEqual(index["evaluator"], self.fixture.digests)
        # 同基线幂等重入：有效 checkpoint／index 视为完成，零 checker（文档未改写），按既有 fail 退出。
        stamps = {p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("*.json")}
        reentry = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(reentry.returncode, 1)
        self.assertIn('"reentry": true', reentry.stdout)
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("*.json")}, stamps)
        self.assertEqual(len(self.fixture.checkpoints(0)), 3)

    def test_crash_resume_only_runs_remaining_rules(self) -> None:
        run_dir = self.fixture.run_dir("run-b0", baseline=0)
        # 第一次只跑一条规则（模拟第 n 条后崩溃：checkpoint 已落、index 未落）。
        config_path = self.fixture.config(0)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["rules"] = ["SPEC-H1-001"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        partial = subprocess.run(
            [sys.executable, str(BUILDER), "--config", str(config_path), "--output", str(self.fixture.assertions_root(0) / "results.json"), "--results-dir", str(self.fixture.assertions_root(0) / "machine"), "--evaluation-baseline", "0"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={**os.environ, "CODEX_UPGRADE_CAMPAIGN_RUN_DIR": str(run_dir), "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": "8" * 64},
        )
        self.assertEqual(partial.returncode, 0, partial.stderr)
        # 人为撤回 index 与 results（模拟 index 落盘前崩溃），保留 checkpoint。
        (self.fixture.assertions_root(0) / "evaluation-run.json").unlink()
        (self.fixture.assertions_root(0) / "results.json").unlink()
        self.assertEqual(len(self.fixture.checkpoints(0)), 2)
        first_documents = {p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("SPEC-H1-001.json")}
        # 续跑全部规则：SPEC-H1-001 两侧命中既有 checkpoint 不重跑，只执行 SPEC-EP-006。
        resumed = self.fixture.run_builder(run_dir, baseline=0)
        self.assertEqual(resumed.returncode, 1)
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.fixture.assertions_root(0) / "machine").rglob("SPEC-H1-001.json")}, first_documents)
        self.assertEqual(len(self.fixture.checkpoints(0)), 3)
        index = self.fixture.index(0)
        self.assertEqual({row["rule"]: row["status"] for row in index["rules"]}, {"SPEC-EP-006": "fail", "SPEC-H1-001": "pass"})

    def test_b1_reuses_pass_rule_when_anchored_and_dependencies_unchanged(self) -> None:
        run_b0 = self.fixture.run_dir("run-b0", baseline=0)
        self.assertEqual(self.fixture.run_builder(run_b0, baseline=0).returncode, 1)
        b0_index = self.fixture.assertions_root(0) / "evaluation-run.json"
        run_b1 = self.fixture.run_dir("run-b1", baseline=1)
        # reuse_authority=none：拒写 reused，全部规则重跑（新 checkpoint 无 reused_from）。
        none_run = self.fixture.run_builder(run_b1, baseline=1, reuse_from=b0_index, authority="none")
        self.assertEqual(none_run.returncode, 1, none_run.stderr)
        b1_none = [json.loads(p.read_text(encoding="utf-8")) for p in self.fixture.checkpoints(1)]
        self.assertEqual(len(b1_none), 3)
        self.assertTrue(all(c["reused_from"] is None for c in b1_none))
        # anchored 且依赖未变：新基线 b2 复用 pass 规则（reused checkpoint 引用 b0 文档），fail 规则重跑重现失败。
        run_b2 = self.fixture.run_dir("run-b2", baseline=2)
        anchored = self.fixture.run_builder(run_b2, baseline=2, reuse_from=b0_index, authority="anchored")
        self.assertEqual(anchored.returncode, 1, anchored.stderr)
        b2 = {(c["rule"], c["side"]): c for c in (json.loads(p.read_text(encoding="utf-8")) for p in self.fixture.checkpoints(2))}
        self.assertEqual(b2[("SPEC-H1-001", "candidate")]["reused_from"]["baseline"], 0)
        self.assertEqual(b2[("SPEC-H1-001", "official")]["reused_from"]["baseline"], 0)
        self.assertIsNone(b2[("SPEC-EP-006", "candidate")]["reused_from"])
        b0_documents = {c["rule"]: c["document"] for c in (json.loads(p.read_text(encoding="utf-8")) for p in self.fixture.checkpoints(0)) if c["side"] == "candidate"}
        self.assertEqual(b2[("SPEC-H1-001", "candidate")]["document"], b0_documents["SPEC-H1-001"])
        self.assertFalse((self.fixture.assertions_root(2) / "machine" / "candidate" / "SPEC-H1-001.json").exists())
        index2 = artifacts.validate_evaluation_run(self.fixture.index(2))
        rows = {row["rule"]: row for row in index2["rules"]}
        self.assertEqual(rows["SPEC-H1-001"]["reused_from"]["baseline"], 0)
        self.assertEqual(rows["SPEC-EP-006"]["status"], "fail")
        # 冻结值与当前树不一致（伪造 checker 摘要）：builder 互校失败关闭，不写任何 checkpoint／投影。
        forged = dict(self.fixture.digests, checker_sha256="e5" * 32)
        run_forged = self.fixture.run_dir("run-forged", baseline=3, digests=forged)
        rejected = self.fixture.run_builder(run_forged, baseline=3, reuse_from=b0_index, authority="anchored")
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("builder 拒绝执行", rejected.stderr)
        self.assertFalse((self.fixture.assertions_root(3) / "checkpoints").exists())
        # checker 真实变化（副本受管树：checker 文件末尾追加注释，冻结值＝副本树真实摘要）：
        # 复用判据不成立，全部重跑，checkpoint 记录的 checker 摘要＝副本 checker 文件摘要。
        copy_root = managed_tree_copy.copy_managed_tree(self.root / "tree-checker-changed", include_tests=False)
        managed_tree_copy.append_comment(copy_root, managed_tree_copy.CHECKER_RELATIVE, "evaluator fix: no behavior change")
        managed_tree_copy.assert_tree_binding(copy_root)
        changed = managed_tree_copy.evaluator_digests(copy_root)
        self.assertNotEqual(changed["checker_sha256"], self.fixture.digests["checker_sha256"])
        self.assertEqual(changed["builder_sha256"], self.fixture.digests["builder_sha256"])
        run_b3 = self.fixture.run_dir("run-b3", baseline=4, digests=changed)
        rerun = self.fixture.run_builder(run_b3, baseline=4, reuse_from=b0_index, authority="anchored", tree_root=copy_root)
        self.assertEqual(rerun.returncode, 1, rerun.stderr)
        b3 = [json.loads(p.read_text(encoding="utf-8")) for p in self.fixture.checkpoints(4)]
        self.assertEqual(len(b3), 3)
        self.assertTrue(all(c["reused_from"] is None for c in b3))
        checker_copy = managed_tree_copy.tool_root(copy_root) / managed_tree_copy.CHECKER_RELATIVE
        self.assertTrue(all(c["checker_sha256"] == _file_sha256(checker_copy) for c in b3))
        index3 = artifacts.validate_evaluation_run(self.fixture.index(4))
        self.assertEqual(index3["evaluator"], changed)
        for checkpoint in b3:
            document = json.loads((self.fixture.campaign_dir / checkpoint["document"]["path"]).read_text(encoding="utf-8"))
            # 三者一致：单规则文档（checker 自记）＝checkpoint＝index。
            self.assertEqual(document["checker_sha256"], checkpoint["checker_sha256"])

    def test_fixed_evidence_changes_projection_and_full_pass_writes_results(self) -> None:
        run_b0 = self.fixture.run_dir("run-b0", baseline=0)
        self.assertEqual(self.fixture.run_builder(run_b0, baseline=0).returncode, 1)
        b0_index = self.fixture.assertions_root(0) / "evaluation-run.json"
        self.fixture.fix_candidate()
        run_b1 = self.fixture.run_dir("run-b1", baseline=1)
        completed = self.fixture.run_builder(run_b1, baseline=1, reuse_from=b0_index, authority="anchored")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        index = artifacts.validate_evaluation_run(self.fixture.index(1))
        self.assertTrue(all(row["status"] == "pass" for row in index["rules"]))
        # 候选投影变化：候选侧全部重跑；官方侧投影未变但规则级依赖摘要变化，同样不复用。
        self.assertTrue(all(row["reused_from"] is None for row in index["rules"]))
        results = json.loads((self.fixture.assertions_root(1) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual({row["rule"] for row in results["rules"]}, {"SPEC-H1-001", "SPEC-EP-006"})
        for row in results["rules"]:
            self.assertIn(assertion.PROJECTION_FLAG, row["candidate_command"])
        # 全 pass 后 results.json write-once：再次运行是幂等重入。
        again = self.fixture.run_builder(run_b1, baseline=1, reuse_from=b0_index, authority="anchored")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('"reentry": true', again.stdout)


class AcceptReuseBranchTests(unittest.TestCase):
    """T5.7：accept 对 evaluation-run.json 的 executed／reused 行分支——复用行以历史 inventory／context 校验、不重放 checker，锚点链伪造拒绝。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="eval-accept-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.fixture = EvaluationBuilderFixture(self.root)
        self.fixture.use_passing_candidate()

    def _inventory(self, bundle_dir: Path, prefix: str) -> dict:
        entries = []
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file() and p.name != "provenance.json"):
            entries.append({"path": f"{prefix}/{path.relative_to(bundle_dir).as_posix()}", "sha256": _file_sha256(path)})
        return {"entries": entries}

    def _stage(self, bundle_dir: Path, prefix: str) -> dict:
        return {
            "status": "complete",
            "assertion_context": {"capture_manifest_path": str(bundle_dir / gate.MANIFEST_FILENAME), "evidence_root": str(bundle_dir), "evidence_prefix": prefix},
            "evidence_inventory": self._inventory(bundle_dir, prefix),
            "identity": {"profile_id": "codex-eval-v1", "profile_digest": "d" * 64},
        }

    def _anchor_chain(self, run_b0: Path, b0_digests: dict, current: dict) -> tuple[dict, Path]:
        """b1 的 recovery／AUTHORIZATION／COMMIT／diagnosis 与失败 run 的动作输出绑定（accept 复用行的锚点链）。

        ``current``：修复后（accept 读侧真实变化的副本树）的 evaluator 四项，作为 b1 授权口径冻结进 recovery。
        """

        campaign_dir = self.fixture.campaign_dir
        b0_root = self.fixture.assertions_root(0)
        index_path = b0_root / "evaluation-run.json"
        chain = [json.loads(p.read_text(encoding="utf-8")) for p in self.fixture.checkpoints(0)]
        head = chain[-1]["checkpoint_sha256"]
        inner = json.loads((run_b0 / "campaign-run-manifest.json").read_text(encoding="utf-8"))
        supervisor._write_json(run_b0 / "state.json", {"state": "failed"}, replace=False)
        binding = supervisor.write_action_output_binding(
            run_b0, campaign_dir=campaign_dir, campaign_id="campaign-eval", phase="VC-5", action_id="acceptance",
            run_manifest_sha256=inner["manifest_sha256"], owner_nonce="8" * 64,
            output_bindings=sorted([f"acceptance/{CANDIDATE}/result.json", f"assertions/{CANDIDATE}/checkpoints", f"assertions/{CANDIDATE}/evaluation-run.json"]),
        )
        supervisor._stop_receipt(run_b0, event_type="failed", reason="action-failed:acceptance", detected_at_epoch=1.0, owner_pid=1, owner_nonce="8" * 64, campaign_id="campaign-eval", phase="VC-5", action_outputs_sha256=binding["binding_sha256"])
        diagnostic = run_b0 / "action-diagnostics" / "action-acceptance-failure.json"
        diagnostic.parent.mkdir(mode=0o700, exist_ok=True)
        supervisor._write_action_diagnostic(diagnostic, campaign_id="campaign-eval", phase="VC-5", action_id="acceptance", owner_pid=1, owner_nonce="8" * 64, failure_kind="child-returncode", error_type="ChildProcessError", message="子命令以非零状态退出，未提供进一步的脱敏诊断。")
        baseline_dir = campaign_dir / "candidates" / CANDIDATE / "revisions" / "b1"
        baseline_dir.mkdir(parents=True)
        diagnosis = artifacts.build_evaluation_failure_diagnosis(
            campaign_id="campaign-eval", campaign_manifest_sha256="1" * 64, candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=0,
            failure_source="offline-accept-failed", reuse_authority="anchored", failed_step="acceptance",
            failed_run={"run_id": run_b0.name, "run_dir": str(run_b0), "manifest_sha256": _file_sha256(run_b0 / "campaign-run-manifest.json"), "state_sha256": _file_sha256(run_b0 / "state.json"), "stop_receipt_sha256": _file_sha256(run_b0 / "stop-receipt.json"), "action_id": "acceptance", "action_diagnostic_sha256": _file_sha256(diagnostic), "action_outputs_sha256": binding["binding_sha256"]},
            action_outputs={"path": str(run_b0 / "action-outputs" / "acceptance.json"), "sha256": _file_sha256(run_b0 / "action-outputs" / "acceptance.json"), "evaluation_run": {"path": index_path.relative_to(campaign_dir).as_posix(), "sha256": _file_sha256(index_path)}, "checkpoint_head_sha256": head},
            evaluation_run={"path": index_path.relative_to(campaign_dir).as_posix(), "sha256": _file_sha256(index_path), "derived": False},
            failure_scope={"failed_rules": [], "failed_checks": [], "jobs": [], "rules": [], "official_refs": []},
            failed_evaluator_digests=b0_digests, current_evaluator_digests=current, admissible_classes=["approval-inputs", "candidate-source", "evaluator-defect"],
            project_ledger_head_sequence=1, project_ledger_head_sha256="2" * 64, reviewer="boss", reviewed_at_utc="2026-09-19T00:00:00Z",
        )
        diagnosis_path = baseline_dir / "diagnosis.json"
        diagnosis_path.write_text(json.dumps(diagnosis), encoding="utf-8")
        recovery = artifacts.build_evaluation_recovery(
            campaign_id="campaign-eval", candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=1, kind="evaluator-only",
            diagnosis={"path": "candidates/candidate-eval/revisions/b1/diagnosis.json", "sha256": _file_sha256(diagnosis_path)},
            failure_source="offline-accept-failed", reuse_authority="anchored", root_cause_class="evaluator-defect", root_cause_id="rc1-" + "0" * 20,
            failed_step="acceptance", previous_baseline=0, previous_baseline_commit_sha256=None, execute_rules=[], reuse_rules=["SPEC-EP-006", "SPEC-H1-001"],
            execute_jobs=[], reuse_jobs=["job-a"], attempt_id=None, recovery_revision=None, fix_commit="a" * 40,
            deployment_receipt={"path": "/deploy/receipt.json", "sha256": "3" * 64}, evaluation_epoch=None,
            failed_evaluator_digests=b0_digests, current_evaluator_digests=current,
            reviewer="boss", approved_at_utc="2026-09-19T00:00:00Z",
        )
        (baseline_dir / "recovery.json").write_text(json.dumps(recovery), encoding="utf-8")
        authorization = artifacts.build_evaluation_baseline_authorization(
            campaign_id="campaign-eval", candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=1, recovery_sha256=recovery["recovery_sha256"],
            ledger_operation_id="evaluation-recover:candidate-eval:r1:b1", ledger_event_sha256="4" * 64, project_ledger_head_sequence=2, project_ledger_head_sha256="5" * 64,
            root_cause_id="rc1-" + "0" * 20, root_cause_count=1, authorized_at_utc="2026-09-19T00:00:00Z",
        )
        (baseline_dir / "AUTHORIZATION").write_text(json.dumps(authorization), encoding="utf-8")
        capture_result = campaign_dir / "candidates" / CANDIDATE / "result.json"
        capture_result.write_text('{"status":"complete"}', encoding="utf-8")
        commit = artifacts.build_evaluation_baseline_commit(
            campaign_id="campaign-eval", candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=1, kind="evaluator-only",
            recovery_sha256=recovery["recovery_sha256"], authorization_sha256=authorization["authorization_sha256"],
            stage_sources={
                "capture-candidate": {"source": "reused", "baseline": 0, "path": f"candidates/{CANDIDATE}/result.json", "sha256": _file_sha256(capture_result)},
                "compare": {"source": "reused", "baseline": 0, "path": f"comparisons/{CANDIDATE}/result.json", "sha256": "6" * 64},
                "assertions": {"source": "local", "target": f"assertions/{CANDIDATE}/revisions/b1"},
                "accept": {"source": "local", "target": f"acceptance/{CANDIDATE}/revisions/b1/result.json"},
            },
            committed_at_utc="2026-09-19T00:00:00Z",
        )
        (baseline_dir / "COMMIT").write_text(json.dumps(commit), encoding="utf-8")
        return commit, baseline_dir

    def test_reused_rows_validate_against_history_without_checker_replay(self) -> None:
        run_b0 = self.fixture.run_dir("run-b0", baseline=0)
        self.assertEqual(self.fixture.run_builder(run_b0, baseline=0).returncode, 0)
        b0_index = self.fixture.assertions_root(0) / "evaluation-run.json"
        # accept 读侧真实变化的副本树（只有 accept_reader 闭包摘要变；checker／builder 不变）。
        fixed_tree = managed_tree_copy.copy_managed_tree(self.root / "tree-accept-fixed", include_tests=False)
        managed_tree_copy.mutate_accept_reader(fixed_tree)
        managed_tree_copy.assert_tree_binding(fixed_tree)
        current = managed_tree_copy.evaluator_digests(fixed_tree)
        self.assertNotEqual(current["accept_reader_sha256"], self.fixture.digests["accept_reader_sha256"])
        self.assertEqual(current["checker_sha256"], self.fixture.digests["checker_sha256"])
        commit, baseline_dir = self._anchor_chain(run_b0, self.fixture.digests, current)
        run_b1 = self.fixture.run_dir("run-b1", baseline=1, digests=current)
        completed = self.fixture.run_builder(run_b1, baseline=1, reuse_from=b0_index, authority="anchored", tree_root=fixed_tree)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        index_b1 = artifacts.validate_evaluation_run(self.fixture.index(1))
        self.assertTrue(all(row["reused_from"] is not None for row in index_b1["rules"]))
        results = json.loads((self.fixture.assertions_root(1) / "results.json").read_text(encoding="utf-8"))
        campaign_dir = self.fixture.campaign_dir
        current_identity = codex_upgrade._tool_identity(include_git=False)
        manifest = {
            "target_version": e2e.TARGET_VERSION,
            "tool_identity": {
                "entries": [
                    {"path": "candidate_rule_assertion.py", "sha256": _file_sha256(CHECKER)},
                    {"path": "build_rule_assertion_results.py", "sha256": _file_sha256(BUILDER)},
                ],
                "evidence_semantics_sha256": current_identity["evidence_semantics_sha256"],
            },
        }
        attempt_root = self.root / "candidate-attempt"
        attempt_root.mkdir(mode=0o700)
        classification = {
            "assertion_profile_manifest": {"path": "control/assertion-profile.json", "sha256": self.fixture.profile_sha256},
            "target_rule_manifest": {"path": "control/target-rules.json", "sha256": _file_sha256(self.fixture.rule_manifest_path)},
            "package_digest": e2e.AUTHORITY["classification_package_digest"],
            "joint_manifest_sha256": e2e.AUTHORITY["review_sha256"],
        }
        # 让 candidate_profile 行的官方权威与 builder 写入的三摘要一致。
        classification["assertion_profile_manifest"]["sha256"] = e2e.AUTHORITY["assertion_profile_sha256"]
        official = self._stage(self.fixture.official, "official-run")
        candidate = self._stage(self.fixture.candidate, "candidate-run")
        comparison = {"official_package_digest": "1" * 64, "candidate_package_digest": "2" * 64, "package_digest": "3" * 64}
        rules = ("SPEC-EP-006", "SPEC-H1-001")
        calls: list[str] = []

        def rerun(command, submitted, *, rule, label):
            calls.append(rule)

        def stage_result(campaign_dir_arg, stage, candidate_id=None, **kwargs):
            return candidate

        # 批准画像摘要与文件不一致时 _acceptance_contract 会拒绝：这里以真实文件摘要作 profile 绑定，
        # 官方权威三摘要单独从 classification 派生（builder config 用的是 e2e.AUTHORITY）。
        with mock.patch.object(codex_upgrade, "_current_evaluation_baseline", return_value=(1, commit)), \
             mock.patch.object(codex_upgrade, "_load_stage_result", side_effect=stage_result), \
             mock.patch.object(codex_upgrade, "_capture_stage_attempt_context", return_value=(attempt_root, {})), \
             mock.patch.object(codex_upgrade, "_rerun_machine_assertion", side_effect=rerun), \
             mock.patch.object(codex_upgrade, "_classification_official_authority", return_value=dict(e2e.AUTHORITY)), \
             mock.patch.object(codex_upgrade, "_acceptance_contract_sha256", return_value=results["acceptance_contract_sha256"]):
            classification["assertion_profile_manifest"]["sha256"] = self.fixture.profile_sha256
            gate_result = codex_upgrade._validate_assertion_results(
                campaign_dir, results, rules=rules, manifest=manifest, candidate_id=CANDIDATE,
                classification=classification, official=official, candidate=candidate, comparison=comparison,
            )
            self.assertEqual((gate_result["complete"], gate_result["pass_count"]), (True, 2))
            # 复用行不重放 checker。
            self.assertEqual(calls, [])
            # 锚点链伪造：recovery 的 reuse_authority 改为 none → 拒绝。
            recovery_path = baseline_dir / "recovery.json"
            original_recovery = recovery_path.read_bytes()
            forged = json.loads(original_recovery)
            forged["reuse_authority"] = "none"
            forged["reuse_rules"] = []
            forged["recovery_sha256"] = artifacts.digest({k: v for k, v in forged.items() if k != "recovery_sha256"})
            recovery_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "复用授权|摘要链|不一致"):
                codex_upgrade._validate_assertion_results(
                    campaign_dir, results, rules=rules, manifest=manifest, candidate_id=CANDIDATE,
                    classification=classification, official=official, candidate=candidate, comparison=comparison,
                )
            recovery_path.write_bytes(original_recovery)
            # 动作输出绑定记录的 evaluation-run 摘要漂移（被复用基线索引被改写）→ 拒绝。
            index_bytes = b0_index.read_bytes()
            b0_index.chmod(0o600)
            b0_index.write_bytes(index_bytes + b"\n")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "被复用基线不一致|无法校验|漂移"):
                codex_upgrade._validate_assertion_results(
                    campaign_dir, results, rules=rules, manifest=manifest, candidate_id=CANDIDATE,
                    classification=classification, official=official, candidate=candidate, comparison=comparison,
                )
            b0_index.write_bytes(index_bytes)
            # 历史 inventory：复用行按 b0 候选 inventory 校验；当前 inventory 不含旧根也不影响复用行。
            stripped = dict(candidate, evidence_inventory={"entries": [{"path": "candidate-run/other", "sha256": "0" * 64}]})
            with mock.patch.object(codex_upgrade, "_load_stage_result", side_effect=lambda *a, **k: candidate):
                gate_result = codex_upgrade._validate_assertion_results(
                    campaign_dir, results, rules=rules, manifest=manifest, candidate_id=CANDIDATE,
                    classification=classification, official=official, candidate=stripped, comparison=comparison,
                )
            self.assertTrue(gate_result["complete"])


if __name__ == "__main__":
    unittest.main()
