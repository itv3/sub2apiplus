"""上游新增发送点的预先登记补丁（UM-25）：按预检列出的未登记新增发送点，生成可直接 ``git apply`` 的补丁。

v0.2.10、v0.2.13 两次合并前，都要在主干手改几处才能让试合并通过发送面基线检查（每次约 15 分钟）：
``classify.go`` 的分类规则、``post_bootstrap_acceptance.go`` 的承接收据变量与新增发送点登记、
``scanner-algorithm-successor.json`` 的算法后继摘要。本模块一次生成这几处改动：

* 分类规则：只给“没有任何规则命中”的发送点文件补精确到文件的 out-of-scope 规则，追加在规则表末尾——
  没有规则命中它，追加在哪都不会抢走别的文件；已被现有规则分类的发送点不改分类规则。
* 登记条目：每个发送点一条 out-of-scope、``absentBeforeMerge`` 的登记，同一批次同一 mergeGroup，
  证据指向本批次将要生成的冻结承接收据（命名见 ``receipt_tag``）。登记与扫描结果比对的是 persona、
  运行时身份与后端边界，说明文字不比对；已被现有规则分类、但实际不是范围外的发送点，应用补丁后
  重跑预检会报“分类……与审核收据不一致”，那类发送点须人工登记。
* 算法后继：按补丁后的扫描器源码重算算法摘要（与扫描器 ``scannerAlgorithmDigest`` 同一算法），写入
  ``scanner-algorithm-successor.json``，并把承接收据、审阅来源与原因改为本批次。

说明文字先按函数与文件起草，同时写出 notes 文件；要改写说明时改 notes，再用
``sink-registration-draft --notes`` 重新生成补丁（算法摘要随文字重算），不要在应用补丁之后手改源码。
上游新增的发送点几乎都是第三方平台（范围外）；范围内的发送点牵涉官方出站画像，不在本模块起草范围。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .canonical import expect_object, load_json, write_json_once, write_once
from .errors import UpstreamMergeError
from .gitops import assert_private_path

SCANNER_RELATIVE = "backend/cmd/egressscan"
CLASSIFY_RELATIVE = f"{SCANNER_RELATIVE}/classify.go"
ACCEPTANCE_RELATIVE = f"{SCANNER_RELATIVE}/post_bootstrap_acceptance.go"
SUCCESSOR_RELATIVE = "docs/egress/maintenance/scanner-algorithm-successor.json"
NOTES_SCHEMA = "official-egress-upstream-sink-registration-notes/v1"

CANDIDATE_RE = re.compile(r"^(?P<function>[^@#]+)@(?P<file>[^@#]+)#(?P<kind>[a-z0-9_]+)#(?P<ordinal>[0-9]+)$")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DATE_RE = re.compile(r"^\d{8}$")
CLASSIFY_OPEN = "var classifyRules = []classifyRule{\n"
ADDITIONS_OPEN = "var reviewedPostBootstrapSinkAdditions = []postBootstrapSinkAddition{\n"
EVIDENCE_VAR_RE = re.compile(r"^var upstream\w*PendingAdditionsEvidence = \"[^\"\n]+\"\n", re.MULTILINE)
# 登记条目字段与 gofmt 对齐宽度（最长的 absentBeforeMerge: 再空一格）。
FIELD_WIDTH = len("absentBeforeMerge:") + 1


def parse_candidate(identifier: str) -> dict[str, str]:
    """拆开扫描候选 ID：``<包路径>.<函数>@<文件>#<发送类型>#<序号>``。"""

    match = CANDIDATE_RE.fullmatch(identifier)
    if match is None:
        raise UpstreamMergeError(f"无法解析扫描候选 ID：{identifier}")
    qualified = match.group("function").rsplit("/", 1)[-1]
    if "." not in qualified:
        raise UpstreamMergeError(f"扫描候选 ID 缺少函数名：{identifier}")
    return {
        "scan_candidate_id": identifier,
        "function": qualified.split(".", 1)[1],
        "file": match.group("file"),
        "sink_kind": match.group("kind"),
    }


def tag_slug(upstream_tag: str) -> str:
    """v0.2.13 → v0213，与 v0.2.13 合并时的收据与 mergeGroup 命名一致。"""

    if TAG_RE.fullmatch(upstream_tag) is None:
        raise UpstreamMergeError(f"上游 tag 必须形如 v0.2.13：{upstream_tag}")
    return "v" + upstream_tag[1:].replace(".", "")


def receipt_tag(upstream_tag: str, date: str) -> str:
    """本批次冻结承接收据的 --tag；收据路径为 docs/egress/maintenance/upstream-<tag>-freeze-successor.json。"""

    if DATE_RE.fullmatch(date) is None:
        raise UpstreamMergeError(f"日期必须是 YYYYMMDD：{date}")
    return f"{tag_slug(upstream_tag)}-scanner-pending-additions-{date}"


