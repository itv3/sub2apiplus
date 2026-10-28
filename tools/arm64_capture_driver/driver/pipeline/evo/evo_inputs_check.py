#!/usr/bin/env python3
"""VC-2 批准预览（classify-preview）输入的离线只读预检（EVO 路）。

背景：classify-preview 被拒是因为目标断言画像里残留 baseline 版本字面量。同一根因再失败一次，项目总账的
“同根因重试上限”（same_root_cause_retry_limit=2）就要触顶，所以重派批次 7 之前，把批次会做的全部输入校验在
进程内只读重放一遍，通过才派发。本脚本照 ``codex_upgrade.classify_campaign`` 批准分支的校验顺序做，只校验、
不写任何文件（不调用 classify_campaign 本身，不会落 classification/approved）：

  1. 目标断言画像：_validate_assertion_profile_manifest（baseline 字面量残留、行为版本坐标、规格摘要闭环）；
  2. 目标场景清单：official 执行契约必须等于 Formal 冻结值（描述类修改允许，命令／环境类不允许）；
  3. 场景作业装载：load_scenario_jobs（require_bindings）→ _validate_jobs → _validate_phase_coverage；
  4. 官方作业与已封存结果逐项核对：_validate_capture_job_results；
  5. 画像派生：validate_profile_derivation（活动画像＋目标画像＋迁移清单＋画像补丁）。

用法（ARM64，root，已部署工具下）：
  python3 -B evo_inputs_check.py --rules R --migration M --scenarios S --profile P --assertion A --active-profile V --patch X
  （路径缺省取 $W 下的批准输入副本与数据根工具树里的清单，即 classify-preview 实际会读的那组文件）
退出码：0 全部通过；3 某项被拒（打印哪一项与原因）；2 用法错误。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("EVO_DATA_ROOT", "/root/docker/capture-cli/data"))
def _need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"缺少环境变量 {name}（evolve-and-resume.sh 会按本轮参数文件导出；单独运行时请手工给出）")
    return value


CAMPAIGN = os.environ.get("EVO_CAMPAIGN", "")   # 本轮 Formal Campaign 名（参数文件的 NEW）
INPUTS = os.environ.get("EVO_INPUTS", "")      # 本轮输入目录名（参数文件的 IN）
TARGET = os.environ.get("EVO_TARGET_VERSION", "")  # 目标版本（参数文件的 TARGET_VERSION），用来定位受管清单文件名


def main() -> int:
    tools = DATA_ROOT / "tools" / "official_client_capture"
    work = DATA_ROOT / "control" / (INPUTS or _need("EVO_INPUTS"))
    target_tag = (TARGET or _need("EVO_TARGET_VERSION")).replace(".", "_")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign-dir", type=Path, default=DATA_ROOT / "evidence" / "campaigns" / (CAMPAIGN or _need("EVO_CAMPAIGN")))
    parser.add_argument("--rules", type=Path, default=work / "target-rules.json")
    parser.add_argument("--migration", type=Path, default=work / "rule-migration.json")
    parser.add_argument("--scenarios", type=Path, default=work / "scenarios.json")
    parser.add_argument("--profile", type=Path, default=work / "profile.json")
    parser.add_argument("--assertion", type=Path, default=work / "assertion-profile.json")
    parser.add_argument("--active-profile", type=Path, default=work / "baseline-profile.json")
    parser.add_argument("--patch", type=Path, default=tools / f"profile_rule_patches_{target_tag}.json")
    arguments = parser.parse_args()

    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(DATA_ROOT)
    sys.path.insert(0, str(DATA_ROOT))
    from tools.official_client_capture import codex_upgrade as cu  # noqa: E402

    def step(label: str, action):
        try:
            value = action()
        except Exception as error:  # noqa: BLE001 - 被拒就是检查结果
            print(f"CHECK_REJECTED [{label}] {type(error).__name__}: {error}")
            raise SystemExit(3)
        print(f"  OK  {label}")
        return value

    campaign_dir = arguments.campaign_dir
    manifest = cu._require_formal_campaign(campaign_dir)
    summary = cu._load_stage_result(campaign_dir, "capture-official", _shallow=True)
    target_rules = step("目标规则清单装载", lambda: cu.load_rule_manifest(arguments.rules, manifest["target_version"]))
    assertion_payload = step("断言画像读取", lambda: cu._approved_manifest_payload(arguments.assertion, "目标断言画像清单"))
    step("1 目标断言画像校验（含 baseline 字面量残留）", lambda: cu._validate_assertion_profile_manifest(
        assertion_payload, baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"], target_rules=target_rules))
    scenario_payload = step("场景清单读取", lambda: cu._approved_manifest_payload(arguments.scenarios, "目标场景清单"))
    reference = manifest.get("inputs", {}).get("target_discovery_scenarios")

    def contract():
        if reference is None:
            return
        frozen = cu._read_json(cu._campaign_file(campaign_dir, reference["path"]), "Formal 冻结的 target 场景清单")
        if cu._fingerprint(cu._official_scenario_execution_contract(scenario_payload)) != cu._fingerprint(
                cu._official_scenario_execution_contract(frozen)):
            raise cu.ConfigurationError("批准的目标场景 official 执行契约与 Formal Attempt 冻结值不一致")

    step("2 official 执行契约等于 Formal 冻结值", contract)
    scenario_arguments = cu._campaign_arguments(campaign_dir, manifest)
    scenario_arguments.scenario_manifest = arguments.scenarios
    context = cu._job_context(scenario_arguments)
    jobs = step("3a 场景作业装载", lambda: cu.load_scenario_jobs(
        arguments.scenarios, context, expected_version=manifest["target_version"],
        expected_rule_sha256=cu.file_sha256(arguments.rules), require_bindings=True))
    jobs = [job for job in jobs if manifest["suite"] in job.suites]
    step("3b 作业与规则闭环", lambda: cu._validate_jobs(jobs, target_rules))
    if manifest["suite"] == "full":
        step("3c 阶段覆盖", lambda: cu._validate_phase_coverage(jobs, target_rules))
    if summary.get("predecessor_import") is None:
        step("4 官方作业与已封存结果核对", lambda: cu._validate_capture_job_results(
            [job for job in jobs if job.phase == "official"], summary.get("results"), phase="official"))
    step("5 画像派生", lambda: cu.validate_profile_derivation(
        active_profile_path=arguments.active_profile, target_profile_path=arguments.profile,
        migration_path=arguments.migration, patch_manifest_path=arguments.patch))
    print("CHECK_OK classify-preview 的输入校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
