#!/usr/bin/env python3
"""只读解析收尾清理的当前身份和凭证；本入口不批准、不删除、不写收据。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
try:
    from . import phase_context, round_context
    from .parse_env import derive, parse
except ImportError:
    import phase_context
    import round_context
    from parse_env import derive, parse

SCHEMA = "arm64-cleanup-context/v1"


def resolve(config, *, upgrade=None):
    """消费当前 VC-5／VC-6 和 canonical 链，输出可纳入人工审核的稳定参数。"""
    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        context = phase_context.Context(config, upgrade=upgrade)
        context.stage_paths()
        if any(context.parameters[name + "_READY"] != "1" for name in phase_context.STAGES.values()):
            raise ValueError("清理前候选采集、比较、断言和验收必须全部完成")
        context.build(materialized=False)
        context.attempt()
        context.completion(required="VC-6")
        if context.parameters["VC5_COMPLETE"] != "1":
            raise ValueError("清理前 VC-5 必须完成并重放")
        checkpoint = context.canonical()
        index = context.cu._canonical_item_index(checkpoint)
        actions = []
        for step, key in (("production-activation", "ACTIVATION_RECEIPT"),
                          ("rollback-verification", "ROLLBACK_RECEIPT"), ("retire", "RETIRE_RECEIPT")):
            item = step if step != "retire" else "retire-" + config["RETIRE_VERSION"]
            if item not in index:
                raise ValueError("清理前 canonical 步骤未完成：" + item)
            path = Path(index[item]["source"]["path"])
            if path.is_absolute():
                raise ValueError("canonical 来源必须使用 Campaign 内相对路径")
            path = context.path(context.campaign / path)
            command = [sys.executable, str(Path(config["TOOLS"]) / "codex_upgrade.py"), "canonical-advance",
                       "--campaign-dir", str(context.campaign), "--candidate-id", context.candidate,
                       "--attempt-id", context.parameters["ATT"], "--canonical-step", step,
                       "--step-receipt", str(path)]
            if step == "retire":
                command += ["--retire-version", config["RETIRE_VERSION"]]
            actions.append({"action_id": "cleanup-step-" + str(len(actions) + 1), "operation": "VC-6:canonical-advance-" + step,
                            "timeout_seconds": 1800, "command": command, "item_ids": [item]})
            context.parameters[key] = str(path)
        plan = context.cu.codex_upgrade_vc_artifacts.validate_action_plan({
            "schema_version": "codex-upgrade-vc-action-plan/v1", "actions": actions,
            "execute_item_ids": sorted(action["item_ids"][0] for action in actions), "reuse_item_ids": []})
        phase_context._vc6_receipts(context, plan)
        report = context.finish()
        # 输出白名单避免将其他配置、凭证内容或将来的新增配置隐式传给清理脚本。
        names = ("B", "NEWDIR", "L", "CANDIDATE_REVISION", "EVALUATION_BASELINE", "ATT", "A", "EV",
                 "BUILD_RECEIPT", "SOURCE_ROOT", "SOURCE_COMMIT", "IMAGE_ID", "IMAGE_REF", "BUILD_ID",
                 "PROFILE_ID", "PROFILE_DIGEST", "VC5_RECEIPT", "VC5_CHECKPOINT", "VC6_RECEIPT", "VC6_CHECKPOINT",
                 "ACTIVATION_RECEIPT", "ROLLBACK_RECEIPT", "RETIRE_RECEIPT")
        parameters = {key: report["parameters"][key] for key in names}
        parameters.update({key: config[key] for key in ("D", "W", "NEW", "CAND", "TARGET_VERSION", "BASELINE_VERSION", "RETIRE_VERSION")})
        parameters.update({name + "_RESULT": report["parameters"][name + "_RESULT"] for name in phase_context.STAGES.values()})
        # canonical latest 的选择也须在输出前保持稳定，而不只核对旧文件仍在。
        if context.canonical() != checkpoint:
            raise ValueError("清理解析期间 canonical 发生变化")
        return {"schema_version": SCHEMA, "parameters": parameters, "bindings": report["bindings"]}
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        binding = round_context.binding(args.env)
        values = parse(args.env.read_text())
        report = resolve({**values, **derive(values)})
        if round_context.binding(args.env) != binding:
            raise ValueError("清理解析期间参数文件变化")
        report["bindings"]["round_env"] = binding
    except Exception as error:
        print(f"清理参数解析拒绝：{error}", file=sys.stderr)
        return 3
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
