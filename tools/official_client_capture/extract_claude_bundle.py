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

模块图（Claude 升级控制面设计 B2-02）
------------------------------------
2.1.220 是 8 个模块的单主 bundle（主模块 ``/$bunfs/root/src/entrypoints/cli.js``）；2.1.245 起改为
Bun 分块打包：上千个 ``chunk-*.js`` 加若干入口、资产与原生模块。提取不再要求旧主模块存在，而是对
每个模块解码记录尾部的 Bun 标志（encoding／loader／module_format），归类为入口、chunk、JS 模块、
资产或原生模块，并输出 ``module_graph``。入口规则：

1. 存在旧主模块路径即唯一入口（单主模块退化，与旧产物同口径）；
2. 否则取非 ``chunk-*``、非资产、非 ``.node`` 的 JS 文本模块为入口候选，由 AST 阶段
   （``claude_bundle_ast.mjs --bundle-manifest``）按跨模块边确认；
3. 候选为空时全部 chunk 记「未决」。

模块按 ``modules/<序号:04d>-<basename>`` 落盘（分块 bundle 的 basename 可能重名）；
``bundle_manifest`` 给 AST 与词法索引一份带逐模块摘要的模块清单，二者都以清单文件的 SHA-256
作为 bundle 身份。

用法：
  # 提取并写出锚点
  python3 extract_claude_bundle.py --binary <claude> --out-dir <目录> \\
      --expected-sha256 674f61f2... --emit-anchors anchors.json

  # 分块 bundle：同时写出模块清单与多模块词法索引
  python3 extract_claude_bundle.py --binary <claude> --out-dir <目录>/modules \\
      --emit-bundle-manifest <目录>/bundle-manifest.json \\
      --emit-reachability-index <目录>/reachability-index.json

  # 只校验结构、不落盘，打印模块图（CI 门禁用）
  python3 extract_claude_bundle.py --binary <claude> --check-only
