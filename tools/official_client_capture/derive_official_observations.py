#!/usr/bin/env python3
"""ACC-02b 官方侧观测派生器：把官方原始记录确定性投影为 wire 观测。

正式验收模型复核表明：25 条 ``dual_wire`` 规则中 21 条绑定的场景要求
``process_trace``／``websocket_trace`` kind，而这两种 kind 只接受
``observation_json``／``observation_jsonl``；官方侧没有任何正式产出器。本工具
把 **bundle 内已收口的官方原件**（relay 明文字节流、mitm HTTP jsonl）投影为
``codex-candidate-observation/v1`` 记录，只产三种 wire record
（``http_request``／``websocket_frame``／``tls_client_hello`` 中的前两种；TLS 由
pcap 解析器直接消费，无需派生），**禁止一切内部 record type**——官方侧永远
不伪造 Sub2API 内部状态。

同构与防双计数：

- 记录组装逐字段复用 ``candidate_rule_assertion._h1_observations`` 的同一套
  解析（``parse_h1_stream``／``parse_ws_frames``／``_parse_request_line``／
  ``_header_values``），派生记录与断言器直接解析原件的结果同构，画像判据
  无需任何改写；
- 因此**同一原始文件在 manifest 中必须二选一**：要么以
  ``h1_request_stream`` 直接解析，要么以 ``opaque_bound_source`` 登记并由本
  工具的派生文件绑定——否则同一请求会产出两份 record 导致计数类判据失真。
  该互斥由 seal 门禁（ACC-03）强制。

脱敏收敛：mitm jsonl 含解码后的明文 body，派生记录只保留与 relay
``summarize_body`` 同构的结构摘要（``top_level_fields_in_order``／``shape``），
header 值再过一遍 relay 侧 ``redact``，query 走白名单剥离；失败方向只会是
多脱敏。

确定性：同输入必然逐字节同输出；``--verify`` 重放派生并与收据逐字节比对，
来源未登记、来源漂移、产物漂移、未登记派生文件全部失败关闭。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.build_assertion_bundle import (  # noqa: E402
    AssertionBundleError,
    load_provenance,
    make_private_parents,
    validate_relative_path,
)
from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    OBSERVATION_SCHEMA_VERSION,
    _header_values,
    _parse_request_line,
)
from tools.official_client_capture.relay_extract import (  # noqa: E402
    parse_h1_stream,
    parse_ws_frames,
    redact,
    redact_query,
    shape_value,
)

DERIVED_PREFIX = "derived/"
DERIVED_PROVENANCE_RELATIVE_PATH = "derived/derived-provenance.json"
DERIVATION_PROVENANCE_SCHEMA = "codex-derived-observation-provenance/v1"

PARSER_H1 = "h1_request_stream"
PARSER_MITM = "mitm_http_jsonl"
PARSER_H1_PROBE = "h1_wire_probe"
ALLOWED_SOURCE_PARSERS = frozenset({PARSER_H1, PARSER_MITM, PARSER_H1_PROBE})
H1_PROBE_SCHEMA = "h1-wire-probe/v1"
ALLOWED_TARGET_KINDS = frozenset({"process_trace", "websocket_trace"})
ALLOWED_DERIVED_RECORD_TYPES = frozenset({"http_request", "websocket_frame"})


class ObservationDerivationError(RuntimeError):
    """官方观测派生失败，必须失败关闭。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_derivation_plan(path: Path) -> list[dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ObservationDerivationError(f"无法读取派生清单：{error}") from error
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ObservationDerivationError("派生清单 entries 必须是非空数组")
    seen_targets: set[str] = set()
    plan: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {
            "source",
            "parser",
            "scenario_id",
            "kind",
            "target",
            "connection_id",
        }:
            raise ObservationDerivationError(f"派生清单第 {index} 项字段不闭合")
        source = validate_relative_path(entry["source"], f"派生清单第 {index} 项 source")
        target = validate_relative_path(entry["target"], f"派生清单第 {index} 项 target")
        if not target.startswith(DERIVED_PREFIX):
            raise ObservationDerivationError(
                f"派生产物必须位于 {DERIVED_PREFIX} 下：{target}"
            )
        if target == DERIVED_PROVENANCE_RELATIVE_PATH:
            raise ObservationDerivationError("派生产物不得占用派生收据路径")
        if target in seen_targets:
            raise ObservationDerivationError(f"派生目标重复：{target}")
        parser = entry["parser"]
        if parser not in ALLOWED_SOURCE_PARSERS:
            raise ObservationDerivationError(
                f"派生只接受官方原始形态 {sorted(ALLOWED_SOURCE_PARSERS)}：{parser}"
            )
        kind = entry["kind"]
        if kind not in ALLOWED_TARGET_KINDS:
            raise ObservationDerivationError(
                f"派生产物 kind 只能是 {sorted(ALLOWED_TARGET_KINDS)}：{kind}"
            )
        scenario_id = entry["scenario_id"]
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ObservationDerivationError("scenario_id 不能为空")
        connection_id = entry["connection_id"]
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ObservationDerivationError("connection_id 不能为空")
        seen_targets.add(target)
        plan.append(
            {
                "source": source,
                "parser": parser,
                "scenario_id": scenario_id,
                "kind": kind,
                "target": target,
                "connection_id": connection_id,
            }
        )
    return plan


def _wire_protocol(header_names: Iterable[Any]) -> str:
    return (
        "websocket"
        if any(
            isinstance(name, str) and name.lower() == "upgrade"
            for name in header_names
        )
        else "http"
    )


def _observation_record(
    *,
    target: str,
    scenario_id: str,
    record_type: str,
    record_index: int,
    index_kind: str,
    data: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    if record_type not in ALLOWED_DERIVED_RECORD_TYPES:
        raise ObservationDerivationError(
            f"派生记录禁止 record type：{record_type}"
        )
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "record_id": f"{target}#{index_kind}-{record_index}",
        "scenario_id": scenario_id,
        "record_type": record_type,
        "data": dict(data),
        "source_artifacts": [source],
    }


def _derive_from_h1(
    payload: bytes,
    entry: Mapping[str, str],
) -> list[dict[str, Any]]:
    try:
        requests = parse_h1_stream(payload)
    except SystemExit as error:
        raise ObservationDerivationError(
            f"H1 原始字节解析失败：{entry['source']}: {error}"
        ) from error
    if not requests:
        raise ObservationDerivationError(
            f"H1 原始字节未解析出任何请求：{entry['source']}"
        )
    records: list[dict[str, Any]] = []
    for request_index, request in enumerate(requests):
        line = _parse_request_line(request.get("request_line"))
        header_names = request.get("header_names_in_order", [])
        data = {
            **line,
            "connection_id": entry["connection_id"],
            "request_index": request_index,
            "wire_protocol": _wire_protocol(header_names),
            "header_names_in_order": header_names,
            "remaining_header_names": header_names[5:],
            "header_values": _header_values(request),
            "body": request.get("body"),
        }
        records.append(
            _observation_record(
                target=entry["target"],
                scenario_id=entry["scenario_id"],
                record_type="http_request",
                record_index=request_index,
                index_kind="request",
                data=data,
                source=entry["source"],
            )
        )
    first_header_names = requests[0].get("header_names_in_order", [])
    if _wire_protocol(first_header_names) == "websocket":
        header_end = payload.find(b"\r\n\r\n")
        frames = parse_ws_frames(payload[header_end + 4 :]) if header_end >= 0 else []
        for frame_index, frame in enumerate(frames):
            data = {
                **frame,
                "connection_id": entry["connection_id"],
                "frame_index": frame_index,
            }
            records.append(
                _observation_record(
                    target=entry["target"],
                    scenario_id=entry["scenario_id"],
                    record_type="websocket_frame",
                    record_index=frame_index,
                    index_kind="frame",
                    data=data,
                    source=entry["source"],
                )
            )
    return records


def _derive_from_h1_probe(
    payload: bytes,
    entry: Mapping[str, str],
) -> list[dict[str, Any]]:
    """把 h1 探针的结构化输出投影为与 relay 解析同构的 http_request 记录。

    探针已按 wire 顺序记录 request_line 与 headers，字段语义与
    ``parse_h1_stream`` 的输出一致；这里只做同构映射，不做任何推断。
    """

    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except ValueError as error:
        raise ObservationDerivationError(
            f"h1 探针记录不是 JSON：{entry['source']}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != H1_PROBE_SCHEMA
    ):
        raise ObservationDerivationError(
            f"h1 探针记录 schema_version 必须是 {H1_PROBE_SCHEMA}：{entry['source']}"
        )
    requests = document.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ObservationDerivationError(
            f"h1 探针记录 requests 为空：{entry['source']}"
        )
    records: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            raise ObservationDerivationError(
                f"h1 探针第 {index + 1} 条记录必须是对象：{entry['source']}"
            )
        line = _parse_request_line(request.get("request_line"))
        headers = request.get("headers")
        if not isinstance(headers, list):
            raise ObservationDerivationError(
                f"h1 探针第 {index + 1} 条缺少 headers：{entry['source']}"
            )
        normalized = [
            {
                "name": item.get("name"),
                "value": redact(str(item.get("name")), str(item.get("value"))),
            }
            for item in headers
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        header_names = [item["name"] for item in normalized]
        data = {
            **line,
            "connection_id": entry["connection_id"],
            "request_index": index,
            "wire_protocol": _wire_protocol(header_names),
            "header_names_in_order": header_names,
            "remaining_header_names": header_names[5:],
            "header_values": _header_values({"headers": normalized}),
            "body": None,
        }
        records.append(
            _observation_record(
                target=entry["target"],
                scenario_id=entry["scenario_id"],
                record_type="http_request",
                record_index=index,
                index_kind="request",
                data=data,
                source=entry["source"],
            )
        )
    return records


def _mitm_body_summary(body: Any) -> dict[str, Any] | None:
    """把 mitm 明文 body 收敛为 relay ``summarize_body`` 同构的结构摘要。"""

    if not isinstance(body, Mapping):
        return None
    parsed = body.get("json")
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        return {"top_level_type": type(parsed).__name__}
    return {
        "top_level_fields_in_order": list(parsed.keys()),
        "shape": {key: shape_value(value) for key, value in parsed.items()},
    }


def _derive_from_mitm(
    payload: bytes,
    entry: Mapping[str, str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    # JSON Lines 以 \n 分隔记录。str.splitlines() 还会在 \x85、\u2028 等
    # Unicode 行分隔符处切分，而这些字符可以合法出现在 JSON 字符串内部——真实
    # mitm 记录就含 \x85，用 splitlines() 会把一条记录截成两半并报「不是 JSON」。
    lines = [
        line
        for line in payload.decode("utf-8", errors="strict").split("\n")
        if line.strip()
    ]
    if not lines:
        raise ObservationDerivationError(
            f"mitm 记录为空：{entry['source']}"
        )
    for flow_index, line in enumerate(lines):
        try:
            flow = json.loads(line)
        except ValueError as error:
            raise ObservationDerivationError(
                f"mitm 第 {flow_index + 1} 行不是 JSON：{entry['source']}"
            ) from error
        if not isinstance(flow, dict):
            raise ObservationDerivationError(
                f"mitm 第 {flow_index + 1} 行必须是对象：{entry['source']}"
            )
        if flow.get("schema_version") == OBSERVATION_SCHEMA_VERSION:
            raise ObservationDerivationError(
                "禁止以派生观测再派生：" + entry["source"]
            )
        request = flow.get("request")
        if not isinstance(request, dict):
            raise ObservationDerivationError(
                f"mitm 第 {flow_index + 1} 行缺少 request：{entry['source']}"
            )
        method = request.get("method")
        raw_path = request.get("path")
        http_version = request.get("http_version")
        headers = request.get("headers")
        if (
            not isinstance(method, str)
            or not isinstance(raw_path, str)
            or not isinstance(http_version, str)
            or not isinstance(headers, list)
        ):
            raise ObservationDerivationError(
                f"mitm 第 {flow_index + 1} 行 request 字段非法：{entry['source']}"
            )
        target_value = redact_query(raw_path) if "?" in raw_path else raw_path
        line_fields = _parse_request_line(
            f"{method} {target_value} {http_version}"
        )
        pairs: list[tuple[str, Any]] = []
        for item in headers:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
            ):
                raise ObservationDerivationError(
                    f"mitm 第 {flow_index + 1} 行 header 形态非法：{entry['source']}"
                )
            pairs.append((item[0], item[1]))
        header_dicts = [
            {"name": name, "value": redact(name, str(value))}
            for name, value in pairs
        ]
        header_names = [name for name, _ in pairs]
        data = {
            **line_fields,
            "connection_id": entry["connection_id"],
            "request_index": flow_index,
            "wire_protocol": _wire_protocol(header_names),
            "header_names_in_order": header_names,
            "remaining_header_names": header_names[5:],
            "header_values": _header_values({"headers": header_dicts}),
            "body": _mitm_body_summary(request.get("body")),
        }
        records.append(
            _observation_record(
                target=entry["target"],
                scenario_id=entry["scenario_id"],
                record_type="http_request",
                record_index=flow_index,
                index_kind="request",
                data=data,
                source=entry["source"],
            )
        )
    return records


def _render_records(records: list[dict[str, Any]]) -> bytes:
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _derive_entry(
    bundle_dir: Path,
    entry: Mapping[str, str],
    registered_targets: Mapping[str, str],
) -> tuple[bytes, dict[str, Any]]:
    source = entry["source"]
    if source not in registered_targets:
        raise ObservationDerivationError(
            f"派生来源未在 bundle provenance 登记：{source}"
        )
    source_path = bundle_dir / source
    if source_path.is_symlink() or not source_path.is_file():
        raise ObservationDerivationError(f"派生来源不可信：{source}")
    payload = source_path.read_bytes()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    if source_sha256 != registered_targets[source]:
        raise ObservationDerivationError(
            f"派生来源相对收口时发生漂移：{source}"
        )
    if entry["parser"] == PARSER_H1:
        records = _derive_from_h1(payload, entry)
    elif entry["parser"] == PARSER_H1_PROBE:
        records = _derive_from_h1_probe(payload, entry)
    else:
        records = _derive_from_mitm(payload, entry)
    if not records:
        raise ObservationDerivationError(f"派生结果为空：{source}")
    rendered = _render_records(records)
    record_types = sorted({record["record_type"] for record in records})
    receipt_entry = {
        "source": source,
        "source_sha256": source_sha256,
        "parser": entry["parser"],
        "scenario_id": entry["scenario_id"],
        "kind": entry["kind"],
        "connection_id": entry["connection_id"],
        "target": entry["target"],
        "target_sha256": hashlib.sha256(rendered).hexdigest(),
        "record_count": len(records),
        "record_types": record_types,
    }
    return rendered, receipt_entry


def _registered_bundle_targets(bundle_dir: Path) -> dict[str, str]:
    provenance = load_provenance(bundle_dir)
    return {
        entry["target_path"]: entry["target_sha256"]
        for entry in provenance["entries"]
    }


def derive_observations(
    bundle_dir: Path,
    plan: list[dict[str, str]],
) -> dict[str, Any]:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ObservationDerivationError(f"bundle 目录不可信：{bundle_dir}")
    derived_dir = bundle_dir / DERIVED_PREFIX.rstrip("/")
    if derived_dir.exists():
        raise ObservationDerivationError(
            f"派生目录必须全新创建：{derived_dir}"
        )
    registered = _registered_bundle_targets(bundle_dir)
    receipt_entries: list[dict[str, Any]] = []
    outputs: list[tuple[Path, bytes]] = []
    for entry in plan:
        rendered, receipt_entry = _derive_entry(bundle_dir, entry, registered)
        outputs.append((bundle_dir / entry["target"], rendered))
        receipt_entries.append(receipt_entry)
    receipt_entries.sort(key=lambda item: item["target"])
    receipt = {
        "schema_version": DERIVATION_PROVENANCE_SCHEMA,
        "entry_count": len(receipt_entries),
        "entries": receipt_entries,
    }
    receipt["provenance_sha256"] = _canonical_sha256(
        {
            "entries": receipt_entries,
            "schema_version": DERIVATION_PROVENANCE_SCHEMA,
        }
    )
    for path, rendered in outputs:
        # 与 bundle 同在 attempt 证据根内，必须满足 0700／0600 权限门禁。
        make_private_parents(bundle_dir, path.relative_to(bundle_dir).as_posix())
        if path.exists() or path.is_symlink():
            raise ObservationDerivationError(f"派生目标已存在：{path}")
        path.write_bytes(rendered)
        path.chmod(0o400)
    receipt_path = bundle_dir / DERIVED_PROVENANCE_RELATIVE_PATH
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o400)
    return receipt


def load_derivation_receipt(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / DERIVED_PROVENANCE_RELATIVE_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ObservationDerivationError(
            f"无法读取派生收据：{error}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != DERIVATION_PROVENANCE_SCHEMA
        or not isinstance(document.get("entries"), list)
        or not document["entries"]
        or document.get("entry_count") != len(document["entries"])
    ):
        raise ObservationDerivationError("派生收据结构非法")
    expected = _canonical_sha256(
        {
            "entries": document["entries"],
            "schema_version": DERIVATION_PROVENANCE_SCHEMA,
        }
    )
    if document.get("provenance_sha256") != expected:
        raise ObservationDerivationError("派生收据摘要不符")
    return document


def verify_derivation(bundle_dir: Path) -> dict[str, Any]:
    """重放派生：同输入必须重现逐字节相同产物，任何漂移失败关闭。"""

    receipt = load_derivation_receipt(bundle_dir)
    registered = _registered_bundle_targets(bundle_dir)
    expected_files = {DERIVED_PROVENANCE_RELATIVE_PATH}
    for entry in receipt["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "source",
            "source_sha256",
            "parser",
            "scenario_id",
            "kind",
            "connection_id",
            "target",
            "target_sha256",
            "record_count",
            "record_types",
        }:
            raise ObservationDerivationError("派生收据条目字段不闭合")
        plan_entry = {
            "source": entry["source"],
            "parser": entry["parser"],
            "scenario_id": entry["scenario_id"],
            "kind": entry["kind"],
            "target": entry["target"],
            "connection_id": entry["connection_id"],
        }
        if entry["parser"] not in ALLOWED_SOURCE_PARSERS:
            raise ObservationDerivationError(
                f"派生收据 parser 非法：{entry['parser']}"
            )
        if entry["kind"] not in ALLOWED_TARGET_KINDS:
            raise ObservationDerivationError(
                f"派生收据 kind 非法：{entry['kind']}"
            )
        rendered, replayed = _derive_entry(bundle_dir, plan_entry, registered)
        if replayed["source_sha256"] != entry["source_sha256"]:
            raise ObservationDerivationError(
                f"派生来源相对收据发生漂移：{entry['source']}"
            )
        if (
            replayed["target_sha256"] != entry["target_sha256"]
            or replayed["record_count"] != entry["record_count"]
            or replayed["record_types"] != entry["record_types"]
        ):
            raise ObservationDerivationError(
                f"派生重放与收据不一致：{entry['target']}"
            )
        target_path = bundle_dir / entry["target"]
        if target_path.is_symlink() or not target_path.is_file():
            raise ObservationDerivationError(
                f"派生产物不可信：{entry['target']}"
            )
        if hashlib.sha256(target_path.read_bytes()).hexdigest() != entry[
            "target_sha256"
        ]:
            raise ObservationDerivationError(
                f"派生产物相对收据发生漂移：{entry['target']}"
            )
        expected_files.add(entry["target"])
    derived_dir = bundle_dir / DERIVED_PREFIX.rstrip("/")
    for path in sorted(derived_dir.rglob("*")):
        if path.is_symlink():
            raise ObservationDerivationError(f"派生目录禁止符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_dir).as_posix()
        if relative not in expected_files:
            raise ObservationDerivationError(
                f"派生目录存在未登记文件：{relative}"
            )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="派生或重放官方侧 wire 观测（ACC-02b）"
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, help="派生清单 JSON")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="重放派生收据而不是执行派生",
    )
    arguments = parser.parse_args()
    try:
        if arguments.verify:
            receipt = verify_derivation(arguments.bundle_dir)
            print(
                f"官方观测派生重放通过：{receipt['entry_count']} 项，"
                f"摘要={receipt['provenance_sha256']}"
            )
        else:
            if arguments.plan is None:
                raise ObservationDerivationError("派生模式必须提供 --plan")
            plan = load_derivation_plan(arguments.plan)
            receipt = derive_observations(arguments.bundle_dir, plan)
            print(
                f"官方观测派生完成：{receipt['entry_count']} 项，"
                f"摘要={receipt['provenance_sha256']}"
            )
    except (ObservationDerivationError, AssertionBundleError) as error:
        print(f"官方观测派生失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
