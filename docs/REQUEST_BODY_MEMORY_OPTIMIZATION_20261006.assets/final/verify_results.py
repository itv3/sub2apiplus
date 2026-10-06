#!/usr/bin/env python3
"""核对归档结果、原始日志、源码摘要与门禁记录；不会重跑实验或修改文件。"""

import argparse
import hashlib
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, help="可选：同时核对当前仓库中的 82 份源码")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
    cases = json.loads((directory / "cases.json").read_text())
    require(len(rows) == len(cases) == 42, "必须保留完整 42 场结果")
    require([row["case"] for row in rows] == cases, "结果与用例顺序或内容不一致")
    require(len({row["case"]["name"] for row in rows}) == 42, "用例名称重复")
    large = []
    for row in rows:
        name = row["case"]["name"]
        require(row["container_state"]["ExitCode"] == 0, f"{name} 退出失败")
        require(row["container_state"]["OOMKilled"] is False, f"{name} 发生 OOM")
        require(row["owned_memory_before_bytes"] == row["owned_memory_after_release_bytes"] == 0,
                f"{name} 映射未释放或基线持有原文")
        require(row["go_version"] == "go1.27.1" and row["gogc"] == 100 and row["gomemlimit_mib"] == 512,
                f"{name} 使用不同运行参数")
        if row["body_bytes_total"] / row["concurrency"] >= 16 * 1024 * 1024:
            require(row["request_memory_peak_bytes"] <= 2.5 * row["body_bytes_total"], f"{name} 超过 2.5 倍")
            require(row["large_body_target_met"] is True, f"{name} 目标字段错误")
            large.append(row)
    require(len(large) == 35, "大正文场景数量不符")
    ci = json.loads((directory / "ci-summary.json").read_text())
    require(ci["status"] == "passed" and ci["unit_count"] == 48 and not ci["failed_units"], "CI 不是 48 项全通过")
    records = [json.loads(line) for line in (directory / "ci-unit-records.jsonl").read_text().splitlines()]
    require(len(records) == 48 and all(row["exit_code"] == 0 for row in records), "CI 单元记录不符")
    for name in ("container-logs.jsonl", "ci-logs.jsonl", "targeted-test-logs.jsonl"):
        for line in (directory / name).read_text().splitlines():
            row = json.loads(line)
            require(hashlib.sha256(row["content"].encode()).hexdigest() == row["sha256"], f"日志摘要不符：{row['name']}")
    manifest = json.loads((directory / "evidence-sha256.json").read_text())
    for name, digest in manifest.items():
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, f"证据摘要不符：{name}")
    if args.repository:
        sources = json.loads((directory / "source-sha256.json").read_text())
        for name, digest in sources.items():
            require(hashlib.sha256((args.repository / name).read_bytes()).hexdigest() == digest, f"源码摘要不符：{name}")
    print(json.dumps({"状态": "核对通过", "场景": len(rows), "大正文通过": len(large),
                      "最高倍数": round(max(row["request_memory_peak_bytes"] / row["body_bytes_total"] for row in large), 4),
                      "CI通过": len(records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
