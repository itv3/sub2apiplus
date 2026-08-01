#!/usr/bin/env python3
"""校验 Claude Code 2.1.220 规则、旧版线索和文档之间的一致性。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
LEDGER_DIR = Path(__file__).resolve().parent
SPEC_PATH = ROOT / "docs/Claude_code_21220_EGRESS_SPEC.md"
RULES_PATH = LEDGER_DIR / "rules_2_1_220.json"
HITCC_PATH = LEDGER_DIR / "hitcc_2_1_197_coverage.json"
SOURCE_PATH = LEDGER_DIR / "source_2_1_88_coverage.json"
SOURCE_2188_ROOT = ROOT / "local-analysis/sources/claude-code-2.1.88"
BASELINE_ROOT = ROOT / "local-analysis/captures/claude-spec-baseline-20260801"
HITCC_ROOT = ROOT / "local-analysis/sources/hitcc-2.1.197"

HISTORICAL_IDS = {
    "SPEC-TLS-001",
    "SPEC-TLS-002",
    "SPEC-PROTO-001",
    *(f"SPEC-HDR-{index:03d}" for index in range(1, 20)),
    *(f"SPEC-BODY-{index:03d}" for index in range(1, 7)),
    "SPEC-EP-001",
    "SPEC-EP-002",
    "SPEC-CONN-001",
    "SPEC-CONN-002",
    "SPEC-RESP-001",
    "SPEC-RESP-002",
    "SPEC-RESP-003",
    "SPEC-BETA-001",
}
EXPECTED_DISPOSITIONS = {
    "verified": 0,
    "observed": 27,
    "candidate": 5,
    "superseded": 1,
    "response_compat": 3,
}
EXPECTED_REPLACEMENTS = {"SPEC-HDR-020", "SPEC-BODY-007"}
EXPECTED_ADDITIONAL = {
    *(f"SPEC-BODY-{index:03d}" for index in range(8, 17)),
    "SPEC-TLS-003",
    "SPEC-EP-003",
    "SPEC-EP-004",
}
KNOWN_SPEC_IDS = HISTORICAL_IDS | EXPECTED_REPLACEMENTS | EXPECTED_ADDITIONAL
HITCC_COVERAGE = {"covered", "partial", "missing", "out_of_scope"}
HITCC_DRIFT = {"same", "changed", "removed", "unknown", "not_applicable"}
EXPECTED_HITCC_COVERAGE = {
    "covered": 11,
    "partial": 63,
    "missing": 4,
    "out_of_scope": 10,
}
HITCC_DOCUMENT_DISPOSITIONS = {
    "clue_source",
    "cross_reference_only",
    "no_egress_clue",
}
HITCC_DOCUMENT_MAPPINGS = {"mapped", "unmapped", "not_applicable"}
EXPECTED_HITCC_DOCUMENTS = {
    "total": 112,
    "clue_source": 71,
    "clue_source_mapped": 18,
    "clue_source_unmapped": 53,
    "cross_reference_only": 33,
    "no_egress_clue": 8,
}
SOURCE_STATUSES = {
    "runtime_verified",
    "static_only",
    "changed",
    "unverified",
    "out_of_scope",
}
EXPECTED_SOURCE_STATUSES = {
    "runtime_verified": 0,
    "static_only": 43,
    "changed": 3,
    "unverified": 38,
    "out_of_scope": 18,
}
SOURCE_FILE_DISPOSITIONS = {
    "rule_source",
    "transitive_support",
    "no_direct_egress_signal",
    "egress_candidate_unmapped",
}
EXPECTED_SOURCE_FILES = {
    "total": 1902,
    "rule_source": 21,
    "transitive_support": 237,
    "no_direct_egress_signal": 0,
    "egress_candidate_unmapped": 1644,
}
EXPECTED_SOURCE_TRIAGE_LIMITS = {
    "unmapped_with_lexical_signals": 458,
    "unreviewed_without_lexical_signals": 1186,
    "transitive_with_non_import_signals": 142,
    "transitive_import_only": 95,
}
EXPECTED_FULL_SOURCE_OBSERVATIONS = {
    "SRC2188-HDR-001",
    "SRC2188-BODY-002",
    "SRC2188-BODY-003",
}
EXPECTED_NARROWED_SOURCE_SUBCLAIMS = {
    "SRC2188-HDR-002",
    "SRC2188-HDR-003",
    "SRC2188-HDR-004",
    "SRC2188-HDR-010",
    "SRC2188-HDR-014",
    "SRC2188-BODY-007",
    "SRC2188-BODY-013",
}
EXPECTED_CHANGED_SOURCE_RULES = {
    "SRC2188-HIST-001",
    "SRC2188-HIST-002",
    "SRC2188-HIST-003",
}
EXPECTED_SOURCE_GAPS = {
    "src/bridge/bridgeMain.ts",
    "src/cli/transports/SSETransport.ts",
    "src/remote/SessionsWebSocket.ts",
    "src/server/createDirectConnectSession.ts",
    "src/services/api/adminRequests.ts",
    "src/services/api/firstTokenDate.ts",
    "src/services/api/overageCreditGrant.ts",
    "src/services/api/referral.ts",
    "src/services/api/ultrareviewQuota.ts",
    "src/services/mcp/xaaIdpLogin.ts",
    "src/services/voiceStreamSTT.ts",
}
EXPECTED_SOURCE_ROOT_INVENTORIES = {
    "root": (
        "local-analysis/sources/claude-code-2.1.88",
        4756,
        47310128,
        "dafc1b37756e0f6bb312a8bf5c98c115c40a65d5d87cc1aa80910cf6e956878f",
    ),
    "src": (
        "local-analysis/sources/claude-code-2.1.88/src",
        1902,
        30382832,
        "d865dbfa59f24563fedc767a425f1e2e35ff15a458f478ebf6ac24d800cef4a8",
    ),
    "node_modules": (
        "local-analysis/sources/claude-code-2.1.88/node_modules",
        2850,
        16915042,
        "f049798ba432bd9286c299810c95e5c72d6ae40a4ff78911bcc3750b3e56ed2a",
    ),
    "vendor": (
        "local-analysis/sources/claude-code-2.1.88/vendor",
        4,
        12254,
        "6d2d7395c398aa05e42e7b2b89c239b85de1ba9fd8621f3c4635924ed2ee6455",
    ),
    "anthropic_sdk": (
        "local-analysis/sources/claude-code-2.1.88/node_modules/@anthropic-ai/sdk",
        51,
        231805,
        "0a1e18ded2ef751f5c8ff6e7d4199d4f855f916cc3094e69096ebd49447c1c30",
    ),
}
EXPECTED_CLAUDE_21220_SHA256 = (
    "674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863"
)
EXPECTED_PLATFORM = "Linux-6.8.0-136-generic-x86_64-with-glibc2.39"
EXPECTED_AUTH_PREFLIGHT = {
    "api_provider": "firstParty",
    "auth_method": "claude.ai",
    "logged_in": True,
}
RUNTIME_EVIDENCE_CHANNELS = {"J", "P", "R", "L"}
DOCUMENT_DISPOSITIONS = {
    "已验证": "verified",
    "已观察": "observed",
    "候选": "candidate",
    "已取代": "superseded",
    "响应兼容": "response_compat",
}
EXPECTED_SELECTED_DENOMINATORS = {
    "SPEC-TLS-002": [
        (
            "P",
            "all ClientHello in selected pcaps",
            8,
            ("BASELINE-P", "HISTORICAL-220"),
        ),
    ],
    "SPEC-PROTO-001": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-HDR-002": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-EP-001": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-HDR-020": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-BODY-007": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    **{
        f"SPEC-BODY-{index:03d}": [
            (
                "J",
                "all request records in selected J-raw artifacts",
                6,
                ("BASELINE-J", "PROBE-BASELINE-J"),
            ),
        ]
        for index in range(8, 14)
    },
    "SPEC-TLS-003": [
        ("P", "all ClientHello in selected pcaps", 4, ("ALLHOSTS",)),
    ],
    "SPEC-EP-003": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-EP-004": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-BODY-014": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-BODY-015": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
    "SPEC-BODY-016": [
        (
            "J",
            "all request records in selected J-raw artifacts",
            6,
            ("BASELINE-J", "PROBE-BASELINE-J"),
        ),
    ],
}
EXPECTED_A1_SOURCE_PATHS = {
    "local-analysis/sources/claude-code-2.1.220-official/SOURCE_MANIFEST.json",
    "local-analysis/sources/claude-code-2.1.220-official/bundle-anchors-linux-x64.json",
}
EXPECTED_A1_CANDIDATE_PATHS = {
    "local-analysis/sources/claude-code-2.1.220-official/CANDIDATE_EVIDENCE.json",
}
EXPECTED_TLS_ANALYSIS_PATHS = {
    "local-analysis/captures/claude-spec-baseline-20260801/"
    "oauth-claude-newaccount-baseline/analysis/direct/claude-http/s1.json",
    "local-analysis/captures/claude-spec-baseline-20260801/"
    "oauth-claude-newaccount-baseline/analysis/direct/claude-http/s2.json",
    "local-analysis/captures/claude-spec-baseline-20260801/"
    "oauth-claude-newaccount-baseline/analysis/direct/claude-http/s4.json",
    "local-analysis/captures/official-egress-final-review-fix-20260727-094500/"
    "official-client/oauth-oauth-p0p2-zstd-final-20260726T1420Z/"
    "analysis/direct/claude-http/s1.json",
}
EXPECTED_TLS_PCAP_PATHS = {
    path.replace("analysis/direct/claude-http/", "direct/claude-http/").replace(
        ".json", "/traffic.pcap"
    )
    for path in EXPECTED_TLS_ANALYSIS_PATHS
}
EXPECTED_ALLHOSTS_ANALYSIS_PATHS = {
    "local-analysis/captures/claude-spec-baseline-20260801/"
    f"oauth-discover-allhosts-r{run}/analysis/direct/claude-http/s1.json"
    for run in (1, 2)
}
EXPECTED_ALLHOSTS_PCAP_PATHS = {
    path.replace("analysis/direct/claude-http/s1.json", "direct/claude-http/s1/traffic.pcap")
    for path in EXPECTED_ALLHOSTS_ANALYSIS_PATHS
}
EXPECTED_ALLHOSTS_MANIFEST_PATHS = {
    "local-analysis/captures/claude-spec-baseline-20260801/"
    f"oauth-discover-allhosts-r{run}/manifest.json"
    for run in (1, 2)
}


def load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"无法读取 JSON：{path.relative_to(ROOT)}：{exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"JSON 顶层必须是对象：{path.relative_to(ROOT)}")
        return {}
    return value


def require_fields(
    item: dict[str, Any], fields: tuple[str, ...], label: str, errors: list[str]
) -> None:
    for field in fields:
        if field not in item:
            errors.append(f"{label} 缺少字段 {field}")


def count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def compute_path_content_inventory(base: Path) -> dict[str, Any]:
    """按仓库相对路径与文件内容复算确定性取证快照。"""

    files = sorted(
        (path for path in base.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )
    aggregate = hashlib.sha256()
    byte_count = 0
    for path in files:
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(ROOT).as_posix()
        aggregate.update(f"{file_digest}  {relative}\n".encode())
        byte_count += path.stat().st_size
    return {
        "file_count": len(files),
        "byte_count": byte_count,
        "path_and_content_sha256": aggregate.hexdigest(),
    }


def load_pcap_parser(errors: list[str]) -> Any | None:
    """加载仓库归档的原始 pcap 解析器，避免只信任派生分析 JSON。"""

    parser_path = ROOT / "tools/official_client_capture/pcap_clienthello.py"
    spec = importlib.util.spec_from_file_location("claude_21220_pcap_parser", parser_path)
    if spec is None or spec.loader is None:
        errors.append("无法构造 pcap_clienthello.py 的加载规格")
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError) as exc:
        errors.append(f"无法加载 pcap_clienthello.py：{exc}")
        return None
    return module


def parse_raw_client_hellos(
    path: Path, parser: Any, errors: list[str]
) -> list[dict[str, Any]]:
    """从单个原始 pcap 解析全部 ClientHello，不按待证字段筛选。"""

    observations: list[dict[str, Any]] = []
    try:
        packets = parser.iter_packets(path)
        for link, packet in packets:
            tcp = parser.tcp_payload(link, packet)
            if not tcp:
                continue
            _, _, payload = tcp
            parsed = parser.parse_client_hello(payload)
            if not parsed or not parsed[1]:
                continue
            sni, extensions, ciphers, alpn = parsed
            observations.append(
                {
                    "sni": sni,
                    "extensions": list(extensions),
                    "ciphers": list(ciphers),
                    "alpn": list(alpn),
                }
            )
    except (OSError, ValueError) as exc:
        errors.append(f"无法解析原始 pcap {path.relative_to(ROOT)}：{exc}")
    return observations


def validate_line_range(path: Path, ranges: str, label: str, errors: list[str]) -> None:
    try:
        line_count = count_lines(path)
    except OSError as exc:
        errors.append(f"{label} 无法读取来源文件：{exc}")
        return
    matches = re.findall(r"L(\d+)-L(\d+)", ranges)
    if not matches:
        errors.append(f"{label} 的 source_lines 格式无效：{ranges}")
        return
    for start_text, end_text in matches:
        start, end = int(start_text), int(end_text)
        if start < 1 or end < start or end > line_count:
            errors.append(
                f"{label} 行号越界：{ranges}，文件共 {line_count} 行"
            )


def validate_selected_runtime_catalogs(
    data: dict[str, Any], errors: list[str]
) -> None:
    """逐 catalog 复核选定有限样本观察与其 manifest、artifact 摘要绑定。"""

    catalog = data.get("evidence_catalog", {})
    if not isinstance(catalog, dict):
        return
    runtime_catalog_users: dict[str, set[str]] = {}
    for item in [
        *data.get("rules", []),
        *data.get("replacement_rules", []),
        *data.get("additional_rules", []),
    ]:
        if not isinstance(item, dict):
            continue
        if str(item.get("id")) not in EXPECTED_SELECTED_DENOMINATORS:
            continue
        rule_id = str(item.get("id", "<unknown>"))
        for evidence_ref in item.get("evidence_refs", []):
            if not isinstance(evidence_ref, dict):
                continue
            catalog_id = str(evidence_ref.get("catalog_ref", ""))
            entry = catalog.get(catalog_id)
            if not isinstance(entry, dict):
                continue
            channels = set(entry.get("channels", []))
            if not channels & RUNTIME_EVIDENCE_CHANNELS:
                continue
            runtime_catalog_users.setdefault(catalog_id, set()).add(rule_id)
            if "M" not in channels:
                errors.append(
                    f"{rule_id} 的运行证据 catalog {catalog_id} 自身不含 M"
                )

    for catalog_id, rule_ids in sorted(runtime_catalog_users.items()):
        entry = catalog[catalog_id]
        label = f"{catalog_id}（有限样本规则 {', '.join(sorted(rule_ids))}）"
        relative_paths = entry.get("paths", [])
        if not isinstance(relative_paths, list):
            errors.append(f"{label} paths 必须是数组")
            continue
        normalized_paths = [str(relative) for relative in relative_paths]
        manifest_relatives = [
            relative
            for relative in normalized_paths
            if Path(relative).name == "manifest.json"
        ]
        if not manifest_relatives:
            errors.append(f"{label} 没有 catalog 内 manifest")
            continue

        manifests: list[tuple[Path, dict[str, dict[str, Any]]]] = []
        for manifest_relative in manifest_relatives:
            manifest_path = ROOT / manifest_relative
            manifest = load_json(manifest_path, errors)
            if manifest.get("status") != "complete":
                errors.append(f"{label} 引用非 complete manifest：{manifest_relative}")
            client = manifest.get("clients", {}).get("claude", {})
            if client.get("sha256") != EXPECTED_CLAUDE_21220_SHA256:
                errors.append(f"{label} Claude 2.1.220 摘要不匹配：{manifest_relative}")
            if client.get("expected_sha256") != EXPECTED_CLAUDE_21220_SHA256:
                errors.append(
                    f"{label} Claude 2.1.220 预期摘要不匹配：{manifest_relative}"
                )
            if not str(client.get("version", "")).startswith("2.1.220 "):
                errors.append(f"{label} Claude 版本不匹配：{manifest_relative}")
            runtime = manifest.get("runtime", {})
            if not isinstance(runtime, dict):
                errors.append(f"{label} runtime 不是对象：{manifest_relative}")
                runtime = {}
            if runtime.get("platform") != EXPECTED_PLATFORM:
                errors.append(f"{label} 平台不匹配：{manifest_relative}")
            if runtime.get("clean_environment_keys") != ["HOME", "PATH"]:
                errors.append(f"{label} 不是 clean environment：{manifest_relative}")
            if runtime.get("runtime_image_verified") is not False:
                errors.append(
                    f"{label} 当前审计基线应明确记录 runtime image 未独立验证："
                    f"{manifest_relative}"
                )
            if runtime.get("auth_preflight", {}).get("claude") != (
                EXPECTED_AUTH_PREFLIGHT
            ):
                errors.append(f"{label} OAuth/provider 不匹配：{manifest_relative}")
            secret_scan = manifest.get("secret_scan", {})
            if not isinstance(secret_scan, dict) or not isinstance(
                secret_scan.get("performed"), bool
            ):
                errors.append(f"{label} 未记录 secret_scan 边界：{manifest_relative}")
            elif secret_scan.get("performed") is not False:
                errors.append(
                    f"{label} 当前审计基线应明确记录未执行秘密扫描："
                    f"{manifest_relative}"
                )
            elif not secret_scan.get("limitation"):
                errors.append(f"{label} secret_scan=false 却未记录限制：{manifest_relative}")

            artifact_items = manifest.get("artifacts", [])
            if not isinstance(artifact_items, list):
                errors.append(f"{label} manifest artifacts 不是数组：{manifest_relative}")
                artifact_items = []
            artifacts: dict[str, dict[str, Any]] = {}
            for index, artifact in enumerate(artifact_items):
                if not isinstance(artifact, dict):
                    errors.append(
                        f"{label} manifest artifact[{index}] 不是对象：{manifest_relative}"
                    )
                    continue
                artifact_relative = str(artifact.get("path", ""))
                if not artifact_relative:
                    errors.append(
                        f"{label} manifest artifact[{index}] 缺少路径：{manifest_relative}"
                    )
                    continue
                if artifact_relative in artifacts:
                    errors.append(
                        f"{label} manifest artifact 路径重复："
                        f"{manifest_relative} -> {artifact_relative}"
                    )
                    continue
                artifacts[artifact_relative] = artifact
            manifests.append((Path(manifest_relative).parent, artifacts))

        for evidence_relative in normalized_paths:
            if evidence_relative in manifest_relatives:
                continue
            evidence_path = ROOT / evidence_relative
            evidence_relative_path = Path(evidence_relative)
            owners: list[tuple[Path, dict[str, dict[str, Any]], str]] = []
            for run_root, artifacts in manifests:
                try:
                    artifact_relative = evidence_relative_path.relative_to(run_root)
                except ValueError:
                    continue
                owners.append((run_root, artifacts, artifact_relative.as_posix()))
            if len(owners) != 1:
                errors.append(
                    f"{label} 证据文件必须且只能归属一个 catalog manifest："
                    f"{evidence_relative}，实际归属数 {len(owners)}"
                )
                continue
            _, artifacts, artifact_relative = owners[0]
            artifact = artifacts.get(artifact_relative)
            if artifact is None:
                errors.append(
                    f"{label} 证据文件未进入 manifest artifacts：{evidence_relative}"
                )
                continue
            if not evidence_path.is_file():
                continue
            actual_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            if artifact.get("sha256") != actual_sha256:
                errors.append(
                    f"{label} 证据文件摘要与 manifest 不一致：{evidence_relative}，"
                    f"实际 {actual_sha256}"
                )


def validate_catalog_hashes_and_a1_identity(
    data: dict[str, Any], errors: list[str]
) -> None:
    """复核 catalog 自带摘要，并闭合 A1 候选与 Linux 官方产物身份。"""

    catalog = data.get("evidence_catalog", {})
    if not isinstance(catalog, dict):
        return
    for required_catalog_id in ("A1-SOURCE-MANIFEST", "A1-CANDIDATES"):
        required_entry = catalog.get(required_catalog_id)
        if not isinstance(required_entry, dict) or "sha256_by_path" not in required_entry:
            errors.append(f"{required_catalog_id} 缺少 sha256_by_path")
    for catalog_id, entry in catalog.items():
        if not isinstance(entry, dict) or "sha256_by_path" not in entry:
            continue
        hashes = entry.get("sha256_by_path")
        if not isinstance(hashes, dict):
            errors.append(f"{catalog_id}.sha256_by_path 必须是对象")
            continue
        paths = {str(path) for path in entry.get("paths", [])}
        if set(hashes) != paths:
            errors.append(
                f"{catalog_id}.sha256_by_path 必须精确覆盖 catalog paths"
            )
        for relative, expected_sha256 in hashes.items():
            path = ROOT / str(relative)
            if not path.is_file():
                continue
            actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_sha256 != actual_sha256:
                errors.append(
                    f"{catalog_id} catalog 摘要不匹配：{relative}，实际 {actual_sha256}"
                )

    source_entry = catalog.get("A1-SOURCE-MANIFEST", {})
    candidate_entry = catalog.get("A1-CANDIDATES", {})
    source_paths = source_entry.get("paths", []) if isinstance(source_entry, dict) else []
    candidate_paths = (
        candidate_entry.get("paths", []) if isinstance(candidate_entry, dict) else []
    )
    if set(source_paths) != EXPECTED_A1_SOURCE_PATHS:
        errors.append(f"A1 来源文件集合变化：实际 {sorted(source_paths)}")
    if set(candidate_paths) != EXPECTED_A1_CANDIDATE_PATHS:
        errors.append(f"A1 候选文件集合变化：实际 {sorted(candidate_paths)}")
    if (
        not EXPECTED_A1_SOURCE_PATHS <= set(source_paths)
        or not EXPECTED_A1_CANDIDATE_PATHS <= set(candidate_paths)
    ):
        return
    source_manifest = load_json(
        ROOT
        / "local-analysis/sources/claude-code-2.1.220-official/SOURCE_MANIFEST.json",
        errors,
    )
    bundle_anchors = load_json(
        ROOT
        / "local-analysis/sources/claude-code-2.1.220-official/"
        "bundle-anchors-linux-x64.json",
        errors,
    )
    candidate_evidence = load_json(
        ROOT
        / "local-analysis/sources/claude-code-2.1.220-official/"
        "CANDIDATE_EVIDENCE.json",
        errors,
    )
    linux_artifacts = [
        artifact
        for artifact in source_manifest.get("artifacts", [])
        if isinstance(artifact, dict)
        and artifact.get("version") == "2.1.220"
        and "linux" in artifact.get("os", [])
        and "x64" in artifact.get("cpu", [])
    ]
    if len(linux_artifacts) != 1:
        errors.append(
            f"SOURCE_MANIFEST 应唯一标识 Linux x64 2.1.220，实际 {len(linux_artifacts)}"
        )
        return
    source_binary_sha256 = linux_artifacts[0].get("local_binary_sha256")
    candidate_binary_sha256 = candidate_evidence.get("static_baseline", {}).get(
        "binary_sha256"
    )
    if source_binary_sha256 != EXPECTED_CLAUDE_21220_SHA256:
        errors.append("SOURCE_MANIFEST Linux A1 二进制摘要不是预期 Claude 2.1.220")
    if candidate_binary_sha256 != EXPECTED_CLAUDE_21220_SHA256:
        errors.append("CANDIDATE_EVIDENCE static_baseline 未绑定预期 Claude 2.1.220")
    if candidate_binary_sha256 != source_binary_sha256:
        errors.append("CANDIDATE_EVIDENCE 与 SOURCE_MANIFEST 的 Linux A1 摘要不一致")
    binary_relative = linux_artifacts[0].get("local_binary")
    binary_path = ROOT / str(binary_relative)
    if not binary_path.is_file():
        errors.append(f"SOURCE_MANIFEST 指向的 Linux A1 二进制不存在：{binary_relative}")
    else:
        actual_binary_sha256 = hashlib.sha256(binary_path.read_bytes()).hexdigest()
        if actual_binary_sha256 != source_binary_sha256:
            errors.append(
                "SOURCE_MANIFEST 指向的实际 Linux A1 二进制摘要不匹配："
                f"实际 {actual_binary_sha256}"
            )
        if binary_path.stat().st_size != linux_artifacts[0].get("local_binary_size"):
            errors.append("SOURCE_MANIFEST 指向的实际 Linux A1 二进制大小不匹配")
    if candidate_evidence.get("target_version") != "2.1.220":
        errors.append("CANDIDATE_EVIDENCE target_version 不是 2.1.220")
    if bundle_anchors.get("binary_sha256") != EXPECTED_CLAUDE_21220_SHA256:
        errors.append("bundle anchors 未绑定预期 Claude 2.1.220 二进制")
    extractor_path = ROOT / "tools/official_client_capture/extract_claude_bundle.py"
    analyzer_path = ROOT / "tools/official_client_capture/claude_bundle_reachability.py"
    extractor_sha256 = hashlib.sha256(extractor_path.read_bytes()).hexdigest()
    analyzer_sha256 = hashlib.sha256(analyzer_path.read_bytes()).hexdigest()
    if bundle_anchors.get("extractor_sha256") != extractor_sha256:
        errors.append("bundle anchors 的提取器摘要与当前文件不一致")
    static_baseline = candidate_evidence.get("static_baseline", {})
    if static_baseline.get("analyzer_sha256") != analyzer_sha256:
        errors.append("CANDIDATE_EVIDENCE 的分析器摘要与当前文件不一致")
    main_modules = [
        module
        for module in bundle_anchors.get("modules", [])
        if isinstance(module, dict)
        and module.get("name") == "/$bunfs/root/src/entrypoints/cli.js"
    ]
    if len(main_modules) != 1:
        errors.append(f"bundle anchors 主模块数量应为 1，实际 {len(main_modules)}")
    elif static_baseline.get("bundle_sha256") != main_modules[0].get("sha256"):
        errors.append("CANDIDATE_EVIDENCE 的 bundle 摘要与 bundle anchors 不一致")
    candidates_by_id = {
        str(candidate.get("candidate")): candidate
        for candidate in candidate_evidence.get("candidates", [])
        if isinstance(candidate, dict)
    }
    expected_candidates = {
        "CAND-UA-ENTRYPOINT": (
            "claude-cli/",
            "a20d7b8f44d989868f38d6bc90c9777b1e0d9a06d44e8c314ebf50692fbd3663",
            "fetch",
        ),
        "CAND-BODY-BILLING": (
            "x-anthropic-billing-header",
            "d29b00f5d6f5a2369cb2e0da9ec2de59c1d8f1d7b2cec975411ceda37b4bb23c",
            "messages_create",
        ),
    }
    for candidate_id, (literal, alpha_sha256, sink) in expected_candidates.items():
        candidate = candidates_by_id.get(candidate_id, {})
        if candidate.get("locator_literal") != literal:
            errors.append(f"{candidate_id} locator_literal 漂移")
        if alpha_sha256 not in candidate.get("alpha_sha256_linux", []):
            errors.append(f"{candidate_id} Linux 语义锚点漂移")
        if sink not in candidate.get("sinks_within_window", []):
            errors.append(f"{candidate_id} 缺少声明的局部 sink 线索：{sink}")


def validate_selected_denominator_declarations(
    data: dict[str, Any], errors: list[str]
) -> None:
    """要求十八条有限样本规则声明非循环选择的精确分母。"""

    all_rule_items = [
        *data.get("rules", []),
        *data.get("replacement_rules", []),
        *data.get("additional_rules", []),
    ]
    selected_rules = {
        str(item.get("id")): item
        for item in all_rule_items
        if isinstance(item, dict)
        and str(item.get("id")) in EXPECTED_SELECTED_DENOMINATORS
    }
    if set(selected_rules) != set(EXPECTED_SELECTED_DENOMINATORS):
        errors.append(
            "有限样本规则集合与 verification_denominators 审计基线不一致："
            f"实际 {sorted(selected_rules)}"
        )
    declared_denominator_ids = {
        str(item.get("id"))
        for item in all_rule_items
        if isinstance(item, dict) and "verification_denominators" in item
    }
    if declared_denominator_ids != set(EXPECTED_SELECTED_DENOMINATORS):
        errors.append(
            "verification_denominators 声明规则集合变化："
            f"实际 {sorted(declared_denominator_ids)}"
        )
    for rule_id, expected in EXPECTED_SELECTED_DENOMINATORS.items():
        rule = selected_rules.get(rule_id, {})
        if rule.get("status", {}).get("disposition") != "observed":
            errors.append(f"{rule_id} 当前证据边界下必须保持 observed")
        if rule.get("atomicity", {}).get("atomic") is not True:
            errors.append(f"{rule_id} 有限样本命题不是原子命题")
        declarations = rule.get("verification_denominators", [])
        if not isinstance(declarations, list):
            errors.append(f"{rule_id}.verification_denominators 必须是数组")
            continue
        evidence_catalog_ids = {
            str(ref.get("catalog_ref"))
            for ref in rule.get("evidence_refs", [])
            if isinstance(ref, dict)
        }
        actual: list[tuple[str, str, int, tuple[str, ...]]] = []
        for declaration in declarations:
            if not isinstance(declaration, dict):
                errors.append(f"{rule_id}.verification_denominators 含非对象条目")
                continue
            actual.append(
                (
                    str(declaration.get("channel")),
                    str(declaration.get("unit")),
                    declaration.get("expected"),
                    tuple(declaration.get("catalog_refs", [])),
                )
            )
            undeclared_evidence = set(declaration.get("catalog_refs", [])) - (
                evidence_catalog_ids
            )
            if undeclared_evidence:
                errors.append(
                    f"{rule_id}.verification_denominators 引用了未列入 evidence_refs "
                    f"的 catalog：{sorted(undeclared_evidence)}"
                )
        if actual != expected:
            errors.append(
                f"{rule_id}.verification_denominators 不匹配："
                f"实际 {actual}，预期 {expected}"
            )


def validate_tls002_denominator(data: dict[str, Any], errors: list[str]) -> int:
    """从四个原始 pcap 复算 SPEC-TLS-002 的全部八个 ClientHello。"""

    rules = [
        item
        for item in [
            *data.get("rules", []),
            *data.get("replacement_rules", []),
            *data.get("additional_rules", []),
        ]
        if isinstance(item, dict) and item.get("id") == "SPEC-TLS-002"
    ]
    if len(rules) != 1:
        errors.append(f"SPEC-TLS-002 条目数应为 1，实际 {len(rules)}")
        return 0
    catalog_ids = {
        str(ref.get("catalog_ref"))
        for ref in rules[0].get("evidence_refs", [])
        if isinstance(ref, dict)
    }
    required_catalogs = {"BASELINE-P", "HISTORICAL-220"}
    if not required_catalogs <= catalog_ids:
        errors.append(
            "SPEC-TLS-002 未同时绑定 BASELINE-P 与 HISTORICAL-220"
        )
        return 0

    catalog = data.get("evidence_catalog", {})
    analysis_relative_paths: set[str] = set()
    pcap_relative_paths: set[str] = set()
    for catalog_id in sorted(required_catalogs):
        entry = catalog.get(catalog_id, {}) if isinstance(catalog, dict) else {}
        for relative in entry.get("paths", []):
            relative_text = str(relative)
            if (
                "/analysis/direct/claude-http/" in relative_text
                and relative_text.endswith(".json")
            ):
                analysis_relative_paths.add(relative_text)
            if relative_text.endswith("/traffic.pcap"):
                pcap_relative_paths.add(relative_text)
    if analysis_relative_paths != EXPECTED_TLS_ANALYSIS_PATHS:
        errors.append(
            "SPEC-TLS-002 ClientHello 分析文件集合变化："
            f"实际 {sorted(analysis_relative_paths)}"
        )
    if pcap_relative_paths != EXPECTED_TLS_PCAP_PATHS:
        errors.append(
            f"SPEC-TLS-002 原始 pcap 集合变化：实际 {sorted(pcap_relative_paths)}"
        )
    analysis_paths = [ROOT / relative for relative in sorted(analysis_relative_paths)]

    client_hello_count = 0
    for path in analysis_paths:
        analysis = load_json(path, errors)
        client_hellos = analysis.get("client_hellos", [])
        if not isinstance(client_hellos, list):
            errors.append(f"ClientHello 列表不是数组：{path.relative_to(ROOT)}")
            continue
        if analysis.get("client_hello_count") != len(client_hellos):
            errors.append(
                f"ClientHello 文件内分母不一致：{path.relative_to(ROOT)}，"
                f"声明 {analysis.get('client_hello_count')}，实际 {len(client_hellos)}"
            )
        client_hello_count += len(client_hellos)
        for index, client_hello in enumerate(client_hellos):
            if not isinstance(client_hello, dict) or client_hello.get("alpn") != [
                "http/1.1"
            ]:
                errors.append(
                    f"SPEC-TLS-002 ALPN 漂移：{path.relative_to(ROOT)}"
                    f"#{index + 1}={client_hello!r}"
                )
    if client_hello_count != 8:
        errors.append(
            f"SPEC-TLS-002 派生分析应绑定 8 个 ClientHello，实际 {client_hello_count}"
        )
    parser = load_pcap_parser(errors)
    if parser is None:
        return 0
    raw_client_hellos = [
        observation
        for relative in sorted(pcap_relative_paths)
        for observation in parse_raw_client_hellos(ROOT / relative, parser, errors)
    ]
    if len(raw_client_hellos) != 8:
        errors.append(
            f"SPEC-TLS-002 原始 pcap 应解析出 8 个 ClientHello，"
            f"实际 {len(raw_client_hellos)}"
        )
    for index, observation in enumerate(raw_client_hellos, start=1):
        if observation.get("alpn") != ["http/1.1"]:
            errors.append(
                f"SPEC-TLS-002 原始 pcap ALPN 漂移："
                f"#{index}={observation.get('alpn')!r}"
            )
    return len(raw_client_hellos)


def validate_tls003_p_denominator(data: dict[str, Any], errors: list[str]) -> int:
    """从两个原始 pcap 复算 SPEC-TLS-003 的全部四个 ClientHello。"""

    catalog = data.get("evidence_catalog", {})
    entry = catalog.get("ALLHOSTS", {}) if isinstance(catalog, dict) else {}
    analysis_relative_paths = {
        str(relative)
        for relative in entry.get("paths", [])
        if "/analysis/direct/claude-http/" in str(relative)
        and str(relative).endswith(".json")
    }
    pcap_relative_paths = {
        str(relative)
        for relative in entry.get("paths", [])
        if str(relative).endswith("/traffic.pcap")
    }
    manifest_relative_paths = {
        str(relative)
        for relative in entry.get("paths", [])
        if str(relative).endswith("/manifest.json")
    }
    if analysis_relative_paths != EXPECTED_ALLHOSTS_ANALYSIS_PATHS:
        errors.append(
            "SPEC-EP-001 ALLHOSTS 分析文件集合变化："
            f"实际 {sorted(analysis_relative_paths)}"
        )
    if pcap_relative_paths != EXPECTED_ALLHOSTS_PCAP_PATHS:
        errors.append(
            "SPEC-EP-001 ALLHOSTS 原始 pcap 集合变化："
            f"实际 {sorted(pcap_relative_paths)}"
        )
    if manifest_relative_paths != EXPECTED_ALLHOSTS_MANIFEST_PATHS:
        errors.append(
            "SPEC-EP-001 ALLHOSTS manifest 集合变化："
            f"实际 {sorted(manifest_relative_paths)}"
        )
    for manifest_relative in sorted(manifest_relative_paths):
        manifest = load_json(ROOT / manifest_relative, errors)
        direct_cases = [
            case
            for case in manifest.get("case_results", [])
            if isinstance(case, dict)
            and case.get("scenario") == "s1"
            and case.get("capture", {}).get("mitm_port") is None
        ]
        if len(direct_cases) != 1:
            errors.append(
                f"ALLHOSTS manifest 的 direct s1 数量应为 1：{manifest_relative}"
            )
        elif direct_cases[0].get("capture", {}).get("bpf") != "tcp port 443":
            errors.append(
                f"ALLHOSTS 未关闭 host 预过滤：{manifest_relative}"
            )
    analysis_paths = [ROOT / relative for relative in sorted(analysis_relative_paths)]
    client_hello_count = 0
    for path in analysis_paths:
        analysis = load_json(path, errors)
        client_hellos = analysis.get("client_hellos", [])
        if not isinstance(client_hellos, list):
            errors.append(f"ALLHOSTS ClientHello 列表不是数组：{path.relative_to(ROOT)}")
            continue
        if analysis.get("client_hello_count") != len(client_hellos):
            errors.append(f"ALLHOSTS 文件内分母不一致：{path.relative_to(ROOT)}")
        client_hello_count += len(client_hellos)
        for index, client_hello in enumerate(client_hellos):
            if not isinstance(client_hello, dict) or client_hello.get("sni") != (
                "api.anthropic.com"
            ):
                errors.append(
                    f"SPEC-TLS-003 派生分析 SNI 漂移：{path.relative_to(ROOT)}"
                    f"#{index + 1}={client_hello!r}"
                )
    if client_hello_count != 4:
        errors.append(
            f"SPEC-TLS-003 派生分析应绑定 4 个 ClientHello，实际 {client_hello_count}"
        )
    parser = load_pcap_parser(errors)
    if parser is None:
        return 0
    raw_client_hellos = [
        observation
        for relative in sorted(pcap_relative_paths)
        for observation in parse_raw_client_hellos(ROOT / relative, parser, errors)
    ]
    if len(raw_client_hellos) != 4:
        errors.append(
            f"SPEC-TLS-003 原始 pcap 应解析出 4 个 ClientHello，"
            f"实际 {len(raw_client_hellos)}"
        )
    for index, observation in enumerate(raw_client_hellos, start=1):
        if observation.get("sni") != "api.anthropic.com":
            errors.append(
                f"SPEC-TLS-003 原始 pcap SNI 漂移："
                f"#{index}={observation.get('sni')!r}"
            )
    return len(raw_client_hellos)


def validate_rule_ledger(data: dict[str, Any], errors: list[str]) -> dict[str, int]:
    if data.get("schema_version") != "claude-egress-rule-ledger/v1":
        errors.append("规则台账 schema_version 不匹配")
    if data.get("target_version") != "2.1.220":
        errors.append("规则台账 target_version 不匹配")
    denominator_semantics = str(data.get("denominator_field_semantics", ""))
    if "不代表" not in denominator_semantics or "verified" not in (
        denominator_semantics
    ):
        errors.append("规则台账未声明有限分母字段不等于 verified 状态")
    rules = data.get("rules", [])
    replacements = data.get("replacement_rules", [])
    additional = data.get("additional_rules", [])
    catalog = data.get("evidence_catalog", {})
    if not all(isinstance(value, list) for value in (rules, replacements, additional)):
        errors.append("规则台账的 rules/replacement_rules/additional_rules 必须是数组")
        return {}

    required = (
        "id",
        "revision",
        "domain",
        "status",
        "atomicity",
        "retained_claim",
        "scope",
        "evidence_refs",
        "missing_refs",
        "required_channels",
        "next_probe",
        "superseded_by",
        "notes",
    )
    ids: list[str] = []
    dispositions: Counter[str] = Counter()
    for item in rules:
        if not isinstance(item, dict):
            errors.append("规则台账含非对象条目")
            continue
        rule_id = str(item.get("id", "<unknown>"))
        require_fields(item, required, rule_id, errors)
        ids.append(rule_id)
        status = item.get("status", {})
        disposition = status.get("disposition") if isinstance(status, dict) else None
        dispositions[str(disposition)] += 1
        for ref in item.get("evidence_refs", []):
            catalog_ref = ref.get("catalog_ref") if isinstance(ref, dict) else None
            if catalog_ref not in catalog:
                errors.append(f"{rule_id} 引用未知 evidence_catalog：{catalog_ref}")

    if set(ids) != HISTORICAL_IDS or len(ids) != len(HISTORICAL_IDS):
        errors.append("规则台账没有完整且唯一地处置 36 个历史 ID")
    normalized_dispositions = {
        disposition: dispositions.get(disposition, 0)
        for disposition in EXPECTED_DISPOSITIONS
    }
    if normalized_dispositions != EXPECTED_DISPOSITIONS:
        errors.append(
            "历史状态统计不一致："
            f"实际 {normalized_dispositions}，预期 {EXPECTED_DISPOSITIONS}"
        )
    summary = data.get("historical_36_summary", {})
    if summary.get("total") != len(HISTORICAL_IDS):
        errors.append("historical_36_summary.total 应为 36")
    for disposition, expected in EXPECTED_DISPOSITIONS.items():
        if summary.get(disposition) != expected:
            errors.append(f"historical_36_summary.{disposition} 应为 {expected}")
        expected_ids = {
            str(item.get("id"))
            for item in rules
            if isinstance(item, dict)
            and item.get("status", {}).get("disposition") == disposition
        }
        summary_ids = set(summary.get(f"{disposition}_ids", []))
        if summary_ids != expected_ids:
            errors.append(
                f"historical_36_summary.{disposition}_ids 不一致："
                f"实际 {sorted(summary_ids)}，预期 {sorted(expected_ids)}"
            )
    active_summary = data.get("active_egress_summary", {})
    expected_active = {
        "historical_verified": EXPECTED_DISPOSITIONS["verified"],
        "replacement_verified": 0,
        "replacement_observed": len(EXPECTED_REPLACEMENTS),
        "additional_verified": 0,
        "additional_observed": len(EXPECTED_ADDITIONAL),
        "total_verified": 0,
    }
    for field, expected in expected_active.items():
        if active_summary.get(field) != expected:
            errors.append(f"active_egress_summary.{field} 应为 {expected}")

    replacement_ids = {
        item.get("id") for item in replacements if isinstance(item, dict)
    }
    if replacement_ids != EXPECTED_REPLACEMENTS:
        errors.append(f"替代规则 ID 不一致：{sorted(replacement_ids)}")
    if len(replacements) != len(EXPECTED_REPLACEMENTS):
        errors.append("替代规则数组含重复或多余条目")
    if set(ids) & replacement_ids:
        errors.append("替代规则复用了历史 ID")
    additional_ids = {
        item.get("id") for item in additional if isinstance(item, dict)
    }
    if additional_ids != EXPECTED_ADDITIONAL:
        errors.append(f"新增规则 ID 不一致：{sorted(additional_ids)}")
    if len(additional) != len(EXPECTED_ADDITIONAL):
        errors.append("新增规则数组含重复或多余条目")
    if (set(ids) | replacement_ids) & additional_ids:
        errors.append("新增规则复用了历史或替代规则 ID")
    for item in [*replacements, *additional]:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id", "<unknown>"))
        require_fields(item, required, rule_id, errors)
        expected_status = "observed"
        if item.get("status", {}).get("disposition") != expected_status:
            errors.append(f"活动规则 {rule_id} 当前应为 {expected_status}")
        for ref in item.get("evidence_refs", []):
            catalog_ref = ref.get("catalog_ref") if isinstance(ref, dict) else None
            if catalog_ref not in catalog:
                errors.append(f"{rule_id} 引用未知 evidence_catalog：{catalog_ref}")

    if not isinstance(catalog, dict):
        errors.append("evidence_catalog 必须是对象")
        return dict(dispositions)
    for catalog_id, entry in catalog.items():
        if not isinstance(entry, dict):
            errors.append(f"evidence_catalog.{catalog_id} 必须是对象")
            continue
        for relative in entry.get("paths", []):
            if not (ROOT / relative).is_file():
                errors.append(f"证据文件不存在：{catalog_id} -> {relative}")
    for item in [*rules, *replacements, *additional]:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id", "<unknown>"))
        evidence_refs = item.get("evidence_refs", [])
        catalog_refs = [
            ref.get("catalog_ref")
            for ref in evidence_refs
            if isinstance(ref, dict)
        ]
        if len(catalog_refs) != len(set(catalog_refs)):
            errors.append(f"{rule_id} evidence_refs 存在重复 catalog_ref")
        available_channels = {
            channel
            for catalog_ref in catalog_refs
            if isinstance(catalog.get(catalog_ref), dict)
            for channel in catalog[catalog_ref].get("channels", [])
        }
        if item.get("status", {}).get("disposition") == "verified":
            if item.get("atomicity", {}).get("atomic") is not True:
                errors.append(f"{rule_id} verified 规则不是原子命题")
            missing_channels = set(item.get("required_channels", [])) - available_channels
            if missing_channels:
                errors.append(
                    f"{rule_id} verified 规则缺少所需证据通道：{sorted(missing_channels)}"
                )
            if "M" not in available_channels:
                errors.append(f"{rule_id} verified 规则没有 M 绑定")
            domain_requirements = {
                "tls": {"P", "M"},
                "protocol": {"J", "M"},
                "header": {"A1", "J", "M"},
                "body": {"A1", "J", "M"},
                "endpoint": {"J", "M"},
                "connection_retry": {"P", "J", "M"},
            }
            domain_required = domain_requirements.get(str(item.get("domain")), {"M"})
            domain_missing = domain_required - available_channels
            if domain_missing:
                errors.append(
                    f"{rule_id} 不满足 {item.get('domain')} 域最低证据："
                    f"缺少 {sorted(domain_missing)}"
                )
    common_scope = data.get("common_scope", {})
    if common_scope.get("profile_name") != "SCOPE-BASELINE":
        errors.append("规则台账未把共同范围命名为 SCOPE-BASELINE")
    if common_scope.get("argv_manifested") is not False:
        errors.append("规则台账没有记录 argv 未进入 manifest 的证据边界")
    finite_scopes = common_scope.get("finite_evidence_scopes", {})
    if set(finite_scopes) != {"SCOPE-J6", "SCOPE-P8", "SCOPE-ALLHOSTS-P4"}:
        errors.append("规则台账的有限证据 scope 集合不完整")
    elif (
        "不得使用待证" not in str(finite_scopes["SCOPE-J6"])
        or "不得按待证" not in str(finite_scopes["SCOPE-P8"])
        or "不得按待证" not in str(finite_scopes["SCOPE-ALLHOSTS-P4"])
    ):
        errors.append("有限证据 scope 未声明非循环分母选择条件")
    if common_scope.get("clean_environment_keys") != ["HOME", "PATH"]:
        errors.append("规则台账没有绑定基线 clean environment keys")
    if "未记录 injected_probe_env" not in str(
        common_scope.get("probe_env_manifest_boundary", "")
    ):
        errors.append("规则台账没有区分 probe env 字段缺失与空对象")
    driver_relative = common_scope.get("current_driver_source")
    driver_path = ROOT / str(driver_relative)
    if not driver_path.is_file():
        errors.append(f"当前抓包 driver 不存在：{driver_relative}")
    else:
        driver_sha256 = hashlib.sha256(driver_path.read_bytes()).hexdigest()
        if driver_sha256 != common_scope.get("current_driver_source_sha256"):
            errors.append(f"当前抓包 driver 摘要变化：实际 {driver_sha256}")
    all_items = [*rules, *replacements, *additional]
    verified_ids = {
        str(item.get("id"))
        for item in all_items
        if isinstance(item, dict)
        and item.get("status", {}).get("disposition") == "verified"
    }
    if verified_ids:
        errors.append(f"当前完整 M 边界下不应存在 formally verified：{sorted(verified_ids)}")
    for item in all_items:
        if not isinstance(item, dict) or str(item.get("id")) not in (
            EXPECTED_SELECTED_DENOMINATORS
        ):
            continue
        missing_text = json.dumps(item.get("missing_refs", []), ensure_ascii=False)
        if "完整 M 绑定" not in missing_text:
            errors.append(f"{item.get('id')} 未记录阻止升格的完整 M 缺口")
    return normalized_dispositions


def validate_hitcc(data: dict[str, Any], errors: list[str]) -> dict[str, int]:
    if data.get("schema_version") != "claude-hitcc-coverage/v1":
        errors.append("HitCC 台账 schema_version 不匹配")
    inventory = data.get("source_inventory", {})
    if inventory.get("git_commit") != "f4556e5b18a65232023998219e53c2598cc17d82":
        errors.append("HitCC 台账没有绑定预期 commit")
    if inventory.get("markdown_files_total") != 112:
        errors.append("HitCC 台账的 Markdown 文件数不是 112")
    if inventory.get("pretty_bundle_present") is not False:
        errors.append("HitCC 台账没有记录 pretty bundle 缺失边界")
    actual_markdown = sum(1 for _ in HITCC_ROOT.rglob("*.md"))
    docs_markdown = sum(1 for _ in (HITCC_ROOT / "docs").rglob("*.md"))
    if actual_markdown != inventory.get("markdown_files_total"):
        errors.append(f"HitCC Markdown 文件数变化：实际 {actual_markdown}")
    if docs_markdown != inventory.get("markdown_files_under_docs"):
        errors.append(f"HitCC docs/ Markdown 文件数变化：实际 {docs_markdown}")
    try:
        commit = subprocess.run(
            ["git", "-C", str(HITCC_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"无法复核 HitCC commit：{exc}")
    else:
        if commit != inventory.get("git_commit"):
            errors.append(f"HitCC commit 变化：实际 {commit}")
    clues = data.get("clues", [])
    if not isinstance(clues, list):
        errors.append("HitCC 台账 clues 必须是数组")
        return {}
    required = (
        "clue_id",
        "category",
        "proposition",
        "source_version",
        "source_path",
        "source_lines",
        "source_kind",
        "old_reachability",
        "target_version",
        "spec_rule_ids",
        "coverage",
        "drift_status",
        "current_evidence",
        "next_probe",
        "notes",
    )
    ids: list[str] = []
    counts: Counter[str] = Counter()
    for item in clues:
        if not isinstance(item, dict):
            errors.append("HitCC 台账含非对象条目")
            continue
        clue_id = str(item.get("clue_id", "<unknown>"))
        require_fields(item, required, clue_id, errors)
        ids.append(clue_id)
        coverage = item.get("coverage")
        drift = item.get("drift_status")
        if coverage not in HITCC_COVERAGE:
            errors.append(f"{clue_id} coverage 非法：{coverage}")
        else:
            counts[coverage] += 1
        if drift not in HITCC_DRIFT:
            errors.append(f"{clue_id} drift_status 非法：{drift}")
        refs = item.get("spec_rule_ids", [])
        if coverage == "covered" and not refs:
            errors.append(f"{clue_id} 标记 covered 但没有规则映射")
        for ref in refs:
            if str(ref).startswith("SPEC-") and ref not in KNOWN_SPEC_IDS:
                errors.append(f"{clue_id} 引用未知正式规则：{ref}")
        relative = item.get("source_path")
        source = ROOT / str(relative)
        if not source.is_file():
            errors.append(f"{clue_id} 来源文件不存在：{relative}")
        else:
            validate_line_range(source, str(item.get("source_lines", "")), clue_id, errors)
    if len(ids) != len(set(ids)):
        errors.append("HitCC clue_id 存在重复")
    if len(clues) < 40:
        errors.append(f"HitCC 原子线索少于 40：{len(clues)}")
    summary = data.get("summary", {})
    expected = summary.get("coverage_counts", {})
    if dict(counts) != expected:
        errors.append(f"HitCC 覆盖统计不一致：实际 {dict(counts)}，摘要 {expected}")
    if expected != EXPECTED_HITCC_COVERAGE:
        errors.append(
            f"HitCC 覆盖审计基线变化：实际 {expected}，预期 {EXPECTED_HITCC_COVERAGE}"
        )
    if summary.get("total_clues") != len(clues):
        errors.append("HitCC summary.total_clues 与 clues 条数不一致")

    documents = data.get("document_inventory", [])
    if not isinstance(documents, list):
        errors.append("HitCC 台账 document_inventory 必须是数组")
        documents = []
    document_paths: list[str] = []
    document_by_path: dict[str, dict[str, Any]] = {}
    document_counts: Counter[str] = Counter()
    clue_id_set = set(ids)
    for index, item in enumerate(documents):
        label = f"HitCC document_inventory[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 不是对象")
            continue
        require_fields(
            item,
            ("path", "disposition", "mapping_status", "clue_ids", "notes"),
            label,
            errors,
        )
        relative = str(item.get("path", ""))
        disposition = item.get("disposition")
        mapping = item.get("mapping_status")
        clue_refs = item.get("clue_ids", [])
        document_paths.append(relative)
        document_by_path[relative] = item
        if disposition not in HITCC_DOCUMENT_DISPOSITIONS:
            errors.append(f"{label} disposition 非法：{disposition}")
        if mapping not in HITCC_DOCUMENT_MAPPINGS:
            errors.append(f"{label} mapping_status 非法：{mapping}")
        if not isinstance(clue_refs, list):
            errors.append(f"{label} clue_ids 必须是数组")
            clue_refs = []
        for clue_ref in clue_refs:
            if clue_ref not in clue_id_set:
                errors.append(f"{label} 引用未知 clue_id：{clue_ref}")
        if disposition == "clue_source":
            document_counts["clue_source"] += 1
            if mapping == "mapped":
                document_counts["clue_source_mapped"] += 1
                if not clue_refs:
                    errors.append(f"{label} 标记 mapped 但 clue_ids 为空")
            elif mapping == "unmapped":
                document_counts["clue_source_unmapped"] += 1
                if clue_refs:
                    errors.append(f"{label} 标记 unmapped 但仍含 clue_ids")
            else:
                errors.append(f"{label} 是 clue_source 但 mapping_status={mapping}")
        else:
            document_counts[str(disposition)] += 1
            if mapping != "not_applicable" or clue_refs:
                errors.append(f"{label} 非 clue_source，不应建立 clue 映射")
        if not (ROOT / relative).is_file():
            errors.append(f"{label} 路径不存在：{relative}")

    actual_document_paths = {
        path.relative_to(ROOT).as_posix() for path in HITCC_ROOT.rglob("*.md")
    }
    if len(document_paths) != len(set(document_paths)):
        errors.append("HitCC document_inventory 路径存在重复")
    if set(document_paths) != actual_document_paths:
        errors.append("HitCC document_inventory 未精确覆盖 112 个 Markdown 路径")
    computed_document_counts = {
        "total": len(documents),
        **{
            key: document_counts.get(key, 0)
            for key in (
                "clue_source",
                "clue_source_mapped",
                "clue_source_unmapped",
                "cross_reference_only",
                "no_egress_clue",
            )
        },
    }
    if computed_document_counts != summary.get("document_counts"):
        errors.append(
            "HitCC 文档统计不一致："
            f"实际 {computed_document_counts}，摘要 {summary.get('document_counts')}"
        )
    if computed_document_counts != EXPECTED_HITCC_DOCUMENTS:
        errors.append(
            "HitCC 文档审计基线变化："
            f"实际 {computed_document_counts}，预期 {EXPECTED_HITCC_DOCUMENTS}"
        )
    expected_document_refs: dict[str, set[str]] = {}
    for clue in clues:
        if not isinstance(clue, dict):
            continue
        clue_id = str(clue.get("clue_id"))
        source_path = str(clue.get("source_path"))
        expected_document_refs.setdefault(source_path, set()).add(clue_id)
    supporting_mappings = data.get("supporting_document_mappings", [])
    if not isinstance(supporting_mappings, list):
        errors.append("HitCC supporting_document_mappings 必须是数组")
        supporting_mappings = []
    supporting_paths: list[str] = []
    for index, mapping in enumerate(supporting_mappings):
        label = f"HitCC supporting_document_mappings[{index}]"
        if not isinstance(mapping, dict):
            errors.append(f"{label} 不是对象")
            continue
        require_fields(mapping, ("path", "clue_ids", "notes"), label, errors)
        path = str(mapping.get("path", ""))
        supporting_paths.append(path)
        for clue_id in mapping.get("clue_ids", []):
            if clue_id not in clue_id_set:
                errors.append(f"{label} 引用未知 clue_id：{clue_id}")
            expected_document_refs.setdefault(path, set()).add(str(clue_id))
    if len(supporting_paths) != len(set(supporting_paths)):
        errors.append("HitCC supporting_document_mappings 路径重复")
    for path, expected_refs in expected_document_refs.items():
        source_document = document_by_path.get(path)
        if not source_document:
            errors.append(f"HitCC 线索来源不在 document_inventory：{path}")
            continue
        if source_document.get("mapping_status") != "mapped":
            errors.append(f"HitCC 线索来源未标 mapped：{path}")
        actual_refs = set(source_document.get("clue_ids", []))
        if actual_refs != expected_refs:
            errors.append(
                f"HitCC 文档反向映射不精确：{path}，实际 {sorted(actual_refs)}，"
                f"预期 {sorted(expected_refs)}"
            )
    for path, document_item in document_by_path.items():
        if document_item.get("mapping_status") == "mapped" and path not in expected_document_refs:
            errors.append(f"HitCC 文档无主来源或 supporting 声明却标 mapped：{path}")
    content = json.dumps(data, ensure_ascii=False).lower()
    for topic in (
        "traceparent",
        "claude_code_extra_metadata",
        "temperature",
        "toolsearch",
        "quota",
        "count_tokens",
        "currentdate",
        "telemetry",
    ):
        if topic not in content:
            errors.append(f"HitCC 台账缺少必盘主题：{topic}")
    return dict(counts)


def validate_source_2188(
    data: dict[str, Any], rules_data: dict[str, Any], errors: list[str]
) -> dict[str, int]:
    if data.get("schema_version") != "claude-source-coverage/v1":
        errors.append("2.1.88 台账 schema_version 不匹配")
    inventory = data.get("source_inventory", {})
    if inventory.get("file_count") != 1902:
        errors.append("2.1.88 台账的 src 文件数不是 1902")
    if inventory.get("path_and_content_sha256") != (
        "d865dbfa59f24563fedc767a425f1e2e35ff15a458f478ebf6ac24d800cef4a8"
    ):
        errors.append("2.1.88 台账的路径＋内容摘要不匹配")
    if inventory.get("provenance_manifest_present") is not False:
        errors.append("2.1.88 台账没有记录来源 manifest 缺失边界")
    if inventory.get("semantic_rule_inventory_scope") != ["src"]:
        errors.append("2.1.88 台账的语义规则盘点范围必须明确限制为 src")
    if inventory.get("dependency_semantic_audit_complete") is not False:
        errors.append("2.1.88 台账必须明确依赖源码语义审计未完成")
    source_files = sorted(
        (path for path in (SOURCE_2188_ROOT / "src").rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )
    if len(source_files) != inventory.get("file_count"):
        errors.append(f"2.1.88 src 文件数变化：实际 {len(source_files)}")
    aggregate = hashlib.sha256()
    for source_file in source_files:
        file_digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
        relative = source_file.relative_to(ROOT).as_posix()
        aggregate.update(f"{file_digest}  {relative}\n".encode())
    if aggregate.hexdigest() != inventory.get("path_and_content_sha256"):
        errors.append(f"2.1.88 路径＋内容摘要变化：实际 {aggregate.hexdigest()}")
    forensic_inventory = data.get("forensic_root_inventory", {})
    components = forensic_inventory.get("components", {})
    for inventory_id, (
        relative_root,
        expected_count,
        expected_bytes,
        expected_digest,
    ) in EXPECTED_SOURCE_ROOT_INVENTORIES.items():
        declared = (
            forensic_inventory
            if inventory_id == "root"
            else components.get(inventory_id, {})
        )
        expected_declared = {
            "root": relative_root,
            "file_count": expected_count,
            "byte_count": expected_bytes,
            "path_and_content_sha256": expected_digest,
        }
        for field, expected_value in expected_declared.items():
            if declared.get(field) != expected_value:
                errors.append(
                    f"2.1.88 {inventory_id} 取证快照字段 {field} 不匹配："
                    f"实际 {declared.get(field)!r}"
                )
        actual = compute_path_content_inventory(ROOT / relative_root)
        if actual != {
            "file_count": expected_count,
            "byte_count": expected_bytes,
            "path_and_content_sha256": expected_digest,
        }:
            errors.append(
                f"2.1.88 {inventory_id} 取证快照变化：实际 {actual}"
            )
        if inventory_id != "root" and declared.get(
            "semantic_rule_inventory_complete"
        ) is not False:
            errors.append(
                f"2.1.88 {inventory_id} 不得宣称语义规则盘点完成"
            )
    if sum(
        EXPECTED_SOURCE_ROOT_INVENTORIES[name][1]
        for name in ("src", "node_modules", "vendor")
    ) != EXPECTED_SOURCE_ROOT_INVENTORIES["root"][1]:
        errors.append("2.1.88 取证根三部分文件数无法闭合")
    symlinks = [path for path in SOURCE_2188_ROOT.rglob("*") if path.is_symlink()]
    if symlinks:
        errors.append(f"2.1.88 取证根出现未登记符号链接：{len(symlinks)} 个")
    scope_included = str(data.get("scope", {}).get("included", ""))
    if "候选机制全集" in scope_included:
        errors.append("2.1.88 scope 仍错误宣称当前台账是候选机制全集")
    if "node_modules" not in scope_included or "vendor" not in scope_included:
        errors.append("2.1.88 scope 未披露依赖源码尚未规则抽取")
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        errors.append("2.1.88 台账 rules 必须是数组")
        return {}
    required = (
        "source_rule_id",
        "category",
        "proposition",
        "source_paths",
        "source_kind",
        "target_version",
        "target_static_status",
        "spec_rule_ids",
        "current_evidence",
        "required_probe",
        "notes",
    )
    ids: list[str] = []
    counts: Counter[str] = Counter()
    source_ref_count = 0
    rule_source_pairs: set[tuple[str, str]] = set()
    pattern = re.compile(r"^(.+):(\d+)-(\d+)$")
    for item in rules:
        if not isinstance(item, dict):
            errors.append("2.1.88 台账含非对象条目")
            continue
        rule_id = str(item.get("source_rule_id", "<unknown>"))
        require_fields(item, required, rule_id, errors)
        ids.append(rule_id)
        status = item.get("target_static_status")
        if status not in SOURCE_STATUSES:
            errors.append(f"{rule_id} target_static_status 非法：{status}")
        else:
            counts[status] += 1
        refs = item.get("spec_rule_ids", [])
        if status == "runtime_verified" and not refs:
            errors.append(f"{rule_id} 标记 runtime_verified 但没有规则映射")
        for ref in refs:
            if str(ref).startswith("SPEC-") and ref not in KNOWN_SPEC_IDS:
                errors.append(f"{rule_id} 引用未知正式规则：{ref}")
        for source_ref in item.get("source_paths", []):
            source_ref_count += 1
            match = pattern.match(str(source_ref))
            if not match:
                errors.append(f"{rule_id} source_paths 格式无效：{source_ref}")
                continue
            relative, start_text, end_text = match.groups()
            rule_source_pairs.add((relative, rule_id))
            source = SOURCE_2188_ROOT / relative
            if not source.is_file():
                errors.append(f"{rule_id} 来源文件不存在：{source_ref}")
                continue
            line_count = count_lines(source)
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start or end > line_count:
                errors.append(
                    f"{rule_id} 行号越界：{source_ref}，文件共 {line_count} 行"
                )
    if len(ids) != len(set(ids)):
        errors.append("2.1.88 source_rule_id 存在重复")
    if len(rules) < 55:
        errors.append(f"2.1.88 原子候选少于 55：{len(rules)}")
    normalized_counts = {
        status: counts.get(status, 0) for status in SOURCE_STATUSES
    }
    expected = data.get("statistics", {}).get("by_target_static_status", {})
    if normalized_counts != expected:
        errors.append(
            f"2.1.88 状态统计不一致：实际 {normalized_counts}，摘要 {expected}"
        )
    if expected != EXPECTED_SOURCE_STATUSES:
        errors.append(
            f"2.1.88 状态审计基线变化：实际 {expected}，预期 {EXPECTED_SOURCE_STATUSES}"
        )
    changed_ids = {
        str(item.get("source_rule_id"))
        for item in rules
        if isinstance(item, dict) and item.get("target_static_status") == "changed"
    }
    if changed_ids != EXPECTED_CHANGED_SOURCE_RULES:
        errors.append(
            f"2.1.88 changed 清单变化：实际 {sorted(changed_ids)}"
        )

    file_inventory = data.get("file_inventory", [])
    if not isinstance(file_inventory, list):
        errors.append("2.1.88 台账 file_inventory 必须是数组")
        file_inventory = []
    inventory_paths: list[str] = []
    inventory_by_path: dict[str, dict[str, Any]] = {}
    file_counts: Counter[str] = Counter()
    source_rule_ids = set(ids)
    for index, item in enumerate(file_inventory):
        label = f"2.1.88 file_inventory[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 不是对象")
            continue
        require_fields(
            item,
            ("path", "disposition", "source_rule_ids", "scan_signals", "notes"),
            label,
            errors,
        )
        relative = str(item.get("path", ""))
        disposition = item.get("disposition")
        rule_refs = item.get("source_rule_ids", [])
        signals = item.get("scan_signals", [])
        inventory_paths.append(relative)
        inventory_by_path[relative] = item
        if disposition not in SOURCE_FILE_DISPOSITIONS:
            errors.append(f"{label} disposition 非法：{disposition}")
        else:
            file_counts[disposition] += 1
        if not isinstance(rule_refs, list) or not isinstance(signals, list):
            errors.append(f"{label} source_rule_ids/scan_signals 必须是数组")
            rule_refs = []
        for rule_ref in rule_refs:
            if rule_ref not in source_rule_ids:
                errors.append(f"{label} 引用未知 source_rule_id：{rule_ref}")
        if disposition in {"rule_source", "transitive_support"} and not rule_refs:
            errors.append(f"{label} {disposition} 但 source_rule_ids 为空")
        if disposition in {"no_direct_egress_signal", "egress_candidate_unmapped"} and rule_refs:
            errors.append(f"{label} {disposition} 不应映射 source_rule_ids")

    actual_inventory_paths = {
        path.relative_to(SOURCE_2188_ROOT).as_posix() for path in source_files
    }
    if len(inventory_paths) != len(set(inventory_paths)):
        errors.append("2.1.88 file_inventory 路径存在重复")
    if set(inventory_paths) != actual_inventory_paths:
        errors.append("2.1.88 file_inventory 未精确覆盖 1902 个 src 文件")
    computed_file_counts = {
        "total": len(file_inventory),
        **{
            disposition: file_counts.get(disposition, 0)
            for disposition in (
                "rule_source",
                "transitive_support",
                "no_direct_egress_signal",
                "egress_candidate_unmapped",
            )
        },
    }
    summary = data.get("summary", {})
    if computed_file_counts != summary.get("file_counts"):
        errors.append(
            "2.1.88 文件统计不一致："
            f"实际 {computed_file_counts}，摘要 {summary.get('file_counts')}"
        )
    if computed_file_counts != EXPECTED_SOURCE_FILES:
        errors.append(
            "2.1.88 文件审计基线变化："
            f"实际 {computed_file_counts}，预期 {EXPECTED_SOURCE_FILES}"
        )
    expected_rule_refs_by_path: dict[str, set[str]] = {}
    for relative, rule_id in rule_source_pairs:
        expected_rule_refs_by_path.setdefault(relative, set()).add(rule_id)
    for relative, expected_rule_refs in expected_rule_refs_by_path.items():
        item = inventory_by_path.get(relative)
        if not item:
            errors.append(f"规则直接来源不在 file_inventory：{relative}")
            continue
        actual_rule_refs = set(item.get("source_rule_ids", []))
        if item.get("disposition") != "rule_source":
            errors.append(f"规则直接来源未标 rule_source：{relative}")
        if actual_rule_refs != expected_rule_refs:
            errors.append(
                f"规则直接来源反向映射不精确：{relative}，实际 {sorted(actual_rule_refs)}，"
                f"预期 {sorted(expected_rule_refs)}"
            )
    for relative, item in inventory_by_path.items():
        if item.get("disposition") == "rule_source" and relative not in expected_rule_refs_by_path:
            errors.append(f"file_inventory 无规则引用却标 rule_source：{relative}")
    reverse_mapping = summary.get("reverse_mapping", {})
    if reverse_mapping.get("source_path_reference_count") != source_ref_count:
        errors.append("2.1.88 summary 的源码引用计数不一致")
    if reverse_mapping.get("unique_rule_file_association_count") != len(rule_source_pairs):
        errors.append("2.1.88 summary 的规则－文件关联计数不一致")
    if reverse_mapping.get("all_rule_source_paths_mapped") is not True:
        errors.append("2.1.88 summary 未声明全部已抽取规则来源完成反向映射")
    triage_counts = {
        # 这里只统计 file_inventory 已自报的 scan_signals；没有从源码独立重跑。
        "unmapped_with_lexical_signals": sum(
            1
            for item in file_inventory
            if isinstance(item, dict)
            and item.get("disposition") == "egress_candidate_unmapped"
            and bool(item.get("scan_signals"))
        ),
        "unreviewed_without_lexical_signals": sum(
            1
            for item in file_inventory
            if isinstance(item, dict)
            and item.get("disposition") == "egress_candidate_unmapped"
            and not item.get("scan_signals")
        ),
        "transitive_with_non_import_signals": sum(
            1
            for item in file_inventory
            if isinstance(item, dict)
            and item.get("disposition") == "transitive_support"
            and any(
                signal != "direct_import_from_rule_source"
                for signal in item.get("scan_signals", [])
            )
        ),
        "transitive_import_only": sum(
            1
            for item in file_inventory
            if isinstance(item, dict)
            and item.get("disposition") == "transitive_support"
            and not any(
                signal != "direct_import_from_rule_source"
                for signal in item.get("scan_signals", [])
            )
        ),
    }
    if triage_counts != summary.get("triage_limit_counts"):
        errors.append(
            "2.1.88 词法分流边界统计不一致："
            f"实际 {triage_counts}，摘要 {summary.get('triage_limit_counts')}"
        )
    if triage_counts != EXPECTED_SOURCE_TRIAGE_LIMITS:
        errors.append(
            "2.1.88 词法分流审计基线变化："
            f"实际 {triage_counts}，预期 {EXPECTED_SOURCE_TRIAGE_LIMITS}"
        )
    scan_reproducibility = summary.get("scan_reproducibility", {})
    if not isinstance(scan_reproducibility, dict):
        errors.append("2.1.88 summary.scan_reproducibility 必须是对象")
    else:
        if (
            scan_reproducibility.get("independently_recomputed_from_source")
            is not False
        ):
            errors.append("2.1.88 词法计数必须明确声明未从源码独立复算")
        if "scan_signals" not in json.dumps(
            scan_reproducibility, ensure_ascii=False
        ):
            errors.append(
                "2.1.88 scan_reproducibility 必须说明 checker 仅统计自报 scan_signals"
            )
    known_gaps = set(summary.get("known_single_disposition_gaps", []))
    if known_gaps != EXPECTED_SOURCE_GAPS:
        errors.append("2.1.88 已知单一 disposition 漏分清单变化")
    for path in known_gaps:
        if path not in inventory_by_path:
            errors.append(f"2.1.88 已知漏分路径不在 file_inventory：{path}")

    verification_audit = data.get("verification_audit", {})
    full_verified = set(
        verification_audit.get("full_historical_proposition_verified_ids", [])
    )
    full_observed = set(
        verification_audit.get("full_historical_proposition_observed_ids", [])
    )
    narrowed = set(verification_audit.get("narrowed_current_subclaim_ids", []))
    runtime_ids = {
        str(item.get("source_rule_id"))
        for item in rules
        if isinstance(item, dict)
        and item.get("target_static_status") == "runtime_verified"
    }
    rules_by_id = {
        str(item.get("source_rule_id")): item
        for item in rules
        if isinstance(item, dict)
    }
    current_verified_spec_ids = {
        str(item.get("id"))
        for item in [
            *rules_data.get("rules", []),
            *rules_data.get("replacement_rules", []),
            *rules_data.get("additional_rules", []),
        ]
        if isinstance(item, dict)
        and item.get("status", {}).get("disposition") == "verified"
    }
    if set(verification_audit.get("current_verified_spec_ids_snapshot", [])) != (
        current_verified_spec_ids
    ):
        errors.append("2.1.88 verification_audit 与当前 verified SPEC 集合不同步")
    if full_verified != runtime_ids:
        errors.append(
            "2.1.88 runtime_verified 条目与完整历史命题验证清单未双向闭合"
        )
    for source_rule_id in runtime_ids:
        spec_refs = set(rules_by_id.get(source_rule_id, {}).get("spec_rule_ids", []))
        if not spec_refs:
            errors.append(f"{source_rule_id} runtime_verified 却没有 SPEC 映射")
        elif not spec_refs <= current_verified_spec_ids:
            errors.append(
                f"{source_rule_id} runtime_verified 引用了非 verified SPEC："
                f"{sorted(spec_refs - current_verified_spec_ids)}"
            )
    semantic_boundary = str(
        verification_audit.get("manual_semantic_review_boundary", "")
    )
    if "不能自动证明" not in semantic_boundary or "人工语义复核" not in (
        semantic_boundary
    ):
        errors.append("2.1.88 verification_audit 未披露语义包含关系的人工边界")
    if full_observed != EXPECTED_FULL_SOURCE_OBSERVATIONS:
        errors.append("2.1.88 完整历史命题样本一致清单变化")
    for source_rule_id in full_observed:
        if rules_by_id.get(source_rule_id, {}).get("target_static_status") != "static_only":
            errors.append(f"{source_rule_id} 只能记为 static_only 样本一致")
    if narrowed != EXPECTED_NARROWED_SOURCE_SUBCLAIMS:
        errors.append("2.1.88 收窄子命题清单变化")
    for source_rule_id in narrowed:
        item = rules_by_id.get(source_rule_id, {})
        if item.get("target_static_status") != "static_only":
            errors.append(f"{source_rule_id} 收窄子命题不得计作 runtime_verified")
        if not item.get("observed_subclaim"):
            errors.append(f"{source_rule_id} 缺少 observed_subclaim")
    corrected_headers = {
        "SRC2188-HDR-005": ("x-claude-remote-container-id", "SPEC-HDR-017"),
        "SRC2188-HDR-006": ("x-claude-remote-session-id", "SPEC-HDR-018"),
        "SRC2188-HDR-007": ("x-client-app", "SPEC-HDR-016"),
    }
    for source_rule_id, (expected_header, expected_spec) in corrected_headers.items():
        item = rules_by_id.get(source_rule_id, {})
        if item.get("target_static_status") != "static_only":
            errors.append(f"{source_rule_id} 同名静态候选应标 static_only")
        if expected_header not in str(item.get("proposition", "")):
            errors.append(f"{source_rule_id} 未恢复 2.1.88 历史 Header 名称")
        if item.get("spec_rule_ids") != [expected_spec]:
            errors.append(f"{source_rule_id} 目标 SPEC 映射错误")
    sdk_migration = data.get("sdk_migration", {})
    if sdk_migration.get("source_sdk_version") != "0.74.0":
        errors.append("2.1.88 台账没有绑定旧 SDK 0.74.0")
    version_relative = sdk_migration.get("source_version_file")
    expected_version_relative = (
        "local-analysis/sources/claude-code-2.1.88/"
        "node_modules/@anthropic-ai/sdk/version.mjs"
    )
    if version_relative != expected_version_relative:
        errors.append("2.1.88 台账没有绑定旧 SDK version.mjs")
    else:
        version_path = ROOT / version_relative
        if not version_path.is_file():
            errors.append("2.1.88 SDK version.mjs 不存在")
        else:
            version_sha256 = hashlib.sha256(version_path.read_bytes()).hexdigest()
            expected_version_sha256 = (
                "5a3c5bb6fb24619124a856ac40defa1514ef7ca64e7d556ebce18da15ef7d942"
            )
            if version_sha256 != expected_version_sha256:
                errors.append(
                    f"2.1.88 SDK version.mjs 摘要变化：实际 {version_sha256}"
                )
            if sdk_migration.get("source_version_file_sha256") != version_sha256:
                errors.append("2.1.88 台账的 SDK version.mjs 摘要不匹配")
            if "VERSION = '0.74.0'" not in version_path.read_text(encoding="utf-8"):
                errors.append("2.1.88 SDK version.mjs 不再声明 0.74.0")
    if sdk_migration.get("target_sdk_version") != "0.94.0":
        errors.append("2.1.88 台账没有绑定目标 SDK 0.94.0")
    content = json.dumps(data, ensure_ascii=False).lower()
    for topic in (
        "system[0].text",
        "counttokens",
        "withretry",
        "oauth",
        "非流式",
        "telemetry",
    ):
        if topic not in content:
            errors.append(f"2.1.88 台账缺少必盘主题：{topic}")
    return {**normalized_counts, "source_refs": source_ref_count}


def validate_document(
    rules_data: dict[str, Any],
    hitcc: dict[str, Any],
    source: dict[str, Any],
    errors: list[str],
) -> None:
    try:
        document = SPEC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"无法读取规格文档：{exc}")
        return
    if "文档状态：**36 条规则已验证" in document or "全部处于「已验证」" in document:
        errors.append("规格文档仍宣称 36 条规则全部已验证")
    audit_match = re.search(
        r"## 2\.2 36 个历史编号逐条处置(.*?)## 2\.3 规则详表",
        document,
        flags=re.DOTALL,
    )
    if not audit_match:
        errors.append("规格文档缺少 2.2 历史编号逐条处置区")
        return
    audit_ids = set(re.findall(r"`(SPEC-[A-Z]+-\d{3})`", audit_match.group(1)))
    if audit_ids != HISTORICAL_IDS:
        missing = sorted(HISTORICAL_IDS - audit_ids)
        extra = sorted(audit_ids - HISTORICAL_IDS)
        errors.append(f"文档历史 ID 对账失败：缺少 {missing}，多出 {extra}")
    audit_rows = re.findall(
        r"^\|\s*`(SPEC-[A-Z]+-\d{3})`\s*\|\s*([^|]+?)\s*\|",
        audit_match.group(1),
        flags=re.MULTILINE,
    )
    audit_row_ids = [rule_id for rule_id, _ in audit_rows]
    if len(audit_row_ids) != len(set(audit_row_ids)):
        errors.append("规格文档 2.2 状态表含重复历史 ID")
    document_statuses: dict[str, str] = {}
    for rule_id, chinese_status in audit_rows:
        disposition = DOCUMENT_DISPOSITIONS.get(chinese_status.strip())
        if disposition is None:
            errors.append(f"规格文档 2.2 含未知中文状态：{rule_id}={chinese_status}")
            continue
        document_statuses[rule_id] = disposition
    ledger_statuses = {
        str(item.get("id")): str(item.get("status", {}).get("disposition"))
        for item in rules_data.get("rules", [])
        if isinstance(item, dict)
    }
    if document_statuses != ledger_statuses:
        for rule_id in sorted(HISTORICAL_IDS):
            document_status = document_statuses.get(rule_id)
            ledger_status = ledger_statuses.get(rule_id)
            if document_status != ledger_status:
                errors.append(
                    f"规格文档 2.2 与规则台账状态不一致：{rule_id} "
                    f"文档={document_status}，台账={ledger_status}"
                )
    for active_rule in EXPECTED_REPLACEMENTS | EXPECTED_ADDITIONAL:
        if f"#### {active_rule} " not in document:
            errors.append(f"规格文档缺少新增规则正文：{active_rule}")
    detail_match = re.search(
        r"## 2\.3 规则详表(.*?)## 2\.4 候选台账",
        document,
        flags=re.DOTALL,
    )
    if not detail_match:
        errors.append("规格文档缺少 2.3 规则详表区")
    else:
        detailed_statuses: dict[str, str] = {}
        detail_sections = re.finditer(
            r"^#### (SPEC-[A-Z]+-\d{3}) .*?\n(.*?)(?=^#### |^### |\Z)",
            detail_match.group(1),
            flags=re.MULTILINE | re.DOTALL,
        )
        for detail_section in detail_sections:
            rule_id = detail_section.group(1)
            if rule_id in detailed_statuses:
                errors.append(f"规格文档 2.3 重复规则详表：{rule_id}")
                continue
            status_match = re.search(
                r"^- \*\*状态\*\*：\*\*"
                r"(已验证|已观察|候选|已取代|响应兼容)"
                r"(?:／已观察)?\*\*",
                detail_section.group(2),
                flags=re.MULTILINE,
            )
            if not status_match:
                errors.append(f"规格文档 2.3 缺少可解析状态：{rule_id}")
                continue
            detailed_statuses[rule_id] = DOCUMENT_DISPOSITIONS[status_match.group(1)]
        all_ledger_statuses = {
            str(item.get("id")): str(item.get("status", {}).get("disposition"))
            for item in [
                *rules_data.get("rules", []),
                *rules_data.get("replacement_rules", []),
                *rules_data.get("additional_rules", []),
            ]
            if isinstance(item, dict)
        }
        if detailed_statuses != all_ledger_statuses:
            missing_details = sorted(set(all_ledger_statuses) - set(detailed_statuses))
            extra_details = sorted(set(detailed_statuses) - set(all_ledger_statuses))
            if missing_details or extra_details:
                errors.append(
                    "规格文档 2.3 规则集合不一致："
                    f"缺少 {missing_details}，多出 {extra_details}"
                )
            for rule_id in sorted(set(detailed_statuses) & set(all_ledger_statuses)):
                if detailed_statuses[rule_id] != all_ledger_statuses[rule_id]:
                    errors.append(
                        f"规格文档 2.3 与规则台账状态不一致：{rule_id} "
                        f"文档={detailed_statuses[rule_id]}，"
                        f"台账={all_ledger_statuses[rule_id]}"
                    )
    candidate_refs = {
        ref
        for item in [*hitcc.get("clues", []), *source.get("rules", [])]
        if isinstance(item, dict)
        for ref in item.get("spec_rule_ids", [])
        if str(ref).startswith("CAND-")
    }
    for candidate_ref in candidate_refs:
        if candidate_ref not in document:
            errors.append(f"规格文档缺少台账引用的候选：{candidate_ref}")
    compact_document = re.sub(r"\s+", "", document)
    if "当前正式已验证计数为**0**" not in compact_document:
        errors.append("规格文档没有明确声明当前正式已验证计数为 0")
    conflicting_claim_patterns = (
        r"所有\s*HitCC\s*线索.*?答案是：\*\*是\*\*",
        r"所有\s*源码.*?答案是：\*\*是\*\*",
        r"当前正式已验证计数为\s*\*\*[1-9]\d*\*\*",
    )
    for pattern in conflicting_claim_patterns:
        if re.search(pattern, document, flags=re.DOTALL):
            errors.append(f"规格文档出现与审计结论冲突的声明：{pattern}")
    for forbidden_denominator in (
        "42/42",
        "30/30",
        "26/26",
        "25/25",
        "22/22",
        "20/20",
    ):
        if forbidden_denominator in document:
            errors.append(
                f"规格文档残留未绑定分母：{forbidden_denominator}"
            )
    for required in (
        "覆盖 11、部分覆盖 63、缺失 4、范围外 10",
        (
            "正式运行证实 0、仅静态／样本相容 43、已变化 3、"
            "未证实 38、范围外 18"
        ),
        "53 篇直接线索源尚未逐条抽取",
        (
            "458 个“命中词法候选信号”与 1186 个“未人工排除”只是台账现有 "
            "`scan_signals` 字段的内部计数"
        ),
        "不能从 2.1.88 源码独立复算该词法分组",
        "达到当前正式准入的为 0 条",
        "依赖源码只完成确定性快照，**尚未做语义规则抽取**",
        "筛选条件不使用待证的 method、path、host、HTTP 版本、Header 或 Body 字段",
        "当前 verified 集合为空",
        "不是“CLI 无参数默认值”",
        "manifest 没有归档 argv",
        "响应兼容不计入 egress",
        "currentDate",
    ):
        if re.sub(r"\s+", "", required) not in compact_document:
            errors.append(f"规格文档缺少关键审计结论：{required}")


def validate_capture_snapshot(errors: list[str]) -> dict[str, int]:
    statuses: Counter[str] = Counter()
    for manifest in sorted(BASELINE_ROOT.glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"manifest 无法解析：{manifest.relative_to(ROOT)}：{exc}")
            continue
        statuses[str(data.get("status"))] += 1
    expected = {"complete": 18, "failed": 4}
    if dict(statuses) != expected:
        errors.append(f"专项 manifest 快照变化：实际 {dict(statuses)}，文档基线 {expected}")
    return dict(statuses)


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析原始 Body 时拒绝重复键，避免解析后对象掩盖 wire 差异。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 键：{key}")
        result[key] = value
    return result


def validate_default_body_snapshot(
    data: dict[str, Any], errors: list[str]
) -> int:
    """非循环枚举并复算 J 层原子观察绑定的六个基线请求。"""

    run_roots = (
        BASELINE_ROOT / "oauth-claude-newaccount-baseline",
        BASELINE_ROOT / "oauth-probe-baseline-r2",
    )
    for run_root in run_roots:
        manifest = load_json(run_root / "manifest.json", errors)
        if manifest.get("status") != "complete":
            errors.append(f"基线 Body 规则引用非 complete run：{run_root.name}")
        client = manifest.get("clients", {}).get("claude", {})
        if client.get("sha256") != EXPECTED_CLAUDE_21220_SHA256:
            errors.append(f"基线 Body 规则客户端摘要不匹配：{run_root.name}")
        if client.get("expected_sha256") != EXPECTED_CLAUDE_21220_SHA256:
            errors.append(f"基线 Body 规则预期摘要不匹配：{run_root.name}")
        runtime = manifest.get("runtime", {})
        if runtime.get("platform") != EXPECTED_PLATFORM:
            errors.append(f"基线 Body 规则平台不匹配：{run_root.name}")
        if runtime.get("clean_environment_keys") != ["HOME", "PATH"]:
            errors.append(f"基线 Body 规则不是 clean environment：{run_root.name}")
        auth = runtime.get("auth_preflight", {}).get("claude", {})
        if auth != EXPECTED_AUTH_PREFLIGHT:
            errors.append(f"基线 Body 规则 OAuth/provider 不匹配：{run_root.name}")
        if run_root.name == "oauth-claude-newaccount-baseline":
            if "injected_probe_env" in runtime:
                errors.append("newaccount 基线的 probe env 缺失边界发生变化")
        elif runtime.get("injected_probe_env") != {}:
            errors.append(f"基线 Body 规则 run 未明确记录空探针环境：{run_root.name}")

    expected_request_counts = {
        run_roots[0] / "mitm/claude-http/s1/claude-http.jsonl": 1,
        run_roots[0] / "mitm/claude-http/s2/claude-http.jsonl": 2,
        run_roots[0] / "mitm/claude-http/s4/claude-http.jsonl": 2,
        run_roots[1] / "mitm/claude-http/s1/claude-http.jsonl": 1,
    }
    catalog = data.get("evidence_catalog", {})
    declared_request_paths = {
        ROOT / str(relative)
        for catalog_id in ("BASELINE-J", "PROBE-BASELINE-J")
        for relative in (
            catalog.get(catalog_id, {}).get("paths", [])
            if isinstance(catalog, dict)
            and isinstance(catalog.get(catalog_id), dict)
            else []
        )
        if str(relative).endswith("/claude-http.jsonl")
    }
    if declared_request_paths != set(expected_request_counts):
        actual_paths = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(declared_request_paths)
        ]
        errors.append(
            "六请求 J 分母的 catalog 文件集合不匹配："
            f"实际 {actual_paths}"
        )
    expected_values = {
        "model": "claude-sonnet-5",
        "max_tokens": 64000,
        "thinking": {"type": "adaptive"},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "output_config": {"effort": "high"},
        "stream": True,
    }
    request_count = 0
    for path, expected_count in expected_request_counts.items():
        current_count = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(f"无法读取基线 Body J-raw：{path.relative_to(ROOT)}：{exc}")
            continue
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(
                    f"基线 Body J-raw JSON 无效：{path.relative_to(ROOT)}:{line_number}：{exc}"
                )
                continue
            request = record.get("request")
            if not (
                record.get("_boundary") == "official_cli_to_official_platform"
                and record.get("_category") == "claude"
                and record.get("_subject") == "claude-http"
                and isinstance(request, dict)
            ):
                continue
            current_count += 1
            request_count += 1
            if request.get("method") != "POST":
                errors.append(
                    f"SPEC-EP-003 method 漂移：{path.relative_to(ROOT)}:{line_number}"
                )
            if request.get("path") != "/v1/messages?beta=true":
                errors.append(
                    f"SPEC-EP-001 path 漂移：{path.relative_to(ROOT)}:{line_number}"
                )
            if request.get("http_version") != "HTTP/1.1":
                errors.append(
                    f"SPEC-PROTO-001 漂移：{path.relative_to(ROOT)}:{line_number}"
                )
            if request.get("host") != "api.anthropic.com":
                errors.append(
                    f"SPEC-EP-004 HTTP host 漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            if request.get("scheme") != "https" or request.get("port") != 443:
                errors.append(
                    f"SCOPE-J6 scheme/port 边界漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            headers = request.get("headers", [])
            normalized_headers: dict[str, Any] = {}
            normalized_header_names: list[str] = []
            if not isinstance(headers, list):
                errors.append(f"基线请求 headers 不是数组：{path.relative_to(ROOT)}:{line_number}")
                headers = []
            for header in headers:
                if not isinstance(header, list) or len(header) != 2:
                    errors.append(
                        f"基线请求 header 结构无效：{path.relative_to(ROOT)}:{line_number}"
                    )
                    continue
                name, value = header
                if not isinstance(name, str):
                    errors.append(
                        f"基线请求 header 名不是字符串：{path.relative_to(ROOT)}:{line_number}"
                    )
                    continue
                normalized_name = name.lower()
                normalized_header_names.append(normalized_name)
                normalized_headers[normalized_name] = value
            header_name_counts = Counter(normalized_header_names)
            if header_name_counts.get("user-agent") != 1:
                errors.append(
                    f"SPEC-HDR-002 User-Agent 数量漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            if header_name_counts.get("x-anthropic-billing-header", 0) != 0:
                errors.append(
                    f"SPEC-HDR-020 漂移：{path.relative_to(ROOT)}:{line_number}"
                )
            if normalized_headers.get("user-agent") != (
                "claude-cli/2.1.220 (external, sdk-cli)"
            ):
                errors.append(f"基线请求 UA/entrypoint 不匹配：{path.relative_to(ROOT)}:{line_number}")
            body_container = request.get("body", {})
            if body_container.get("decode_error") != "":
                errors.append(f"基线 Body 解码失败：{path.relative_to(ROOT)}:{line_number}")
            body = body_container.get("json")
            if not isinstance(body, dict):
                errors.append(
                    f"基线 Body J-raw 缺少解析对象：{path.relative_to(ROOT)}:{line_number}"
                )
                continue
            raw_body = body_container.get("text")
            if not isinstance(raw_body, str):
                errors.append(f"基线 Body 缺少原始文本：{path.relative_to(ROOT)}:{line_number}")
            else:
                try:
                    reparsed = json.loads(
                        raw_body, object_pairs_hook=reject_duplicate_json_keys
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(
                        f"基线 Body 原始 JSON 无效或含重复键："
                        f"{path.relative_to(ROOT)}:{line_number}：{exc}"
                    )
                else:
                    if reparsed != body:
                        errors.append(
                            f"基线 Body 原始文本与解析对象不一致："
                            f"{path.relative_to(ROOT)}:{line_number}"
                        )
            for field, expected in expected_values.items():
                actual = body.get(field)
                if type(actual) is not type(expected) or actual != expected:
                    errors.append(
                        f"基线 Body 字段漂移：{path.relative_to(ROOT)}:{line_number} "
                        f"{field}={actual!r}，预期 {expected!r}"
                    )
            system = body.get("system")
            first_system_block = (
                system[0]
                if isinstance(system, list)
                and system
                and isinstance(system[0], dict)
                else None
            )
            if not isinstance(first_system_block, dict) or first_system_block.get(
                "type"
            ) != "text":
                errors.append(
                    f"SPEC-BODY-003 system[0] 类型漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            attribution = (
                first_system_block.get("text")
                if isinstance(first_system_block, dict)
                else None
            )
            attribution_prefix = "x-anthropic-billing-header:"
            if not isinstance(attribution, str) or not attribution.startswith(
                attribution_prefix
            ):
                errors.append(
                    f"SPEC-BODY-007 attribution 前缀漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
                continue
            segments: dict[str, str] = {}
            duplicate_segments: set[str] = set()
            for segment in attribution[len(attribution_prefix):].split(";"):
                segment = segment.strip()
                if not segment or "=" not in segment:
                    continue
                key, value = segment.split("=", 1)
                if key in segments:
                    duplicate_segments.add(key)
                segments[key] = value
            if duplicate_segments:
                errors.append(
                    f"attribution 字段重复：{path.relative_to(ROOT)}:{line_number} "
                    f"{sorted(duplicate_segments)}"
                )
            if re.fullmatch(r"2\.1\.220\.[^;\s]+", segments.get("cc_version", "")) is None:
                errors.append(
                    f"SPEC-BODY-014 cc_version 漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            if segments.get("cc_entrypoint") != "sdk-cli":
                errors.append(
                    f"SPEC-BODY-015 cc_entrypoint 漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
            if re.fullmatch(r"[A-Za-z0-9]{5}", segments.get("cch", "")) is None:
                errors.append(
                    f"SPEC-BODY-016 cch 漂移："
                    f"{path.relative_to(ROOT)}:{line_number}"
                )
        if current_count != expected_count:
            errors.append(
                f"基线 Body 请求分母变化：{path.relative_to(ROOT)} "
                f"实际 {current_count}，预期 {expected_count}"
            )
    if request_count != 6:
        errors.append(f"基线 Body 规则应绑定 6 个请求，实际 {request_count}")
    return request_count


def main() -> int:
    errors: list[str] = []
    rules = load_json(RULES_PATH, errors)
    hitcc = load_json(HITCC_PATH, errors)
    source = load_json(SOURCE_PATH, errors)
    rule_counts = validate_rule_ledger(rules, errors) if rules else {}
    if rules:
        validate_selected_runtime_catalogs(rules, errors)
        validate_catalog_hashes_and_a1_identity(rules, errors)
        validate_selected_denominator_declarations(rules, errors)
        tls_client_hello_count = validate_tls002_denominator(rules, errors)
        sni_client_hello_count = validate_tls003_p_denominator(rules, errors)
    else:
        tls_client_hello_count = 0
        sni_client_hello_count = 0
    hitcc_counts = validate_hitcc(hitcc, errors) if hitcc else {}
    source_counts = (
        validate_source_2188(source, rules, errors) if source and rules else {}
    )
    validate_document(rules, hitcc, source, errors)
    capture_counts = validate_capture_snapshot(errors)
    default_body_count = validate_default_body_snapshot(rules, errors)
    if errors:
        for error in errors:
            print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "通过："
        f"历史规则={sum(rule_counts.values())}，"
        f"HitCC线索={sum(hitcc_counts.values())}，"
        f"2.1.88候选={sum(value for key, value in source_counts.items() if key != 'source_refs')}，"
        f"源码引用={source_counts.get('source_refs', 0)}，"
        f"基线Body请求={default_body_count}，"
        f"TLS ClientHello={tls_client_hello_count}，"
        f"TLS SNI ClientHello={sni_client_hello_count}，"
        f"manifest={sum(capture_counts.values())}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
