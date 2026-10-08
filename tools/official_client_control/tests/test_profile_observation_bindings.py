"""真实 Docker 返回结构的轻量来源模拟；不运行 Docker、现场服务或重链。

容器 inspect、image inspect 与 cp tar 是模拟返回；Profile 生产、before、
执行／恢复守卫、export、客户端来源复核及完整归档均调用当前源码。
来源只在测试局部登记 regression_fixture，不签正式能力或认证。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world, prepare_request
from tools.official_client_control import delivery_execution as execution, profile_observation_bindings as profiles
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore


class ProfileObservationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="profile-observation-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configuration, self.context, _environment = fixture_world(self.root)
        self.history = {"path": "old-domain/unavailable.json", "sha256": "f" * 64, "bytes": 9}
        for role in ("candidate", "rollback"):
            binding = self.write("inputs/" + role + "-full-profile.json", {"client": "claude", "role": role, "old_parent": self.history})
            self.configuration[role].update(profile_binding=binding, runtime_profile_sha256=binding["sha256"])
        self.context["configuration_ref"] = self.write("inputs/full-configuration.json", self.configuration)
        self.role = "candidate"
        self.write(self.configuration["paths"]["active_profile_relative"], self.bytes(self.configuration["candidate"]["profile_binding"]))
        self.identity = executor.guard_identity(self.configuration, self.context, formal=False)
        self.runtime = executor.DockerComposeRuntime(self.configuration, self.context)
        self.runtime._compose_cache = {"services": {"app": {}, "db": {}, "redis": {}}}
        self.runtime._command = mock.Mock(side_effect=self.command)
        self.runtime.check_qualification = mock.Mock(return_value={"scope": "regression_fixture"})
        self.runtime._credential = b"fixture-only"
        self.runtime._switch = mock.Mock(side_effect=lambda role, checkpoint=None: setattr(self, "role", role))
        self.runtime._business = mock.Mock(return_value=({key: True for key in ("health", "entry-routing", "smoke", "auth", "data", "dependencies")}, []))
        self.engine = guard.GuardEngine(self.configuration, self.context, self.runtime, self.identity)
        self.addCleanup(self.engine.close)
        self.engine.complete_startup()
        self.environment = self.runtime._inspect(self.root / "baseline")[1]
        self.owner = {"pid": os.getpid(), "boot_id": "worker-boot", "process_start": "worker-start", "process_group_id": os.getpgrp() + 1}
        alive = mock.patch.object(guard, "owner_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)
        self.reader = executor.Arm64DeliveryExecutor.__new__(executor.Arm64DeliveryExecutor)
        self.reader.configuration, self.reader.context, self.reader.data_root, self.reader.owner = self.configuration, self.context, self.root, self.owner
        self.reader.client = mock.Mock()
        self.reader.client.call.side_effect = lambda action, payload: self.engine.export(payload) if action == "export" else (_ for _ in ()).throw(AssertionError(action))

    def write(self, relative: str, value: object) -> dict:
        path = guard.managed_path(self.root, relative)
        guard.write_once(path, value if isinstance(value, bytes) else canonical_json_bytes(value))
        return guard.binding(self.root, path)

    def bytes(self, binding: dict) -> bytes:
        return guard.read_bytes(guard.check_binding(self.root, binding))

    @staticmethod
    def tar(raw: bytes, members: list[tuple[str, bytes | None, bytes]] | None = None) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, kind, content in members if members is not None else [("profile.json", tarfile.REGTYPE, raw)]:
                member = tarfile.TarInfo(name)
                member.type = kind or tarfile.REGTYPE
                member.size = len(content) if member.isreg() else 0
                if member.islnk() or member.issym():
                    member.linkname = "profile.json"
                archive.addfile(member, io.BytesIO(content) if member.isreg() else None)
        return buffer.getvalue()

    def command(self, arguments: list, **_options: object) -> bytes:
        ids = {"app": "a" * 64, "db": "b" * 64, "redis": "c" * 64}
        image_id = "sha256:" + "d" * 64
        image_name = "example/isolated@" + self.configuration[self.role]["image_digest"]
        if arguments[0] == "compose" and "ps" in arguments:
            return ids[arguments[-1]].encode("ascii") + b"\n"
        if arguments[:2] == ["container", "inspect"]:
            service = next(name for name, identifier in ids.items() if identifier == arguments[-1])
            labels = {"com.docker.compose.project": self.configuration["runtime"]["compose_project"], "com.docker.compose.service": service}
            if service == "app":
                labels.update(self.configuration["runtime"]["expected_labels"])
            container_image = image_name if service == "app" else "example/dependency-" + service + "@sha256:" + ("1" if service == "db" else "2") * 64
            return canonical_json_bytes([{"Id": ids[service], "Image": image_id, "Config": {"Image": container_image, "Labels": labels},
                                          "NetworkSettings": {"Networks": {}}, "HostConfig": {}}])
        if arguments[:2] == ["image", "inspect"]:
            return canonical_json_bytes([{"Id": image_id, "RepoDigests": [image_name], "Os": "linux", "Architecture": "arm64"}])
        if arguments[:2] == ["container", "cp"]:
            return self.tar(self.bytes(self.configuration[self.role]["profile_binding"]))
        raise AssertionError("未登记的 Docker 模拟命令")

    def committed(self, *, failed: bool = False) -> dict:
        request = prepare_request(self.environment)
        ticket = self.engine.prepare(request, self.owner)
        if failed:
            original = self.runtime._business.side_effect
            count = 0
            def business(*_args, **_kwargs):
                nonlocal count
                count += 1
                return ({key: count != 1 if key == "health" else True for key in ("health", "entry-routing", "smoke", "auth", "data", "dependencies")}, [])
            self.runtime._business.side_effect = business
            self.addCleanup(setattr, self.runtime._business, "side_effect", original)
        observed = self.engine.run(ticket)
        verifier = self.write("verdict/readonly.json", {"observation_sha256": canonical_sha256(observed)})
        return self.engine.finish(ticket, {"result": observed["result"], "observation_sha256": canonical_sha256(observed),
                                           "verifier_binding": verifier, "reason": "本机真实源码来源模拟"})

    def exported(self, core: dict) -> tuple[dict, execution.VerifiedSource]:
        content = {key: value for key, value in core.items() if key != "origin"}
        raw = self.engine.export({"content": content, "origin": core["origin"]})
        source = execution.VerifiedSource(**self.reader.verify_origin_evidence(content, core["origin"]))
        return raw, source

    def projection(self, raw: dict, *, override: dict | None = None) -> profiles.VerifiedProfileProjection:
        response = deepcopy(raw)
        if override is not None:
            response.update(override)
        originals = {path: base64.b64decode(value) for path, value in response["bytes_by_path"].items()}
        return profiles.validate_profile_observation_projection(self.configuration, self.context, response["profile_proof_bindings"],
                lambda binding: originals.get(binding["path"]), purpose="formed_replay", producer_materials=response["profile_producer_materials"])

    def rebound_response(self, raw: dict, target: dict, mutate) -> dict:
        """负例重算受影响原父摘要，保证拒绝原因是合同语义而非旧摘要未更新。"""
        response = deepcopy(raw)
        changed = {}
        document = json.loads(base64.b64decode(response["bytes_by_path"][target["path"]]))
        mutate(document)
        encoded = canonical_json_bytes(document)
        response["bytes_by_path"][target["path"]] = base64.b64encode(encoded).decode("ascii")
        changed[target["path"]] = {"path": target["path"], "sha256": sha256_bytes(encoded), "bytes": len(encoded)}

        def replace_bindings(node):
            if isinstance(node, dict):
                if set(node) == {"path", "sha256", "bytes"} and node["path"] in changed:
                    return deepcopy(changed[node["path"]])
                return {key: replace_bindings(value) for key, value in node.items()}
            if isinstance(node, list):
                return [replace_bindings(value) for value in node]
            return node

        for _iteration in range(100):
            progressed = False
            for path, encoded_bytes in list(response["bytes_by_path"].items()):
                original_bytes = base64.b64decode(encoded_bytes)
                try:
                    original_document = json.loads(original_bytes)
                except (UnicodeDecodeError, ValueError):
                    continue
                new_document = replace_bindings(original_document)
                if new_document == original_document:
                    continue
                rewritten = canonical_json_bytes(new_document)
                response["bytes_by_path"][path] = base64.b64encode(rewritten).decode("ascii")
                changed[path] = {"path": path, "sha256": sha256_bytes(rewritten), "bytes": len(rewritten)}
                progressed = True
            response = replace_bindings(response)
            if not progressed:
                break
        else:
            raise AssertionError("负例原父引用意外形成循环")
        response["profile_producer_materials"].sort(key=canonical_sha256)
        return response

    def archive(self, source: execution.VerifiedSource, initial: object) -> list[dict]:
        """走 Package／Receipt 共用的正式归档消费者，而非直接调用测试 walker。"""
        self._archive_count = getattr(self, "_archive_count", 0) + 1
        store = ControlStore.initialize(self.root / ("archive-store-" + str(self._archive_count)), "2026-10-08T00:00:00Z")
        facts = {"contract_version": "claude-delivery-configuration/v1", "result": "pass", "campaign_id": self.context["campaign_id"],
                 "delivery_id": self.context["delivery_id"], "origin": {"authority": "profile-test-channel", "source_scope": "regression_fixture",
                 "configuration_sha256": source.configuration_sha256, "service_instance_id": source.service_instance_id, "operations_index_binding": None},
                 "configuration": self.configuration, "configuration_sha256": canonical_sha256(self.configuration), "binding": self.context["configuration_ref"]}
        payload = {"schema_version": execution.OPERATIONAL_V2, "persona": {"provider": "fixture", "official_product": "fixture",
                    "auth_family": "oauth", "upstream_route_family": "fixture"}, "manifest_id": "profile-config",
                   "evidence_type": "configuration", "entries": [{"id": "configuration", "facts": facts}]}
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {"profile-test-channel": lambda _store, content, _origin: replace(source, content_sha256=canonical_sha256(content))}, clear=True):
            reference = store.seal_object("operational_evidence", payload)
            plan = {"schema_version": execution.PLAN_V2, "campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"],
                    "executor_configuration_ref": reference, "configuration_sha256": source.configuration_sha256,
                    "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"],
                    "rollback_runtime_profile_sha256": self.configuration["rollback"]["runtime_profile_sha256"]}
            return execution.complete_delivery_raw_bindings(store, plan, source, initial)

    def test_before_execution_export_client_and_archive_share_exact_proof(self) -> None:
        core = self.committed()
        raw, source = self.exported(core)
        projection = source.profile_projection
        self.assertIsInstance(projection, profiles.VerifiedProfileProjection)
        self.assertEqual({json.loads(self.bytes(binding))["collection_phase"] for binding in projection.proof_bindings}, {"before", "execution"})
        self.assertEqual(len(projection.proof_bindings), 3)
        entries = self.archive(source, core)
        for binding in projection.proof_bindings:
            proof = json.loads(self.bytes(binding))
            self.assertIn(proof["observed_profile_binding"], entries)
            self.assertIn(proof["source_profile_binding"], entries)
            self.assertEqual(projection.project(proof["observed_profile_binding"], self.bytes(proof["observed_profile_binding"])), [])
        for material in projection.material_bindings():
            self.assertIn(material, entries)
        self.assertNotIn(self.history, entries)
        self.assertEqual(raw["profile_proof_bindings"], list(projection.proof_bindings))

    def test_freeze_real_inspect_origin_and_formed_replay(self) -> None:
        def freeze(request: dict, directory: Path) -> dict:
            _actual, environment, originals, _identifier = self.runtime._inspect(directory / "before", collection_phase="freeze")
            core = {"contract_version": "claude-delivery-freeze/v1", "result": "pass", "campaign_id": self.context["campaign_id"],
                    "delivery_id": self.context["delivery_id"], "environment": environment, "before_bindings": originals,
                    "gates": [], "guard_identity": {}}
            return {"environment": environment, "rollback": self.configuration["rollback"], "gates": [], "cores": {"freeze": core}}
        self.runtime.freeze = freeze
        snapshot = self.engine.inspect({"fixture": "freeze-request"})
        raw, source = self.exported(snapshot["cores"]["freeze"])
        self.assertEqual(len(source.profile_projection.proof_bindings), 1)
        proof = json.loads(self.bytes(source.profile_projection.proof_bindings[0]))
        self.assertEqual(proof["capture_coordinate"]["freeze_request_sha256"], canonical_sha256({"fixture": "freeze-request"}))
        self.assertEqual(self.projection(raw).proof_bindings, source.profile_projection.proof_bindings)
        self.assertNotIn(self.history, self.archive(source, snapshot["cores"]["freeze"]))

    def test_failed_execution_keeps_observed_candidate_and_real_recovery_proof(self) -> None:
        core = self.committed(failed=True)
        self.assertEqual(core["result"], "failed")
        self.assertEqual(core["restoration"]["state"], "verified")
        _raw, source = self.exported(core)
        proofs = [json.loads(self.bytes(binding)) for binding in source.profile_projection.proof_bindings]
        self.assertEqual({proof["collection_phase"] for proof in proofs}, {"before", "execution", "recovery"})
        recovery = next(proof for proof in proofs if proof["collection_phase"] == "recovery")
        self.assertEqual(recovery["role"], "rollback")
        execution_proof = next(proof for proof in proofs if proof["collection_phase"] == "execution")
        self.assertEqual(execution_proof["role"], "candidate")
        entries = self.archive(source, core)
        self.assertIn(recovery["observed_profile_binding"], entries)
        self.assertIn(execution_proof["observed_profile_binding"], entries)

    def test_unlisted_same_sha_path_remains_strict(self) -> None:
        core = self.committed()
        _raw, source = self.exported(core)
        original = next(iter(source.profile_projection._documents.values()))
        copied = self.write("unknown/runtime-profile.json", original[1])
        self.assertIsNone(source.profile_projection.project(copied, original[1]))
        with self.assertRaisesRegex(ControlError, "交付原件缺失"):
            self.archive(source, [core, copied])

    def test_missing_projection_cannot_downgrade_current_docker_archive(self) -> None:
        core = self.committed()
        _raw, source = self.exported(core)
        with self.assertRaisesRegex(ControlError, "Docker Profile.*来源证明"):
            self.archive(replace(source, profile_projection=None), core)

    def test_proof_or_source_or_tar_byte_drift_is_rejected(self) -> None:
        core = self.committed()
        raw, source = self.exported(core)
        proof = next(json.loads(self.bytes(binding)) for binding in source.profile_projection.proof_bindings if json.loads(self.bytes(binding))["observation"]["kind"] == "container")
        targets = [source.profile_projection.proof_bindings[0], proof["source_profile_binding"], proof["observed_profile_binding"],
                   proof["observation"]["container_inspect_binding"], proof["observation"]["image_inspect_binding"], proof["observation"]["profile_tar_binding"]]
        for binding in targets:
            with self.subTest(path=binding["path"]):
                response = deepcopy(raw)
                original = base64.b64decode(response["bytes_by_path"][binding["path"]])
                response["bytes_by_path"][binding["path"]] = base64.b64encode(original + b" ").decode("ascii")
                with self.assertRaisesRegex(ControlError, "字节漂移"):
                    self.projection(response)

    def test_missing_proof_or_unlisted_proof_reference_is_rejected(self) -> None:
        core = self.committed()
        raw, _source = self.exported(core)
        with self.assertRaisesRegex(ControlError, "集合遗漏"):
            self.projection(raw, override={"profile_proof_bindings": raw["profile_proof_bindings"][1:]})
        response = deepcopy(raw)
        response["bytes_by_path"].pop(response["profile_proof_bindings"][0]["path"])
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.projection(response)
        materials = deepcopy(raw["profile_producer_materials"])
        next(item for item in materials if item["proof_references"])["proof_references"] = []
        with self.assertRaisesRegex(ControlError, "完整显式闭集"):
            self.projection(raw, override={"profile_producer_materials": materials})

    def test_self_hashed_proof_semantic_drift_still_rejects(self) -> None:
        core = self.committed()
        raw, source = self.exported(core)
        target = next(binding for binding in source.profile_projection.proof_bindings if json.loads(self.bytes(binding))["observation"]["kind"] == "container")
        mutations = {
            "campaign": lambda proof: proof.update(campaign_id="C-other"),
            "configuration": lambda proof: proof.update(configuration_sha256="e" * 64),
            "scope": lambda proof: proof.update(source_scope="formal"),
            "phase": lambda proof: proof.update(collection_phase="freeze"),
            "coordinate": lambda proof: proof["capture_coordinate"].update(root_relative="guard/operations/op-wrong"),
            "role": lambda proof: proof.update(role="rollback"),
            "producer_epoch": lambda proof: proof["producer_identity"].update(service_instance_id="new-current-process"),
            "container_id": lambda proof: proof["observation"].update(container_id="e" * 64),
            "image": lambda proof: proof["observation"].update(image_digest="sha256:" + "e" * 64),
            "extra_field": lambda proof: proof.update(inherit_by_sha=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(change=name):
                with self.assertRaises(ControlError):
                    self.projection(self.rebound_response(raw, target, mutate))

    def test_host_proof_requires_same_container_parent_and_active_coordinate(self) -> None:
        core = self.committed()
        raw, source = self.exported(core)
        host = next(binding for binding in source.profile_projection.proof_bindings if json.loads(self.bytes(binding))["observation"]["kind"] == "host")
        execution_container = next(binding for binding in source.profile_projection.proof_bindings if json.loads(self.bytes(binding))["collection_phase"] == "execution")
        mutations = [lambda proof: proof["observation"].update(active_profile_relative="runtime/other.json"),
                     lambda proof: proof["observation"].update(container_proof_binding=execution_container)]
        for mutate in mutations:
            with self.assertRaises(ControlError):
                self.projection(self.rebound_response(raw, host, mutate))

    def test_matched_runtime_identity_cannot_replace_missing_proof_with_none(self) -> None:
        core = self.committed()
        raw, _source = self.exported(core)
        reference = next(material["proof_references"][0] for material in raw["profile_producer_materials"] if material["proof_references"])
        identity_binding = reference["identity_binding"]
        identity = json.loads(self.bytes(identity_binding))
        if identity["schema_version"] != profiles.RUNTIME_IDENTITY_SCHEMA:
            reference = next(row for material in raw["profile_producer_materials"] for row in material["proof_references"]
                             if json.loads(self.bytes(row["identity_binding"]))["schema_version"] == profiles.RUNTIME_IDENTITY_SCHEMA)
            identity_binding = reference["identity_binding"]
        response = self.rebound_response(raw, identity_binding, lambda document: document.update(profile_observation_proof_binding=None))
        with self.assertRaisesRegex(ControlError, "实测已匹配冻结角色但来源证明缺失"):
            self.projection(response)

    def test_producer_pointer_and_unlisted_same_sha_original_cannot_grant_permission(self) -> None:
        core = self.committed()
        raw, _source = self.exported(core)
        for field in ("producer_pointer", "identity_pointer"):
            response = deepcopy(raw)
            material = next(item for item in response["profile_producer_materials"] if item["proof_references"])
            material["proof_references"][0][field] = "/other_unlisted_pointer"
            response["profile_producer_materials"].sort(key=canonical_sha256)
            with self.assertRaises(ControlError):
                self.projection(response)
        material = next(item for item in raw["profile_producer_materials"] if item["collection_phase"] == "before")
        copied = self.write("unknown/prepared.json", self.bytes(material["producer_binding"]))
        response = deepcopy(raw)
        next(item for item in response["profile_producer_materials"] if item["collection_phase"] == "before")["producer_binding"] = copied
        response["bytes_by_path"][copied["path"]] = base64.b64encode(self.bytes(copied)).decode("ascii")
        response["profile_producer_materials"].sort(key=canonical_sha256)
        with self.assertRaisesRegex(ControlError, "原父坐标漂移"):
            self.projection(response)

    def test_consumer_revalidates_producer_proof_again_after_source_export(self) -> None:
        core = self.committed()
        _raw, source = self.exported(core)
        target = source.profile_projection.proof_bindings[0]
        new_bytes = dict(source.bytes_by_path)
        new_bytes[target["path"]] += b" "
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            self.archive(replace(source, bytes_by_path=new_bytes), core)

    def test_memory_projection_cannot_add_an_unverified_same_sha_parent(self) -> None:
        core = self.committed()
        _raw, source = self.exported(core)
        original = next(iter(source.profile_projection._documents.values()))
        copied = self.write("unknown/pretended-profile.json", original[1])
        forged_documents = dict(source.profile_projection._documents)
        forged_documents[copied["path"]] = (copied, original[1])
        forged = replace(source.profile_projection, _documents=forged_documents)
        with self.assertRaisesRegex(ControlError, "内存投影.*未授权路径"):
            self.archive(replace(source, profile_projection=forged), [core, copied])

    def test_guard_does_not_authorize_uploaded_or_self_hashed_core(self) -> None:
        core = self.committed()
        content = {key: value for key, value in core.items() if key != "origin"}
        content["observed_seconds"] += 1
        with self.assertRaisesRegex(guard.GuardError, "不是该受管守卫"):
            self.engine.export({"content": content, "origin": core["origin"]})

    def test_unknown_json_descendants_remain_complete_and_strict(self) -> None:
        core = self.committed()
        _raw, source = self.exported(core)
        leaf = self.write("unknown/leaf.bin", b"descendant")
        child = self.write("unknown/child.json", {"schema_version": profiles.PROOF_SCHEMA, "raw_child": leaf})
        entries = self.archive(source, [core, child])
        self.assertIn(leaf, entries)
        (self.root / leaf["path"]).unlink()
        with self.assertRaisesRegex(ControlError, "交付原件缺失"):
            self.archive(source, [core, child])

    def test_invalid_tar_types_duplicates_and_fake_target_paths_are_refused(self) -> None:
        data = self.bytes(self.configuration["candidate"]["profile_binding"])
        cases = [[("profile.json", tarfile.SYMTYPE, b"")], [("profile.json", tarfile.LNKTYPE, b"")],
                 [("profile.json", tarfile.DIRTYPE, b"")], [("profile.json", tarfile.REGTYPE, data)] * 2,
                 [("profile.json", tarfile.REGTYPE, data), ("other.json", tarfile.REGTYPE, data)],
                 [("../profile.json", tarfile.REGTYPE, data)], [("nested/profile.json", tarfile.REGTYPE, data)]]
        for members in cases:
            with self.subTest(members=[(name, kind) for name, kind, _raw in members]):
                with self.assertRaises(ControlError):
                    profiles.extract_profile_tar(self.tar(data, members), "/app/profile.json")
        with self.assertRaises(ControlError):
            profiles.extract_profile_tar(self.tar(data) + self.tar(data), "/app/profile.json")
        with self.assertRaises(ControlError):
            profiles.extract_profile_tar(self.tar(data) + b"truncated-extra-header", "/app/profile.json")

    def test_tar_allows_timestamp_pax_but_rejects_target_alias(self) -> None:
        data = self.bytes(self.configuration["candidate"]["profile_binding"])
        for pax, allowed in (({"mtime": "1.123456789"}, True), ({"path": "profile.json"}, False)):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("profile.json")
                member.size, member.pax_headers = len(data), pax
                archive.addfile(member, io.BytesIO(data))
            if allowed:
                self.assertEqual(profiles.extract_profile_tar(buffer.getvalue(), "/app/profile.json"), data)
            else:
                with self.assertRaises(ControlError):
                    profiles.extract_profile_tar(buffer.getvalue(), "/app/profile.json")

    def test_unknown_image_keeps_actual_profile_and_has_no_target_proof(self) -> None:
        original_command = self.runtime._command.side_effect
        def unknown(arguments: list, **options: object) -> bytes:
            raw = original_command(arguments, **options)
            if arguments[:2] == ["image", "inspect"]:
                document = json.loads(raw)
                document[0]["RepoDigests"] = []
                return canonical_json_bytes(document)
            return raw
        self.runtime._command.side_effect = unknown
        operation = "op-" + "e" * 32
        actual, _environment, originals, _identifier = self.runtime._inspect(self.root / "guard" / "operations" / operation / "during" / "identity", collection_phase="execution")
        self.assertEqual(actual["image"]["state"], "unknown")
        self.assertEqual(actual["profile"]["value"], self.configuration["candidate"]["profile_binding"]["sha256"])
        identity = json.loads(self.bytes(actual["profile"]["direct_binding"]))
        self.assertIsNone(identity["profile_observation_proof_binding"])
        self.assertFalse(any(binding["path"].endswith("runtime-profile-proof.json") for binding in originals))

    def test_source_symlink_and_host_container_mismatch_are_refused_before_armed(self) -> None:
        active = self.root / self.configuration["paths"]["active_profile_relative"]
        active.write_bytes(self.bytes(self.configuration["rollback"]["profile_binding"]))
        with self.assertRaisesRegex(guard.GuardError, "宿主Profile与容器实测不符"):
            self.engine.prepare(prepare_request(self.environment), self.owner)
        self.assertEqual(self.engine._index()["operations"], [])
        source = self.root / self.configuration["candidate"]["profile_binding"]["path"]
        original = source.read_bytes()
        copied = self.root / "copy-source.json"
        copied.write_bytes(original)
        source.unlink()
        source.symlink_to(copied)
        with self.assertRaises(guard.GuardError):
            self.runtime._inspect(self.root / "guard" / "operations" / ("op-" + "f" * 32) / "during" / "identity", collection_phase="execution")


if __name__ == "__main__":
    unittest.main()
