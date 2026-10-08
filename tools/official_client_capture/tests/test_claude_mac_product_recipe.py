"""离线启动计划回归；模板只在fixture作用域存在，不操作真实产品／账号。"""

from __future__ import annotations

import copy
import hashlib
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_recipe as r
from tools.official_client_capture.tests import claude_client_receipt_fixture as f


class ProductRecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name).resolve()
        self.attempt = self.outer / "staging/data/evidence/campaigns/campaign-1/candidates/candidate-1/attempts/attempt-1"
        values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        f.write(self.attempt / "attempt.json", values)
        f.write(self.attempt.parents[3] / "campaign.json", {"campaign_id": "campaign-1", "client": "claude"})
        self.task_path, self.bundle, _capture = f.material(self.attempt, "claude-vscode", values, scope="regression_fixture")
        self.task = c.read_json(self.task_path)
        self.recipe = c.read_json(self.bundle / "isolation-recipe.json")
        self.template = r._ReviewedLaunchTemplate(
            recipe_id=self.recipe["recipe_id"], client_id="claude-vscode", source_scope="regression_fixture",
            artifact_identity_sha256=r._artifact_identity(self.task),
            official_support_sha256=self.recipe["official_support"]["sha256"], installation_mode="standard",
            argv_tokens=("{executable}", "--user-data-dir", "{user_data}", "--extensions-dir", "{extensions}", "--new-window"),
            env_names=("CLAUDE_CONFIG_DIR",), private_root_names=("user_data", "extensions", "config"))
        executable = next(row["content"]["path"] for row in self.task["client_artifacts"] if row["role"] == "application_executable")
        session = Path(self.recipe["private_session_root"])
        roots = {name: str(session / name) for name in self.template.private_root_names}
        self.plan = {"client_id": self.task["client_id"], "task_sha256": self.task["task_sha256"],
                     "recipe_sha256": self.task["execution"]["isolation_recipe"]["sha256"], "executable": executable,
                     "argv": [executable, "--user-data-dir", roots["user_data"], "--extensions-dir", roots["extensions"], "--new-window"],
                     "env_names": ["CLAUDE_CONFIG_DIR"], "private_roots": roots, "installation_mode": "standard",
                     "official_support_bindings": [self.recipe["official_support"]]}

    def verify(self, *, task=None, plan=None):
        with mock.patch.object(r, "_REVIEWED_TEMPLATES", (self.template,)):
            return r.validate_private_launch_plan(task or self.task, plan or self.plan, bundle_root=self.bundle)

    def freeze_task(self):
        self.task["task_sha256"] = c.digest({key: value for key, value in self.task.items() if key != "task_sha256"})
        self.plan["task_sha256"] = self.task["task_sha256"]

    def rewrite_recipe(self):
        f.write(self.bundle / "isolation-recipe.json", self.recipe)
        self.task["execution"]["isolation_recipe"] = c.binding(self.bundle / "isolation-recipe.json", self.bundle)
        self.plan["recipe_sha256"] = self.task["execution"]["isolation_recipe"]["sha256"]
        self.freeze_task()

    def test_fixture_plan_validates_but_never_grants_recipe_or_launch_authority(self):
        result = self.verify()
        self.assertEqual(result["plan"], self.plan)
        self.assertEqual(result["recipe"], self.recipe)
        self.assertIs(result["template_matched"], True)
        self.assertEqual(result["source_scope"], "readonly_diagnostic")
        for field in ("registration_verified", "formal_ready", "launch_allowed"):
            self.assertIs(result[field], False)
        result["plan"]["argv"].append("--changed")
        result["recipe"]["state_paths"].append("/changed")
        self.assertNotIn("--changed", self.plan["argv"])
        self.assertNotIn("/changed", self.recipe["state_paths"])

    def test_production_template_set_empty_rejects_even_well_shaped_candidate(self):
        self.assertEqual(r._REVIEWED_TEMPLATES, ())
        with self.assertRaisesRegex(r.ProductRecipeUnavailable, "没有唯一已审核模板"):
            r.validate_private_launch_plan(self.task, self.plan, bundle_root=self.bundle)
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def test_extra_plan_fields_handlers_shell_or_permissions_are_rejected(self):
        for field, value in (("handler", "module.function"), ("shell", "echo ok"), ("launch_allowed", True),
                             ("env", {"TOKEN": "不得消费"}), ("approved", True)):
            with self.subTest(field=field), self.assertRaises(r.ProductRecipeError):
                self.verify(plan={**self.plan, field: value})

    def test_additional_argv_shell_and_non_application_executable_are_rejected(self):
        for changed in ({**self.plan, "argv": self.plan["argv"] + ["--handler", "/arbitrary"]},
                        {**self.plan, "argv": "echo ok; launch"},
                        {**self.plan, "executable": "/bin/sh"},
                        {**self.plan, "executable": "/usr/local/bin/claude"}):
            with self.subTest(changed=changed), self.assertRaises(r.ProductRecipeError):
                self.verify(plan=changed)

    def test_environment_values_and_user_expanded_whitelist_are_rejected(self):
        for names in (["CLAUDE_CONFIG_DIR", "HOME"], ["CLAUDE_CONFIG_DIR=/secret"],
                      {"CLAUDE_CONFIG_DIR": "/secret"}, []):
            with self.subTest(names=names), self.assertRaises(r.ProductRecipeError):
                self.verify(plan={**self.plan, "env_names": names})

    def test_portable_or_unfrozen_installation_mode_is_rejected(self):
        for mode in ("portable", "unknown", True):
            with self.subTest(mode=mode), self.assertRaisesRegex(r.ProductRecipeError, "安装模式"):
                self.verify(plan={**self.plan, "installation_mode": mode})

    def test_task_recipe_and_client_bindings_cannot_be_substituted(self):
        for key, value in (("task_sha256", "f" * 64), ("recipe_sha256", "f" * 64), ("client_id", "claude-desktop")):
            with self.subTest(key=key), self.assertRaises(r.ProductRecipeError):
                self.verify(plan={**self.plan, key: value})

    def test_wrong_product_version_cannot_use_reviewed_template(self):
        task = copy.deepcopy(self.task)
        next(row for row in task["client_artifacts"] if row["role"] == "extension_package")["version"]["value"] = "different"
        task["task_sha256"] = c.digest({key: value for key, value in task.items() if key != "task_sha256"})
        with self.assertRaisesRegex(r.ProductRecipeError, "制品／版本"):
            self.verify(task=task, plan={**self.plan, "task_sha256": task["task_sha256"]})

    def test_private_roots_outside_session_or_with_extra_entries_are_rejected(self):
        for roots in ({**self.plan["private_roots"], "config": "/outside"},
                      {**self.plan["private_roots"], "arbitrary": "/extra"},
                      {name: value + "/../escape" for name, value in self.plan["private_roots"].items()}):
            with self.subTest(roots=roots), self.assertRaises(r.ProductRecipeError):
                self.verify(plan={**self.plan, "private_roots": roots})

    def test_symlink_in_private_session_path_is_rejected(self):
        session = Path(self.recipe["private_session_root"])
        session.symlink_to(self.outer, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.verify()

    def test_recipe_schema_fields_and_self_reported_flags_are_strict(self):
        for mutation in (lambda value: value.update(schema_version="claude-upgrade-client-isolation-recipe/v1"),
                         lambda value: value.update(launch_allowed=True),
                         lambda value: value.pop("state_paths")):
            original = copy.deepcopy(self.recipe)
            mutation(self.recipe)
            self.rewrite_recipe()
            with self.assertRaises(r.ProductRecipeError):
                self.verify()
            self.recipe = original
            self.rewrite_recipe()

    def test_recipe_cannot_change_endpoint_credential_or_request_policy(self):
        for field, value in (("endpoint", "https://elsewhere.invalid"), ("credential_ref", "private-ref:another"),
                             ("request_policy", {**self.recipe["request_policy"], "max_concurrency": 2})):
            original = copy.deepcopy(self.recipe)
            self.recipe[field] = value
            self.rewrite_recipe()
            with self.subTest(field=field), self.assertRaises(r.ProductRecipeError):
                self.verify()
            self.recipe = original
            self.rewrite_recipe()

    def test_missing_changed_or_nonexact_support_original_is_rejected(self):
        support = self.bundle / self.recipe["official_support"]["path"]
        original = support.read_bytes()
        support.write_bytes(original + b" ")
        with self.assertRaises(ValueError):
            self.verify()
        support.unlink()
        with self.assertRaises(ValueError):
            self.verify()

    def test_nonexact_support_binding_and_plan_binding_are_rejected(self):
        with self.assertRaises(r.ProductRecipeError):
            self.verify(plan={**self.plan, "official_support_bindings": []})
        self.recipe["official_support"]["unexpected"] = True
        self.rewrite_recipe()
        with self.assertRaises(ValueError):
            self.verify()

    def test_offline_validation_has_no_creation_spawn_ipc_provider_or_environment_reads(self):
        before = {str(path.relative_to(self.outer)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.outer.rglob("*") if path.is_file()}
        provider = mock.Mock()
        provider.verify_isolation_recipe.side_effect = AssertionError("离线校验不得启动provider")
        allowed = {self.bundle / "isolation-recipe.json", self.bundle / "isolation-support.json"}
        original = Path.read_bytes
        def guarded(path):
            self.assertIn(path, allowed, "只能读明确recipe／支持原件，不能读账号状态或凭据引用")
            return original(path)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {self.task["execution"]["recorder_id"]: provider}), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("不得创建目录")), \
                mock.patch.object(Path, "read_bytes", guarded), \
                mock.patch.object(os, "getenv", side_effect=AssertionError("不得读环境")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不得启动")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("不得连接")):
            self.verify()
        provider.verify_isolation_recipe.assert_not_called()
        after = {str(path.relative_to(self.outer)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.outer.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_fixture_never_qualifies_even_with_provider_and_reviewed_template(self):
        with mock.patch.object(r, "_REVIEWED_TEMPLATES", (self.template,)), self.assertRaisesRegex(r.ProductRecipeUnavailable, "fixture"):
            r.check_recipe_qualification(self.task, self.recipe, bundle_root=self.bundle, provider=mock.Mock())

    def test_formal_missing_or_arbitrary_provider_remains_unavailable(self):
        self.task["execution"]["source_scope"] = "formal"
        self.freeze_task()
        for provider in (None, True, {"available": True}, mock.Mock()):
            with self.subTest(provider=type(provider).__name__), mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), \
                    self.assertRaisesRegex(r.ProductRecipeUnavailable, "静态真实provider"):
                r.check_recipe_qualification(self.task, self.recipe, bundle_root=self.bundle, provider=provider)

    def test_formal_registered_object_without_formal_template_does_not_call_provider(self):
        self.task["execution"]["source_scope"] = "formal"
        self.freeze_task()
        provider = mock.Mock()
        with mock.patch.dict(c.SOURCE_ADAPTERS, {self.task["execution"]["recorder_id"]: provider}), \
                self.assertRaisesRegex(r.ProductRecipeUnavailable, "没有唯一已审核模板"):
            r.check_recipe_qualification(self.task, self.recipe, bundle_root=self.bundle, provider=provider)
        provider.verify_isolation_recipe.assert_not_called()

    def test_qualification_cannot_consume_rewritten_recipe_object(self):
        changed = {**self.recipe, "endpoint": "https://changed.invalid"}
        with self.assertRaises(r.ProductRecipeError):
            r.check_recipe_qualification(self.task, changed, bundle_root=self.bundle)


if __name__ == "__main__":
    unittest.main()
