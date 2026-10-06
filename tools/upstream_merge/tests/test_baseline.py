"""基线验收收据的身份、漂移分类和不可变写入测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.baseline import (
    BASELINE_INPUT_SCHEMA,
    seal_baseline_acceptance,
    validate_baseline_acceptance,
)
from tools.upstream_merge.errors import UpstreamMergeError


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
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


class BaselineRepository:
    def __init__(self, root: Path) -> None:
        self.root = root / "repository"
        self.root.mkdir()
        run(self.root, "git", "init", "-b", "main")
        run(self.root, "git", "config", "user.name", "Baseline Test")
        run(self.root, "git", "config", "user.email", "baseline@example.invalid")
        shutil.copytree(SOURCE_ROOT / "tools/upstream_merge", self.root / "tools/upstream_merge")
        (self.root / "tools").mkdir(exist_ok=True)
        for name in (
            "check_ledger_completeness.py",
            "upstream_merge_plan.schema.json",
            "upstream_merge_request.schema.json",
            "upstream_merge_artifacts.schema.json",
        ):
            shutil.copy2(SOURCE_ROOT / "tools" / name, self.root / "tools" / name)
        (self.root / "backend/cmd/egressscan").mkdir(parents=True)
        (self.root / "backend/cmd/egressscan/main.go").write_text(
            "package main\nfunc main() {}\n", encoding="utf-8"
        )
        (self.root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
        (self.root / "README.md").write_text("baseline\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", "baseline")


def draft(*, known_drift: bool = False) -> dict[str, object]:
    return {
        "schema_version": BASELINE_INPUT_SCHEMA,
        "functional_checks": [
            {
                "id": "go-build",
                "status": "passed",
                "command": "go build ./...",
                "observed_at_utc": "2026-09-09T00:00:00Z",
                "failure_ids": [],
            }
        ],
        "evidence_checks": [
            {
                "id": "historical-evidence",
                "status": "known_drift" if known_drift else "passed",
                "command": "go test ./internal/service -count=1",
                "observed_at_utc": "2026-09-09T00:00:01Z",
                "failure_ids": ["drift-001"] if known_drift else [],
            }
        ],
        "known_drift": (
            [
                {
                    "id": "drift-001",
                    "path": "README.md",
                    "prior_sha256": "0" * 63 + "1",
                    "current_sha256": "0" * 63 + "2",
                    "source_receipts": ["docs/receipt.json"],
                    "reason": "历史收据绑定旧文件摘要",
                }
            ]
            if known_drift
            else []
        ),
    }


class BaselineAcceptanceTests(unittest.TestCase):
    def test_seal_in_ignored_private_root_keeps_baseline_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            (repository.root / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
            run(repository.root, "git", "add", ".gitignore")
            run(repository.root, "git", "commit", "-m", "忽略私有分析资料")
            input_path = root / "input.json"
            input_path.write_text(json.dumps(draft()) + "\n", encoding="utf-8")
            output = repository.root / "local-analysis/upstream/baseline/evidence/receipt.json"
            seal_baseline_acceptance(repository.root, input_path, output)
            self.assertEqual(validate_baseline_acceptance(repository.root, output)["result"], "accepted")
            self.assertEqual(run(repository.root, "git", "status", "--porcelain"), "")

    def test_seal_and_validate_clean_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            input_path = root / "input.json"
            output_path = root / "baseline.json"
            input_path.write_text(json.dumps(draft()) + "\n", encoding="utf-8")
            receipt = seal_baseline_acceptance(repository.root, input_path, output_path)
            self.assertEqual(receipt["result"], "accepted")
            self.assertEqual(receipt["evidence"]["result"], "passed")
            self.assertEqual(validate_baseline_acceptance(repository.root, output_path)["result"], "accepted")

    def test_seal_preserves_explicit_known_drift_without_accepting_function_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            input_path = root / "input.json"
            output_path = root / "baseline.json"
            input_path.write_text(json.dumps(draft(known_drift=True)) + "\n", encoding="utf-8")
            receipt = seal_baseline_acceptance(repository.root, input_path, output_path)
            self.assertEqual(receipt["evidence"]["result"], "accepted_with_known_drift")
            self.assertEqual(len(receipt["evidence"]["known_drift"]), 1)

    def test_functional_known_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            invalid = draft()
            invalid["functional_checks"] = [
                {
                    "id": "go-build",
                    "status": "known_drift",
                    "command": "go build ./...",
                    "observed_at_utc": "2026-09-09T00:00:00Z",
                    "failure_ids": ["drift-001"],
                }
            ]
            input_path = root / "input.json"
            output_path = root / "baseline.json"
            input_path.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(UpstreamMergeError, "status 非法"):
                seal_baseline_acceptance(repository.root, input_path, output_path)

    def test_receipt_is_bound_to_current_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            input_path = root / "input.json"
            output_path = root / "baseline.json"
            input_path.write_text(json.dumps(draft()) + "\n", encoding="utf-8")
            seal_baseline_acceptance(repository.root, input_path, output_path)
            (repository.root / "README.md").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
                validate_baseline_acceptance(repository.root, output_path)
            # 历史计划回放只校验收据自身及其冻结 commit/tree，不把后续工作树
            # 的变化误报成基线收据损坏。
            self.assertEqual(
                validate_baseline_acceptance(
                    repository.root,
                    output_path,
                    require_current=False,
                )["result"],
                "accepted",
            )

    def test_seal_rejects_repository_internal_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = BaselineRepository(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(draft()) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(UpstreamMergeError, "仓库之外"):
                seal_baseline_acceptance(
                    repository.root,
                    input_path,
                    repository.root / "baseline.json",
                )


if __name__ == "__main__":
    unittest.main()
