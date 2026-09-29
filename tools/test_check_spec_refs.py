#!/usr/bin/env python3
"""验证规格表源码引用门禁的正向与负向行为。"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_spec_refs.py"
SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
DEPENDENCIES = ROOT / "tools" / "spec_source_deps"


class SpecRefGateTests(unittest.TestCase):
    """负例都在指南现行原文上做一次替换再跑门禁，替换目标必须逐字取自当前第二部分。

    指南改写源码字段后要同步这里的目标文字，否则 replace_once 先失败，负例根本没有执行。
    源码树默认取仓库内 local-analysis；隔离工作树可用与 Makefile 同名的环境变量
    CODEX_0_149_1_SOURCE_ROOT 指向另一处只读 0.149.1 源码树。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec_text = SPEC.read_text(encoding="utf-8")

    def replace_once(self, old: str, new: str) -> str:
        self.assertEqual(self.spec_text.count(old), 1, f"测试替换目标不唯一：{old}")
        return self.spec_text.replace(old, new, 1)

    def run_gate(
        self,
        spec_text: str,
        dependency_manifest: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="spec-ref-gate-") as temp_dir:
            spec_path = pathlib.Path(temp_dir) / "spec.md"
            spec_path.write_text(spec_text, encoding="utf-8")
            command = [
                sys.executable,
                str(CHECKER),
                "--spec",
                str(spec_path),
                "--symbol",
                "--cfg-test",
            ]
            if dependency_manifest is not None:
                command.extend(["--dependency-manifest", str(dependency_manifest)])
            source_root = os.environ.get("CODEX_0_149_1_SOURCE_ROOT")
            if source_root:
                command.extend([
                    "--source-root",
                    source_root,
                    "--cargo-lock",
                    str(pathlib.Path(source_root) / "codex-rs" / "Cargo.lock"),
                ])
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            return subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def assert_failed_with(
        self,
        result: subprocess.CompletedProcess[str],
        expected: str,
    ) -> None:
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(expected, output)

    def test_current_spec_passes(self) -> None:
        result = self.run_gate(self.spec_text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wrong_line_fails_rule_anchor(self) -> None:
        mutated = self.replace_once(
            "- **源码**：[L1] `codex-api/src/common.rs:259`、`core/src/client.rs:929`。",
            "- **源码**：[L1] `codex-api/src/common.rs:260`、`core/src/client.rs:929`。",
        )
        result = self.run_gate(mutated)
        self.assert_failed_with(result, "SPEC-BODY-005 的源码引用与锚点清单不一致")

    def test_bare_filename_ambiguity_fails(self) -> None:
        # SPEC-PROTO-002 的源码字段；client.rs 在 0.149.1 源码树中有多个同名文件。
        mutated = self.replace_once(
            "`core/src/client.rs:524`、`core/src/client.rs:955`",
            "`client.rs:524`、`core/src/client.rs:955`",
        )
        result = self.run_gate(mutated)
        self.assert_failed_with(result, "裸文件名有")
        self.assert_failed_with(result, "个候选")

    def test_cfg_test_reference_fails(self) -> None:
        # 0.149.1 的 responses_websocket.rs 在第 908-1227 行是 #[cfg(test)] 模块，第 920 行落在其中。
        mutated = self.replace_once(
            "- **源码**：[L1] `codex-api/src/common.rs:259`、`core/src/client.rs:929`。",
            "- **源码**：[L1] `codex-api/src/endpoint/responses_websocket.rs:920`、`core/src/client.rs:929`。",
        )
        result = self.run_gate(mutated)
        self.assert_failed_with(result, "测试代码引用")
        self.assert_failed_with(result, "responses_websocket.rs:920")

    def test_l2_without_exact_line_fails(self) -> None:
        mutated = self.replace_once(
            "- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h1/role.rs:1572-1578`\n"
            "  的 `write_headers` 默认分支；官方未启用保留大小写选项。",
            "- **源码**：[L2] hyper 1.8.1 的 `write_headers` 默认分支；"
            "官方未启用保留大小写选项。",
        )
        result = self.run_gate(mutated)
        self.assert_failed_with(result, "L1/L2 无精确行号")
        self.assert_failed_with(result, "SPEC-H1-001")

    def test_dependency_snapshot_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spec-dependency-gate-") as temp_dir:
            copied = pathlib.Path(temp_dir) / "spec_source_deps"
            shutil.copytree(DEPENDENCIES, copied)
            source = copied / "hyper-1.8.1" / "src" / "proto" / "h1" / "role.rs"
            source.write_text(
                source.read_text(encoding="utf-8") + "\n// 篡改测试\n",
                encoding="utf-8",
            )
            result = self.run_gate(self.spec_text, copied / "manifest.json")
        self.assert_failed_with(result, "L2 快照哈希不一致")


if __name__ == "__main__":
    unittest.main()
