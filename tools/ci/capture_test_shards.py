#!/usr/bin/env python3
"""官方客户端采集工具测试的分片运行器（保持全量门禁，把 test-capture-tools 拆成 N 片并行）。

与 ``make test-capture-tools`` 完全相同的加载语义：``unittest`` 以 ``tools/official_client_capture/tests``
为起点 discover ``test_*.py``（sys.path 与模块名都和单进程全量一致），再按模块名把用例分到 N 片。
分片确定性来自权重表 ``tools/ci/capture_test_weights.json``（模块 → 测量权重，缺省 1.0；测量口径与收据见表内元数据）：
模块按权重降序、同权重按名字排序，贪心放入当前总权重最小的片（LPT），所以同一棵树上任何机器算出的
分片都一样。

子命令：
* ``check --count N``：discover 出的模块集合必须等于各片并集且互不相交；权重表里的模块必须仍存在
  （防止陈旧表把已删模块算进去），discover 出但不在表里的模块以权重 1.0 计并打印提示。任何不闭合即退出 2。
* ``list --count N --index i``：打印第 i 片的模块名与总权重。
* ``run --count N --index i``：运行第 i 片（先做 check），退出码 0 表示该片全部通过。
* ``weights --from-timing <tsv>``：从耗时表（模块<TAB>秒<TAB>结果）生成权重表 JSON 并写入。

不写入工作树中的任何测试目录；只读 discover。运行时请与 make 目标一样设置 ``CLAUDE_AST_TYPESCRIPT_MODULE``
与 ``PYTHONDONTWRITEBYTECODE=1``。
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

DEFAULT_START = Path("tools/official_client_capture/tests")
DEFAULT_PATTERN = "test_*.py"
DEFAULT_WEIGHTS = Path("tools/ci/capture_test_weights.json")
WEIGHTS_SCHEMA = "capture-test-shard-weights/v1"


class ShardError(RuntimeError):
    """分片闭合或参数错误。"""


def _iterate(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iterate(item)
        else:
            yield item


def discover_cases(start: Path, pattern: str) -> dict[str, list[unittest.TestCase]]:
    """按 discover 语义加载全量用例，按顶层模块名分组（含加载失败的 _FailedTest，归入其模块名）。"""

    if not start.is_dir():
        raise ShardError(f"测试起点目录不存在：{start}")
    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir=str(start), pattern=pattern)
    grouped: dict[str, list[unittest.TestCase]] = {}
    for case in _iterate(suite):
        module = case.__class__.__module__.split(".")[-1]
        if module == "loader":
            # unittest.loader._FailedTest：模块导入失败，用例 id 形如 "test_x" 或 "test_x.Class"；按首段归组。
            module = case.id().split(".")[0]
        grouped.setdefault(module, []).append(case)
    if not grouped:
        raise ShardError("discover 没有发现任何用例")
    return grouped


def load_weights(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != WEIGHTS_SCHEMA or not isinstance(payload.get("weights"), dict):
        raise ShardError(f"权重表格式非法：{path}")
    weights: dict[str, float] = {}
    for module, value in payload["weights"].items():
        if not isinstance(module, str) or isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ShardError(f"权重表条目非法：{module}={value!r}")
        weights[module] = float(value)
    return weights


def assign(modules: list[str], weights: dict[str, float], count: int) -> list[list[str]]:
    """确定性 LPT：权重降序、同权重按名字，放入当前总权重最小的片（并列取序号小的）。"""

    if count < 1:
        raise ShardError("分片数必须 ≥ 1")
    ordered = sorted(modules, key=lambda name: (-weights.get(name, 1.0), name))
    shards: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for name in ordered:
        index = min(range(count), key=lambda i: (totals[i], i))
        shards[index].append(name)
        totals[index] += weights.get(name, 1.0)
    return [sorted(shard) for shard in shards]


def check(grouped: dict[str, list[unittest.TestCase]], weights: dict[str, float], count: int) -> list[list[str]]:
    modules = sorted(grouped)
    stale = sorted(set(weights) - set(modules))
    if stale:
        raise ShardError("权重表登记了不存在的模块：" + "、".join(stale))
    unweighted = sorted(set(modules) - set(weights))
    if unweighted:
        print(f"提示：{len(unweighted)} 个模块不在权重表，按权重 1.0 计：" + "、".join(unweighted), file=sys.stderr)
    shards = assign(modules, weights, count)
    union: list[str] = []
    for shard in shards:
        union.extend(shard)
    if sorted(union) != modules or len(union) != len(set(union)):
        raise ShardError("分片并集不等于全量模块集合或存在重复")
    return shards


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=Path, default=DEFAULT_START)
    parser.add_argument("--pattern", default=DEFAULT_PATTERN)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "list", "run"):
        p = sub.add_parser(name)
        p.add_argument("--count", type=int, required=True)
        if name != "check":
            p.add_argument("--index", type=int, required=True, help="1 起")
    w = sub.add_parser("weights")
    w.add_argument("--from-timing", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "weights":
            weights: dict[str, float] = {}
            for line in args.from_timing.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) < 2 or not parts[0].startswith("tools.official_client_capture.tests."):
                    continue
                weights[parts[0].split(".")[-1]] = max(0.1, round(float(parts[1]), 1))
            if not weights:
                raise ShardError("耗时表里没有可用条目")
            args.weights.parent.mkdir(parents=True, exist_ok=True)
            args.weights.write_text(
                json.dumps({"schema_version": WEIGHTS_SCHEMA, "source": str(args.from_timing.name), "weights": dict(sorted(weights.items()))}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"权重表已写入 {args.weights}（{len(weights)} 个模块）")
            return 0
        grouped = discover_cases(args.start, args.pattern)
        weights = load_weights(args.weights)
        shards = check(grouped, weights, args.count)
        if args.command == "check":
            for index, shard in enumerate(shards, 1):
                total = sum(weights.get(m, 1.0) for m in shard)
                print(f"分片 {index}/{args.count}：{len(shard)} 模块，权重 {total:.1f}")
            print(f"分片闭合：{len(grouped)} 模块、{sum(len(v) for v in grouped.values())} 用例")
            return 0
        if not 1 <= args.index <= args.count:
            raise ShardError("--index 必须在 1..count 之间")
        shard = shards[args.index - 1]
        if args.command == "list":
            total = sum(weights.get(m, 1.0) for m in shard)
            print(f"分片 {args.index}/{args.count}：{len(shard)} 模块，权重 {total:.1f}")
            for name in shard:
                print(name)
            return 0
        selected = unittest.TestSuite()
        for name in shard:
            selected.addTests(grouped[name])
        print(f"分片 {args.index}/{args.count}：{len(shard)} 模块，{selected.countTestCases()} 用例", flush=True)
        result = unittest.TextTestRunner(verbosity=1).run(selected)
        return 0 if result.wasSuccessful() else 1
    except ShardError as error:
        print(f"分片错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
