"""Claude VC-3 的 Wire、模型能力目录与暂存 Release（Claude 升级控制面设计 B3-03 补充项）。

投影算法在 ``tools/generate_claude_fw_g_profile.py``（指南 §4.3.2 的确定性投影工具，历史 2.1.226 制品由它逐字复算）。
本模块只提供两样东西：Claude 升级 VC-1 证据布局的证据来源，以及 VC-3 的装配（投影策略草案、模型身份派生、Release
身份、Store 模型能力目录对象、暂存 Release 与收据）。全程离线、零请求，不写仓库文件。

一、证据来源（``UpgradeEvidenceSource``，与生成器的 ``FwfCampaignSource`` 同名方法）
=====================================================================================
* 某 probe 的请求字节取 VC-1 封存的 capture manifest 里该 probe 的 ``relay_binary`` 标签（按有效连接序号排序），P 通道取
  ``pcap`` 标签；每个文件读前按 EvidencePackage v3 的外部证据绑定核对路径（宿主数据根内）、摘要与字节数，未封存即拒绝。
  这等价于历史布局里「R 通道必须绑定 MeasuredRuleLedger」。
* 非主模型的 probe 为 ``<模型>-<基础 probe>``（FW-G 模型能力 Campaign 的约定）。VC-1 现行场景清单只用主模型取证，投影
  策略登记多个模型时，缺证据的 probe 会被一次列全并失败关闭。
* thinking.display 的生产零改动证明、模型能力目录的 ``production_diff`` 都绑定 VC-1 的 CaptureIndex：它逐 probe 核过中继
  计划 ``production_changes=false`` 与环境闭环，是 VC-1 的生产零改动证明。实现策略的 ``evidence_ledger`` 绑定 VC-2 批准的
  原子断言台账副本。证据路径一律相对宿主数据根。

二、投影策略（准备草案第五个文件 ``wire-projection-policy.json``）
===============================================================
机器草案取 Active 版本的投影策略（受管树 ``claude_upgrade_wire_projection_<版本>.json``），只把 ``version`` 换成目标版本；
审核人按目标证据修订（模型闭集与角色、版本指纹 salt、选择器、TLS 期望、实现策略三段）。门禁：合同、投影成功、版本
指纹逐条复算一致。

三、模型身份派生与 Release
========================
* 画像 ``identity`` 的 ``supported_models``、``model_alias_policy``、``unknown_model_policy``、
  ``model_capability_catalog_sha256`` 由投影出的模型能力目录派生（与历史 ``extend_profile_for_model_capabilities`` 同一
  映射），是 ``profile_diff_paths`` 的一类解释落点（``model_identity_paths``），审核人的模型补丁不得再碰这四个落点。
* Release 身份与 Go 加载器同一算法：``required_rules_manifest_sha256`` 取 VC-2 批准的目标规则清单副本，
  ``required_rule_count`` 是目标规则数；画像原子断言数与本地场景断言数取 Store 原子断言台账按 ``owner_kind`` 计数。
* Store 对象 ``model_capability_catalog``：通用清单 ``official-client-model-capability-catalog/v1``，单条目 ``catalog``
  携带完整目录与摘要，随联合制品封存；VC-4 的候选构建收据经 ``profile_identity.model_capability_catalog_ref`` 引用它。
* 暂存 Release：stage-profile 批准后写 ``profile/release/``，布局镜像仓库 ``backend/internal/officialegress/``：
  ``catalogdata/claude/profiles/<版本>/<摘要>.json``、``catalogdata/claude/wire/<版本>/<摘要>.json``，另有
  ``release-entry.json``（要追加进 release-catalog.json 的 Release 条目）、``projection-policy.json`` 与收据
  ``release-stage-receipt.json``（inventory、Release 条目、validation_candidate 选择器建议、Store 引用）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import generate_claude_fw_g_profile as generator
from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256

ProjectionError = generator.ProjectionError

PROJECTION_POLICY_FILENAME = "claude_upgrade_wire_projection_{underscored}.json"
CATALOG_OBJECT_KIND = "model_capability_catalog"
CATALOG_OBJECT_SCHEMA = "official-client-model-capability-catalog/v1"
CATALOG_MANIFEST_ID = "claude-upgrade-model-capability-catalog"
STAGED_RELEASE_ROOT = Path("profile") / "release"
STAGED_RECEIPT_FILENAME = "release-stage-receipt.json"
STAGED_RECEIPT_SCHEMA = "claude-upgrade-staged-release/v1"
STAGED_TEMPORARY_PREFIX = ".tmp-release-"
RELEASE_ENTRY_FILENAME = "release-entry.json"
PROJECTION_POLICY_COPY_FILENAME = "projection-policy.json"
CATALOG_PREFIX = PurePosixPath("catalogdata") / "claude"
MODEL_IDENTITY_FIELDS = ("supported_models", "model_alias_policy", "unknown_model_policy", "model_capability_catalog_sha256")


# ---------------------------------------------------------------------------
# VC-1 证据（collect_inputs 时读一次，投影时按 probe 惰性解析）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseEvidence:
    """VC-1 封存证据里 Wire 投影要用的部分：标签索引、外部绑定与两份封存对象的绑定。"""

    data_root: Path
    campaign_root: str
    attempt_id: str
    relay: dict[str, list[dict[str, Any]]]
    pcaps: dict[str, dict[str, Any]]
    bindings: dict[str, dict[str, Any]]
    items: dict[str, dict[str, Any]]
    capture_index: dict[str, Any]
    capture_manifest: dict[str, Any]
    ledger: dict[str, str]


def _completeness_bindings(store: Any, package: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    completeness = classify._store_call("读取完整性清单", lambda: store.load_object(dict(package["completeness_ref"])))["payload"]
    by_purpose = {purpose: name for name, purpose in claude_upgrade_evidence.COMPLETENESS_PURPOSES.items()}
    found: dict[str, dict[str, Any]] = {}
    for entry in completeness.get("entries") or []:
        facts = entry.get("facts") if isinstance(entry, Mapping) else None
        name = by_purpose.get(facts.get("purpose")) if isinstance(facts, Mapping) else None
        if name is None:
            continue
        if name in found:
            raise ConfigurationError(f"VC-1 完整性清单重复登记了 {name}。")
        found[name] = dict(facts.get("binding") or {})
    return found


def load_release_evidence(
    campaign_dir: Path,
    store: Any,
    package: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> ReleaseEvidence:
    """读取 VC-1 capture manifest、CaptureIndex 与 EvidencePackage 外部绑定，建立 probe → 请求字节的索引。"""

    capture = classify._capture()
    data_root = capture._host_data_root(campaign_dir)
    campaign_root = campaign_dir.resolve(strict=True).relative_to(data_root.resolve(strict=True)).as_posix()
    found = _completeness_bindings(store, package)
    for name in ("capture_manifest", "capture_index"):
        if name not in found:
            raise ConfigurationError(f"VC-1 完整性清单缺少 Wire 投影必需的 {name}。")
    manifest, manifest_binding = classify._read_bound_json(data_root, found["capture_manifest"], "VC-1 capture manifest")
    index, index_binding = classify._read_bound_json(data_root, found["capture_index"], "VC-1 CaptureIndex")
    if manifest.get("schema_version") != claude_upgrade_evidence.CAPTURE_MANIFEST_SCHEMA or manifest.get("label_mismatches"):
        raise ConfigurationError("VC-1 capture manifest 合同名不符或仍有标签不一致。")
    if index.get("result") != "passed":
        raise ConfigurationError("VC-1 CaptureIndex 不是 passed：VC-1 尚未闭合。")
    bindings: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    for item in package["evidence_items"]:
        items[str(item["evidence_id"])] = dict(item)
        for ref in item.get("evidence_refs") or []:
            path = str(ref.get("path", ""))
            binding = {"path": path, "sha256": str(ref.get("sha256", "")), "bytes": ref.get("bytes")}
            if path in bindings and bindings[path] != binding:
                raise ConfigurationError(f"EvidencePackage 对同一证据文件有两份不同的绑定：{path}")
            bindings[path] = binding
    relay: dict[str, list[dict[str, Any]]] = {}
    pcaps: dict[str, dict[str, Any]] = {}
    for artifact in manifest.get("artifacts") or []:
        if not isinstance(artifact, Mapping) or str(artifact.get("path", "")) not in bindings:
            continue  # 未进 EvidencePackage 的 probe（失败或未封存）不可用作投影证据
        probe = str(artifact.get("probe_id", ""))
        if artifact.get("kind") == "relay_binary" and isinstance(artifact.get("connection"), Mapping):
            relay.setdefault(probe, []).append(dict(artifact))
        elif artifact.get("kind") == "pcap":
            if probe in pcaps:
                raise ConfigurationError(f"probe {probe} 登记了多份 pcap。")
            pcaps[probe] = dict(artifact)
    for probe, artifacts in relay.items():
        artifacts.sort(key=lambda item: int(item["connection"]["ordinal"]))
        ordinals = [int(item["connection"]["ordinal"]) for item in artifacts]
        if len(ordinals) != len(set(ordinals)):
            raise ConfigurationError(f"probe {probe} 的有效连接序号重复。")
    ledger_binding = classification["approved"]["atomic_assertions"]
    ledger = {"path": f"{campaign_root}/{ledger_binding['path']}", "sha256": str(ledger_binding["sha256"])}
    attempt = manifest.get("attempt") if isinstance(manifest.get("attempt"), Mapping) else {}
    return ReleaseEvidence(
        data_root=data_root,
        campaign_root=campaign_root,
        attempt_id=str(attempt.get("attempt_id", "")),
        relay=relay,
        pcaps=pcaps,
        bindings=bindings,
        items=items,
        capture_index=dict(index_binding),
        capture_manifest=dict(manifest_binding),
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# 证据来源
# ---------------------------------------------------------------------------


@dataclass
class UpgradeEvidenceSource:
    """Claude 升级 VC-1 布局的证据来源（方法与 ``generator.FwfCampaignSource`` 同名同义）。"""

    evidence: ReleaseEvidence
    policy: dict[str, Any]
    _requests: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _read(self, path: str) -> bytes:
        binding = self.evidence.bindings.get(path)
        if binding is None:
            raise ProjectionError(f"证据文件未进 EvidencePackage：{path}")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ProjectionError(f"证据路径不是数据根内相对路径：{path}")
        target = self.evidence.data_root.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file():
            raise ProjectionError(f"证据文件缺失或不是可信普通文件：{path}")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(self.evidence.data_root.resolve(strict=True)):
            raise ProjectionError(f"证据文件越过宿主数据根：{path}")
        data = resolved.read_bytes()
        if generator.sha256_hex(data) != binding["sha256"] or len(data) != binding["bytes"]:
            raise ProjectionError(f"证据文件摘要或字节数与 VC-1 封存绑定不一致：{path}")
        return data

    def require_probes(self, probes: list[str]) -> None:
        missing = [probe for probe in probes if not self.evidence.relay.get(probe)]
        if generator.PROBE_TLS in probes and generator.PROBE_TLS not in self.evidence.pcaps:
            missing.append(f"{generator.PROBE_TLS}（pcap）")
        if missing:
            raise ProjectionError(f"VC-1 证据缺少 Wire 投影所需的 probe：{sorted(set(missing))}")

    def requests(self, probe: str) -> list[dict[str, Any]]:
        if probe not in self._requests:
            artifacts = self.evidence.relay.get(probe)
            if not artifacts:
                raise ProjectionError(f"VC-1 证据缺少 probe：{probe}")
            parsed: list[dict[str, Any]] = []
            for artifact in artifacts:
                path = str(artifact["path"])
                for request in generator.parse_http_stream(self._read(path), f"{probe}:{PurePosixPath(path).name}"):
                    request = dict(request)
                    request["evidence_path"] = path
                    parsed.append(request)
            self._requests[probe] = parsed
        return self._requests[probe]

    def messages(self, probe: str) -> list[dict[str, Any]]:
        selected = [request for request in self.requests(probe) if request.get("request_target") == generator.MESSAGES_TARGET]
        if not selected:
            raise ProjectionError(f"{probe} 没有 messages R 通道")
        return selected

    def select_messages(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return generator.select_request(self.messages(probe), predicate, description, probe)

    def base_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return generator.select_request(self.requests(probe), predicate, description, probe)

    def model_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return generator.select_request(self.requests(probe), predicate, description, probe)

    def model_requests(self, probe: str) -> list[dict[str, Any]]:
        return self.requests(probe)

    def thinking_display_request(self, display: str) -> dict[str, Any]:
        probe = f"{generator.PROBE_THINKING_DISPLAY_PREFIX}{display}"
        requests = self.messages(probe)
        if len(requests) != 1:
            raise ProjectionError(f"{probe} messages R 通道数量不是 1")
        return requests[0]

    def thinking_production_receipt(self) -> dict[str, str]:
        return {"path": str(self.evidence.capture_index["path"]), "sha256": str(self.evidence.capture_index["sha256"])}

    def evidence_ledger(self) -> dict[str, str]:
        return dict(self.evidence.ledger)

    def catalog_evidence(self) -> dict[str, Any]:
        """模型能力目录的证据摘要：目录引用的每个 probe 一个完整 attempt 条目；VC-1 只封存通过的 probe，历史失败记 0。"""

        attempts: list[dict[str, Any]] = []
        for probe in generator.catalog_probes(self.policy):
            item = self.evidence.items.get(f"EVIDENCE-{probe}")
            manifests = [
                ref
                for ref in (item or {}).get("evidence_refs") or []
                if PurePosixPath(str(ref.get("path", ""))).name == claude_upgrade_evidence.RELAY_MANIFEST_FILENAME
            ]
            if item is None or len(manifests) != 1:
                raise ProjectionError(f"VC-1 EvidencePackage 缺少模型能力证据项或其中继清单：EVIDENCE-{probe}")
            attempts.append(
                {"attempt_id": f"{self.evidence.attempt_id}/{probe}", "status": "complete", "manifest_sha256": str(manifests[0]["sha256"])}
            )
        evidence = {
            "campaign_root": self.evidence.campaign_root,
            "successful_attempts": len(attempts),
            "historical_failed_attempts": 0,
            "attempts": attempts,
            "production_diff": {
                "path": str(self.evidence.capture_index["path"]),
                "sha256": str(self.evidence.capture_index["sha256"]),
                "identical": True,
            },
            "behavior_traffic_disposition": generator.BEHAVIOR_TRAFFIC_DISPOSITION,
        }
        evidence["campaign_sha256"] = generator.sha256_hex(generator.canonical_bytes(evidence))
        return evidence

    def messages_evidence(self) -> tuple[bytes, str]:
        """Wire 的 messages 基线：基线 probe 第一条推理请求的原始字节（同一连接复用时按流内偏移截取）。"""

        request = self.messages(generator.PROBE_BASELINE)[0]
        data = self._read(str(request["evidence_path"]))
        start = int(request["stream_offset"])
        return data[start : start + int(request["stream_length"])], str(request["evidence_path"])

    def tls_evidence(self) -> tuple[bytes, str]:
        artifact = self.evidence.pcaps.get(generator.PROBE_TLS)
        if artifact is None:
            raise ProjectionError(f"VC-1 证据缺少 P 通道 pcap：{generator.PROBE_TLS}")
        return self._read(str(artifact["path"])), str(artifact["path"])

    def inference_requests(self) -> list[dict[str, Any]]:
        """本次投影读过的全部推理请求（版本指纹逐条复算用）。"""

        return [
            request
            for probe in sorted(self._requests)
            for request in self._requests[probe]
            if request.get("request_target") == generator.MESSAGES_TARGET
        ]


# ---------------------------------------------------------------------------
# 投影策略
# ---------------------------------------------------------------------------


def active_projection_policy(active_version: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Active 版本的 Wire 投影策略（受管树内按版本命名的文件）与其绑定。"""

    path = Path(__file__).resolve().parent / PROJECTION_POLICY_FILENAME.format(underscored=active_version.replace(".", "_"))
    try:
        policy = generator.load_projection_policy(path)
    except ProjectionError as error:
        raise ConfigurationError(f"Active 版本 {active_version} 的 Wire 投影策略不可用：{error}") from error
    if policy["version"] != active_version:
        raise ConfigurationError(f"Active 投影策略的 version 不是 {active_version}。")
    return policy, {"path": f"tools/official_client_capture/{path.name}", "sha256": file_sha256(path)}


