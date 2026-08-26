#!/usr/bin/env python3
"""检查 §3.5 人工合并缝与机器精确台账是否覆盖 Codex/OpenAI 出站定型面。

§5.1 要求每次合并上游后刷新台账。文档只保留少量高风险合并缝；完整路径集合由本脚本
相对 UpstreamMergePlan 冻结的 upstream commit 自动复算，并与结构化 JSON 比对，避免继续
人工维护几十行路径。目标 tag、commit 和输出位置只能来自不可变计划，工具本身不保存版本事实。

判据是「生产 Go 代码中引用了 Codex/OpenAI 出站定型专属符号」。该判据刻意不使用
通用的 OfficialEgress 前缀：那个前缀同时覆盖 Anthropic 画像，会把 §1.1 明确排除
的供应商路径卷进来。当前生产判据可区分两侧；唯一排除项是扫描器分类表对被扫描
符号名的工具自引用，不是生产出站路径。

用法：

    python3 tools/check_ledger_completeness.py \
      --upstream-merge-plan docs/egress/maintenance/upstream-<tag>-merge-plan.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_capture.tests import (  # noqa: E402
    test_codex_01491_r4_catalog_successor_transition as r4_catalog,
)

SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
SCAN_ROOT = ROOT / "backend"
INVENTORY = ROOT / "docs" / "egress" / "consolidation" / "egress-surface-inventory.json"
CHANGESET6_TRANSITION = ROOT / "docs" / "egress" / "validation" / "egress-surface-transition.json"
CANDIDATE_SURFACE_SUCCESSOR = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "codex-0.149.1-egress-surface-successor-transition.json"
)
CANDIDATE_GATE_TRANSITION = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "codex-0.149.1-candidate-gate-successor-transition.json"
)
MAINTENANCE_RETIREMENT = ROOT / "docs" / "egress" / "maintenance" / "official-egress-consolidation-retirement.json"
MAINTENANCE_RETIREMENT_SHA256 = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"
UPSTREAM_MERGE_PLAN_SCHEMA = "official-egress-upstream-merge-plan/v1"
UPSTREAM_MERGE_PLAN_SCHEMA_V2 = "official-egress-upstream-merge-plan/v2"
UPSTREAM_MERGE_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UPSTREAM_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class UpstreamMergePlan:
    """台账生成器实际消费的不可变上游计划投影。"""

    plan_id: str
    upstream_url: str
    upstream_tag: str
    upstream_commit: str
    ledger_relative: str
    ledger_path: pathlib.Path
    identity_sha256: str

# 这些文件直接改变 Codex body、namespace、能力、身份、连接或 Guard，但不一定
# 命中 SURFACE_RE。单独冻结必要集合，防止人工表格在保持总数不变时用无关路径替换。
REQUIRED_REVIEW_TOUCHPOINTS = {
    "backend/cmd/server/wire.go",
    "backend/internal/config/config.go",
    "backend/internal/pkg/apicompat/chatcompletions_responses_bridge.go",
    "backend/internal/pkg/apicompat/responses_namespace.go",
    "backend/internal/pkg/httpclient/pool.go",
    "backend/internal/repository/http_upstream.go",
    "backend/internal/service/account.go",
    "backend/internal/service/openai_codex_identity.go",
    "backend/internal/service/openai_codex_transform.go",
    "backend/internal/service/openai_content_session_seed.go",
    "backend/internal/service/openai_cookie_jar.go",
    "backend/internal/service/openai_gateway_request_body.go",
    "backend/internal/service/openai_gateway_service.go",
    "backend/internal/service/openai_model_capabilities.go",
    "backend/internal/service/openai_oauth_service.go",
    "backend/internal/service/openai_responses_lite_tools.go",
    "backend/internal/service/openai_responses_namespace.go",
    "backend/internal/service/openai_ws_forwarder.go",
    "backend/internal/service/openai_ws_protocol_resolver.go",
    "backend/internal/service/official_egress_upstream_identity_bridge.go",
}

# 上游继承的身份发现／归一化基础设施不一定直接命中 strict surface 扫描，
# 但它们决定“发现值是否能越权成为 active wire 身份”，必须进入机器合并台账。
IDENTITY_BOUNDARY_TOUCHPOINTS = {
    "backend/internal/handler/admin/setting_handler.go",
    "backend/internal/handler/admin/setting_handler_update.go",
    "backend/internal/handler/dto/settings.go",
    "backend/internal/pkg/openai/request.go",
    "backend/internal/service/domain_constants.go",
    "backend/internal/service/openai_codex_version_sync_service.go",
    "backend/internal/service/setting_gateway_runtime.go",
    "backend/internal/service/setting_parse.go",
    "backend/internal/service/setting_update.go",
    "backend/internal/service/settings_view.go",
    "backend/internal/service/wire.go",
    "frontend/src/api/admin/settings.ts",
    "frontend/src/i18n/locales/en/admin/settings.ts",
    "frontend/src/i18n/locales/zh/admin/settings.ts",
    "frontend/src/views/admin/SettingsView.vue",
}

# 人类文档只需解释这些高风险边界；完整文件闭集由结构化机器台账负责。
HUMAN_REVIEW_SEAMS = {
    "backend/internal/pkg/openai/request.go",
    "backend/internal/service/openai_codex_identity.go",
    "backend/internal/service/openai_codex_version_sync_service.go",
    "backend/internal/service/official_egress_upstream_identity_bridge.go",
    "backend/internal/service/official_egress_identity_authority.go",
    "backend/internal/officialegress/compiler.go",
    "backend/internal/officialegress/executor.go",
    "backend/internal/service/openai_ws_forwarder_payload.go",
    "backend/internal/service/openai_quota_service.go",
    "backend/internal/service/setting_gateway_runtime.go",
    "backend/internal/service/wire.go",
    "frontend/src/views/admin/SettingsView.vue",
}

# Codex/OpenAI 出站定型专属符号。命中即表示该文件参与官方出站形态的产生，
# 无论它是否携带版本字面量——WS 传输、连接池与握手定型点都不含版本号。
# 版本化的符号名按形状匹配而不写死版本号：判据若写成 Codex0145，升级后新增的
# Codex0146 文件就不会被认定为出站定型面，台账会在最需要复算的时刻悄悄漏登。
SURFACE_RE = re.compile(
    r"officialCodex|OfficialCodex|[Cc]odex\d{3,}"
    r"|OpenAIOfficialEgress|officialEgressWebSocket"
    r"|compiledOfficialEgressHTTPClient|officialEgressHTTPClient"
    r"|officialOpenAIHTTPBodyContract|OfficialEgressTransportWebSocket"
)

# 命中判据但按 §1.1 不属于本版规则范围的文件。每条必须写明依据。
# 当前只有扫描器分类表会因“以字符串引用被扫描符号”而误中；生产 Anthropic 与
# API Key mimic 路径仍由判据自身区分，不靠排除表隐藏。
SCOPE_EXCLUSIONS: dict[str, str] = {
    # 扫描工具自身：分类规则里出现 officialCodexFileUploadCall 等符号是作为
    # **被扫描对象的名字**，不是参与出站定型。把它登记进 §3.5 台账反而会让
    # 「台账 = 出站定型面」这个语义失真。
    "backend/cmd/egressscan/classify.go": "egressscan 分类规则表，符号为被扫描对象名称，不参与出站定型",
    # 画像枚举生成器是离线 main 包，只把 schema 名称写入生成源码；生产 server
    # 不导入 cmd 包。其余 dump 命令当前未命中判据，不制造空豁免。
    "backend/cmd/egressprofiledump/genenums.go": "离线枚举生成器 main 包，不进入生产 server 二进制",
}


def scan_surface_files() -> list[str]:
    """返回参与 Codex/OpenAI 出站定型的生产文件，路径相对仓库根。"""
    found: list[str] = []
    for path in sorted(SCAN_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SURFACE_RE.search(text):
            found.append(path.relative_to(ROOT).as_posix())
    return found


def ledger_text() -> str:
    """取 §3.5 全文；台账登记只在该节内有效。"""
    text = SPEC.read_text(encoding="utf-8")
    start = text.find("## 3.5 源码改动台账")
    if start < 0:
        raise SystemExit("🔴 未找到 §3.5 源码改动台账")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else len(text)]


def ledger_subsection(ledger: str, heading: str, next_heading: str) -> str:
    start = ledger.find(heading)
    if start < 0:
        raise RuntimeError(f"未找到台账小节：{heading}")
    end = ledger.find(next_heading, start + len(heading))
    if end < 0:
        raise RuntimeError(f"未找到台账小节结束标记：{next_heading}")
    return ledger[start:end]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复键，避免计划或台账产生多重解释。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


def _expect_exact_fields(
    value: object,
    expected: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}必须是对象")
    actual = set(value)
    if actual != expected:
        raise RuntimeError(
            f"{label}字段不闭合：缺失={sorted(expected - actual)}，"
            f"多余={sorted(actual - expected)}"
        )
    return value


def upstream_merge_plan_identity(document: dict[str, object]) -> str:
    """复算排除自摘要字段后的计划身份。"""

    payload = {key: value for key, value in document.items() if key != "identity_sha256"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_tag_commit(repository_root: pathlib.Path, tag: str) -> str:
    """只从本地 Git 对象库解析 tag，禁止以网络最新值补足计划。"""

    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or not GIT_COMMIT_RE.fullmatch(resolved):
        detail = completed.stderr.strip() or f"退出码 {completed.returncode}"
        raise RuntimeError(f"无法从本地 Git 解析 upstream tag {tag}：{detail}")
    return resolved


def _safe_plan_output(
    repository_root: pathlib.Path,
    raw_path: object,
    upstream_tag: str,
) -> tuple[str, pathlib.Path]:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise RuntimeError("UpstreamMergePlan 台账输出路径必须是非空 POSIX 相对路径")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or str(relative) != raw_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("UpstreamMergePlan 台账输出路径不规范或发生目录逃逸")
    expected = (
        f"docs/egress/maintenance/upstream-{upstream_tag}-egress-merge-ledger.json"
    )
    if raw_path != expected:
        raise RuntimeError(
            "UpstreamMergePlan 台账输出路径必须按目标 tag 使用不可变命名："
            f"{expected}"
        )
    output = repository_root.joinpath(*relative.parts)
    current = repository_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"UpstreamMergePlan 台账输出父路径包含符号链接：{current}")
    if output.is_symlink():
        raise RuntimeError("UpstreamMergePlan 台账输出不能是符号链接")
    return raw_path, output


def load_upstream_merge_plan(
    plan_path: pathlib.Path,
    *,
    repository_root: pathlib.Path = ROOT,
    tag_resolver: Callable[[pathlib.Path, str], str] = resolve_tag_commit,
) -> UpstreamMergePlan:
    """加载、复算并交叉验证不可变 UpstreamMergePlan。"""

    if plan_path.is_symlink() or not plan_path.is_file():
        raise RuntimeError(f"UpstreamMergePlan 必须是可信普通文件：{plan_path}")
    try:
        document = json.loads(
            plan_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 UpstreamMergePlan：{exc}") from exc
    if isinstance(document, dict) and document.get("schema_version") == UPSTREAM_MERGE_PLAN_SCHEMA_V2:
        # v2 的其余字段由统一状态机完整验证；本工具只消费其中的 Codex overlay 投影。
        # 不能只在这里挑出 tag/commit，否则会让缺失 U-0 基线的伪计划绕过完整合同。
        repository_text = str(repository_root.resolve())
        if repository_text not in sys.path:
            sys.path.insert(0, repository_text)
        try:
            from tools.upstream_merge.contracts import load_plan as load_complete_plan
            from tools.upstream_merge.errors import UpstreamMergeError

            complete = load_complete_plan(
                plan_path.resolve(),
                repository_root.resolve(),
                allow_execution_worktree=True,
            )
        except (ImportError, UpstreamMergeError) as exc:
            raise RuntimeError(f"完整 UpstreamMergePlan v2 校验失败：{exc}") from exc
        upstream = complete.document["upstream"]
        outputs = complete.document["outputs"]
        ledger_relative, ledger_path = _safe_plan_output(
            repository_root,
            outputs.get("codex_overlay_ledger"),
            upstream["tag"],
        )
        return UpstreamMergePlan(
            plan_id=complete.plan_id,
            upstream_url=upstream["url"],
            upstream_tag=upstream["tag"],
            upstream_commit=upstream["commit"],
            ledger_relative=ledger_relative,
            ledger_path=ledger_path,
            identity_sha256=complete.identity,
        )
    plan = _expect_exact_fields(
        document,
        {
            "schema_version",
            "plan_id",
            "purpose",
            "upstream",
            "outputs",
            "identity_sha256",
        },
        "UpstreamMergePlan",
    )
    if plan.get("schema_version") != UPSTREAM_MERGE_PLAN_SCHEMA:
        raise RuntimeError("UpstreamMergePlan schema_version 非法")
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or not UPSTREAM_MERGE_PLAN_ID_RE.fullmatch(plan_id):
        raise RuntimeError("UpstreamMergePlan plan_id 非法")
    if plan.get("purpose") not in {"baseline_replay", "upstream_merge"}:
        raise RuntimeError("UpstreamMergePlan purpose 非法")
    identity = plan.get("identity_sha256")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise RuntimeError("UpstreamMergePlan identity_sha256 非法")
    if upstream_merge_plan_identity(plan) != identity:
        raise RuntimeError("UpstreamMergePlan identity_sha256 漂移")

    upstream = _expect_exact_fields(
        plan.get("upstream"), {"url", "tag", "commit"}, "UpstreamMergePlan.upstream"
    )
    upstream_url = upstream.get("url")
    upstream_tag = upstream.get("tag")
    upstream_commit = upstream.get("commit")
    if (
        not isinstance(upstream_url, str)
        or not upstream_url.startswith("https://")
        or upstream_url != upstream_url.strip()
    ):
        raise RuntimeError("UpstreamMergePlan upstream.url 必须是非空 HTTPS URL")
    if not isinstance(upstream_tag, str) or not UPSTREAM_TAG_RE.fullmatch(upstream_tag):
        raise RuntimeError("UpstreamMergePlan upstream.tag 不是受支持的版本 tag")
    if not isinstance(upstream_commit, str) or not GIT_COMMIT_RE.fullmatch(upstream_commit):
        raise RuntimeError("UpstreamMergePlan upstream.commit 不是完整 Git commit")
    resolved_commit = tag_resolver(repository_root, upstream_tag)
    if resolved_commit != upstream_commit:
        raise RuntimeError(
            "UpstreamMergePlan tag／commit 不匹配："
            f"tag={upstream_tag} resolved={resolved_commit} planned={upstream_commit}"
        )

    outputs = _expect_exact_fields(
        plan.get("outputs"), {"egress_merge_ledger"}, "UpstreamMergePlan.outputs"
    )
    ledger_relative, ledger_path = _safe_plan_output(
        repository_root,
        outputs.get("egress_merge_ledger"),
        upstream_tag,
    )
    return UpstreamMergePlan(
        plan_id=plan_id,
        upstream_url=upstream_url,
        upstream_tag=upstream_tag,
        upstream_commit=upstream_commit,
        ledger_relative=ledger_relative,
        ledger_path=ledger_path,
        identity_sha256=identity,
    )


def git_path_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def git_path_differs(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", commit, "--", path],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"无法比较 upstream 路径：{path}")
    return result.returncode == 1


def validate_human_ledger(ledger: str) -> None:
    """校验 Markdown 只保留且完整解释高风险合并缝。"""

    review_section = ledger_subsection(ledger, "### 3.5.2", "### 3.5.3")
    missing = sorted(path for path in HUMAN_REVIEW_SEAMS if path not in review_section)
    if missing:
        raise RuntimeError(f"§3.5.2 缺少高风险人工复核缝：{missing}")
    for path in HUMAN_REVIEW_SEAMS:
        if not (ROOT / path).is_file():
            raise RuntimeError(f"§3.5.2 高风险合并缝不是普通文件：{path}")


def current_upstream_merge_entries(
    surface: list[str],
    upstream_commit: str,
    post_upstream_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    """生成 upstream 合并时点的 Codex 出站 overlay 精确闭集。

    后续候选 Campaign 新增的出站面由独立 successor transition 冻结，不能倒灌并改写
    已经封存的 upstream overlay 台账。
    """

    strict_surface = set(surface) - set(SCOPE_EXCLUSIONS)
    candidates = strict_surface | REQUIRED_REVIEW_TOUCHPOINTS | IDENTITY_BOUNDARY_TOUCHPOINTS
    candidates -= post_upstream_paths or set()
    entries: list[dict[str, object]] = []
    for path in sorted(candidates):
        if not (ROOT / path).is_file():
            raise RuntimeError(f"机器合并台账候选不是普通文件：{path}")
        if not git_path_differs(upstream_commit, path):
            continue
        scopes: list[str] = []
        if path in strict_surface:
            scopes.append("strict_surface")
        if path in REQUIRED_REVIEW_TOUCHPOINTS:
            scopes.append("required_review_touchpoint")
        if path in IDENTITY_BOUNDARY_TOUCHPOINTS:
            scopes.append("identity_boundary")
        entries.append(
            {
                "path": path,
                "source": "upstream" if git_path_exists(upstream_commit, path) else "fork",
                "scopes": scopes,
            }
        )
    return entries


def upstream_merge_ledger_payload(
    entries: list[dict[str, object]],
    plan: UpstreamMergePlan,
) -> dict[str, object]:
    """构造可精确重放的 upstream overlay 台账。"""

    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "schema_version": "upstream-egress-merge-ledger/v1",
        "upstream_tag": plan.upstream_tag,
        "upstream_commit": plan.upstream_commit,
        "generation_rule": "strict_surface ∪ required_review_touchpoint ∪ identity_boundary 中相对 upstream 有差异的普通文件",
        "overlay_count": len(entries),
        "overlay_sha256": sha256(canonical),
        "overlays": entries,
    }


def validate_upstream_merge_ledger(
    ledger_path: pathlib.Path,
    expected: dict[str, object],
    upstream_tag: str,
) -> None:
    """要求结构化台账与当前工作树复算结果逐字段一致。"""

    try:
        actual = json.loads(
            ledger_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"无法读取 {upstream_tag} 机器合并台账；请使用同一 UpstreamMergePlan 运行 "
            "check_ledger_completeness.py --write-upstream-merge-ledger："
            f"{exc}"
        ) from exc
    if actual != expected:
        raise RuntimeError(
            f"{upstream_tag} 机器合并台账与当前源码 overlay 不一致；"
            "必须为新计划生成新路径，禁止覆盖历史台账"
        )


def write_json_once(path: pathlib.Path, payload: dict[str, object]) -> None:
    """只允许写入新台账，历史计划的输出不得原位覆盖。"""

    if path.exists() or path.is_symlink():
        raise RuntimeError(f"台账输出已存在，禁止覆盖：{path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RuntimeError(f"台账输出父目录不存在或不可信：{path.parent}")
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(f"台账输出已存在，禁止覆盖：{path}") from exc


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def changeset6_additions(inventory_raw: bytes) -> list[dict[str, str]]:
    """读取变更集 6 增量面；变更集 5 的 52 面历史清单保持只读。"""

    try:
        transition = json.loads(CHANGESET6_TRANSITION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取变更集 6 出站面 transition：{exc}") from exc
    if (
        transition.get("schema_version") != "changeset6-egress-surface-transition/v1"
        or transition.get("changeset") != "6"
        or transition.get("base_inventory_path")
        != "docs/changeset5/egress-surface-inventory.json"
        or transition.get("base_inventory_sha256") != sha256(inventory_raw)
        or transition.get("removals") != []
    ):
        raise RuntimeError("变更集 6 出站面 transition schema、基线绑定或 removals 非法")
    additions = transition.get("additions")
    if not isinstance(additions, list) or transition.get("addition_count") != len(additions):
        raise RuntimeError("变更集 6 出站面 transition additions 或计数非法")
    paths = [item.get("path") for item in additions if isinstance(item, dict)]
    if len(paths) != len(additions) or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("变更集 6 出站面 transition 路径为空、未排序或重复")
    for item in additions:
        if item.get("file_type") != "regular" or not item.get("reason"):
            raise RuntimeError(f"变更集 6 出站面 transition 条目非法：{item!r}")
    if transition.get("resulting_surface_count") != 52 + len(additions):
        raise RuntimeError("变更集 6 出站面 transition 结果计数非法")
    return additions


def candidate_surface_successor_identity(document: dict[str, object]) -> str:
    """复算候选出站面后继 transition 排除自摘要后的规范身份。"""

    payload = {key: value for key, value in document.items() if key != "identity_sha256"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    return sha256(canonical)


def validate_candidate_surface_successor(
    transition: dict[str, object],
    inventory_raw: bytes,
    changeset6_raw: bytes,
    candidate_gate_raw: bytes,
) -> list[dict[str, str]]:
    """验证追加式候选出站面登记，历史清单与变更集 6 原文保持只读。"""

    expected_fields = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "scope",
        "predecessor_transition",
        "candidate_gate_transition",
        "base_inventory",
        "additions",
        "removals",
        "resulting_surface_count",
        "implementation_transitions",
        "safety",
        "result",
        "identity_sha256",
    }
    if set(transition) != expected_fields:
        raise RuntimeError("候选出站面后继 transition 顶层字段不闭合")
    if (
        transition.get("schema_version")
        != "official-client-codex-0.149.1-egress-surface-successor-transition/v1"
        or transition.get("issued_at_utc") != "2026-08-26T11:50:00Z"
        or transition.get("base_commit")
        != "580ac615c759170cfb745e7b71fa02a9e1c3f12e"
        or transition.get("scope") != "codex-0.149.1-egress-surface-successor"
        or transition.get("result") != "candidate_surface_successor_frozen"
    ):
        raise RuntimeError("候选出站面后继 transition 顶层事实非法")
    if transition.get("identity_sha256") != candidate_surface_successor_identity(
        transition
    ):
        raise RuntimeError("候选出站面后继 transition 自摘要漂移")

    expected_predecessor = {
        "path": "docs/egress/validation/egress-surface-transition.json",
        "file_sha256": sha256(changeset6_raw),
    }
    if transition.get("predecessor_transition") != expected_predecessor:
        raise RuntimeError("候选出站面后继 transition 未精确绑定变更集 6 原文")

    try:
        candidate_gate = json.loads(
            candidate_gate_raw,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取候选门禁 transition：{exc}") from exc
    expected_candidate_gate = {
        "path": (
            "docs/egress/maintenance/"
            "codex-0.149.1-candidate-gate-successor-transition.json"
        ),
        "file_sha256": sha256(candidate_gate_raw),
        "identity_sha256": candidate_gate.get("identity_sha256"),
    }
    if transition.get("candidate_gate_transition") != expected_candidate_gate:
        raise RuntimeError("候选出站面后继 transition 未精确绑定候选门禁后继")

    expected_inventory = {
        "path": "docs/egress/consolidation/egress-surface-inventory.json",
        "sha256": sha256(inventory_raw),
        "surface_count": 52,
    }
    if transition.get("base_inventory") != expected_inventory:
        raise RuntimeError("候选出站面后继 transition 基线清单绑定非法")

    additions = transition.get("additions")
    if not isinstance(additions, list) or additions != [
        {
            "path": "backend/internal/officialegress/routing_hint.go",
            "file_type": "regular",
            "sha256": "80626878919a1a06f54361da972efab2db4e3750babb90053ece3f2bf6c71282",
            "reason": "候选画像新增官方 routing hint 定型面，必须追加登记到 Codex/OpenAI 出站闭集。",
        }
    ]:
        raise RuntimeError("候选出站面后继 transition additions 非法")
    if transition.get("removals") != [] or transition.get("resulting_surface_count") != 54:
        raise RuntimeError("候选出站面后继 transition 禁止移除历史路径且结果计数必须为 54")

    implementation_transitions = transition.get("implementation_transitions")
    expected_implementations = [
        (
            "backend/cmd/egressruntimedump/main.go",
            "modified",
            ["6e02a1a8b937a50b761b2031630994ace2267372d99e52db648283a946c5e8b1"],
        ),
        (
            "backend/internal/officialegress/"
            "codex_01491_candidate_source_transition_test.go",
            "modified",
            ["115eff32cfdb74f097f657b74fcf5c9c251f84850beeb59ac738329f3a6db46e"],
        ),
        (
            "backend/internal/officialegress/runtime_catalog_files.go",
            "modified",
            ["67751e305d14e0c9529d0e7316da14f9348096dbb99cceedb5dada89fcd8f311"],
        ),
        (
            "backend/internal/officialegress/runtime_catalog_files_test.go",
            "modified",
            ["38c6a15738edd545e64bdb62d6d0ddf6bf2fafc42a4b3be2f91dda8a73a37058"],
        ),
        (
            "backend/internal/officialegress/"
            "upstream_merge_framework_transition_test.go",
            "modified",
            ["86c64d4418bce2f8a54aca4cc965f37d5bff5a5cb9c0448e1269e82ad4df144c"],
        ),
        (
            "backend/internal/service/"
            "codex_01491_candidate_source_transition_test.go",
            "modified",
            ["e3458abc3bdd7a3b285819c0a4c7b8313665262da9b49fbf34ed08aaf029dd28"],
        ),
        (
            "tools/check_ledger_completeness.py",
            "modified",
            ["3e650b9d07982ff96f1c90f6fcf70b4ba9264df7183b0ef91edfcc3cbd1cf375"],
        ),
        (
            "tools/official_client_capture/tests/"
            "test_codex_01491_candidate_gate_successor_transition.py",
            "modified",
            ["d9adc78a868492110d0c277731f617ac1d836aa74295ea534a1317c5c1f0121d"],
        ),
        (
            "tools/official_client_capture/tests/"
            "test_codex_01491_egress_surface_successor_transition.py",
            "added",
            [],
        ),
    ]
    if not isinstance(implementation_transitions, list) or len(
        implementation_transitions
    ) != len(expected_implementations):
        raise RuntimeError("候选出站面后继 transition 实现路径数量非法")
    for entry, (expected_path, expected_change, expected_predecessors) in zip(
        implementation_transitions,
        expected_implementations,
        strict=True,
    ):
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "change",
            "predecessor_sha256s",
            "to_sha256",
            "reason",
        }:
            raise RuntimeError("候选出站面后继 transition 实现条目字段非法")
        implementation_path = ROOT / expected_path
        current_digest = (
            sha256(implementation_path.read_bytes())
            if implementation_path.is_file()
            else ""
        )
        if (
            entry.get("path") != expected_path
            or entry.get("change") != expected_change
            or entry.get("predecessor_sha256s") != expected_predecessors
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
            or implementation_path.is_symlink()
            or not implementation_path.is_file()
            or (
                entry.get("to_sha256") != current_digest
                and not r4_catalog.transition_chain_supersedes(
                    expected_path,
                    entry.get("to_sha256", ""),
                    current_digest,
                )
            )
        ):
            raise RuntimeError(
                f"候选出站面后继 transition 实现摘要非法：{expected_path}"
            )
    if transition.get("safety") != {
        "active_remained_0_147_0": True,
        "arm64_accessed": False,
        "deployment_performed": False,
        "historical_inventory_modified": False,
        "historical_transition_modified": False,
        "vircs_accessed": False,
    }:
        raise RuntimeError("候选出站面后继 transition 安全边界非法")

    addition = additions[0]
    path = ROOT / addition["path"]
    if (
        path.is_symlink()
        or not path.is_file()
        or sha256(path.read_bytes()) != addition["sha256"]
    ):
        raise RuntimeError("候选新增出站面文件类型或摘要漂移")
    return [
        {
            "path": addition["path"],
            "file_type": addition["file_type"],
            "reason": addition["reason"],
        }
    ]


def candidate_surface_additions(inventory_raw: bytes) -> list[dict[str, str]]:
    """读取并验证候选出站面追加登记。"""

    try:
        changeset6_raw = CHANGESET6_TRANSITION.read_bytes()
        candidate_gate_raw = CANDIDATE_GATE_TRANSITION.read_bytes()
        transition = json.loads(
            CANDIDATE_SURFACE_SUCCESSOR.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取候选出站面后继 transition：{exc}") from exc
    return validate_candidate_surface_successor(
        transition,
        inventory_raw,
        changeset6_raw,
        candidate_gate_raw,
    )


def maintenance_removals(frozen_paths: set[str]) -> list[str]:
    """读取本次退休收据，只允许删除收据绑定且确已不存在的历史出站面。"""

    raw = MAINTENANCE_RETIREMENT.read_bytes()
    if sha256(raw) != MAINTENANCE_RETIREMENT_SHA256:
        raise RuntimeError("官方出站维护退休收据摘要漂移")
    receipt = json.loads(raw)
    if receipt.get("schema_version") != "official-egress-consolidation-retirement/v1":
        raise RuntimeError("官方出站维护退休收据 schema 非法")
    retired = receipt.get("retired_production_sources")
    if not isinstance(retired, list):
        raise RuntimeError("官方出站维护退休收据缺少生产源码清单")
    removals = sorted(
        item.get("path")
        for item in retired
        if isinstance(item, dict) and item.get("path") in frozen_paths
    )
    expected = ["backend/internal/service/official_egress_legacy_dispatch.go"]
    if removals != expected:
        raise RuntimeError(f"维护出站面退休集合漂移：{removals!r}")
    for path in removals:
        if (ROOT / path).exists():
            raise RuntimeError(f"已退休出站面仍存在：{path}")
    return removals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-merge-plan",
        required=True,
        type=pathlib.Path,
        help="不可变 UpstreamMergePlan；目标 tag、commit 和台账输出位置只从此处读取",
    )
    writes = parser.add_mutually_exclusive_group()
    writes.add_argument(
        "--write-inventory",
        action="store_true",
        help="按当前受审扫描结果写入变更集 5 的结构化完整性清单",
    )
    writes.add_argument(
        "--write-upstream-merge-ledger",
        action="store_true",
        help="按计划目标写入新的结构化 Codex 出站 overlay 台账；禁止覆盖已有路径",
    )
    args = parser.parse_args()

    ledger = ledger_text()
    surface = scan_surface_files()

    try:
        plan = load_upstream_merge_plan(args.upstream_merge_plan.resolve())
        validate_human_ledger(ledger)
        candidate_successor_paths = {
            item["path"]
            for item in candidate_surface_additions(INVENTORY.read_bytes())
        }
        upstream_entries = current_upstream_merge_entries(
            surface,
            plan.upstream_commit,
            candidate_successor_paths,
        )
        upstream_payload = upstream_merge_ledger_payload(upstream_entries, plan)
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1

    if args.write_upstream_merge_ledger:
        try:
            write_json_once(plan.ledger_path, upstream_payload)
        except RuntimeError as exc:
            print(f"🔴 {exc}", file=sys.stderr)
            return 1
        print(
            f"✅ 已按计划 {plan.plan_id} 写入 {len(upstream_entries)} 个 "
            f"{plan.upstream_tag} 出站 overlay：{plan.ledger_path}"
        )
        return 0

    try:
        validate_upstream_merge_ledger(
            plan.ledger_path,
            upstream_payload,
            plan.upstream_tag,
        )
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1

    if args.write_inventory:
        authoritative = [rel for rel in surface if rel not in SCOPE_EXCLUSIONS]
        payload = {
            "schema_version": "changeset5-egress-surface-inventory/v1",
            "changeset": "5",
            "classification_upstream_base": "26d894ef4f50645a4bf1030e378ac892f17d0223",
            "observed_remote_head": "825ca7b1fc9335f904bc077f051de815fb61e47f",
            "surface_count": len(authoritative),
            "surfaces": [
                {"path": rel, "file_type": "regular"} for rel in authoritative
            ],
            "exclusions": [
                {"path": rel, "reason": reason}
                for rel, reason in sorted(SCOPE_EXCLUSIONS.items())
            ],
        }
        INVENTORY.parent.mkdir(parents=True, exist_ok=True)
        INVENTORY.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"✅ 已写入 {len(authoritative)} 面结构化清单：{INVENTORY}")
        return 0

    try:
        inventory_raw = INVENTORY.read_bytes()
        payload = json.loads(inventory_raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"🔴 无法读取结构化出站面清单：{exc}", file=sys.stderr)
        return 1
    if payload.get("schema_version") != "changeset5-egress-surface-inventory/v1":
        print("🔴 结构化出站面清单 schema 非法", file=sys.stderr)
        return 1
    declared = payload.get("surfaces")
    exclusions = payload.get("exclusions")
    if not isinstance(declared, list) or not isinstance(exclusions, list):
        print("🔴 结构化出站面清单缺少 surfaces/exclusions 数组", file=sys.stderr)
        return 1
    declared_paths = [item.get("path") for item in declared if isinstance(item, dict)]
    exclusion_paths = [item.get("path") for item in exclusions if isinstance(item, dict)]
    if (
        len(declared_paths) != len(declared)
        or len(exclusion_paths) != len(exclusions)
        or len(set(declared_paths + exclusion_paths)) != len(declared_paths) + len(exclusion_paths)
    ):
        print("🔴 结构化出站面清单存在空路径或重复路径", file=sys.stderr)
        return 1
    if payload.get("surface_count") != 52 or len(declared_paths) != 52:
        print("🔴 变更集 5 的完整出站面必须严格为 52 项", file=sys.stderr)
        return 1
    try:
        changeset6 = changeset6_additions(inventory_raw)
        candidate_additions = candidate_surface_additions(inventory_raw)
        additions = changeset6 + candidate_additions
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1
    addition_paths = [item["path"] for item in additions]
    if set(addition_paths) & (set(declared_paths) | set(exclusion_paths)):
        print("🔴 变更集 6 出站面 transition 与历史清单重复", file=sys.stderr)
        return 1
    declared.extend(additions)
    declared_paths.extend(addition_paths)
    try:
        removal_paths = maintenance_removals(set(declared_paths))
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1
    declared = [item for item in declared if item["path"] not in set(removal_paths)]
    declared_paths = [path for path in declared_paths if path not in set(removal_paths)]
    if set(exclusion_paths) != set(SCOPE_EXCLUSIONS):
        print("🔴 工具自引用排除项与源码门禁不一致", file=sys.stderr)
        return 1
    scanned = set(surface)
    frozen = set(declared_paths) | set(exclusion_paths)
    if scanned != frozen:
        for rel in sorted(scanned - frozen):
            print(f"🔴 出现未登记的新出站定型文件：{rel}", file=sys.stderr)
        for rel in sorted(frozen - scanned):
            print(f"🔴 已冻结出站定型文件不再命中扫描：{rel}", file=sys.stderr)
        return 1
    for item in declared:
        rel = item["path"]
        path = ROOT / rel
        if item.get("file_type") != "regular" or not path.is_file() or path.is_symlink():
            print(f"🔴 出站定型路径不是已登记的普通文件：{rel}", file=sys.stderr)
            return 1

    stale = [rel for rel in SCOPE_EXCLUSIONS if rel not in surface]

    if stale:
        for rel in stale:
            print(
                f"🔴 {rel} 已不在出站定型面上，应从 SCOPE_EXCLUSIONS 删除",
                file=sys.stderr,
            )
        return 1

    covered = len(declared_paths)
    print(
        f"✅ §3.5 台账完整：变更集 5 冻结 52 面 + 变更集 6 增量 {len(changeset6)} 面"
        f" + 候选后继增量 {len(candidate_additions)} 面"
        f" - 维护退休 {len(removal_paths)} 面 = {covered} 个出站定型文件全部登记"
        f"；{plan.upstream_tag} 机器 overlay {len(upstream_entries)} 个文件逐项一致"
        f"（plan={plan.plan_id}）"
        f"；人工只保留 {len(HUMAN_REVIEW_SEAMS)} 个高风险合并缝"
        + (f"，另有 {len(SCOPE_EXCLUSIONS)} 个工具自引用排除" if SCOPE_EXCLUSIONS else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
