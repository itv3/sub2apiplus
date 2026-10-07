"""``tools/generate_claude_fw_g_profile.py`` 的确定性投影（Claude 升级控制面设计 B3-03 及其补充项）。

全部离线：不运行 git、不读本地忽略区。验证的改动：

* 版本与时间不写死：Release 身份、迁移收据的平台与日期都由调用方给出；
* 输出里的制品路径一律是仓库相对路径，与传绝对路径还是相对路径无关；路径在仓库外即拒绝；
* 输出只写一次：同内容复用、异内容拒绝；
* 迁移收据的源码 transition 不读工作树：策略登记的源码摘要取自冻结提交读取器，本次生成的 Profile、Wire 取内存摘要；
* 补充项：Wire 投影策略的合同与 2.1.226 那一份的取值、HTTP/1.1 请求流解析、版本指纹的定义与逐条复算、Release 身份
  与已入库 2.1.226 Release 的摘要一致，以及多模型（非主模型逐场景、server fallback 锁存）的投影。

历史 2.1.226 Profile／Wire 的逐字复算需要本地取证数据，不在单元测试里跑（见收口汇报的回归记录）。
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import generate_claude_fw_g_profile as generator
from tools.official_client_capture.tests import claude_wire_fixture as wire_fixture

POLICY = Path(generator.ROOT) / "tools" / "official_client_capture" / "claude_fw_g_generation_policy_2_1_226_v2.json"
PROJECTION = Path(generator.ROOT) / "tools" / "official_client_capture" / "claude_upgrade_wire_projection_2_1_226.json"
ACTIVE_RELEASE = Path(generator.ROOT) / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "release-catalog.json"
PROFILE = Path(generator.ROOT) / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "profiles" / "2.1.285" / ("1" * 64 + ".json")
WIRE = Path(generator.ROOT) / "backend" / "internal" / "officialegress" / "catalogdata" / "claude" / "wire" / "2.1.285" / ("2" * 64 + ".json")


class DeterministicProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        generator.configure_generation_policy(POLICY)

    def test_write_once_reuses_identical_and_refuses_different(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "out.json"
            self.assertEqual(generator.write_once(path, b"{}\n"), "generated")
            self.assertEqual(generator.write_once(path, b"{}\n"), "reused")
            with self.assertRaisesRegex(SystemExit, "禁止覆盖"):
                generator.write_once(path, b"[]\n")
            link = Path(directory) / "link.json"
            os.symlink(path, link)
            with self.assertRaisesRegex(SystemExit, "符号链接"):
                generator.write_once(link, b"{}\n")

    def test_recorded_paths_are_repository_relative(self) -> None:
        relative = Path("backend") / "x.json"
        with mock.patch.object(Path, "cwd", return_value=Path(generator.ROOT)):
            self.assertEqual(generator.repository_relative(relative), "backend/x.json")
        self.assertEqual(generator.repository_relative(Path(generator.ROOT) / "backend" / "x.json"), "backend/x.json")
        with self.assertRaisesRegex(SystemExit, "不在仓库内"):
            generator.repository_relative(Path("/") / "outside" / "x.json")

    def test_source_transitions_do_not_read_the_worktree(self) -> None:
        reads: list[str] = []

        def frozen(path: str) -> str:
            reads.append(path)
            return "f" * 64

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("读了工作树文件")):
            transitions = generator.build_model_capability_source_transitions(PROFILE, "1" * 64, WIRE, "2" * 64, frozen)
        by_path = {item["path"]: item for item in transitions}
        profile_key = PROFILE.relative_to(generator.ROOT).as_posix()
        wire_key = WIRE.relative_to(generator.ROOT).as_posix()
        self.assertEqual((by_path[profile_key]["from_sha256"], by_path[profile_key]["to_sha256"]), (generator.EMPTY_FILE_SHA256, "1" * 64))
        self.assertEqual(by_path[wire_key]["to_sha256"], "2" * 64)
        self.assertEqual(sorted(reads), sorted(path for path, _before, _reason in generator.MODEL_CAPABILITY_SOURCE_TRANSITIONS))
        self.assertEqual([item["path"] for item in transitions], sorted(by_path))

    def test_receipt_and_release_identity_take_platform_and_date_from_caller(self) -> None:
        catalog = {"evidence": {"campaign_sha256": "3" * 64}, "models": []}
        release, bundle = generator.build_model_capability_release_identity("1" * 64, "2" * 64, "4" * 64, "linux/arm64")
        self.assertNotEqual(release, generator.build_model_capability_release_identity("1" * 64, "2" * 64, "4" * 64, "linux/amd64")[0])
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("读了工作树文件")):
            receipt = generator.build_model_capability_receipt(
                catalog, PROFILE, "1" * 64, WIRE, "2" * 64, release, bundle,
                receipt_date="2026-10-07", platform="linux/arm64", source_digest=lambda path: "f" * 64,
            )
        self.assertEqual((receipt["date"], receipt["phase"], receipt["target"]["platform"]), ("2026-10-07", generator.RECEIPT_PHASE, "linux/arm64"))
        self.assertEqual(receipt["artifacts"]["after"]["profile"], PROFILE.relative_to(generator.ROOT).as_posix())
        self.assertEqual(receipt["artifacts"]["after"]["wire"], WIRE.relative_to(generator.ROOT).as_posix())
        with self.assertRaisesRegex(SystemExit, "YYYY-MM-DD"):
            generator.build_model_capability_receipt(
                catalog, PROFILE, "1" * 64, WIRE, "2" * 64, release, bundle,
                receipt_date="2026/10/07", platform="linux/arm64", source_digest=lambda path: "f" * 64,
            )

    def test_source_commit_must_be_a_full_commit_id(self) -> None:
        for value in ("HEAD", "a" * 39, "G" * 40):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SystemExit, "40 位"):
                    generator.git_blob_digest_reader(value)



class ProjectionPolicyTests(unittest.TestCase):
    """Wire 投影策略：合同闭合、2.1.226 取值与 v2 generation policy 的重叠事实一致、probe 清单。"""

    def test_policy_2_1_226_matches_generation_policy_and_release(self) -> None:
        generator.configure_generation_policy(POLICY)
        policy = generator.load_projection_policy(PROJECTION)
        generator.check_projection_matches_generation_policy(policy)
        self.assertEqual(generator.base_model(policy), "claude-sonnet-5")
        self.assertEqual(generator.model_probe(policy, "claude-fable-5", generator.PROBE_BASH), "claude-fable-5-v4-bash")
        self.assertEqual(generator.model_probe(policy, "claude-sonnet-5", generator.PROBE_BASH), generator.PROBE_BASH)
        # Release 身份与已入库 2.1.226 Release 逐字一致（与 Go 加载器同一算法）。
        release = json.loads(ACTIVE_RELEASE.read_text(encoding="utf-8"))["releases"][0]
        self.assertEqual(
            generator.build_release_identity(
                version=release["version"],
                platform=release["platform"],
                profile_digest=release["profile"]["sha256"],
                wire_digest=release["wire"]["sha256"],
                model_catalog_digest="d34ca049ec851b220f06a4701c951a8be270d4a498b8012229c7f62cb8183df1",
                required_rules_manifest_sha256=release["required_rules_manifest_sha256"],
                required_rule_count=release["required_rule_count"],
            ),
            (release["release_sha256"], release["bundle_sha256"]),
        )

    def test_policy_rejects_open_or_inconsistent_fields(self) -> None:
        base = generator.load_projection_policy(PROJECTION)
        variants = {
            "多余字段": lambda d: d.update(extra=1),
            "legacy 不在闭集": lambda d: d["models"].update(legacy_retry_fallback=["claude-x"]),
            "主模型登记 server fallback": lambda d: d["models"]["server_fallbacks"].append(
                {"model": "claude-sonnet-5", "fallback_model": "y", "probe": "v4-bash"}
            ),
            "缺官方缺省 effort": lambda d: d.update(efforts=["low"]),
            "指纹算法": lambda d: d["attestation"]["version_fingerprint"].update(algorithm="md5"),
            "TLS 向量重复": lambda d: d["tls"].update(cipher_suites=[4865, 4865]),
            "模型重复": lambda d: d["models"].update(ordered=["claude-sonnet-5", "claude-sonnet-5"]),
            "版本号": lambda d: d.update(version="2.1"),
        }
        for label, mutate in variants.items():
            with self.subTest(label=label):
                document = copy.deepcopy(base)
                mutate(document)
                with self.assertRaises(generator.ProjectionError):
                    generator.validate_projection_policy(document)

    def test_required_probes(self) -> None:
        policy = generator.load_projection_policy(PROJECTION)
        self.assertEqual(len(generator.catalog_probes(policy)), 49)
        self.assertTrue(set(generator.catalog_probes(policy)) <= set(generator.required_probes(policy)))
        single = wire_fixture.single_model_policy(policy)
        required = generator.required_probes(single)
        self.assertEqual(len(required), 23)
        self.assertFalse([probe for probe in required if probe.startswith("claude-")])


class EvidenceParsingTests(unittest.TestCase):
    """HTTP/1.1 请求流与版本指纹。"""

    def test_http_stream_splits_keepalive_and_requires_redaction(self) -> None:
        body = json.dumps({"model": "m", "messages": []}).encode()
        gz = gzip.compress(body, mtime=0)
        first = b"POST /v1/messages?beta=true HTTP/1.1\r\nAuthorization: Bearer <secret>\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        second = (
            b"POST /v1/messages?beta=true HTTP/1.1\r\nAuthorization: Bearer <secret>\r\nContent-Encoding: gzip\r\n"
            b"Content-Length: %d\r\n\r\n" % len(gz)
        ) + gz
        hello = b"HEAD /api/hello HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n"
        requests = generator.parse_http_stream(first + second + hello, "fixture")
        self.assertEqual([request["stream_offset"] for request in requests], [0, len(first), len(first) + len(second)])
        self.assertEqual(requests[1]["body"], {"model": "m", "messages": []})
        self.assertEqual(requests[1]["content_encoding"], "gzip")
        self.assertEqual(requests[0]["raw_sha256"], hashlib.sha256(first).hexdigest())
        with self.assertRaisesRegex(generator.ProjectionError, "未脱敏"):
            generator.parse_http_stream(first.replace(b"<secret>", b"token"), "fixture")

    def test_version_fingerprint_definition_and_verification(self) -> None:
        policy = generator.load_projection_policy(PROJECTION)
        expected = hashlib.sha256(("59cf53e54c78" + "eh0" + "2.1.285").encode()).hexdigest()[:3]
        self.assertEqual(generator.version_fingerprint(policy, "2.1.285", "abcdefghij"), expected)
        builder = wire_fixture.WireEvidenceBuilder(policy, "2.1.285")
        requests = [
            request
            for raw_list in builder.probes().values()
            for raw in raw_list
            for request in generator.parse_http_stream(raw, "fixture")
            if request["request_target"] == generator.MESSAGES_TARGET
        ]
        self.assertEqual(generator.verify_version_fingerprints(requests, policy, "2.1.285"), len(requests))
        wrong = copy.deepcopy(policy)
        wrong["attestation"]["version_fingerprint"]["salt"] = "000000000000"
        with self.assertRaisesRegex(generator.ProjectionError, "版本指纹"):
            generator.verify_version_fingerprints(requests, wrong, "2.1.285")


class _MemorySource:
    """内存证据来源（多模型投影用例）：probe → 请求字节，按 generator 的来源协议提供请求。"""

    def __init__(self, probes: dict[str, list[bytes]]) -> None:
        self.parsed = {
            probe: [
                {**request, "evidence_path": f"memory/{probe}/conn{index:03d}"}
                for index, raw in enumerate(raws, start=1)
                for request in generator.parse_http_stream(raw, probe)
            ]
            for probe, raws in probes.items()
        }

    def requests(self, probe: str) -> list[dict[str, Any]]:
        if probe not in self.parsed:
            raise generator.ProjectionError(f"缺少 probe：{probe}")
        return self.parsed[probe]

    def messages(self, probe: str) -> list[dict[str, Any]]:
        return [request for request in self.requests(probe) if request["request_target"] == generator.MESSAGES_TARGET]

    def select_messages(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return generator.select_request(self.messages(probe), predicate, description, probe)

    def base_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return generator.select_request(self.requests(probe), predicate, description, probe)

    model_select = base_select

    def model_requests(self, probe: str) -> list[dict[str, Any]]:
        return self.requests(probe)

    def thinking_display_request(self, display: str) -> dict[str, Any]:
        return self.messages(f"{generator.PROBE_THINKING_DISPLAY_PREFIX}{display}")[0]

    def thinking_production_receipt(self) -> dict[str, str]:
        return {"path": "memory/capture-index.json", "sha256": "c" * 64}

    def evidence_ledger(self) -> dict[str, str]:
        return {"path": "memory/atomic-assertion-ledger.json", "sha256": "d" * 64}

    def catalog_evidence(self) -> dict[str, Any]:
        return {"campaign_root": "memory", "successful_attempts": 1, "historical_failed_attempts": 0}


class MultiModelProjectionTests(unittest.TestCase):
    """多模型：非主模型逐场景投影自身证据，登记 server fallback 的模型补锁存场景；主模型复用公共场景。"""

    def test_non_base_models_and_server_fallback(self) -> None:
        policy = generator.load_projection_policy(PROJECTION)
        policy["version"] = "2.1.285"
        probes: dict[str, list[bytes]] = dict(wire_fixture.WireEvidenceBuilder(policy, "2.1.285").probes())
        for model in policy["models"]["ordered"][1:]:
            single = copy.deepcopy(policy)
            single["models"]["ordered"] = [model]
            builder = wire_fixture.WireEvidenceBuilder(single, "2.1.285")
            for probe, raws in builder.probes().items():
                probes[generator.model_probe(policy, model, probe)] = raws
            for entry in policy["models"]["server_fallbacks"]:
                if entry["model"] == model:
                    probes[generator.model_probe(policy, model, entry["probe"])] = [builder.messages(), builder.server_fallback(entry["fallback_model"])]
        source = _MemorySource(probes)
        implementation = generator.build_implementation_policy(source, policy)
        catalog = generator.build_model_capability_catalog(source, policy, implementation["scenarios"], 39)
        models = {item["canonical_model"]: item for item in catalog["models"]}
        self.assertEqual(list(models), policy["models"]["ordered"])
        self.assertEqual({name: item["legacy_retry_fallback_supported"] for name, item in models.items()}, {"claude-sonnet-5": True, "claude-opus-5": False, "claude-fable-5": False})
        self.assertIsNone(models["claude-sonnet-5"]["scenarios"]["server_fallback"])
        self.assertIsNone(models["claude-opus-5"]["scenarios"]["server_fallback"])
        fallback = models["claude-fable-5"]["scenarios"]["server_fallback"]
        self.assertEqual((fallback["model"], fallback["refusal_fallback_value"], fallback["fallback_latched_by_present"]), ("claude-opus-4-8", "true", True))
        self.assertEqual(models["claude-sonnet-5"]["scenarios"]["sdk_cli"], implementation["scenarios"]["sdk_cli"])
        for name, item in models.items():
            with self.subTest(model=name):
                self.assertEqual(item["scenarios"]["sdk_cli"]["model"], name)
                self.assertEqual(len(item["scenarios"]["background"]), 4)
                self.assertEqual(sum(scenario["model"] == name for scenario in item["scenarios"]["background"]), 1)
                self.assertEqual(sorted(item["effort_evidence"]), sorted(policy["efforts"]))
        self.assertEqual(models["claude-opus-5"]["scenarios"]["agent_background"]["model"], "claude-opus-5")
        # 缺一个非主模型 probe 即失败关闭。
        del source.parsed["claude-opus-5-v4-replay-tui"]
        with self.assertRaisesRegex(generator.ProjectionError, "claude-opus-5-v4-replay-tui"):
            generator.build_model_capability_catalog(source, policy, implementation["scenarios"], 39)


if __name__ == "__main__":
    unittest.main()
