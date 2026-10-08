"""按事先固定的原 manifest 和成员索引离线重建任务私有依赖。"""
from __future__ import annotations
import atexit
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies

PRODUCTION = {"path": "production_decoder-dependencies.json", "sha256": "634dd0cf165d872d5fcc00bd8789df8834b8cae28df1edb4ab58d11e0e6a2cbe", "bytes": 4625}
PEER = {"path": "test_peer-dependencies.json", "sha256": "4f9208a5b949ca24bca3e2deea211840dfb26b2ba1705e2910014347e2bf255d", "bytes": 12008}
INDEX = {"path": "source-summary.json", "sha256": "8a064c3666bfea39662497a4b3e54b2b01cb840407ecc0863c55b539f0f0b670", "bytes": 14790}
DEFAULT_SOURCE = Path(__file__).resolve().parent / "fixtures/claude_h2_dependencies"
_cached = None

def _source(root, binding):
    path = root / binding["path"]
    c.require(path.is_file() and not path.is_symlink(), "冻结发行物原件不是普通文件")
    raw = path.read_bytes()
    c.require(len(raw) == binding["bytes"] and hashlib.sha256(raw).hexdigest() == binding["sha256"],
              "提交态冻结发行物或 manifest 摘要漂移")
    return raw

def prepare(root, *, source=DEFAULT_SOURCE):
    """只按原索引复制和解包；不生成另一份 manifest 或可信摘要。"""
    root = c.private_directory(Path(root).resolve())
    raw_index = _source(source, INDEX)
    index = json.loads(raw_index)
    for binding in (INDEX, PRODUCTION, PEER):
        c.write_once(root / binding["path"], _source(source, binding))
    for row in index:
        for key in ("metadata", "wheel"):
            binding = row[key]
            target = c.managed_path(root, binding["path"])
            c.private_directory(target.parent)
            c.write_once(target, _source(source, binding))
        with zipfile.ZipFile(c.managed_path(root, row["wheel"]["path"])) as archive:
            members = archive.infolist()
            expected = {Path(item["path"]).relative_to(row["root"]).as_posix(): item for item in row["files"]}
            c.require(len(members) == len(expected) and {item.filename for item in members} == set(expected),
                      "官方 wheel 与冻结完整成员索引不符")
            for member in members:
                binding = expected[member.filename]
                raw = archive.read(member)
                c.require(not member.is_dir() and len(raw) == binding["bytes"]
                          and hashlib.sha256(raw).hexdigest() == binding["sha256"],
                          "官方成员原字节不符固定索引")
                target = c.managed_path(root, binding["path"])
                parts = target.parent.relative_to(root).parts
                for depth in range(1, len(parts) + 1):
                    c.private_directory(root.joinpath(*parts[:depth]))
                c.write_once(target, raw)
    FrozenDependencies(root, PRODUCTION, artifact_only=True)
    FrozenDependencies(root, PEER, kind="test_peer", artifact_only=True)
    return root

def bundles():
    global _cached
    names = ("CLAUDE_H2_DEPENDENCY_ROOT", "CLAUDE_H2_PRODUCTION_MANIFEST_BINDING",
             "CLAUDE_H2_PEER_MANIFEST_BINDING")
    values = [os.environ.get(name) for name in names]
    c.require(all(values) or not any(values), "H2 私有依赖覆盖必须完整提供三个环境变量")
    if all(values):
        root = Path(values[0])
        bindings = [json.loads(value) for value in values[1:]]
        c.require(bindings == [PRODUCTION, PEER], "显式覆盖不能变更固定原 manifest")
        return root, FrozenDependencies(root, PRODUCTION), FrozenDependencies(root, PEER, kind="test_peer")
    if _cached is None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-h2-private-")
        atexit.register(temporary.cleanup)
        root = prepare(Path(temporary.name))
        _cached = (root, FrozenDependencies(root, PRODUCTION),
                   FrozenDependencies(root, PEER, kind="test_peer"))
    return _cached
