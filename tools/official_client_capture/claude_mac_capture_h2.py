"""原夹具 H2 观察器薄壳；正式策略不能借本类取得用途权限。"""
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies
from tools.official_client_capture.claude_mac_capture_h2_core import *
from tools.official_client_capture.claude_mac_capture_h2_core import RawH2Parser, _integer
class H2Parser(RawH2Parser):
    """保持原调用形状、regression_fixture 断言与 false 权限字段。"""
    source_scope = c.SCOPE
    formal_ready = False
    launch_allowed = False
    def __init__(self, connection_id, limits, on_header=None, *, dependencies, source_scope=c.SCOPE):
        c.require(source_scope == c.SCOPE and isinstance(dependencies, FrozenDependencies),
                  "H2 观察器只接受回归夹具与冻结夹具依赖")
        super().__init__(connection_id, limits, on_header, dependencies=dependencies)
