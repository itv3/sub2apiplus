#!/usr/bin/env python3
"""校验运行投影闭集：`catalogdata/runtime` 必须等于 `egressruntimedump` 的导出，
只允许经当前 Active 终态收据批准的“原地冻结历史制品”不参与比较。

退休一个旧 Previous 时，它的运行画像会退出 Catalog、selector 与运行投影；但当该画像
被更早的终态收据登记为逐文件校验制品时（例如 `0.147→0.149.1` 终态收据把
`profiles/0.149.1/8c22d3b1….json` 冻结为 `runtime_catalog.active_profile`），字节必须
原地保留，于是运行目录里会出现不在 dump 闭集内的文件。

本脚本让门禁显式区分这两类文件，而不是按路径做可扩张白名单：排除项只能来自当前 Active
终态收据的 `retained_runtime_profiles`，并与它绑定的 RemovalReceipt 逐条交叉验证；任何
一项不满足都失败关闭，排除之后其余文件仍必须与 dump 结果逐字节完全一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

RUNTIME_RELATIVE = "backend/internal/officialegress/catalogdata/runtime"
MAINTENANCE_RELATIVE = "docs/egress/maintenance"
TERMINAL_RECEIPT_GLOB = "CODEX_CLI_*_TERMINAL_STATE_RECEIPT.json"
TERMINAL_SCHEMA_RE = re.compile(r"^official-client-codex-(\d+\.\d+\.\d+)-terminal-state/v1$")
REMOVAL_SCHEMA = "codex-runtime-profile-removal/v1"
RETAINED_STATE = "retained_as_frozen_terminal_artifact"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProjectionError(RuntimeError):
    """运行投影闭集不可信；一律失败关闭，不降级为警告。"""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ProjectionError(f"{label}不是普通文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectionError(f"{label}无法读取：{path}：{error}") from error


def _terminal_identity(receipt: dict[str, Any]) -> tuple[str, str]:
    """按生成端口径复算终态收据自摘要（带／不带尾换行两种都接受）。"""

    document = {key: value for key, value in receipt.items() if key != "identity_sha256"}
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8")), _sha256_bytes((canonical + "\n").encode("utf-8"))


def _active_version(root: Path) -> str:
    """从 selector 指向的 ReleaseGraph 解析当前 Active 版本。"""

    runtime_root = root / RUNTIME_RELATIVE
    selector = _load_json(runtime_root / "release-catalog.json", "运行 selector")
    graph_reference = selector.get("release_graph") if isinstance(selector, dict) else None
    if not isinstance(graph_reference, dict) or not isinstance(graph_reference.get("path"), str):
        raise ProjectionError("运行 selector 缺少 release_graph 坐标")
    graph_relative = graph_reference["path"]
    prefix = "catalogdata/runtime/"
    if not graph_relative.startswith(prefix) or ".." in Path(graph_relative).parts:
        raise ProjectionError(f"ReleaseGraph 坐标非法：{graph_relative}")
    graph = _load_json(runtime_root / graph_relative[len(prefix):], "ReleaseGraph")
    versions = {
        str((node.get("build") or {}).get("version", ""))
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("mode") == "active"
    }
    if len(versions) != 1 or not VERSION_RE.fullmatch(next(iter(versions))):
        raise ProjectionError(f"ReleaseGraph active 版本不唯一或非法：{sorted(versions)}")
    return versions.pop()


def _catalog_references_version(root: Path, version: str) -> bool:
    """当前 selector 指向的 ReleaseGraph 与 SnapshotCatalog 是否仍引用该版本。"""

    runtime_root = root / RUNTIME_RELATIVE
    selector = _load_json(runtime_root / "release-catalog.json", "运行 selector")
    prefix = "catalogdata/runtime/"
    for key in ("release_graph", "snapshot_catalog"):
        reference = selector.get(key)
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ProjectionError(f"运行 selector 缺少 {key} 坐标")
        relative = reference["path"]
        if not relative.startswith(prefix) or ".." in Path(relative).parts:
            raise ProjectionError(f"{key} 坐标非法：{relative}")
        raw = (runtime_root / relative[len(prefix):]).read_text(encoding="utf-8")
        if version in raw:
            return True
    return version in (runtime_root / "release-catalog.json").read_text(encoding="utf-8")


def _terminal_receipt_for(root: Path, active_version: str) -> tuple[Path, dict[str, Any]]:
    maintenance = root / MAINTENANCE_RELATIVE
    matched: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(maintenance.glob(TERMINAL_RECEIPT_GLOB)):
        receipt = _load_json(path, "终态收据")
        if not isinstance(receipt, dict):
            continue
        schema = TERMINAL_SCHEMA_RE.fullmatch(str(receipt.get("schema_version", "")))
        if schema is not None and schema.group(1) == active_version:
            matched.append((path, receipt))
    if len(matched) != 1:
        raise ProjectionError(
            f"当前 Active {active_version} 的终态收据必须恰好一份，实得 {len(matched)} 份"
        )
    return matched[0]


def approved_retained_files(root: Path, dump_relatives: set[str]) -> set[str]:
    """返回允许不参与运行闭集比较的文件（相对 runtime 根）；无批准项时返回空集。

    只有同时满足下列条件才批准：两份收据自摘要与绑定摘要一致、身份一致；路径严格位于
    对应退休版本的 profiles 目录；普通文件、非符号链接、无路径穿越；两份收据的路径、
    摘要与状态一一对应；文件当前摘要与收据一致；该文件既不在 dump 结果中，也不被当前
    Catalog 引用。
    """

    active_version = _active_version(root)
    receipt_path, receipt = _terminal_receipt_for(root, active_version)
    identity = str(receipt.get("identity_sha256", ""))
    if identity not in _terminal_identity(receipt):
        raise ProjectionError(f"终态收据自摘要不一致：{receipt_path}")
    if receipt.get("result") != "passed":
        raise ProjectionError(f"终态收据 result 非 passed：{receipt_path}")

    retained = receipt.get("retained_runtime_profiles")
    if retained is None or (isinstance(retained, list) and not retained):
        return set()
    if not isinstance(retained, list):
        raise ProjectionError("终态收据 retained_runtime_profiles 必须是数组")

    binding = receipt.get("runtime_profile_removal")
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise ProjectionError("终态收据缺少 runtime_profile_removal 坐标")
    removal_relative = binding["path"]
    if not removal_relative.startswith(MAINTENANCE_RELATIVE + "/") or ".." in Path(removal_relative).parts:
        raise ProjectionError(f"RemovalReceipt 坐标非法：{removal_relative}")
    removal_path = root / removal_relative
    if not SHA256_RE.fullmatch(str(binding.get("sha256", ""))) or _sha256_file(removal_path) != binding["sha256"]:
        raise ProjectionError("RemovalReceipt 摘要与终态收据绑定不一致")
    if binding.get("bytes") != removal_path.stat().st_size:
        raise ProjectionError("RemovalReceipt 字节数与终态收据绑定不一致")

    removal = _load_json(removal_path, "RemovalReceipt")
    if (
        removal.get("schema_version") != REMOVAL_SCHEMA
        or removal.get("status") != "complete"
        or removal.get("active_version") != active_version
        or not VERSION_RE.fullmatch(str(removal.get("removed_version", "")))
    ):
        raise ProjectionError("RemovalReceipt 顶层身份与当前 Active 不一致")
    removed_version = str(removal["removed_version"])
    if removed_version == active_version:
        raise ProjectionError("RemovalReceipt 的退休版本不得等于当前 Active")

    chain = receipt.get("campaign_chain")
    if not isinstance(chain, list) or not chain:
        raise ProjectionError("终态收据缺少 Campaign 承接链")
    last_campaign = str((chain[-1] or {}).get("campaign_id", ""))
    if not last_campaign or removal.get("campaign_id") != last_campaign:
        raise ProjectionError("RemovalReceipt 与终态收据的末级 Campaign 身份不一致")

    retired = removal.get("retired_runtime_profiles")
    if not isinstance(retired, list) or len(retired) != len(retained):
        raise ProjectionError("两份收据的退休画像清单条数不一致")

    def normalized(entries: list[Any], label: str) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ProjectionError(f"{label}条目必须是对象")
            path_text = entry.get("path")
            digest = entry.get("sha256")
            state = entry.get("state")
            if (
                not isinstance(path_text, str)
                or not SHA256_RE.fullmatch(str(digest))
                or state != RETAINED_STATE
            ):
                raise ProjectionError(f"{label}条目字段非法或状态不是 {RETAINED_STATE}")
            rows.append((path_text, str(digest), str(state)))
        return sorted(rows)

    if normalized(retained, "终态收据") != normalized(retired, "RemovalReceipt"):
        raise ProjectionError("两份收据的路径、摘要或状态未一一对应")

    profiles_prefix = f"{RUNTIME_RELATIVE}/profiles/{removed_version}/"
    approved: set[str] = set()
    for path_text, digest, _state in normalized(retained, "终态收据"):
        parts = Path(path_text).parts
        if Path(path_text).is_absolute() or ".." in parts or "" in parts:
            raise ProjectionError(f"冻结制品路径不规范：{path_text}")
        if not path_text.startswith(profiles_prefix):
            raise ProjectionError(
                f"冻结制品必须位于退休版本目录 {profiles_prefix}：{path_text}"
            )
        target = root / path_text
        if target.is_symlink() or not target.is_file():
            raise ProjectionError(f"冻结制品不是普通文件：{path_text}")
        if _sha256_file(target) != digest:
            raise ProjectionError(f"冻结制品当前摘要与收据不一致：{path_text}")
        relative = path_text[len(RUNTIME_RELATIVE) + 1:]
        if relative in dump_relatives:
            raise ProjectionError(f"冻结制品仍在运行投影导出中，不得排除：{path_text}")
        approved.add(relative)

    if _catalog_references_version(root, removed_version):
        raise ProjectionError(f"当前 Catalog 仍引用已退休版本 {removed_version}，不得排除其画像")
    return approved


def _relative_files(directory: Path, label: str) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ProjectionError(f"{label}含符号链接：{path}")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path
    return files


def verify(root: Path, dump_root: Path, repo_root: Path) -> dict[str, Any]:
    dump_files = _relative_files(dump_root, "运行投影导出")
    repo_files = _relative_files(repo_root, "仓库运行目录")
    approved = approved_retained_files(root, set(dump_files))

    missing = sorted(set(dump_files) - set(repo_files))
    if missing:
        raise ProjectionError(f"仓库缺少运行投影文件：{missing}")
    extra = sorted(set(repo_files) - set(dump_files))
    unapproved = sorted(set(extra) - approved)
    if unapproved:
        raise ProjectionError(f"仓库存在未经终态收据批准的多余文件：{unapproved}")
    stale = sorted(approved - set(extra))
    if stale:
        raise ProjectionError(f"终态收据批准的冻结制品不存在于仓库运行目录：{stale}")
    drifted = [
        name
        for name in sorted(set(dump_files) & set(repo_files))
        if dump_files[name].read_bytes() != repo_files[name].read_bytes()
    ]
    if drifted:
        raise ProjectionError(f"运行投影文件内容漂移：{drifted}")
    return {
        "dump_file_count": len(dump_files),
        "repo_file_count": len(repo_files),
        "approved_frozen_artifacts": sorted(approved),
    }


# ---------------------------------------------------------------------------
# 自测：正例与六类负例都在门禁内执行，避免排除逻辑只在真实仓库上被验证。
# ---------------------------------------------------------------------------

ACTIVE_VERSION = "1.0.0"
RETIRED_VERSION = "0.9.0"
CAMPAIGN_ID = "c-selftest-campaign"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_fixture(base: Path) -> tuple[Path, Path, Path]:
    """构造最小夹具：runtime 目录、dump 目录与两份收据。"""

    root = base / "repo"
    runtime = root / RUNTIME_RELATIVE
    active_profile = runtime / "profiles" / ACTIVE_VERSION / ("a" * 64 + ".json")
    retained_one = runtime / "profiles" / RETIRED_VERSION / ("b" * 64 + ".json")
    retained_two = runtime / "profiles" / RETIRED_VERSION / ("c" * 64 + ".json")
    for path, payload in (
        (active_profile, {"Version": ACTIVE_VERSION}),
        (retained_one, {"Version": RETIRED_VERSION, "slot": 1}),
        (retained_two, {"Version": RETIRED_VERSION, "slot": 2}),
    ):
        _write_json(path, payload)

    graph = {"nodes": [{"mode": "active", "build": {"version": ACTIVE_VERSION, "source": f"campaign:{CAMPAIGN_ID}/x"}}]}
    graph_name = _sha256_bytes(json.dumps(graph, sort_keys=True).encode()) + ".json"
    _write_json(runtime / "release-graphs" / graph_name, graph)
    snapshot = {"snapshots": [{"version": ACTIVE_VERSION, "file": f"profiles/{ACTIVE_VERSION}/{'a' * 64}.json"}]}
    snapshot_name = _sha256_bytes(json.dumps(snapshot, sort_keys=True).encode()) + ".json"
    _write_json(runtime / "snapshot-catalogs" / snapshot_name, snapshot)
    _write_json(
        runtime / "release-catalog.json",
        {
            "release_graph": {"path": f"catalogdata/runtime/release-graphs/{graph_name}"},
            "snapshot_catalog": {"path": f"catalogdata/runtime/snapshot-catalogs/{snapshot_name}"},
            "source": f"campaign:{CAMPAIGN_ID}/retirement:selftest",
        },
    )

    retained_entries = [
        {
            "path": str(path.relative_to(root)),
            "sha256": _sha256_file(path),
            "state": RETAINED_STATE,
        }
        for path in (retained_one, retained_two)
    ]
    removal = {
        "schema_version": REMOVAL_SCHEMA,
        "status": "complete",
        "campaign_id": CAMPAIGN_ID,
        "active_version": ACTIVE_VERSION,
        "rollback_version": "0.9.5",
        "removed_version": RETIRED_VERSION,
        "retired_runtime_profiles": retained_entries,
    }
    removal_relative = f"{MAINTENANCE_RELATIVE}/CODEX_CLI_SELFTEST_RUNTIME_PROFILE_REMOVAL_RECEIPT.json"
    _write_json(root / removal_relative, removal)
    removal_path = root / removal_relative

    receipt = {
        "schema_version": f"official-client-codex-{ACTIVE_VERSION}-terminal-state/v1",
        "result": "passed",
        "target": {"version": ACTIVE_VERSION},
        "campaign_chain": [{"campaign_id": CAMPAIGN_ID}],
        "runtime_profile_removal": {
            "path": removal_relative,
            "sha256": _sha256_file(removal_path),
            "bytes": removal_path.stat().st_size,
        },
        "retained_runtime_profiles": retained_entries,
    }
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    receipt["identity_sha256"] = _sha256_bytes(canonical.encode())
    _write_json(root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_TERMINAL_STATE_RECEIPT.json", receipt)

    dump = base / "dump"
    for relative in (
        f"profiles/{ACTIVE_VERSION}/{'a' * 64}.json",
        f"release-graphs/{graph_name}",
        f"snapshot-catalogs/{snapshot_name}",
        "release-catalog.json",
    ):
        target = dump / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((runtime / relative).read_bytes())
    return root, dump, runtime


def _expect_failure(label: str, root: Path, dump: Path, runtime: Path) -> None:
    try:
        verify(root, dump, runtime)
    except ProjectionError:
        return
    raise SystemExit(f"🔴 自测负例未失败关闭：{label}")


def self_test() -> int:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)

        root, dump, runtime = _build_fixture(base / "ok")
        report = verify(root, dump, runtime)
        if len(report["approved_frozen_artifacts"]) != 2 or report["dump_file_count"] != 4:
            raise SystemExit(f"🔴 自测正例结果异常：{report}")

        # 负例 1：未登记的孤儿文件。
        root, dump, runtime = _build_fixture(base / "orphan")
        _write_json(runtime / "profiles" / RETIRED_VERSION / ("d" * 64 + ".json"), {"orphan": True})
        _expect_failure("孤儿文件", root, dump, runtime)

        # 负例 2：伪造终态收据（自摘要不符）。
        root, dump, runtime = _build_fixture(base / "identity")
        receipt_path = root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_TERMINAL_STATE_RECEIPT.json"
        forged = json.loads(receipt_path.read_text(encoding="utf-8"))
        forged["identity_sha256"] = "0" * 64
        _write_json(receipt_path, forged)
        _expect_failure("伪造终态收据", root, dump, runtime)

        # 负例 3：RemovalReceipt 未被终态收据绑定（摘要不符）。
        root, dump, runtime = _build_fixture(base / "binding")
        removal_path = root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_RUNTIME_PROFILE_REMOVAL_RECEIPT.json"
        tampered = json.loads(removal_path.read_text(encoding="utf-8"))
        tampered["rollback_version"] = "0.9.6"
        _write_json(removal_path, tampered)
        _expect_failure("RemovalReceipt 未绑定", root, dump, runtime)

        # 负例 4：冻结制品内容漂移。
        root, dump, runtime = _build_fixture(base / "drift")
        _write_json(runtime / "profiles" / RETIRED_VERSION / ("b" * 64 + ".json"), {"drifted": True})
        _expect_failure("冻结制品摘要漂移", root, dump, runtime)

        # 负例 5：冻结制品是符号链接。
        root, dump, runtime = _build_fixture(base / "symlink")
        victim = runtime / "profiles" / RETIRED_VERSION / ("b" * 64 + ".json")
        payload = victim.read_bytes()
        elsewhere = root / "elsewhere.json"
        elsewhere.write_bytes(payload)
        victim.unlink()
        victim.symlink_to(elsewhere)
        _expect_failure("符号链接冻结制品", root, dump, runtime)

        # 负例 6：收据登记的路径越出退休版本目录。
        root, dump, runtime = _build_fixture(base / "escape")
        receipt_path = root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_TERMINAL_STATE_RECEIPT.json"
        escaped = json.loads(receipt_path.read_text(encoding="utf-8"))
        escaped["retained_runtime_profiles"][0]["path"] = f"{RUNTIME_RELATIVE}/../../../secret.json"
        canonical = json.dumps(
            {k: v for k, v in escaped.items() if k != "identity_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        escaped["identity_sha256"] = _sha256_bytes(canonical.encode())
        _write_json(receipt_path, escaped)
        _expect_failure("路径越界", root, dump, runtime)

        # 负例 7：两份收据清单不一致。
        root, dump, runtime = _build_fixture(base / "mismatch")
        removal_path = root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_RUNTIME_PROFILE_REMOVAL_RECEIPT.json"
        mismatched = json.loads(removal_path.read_text(encoding="utf-8"))
        mismatched["retired_runtime_profiles"][0]["sha256"] = "e" * 64
        _write_json(removal_path, mismatched)
        receipt_path = root / MAINTENANCE_RELATIVE / "CODEX_CLI_SELFTEST_TERMINAL_STATE_RECEIPT.json"
        rebound = json.loads(receipt_path.read_text(encoding="utf-8"))
        rebound["runtime_profile_removal"]["sha256"] = _sha256_file(removal_path)
        rebound["runtime_profile_removal"]["bytes"] = removal_path.stat().st_size
        canonical = json.dumps(
            {k: v for k, v in rebound.items() if k != "identity_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        rebound["identity_sha256"] = _sha256_bytes(canonical.encode())
        _write_json(receipt_path, rebound)
        _expect_failure("两份收据清单不一致", root, dump, runtime)

        # 负例 8：当前 Catalog 仍引用被退休版本。
        root, dump, runtime = _build_fixture(base / "referenced")
        selector_path = runtime / "release-catalog.json"
        selector = json.loads(selector_path.read_text(encoding="utf-8"))
        snapshot_relative = selector["snapshot_catalog"]["path"].split("catalogdata/runtime/", 1)[1]
        snapshot_path = runtime / snapshot_relative
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["snapshots"].append({"version": RETIRED_VERSION, "file": "x"})
        _write_json(snapshot_path, snapshot)
        (dump / snapshot_relative).write_bytes(snapshot_path.read_bytes())
        _expect_failure("Catalog 仍引用退休版本", root, dump, runtime)

    print("运行投影闭集门禁自测通过：正例 1 项、负例 8 项")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="只运行内置正负例夹具")
    parser.add_argument("--root", type=Path, default=None, help="仓库根；默认按脚本位置推断")
    parser.add_argument("--dump", type=Path, default=None, help="egressruntimedump 的导出目录")
    parser.add_argument("--repo", type=Path, default=None, help="仓库内 catalogdata/runtime 目录")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        return self_test()
    root = (arguments.root or Path(__file__).resolve().parents[1]).resolve()
    dump = arguments.dump
    repo = arguments.repo or (root / RUNTIME_RELATIVE)
    if dump is None:
        parser.error("实检必须提供 --dump")
    try:
        report = verify(root, dump.resolve(), repo.resolve())
    except ProjectionError as error:
        print(f"🔴 运行投影闭集校验失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
