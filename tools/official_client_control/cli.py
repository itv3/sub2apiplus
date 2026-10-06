"""FW-D 受管控制面的类型化命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import canonical_json_bytes, load_json_file
from .contracts import CAMPAIGN_SCHEMA, CAMPAIGN_V2_SCHEMA
from .errors import ControlError
from .gates import WorkflowGates
from .receipts import (
    effective_tool_identity,
    finalize_activation,
    finalize_candidate_build,
    finalize_candidate_delivery,
    finalize_promotion,
    finalize_validation,
    finalize_validation_gate,
    replay_receipt,
    tool_digest_v2,
)
from .store import ControlStore

# campaign-create --schema 的取值 → 输入 Campaign 必须声明的合同名。
CAMPAIGN_SCHEMA_CHOICES = {"v1": CAMPAIGN_SCHEMA, "v2": CAMPAIGN_V2_SCHEMA}


FACT_COMMANDS = {
    "discovery-record": "discovery_recorded",
    "evidence-record": "evidence_recorded",
    "rule-classification-record": "rule_classification_recorded",
    "evidence-approve": "evidence_approved",
    "profile-approve": "profile_approved",
    "candidate-freeze": "candidate_frozen",
    "scenario-prepare": "scenario_prepared",
    "scenario-capture": "scenario_captured",
    "scenario-seal": "scenario_sealed",
    "scenario-approve": "scenario_approved",
    "pair-record": "pair_recorded",
    "acceptance-record": "acceptance_recorded",
    "validation-attempt-create": "validation_attempt_created",
    "selector-observe": "selector_observed",
    "selector-activate": "selector_activated",
    "promotion-record": "release_promoted",
    "inventory-current-append": "inventory_current_appended",
    # B1-12 最小形态：只允许 Campaign v2；revision-open／invalidate-candidate 编排入口由 B3-05 接入。
    "candidate-revision-record": "candidate_revision_recorded",
}


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("路径必须是绝对路径")
    return path


def _add_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, type=_absolute_path, help="受管 Store 绝对路径")


def _add_fact_command(parser: argparse.ArgumentParser) -> None:
    _add_store(parser)
    parser.add_argument("--campaign", required=True, help="不可变 Campaign ID")
    parser.add_argument("--input", required=True, type=_absolute_path, help="事实 payload JSON")
    parser.add_argument("--issued-at", required=True, help="事实签发 RFC3339 时间")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="official-client-control",
        description="官方 OAuth 客户端仿真通用受管控制面",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init-store", help="初始化空的只写追加 Store")
    initialize.add_argument("--root", required=True, type=_absolute_path)
    initialize.add_argument("--created-at", required=True)

    seal = subparsers.add_parser("artifact-seal", help="封存受管内容寻址对象")
    _add_store(seal)
    seal.add_argument("--kind", required=True)
    seal.add_argument("--input", required=True, type=_absolute_path)

    campaign = subparsers.add_parser("campaign-create", help="创建不可变 Campaign")
    _add_store(campaign)
    campaign.add_argument("--input", required=True, type=_absolute_path)
    campaign.add_argument(
        "--schema",
        choices=tuple(CAMPAIGN_SCHEMA_CHOICES),
        default="v1",
        help="输入 Campaign 的合同版本；v2 建立时核对冻结工具身份与当前受管代码",
    )

    evolution = subparsers.add_parser(
        "tool-evolution", help="登记 Campaign v2 的工具演进，使当前受管代码成为有效工具身份"
    )
    _add_store(evolution)
    evolution.add_argument("--campaign", required=True, help="Campaign v2 ID")
    evolution.add_argument(
        "--input", required=True, type=_absolute_path, help="tool_evolution_registration payload JSON"
    )

    for command in FACT_COMMANDS:
        fact_parser = subparsers.add_parser(command, help=f"追加 {FACT_COMMANDS[command]} 事实")
        _add_fact_command(fact_parser)

    deployment = subparsers.add_parser("deployment-record", help="追加受管部署五阶段事实")
    _add_fact_command(deployment)
    deployment.add_argument("--stage", required=True, choices=(
        "accepted_not_activated",
        "canary_passed",
        "active",
        "rollback_verified",
        "restored_active",
    ))

    promotion = subparsers.add_parser("promotion-finalize", help="生成不可覆盖晋升收据")
    _add_store(promotion)
    promotion.add_argument("--campaign", required=True)
    promotion.add_argument("--promotion-fact-ref", required=True, type=_absolute_path)

    activation = subparsers.add_parser("activation-finalize", help="生成不可覆盖激活收据")
    _add_store(activation)
    activation.add_argument("--campaign", required=True)
    activation.add_argument("--restored-active-ref", required=True, type=_absolute_path)
    activation.add_argument("--selector-before-ref", required=True, type=_absolute_path)
    activation.add_argument("--selector-after-ref", required=True, type=_absolute_path)
    activation.add_argument("--inventory-current-ref", required=True, type=_absolute_path)

    candidate_build = subparsers.add_parser(
        "candidate-build-finalize", help="生成不可覆盖 CandidateBuildReceipt"
    )
    _add_store(candidate_build)
    candidate_build.add_argument("--campaign", required=True)
    candidate_build.add_argument("--input", required=True, type=_absolute_path)

    validation_gate = subparsers.add_parser(
        "validation-gate-finalize", help="生成一次执行或复用的外部门禁收据"
    )
    _add_store(validation_gate)
    validation_gate.add_argument("--campaign", required=True)
    validation_gate.add_argument("--attempt-ref", required=True, type=_absolute_path)
    validation_gate.add_argument("--input", required=True, type=_absolute_path)

    validation = subparsers.add_parser(
        "validation-finalize", help="复算严格验收链并追加 VC-5 完成事实"
    )
    _add_store(validation)
    validation.add_argument("--campaign", required=True)
    validation.add_argument("--acceptance-ref", required=True, type=_absolute_path)
    validation.add_argument("--selector-after-ref", required=True, type=_absolute_path)
    validation.add_argument("--issued-at", required=True)

    candidate_delivery = subparsers.add_parser(
        "candidate-delivery-record", help="追加候选交付四阶段事实"
    )
    _add_fact_command(candidate_delivery)
    candidate_delivery.add_argument(
        "--stage",
        required=True,
        choices=(
            "candidate_active",
            "rollback_verified",
            "candidate_restored",
            "stable_observed",
        ),
    )

    candidate_delivery_finalize = subparsers.add_parser(
        "candidate-delivery-finalize", help="生成 ready_for_operator_release 候选交付收据"
    )
    _add_store(candidate_delivery_finalize)
    candidate_delivery_finalize.add_argument("--campaign", required=True)
    candidate_delivery_finalize.add_argument(
        "--package-ref", required=True, type=_absolute_path
    )

    replay = subparsers.add_parser("replay", help="独立复算 Store、事实链和收据")
    _add_store(replay)
    replay.add_argument("--external-root", type=_absolute_path)
    replay.add_argument("--require-external", action="store_true")

    receipt_replay = subparsers.add_parser("receipt-replay", help="独立重建并核对一份收据")
    _add_store(receipt_replay)
    receipt_replay.add_argument("--receipt-ref", required=True, type=_absolute_path)

    status = subparsers.add_parser("status", help="由正交事实推导 Campaign 检查点")
    _add_store(status)
    status.add_argument("--campaign", required=True)

    subparsers.add_parser(
        "tool-digest", help="输出当前 Store 自身摘要与七项工具身份（tool-identity/v2）"
    )
    return parser


def _load_object(path: Path, label: str) -> dict[str, Any]:
    value = load_json_file(path, label)
    if not isinstance(value, dict):
        raise ControlError(f"{label} 顶层必须是对象")
    return value


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    command = arguments.command
    if command == "init-store":
        store = ControlStore.initialize(arguments.root, arguments.created_at)
        return {"result": "initialized", "store": str(store.root)}
    if command == "tool-digest":
        return tool_digest_v2()

    store = ControlStore(arguments.store)
    if command == "artifact-seal":
        return store.seal_object(arguments.kind, _load_object(arguments.input, "artifact payload"))
    if command == "campaign-create":
        campaign = _load_object(arguments.input, "campaign")
        expected = CAMPAIGN_SCHEMA_CHOICES[arguments.schema]
        if campaign.get("schema_version") != expected:
            raise ControlError(
                f"campaign-create --schema {arguments.schema} 要求输入声明 {expected}"
            )
        return store.create_campaign(campaign)
    if command == "tool-evolution":
        payload = _load_object(arguments.input, "tool evolution registration")
        if payload.get("campaign_id") != arguments.campaign:
            raise ControlError("tool evolution registration.campaign_id 与 --campaign 不一致")
        reference = store.seal_object("tool_evolution_registration", payload)
        return {
            "registration_ref": reference,
            "effective_tool_identity": effective_tool_identity(
                store, store.load_campaign(arguments.campaign)
            ),
        }
    if command in FACT_COMMANDS:
        return store.append_fact(
            arguments.campaign,
            FACT_COMMANDS[command],
            _load_object(arguments.input, f"{FACT_COMMANDS[command]} payload"),
            arguments.issued_at,
        )
    if command == "deployment-record":
        return store.append_fact(
            arguments.campaign,
            arguments.stage,
            _load_object(arguments.input, f"{arguments.stage} payload"),
            arguments.issued_at,
        )
    if command == "promotion-finalize":
        return finalize_promotion(
            store,
            arguments.campaign,
            _load_object(arguments.promotion_fact_ref, "promotion fact ref"),
        )
    if command == "activation-finalize":
        return finalize_activation(
            store,
            arguments.campaign,
            _load_object(arguments.restored_active_ref, "restored active ref"),
            _load_object(arguments.selector_before_ref, "selector before ref"),
            _load_object(arguments.selector_after_ref, "selector after ref"),
            _load_object(arguments.inventory_current_ref, "inventory current ref"),
        )
    if command == "candidate-build-finalize":
        return finalize_candidate_build(
            store,
            arguments.campaign,
            _load_object(arguments.input, "candidate build input"),
        )
    if command == "validation-gate-finalize":
        return finalize_validation_gate(
            store,
            arguments.campaign,
            _load_object(arguments.attempt_ref, "validation attempt ref"),
            _load_object(arguments.input, "validation gate result"),
        )
    if command == "validation-finalize":
        return finalize_validation(
            store,
            arguments.campaign,
            _load_object(arguments.acceptance_ref, "acceptance ref"),
            _load_object(arguments.selector_after_ref, "selector after ref"),
            arguments.issued_at,
        )
    if command == "candidate-delivery-record":
        payload = _load_object(arguments.input, "candidate delivery payload")
        if payload.get("stage") != arguments.stage:
            raise ControlError("candidate delivery payload.stage 与 --stage 不一致")
        return store.append_fact(
            arguments.campaign,
            "candidate_delivery_recorded",
            payload,
            arguments.issued_at,
        )
    if command == "candidate-delivery-finalize":
        return finalize_candidate_delivery(
            store,
            arguments.campaign,
            _load_object(arguments.package_ref, "candidate delivery package ref"),
        )
    if command == "replay":
        return store.replay(
            external_root=arguments.external_root,
            require_external=arguments.require_external,
        )
    if command == "receipt-replay":
        return replay_receipt(store, _load_object(arguments.receipt_ref, "receipt ref"))
    if command == "status":
        return WorkflowGates(store).status(arguments.campaign)
    raise ControlError(f"未处理命令：{command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        result = execute(parser.parse_args(argv))
    except ControlError as error:
        print(f"FW-D 控制面拒绝：{error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
