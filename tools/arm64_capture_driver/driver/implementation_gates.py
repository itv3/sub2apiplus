#!/usr/bin/env python3
"""执行本轮批准门禁并生成实现测试 facts，不内置某个版本的规则或测试名。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ["D"])
from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt


def load_plan():
    campaign = Path(os.environ["D"]) / "evidence/campaigns" / os.environ["NEW"]
    manifest = upgrade.load_campaign_manifest(campaign)
    classification = upgrade._load_stage_result(campaign, "classify")
    _, _, requirements = upgrade._load_vc3_gate_requirements(campaign, manifest, classification)
    path = Path(os.environ["B"]) / "source" / os.environ["LIFECYCLE_DIR"] / "gate-plan.json"
    plan = artifacts.validate_gate_plan(json.loads(path.read_text()), requirements)
    mapping = path.with_name("gate-mapping.json").read_bytes()
    approved = artifacts.build_gate_plan(requirements, json.loads(mapping), mapping_sha256=hashlib.sha256(mapping).hexdigest())
    if plan != approved:
        raise ValueError("执行计划与本轮门禁映射不一致")
    return manifest, requirements, plan


def run_gates(root: Path) -> None:
    _, _, plan = load_plan()
    gate_tree = Path(os.environ["B"]) / "gate-tree"
    tree = upgrade._directory_tree_digest(gate_tree)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    passed = True
    with (root / "logs/implementation.log").open("x") as stream:
        stream.write(f"# 本轮批准的实现门禁\ncommit={os.environ['C']}\ngate_tree_sha256={tree}\nplan_sha256={plan['plan_sha256']}\n")
        for row in plan["gates"]:
            directory = (gate_tree / row["working_directory"]).resolve(strict=True)
            directory.relative_to(gate_tree.resolve())
            stream.write(f"\n## gate {row['gate_id']} (working_directory={row['working_directory']})\n$ {shlex.join(row['command'])}\n")
            stream.flush()
            result = subprocess.run(row["command"], cwd=directory, stdout=stream, stderr=subprocess.STDOUT, check=False)
            stream.write(f"\nexit_code={result.returncode}\n")
            passed = passed and result.returncode == 0
        after = upgrade._directory_tree_digest(gate_tree)
        stream.write(f"gate_tree_sha256_after={after}\n")
        if not passed or after != tree:
            raise ValueError("本轮门禁失败或执行期间源码树漂移")
        stream.write("GATES_DONE " + datetime.now(timezone.utc).isoformat() + "\n")


def current_inputs(plan: dict) -> dict:
    """构建完成后读取二进制实际 Go 版本，与执行前已冻结的来源参数交叉核对。"""

    base = Path(os.environ["B"])
    parameters = json.loads((base / "artifacts/build-parameters.json").read_text())
    output = subprocess.run(["go", "version", "-m", str(base / "artifacts/sub2api")],
                            capture_output=True, text=True, check=True, timeout=30).stdout
    return build.collect_implementation_inputs(parameters,
        source_tree_sha256=upgrade._directory_tree_digest(base / "source"),
        requirements_sha256=plan["requirements_sha256"], go_version=build._parse_go_build_info(output)["go_version"])


def target_revision() -> Path:
    campaign = Path(os.environ["D"]) / "evidence/campaigns" / os.environ["NEW"]
    manifest = upgrade.load_campaign_manifest(campaign)
    revision, record = upgrade._require_candidate_in_current_revision(campaign, manifest, os.environ["CAND"], action="实现测试复用")
    if record is None:
        raise ValueError("实现测试复用要求显式登记当前 revision")
    return campaign / "control/vc/revisions" / f"r{revision}" / "revision.json"


def retest_plan(root: Path, *, write: bool = False) -> dict:
    _, _, plan = load_plan()
    inputs = current_inputs(plan)
    path = target_revision()
    revision = json.loads(path.read_text())
    proof = None
    if revision["supersedes"] is not None:
        old_path = path.parents[4] / "candidates" / revision["supersedes"]["candidate_id"] / "build-receipt.json"
        if old_path.exists():
            old = artifacts.validate_candidate_build_receipt(json.loads(old_path.read_text()))
            if old["build"].get("inputs") is not None:
                proof = vc_receipt.plan_implementation_reuse(path, inputs)
    decision = (proof if proof is not None else build.implementation_retest_plan(None, inputs,
                [row["gate_id"] for row in plan["gates"]] + ["check-egress-spec"]))
    value = {"schema_version": "arm64-implementation-retest-plan/v1", "inputs": inputs,
             "decision": decision, "target_revision": str(path)}
    output = root / "retest-plan.json"
    if output.exists():
        if output.is_symlink() or json.loads(output.read_text()) != value:
            raise ValueError("已有实现测试计划与实际构建输入或前序收据不同")
    elif write:
        with output.open("x") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        output.chmod(0o600)
    else:
        raise ValueError("缺少冻结的实现测试重跑／复用计划")
    return value


def make_facts(root: Path, tree: str) -> None:
    manifest, requirements, plan = load_plan()
    decision = retest_plan(root) if (root / "retest-plan.json").exists() else None
    if decision is not None and decision["decision"]["mode"] == "all":
        facts = vc_receipt.build_reused_implementation_facts(Path(decision["target_revision"]), decision["inputs"])
        write_facts(root, facts)
        return
    text = (root / "logs/implementation.log").read_text()
    required_lines = (f"commit={os.environ['C']}", f"gate_tree_sha256={tree}", f"gate_tree_sha256_after={tree}",
                      f"plan_sha256={plan['plan_sha256']}")
    if not tree or any(text.splitlines().count(line) != 1 for line in required_lines) or not re.search(r"^GATES_DONE .+$", text, re.M):
        raise ValueError("门禁日志缺少本轮提交、源码树、批准计划或完成标记")
    if re.findall(r"^## gate (\S+) \(", text, re.M) != [row["gate_id"] for row in plan["gates"]]:
        raise ValueError("门禁日志未精确覆盖本轮批准集合")
    expected = {row["gate_id"]: row for row in requirements["requirements"]}
    gates = []
    for row in plan["gates"]:
        segment = text.split(f"## gate {row['gate_id']} (", 1)[1].split("## gate ", 1)[0]
        codes = re.findall(r"^exit_code=(-?\d+)$", segment, re.M)
        if codes != ["0"] or f"working_directory={row['working_directory']})\n$ {shlex.join(row['command'])}\n" not in segment:
            raise ValueError(f"门禁未成功：{row['gate_id']}")
        gates.append({"gate_id": row["gate_id"], "kind": "affected" if expected[row["gate_id"]]["kind"] == "affected_rule" else "public",
                      "command": row["command"], "exit_code": 0, "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0})
    if decision is not None and decision["decision"]["mode"] == "target_platform":
        facts = vc_receipt.build_reused_implementation_facts(Path(decision["target_revision"]), decision["inputs"], gates)
        write_facts(root, facts)
        return
    spec = (root / "logs/check-egress-spec.log").read_text().split("## make check-egress-spec", 1)[1]
    if re.findall(r"^exit_code=(-?\d+)$", spec, re.M) != ["0"]:
        raise ValueError("本机 check-egress-spec 未成功")
    gates.append({"gate_id": "check-egress-spec", "kind": "public", "command": ["make", "check-egress-spec"],
                  "exit_code": 0, "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0})
    facts = {"schema_version": "codex-upgrade-vc-receipt-facts/v1", "kind": "implementation_tests",
             "subject": {"upgrade_id": os.environ["UP"], "campaign_id": manifest["campaign_id"],
                         "campaign_purpose": manifest["campaign_purpose"], "baseline_version": manifest["baseline_version"],
                         "target_version": manifest["target_version"], "candidate_id": os.environ["CAND"], "attempt_id": None},
             "assertions": {"git_commit": os.environ["C"], "source_tree_sha256": tree,
                            "target_architecture": "linux/arm64", "gates": sorted(gates, key=lambda row: row["gate_id"])},
             "evidence": [{"role": "check_egress_spec", "path": "logs/check-egress-spec.log"},
                          {"role": "implementation_tests", "path": "logs/implementation.log"}]}
    if decision is not None:
        facts["assertions"]["build_inputs"] = decision["inputs"]
    write_facts(root, facts)


def write_facts(root: Path, facts: dict) -> None:
    encoded = (json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path = root / "facts.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ValueError("已有实现测试 facts 与本次重算不一致")
    else:
        with path.open("xb") as stream:
            stream.write(encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "facts", "plan-retest"))
    parser.add_argument("evidence_root", type=Path)
    parser.add_argument("--tree")
    args = parser.parse_args()
    try:
        if args.action == "plan-retest":
            result = retest_plan(args.evidence_root, write=True)["decision"]
            print(json.dumps({key: result[key] for key in ("mode", "execute_gate_ids", "reuse_gate_ids", "changed_inputs")}, ensure_ascii=False))
        elif args.action == "run":
            run_gates(args.evidence_root)
        else:
            make_facts(args.evidence_root, args.tree)
        return 0
    except (ValueError, OSError, KeyError, IndexError, subprocess.SubprocessError,
            upgrade.ConfigurationError, artifacts.VCArtifactError, build.CandidateBuildError, vc_receipt.VCReceiptError) as error:
        print(f"实现门禁拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
