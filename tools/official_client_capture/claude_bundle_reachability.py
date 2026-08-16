#!/usr/bin/env python3
"""对提取出的 Claude Code bundle 做词法扫描、顶层声明切分和网络 sink 可达性判定。

为什么需要这个工具
------------------
`docs/Claude_code_21220_EGRESS_SPEC.md` §4.1.1 执行顺序表第 3 步要求「枚举 sink：
建立入口到网络 sink 的可达窗口」，据此产出端点／Header／Body／retry 候选清单；
§2.1.2 准入条件第 2 条要求「确认到网络 sink 可达」。在 minify bundle 上，光靠 grep
命中一个 header 写入点不足以满足这条：写入点可能位于死代码、其他 provider 分支或
非出站路径上。

本模块提供三件事：

1. **正确的词法扫描**——必须正确处理嵌套模板字面量。minify 代码里 `` `a${`b`}c` ``
   这种嵌套很常见，用朴素正则匹配模板会在第一个内层反引号处提前结束，导致其后
   所有花括号深度错位（实测：朴素实现扫到 73 KB 处深度即变负）。这里改用递归下降，
   并把 `${}` 内的表达式一并作为 token 产出，使模板内的函数调用不会从引用图中丢失。
2. **声明切分**——不依赖全局括号配平。对 21 MB minify 代码手写一个 100% 正确的
   括号配平扫描器不现实：实测两处失配（模板内 `http://` 的 `//` 被当成行注释是其一）
   就足以让一个 40 字节的函数被算成 17.7 MB，进而让整张图退化成单节点。因此这里改用
   **声明锚定分段**：从 token 流取每个 `function`/`var`/`let`/`const`/`class` 声明作为
   段起点，段范围延伸到下一个声明起点。分段是作用域的近似而非精确重建，但它对
   「这段代码里引用了哪些顶层名」这个问题足够，且对局部词法错位免疫。
3. **引用图与可达路径**——以顶层声明为节点、声明体内出现的其他顶层名为边，
   从入口做 BFS，输出到目标声明的具体路径，供人工复核而不是只给一个布尔值。

可达性是**保守近似**：标识符引用不区分同名局部变量，因此图中可能存在多余的边，
结论方向是「可能可达」。据此可以否证（不可达即确定不在出站路径上），
但确认可达时必须配合输出的路径人工复核，并最终由运行证据闭环。

只依赖标准库。
"""

from __future__ import annotations

import bisect
import collections
import hashlib
import re

# 标识符、数字、运算符。字符串与模板在扫描器里单独处理。
_RE_ID = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_RE_NUM = re.compile(r"\d[\w.]*")
_RE_REGEX = re.compile(r"/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\]|\\.)*\])+/[gimsuyvd]*")

# `/` 之前若是这些 token，说明它是除号而不是正则起始。
_VALUE_KINDS = {"id", "num", "str", "tpl", "regex"}
_VALUE_OPS = {")", "]", "}"}

# 这些关键字虽然被词法器归为 id，但它们不是值——其后的 `/` 是正则起始而非除号。
# 漏掉这一条会让 `function tu(e){return/^[\\/]{2}/.test(e)}` 这类写法把正则当除号，
# 吞掉其后大段代码并使花括号深度永久错位（实测会让一个 40 字节的函数被算成 21 MB）。
_KEYWORDS_BEFORE_REGEX = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await", "if", "while", "for",
    "switch", "catch", "finally", "try", "default", "const", "let", "var",
}

# α-归一化时保留原样的标识符：JS 关键字与宿主全局。它们不参与 minify 重命名，
# 保留下来才能让归一化文本仍具可读性和判别力。
_RESERVED_IDENTIFIERS = {
    "async", "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "export", "extends", "finally",
    "for", "function", "if", "import", "in", "instanceof", "let", "new", "return",
    "static", "super", "switch", "this", "throw", "try", "typeof", "var", "void",
    "while", "with", "yield", "true", "false", "null", "undefined",
    "Object", "Array", "String", "Number", "Boolean", "Symbol", "Promise", "Math",
    "JSON", "Map", "Set", "WeakMap", "WeakSet", "Error", "TypeError", "RegExp",
    "Date", "process", "globalThis", "console", "Buffer", "URL", "URLSearchParams",
    "fetch", "Headers", "Request", "Response", "AbortController", "WebSocket",
    "TextEncoder", "TextDecoder", "Uint8Array", "ArrayBuffer",
}

