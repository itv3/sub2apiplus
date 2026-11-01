#!/usr/bin/python3
"""收尾：门禁通过后在 ARM64 生成两份批准件（指南审核、发布）。

做什么
    按 ``codex_closeout.approval`` 的合同写两份批准件：绑定 plan 的 review_sha256、scope、批准人、UTC 有效期与 proof 文件；
    文件 0600、属主为运行账号。已存在即拒绝覆盖。脚本不替任何人编造授权：批准人与授权依据全是必填参数，
    由调度者按老板的实际指令填写。没有老板对推 main／发版的明确指令时不要运行本脚本（项目规则：推 main、发版不下放）。
输入
    write_approvals.py <工作根> --approved-by '<批准人及依据>' --authorization-scope '<授权范围>' --guide-review '<指南审核说明>' \\
        --instruction '<老板原话或令号>' [--instruction ...] [--valid-hours 6]
    工作根里要有 plan.json（含 review_sha256）与 journal/gates-done.json（门禁已通过的标志）。
输出
    <工作根上一级>/approval-proof.json、approval-guide-review.json、approval-release-publication.json（都是 0600）。
何时用
    收尾全集门禁绿之后、``codex_closeout.py publish`` 之前，且已拿到老板的明确指令。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCOPES = (("guide-review", "guide-review-approval/v1"), ("release-publication", "codex-closeout-operation-approval/v1"))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work")
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--authorization-scope", required=True)
    parser.add_argument("--guide-review", required=True)
    parser.add_argument("--instruction", action="append", required=True)
    parser.add_argument("--valid-hours", type=float, default=6)
    return parser


def main(argv=None, now=None):
    args = build_parser().parse_args(argv)
    work = Path(args.work)
    plan = json.loads((work / "plan.json").read_text())
    review = plan["review_sha256"]
    gates_done = work / "journal" / "gates-done.json"
    if not gates_done.is_file():
        raise SystemExit("门禁尚未通过（缺 journal/gates-done.json），不签批准")
    now = now or datetime.now(timezone.utc)

    def stamp(moment):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    proof = work.parent / "approval-proof.json"
    if not proof.exists():
        proof.write_text(json.dumps({
            "schema_version": "codex-closeout-authorization-proof/v1",
            "authorized_by": args.approved_by,
            "instructions": args.instruction,
            "scope": args.authorization_scope,
            "guide_review": args.guide_review,
            "plan_review_sha256": review,
            "recorded_at_utc": stamp(now),
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.chmod(proof, 0o600)
    binding = {"path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
    for scope, schema in SCOPES:
        path = work.parent / f"approval-{scope}.json"
        if path.exists():
            raise SystemExit(f"批准件已存在，拒绝覆盖：{path}")
        value = {"schema_version": schema, "status": "approved", "review_sha256": review, "scope": scope,
                 "approved_by": args.approved_by, "approved_at_utc": stamp(now - timedelta(minutes=1)),
                 "expires_at_utc": stamp(now + timedelta(hours=args.valid_hours)), "proof": binding}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print("已写", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
