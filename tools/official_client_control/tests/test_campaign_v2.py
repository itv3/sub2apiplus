"""Claude 升级 B1-12：Store Campaign v2 合同、建立时的受管清单核对、fact v2 形状与 CLI。"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_control import receipts as receipts_module
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.cli import build_parser, execute, main
from tools.official_client_control.contracts import (
    CAMPAIGN_V2_SCHEMA,
    FACT_V2_SCHEMA,
    campaign_identity_sha256,
    validate_campaign,
    validate_fact_document,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.receipts import (
    TOOL_IDENTITY_V2_SCHEMA,
    control_tool_bundle_sha256,
    current_tool_manifest,
)
from tools.official_client_control.tests.fixtures import (
    ManagedToolTree,
    SyntheticCampaign,
    synthetic_tool_identity,
)

_V2_NEW_FIELDS = (
    "client",
    "campaign_purpose",
    "oauth_org_id",
    "model_catalog_sha256",
    "execution_permissions",
    "stage_dependencies",
    "stage_budgets_minutes",
    "absolute_deadline_utc",
    "supervisor_parameters",
    "project_ledger_ref",
    "tool_identity",
)


def _with_identity(campaign: dict[str, Any]) -> dict[str, Any]:
    campaign = deepcopy(campaign)
    campaign["identity_sha256"] = campaign_identity_sha256(campaign)
    return campaign


class _ManagedTreeCase(unittest.TestCase):
    """每个测试类复制一次受管树；每个测试在副本上运行，结束时撤销对副本的改动。"""

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
        self.work = Path(self.temporary.name)
        self._active = self.tree.active()
        self._active.__enter__()

    def tearDown(self) -> None:
        self._active.__exit__(None, None, None)
        self.tree.restore()
        self.temporary.cleanup()

    def fixture(self, name: str = "work") -> SyntheticCampaign:
        directory = self.work / name
        directory.mkdir()
        return SyntheticCampaign(directory, schema="v2")


class CampaignV2ContractTests(_ManagedTreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.synthetic = self.fixture()
        self.synthetic.bootstrap_and_campaign()
        self.campaign = deepcopy(self.synthetic.campaign_document)

    def test_v2_campaign_is_created_and_reloaded(self) -> None:
        loaded = self.synthetic.store.load_campaign(self.synthetic.campaign_id)
        self.assertEqual(loaded["schema_version"], CAMPAIGN_V2_SCHEMA)
        self.assertEqual(loaded["tool_identity"]["client"], "claude")
        self.assertEqual(loaded["tool_bundle_sha256"], control_tool_bundle_sha256())

    def test_identity_covers_every_new_field(self) -> None:
        mutations = {
            "client": "codex",
            "campaign_purpose": "validation_only",
            "oauth_org_id": "synthetic-org-0002",
            "model_catalog_sha256": "d" * 64,
            "execution_permissions": {"hosts": ["arm64-other"], "actions": ["official_capture"]},
            "stage_dependencies": dict(self.campaign["stage_dependencies"], **{"VC-6": ["VC-4", "VC-5"]}),
            "stage_budgets_minutes": dict(self.campaign["stage_budgets_minutes"], **{"VC-0": 1}),
            "absolute_deadline_utc": "2026-08-26T00:00:00Z",
            "supervisor_parameters": {"watchdog_seconds": 31},
            "project_ledger_ref": {"path": "other/plan.json", "plan_sha256": "e" * 64},
            "tool_identity": dict(self.campaign["tool_identity"], control_sha256="f" * 64),
        }
        self.assertEqual(sorted(mutations), sorted(_V2_NEW_FIELDS))
        original = campaign_identity_sha256(self.campaign)
        for field, value in mutations.items():
            with self.subTest(field=field):
                mutated = deepcopy(self.campaign)
                mutated[field] = value
                if field == "client":
                    mutated["tool_identity"]["client"] = "codex"
                self.assertNotEqual(campaign_identity_sha256(mutated), original)
                with self.assertRaisesRegex(ControlError, "identity_sha256 与身份字段不一致"):
                    validate_campaign(mutated)
                validate_campaign(_with_identity(mutated))

    def test_campaign_id_and_created_at_stay_outside_identity(self) -> None:
        moved = deepcopy(self.campaign)
        moved["campaign_id"] = "synthetic-campaign-other"
        moved["created_at_utc"] = "2026-08-18T00:02:00Z"
        self.assertEqual(campaign_identity_sha256(moved), self.campaign["identity_sha256"])

    def test_v1_identity_formula_is_unchanged(self) -> None:
        v1 = {
            key: self.campaign[key]
            for key in (
                "campaign_id",
                "persona",
                "target_version",
                "official_artifacts",
                "platforms",
                "entrypoints",
                "default_conditions",
                "tool_bundle_sha256",
                "bootstrap_ref",
                "created_at_utc",
            )
        }
        v1["schema_version"] = "official-client-control-campaign/v1"
        expected = canonical_sha256(
            {
                key: v1[key]
                for key in (
                    "persona",
                    "target_version",
                    "official_artifacts",
                    "platforms",
                    "entrypoints",
                    "default_conditions",
                    "tool_bundle_sha256",
                    "bootstrap_ref",
                )
            }
        )
        self.assertEqual(campaign_identity_sha256(v1), expected)

    def test_rejects_malformed_v2_fields(self) -> None:
        cases: list[tuple[str, Any, str]] = [
            ("client", "gemini", "campaign.client 非法"),
            ("campaign_purpose", "staging", "campaign_purpose 非法"),
            ("oauth_org_id", " org", "oauth_org_id"),
            ("model_catalog_sha256", "A" * 64, "model_catalog_sha256"),
            (
                "execution_permissions",
                {"hosts": ["b", "a"], "actions": ["x"]},
                "严格排序",
            ),
            ("execution_permissions", {"hosts": ["a"]}, "字段不闭合"),
            ("stage_dependencies", {}, "不得为空"),
            ("stage_dependencies", {"VC-0": ["VC-0"]}, "不得依赖自身"),
            ("stage_dependencies", {"VC-0": ["VC-9"]}, "未登记阶段"),
            ("stage_dependencies", {"A": ["B"], "B": ["A"]}, "循环依赖"),
            ("absolute_deadline_utc", "2026-08-17T00:00:00Z", "必须晚于 created_at_utc"),
            ("supervisor_parameters", {"bad key": 1}, "参数名"),
            ("project_ledger_ref", {"path": "a/../b", "plan_sha256": "c" * 64}, "规范 POSIX 路径"),
            ("project_ledger_ref", {"path": "a//b", "plan_sha256": "c" * 64}, "规范 POSIX 路径"),
            ("project_ledger_ref", {"path": "a/b"}, "字段不闭合"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                mutated = deepcopy(self.campaign)
                mutated[field] = value
                if field == "stage_dependencies" and value:
                    mutated["stage_budgets_minutes"] = {stage: 1 for stage in value}
                with self.assertRaisesRegex(ControlError, message):
                    validate_campaign(_with_identity(mutated))

    def test_rejects_inconsistent_stages_and_tool_identity(self) -> None:
        budgets = deepcopy(self.campaign)
        budgets["stage_budgets_minutes"].pop("VC-6")
        with self.assertRaisesRegex(ControlError, "阶段集合必须等于 stage_dependencies"):
            validate_campaign(_with_identity(budgets))
        zero = deepcopy(self.campaign)
        zero["stage_budgets_minutes"]["VC-0"] = 0
        with self.assertRaisesRegex(ControlError, "正整数"):
            validate_campaign(_with_identity(zero))
        client = deepcopy(self.campaign)
        client["tool_identity"]["client"] = "codex"
        with self.assertRaisesRegex(ControlError, "tool_identity.client 与 campaign.client 不一致"):
            validate_campaign(_with_identity(client))
        version = deepcopy(self.campaign)
        version["tool_identity"]["policy_version"] = True
        with self.assertRaisesRegex(ControlError, "policy_version 必须是正整数"):
            validate_campaign(_with_identity(version))
        extra = deepcopy(self.campaign)
        extra["tool_identity"]["files_sha256"] = "a" * 64
        with self.assertRaisesRegex(ControlError, "字段不闭合"):
            validate_campaign(_with_identity(extra))


class CampaignCreateManifestTests(_ManagedTreeCase):
    """Store 全包摘要的核对：清单指纹、Store 口径复算、Store 文件进清单且与清单一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.synthetic = self.fixture()
        self.synthetic.bootstrap_and_campaign()
        self.campaign = deepcopy(self.synthetic.campaign_document)
        self.counter = 0

    def _create(self, campaign: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        variant = deepcopy(campaign)
        variant["campaign_id"] = f"synthetic-variant-{self.counter}"
        return self.synthetic.store.create_campaign(_with_identity(variant))

    def _recomputed(self, *, tool_files: bool, store_bundle: bool) -> dict[str, Any]:
        campaign = deepcopy(self.campaign)
        manifest = current_tool_manifest("claude")
        if tool_files:
            campaign["tool_identity"] = synthetic_tool_identity(manifest)
        if store_bundle:
            campaign["tool_bundle_sha256"] = control_tool_bundle_sha256(
                self.tree.root / "tools" / "official_client_control"
            )
        return campaign

    def test_store_file_changed_after_freeze_is_rejected(self) -> None:
        key = "official_client_control/errors.py"
        self.tree.write(key, self.tree.path(key).read_bytes() + b"# drift\n")
        with self.assertRaisesRegex(ControlError, "tool_files_sha256 与现算受管文件清单不一致"):
            self._create(self.campaign)
        stale_bundle = self._recomputed(tool_files=True, store_bundle=False)
        with self.assertRaisesRegex(ControlError, "tool_bundle_sha256 与受管清单内 Store 文件"):
            self._create(stale_bundle)
        fresh = self._recomputed(tool_files=True, store_bundle=True)
        self.assertEqual(self._create(fresh)["identity_sha256"], campaign_identity_sha256(fresh))

    def test_capture_root_change_only_moves_tool_files(self) -> None:
        self.tree.write("official_client_capture/claude_upgrade_b112_probe.json", b"{}\n")
        with self.assertRaisesRegex(ControlError, "tool_files_sha256"):
            self._create(self.campaign)
        self._create(self._recomputed(tool_files=True, store_bundle=False))

    def test_store_file_missing_from_manifest_is_rejected(self) -> None:
        engine = receipts_module._identity_engine("claude")
        original = engine.managed_tree_digests

        def without_errors(root: Path, policy: dict[str, Any]) -> dict[str, str]:
            digests = dict(original(root, policy))
            digests.pop("official_client_control/errors.py")
            return digests

        with mock.patch.object(engine, "managed_tree_digests", side_effect=without_errors):
            campaign = self._recomputed(tool_files=True, store_bundle=False)
            with self.assertRaisesRegex(ControlError, "未进清单=\\['official_client_control/errors.py'\\]"):
                self._create(campaign)

    def test_manifest_entry_disagreeing_with_store_file_is_rejected(self) -> None:
        engine = receipts_module._identity_engine("claude")
        original = engine.managed_tree_digests

        def drifted(root: Path, policy: dict[str, Any]) -> dict[str, str]:
            digests = dict(original(root, policy))
            digests["official_client_control/store.py"] = "0" * 64
            return digests

        with mock.patch.object(engine, "managed_tree_digests", side_effect=drifted):
            campaign = self._recomputed(tool_files=True, store_bundle=False)
            with self.assertRaisesRegex(ControlError, "文件被改而清单未跟上.*store.py"):
                self._create(campaign)

    def test_policy_fields_must_match_current_policy(self) -> None:
        for field, value in (("policy_sha256", "0" * 64), ("policy_version", 99)):
            with self.subTest(field=field):
                campaign = deepcopy(self.campaign)
                campaign["tool_identity"][field] = value
                with self.assertRaisesRegex(ControlError, f"tool_identity.{field} 与当前身份策略不一致"):
                    self._create(campaign)

    def test_codex_client_has_no_multi_root_policy(self) -> None:
        campaign = deepcopy(self.campaign)
        campaign["client"] = "codex"
        campaign["tool_identity"]["client"] = "codex"
        with self.assertRaisesRegex(ControlError, "client=codex 没有覆盖"):
            self._create(campaign)


class FactV2ShapeTests(_ManagedTreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.synthetic = self.fixture()
        self.synthetic.bootstrap_and_campaign()
        self.synthetic.discovery_and_evidence()

    def _document(self) -> dict[str, Any]:
        fact = self.synthetic.store.load_fact(self.synthetic.references["discovery"])
        return {key: value for key, value in fact.items() if key != "_sha256"}

    def test_v2_fact_records_formed_under_and_optional_run_fields(self) -> None:
        document = self._document()
        self.assertEqual(document["schema_version"], FACT_V2_SCHEMA)
        self.assertEqual(
            document["formed_under"],
            {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None},
        )
        self.assertIsNone(document["idempotency_key"])
        self.assertIsNone(document["producer_run"])
        reference = self.synthetic.store.append_fact(
            self.synthetic.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": self.synthetic.store.seal_object(
                    "runtime_catalog_snapshot",
                    {
                        "schema_version": "official-client-runtime-catalog-snapshot/v1",
                        "persona": self.synthetic.persona,
                        "catalog_digest": "d" * 64,
                        "production_active_ref": None,
                        "production_rollback_ref": None,
                        "observed_at_utc": self.synthetic._time(),
                        "source_ref": self.synthetic.runtime_binding,
                    },
                ),
                "observation_kind": "read_only",
            },
            self.synthetic._time(),
            idempotency_key="1" * 64,
            producer_run={"run_dir": "control/runs/r1", "owner_nonce": "nonce-1", "action_id": "a-1"},
        )
        stored = self.synthetic.store.load_fact(reference)
        self.assertEqual(stored["idempotency_key"], "1" * 64)
        self.assertEqual(stored["producer_run"]["action_id"], "a-1")

    def test_fact_v2_shape_is_closed(self) -> None:
        base = self._document()
        cases: list[tuple[str, Any, str]] = [
            ("formed_under", None, "formed_under 必须是对象"),
            ("formed_under", {"tool_identity_ref": None, "candidate_revision": 1}, "字段不闭合"),
            (
                "formed_under",
                {"tool_identity_ref": None, "candidate_revision": 0, "evaluation_baseline": None},
                "candidate_revision 必须是正整数",
            ),
            (
                "formed_under",
                {
                    "tool_identity_ref": {"object_kind": "bootstrap", "sha256": "a" * 64},
                    "candidate_revision": 1,
                    "evaluation_baseline": None,
                },
                "只能引用 tool_evolution_registration",
            ),
            (
                "formed_under",
                {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": {"baseline": 1}},
                "字段不闭合",
            ),
            ("idempotency_key", "X" * 64, "idempotency_key 不是小写 SHA-256"),
            ("producer_run", {"run_dir": "r", "owner_nonce": "n"}, "字段不闭合"),
            ("producer_run", {"run_dir": "r", "owner_nonce": "", "action_id": "a"}, "owner_nonce"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                document = deepcopy(base)
                document[field] = value
                with self.assertRaisesRegex(ControlError, message):
                    validate_fact_document(document)
        missing = deepcopy(base)
        missing.pop("producer_run")
        with self.assertRaisesRegex(ControlError, "字段不闭合"):
            validate_fact_document(missing)
        baseline = deepcopy(base)
        baseline["formed_under"]["evaluation_baseline"] = {
            "baseline": 1,
            "baseline_commit_sha256": "b" * 64,
        }
        validate_fact_document(baseline)

    def test_fact_versions_cannot_mix_with_campaign_versions(self) -> None:
        document = self._document()
        legacy = {
            key: document[key]
            for key in (
                "campaign_id",
                "dimension",
                "fact_kind",
                "sequence",
                "previous_fact_sha256",
                "issued_at_utc",
                "payload",
            )
        }
        legacy["schema_version"] = "official-client-control-fact/v1"
        validate_fact_document(legacy)
        content = canonical_json_bytes(legacy)
        directory = self.synthetic.store_root / "facts" / self.synthetic.campaign_id / "discovery"
        original = next(directory.iterdir())
        original.unlink()
        write_once(directory / f"00000001-{sha256_bytes(content)}.json", content)
        with self.assertRaisesRegex(ControlError, "事实合同版本与 Campaign 版本不一致"):
            self.synthetic.store.list_facts(self.synthetic.campaign_id, "discovery")

    def test_revision_fact_kind_is_v2_only(self) -> None:
        document = self._document()
        document.update(
            {
                "schema_version": "official-client-control-fact/v1",
                "dimension": "validation",
                "fact_kind": "candidate_revision_recorded",
                "payload": {
                    "revision": 2,
                    "candidate_id": "c",
                    "supersedes": None,
                    "invalidation_ref": None,
                },
            }
        )
        for key in ("formed_under", "idempotency_key", "producer_run"):
            document.pop(key)
        with self.assertRaisesRegex(ControlError, "只能出现在 fact v2"):
            validate_fact_document(document)


class V1CampaignUnchangedTests(unittest.TestCase):
    """v1 Campaign 不接受 v2 专属输入；其余行为由现有测试零改动覆盖。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = SyntheticCampaign(Path(self.temporary.name))
        self.fixture.bootstrap_and_campaign()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v1_campaign_rejects_v2_only_inputs(self) -> None:
        with self.assertRaisesRegex(ControlError, "v1 Campaign 的事实不支持 idempotency_key"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "discovery_recorded",
                {
                    "version": "1.0.0",
                    "source": "synthetic-fixture",
                    "discovered_at_utc": self.fixture._time(),
                    "tool_sha256": self.fixture.tool_sha256,
                    "artifact_refs": [self.fixture.official_binding],
                },
                self.fixture._time(),
                idempotency_key="1" * 64,
            )
        with self.assertRaisesRegex(ControlError, "只能出现在 fact v2"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "candidate_revision_recorded",
                {"revision": 2, "candidate_id": "c-2", "supersedes": None, "invalidation_ref": None},
                self.fixture._time(),
            )
        campaign = self.fixture.store.load_campaign(self.fixture.campaign_id)
        payload = {
            "client": "claude",
            "campaign_id": self.fixture.campaign_id,
            "index": 1,
            "from_identity": {
                "client": "claude",
                "policy_version": 1,
                "policy_sha256": "1" * 64,
                "wire_producer_sha256": "2" * 64,
                "evidence_semantics_sha256": "3" * 64,
                "control_sha256": "4" * 64,
                "tool_files_sha256": "5" * 64,
            },
            "to_identity": {
                "client": "claude",
                "policy_version": 1,
                "policy_sha256": "1" * 64,
                "wire_producer_sha256": "2" * 64,
                "evidence_semantics_sha256": "3" * 64,
                "control_sha256": "4" * 64,
                "tool_files_sha256": "6" * 64,
            },
            "evolution_receipt": {"path": "control/evolution.json", "sha256": "7" * 64},
            "approved_by": "synthetic-operator",
            "approved_at_utc": campaign["created_at_utc"],
            "previous_ref": None,
        }
        with self.assertRaisesRegex(ControlError, "v1 Campaign 不支持工具演进登记"):
            self.fixture.store.seal_object("tool_evolution_registration", payload)


class CampaignV2CLITests(_ManagedTreeCase):
    def _run(self, argv: list[str]) -> dict[str, Any]:
        return execute(build_parser().parse_args(argv))

    def test_tool_digest_outputs_identity_v2(self) -> None:
        # 编排器（B1-09）不在受管树里时三层摘要为 null；与真实工作树里编排器是否已出现无关。
        engine = receipts_module._identity_engine("claude")
        self.tree.remove(engine.load_policy()["orchestrator"]["file"])
        result = self._run(["tool-digest"])
        self.assertEqual(result["schema_version"], TOOL_IDENTITY_V2_SCHEMA)
        self.assertEqual(
            result["tool_bundle_sha256"],
            control_tool_bundle_sha256(self.tree.root / "tools" / "official_client_control"),
        )
        manifest = current_tool_manifest("claude")
        identity = result["tool_identity"]
        for key in ("client", "policy_version", "policy_sha256", "tool_files_sha256"):
            self.assertEqual(identity[key], manifest[key])
        self.assertFalse(result["layer_digests_available"])
        self.assertIsNone(identity["wire_producer_sha256"])

    def test_tool_digest_uses_v3_engine_once_orchestrator_exists(self) -> None:
        engine = receipts_module._identity_engine("claude")
        orchestrator = engine.load_policy()["orchestrator"]["file"]
        self.tree.write(orchestrator, '"""B1-09 之前的编排器占位。"""\n'.encode("utf-8"))
        manifest = current_tool_manifest("claude")
        layers = {
            "wire_producer_sha256": "1" * 64,
            "evidence_semantics_sha256": "2" * 64,
            "control_sha256": "3" * 64,
            "tool_files_sha256": manifest["tool_files_sha256"],
        }
        with mock.patch.object(engine, "compute_identity", return_value=layers) as compute:
            result = self._run(["tool-digest"])
        compute.assert_called_once()
        self.assertTrue(result["layer_digests_available"])
        self.assertEqual(result["tool_identity"]["control_sha256"], "3" * 64)

    def test_campaign_create_schema_flag_must_match_input(self) -> None:
        synthetic = self.fixture()
        synthetic.bootstrap_and_campaign()
        campaign = deepcopy(synthetic.campaign_document)
        campaign["campaign_id"] = "synthetic-cli-v2"
        path = self.work / "campaign.json"
        path.write_bytes(canonical_json_bytes(campaign))
        store = str(synthetic.store_root)
        with self.assertRaisesRegex(ControlError, "--schema v1 要求输入声明"):
            self._run(["campaign-create", "--store", store, "--input", str(path)])
        result = self._run(["campaign-create", "--store", store, "--input", str(path), "--schema", "v2"])
        self.assertEqual(result["campaign_id"], "synthetic-cli-v2")

    def test_tool_evolution_command_registers_current_code(self) -> None:
        synthetic = self.fixture()
        synthetic.bootstrap_and_campaign()
        self.tree.write("official_client_capture/claude_upgrade_b112_probe.json", b"{}\n")
        payload = synthetic.evolution_payload()
        path = self.work / "evolution.json"
        path.write_bytes(canonical_json_bytes(payload))
        store = str(synthetic.store_root)
        with self.assertRaisesRegex(ControlError, "与 --campaign 不一致"):
            self._run(["tool-evolution", "--store", store, "--campaign", "other", "--input", str(path)])
        result = self._run(
            ["tool-evolution", "--store", store, "--campaign", synthetic.campaign_id, "--input", str(path)]
        )
        self.assertEqual(result["registration_ref"]["object_kind"], "tool_evolution_registration")
        self.assertEqual(result["effective_tool_identity"], payload["to_identity"])
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main(["tool-evolution", "--store", store, "--campaign", synthetic.campaign_id, "--input", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
