"""Claude B5b-02 第一段：2.1.226 录制来源的只读快照与回归评估输入。

沿用 ``vc1_recorded_replay`` 的「先复制来源、再消费副本、前后核对来源哨兵」结构。Claude 的来源是旧 FW-F
录制，不是现控制面的 Formal Campaign，因此本模块只产出 ``regression_fixture``；不创建 VC 阶段事实、不替换
控制动作，也不登记 ``vc-chain.claude-vc1-capture`` 或完整链通过。后续驱动可以消费 ``load_snapshot`` 返回的
来源，替换 ``claude_chain_driver`` 的来源接口，不能因此跳过真实 VC-0／VC-1 控制动作。

只复制既有 ``claude_2_1_226_evaluator_fixture`` 消费的中继清单、relay 字节、debug 日志与 pcap，另保留明确登记的
顶层来源清单与每个已选 probe 的唯一宿主收据。永不遍历、读取或复制 ``state``／``recovery`` 凭据目录。主录制全部场景及补充录制的 omitted 场景
保持原字节、原 probe／run 坐标；summarized 补充缺 actual_wire_inventory，不纳入来源。

本模块不联网、不启动客户端或子进程。真实请求零次仅指本次离线操作，不能把录制中的历史模型请求当成零次。
测试入口由主会话串行运行；本模块不是 ARM64 完整链认证。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tools.official_client_capture.tests import claude_2_1_226_evaluator_fixture as evaluator_fixture

SNAPSHOT_SCHEMA = "claude-recorded-source-snapshot/v1"
SNAPSHOT_DIRNAME = "claude-recorded"
PURPOSE = "regression_fixture"
SOURCE_METADATA = ("campaign.json", "scenario-catalog.json", "candidate-denominator.json", "execution-summary.json")
PROBE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
# 合成场景的干预记录也是既有评估器的证据输入，必须保持原字节一起封存。
RELAY_FILE = re.compile(r"(?:relay\.json|intervention\.jsonl|conn[0-9]+\.(?:client_to_upstream|upstream_to_client)\.bin)\Z")


class ReplayError(RuntimeError):
    """录制来源、快照或副本完整性不满足回归输入合同。"""


def recorded_campaign_dir() -> Path:
    """显式环境变量优先；未设置时只查当前仓库的历史分析目录，不猜别的工作树。"""

    configured = os.environ.get(evaluator_fixture.FIXTURE_ENV)
    return Path(configured) if configured else evaluator_fixture.REPOSITORY_ROOT / "local-analysis" / "fw-f" / "claude-code-2.1.226" / evaluator_fixture.CAPTURE_ID


def _regular(path: Path, root: Path, *, directory: bool = False) -> None:
    """从已给定的来源根开始逐段核对，不跟随根内符号链接或特殊文件。"""

    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ReplayError(f"路径越过录制根：{path}") from error
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ReplayError(f"录制材料不存在或不可读：{current}") from error
        wanted_directory = current != path or directory
        if not (stat.S_ISDIR(mode) if wanted_directory else stat.S_ISREG(mode)):
            raise ReplayError(f"录制材料类型非法或含符号链接：{current}")


def _json(path: Path, root: Path) -> dict[str, Any]:
    _regular(path, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayError(f"录制 JSON 无法解析：{path}") from error
    if not isinstance(payload, dict):
        raise ReplayError(f"录制 JSON 必须是对象：{path}")
    return payload


def _source_plan(recorded: Path, *, source_selections: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
    """只读发现明确允许复制的文件；先核清单，再按场景访问已选 attempt，不扫描凭据目录。"""

    recorded = Path(os.path.abspath(recorded))
    _regular(recorded, recorded, directory=True)
    sources = {
        evaluator_fixture.CAPTURE_ID: recorded,
        evaluator_fixture.SUPPLEMENT_DIRECTORY: recorded.parent / evaluator_fixture.SUPPLEMENT_DIRECTORY,
    }
    selections: dict[str, Mapping[str, Any]] = {}
    for row in source_selections:
        fields = {"source_id", "probe_id", "attempt", "host_receipt", "manifest_sha256", "host_receipt_sha256"}
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ReplayError("显式录制来源选择字段不闭合")
        probe, source_id = row["probe_id"], row["source_id"]
        if (source_id != evaluator_fixture.SUPPLEMENT_DIRECTORY or not isinstance(probe, str)
                or not PROBE_ID.fullmatch(probe) or probe in selections
                or not isinstance(row["attempt"], str)
                or not re.fullmatch(re.escape(source_id + "/attempts/" + probe + "/") + r"attempt-[0-9]{3}", row["attempt"])
                or row["host_receipt"] != source_id + "/runtime-receipts/" + probe + "-" + Path(row["attempt"]).name + ".json"
                or any(not isinstance(row[key], str) or not re.fullmatch(r"[a-f0-9]{64}", row[key])
                       for key in ("manifest_sha256", "host_receipt_sha256"))):
            raise ReplayError("显式录制来源选择坐标或摘要非法")
        selections[probe] = row
    files: dict[str, Path] = {}
    probes: list[dict[str, str]] = []
    for source_id, root in sources.items():
        _regular(root, root, directory=True)
        campaign = _json(root / "campaign.json", root)
        catalog = _json(root / "scenario-catalog.json", root)
        if campaign.get("campaign_id") != source_id or any(document.get("target_version") != evaluator_fixture.TARGET_VERSION for document in (campaign, catalog)):
            raise ReplayError(f"来源 Campaign 或版本不符：{root}")
        rows = catalog.get("probes")
        if not isinstance(rows, list) or not rows:
            raise ReplayError(f"来源场景清单为空或非法：{root}")
        catalog_probes: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            probe = row.get("probe_id") if isinstance(row, Mapping) else None
            if not isinstance(probe, str) or not PROBE_ID.fullmatch(probe) or probe in catalog_probes:
                raise ReplayError(f"来源场景 ID 非法或重复：{root}")
            catalog_probes[probe] = row
        chosen = sorted(catalog_probes) if source_id == evaluator_fixture.CAPTURE_ID else list(evaluator_fixture.SUPPLEMENT_PROBES)
        if source_id == evaluator_fixture.SUPPLEMENT_DIRECTORY:
            chosen = sorted(set(chosen) | set(selections))
        for filename in SOURCE_METADATA:
            path = root / filename
            if path.exists() or path.is_symlink():
                _regular(path, root)
                files[f"{source_id}/{filename}"] = path
        for probe in chosen:
            if probe not in catalog_probes:
                raise ReplayError(f"补充场景没有登记：{probe}")
            selection = selections.get(probe) if source_id == evaluator_fixture.SUPPLEMENT_DIRECTORY else None
            attempt = recorded.parent / selection["attempt"] if selection is not None else root / "attempts" / probe / "attempt-001"
            _regular(attempt, root, directory=True)
            manifest = _json(attempt / "relay-manifest.json", root)
            if selection is not None and _digest(attempt / "relay-manifest.json")["sha256"] != selection["manifest_sha256"]:
                raise ReplayError("显式录制来源的清单摘要漂移")
            client = manifest.get("client") or {}
            dimensions = manifest.get("dimension_evidence") or {}
            if (manifest.get("schema_version") != "claude-code-fw-e-relay-run/v1" or manifest.get("status") != "complete"
                    or manifest.get("probe_id") != probe or not isinstance(client, Mapping)
                    or str(client.get("version", "")).split(" ")[0] != evaluator_fixture.TARGET_VERSION
                    or not isinstance(dimensions, Mapping) or not isinstance(dimensions.get("actual_wire_inventory"), list)):
                raise ReplayError(f"录制场景尚未闭合、版本不符或缺 wire inventory：{source_id}/{probe}")
            # 只按已选 probe 精确加入一份原宿主收据，不遍历 runtime-receipts，也不接受旁支凭据。
            receipt_path = recorded.parent / selection["host_receipt"] if selection is not None else root / "runtime-receipts" / f"{probe}-attempt-001.json"
            receipt = _json(receipt_path, root)
            if selection is not None and _digest(receipt_path)["sha256"] != selection["host_receipt_sha256"]:
                raise ReplayError("显式录制来源的宿主收据摘要漂移")
            binding = (manifest.get("runtime") or {}).get("host_runtime_receipt") or {}
            if (receipt.get("schema_version") != "official-client-runtime-host-receipt/v1"
                    or _digest(receipt_path)["sha256"] != binding.get("sha256")
                    or not receipt.get("run_nonce") or receipt.get("run_nonce") != binding.get("run_nonce")):
                raise ReplayError(f"原宿主收据与 relay 的摘要或 nonce 不闭合：{source_id}/{probe}")
            selected = [attempt / "relay-manifest.json", receipt_path]
            relay = attempt / "relay"
            _regular(relay, root, directory=True)
            _regular(relay / "relay.json", root)
            for path in sorted(relay.iterdir()):
                if not RELAY_FILE.fullmatch(path.name):
                    raise ReplayError(f"中继目录出现未登记的文件：{path}")
                _regular(path, root)
                selected.append(path)
            for relative, required_flag in (("results/debug.log", "require_debug_log"), ("tls-clienthello.pcap", "require_pcap")):
                path = attempt / relative
                if path.exists() or path.is_symlink() or catalog_probes[probe].get(required_flag) is True:
                    _regular(path, root)
                    selected.append(path)
            for path in selected:
                files[f"{source_id}/{path.relative_to(root).as_posix()}"] = path
            row = {"source_id": source_id, "probe_id": probe, "attempt": f"{source_id}/{attempt.relative_to(root).as_posix()}"}
            if selection is not None:
                row["host_receipt"] = selection["host_receipt"]
            probes.append(row)
    if len({row["probe_id"] for row in probes}) != len(probes):
        raise ReplayError("主录制与补充录制出现同名场景，拒绝猜选来源")
    return {"sources": sources, "files": files, "probes": probes}


def available() -> bool:
    """只报告快照输入是否可用；不代表 Linux 命名空间或 VC 链运行条件已满足。"""

    try:
        _source_plan(recorded_campaign_dir())
    except (ReplayError, OSError, ValueError):
        return False
    return True


def _stat(path: Path) -> list[int]:
    metadata = path.lstat()
    return [metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns]


def _sentinel(plan: Mapping[str, Any]) -> dict[str, list[int]]:
    """只登记允许读取的来源文件及其祖先目录；atime 不计，读文件不应触发内容漂移。"""

    paths = set(plan["files"].values())
    roots = set(plan["sources"].values())
    for path in list(paths):
        parent = path.parent
        while parent not in roots:
            paths.add(parent)
            parent = parent.parent
        paths.add(parent)
    return {str(path): _stat(path) for path in sorted(paths)}


def production_sentinel(recorded: Path | None = None, *, source_selections: tuple[Mapping[str, Any], ...] = ()) -> dict[str, list[int]]:
    """沿用 Codex 回放基座的哨兵接口；这里的“生产来源”仅指只读历史录制文件，不连接生产服务。"""

    return _sentinel(_source_plan(recorded if recorded is not None else recorded_campaign_dir(), source_selections=source_selections))


def _digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "bytes": size}


def build_snapshot(staging: Path, *, recorded: Path | None = None,
                   source_selections: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
    """只读复制来源，返回外部绑定的索引摘要；失败的私有快照保留，禁止覆盖重用。"""

    plan = _source_plan(recorded if recorded is not None else recorded_campaign_dir(), source_selections=source_selections)
    before = _sentinel(plan)
    staging = Path(staging).resolve()
    snapshot = staging / SNAPSHOT_DIRNAME
    for root in plan["sources"].values():
        source = root.resolve()
        if snapshot == source or snapshot.is_relative_to(source) or source.is_relative_to(snapshot):
            raise ReplayError("快照与只读来源目录重叠，拒绝写入")
    if snapshot.exists() or snapshot.is_symlink():
        raise ReplayError(f"快照目录已存在，禁止覆盖：{snapshot}")
    snapshot.mkdir(parents=True, mode=0o700)
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    bindings: dict[str, dict[str, Any]] = {}
    for relative, source in sorted(plan["files"].items()):
        destination = data / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        bindings[relative] = _digest(destination)
        if bindings[relative] != _digest(source):
            raise ReplayError(f"录制复制期间内容漂移：{relative}")
    if _sentinel(plan) != before:
        raise ReplayError("只读来源在快照期间发生元数据或内容边界变化")
    index = {
        "schema_version": SNAPSHOT_SCHEMA,
        "purpose": PURPOSE,
        "target_version": evaluator_fixture.TARGET_VERSION,
        "sources": {key: str(value) for key, value in plan["sources"].items()},
        "probes": plan["probes"],
        "files": bindings,
        "source_sentinel": before,
        "excluded_sources": [{"source_id": evaluator_fixture.SUPPLEMENT_DIRECTORY, "probe_id": "v4-thinking-display-summarized", "reason": "历史补充缺 actual_wire_inventory，未登记为可复用来源"}],
    }
    if source_selections:
        index["source_selections"] = list(source_selections)
        selected = {row["probe_id"] for row in source_selections}
        index["excluded_sources"] = [row for row in index["excluded_sources"] if row["probe_id"] not in selected]
    path = snapshot / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    binding = {"path": str(path), **_digest(path)}
    load_snapshot(binding)
    return binding


def _snapshot_files(root: Path) -> dict[str, Path]:
    """副本目录必须恰好由清单覆盖；不跟随符号链接，不容许夹带凭据目录或特殊文件。"""

    files: dict[str, Path] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        _regular(directory, root, directory=True)
        for path in directory.iterdir():
            if path.name in {"state", "recovery"} or path.is_symlink():
                raise ReplayError(f"快照夹带禁止目录或符号链接：{path}")
            if path.is_dir():
                pending.append(path)
            else:
                _regular(path, root)
                files[path.relative_to(root).as_posix()] = path
    return files


def load_snapshot(binding: Mapping[str, Any]) -> dict[str, Any]:
    """先验证调用方持有的索引摘要，再逐文件复算；不能靠改索引给篡改后的副本重新背书。"""

    if set(binding) != {"path", "sha256", "bytes"}:
        raise ReplayError("录制快照索引绑定字段不闭合")
    index_path = Path(str(binding["path"]))
    root = index_path.parent
    _regular(index_path, root)
    if _digest(index_path) != {key: binding[key] for key in ("sha256", "bytes")}:
        raise ReplayError("录制快照索引摘要漂移")
    index = _json(index_path, root)
    if (index.get("schema_version") != SNAPSHOT_SCHEMA or index.get("purpose") != PURPOSE
            or index.get("target_version") != evaluator_fixture.TARGET_VERSION):
        raise ReplayError("快照不是登记的 2.1.226 regression_fixture")
    expected = index.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ReplayError("录制快照文件清单为空或非法")
    actual = _snapshot_files(root / "data")
    if set(actual) != set(expected):
        raise ReplayError("录制快照文件集合与索引不一致")
    for relative, record in expected.items():
        parts = PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or any(part in {".", "..", "state", "recovery"} for part in parts):
            raise ReplayError(f"录制快照相对路径非法：{relative}")
        if not isinstance(record, dict) or set(record) != {"sha256", "bytes"} or _digest(actual[relative]) != record:
            raise ReplayError(f"录制快照附件摘要漂移：{relative}")
    return {**index, "evidence_root": str(root / "data"), "recorded_campaign_dir": str(root / "data" / evaluator_fixture.CAPTURE_ID)}


def assert_source_untouched(binding: Mapping[str, Any]) -> None:
    """消费副本之后重放来源哨兵；不修改来源权限或时间。"""

    index = load_snapshot(binding)
    current = production_sentinel(Path(index["sources"][evaluator_fixture.CAPTURE_ID]),
                                  source_selections=tuple(index.get("source_selections", ())))
    if current != index["source_sentinel"]:
        raise ReplayError("历史录制来源在回放期间发生变化")


def write_evaluation_manifest(binding: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """复用既有真实评估器夹具，只从副本生成评估输入；不签官方／候选证据或阶段通过事实。"""

    index = load_snapshot(binding)
    output = Path(output).resolve()
    protected = [Path(index["evidence_root"]).parent, *(Path(root).resolve() for root in index["sources"].values())]
    if any(output == root or output.is_relative_to(root) for root in protected):
        raise ReplayError("评估输出不得写回快照或历史来源")
    if output.exists() or output.is_symlink():
        raise ReplayError("评估输出已存在，禁止覆盖")
    manifest = evaluator_fixture.write_evaluation_manifest(Path(index["recorded_campaign_dir"]), output)
    output.chmod(0o600)
    assert_source_untouched(binding)
    return {"purpose": PURPOSE, "snapshot": dict(binding), "evaluation_manifest": {"path": str(output), **_digest(output)}, "artifact_count": len(manifest["artifacts"])}
