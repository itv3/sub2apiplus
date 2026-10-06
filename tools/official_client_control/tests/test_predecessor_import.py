"""Claude 升级 B1-14：前序证据导入收据与消费端核验的验收用例。

设计验收逐条对应：
* 未 finalize 的导入被引用即拒绝（消费端核验 ②）；
* regression_fixture 收据不授予正式引用权限（③）；
* 官方证据白名单以外的类型（事实、operational_evidence、收据）被拒（④）；
* 合成 v1 linux/amd64 Store 以 regression_fixture 导入 v2 linux/arm64 Campaign：收据记录 platform 差异与两项不可证；
  同一 Store 申请 read_only_reuse 被拒；
* 源 org A、目标 org B、两组声明都写 A：两种模式都拒绝（登记时与 finalizer 独立核验各证一次）；
* read_only_reuse 正向链路：目标 Campaign 用导入的 EvidencePackage 与两份台账走到 Evidence 批准，整库重放与外部
  证据核验通过。

补充：前序 Store 去掉写权限后登记、finalize、复核全程只读（目录快照前后一致）；finalize 幂等（含工具演进后）；
前序 Store 搬走后重放不受影响、finalize／复核可给新位置；store_root 不得指向本 Store；导入清单必须闭合、可导入且
属于前序 Campaign；前序材料缺失或漂移即拒绝；回归夹具的外部证据不进入本 Store 的外部核验；伪造收据重放失败且不
授权；前序与目标工具身份相同时回归夹具照样被拒、已当本地证据用过的包不得再登记为导入；CLI 三个子命令。真实
2.1.226 Store 一例由环境变量 ``CLAUDE_2_1_226_STORE`` 指定（未设置即跳过），只读使用，前后做目录快照比对；合成 v1
用例默认覆盖同样的断言。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    write_once,
)
from tools.official_client_control.cli import build_parser, execute
from tools.official_client_control.contracts import (
    CAMPAIGN_SCHEMA,
    IDENTITY_CONDITION_SOURCE_FIELDS,
    PREDECESSOR_IMPORT_RECEIPT_SCHEMA,
    campaign_identity_sha256,
    derive_identity_conditions,
    rule_migration_ledger_identity_sha256,
    validate_receipt_document,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    authorize_cross_campaign_reference,
    build_predecessor_import_receipt,
    capture_predecessor_material,
    cross_campaign_denial,
    finalize_predecessor_import,
    finalize_predecessor_import_outcome,
    fixture_only_imported_objects,
    predecessor_object_closure,
    register_predecessor_import,
    replay_receipt,
    verify_predecessor_import,
)
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import (
    SHA_B,
    ManagedToolTree,
    SyntheticCampaign,
)

PROBE = "official_client_capture/claude_upgrade_b112_probe.json"
REAL_STORE_ENV = "CLAUDE_2_1_226_STORE"
PREDECESSOR_ID = "synthetic-1.0.0-predecessor"
TARGET_ID = "synthetic-1.0.0-target"


def snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """目录树快照：相对路径 → (类型, 权限, 大小, mtime_ns, 内容摘要)；前后相等即证明没有任何写入。"""

    entries: dict[str, tuple[Any, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        regular = stat.S_ISREG(info.st_mode)
        entries[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode),
            info.st_size if regular else None,
            info.st_mtime_ns,
            sha256_file(path) if regular else None,
        )
    return entries


def make_read_only(root: Path) -> None:
    """去掉整棵目录树的写权限（目录 0500、文件 0400）：任何写入都会在操作系统层面失败。"""

    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)


def make_writable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o700)
    for path in sorted(root.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)


class _ImportCase(unittest.TestCase):
    """两份 Store：前序（证据已在 VC-1～VC-3 第一道批准走完）与目标（另一份工具身份下建立的 Campaign v2）。"""

    tree: ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        self.addCleanup(self.tree.restore)
        self._locked_roots: list[Path] = []
        self.addCleanup(self._unlock_roots)

    def _unlock_roots(self) -> None:
        for root in self._locked_roots:
            make_writable(root)

    def lock(self, root: Path) -> None:
        self._locked_roots.append(root)
        make_read_only(root)

    def make_fixture(
        self, name: str, *, schema: str, campaign_id: str, **overrides: Any
    ) -> SyntheticCampaign:
        root = self.workspace / name
        root.mkdir()
        fixture = SyntheticCampaign(root, schema=schema)
        fixture.campaign_id = campaign_id
        fixture.bootstrap_and_campaign(**overrides)
        return fixture

    def make_predecessor(self, *, schema: str = "v2", **overrides: Any) -> SyntheticCampaign:
        fixture = self.make_fixture(
            "predecessor", schema=schema, campaign_id=PREDECESSOR_ID, **overrides
        )
        fixture.discovery_and_evidence()
        return fixture

    def make_target(
        self, *, schema: str = "v2", change_tools: bool = True, **overrides: Any
    ) -> SyntheticCampaign:
        # 缺省在改过的受管代码下建立目标 Campaign：前序 EvidencePackage 的产出工具因而不同于目标的有效工具身份。
        if change_tools:
            self.tree.write(PROBE, b'{"probe": "b1-14-target"}\n')
        return self.make_fixture("target", schema=schema, campaign_id=TARGET_ID, **overrides)

    @staticmethod
    def import_payload(
        predecessor: SyntheticCampaign,
        target: SyntheticCampaign,
        *,
        mode: str = "read_only_reuse",
        roots: list[dict[str, Any]] | None = None,
        fact_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """导入登记 payload；两组七项声明默认取双方冻结字段的导出值（测试按需改写成错误声明）。"""

        source = predecessor.store.load_campaign(predecessor.campaign_id)
        campaign = target.store.load_campaign(target.campaign_id)
        roots = [predecessor.references["atomic-assertion-ledger"]] if roots is None else roots
        return {
            "client": "claude",
            "campaign_id": target.campaign_id,
            "predecessor": {
                "store_root": str(predecessor.store.root),
                "campaign_id": predecessor.campaign_id,
                "campaign_identity_sha256": source["identity_sha256"],
            },
            "imported_objects": predecessor_object_closure(predecessor.store, roots),
            "imported_fact_refs": sorted(fact_refs or [], key=canonical_sha256),
            "identity_conditions": derive_identity_conditions(campaign)[0],
            "source_identity_conditions": derive_identity_conditions(source)[0],
            "mode": mode,
            "approved_by": "synthetic-operator",
            "approved_at_utc": target._time(),
        }

    @staticmethod
    def record_discovery(target: SyntheticCampaign) -> dict[str, Any]:
        """目标 Campaign 自己的 Discovery（不封存比较策略：同内容对象已随导入复制进来）。"""

        return target.store.append_fact(
            target.campaign_id,
            "discovery_recorded",
            {
                "version": "1.0.0",
                "source": "synthetic-fixture",
                "discovered_at_utc": target._time(),
                "tool_sha256": target.tool_sha256,
                "artifact_refs": [target.official_binding],
            },
            target._time(),
        )

    @staticmethod
    def record_evidence(
        target: SyntheticCampaign, discovery_ref: dict[str, Any], package_ref: dict[str, Any]
    ) -> dict[str, Any]:
        return target.store.append_fact(
            target.campaign_id,
            "evidence_recorded",
            {"discovery_fact_ref": discovery_ref, "evidence_package_ref": package_ref},
            target._time(),
        )

    def register_and_finalize(
        self, target: SyntheticCampaign, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        import_ref = register_predecessor_import(target.store, payload)["import_ref"]
        receipt_ref = finalize_predecessor_import(target.store, import_ref, target._time())
        return import_ref, receipt_ref


class ReadOnlyReuseTests(_ImportCase):
    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor()
        self.target = self.make_target()
        self.store = self.target.store

    def payload(self, **options: Any) -> dict[str, Any]:
        return self.import_payload(self.predecessor, self.target, **options)

    def test_read_only_reuse_authorizes_whitelisted_evidence(self) -> None:
        predecessor = self.predecessor.references
        payload = self.payload(fact_refs=[predecessor["evidence-fact"], predecessor["evidence-approval"]])
        registered = register_predecessor_import(self.store, payload)
        self.assertFalse(registered["reused"])
        self.assertEqual(len(registered["copied_objects"]), 4)
        self.assertEqual(registered["reused_objects"], [])
        outcome = finalize_predecessor_import_outcome(
            self.store, registered["import_ref"], self.target._time()
        )
        self.assertFalse(outcome["reused"])
        receipt = self.store.load_receipt(outcome["receipt_ref"])
        self.assertEqual(receipt["schema_version"], PREDECESSOR_IMPORT_RECEIPT_SCHEMA)
        self.assertEqual(receipt["mode"], "read_only_reuse")
        self.assertEqual(receipt["identity_differences"], [])
        self.assertEqual(receipt["source_conditions"], receipt["target_conditions"])
        self.assertEqual(receipt["condition_source_fields"], IDENTITY_CONDITION_SOURCE_FIELDS)
        self.assertEqual(
            sorted(item["fact_kind"] for item in receipt["imported_facts"]),
            ["evidence_approved", "evidence_recorded"],
        )
        document = json.loads(receipt["source_campaign_document"])
        self.assertEqual(document["campaign_id"], PREDECESSOR_ID)
        self.assertEqual(campaign_identity_sha256(document), payload["predecessor"]["campaign_identity_sha256"])

        package_ref = predecessor["evidence-package"]
        package = self.store.load_object(package_ref)["payload"]
        self.assertNotEqual(package["producer_tool_sha256"], self.target.tool_sha256)
        discovery = self.record_discovery(self.target)
        evidence = self.record_evidence(self.target, discovery, package_ref)
        classification = self.store.append_fact(
            TARGET_ID,
            "rule_classification_recorded",
            {
                "evidence_fact_ref": evidence,
                "evidence_package_ref": package_ref,
                "rule_migration_ledger_ref": predecessor["rule-migration-ledger"],
                "atomic_assertion_ledger_ref": predecessor["atomic-assertion-ledger"],
            },
            self.target._time(),
        )
        self.store.append_fact(
            TARGET_ID,
            "evidence_approved",
            {
                "evidence_fact_ref": evidence,
                "evidence_package_ref": package_ref,
                "classification_fact_ref": classification,
                "reviewer": "synthetic-reviewer",
                "review_ref": "review/imported-evidence",
            },
            self.target._time(),
        )
        self.assertEqual(WorkflowGates(self.store).status(TARGET_ID)["checkpoint"], "official_sealed")
        result = self.store.replay(external_root=self.target.external_root, require_external=True)
        self.assertEqual(result["receipts"], 1)
        self.assertEqual(fixture_only_imported_objects(self.store), set())
        replay_receipt(self.store, outcome["receipt_ref"])

    def test_unfinalized_import_grants_nothing(self) -> None:
        import_ref = register_predecessor_import(self.store, self.payload())["import_ref"]
        discovery = self.record_discovery(self.target)
        with self.assertRaisesRegex(ControlError, "EvidencePackage 是登记导入的前序证据，未获授权引用：② 导入登记 .* 尚未 finalize"):
            self.record_evidence(self.target, discovery, self.predecessor.references["evidence-package"])
        campaign = self.store.load_campaign(TARGET_ID)
        with self.assertRaisesRegex(ControlError, "RuleMigrationLedger 与 Campaign 身份不一致（未获前序导入授权：② "):
            authorize_cross_campaign_reference(
                self.store,
                campaign,
                self.predecessor.references["rule-migration-ledger"],
                owner_campaign_id=PREDECESSOR_ID,
                label="RuleMigrationLedger 与 Campaign 身份不一致",
            )
        finalize_predecessor_import(self.store, import_ref, self.target._time())
        self.assertIsNone(
            cross_campaign_denial(
                self.store,
                campaign,
                self.predecessor.references["rule-migration-ledger"],
                owner_campaign_id=PREDECESSOR_ID,
            )
        )

    def test_regression_fixture_grants_nothing(self) -> None:
        _import_ref, receipt_ref = self.register_and_finalize(
            self.target, self.payload(mode="regression_fixture")
        )
        receipt = self.store.load_receipt(receipt_ref)
        self.assertEqual(receipt["mode"], "regression_fixture")
        self.assertEqual(receipt["identity_differences"], [])
        discovery = self.record_discovery(self.target)
        with self.assertRaisesRegex(ControlError, "③ 导入收据 mode=regression_fixture"):
            self.record_evidence(self.target, discovery, self.predecessor.references["evidence-package"])
        reason = cross_campaign_denial(
            self.store,
            self.store.load_campaign(TARGET_ID),
            self.predecessor.references["atomic-assertion-ledger"],
            owner_campaign_id=PREDECESSOR_ID,
        )
        self.assertRegex(reason or "", "^③ ")

    def test_non_whitelisted_references_are_rejected(self) -> None:
        discovery_ref = self.predecessor.references["discovery"]
        self.register_and_finalize(self.target, self.payload(fact_refs=[discovery_ref]))
        campaign = self.store.load_campaign(TARGET_ID)
        package_ref = self.predecessor.references["evidence-package"]
        # 事实：登记在导入里、收据为 read_only_reuse，仍因不是白名单对象类型被拒（写入前预检，不解引用前序事实）。
        with self.assertRaisesRegex(ControlError, "事实不得跨 Campaign 引用（未获前序导入授权：④ 被引用的是事实"):
            self.record_evidence(self.target, discovery_ref, package_ref)
        policy_ref = self.predecessor.references["comparison-policy"]
        self.assertEqual(
            cross_campaign_denial(self.store, campaign, policy_ref),
            "④ 对象类型 operational_evidence 不在官方证据白名单内",
        )
        receipt_ref = self.store.list_receipt_refs("predecessor_import")[0]
        self.assertRegex(cross_campaign_denial(self.store, campaign, receipt_ref) or "", "^① 收据不能导入")
        bootstrap_ref = self.target.references["bootstrap"]
        self.assertRegex(
            cross_campaign_denial(self.store, campaign, bootstrap_ref, owner_campaign_id=PREDECESSOR_ID) or "",
            "^① 引用不在本 Campaign",
        )
        # 被引用者所属 Campaign 与导入登记的前序 Campaign 不一致，同样不算登记过。
        self.assertRegex(
            cross_campaign_denial(
                self.store,
                campaign,
                self.predecessor.references["rule-migration-ledger"],
                owner_campaign_id="synthetic-other-campaign",
            )
            or "",
            "^① ",
        )
        self.assertIsNone(cross_campaign_denial(self.store, campaign, package_ref))
        # v1 Campaign 没有导入：一律按原文案拒绝，不附任何理由。
        with self.assertRaisesRegex(ControlError, "^原报错文案$"):
            authorize_cross_campaign_reference(
                self.store,
                {"schema_version": CAMPAIGN_SCHEMA, "campaign_id": "synthetic-legacy"},
                package_ref,
                owner_campaign_id=PREDECESSOR_ID,
                label="原报错文案",
            )

    def test_registration_and_finalize_are_idempotent(self) -> None:
        payload = self.payload()
        first = register_predecessor_import(self.store, payload)
        again = register_predecessor_import(self.store, payload)
        self.assertEqual(again["import_ref"], first["import_ref"])
        self.assertTrue(again["reused"])
        self.assertEqual(again["copied_objects"], [])
        self.assertEqual(len(again["reused_objects"]), 4)
        import_ref = first["import_ref"]
        original = finalize_predecessor_import_outcome(self.store, import_ref, self.target._time())
        retried = finalize_predecessor_import_outcome(self.store, import_ref, self.target._time())
        self.assertEqual(retried, {"receipt_ref": original["receipt_ref"], "reused": True})
        # 工具变化（未登记与登记演进后）都返回原收据：一份登记只对应一份收据，原收据按形成期坐标重放。
        self.tree.write(PROBE, b'{"probe": "b1-14-evolved"}\n')
        self.assertEqual(
            finalize_predecessor_import_outcome(self.store, import_ref, self.target._time())["receipt_ref"],
            original["receipt_ref"],
        )
        self.target.register_evolution()
        self.assertEqual(
            finalize_predecessor_import_outcome(self.store, import_ref, self.target._time()),
            {"receipt_ref": original["receipt_ref"], "reused": True},
        )
        self.assertEqual(len(self.store.list_receipt_refs("predecessor_import")), 1)
        self.store.replay()

    def test_predecessor_store_is_never_written(self) -> None:
        payload = self.payload(fact_refs=[self.predecessor.references["evidence-fact"]])
        root = self.predecessor.store_root
        self.lock(root)
        before = snapshot_tree(root)
        import_ref, _receipt_ref = self.register_and_finalize(self.target, payload)
        verified = verify_predecessor_import(self.store, import_ref)
        self.assertEqual(verified["result"], "passed")
        self.assertEqual(snapshot_tree(root), before)
        reader = ControlStore.open_read_only(root)
        with self.assertRaisesRegex(ControlError, "只读打开的 Store 禁止写入"):
            reader.append_fact(PREDECESSOR_ID, "selector_observed", {}, self.target._time())
        with self.assertRaisesRegex(ControlError, "只读打开的 Store 禁止写入"):
            reader.seal_object(
                "operational_evidence",
                {
                    "schema_version": "official-client-operational-evidence/v1",
                    "persona": self.predecessor.persona,
                    "manifest_id": "read-only-probe",
                    "entries": [{"id": "entry-1", "facts": {"result": "pass"}}],
                },
            )
        # 归档权限下普通（可写）打开直接失败；只读打开照常读。
        with self.assertRaisesRegex(ControlError, "Store 根 权限必须是 0o700"):
            ControlStore(root)
        self.assertEqual(reader.load_campaign(PREDECESSOR_ID)["campaign_id"], PREDECESSOR_ID)
        self.assertEqual(snapshot_tree(root), before)

    def test_replay_is_self_contained_after_predecessor_moves(self) -> None:
        import_ref, receipt_ref = self.register_and_finalize(self.target, self.payload())
        old_root = self.predecessor.store_root
        new_root = old_root.with_name("moved-store")
        old_root.rename(new_root)
        self.store.replay()
        replay_receipt(self.store, receipt_ref)
        with self.assertRaisesRegex(ControlError, "Store 根必须是现有的非符号链接目录"):
            verify_predecessor_import(self.store, import_ref)
        verified = verify_predecessor_import(self.store, import_ref, predecessor_root=new_root)
        self.assertEqual(verified["recorded_store_root"], str(old_root.resolve()))
        self.assertEqual(verified["verified_store_root"], str(new_root.resolve()))

    def test_finalize_reads_moved_predecessor_by_content(self) -> None:
        import_ref = register_predecessor_import(self.store, self.payload())["import_ref"]
        new_root = self.predecessor.store_root.with_name("moved-store")
        self.predecessor.store_root.rename(new_root)
        with self.assertRaisesRegex(ControlError, "Store 根必须是现有的非符号链接目录"):
            finalize_predecessor_import(self.store, import_ref, self.target._time())
        receipt_ref = finalize_predecessor_import(
            self.store, import_ref, self.target._time(), predecessor_root=new_root
        )
        receipt = self.store.load_receipt(receipt_ref)
        self.assertEqual(receipt["predecessor"]["store_root"], str(self.predecessor.store.root))
        with self.assertRaisesRegex(ControlError, "前序 Store 不得指向本 Store"):
            capture_predecessor_material(self.store, import_ref, predecessor_root=self.store.root)

    def test_store_root_must_not_be_this_store(self) -> None:
        for root in (self.store.root, self.store.root / "objects", self.store.root.parent):
            with self.subTest(root=root):
                payload = self.payload()
                payload["predecessor"]["store_root"] = str(root)
                with self.assertRaisesRegex(ControlError, "前序 Store 不得指向本 Store，也不得与本 Store 互相嵌套"):
                    register_predecessor_import(self.store, payload)
        # 绕过登记直接封存：写入时门禁同样拒绝。
        payload = self.payload(roots=[], fact_refs=[self.predecessor.references["discovery"]])
        payload["predecessor"]["store_root"] = str(self.store.root)
        with self.assertRaisesRegex(ControlError, "前序 Store 不得指向本 Store"):
            self.store.seal_object("predecessor_import", payload)

    def test_campaign_identity_and_naming_rules(self) -> None:
        payload = self.payload()
        payload["predecessor"]["campaign_id"] = TARGET_ID
        with self.assertRaisesRegex(ControlError, "前序 Campaign 不得与目标 Campaign 同 ID"):
            register_predecessor_import(self.store, payload)
        payload = self.payload()
        payload["predecessor"]["campaign_identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(ControlError, "前序 Campaign 的 identity_sha256 与导入登记声明的不一致"):
            register_predecessor_import(self.store, payload)
        # 本 Store 已有与前序 Campaign 同名的 Campaign：跨 Store 引用会与本地 Campaign 混淆。
        payload = self.payload()
        payload["client"] = "codex"
        with self.assertRaisesRegex(ControlError, "前序证据导入的 client 与 Campaign 不一致"):
            register_predecessor_import(self.store, payload)
        payload = self.payload()
        payload["approved_at_utc"] = "2026-08-01T00:00:00Z"
        with self.assertRaisesRegex(ControlError, "前序证据导入的批准时间早于 Campaign 建立时间"):
            register_predecessor_import(self.store, payload)
        clash = deepcopy(self.target.campaign_document)
        clash["campaign_id"] = PREDECESSOR_ID
        clash["identity_sha256"] = campaign_identity_sha256(clash)
        self.store.create_campaign(clash)
        with self.assertRaisesRegex(ControlError, "前序 Campaign ID 与本 Store 已有 Campaign 重名"):
            register_predecessor_import(self.store, self.payload())
        # 目标侧判据都在读前序、复制对象之前核：以上被拒的登记没有留下任何导入对象。
        self.assertEqual(self.store.list_object_refs("evidence_package"), [])

    def test_missing_or_drifted_predecessor_material_is_rejected(self) -> None:
        payload = self.payload()
        wrong_fact = dict(self.predecessor.references["evidence-fact"], sha256="0" * 64)
        payload["imported_fact_refs"] = [wrong_fact]
        with self.assertRaisesRegex(ControlError, "事实引用摘要不匹配"):
            register_predecessor_import(self.store, payload)
        import_ref = register_predecessor_import(self.store, self.payload())["import_ref"]
        # 登记之后前序 Store 里的导入对象不见了：finalize 按内容核对前序 Store，拒绝签发。
        self.predecessor.store.object_path(self.predecessor.references["atomic-assertion-ledger"]).unlink()
        with self.assertRaisesRegex(ControlError, "对象引用不存在"):
            finalize_predecessor_import(self.store, import_ref, self.target._time())
        self.assertEqual(self.store.list_receipt_refs("predecessor_import"), [])

    def test_import_list_must_be_closed_and_importable(self) -> None:
        payload = self.payload()
        payload["imported_objects"] = [
            reference
            for reference in payload["imported_objects"]
            if reference["object_kind"] != "operational_evidence"
        ]
        with self.assertRaisesRegex(ControlError, "imported_objects 未对对象引用闭合：evidence_package/"):
            register_predecessor_import(self.store, payload)
        self.assertEqual(self.store.list_object_refs("evidence_package"), [])
        payload = self.payload()
        payload["imported_objects"] = sorted(
            [*payload["imported_objects"], {"object_kind": "candidate_delivery_plan", "sha256": "0" * 64}],
            key=canonical_sha256,
        )
        with self.assertRaisesRegex(ControlError, "candidate_delivery_plan 不是可导入的证据对象"):
            register_predecessor_import(self.store, payload)
        payload = self.payload(roots=[], fact_refs=[])
        with self.assertRaisesRegex(ControlError, "至少要导入一个对象或事实"):
            register_predecessor_import(self.store, payload)
        # 通用证据对象里夹带事实引用：脱离前序 Store 无法解引用，整批在复制前即被拒绝。
        smuggled = self.predecessor.seal_manifest(
            "operational_evidence",
            "smuggled-fact",
            [{"id": "entry-1", "facts": {"result": "pass", "source": self.predecessor.references["discovery"]}}],
        )
        with self.assertRaisesRegex(ControlError, "operational_evidence 引用了事实或收据，脱离前序 Store 无法重放"):
            register_predecessor_import(self.store, self.payload(roots=[smuggled]))
        self.assertEqual(self.store.list_object_refs("operational_evidence"), [])
        self.assertEqual(self.store.list_object_refs("predecessor_import"), [])

    def test_imported_objects_must_belong_to_predecessor_campaign(self) -> None:
        # 前序 Store 里另一份 Campaign 的规则台账（同一 EvidencePackage）：不能借本份导入登记搬运。
        ledger = deepcopy(
            self.predecessor.store.load_object(self.predecessor.references["rule-migration-ledger"])["payload"]
        )
        ledger["campaign_id"] = "synthetic-other-campaign"
        ledger["identity_sha256"] = rule_migration_ledger_identity_sha256(ledger)
        foreign = self.predecessor.store.seal_object("rule_migration_ledger", ledger)
        with self.assertRaisesRegex(
            ControlError,
            f"rule_migration_ledger 属于 Campaign synthetic-other-campaign，不是导入登记的前序 Campaign {PREDECESSOR_ID}",
        ):
            register_predecessor_import(self.store, self.payload(roots=[foreign]))
        self.assertEqual(self.store.list_object_refs("evidence_package"), [])

    def test_fixture_external_bindings_stay_out_of_store_replay(self) -> None:
        self.register_and_finalize(self.target, self.payload(mode="regression_fixture"))
        (self.target.external_root / "evidence" / "spec-001.json").unlink()
        fixture_only = fixture_only_imported_objects(self.store)
        self.assertIn(
            ("evidence_package", self.predecessor.references["evidence-package"]["sha256"]), fixture_only
        )
        self.store.replay(external_root=self.target.external_root, require_external=True)
        # 同一批对象再经 read_only_reuse 授权：成为本 Campaign 的正式依赖，外部证据随即纳入核验。
        self.register_and_finalize(self.target, self.payload())
        self.assertEqual(fixture_only_imported_objects(self.store), set())
        with self.assertRaisesRegex(ControlError, "外部证据不存在或不可信：evidence/spec-001.json"):
            self.store.replay(external_root=self.target.external_root, require_external=True)

    def test_forged_receipt_fails_replay_and_grants_nothing(self) -> None:
        import_ref = register_predecessor_import(self.store, self.payload())["import_ref"]
        captured = capture_predecessor_material(self.store, import_ref)
        receipt = build_predecessor_import_receipt(
            self.store, import_ref, captured=captured, completed_at_utc=self.target._time()
        )
        forged = dict(receipt, approved_by="someone-else")
        forged["identity_sha256"] = canonical_sha256(
            {key: value for key, value in forged.items() if key != "identity_sha256"}
        )
        validate_receipt_document(forged, "predecessor_import")
        content = canonical_json_bytes(forged)
        write_once(
            self.store.receipt_path({"receipt_kind": "predecessor_import", "sha256": sha256_bytes(content)}),
            content,
        )
        discovery = self.record_discovery(self.target)
        with self.assertRaisesRegex(ControlError, "② 导入收据 .* 重放失败：predecessor_import 收据与事实独立复算结果不匹配"):
            self.record_evidence(self.target, discovery, self.predecessor.references["evidence-package"])
        with self.assertRaisesRegex(ControlError, "predecessor_import 收据与事实独立复算结果不匹配"):
            self.store.replay()

    def test_receipt_contract_rejects_inconsistent_fields(self) -> None:
        _import_ref, receipt_ref = self.register_and_finalize(self.target, self.payload())
        receipt = self.store.load_receipt(receipt_ref)
        cases = {
            "source_conditions 不是由源身份文档导出的七项": lambda value: value["source_conditions"].update(
                platform="linux/arm64"
            ),
            "identity_differences 与两组七项不一致": lambda value: value.update(
                identity_differences=[{"item": "platform", "source": "a", "target": "b", "reason": "different"}]
            ),
            "源身份文档与前序 Campaign 身份摘要不符": lambda value: value["predecessor"].update(
                campaign_identity_sha256="0" * 64
            ),
            "不是规范 JSON 文本": lambda value: value.update(
                source_campaign_document=json.dumps(
                    json.loads(value["source_campaign_document"]), ensure_ascii=False, indent=1
                )
            ),
            "identity_sha256 与收据内容不一致": lambda value: value.update(approved_by="tampered"),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                value = deepcopy(receipt)
                mutate(value)
                with self.assertRaisesRegex(ControlError, message):
                    validate_receipt_document(value, "predecessor_import")

    def test_cli_register_finalize_verify(self) -> None:
        help_text = build_parser().format_help()
        for command in ("predecessor-import-register", "predecessor-import-finalize", "predecessor-import-verify"):
            self.assertIn(command, help_text)
        payload_path = self.workspace / "import.json"
        payload_path.write_bytes(canonical_json_bytes(self.payload()))
        store = str(self.store.root)
        registered = execute(
            build_parser().parse_args(
                ["predecessor-import-register", "--store", store, "--campaign", TARGET_ID, "--input", str(payload_path)]
            )
        )
        self.assertFalse(registered["reused"])
        reference_path = self.workspace / "import-ref.json"
        reference_path.write_bytes(canonical_json_bytes(registered["import_ref"]))
        finalize = [
            "predecessor-import-finalize",
            "--store",
            store,
            "--import-ref",
            str(reference_path),
            "--completed-at",
            self.target._time(),
        ]
        first = execute(build_parser().parse_args(finalize))
        self.assertFalse(first["reused"])
        self.assertEqual(execute(build_parser().parse_args(finalize)), first | {"reused": True})
        verified = execute(
            build_parser().parse_args(
                ["predecessor-import-verify", "--store", store, "--import-ref", str(reference_path)]
            )
        )
        self.assertEqual(verified["receipt_ref"], first["receipt_ref"])
        with self.assertRaisesRegex(ControlError, "campaign_id 与 --campaign 不一致"):
            execute(
                build_parser().parse_args(
                    ["predecessor-import-register", "--store", store, "--campaign", "other", "--input", str(payload_path)]
                )
            )


class CrossPlatformFixtureTests(_ImportCase):
    """合成 v1 linux/amd64 Store（与真实 2.1.226 Store 同形）导入 v2 linux/arm64 Campaign。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor(schema="v1")
        self.target = self.make_target(platforms=["linux/arm64"])
        self.store = self.target.store

    def test_regression_fixture_records_platform_difference_and_unprovable(self) -> None:
        payload = self.import_payload(
            self.predecessor,
            self.target,
            mode="regression_fixture",
            fact_refs=[self.predecessor.references["evidence-fact"]],
        )
        self.assertIsNone(payload["source_identity_conditions"]["oauth_org_id"])
        import_ref, receipt_ref = self.register_and_finalize(self.target, payload)
        receipt = self.store.load_receipt(receipt_ref)
        self.assertEqual(
            receipt["identity_differences"],
            [
                {"item": "platform", "source": "linux/amd64", "target": "linux/arm64", "reason": "different"},
                {
                    "item": "oauth_org_id",
                    "source": None,
                    "target": "synthetic-org-0001",
                    "reason": "unprovable_in_source",
                },
                {
                    "item": "model_catalog_sha256",
                    "source": None,
                    "target": SHA_B,
                    "reason": "unprovable_in_source",
                },
            ],
        )
        self.assertEqual(json.loads(receipt["source_campaign_document"])["schema_version"], "official-client-control-campaign/v1")
        self.store.replay()
        self.assertEqual(verify_predecessor_import(self.store, import_ref)["mode"], "regression_fixture")
        discovery = self.record_discovery(self.target)
        with self.assertRaisesRegex(ControlError, "③ 导入收据 mode=regression_fixture"):
            self.record_evidence(self.target, discovery, self.predecessor.references["evidence-package"])

    def test_read_only_reuse_from_same_store_is_rejected(self) -> None:
        payload = self.import_payload(self.predecessor, self.target, mode="read_only_reuse")
        with self.assertRaisesRegex(
            ControlError,
            "read_only_reuse 要求七项全部可证且逐项相等：platform different.*oauth_org_id unprovable_in_source"
            ".*model_catalog_sha256 unprovable_in_source",
        ):
            register_predecessor_import(self.store, payload)
        # finalizer 独立核验：先以 regression_fixture 复制对象，再绕过登记直接封存 read_only_reuse 声明。
        self.register_and_finalize(
            self.target, self.import_payload(self.predecessor, self.target, mode="regression_fixture")
        )
        import_ref = self.store.seal_object("predecessor_import", payload)
        with self.assertRaisesRegex(ControlError, "read_only_reuse 要求七项全部可证且逐项相等"):
            finalize_predecessor_import(self.store, import_ref, self.target._time())
        self.assertEqual(len(self.store.list_receipt_refs("predecessor_import")), 1)

    def test_unprovable_items_must_be_declared_null(self) -> None:
        payload = self.import_payload(self.predecessor, self.target, mode="regression_fixture")
        payload["source_identity_conditions"]["oauth_org_id"] = "synthetic-org-0001"
        with self.assertRaisesRegex(
            ControlError,
            'source_identity_conditions.oauth_org_id 声明 "synthetic-org-0001"，由 oauth_org_id 独立导出 null',
        ):
            register_predecessor_import(self.store, payload)

    def test_v1_target_campaign_cannot_import(self) -> None:
        legacy = self.make_fixture("legacy-target", schema="v1", campaign_id="synthetic-legacy-target")
        payload = self.import_payload(self.predecessor, legacy, mode="regression_fixture")
        with self.assertRaisesRegex(ControlError, "前序证据导入只属于 Campaign v2"):
            register_predecessor_import(legacy.store, payload)


