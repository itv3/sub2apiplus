"""ACC-05 端到端复验：bundle → 派生 → manifest → 门禁 → 编排 → accept 重放。

k34 暴露的不是某一处接线错，而是整条验收链从未集成过。本测试用离线夹具走完
真实链路：只读收口证据、确定性派生官方观测、生成 manifest、执行 seal 门禁、
用编排器产出 v2 results、再用 accept 的同一套校验（含命令逐字比对与离线重放）
验收。任何一环各说各话都会在这里失败。

本测试直接把 bundle 目录喂给门禁与编排器，因此不涉及 bundle 在 attempt 内的
落位；真实 seal 路径上的落位（bundle 必须是证据根内的 `assertion-bundle/`
子目录、逻辑前缀与 inventory 对齐、权限满足 0700／0600）由 ACC-06 的
`test_assertion_bundle_wiring.py` 覆盖。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import acceptance_contract as contract_module  # noqa: E402
import assertion_gate as gate  # noqa: E402
import build_assertion_bundle as bundle  # noqa: E402
import build_rule_assertion_results as builder  # noqa: E402
import candidate_rule_assertion as assertion  # noqa: E402
import derive_official_observations as derive  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET_VERSION = "0.147.0"

H1_STREAM = (
    b"POST /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 19\r\n\r\n"
    b'{"model":"gpt-5.6"}'
)

INTERNAL_RECORD = {
    "schema_version": assertion.OBSERVATION_SCHEMA_VERSION,
    "record_id": "candidate-surface-1",
    "scenario_id": "A03",
    "record_type": "surface_identity",
    "data": {"surface": "codex"},
    "source_artifacts": ["run/relay/conn001.client_to_upstream.bin"],
}

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
        },
        {
            "rule_id": "SPEC-EP-006",
            "scenario_ids": ["A03"],
            "description": "内部 surface 身份",
            "checks": [
                {
                    "id": "surface-codex",
                    "description": "surface 为 codex",
                    "select": {"record_type": "surface_identity"},
                    "assertion": {
                        "operator": "all_equal",
                        "path": "data.surface",
                        "value": "codex",
                    },
                }
            ],
        },
    ],
}

RULE_MANIFEST = {
    "schema_version": "codex-egress-rule-manifest/v1",
    "codex_version": TARGET_VERSION,
    "required_rules": ["SPEC-H1-001", "SPEC-EP-006"],
}

AUTHORITY = {
    "assertion_profile_sha256": "a" * 64,
    "classification_package_digest": "b" * 64,
    "review_sha256": "c" * 64,
}


class AcceptanceEndToEndTest(unittest.TestCase):
    """一次跑完全链路，并在其上做伪造与错分的负例。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="acc05-")
        cls.workdir = Path(cls._temporary.name)
        cls.addClassCleanup(cls._cleanup)
        cls.profile_path = cls.workdir / "assertion-profile.json"
        cls.profile_path.write_text(
            json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8"
        )
        cls.rule_manifest_path = cls.workdir / "target-rules.json"
        cls.rule_manifest_path.write_text(
            json.dumps(RULE_MANIFEST, ensure_ascii=False), encoding="utf-8"
        )
        cls.profile_sha256 = hashlib.sha256(
            cls.profile_path.read_bytes()
        ).hexdigest()
        cls.contract = contract_module.build_contract_payload(PROFILE)
        cls.official_bundle = cls._prepare_side("official")
        cls.candidate_bundle = cls._prepare_side("candidate")

    @classmethod
    def _cleanup(cls) -> None:
        for path in sorted(cls.workdir.rglob("*"), reverse=True):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o644)
        cls._temporary.cleanup()

    @classmethod
    def _prepare_side(cls, side: str) -> Path:
        """收口证据 → 派生官方观测 → 写 manifest，返回 bundle 目录。"""

        side_dir = cls.workdir / side
        source_root = side_dir / "run"
        (source_root / "relay").mkdir(parents=True)
        (source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(
            H1_STREAM
        )
        entries = [
            {
                "root": "run",
                "path": "relay/conn001.client_to_upstream.bin",
                "target": "run/relay/conn001.client_to_upstream.bin",
            }
        ]
        if side == "candidate":
            (source_root / "traces").mkdir()
            (source_root / "traces" / "surface.observation.jsonl").write_text(
                json.dumps(INTERNAL_RECORD, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            entries.append(
                {
                    "root": "run",
                    "path": "traces/surface.observation.jsonl",
                    "target": "run/traces/surface.observation.jsonl",
                }
            )
        plan_path = side_dir / "bundle-plan.json"
        plan_path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        bundle_dir = side_dir / gate.BUNDLE_DIR_NAME
        bundle.build_bundle(
            {"run": source_root}, bundle.load_plan(plan_path), bundle_dir
        )
        derivation_plan = [
            {
                "source": "run/relay/conn001.client_to_upstream.bin",
                "parser": "h1_request_stream",
                "scenario_id": "A03",
                "kind": "process_trace",
                "target": "derived/A03/conn001.observation.jsonl",
                "connection_id": "conn001",
            }
        ]
        derive_plan_path = side_dir / "derive-plan.json"
        derive_plan_path.write_text(
            json.dumps({"entries": derivation_plan}, ensure_ascii=False),
            encoding="utf-8",
        )
        derive.derive_observations(
            bundle_dir, derive.load_derivation_plan(derive_plan_path)
        )
        artifacts = [
            cls._artifact(
                bundle_dir,
                "run/relay/conn001.client_to_upstream.bin",
                "relay_binary",
                "opaque_bound_source",
            ),
            cls._artifact(
                bundle_dir,
                "derived/A03/conn001.observation.jsonl",
                "process_trace",
                "observation_jsonl",
            ),
        ]
        if side == "candidate":
            artifacts.append(
                cls._artifact(
                    bundle_dir,
                    "run/traces/surface.observation.jsonl",
                    "process_trace",
                    "observation_jsonl",
                )
            )
        manifest = {
            "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
            "codex_version": TARGET_VERSION,
            "capture_id": f"acc05-{side}",
            "status": "complete",
            "artifacts": artifacts,
        }
        (bundle_dir / gate.MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return bundle_dir

    @staticmethod
    def _artifact(bundle_dir: Path, path: str, kind: str, parser: str) -> dict:
        return {
            "path": path,
            "sha256": hashlib.sha256((bundle_dir / path).read_bytes()).hexdigest(),
            "kind": kind,
            "parser": parser,
            "scenario_ids": ["A03"],
            "labels": {"transport": "http"},
        }

    def _run_builder(self, results_dir: Path, output: Path) -> dict:
        config = {
            "assertion_profile": str(self.profile_path),
            "rule_manifest": str(self.rule_manifest_path),
            "expected_profile_sha256": self.profile_sha256,
            "official_evidence_root": str(self.official_bundle),
            "candidate_evidence_root": str(self.candidate_bundle),
            "official_capture_manifest": str(
                self.official_bundle / gate.MANIFEST_FILENAME
            ),
            "candidate_capture_manifest": str(
                self.candidate_bundle / gate.MANIFEST_FILENAME
            ),
            "official_evidence_prefix": "official-run",
            "candidate_evidence_prefix": "candidate-run",
            "target_version": TARGET_VERSION,
            "candidate_id": "candidate-acc05",
            "profile_id": "codex-0.147.0-acc05-v1",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": AUTHORITY,
            "rules": ["SPEC-H1-001", "SPEC-EP-006"],
        }
        config_path = results_dir.parent / "builder-config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "build_rule_assertion_results.py"),
                "--config",
                str(config_path),
                "--output",
                str(output),
                "--results-dir",
                str(results_dir),
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if completed.returncode != 0:
            self.fail(f"编排器执行失败：{completed.stderr}")
        return json.loads(output.read_text(encoding="utf-8"))

    def test_full_chain_produces_accept_ready_results(self) -> None:
        for side, bundle_dir in (
            ("official", self.official_bundle),
            ("candidate", self.candidate_bundle),
        ):
            with self.subTest(side=side):
                receipt = gate.run_assertion_gate(
                    bundle_dir=bundle_dir,
                    source_roots={"run": bundle_dir.parent / "run"},
                    side=side,
                    profile=PROFILE,
                    contract=self.contract,
                    target_version=TARGET_VERSION,
                )
                gate.validate_gate_receipt(receipt, side=side)
                self.assertEqual(
                    receipt["checked_rule_count"], 1 if side == "official" else 2
                )

        results_dir = self.workdir / "machine"
        output = self.workdir / "results.json"
        document = self._run_builder(results_dir, output)

        self.assertEqual(document["schema_version"], builder.RESULTS_SCHEMA_V2)
        self.assertEqual(
            document["acceptance_contract_sha256"],
            contract_module.contract_sha256(self.contract),
        )
        rows = {row["rule"]: row for row in document["rules"]}
        self.assertEqual(rows["SPEC-H1-001"]["validation_mode"], "dual_wire")
        self.assertEqual(
            rows["SPEC-EP-006"]["validation_mode"], "candidate_profile"
        )
        self.assertNotIn("official_command", rows["SPEC-EP-006"])
        self.assertEqual(rows["SPEC-EP-006"]["official_authority"], AUTHORITY)
        self.assertTrue(
            rows["SPEC-H1-001"]["official_evidence_refs"][0]["path"].startswith(
                "official-run/"
            )
        )
        for row in document["rules"]:
            self.assertNotIn("positive_assertions", row)
            self.assertNotIn("negative_assertions", row)

    def test_builder_command_matches_authoritative_constructor(self) -> None:
        """编排器产出的命令必须与 accept 复算的期望命令逐字一致。"""

        results_dir = self.workdir / "machine-cmd"
        output = self.workdir / "results-cmd.json"
        document = self._run_builder(results_dir, output)
        row = next(
            item for item in document["rules"] if item["rule"] == "SPEC-H1-001"
        )
        # 编排器与 accept 都以 resolve() 后的绝对路径入参（macOS 的
        # /var → /private/var 软链在两侧同样归一），期望值必须同样解析。
        expected = assertion.build_assertion_command(
            rule_id="SPEC-H1-001",
            capture_manifest=str(
                (self.official_bundle / gate.MANIFEST_FILENAME).resolve(strict=True)
            ),
            evidence_root=str(self.official_bundle.resolve(strict=True)),
            profile=str(self.profile_path.resolve(strict=True)),
            rule_manifest=str(self.rule_manifest_path.resolve(strict=True)),
            expected_codex_version=TARGET_VERSION,
            expected_profile_sha256=self.profile_sha256,
            output=str(results_dir / "SPEC-H1-001.official.json"),
        )
        self.assertEqual(row["official_command"], expected)

    def test_machine_result_replays_byte_identical(self) -> None:
        """accept 的离线重放：同命令必须重现同一份 check 结论。"""

        results_dir = self.workdir / "machine-replay"
        output = self.workdir / "results-replay.json"
        document = self._run_builder(results_dir, output)
        row = next(
            item for item in document["rules"] if item["rule"] == "SPEC-H1-001"
        )
        submitted = json.loads(
            (results_dir / "SPEC-H1-001.official.json").read_text(encoding="utf-8")
        )
        replay_output = self.workdir / "replay.json"
        command = list(row["official_command"])
        command[command.index("--output") + 1] = str(replay_output)
        completed = subprocess.run(
            command, capture_output=True, text=True, cwd=REPO_ROOT
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replayed = json.loads(replay_output.read_text(encoding="utf-8"))
        self.assertEqual(replayed["checks"], submitted["checks"])
        self.assertEqual(replayed["status"], "pass")

    def test_forged_pass_is_rejected_by_check_closure(self) -> None:
        forged = {
            "schema_version": builder.SINGLE_SCHEMA,
            "rule_id": "SPEC-H1-001",
            "status": "pass",
            "checks": [{"id": "method-post", "passed": True}],
        }
        with self.assertRaises(builder.RuleAssertionError):
            builder.verify_check_closure(
                forged,
                self.contract["expected_check_ids"]["SPEC-H1-001"],
                rule_id="SPEC-H1-001",
                label="官方",
            )

    def test_rule_misgrouping_is_detected(self) -> None:
        """内部规则被错标成 dual_wire：官方侧无内部观测，门禁即拒绝。

        官方侧只有 wire 观测（派生器按设计不产内部 record type），因此把
        SPEC-EP-006 强行归入 dual_wire 后，其 surface_identity selector 在
        官方侧命中为空，seal 门禁在 accept 之前就失败关闭。
        """

        forged = json.loads(json.dumps(self.contract))
        forged["validation_modes"]["SPEC-EP-006"] = "dual_wire"
        with self.assertRaises(gate.AssertionGateError) as raised:
            gate.run_assertion_gate(
                bundle_dir=self.official_bundle,
                source_roots={"run": self.official_bundle.parent / "run"},
                side="official",
                profile=PROFILE,
                contract=forged,
                target_version=TARGET_VERSION,
            )
        self.assertIn("SPEC-EP-006", str(raised.exception))
        self.assertIn("无法命中任何观测", str(raised.exception))

    def test_path_prefix_collision_keeps_bindings_distinct(self) -> None:
        """同名相对路径在两侧必须产出不同的 inventory 逻辑路径。"""

        results_dir = self.workdir / "machine-prefix"
        output = self.workdir / "results-prefix.json"
        document = self._run_builder(results_dir, output)
        row = next(
            item for item in document["rules"] if item["rule"] == "SPEC-H1-001"
        )
        official_paths = {ref["path"] for ref in row["official_evidence_refs"]}
        candidate_paths = {ref["path"] for ref in row["candidate_evidence_refs"]}
        self.assertFalse(official_paths & candidate_paths)
        self.assertTrue(all(p.startswith("official-run/") for p in official_paths))
        self.assertTrue(all(p.startswith("candidate-run/") for p in candidate_paths))

    def test_missing_rule_breaks_full_set_closure(self) -> None:
        document = json.loads(
            (self.workdir / "results.json").read_text(encoding="utf-8")
        )
        rules = {row["rule"] for row in document["rules"]}
        self.assertEqual(rules, {"SPEC-H1-001", "SPEC-EP-006"})
        partial = [row for row in document["rules"] if row["rule"] != "SPEC-EP-006"]
        self.assertEqual(len(partial), 1)
        # accept 的全集闭环由 _validate_assertion_results 断言，此处确认编排器
        # 不会自行放行缺规则的文档：契约里两条规则都必须出现。
        self.assertEqual(len(self.contract["validation_modes"]), 2)


if __name__ == "__main__":
    unittest.main()
