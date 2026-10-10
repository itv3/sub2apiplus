"""固定 Mac 来源适配器；接口存在不能替代受管安装与实际来源资格。

来源名称由源码固定。只读解析先核 ARM64 独立捕获与 Mac 认证桥的真实安装，
两端均通过后才登记同一个适配器实例。构造句柄不占用预算、写文件或启动产品；
这些动作仍由原 Candidate 服务在租约和持久预算准入后执行。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c


RECORDER_ID = c.MANAGED_RECORDER_ID


class ManagedMacSource:
    """只调用固定实现和实际验证器，不接收调用方工厂、模块或命令。

    每个实例拥有它构造的捕获与认证桥句柄；只有类型相同、字段相同或自报
    formal 的外部对象不能通过所有权检查。来源重放重新验证已封存原件，
    不要求产品仍在运行，也不会为重放再启动产品。
    """

    def __init__(self) -> None:
        self._captures: dict[int, tuple[Any, dict[str, Any], Path]] = {}
        self._recorders: dict[int, tuple[Any, dict[str, Any], Path]] = {}

    def require_installation(self, task: Mapping[str, Any]) -> None:
        from tools.official_client_capture import claude_mac_formal_capture_contract as capture
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        c.require(task["execution"]["recorder_id"] == RECORDER_ID
                  and task["execution"]["source_scope"] == "formal",
                  "固定 Mac 来源只接受准确的正式 recorder 与用途")
        capture.require_formal_capture_installation(task)
        bridge.require_mac_bridge_installation(task)

    def owns_services(self, services: Any) -> bool:
        from tools.official_client_capture.claude_upgrade_mac_candidate_services import CandidateMacClientServices

        return type(services) is CandidateMacClientServices

    def capture_factory(self, task: Mapping[str, Any], *, attempt_root: Path, services: Any) -> Any:
        from tools.official_client_capture import claude_mac_formal_capture_service as capture

        self.require_installation(task)
        c.require(self.owns_services(services), "正式捕获必须由原 Candidate 服务构造")
        handle = capture.create_formal_capture(task, attempt_root=Path(attempt_root), services=services)
        capture.verify_formal_capture_handle(task, handle, Path(attempt_root))
        self._captures[id(handle)] = (handle, copy.deepcopy(dict(task)), Path(attempt_root))
        return handle

    def owns_capture(self, handle: Any) -> bool:
        saved = self._captures.get(id(handle))
        return saved is not None and saved[0] is handle

    def verify_capture_handle(self, task: Mapping[str, Any], handle: Any, attempt_root: Path) -> None:
        from tools.official_client_capture import claude_mac_formal_capture_service as capture

        saved = self._captures.get(id(handle))
        c.require(saved is not None and saved[0] is handle and saved[1] == dict(task)
                  and saved[2] == Path(attempt_root), "捕获句柄没有归属同一冻结 task 与原 attempt")
        self.require_installation(task)
        capture.verify_formal_capture_handle(task, handle, Path(attempt_root))

    def recorder_factory(self, task: Mapping[str, Any], *, bundle_root: Path, services: Any) -> Any:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        self.require_installation(task)
        c.require(self.owns_services(services), "产品认证桥必须复用原 Candidate 服务")
        saved = services._captures.get(task["task_sha256"])
        c.require(saved is not None and saved[0] is self and self.owns_capture(saved[1]),
                  "产品认证桥缺原 task 的已验独立捕获句柄")
        recorder = bridge.create_mac_agent_bridge(task, bundle_root=Path(bundle_root),
                    capture_handle=saved[1], services=services)
        c.require(bridge.owns_mac_agent_bridge(recorder, task, Path(bundle_root)),
                  "产品认证桥不是受管固定工厂的原实例")
        self._recorders[id(recorder)] = (recorder, copy.deepcopy(dict(task)), Path(bundle_root))
        return recorder

    def owns_recorder(self, recorder: Any) -> bool:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        saved = self._recorders.get(id(recorder))
        return (saved is not None and saved[0] is recorder
                and bridge.owns_mac_agent_bridge(recorder, saved[1], saved[2]))

    def verify_capture_origin(self, task: Mapping[str, Any], capture_root: Path) -> None:
        from tools.official_client_capture import claude_mac_formal_capture_contract as capture

        self.require_installation(task)
        capture.verify_formal_capture_origin(task, Path(capture_root))

    def verify_undispatched(self, task: Mapping[str, Any], capture_root: Path) -> None:
        from tools.official_client_capture import claude_mac_formal_capture_contract as capture

        self.require_installation(task)
        capture.verify_formal_undispatched(task, Path(capture_root))

    def verify_bridge_path_context(self, task: Mapping[str, Any], bundle_root: Path) -> Any:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        self.require_installation(task)
        return bridge.verify_bridge_path_context(task, Path(bundle_root))

    def verify_isolation_recipe(self, task: Mapping[str, Any], recipe: Mapping[str, Any], bundle_root: Path) -> None:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        self.require_installation(task)
        bridge.verify_bridge_isolation_recipe(task, recipe, Path(bundle_root))

    def verify_origin(self, task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> None:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        self.verify_capture_origin(task, Path(capture_root))
        bridge.verify_mac_bridge_origin(task, Path(bundle_root))


_MANAGED_SOURCE = ManagedMacSource()


def load_registered_source(task: Mapping[str, Any]) -> ManagedMacSource:
    """固定安装实际验真后才返回原实例；没有备用路径、动态回调或占位成功。"""
    _MANAGED_SOURCE.require_installation(task)
    return _MANAGED_SOURCE
