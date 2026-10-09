"""批量来源的本机边界测试，不消费正式账号或正式认证登记。

直连测试保留真实守卫原件生成和执行器验真，仅把 socket 调用转为同进程
方法调用；独立 socket 测试另行覆盖协议。轻量配置不替代第二根集成验收。
"""

from __future__ import annotations

import base64
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests import test_claude_upgrade_arm64_delivery_executor as fixtures
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError


def freeze_request(configuration: dict, context: dict) -> dict:
    """按真实固定冻结入口构造文件现场请求。"""
    return {"campaign_id": context["campaign_id"], "candidate_id": context["candidate_id"],
            "delivery_id": context["delivery_id"], "candidate_image_digest": configuration["candidate"]["image_digest"],
            "architecture": "arm64", "runtime_profile_sha256": configuration["candidate"]["runtime_profile_sha256"],
            "observation_window_seconds": 1, "delivery_gates": list(executor.DELIVERY_GATES)}


def snapshot_items(snapshot: dict) -> list[dict]:
    return [execution._source_request(snapshot["cores"][name], snapshot["cores"][name]["origin"])
            for name in ("configuration", "stable_projection", "freeze")]


class DirectSourceBatchTests(unittest.TestCase):
    """同进程方法直连，不能算真实 RPC 或第二根资格投影认证。"""

    _close = fixtures.GuardTests._close

    def setUp(self) -> None:
        fixtures.GuardTests.setUp(self)
        self.items = snapshot_items(self.engine.inspect(freeze_request(self.configuration, self.context)))
        self.reader = executor.create_executor("arm64-isolated", self.configuration, self.context)
        # 文件守卫运行在本测试进程，直连消费者使用另一个真实存活进程的身份。
        # 守卫身份、部署字节和存活校验均保持真实值，不替换验真函数。
        self.reader.owner = self.owner
        self.channel = mock.patch.object(self.reader.client, "call", side_effect=self.direct_call)
        self.channel.start()
        self.addCleanup(self.channel.stop)

    def direct_call(self, action: str, payload: dict) -> dict:
        if action == "export_batch":
            return self.engine.export_batch(payload)
        if action == "export":
            return self.engine.export(payload)
        raise AssertionError("直连夹具不允许额外守卫动作：" + action)

    def test_batch_matches_individual_sources_and_complete_original_union(self) -> None:
        individual = [self.reader.verify_origin_evidence(**item) for item in self.items]
        batch = self.reader.verify_origin_evidence_batch(self.items)
        expected_originals = {}
        for expected in individual:
            expected_originals.update(expected["bytes_by_path"])
        self.assertEqual(len(batch), len(individual))
        for actual, expected in zip(batch, individual):
            for field in ("content_sha256", "deployment_sha256", "configuration_sha256", "service_instance_id", "trusted_data_root"):
                self.assertEqual(actual[field], expected[field])
            self.assertEqual(actual["bytes_by_path"], expected_originals)
            proof = actual["guard_continuity"]
            self.assertEqual(proof.source_content_sha256, expected["guard_continuity"].source_content_sha256)
            proof.revalidate(lambda binding: actual["bytes_by_path"][binding["path"]], purpose="formed_replay")
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)

    def test_empty_duplicate_and_oversized_requests_are_rejected(self) -> None:
        requests = ([], [self.items[0], self.items[0]], self.items * (guard.MAX_EXPORT_ITEMS + 1))
        for items in requests:
            with self.subTest(count=len(items)):
                with self.assertRaises(guard.GuardError):
                    self.engine.export_batch({"items": items})
                with self.assertRaises(guard.GuardError):
                    self.reader.verify_origin_evidence_batch(items)

    def test_cross_configuration_and_wrong_producer_are_rejected(self) -> None:
        for field, value in (("configuration_sha256", "f" * 64), ("service_instance_id", "unrelated-producer")):
            items = deepcopy(self.items)
            items[1]["origin"][field] = value
            with self.subTest(field=field), self.assertRaises(guard.GuardError):
                self.reader.verify_origin_evidence_batch(items)

    def test_self_summarized_unknown_content_is_not_a_guard_source(self) -> None:
        items = deepcopy(self.items)
        items[1]["content"]["projection"]["scope"] = "invented"
        with self.assertRaisesRegex(guard.GuardError, "不是该受管守卫"):
            self.reader.verify_origin_evidence_batch(items)

    def test_missing_extra_reordered_or_other_request_responses_are_rejected(self) -> None:
        original = self.engine.export_batch({"items": self.items})
        changes = {
            "漏项": lambda value: value["items"].pop(),
            "额外项": lambda value: value["items"].append(deepcopy(value["items"][0])),
            "调换": lambda value: value["items"].reverse(),
            "请求全集变化": lambda value: value.update(requests_sha256="e" * 64),
            "单项来源变化": lambda value: value["items"][1].update(request_sha256="e" * 64),
        }
        for name, mutate in changes.items():
            response = deepcopy(original)
            mutate(response)
            with self.subTest(name=name), mock.patch.object(self.reader.client, "call", return_value=response):
                with self.assertRaises(guard.GuardError):
                    self.reader.verify_origin_evidence_batch(self.items)

    def test_response_missing_or_changed_original_is_rejected(self) -> None:
        original = self.engine.export_batch({"items": self.items})
        path = self.context["configuration_ref"]["path"]
        for missing in (True, False):
            response = deepcopy(original)
            if missing:
                response["bytes_by_path"].pop(path)
            else:
                response["bytes_by_path"][path] = base64.b64encode(b"changed-original").decode("ascii")
            with self.subTest(missing=missing), mock.patch.object(self.reader.client, "call", return_value=response):
                with self.assertRaises((guard.GuardError, ControlError)):
                    self.reader.verify_origin_evidence_batch(self.items)

    def test_guard_rechecks_originals_on_next_call(self) -> None:
        self.reader.verify_origin_evidence_batch(self.items)
        path = self.root / self.context["configuration_ref"]["path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises((guard.GuardError, ControlError)):
            self.reader.verify_origin_evidence_batch(self.items)

    def test_current_channel_and_deployment_identity_are_independently_checked(self) -> None:
        original = self.engine.export_batch({"items": self.items})
        for change_one in (True, False):
            response = deepcopy(original)
            rows = response["items"][1:2] if change_one else response["items"]
            for row in rows:
                # 原响应各项可能引用同一个字典，复制后才只篡改单项。
                row["guard_identity"] = {**row["guard_identity"], "instance_id": "other-instance"}
            with self.subTest(change_one=change_one), mock.patch.object(self.reader.client, "call", return_value=response):
                with self.assertRaises(guard.GuardError):
                    self.reader.verify_origin_evidence_batch(self.items)

    def test_missing_epochs_and_wrong_continuity_producer_are_rejected(self) -> None:
        original = self.engine.export_batch({"items": self.items})
        for field, changed in (("epoch_bindings", []), ("source_service_instance_id", "unknown-producer")):
            response = deepcopy(original)
            response["items"][1]["guard_continuity"] = {
                **response["items"][1]["guard_continuity"], field: changed}
            with self.subTest(field=field), mock.patch.object(self.reader.client, "call", return_value=response):
                with self.assertRaises((guard.GuardError, ControlError)):
                    self.reader.verify_origin_evidence_batch(self.items)

    def test_channel_failure_cannot_fall_back_to_old_export(self) -> None:
        with mock.patch.object(self.reader.client, "call", side_effect=guard.GuardError("通道已失效")) as call:
            with self.assertRaisesRegex(guard.GuardError, "通道已失效"):
                self.reader.verify_origin_evidence_batch(self.items)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "export_batch")

    def test_channel_death_after_batch_before_consumption_is_rejected(self) -> None:
        store = object()
        def verify(actual_store, content, origin):
            return execution.VerifiedSource(**self.reader.verify_origin_evidence(content, origin))
        verify.verify_batch = lambda actual_store, items: [
            execution.VerifiedSource(**row) for row in self.reader.verify_origin_evidence_batch(items)]
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {executor.AUTHORITY: verify}, clear=True), \
                execution._source_batch_scope(store, self.items):
            item = self.items[0]
            execution._verified_source(store, **item)
            with mock.patch.object(guard, "owner_alive", return_value=False):
                with self.assertRaisesRegex(ControlError, "当前守卫实例已消失"):
                    execution._verified_source(store, **item)

    def test_conflicting_bytes_at_same_path_fail_batch_union(self) -> None:
        outputs = [self.engine.export(item) for item in self.items[:2]]
        path = self.context["configuration_ref"]["path"]
        self.assertIn(path, outputs[0]["bytes_by_path"])
        outputs[1]["bytes_by_path"][path] = base64.b64encode(b"conflict").decode("ascii")
        with mock.patch.object(self.engine, "_export", side_effect=outputs):
            with self.assertRaisesRegex(guard.GuardError, "同一路径"):
                self.engine.export_batch({"items": self.items[:2]})


