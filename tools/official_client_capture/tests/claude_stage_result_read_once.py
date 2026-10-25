"""VC-6 真实批次链测试专用：结尾只读断言共用一次「交付阶段结果」读取（只在测试里，不进受管树）。

背景（2026-10-25 第 5 步分段计时，T1 报告 C3）
============================================
``test_vc6_batches_deliver_recover_and_complete`` 在最后一次派发（finalize）之后，依次做三件互相独立的只读核对：

1. 测试自己调用 ``claude_upgrade_delivery.load_stage_result``，取 ``vc6_status``、``release_state``、``receipt_ref`` 做断言；
2. 测试调用 ``claude_upgrade_vc.replay_vc_checkpoint(... "VC-6" ...)``，它在 VC-6 分支里的
   ``_validate_vc6_delivery_consumer`` 会**再调用一次** ``load_stage_result`` 并要求与阶段收据逐字相同；
3. 测试调用 ``assert_status(... "ready_for_operator_release")``，``delivery_status`` 在里面**第三次**调用
   ``load_stage_result``。

``load_stage_result`` 会重验 VC-5 交接与 Store 的交付收据链（``validate_current_stage_result`` → ``replay_receipt``），
本机单次约 10～20 秒（CI 约 3 倍）。三次调用的参数等价（同一 Campaign、同一候选），读到的文件一个字节都没变，
白白算了三遍。

做法
----
在这三步外面套一个作用域：作用域内对同一 ``(Campaign 目录, 候选)`` 的**第一次**调用真实执行，之后的调用直接返回
第一次的结果（深拷贝）。三条断言一条不少，三个调用方各自的其余校验（replay 的前序链回溯与逐字比对、status 的
``_resolve`` 与 checkpoint 文件核对、测试自己的字段断言）全部照常执行。

为什么安全
----------
* 作用域内只有只读调用，测试代码不写任何文件；作用域入口记下阶段结果文件与 VC-6 checkpoint 文件的 sha256，
  出口再核一次，**不相等就失败**。因此“作用域内读到的是同一份字节”不是假设，而是被核实过的事实。
* 复用的只是 ``load_stage_result`` 这一个确定性函数在同一份输入上的返回值；``replay_vc_checkpoint`` 拿到后仍会与
  它自己从磁盘读出的阶段收据逐字比较（``checked != result`` 分支），所以文件若有漂移，这一比较照常拒绝。
* 调用方若带了 ``manifest`` 参数，复用前仍核 ``campaign_id`` 一致，不一致就走真实调用，由它给出权威拒绝。
* 真实调用抛出异常时不缓存，异常原样向上传播；返回 ``None`` 时同样不缓存。
* 退出作用域即恢复原函数，其他所有测试不受影响。

独立性损失：三个调用方不再各自独立地重验一遍 VC-5 交接与 Store 收据链。这条链本身由 ``load_stage_result`` 的第一次真实
调用完整验证，且它前面的派发 16（finalize）已经在动作进程里做过同样的验证；专项覆盖在
``test_claude_real_delivery_integration`` 与 ``test_claude_upgrade_delivery``。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_vc


class StageResultReadOnce:
    """``load_stage_result`` 的作用域内单次读取包装；见模块说明。"""

    def __init__(self, real: Any):
        self.real = real
        self.results: dict[tuple[str, str], dict[str, Any]] = {}
        self.real_calls = 0
        self.reused_calls = 0

    def __call__(self, campaign_dir: Path, candidate_id: str, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        key = (str(Path(campaign_dir).resolve()), str(candidate_id))
        cached = self.results.get(key)
        if cached is not None and (manifest is None or cached.get("campaign_id") == manifest.get("campaign_id")):
            self.reused_calls += 1
            return copy.deepcopy(cached)
        self.real_calls += 1
        result = self.real(campaign_dir, candidate_id, manifest=manifest)
        if result is not None:
            self.results[key] = copy.deepcopy(result)
        return result


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


@contextlib.contextmanager
def stage_result_read_once(campaign: Path, candidate_id: str, revision: int) -> Iterator[StageResultReadOnce]:
    """作用域内共用一次 ``load_stage_result``；出口核对两份落盘文件未变。"""

    stage_path = delivery.stage_result_path(campaign, candidate_id)
    checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(campaign, delivery.DELIVERY_PHASE, revision=revision)
    watched = (stage_path, checkpoint_path)
    before = [_file_digest(path) for path in watched]
    wrapper = StageResultReadOnce(delivery.load_stage_result)
    with mock.patch.object(delivery, "load_stage_result", wrapper):
        yield wrapper
    after = [_file_digest(path) for path in watched]
    if before != after or None in before:
        raise AssertionError(f"结尾只读核对期间阶段结果或 VC-6 checkpoint 发生了变化（{list(zip(watched, before, after))}）")


__all__ = ["StageResultReadOnce", "stage_result_read_once"]