def machine_projection_policy(active_policy: Mapping[str, Any], target_version: str) -> dict[str, Any]:
    """机器草案：Active 版本的投影策略只换版本号；其余版本事实由审核人按目标证据修订。"""

    policy = copy.deepcopy(dict(active_policy))
    policy["version"] = target_version
    return policy


# ---------------------------------------------------------------------------
# 投影（画像派生之前：实现策略与模型能力目录；之后：Wire 与 Release）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProjection:
    policy: dict[str, Any]
    implementation: dict[str, Any]
    catalog: dict[str, Any]
    catalog_bytes: bytes
    catalog_sha256: str
    fingerprint_count: int
    messages: tuple[bytes, str]
    tls: tuple[bytes, str]


def project_catalog(evidence: ReleaseEvidence, policy_document: Any, *, target_version: str, required_rule_count: int) -> CatalogProjection:
    """按投影策略从 VC-1 证据投影实现策略与模型能力目录，并逐条复算版本指纹。失败抛 ``ProjectionError``。"""

    policy = generator.validate_projection_policy(policy_document)
    if policy["version"] != target_version:
        raise ProjectionError(f"Wire 投影策略的 version 不是目标版本 {target_version}")
    source = UpgradeEvidenceSource(evidence, policy)
    source.require_probes(generator.required_probes(policy))
    implementation = generator.build_implementation_policy(source, policy)
    catalog = generator.build_model_capability_catalog(source, policy, implementation["scenarios"], required_rule_count)
    messages = source.messages_evidence()
    tls = source.tls_evidence()
    count = generator.verify_version_fingerprints(source.inference_requests(), policy, target_version)
    catalog_bytes = generator.canonical_bytes(catalog)
    return CatalogProjection(
        policy=policy,
        implementation=implementation,
        catalog=catalog,
        catalog_bytes=catalog_bytes,
        catalog_sha256=generator.sha256_hex(catalog_bytes),
        fingerprint_count=count,
        messages=messages,
        tls=tls,
    )


