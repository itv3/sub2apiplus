"""第 18 项：用 0.163 真实 Campaign 的 VC-1／VC-5 失败现场重放恢复计划。

数据：``sub2apiplus-evidence/codex-cli/0.163.0/final/*.tar.gz``（0.163 升级 Campaign 的完整归档，环境变量
``RECOVERY_PLAN_EVIDENCE_TAR`` 可指定别处）。归档里没有的机器（CI、其它开发机）上本模块整体跳过，不影响门禁。

两个现场都取"同根因达上限、账本停线，人工 campaign-resume 后出恢复预览、批准、授权、派发后继 attempt"的真实一轮：

* **VC-1（官方首批）**：attempt ``20261021T000304Z-bbeccafa79311065``（18 秒即环境污染）。真实记录：campaign-resume 批准收据
  ``resume-07b53a5b…``（00:09:25）→ 恢复预览 #1（00:09:44：只读复用 30 个、重采 17 个）→ 批准收据 #1 → 后继 attempt
  ``20261021T001412Z-aab33653057774c8``（00:14:12）。
* **VC-5（候选采集）**：attempt ``20261022T021856Z-7a0bd7c67d2f06ca``。真实记录：campaign-resume 批准收据 ``resume-7b48e12e…``
  （06:25:18）→ 恢复预览 #1（06:25:31：重采 10 个，工具演进 #11）→ 批准收据 #1 → 后继 attempt ``20261022T085520Z-60fd5f255335c599``。

受管工具在本机（非 root、非采集主机）跑不起来，所以现场分两层：

1. 现场文件（attempt.json、对账收据、恢复预览、批准收据、campaign-resume 收据、环境隔离收据）是真实归档里的原字节；
   失败时刻之前已有的历史原样放进假数据根，失败之后才产生的文件由受管工具替身在对应命令执行时按原字节写出；
2. 恢复计划本身（参数、判定、批准校验、执行、打断重入）是真代码。

所以这里验证的是：计划能读懂真实记录的形状，保留／重采清单与真实预览逐项一致，批准以真实摘要为对象，一条命令走到续跑，
打断后再执行结果相同。受管命令本身的行为（能否真的恢复）只能在 ARM64 上验证——见交付里的 ARM64 验收步骤。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import recovery_plan_world as rw
from tools.official_client_capture.tests.test_arm64_driver_recovery_plan import EXIT_OK, EXIT_OPERATOR, _WorldCase

EVIDENCE_GLOB = "/Users/czs/Developer/sub2apiplus-evidence/codex-cli/0.163.0/final/codex-0.163.0-c01630-formal-vc1-r2-*.tar.gz"
WANTED = re.compile(
    r"^[^/]+/(official/attempts/[^/]+/attempt\.json"
    r"|candidates/[^/]+/attempts/[^/]+/attempt\.json"
    r"|control/reconciliation/attempt-[^/]+/(attempt-reconciliation|recovery-preview-\d+|recovery-approval-\d+)\.json"
    r"|control/resume/resume-[0-9a-f]{64}\.json"
    r"|control/environment/isolation-\d+\.json)$"
)

VC1_ATTEMPT = "20261021T000304Z-bbeccafa79311065"
VC1_SUCCESSOR = "20261021T001412Z-aab33653057774c8"
VC1_RESUME = "07b53a5be331dd7e6609079afccdd3a265086587f6925bb55a39a5123621c08f"
VC5_ATTEMPT = "20261022T021856Z-7a0bd7c67d2f06ca"
VC5_SUCCESSOR = "20261022T085520Z-60fd5f255335c599"
VC5_RESUME = "7b48e12ea333d9192db509b0eb0cc00f743e495ba46cfe395c62bc959be57c28"


def _tarball() -> Path | None:
    override = os.environ.get("RECOVERY_PLAN_EVIDENCE_TAR")
    candidates = [override] if override else sorted(glob.glob(EVIDENCE_GLOB))
    for item in candidates:
        if item and Path(item).is_file():
            return Path(item)
    return None


_EXTRACTED: dict[str, Path] = {}


def evidence_root() -> Path:
    """只抽取恢复计划会读的那几类小 JSON（几十 MB 的轨迹与日志不碰）；同一进程只抽一次。"""

    if "root" in _EXTRACTED:
        return _EXTRACTED["root"]
    tar_path = _tarball()
    if tar_path is None:
        raise unittest.SkipTest("本机没有 0.163 Campaign 归档（sub2apiplus-evidence），跳过真实现场重放")
    directory = Path(tempfile.mkdtemp(prefix="recovery-plan-evidence-"))
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive:
            if member.isfile() and WANTED.match(member.name):
                target = directory / member.name.split("/", 1)[1]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    _EXTRACTED["root"] = directory
    return directory


def tearDownModule() -> None:
    root = _EXTRACTED.pop("root", None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RealScene(rw.World):
    """真实现场的失败时刻：目标 attempt 与它之前的历史是归档原字节，之后的记录由替身按原字节写出。"""

    def __init__(self, root: Path, evidence: Path, *, phase: str, attempt: str, successor: str, resume: str,
                 evolution_registered: bool, effective_index: int = 7) -> None:
        super().__init__(root, phase=phase, ledger_status="stop_required", evolution_registered=evolution_registered)
        self.evidence = evidence
        shutil.rmtree(self.attempts_dir() / self.attempt)  # 去掉基类造的合成 attempt
        self.attempt = attempt
        self.successor_name = successor
        self.real_attempts = sorted(
            path.parent.name for path in (evidence.glob("official/attempts/*/attempt.json") if phase == "official"
                                          else evidence.glob("candidates/*/attempts/*/attempt.json")))
        # 失败时刻之前的历史：同阶段 id 不晚于目标的 attempt.json、更早 attempt 的对账记录、更早的环境隔离。
        for name in self.real_attempts:
            if name <= attempt:
                source = next(evidence.glob(f"official/attempts/{name}/attempt.json") if phase == "official"
                              else evidence.glob(f"candidates/*/attempts/{name}/attempt.json"))
                rw.write(self.attempts_dir() / name / "attempt.json", source.read_text(encoding="utf-8"))
        for directory in sorted((evidence / "control" / "reconciliation").glob("attempt-*")):
            name = directory.name[len("attempt-"):]
            if name < attempt:
                for item in directory.glob("*.json"):
                    rw.write(self.campaign_dir / "control/reconciliation" / directory.name / item.name, item.read_text(encoding="utf-8"))
        started = _json(self.attempts_dir() / attempt / "attempt.json")["started_at_utc"]
        for item in sorted((evidence / "control" / "environment").glob("isolation-*.json")):
            if _json(item)["approved_at_utc"] < started:
                rw.write(self.campaign_dir / "control/environment" / item.name, item.read_text(encoding="utf-8"))
        # 失败之后才产生、由替身按原字节写出的记录。
        recorded = root / "recorded"
        recorded.mkdir()
        reconciliation = evidence / "control" / "reconciliation" / f"attempt-{attempt}"
        for name in ("attempt-reconciliation.json", "recovery-preview-01.json", "recovery-approval-01.json"):
            shutil.copyfile(reconciliation / name, recorded / name)
        shutil.copyfile(evidence / "control" / "resume" / f"resume-{resume}.json", recorded / f"resume-{resume}.json")
        successor_json = next(evidence.glob(f"official/attempts/{successor}/attempt.json") if phase == "official"
                              else evidence.glob(f"candidates/*/attempts/{successor}/attempt.json"))
        shutil.copyfile(successor_json, recorded / "successor-attempt.json")
        self.successor_source = str(recorded / "successor-attempt.json")
        self.real_preview = _json(recorded / "recovery-preview-01.json")
        self.real_approval = _json(recorded / "recovery-approval-01.json")
        self.real_resume = _json(recorded / f"resume-{resume}.json")
        self.recorded_dir = recorded
        self.set_scenario(attempt=attempt, recorded={
            "dir": str(recorded), "receipt_name": "attempt-reconciliation.json", "preview_name": "recovery-preview-01.json",
            "approval_name": "recovery-approval-01.json", "resume_name": f"resume-{resume}.json", "resume_sha": resume})
        self.set_state(effective_index=effective_index, ledger_status="stop_required")
        self._install_driver()
        self.write_params()
        assert self.real_preview["review_sha256"] == self.real_approval["approved_sha256"], "真实预览与批准收据的摘要应一致"

    def sign_everything(self) -> None:
        """人工批准：campaign-resume、（VC-5）工具演进登记、以及恢复预览的批准／授权（官方首批的一次续跑批准）。"""

        self.pre_approve("campaign-resume", self.real_resume["approved_sha256"], extra_files=("REGRESSION_RECEIPT",))
        self.pre_approve("tool-evolution", rw.EVOLUTION_SHA)
        review = self.real_preview["review_sha256"]
        if self.phase == "official":
            self.pre_approve("recovery-continue", review)
        else:
            self.pre_approve("recovery-approve", review)
            self.pre_approve("recovery-authorize", review)


class _RealCase(_WorldCase):
    def vc1(self) -> RealScene:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root.chmod(0o700)
        scene = RealScene(root, evidence_root(), phase="official", attempt=VC1_ATTEMPT, successor=VC1_SUCCESSOR, resume=VC1_RESUME,
                          evolution_registered=True)
        self.addCleanup(self._kill_launched, scene)
        return scene

    def vc5(self) -> RealScene:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root.chmod(0o700)
        scene = RealScene(root, evidence_root(), phase="candidate", attempt=VC5_ATTEMPT, successor=VC5_SUCCESSOR, resume=VC5_RESUME,
                          evolution_registered=False, effective_index=10)
        self.addCleanup(self._kill_launched, scene)
        return scene


class RealRecordShapeTests(_RealCase):
    """恢复计划的读取函数对归档里全部真实记录都能解析，数量与形状符合真实情况。"""

    def test_every_real_reconciliation_directory_parses(self) -> None:
        evidence = evidence_root()
        scene = self.vc5()
        helper = scene.helper()
        total_previews = total_approvals = 0
        for directory in sorted((evidence / "control" / "reconciliation").glob("attempt-*")):
            params = {"C": str(evidence), "ATTEMPT": directory.name[len("attempt-"):], "CANDIDATE": "x"}
            # read_recovery_records 读 C/control/reconciliation/attempt-<ATTEMPT>。
            records = helper.read_recovery_records(params)
            previews = len(list(directory.glob("recovery-preview-*.json")))
            approvals = len(list(directory.glob("recovery-approval-*.json")))
            self.assertEqual((len(records["previews"]), len(records["approvals"])), (previews, approvals), directory.name)
            for preview in records["previews"]:
                self.assertRegex(preview["review_sha256"], r"^[0-9a-f]{64}$")
                # 执行集合 ⊇ 失败／不确定／未执行；保留集合与执行集合不相交。
                self.assertTrue(set(preview["reuse_job_ids"]).isdisjoint(preview["execute_job_ids"]), directory.name)
            if records["previews"]:
                self.assertIsNotNone(records["receipt"], directory.name)
            total_previews += previews
            total_approvals += approvals
        self.assertEqual((total_previews, total_approvals), (16, 11))

    def test_real_attempts_and_resume_receipts_and_isolations_parse(self) -> None:
        evidence = evidence_root()
        helper = self.vc5().helper()
        statuses = {}
        for path in sorted(evidence.glob("official/attempts/*/attempt.json")) + sorted(evidence.glob("candidates/*/attempts/*/attempt.json")):
            summary = helper.read_attempt(path.parent)
            self.assertTrue(summary["exists"])
            statuses[path.parent.name] = summary["status"]
        self.assertEqual(statuses[VC1_ATTEMPT], "environment_contaminated")
        self.assertEqual(statuses[VC5_ATTEMPT], "environment_contaminated")
        self.assertEqual(statuses[VC1_SUCCESSOR], "environment_contaminated", "真实恢复链上一次恢复的后继 attempt 又被污染，所以还有下一轮")
        self.assertEqual(statuses["20261021T003916Z-24ff3483a4288709"], "awaiting_receipts")
        receipts = helper.read_resume_receipts({"C": str(evidence)})
        self.assertEqual({row["approved_sha256"] for row in receipts}, {VC1_RESUME, VC5_RESUME})
        # 真实收据都带 receipt_sha256 与冻结的绑定（半完成续接靠它们重建命令行）；缺一份总账事件的那份被判成未落账。
        for row in receipts:
            self.assertRegex(row["receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["fix_commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(row["regression_receipt_path"].startswith("/") and row["reason"] and row["approved_by"])
        only_first = {"resume_receipts": receipts, "project_resumed": {"receipts": [receipts[0]["receipt_sha256"]]}}
        self.assertEqual([row["approved_sha256"] for row in helper.unresolved_resumes(only_first)], [receipts[1]["approved_sha256"]])
        everything = {"resume_receipts": receipts, "project_resumed": {"receipts": [row["receipt_sha256"] for row in receipts]}}
        self.assertEqual(helper.unresolved_resumes(everything), [])
        isolations = helper.read_isolations({"C": str(evidence), "ATTEMPT": "20261020T212831Z-3939d80823307ee4"})
        self.assertEqual([row["path"] for row in isolations], ["isolation-01.json"])


class RealVc1Tests(_RealCase):
    def test_plan_before_lists_the_real_failure_scene(self) -> None:
        scene = self.vc1()
        plan = scene.plan()
        self.assertEqual((plan["phase"], plan["attempt_status"], plan["ledger_status"]), ("official", "environment_contaminated", "stop_required"))
        self.assertEqual(self.statuses(plan), {
            "resume": "pending", "evolution": "done", "reconcile-runs": "done", "reconcile": "waiting", "approve": "skipped",
            "authorize": "skipped", "continue": "waiting"})
        self.assertEqual(plan["objects"]["source"], "attempt")
        self.assertEqual(len(plan["objects"]["keep"]), 30, "失败 attempt 已完成的 30 个作业（真实 attempt.json）")

    def test_one_command_reaches_continuation_with_real_digests(self) -> None:
        scene = self.vc1()
        scene.sign_everything()
        result = scene.run("--apply")
        # 真实历史里这次恢复派发出的后继 attempt（20261021T001412Z）又被环境污染了——所以才有下一轮恢复。后继一旦已经存在且失败，
        # 计划不报"总体完成"，而是要求改指最新失败的 attempt（退出码 4）；后继还没出现时派发刚结束，退出码是 0。两种都对。
        self.assertIn(result.returncode, (EXIT_OK, EXIT_OPERATOR), result.stderr)
        self.assertEqual(scene.write_calls(), ["campaign-resume:apply"], "官方首批：账本恢复之后批准／授权由 entry.sh 一次完成")
        review = scene.real_preview["review_sha256"]
        self.assertEqual(scene.dispatches(), [f"entry.sh --approve-sha256 {review} --approved-by {rw.APPROVER}"])
        plan = scene.wait_continue_finished()
        self.assertEqual(plan["overall"], "superseded-failed")
        self.assertTrue(any(VC1_SUCCESSOR in warning and "已失败" in warning for warning in plan["warnings"]), plan["warnings"])
        again = scene.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertIn("改指最新失败的 attempt", again.stderr)
        self.assertEqual(len(scene.dispatches()), 1)
        # --follow-latest：目标改指真实历史里的后继，这一轮要恢复的是它。
        followed = json.loads(scene.run("--plan", "--json", "--follow-latest").stdout)
        self.assertEqual((followed["attempt"], followed["followed_from"], followed["attempt_status"]),
                         (VC1_SUCCESSOR, VC1_ATTEMPT, "environment_contaminated"))
        self.assertEqual(self.step(followed, "reconcile")["status"], "pending")
        self.assertEqual(followed["warnings"], [])
        # 写出的现场文件与归档原字节逐字相同（计划读到的就是真实记录）。
        directory = scene.campaign_dir / "control/reconciliation" / f"attempt-{scene.attempt}"
        evidence = evidence_root() / "control/reconciliation" / f"attempt-{scene.attempt}"
        self.assertEqual(_sha(directory / "recovery-preview-01.json"), _sha(evidence / "recovery-preview-01.json"))
        self.assertEqual(_sha(scene.campaign_dir / "control/resume" / f"resume-{VC1_RESUME}.json"),
                         _sha(evidence_root() / "control/resume" / f"resume-{VC1_RESUME}.json"))
        # 保留／重采清单与真实预览逐项一致：保留 30、重采 17（16 个未执行＋1 个结果不确定）。
        objects = plan["objects"]
        real = scene.real_preview
        self.assertEqual(objects["source"], "preview")
        self.assertEqual(objects["keep"], sorted(real["reuse_job_ids"]))
        self.assertEqual((len(objects["keep"]), len(objects["recollect"])), (30, 17))
        reasons = {}
        for row in objects["recollect"]:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        self.assertEqual(reasons, {"尚未执行": 16, "结果不确定": 1})
        self.assertEqual([row["job"] for row in objects["recollect"]], sorted(real["execute_job_ids"]))
        # 后继 attempt 就是真实归档里的那一个。
        self.assertTrue((scene.attempts_dir() / VC1_SUCCESSOR / "attempt.json").is_file())
        self.assertEqual(self.step(plan, "continue")["status"], "done")

    def test_interrupt_at_every_step_then_rerun_matches_clean_run(self) -> None:
        clean = self.vc1()
        clean.sign_everything()
        self.assertIn(clean.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
        reference = clean.wait_continue_finished()["plan_sha256"]
        reference_writes = clean.write_calls()
        for crash in ("resume", "reconcile"):
            with self.subTest(crash_after=crash):
                scene = self.vc1()
                scene.sign_everything()
                self.assertEqual(scene.run("--apply", **scene.crash(crash)).returncode, 137)
                self.assertIn(scene.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
                self.assertEqual(scene.wait_continue_finished()["plan_sha256"], reference)
                self.assertEqual(scene.write_calls(), reference_writes)
                self.assertEqual(len(scene.dispatches()), 1)


class RealVc5Tests(_RealCase):
    def test_plan_before_lists_the_real_failure_scene(self) -> None:
        scene = self.vc5()
        plan = scene.plan()
        self.assertEqual((plan["phase"], plan["attempt_status"], plan["ledger_status"]), ("candidate", "environment_contaminated", "stop_required"))
        self.assertEqual(self.statuses(plan), {
            "resume": "pending", "evolution": "waiting", "reconcile-runs": "done", "reconcile": "waiting", "approve": "waiting",
            "authorize": "waiting", "continue": "waiting"})
        self.assertEqual(len(plan["warnings"]), 1, "演进未登记且尚无对账收据：提示先对账再部署修复")
        # 失败 attempt 已完成 6 个作业（真实 attempt.json）。
        self.assertEqual(len(plan["objects"]["keep"]), 6)

    def test_one_command_reaches_continuation_with_real_digests(self) -> None:
        scene = self.vc5()
        scene.sign_everything()
        result = scene.run("--apply")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(scene.write_calls(), ["campaign-resume:apply", "tool-evolution:apply", "reconcile-attempt:approve",
                                               "reconcile-attempt:authorize"])
        self.assertEqual(len(scene.dispatches()), 1)
        self.assertEqual(scene.dispatches()[0], f"vc5-recover.sh {scene.campaign_dir}/control/reconciliation/attempt-{scene.attempt}/recovery-preview-01.json")
        plan = scene.wait_continue_finished()
        self.assertEqual(plan["overall"], "complete")
        objects, real = plan["objects"], scene.real_preview
        self.assertEqual(objects["keep"], [])
        self.assertEqual([row["job"] for row in objects["recollect"]], sorted(real["execute_job_ids"]))
        reasons = {}
        for row in objects["recollect"]:
            reasons[row["reason"].split("（")[0]] = reasons.get(row["reason"].split("（")[0], 0) + 1
        self.assertEqual(reasons, {"尚未执行": 4, "已完成但不可复用": 6})
        # 预览基于真实的演进 #11，登记后的有效演进也是 #11：重验里这一项为 valid。
        states = {row["object"]: row["state"] for row in objects["reverify"]}
        self.assertEqual(states["恢复预览 #1"], "valid")
        self.assertEqual(states["恢复批准 #1"], "valid")
        self.assertTrue((scene.attempts_dir() / VC5_SUCCESSOR / "attempt.json").is_file())

    def test_interrupt_at_every_step_then_rerun_matches_clean_run(self) -> None:
        clean = self.vc5()
        clean.sign_everything()
        self.assertEqual(clean.run("--apply").returncode, EXIT_OK)
        reference = clean.wait_continue_finished()["plan_sha256"]
        reference_writes = clean.write_calls()
        for crash in ("resume", "evolution", "reconcile", "approve", "authorize"):
            with self.subTest(crash_after=crash):
                scene = self.vc5()
                scene.sign_everything()
                self.assertEqual(scene.run("--apply", **scene.crash(crash)).returncode, 137)
                self.assertEqual(scene.run("--apply").returncode, EXIT_OK)
                self.assertEqual(scene.wait_continue_finished()["plan_sha256"], reference)
                self.assertEqual(scene.write_calls(), reference_writes)
                self.assertEqual(len(scene.dispatches()), 1)

    def test_replay_after_dispatch_is_a_noop(self) -> None:
        scene = self.vc5()
        scene.sign_everything()
        self.assertEqual(scene.run("--apply").returncode, EXIT_OK)
        scene.wait_continue_finished()
        writes, dispatches = scene.write_calls(), scene.dispatches()
        for _ in range(2):
            self.assertEqual(scene.run("--apply").returncode, EXIT_OK)
        self.assertEqual((scene.write_calls(), scene.dispatches()), (writes, dispatches))


if __name__ == "__main__":
    unittest.main()
