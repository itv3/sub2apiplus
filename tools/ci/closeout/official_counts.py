#!/usr/bin/env python3
"""【可选组 --with-part2，现行流程已作废】收尾：对 VC-1 官方断言包复算每条规则每个 check 的匹配数（指南第二部分“官方样本”计数回填用）。

状态：收尾不再改第二部分（evidence_index 改读指南 4.9 小节），本脚本只在要把官方样本匹配数回填第二部分规则正文时才用；逻辑保留。
做什么
    用受管读侧同一函数（candidate_rule_assertion.load_observations／evaluate_rule）对只读取回的官方断言包求值，
    取每个 check 结果里的 matched_count（或 actual 里的 values 长度）；不分 dual_wire／candidate_profile——候选侧专属规则的判据也对官方观测求值，
    计数反映“官方样本里有几条满足该判据的选择条件”。某条规则在官方断言包上无法求值（规则场景在官方侧没有证据）记为 _error。
输入
    在仓库根运行：python3 -B official_counts.py --bundle <官方 attempt 目录或 assertion-bundle 目录> --profile <断言画像 JSON> --output <counts.json>
输出
    counts.json（规则 → check → 匹配数），并打印规则总数与无法求值的条数。
何时用
    仅第二部分需要回填官方样本计数时。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from tools.official_client_capture import candidate_rule_assertion as cra  # noqa: E402
from tools.official_client_capture.tests import registration_preview as rp  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--bundle", required=True)
p.add_argument("--profile", required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
evidence = rp.locate_evidence(Path(a.bundle))
profile = json.loads(Path(a.profile).read_text(encoding="utf-8"))
manifest, observations = cra.load_observations(evidence.bundle_dir / "capture-manifest.json", evidence.bundle_dir, evidence.codex_version)
counts: dict = {}
for rule in profile["rules"]:
    rid = rule["rule_id"]
    try:
        results = cra.evaluate_rule(profile, rid, observations, manifest, "official")
    except cra.AssertionConfigurationError as error:
        counts[rid] = {"_error": str(error)[:160]}
        continue
    row = {}
    for item in results:
        actual = item.get("actual")
        if isinstance(actual, dict) and "matched_count" in actual:
            row[item["id"]] = actual["matched_count"]
        elif isinstance(actual, dict) and "values" in actual:
            row[item["id"]] = len(actual["values"])
        else:
            row[item["id"]] = {"passed": item.get("passed"), "actual": json.dumps(actual, ensure_ascii=False)[:80]}
    counts[rid] = row
Path(a.output).write_text(json.dumps(counts, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
print("规则", len(counts), "；无法求值", sum(1 for v in counts.values() if "_error" in v))
