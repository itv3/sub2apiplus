#!/usr/bin/env python3
"""生成 VC-5 各批次动作计划（阶段项闭集形态：execute=candidate-seal/compare/assert-rules/acceptance，reuse=全部候选 Job）。
用法：gen_vc5_plans.py <输出目录> <campaign_id> <candidate_id> <image_id sha256:…> <build_id> [attempt_id] [seal_review_sha]
环境变量：D（数据根）、CANDIDATE_DIR（候选实物目录）、PROFILE_ID／PROFILE_DIGEST（目标画像）。
未提供 attempt_id 时只生成 run 计划；提供后生成 seal checkpoint、assertion bundle、seal 预览、批准+compare、assert、accept。"""
import json, os, pathlib, sys
sys.dont_write_bytecode = True
from driver_config import load_config, candidate_job_ids
CONFIG = load_config()
from vc5_admission import Admission, ManagedFacts
try:
    ADMISSION = Admission(CONFIG, ManagedFacts(CONFIG)).consume()
except Exception:
    raise SystemExit("VC-5 准入收据复核失败，禁止生成计划")
CONFIG.update(ADMISSION["effective_parameters"])
os.environ.update(ADMISSION["effective_parameters"])

out = pathlib.Path(sys.argv[1]); cid = sys.argv[2]; cand = sys.argv[3]; image_id = sys.argv[4]; build_id = sys.argv[5]
if (cid, cand, image_id, build_id) != tuple(ADMISSION["bindings"][key] for key in ("campaign", "candidate", "image_id", "build_id")):
    raise SystemExit("VC-5 计划参数未绑定当前准入收据")
attempt = sys.argv[6] if len(sys.argv) > 6 else None
seal_sha = sys.argv[7] if len(sys.argv) > 7 else None
from phase_context import resolve
CONTEXT = resolve(CONFIG, mode="attempt" if attempt else "build", attempt_id=attempt)
PARAMS = CONTEXT["parameters"]
if (image_id, build_id) != (PARAMS["IMAGE_ID"], PARAMS["BUILD_ID"]):
    raise SystemExit("VC-5 计划镜像或构建与当前收据不一致")
B = CONFIG["B"]
if os.environ.get("CANDIDATE_DIR", B) != B:
    raise SystemExit("CANDIDATE_DIR 与本轮候选实物目录不一致")
if out != pathlib.Path(CONFIG["W"]):
    raise SystemExit("VC-5 计划输出目录必须等于本轮 W")
D = CONFIG["D"]; NEW = CONFIG["NEWDIR"]; TOOLS = CONFIG["TOOLS"]
BUILD_RECEIPT = PARAMS["BUILD_RECEIPT"]
PROFILE_ID = PARAMS["PROFILE_ID"]
PROFILE_DIGEST = PARAMS["PROFILE_DIGEST"]
JOBS = candidate_job_ids(pathlib.Path(NEW), cand) if attempt else []


def action(item, operation, command, stage_item, timeout=1800):
    return {"action_id": item, "operation": operation, "timeout_seconds": timeout, "command": command, "item_ids": [stage_item]}


