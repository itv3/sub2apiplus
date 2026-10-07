"""Claude 运行画像审计（升级控制面设计 B4-03）：激活事实校验、event_id 按 Go 口径复算、声明身份逐字比对与 write-once 输出。

两份样例事实的 event_id 是在本机离线编译运行一段只含 Go 标准库的程序得到的：程序逐字复制
``backend/internal/service/claude_official_egress_activation_fact.go`` 的结构体与 ``claudeOfficialEgressActivationEventID``，
对下面两份事实（第二份专门放了 HTML 字符、U+2028、中文、引号、反斜杠、换行、制表与控制字符）求值。Python 复算必须与之
逐字相等，否则 Go 侧写出的真实事实会被误拒。
"""

from __future__ import annotations

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_build_runtime_audit as audit

ACTIVE_FACT = {
    "schema_version": "claude-egress-activation-fact/v1",
    "source": "sub2api-runtime",
    "event_type": "claude_profile_observed",
    "event_id": "d5ff799f3c76067152401797b9b5d25e5036134618ea240ef04c8e3dcd207e20",
    "observed_at_utc": "2026-08-21T07:14:03Z",
    "profile_mode": "active",
    "claude_version": "2.1.226",
    "profile_digest": "1" * 64,
    "wire_digest": "2" * 64,
    "release_digest": "3" * 64,
    "bundle_digest": "4" * 64,
    "approval_digest": "5" * 64,
    "image_id": "sha256:" + "6" * 64,
    "image_reference": "ghcr.io/itv3/sub2apiplus:v0.2.13-1",
    "source_tree_sha256": "7" * 64,
    "build_id": "build-20260821",
    "deployed_version": "0.2.13-1",
}
LEGACY_FACT = {
    "schema_version": "claude-egress-activation-fact/v1",
    "source": "sub2api-runtime",
    "event_type": "claude_profile_observed",
    "event_id": "160588dcf227e59916e4e62495a639a81235f415f787630f035e2418674f4e09",
    "observed_at_utc": "2026-08-21T07:14:03.12345Z",
    "profile_mode": "legacy",
    "claude_version": "",
    "profile_digest": "",
    "wire_digest": "",
    "release_digest": "",
    "bundle_digest": "",
    "approval_digest": "",
    "image_id": "img<&> 中文",
    "image_reference": 'ref"q\\\n\t',
    "source_tree_sha256": "x\b\f\x01\x7f",
    "build_id": "b",
    "deployed_version": "v",
}
DECLARED = {
    "campaign_id": "claude-code-2_1_226-runtime-audit-fixture",
    "attempt_id": "attempt-0001",
    "run_nonce": "nonce-0001",
    "candidate_id": "candidate-eval",
    "target_version": "2.1.226",
    "profile_digest": "1" * 64,
    "wire_digest": "2" * 64,
    "release_digest": "3" * 64,
    "bundle_digest": "4" * 64,
    "approval_digest": "5" * 64,
    "image_id": "sha256:" + "6" * 64,
    "image_reference": "ghcr.io/itv3/sub2apiplus:v0.2.13-1",
    "source_tree_sha256": "7" * 64,
    "build_id": "build-20260821",
    "deployed_version": "0.2.13-1",
}


def _resealed(fact: dict, **changes) -> dict:
    """改字段后按 Go 口径重算 event_id：模拟服务真的写出了这样一份事实。"""

    updated = dict(fact, **changes)
    updated["event_id"] = audit.activation_event_id(updated)
    return updated