def model_identity(projection: CatalogProjection) -> dict[str, Any]:
    """画像 identity 由模型能力目录派生的四个字段（与历史 ``extend_profile_for_model_capabilities`` 同一映射）。"""

    catalog = projection.catalog
    return {
        "supported_models": [model["canonical_model"] for model in catalog["models"]],
        "model_alias_policy": catalog["alias_policy"],
        "unknown_model_policy": catalog["unknown_model_policy"],
        "model_capability_catalog_sha256": projection.catalog_sha256,
    }


def finish_release(
    projection: CatalogProjection,
    *,
    target_profile: Mapping[str, Any],
    target_profile_bytes: bytes,
    platform: str,
    required_rules_manifest_sha256: str,
    atomic_counts: Mapping[str, int],
) -> dict[str, Any]:
    """画像派生之后：投影 Wire，计算 Release 身份与 release-catalog 条目（与 Go 加载器的交叉校验同口径）。"""

    version = projection.policy["version"]
    profile_sha256 = generator.sha256_hex(target_profile_bytes)
    rules = target_profile.get("rules") if isinstance(target_profile.get("rules"), list) else []
    endpoints = target_profile.get("strict_endpoints") if isinstance(target_profile.get("strict_endpoints"), list) else []
    identity = target_profile.get("identity") if isinstance(target_profile.get("identity"), Mapping) else {}
    if identity.get("platform") != platform or identity.get("version") != version:
        raise ProjectionError("目标画像的版本或平台与投影目标不一致")
    if identity.get("model_capability_catalog_sha256") != projection.catalog_sha256 or list(identity.get("supported_models") or []) != [
        model["canonical_model"] for model in projection.catalog["models"]
    ]:
        raise ProjectionError("目标画像的模型闭集或能力目录摘要与投影出的模型能力目录不一致")
    wire = generator.build_wire_artifact(
        projection.policy,
        projection.implementation,
        projection.catalog,
        projection.messages[0],
        projection.messages[1],
        projection.tls[0],
        projection.tls[1],
        profile_sha256,
        platform,
    )
    wire_bytes = generator.canonical_bytes(wire)
    wire_sha256 = generator.sha256_hex(wire_bytes)
    rule_count = len(rules)
    if rule_count != projection.catalog["required_rule_count"]:
        raise ProjectionError("目标画像规则数与模型能力目录的 required_rule_count 不一致")
    profile_assertions = int(atomic_counts.get("required_rule", 0))
    local_assertions = int(atomic_counts.get("scenario_only", 0))
    evidence = projection.catalog["evidence"]
    if min(rule_count, len(endpoints), profile_assertions, local_assertions, int(evidence["successful_attempts"])) <= 0:
        raise ProjectionError("Release 计数不完整：规则、严格端点、画像断言、本地场景断言与模型证据都必须大于 0")
    release_sha256, bundle_sha256 = generator.build_release_identity(
        version=version,
        platform=platform,
        profile_digest=profile_sha256,
        wire_digest=wire_sha256,
        model_catalog_digest=projection.catalog_sha256,
        required_rules_manifest_sha256=required_rules_manifest_sha256,
        required_rule_count=rule_count,
    )
    profile_path = CATALOG_PREFIX / "profiles" / version / f"{profile_sha256}.json"
    wire_path = CATALOG_PREFIX / "wire" / version / f"{wire_sha256}.json"
    entry = {
        "version": version,
        "platform": platform,
        "profile": {"path": profile_path.as_posix(), "sha256": profile_sha256},
        "wire": {"path": wire_path.as_posix(), "sha256": wire_sha256},
        "required_rules_manifest_sha256": required_rules_manifest_sha256,
        "release_sha256": release_sha256,
        "bundle_sha256": bundle_sha256,
        "required_rule_count": rule_count,
        "profile_atomic_assertion_count": profile_assertions,
        "local_scenario_assertion_count": local_assertions,
        "strict_endpoint_count": len(endpoints),
        "models": [model["canonical_model"] for model in projection.catalog["models"]],
        "model_evidence": {
            "successful_attempts": int(evidence["successful_attempts"]),
            "historical_failed_attempts": int(evidence["historical_failed_attempts"]),
        },
    }
    return {
        "wire": wire,
        "wire_bytes": wire_bytes,
        "wire_sha256": wire_sha256,
        "release_sha256": release_sha256,
        "bundle_sha256": bundle_sha256,
        "entry": entry,
        "entry_bytes": classify._serialize(entry),
    }


