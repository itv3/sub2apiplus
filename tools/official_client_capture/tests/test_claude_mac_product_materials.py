"""公开产品物料与诊断计划回归：覆盖版本、读中漂移和正式资格关闭。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import socket
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_materials as m
from tools.official_client_capture import claude_mac_product_recipe as r
from tools.official_client_capture import claude_mac_client_source as source


def write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def plist(path: Path, identity: str, executable: str, version: str) -> None:
    write(path, plistlib.dumps({"CFBundleIdentifier": identity, "CFBundleExecutable": executable,
                              "CFBundleShortVersionString": version, "CFBundleVersion": version}))


def binary(path: Path, version: str = "") -> None:
    header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
    source = (f"\n// Version: {version}\n".encode()
              + b'function config(){let t=".claude.json";return join(process.env.CLAUDE_CONFIG_DIR||homedir(),t)}') if version else b""
    write(path, header + source)
    path.chmod(0o755)


def asar(path: Path, members: dict[str, bytes]) -> None:
    tree: dict = {"files": {}}
    payload, offset = [], 0
    for name, raw in sorted(members.items()):
        directory = tree
        parts = name.split("/")
        for part in parts[:-1]:
            directory = directory["files"].setdefault(part, {"files": {}})
        directory["files"][parts[-1]] = {"size": len(raw), "offset": str(offset)}
        payload.append(raw)
        offset += len(raw)
    raw = c.canonical(tree)
    padding = (4 - len(raw) % 4) % 4
    size = len(raw) + padding + 8
    write(path, struct.pack("<4I", 4, size, size - 4, len(raw)) + raw + b"\0" * padding + b"".join(payload))


class ProductMaterialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name).resolve()
        self.run = self.outer / "run"
        self.run.mkdir()

    def desktop(self, tag="desktop", *, pin_version="2.1.293", plist_version="2.1.293",
                binary_version="2.1.293", verified="b" * 64):
        app = self.outer / tag / "Claude.app"
        cli = self.outer / tag / "claude-code/2.1.293" / ("b" * 12)
        plist(app / "Contents/Info.plist", "com.anthropic.claudefordesktop", "Claude", "2.31226.0")
        binary(app / "Contents/MacOS/Claude")
        pin = {"version": pin_version, "manifest": {"version": pin_version, "commit": "commit-1",
                "platforms": {"darwin-arm64": {"checksum": "a" * 64, "bundle": {"checksum": "b" * 64}}}},
               "baseUrl": "https://downloads.claude.ai/frozen", "sdkWrapperVersion": "0.3.293-test"}
        asar(app / "Contents/Resources/app.asar", {
            "package.json": c.canonical({"name": "@ant/desktop", "version": "2.31226.0", "main": ".vite/build/index.pre.js"}),
            ".vite/build/index.pre.js": b'let e=process.env.CLAUDE_USER_DATA_DIR;app.setPath("userData",e);',
            ".vite/build/index.chunk.js": b"function pin(){return JSON.parse('" + c.canonical(pin)
                + b"')}function root(){return join(app.getPath(\"userData\"),\"claude-code\")}"
                + b";function unrelated(){return JSON.parse('{\"version\":\"commit-hash\",\"manifest\":{\"platforms\":{}}}')}",})
        plist(cli / "claude.app/Contents/Info.plist", "com.anthropic.claude-code", "claude", plist_version)
        binary(cli / "claude.app/Contents/MacOS/claude", binary_version)
        write(cli / ".verified", verified.encode())
        return {"application_root": app, "desktop_cli_root": cli}

    def vscode(self, tag="vscode", *, cli_version="2.1.295", extension_version="2.1.295"):
        app = self.outer / tag / "Visual Studio Code.app"
        extension = self.outer / tag / ("anthropic.claude-code-" + extension_version + "-darwin-arm64")
        plist(app / "Contents/Info.plist", "com.microsoft.VSCode", "Code", "1.141.0")
        binary(app / "Contents/MacOS/Code")
        binary(app / "Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)")
        write(app / "Contents/Resources/app/package.json", c.canonical({"name": "Code", "version": "1.141.0"}))
        write(app / "Contents/Resources/app/product.json", c.canonical({"applicationName": "code", "version": "1.141.0"}))
        write(app / "Contents/Resources/app/out/cli.js", b'"user-data-dir";"extensions-dir";"inspect-extensions"')
        write(extension / "package.json", c.canonical({"name": "claude-code", "publisher": "Anthropic",
            "version": extension_version, "main": "./extension.js", "activationEvents": ["onStartupFinished"],
            "contributes": {"configuration": {"properties": {"claudeCode.environmentVariables": {
                "type": "array", "items": {"required": ["name", "value"]}}}}}}))
        write(extension / "extension.js", b'exports.activate=()=>{};')
        binary(extension / "resources/native-binary/claude", cli_version)
        return {"application_root": app, "extension_root": extension}

    def freeze(self, client_id, roots, tag=""):
        output = self.run / (client_id + tag)
        result = m.freeze_product_materials(client_id, output_root=output, **roots)
        return output, result

    def task(self, root, material):
        cli = next(row for row in material["client_artifacts"] if row["role"] == "embedded_cli")
        deployments = {"mac": {"deployment_id": "fixture-mac", "tool_identity_sha256": "a" * 64},
                       "capture": {"deployment_id": "fixture-capture", "tool_identity_sha256": "b" * 64}}
        policy = {"methods": ["POST"], "model_paths": sorted(c.MODEL_ENDPOINTS), "retry_upper_bound": 0,
                  "max_concurrency": 1, "deadline_utc": "2026-10-10T02:00:00Z"}
        execution = {"endpoint": "http://candidate.invalid:8080", "credential_ref": "private-ref:unused",
                     "timeout_seconds": 300, "request_budget": 10, "recorder_id": "fixture-recorder",
                     "capture_id": "fixture-capture", "producer_run_id": "fixture-run", "source_scope": "regression_fixture",
                     "capture_root": str(self.run / "unused-capture"), "project_ledger": str(self.run / "unused-ledger"),
                     "source_deployments": deployments, "request_policy": policy}
        recipe = {"schema_version": "claude-upgrade-client-isolation-recipe/v2", "client_id": material["client_id"],
                  "deployment": deployments["mac"], "official_support": material["official_support"],
                  "endpoint": execution["endpoint"], "credential_ref": execution["credential_ref"],
                  "private_session_root": str(self.run / (material["client_id"] + "-private-session")),
                  "state_paths": [str(self.outer / "unused-original-state")], "recipe_id": "fixture-isolation",
                  "request_policy": policy}
        write(root / "isolation-recipe.json", c.canonical(recipe))
        execution["isolation_recipe"] = c.binding(root / "isolation-recipe.json", root)
        task = {"schema_version": c.TASK_SCHEMA, "client_id": material["client_id"],
                "campaign_id": "fixture-campaign", "candidate_id": "fixture-candidate", "attempt_id": "fixture-attempt",
                "run_nonce": "fixture-nonce", "candidate_revision": 1, "candidate": {key: "c" * 64 for key in
                    ("source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256")},
                "catalog": {"sha256": "d" * 64, "entry": {"client_id": material["client_id"] + "-fixture",
                    "product": cli["product"], "version": cli["version"]["value"], "platform": cli["platform"],
                    "binary_sha256": cli["content"]["sha256"]}}, "client_artifacts": material["client_artifacts"],
                "scenarios": [{"id": "fixture-smoke", "model": "fixture-model", "challenge": "fixture-challenge-12345",
                    "response_marker": "fixture-response", "minimum_requests": 1}], "scenario_reuse": [],
                "execution": execution, "issued_at_utc": "2026-10-10T01:00:00Z", "task_nonce": "fixture-task"}
        task["task_sha256"] = c.digest(task)
        return task

    def derive(self, client_id, roots):
        root, material = self.freeze(client_id, roots)
        task = self.task(root, material)
        return root, material, task, r.derive_readonly_private_launch_plan(
            task, bundle_root=root, materials_receipt=root / "product-materials.json")

    def test_desktop_roles_versions_package_checksum_and_raw_binary_are_distinct(self):
        root, material = self.freeze("claude-desktop", self.desktop())
        roles = {row["role"]: row for row in material["client_artifacts"]}
        self.assertEqual(set(roles), {"application", "application_executable", "embedded_cli"})
        self.assertEqual(material["versions"]["product"], "2.31226.0")
        self.assertEqual(material["versions"]["embedded_cli"], "2.1.293")
        self.assertNotEqual(roles["embedded_cli"]["content"]["sha256"], material["facts"]["desktop_pin"]["download_binary_sha256"])
        self.assertNotEqual(roles["embedded_cli"]["content"]["sha256"], material["facts"]["desktop_pin"]["download_bundle_sha256"])
        for role in roles.values():
            proof = c.read_json(c.bound_file(root, role["official_source"]))
            self.assertEqual(proof["content"], role["content"])
            self.assertIs(proof["formal_ready"], False)
        self.assertEqual(m.read_product_materials(root / "product-materials.json"), material)

    def test_vscode_freezes_six_roles_without_confusing_host_extension_and_cli(self):
        _root, material = self.freeze("claude-vscode", self.vscode())
        self.assertEqual({row["role"] for row in material["client_artifacts"]},
                         {"application", "application_executable", "extension_host", "extension_package", "extension_entry", "embedded_cli"})
        self.assertEqual(material["versions"], {"product": "1.141.0", "host": "1.141.0",
                                               "extension_package": "2.1.295", "embedded_cli": "2.1.295"})
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def test_desktop_pin_plist_binary_and_verification_conflicts_fail_closed(self):
        for index, options in enumerate(({"pin_version": "2.1.294"}, {"plist_version": "2.1.292"},
                                          {"binary_version": "2.1.292"}, {"verified": "c" * 64})):
            with self.subTest(options=options), self.assertRaises(m.ProductMaterialsError):
                self.freeze("claude-desktop", self.desktop("conflict-" + str(index), **options), str(index))

    def test_vscode_embedded_cli_and_extension_version_conflict_is_rejected(self):
        with self.assertRaisesRegex(m.ProductMaterialsError, "CLI本体版本"):
            self.freeze("claude-vscode", self.vscode(cli_version="2.1.293"))

    def test_cli_version_banner_split_across_stream_chunks_is_read_without_partial_version(self):
        roots = self.desktop()
        target = roots["desktop_cli_root"] / "claude.app/Contents/MacOS/claude"
        original = target.read_bytes()
        banner = b"\n// Version: 2.1.293\n"
        padding = m._CHUNK - len(banner) + 2
        target.write_bytes(original[:32] + b"\0" * padding + original[32:])
        _root, material = self.freeze("claude-desktop", roots)
        self.assertEqual(material["versions"]["embedded_cli"], "2.1.293")

    def test_cli_configuration_source_clue_is_required_and_frozen_as_original_bytes(self):
        roots = self.desktop()
        root, material = self.freeze("claude-desktop", roots)
        original = root / "public/embedded-cli-config-source.js"
        binary_path = roots["desktop_cli_root"] / "claude.app/Contents/MacOS/claude"
        facts = material["facts"]["embedded_cli_config_source"]
        self.assertEqual(binary_path.read_bytes()[facts["byte_offset"]:facts["byte_offset"] + facts["bytes"]], original.read_bytes())
        binary_path.write_bytes(binary_path.read_bytes().split(b"function config")[0])
        with self.assertRaisesRegex(m.ProductMaterialsError, "配置路径线索"):
            self.freeze("claude-desktop", roots, "missing-source")

    def test_catalog_version_and_original_binary_digest_cannot_be_silently_replaced(self):
        root, material = self.freeze("claude-desktop", self.desktop())
        task = self.task(root, material)
        for key, value in (("version", "2.1.237"), ("binary_sha256", "f" * 64)):
            changed = copy.deepcopy(task)
            changed["catalog"]["entry"][key] = value
            changed["task_sha256"] = c.digest({k: v for k, v in changed.items() if k != "task_sha256"})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "实际 Catalog"):
                r.derive_readonly_private_launch_plan(changed, bundle_root=root, materials_receipt=root / "product-materials.json")

    def test_internal_product_links_are_recorded_without_traversing_twice(self):
        roots = self.desktop()
        framework = roots["application_root"] / "Contents/Frameworks/Test.framework"
        write(framework / "Versions/A/Test", b"framework-byte-original")
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        (framework / "Test").symlink_to("Versions/Current/Test")
        root, material = self.freeze("claude-desktop", roots)
        tree = c.read_json(c.bound_file(root, next(row for row in material["inventories"] if row["path"].endswith("application.json"))))
        links = [row for row in tree["entries"] if row["kind"] == "symlink"]
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["target"], "Versions/Current/Test")
        self.assertTrue(all(row["resolved"].endswith(("Versions/A", "Versions/A/Test")) for row in links))
        self.assertEqual(sum(row.get("bytes", 0) for row in tree["entries"]), tree["bytes"])

    def test_external_cycle_and_special_product_entries_are_rejected(self):
        for index, kind in enumerate(("external", "cycle", "fifo")):
            roots = self.desktop("bad-tree-" + kind)
            directory = roots["application_root"] / "Contents"
            if kind == "external":
                secret = self.outer / "unread-secret"
                secret.write_bytes(b"must-not-read")
                (directory / "bad").symlink_to(secret)
            elif kind == "cycle":
                (directory / "bad").symlink_to("other")
                (directory / "other").symlink_to("bad")
            else:
                os.mkfifo(directory / "bad")
            with self.subTest(kind=kind), self.assertRaises(m.ProductMaterialsError):
                self.freeze("claude-desktop", roots, str(index))

    def test_same_byte_replacement_between_reads_is_detected_as_instance_drift(self):
        roots = self.vscode()
        target = roots["extension_root"] / "extension.js"
        original = m._read
        replaced = False
        def drift(root, relative, **kwargs):
            nonlocal replaced
            raw = original(root, relative, **kwargs)
            if root == roots["extension_root"] and relative == "extension.js" and not replaced:
                replacement = target.with_suffix(".replacement")
                replacement.write_bytes(raw)
                os.replace(replacement, target)
                replaced = True
            return raw
        with mock.patch.object(m, "_read", side_effect=drift), self.assertRaisesRegex(m.ProductMaterialsError, "实例或字节"):
            self.freeze("claude-vscode", roots)

    def test_midstream_byte_drift_is_rejected(self):
        roots = self.desktop()
        target = roots["desktop_cli_root"] / "claude.app/Contents/MacOS/claude"
        inode, original, changed = target.stat().st_ino, os.read, False
        def drift(descriptor, count):
            nonlocal changed
            raw = original(descriptor, count)
            if os.fstat(descriptor).st_ino == inode and raw and not changed:
                target.write_bytes(target.read_bytes() + b"changed")
                changed = True
            return raw
        with mock.patch.object(os, "read", side_effect=drift), self.assertRaisesRegex(m.ProductMaterialsError, "漂移"):
            self.freeze("claude-desktop", roots)

    def test_exact_freeze_reentry_converges_and_never_overwrites_or_repairs_damage(self):
        roots = self.desktop()
        root, material = self.freeze("claude-desktop", roots)
        self.assertEqual(self.freeze("claude-desktop", roots)[1], material)
        for path in root.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        original = root / "public/app-info.plist"
        original.write_bytes(original.read_bytes() + b"changed")
        with self.assertRaisesRegex(m.ProductMaterialsError, "禁止覆盖"):
            self.freeze("claude-desktop", roots)
        self.assertTrue(original.read_bytes().endswith(b"changed"))

    def test_incomplete_existing_output_is_not_repaired_and_product_root_is_not_writable_target(self):
        roots = self.desktop()
        output = self.run / "claude-desktop"
        output.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            m.freeze_product_materials("claude-desktop", output_root=output, **roots)
        with self.assertRaisesRegex(m.ProductMaterialsError, "不得.*重叠"):
            m.freeze_product_materials("claude-desktop", output_root=roots["application_root"] / "archive", **roots)
        self.assertFalse((roots["application_root"] / "archive").exists())

    def test_both_product_plans_bind_exact_task_recipe_and_environment_values_to_private_roots(self):
        for client, roots in (("claude-desktop", self.desktop()), ("claude-vscode", self.vscode())):
            with self.subTest(client=client):
                root, material, task, result = self.derive(client, roots)
                plan = result["plan"]
                self.assertEqual(plan["task_sha256"], task["task_sha256"])
                self.assertEqual(plan["recipe_sha256"], task["execution"]["isolation_recipe"]["sha256"])
                self.assertEqual(result["environment"]["HOME"], plan["private_roots"]["home"])
                self.assertEqual(result["environment"]["CLAUDE_CONFIG_DIR"], plan["private_roots"]["config"])
                self.assertEqual(result["materials_receipt_sha256"], material["receipt_sha256"])
                self.assertEqual(set(result["environment"]), set(plan["env_names"]))
                for field in ("formal_ready", "launch_allowed", "registration_verified"):
                    self.assertIs(result[field], False)
                self.assertFalse(Path(result["recipe"]["private_session_root"]).exists())
                if client == "claude-desktop":
                    self.assertEqual(plan["argv"], [plan["executable"]])
                    self.assertEqual(result["environment"]["CLAUDE_USER_DATA_DIR"], plan["private_roots"]["user_data"])
                else:
                    self.assertIn("--user-data-dir", plan["argv"])
                    self.assertIn("--extensions-dir", plan["argv"])
                    self.assertEqual(result["private_user_data_settings"]["claudeCode.environmentVariables"],
                                     [{"name": name, "value": result["environment"][name]} for name in ("HOME", "CLAUDE_CONFIG_DIR")])

    def test_changed_raw_original_digest_or_symlink_evidence_cannot_derive(self):
        for index, kind in enumerate(("raw", "digest", "symlink")):
            roots = self.desktop("evidence-" + kind)
            root, material = self.freeze("claude-desktop", roots, str(index))
            task = self.task(root, material)
            path = root / ("product-materials.json" if kind == "digest" else "public/desktop-main.js")
            if kind == "symlink":
                path.unlink()
                path.symlink_to(self.outer / "unread-target")
            else:
                path.write_bytes(path.read_bytes() + (b" " if kind == "raw" else b"x"))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                r.derive_readonly_private_launch_plan(task, bundle_root=root, materials_receipt=root / "product-materials.json")

    def test_wrong_task_client_root_or_product_artifact_cannot_derive(self):
        root, material, task, _ = self.derive("claude-vscode", self.vscode())
        changed = copy.deepcopy(task)
        next(row for row in changed["client_artifacts"] if row["role"] == "extension_package")["version"]["value"] = "other-product"
        changed["task_sha256"] = c.digest({k: v for k, v in changed.items() if k != "task_sha256"})
        with self.assertRaisesRegex(r.ProductRecipeError, "实读产品物料"):
            r.derive_readonly_private_launch_plan(changed, bundle_root=root, materials_receipt=root / "product-materials.json")
        with self.assertRaisesRegex(r.ProductRecipeError, "本task来源根"):
            r.derive_readonly_private_launch_plan(task, bundle_root=root, materials_receipt=self.run / "product-materials.json")
        changed = copy.deepcopy(task)
        changed["client_id"] = "claude-desktop"
        changed["task_sha256"] = c.digest({k: v for k, v in changed.items() if k != "task_sha256"})
        with self.assertRaises(ValueError):
            r.derive_readonly_private_launch_plan(changed, bundle_root=root, materials_receipt=root / "product-materials.json")

    def test_portable_installation_marker_is_rejected_without_reading_its_private_contents(self):
        roots = self.vscode()
        portable = roots["application_root"].parent / "code-portable-data"
        portable.mkdir()
        write(portable / "unread-user-state", b"private-state-must-not-be-read")
        with self.assertRaisesRegex(m.ProductMaterialsError, "portable"):
            self.freeze("claude-vscode", roots)

    def test_derivation_has_no_caller_plan_callback_or_formal_authority_switch(self):
        root, material, task, result = self.derive("claude-desktop", self.desktop())
        for field, value in (("plan", {"argv": ["/bin/sh"]}), ("env", {"HOME": "/outside"}),
                             ("installation_mode", "portable"), ("provider", mock.Mock())):
            with self.subTest(field=field), self.assertRaises(TypeError):
                r.derive_readonly_private_launch_plan(task, bundle_root=root,
                                                     materials_receipt=root / "product-materials.json", **{field: value})
        with self.assertRaises(r.ProductRecipeUnavailable):
            r.validate_private_launch_plan(task, result["plan"], bundle_root=root)
        formal = copy.deepcopy(task)
        formal["execution"]["source_scope"] = "formal"
        formal["task_sha256"] = c.digest({k: v for k, v in formal.items() if k != "task_sha256"})
        with self.assertRaisesRegex(r.ProductRecipeError, "formal"):
            r.derive_readonly_private_launch_plan(formal, bundle_root=root, materials_receipt=root / "product-materials.json")
        self.assertEqual(r._REVIEWED_TEMPLATES, ())
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def test_readonly_derivation_does_not_create_spawn_connect_read_environment_credentials_or_provider(self):
        root, material = self.freeze("claude-vscode", self.vscode())
        task = self.task(root, material)
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.outer.rglob("*") if path.is_file()}
        provider = mock.Mock()
        with mock.patch.dict(c.SOURCE_ADAPTERS, {"fixture-recorder": provider}), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("派生不得创建目录")), \
                mock.patch.object(os, "getenv", side_effect=AssertionError("不得读取环境")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不得启动产品")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("不得连接")):
            result = r.derive_readonly_private_launch_plan(task, bundle_root=root, materials_receipt=root / "product-materials.json")
        provider.assert_not_called()
        provider.verify_isolation_recipe.assert_not_called()
        self.assertIs(result["launch_allowed"], False)
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.outer.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_freezer_opens_only_explicit_product_material_and_new_archive_files(self):
        roots = self.desktop()
        secret = self.outer / "unread-credential"
        secret.write_bytes(b"never-read-or-copy")
        approved = [*roots.values(), self.run / "claude-desktop"]
        original, descriptors, reads = os.open, {}, []
        def guarded(path, flags, *args, dir_fd=None, **kwargs):
            value = Path(path)
            if not value.is_absolute():
                self.assertIn(dir_fd, descriptors)
                value = descriptors[dir_fd] / value
            if not flags & os.O_DIRECTORY:
                self.assertTrue(any(value.is_relative_to(root) for root in approved), str(value))
                reads.append(value)
            descriptor = original(path, flags, *args, dir_fd=dir_fd, **kwargs)
            descriptors[descriptor] = value
            return descriptor
        duplicate = os.dup
        def copied(descriptor):
            result = duplicate(descriptor)
            descriptors[result] = descriptors[descriptor]
            return result
        with mock.patch.object(os, "open", side_effect=guarded), mock.patch.object(os, "dup", side_effect=copied), \
                mock.patch.object(os, "getenv", side_effect=AssertionError("不得读取环境")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不得启动")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("不得连接")):
            _root, material = self.freeze("claude-desktop", roots)
        self.assertNotIn(secret, reads)
        self.assertEqual(secret.read_bytes(), b"never-read-or-copy")
        self.assertIs(material["formal_ready"], False)


if __name__ == "__main__":
    unittest.main()