class ActivationFactTests(unittest.TestCase):
    def test_event_id_matches_go_marshal(self) -> None:
        self.assertEqual(audit.activation_event_id(ACTIVE_FACT), ACTIVE_FACT["event_id"])
        self.assertEqual(audit.activation_event_id(LEGACY_FACT), LEGACY_FACT["event_id"])
        self.assertEqual(audit.FACT_FIELDS, tuple(ACTIVE_FACT))
        self.assertEqual(audit.validate_fact(LEGACY_FACT)["profile_mode"], "legacy")

    def test_fact_validation_fails_closed(self) -> None:
        cases = {
            "键集": dict(ACTIVE_FACT, profile_id="claude-code"),
            "event_id 与内容复算不一致": dict(ACTIVE_FACT, build_id="build-other"),
            "schema 不匹配": _resealed(ACTIVE_FACT, schema_version="codex-egress-activation-fact/v1"),
            "来源不是运行中服务": _resealed(ACTIVE_FACT, source="manual"),
            "事件类型不匹配": _resealed(ACTIVE_FACT, event_type="profile_activated"),
            "profile_mode 非法": _resealed(ACTIVE_FACT, profile_mode="candidate"),
            "不是 RFC3339 UTC": _resealed(ACTIVE_FACT, observed_at_utc="2026-08-21T07:14:03+08:00"),
            "不是合法日期时间": _resealed(ACTIVE_FACT, observed_at_utc="2026-02-30T07:14:03Z"),
            "必须是 SHA-256": _resealed(ACTIVE_FACT, release_digest=""),
            "claude_version 非法": _resealed(ACTIVE_FACT, claude_version="2.1"),
            "legacy 事实不应携带": _resealed(LEGACY_FACT, profile_digest="1" * 64),
        }
        for message, fact in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(audit.RuntimeAuditError, message):
                audit.validate_fact(fact)
        non_string = dict(ACTIVE_FACT, build_id=7)
        with self.assertRaisesRegex(audit.RuntimeAuditError, "必须是字符串"):
            audit.validate_fact(non_string)


class RuntimeAuditTests(unittest.TestCase):
    def test_build_runtime_audit_binds_declared_identity(self) -> None:
        document = audit.build_runtime_audit(ACTIVE_FACT, **DECLARED)
        self.assertEqual(document["schema_version"], audit.RUNTIME_AUDIT_SCHEMA)
        self.assertEqual((document["event_id"], document["profile_mode"], document["observed_at_utc"]), (ACTIVE_FACT["event_id"], "active", ACTIVE_FACT["observed_at_utc"]))
        for field, value in DECLARED.items():
            with self.subTest(field=field):
                self.assertEqual(document[field], value)
        self.assertEqual(set(document), {"schema_version", "source", "event_type", "event_id", "profile_mode", "observed_at_utc", *DECLARED})

    def test_legacy_and_mismatches_are_refused(self) -> None:
        with self.assertRaisesRegex(audit.RuntimeAuditError, "legacy 模式不加载 Claude 画像"):
            audit.build_runtime_audit(LEGACY_FACT, **DECLARED)
        cases = {
            "运行时画像版本与目标版本不一致": dict(DECLARED, target_version="2.1.285"),
            "运行时 release_digest 与声明身份不一致": dict(DECLARED, release_digest="9" * 64),
            "运行时 approval_digest 与声明身份不一致": dict(DECLARED, approval_digest="9" * 64),
            "运行时事实的 build_id 与声明身份不一致": dict(DECLARED, build_id="build-other"),
            "声明的 wire_digest 必须是 SHA-256": dict(DECLARED, wire_digest="sha256:" + "2" * 64),
            "candidate_id 格式非法": dict(DECLARED, candidate_id="../x"),
            "run_nonce 不能为空": dict(DECLARED, run_nonce=" "),
        }
        for message, declared in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(audit.RuntimeAuditError, message):
                audit.build_runtime_audit(ACTIVE_FACT, **declared)

    def test_cli_writes_once_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claude-runtime-audit-") as temporary:
            root = Path(temporary)
            fact_path = root / "activation-fact.json"
            fact_path.write_text(json.dumps(ACTIVE_FACT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output = root / "out" / "runtime-audit.json"
            argv = ["--activation-fact", str(fact_path), "--output", str(output)]
            for field, value in DECLARED.items():
                argv.extend([f"--{field.replace('_', '-')}", value])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(argv), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), audit.build_runtime_audit(ACTIVE_FACT, **DECLARED))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaisesRegex(audit.RuntimeAuditError, "拒绝覆盖"):
                audit.main(argv)
            duplicated = root / "duplicated.json"
            duplicated.write_text('{"source": "a", "source": "b"}', encoding="utf-8")
            with self.assertRaisesRegex(audit.RuntimeAuditError, "重复键"):
                audit.load_fact(duplicated)
            link = root / "link.json"
            link.symlink_to(fact_path)
            with self.assertRaisesRegex(audit.RuntimeAuditError, "非符号链接"):
                audit.load_fact(link)


if __name__ == "__main__":
    unittest.main()
