#!/usr/bin/env python3
"""检查生产 Go 代码中把 Codex 版本焊进符号名或注释的写法。

§3.2 要求稳定执行引擎“不包含按版本散落的常量和模型特判”，§4.3.2 要求新 Codex
版本原则上只新增画像、版本清单和测试，不修改 §3.5.2 的共享接入点。两条约束依赖
同一个可机器复算的事实：除版本快照本身以外，生产代码不应把某个具体版本写进代码结构。

**与 AST 门禁的分工。** 判断一个裸版本字面量（`"0.146.0"`）算不算泄漏，取决于它属于
谁——Codex/OpenAI 配置写死版本是泄漏，Anthropic 画像的版本不在本规格范围内（§1.1）。
这个归属问题必须由语法结构回答：同一 ValueSpec 里的多个名称与值、同一切片里的多个
CallExpr、同一行上的多个表达式，正则都区分不了，每补一条规则就在假绿与误报之间摆动
一次。归属判定因此迁移到
`backend/internal/service/official_egress_version_leak_ast_test.go`，用 go/ast 实现。

本脚本只保留不需要语法归属的那一半：版本被焊进符号名（`Codex0146`、
`officialCodexVersion0146`），或与 codex 同行出现在注释里。这类模式是纯文本形态，
正则足够且不会误判。

判据本身必须与版本号解耦。早期实现把 0.145.0、Codex0145 写进正则，结果是升级时整组
失效：新代码写 Codex0146 完全不命中，而升级恰恰是本门禁最该起作用的时刻。
`--self-test` 用内联样本把这一点变成机器断言。

基线记录指纹而非计数：只比数量时，同一文件删掉一处旧泄漏、同时新增一处新泄漏即可
通过，而实际内容已经变了。

用法：

    python3 tools/check_version_leak.py              # 门禁模式，比对基线
    python3 tools/check_version_leak.py --self-test  # 校验判据本身
    python3 tools/check_version_leak.py --update-baseline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "version_leak_baseline.json"
SCAN_ROOT = ROOT / "backend"

# 把某个 Codex 版本焊进符号名，或与 codex 同行出现的版本字面量。
# 按标识符形状匹配，不写死版本号。
SYMBOL_LEAK_RE = re.compile(
    r"""
      codex[_-]?version[_-]?\d[\d._]*        # officialCodexVersion0145 / codex_version_0_146
    | codex[_-]?\d{3,}                       # Codex0145 / codex0146
    | codex[^\n]{0,60}?\d+[._]\d+[._]\d+     # 同行 codex 语境下的三段版本
    | \d+[._]\d+[._]\d+[^\n]{0,60}?codex     # 版本在前、codex 在后
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 版本快照按定义必须携带版本字面量，这是设计要求而非泄漏。此处只豁免快照文件本身；
# 通用执行层与共享业务层都不在豁免范围内。用模式而不是固定路径，新增版本快照时
# 不必再回来改门禁。
EXEMPT_RE = re.compile(
    r"^backend/internal/service/official_egress_codex_\d+_profile\.go$"
)


