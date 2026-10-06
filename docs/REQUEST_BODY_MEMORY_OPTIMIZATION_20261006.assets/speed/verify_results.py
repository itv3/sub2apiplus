#!/usr/bin/env python3
"""核对归档结果、原始日志、源码摘要与门禁记录；不会重跑实验或修改文件。"""

import argparse
import hashlib
import json
import statistics
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, help="可选：同时核对当前仓库源码")
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
    performance = [json.loads(line) for line in (directory / "performance-results.jsonl").read_text().splitlines()]
    baseline = [json.loads(line) for line in (directory / "baseline-results.jsonl").read_text().splitlines()]
    performance_cases = json.loads((directory / "performance-cases.json").read_text())
    require(len(performance) == len(baseline) == len(performance_cases) == 6, "性能对照必须各保留两形态、三轮")
    require([row["case"] for row in performance] == [row["case"] for row in baseline] == performance_cases,
            "新旧性能对照使用了不同用例")
    comparisons = {}
    for current, previous in zip(performance, baseline):
        require(current["body_sha256"] == previous["body_sha256"] and
                current["body_bytes_total"] == previous["body_bytes_total"], "新旧固定样本不同")
        require(current["request_memory_peak_bytes"] <= 2.5 * current["body_bytes_total"], "性能复测超出内存目标")
        require(current["owned_memory_after_release_bytes"] == 0, "性能复测映射未释放")
        for row in (current, previous):
            require(row["container_state"]["ExitCode"] == 0 and row["container_state"]["OOMKilled"] is False,
                    "性能对照退出异常")
            require(row["gogc"] == 100 and row["gomemlimit_mib"] == 512 and
                    row["container_memory_limit_bytes"] == 768 << 20 and
                    row["concurrency"] == 2 and row["requests_per_slot"] == 4 and
                    len(row["wire_content_lengths"]) == 8,
                    "性能对照使用了不同运行参数或请求数量")
    for shape in ("native", "explicit_instructions"):
        current = [row["elapsed_ms"] for row in performance if row["body_shape"] == shape]
        previous = [row["elapsed_ms"] for row in baseline if row["body_shape"] == shape]
        comparisons[shape] = {"新中位数毫秒": statistics.median(current), "旧中位数毫秒": statistics.median(previous)}
    ci = json.loads((directory / "ci-summary.json").read_text())
    require(ci["status"] == "passed" and ci["unit_count"] == 48 and not ci["failed_units"], "CI 不是 48 项全通过")
    records = [json.loads(line) for line in (directory / "ci-unit-records.jsonl").read_text().splitlines()]
    require(len(records) == 48 and all(row["exit_code"] == 0 for row in records), "CI 单元记录不符")
    for name in ("container-logs.jsonl", "ci-logs.jsonl", "targeted-test-logs.jsonl"):
        for line in (directory / name).read_text().splitlines():
            row = json.loads(line)
            require(hashlib.sha256(row["content"].encode()).hexdigest() == row["sha256"], f"日志摘要不符：{row['name']}")
    logs = [json.loads(line) for line in (directory / "container-logs.jsonl").read_text().splitlines()]
    log_index = {(row["measurement_label"], row["case"]): row for row in logs}
    require(len(log_index) == 54, "最终矩阵和新旧性能对照原始日志不完整")
    for row in rows + performance + baseline:
        log = log_index[(row["measurement_label"], row["case"]["name"])]
        raw_results = [json.loads(line.split("MEMPROFILE_RESULT ", 1)[1])
                       for line in log["content"].splitlines() if "MEMPROFILE_RESULT " in line]
        require(len(raw_results) == 1, "一场必须对应一个原始测量结果")
        require(all(row.get(key) == value for key, value in raw_results[0].items()), "汇总与原始日志不一致")
    manifest = json.loads((directory / "evidence-sha256.json").read_text())
    for name, digest in manifest.items():
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, f"证据摘要不符：{name}")
    if args.repository:
        sources = json.loads((directory / "source-sha256.json").read_text())
        for name, digest in sources.items():
            require(hashlib.sha256((args.repository / name).read_bytes()).hexdigest() == digest, f"源码摘要不符：{name}")
    print(json.dumps({"状态": "核对通过", "场景": len(rows), "大正文通过": len(large),
                      "最高倍数": round(max(row["request_memory_peak_bytes"] / row["body_bytes_total"] for row in large), 4),
                      "CI通过": len(records), "性能对照": comparisons}, ensure_ascii=False))


if __name__ == "__main__":
    main()
