"""基线实际生成的 Claude v1 原件，只解包原字节，不调用当前签发器。

固定来源为 cfc3ae053f8582486e1c4dc76b759e18d370f406。原宿主及容器枚举
使用该提交的真实实现，Docker 仅由本机执行替身承接；官方二进制与失败生命周期
采用既有离线事实替身。三个事实/收据对的 producer、路径、摘要和缺项原样保留。
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).with_name("fixtures") / "claude_job_rehearsal_v1.json.gz"
BASE_COMMIT = "cfc3ae053f8582486e1c4dc76b759e18d370f406"


def copy_formed_v1(root: Path) -> dict[str, Any]:
    """校验形成来源后逐字节解包；既有文件拒绝覆盖。"""
    bundle = json.loads(gzip.decompress(FIXTURE.read_bytes()).decode("utf-8"))
    provenance = bundle["provenance"]
    if provenance["base_commit"] != BASE_COMMIT or provenance["actual_enumeration"] is not True:
        raise AssertionError("v1 原件形成来源不匹配")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for record in provenance["artifacts"]:
        raw = bundle["artifacts"][record["path"]].encode("utf-8")
        if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise AssertionError("v1 原件字节摘要不匹配")
        output = root / record["path"]
        with output.open("xb") as stream:
            stream.write(raw)
        output.chmod(0o600)
    return provenance