class SourceBatchScopeTests(unittest.TestCase):
    """只验证控制层来源作用域，不假称来源夹具是正式守卫通道。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-source-scope-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = object()
        self.origin = {"authority": "fixture-batch", "source_scope": "regression_fixture",
                       "configuration_sha256": "a" * 64, "service_instance_id": "fixture-instance",
                       "operations_index_binding": None}
        self.raw = b"source-original\n"
        (self.root / "original").write_bytes(self.raw)
        self.content = {"value": "first", "binding": {"path": "original", "sha256": sha256_bytes(self.raw), "bytes": len(self.raw)}}
        self.items = [execution._source_request(self.content, self.origin)]
        self.calls = []

        def source(content, origin):
            return execution.VerifiedSource(canonical_sha256(content), "b" * 64,
                {"original": self.raw}, origin["configuration_sha256"], origin["service_instance_id"], self.root)

        def single(store, content, origin):
            self.calls.append("single")
            return source(content, origin)

        def batch(store, items):
            self.calls.append("batch")
            return [source(item["content"], item["origin"]) for item in items]

        single.verify_batch = batch
        self.verifier = single
        registration = mock.patch.dict(execution.SOURCE_VERIFIERS, {self.origin["authority"]: single}, clear=True)
        registration.start()
        self.addCleanup(registration.stop)

    def read(self, store=None, content=None):
        return execution._verified_source(self.store if store is None else store,
            self.content if content is None else content, self.origin)

    def test_nested_declared_reads_use_one_batch_and_next_call_revalidates(self) -> None:
        with execution._source_batch_scope(self.store, self.items):
            first = self.read()
            with execution._source_batch_scope(self.store, self.items):
                self.assertIs(self.read(), first)
        with execution._source_batch_scope(self.store, self.items):
            self.assertIsNot(self.read(), first)
        self.assertEqual(self.calls, ["batch", "batch"])

    def test_normal_and_exceptional_exit_invalidate_copied_context(self) -> None:
        for exceptional in (False, True):
            with self.subTest(exceptional=exceptional):
                try:
                    with execution._source_batch_scope(self.store, self.items):
                        copied = copy_context()
                        self.read()
                        if exceptional:
                            raise RuntimeError("模拟只读调用中断")
                except RuntimeError:
                    if not exceptional:
                        raise
                with self.assertRaises(ControlError):
                    copied.run(self.read)
                self.read()
        self.assertEqual(self.calls, ["batch", "single", "batch", "single"])

    def test_undeclared_item_other_store_and_revoked_registration_are_rejected(self) -> None:
        with execution._source_batch_scope(self.store, self.items):
            with self.assertRaises(ControlError):
                self.read(content={**self.content, "value": "unknown"})
            with self.assertRaises(ControlError):
                self.read(store=object())
            with self.assertRaises(ControlError):
                with execution._source_batch_scope(object(), self.items):
                    pass
            with mock.patch.dict(execution.SOURCE_VERIFIERS, {}, clear=True):
                with self.assertRaises(ControlError):
                    self.read()
            with mock.patch.dict(execution.SOURCE_VERIFIERS, {self.origin["authority"]: lambda *args: None}):
                with self.assertRaises(ControlError):
                    self.read()

    def test_live_original_drift_after_batch_is_rejected(self) -> None:
        with execution._source_batch_scope(self.store, self.items):
            self.read()
            (self.root / "original").write_bytes(b"changed-original\n")
            with self.assertRaisesRegex(ControlError, "漂移"):
                self.read()

    def test_missing_extra_and_wrong_content_from_batch_verifier_are_rejected(self) -> None:
        original = self.verifier.verify_batch
        for mode in ("missing", "extra", "wrong-content"):
            def broken(store, items):
                rows = original(store, items)
                if mode == "missing":
                    return []
                if mode == "extra":
                    return rows * 2
                item = deepcopy(items[0])
                item["content"]["value"] = "other"
                return original(store, [item])
            with self.subTest(mode=mode), mock.patch.object(self.verifier, "verify_batch", broken):
                with self.assertRaises(ControlError):
                    with execution._source_batch_scope(self.store, self.items):
                        self.read()


class SocketSourceBatchTests(unittest.TestCase):
    """通过独立守卫进程的真实私有 socket 验证批量协议；仍属本机夹具。"""

    setUp = fixtures.ProcessTests.setUp
    close = fixtures.ProcessTests.close
    start_guard = fixtures.ProcessTests.start_guard

    def test_independent_socket_returns_the_requested_source_set(self) -> None:
        self.start_guard()
        reader = executor.create_executor("arm64-isolated", self.configuration, self.context)
        items = snapshot_items(reader.freeze(freeze_request(self.configuration, self.context)))
        sources = reader.verify_origin_evidence_batch(items)
        self.assertEqual([source["content_sha256"] for source in sources],
                         [canonical_sha256(item["content"]) for item in items])
        for source in sources:
            source["guard_continuity"].revalidate(lambda binding: source["bytes_by_path"][binding["path"]], purpose="formed_replay")
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)


if __name__ == "__main__":
    unittest.main()
