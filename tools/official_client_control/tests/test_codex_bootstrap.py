"""只读消费 FW-C Codex 收据与 Runtime Catalog 的兼容性证明。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_control.canonical import canonical_sha256, sha256_file
from tools.official_client_control.errors import ControlError
from tools.official_client_control.receipts import control_tool_bundle_sha256
from tools.official_client_control.store import ControlStore


class CodexBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(__file__).resolve().parents[3]
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "store"
        self.store = ControlStore.initialize(root, "2026-08-18T00:00:00Z")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _binding(self, relative: str) -> dict[str, object]:
        path = self.repo / relative
        return {
            "path": relative,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    def test_fw_c_inputs_are_bound_as_external_immutable_evidence(self) -> None:
        contract_paths = sorted(
            [
                "backend/internal/officialegress/compiler.go",
                "backend/internal/officialegress/executor.go",
                "backend/internal/officialegress/guard.go",
                "backend/internal/officialegress/persona_registry.go",
                "backend/internal/officialegress/persona_release_catalog.go",
            ]
        )
        receipt_paths = sorted(
            [
                "docs/egress/maintenance/CODEX_CLI_0145_TO_0147_K83_CATALOG_PROMOTION_RECEIPT.json",
                "docs/egress/maintenance/CODEX_CLI_0145_TO_0147_K83_PRODUCTION_ACTIVATION_RECEIPT.json",
            ]
        )
        runtime_paths = sorted(
            path.relative_to(self.repo).as_posix()
            for path in (
                self.repo / "backend/internal/officialegress/catalogdata/runtime"
            ).rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        contract_sources = [self._binding(path) for path in contract_paths]
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        payload = {
            "schema_version": "official-client-control-bootstrap/v1",
            "source_commit": commit,
            "contract_sources": contract_sources,
            "contract_bundle_sha256": canonical_sha256(contract_sources),
            "fw_c_receipts": [self._binding(path) for path in receipt_paths],
            "runtime_catalog": [self._binding(path) for path in runtime_paths],
            "tool_bundle_sha256": control_tool_bundle_sha256(),
            "result": "stable",
        }
        reference = self.store.seal_object("bootstrap", payload)
        stored = self.store.load_object(reference)["payload"]
        self.assertEqual(stored["fw_c_receipts"], payload["fw_c_receipts"])
        result = self.store.replay(external_root=self.repo, require_external=True)
        self.assertEqual(result["result"], "passed")
        self.assertTrue(result["external_verified"])

    def test_current_codex_snapshot_can_be_sealed_without_runtime_registration(self) -> None:
        persona = {
            "provider": "openai",
            "official_product": "codex-cli",
            "auth_family": "oauth",
            "upstream_route_family": "responses",
        }
        schema_ref = self.store.seal_object(
            "profile_schema",
            {
                "schema_version": "official-client-profile-schema/v1",
                "persona": persona,
                "version": "0.147.0",
                "schema_id": "codex-read-only-runtime-contract",
                "document": {"source": "FW-B-read-only-contract"},
            },
        )
        snapshot_path = self.repo / (
            "backend/internal/officialegress/catalogdata/runtime/profiles/0.147.0/"
            "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc.json"
        )
        snapshot_document = json.loads(snapshot_path.read_text(encoding="utf-8"))
        payload = {
            "schema_version": "official-client-snapshot/v1",
            "persona": persona,
            "version": "0.147.0",
            "profile_digest": snapshot_document["Digest"],
            "profile_schema_ref": schema_ref,
            "compiler_attestation_sha256": sha256_file(
                self.repo / "backend/internal/officialegress/compiler.go"
            ),
            "document": snapshot_document,
        }
        reference = self.store.seal_object("snapshot", payload)
        stored = self.store.load_object(reference)["payload"]
        self.assertEqual(stored["document"], snapshot_document)
        self.assertEqual(stored["profile_digest"], snapshot_document["Digest"])
        with self.assertRaisesRegex(ControlError, "禁止覆盖"):
            self.store.seal_object("snapshot", payload)


if __name__ == "__main__":
    unittest.main()
