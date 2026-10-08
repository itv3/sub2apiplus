"""录制官方 R/P 原件 → 本次候选控制来源投影。

只用于已隔离的 fixture_only 世界。正式预约、宿主运行凭据、Job 白名单和历史来源同时重验，复制动作仍由真实监督器
执行。R/P 及其附件逐字复制，原始 relay 清单另存并绑定；候选清单只重建本次 runtime/gateway 的控制封套。
它不表示真实候选服务已发送这些字节；真实候选 run、seal、评估和 Store 消费仍执行正式合同。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay


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

    manifest = json.loads(json.dumps(origin["manifest"]))
    if (manifest.get("status") != "complete" or (manifest.get("m_binding") or {}).get("complete") is not True
            or (manifest.get("secret_scan") or {}).get("passed") is not True
            or manifest.get("cleanup") != {"relay_stopped": True, "hosts_restored": True}
            or not isinstance(manifest.get("upstream"), dict)):
        raise replay.ReplayError("历史 relay 来源未具备真实完成、秘密扫描、完整 M 与恢复事实")
    candidate = projection["candidate"]
    runtime_path = source.bound(projection["runtime"]["receipt"], root=Path(state["data_root"]))
    runtime = source.read(runtime_path)
    current = projection["runtime"]
    host_binding = {
        "schema_version": runtime["schema_version"], "path": str(runtime_path),
        "sha256": current["receipt"]["sha256"], "run_nonce": current["run_nonce"],
        **{name: runtime[name] for name in ("container", "docker_server", "runtime_image_reference", "runtime_image_id", "repo_digest_verified")},
        "capture_source_bundle_sha256": runtime["capture_source_bundle"]["sha256"],
        "container_runtime_binding": {"verified": True, "container_id": runtime["container"]["id"]},
    }
    manifest.update(side="candidate", run_id=candidate["run_id"])
    manifest["runtime"].update(runtime_image_reference=runtime["runtime_image_reference"], host_runtime_receipt=host_binding,
                               capture_tools={"execution_sources": runtime["capture_source_bundle"]})
    manifest["candidate_gateway"] = {
        "base_url": candidate["base_url"], "account_id": candidate["account_id"], "api_key_id": candidate["api_key_id"],
        "trigger": candidate["trigger"], "client_authentication": evidence.CANDIDATE_CLIENT_AUTHENTICATION,
        "oauth_credentials_read": False,
    }
    manifest["upstream"].update(relay_mode="connect", relay_listen_port=candidate["relay_port"])
    manifest["hosts_recovery"] = dict(evidence.CANDIDATE_HOSTS_RECOVERY)
    manifest["m_binding"] = {"complete": True, "requirements": {key: True for key in sorted(evidence.CANDIDATE_M_REQUIREMENTS)}}
    manifest["candidate_observation"] = {
        "client_outcome_valid": True, "message_request_expectation_met": True,
        "synthetic_plan_closed": None if candidate["live"] else True, "dimension_result": "passed",
    }
    if isinstance(manifest.get("dimension_evidence"), dict):
        manifest["dimension_evidence"].update(side="candidate", observation_only=True)
    return manifest


def copy_registered(state_path: Path, digest: str, projection_path: Path, projection_digest: str) -> int:
    """监督器的真实动作体：原件保持不变，候选投影和逐文件来源绑定只写一次。"""

    from tools.official_client_capture import claude_upgrade_evidence as evidence

    state = source.load_state(state_path, digest)
    source.bound({"path": str(projection_path), **replay._digest(projection_path)}, root=Path(state["data_root"]))
    if source.binding(projection_path)["sha256"] != projection_digest:
        raise replay.ReplayError("候选投影的外持摘要漂移")
    projection = source.read(projection_path)
    origin = verify_candidate_projection(state, projection)
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
        target = destination / (".origin-relay-manifest.json" if suffix.as_posix() == "relay-manifest.json" else suffix)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as handle:
            handle.write(original.read_bytes())
        target.chmod(0o600)
        if replay._digest(target) != item:
            raise replay.ReplayError("候选来源 R/P 或原始 relay 附件字节漂移")
        files.append({"original_relative": relative, "copied": source.binding(target)})
    manifest = source.write(destination / "relay-manifest.json", _candidate_manifest(state, projection, origin))
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
    source.write(projection_path.with_suffix(".copied.json"), {
        "fixture_only": True, "purpose": "candidate_control_source_projection", "operation": projection["operation"],
        "projection": source.binding(projection_path), "runtime": projection["runtime"], "origin": projection["origin"],
        "files": files, "candidate_manifest": source.binding(manifest), "restoration": source.binding(restoration),
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
