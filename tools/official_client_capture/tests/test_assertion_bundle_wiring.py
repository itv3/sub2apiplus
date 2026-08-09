"""ACC-06 接线：assertion-bundle 必须能在真实 seal 路径上落位并通过门禁。

ACC-02／02b 产出的证据包若没有接进采集流程，`seal` 会在断言门禁失败——而这
恰恰是 §10.8 反复强调的"最后一步才发现"。本测试直接驱动
`_capture_assertion_context` 与 `_run_seal_assertion_gate`（seal 真实调用的两个
入口），确认：

- bundle 作为 attempt 证据根内的子目录被正确定位；
- `evidence_root` 指向 bundle 自身、`evidence_prefix` 是它在封存 inventory
  中的逻辑前缀，机器 check 的相对路径加前缀后逐字命中 inventory；
- bundle 与派生产物的权限满足 `_evidence_permissions_private` 的 0700／0600 门禁；
- manifest 不在 bundle 内、bundle 收口自身内容时失败关闭。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[3]))

import build_assertion_bundle as bundle  # noqa: E402
import candidate_rule_assertion as assertion  # noqa: E402
import derive_official_observations as derive  # noqa: E402
from tools.official_client_capture import codex_upgrade  # noqa: E402

TARGET_VERSION = "0.147.0"

H1_STREAM = (
    b"POST /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 19\r\n\r\n"
    b'{"model":"gpt-5.6"}'
)

PROFILE = {
    "schema_version": "codex-candidate-rule-expectations/v1",
    "codex_version": TARGET_VERSION,
    "source_spec": "docs/CODEX_CLI_0145_EGRESS_SPEC.md#第二章",
    "source_spec_sha256": "0" * 64,
    "scenarios": [
        {
            "scenario_id": "A03",
            "description": "responses 出站",
            "trigger": "codex exec",
            "preconditions": ["已登录"],
            "required_artifact_kinds": ["relay_binary", "process_trace"],
        }
    ],
    "rules": [
        {
            "rule_id": "SPEC-H1-001",
            "scenario_ids": ["A03"],
            "description": "responses 使用 POST",
            "checks": [
                {
                    "id": "method-post",
                    "description": "方法为 POST",
                    "select": {"record_type": "http_request"},
                    "assertion": {
                        "operator": "all_equal",
                        "path": "data.method",
                        "value": "POST",
                    },
                }
            ],
        }
    ],
}


class AssertionBundleWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="acc06-"))
        self.addCleanup(self._cleanup)
        # 模拟 attempt 布局：job 根与 attempt 环境根并列，bundle 落在环境根内。
        self.job_root = self.workdir / "official-relay-run"
        self.job_root.mkdir(mode=0o700)
        (self.job_root / "relay").mkdir(mode=0o700)
        source = self.job_root / "relay" / "conn001.client_to_upstream.bin"
        source.write_bytes(H1_STREAM)
        source.chmod(0o600)
        self.attempt_root = self.workdir / "evidence"
        self.attempt_root.mkdir(mode=0o700)
        (self.attempt_root / "restoration.json").write_text("{}\n", encoding="utf-8")
        (self.attempt_root / "restoration.json").chmod(0o600)
        self.roots = [self.job_root, self.attempt_root]
        self.bundle_dir = self.attempt_root / bundle_dir_name()

    def _cleanup(self) -> None:
        for path in sorted(self.workdir.rglob("*"), reverse=True):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
            elif path.is_dir():
                path.chmod(0o700)
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    def _build_bundle(self, *, self_reference: bool = False) -> None:
        entries = [
            {
                "root": "official-relay-run",
                "path": "relay/conn001.client_to_upstream.bin",
                "target": "relay/conn001.client_to_upstream.bin",
            }
        ]
        plan_path = self.workdir / "plan.json"
        plan_path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        bundle.build_bundle(
            {
                "official-relay-run": self.job_root,
                "evidence": self.attempt_root,
            },
            bundle.load_plan(plan_path),
            self.bundle_dir,
        )
        derivation = [
            {
                "source": "relay/conn001.client_to_upstream.bin",
                "parser": "h1_request_stream",
                "scenario_id": "A03",
                "kind": "process_trace",
                "target": "derived/A03/conn001.observation.jsonl",
                "connection_id": "conn001",
            }
        ]
        derive_plan = self.workdir / "derive-plan.json"
        derive_plan.write_text(
            json.dumps({"entries": derivation}, ensure_ascii=False), encoding="utf-8"
        )
        derive.derive_observations(
            self.bundle_dir, derive.load_derivation_plan(derive_plan)
        )
        if self_reference:
            # 把 bundle 内的文件再收口一次，模拟自引用。
            provenance_path = self.bundle_dir / bundle.PROVENANCE_FILENAME
            document = json.loads(provenance_path.read_text(encoding="utf-8"))
            entry = dict(document["entries"][0])
            entry["source_root"] = "evidence"
            entry["source_path"] = (
                f"{bundle_dir_name()}/relay/conn001.client_to_upstream.bin"
            )
            entry["source_inventory_path"] = f"evidence/{entry['source_path']}"
            document["entries"] = [entry]
            document["entry_count"] = 1
            document["provenance_sha256"] = bundle._canonical_sha256(
                {
                    "entries": document["entries"],
                    "schema_version": bundle.BUNDLE_PROVENANCE_SCHEMA,
                }
            )
            provenance_path.chmod(0o600)
            provenance_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            provenance_path.chmod(0o400)
        self._write_manifest()

    def _write_manifest(self) -> None:
        def artifact(relative: str, kind: str, parser: str) -> dict:
            path = self.bundle_dir / relative
            return {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "kind": kind,
                "parser": parser,
                "scenario_ids": ["A03"],
                "labels": {"transport": "http"},
            }

        manifest = {
            "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
            "codex_version": TARGET_VERSION,
            "capture_id": "acc06",
            "status": "complete",
            "artifacts": [
                artifact(
                    "relay/conn001.client_to_upstream.bin",
                    "relay_binary",
                    "opaque_bound_source",
                ),
                artifact(
                    "derived/A03/conn001.observation.jsonl",
                    "process_trace",
                    "observation_jsonl",
                ),
            ],
        }
        path = self.bundle_dir / "capture-manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def test_context_points_at_bundle_with_inventory_prefix(self) -> None:
        self._build_bundle()
        context = codex_upgrade._capture_assertion_context(
            None, None, self.roots, target_version=TARGET_VERSION
        )
        self.assertEqual(
            context["evidence_root"], str(self.bundle_dir.resolve(strict=True))
        )
        self.assertEqual(
            context["evidence_prefix"], f"evidence/{bundle_dir_name()}"
        )
        # 机器 check 的相对路径加前缀后必须逐字命中封存 inventory。
        inventory = codex_upgrade._evidence_inventory(self.roots)
        logical = {entry["path"] for entry in inventory["entries"]}
        for relative in (
            "relay/conn001.client_to_upstream.bin",
            "derived/A03/conn001.observation.jsonl",
        ):
            self.assertIn(f"{context['evidence_prefix']}/{relative}", logical)
        self.assertIn(context["capture_manifest"]["path"], logical)

    def test_bundle_satisfies_private_permission_gate(self) -> None:
        self._build_bundle()
        self.assertTrue(codex_upgrade._evidence_permissions_private(self.roots))

    def test_seal_gate_passes_on_wired_bundle(self) -> None:
        self._build_bundle()
        context = codex_upgrade._capture_assertion_context(
            None, None, self.roots, target_version=TARGET_VERSION
        )
        original = codex_upgrade.load_acceptance_profile
        try:
            codex_upgrade.load_acceptance_profile = lambda _path: PROFILE
            codex_upgrade.verify_frozen_contract = (
                lambda profile: codex_upgrade.build_acceptance_contract(profile)
            )
            receipt = codex_upgrade._run_seal_assertion_gate(
                context,
                self.roots,
                phase="official",
                target_version=TARGET_VERSION,
            )
        finally:
            codex_upgrade.load_acceptance_profile = original
        self.assertEqual(receipt["side"], "official")
        self.assertEqual(receipt["checked_rule_count"], 1)
        codex_upgrade.validate_gate_receipt(receipt, side="official")

    def test_manifest_outside_bundle_is_rejected(self) -> None:
        self._build_bundle()
        stray = self.attempt_root / "capture-manifest.json"
        (self.bundle_dir / "capture-manifest.json").chmod(0o600)
        (self.bundle_dir / "capture-manifest.json").rename(stray)
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError, "断言证据包内"
        ):
            codex_upgrade._capture_assertion_context(
                None, None, self.roots, target_version=TARGET_VERSION
            )

    def test_self_referencing_bundle_is_rejected(self) -> None:
        self._build_bundle(self_reference=True)
        context = codex_upgrade._capture_assertion_context(
            None, None, self.roots, target_version=TARGET_VERSION
        )
        original = codex_upgrade.load_acceptance_profile
        try:
            codex_upgrade.load_acceptance_profile = lambda _path: PROFILE
            codex_upgrade.verify_frozen_contract = (
                lambda profile: codex_upgrade.build_acceptance_contract(profile)
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "禁止收口自身内容"
            ):
                codex_upgrade._run_seal_assertion_gate(
                    context,
                    self.roots,
                    phase="official",
                    target_version=TARGET_VERSION,
                )
        finally:
            codex_upgrade.load_acceptance_profile = original


def bundle_dir_name() -> str:
    from tools.official_client_capture.assertion_gate import BUNDLE_DIR_NAME

    return BUNDLE_DIR_NAME


if __name__ == "__main__":
    unittest.main()
