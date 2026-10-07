"""Claude 升级两条前序证据导入路径（Claude 升级控制面设计 B2-09）。

验收三点（设计 B2-09）：
* 2.1.226 Store 作 ``regression_fixture`` 导入通过：真实 Store 一例由环境变量 ``CLAUDE_2_1_226_STORE`` 指定（未设置即
  跳过），测试先把它复制进临时目录再导入，前后对原 Store 做目录快照比对；合成 v1 linux/amd64 前序默认覆盖同样的断言。
* 导入的对象不可被正式引用：消费端核验对每个导入对象都给出 ③ 拒绝，本 Campaign 引用导入的 EvidencePackage 写
  EvidenceFact 被拒。
* 伪称同身份被拒：两组七项声明由编排器从双方冻结字段导出、Store 再独立复算——申请 read_only_reuse 被拒；总计划
  冻结的前序身份摘要与前序 Store 不符被拒；绕过编排器把源身份声明成目标身份的登记被 Store 拒绝（两种模式都拒）。

另覆盖 read_only_reuse 正向链路（同身份 v2 前序：EvidencePackage 获授权、VC-1 复用计划复用全部 official Job、本
Campaign 可写 EvidenceFact）、编排器门禁（只用于 Formal 与总计划冻结的前序、租约、Store 同源、前序 Store 不得是本
Store、前序没有官方证据）、幂等重跑与前序 Store 搬家、总计划 ``predecessor`` 字段形状与命令行。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_predecessor as predecessor_module
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.contracts import derive_identity_conditions
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import SHA_B, ManagedToolTree, SyntheticCampaign
from tools.official_client_control.tests.test_predecessor_import import make_read_only, make_writable, snapshot_tree

REAL_STORE_ENV = "CLAUDE_2_1_226_STORE"
TARGET_ID = "claude-code-2_1_285-b209-target"
PREDECESSOR_ID = "synthetic-1.0.0-predecessor"
OFFICIAL_JOBS = ("official-transport-baseline", "official-tui", "official-synthetic-retry")
# 合成目标：与合成前序同版本、同官方产物、同入口与隐私条件，只有平台为 arm64（Store v2 的组织与模型目录取夹具缺省）。
SYNTHETIC_TARGET = {
    "target_version": "1.0.0",
    "platforms": ["linux/arm64"],
    "entrypoints": ["synthetic-cli"],
    "default_conditions": ["privacy=default"],
}


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _import_argv(campaign_dir: Path, store_root: Path, campaign_id: str, mode: str) -> list[str]:
    return [
        "import-predecessor-evidence", "--campaign-dir", str(campaign_dir), "--predecessor-store", str(store_root),
        "--predecessor-campaign", campaign_id, "--mode", mode, "--approved-by", "synthetic-operator",
    ]


class _PredecessorCase(unittest.TestCase):
    """受管树副本（Store 在副本上现算工具身份）、前序 Store 与带受管 Store 的 Claude 目标 Campaign。"""

    tree: ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        self.addCleanup(self.tree.restore)
        self._locked: list[Path] = []
        self.addCleanup(lambda: [make_writable(root) for root in self._locked])

    # ------------------------------------------------------------------ 构造

    def make_predecessor(self, name: str = "predecessor", *, schema: str = "v1", **overrides: Any) -> SyntheticCampaign:
        root = self.workspace / name
        root.mkdir()
        fixture = SyntheticCampaign(root, schema=schema)
        fixture.campaign_id = PREDECESSOR_ID
        fixture.bootstrap_and_campaign(**overrides)
        fixture.record_discovery_and_evidence()
        return fixture

    def make_target(
        self,
        name: str = "target",
        *,
        campaign_mode: str = "formal",
        predecessor: dict[str, Any] | None = None,
        store_overrides: dict[str, Any] | None = None,
        configuration_overrides: dict[str, Any] | None = None,
        jobs: tuple[str, ...] = OFFICIAL_JOBS,
    ) -> tuple[Path, SyntheticCampaign]:
        """Claude 目标 Campaign：Store Campaign v2 放在 ``<campaign_dir>/control-store``，总计划与它同源。"""

        identity = {**SYNTHETIC_TARGET, **(store_overrides or {})}
        root = self.workspace / f"{name}-store-build"
        root.mkdir()
        fixture = SyntheticCampaign(root, schema="v2")
        fixture.campaign_id = TARGET_ID
        fixture.bootstrap_and_campaign(**identity)
        campaign_dir = self.workspace / name / "evidence" / "campaigns" / TARGET_ID
        campaign_dir.mkdir(parents=True)
        shutil.move(str(fixture.store_root), str(claude_upgrade_store.campaign_store_root(campaign_dir)))
        store_campaign = self.target_store(campaign_dir).load_campaign(TARGET_ID)
        derived = derive_identity_conditions(store_campaign)[0]
        configuration = {
            "platform": derived["platform"],
            "entrypoint": derived["entrypoint"],
            "privacy_mode": derived["privacy_mode"],
            "oauth_org_id": derived["oauth_org_id"],
            "model_catalog_sha256": derived["model_catalog_sha256"],
            **(configuration_overrides or {}),
        }
        manifest = {
            "schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": TARGET_ID,
            "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": campaign_mode,
            "campaign_purpose": "validation_only", "baseline_version": "0.9.0",
            "target_version": derived["target_version"], "target_sha256": "a" * 64,
            "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": "b" * 64, "binary_equivalence": "npm=gcs"},
            "baseline_identity": {}, "tool_identity": {"client": "claude"}, "control_receipts": {}, "inputs": {},
            "configuration": configuration, "required_rules": [], "coverage_plan": {},
            "jobs": [{"id": job_id, "phase": "official"} for job_id in jobs] + [{"id": "candidate-x", "phase": "candidate"}],
            "vc_control": {}, "predecessor": predecessor,
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign_dir / "campaign.json").write_bytes(raw)
        (campaign_dir / "campaign.json").chmod(0o600)
        (campaign_dir / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        return campaign_dir, fixture

    @staticmethod
    def target_store(campaign_dir: Path) -> ControlStore:
        return ControlStore(claude_upgrade_store.campaign_store_root(campaign_dir))

    @staticmethod
    def binding(store: ControlStore, campaign_id: str, *, sha256: str | None = None) -> dict[str, Any]:
        campaign = store.load_campaign(campaign_id)
        return {
            "mode": "read_only_reuse",
            "campaign_id": campaign_id,
            "campaign_identity_sha256": sha256 or campaign["identity_sha256"],
        }

    def lock(self, root: Path) -> None:
        self._locked.append(root)
        make_read_only(root)

    def run_import(self, campaign_dir: Path, store_root: Path, campaign_id: str, mode: str) -> dict[str, Any]:
        code, stdout, stderr = _run_main(_import_argv(campaign_dir, store_root, campaign_id, mode))
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def assert_import_rejected(self, campaign_dir: Path, store_root: Path, campaign_id: str, mode: str, message: str) -> None:
        code, _stdout, stderr = _run_main(_import_argv(campaign_dir, store_root, campaign_id, mode))
        self.assertEqual(code, 1, stderr)
        self.assertRegex(stderr, message)

    def write_target_evidence(self, campaign_dir: Path, fixture: SyntheticCampaign, package_ref: dict[str, Any]) -> dict[str, Any]:
        """本 Campaign 自己的 Discovery 加一条引用 ``package_ref`` 的 EvidenceFact（消费端核验在这里判定）。"""

        store = self.target_store(campaign_dir)
        discovery = store.append_fact(
            TARGET_ID,
            "discovery_recorded",
            {
                "version": store.load_campaign(TARGET_ID)["target_version"],
                "source": "synthetic-fixture",
                "discovered_at_utc": fixture._time(),
                "tool_sha256": fixture.tool_sha256,
                "artifact_refs": [fixture.official_binding],
            },
            fixture._time(),
        )
        return store.append_fact(
            TARGET_ID,
            "evidence_recorded",
            {"discovery_fact_ref": discovery, "evidence_package_ref": package_ref},
            fixture._time(),
        )


class RegressionFixtureTests(_PredecessorCase):
    """合成 v1 linux/amd64 前序 → arm64 Campaign v2：与真实 2.1.226 一例同样的三点断言（默认运行）。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor()
        self.predecessor_root = self.predecessor.store_root
        source = ControlStore.open_read_only(self.predecessor_root).load_campaign(PREDECESSOR_ID)
        self.campaign_dir, self.fixture = self.make_target(
            predecessor=self.binding(ControlStore.open_read_only(self.predecessor_root), PREDECESSOR_ID)
        )
        self.source_identity = source["identity_sha256"]

    def test_regression_fixture_imports_and_grants_nothing(self) -> None:
        self.lock(self.predecessor_root)
        before = snapshot_tree(self.predecessor_root)
        summary = self.run_import(self.campaign_dir, self.predecessor_root, PREDECESSOR_ID, "regression_fixture")
        self.assertEqual(snapshot_tree(self.predecessor_root), before)
        self.assertEqual((summary["schema_version"], summary["mode"]), (predecessor_module.IMPORT_SUMMARY_SCHEMA, "regression_fixture"))
        self.assertEqual((summary["verification"], summary["live_request_count"], summary["vc1_reuse"]), ("passed", 0, None))
        self.assertEqual(summary["predecessor"]["campaign_identity_sha256"], self.source_identity)
        self.assertEqual(
            [(item["item"], item["reason"]) for item in summary["identity_differences"]],
            [("platform", "different"), ("oauth_org_id", "unprovable_in_source"), ("model_catalog_sha256", "unprovable_in_source")],
        )
        self.assertEqual(sorted(item["fact_kind"] for item in summary["imported_facts"]), ["discovery_recorded", "evidence_recorded"])
        self.assertEqual(summary["registration"]["copied_objects"], len(summary["imported_objects"]))
        # ② 导入对象不可被正式引用：逐个对象都是 ③（回归夹具收据不授予任何正式引用权限）。
        self.assertTrue(summary["formal_references"])
        for item in summary["formal_references"]:
            self.assertFalse(item["authorized"])
            self.assertRegex(item["denial"], "^③ 导入收据 mode=regression_fixture")
        with self.assertRaisesRegex(ControlError, "EvidencePackage 是登记导入的前序证据，未获授权引用：③"):
            self.write_target_evidence(self.campaign_dir, self.fixture, summary["evidence_package_ref"])
        store = self.target_store(self.campaign_dir)
        self.assertEqual(store_receipts.fixture_only_imported_objects(store), {
            (item["object_kind"], item["sha256"]) for item in summary["imported_objects"]
        })
        store.replay()

    def test_claiming_same_identity_is_rejected(self) -> None:
        """伪称同身份：申请只读复用、伪造总计划身份摘要、绕过编排器伪造声明，都被拒绝。"""

        self.assert_import_rejected(
            self.campaign_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse",
            "登记前序导入失败：read_only_reuse 要求七项全部可证且逐项相等：platform",
        )
        forged_dir, _ = self.make_target(
            "forged", predecessor=self.binding(ControlStore.open_read_only(self.predecessor_root), PREDECESSOR_ID, sha256="c" * 64)
        )
        self.assert_import_rejected(
            forged_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse", "身份摘要与总计划冻结的不一致"
        )
        store = self.target_store(self.campaign_dir)
        predecessor = ControlStore.open_read_only(self.predecessor_root)
        source = predecessor.load_campaign(PREDECESSOR_ID)
        evidence = predecessor_module.select_official_evidence(predecessor, PREDECESSOR_ID)
        for mode in ("read_only_reuse", "regression_fixture"):
            with self.subTest(mode=mode):
                forged = predecessor_module.build_import_payload(
                    store.load_campaign(TARGET_ID), predecessor, source, evidence,
                    mode=mode, approved_by="synthetic-operator", approved_at_utc="2026-10-07T00:00:00Z",
                )
                forged["source_identity_conditions"] = dict(forged["identity_conditions"])
                with self.assertRaisesRegex(ControlError, "七项身份声明与冻结字段独立导出的不一致"):
                    store_receipts.register_predecessor_import(store, forged)
        self.assertEqual(store.list_receipt_refs("predecessor_import"), [])
        self.assertEqual(store.list_object_refs("predecessor_import"), [])

    def test_rerun_and_moved_store_reuse_registration(self) -> None:
        first = self.run_import(self.campaign_dir, self.predecessor_root, PREDECESSOR_ID, "regression_fixture")
        again = self.run_import(self.campaign_dir, self.predecessor_root, PREDECESSOR_ID, "regression_fixture")
        self.assertFalse(first["registration"]["reused"])
        self.assertTrue(again["registration"]["reused"])
        self.assertEqual((again["import_ref"], again["receipt_ref"]), (first["import_ref"], first["receipt_ref"]))
        self.assertTrue(again["receipt_reused"])
        moved = self.workspace / "archive" / "moved-store"
        moved.parent.mkdir()
        shutil.move(str(self.predecessor_root), str(moved))
        relocated = self.run_import(self.campaign_dir, moved, PREDECESSOR_ID, "regression_fixture")
        self.assertEqual((relocated["import_ref"], relocated["receipt_ref"]), (first["import_ref"], first["receipt_ref"]))
        self.assertEqual(relocated["predecessor"]["store_root"], str(moved.resolve()))
        store = self.target_store(self.campaign_dir)
        self.assertEqual(len(store.list_object_refs("predecessor_import")), 1)
        self.assertEqual(len(store.list_receipt_refs("predecessor_import")), 1)