def release_summary(projection: CatalogProjection, release: Mapping[str, Any]) -> dict[str, Any]:
    """进审核摘要与联合清单的 Release 身份摘要。"""

    return {
        "model_capability_catalog_sha256": projection.catalog_sha256,
        "wire_sha256": release["wire_sha256"],
        "release_sha256": release["release_sha256"],
        "bundle_sha256": release["bundle_sha256"],
        "models": list(release["entry"]["models"]),
        "fingerprint_verified_request_count": projection.fingerprint_count,
    }


# ---------------------------------------------------------------------------
# Store 模型能力目录对象与暂存 Release
# ---------------------------------------------------------------------------


def catalog_object_payload(persona: Mapping[str, Any], catalog: Mapping[str, Any], catalog_sha256: str) -> dict[str, Any]:
    """Store 的 ModelCapabilityCatalog 对象：通用清单单条目，携带完整目录（可逐字还原并复算摘要）。"""

    return {
        "schema_version": CATALOG_OBJECT_SCHEMA,
        "persona": dict(persona),
        "manifest_id": CATALOG_MANIFEST_ID,
        "entries": [
            {
                "id": "catalog",
                "facts": {
                    "catalog_sha256": catalog_sha256,
                    "models": [model["canonical_model"] for model in catalog["models"]],
                    "document": copy.deepcopy(dict(catalog)),
                },
            }
        ],
    }


