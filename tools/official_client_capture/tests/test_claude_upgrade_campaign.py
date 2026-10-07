"""Claude 建 Campaign（Claude 升级控制面设计 B2-11）：plan 建预检、控制收据读回、Store Campaign v2 同源、生产现状清单与首批。

* plan 带计时账本与 P0 环境收据时建 ``preflight_only`` Campaign：控制收据是全形，冻结输入逐字节落进 ``inputs/``，总计划的
  官方身份取 B2-01 冻结收据，``configuration`` 是取证坐标加 Store 身份四项与冻结坐标；Store Campaign v2 与总计划同源（七项
  身份含官方制品），bootstrap 的外部绑定在宿主数据根上逐个重放通过；四个身份根的 client 一致；项目总账登记了本 Campaign；
* 命令行 ``plan --campaign-mode formal`` 一律拒绝（Formal 只由 VC-0 收口建立）；控制收据参数只给一部分或混入 Formal 参数即拒绝，
  且零写入；
* 控制收据读回：形状闭集按模式、计时绑定允许 B2-08 以来的最小形但带 checkpoint 时必须是全形；``verify_control_receipts``
  在要求齐全时拒绝最小形；
* 生产现状清单：从前序 Store 的 ProfileApprovalFact 原样取出四个对象与目标处置，篡改任一对象、目标处置不覆盖清单即拒绝；
* Store 第七项身份：官方制品绑定缺冻结收据或目标二进制即判不同源。

依赖真实冻结流程产出的合成冻结目录（``test_claude_upgrade_freeze`` 的夹具，需要 openssl）；ARM64 环境收据的重放用替身（真实
收据要在 ARM64 上采集，环境收据模块自己的测试覆盖它的重放）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as environment_receipt
from tools.official_client_capture import claude_upgrade_campaign as campaign_module
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_freeze as freeze_tests
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import SyntheticCampaign

TARGET = "2.1.285"
BASELINE = "2.1.226"
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
SOURCE_COMMIT = "e" * 40
MODEL_CATALOG = "f" * 64
UPGRADE_ID = "claude-code-2_1_285-upgrade-t1"


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_main(argv: list[str]) -> tuple[int, dict[str, Any] | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    text = stdout.getvalue().strip()
    return code, (json.loads(text) if text else None), stderr.getvalue()


def build_predecessor_store(root: Path) -> tuple[Path, str]:
    """合成前序 Store：走到 ProfileApprovalFact（生产入口清单、出站处置清单与目标处置都在里面）。"""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fixture = SyntheticCampaign(root)
    fixture.bootstrap_and_campaign()
    fixture.discovery_and_legacy_evidence()
    fixture.profile_objects()
    fixture.profile_approve()
    return fixture.store_root, fixture.campaign_id


class CampaignWorld:
    """一个宿主数据根：Claude 项目总账、Claude 计时账本与 VC-0 checkpoint、P0 环境收据、B2-01 冻结目录、Active 两件、
    静态基线两件与前序 Store。环境收据的重放用 ``environment_patch`` 替身（返回当前 producer、本轮目标二进制）。"""

    def __init__(self, root: Path, *, signer: Any, binary: bytes, evidence_decision: str = "recapture") -> None:
        self.root = root
        self.data = root / "data"
        for directory in (self.data, self.data / "evidence", self.data / "evidence" / "campaigns", self.data / "control"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(self.data)
        self.binary_sha256 = hashlib.sha256(binary).hexdigest()
        self.freeze = freeze_tests.freeze_fixture(
            self.data / "official" / f"claude-code-{TARGET}", signer=signer, binary=binary, version=TARGET
        )
        self.ledger = self.data / "control" / "timing" / UPGRADE_ID
        self.ledger.parent.mkdir(mode=0o700)
        timing.create_ledger(
            self.ledger,
            upgrade_id=UPGRADE_ID,
            baseline_version=BASELINE,
            target_version=TARGET,
            campaign_purpose="production_replacement",
            evidence_decision=evidence_decision,
            client="claude",
        )
        self.checkpoint = "receipts/vc0-input-preflight.json"
        timing.checkpoint(self.ledger, self.checkpoint)
        self.environment_root = self.data / "control" / "environment" / f"{UPGRADE_ID}-p0"
        self.environment_root.mkdir(parents=True, mode=0o700)
        self.environment_root.parent.chmod(0o700)
        _write_json(self.environment_root / "receipt.json", {"synthetic": "claude-p0-environment"})
        inputs = self.data / "control" / "inputs"
        self.active_catalog = _write_json(inputs / "active-release-catalog.json", {"release": "claude-active"})
        self.active_profile = _write_json(inputs / "active-profile.json", {"profile": "claude-active"})
        self.anchors = _write_json(inputs / "baseline-anchors.json", {"baseline": "2.1.220", "kind": "anchors"})
        self.reachability = _write_json(inputs / "baseline-reachability.json", {"baseline": "2.1.220", "kind": "reachability"})
        self.predecessor_root, self.predecessor_campaign = build_predecessor_store(root / "predecessor")

    def environment_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": environment_receipt.RECEIPT_SCHEMA,
            "client": "claude",
            "status": "passed",
            "phase": "p0",
            "subject_id": UPGRADE_ID,
            "contract_sha256": "c" * 64,
            "continuity_identity_sha256": "d" * 64,
            "client_binary": {"path": f"/opt/claude-{TARGET}/claude", "sha256": self.binary_sha256, "version": TARGET},
            "producer": {"version": environment_receipt.PRODUCER_VERSION},
        }

    def environment_patch(self) -> Any:
        return mock.patch.object(environment_receipt, "replay", return_value=self.environment_receipt())

    def campaign_dir(self, campaign_id: str) -> Path:
        return self.data / "evidence" / "campaigns" / campaign_id

    def plan_argv(self, campaign_id: str, *extra: str, inventory: str = "store", **overrides: str) -> list[str]:
        values: dict[str, str | None] = {
            "--campaign-dir": str(self.campaign_dir(campaign_id)),
            "--campaign-id": campaign_id,
            "--campaign-mode": "preflight_only",
            "--campaign-purpose": "production_replacement",
            "--baseline-version": BASELINE,
            "--target-version": TARGET,
            "--target-sha256": self.binary_sha256,
            "--target-package": str(self.freeze),
            "--target-package-sha256": _sha256(self.freeze),
            "--runtime-image": IMAGE,
            "--model": "claude-sonnet-5",
            "--upstream-ip": "160.79.104.10",
            "--timing-ledger-dir": str(self.ledger),
            "--timing-receipt": self.checkpoint,
            "--arm64-environment-root": str(self.environment_root),
            "--arm64-environment-receipt": "receipt.json",
            "--active-release-catalog": str(self.active_catalog),
            "--active-profile": str(self.active_profile),
            "--static-baseline-anchors": str(self.anchors),
            "--static-baseline-reachability": str(self.reachability),
            "--entrypoint": "sdk-cli",
            "--privacy-mode": "essential-traffic",
            "--oauth-org-id": "claude-org-fixture",
            "--model-catalog-sha256": MODEL_CATALOG,
            "--source-commit": SOURCE_COMMIT,
        }
        if inventory == "store":
            values["--production-inventory-store"] = str(self.predecessor_root)
            values["--production-inventory-campaign"] = self.predecessor_campaign
        values.update({f"--{key.replace('_', '-')}": value for key, value in overrides.items()})
        argv = ["plan"]
        for flag, value in values.items():
            if value is not None:
                argv += [flag, value]
        return [*argv, *extra]

    def predecessor_argv(self) -> list[str]:
        return ["--predecessor-store", str(self.predecessor_root), "--predecessor-campaign", self.predecessor_campaign]

    def create_preflight(self, campaign_id: str = "claude-code-2_1_285-preflight-t1", *extra: str) -> tuple[Path, dict[str, Any]]:
        with self.environment_patch():
            code, result, stderr = run_main(self.plan_argv(campaign_id, *extra))
        if code != 0 or result is None:
            raise AssertionError(stderr)
        campaign = self.campaign_dir(campaign_id)
        return campaign, claude_upgrade.load_campaign_manifest(campaign)


class CampaignWorldTestCase(unittest.TestCase):
    evidence_decision = "recapture"

    @classmethod
    def setUpClass(cls) -> None:
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = freeze_tests._Signer(Path(cls._keys.name))
        cls.binary = freeze_tests._elf(freeze_tests.GLIBC, payload=b"claude-2.1.285-b2-11")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._keys.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.world = CampaignWorld(self.root, signer=self.signer, binary=self.binary, evidence_decision=self.evidence_decision)


class PreflightCreationTests(CampaignWorldTestCase):
    def test_plan_creates_preflight_campaign_with_store_and_registration(self) -> None:
        campaign, manifest = self.world.create_preflight()
        # 控制收据：预检两项、计时绑定是全形，逐项语义重放通过。
        self.assertEqual(sorted(manifest["control_receipts"]), ["arm64_environment", "upgrade_timing"])
        self.assertEqual(set(manifest["control_receipts"]["upgrade_timing"]), campaign_module.TIMING_FULL_FIELDS)
        with self.world.environment_patch():
            verified = campaign_module.verify_control_receipts(manifest, require_complete=True)
        self.assertEqual(verified["controls"], ["arm64_environment", "upgrade_timing"])
        # 冻结输入：十一份全部落在 inputs/ 下，摘要与 Campaign 内文件一致；生产现状清单取自前序 ProfileApprovalFact。
        self.assertEqual(set(manifest["inputs"]), set(campaign_module.FROZEN_INPUT_FILES))
        for name, reference in manifest["inputs"].items():
            self.assertEqual(_sha256(campaign / reference["path"]), reference["sha256"], name)
        self.assertEqual(_sha256(campaign / "inputs" / "active-profile.json"), _sha256(self.world.active_profile))
        inventory = json.loads((campaign / "inputs" / "production-inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(inventory["source"]["store_campaign_id"], self.world.predecessor_campaign)
        self.assertEqual(campaign_module.production_egress_ids(inventory), frozenset({"aux-refresh", "infer-main", "unknown-aux"}))
        # 官方身份取冻结收据；configuration 带 Store 身份四项与冻结坐标。
        freeze = json.loads(self.world.freeze.read_text(encoding="utf-8"))
        self.assertEqual(manifest["official_identity"], {key: freeze[key] for key in ("npm", "gcs", "binary_sha256", "binary_equivalence")})
        configuration = manifest["configuration"]
        self.assertEqual(
            {key: configuration[key] for key in ("platform", "entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256", "source_commit")},
            {"platform": "linux/arm64", "entrypoint": "sdk-cli", "privacy_mode": "essential-traffic", "oauth_org_id": "claude-org-fixture",
             "model_catalog_sha256": MODEL_CATALOG, "source_commit": SOURCE_COMMIT},
        )
        self.assertEqual(configuration["official_freeze_sha256"], _sha256(self.world.freeze))
        # VC 控制制品：预检只有总计划与 VC-0 checkpoint，没有首批；前序为 null。VC 总计划冻结的控制摘要与控制收据对应。
        self.assertIsNone(manifest["vc_control"]["first_formal_batch"])
        self.assertIsNone(manifest["predecessor"])
        campaign_module.verify_vc_control_correspondence(campaign, manifest)
        drifted = json.loads(json.dumps(manifest))
        drifted["control_receipts"]["arm64_environment"]["receipt"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ConfigurationError, "arm64_environment_sha256"):
            campaign_module.verify_vc_control_correspondence(campaign, drifted)
        # Store Campaign v2：与总计划同源，官方制品含冻结收据与目标二进制，外部绑定在数据根上逐个重放通过。
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, manifest)
        self.assertEqual((store_campaign["client"], store_campaign["schema_version"]), ("claude", "official-client-control-campaign/v2"))
        artifacts = {item["sha256"] for item in store_campaign["official_artifacts"]}
        self.assertTrue({_sha256(self.world.freeze), self.world.binary_sha256} <= artifacts)
        self.assertIn("privacy=essential-traffic", store_campaign["default_conditions"])
        self.assertEqual(store_campaign["project_ledger_ref"]["path"], "upgrade-project-ledger-claude/plan.json")
        replay = store.replay(external_root=self.world.data, require_external=True)
        self.assertTrue(replay)
        # 四个身份根 client 一致；项目总账登记了本 Campaign。
        self.assertEqual(len(campaign_module.require_campaign_client_roots(campaign, manifest)), 4)
        head = project_ledger.replay_head(self.world.project_root)
        self.assertIn(manifest["campaign_id"], head["registered_campaigns"])

    def test_plan_without_control_receipts_stays_zero_write_report(self) -> None:
        argv = [
            item
            for item in self.world.plan_argv("claude-code-2_1_285-dry")
        ]
        # 去掉四个控制收据参数，其余照旧：B2-05 的零写入展开报告。
        for flag in ("--timing-ledger-dir", "--timing-receipt", "--arm64-environment-root", "--arm64-environment-receipt"):
            index = argv.index(flag)
            del argv[index : index + 2]
        code, result, stderr = run_main(argv)
        self.assertEqual(code, 0, stderr)
        self.assertEqual((result["mode"], result["campaign_created"]), ("dry-run", False))
        self.assertFalse(self.world.campaign_dir("claude-code-2_1_285-dry").exists())

    def test_partial_or_formal_control_arguments_are_rejected_without_writes(self) -> None:
        campaign_id = "claude-code-2_1_285-bad"
        argv = self.world.plan_argv(campaign_id)
        index = argv.index("--arm64-environment-receipt")
        partial = argv[:index] + argv[index + 2 :]
        with self.world.environment_patch():
            code, _result, stderr = run_main(partial)
            self.assertEqual(code, 1)
            self.assertIn("控制收据参数不完整", stderr)
            code, _result, stderr = run_main([*argv, "--release-certification", str(self.world.freeze)])
            self.assertEqual(code, 1)
            self.assertIn("不属于该模式", stderr)
            formal = self.world.plan_argv(campaign_id, campaign_mode="formal")
            code, _result, stderr = run_main(formal)
            self.assertEqual(code, 1)
            self.assertIn("VC-0 收口", stderr)
        self.assertFalse(self.world.campaign_dir(campaign_id).exists())

    def test_inputs_must_be_consistent_before_any_write(self) -> None:
        cases = {
            "两份生产现状清单来源都给": (
                self.world.plan_argv("claude-code-2_1_285-c1", "--production-inventory", str(self.world.active_profile)),
                "二选一",
            ),
            "冻结摘要不对": (
                self.world.plan_argv("claude-code-2_1_285-c2", target_package_sha256="0" * 64),
                "冻结目录复核失败",
            ),
            "二进制摘要不对": (self.world.plan_argv("claude-code-2_1_285-c3", target_sha256="1" * 64), "目标"),
            "缺 Store 身份": (self.world.plan_argv("claude-code-2_1_285-c4", entrypoint=None), "--entrypoint"),
            "复用前序但账本是重采": (
                self.world.plan_argv(
                    "claude-code-2_1_285-c5",
                    "--predecessor-store",
                    str(self.world.predecessor_root),
                    "--predecessor-campaign",
                    self.world.predecessor_campaign,
                ),
                "证据决定不一致",
            ),
        }
        for label, (argv, message) in cases.items():
            with self.subTest(label=label), self.world.environment_patch():
                code, _result, stderr = run_main(argv)
                self.assertEqual(code, 1, label)
                self.assertIn(message, stderr)
                campaign_id = argv[argv.index("--campaign-id") + 1]
                self.assertFalse(self.world.campaign_dir(campaign_id).exists(), label)

    def test_environment_receipt_must_be_current_producer_for_target_binary(self) -> None:
        stale = {**self.world.environment_receipt(), "producer": {"version": "1"}}
        wrong_binary = {**self.world.environment_receipt(), "client_binary": {"path": "/opt/x", "sha256": "0" * 64, "version": TARGET}}
        for label, receipt, message in (("历史 producer", stale, "当前环境 producer"), ("二进制不符", wrong_binary, "目标客户端二进制")):
            with self.subTest(label=label), mock.patch.object(environment_receipt, "replay", return_value=receipt):
                code, _result, stderr = run_main(self.world.plan_argv(f"claude-code-2_1_285-{label.encode().hex()[:8]}"))
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)


class ControlReceiptShapeTests(unittest.TestCase):
    TIMING_MINIMAL = {"ledger_dir": "/l", "ledger_plan_sha256": "a" * 64}

    def test_timing_binding_minimal_or_full(self) -> None:
        full = {
            **self.TIMING_MINIMAL,
            "receipt": {"path": "receipts/c.json", "sha256": "b" * 64, "bytes": 1},
            "upgrade_id": "u",
            "evidence_decision": "recapture",
            "checkpoint_head_sha256": "c" * 64,
        }
        for label, timing_binding, ok in (
            ("最小形", self.TIMING_MINIMAL, True),
            ("带升级 ID 的最小形", {**self.TIMING_MINIMAL, "upgrade_id": "u", "evidence_decision": "reuse"}, True),
            ("全形", full, True),
            ("带 checkpoint 但缺 head", {key: value for key, value in full.items() if key != "checkpoint_head_sha256"}, False),
            ("缺账本目录", {"ledger_plan_sha256": "a" * 64}, False),
            ("多出字段", {**self.TIMING_MINIMAL, "extra": 1}, False),
        ):
            with self.subTest(label=label):
                manifest = {"campaign_mode": "formal", "control_receipts": {"upgrade_timing": timing_binding}}
                if ok:
                    campaign_module.validate_control_receipts_shape(manifest)
                else:
                    with self.assertRaises(ConfigurationError):
                        campaign_module.validate_control_receipts_shape(manifest)
        with self.assertRaisesRegex(ConfigurationError, "完整控制收据"):
            campaign_module.verify_control_receipts(
                {"campaign_mode": "preflight_only", "control_receipts": {"upgrade_timing": self.TIMING_MINIMAL}}, require_complete=True
            )

    def test_mode_closed_names_and_binding_fields(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "不允许"):
            campaign_module.validate_control_receipts_shape(
                {"campaign_mode": "preflight_only", "control_receipts": {"p0_gate": {}}}
            )
        bad_environment = {"evidence_root": "/e", "receipt": {"path": "r.json", "sha256": "a" * 64}, "subject_id": "u",
                           "contract_sha256": "a" * 64, "continuity_identity_sha256": "a" * 64}
        with self.assertRaisesRegex(ConfigurationError, "绑定字段不闭合"):
            campaign_module.validate_control_receipts_shape(
                {"campaign_mode": "formal", "control_receipts": {"arm64_environment": bad_environment}}
            )
        relative_root = {**bad_environment, "receipt": {"path": "r.json", "sha256": "a" * 64, "bytes": 3}, "evidence_root": "rel"}
        with self.assertRaisesRegex(ConfigurationError, "绝对路径"):
            campaign_module.validate_control_receipts_shape(
                {"campaign_mode": "formal", "control_receipts": {"arm64_environment": relative_root}}
            )

    def test_creating_campaign_switch(self) -> None:
        namespace = mock.Mock(spec=[])
        self.assertFalse(campaign_module.creating_campaign(namespace))


class ProductionInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store_root, self.campaign_id = build_predecessor_store(Path(directory.name).resolve())

    def test_extracts_latest_profile_approval_verbatim(self) -> None:
        inventory = campaign_module.extract_production_inventory(self.store_root, self.campaign_id)
        store = ControlStore.open_read_only(self.store_root)
        fact = [item for item in store.list_facts(self.campaign_id) if item["fact_kind"] == "profile_approved"][-1]
        self.assertEqual(inventory["source"]["profile_approved_fact_ref"], store.fact_ref(fact))
        self.assertEqual(inventory["egress_target_dispositions"], fact["payload"]["egress_target_dispositions"])
        self.assertEqual(
            [item["object_kind"] for item in inventory["objects"]], list(campaign_module.PRODUCTION_OBJECT_KINDS)
        )
        for item in inventory["objects"]:
            self.assertEqual(store.load_object({"object_kind": item["object_kind"], "sha256": item["sha256"]})["payload"], item["payload"])
        current = campaign_module.current_dispositions(inventory)
        self.assertEqual(current["egress"]["aux-refresh"]["disposition"], "non_persona_managed")
        self.assertEqual(current["ingress"]["third-responses"]["disposition"], "migrated_strict")

    def test_tampering_or_uncovered_targets_are_rejected(self) -> None:
        inventory = campaign_module.extract_production_inventory(self.store_root, self.campaign_id)
        tampered = json.loads(json.dumps(inventory))
        tampered["objects"][3]["payload"]["entries"][0]["current_disposition"] = "denied"
        with self.assertRaisesRegex(ConfigurationError, "摘要与内容不一致|不满足 Store 合同"):
            campaign_module.validate_production_inventory(tampered)
        uncovered = json.loads(json.dumps(inventory))
        uncovered["egress_target_dispositions"] = uncovered["egress_target_dispositions"][1:]
        with self.assertRaisesRegex(ConfigurationError, "出站目标处置"):
            campaign_module.validate_production_inventory(uncovered)
        with self.assertRaisesRegex(ConfigurationError, "ProfileApprovalFact|读不出"):
            campaign_module.extract_production_inventory(self.store_root, "missing-campaign")


class StoreIdentityCorrespondenceTests(unittest.TestCase):
    def test_official_artifacts_must_carry_freeze_and_binary(self) -> None:
        manifest = {
            "campaign_id": "c1",
            "target_version": TARGET,
            "client": "claude",
            "official_identity": {"binary_sha256": "b" * 64},
            "configuration": {"platform": "linux/arm64", "entrypoint": "sdk-cli", "privacy_mode": "p", "oauth_org_id": "o",
                              "model_catalog_sha256": "c" * 64, "official_freeze_sha256": "a" * 64},
        }
        campaign = {
            "campaign_id": "c1", "schema_version": "official-client-control-campaign/v2", "client": "claude",
            "target_version": TARGET, "platforms": ["linux/arm64"], "entrypoints": ["sdk-cli"], "default_conditions": ["privacy=p"],
            "oauth_org_id": "o", "model_catalog_sha256": "c" * 64,
            "official_artifacts": [{"path": "official/freeze.json", "sha256": "a" * 64, "bytes": 1}],
        }
        with self.assertRaisesRegex(ConfigurationError, "目标二进制"):
            claude_upgrade_store.require_store_campaign_matches_plan(manifest, campaign)
        campaign["official_artifacts"].append({"path": "official/claude", "sha256": "b" * 64, "bytes": 1})
        claude_upgrade_store.require_store_campaign_matches_plan(manifest, campaign)


class FirstBatchShapeTests(unittest.TestCase):
    def test_first_batch_is_single_capture_action_like_codex(self) -> None:
        action = campaign_module.first_batch_action(Path("/data/evidence/campaigns/c1"), ["official-b", "official-a"], timeout_seconds=3600)
        self.assertEqual((action["action_id"], action["operation"]), ("capture-official", "VC-1:capture-official"))
        self.assertEqual(action["item_ids"], ["official-a", "official-b"])
        self.assertEqual(
            action["command"][2:], ["capture-official", "run", "--campaign-dir", "/data/evidence/campaigns/c1", "--acknowledge-live-requests"]
        )
        self.assertEqual(Path(action["command"][1]).name, "claude_upgrade.py")
        self.assertEqual(campaign_module.first_batch_timeout(12, remaining_seconds=0), 3600)
        self.assertEqual(campaign_module.first_batch_timeout(30, remaining_seconds=0), 7200)
        self.assertEqual(campaign_module.first_batch_timeout(30, remaining_seconds=1000), 1000)



# 模型目录摘要的定义（10-08 定）固定在下面这个值上：production_active Release 登记 claude-sonnet-5、claude-opus-5、
# claude-fable-5 三个模型（按此顺序）。用内联最小目录固定，不对入库目录固定值——VC-6 切换 production_active 后入库目录会变，
# 那时参数文件的 MODEL_CATALOG_SHA256 随之重算，这条不该挡住主线。
CURRENT_MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"]
CURRENT_MODEL_CATALOG_SHA256 = "49096e9db8faff6786f7cf92cb4a209de038b8ce1265c30b8ce8bb940e9a29ea"
REPOSITORY_RELEASE_CATALOG = (
    Path(__file__).resolve().parents[3] / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "release-catalog.json"
)


def minimal_release_catalog(models: Any = None, **overrides: Any) -> dict[str, Any]:
    """只含选择与模型字段的最小 Release 目录：production_active 指向第一个 Release，另有一个不被选中的 Release。"""

    catalog = {
        "schema_version": "official-egress-claude-release-catalog/v1",
        "releases": [
            {"release_sha256": "a" * 64, "models": list(CURRENT_MODELS) if models is None else models},
            {"release_sha256": "b" * 64, "models": ["claude-other-5"]},
        ],
        "selectors": {
            "production_active": {"kind": "release", "release_sha256": "a" * 64},
            "validation_candidate": {"kind": "release", "release_sha256": "b" * 64},
        },
    }
    catalog.update(overrides)
    return catalog


class ReleaseModelCatalogDigestTests(unittest.TestCase):
    def test_definition_fixes_the_current_value(self) -> None:
        self.assertEqual(campaign_module.release_model_catalog_sha256(minimal_release_catalog()), CURRENT_MODEL_CATALOG_SHA256)
        # 只看 production_active 指向的 Release；列表顺序参与摘要。
        moved = minimal_release_catalog()
        moved["selectors"]["production_active"]["release_sha256"] = "b" * 64
        self.assertNotEqual(campaign_module.release_model_catalog_sha256(moved), CURRENT_MODEL_CATALOG_SHA256)
        reordered = minimal_release_catalog(list(reversed(CURRENT_MODELS)))
        self.assertNotEqual(campaign_module.release_model_catalog_sha256(reordered), CURRENT_MODEL_CATALOG_SHA256)

    def test_repository_catalog_is_accepted(self) -> None:
        catalog = json.loads(REPOSITORY_RELEASE_CATALOG.read_text(encoding="utf-8"))
        self.assertRegex(campaign_module.release_model_catalog_sha256(catalog), r"^[0-9a-f]{64}$")

    def test_malformed_catalogs_are_rejected(self) -> None:
        missing = minimal_release_catalog()
        missing["selectors"]["production_active"]["release_sha256"] = "c" * 64
        duplicated = minimal_release_catalog()
        duplicated["releases"].append(dict(duplicated["releases"][0]))
        operational = minimal_release_catalog()
        operational["selectors"]["production_active"] = {"kind": "operational-deployment"}
        cases = {
            "不是目录合同": (minimal_release_catalog(schema_version="official-egress-release-catalog/v1"), "不是"),
            "没有 Release 列表": (minimal_release_catalog(releases={}), "不是"),
            "选择器不是 Release": (operational, "不是 Release"),
            "没有选择器": (minimal_release_catalog(selectors={}), "不是 Release"),
            "指向不存在的 Release": (missing, "不唯一或不存在"),
            "指向两个 Release": (duplicated, "不唯一或不存在"),
            "模型列表为空": (minimal_release_catalog([]), "模型列表"),
            "模型列表不是列表": (minimal_release_catalog("claude-sonnet-5"), "模型列表"),
            "含非字符串": (minimal_release_catalog(["claude-sonnet-5", 5]), "模型列表"),
            "含空串": (minimal_release_catalog(["claude-sonnet-5", ""]), "模型列表"),
            "有重复": (minimal_release_catalog(["claude-sonnet-5", "claude-sonnet-5"]), "模型列表"),
        }
        for label, (catalog, pattern) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    campaign_module.release_model_catalog_sha256(catalog)


if __name__ == "__main__":
    unittest.main()
