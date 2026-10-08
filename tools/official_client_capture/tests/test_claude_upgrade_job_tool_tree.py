"""Claude Job v2 双根合同与 current/formed 边界的离线专项验证。

树观测在本机子进程真正执行生产枚举脚本；容器命令由本机 Docker 替身执行。
不启动 Docker，不执行 Claude、SDK 或原生构建物。版本负例使用固定基线生成的
v1 原字节，不把当前事实重新改名签发成历史记录。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tools.official_client_capture import certify_release
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_campaign as campaign_api
from tools.official_client_capture import claude_upgrade_job_rehearsal as profile
from tools.official_client_capture import claude_upgrade_job_tool_tree as tree_api
from tools.official_client_capture import claude_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as shared
from tools.official_client_capture import codex_upgrade_policy_certification as policy_api
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests.claude_job_rehearsal_v1_fixture import copy_formed_v1
from tools.official_client_capture.tests.test_claude_upgrade_job_rehearsal import (
    ClaudeRehearsalFixture, FAKE_DOCKER, TOOL_ROOT, _scenario,
)

FILES = {
    "official_client_capture/relay.py": b"# relay\n",
    "official_client_capture/native.m": b"// native\n",
    "official_client_capture/entry.mjs": b"// entry\n",
    "official_client_control/store.py": b"# store\n",
    "official_client_control/schema.json": b"{}\n",
}
PRODUCTION_CONTAINER_COMMANDS = profile.CONTAINER_REQUIRED_COMMANDS


class ToolTreeV2Tests(unittest.TestCase):
    """实际枚举四角色八根，以及纯校验的完整读集拒绝条件。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        policy_path = TOOL_ROOT / "claude_tool_identity_policy_v2.json"
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        self.scope = tree_api.build_read_contract(policy_path, policy_sha)
        entries = [{"path": name, "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in sorted(FILES.items())]
        self.contract = {
            "policy_sha256": policy_sha,
            "tool_tree_read_contract": self.scope,
            "tool_files_sha256": shared._fingerprint({"entries": entries}),
        }
        self.roots = {}
        for role in tree_api.TREE_NAMES:
            root = self.root / role
            self.roots[role] = root
            for name, raw in FILES.items():
                path = root / "tools" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            # 明确冻结的排除项与非受管后缀都不进入摘要。
            for name in ("tests/fixture.py", "versions/history.json", "__pycache__/unused.py", "ignore.txt"):
                path = root / "tools/official_client_capture" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"excluded")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable))
        docker.chmod(0o700)
        self.calls = self.root / "calls.jsonl"

    def observation(self, role: str) -> dict:
        tool_root = self.roots[role] / "tools/official_client_capture"
        if role.endswith("host"):
            return tree_api.observe_host_tree(tool_root, self.contract)
        with mock.patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_DOCKER_CALLS": str(self.calls),
        }):
            return tree_api.observe_container_tree(role, str(tool_root), self.contract)

    def test_eight_roots_run_the_real_host_and_container_script(self) -> None:
        observations = {role: self.observation(role) for role in tree_api.TREE_NAMES}
        tree_api.validate_tool_trees(observations, self.contract)
        self.assertEqual(sum(len(item["root_observations"]) for item in observations.values()), 8)
        for role, item in observations.items():
            self.assertEqual(item["root"], str(self.roots[role]))
            self.assertEqual([row["path"] for row in item["entries"]], sorted(FILES))
            self.assertEqual(item["files_sha256"], self.contract["tool_files_sha256"])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[:1], ["exec"])
            self.assertIn(tree_api.ENUMERATION_SCRIPT, call)

    def test_each_missing_or_modified_sensitive_file_fails_real_observation(self) -> None:
        root = self.roots["execution_host"] / "tools"
        for name in ("official_client_capture/native.m", "official_client_capture/entry.mjs",
                     "official_client_control/store.py", "official_client_control/schema.json"):
            path = root / name
            raw = path.read_bytes()
            for operation in ("删除", "修改"):
                with self.subTest(path=name, operation=operation):
                    if operation == "删除":
                        path.unlink()
                    else:
                        path.write_bytes(raw + b"changed")
                    try:
                        with self.assertRaises(shared.JobRehearsalReceiptError):
                            self.observation("execution_host")
                    finally:
                        path.write_bytes(raw)

    def test_an_extra_managed_file_fails_real_observation(self) -> None:
        path = self.roots["execution_container"] / "tools/official_client_control/unknown.py"
        path.write_bytes(b"extra")
        with self.assertRaisesRegex(shared.JobRehearsalReceiptError, "冻结合同"):
            self.observation("execution_container")

    def test_missing_roots_symlink_roots_and_descendants_fail_in_each_role(self) -> None:
        for role in tree_api.TREE_NAMES:
            root = self.roots[role] / "tools/official_client_control"
            retained = root.with_name("retained-control")
            root.rename(retained)
            try:
                with self.subTest(role=role, case="根不存在"), self.assertRaises(shared.JobRehearsalReceiptError):
                    self.observation(role)
                root.symlink_to(retained, target_is_directory=True)
                with self.subTest(role=role, case="根为符号链接"), self.assertRaises(shared.JobRehearsalReceiptError):
                    self.observation(role)
                root.unlink()
            finally:
                retained.rename(root)
            link = root / "alias.py"
            link.symlink_to(root / "store.py")
            try:
                with self.subTest(role=role, case="后代符号链接"), self.assertRaises(shared.JobRehearsalReceiptError):
                    self.observation(role)
            finally:
                link.unlink()

    def test_four_copies_cannot_agree_by_omitting_the_same_file(self) -> None:
        first = self.observation("managed_host")
        observations = {role: copy.deepcopy(first) for role in tree_api.TREE_NAMES}
        for item in observations.values():
            item["entries"] = [row for row in item["entries"] if row["path"] != "official_client_control/store.py"]
            item["entry_count"] = len(item["entries"])
            item["files_sha256"] = shared._fingerprint({"entries": item["entries"]})
        with self.assertRaisesRegex(shared.JobRehearsalReceiptError, "完整工具树与冻结合同"):
            tree_api.validate_tool_trees(observations, self.contract)

    def test_observed_roots_require_real_directory_and_non_symlink_facts(self) -> None:
        first = self.observation("managed_host")
        cases = (
            lambda value: value["root_observations"].pop(),
            lambda value: value["root_observations"][1].update(is_directory=False),
            lambda value: value["root_observations"][1].update(is_symlink=True),
            lambda value: value["root_observations"][1].update(path="/other/control"),
            lambda value: value["root_observations"][1].update(is_directory=1),
            lambda value: value["root_observations"].reverse(),
        )
        for index, mutate in enumerate(cases):
            current = copy.deepcopy(first)
            mutate(current)
            with self.subTest(case=index), self.assertRaises(shared.JobRehearsalReceiptError):
                tree_api.validate_tree(current, self.contract, "测试")

    def test_paths_duplicates_order_and_digest_wrappers_are_strict(self) -> None:
        first = self.observation("managed_host")
        for name in ("../outside.py", "/official_client_capture/outside.py",
                     "official_client_capture/../outside.py", "official_client_capture//relay.py",
                     "official_client_capture/./relay.py", "official_client_capture\\relay.py",
                     "official_client_capture/invalid\x00.py",
                     "official_client_capture-other/relay.py", "official_client_capture/relay.txt",
                     "official_client_capture/tests/relay.py"):
            current = copy.deepcopy(first)
            current["entries"][0]["path"] = name
            current["files_sha256"] = shared._fingerprint({"entries": current["entries"]})
            with self.subTest(path=name), self.assertRaises(shared.JobRehearsalReceiptError):
                tree_api.validate_tree(current, self.contract, "测试")
        for case in ("重复", "乱序", "数组摘要", "尾部换行", "数量类型", "摘要类型"):
            current = copy.deepcopy(first)
            if case == "重复":
                current["entries"].append(copy.deepcopy(current["entries"][-1]))
                current["entry_count"] += 1
            elif case == "乱序":
                current["entries"].reverse()
            elif case == "数组摘要":
                current["files_sha256"] = shared._fingerprint(current["entries"])
            elif case == "尾部换行":
                current["files_sha256"] = hashlib.sha256(shared._canonical({"entries": current["entries"]})).hexdigest()
            elif case == "数量类型":
                current["entry_count"] = str(current["entry_count"])
            else:
                current["entries"][0]["sha256"] = int("1" * 64)
            with self.subTest(case=case), self.assertRaises(shared.JobRehearsalReceiptError):
                tree_api.validate_tree(current, self.contract, "测试")

    def test_read_scope_cannot_narrow_roots_suffixes_or_exclusions(self) -> None:
        mutations = (
            lambda value: value["roots"].pop(),
            lambda value: value["suffixes"].remove(".m"),
            lambda value: value["suffixes"].remove(".mjs"),
            lambda value: value["excluded_parts"].append("official_client_control"),
            lambda value: value.update(policy_json=value["policy_json"] + " "),
            lambda value: value.update(policy_sha256="0" * 64),
            lambda value: value.update(extra=True),
        )
        for index, mutate in enumerate(mutations):
            current = copy.deepcopy(self.scope)
            mutate(current)
            with self.subTest(case=index), self.assertRaises(shared.JobRehearsalReceiptError):
                tree_api.validate_read_contract(current, self.contract["policy_sha256"])
        for field in ("roots", "suffixes"):
            policy = json.loads(self.scope["policy_json"])
            policy[field].pop()
            raw = json.dumps(policy, ensure_ascii=False)
            sha = hashlib.sha256(raw.encode()).hexdigest()
            current = {**self.scope, "policy_json": raw, "policy_sha256": sha, field: policy[field]}
            with self.subTest(frozen_policy=field), self.assertRaises(shared.JobRehearsalReceiptError):
                tree_api.validate_read_contract(current, sha)

    def test_policy_duplicate_keys_and_actual_file_binding_fail_closed(self) -> None:
        raw = self.scope["policy_json"].replace('"client": "claude"', '"client": "claude", "client": "claude"')
        sha = hashlib.sha256(raw.encode()).hexdigest()
        current = {**self.scope, "policy_json": raw, "policy_sha256": sha}
        with self.assertRaisesRegex(shared.JobRehearsalReceiptError, "合法 JSON"):
            tree_api.validate_read_contract(current, sha)
        with self.assertRaisesRegex(shared.JobRehearsalReceiptError, "原字节摘要"):
            tree_api.build_read_contract(TOOL_ROOT / "claude_tool_identity_policy_v2.json", "0" * 64)

    def test_pure_tree_validation_never_reads_current_files_or_runs_a_probe(self) -> None:
        first = self.observation("managed_host")
        observations = {role: copy.deepcopy(first) for role in tree_api.TREE_NAMES}
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("纯校验不得读文件")), \
             mock.patch.object(shared, "_run", side_effect=AssertionError("纯校验不得执行探针")), \
             mock.patch.object(claude_upgrade, "_tool_identity", side_effect=AssertionError("纯校验不得取当前身份")):
            tree_api.validate_tool_trees(observations, self.contract)


