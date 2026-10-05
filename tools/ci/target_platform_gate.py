#!/usr/bin/env python3
"""B-10 目标平台门禁：同一全集来源的单元承接、差异执行与可重放证据。

生产缺省仍执行原 make test。只有显式请求才进入本入口；批准或来源失效时
执行目标全集。工程验收不产生批准、不续期，不把承接记录写成新执行记录。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path


def sibling(name):
    key = "_target_gate_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


fs = sibling("full_set_receipt")
CONTRACT = "target-platform-inheritance/v1"
REQUEST_SCHEMA = "target-platform-request/v1"
EVIDENCE_SCHEMA = "target-platform-unit-evidence/v1"
COMMAND = ["python3", "-B", "tools/ci/target_platform_gate.py", "run"]
GATE_IDS = frozenset(("backend-go-test", "backend-lint", "frontend-lint", "frontend-typecheck", "frontend-critical",
                      "test-capture-tools", "test-official-client-control", "check-egress-spec"))
SUBJECT_FIELDS = frozenset(("campaign_id", "candidate_id", "profile_digest", "candidate_image_id",
                            "candidate_source_tree_sha256", "target_architecture"))
ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError)


def target_check(request, binding):
    """执行环境镜像与被验收候选镜像是不同身份，二者分别绑定，不能强行相等。"""
    subject = request.get("target")
    if not isinstance(subject, dict) or set(subject) != SUBJECT_FIELDS:
        raise fs.ContractError("目标候选身份字段不完整")
    for field in ("profile_digest", "candidate_source_tree_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(subject[field])):
            raise fs.ContractError("目标候选摘要不完整")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(subject["candidate_image_id"])):
        raise fs.ContractError("候选镜像必须绑定实际 Image ID")
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(binding["architecture"])
    if architecture is None or subject["target_architecture"] != "linux/" + architecture:
        raise fs.ContractError("目标平台与来源架构不符")
    for field, key in (("campaign_id", "campaign"), ("candidate_id", "candidate"), ("profile_digest", "target_profile_digest")):
        if subject[field] != binding[key]:
            raise fs.ContractError("目标候选与全集来源身份不同：" + field)
    for ref in [*request["approvals"], request["platform_compatibility"]]:
        if fs.replay_ref(ref).get("target_sha256") != fs.digest(subject):
            raise fs.ContractError("B-10 专项批准或平台签字未绑定本次候选源码／镜像")
    return subject


def assess(request, currents, facts, *, gate_ids, now):
    """来源或共享条件失败全拒；仅规格／输入不同及新增单元作为差异重跑。"""
    records = fs.records_module()
    verdict = {"schema_version": "target-platform-decision/v1", "decided_at_utc": fs.timestamp(now),
               "action": "reexecute-all", "eligible": False, "reuse_enabled": request.get("reuse_enabled") is True,
               "source_receipt": request.get("receipt"), "source_run_id": None, "expires_at_utc": None,
               "reasons": [], "units": {}}
    try:
        if set(gate_ids) != GATE_IDS or len(gate_ids) != len(GATE_IDS) or not currents:
            raise fs.ContractError("目标门禁必须完整覆盖 make test 的八类检查")
        receipt, binding, manifest, _summary, store = fs.authorize(
            request, now=now, request_schema=REQUEST_SCHEMA, approval_scope=CONTRACT)
        subject = target_check(request, binding)
        verdict.update(source_run_id=receipt["run_id"], expires_at_utc=receipt["expires_at_utc"], binding=binding,
                       target=subject, revocation=request["revocation"], platform_compatibility=request.get("platform_compatibility"))
        compatibility = fs.replay_ref(request["platform_compatibility"])
        if (compatibility.get("status") != "approved" or not compatibility.get("account")
                or compatibility.get("scope") != CONTRACT or compatibility.get("binding_sha256") != fs.digest(binding)
                or not fs.utc(compatibility["approved_at_utc"]) <= now < fs.utc(compatibility["expires_at_utc"])):
            raise fs.ContractError("平台兼容性尚未签字、错绑或已过期")
        if (facts.policy_sha256 != manifest["policy_sha256"]
                or facts.environment_sha256 != binding["environment_fingerprint"]
                or facts.executor.get("sha256") != binding["tool_package_digest"]):
            raise fs.ContractError("当前调度策略、环境或执行工具已变化")
        sources = {row["unit_id"]: row for row in manifest["units"]}
        facts = replace(facts, require_read_audit=True, max_age_hours=24, now=now)
        decisions = {}
        for unit_id, current in currents.items():
            row = sources.get(unit_id)
            # 沿用已重放来源的宿主路径登记，但每次重新快照；不缩小当前仓库声明，
            # 也不拿来源摘要冒充现场事实。来源更窄时仍作为差异执行。
            if row is not None and current.inheritable:
                old_inputs = fs.read(row["record_path"])["inputs"]
                names = {entry["name"] for entry in current.inputs or []}
                additions = []
                for entry in old_inputs:
                    name = entry["name"]
                    if name in names:
                        continue
                    if name.startswith("host:"):
                        additions.append(records._audit_module().host_snapshot(name[5:]))
                    elif name == "runtime-contract":
                        additions.append(records._audit_module()._runtime_module().contract_entry(entry["detail"]["contract"]))
                    elif name == "require-read-audit":
                        additions.append(records.value_entry("policy", name, "all-file-paths/v1"))
                if additions:
                    current.inputs = records._unique([*(current.inputs or []), *additions])
                    current.inputs_sha256 = records.entries_sha256(current.inputs)
            if row is None:
                decision = records.Decision(reasons=["目标新增单元：完整执行"])
            elif not current.inheritable:
                raise fs.ContractError(f"当前单元 {unit_id} 输入覆盖未闭合")
            elif row["spec_sha256"] != current.spec_sha256 or row["inputs_sha256"] != current.inputs_sha256:
                decision = records.Decision(reasons=["目标单元规格或输入不同：完整执行"])
            else:
                record = fs.read(row["record_path"])
                reasons = records.check_record(store, Path(row["record_path"]), record, current, facts)
                if reasons:
                    raise fs.ContractError(f"单元 {unit_id} 来源不能重放：{'；'.join(reasons)}")
                decision = records.Decision(record, Path(row["record_path"]), [])
            decisions[unit_id] = decision
            verdict["units"][unit_id] = {
                "disposition": "inherited" if decision.inherit else "executed", "reasons": decision.reasons,
                "spec_sha256": current.spec_sha256, "inputs_sha256": current.inputs_sha256,
                "record_path": str(decision.record_path) if decision.inherit else None,
                "record_sha256": decision.record["record_sha256"] if decision.inherit else None}
        verdict["eligible"] = True
        if not verdict["reuse_enabled"]:
            raise fs.ContractError("真实承接开关关闭，仅记录可承接判定")
        verdict["action"] = "inherit-matching-units"
        return verdict, decisions
    except ERRORS as error:
        verdict["reasons"].append(str(error))
        return verdict, {key: records.Decision(reasons=["B-10 全集重跑：" + str(error)]) for key in currents}


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return fs.reference(path)


def snapshot_request(path, directory):
    """封存本次消费的动态凭证，后续刷新外部查询不破坏历史重放。"""
    request = fs.read(path)
    directory.mkdir(mode=0o700)
    for name in ("consumer_clock", "revocation", "platform_compatibility"):
        request[name] = write_new(directory / (name + ".json"), fs.replay_ref(request[name]))
    request["approvals"] = [write_new(directory / f"approval-{i}.json", fs.replay_ref(ref))
                            for i, ref in enumerate(request["approvals"])]
    return write_new(directory / "request.json", request)


def verify_evidence(evidence, *, subject=None, current_request=None, deployment=None, now=None):
    """历史重放使用封存时间；新消费另给当前请求，再核对现场与即时撤销状态。"""
    records = fs.records_module()
    if evidence.get("schema_version") != EVIDENCE_SCHEMA or evidence.get("contract") != CONTRACT:
        raise fs.ContractError("目标单元证据合同不匹配")
    plan, summary, manifest = (fs.replay_ref(evidence[key]) for key in ("plan", "summary", "manifest"))
    store = records.RecordStore(Path(manifest["record_store"]))
    problems = records.verify_manifest(manifest, store=store)
    rows = manifest["units"]
    ids = [row["unit_id"] for row in rows]
    if (problems or not ids or len(ids) != len(set(ids)) or sorted(ids) != sorted(manifest["planned_units"])
            or store.manifest(manifest["run_id"]) != manifest
            or summary.get("run_id") != manifest["run_id"] or summary.get("status") != "passed"
            or sorted(row["unit_id"] for row in summary["units"]) != sorted(ids)
            or any(row.get("passed") is not True for row in summary["units"]) or summary.get("units_not_run")
            or summary.get("failed_units") or plan.get("profile") != "preflight"
            or {row["gate_id"] for row in plan["gates"]} != GATE_IDS
            or len(plan["gates"]) != len(GATE_IDS)
            or {row["gate_id"] for row in summary["gates"]} != GATE_IDS or len(summary["gates"]) != len(GATE_IDS)):
        raise fs.ContractError("目标运行清单、正式记录或门禁全集不能重放：" + "；".join(problems))
    groups = {group: {row["unit_id"] for row in rows if row.get("test_group") == group}
              for group in manifest.get("test_groups", {})}
    expected_ids = {row["unit_id"] for row in plan["units"]} | {key for values in groups.values() for key in values}
    plan_command_ids = [row["unit_id"] for row in plan["units"]]
    if (expected_ids != set(ids) or len(plan_command_ids) != len(set(plan_command_ids))
            or sorted(row["group_id"] for row in plan.get("test_groups", [])) != sorted(groups)):
        raise fs.ContractError("目标计划和执行／承接集合有遗漏或多项")
    gates = {row["gate_id"]: row for row in summary["gates"]}
    if {key for row in gates.values() for key in row["units"]} != set(ids):
        raise fs.ContractError("八类门禁没有完整覆盖全部单元")
    for gate in plan["gates"]:
        expected = set(gate.get("units", []))
        for group in gate.get("test_groups", []):
            expected.update(groups[group])
        actual = gates[gate["gate_id"]]
        if (set(actual["units"]) != expected or len(actual["units"]) != len(expected)
                or actual["status"] != "passed" or actual.get("not_executed")):
            raise fs.ContractError("目标门禁存在跳过、重复或成员差异")
    counts = {"executed": sum(row["disposition"] == "executed" for row in rows),
              "inherited": sum(row["disposition"] == "inherited" for row in rows)}
    if any(not records.derived_pass(fs.read(row["record_path"]))[0] for row in rows):
        raise fs.ContractError("目标正式记录包含失败单元")
    if evidence.get("counts") != counts or sum(counts.values()) != len(ids):
        raise fs.ContractError("目标执行／承接计数不闭合")
    if not counts["inherited"]:
        if evidence.get("expires_at_utc") is not None:
            raise fs.ContractError("全部重新执行不应宣称来源有效期")
        return counts
    request = fs.replay_ref(evidence["request"]) if current_request is None else fs.read(current_request)
    decision = manifest.get("target_platform_decision") or {}
    if request.get("receipt") != decision.get("source_receipt"):
        raise fs.ContractError("目标门禁改用了另一个全集来源")
    reference_time = fs.utc(evidence["checked_at_utc"]) if now is None else now
    if current_request is not None:
        fs.live_check(request, deployment=deployment, store=store.root, tree=Path(evidence["tree"]))
    currents = {}
    for row in rows:
        record = fs.read(row["record_path"])
        currents[row["unit_id"]] = records.Current(row["unit_id"], record["unit_type"], record["spec"],
            row["spec_sha256"], record["inputs"], row["inputs_sha256"], True)
    facts = records.RunFacts(manifest["policy_sha256"], manifest["environment"], manifest["environment_sha256"],
                             manifest["executor"], 24, reference_time)
    verdict, decisions = assess(request, currents, facts, gate_ids=sorted(GATE_IDS), now=reference_time)
    inherited_ids = {row["unit_id"] for row in rows if row["disposition"] == "inherited"}
    if (verdict["action"] != "inherit-matching-units"
            or evidence["expires_at_utc"] != verdict["expires_at_utc"]
            or any(not decisions[key].inherit for key in inherited_ids)):
        raise fs.ContractError("目标承接已失效或来源集合不同：" + "；".join(verdict["reasons"]))
    for row in rows:
        if row["unit_id"] in inherited_ids and decisions[row["unit_id"]].record["record_sha256"] != row["record_sha256"]:
            raise fs.ContractError("目标单元引用了其它运行的记录")
    if subject is not None:
        for field in SUBJECT_FIELDS:
            if subject[field] != verdict["target"][field]:
                raise fs.ContractError("目标门禁承接身份与候选验收主体不同：" + field)
    return counts


def latest_deployment(data_root):
    candidates = sorted((Path(data_root) / "control").glob("codex-*-supervisor-enable-*.json"),
                        key=lambda p: p.name.split("-supervisor-enable-", 1)[1].lower())
    if not candidates:
        raise fs.ContractError("当前部署收据不存在")
    return candidates[-1]


def check_cached(root, request, data_root):
    root = Path(root)
    meta = fs.read(root / "logs/target-platform.gate.json")
    if meta.get("exit_code") != 0:
        raise fs.ContractError("上次目标门禁未通过")
    proof = meta.get("unit_execution")
    if proof is None:
        if meta.get("command") != ["make", "test"]:
            raise fs.ContractError("目标门禁命令缺少正式单元证据")
        return {"executed": 1, "inherited": 0}
    if meta.get("command") != COMMAND or proof.get("path") != "target-units/evidence.json":
        raise fs.ContractError("目标单元证据路径或命令错误")
    evidence = fs.replay_ref({**proof, "path": str(root / proof["path"])})
    if evidence["counts"]["inherited"] and request is None:
        raise fs.ContractError("承接结果须提供本次即时复核请求")
    return verify_evidence(evidence, current_request=request if evidence["counts"]["inherited"] else None,
                           deployment=latest_deployment(data_root), now=time.time())


def archive(root, attempt):
    """仅归档本次目标门禁和派生事实，不动本机门禁、采集封存包或既有归档。"""
    root = Path(root)
    paths = sorted((root / "logs").glob("target-platform.*"))
    paths += sorted((root / "environment").glob(attempt + "-*"))
    paths += [root / name for name in ("target-units", "candidate-gates.facts.json", "candidate-gates.receipt.json")
              if (root / name).exists()]
    destination = root / "superseded" / ("target-platform-" + time.strftime("%Y%m%dt%H%M%Sz", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, mode=0o700)
    moved = []
    files = []
    for path in paths:
        if path.is_symlink():
            raise fs.ContractError("归档拒绝符号链接")
        target = destination / path.relative_to(root)
        for item in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if item.is_symlink():
                raise fs.ContractError("归档内容拒绝符号链接")
            if item.is_file():
                files.append({"original": str(item), "archived": str(destination / item.relative_to(root)),
                              "sha256": fs.file_digest(item)})
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.rename(target)
        moved.append(str(target))
    write_new(destination / "archive.json", {"archived_at_utc": fs.timestamp(time.time()), "paths": moved, "files": files,
                                             "reason": "目标门禁失败或当前消费复核拒绝，原始证据保留"})
    return str(destination)


def run(args):
    """只使用正式八类计划；不接收调用方删减过的门禁清单。"""
    tree, out = args.tree.resolve(), args.out.resolve()
    out.mkdir(mode=0o700)
    env = {}
    for name, value in os.environ.items():
        if name in ("HOME", "TZ", "TMPDIR", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR") or name.startswith(
                        ("GO", "CGO_", "DOCKER_", "NODE_", "PNPM_", "npm_config_", "COREPACK_")):
            env[name] = value
    env.update(PATH="/usr/local/go/bin:/opt/node-v20/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               LANG="C.UTF-8", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=".", PYTHONPYCACHEPREFIX=str(args.pycache),
               CODEX_0_149_1_SOURCE_ROOT=str(args.historical_source),
               CAPTURE_TYPESCRIPT_MODULE=str(tree / "frontend/node_modules/typescript/lib/typescript.js"))
    here = Path(__file__).parent
    launcher = ["unshare", "-m", "--propagation", "private", "bash", "-c",
                'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec "$@"', "entry-gates"]
    plan_command = [sys.executable, "-B", str(here / "entry_gates.py"), "plan", "--profile", "preflight", "--tree", str(tree),
        "--launcher-json", json.dumps(launcher), "--typescript-module", env["CAPTURE_TYPESCRIPT_MODULE"],
        "--historical-source-root", str(args.historical_source), "--output", str(out / "plan.json")]
    subprocess.run(plan_command, cwd=tree, env=env, check=True, stdin=subprocess.DEVNULL)
    command = [sys.executable, "-B", str(here / "unit_executor.py"), "run-gates", "--manifest", str(out / "plan.json"),
        "--out-dir", str(out / "executor"), "--record-store", str(args.record_store), "--mode", "full-set-pass",
        "--target-platform-request", str(args.request), "--full-set-deployment", str(args.deployment)]
    result = subprocess.run(command, cwd=tree, env=env, stdin=subprocess.DEVNULL)
    if result.returncode:
        return result.returncode
    summary = fs.read(out / "executor/summary.json")
    counts = {key: summary["inheritance"][key] for key in ("executed", "inherited")}
    decision = summary.get("target_platform_decision") or {}
    request_ref = snapshot_request(args.request, out / "authorization") if counts["inherited"] else None
    evidence = {"schema_version": EVIDENCE_SCHEMA, "contract": CONTRACT, "counts": counts, "tree": str(tree),
        "checked_at_utc": fs.timestamp(time.time()), "request": request_ref,
        "expires_at_utc": decision.get("expires_at_utc") if counts["inherited"] else None,
        "plan": fs.reference(out / "plan.json"), "summary": fs.reference(out / "executor/summary.json"),
        "manifest": fs.reference(out / "executor/unit-manifest.json")}
    verify_evidence(evidence, current_request=args.request if counts["inherited"] else None,
                    deployment=args.deployment, now=fs.utc(evidence["checked_at_utc"]))
    write_new(out / "evidence.json", evidence)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("run")
    for key in ("tree", "out", "request", "deployment", "record-store", "pycache", "historical-source"):
        execute.add_argument("--" + key, type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--request", type=Path)
    verify.add_argument("--deployment", type=Path)
    deployed = sub.add_parser("deployment")
    deployed.add_argument("--data-root", type=Path, required=True)
    cached = sub.add_parser("check-cached")
    cached.add_argument("--gate-root", type=Path, required=True)
    cached.add_argument("--request", type=Path)
    cached.add_argument("--data-root", type=Path, required=True)
    archived = sub.add_parser("archive")
    archived.add_argument("--gate-root", type=Path, required=True)
    archived.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run(args)
        if args.command == "deployment":
            print(latest_deployment(args.data_root))
            return 0
        if args.command == "archive":
            print(archive(args.gate_root, args.attempt))
            return 0
        if args.command == "check-cached":
            counts = check_cached(args.gate_root, args.request, args.data_root)
            print(json.dumps({"status": "passed", **counts}, ensure_ascii=False))
            return 0
        counts = verify_evidence(fs.read(args.evidence), current_request=args.request, deployment=args.deployment,
                                 now=time.time() if args.request else None)
        print(json.dumps({"status": "passed", **counts}, ensure_ascii=False))
        return 0
    except ERRORS as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
