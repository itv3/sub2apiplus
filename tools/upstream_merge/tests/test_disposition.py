"""UM-9 第 4 项：U-5 收据派生与校验、Campaign 格式校验、disposition-draft 草稿测试。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import disposition_draft as dd
from tools.upstream_merge.canonical import bind_identity, load_json, validate_identity
from tools.upstream_merge.disposition_receipts import (
    SHARED_CONTRACT_FAIL_CLOSE,
    SHARED_CONTRACT_SUCCESSOR_SCHEMA,
    build_original_business_receipt,
    persona_baseline,
    shared_contract_verification,
    validate_campaign,
    validate_original_business_receipt,
    validate_shared_contract_receipt,
)
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.plan_inputs import AWAITING_MANUAL_INPUT
from tools.upstream_merge.tests.test_workflow import SyntheticRepository

SOURCE_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_APPROVAL = SOURCE_ROOT / "backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-official-client-only-approval.json"
CODEX_ACTIVATION = SOURCE_ROOT / "docs/egress/maintenance/CODEX_CLI_0154_TO_0157_PRODUCTION_ACTIVATION_RECEIPT.json"
CODEX_LEGACY = SOURCE_ROOT / "docs/egress/maintenance/CODEX_CLI_0145_TO_0147_PRODUCTION_ACTIVATION_RECEIPT.json"
CLAUDE_FW_G = SOURCE_ROOT / "docs/egress/maintenance/claude-fw-g-acceptance.json"
OFFICIAL_CLIENTS = {
    "claude": {
        "persona": {"auth_family": "oauth", "official_product": "claude-code", "provider": "anthropic", "upstream_route_family": "anthropic-api"},
        "target_version": "2.1.226",
    },
    "codex": {
        "persona": {"auth_family": "oauth", "official_product": "codex-cli", "provider": "openai", "upstream_route_family": "chatgpt-backend-api"},
        "target_version": "0.157.0",
    },
}
VERIFICATION = {
    "attempt_id": "attempt-003",
    "identity_sha256": "7" * 64,
    "result": "passed",
    "executed_gate_count": 0,
    "skipped_gate_count": 0,
    "gates": [
        {"id": "01-claude-active-wire", "status": "passed", "category": "claude_active_wire"},
        {"id": "09-original-business", "status": "passed", "category": "original_business"},
    ],
}
SOURCE = {"source_tree": "a" * 40, "source_commit": "b" * 40}
SOURCE_BINDING = {"path": "u2/source-candidates/source-candidate-004.json", "sha256": "c" * 64, "bytes": 10}
SHARED = {"backend/internal/repository/http_upstream.go": ("d" * 64, 123), "deploy/config.example.yaml": ("e" * 64, 45)}


def shared_contract(**overrides: object) -> dict:
    document = {
        "schema_version": SHARED_CONTRACT_SUCCESSOR_SCHEMA,
        "plan_id": "plan",
        "plan_identity_sha256": "f" * 64,
        "source_tree": SOURCE["source_tree"],
        "source_commit": SOURCE["source_commit"],
        "scope": "sub2api-v0.2.10-upstream-merge",
        "purpose": "逐项复核上游触及共享控制面的路径，没有修改共享合同要素",
        "affected_paths": [
            {"path": path, "sha256": sha, "bytes": size, "change_kind": "data_access", "assessment": "只读写本站数据库，不涉及出站共享合同要素"}
            for path, (sha, size) in sorted(SHARED.items())
        ],
        "fail_close_behavior": SHARED_CONTRACT_FAIL_CLOSE,
        "verification": shared_contract_verification(VERIFICATION),
        "persona_baseline_unchanged": persona_baseline(OFFICIAL_CLIENTS, 0),
        "result": "closed",
    }
    document.update(overrides)
    return bind_identity(document)


def validate_shared(document: dict) -> None:
    validate_shared_contract_receipt(
        document,
        plan_id="plan",
        plan_identity="f" * 64,
        source=SOURCE,
        shared_paths=SHARED,
        verification=VERIFICATION,
        official_clients=OFFICIAL_CLIENTS,
        identity_change_count=0,
    )


class ReceiptValidationTests(unittest.TestCase):
    def test_original_business_receipt_round_trip_and_tamper(self) -> None:
        receipt = bind_identity(
            build_original_business_receipt(plan_id="plan", plan_identity="f" * 64, verification=VERIFICATION, source_binding=SOURCE_BINDING)
        )
        self.assertEqual(receipt["gate"]["id"], "09-original-business")
        kwargs = {"plan_id": "plan", "plan_identity": "f" * 64, "verification": VERIFICATION, "source_binding": SOURCE_BINDING}
        validate_original_business_receipt(receipt, **kwargs)
        stale = bind_identity({**{k: v for k, v in receipt.items() if k != "identity_sha256"}, "source_candidate": {**SOURCE_BINDING, "path": "u2/source-candidates/source-candidate-002.json"}})
        with self.assertRaisesRegex(UpstreamMergeError, "source_candidate"):
            validate_original_business_receipt(stale, **kwargs)
        other_attempt = {**VERIFICATION, "identity_sha256": "8" * 64}
        with self.assertRaisesRegex(UpstreamMergeError, "verification_receipt_identity_sha256"):
            validate_original_business_receipt(receipt, **{**kwargs, "verification": other_attempt})
        failed = {**VERIFICATION, "gates": [{"id": "09-original-business", "status": "failed", "category": "original_business"}]}
        with self.assertRaisesRegex(UpstreamMergeError, "original_business"):
            build_original_business_receipt(plan_id="plan", plan_identity="f" * 64, verification=failed, source_binding=SOURCE_BINDING)

    def test_shared_contract_requires_closure_facts_and_assessments(self) -> None:
        validate_shared(shared_contract())
        missing = shared_contract(affected_paths=shared_contract()["affected_paths"][:1])
        with self.assertRaisesRegex(UpstreamMergeError, "未闭合"):
            validate_shared(missing)
        drifted_paths = copy.deepcopy(shared_contract()["affected_paths"])
        drifted_paths[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(UpstreamMergeError, "摘要与最新源码候选不一致"):
            validate_shared(shared_contract(affected_paths=drifted_paths))
        blank = copy.deepcopy(shared_contract()["affected_paths"])
        blank[1]["assessment"] = ""
        with self.assertRaisesRegex(UpstreamMergeError, "assessment"):
            validate_shared(shared_contract(affected_paths=blank))
        with self.assertRaisesRegex(UpstreamMergeError, "verification"):
            validate_shared(shared_contract(verification={**shared_contract_verification(VERIFICATION), "attempt_id": "attempt-001"}))
        with self.assertRaisesRegex(UpstreamMergeError, "purpose"):
            validate_shared(shared_contract(purpose=""))

    def test_campaign_formats_persona_and_mode(self) -> None:
        claude = load_json(CLAUDE_APPROVAL, "claude")
        codex = load_json(CODEX_ACTIVATION, "codex")
        facts = validate_campaign(claude, client="claude", mode="new_candidate", persona=OFFICIAL_CLIENTS["claude"]["persona"], target_version="2.1.226")
        self.assertEqual(facts["version"], "2.1.226")
        validate_campaign(codex, client="codex", mode="new_candidate", persona=OFFICIAL_CLIENTS["codex"]["persona"], target_version="0.157.0")
        # 历史格式仍可识别；new_candidate 必须正是计划冻结的 Active 版本，后继 Campaign 不要求版本相同。
        legacy = load_json(CODEX_LEGACY, "codex legacy")
        with self.assertRaisesRegex(UpstreamMergeError, "0.157.0"):
            validate_campaign(legacy, client="codex", mode="new_candidate", persona=OFFICIAL_CLIENTS["codex"]["persona"], target_version="0.157.0")
        validate_campaign(legacy, client="codex", mode="successor_campaign", persona=OFFICIAL_CLIENTS["codex"]["persona"], target_version="0.157.0")
        validate_campaign(load_json(CLAUDE_FW_G, "fw-g"), client="claude", mode="new_candidate", persona=OFFICIAL_CLIENTS["claude"]["persona"], target_version="2.1.226")
        with self.assertRaisesRegex(UpstreamMergeError, "已登记"):
            validate_campaign(codex, client="claude", mode="new_candidate", persona=OFFICIAL_CLIENTS["claude"]["persona"], target_version="2.1.226")
        with self.assertRaisesRegex(UpstreamMergeError, "产品"):
            validate_campaign(claude, client="claude", mode="new_candidate", persona={"official_product": "other"}, target_version="2.1.226")
        revoked = {**claude, "status": "revoked"}
        with self.assertRaisesRegex(UpstreamMergeError, "approved"):
            validate_campaign(revoked, client="claude", mode="new_candidate", persona=OFFICIAL_CLIENTS["claude"]["persona"], target_version="2.1.226")


class DispositionDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name).resolve()
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        self.plan = self.fixture.plan
        self.plan.document["official_clients"] = OFFICIAL_CLIENTS
        attempt = self.plan.evidence_root / "u4/attempts/attempt-003"
        (attempt / "client-receipts").mkdir(parents=True)
        for client in ("claude", "codex"):
            (attempt / f"client-receipts/{client}_active_wire.json").write_text("{}\n", encoding="utf-8")
        source_path = self.plan.evidence_root / "u2/source-candidate.json"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("{}\n", encoding="utf-8")
        self.impact = {
            "client_impacts": {"claude": True, "codex": True},
            "successor_campaign_required": {"claude": False, "codex": False},
            "shared_contract_required": True,
            "official_client_identity_change_count": 0,
        }
        self.patches = [
            mock.patch.object(dd, "load_verification_receipt", return_value=VERIFICATION),
            mock.patch.object(dd, "_load_source_candidate", return_value=SOURCE),
            mock.patch.object(dd, "_load_impact_receipt", side_effect=lambda plan: self.impact),
            mock.patch.object(dd, "shared_control_facts", return_value=SHARED),
        ]
        for patcher in self.patches:
            patcher.start()
        self.campaigns = {"claude": CLAUDE_APPROVAL, "codex": CODEX_ACTIVATION}

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.fixture.cleanup_worktree()
        self.temporary.cleanup()

    def test_dry_run_derives_three_documents_without_writing(self) -> None:
        result = dd.draft_candidate_disposition(self.plan, "attempt-003", self.campaigns, dry_run=True)
        self.assertEqual(result["result"], "dry_run")
        validate_identity(result["original_business_receipt"], "原业务回归收据")
        draft = result["shared_contract_draft"]
        self.assertEqual([item["path"] for item in draft["affected_paths"]], sorted(SHARED))
        self.assertTrue(all(item["assessment"] == "" for item in draft["affected_paths"]))
        self.assertEqual(draft["scope"], "sub2api-v1.0.1-upstream-merge")
        clients = result["disposition_input"]["clients"]
        self.assertEqual(clients["claude"]["mode"], "new_candidate")
        self.assertTrue(clients["codex"]["approval_path"].endswith("attempt-003/client-receipts/codex_active_wire.json"))
        self.assertFalse((self.temp_root / "inputs").exists())
        self.assertFalse((self.plan.evidence_root / "u5").exists())

    def test_draft_writes_inputs_and_waits_for_shared_contract(self) -> None:
        result = dd.draft_candidate_disposition(self.plan, "attempt-003", self.campaigns)
        self.assertEqual(result["result"], AWAITING_MANUAL_INPUT)
        original = json.loads(Path(result["original_business_receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(original["verification_receipt_identity_sha256"], VERIFICATION["identity_sha256"])
        self.assertTrue(Path(result["shared_contract_draft"]).is_file())
        disposition = json.loads(Path(result["disposition_input"]).read_text(encoding="utf-8"))
        validate_identity(disposition, "CandidateDispositionInput")
        self.assertTrue(disposition["shared_contract_receipt_path"].endswith("u5/shared-contract-successor.json"))
        # 同内容重跑可复用，已有草稿不被覆盖。
        again = dd.draft_candidate_disposition(self.plan, "attempt-003", self.campaigns)
        self.assertEqual(again["result"], AWAITING_MANUAL_INPUT)

    def test_mode_and_campaign_are_enforced(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "--campaign codex"):
            dd.draft_candidate_disposition(self.plan, "attempt-003", {"claude": CLAUDE_APPROVAL}, dry_run=True)
        with self.assertRaisesRegex(UpstreamMergeError, "0.157.0"):
            dd.draft_candidate_disposition(self.plan, "attempt-003", {"claude": CLAUDE_APPROVAL, "codex": CODEX_LEGACY}, dry_run=True)
        self.impact = {**self.impact, "client_impacts": {"claude": True, "codex": False}}
        with self.assertRaisesRegex(UpstreamMergeError, "不得指定 --campaign"):
            dd.draft_candidate_disposition(self.plan, "attempt-003", self.campaigns, dry_run=True)
        self.impact = {**self.impact, "shared_contract_required": False}
        result = dd.draft_candidate_disposition(self.plan, "attempt-003", {"claude": CLAUDE_APPROVAL})
        self.assertEqual(result["result"], "drafted")
        disposition = json.loads(Path(result["disposition_input"]).read_text(encoding="utf-8"))
        self.assertIsNone(disposition["shared_contract_receipt_path"])
        self.assertEqual(disposition["clients"]["codex"], {"mode": "none", "campaign_path": None, "candidate_path": None, "approval_path": None, "acceptance_path": None})

    def test_parse_campaigns(self) -> None:
        parsed = dd.parse_campaigns([f"claude={CLAUDE_APPROVAL}"])
        self.assertEqual(parsed, {"claude": CLAUDE_APPROVAL})
        for bad in (["other=/x"], ["claude=relative.json"], [f"claude={CLAUDE_APPROVAL}", f"claude={CLAUDE_APPROVAL}"]):
            with self.assertRaises(UpstreamMergeError):
                dd.parse_campaigns(bad)


if __name__ == "__main__":
    unittest.main()
