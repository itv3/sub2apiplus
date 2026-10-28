"""R15 运行时出口的统一测试隔离夹具。

离线测试不得读取开发机或 ARM64 宿主上的真实出口配置（``/etc/sub2api-egress``、
``/run/sub2api-egress`` 与 ``/proc`` 下的宿主启动身份）。本模块把这三个读取位置统一重定向到
每次进入夹具时新建的私有临时目录下一个不存在的子目录，任何未经夹具显式替身的读取都会失败关闭，
而不会因为宿主上恰好存在（或不存在）真实配置而改变测试结果。在此之上按测试场景提供替身：

- ``offline_campaign_egress``：零请求离线夹具（合成 Campaign、演练父 run），判定为不要求出口准入；
- ``stubbed_runtime_egress_admission``：只验证消费者逻辑、需要一个准入返回值的测试；
- ``synthetic_environment_equivalence``：整条采集流程都是替身、环境收据只带连续性摘要的流程单测，
  用连续性摘要代替等价投影。凡能生成真实收据的消费者测试应改用
  ``control_receipt_fixtures.create_arm_receipt`` 经正式 finalize 封存的完整收据，不使用此替身。

真实准入（root 专有策略、守护状态、内核租期）只能以 root 在 ARM64 上验证，由 R15 专项测试与
真实链覆盖；这里的替身都不能授权任何真实请求。
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm

# ---------------------------------------------------------------------------------------------
# 测试里一切“出口 IP”的唯一来源。
#
# 真实出口由运维在 ``/etc/sub2api-egress/policy.json`` 里指定，老板想换就换，不属于工具、测试或文档；这里的值是
# 公网上随手挑的、不属于任何一台自有机器的全局单播地址，只用来让夹具、隔离网络命名空间拓扑和断言有值可用。
# 其余测试文件一律从本模块引用，不再自己写地址字面量（反向守卫也由这些常量派生，见 test_codex_runtime_egress）。
# ``tests/fixtures/runtime_egress_policy.json`` 是静态数据、不能引用常量，其中的取值必须与下面一致，
# 由 test_codex_runtime_egress 的一致性用例逐项核对；改常量时两处一起改。
# ---------------------------------------------------------------------------------------------
FIXTURE_EXIT_IPV4 = "1.0.0.102"
"""夹具策略的 ``allowed_public_ipv4[0]``，同时是出口节点的端点地址（端点与最终 SNAT 地址可以不同，夹具里取同一个）。"""
FIXTURE_ORIGIN_IPV4 = "8.8.4.102"
"""夹具策略里源端节点的公网端点地址。"""
FIXTURE_ALTERNATE_EXIT_IPV4S = ("1.0.0.1", "1.0.0.2")
"""等价性测试里“用户改选出口”所换成的其他取值；与 ``FIXTURE_EXIT_IPV4`` 互不相同。"""


@contextlib.contextmanager
def isolated_runtime_egress_paths() -> Iterator[Path]:
    """把策略、守护状态与启动身份三个读取位置重定向到私有临时目录下不存在的子目录。

    产出该子目录路径；调用方不应在其中创建文件，需要准入结果时使用下面的替身夹具。
    """

    with tempfile.TemporaryDirectory(prefix="runtime-egress-isolated-") as directory:
        absent = Path(directory).resolve() / "absent"
        with mock.patch.multiple(
            arm,
            EGRESS_POLICY_PATH=absent / "policy.json",
            EGRESS_STATUS_PATH=absent / "status.json",
            EGRESS_BOOT_ID_PATH=absent / "boot_id",
        ):
            yield absent


@contextlib.contextmanager
def offline_campaign_egress() -> Iterator[mock.Mock]:
    """零请求离线夹具：隔离读取位置，并判定当前 Campaign 不要求运行时出口准入。"""

    with isolated_runtime_egress_paths(), mock.patch.object(
        arm, "campaign_requires_runtime_egress", return_value=False,
    ) as requires:
        yield requires


@contextlib.contextmanager
def stubbed_runtime_egress_admission(value: dict[str, Any] | None = None) -> Iterator[mock.Mock]:
    """隔离读取位置，并让实时准入返回给定替身值（默认空对象），供只验证消费者逻辑的测试使用。"""

    with isolated_runtime_egress_paths(), mock.patch.object(
        arm, "require_runtime_egress", return_value={} if value is None else value,
    ) as admission:
        yield admission


@contextlib.contextmanager
def synthetic_environment_equivalence() -> Iterator[mock.Mock]:
    """合成环境收据只带连续性摘要时，用连续性摘要代替按原 producer 重放的等价投影。"""

    with mock.patch.object(
        arm,
        "receipt_equivalence_sha256",
        side_effect=lambda _root, receipt: receipt["continuity_identity_sha256"],
    ) as equivalence:
        yield equivalence
