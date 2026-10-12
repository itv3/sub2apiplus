"""仅供离线回归的 2.1.226 来源合同；不扩展生产冻结、relay 或封存合同。

旧录制是 linux/amd64、npm 单来源。这里明确保留该身份，不能声称它是 ARM64 双渠道冻结。
每次监督复制分别绑定本轮 runtime 和原始 origin，两个收据都要重验。原 relay 清单另存为原件，host 收据、
请求字节与附件逐字保留；只有通过真实 v5 判定器重验后才生成本轮控制封套。封套不是本轮客户端执行的证明，
也不能签发正式版本语义认证。此模块必须由隔离测试入口显式启用，生产代码不会导入它。
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_replay as replay

SCHEMA = "claude-recorded-chain-source/v1"
PROJECTION_SCHEMA = "claude-recorded-step-projection/v1"
VERSION = "2.1.226"
# 已登记的历史原件；不能用另一份自洽 v1 清单扩张适配器接受范围。
ORIGIN_FREEZE_SHA256 = "947ea92b56bc85b0fc87cc2ef196b627a846c35b925b47fa9730c01ae3381283"
STATE_ENV = "CLAUDE_VC1_RECORDED_SOURCE"
DIGEST_ENV = "CLAUDE_VC1_RECORDED_SOURCE_SHA256"
OPERATION = re.compile(r"(?:synthetic-job|job):([a-z0-9-]+):step-([1-9][0-9]*):attempt-([1-9][0-9]*)\Z")
RECOVERY_CONTROL_SCHEMA = "claude-recorded-candidate-recovery-control/v1"
CONTROL_PROJECTION_SCHEMA = "claude-recorded-control-envelope/v1"
ORIGIN_MANIFEST_FILENAME = ".origin-relay-manifest.json"


def full79_time_budget() -> dict[str, Any]:
    """完整79回归的唯一执行上限；原方法、子外壳与最终单元消费同值。

    四小时是挂死保护上限，不是预计耗时或性能通过凭证。业务三层截止、原始起点、
    旧动作和旧失败原件仍由原消费者核验；VC-1及其它八链继续原v2时间合同。
    不读取任意预算环境变量，也不签发预算授权或覆盖已派发动作。
    """
    from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification

    return certification.claude_full79_time_budget()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise replay.ReplayError("测试来源 JSON 必须是对象")
    return value


def write(path: Path, value: Any) -> Path:
    """测试控制输入一次性写入；路径本身不允许覆盖或跟随链接。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    path.chmod(0o600)
    return path


def binding(path: Path) -> dict[str, Any]:
    return {"path": str(path), **replay._digest(path)}


def bound(value: Mapping[str, Any], *, root: Path | None = None) -> Path:
    """外持摘要是信任起点；逐段拒绝符号链接及目录穿越。"""

    if set(value) != {"path", "sha256", "bytes"}:
        raise replay.ReplayError("测试来源绑定字段不闭合")
    path = Path(str(value["path"]))
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise replay.ReplayError("测试来源绑定必须是无链接、无穿越的绝对路径")
    replay._regular(path, root or path.parent)
    if replay._digest(path) != {key: value[key] for key in ("sha256", "bytes")}:
        raise replay.ReplayError("测试来源绑定摘要或字节数漂移")
    return path


def stage_freeze(destination: Path, source: Path) -> dict[str, Any]:
    """按旧清单精确复制 registry、主包、x64 包与 x64 binary；不杜撰 GCS 来源。"""

    source = Path(source).resolve()
    replay._regular(source / "freeze.json", source)
    if replay._digest(source / "freeze.json")["sha256"] != ORIGIN_FREEZE_SHA256:
        raise replay.ReplayError("旧冻结不是已登记的 2.1.226 历史原件")
    document = read(source / "freeze.json")
    if document.get("schema_version") != "claude-code-fw-e-official-freeze/v1" or document.get("target_version") != VERSION:
        raise replay.ReplayError("录制来源必须是 2.1.226 的 v1 冻结")
    linux = document["binaries"]["linux/amd64"]
    packages = {"@anthropic-ai/claude-code", "@anthropic-ai/claude-code-linux-x64"}
    artifacts = [item for item in document["artifacts"] if item["package"] in packages]
    if {item["package"] for item in artifacts} != packages or len(artifacts) != 2:
        raise replay.ReplayError("旧冻结缺少主包或 x64 平台包")
    members = {
        "freeze.json": replay._digest(source / "freeze.json"),
        document["registry_snapshot_path"]: {"sha256": document["registry_snapshot_sha256"]},
        linux["path"]: {"sha256": linux["sha256"], "bytes": linux["bytes"]},
        **{item["tarball_path"]: {"sha256": item["tarball_sha256"], "bytes": item["tarball_bytes"]} for item in artifacts},
    }
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(source):
        raise replay.ReplayError("冻结副本已存在或与来源重叠")
    destination.mkdir(parents=True, mode=0o700)
    records = {}
    sentinel = {}
    for relative, expected in sorted(members.items()):
        original = source / relative
        replay._regular(original, source)
        actual = replay._digest(original)
        if any(actual[key] != value for key, value in expected.items()):
            raise replay.ReplayError("旧冻结附件与原清单不一致")
        sentinel[str(original)] = replay._stat(original)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(original, copied)
        copied.chmod(0o600)
        records[relative] = binding(copied)
    return {"fixture_only": True, "origin_platform": "linux/amd64", "files": records,
            "source_sentinel": sentinel, "binary_sha256": linux["sha256"], "binary_path": linux["path"],
            "npm": {"artifacts": artifacts, "platform_package": "@anthropic-ai/claude-code-linux-x64"}}


