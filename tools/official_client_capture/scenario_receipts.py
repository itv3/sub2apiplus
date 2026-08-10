"""SCN-REALITY-01 场景成功收据的构建与严格校验。

收据只表达成功态：schema 在结构上只允许成功值，场景未成立时不产出收据，
判定看的是「声明的收据是否齐备且合法」，而不是收据里的某个布尔值。这样伪造
一次成功需要伪造整条 producer 链，而不是改一个字段值。

三类文档：

- 原始事实（`codex-egress-scenario-facts/v1`）由采集侧 `build_scenario_facts.py`
  在 job 证据根内产出，只含协议事实与证据绑定，不含 attempt 身份；
- 成功收据（`codex-egress-scenario-receipt/v1`）由外层 finalizer 承接原始事实并
  注入 attempt 身份三元后产出；
- 失败诊断（`codex-egress-scenario-failure/v1`）不进收据体系、不参与判定。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tools.official_client_capture.capturelib.security import secure_write_json


SCHEMA_VERSION = "codex-egress-scenario-receipt/v1"
FACTS_SCHEMA_VERSION = "codex-egress-scenario-facts/v1"
FAILURE_SCHEMA_VERSION = "codex-egress-scenario-failure/v1"
# 沿用既有收据体系的 producer 契约，不另造一份；重放器按同一套规则复算。
PRODUCER_SCHEMA = "codex-egress-receipt-producer/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCENARIO_RE = re.compile(r"^A[0-9]{2}$")
REGIONAL_SNI_RE = re.compile(r"^[a-z0-9.-]+\.oaiusercontent\.com$")
FINAL_STATES = {
    "A11": "sideband_established",
    "A13": "token_refreshed",
    "A14": "upload_chain_complete",
}
# R0 §4.3 只为三个已证实失效的目标场景定义收据，其余场景不引入真实性收据。
SUPPORTED_SCENARIOS = tuple(sorted(FINAL_STATES))


class ScenarioReceiptError(ValueError):
    """原始事实不能形成可信的场景成功收据。"""


def _expect_exact(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ScenarioReceiptError(f"{label} 字段不闭合。")


def _nonempty(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ScenarioReceiptError(f"{label} 必须是非空字符串。")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ScenarioReceiptError(f"{label} 必须是小写 SHA-256。")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ScenarioReceiptError(f"{label} 身份非法。")
    return value


def _rfc3339(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioReceiptError(f"{label} 不是 RFC 3339 时间。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScenarioReceiptError(f"{label} 不是 RFC 3339 时间。") from error
    if parsed.tzinfo is None:
        raise ScenarioReceiptError(f"{label} 必须带时区。")
    return value


def _facts_a11(facts: Any) -> dict[str, Any]:
    expected = {
        "call_create_status",
        "call_id_sha256",
        "sdp_or_started_event",
        "async_error_count",
        "sideband_sni",
        "sideband_call_id_linked",
    }
    _expect_exact(facts, expected, "A11 facts")
    if (
        not isinstance(facts["call_create_status"], int)
        or isinstance(facts["call_create_status"], bool)
        or not 200 <= facts["call_create_status"] <= 299
    ):
        raise ScenarioReceiptError("A11 call_create_status 必须是 2xx。")
    _sha256(facts["call_id_sha256"], "A11 call_id_sha256")
    if facts["sdp_or_started_event"] not in {
        "sdp_answer",
        "thread_realtime_started",
    }:
        raise ScenarioReceiptError("A11 缺少成功的 started/SDP 事件。")
    if facts["async_error_count"] != 0 or isinstance(facts["async_error_count"], bool):
        raise ScenarioReceiptError("A11 不允许存在异步 error。")
    if facts["sideband_sni"] != "api.openai.com":
        raise ScenarioReceiptError("A11 sideband SNI 不匹配。")
    if facts["sideband_call_id_linked"] is not True:
        raise ScenarioReceiptError("A11 sideband 未绑定 call_id。")
    return dict(facts)


def _facts_a13(facts: Any) -> dict[str, Any]:
    expected = {
        "token_request_method",
        "token_request_path",
        "oauth_sni",
        "jwt_exp_observation",
        "credential_restore",
    }
    _expect_exact(facts, expected, "A13 facts")
    if facts["token_request_method"] != "POST":
        raise ScenarioReceiptError("A13 token 请求必须是 POST。")
    if facts["token_request_path"] != "/oauth/token":
        raise ScenarioReceiptError("A13 token 请求路径不匹配。")
    if facts["oauth_sni"] != "auth.openai.com":
        raise ScenarioReceiptError("A13 OAuth SNI 不匹配。")
    observation = facts["jwt_exp_observation"]
    _expect_exact(
        observation,
        {"exp_at_utc", "observed_at_utc", "within_refresh_window", "token_sha256"},
        "A13 jwt_exp_observation",
    )
    _rfc3339(observation["exp_at_utc"], "A13 exp_at_utc")
    _rfc3339(observation["observed_at_utc"], "A13 observed_at_utc")
    if observation["within_refresh_window"] is not True:
        raise ScenarioReceiptError("A13 JWT 不在自然刷新窗口。")
    _sha256(observation["token_sha256"], "A13 token_sha256")
    restore = facts["credential_restore"]
    _expect_exact(
        restore,
        {"before_sha256", "after_sha256", "restored"},
        "A13 credential_restore",
    )
    _sha256(restore["before_sha256"], "A13 credential_restore.before_sha256")
    _sha256(restore["after_sha256"], "A13 credential_restore.after_sha256")
    if restore["restored"] is not True or restore["before_sha256"] != restore["after_sha256"]:
        raise ScenarioReceiptError("A13 auth.json 未逐字恢复。")
    return dict(facts)


def _facts_a14(facts: Any) -> dict[str, Any]:
    expected = {
        "tool_name",
        "tool_call_id",
        "create_request",
        "upload_url_source_event",
        "put_destination",
        "uploaded_event",
        "regional_sni",
        "regional_host_from_response",
        "upload_sequence",
    }
    _expect_exact(facts, expected, "A14 facts")
    _nonempty(facts["tool_name"], "A14 tool_name")
    _nonempty(facts["tool_call_id"], "A14 tool_call_id")
    create = facts["create_request"]
    _expect_exact(create, {"method", "path", "status_2xx"}, "A14 create_request")
    if create != {"method": "POST", "path": "/backend-api/files", "status_2xx": True}:
        raise ScenarioReceiptError("A14 file create 不匹配。")
    source = facts["upload_url_source_event"]
    _expect_exact(source, {"event", "host", "url_sha256"}, "A14 upload_url_source_event")
    _nonempty(source["event"], "A14 upload_url_source_event.event")
    _nonempty(source["host"], "A14 upload_url_source_event.host")
    _sha256(source["url_sha256"], "A14 upload_url_source_event.url_sha256")
    put = facts["put_destination"]
    _expect_exact(
        put,
        {"host", "sni", "first_seen_at_utc", "last_seen_at_utc"},
        "A14 put_destination",
    )
    _nonempty(put["host"], "A14 put_destination.host")
    if not isinstance(put["sni"], str) or not REGIONAL_SNI_RE.fullmatch(put["sni"]):
        raise ScenarioReceiptError("A14 put_destination.sni 不匹配。")
    _rfc3339(put["first_seen_at_utc"], "A14 first_seen_at_utc")
    _rfc3339(put["last_seen_at_utc"], "A14 last_seen_at_utc")
    if not isinstance(facts["regional_sni"], str) or not REGIONAL_SNI_RE.fullmatch(
        facts["regional_sni"]
    ):
        raise ScenarioReceiptError("A14 regional_sni 不匹配。")
    # 规格要求区域上传主机由响应派生，预列域名凑出的 SNI 无法满足这一条。
    if put["host"] != source["host"] or put["sni"] != facts["regional_sni"]:
        raise ScenarioReceiptError("A14 响应 host 与区域 SNI 不一致。")
    if facts["regional_host_from_response"] is not True:
        raise ScenarioReceiptError("A14 区域主机不是由响应派生。")
    uploaded = facts["uploaded_event"]
    _expect_exact(uploaded, {"method", "path_suffix", "status_2xx"}, "A14 uploaded_event")
    if uploaded != {"method": "POST", "path_suffix": "/uploaded", "status_2xx": True}:
        raise ScenarioReceiptError("A14 uploaded 事件不匹配。")
    sequence = facts["upload_sequence"]
    _expect_exact(
        sequence,
        {"create_before_regional", "regional_before_uploaded"},
        "A14 upload_sequence",
    )
    if (
        sequence["create_before_regional"] is not True
        or sequence["regional_before_uploaded"] is not True
    ):
        raise ScenarioReceiptError("A14 上传事件顺序不完整。")
    return dict(facts)


def validate_facts(scenario_id: str, facts: Any) -> dict[str, Any]:
    """验证场景原始事实，只允许形成成功态。"""

    if not isinstance(scenario_id, str) or not SCENARIO_RE.fullmatch(scenario_id):
        raise ScenarioReceiptError("scenario_id 非法。")
    if scenario_id == "A11":
        return _facts_a11(facts)
    if scenario_id == "A13":
        return _facts_a13(facts)
    if scenario_id == "A14":
        return _facts_a14(facts)
    raise ScenarioReceiptError(f"R0 未登记场景：{scenario_id}")


def _validate_evidence_bindings(
    bindings: Any,
    approved_roots: Mapping[str, Path] | None,
) -> list[dict[str, Any]]:
    if not isinstance(bindings, list) or not bindings:
        raise ScenarioReceiptError("evidence_bindings 不能为空。")
    result: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        _expect_exact(
            binding,
            {"root_role", "path", "sha256", "bytes"},
            f"evidence_bindings[{index}]",
        )
        role = _nonempty(binding["root_role"], f"evidence_bindings[{index}].root_role")
        relative = binding["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or str(PurePosixPath(relative)) != relative
            or PurePosixPath(relative).is_absolute()
            or "." in PurePosixPath(relative).parts
            or ".." in PurePosixPath(relative).parts
            or "\\" in relative
        ):
            raise ScenarioReceiptError(f"evidence_bindings[{index}].path 非法。")
        digest = _sha256(binding["sha256"], f"evidence_bindings[{index}].sha256")
        size = binding["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ScenarioReceiptError(f"evidence_bindings[{index}].bytes 非法。")
        if approved_roots is not None:
            root = approved_roots.get(role)
            if root is None or root.is_symlink() or not root.is_dir():
                raise ScenarioReceiptError(f"evidence root {role} 未获批准。")
            path = root / relative
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(root.resolve())
            ):
                raise ScenarioReceiptError(f"evidence_bindings[{index}] 越过批准证据根。")
            if path.stat().st_size != size or file_sha256(path) != digest:
                raise ScenarioReceiptError(f"evidence_bindings[{index}] 字节绑定不一致。")
        result.append(dict(binding))
    if len({(item["root_role"], item["path"]) for item in result}) != len(result):
        raise ScenarioReceiptError("evidence_bindings 存在重复路径。")
    return result


def file_sha256(path: Path) -> str:
    """计算证据文件的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_facts_document(
    payload: Any,
    *,
    scenario_id: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
    approved_roots: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """校验采集侧产出的原始事实文件；它不含 attempt 身份，由外层 finalizer 注入。"""

    expected_top = {
        "schema_version",
        "scenario_id",
        "job_id",
        "run_id",
        "final_state",
        "observed_at_utc",
        "evidence_bindings",
        "facts",
    }
    _expect_exact(payload, expected_top, "scenario_facts")
    if payload["schema_version"] != FACTS_SCHEMA_VERSION:
        raise ScenarioReceiptError("scenario_facts schema_version 不匹配。")
    observed_scenario = payload["scenario_id"]
    if not isinstance(observed_scenario, str) or not SCENARIO_RE.fullmatch(
        observed_scenario
    ):
        raise ScenarioReceiptError("scenario_facts scenario_id 非法。")
    if scenario_id is not None and observed_scenario != scenario_id:
        raise ScenarioReceiptError("scenario_facts 场景身份不匹配。")
    _safe_id(payload["job_id"], "scenario_facts job_id")
    if job_id is not None and payload["job_id"] != job_id:
        raise ScenarioReceiptError("scenario_facts job 身份不匹配。")
    _safe_id(payload["run_id"], "scenario_facts run_id")
    if run_id is not None and payload["run_id"] != run_id:
        raise ScenarioReceiptError("scenario_facts run 身份不匹配。")
    if payload["final_state"] != FINAL_STATES.get(observed_scenario):
        raise ScenarioReceiptError("scenario_facts final_state 不匹配。")
    _rfc3339(payload["observed_at_utc"], "scenario_facts observed_at_utc")
    facts = validate_facts(observed_scenario, payload["facts"])
    bindings = _validate_evidence_bindings(payload["evidence_bindings"], approved_roots)
    return {**payload, "facts": facts, "evidence_bindings": bindings}


def validate_receipt(
    payload: Any,
    *,
    scenario_id: str,
    job_id: str,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    run_id: str,
    approved_roots: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """执行不依赖 jsonschema 的运行时等价校验。"""

    expected_top = {
        "schema_version",
        "scenario_id",
        "job_id",
        "campaign_id",
        "attempt_id",
        "run_nonce",
        "run_id",
        "status",
        "final_state",
        "observed_at_utc",
        "evidence_bindings",
        "facts",
        "producer",
    }
    _expect_exact(payload, expected_top, "scenario_receipt")
    if payload["schema_version"] != SCHEMA_VERSION or payload["status"] != "success":
        raise ScenarioReceiptError("scenario_receipt 只允许成功态。")
    if payload["scenario_id"] != scenario_id or payload["job_id"] != job_id:
        raise ScenarioReceiptError("scenario_receipt 场景或 job 身份不匹配。")
    if payload["campaign_id"] != campaign_id or payload["attempt_id"] != attempt_id:
        raise ScenarioReceiptError("scenario_receipt attempt 身份不匹配。")
    if payload["run_nonce"] != run_nonce or payload["run_id"] != run_id:
        raise ScenarioReceiptError("scenario_receipt run 身份不匹配。")
    _safe_id(payload["scenario_id"], "scenario_id")
    _safe_id(payload["job_id"], "job_id")
    _safe_id(payload["campaign_id"], "campaign_id")
    _safe_id(payload["attempt_id"], "attempt_id")
    _sha256(payload["run_nonce"], "run_nonce")
    _safe_id(payload["run_id"], "run_id")
    _rfc3339(payload["observed_at_utc"], "observed_at_utc")
    if payload["final_state"] != FINAL_STATES.get(scenario_id):
        raise ScenarioReceiptError("scenario_receipt final_state 不匹配。")
    facts = validate_facts(scenario_id, payload["facts"])
    _validate_evidence_bindings(payload["evidence_bindings"], approved_roots)
    producer = payload["producer"]
    _expect_exact(
        producer,
        {
            "schema_version",
            "tool",
            "subcommand",
            "canonical_arguments",
            "input_bindings",
            "command_sha256",
        },
        "producer",
    )
    if producer["schema_version"] != PRODUCER_SCHEMA or producer["subcommand"] != "scenario":
        raise ScenarioReceiptError("producer schema 或 subcommand 不匹配。")
    tool = producer["tool"]
    _expect_exact(tool, {"path", "sha256"}, "producer.tool")
    _nonempty(tool["path"], "producer.tool.path")
    _sha256(tool["sha256"], "producer.tool.sha256")
    if not isinstance(producer["canonical_arguments"], dict):
        raise ScenarioReceiptError("producer.canonical_arguments 必须是对象。")
    inputs = producer["input_bindings"]
    if not isinstance(inputs, list) or not inputs:
        raise ScenarioReceiptError("producer.input_bindings 不能为空。")
    for index, item in enumerate(inputs):
        _expect_exact(
            item, {"name", "path", "sha256", "bytes"}, f"producer.input_bindings[{index}]"
        )
        _nonempty(item["name"], f"producer.input_bindings[{index}].name")
        _nonempty(item["path"], f"producer.input_bindings[{index}].path")
        _sha256(item["sha256"], f"producer.input_bindings[{index}].sha256")
        if (
            not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] <= 0
        ):
            raise ScenarioReceiptError(f"producer.input_bindings[{index}].bytes 非法。")
    core = {key: value for key, value in producer.items() if key != "command_sha256"}
    if fingerprint(core) != producer["command_sha256"]:
        raise ScenarioReceiptError("producer.command_sha256 校验失败。")
    return {**payload, "facts": facts}


def fingerprint(payload: Any) -> str:
    """与既有收据体系一致的紧凑 JSON 摘要。"""

    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def build_facts_document(
    *,
    scenario_id: str,
    job_id: str,
    run_id: str,
    facts: Mapping[str, Any],
    evidence_bindings: list[dict[str, Any]],
    observed_at_utc: str,
    approved_roots: Mapping[str, Path] | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    """产出采集侧原始事实文件；任一必填字段缺失即抛错且不落盘。"""

    if scenario_id not in FINAL_STATES:
        raise ScenarioReceiptError(f"R0 未登记场景：{scenario_id}")
    payload = {
        "schema_version": FACTS_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "job_id": job_id,
        "run_id": run_id,
        "final_state": FINAL_STATES[scenario_id],
        "observed_at_utc": observed_at_utc,
        "evidence_bindings": evidence_bindings,
        "facts": dict(facts),
    }
    validated = validate_facts_document(
        payload,
        scenario_id=scenario_id,
        job_id=job_id,
        run_id=run_id,
        approved_roots=approved_roots,
    )
    if output is not None:
        secure_write_json(output, validated)
    return validated


def build_receipt(
    *,
    scenario_id: str,
    job_id: str,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    run_id: str,
    facts: Mapping[str, Any],
    evidence_bindings: list[dict[str, Any]],
    producer: dict[str, Any],
    observed_at_utc: str,
    output: Path | None = None,
) -> dict[str, Any]:
    """从已绑定的原始事实生成成功收据；失败时不产生收据。"""

    normalized_facts = validate_facts(scenario_id, facts)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "job_id": job_id,
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "run_nonce": run_nonce,
        "run_id": run_id,
        "status": "success",
        "final_state": FINAL_STATES[scenario_id],
        "observed_at_utc": observed_at_utc,
        "evidence_bindings": evidence_bindings,
        "facts": normalized_facts,
        "producer": producer,
    }
    validate_receipt(
        payload,
        scenario_id=scenario_id,
        job_id=job_id,
        campaign_id=campaign_id,
        attempt_id=attempt_id,
        run_nonce=run_nonce,
        run_id=run_id,
    )
    if output is not None:
        secure_write_json(output, payload)
    return payload


def write_failure_diagnostic(output: Path, *, scenario_id: str, reason: str) -> None:
    """写入不参与判定的失败诊断，不伪造成功收据。"""

    secure_write_json(
        output,
        {
            "schema_version": FAILURE_SCHEMA_VERSION,
            "scenario_id": scenario_id,
            "status": "diagnostic",
            "reason": reason[:1000],
        },
    )
