"""Claude 的 VC-5 驱动（Claude 升级 B5b-01b：``vc5-claude-*.sh``／``claude_vc5.py``、阶段解析的 Claude 分支与派发脚本表项）。

* 客户端表：两客户端 VC-5 各步的派发脚本（Codex 即原来四个脚本、Claude 的 canonical 为空）都在驱动里；待接入只剩边界
  核对（B4-01 f 段）与候选恢复段（B4-05b）；
* 派发脚本互斥：Codex 的 VC-5 脚本及其辅助脚本对 Claude 参数、Claude 的脚本对 Codex 参数，都在阶段解析与任何副作用之前
  拒绝（``lib.sh`` 的 ``CLIENT_STEP_REQUIRED``）；Codex 的计划生成器 ``gen_vc5_plans.py`` 对 Claude 拒绝；
* 计划与辅助：compose 改写（候选开关、隔离实例 ARM64 与激活事实变量，不声明批准摘要；备份里已有这些变量即拒绝）、门禁执行
  主机覆盖（两条本机门禁取本机门禁记录、目标平台门禁按 ``write_gate_json`` 的算法）、边界核对在待接入期间拒绝且接入后只有
  Codex final wire 一项带真实请求确认、seal 批准必须给审核摘要与批准人；
* ``vc5-all.sh`` 的暂停语义（原文件逐字节执行，替身 lib.sh 与派发脚本，暂停文件由真实 ``claude_vc5.py pause`` 写）：seal 链
  暂停即打印 VC5_ALL_PAUSED 并退出 0、不进 accept；seal 链失败仍按失败退出；重起先清掉旧暂停文件；Claude 没有 canonical 交接；
* 真实批次链（沿用 B4-06b 的输入源接缝，见 ``claude_vc5_chain_fixture``）：驱动生成的 compare、assert 与 accept 计划在父监督器里
  派发到 VC-5 checkpoint，计时账本 VC-5 完成；阶段解析的 Claude 分支在 paths／attempt 模式给出读写坐标、attempt 与 VC-5 完成，
  批次模式核对断言计划（篡改配置即拒绝）；驱动的外部门禁 subject 与 accept 期望的逐字相同，``build_gate_facts.py --subject``
  照它写 facts。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import client_table as CT
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_claude_upgrade_compare_accept as chain_tests
from tools.official_client_capture.tests.test_driver_client_table import claude_env_text

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
CODEX_STEPS = {
    "vc5_start_script": "vc5-start.sh",
    "vc5_seal_script": "vc5-seal.sh",
    "vc5_accept_script": "vc5-accept.sh",
    "vc5_canonical_script": "vc5-canonical2.sh",
}
CLAUDE_STEPS = {
    "vc5_start_script": "vc5-claude-start.sh",
    "vc5_seal_script": "vc5-claude-seal.sh",
    "vc5_accept_script": "vc5-claude-accept.sh",
    "vc5_canonical_script": None,
}


def load_vc5() -> Any:
    return driver_tests.load_script("claude_vc5")


class Vc5TableAndDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_table_entries_and_pending(self) -> None:
        self.assertEqual({key: CT.spec("codex")[key] for key in CODEX_STEPS}, CODEX_STEPS)
        self.assertEqual({key: CT.spec("claude")[key] for key in CLAUDE_STEPS}, CLAUDE_STEPS)
        for script in [*CODEX_STEPS.values(), *(value for value in CLAUDE_STEPS.values() if value), "vc5-claude-switch.sh"]:
            with self.subTest(script=script):
                self.assertTrue((DRIVER / script).is_file())
                self.assertEqual(os.stat(DRIVER / script).st_mode & 0o111, 0o111)
        self.assertEqual(sorted(CT.TABLE["claude"]["pending"]), ["tool:vc5-boundary-check", "tool:vc5-recover"])
        self.assertEqual(CT.TABLE["codex"]["pending"], {})
        shell = subprocess.run([sys.executable, str(DRIVER / "client_table.py"), "shell"], env={**os.environ, "CLIENT": "claude"},
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("CT_VC5_CANONICAL_SCRIPT=''", shell)
        self.assertIn("CT_PENDING_TOOL_VC5_BOUNDARY_CHECK=", shell)
        self.assertNotIn("CT_PENDING_TOOL_VC5_RUN", shell)

    def run_script(self, script: str, client: str, *args: str) -> subprocess.CompletedProcess[str]:
        fixture = driver_tests._DriverFixture(self.root / f"fixture-{client}-{script}-{len(args)}")
        if client == "claude":
            fixture.env_file.write_text(claude_env_text(fixture), encoding="utf-8")
        return subprocess.run(["bash", str(DRIVER / script), *args], env={**os.environ, **fixture.env}, capture_output=True, text=True,
                              cwd=str(self.root))

    def test_codex_scripts_refuse_claude_before_side_effects(self) -> None:
        cases = (
            ("vc5-start.sh", (), "vc5-claude-start.sh"),
            ("vc5-switch.sh", ("candidate", "tag", "sha256:" + "a" * 64, "b" * 64, "build"), "vc5-claude-start.sh"),
            ("vc5-precheck.sh", ("sha256:" + "a" * 64, "build"), "vc5-claude-start.sh"),
            ("vc5-seal.sh", ("attempt-1",), "vc5-claude-seal.sh"),
            ("vc5-kilo.sh", ("attempt-1",), "vc5-claude-seal.sh"),
            ("vc5-seal-receipts.sh", ("attempt-1", "2026-10-08T00:00:00Z"), "vc5-claude-seal.sh"),
            ("vc5-accept.sh", ("attempt-1",), "vc5-claude-accept.sh"),
            ("vc5-canonical2.sh", ("attempt-1",), "（无，本客户端没有这一步）"),
        )
        for script, args, owner in cases:
            with self.subTest(script=script):
                completed = self.run_script(script, "claude", *args)
                self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
                self.assertIn(f"由 {owner} 负责", completed.stdout)
                self.assertNotIn("阶段解析拒绝", completed.stdout + completed.stderr)
        completed = self.run_script("vc5-recover.sh", "claude")
        self.assertEqual(completed.returncode, 3)
        self.assertIn("尚未接入：B4-05b", completed.stdout)

    def test_claude_scripts_refuse_codex(self) -> None:
        for script, args, owner in (
            ("vc5-claude-start.sh", (), "vc5-start.sh"),
            ("vc5-claude-switch.sh", ("candidate", "tag"), "vc5-start.sh"),
            ("vc5-claude-seal.sh", ("attempt-1",), "vc5-seal.sh"),
            ("vc5-claude-accept.sh", ("attempt-1",), "vc5-accept.sh"),
        ):
            with self.subTest(script=script):
                completed = self.run_script(script, "codex", *args)
                self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
                self.assertIn(f"由 {owner} 负责", completed.stdout)

    def test_codex_plan_generator_refuses_claude(self) -> None:
        fixture = driver_tests._DriverFixture(self.root / "gen")
        fixture.env_file.write_text(claude_env_text(fixture), encoding="utf-8")
        completed = subprocess.run([sys.executable, str(DRIVER / "gen_vc5_plans.py"), str(self.root), "c", "cand", "sha256:" + "a" * 64, "build"],
                                   env={**os.environ, **fixture.env}, capture_output=True, text=True, cwd=str(DRIVER))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("由 vc5-claude-start.sh 派发", completed.stderr)


class ClaudeVc5HelperTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.module = load_vc5()
        self.receipt = {
            "target_version": "2.1.285",
            "profile": {"profile_digest": "1" * 64, "release_sha256": "2" * 64, "wire_sha256": "3" * 64, "bundle_sha256": "4" * 64},
            "image": {"image_id": "sha256:" + "5" * 64, "reference": "registry/sub2api@sha256:" + "6" * 64},
            "source": {"tree_sha256": "7" * 64},
            "build": {"build_id": "build-1"},
            "deployed_version": "2.1.285",
        }

    def env(self, client: str = "claude", **extra: str) -> Any:
        values = {"CLIENT": client, "W": str(self.root), "NEWDIR": str(self.root / "campaign"), "CAND": "cand-1", **extra}
        return mock.patch.dict(os.environ, values)

    def compose(self, extra: str = "") -> str:
        return (
            "services:\n  sub2api:\n    image: ghcr.io/x:1\n    container_name: sub2apiplus\n    environment:\n"
            f"      - AUTO_SETUP=true\n{extra}      - SERVER_PORT=8080\n"
        )

    def test_compose_rewrite_writes_candidate_switch_isolated_instance_and_activation_identity(self) -> None:
        environment = self.module.candidate_environment(self.receipt, "cand-1")
        text = self.module.rewrite_compose(self.compose(), production_image="ghcr.io/x:1", tag="sub2apiplus-x-candidate:r-abc", environment=environment)
        self.assertIn("    image: sub2apiplus-x-candidate:r-abc\n    container_name: sub2apiplus\n", text)
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith("- GATEWAY_")]
        self.assertEqual(
            lines,
            [
                "- GATEWAY_CLAUDE_FW_G_CANDIDATE_ENABLED=true",
                "- GATEWAY_OFFICIAL_EGRESS_GUARD_INSTANCE_ID=ARM64",
                "- GATEWAY_CLAUDE_EGRESS_ACTIVATION_FACT_PATH=/app/data/cand-1-claude-activation-fact.json",
                f"- GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST={'1' * 64}",
                f"- GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST={'2' * 64}",
                f"- GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_ID=sha256:{'5' * 64}",
                f"- GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_REFERENCE=registry/sub2api@sha256:{'6' * 64}",
                f"- GATEWAY_CLAUDE_EGRESS_ACTIVATION_SOURCE_TREE_SHA256={'7' * 64}",
                "- GATEWAY_CLAUDE_EGRESS_ACTIVATION_BUILD_ID=build-1",
                "- GATEWAY_CLAUDE_EGRESS_ACTIVATION_DEPLOYED_VERSION=2.1.285",
            ],
        )
        self.assertNotIn("APPROVAL_DIGEST", text)
        for extra, message in (
            ("      - GATEWAY_OFFICIAL_EGRESS_GUARD_INSTANCE_ID=BWG\n", "已经有候选或实例变量"),
            ("      - GATEWAY_CLAUDE_FW_G_CANDIDATE_ENABLED=true\n", "已经有候选或实例变量"),
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.module.rewrite_compose(self.compose(extra), production_image="ghcr.io/x:1", tag="t", environment=environment)
        with self.assertRaisesRegex(ValueError, "生产镜像行"):
            self.module.rewrite_compose(self.compose(), production_image="ghcr.io/other:1", tag="t", environment=environment)

    def test_gate_overrides_follow_where_each_gate_runs(self) -> None:
        local = self.root / "gates" / "local"
        local.mkdir(parents=True)
        for gate_id in ("check-egress-spec", "full-regression"):
            (local / f"{gate_id}.gate.json").write_text(json.dumps({"host": "operator-mac", "architecture": "darwin/arm64"}), encoding="utf-8")
        with self.env(GATE_ROOT=str(self.root / "gates")), mock.patch.object(self.module.socket, "gethostname", return_value="arm64-runner.lan"), \
                mock.patch.object(self.module.platform, "system", return_value="Linux"), mock.patch.object(self.module.platform, "machine", return_value="aarch64"):
            overrides = self.module.gate_overrides()
        self.assertEqual(
            overrides,
            {
                "check-egress-spec": {"host_id": "operator-mac", "architecture": "darwin/arm64"},
                "full-regression": {"host_id": "operator-mac", "architecture": "darwin/arm64"},
                "target-platform": {"host_id": "arm64-runner", "architecture": "linux/aarch64"},
            },
        )
        # 目标平台门禁按采集主机当时的系统现算（换一种系统也跟着变，不是写死的值）；算法与 lib.sh 的 write_gate_json 同一行。
        with self.env(GATE_ROOT=str(self.root / "gates")), mock.patch.object(self.module.socket, "gethostname", return_value="mac-mini"), \
                mock.patch.object(self.module.platform, "system", return_value="Darwin"), mock.patch.object(self.module.platform, "machine", return_value="arm64"):
            self.assertEqual(self.module.gate_overrides()["target-platform"], {"host_id": "mac-mini", "architecture": "darwin/arm64"})
        self.assertIn(
            '"host": socket.gethostname().split(".")[0], "architecture": f"{platform.system().lower()}/{platform.machine()}"',
            (DRIVER / "lib.sh").read_text(encoding="utf-8"),
        )
        (local / "full-regression.gate.json").unlink()
        with self.env(GATE_ROOT=str(self.root / "gates")), self.assertRaisesRegex(ValueError, "本机门禁记录还没注入"):
            self.module.gate_overrides()

    def test_boundary_check_is_pending_then_only_codex_final_wire_acknowledges_live_requests(self) -> None:
        with self.env(ATT="attempt-1"), self.assertRaisesRegex(ValueError, "边界核对尚未接入：B4-01 f 段"):
            self.module.boundary_plan("codex-final-wire-isolation", self.root / "plan.json")
        row = dict(CT.spec("claude"))
        row["pending"] = {}
        with self.env(ATT="attempt-1"), mock.patch.object(self.module, "_row", return_value=row), contextlib.redirect_stdout(io.StringIO()):
            live = self.module.boundary_plan("codex-final-wire-isolation", self.root / "live.json")
            quiet = self.module.boundary_plan("third-party-ingress-rejection", self.root / "quiet.json")
            with self.assertRaisesRegex(ValueError, "未知边界核对"):
                self.module.boundary_plan("kilo", self.root / "x.json")
        for plan, check, live_flag in ((live, "codex-final-wire-isolation", True), (quiet, "third-party-ingress-rejection", False)):
            [action] = plan["actions"]
            self.assertEqual((action["action_id"], action["item_ids"]), (f"candidate-boundary-{check}", ["candidate-seal"]))
            self.assertEqual(action["command"][2:], ["capture-candidate", "boundary-check", "--campaign-dir", str(self.root / "campaign"),
                                                     "--candidate-id", "cand-1", "--attempt-id", "attempt-1", "--check", check,
                                                     *(["--acknowledge-live-requests"] if live_flag else [])])

    def test_seal_approval_needs_reviewed_digest_and_approver(self) -> None:
        files = {"client_bindings": {"claude-desktop": self.root / "d.json"}, "boundary_checks": {"third-party-ingress-rejection": self.root / "b.json"}}
        with self.env(ATT="attempt-1"), mock.patch.object(self.module, "_client_files", return_value=files), \
                mock.patch.object(self.module, "gate_overrides", return_value={"target-platform": {"host_id": "h", "architecture": "linux/aarch64"}}), \
                contextlib.redirect_stdout(io.StringIO()):
            for approve, approver in ((None, "reviewer-a"), ("0" * 64, None), ("not-a-digest", "reviewer-a"), ("0" * 64, "  ")):
                with self.subTest(approve=approve, approver=approver), self.assertRaisesRegex(ValueError, "驱动不替审核人批准"):
                    self.module.seal_plan("approve", self.root / "a.json", approve=approve, approved_by=approver)
            with self.assertRaisesRegex(ValueError, "不接受批准参数"):
                self.module.seal_plan("preview", self.root / "p.json", approve="0" * 64)
            plan = self.module.seal_plan("approve", self.root / "a.json", approve="a" * 64, approved_by="reviewer-a")
        self.assertEqual([(item["action_id"], item["item_ids"]) for item in plan["actions"]],
                         [("candidate-seal-approve", ["candidate-seal"]), ("compare", ["compare"])])
        argv = plan["actions"][0]["command"][2:]
        self.assertIn("--gate-execution-overrides", argv)
        self.assertEqual(argv[argv.index("--approve-seal-sha256") + 1:], ["a" * 64, "--approved-by", "reviewer-a"])
        self.assertIn(f"claude-desktop={self.root / 'd.json'}", argv)
        missing = {"client_bindings": {"claude-desktop": None}, "boundary_checks": {}}
        with self.env(ATT="attempt-1"), mock.patch.object(self.module, "_client_files", return_value=missing), \
                self.assertRaisesRegex(ValueError, "还没就位：claude-desktop"):
            self.module.seal_plan("preview", self.root / "p.json")

    def test_helper_refuses_codex(self) -> None:
        with self.env("codex"), self.assertRaisesRegex(ValueError, "VC-5 派发脚本是 vc5-start.sh"):
            self.module.run_plan(self.root / "plan.json")
        self.assertFalse((self.root / "plan.json").exists())


# vc5-all.sh 暂停语义用的替身 lib.sh：只给 vc5-all.sh 用到的变量与函数（Claude 的四个派发表项，canonical 为空）；阶段解析按
# RUNROOT 下的标记文件给出 compare／accept 是否就绪。vc5-all.sh 本身逐字节拷进同一目录执行。
STUB_LIB = """DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUNROOT=$STUB_RUNROOT; GATE_ROOT=$STUB_RUNROOT/gates; D=$STUB_RUNROOT; NEW=c; NEWDIR=$STUB_RUNROOT/campaign; DC=dc; B=$STUB_RUNROOT/b
CT_CLIENT=claude; CT_VC5_START_SCRIPT=vc5-claude-start.sh; CT_VC5_SEAL_SCRIPT=vc5-claude-seal.sh
CT_VC5_ACCEPT_SCRIPT=vc5-claude-accept.sh; CT_VC5_CANONICAL_SCRIPT=
vc5_require_admission() { :; }
utc_now() { echo 2026-10-08T00:00:00Z; }
wait_budget() { echo 60; }
wait_for_marker() { :; }
phase_require() {
  ATT=attempt-1; ATT_STATUS=awaiting_receipts; EVALUATION_BASELINE=0; VC5_CHECKPOINT=ckpt; VC5_RECEIPT=
  COMPARE_READY=0; ACCEPT_READY=0
  if [ -f "$RUNROOT/compared" ]; then COMPARE_READY=1; fi
  if [ -f "$RUNROOT/accepted" ]; then ACCEPT_READY=1; fi
}
"""
# 替身 seal 链：STUB_PAUSE 非空时经真实 claude_vc5.py pause 写暂停文件后退出 0；STUB_FAIL 非空时退出 3；否则写 compare 就绪标记。
STUB_SEAL = """#!/bin/bash
set -e
echo "=== 替身 seal 链"
if [ -n "${STUB_FAIL:-}" ]; then echo "SEAL_ABORT 替身失败"; exit 3; fi
if [ -n "${STUB_PAUSE:-}" ]; then python3 "$REAL_DRIVER/claude_vc5.py" pause --reason "$STUB_PAUSE" --next "处置后重起 vc5-all.sh"; exit 0; fi
touch "$STUB_RUNROOT/compared"; echo "SEAL_DONE"
"""
STUB_ACCEPT = """#!/bin/bash
set -e
echo "=== 替身 accept"; touch "$STUB_RUNROOT/accepted"; echo "ACCEPT_DONE"
"""


class Vc5AllPauseTests(unittest.TestCase):
    """``vc5-all.sh``（逐字节原文件，替身 lib.sh 与派发脚本）：seal 链写暂停文件即打印 VC5_ALL_PAUSED 并退出 0、不进 accept；
    重起时先清掉上一次的暂停文件再走 seal 链；seal 链失败仍按失败退出；Claude 没有 canonical 交接。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.drv, self.runroot = self.root / "drv", self.root / "runroot"
        for path in (self.drv, self.runroot / "gates" / "local", self.runroot / "campaign" / "control" / "vc"):
            path.mkdir(parents=True)
        (self.drv / "vc5-all.sh").write_bytes((DRIVER / "vc5-all.sh").read_bytes())
        (self.drv / "lib.sh").write_text(STUB_LIB, encoding="utf-8")
        for name, text in (("vc5-claude-seal.sh", STUB_SEAL), ("vc5-claude-accept.sh", STUB_ACCEPT)):
            (self.drv / name).write_text(text, encoding="utf-8")
            (self.drv / name).chmod(0o700)
        (self.runroot / "gates" / "local" / "full-regression.gate.json").write_text("{}\n", encoding="utf-8")
        (self.runroot / "vc5-run-batch.out").write_text("actions: 1\nRUN_BATCH_DONE 2026-10-08T00:00:00Z\n", encoding="utf-8")

    def run_all(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
            "STUB_RUNROOT": str(self.runroot), "RUNROOT": str(self.runroot), "REAL_DRIVER": str(DRIVER), "CLIENT": "claude", **extra,
        }
        return subprocess.run(["bash", str(self.drv / "vc5-all.sh")], cwd=self.root, env=env, capture_output=True, text=True, timeout=120)

    def test_seal_pause_stops_with_exit_zero_and_restart_clears_it(self) -> None:
        pause = self.runroot / "vc5-pause.json"
        paused = self.run_all(STUB_PAUSE="seal_approval_required")
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        self.assertIn("VC5_ALL_PAUSED reason=seal_approval_required next=处置后重起 vc5-all.sh", paused.stdout)
        self.assertNotIn("VC5_ALL_DONE", paused.stdout)
        self.assertEqual(json.loads(pause.read_text(encoding="utf-8")), {"reason": "seal_approval_required", "next": "处置后重起 vc5-all.sh"})
        self.assertEqual(pause.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.runroot / "accepted").exists())
        # seal 链失败：不是暂停，按失败退出（暂停文件先被清掉，不会把失败误报成暂停）。
        failed = self.run_all(STUB_FAIL="1")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertNotIn("VC5_ALL_PAUSED", failed.stdout)
        self.assertFalse(pause.exists())
        # 处置后重起：seal 链走完，accept 完成 VC-5，Claude 没有 canonical 交接。
        pause.write_text(json.dumps({"reason": "client_receipts_missing", "next": "旧的"}) + "\n", encoding="utf-8")
        done = self.run_all()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(pause.exists())
        self.assertNotIn("VC5_ALL_PAUSED", done.stdout)
        self.assertIn("本轮客户端（claude）没有 canonical 交接", done.stdout)
        self.assertIn("VC5_ALL_DONE ATT=attempt-1", done.stdout)
        self.assertTrue((self.runroot / "accepted").exists())