# 网络 sink 标记。命中即认为该声明体内存在出站动作。
SINK_PATTERNS = {
    "fetch": re.compile(r"\bfetch\s*\("),
    "messages_create": re.compile(r"\bmessages\s*\.\s*create\s*\("),
    "sdk_post": re.compile(r"\.\s*post\s*\(\s*[\"'`]/v1/"),
    "websocket": re.compile(r"\bnew\s+WebSocket\b"),
    "xhr": re.compile(r"\bXMLHttpRequest\b"),
}


class Token:
    __slots__ = ("kind", "text", "start", "end", "in_template")

    def __init__(self, kind: str, text: str, start: int, end: int, in_template: bool = False):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.in_template = in_template

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Token({self.kind}, {self.text[:20]!r}, {self.start})"


def _skip_string(src: str, i: int) -> int:
    """跳过 '...' 或 "..."，返回结束位置（引号之后）。"""
    quote = src[i]
    j = i + 1
    n = len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "\n":  # 未闭合的普通字符串不跨行，容错退出
            return j
        j += 1
    return n


def _scan_template(src: str, i: int, toks: list[Token]) -> int:
    """扫描模板字面量，递归处理 ${} 内的表达式，返回结束位置。

    这是本模块正确性的关键：`${}` 内可以再出现模板、字符串和任意括号，
    朴素正则会在内层反引号处提前结束，使其后的花括号深度全部错位。
    """
    n = len(src)
    j = i + 1
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and j + 1 < n and src[j + 1] == "{":
            # 找到与之匹配的 `}`，其间可能嵌套任意结构。
            depth = 1
            k = j + 2
            expr_start = k
            while k < n and depth:
                ch = src[k]
                if ch in "'\"":
                    k = _skip_string(src, k)
                    continue
                if ch == "`":
                    # 内层模板整体跳过，其内部的 token 由递归调用补齐。
                    inner: list[Token] = []
                    k = _scan_template(src, k, inner)
                    toks.extend(inner)
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            # 把 ${} 内的表达式作为普通代码扫描，保留其中的函数调用与标识符。
            for tok in _lex_range(src, expr_start, k):
                tok.in_template = True
                toks.append(tok)
            j = k + 1
            continue
        j += 1
    return n


def _lex_range(src: str, start: int, stop: int) -> list[Token]:
    """扫描 [start, stop) 区间，产出 token 列表。"""
    toks: list[Token] = []
    i = start
    prev: Token | None = None
    while i < stop:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < stop:
            nxt = src[i + 1]
            if nxt == "/":
                nl = src.find("\n", i)
                i = stop if nl < 0 or nl > stop else nl
                continue
            if nxt == "*":
                end = src.find("*/", i + 2)
                i = stop if end < 0 else min(end + 2, stop)
                continue
            # 正则字面量消歧：看上一个有意义 token 是不是值的结尾。
            is_value = prev is not None and (
                (prev.kind in _VALUE_KINDS
                 and not (prev.kind == "id" and prev.text in _KEYWORDS_BEFORE_REGEX))
                or prev.text in _VALUE_OPS)
            if not is_value:
                m = _RE_REGEX.match(src, i)
                if m and m.end() <= stop:
                    prev = Token("regex", m.group(), i, m.end())
                    toks.append(prev)
                    i = m.end()
                    continue
        if c in "'\"":
            end = _skip_string(src, i)
            prev = Token("str", src[i:end], i, end)
            toks.append(prev)
            i = end
            continue
        if c == "`":
            sub: list[Token] = []
            end = _scan_template(src, i, sub)
            prev = Token("tpl", "`", i, end)
            toks.append(prev)
            toks.extend(sub)
            i = end
            continue
        m = _RE_ID.match(src, i)
        if m:
            prev = Token("id", m.group(), i, m.end())
            toks.append(prev)
            i = m.end()
            continue
        m = _RE_NUM.match(src, i)
        if m:
            prev = Token("num", m.group(), i, m.end())
            toks.append(prev)
            i = m.end()
            continue
        prev = Token("op", c, i, i + 1)
        toks.append(prev)
        i += 1
    return toks


def lex(src: str) -> list[Token]:
    """对整个 bundle 做词法扫描。"""
    return _lex_range(src, 0, len(src))


_DECL_KEYWORDS = {"function", "var", "let", "const", "class"}

# 一条 var 声明里逗号分隔的绑定最多向后看这么多 token。minify 会把大量常量塞进
# 同一条 var（实测 `var Oyr,Tqs="anthropic-dispatch-id",Ftp="v2s",…`），只取紧邻
# 关键字的那一个会让这些常量从符号表里整体消失。
_COMMA_SCAN_LIMIT = 4000