def staged_release_files(
    *,
    entry: Mapping[str, Any],
    profile_bytes: bytes,
    wire_bytes: bytes,
    entry_bytes: bytes,
    policy_bytes: bytes,
) -> dict[str, bytes]:
    """暂存 Release 的文件集合（相对 ``profile/release/``；画像与 Wire 的路径就是 Release 条目里的仓库内路径）。"""

    return {
        str(entry["profile"]["path"]): profile_bytes,
        str(entry["wire"]["path"]): wire_bytes,
        RELEASE_ENTRY_FILENAME: entry_bytes,
        PROJECTION_POLICY_COPY_FILENAME: policy_bytes,
    }


def staged_release_receipt(
    *,
    campaign_id: str,
    target_version: str,
    platform: str,
    generation: str,
    joint_sha256: str,
    entry: Mapping[str, Any],
    files: Mapping[str, bytes],
    store_refs: Mapping[str, Any],
) -> dict[str, Any]:
    """暂存 Release 收据：逐文件 inventory、Release 条目、validation_candidate 选择器建议与 Store 引用。"""

    inventory = [
        {"path": path, "sha256": generator.sha256_hex(data), "size": len(data)}
        for path, data in sorted(files.items())
    ]
    return {
        "schema_version": STAGED_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "target_version": target_version,
        "platform": platform,
        "generation": generation,
        "joint_sha256": joint_sha256,
        "release_entry": copy.deepcopy(dict(entry)),
        # VC-4 把 Release 条目追加进 release-catalog.json 的 releases，并把 validation_candidate 指向它；Active、Rollback
        # 与历史 Release 不动（指南 §4.4.1）。changeset 取 Campaign ID，必须不同于 production_active 的 changeset。
        "validation_candidate_selector": {
            "kind": "release",
            "release_sha256": entry["release_sha256"],
            "changeset": f"{campaign_id}-candidate",
        },
        "store": {key: dict(value) for key, value in store_refs.items()},
        "active_unchanged": True,
        "production_selector_changed": False,
        "inventory": inventory,
        "inventory_sha256": generator.sha256_hex(generator.canonical_bytes(inventory)),
    }


