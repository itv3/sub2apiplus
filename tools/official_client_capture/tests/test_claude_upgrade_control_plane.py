"""Claude 升级控制面：共享监督器、对账器、项目总账与计时账本按客户端工作（Claude 升级控制面设计 B1-02～B1-05）。

监督器（B1-02）：
* campaign-run 清单：Codex 沿用 v1～v3、不带 client、隐含 codex；Claude 用 ``official-client-campaign-run/v1``（v2 结构加
  client，只允许 claude）；字段不闭合、client 非法、与 Campaign 总计划归属不符一律拒绝；
* 预声明动作按清单所属客户端识别编排器 CLI 与禁止子命令；
* 后继协议与批次历史要求前后清单属于同一客户端；
* 根因维度按码表补齐 client（Codex 码表不含，根因 ID 逐字不变），Claude Campaign ID 作维度值被拒；
* 监督器不再导入编排器 codex_upgrade（编排器符号全部经适配器）。

对账器（B1-03）：入口按 adapter 参数或 Campaign 归属设定适配器、传错即拒绝；内部函数在入口之外缺省 Codex；
恢复接口用失败源的候选坐标。

项目总账（B1-04）：
* Claude 总账用 Claude 码表创建（v2 plan 带 client），``status``／``reconcile`` 不被默认码表拒绝；
* Claude 入口（Campaign 声明 claude，或显式 client）遇到缺 client 的 plan 即拒绝，Campaign 与总账归属不一致即拒绝；
* 两客户端共用数据根时各找各的总账，v2 注册事件、Campaign 账本 plan 与 ``registered_campaigns`` 带 client；
* ``record-code-migration`` 写出的映射收据被 ``_load_plan`` 按客户端码表衔接；
* Codex 缺省与 ``--client codex`` 都写 v1 plan，产物与分桶结果逐字相同；候选探针计量类别按客户端。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_client_adapter as claude_adapter
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_root_cause as rc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import upgrade_client_adapter as uca

TOOL_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = {"path": "control/vc/vc-0-checkpoint.json", "sha256": "c" * 64, "phase": "VC-0", "checkpoint_sha256": "d" * 64}


def _build(**extra):
    arguments = dict(
        campaign_id="fixture-campaign", campaign_plan_sha256="a" * 64, batch_id="batch-0001", batch_sequence=1,
        batch_sha256="b" * 64, phase="VC-1", predecessor_checkpoint=CHECKPOINT,
        original_deadline_at_utc="2026-10-10T00:00:00Z", actions=[], execute_items=[], reuse_items=[],
    )
    arguments.update(extra)
    return supervisor.build_batched_campaign_run_manifest(**arguments)


def _build_with_action(command: list[str], client: str | None = None):
    action = {
        "action_id": "step-1", "operation": "VC-1:capture-official", "timeout_seconds": 60,
        "command": command, "item_ids": ["step-1"],
    }
    return _build(actions=[action], execute_items=["step-1"], client=client)


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class RunManifestClientTests(unittest.TestCase):
    def test_codex_manifest_keeps_v2_and_implies_codex(self) -> None:
        manifest = _build()
        self.assertEqual(manifest["schema_version"], supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA)
        self.assertNotIn("client", manifest)
        self.assertEqual(supervisor.run_manifest_client(manifest), "codex")

    def test_claude_manifest_uses_new_contract(self) -> None:
        manifest = _build(client="claude")
        self.assertEqual(manifest["schema_version"], supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA)
        self.assertEqual(manifest["client"], "claude")
        self.assertEqual(supervisor.run_manifest_client(manifest), "claude")
        codex = _build()
        strip = lambda payload, keys: {key: value for key, value in payload.items() if key not in keys}  # noqa: E731
        self.assertEqual(strip(manifest, {"schema_version", "client"}), strip(codex, {"schema_version"}))

    def test_new_contract_rejects_codex_and_unknown_clients(self) -> None:
        for client in ("codex", "gemini", ""):
            with self.subTest(client=client), self.assertRaises(supervisor.SupervisorError):
                _build(client=client)

    def test_parser_requires_closed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = dict(_build(client="claude"))
            cases = {
                "新合同缺 client": {key: value for key, value in claude.items() if key != "client"},
                "v2 带 client": dict(_build(), client="claude"),
            }
            for label, payload in cases.items():
                with self.subTest(label=label), self.assertRaises(supervisor.SupervisorError):
                    supervisor._campaign_run_manifest(_write(root / f"{len(label)}.json", payload))

    def test_campaign_owner_must_match_manifest_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_campaign = root / "claude"
            _write(claude_campaign / "campaign.json", {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude"})
            codex_campaign = root / "codex"
            _write(codex_campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            self.assertEqual(supervisor._run_manifest_adapter(_build(client="claude"), claude_campaign).client, "claude")
            self.assertEqual(supervisor._run_manifest_adapter(_build(), codex_campaign).client, "codex")
            self.assertEqual(supervisor._run_manifest_adapter(_build(), None).client, "codex")
            for manifest, campaign in ((_build(client="claude"), codex_campaign), (_build(), claude_campaign)):
                with self.subTest(schema=manifest["schema_version"]), self.assertRaises(supervisor.SupervisorError):
                    supervisor._run_manifest_adapter(manifest, campaign)

    def test_supervisor_no_longer_imports_orchestrator(self) -> None:
        tree = ast.parse((TOOL_ROOT / "codex_upgrade_supervisor.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [alias.name for alias in node.names if alias.name.rsplit(".", 1)[-1] == "codex_upgrade"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").rsplit(".", 1)[-1] == "codex_upgrade":
                    offenders.append(node.module or "")
                offenders += [alias.name for alias in node.names if alias.name == "codex_upgrade"]
        self.assertEqual(offenders, [])


class PredeclaredActionClientTests(unittest.TestCase):
    def test_forbidden_subcommands_follow_manifest_client(self) -> None:
        for subcommand in ("plan", "freeze", "import-predecessor-evidence", "reconcile-attempt"):
            with self.subTest(client="claude", subcommand=subcommand), self.assertRaisesRegex(supervisor.SupervisorError, "控制面"):
                _build_with_action(["python3", "claude_upgrade.py", subcommand], client="claude")
        _build_with_action(["python3", "claude_upgrade.py", "capture-official", "run"], client="claude")
        with self.assertRaisesRegex(supervisor.SupervisorError, "控制面"):
            _build_with_action(["python3", "codex_upgrade.py", "plan"])
        # Codex 清单与改造前一致：claude_upgrade.py 不是 Codex 编排器，不按编排器子命令拦截。
        _build_with_action(["python3", "claude_upgrade.py", "plan"])


class SuccessorClientTests(unittest.TestCase):
    def test_successor_protocols_require_same_client(self) -> None:
        codex, claude = _build(), _build(client="claude")
        self.assertTrue(supervisor._same_client_batched_runs(codex, codex))
        self.assertTrue(supervisor._same_client_batched_runs(claude, claude))
        self.assertFalse(supervisor._same_client_batched_runs(codex, claude))
        self.assertFalse(supervisor._same_client_batched_runs(claude, codex))
        self.assertFalse(supervisor._same_client_batched_runs({"schema_version": supervisor.CAMPAIGN_RUN_SCHEMA}, codex))


class RootCauseDimensionTests(unittest.TestCase):
    def test_dimensions_follow_client_code_table(self) -> None:
        codex, claude = uca.adapter_for("codex"), uca.adapter_for("claude")
        codex_codes = rc.load_codes(codex.root_cause_codes_path)
        claude_codes = rc.load_codes(claude.root_cause_codes_path)
        code = "campaign-run.action-failed"
        self.assertEqual(uca.root_cause_dimensions(codex, codex_codes, code, {"phase": "VC-1"}), {"phase": "VC-1"})
        self.assertEqual(
            uca.root_cause_dimensions(claude, claude_codes, code, {"phase": "VC-1"}), {"phase": "VC-1", "client": "claude"}
        )
        before = rc.structured_root_cause(
            component="supervisor", stable_error_code=code, failed_step="capture-official", stable_dimensions={"phase": "VC-1"},
        )
        after = rc.structured_root_cause(
            component="supervisor", stable_error_code=code, failed_step="capture-official",
            stable_dimensions=uca.root_cause_dimensions(codex, codex_codes, code, {"phase": "VC-1"}), codes=codex_codes,
        )
        self.assertEqual(after, before)
        with self.assertRaisesRegex(rc.RootCauseError, "Claude Campaign ID"):
            uca.root_cause_dimensions(
                claude, claude_codes, "claude.capture.relay-failed", {"phase": "VC-1", "scenario": "claude-code-2.1.286-formal"}
            )


class ReconcilerClientTests(unittest.TestCase):
    """对账器（B1-03）：入口按 adapter 参数或 Campaign 归属设定适配器，传错即拒绝；内部函数缺省 Codex；
    恢复接口用失败源的候选坐标。"""

    def test_internal_functions_default_to_codex(self) -> None:
        self.assertEqual(reconciler._adapter().client, "codex")

    def test_scope_follows_campaign_and_rejects_wrong_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_campaign = root / "codex"
            _write(codex_campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            claude_campaign = root / "claude"
            _write(claude_campaign / "campaign.json", {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude"})
            bare = root / "bare"
            bare.mkdir()
            with reconciler._client_scope(codex_campaign, None) as adapter:
                self.assertEqual((adapter.client, reconciler._adapter().client), ("codex", "codex"))
            with reconciler._client_scope(claude_campaign, None):
                self.assertEqual(reconciler._adapter().client, "claude")
                with reconciler._client_scope(claude_campaign, None):
                    self.assertEqual(reconciler._adapter().client, "claude")
            self.assertEqual(reconciler._adapter().client, "codex")
            with reconciler._client_scope(bare, None) as adapter:
                self.assertEqual(adapter.client, "codex")
            for campaign, wrong in ((codex_campaign, "claude"), (claude_campaign, "codex"), (bare, "claude")):
                with self.subTest(campaign=campaign.name), self.assertRaisesRegex(reconciler.ReconcilerError, "拒绝对账"):
                    with reconciler._client_scope(campaign, uca.adapter_for(wrong)):
                        pass

    def test_entry_points_reject_wrong_adapter_before_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "codex"
            _write(campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            with self.assertRaisesRegex(reconciler.ReconcilerError, "拒绝对账"):
                reconciler.reconcile_attempt(campaign, "a1", adapter=uca.adapter_for("claude"))

    def test_recovery_reuse_check_uses_source_candidate(self) -> None:
        recorded: dict[str, object] = {}

        def fake(campaign_dir, **kwargs):
            recorded.update(kwargs)
            return {"status": "consistent"}

        preview = {
            "source_attempt_receipt_exists": True, "candidate_id": "cand-v7", "source_attempt_id": "a7",
            "reuse_job_ids": ["j1"], "execute_job_ids": ["j2"],
        }
        with mock.patch.object(codex_upgrade, "recovery_reuse_check", side_effect=fake):
            reconciler._resume_reuse_check(Path("/c"), phase="candidate", recovery_revision=None, preview=preview)
        self.assertEqual((recorded["candidate_id"], recorded["source_attempt_id"]), ("cand-v7", "a7"))


CLAUDE_VERSION = "2.1.286"
CODEX_VERSION = "0.160.0"


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ledger_campaign(root: Path, campaign_id: str, *, client: str | None, target_version: str) -> Path:
    """在数据根 ``root`` 的规范布局下建 Campaign 目录与总计划：Claude 用新合同并显式声明 client，Codex 不带（历史形态）。"""

    campaigns = root / "evidence" / "campaigns"
    campaigns.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (root / "evidence", campaigns):
        directory.chmod(0o700)
    campaign_dir = campaigns / campaign_id
    campaign_dir.mkdir(mode=0o700)
    manifest = {"campaign_id": campaign_id, "campaign_mode": "formal", "target_version": target_version}
    if client is not None:
        manifest.update(schema_version="official-client-upgrade-campaign/v1", client=client)
    _write(campaign_dir / "campaign.json", manifest)
    return campaign_dir


def _ledger_create(root: Path, *, client: str | None, name: str | None = None, **extra) -> Path:
    """在 ``root`` 下建项目总账：目录名缺省按客户端取。"""

    ledger_root = root / (name or ledger.ledger_dir_name(client or "codex"))
    options = dict(
        project_id="claude-b1-04", absolute_deadline_utc=_iso(datetime.now(timezone.utc) + timedelta(hours=48)),
        deadline_approved_by="老板", estimation_policy="none", estimation_policy_approved_by="老板", fixture_only=False,
        client=client,
    )
    options.update(extra)
    ledger.create_project_ledger(ledger_root, **options)
    return ledger_root


def _ledger_cli(argv: list[str]) -> tuple[int, dict]:
    """调项目总账 CLI 并捕获输出；main 会把进程 umask 改成 077，调用后恢复。"""

    previous = os.umask(0o077)
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = ledger.main(argv)
    finally:
        os.umask(previous)
    return code, (json.loads(output.getvalue()) if code == 0 else {})


def _ledger_reconciliation(campaign_dir: Path, operation_id: str, root_cause_id: str) -> None:
    """在 Campaign outbox 写一个带根因的对账 batch（请求部分已解析、无新身份键）。"""

    with ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
        ledger.write_batch(
            ledger_dir,
            operation_id=operation_id,
            event_type="reconciliation_committed",
            payload={
                "campaign_id": campaign_dir.name,
                "request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0, "provenance_receipt_sha256": "b" * 64},
                "root_cause": {"root_cause_id": root_cause_id},
            },
            source={"kind": "campaign_event", "sha256": "c" * 64},
        )


class ProjectLedgerClientTests(unittest.TestCase):
    """项目总账（B1-04）：两客户端共用数据根、各有一本总账；Claude 用 v2 plan 与 Claude 码表，Codex v1 逐字不变。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)

    def test_ledger_dir_names_and_plan_contracts_follow_client(self) -> None:
        self.assertEqual(ledger.ledger_dir_name("codex"), ledger.LEDGER_DIR_NAME)
        self.assertEqual(ledger.ledger_dir_name("claude"), "upgrade-project-ledger-claude")
        self.assertEqual(ledger.ledger_dir_name("claude"), claude_adapter.PROJECT_LEDGER_DIR_NAME)
        with self.assertRaises(ledger.ProjectLedgerError):
            ledger.ledger_dir_name("gemini")
        self.assertEqual(uca.adapter_for("codex").project_ledger_plan_schema, ledger.PLAN_SCHEMA)
        self.assertEqual(uca.adapter_for("claude").project_ledger_plan_schema, ledger.PLAN_SCHEMA_V2)
        # Claude 总账只能建在 Claude 的总账目录名下：Codex 按目录名查找时永远看不到它。
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "目录名必须是"):
            _ledger_create(self.root, client="claude", name=ledger.LEDGER_DIR_NAME)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "未知客户端"):
            _ledger_create(self.root, client="gemini", name="gemini-ledger")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [])

    def test_claude_ledger_uses_claude_codes_for_status_and_reconcile(self) -> None:
        ledger_root = self.root / ledger.ledger_dir_name("claude")
        code, created = _ledger_cli([
            "create-project-ledger", "--ledger-dir", str(ledger_root), "--project-id", "claude-2-1-286",
            "--absolute-deadline-utc", _iso(datetime.now(timezone.utc) + timedelta(hours=48)),
            "--deadline-approved-by", "老板", "--estimation-policy", "none", "--estimation-policy-approved-by", "老板",
            "--client", "claude",
        ])
        self.assertEqual(code, 0)
        plan = json.loads((ledger_root / "plan.json").read_text(encoding="utf-8"))
        claude_codes = rc.load_codes(claude_adapter.ROOT_CAUSE_CODES_PATH)
        self.assertEqual((plan["schema_version"], plan["client"]), (ledger.PLAN_SCHEMA_V2, "claude"))
        self.assertEqual(set(plan), ledger.PLAN_FIELDS | {"client"})
        self.assertEqual(plan["root_cause_codes_sha256"], claude_codes["codes_sha256"])
        self.assertNotEqual(plan["root_cause_codes_sha256"], rc.load_codes()["codes_sha256"])
        self.assertEqual(created["plan_sha256"], plan["plan_sha256"])
        # 改造前的读法（默认码表核对冻结身份）会拒绝这本总账。
        with self.assertRaises(rc.RootCauseError):
            rc.assert_codes_identity(
                expected_codes_sha256=plan["root_cause_codes_sha256"],
                expected_algorithm_version=plan["root_cause_algorithm_version"],
            )
        for argv in (["status", "--ledger-dir", str(ledger_root)], ["reconcile-project-ledger", "--ledger-dir", str(ledger_root)]):
            with self.subTest(command=argv[0]):
                code, result = _ledger_cli(argv)
                self.assertEqual(code, 0)
                self.assertEqual(result["head_sha256"], plan["plan_sha256"])

    def test_claude_entries_reject_plan_without_client(self) -> None:
        # Claude 总账目录下放的是缺 client 的 v1 plan（只按 codex 解释）：Claude 各入口一律拒绝，且不写入任何东西。
        misplaced = _ledger_create(self.root, client=None, name=ledger.ledger_dir_name("claude"))
        self.assertNotIn("client", json.loads((misplaced / "plan.json").read_text(encoding="utf-8")))
        campaign = _ledger_campaign(self.root, "claude-c1", client="claude", target_version=CLAUDE_VERSION)
        self.assertEqual(ledger.find_project_ledger(campaign), misplaced)
        rejected = "属于 codex，与 claude 不一致"
        with self.assertRaisesRegex(ledger.ProjectLedgerError, rejected):
            ledger.assert_campaign_admitted(campaign, command="plan", require=True)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, rejected):
            with ledger.runtime_admission_scope(campaign, require=True):
                pass
        with self.assertRaisesRegex(ledger.ProjectLedgerError, rejected):
            with ledger.admission_scope(
                campaign, campaign_id="claude-c1", campaign_mode="formal", target_version=CLAUDE_VERSION,
                require=True, client="claude",
            ):
                pass
        with self.assertRaisesRegex(ledger.ProjectLedgerError, rejected):
            ledger.register_existing_campaign(campaign)
        self.assertEqual(ledger.replay_head(misplaced)["sequence"], 0)
        self.assertFalse((campaign / ledger.CAMPAIGN_LEDGER_DIR_NAME).exists())
        # 不能用别的客户端的入口拿这个 Campaign 去找或用总账。
        codex_campaign = _ledger_campaign(self.root, "codex-c1", client=None, target_version=CODEX_VERSION)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "Campaign 属于 codex，不能按 claude"):
            ledger.find_project_ledger(codex_campaign, client="claude")

    def test_two_clients_share_data_root_and_registration_carries_client(self) -> None:
        codex_root = _ledger_create(self.root, client=None)
        claude_root = _ledger_create(self.root, client="claude")
        claude_campaign = _ledger_campaign(self.root, "claude-c1", client="claude", target_version=CLAUDE_VERSION)
        codex_campaign = _ledger_campaign(self.root, "codex-c1", client=None, target_version=CODEX_VERSION)
        campaigns = self.root / "evidence" / "campaigns"
        self.assertEqual(ledger.find_project_ledger(claude_campaign), claude_root)
        self.assertEqual(ledger.find_project_ledger(codex_campaign), codex_root)
        self.assertEqual(ledger.find_project_ledger(campaigns), codex_root)
        self.assertEqual(ledger.find_project_ledger(campaigns, client="claude"), claude_root)

        self.assertEqual(ledger.register_existing_campaign(claude_campaign)["status"], "appended")
        self.assertEqual(ledger.register_existing_campaign(codex_campaign)["status"], "appended")
        claude_plan = json.loads((claude_campaign / "ledger" / "plan.json").read_text(encoding="utf-8"))
        codex_plan = json.loads((codex_campaign / "ledger" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual((claude_plan["schema_version"], claude_plan["client"]), (ledger.CAMPAIGN_PLAN_SCHEMA_V2, "claude"))
        self.assertEqual((codex_plan["schema_version"], set(codex_plan)), (ledger.CAMPAIGN_PLAN_SCHEMA, ledger.CAMPAIGN_PLAN_FIELDS))
        claude_head, codex_head = ledger.replay_head(claude_root), ledger.replay_head(codex_root)
        self.assertEqual((sorted(claude_head["registered_campaigns"]), sorted(codex_head["registered_campaigns"])), (["claude-c1"], ["codex-c1"]))
        self.assertEqual(claude_head["registered_campaigns"]["claude-c1"]["client"], "claude")
        self.assertNotIn("client", codex_head["registered_campaigns"]["codex-c1"])
        self.assertEqual(json.loads((claude_root / "events" / "000001.json").read_text(encoding="utf-8"))["payload"]["client"], "claude")
        self.assertNotIn("client", json.loads((codex_root / "events" / "000001.json").read_text(encoding="utf-8"))["payload"])

        # 分桶键 (client, target_version)：Claude 根因只进 Claude 总账的 Claude 版本桶，Codex 总账不受影响。
        cause = rc.structured_root_cause(
            component="supervisor", stable_error_code="campaign-run.action-failed", failed_step="capture-official",
            stable_dimensions={"phase": "VC-1", "client": "claude"}, codes=rc.load_codes(claude_adapter.ROOT_CAUSE_CODES_PATH),
        )
        _ledger_reconciliation(claude_campaign, "claude-rec-1", cause)
        ledger.reconcile_project_ledger(claude_root, campaign_dir=claude_campaign)
        claude_head = ledger.replay_head(claude_root)
        self.assertEqual(claude_head["root_cause_counts_by_version"], {CLAUDE_VERSION: {cause: 1}})
        self.assertEqual(ledger.root_cause_counts_for(claude_head, CLAUDE_VERSION), {cause: 1})
        self.assertEqual(ledger.replay_head(codex_root)["root_cause_counts"], {})
        admitted = ledger.assert_campaign_admitted(claude_campaign, command="plan", require=True)
        self.assertEqual(admitted["project_ledger"], str(claude_root))

        # 身份链：Claude Campaign 进不了 Codex 总账；v2 总账拒绝不带 client 的注册事件。
        stray = _ledger_campaign(self.root, "claude-c2", client="claude", target_version=CLAUDE_VERSION)
        with ledger.project_lock(codex_root):
            head_sha256 = ledger.replay_head(codex_root)["head_sha256"]
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "注册 Campaign：.*属于 codex，与 claude 不一致"):
                ledger.register_campaign(
                    codex_root, stray, campaign_id="claude-c2", campaign_mode="formal", target_version=CLAUDE_VERSION,
                    deadline_at_utc=None, admission_head_sha256=head_sha256,
                )
        self.assertFalse((stray / ledger.CAMPAIGN_LEDGER_DIR_NAME).exists())
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "client None 与总账 claude 不一致"):
            ledger.append_project_event(
                claude_root, operation_id="register:forged", event_type="campaign_registered",
                payload={"campaign_id": "forged", "campaign_mode": "formal", "target_version": CLAUDE_VERSION},
                source_batch_sha256=None,
            )

    def test_record_code_migration_links_with_load_plan(self) -> None:
        claude_root = _ledger_create(self.root, client="claude")
        original_path = claude_adapter.ROOT_CAUSE_CODES_PATH
        frozen = rc.load_codes(original_path)
        upgraded = json.loads(original_path.read_text(encoding="utf-8"))
        upgraded["codes"]["claude.store.replay-drift"] = {"component": "claude-store", "stable_dimensions": ["phase", "client"]}
        upgraded_path = self.root / "claude-root-cause-codes-next.json"
        upgraded_path.write_text(json.dumps(upgraded, ensure_ascii=False), encoding="utf-8")
        target = rc.load_codes(upgraded_path)
        argv = [
            "record-code-migration", "--ledger-dir", str(claude_root), "--from-codes", str(original_path),
            "--to-codes", str(upgraded_path), "--approved-by", "老板",
        ]
        with mock.patch.object(claude_adapter, "ROOT_CAUSE_CODES_PATH", upgraded_path):
            # Claude 码表已变更、还没有映射收据：总账按 Claude 码表核对冻结身份，拒绝服务。
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "没有衔接的旧新 ID 映射收据"):
                ledger.replay_head(claude_root)
            self.assertEqual(_ledger_cli([*argv, "--id-mapping", "[]"])[0], 2)
            code, written = _ledger_cli([*argv, "--id-mapping", json.dumps({"rc1-old": "rc1-new"})])
            self.assertEqual(code, 0)
            path = claude_root / "migrations" / "000001.json"
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(receipt), {
                "schema_version", "sequence", "from_codes_sha256", "to_codes_sha256", "from_algorithm_version",
                "to_algorithm_version", "id_mapping", "approved_by", "approved_at_utc",
            })
            self.assertEqual(
                (receipt["schema_version"], receipt["sequence"], receipt["from_codes_sha256"], receipt["to_codes_sha256"],
                 receipt["id_mapping"], receipt["approved_by"]),
                (ledger.MIGRATION_SCHEMA, 1, frozen["codes_sha256"], target["codes_sha256"], {"rc1-old": "rc1-new"}, "老板"),
            )
            self.assertEqual(written["migration_path"], str(path))
            # 读取方原样接受，_load_plan 沿映射链走到新表并按 Claude 当前码表核对通过。
            self.assertEqual(ledger._code_migrations(claude_root), [receipt])
            plan, _raw = ledger._load_plan(claude_root)
            self.assertEqual(plan["root_cause_codes_sha256"], frozen["codes_sha256"])
            self.assertEqual(ledger.replay_head(claude_root)["sequence"], 0)
            # 不衔接映射链末端、码表身份未变都拒绝，不写收据。
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "不衔接"):
                ledger.record_code_migration(claude_root, from_codes=original_path, to_codes=upgraded_path, approved_by="老板")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "身份相同"):
                ledger.record_code_migration(claude_root, from_codes=upgraded_path, to_codes=upgraded_path, approved_by="老板")
            self.assertEqual(sorted(item.name for item in (claude_root / "migrations").iterdir()), ["000001.json"])
        # 收据链末端是新表：仍按旧表核对的工具拒绝服务（部署新表之后才恢复），映射链与客户端码表一一衔接。
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "没有衔接的旧新 ID 映射收据"):
            ledger.replay_head(claude_root)

    def test_codex_v1_plan_and_buckets_unchanged(self) -> None:
        """缺省与 --client codex 都写 v1：总账、Campaign 账本、事件与 head 逐字节相同，分桶仍按 target_version。"""

        started = _iso(datetime.now(timezone.utc))
        deadline = _iso(datetime.now(timezone.utc) + timedelta(hours=48))
        codex_codes = rc.load_codes()
        cause = rc.structured_root_cause(
            component="supervisor", stable_error_code="campaign-run.action-failed", failed_step="capture-official",
            stable_dimensions={"phase": "VC-1"}, codes=codex_codes,
        )

        def scenario(client: str | None) -> dict[str, bytes]:
            data_root = self.root / "data"
            data_root.mkdir(mode=0o700)
            with mock.patch.object(ledger, "_utc_now", return_value=started):
                ledger_root = _ledger_create(data_root, client=client, absolute_deadline_utc=deadline, started_at_utc=started)
                campaign = _ledger_campaign(data_root, "codex-c1", client=None, target_version=CODEX_VERSION)
                ledger.register_existing_campaign(campaign)
                _ledger_reconciliation(campaign, "codex-rec-1", cause)
                ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign)
                head = ledger.replay_head(ledger_root)
            plan = json.loads((ledger_root / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual((ledger_root.name, plan["schema_version"], set(plan)), (ledger.LEDGER_DIR_NAME, ledger.PLAN_SCHEMA, ledger.PLAN_FIELDS))
            self.assertEqual(plan["root_cause_codes_sha256"], codex_codes["codes_sha256"])
            campaign_plan = json.loads((campaign / "ledger" / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual((campaign_plan["schema_version"], set(campaign_plan)), (ledger.CAMPAIGN_PLAN_SCHEMA, ledger.CAMPAIGN_PLAN_FIELDS))
            self.assertEqual(set(head["registered_campaigns"]["codex-c1"]), {
                "operation_id", "campaign_dir", "campaign_mode", "target_version", "deadline_at_utc",
                "registration_batch_sha256", "sequence",
            })
            self.assertEqual(head["root_cause_counts_by_version"], {CODEX_VERSION: {cause: 1}})
            self.assertEqual(head["root_causes_at_limit_by_version"], {CODEX_VERSION: []})
            snapshot = {str(path.relative_to(data_root)): path.read_bytes() for path in sorted(data_root.rglob("*")) if path.is_file()}
            shutil.rmtree(data_root)
            return snapshot

        default, explicit = scenario(None), scenario("codex")
        self.assertEqual(default, explicit)
        self.assertNotIn(b'"client"', default[f"{ledger.LEDGER_DIR_NAME}/plan.json"])

    def test_candidate_probe_accounting_category_follows_ledger_client(self) -> None:
        claude_root = _ledger_create(self.root, client="claude", live_request_budget=10)
        campaign = _ledger_campaign(self.root, "claude-c1", client="claude", target_version=CLAUDE_VERSION)
        ledger.register_existing_campaign(campaign)
        with ledger.runtime_admission_scope(campaign, require=True) as admission:
            assert admission is not None
            result = admission.account_candidate_probe(
                campaign_id="claude-c1", candidate_id="candidate-a", dispatch_id="dispatch-a", identity_key="1" * 64,
                receipt_sha256="2" * 64, response_status=200, expected_head_sha256=admission.head_sha256,
            )
        self.assertEqual(result["status"], "appended")
        probe = json.loads(sorted((claude_root / "events").iterdir())[-1].read_text(encoding="utf-8"))
        self.assertEqual(probe["payload"]["accounting_category"], claude_adapter.CANDIDATE_PROBE_ACCOUNTING_CATEGORY)
        # Codex 的计量类别进不了 Claude 总账。
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "计量类别非法"):
            ledger.append_project_event(
                claude_root, operation_id="candidate-probe:codex-category", event_type="candidate_probe_accounted",
                payload={
                    "campaign_id": "claude-c1", "accounting_category": "candidate_readiness_models_probe/v1",
                    "request": {"status": "resolved", "identity_keys": ["3" * 64], "estimated_delta": 0},
                },
                source_batch_sha256=None,
            )

    def test_fixture_ledger_follows_client(self) -> None:
        data_root = self.root / "staging" / "rehearsal"
        data_root.mkdir(parents=True, mode=0o700)
        (self.root / "staging").chmod(0o700)
        claude_root = ledger.create_fixture_ledger(data_root, client="claude")
        codex_root = ledger.create_fixture_ledger(data_root)
        self.assertEqual((claude_root.name, codex_root.name), (ledger.ledger_dir_name("claude"), ledger.LEDGER_DIR_NAME))
        self.assertEqual(ledger.create_fixture_ledger(data_root, client="claude"), claude_root)
        plan = json.loads((claude_root / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["schema_version"], plan["client"], plan["fixture_only"]), (ledger.PLAN_SCHEMA_V2, "claude", True))
        self.assertNotIn("client", json.loads((codex_root / "plan.json").read_text(encoding="utf-8")))
        # Claude 夹具目录里若是缺 client 的 plan，同样拒绝。
        other = self.root / "staging" / "other"
        other.mkdir(mode=0o700)
        _ledger_create(other, client=None, name=ledger.ledger_dir_name("claude"), fixture_only=True)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "属于 codex，与 claude 不一致"):
            ledger.create_fixture_ledger(other, client="claude")

    def test_v2_contracts_are_claude_only(self) -> None:
        claude_root = _ledger_create(self.root, client="claude")
        plan = json.loads((claude_root / "plan.json").read_text(encoding="utf-8"))
        ledger._validate_plan(plan)

        def resign(payload: dict) -> dict:
            payload = dict(payload)
            payload["plan_sha256"] = ledger._digest({key: value for key, value in payload.items() if key != "plan_sha256"})
            return payload

        for label, forged, pattern in (
            ("v2 带 codex", resign(dict(plan, client="codex")), "client 非法"),
            ("v2 未知客户端", resign(dict(plan, client="gemini")), "未知客户端"),
            ("v2 缺 client", resign({key: value for key, value in plan.items() if key != "client"}), "字段集合不闭合"),
            ("v1 带 client", resign(dict(plan, schema_version=ledger.PLAN_SCHEMA)), "字段集合不闭合"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(ledger.ProjectLedgerError, pattern):
                ledger._validate_plan(forged)
        # Campaign 账本 plan 的 v2 同样只认 Claude。
        ledger_dir = self.root / "campaign-ledger"
        ledger_dir.mkdir(mode=0o700)
        _write(ledger_dir / "plan.json", {
            "schema_version": ledger.CAMPAIGN_PLAN_SCHEMA_V2, "client": "codex", "campaign_id": "c1", "campaign_mode": "formal",
            "target_version": CLAUDE_VERSION, "registration_operation_id": "register:c1", "admission_head_sha256": "a" * 64,
            "project_ledger": str(claude_root), "deadline_at_utc": None, "created_at_utc": _iso(datetime.now(timezone.utc)),
        })
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "client 非法"):
            ledger._campaign_plan(ledger_dir)

    def test_project_ledger_does_not_import_orchestrator(self) -> None:
        """预算控制经 adapter.orchestrator 字面访问 Campaign 锁与执行租约，项目总账不再导入编排器。"""

        tree = ast.parse((TOOL_ROOT / "codex_upgrade_project_ledger.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [alias.name for alias in node.names if alias.name.rsplit(".", 1)[-1] == "codex_upgrade"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").rsplit(".", 1)[-1] == "codex_upgrade":
                    offenders.append(node.module or "")
                offenders += [alias.name for alias in node.names if alias.name == "codex_upgrade"]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
