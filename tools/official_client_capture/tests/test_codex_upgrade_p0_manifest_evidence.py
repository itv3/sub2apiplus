"""E3-03：P0 离线门禁证据的新形状（以执行器的运行清单为证据）——签发与 VC-0 收口的逐条重验。

合成记录库覆盖逐条规则的正反向；端到端用例在临时 git 仓库里真跑执行器两次（重新执行全集出 v1、全集通过全部承接
出 v2），照编排器的组装方式签出两种 P0 收据，并都通过 VC-0 收口的 P0 校验。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
IDENTITY = {name: f"{index}" * 64 for index, name in enumerate(receipts.P0_IDENTITY_FIELDS, 1)}
SUBJECT = {"upgrade_id": "codex-0159-to-0160-r1", "campaign_id": None, "campaign_purpose": "validation_only",
           "baseline_version": "0.159.2", "target_version": "0.160.0", "candidate_id": None, "attempt_id": None}


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _assemble_p0(root: Path, capture: Path, egress: Path, identity: dict[str, str], *, passed_offset: int = 0,
                 declared: tuple[int, int] | None = None) -> Path:
    """照编排器 step_p0_receipt 的方式组装 P0 证据根：复制两份门禁证据，加发布认证（登记工具五摘要）与回退依据，
    facts 的离线门禁从证据取数（``passed_offset`` 故意改错 test-capture-tools 的通过数）；``declared`` 给出时两项
    门禁的（通过数, 跳过数）照它填（手写日志证据没有可取的数）。"""

    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    for name, source in (("test-capture-tools.json", capture), ("check-egress-spec.json", egress)):
        shutil.copyfile(source, evidence / name)
        (evidence / name).chmod(0o600)
    _write_json(evidence / "release-certification.json", {"schema_version": "合成发布认证", "identity": identity})
    _write_json(evidence / "rollback.json", {"rollback": True})
    gates = {}
    for gate_id in ("check-egress-spec", "test-capture-tools"):
        if declared is None:
            payload = json.loads((evidence / f"{gate_id}.json").read_text(encoding="utf-8"))
            passed, skipped = payload["passed"], payload["approved_skip"]
        else:
            passed, skipped = declared
        gates[gate_id] = {"gate_id": gate_id, "kind": "public", "command": ["make", gate_id], "exit_code": 0,
                          "passed": passed + (passed_offset if gate_id == "test-capture-tools" else 0), "failed": 0,
                          "approved_skip": skipped, "unexpected_skip": 0}
    release_sha256 = receipts.file_sha256(evidence / "release-certification.json")
    _write_json(root / "p0-facts.json", {
        "schema_version": receipts.FACTS_SCHEMA, "kind": "p0_gate", "subject": dict(SUBJECT),
        "assertions": {"offline_gates": [gates["check-egress-spec"], gates["test-capture-tools"]], "tool_blockers": [],
                       "rollback_ready": True, "release_certification_sha256": release_sha256},
        "evidence": [
            {"path": "evidence/check-egress-spec.json", "role": "check_egress_spec"},
            {"path": "evidence/release-certification.json", "role": "release_certification"},
            {"path": "evidence/rollback.json", "role": "rollback"},
            {"path": "evidence/test-capture-tools.json", "role": "test_capture_tools"},
        ],
    })
    return root


def _closeout(root: Path) -> None:
    """VC-0 收口对 P0 收据的校验（重放、绑定当前升级与发布认证、角色闭合，以及 E3-03 的 v2 重验）。"""

    closeout._validate_p0_gate(
        root, root / "p0-receipt.json",
        {"campaign_purpose": SUBJECT["campaign_purpose"], "baseline_version": SUBJECT["baseline_version"],
         "target_version": SUBJECT["target_version"]},
        {"upgrade_id": SUBJECT["upgrade_id"]}, root / "evidence" / "release-certification.json")


class _Fixture:
    """合成记录库与一份 P0 证据根：run1 全部本次执行；run2 承接 run1 的两个单元、其余本次执行，两份证据按 run2 的
    清单写成 v2。各个变异参数对应一条重验规则。"""

    RUN1, RUN2 = "20261002t000000z-1-aaaaaa", "20261002t020000z-2-bbbbbb"
    TIME1, TIME2 = "2026-10-02T00:00:00Z", "2026-10-02T02:00:00Z"
    TESTS = {
        "test_alpha": {"test_alpha.AlphaTests.test_a": {"outcome": "passed"},
                       "test_alpha.AlphaTests.test_skipped": {"outcome": "skipped", "reason": "示例跳过"}},
        "test_beta": {"test_beta.BetaTests.test_b": {"outcome": "passed"}},
    }
    CAPTURE_COMMAND = "capture:prerequisites"
    EGRESS = ("egress-spec:check-a", "egress-spec:check-b")
    INHERITED = ("test_alpha", "egress-spec:check-a")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = ur.RecordStore(root / "store")
        self.logs = 0
        self.environment: list[dict[str, Any]] = []
        self.executor = {"files": {"unit_executor.py": "e" * 64}, "sha256": "e" * 64}
        self.records: dict[str, dict[str, Any]] = {}

    def record(self, unit_id: str, run_id: str, completed: str, tests: dict[str, Any] | None = None) -> dict[str, Any]:
        self.logs += 1
        log = self.root / "logs" / f"{self.logs}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{unit_id} 的第 {self.logs} 份日志\n", encoding="utf-8")
        digest = ur.file_sha256(log)
        self.store.put_log(log, digest)
        record = ur.seal_record({
            "unit_id": unit_id, "unit_type": "test" if tests is not None else "command", "kind": "formal",
            "run": {"run_id": run_id, "mode": "full-set-pass", "out_dir": "/out"}, "executor": self.executor, "policy_sha256": "p" * 64,
            "environment": self.environment, "environment_sha256": ur.entries_sha256(self.environment),
            "spec": {"unit": unit_id}, "spec_sha256": ur.sha256_json({"unit": unit_id}), "inputs": [],
            "inputs_sha256": ur.entries_sha256([]), "inheritable": True, "not_inheritable_reason": None,
            "test_ids": sorted(tests or {}), "tests": tests, "passed": True, "exit_code": 0, "signal": None, "timed_out": False,
            "seconds": 1.0, "cpu_seconds": 0.5, "max_rss_mb": 30.0, "cores": 1, "memory_mb": 128, "orphans": [],
            "log": {"path": str(log), "sha256": digest, "bytes": log.stat().st_size}, "started_at_utc": completed,
            "completed_at_utc": completed,
        })
        self.store.put_record(record)
        return record

    def entry(self, record: dict[str, Any], disposition: str = "executed") -> dict[str, Any]:
        unit_id = record["unit_id"]
        test = record["unit_type"] == "test"
        entry = {"unit_id": unit_id, "unit_type": record["unit_type"],
                 "gates": ["test-capture-tools"] if test or unit_id == self.CAPTURE_COMMAND else ["check-egress-spec"],
                 "test_group": "capture-tools" if test else None, "disposition": disposition,
                 "record_sha256": record["record_sha256"], "record_path": str(self.store.record_path(unit_id, record["record_sha256"])),
                 "passed": True, "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"], "inheritable": True}
        if disposition == "inherited":
            entry["basis"] = {"run_id": record["run"]["run_id"], "completed_at_utc": record["completed_at_utc"]}
        else:
            entry["reasons"] = ["记录库里没有这个单元的记录"]
        return entry

    def manifest(self, run_id: str, mode: str, entries: list[dict[str, Any]], decided: str, test_ids: list[str],
                 *, publish: bool = True) -> dict[str, Any]:
        manifest = ur.build_manifest(
            run_id=run_id, mode=mode, inheritance_max_age_hours=168.0, record_store=str(self.store.root), out_dir="/out",
            decided_at_utc=decided, completed_at_utc=decided, policy_sha256="p" * 64, environment=self.environment,
            environment_sha256=ur.entries_sha256(self.environment), executor=self.executor,
            planned_units=[entry["unit_id"] for entry in entries], units=entries, diagnostic=[],
            test_groups={"capture-tools": sorted(test_ids)},
            counts={"planned": len(entries), "executed": sum(1 for e in entries if e["disposition"] == "executed"),
                    "inherited": sum(1 for e in entries if e["disposition"] == "inherited"), "inherited_tests": 0})
        if publish:
            self.store.put_manifest(manifest)
        return manifest

    def build(self, *, extra_egress: bool = False, missing_test_id: bool = False, duplicate_test_id: bool = False,
              stale: bool = False, publish: bool = True, egress_v1: bool = False, identity: dict[str, str] | None = None,
              passed_offset: int = 0) -> Path:
        time1 = "2026-09-22T00:00:00Z" if stale else self.TIME1
        units = (self.CAPTURE_COMMAND, *self.TESTS, *self.EGRESS)
        all_ids = sorted(test_id for tests in self.TESTS.values() for test_id in tests)
        run1 = {unit_id: self.record(unit_id, self.RUN1, time1, self.TESTS.get(unit_id)) for unit_id in units}
        self.manifest(self.RUN1, "re-execute", [self.entry(record) for record in run1.values()], time1, all_ids)
        entries = []
        for unit_id in units:
            if unit_id in self.INHERITED:
                record, disposition = run1[unit_id], "inherited"
            else:
                tests = self.TESTS.get(unit_id)
                if duplicate_test_id and unit_id == "test_beta":
                    tests = {**tests, "test_alpha.AlphaTests.test_a": {"outcome": "passed"}}
                record, disposition = self.record(unit_id, self.RUN2, self.TIME2, tests), "executed"
            self.records[unit_id] = record
            entries.append(self.entry(record, disposition))
        if extra_egress:
            entries.append(self.entry(self.record("egress-spec:check-c", self.RUN2, self.TIME2)))
        test_ids = all_ids + (["test_gamma.GammaTests.test_missing"] if missing_test_id else [])
        manifest = self.manifest(self.RUN2, "full-set-pass", entries, self.TIME2, test_ids, publish=publish)
        common = {"unit_manifest": {"run_id": self.RUN2, "manifest_sha256": manifest["manifest_sha256"], "mode": "full-set-pass"},
                  "record_store": str(self.store.root), "tool_identity": dict(IDENTITY)}
        base = {"working_directory": "/tree", "git_commit": "c" * 40, "status": "passed", "exit_code": 0, "failed": 0,
                "unexpected_skip": 0, "elapsed_seconds": 60.0, "raw_errors": [], "temporary_asset_inventory": [],
                "executed_as": "合成", "executor_summary": "/out/summary.json"}
        capture = {"schema_version": receipts.P0_GATE_EVIDENCE_V2_SCHEMA, "gate_id": "test-capture-tools",
                   "command": ["make", "test-capture-tools"], **base, "passed": 2, "approved_skip": 1, **common,
                   "units": [self.CAPTURE_COMMAND], "test_group": "capture-tools", "unit_counts": {"executed": 2, "inherited": 1},
                   "expected_tests": 3, "reported_tests": 3,
                   "skipped": [{"test_id": "test_alpha.AlphaTests.test_skipped", "reason": "示例跳过"}]}
        egress = {"schema_version": receipts.P0_GATE_EVIDENCE_V2_SCHEMA, "gate_id": "check-egress-spec",
                  "command": ["make", "check-egress-spec"], **base, "passed": 2, "approved_skip": 0, **common,
                  "units": list(self.EGRESS), "test_group": None, "unit_counts": {"executed": 1, "inherited": 1},
                  "checks": [{"target": unit_id.removeprefix("egress-spec:"), "passed": True, "exit_code": 0, "seconds": 1.0,
                              "log": "/logs/x.log"} for unit_id in self.EGRESS]}
        if egress_v1:
            egress = {key: value for key, value in egress.items() if key not in common and key not in {"units", "test_group", "unit_counts"}}
            egress["schema_version"] = eg.P0_EVIDENCE_SCHEMA
        staged = self.root / "exported"
        capture_path = _write_json(staged / "test-capture-tools.json", capture)
        egress_path = _write_json(staged / "check-egress-spec.json", egress)
        return _assemble_p0(self.root / "p0", capture_path, egress_path, identity or dict(IDENTITY), passed_offset=passed_offset)


class P0ManifestEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.cases = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self) -> _Fixture:
        self.cases += 1
        return _Fixture(self.base / f"case-{self.cases}")

    def test_v2_evidence_is_reverified_record_by_record_and_replay_does_not_read_the_store(self) -> None:
        fixture = self._fixture()
        root = fixture.build()
        receipt = receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        self.assertEqual((receipt["kind"], receipt["status"]), ("p0_gate", "passed"))
        replayed = receipts.replay(root, "p0-receipt.json")
        result = receipts.verify_p0_manifest_evidence(root, replayed)
        self.assertEqual((result["run_id"], result["mode"]), (fixture.RUN2, "full-set-pass"))
        self.assertEqual(result["gates"]["test-capture-tools"],
                         {"gate_id": "test-capture-tools", "units": 3, "executed": 2, "inherited": 1, "passed": 2, "approved_skip": 1})
        self.assertEqual(result["gates"]["check-egress-spec"],
                         {"gate_id": "check-egress-spec", "units": 2, "executed": 1, "inherited": 1, "passed": 2, "approved_skip": 0})
        manifest = json.loads(fixture.store.manifest_path(fixture.RUN2).read_text(encoding="utf-8"))
        self.assertEqual(ur.verify_manifest(manifest, store=fixture.store), [], "合成清单与执行器的清单自检口径一致")
        _closeout(root)
        # 重放只核对字节绑定：记录库清理之后照常重放；收口的重验拒绝。
        shutil.rmtree(fixture.store.root)
        receipts.replay(root, "p0-receipt.json")
        with self.assertRaisesRegex(receipts.VCReceiptError, "记录库"):
            receipts.verify_p0_manifest_evidence(root, replayed)
        with self.assertRaisesRegex(closeout.VC0CloseoutError, "离线门禁证据重验失败"):
            _closeout(root)

    def test_unclosed_manifest_and_missing_or_duplicated_test_ids_are_rejected(self) -> None:
        cases = {
            "不闭合": {"extra_egress": True},
            "缺 1、多 0、重复 0": {"missing_test_id": True},
            "缺 0、多 0、重复 1": {"duplicate_test_id": True},
        }
        for pattern, mutation in cases.items():
            with self.subTest(pattern):
                root = self._fixture().build(**mutation)
                with self.assertRaisesRegex(receipts.VCReceiptError, pattern):
                    receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
                self.assertFalse((root / "p0-receipt.json").exists())

    def test_identity_counts_shape_and_publication_mismatches_are_rejected(self) -> None:
        cases = {
            "工具五摘要与发布认证登记的身份不一致": {"identity": dict(IDENTITY, control_sha256="9" * 64)},
            "P0 断言里 test-capture-tools 的通过数": {"passed_offset": 1},
            "必须同一形状": {"egress_v1": True},
            "没有发布到记录库": {"publish": False},
            "承接期限": {"stale": True},
        }
        for pattern, mutation in cases.items():
            with self.subTest(pattern):
                root = self._fixture().build(**mutation)
                with self.assertRaisesRegex(receipts.VCReceiptError, pattern):
                    receipts.finalize(root, "p0-facts.json", "p0-receipt.json")

    def test_tampered_record_or_missing_log_is_rejected(self) -> None:
        fixture = self._fixture()
        root = fixture.build()
        record = fixture.records["test_beta"]
        path = fixture.store.record_path("test_beta", record["record_sha256"])
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["seconds"] = 99.0
        path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(receipts.VCReceiptError, "test_beta：执行记录自摘要不符"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")

        fixture = self._fixture()
        root = fixture.build()
        fixture.store.log_path(fixture.records["egress-spec:check-b"]["log"]["sha256"]).unlink()
        with self.assertRaisesRegex(receipts.VCReceiptError, "egress-spec:check-b：日志不在记录库里"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")

    def test_v1_and_handwritten_evidence_stay_bound_by_bytes_only(self) -> None:
        """v1（make 命令的一次运行）与手写日志证据不读内容：没有记录库照常签发，收口照常通过。"""

        for label in ("v1", "手写日志"):
            with self.subTest(label):
                fixture = self._fixture()
                staged = fixture.root / "exported"
                staged.mkdir(parents=True)
                paths = {}
                for name in ("test-capture-tools", "check-egress-spec"):
                    if label == "v1":
                        paths[name] = _write_json(staged / f"{name}.json", {"schema_version": eg.P0_EVIDENCE_SCHEMA, "gate_id": name,
                                                                              "passed": 3, "approved_skip": 0})
                    else:
                        paths[name] = staged / f"{name}.log"
                        paths[name].write_text("Ran 3 tests\nOK\n", encoding="utf-8")
                root = _assemble_p0(fixture.root / "p0", paths["test-capture-tools"], paths["check-egress-spec"], dict(IDENTITY),
                                    declared=(3, 0))
                self.assertFalse(fixture.store.root.exists(), "没有记录库")
                receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
                self.assertIsNone(receipts.verify_p0_manifest_evidence(root, receipts.replay(root, "p0-receipt.json")))
                _closeout(root)


class P0ManifestEvidenceEndToEndTests(unittest.TestCase):
    def test_inherited_entry_gates_run_exports_v2_evidence_that_signs_and_passes_closeout(self) -> None:
        """真跑：两个门禁项（测试组＋前置检查、两个子检查）交给执行器 run-gates，第一次重新执行全集、第二次全集通过且
        全部承接；入口门禁导出分别得到 v1 与 v2 证据，照编排器的方式签出两种 P0 收据，都通过 VC-0 收口的 P0 校验。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = base / "repo"
            tests = repo / "tools" / "official_client_capture" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_p0_alpha.py").write_text(
                "import unittest\nclass AlphaTests(unittest.TestCase):\n    def test_a(self): pass\n"
                "    @unittest.skip('示例跳过')\n    def test_skipped(self): pass\n", encoding="utf-8")
            (tests / "test_p0_beta.py").write_text(
                "import unittest\nclass BetaTests(unittest.TestCase):\n    def test_b(self): pass\n", encoding="utf-8")
            git = ["git", "-C", str(repo), "-c", "user.name=e3", "-c", "user.email=e3@example.invalid", "-c", "commit.gpgsign=false"]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "P0 门禁的小测试组"], check=True)

            def unit(unit_id: str) -> dict[str, Any]:
                return {"unit_id": unit_id, "argv": ["true"], "cwd": str(repo), "cores": 1, "memory_mb": 128, "timeout_seconds": 60,
                        "weight": 1.0, "inputs": eg.command_inputs()}

            gates = _write_json(base / "gates.json", {
                "schema_version": eg.GATES_SCHEMA, "profile": "preflight",
                "test_groups": [{"group_id": eg.CAPTURE_GROUP, "start": "tools/official_client_capture/tests", "pattern": "test_*.py",
                                 "env": {}, "launcher": []}],
                "units": [unit("capture:prerequisites"), unit("egress-spec:check-a"), unit("egress-spec:check-b")],
                "gates": [{"gate_id": "test-capture-tools", "units": ["capture:prerequisites"], "test_groups": [eg.CAPTURE_GROUP]},
                          {"gate_id": "check-egress-spec", "units": ["egress-spec:check-a", "egress-spec:check-b"]}],
                "scheduling": eg.scheduling_table(),
            })
            config = _write_json(base / "config.json", {
                "schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 3, "default_quota": {"cores": 1, "memory_mb": 1024},
                "quotas": {}, "splits": {}, "exclusive": [], "unit_timeout_seconds": 300, "orphan_grace_seconds": 1})
            environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
            environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)})
            store = base / "store"
            roots = {}
            for run, mode in ((1, "re-execute"), (2, "full-set-pass")):
                out = base / f"out-{run}"
                completed = subprocess.run(
                    [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(gates), "--config", str(config),
                     "--weights", str(base / "none.json"), "--durations", str(base / "none.json"), "--parallel", "3", "--cores", "4",
                     "--state-dir", str(base / "state"), "--out-dir", str(out), "--shared-caches", "off", "--record-store", str(store),
                     "--mode", mode], cwd=repo, capture_output=True, text=True, timeout=600, env=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
                entry = eg.export_records(gates, out / "summary.json", base / f"export-{run}", source={"commit": "c" * 40, "tree_head": "c" * 40},
                                          subject="p0-e2e", round_id="r1", tree=str(repo), isolation="none", host="test",
                                          architecture="test", tool_identity=lambda tree: dict(IDENTITY))
                self.assertEqual(entry["p0_evidence_withheld"], {})
                self.assertEqual(entry["p0_evidence_schema"], eg.P0_EVIDENCE_SCHEMA if run == 1 else eg.P0_EVIDENCE_SCHEMA_V2)
                counts = entry["inheritance"]
                self.assertEqual((counts["executed"], counts["inherited"]), (5, 0) if run == 1 else (0, 5))
                root = _assemble_p0(base / f"p0-{run}", Path(entry["p0_evidence"]["test-capture-tools"]),
                                    Path(entry["p0_evidence"]["check-egress-spec"]), dict(IDENTITY))
                receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
                _closeout(root)
                roots[run] = root
            capture = json.loads((roots[2] / "evidence" / "test-capture-tools.json").read_text(encoding="utf-8"))
            self.assertEqual((capture["passed"], capture["approved_skip"], capture["unit_counts"]), (2, 1, {"executed": 0, "inherited": 3}))
            result = receipts.verify_p0_manifest_evidence(roots[2], receipts.replay(roots[2], "p0-receipt.json"))
            self.assertEqual({gate_id: (row["executed"], row["inherited"]) for gate_id, row in result["gates"].items()},
                             {"test-capture-tools": (0, 3), "check-egress-spec": (0, 2)})
            self.assertIsNone(receipts.verify_p0_manifest_evidence(roots[1], receipts.replay(roots[1], "p0-receipt.json")))
            # 记录库里少了一份日志：v2 收据照常重放，收口重验拒绝；v1 收据不受影响。
            next((store / "logs").iterdir()).unlink()
            receipts.replay(roots[2], "p0-receipt.json")
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "日志不在记录库里"):
                _closeout(roots[2])
            _closeout(roots[1])


if __name__ == "__main__":
    unittest.main()
