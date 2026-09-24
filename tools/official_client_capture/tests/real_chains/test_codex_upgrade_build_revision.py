"""R2：ARM64 真二进制／镜像在同一 Campaign 换 revision，并在 seal 写后真实 SIGKILL。

官方与分类输入沿用既有零请求夹具；实际 Docker 装配、构建收据、账本和批次入口不替换。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import traceback
import unittest
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture.tests import candidate_identity_fixture as cif
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests.test_codex_upgrade_candidate_revision import _ChainMixin, R1, R2
from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import dispatch_cli


class ReusableIdentity(cif.CandidateIdentityFixture):
    """为已有真实制品夹具补齐 R2 输入收据；前端仍是明确的隔离样本。"""

    created = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created.append(self)

    def create_source_tree(self):
        result = super().create_source_tree()
        (self.source / "go.sum").write_text("")
        return result

    def assemble_trees(self):
        super().assemble_trees()
        (self.build_tree / "vendor").mkdir(mode=0o755)
        (self.build_tree / "vendor/modules.txt").write_text("")

    def build_parameters(self):
        path = super().build_parameters()
        parameters = driver._read(path)
        info = build._parse_go_build_info(cif._run([cif.go_binary(), "version", "-m", str(self.binary)]).stdout)
        # 本隔离样本只有一个真实 Alpine 基础镜像；三项引用均冻结到它，绝不宣称是生产前端构建。
        inspected = json.loads(cif._run(["docker", "image", "inspect", cif.BASE_IMAGE]).stdout)[0]
        parameters["input_provenance"] = {"go_version": info["go_version"], "base_images": {
            name: inspected["RepoDigests"][0] for name in ("ALPINE_IMAGE", "POSTGRES_IMAGE", "NODE_IMAGE")}}
        driver._write(path, parameters)
        return path

    def inputs(self):
        parameters = driver._read(self.work / "build-parameters.json")
        return build.collect_implementation_inputs(parameters,
            source_tree_sha256=upgrade._directory_tree_digest(self.source),
            requirements_sha256=driver._read(self.source / "gates/gate-plan.json")["requirements_sha256"],
            go_version=parameters["input_provenance"]["go_version"])

    def write_implementation_receipt(self, **kwargs):
        # 父夹具生成 facts 与原始证据；本夹具在第一次正式 record 之前重新签发含输入的版本。
        root, path = super().write_implementation_receipt(**kwargs)
        facts = driver._read(root / "facts.json")
        facts["assertions"]["build_inputs"] = self.inputs()
        driver._write(root / "facts.json", facts)
        path.unlink()
        receipts.finalize(root, "facts.json", path.name)
        return root, path


@unittest.skipUnless(cif.available(), "需要 ARM64 Linux 的 Go 与 Docker 实物环境")
class BuildRevisionRealChainTests(_ChainMixin, unittest.TestCase):
    def setUp(self):
        self.helper = driver.new_real_chain_case()
        self.addCleanup(self.helper.doCleanups)

    def test_image_only_revision_seal_kill_resume_and_vc5_batch(self):
        ReusableIdentity.created.clear()
        self.addCleanup(lambda: [item.cleanup_docker() for item in ReusableIdentity.created])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {project_ledger_fixture.FIXTURE_ONLY_ENV: "1"}):
            root = Path(directory).resolve() / "staging"
            root.mkdir(mode=0o700)
            project_ledger_fixture.install_fixture_ledger(root)
            args = argparse.Namespace(root=root, campaign_id="r2-image-revision", no_candidate_identity=False,
                full_chain=False, two_job_candidate=False, spec_path=Path(upgrade.__file__).parents[2] / "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md")
            with mock.patch.object(cif, "CandidateIdentityFixture", ReusableIdentity):
                context = driver.init_campaign_to_vc4(args, self.helper)
            old = context["identity_fixture"]
            campaign, manifest = context["campaign_dir"], context["manifest"]
            fixture = {**context["fixture"], "state_dir": context["state_dir"]}
            original = {path: path.read_bytes() for base in (old.implementation_root, campaign / "official-evidence", campaign / "classification")
                        for path in base.rglob("*") if path.is_file()}
            vc3 = (campaign / "control/vc/vc-3-checkpoint.json").read_bytes()
            next_sequence = max(upgrade._committed_vc_sequences(campaign, manifest)) + 1
            failed = self._fail_vc5_into_review(fixture, root, sequence=next_sequence, tag="r1-image-fail")
            reconciler.reconcile_supervisor_run(failed, campaign)
            preview = upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview"))
            upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=preview["review_sha256"]))
            self._open(fixture, R2, supersedes=R1)
            plan_source = root / "r2-plan-source"
            cif._run(["git", "clone", "-q", str(old.source), str(plan_source)])
            dispatch_cli(self.helper, root, fixture, context["state_dir"], "VC-4", next_sequence + 1, "r2-plan", [
                "plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", R2,
                "--candidate-source", str(plan_source), "--mapping", str(plan_source / "gates/mapping.json"),
                "--output", str(plan_source / "gates/gate-plan-dispatch.json"),
            ])
            new = ReusableIdentity(root / "candidate-r2", candidate_id=R2, target_version=old.target_version, baseline_version=old.baseline_version)
            new.source, new.base_commit, new.current_commit = old.source, old.base_commit, old.current_commit
            new.binary.parent.mkdir(parents=True)
            cif._copy_file(old.binary, new.binary)
            new.assemble_trees()
            new.build_image()
            new.write_builder_receipt()
            parameters_path = new.build_parameters()
            new.write_source_transition()
            current = new.inputs()
            self.assertEqual(current, old.inputs(), "镜像变化不能混入实现测试输入")
            evidence = new.implementation_root
            evidence.mkdir(mode=0o700)
            facts = receipts.build_reused_implementation_facts(campaign / "control/vc/revisions/r2/revision.json", current)
            driver._write(evidence / "facts.json", facts)
            reuse = receipts.finalize(evidence, "facts.json", "receipt.json")
            self.assertEqual(reuse["assertions"]["reuse"]["execute_gate_ids"], [])
            self.assertEqual(reuse["assertions"]["reuse"]["reused_from"], "r1")
            error_path = root / "child-error.txt"

            def record():
                return new.record_build(campaign, manifest, build_parameters=parameters_path,
                    implementation_root=evidence, implementation_receipt=evidence / "receipt.json")

            def child():
                original_seal = upgrade._seal_candidate_revision
                def killed(*args, **kwargs):
                    original_seal(*args, **kwargs)
                    os.kill(os.getpid(), signal.SIGKILL)
                try:
                    with mock.patch.object(upgrade, "_seal_candidate_revision", side_effect=killed):
                        record()
                except BaseException:
                    error_path.write_text(traceback.format_exc())
            process = multiprocessing.get_context("fork").Process(target=child)
            process.start()
            process.join(60)
            if process.is_alive():
                process.kill()
                process.join(5)
                self.fail("seal 后故障注入超时")
            self.assertEqual(process.exitcode, -signal.SIGKILL, error_path.read_text() if error_path.exists() else "")
            seal_path = campaign / "control/vc/revisions/r2/seal.json"
            seal_bytes = seal_path.read_bytes()
            self.assertFalse((campaign / "candidates" / R2 / "build-receipt.json").exists())
            # 故障注入不伪造父批次成功；正式 record 批次从已有 seal 续作并收口阶段账本。
            result = dispatch_cli(self.helper, root, fixture, context["state_dir"], "VC-4", next_sequence + 2, "r2-build", [
                "record-candidate-build", "--campaign-dir", str(campaign), "--candidate-id", R2,
                "--candidate-purpose", manifest["campaign_purpose"], "--deployed-version", manifest["target_version"],
                "--target-architecture", "linux/arm64", "--build-id", f"build-eval-{new.nonce}",
                "--runtime-image", new.runtime_image, "--candidate-image-id", new.image_id,
                "--candidate-source", str(new.source), "--candidate-binary", str(new.binary),
                "--build-parameters", str(parameters_path), "--build-tree", str(new.build_tree),
                "--docker-context", str(new.context), "--frontend-dist-source", str(new.dist_source),
                "--catalog-stage-dir", str(new.source / "catalog"), "--source-transition", str(new.transition_path),
                "--gate-plan", str(new.source / "gates/gate-plan.json"),
                "--implementation-test-root", str(evidence), "--implementation-test-receipt", str(evidence / "receipt.json"),
            ])
            self.assertEqual(result["revision_seal"]["changed_layers"], ["build"])
            self.assertEqual(seal_path.read_bytes(), seal_bytes)
            checkpoint = campaign / "control/vc/revisions/r2/vc-4-checkpoint.json"
            before = checkpoint.read_bytes()
            self.assertEqual(record()["receipt_digest"], result["receipt_digest"])
            self.assertEqual(checkpoint.read_bytes(), before)
            # 负例：同一候选只改 --build-id 再登记。构建标识进入 seal 绑定的构建收据摘要，seal 先于收据核对，
            # 必须拒绝，且已登记的构建收据、seal 与 VC-4 checkpoint 字节不变。
            receipt_path = Path(result["build_receipt"])
            frozen = {path: path.read_bytes() for path in (receipt_path, seal_path, checkpoint)}
            with self.assertRaisesRegex(upgrade.ConfigurationError, "已经存在且内容不一致"):
                new.record_build(campaign, manifest, build_parameters=parameters_path, implementation_root=evidence,
                                 implementation_receipt=evidence / "receipt.json", build_id=f"build-eval-{new.nonce}-other")
            self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)
            # 正式读侧仍要求当前 Candidate，并且重跑镜像／context／dist 装配校验。
            upgrade._replay_candidate_build_receipt(campaign, manifest, R2, Path(result["build_receipt"]))
            continued, code = self._dispatch(fixture, root, "VC-5", next_sequence + 3, tag="r2-image-pass")
            self.assertEqual(code, 0, continued)
            self.assertEqual(self._summary(fixture)["current_revision"], 2)
            self.assertEqual((campaign / "control/vc/vc-3-checkpoint.json").read_bytes(), vc3)
            self.assertEqual(original, {path: path.read_bytes() for path in original})
            print(json.dumps({"改造项": "R2", "实现测试执行": 0,
                "实现测试复用": len(reuse["assertions"]["gates"]), "新增上游请求": 0,
                "SIGKILL断点": "revision-seal 写后、build receipt 写前", "候选revision": 2,
                "VC4_checkpoint文件数": len(list(checkpoint.parent.glob("vc-4-checkpoint.json"))),
                "VC5批次状态": continued["status"], "changed_layers": result["revision_seal"]["changed_layers"]}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
