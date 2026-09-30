"""UM-9 第 3 项：revision-advance 编排测试。

重型阶段（source-seal、surface-scan、surface-seal、impact-generate/suggest/seal）用替身按真实文件布局
写出 revision 制品，被测的是编排本身：续用 Inventory 与 SurfaceDecision、同 diff 复用 ChangeDecision、
停在人工输入并写草稿、``--resume`` 续跑不重复编号，以及各类前置拒绝。真实阶段函数由 v0.2.10
Plan 003/004 数据回放验收。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.upstream_merge import __main__ as cli
from tools.upstream_merge import revision_advance as ra
from tools.upstream_merge.canonical import (
    artifact_binding,
    bind_identity,
    file_binding,
    sha256_file,
    validate_identity,
    write_json_once,
)
from tools.upstream_merge.contracts import latest_revision, next_inventory_path, next_stage_path
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.plan_inputs import AWAITING_MANUAL_INPUT
from tools.upstream_merge.tests.test_workflow import SyntheticRepository
from tools.upstream_merge.workflow import (
    CHANGE_DECISION_INPUT_SCHEMA,
    CHANGE_DECISION_RECEIPT_SCHEMA,
    IMPACT_MATRIX_SCHEMA,
    SOURCE_CANDIDATE_SCHEMA,
    SOURCE_CHANGE_INPUT_SCHEMA,
    SURFACE_DECISION_SCHEMA,
    SURFACE_DELTA_SCHEMA,
    SURFACE_RECEIPT_SCHEMA,
)

KNOWN = {"status": "known", "component_ids": ["service"], "risk_levels": ["high"]}
LOW = {"status": "known", "component_ids": ["repository_support"], "risk_levels": ["low"]}
DELTA_1 = "1" * 64
DELTA_2 = "2" * 64
CLIENTS = ("claude", "codex")
KINDS = ("ingress", "egress")


class FakeStages:
    """按真实 revision 布局写出阶段制品的替身，记录调用顺序。"""

    def __init__(self, plan: Any, inputs: Path) -> None:
        self.plan = plan
        self.inputs = inputs
        self.calls: list[str] = []
        self.next_deltas = [DELTA_1]
        self.next_files: list[dict[str, Any]] = []

    def doc(self, schema: str, revision: int, payload: dict[str, Any]) -> dict[str, Any]:
        return bind_identity(
            {
                "schema_version": schema,
                "plan_id": self.plan.plan_id,
                "plan_identity_sha256": self.plan.identity,
                "revision": revision,
                **payload,
            }
        )

    def write(self, key: str, schema: str, payload: dict[str, Any]) -> Path:
        revision, path = next_stage_path(self.plan, key)
        write_json_once(path, self.doc(schema, revision, payload))
        return path

    # 替身阶段函数
    def seal_source_candidate(self, plan: Any, source_input: Path) -> dict[str, Any]:
        self.calls.append("source-seal")
        document = json.loads(source_input.read_text(encoding="utf-8"))
        validate_identity(document, "SourceChangeInput")
        self.write("source_candidate", SOURCE_CANDIDATE_SCHEMA, {"source_change_input": file_binding(source_input)})
        return {}

    def scan_surfaces(self, plan: Any) -> dict[str, Any]:
        self.calls.append("surface-scan")
        self.write("surface_delta", SURFACE_DELTA_SCHEMA, {"deltas": [{"delta_id": item} for item in self.next_deltas]})
        return {}

    def seal_surfaces(self, plan: Any, decision_path: Path | None) -> dict[str, Any]:
        self.calls.append("surface-seal")
        revision = latest_revision(plan, "source_candidate")
        inventories = {
            client: {kind: artifact_binding(plan.evidence_root, plan.inventory_output(client, kind)) for kind in KINDS}
            for client in CLIENTS
        }
        for client in CLIENTS:
            for kind in KINDS:
                assert ra.inventory_revision_number(plan, client, kind, plan.inventory_output(client, kind)) == revision
        self.write(
            "surface_receipt",
            SURFACE_RECEIPT_SCHEMA,
            {
                "candidate_inventories": inventories,
                "surface_decision": file_binding(decision_path) if decision_path else None,
            },
        )
        return {}

    def generate_impact_matrix(self, plan: Any) -> dict[str, Any]:
        self.calls.append("impact-generate")
        _, delta = ra._revision_document(
            plan, "surface_delta", latest_revision(plan, "surface_delta"), SURFACE_DELTA_SCHEMA, "SurfaceDelta"
        )
        self.write(
            "impact_matrix",
            IMPACT_MATRIX_SCHEMA,
            {"file_changes": self.next_files, "surface_deltas": delta["deltas"]},
        )
        return {}

    def generate_change_decision_suggestion(self, plan: Any) -> dict[str, Any]:
        self.calls.append("impact-suggest")
        revision = latest_revision(plan, "impact_matrix")
        matrix_path, matrix = ra._revision_document(plan, "impact_matrix", revision, IMPACT_MATRIX_SCHEMA, "ImpactMatrix")
        files = []
        for entry in matrix["file_changes"]:
            auto = entry["component_ownership"] is LOW or entry["component_ownership"] == LOW
            files.append(
                {
                    "path": entry["path"],
                    "categories": ["repository_support"] if auto else ["shared_control"],
                    "rationale": "自动分类：低风险仓库支撑文件" if auto else "待人工审查：必须人工确认",
                    "required_actions": ["运行公共终态门禁"],
                    "official_client_identity_changed": False,
                    "evidence_semantics_changed": False,
                    "decision_source": "auto" if auto else "manual_required",
                    "component_ownership": entry["component_ownership"],
                    "auto_reason": "低风险" if auto else "必须人工",
                }
            )
        deltas = [
            {"delta_id": item["delta_id"], "rationale": "待人工确认", "required_actions": ["人工确认"], "decision_source": "manual_required"}
            for item in matrix["surface_deltas"]
        ]
        return self.doc(
            CHANGE_DECISION_INPUT_SCHEMA,
            revision,
            {
                "source_tree": "t" * 40,
                "impact_matrix_sha256": sha256_file(matrix_path),
                "files": files,
                "surface_deltas": deltas,
                "auto_accepted_count": 0,
                "manual_required_count": 0,
                "unresolved_paths": [],
                "result": "ready_for_review",
            },
        )

    def seal_change_decision(self, plan: Any, decision_path: Path) -> dict[str, Any]:
        self.calls.append("impact-seal")
        document = json.loads(decision_path.read_text(encoding="utf-8"))
        validate_identity(document, "ChangeDecision")
        assert document["manual_required_count"] == 0 and document["result"] == "ready_to_seal"
        assert all(item["decision_source"] != "manual_required" for item in document["files"])
        self.write("impact_receipt", CHANGE_DECISION_RECEIPT_SCHEMA, {"change_decision": file_binding(decision_path)})
        return {}

    def preflight_revisions(self, plan: Any, transitions: Any = None) -> dict[str, Any]:
        self.calls.append("revision-preflight")
        return {"result": "ready"}


class RevisionAdvanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name).resolve()
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        self.plan = self.fixture.plan
        self.plan.document["official_clients"] = {
            client: {"persona": {"official_product": client}, "target_version": "1.0.0"} for client in CLIENTS
        }
        self.inputs = self.temp_root / "inputs"
        self.fake = FakeStages(self.plan, self.inputs)
        self.patches = [
            mock.patch.object(ra, name, getattr(self.fake, name))
            for name in (
                "seal_source_candidate",
                "scan_surfaces",
                "seal_surfaces",
                "generate_impact_matrix",
                "generate_change_decision_suggestion",
                "seal_change_decision",
                "preflight_revisions",
            )
        ] + [
            mock.patch.object(ra, "_load_source_candidate", return_value={"source_tree": "t" * 40, "source_commit": "c" * 40}),
            mock.patch.object(ra, "_load_merge_candidate", return_value={"merge_commit": "m" * 40}),
            mock.patch.object(ra, "_worktree_root", return_value=self.temp_root),
            mock.patch.object(ra, "status_paths", return_value=["docs/new-receipt.json"]),
            mock.patch.object(ra, "_validate_inventory_payload", return_value={}),
        ]
        for patcher in self.patches:
            patcher.start()
        self.seed_revision_one()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.fixture.cleanup_worktree()
        self.temporary.cleanup()

    def seed_revision_one(self) -> None:
        """模拟已封存到 U-3 的 r1：a.go 人工决定、b.md 自动、一个发送面差异已处置。"""

        plan, fake = self.plan, self.fake
        self.inputs.mkdir(mode=0o700)
        fake.write("source_candidate", SOURCE_CANDIDATE_SCHEMA, {})
        fake.write("surface_delta", SURFACE_DELTA_SCHEMA, {"deltas": [{"delta_id": DELTA_1}]})
        for client in CLIENTS:
            for kind in KINDS:
                write_json_once(plan.inventory_output(client, kind), {"inventory": f"{client}-{kind}-r1"})
        decision = self.inputs / "surface-decision-revision-001.json"
        write_json_once(
            decision,
            bind_identity(
                {
                    "schema_version": SURFACE_DECISION_SCHEMA,
                    "decisions": [{"delta_id": DELTA_1, "disposition": "out_of_scope", "inventory_entries": {}, "rationale": "r1 发送面人工处置理由"}],
                }
            ),
        )
        fake.seal_surfaces(plan, decision)
        fake.next_files = [
            {"path": "a.go", "diff_sha256": "d-a", "old_path": "", "status": "M", "component_ownership": KNOWN},
            {"path": "b.md", "diff_sha256": "d-b", "old_path": "", "status": "M", "component_ownership": LOW},
        ]
        fake.generate_impact_matrix(plan)
        suggestion = fake.generate_change_decision_suggestion(plan)
        files = suggestion["files"]
        files[0] = {**files[0], "decision_source": "manual", "rationale": "r1 人工理由：a.go 只改本站调度，不触及官方出站"}
        deltas = [{**suggestion["surface_deltas"][0], "decision_source": "manual", "rationale": "r1 发送面差异人工确认理由"}]
        change = self.inputs / "change-decision-revision-001.json"
        draft = {key: value for key, value in suggestion.items() if key != "identity_sha256"}
        draft.update({"files": files, "surface_deltas": deltas})
        write_json_once(change, bind_identity(ra._recount(draft)))
        fake.seal_change_decision(plan, change)
        fake.calls.clear()

    def source_changes(self, entries: list[dict[str, str]] | None = None) -> Path:
        self.draft_count = getattr(self, "draft_count", 0) + 1
        path = self.temp_root / f"source-changes-draft-{self.draft_count}.json"
        write_json_once(
            path,
            {"entries": entries or [{"path": "docs/new-receipt.json", "reason": "台账 revision：登记本轮冻结承接收据"}]},
        )
        return path

    def test_advance_stops_for_new_file_then_resumes(self) -> None:
        self.fake.next_files = [
            {"path": "a.go", "diff_sha256": "d-a", "old_path": "", "status": "M", "component_ownership": KNOWN},
            {"path": "b.md", "diff_sha256": "d-b", "old_path": "", "status": "M", "component_ownership": LOW},
            {"path": "docs/new-receipt.json", "diff_sha256": "d-n", "old_path": "", "status": "A", "component_ownership": KNOWN},
        ]
        waiting = ra.advance_revision(self.plan, self.source_changes(), resume=False)
        self.assertEqual(waiting["result"], AWAITING_MANUAL_INPUT)
        self.assertEqual(waiting["stage"], "U-3 ChangeDecision")
        self.assertEqual([item.get("path") for item in waiting["pending"]], ["docs/new-receipt.json"])
        self.assertIn("本轮新增", waiting["pending"][0]["reason"])
        self.assertEqual(
            self.fake.calls,
            ["source-seal", "surface-scan", "surface-seal", "impact-generate", "impact-suggest"],
        )
        # 机械字段由工具补齐并签名。
        source_input = json.loads((self.inputs / "source-change-revision-002.json").read_text(encoding="utf-8"))
        validate_identity(source_input, "SourceChangeInput")
        self.assertEqual(
            (source_input["schema_version"], source_input["revision"], source_input["merge_commit"], source_input["base_source_commit"]),
            (SOURCE_CHANGE_INPUT_SCHEMA, 2, "m" * 40, "c" * 40),
        )
        # 发送面与上一轮相同：四份 Inventory 原样续用，SurfaceDecision 沿用上一轮逐条处置。
        for client in CLIENTS:
            for kind in KINDS:
                carried = self.plan.inventory_output(client, kind)
                self.assertTrue(carried.name.endswith("-002.json"))
                self.assertEqual(json.loads(carried.read_text(encoding="utf-8")), {"inventory": f"{client}-{kind}-r1"})
        decision = json.loads((self.inputs / "surface-decision-revision-002.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["decisions"][0]["rationale"], "r1 发送面人工处置理由")
        delta_path = self.plan.evidence_root / "u2/surface-revisions/surface-delta-002.json"
        self.assertEqual(decision["surface_delta_sha256"], sha256_file(delta_path))

        with self.assertRaisesRegex(UpstreamMergeError, "--resume"):
            ra.advance_revision(self.plan, self.source_changes(), resume=False)

        draft_path = Path(waiting["draft"])
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        for item in draft["files"]:
            if item["path"] == "docs/new-receipt.json":
                item["decision_source"] = "manual"
                item["rationale"] = "台账 revision 新生成的冻结承接收据，只登记摘要，不改变运行时"
        draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        self.fake.calls.clear()
        done = ra.advance_revision(self.plan, None, resume=True)
        self.assertEqual(done["result"], "advanced")
        self.assertEqual(done["revision"], 2)
        self.assertEqual(self.fake.calls, ["impact-seal", "revision-preflight"])
        signed = json.loads((self.inputs / "change-decision-revision-002.json").read_text(encoding="utf-8"))
        validate_identity(signed, "ChangeDecision")
        by_path = {item["path"]: item for item in signed["files"]}
        self.assertEqual(by_path["a.go"]["rationale"], "r1 人工理由：a.go 只改本站调度，不触及官方出站")
        self.assertEqual(by_path["b.md"]["decision_source"], "auto")
        self.assertEqual((signed["auto_accepted_count"], signed["manual_required_count"], signed["result"]), (1, 0, "ready_to_seal"))
        self.assertEqual(signed["surface_deltas"][0]["rationale"], "r1 发送面差异人工确认理由")
        with self.assertRaisesRegex(UpstreamMergeError, "没有需要续跑"):
            ra.advance_revision(self.plan, None, resume=True)

    def test_same_diff_is_reused_without_stopping(self) -> None:
        self.fake.next_files = [
            {"path": "a.go", "diff_sha256": "d-a", "old_path": "", "status": "M", "component_ownership": KNOWN},
            {"path": "b.md", "diff_sha256": "d-b2", "old_path": "", "status": "M", "component_ownership": LOW},
        ]
        done = ra.advance_revision(self.plan, self.source_changes(), resume=False)
        self.assertEqual(done["result"], "advanced")
        self.assertEqual(done["reuse"]["change_decision"], {"auto": 1, "reused": 2, "pending": 0})
        self.assertEqual(latest_revision(self.plan, "impact_receipt"), 2)

    def test_changed_diff_and_surface_change_stop(self) -> None:
        self.fake.next_deltas = [DELTA_2]
        waiting = ra.advance_revision(self.plan, self.source_changes(), resume=False)
        self.assertEqual(waiting["stage"], "U-2 Inventory")
        self.assertEqual(waiting["pending"][0]["added_delta_ids"], [DELTA_2])
        self.assertEqual(waiting["pending"][0]["removed_delta_ids"], [DELTA_1])
        self.assertEqual(latest_revision(self.plan, "surface_receipt"), 1)
        # 人工按 U-2 专用流程写出本轮 Inventory 与 SurfaceDecision 后续跑；a.go 的 diff 也变了，停在 U-3。
        for client in CLIENTS:
            for kind in KINDS:
                write_json_once(next_inventory_path(self.plan, client, kind, revision=2), {"inventory": "human"})
        delta_path = self.plan.evidence_root / "u2/surface-revisions/surface-delta-002.json"
        write_json_once(
            self.inputs / "surface-decision-revision-002.json",
            bind_identity({"schema_version": SURFACE_DECISION_SCHEMA, "surface_delta_sha256": sha256_file(delta_path), "decisions": []}),
        )
        self.fake.next_files = [
            {"path": "a.go", "diff_sha256": "d-a2", "old_path": "", "status": "M", "component_ownership": KNOWN},
        ]
        waiting = ra.advance_revision(self.plan, None, resume=True)
        self.assertEqual(waiting["stage"], "U-3 ChangeDecision")
        reasons = {item.get("path") or item.get("delta_id"): item["reason"] for item in waiting["pending"]}
        self.assertEqual(reasons["a.go"], "diff 与上一轮不同")
        self.assertIn(DELTA_2, reasons)

    def test_interrupted_inventory_carry_is_completed_on_resume(self) -> None:
        # 续用 Inventory 写到第 3 份时中断：--resume 识别已续用的 3 份（与上一轮逐字节相同），只补第 4 份。
        self.fake.next_files = [
            {"path": "a.go", "diff_sha256": "d-a", "old_path": "", "status": "M", "component_ownership": KNOWN},
        ]
        calls = {"count": 0}

        def flaky(*args: object, **kwargs: object) -> dict:
            calls["count"] += 1
            if calls["count"] == 3:
                raise UpstreamMergeError("模拟校验中断")
            return {}

        with mock.patch.object(ra, "_validate_inventory_payload", side_effect=flaky):
            with self.assertRaisesRegex(UpstreamMergeError, "模拟校验中断"):
                ra.advance_revision(self.plan, self.source_changes(), resume=False)
        carried = [
            client_kind
            for client_kind in ((client, kind) for client in CLIENTS for kind in KINDS)
            if self.plan.inventory_output(*client_kind).name.endswith("-002.json")
        ]
        self.assertEqual(len(carried), 3)
        done = ra.advance_revision(self.plan, None, resume=True)
        self.assertEqual(done["result"], "advanced")
        self.assertEqual(len(done["reuse"]["inventories"]), 1)
        self.assertEqual(latest_revision(self.plan, "impact_receipt"), 2)

    def test_human_override_of_auto_is_kept(self) -> None:
        previous_matrix = {"file_changes": [{"path": "b.md", "diff_sha256": "d", "old_path": "", "status": "M", "component_ownership": LOW}]}
        previous_decision = {
            "files": [{"path": "b.md", "categories": ["shared_control"], "rationale": "人工改判：虽可自动但涉及共享控制面", "required_actions": ["x"], "official_client_identity_changed": False, "evidence_semantics_changed": False, "decision_source": "manual", "component_ownership": LOW}],
            "surface_deltas": [],
        }
        suggestion = {
            "files": [{"path": "b.md", "categories": ["repository_support"], "rationale": "自动", "required_actions": ["y"], "official_client_identity_changed": False, "evidence_semantics_changed": False, "decision_source": "auto", "component_ownership": LOW}],
            "surface_deltas": [],
        }
        draft, pending, counts = ra._fill_change_decision(suggestion, previous_matrix, previous_matrix, previous_decision)
        self.assertEqual(pending, [])
        self.assertEqual(draft["files"][0]["rationale"], "人工改判：虽可自动但涉及共享控制面")
        self.assertEqual(counts, {"auto": 0, "reused": 1, "pending": 0})
        self.assertEqual(draft["auto_accepted_count"], 0)

    def test_preconditions_and_source_change_draft_checks(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "需要 --source-changes"):
            ra.advance_revision(self.plan, None, resume=False)
        with self.assertRaisesRegex(UpstreamMergeError, "已封存完毕"):
            ra.advance_revision(self.plan, None, resume=True)
        with self.assertRaisesRegex(UpstreamMergeError, "未闭合 worktree 当前变化"):
            ra.advance_revision(self.plan, self.source_changes([{"path": "other.txt", "reason": "与本轮无关的另一个路径的修复理由说明"}]), resume=False)
        wrong = self.temp_root / "wrong-plan.json"
        write_json_once(wrong, {"plan_id": "other", "entries": [{"path": "docs/new-receipt.json", "reason": "台账 revision：登记收据"}]})
        with self.assertRaisesRegex(UpstreamMergeError, "plan_id 与本 Plan"):
            ra.advance_revision(self.plan, wrong, resume=False)
        short = self.temp_root / "short.json"
        write_json_once(short, {"entries": [{"path": "docs/new-receipt.json", "reason": "短"}]})
        with self.assertRaisesRegex(UpstreamMergeError, "reason 必须说明"):
            ra.advance_revision(self.plan, short, resume=False)
        self.assertEqual(latest_revision(self.plan, "source_candidate"), 1)
        self.assertEqual(self.fake.calls, [])


class AwaitingExitCodeTest(unittest.TestCase):
    def test_awaiting_result_is_printed_with_exit_code_four(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory).resolve() / "timing-ledger.jsonl"
            stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with mock.patch.object(cli, "execute", return_value={"result": AWAITING_MANUAL_INPUT, "pending": ["x"]}), mock.patch(
                "sys.stdout", stdout
            ):
                code = cli.main(["revision-advance", "--plan", "/nonexistent/plan.json", "--resume", "--timing-ledger", str(ledger)])
            stdout.flush()
            printed = json.loads(stdout.buffer.getvalue().decode("utf-8"))
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(code, 4)
        self.assertEqual(printed["result"], AWAITING_MANUAL_INPUT)
        self.assertEqual((record["status"], record["exit_code"]), ("awaiting_input", 4))


if __name__ == "__main__":
    unittest.main()
