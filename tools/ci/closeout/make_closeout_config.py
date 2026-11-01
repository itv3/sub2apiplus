#!/usr/bin/env python3
"""收尾：生成 ``codex_closeout.py`` 的配置（closeout-config.json），在 ARM64 上跑。

做什么
    早年配置是手写的；这里把路径和轮次取值参数化，避免手抄。配置字段集合与 ``codex_closeout.validate_config`` 要求的完全一致
    （schema_version、repo、work_root、guide、material、terminal_facts、terminal_relative、tag、gate、cleanup、push、deployment_check）。
输入
    python3 -B make_closeout_config.py --closeout-dir <ARM64 收尾目录> --target-version <目标版本> --baseline-version <基线版本> \\
        --runround <ARM64 轮次目录> --campaign <Campaign 全名> --candidate <候选 id> --fact-name <激活事实文件名> \\
        --tag <收尾标识> --expected-tip <GitHub main 当前完整提交号> --node-modules <锁定前端依赖目录> \\
        --output <收尾目录>/closeout-config.json
    所有输入文件必须已经存在（指南 final-guide.md、材料 draft/material.json、终态事实 terminal-facts-<目标标记>.json、驱动、
    参数文件、清理决定、部署核验脚本 deployment_check.py）。
    ``--expected-tip`` 必须现取（``git ls-remote origin refs/heads/main``），不凭记忆。
输出
    --output 指定的配置文件（0600）；已存在则拒绝覆盖。可选的 ``gate.work`` / ``gate.record_store`` / ``gate.unit_inherit`` 只在三者同时给出时写
    （逐单元承接的请求后置写法，评估后收尾已不使用，保留选项以备后用）。
何时用
    b10b-prepare 步骤，在 ``codex_closeout.py draft`` 之后、``prepare --dry-run`` 之前。
"""
import argparse
import json
import sys
from pathlib import Path


def tag_of(version):
    """0.149.0 -> 0149；0.149.1 -> 01491（与终态收据文件名一致，补丁为 0 时省略）。"""
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}{minor:03d}{patch if patch else ''}"


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--closeout-dir", required=True)
    p.add_argument("--target-version", required=True, help="目标版本；终态收据与终态事实的文件名标记由它和基线版本推出")
    p.add_argument("--baseline-version", required=True)
    p.add_argument("--runround", required=True, help="ARM64 轮次目录（其下 vc6/vc6-root.txt 等必须已存在）")
    p.add_argument("--campaign", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--fact-name", required=True, help="ARM64 激活事实文件名（vc6-env.sh 的 ACT_FACT_NAME）")
    p.add_argument("--tag", required=True, help="收尾标识，字母数字开头，只含字母数字._-")
    p.add_argument("--expected-tip", required=True, help="推送前远端 main 的完整提交号（ls-remote 现取，不凭记忆）")
    p.add_argument("--node-modules", required=True, help="门禁用的锁定前端依赖目录（ARM64 上，沿用上一轮配置里的值）")
    p.add_argument("--output", required=True)
    p.add_argument("--data", default="/root/docker/capture-cli/data")
    p.add_argument("--repo-name", default="repo", help="权威克隆目录名；已有门禁在用 repo 时（load_plan 要求克隆头不变），新方案用另一个克隆 repo-r3 等")
    p.add_argument("--work-name", default="work-1", help="工作根目录名；指南定稿后需要重做 prepare 时换成 work-2 等新名字（prepare 在输入变化时拒绝复用旧工作根）")
    p.add_argument("--gate-driver", default="/root/arm64-capture-driver/driver/entry-gates.sh")
    p.add_argument("--deployment-script", default="deployment_check.py", help="部署核验脚本在收尾目录里的文件名")
    p.add_argument("--act-fact-dir", default="/root/docker/sub2apiplus/app/data", help="ARM64 激活事实文件所在目录")
    # 逐单元承接（请求后置）：gate.work＝固定测试树坐标（与后台验证同一位置，承接要求测试树绝对路径一致）、
    # gate.record_store＝逐单元记录库；三者齐了才在 gate 里写 unit_inherit={"stage": …}
    p.add_argument("--gate-work", default="", help="固定测试树坐标，例如 <数据根的上一级>/entry-gates-source-work/fixed/<客户端>")
    p.add_argument("--record-store", default="", help="逐单元记录库目录（后台验证用的那个）")
    p.add_argument("--unit-inherit-stage", default="", help="承接阶段名（如 pre-ledger）；给了就必须同时给 --gate-work 与 --record-store")
    return p


def build_config(a):
    """按参数拼出配置字典（读轮次目录里 vc6-root.txt／vc6-retire-root.txt，所以必须在 ARM64 或同构目录上运行）。"""
    if bool(a.unit_inherit_stage) and not (a.gate_work and a.record_store):
        sys.exit("--unit-inherit-stage 须同时给 --gate-work 与 --record-store")
    closeout = Path(a.closeout_dir)
    campaign = f"{a.data}/evidence/campaigns/{a.campaign}"
    run_vc6 = Path(a.runround) / "vc6"
    vc6_root = (run_vc6 / "vc6-root.txt").read_text().strip()            # control/vc6-arm64-<TS>
    retire_root = (run_vc6 / "vc6-retire-root.txt").read_text().strip()  # control/vc6-retire-deploy-<STAMP>
    cleanup_decision = f"{campaign}/control/vc/receipts/{a.candidate}/cleanup-decision.json"
    stage = f"{retire_root}/stages/retire_switch/stage.json"
    activation = f"{vc6_root}/activation-receipt.json"
    fact = f"{a.act_fact_dir}/{a.fact_name}"
    baseline_tag, target_tag = tag_of(a.baseline_version), tag_of(a.target_version)
    config = {
        "schema_version": "codex-upgrade-closeout/v1",
        "repo": str(closeout / a.repo_name),
        "work_root": str(closeout / a.work_name),
        "guide": str(closeout / "final-guide.md"),
        "material": str(closeout / "draft/material.json"),
        "terminal_facts": str(closeout / f"terminal-facts-{target_tag}.json"),
        "terminal_relative": f"docs/egress/maintenance/CODEX_CLI_{baseline_tag}_TO_{target_tag}_TERMINAL_STATE_RECEIPT.json",
        "tag": a.tag,
        "gate": {
            "driver": a.gate_driver,
            "vc_env": f"{a.runround}/env.sh",
            "node_modules": a.node_modules,
        },
        "cleanup": {
            "mode": "defer",
            "reason": "VC-6 已按既定决定登记 deferred 清理决定（只读列出候选删除目标与必须保留项，未删除）；实际清理另需精确目标批准，收尾不执行删除",
            "inputs": [cleanup_decision],
        },
        "push": {"remote": "origin", "ref": "refs/heads/main", "expected_tip": a.expected_tip},
        "deployment_check": {
            "script": str(closeout / a.deployment_script),
            "arguments": [stage, activation, fact],
            "inputs": [stage, activation],
            "result": str(closeout / a.work_name / "deployment-check.json"),
        },
    }
    if a.gate_work:
        config["gate"]["work"] = a.gate_work
    if a.record_store:
        config["gate"]["record_store"] = a.record_store
    if a.unit_inherit_stage:
        config["gate"]["unit_inherit"] = {"stage": a.unit_inherit_stage}
    return config


def main(argv=None):
    a = build_parser().parse_args(argv)
    config = build_config(a)
    out = Path(a.output)
    if out.exists():
        sys.exit(f"已存在，拒绝覆盖：{out}")
    out.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    out.chmod(0o600)
    print("已写", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
