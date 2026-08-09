#!/usr/bin/env python3
"""按显式规则把证据根扫描成统一 capture manifest。

`capture-official seal` 与 `capture-candidate seal` 都要求证据根内存在一份
`capture-manifest.json`，但仓库里只有它的 schema 和消费方：official 侧一直靠每轮手写
一个一次性脚本（`gen_official_k<N>_manifest.py`），候选侧则根本没有产出方——候选 seal
因此必然卡住。

本工具把这件事正规化：路径到场景的映射由调用方以规则文件显式声明，工具只负责按规则
匹配文件、计算摘要、拼装 manifest。规则显式意味着"哪份字节归哪条场景"是可审计的输入，
而不是散落在一次性脚本里的硬编码。

规则文件形如：

    {
      "codex_version": "0.147.0",
      "capture_id": "official-<campaign>-core",
      "rules": [
        {
          "glob": "direct/*/s1/traffic.pcap",
          "kind": "pcap",
          "parser": "pcap_client_hello",
          "scenario_ids": ["A01"],
          "labels": {"side": "official", "transport": "direct"},
          "labels_from_path": {"surface": 1}
        }
      ]
    }

`labels_from_path` 把匹配路径的第 N 段作为标签值，用于 surface 这类随目录变化的维度。
每条规则至少要匹配到一个文件，否则报错——静默漏掉证据比缺少 manifest 更危险。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MANIFEST_SCHEMA = "codex-candidate-capture-manifest/v1"
ALLOWED_KINDS = {
    "application_log",
    "database_trace",
    "filesystem_snapshot",
    "http_trace",
    "mitm_jsonl",
    "pcap",
    "pcapng",
    "process_trace",
    "relay_binary",
    "stderr_log",
    "stdout_log",
    "tls_keylog",
    "websocket_trace",
    "wire_dump",
}
ALLOWED_PARSERS = {
    "h1_request_stream",
    "observation_json",
    "observation_jsonl",
    "opaque_bound_source",
    "pcap_client_hello",
}
SCENARIO_RE = re.compile(r"^A[0-9]{2}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class CaptureManifestError(RuntimeError):
    """规则或证据不足以生成可信 manifest。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_rule(rule: Mapping[str, Any], index: int) -> None:
    for field in ("glob", "kind", "parser", "scenario_ids"):
        if field not in rule:
            raise CaptureManifestError(f"rules[{index}] 缺少 {field}")
    if rule["kind"] not in ALLOWED_KINDS:
        raise CaptureManifestError(f"rules[{index}].kind 不在允许集合：{rule['kind']}")
    if rule["parser"] not in ALLOWED_PARSERS:
        raise CaptureManifestError(
            f"rules[{index}].parser 不在允许集合：{rule['parser']}"
        )
    scenario_ids = rule["scenario_ids"]
    if not isinstance(scenario_ids, list) or not scenario_ids:
        raise CaptureManifestError(f"rules[{index}].scenario_ids 必须是非空数组")
    for scenario in scenario_ids:
        if not isinstance(scenario, str) or not SCENARIO_RE.fullmatch(scenario):
            raise CaptureManifestError(
                f"rules[{index}] 的场景标识必须形如 A01：{scenario}"
            )
    labels = rule.get("labels", {})
    if not isinstance(labels, dict):
        raise CaptureManifestError(f"rules[{index}].labels 必须是对象")
    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise CaptureManifestError(f"rules[{index}].labels 只能是字符串键值")


def _labels_for(rule: Mapping[str, Any], relative: Path) -> dict[str, str]:
    labels = dict(rule.get("labels", {}))
    for label, position in (rule.get("labels_from_path") or {}).items():
        if not isinstance(position, int) or position < 0:
            raise CaptureManifestError(f"labels_from_path.{label} 必须是非负整数")
        parts = relative.parts
        if position >= len(parts):
            raise CaptureManifestError(
                f"labels_from_path.{label} 越界：{relative} 没有第 {position} 段"
            )
        labels[label] = parts[position]
    return labels


def build_manifest(
    *,
    evidence_root: Path,
    codex_version: str,
    capture_id: str,
    rules: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not VERSION_RE.fullmatch(codex_version):
        raise CaptureManifestError("codex_version 必须是三段数字")
    if not capture_id or capture_id != capture_id.strip():
        raise CaptureManifestError("capture_id 缺失或含首尾空白")
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        raise CaptureManifestError(f"证据根不存在或不可信：{evidence_root}")
    if not rules:
        raise CaptureManifestError("规则集不能为空")

    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        _validate_rule(rule, index)
        matched = sorted(
            path
            for path in evidence_root.glob(rule["glob"])
            if path.is_file() and not path.is_symlink()
        )
        if not matched:
            # 静默漏掉证据比缺少 manifest 更危险：断言会在一个不完整的集合上通过。
            raise CaptureManifestError(
                f"rules[{index}] 的 glob 未匹配到任何文件：{rule['glob']}"
            )
        for path in matched:
            relative = path.relative_to(evidence_root)
            key = relative.as_posix()
            if key in seen:
                raise CaptureManifestError(f"同一份证据被多条规则重复登记：{key}")
            seen.add(key)
            artifacts.append(
                {
                    "kind": rule["kind"],
                    "labels": _labels_for(rule, relative),
                    "parser": rule["parser"],
                    "path": key,
                    "scenario_ids": list(rule["scenario_ids"]),
                    "sha256": _file_sha256(path),
                }
            )

    artifacts.sort(key=lambda item: item["path"])
    return {
        "schema_version": MANIFEST_SCHEMA,
        "codex_version": codex_version,
        "capture_id": capture_id,
        "status": "complete",
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按显式规则把证据根扫描成统一 capture manifest"
    )
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True, help="路径到场景的映射规则")
    parser.add_argument("--output", type=Path, help="默认写入证据根下的 capture-manifest.json")
    arguments = parser.parse_args()

    try:
        document = json.loads(arguments.rules.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"无法读取规则文件：{error}") from error

    manifest = build_manifest(
        evidence_root=arguments.evidence_root,
        codex_version=str(document.get("codex_version", "")),
        capture_id=str(document.get("capture_id", "")),
        rules=document.get("rules") or [],
    )

    output = arguments.output or (arguments.evidence_root / "capture-manifest.json")
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output.chmod(0o600)
    print(
        json.dumps(
            {
                "output": str(output),
                "capture_id": manifest["capture_id"],
                "artifact_count": len(manifest["artifacts"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