class VersionBoundaryFixture(ClaudeRehearsalFixture):
    """历史原件与实际当前合同的共同离线环境。"""

    def setUp(self) -> None:
        super().setUp()
        # 完整声明五个容器依赖。Python 之外的替身仅被 which/hash 探针读取，
        # 若误执行会直接失败，不掩盖任何真实工作负载。
        for name in PRODUCTION_CONTAINER_COMMANDS:
            if name == "python3":
                continue
            command = self.bin / name
            command.write_text("#!" + sys.executable + "\nraise SystemExit(125)\n")
            command.chmod(0o700)
        self.legacy_root = self.root / "formed-v1"
        self.provenance = copy_formed_v1(self.legacy_root)
        self.legacy_receipt = json.loads((self.legacy_root / "receipt.json").read_text())

    @contextlib.contextmanager
    def patched(self, **options):
        with super().patched(**options), mock.patch.object(profile, "CONTAINER_REQUIRED_COMMANDS", PRODUCTION_CONTAINER_COMMANDS):
            yield

    @contextlib.contextmanager
    def formed_environment(self):
        """按原件登记坐标解释生命周期，不改变校验器或历史事实字节。"""
        facts = json.loads((self.legacy_root / "facts.json").read_text())
        probe = facts["probes"]["failure_lifecycle"]
        with mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", Path(probe["host_runs_root"])), \
             mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS",
                               tuple(PurePosixPath(alias) / "runs" for alias in probe["container_aliases"])):
            yield

    def current_contract(self) -> tuple[Path, dict, dict]:
        campaign = self.write_campaign()
        with self.patched():
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            contract = profile.build_job_contract(manifest, _scenario(), None)
        return campaign, manifest, contract

    def binding(self, root: Path, name: str, receipt: dict) -> dict:
        return {
            "evidence_root": str(root), "receipt": name,
            "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            "job_count": receipt["job_count"], "execution_contract_sha256": receipt["execution_contract_sha256"],
            "storage_probe_sha256": receipt.get("storage_probe_sha256"),
            "failure_lifecycle_probe_sha256": receipt.get("failure_lifecycle_probe_sha256"),
            "preflight_campaign_id": receipt["preflight_campaign"]["campaign_id"],
        }

    def certification(self, binding: dict) -> tuple[Path, dict]:
        """只替代无关的 pre-A3 覆盖解释；实际认证、自摘要、文件绑定及 Job 消费均执行。"""
        identity = {field: "a" * 64 for field in policy_api.IDENTITY_FIELDS}
        identity["policy_version"] = 11
        other = self.root / "other.json"
        other.write_bytes(b"{}\n")
        other.chmod(0o600)
        file_binding = {"path": str(other), "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}
        coverage = [{"id": name, "test": "离线夹具", "status": "passed"}
                    for name in certify_release.RELEASE_REQUIRED_CHAINS["claude"]]
        unsigned = {
            "schema_version": certify_release.CLIENT_SCHEMAS["claude"], "client": "claude",
            "status": "active", "superseded_by": None, "identity": {
                field: identity[field] for field in policy_api.IDENTITY_FIELDS},
            "policy_version": identity["policy_version"], "deployment_receipt": file_binding,
            "pre_a3_certification": file_binding, "real_chain_coverage": coverage,
            "job_rehearsal": binding,
            "atomic_double_rehearsal": {
                "evidence_root": str(other.parent), "receipt": other.name, "sha256": file_binding["sha256"]},
        }
        unsigned["receipt_sha256"] = certify_release._fingerprint(unsigned, "claude")
        path = self.root / "certification.json"
        path.write_bytes(shared._canonical(unsigned))
        path.chmod(0o600)
        return path, identity


class FormedV1BoundaryTests(VersionBoundaryFixture):
    """v1 原字节只读重放，以及所有当前消费者不可降级。"""

    def test_formed_full_storage_only_and_noop_receipts_keep_original_bytes(self) -> None:
        originals = {row["path"]: (self.legacy_root / row["path"]).read_bytes()
                     for row in self.provenance["artifacts"]}
        with self.formed_environment(), \
             mock.patch.object(profile.tool_tree, "build_read_contract", side_effect=AssertionError("历史重放不得读当前策略")), \
             mock.patch.object(shared, "_run", side_effect=AssertionError("历史重放不得启动探针")):
            for name in ("receipt.json", "storage-only-receipt.json", "noop-receipt.json"):
                with self.subTest(receipt=name):
                    replayed = shared.replay(self.legacy_root, name, client="claude")
                    self.assertEqual(shared._canonical(replayed), originals[name])
                    self.assertEqual(replayed["producer"], self.provenance["collector"])
        self.assertEqual({name: (self.legacy_root / name).read_bytes() for name in originals}, originals)

    def test_current_build_and_finalize_reject_all_v1_even_with_collector_drift(self) -> None:
        for name in ("facts.json", "storage-only-facts.json", "noop-facts.json"):
            for drift in (False, True):
                with self.subTest(facts=name, drift=drift), self.assertRaisesRegex(
                    shared.JobRehearsalReceiptError, "历史 Claude v1"
                ):
                    shared.build_receipt(self.legacy_root, name, allow_collector_drift=drift)
            with self.subTest(finalize=name), self.assertRaisesRegex(shared.JobRehearsalReceiptError, "历史 Claude v1"):
                shared.finalize(self.legacy_root, name, "new-" + name, client="claude")
            self.assertFalse((self.legacy_root / ("new-" + name)).exists())

    def test_current_certification_issue_and_existing_active_verify_reject_v1(self) -> None:
        with self.formed_environment(), self.assertRaisesRegex(certify_release.ReleaseCertificationError, "历史 Claude v1"):
            certify_release.replay_job_rehearsal(self.legacy_root, Path("receipt.json"), "claude")
        binding = self.binding(self.legacy_root, "receipt.json", self.legacy_receipt)
        path, identity = self.certification(binding)
        payload = json.loads(path.read_text())
        with self.formed_environment(), \
             mock.patch.object(certify_release.pre_a3, "real_chain_coverage", return_value=payload["real_chain_coverage"]), \
             self.assertRaisesRegex(certify_release.ReleaseCertificationError, "历史 Claude v1"):
            certify_release.verify(path, expected_identity=identity, client="claude")

    def test_current_formal_recovery_vc0_and_formal_plan_reject_v1(self) -> None:
        campaign, manifest, current = self.current_contract()
        for expected in (current, self.legacy_receipt["execution_contract"]):
            for function in (shared.assert_formal_compatible, shared.assert_recovery_compatible):
                with self.subTest(consumer=function.__name__, expected=expected["schema_version"]), \
                     self.assertRaisesRegex(shared.JobRehearsalReceiptError, "历史 Claude v1"):
                    function(self.legacy_receipt, expected)
        with self.formed_environment(), self.assertRaisesRegex(closeout.VC0CloseoutError, "历史 Claude v1"):
            closeout._validate_job_rehearsal(campaign, manifest, self.legacy_root, Path("receipt.json"))
        arguments = argparse.Namespace(job_rehearsal_root=self.legacy_root, job_rehearsal_receipt=Path("receipt.json"))
        with self.formed_environment(), self.assertRaisesRegex(ConfigurationError, "历史 Claude v1"):
            campaign_api._job_rehearsal_control(arguments, expected_contract=current, release={})

    def test_v1_cannot_be_a_current_incremental_source_and_no_docker_starts(self) -> None:
        campaign, _manifest, _contract = self.current_contract()
        with self.patched(), self.assertRaisesRegex(shared.JobRehearsalReceiptError, "前序 Job 收据版本"):
            self.collect(campaign, "facts.json", previous_receipt="receipt.json",
                         previous_receipt_root=self.legacy_root, rerun_failed=True)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.binary_calls, 0)
        self.assertFalse((self.evidence / "facts.json").exists())

    def test_formed_schema_and_collector_mixes_never_gain_v2_qualification(self) -> None:
        facts = json.loads((self.legacy_root / "facts.json").read_text())
        changes = {
            "改 facts 名字": lambda value: value.update(schema_version=profile.FACTS_SCHEMA),
            "改 contract 名字": lambda value: value["execution_contract"].update(schema_version=profile.EXECUTION_CONTRACT_SCHEMA),
            "v1 collector 混入 v2": lambda value: value["collector"].update(schema_version=profile.PRODUCER_SCHEMA),
            "v1 collector 混版本": lambda value: value["collector"].update(version="2"),
            "未知版本": lambda value: value.update(schema_version="official-client-job-rehearsal-facts/v9"),
        }
        for name, mutate in changes.items():
            current = copy.deepcopy(facts)
            mutate(current)
            with self.formed_environment(), self.subTest(case=name), self.assertRaises(shared.JobRehearsalReceiptError):
                shared.validate_facts(current, allow_collector_drift=True)
        for field, value in (("schema_version", profile.RECEIPT_SCHEMA), ("producer", {**facts["collector"], "version": "2"})):
            current = copy.deepcopy(self.legacy_receipt)
            current[field] = value
            shared._write_once(self.legacy_root / "mixed.json", current)
            try:
                with self.formed_environment(), self.subTest(receipt=field), self.assertRaises(shared.JobRehearsalReceiptError):
                    shared.replay(self.legacy_root, "mixed.json", client="claude")
            finally:
                (self.legacy_root / "mixed.json").unlink()
        with self.assertRaises(shared.JobRehearsalReceiptError):
            shared.replay(self.legacy_root, "receipt.json", client="codex")


