"""当前回归输入的语义负例：不放宽身份核对、不绕过规格绑定、不改写正式原件。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests.claude_current_fixture_inputs import (
    IDENTITY_FIELDS, freeze_current_scenario_inputs, freeze_current_tool_inputs, scenario_input_files,
)
from tools.official_client_control.canonical import canonical_json_bytes
from tools.official_client_control.tests.fixtures import ManagedToolTree


class CurrentToolFixtureInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name).resolve()
        cls.tree = ManagedToolTree(root / "repo")
        cls.frozen = freeze_current_tool_inputs(cls.tree, root / "fixture-inputs")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.tree.restore()
        self.addCleanup(self.tree.restore)
        self.identity = json.loads(self.frozen.identity_bytes)

    def test_original_policy_bytes_and_complete_identity_replay(self) -> None:
        self.frozen.verify(self.tree, self.identity)
        self.assertEqual(set(self.identity), set(IDENTITY_FIELDS))
        record = json.loads(self.frozen.record_bytes)
        self.assertEqual(record["source_scope"], "regression_fixture")
        self.assertEqual(record["policy"]["sha256"], hashlib.sha256(self.frozen.policy_bytes).hexdigest())

    def test_each_of_seven_identity_fields_is_exactly_checked(self) -> None:
        for name in IDENTITY_FIELDS:
            with self.subTest(field=name):
                changed = deepcopy(self.identity)
                changed[name] = self.identity[name] + 1 if name == "policy_version" else ("codex" if name == "client" else "0" * 64)
                with self.assertRaisesRegex(ValueError, name):
                    self.frozen.verify(self.tree, changed)
        # 数值相等但类型不同也不能借 JSON 的弱相等冒充原策略版本。
        with self.assertRaisesRegex(ValueError, "policy_version"):
            self.frozen.check_identity({**self.identity, "policy_version": float(self.identity["policy_version"])})

    def test_missing_or_extra_identity_fields_are_rejected(self) -> None:
        for name in IDENTITY_FIELDS:
            with self.subTest(field=name):
                missing = {key: value for key, value in self.identity.items() if key != name}
                with self.assertRaisesRegex(ValueError, "字段不闭合"):
                    self.frozen.check_identity(missing)
        with self.assertRaisesRegex(ValueError, "字段不闭合"):
            self.frozen.check_identity({**self.identity, "extra": "not-authorized"})

    def test_policy_byte_drift_is_rejected(self) -> None:
        key = self.frozen.policy_relative.relative_to("tools").as_posix()
        self.tree.write(key, self.frozen.policy_bytes + b"\n")
        with self.assertRaisesRegex(ValueError, "策略原字节漂移"):
            self.frozen.verify(self.tree, self.identity)

    def test_managed_source_drift_is_rejected_before_identity_acceptance(self) -> None:
        key = "official_client_control/receipts.py"
        self.tree.write(key, self.tree.path(key).read_bytes() + "\n# 隔离副本的漂移负例。\n".encode())
        with self.assertRaisesRegex(ValueError, "完整受管文件清单漂移"):
            self.frozen.verify(self.tree, self.identity)


class CurrentScenarioFixtureInputTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source_root = plan.REPOSITORY_ROOT
        template = plan.scenario_manifest_path("2.1.285")
        # 复制真实输入到私有读根；只改路径根，正式规格验证器、参数默认值与规则仍使用原实现。
        self.repo = root / "repo"
        for path in scenario_input_files(template).values():
            if path.is_relative_to(source_root):
                destination = self.repo / path.relative_to(source_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        self.template = self.repo / template.relative_to(source_root)
        patcher = mock.patch.object(plan, "REPOSITORY_ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.template_bytes = self.template.read_bytes()
        payload = json.loads(self.template_bytes)
        self.guide = self.repo / payload["source_spec"]["path"]
        self.rules = json.loads((self.repo / payload["rule_manifest"]["path"]).read_bytes())["required_rules"]
        self.frozen = freeze_current_scenario_inputs(self.template, root / "fixture-inputs")

    def read_manifest(self, path: Path):
        return plan.read_scenario_manifest(path, expected_version="2.1.285", rules=self.rules)

    def test_new_fixture_changes_only_source_binding_and_keeps_original_template(self) -> None:
        original = json.loads(self.template_bytes)
        generated = self.read_manifest(self.frozen.manifest_path)
        expected = deepcopy(original)
        expected["source_spec"]["sha256"] = plan.guide_section_sha256(self.guide, "第二部分")
        self.assertEqual(generated, expected)
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.frozen.verify()
        record = json.loads(self.frozen.record_bytes)
        self.assertEqual(record["source_scope"], "regression_fixture")
        self.assertEqual(record["allowed_changed_field"], "source_spec.sha256")
        self.assertEqual(set(record["inputs"]), set(scenario_input_files(self.template)))
        for name, path, raw in self.frozen.sources:
            self.assertEqual(record["inputs"][name]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(record["inputs"][name]["bytes"], len(raw))
            self.assertEqual(path.read_bytes(), raw)

    def test_historical_template_still_fails_the_default_strict_source_check(self) -> None:
        original = json.loads(self.template_bytes)
        self.assertNotEqual(original["source_spec"]["sha256"], plan.guide_section_sha256(self.guide, "第二部分"))
        with self.assertRaisesRegex(ConfigurationError, "规格章节摘要不一致"):
            self.read_manifest(self.template)
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.read_manifest(self.frozen.manifest_path)

    def test_later_guide_change_is_rejected_by_the_real_default_reader(self) -> None:
        text = self.guide.read_text()
        heading = plan.GUIDE_SECTION_HEADINGS["第二部分"]
        self.guide.write_text(text.replace(heading, heading + "\n\n冻结之后的私有负例。", 1))
        with self.assertRaisesRegex(ConfigurationError, "规格章节摘要不一致"):
            self.read_manifest(self.frozen.manifest_path)
        with self.assertRaisesRegex(ValueError, "来源输入漂移：source_spec"):
            self.frozen.verify()

    def test_declared_input_drift_is_rejected(self) -> None:
        sources = {name: (path, raw) for name, path, raw in self.frozen.sources}
        for name in ("template", "rule_manifest", "baseline_rule_manifest", "schema", "probe_catalog", "version_facts"):
            path, raw = sources[name]
            with self.subTest(input=name):
                path.write_bytes(raw + b"\n")
                try:
                    with self.assertRaisesRegex(ValueError, "来源输入漂移：" + name):
                        self.frozen.verify()
                finally:
                    path.write_bytes(raw)

    def test_non_source_change_cannot_be_accepted_by_rehashing_the_record(self) -> None:
        changed = json.loads(self.frozen.manifest_bytes)
        changed["profile_id"] = "claude-code-regression-edited"
        raw = canonical_json_bytes(changed)
        self.frozen.manifest_path.write_bytes(raw)
        # 该改动结构合法，但超出本夹具唯一授权字段；重新计算记录的摘要也不能放行。
        self.read_manifest(self.frozen.manifest_path)
        record = json.loads(self.frozen.record_bytes)
        record["generated"].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        (self.frozen.directory / "inputs.json").write_bytes(canonical_json_bytes(record))
        with self.assertRaisesRegex(ValueError, "新场景文件漂移"):
            self.frozen.verify()

    def test_provenance_only_drift_is_rejected(self) -> None:
        record = json.loads(self.frozen.record_bytes)
        record["source_scope"] = "formal"
        (self.frozen.directory / "inputs.json").write_bytes(canonical_json_bytes(record))
        with self.assertRaisesRegex(ValueError, "场景输入记录漂移"):
            self.frozen.verify()

    def test_second_generation_cannot_overwrite_frozen_inputs(self) -> None:
        before = {path.relative_to(self.frozen.directory): path.read_bytes() for path in self.frozen.directory.rglob("*") if path.is_file()}
        with self.assertRaises(FileExistsError):
            freeze_current_scenario_inputs(self.template, self.frozen.directory)
        self.assertEqual({path.relative_to(self.frozen.directory): path.read_bytes() for path in self.frozen.directory.rglob("*") if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
