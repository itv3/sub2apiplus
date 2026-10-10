"""原夹具解释器与 OpenSSL 合同；只复用用途中性的实际库定位。"""
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_runtime_core import collect_runtime_facts, _file
def collect_runtime():
    return {"schema_version": "claude-upgrade-native-runtime/v2", "source_scope": c.SCOPE,
            "formal_ready": False, "launch_allowed": False, **collect_runtime_facts()}
def verify_runtime(expected):
    c.require(expected == collect_runtime(), "冻结解释器或实际加载 OpenSSL 已漂移")
    c.require(expected["ssl"]["has_alpn"] is True, "实际 SSL 不具备 ALPN")
def verify_binding(root, binding):
    expected = c.read_json(c.check_binding(root, binding))
    verify_runtime(expected)
    return expected
