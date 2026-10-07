"""Claude VC-3 画像 producer（Claude 升级控制面设计 B3-03）。

全部离线、零请求。夹具沿用 B3-02 的 VC-1 封存 Campaign，先用 ``classify`` 走完 VC-2（``SPEC-BODY-001`` 判 change、
``SPEC-TOOL-024`` 判 delete，两条规则的 ``profile_paths`` 按 B3-03 的键控 JSON Pointer 登记），再把仓库里真实的 2.1.226
``production_active`` Release 目录与画像（跟踪文件，只读复制）作为当前 Active 交给 ``prepare-profile``。

* 机器草案：policy 由输入派生、补丁为空、联合输入只预填严格端点，门禁 blocked；同输入复用同一 revision。
* ``profile_diff_paths`` 约束复算：版本身份只做精确值替换（版本、平台、二进制摘要与三种条件串）；越出 VC-2 登记范围、
  触碰继承规则、占用版本身份落点、空补丁、模型补丁越界、删除规则后仍有残留引用，都被门禁拒绝。
* 联合制品门禁：SupportEnvelope 覆盖非 migrated_strict 入口、入口别名未闭合、严格端点目标处置不符都被拒绝。
* 完整链：准备批准追加 EvidenceApprovalFact（Store 到 official_sealed）、写 ``approved/<rev>/`` 准备包与
  ``results/<rev>.json``；ProfileApprovalFact 之前仍可起草案、修订并批准新的准备 revision（EvidenceApprovalFact 沿用、
  事实数不增，暂存采用最新的一份）；暂存预览给出联合摘要；暂存批准封存全部联合制品并追加 ProfileApprovalFact（Store 到
  profile_approved）、写阶段结果与 VC-3 checkpoint；批准在事实写入后被 SIGKILL，重跑收敛、事实数不增；再批准逐字复用；
  ProfileApprovalFact 之后再起草案、以不同批准人重复批准一律拒绝。
* 准备批准在 evidence-approve 之后被 SIGKILL：草案仍可起，重跑原批准命令沿用已写的 EvidenceApprovalFact 收敛。
* 画像流程不进 wire／evidence 闭包。
* VC-1 证据来源（B2-07 补充项）：模型能力目录证据的 campaign_root 取 VC-1 证据所在的 Campaign（只读复用时是前序），
  capture manifest 与 CaptureIndex 自报的来源对不上即失败关闭；VC-1 带上非主模型的模型能力取证证据后，Active 的三模型
  投影策略原样投影出三模型目录，缺非主模型证据时一次列全缺的 probe 并失败关闭。
"""

from __future__ import annotations

import collections
import copy
import json
import os
import shutil
import signal
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import claude_upgrade_wire_projection as wire_projection
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_wire_fixture as wire_fixture
from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control.store import ControlStore

REPOSITORY_ROOT = classify_tests.REPOSITORY_ROOT
CATALOG_ROOT = REPOSITORY_ROOT / "backend" / "internal" / "officialegress" / "catalogdata" / "claude"
ACTIVE_CATALOG = CATALOG_ROOT / "release-catalog.json"
ACTIVE_PROFILE = CATALOG_ROOT / "profiles" / "2.1.226" / "e02a3af6fa56cf09b6525d884c9de3f7b76ffe84eb000d92606681b0085b9ab5.json"
ACTIVE_WIRE = CATALOG_ROOT / "wire" / "2.1.226" / "a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d.json"
CAMPAIGN_ID = classify_tests.CAMPAIGN_ID
CHANGE = "SPEC-BODY-001"
DELETE = "SPEC-TOOL-024"
MESSAGES_BINDING = "validation-only:messages-oauth"
MESSAGES_EGRESS = "egress-claude-messages-inference"
PLANNED = {
    CHANGE: ["/rules/spec_id=SPEC-BODY-001"],
    DELETE: sorted(
        [
            "/feature_matrix/rule_ids_by_domain/tool",
            f"/feature_matrix/rule_ids_by_egress/{MESSAGES_EGRESS}",
            "/rules/spec_id=SPEC-TOOL-024",
            f"/strict_endpoints/binding_id={MESSAGES_BINDING}/spec_ids",
        ]
    ),
}
INGRESSES = {"official-count-tokens-oauth": "/v1/messages/count_tokens", "official-messages-oauth": "/v1/messages"}


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    return classify_tests.run_main(argv)


