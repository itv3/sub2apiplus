"""验证 Codex CLI 候选出站面只通过追加式 transition 登记。"""

from __future__ import annotations

import copy
import json
import unittest

from tools import check_ledger_completeness as ledger


class Codex01491EgressSurfaceSuccessorTransitionTests(unittest.TestCase):
    def _fixtures(
        self,
    ) -> tuple[dict[str, object], bytes, bytes, bytes]:
        transition = json.loads(
            ledger.CANDIDATE_SURFACE_SUCCESSOR.read_text(encoding="utf-8"),
            object_pairs_hook=ledger._unique_json_object,
        )
        return (
            transition,
            ledger.INVENTORY.read_bytes(),
            ledger.CHANGESET6_TRANSITION.read_bytes(),
            ledger.CANDIDATE_GATE_TRANSITION.read_bytes(),
        )

    def _seal(self, transition: dict[str, object]) -> None:
        transition["identity_sha256"] = ledger.candidate_surface_successor_identity(
            transition
        )

    def test_后继身份与新增出站面可独立重放(self) -> None:
        transition, inventory, predecessor, candidate_gate = self._fixtures()
        additions = ledger.validate_candidate_surface_successor(
            transition,
            inventory,
            predecessor,
            candidate_gate,
        )
        self.assertEqual(
            [entry["path"] for entry in additions],
            ["backend/internal/officialegress/routing_hint.go"],
        )

    def test_拒绝自摘要与前序摘要篡改(self) -> None:
        transition, inventory, predecessor, candidate_gate = self._fixtures()
        identity_mutation = copy.deepcopy(transition)
        identity_mutation["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "自摘要漂移"):
            ledger.validate_candidate_surface_successor(
                identity_mutation,
                inventory,
                predecessor,
                candidate_gate,
            )

        predecessor_mutation = copy.deepcopy(transition)
        predecessor_mutation["predecessor_transition"]["file_sha256"] = "0" * 64
        self._seal(predecessor_mutation)
        with self.assertRaisesRegex(RuntimeError, "精确绑定变更集 6"):
            ledger.validate_candidate_surface_successor(
                predecessor_mutation,
                inventory,
                predecessor,
                candidate_gate,
            )

    def test_拒绝移除历史路径与实现摘要漂移(self) -> None:
        transition, inventory, predecessor, candidate_gate = self._fixtures()
        removal_mutation = copy.deepcopy(transition)
        removal_mutation["removals"] = ["backend/internal/officialegress/body_document.go"]
        self._seal(removal_mutation)
        with self.assertRaisesRegex(RuntimeError, "禁止移除历史路径"):
            ledger.validate_candidate_surface_successor(
                removal_mutation,
                inventory,
                predecessor,
                candidate_gate,
            )

        implementation_mutation = copy.deepcopy(transition)
        implementation_mutation["implementation_transitions"][0]["to_sha256"] = (
            "0" * 64
        )
        self._seal(implementation_mutation)
        with self.assertRaisesRegex(RuntimeError, "实现摘要非法"):
            ledger.validate_candidate_surface_successor(
                implementation_mutation,
                inventory,
                predecessor,
                candidate_gate,
            )


if __name__ == "__main__":
    unittest.main()
