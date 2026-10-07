"""Claude VC-1 静态分析动作（Claude 升级控制面设计 B2-11）：``analyze-target`` 的产物、收据、幂等与 seal 的默认静态输入。

* 在 Campaign 的 ``official/`` 下临时目录里分析、写收据后整目录改名到 ``official/static-analysis``；收据绑定冻结收据、基线
  两件、运行时两件与全部产物；重跑只重放收据、不再分析；被中断留下的临时目录先清掉；产物被改动即重放失败（不覆盖）；
* 批次动作：``vc1-01-analyze-target``（排在同批 seal 动作之前），命令是受管 Python 直接调用 ``claude_upgrade.py analyze-target --campaign-dir``；
* seal 不给静态参数时取规范产物，显式给出的与规范产物不同即拒绝；规范收据与冻结的生产现状清单一并交给完整性清单；
* VC-2 分类：``managed_egress_bound`` 的出站 ID 必须在 VC-1 封存的生产现状清单里，没有清单时无法核对（失败关闭）。

``claude_fw_e.analyze_bundles`` 与 containment 子进程用替身（它们的真实行为由 FW-E 与多模块 AST 的测试覆盖；本机没有受管
node 与 TypeScript）；Campaign、冻结目录与 Store 都由 ``plan`` 真实建立。
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_fw_e
from tools.official_client_capture import claude_target_inventory
from tools.official_client_capture import claude_upgrade_classify
from tools.official_client_capture import claude_upgrade_discovery
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_static as static
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests.test_claude_upgrade_campaign import TARGET, CampaignWorldTestCase, _sha256, _write_json

RUNTIME = {
    "node": {"path": "/opt/claude-tooling/node", "sha256": "1" * 64, "version": "v24.21.0"},
    "typescript": {"path": "/opt/claude-tooling/typescript/lib/typescript.js", "sha256": "2" * 64, "version": "5.6.3"},
}


class StaticAnalysisTests(CampaignWorldTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.campaign, self.manifest = self.world.create_preflight()
        self.analyze_calls = 0

    def fake_analyze(self, freeze_root: Path, output_root: Path, anchors: Path, reachability: Path, **kwargs: Any) -> dict[str, Any]:
        self.analyze_calls += 1
        self.assertEqual(freeze_root, self.world.freeze.parent)
        self.assertEqual((kwargs["node_binary"], str(kwargs["typescript_module_path"])), (RUNTIME["node"]["path"], RUNTIME["typescript"]["path"]))
        self.assertFalse(output_root.exists())
        self.assertEqual(output_root.parent, (self.campaign / "official").resolve(strict=False))
        platform_dir = output_root / static.PLATFORM_DIRNAME
        platform_dir.mkdir(parents=True, mode=0o700)
        for name in ("bundle-anchors.json", "bundle-manifest.json", "reachability-index.json", "target-native-ast.json"):
            _write_json(platform_dir / name, {"artifact": name})
        _write_json(
            platform_dir / "target-sink-inventory.json",
            {"schema_version": claude_target_inventory.INVENTORY_SCHEMA, "target_version": TARGET, "sinks": [{"sink_id": "s1"}]},
        )
        static_diff = {
            "target_version": TARGET,
            "target_platforms": {static.PLATFORM: {"binary_sha256": self.manifest["target_sha256"]}},
            "baseline": {"anchors_path": str(anchors), "reachability_path": str(reachability)},
        }
        _write_json(output_root / "static-diff.json", static_diff)
        return static_diff

    def fake_containment(self, temp_root: Path, runtime: Any) -> dict[str, Any]:
        payload = {
            "schema_version": claude_upgrade_discovery.CONTAINMENT_SCHEMA,
            "target_version": TARGET,
            "inventory": {"path": static.OUTPUTS["target_sink_inventory"], "sha256": _sha256(temp_root / static.OUTPUTS["target_sink_inventory"])},
            "completeness": {"result": "passed", "evidence_row_count": 1, "unmatched_sink_ids": []},
            "evidence": [{"sink_id": "s1"}],
        }
        _write_json(temp_root / static.OUTPUTS["sink_containment"], payload)
        return payload

    def analyze(self) -> dict[str, Any]:
        with (
            mock.patch.object(static, "runtime_tools", return_value=RUNTIME),
            mock.patch.object(claude_fw_e, "analyze_bundles", side_effect=self.fake_analyze),
            mock.patch.object(static, "_run_containment", side_effect=self.fake_containment),
        ):
            return static.analyze_target(self.campaign, self.manifest)

    def test_analysis_writes_bound_receipt_and_is_idempotent(self) -> None:
        stale = self.campaign / "official" / ".static-analysis-dead"
        stale.mkdir(parents=True, mode=0o700)
        (stale / "partial.json").write_text("{}", encoding="utf-8")
        result = self.analyze()
        self.assertEqual((result["status"], result["reused"], result["removed_temporaries"]), ("complete", False, [stale.name]))
        self.assertFalse(stale.exists())
        with mock.patch.object(static, "runtime_tools", return_value=RUNTIME):
            receipt = static.load_receipt(self.campaign, self.manifest)
        assert receipt is not None
        self.assertEqual(receipt["official_freeze"], {"path": str(self.world.freeze.resolve()), "sha256": _sha256(self.world.freeze)})
        self.assertEqual(set(receipt["outputs"]), set(static.OUTPUTS))
        self.assertEqual(receipt["containment"]["result"], "passed")
        self.assertEqual((receipt["target_sink_count"], receipt["live_request_count"]), (1, 0))
        self.assertEqual([path.name for path in (self.campaign / "official").iterdir()], ["static-analysis"])
        again = self.analyze()
        self.assertEqual((again["reused"], self.analyze_calls), (True, 1))
        # 产物被改动：重放失败关闭，不覆盖。
        inventory = self.campaign / static.STATIC_ROOT / static.OUTPUTS["target_sink_inventory"]
        inventory.write_text(inventory.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "摘要漂移"):
            self.analyze()

    def test_seal_defaults_to_canonical_outputs_and_binds_inventory(self) -> None:
        arguments = argparse.Namespace(target_inventory=None, sink_containment=None, discovery_dispositions=None)
        data_root = self.world.data.resolve()
        with self.assertRaisesRegex(ConfigurationError, "analyze-target"):
            claude_upgrade_seal._static_inputs(self.campaign, arguments, data_root, TARGET, self.manifest)
        self.analyze()
        with mock.patch.object(static, "runtime_tools", return_value=RUNTIME):
            inputs = claude_upgrade_seal._static_inputs(self.campaign.resolve(), arguments, data_root, TARGET, self.manifest)
        root = (self.campaign / static.STATIC_ROOT).resolve()
        self.assertEqual(inputs["target_inventory_file"], root / static.OUTPUTS["target_sink_inventory"])
        self.assertEqual(inputs["static_analysis_file"], root / static.RECEIPT_NAME)
        self.assertEqual(inputs["production_inventory_path"], (self.campaign / "inputs" / "production-inventory.json").resolve().relative_to(data_root).as_posix())
        other = _write_json(self.campaign / "other-inventory.json", {"schema_version": claude_target_inventory.INVENTORY_SCHEMA, "target_version": TARGET})
        explicit = argparse.Namespace(target_inventory=other, sink_containment=None, discovery_dispositions=None)
        with mock.patch.object(static, "runtime_tools", return_value=RUNTIME):
            with self.assertRaisesRegex(ConfigurationError, "规范产物不一致"):
                claude_upgrade_seal._static_inputs(self.campaign.resolve(), explicit, data_root, TARGET, self.manifest)

    def test_static_action_shape(self) -> None:
        action = static.static_action(self.campaign)
        self.assertEqual((action["action_id"], action["operation"], action["item_ids"]), (static.ACTION_ID, static.ACTION_OPERATION, [static.ACTION_ID]))
        self.assertEqual(action["command"][2:], ["analyze-target", "--campaign-dir", str(self.campaign.resolve())])
        self.assertEqual(Path(action["command"][1]).name, "claude_upgrade.py")

    def test_runtime_tools_must_match_policy_digests(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "缺少受管运行时 node"):
            static.runtime_tools(self.world.data)
        node = self.world.data / "runtime" / "claude-tooling" / "node"
        node.parent.mkdir(parents=True)
        node.write_bytes(b"not the pinned node")
        with self.assertRaisesRegex(ConfigurationError, "不是策略登记的"):
            static.runtime_tools(self.world.data)


class CompletenessAndClassificationTests(unittest.TestCase):
    def test_new_completeness_purposes_are_optional(self) -> None:
        for name in ("static_analysis", "production_inventory"):
            self.assertIn(name, claude_upgrade_evidence.COMPLETENESS_PURPOSES)
            self.assertIn(name, claude_upgrade_evidence.OPTIONAL_COMPLETENESS)

    def test_managed_egress_binding_is_checked_against_production_inventory(self) -> None:
        binding = {"binding_type": "managed_egress_bound", "managed_egress_ids": ["aux-refresh"], "disposition": "non_persona_managed"}

        def problems(value: dict[str, Any], egress: frozenset[str] | None) -> list[str]:
            return [
                code
                for code, _detail in claude_upgrade_classify._binding_problems(
                    value, own_id="d1", partition_ids={"d1"}, rule_ids=set(), target_ids=set(), production_egress_ids=egress
                )
            ]

        self.assertEqual(problems(binding, None), ["managed_egress_unverifiable"])
        self.assertEqual(problems(binding, frozenset({"aux-refresh", "infer-main"})), [])
        self.assertEqual(problems({**binding, "managed_egress_ids": ["ghost-egress"]}, frozenset({"aux-refresh"})), ["managed_egress_unknown"])
        self.assertEqual(problems({**binding, "managed_egress_ids": []}, frozenset({"aux-refresh"})), ["binding_invalid"])


if __name__ == "__main__":
    unittest.main()
