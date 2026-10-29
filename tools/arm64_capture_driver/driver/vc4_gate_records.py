#!/usr/bin/env python3
"""VC-4 门禁（arm64-vc4-gates.sh）经统一执行器一次运行之后，把入口门禁的记录改写成 VC-4／VC-5 一直读的那套门禁产物。

背景：VC-4 实现门禁原来在候选链 DC 上顺序执行 ``make check-egress-spec`` 与 ``make test``（后者又含一遍前者），产物是
``local-gates`` 六件套（check-egress-spec 与 full-regression 各 gate.json／stdout.log／stderr.log）和 ``impl-logs/check-egress-spec.log``。
现在改成入口门禁 ``entry-gates.sh --profile vc4`` 一次运行（组合 vc4 = make test 的组成，其中 check-egress-spec 门禁项就是
make check-egress-spec 本身），支持同提交续跑沿用与换提交补跑。本脚本只做格式转换，不做判定：

* ``local-gates/<门禁>.gate.json``：字段与改造前逐字段相同（gate_id、command、working_directory、host、architecture、起止时间、
  exit_code、tree、tree_head、isolation——``build_gate_facts.py`` 与 ``vc5-all.sh`` 只读这些），另带 ``unit_execution``（运行清单摘要、本次执行与
  沿用的单元数、续跑来源）；按客户端筛选的子集运行另带 ``client_selection``，让读的人分得出它不是全集；
* ``local-gates/<门禁>.stdout.log`` ／ ``.stderr.log``：由执行器的逐单元日志拼成（沿用的单元取记录库里的日志，标明来自哪次运行）；
* ``impl-logs/check-egress-spec.log``：VC-4 实现测试日志，保留改造前的头部字段与 ``exit_code=`` 行。

退出码 0 写出；3 入口门禁产物缺失或格式不对。标准输出末尾打印 ``SPEC_RC=<n> REG_RC=<n>`` 供脚本读取。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 一个单元日志最多放进拼接日志的字节数（失败的单元放宽）：整份日志仍在记录库里，这里只要让人读得到现场。
CLIP_BYTES = 40 * 1024
CLIP_BYTES_FAILED = 200 * 1024


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"VC4_GATE_RECORDS_ERROR：读不出 {path}：{error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"VC4_GATE_RECORDS_ERROR：{path} 不是 JSON 对象")
    return value


def _clip(path: str | None, limit: int) -> str:
    if not path:
        return "（没有日志）\n"
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        return f"（日志读不出：{error}）\n"
    text = data[-limit:].decode("utf-8", "replace")
    return (f"…（前 {len(data) - limit} 字节略，全文见记录库／运行目录）\n" if len(data) > limit else "") + text + ("" if text.endswith("\n") else "\n")


def _members(entry_out: Path, gate_ids: list[str], summary: dict[str, Any]) -> list[dict[str, Any]]:
    """门禁项的成员单元（门禁记录里的 units 与测试组的单元），按汇总里的逐单元行取。"""

    rows = {row["unit_id"]: row for row in summary["units"]}
    chosen: list[str] = []
    for gate_id in gate_ids:
        record = _read(entry_out / "logs" / f"{gate_id}.gate.json")
        chosen.extend(record["units"])
        for group_id in record.get("test_groups") or []:
            chosen.extend(summary["test_groups"][group_id]["units"])
    seen: set[str] = set()
    return [rows[unit_id] for unit_id in chosen if unit_id in rows and not (unit_id in seen or seen.add(unit_id))]


def _logs(entry_out: Path, gate_ids: list[str], summary: dict[str, Any], title: str) -> tuple[str, str]:
    stdout, stderr = [f"# {title}：统一调度执行器一次运行的逐单元日志（沿用的单元取自上一轮记录，已标明来源运行）\n"], []
    for row in _members(entry_out, gate_ids, summary):
        failed = row.get("passed") is not True
        origin = row.get("disposition", "executed")
        stdout.append(f"\n=== {row['unit_id']} rc={row.get('exit_code')} {origin} {row.get('seconds')}s\n")
        stdout.append(_clip(row.get("log"), CLIP_BYTES_FAILED if failed else CLIP_BYTES))
        if failed:
            stderr.append(f"=== {row['unit_id']} rc={row.get('exit_code')} {origin}\n")
            stderr.append(_clip(row.get("log"), CLIP_BYTES_FAILED))
    return "".join(stdout), "".join(stderr)


def build(args: argparse.Namespace) -> int:
    entry_out, gates_dir, impl_dir = Path(args.entry_out), Path(args.gates_dir), Path(args.impl_dir)
    entry = _read(entry_out / "entry-gates.json")
    summary = _read(Path(entry["executor_summary"]))
    spec_gate = _read(entry_out / "logs" / "check-egress-spec.gate.json")
    regression_gate = _read(entry_out / "logs" / "full-regression.gate.json")
    resume = summary.get("resume") if isinstance(summary.get("resume"), dict) else None
    manifest_ref = summary.get("unit_manifest") or {}
    execution = {"run_id": summary.get("run_id"), "mode": summary.get("mode"), "manifest_sha256": manifest_ref.get("manifest_sha256"),
                 "executed": (summary.get("inheritance") or {}).get("executed"), "inherited": (summary.get("inheritance") or {}).get("inherited"),
                 "self_check": manifest_ref.get("self_check"), "resume": resume}
    selection = entry.get("client_selection") if isinstance(entry.get("client_selection"), dict) else None
    fields = ("gate_id", "command", "working_directory", "host", "architecture", "started_at_utc", "completed_at_utc", "exit_code", "tree",
              "tree_head", "isolation")
    for record, gate_ids, title in ((spec_gate, ["check-egress-spec"], "make check-egress-spec"),
                                    (regression_gate, list(regression_gate["composed_of"]), "make test")):
        payload = {key: record.get(key) for key in fields}
        payload["unit_execution"] = execution
        if record.get("time_basis"):
            payload["time_basis"] = record["time_basis"]
        if selection is not None:
            payload["client_selection"] = selection
        stdout, stderr = _logs(entry_out, gate_ids, summary, title)
        gate_id = record["gate_id"]
        (gates_dir / f"{gate_id}.stdout.log").write_text(stdout, encoding="utf-8")
        (gates_dir / f"{gate_id}.stderr.log").write_text(stderr, encoding="utf-8")
        (gates_dir / f"{gate_id}.gate.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("stdout.log", "stderr.log", "gate.json"):
            (gates_dir / f"{gate_id}.{name}").chmod(0o600)
    # 实现测试日志：头部字段保留改造前的写法，正文列出 check-egress-spec 各子检查的结果。
    checks = [row for row in _members(entry_out, ["check-egress-spec"], summary)]
    lines = [f"# VC-4 实现测试：make check-egress-spec（{args.round}）",
             f"candidate_commit={args.candidate}（候选源码树，record-candidate-build 绑定的 commit）",
             f"executed_on_commit={args.executed}（= 候选 commit + 描述它的冻结承接收据 {args.receipt}）",
             "reason=冻结承接收据按工具约束不得进入它所描述的提交，而 check-egress-spec 含 Go 冻结承接测试；在候选 commit 上单独执行 "
             "check-egress-spec-ci 作交叉核对（见 cross-check/）",
             f"executed_by=统一调度执行器 entry-gates.sh --profile vc4（run_id={execution['run_id']}，本次执行 {execution['executed']} 个单元、"
             f"沿用 {execution['inherited']} 个）",
             f"worktree={spec_gate.get('tree')}", f"executed_on={args.isolation}",
             f"started={spec_gate.get('started_at_utc')} completed={spec_gate.get('completed_at_utc')}", "",
             "## make check-egress-spec（子检查逐个）"]
    for row in checks:
        lines.append(f"{row['unit_id']}\trc={row.get('exit_code')}\t{row.get('disposition', 'executed')}\t{row.get('seconds')}s")
    lines.append(f"exit_code={spec_gate.get('exit_code')}")
    (impl_dir / "check-egress-spec.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (impl_dir / "check-egress-spec.log").chmod(0o600)
    print(f"SPEC_RC={spec_gate.get('exit_code')} REG_RC={regression_gate.get('exit_code')}")
    return 0


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    for name in ("entry-out", "gates-dir", "impl-dir", "round", "candidate", "executed", "receipt", "isolation"):
        build_parser.add_argument(f"--{name}", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