class OrganizationMismatchTests(_ImportCase):
    """源 org A、目标 org B、两组声明都写 A：声明与目标冻结字段不符，两种模式都拒绝。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor(oauth_org_id="synthetic-org-a")
        self.target = self.make_target(oauth_org_id="synthetic-org-b")
        self.store = self.target.store

    def declared_a(self, mode: str) -> dict[str, Any]:
        payload = self.import_payload(self.predecessor, self.target, mode=mode)
        self.assertEqual(payload["source_identity_conditions"]["oauth_org_id"], "synthetic-org-a")
        payload["identity_conditions"]["oauth_org_id"] = "synthetic-org-a"
        return payload

    def test_both_declarations_a_rejected_in_both_modes(self) -> None:
        message = 'identity_conditions.oauth_org_id 声明 "synthetic-org-a"，由 oauth_org_id 独立导出 "synthetic-org-b"'
        for mode in ("read_only_reuse", "regression_fixture"):
            with self.subTest(stage="register", mode=mode):
                with self.assertRaisesRegex(ControlError, message):
                    register_predecessor_import(self.store, self.declared_a(mode))
        # 声明正确时 regression_fixture 可以登记（差异记入收据），对象因此已复制进本 Store。
        _import_ref, receipt_ref = self.register_and_finalize(
            self.target, self.import_payload(self.predecessor, self.target, mode="regression_fixture")
        )
        self.assertEqual(
            self.store.load_receipt(receipt_ref)["identity_differences"],
            [{"item": "oauth_org_id", "source": "synthetic-org-a", "target": "synthetic-org-b", "reason": "different"}],
        )
        for mode in ("read_only_reuse", "regression_fixture"):
            with self.subTest(stage="finalize", mode=mode):
                import_ref = self.store.seal_object("predecessor_import", self.declared_a(mode))
                with self.assertRaisesRegex(ControlError, message):
                    finalize_predecessor_import(self.store, import_ref, self.target._time())
        self.assertEqual(len(self.store.list_receipt_refs("predecessor_import")), 1)


class SameToolIdentityTests(_ImportCase):
    """前序与目标工具身份相同（同一份受管代码先后建立的两个 Campaign，目标换成 linux/arm64）。

    前序 EvidencePackage 的产出工具、版本与官方产物都与目标一致，按原工具身份判据本可直接引用；登记为导入后一律
    改按消费端核验判定，回归夹具因此仍被拒绝，平台差异不会绕过收据。已被本 Campaign 事实当本地证据引用的包不得再
    登记为导入（否则既有事实的重放会改判）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor()
        self.target = self.make_target(change_tools=False, platforms=["linux/arm64"])
        self.store = self.target.store

    def test_fixture_import_is_rejected_even_with_same_tool_identity(self) -> None:
        package_ref = self.predecessor.references["evidence-package"]
        _import_ref, receipt_ref = self.register_and_finalize(
            self.target, self.import_payload(self.predecessor, self.target, mode="regression_fixture")
        )
        self.assertEqual(
            [item["item"] for item in self.store.load_receipt(receipt_ref)["identity_differences"]], ["platform"]
        )
        package = self.store.load_object(package_ref)["payload"]
        self.assertEqual(package["producer_tool_sha256"], self.target.tool_sha256)
        discovery = self.record_discovery(self.target)
        with self.assertRaisesRegex(ControlError, "EvidencePackage 是登记导入的前序证据，未获授权引用：③ "):
            self.record_evidence(self.target, discovery, package_ref)

    def test_locally_used_package_cannot_be_registered_as_import(self) -> None:
        package_ref = self.predecessor.references["evidence-package"]
        # 目标 Store 自己封存同内容的比较策略与 EvidencePackage，并作为本地证据引用。
        self.target.seal_manifest("operational_evidence", "comparison-policy")
        local = self.store.seal_object(
            "evidence_package", self.predecessor.store.load_object(package_ref)["payload"]
        )
        self.assertEqual(local, package_ref)
        self.record_evidence(self.target, self.record_discovery(self.target), local)
        # 目标侧判据先于读前序与核七项，所以两种模式都在这里被拒，Store 不留任何导入登记。
        for mode in ("regression_fixture", "read_only_reuse"):
            with self.subTest(mode=mode):
                payload = self.import_payload(self.predecessor, self.target, mode=mode)
                with self.assertRaisesRegex(
                    ControlError, "导入清单里的 EvidencePackage 已被本 Campaign 的事实当本地证据引用"
                ):
                    register_predecessor_import(self.store, payload)
        self.assertEqual(self.store.list_object_refs("predecessor_import"), [])
        self.store.replay()


