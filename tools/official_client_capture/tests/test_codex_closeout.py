"""收尾的人工门、候选隔离、失败关闭和崩溃恢复；所有发布目标均为临时本地裸仓库。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import codex_closeout as closeout
from tools.ci import entry_gates


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    return path


class GuideTests(unittest.TestCase):
    def test_material_reader_rejects_non_managed_layout(self):
        with self.assertRaisesRegex(closeout.CloseoutError, "受管根"):
            closeout.collect_guide_material(Path.cwd() / "arbitrary-campaign", "candidate-a")

    def test_material_reader_uses_fresh_process_and_binds_reader_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tool = root / "tools/official_client_capture/codex_upgrade.py"
            tool.parent.mkdir(parents=True)
            tool.write_text("# 受管读侧占位，实际读取在独立进程合同测试与 ARM64 只读重放覆盖。\n")
            campaign = root / "evidence/campaigns/c-test"
            with mock.patch.object(closeout.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"reader_root": str(root)}), "")) as worker:
                closeout.collect_guide_material(campaign, "candidate-a")
            self.assertEqual(worker.call_args.kwargs["cwd"], root)
            self.assertIn("read-material", worker.call_args.args[0])
            with mock.patch.object(closeout.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"reader_root": "/wrong"}), "")):
                with self.assertRaisesRegex(closeout.CloseoutError, "绑定受管数据根"):
                    closeout.collect_guide_material(campaign, "candidate-a")

    def test_real_source_cascade_preserves_historical_profiles(self):
        root = Path(closeout.__file__).resolve().parents[1]
        before = (root / closeout.GUIDE).read_text()
        changed = before.replace(closeout.PART2, closeout.PART2 + "\n\n测试新增说明。", 1)
        updates = closeout.source_updates(root, changed)
        self.assertEqual(len(updates), 15)
        self.assertNotIn("tools/official_client_capture/candidate_rule_expectations_0_145_0.json", updates)
        self.assertNotIn("tools/official_client_capture/candidate_rule_expectations_0_147_0.json", updates)
        self.assertEqual((root / closeout.GUIDE).read_text(), before)
        for relative, value in updates.items():
            if relative.endswith(".json"):
                old = json.loads((root / relative).read_text())
                new = json.loads(value["text"])
                if isinstance(old["source_spec"], dict):
                    old["source_spec"]["sha256"] = closeout.part2_sha(changed)
                else:
                    old["source_spec_sha256"] = closeout.part2_sha(changed)
                self.assertEqual(old, new)

    def test_draft_changes_only_affected_rules_inside_part2(self):
        original = "# 第一部分\n### SPEC-X-001 别处\n原文\n" + closeout.PART2 + "\n\n### SPEC-X-001 规则\n旧说明\n\n### SPEC-X-002 不变\n保留\n\n# 第三部分\n结束\n"
        rule = {"rule_id": "SPEC-X-001", "scenario_ids": ["A01"], "checks": [{"id": "a", "assertion": {"operator": "count"}}]}
        material = {"migration": {"entries": [{"target_rule": "SPEC-X-001", "classification": "change", "rationale": "正式迁移理由"}]},
                    "profile": {"rules": [rule]}, "official_observation_counts": {"SPEC-X-001": {"a": 7}}}
        result = closeout.guide_draft(original, material, {"rules": []})
        self.assertIn("匹配 7 条", result["guide_text"])
        self.assertIn("不是去重请求数", result["guide_text"])
        self.assertIn("### SPEC-X-002 不变\n保留", result["guide_text"])
        self.assertTrue(result["guide_text"].startswith("# 第一部分\n### SPEC-X-001 别处\n原文\n"))
        self.assertTrue(result["guide_text"].endswith("# 第三部分\n结束\n"))
        self.assertEqual(result["affected_rule_ids"], ["SPEC-X-001"])

    def test_unchanged_inheritance_produces_no_cascade(self):
        original = closeout.PART2 + "\n\n### SPEC-X-001\n保留\n"
        rule = {"rule_id": "SPEC-X-001", "checks": []}
        material = {"migration": {"entries": [{"target_rule": "SPEC-X-001", "classification": "inherit"}]}, "profile": {"rules": [rule]}}
        result = closeout.guide_draft(original, material, material["profile"])
        self.assertEqual(result["guide_text"], original)
        self.assertEqual(result["affected_rule_ids"], [])

    def test_add_and_delete_rules_preserve_other_chapters(self):
        original = closeout.PART2 + "\n\n### SPEC-X-001\n旧规则\n\n# 后续\n保留\n"
        material = {"migration": {"entries": [
            {"target_rule": None, "baseline_rule": "SPEC-X-001", "classification": "delete", "rationale": "已退休"},
            {"target_rule": "SPEC-X-002", "classification": "add", "rationale": "新增"}]},
            "profile": {"rules": [{"rule_id": "SPEC-X-002", "scenario_ids": ["A01"], "checks": []}]},
            "official_observation_counts": {}}
        result = closeout.guide_draft(original, material, {"rules": []})["guide_text"]
        self.assertIn("迁移分类**：delete", result)
        self.assertIn("### SPEC-X-002", result)
        self.assertTrue(result.endswith("# 后续\n保留\n"))


class CloseoutTests(unittest.TestCase):
    """真实 Git 候选和本地推送；Campaign 读侧与终态事实用独立夹具替代，另由合同回归覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo, self.work = self.root / "repo", self.root / "work"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "隔离验收")
        self.git("config", "user.email", "closeout@example.invalid")
        guide = closeout.PART2 + "\n\n测试基线\n"
        (self.repo / closeout.GUIDE).parent.mkdir(parents=True)
        (self.repo / closeout.GUIDE).write_text(guide)
        write_json(self.repo / "tools/official_client_capture/profile.json", {"source_spec": closeout.GUIDE + "#第二章", "source_spec_sha256": closeout.part2_sha(guide)})
        self.git("add", ".")
        self.git("commit", "-m", "隔离基线")
        self.base = self.git("rev-parse", "HEAD")
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        material = {"campaign_path": str(self.root / "campaign"), "campaign_id": "c-test", "candidate_id": "candidate-test", "target_version": "9.9.9",
                    "classification_sha256": "a" * 64, "official_stage_sha256": "b" * 64}
        self.material = material
        write_json(self.root / "material.json", material)
        write_json(self.root / "facts.json", {"target": {"version": "9.9.9"}, "campaign_chain": [{"campaign_id": "c-test"}]})
        (self.root / "guide.md").write_text(guide.replace("测试基线", "经审核的新规则"))
        (self.root / "env.sh").write_text("# 隔离门禁参数\n")
        (self.root / "driver").mkdir()
        (self.root / "driver/entry-gates.sh").write_text("#!/bin/bash\nexit 1\n")
        (self.root / "frontend").mkdir()
        (self.root / "frontend/pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        write_json(self.root / "decision.json", {"reason": "演练没有生产清理对象"})
        script = self.root / "verify.py"
        script.write_text("#!/usr/bin/env python3\nimport os, json\nfrom pathlib import Path\np=Path(os.environ['CODEX_CLOSEOUT_RESULT'])\np.parent.mkdir(parents=True, exist_ok=True)\nv={'schema_version':'codex-closeout-action-result/v1','status':'passed','operation_key':os.environ['CODEX_CLOSEOUT_OPERATION_KEY'],'review_sha256':os.environ['CODEX_CLOSEOUT_REVIEW_SHA256'],'execution_receipt':json.loads(Path(__file__).with_name('action-proof.json').read_text())}\np.write_text(json.dumps(v))\n")
        script.chmod(0o700)
        write_json(self.root / "action-proof.json", closeout.safety.file_binding(self.root / "decision.json"))
        self.config = {"schema_version": closeout.SCHEMA, "repo": str(self.repo), "work_root": str(self.work), "guide": str(self.root / "guide.md"),
                       "material": str(self.root / "material.json"), "terminal_facts": str(self.root / "facts.json"),
                       "terminal_relative": "docs/egress/maintenance/CODEX_CLI_TEST_TERMINAL_STATE_RECEIPT.json", "tag": "isolated-test",
                       "gate": {"driver": str(self.root / "driver/entry-gates.sh"), "vc_env": str(self.root / "env.sh"), "node_modules": str(self.root / "frontend")},
                       "cleanup": {"mode": "defer", "reason": "没有清理对象", "inputs": [str(self.root / "decision.json")]},
                       "deployment_check": {"script": str(script), "arguments": [], "inputs": [str(self.root / "action-proof.json"), str(self.root / "decision.json")],
                                            "result": str(self.work / "deployment.json")},
                       "push": {"remote": "origin", "ref": "refs/heads/main", "expected_tip": self.base}}
        self.plan_path = self.work / "plan.json"
        for patcher in (
            mock.patch.object(closeout, "collect_guide_material", return_value=material),
            mock.patch.object(closeout, "terminal_candidate", side_effect=lambda _repo, facts, _path: json.dumps(facts) + "\n"),
            mock.patch("tools.upstream_merge.freeze.generate_freeze_successor", return_value={"transitions": []}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        return closeout.git(self.repo, *args).stdout.strip()

    def prepare(self):
        self.plan = closeout.prepare(self.config)
        return self.plan

    def approvals(self):
        now = datetime.now(timezone.utc)
        result = {}
        for scope in ("guide-review", "release-publication", "cleanup"):
            path = self.root / (scope + ".json")
            write_json(path, {"schema_version": "guide-review-approval/v1" if scope == "guide-review" else "codex-closeout-operation-approval/v1",
                             "status": "approved", "review_sha256": self.plan["review_sha256"], "scope": scope, "approved_by": "隔离测试角色",
                             "approved_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                             "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                             "proof": closeout.safety.file_binding(self.root / "decision.json")})
            result[scope] = path
        return result

    def gate_result(self):
        """按真实导出的字段构造边界测试证据；不宣称实际运行全量业务测试。"""
        root = self.work / "gates"
        rows, manifest_rows, units = [], [], []
        for name in entry_gates.profile_gates("full-gates"):
            command, directory = entry_gates.GATE_COMMANDS[name]
            log = root / "executor" / (name + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("隔离结果夹具\n")
            write_json(root / "logs" / (name + ".gate.json"), {
                "gate_id": name, "command": command, "working_directory": directory, "status": "passed", "exit_code": 0,
                "tree_head": self.plan["candidate_commit"], "mode": "re-execute", "failed_units": [], "inherited_units": [],
                "started_at_utc": "2026-10-05T00:00:00Z", "completed_at_utc": "2026-10-05T00:01:00Z"})
            rows.append({"gate_id": name, "status": "passed", "exit_code": 0, "gate_json": "logs/" + name + ".gate.json"})
            manifest_rows.append({"gate_id": name, "units": [name]})
            units.append({"unit_id": name, "passed": True, "exit_code": 0, "orphans": 0, "log": str(log)})
        write_json(root / "entry-gates.json", {"schema_version": entry_gates.ENTRY_SUMMARY_SCHEMA, "status": "passed", "profile": "full-gates",
                   "mode": "re-execute", "source": {"commit": self.plan["candidate_commit"], "tree_head": self.plan["candidate_commit"]},
                   "gates": rows, "executor_summary": str(root / "executor/summary.json")})
        write_json(root / "gates-manifest.json", {"schema_version": entry_gates.GATES_SCHEMA, "profile": "full-gates", "gates": manifest_rows,
                   "units": [{"unit_id": row["unit_id"]} for row in units]})
        write_json(root / "executor/summary.json", {"schema_version": entry_gates.GATES_SUMMARY_SCHEMA, "status": "passed", "mode": "re-execute",
                   "units": units, "units_not_run": [], "test_groups": {}})
        closeout.journal(self.plan, "gates", "started", {"fixture": True})
        return closeout.run_gates(self.plan_path)

    def test_dry_run_does_not_create_work_or_modify_repository(self):
        result = closeout.prepare(self.config, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(self.work.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_prepare_is_private_and_repeat_does_not_resign(self):
        first = self.prepare()
        second = closeout.prepare(self.config)
        self.assertEqual(first, second)
        self.assertFalse((self.repo / self.config["terminal_relative"]).exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(closeout.load_plan(self.plan_path), first)

    def test_existing_terminal_receipt_is_never_resigned(self):
        write_json(self.repo / self.config["terminal_relative"], {"already": "issued"})
        self.git("add", ".")
        self.git("commit", "-m", "已有终态")
        with self.assertRaisesRegex(closeout.CloseoutError, "已存在"):
            self.prepare()

    def test_same_version_terminal_at_another_path_blocks_resigning(self):
        write_json(self.repo / "docs/egress/maintenance/CODEX_CLI_OTHER_TERMINAL_STATE_RECEIPT.json", {"target": {"version": "9.9.9"}})
        self.git("add", ".")
        self.git("commit", "-m", "已有同版本终态")
        with self.assertRaisesRegex(closeout.CloseoutError, "换路径重复签发"):
            self.prepare()

    def test_missing_approval_blocks_before_formal_writes(self):
        self.prepare()
        with self.assertRaisesRegex(closeout.CloseoutError, "缺少人工批准"):
            closeout.publish(self.plan_path, {})
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertFalse((self.work / "deployment.json").exists())

    def test_expired_wrong_scope_digest_and_permissions_are_rejected(self):
        self.prepare()
        approvals = self.approvals()
        path = approvals["guide-review"]
        original = json.loads(path.read_text())
        for delta in ({"review_sha256": "0" * 64}, {"scope": "cleanup"}, {"expires_at_utc": "2020-01-01T00:00:00Z"}):
            with self.subTest(delta=delta):
                write_json(path, {**original, **delta})
                with self.assertRaises(closeout.CloseoutError):
                    closeout.publish(self.plan_path, approvals)
        write_json(path, original)
        path.chmod(0o644)
        with self.assertRaisesRegex(closeout.CloseoutError, "0600"):
            closeout.publish(self.plan_path, approvals)

    def test_no_gate_blocks_approved_publication(self):
        self.prepare()
        with self.assertRaisesRegex(closeout.CloseoutError, "完整收尾门禁"):
            closeout.publish(self.plan_path, self.approvals())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_gate_failure_stays_failed_and_does_not_restart(self):
        self.prepare()
        with self.assertRaisesRegex(closeout.CloseoutError, "全量门禁失败"):
            closeout.run_gates(self.plan_path)
        log = (self.work / "gates.log").read_bytes()
        with self.assertRaisesRegex(closeout.CloseoutError, "原门禁已失败"):
            closeout.run_gates(self.plan_path)
        self.assertEqual((self.work / "gates.log").read_bytes(), log)

    def test_complete_local_publication_is_idempotent(self):
        self.prepare()
        self.gate_result()
        approvals = self.approvals()
        first = closeout.publish(self.plan_path, approvals)
        snapshot = {str(p.relative_to(self.work)): p.read_bytes() for p in (self.work / "journal").glob("*.json")}
        second = closeout.publish(self.plan_path, {})
        self.assertEqual(first, second)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.plan["candidate_commit"])
        self.assertEqual(closeout.remote_tip(self.plan), self.plan["candidate_commit"])
        self.assertTrue((self.repo / self.config["terminal_relative"]).is_file())
        self.assertFalse(first["performance_claim"])
        self.assertEqual(snapshot, {str(p.relative_to(self.work)): p.read_bytes() for p in (self.work / "journal").glob("*.json")})

    def test_resume_after_successful_push_before_receipt_writes(self):
        self.prepare()
        self.gate_result()
        approvals = self.approvals()
        original = closeout.journal
        def interrupted(plan, name, phase, value):
            if name == "push" and phase == "done":
                raise InterruptedError("模拟已推送但完成收据未落盘")
            return original(plan, name, phase, value)
        with mock.patch.object(closeout, "journal", side_effect=interrupted):
            with self.assertRaises(InterruptedError):
                closeout.publish(self.plan_path, approvals)
        self.assertEqual(closeout.remote_tip(self.plan), self.plan["candidate_commit"])
        real_git = closeout.git
        def no_second_push(repo, *args, **kwargs):
            self.assertNotEqual(args[0], "push", "恢复不能再次推送")
            return real_git(repo, *args, **kwargs)
        with mock.patch.object(closeout, "git", side_effect=no_second_push):
            self.assertEqual(closeout.publish(self.plan_path, approvals)["status"], "passed")

    def test_unknown_push_does_not_retry(self):
        self.prepare()
        self.gate_result()
        closeout.journal(self.plan, "push", "started", {"fixture": True})
        with self.assertRaisesRegex(closeout.CloseoutError, "不自动重推"):
            closeout.publish(self.plan_path, self.approvals())
        self.assertEqual(closeout.remote_tip(self.plan), self.base)

    def test_external_candidate_edit_blocks_resume(self):
        self.prepare()
        Path(self.plan["candidate"], closeout.GUIDE).write_text("外部改动")
        with self.assertRaisesRegex(closeout.CloseoutError, "候选源码"):
            closeout.load_plan(self.plan_path)

    def test_input_changes_require_new_plan(self):
        self.prepare()
        (self.root / "guide.md").write_text("外部修改")
        with self.assertRaisesRegex(closeout.CloseoutError, "输入或工具"):
            closeout.load_plan(self.plan_path)

    def test_plan_self_digest_rejects_tamper(self):
        self.prepare()
        forged = {**self.plan, "candidate_commit": "f" * 40}
        write_json(self.plan_path, forged)
        with self.assertRaisesRegex(closeout.CloseoutError, "自摘要"):
            closeout.load_plan(self.plan_path)

    def test_gate_coverage_or_unit_failure_cannot_hide_behind_passed(self):
        self.prepare()
        self.gate_result()
        path = self.work / "gates/executor/summary.json"
        original = json.loads(path.read_text())
        for change in ("missing", "failed", "inherited", "orphan", "duplicate"):
            value = deepcopy(original)
            if change == "missing": value["units"].pop()
            elif change == "failed": value["units"][0]["passed"] = False
            elif change == "inherited": value["units"][0]["disposition"] = "inherited"
            elif change == "orphan": value["units"][0]["orphans"] = 1
            else: value["units"].append(value["units"][0])
            write_json(path, value)
            with self.subTest(change=change), self.assertRaises(closeout.CloseoutError):
                closeout.check_gate_result(self.plan)

    def test_gate_log_tamper_blocks_publication(self):
        self.prepare()
        self.gate_result()
        (self.work / "gates/executor/backend-go-test.log").write_text("替换日志")
        with self.assertRaisesRegex(closeout.CloseoutError, "完整收尾门禁"):
            closeout.publish(self.plan_path, self.approvals())

    def test_remote_change_blocks_before_local_publication(self):
        self.prepare()
        self.gate_result()
        with mock.patch.object(closeout, "remote_tip", return_value="c" * 40):
            with self.assertRaisesRegex(closeout.CloseoutError, "远端提交变化"):
                closeout.publish(self.plan_path, self.approvals())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_cleanup_requires_specific_approval_backup_and_verification(self):
        proof = closeout.safety.file_binding(self.root / "decision.json")
        self.config["cleanup"] = {**self.config["deployment_check"], "mode": "execute", "targets": ["isolated-object"],
                                  "backup": proof, "restore_check": proof, "result": str(self.work / "cleanup.json")}
        self.prepare()
        self.gate_result()
        approvals = self.approvals()
        del approvals["cleanup"]
        with self.assertRaisesRegex(closeout.CloseoutError, "cleanup"):
            closeout.publish(self.plan_path, approvals)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        # 脚本只返回一般成功，没有删除证明，必须阻断推送且保留清理占位。
        with self.assertRaisesRegex(closeout.CloseoutError, "清理对象"):
            closeout.publish(self.plan_path, self.approvals())
        self.assertTrue((self.work / "journal/cleanup-started.json").is_file())
        self.assertEqual(closeout.remote_tip(self.plan), self.base)

    def test_cleanup_unknown_result_never_reexecutes(self):
        self.prepare()
        self.gate_result()
        closeout.journal(self.plan, "deployment_check", "started", {"fixture": True})
        with self.assertRaises(ValueError):
            closeout.publish(self.plan_path, self.approvals())
        self.assertFalse((self.work / "deployment_check.log").exists())

    def test_approved_cleanup_deletes_only_isolated_target_once(self):
        target = self.root / "disposable.txt"
        target.write_text("隔离数据")
        backup = self.root / "backup.txt"
        backup.write_bytes(target.read_bytes())
        restore = write_json(self.root / "restore.json", {"target": str(target), "restored_sha256": closeout.sha(backup.read_bytes())})
        action = {**self.config["deployment_check"], "mode": "execute", "targets": [str(target)],
                  "backup": closeout.safety.file_binding(backup), "restore_check": closeout.safety.file_binding(restore),
                  "result": str(self.work / "cleanup.json")}
        script = self.root / "cleanup.py"
        action["script"] = str(script)
        script.write_text("#!/usr/bin/env python3\nimport os, json, hashlib\nfrom pathlib import Path\n"
                          + "action=" + repr(action) + "\n"
                          + "target=Path(action['targets'][0]); target.unlink()\n"
                          + "p=Path(os.environ['CODEX_CLOSEOUT_RESULT']); verification=p.with_name('deletion.json')\n"
                          + "key=os.environ['CODEX_CLOSEOUT_OPERATION_KEY']\n"
                          + "verification.write_text(json.dumps({'operation_key':key,'targets':action['targets'],'status':'passed','remaining_targets':[]}))\n"
                          + "result={'schema_version':'codex-closeout-action-result/v1','operation_key':key,'review_sha256':os.environ['CODEX_CLOSEOUT_REVIEW_SHA256'],'status':'passed','execution_receipt':action['restore_check'],'backup':action['backup'],'restore_check':action['restore_check'],'targets':action['targets'],'deletion_verification':{'path':str(verification),'sha256':hashlib.sha256(verification.read_bytes()).hexdigest()}}\n"
                          + "p.write_text(json.dumps(result))\n")
        script.chmod(0o700)
        self.config["cleanup"] = action
        self.prepare()
        self.gate_result()
        first = closeout.publish(self.plan_path, self.approvals())
        self.assertFalse(target.exists())
        self.assertEqual(backup.read_text(), "隔离数据")
        self.assertEqual(first, closeout.publish(self.plan_path, {}))

    def test_symlink_and_path_escape_rejected(self):
        link = self.root / "linked.md"
        link.symlink_to(self.root / "guide.md")
        self.config["guide"] = str(link)
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            closeout.inside(self.repo, "../outside")

    def test_interrupted_prepare_preserves_existing_candidate(self):
        (self.work / "candidate").mkdir(parents=True)
        marker = self.work / "candidate/marker"
        marker.write_text("现场")
        with self.assertRaisesRegex(closeout.CloseoutError, "准备中断"):
            self.prepare()
        self.assertEqual(marker.read_text(), "现场")

    def test_mutual_exclusion_blocks_second_runner(self):
        with closeout.run_lock(self.work):
            with self.assertRaisesRegex(closeout.CloseoutError, "已有实例"):
                with closeout.run_lock(self.work):
                    self.fail("竞争者获得了锁")


if __name__ == "__main__":
    unittest.main()
