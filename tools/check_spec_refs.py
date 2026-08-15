#!/usr/bin/env python3
"""校验规格表源码引用、规则锚点、测试代码边界和 L2 依赖快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
DEFAULT_ANCHOR_MANIFEST = ROOT / "tools" / "spec_ref_anchors.json"
DEFAULT_DEPENDENCY_MANIFEST = ROOT / "tools" / "spec_source_deps" / "manifest.json"
CODEX_SOURCE_ROOT = ROOT / "local-analysis" / "sources" / "codex-cli-0.147"
CODEX_SOURCE_VERSION = "0.147.0"
DEPENDENCY_SOURCE_ROOT = ROOT / "tools" / "spec_source_deps"
SEARCH_ROOTS = [CODEX_SOURCE_ROOT, ROOT / "backend", DEPENDENCY_SOURCE_ROOT]

REF_RE = re.compile(r"`?([\w/\-.]+\.(?:rs|go)):(\d+)(?:-(\d+))?")
RULE_HEAD_RE = re.compile(r"^### (SPEC-[A-Z0-9]+-\d+)\b.*$", re.M)
ANY_HEAD_RE = re.compile(r"^#{1,6} \S", re.M)
SOURCE_FIELD_RE = re.compile(
    r"^- \*\*源码\*\*：(.*?)(?=^- \*\*[^\n]+\*\*：)",
    re.M | re.S,
)

# 这些 Go 标准库或外部模块引用不属于 Codex 0.147.0 与已固化的 Rust 依赖。
EXTERNAL_PREFIXES = (
    "x/net/",
    "internal/httpcommon/",
    "net/http/",
    "http2/",
    "transport.go",
    "config.go",
    "frame.go",
)


@dataclass(frozen=True)
class SourceRef:
    path: str
    start: int
    end: int

    @classmethod
    def from_match(cls, match: re.Match[str]) -> "SourceRef":
        start = int(match.group(2))
        end = int(match.group(3) or start)
        return cls(match.group(1), start, end)

    def target(self) -> str:
        suffix = f"-{self.end}" if self.end != self.start else ""
        return f"{self.path}:{self.start}{suffix}"

    def identity(self) -> dict[str, Any]:
        return {"path": self.path, "start": self.start, "end": self.end}


def parse_refs(text: str) -> list[SourceRef]:
    return [SourceRef.from_match(match) for match in REF_RE.finditer(text)]


def chapter_two(text: str) -> str:
    start = re.search(r"^# 第二部分 Codex CLI 客户端规则画像\s*$", text, re.M)
    if start is None:
        raise ValueError("规格表缺少第二部分客户端规则画像")
    end = re.search(r"^# 第三部分(?:\s|$)", text[start.end() :], re.M)
    if end is None:
        raise ValueError("规格表缺少第二部分之后的第三部分边界")
    return text[start.end() : start.end() + end.start()]


def extract_rule_source_fields(text: str) -> dict[str, str]:
    section = chapter_two(text)
    heads = list(RULE_HEAD_RE.finditer(section))
    fields: dict[str, str] = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(section)
        block = section[head.start() : end]
        source = SOURCE_FIELD_RE.search(block)
        if source is None:
            raise ValueError(f"{head.group(1)} 缺少源码字段")
        fields[head.group(1)] = source.group(1).strip()
    return fields


def extract_rule_source_refs(text: str) -> dict[str, list[SourceRef]]:
    return {sid: parse_refs(field) for sid, field in extract_rule_source_fields(text).items()}


def build_index(search_roots: list[pathlib.Path] | None = None) -> dict[str, list[pathlib.Path]]:
    index: dict[str, list[pathlib.Path]] = {}
    for root in search_roots or SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".rs", ".go"):
                index.setdefault(path.name, []).append(path)
    return index


@lru_cache(maxsize=None)
def source_lines(path: pathlib.Path) -> tuple[str, ...]:
    return tuple(path.read_text(encoding="utf-8", errors="replace").split("\n"))


def is_external(ref: SourceRef) -> bool:
    return ref.path.startswith(EXTERNAL_PREFIXES)


def resolve_ref(
    ref: SourceRef,
    index: dict[str, list[pathlib.Path]],
) -> tuple[pathlib.Path | None, str | None]:
    name = pathlib.Path(ref.path).name
    same_name = index.get(name, [])

    if "/" not in ref.path:
        if not same_name:
            return None, "文件不存在"
        if len(same_name) != 1:
            return None, f"裸文件名有 {len(same_name)} 个候选"
        candidates = same_name
    else:
        candidates = [path for path in same_name if str(path).endswith(ref.path)]
        if not candidates:
            if same_name:
                return None, f"路径不符；存在 {len(same_name)} 个同名文件"
            return None, "文件不存在"
        if len(candidates) != 1:
            return None, f"完整路径仍命中 {len(candidates)} 个候选"

    if ref.start < 1 or ref.end < ref.start:
        return None, "行区间无效"
    line_count = len(source_lines(candidates[0]))
    if ref.end > line_count:
        return None, f"行号越界；文件共 {line_count} 行"
    return candidates[0], None


def context_digest(path: pathlib.Path, ref: SourceRef) -> tuple[str, str]:
    lines = source_lines(path)
    low = max(1, ref.start - 8)
    high = min(len(lines), ref.end + 8)
    context = "\n".join(lines[low - 1 : high])
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()

    direct = [line.strip() for line in lines[ref.start - 1 : ref.end] if line.strip()]
    if not direct:
        direct = [line.strip() for line in lines[low - 1 : high] if line.strip()]
    preview = (direct[0] if direct else "<空上下文>")[:160]
    return digest, preview


@lru_cache(maxsize=None)
def cfg_test_spans(path: pathlib.Path) -> tuple[tuple[int, int], ...]:
    """用大括号配平求出文件中各个 `#[cfg(test)]` 作用区间。"""
    lines = source_lines(path)
    spans: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not re.match(r"^\s*#\[cfg\(test\)\]", line):
            continue
        probe = "\n".join(lines[index : index + 4])
        brace_at = probe.find("{")
        semi_at = probe.find(";")
        if brace_at < 0 or 0 <= semi_at < brace_at:
            end = index + 1 + probe[: semi_at if semi_at >= 0 else len(probe)].count("\n")
            spans.append((index + 1, end + 1))
            continue

        depth = 0
        started = False
        end = None
        for cursor in range(index, len(lines)):
            depth += lines[cursor].count("{") - lines[cursor].count("}")
            started = started or "{" in lines[cursor]
            if started and depth <= 0:
                end = cursor + 1
                break
        spans.append((index + 1, end or len(lines)))
    return tuple(spans)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_dependency_snapshot(manifest_path: pathlib.Path) -> tuple[int, list[str]]:
    errors: list[str] = []
    if not manifest_path.is_file():
        return 0, [f"L2 依赖清单不存在：{manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, [f"L2 依赖清单无法读取：{exc}"]

    if manifest.get("schema_version") != 1:
        errors.append("L2 依赖清单 schema_version 必须为 1")

    lock_path = ROOT / str(manifest.get("cargo_lock", ""))
    if not lock_path.is_file():
        errors.append(f"Cargo.lock 不存在：{lock_path}")
        lock_packages: list[dict[str, Any]] = []
    else:
        lock_data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        lock_packages = lock_data.get("package", [])

    expected_files: set[pathlib.Path] = set()
    dependencies = manifest.get("dependencies", [])
    for dependency in dependencies:
        name = str(dependency.get("name", ""))
        version = str(dependency.get("version", ""))
        commit = str(dependency.get("commit", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append(f"{name} 的上游提交不是完整 SHA-1")

        matches = [
            package
            for package in lock_packages
            if package.get("name") == name and package.get("version") == version
        ]
        if len(matches) != 1:
            errors.append(f"Cargo.lock 中 {name} {version} 命中 {len(matches)} 次")
        else:
            package = matches[0]
            if package.get("source") != dependency.get("lock_source"):
                errors.append(f"{name} {version} 的 Cargo.lock source 不一致")
            expected_checksum = dependency.get("lock_checksum")
            if expected_checksum is not None and package.get("checksum") != expected_checksum:
                errors.append(f"{name} {version} 的 Cargo.lock checksum 不一致")

        files = dependency.get("files", {})
        for relative, expected_hash in files.items():
            relative_path = pathlib.Path(relative)
            expected_files.add(relative_path)
            path = manifest_path.parent / relative_path
            if not path.is_file():
                errors.append(f"L2 快照文件不存在：{relative}")
                continue
            actual_hash = sha256_file(path)
            if actual_hash != expected_hash:
                errors.append(f"L2 快照哈希不一致：{relative}")

    actual_files = {
        path.relative_to(manifest_path.parent)
        for path in manifest_path.parent.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "README.md"}
    }
    for relative in sorted(actual_files - expected_files):
        errors.append(f"L2 快照存在未登记文件：{relative}")
    for relative in sorted(expected_files - actual_files):
        errors.append(f"L2 快照清单登记但文件缺失：{relative}")
    return len(dependencies), errors


def validate_rule_anchors(
    spec_text: str,
    manifest_path: pathlib.Path,
    index: dict[str, list[pathlib.Path]],
) -> tuple[int, int, list[str]]:
    errors: list[str] = []
    if not manifest_path.is_file():
        return 0, 0, [f"规则锚点清单不存在：{manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, 0, [f"规则锚点清单无法读取：{exc}"]

    if manifest.get("schema_version") != 1:
        errors.append("规则锚点清单 schema_version 必须为 1")

    current = extract_rule_source_refs(spec_text)
    expected = manifest.get("rules", {})
    if set(current) != set(expected):
        missing = sorted(set(current) - set(expected))
        stale = sorted(set(expected) - set(current))
        if missing:
            errors.append(f"规则锚点缺少条目：{', '.join(missing)}")
        if stale:
            errors.append(f"规则锚点存在多余条目：{', '.join(stale)}")

    checked = 0
    for sid in sorted(set(current) & set(expected)):
        current_refs = current[sid]
        expected_refs = expected[sid]
        identities = [ref.identity() for ref in current_refs]
        recorded = [
            {"path": item.get("path"), "start": item.get("start"), "end": item.get("end")}
            for item in expected_refs
        ]
        if identities != recorded:
            errors.append(f"{sid} 的源码引用与锚点清单不一致")
            continue

        for ref, item in zip(current_refs, expected_refs, strict=True):
            if is_external(ref):
                errors.append(f"{sid} 仍引用未固化的外部源码：{ref.target()}")
                continue
            path, error = resolve_ref(ref, index)
            if error or path is None:
                errors.append(f"{sid} 无法解析锚点 {ref.target()}：{error}")
                continue
            digest, preview = context_digest(path, ref)
            if digest != item.get("context_sha256"):
                errors.append(f"{sid} 的源码上下文已变化：{ref.target()}")
                continue
            if preview != item.get("anchor"):
                errors.append(f"{sid} 的可读锚点已变化：{ref.target()}")
                continue
            checked += 1
    return len(current), checked, errors


def find_no_line_gaps(spec_text: str) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    gaps: list[str] = []
    for sid, field in extract_rule_source_fields(spec_text).items():
        levels = set(re.findall(r"\[(L[1-4])[^\]]*\]", field))
        if not levels & {"L1", "L2"} or parse_refs(field):
            continue
        if "L1" in levels and "反证" in field:
            allowed.append(sid)
        else:
            gaps.append(sid)
    return allowed, gaps


def codex_version_error() -> str | None:
    cargo = CODEX_SOURCE_ROOT / "codex-rs" / "Cargo.toml"
    if not cargo.is_file():
        return f"找不到 Codex 源码树：{CODEX_SOURCE_ROOT}"
    match = re.search(
        r'^version\s*=\s*"([^"]+)"',
        cargo.read_text(encoding="utf-8"),
        re.M,
    )
    version = match.group(1) if match else "?"
    if version != CODEX_SOURCE_VERSION:
        return f"源码树版本为 {version}，规格表要求 {CODEX_SOURCE_VERSION}"
    print(f"源码基线：codex-cli {CODEX_SOURCE_VERSION} ✅")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--symbol",
        action="store_true",
        help="校验第二部分规则级源码引用及上下文锚点",
    )
    parser.add_argument(
        "--cfg-test",
        action="store_true",
        help="禁止 L1/L2 引用落入 #[cfg(test)] 区间",
    )
    parser.add_argument("--spec", type=pathlib.Path, default=DEFAULT_SPEC)
    parser.add_argument(
        "--anchor-manifest",
        type=pathlib.Path,
        default=DEFAULT_ANCHOR_MANIFEST,
    )
    parser.add_argument(
        "--dependency-manifest",
        type=pathlib.Path,
        default=DEFAULT_DEPENDENCY_MANIFEST,
    )
    args = parser.parse_args()

    fatal: list[str] = []
    version_error = codex_version_error()
    if version_error:
        fatal.append(version_error)

    if not args.spec.is_file():
        fatal.append(f"规格表不存在：{args.spec}")
        spec_text = ""
    else:
        spec_text = args.spec.read_text(encoding="utf-8")

    dependency_count, dependency_errors = validate_dependency_snapshot(
        args.dependency_manifest
    )
    fatal.extend(dependency_errors)
    if not dependency_errors:
        print(f"L2 依赖源码快照：{dependency_count} 个 ✅")

    index = build_index()
    seen: set[SourceRef] = set()
    located = 0
    external = 0
    reference_errors: list[str] = []
    cfg_hits: list[str] = []

    for ref in parse_refs(spec_text):
        if ref in seen:
            continue
        seen.add(ref)
        if is_external(ref):
            external += 1
            continue
        path, error = resolve_ref(ref, index)
        if error or path is None:
            reference_errors.append(f"{ref.target()}：{error}")
            continue
        located += 1
        if args.verbose:
            print(f"  {ref.target()} → {source_lines(path)[ref.start - 1].strip()[:80]}")
        if args.cfg_test:
            for low, high in cfg_test_spans(path):
                if ref.start <= high and ref.end >= low:
                    cfg_hits.append(f"{ref.target()}（测试区间 {low}-{high}）")
                    break

    print(
        f"源码引用 {len(seen)} 处：✅ 可定位 {located}   "
        f"⏭ 外部库跳过 {external}   ❌ 有问题 {len(reference_errors)}"
    )

    anchor_errors: list[str] = []
    if args.symbol and spec_text:
        rule_count, anchor_count, anchor_errors = validate_rule_anchors(
            spec_text,
            args.anchor_manifest,
            index,
        )
        if not anchor_errors:
            print(f"第二部分规则锚点：{rule_count} 条规则、{anchor_count} 处引用 ✅")

    allowed_no_line: list[str] = []
    no_line_gaps: list[str] = []
    if spec_text:
        allowed_no_line, no_line_gaps = find_no_line_gaps(spec_text)
        if allowed_no_line:
            print(f"L1 反证无单一行号：{len(allowed_no_line)} 条（允许）")

    groups = [
        ("基础门禁", fatal),
        ("引用定位", reference_errors),
        ("规则锚点", anchor_errors),
        ("测试代码引用", cfg_hits),
        ("L1/L2 无精确行号", no_line_gaps),
    ]
    has_errors = False
    for title, errors in groups:
        if not errors:
            continue
        has_errors = True
        print(f"\n🔴 {title}：{len(errors)} 项", file=sys.stderr)
        for error in errors:
            print(f"   {error}", file=sys.stderr)

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
