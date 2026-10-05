#!/usr/bin/env python3
"""只读解析当前阶段的路径、attempt 和收据；不按目录时间推断有效身份。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

sys.dont_write_bytecode = True
try:
    from . import round_context
    from .parse_env import derive, parse, plain_path
except ImportError:
    import round_context
    from parse_env import derive, parse, plain_path


STAGES = {"capture-candidate": "CAPTURE", "compare": "COMPARE", "accept": "ACCEPT", "assertions": "ASSERTIONS"}


class Context:
    """每次调用重放一组一致的输入；输出前再检查身份与已消费文件没有变化。"""

    def __init__(self, config, *, candidate=True, upgrade=None):
        self.config = config
        self.cu = upgrade if upgrade is not None else round_context._managed_upgrade(Path(config["D"]))
        self.roots = round_context.resolve(config, mode="candidate" if candidate else "campaign", upgrade=self.cu)
        self.parameters = dict(self.roots["parameters"])
        self.campaign = Path(self.parameters["NEWDIR"])
        self.candidate = config["CAND"]
        self.manifest = self.cu._require_formal_campaign(self.campaign)
        self.plan = self.cu._vc_campaign_plan(self.campaign, self.manifest)
        self.bindings = dict(self.roots["bindings"])
        self.absent = set()
        self.baseline = self.cu._current_evaluation_baseline(self.campaign, self.candidate) if candidate else None
        self.stages = {}
        if self.baseline is not None:
            number, commit = self.baseline
            self.parameters["EVALUATION_BASELINE"] = str(number)
            if commit is not None:
                self.bind("evaluation_commit", self.cu._evaluation_baseline_dir(self.campaign, self.candidate, number) / "COMMIT")
            # 配置与门禁工作目录按候选及评估基线分开，避免重开后消费旧 facts 或断言配置。
            suffix = Path(self.candidate) / f"b{number}"
            self.parameters["ASSERTION_CONFIG_DIR"] = str(Path(config["AS"]) / suffix)
            self.parameters["GATE_ROOT"] = str(Path(config["G"]) / suffix)

    def path(self, path):
        path = Path(path)
        plain_path(str(path), "阶段路径")
        return path

    def bind(self, name, path):
        value = round_context.binding(self.path(path))
        self.bindings[name] = value
        return value

    def exists(self, path):
        path = self.path(path)
        if path.exists():
            if not path.is_file():
                raise ValueError(f"阶段输入不是普通文件：{path}")
            return True
        self.absent.add(path)
        return False

    def build(self, *, materialized=True):
        cu, campaign, candidate = self.cu, self.campaign, self.candidate
        path = cu._candidate_build_receipt_path(campaign, candidate)
        bound = self.bind("build", path)
        if materialized:
            receipt, _ = cu._replay_candidate_build_receipt(campaign, self.manifest, candidate, path)
            self.parameters["BUILD_REPLAY_STATE"] = "materialized_build_verified"
        else:
            # 封存后的路径查询不能要求已获准清理的构建树仍存在；这里只重放不变收据绑定，
            # 不能将此状态当成重新采集或构建实物验收。新派发仍必须走 materialized 分支。
            receipt = cu.codex_upgrade_vc_artifacts.validate_candidate_build_receipt(json.loads(path.read_text()))
            identity = self.stages["capture-candidate"]["identity"]
            if (identity.get("build_receipt") != {**bound, "path": str(path.relative_to(campaign))}
                or identity.get("build_receipt_digest") != receipt["receipt_digest"]
                or receipt["campaign_id"] != self.config["NEW"] or receipt["candidate_id"] != candidate
                or receipt["campaign_manifest_sha256"] != self.bindings["campaign"]["sha256"]):
                raise ValueError("已封存采集未绑定当前构建收据")
            self.parameters["BUILD_REPLAY_STATE"] = "sealed_receipt_binding"
        source, profile = receipt["source"], receipt["profile"]
        if (source["root"], source["git_commit"], profile["profile_id"], receipt["target_version"]) != (
            str(Path(self.config["B"]) / "source"), self.config["C"], self.config["PROFILE_ID"], self.config["TARGET_VERSION"]
        ):
            raise ValueError("构建收据的源码、提交、画像或目标版本与本轮不一致")
        self.parameters.update(BUILD_RECEIPT=str(path), IMAGE_ID=receipt["image"]["image_id"],
                               IMAGE_REF=receipt["image"]["reference"], BUILD_ID=receipt["build"]["build_id"],
                               TREE=source["tree_sha256"], SOURCE_ROOT=source["root"],
                               SOURCE_COMMIT=source["git_commit"],
                               DEPLOYED=receipt["target_version"], PROFILE_ID=profile["profile_id"],
                               PROFILE_DIGEST=profile["profile_digest"])

    def stage_paths(self, *, replay=True):
        cu, campaign, candidate = self.cu, self.campaign, self.candidate
        number, commit = self.baseline
        for stage, name in STAGES.items():
            source = cu._stage_read_source(campaign, candidate, number, stage)
            read = self.path(source["path"])
            reused = commit is not None and commit["stage_sources"][stage]["source"] == "reused"
            write = None if reused else self.path(cu._stage_write_target(campaign, candidate, number, stage))
            self.parameters[f"{name}_RESULT"] = str(read)
            if stage == "assertions":
                self.parameters["EVALUATION_RUN"] = str(read.parent / "evaluation-run.json")
            self.parameters[f"{name}_WRITE"] = str(write) if write is not None else ""
            self.parameters[f"{name}_READY"] = "0"
            if not self.exists(read):
                continue
            bound = self.bind(stage, read)
            if bound["sha256"] != source["sha256"]:
                raise ValueError("阶段结果在路径解析期间发生漂移")
            if replay:
                if stage == "assertions":
                    # 此处仅核对索引及其链；完整规则验收仍由 accept 的原生重放负责。
                    origin = int(source["baseline_of_record"])
                    index = cu._load_evaluation_run_index(campaign, candidate, origin)
                    if index is None:
                        raise ValueError("已有断言结果缺少可重放的 evaluation-run 索引")
                    self.bind("evaluation_run", read.parent / "evaluation-run.json")
                    result = json.loads(read.read_text())
                    if (result.get("schema_version") != cu.RESULTS_SCHEMA_V2 or result.get("candidate_id") != candidate
                        or result.get("target_version") != self.config["TARGET_VERSION"]
                        or result.get("comparison_package_digest") != self.stages.get("compare", {}).get("package_digest")):
                        raise ValueError("断言结果未绑定当前候选、版本和比较收据")
                    accepted = self.stages.get("accept")
                    if accepted is not None and accepted.get("assertion_result") != {"path": str(read.relative_to(campaign)), "sha256": bound["sha256"]}:
                        raise ValueError("验收收据中的断言路径或摘要与当前读来源不一致")
                else:
                    self.stages[stage] = cu._load_stage_result(
                        campaign, stage, candidate, _verified_campaign_manifest=self.manifest)
                    if self.stages[stage].get("status") != "complete":
                        raise ValueError(f"{stage} 结果尚未成功完成，禁止跳过")
                self.parameters[f"{name}_READY"] = "1"

    def attempt(self, expected=None):
        cu, campaign, candidate = self.cu, self.campaign, self.candidate
        stage = self.stages.get("capture-candidate")
        # 当前阶段收据已有绑定时优先取它，并检查不存在另一个有效待封存项。
        # 重开且 capture 目标未生成时，按前序基线检查旧封存项，避免将旧 attempt 当成新项。
        kwargs = {"_manifest": self.manifest}
        if stage is None and self.baseline[0] > 0:
            kwargs["_baseline"] = self.baseline[0] - 1
        active = [value.split(":") for value in cu._active_unsealed_attempts(campaign, "candidate", **kwargs)
                  if value.split(":", 1)[0] == candidate]
        if any(len(value) != 2 for value in active) or len(active) > 1:
            raise ValueError("当前候选有未完成预约或多个待封存 attempt，禁止猜选")
        reference = stage.get("attempt") if stage is not None else None
        sealed_id = Path(reference["path"]).parent.name if isinstance(reference, dict) else None
        selected = active[0][1] if active else sealed_id
        if not selected or (active and sealed_id and selected != sealed_id):
            raise ValueError("当前阶段与待封存 attempt 不唯一或尚无有效 attempt")
        if expected is not None and expected != selected:
            raise ValueError("显式 ATT 与当前有效 attempt 不一致")
        root = self.path(cu._capture_attempt_path(campaign, "candidate", candidate, selected))
        bound = self.bind("attempt", root / "attempt.json")
        if reference is not None and reference != {"path": str((root / "attempt.json").relative_to(campaign)), "sha256": bound["sha256"]}:
            raise ValueError("当前采集结果的 attempt 路径或摘要不一致")
        actual_root, payload = cu._load_capture_attempt(campaign, "candidate", candidate, selected,
                                                       _verified_campaign_manifest=self.manifest)
        if actual_root != root or payload["status"] != "awaiting_receipts":
            raise ValueError("当前 attempt 未通过重放或不处于 awaiting_receipts")
        identity = payload["identity"]
        for key, field in (("IMAGE_ID", "image_id"), ("IMAGE_REF", "image_reference"), ("BUILD_ID", "build_id"), ("TREE", "source_tree_sha256"),
                           ("SOURCE_COMMIT", "git_commit"),
                           ("SOURCE_ROOT", "source_root"), ("DEPLOYED", "deployed_version"),
                           ("PROFILE_ID", "profile_id"), ("PROFILE_DIGEST", "profile_digest")):
            if identity.get(field) != self.parameters[key]:
                raise ValueError(f"attempt 的 {field} 未绑定当前构建收据")
        self.parameters.update(ATT=selected, A=str(root), EV=str(root / "evidence"), CID=self.config["NEW"],
                               ATT_STATUS=payload["status"], RUN_NONCE=payload["run_nonce"], STARTED=payload["started_at_utc"])

    def completion(self, *, required=None):
        for phase in ("VC-5", "VC-6"):
            name = phase.replace("-", "")
            receipt = self.cu._vc_completion_receipt_path(self.campaign, self.candidate, name.lower() + "_completion")
            checkpoint = self.cu._vc_checkpoint_path(self.campaign, phase, revision=self.roots["candidate_revision"])
            self.parameters.update({f"{name}_RECEIPT": str(self.path(receipt)), f"{name}_CHECKPOINT": str(self.path(checkpoint)),
                                    f"{name}_COMPLETE": "0"})
            has_receipt, has_checkpoint = self.exists(receipt), self.exists(checkpoint)
            if has_receipt or has_checkpoint or required == phase:
                self.cu._replay_vc_completion(self.campaign, self.manifest, phase=phase,
                                             candidate_id=self.candidate, attempt_id=self.parameters["ATT"])
                self.bind(name + "_receipt", receipt)
                self.bind(name + "_checkpoint", checkpoint)
                self.parameters[f"{name}_COMPLETE"] = "1"

    def canonical(self):
        checkpoint = self.cu._canonical_latest_checkpoint(self.campaign)
        identity = checkpoint["campaign"]
        for key, expected in (("campaign_id", self.config["NEW"]), ("candidate_id", self.candidate),
                              ("attempt_id", self.parameters["ATT"]), ("target_version", self.config["TARGET_VERSION"]),
                              ("campaign_manifest_sha256", self.bindings["campaign"]["sha256"])):
            if identity.get(key) != expected:
                raise ValueError(f"canonical 的 {key} 与当前阶段不一致")
        self.bind("canonical", self.cu._canonical_checkpoint_file(self.campaign, checkpoint))
        return checkpoint

    def finish(self):
        for value in self.bindings.values():
            if round_context.binding(Path(value["path"])) != value:
                raise ValueError("阶段解析期间已绑定文件发生变化")
        if any(path.exists() or path.is_symlink() for path in self.absent):
            raise ValueError("阶段解析期间出现新产物，须重新解析")
        current = round_context.resolve(self.config, mode="candidate" if self.baseline is not None else "campaign", upgrade=self.cu)
        if current != self.roots or (self.baseline is not None and self.cu._current_evaluation_baseline(self.campaign, self.candidate) != self.baseline):
            raise ValueError("阶段解析期间当前身份或评估基线发生变化")
        return {"schema_version": "arm64-phase-context/v1", "parameters": self.parameters, "bindings": self.bindings}


def _vc6_receipts(context, plan):
    """只读核对待派发的 canonical 收据；不调用会推进 checkpoint 的生产入口。"""

    cu, campaign = context.cu, context.campaign
    artifacts = cu.codex_upgrade_vc_artifacts
    group = artifacts.canonical_batch_binding(plan["actions"], execute_item_ids=plan["execute_item_ids"], phase="VC-6")
    if group is None or (group["campaign_dir"], group["candidate_id"], group["attempt_id"]) != (
        str(campaign), context.candidate, context.parameters["ATT"]
    ):
        raise ValueError("VC-6 计划未绑定当前 Campaign、Candidate 和 attempt")
    checkpoint = context.canonical()
    index = cu._canonical_item_index(checkpoint)
    for action in plan["actions"]:
        item = artifacts.canonical_action_binding(action)
        path = context.path(item["step_receipt"])
        bound = context.bind(action["action_id"], path)
        step = item["canonical_step"]
        if step in {"production-activation", "rollback-verification"}:
            cu._canonical_activation_receipt(campaign, checkpoint, path)
            if step == "rollback-verification" and "production-activation" not in index:
                raise ValueError("VC-6 回滚收据前缺少生产激活绑定")
        else:
            receipt = json.loads(path.read_text())
            version = item["retire_version"]
            scan = receipt.get("consumer_scan")
            if (version != context.config["RETIRE_VERSION"] or not {"production-activation", "rollback-verification"}.issubset(index)
                or (item["item_id"] not in index and item["item_id"] not in checkpoint["plan"]["execute_item_ids"])
                or receipt.get("schema_version") != cu.CANONICAL_REMOVAL_RECEIPT_SCHEMA or receipt.get("status") != "complete"
                or receipt.get("campaign_id") != context.config["NEW"] or receipt.get("removed_version") != version
                or receipt.get("active_version") != context.config["TARGET_VERSION"]
                or receipt.get("rollback_version") != context.config["BASELINE_VERSION"]
                or receipt.get("production_activation_sha256") != index["production-activation"]["source"]["sha256"]
                or not isinstance(scan, dict) or set(scan) != {"catalog_references", "selector_references", "unknown_references"}
                or any(scan.values()) or receipt.get("runtime_catalog_removed") is not True
                or receipt.get("historical_evidence_preserved") is not True):
                raise ValueError("VC-6 退役收据未绑定当前激活、版本或零消费者合同")
        if item["item_id"] in index and index[item["item_id"]]["source"] != {"path": str(path.relative_to(campaign)), "sha256": bound["sha256"]}:
            raise ValueError("VC-6 已完成步骤与传入收据不一致")
        # 同一纯 canonical 批次允许连续三步；这里只构造只读校验视图，不修改 checkpoint。
        index[item["item_id"]] = {"source": {"path": str(path), "sha256": bound["sha256"]}}


def _vc5_plan(context, plan):
    """提前拒绝旧阶段参数；具体动作授权仍由正式编译器和监督器重放。"""

    params = context.parameters
    option = context.cu.codex_upgrade_vc_artifacts._command_option
    expected = {"--campaign-dir": str(context.campaign), "--candidate-id": context.candidate,
                "--build-receipt": params["BUILD_RECEIPT"], "--candidate-source": params["SOURCE_ROOT"],
                "--candidate-image-id": params["IMAGE_ID"], "--build-id": params["BUILD_ID"],
                "--profile-id": params["PROFILE_ID"], "--profile-digest": params["PROFILE_DIGEST"],
                "--deployed-version": params["DEPLOYED"], "--evaluation-baseline": params["EVALUATION_BASELINE"],
                "--assertions": params["ASSERTIONS_RESULT"], "--external-gate-root": params["GATE_ROOT"]}
    write_stages = {"candidate-run": "CAPTURE", "candidate-seal": "CAPTURE", "compare": "COMPARE",
                    "assert-rules": "ASSERTIONS", "acceptance": "ACCEPT"}
    for item in plan["execute_item_ids"]:
        if item in write_stages and not params[write_stages[item] + "_WRITE"]:
            raise ValueError("批次试图写入 reused 阶段")
    attempts = set()
    for action in plan["actions"]:
        command = action["command"]
        for flag, value in expected.items():
            provided = option(command, flag)
            if provided is not None and provided != value:
                raise ValueError(f"VC-5 计划的 {flag} 与当前阶段不一致")
        explicit = option(command, "--attempt-id")
        if explicit is not None:
            attempts.add(explicit)
        for key, value in (("CAMPAIGN_DIR", str(context.campaign)), ("CANDIDATE_ID", context.candidate),
                           ("CANDIDATE_SOURCE_ROOT", params["SOURCE_ROOT"]), ("REPO_ROOT", context.config["D"]),
                           ("TOOL_ROOT", context.config["TOOLS"])):
            provided = [word.partition("=")[2] for word in command if word.startswith(key + "=")]
            if provided and provided != [value]:
                raise ValueError(f"VC-5 环境参数 {key} 与当前阶段不一致")
        attempts.update(word.partition("=")[2] for word in command if word.startswith("ATTEMPT_ID="))
        if action["operation"] == "VC-5:assert":
            for flag, value in (("--config", str(Path(params["ASSERTION_CONFIG_DIR"]) / "config.json")),
                                ("--output", str(Path(params["ASSERTIONS_WRITE"]) / "results.json")),
                                ("--results-dir", str(Path(params["ASSERTIONS_WRITE"]) / "machine"))):
                if option(command, flag) != value:
                    raise ValueError(f"断言计划的 {flag} 不是当前基线写目标")
    if len(attempts) > 1:
        raise ValueError("VC-5 计划包含多个 attempt")
    if attempts:
        context.attempt(next(iter(attempts)))


def resolve(config, *, mode="paths", attempt_id=None, phase=None, predecessor=None,
            campaign_id=None, inputs=None, plan_name=None, require_completion=None, client_checkpoint_at_utc=None, upgrade=None):
    """进程内入口；注入模块仅用于共用受管模块或隔离测试，不提供 CLI 跳过验证选项。"""

    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        if mode not in {"build", "paths", "attempt", "batch"}:
            raise ValueError("未知阶段解析模式")
        candidate = mode != "batch" or phase in {"VC-5", "VC-6"}
        context = Context(config, candidate=candidate, upgrade=upgrade)
        if mode == "batch":
            phases = ("VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6")
            if phase not in phases[1:] or predecessor != phases[phases.index(phase) - 1]:
                raise ValueError("批次前序必须是当前阶段的直接前序")
            if (campaign_id, inputs) != (config["NEW"], config["IN"]) or not plan_name or Path(plan_name).name != plan_name:
                raise ValueError("批次 Campaign、输入目录或计划名与本轮不一致")
            revision = context.roots["candidate_revision"] if predecessor in {"VC-4", "VC-5"} else None
            path, _ = context.cu._replay_vc_checkpoint(context.campaign, context.plan, predecessor, revision=revision)
            context.bind("predecessor", path)
            context.parameters["PRED_CKPT"] = str(path)
            plan_path = Path(config["W"]) / plan_name
            context.bind("action_plan", plan_path)
            plan = context.cu.codex_upgrade_vc_artifacts.validate_action_plan(json.loads(plan_path.read_text()))
            if phase not in {"VC-5", "VC-6"}:
                return context.finish()
        context.stage_paths(replay=mode != "build")
        context.build(materialized=mode == "build" or "capture-candidate" not in context.stages or
                      (mode == "batch" and "candidate-run" in plan["execute_item_ids"]))
        if mode == "batch" and phase == "VC-5":
            _vc5_plan(context, plan)
            return context.finish()
        if mode in {"attempt", "batch"} or require_completion is not None:
            context.attempt(attempt_id)
            if client_checkpoint_at_utc is not None:
                path = Path(context.parameters["EV"]) / "environment/client-after/probe-manifest.json"
                context.bind("client_checkpoint", path)
                if json.loads(path.read_text()).get("observed_at_utc") != client_checkpoint_at_utc:
                    raise ValueError("客户端 checkpoint 时间未绑定当前 attempt 的探针收据")
            context.completion(required=require_completion or ("VC-5" if mode == "batch" else None))
        if mode == "batch":
            _vc6_receipts(context, plan)
        return context.finish()
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--mode", choices=("build", "paths", "attempt", "batch"), default="paths")
    for flag in ("attempt-id", "phase", "predecessor", "campaign-id", "inputs", "plan-name", "require-completion", "client-checkpoint-at-utc"):
        parser.add_argument("--" + flag)
    parser.add_argument("--shell", action="store_true")
    args = parser.parse_args(argv)
    try:
        round_context.binding(args.env)
        values = parse(args.env.read_text())
        report = resolve({**values, **derive(values)}, **{key: value for key, value in vars(args).items() if key not in {"env", "shell"}})
    except Exception as error:
        print(f"阶段解析拒绝：{error}", file=sys.stderr)
        return 3
    if args.shell:
        for key, value in sorted(report["parameters"].items()):
            if key in {"G", "AS"}:
                continue
            print(f"export {key}={shlex.quote(str(value))}")
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
