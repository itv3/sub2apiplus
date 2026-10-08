"""Claude 的 VC-4 驱动（Claude 升级 B3-04a）：客户端表两项、驱动绑定、采集主机门禁需求预览、本机候选链装配与 VC-4 派发计划。

全部离线、零请求，不读真实仓库的 git 历史，不运行 go（候选源码的 Go 目录核对以替身给出加载结果；真实 go run 与本机候选链脚本的
端到端在真实链 ``real_chains/test_claude_vc4_candidate_chain.py``）。夹具沿用 B3-04 的候选夹具（VC-3 暂存批准、计时账本、
真实 revision-open）；驱动模块按路径加载（与驱动测试同一方式），编排器 CLI 在测试里换成进程内调用（夹具的受管工具树副本只在
本进程生效）。

* 客户端表：两客户端的门禁需求来源与派发脚本指向驱动里真实存在的脚本；绑定表与客户端表的客户端集合相同；VC-4 总控最后一步
  按表派发；本机候选链取料的路径与装配器的约定一致。
* 对 Codex 拒绝：Claude 专用的小步骤、派发脚本、两个 Python 入口与本机候选链遇到 Codex 参数失败关闭（Codex 流程不经过它们）。
* Claude 绑定：只读推出的门禁需求与需求预览逐字相同；需求预览子命令写 0600 文件，同一轮重跑收敛、内容不同即拒绝；revision 开启
  经 revision-open（同一参数重跑幂等）；本候选属于当前 revision 的核对与作废标记；前序构建收据只认客户端中立合同；实现测试复用
  关闭（early-test-mode 恒为 full）；实现门禁的目标 revision 路径；编排器 CLI 的失败与非 JSON 输出都拒绝。
* 本机装配：A 段文件的落点与字节（gate-plan.json 与 plan-candidate-gates 生成模式写出的字节相同，是采集主机派发后 cmp 的依据）、
  C 段经 verify_candidate_catalog 验收；总计划、需求预览、Active 底稿、内容寻址文件、Go 加载结果不符都在写入前拒绝或还原；
  承接收据只认 release-catalog.json 一条摘要边。
* VC-4 计划：两个批次计划通过共享动作计划合同与 Claude 的 VC-4 阶段幂等重派合同（构建开始前可逐字重派）；plan 预演写出与提交内
  gate-plan.json 逐字相同后删除；record 零写入预览在 Campaign 写域有变化时拒绝；派发完成后的收尾摘要。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from tools.ci import client_table as CT
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch
from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.tests import claude_candidate_build_fixture as build_fixture
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_capture.tests import test_driver_client_table as table_tests

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
CAMPAIGN_ID = candidate_tests.CAMPAIGN_ID
CANDIDATE = candidate_tests.CANDIDATE
OTHER = candidate_tests.OTHER
TARGET = classify_tests.TARGET
LIFECYCLE = PurePosixPath(CT.spec("claude")["lifecycle_dir"].format(**CT.format_fields(TARGET)))
REQUIREMENTS_FILENAME = "vc4-gate-requirements.json"


def load_driver(name: str, environment: dict[str, str]) -> types.ModuleType:
    """按路径加载驱动模块（同目录在 sys.path 前面，与驱动测试同一方式）；加载期间用给定环境（决定本轮客户端）。"""

    spec = importlib.util.spec_from_file_location(f"claude_vc4_driver_test_{name}", DRIVER / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, environment), mock.patch.object(sys, "path", [str(DRIVER), *sys.path]):
        spec.loader.exec_module(module)
    return module


class ClaudeCandidateImageReferenceTests(unittest.TestCase):
    """镜像配置 ID 和仓库 manifest digest 不同，成功与拒绝都不调用真实 Docker。"""

    def setUp(self) -> None:
        self.driver = load_driver("claude_vc4", {})
        self.repository = "registry.local/sub2api"
        self.reference = build_fixture.RUNTIME_IMAGE

    def _inspect(self, payload: Any, returncode: int = 0) -> Any:
        def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            self.assertEqual(arguments, ["docker", "image", "inspect", build_fixture.IMAGE_ID])
            self.assertEqual(kwargs["timeout"], 60)
            return subprocess.CompletedProcess(arguments, returncode, json.dumps(payload), "检查失败" if returncode else "")
        return run

    def test_uses_real_repository_digest_and_preserves_image_id(self) -> None:
        payload = [{"Id": build_fixture.IMAGE_ID, "RepoDigests": ["other/repo@sha256:" + "6" * 64, self.reference, self.reference]}]
        self.assertEqual(
            self.driver.candidate_image_reference(build_fixture.IMAGE_ID, self.repository, runner=self._inspect(payload)),
            self.reference,
        )
        self.assertNotEqual(self.reference.split("@", 1)[1], build_fixture.IMAGE_ID)

    def test_missing_ambiguous_or_wrong_image_is_rejected(self) -> None:
        cases = (
            ([], "恰好"),
            ([{"Id": "sha256:" + "9" * 64, "RepoDigests": [self.reference]}], "配置 ID"),
            ([{"Id": build_fixture.IMAGE_ID, "RepoDigests": []}], "唯一真实 RepoDigest"),
            ([{"Id": build_fixture.IMAGE_ID, "RepoDigests": ["other/repo@sha256:" + "6" * 64]}], "唯一真实 RepoDigest"),
            ([{"Id": build_fixture.IMAGE_ID, "RepoDigests": [self.reference, self.repository + "@sha256:" + "6" * 64]}], "唯一真实 RepoDigest"),
            ([{"Id": build_fixture.IMAGE_ID, "RepoDigests": [None]}], "字符串数组"),
            ([{"Id": build_fixture.IMAGE_ID, "RepoDigests": [self.repository + "@sha256:" + "z" * 64]}], "唯一真实 RepoDigest"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                self.driver.candidate_image_reference(build_fixture.IMAGE_ID, self.repository, runner=self._inspect(payload))

    def test_invalid_id_or_inspection_failure_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "镜像 ID"):
            self.driver.candidate_image_reference("sha256:" + "z" * 64, self.repository, runner=mock.Mock())
        with self.assertRaisesRegex(ValueError, "检查失败"):
            self.driver.candidate_image_reference(build_fixture.IMAGE_ID, self.repository, runner=self._inspect([], 1))
        with self.assertRaisesRegex(ValueError, "合法 JSON"):
            self.driver.candidate_image_reference(
                build_fixture.IMAGE_ID, self.repository,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "not-json", ""),
            )


def in_process_cli(argv: list[str]) -> tuple[int, dict[str, Any]]:
    """编排器 CLI 的进程内替身：与 ``ClaudeBinding.cli`` 同一口径（退出码 0／2 返回结果，其余报错）。"""

    code, result, stderr = candidate_tests.run_main([str(item) for item in argv])
    if code not in {0, 2} or result is None:
        raise ValueError(f"claude_upgrade {argv[0]} 失败（退出码 {code}）：{stderr.strip()[-800:]}")
    return code, result


def go_view(repository: Path, loaded: dict[str, Any]) -> dict[str, Any]:
    """Go 目录核对的替身：按装配器读到的暂存 Release 与 Active 底稿给出加载结果（与 claude_catalog_check.go 的输出同形）。"""

    staged = loaded["staged"]
    entry, selector = staged["release_entry"], staged["validation_candidate_selector"]
    active = json.loads(loaded["active"].read_text(encoding="utf-8"))["selectors"]["production_active"]
    data = (repository / candidate.RELEASE_CATALOG_RELATIVE).read_bytes()
    return {
        "status": "passed",
        "catalog_sha256": hashlib.sha256(data).hexdigest(),
        "validation_candidate": {
            "version": entry["version"], "platform": entry["platform"], "release_sha256": entry["release_sha256"],
            "profile_sha256": entry["profile"]["sha256"], "wire_sha256": entry["wire"]["sha256"], "changeset": selector["changeset"],
        },
        "production_active": {"release_sha256": active["release_sha256"], "changeset": active["changeset"]},
        "checker_sha256": "0" * 64,
    }


def git(root: Path, *arguments: str) -> str:
    return build_fixture._git(root, *arguments)


class Vc4TableAndRefusalTests(unittest.TestCase):
    """客户端表两项、绑定表与对 Codex 的失败关闭（不建 Campaign）。"""

    def test_table_entries_bindings_and_dispatch(self) -> None:
        for client in CT.CLIENTS:
            with self.subTest(client=client):
                row = CT.spec(client)
                self.assertIn(row["vc4_requirements_source"], ("local-chain", "arm64-preview"))
                self.assertTrue((DRIVER / row["vc4_dispatch_script"]).is_file())
        self.assertEqual((CT.spec("codex")["vc4_requirements_source"], CT.spec("codex")["vc4_dispatch_script"]), ("local-chain", "vc4.sh"))
        self.assertEqual((CT.spec("claude")["vc4_requirements_source"], CT.spec("claude")["vc4_dispatch_script"]), ("arm64-preview", "vc4-claude.sh"))
        client = load_driver("vc4_client", {"D": str(REPO_ROOT)})
        self.assertEqual(set(client.BINDINGS), set(CT.CLIENTS))
        self.assertEqual({name: binding.client for name, binding in client.BINDINGS.items()}, {name: name for name in CT.CLIENTS})
        self.assertEqual((client.CodexBinding.implementation_reuse, client.ClaudeBinding.implementation_reuse), (True, False))
        self.assertIn('bash "$DRV/$CT_VC4_DISPATCH_SCRIPT" "$E"', (DRIVER / "vc4-all.sh").read_text(encoding="utf-8"))
        # 本机候选链的 ssh tar 取料与装配器读的路径一致（总计划、profile 下的 VC-3 产物、需求预览）。
        script = (DRIVER / "local" / "local-claude-candidate-chain.sh").read_text(encoding="utf-8")
        self.assertIn("tar -cf - " + " ".join(LocalAssemblyTests.TAKE_PATHS) + " -C %q " + REQUIREMENTS_FILENAME, script)

    def test_codex_binding_forwards_to_the_original_codex_members(self) -> None:
        """Codex 的绑定逐个转调改造前直接调用的 codex_upgrade 成员（调用时按属性取，参数原样），续跑与实现门禁的编排器仍是
        codex_upgrade——Codex 的 VC-4 行为与改造前相同（现有驱动测试的打桩照样生效）。"""

        from tools.official_client_capture import codex_upgrade

        environment = {"D": str(REPO_ROOT), "NEW": "fixture-campaign", "CAND": "fixture-candidate"}
        with mock.patch.dict(os.environ, {"CLIENT": ""}):
            resume = load_driver("vc4_resume", environment)
            gates = load_driver("implementation_gates", environment)
        client = sys.modules["vc4_client"]
        for module in (resume, gates):
            self.assertIsInstance(module.CLIENT, client.CodexBinding)
            self.assertIs(module.upgrade, codex_upgrade)
        binding = resume.CLIENT
        campaign, manifest = Path("/fixture/campaign"), {"campaign_id": "fixture-campaign"}
        with mock.patch.object(codex_upgrade, "_load_stage_result", return_value={"stage": "classify"}) as stage, \
                mock.patch.object(codex_upgrade, "_load_vc3_gate_requirements", return_value=(None, None, {"requirements": 1})) as vc3, \
                mock.patch.object(codex_upgrade, "open_candidate_revision", return_value={"revision": 2}) as opened, \
                mock.patch.object(codex_upgrade, "_require_candidate_in_current_revision", return_value=(2, {"candidate_id": "x"})) as required, \
                mock.patch.object(vc_artifacts, "validate_candidate_build_receipt", return_value={"checked": True}) as validated:
            self.assertEqual(binding.requirements(campaign, manifest), {"requirements": 1})
            stage.assert_called_once_with(campaign, "classify")
            vc3.assert_called_once_with(campaign, manifest, {"stage": "classify"})
            self.assertEqual(binding.open_revision(campaign, "x", initial=False, supersedes="old"), {"revision": 2})
            opened.assert_called_once_with(argparse.Namespace(campaign_dir=campaign, candidate_id="x", initial=False, supersedes="old"))
            self.assertEqual(binding.require_current_revision(campaign, manifest, "x", action="实现测试复用"), (2, {"candidate_id": "x"}))
            required.assert_called_once_with(campaign, manifest, "x", action="实现测试复用")
            self.assertEqual(binding.previous_build_receipt({"receipt": 1}), {"checked": True})
            validated.assert_called_once_with({"receipt": 1})
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(binding.campaign(), REPO_ROOT / "evidence" / "campaigns" / "fixture-campaign")
        with self.assertRaisesRegex(ValueError, "本机候选链"):
            binding.requirements_preview(campaign, manifest, "x")

    def test_claude_only_entries_refuse_codex(self) -> None:
        with self.subTest("脚本"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            environment = {key: value for key, value in os.environ.items() if key != "CLIENT"}
            environment.update(fixture.env, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO_ROOT))
            for script, args in (("vc4-requirements.sh", ()), ("vc4-claude.sh", (str(root / "evidence"),))):
                result = driver_tests._run(DRIVER / script, *args, env=environment)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("（codex）", result.stdout)
            message = root / "a-message.txt"
            message.write_text("A\n", encoding="utf-8")
            result = driver_tests._run(DRIVER / "local" / "local-claude-candidate-chain.sh", fixture.round, fixture.stamp,
                                       str(root / "work"), "20261007", "c" * 40, str(message), env=environment)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("local-candidate-chain.sh", result.stdout)
            self.assertFalse((root / "work").exists(), "拒绝发生在取料之前")
            # 两个 Python 入口：在加载编排器之前就按客户端表拒绝。
            parser = table_tests.load_parse_env()
            values = parser.parse(fixture.env_file.read_text(encoding="utf-8"))
            exported = {**environment, **values, **parser.derive(values)}
            for argv in (["vc4_client.py", "requirements", "--output", str(fixture.runroot / REQUIREMENTS_FILENAME)], ["claude_vc4.py", "tree"]):
                completed = subprocess.run([sys.executable, "-B", str(DRIVER / argv[0]), *argv[1:]], env=exported, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertIn("codex", completed.stderr)
            self.assertFalse((fixture.runroot / REQUIREMENTS_FILENAME).exists())


class LocalAssemblyTests(candidate_tests._CandidateCase):
    """Claude 驱动在 B3-04 候选夹具上的全部用例（一个类：受管工具树副本只建一次）。"""

    # 本机候选链 ssh tar 取的 Campaign 路径（与 local-claude-candidate-chain.sh 同一组，测试核对两边一致）。
    TAKE_PATHS = (
        "campaign.json", "campaign.sha256", "profile/result.json", "profile/prepare/results", "profile/prepare/approved",
        "profile/release", "profile/stage",
    )

    def environment(self, campaign: Path, **extra: str) -> dict[str, str]:
        return {"CLIENT": "claude", "D": str(self.data), "NEW": CAMPAIGN_ID, "NEWDIR": str(campaign), "CAND": CANDIDATE, **extra}

    # -- 取料与合成仓库 --------------------------------------------------------------------------------

    def take_from(self, campaign: Path, preview: dict[str, Any]) -> Path:
        """模拟本机脚本的 ssh tar 取料：同一组 Campaign 路径，另放采集主机小步骤写的需求预览。"""

        take = self.root / "take"
        take.mkdir(mode=0o700)
        for relative in self.TAKE_PATHS:
            source, target = campaign / relative, take / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        (take / REQUIREMENTS_FILENAME).write_text(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return take

    def repository(self) -> Path:
        """合成候选仓库：基线提交只有 Claude 的 Active 目录（与真实仓库同一份字节）。"""

        repository = self.root / "repository"
        path = repository / candidate.RELEASE_CATALOG_RELATIVE
        path.parent.mkdir(parents=True)
        shutil.copyfile(profile_tests.ACTIVE_CATALOG, path)
        git(repository.parent, "init", "-q", str(repository))
        git(repository, "add", "-A")
        git(repository, "commit", "-q", "-m", "基线：Active 目录")
        return repository

    # -- 用例 ------------------------------------------------------------------------------------------

    def test_claude_binding_requirements_revision_and_receipts(self) -> None:
        campaign = self.staged()
        runroot = self.root / "runroot"
        runroot.mkdir(mode=0o700)
        environment = self.environment(campaign, RUNROOT=str(runroot), B=str(self.root / "candidate-dir"))
        resume = load_driver("vc4_resume", environment)
        client = sys.modules["vc4_client"]
        self.assertIsInstance(resume.CLIENT, client.ClaudeBinding)
        self.assertIs(resume.upgrade, claude_upgrade)
        with mock.patch.dict(os.environ, environment), mock.patch.object(client.ClaudeBinding, "cli", side_effect=in_process_cli) as cli, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            # revision 开启经 revision-open（编排器与 Store 两份登记），同一参数重跑幂等。
            resume.open_revision()
            resume.open_revision()
            self.assertEqual([call.args[0][:2] for call in cli.call_args_list], [["revision-open", "--campaign-dir"]] * 2)
            self.assertEqual([call.args[0][-1] for call in cli.call_args_list], ["--initial", "--initial"])
            self.assertEqual(printed.getvalue().splitlines(), [f"REVISION_OPENED r1 {CANDIDATE}"] * 2)
            binding = client.current()
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            self.assertEqual(binding.campaign(), campaign)
            code, preview, stderr = candidate_tests.run_main(self.preview_argv(campaign))
            self.assertEqual(code, 2, stderr)
            self.assertEqual(binding.requirements(campaign, manifest), preview["requirements"], "只读推出的需求与需求预览逐字相同")
            self.assertEqual(resume.context()[3], preview["requirements"])
            # 需求预览子命令：写 0600、同一轮重跑收敛、内容不同即拒绝；只能写在本轮 RUNROOT 下。
            output = runroot / REQUIREMENTS_FILENAME
            self.assertEqual(client.main(["requirements", "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), preview)
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
            self.assertEqual(client.main(["requirements", "--output", str(output)]), 0)
            output.write_text(output.read_text(encoding="utf-8").replace(preview["requirements_sha256"], "0" * 64), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(client.main(["requirements", "--output", str(output)]), 3)
                self.assertEqual(client.main(["requirements", "--output", str(self.root / REQUIREMENTS_FILENAME)]), 3)
            self.assertIn("已有一份不同的门禁需求文件", error.getvalue())
            self.assertIn("只能写在本轮 RUNROOT 下", error.getvalue())
            # 需求预览与只读推出的需求不同、或预览登记的摘要不能复算，都拒绝。
            with mock.patch.object(client.ClaudeBinding, "requirements", return_value={**preview["requirements"], "affected_rule_ids": []}), \
                    self.assertRaisesRegex(ValueError, "只读推出的门禁需求不同"):
                binding.requirements_preview(campaign, manifest, CANDIDATE)
            with mock.patch.object(client.ClaudeBinding, "cli", return_value=(2, {**preview, "requirements_sha256": "0" * 64})), \
                    self.assertRaisesRegex(ValueError, "摘要与需求本身不一致"):
                binding.requirements_preview(campaign, manifest, CANDIDATE)
            # 本候选属于当前 revision；别的候选、作废标记、取代标记都拒绝。
            revision, record = binding.require_current_revision(campaign, manifest, CANDIDATE, action="测试")
            self.assertEqual((revision, record["candidate_id"]), (1, CANDIDATE))
            with self.assertRaisesRegex(ValueError, "不属于当前 revision"):
                binding.require_current_revision(campaign, manifest, OTHER, action="测试")
            for name in ("invalidation.json", "superseded-by.json"):
                marker = campaign / "candidates" / CANDIDATE / name
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("{}", encoding="utf-8")
                with self.subTest(marker=name), self.assertRaisesRegex(ValueError, "只读"):
                    binding.require_current_revision(campaign, manifest, CANDIDATE, action="测试")
                marker.unlink()
            # 实现门禁的目标 revision 路径与 Codex 同一布局；实现测试不复用，early-test-mode 恒为 full。
            gates = load_driver("implementation_gates", environment)
            self.assertEqual(gates.target_revision(), claude_upgrade._candidate_revision_dir(campaign, 1) / "revision.json")
            self.assertEqual(resume.early_test_mode(self.root), "full")
        # 前序构建收据：Claude 绑定只认客户端中立合同，Codex 绑定只认 Codex 合同。
        codex_shaped = {"schema_version": "codex-upgrade-candidate-build-receipt/v1"}
        with self.assertRaises(vc_artifacts.VCArtifactError):
            client.ClaudeBinding.previous_build_receipt(binding, codex_shaped)
        # 编排器 CLI：失败退出码与非 JSON 输出都拒绝（真实子进程）。
        with mock.patch.dict(os.environ, environment), self.assertRaisesRegex(ValueError, "失败（退出码 1）"):
            client.ClaudeBinding.cli(binding, ["plan-candidate-gates", "--campaign-dir", str(self.root / "missing"), "--candidate-id", CANDIDATE])

    def test_assets_catalog_and_transition(self) -> None:
        campaign, preview = self.ready()
        take = self.take_from(campaign, preview)
        repository = self.repository()
        chain = load_driver("claude_catalog_chain", {})
        mapping = (json.dumps(self.mapping_for(preview), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        load = lambda: chain.load_take(take, campaign_id=CAMPAIGN_ID, candidate_id=CANDIDATE, target_version=TARGET)  # noqa: E731
        loaded = load()
        # 取料被改：总计划、需求预览（摘要、归属、联合摘要）都在装配之前拒绝。
        requirements_path = take / REQUIREMENTS_FILENAME
        original = requirements_path.read_bytes()
        for label, change in (
            ("需求摘要", lambda doc: doc.update(requirements_sha256="0" * 64)),
            ("候选", lambda doc: doc.update(candidate_id=OTHER)),
            ("联合摘要", lambda doc: doc["requirements"].update(joint_manifest_sha256="0" * 64)),
        ):
            document = json.loads(original)
            change(document)
            requirements_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with self.subTest(label=label), self.assertRaises(chain.ChainError):
                load()
        requirements_path.write_bytes(original)
        manifest_path = take / "campaign.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_path.write_bytes(manifest_bytes.replace(b'"formal"', b'"formal" '))
        with self.assertRaisesRegex(chain.ChainError, "campaign.sha256"):
            load()
        manifest_path.write_bytes(manifest_bytes)
        # Active 底稿：仓库当前的 release-catalog.json 必须就是 VC-3 冻结的 Active 目录。
        catalog_path = repository / candidate.RELEASE_CATALOG_RELATIVE
        active_bytes = catalog_path.read_bytes()
        catalog_path.write_bytes(active_bytes + b"\n")
        with self.assertRaisesRegex(chain.ChainError, "主线在 VC-3 之后改过"):
            chain.assemble_assets(loaded, repository, LIFECYCLE, mapping)
        catalog_path.write_bytes(active_bytes)

        # A：写入清单、落点与字节；重跑逐字复用；catalog-stage 恰好两个文件。
        assets = chain.assemble_assets(loaded, repository, LIFECYCLE, mapping)
        entry = loaded["staged"]["release_entry"]
        stage = LIFECYCLE / "catalog-stage"
        projection = candidate.PROJECTION_POLICY_TREE_DIRECTORY / f"claude_upgrade_wire_projection_{TARGET.replace('.', '_')}.json"
        self.assertEqual(assets["written"], sorted([
            (candidate.REPOSITORY_CATALOG_PARENT / entry["profile"]["path"]).as_posix(),
            (candidate.REPOSITORY_CATALOG_PARENT / entry["wire"]["path"]).as_posix(),
            projection.as_posix(), (stage / "release-entry.json").as_posix(), (stage / "release-stage-receipt.json").as_posix(),
            (LIFECYCLE / "gate-mapping.json").as_posix(), (LIFECYCLE / "gate-plan.json").as_posix(),
        ]))
        self.assertEqual((assets["release_sha256"], assets["requirements_sha256"]), (entry["release_sha256"], preview["requirements_sha256"]))
        self.assertEqual((repository / LIFECYCLE / "gate-mapping.json").read_bytes(), mapping)
        (repository / stage / "stale.json").write_text("{}", encoding="utf-8")
        again = chain.assemble_assets(loaded, repository, LIFECYCLE, mapping)
        self.assertEqual(again, assets)
        self.assertEqual(sorted(path.name for path in (repository / stage).iterdir()), sorted(candidate.CATALOG_STAGE_FILES))
        git(repository, "add", "-A")
        git(repository, "commit", "-q", "-m", "A：资产")
        a_commit = git(repository, "rev-parse", "HEAD")
        # gate-plan.json 与 plan-candidate-gates 生成模式写出的字节相同（采集主机派发后 cmp 的依据）。
        dispatched = repository / LIFECYCLE / "gate-plan-dispatch.json"
        code, result, stderr = candidate_tests.run_main(
            self.plan_argv(campaign, repository, repository / LIFECYCLE / "gate-mapping.json", dispatched)
        )
        self.assertEqual((code, result["plan_sha256"]), (0, assets["plan_sha256"]), stderr)
        self.assertEqual(dispatched.read_bytes(), (repository / LIFECYCLE / "gate-plan.json").read_bytes())
        dispatched.unlink()
        # 内容寻址文件被改：再装配即拒绝（批准后变化须建后继 Campaign）。
        profile_blob = repository / candidate.REPOSITORY_CATALOG_PARENT / entry["profile"]["path"]
        blob_bytes = profile_blob.read_bytes()
        profile_blob.write_bytes(blob_bytes + b" ")
        with self.assertRaisesRegex(chain.ChainError, "内容寻址文件已存在且内容不同"):
            chain.assemble_assets(loaded, repository, LIFECYCLE, mapping)
        profile_blob.write_bytes(blob_bytes)

        # C：Go 加载结果不符、或 verify_candidate_catalog 验收不过，都把 release-catalog.json 还原成原字节。
        def wrong_candidate(repo: Path) -> dict[str, Any]:
            view = go_view(repo, loaded)
            view["validation_candidate"]["release_sha256"] = "0" * 64
            return view

        with self.assertRaisesRegex(chain.ChainError, "validation_candidate 不是暂存 Release"):
            chain.assemble_catalog(loaded, repository, LIFECYCLE, go_check=wrong_candidate)
        self.assertEqual(catalog_path.read_bytes(), active_bytes)
        extra = repository / stage / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "两个普通文件"):
            chain.assemble_catalog(loaded, repository, LIFECYCLE, go_check=lambda repo: go_view(repo, loaded))
        self.assertEqual(catalog_path.read_bytes(), active_bytes)
        extra.unlink()
        catalog = chain.assemble_catalog(loaded, repository, LIFECYCLE, go_check=lambda repo: go_view(repo, loaded))
        expected = json.loads(active_bytes)
        expected["releases"].append(entry)
        expected["selectors"]["validation_candidate"] = loaded["staged"]["validation_candidate_selector"]
        self.assertEqual(catalog_path.read_bytes(), (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        self.assertEqual(catalog["release_catalog_sha256"], hashlib.sha256(catalog_path.read_bytes()).hexdigest())
        self.assertEqual(git(repository, "status", "--porcelain", "--untracked-files=all"), f"M {candidate.RELEASE_CATALOG_RELATIVE.as_posix()}")
        git(repository, "add", "-A")
        git(repository, "commit", "-q", "-m", "C：release-catalog.json")
        c_commit = git(repository, "rev-parse", "HEAD")

        # D：承接收据只认 release-catalog.json 一条摘要边（收据形状同 freeze-successor-generate，自摘要按 Claude 校验的算法）。
        relative = candidate.RELEASE_CATALOG_RELATIVE.as_posix()
        receipt = {
            "schema_version": "official-egress-upstream-freeze-successor/v1", "issued_at_utc": "2026-10-07T00:00:00Z",
            "base_commit": a_commit, "current_commit": c_commit, "scope": "upstream-claude-candidate-freeze-successor", "mode": "commit",
            "extra_worktree_paths": [], "frozen_path_count": 1, "frozen_edge_count": 1, "changed_path_count": 1,
            "transitions": [{
                "path": relative, "old_path": "", "status": "M",
                "predecessor_sha256s": [candidate._git_blob_sha256(repository, a_commit, relative)],
                "to_sha256": candidate._git_blob_sha256(repository, c_commit, relative), "source_receipts": [], "reason": "测试",
            }],
            "unregistered_path_count": 0, "unregistered_paths": [], "deleted_frozen_paths": [], "required_manual_actions": [],
            "verification": ["make check-egress-spec-ci"],
            "safety": {"live_account_used": False, "official_egress_profile_changed": False, "production_config_changed": False,
                       "wire_or_persona_selection_changed": False, "deployment_performed": False},
            "result": "passed_local_evidence_successor",
        }
        receipt_path = self.root / "receipt.json"

        def write_receipt(document: dict[str, Any]) -> None:
            unsigned = {key: value for key, value in document.items() if key != "identity_sha256"}
            receipt_path.write_text(json.dumps({**unsigned, "identity_sha256": candidate._fingerprint(unsigned)}, ensure_ascii=False), encoding="utf-8")

        write_receipt(receipt)
        self.assertEqual(chain.check_transition(receipt_path, repository, c_commit)["current_commit"], c_commit)
        for label, change, message in (
            ("多一条摘要边", lambda doc: doc["transitions"].append({**doc["transitions"][0], "path": "README.md"}), "恰好只有"),
            ("人工动作", lambda doc: doc.update(required_manual_actions=[{"rule_id": "x"}]), "人工动作"),
            ("结论", lambda doc: doc.update(result="manual_actions_required"), "通过结论"),
            ("未登记", lambda doc: doc.update(unregistered_path_count=1), "未登记"),
        ):
            document = json.loads(json.dumps(receipt))
            change(document)
            write_receipt(document)
            with self.subTest(label=label), self.assertRaisesRegex(chain.ChainError, message):
                chain.check_transition(receipt_path, repository, c_commit)
        write_receipt(receipt)
        with self.assertRaises(claude_upgrade.ConfigurationError):
            chain.check_transition(receipt_path, repository, a_commit)

    def driver_candidate_dir(self, fixture: build_fixture.CandidateBuildFixture) -> Path:
        """按驱动布局摆放候选目录（source／plan-source 是候选仓库的克隆，构建实物照搬夹具），供 claude_vc4 生成计划。"""

        base = self.root / "driver-candidate"
        base.mkdir()
        for name in ("source", "plan-source"):
            git(base, "clone", "-q", str(fixture.source), name)
            git(base / name, "checkout", "-q", "--detach", fixture.commit)
        shutil.copytree(fixture.build_tree, base / "build-tree")
        shutil.copytree(fixture.dist_source, base / "frontend-dist")
        artifacts = base / "artifacts"
        shutil.copytree(fixture.context, artifacts / "ctx")
        shutil.copy2(fixture.binary, artifacts / "sub2api")
        shutil.copy2(fixture.parameters_path, artifacts / "build-parameters.json")
        shutil.copy2(fixture.transition_path, artifacts / "source-transition.json")
        (artifacts / "built-at-utc.txt").write_text("2026-10-07T01:02:03Z\n", encoding="utf-8")
        return base

    def test_vc4_plans_rehearsal_preview_and_summary(self) -> None:
        campaign, preview = self.ready()
        fixture = build_fixture.CandidateBuildFixture(self.root / "build", campaign, CANDIDATE, run_main=candidate_tests.run_main)
        fixture.build_source(preview=preview)
        fixture.build_artifacts()
        fixture.build_transition()
        fixture.build_implementation_receipt(preview["requirements"], CAMPAIGN_ID)
        base = self.driver_candidate_dir(fixture)
        work = self.root / "W"
        work.mkdir(mode=0o700)
        environment = self.environment(
            campaign, B=str(base), W=str(work), C=fixture.commit, TARGET_VERSION=TARGET,
            LIFECYCLE_DIR=fixture.lifecycle_dir.relative_to(fixture.source).as_posix(), CANDIDATE_IMAGE_REPOSITORY="registry.local/sub2api",
        )
        dispatch = load_driver("claude_vc4", environment)
        client = sys.modules["vc4_client"]
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()) as printed:
            # 两个批次计划：受管 Python 直接调 claude_upgrade.py，按 VC-4 阶段幂等重派合同在构建开始前可逐字重派。
            plan = dispatch.plan_gates(work / "action-plan-vc4-plan-gates.json")
            with mock.patch.object(dispatch.subprocess, "run", return_value=fixture.runner()(["docker", "image", "inspect", build_fixture.IMAGE_ID])):
                record = dispatch.record_plan(fixture.implementation_root, work / "action-plan-vc4-record-build.json")
            for document in (plan, record):
                self.assertEqual(vc_artifacts.validate_action_plan(document, phase="VC-4"), document)
                (action,) = document["actions"]
                self.assertEqual(action["command"][:2], claude_upgrade_batch.managed_command([])[:2])
                facts = claude_upgrade._campaign_stage_replay_facts(
                    campaign, {"phase": "VC-4", "candidate_id": CANDIDATE, "candidate_revision": 1, "actions": [action]}
                )
                self.assertTrue(facts["allowed"], facts)
            self.assertEqual(json.loads((work / "action-plan-vc4-record-build.json").read_text(encoding="utf-8")), record)
            argv = record["actions"][0]["command"][2:]
            flags = dict(zip(argv[1::2], argv[2::2]))
            self.assertEqual(set(flags), set(claude_upgrade_batch.STAGE_REPLAY_CONTRACTS["VC-4"]["record-candidate-build"]["record"][0]))
            self.assertEqual(flags["--candidate-purpose"], manifest["campaign_purpose"])
            self.assertEqual(flags["--build-id"], f"{CANDIDATE}-{fixture.commit[:9]}-20261007t010203z")
            self.assertEqual(flags["--runtime-image"], build_fixture.RUNTIME_IMAGE)
            self.assertEqual(flags["--candidate-image-id"], build_fixture.IMAGE_ID)
            self.assertEqual(flags["--implementation-test-receipt"], str(fixture.implementation_root.resolve() / "receipt.json"))
            self.assertEqual(plan["actions"][0]["command"][-1], str(base / "plan-source" / environment["LIFECYCLE_DIR"] / "gate-plan-dispatch.json"))
            # plan 预演：批次外生成模式写预演文件，与提交内 gate-plan.json 逐字相同后删除；派发产物位置不受影响。
            with mock.patch.object(client.ClaudeBinding, "cli", side_effect=in_process_cli):
                dispatch.plan_rehearsal()
            self.assertIn("预演 plan 与提交内 gate-plan.json 逐字一致", printed.getvalue())
            self.assertFalse((base / "plan-source" / environment["LIFECYCLE_DIR"] / "gate-plan-rehearsal.json").exists())
            # record 零写入预览：用夹具输入的计划（构建实物可复算），写域不变才通过；预览期间写域被改即拒绝。
            argv_plan = claude_upgrade_batch.action_plan(
                [claude_upgrade_batch.managed_action("record-candidate-build", "VC-4:record-candidate-build", fixture.argv(),
                                                     item_ids=["record-candidate-build"], timeout_seconds=1800)],
                phase="VC-4",
            )
            plan_path = work / "action-plan-vc4-record-fixture.json"
            plan_path.write_text(json.dumps(argv_plan, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()), \
                    mock.patch.object(client.ClaudeBinding, "cli", side_effect=in_process_cli):
                result = dispatch.record_preview(plan_path)
            self.assertEqual((result["status"], result["candidate_id"]), ("preview", CANDIDATE))
            self.assertIn("预演零副作用", printed.getvalue())

            stray = campaign / "candidates" / CANDIDATE / "unexpected"
            created = [path for path in (stray.parent.parent, stray.parent, stray) if not path.exists()]

            def writes_while_previewing(argv: list[str]) -> tuple[int, dict[str, Any]]:
                outcome = in_process_cli(argv)
                stray.mkdir(parents=True)
                return outcome

            with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()), \
                    mock.patch.object(client.ClaudeBinding, "cli", side_effect=writes_while_previewing), \
                    self.assertRaisesRegex(ValueError, "留下了变更"):
                dispatch.record_preview(plan_path)
            for path in reversed(created):
                path.rmdir()
            with self.assertRaisesRegex(ValueError, "只接受 record-candidate-build"):
                dispatch.record_preview(work / "action-plan-vc4-plan-gates.json")
            # 正式构建完成后的收尾摘要：VC-4 checkpoint、客户端中立的候选构建收据与账本状态。
            with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
                code, _result, stderr = candidate_tests.run_main(fixture.argv())
            self.assertEqual(code, 0, stderr)
            dispatch.summary()
        lines = printed.getvalue()
        self.assertIn("VC-4 checkpoint:", lines)
        self.assertIn("'client': 'claude'", lines)
        self.assertIn("账本:", lines)
        # 前序构建收据（r≥2 只换制品时沿用 ldflags 的依据）按客户端中立合同读得出。
        receipt = json.loads((campaign / "candidates" / CANDIDATE / "build-receipt.json").read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(client.current().previous_build_receipt(receipt)["candidate_id"], CANDIDATE)


if __name__ == "__main__":
    unittest.main()
