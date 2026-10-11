"""录制官方 R/P 原件 → 本次候选控制来源投影。

只用于已隔离的 fixture_only 世界。正式预约、宿主运行凭据、Job 白名单和历史来源同时重验，复制动作仍由真实监督器
执行。完整官方原件在独立目录逐字保留；候选 R 只投影明确的网关出站，并保留请求／响应的原字节范围映射。
P 保留绑定的官方 TLS 原件，只作为明确的夹具观察，绝不冒称本轮网关的 TLS 取证。
它不表示真实候选服务已发送这些字节；真实候选 run、seal、评估和 Store 消费仍执行正式合同。
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay

GATEWAY_PROJECTION_SCHEMA = "claude-recorded-gateway-byte-projection/v1"
ORIGINAL_DIRECTORY = "recorded-official-originals"
LOCAL_REQUESTS = frozenset({
    "HEAD /api/hello", "GET /api/claude_code/policy_limits", "GET /api/claude_code/settings",
    "GET /api/oauth/profile", "GET /v1/mcp_servers",
})


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _response_spans(raw: bytes, methods: list[str], *, intervention: str | None) -> list[dict[str, Any]]:
    """生产者与读侧共用固定字节边界实现；旧 fixture 异常类型保持不变。"""

    from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

    try:
        return recorded.response_spans(raw, methods, intervention=intervention)
    except recorded.RecordedEvaluationError as error:
        raise replay.ReplayError(str(error)) from error


def _project_segments(segments: list[dict[str, Any]], ranges: dict[str, list[tuple[int, int]]]) -> list[dict[str, Any]]:
    """生产者与读侧共用固定字节边界实现；旧 fixture 异常类型保持不变。"""

    from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

    try:
        return recorded.project_segments(segments, ranges)
    except recorded.RecordedEvaluationError as error:
        raise replay.ReplayError(str(error)) from error


def gateway_material(state: Mapping[str, Any], projection: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """生产者与读侧复算同一固定投影，既有v1混合字节用例与异常类型保持不变。"""

    from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

    try:
        return recorded.gateway_material(state, projection, origin)
    except recorded.RecordedEvaluationError as error:
        raise replay.ReplayError(str(error)) from error


def verify_gateway_material(state: Mapping[str, Any], projection: Mapping[str, Any], origin: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    """从原件重新推导范围与摘要，再核输出闭集；修改字节或映射都不能靠重写一份摘要逃过。"""

    expected = gateway_material(state, projection, origin)
    receipt = source.read(destination / "gateway-byte-projection.json")
    if receipt != expected["receipt"]:
        raise replay.ReplayError("网关投影范围、响应配对或原件绑定漂移")
    for item in receipt["projected_files"]:
        source.bound({**item, "path": str(destination / item["path"])}, root=destination)
    if {"relay/" + path.name for path in (destination / "relay").iterdir()} != {name for name in expected["files"] if name.startswith("relay/")}:
        raise replay.ReplayError("候选投影 relay 输出闭集漂移")
    original_root = destination / ORIGINAL_DIRECTORY
    prefix = origin["row"]["attempt"] + "/"
    original_files = {relative[len(prefix):]: item for relative, item in origin["index"]["files"].items() if relative.startswith(prefix)}
    if {path.relative_to(original_root).as_posix() for path in original_root.rglob("*") if path.is_file() or path.is_symlink()} != set(original_files):
        raise replay.ReplayError("候选投影的原件副本闭集漂移")
    for name, item in original_files.items():
        source.bound({"path": str(original_root / name), **item}, root=original_root)
    return receipt


def verify_candidate_projection(state: Mapping[str, Any], projection: Mapping[str, Any]) -> dict[str, Any]:
    """在官方来源的双绑定之外，核对冻结候选分母、网关变量与本次真实候选 reservation。"""

    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import claude_upgrade_plan as plan

    if projection.get("side") != "candidate":
        raise replay.ReplayError("候选控制来源缺少独立 side")
    candidate_jobs = state["inputs"]["candidate_jobs"]
    origin = source.verify_projection({**state, "jobs": candidate_jobs}, projection)
    data = Path(state["data_root"])
    runtime = source.read(source.bound(projection["runtime"]["receipt"], root=data))
    server, bundle = runtime.get("docker_server"), runtime.get("capture_source_bundle")
    if (not isinstance(server, Mapping) or server.get("os") != "linux" or server.get("arch") != "arm64"
            or not capture.IMAGE_ID_RE.fullmatch(str(runtime.get("runtime_image_id", "")))
            or not isinstance(bundle, Mapping) or not capture.SHA256_RE.fullmatch(str(bundle.get("sha256", "")))):
        raise replay.ReplayError("本次候选 runtime 缺少完整 Docker、ARM64 镜像或执行源事实")
    reservation_path = source.bound(projection["runtime"]["reservation"], root=data)
    reservation = source.read(reservation_path)
    candidate_id = reservation.get("candidate_id")
    if (not isinstance(candidate_id, str) or reservation_path.parent.parents[1].name != candidate_id
            or reservation.get("phase") != "candidate" or projection.get("candidate_id") != candidate_id):
        raise replay.ReplayError("候选投影不属于本次真实候选预约")
    coordinates = capture._relay_step_coordinates(projection["argv"])
    configuration = state["inputs"]["candidate_configuration"]
    expected = {
        "run_id": coordinates["run_id"], "probe_id": coordinates["probe_id"], "model": coordinates["model"],
        "account_id": int(configuration["claude_account_id"]), "api_key_id": int(configuration["api_key_id"]),
        "base_url": "http://" + configuration["service_container"] + ":8080",
        "trigger": plan.candidate_trigger(projection["probe_id"]),
        "relay_port": int(plan.CANDIDATE_RELAY_PORT),
        "live": coordinates["upstream_ip"] is not None,
    }
    if (projection.get("candidate") != expected or coordinates["probe_id"] != projection["probe_id"]
            or coordinates["expected_version"] != source.VERSION
            or coordinates["expected_sha256"] != state["freeze"]["binary_sha256"]
            or coordinates["output_root"] != projection["output_root"]):
        raise replay.ReplayError("候选投影的 argv、冻结账号或运行坐标不符")
    return origin


def _candidate_manifest(state: Mapping[str, Any], projection: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """保留历史事实与字节目录，运行身份只指向本轮凭据；夹具来源证据放独立文件。"""

    from tools.official_client_capture import claude_upgrade_evidence as evidence

    # 官方与候选的目录兼容、原始 wire/debug 重验和双身份封套必须走同一实现。
    manifest = source.control_manifest(state, projection, origin)
    candidate = projection["candidate"]
    manifest.update(side="candidate", run_id=candidate["run_id"])
    manifest["candidate_gateway"] = {
        "base_url": candidate["base_url"], "account_id": candidate["account_id"], "api_key_id": candidate["api_key_id"],
        "trigger": candidate["trigger"], "client_authentication": evidence.CANDIDATE_CLIENT_AUTHENTICATION,
        "oauth_credentials_read": False,
    }
    manifest["upstream"].update(relay_mode="connect", relay_listen_port=candidate["relay_port"])
    manifest["hosts_recovery"] = dict(evidence.CANDIDATE_HOSTS_RECOVERY)
    manifest["m_binding"] = {"complete": True, "requirements": {key: True for key in sorted(evidence.CANDIDATE_M_REQUIREMENTS)}}
    # 这里只重验原官方来源；候选完整性与维度观测须在实际投影落盘后由正式消费者重新生成。
    manifest["fixture_projection"]["original_v5_dimension_evidence"] = copy.deepcopy(manifest["dimension_evidence"])
    return manifest


def copy_registered(state_path: Path, digest: str, projection_path: Path, projection_digest: str) -> int:
    """监督器的真实动作体：原件保持不变，候选投影和逐文件来源绑定只写一次。"""

    from tools.official_client_capture import claude_upgrade_evidence as evidence
    from tools.official_client_capture import claude_fw_e_relay as relay_capture
    from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

    state = source.load_state(state_path, digest)
    source.bound({"path": str(projection_path), **replay._digest(projection_path)}, root=Path(state["data_root"]))
    if source.binding(projection_path)["sha256"] != projection_digest:
        raise replay.ReplayError("候选投影的外持摘要漂移")
    projection = source.read(projection_path)
    origin = verify_candidate_projection(state, projection)
    from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

    policy = recorded.optional_policy(state)
    if policy is not None:
        # 分域必须在任何输出落盘前闭合；旧无 policy 输入继续使用原纯 gateway 语义。
        recorded.connection_domains(state, origin)
    manifest_payload = _candidate_manifest(state, projection, origin)
    material = gateway_material(state, projection, origin)
    source.recovery_control(state)
    index, row = origin["index"], origin["row"]
    destination = Path(projection["output_root"])
    if destination.exists() or destination.is_symlink():
        raise replay.ReplayError("候选投影目的已存在，禁止覆盖")
    destination.mkdir(parents=True, mode=0o700)
    files = []
    for relative, item in sorted(index["files"].items()):
        if not relative.startswith(row["attempt"] + "/"):
            continue
        original = source.bound({"path": str(Path(index["root"]) / relative), **item}, root=Path(index["root"]))
        suffix = Path(relative).relative_to(row["attempt"])
        target = destination / ORIGINAL_DIRECTORY / suffix
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as handle:
            handle.write(original.read_bytes())
        target.chmod(0o600)
        if replay._digest(target) != item:
            raise replay.ReplayError("候选来源 R/P 或原始 relay 附件字节漂移")
        files.append({"original_relative": relative, "copied": source.binding(target)})
    for relative, raw in material["files"].items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as handle:
            handle.write(raw)
        target.chmod(0o600)
    projection_receipt = source.write(destination / "gateway-byte-projection.json", material["receipt"])
    recorded_binding = None
    if policy is not None:
        contract_path = source.write(destination / recorded.PROBE_FILENAME,
                                     recorded.build_probe_contract(state_path, projection_path, destination))
        recorded_binding = source.binding(contract_path)
    probe = v5.get_probe(projection["probe_id"])
    integrity = relay_capture._validate_relay_integrity(
        destination / "relay", relay_mode="connect", candidate=True, synthetic_plan=probe.response_plan,
        synthetic_profile="claude-fw-f-v4" if probe.response_plan else None,
        message_request_expectation=probe.message_request_expectation, target_host=probe.target_host,
    )
    dimension = relay_capture._candidate_wire_record(
        v5, projection["probe_id"], destination / "relay", manifest_payload["scenario_result"],
        integrity, manifest_payload["tls_p_channel"], manifest_payload["fw_f_v5_version_facts"]["facts"],
    )
    manifest_payload.update(relay_integrity=integrity, dimension_evidence=dimension, candidate_observation={
        "client_outcome_valid": manifest_payload["scenario_result"].get("valid") is True,
        "message_request_expectation_met": integrity.get("message_request_expectation_met"),
        "synthetic_plan_closed": integrity.get("synthetic_plan_closed"), "dimension_result": dimension["result"],
    })
    manifest_payload["fixture_projection"].update(
        copied_origin_manifest=source.binding(destination / ORIGINAL_DIRECTORY / "relay-manifest.json"),
        gateway_byte_projection=source.binding(projection_receipt),
    )
    if recorded_binding is not None:
        manifest_payload["fixture_projection"][recorded.PROBE_KEY] = recorded_binding
    manifest_payload["artifacts"] = relay_capture.inventory_artifacts(destination)
    manifest = source.write(destination / "relay-manifest.json", manifest_payload)
    return_code, decision = source.record_candidate_action(state, projection)
    candidate = projection["candidate"]
    restoration = source.write(destination.parent / evidence.CANDIDATE_RESTORATION_DIRNAME / (projection["probe_id"] + ".json"), {
        "schema_version": evidence.CANDIDATE_RESTORATION_SCHEMA, "status": "capture_failed" if return_code else "complete",
        "run_id": candidate["run_id"], "probe_id": candidate["probe_id"], "account_id": candidate["account_id"],
        "api_key_id": candidate["api_key_id"], "trigger": candidate["trigger"], "relay_port": candidate["relay_port"],
        "capture_exit_code": return_code, "runtime_secrets_removed": True,
        **{key: True for key in evidence.CANDIDATE_RESTORATION_EQUALITIES},
    })
    trigger = None
    if candidate["trigger"] == evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH:
        trigger = source.write(destination / evidence.CANDIDATE_TRIGGER_RELATIVE, {
            "schema_version": evidence.CANDIDATE_TRIGGER_SCHEMA, "trigger": evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH,
            "account_id": candidate["account_id"], "valid": True,
        })
    verify_candidate_projection(state, projection)
    verify_gateway_material(state, projection, origin, destination)
    if recorded_binding is not None:
        recorded.verify_probe_contract(recorded_binding, Path(state["data_root"]))
    # 读取真实候选视图，确保后续封存消费者看见投影字节解析出的连接，而不是原官方启动连接。
    evidence.candidate_probe_view(destination, base=Path(state["data_root"]))
    source.write(projection_path.with_suffix(".copied.json"), {
        "fixture_only": True, "purpose": "candidate_control_source_projection", "operation": projection["operation"],
        "projection": source.binding(projection_path), "runtime": projection["runtime"], "origin": projection["origin"],
        "files": files, "gateway_byte_projection": source.binding(projection_receipt),
        "candidate_manifest": source.binding(manifest), "restoration": source.binding(restoration),
        "gateway_trigger": source.binding(trigger) if trigger else None, "formal_version_semantics_certified": False,
        **({"recovery_action": decision} if decision is not None else {}),
    })
    return return_code


class SourceAdapter(source.SourceAdapter):
    """官方复制沿用旧适配器；候选只增加明确来源投影，不替换产品候选校验或阶段判定。"""

    def __init__(self, state_path: Path, digest: str) -> None:
        super().__init__(state_path, digest)
        self.candidate_pending: dict[str, tuple[list[str], Path, str]] = {}

    def issue(self, original: Any, context: Any, **options: Any):
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan

        job_id, probe, number = options["job_id"], options["probe_id"], options["step_index"]
        allowed = self.state["inputs"]["candidate_jobs"].get(job_id)
        if allowed is None:
            return super().issue(original, context, **options)
        if context.mode != capture.MODE_FORMAL or number > len(allowed) or allowed[number - 1] != probe:
            raise replay.ReplayError("未登记的正式候选步骤")
        values, current_binding = original(context, **options)
        origin = source.origin_probe(self.state, probe)
        runtime_path = context.attempt_root / current_binding["path"]
        old_path = runtime_path.with_name(runtime_path.stem + ".origin.json")
        with old_path.open("xb") as handle:
            handle.write(origin["receipt_path"].read_bytes())
        old_path.chmod(0o600)
        candidate_id = context.attempt_root.parents[1].name
        jobs = capture.claude_upgrade._campaign_jobs(context.campaign_dir, context.manifest, "candidate", candidate_id=candidate_id)
        job = next(item for item in jobs if item.job_id == job_id)
        raw_step = job.steps[number - 1]
        step = plan.bind_step_runtime(raw_step, {name: values[name] for name in raw_step["runtime_variables"]})
        argv = list(capture._host_step_argv([str(value) for value in step["argv"]], context.layout))
        role, _container = capture._step_route(argv, context.layout)
        prefix = "synthetic-job" if role == capture.ROLE_SYNTHETIC else "job"
        operation = f"{prefix}:{job_id}:step-{number}:attempt-{options['run_index']}"
        coordinates = capture._relay_step_coordinates(argv)
        output = capture._host_path(coordinates["output_root"], context.layout)
        configuration = self.state["inputs"]["candidate_configuration"]
        projection = {
            "schema_version": source.PROJECTION_SCHEMA, "fixture_only": True, "side": "candidate", "candidate_id": candidate_id,
            "operation": operation, "job_id": job_id, "probe_id": probe, "argv": argv, "output_root": str(output),
            "runtime": {"receipt": source.binding(runtime_path), "run_nonce": current_binding["run_nonce"],
                        "reservation": source.binding(context.attempt_root / "reservation.json"), "attempt_id": context.attempt_id,
                        "container_name": current_binding["container_name"], "container_id": current_binding["container_id"],
                        "runtime_image": context.layout.runtime_image},
            "origin": {"receipt": source.binding(old_path), "run_nonce": origin["receipt"]["run_nonce"],
                       "source_id": origin["row"]["source_id"], "attempt": origin["row"]["attempt"],
                       "platform": "linux/amd64", "binary_sha256": self.state["freeze"]["binary_sha256"]},
            "candidate": {"run_id": coordinates["run_id"], "probe_id": coordinates["probe_id"], "model": coordinates["model"],
                          "account_id": int(configuration["claude_account_id"]), "api_key_id": int(configuration["api_key_id"]),
                          "base_url": "http://" + configuration["service_container"] + ":8080",
                          "trigger": plan.candidate_trigger(probe), "relay_port": int(plan.CANDIDATE_RELAY_PORT),
                          "live": coordinates["upstream_ip"] is not None},
        }
        catalog_argument = context.layout.capture_tool_root / "capturelib/claude_fw_f_v5.py"
        catalog_path = capture._host_path(catalog_argument, context.layout)
        projection["v5_catalog"] = {"file": source.binding(catalog_path), "argument": str(catalog_argument)}
        verify_candidate_projection(self.state, projection)
        path = source.write(runtime_path.with_suffix(".candidate-projection.json"), projection)
        if operation in self.candidate_pending:
            raise replay.ReplayError("候选 operation 重复预约")
        self.candidate_pending[operation] = (argv, path, source.binding(path)["sha256"])
        return values, current_binding

    def command(self, original: Any, client: Any, argv: Any, *, operation: str, **options: Any):
        if operation not in self.candidate_pending:
            return super().command(original, client, argv, operation=operation, **options)
        expected, path, digest = self.candidate_pending.pop(operation)
        if list(argv) != expected or options.get("job_id") != source.OPERATION.fullmatch(operation).group(1):
            raise replay.ReplayError("候选命令与冻结 operation/Job 白名单不符")
        verify_candidate_projection(self.state, source.read(path))
        script = ("import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;"
                  "from tools.official_client_capture.tests.claude_recorded_candidate_source import copy_registered;"
                  "raise SystemExit(copy_registered(Path(sys.argv[2]),sys.argv[3],Path(sys.argv[4]),sys.argv[5]))")
        command = [sys.executable, "-S", "-c", script, str(Path(__file__).resolve().parents[3]),
                   str(self.path), self.digest, str(path), digest]
        return original(client, command, operation=operation, **options)
