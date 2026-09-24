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