@unittest.skipUnless(site.ENABLE_USER_SITE, "VC-5 动作子进程里的输入源接缝经 usercustomize 注入，要求启用用户 site")
class ClaudeVc5DriverChainTests(chain_tests.CompareAcceptChainTests):
    """驱动生成的计划在真实批次链上走 compare → assert → accept 到 VC-5 checkpoint；阶段解析的 Claude 分支读同一个世界。"""

    # 父类的用例由它自己的模块跑；这里只跑本类新增的用例。
    test_pass_chain_reaches_ready_and_writes_vc5_checkpoint = None  # type: ignore[assignment]
    test_failing_candidate_assert_batch_fails_and_accept_refuses_without_writes = None  # type: ignore[assignment]
    test_failed_external_gate_records_round_without_accept_result = None  # type: ignore[assignment]
    test_zero_seam_compare_freezes_real_profile_and_fails_closed_at_official_source = None  # type: ignore[assignment]

    def driver_env(self, world: dict[str, Any], **extra: str) -> dict[str, str]:
        campaign = world["campaign"]
        inputs = self.root / "driver-inputs"
        inputs.mkdir(exist_ok=True)
        return {
            "CLIENT": "claude", "D": str(compare.data_root(campaign)), "NEWDIR": str(campaign), "CAND": chain_tests.CANDIDATE,
            "W": str(inputs), "GATE_ROOT": str(world["gate_root"]), "CANDIDATE_REVISION": "1",
            "TOOLS": str(REPO_ROOT / "tools" / "official_client_capture"), **extra,
        }

    def plan(self, world: dict[str, Any], name: str, **extra: str) -> Path:
        module = load_vc5()
        output = Path(self.driver_env(world)["W"]) / f"action-plan-vc5-{name}.json"
        with mock.patch.dict(os.environ, self.driver_env(world, **extra)), contextlib.redirect_stdout(io.StringIO()):
            getattr(module, f"{name}_plan")(output)
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
        return output

    def dispatch_plan(self, world: dict[str, Any], sequence: int, plan_path: Path) -> tuple[int, dict | None, str]:
        actions = [str(item["action_id"]) for item in json.loads(plan_path.read_text(encoding="utf-8"))["actions"]]
        environment = chain.subprocess_environment(self.root / "inject", world["config_path"], actions)
        with mock.patch.dict(os.environ, environment):
            return chain_tests.run_main(
                [
                    "compile-and-run-vc-batch", "--campaign-dir", world["campaign"], "--phase", "VC-5", "--sequence", sequence,
                    "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(world["campaign"], "VC-4", revision=1),
                    "--action-plan", plan_path, "--state-dir", self.state_dir,
                ]
            )

    def phase(self, world: dict[str, Any], *, mode: str, **options: Any) -> dict[str, Any]:
        """阶段解析的 Claude 分支（受管编排器即进程内模块；根解析换成给定参数、候选源码目录名换成批次世界的：批次世界的账本与
        候选目录不在驱动的规范坐标上）。"""

        phase_context = driver_tests.load_script("phase_context")
        campaign, manifest = world["campaign"], world["manifest"]
        build = world["build"]
        config = {
            "CLIENT": "claude", "D": str(compare.data_root(campaign)), "NEW": str(manifest["campaign_id"]), "CAND": chain_tests.CANDIDATE,
            "TARGET_VERSION": str(manifest["target_version"]), "BASELINE_VERSION": str(manifest["baseline_version"]),
            "PROFILE_ID": f"claude-code-{manifest['target_version']}-official", "C": str(build["source"]["git_commit"]),
            "B": str(Path(build["source"]["root"]).parent), "AS": str(self.root / "assertion-config"), "G": str(self.root / "gates-root"),
            "W": str(self.root / "driver-inputs"), "IN": "inputs",
        }
        roots = {"parameters": {"NEWDIR": str(campaign)}, "bindings": {}, "candidate_revision": 1}
        with chain.seams(world["config_path"]), mock.patch.object(phase_context.round_context, "resolve", return_value=roots), \
                mock.patch.object(phase_context.ClaudeContext, "SOURCE_DIRNAME", Path(build["source"]["root"]).name):
            report = phase_context.resolve(config, mode=mode, upgrade=claude_upgrade, **options)
        return report["parameters"]

    def test_driver_plans_reach_vc5_checkpoint_and_phase_context_reads_the_world(self) -> None:
        world = self.vc5_world()
        campaign = world["campaign"]
        # 封存之后、compare 之前：阶段解析给出读写坐标，capture 已就绪、compare 未就绪。
        params = self.phase(world, mode="paths")
        self.assertEqual((params["CAPTURE_READY"], params["COMPARE_READY"], params["ACCEPT_READY"]), ("1", "0", "0"))
        self.assertEqual(params["PROFILE_ID"], f"claude-code-{world['manifest']['target_version']}-official")
        self.assertEqual(params["BUILD_REPLAY_STATE"], "sealed_receipt_binding")
        # compare（驱动计划）。
        self.assert_batch_passed(self.dispatch_plan(world, 9, self.plan(world, "compare")), 9, "VC-5")
        params = self.phase(world, mode="attempt")
        self.assertEqual((params["COMPARE_READY"], params["ATT"], params["VC5_COMPLETE"], params["VC5_RECEIPT"]), ("1", chain_tests.ATTEMPT_ID, "0", ""))
        # assert（驱动计划取比较收据登记的 builder 配置）；批次模式核对计划，篡改配置即拒绝。
        assert_path = self.plan(world, "assert")
        plan = json.loads(assert_path.read_text(encoding="utf-8"))
        comparison = claude_upgrade._load_stage_result(campaign, "compare", chain_tests.CANDIDATE)
        self.assertIn(str(campaign / comparison["builder"]["config"]["path"]), plan["actions"][0]["command"])
        tampered = json.loads(json.dumps(plan))
        tampered["actions"][0]["command"][tampered["actions"][0]["command"].index("--config") + 1] = str(self.root / "other.json")
        self.check_batch_plan(world, plan, accepted=True)
        self.check_batch_plan(world, tampered, accepted=False)
        self.assert_batch_passed(self.dispatch_plan(world, 10, assert_path), 10, "VC-5")
        # 外部门禁 subject：驱动生成的与 accept 期望的逐字相同；build_gate_facts --subject 照它写 facts。
        module = load_vc5()
        with mock.patch.dict(os.environ, self.driver_env(world)):
            subject = module.gate_subject()
        identity = compare.candidate_gate_identity(campaign, world["manifest"], chain_tests.CANDIDATE, world["build"])
        expected = chain.expected_subject(world["manifest"], world["candidate_payload"], identity)
        self.assertEqual(subject, {**expected, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None})
        receipt = self.gate_receipt(world, "candidate-gates.receipt.json")
        # accept（驱动计划）：VC-5 checkpoint 写出，计时账本 VC-5 完成，阶段解析给出 VC-5 完成。
        accept_path = self.plan(world, "accept")
        self.assertIn(receipt, json.loads(accept_path.read_text(encoding="utf-8"))["actions"][0]["command"])
        result = self.assert_batch_passed(self.dispatch_plan(world, 11, accept_path), 11, "VC-5")
        self.assertEqual(result["timing_ledger"]["completion"]["idempotent"], False)
        self.assertIn("VC-5", timing.phase_ledger_state(self.ledger_dir)["completed_phases"])
        params = self.phase(world, mode="attempt", require_completion="VC-5")
        self.assertEqual((params["ACCEPT_READY"], params["ASSERTIONS_READY"], params["VC5_COMPLETE"]), ("1", "1", "1"))
        self.assertEqual(params["VC5_CHECKPOINT"], str(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1)))
        self.assert_zero_requests()

    def check_batch_plan(self, world: dict[str, Any], plan: dict[str, Any], *, accepted: bool) -> None:
        phase_context = driver_tests.load_script("phase_context")
        campaign, manifest = world["campaign"], world["manifest"]
        build = world["build"]
        config = {
            "CLIENT": "claude", "D": str(compare.data_root(campaign)), "NEW": str(manifest["campaign_id"]), "CAND": chain_tests.CANDIDATE,
            "TARGET_VERSION": str(manifest["target_version"]), "BASELINE_VERSION": str(manifest["baseline_version"]),
            "PROFILE_ID": f"claude-code-{manifest['target_version']}-official", "C": str(build["source"]["git_commit"]),
            "B": str(Path(build["source"]["root"]).parent), "AS": str(self.root / "assertion-config"), "G": str(self.root / "gates-root"),
            "W": str(self.root / "driver-inputs"), "IN": "inputs",
        }
        roots = {"parameters": {"NEWDIR": str(campaign)}, "bindings": {}, "candidate_revision": 1}
        with chain.seams(world["config_path"]), mock.patch.object(phase_context.round_context, "resolve", return_value=roots), \
                mock.patch.object(phase_context.ClaudeContext, "SOURCE_DIRNAME", Path(build["source"]["root"]).name):
            context = phase_context.ClaudeContext(config, upgrade=claude_upgrade)
            context.stage_paths(replay=True)
            context.build(materialized=False)
            if accepted:
                phase_context._claude_vc5_plan(context, plan)
            else:
                with self.assertRaisesRegex(ValueError, "不是比较收据登记的那一份"):
                    phase_context._claude_vc5_plan(context, plan)


if __name__ == "__main__":
    unittest.main()