def plan(stage_item, actions, reuse):
    return {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": [stage_item], "reuse_item_ids": list(reuse), "actions": actions}


py = ["/usr/bin/python3", f"{TOOLS}/codex_upgrade.py"]
ref = ["--campaign-dir", NEW, "--candidate-id", cand]
out.mkdir(parents=True, exist_ok=True)


def write(name, payload):
    # reused 阶段只读；不为其生成写动作，也不沿用目录里上一基线的计划。
    stages = {"candidate-seal": "CAPTURE", "candidate-run": "CAPTURE", "compare": "COMPARE", "assert-rules": "ASSERTIONS", "acceptance": "ACCEPT"}
    if any(not PARAMS[stages[item] + "_WRITE"] for item in payload["execute_item_ids"]):
        return
    (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


run_cmd = py + ["capture-candidate", "run"] + ref + [
    "--build-receipt", BUILD_RECEIPT, "--runtime-image", f"{CONFIG['CANDIDATE_IMAGE_REPOSITORY']}@{image_id}", "--candidate-image-id", image_id,
    "--candidate-source", f"{B}/source", "--build-id", build_id, "--deployed-version", CONFIG["TARGET_VERSION"],
    "--profile-id", PROFILE_ID, "--profile-digest", PROFILE_DIGEST, "--candidate-purpose", "production_replacement",
    "--max-wall-seconds", "21600", "--acknowledge-live-requests",
]
if not attempt:
    write("action-plan-vc5-run.json", {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["candidate-run"], "reuse_item_ids": [],
      "actions": [{"action_id": "candidate-run", "operation": "VC-5:capture-candidate-run", "timeout_seconds": 21600, "command": run_cmd, "item_ids": ["candidate-run"]}]})
if attempt:
    seal_base = py + ["capture-candidate", "seal"] + ref + ["--build-receipt", BUILD_RECEIPT, "--candidate-purpose", "production_replacement", "--attempt-id", attempt]
    write("action-plan-vc5-seal-checkpoint.json", plan("candidate-seal", [action("candidate-seal-checkpoint", "VC-5:capture-candidate-seal-checkpoint", seal_base, "candidate-seal", 900)], JOBS))
    A = PARAMS["EV"]
    assertion = ["/usr/bin/env", f"CAMPAIGN_DIR={NEW}", f"ATTEMPT_ID={attempt}", "SIDE=candidate", f"CANDIDATE_ID={cand}",
                 f"CANDIDATE_SOURCE_ROOT={B}/source", f"REPO_ROOT={D}", f"TOOL_ROOT={TOOLS}", "/usr/bin/bash", f"{D}/tools/prepare_assertion_bundle.sh"]
    preview = seal_base + [
        "--capture-manifest", f"{A}/assertion-bundle/capture-manifest.json", "--assertion-evidence-root", f"{A}/assertion-bundle",
        "--observed-profile-receipt", f"{A}/client/receipts/observed-profile-receipt.json",
        "--client-evidence", f"kilo-compatible={A}/client/receipts/kilo-compatible-receipt.json",
        "--client-evidence", f"kilo-responses={A}/client/receipts/kilo-responses-receipt.json",
    ]
    # 一个批次的 actions 必须无重叠地精确覆盖 execute_item_ids（candidate-seal 只有一项），
    # 因此 assertion bundle 与 seal 预览各自独立成批，各自预演后派发。
    write("action-plan-vc5-seal-assertion.json", plan("candidate-seal", [
        action("candidate-seal-a-assertion-bundle", "VC-5:prepare-candidate-assertion-bundle", assertion, "candidate-seal", 1800)], JOBS))
    write("action-plan-vc5-seal-preview.json", plan("candidate-seal", [
        action("candidate-seal-b-preview", "VC-5:capture-candidate-seal-preview", preview, "candidate-seal", 3600)], JOBS))
    if seal_sha:
        write("action-plan-vc5-seal-approve.json", plan("candidate-seal", [action("candidate-seal-approve", "VC-5:capture-candidate-seal-approve", seal_base + ["--approve-seal-sha256", seal_sha], "candidate-seal", 1800)], JOBS))
        # 批准与 compare 合并为一个零请求批次（两者都在 post-run-tooling 阶段项闭集内），先整体预演再派发
        approve_compare = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["candidate-seal", "compare"], "reuse_item_ids": list(JOBS), "actions": [
            action("candidate-seal-c-approve", "VC-5:capture-candidate-seal-approve", seal_base + ["--approve-seal-sha256", seal_sha], "candidate-seal", 1800),
            action("candidate-seal-d-compare", "VC-5:compare", py + ["compare"] + ref, "compare", 1800)]}
        write("action-plan-vc5-seal-approve-compare.json", approve_compare)
    # 断言冻结当前基线；读来源与可写目标分离，reused 阶段不生成写计划。
    AS = PARAMS["ASSERTION_CONFIG_DIR"]
    assert_root = PARAMS["ASSERTIONS_WRITE"]
    assert_cmd = ["/usr/bin/python3", f"{TOOLS}/build_rule_assertion_results.py", "--config", f"{AS}/config.json",
                  "--output", f"{assert_root}/results.json", "--results-dir", f"{assert_root}/machine",
                  "--evaluation-baseline", PARAMS["EVALUATION_BASELINE"], "--reuse-authority", "none"]
    write("action-plan-vc5-assert.json", plan("assert-rules", [action("candidate-assert", "VC-5:assert", assert_cmd, "assert-rules", 1800)], JOBS))
    write("action-plan-vc5-compare.json", plan("compare", [action("candidate-compare", "VC-5:compare", py + ["compare"] + ref, "compare", 1800)], JOBS))
    write("action-plan-vc5-accept.json", plan("acceptance", [action("candidate-accept", "VC-5:accept", py + ["accept"] + ref + [
        "--assertions", PARAMS["ASSERTIONS_RESULT"], "--external-gate-root", PARAMS["GATE_ROOT"],
        "--external-gate-receipt", "candidate-gates.receipt.json"], "acceptance", 1800)], JOBS))
print("plans ->", out, sorted(p.name for p in out.iterdir() if p.name.startswith("action-plan-vc5")))