class ReadOnlyReuseTests(_PredecessorCase):
    """同身份 v2 前序（arm64、同组织与模型目录）只读复用：EvidencePackage 获授权，VC-1 复用全部 official Job。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor(schema="v2", **SYNTHETIC_TARGET)
        self.predecessor_root = self.predecessor.store_root

    def reuse_binding(self, **options: Any) -> dict[str, Any]:
        return self.binding(ControlStore.open_read_only(self.predecessor_root), PREDECESSOR_ID, **options)

    def test_read_only_reuse_authorizes_and_plans_vc1(self) -> None:
        campaign_dir, fixture = self.make_target(predecessor=self.reuse_binding())
        summary = self.run_import(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse")
        self.assertEqual((summary["mode"], summary["identity_differences"]), ("read_only_reuse", []))
        self.assertEqual(summary["source_conditions"], summary["target_conditions"])
        package = summary["evidence_package_ref"]
        authorized = {item["object_ref"]["object_kind"]: item["authorized"] for item in summary["formal_references"]}
        self.assertTrue(authorized["evidence_package"])
        self.assertFalse(authorized["operational_evidence"])  # 白名单外类型照样 ④ 拒绝
        plan = summary["vc1_reuse"]
        self.assertEqual(plan["schema_version"], predecessor_module.VC1_REUSE_PLAN_SCHEMA)
        self.assertEqual((plan["execute_item_ids"], plan["reuse_item_ids"]), ([], list(OFFICIAL_JOBS)))
        self.assertEqual((plan["evidence_package_ref"], plan["live_request_count"]), (package, 0))
        self.assertEqual(plan["import_ref"], summary["import_ref"])
        manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
        self.assertEqual(predecessor_module.vc1_reuse_plan(campaign_dir, manifest), plan)
        # 正式引用：本 Campaign 用导入的 EvidencePackage 写 EvidenceFact 成立（产出工具由导入收据担保）。
        self.write_target_evidence(campaign_dir, fixture, package)
        store = self.target_store(campaign_dir)
        self.assertEqual(store_receipts.fixture_only_imported_objects(store), set())
        store.replay()
        again = self.run_import(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse")
        self.assertEqual((again["receipt_ref"], again["receipt_reused"]), (summary["receipt_ref"], True))

    def test_read_only_reuse_guards(self) -> None:
        cases = {
            "总计划没冻结前序": ({"predecessor": None}, "总计划没有冻结前序只读复用"),
            "preflight_only": ({"campaign_mode": "preflight_only", "predecessor": self.reuse_binding()}, "只用于 Formal Campaign"),
            "冻结的是另一个前序": (
                {"predecessor": {**self.reuse_binding(), "campaign_id": "synthetic-other"}}, "不是总计划冻结的前序 Campaign"
            ),
            "身份摘要伪造": ({"predecessor": self.reuse_binding(sha256="d" * 64)}, "身份摘要与总计划冻结的不一致"),
            "总计划没有 official Job": ({"predecessor": self.reuse_binding(), "jobs": ()}, "没有可复用的 official Job"),
        }
        for index, (label, (options, message)) in enumerate(cases.items()):
            with self.subTest(label=label):
                campaign_dir, _fixture = self.make_target(f"guard-{index}", **options)
                self.assert_import_rejected(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse", message)
                self.assertEqual(self.target_store(campaign_dir).list_object_refs("predecessor_import"), [])
        # 身份不同的前序（平台 amd64）：总计划冻结了它，Store 仍按七项逐项相等拒绝签发。
        other = self.make_predecessor("amd64-predecessor", schema="v2", **{**SYNTHETIC_TARGET, "platforms": ["linux/amd64"]})
        campaign_dir, _fixture = self.make_target(
            "amd64", predecessor=self.binding(ControlStore.open_read_only(other.store_root), PREDECESSOR_ID)
        )
        self.assert_import_rejected(
            campaign_dir, other.store_root, PREDECESSOR_ID, "read_only_reuse", "read_only_reuse 要求七项全部可证且逐项相等：platform"
        )

    def test_vc1_reuse_plan_requires_finalized_import(self) -> None:
        campaign_dir, _fixture = self.make_target(predecessor=self.reuse_binding())
        manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
        with self.assertRaisesRegex(ConfigurationError, "还没有对应的导入登记"):
            predecessor_module.vc1_reuse_plan(campaign_dir, manifest)
        store = self.target_store(campaign_dir)
        predecessor = ControlStore.open_read_only(self.predecessor_root)
        payload = predecessor_module.build_import_payload(
            store.load_campaign(TARGET_ID), predecessor, predecessor.load_campaign(PREDECESSOR_ID),
            predecessor_module.select_official_evidence(predecessor, PREDECESSOR_ID),
            mode="read_only_reuse", approved_by="synthetic-operator", approved_at_utc="2026-10-07T00:00:00Z",
        )
        store_receipts.register_predecessor_import(store, payload)
        with self.assertRaisesRegex(ConfigurationError, "尚未 finalize"):
            predecessor_module.vc1_reuse_plan(campaign_dir, manifest)
        # 同一前序同一模式已登记过：再导入沿用这份登记、补签收据。
        summary = self.run_import(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "read_only_reuse")
        self.assertTrue(summary["registration"]["reused"])
        self.assertEqual(summary["registration"]["approved_at_utc"], "2026-10-07T00:00:00Z")
        self.assertEqual(predecessor_module.vc1_reuse_plan(campaign_dir, manifest)["reuse_item_ids"], list(OFFICIAL_JOBS))


class OrchestratorGateTests(_PredecessorCase):
    """导入前的编排器门禁：租约、Store 同源、前序 Store 位置、前序证据。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor()
        self.predecessor_root = self.predecessor.store_root

    def test_active_lease_blocks_import(self) -> None:
        from tools.official_client_capture import incremental_recovery

        campaign_dir, _fixture = self.make_target()
        deadline = incremental_recovery.WallClockDeadline(30)
        deadline.heartbeat_seconds = 1
        lease = claude_upgrade.CampaignLease(
            campaign_dir, phase="official", candidate_id=None, deadline=deadline, command="capture-official", campaign_id=TARGET_ID,
        )
        lease.acquire()
        try:
            self.assert_import_rejected(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "regression_fixture", "未释放的执行租约")
        finally:
            lease.release()
        self.run_import(campaign_dir, self.predecessor_root, PREDECESSOR_ID, "regression_fixture")

    def test_store_binding_and_predecessor_checks(self) -> None:
        campaign_dir, _fixture = self.make_target()
        own_store = claude_upgrade_store.campaign_store_root(campaign_dir)
        self.assert_import_rejected(campaign_dir, own_store, PREDECESSOR_ID, "regression_fixture", "不得指向本 Store")
        self.assert_import_rejected(campaign_dir, self.predecessor_root, "synthetic-missing", "regression_fixture", "Campaign 不存在")
        empty = self.make_target("no-evidence")[0]
        bare_root = self.workspace / "bare"
        bare_root.mkdir()
        bare = SyntheticCampaign(bare_root, schema="v1")
        bare.campaign_id = PREDECESSOR_ID
        bare.bootstrap_and_campaign()
        self.assert_import_rejected(empty, bare.store_root, PREDECESSOR_ID, "regression_fixture", "没有官方证据事实 evidence_recorded")
        drifted = self.make_target("drifted", configuration_overrides={"oauth_org_id": "synthetic-org-9999"})[0]
        self.assert_import_rejected(drifted, self.predecessor_root, PREDECESSOR_ID, "regression_fixture", "冻结身份与总计划不同源：oauth_org_id")
        missing = self.make_target("missing-store")[0]
        shutil.rmtree(claude_upgrade_store.campaign_store_root(missing))
        self.assert_import_rejected(missing, self.predecessor_root, PREDECESSOR_ID, "regression_fixture", "缺少受管 Store")
        with self.assertRaises(SystemExit):
            _run_main(_import_argv(campaign_dir, Path("relative/store"), PREDECESSOR_ID, "regression_fixture"))


