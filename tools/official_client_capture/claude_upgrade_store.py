"""Claude Campaign 与受管 Store 的绑定（Claude 升级控制面设计 B2-09 起用；B2-07 seal 链、B2-11 建 Campaign 共用）。

约定：
* 一个 Claude Campaign 的受管 Store 固定在 ``<campaign_dir>/control-store``（沿用 2.1.226 各轮 ``control-store`` 的
  布局）；Store 里与总计划同 ID 的 Campaign 是这个 Campaign 的事实与收据层，必须是 Campaign v2、``client == claude``。
* Store Campaign 的冻结身份与总计划同源（设计 B2-11）：``target_version`` 与总计划相同，``platforms``／``entrypoints``
  各恰有一项且等于 ``configuration.platform``／``configuration.entrypoint``，``default_conditions`` 里唯一的 ``privacy=``
  条件等于 ``configuration.privacy_mode``，``oauth_org_id``、``model_catalog_sha256`` 与 ``configuration`` 相同。
  比较口径取 Store 合同的七项身份导出（``contracts.derive_identity_conditions``）。第七项 ``official_artifacts``（B2-11 定）：
  总计划 ``configuration`` 冻结了 B2-01 冻结收据坐标（``official_freeze_sha256``）时，Store 的官方制品绑定里必须有这份冻结
  收据与目标二进制（``official_identity.binary_sha256``）；B2-11 之前的夹具总计划没有这个坐标，不核。

Store 的 ``ControlError`` 在这里统一转成编排器的 ``ConfigurationError``，入口按同一口径报错退出。本文件被 Codex 身份
策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control.contracts import campaign_is_v2, derive_identity_conditions
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore

CAMPAIGN_STORE_DIRNAME = "control-store"
# 总计划 configuration 与 Store Campaign 七项身份导出的对应（official_artifacts 由 B2-11 定）。
PLAN_CONFIGURATION_CONDITIONS = ("platform", "entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256")


def campaign_store_root(campaign_dir: Path) -> Path:
    """Campaign 的受管 Store 根（固定坐标，不随参数变化）。"""

    return Path(campaign_dir) / CAMPAIGN_STORE_DIRNAME


def plan_identity_conditions(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """总计划声明的六项身份（七项去掉 ``official_artifacts``），口径与 Store 七项身份导出一致。"""

    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ConfigurationError("Claude Campaign 总计划缺少 configuration。")
    conditions = {"target_version": manifest.get("target_version")}
    for item in PLAN_CONFIGURATION_CONDITIONS:
        conditions[item] = configuration.get(item)
    return conditions


def require_store_campaign_matches_plan(manifest: Mapping[str, Any], campaign: Mapping[str, Any]) -> None:
    """Store Campaign 必须属于这份总计划：同 ID、v2、client 一致、六项冻结身份与总计划同源。"""

    if campaign.get("campaign_id") != manifest.get("campaign_id"):
        raise ConfigurationError("Store Campaign 与总计划不是同一个 Campaign。")
    if not campaign_is_v2(dict(campaign)):
        raise ConfigurationError("Claude Campaign 的 Store Campaign 必须是 v2。")
    try:
        upgrade_client_adapter.require_consistent_client(
            "claude", {"Campaign 总计划": manifest, "Store Campaign": campaign}
        )
    except upgrade_client_adapter.ClientAdapterError as error:
        raise ConfigurationError(f"Campaign 客户端身份不一致：{error}") from error
    derived, _unprovable = derive_identity_conditions(dict(campaign))
    declared = plan_identity_conditions(manifest)
    mismatches = [
        f"{item}：总计划 {declared[item]!r}，Store {derived[item]!r}"
        for item in declared
        if declared[item] != derived[item]
    ]
    if mismatches:
        raise ConfigurationError("Store Campaign 的冻结身份与总计划不同源：" + "；".join(mismatches))
    configuration = manifest.get("configuration")
    freeze_sha256 = configuration.get("official_freeze_sha256") if isinstance(configuration, Mapping) else None
    if freeze_sha256 is not None:
        official = manifest.get("official_identity")
        binary_sha256 = official.get("binary_sha256") if isinstance(official, Mapping) else None
        bound = {str(item.get("sha256")) for item in campaign.get("official_artifacts") or [] if isinstance(item, Mapping)}
        missing = [label for label, digest in (("冻结收据", freeze_sha256), ("目标二进制", binary_sha256)) if digest not in bound]
        if missing:
            raise ConfigurationError("Store Campaign 的官方制品绑定缺少总计划冻结的" + "、".join(missing) + "（第七项身份不同源）。")


def open_campaign_store(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[ControlStore, dict[str, Any]]:
    """打开 Campaign 的受管 Store（可写）并取出与总计划同 ID 的 Store Campaign，核对两者同源后返回。"""

    root = campaign_store_root(campaign_dir)
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(
            f"Campaign 缺少受管 Store：{root}（Store Campaign 由 VC-0 收口建 Campaign 时创建）。"
        )
    try:
        store = ControlStore(root)
        campaign = store.load_campaign(str(manifest["campaign_id"]))
    except ControlError as error:
        raise ConfigurationError(f"Campaign 受管 Store 不可用：{error}") from error
    require_store_campaign_matches_plan(manifest, campaign)
    return store, campaign
