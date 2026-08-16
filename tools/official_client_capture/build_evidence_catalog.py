#!/usr/bin/env python3
"""ACC-07 证据编目器：按冻结声明把多 job 根编目成断言证据包的三份计划。

manifest 覆盖不全、标签语义与画像 selector 错位的根因是
capture manifest 一直由执行者临时手写。本工具把编目变成确定性推导：

输入
    - 冻结的证据标签声明（`codex_upgrade_evidence_labels_*.json`）：按
      (job_id, glob) 声明 scenario_ids、kind、parser、labels 与可选的派生目标；
    - attempt 的 job → 证据根映射（来自 campaign.json 与 attempt.json）。

输出
    - bundle plan（ACC-02 收口清单）；
    - derivation plan（ACC-02b 派生清单）；
    - capture manifest 草案（artifact 的 path／kind／parser／scenario_ids／labels，
      sha256 由收口后回填）。

**本工具只做路径匹配，不打开任何证据文件**：标签一律来自声明。这条边界是判据独立性
的前提——若标签由被它选中的判据所验证的属性反推（例如看到 `content-encoding: zstd`
就贴 `compression=zstd`，而判据正是验 zstd），判据即退化为同义反复。声明本身要求
每条规则给出 `rationale`，说明该标签如何由采集参数或场景 precondition 得出。

同一原始文件在 manifest 中二选一：直接以原生 parser 解析，或以 `opaque_bound_source`
登记并由派生器产出结构化观测（声明中的 `derive`）。该互斥由 ACC-03 seal 门禁强制。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.build_assertion_bundle import (  # noqa: E402
    AssertionBundleError,
    validate_relative_path,
)

LABELS_SCHEMA = "codex-upgrade-evidence-labels/v1"
CAPTURE_MANIFEST_SCHEMA = "codex-candidate-capture-manifest/v1"
DERIVED_PREFIX = "derived/"

SIDES = frozenset({"official", "candidate"})
# 与 derive_official_observations.ALLOWED_TARGET_KINDS 一致；一致性由离线测试锁定。
DERIVABLE_KINDS = frozenset({"process_trace", "websocket_trace"})
DERIVE_PARSERS = frozenset({"h1_request_stream", "mitm_http_jsonl", "h1_wire_probe"})


class EvidenceCatalogError(RuntimeError):
    """编目失败，必须失败关闭。"""


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCatalogError(f"{label} 必须是非空字符串")
    return value


def load_label_declaration(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvidenceCatalogError(f"无法读取证据标签声明：{error}") from error
    if not isinstance(document, dict):
        raise EvidenceCatalogError("证据标签声明必须是 JSON 对象")
    if document.get("schema_version") != LABELS_SCHEMA:
        raise EvidenceCatalogError(
            f"证据标签声明 schema_version 必须是 {LABELS_SCHEMA}"
        )
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise EvidenceCatalogError("证据标签声明 entries 必须非空")
    seen_jobs: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EvidenceCatalogError("entries 项必须是对象")
        job_id = _require_str(entry.get("job_id"), "job_id")
        if job_id in seen_jobs:
            raise EvidenceCatalogError(f"job 重复声明：{job_id}")
        seen_jobs.add(job_id)
        if entry.get("side") not in SIDES:
            raise EvidenceCatalogError(f"job {job_id} 的 side 非法")
        rules = entry.get("rules")
        if not isinstance(rules, list) or not rules:
            raise EvidenceCatalogError(f"job {job_id} 的 rules 必须非空")
        seen_globs: set[tuple[str, str]] = set()
        for rule in rules:
            _validate_rule(rule, job_id, seen_globs)
    return document


def _validate_rule(
    rule: Any, job_id: str, seen_globs: set[tuple[str, str]]
) -> None:
    if not isinstance(rule, dict):
        raise EvidenceCatalogError(f"job {job_id} 的 rule 必须是对象")
    allowed = {
        "glob",
        "root_suffix",
        "scenario_ids",
        "kind",
        "parser",
        "labels",
        "frame_labels",
        "derive",
        "rationale",
    }
    if set(rule) - allowed:
        raise EvidenceCatalogError(
            f"job {job_id} 的 rule 含未知字段：{sorted(set(rule) - allowed)}"
        )
    glob = _require_str(rule.get("glob"), f"job {job_id} 的 glob")
    if glob.startswith("/") or ".." in glob.split("/"):
        raise EvidenceCatalogError(f"job {job_id} 的 glob 存在路径逃逸：{glob}")
    # 一个 job 可绑定多个证据根（如 official-wham-safe 的 get／safe 双根），
    # root_suffix 按根目录名后缀限定本规则生效范围；省略则对该 job 全部根生效。
    root_suffix = rule.get("root_suffix")
    if root_suffix is not None:
        _require_str(root_suffix, f"job {job_id} 的 root_suffix")
    derive_kind = (rule.get("derive") or {}).get("kind", "")
    key = (root_suffix or "", glob, rule.get("kind", ""), derive_kind,
           ",".join(sorted(rule.get("scenario_ids") or [])))
    if key in seen_globs:
        raise EvidenceCatalogError(
            f"job {job_id} 的 glob 在同一 root_suffix／kind／场景组合下重复：{glob}"
        )
    seen_globs.add(key)
    scenario_ids = rule.get("scenario_ids")
    if (
        not isinstance(scenario_ids, list)
        or not scenario_ids
        or any(not isinstance(item, str) or not item for item in scenario_ids)
        or len(scenario_ids) != len(set(scenario_ids))
    ):
        raise EvidenceCatalogError(f"job {job_id} 的 scenario_ids 非法：{glob}")
    _require_str(rule.get("kind"), f"job {job_id} 的 kind")
    _require_str(rule.get("parser"), f"job {job_id} 的 parser")
    labels = rule.get("labels")
    if (
        not isinstance(labels, dict)
        or not labels
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items())
    ):
        raise EvidenceCatalogError(f"job {job_id} 的 labels 必须是非空字符串映射：{glob}")
    _require_str(rule.get("rationale"), f"job {job_id} 的 rationale")
    derive = rule.get("derive")
    if derive is not None:
        if not isinstance(derive, dict) or set(derive) != {"parser", "kind"}:
            raise EvidenceCatalogError(f"job {job_id} 的 derive 字段不闭合：{glob}")
        if derive["parser"] not in DERIVE_PARSERS:
            raise EvidenceCatalogError(
                f"job {job_id} 的 derive.parser 非法：{derive['parser']}"
            )
        if derive["kind"] not in DERIVABLE_KINDS:
            raise EvidenceCatalogError(
                f"job {job_id} 的 derive.kind 非法：{derive['kind']}"
            )
        if rule["parser"] != "opaque_bound_source":
            raise EvidenceCatalogError(
                f"job {job_id} 声明派生时原件 parser 必须是 opaque_bound_source：{glob}"
            )
    frame_labels = rule.get("frame_labels")
    if frame_labels is not None:
        # 帧级标签只在能产出 websocket_frame 观测的规则上有意义：
        # 派生 websocket_trace，或直接以 h1_request_stream 解析。
        if (derive or {}).get("kind") != "websocket_trace" and rule.get(
            "parser"
        ) != "h1_request_stream":
            raise EvidenceCatalogError(
                f"job {job_id} 的 frame_labels 仅支持派生 websocket_trace "
                f"或 h1_request_stream 解析：{glob}"
            )
        if not isinstance(frame_labels, dict) or not frame_labels:
            raise EvidenceCatalogError(
                f"job {job_id} 的 frame_labels 必须是非空对象：{glob}"
            )
        labels = rule["labels"]
        for frame_index, values in frame_labels.items():
            if not isinstance(frame_index, str) or not frame_index.isdigit():
                raise EvidenceCatalogError(
                    f"job {job_id} 的 frame_labels 索引非法：{frame_index!r}"
                )
            if (
                not isinstance(values, dict)
                or not values
                or any(
                    not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                    for k, v in values.items()
                )
            ):
                raise EvidenceCatalogError(
                    f"job {job_id} 的 frame_labels[{frame_index!r}] "
                    f"必须是非空字符串映射：{glob}"
                )
            conflicts = sorted(set(labels) & set(values))
            if conflicts:
                raise EvidenceCatalogError(
                    f"job {job_id} 的 frame_labels[{frame_index!r}] "
                    f"不能覆盖 labels：{conflicts}"
                )


def _relative_files(root: Path) -> list[str]:
    """枚举证据根内的普通文件相对路径；只看目录项，不读内容。"""

    if root.is_symlink() or not root.is_dir():
        raise EvidenceCatalogError(f"证据根不存在或不可信：{root}")
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def build_catalog(
    declaration: Mapping[str, Any],
    job_roots: Mapping[str, list[tuple[str, Path]]],
    *,
    side: str,
) -> dict[str, Any]:
    """按声明编目本侧全部 job，返回三份计划。

    `job_roots` 形如 {job_id: [(inventory_prefix, root_path), ...]}；prefix 必须等于
    封存 inventory 的逻辑前缀，收口 provenance 据此绑定来源。一个 job 可有多个根
    （如 official-wham-safe 的 get／safe 双根），规则用 root_suffix 限定生效范围。
    """

    if side not in SIDES:
        raise EvidenceCatalogError(f"未知验收侧：{side}")
    by_job = {
        entry["job_id"]: entry
        for entry in declaration["entries"]
        if entry["side"] == side
    }
    missing = sorted(set(job_roots) - set(by_job))
    if missing:
        raise EvidenceCatalogError(
            f"{side} 侧以下 job 缺少证据标签声明，无法编目：{missing}"
        )

    bundle_entries: list[dict[str, str]] = []
    derive_entries: list[dict[str, str]] = []
    artifacts: list[dict[str, Any]] = []
    claimed_targets: set[str] = set()

    for job_id in sorted(job_roots):
        matched_any = False
        for prefix, root in job_roots[job_id]:
            available = _relative_files(root)
            root_name = root.resolve(strict=False).name
            applicable = [
                rule
                for rule in by_job[job_id]["rules"]
                if not rule.get("root_suffix")
                or root_name.endswith(rule["root_suffix"])
            ]
            if not applicable:
                raise EvidenceCatalogError(
                    f"job {job_id} 的证据根 {root_name} 没有任何适用声明规则"
                )
            for rule in applicable:
                glob = rule["glob"]
                hits = sorted(
                    name for name in available if fnmatch.fnmatch(name, glob)
                )
                if not hits:
                    raise EvidenceCatalogError(
                        f"job {job_id} 根 {root_name} 的声明 glob 未命中任何证据，"
                        f"编目不完整：{glob}"
                    )
                matched_any = True
                for relative in hits:
                    target = f"{prefix}/{relative}"
                    validate_relative_path(target, f"job {job_id} 的收口目标")
                    if target in claimed_targets:
                        # 同一原件被多条规则引用（不同场景／kind）：只收口一次，
                        # 但把新场景并入已登记 artifact 的 scenario_ids。
                        for existing in artifacts:
                            if existing["path"] == target:
                                merged = sorted(
                                    set(existing["scenario_ids"])
                                    | set(rule["scenario_ids"])
                                )
                                existing["scenario_ids"] = merged
                                break
                    else:
                        claimed_targets.add(target)
                        bundle_entries.append(
                            {"root": prefix, "path": relative, "target": target}
                        )
                        artifacts.append(
                            {
                                "path": target,
                                "kind": rule["kind"],
                                "parser": rule["parser"],
                                "scenario_ids": list(rule["scenario_ids"]),
                                "labels": dict(rule["labels"]),
                            }
                        )
                    derive = rule.get("derive")
                    if derive is None:
                        continue
                    for scenario_id in rule["scenario_ids"]:
                        stem = f"{prefix}_{relative}".replace("/", "_")
                        derived_target = (
                            f"{DERIVED_PREFIX}{scenario_id}/{derive['kind']}/"
                            f"{stem}.observation.jsonl"
                        )
                        if derived_target in claimed_targets:
                            raise EvidenceCatalogError(
                                f"派生目标重复：{derived_target}"
                            )
                        claimed_targets.add(derived_target)
                        derive_entries.append(
                            {
                                "source": target,
                                "parser": derive["parser"],
                                "scenario_id": scenario_id,
                                "kind": derive["kind"],
                                "target": derived_target,
                                "connection_id": Path(relative).stem,
                            }
                        )
                        derived_artifact = {
                            "path": derived_target,
                            "kind": derive["kind"],
                            "parser": "observation_jsonl",
                            "scenario_ids": [scenario_id],
                            "labels": dict(rule["labels"]),
                        }
                        # 帧级标签只挂在产出 websocket_frame 观测的派生 artifact 上，
                        # 由断言加载器按 data.frame_index 叠加到帧事实。
                        if rule.get("frame_labels"):
                            derived_artifact["frame_labels"] = {
                                key: dict(value)
                                for key, value in rule["frame_labels"].items()
                            }
                        artifacts.append(derived_artifact)
        if not matched_any:
            raise EvidenceCatalogError(f"job {job_id} 未编目任何证据")

    artifacts.sort(key=lambda item: item["path"])
    bundle_entries.sort(key=lambda item: item["target"])
    derive_entries.sort(key=lambda item: item["target"])
    return {
        "bundle_plan": {"entries": bundle_entries},
        "derivation_plan": {"entries": derive_entries},
        "manifest_draft": {
            "schema_version": CAPTURE_MANIFEST_SCHEMA,
            "status": "complete",
            "artifacts": artifacts,
        },
    }


def finalize_manifest(
    draft: Mapping[str, Any],
    bundle_dir: Path,
    *,
    codex_version: str,
    capture_id: str,
) -> dict[str, Any]:
    """收口与派生完成后回填 sha256，产出可提交的 capture manifest。"""

    artifacts: list[dict[str, Any]] = []
    for artifact in draft["artifacts"]:
        path = bundle_dir / artifact["path"]
        if path.is_symlink() or not path.is_file():
            raise EvidenceCatalogError(f"manifest 引用的产物不存在：{artifact['path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts.append({**artifact, "sha256": digest})
    return {
        "schema_version": CAPTURE_MANIFEST_SCHEMA,
        "codex_version": codex_version,
        "capture_id": capture_id,
        "status": "complete",
        "artifacts": artifacts,
    }


def declaration_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_job_roots(pairs: Iterable[str]) -> dict[str, list[tuple[str, Path]]]:
    roots: dict[str, list[tuple[str, Path]]] = {}
    seen_prefixes: set[str] = set()
    for pair in pairs:
        parts = pair.split("=", 2)
        if len(parts) != 3:
            raise EvidenceCatalogError(
                f"--job-root 必须是 job_id=inventory_prefix=path 形式：{pair}"
            )
        job_id, prefix, raw_path = parts
        if prefix in seen_prefixes:
            raise EvidenceCatalogError(f"inventory 前缀重复：{prefix}")
        seen_prefixes.add(prefix)
        roots.setdefault(job_id, []).append((prefix, Path(raw_path)))
    if not roots:
        raise EvidenceCatalogError("至少需要一个 --job-root")
    return roots


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按冻结证据标签声明编目断言证据（ACC-07）"
    )
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--side", choices=sorted(SIDES), required=True)
    parser.add_argument(
        "--job-root",
        action="append",
        default=[],
        metavar="JOB=PREFIX=PATH",
        help="job 的证据根；PREFIX 必须等于封存 inventory 的逻辑前缀",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        declaration = load_label_declaration(arguments.declaration)
        catalog = build_catalog(
            declaration,
            parse_job_roots(arguments.job_root),
            side=arguments.side,
        )
        arguments.output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        for name, payload in (
            ("bundle-plan.json", catalog["bundle_plan"]),
            ("derivation-plan.json", catalog["derivation_plan"]),
            ("manifest-draft.json", catalog["manifest_draft"]),
        ):
            path = arguments.output_dir / name
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
    except (EvidenceCatalogError, AssertionBundleError) as error:
        print(f"证据编目失败：{error}", file=sys.stderr)
        return 1
    print(
        f"编目完成：收口 {len(catalog['bundle_plan']['entries'])} 项，"
        f"派生 {len(catalog['derivation_plan']['entries'])} 项，"
        f"artifact {len(catalog['manifest_draft']['artifacts'])} 项，"
        f"声明摘要={declaration_sha256(arguments.declaration)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