class PlanBindingTests(unittest.TestCase):
    """总计划 ``predecessor`` 字段：null 或冻结前序身份的只读复用声明。"""

    def test_binding_shapes(self) -> None:
        valid = {"mode": "read_only_reuse", "campaign_id": "claude-old", "campaign_identity_sha256": "e" * 64}
        self.assertIsNone(predecessor_module.validate_predecessor_binding(None, campaign_id="claude-new"))
        self.assertEqual(predecessor_module.validate_predecessor_binding(valid, campaign_id="claude-new"), valid)
        cases = {
            "字段不闭合": ({**valid, "store_root": "/data/store"}, "必须为 null"),
            "回归夹具不入总计划": ({**valid, "mode": "regression_fixture"}, "回归夹具"),
            "指向自身": ({**valid, "campaign_id": "claude-new"}, "本 Campaign 自身"),
            "摘要非法": ({**valid, "campaign_identity_sha256": "x"}, "campaign_identity_sha256 非法"),
            "不是对象": (["claude-old"], "必须为 null"),
        }
        for label, (value, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ConfigurationError, message):
                predecessor_module.validate_predecessor_binding(value, campaign_id="claude-new")

    def test_command_line_registers_subcommand(self) -> None:
        parser = claude_upgrade._build_parser()
        arguments = parser.parse_args(_import_argv(Path("/data/c"), Path("/data/s"), "claude-old", "regression_fixture"))
        self.assertEqual(
            (arguments.command, arguments.mode, arguments.predecessor_store, arguments.approved_by),
            ("import-predecessor-evidence", "regression_fixture", Path("/data/s"), "synthetic-operator"),
        )
        self.assertNotIn("import-predecessor-evidence", claude_upgrade.CLAUDE_OWN_SUBCOMMANDS)


