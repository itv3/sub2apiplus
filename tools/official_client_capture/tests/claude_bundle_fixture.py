"""Claude bundle 模块图测试的合成夹具（Claude 升级控制面设计 B2-12）。

按 Bun SEA 的真实布局合成最小 ELF64：``.bun`` section 内依次是 8 字节前缀、模块内容与模块名、
52 字节一条的模块表、32 字节 Offsets（含入口序号）与 trailer magic；模块表记录尾部的
encoding／loader／module_format 与 2.1.220、2.1.285 实测取值一致。合成 JS 全部是测试自写的
最小代码，只模仿分块 bundle 的结构（``/$bunfs/root/chunk-*.js`` 绝对说明符、``import.meta.require``
别名、``export{...}``），不含任何官方源码。

三种形态：
- ``legacy_single_main_modules``：2.1.220 形态（旧主模块 + napi 包装模块 + 资产 + 原生模块）；
- ``chunked_modules``：2.1.285 形态（入口候选、chunk、Worker 路径引用、require 别名、动态加载、孤立 chunk、
  原生边界、UTF-16 编码模块里的增补平面字符）；
- ``chunked_without_candidates``：入口候选为空的规则 ③ 形态（设计按 2.1.245 推测如此；实测 2.1.245 与 2.1.285
  一样有入口候选，规则 ③ 目前只由本合成形态覆盖）。

另提供真实夹具与真实 AST 回归测试共用的口径：旧字段逐字段投影摘要（``legacy_field_digests``）与跨模块边行摘要
（``edge_rows_sha256``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAGIC = b"\n---- Bun! ----\n"
# Bun 记录尾部枚举：encoding（0 binary／1 latin1／2 utf16）、loader（1 js／5 file／10 napi／13 text）、
# module_format（0 none／1 esm／2 cjs）。
LATIN1, UTF16 = 1, 2
LOADER_JS, LOADER_FILE, LOADER_NAPI, LOADER_TEXT = 1, 5, 10, 13
ESM, CJS, NONE = 1, 2, 0
TYPESCRIPT_SHA256 = "f316520790d4db220a10d890c5f85310e26a1bd3c104b8d3b5eb62ba0491651b"


@dataclass(frozen=True)
class FixtureModule:
    name: str
    contents: bytes
    encoding: int
    loader: int
    module_format: int


def js(name: str, source: str, *, module_format: int = ESM) -> FixtureModule:
    """latin1 编码的 JS 模块（Bun 把 ASCII 源码登记为 latin1）。"""

    return FixtureModule(name, source.encode("latin-1"), LATIN1, LOADER_JS, module_format)


def js_utf16(name: str, source: str) -> FixtureModule:
    """UTF-16LE 编码的 JS 模块：用于覆盖增补平面字符的偏移单位换算。"""

    return FixtureModule(name, source.encode("utf-16-le"), UTF16, LOADER_JS, ESM)


def native(name: str) -> FixtureModule:
    return FixtureModule(name, b"\x7fELF\x02\x01\x01" + b"\0" * 57 + b"napi-fixture", 0, LOADER_NAPI, NONE)


def file_asset(name: str, contents: bytes) -> FixtureModule:
    return FixtureModule(name, contents, 0, LOADER_FILE, NONE)


def text_asset(name: str, text: str) -> FixtureModule:
    return FixtureModule(name, text.encode("latin-1"), LATIN1, LOADER_TEXT, NONE)


def bun_section(modules: list[FixtureModule], *, entry_point_id: int) -> bytes:
    """按 Bun StandaloneModuleGraph 布局合成 ``.bun`` section 内容。"""

    blob = bytearray()
    records = []
    for module in modules:
        contents_offset = len(blob)
        blob += module.contents
        name = module.name.encode("utf-8")
        name_offset = len(blob)
        blob += name
        records.append((name_offset, len(name), contents_offset, len(module.contents), module))
    table_offset = len(blob)
    for name_offset, name_length, contents_offset, contents_length, module in records:
        blob += struct.pack("<IIIIII", name_offset, name_length, contents_offset, contents_length, 0, 0)
        blob += bytes(24)
        blob += bytes([module.encoding, module.loader, module.module_format, 0])
    byte_count = len(blob)
    offsets = struct.pack("<QII", byte_count, table_offset, len(records) * 52)
    offsets += struct.pack("<IIII", entry_point_id, 0, 0, 0)
    return struct.pack("<Q", byte_count) + bytes(blob) + offsets + MAGIC


def elf_with_bun_section(section: bytes, *, machine: int = 183) -> bytes:
    """最小 ELF64：节头表含空节、``.shstrtab`` 与 ``.bun``，满足 extract_claude_bundle 的 ELF 定位。"""

    shstrtab = b"\0.shstrtab\0.bun\0"
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<HHI", header, 16, 2, machine, 1)
    body = bytearray(header)
    shstrtab_offset = len(body)
    body += shstrtab
    body += bytes((-len(body)) % 16)
    section_offset = len(body)
    body += section
    body += bytes((-len(body)) % 16)
    section_headers_offset = len(body)
    for name_index, offset, size in ((0, 0, 0), (1, shstrtab_offset, len(shstrtab)), (11, section_offset, len(section))):
        entry = bytearray(64)
        struct.pack_into("<I", entry, 0, name_index)
        struct.pack_into("<QQ", entry, 0x18, offset, size)
        body += entry
    struct.pack_into("<Q", body, 0x28, section_headers_offset)
    struct.pack_into("<HHH", body, 0x3A, 64, 3, 1)
    return bytes(body)


def write_binary(path: Path, modules: list[FixtureModule], *, entry_point_id: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = elf_with_bun_section(bun_section(modules, entry_point_id=entry_point_id))
    path.write_bytes(content)
    path.chmod(0o700)
    return hashlib.sha256(content).hexdigest()


def legacy_single_main_modules() -> tuple[list[FixtureModule], int]:
    """2.1.220 形态：旧主模块按 CJS 包装，直接 require 原生模块；另有一个没人加载的 napi 包装模块。"""

    cli = (
        "// @bun @bytecode @bun-cjs\n"
        "(function(exports, require, module, __filename, __dirname) {"
        "var Rf=fetch;"
        "function Hdr(){return {\"x-client-app\":process.env.CLAUDE_AGENT_SDK_CLIENT_APP}}"
        "async function Req(u){return Rf(u,{headers:Hdr()})}"
        "async function Msg(c){return c.beta.messages.create({model:\"m\"},{headers:Hdr()})}"
        "function Img(){return require(\"/$bunfs/root/image-processor.node\")}"
        "var Mmd=\"/$bunfs/root/mermaid.min.js\";"
        "function Docs(){return \"call fetch(x) in docs\"}"
        "function Cli(){Img();Req(\"https://api.example.test/v1/messages\");Msg(globalThis.c)}"
        "Cli();})"
    )
    wrapper = (
        "// @bun @bytecode @bun-cjs\n"
        "(function(exports, require, module, __filename, __dirname) {"
        "var i=function(){return require(\"/$bunfs/root/image-processor.node\")};"
        "module.exports=i();})"
    )
    modules = [
        js("/$bunfs/root/src/entrypoints/cli.js", cli, module_format=CJS),
        js("/$bunfs/root/image-processor.js", wrapper, module_format=CJS),
        native("/$bunfs/root/image-processor.node"),
        file_asset("/$bunfs/root/mermaid.min.js", b"/*! mermaid */ var mermaid={};\n"),
    ]
    return modules, 0


def chunked_modules() -> tuple[list[FixtureModule], int]:
    """2.1.285 形态的最小分块 bundle。

    - ``cli``：Bun 声明的入口；静态导入 chunk-a 与 wrapper.js，动态导入 chunk-b；
    - ``chunk-aaaa1111.js``：定义 ``Ie=import.meta.require`` 别名与两个 fetch（一个被入口用到、一个没人用）；
    - ``chunk-bbbb2222.js``：UTF-16 编码，含增补平面字符；经别名做字面量原生加载与非字面量 require、
      非字面量动态 import、Worker 路径字面量与 WebSocket sink；
    - ``chunk-cccc3333.js``：没人加载的孤立 chunk（入口关系未决），含一个 fetch 与一个文档字符串里的词法命中；
    - ``src/hooks-worker.js``：只被路径字面量引用的 Worker 入口；
    - ``wrapper.js``：入口候选，但被 cli 静态导入，AST 阶段否定其入口身份。
    """

    cli = (
        "// @bun @bytecode\n"
        "import{L}from\"/$bunfs/root/chunk-aaaa1111.js\";"
        "import\"/$bunfs/root/wrapper.js\";"
        "async function bt(){L();let{main:gt}=await import(\"/$bunfs/root/chunk-bbbb2222.js\");await gt()}"
        "bt();\n"
    )
    chunk_a = (
        "// @bun @bytecode\n"
        "var Ie=import.meta.require;"
        "function L(){return fetch(\"https://api.example.test/a\")}"
        "function unusedA(){return fetch(\"https://api.example.test/unused\")}"
        "export{L,Ie,unusedA};\n"
    )
    chunk_b = (
        "// @bun @bytecode\n"
        "import{Ie}from\"/$bunfs/root/chunk-aaaa1111.js\";"
        "var Emoji=\"\U0001F600\U0001F680\";"
        "var Hooks={url:\"/$bunfs/root/src/hooks-worker.js\"};"
        "function load(n){return Ie(n)}"
        "function nativeAddon(){return Ie(\"/$bunfs/root/addon.node\")}"
        "async function main(){nativeAddon();load(globalThis.p);"
        "new WebSocket(\"wss://ws.example.test/\");"
        "return import(globalThis.dynamicTarget)}"
        "export{main};\n"
    )
    chunk_c = (
        "// @bun @bytecode\n"
        "var Doc=\"call fetch(x) when ready\";"
        "function z(){return fetch(\"https://orphan.example.test/\")}"
        "export{z};\n"
    )
    worker = (
        "// @bun @bytecode\n"
        "import{L}from\"/$bunfs/root/chunk-aaaa1111.js\";"
        "function w(){L()}"
        "w();\n"
    )
    wrapper = (
        "// @bun @bytecode\n"
        "import{Ie}from\"/$bunfs/root/chunk-aaaa1111.js\";"
        "export default Ie(\"/$bunfs/root/addon.node\");\n"
    )
    modules = [
        js("/$bunfs/root/chunk-aaaa1111.js", chunk_a),
        js("/$bunfs/root/cli", cli),
        js_utf16("/$bunfs/root/chunk-bbbb2222.js", chunk_b),
        js("/$bunfs/root/chunk-cccc3333.js", chunk_c),
        js("/$bunfs/root/src/hooks-worker.js", worker),
        js("/$bunfs/root/wrapper.js", wrapper),
        native("/$bunfs/root/addon.node"),
        text_asset("/$bunfs/root/notes-abc123.md", "# notes\nfetch(not code)\n"),
        file_asset("/$bunfs/root/data-def456.txt.zst", b"\x28\xb5\x2f\xfd\x00\x00fixture"),
        file_asset("/$bunfs/root/mermaid.min.js", b"\x28\xb5\x2f\xfdmermaid"),
    ]
    return modules, 1


def chunked_without_candidates() -> tuple[list[FixtureModule], int]:
    """设计里 2.1.245 形态：只有 chunk（入口候选为空），另有资产与原生模块。"""

    chunk_x = (
        "// @bun @bytecode\n"
        "function X(){return fetch(\"https://api.example.test/x\")}"
        "export{X};\n"
    )
    chunk_y = (
        "// @bun @bytecode\n"
        "import{X}from\"/$bunfs/root/chunk-xxxx0001.js\";"
        "function Y(){return X()}"
        "Y();\n"
    )
    modules = [
        js("/$bunfs/root/chunk-xxxx0001.js", chunk_x),
        js("/$bunfs/root/chunk-yyyy0002.js", chunk_y),
        file_asset("/$bunfs/root/payload.template.html.asset", b"<html></html>"),
        native("/$bunfs/root/image-processor.node"),
    ]
    return modules, 1


# ---------------------------------------------------------------- 旧字段逐字段比较（规则 ① 的验收口径）
# 新工具在规则 ① 下只追加字段：旧对象只追加键、sink／probe 行只追加 module 与 offset_utf16。按旧产物的字段形状
# 投影新产物后逐字段求摘要，即可与改前工具的产物比较。计数映射整体比较（多一个类别是取值变化，不是追加字段）。
LEGACY_ROW_EXTRA_KEYS = frozenset({"module", "offset_utf16"})
WHOLE_VALUE_KEYS = frozenset({"sink_category_counts"})


def legacy_shape(value: dict) -> dict:
    """旧产物的字段形状：顶层键 → None（标量／列表／计数映射）或子键列表（记录型对象）。"""

    return {
        key: (sorted(item) if isinstance(item, dict) and key not in WHOLE_VALUE_KEYS else None)
        for key, item in value.items()
    }


def _strip_row(row: dict) -> dict:
    stripped = {key: item for key, item in row.items() if key not in LEGACY_ROW_EXTRA_KEYS}
    if "lexical_candidates" in stripped:
        stripped["lexical_candidates"] = [
            {key: item for key, item in candidate.items() if key not in LEGACY_ROW_EXTRA_KEYS}
            for candidate in stripped["lexical_candidates"]
        ]
    if "hits" in stripped:
        stripped["hits"] = [
            {key: item for key, item in hit.items() if key not in LEGACY_ROW_EXTRA_KEYS} for hit in stripped["hits"]
        ]
    return stripped


def project_legacy_fields(value: dict, shape: dict) -> dict:
    """按旧形状投影：只取旧字段；记录型对象只取旧子键；sinks／probes 行去掉新增键。缺失字段记为 ``<缺失>``。"""

    projected = {}
    for key, subkeys in shape.items():
        if key not in value:
            projected[key] = "<缺失>"
            continue
        item = value[key]
        if key in {"sinks", "probes"} and isinstance(item, list):
            item = [_strip_row(row) for row in item]
        elif subkeys is not None and isinstance(item, dict):
            item = {subkey: item.get(subkey, "<缺失>") for subkey in subkeys}
        projected[key] = item
    return projected


def legacy_field_digests(value: dict, shape: dict) -> dict:
    """逐字段摘要：每个旧字段按投影后的规范 JSON（键排序）求 sha256。"""

    return {
        key: hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        for key, item in project_legacy_fields(value, shape).items()
    }


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def edge_rows_sha256(rows: list[list]) -> str:
    """一组边行的摘要：每行按紧凑 JSON 编码后排序再整体编码（行里的目标可能是模块序号或外部说明符）。"""

    ordered = sorted(compact_json(row) for row in rows)
    return hashlib.sha256(("[" + ",".join(ordered) + "]").encode("utf-8")).hexdigest()


def module_edge_rows(ast: dict, index_of: dict[str, int]) -> list[list]:
    """把 AST 的 module_edges 转成夹具行 ``[from, to, kind, target_role, occurrences]``（清单内模块用序号）。"""

    return [
        [index_of[edge["from"]], index_of.get(edge["to"], edge["to"]), edge["kind"], edge["target_role"], edge["occurrences"]]
        for edge in ast["module_edges"]
    ]


def typescript_module() -> Path | None:
    """测试用 TypeScript：与 Makefile 一致优先取 CLAUDE_AST_TYPESCRIPT_MODULE，其次仓库 frontend 依赖；摘要须等于锁定值。"""

    candidates = []
    if os.environ.get("CLAUDE_AST_TYPESCRIPT_MODULE"):
        candidates.append(Path(os.environ["CLAUDE_AST_TYPESCRIPT_MODULE"]))
    candidates.append(ROOT / "frontend/node_modules/typescript/lib/typescript.js")
    for candidate in candidates:
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == TYPESCRIPT_SHA256:
            return candidate
    return None
