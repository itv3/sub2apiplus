"""Claude 的 VC-5 准入（Claude 升级控制面设计 B5b-01，原 B4-01 d 段）：适配器两项受管事实与驱动统一准入的 Claude 版。

真实链部分沿用 B3-01 的真实工作树批次夹具（``test_claude_upgrade_batch._RealTreeBatchCase``）：VC-3／VC-4 批次真实派发到 Store
``candidate_sealed``，再对这个 Campaign 调 Claude 的 ``catalog_stage_profile``（暂存画像、VC-3／VC-4 checkpoint、候选构建收据、
Store 候选冻结与候选源码树逐项重放），并以驱动 ``vc5_admission.Admission`` 走完只读预演 → 显式批准补齐 → 重放同一收据：

* ``validation_only``：夹具本身的用途（共享 Claude 夹具只建这一种用途），整条链全部真实；
* ``production_replacement``：共享夹具建不出这种用途的 Campaign（VC-3 批准门禁要求目标规则证据等级为 verified），这里把总计划、
  候选构建收据与 Store 候选冻结三处的用途一致地替换成 ``production_replacement``（合成夹具），验证用途取自冻结的总计划、
  三处任一处不一致即失败关闭。

准入的签发合同（JWT 验签）、Docker 容器与部署状态三项与客户端无关，由替身给出（与 ``test_vc5_admission`` 同一做法）；驱动的
准入流程、Claude 准入合同名、运行画像副本路径与适配器事实全部真实。单元部分覆盖 Claude 的 ``vc5_official_campaign`` 判据。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import site
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.arm64_capture_driver.driver import vc5_admission as admission
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_baseline
from tools.official_client_capture import claude_upgrade_predecessor
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import claude_upgrade_vc5_admission as claude_vc5
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests.test_vc5_admission import token

CANDIDATE = "claude-cand-r1"
CLAUDE_SCHEMA = "official-client-vc5-admission/v1"
CLAUDE_APPROVAL_SCHEMA = "official-client-vc5-admission-approval/v1"


class ClaudeFacts:
    """驱动准入的事实替身：目标画像与官方证据 Campaign 走真实的 Claude 适配器，签发、容器与部署状态由替身给出。"""

    def __init__(self, config: dict[str, str], request: dict[str, Any]) -> None:
        self.config, self.request = config, request
        self.issued = 0

    def catalog(self) -> dict[str, Any]:
        adapter = uca.adapter_for("claude")
        try:
            return dict(adapter.catalog_stage_profile(Path(self.config["NEWDIR"]), **self.request))
        except uca.Vc5AdmissionFactError as error:
            raise admission.AdmissionError(str(error)) from error

    def official_campaign(self) -> tuple[Path, str | None]:
        subject = admission.ManagedFacts.__new__(admission.ManagedFacts)
        subject.config = self.config
        return subject.official_campaign()

    def state(self) -> dict[str, Any]:
        return {"deployment": "fixture-claude-deploy", "background_validation": "passed"}

    def container(self) -> dict[str, Any]:
        return {"container_id": "fixture-container", "image_id": "fixture-image", "mount_destination": "/capture"}

    def container_profile(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return {"symlink": False, "uid": os.geteuid(), "mode": stat.S_IMODE(path.stat().st_mode),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def issue_token(self, signing: dict[str, str]) -> bytes:
        self.issued += 1
        return token()


@unittest.skipUnless(site.ENABLE_USER_SITE, "批次夹具经 usercustomize 进入动作子进程，要求启用用户 site")
class ClaudeVc5AdmissionChainTests(batch_tests._RealTreeBatchCase):
    """真实 VC-3／VC-4 批次之后的 Claude VC-5 准入（见模块说明）。"""

    def world(self) -> dict[str, Any]:
        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        build_path = claude_upgrade._candidate_build_receipt_path(campaign, CANDIDATE)
        build = json.loads(build_path.read_text(encoding="utf-8"))
        source = Path(build["source"]["root"])
        stage_dir = Path(build["catalog_stage"]["path"]).parent
        request = {
            "campaign_id": batch_tests.CAMPAIGN_ID, "candidate_id": CANDIDATE, "target_version": batch_tests.TARGET,
            "profile_id": f"claude-code-{batch_tests.TARGET}-official", "candidate_source": source,
            "lifecycle_dir": stage_dir.parent.relative_to(source).as_posix(), "candidate_commit": build["source"]["git_commit"],
        }
        staged = json.loads((campaign / claude_upgrade_release.STAGED_RELEASE_ROOT / claude_upgrade_release.STAGED_RECEIPT_FILENAME)
                            .read_text(encoding="utf-8"))
        return {"campaign": campaign, "build": build, "build_path": build_path, "request": request, "staged": staged}

    def catalog(self, world: dict[str, Any], **changes: Any) -> dict[str, Any]:
        return dict(uca.adapter_for("claude").catalog_stage_profile(world["campaign"], **{**world["request"], **changes}))

    @contextlib.contextmanager
    def purpose(self, purpose: str, *, manifest: bool = True, build: bool = True, store: bool = True) -> Iterator[None]:
        """把总计划、候选构建收据与 Store 候选冻结三处的用途替换成 ``purpose``（合成夹具，见模块说明）。"""

        original_manifest = claude_upgrade._require_formal_campaign
        original_validate = vc_artifacts.validate_official_client_candidate_build_receipt
        original_fact = claude_upgrade_baseline._store_candidate_fact

        def fake_manifest(campaign_dir: Path, manifest_value: Any = None) -> dict[str, Any]:
            value = dict(original_manifest(campaign_dir, manifest_value))
            return {**value, "campaign_purpose": purpose} if manifest else value

        def fake_validate(value: Any) -> dict[str, Any]:
            receipt = dict(original_validate(value))
            return {**receipt, "candidate_purpose": purpose} if build else receipt

        def fake_fact(store_value: Any, campaign: Any, candidate_id: str, revision: int) -> dict[str, Any] | None:
            fact = original_fact(store_value, campaign, candidate_id, revision)
            if fact is None or not store:
                return fact
            return {**fact, "payload": {**fact["payload"], "candidate_purpose": purpose}}

        with mock.patch.object(claude_upgrade, "_require_formal_campaign", side_effect=fake_manifest), \
                mock.patch.object(vc_artifacts, "validate_official_client_candidate_build_receipt", side_effect=fake_validate), \
                mock.patch.object(claude_upgrade_baseline, "_store_candidate_fact", side_effect=fake_fact):
            yield

    def admission_config(self, world: dict[str, Any], runroot: Path) -> dict[str, str]:
        compose = self.root / "compose"
        if not compose.is_dir():
            compose.mkdir()
            admission.atomic_write(compose / ".env", ('JWT_SECRET="隔离验收签名密钥"\nADMIN_EMAIL=admin@fixture.invalid\n'
                                                      "POSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture\nPOSTGRES_DB=fixture\n").encode())
            admission.atomic_write(self.root / "signer", b"#!/bin/sh\nexit 1\n", 0o700)
        return {
            "CLIENT": "claude", "D": str(self.data), "RUNROOT": str(runroot), "NEW": batch_tests.CAMPAIGN_ID, "NEWDIR": str(world["campaign"]),
            "CAND": CANDIDATE, "UP": "fixture-claude", "TARGET_VERSION": batch_tests.TARGET,
            "PROFILE_ID": world["request"]["profile_id"], "PROFILE_DIGEST": "b" * 64, "EVIDENCE_DECISION": "recapture",
            "PREDECESSOR_CAMPAIGN": "none", "B": str(self.data / "candidates" / CANDIDATE), "COMPOSE_DIR": str(compose),
            "JWTGEN_BIN": str(self.root / "signer"),
        }

    def admit(self, world: dict[str, Any], runroot: Path) -> tuple[dict[str, Any], dict[str, Any], admission.Admission]:
        """只读预演 → 批准补齐（运行画像副本与管理 token）→ 重放同一收据；返回（预演报告，收据，准入对象）。"""

        config = self.admission_config(world, runroot)
        facts = ClaudeFacts(config, world["request"])
        subject = admission.Admission(config, facts)
        report = subject.inspect()
        self.assertEqual((report["schema_version"], report["status"]), (CLAUDE_SCHEMA, "needs_apply"), report["checks"])
        self.assertIn("install_profile", report["actions"], "运行画像副本由批准补齐装到 Claude 的路径")
        now = datetime.now(timezone.utc)
        approval = runroot.parent / f"{runroot.name}-approval.json"
        admission.write_json(approval, {
            "schema_version": CLAUDE_APPROVAL_SCHEMA, "approved_by": "隔离验收操作者", "approval_id": f"{runroot.name}-approval",
            "approved_at_utc": (now - timedelta(minutes=1)).isoformat(), "expires_at_utc": (now + timedelta(minutes=10)).isoformat(),
            "admission_key": report["admission_key"], "actions": report["actions"], "seal_admission": True,
        })
        receipt = subject.apply(approval)
        self.assertEqual(receipt["schema_version"], CLAUDE_SCHEMA)
        self.assertEqual(subject.consume()["receipt_sha256"], receipt["receipt_sha256"])
        return report, receipt, subject

    def test_validation_only_and_production_replacement_admissions(self) -> None:
        world = self.world()
        campaign, build, staged = world["campaign"], world["build"], world["staged"]
        entry = staged["release_entry"]
        profile_path = campaign / claude_upgrade_release.STAGED_RELEASE_ROOT / entry["profile"]["path"]

        # catalog_stage_profile：两客户端同一组字段，Claude 的画像摘要就是文件 SHA-256，另带 Release 与用途绑定。
        catalog = self.catalog(world)
        self.assertEqual(catalog["profile_bytes"], profile_path.read_bytes())
        self.assertEqual(catalog["profile_digest"], entry["profile"]["sha256"])
        self.assertEqual(catalog["profile_sha256"], hashlib.sha256(catalog["profile_bytes"]).hexdigest())
        self.assertEqual((catalog["build_id"], catalog["image_id"]), (build["build"]["build_id"], build["image"]["image_id"]))
        self.assertEqual(catalog["build_receipt_sha256"], claude_upgrade.file_sha256(world["build_path"]))
        self.assertEqual(catalog["profile_id"], world["request"]["profile_id"], "Claude 画像没有 profile_id：PROFILE_ID 只作标签")
        self.assertEqual(catalog["extra_bindings"], {"release_sha256": entry["release_sha256"], "campaign_purpose": "validation_only"})

        # 每一处重放失败关闭（静态错误码）。
        for changes, code in (
            ({"candidate_commit": "0" * 40}, "candidate_commit_invalid"),
            ({"candidate_id": "claude-cand-other"}, "active_candidate_revision_invalid"),
            ({"target_version": "9.9.9"}, "campaign_binding_invalid"),
            ({"candidate_source": world["request"]["candidate_source"].parent}, "candidate_source_invalid"),
            ({"lifecycle_dir": "docs/egress/lifecycle/other-candidate"}, "candidate_catalog_receipt_drift"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(uca.Vc5AdmissionFactError, f"^{code}$"):
                self.catalog(world, **changes)
        tree_profile = world["request"]["candidate_source"].joinpath("backend", "internal", "officialegress", *Path(entry["profile"]["path"]).parts)
        original = tree_profile.read_bytes()
        tree_profile.write_bytes(original + b" ")
        with self.assertRaisesRegex(uca.Vc5AdmissionFactError, "^candidate_catalog_receipt_drift$"):
            self.catalog(world)
        tree_profile.write_bytes(original)

        # validation_only：驱动统一准入走完（Claude 合同名、运行画像副本路径、绑定 Release 与用途）。
        report, receipt, subject = self.admit(world, self.root / "round-validation")
        self.assertEqual(subject.profile, self.data / "runtime" / f"claude-profile-{batch_tests.TARGET}.json")
        self.assertEqual(subject.profile.read_bytes(), profile_path.read_bytes())
        self.assertEqual(stat.S_IMODE(subject.profile.stat().st_mode), 0o600)
        self.assertEqual((report["bindings"]["campaign_purpose"], report["bindings"]["release_sha256"]),
                         ("validation_only", entry["release_sha256"]))
        self.assertEqual(receipt["effective_parameters"], {"PROFILE_ID": world["request"]["profile_id"],
                                                           "PROFILE_DIGEST": entry["profile"]["sha256"], "OFFICIAL_CAMPAIGN": str(campaign)})

        # production_replacement（合成夹具）：用途取自冻结的总计划，三处一致才放行；准入键随用途变化。
        with self.purpose("production_replacement"):
            catalog = self.catalog(world)
            self.assertEqual(catalog["extra_bindings"]["campaign_purpose"], "production_replacement")
            subject.profile.unlink()
            production, production_receipt, _ = self.admit(world, self.root / "round-production")
            self.assertEqual(production["bindings"]["campaign_purpose"], "production_replacement")
            self.assertNotEqual(production["admission_key"], report["admission_key"])
            self.assertEqual(production_receipt["effective_parameters"]["PROFILE_DIGEST"], entry["profile"]["sha256"])
        for part, code in (("build", "build_receipt_binding_invalid"), ("store", "store_candidate_binding_invalid")):
            with self.subTest(part=part), self.purpose("production_replacement", **{part: False}):
                with self.assertRaisesRegex(uca.Vc5AdmissionFactError, f"^{code}$"):
                    self.catalog(world)
        # 准入报告把事实错误记为 blocked 的静态原因。
        config = self.admission_config(world, self.root / "round-blocked")
        blocked = admission.Admission(config, ClaudeFacts(config, {**world["request"], "candidate_commit": "0" * 40})).inspect()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn({"check": "catalog", "status": "blocked", "reason": "candidate_commit_invalid"}, blocked["checks"])


class ClaudeOfficialCampaignTests(unittest.TestCase):
    """Claude 的 ``vc5_official_campaign``：证据决定与总计划冻结的前序必须一致（见模块说明第二节）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "data"
        self.campaigns = self.data / "evidence" / "campaigns"
        (self.campaigns / "current").mkdir(parents=True)
        (self.campaigns / "previous").mkdir()
        self.binding = {"mode": "read_only_reuse", "campaign_id": "previous", "campaign_identity_sha256": "a" * 64}
        self.manifests = {
            self.campaigns / "current": {"campaign_id": "current", "target_version": "2.1.285", "predecessor": dict(self.binding)},
            self.campaigns / "previous": {"campaign_id": "previous", "target_version": "2.1.285", "predecessor": None},
        }

    def call(self, decision: str, predecessor: str = "previous") -> dict[str, Any]:
        def manifest(path: Path, _value: Any = None) -> dict[str, Any]:
            return dict(self.manifests[Path(path)])

        with mock.patch.object(claude_upgrade, "_require_formal_campaign", side_effect=manifest), \
                mock.patch.object(claude_vc5, "_read_only_store", return_value=(object(), {"campaign_id": "current"})), \
                mock.patch.object(claude_upgrade_predecessor, "vc1_reuse_plan_from_store", return_value={}) as reuse:
            result = dict(uca.adapter_for("claude").vc5_official_campaign(
                self.data, campaign_id="current", target_version="2.1.285", evidence_decision=decision, predecessor_campaign=predecessor,
            ))
        result["reuse_checked"] = reuse.called
        return result

    def test_reuse_requires_the_frozen_predecessor(self) -> None:
        self.assertEqual(self.call("reuse"), {"path": self.campaigns / "previous", "blocked_reason": None, "reuse_checked": True})
        self.assertEqual(self.call("reuse", str(self.campaigns / "previous"))["blocked_reason"], None, "绝对路径同样认")
        blocked = {"path": self.campaigns / "previous", "blocked_reason": "official_reuse_binding_invalid"}
        self.assertEqual({key: self.call("reuse", "other")[key] for key in blocked}, blocked, "参数与冻结的前序不一致")
        self.manifests[self.campaigns / "previous"]["target_version"] = "2.1.226"
        self.assertEqual(self.call("reuse")["blocked_reason"], "official_reuse_binding_invalid", "前序目标版本不同")

    def test_recapture_and_reuse_must_match_the_manifest(self) -> None:
        self.assertEqual(self.call("recapture")["blocked_reason"], "official_reuse_binding_invalid", "总计划冻结了前序却按 recapture 准入")
        self.manifests[self.campaigns / "current"]["predecessor"] = None
        self.assertEqual(self.call("recapture"), {"path": self.campaigns / "current", "blocked_reason": None, "reuse_checked": False})
        self.assertEqual(self.call("reuse")["blocked_reason"], "official_reuse_binding_invalid", "没有冻结前序却按 reuse 准入")

    def test_adapter_members_are_bound_for_both_clients(self) -> None:
        for client, module in (("codex", "tools.official_client_capture.codex_upgrade_client_adapter"),
                               ("claude", "tools.official_client_capture.claude_upgrade_vc5_admission")):
            adapter = uca.adapter_for(client)
            self.assertEqual((adapter.catalog_stage_profile.__module__, adapter.vc5_official_campaign.__module__), (module, module))


if __name__ == "__main__":
    unittest.main()