class _ProfileCase(classify_tests._ClassifyCase):
    """在 B3-02 夹具上走完 VC-2，并把真实的 2.1.226 Active 复制进 Campaign。

    VC-1 证据缺省另带主模型 23 个 probe 的合成请求字节与 P 通道（``claude_wire_fixture``），供 Wire 投影；投影策略的机器
    草案取受管树里 2.1.226 的那一份（三个模型），审核人替身改成只保留主模型（夹具缺省不合成非主模型证据；三模型投影见
    ``ReleaseEvidenceTests``）。
    """

    @staticmethod
    def projection_policy() -> dict[str, Any]:
        policy, _binding = claude_upgrade_release.active_projection_policy("2.1.226")
        return claude_upgrade_release.machine_projection_policy(policy, classify_tests.TARGET)

    def build_campaign(self, **kwargs: Any) -> Path:
        if "wire_files" not in kwargs:
            builder = wire_fixture.WireEvidenceBuilder(wire_fixture.single_model_policy(self.projection_policy()), classify_tests.TARGET)
            kwargs["wire_files"] = builder.files()
        return super().build_campaign(**kwargs)

    def approved_vc2(self, **kwargs: Any) -> Path:
        campaign = self.build_campaign(**kwargs)
        draft = self.draft(campaign)
        docs = self.resolved_documents(Path(draft["draft_dir"]), change=(CHANGE,), delete=(DELETE,))
        for row in docs["profile_patches"]["rule_patches"]:
            row["profile_paths"] = list(PLANNED[row["spec_id"]])
        code, revised, stderr = self.revise(campaign, draft["revision"], self.write_documents(docs, self.root / "vc2-resolved"))
        self.assertEqual((code, revised["gate"]["reasons"]), (2, []), stderr)
        code, approved, stderr = run_main(self.approve_argv(campaign, revised["revision"], revised["review_sha256"]))
        self.assertEqual(code, 0, stderr)
        shutil.copyfile(ACTIVE_CATALOG, campaign / "analysis" / "active-release-catalog.json")
        shutil.copyfile(ACTIVE_PROFILE, campaign / "analysis" / "active-profile.json")
        return campaign

    def prepare_draft_argv(self, campaign: Path) -> list[str]:
        return [
            "prepare-profile", "--campaign-dir", str(campaign),
            "--active-release-catalog", str(campaign / "analysis" / "active-release-catalog.json"),
            "--active-profile", str(campaign / "analysis" / "active-profile.json"),
        ]

    def prepare_draft(self, campaign: Path) -> dict[str, Any]:
        code, result, stderr = run_main(self.prepare_draft_argv(campaign))
        self.assertEqual(code, 2, stderr)
        return result

    def privacy_mode(self, campaign: Path) -> str:
        return json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))["configuration"]["privacy_mode"]

    def evidence_id(self) -> str:
        probes = sorted({probe for item in classify_tests._scenarios(classify_tests.TARGET)["evidence_scenarios"] for probe in item["probe_ids"]})
        return f"EVIDENCE-{probes[0]}"

    def resolved_prepare(self, campaign: Path, draft_dir: Path) -> dict[str, dict[str, Any]]:
        """审核人替身：按 VC-2 登记范围给两条受影响规则写补丁，并补齐联合输入。"""

        docs = {name: json.loads((draft_dir / filename).read_text(encoding="utf-8")) for name, filename in profile.PREPARE_FILES.items()}
        active = json.loads(ACTIVE_PROFILE.read_text(encoding="utf-8"))
        endpoint = next(item for item in active["strict_endpoints"] if item["binding_id"] == MESSAGES_BINDING)
        ops = {
            CHANGE: [{"op": "replace", "path": "/rules/spec_id=SPEC-BODY-001/title", "value": "目标版本推理 Body 顶层序列化合同"}],
            DELETE: [
                {"op": "remove", "path": "/rules/spec_id=SPEC-TOOL-024"},
                {
                    "op": "replace",
                    "path": "/feature_matrix/rule_ids_by_domain/tool",
                    "value": [spec for spec in active["feature_matrix"]["rule_ids_by_domain"]["tool"] if spec != DELETE],
                },
                {
                    "op": "replace",
                    "path": f"/feature_matrix/rule_ids_by_egress/{MESSAGES_EGRESS}",
                    "value": [spec for spec in active["feature_matrix"]["rule_ids_by_egress"][MESSAGES_EGRESS] if spec != DELETE],
                },
                {
                    "op": "replace",
                    "path": f"/strict_endpoints/binding_id={MESSAGES_BINDING}/spec_ids",
                    "value": [spec for spec in endpoint["spec_ids"] if spec != DELETE],
                },
            ],
        }
        for row in docs["rule_patches"]["rule_patches"]:
            row.update(ops=ops[row["spec_id"]], evidence_item_ids=[self.evidence_id()], rationale="按目标证据处置画像落点。")
        privacy = self.privacy_mode(campaign)
        capabilities = sorted(
            (
                {
                    "platform": "linux/arm64", "logical_ingress_id": ingress, "protocol_class": "anthropic-messages",
                    "privacy_mode": privacy, "model": "claude-sonnet-5", "feature": "baseline", "endpoint": route,
                }
                for ingress, route in INGRESSES.items()
            ),
            key=lambda item: item["logical_ingress_id"],
        )
        joint = docs["joint_inputs"]
        joint["ingress"].update(
            aliases=[
                {"alias_id": f"alias-{ingress}", "logical_ingress_id": ingress, "physical_route": route, "caller_ids": ["claude-code-cli"]}
                for ingress, route in sorted(INGRESSES.items())
            ],
            entries=[
                {
                    "logical_ingress_id": ingress, "physical_alias_ids": [f"alias-{ingress}"], "caller_ids": ["claude-code-cli"],
                    "adapter_id": "claude-official-adapter", "route_id": f"route-{ingress}", "ingress_kind": "official",
                    "protocol_class": "anthropic-messages", "current_disposition": "migrated_strict",
                }
                for ingress in sorted(INGRESSES)
            ],
            target_dispositions=[{"logical_ingress_id": ingress, "target_disposition": "migrated_strict"} for ingress in sorted(INGRESSES)],
        )
        joint["support"] = {"capabilities": capabilities, "unsupported_conditions": []}
        joint["boundary_assertions"] = [{"id": "outside-fail-close", "facts": {"result": "pass"}}]
        joint["persona_derivation"] = [{"id": "persona-from-active", "facts": {"result": "pass"}}]
        joint["compatibility_boundary"] = [{"id": "response-compat-excluded", "facts": {"result": "pass"}}]
        joint["deployment"] = {
            "rollback_operational_capabilities": capabilities,
            "deployment_traffic_capabilities": capabilities,
            "rollback_target_kind": "legacy_deployment",
            "failure_policy": "persona_fail_close",
        }
        # 夹具缺省只合成主模型的证据：投影策略只保留主模型（画像的 supported_models 随目录派生成一个模型）。
        docs["projection"] = wire_fixture.single_model_policy(docs["projection"])
        return docs

    def write_prepare(self, docs: dict[str, dict[str, Any]], directory: Path) -> Path:
        directory.mkdir(parents=True)
        for name, filename in profile.PREPARE_FILES.items():
            (directory / filename).write_bytes(classify_tests.classify._serialize(docs[name]))
        return directory

    def revise_prepare(self, campaign: Path, parent: str, directory: Path) -> tuple[int, dict | None, str]:
        return run_main(["prepare-profile", "--campaign-dir", str(campaign), "--from-revision", parent, "--revision-input", str(directory)])

    def approve_prepare_argv(self, campaign: Path, revision: str, review: str, approved_by: str = "reviewer-a") -> list[str]:
        return [
            "prepare-profile", "--campaign-dir", str(campaign), "--approve-revision", revision,
            "--approve-review-sha256", review, "--approved-by", approved_by,
        ]

    def prepared(self) -> tuple[Path, dict[str, Any]]:
        """走到准备批准完成：返回 (Campaign, 准备批准输出)。"""

        campaign = self.approved_vc2()
        draft = self.prepare_draft(campaign)
        directory = self.write_prepare(self.resolved_prepare(campaign, Path(draft["draft_dir"])), self.root / "prepare-resolved")
        code, revised, stderr = self.revise_prepare(campaign, draft["revision"], directory)
        self.assertEqual((code, revised["gate"]["reasons"]), (2, []), stderr)
        code, approved, stderr = run_main(self.approve_prepare_argv(campaign, revised["revision"], revised["review_sha256"]))
        self.assertEqual((code, approved["status"]), (0, "profile_prepared"), stderr)
        return campaign, approved

    @staticmethod
    def codes(result: dict[str, Any]) -> set[str]:
        return {reason["code"] for reason in result["gate"]["reasons"]}