class SchemaContractTests(unittest.TestCase):
    def test_schemas_match_runtime_contract(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        objects = json.loads((root / "object.schema.json").read_text(encoding="utf-8"))
        receipts = json.loads((root / "receipt.schema.json").read_text(encoding="utf-8"))
        self.assertIn("predecessor_import", objects["properties"]["object_kind"]["enum"])
        self.assertIn({"$ref": "#/$defs/predecessorImport"}, receipts["oneOf"])
        definition = receipts["$defs"]["predecessorImport"]
        self.assertEqual(definition["properties"]["schema_version"]["const"], PREDECESSOR_IMPORT_RECEIPT_SCHEMA)
        self.assertIn("formed_under", definition["required"])
        self.assertEqual(
            {key: value["const"] for key, value in definition["properties"]["condition_source_fields"]["properties"].items()},
            IDENTITY_CONDITION_SOURCE_FIELDS,
        )
        conditions = objects["$defs"]["identityConditions"]["required"]
        self.assertEqual(conditions, list(IDENTITY_CONDITION_SOURCE_FIELDS))


@unittest.skipUnless(os.environ.get(REAL_STORE_ENV), f"未设置 {REAL_STORE_ENV}：真实 2.1.226 Store 一例跳过")
class RealPredecessorStoreTests(_ImportCase):
    """真实 2.1.226 x86_64 Store（v1、linux/amd64）以 regression_fixture 导入 arm64 Campaign v2；只读使用。"""

    def test_real_store_imports_as_regression_fixture_only(self) -> None:
        root = Path(os.environ[REAL_STORE_ENV]).resolve()
        before = snapshot_tree(root)
        predecessor = ControlStore.open_read_only(root)
        candidates = []
        for path in sorted((root / "campaigns").glob("*.json")):
            facts = predecessor.list_facts(path.stem, "evidence")
            recorded = [fact for fact in facts if fact["fact_kind"] == "evidence_recorded"]
            if recorded:
                candidates.append((path.stem, recorded[-1]))
        self.assertTrue(candidates, "真实 Store 没有可导入的 EvidenceFact")
        source_id, evidence_fact = candidates[-1]
        source = predecessor.load_campaign(source_id)
        self.assertEqual(source["platforms"], ["linux/amd64"])
        target = self.make_fixture(
            "target",
            schema="v2",
            campaign_id="claude-code-arm64-regression-target",
            target_version=source["target_version"],
            official_artifacts=source["official_artifacts"],
            platforms=["linux/arm64"],
            entrypoints=source["entrypoints"],
            default_conditions=source["default_conditions"],
        )
        campaign = target.store.load_campaign(target.campaign_id)

        def payload(mode: str) -> dict[str, Any]:
            return {
                "client": "claude",
                "campaign_id": target.campaign_id,
                "predecessor": {
                    "store_root": str(root),
                    "campaign_id": source_id,
                    "campaign_identity_sha256": source["identity_sha256"],
                },
                "imported_objects": predecessor_object_closure(
                    predecessor, [evidence_fact["payload"]["evidence_package_ref"]]
                ),
                "imported_fact_refs": [predecessor.fact_ref(evidence_fact)],
                "identity_conditions": derive_identity_conditions(campaign)[0],
                "source_identity_conditions": derive_identity_conditions(source)[0],
                "mode": mode,
                "approved_by": "synthetic-operator",
                "approved_at_utc": target._time(),
            }

        import_ref, receipt_ref = self.register_and_finalize(target, payload("regression_fixture"))
        receipt = target.store.load_receipt(receipt_ref)
        self.assertEqual(
            [(item["item"], item["reason"]) for item in receipt["identity_differences"]],
            [
                ("platform", "different"),
                ("oauth_org_id", "unprovable_in_source"),
                ("model_catalog_sha256", "unprovable_in_source"),
            ],
        )
        self.assertEqual(receipt["identity_differences"][0]["source"], "linux/amd64")
        self.assertEqual(receipt["identity_differences"][0]["target"], "linux/arm64")
        with self.assertRaisesRegex(ControlError, "read_only_reuse 要求七项全部可证且逐项相等"):
            register_predecessor_import(target.store, payload("read_only_reuse"))
        target.store.replay()
        self.assertEqual(verify_predecessor_import(target.store, import_ref)["result"], "passed")
        self.assertEqual(snapshot_tree(root), before)


if __name__ == "__main__":
    unittest.main()