def rule_prefix(file: str) -> str:
    """分类规则按 backend/internal/ 之后的路径书写（如 service/gateway_systemone.go）。"""

    for root in ("backend/internal/", "backend/"):
        if file.startswith(root):
            return file[len(root):]
    return file


def _kebab(function: str) -> str:
    name = function.rsplit(".", 1)[-1].lstrip("*")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower()


def _go_string(text: str) -> str:
    """JSON 字符串字面量同时是合法的 Go 解释型字符串字面量。"""

    if not text.strip():
        raise UpstreamMergeError("登记说明不能为空")
    return json.dumps(text, ensure_ascii=False)


def default_notes(sinks: Sequence[dict[str, Any]], *, upstream_tag: str, date: str) -> dict[str, Any]:
    """按函数与文件起草的 notes；人改写后经 --notes 回灌，补丁与算法摘要随之重算。"""

    slug = tag_slug(upstream_tag)
    entries: list[dict[str, Any]] = []
    names: set[str] = set()
    for sink in sinks:
        parsed = parse_candidate(str(sink["scan_candidate_id"]))
        name = f"upstream-{slug}-{_kebab(parsed['function'])}"
        if name in names:
            name = f"{name}-{len(names) + 1}"
        names.add(name)
        location = f"{parsed['function']}，{rule_prefix(parsed['file'])}"
        entries.append(
            {
                "scan_candidate_id": parsed["scan_candidate_id"],
                "name": name,
                "classify_rationale": (
                    f"上游 {upstream_tag} 新增发送点（{parsed['function']}），范围外"
                    if sink.get("missing_classification")
                    else None
                ),
                "rationale": f"{slug} 批次上游新增发送点（{location}），按范围外登记，不承载官方 OAuth 出站。",
            }
        )
    return {
        "schema_version": NOTES_SCHEMA,
        "upstream_tag": upstream_tag,
        "receipt_tag": receipt_tag(upstream_tag, date),
        "merge_group": f"upstream-{slug}-additions",
        "successor_reason": None,
        "sinks": entries,
    }


