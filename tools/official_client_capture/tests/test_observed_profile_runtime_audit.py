"""运行画像审计组装只能承接服务事实，不能自行合成画像结论。"""

from __future__ import annotations

import unittest

from tools.official_client_capture.build_observed_profile_runtime_audit import (
    RuntimeAuditError,
    build_runtime_audit,
)

IDENTITY = {
    "campaign_id": "codex-0145-to-0147-20260808T103218Z-k24",
    "attempt_id": "20260808T120000Z-abcdef0123456789",
    "run_nonce": "b" * 64,
    "candidate_id": "candidate-20260808T120000Z-k24-r1",
    "target_version": "0.147.0",
    "profile_id": "codex-0.147.0-official-k24-v1",
    "profile_digest": "0" * 64,
    "image_id": "sha256:" + "1" * 64,
    "image_reference": "127.0.0.1:5000/sub2api/codex0147-candidate-k24@sha256:" + "1" * 64,
    "source_tree_sha256": "2" * 64,
    "build_id": "sub2api-codex0147-k24-r1",
    "deployed_version": "0.147.0",
}


def make_fact(**overrides: str) -> dict[str, str]:
    fact = {
        "schema_version": "codex-egress-activation-fact/v1",
        "source": "sub2api-runtime",
        "event_type": "profile_activated",
        "event_id": "a" * 64,
        "observed_at_utc": "2026-08-08T12:01:02.345678Z",
        "profile_mode": "previous",
        "codex_version": IDENTITY["target_version"],
        "profile_id": IDENTITY["profile_id"],
        "profile_digest": IDENTITY["profile_digest"],
        "release_digest": "3" * 64,
        "image_id": IDENTITY["image_id"],
        "image_reference": IDENTITY["image_reference"],
        "source_tree_sha256": IDENTITY["source_tree_sha256"],
        "build_id": IDENTITY["build_id"],
        "deployed_version": IDENTITY["deployed_version"],
    }
    fact.update(overrides)
    return fact


class ObservedProfileRuntimeAuditTest(unittest.TestCase):
    def test_承接服务事实并补齐_attempt_坐标(self) -> None:
        document = build_runtime_audit(make_fact(), **IDENTITY)
        self.assertEqual(document["schema_version"], "codex-egress-runtime-audit/v1")
        self.assertEqual(document["source"], "sub2api-runtime")
        self.assertEqual(document["event_type"], "profile_activated")
        # event_id 与观测时刻只能来自服务，不得由组装侧生成。
        self.assertEqual(document["event_id"], "a" * 64)
        self.assertEqual(document["observed_at_utc"], "2026-08-08T12:01:02.345678Z")
        self.assertEqual(document["attempt_id"], IDENTITY["attempt_id"])
        self.assertEqual(document["run_nonce"], IDENTITY["run_nonce"])

    def test_画像摘要不符时失败关闭(self) -> None:
        with self.assertRaises(RuntimeAuditError):
            build_runtime_audit(make_fact(profile_digest="9" * 64), **IDENTITY)

    def test_版本不符时失败关闭(self) -> None:
        with self.assertRaises(RuntimeAuditError):
            build_runtime_audit(make_fact(codex_version="0.145.0"), **IDENTITY)

    def test_构建期身份缺失时失败关闭(self) -> None:
        for field in (
            "profile_id",
            "image_id",
            "image_reference",
            "source_tree_sha256",
            "build_id",
            "deployed_version",
        ):
            with self.subTest(field=field):
                with self.assertRaises(RuntimeAuditError):
                    build_runtime_audit(make_fact(**{field: ""}), **IDENTITY)

    def test_非服务来源的事实被拒绝(self) -> None:
        for overrides in (
            {"source": "capture-tool"},
            {"event_type": "profile_probed"},
            {"schema_version": "codex-egress-activation-fact/v2"},
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(RuntimeAuditError):
                    build_runtime_audit(make_fact(**overrides), **IDENTITY)

    def test_缺少内容寻址事件编号被拒绝(self) -> None:
        with self.assertRaises(RuntimeAuditError):
            build_runtime_audit(make_fact(event_id="not-a-digest"), **IDENTITY)


if __name__ == "__main__":
    unittest.main()
