#!/usr/bin/env python3
"""工具演进只读预测（EVO 路）：部署之前，用“新工具树的副本”预演 tool-evolution 预览会判出什么。

为什么需要它：
  tool-evolution 必须先受监督部署才能预览（缺部署绑定会报错），而部署是单向门（换部署作废后台验证，+2 小时）。
  所以部署之前先判断“登记会不会被拒、哪些作业受影响、是否要重采”。本脚本不部署、不写任何账本或 Campaign：
  1. 新树在独立临时目录（--new-root，里面有 tools/official_client_capture 与 tools/*.py），
     用子进程（cwd 与 PYTHONPATH 都指向新树）算出新树的工具身份 current；
  2. 本进程加载“已部署的数据根”里的编排器（旧工具），把 _tool_identity 换成返回 current，
     把部署收据查询换成占位桩（预测用，不代表真有部署），然后原样调用受管的 _tool_evolution_preview；
  3. 打印预览摘要。预览本身是只读的（不取 Campaign 锁、不写文件）。

用法（ARM64，root）：
  python3 -B evo_predict.py --new-root /var/tmp/evo-predict/new-XXXX [--json] [--campaign-dir ...]
退出码：0 预览成功（会放行）；3 预览被拒（打印原因，等同正式 tool-evolution 的拒绝）；2 用法或环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("EVO_DATA_ROOT", "/root/docker/capture-cli/data"))
def _need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"缺少环境变量 {name}（evolve-and-resume.sh 会按本轮参数文件导出；单独运行时请手工给出）")
    return value


DEFAULT_CAMPAIGN = os.environ.get("EVO_CAMPAIGN", "")   # 本轮 Formal Campaign 名（参数文件的 NEW）


def compute_new_identity(new_root: Path, memo_dir: Path) -> dict:
    """在新树里算 _tool_identity（子进程隔离，避免两棵树的同名模块混在一个进程里）。"""

    code = (
        "import json, sys\n"
        "from tools.official_client_capture import codex_upgrade as cu\n"
        "sys.stdout.write(json.dumps(cu._tool_identity(include_git=False)))\n"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(new_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        # 身份记忆化缓存指向临时目录，不碰生产的 identity-memo。
        "CODEX_UPGRADE_IDENTITY_MEMO": str(memo_dir),
        "HOME": os.environ.get("HOME", "/root"),
    }
    done = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(new_root), env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=1800,
    )
    if done.returncode != 0:
        sys.stderr.write("新树工具身份计算失败：\n" + done.stderr[-3000:] + "\n")
        raise SystemExit(2)
    return json.loads(done.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--new-root", required=True, type=Path, help="新树根目录（含 tools/official_client_capture）")
    parser.add_argument("--campaign-dir", type=Path, default=DATA_ROOT / "evidence" / "campaigns" / (DEFAULT_CAMPAIGN or _need("EVO_CAMPAIGN")))
    parser.add_argument("--fix-commit", default="f" * 40, help="预测用的占位提交号（40 位小写十六进制）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（默认输出摘要）")
    arguments = parser.parse_args()

    new_root = arguments.new_root.resolve()
    if not (new_root / "tools" / "official_client_capture" / "codex_upgrade.py").is_file():
        sys.stderr.write(f"--new-root 下没有 tools/official_client_capture/codex_upgrade.py：{new_root}\n")
        return 2
    if str(new_root) == str(DATA_ROOT) or str(new_root).startswith(str(DATA_ROOT) + "/"):
        sys.stderr.write("--new-root 不得是数据根或其子目录（预测只对独立副本做）\n")
        return 2
    memo_dir = Path("/var/tmp/evo-predict/memo")
    memo_dir.mkdir(parents=True, exist_ok=True)
    current = compute_new_identity(new_root, memo_dir)

    # 以下在“已部署的旧工具”里运行受管的预览函数。
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = str(memo_dir)
    os.chdir(DATA_ROOT)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(DATA_ROOT))
    from tools.official_client_capture import codex_upgrade as cu  # noqa: E402
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler  # noqa: E402

    campaign_dir = arguments.campaign_dir
    manifest = cu._require_formal_campaign(campaign_dir)
    cu._tool_identity = lambda *, include_git=True: current  # 预测：当前受管工具＝新树
    reconciler._deployment_receipt = lambda *args, **kwargs: {  # 预测：占位部署收据
        "created_at_utc": "2999-01-01T00:00:00+00:00",
        "predicted_only": True,
    }
    try:
        preview = cu._tool_evolution_preview(
            campaign_dir, manifest, fix_commit=arguments.fix_commit, reason="预测（不登记）",
            control_root=None,
        )
    except Exception as error:  # noqa: BLE001 - 预览被拒就是预测结果
        print(f"PREDICT_REJECTED {type(error).__name__}: {error}")
        return 3
    summary = {
        "from_summary": preview["from_summary"],
        "to_summary": preview["to_summary"],
        "changes": preview["changes"],
        "impact": preview["impact"],
        "evaluator": preview["evaluator"],
        "campaign_ledger_head": preview["campaign_ledger_head"],
    }
    if arguments.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    changes = summary["changes"]
    print("PREDICT_OK 预览会放行（部署后真正的 tool-evolution 预览应得到同样的影响判定）")
    print("层级漂移：", {layer: len(paths) for layer, paths in changes["paths_by_layer"].items() if paths})
    for layer, paths in changes["paths_by_layer"].items():
        if paths:
            print(f"  [{layer}] " + "、".join(paths))
    print("impact_paths（产出侧，决定重采）：", changes["impact_paths"])
    print("evaluation_paths（证据语义侧，追加评估 epoch）：", changes["evaluation_paths"])
    print("unmapped_paths：", changes["unmapped_paths"], "| wire_closure_changed：", changes["wire_closure_changed"],
          "| evidence_closure_changed：", changes["evidence_closure_changed"])
    print("official 受影响作业：", summary["impact"]["official"]["affected_job_ids"],
          "| 计划作业数", len(summary["impact"]["official"]["planned_job_ids"]),
          "| 已封存", summary["impact"]["official"]["sealed"])
    print("候选：", summary["impact"]["candidates"], "| 无效候选：", summary["impact"]["inactive_candidates"])
    print("evaluator 变化字段：", summary["evaluator"]["changed_fields"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