class PrepareProfileTests(_ProfileCase):
    """机器草案、补丁派生与 profile_diff_paths 约束、联合输入门禁。"""

    def test_machine_draft_is_blocked_and_idempotent(self) -> None:
        campaign = self.approved_vc2()
        result = self.prepare_draft(campaign)
        self.assertEqual((result["status"], result["revision"], result["reused"]), ("profile_blocked", "rev-0001", False))
        self.assertTrue({"rule_patch_missing", "rationale_placeholder", "patch_evidence_missing", "joint_ingress_missing"} <= self.codes(result))
        # 机器草案的投影策略是 Active 的那份（三个模型），VC-1 没有非主模型的证据：投影失败关闭并一次列全缺的 probe。
        self.assertIn("wire_projection_failed", self.codes(result))
        projection_reason = next(item for item in result["gate"]["reasons"] if item["code"] == "wire_projection_failed")
        self.assertIn("claude-opus-5-v4-replay-baseline", " ".join(projection_reason["sample"]))
        draft_dir = Path(result["draft_dir"])
        projection = json.loads((draft_dir / "wire-projection-policy.json").read_text(encoding="utf-8"))
        self.assertEqual((projection["version"], projection["models"]["ordered"]), (classify_tests.TARGET, ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"]))
        policy = json.loads((draft_dir / "profile-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["rules"]["affected_rules"], sorted([CHANGE, DELETE]))
        self.assertEqual(len(policy["rules"]["target_rules"]), 39)
        self.assertEqual(policy["active"]["profile_sha256"], ACTIVE_PROFILE.stem)
        substitutions = {item["from"]: item["to"] for item in policy["identity_substitutions"]}
        self.assertEqual(substitutions["2.1.226"], classify_tests.TARGET)
        self.assertEqual(substitutions["platform=linux/amd64"], "platform=linux/arm64")
        self.assertEqual(substitutions["binary_sha256=4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555"], "binary_sha256=" + "a" * 64)
        patches = json.loads((draft_dir / "rule-patches.json").read_text(encoding="utf-8"))
        self.assertEqual({row["spec_id"]: (row["planned_paths"], row["ops"]) for row in patches["rule_patches"]}, {spec: (PLANNED[spec], []) for spec in PLANNED})
        joint = json.loads((draft_dir / "joint-inputs.json").read_text(encoding="utf-8"))
        targets = {item["egress_id"]: item["spec_ids"] for item in joint["egress"]["target_dispositions"]}
        self.assertEqual(len(targets), 8)
        self.assertNotIn(DELETE, targets[MESSAGES_EGRESS])
        again = self.prepare_draft(campaign)
        self.assertEqual((again["revision"], again["reused"], again["review_sha256"]), ("rev-0001", True, result["review_sha256"]))

    def test_profile_diff_constraint_is_recomputed(self) -> None:
        campaign = self.approved_vc2()
        draft = self.prepare_draft(campaign)
        docs = self.resolved_prepare(campaign, Path(draft["draft_dir"]))
        code, revised, stderr = self.revise_prepare(campaign, draft["revision"], self.write_prepare(docs, self.root / "ok"))
        self.assertEqual((code, revised["status"], revised["gate"]["reasons"]), (2, "awaiting_profile_preparation_approval", []), stderr)
        diff = revised["profile_diff_paths"]
        self.assertIn("/identity/version", diff)
        self.assertIn("/identity/platform", diff)
        # 模型闭集与能力目录摘要由投影出的模型能力目录派生（model_identity_paths）。
        self.assertIn("/identity/supported_models", diff)
        self.assertIn("/identity/model_capability_catalog_sha256", diff)
        release_summary = revised["gate"]["counts"]["release"]
        self.assertEqual(release_summary["models"], ["claude-sonnet-5"])
        self.assertGreater(release_summary["fingerprint_verified_request_count"], 20)
        self.assertIn("/rules/spec_id=SPEC-TOOL-024", diff)
        self.assertIn("/rules/spec_id=SPEC-BODY-001/title", diff)
        # 继承规则只有版本身份落点变化（适用条件里的 version=／platform=／binary_sha256=）。
        inherited = [path for path in diff if path.startswith("/rules/spec_id=SPEC-HDR-001")]
        self.assertTrue(inherited and all("/applicability/" in path for path in inherited), inherited)
        evidence = self.evidence_id()
        for label, mutate, expected in (
            (
                "越出 VC-2 登记范围",
                lambda d: d["rule_patches"]["rule_patches"][0]["ops"].append({"op": "replace", "path": "/rules/spec_id=SPEC-BODY-002/title", "value": "x"}),
                {"rule_patch_out_of_scope", "inherited_rule_changed"},
            ),
            (
                "占用版本身份落点",
                lambda d: d["model_patch"].update(ops=[{"op": "replace", "path": "/identity/version", "value": "9.9.9"}], evidence_item_ids=[evidence], rationale="x"),
                {"model_patch_out_of_scope", "patch_owner_conflict"},
            ),
            (
                "空补丁",
                lambda d: d["rule_patches"]["rule_patches"][0]["ops"].append(
                    {"op": "replace", "path": "/rules/spec_id=SPEC-BODY-001/domain", "value": "body"}
                ),
                {"patch_noop"},
            ),
            (
                "删除规则后残留引用",
                lambda d: d["rule_patches"]["rule_patches"][1].update(ops=d["rule_patches"]["rule_patches"][1]["ops"][:1]),
                {"profile_rule_reference_stale", "joint_strict_endpoint_mismatch"},
            ),
            (
                "改只读 policy",
                lambda d: d["policy"].update(campaign_purpose="production_replacement"),
                {"policy_drift"},
            ),
            (
                "补丁类型错误",
                lambda d: d["rule_patches"]["rule_patches"][0].update(ops=[{"op": "move", "path": "/rules"}]),
                {"patch_invalid"},
            ),
            (
                "模型补丁碰目录派生落点",
                lambda d: d["model_patch"].update(
                    ops=[{"op": "replace", "path": "/identity/supported_models", "value": ["claude-sonnet-5", "claude-opus-5"]}],
                    evidence_item_ids=[evidence],
                    rationale="x",
                ),
                {"patch_owner_conflict"},
            ),
            (
                "投影策略登记了没有证据的模型",
                lambda d: d.update(projection=self.projection_policy()),
                {"wire_projection_failed"},
            ),
            (
                "版本指纹 salt 与观测不符",
                lambda d: d["projection"]["attestation"]["version_fingerprint"].update(salt="000000000000"),
                {"wire_projection_failed"},
            ),
            (
                "工具目录计数与观测不符",
                lambda d: d["projection"]["selectors"].update(mcp_deferred_tool_count=34),
                {"wire_projection_failed"},
            ),
            (
                "TLS 期望与观测不符",
                lambda d: d["projection"]["tls"].update(psk_modes=[0]),
                {"wire_projection_failed"},
            ),
        ):
            with self.subTest(label=label):
                variant = copy.deepcopy(docs)
                mutate(variant)
                code, result, stderr = self.revise_prepare(campaign, draft["revision"], self.write_prepare(variant, self.root / f"variant-{label}"))
                self.assertEqual((code, result["status"]), (2, "profile_blocked"), stderr)
                self.assertTrue(expected <= self.codes(result), (expected, self.codes(result)))

    def test_joint_inputs_are_cross_checked(self) -> None:
        campaign = self.approved_vc2()
        draft = self.prepare_draft(campaign)
        docs = self.resolved_prepare(campaign, Path(draft["draft_dir"]))
        for label, mutate, expected in (
            (
                "支持范围覆盖非 migrated_strict 入口",
                lambda d: d["joint_inputs"]["ingress"]["target_dispositions"][0].update(target_disposition="retained_legacy"),
                {"joint_support_mismatch"},
            ),
            (
                "入口别名未闭合",
                lambda d: d["joint_inputs"]["ingress"]["aliases"].append(
                    {"alias_id": "alias-orphan", "logical_ingress_id": "official-messages-oauth", "physical_route": "/v1/x", "caller_ids": ["x"]}
                ),
                {"joint_ingress_open"},
            ),
            (
                "严格端点目标处置不符",
                lambda d: next(
                    item for item in d["joint_inputs"]["egress"]["target_dispositions"] if item["egress_id"] == MESSAGES_EGRESS
                ).update(target_disposition="denied", spec_ids=[]),
                {"joint_strict_endpoint_mismatch"},
            ),
            (
                "能力平台不是目标平台",
                lambda d: [item.update(platform="linux/amd64") for item in d["joint_inputs"]["support"]["capabilities"]],
                {"joint_support_mismatch"},
            ),
        ):
            with self.subTest(label=label):
                variant = copy.deepcopy(docs)
                mutate(variant)
                code, result, stderr = self.revise_prepare(campaign, draft["revision"], self.write_prepare(variant, self.root / f"joint-{label}"))
                self.assertEqual((code, result["status"]), (2, "profile_blocked"), stderr)
                self.assertTrue(expected <= self.codes(result), (expected, self.codes(result)))

    def test_profile_module_stays_outside_wire_and_evidence_closures(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        modules = ("tools.official_client_capture.claude_upgrade_profile", "tools.official_client_capture.claude_upgrade_release")
        for layer, roots in (("wire_producer", "wire_roots"), ("evidence_semantics", "evidence_roots")):
            closure = claude_identity.closure(
                policy, REPOSITORY_ROOT, digests, claude_identity._orchestrator_roots(policy, index, policy["orchestrator"][roots]), layer=layer
            )
            for module in modules:
                with self.subTest(layer=layer, module=module):
                    self.assertNotIn(module, {item["module"] for item in closure["symbols"]})
                    self.assertNotIn(module, {item["module"] for item in closure["modules"]})
        for name in ("claude_upgrade_profile.py", "claude_upgrade_release.py"):
            self.assertEqual(claude_identity.classify_path(policy, f"official_client_capture/{name}"), "control")


class ProfileApprovalChainTests(_ProfileCase):
    """准备批准 → 暂存预览 → 暂存批准：两条批准事实、联合制品封存、VC-3 checkpoint 与崩溃收敛。"""

    def test_full_chain_reaches_profile_approved_and_converges(self) -> None:
        campaign, approved = self.prepared()
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "official_sealed")
        store = ControlStore(store_root)
        facts = {fact["fact_kind"]: fact for fact in store.list_facts(CAMPAIGN_ID)}
        evidence_approval = facts["evidence_approved"]["payload"]
        self.assertEqual(evidence_approval["classification_fact_ref"], store.fact_ref(facts["rule_classification_recorded"]))
        self.assertEqual(evidence_approval["reviewer"], "reviewer-a")
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        first = profile.load_prepare_result(campaign, manifest)
        self.assertEqual(first["revision"], approved["revision"])
        target = json.loads((campaign / first["prepared"]["target-profile.json"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(target["identity"]["version"], classify_tests.TARGET)
        self.assertEqual(target["identity"]["platform"], "linux/arm64")
        self.assertNotIn(DELETE, [rule["spec_id"] for rule in target["rules"]])
        derivation = json.loads((campaign / first["prepared"]["profile-derivation.json"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(derivation["unexplained_paths"], [])
        self.assertEqual(sorted(derivation["rule_field_paths"]), sorted([CHANGE, DELETE]))
        # 已批准的 revision 不得以不同批准人重复批准。
        code, _result, stderr = run_main(self.approve_prepare_argv(campaign, approved["revision"], approved["review_sha256"], "reviewer-b"))
        self.assertEqual(code, 1)
        self.assertIn("已由 reviewer-a 批准", stderr)

        # ProfileApprovalFact 之前仍可起草案（同输入复用机器草案）、修订并批准新的准备 revision：
        # EvidenceApprovalFact 沿用第一次的（批准人仍是 reviewer-a、事实数不增），暂存采用最新批准的一份。
        redraft = self.prepare_draft(campaign)
        docs = self.resolved_prepare(campaign, Path(redraft["draft_dir"]))
        retitled = "目标版本推理 Body 顶层序列化合同（复审修订）"
        for row in docs["rule_patches"]["rule_patches"]:
            if row["spec_id"] == CHANGE:
                row["ops"][0]["value"] = retitled
        code, second, stderr = self.revise_prepare(campaign, approved["revision"], self.write_prepare(docs, self.root / "prepare-second"))
        self.assertEqual((code, second["gate"]["reasons"]), (2, []), stderr)
        self.assertNotEqual(second["revision"], approved["revision"])
        code, reapproved, stderr = run_main(self.approve_prepare_argv(campaign, second["revision"], second["review_sha256"], "reviewer-b"))
        self.assertEqual((code, reapproved["status"]), (0, "profile_prepared"), stderr)
        self.assertTrue(reapproved["store"]["evidence_approval_reused"])
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["evidence_approved"], 1)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "official_sealed")
        self.assertEqual(profile.approved_prepare_revisions(campaign), [approved["revision"], second["revision"]])
        prepared = profile.load_prepare_result(campaign, manifest)
        self.assertEqual((prepared["revision"], prepared["approved_by"]), (second["revision"], "reviewer-b"))
        self.assertEqual(prepared["store"]["evidence_approval_fact_ref"], first["store"]["evidence_approval_fact_ref"])
        self.assertNotEqual(prepared["target_profile_sha256"], first["target_profile_sha256"])

        # 暂存预览：联合摘要与对象数；同代重跑逐字复用；错误摘要不能批准。
        code, preview, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual((code, preview["status"]), (2, "awaiting_profile_approval"), stderr)
        self.assertEqual(preview["object_count"], 17)
        self.assertEqual(run_main(["stage-profile", "--campaign-dir", str(campaign)])[1]["joint_sha256"], preview["joint_sha256"])
        code, _result, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign), "--approve-joint-sha256", "0" * 64, "--approved-by", "reviewer-a"])
        self.assertEqual(code, 1)
        self.assertIn("joint_sha256 不一致", stderr)

        # 暂存批准在 profile-approve 之后被硬杀（父监督器上下文）：事实已在，阶段结果与 VC-3 checkpoint 未写。
        approve = ["stage-profile", "--campaign-dir", str(campaign), "--approve-joint-sha256", preview["joint_sha256"], "--approved-by", "reviewer-a"]
        producer = {
            "CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE": "1",
            "CODEX_UPGRADE_CAMPAIGN_RUN_DIR": "control/vc/runs/run-0003",
            "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": "c" * 64,
            "CODEX_UPGRADE_CAMPAIGN_ACTION_ID": "vc3-stage-approve",
        }
        status = seal_tests.run_killed(approve, kill_after="profile-approve", environment=producer)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["profile_approved"], 1)
        self.assertFalse((campaign / profile.STAGE_RESULT_RELATIVE).exists())
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-3").exists())

        code, staged, stderr = run_main(approve)
        self.assertEqual((code, staged["status"]), (0, "complete"), stderr)
        self.assertTrue(staged["store"]["profile_approval_reused"])
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "profile_approved")
        kinds = seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)
        self.assertEqual(
            kinds,
            collections.Counter(
                {"discovery_recorded": 1, "evidence_recorded": 1, "rule_classification_recorded": 1, "evidence_approved": 1, "profile_approved": 1}
            ),
        )
        approval = next(fact for fact in ControlStore(store_root).list_facts(CAMPAIGN_ID) if fact["fact_kind"] == "profile_approved")
        self.assertEqual(approval["producer_run"]["action_id"], "vc3-stage-approve")
        payload = approval["payload"]
        self.assertEqual(payload["approval_purpose"], "validation_only")
        self.assertEqual(len(payload["target_spec_ids"]), 39)
        self.assertEqual(payload["evidence_approval_ref"], prepared["store"]["evidence_approval_fact_ref"])
        snapshot = ControlStore(store_root).load_object(payload["snapshot_ref"])["payload"]
        self.assertEqual(snapshot["profile_digest"], prepared["target_profile_sha256"])
        stage = profile.load_stage_result(campaign, manifest)
        self.assertEqual(stage["prepared_revision"], second["revision"])
        self.assertEqual((stage["reuse_item_ids"], stage["execute_item_ids"]), (stage["inherited_rules"], []))
        _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign, claude_upgrade_vc.vc_campaign_plan(campaign, manifest), "VC-3")
        self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})
        self.assertEqual(checkpoint["reuse_item_ids"], stage["inherited_rules"])
        self.assert_staged_release(campaign, manifest, stage, prepared, store_root)

        # 再批准逐字复用；不同批准人被拒；批准后不能再预览。
        before = {path: claude_upgrade.file_sha256(path) for path in (campaign / profile.STAGE_RESULT_RELATIVE, claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-3"))}
        code, repeated, stderr = run_main(approve)
        self.assertEqual((code, repeated["checkpoint_sha256"]), (0, staged["checkpoint_sha256"]), stderr)
        self.assertEqual({path: claude_upgrade.file_sha256(path) for path in before}, before)
        self.assertEqual(sum(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID).values()), 5)
        code, _result, stderr = run_main([*approve[:-1], "reviewer-b"])
        self.assertEqual(code, 1)
        self.assertIn("另一份 ProfileApprovalFact", stderr)
        code, _result, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 1)
        self.assertIn("画像已批准", stderr)
        # ProfileApprovalFact 之后不再起草案、不再批准新的准备 revision；已批准 revision 的原命令仍逐字重放收敛。
        code, _result, stderr = run_main(self.prepare_draft_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("画像已批准", stderr)
        code, _result, stderr = run_main(self.approve_prepare_argv(campaign, redraft["revision"], redraft["review_sha256"]))
        self.assertEqual(code, 1)
        self.assertIn("画像已批准", stderr)
        code, replayed, stderr = run_main(self.approve_prepare_argv(campaign, second["revision"], second["review_sha256"], "reviewer-b"))
        self.assertEqual((code, replayed["target_profile_sha256"]), (0, prepared["target_profile_sha256"]), stderr)
        self.assertEqual(sum(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID).values()), 5)

    def assert_staged_release(
        self, campaign: Path, manifest: dict[str, Any], stage: dict[str, Any], prepared: dict[str, Any], store_root: Path
    ) -> None:
        """暂存 Release：文件布局镜像仓库 catalogdata，Release 身份按 Go 加载器同一算法复算一致，模型能力目录对象已封存。"""

        receipt = claude_upgrade_release.load_staged_release(campaign, stage["staged_release"])
        entry = receipt["release_entry"]
        root = campaign / claude_upgrade_release.STAGED_RELEASE_ROOT
        profile_raw = (root / entry["profile"]["path"]).read_bytes()
        wire_raw = (root / entry["wire"]["path"]).read_bytes()
        self.assertEqual(entry["profile"]["path"], f"catalogdata/claude/profiles/{classify_tests.TARGET}/{prepared['target_profile_sha256']}.json")
        self.assertEqual(wire_projection.sha256_hex(profile_raw), prepared["target_profile_sha256"])
        wire = json.loads(wire_raw)
        target = json.loads(profile_raw)
        self.assertEqual(wire["identity"]["profile_digest"], prepared["target_profile_sha256"])
        self.assertEqual(wire["identity"]["model_capability_catalog_digest"], target["identity"]["model_capability_catalog_sha256"])
        self.assertEqual(entry["models"], target["identity"]["supported_models"])
        self.assertEqual(entry["models"], [item["canonical_model"] for item in wire["implementation_policy"]["model_catalog"]["models"]])
        catalog_sha = wire_projection.sha256_hex(wire_projection.canonical_bytes(wire["implementation_policy"]["model_catalog"]))
        self.assertEqual(catalog_sha, target["identity"]["model_capability_catalog_sha256"])
        release_sha, bundle_sha = wire_projection.build_release_identity(
            version=classify_tests.TARGET,
            platform="linux/arm64",
            profile_digest=prepared["target_profile_sha256"],
            wire_digest=wire_projection.sha256_hex(wire_raw),
            model_catalog_digest=catalog_sha,
            required_rules_manifest_sha256=entry["required_rules_manifest_sha256"],
            required_rule_count=len(target["rules"]),
        )
        self.assertEqual((entry["release_sha256"], entry["bundle_sha256"]), (release_sha, bundle_sha))
        self.assertEqual((entry["required_rule_count"], entry["strict_endpoint_count"]), (39, len(target["strict_endpoints"])))
        self.assertEqual(receipt["validation_candidate_selector"]["release_sha256"], release_sha)
        catalog_object = ControlStore(store_root).load_object(receipt["store"]["model_capability_catalog_ref"])["payload"]
        self.assertEqual(catalog_object["entries"][0]["facts"]["catalog_sha256"], catalog_sha)
        self.assertEqual(catalog_object["entries"][0]["facts"]["document"], wire["implementation_policy"]["model_catalog"])
        self.assertEqual(stage["store"]["model_capability_catalog_ref"], receipt["store"]["model_capability_catalog_ref"])
        # 结构对照：投影出的 Wire 与已入库的 2.1.226 Wire 键集合一致（Go 加载器按这些键读取）。
        active = json.loads(ACTIVE_WIRE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(wire), sorted(active))
        self.assertEqual(sorted(wire["implementation_policy"]), sorted(active["implementation_policy"]))
        self.assertEqual(sorted(wire["implementation_policy"]["scenarios"]), sorted(active["implementation_policy"]["scenarios"]))
        self.assertEqual(sorted(wire["implementation_policy"]["tool_policy"]), sorted(active["implementation_policy"]["tool_policy"]))
        self.assertEqual(sorted(wire["implementation_policy"]["model_catalog"]), sorted(active["implementation_policy"]["model_catalog"]))
        model = wire["implementation_policy"]["model_catalog"]["models"][0]
        active_model = active["implementation_policy"]["model_catalog"]["models"][0]
        self.assertEqual(sorted(model), sorted(active_model))
        self.assertEqual(sorted(model["scenarios"]), sorted(active_model["scenarios"]))
        self.assertEqual(sorted(wire["implementation_policy"]["model_catalog"]["evidence"]), sorted(active["implementation_policy"]["model_catalog"]["evidence"]))
        self.assertEqual(wire["attestation"], active["attestation"])
        self.assertEqual(len(model["scenarios"]["background"]), 4)

    def test_prepare_approval_converges_after_sigkill(self) -> None:
        campaign = self.approved_vc2()
        draft = self.prepare_draft(campaign)
        directory = self.write_prepare(self.resolved_prepare(campaign, Path(draft["draft_dir"])), self.root / "prepare-resolved")
        code, revised, stderr = self.revise_prepare(campaign, draft["revision"], directory)
        self.assertEqual((code, revised["gate"]["reasons"]), (2, []), stderr)
        approve = self.approve_prepare_argv(campaign, revised["revision"], revised["review_sha256"])
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        status = seal_tests.run_killed(approve, kill_after="evidence-approve")
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["evidence_approved"], 1)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "official_sealed")
        self.assertFalse(profile.prepare_result_path(campaign, revised["revision"]).exists())
        self.assertFalse((campaign / profile.PREPARED_ROOT).exists())
        # 未收尾时暂存没有准备结果可用；草案仍可起（同输入复用机器草案）。
        code, _result, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 1)
        self.assertIn("没有已批准的画像准备结果", stderr)
        self.assertEqual(self.prepare_draft(campaign)["revision"], draft["revision"])
        code, approved, stderr = run_main(approve)
        self.assertEqual((code, approved["status"]), (0, "profile_prepared"), stderr)
        self.assertTrue(approved["store"]["evidence_approval_reused"])
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["evidence_approved"], 1)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertEqual(profile.load_prepare_result(campaign, manifest)["revision"], revised["revision"])
        self.assertTrue((campaign / profile.PREPARED_ROOT / revised["revision"] / "target-profile.json").is_file())

    def test_stage_requires_prepared_package(self) -> None:
        campaign = self.approved_vc2()
        code, _result, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 1)
        self.assertIn("画像准备结果", stderr)
        code, _result, stderr = run_main(["prepare-profile", "--campaign-dir", str(campaign), "--approve-revision", "rev-0001"])
        self.assertEqual(code, 1)
        self.assertIn("--approved-by", stderr)