def fingerprint_line(line: str) -> str:
    """把命中行归一化成与行号、缩进无关的指纹。"""
    normalized = " ".join(line.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def leak_fingerprints(text: str) -> dict[str, int]:
    """返回单个文件的命中指纹到出现次数的映射。"""
    hits: dict[str, int] = {}
    for line in text.splitlines():
        if not SYMBOL_LEAK_RE.search(line):
            continue
        key = fingerprint_line(line)
        hits[key] = hits.get(key, 0) + 1
    return hits


def count_leaks(text: str) -> int:
    """返回单个文件中的版本标识符命中行数。"""
    return sum(leak_fingerprints(text).values())


def scan() -> dict[str, dict[str, int]]:
    """扫描生产 Go 代码，返回文件到命中指纹映射的字典。"""
    hits: dict[str, dict[str, int]] = {}
    for path in sorted(SCAN_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if EXEMPT_RE.match(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fingerprints = leak_fingerprints(text)
        if fingerprints:
            hits[rel] = fingerprints
    return hits


def total_hits(hits: dict[str, dict[str, int]]) -> int:
    return sum(sum(counts.values()) for counts in hits.values())


def load_baseline() -> dict[str, dict[str, int]] | None:
    if not BASELINE.exists():
        return None
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    baseline: dict[str, dict[str, int]] = {}
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            # 旧格式只有计数，无法定位内容，必须重新生成基线。
            return None
        baseline[str(rel)] = {str(k): int(v) for k, v in entry.items()}
    return baseline


def write_baseline(hits: dict[str, dict[str, int]]) -> None:
    payload = {
        "_comment": (
            "各生产文件当前的 Codex 版本符号名／注释命中指纹（规范化行内容哈希 → 次数）。"
            "裸版本字面量的归属由 go/ast 判定，见 backend/internal/service/"
            "official_egress_version_leak_ast_test.go 及其 testdata 基线。"
            "门禁禁止出现基线外的新指纹，也禁止同一指纹次数上升。"
        ),
        "_exempt": EXEMPT_RE.pattern,
        "files": {rel: dict(sorted(fp.items())) for rel, fp in sorted(hits.items())},
    }
    BASELINE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# 判据自测样本。每条都写明它守护的是哪种失效：门禁写死版本号时，"新版本"分组
# 会整组失效，而这正是升级时最需要它工作的时刻。
SELF_TEST_CASES: list[tuple[str, bool, str]] = [
    # 新版本必须被检出——这是本门禁存在的首要理由。
    ('officialCodexVersion0146 = "0.146.0"', True, "新版本常量"),
    ("func officialCodex0146ApplyHeaderContract() {}", True, "新版本符号名"),
    ('transportID = "codex-0.146.0-http-ubuntu24-native"', True, "新版本传输 ID"),
    ("// 对齐 codex_exec 0.147.2 抓包默认值。", True, "注释中的 codex 版本"),
    ("codex0146EndpointID(endpoint)", True, "新版本端点类型"),
    # 现役版本同样必须被检出。
    ('officialCodexVersion0145 = "0.145.0"', True, "现役版本常量"),
    ("codex0145EndpointID(officialCodexEndpointModels)", True, "现役版本符号名"),
    # 不含 codex 语境的裸版本由 AST 门禁负责归属判定，本脚本不再处理，
    # 以免在没有语法信息的情况下猜测供应商归属。
    ('{Name: "version", Value: "0.146.0"},', False, "裸版本交给 AST 门禁"),
    ('Version:    "2.1.220",', False, "Anthropic 版本不属本脚本范围"),
    ("// 见 §3.5.2 与 §4.3.2 的约定", False, "文档章节号"),
    ("// coder/websocket@v1.8.14 Conn.Write is synchronous", False, "依赖库版本"),
    ('proxyURL = "http://192.168.1.10:8080"', False, "内网地址"),
    ('grokCLIStableVersion = "0.2.93"', False, "其他供应商 CLI 版本"),
]


def run_self_test() -> int:
    """校验判据本身：新版本必须命中，同形噪声必须不命中。"""
    failures: list[str] = []
    for sample, want_hit, label in SELF_TEST_CASES:
        got_hit = count_leaks(sample) > 0
        if got_hit != want_hit:
            expected = "命中" if want_hit else "不命中"
            failures.append(f"{label}：期望{expected}，实际相反 —— {sample}")

    # 基线必须能区分内容而不只是数量：同一文件删一处旧泄漏、加一处新泄漏时，
    # 计数不变，只有指纹会变。
    before = leak_fingerprints('officialCodexVersion0145 = "0.145.0"\n')
    after = leak_fingerprints('officialCodexVersion0146 = "0.146.0"\n')
    if sum(before.values()) != sum(after.values()):
        failures.append("样本构造有误：删旧加新的命中数应当相同")
    elif set(before) == set(after):
        failures.append("基线指纹无法区分内容变化，等量替换可以绕过门禁")

    if failures:
        for failure in failures:
            print(f"🔴 判据自测失败：{failure}", file=sys.stderr)
        return 1
    print(f"✅ 判据自测通过：{len(SELF_TEST_CASES)} 条样本 + 指纹可辨性")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="以当前扫描结果覆盖基线，用于版本解耦推进后收紧门禁",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="只校验判据本身是否与版本号解耦，不扫描仓库",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    hits = scan()
    total = total_hits(hits)

    if args.update_baseline:
        write_baseline(hits)
        print(f"✅ 已更新版本泄漏基线：{len(hits)} 个文件，合计 {total} 个命中行")
        return 0

    baseline = load_baseline()
    if baseline is None:
        print(
            f"🔴 缺少版本泄漏基线 {BASELINE.relative_to(ROOT)}（或仍是旧的计数格式），"
            "请先执行 --update-baseline",
            file=sys.stderr,
        )
        return 1

    # 按指纹比对：基线外的新指纹与次数上升都算违规。只比总数会放过“删一处旧泄漏、
    # 同时加一处新泄漏”这种总量不变的替换。
    errors: list[str] = []
    for rel, fingerprints in sorted(hits.items()):
        allowed = baseline.get(rel, {})
        if not allowed:
            errors.append(
                f"{rel} 是基线外的新增版本泄漏（{sum(fingerprints.values())} 行）；"
                "共享业务层与通用执行层不得引入版本标识符"
            )
            continue
        for key, count in sorted(fingerprints.items()):
            permitted = allowed.get(key, 0)
            if count > permitted:
                errors.append(
                    f"{rel} 出现基线外的版本泄漏内容（指纹 {key}，"
                    f"基线 {permitted} 次、当前 {count} 次）"
                )

    if errors:
        for error in errors:
            print(f"🔴 {error}", file=sys.stderr)
        return 1

    improved: dict[str, tuple[int, int]] = {}
    for rel, allowed in baseline.items():
        was = sum(allowed.values())
        now = sum(hits.get(rel, {}).values())
        if now < was:
            improved[rel] = (was, now)
    if improved:
        for rel, (was, now) in sorted(improved.items()):
            print(f"⬇️  {rel}：{was} → {now}")
        print(
            "提示：命中已下降，可执行 --update-baseline 收紧门禁，"
            "防止后续回升到旧水位"
        )

    print(f"✅ 版本泄漏未超基线：{len(hits)} 个文件，合计 {total} 个命中行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