def _comma_bindings(toks: list[Token], start: int) -> list[dict]:
    """收集一条 var/let/const 声明中逗号分隔的其余绑定名。

    只做声明内的局部括号跟踪，范围有限，因此不受全局词法错位影响；
    遇到 `;` 或深度转负即停止。
    """
    out: list[dict] = []
    depth = 0
    expect = False
    limit = min(len(toks), start + _COMMA_SCAN_LIMIT)
    for j in range(start, limit):
        tok = toks[j]
        if tok.in_template:
            continue
        if tok.kind == "op":
            ch = tok.text
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
                if depth < 0:
                    break
            elif ch == ";" and depth == 0:
                break
            elif ch == "," and depth == 0:
                expect = True
                continue
        elif expect and tok.kind == "id":
            out.append({"name": tok.text, "kind": "binding",
                        "start": tok.start, "end": None})
        expect = False
    return out


def declarations(src: str, toks: list[Token]) -> list[dict]:
    """按声明关键字把 bundle 切成段。

    段起点取声明关键字所在 token，段终点取下一个声明起点，因此所有代码都被覆盖，
    每段代码归属于它前面最近的那个声明。这是作用域的近似：嵌套函数也会成为独立段，
    对引用图而言粒度更细并无害处，而代价是段边界不等于真实的词法作用域边界。
    位于字符串、模板和正则内的关键字不会进入 token 流，因此不会产生假段。
    """
    marks: list[dict] = []
    n = len(toks)
    for idx, tok in enumerate(toks):
        if tok.kind != "id" or tok.text not in _DECL_KEYWORDS:
            continue
        name = None
        kind = tok.text
        nxt = toks[idx + 1] if idx + 1 < n else None
        if tok.text == "function":
            # 兼容 `function* name(` 与匿名 `function(`。
            if nxt and nxt.kind == "op" and nxt.text == "*":
                nxt = toks[idx + 2] if idx + 2 < n else None
            if nxt and nxt.kind == "id":
                name = nxt.text
            kind = "function"
        elif tok.text == "class":
            if nxt and nxt.kind == "id":
                name = nxt.text
            kind = "class"
        else:
            # var/let/const：只在其后紧跟标识符时视为绑定。
            if nxt and nxt.kind == "id":
                name = nxt.text
            kind = "binding"
        marks.append({"name": name, "kind": kind, "start": tok.start, "end": None})
        if kind == "binding":
            marks.extend(_comma_bindings(toks, idx + 1))

    # 逗号绑定是回看产生的，位置未必有序；算 end 之前必须先按起点排序。
    marks.sort(key=lambda m: m["start"])
    for i, mark in enumerate(marks):
        mark["end"] = marks[i + 1]["start"] if i + 1 < len(marks) else len(src)
    return marks