class ReleaseEvidenceTests(_ProfileCase):
    """VC-1 证据来源（B2-07 补充项）：目录证据的 campaign_root 与 attempt 标签同源；三模型投影消费模型能力取证证据。"""

    def release_evidence(self, campaign: Path) -> claude_upgrade_release.ReleaseEvidence:
        """与 prepare-profile 同一读法：VC-2 阶段结果 → Store 里的 EvidencePackage → VC-1 证据索引。"""

        manifest = claude_upgrade.load_campaign_manifest(campaign)
        store, _store_campaign = claude_upgrade_store.open_campaign_store(campaign, manifest)
        stage = json.loads((campaign / classify_tests.classify.STAGE_RESULT_RELATIVE).read_text(encoding="utf-8"))
        package = store.load_object(dict(stage["store"]["evidence_package_ref"]))["payload"]
        return claude_upgrade_release.load_release_evidence(campaign, store, package, stage)

    def test_catalog_evidence_root_on_captured_campaign(self) -> None:
        evidence = self.release_evidence(self.approved_vc2())
        own = f"evidence/campaigns/{CAMPAIGN_ID}"
        self.assertEqual((evidence.campaign_root, evidence.evidence_campaign_root), (own, own))
        source = claude_upgrade_release.UpgradeEvidenceSource(evidence, wire_fixture.single_model_policy(self.projection_policy()))
        self.assertEqual(source.catalog_evidence()["campaign_root"], own)

    def test_catalog_evidence_root_follows_read_only_reuse(self) -> None:
        """只读复用：VC-1 证据（含 capture manifest）在前序 Campaign，目录证据的 campaign_root 记前序、attempt 标签取同一份
        capture manifest；实现策略的 evidence_ledger 仍是本 Campaign 的 VC-2 副本。"""

        evidence = self.release_evidence(self.approved_vc2(official_reuse=True))
        own, predecessor = f"evidence/campaigns/{CAMPAIGN_ID}", f"evidence/campaigns/{CAMPAIGN_ID}-predecessor"
        self.assertEqual((evidence.campaign_root, evidence.evidence_campaign_root), (own, predecessor))
        self.assertTrue(evidence.capture_manifest["path"].startswith(predecessor + "/"))
        self.assertTrue(evidence.ledger["path"].startswith(own + "/"))
        catalog = claude_upgrade_release.UpgradeEvidenceSource(evidence, wire_fixture.single_model_policy(self.projection_policy())).catalog_evidence()
        self.assertEqual(catalog["campaign_root"], predecessor)
        self.assertTrue(catalog["attempts"])
        self.assertTrue(all(item["attempt_id"].startswith("attempt-fixture/") for item in catalog["attempts"]))

    def test_evidence_campaign_root_requires_one_source(self) -> None:
        manifest = {"campaign_id": "c-pre", "attempt": {"attempt_id": "attempt-1"}}
        index = {"campaign_id": "c-pre", "attempt": {"attempt_id": "attempt-1"}}
        binding = {"path": "evidence/campaigns/c-pre/official/seal/g1/drafts/capture-manifest.json"}
        self.assertEqual(claude_upgrade_release._evidence_campaign_root(manifest, binding, index), "evidence/campaigns/c-pre")
        cases = {
            "自报 Campaign 与路径不同": (dict(manifest, campaign_id="c-other"), binding, dict(index, campaign_id="c-other"), "自报的 Campaign"),
            "缺自报 Campaign": ({"attempt": manifest["attempt"]}, binding, index, "自报的 Campaign"),
            "不在 evidence/campaigns 下": (manifest, {"path": "runs/c-pre/capture-manifest.json"}, index, "evidence/campaigns"),
            "CaptureIndex 来自别的 Campaign": (manifest, binding, dict(index, campaign_id="c-other"), "CaptureIndex"),
            "CaptureIndex 来自别的 attempt": (manifest, binding, dict(index, attempt={"attempt_id": "attempt-2"}), "CaptureIndex"),
        }
        for label, (bad_manifest, bad_binding, bad_index, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ConfigurationError, message):
                claude_upgrade_release._evidence_campaign_root(bad_manifest, bad_binding, bad_index)

    def test_three_model_projection_consumes_model_capability_evidence(self) -> None:
        """VC-1 带上非主模型的模型能力取证证据（EVIDENCE-<模型>-<基础 probe>）后，Active 的三模型投影策略原样投影出三模型
        目录；只带主模型证据时，缺的非主模型 probe 一次列全并失败关闭。"""

        policy = self.projection_policy()
        self.assertEqual(policy["models"]["ordered"], ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"])
        builder = wire_fixture.WireEvidenceBuilder(policy, classify_tests.TARGET)
        evidence = self.release_evidence(self.approved_vc2(wire_files=builder.files(models=True)))
        projection = claude_upgrade_release.project_catalog(evidence, policy, target_version=classify_tests.TARGET, required_rule_count=40)
        models = projection.catalog["models"]
        self.assertEqual([item["canonical_model"] for item in models], policy["models"]["ordered"])
        self.assertIsNone(models[0]["scenarios"]["server_fallback"])
        self.assertIsNone(models[1]["scenarios"]["server_fallback"])
        self.assertIsNotNone(models[2]["scenarios"]["server_fallback"])
        catalog_evidence = projection.catalog["evidence"]
        self.assertEqual(catalog_evidence["successful_attempts"], len(wire_projection.catalog_probes(policy)))
        attempts = {item["attempt_id"] for item in catalog_evidence["attempts"]}
        self.assertIn("attempt-fixture/claude-opus-5-v4-replay-tui", attempts)
        self.assertIn("attempt-fixture/claude-fable-5-v4-bash", attempts)
        self.assertEqual(claude_upgrade_release.model_identity(projection)["supported_models"], policy["models"]["ordered"])

    def test_three_model_projection_lists_missing_model_probes(self) -> None:
        """只带主模型证据（夹具缺省）时，三模型投影策略缺的 33 个非主模型 probe 一次列全并失败关闭。"""

        policy = self.projection_policy()
        primary_only = self.release_evidence(self.approved_vc2())
        with self.assertRaises(claude_upgrade_release.ProjectionError) as raised:
            claude_upgrade_release.project_catalog(primary_only, policy, target_version=classify_tests.TARGET, required_rule_count=40)
        missing = [probe for probe in wire_projection.catalog_probes(policy) if probe.startswith(("claude-opus-5-", "claude-fable-5-"))]
        self.assertEqual(len(missing), 33)
        for probe in missing:
            self.assertIn(probe, str(raised.exception))


class PointerAndDiffTests(unittest.TestCase):
    """键控 JSON Pointer、补丁应用与差异落点。"""

    def test_keyed_lists_apply_and_diff(self) -> None:
        document = {"rules": [{"spec_id": "SPEC-A-001", "title": "a"}, {"spec_id": "SPEC-B-001", "title": "b"}], "path/x": {"~k": [1, 2]}}
        target = copy.deepcopy(document)
        profile.apply_op(target, {"op": "replace", "path": "/rules/spec_id=SPEC-A-001/title", "value": "A"})
        profile.apply_op(target, {"op": "add", "path": "/rules/spec_id=SPEC-AB-001", "value": {"spec_id": "SPEC-AB-001", "title": "ab"}})
        profile.apply_op(target, {"op": "remove", "path": "/rules/spec_id=SPEC-B-001"})
        profile.apply_op(target, {"op": "add", "path": "/path~1x/~0k/-", "value": 3})
        self.assertEqual([rule["spec_id"] for rule in target["rules"]], ["SPEC-A-001", "SPEC-AB-001"])
        self.assertEqual(
            profile.diff_paths(document, target),
            ["/path~1x/~0k", "/rules/spec_id=SPEC-A-001/title", "/rules/spec_id=SPEC-AB-001", "/rules/spec_id=SPEC-B-001"],
        )
        for op, message in (
            ({"op": "replace", "path": "/rules/0/title", "value": "x"}, "键字段 spec_id"),
            ({"op": "add", "path": "/rules/spec_id=SPEC-A-001", "value": {"spec_id": "SPEC-A-001"}}, "已存在"),
            ({"op": "add", "path": "/rules/spec_id=SPEC-C-001", "value": {"spec_id": "SPEC-X-001"}}, "等于路径里的键"),
            ({"op": "remove", "path": ""}, "文档根"),
        ):
            with self.subTest(op=op):
                with self.assertRaisesRegex(profile.PatchError, message):
                    profile.apply_op(copy.deepcopy(document), op)


class ActiveInputSourceTests(unittest.TestCase):
    """Active 输入的来源（B2-11 第二部分）：冻结副本优先；带控制收据的 Campaign 没有冻结副本即拒绝，不接受命令行回退；
    只有不带控制收据的旧形状 Campaign 用命令行给出的 Campaign 内文件。"""

    def test_control_receipt_campaigns_never_fall_back_to_the_command_line(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory).resolve() / "campaign"
            (campaign / "analysis").mkdir(parents=True)
            catalog = campaign / "analysis" / "active-release-catalog.json"
            active = campaign / "analysis" / "active-profile.json"
            catalog.write_text("{}", encoding="utf-8")
            active.write_text("{}", encoding="utf-8")
            legacy = {"campaign_id": "c1", "inputs": {}, "control_receipts": {}}
            self.assertEqual(profile._active_paths(campaign, legacy, catalog, active), (catalog, active), "旧形状 Campaign 照旧取命令行文件")
            with self.assertRaisesRegex(ConfigurationError, "必须提供 --active-release-catalog"):
                profile._active_paths(campaign, legacy, None, None)
            created = {**legacy, "control_receipts": {"upgrade_timing": {"receipt": "x"}}}
            for given in ((catalog, active), (None, None)):
                with self.subTest(given=bool(given[0])), self.assertRaisesRegex(ConfigurationError, "不接受命令行回退"):
                    profile._active_paths(campaign, created, *given)


if __name__ == "__main__":
    unittest.main()
