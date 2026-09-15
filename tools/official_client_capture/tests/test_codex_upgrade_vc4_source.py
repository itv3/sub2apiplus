"""VC-4 Candidate Git 源码与 source transition 边界测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade


class CodexUpgradeVC4SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "candidate-source"
        self.source.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "codex-test@example.invalid")
        self._git("config", "user.name", "Codex Test")
        self.managed = self.source / "managed.txt"
        self.managed.write_text("baseline\n", encoding="utf-8")
        self._git("add", "managed.txt")
        self._git("commit", "-q", "-m", "baseline")
        self.base_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.before_sha256 = hashlib.sha256(self.managed.read_bytes()).hexdigest()

        self.managed.write_text("candidate\n", encoding="utf-8")
        self._git("add", "managed.txt")
        self._git("commit", "-q", "-m", "candidate")
        self.current_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.after_sha256 = hashlib.sha256(self.managed.read_bytes()).hexdigest()
        self.transition = self._transition()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.source), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _transition(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": "2026-09-12T08:00:00Z",
            "base_commit": self.base_commit,
            "current_commit": self.current_commit,
            "scope": "upstream-codex-0-154-candidate-freeze-successor",
            "mode": "commit",
            "extra_worktree_paths": [],
            "frozen_path_count": 1,
            "frozen_edge_count": 1,
            "changed_path_count": 1,
            "transitions": [
                {
                    "path": "managed.txt",
                    "old_path": "",
                    "status": "M",
                    "predecessor_sha256s": [self.before_sha256],
                    "to_sha256": self.after_sha256,
                    "source_receipts": ["docs/egress/maintenance/baseline.json"],
                    "reason": "登记 Candidate 源码后继摘要",
                }
            ],
            "unregistered_path_count": 0,
            "unregistered_paths": [],
            "deleted_frozen_paths": [],
            "required_manual_actions": [],
            "verification": ["make check-egress-spec"],
            "safety": {
                "live_account_used": False,
                "official_egress_profile_changed": False,
                "production_config_changed": False,
                "wire_or_persona_selection_changed": False,
                "deployment_performed": False,
            },
            "result": "passed_local_evidence_successor",
        }
        payload["identity_sha256"] = codex_upgrade._fingerprint(payload)
        return payload

    def _signed(self, payload: dict[str, object]) -> dict[str, object]:
        payload = copy.deepcopy(payload)
        payload.pop("identity_sha256", None)
        payload["identity_sha256"] = codex_upgrade._fingerprint(payload)
        return payload

    def test_transition_with_proven_deletion_is_accepted_and_unproven_rejected(self) -> None:
        """B1：删除冻结路径的 transition 必须以 passed_with_deletions 携带三项证明。"""

        retired = self.source / "retired.py"
        retired.write_text("VALUE = 1\n", encoding="utf-8")
        self._git("add", "retired.py")
        self._git("commit", "-q", "-m", "add retired")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        before_managed = hashlib.sha256(self.managed.read_bytes()).hexdigest()
        retired.unlink()
        reader = self.source / "reader.py"
        reader.write_text("def load():\n    return None\n", encoding="utf-8")
        self.managed.write_text("after deletion\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "retire module")
        current = self._git("rev-parse", "HEAD").stdout.strip()
        reader_sha256 = hashlib.sha256(reader.read_bytes()).hexdigest()
        proof = {
            "algorithm": "deletion-proof/v1",
            "reason": "执行分支已由 reconciler 取代",
            "deleted_paths": [
                {
                    "path": "retired.py",
                    "frozen": True,
                    "last_sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
                    "reference_scan": {
                        "algorithm": "reference-scan/v1",
                        "patterns": ["retired", "retired.py"],
                        "scopes": ["python:import-and-attribute", "shell:invocation", "json:command-fields"],
                        "references": [],
                    },
                }
            ],
            "historical_readers": [{"path": "reader.py", "sha256": reader_sha256}],
        }
        payload = copy.deepcopy(self.transition)
        payload.update(
            {
                "base_commit": base,
                "current_commit": current,
                "changed_path_count": 3,
                "deleted_frozen_paths": ["retired.py"],
                "result": "passed_with_deletions",
                "deletion_proof": proof,
            }
        )
        payload["transitions"][0]["predecessor_sha256s"] = [before_managed]
        payload["transitions"][0]["to_sha256"] = hashlib.sha256(self.managed.read_bytes()).hexdigest()
        codex_upgrade._validate_candidate_source_transition(
            self.source, self._signed(payload), git_commit=current
        )
        negatives = []
        unproven = copy.deepcopy(payload)
        unproven.pop("deletion_proof")
        negatives.append((unproven, "deletion_proof"))
        wrong_result = copy.deepcopy(payload)
        wrong_result["result"] = "passed_local_evidence_successor"
        negatives.append((wrong_result, "passed_with_deletions"))
        referenced = copy.deepcopy(payload)
        referenced["deletion_proof"]["deleted_paths"][0]["reference_scan"]["references"] = [
            {"kind": "python", "path": "reader.py", "line": 1, "content": "import retired"}
        ]
        negatives.append((referenced, "无引用扫描证明非法"))
        drifted = copy.deepcopy(payload)
        drifted["deletion_proof"]["historical_readers"][0]["path"] = "missing_reader.py"
        negatives.append((drifted, "历史读取器不存在"))
        uncovered = copy.deepcopy(payload)
        uncovered["deletion_proof"]["deleted_paths"][0]["path"] = "other.py"
        negatives.append((uncovered, "没有无引用证明的冻结删除路径"))
        for document, message in negatives:
            with self.subTest(message=message), self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, message
            ):
                codex_upgrade._validate_candidate_source_transition(
                    self.source, self._signed(document), git_commit=current
                )
        # 没有删除冻结路径却在证明里声称冻结删除同样拒绝；未登记删除的证明则允许。
        extra = copy.deepcopy(self.transition)
        extra["deletion_proof"] = copy.deepcopy(proof)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "与 deleted_frozen_paths 不一致"):
            codex_upgrade._validate_candidate_source_transition(
                self.source, self._signed(extra), git_commit=self.current_commit
            )
        extra["deletion_proof"]["deleted_paths"][0]["frozen"] = False
        extra["deletion_proof"]["historical_readers"] = [{"path": "managed.txt", "sha256": self.after_sha256}]
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "历史读取器登记非法"):
            codex_upgrade._validate_candidate_source_transition(
                self.source, self._signed(extra), git_commit=self.current_commit
            )

    def test_transition_replays_from_both_git_blobs(self) -> None:
        codex_upgrade._validate_candidate_source_transition(
            self.source,
            self.transition,
            git_commit=self.current_commit,
        )

    def test_transition_rejects_self_digest_tampering(self) -> None:
        tampered = copy.deepcopy(self.transition)
        tampered["scope"] = "upstream-tampered-freeze-successor"
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "自摘要"):
            codex_upgrade._validate_candidate_source_transition(
                self.source,
                tampered,
                git_commit=self.current_commit,
            )

    def test_transition_rejects_official_egress_profile_change(self) -> None:
        tampered = copy.deepcopy(self.transition)
        tampered["safety"]["official_egress_profile_changed"] = True
        unsigned = dict(tampered)
        unsigned.pop("identity_sha256")
        tampered["identity_sha256"] = codex_upgrade._fingerprint(unsigned)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "安全边界"):
            codex_upgrade._validate_candidate_source_transition(
                self.source,
                tampered,
                git_commit=self.current_commit,
            )

    def test_transition_rejects_current_commit_mismatch(self) -> None:
        tampered = copy.deepcopy(self.transition)
        tampered["current_commit"] = self.base_commit
        unsigned = dict(tampered)
        unsigned.pop("identity_sha256")
        tampered["identity_sha256"] = codex_upgrade._fingerprint(unsigned)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "当前 commit"):
            codex_upgrade._validate_candidate_source_transition(
                self.source,
                tampered,
                git_commit=self.current_commit,
            )

    def test_transition_rejects_git_blob_digest_mismatch(self) -> None:
        tampered = copy.deepcopy(self.transition)
        tampered["transitions"][0]["to_sha256"] = "f" * 64
        unsigned = dict(tampered)
        unsigned.pop("identity_sha256")
        tampered["identity_sha256"] = codex_upgrade._fingerprint(unsigned)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "Git 两端"):
            codex_upgrade._validate_candidate_source_transition(
                self.source,
                tampered,
                git_commit=self.current_commit,
            )

    def test_transition_file_inside_source_is_rejected(self) -> None:
        transition_path = self.source / "transition.json"
        transition_path.write_text(json.dumps(self.transition), encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "源码树外"):
            codex_upgrade._require_outside_candidate_source(
                self.source,
                transition_path,
                "Candidate source transition",
            )

    def test_dirty_candidate_source_is_rejected(self) -> None:
        (self.source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是干净 commit"):
            codex_upgrade._require_clean_candidate_source(self.source)


if __name__ == "__main__":
    unittest.main()