def verify_origin_freeze(state: Mapping[str, Any], path: Path, **expected: Any) -> dict[str, Any]:
    """严格验证测试合同选定的旧来源；不会调用或替换生产 v2 算法内部的任何判据。"""

    freeze = state["freeze"]
    files = freeze["files"]
    recorded = bound(files["freeze.json"])
    if Path(path) != recorded or files["freeze.json"]["sha256"] != ORIGIN_FREEZE_SHA256:
        raise replay.ReplayError("来源适配仅接受本合同登记的旧 freeze")
    document = read(recorded)
    if document.get("schema_version") != "claude-code-fw-e-official-freeze/v1" or document.get("target_version") != VERSION:
        raise replay.ReplayError("旧来源冻结合同或版本漂移")
    linux = document["binaries"]["linux/amd64"]
    packages = {"@anthropic-ai/claude-code", "@anthropic-ai/claude-code-linux-x64"}
    artifacts = [item for item in document["artifacts"] if item["package"] in packages]
    expected_files = {"freeze.json", document["registry_snapshot_path"], linux["path"],
                      *(item["tarball_path"] for item in artifacts)}
    if (set(files) != expected_files or len(artifacts) != 2 or any(item["version"] != VERSION for item in artifacts)
            or {item["package"] for item in artifacts} != packages or artifacts != freeze["npm"]["artifacts"]
            or files[document["registry_snapshot_path"]]["sha256"] != document["registry_snapshot_sha256"]
            or any(files[linux["path"]][key] != linux[key] for key in ("sha256", "bytes"))
            or any(files[item["tarball_path"]][key] != item["tarball_" + key] for item in artifacts for key in ("sha256", "bytes"))):
        raise replay.ReplayError("旧来源附件分母、registry、npm 或 binary 清单绑定不闭合")
    for name, item in files.items():
        if bound(item, root=recorded.parent) != recorded.parent / name:
            raise replay.ReplayError("旧来源冻结附件坐标错位")
    wanted = {"expected_version": VERSION, "expected_binary_sha256": freeze["binary_sha256"],
              "expected_freeze_sha256": files["freeze.json"]["sha256"]}
    if any(value is not None and value != wanted[key] for key, value in expected.items() if key in wanted):
        raise replay.ReplayError("本次 Campaign 身份与录制旧来源不符")
    if linux["sha256"] != freeze["binary_sha256"] or linux["path"] != freeze["binary_path"]:
        raise replay.ReplayError("旧来源二进制绑定不符")
    # 该返回只供测试中的来源接缝消费；明确不返回 npm=gcs。
    return {"schema_version": "claude-recorded-origin-verification/v1", "fixture_only": True,
            "freeze_path": str(recorded), "freeze_sha256": files["freeze.json"]["sha256"],
            "binary_sha256": freeze["binary_sha256"], "binary_equivalence": "recorded-npm-origin-only",
            "target_version": VERSION, "platform": "linux/amd64",
            "verified_files": [{"path": key, "sha256": item["sha256"]} for key, item in files.items() if key != "freeze.json"]}


def origin_identity(state: Mapping[str, Any], path: Path) -> dict[str, Any]:
    verified = verify_origin_freeze(state, path)
    return {"npm": state["freeze"]["npm"], "gcs": {"fixture_only": True, "available": False},
            "binary_sha256": verified["binary_sha256"], "binary_equivalence": verified["binary_equivalence"]}


def verify_historical_static(root: Path) -> dict[str, Any]:
    """独立只读验源：固定 307 sink 原件，不把历史静态结论混成主链的合成控制投影。"""

    expected = {
        "target-sink-inventory.json": "1fa7c8133e4d0ab971b46ed44e7d6fb26766367ec63eeba9a5d9eb05ca5645db",
        "disposition-evidence-v2/sink-containment.json": "ffacbf0183b87f133b8a1057ec08a42e1b3f83a9cc419454bbb66ac87ed46e12",
    }
    records = {}
    before = {}
    for relative, digest in expected.items():
        path = root / relative
        replay._regular(path, root)
        before[str(path)] = replay._stat(path)
        records[relative] = binding(path)
        if records[relative]["sha256"] != digest:
            raise replay.ReplayError("历史 307 sink 静态原件摘要不符")
    inventory = read(root / "target-sink-inventory.json")
    containment = read(root / "disposition-evidence-v2/sink-containment.json")
    if (inventory.get("target_version") != VERSION or containment.get("target_version") != VERSION
            or len(inventory.get("sinks", [])) != 307 or len(containment.get("evidence", [])) != 307
            or containment.get("inventory", {}).get("sha256") != expected["target-sink-inventory.json"]
            or {item["sink_id"] for item in inventory["sinks"]} != {item["sink_id"] for item in containment["evidence"]}):
        raise replay.ReplayError("历史 307 sink 静态合同或绑定不闭合")
    if any(replay._stat(Path(path)) != metadata for path, metadata in before.items()):
        raise replay.ReplayError("只读静态验源期间历史材料发生变化")
    return {"fixture_only": True, "purpose": "historical_static_source_validation", "target_version": VERSION,
            "sink_count": 307, "files": records, "files_written": 0, "vc_chain_certified": False}


