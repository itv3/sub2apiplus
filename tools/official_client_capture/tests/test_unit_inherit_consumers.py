"""B5a-08 逐单元承接运行的消费端：P0 v2 证据与收据（签发 finalize、VC-0 收口）、pre-A3 v2 签发与核验。

运行清单 ``mode == unit-inherit`` 时，消费端在原有逐条重验之外另重验形成期证明（决策收据在记录库且自摘要相符、每个复核点
按形成期快照成立、每条承接记录按决策收据登记的上下文重做 check_record）并核对消费时的校时、撤销与批准：
* 正常运行签出 P0 收据与 pre-A3 认证，收口通过，认证核验按历史重放通过；
* 缺决策收据、决策收据被改、消费时凭证过期（样本过旧、批准过期、来源已撤销）一律拒签；
* 混合执行超过 300 秒：按复核点刷新凭证后仍签出 P0 与 pre-A3，原凭证此时已过期，认证核验仍按形成期快照与签发时刻通过。
时间是替身时刻：消费端经验证器的 ``current_time`` 取此刻，测试替换它，不等真实时间。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
from tools.official_client_capture.tests import test_codex_upgrade_pre_a3_certification as pre_a3_tests
from tools.official_client_capture.tests import unit_inherit_fixture as fx
from tools.official_client_capture.tests.test_codex_upgrade_p0_manifest_evidence import IDENTITY, _assemble_p0, _closeout

T0 = fx.T0


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.cases = 0
        self.verifier = receipts._unit_inherit_verifier()
        self.assertIs(certification._unit_inherit_verifier(), self.verifier, "两个消费端加载同一份验证器")
        self.assertEqual(Path(self.verifier.__file__).resolve(), fx.ui.HERE / "unit_inherit.py", "仓库里从 tools/ci 加载")

    def world(self, **options: object) -> fx.World:
        self.cases += 1
        return fx.World(self.base / f"case-{self.cases}", **options)

    def at(self, world: fx.World, moment: float):
        """替身时钟：消费端此刻为 ``moment``，外部校时源也在这一刻刷新样本。"""

        world.sample(moment)
        return mock.patch.object(self.verifier, "current_time", return_value=moment)

    def published(self, world: fx.World, times: tuple[float, float, float] = (T0, T0 + 60, T0 + 120)) -> fx.RunResult:
        result = world.run(world.request(at=times[0]), times=times)
        self.assertEqual(result.problems, [])
        self.assertEqual(result.manifest["mode"], "unit-inherit")
        return result

    def p0_root(self, world: fx.World, result: fx.RunResult) -> Path:
        capture, egress = world.p0_evidence(result.manifest, IDENTITY, world.root / "exported")
        return _assemble_p0(world.root / "p0", capture, egress, dict(IDENTITY))


class P0UnitInheritTests(_Base):
    def test_p0_signs_after_reverifying_the_formation_proof_and_closeout_reverifies_again(self) -> None:
        world = self.world()
        result = self.published(world)
        root = self.p0_root(world, result)
        with self.at(world, T0 + 150):
            receipt = receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
            verified = receipts.verify_p0_manifest_evidence(root, receipts.replay(root, "p0-receipt.json"))
            _closeout(root)
        self.assertEqual((receipt["kind"], receipt["status"]), ("p0_gate", "passed"))
        self.assertEqual(verified["mode"], "unit-inherit")
        self.assertEqual(verified["unit_inherit"]["decision_sha256"], result.decision["decision_sha256"])
        self.assertEqual(verified["gates"]["test-capture-tools"]["inherited"], 3)
        consumptions = sorted((fx.ui.inherit_root(world.store.root) / "consumptions" / result.manifest["run_id"]).glob("p0-receipt-*.json"))
        self.assertEqual(len(consumptions), 3, "签发、重验、收口各留一份消费登记")

    def test_p0_refuses_missing_or_tampered_decision(self) -> None:
        world = self.world()
        result = self.published(world)
        root = self.p0_root(world, result)
        decision = world.store.decision_path(result.manifest["run_id"])
        original = decision.read_bytes()
        decision.unlink()
        with self.at(world, T0 + 150), self.assertRaisesRegex(receipts.VCReceiptError, "形成期证明重验不通过.*决策收据"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        self.assertFalse((root / "p0-receipt.json").exists())
        payload = json.loads(original)
        payload["checkpoints"][1]["authorized"] = False
        decision.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.at(world, T0 + 150), self.assertRaisesRegex(receipts.VCReceiptError, "决策收据"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        decision.write_bytes(original)
        with self.at(world, T0 + 150):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")

    def test_p0_refuses_expired_credentials_at_signing_and_at_closeout(self) -> None:
        world = self.world()
        result = self.published(world)
        root = self.p0_root(world, result)
        world.sample(T0 - 400)
        with mock.patch.object(self.verifier, "current_time", return_value=T0 + 150), \
                self.assertRaisesRegex(receipts.VCReceiptError, "校时"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        with self.at(world, T0 + 3 * 86400), self.assertRaisesRegex(receipts.VCReceiptError, "批准"):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        with self.at(world, T0 + 150):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
        # 签发之后来源运行被撤销：VC-0 收口重验时拒绝（消费时撤销查询）。
        fx.ui.revoke(world.store.root, fx.SOURCE_RUN, reason="签发后撤销", actor="替身操作员", now=T0 + 160)
        with self.at(world, T0 + 170), self.assertRaisesRegex(closeout.VC0CloseoutError, "已撤销"):
            _closeout(root)


class _PreA3Base(_Base):
    """pre-A3 签发要的场景表（替身两个场景加补账场景）、部署收据与策略激活认证。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(certification, "SCENARIOS", pre_a3_tests.FAKE_SCENARIOS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.deployment, self.activation = pre_a3_tests.PreA3CertificationTests._bindings(self, self.base)  # type: ignore[arg-type]

    def issue(self, world: fx.World, result: fx.RunResult) -> dict:
        return certification.issue_certification_from_records(
            world.root / "data" / "staging" / "pre-a3-scenarios", unit_manifest=result.manifest_path, record_store=world.store.root,
            deployment_receipt=self.deployment, policy_activation=self.activation)


class PreA3UnitInheritTests(_PreA3Base):
    def test_pre_a3_signs_from_a_unit_inherit_run_and_verification_replays_history(self) -> None:
        world = self.world(scenarios=certification.scenario_names())
        result = self.published(world)
        with self.at(world, T0 + 150):
            receipt = self.issue(world, result)
        self.assertEqual((receipt["schema_version"], receipt["status"], receipt["failed_scenarios"]),
                         (certification.SCHEMA_VERSION_V2, "passed", []))
        proof = receipt["unit_manifest"]["unit_inherit"]
        self.assertEqual(proof["decision_sha256"], result.decision["decision_sha256"])
        self.assertEqual({row["unit_record"]["disposition"] for row in receipt["scenarios"]}, {"inherited"})
        output = certification.write_certification(world.root / "pre-a3.json", receipt)
        # 认证核验按形成期快照与签发时刻历史重放：十天后批准早已过期，仍然有效。
        with self.at(world, T0 + 10 * 86400):
            certification.verify_certification(output)
        # 认证里的形成期登记被拿掉（重算自摘要）：核验拒绝。
        forged = json.loads(output.read_text(encoding="utf-8"))
        del forged["unit_manifest"]["unit_inherit"]
        forged["receipt_sha256"] = certification._fingerprint({k: v for k, v in forged.items() if k != "receipt_sha256"})
        forged_path = certification.write_certification(world.root / "forged.json", forged)
        with self.assertRaisesRegex(certification.CertificationError, "没有登记签发时的形成期证明"):
            certification.verify_certification(forged_path)
        # 决策收据被删：核验拒绝。
        world.store.decision_path(result.manifest["run_id"]).unlink()
        with self.assertRaisesRegex(certification.CertificationError, "历史重放不通过"):
            certification.verify_certification(output)

    def test_pre_a3_refuses_missing_decision_or_expired_credentials(self) -> None:
        world = self.world(scenarios=certification.scenario_names())
        result = self.published(world)
        with self.at(world, T0 + 3 * 86400), self.assertRaisesRegex(certification.CertificationError, "批准"):
            self.issue(world, result)
        world.sample(T0 - 400)
        with mock.patch.object(self.verifier, "current_time", return_value=T0 + 150), \
                self.assertRaisesRegex(certification.CertificationError, "校时"):
            self.issue(world, result)
        world.store.decision_path(result.manifest["run_id"]).unlink()
        with self.at(world, T0 + 150), self.assertRaisesRegex(certification.CertificationError, "决策收据"):
            self.issue(world, result)


class LongMixedRunConsumersTests(_PreA3Base):
    """混合执行超过 300 秒（T0 排队、T0+200 取锁、T0+400 发布）：P0 与 pre-A3 都在 T0+450 签出。"""

    def test_p0_and_pre_a3_sign_after_a_mixed_run_longer_than_300_seconds(self) -> None:
        world = self.world(scenarios=certification.scenario_names())
        request = world.request(at=T0)
        result = world.run(request, times=(T0, T0 + 200, T0 + 400))
        self.assertEqual(result.problems, [])
        initial = json.loads(request.read_text(encoding="utf-8"))["consumer_clock"]
        with self.assertRaises(fx.fs.ContractError):
            fx.fs.clock_check(initial, T0 + 450)
        root = self.p0_root(world, result)
        with self.at(world, T0 + 450):
            receipts.finalize(root, "p0-facts.json", "p0-receipt.json")
            _closeout(root)
            receipt = self.issue(world, result)
        self.assertEqual(receipt["status"], "passed")
        output = certification.write_certification(world.root / "pre-a3.json", receipt)
        with self.at(world, T0 + 10 * 86400):
            certification.verify_certification(output)


if __name__ == "__main__":
    unittest.main()