"""

from __future__ import annotations

import argparse
import bisect
import collections
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
# 其后 16 字节原样记入锚点（``offsets_reserved_hex``），格式变化时可被检出。其中前 4 字节是
# Bun 声明的入口模块序号（entry_point_id）：2.1.220 为 0（旧主模块），2.1.285 为 5（``/$bunfs/root/cli``），
# 模块图只把它记作 ``declared_entry_index`` 供核对，不据此判定入口。
OFFSETS_SIZE = 32

# 模块表每条记录长度。前 24 字节为 name/contents/sourcemap 三组 StringPointer，
# 其余 28 字节原样记入锚点（``record_tail_hex``）：三组 StringPointer（bytecode、module_info、
# bytecode_origin_path）之后是 4 个单字节枚举 encoding／loader／module_format／side。
# 2.1.220 linux-x64 与 2.1.285 linux-arm64 实测：JS 模块 loader=1、原生模块 loader=10、
# 嵌入文件 loader=5、文本资产 loader=13，与文件类型逐一吻合。
ENTRY_SIZE = 52
RECORD_FLAGS_OFFSET = 24 + 24

# 旧版（≤2.1.226）单主 bundle 的主模块路径。存在即按入口规则 ① 作唯一入口（单主模块退化）；
# 分块 bundle 没有这个模块。
MAIN_MODULE = "/$bunfs/root/src/entrypoints/cli.js"

# Bun 记录尾部枚举的取值表。只登记已知取值，未知值说明打包格式变化，提取必须中止。
# encoding=2 在 2.1.285 的文本资产里实测为 UTF-16LE 内容。
BUN_ENCODINGS = {0: "binary", 1: "latin1", 2: "utf16"}
BUN_LOADERS = {
    0: "jsx", 1: "js", 2: "ts", 3: "tsx", 4: "css", 5: "file", 6: "json", 7: "jsonc",
    8: "toml", 9: "wasm", 10: "napi", 11: "base64", 12: "dataurl", 13: "text", 14: "bunsh",
    15: "sqlite", 16: "sqlite_embedded", 17: "html", 18: "yaml",
}
BUN_MODULE_FORMATS = {0: "none", 1: "esm", 2: "cjs"}
# 作为代码执行、需要做 AST 的 loader；原生边界的 loader。其余 loader 一律是资产（嵌入文件、文本、样式等）。
JS_LOADERS = frozenset({"jsx", "js", "ts", "tsx"})
NATIVE_LOADERS = frozenset({"napi", "wasm"})
# JS 模块只接受能逐字符还原的编码：latin1 一字节一字符，utf16 按小端两字节一单元。
TEXT_ENCODINGS = frozenset({"latin1", "utf16"})

# 模块角色。entry 只出现在规则 ①；entry_candidate 只出现在规则 ②，由 AST 阶段确认。
ROLE_ENTRY = "entry"
ROLE_ENTRY_CANDIDATE = "entry_candidate"
ROLE_CHUNK = "chunk"
ROLE_JS_MODULE = "js_module"
ROLE_ASSET = "asset"
ROLE_NATIVE = "native"
JS_ROLES = frozenset({ROLE_ENTRY, ROLE_ENTRY_CANDIDATE, ROLE_CHUNK, ROLE_JS_MODULE})

MODULE_GRAPH_SCHEMA = "claude-code-bun-module-graph/v1"
BUNDLE_MANIFEST_SCHEMA = "claude-code-bun-bundle-manifest/v1"
# 入口规则与确认状态的取值。
ENTRY_RULE_LEGACY = "legacy_main_module"
ENTRY_RULE_CANDIDATES = "non_chunk_js_candidates"
ENTRY_RULE_NONE = "no_entry_candidates"


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
            # 尾部 28 字节原样保留，供跨版本比对格式是否变化；其中的 encoding／loader／
            # module_format 由 build_module_graph 解码。
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


def _is_text_str(text: str) -> bool:
    """对已解码文本做与 ``is_text`` 同口径的粗判（取前 256 个字符）。"""
    head = text[:256]
    printable = sum(" " <= ch < "\x7f" or ch in "\t\n\r" for ch in head)
    return bool(head) and printable >= len(head) * 0.9


def decode_record_flags(record_tail_hex: str) -> dict:
    """解码模块记录尾部的 4 个单字节枚举，返回 encoding／loader／module_format 的名字。

    未登记的取值说明 Bun 打包格式变化，按「结构不符必须中止」的原则抛出 ExtractError。
    """
    try:
        tail = bytes.fromhex(record_tail_hex)
    except ValueError as exc:
        raise ExtractError(f"模块记录尾部不是十六进制：{record_tail_hex!r}") from exc
    if len(tail) != ENTRY_SIZE - 24:
        raise ExtractError(f"模块记录尾部长度 {len(tail)} 不是 {ENTRY_SIZE - 24}")
    flags = tail[RECORD_FLAGS_OFFSET - 24:RECORD_FLAGS_OFFSET - 24 + 3]
    decoded = {}
    for label, value, table in (
        ("encoding", flags[0], BUN_ENCODINGS),
        ("loader", flags[1], BUN_LOADERS),
        ("module_format", flags[2], BUN_MODULE_FORMATS),
    ):
        if value not in table:
            raise ExtractError(f"未登记的 Bun {label} 取值 {value}，打包格式可能已变化")
        decoded[label] = table[value]
    return decoded


def module_role(module: dict, *, legacy_main_present: bool) -> str:
    """按 loader 与模块名归类模块角色（不含入口确认，规则见模块文档）。"""
    name = module["name"]
    basename = pathlib.PurePosixPath(name).name
    loader = module["loader"]
    if loader in NATIVE_LOADERS or basename.endswith(".node"):
        return ROLE_NATIVE
    if loader not in JS_LOADERS:
        return ROLE_ASSET
    if basename.startswith("chunk-"):
        return ROLE_CHUNK
    if legacy_main_present:
        return ROLE_ENTRY if name == MAIN_MODULE else ROLE_JS_MODULE
    return ROLE_ENTRY_CANDIDATE


def build_module_graph(modules: list[dict], offsets_reserved_hex: str) -> dict:
    """为模块表补 encoding／loader／module_format／role 字段，并返回模块图摘要。

    纯函数（只读模块记录与 trailer 字段），可直接用真实模块表夹具测试，不需要二进制。
    """
    names = [module["name"] for module in modules]
    if len(set(names)) != len(names):
        raise ExtractError("模块表中存在重名模块")
    for module in modules:
        module.update(decode_record_flags(module["record_tail_hex"]))
    legacy_main_present = MAIN_MODULE in names
    for module in modules:
        module["role"] = module_role(module, legacy_main_present=legacy_main_present)
        if module["role"] == ROLE_ENTRY and module["loader"] not in JS_LOADERS:
            raise ExtractError("旧主模块不是 JS 模块，提取结果不可信")

    by_role = collections.Counter(module["role"] for module in modules)
    chunks = [module["name"] for module in modules if module["role"] == ROLE_CHUNK]
    if legacy_main_present:
        entry_rule = ENTRY_RULE_LEGACY
        confirmation = "legacy_main_module"
        candidates = [MAIN_MODULE]
        unresolved: list[str] = []
    else:
        candidates = [
            module["name"] for module in modules if module["role"] == ROLE_ENTRY_CANDIDATE
        ]
        if candidates:
            entry_rule = ENTRY_RULE_CANDIDATES
            confirmation = "pending_ast"
            unresolved = []
        else:
            # 设计规则 ③：没有任何入口候选时，全部 chunk 记「未决」，不猜测哪个 chunk 是入口。
            entry_rule = ENTRY_RULE_NONE
            confirmation = "unresolved"
            unresolved = list(chunks)

    reserved = bytes.fromhex(offsets_reserved_hex)
    declared_index = struct.unpack_from("<I", reserved, 0)[0] if len(reserved) >= 4 else None
    declared_name = (
        names[declared_index]
        if declared_index is not None and declared_index < len(names)
        else None
    )
    return {
        "schema_version": MODULE_GRAPH_SCHEMA,
        "entry_rule": entry_rule,
        "entry_confirmation": confirmation,
        "entry_candidates": candidates,
        "declared_entry_index": declared_index,
        "declared_entry": declared_name,
        "unresolved": unresolved,
        "module_count": len(modules),
        "js_module_count": sum(by_role[role] for role in JS_ROLES),
        "chunk_count": by_role[ROLE_CHUNK],
        "asset_count": by_role[ROLE_ASSET],
        "native_count": by_role[ROLE_NATIVE],
        "unresolved_count": len(unresolved),
        "role_counts": dict(sorted(by_role.items())),
        "loader_counts": dict(sorted(collections.Counter(m["loader"] for m in modules).items())),
    }


def decode_module_text(data: bytes, encoding: str) -> str:
    """按模块表登记的编码还原 JS 文本；AST 工具用同一口径（latin1／utf16le）解码。

    utf16 严格解码：含孤立代理项的模块无法写成 UTF-8 证据，直接失败，不在后续 JSON 输出处才暴露。
    """
    if encoding == "latin1":
        return data.decode("latin-1")
    if encoding == "utf16":
        if len(data) % 2:
            raise ExtractError("utf16 模块字节数不是偶数")
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ExtractError("utf16 模块不是合法的 UTF-16LE 文本（含孤立代理项？）") from exc
    raise ExtractError(f"模块编码 {encoding} 不能作为 JS 文本解析")


def extract(binary: pathlib.Path, expected_sha: str | None) -> dict:
    binary_sha = sha256_file(binary)
    if expected_sha and binary_sha != expected_sha:
        raise ExtractError(
            f"二进制 SHA-256 不匹配：期望 {expected_sha}，实际 {binary_sha}")

    container, sec_name, sec_off, sec_size = locate_bun_section(binary)
    blob = binary.read_bytes()
    graph = parse_module_graph(blob, sec_off, sec_size)
    base = graph["blob_base"]

    for mod in graph["modules"]:
        start = base + mod["contents_offset"]
        data = blob[start:start + mod["contents_length"]]
        mod["sha256"] = hashlib.sha256(data).hexdigest()
        mod["kind"] = "text" if is_text(data) else "native"
    module_graph = build_module_graph(graph["modules"], graph["offsets_reserved_hex"])
    # 要做 AST 的模块必须能按登记编码还原成文本：取代旧版只检查主模块的「主模块不是文本」。
    for mod in graph["modules"]:
        if mod["role"] not in JS_ROLES:
            continue
        if mod["encoding"] not in TEXT_ENCODINGS:
            raise ExtractError(f"JS 模块 {mod['name']} 的编码 {mod['encoding']} 不是文本编码")
        start = base + mod["contents_offset"]
        text = decode_module_text(blob[start:start + mod["contents_length"]], mod["encoding"])
        if not _is_text_str(text):
            raise ExtractError(f"JS 模块 {mod['name']} 不是文本，提取结果不可信")

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
        "module_graph": module_graph,
    }


def module_file_name(module: dict) -> str:
    """落盘文件名 ``<序号:04d>-<basename>``：分块 bundle 的 basename 可能重名，序号保证唯一。"""
    basename = pathlib.PurePosixPath(module["name"]).name
    if basename in {"", ".", ".."}:
        raise ExtractError(f"模块名没有可落盘的 basename：{module['name']!r}")
    return f"{module['index']:04d}-{basename}"


def write_modules(binary: pathlib.Path, result: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = binary.read_bytes()
    base = result["blob_base"]
    for mod in result["modules"]:
        start = base + mod["contents_offset"]
        data = blob[start:start + mod["contents_length"]]
        # 模块名是打包前的绝对路径，只取 basename 并加序号前缀落盘，避免写出输出目录或互相覆盖。
        target = out_dir / module_file_name(mod)
        target.write_bytes(data)
        # 只记文件名：输出目录是调用方环境，写进锚点会让同一二进制在不同目录下
        # 算出不同的锚点，破坏复算。
        mod["extracted_name"] = target.name


def bundle_manifest(result: dict, manifest_dir: pathlib.Path, modules_dir: pathlib.Path) -> dict:
    """生成 AST 与词法索引共用的模块清单（必须在 ``write_modules`` 之后调用）。

    每个模块登记角色、编码、摘要与相对 ``manifest_dir`` 的落盘路径；``analyze`` 标出要做
    AST 与词法分析的 JS 模块。资产与原生模块也列出，供跨模块边识别资产引用与原生边界。
    清单不含工具摘要与绝对路径，同一二进制在不同目录下得到同一清单摘要。
    """
    try:
        relative_dir = modules_dir.resolve().relative_to(manifest_dir.resolve())
    except ValueError as exc:
        raise ExtractError("模块目录必须位于模块清单所在目录之下") from exc
    rows = []
    for mod in result["modules"]:
        if "extracted_name" not in mod:
            raise ExtractError("生成模块清单前必须先落盘模块")
        rows.append({
            "index": mod["index"],
            "name": mod["name"],
            "role": mod["role"],
            "loader": mod["loader"],
            "encoding": mod["encoding"],
            "module_format": mod["module_format"],
            "sha256": mod["sha256"],
            "bytes": mod["contents_length"],
            "analyze": mod["role"] in JS_ROLES,
            "path": (relative_dir / mod["extracted_name"]).as_posix(),
        })
    return {
        "schema_version": BUNDLE_MANIFEST_SCHEMA,
        "binary_sha256": result["binary_sha256"],
        "module_graph": result["module_graph"],
        "modules": rows,
    }


def load_bundle_manifest(manifest_path: pathlib.Path) -> tuple[dict, str]:
    """读取并校验模块清单，返回（清单，清单文件 SHA-256）。"""
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractError(f"模块清单不是合法 JSON：{manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BUNDLE_MANIFEST_SCHEMA:
        raise ExtractError(f"模块清单 schema 不匹配：{manifest_path}")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ExtractError("模块清单缺少 modules")
    names = set()
    for row in modules:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ExtractError("模块清单行非法")
        if row["name"] in names:
            raise ExtractError(f"模块清单重名：{row['name']}")
        names.add(row["name"])
        relative = pathlib.PurePosixPath(str(row.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ExtractError(f"模块清单路径非法：{row.get('path')!r}")
        if row.get("analyze") is True and row.get("role") not in JS_ROLES:
            raise ExtractError(f"模块清单把非 JS 模块标为待分析：{row['name']}")
    return manifest, hashlib.sha256(raw).hexdigest()


def read_manifest_module_text(manifest_path: pathlib.Path, row: dict) -> str:
    """读取清单中一个待分析模块：核对字节数与摘要后按登记编码解码。"""
    path = manifest_path.parent / pathlib.PurePosixPath(row["path"])
    data = path.read_bytes()
    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ExtractError(f"模块文件与清单登记不一致：{row['name']}")
    return decode_module_text(data, row["encoding"])


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


def _reachability_module():
    """加载词法分析器：包方式导入（claude_fw_e、测试）与脚本方式运行（脚本目录在 sys.path）都要可用。"""
    try:
        from tools.official_client_capture import claude_bundle_reachability as module
    except ImportError:
        import claude_bundle_reachability as module  # type: ignore[no-redef]
    return module


_ASTRAL_RE = re.compile("[\U00010000-\U0010FFFF]")


def utf16_offset_mapper(src: str):
    """返回把 Python 码点偏移换算成 UTF-16 码元偏移的函数。

    Python 字符串按码点计数，TypeScript 按 UTF-16 码元计数，二者只在 BMP 之外的字符
    （每个占两个码元）处分叉：UTF-16 偏移 = 码点偏移 + 其前的增补平面字符数。
    """
    astral = [match.start() for match in _ASTRAL_RE.finditer(src)]
    if not astral:
        return lambda offset: offset
    return lambda offset: offset + bisect.bisect_left(astral, offset)


def _index_source(src: str, index, module_name: str | None) -> tuple[list, list]:
    """对一段源码收集词法 sink 与候选探针命中；``module_name`` 非空时每行加 ``module``。

    单文件与模块清单两种模式共用本函数，单文件模式的输出与旧实现逐字段一致。
    """
    SINK_PATTERNS = _reachability_module().SINK_PATTERNS
    to_utf16 = utf16_offset_mapper(src) if module_name is not None else None

    sinks = []
    for kind, pat in SINK_PATTERNS.items():
        for m in pat.finditer(src):
            sym = index.enclosing_symbol(m.start())
            row = {"kind": kind, "offset": m.start(),
                   "nearest_symbol": sym["name"] if sym else None}
            if module_name is not None:
                row["module"] = module_name
                row["offset_utf16"] = to_utf16(m.start())
            sinks.append(row)

    probes = []
    for probe_id, literal in CANDIDATE_PROBES:
        hits = []
        pos = src.find(literal)
        while pos >= 0:
            text, digest = index.alpha_normalize_around(
                pos, before=ANCHOR_TOKENS, after=ANCHOR_TOKENS)
            sym = index.enclosing_symbol(pos)
            window = src[max(0, pos - SINK_WINDOW):pos + SINK_WINDOW]
            hit = {
                "offset": pos,
                "nearest_symbol": sym["name"] if sym else None,
                "alpha_sha256": digest,
                "alpha_text": text[:600],
                "sinks_within_window": sorted(
                    k for k, p in SINK_PATTERNS.items() if p.search(window)),
            }
            if module_name is not None:
                hit["module"] = module_name
            hits.append(hit)
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
            hit = {
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
            }
            if module_name is not None:
                hit["module"] = module_name
            hits.append(hit)
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
    return sinks, probes


_LIMITATIONS = [
    "声明分段是作用域近似，nearest_symbol 表示写入点之前最近的顶层符号，"
    "不等于严格意义上的所属函数。",
    "sinks_within_window 只说明写入点附近存在出站动作，不构成数据流证明。",
    "α-归一化摘要跨平台稳定，可用于比对 Darwin 与 Linux 是否同一逻辑。",
    "结构化 regex 探针只证明命名回溯所约束的局部语义；"
    "其中的 minify 符号名不得被当成跨版本接口。",
]


def build_reachability_index(bundle_path: pathlib.Path) -> dict:
    """生成 sink 索引与候选锚点。

    参数是单个 bundle 文件时按旧口径处理（输出与旧实现逐字段一致）；是 ``bundle_manifest``
    写出的模块清单（``.json``）时逐模块处理全部 JS 模块，每条 sink 与命中带 ``module``，
    sink 另带 ``offset_utf16`` 供与 TypeScript 坐标合并。
    """
    if bundle_path.suffix == ".json":
        return _build_manifest_reachability_index(bundle_path)
    BundleIndex = _reachability_module().BundleIndex

    src = bundle_path.read_text(encoding="utf-8")
    index = BundleIndex(src)
    sinks, probes = _index_source(src, index, None)

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
        # sink inventory 是 FW-E 闭集分母，禁止以展示便利为由截断。旧实现只保存前
        # 200 条，目标 bundle 命中超过该数量时会让新增调用点永久失去 disposition。
        "sinks": sinks,
        "sink_inventory_truncated": False,
        "probes": probes,
        "limitations": list(_LIMITATIONS),
    }


def bundle_identity(manifest: dict) -> tuple[str, dict | None]:
    """模块清单对应的 bundle 身份：返回（身份类别，旧主模块清单行或 None）。

    规则 ①（旧版单主 bundle）的身份保持旧含义，即旧主模块文件本身（路径、字节数、内容摘要与旧产物相同）；
    分块 bundle 没有单一主文件，身份是模块清单文件。AST、词法索引、inventory 与 containment 都按本函数取身份，
    并且一律另外绑定清单摘要（``bundle_manifest_sha256``／``bundle.manifest_sha256``）。
    """
    graph = manifest.get("module_graph") or {}
    if graph.get("entry_rule") != ENTRY_RULE_LEGACY:
        return "bundle_manifest", None
    rows = [row for row in manifest["modules"] if row.get("name") == MAIN_MODULE]
    if len(rows) != 1 or rows[0].get("analyze") is not True:
        raise ExtractError("规则 ① 的模块清单缺少待分析的旧主模块")
    return "legacy_main_module", rows[0]


def _build_manifest_reachability_index(manifest_path: pathlib.Path) -> dict:
    """按模块清单逐模块建立词法索引；一次只持有一个模块的 BundleIndex，内存以最大模块为上界。

    旧字段的含义不变：规则 ①（旧版单主 bundle）时 ``bundle``、``bundle_sha256``、``bundle_bytes``、
    ``entry_symbol``、``declaration_segments``、``top_level_symbols`` 只取旧主模块，与单文件旧口径相同；
    分块 bundle 时它们指模块清单与全部模块合计。``sinks``／``probes`` 始终覆盖全部 JS 模块（规则 ① 时
    旧主模块的行排在最前、与旧口径逐行相同，其它模块的行只是追加）。新增字段一律追加在 ``limitations`` 之后。
    """
    BundleIndex = _reachability_module().BundleIndex
    manifest, manifest_sha256 = load_bundle_manifest(manifest_path)
    graph = manifest.get("module_graph") or {}
    identity_kind, main_row = bundle_identity(manifest)
    rows = sorted(
        (row for row in manifest["modules"] if row.get("analyze") is True),
        key=lambda item: item["index"],
    )
    if main_row is not None:
        rows = [main_row, *(row for row in rows if row["name"] != MAIN_MODULE)]
        primary = MAIN_MODULE
    elif graph.get("declared_entry") in (graph.get("entry_candidates") or []):
        # 分块 bundle 的顶层 entry_symbol 取 Bun 声明的入口候选末尾的入口调用；入口是否成立以 AST 确认为准。
        primary = graph.get("declared_entry")
    else:
        primary = None
    sinks: list[dict] = []
    probe_rows: dict[str, dict] = {}
    probe_order: list[str] = []
    module_rows = []
    totals = {"text_length": 0, "declaration_segments": 0, "top_level_symbols": 0}
    entry_symbol = None
    for row in rows:
        src = read_manifest_module_text(manifest_path, row)
        index = BundleIndex(src)
        module_sinks, module_probes = _index_source(src, index, row["name"])
        sinks.extend(module_sinks)
        for probe in module_probes:
            key = probe["candidate"]
            if key not in probe_rows:
                probe_rows[key] = {**probe, "hits": []}
                probe_order.append(key)
            probe_rows[key]["hits"].extend(probe["hits"])
        module_entry = index.entry_name()
        if row["name"] == primary:
            entry_symbol = module_entry
        module_rows.append({
            "name": row["name"],
            "index": row["index"],
            "role": row["role"],
            "sha256": row["sha256"],
            "bytes": row["bytes"],
            "text_length": len(src),
            "entry_symbol": module_entry,
            "declaration_segments": len(index.statements),
            "top_level_symbols": len(index.by_name),
            "sink_count": len(module_sinks),
        })
        totals["text_length"] += len(src)
        totals["declaration_segments"] += len(index.statements)
        totals["top_level_symbols"] += len(index.by_name)
        del index, src
    probes = []
    for key in probe_order:
        probe = probe_rows[key]
        probe["hit_count"] = len(probe["hits"])
        probes.append(probe)
    if main_row is not None:
        main_stats = module_rows[0]
        bundle_path = str(manifest_path.parent / pathlib.PurePosixPath(main_row["path"]))
        bundle_sha256 = main_row["sha256"]
        scoped = {key: main_stats[key] for key in ("text_length", "declaration_segments", "top_level_symbols")}
        scope = "entry_module"
    else:
        bundle_path = str(manifest_path)
        bundle_sha256 = manifest_sha256
        scoped = dict(totals)
        scope = "all_analyzed_modules"
    return {
        "bundle": bundle_path,
        "bundle_sha256": bundle_sha256,
        "bundle_bytes": scoped["text_length"],
        "analyzer_sha256": sha256_file(
            pathlib.Path(__file__).with_name("claude_bundle_reachability.py")),
        "entry_symbol": entry_symbol,
        "declaration_segments": scoped["declaration_segments"],
        "top_level_symbols": scoped["top_level_symbols"],
        "sink_total": len(sinks),
        "sinks": sinks,
        "sink_inventory_truncated": False,
        "probes": probes,
        "limitations": list(_LIMITATIONS),
        "bundle_kind": "bun_module_manifest",
        "bundle_identity_kind": identity_kind,
        "bundle_manifest": str(manifest_path),
        "bundle_manifest_sha256": manifest_sha256,
        "offset_unit": "codepoint",
        "top_level_count_scope": scope,
        "all_modules_counts": {"analyzed_module_count": len(module_rows), **totals},
        "module_graph": graph,
        "modules": module_rows,
        "multi_module_limitations": [
            "多模块索引逐模块分段，nearest_symbol 只在所属模块内有意义；"
            "offset 按码点、offset_utf16 按 UTF-16 码元，与 AST 坐标合并时用后者。",
            "bundle、bundle_sha256、bundle_bytes、entry_symbol、declaration_segments、top_level_symbols 的口径见 "
            "top_level_count_scope 与 bundle_identity_kind：规则 ① 只取旧主模块（与旧单文件产物同义），"
            "分块 bundle 指模块清单与全部模块合计；全部模块合计另见 all_modules_counts。",
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
                    help="写出 sink 索引与候选语义锚点 JSON；需要同时给 --out-dir。"
                         "旧版单主 bundle 不给 --emit-bundle-manifest 时只索引主模块（旧口径），"
                         "分块 bundle 必须同时给 --emit-bundle-manifest")
    ap.add_argument("--emit-bundle-manifest", type=pathlib.Path,
                    help="写出 AST 与词法索引共用的模块清单 JSON；需要同时给 --out-dir，"
                         "且 --out-dir 必须位于清单所在目录之下")
    args = ap.parse_args()

    if (args.emit_reachability_index or args.emit_bundle_manifest) and not args.out_dir:
        print("--emit-reachability-index／--emit-bundle-manifest 需要 --out-dir"
              "（索引与清单基于提取出的模块文件）", file=sys.stderr)
        return 2

    try:
        result = extract(args.binary, args.expected_sha256)
    except ExtractError as exc:
        print(f"提取失败：{exc}", file=sys.stderr)
        return 1
    graph = result["module_graph"]
    if (args.emit_reachability_index and not args.emit_bundle_manifest
            and graph["entry_rule"] != ENTRY_RULE_LEGACY and not args.check_only):
        print("分块 bundle 没有旧主模块，--emit-reachability-index 必须同时给 "
              "--emit-bundle-manifest", file=sys.stderr)
        return 2

    if args.out_dir and not args.check_only:
        write_modules(args.binary, result, args.out_dir)
    if args.emit_anchors and not args.check_only:
        args.emit_anchors.parent.mkdir(parents=True, exist_ok=True)
        args.emit_anchors.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.emit_bundle_manifest and not args.check_only:
        try:
            manifest = bundle_manifest(result, args.emit_bundle_manifest.parent, args.out_dir)
        except ExtractError as exc:
            print(f"生成模块清单失败：{exc}", file=sys.stderr)
            return 1
        args.emit_bundle_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.emit_bundle_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.emit_reachability_index and not args.check_only:
        if args.emit_bundle_manifest:
            index = build_reachability_index(args.emit_bundle_manifest)
        else:
            main_mod = next(m for m in result["modules"] if m["name"] == MAIN_MODULE)
            index = build_reachability_index(args.out_dir / main_mod["extracted_name"])
        args.emit_reachability_index.parent.mkdir(parents=True, exist_ok=True)
        args.emit_reachability_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"可达性索引：顶层符号 {index['top_level_symbols']:,}，"
              f"sink {index['sink_total']:,}，入口 {index['entry_symbol']}")
        for probe in index["probes"]:
            print(f"  {probe['candidate']:32s} 命中 {probe['hit_count']}")

    print(f"容器 {result['container']}  section {result['bun_section']} "
          f"offset={result['bun_section_offset']} size={result['bun_section_size']}")
    print(f"模块数 {graph['module_count']}；JS 模块 {graph['js_module_count']}，"
          f"chunk {graph['chunk_count']}，资产 {graph['asset_count']}，"
          f"原生 {graph['native_count']}；入口规则 {graph['entry_rule']}，"
          f"入口候选 {len(graph['entry_candidates'])}，未决 {graph['unresolved_count']}")
    # chunk 数以千计，逐行只列非 chunk 模块；完整模块图以 JSON 输出。
    for mod in result["modules"]:
        if mod["role"] == ROLE_CHUNK:
            continue
        print(f"  [{mod['index']:4d}] {mod['role']:15s} {mod['loader']:6s} "
              f"{mod['contents_length']:>11,} B  {mod['name']}")
    if args.check_only:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
