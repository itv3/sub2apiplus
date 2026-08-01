#!/usr/bin/env python3
"""扫描仓库中全部具备网络发送能力的调用点（sink），产出变更集 0 的 sink 台账草稿。

用途与边界
----------
本脚本是**发现工具**，不是门禁。它的职责是把"可能向外发包的代码位置"全部列出来，
供人工判定 persona 归属；判定结果最终写入 docs/changeset0/sink-inventory.md。

为什么不能靠人工梳理：三份独立的架构评审都遗漏了 openai_privacy_service.go 这条
直发 chatgpt.com 的路径，它既不含伪装符号（躲过既有台账门禁），用的又是第三套 HTTP
库（req/v3），任何按符号或按已知文件列表的梳理都发现不了。

设计取向：**宁可多报，不可漏报**。误报由人工在台账里标记为 out-of-scope 并写明理由，
漏报则会让整个 Guard 体系出现看不见的缺口。

变更集 1A 会把 sink 判据升级为 Go AST 实现并接入 CI hard-fail；本脚本的正则实现
只服务于变更集 0 的一次性枚举，两者的 sink 形态清单必须保持同步。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field, asdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = [ROOT / "backend"]

# ---------------------------------------------------------------------------
# 官方上游 host 闭集
#
# 这些 host 一旦被请求，出站身份就必须成立。判定 sink 是否 in-scope 时以此为准。
# 注意 openai.com 的子域要分开列：auth.openai.com 承载 OAuth/PAT/Agent 身份端点，
# 与 api.openai.com 的第三方 API 语义完全不同，不能合并成一条 openai.com 规则。
# ---------------------------------------------------------------------------
OFFICIAL_HOSTS = [
    "chatgpt.com",
    "auth.openai.com",
    "api.openai.com",
    "api.anthropic.com",
    "console.anthropic.com",
]

# ---------------------------------------------------------------------------
# sink 形态
#
# 每条对应文档 §9 首批 sink 清单中的一项。confidence 表示"命中即为真实发送点"的
# 把握程度：high 基本无误报；medium 需要人工确认（例如 .Get( 也可能是 map 取值）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SinkPattern:
    sink_kind: str
    regex: re.Pattern
    confidence: str
    note: str


SINK_PATTERNS: list[SinkPattern] = [
    SinkPattern(
        "http_upstream_do_with_tls",
        re.compile(r"\.DoWithTLS\s*\("),
        "high",
        "repository HTTPUpstream 带 TLS 画像发送",
    ),
    SinkPattern(
        "http_upstream_do",
        re.compile(r"\bhttpUpstream\.Do\s*\(|\.httpUpstream\.Do\s*\("),
        "high",
        "repository HTTPUpstream 不带显式 TLS 画像发送",
    ),
    SinkPattern(
        "net_http_client_do",
        re.compile(r"\bclient\.Do\s*\(|\bhttpClient\.Do\s*\(|\.GetClient\(\)\.Do\s*\("),
        "high",
        "net/http Client.Do，含 req/v3 取底层 client 后直发",
    ),
    SinkPattern(
        "req_v3_chain_start",
        re.compile(r"\.R\s*\(\s*\)"),
        "high",
        "req/v3 请求链起点，终态方法可能在后续行",
    ),
    SinkPattern(
        "req_v3_terminal",
        re.compile(
            r"\.(Get|Post|Patch|Put|Delete|Head|Options|Send)\s*\(\s*"
            r"(?:[a-zA-Z_][\w.]*URL|\"https?://|fmt\.Sprintf)"
        ),
        "medium",
        "req/v3 终态方法，按实参形似 URL 过滤",
    ),
    SinkPattern(
        "websocket_dial",
        re.compile(r"\bDial\s*\(|websocket\.Dial|coderws\.Dial|gorillaws\.Dial"),
        "medium",
        "WebSocket 拨号，含自定义 dialer 的 Dial 方法",
    ),
    SinkPattern(
        "raw_dial",
        re.compile(r"\bnet\.Dial\w*\s*\(|\btls\.Dial\w*\s*\("),
        "high",
        "直接 TCP/TLS 拨号，绕过任何 HTTP 客户端",
    ),
    SinkPattern(
        "client_factory",
        re.compile(
            r"\bhttpclient\.GetClient\s*\(|\bhttppool\.GetClient\s*\("
            r"|\bgetSharedReqClient\s*\(|\bCreatePrivacyReqClient\s*\("
            r"|\bcreateOpenAIReqClientWithProfile\s*\("
        ),
        "high",
        "客户端构造点。本身不发包，但决定 TLS 指纹，是 persona 归属的关键证据",
    ),
]

FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
CONST_URL_RE = re.compile(
    r'^\s*(?:const\s+)?([A-Za-z_]\w*)\s*=\s*"(https?://[^"]+)"'
)


@dataclass
class SinkHit:
    file: str
    line: int
    func: str
    sink_kind: str
    confidence: str
    code: str
    url_hints: list[str] = field(default_factory=list)
    official_host_hints: list[str] = field(default_factory=list)
    in_scope: bool = False
    openai_relevant: bool = False


def collect_url_constants(
    files: list[pathlib.Path],
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """收集 URL 常量，用于把 sink 处的标识符回溯成具体 host。

    返回 (按目录索引的常量表, 按包名索引的常量表)。

    必须按作用域索引而不是拉平成一张全局表：早期实现用全局裸名匹配，导致钉钉登录的
    `ExchangeCodeForUserToken` 里的局部 `TokenURL` 撞上 openai 包的
    `TokenURL = "https://auth.openai.com/oauth/token"`，被误判为在请求 OpenAI。
    同名常量在 Go 里极常见，跨包引用必须带包限定符才算数。

    只认字面量常量。用 fmt.Sprintf 或配置注入拼出的动态 URL 无法静态解析，这类 sink
    在台账里标注为 dynamic-url 并依赖运行时 Guard——文档 §9 检查项 4 正为它们准备。
    """
    by_dir: dict[str, dict[str, str]] = {}
    by_package: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        directory = path.parent.as_posix()
        package = path.parent.name
        for line in text.splitlines():
            match = CONST_URL_RE.match(line)
            if not match:
                continue
            name, url = match.group(1), match.group(2)
            by_dir.setdefault(directory, {})[name] = url
            # 跨包引用形如 openai.TokenURL，用「包名.常量名」作键。
            by_package[f"{package}.{name}"] = url
    return by_dir, by_package


def official_hosts_in(text: str) -> list[str]:
    return [host for host in OFFICIAL_HOSTS if host in text]


# OpenAI/Codex 相关性判据。命中者即使 URL 是动态的，也必须逐条人工回溯调用链；
# 未命中者才允许按「与本方案无关」批量处置。判据故意宽松，因为漏判的代价远高于多审。
OPENAI_RELEVANCE_RE = re.compile(
    r"openai|codex|chatgpt|OpenAI|Codex|ChatGPT|wham|WHAM|responses|Responses",
)


def scan_file(
    path: pathlib.Path,
    constants_by_dir: dict[str, dict[str, str]],
    constants_by_package: dict[str, str],
) -> list[SinkHit]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    rel = path.relative_to(ROOT).as_posix()

    # 先按顶层 func 切分函数体，供 sink 命中时回溯所在函数与其 URL 线索。
    # gofmt 保证顶层 func 以 "func " 开头、以列 0 的 "}" 结束。
    func_ranges: list[tuple[str, int, int]] = []
    current_name: str | None = None
    current_start = 0
    for index, line in enumerate(lines):
        match = FUNC_RE.match(line)
        if match:
            if current_name is not None:
                func_ranges.append((current_name, current_start, index - 1))
            current_name = match.group(1)
            current_start = index
        elif line == "}" and current_name is not None:
            func_ranges.append((current_name, current_start, index))
            current_name = None
    if current_name is not None:
        func_ranges.append((current_name, current_start, len(lines) - 1))

    def enclosing(index: int) -> tuple[str, str]:
        for name, start, end in func_ranges:
            if start <= index <= end:
                return name, "\n".join(lines[start : end + 1])
        return "<package-level>", ""

    hits: list[SinkHit] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        for pattern in SINK_PATTERNS:
            if not pattern.regex.search(line):
                continue

            func_name, func_body = enclosing(index)

            # URL 线索按作用域解析：同目录常量认裸名，跨包常量要求 pkg.Name 限定形式。
            url_hints: list[str] = []
            host_hints: set[str] = set()
            same_dir = constants_by_dir.get(path.parent.as_posix(), {})
            for const_name, const_value in same_dir.items():
                if re.search(rf"\b{re.escape(const_name)}\b", func_body):
                    url_hints.append(f"{const_name}={const_value}")
                    host_hints.update(official_hosts_in(const_value))
            for qualified, const_value in constants_by_package.items():
                if re.search(rf"\b{re.escape(qualified)}\b", func_body):
                    url_hints.append(f"{qualified}={const_value}")
                    host_hints.update(official_hosts_in(const_value))
            for literal in re.findall(r'"(https?://[^"]+)"', func_body):
                url_hints.append(f"literal={literal}")
                host_hints.update(official_hosts_in(literal))
            # 裸 host 字符串：req.Host = "chatgpt.com" 这类不带 scheme 的写法。
            host_hints.update(official_hosts_in(func_body))

            hits.append(
                SinkHit(
                    file=rel,
                    line=index + 1,
                    func=func_name,
                    sink_kind=pattern.sink_kind,
                    confidence=pattern.confidence,
                    code=stripped[:160],
                    url_hints=sorted(set(url_hints)),
                    official_host_hints=sorted(host_hints),
                    in_scope=bool(host_hints),
                    openai_relevant=bool(
                        OPENAI_RELEVANCE_RE.search(rel)
                        or OPENAI_RELEVANCE_RE.search(func_name)
                        or OPENAI_RELEVANCE_RE.search(func_body)
                    ),
                )
            )
            break  # 一行只归一种 sink 形态，避免同一行重复登记

    return hits


def build_call_index(
    files: list[pathlib.Path],
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, int, str]]]]:
    """建立调用图索引，用于把共享 facade 上的 sink 回溯到真正的业务调用点。

    这一步不是锦上添花，是台账能否成立的前提。实测 `openai_gateway_forward.go` 全文
    没有任何发送调用：主链在 forward.go:980 调 `doOpenAIHTTPUpstreamForRequest`，经
    `doOpenAIHTTPUpstreamWithProfile`（openai_upstream_http.go:118）才落到
    `httpUpstream.DoWithTLS`。images、models、passthrough 等路径共用同一个 facade。

    因此 sink 所在函数并不等于业务身份来源。若 SinkID 在 facade 处生成，所有业务路径
    会拿到同一个 ID，per-sink enforcement 直接失效——这正是文档 §8 要求 SinkID 必须在
    业务调用点生成并随 Plan/context 穿过 facade 的原因。

    返回 (函数名 -> 定义文件列表, 函数名 -> [(调用方文件, 行号, 调用方函数)])。
    正则近似：Go 调用形如 `name(` 或 `x.name(`，会有少量同名误配，人工核对时以链路
    语义为准。
    """
    definitions: dict[str, list[str]] = {}
    call_sites: dict[str, list[tuple[str, int, str]]] = {}

    parsed: list[tuple[pathlib.Path, list[str], list[tuple[str, int, int]]]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        ranges: list[tuple[str, int, int]] = []
        current: str | None = None
        start = 0
        for index, line in enumerate(lines):
            match = FUNC_RE.match(line)
            if match:
                if current is not None:
                    ranges.append((current, start, index - 1))
                current = match.group(1)
                start = index
                definitions.setdefault(current, []).append(
                    f"{path.relative_to(ROOT).as_posix()}:{index + 1}"
                )
            elif line == "}" and current is not None:
                ranges.append((current, start, index))
                current = None
        if current is not None:
            ranges.append((current, start, len(lines) - 1))
        parsed.append((path, lines, ranges))

    known = set(definitions)
    for path, lines, ranges in parsed:
        rel = path.relative_to(ROOT).as_posix()
        for caller, start, end in ranges:
            for index in range(start, min(end + 1, len(lines))):
                line = lines[index]
                if line.strip().startswith("//"):
                    continue
                for callee in re.findall(r"\b([A-Za-z_]\w*)\s*\(", line):
                    if callee in known and callee != caller:
                        call_sites.setdefault(callee, []).append(
                            (rel, index + 1, caller)
                        )
    return definitions, call_sites


def trace_callers(
    func_name: str,
    call_sites: dict[str, list[tuple[str, int, str]]],
    max_depth: int = 3,
) -> list[str]:
    """自 sink 所在函数向上回溯调用者，产出候选业务调用点清单。"""
    seen: set[str] = {func_name}
    frontier = [func_name]
    chains: list[str] = []
    for depth in range(max_depth):
        next_frontier: list[str] = []
        for name in frontier:
            for caller_file, caller_line, caller_func in call_sites.get(name, []):
                label = f"L{depth + 1} {caller_file}:{caller_line} in {caller_func}()"
                chains.append(label)
                if caller_func not in seen:
                    seen.add(caller_func)
                    next_frontier.append(caller_func)
        frontier = next_frontier
        if not frontier:
            break
    return chains


def go_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for scan_root in SCAN_ROOTS:
        for path in sorted(scan_root.rglob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            if "/vendor/" in path.as_posix():
                continue
            files.append(path)
    return files


def render_markdown(hits: list[SinkHit]) -> str:
    in_scope = [h for h in hits if h.in_scope]
    out_scope = [h for h in hits if not h.in_scope]

    by_file: dict[str, list[SinkHit]] = {}
    for hit in in_scope:
        by_file.setdefault(hit.file, []).append(hit)

    lines = [
        "# sink 扫描原始结果（自动生成，勿手工编辑）",
        "",
        "由 `tools/scan_egress_sinks.py` 生成。人工判定结果写入 `sink-inventory.md`，不要改本文件。",
        "",
        f"- 扫描文件数：{len(go_files())}",
        f"- 命中总数：{len(hits)}",
        f"- **疑似 in-scope（函数体出现官方 host 线索）：{len(in_scope)}**",
        f"- 疑似 out-of-scope：{len(out_scope)}",
        "",
        "in-scope 判据是「所在函数体内出现官方 host 字面量或 URL 常量引用」。",
        "该判据会漏掉 URL 完全由参数传入的 sink，这类必须人工补充——见文末待人工确认清单。",
        "",
        "## 疑似 in-scope 命中",
        "",
    ]

    for file in sorted(by_file):
        lines.append(f"### `{file}`")
        lines.append("")
        lines.append("| 行 | 所在函数 | sink 形态 | 置信度 | 官方 host 线索 |")
        lines.append("|---:|---|---|---|---|")
        for hit in sorted(by_file[file], key=lambda h: h.line):
            hosts = ", ".join(hit.official_host_hints) or "—"
            lines.append(
                f"| {hit.line} | `{hit.func}` | {hit.sink_kind} | {hit.confidence} | {hosts} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 待人工确认：URL 由参数传入的候选",
            "",
            "以下 sink 未在所在函数内发现官方 host 线索，但 sink 形态为 high 置信度，",
            "URL 可能由调用方传入。必须逐个回溯调用链确认，不能直接判为 out-of-scope。",
            "",
            "| 文件 | 行 | 所在函数 | sink 形态 |",
            "|---|---:|---|---|",
        ]
    )
    for hit in sorted(
        [h for h in out_scope if h.confidence == "high"],
        key=lambda h: (h.file, h.line),
    ):
        lines.append(f"| `{hit.file}` | {hit.line} | `{hit.func}` | {hit.sink_kind} |")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument("--out", type=str, default="", help="写入文件而非 stdout")
    parser.add_argument(
        "--trace",
        type=str,
        default="",
        help="回溯指定函数的调用者链，用于定位共享 facade 背后的业务调用点",
    )
    args = parser.parse_args()

    files = go_files()
    constants_by_dir, constants_by_package = collect_url_constants(files)

    if args.trace:
        _, call_sites = build_call_index(files)
        chains = trace_callers(args.trace, call_sites)
        if not chains:
            print(f"未找到 {args.trace}() 的调用者")
            return 0
        print(f"{args.trace}() 的调用者链（最多 3 层）：")
        for chain in chains:
            print(f"  {chain}")
        return 0

    hits: list[SinkHit] = []
    for path in files:
        hits.extend(scan_file(path, constants_by_dir, constants_by_package))

    if args.json:
        payload = json.dumps(
            {
                "scanned_files": len(files),
                "url_constants_by_package": constants_by_package,
                "hits": [asdict(h) for h in hits],
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        payload = render_markdown(hits)

    if args.out:
        out_path = ROOT / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        in_scope = sum(1 for h in hits if h.in_scope)
        print(f"已写入 {args.out}：{len(hits)} 条命中，其中疑似 in-scope {in_scope} 条")
    else:
        print(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