class BundleIndex:
    """bundle 的顶层索引：偏移 → 声明、声明 → 引用、声明 → sink。"""

    def __init__(self, src: str):
        self.src = src
        self.tokens = lex(src)
        self.statements = declarations(src, self.tokens)
        self._starts = [s["start"] for s in self.statements]
        self._token_starts = [t.start for t in self.tokens]

        # 顶层符号识别：minify 后的局部变量名（e/t/r/n…）会被声明成千上万次，
        # 而 esbuild 分配的顶层名在整个 bundle 中只声明一次。用「声明次数为 1
        # 且长度不小于 2」把两者分开，避免局部变量污染引用图。
        counts = collections.Counter(s["name"] for s in self.statements if s["name"])
        self.by_name: dict[str, dict] = {}
        for stmt in self.statements:
            name = stmt["name"]
            if name and counts[name] == 1 and len(name) >= 2:
                self.by_name[name] = stmt
        self._refs: dict[str, set[str]] | None = None

    def locate(self, offset: int) -> dict | None:
        """把字节偏移映射到所属声明段。"""
        idx = bisect.bisect_right(self._starts, offset) - 1
        if idx < 0:
            return None
        stmt = self.statements[idx]
        return stmt if stmt["start"] <= offset < stmt["end"] else None

    def enclosing_symbol(self, offset: int) -> dict | None:
        """向前找到包含该偏移的最近一个顶层符号段。

        细粒度分段会把 `let pi=…` 这类局部绑定也切成段，直接 locate 得到的往往是
        局部变量。做引用图和可达性时需要的是它所属的顶层符号，因此在段序列上回退，
        直到遇到一个进入了 by_name 的声明。
        """
        idx = bisect.bisect_right(self._starts, offset) - 1
        while idx >= 0:
            stmt = self.statements[idx]
            name = stmt["name"]
            if name and name in self.by_name and self.by_name[name] is stmt:
                return stmt
            idx -= 1
        return None

    def sinks_in(self, stmt: dict) -> list[str]:
        body = self.src[stmt["start"]:stmt["end"]]
        return [name for name, pat in SINK_PATTERNS.items() if pat.search(body)]

    def _token_slice(self, stmt: dict) -> list[Token]:
        lo = bisect.bisect_left(self._token_starts, stmt["start"])
        out = []
        for tok in self.tokens[lo:]:
            if tok.start >= stmt["end"]:
                break
            out.append(tok)
        return out

    def reference_graph(self) -> dict[str, set[str]]:
        """顶层声明之间的引用图（保守近似，不区分同名局部变量）。"""
        if self._refs is not None:
            return self._refs
        names = set(self.by_name)
        refs: dict[str, set[str]] = {}
        ti = 0
        tokens = self.tokens
        for stmt in self.statements:
            if not stmt["name"]:
                continue
            while ti < len(tokens) and tokens[ti].start < stmt["start"]:
                ti += 1
            j = ti
            found: set[str] = set()
            while j < len(tokens) and tokens[j].start < stmt["end"]:
                tok = tokens[j]
                if tok.kind == "id" and tok.text in names and tok.text != stmt["name"]:
                    found.add(tok.text)
                j += 1
            refs.setdefault(stmt["name"], set()).update(found)
        self._refs = refs
        return refs

    def alpha_normalize(self, start: int, end: int) -> tuple[str, str]:
        """把区间内的代码做 α-归一化，返回 (归一化文本, sha256)。

        minify 分配的标识符跨平台、跨版本都会漂移（同一个常量在 Darwin 叫 `S8s`、
        在 Linux 叫 `Tqs`），因此不能直接拿符号名或字节跨度当锚点。这里把非保留的
        标识符按首次出现顺序替换成 `$0`、`$1`…，保留关键字、成员属性名和全部字面量
        ——后者正是 header 名、gate 名和环境变量名所在，也是命题真正依赖的部分。
        归一化后同一段逻辑在两个平台上应得到同一个摘要。
        """
        lo = bisect.bisect_left(self._token_starts, start)
        parts: list[str] = []
        mapping: dict[str, str] = {}
        dots = 0  # 紧邻的连续 `.` 数量：1 个是成员访问，3 个是展开语法
        for tok in self.tokens[lo:]:
            if tok.start >= end:
                break
            prev_dot = dots == 1
            if tok.kind == "id":
                if prev_dot or tok.text in _RESERVED_IDENTIFIERS:
                    parts.append(tok.text)
                else:
                    if tok.text not in mapping:
                        mapping[tok.text] = f"${len(mapping)}"
                    parts.append(mapping[tok.text])
            elif tok.kind == "tpl":
                parts.append("`")
            else:
                parts.append(tok.text)
            dots = dots + 1 if (tok.kind == "op" and tok.text == ".") else 0
        text = " ".join(parts)
        return text, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def alpha_normalize_around(self, offset: int, before: int = 40,
                               after: int = 40) -> tuple[str, str]:
        """以某个偏移为中心，取前后固定数量的 token 做 α-归一化。

        比「向前退到最近的 `;`」稳健：minify 代码里分号分布不均，命中点前若干百字节
        内可能一个分号都没有，按分号切分会退化成从文件开头截取，使两个平台的锚点
        包含各自的 bundle 前言而误报为不一致。固定 token 窗口在两个平台上覆盖的是
        同一段逻辑，因此摘要可直接比对。
        """
        pos = bisect.bisect_left(self._token_starts, offset)
        lo = max(0, pos - before)
        hi = min(len(self.tokens), pos + after)
        if lo >= hi:
            return "", hashlib.sha256(b"").hexdigest()
        return self.alpha_normalize(self.tokens[lo].start, self.tokens[hi - 1].end)

    def entry_name(self) -> str | None:
        """bundle 末尾的入口调用，形如 `…;sTT();})`。"""
        tail = [t for t in self.tokens if not t.in_template][-8:]
        for k in range(len(tail) - 2, -1, -1):
            if tail[k].kind == "id" and tail[k + 1].text == "(" :
                return tail[k].text
        return None

    def path_to(self, target: str, entry: str | None = None,
                max_depth: int = 12) -> list[str] | None:
        """从入口到目标声明的引用路径（BFS 最短路），不可达返回 None。"""
        entry = entry or self.entry_name()
        if entry is None or entry not in self.by_name or target not in self.by_name:
            return None
        refs = self.reference_graph()
        seen = {entry}
        queue = collections.deque([(entry, [entry])])
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            if len(path) > max_depth:
                continue
            for nxt in refs.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, path + [nxt]))
        return None
