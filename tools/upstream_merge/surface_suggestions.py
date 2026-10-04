"""U-2 新增路由的 Inventory 条目建议（UM-22）：surface-scan 之后按路由快照起草别名与调用方。

v0.2.13 合并时两条新增路由（System One 原生入口、Claude 重置额度兑换）的 Inventory 条目全靠人工翻源码：
别名照路径起名，调用方要找到处理函数与注册函数，还要判断并入哪个已有条目。本模块只给非权威建议，写到
``inputs/surface-suggestions-revision-NNN.json``，不改任何证据制品；SurfaceDecision 仍由人工决定：

* 别名：``alias-<分组变量>-<路径段（去掉 :参数）>-<方法>``，如 ``alias-accounts-claude-reset-credits-redeem-post``；
* 调用方：注册函数（``server.routes.registerAccountRoutes``）与注册行里的处理函数
  （``handler.Admin.Account.RedeemClaudeResetCredit``，从候选提交的源码按行号读出）；
* 已有条目：在当前 Inventory 里找调用方含同一注册函数、别名词重合最多的条目（最多 3 个），没有就建议新建。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .canonical import expect_object, load_json, resolve_within
from .contracts import CLIENT_KEYS, LoadedPlan, latest_inventory_path
from .errors import UpstreamMergeError
from .gitops import run_git
from .plan_inputs import plan_inputs_root, write_json_input

SUGGESTIONS_SCHEMA = "official-egress-upstream-surface-inventory-suggestions/v1"
ROUTE_CALL_RE = re.compile(
    r"\.(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any|Handle)\(\s*\"[^\"]*\"\s*,\s*(?P<handlers>.+?)\)\s*$"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def route_alias(row: dict[str, Any]) -> str:
    segments = [row["receiver"]] + [part for part in row["path"].split("/") if part and not part.startswith((":", "*"))]
    return "alias-" + "-".join(filter(None, (_slug(part) for part in segments))) + f"-{row['method'].lower()}"


def registration_caller(row: dict[str, Any]) -> str:
    package = row["file"].removeprefix("backend/internal/").rsplit("/", 1)[0].replace("/", ".")
    return f"{package}.{row['function']}"


def handler_caller(source_text: str, line_hint: int) -> str | None:
    """注册行最后一个参数即处理函数，如 h.Gateway.SystemOne → handler.Gateway.SystemOne；解析不了返回 None。"""

    lines = source_text.splitlines()
    if not 1 <= line_hint <= len(lines):
        return None
    match = ROUTE_CALL_RE.search(lines[line_hint - 1].strip())
    if match is None:
        return None
    parts = match.group("handlers").split(",")[-1].strip().split(".")
    if len(parts) < 2 or not all(part.isidentifier() for part in parts):
        return None
    return "handler." + ".".join(parts[1:])


def suggest_inventory_entries(
    deltas: Sequence[dict[str, Any]],
    route_rows: Sequence[dict[str, Any]],
    read_source: Any,
    inventories: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """对每个新增路由差异给出别名、调用方与可并入的已有条目；read_source(文件) 返回候选提交里的源码文本。"""

    rows = {row["route_fingerprint"]: row for row in route_rows}
    suggestions: list[dict[str, Any]] = []
    for delta in deltas:
        if delta.get("surface") != "route" or delta.get("change") != "added":
            continue
        row = rows.get(delta.get("identity"))
        if row is None:
            continue
        alias = route_alias(row)
        registration = registration_caller(row)
        try:
            handler = handler_caller(read_source(row["file"]), int(row["line_hint"]))
        except (UpstreamMergeError, OSError, ValueError):
            handler = None
        tokens = set(alias.split("-")) - {"alias", row["method"].lower(), row["receiver"].lower()}
        scored: set[tuple[int, str, str]] = set()
        for client in delta.get("clients") or []:
            for entry in inventories.get(client, []):
                if registration not in (entry.get("caller_ids") or []):
                    continue
                entry_tokens = set("-".join(entry.get("physical_alias_ids") or []).split("-"))
                shared = len(tokens & entry_tokens)
                if shared:
                    scored.add((shared, client, str(entry.get("logical_ingress_id"))))
        candidates = sorted(scored, key=lambda item: (-item[0], item[1], item[2]))[:3]
        suggestions.append(
            {
                "delta_id": delta["delta_id"],
                "clients": list(delta.get("clients") or []),
                "method": row["method"],
                "path": row["path"],
                "receiver": row["receiver"],
                "file": row["file"],
                "line_hint": row["line_hint"],
                "suggested_physical_alias_id": alias,
                "suggested_caller_ids": sorted(filter(None, {handler, registration})),
                "existing_entry_candidates": [
                    {"client": client, "logical_ingress_id": logical, "shared_alias_tokens": shared}
                    for shared, client, logical in candidates
                ],
                "new_logical_ingress_id": None if candidates else alias.removeprefix("alias-").rsplit("-", 1)[0],
            }
        )
    return suggestions


def _current_inventories(plan: LoadedPlan) -> dict[str, list[dict[str, Any]]]:
    """当前候选 Inventory（已有修订）或 Plan 基线登记的生产 Inventory；读不到的客户端跳过。"""

    result: dict[str, list[dict[str, Any]]] = {}
    outputs = plan.document.get("outputs", {}).get("candidate_inventories", {})
    baselines = plan.document.get("baselines", {}).get("production_ingress_inventory", {})
    for client in CLIENT_KEYS:
        path: Path | None = None
        fallback = (outputs.get(client) or {}).get("ingress")
        if isinstance(fallback, str):
            candidate = latest_inventory_path(plan, client, "ingress", fallback=fallback)
            path = candidate if candidate.is_file() else None
        if path is None and isinstance((baselines.get(client) or {}).get("path"), str):
            baseline = Path(baselines[client]["path"])
            path = baseline if baseline.is_file() else None
        if path is not None:
            document = expect_object(load_json(path, f"{client} ingress inventory"), f"{client} ingress inventory")
            result[client] = [item for item in document.get("entries") or [] if isinstance(item, dict)]
    return result


def write_surface_suggestions(plan: LoadedPlan, delta_document: dict[str, Any]) -> str | None:
    """surface-scan 之后写出建议文件并返回路径；没有新增路由时返回 None。"""

    binding = expect_object(delta_document.get("candidate_route_snapshot"), "SurfaceDelta.candidate_route_snapshot")
    route = load_json(resolve_within(plan.evidence_root, binding["path"], "candidate route snapshot"), "route snapshot")
    source_commit = route.get("source_commit")

    def read_source(relative: str) -> str:
        return run_git(plan.repository_root, "show", f"{source_commit}:{relative}").stdout

    suggestions = suggest_inventory_entries(
        delta_document.get("deltas") or [],
        route.get("entries") or [],
        read_source,
        _current_inventories(plan),
    )
    if not suggestions:
        return None
    revision = delta_document.get("revision") or 1
    path = plan_inputs_root(plan) / f"surface-suggestions-revision-{int(revision):03d}.json"
    write_json_input(
        path,
        {
            "schema_version": SUGGESTIONS_SCHEMA,
            "plan_id": plan.plan_id,
            "source_commit": source_commit,
            "note": "非权威建议：别名与调用方按路由快照与源码起草，已有条目按调用方与别名词重合度排序；SurfaceDecision 仍由人工决定",
            "route_suggestions": suggestions,
        },
        "Inventory 条目建议",
    )
    return str(path)
