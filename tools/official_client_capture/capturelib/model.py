"""抓包任务模型、矩阵和输入校验。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import urllib.parse
from collections.abc import Iterable
from typing import Any


SCHEMA_VERSION = "official-client-capture/v1"
TASKS = ("oauth", "api")
EVIDENCE_MODES = ("direct", "mitm")
CLAUDE_AGENT_SCENARIOS = ("a1", "a2", "a3")
SCENARIOS = ("s1", "s2", "s4", *CLAUDE_AGENT_SCENARIOS)
PRODUCT_TRANSPORTS = (
    ("claude", "http"),
    ("codex", "http"),
    ("codex", "ws"),
)
SUBJECTS = tuple(f"{product}-{transport}" for product, transport in PRODUCT_TRANSPORTS)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 0.145→0.147 双轨采集的模型坐标权威定义。
#
# 收录进主线的唯一条件是上游 /models 元数据给出 use_responses_lite=false：主线判据
# （BODY-006、EP-014、H1-004 等）整体建立在非 Lite 形态上，混进一个 Lite 模型会让
# 全部主线样本的语义翻转，而不是少采一条。两个集合都实测自官方 /models 原文，
# k51 官方证据：gpt-5.4／gpt-5.5／gpt-5.4-mini／gpt-5.3-codex-spark 为 false，
# gpt-5.6-* 全系为 true。
#
# 主线只收录已在本升级中实际采过或即将采的两个，而不是所有 non-lite 模型——没被
# 实测过的模型不进白名单，保持 fail-closed。改这两个集合必须同步 h1_wire_probe 的
# 受控 /models 载荷与 extract_compaction_reason 的 ALLOWED_MODELS，
# test_main_track_models.py 锁定三者一致。
MAIN_TRACK_MODELS = ("gpt-5.4", "gpt-5.5")
LITE_TRACK_MODELS = ("gpt-5.6-luna",)


class ConfigurationError(ValueError):
    """抓包配置不满足安全或可比性约束。"""


def utc_now() -> str:
    """返回带毫秒的 UTC 时间。"""

    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def make_batch_id(now: dt.datetime | None = None) -> str:
    """生成不含秘密且可安全用于目录名的批次编号。"""

    value = now or dt.datetime.now(dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def validate_safe_name(value: str, field: str) -> str:
    """拒绝可造成目录穿越或命令歧义的标识。"""

    if not SAFE_NAME_RE.fullmatch(value):
        raise ConfigurationError(
            f"{field} 只能包含字母、数字、点、下划线和连字符，且长度不超过 128。"
        )
    return value


def validate_choice_list(
    values: Iterable[str], allowed: tuple[str, ...], field: str
) -> tuple[str, ...]:
    """校验、去重并保持用户给定顺序。"""

    result: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ConfigurationError(
                f"{field} 包含不支持的值 {value!r}；允许值为 {', '.join(allowed)}。"
            )
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ConfigurationError(f"{field} 不能为空。")
    return tuple(result)


def parse_https_base_url(value: str) -> urllib.parse.SplitResult:
    """解析 API 任务使用的 Sub2API 公共 HTTPS 地址。"""

    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() != "https":
        raise ConfigurationError("API 抓包的 Sub2API Base URL 必须使用 HTTPS。")
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ConfigurationError("Sub2API Base URL 的主机或端口格式非法。") from error
    if not hostname:
        raise ConfigurationError("Sub2API Base URL 缺少主机名。")
    if parsed.username or parsed.password:
        raise ConfigurationError("Sub2API Base URL 不允许包含用户名或密码。")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("Sub2API Base URL 不允许包含 query 或 fragment。")
    if hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError(
            "API direct 必须连接 Sub2API 公共 HTTPS 地址，不能使用本机明文入口。"
        )
    return parsed


def normalized_api_urls(value: str) -> tuple[str, str, str]:
    """返回 Claude Base URL、Codex `/v1` Base URL 和目标主机。"""

    parsed = parse_https_base_url(value)
    path = parsed.path.rstrip("/")
    origin_path = path[:-3] if path.endswith("/v1") else path
    claude_path = origin_path
    codex_path = f"{origin_path}/v1" if origin_path else "/v1"
    claude_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, claude_path, "", "")
    ).rstrip("/")
    codex_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, codex_path, "", "")
    )
    return claude_url, codex_url, parsed.hostname.lower()


@dataclasses.dataclass(frozen=True)
class CaptureCase:
    """一个不能与其他认证、传输或证据轮次混合的抓包单元。"""

    task: str
    run_id: str
    subject: str
    product: str
    transport: str
    evidence: str
    scenarios: tuple[str, ...]
    target_hosts: tuple[str, ...]
    base_url: str | None

    @property
    def boundary(self) -> str:
        """返回该单元的证据边界。"""

        if self.task == "oauth":
            return "official_cli_to_official_platform"
        return "official_cli_to_sub2api"

    def to_dict(self) -> dict[str, Any]:
        """生成不含凭据的序列化结构。"""

        return {
            "task": self.task,
            "run_id": self.run_id,
            "subject": self.subject,
            "product": self.product,
            "transport": self.transport,
            "evidence": self.evidence,
            "scenarios": list(self.scenarios),
            "target_hosts": list(self.target_hosts),
            "base_url": self.base_url,
            "boundary": self.boundary,
        }


@dataclasses.dataclass(frozen=True)
class CampaignPlan:
    """OAuth 或 API 的独立任务计划。"""

    schema_version: str
    task: str
    batch_id: str
    run_id: str
    cases: tuple[CaptureCase, ...]
    credential: dict[str, Any]
    external_ab_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """生成 dry-run 与 manifest 共用的安全结构。"""

        return {
            "schema_version": self.schema_version,
            "task": self.task,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "credential": dict(self.credential),
            "external_ab_executed": self.external_ab_executed,
            "cases": [case.to_dict() for case in self.cases],
        }


def build_campaign_plan(
    *,
    task: str,
    batch_id: str,
    scenarios: tuple[str, ...],
    evidence_modes: tuple[str, ...],
    sub2api_base_url: str | None,
    api_key_env: str,
    subjects: tuple[str, ...] = SUBJECTS,
    oauth_claude_token_env: str | None = None,
) -> CampaignPlan:
    """构造一套 OAuth 或 API 任务，绝不合并两者的运行目录。"""

    if task not in TASKS:
        raise ConfigurationError(f"不支持的任务：{task}")
    validate_safe_name(batch_id, "batch_id")
    run_id = validate_safe_name(f"{task}-{batch_id}", "run_id")

    claude_api_url: str | None = None
    codex_api_url: str | None = None
    api_host: str | None = None
    if task == "api":
        if not sub2api_base_url:
            raise ConfigurationError("API 任务必须提供 --sub2api-base-url。")
        claude_api_url, codex_api_url, api_host = normalized_api_urls(
            sub2api_base_url
        )

    cases: list[CaptureCase] = []
    for evidence in evidence_modes:
        for product, transport in PRODUCT_TRANSPORTS:
            if f"{product}-{transport}" not in subjects:
                continue
            if task == "oauth":
                target_hosts = (
                    ("api.anthropic.com",)
                    if product == "claude"
                    else ("chatgpt.com",)
                )
                base_url = None
            else:
                assert api_host is not None
                target_hosts = (api_host,)
                base_url = claude_api_url if product == "claude" else codex_api_url
            cases.append(
                CaptureCase(
                    task=task,
                    run_id=run_id,
                    subject=f"{product}-{transport}",
                    product=product,
                    transport=transport,
                    evidence=evidence,
                    scenarios=scenarios,
                    target_hosts=target_hosts,
                    base_url=base_url,
                )
            )

    credential = (
        {
            "kind": (
                "oauth_state_with_claude_runtime_token"
                if oauth_claude_token_env
                else "oauth_state"
            ),
            "source": (
                "isolated_state_plus_runtime_environment"
                if oauth_claude_token_env
                else "isolated_existing_state"
            ),
            **(
                {"claude_token_source_env": oauth_claude_token_env}
                if oauth_claude_token_env
                else {}
            ),
            "orchestrator_policy": "use_in_place_without_copying",
            "runtime_value_scan_available": bool(oauth_claude_token_env),
        }
        if task == "oauth"
        else {
            "kind": "sub2api_access_key",
            "source_env": api_key_env,
            "orchestrator_policy": "runtime_environment_only",
            "runtime_value_scan_available": True,
        }
    )
    return CampaignPlan(
        schema_version=SCHEMA_VERSION,
        task=task,
        batch_id=batch_id,
        run_id=run_id,
        cases=tuple(cases),
        credential=credential,
    )


def build_suite_plans(
    *,
    task: str,
    batch_id: str,
    scenarios: tuple[str, ...],
    evidence_modes: tuple[str, ...],
    sub2api_base_url: str | None,
    api_key_env: str,
    subjects: tuple[str, ...] = SUBJECTS,
    oauth_claude_token_env: str | None = None,
) -> tuple[CampaignPlan, ...]:
    """按 OAuth→API 顺序返回两个互相独立的任务。"""

    selected = TASKS if task == "all" else (task,)
    return tuple(
        build_campaign_plan(
            task=item,
            batch_id=batch_id,
            scenarios=scenarios,
            evidence_modes=evidence_modes,
            sub2api_base_url=sub2api_base_url,
            api_key_env=api_key_env,
            subjects=subjects,
            oauth_claude_token_env=oauth_claude_token_env,
        )
        for item in selected
    )
