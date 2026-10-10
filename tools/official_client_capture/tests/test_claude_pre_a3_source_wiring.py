"""Claude 正式入口的历史来源接线短验：干净环境生产清单、内容失效、来源缺失与持久日志坐标。

只生产正式单元清单与输入声明，不运行九条长链；本文件的通过不等同 pre-A3 或官方原生资格认证。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.ci import client_table, entry_gates, entry_steps, unit_records
from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification

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


if __name__ == "__main__":
    unittest.main()