class CurrentV2BoundaryTests(VersionBoundaryFixture):
    """当前 v2 来源与认证实际重放，以及历史来源洗入的拒绝。"""

    def current_pair(self) -> tuple[dict, dict]:
        campaign = self.write_campaign()
        with self.patched():
            facts = self.collect(campaign, "facts.json")
            receipt = shared.finalize(self.evidence, "facts.json", "receipt.json", client="claude")
        return facts, receipt

    def test_valid_current_v2_certification_replays_and_rejects_damaged_source(self) -> None:
        facts, receipt = self.current_pair()
        with self.patched():
            binding = certify_release.replay_job_rehearsal(self.evidence, Path("receipt.json"), "claude")
            profile.rehearsal_verify_current_certification_binding(binding)
        path, identity = self.certification(binding)
        payload = json.loads(path.read_text())
        with self.patched(), mock.patch.object(certify_release.pre_a3, "real_chain_coverage", return_value=payload["real_chain_coverage"]):
            self.assertEqual(certify_release.verify(path, expected_identity=identity, client="claude"), payload)
            tampered = copy.deepcopy(payload)
            tampered["job_rehearsal"]["job_count"] += 1
            tampered.pop("receipt_sha256")
            tampered["receipt_sha256"] = certify_release._fingerprint(tampered, "claude")
            path.write_bytes(shared._canonical(tampered))
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "与绑定不一致"):
                certify_release.verify(path, expected_identity=identity, client="claude")
            path.write_bytes(shared._canonical(payload))
            current = copy.deepcopy(facts)
            current["collector"]["version"] = "1"
            (self.evidence / "facts.json").write_bytes(shared._canonical(current))
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "采集器版本混用"):
                certify_release.verify(path, expected_identity=identity, client="claude")
        self.assertEqual(receipt["schema_version"], profile.RECEIPT_SCHEMA)

    def test_current_noop_recovery_cannot_use_a_v1_source(self) -> None:
        _facts, original = self.current_pair()
        campaign = Path(original["preflight_campaign"]["path"])
        before = len(self.docker_calls())
        with self.patched():
            self.collect(campaign, "noop-facts.json", previous_receipt="receipt.json", rerun_failed=True)
            noop = shared.finalize(self.evidence, "noop-facts.json", "noop-receipt.json", client="claude")
        self.assertEqual(len(self.docker_calls()), before)
        with self.patched():
            self.assertEqual(shared.assert_recovery_compatible(noop, original["execution_contract"]), original)
        raw = (self.legacy_root / "receipt.json").read_bytes()
        binding = {"path": "receipt.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        current = copy.deepcopy(noop)
        current["incremental_noop"].update(source_root=str(self.legacy_root), source_receipt=binding)
        with self.formed_environment(), self.assertRaisesRegex(shared.JobRehearsalReceiptError, "历史 Claude v1"):
            shared.assert_recovery_compatible(current, original["execution_contract"])

    def test_current_collector_cannot_downgrade_even_when_drift_is_allowed(self) -> None:
        facts, _receipt = self.current_pair()
        for changes in (
            {"schema_version": profile.LEGACY_PRODUCER_SCHEMA}, {"version": "1"},
            {"schema_version": profile.LEGACY_PRODUCER_SCHEMA, "version": "1"},
        ):
            current = copy.deepcopy(facts)
            current["collector"].update(changes)
            with self.patched(), self.subTest(changes=changes), self.assertRaisesRegex(shared.JobRehearsalReceiptError, "采集器版本混用"):
                shared.validate_facts(current, allow_collector_drift=True)
        for mutate in (
            lambda value: value["execution_contract"].pop("tool_tree_read_contract"),
            lambda value: value["execution_contract"].update(schema_version=profile.LEGACY_EXECUTION_CONTRACT_SCHEMA),
            lambda value: value.update(schema_version=profile.LEGACY_FACTS_SCHEMA),
        ):
            current = copy.deepcopy(facts)
            mutate(current)
            with self.patched(), self.assertRaises(shared.JobRehearsalReceiptError):
                shared.validate_facts(current, allow_collector_drift=True)

    def test_current_builder_rechecks_the_actual_result_after_a_facts_read_race(self) -> None:
        facts, _receipt = self.current_pair()
        historical = json.loads((self.legacy_root / "facts.json").read_text())
        with self.formed_environment(), mock.patch.object(shared, "_load_json", side_effect=[
            (facts, shared._canonical(facts)), (historical, shared._canonical(historical)),
        ]) as reader, self.assertRaisesRegex(shared.JobRehearsalReceiptError, "历史 Claude v1"):
            shared.build_receipt(self.evidence, "facts.json", allow_collector_drift=True)
        self.assertEqual(reader.call_count, 2)


class CodexOriginalBytesTests(unittest.TestCase):
    """基线 Codex 事实、运行时摘要及收据规范字节保持原样。"""

    def test_original_codex_facts_receipt_and_canonical_bytes_do_not_change(self) -> None:
        path = Path(__file__).with_name("fixtures") / "codex_job_rehearsal_original.json.gz"
        bundle = json.loads(gzip.decompress(path.read_bytes()))
        provenance = bundle["provenance"]
        self.assertEqual(provenance["base_commit"], "cfc3ae053f8582486e1c4dc76b759e18d370f406")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            originals = {}
            for record in provenance["artifacts"]:
                raw = bundle["artifacts"][record["path"]].encode("utf-8")
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])
                self.assertEqual(len(raw), record["bytes"])
                (root / record["path"]).write_bytes(raw)
                (root / record["path"]).chmod(0o600)
                originals[record["path"]] = raw
            with mock.patch.object(shared, "_run", side_effect=AssertionError("重放不得启动探针")):
                replayed = shared.replay(root, "receipt.json", client="codex")
                rebuilt = shared.build_receipt(root, "facts.json", allow_collector_drift=True)
            self.assertEqual(shared._canonical(replayed), originals["receipt.json"])
            self.assertEqual(shared._canonical(rebuilt), originals["receipt.json"])
            self.assertEqual(replayed["producer"], provenance["producer"])
            self.assertNotIn("tool_tree_read_contract", replayed["execution_contract"])
            facts = json.loads(originals["facts.json"])
            self.assertEqual(shared._runtime_identity(facts), facts["runtime_identity_sha256"])
            shared.assert_formal_compatible(replayed, replayed["execution_contract"])
            self.assertEqual({name: (root / name).read_bytes() for name in originals}, originals)


if __name__ == "__main__":
    unittest.main()
