"""Claude 正式入口的历史来源接线短验：干净环境生产清单、内容失效、来源缺失与持久日志坐标。

只生产正式单元清单与输入声明，不运行九条长链；本文件的通过不等同 pre-A3 或官方原生资格认证。
"""

from __future__ import annotations

import json
import copy
import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.ci import client_table, entry_gates, entry_steps, unit_records
from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture.tests import claude_recorded_budget_carry as budget_carry
from tools.official_client_capture.tests import claude_recorded_chain_source as recorded_source
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as full_driver

REPO = Path(__file__).resolve().parents[3]


def source_environment(root: Path) -> dict[str, str]:
    """最小的真实目录布局；每个来源有实际内容，relay 的同级补充目录也有单独内容。"""

    paths = (root / "relay/complete-v21-78fae770cbb5", root / "freeze", root / "static")
    values = {}
    for key, path in zip(client_table.spec("claude")["pre_a3_fixture_keys"], paths, strict=True):
        path.mkdir(parents=True, exist_ok=True)
        (path / "source.json").write_text('{"source":"original"}\n', encoding="utf-8")
        values[key] = str(path.resolve())
    supplement = root / "relay/thinking-display-supplement-v1-94f7d00d704f"
    supplement.mkdir(exist_ok=True)
    (supplement / "source.json").write_text('{"supplement":"original"}\n', encoding="utf-8")
    return values


def recorded_bridge_fixture(root: Path, sources: dict[str, str]) -> tuple[Path, dict, dict]:
    """接线接口夹具保留真实文件绑定和完整CARRY字段；原账重放另由原预算合同测试覆盖。"""

    original = root / "original"
    original.mkdir()
    bindings = []
    for index in range(45):
        path = original / f"event-{index:02d}.json"
        path.write_text(json.dumps({"sequence": index}), encoding="utf-8")
        bindings.append(recorded_source.binding(path))
    preserved = {}
    for name in ("batch19_commit", "store_campaign"):
        path = original / (name + ".json")
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
        preserved[name] = recorded_source.binding(path)
    snapshot = {"source_bindings": bindings, "project_head": {"sequence": 18, "sha256": "1" * 64},
                "timing_head": {"sequence": 22, "sha256": "2" * 64},
                "project_plan_sha256": "3" * 64, "timing_plan_sha256": "4" * 64}
    parent = root / "staging/scenarios"
    scenario = parent / "full79"
    intent = {"schema_version": full_driver.BUDGET_CARRY_INTENT_SCHEMA,
              "data_root": str(scenario / "tmp/world/staging/data"), "predecessor_data": str(original),
              "predecessor_campaign_id": "claude-recorded-formal", "preserved_bindings": preserved,
              **{"expected_" + key: snapshot[key] for key in ("project_head", "timing_head", "project_plan_sha256", "timing_plan_sha256")}}
    intent_path = root / "carry.json"
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    env = {**sources, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
           full_driver.BUDGET_CARRY_INTENT_ENV: str(intent_path),
           full_driver.BUDGET_CARRY_DIGEST_ENV: hashlib.sha256(intent_path.read_bytes()).hexdigest(),
           "CLAUDE_RECORDED_CHAIN_RECORD_DIR": str(root / "records")}
    unit = {"unit_id": entry_gates.RECORDED_FULL79_ID,
            "argv": ["/usr/bin/python3", "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification",
                     "run-scenario", "--client", "claude", "--name", "vc-chain.claude-full-validation-only",
                     "--staging-parent", str(parent), "--scenario-root", str(scenario)],
            "cwd": str(REPO), "env": env, "cores": 1, "memory_mb": 1024, "weight": 30.0,
            "timeout_seconds": certification.claude_recorded_time_budget("vc-chain.claude-full-validation-only")["unit_timeout_seconds"]}
    unit["inputs"] = entry_gates.pre_a3_inputs(
        certification.scenario_test_module("vc-chain.claude-full-validation-only", "claude").replace(".", "/") + ".py",
        entry_gates.pre_a3_fixture_resolved("claude", sources) +
        [{"category": "environment", "name": "file:" + binding["path"], "sha256": binding["sha256"],
          "detail": {"path": binding["path"]}} for binding in
         [recorded_source.binding(intent_path), *bindings, *preserved.values()]], "claude")
    spec = root / "full79-spec.json"
    spec.write_text(json.dumps(unit), encoding="utf-8")
    return spec, unit, snapshot


