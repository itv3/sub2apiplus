#!/usr/bin/env python3
"""从真实服务端记录与客户端安装事实组装 Kilo 双协议收据。

`capture-candidate seal` 要求每个 Kilo 客户端提供五份事实：installation、ingress、
runtime-audit、response、usage。其中 runtime-audit 由
`build_observed_profile_runtime_audit.py` 承接服务启动事实产出，其余四份此前没有任何
产出方——candidate seal 因此从未走通。

本工具的边界与 observed-profile 一致：**只承接既有事实，不编造**。

- ingress／response／usage 的内容全部来自服务端自己的记录：`usage_logs` 行与
  `http.access` 结构化日志。工具不参与判断请求是否成功，只搬运 `status_code`、
  `completed_at` 等字段；
- installation 来自客户端可执行文件本身（路径与内容摘要），由调用方在发起请求的那台
  机器上测量后传入；
- campaign／attempt／run_nonce／candidate 这组坐标是采集侧的权威事实，服务端无从得知，
  由调用方提供并写入每份收据，供 finalizer 做关联校验。

标识映射（两个客户端一致，可复算）：

- `request_id` 取服务端记录的 `client_request_id`，即客户端为该次调用生成的标识；
- `response_id` 取服务端 `request_id`，即服务端为这次请求-响应对分配的唯一标识。
  Kilo 请求走冻结合成 relay，上游不返回自己的响应 ID，服务端标识是唯一可复算的真实值。

`witness_id`／`event_id`／`installation_id` 一律对收据内容做内容寻址，不使用随机数，
同一批事实重复运行必然得到同一份产物。
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

INSTALLATION_SCHEMA = "kilo-installation/v1"
INGRESS_SCHEMA = "kilo-ingress-witness/v1"
RESPONSE_SCHEMA = "kilo-response-witness/v1"
USAGE_SCHEMA = "sub2api-usage-audit/v1"
RUNTIME_AUDIT_SCHEMA = "codex-egress-runtime-audit/v1"

CLIENT_CONTRACTS = {
    "kilo-compatible": {
        "protocol": "openai-compatible",
        "entrypoint": "/v1/chat/completions",
    },
    "kilo-responses": {
        "protocol": "openai-responses",
        "entrypoint": "/v1/responses",
    },
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class KiloReceiptError(RuntimeError):
    """输入事实不足以生成可信收据。"""


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _content_id(prefix: str, payload: Mapping[str, Any]) -> str:
    """按收据内容寻址生成标识；同一批事实必然复算出同一个值。"""

    identifier = f"{prefix}-{_fingerprint(payload)[:32]}"
    if not SAFE_ID_RE.fullmatch(identifier):
        raise KiloReceiptError(f"生成的标识不符合安全格式：{identifier}")
    return identifier


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise KiloReceiptError(f"{label}缺失或含首尾空白")
    return value


def _require_utc(value: Any, label: str) -> str:
    """服务端时间戳统一归一到带 Z 的 UTC，避免收据里混用本地偏移。"""

    from datetime import datetime, timezone

    text = _require(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise KiloReceiptError(f"{label}不是有效时间：{text}") from error
    if parsed.tzinfo is None:
        raise KiloReceiptError(f"{label}缺少时区")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# Kilo 两条入口用不同的 SDK 发请求：Compatible 走 ai-sdk，User-Agent 带 Kilo-Code；
# Responses 的 WebSocket 走 OpenAI 官方 JS SDK，User-Agent 就是 SDK 自己的标识。
# 二者都是同一个客户端的真实行为，不能要求两条路径的 UA 形态一致。
KNOWN_SDK_USER_AGENT_RE = re.compile(r"^(OpenAI/JS|openai-python|OpenAI/Python)[/ ]")


def _client_version_from_user_agent(user_agent: str, fallback: str) -> str:
    """取客户端版本：UA 自报时以 UA 为准，SDK UA 则回落到安装事实。

    UA 里出现 Kilo-Code 时必须能解析出版本，并由调用方与安装事实逐字比对——这是最强的
    一手证据。走 OpenAI SDK 的那条入口，UA 描述的是 SDK 而不是宿主客户端，此时客户端版本
    只能来自本机安装事实（可执行文件路径 + 内容摘要），UA 原文仍如实写进收据备查。

    两者都不是则拒绝：那说明这次调用并非来自已声明的 Kilo 安装。
    """

    match = re.search(r"Kilo-Code/([0-9]+\.[0-9]+\.[0-9]+)", user_agent)
    if match:
        version = match.group(1)
        if not VERSION_RE.fullmatch(version):
            raise KiloReceiptError(f"客户端版本不是三段数字：{version}")
        return version
    if KNOWN_SDK_USER_AGENT_RE.match(user_agent):
        return fallback
    raise KiloReceiptError(
        f"User-Agent 既不含 Kilo-Code 版本，也不是已知 SDK 形态：{user_agent}"
    )


def _validate_identity(identity: Mapping[str, Any]) -> None:
    for field in ("campaign_id", "attempt_id", "candidate_id", "target_version"):
        _require(identity.get(field), f"identity.{field}")
    nonce = _require(identity.get("run_nonce"), "identity.run_nonce")
    if not RUN_NONCE_RE.fullmatch(nonce):
        raise KiloReceiptError("identity.run_nonce 必须是 64 位十六进制")
    if not VERSION_RE.fullmatch(str(identity.get("target_version"))):
        raise KiloReceiptError("identity.target_version 必须是三段数字")


def build_installation(
    *,
    executable_path: str,
    executable_sha256: str,
    client_version: str,
    display_name: str,
    observed_at_utc: str,
) -> dict[str, Any]:
    if not executable_path.startswith("/"):
        raise KiloReceiptError("executable_path 必须是绝对 POSIX 路径")
    if not SHA256_RE.fullmatch(executable_sha256):
        raise KiloReceiptError("executable_sha256 必须是 64 位小写十六进制")
    body = {
        "client_version": client_version,
        "display_name": display_name,
        "executable_path": executable_path,
        "executable_sha256": executable_sha256,
        "product_id": "kilo",
    }
    return {
        "schema_version": INSTALLATION_SCHEMA,
        "source": "kilo-installation",
        "installation_id": _content_id("kilo", body),
        "product_id": "kilo",
        "display_name": display_name,
        "client_version": client_version,
        "executable_path": executable_path,
        "executable_sha256": executable_sha256,
        "observed_at_utc": _require_utc(observed_at_utc, "installation.observed_at_utc"),
    }


def build_ingress(
    *,
    identity: Mapping[str, Any],
    client_id: str,
    installation_id: str,
    client_version: str,
    model: str,
    request_id: str,
    received_at_utc: str,
) -> dict[str, Any]:
    contract = CLIENT_CONTRACTS[client_id]
    body = {
        "attempt_id": identity["attempt_id"],
        "campaign_id": identity["campaign_id"],
        "client_id": client_id,
        "installation_id": installation_id,
        "received_at_utc": received_at_utc,
        "request_id": request_id,
        "run_nonce": identity["run_nonce"],
    }
    return {
        "schema_version": INGRESS_SCHEMA,
        "source": "kilo-ingress",
        "witness_id": _content_id("ingress", body),
        "request_id": request_id,
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "installation_id": installation_id,
        "client_id": client_id,
        "client_version": client_version,
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": model,
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
        "received_at_utc": _require_utc(received_at_utc, "ingress.received_at_utc"),
    }


def build_response(
    *,
    identity: Mapping[str, Any],
    client_id: str,
    installation_id: str,
    request_id: str,
    response_id: str,
    http_status: int,
    completed_at_utc: str,
) -> dict[str, Any]:
    # Kilo 的 Responses 入口走 WebSocket，服务端记 101 Switching Protocols——那是该
    # 链路成功的唯一正确状态码，与 2xx 同属成功语义；101 之外的 1xx 仍然拒绝。
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        raise KiloReceiptError("http_status 必须是整数")
    if http_status != 101 and not 200 <= http_status < 300:
        raise KiloReceiptError(f"服务端记录的响应不是成功状态：{http_status}")
    body = {
        "attempt_id": identity["attempt_id"],
        "campaign_id": identity["campaign_id"],
        "client_id": client_id,
        "request_id": request_id,
        "response_id": response_id,
        "run_nonce": identity["run_nonce"],
    }
    return {
        "schema_version": RESPONSE_SCHEMA,
        "source": "kilo-response",
        "witness_id": _content_id("response", body),
        "request_id": request_id,
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "installation_id": installation_id,
        "client_id": client_id,
        "candidate_id": identity["candidate_id"],
        "http_status": http_status,
        "response_id": response_id,
        "completed_at_utc": _require_utc(completed_at_utc, "response.completed_at_utc"),
    }


def build_usage(
    *,
    identity: Mapping[str, Any],
    request_id: str,
    response_id: str,
    usage_id: str,
    oauth_account_id: int,
    recorded_at_utc: str,
) -> dict[str, Any]:
    if not isinstance(oauth_account_id, int) or oauth_account_id <= 0:
        raise KiloReceiptError("oauth_account_id 必须是正整数")
    body = {
        "attempt_id": identity["attempt_id"],
        "campaign_id": identity["campaign_id"],
        "request_id": request_id,
        "run_nonce": identity["run_nonce"],
        "usage_id": usage_id,
    }
    return {
        "schema_version": USAGE_SCHEMA,
        "source": "sub2api-usage",
        "event_id": _content_id("usage", body),
        "request_id": request_id,
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "response_id": response_id,
        "candidate_id": identity["candidate_id"],
        "usage_id": usage_id,
        "oauth_account_id": oauth_account_id,
        "recorded_at_utc": _require_utc(recorded_at_utc, "usage.recorded_at_utc"),
    }


def build_runtime_audit(
    *,
    identity: Mapping[str, Any],
    client_id: str,
    installation_id: str,
    ingress_witness_id: str,
    request_id: str,
    model: str,
    oauth_account_id: int,
    upstream_endpoint: str,
    transport: str,
    observed_at_utc: str,
    candidate_image_id: str,
    source_tree_sha256: str,
    build_id: str,
    deployed_version: str,
) -> dict[str, Any]:
    """记录这次入站调用在服务内部实际走了哪条出站链路。

    与 observed-profile 那份 runtime-audit 不是一回事：那份描述服务整体解析到哪个画像，
    这份描述**单次调用**的出站形态——传输方式、上游端点、认证模式。内容全部来自服务端
    对该次请求的记录（usage_logs 的 upstream_endpoint 与 openai_ws_mode），工具不推断。
    """

    if transport not in {"http", "websocket"}:
        raise KiloReceiptError(f"transport 只能是 http 或 websocket：{transport}")
    if client_id == "kilo-compatible" and transport != "http":
        raise KiloReceiptError("kilo-compatible 的出站事实必须是 HTTP 传输")
    if not isinstance(oauth_account_id, int) or oauth_account_id <= 0:
        raise KiloReceiptError("oauth_account_id 必须是正整数")
    contract = CLIENT_CONTRACTS[client_id]
    body = {
        "attempt_id": identity["attempt_id"],
        "campaign_id": identity["campaign_id"],
        "client_id": client_id,
        "request_id": request_id,
        "run_nonce": identity["run_nonce"],
        "transport": transport,
    }
    return {
        "schema_version": RUNTIME_AUDIT_SCHEMA,
        "source": "sub2api-runtime",
        "event_type": "oauth_request_forwarded",
        "event_id": _content_id("runtime", body),
        "request_id": request_id,
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "ingress_witness_id": ingress_witness_id,
        "installation_id": installation_id,
        "client_id": client_id,
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": model,
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "image_id": candidate_image_id,
        "source_tree_sha256": source_tree_sha256,
        "build_id": build_id,
        "deployed_version": deployed_version,
        "auth_mode": "oauth",
        "oauth_account_id": oauth_account_id,
        "upstream_endpoint": upstream_endpoint,
        "transport": transport,
        "affected_branches": [transport],
        "observed_at_utc": _require_utc(observed_at_utc, "runtime_audit.observed_at_utc"),
    }

def build_client_receipts(
    *,
    identity: Mapping[str, Any],
    client_id: str,
    observation: Mapping[str, Any],
    installation: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """把一个客户端的服务端观测组装成四份收据。"""

    if client_id not in CLIENT_CONTRACTS:
        raise KiloReceiptError(f"未知 client_id：{client_id}")
    _validate_identity(identity)

    entrypoint = _require(observation.get("entrypoint"), "observation.entrypoint")
    expected = CLIENT_CONTRACTS[client_id]["entrypoint"]
    if entrypoint != expected:
        raise KiloReceiptError(
            f"{client_id} 的服务端入口是 {entrypoint}，与契约 {expected} 不符"
        )

    user_agent = _require(observation.get("user_agent"), "observation.user_agent")
    client_version = _client_version_from_user_agent(
        user_agent, installation["client_version"]
    )
    if client_version != installation["client_version"]:
        raise KiloReceiptError(
            "服务端观测到的客户端版本与安装事实不一致："
            f"{client_version} != {installation['client_version']}"
        )

    request_id = _require(observation.get("request_id"), "observation.request_id")
    response_id = _require(observation.get("response_id"), "observation.response_id")
    installation_id = installation["installation_id"]

    ingress = build_ingress(
        identity=identity,
        client_id=client_id,
        installation_id=installation_id,
        client_version=client_version,
        model=_require(observation.get("model"), "observation.model"),
        request_id=request_id,
        received_at_utc=_require(
            observation.get("received_at_utc"), "observation.received_at_utc"
        ),
    )
    result = {
        "ingress": ingress,
        "response": build_response(
            identity=identity,
            client_id=client_id,
            installation_id=installation_id,
            request_id=request_id,
            response_id=response_id,
            http_status=observation.get("http_status"),
            completed_at_utc=_require(
                observation.get("completed_at_utc"), "observation.completed_at_utc"
            ),
        ),
        "usage": build_usage(
            identity=identity,
            request_id=request_id,
            response_id=response_id,
            usage_id=_require(observation.get("usage_id"), "observation.usage_id"),
            oauth_account_id=observation.get("oauth_account_id"),
            recorded_at_utc=_require(
                observation.get("recorded_at_utc"), "observation.recorded_at_utc"
            ),
        ),
    }
    result["runtime_audit"] = build_runtime_audit(
        identity=identity,
        client_id=client_id,
        installation_id=installation_id,
        ingress_witness_id=ingress["witness_id"],
        request_id=request_id,
        model=ingress["model"],
        oauth_account_id=observation.get("oauth_account_id"),
        upstream_endpoint=_require(
            observation.get("upstream_endpoint"), "observation.upstream_endpoint"
        ),
        transport=_require(observation.get("transport"), "observation.transport"),
        observed_at_utc=_require(
            observation.get("completed_at_utc"), "observation.completed_at_utc"
        ),
        candidate_image_id=_require(
            identity.get("candidate_image_id"), "identity.candidate_image_id"
        ),
        source_tree_sha256=_require(
            identity.get("source_tree_sha256"), "identity.source_tree_sha256"
        ),
        build_id=_require(identity.get("build_id"), "identity.build_id"),
        deployed_version=_require(
            identity.get("deployed_version"), "identity.deployed_version"
        ),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从服务端观测与客户端安装事实组装 Kilo 双协议收据"
    )
    parser.add_argument("--facts", type=Path, required=True, help="服务端观测与安装事实 JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        document = json.loads(arguments.facts.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"无法读取事实文件：{error}") from error

    identity = document.get("identity") or {}
    installation_facts = document.get("installation") or {}
    observations = document.get("observations") or {}
    if not isinstance(observations, dict) or not observations:
        raise SystemExit("事实文件缺少 observations")

    installation = build_installation(
        executable_path=_require(
            installation_facts.get("executable_path"), "installation.executable_path"
        ),
        executable_sha256=_require(
            installation_facts.get("executable_sha256"), "installation.executable_sha256"
        ),
        client_version=_require(
            installation_facts.get("client_version"), "installation.client_version"
        ),
        display_name=_require(
            installation_facts.get("display_name"), "installation.display_name"
        ),
        observed_at_utc=_require(
            installation_facts.get("observed_at_utc"), "installation.observed_at_utc"
        ),
    )

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    installation_path = arguments.output_dir / "kilo-installation.json"
    installation_path.write_text(
        json.dumps(installation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    installation_path.chmod(0o600)
    written.append(installation_path.name)

    for client_id, observation in sorted(observations.items()):
        receipts = build_client_receipts(
            identity=identity,
            client_id=client_id,
            observation=observation,
            installation=installation,
        )
        for kind, payload in sorted(receipts.items()):
            path = arguments.output_dir / f"{client_id}-{kind}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            written.append(path.name)

    print(
        json.dumps(
            {
                "installation_id": installation["installation_id"],
                "clients": sorted(observations),
                "written": written,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
