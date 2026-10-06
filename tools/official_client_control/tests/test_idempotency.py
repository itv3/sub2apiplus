"""Claude 升级 B1-13：Store 幂等键、动作结果 WAL 的 Store 侧原语与两层事务边界。

设计验收逐条对应：
* 同键同内容返回原引用（``reused: true``、不新增事实），同键异内容拒绝；
* ①② 之间崩溃（Store 已写、账本未记）：按幂等键 ``fact-lookup`` 找到原事实及其 ``formed_under``、``producer_run``
  补账，N+1 重派得到原引用；
* ②③ 之间崩溃（账本已记、绑定未写）：重派同样复用原事实，对账按 ``reuse_authority=none`` 处理；
* selector 重复被拒（``selector_activated`` 同一 Deployment 唯一；``selector_observed`` 可重复观察、重试靠幂等键）。

账本与 ``action-outputs`` 绑定属于监督器与对账器（B2-08 接入），这里用临时目录里的 JSON 文件模拟，只验证
Store 一侧给出的保证。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_control import canonical as canonical_module
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.cli import build_parser, execute, main
from tools.official_client_control.contracts import (
    FACT_LOOKUP_SCHEMA,
    campaign_identity_sha256,
    fact_idempotency_key,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    current_tool_manifest,
    finalize_validation_outcome,
)
from tools.official_client_control.tests.fixtures import (
    ManagedToolTree,
    SyntheticCampaign,
    synthetic_tool_identity,
)

PROBE = "official_client_capture/claude_upgrade_b113_probe.json"


def run_coordinates(name: str, action_id: str = "vc5-selector-before") -> dict[str, Any]:
    """合成的监督器 run 坐标：每次重派是新 run（新 run_dir 与 owner nonce），逻辑动作不变。"""

    return {
        "run_dir": f"control/supervisor/runs/run-{name}",
        "owner_nonce": canonical_sha256({"run": name}),
        "action_id": action_id,
    }


class IdempotencyKeyFormulaTests(unittest.TestCase):
    def test_key_is_canonical_digest_of_three_fields(self) -> None:
        key = fact_idempotency_key("vc1/evidence-record", "a" * 64, "evidence_recorded")
        self.assertEqual(
            key,
            canonical_sha256(
                {
                    "logical_action": "vc1/evidence-record",
                    "input_identity_sha256": "a" * 64,
                    "fact_kind": "evidence_recorded",
                }
            ),
        )
        variants = {
            fact_idempotency_key("vc1/evidence-record-2", "a" * 64, "evidence_recorded"),
            fact_idempotency_key("vc1/evidence-record", "b" * 64, "evidence_recorded"),
            fact_idempotency_key("vc1/evidence-record", "a" * 64, "discovery_recorded"),
        }
        self.assertEqual(len(variants), 3)
        self.assertNotIn(key, variants)

    def test_key_inputs_are_validated(self) -> None:
        cases = [
            (("", "a" * 64, "evidence_recorded"), "logical_action"),
            (("vc1", "A" * 64, "evidence_recorded"), "input_identity_sha256"),
            (("vc1", "a" * 64, "made_up_kind"), "未登记的 fact_kind"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ControlError, message):
                    fact_idempotency_key(*arguments)


class _V2Case(unittest.TestCase):
    """每个测试类复制一次受管树；每个测试在副本上建一个 Campaign v2，结束时撤销对副本的改动。"""

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
        self.workspace = Path(self.temporary.name)
        self._active = self.tree.active()
        self._active.__enter__()
        (self.workspace / "campaign").mkdir()
        self.fixture = SyntheticCampaign(self.workspace / "campaign", schema="v2")
        self.fixture.bootstrap_and_campaign()
        self.store = self.fixture.store
        self.campaign_id = self.fixture.campaign_id

    def tearDown(self) -> None:
        self._active.__exit__(None, None, None)
        self.tree.restore()
        self.temporary.cleanup()

    def facts(self, dimension: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_facts(self.campaign_id, dimension)

    def observation(self) -> dict[str, Any]:
        """一次只读 selector 观察的 payload（快照对象每次新建，观察时间不同）。"""

        snapshot = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.fixture.persona,
                "catalog_digest": "d" * 64,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": self.fixture._time(),
                "source_ref": self.fixture.runtime_binding,
            },
        )
        return {"catalog_snapshot_ref": snapshot, "observation_kind": "read_only"}

    def observation_key(self, payload: dict[str, Any]) -> str:
        return fact_idempotency_key("vc5/selector-before", canonical_sha256(payload), "selector_observed")

    def run_cli(self, argv: list[str]) -> dict[str, Any]:
        return execute(build_parser().parse_args(argv))

    def write_json(self, name: str, value: Any) -> Path:
        path = self.workspace / name
        path.write_bytes(canonical_json_bytes(value))
        return path


class AppendFactIdempotencyTests(_V2Case):
    def test_same_key_same_payload_returns_original_reference(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        first = self.store.append_fact_outcome(
            self.campaign_id,
            "selector_observed",
            payload,
            self.fixture._time(),
            idempotency_key=key,
            producer_run=run_coordinates("r1"),
        )
        self.assertFalse(first["reused"])
        count = len(self.facts())
        retry = self.store.append_fact_outcome(
            self.campaign_id,
            "selector_observed",
            deepcopy(payload),
            self.fixture._time(),
            idempotency_key=key,
            producer_run=run_coordinates("r2"),
        )
        self.assertEqual(retry, {"fact_ref": first["fact_ref"], "reused": True})
        self.assertEqual(len(self.facts()), count)
        stored = self.store.load_fact(first["fact_ref"])
        self.assertEqual(stored["idempotency_key"], key)
        self.assertEqual(stored["producer_run"], run_coordinates("r1"))
        # 普通接口带同一幂等键时返回的也是原事实的引用。
        self.assertEqual(
            self.store.append_fact(
                self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
            ),
            first["fact_ref"],
        )

    def test_same_key_different_content_is_rejected(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        self.store.append_fact(
            self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
        )
        with self.assertRaisesRegex(ControlError, "同键异内容拒绝"):
            self.store.append_fact(
                self.campaign_id,
                "selector_observed",
                self.observation(),
                self.fixture._time(),
                idempotency_key=key,
            )
        with self.assertRaisesRegex(ControlError, "同键异内容拒绝"):
            self.store.append_fact(
                self.campaign_id,
                "discovery_recorded",
                {
                    "version": "1.0.0",
                    "source": "synthetic-fixture",
                    "discovered_at_utc": self.fixture._time(),
                    "tool_sha256": self.fixture.tool_sha256,
                    "artifact_refs": [self.fixture.official_binding],
                },
                self.fixture._time(),
                idempotency_key=key,
            )
        self.assertEqual(len(self.facts()), 1)

    def test_writes_without_key_append_normally(self) -> None:
        payload = self.observation()
        first = self.store.append_fact(self.campaign_id, "selector_observed", payload, self.fixture._time())
        second = self.store.append_fact(self.campaign_id, "selector_observed", payload, self.fixture._time())
        # 只读观察可以重复记录同一状态（VC-5 的 before／after）；重试去重靠幂等键。
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.facts("runtime_selector")), 2)

    def test_retry_after_registered_evolution_reuses_original(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        original = self.store.append_fact(
            self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
        )
        self.tree.write(PROBE, b'{"probe": 1}\n')
        self.fixture.register_evolution()
        retry = self.store.append_fact_outcome(
            self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
        )
        self.assertEqual(retry, {"fact_ref": original, "reused": True})
        self.assertIsNone(self.store.load_fact(original)["formed_under"]["tool_identity_ref"])

    def test_retry_under_unregistered_drift_is_rejected(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        self.store.append_fact(
            self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
        )
        self.tree.write(PROBE, b'{"probe": 2}\n')
        with self.assertRaisesRegex(ControlError, "工具身份未登记演进"):
            self.store.append_fact(
                self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
            )

    def test_vc5_finalizer_is_idempotent_with_key(self) -> None:
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate(finalize=False)
        acceptance_ref = self.fixture.references["acceptance-strict"]
        key = fact_idempotency_key(
            "vc5/validation-finalize", canonical_sha256(acceptance_ref), "validation_completed"
        )

        def finalize(run: str) -> dict[str, Any]:
            return finalize_validation_outcome(
                self.store,
                self.campaign_id,
                acceptance_ref,
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
                idempotency_key=key,
                producer_run=run_coordinates(run, "vc5-finalize"),
            )

        first = finalize("r1")
        self.assertEqual(finalize("r2"), {"fact_ref": first["fact_ref"], "reused": True})
        completed = [fact for fact in self.facts() if fact["fact_kind"] == "validation_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(WorkflowGates(self.store).status(self.campaign_id)["checkpoint"], "ready")
        self.assertEqual(self.store.replay()["result"], "passed")


class ActionResultWalTests(_V2Case):
    """动作结果 WAL 三个崩溃点在 Store 一侧的表现；账本与绑定是模拟文件。"""

    LOGICAL_ACTION = "vc1/capture-official-seal/evidence-record"

    def setUp(self) -> None:
        super().setUp()
        self.fixture._record_discovery()
        self.ledger_path = self.workspace / "ledger.json"
        self.outputs = self.workspace / "action-outputs"
        self.outputs.mkdir()

    def _payload(self) -> dict[str, Any]:
        package = self.store.seal_object(
            "evidence_package", self.fixture.evidence_package_payload(), allow_existing=True
        )
        return {
            "discovery_fact_ref": self.fixture.references["discovery"],
            "evidence_package_ref": package,
        }

    def _key(self, payload: dict[str, Any]) -> str:
        return fact_idempotency_key(self.LOGICAL_ACTION, canonical_sha256(payload), "evidence_recorded")

    def _ledger(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {}
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def _save_ledger(self, ledger: dict[str, Any]) -> None:
        self.ledger_path.write_text(json.dumps(ledger, sort_keys=True), encoding="utf-8")

    def _action(self, run: str, *, crash_after: str | None = None) -> dict[str, Any]:
        """一次动作体：① Store 追加（带幂等键）→ ② 账本记账 → ③ 动作输出绑定；``crash_after`` 模拟在某步后被杀。"""

        payload = self._payload()
        key = self._key(payload)
        outcome = self.store.append_fact_outcome(
            self.campaign_id,
            "evidence_recorded",
            payload,
            self.fixture._time(),
            idempotency_key=key,
            producer_run=run_coordinates(run, "vc1-evidence-record"),
        )
        result = {"key": key, "payload": payload, **outcome}
        if crash_after == "store":
            return result
        ledger = self._ledger()
        ledger[key] = outcome["fact_ref"]
        self._save_ledger(ledger)
        if crash_after == "ledger":
            return result
        (self.outputs / f"run-{run}.json").write_text(
            json.dumps({"fact_refs": [outcome["fact_ref"]]}, sort_keys=True), encoding="utf-8"
        )
        return result

    def _reconcile(self, key: str, run: str) -> str:
        """按设计的崩溃分类模拟对账：B2-08 在 reconcile-supervisor-run 里用同一组 Store 接口实现。"""

        lookup = self.store.fact_lookup(self.campaign_id, key)
        if not lookup["found"]:
            return "redispatch"
        ledger = self._ledger()
        if key not in ledger:
            ledger[key] = lookup["fact_ref"]
            self._save_ledger(ledger)
            return "backfilled"
        self.assertEqual(ledger[key], lookup["fact_ref"])
        if not (self.outputs / f"run-{run}.json").exists():
            return "reuse_authority=none"
        return "complete"

    def test_crash_before_store_write_redispatches(self) -> None:
        lookup = self.store.fact_lookup(self.campaign_id, self._key(self._payload()))
        self.assertEqual(lookup["schema_version"], FACT_LOOKUP_SCHEMA)
        self.assertFalse(lookup["found"])
        for field in ("fact_ref", "fact_kind", "payload_sha256", "issued_at_utc", "formed_under", "producer_run"):
            self.assertIsNone(lookup[field])
        self.assertEqual(self._reconcile(self._key(self._payload()), "r1"), "redispatch")

    def test_crash_between_store_and_ledger_is_backfilled_by_key(self) -> None:
        first = self._action("r1", crash_after="store")
        count = len(self.facts())
        lookup = self.store.fact_lookup(self.campaign_id, first["key"])
        self.assertTrue(lookup["found"])
        self.assertEqual(lookup["fact_ref"], first["fact_ref"])
        self.assertEqual(lookup["fact_kind"], "evidence_recorded")
        self.assertEqual(lookup["payload_sha256"], canonical_sha256(first["payload"]))
        self.assertEqual(lookup["producer_run"], run_coordinates("r1", "vc1-evidence-record"))
        self.assertEqual(
            lookup["formed_under"],
            {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None},
        )
        self.assertEqual(self._reconcile(first["key"], "r1"), "backfilled")
        # 不带幂等键的朴素重试会被门禁拒绝；带同一幂等键的 N+1 重派得到原引用、不新增事实。
        with self.assertRaisesRegex(ControlError, "不得原位替换 EvidencePackage"):
            self.store.append_fact(
                self.campaign_id, "evidence_recorded", first["payload"], self.fixture._time()
            )
        retry = self._action("r2")
        self.assertEqual((retry["fact_ref"], retry["reused"]), (first["fact_ref"], True))
        self.assertEqual(len(self.facts()), count)
        self.assertEqual(self._reconcile(first["key"], "r2"), "complete")
        self.assertEqual(
            WorkflowGates(self.store).status(self.campaign_id)["checkpoint"], "evidence_recorded"
        )
        self.assertEqual(self.store.replay()["result"], "passed")

    def test_crash_between_ledger_and_binding_degrades_reuse_authority(self) -> None:
        first = self._action("r1", crash_after="ledger")
        self.assertEqual(self._reconcile(first["key"], "r1"), "reuse_authority=none")
        count = len(self.facts())
        retry = self._action("r2")
        self.assertEqual((retry["fact_ref"], retry["reused"]), (first["fact_ref"], True))
        self.assertEqual(len(self.facts()), count)
        self.assertEqual(self._reconcile(first["key"], "r2"), "complete")


class AllowExistingTests(_V2Case):
    def _manifest(self, manifest_id: str) -> dict[str, Any]:
        return {
            "schema_version": "official-client-operational-evidence/v1",
            "persona": self.fixture.persona,
            "manifest_id": manifest_id,
            "entries": [{"id": "entry-1", "facts": {"result": "pass"}}],
        }

    def test_seal_object_allow_existing_returns_original(self) -> None:
        payload = self._manifest("allow-existing")
        first = self.store.seal_object_outcome("operational_evidence", payload)
        self.assertFalse(first["reused"])
        with self.assertRaisesRegex(ControlError, "禁止覆盖"):
            self.store.seal_object("operational_evidence", payload)
        self.assertEqual(
            self.store.seal_object_outcome("operational_evidence", payload, allow_existing=True),
            {"object_ref": first["object_ref"], "reused": True},
        )
        path = self.write_json("manifest.json", payload)
        result = self.run_cli(
            [
                "artifact-seal",
                "--store",
                str(self.fixture.store_root),
                "--kind",
                "operational_evidence",
                "--input",
                str(path),
                "--allow-existing",
            ]
        )
        self.assertEqual(result, {"object_ref": first["object_ref"], "reused": True})

    def test_registration_retry_with_allow_existing(self) -> None:
        self.tree.write(PROBE, b'{"probe": 3}\n')
        payload = self.fixture.evolution_payload()
        reference = self.store.seal_object("tool_evolution_registration", payload)
        with self.assertRaisesRegex(ControlError, "必须接在当前链尾"):
            self.store.seal_object("tool_evolution_registration", payload)
        self.assertEqual(
            self.store.seal_object_outcome("tool_evolution_registration", payload, allow_existing=True),
            {"object_ref": reference, "reused": True},
        )
        path = self.write_json("evolution.json", payload)
        result = self.run_cli(
            [
                "tool-evolution",
                "--store",
                str(self.fixture.store_root),
                "--campaign",
                self.campaign_id,
                "--input",
                str(path),
                "--allow-existing",
            ]
        )
        self.assertEqual((result["registration_ref"], result["reused"]), (reference, True))

    def test_write_receipt_allow_existing_only_reuses_identical_content(self) -> None:
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate(finalize=False)
        reference = self.fixture.references["validation-gate-receipt"]
        receipt = self.store.load_receipt(reference)
        with self.assertRaisesRegex(ControlError, "禁止覆盖"):
            self.store.write_receipt("validation_gate", receipt)
        self.assertEqual(
            self.store.write_receipt_outcome("validation_gate", receipt, allow_existing=True),
            {"receipt_ref": reference, "reused": True},
        )
        # 换 revision 后改字段的旧收据内容不同，allow_existing 不豁免「旧结果冒充当前写入」。
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        forged = deepcopy(receipt)
        forged["output_sha256"] = "e" * 64
        forged["identity_sha256"] = canonical_sha256(
            {key: value for key, value in forged.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "旧结果不得冒充当前写入"):
            self.store.write_receipt("validation_gate", forged, allow_existing=True)

    def test_create_campaign_allow_existing(self) -> None:
        document = deepcopy(self.fixture.campaign_document)
        with self.assertRaisesRegex(ControlError, "禁止覆盖"):
            self.store.create_campaign(document)
        expected = {
            "campaign_id": self.campaign_id,
            "identity_sha256": document["identity_sha256"],
            "reused": True,
        }
        self.assertEqual(self.store.create_campaign(document, allow_existing=True), expected)
        # Campaign 冻结的是建立时的身份：之后代码变化，重跑同一建立步骤仍得到原 Campaign。
        self.tree.write(PROBE, b'{"probe": 4}\n')
        self.assertEqual(self.store.create_campaign(document, allow_existing=True), expected)
        self.tree.restore()
        changed = deepcopy(document)
        changed["supervisor_parameters"] = {"watchdog_seconds": 31}
        changed["identity_sha256"] = campaign_identity_sha256(changed)
        with self.assertRaisesRegex(ControlError, "已存在且内容不同"):
            self.store.create_campaign(changed, allow_existing=True)
        fresh = deepcopy(document)
        fresh["campaign_id"] = "synthetic-allow-existing-new"
        path = self.write_json("campaign.json", fresh)
        result = self.run_cli(
            [
                "campaign-create",
                "--store",
                str(self.fixture.store_root),
                "--input",
                str(path),
                "--schema",
                "v2",
                "--allow-existing",
            ]
        )
        self.assertEqual(result["reused"], False)
        self.assertEqual(
            self.run_cli(
                [
                    "campaign-create",
                    "--store",
                    str(self.fixture.store_root),
                    "--input",
                    str(path),
                    "--schema",
                    "v2",
                    "--allow-existing",
                ]
            )["reused"],
            True,
        )


class SelectorUniquenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = SyntheticCampaign(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_selector_activated_is_unique_per_deployment(self) -> None:
        self.fixture.complete()
        activated = self.fixture.store.load_fact(self.fixture.references["selector-after"])
        self.assertEqual(activated["fact_kind"], "selector_activated")
        with self.assertRaisesRegex(ControlError, "selector_activated 不得重复"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "selector_activated",
                activated["payload"],
                self.fixture._time(),
            )
        self.assertEqual(
            self.fixture.store.replay(
                external_root=self.fixture.external_root, require_external=True
            )["result"],
            "passed",
        )


class WriteOnceDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name) / "base"
        self.base.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parent_and_created_directories_are_fsynced(self) -> None:
        calls: list[Path] = []
        target = self.base / "a" / "b" / "fact.json"
        with mock.patch.object(canonical_module, "_fsync_directory", side_effect=calls.append):
            write_once(target, b"{}\n")
        self.assertEqual(calls, [self.base / "a" / "b", self.base / "a", self.base])
        calls.clear()
        with mock.patch.object(canonical_module, "_fsync_directory", side_effect=calls.append):
            write_once(self.base / "second.json", b"{}\n")
        self.assertEqual(calls, [self.base])

    def test_real_directory_fsync_succeeds(self) -> None:
        target = self.base / "nested" / "fact.json"
        write_once(target, b"{}\n")
        self.assertEqual(target.read_bytes(), b"{}\n")

    def test_directory_fsync_failure_is_reported_after_write(self) -> None:
        target = self.base / "fact.json"
        with mock.patch.object(canonical_module, "_fsync_directory", side_effect=OSError("boom")):
            with self.assertRaisesRegex(ControlError, "父目录无法落盘"):
                write_once(target, b"{}\n")
        # 内容已写入并 fsync；调用方按幂等键或 allow_existing 重试即可收敛。
        self.assertEqual(target.read_bytes(), b"{}\n")


class FactLookupTests(_V2Case):
    def _write_raw_fact(self, payload: dict[str, Any], key: str) -> None:
        """绕过 append_fact 直接落盘一条形状合法的 fact v2，模拟被篡改出重复幂等键的 Store。"""

        chain = self.facts("runtime_selector")
        document = {
            "schema_version": "official-client-control-fact/v2",
            "campaign_id": self.campaign_id,
            "dimension": "runtime_selector",
            "fact_kind": "selector_observed",
            "sequence": len(chain) + 1,
            "previous_fact_sha256": chain[-1]["_sha256"] if chain else None,
            "issued_at_utc": self.fixture._time(),
            "payload": payload,
            "formed_under": {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None},
            "idempotency_key": key,
            "producer_run": None,
        }
        content = canonical_json_bytes(document)
        write_once(
            self.fixture.store_root
            / "facts"
            / self.campaign_id
            / "runtime_selector"
            / f"{len(chain) + 1:08d}-{sha256_bytes(content)}.json",
            content,
        )

    def test_cli_writes_with_key_and_looks_up(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        payload_path = self.write_json("observation.json", payload)
        run_path = self.write_json("run.json", run_coordinates("r1"))
        store = str(self.fixture.store_root)
        missing = self.run_cli(
            ["fact-lookup", "--store", store, "--campaign", self.campaign_id, "--idempotency-key", key]
        )
        self.assertEqual((missing["found"], missing["fact_ref"]), (False, None))
        argv = [
            "selector-observe",
            "--store",
            store,
            "--campaign",
            self.campaign_id,
            "--input",
            str(payload_path),
            "--issued-at",
            self.fixture._time(),
            "--idempotency-key",
            key,
            "--producer-run",
            str(run_path),
        ]
        first = self.run_cli(argv)
        self.assertFalse(first["reused"])
        self.assertTrue(self.run_cli(argv)["reused"])
        found = self.run_cli(
            ["fact-lookup", "--store", store, "--campaign", self.campaign_id, "--idempotency-key", key]
        )
        self.assertEqual(found, self.store.fact_lookup(self.campaign_id, key))
        self.assertEqual(found["fact_ref"], first["fact_ref"])
        self.assertEqual(found["producer_run"], run_coordinates("r1"))
        # 不带幂等键时输出仍是可直接当引用文件用的事实引用。
        plain = self.run_cli(argv[: argv.index("--idempotency-key")])
        self.assertEqual(set(plain), {"campaign_id", "dimension", "sequence", "sha256"})

    def test_lookup_rejects_malformed_keys_and_v1_campaigns(self) -> None:
        with self.assertRaisesRegex(ControlError, "不是小写 SHA-256"):
            self.store.fact_lookup(self.campaign_id, "X" * 64)
        with tempfile.TemporaryDirectory() as directory:
            legacy = SyntheticCampaign(Path(directory))
            legacy.bootstrap_and_campaign()
            with self.assertRaisesRegex(ControlError, "v1 Campaign 的事实没有幂等键"):
                legacy.store.fact_lookup(legacy.campaign_id, "a" * 64)
            output = io.StringIO()
            with redirect_stdout(output), mock.patch("sys.stderr", new=io.StringIO()):
                code = main(
                    [
                        "fact-lookup",
                        "--store",
                        str(legacy.store_root),
                        "--campaign",
                        legacy.campaign_id,
                        "--idempotency-key",
                        "a" * 64,
                    ]
                )
            self.assertEqual(code, 2)

    def test_duplicate_keys_are_detected(self) -> None:
        payload = self.observation()
        key = self.observation_key(payload)
        self.store.append_fact(
            self.campaign_id, "selector_observed", payload, self.fixture._time(), idempotency_key=key
        )
        self._write_raw_fact(payload, key)
        with self.assertRaisesRegex(ControlError, "幂等键在多条事实上重复"):
            self.store.fact_lookup(self.campaign_id, key)
        with self.assertRaisesRegex(ControlError, "重复的事实幂等键"):
            self.store.replay()


class IdentityLayerVerificationTests(_V2Case):
    """B1-13 补齐：建 Campaign v2 与登记工具演进时，三层摘要按 v3 引擎 compute_identity 现算核对。"""

    def test_campaign_layer_digest_must_match_engine(self) -> None:
        for field in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"):
            with self.subTest(field=field):
                document = deepcopy(self.fixture.campaign_document)
                document["campaign_id"] = f"synthetic-layer-{field.split('_')[0]}"
                document["tool_identity"][field] = "0" * 64
                document["identity_sha256"] = campaign_identity_sha256(document)
                with self.assertRaisesRegex(ControlError, f"与 v3 身份引擎现算结果不一致：{field}"):
                    self.store.create_campaign(document)

    def test_registration_layer_digest_must_match_engine(self) -> None:
        self.tree.write(PROBE, b'{"probe": 5}\n')
        payload = self.fixture.evolution_payload()
        payload["to_identity"]["wire_producer_sha256"] = "0" * 64
        with self.assertRaisesRegex(ControlError, "to_identity 不是当前受管代码的身份：wire_producer_sha256"):
            self.store.seal_object("tool_evolution_registration", payload)

    def test_layers_unavailable_without_orchestrator(self) -> None:
        orchestrator = current_tool_manifest("claude")["policy"]["orchestrator"]["file"]
        self.tree.remove(orchestrator)
        document = deepcopy(self.fixture.campaign_document)
        document["campaign_id"] = "synthetic-no-orchestrator"
        document["tool_identity"]["tool_files_sha256"] = current_tool_manifest("claude")["tool_files_sha256"]
        document["identity_sha256"] = campaign_identity_sha256(document)
        with self.assertRaisesRegex(ControlError, "不在受管树内，无法计算三层工具身份摘要"):
            self.store.create_campaign(document)

    def test_fixture_identity_is_the_engine_identity(self) -> None:
        self.assertEqual(
            self.fixture.campaign_document["tool_identity"],
            synthetic_tool_identity(current_tool_manifest("claude")),
        )


if __name__ == "__main__":
    unittest.main()
