"""R13：隔离副本中的连续 validation_only 链。

官方证据与 Catalog 使用明确的零请求夹具；复用导入、三批分类、候选制品身份、
compare、builder、checker、accept 与 deliver-candidate 均走当前工具。
Linux ARM64 上实际构建夹具 Go 二进制及 Docker 镜像；缺少环境时如实 skip，
发布认证必须把 skip 视为未认证，不能据此宣称连续链通过。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import candidate_identity_fixture as cif
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests.real_chains.test_codex_upgrade_evaluation_real_chain import _RealChainHarness


def fixture_cli(arguments):
    """只替换 Catalog 的离线产物生成器，正式 CLI 的验证与 checkpoint 写入原样执行。"""

    from tools.official_client_capture import codex_upgrade as upgrade

    original = upgrade._run_external_command

    def generate(command, **kwargs):
        if kwargs.get("operation") != "stage-profile:generator":
            return original(command, **kwargs)
        options = dict(zip(command[3::2], command[4::2]))
        output = Path(options["-output"])
        profile = driver._read(Path(options["-profile-manifest"]))
        output.mkdir(mode=0o700, parents=True)
        asset = output / "runtime-catalog.json"
        driver._write(asset, {"version": profile["codex_version"], "fixture_only": True})
        inventory = [{"path": asset.name, "sha256": driver._sha(asset), "size": asset.stat().st_size}]
        receipt = {
            "schema_version": "official-egress-catalog-stage/v1",
            "campaign_id": options["-campaign-id"],
            "classification_sha256": options["-classification-sha256"],
            "target_version": profile["codex_version"],
            "target_profile_digest": profile["profile_digest"],
            "profile_derivation_sha256": options["-profile-derivation-sha256"],
            "post_promotion_gate_requirements_sha256": options["-gate-requirements-sha256"],
            "active_unchanged": True, "production_selector_changed": False,
            "candidate_release_mode": "previous", "live_request_count": 0,
            "inventory": inventory, "inventory_sha256": upgrade._fingerprint(inventory),
        }
        driver._write(output / "catalog-stage-receipt.json", receipt)
        return argparse.Namespace(returncode=0, stdout=json.dumps(receipt), stderr="")

    with mock.patch.object(upgrade, "_run_external_command", side_effect=generate):
        return upgrade.main(arguments)


def campaign_snapshot(campaign):
    """读取 Campaign、计时账本字节和总账历史，供重复执行前后精确比较。"""

    from tools.official_client_capture import codex_upgrade as upgrade
    from tools.official_client_capture import codex_upgrade_project_ledger as project

    manifest = upgrade.load_campaign_manifest(campaign)
    ledger = upgrade._campaign_timing_ledger_dir(campaign, manifest)
    project_root = project.find_project_ledger(campaign)
    if project_root is None:
        raise RuntimeError("连续链缺少 fixture 总账")

    files = {str(path): path.read_bytes() for root in (campaign, ledger)
             for path in root.rglob("*") if path.is_file()}
    return files, project.read_project_history_snapshot(project_root)


def assert_duplicate_bootstrap_unchanged(campaign, manifest, state_dir):
    """导入的 VC-1 首批由 VC-2 引导器派发；重复引导不得产生新的执行与账本记录。"""

    from tools.official_client_capture import codex_upgrade as upgrade

    before = campaign_snapshot(campaign)
    result = upgrade._bootstrap_noop_first_batch(
        campaign, manifest, sequence=2, state_dir=state_dir,
        run_arguments=argparse.Namespace(heartbeat_seconds=0.2, watchdog_timeout_seconds=5.0, ledger_interval_seconds=0.2),
    )
    if result is not None or campaign_snapshot(campaign) != before:
        raise RuntimeError("重复引导 VC-1 首批产生了执行或改变了 Campaign／账本")


def assert_duplicate_dispatch_unchanged(namespace):
    """已提交批次重派必须在执行前拒绝，Campaign 与两个账本均不得增加或改写字节。"""

    from tools.official_client_capture import codex_upgrade as upgrade

    before = campaign_snapshot(namespace.campaign_dir)
    try:
        upgrade.compile_and_run_vc_batch(namespace)
    except upgrade.ConfigurationError:
        pass
    else:
        raise RuntimeError("已提交批次被重复派发")
    if campaign_snapshot(namespace.campaign_dir) != before:
        raise RuntimeError("重复派发改变了 Campaign 或账本字节")


def dispatch_cli(case, root, fixture, state_dir, phase, sequence, tag, arguments):
    """以正式批次派发 CLI 并保留输出，预览的退出语义仍由当前工具决定。"""

    from tools.official_client_capture import codex_upgrade as upgrade
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    campaign = Path(fixture["campaign_dir"])
    output = campaign / "control" / "vc-chain" / f"{tag}.json"
    script = (
        "import contextlib,io,sys\n"
        "from pathlib import Path\n"
        "from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import fixture_cli\n"
        "stream=io.StringIO()\n"
        "with contextlib.redirect_stdout(stream):\n"
        "    code=fixture_cli(sys.argv[2:])\n"
        "Path(sys.argv[1]).write_text(stream.getvalue(),encoding='utf-8')\n"
        "print(stream.getvalue())\n"
        "raise SystemExit(code)\n"
    )
    plan = root / "action-plans" / f"{tag}.json"
    plan.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver._write(plan, {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": [tag],
        "reuse_item_ids": [],
        "actions": [{
            "action_id": tag, "operation": f"{phase}:{arguments[0]}",
            "timeout_seconds": 120,
            "command": [sys.executable, "-c", script, str(output), *arguments],
            "item_ids": [tag],
        }],
    })
    namespace = case._vc_chain_arguments({**fixture, "state_dir": state_dir}, phase, sequence, plan)
    result, code = upgrade.compile_and_run_vc_batch(namespace)
    if code != 0:
        run_dir = Path(result["campaign_run"]["run_dir"])
        details = [driver._read(path).get("message") for path in run_dir.glob("action-diagnostics/*.json")]
        raise RuntimeError(f"连续链 {tag} 失败：{result['campaign_run']['reason']}；诊断={details}")
    payload = driver._read(output)
    assert_duplicate_dispatch_unchanged(namespace)
    driver._write(root / f"metrics-{tag}.json", {
        "phase": phase, "sequence": sequence, "execute": [tag], "reuse": [],
        "live_request_count": 0, "duplicate_dispatch_requests": 0, "result_status": payload.get("status"),
    })
    return payload


def stage_full_chain(case, root, fixture, state_dir, identity_fixture):
    """真实 stage-profile 消费零请求 Catalog producer；生成收据逐字进入候选源码树。"""

    output = root / "full-chain-catalog"
    dispatch_cli(case, root, fixture, state_dir, "VC-3", 5, "stage-profile", [
        "stage-profile", "--campaign-dir", str(fixture["campaign_dir"]), "--output", str(output),
    ])
    if identity_fixture is not None:
        for path in output.iterdir():
            shutil.copyfile(path, identity_fixture.source / "catalog" / path.name)


def classify_full_chain(case, root, fixture, state_dir, manifests):
    """连续执行草案、批准预览及批准三批，输入只在批准之前准备。"""

    from tools.official_client_capture import codex_upgrade as upgrade

    campaign = Path(fixture["campaign_dir"])
    manifest = fixture["manifest"]
    draft = dispatch_cli(case, root, fixture, state_dir, "VC-2", 2, "classify-draft", [
        "classify", "--campaign-dir", str(campaign),
    ])
    assert_duplicate_bootstrap_unchanged(campaign, manifest, state_dir)
    target, migration, scenario, profile, assertion = manifests
    migration_payload = driver._read(Path(draft["path"]) / "rule-migration.json")
    migration_payload["status"] = "approved"
    for entry in migration_payload["entries"]:
        entry["rationale"] = "隔离夹具的两条规则语义保持不变，版本字段变化不改变断言。"
    for entry in migration_payload["discovery_classifications"]:
        entry["classification"] = "change"
        entry["target_rule"] = "SPEC-EP-006"
        entry["rationale"] = "合成证据中的版本及 surface 观察差异，由固定夹具显式分类。"
    driver._write(migration, migration_payload)
    active = root / "full-chain-active-profile.json"
    active_payload = driver._read(profile)["profile_payload"].copy()
    active_payload["Version"] = manifest["baseline_version"]
    active_payload.pop("Digest", None)
    driver._write(active, active_payload)
    patches = root / "full-chain-profile-patches.json"
    driver._write(patches, {
        "schema_version": "codex-upgrade-profile-rule-patches/v1",
        "baseline_version": manifest["baseline_version"], "target_version": manifest["target_version"],
        "active_profile_sha256": driver._sha(active), "rule_patches": [],
    })
    argv = [*case._classification_arguments(campaign, manifests),
            "--active-profile", str(active), "--profile-patch-manifest", str(patches)]
    preview = dispatch_cli(case, root, fixture, state_dir, "VC-2", 3, "classify-preview", argv)
    if preview.get("status") != "approval_required":
        raise RuntimeError(f"分类预览未停在预期边界：{preview}")
    approved = dispatch_cli(case, root, fixture, state_dir, "VC-2", 4, "classify-approve", [
        *argv, "--approve-manifest-sha256", preview["joint_manifest_sha256"],
    ])
    if approved.get("status") != "complete":
        raise RuntimeError(f"分类批准未完成：{approved}")


def deliver_full_chain(arguments):
    """将已验收候选交付到 VC-6，并验证再次派发不会写账或改动封存文件。"""

    from tools.official_client_capture import codex_upgrade as upgrade
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing

    state = driver._load_state(arguments)
    root, campaign = Path(state["root"]), Path(state["campaign_dir"])
    manifest = upgrade._require_formal_campaign(campaign)
    case = driver.new_real_chain_case(full_chain=True)
    try:
        sequence = max(upgrade._committed_vc_sequences(campaign, manifest)) + 1
        result = dispatch_cli(case, root, {"campaign_dir": campaign}, Path(state["state_dir"]), "VC-6", sequence, "deliver", [
            "deliver-candidate", "--campaign-dir", str(campaign),
            "--candidate-id", state["candidate_id"], "--attempt-id", state["attempt_id"],
            "--build-receipt", str(upgrade._candidate_build_receipt_path(campaign, state["candidate_id"])),
        ])
        if result.get("vc6_status") != "complete":
            raise RuntimeError(f"VC-6 交付没有完成：{result}")
        ledger = upgrade._campaign_timing_ledger_dir(campaign, manifest)
        return {"status": "complete", "live_request_count": 0, "duplicate_dispatch_requests": 0,
                "completed_phases": timing.phase_ledger_state(ledger)["completed_phases"],
                "delivery": result}
    finally:
        case.doCleanups()


class FullValidationOnlyChainTests(unittest.TestCase):
    def test_full_validation_only_chain(self):
        from tools.official_client_capture import codex_upgrade_project_ledger as project

        if not cif.available():
            self.skipTest("连续链必须在有 Docker 和 Go 的 Linux ARM64 环境运行")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codex-full-chain-") as directory, \
             mock.patch.dict(os.environ, {project_ledger_fixture.FIXTURE_ONLY_ENV: "1"}):
            staging = Path(directory).resolve() / "staging"
            staging.mkdir(mode=0o700)
            harness = _RealChainHarness(self, staging)
            tree = harness.tree("full")
            initialized = harness.run(tree, "init", "--full-chain")
            self.addCleanup(cif.cleanup_identity, initialized["candidate_identity"])
            self.assertIsNotNone(initialized["candidate_identity"])
            harness.run(tree, "gate", "--tag", "full")
            compared = harness.run(tree, "dispatch", "--tag", "full-compare", "--actions", "compare")
            self.assertEqual(compared["returncode"], 0, compared)
            # 断言配置在比较完成后冻结，才能绑定真实 comparison_package_digest。
            evaluated = harness.run(tree, "dispatch", "--tag", "full-accept", "--actions", "assert", "accept")
            self.assertEqual(evaluated["returncode"], 0, f"{evaluated}\n{harness.last_stderr[-5000:]}\n{harness.last_stdout[-5000:]}")
            harness.assert_accepted_to_completion(0)
            delivered = harness.run(tree, "deliver-full")
            self.assertEqual(delivered["completed_phases"], [f"VC-{index}" for index in range(7)])
            self.assertEqual(delivered["live_request_count"], 0)
            self.assertEqual(delivered["duplicate_dispatch_requests"], 0)
            self.assertFalse((harness.campaign_dir() / "canonical").exists())
            ledger = Path(harness.state()["ledger"])
            self.assertIs(driver._read(ledger / "plan.json")["fixture_only"], True)
            head = project.replay_head(ledger)
            self.assertEqual((head["precise_total"], head["estimated_total"]), (0, 0))
            self.real_chain_metrics = {
                "live_request_count": head["precise_total"] + head["estimated_total"],
                "duplicate_dispatch_requests": 0,
                "seconds": round(time.monotonic() - started, 3),
                "batches": [{"phase": batch["phase"], "sequence": batch["sequence"],
                             "execute": batch["execute_item_ids"], "reuse": batch["reuse_item_ids"],
                             "live_request_count": 0}
                            for path in sorted((harness.campaign_dir() / "control/vc/batches").glob("*.json"))
                            for batch in [driver._read(path)]],
            }
            expected = [
                ("VC-1", [], ["official-core"]),
                ("VC-2", ["classify-draft"], []),
                ("VC-2", ["classify-preview"], []),
                ("VC-2", ["classify-approve"], []),
                ("VC-3", ["stage-profile"], []),
                ("VC-5", ["compare"], ["candidate-frozen-core"]),
                ("VC-5", ["acceptance", "assert-rules"], ["candidate-frozen-core"]),
                ("VC-6", ["deliver"], []),
            ]
            self.assertEqual(self.real_chain_metrics["batches"], [
                {"phase": phase, "sequence": index, "execute": execute, "reuse": reuse, "live_request_count": 0}
                for index, (phase, execute, reuse) in enumerate(expected, 1)
            ])
            self.assertLess(self.real_chain_metrics["seconds"], 900, "ARM64 连续链超过 15 分钟验收上限")
            print(json.dumps({"chain": "vc-chain.full-validation-only", **self.real_chain_metrics}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
