"""原夹具私有依赖合同；正式后端使用另一份合同，不重标本类。"""
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_dependency_core import (
    FrozenDependencyBytes, PINS, _FrozenLoader, _FrozenFinder, _private_directory)
SCHEMA = "claude-upgrade-h2-private-dependencies/v1"
class FrozenDependencies(FrozenDependencyBytes):
    """仍只接受 regression_fixture 及原 false 权限字段。"""
    source_scope = c.SCOPE
    manifest_schema = SCHEMA
    receipt_schema = "claude-upgrade-h2-dependency-read-set/v1"
