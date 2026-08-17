"""FW-D 受管控制面的类型化命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import canonical_json_bytes, load_json_file
from .errors import ControlError
from .gates import WorkflowGates
from .receipts import (
    control_tool_bundle_sha256,
    finalize_activation,
    finalize_promotion,
    replay_receipt,
)
from .store import ControlStore


FACT_COMMANDS = {
    "discovery-record": "discovery_recorded",
    "evidence-record": "evidence_recorded",
    "evidence-approve": "evidence_approved",
    "profile-approve": "profile_approved",
    "candidate-freeze": "candidate_frozen",
    "scenario-prepare": "scenario_prepared",
    "scenario-capture": "scenario_captured",
    "scenario-seal": "scenario_sealed",
    "scenario-approve": "scenario_approved",
    "pair-record": "pair_recorded",
    "acceptance-record": "acceptance_recorded",
    "selector-observe": "selector_observed",
    "selector-activate": "selector_activated",
    "promotion-record": "release_promoted",
    "inventory-current-append": "inventory_current_appended",
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

    subparsers.add_parser("tool-digest", help="输出当前 FW-D 工具身份摘要")
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
        return {
            "schema_version": "official-client-control-tool-identity/v1",
            "sha256": control_tool_bundle_sha256(),
        }

    store = ControlStore(arguments.store)
    if command == "artifact-seal":
        return store.seal_object(arguments.kind, _load_object(arguments.input, "artifact payload"))
    if command == "campaign-create":
        return store.create_campaign(_load_object(arguments.input, "campaign"))
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