def load_state(path: Path, digest: str, *, isolation: bool = True) -> dict[str, Any]:
    replay._regular(path, path.parent)
    if replay._digest(path)["sha256"] != digest:
        raise replay.ReplayError("来源合同与外部持有摘要不符")
    state = read(path)
    if (state.get("schema_version") != SCHEMA or state.get("fixture_only") is not True
            or state.get("purpose") != "regression_fixture" or state.get("target_version") != VERSION):
        raise replay.ReplayError("仅接受 fixture_only 的 2.1.226 来源合同")
    data = Path(state["data_root"])
    if (not data.is_absolute() or data.resolve() != data or ".." in path.parts
            or path.resolve() != path or "staging" not in data.parts or not path.is_relative_to(data)):
        raise replay.ReplayError("录制链只能位于隔离 staging 数据根")
    if isolation:
        require_isolation(state)
    return state


def require_isolation(state: Mapping[str, Any]) -> None:
    """运行条件必须实测满足；环境测试替身不得替代命名空间、root 或宿主架构。"""

    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"} or os.geteuid() != 0:
        raise replay.ReplayError("录制链需要 Linux ARM64 root；缺条件只能报告未验证")
    for kind in ("mnt", "net"):
        current = os.readlink(f"/proc/self/ns/{kind}")
        if current == state["parent_namespaces"][kind]:
            raise replay.ReplayError("录制链没有进入独立挂载与断网命名空间")
    if sorted(name for _index, name in socket.if_nameindex()) != ["lo"]:
        raise replay.ReplayError("断网命名空间仍存在外网接口")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    for root in state["readonly_sources"]:
        rows = [line.split() for line in mounts if len(line.split()) > 6 and line.split()[4] == root]
        if not rows or "ro" not in rows[-1][5].split(","):
            raise replay.ReplayError("历史来源没有只读挂载")


def snapshot_index(state: Mapping[str, Any]) -> dict[str, Any]:
    """步骤只读取外持摘要绑定的索引；所选附件随后逐个重验，避免每步反复扫描全部历史流量。"""

    path = bound(state["snapshot"])
    index = read(path)
    if index.get("purpose") != "regression_fixture" or index.get("target_version") != VERSION:
        raise replay.ReplayError("快照身份不符")
    return {**index, "root": str(path.parent / "data")}


