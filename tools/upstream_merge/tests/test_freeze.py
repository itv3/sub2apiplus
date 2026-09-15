"""冻结台账 successor 生成器的合成仓库测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.canonical import bind_identity, sha256_bytes, validate_identity
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.freeze import (
    FREEZE_REGISTRY_RELATIVE,
    FREEZE_REGISTRY_SCHEMA,
    FREEZE_SUCCESSOR_SCHEMA,
    MAINTENANCE_ROOT,
    gate_compatible_identity,
    generate_freeze_successor,
    load_freeze_registry,
    load_frozen_edges,
    plan_freeze_successor,
)
from tools.upstream_merge.workflow import generate_source_transition

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def run(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"命令失败 {argv!r}: {completed.stderr or completed.stdout}")
    return completed.stdout.strip()


def digest(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def registry_document(rules: list[dict]) -> dict:
    return bind_identity(
        {
            "schema_version": FREEZE_REGISTRY_SCHEMA,
            "issued_at_utc": "2026-09-10T12:00:00Z",
            "scope": "freeze-registry",
            "policy": {"generic_graph": "maintenance/*.json"},
            "rules": rules,
        }
    )


class FreezeSuccessorTest(unittest.TestCase):
    """在临时仓库中构造五种收据形态，验证抽边、命中、断链与自引用防护。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repository"
        self.root.mkdir()
        run(self.root, "git", "init", "-b", "main")
        run(self.root, "git", "config", "user.name", "Freeze Test")
        run(self.root, "git", "config", "user.email", "freeze@example.invalid")
        self.maintenance = self.root / Path(*MAINTENANCE_ROOT.split("/"))
        self.maintenance.mkdir(parents=True)

        # 五个受冻结的源码路径，每个由不同形态的收据登记。
        self.sources = {
            "backend/a.go": "package a\n",
            "backend/b.go": "package b\n",
            "backend/c.go": "package c\n",
            "backend/d.go": "package d\n",
            "tools/e.py": "print('e')\n",
        }
        for relative, content in self.sources.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.root / "backend/free.go").write_text("package free\n", encoding="utf-8")
        history = "0" * 64
        write_json(
            self.maintenance / "shape-list-successor.json",
            {
                "schema_version": "official-egress-example-successor/v1",
                "transitions": [
                    {
                        "path": "backend/a.go",
                        "predecessor_sha256s": [history],
                        "to_sha256": digest(self.sources["backend/a.go"]),
                        "reason": "list 形态",
                    }
                ],
            },
        )
        write_json(
            self.maintenance / "shape-from-to-transition.json",
            {
                "schema_version": "official-egress-example-transition/v1",
                "transitions": [
                    {
                        "path": "backend/b.go",
                        "from_sha256": history,
                        "to_sha256": digest(self.sources["backend/b.go"]),
                        "reason": "from/to 形态",
                    }
                ],
            },
        )
        write_json(
            self.maintenance / "shape-current-ledger.json",
            {
                "schema_version": "official-egress-example-ledger/v1",
                "entries": [
                    {
                        "path": "backend/c.go",
                        "predecessor_sha256": history,
                        "current_sha256": digest(self.sources["backend/c.go"]),
                        "reason": "predecessor/current 形态",
                    }
                ],
            },
        )
        write_json(
            self.maintenance / "shape-head-receipt.json",
            {
                "schema_version": "official-egress-example-receipt/v1",
                "nested": {
                    "deeper": [
                        {
                            "path": "backend/d.go",
                            "predecessor_sha256s": [history],
                            "head_sha256": digest(self.sources["backend/d.go"]),
                            "reason": "head 形态且深层嵌套",
                        }
                    ]
                },
            },
        )
        write_json(
            self.maintenance / "codex-cli-0151-worktree-successor.json",
            {
                "schema_version": "sub2apiplus-codex-cli-0151-worktree-successor/v1",
                "entries": [
                    {
                        "path": "tools/e.py",
                        "before": {"existence": "present", "sha256": history},
                        "after": {"existence": "present", "sha256": digest(self.sources["tools/e.py"])},
                        "reason": "快照形态",
                    }
                ],
            },
        )
        # 不带标记的 schema 与无 reason 的对象都不得产生边。
        write_json(
            self.maintenance / "ignored-plan.json",
            {
                "schema_version": "official-egress-example-plan/v1",
                "transitions": [
                    {"path": "backend/free.go", "from_sha256": history, "to_sha256": digest("x"), "reason": "不应被读取"}
                ],
            },
        )
        write_json(
            self.maintenance / "no-reason-successor.json",
            {
                "schema_version": "official-egress-example-successor/v1",
                "transitions": [{"path": "backend/free.go", "from_sha256": history, "to_sha256": digest("y")}],
            },
        )
        # 尾部带多余 JSON 的文件按 Go Decoder 语义整体忽略。
        (self.maintenance / "trailing-successor.json").write_text(
            json.dumps({"schema_version": "x-successor/v1", "transitions": [{"path": "backend/free.go", "from_sha256": history, "to_sha256": digest("z"), "reason": "尾部多余"}]})
            + "\n{}\n",
            encoding="utf-8",
        )
        write_json(
            self.root / Path(*FREEZE_REGISTRY_RELATIVE.split("/")),
            registry_document(
                [
                    {
                        "id": "worktree-snapshot",
                        "description": "快照收据登记的路径需要更新 Python 显式列表",
                        "match": {"receipt_paths": [f"{MAINTENANCE_ROOT}/codex-cli-0151-worktree-successor.json"]},
                        "action": {"kind": "python_receipt_list", "file": "tools/tests/test_x.py", "instruction": "追加收据路径"},
                        "verification": ["python3 -m unittest tools.tests.test_x"],
                    },
                    {
                        "id": "scanner",
                        "description": "扫描器算法单跳",
                        "match": {"prefixes": ["backend/cmd/scan/"], "exclude_suffixes": ["_test.go"], "exclude_prefixes": ["backend/cmd/scan/testdata/"]},
                        "action": {"kind": "single_hop_file", "file": "docs/x.json", "instruction": "改写 to"},
                        "verification": ["make scan"],
                    },
                ]
            ),
        )
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", "base")
        self.base = run(self.root, "git", "rev-parse", "HEAD")

    def _commit(self, message: str) -> str:
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", message)
        return run(self.root, "git", "rev-parse", "HEAD")

    def test_edges_follow_go_extraction_rules(self) -> None:
        edges, known, receipts = load_frozen_edges(self.root)
        self.assertEqual(sorted(known), sorted(self.sources))
        self.assertNotIn("backend/free.go", known)
        for relative, content in self.sources.items():
            self.assertIn(digest(content), known[relative])
            self.assertIn("0" * 64, known[relative])
        # 快照展开：所有已知摘要都指向快照 after。
        snapshot_edges = [edge for edge in edges if edge.path == "tools/e.py"]
        self.assertEqual({edge.to_sha256 for edge in snapshot_edges}, {digest(self.sources["tools/e.py"])})
        self.assertIn(f"{MAINTENANCE_ROOT}/shape-head-receipt.json", receipts["backend/d.go"])

    def test_added_file_with_only_successor_digest_is_frozen(self) -> None:
        # 上游新增文件在 source-transition 中只有 current_sha256（predecessor 为 null），
        # Go 侧不会为它建边，但下次修改时仍必须从该登记摘要出发承接。
        (self.root / "backend/added.go").write_text("package added\n", encoding="utf-8")
        write_json(
            self.maintenance / "shape-added-transition.json",
            {
                "schema_version": "official-egress-example-transition/v2",
                "entries": [
                    {
                        "path": "backend/added.go",
                        "status": "A",
                        "predecessor_sha256": None,
                        "current_sha256": digest("package added\n"),
                        "reason": "新增文件条目",
                    }
                ],
            },
        )
        base = self._commit("add file with successor-only entry")
        edges, known, receipts = load_frozen_edges(self.root)
        self.assertNotIn("backend/added.go", {edge.path for edge in edges})
        self.assertEqual(known["backend/added.go"], {digest("package added\n")})
        self.assertEqual(receipts["backend/added.go"], {f"{MAINTENANCE_ROOT}/shape-added-transition.json"})
        (self.root / "backend/added.go").write_text("package added // changed\n", encoding="utf-8")
        plan = plan_freeze_successor(self.root, base)
        self.assertEqual([hit["path"] for hit in plan["frozen_hits"]], ["backend/added.go"])
        self.assertEqual(plan["frozen_hits"][0]["predecessor_sha256s"], [digest("package added\n")])

    def test_registry_rejects_missing_or_tampered(self) -> None:
        registry_path = self.root / Path(*FREEZE_REGISTRY_RELATIVE.split("/"))
        document = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(load_freeze_registry(self.root)["schema_version"], FREEZE_REGISTRY_SCHEMA)
        document["rules"][0]["description"] = "篡改"
        registry_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(UpstreamMergeError):
            load_freeze_registry(self.root)
        registry_path.unlink()
        with self.assertRaises(UpstreamMergeError):
            load_freeze_registry(self.root)

    def test_plan_hits_frozen_paths_and_reports_unregistered(self) -> None:
        (self.root / "backend/a.go").write_text("package a // changed\n", encoding="utf-8")
        (self.root / "backend/free.go").write_text("package free // changed\n", encoding="utf-8")
        (self.root / "tools/e.py").write_text("print('changed')\n", encoding="utf-8")
        plan = plan_freeze_successor(self.root, self.base)
        self.assertEqual(plan["mode"], "worktree")
        self.assertEqual([hit["path"] for hit in plan["frozen_hits"]], ["backend/a.go", "tools/e.py"])
        first = plan["frozen_hits"][0]
        self.assertEqual(first["predecessor_sha256s"], [digest(self.sources["backend/a.go"])])
        self.assertEqual(first["to_sha256"], digest("package a // changed\n"))
        self.assertEqual(first["source_receipts"], [f"{MAINTENANCE_ROOT}/shape-list-successor.json"])
        self.assertEqual(plan["unregistered_paths"], ["backend/free.go"])
        self.assertEqual(plan["broken_chain"], [])
        self.assertEqual([action["rule_id"] for action in plan["required_manual_actions"]], ["worktree-snapshot"])
        after = self._commit("change")
        committed = plan_freeze_successor(self.root, self.base, after)
        self.assertEqual(committed["mode"], "commit")
        self.assertEqual(committed["frozen_hits"], plan["frozen_hits"])

    def test_registry_rules_apply_to_unregistered_paths(self) -> None:
        # 受管目录内的新文件不在任何收据边里，但目录级摘要规则仍必须报待办。
        scanner = self.root / "backend/cmd/scan/new_rule.go"
        scanner.parent.mkdir(parents=True)
        scanner.write_text("package scan\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        base = self._commit("add scanner file")
        scanner.write_text("package scan // changed\n", encoding="utf-8")
        plan = plan_freeze_successor(self.root, base)
        self.assertEqual(plan["frozen_hits"], [])
        self.assertEqual(plan["unregistered_paths"], ["backend/cmd/scan/new_rule.go"])
        self.assertEqual([(a["rule_id"], a["path"]) for a in plan["required_manual_actions"]], [("scanner", "backend/cmd/scan/new_rule.go")])
        excluded = self.root / "backend/cmd/scan/testdata/fixture.go"
        excluded.parent.mkdir(parents=True)
        excluded.write_text("package testdata\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        base2 = self._commit("add excluded fixture")
        excluded.write_text("package testdata // changed\n", encoding="utf-8")
        self.assertEqual(plan_freeze_successor(self.root, base2)["required_manual_actions"], [])
        output = self.maintenance / "upstream-t7-freeze-successor.json"
        summary = generate_freeze_successor(self.root, base, None, output, tag="t7")
        self.assertEqual(summary["result"], "manual_actions_required")
        self.assertEqual(summary["transition_count"], 0)

    def test_broken_chain_fails_closed(self) -> None:
        # 先把 a.go 改到一个未登记的摘要并提交，再从该提交出发生成：前序摘要不在登记集合中。
        (self.root / "backend/a.go").write_text("package a // unregistered\n", encoding="utf-8")
        middle = self._commit("unregistered")
        (self.root / "backend/a.go").write_text("package a // again\n", encoding="utf-8")
        plan = plan_freeze_successor(self.root, middle)
        self.assertEqual([item["path"] for item in plan["broken_chain"]], ["backend/a.go"])
        with self.assertRaisesRegex(UpstreamMergeError, "链断裂"):
            generate_freeze_successor(self.root, middle, None, self.maintenance / "out.json", tag="t1")

    def test_generate_writes_once_and_identity_recomputes(self) -> None:
        (self.root / "backend/b.go").write_text("package b // changed\n", encoding="utf-8")
        output = self.maintenance / "upstream-t2-freeze-successor.json"
        summary = generate_freeze_successor(self.root, self.base, None, output, tag="t2")
        self.assertEqual(summary["result"], "passed_local_evidence_successor")
        self.assertEqual(summary["transition_count"], 1)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], FREEZE_SUCCESSOR_SCHEMA)
        self.assertEqual(document["scope"], "upstream-t2-freeze-successor")
        self.assertIsNone(document["current_commit"])
        # 自摘要采用 Python 工作区门禁的算法：紧凑、键排序、无尾换行。
        unsigned = {key: value for key, value in document.items() if key != "identity_sha256"}
        gate_style = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(document["identity_sha256"], gate_style)
        self.assertEqual(gate_compatible_identity(document), gate_style)
        with self.assertRaises(UpstreamMergeError):
            validate_identity(document, "带尾换行的 canonical 算法不应匹配")
        transition = document["transitions"][0]
        self.assertEqual(transition["path"], "backend/b.go")
        self.assertEqual(transition["to_sha256"], digest("package b // changed\n"))
        self.assertIn("path=backend/b.go", transition["reason"])
        # 生成后的收据必须能被同一抽边规则读回，形成承接边。
        edges, known, _ = load_frozen_edges(self.root)
        self.assertIn(digest("package b // changed\n"), known["backend/b.go"])
        with self.assertRaises(UpstreamMergeError):
            generate_freeze_successor(self.root, self.base, None, output, tag="t2")

    def test_dry_run_reports_without_writing(self) -> None:
        (self.root / "backend/c.go").write_text("package c // changed\n", encoding="utf-8")
        report = generate_freeze_successor(self.root, self.base, None, None, tag="t3", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["transitions"]), 1)
        self.assertEqual(list(self.maintenance.glob("upstream-*-freeze-successor.json")), [])
        with self.assertRaises(UpstreamMergeError):
            generate_freeze_successor(self.root, self.base, None, None, tag="t3")

    def test_self_binding_receipt_change_is_rejected(self) -> None:
        # 登记 d.go 的收据在本区间内被改写，则为 d.go 生成 successor 会引用一个变化中的收据。
        (self.root / "backend/d.go").write_text("package d // changed\n", encoding="utf-8")
        receipt = self.maintenance / "shape-head-receipt.json"
        document = json.loads(receipt.read_text(encoding="utf-8"))
        document["note"] = "touched"
        receipt.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "自引用"):
            generate_freeze_successor(self.root, self.base, None, self.maintenance / "out.json", tag="t4")

    def test_extra_worktree_path_is_appended_in_commit_mode(self) -> None:
        (self.root / "backend/a.go").write_text("package a // changed\n", encoding="utf-8")
        after = self._commit("source change")
        # 门禁文件在工作树中定稿但尚未提交：以 before 提交摘要 → 工作树摘要追加。
        (self.root / "tools/e.py").write_text("print('gate references receipt')\n", encoding="utf-8")
        plan = plan_freeze_successor(self.root, self.base, after, extra_worktree_paths=["tools/e.py"])
        self.assertEqual([hit["path"] for hit in plan["frozen_hits"]], ["backend/a.go", "tools/e.py"])
        extra = plan["frozen_hits"][1]
        self.assertEqual(extra["predecessor_sha256s"], [digest(self.sources["tools/e.py"])])
        self.assertEqual(extra["to_sha256"], digest("print('gate references receipt')\n"))
        output = self.maintenance / "upstream-t8-freeze-successor.json"
        summary = generate_freeze_successor(
            self.root, self.base, after, output, tag="t8", extra_worktree_paths=["tools/e.py"]
        )
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["current_commit"], after)
        self.assertEqual(document["extra_worktree_paths"], ["tools/e.py"])
        self.assertEqual(summary["transition_count"], 2)
        with self.assertRaisesRegex(UpstreamMergeError, "只能与 --after"):
            plan_freeze_successor(self.root, self.base, None, extra_worktree_paths=["tools/e.py"])
        with self.assertRaisesRegex(UpstreamMergeError, "不得重复登记"):
            plan_freeze_successor(self.root, self.base, after, extra_worktree_paths=["backend/a.go"])

    def test_output_inside_after_commit_is_rejected(self) -> None:
        (self.root / "backend/a.go").write_text("package a // changed\n", encoding="utf-8")
        output = self.maintenance / "upstream-t5-freeze-successor.json"
        output.write_text("{}\n", encoding="utf-8")
        after = self._commit("change with receipt inside")
        output.unlink()
        with self.assertRaisesRegex(UpstreamMergeError, "after 提交树"):
            generate_freeze_successor(self.root, self.base, after, output, tag="t5")

    def test_source_transition_rejects_receipt_bound_to_its_own_interval(self) -> None:
        (self.root / "backend/a.go").write_text("package a // changed\n", encoding="utf-8")
        middle = self._commit("source change")
        write_json(
            self.maintenance / "upstream-t6-freeze-successor.json",
            {
                "schema_version": FREEZE_SUCCESSOR_SCHEMA,
                "base_commit": self.base,
                "current_commit": middle,
                "transitions": [],
            },
        )
        bound = self._commit("receipt bound to interval")
        output = Path(self.temporary.name) / "transition.json"
        with self.assertRaisesRegex(UpstreamMergeError, "绑定本区间"):
            generate_source_transition(self.root, self.base, bound, output)
        self.assertFalse(output.exists())
        # 区间只到源码提交时不受影响。
        generate_source_transition(self.root, self.base, middle, output)
        self.assertTrue(output.exists())

    def test_deleting_frozen_path_requires_and_records_deletion_proof(self) -> None:
        # B1：删除冻结路径 backend/a.go 没有删除原因即拒绝，不再降级为人工待办。
        (self.root / "backend/a.go").unlink()
        output = self.maintenance / "upstream-t9-freeze-successor.json"
        with self.assertRaisesRegex(UpstreamMergeError, "--deletion-reason"):
            generate_freeze_successor(self.root, self.base, None, output, tag="t9")
        plan = plan_freeze_successor(self.root, self.base, None)
        self.assertEqual(plan["deleted_frozen_paths"], ["backend/a.go"])
        self.assertEqual(
            plan["deleted_paths"],
            [{"path": "backend/a.go", "frozen": True, "last_sha256": digest(self.sources["backend/a.go"])}],
        )
        summary = generate_freeze_successor(
            self.root, self.base, None, output, tag="t9", deletion_reason="退役 a"
        )
        self.assertEqual(summary["result"], "passed_with_deletions")
        self.assertTrue(summary["deletion_proof"])
        self.assertEqual(summary["deleted_path_count"], 1)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["result"], "passed_with_deletions")
        self.assertEqual(document["deleted_frozen_paths"], ["backend/a.go"])
        proof = document["deletion_proof"]
        self.assertEqual(proof["algorithm"], "deletion-proof/v1")
        self.assertEqual(proof["reason"], "退役 a")
        self.assertEqual(proof["historical_readers"], [])
        entry = proof["deleted_paths"][0]
        self.assertEqual(
            (entry["path"], entry["frozen"], entry["last_sha256"]),
            ("backend/a.go", True, digest(self.sources["backend/a.go"])),
        )
        self.assertEqual(entry["reference_scan"]["algorithm"], "reference-scan/v1")
        self.assertEqual(entry["reference_scan"]["patterns"], ["a.go"])
        self.assertEqual(entry["reference_scan"]["references"], [])
        self.assertEqual(gate_compatible_identity(document), document["identity_sha256"])
        # 删除不产生摘要边：a.go 的已知摘要集合保持原样，证明结构也不会被抽成边。
        _edges, known, _ = load_frozen_edges(self.root)
        self.assertEqual(known["backend/a.go"], {"0" * 64, digest(self.sources["backend/a.go"])})

    def test_deleted_python_module_reference_scan_and_historical_reader(self) -> None:
        # B1：删除 Python 模块时，三类残留引用任一命中即拒绝，且必须登记历史读取器。
        (self.root / "tools/e.py").unlink()
        reader = self.root / "tools/f.py"
        reader.write_text("from tools import e\n", encoding="utf-8")
        output = self.maintenance / "upstream-t10-freeze-successor.json"
        with self.assertRaisesRegex(UpstreamMergeError, "仍被引用.*python:tools/f.py"):
            generate_freeze_successor(
                self.root, self.base, None, output, tag="t10",
                deletion_reason="退役 e", historical_readers=["tools/f.py"],
            )
        reader.write_text("print('f')\n", encoding="utf-8")
        action = self.root / "config/action.json"
        action.parent.mkdir()
        action.write_text(json.dumps({"command": ["python3", "tools/e.py"]}), encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "仍被引用.*json:config/action.json"):
            generate_freeze_successor(
                self.root, self.base, None, output, tag="t10",
                deletion_reason="退役 e", historical_readers=["tools/f.py"],
            )
        # 非 command 类字段里的路径文本不算引用。
        action.write_text(json.dumps({"note": "tools/e.py"}), encoding="utf-8")
        script = self.root / "run.sh"
        script.write_text("python3 tools/e.py\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "仍被引用.*shell:run.sh"):
            generate_freeze_successor(
                self.root, self.base, None, output, tag="t10",
                deletion_reason="退役 e", historical_readers=["tools/f.py"],
            )
        script.unlink()
        with self.assertRaisesRegex(UpstreamMergeError, "--historical-reader"):
            generate_freeze_successor(self.root, self.base, None, output, tag="t10", deletion_reason="退役 e")
        with self.assertRaisesRegex(UpstreamMergeError, "历史读取器不存在"):
            generate_freeze_successor(
                self.root, self.base, None, output, tag="t10",
                deletion_reason="退役 e", historical_readers=["tools/missing.py"],
            )
        summary = generate_freeze_successor(
            self.root, self.base, None, output, tag="t10",
            deletion_reason="退役 e", historical_readers=["tools/f.py"],
        )
        # tools/e.py 命中注册表 python_receipt_list 规则：结果仍是人工待办，但证明照样写入。
        self.assertEqual(summary["result"], "manual_actions_required")
        self.assertTrue(summary["deletion_proof"])
        document = json.loads(output.read_text(encoding="utf-8"))
        proof = document["deletion_proof"]
        self.assertEqual(proof["historical_readers"], [{"path": "tools/f.py", "sha256": digest("print('f')\n")}])
        self.assertEqual(proof["deleted_paths"][0]["reference_scan"]["patterns"], ["e", "e.py"])
        self.assertEqual(proof["deleted_paths"][0]["reference_scan"]["references"], [])

    def test_commit_mode_deletion_proof_reads_after_tree(self) -> None:
        (self.root / "backend/a.go").unlink()
        after = self._commit("delete a")
        output = self.maintenance / "upstream-t11-freeze-successor.json"
        summary = generate_freeze_successor(
            self.root, self.base, after, output, tag="t11", deletion_reason="退役 a"
        )
        self.assertEqual(summary["result"], "passed_with_deletions")
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["current_commit"], after)
        self.assertEqual(document["deletion_proof"]["deleted_paths"][0]["frozen"], True)
        # 未登记路径的删除给出原因时也记录证明，但 result 不因此变化。
        (self.root / "backend/free.go").unlink()
        free_after = self._commit("delete free")
        (self.root / "backend/b.go").write_text("package b // changed\n", encoding="utf-8")
        changed = self._commit("change b")
        report = generate_freeze_successor(
            self.root, after, changed, None, tag="t12", dry_run=True, deletion_reason="退役 free"
        )
        self.assertEqual(report["result"], "passed_local_evidence_successor")
        self.assertEqual(report["deleted_frozen_paths"], [])
        # t11 收据随 "delete free" 一起进入了区间，因此未登记路径里还会出现它自身。
        self.assertIn("backend/free.go", report["unregistered_paths"])
        self.assertEqual(report["deletion_proof"]["deleted_paths"][0]["frozen"], False)
        self.assertIsNotNone(free_after)

    def test_real_repository_frozen_coverage_is_consistent(self) -> None:
        edges, known, _ = load_frozen_edges(SOURCE_ROOT)
        # 2026-09-10 基线：成对边覆盖 1078 条路径，加上只有后继摘要的新增文件条目共 1254 条。
        self.assertGreaterEqual(len(known), 1254)
        self.assertGreaterEqual(len(edges), 2641)
        self.assertIn("docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", known)
        self.assertIn("Makefile", known)
        self.assertGreater(len(edges), len(known))
        registry = load_freeze_registry(SOURCE_ROOT)
        # B1 起 Python 门禁按 schema glob freeze successor，python_receipt_list 规则已退役。
        self.assertGreaterEqual(len(registry["rules"]), 3)
        self.assertNotIn("codex-0151-worktree-successor", {rule["id"] for rule in registry["rules"]})


if __name__ == "__main__":
    unittest.main()
