"""A2.6：工具身份策略的兼容收据与激活认证。

* ``policy-compatibility-receipt/v1``：策略文件（``tool_identity_policy_v2.json``）内容变化时
  必须升级 ``policy_version`` 并出具本收据。它在同一棵受管树上分别按旧、新策略计算四层身份，
  逐文件列出分层变化、新登记与移除的路径、按旧策略仍会落入默认层的路径，以及既有 Campaign 的
  处置：Campaign 永远使用自身冻结的 policy，新策略只用于之后创建的 Campaign。
* ``policy-activation-certification/v1``：授权某个 ``policy_sha256`` 用于 A2.5 路径认证与 A3b 真实
  执行，直到 C 的最终发布认证替换。它绑定当前 ARM64 部署收据（五摘要必须等于当前工具身份）与
  兼容收据；``superseded_by`` 为空才有效。

两个命令都只读工具树与收据，不触碰 Campaign，也不产生模型请求。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

POLICY_COMPATIBILITY_SCHEMA = "policy-compatibility-receipt/v1"
POLICY_ACTIVATION_SCHEMA = "policy-activation-certification/v1"
DEPLOY_RECEIPT_SCHEMA = "codex-arm64-supervisor-enable/v1"
IDENTITY_FIELDS = (
    "policy_sha256",
    "wire_producer_sha256",
    "evidence_semantics_sha256",
    "control_sha256",
    "tool_files_sha256",
)
DEFAULT_AUTHORIZED_SCOPES = ("A2.5", "A3b")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024


class PolicyCertificationError(ValueError):
    """策略未变化、版本未升级、部署收据与当前工具不一致或收据摘要漂移。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    return codex_upgrade._fingerprint(value)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PolicyCertificationError(f"{label}必须是可信绝对普通文件：{path}")
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise PolicyCertificationError(f"{label}超过大小上限：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyCertificationError(f"{label}不是有效 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise PolicyCertificationError(f"{label}必须是 JSON 对象：{path}")
    return payload


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise PolicyCertificationError("输出路径必须是绝对路径")
    if path.exists() or path.is_symlink():
        raise PolicyCertificationError(f"不可变收据已存在：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def current_identity() -> dict[str, Any]:
    """当前受管树的五摘要（含 ``tool_files_sha256`` 别名）与策略版本。"""

    identity = codex_upgrade._tool_identity(include_git=False)
    return {
        "policy_version": int(identity["policy_version"]),
        "policy_sha256": str(identity["policy_sha256"]),
        "wire_producer_sha256": str(identity["wire_producer_sha256"]),
        "evidence_semantics_sha256": str(identity["evidence_semantics_sha256"]),
        "control_sha256": str(identity["control_sha256"]),
        "tool_files_sha256": str(identity["files_sha256"]),
    }


def _five(identity: Mapping[str, Any]) -> dict[str, str]:
    return {name: str(identity.get(name)) for name in IDENTITY_FIELDS}


def _self_check(payload: Mapping[str, Any], label: str) -> None:
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    digest = payload.get("receipt_sha256")
    if not isinstance(digest, str) or _fingerprint(unsigned) != digest:
        raise PolicyCertificationError(f"{label}自摘要不一致")


# ---------------------------------------------------------------------------
# 兼容收据
# ---------------------------------------------------------------------------


def _layer_map(grouped: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for layer in (*tip.LAYERS, "ignored"):
        for entry in grouped.get(layer, []):
            mapping[str(entry["path"])] = layer
    return mapping


def _explicit_files(policy: Mapping[str, Any]) -> set[str]:
    explicit: set[str] = set()
    for layer in tip.LAYERS:
        explicit.update(policy["layers"][layer].get("files", []))
    return explicit


def build_compatibility_receipt(
    previous_policy: Path,
    *,
    current_policy: Path | None = None,
    campaign_dirs: Iterable[Path] = (),
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """在同一棵受管树上比较旧、新策略，产出兼容收据（不落盘）。"""

    try:
        previous = tip.load_policy(Path(previous_policy))
        current = tip.load_policy(Path(current_policy) if current_policy is not None else None)
    except tip.ToolIdentityPolicyError as error:
        raise PolicyCertificationError(f"策略文件无法加载：{error}") from error
    if previous["policy_sha256"] == current["policy_sha256"]:
        raise PolicyCertificationError("策略未变化，无需兼容收据")
    if int(current["policy_version"]) <= int(previous["policy_version"]):
        raise PolicyCertificationError(
            f"policy_version 必须严格升级：{previous['policy_version']} → {current['policy_version']}"
        )
    tool_root = Path(codex_upgrade.__file__).resolve().parent
    entries = codex_upgrade._tool_tree_entries(tool_root)
    previous_layers = tip.layer_entries(previous, entries)
    current_layers = tip.layer_entries(current, entries)
    previous_map = _layer_map(previous_layers)
    current_map = _layer_map(current_layers)
    reclassified = sorted(
        (
            {"path": path, "previous_layer": previous_map[path], "current_layer": current_map[path]}
            for path in current_map
            if path in previous_map and previous_map[path] != current_map[path]
        ),
        key=lambda item: item["path"],
    )
    previous_explicit = _explicit_files(previous)
    current_explicit = _explicit_files(current)
    try:
        previous_identity = tip.compute_identity_v2(previous, tool_root, entries)
        current_identity_v2 = tip.compute_identity_v2(current, tool_root, entries)
    except tip.ToolIdentityPolicyError as error:
        raise PolicyCertificationError(f"策略身份无法计算：{error}") from error
    campaigns: list[dict[str, Any]] = []
    for campaign_dir in campaign_dirs:
        manifest_path = Path(campaign_dir) / "campaign.json"
        manifest = _read_json(manifest_path, "Campaign 清单")
        frozen = manifest.get("tool_identity") if isinstance(manifest.get("tool_identity"), Mapping) else {}
        frozen_policy = frozen.get("policy_sha256")
        if frozen_policy == previous["policy_sha256"]:
            disposition = "retain_frozen_policy"
        elif frozen_policy is None:
            disposition = "v1_identity_unaffected"
        elif frozen_policy == current["policy_sha256"]:
            disposition = "already_current_policy"
        else:
            disposition = "other_policy_retain_frozen"
        campaigns.append(
            {
                "campaign_dir": str(Path(campaign_dir).resolve(strict=True)),
                "campaign_id": manifest.get("campaign_id"),
                "frozen_policy_sha256": frozen_policy,
                "frozen_policy_version": frozen.get("policy_version"),
                "disposition": disposition,
            }
        )
    receipt = {
        "schema_version": POLICY_COMPATIBILITY_SCHEMA,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "tool_files_sha256": _fingerprint({"entries": entries}),
        "previous": {
            "policy_version": int(previous["policy_version"]),
            "policy_sha256": previous["policy_sha256"],
            "path": previous["policy_path"],
        },
        "current": {
            "policy_version": int(current["policy_version"]),
            "policy_sha256": current["policy_sha256"],
            "path": current["policy_path"],
        },
        "reclassified_paths": reclassified,
        "newly_registered_files": sorted(current_explicit - previous_explicit),
        "unregistered_files": sorted(previous_explicit - current_explicit),
        "defaulted_paths_under_previous_policy": sorted(previous_identity["defaulted_paths"]),
        "defaulted_paths_under_current_policy": sorted(current_identity_v2["defaulted_paths"]),
        "identity_under_previous_policy": {
            name: previous_identity[name]
            for name in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256")
        },
        "identity_under_current_policy": {
            name: current_identity_v2[name]
            for name in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256")
        },
        "layer_counts_under_current_policy": current_identity_v2["layer_counts"],
        "existing_campaigns": campaigns,
        "rule": (
            "Campaign 永远使用自身冻结的 policy；既有 v2 Campaign 不换策略，新策略只用于之后创建的 Campaign；"
            "v1 Campaign 按整树身份不受影响。"
        ),
    }
    receipt["receipt_sha256"] = _fingerprint(receipt)
    return receipt


def load_compatibility_receipt(path: Path) -> dict[str, Any]:
    payload = _read_json(Path(path), "策略兼容收据")
    if payload.get("schema_version") != POLICY_COMPATIBILITY_SCHEMA:
        raise PolicyCertificationError("策略兼容收据 schema 非法")
    _self_check(payload, "策略兼容收据")
    return payload


# ---------------------------------------------------------------------------
# 激活认证
# ---------------------------------------------------------------------------


def load_deployment_receipt(path: Path, *, expected_identity: Mapping[str, Any]) -> dict[str, Any]:
    """部署收据必须通过且五摘要等于期望身份。"""

    payload = _read_json(Path(path), "ARM64 部署收据")
    if payload.get("schema_version") != DEPLOY_RECEIPT_SCHEMA or payload.get("status") != "passed":
        raise PolicyCertificationError("部署收据 schema 非法或未通过")
    if _five(payload) != _five(expected_identity):
        raise PolicyCertificationError("部署收据五摘要与当前工具身份不一致；先部署当前工具树再认证")
    if payload.get("policy_version") != expected_identity.get("policy_version"):
        raise PolicyCertificationError("部署收据 policy_version 与当前策略不一致")
    return payload


def build_activation_certification(
    deployment_receipt: Path,
    compatibility_receipt: Path,
    *,
    authorized_scopes: Iterable[str] = DEFAULT_AUTHORIZED_SCOPES,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """授权当前策略用于 A2.5 与 A3b：绑定部署收据与兼容收据（不落盘）。"""

    identity = current_identity()
    deployment_path = Path(deployment_receipt).resolve(strict=True)
    deployment = load_deployment_receipt(deployment_path, expected_identity=identity)
    compatibility_path = Path(compatibility_receipt).resolve(strict=True)
    compatibility = load_compatibility_receipt(compatibility_path)
    if (
        compatibility["current"]["policy_sha256"] != identity["policy_sha256"]
        or int(compatibility["current"]["policy_version"]) != identity["policy_version"]
    ):
        raise PolicyCertificationError("兼容收据的新策略不是当前策略")
    scopes = sorted({str(item) for item in authorized_scopes if str(item)})
    if not scopes:
        raise PolicyCertificationError("authorized_scopes 不能为空")
    receipt = {
        "schema_version": POLICY_ACTIVATION_SCHEMA,
        "status": "active",
        "activated_at_utc": observed_at_utc or _utc_now(),
        "policy_version": identity["policy_version"],
        "policy_sha256": identity["policy_sha256"],
        "identity": _five(identity),
        "deployment_receipt": {
            "path": str(deployment_path),
            "sha256": codex_upgrade.file_sha256(deployment_path),
            "created_at_utc": deployment.get("created_at_utc"),
        },
        "compatibility_receipt": {
            "path": str(compatibility_path),
            "sha256": codex_upgrade.file_sha256(compatibility_path),
            "previous_policy_sha256": compatibility["previous"]["policy_sha256"],
        },
        "authorized_scopes": scopes,
        "superseded_by": None,
        "supersession_rule": "C 阶段的 tool-release-certification/v1 签发后本认证失效",
    }
    receipt["receipt_sha256"] = _fingerprint(receipt)
    return receipt


def verify_activation_certification(
    path: Path,
    *,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """校验激活认证：schema、状态、自摘要、五摘要等于期望身份、部署收据文件仍在且摘要一致。"""

    payload = _read_json(Path(path), "策略激活认证")
    if payload.get("schema_version") != POLICY_ACTIVATION_SCHEMA:
        raise PolicyCertificationError("策略激活认证 schema 非法")
    _self_check(payload, "策略激活认证")
    if payload.get("status") != "active" or payload.get("superseded_by") is not None:
        raise PolicyCertificationError("策略激活认证不是 active 或已被替换")
    identity = expected_identity or current_identity()
    if _five(payload.get("identity") or {}) != _five(identity):
        raise PolicyCertificationError("策略激活认证五摘要与当前工具身份不一致")
    if payload.get("policy_sha256") != identity["policy_sha256"] or payload.get("policy_version") != identity["policy_version"]:
        raise PolicyCertificationError("策略激活认证的策略不是当前策略")
    deployment = payload.get("deployment_receipt") or {}
    deployment_path = Path(str(deployment.get("path", "")))
    if (
        not deployment_path.is_absolute()
        or deployment_path.is_symlink()
        or not deployment_path.is_file()
        or codex_upgrade.file_sha256(deployment_path) != deployment.get("sha256")
    ):
        raise PolicyCertificationError("策略激活认证绑定的部署收据缺失或摘要漂移")
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2.6：策略兼容收据与策略激活认证。")
    subparsers = parser.add_subparsers(dest="action", required=True)
    compatibility = subparsers.add_parser("compatibility", help="比较旧新策略并出具 policy-compatibility-receipt/v1")
    compatibility.add_argument("--previous-policy", type=Path, required=True, help="旧策略文件（例如从 git 历史导出的副本）")
    compatibility.add_argument("--current-policy", type=Path, help="默认受管目录内的当前策略")
    compatibility.add_argument("--campaign-dir", type=Path, action="append", default=[], help="可重复；登记既有 Campaign 的处置")
    compatibility.add_argument("--output", type=Path, required=True)
    activation = subparsers.add_parser("activation", help="出具 policy-activation-certification/v1")
    activation.add_argument("--deployment-receipt", type=Path, required=True)
    activation.add_argument("--compatibility-receipt", type=Path, required=True)
    activation.add_argument("--authorized-scope", action="append", default=[], help="默认 A2.5 与 A3b")
    activation.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-activation", help="只读校验激活认证是否对当前工具有效")
    verify.add_argument("--certification", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action == "compatibility":
            receipt = build_compatibility_receipt(
                arguments.previous_policy,
                current_policy=arguments.current_policy,
                campaign_dirs=arguments.campaign_dir,
            )
            _write_once(arguments.output.resolve(strict=False), receipt)
            result = {k: v for k, v in receipt.items() if k not in {"reclassified_paths", "existing_campaigns"}}
            result["reclassified_count"] = len(receipt["reclassified_paths"])
            result["output"] = str(arguments.output)
        elif arguments.action == "activation":
            receipt = build_activation_certification(
                arguments.deployment_receipt,
                arguments.compatibility_receipt,
                authorized_scopes=arguments.authorized_scope or DEFAULT_AUTHORIZED_SCOPES,
            )
            _write_once(arguments.output.resolve(strict=False), receipt)
            result = {**receipt, "output": str(arguments.output)}
        else:
            result = verify_activation_certification(arguments.certification)
            result = {"status": "valid", "policy_sha256": result["policy_sha256"], "policy_version": result["policy_version"]}
    except (PolicyCertificationError, codex_upgrade.ConfigurationError, OSError, ValueError) as error:
        print(f"策略认证失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