class ClaudePreA3SourceWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.sources = source_environment(self.root / "sources")
        self.data = self.root / "data"
        self.data.mkdir()
        self.parent = self.data / "staging/pre-a3-scenarios"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO)}
        self.units = {}
        for client in ("claude", "codex"):
            path = self.root / (client + "-units.json")
            plan = certification.plan_scenario_commands(self.parent, python=sys.executable, cwd=self.data, client=client)
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.units[client] = path

    def production_manifest(self, client: str, suffix: str) -> dict:
        output = self.root / (client + "-manifest-" + suffix + ".json")
        argv = [sys.executable, "-B", str(REPO / "tools/ci/entry_gates.py"), "plan", "--tree", str(REPO),
                "--profile", "pre-a3", "--launcher-json", "[]", "--pre-a3-units", str(self.units[client]),
                "--pre-a3-data-root", str(self.data), "--pre-a3-env", "PYTHONPATH=.", "--output", str(output)]
        if client == "claude":
            for key, value in self.sources.items():
                argv.extend(("--pre-a3-env", key + "=" + value))
        completed = subprocess.run(argv, cwd=REPO, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(output.read_text(encoding="utf-8"))

    def test_clean_formal_producer_binds_sources_and_retains_unit_logs(self) -> None:
        manifest = self.production_manifest("claude", "before")
        expected_names = {item.name for item in entry_steps.pre_a3_fixture_inputs("claude")}
        self.assertEqual(len(manifest["units"]), 9)
        directories = set()
        for unit in manifest["units"]:
            with self.subTest(unit=unit["unit_id"]):
                self.assertEqual({key: unit["env"][key] for key in self.sources}, self.sources)
                resolved = {item["name"]: item for item in unit["inputs"]["resolved"]}
                self.assertTrue(expected_names <= resolved.keys())
                self.assertTrue(all(resolved[name]["sha256"] != "missing" for name in expected_names))
                relay = resolved["tree:{CLAUDE_2_1_226_RELAY_FIXTURE}#parent_pycache"]
                self.assertEqual(relay["detail"]["files"], 2, "完整读集含同级补充取证")
                log_root = Path(unit["env"]["CLAUDE_RECORDED_CHAIN_RECORD_DIR"])
                self.assertTrue(log_root.is_relative_to(self.parent / "claude-chain-records"))
                directories.add(str(log_root))
        self.assertEqual(len(directories), 9, "并行单元的产物目录相互隔离")
        for step in ("entry-gates", "pre-a3"):
            self.assertTrue(expected_names <= {item.name for item in entry_steps.STEP_BY_ID_BY_CLIENT["claude"][step].inputs})
            self.assertTrue(expected_names.isdisjoint(item.name for item in entry_steps.STEP_BY_ID_BY_CLIENT["codex"][step].inputs))

    def test_each_source_content_invalidates_claude_and_codex_is_unchanged(self) -> None:
        original = self.production_manifest("claude", "before")
        codex = self.production_manifest("codex", "before")
        before = [unit_records.entries_sha256(unit["inputs"]["resolved"]) for unit in original["units"]]
        changed_paths = [*(Path(value) / "source.json" for value in self.sources.values()),
                         Path(self.sources["CLAUDE_2_1_226_RELAY_FIXTURE"]).parent /
                         "thinking-display-supplement-v1-94f7d00d704f/source.json"]
        for index, path in enumerate(changed_paths):
            old = path.read_bytes()
            path.write_bytes(old + b"\n")
            changed = self.production_manifest("claude", str(index))
            self.assertTrue(all(unit_records.entries_sha256(unit["inputs"]["resolved"]) != digest
                                for unit, digest in zip(changed["units"], before, strict=True)))
            self.assertEqual(self.production_manifest("codex", str(index)), codex)
            path.write_bytes(old)

    def test_missing_empty_symlink_and_undeclared_environment_fail_before_dispatch(self) -> None:
        key = "CLAUDE_2_1_226_RELAY_FIXTURE"
        empty = self.root / "empty"
        empty.mkdir()
        link = self.root / "linked-source"
        link.symlink_to(Path(self.sources[key]))
        bad = [{k: v for k, v in self.sources.items() if k != key},
               {**self.sources, key: str(self.root / "nonexistent")},
               {**self.sources, key: str(link)},
               {**self.sources, "CLAUDE_2_1_226_STATIC_FIXTURE": str(empty)},
               {**self.sources, "CLAUDE_FAULT_CHAIN_DIAGNOSTIC_SECONDS": "10"}]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                entry_gates.plan_gates(REPO, profile="pre-a3", launcher=[], pre_a3_units=self.units["claude"],
                                       pre_a3_env=values, platform="linux")

    def test_real_driver_source_loop_leaves_codex_arguments_unchanged(self) -> None:
        """执行正式脚本里的固定白名单循环，不凭源码替换或残差登记声称 Codex 行为守恒。"""

        text = (REPO / "tools/arm64_capture_driver/driver/entry-gates.sh").read_text(encoding="utf-8")
        start = text.index('    for name in ${CT_PRE_A3_FIXTURE_KEYS[@]+')
        end = text.index('    done', start) + len('    done')
        loop = text[start:end]
        output = shlex.quote(sys.executable) + ' -c \'import json,sys;print(json.dumps(sys.argv[1:]))\' "${PRE_A3_ARGS[@]}"\n'
        for client in ("codex", "claude"):
            keys = client_table.spec(client)["pre_a3_fixture_keys"]
            assignments = "\n".join(key + "=" + shlex.quote(value) for key, value in self.sources.items())
            script = ('set -eu\nPRE_A3_ARGS=(--pre-a3-env PYTHONPATH=.)\n' +
                      'CT_PRE_A3_FIXTURE_KEYS=(' + ' '.join(keys) + ')\n' + assignments + '\n' + loop + '\n' + output)
            completed = subprocess.run(["bash", "-c", script], env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            expected = ["--pre-a3-env", "PYTHONPATH=."]
            for key in keys:
                expected.extend(("--pre-a3-env", key + "=" + self.sources[key]))
            self.assertEqual(json.loads(completed.stdout), expected)

    def test_fixed_recorded_bridge_binds_all_sources_and_preserves_original_coordinates(self) -> None:
        spec, approved, snapshot = recorded_bridge_fixture(self.root, self.sources)
        digest = hashlib.sha256(spec.read_bytes()).hexdigest()
        with mock.patch.object(budget_carry, "_snapshot", return_value=snapshot):
            bridge = entry_gates.recorded_full79_bridge(spec, digest, client="claude", fixture_env=self.sources)
        self.assertEqual(len(bridge["resolved"]), 48)
        self.assertEqual(len(bridge["fixtures"]), 6)
        with self.assertRaisesRegex(ValueError, "显式提供冻结"):
            entry_gates.plan_gates(REPO, profile="pre-a3", launcher=[], pre_a3_units=self.units["claude"],
                                  pre_a3_env=self.sources, platform="linux", recorded_bridge=bridge)
        policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
        original_policy = copy.deepcopy(policy)
        manifest = entry_gates.plan_gates(REPO, profile="pre-a3", launcher=[], pre_a3_units=self.units["claude"],
                                         pre_a3_env=self.sources, platform="linux", recorded_bridge=bridge, audit_policy=policy)
        actual = next(unit for unit in manifest["units"] if unit["unit_id"] == entry_gates.RECORDED_FULL79_ID)
        self.assertEqual(actual["argv"], approved["argv"])
        for key in (full_driver.BUDGET_CARRY_INTENT_ENV, full_driver.BUDGET_CARRY_DIGEST_ENV, "CLAUDE_RECORDED_CHAIN_RECORD_DIR"):
            self.assertEqual(actual["env"][key], approved["env"][key])
        self.assertEqual(manifest["audit_policy"]["default"], "reexecute-only")
        self.assertEqual(policy, original_policy, "调用方的冻结策略原字节语义不改写")
        with self.assertRaisesRegex(ValueError, "不能登记为可承接"):
            entry_gates.plan_gates(REPO, profile="pre-a3", launcher=[], pre_a3_units=self.units["claude"],
                                  pre_a3_env=self.sources, platform="linux", recorded_bridge=bridge,
                                  audit_policy={"schema_version": "unit-audit-policy/v1", "default": "reexecute-only",
                                                "units": {entry_gates.RECORDED_FULL79_ID: {}}})
        self.assertFalse(Path(json.loads(Path(approved["env"][full_driver.BUDGET_CARRY_INTENT_ENV]).read_text())["data_root"]).exists(),
                         "准备接线不能初始化新世界")

    def test_recorded_bridge_rejects_sha_fixture_source_and_original_head_drift(self) -> None:
        spec, _, snapshot = recorded_bridge_fixture(self.root, self.sources)
        digest = hashlib.sha256(spec.read_bytes()).hexdigest()
        def consume() -> dict:
            return entry_gates.recorded_full79_bridge(spec, digest, client="claude", fixture_env=self.sources)
        with mock.patch.object(budget_carry, "_snapshot", return_value=snapshot):
            with self.assertRaises(ValueError):
                entry_gates.recorded_full79_bridge(spec, "f" * 64, client="claude", fixture_env=self.sources)
            with self.assertRaises(ValueError):
                entry_gates.recorded_full79_bridge(spec, digest, client="codex", fixture_env={})
            fixture = Path(next(iter(self.sources.values()))) / "source.json"
            original = fixture.read_bytes()
            fixture.write_bytes(original + b"\n")
            with self.assertRaisesRegex(ValueError, "漂移"):
                consume()
            fixture.write_bytes(original)
            budget = Path(snapshot["source_bindings"][0]["path"])
            original = budget.read_bytes()
            budget.write_bytes(original + b"\n")
            with self.assertRaises(recorded_source.replay.ReplayError):
                consume()
            budget.write_bytes(original)
            snapshot["project_head"] = {"sequence": 19, "sha256": "5" * 64}
            with self.assertRaisesRegex(ValueError, "锚点漂移"):
                consume()

    def test_recorded_bridge_requires_new_ledger_sources_to_be_frozen_without_dropping_old_files(self) -> None:
        spec, approved, snapshot = recorded_bridge_fixture(self.root, self.sources)
        digest = hashlib.sha256(spec.read_bytes()).hexdigest()
        extra = self.root / "original/new-extension.json"
        extra.write_text('{"extension":"real"}', encoding="utf-8")
        binding = recorded_source.binding(extra)
        snapshot["source_bindings"].append(binding)
        with mock.patch.object(budget_carry, "_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(ValueError, "来源全集"):
                entry_gates.recorded_full79_bridge(spec, digest, client="claude", fixture_env=self.sources)
            approved["inputs"]["resolved"].insert(-2, {"category": "environment", "name": "file:" + str(extra),
                                                      "sha256": binding["sha256"], "detail": {"path": str(extra)}})
            spec.write_text(json.dumps(approved), encoding="utf-8")
            bridge = entry_gates.recorded_full79_bridge(spec, hashlib.sha256(spec.read_bytes()).hexdigest(),
                                                      client="claude", fixture_env=self.sources)
        self.assertEqual(len(bridge["resolved"]), 49)
        self.assertEqual(len(bridge["unit"]["inputs"]["resolved"]), 55)

    def test_recorded_certification_reuse_rechecks_current_source_and_original_validator(self) -> None:
        spec, approved, snapshot = recorded_bridge_fixture(self.root, self.sources)
        with mock.patch.object(budget_carry, "_snapshot", return_value=snapshot):
            bridge = entry_gates.recorded_full79_bridge(spec, hashlib.sha256(spec.read_bytes()).hexdigest(),
                                                      client="claude", fixture_env=self.sources)
        current_source = self.root / "current-managed-source.py"
        current_source.write_text("source = 1\n", encoding="utf-8")
        def inputs(_repo, declaration, _cache):
            return [{"category": "managed", "name": "repo:current-managed-source.py",
                     "sha256": hashlib.sha256(current_source.read_bytes()).hexdigest()}, *declaration["resolved"]]
        saved = {}
        for name in certification.scenario_names("claude"):
            unit_id = certification.scenario_unit_prefix("claude") + name
            fixed = bridge["fixtures"] + (bridge["resolved"] if unit_id == entry_gates.RECORDED_FULL79_ID else [])
            saved[unit_id] = {"inputs": inputs(None, {"resolved": fixed}, None), "spec": copy.deepcopy(approved)}
        manifest = {"units": [{"unit_id": unit_id, "record_sha256": "1" * 64} for unit_id in saved]}
        class Store:
            def __init__(self, _root):
                pass
            def manifest_path(self, _run_id):
                return Path("manifest")
            def record_path(self, unit_id, _digest):
                return Path(unit_id)
            def read(self, path):
                return manifest if str(path) == "manifest" else saved[str(path)]
        cert_path = self.root / "real-certification.json"
        cert_path.write_text("{}", encoding="utf-8")
        payload = {"record_store": str(self.root), "unit_manifest": {"run_id": "original-run"}}
        records = entry_gates._unit_records_module()
        with mock.patch.object(certification, "verify_certification", return_value=payload) as validate, \
                mock.patch.object(certification, "_RecordStore", Store), \
                mock.patch.object(records.RepoIndex, "load", return_value=object()), \
                mock.patch.object(records, "declared_inputs", side_effect=inputs):
            entry_gates.verify_recorded_reuse(REPO, cert_path, bridge, [])
            validate.assert_called_with(cert_path, client="claude")
            current_source.write_text("source = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "当前输入已变化"):
                entry_gates.verify_recorded_reuse(REPO, cert_path, bridge, [])
        with mock.patch.object(certification, "verify_certification", side_effect=certification.CertificationError("身份不一致")):
            with self.assertRaises(certification.CertificationError):
                entry_gates.verify_recorded_reuse(REPO, cert_path, bridge, [])


if __name__ == "__main__":
    unittest.main()
