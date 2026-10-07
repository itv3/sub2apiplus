"""VC-1 Wire 投影的合成证据夹具（Claude 升级控制面设计 B3-03 补充项、B3-04 的测试共用）。

只生成主模型（投影策略模型闭集的第一个）需要的 23 个 probe：每个 probe 的中继请求字节按「一条有效连接一条请求」写成
``relay/connNNN.client_to_upstream.bin``，P 通道写四条 ClientHello（两条带 ALPN、两条不带）。请求的形态满足投影的全部
选择谓词（agent-id、标题与 fallback 模型、background 四条、WebSearch 两种、工具目录计数、advisor 类型、effort、
thinking.display、count_tokens），推理请求的计费头按投影策略的 salt 与 message index 算出版本指纹。

不读任何本地取证数据；Authorization 一律是脱敏占位 ``<secret>``。
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any, Mapping

from tools import generate_claude_fw_g_profile as generator

# 证据文件：probe → [(probe 目录内相对路径, 字节, 标签 kind, 有效连接序号或 None)]。
WireFiles = dict[str, list[tuple[str, bytes, str, int | None]]]

STATIC_SYSTEM = [
    {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    {"type": "text", "text": "\nYou are an interactive agent that helps users with software engineering tasks."},
    {"type": "text", "text": "\n# Environment\nPlatform: linux"},
]
DEFAULT_BETA = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-management-2025-06-27"
USER_AGENT = "claude-cli/{version} (external, sdk-cli)"


def _header_list(version: str, *, agent_id: str | None = None, beta: str = DEFAULT_BETA) -> list[tuple[str, str]]:
    headers = [
        ("Accept", "application/json"),
        ("Authorization", "Bearer <secret>"),
        ("Content-Type", "application/json"),
        ("User-Agent", USER_AGENT.format(version=version)),
        ("X-Claude-Code-Session-Id", "00000000-0000-4000-8000-000000000000"),
        ("anthropic-beta", beta),
        ("anthropic-version", "2023-06-01"),
        ("x-app", "cli"),
    ]
    if agent_id is not None:
        headers.append(("x-claude-code-agent-id", agent_id))
    headers.extend([("Connection", "keep-alive"), ("Host", "api.anthropic.com")])
    return headers


def http_request(target: str, headers: list[tuple[str, str]], body: Mapping[str, Any]) -> bytes:
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    head = f"POST {target} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers)
    head += f"Content-Length: {len(raw_body)}\r\n\r\n"
    return head.encode("latin-1") + raw_body


class WireEvidenceBuilder:
    """按投影策略生成主模型的合成请求。"""

    def __init__(self, policy: Mapping[str, Any], version: str) -> None:
        self.policy = policy
        self.version = version
        self.model = generator.base_model(dict(policy))
        self.models = policy["models"]
        self.selectors = policy["selectors"]

    def _system(self, user_text: str) -> list[dict[str, Any]]:
        fingerprint = generator.version_fingerprint(dict(self.policy), self.version, user_text)
        billing = f"{generator.BILLING_HEADER_PREFIX} cc_version={self.version}.{fingerprint}; cc_entrypoint=sdk-cli; cch=abcde;"
        return [{"type": "text", "text": billing}, *(dict(block) for block in STATIC_SYSTEM)]

    def messages(
        self,
        *,
        model: str | None = None,
        user_text: str = "Reply with exactly: ok",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None | bool = True,
        effort: str = generator.BASELINE_EFFORT,
        agent_id: str | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> bytes:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "system": self._system(user_text),
            "tools": tools if tools is not None else [],
            "metadata": {"user_id": "user_fixture"},
            "max_tokens": 64000,
        }
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if thinking is True:
            body["thinking"] = {"type": generator.THINKING_TYPE}
        elif isinstance(thinking, dict):
            body["thinking"] = thinking
        body["output_config"] = {"effort": effort}
        body["stream"] = True
        return http_request(generator.MESSAGES_TARGET, [*_header_list(self.version, agent_id=agent_id), *(extra_headers or [])], body)

    def server_fallback(self, fallback_model: str) -> bytes:
        """server fallback 锁存请求：回退模型、``x-is-refusal-fallback: true`` 与锁存来源 Header。"""

        return self.messages(
            model=fallback_model,
            user_text="Run ls after refusal",
            extra_headers=[("x-cc-fallback-latched-by", self.model), ("x-is-refusal-fallback", "true")],
        )

    def count_tokens(self) -> bytes:
        body = {"model": self.model, "messages": [{"role": "user", "content": "count me"}]}
        return http_request(f"{generator.COUNT_TOKENS_TARGET_PREFIX}?beta=true", _header_list(self.version), body)

    @staticmethod
    def tools(count: int, *, first: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        listed = [first] if first is not None else []
        listed.extend({"name": f"Tool{index:02d}", "description": "fixture", "input_schema": {"type": "object"}} for index in range(count - len(listed)))
        return listed

    def probes(self) -> dict[str, list[bytes]]:
        """主模型 23 个 probe 的推理请求字节（按有效连接顺序）。"""

        named = lambda name: {"name": name, "description": "fixture", "input_schema": {"type": "object"}}  # noqa: E731
        background_tools = self.tools(self.selectors["background_tool_count"])
        result: dict[str, list[bytes]] = {
            generator.PROBE_BASELINE: [self.messages()],
            generator.PROBE_AGENT: [
                self.messages(tools=self.tools(3, first=named(generator.TOOL_AGENT))),
                self.messages(user_text="Subagent task: list files", agent_id="0123456789abcdef0"),
            ],
            generator.PROBE_TUI: [
                self.messages(user_text="Write a haiku about tests"),
                self.messages(model=self.models["title_model"], user_text="Generate a title for: tests", thinking=False),
            ],
            generator.PROBE_FALLBACK: [
                self.messages(user_text="Trigger fallback please"),
                self.messages(model=self.models["fallback_model"], user_text="Trigger fallback please", thinking=False),
            ],
            generator.PROBE_BACKGROUND_SUBAGENT: [
                self.messages(user_text="Background subagent main"),
                self.messages(user_text="Background subagent child", agent_id="fedcba9876543210f"),
            ],
            generator.PROBE_BACKGROUND: [
                self.messages(tools=background_tools, user_text="Run in background"),
                self.messages(model=self.models["title_model"], user_text="Background title", thinking=False),
                self.messages(model=self.models["fallback_model"], user_text="Background summary one", thinking=False),
                self.messages(model=self.models["fallback_model"], user_text="Background summary two", thinking=False),
            ],
            generator.PROBE_CUSTOM_SYSTEM: [self.messages(user_text="Custom system prompt run")],
            generator.PROBE_APPEND_SYSTEM: [self.messages(user_text="Append system prompt run")],
            generator.PROBE_EXCLUDE_DYNAMIC: [self.messages(user_text="Exclude dynamic system run")],
            generator.PROBE_CUSTOM_AGENT: [self.messages(user_text="Custom agent run")],
            generator.PROBE_WEB_SEARCH: [
                self.messages(tools=[named(generator.TOOL_WEB_SEARCH_OUTER)], user_text="Search the web for fixtures"),
                self.messages(
                    tools=[{"type": "web_search_20250305", "name": generator.TOOL_WEB_SEARCH_SERVER}],
                    tool_choice={"type": "tool", "name": generator.TOOL_WEB_SEARCH_SERVER},
                    user_text="Perform a web search for the query: fixtures",
                ),
            ],
            generator.PROBE_JSON_SCHEMA: [self.messages(tools=[named(generator.TOOL_STRUCTURED_OUTPUT)], user_text="Return JSON")],
            generator.PROBE_BASH: [self.messages(tools=self.tools(4, first=named(generator.TOOL_BASH)), user_text="Run ls")],
            generator.PROBE_MCP_DEFERRED: [self.messages(tools=self.tools(self.selectors["mcp_deferred_tool_count"]), user_text="Use MCP")],
            generator.PROBE_ADVISOR: [
                self.messages(
                    tools=[*self.tools(2), {"type": self.selectors["advisor_tool_type"], "name": "advisor", "model": self.model}],
                    user_text="Ask the advisor",
                )
            ],
            generator.PROBE_COUNT_TOKENS: [self.count_tokens()],
            generator.PROBE_THINKING_DISABLED: [self.messages(thinking=False, user_text="No thinking please")],
        }
        for effort in self.policy["efforts"]:
            if effort != generator.BASELINE_EFFORT:
                result[generator.effort_probe(effort)] = [self.messages(effort=effort, user_text=f"Effort {effort} run")]
        for display in self.policy["thinking_display_values"]:
            result[f"{generator.PROBE_THINKING_DISPLAY_PREFIX}{display}"] = [
                self.messages(thinking={"type": generator.THINKING_TYPE, "display": display}, user_text=f"Display {display} run")
            ]
        return result

    def pcap(self) -> bytes:
        tls = self.policy["tls"]
        return b"".join(client_hello(tls, alpn=index < 2, seed=index) for index in range(4))

    def files(self) -> WireFiles:
        """probe → 证据文件（中继请求按连接号逐条一个文件；TLS probe 另带 pcap）。"""

        result: WireFiles = {}
        for probe, requests in self.probes().items():
            result[probe] = [
                (f"relay/conn{index:03d}.client_to_upstream.bin", raw, "relay_binary", index) for index, raw in enumerate(requests, start=1)
            ]
        tls_request = http_request("/api/hello", _header_list(self.version), {"probe": "tls"})
        result[generator.PROBE_TLS] = [
            ("relay/conn001.client_to_upstream.bin", tls_request, "relay_binary", 1),
            ("tls-clienthello.pcap", self.pcap(), "pcap", None),
        ]
        return result


def client_hello(tls: Mapping[str, Any], *, alpn: bool, seed: int) -> bytes:
    """按 TLS 期望向量合成一条 ClientHello record（扩展顺序取策略登记的顺序）。"""

    def u16_vector(items: list[int]) -> bytes:
        data = b"".join(struct.pack(">H", item) for item in items)
        return struct.pack(">H", len(data)) + data

    name = generator.TLS_SNI.encode("ascii")
    payloads: dict[int, bytes] = {
        0: struct.pack(">H", len(name) + 3) + b"\x00" + struct.pack(">H", len(name)) + name,
        10: u16_vector(tls["supported_groups"]),
        11: bytes([len(tls["point_formats"])]) + bytes(tls["point_formats"]),
        13: u16_vector(tls["signature_algorithms"]),
        16: struct.pack(">H", len(generator.TLS_ALPN) + 1) + bytes([len(generator.TLS_ALPN)]) + generator.TLS_ALPN.encode("ascii"),
        43: bytes([2 * len(tls["supported_versions"])]) + b"".join(struct.pack(">H", item) for item in tls["supported_versions"]),
        45: bytes([len(tls["psk_modes"])]) + bytes(tls["psk_modes"]),
    }
    shares = b"".join(struct.pack(">H", group) + struct.pack(">H", 32) + hashlib.sha256(f"{seed}:{group}".encode()).digest() for group in tls["key_share_groups"])
    payloads[51] = struct.pack(">H", len(shares)) + shares
    order = tls["with_alpn_extensions"] if alpn else tls["without_alpn_extensions"]
    extensions = b"".join(struct.pack(">HH", ext, len(payloads.get(ext, b""))) + payloads.get(ext, b"") for ext in order)
    random = hashlib.sha256(f"random:{seed}".encode()).digest()
    session = hashlib.sha256(f"session:{seed}".encode()).digest()
    ciphers = b"".join(struct.pack(">H", item) for item in tls["cipher_suites"])
    body = (
        b"\x03\x03" + random + bytes([len(session)]) + session + struct.pack(">H", len(ciphers)) + ciphers + b"\x01\x00"
        + struct.pack(">H", len(extensions)) + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake


def single_model_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """只保留主模型的投影策略（VC-1 现行场景清单只用主模型取证）。"""

    result = json.loads(json.dumps(policy))
    primary = result["models"]["ordered"][0]
    result["models"]["ordered"] = [primary]
    result["models"]["legacy_retry_fallback"] = [model for model in result["models"]["legacy_retry_fallback"] if model == primary]
    result["models"]["server_fallbacks"] = []
    return result
