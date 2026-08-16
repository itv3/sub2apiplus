#!/usr/bin/env python3
"""从 Claude Code 官方二进制确定性提取 Bun SEA 内嵌 bundle，并生成产物锚点。

为什么需要这个工具
------------------
Claude Code 2.1.220 以 Bun SEA 单二进制发行，没有可读的 TS 源码。此前的取证方式
是对二进制跑 `strings` 撞字面量，这有一个**结构性盲区**：minify 会把 header 名
提取成变量，真正的写入点形如 `pi[S8s]=Vtp`，不含任何字面量，`strings` 永远找不到。
`anthropic-dispatch-id` 就是实例——字面量只出现 2 次（常量定义与日志文本），
而唯一的写入点没有字面量，其触发条件曾因此被误判为「默认发送」。

Bun SEA 的 JS 是**明文原样嵌入**且带完整模块表的，可以确定性提取成语法完整的
JavaScript。提取之后，控制流、常量和条件都在，静态能力等同于读生产源码。
这个工具把提取过程工具化，使 `docs/Claude_code_21220_EGRESS_SPEC.md`
§2.1.1 定义的**产物锚点**可以被机器复算；复算入口见 §2.1.3。

定位方式（重要）
----------------
必须先按容器定位 SEA section —— Mach-O 的 `__BUN/__bun` 或 ELF 的 `.bun` ——
再校验 trailer magic 恰好落在该 section 尾部。**不能用全文件 magic 搜索代替**：
二进制内另有 `Bun!` 同名字面量（实测 5 处），且 Mach-O 在 section 之后还有
`__LINKEDIT` 与代码签名，全文件搜索既不稳也无法自证定位正确。

只依赖标准库（Mach-O 走 otool，ELF 直接解析 section header）。

用法：
  # 提取并写出锚点
  python3 extract_claude_bundle.py --binary <claude> --out-dir <目录> \\
      --expected-sha256 674f61f2... --emit-anchors anchors.json

  # 只校验结构、不落盘（CI 门禁用）
  python3 extract_claude_bundle.py --binary <claude> --check-only
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import pathlib
import re
import struct
import subprocess
import sys

# Bun standalone module graph 的尾部标记。
MAGIC = b"\n---- Bun! ----\n"

# magic 之前的 Offsets 结构长度。前 16 字节语义已确认（byte_count + modules_ptr），
# 其后 16 字节尚未完全逆向，原样记入锚点，格式变化时可被检出。
OFFSETS_SIZE = 32

# 模块表每条记录长度。前 24 字节为 name/contents/sourcemap 三组 StringPointer，
# 其余 28 字节（含 loader 标志）未完全逆向，同样原样记入锚点。
ENTRY_SIZE = 52

# 主模块预期路径。Bun 保留了打包前的源码路径，这个值比模块序号稳定。
MAIN_MODULE = "/$bunfs/root/src/entrypoints/cli.js"


class ExtractError(RuntimeError):
    """结构校验失败。任何一步不满足预期都必须中止，不允许降级为模糊匹配。"""


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_bun_section(path: pathlib.Path) -> tuple[str, str, int, int]:
    """定位 SEA section，返回 (容器类型, section 名, 文件偏移, 长度)。"""
    with open(path, "rb") as fh:
        head = fh.read(4)

    if head == b"\x7fELF":
        return _locate_elf(path)
    if head in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return _locate_macho(path)
    raise ExtractError(f"无法识别的容器格式：{head!r}")


def _locate_elf(path: pathlib.Path) -> tuple[str, str, int, int]:
    with open(path, "rb") as fh:
        ident = fh.read(64)
        if ident[4] != 2 or ident[5] != 1:
            raise ExtractError("仅支持 64 位小端 ELF")
        e_shoff, = struct.unpack_from("<Q", ident, 0x28)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", ident, 0x3A)

        fh.seek(e_shoff + e_shstrndx * e_shentsize)
        sh_strtab = fh.read(e_shentsize)
        str_off, str_size = struct.unpack_from("<QQ", sh_strtab, 0x18)
        fh.seek(str_off)
        strtab = fh.read(str_size)

        for i in range(e_shnum):
            fh.seek(e_shoff + i * e_shentsize)
            sh = fh.read(e_shentsize)
            name_idx, = struct.unpack_from("<I", sh, 0)
            off, size = struct.unpack_from("<QQ", sh, 0x18)
            name = strtab[name_idx:strtab.index(b"\0", name_idx)].decode()
            if name == ".bun":
                return ("elf", ".bun", off, size)
    raise ExtractError("ELF 中未找到 .bun section")


def _locate_macho(path: pathlib.Path) -> tuple[str, str, int, int]:
    try:
        out = subprocess.run(["otool", "-l", str(path)],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ExtractError(f"调用 otool 失败：{exc}") from exc

    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "sectname __bun":
            continue
        offset = size = None
        # sectname 之后的若干行内是本 section 的字段；限定窗口避免串到下一个 section。
        for raw in lines[i + 1:i + 10]:
            field = raw.strip()
            if field.startswith("sectname "):
                break
            if field.startswith("size "):
                size = int(field.split()[1], 16)
            elif field.startswith("offset "):
                offset = int(field.split()[1])
        if offset is None or size is None:
            raise ExtractError("__bun section 缺少 offset/size 字段")
        return ("macho", "__BUN/__bun", offset, size)
    raise ExtractError("Mach-O 中未找到 __BUN/__bun section")


def parse_module_graph(blob: bytes, sec_off: int, sec_size: int) -> dict:
    """在 section 范围内解析模块图。任何越界或对不齐都视为结构不符。"""
    sec_end = sec_off + sec_size
    if sec_end > len(blob):
        raise ExtractError(f"section 越过文件末尾：{sec_end} > {len(blob)}")

    # 校验 magic 恰在 section 尾部，而不是全文件搜索。
    magic_start = sec_end - len(MAGIC)
    if blob[magic_start:sec_end] != MAGIC:
        raise ExtractError(
            f"section 尾部不是 Bun SEA trailer magic（尾部 16 字节为 "
            f"{blob[magic_start:sec_end]!r}）")

    off_start = magic_start - OFFSETS_SIZE
    byte_count, mod_off, mod_len = struct.unpack_from("<QII", blob, off_start)
    reserved = blob[off_start + 16:off_start + OFFSETS_SIZE]

    base = off_start - byte_count
    if base < sec_off:
        raise ExtractError(f"blob 起点 {base} 落在 section 之前 {sec_off}")
    if mod_len % ENTRY_SIZE != 0:
        raise ExtractError(f"模块表长度 {mod_len} 不是 {ENTRY_SIZE} 的整数倍")

    table = base + mod_off
    if not (base <= table and table + mod_len <= magic_start):
        raise ExtractError("模块表落在 blob 范围之外")

    modules = []
    for i in range(mod_len // ENTRY_SIZE):
        rec = table + i * ENTRY_SIZE
        n_off, n_len, c_off, c_len, s_off, s_len = struct.unpack_from("<IIIIII", blob, rec)
        for label, o, ln in (("name", n_off, n_len), ("contents", c_off, c_len)):
            if o + ln > byte_count:
                raise ExtractError(f"模块 {i} 的 {label} 越过 blob 边界")
        name = blob[base + n_off:base + n_off + n_len].decode("utf-8")
        modules.append({
            "index": i,
            "name": name,
            "contents_offset": c_off,
            "contents_length": c_len,
            "sourcemap_length": s_len,
            # 未逆向字段原样保留，供跨版本比对格式是否变化。
            "record_tail_hex": blob[rec + 24:rec + ENTRY_SIZE].hex(),
        })

    return {
        "blob_base": base,
        "byte_count": byte_count,
        "modules_offset": mod_off,
        "modules_length": mod_len,
        "offsets_reserved_hex": reserved.hex(),
        "modules": modules,
    }


def is_text(data: bytes) -> bool:
    """粗判模块是文本还是原生二进制（.node）。只用于分类，不参与校验。"""
    head = data[:256]
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in head)
    return bool(head) and printable >= len(head) * 0.9


def extract(binary: pathlib.Path, expected_sha: str | None) -> dict:
    binary_sha = sha256_file(binary)
    if expected_sha and binary_sha != expected_sha:
        raise ExtractError(
            f"二进制 SHA-256 不匹配：期望 {expected_sha}，实际 {binary_sha}")

    container, sec_name, sec_off, sec_size = locate_bun_section(binary)
    blob = binary.read_bytes()
    graph = parse_module_graph(blob, sec_off, sec_size)
    base = graph["blob_base"]

    main_found = False
    for mod in graph["modules"]:
        start = base + mod["contents_offset"]
        data = blob[start:start + mod["contents_length"]]
        mod["sha256"] = hashlib.sha256(data).hexdigest()
        mod["kind"] = "text" if is_text(data) else "native"
        if mod["name"] == MAIN_MODULE:
            main_found = True
            if mod["kind"] != "text":
                raise ExtractError("主模块不是文本，提取结果不可信")
    if not main_found:
        raise ExtractError(f"模块表中缺少主模块 {MAIN_MODULE}")

    return {
        "binary": str(binary),
        "binary_sha256": binary_sha,
        "binary_size": binary.stat().st_size,
        "container": container,
        "bun_section": sec_name,
        "bun_section_offset": sec_off,
        "bun_section_size": sec_size,
        "extractor_sha256": sha256_file(pathlib.Path(__file__)),
        **graph,
    }


def write_modules(binary: pathlib.Path, result: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = binary.read_bytes()
    base = result["blob_base"]
    for mod in result["modules"]:
        start = base + mod["contents_offset"]
        data = blob[start:start + mod["contents_length"]]
        # 模块名是打包前的绝对路径，取 basename 落盘，避免写出仓库之外。
        target = out_dir / pathlib.PurePosixPath(mod["name"]).name
        target.write_bytes(data)
        # 只记文件名：输出目录是调用方环境，写进锚点会让同一二进制在不同目录下
        # 算出不同的锚点，破坏复算。
        mod["extracted_name"] = target.name


# 候选台账用到的定位字面量。工具对每处命中给出 α-归一化语义锚点、最近的顶层符号
# 和窗口内的网络 sink，供 docs/Claude_code_21220_EGRESS_SPEC.md 第 2.2 节逐条复算。
CANDIDATE_PROBES = [
    ("CAND-HDR-DISPATCH", '"anthropic-dispatch-id"'),
    ("CAND-HDR-USAGE-LIMIT", '"anthropic-usage-limit"'),
    ("CAND-HDR-ADDITIONAL-PROTECTION", '"x-anthropic-additional-protection"'),
    ("CAND-HDR-CUSTOM", '"ANTHROPIC_CUSTOM_HEADERS"'),
    ("CAND-BETA-ENV", "ANTHROPIC_BETAS"),
    ("CAND-BODY-BILLING", "x-anthropic-billing-header"),
    ("CAND-BODY-CCH-PLACEHOLDER", "cch=00000"),
    ("CAND-UA-ENTRYPOINT", "claude-cli/"),
]

# 语义锚点以命中点为中心，取前后各这么多个 token。固定 token 窗口比按分号切分稳健，
# 详见 claude_bundle_reachability.BundleIndex.alpha_normalize_around。
ANCHOR_TOKENS = 45
# 判断写入点附近是否存在出站动作时使用的窗口。
SINK_WINDOW = 20000


@dataclass(frozen=True)
class StructuredProbe:
    """用结构正则锁定不能由单字面量证明的构造点。

    `literal` 是 2.1.220 Linux bundle 中便于人工核对的典型字面量，
    不参与定位。真正定位由 `locator_pattern` 完成：它用命名回溯约束
    minify 变量间的同值关系，因此 Linux 与 Darwin 符号名漂移时仍能
    命中同一段语义。`anchor_group` 指定用于 α-归一化的命名组，
    避免长正则从起点取窗时截丢核心表达式。
    """

    candidate: str
    literal: str
    locator_pattern: str
    anchor_group: str
    rule_ids: tuple[str, ...]
    before_tokens: int = ANCHOR_TOKENS
    after_tokens: int = ANCHOR_TOKENS


# JavaScript 标识符只用 ASCII 子集；这与 Bun/esbuild 的 minify 产物一致。
_JS_ID = r"[A-Za-z_$][A-Za-z0-9_$]*"

# 这七个探针只针对已绑定的 Claude Code 2.1.220 产物。四个 Header
# 探针同时约束「来源值→条件展开→原值写入」；retry 探针分别锁定
# 公式、常量块与主会话 messages.create 调用点，避免把 SDK 内置 retry
# 或 bundle 中的示例文档误当成 Claude Code 主请求链。
CLAUDE_2_1_220_STRUCTURED_PROBES = [
    StructuredProbe(
        candidate="CAND-HDR-CLIENT-APP-CONSTRUCTION",
        literal='"x-client-app"',
        locator_pattern=(
            rf"(?P<value>{_JS_ID})=process\.env\.CLAUDE_AGENT_SDK_CLIENT_APP"
            rf"[\s\S]{{0,512}}?\.\.\.(?P=value)&&"
            rf'\{{"(?P<header_name>x-client-app)":(?P=value)\}}'
        ),
        anchor_group="header_name",
        rule_ids=("SPEC-HDR-016", "SPEC-HDR-021", "SPEC-HDR-022"),
        before_tokens=60,
        after_tokens=30,
    ),
    StructuredProbe(
        candidate="CAND-HDR-REMOTE-CONTAINER-CONSTRUCTION",
        literal='"x-claude-remote-container-id"',
        locator_pattern=(
            rf"(?P<value>{_JS_ID})=process\.env\.CLAUDE_CODE_CONTAINER_ID"
            rf"[\s\S]{{0,512}}?\.\.\.(?P=value)&&"
            rf'\{{"(?P<header_name>x-claude-remote-container-id)":(?P=value)\}}'
        ),
        anchor_group="header_name",
        rule_ids=("SPEC-HDR-017", "SPEC-HDR-023"),
        before_tokens=60,
        after_tokens=30,
    ),
    StructuredProbe(
        candidate="CAND-HDR-REMOTE-SESSION-CONSTRUCTION",
        literal='"x-claude-remote-session-id"',
        locator_pattern=(
            rf"(?P<value>{_JS_ID})=process\.env\.CLAUDE_CODE_REMOTE_SESSION_ID"
            rf"[\s\S]{{0,512}}?\.\.\.(?P=value)&&"
            rf'\{{"(?P<header_name>x-claude-remote-session-id)":(?P=value)\}}'
        ),
        anchor_group="header_name",
        rule_ids=("SPEC-HDR-018", "SPEC-HDR-024"),
        before_tokens=60,
        after_tokens=30,
    ),
    StructuredProbe(
        candidate="CAND-HDR-PARENT-AGENT-CONSTRUCTION",
        literal='"x-claude-code-parent-agent-id"',
        locator_pattern=(
            rf"(?P<context>{_JS_ID})\?\.parentAgentId&&"
            rf'\{{"(?P<header_name>x-claude-code-parent-agent-id)":'
            rf"(?P<encoder>{_JS_ID})\("
            rf"(?P=context)\.parentAgentId\)\}}"
        ),
        anchor_group="header_name",
        rule_ids=("SPEC-HDR-019", "SPEC-HDR-025"),
        before_tokens=60,
        after_tokens=30,
    ),
    StructuredProbe(
        candidate="CAND-RETRY-DELAY-FORMULA",
        literal="Math.round(n+Math.random()*0.25*n)",
        locator_pattern=(
            rf"function\s+(?P<function>{_JS_ID})\("
            rf"(?P<attempt>{_JS_ID}),(?P<retry_after>{_JS_ID}),"
            rf"(?P<cap>{_JS_ID})=32000\)\{{let\s+"
            rf"(?P<base>{_JS_ID})=Math\.min\("
            rf"(?P<base_constant>{_JS_ID})\*Math\.pow\(2,(?P=attempt)-1\),"
            rf"(?P=cap)\),(?P<delay>{_JS_ID})=Math\.round\("
            rf"(?P=base)\+Math\.random\(\)\*0\.25\*(?P=base)\)"
        ),
        anchor_group="cap",
        rule_ids=("SPEC-CONN-002",),
    ),
    StructuredProbe(
        candidate="CAND-RETRY-CONSTANT-BLOCK",
        literal="qU_=500,VU_=60000,zU_=300000,Flp=21600000,KU_=30000",
        locator_pattern=(
            rf"(?P<base>{_JS_ID})=500,"
            rf"(?P<retry_after_limit>{_JS_ID})=60000,"
            rf"(?P<persistent_cap>{_JS_ID})=300000,"
            rf"(?P<persistent_reset_cap>{_JS_ID})=21600000,"
            rf"(?P<heartbeat>{_JS_ID})=30000"
        ),
        anchor_group="base",
        rule_ids=("SPEC-CONN-002", "SPEC-CONN-003"),
    ),
    StructuredProbe(
        candidate="CAND-RETRY-MAIN-MESSAGES-CREATE",
        literal=(
            "stream:!0},{signal:o,...Object.keys(pi).length>0&&"
            "{headers:pi}}).withResponse()"
        ),
        locator_pattern=(
            rf"await\s+(?P<client>{_JS_ID})\.beta\.messages\.create\("
            rf"\{{\.\.\.(?P<params>{_JS_ID}),"
            rf"\.\.\.(?P<credit>{_JS_ID})!==void\s+0&&"
            rf"\{{(?P<credit_key>{_JS_ID}):(?P=credit)\}},stream:!0\}},"
            rf"\{{signal:(?P<signal>{_JS_ID}),\.\.\.Object\.keys\("
            rf"(?P<headers>{_JS_ID})\)\.length>0&&"
            rf"\{{headers:(?P=headers)\}}\}}\)\.withResponse\(\)"
        ),
        anchor_group="headers",
        rule_ids=("SPEC-CONN-002", "SPEC-CONN-003"),
        before_tokens=50,
        after_tokens=20,
    ),
]


def build_reachability_index(bundle_path: pathlib.Path) -> dict:
    """对主 bundle 生成 sink 索引与候选锚点。"""
    from claude_bundle_reachability import SINK_PATTERNS, BundleIndex

    src = bundle_path.read_text(encoding="utf-8")
    index = BundleIndex(src)

    sinks = []
    for kind, pat in SINK_PATTERNS.items():
        for m in pat.finditer(src):
            sym = index.enclosing_symbol(m.start())
            sinks.append({"kind": kind, "offset": m.start(),
                          "nearest_symbol": sym["name"] if sym else None})

    probes = []
    for probe_id, literal in CANDIDATE_PROBES:
        hits = []
        pos = src.find(literal)
        while pos >= 0:
            text, digest = index.alpha_normalize_around(
                pos, before=ANCHOR_TOKENS, after=ANCHOR_TOKENS)
            sym = index.enclosing_symbol(pos)
            window = src[max(0, pos - SINK_WINDOW):pos + SINK_WINDOW]
            hits.append({
                "offset": pos,
                "nearest_symbol": sym["name"] if sym else None,
                "alpha_sha256": digest,
                "alpha_text": text[:600],
                "sinks_within_window": sorted(
                    k for k, p in SINK_PATTERNS.items() if p.search(window)),
            })
            pos = src.find(literal, pos + 1)
        probes.append({"candidate": probe_id, "literal": literal,
                       "hit_count": len(hits), "hits": hits})

    for probe in CLAUDE_2_1_220_STRUCTURED_PROBES:
        hits = []
        pattern = re.compile(probe.locator_pattern)
        for match in pattern.finditer(src):
            anchor = match.start(probe.anchor_group)
            text, digest = index.alpha_normalize_around(
                anchor,
                before=probe.before_tokens,
                after=probe.after_tokens,
            )
            sym = index.enclosing_symbol(anchor)
            window = src[
                max(0, anchor - SINK_WINDOW):anchor + SINK_WINDOW
            ]
            matched = match.group(0).encode("utf-8")
            hits.append({
                "offset": anchor,
                "match_start": match.start(),
                "match_end": match.end(),
                "match_sha256": hashlib.sha256(matched).hexdigest(),
                "nearest_symbol": sym["name"] if sym else None,
                "alpha_sha256": digest,
                "alpha_text": text[:600],
                "sinks_within_window": sorted(
                    kind
                    for kind, sink_pattern in SINK_PATTERNS.items()
                    if sink_pattern.search(window)
                ),
            })
        probes.append({
            "candidate": probe.candidate,
            "literal": probe.literal,
            "locator_kind": "regex",
            "locator_pattern": probe.locator_pattern,
            "anchor_group": probe.anchor_group,
            "rule_ids": list(probe.rule_ids),
            "hit_count": len(hits),
            "hits": hits,
        })

    return {
        "bundle": str(bundle_path),
        "bundle_sha256": sha256_file(bundle_path),
        "bundle_bytes": len(src),
        "analyzer_sha256": sha256_file(
            pathlib.Path(__file__).with_name("claude_bundle_reachability.py")),
        "entry_symbol": index.entry_name(),
        "declaration_segments": len(index.statements),
        "top_level_symbols": len(index.by_name),
        "sink_total": len(sinks),
        "sinks": sinks[:200],
        "probes": probes,
        "limitations": [
            "声明分段是作用域近似，nearest_symbol 表示写入点之前最近的顶层符号，"
            "不等于严格意义上的所属函数。",
            "sinks_within_window 只说明写入点附近存在出站动作，不构成数据流证明。",
            "α-归一化摘要跨平台稳定，可用于比对 Darwin 与 Linux 是否同一逻辑。",
            "结构化 regex 探针只证明命名回溯所约束的局部语义；"
            "其中的 minify 符号名不得被当成跨版本接口。",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", required=True, type=pathlib.Path,
                    help="Claude Code 官方二进制路径")
    ap.add_argument("--expected-sha256",
                    help="期望的二进制 SHA-256；不匹配即失败")
    ap.add_argument("--out-dir", type=pathlib.Path,
                    help="提取模块的输出目录；省略则不落盘")
    ap.add_argument("--emit-anchors", type=pathlib.Path,
                    help="写出产物锚点 JSON 的路径")
    ap.add_argument("--check-only", action="store_true",
                    help="只做结构校验，不落盘也不写锚点")
    ap.add_argument("--emit-reachability-index", type=pathlib.Path,
                    help="写出 sink 索引与候选语义锚点 JSON；需要同时给 --out-dir")
    args = ap.parse_args()

    if args.emit_reachability_index and not args.out_dir:
        print("--emit-reachability-index 需要 --out-dir（索引基于提取出的主 bundle）",
              file=sys.stderr)
        return 2

    try:
        result = extract(args.binary, args.expected_sha256)
    except ExtractError as exc:
        print(f"提取失败：{exc}", file=sys.stderr)
        return 1

    if args.out_dir and not args.check_only:
        write_modules(args.binary, result, args.out_dir)
    if args.emit_anchors and not args.check_only:
        args.emit_anchors.parent.mkdir(parents=True, exist_ok=True)
        args.emit_anchors.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.emit_reachability_index and not args.check_only:
        bundle = args.out_dir / pathlib.PurePosixPath(MAIN_MODULE).name
        index = build_reachability_index(bundle)
        args.emit_reachability_index.parent.mkdir(parents=True, exist_ok=True)
        args.emit_reachability_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"可达性索引：顶层符号 {index['top_level_symbols']:,}，"
              f"sink {index['sink_total']:,}，入口 {index['entry_symbol']}")
        for probe in index["probes"]:
            print(f"  {probe['candidate']:32s} 命中 {probe['hit_count']}")

    main_mod = next(m for m in result["modules"] if m["name"] == MAIN_MODULE)
    print(f"容器 {result['container']}  section {result['bun_section']} "
          f"offset={result['bun_section_offset']} size={result['bun_section_size']}")
    print(f"模块数 {len(result['modules'])}；主模块 {main_mod['contents_length']:,} B "
          f"sha256={main_mod['sha256'][:16]}…")
    for mod in result["modules"]:
        print(f"  [{mod['index']:2d}] {mod['kind']:6s} {mod['contents_length']:>11,} B  "
              f"{mod['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