def _validate_notes(notes: dict[str, Any], sinks: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if notes.get("schema_version") != NOTES_SCHEMA:
        raise UpstreamMergeError("notes schema_version 非法")
    if TAG_RE.fullmatch(str(notes.get("upstream_tag"))) is None:
        raise UpstreamMergeError("notes.upstream_tag 非法")
    for key in ("receipt_tag", "merge_group"):
        if not isinstance(notes.get(key), str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", notes[key]):
            raise UpstreamMergeError(f"notes.{key} 非法")
    entries = notes.get("sinks")
    if not isinstance(entries, list):
        raise UpstreamMergeError("notes.sinks 必须是数组")
    by_id = {str(entry.get("scan_candidate_id")): entry for entry in entries if isinstance(entry, dict)}
    expected = {str(sink["scan_candidate_id"]) for sink in sinks}
    if set(by_id) != expected or len(by_id) != len(entries):
        raise UpstreamMergeError("notes.sinks 必须与预检列出的未登记新增发送点逐个对应")
    names = [str(entry.get("name")) for entry in entries]
    if len(set(names)) != len(names) or not all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in names):
        raise UpstreamMergeError("notes.sinks[].name 必须唯一且只含小写字母、数字与连字符")
    for sink in sinks:
        entry = by_id[str(sink["scan_candidate_id"])]
        if sink.get("missing_classification"):
            _go_string(str(entry.get("classify_rationale") or ""))
        _go_string(str(entry.get("rationale") or ""))
    return by_id


def _insert_before_block_end(text: str, opening: str, insertion: str, label: str) -> str:
    """在以 opening 开头、以单独一行 "}" 结束的块末尾插入内容。"""

    start = text.find(opening)
    if start < 0 or text.count(opening) != 1:
        raise UpstreamMergeError(f"{label} 没有找到唯一的块起点")
    end = text.find("\n}\n", start)
    if end < 0:
        raise UpstreamMergeError(f"{label} 块没有闭合")
    return text[: end + 1] + insertion + text[end + 1 :]


def _addition_block(entry: dict[str, Any], parsed: dict[str, str], *, evidence_var: str, merge_group: str) -> str:
    fields = [
        ("name", _go_string(entry["name"])),
        ("candidateID", _go_string(parsed["scan_candidate_id"])),
        ("persona", '"out-of-scope"'),
        ("runtimeSinkID", '""'),
        ("purpose", '""'),
        ("endpointEvidence", '"not_applicable"'),
        ("sinkKind", _go_string(parsed["sink_kind"])),
        ("backend", '"-"'),
        ("targetBackend", '"-"'),
        ("enforcementState", '"not_applicable"'),
        ("evidenceRef", evidence_var),
        ("rationale", _go_string(entry["rationale"])),
        ("absentBeforeMerge", "true"),
        ("mergeGroup", _go_string(merge_group)),
    ]
    lines = ["\t{"] + [f"\t\t{(name + ':'):<{FIELD_WIDTH}}{value}," for name, value in fields] + ["\t},"]
    return "\n".join(lines) + "\n"


def scanner_algorithm_digest(sources: dict[str, bytes]) -> str:
    """与 egressscan 的 scannerAlgorithmDigest 同一算法：非测试 .go 按文件名排序，逐个累加“名\\0内容\\0”。"""

    digest = hashlib.sha256()
    for name in sorted(name for name in sources if name.endswith(".go") and not name.endswith("_test.go")):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sources[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _unified_diff(relative: str, before: str, after: str) -> str:
    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        f"a/{relative}",
        f"b/{relative}",
    )
    return f"diff --git a/{relative} b/{relative}\n" + "".join(lines)


def draft_sink_registration(
    repository_root: Path,
    sinks: Sequence[dict[str, Any]],
    *,
    upstream_tag: str,
    date: str,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成登记补丁；sinks 取自预检报告的 scanner_coverage.unregistered_added_sinks。"""

    if not sinks:
        raise UpstreamMergeError("没有未登记的新增发送点，无需起草登记补丁")
    notes = notes if notes is not None else default_notes(sinks, upstream_tag=upstream_tag, date=date)
    by_id = _validate_notes(notes, sinks)
    if notes["upstream_tag"] != upstream_tag:
        raise UpstreamMergeError("notes.upstream_tag 与预检报告的目标 tag 不一致")
    scanner_root = repository_root / SCANNER_RELATIVE
    classify_before = (repository_root / CLASSIFY_RELATIVE).read_text(encoding="utf-8")
    acceptance_before = (repository_root / ACCEPTANCE_RELATIVE).read_text(encoding="utf-8")
    successor_before = (repository_root / SUCCESSOR_RELATIVE).read_text(encoding="utf-8")

    slug = tag_slug(upstream_tag)
    evidence_var = f"upstream{slug[0].upper()}{slug[1:]}PendingAdditionsEvidence"
    receipt_path = f"docs/egress/maintenance/upstream-{notes['receipt_tag']}-freeze-successor.json"
    classify_lines: list[str] = []
    addition_blocks: list[str] = []
    drafted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for sink in sorted(sinks, key=lambda item: str(item["scan_candidate_id"])):
        parsed = parse_candidate(str(sink["scan_candidate_id"]))
        entry = by_id[parsed["scan_candidate_id"]]
        if _go_string(parsed["scan_candidate_id"]) in acceptance_before:
            skipped.append({"scan_candidate_id": parsed["scan_candidate_id"], "reason": "已有登记条目"})
            continue
        prefix = rule_prefix(parsed["file"])
        rule_line = None
        if sink.get("missing_classification"):
            rule_line = f"\toos({_go_string(prefix)}, {_go_string(entry['classify_rationale'])}),\n"
            if f"\toos({_go_string(prefix)}, " not in classify_before:
                classify_lines.append(rule_line)
        addition_blocks.append(
            _addition_block(entry, parsed, evidence_var=evidence_var, merge_group=notes["merge_group"])
        )
        drafted.append(
            {
                "scan_candidate_id": parsed["scan_candidate_id"],
                "name": entry["name"],
                "file": parsed["file"],
                "classify_rule": rule_prefix(parsed["file"]) if rule_line else None,
            }
        )
    if not addition_blocks:
        raise UpstreamMergeError("预检列出的发送点都已有登记条目，无需起草登记补丁")
    group_size = len(addition_blocks)

    classify_after = classify_before
    if classify_lines:
        classify_after = _insert_before_block_end(classify_before, CLASSIFY_OPEN, "".join(classify_lines), "classify.go 规则表")
    acceptance_after = acceptance_before
    if f"var {evidence_var} = " not in acceptance_after:
        matches = list(EVIDENCE_VAR_RE.finditer(acceptance_after))
        if not matches:
            raise UpstreamMergeError("post_bootstrap_acceptance.go 没有找到预先登记承接收据变量")
        anchor = matches[-1].end()
        declaration = (
            f"\n// {evidence_var} 是合并 {slug} 批次前在主干预先登记新增发送点的承接收据。\n"
            f"var {evidence_var} = {_go_string(receipt_path)}\n"
        )
        acceptance_after = acceptance_after[:anchor] + declaration + acceptance_after[anchor:]
    acceptance_after = _insert_before_block_end(
        acceptance_after, ADDITIONS_OPEN, "".join(addition_blocks), "post_bootstrap_acceptance.go 新增发送点登记"
    )

    sources = {
        path.name: path.read_bytes()
        for path in scanner_root.iterdir()
        if path.is_file() and path.name.endswith(".go")
    }
    sources["classify.go"] = classify_after.encode("utf-8")
    sources["post_bootstrap_acceptance.go"] = acceptance_after.encode("utf-8")
    algorithm = scanner_algorithm_digest(sources)
    successor = json.loads(successor_before)
    names = "、".join(item["name"] for item in drafted)
    reason = notes.get("successor_reason") or (
        f"上游 {upstream_tag} 合并前在主干预先登记纯新增发送点：{len(drafted)} 个范围外发送点（{names}）按 mergeGroup "
        f"{notes['merge_group']} 登记“合并前整组缺失、合并后整组齐全”"
        + (f"，并补 {len(classify_lines)} 条 out-of-scope 分类规则" if classify_lines else "")
        + "；不改写 bootstrap inventory；单跳从 bootstrap lock 摘要承接到当前扫描器摘要（sink-registration-draft 起草）。"
    )
    successor.update(
        {
            "to_sha256": algorithm,
            "source_transition": receipt_path,
            "reviewed_by": f"sub2api-{upstream_tag}-merge-scanner-pending-additions",
            "reason": reason,
        }
    )
    successor_after = json.dumps(successor, ensure_ascii=False, indent=2) + "\n"
    patch = "".join(
        _unified_diff(relative, before, after)
        for relative, before, after in (
            (CLASSIFY_RELATIVE, classify_before, classify_after),
            (ACCEPTANCE_RELATIVE, acceptance_before, acceptance_after),
            (SUCCESSOR_RELATIVE, successor_before, successor_after),
        )
    )
    return {
        "patch": patch,
        "notes": notes,
        "drafted": drafted,
        "skipped": skipped,
        "merge_group_size": group_size,
        "scanner_algorithm_sha256": algorithm,
        "receipt_tag": notes["receipt_tag"],
        "receipt_path": receipt_path,
        "next_steps": [
            "git apply <补丁>（主仓库主干，干净工作树）",
            "cd backend && go test ./cmd/egressscan/ && go run ./cmd/egressscan -mode self-test",
            "提交后：python3 -m tools.upstream_merge freeze-successor-generate --before <应用前提交> --after <登记提交> "
            f"--tag {notes['receipt_tag']} --output <仓库>/{receipt_path}，收据单独提交",
            "重跑预检，确认 scanner_coverage 不再列出这些发送点",
        ],
    }


def write_sink_registration(
    repository_root: Path,
    report: dict[str, Any],
    output: Path,
    *,
    notes_path: Path | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """按预检报告写出登记补丁；不给 notes 时另写一份 notes 模板（<补丁名>.notes.json）供改写后回灌。

    补丁与 notes 都是非权威草稿，写入仓库外或指定的 Git 忽略目录，不覆盖既有文件。
    """

    root = repository_root.resolve()
    assert_private_path(root, output, "登记补丁")
    coverage = expect_object(expect_object(report.get("report"), "preflight report.report").get("scanner_coverage"), "scanner_coverage")
    sinks = coverage.get("unregistered_added_sinks") or []
    if not isinstance(sinks, list):
        raise UpstreamMergeError("scanner_coverage.unregistered_added_sinks 必须是数组")
    tags = expect_object(report.get("covered_tags"), "preflight report.covered_tags").get("tags") or []
    if not isinstance(tags, list) or not tags or not isinstance(tags[-1], dict):
        raise UpstreamMergeError("预检报告没有覆盖区间 tag，无法确定登记批次")
    upstream_tag = str(tags[-1].get("tag"))
    notes = expect_object(load_json(notes_path, "sink registration notes"), "sink registration notes") if notes_path else None
    draft = draft_sink_registration(
        root,
        sinks,
        upstream_tag=upstream_tag,
        date=date or datetime.now(timezone.utc).strftime("%Y%m%d"),
        notes=notes,
    )
    write_once(output, draft["patch"].encode("utf-8"))
    notes_output = None
    if notes_path is None:
        notes_output = output.with_suffix(".notes.json") if output.suffix == ".patch" else output.with_name(output.name + ".notes.json")
        write_json_once(notes_output, draft["notes"])
    return {
        "result": "drafted",
        "patch": str(output),
        "notes": str(notes_output or notes_path),
        "drafted": draft["drafted"],
        "skipped": draft["skipped"],
        "merge_group_size": draft["merge_group_size"],
        "receipt_tag": draft["receipt_tag"],
        "scanner_algorithm_sha256": draft["scanner_algorithm_sha256"],
        "next_steps": draft["next_steps"],
    }