@unittest.skipUnless(os.environ.get(REAL_STORE_ENV), f"未设置 {REAL_STORE_ENV}：真实 2.1.226 Store 一例跳过")
class RealPredecessorStoreTests(_PredecessorCase):
    """真实 2.1.226 x86_64 Store（Campaign v1、linux/amd64）：复制进临时目录后作回归夹具导入 2.1.285 arm64 Campaign。"""

    def test_2_1_226_store_as_regression_fixture(self) -> None:
        original = Path(os.environ[REAL_STORE_ENV]).resolve()
        before = snapshot_tree(original)
        copy = self.workspace / "predecessor-2.1.226" / "control-store"
        copy.parent.mkdir()
        shutil.copytree(original, copy, copy_function=shutil.copy2)
        predecessor = ControlStore.open_read_only(copy)
        candidates = [
            path.stem
            for path in sorted((copy / "campaigns").glob("*.json"))
            if any(fact["fact_kind"] == "evidence_recorded" for fact in predecessor.list_facts(path.stem, "evidence"))
        ]
        self.assertTrue(candidates, "真实 Store 没有可导入的 EvidenceFact")
        source_id = candidates[-1]
        source = predecessor.load_campaign(source_id)
        self.assertEqual((source["target_version"], source["platforms"]), ("2.1.226", ["linux/amd64"]))
        campaign_dir, fixture = self.make_target(
            store_overrides={
                "target_version": "2.1.285",
                "persona": source["persona"],
                "entrypoints": source["entrypoints"],
                "default_conditions": source["default_conditions"],
                "platforms": ["linux/arm64"],
            },
            predecessor=self.binding(predecessor, source_id),
        )
        self.lock(copy)
        summary = self.run_import(campaign_dir, copy, source_id, "regression_fixture")
        # ① 回归夹具导入通过：收据记录逐项差异（版本、官方产物、平台不同，组织与模型目录在 v1 源上不可证）。
        self.assertEqual(summary["verification"], "passed")
        self.assertEqual(
            [(item["item"], item["reason"]) for item in summary["identity_differences"]],
            [
                ("target_version", "different"),
                ("official_artifacts", "different"),
                ("platform", "different"),
                ("oauth_org_id", "unprovable_in_source"),
                ("model_catalog_sha256", "unprovable_in_source"),
            ],
        )
        platform = next(item for item in summary["identity_differences"] if item["item"] == "platform")
        self.assertEqual((platform["source"], platform["target"]), ("linux/amd64", "linux/arm64"))
        # ② 导入对象不可被正式引用。
        self.assertTrue(all(not item["authorized"] and item["denial"].startswith("③") for item in summary["formal_references"]))
        with self.assertRaises(ControlError):
            self.write_target_evidence(campaign_dir, fixture, summary["evidence_package_ref"])
        # ③ 伪称同身份被拒：申请只读复用（总计划冻结了这个前序、身份摘要真实）仍被 Store 拒绝。
        self.assert_import_rejected(campaign_dir, copy, source_id, "read_only_reuse", "read_only_reuse 要求七项全部可证且逐项相等")
        store = self.target_store(campaign_dir)
        forged = predecessor_module.build_import_payload(
            store.load_campaign(TARGET_ID), predecessor, source, predecessor_module.select_official_evidence(predecessor, source_id),
            mode="read_only_reuse", approved_by="synthetic-operator", approved_at_utc="2026-10-07T00:00:00Z",
        )
        forged["source_identity_conditions"] = dict(forged["identity_conditions"])
        with self.assertRaisesRegex(ControlError, "七项身份声明与冻结字段独立导出的不一致"):
            store_receipts.register_predecessor_import(store, forged)
        self.assertEqual(len(store.list_receipt_refs("predecessor_import")), 1)
        store.replay()
        self.assertEqual(snapshot_tree(original), before)


if __name__ == "__main__":
    unittest.main()