def recovery_control(state: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    """录制候选故障只能来自冻结的私有控制合同，不能由未登记的环境变量注入。"""

    binding = state.get("inputs", {}).get("candidate_recovery_control")
    if binding is None:
        return None
    data = Path(state["data_root"])
    path = bound(binding, root=data / "control")
    control = read(path)
    wanted = {"schema_version", "fixture_only", "purpose", "control_root", "allowed_failure_probe_ids"}
    probes = control.get("allowed_failure_probe_ids")
    registered = {probe for items in state["inputs"]["candidate_jobs"].values() for probe in items}
    if (set(control) != wanted or control.get("schema_version") != RECOVERY_CONTROL_SCHEMA
            or control.get("fixture_only") is not True or control.get("purpose") != "regression_fixture"
            or not isinstance(probes, list) or not probes
            or any(not isinstance(probe, str) or probe not in registered for probe in probes)
            or len(probes) != len(set(probes)) or not isinstance(control.get("control_root"), str)):
        raise replay.ReplayError("候选恢复控制合同或故障 probe 白名单非法")
    root = Path(control["control_root"])
    if root != data / "control/candidate-recovery" or root.resolve() != root:
        raise replay.ReplayError("候选恢复控制根越界或带链接")
    replay._regular(root, data / "control", directory=True)
    if root.stat().st_mode & 0o077:
        raise replay.ReplayError("候选恢复控制根必须私有")
    failures = root / "failures.json"
    replay._regular(failures, root)
    if failures.stat().st_mode & 0o077:
        raise replay.ReplayError("候选故障表必须私有")
    limits = read(failures)
    if (not set(limits).issubset(probes)
            or any(type(limit) is not int or limit < 0 for limit in limits.values())):
        raise replay.ReplayError("候选故障表超出冻结白名单或次数非法")
    return path, control


def record_candidate_action(state: Mapping[str, Any], projection: Mapping[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """原始附件复制完成后，记录真实复制动作并决定一次性退出故障；不修改 R/P 或历史来源。"""

    configured = recovery_control(state)
    if configured is None:
        return 0, None
    import fcntl
    path, control = configured
    root, probe = Path(control["control_root"]), projection["probe_id"]
    # 同一世界的作业可能并发；独立锁保护计数与日志，每个 operation 的决策只发布一次。
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    if (root / ".lock").exists() or (root / ".lock").is_symlink():
        replay._regular(root / ".lock", root)
    descriptor = os.open(root / ".lock", flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        failures_path = root / "failures.json"
        recovery_control(state)
        failures = read(failures_path)
        counts_path = root / "counts.json"
        counts = {}
        if counts_path.exists() or counts_path.is_symlink():
            replay._regular(counts_path, root)
            counts = read(counts_path)
            if (not set(counts).issubset(control["allowed_failure_probe_ids"])
                    or any(type(count) is not int or count < 0 for count in counts.values())):
                raise replay.ReplayError("候选复制故障计数非法")
        count = counts.get(probe, 0)
        if probe in failures:
            count += 1
            counts[probe] = count
        code = 2 if count <= failures.get(probe, 0) and failures.get(probe, 0) > 0 else 0
        decision_path = root / "events" / (hashlib.sha256(str(projection["operation"]).encode()).hexdigest() + ".json")
        decision = write(decision_path, {"schema_version": "claude-recorded-candidate-action/v1", "fixture_only": True,
            "purpose": "regression_fixture", "control": binding(path), "failures": binding(failures_path),
            "operation": projection["operation"], "probe_id": probe, "copy_completed": True,
            "failure_count": count, "return_code": code, "network_requests_this_run": 0})
        raw = (json.dumps(counts, ensure_ascii=False, sort_keys=True) + "\n").encode()
        count_fd = os.open(counts_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(count_fd, raw)
        finally:
            os.close(count_fd)
        log = root / "calls.jsonl"
        if log.exists() or log.is_symlink():
            replay._regular(log, root)
        log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(log_fd, (json.dumps({"probe_id": probe, "operation": projection["operation"],
                                        "decision": binding(decision)}, sort_keys=True) + "\n").encode())
        finally:
            os.close(log_fd)
        return code, binding(decision)
    finally:
        os.close(descriptor)


def origin_probe(state: Mapping[str, Any], probe: str) -> dict[str, Any]:
    index = snapshot_index(state)
    rows = [row for row in index["probes"] if row["probe_id"] == probe]
    if len(rows) != 1:
        raise replay.ReplayError("录制 probe 未唯一登记")
    row = rows[0]
    root = Path(index["root"])
    relative = row.get("host_receipt", f"{row['source_id']}/runtime-receipts/{probe}-attempt-001.json")
    # 新登记来源明确选择第二次及其原收据；旧 v1 行继续读原来的第一次，不猜其它成功 attempt。
    if relative != f"{row['source_id']}/runtime-receipts/{probe}-{Path(row['attempt']).name}.json":
        raise replay.ReplayError("录制来源的 attempt 与宿主收据坐标不同")
    receipt = bound({"path": str(root / relative), **index["files"][relative]}, root=root)
    manifest_relative = f"{row['attempt']}/relay-manifest.json"
    manifest_path = bound({"path": str(root / manifest_relative), **index["files"][manifest_relative]}, root=root)
    manifest, host = read(manifest_path), read(receipt)
    old = manifest["runtime"]["host_runtime_receipt"]
    client = manifest["client"]
    bundle = host.get("capture_source_bundle", {})
    runtime = manifest["runtime"]
    if (host.get("schema_version") != "official-client-runtime-host-receipt/v1"
            or old["sha256"] != replay._digest(receipt)["sha256"] or old["run_nonce"] != host["run_nonce"]
            or str(client["version"]).split(" ")[0] != VERSION or client["sha256"] != state["freeze"]["binary_sha256"]
            or manifest["probe_id"] != probe or host.get("repo_digest_verified") is not True
            or host.get("docker_server", {}).get("os") != "linux" or host.get("docker_server", {}).get("arch") != "amd64"
            or old.get("repo_digest_verified") is not True
            or any(old.get(key) != host.get(key) for key in ("schema_version", "container", "docker_server", "runtime_image_reference", "runtime_image_id"))
            or runtime.get("runtime_image_reference") != host.get("runtime_image_reference")
            or not re.fullmatch(r"[a-f0-9]{64}", str(bundle.get("sha256", "")))
            or old.get("capture_source_bundle_sha256") != bundle["sha256"]
            or runtime.get("capture_tools", {}).get("execution_sources") != bundle
            or old.get("container_runtime_binding", {}).get("verified") is not True
            or old["container_runtime_binding"].get("container_id") != host.get("container", {}).get("id")):
        raise replay.ReplayError("原始 relay、宿主收据或 binary 身份断裂")
    return {"row": row, "index": index, "manifest": manifest, "manifest_path": manifest_path,
            "receipt": host, "receipt_path": receipt}


def require_registered_jobs(snapshot: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    """阶段运行前核双侧实际冻结 Job 的来源闭集；不以来源总数或其它 Job 的记录替代。"""

    index = replay.load_snapshot(snapshot)
    available = {row["probe_id"] for row in index["probes"]}
    for side, key in (("official", "jobs"), ("candidate", "candidate_jobs")):
        missing = sorted({probe for probes in inputs.get(key, {}).values() for probe in probes} - available)
        if missing:
            raise replay.ReplayError(f"{side} 冻结 Job 缺少已登记来源：{missing}")


def verify_projection(state: Mapping[str, Any], projection: Mapping[str, Any]) -> dict[str, Any]:
    """当前运行与历史来源必须同时重验；返回可信原始来源，不能用一方替另一方背书。"""

    from tools.official_client_capture import claude_upgrade_capture as capture

    if projection.get("schema_version") != PROJECTION_SCHEMA or projection.get("fixture_only") is not True:
        raise replay.ReplayError("步骤来源投影合同非法")
    operation = str(projection["operation"])
    matched = OPERATION.fullmatch(operation)
    if matched is None or matched.group(1) != projection["job_id"]:
        raise replay.ReplayError("步骤 operation 与 Job 身份不符")
    expected = state["jobs"].get(projection["job_id"], [])
    step = int(matched.group(2))
    if step > len(expected) or expected[step - 1] != projection["probe_id"]:
        raise replay.ReplayError("未知 Job／probe／step，不允许复制")
    data = Path(state["data_root"])
    runtime_path = bound(projection["runtime"]["receipt"], root=data)
    runtime = read(runtime_path)
    current = projection["runtime"]
    reservation_path = bound(current["reservation"], root=data)
    reservation = read(reservation_path)
    expected_nonce = capture._step_run_nonce(reservation["run_nonce"], projection["job_id"], int(matched.group(3)), step)
    if (reservation.get("schema_version") != capture.CAPTURE_RESERVATION_SCHEMA or reservation.get("mode") != capture.MODE_FORMAL
            or reservation.get("attempt_id") != current["attempt_id"] or reservation_path.parent.name != current["attempt_id"]
            or projection["job_id"] not in {row["id"] for row in reservation.get("planned_jobs", [])}
            or not runtime_path.is_relative_to(reservation_path.parent)
            or runtime.get("schema_version") != capture.HOST_RUNTIME_RECEIPT_SCHEMA
            or current["run_nonce"] != expected_nonce
            or runtime.get("run_nonce") != current["run_nonce"] or runtime.get("runtime_image_reference") != current["runtime_image"]
            or runtime.get("container", {}).get("name") != current["container_name"]
            or runtime.get("container", {}).get("id") != current["container_id"]
            or runtime.get("repo_digest_verified") is not True):
        raise replay.ReplayError("本次复制 runtime 身份不符")
    origin = origin_probe(state, projection["probe_id"])
    original = bound(projection["origin"]["receipt"], root=data)
    if (original.read_bytes() != origin["receipt_path"].read_bytes()
            or projection["origin"]["run_nonce"] != origin["receipt"]["run_nonce"]
            or projection["origin"].get("source_id") != origin["row"]["source_id"]
            or projection["origin"].get("attempt") != origin["row"]["attempt"]
            or projection["origin"].get("platform") != "linux/amd64"
            or projection["origin"].get("binary_sha256") != state["freeze"]["binary_sha256"]
            or current["run_nonce"] == projection["origin"]["run_nonce"]):
        raise replay.ReplayError("origin 原字节漂移或与本轮 runtime 混同")
    destination = Path(projection["output_root"])
    if (destination.resolve() != destination or ".." in destination.parts
            or not destination.is_relative_to(data / "runs") or destination.name != projection["probe_id"]):
        raise replay.ReplayError("复制目的不在本轮 runs 或 probe 坐标不符")
    return origin


def _argument(argv: Any, option: str) -> str:
    """步骤参数必须唯一存在；不容许重复参数改变实际选中的目录或版本事实。"""

    values = list(argv)
    if values.count(option) != 1 or values.index(option) + 1 >= len(values):
        raise replay.ReplayError(f"录制投影缺少唯一参数：{option}")
    return str(values[values.index(option) + 1])


def control_manifest(state: Mapping[str, Any], projection: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """真实 v5 重验历史 R/P 后生成控制封套，官方与候选共用这一份实现。

    原执行身份仍由原清单与原宿主收据证明；本次运行身份只表示隔离 fixture 的复制控制动作。历史目录逐字段与
    当前 v5 目录比对，只有登记的覆盖字段可以变化；advisor 模型从摘要绑定的原 debug 字节解析，绝不从期望值填入。
    判定器、冻结版本事实、历史附件和两份宿主收据全部写入投影说明，保留原摘要与当前摘要不同这一事实。
    """

    from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

    data = Path(state["data_root"])
    if (state.get("fixture_only") is not True or state.get("purpose") != "regression_fixture"
            or state.get("target_version") != VERSION or "staging" not in data.parts):
        raise replay.ReplayError("控制封套仅限隔离 fixture_only 来源")
    manifest = json.loads(json.dumps(origin["manifest"]))
    probe_id = projection["probe_id"]
    if (manifest.get("status") != "complete" or (manifest.get("m_binding") or {}).get("complete") is not True
            or (manifest.get("secret_scan") or {}).get("passed") is not True
            or manifest.get("cleanup") != {"relay_stopped": True, "hosts_restored": True}
            or not isinstance(manifest.get("upstream"), dict)):
        raise replay.ReplayError("历史 relay 来源未具备真实完成、秘密扫描、完整 M 与恢复事实")
    if manifest.get("fw_f_v4_probe") != probe_id or manifest.get("fw_f_v5_probe") is not None:
        raise replay.ReplayError("控制封套只接受登记的原始 v4 录制")
    index, row = origin["index"], origin["row"]
    root = Path(index["root"])
    manifest_relative = f"{row['attempt']}/relay-manifest.json"
    original_manifest_binding = {"path": str(root / manifest_relative), **index["files"][manifest_relative]}
    original_manifest = bound(original_manifest_binding, root=root)
    if original_manifest != origin["manifest_path"] or read(original_manifest) != origin["manifest"]:
        raise replay.ReplayError("历史 relay 清单与已验证的来源映射不同")
    catalog_relative = f"{row['source_id']}/scenario-catalog.json"
    original_catalog_binding = {"path": str(root / catalog_relative), **index["files"][catalog_relative]}
    catalog_path = bound(original_catalog_binding, root=root)
    catalog = read(catalog_path)
    definitions = [item for item in catalog.get("probes", []) if item.get("probe_id") == probe_id]
    if catalog.get("target_version") != VERSION or len(definitions) != 1:
        raise replay.ReplayError("历史目录未唯一绑定本次 probe 与版本")
    v5.validate_probe_catalog()
    probe = v5.get_probe(probe_id)
    current_definition = json.loads(json.dumps(dataclasses.asdict(probe)))
    base_definition = json.loads(json.dumps(dataclasses.asdict(v5.v4.get_probe(probe_id))))
    changed = {key for key in current_definition if current_definition[key] != definitions[0].get(key)}
    if (definitions[0] != base_definition or changed != set(v5.PROBE_OVERRIDES.get(probe_id, {}))):
        raise replay.ReplayError("历史与 v5 目录差异超出登记覆盖字段")

    inputs = state.get("inputs", {}).get("bindings", {})
    if not {"scenarios", "version_facts"} <= set(inputs) or not isinstance(projection.get("v5_catalog"), Mapping):
        raise replay.ReplayError("控制封套缺少冻结场景、版本事实或判定器绑定")
    scenarios_path = bound(inputs["scenarios"], root=data)
    facts_path = bound(inputs["version_facts"], root=data)
    scenarios = read(scenarios_path)
    if (scenarios.get("schema_version") != "claude-upgrade-scenarios/v1" or scenarios.get("claude_version") != VERSION
            or scenarios.get("probe_catalog", {}).get("schema_version") != v5.SCHEMA_CATALOG
            or scenarios.get("probe_catalog", {}).get("path") != "tools/official_client_capture/capturelib/claude_fw_f_v5.py"
            or scenarios.get("version_facts", {}).get("sha256") != inputs["version_facts"]["sha256"]):
        raise replay.ReplayError("冻结场景与 v5 版本事实绑定不符")
    phase = projection.get("side", "official")
    jobs = [job for job in scenarios.get("capture_jobs", [])
            if job.get("id") == projection["job_id"] and job.get("phase") == phase]
    matched = OPERATION.fullmatch(str(projection["operation"]))
    number = int(matched.group(2)) if matched is not None else 0
    if len(jobs) != 1 or not 0 < number <= len(jobs[0].get("steps", [])):
        raise replay.ReplayError("投影步骤没有绑定冻结的客户端阶段与 Job")
    frozen_step = jobs[0]["steps"][number - 1]
    if frozen_step.get("probe_id") != probe_id:
        raise replay.ReplayError("投影 probe 与冻结步骤不同")
    for argv in (projection["argv"], frozen_step["argv"]):
        if (any(option in argv for option in ("--fw-f-v4-probe", "--fw-f-probe"))
                or _argument(argv, "--fw-f-v5-probe") != probe_id
                or _argument(argv, "--fw-f-v5-version-facts-sha256") != inputs["version_facts"]["sha256"]):
            raise replay.ReplayError("投影目录参数与冻结 v5 步骤不同")
    catalog_binding = projection["v5_catalog"]
    current_catalog = bound(catalog_binding["file"], root=data)
    if (_argument(projection["argv"], "--fw-f-v5-catalog") != catalog_binding["argument"]
            or replay._digest(current_catalog) != replay._digest(Path(v5.__file__))):
        raise replay.ReplayError("投影 v5 判定器与实际加载的工具不同")
    facts, facts_binding = v5.load_version_facts(facts_path, inputs["version_facts"]["sha256"], expected_version=VERSION)

    # 先复验所有允许读取的附件摘要，再让真实判定器读它们；未登记的连接文件不能混进其 glob。
    source_files = {}
    for relative, item in sorted(index["files"].items()):
        if relative.startswith(row["attempt"] + "/"):
            path = bound({"path": str(root / relative), **item}, root=root)
            source_files[Path(relative).relative_to(row["attempt"]).as_posix()] = binding(path)
    relay_root = root / row["attempt"] / "relay"
    replay._regular(relay_root, root, directory=True)
    if {"relay/" + path.name for path in relay_root.iterdir()} != {name for name in source_files if name.startswith("relay/")}:
        raise replay.ReplayError("历史 relay 读集含未登记文件")
    summary = json.loads(json.dumps(manifest.get("scenario_result")))
    if (not isinstance(summary, dict) or summary.get("schema_version") != v5.v4.SCHEMA_RESULT
            or summary.get("probe_id") != probe_id or summary.get("valid") is not True
            or not isinstance(summary.get("inner_result"), dict)):
        raise replay.ReplayError("历史录制缺少已绑定的真实场景结果")
    producer = summary.get("catalog_producer", {})
    old_sources = manifest["runtime"]["capture_tools"]["execution_sources"]
    if (producer.get("path") != "tools/official_client_capture/capturelib/claude_fw_f_v4.py"
            or not any(item.get("path") == "capturelib/claude_fw_f_v4.py" and item.get("sha256") == producer.get("sha256")
                       for item in old_sources.get("files", []))):
        raise replay.ReplayError("历史场景结果与原执行源身份断裂")
    if "tool.advisor" in probe.dimensions and probe.require_debug_log:
        if "results/debug.log" not in source_files:
            raise replay.ReplayError("advisor 重验缺少已绑定的原 debug 日志")
        summary["inner_result"]["debug_declarations"] = v5._debug_declarations(relay_root.parent / "results", facts)
    dimension = v5.validate_v5_probe_evidence(
        probe_id=probe_id, relay_root=relay_root, scenario_summary=summary,
        relay_integrity=manifest["relay_integrity"], pcap_receipt=manifest["tls_p_channel"], version_facts=facts,
    )
    if dimension["result"] != "passed":
        raise replay.ReplayError(f"历史录制未通过真实 v5 重验：{probe_id}；{dimension['failed_dimensions']}；{dimension['wire_contract']['failures']}")
    if dimension["actual_wire_inventory"] != manifest["dimension_evidence"]["actual_wire_inventory"]:
        raise replay.ReplayError("原 wire inventory 与实际字节重解析结果不同")

    runtime_path = bound(projection["runtime"]["receipt"], root=data)
    runtime = read(runtime_path)
    server, bundle = runtime.get("docker_server", {}), runtime.get("capture_source_bundle", {})
    if (not isinstance(server, Mapping) or not isinstance(bundle, Mapping)
            or server.get("os") != "linux" or server.get("arch") != "arm64"
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(runtime.get("runtime_image_id", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(bundle.get("sha256", "")))):
        raise replay.ReplayError("本次控制 runtime 缺少完整 ARM64 镜像与执行源事实")
    if (runtime.get("run_nonce") != projection["runtime"]["run_nonce"]
            or runtime.get("run_nonce") == projection["origin"]["run_nonce"]):
        raise replay.ReplayError("控制封套的本次 nonce 与来源混同或漂移")
    host_binding = {
        "schema_version": runtime["schema_version"], "path": str(runtime_path),
        "sha256": projection["runtime"]["receipt"]["sha256"], "run_nonce": runtime["run_nonce"],
        **{name: runtime[name] for name in ("container", "docker_server", "runtime_image_reference", "runtime_image_id", "repo_digest_verified")},
        "capture_source_bundle_sha256": bundle["sha256"],
        "container_runtime_binding": {"verified": True, "container_id": runtime["container"]["id"]},
    }
    manifest.update(
        side=phase, run_id=_argument(projection["argv"], "--run-id"), capture_mode="fw-f-v5",
        fw_f_v4_probe=None, fw_f_v5_probe=probe_id,
        fw_f_v5_catalog={"path": str(current_catalog), "sha256": catalog_binding["file"]["sha256"]},
        fw_f_v5_version_facts={**facts_binding, "facts": facts}, dimension_evidence=dimension,
        fixture_only=True, formal_version_semantics_certified=False,
    )
    manifest["runtime"].update(runtime_image_reference=runtime["runtime_image_reference"], host_runtime_receipt=host_binding,
                               capture_tools={"execution_sources": bundle})
    manifest["fixture_projection"] = {
        "schema_version": CONTROL_PROJECTION_SCHEMA, "purpose": "recorded_control_source_projection", "fixture_only": True,
        "formal_version_semantics_certified": False, "current_client_executed": False,
        "original_manifest": original_manifest_binding, "original_catalog": original_catalog_binding,
        "original_execution_sources": old_sources, "runtime": projection["runtime"], "origin": projection["origin"],
        "current_scenarios": inputs["scenarios"], "version_facts": inputs["version_facts"], "v5_catalog": catalog_binding,
        "v5_validator": binding(Path(v5.__file__)), "v4_validator": binding(Path(v5.v4.__file__)),
        "catalog_changed_fields": sorted(changed), "source_files": source_files,
        "v5_dimension_result": dimension["result"],
    }
    # 判定器读完后再核一遍摘要，避免重验期间的字节变化被写进控制封套。
    for item in (*source_files.values(), original_catalog_binding, inputs["scenarios"], inputs["version_facts"],
                 catalog_binding["file"], projection["runtime"]["receipt"], projection["origin"]["receipt"]):
        bound(item)
    return manifest


def copy_registered(state_path: Path, digest: str, projection_path: Path, projection_digest: str) -> None:
    """真实监督器执行的动作体：只复制外持摘要绑定的唯一步骤；无 shell、Docker 或网络调用。"""

    state = load_state(state_path, digest)
    replay._regular(projection_path, Path(state["data_root"]))
    if replay._digest(projection_path)["sha256"] != projection_digest:
        raise replay.ReplayError("步骤投影外持摘要漂移")
    projection = read(projection_path)
    origin = verify_projection(state, projection)
    manifest = control_manifest(state, projection, origin)
    index, row = origin["index"], origin["row"]
    destination = Path(projection["output_root"])
    if destination.exists() or destination.is_symlink():
        raise replay.ReplayError("复制目的已存在，禁止重复派发")
    destination.mkdir(parents=True, mode=0o700)
    copied = []
    for relative, item in sorted(index["files"].items()):
        if not relative.startswith(row["attempt"] + "/"):
            continue
        source = bound({"path": str(Path(index["root"]) / relative), **item}, root=Path(index["root"]))
        suffix = Path(relative).relative_to(row["attempt"])
        target = destination / (ORIGIN_MANIFEST_FILENAME if suffix.as_posix() == "relay-manifest.json" else suffix)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        if replay._digest(target) != item:
            raise replay.ReplayError("复制结果与原始字节不同")
        copied.append({"path": str(target), **item})
    manifest["fixture_projection"]["copied_origin_manifest"] = binding(destination / ORIGIN_MANIFEST_FILENAME)
    current_manifest = write(destination / "relay-manifest.json", manifest)
    verify_projection(state, projection)
    write(projection_path.with_suffix(".copied.json"), {"fixture_only": True, "operation": projection["operation"],
          "purpose": "recorded_control_source_projection", "formal_version_semantics_certified": False,
          "projection": binding(projection_path), "runtime": projection["runtime"], "origin": projection["origin"],
          "files": copied, "control_manifest": binding(current_manifest)})


class SourceAdapter:
    """测试来源接缝；真实 runtime 签发器、relay checker 和监督器执行体均保留。"""

    def __init__(self, state_path: Path, digest: str) -> None:
        self.path, self.digest = state_path, digest
        self.state = load_state(state_path, digest)
        self.pending: dict[str, tuple[list[str], Path, str]] = {}

    def issue(self, original: Any, context: Any, **options: Any) -> tuple[dict[str, str], dict[str, Any]]:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan

        current_values, current_binding = original(context, **options)
        if context.mode != capture.MODE_FORMAL:
            return current_values, current_binding
        job_id, probe, number = options["job_id"], options["probe_id"], options["step_index"]
        allowed = self.state["jobs"].get(job_id, [])
        if number > len(allowed) or allowed[number - 1] != probe:
            raise replay.ReplayError("未登记的正式 Job 步骤")
        source = origin_probe(self.state, probe)
        runtime_path = context.attempt_root / current_binding["path"]
        old_path = runtime_path.with_name(runtime_path.stem + ".origin.json")
        with old_path.open("xb") as handle:
            handle.write(source["receipt_path"].read_bytes())
        old_path.chmod(0o600)
        old = source["receipt"]
        # 本次 Job/checker 一律消费本次签发收据；旧收据只作为独立来源附件，不替本次复制控制身份背书。
        values = current_values
        jobs = capture.claude_upgrade._campaign_jobs(context.campaign_dir, context.manifest, "official")
        job = next(item for item in jobs if item.job_id == job_id)
        raw_step = job.steps[number - 1]
        step = plan.bind_step_runtime(raw_step, {name: values[name] for name in raw_step["runtime_variables"]})
        argv = [str(value) for value in step["argv"]]
        role, _container = capture._step_route(argv, context.layout)
        prefix = "synthetic-job" if role == capture.ROLE_SYNTHETIC else "job"
        operation = f"{prefix}:{job_id}:step-{number}:attempt-{options['run_index']}"
        argv = list(capture._host_step_argv(argv, context.layout))
        coordinates = capture._relay_step_coordinates(argv)
        output = capture._host_path(coordinates["output_root"], context.layout)
        projection = {"schema_version": PROJECTION_SCHEMA, "fixture_only": True,
                      "operation": operation, "job_id": job_id, "probe_id": probe, "argv": argv,
                      "output_root": str(output),
                      "runtime": {"receipt": binding(runtime_path), "run_nonce": current_binding["run_nonce"],
                                  "reservation": binding(context.attempt_root / "reservation.json"), "attempt_id": context.attempt_id,
                                  "container_name": current_binding["container_name"], "container_id": current_binding["container_id"],
                                  "runtime_image": context.layout.runtime_image},
                      "origin": {"receipt": binding(old_path), "run_nonce": old["run_nonce"],
                                 "source_id": source["row"]["source_id"], "attempt": source["row"]["attempt"],
                                 "platform": "linux/amd64", "binary_sha256": self.state["freeze"]["binary_sha256"]}}
        catalog_argument = context.layout.capture_tool_root / "capturelib/claude_fw_f_v5.py"
        catalog_path = capture._host_path(catalog_argument, context.layout)
        projection["v5_catalog"] = {"file": binding(catalog_path), "argument": str(catalog_argument)}
        verify_projection(self.state, projection)
        path = write(runtime_path.with_suffix(".projection.json"), projection)
        if operation in self.pending:
            raise replay.ReplayError("重复的 operation 预约")
        self.pending[operation] = (argv, path, replay._digest(path)["sha256"])
        return current_values, current_binding

    def command(self, original: Any, client: Any, argv: Any, *, operation: str, **options: Any) -> Any:
        if operation.startswith(("job:", "synthetic-job:")):
            if operation not in self.pending:
                raise replay.ReplayError("未知复制 operation，拒绝执行")
            expected, path, digest = self.pending.pop(operation)
            if list(argv) != expected or options.get("job_id") != OPERATION.fullmatch(operation).group(1):
                raise replay.ReplayError("复制命令与精确 operation／Job／probe 白名单不符")
            verify_projection(self.state, read(path))
            script = ("import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;"
                      "from tools.official_client_capture.tests.claude_recorded_chain_source import copy_registered;"
                      "copy_registered(Path(sys.argv[2]),sys.argv[3],Path(sys.argv[4]),sys.argv[5])")
            argv = [sys.executable, "-S", "-c", script, str(Path(__file__).resolve().parents[3]),
                    str(self.path), self.digest, str(path), digest]
        return original(client, argv, operation=operation, **options)

    @contextlib.contextmanager
    def patched(self):
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_campaign as campaign
        from tools.official_client_capture import claude_upgrade_freeze as freeze
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        issue, command = capture._issue_host_runtime_receipt, supervisor.SupervisorClient.run_command
        production_verify = freeze.verify_freeze_receipt
        production_identity = campaign.official_identity_from_freeze
        registered = Path(self.state["freeze"]["files"]["freeze.json"]["path"])

        def verify(path: Path, **options: Any) -> dict[str, Any]:
            if Path(path) != registered:
                return production_verify(path, **options)
            return verify_origin_freeze(self.state, path, **options)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(freeze, "verify_freeze_receipt", verify))
            stack.enter_context(mock.patch.object(campaign, "official_identity_from_freeze", lambda path: origin_identity(self.state, path)
                                                 if Path(path) == registered else production_identity(path)))
            stack.enter_context(mock.patch.object(capture, "_issue_host_runtime_receipt", lambda context, **kw: self.issue(issue, context, **kw)))
            stack.enter_context(mock.patch.object(supervisor.SupervisorClient, "run_command", lambda client, argv, **kw: self.command(command, client, argv, **kw)))
            yield self
