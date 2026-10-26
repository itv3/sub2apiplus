"""合法的 Formal 控制收据与 bootstrap 控制副本构造器（只在测试里，不进受管树）。

背景：严格 subject 读者（``claude_qualification_material_source._retain_frozen_bootstrap_controls``）要求 Formal
Campaign 的总计划 ``control_receipts`` 带齐计时、ARM64 环境、P0 门禁、发布认证四项，Store bootstrap 的
``fw_c_receipts`` 里有它们在 ``inputs/store-bootstrap/`` 下的四份精确字节副本，且各自的原件都在宿主数据根内。
真实 ``create_campaign`` 会写出这些；批次夹具（``_VC2Mixin.build_batch_campaign``）建的世界只有一个只带
``ledger_dir`` 的计时绑定，所以走严格读者时总是失败。

本模块只造这四项原件与副本：

* 计时：数据根内的 Claude 计时账本，经正式 ``timing.checkpoint`` 签出 checkpoint；
* ARM64 环境：只替换宿主观察源（见 ``test_claude_upgrade_arm64_environment_receipt.fake_arm64``），
  正式 ``collect_facts``／``finalize``／``replay`` 与合同校验全部保留；
* P0 门禁：沿 ``control_receipt_fixtures.create_p0_gate_receipt`` 经正式 ``finalize`` 封存，绑定下面这份发布认证；
* 发布认证：按 ``certify_release`` 的 Claude v2 形状写出的夹具文件，指纹由正式 ``_fingerprint`` 算出
  （真实发布认证要在 ARM64 签发，这里与既有 VC-1 录制驱动一样明确是夹具，不代表任何真实认证）。

不改任何受管工具，不放宽任何读者的校验：读者收到的是字节与形状都合法的原件。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.official_client_capture import certify_release
from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt
from tools.official_client_capture.tests.control_receipt_fixtures import create_p0_gate_receipt

# 严格读者按这四个文件名在 ``<Campaign>/inputs/store-bootstrap/`` 下找控制副本。
COPY_FILENAMES = {
    "upgrade_timing": "timing-checkpoint.json",
    "arm64_environment": "arm64-environment.json",
    "p0_gate": "p0-gate.json",
    "release_certification": "release-certification.json",
}


@dataclass(frozen=True)
class BootstrapControls:
    """``control_receipts``：写进总计划的四项绑定；``copies``：文件名 → 副本字节（与原件逐字节相同）。"""

    control_receipts: dict[str, Any]
    copies: dict[str, bytes]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _binding(root: Path, relative: str) -> dict[str, Any]:
    raw = (root / relative).read_bytes()
    return {"path": relative, "sha256": _sha256(raw), "bytes": len(raw)}


@contextlib.contextmanager
def _fake_environment_host(runtime: Path, claude_version: str):
    """进入替身 ARM64 宿主，只替换宿主观察源；与 VC-1 录制驱动 ``_environment_facts`` 同一做法。"""

    from tools.official_client_capture.tests import test_claude_upgrade_arm64_environment_receipt as fixture

    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mock.patch.object(fixture, "CLAUDE_VERSION", claude_version):
        host = fixture.FakeHost()
        with fixture.fake_arm64(host, runtime):
            yield


def build_formal_bootstrap_controls(
    base: Path, *, ledger: Path, upgrade_id: str, campaign_purpose: str, baseline_version: str,
    target_version: str, evidence_decision: str, policy_version: str,
) -> BootstrapControls:
    """在 ``base``（须在宿主数据根内）下造四项控制原件，返回总计划绑定与副本字节。

    ``ledger`` 是已建好的计时账本目录（须在数据根内、已有 VC-0 完成事件）。
    """

    base.mkdir(parents=True, exist_ok=True, mode=0o700)

    # 一、计时：正式 checkpoint；严格读者核计划绑定与字节数，不重放账本头。
    # checkpoint 放进账本自己的 receipts 目录，与 VC-1 录制驱动、正式 plan 的约定一致。
    receipt = timing.checkpoint(ledger, "receipts/vc0-input.json")
    timing_binding = _binding(ledger, "receipts/vc0-input.json")
    plan_sha256 = hashlib.sha256((ledger / "ledger.json").read_bytes()).hexdigest()

    # 二、ARM64 环境：正式 collect／finalize／replay，只换宿主观察源。
    environment = base / "environment-p0"
    environment.mkdir(mode=0o700)
    with _fake_environment_host(base / "environment-runtime", target_version):
        facts = arm.collect_facts(phase="p0", subject_id=upgrade_id, claude_version=target_version)
    (environment / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                            encoding="utf-8")
    (environment / "facts.json").chmod(0o600)
    arm.finalize(environment, "facts.json", "receipt.json")
    environment_receipt = arm.replay(environment, "receipt.json")

    # 三、发布认证：夹具文件，指纹由正式函数算出。
    identity = {"fixture_only": True, "upgrade_id": upgrade_id, "target_version": target_version}
    certification = {
        "schema_version": certify_release.CLIENT_SCHEMAS["claude"], "client": "claude", "status": "active",
        "superseded_by": None, "identity": identity, "policy_version": policy_version,
        "fixture_only": True, "not_a_release_certification": True,
    }
    certification["receipt_sha256"] = certify_release._fingerprint(
        {key: value for key, value in certification.items() if key != "receipt_sha256"}, "claude")
    certification_path = base / "release-certification.json"
    certification_path.write_text(json.dumps(certification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
    certification_path.chmod(0o600)

    # 四、P0 门禁：正式 finalize，绑定上面的发布认证。
    p0_root = base / "p0-gate"
    create_p0_gate_receipt(p0_root, upgrade_id=upgrade_id, baseline_version=baseline_version,
                           target_version=target_version, campaign_purpose=campaign_purpose,
                           release_certification=certification_path)
    p0_receipt = vc_receipt.replay(p0_root.resolve(), "p0-receipt.json")

    control_receipts = {
        "upgrade_timing": {
            "ledger_dir": str(ledger.resolve()), "ledger_plan_sha256": plan_sha256, "receipt": timing_binding,
            "upgrade_id": upgrade_id, "evidence_decision": evidence_decision,
            "checkpoint_head_sha256": receipt["summary"]["head_sha256"],
        },
        "arm64_environment": {
            "evidence_root": str(environment.resolve()), "receipt": _binding(environment, "receipt.json"),
            "subject_id": upgrade_id, "contract_sha256": environment_receipt["contract_sha256"],
            "continuity_identity_sha256": environment_receipt["continuity_identity_sha256"],
        },
        "p0_gate": {
            "evidence_root": str(p0_root.resolve()), "receipt": _binding(p0_root, "p0-receipt.json"),
            "receipt_digest": p0_receipt["receipt_digest"], "upgrade_id": upgrade_id,
        },
        "release_certification": {
            "path": str(certification_path.resolve()), "sha256": _sha256(certification_path.read_bytes()),
            "receipt_sha256": certification["receipt_sha256"], "identity": identity,
        },
    }
    originals: Mapping[str, Path] = {
        "upgrade_timing": ledger / "receipts/vc0-input.json",
        "arm64_environment": environment / "receipt.json",
        "p0_gate": p0_root / "p0-receipt.json",
        "release_certification": certification_path,
    }
    copies = {COPY_FILENAMES[name]: path.read_bytes() for name, path in originals.items()}
    return BootstrapControls(control_receipts, copies)