def verify_staged_release(root: Path, receipt: Mapping[str, Any]) -> None:
    """逐文件复算暂存 Release：inventory 精确覆盖目录下全部文件，摘要与字节数一致。"""

    inventory = receipt.get("inventory")
    if not isinstance(inventory, list) or generator.sha256_hex(generator.canonical_bytes(inventory)) != receipt.get("inventory_sha256"):
        raise ConfigurationError("暂存 Release 收据的 inventory 摘要不一致。")
    expected: set[str] = set()
    for item in inventory:
        relative = PurePosixPath(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ConfigurationError(f"暂存 Release inventory 路径越界：{item.get('path')}")
        target = root.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file() or file_sha256(target) != item.get("sha256") or target.stat().st_size != item.get("size"):
            raise ConfigurationError(f"暂存 Release 文件缺失或漂移：{item.get('path')}")
        expected.add(relative.as_posix())
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.name != STAGED_RECEIPT_FILENAME}
    if actual != expected:
        raise ConfigurationError("暂存 Release 目录与收据 inventory 不精确一致。")


def publish_staged_release(campaign_dir: Path, files: Mapping[str, bytes], receipt: Mapping[str, Any]) -> dict[str, Any]:
    """只写一次地发布暂存 Release：写进同级临时目录后整体改名；已存在时要求收据逐字相同并逐文件复算（强杀后重跑收敛）。"""

    root = campaign_dir / STAGED_RELEASE_ROOT
    parent = root.parent
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise ConfigurationError(f"暂存 Release 目录不可信：{root}")
        existing = classify._read_json(root / STAGED_RECEIPT_FILENAME, "暂存 Release 收据")
        if existing != dict(receipt):
            raise ConfigurationError("暂存 Release 已存在且收据不同：批准后变化须建立后继 Campaign。")
        verify_staged_release(root, existing)
        return existing
    ensure_private_directory(parent, campaign_dir)
    classify._clear_temporaries(parent, STAGED_TEMPORARY_PREFIX)
    temporary = Path(tempfile.mkdtemp(prefix=STAGED_TEMPORARY_PREFIX, dir=parent))
    try:
        os.chmod(temporary, 0o700)
        for relative, data in {**files, STAGED_RECEIPT_FILENAME: classify._serialize(receipt)}.items():
            target = temporary.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            classify._write_private_bytes(target, data)
        classify._fsync_directory(temporary)
        os.rename(temporary, root)
        classify._fsync_directory(parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    verify_staged_release(root, receipt)
    return dict(receipt)


def load_staged_release(campaign_dir: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """按阶段结果的绑定读取暂存 Release 收据并逐文件复算（交给 VC-4 的候选构建）。"""

    root = campaign_dir / STAGED_RELEASE_ROOT
    path = root / STAGED_RECEIPT_FILENAME
    if path.relative_to(campaign_dir).as_posix() != binding.get("path") or path.is_symlink() or not path.is_file():
        raise ConfigurationError("暂存 Release 收据缺失或路径与绑定不符。")
    if file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError("暂存 Release 收据摘要漂移。")
    receipt = classify._read_json(path, "暂存 Release 收据")
    if receipt.get("schema_version") != STAGED_RECEIPT_SCHEMA:
        raise ConfigurationError("暂存 Release 收据合同名不符。")
    verify_staged_release(root, receipt)
    return receipt
