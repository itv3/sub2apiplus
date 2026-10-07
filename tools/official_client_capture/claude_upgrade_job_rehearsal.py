"""Claude 完整 Job 离线演练画像（Claude 升级控制面设计 B2-10）。

共享流程（增量计划、checkpoint、逐 Job 执行与复用、收据封存与重放、Formal／恢复兼容）在
``codex_upgrade_job_rehearsal_receipt``，按 ``--client claude`` 经客户端适配器取到本模块：

* 适配器成员 ``build_job_contract``／``binary_facts``／``job_contract_replay``：Claude 执行合同的构造、要核对的目标客户端
  二进制表与合同重放。Claude 总计划没有 Codex 的官方包、code-mode-host 与五个服务容器字段，所以合同是完整的另一份
  （``official-client-job-rehearsal-contract/v1``，带 ``client``），不是在 Codex 合同上换名字；
* 模块型成员 ``job_rehearsal_profile``（本模块）：合同名组（facts、收据、合同、采集器同组 ``official-client-job-rehearsal-*``）、
  场景清单在总计划里的输入名（``target_scenarios``）、要展开的阶段（只有 official；候选阶段的展开归 B4-01）、两个采集容器
  （联网的抓包容器与断网容器 ``capture-cli-synthetic``）各自的工具树、命令依赖与步骤启动器、执行副本坐标（总计划
  ``capture_tool_root`` 映射到宿主）、运行时探针（两个容器的证据 runs 根是同一个可写目录）与采集器身份。

演练只做离线检查，不发任何请求，也不读凭据：工具树三处（受管、宿主执行副本、两个容器内）逐文件一致且当前有效 wire 与策略
身份等于总计划登记的身份；两个容器在运行、命令依赖齐全；目标客户端在抓包容器、断网容器与宿主三处摘要等于 ``target_sha256``
（编排器服务面 ``_verify_official_binaries``）；每一步的启动器与执行容器相符（真实上游步骤只在抓包容器、合成步骤只在断网
容器），动作体是受管中继，目录模块与版本事实文件在执行容器里存在、版本事实摘要等于步骤声明，运行期变量的占位与声明一致。
ARM64 还没有做 Claude OAuth 登录时也能通过：凭据、CA 与全局状态文件属于正式取证的前提（B2-06 预约前门禁），这里不看。

本模块属 Claude 身份策略的 control 层（``claude_upgrade_`` 前缀），被 Codex 身份策略忽略（``claude_`` 前缀）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal

CLIENT = "claude"
FACTS_SCHEMA = "official-client-job-rehearsal-facts/v1"
RECEIPT_SCHEMA = "official-client-job-rehearsal-receipt/v1"
EXECUTION_CONTRACT_SCHEMA = "official-client-job-rehearsal-contract/v1"
PRODUCER_SCHEMA = "official-client-job-rehearsal-producer/v1"
PRODUCER_VERSION = "1"
CLI_LABEL = "Claude"
# 采集器身份：共享流程模块与本画像模块各一份路径与摘要（两份都决定 facts 怎么产生）。
PRODUCER_FIELDS = frozenset({"schema_version", "tool", "tool_sha256", "version", "profile", "profile_sha256"})
# 总计划 inputs 里目标场景清单冻结副本的输入名（B2-06 演练 Campaign 与 B2-11 预检 plan 同名）。
TARGET_SCENARIO_INPUT = "target_scenarios"
# 只展开 official 阶段：候选阶段与批准场景的展开依赖 VC-2／VC-4 产物，Claude 编排器在 B4-01 之前失败关闭。
JOB_PHASES = ("official",)
# 步骤启动器（facts 里 launcher 与依赖前缀）→ 总计划 configuration 里的执行容器字段。
LAUNCHER_CONTAINER_FIELDS: Mapping[str, str] = {
    "capture_container": "capture_container",
    "synthetic_container": "synthetic_capture_container",
}
STEP_LAUNCHERS = frozenset(LAUNCHER_CONTAINER_FIELDS)
# facts.tool_trees 里两个容器内执行副本的名字 → 容器字段（宿主两份 managed_host、execution_host 由共享流程记录）。
CONTAINER_TREE_FIELDS: Mapping[str, str] = {
    "execution_container": "capture_container",
    "execution_synthetic_container": "synthetic_capture_container",
}
CONTAINER_TREE_NAMES = tuple(CONTAINER_TREE_FIELDS)
CONTAINER_FIELDS = ("capture_container", "synthetic_capture_container")
# Formal 必需的探针摘要：只有证据 runs 根探针（沙箱、zstd 与失败生命周期探针属于 Codex 取证链）。
FORMAL_PROBE_DIGESTS = ("storage_probe_sha256",)
# facts 的额外字段：采集当时的有效工具身份（整树、wire、策略三摘要），重放时与执行合同登记的身份核对。
EXTRA_FACT_FIELDS = frozenset({"tool_identity"})
TOOL_IDENTITY_FIELDS = ("files_sha256", "wire_producer_sha256", "policy_sha256")

# 改变 Job 真实启动环境的总计划 configuration 字段：B2-05 plan 的 Campaign 变量去掉 Campaign ID／目录与目标版本／摘要
# （与 ``claude_upgrade_capture.CONFIGURATION_VARIABLES`` 相同；合同字段写成字面，变量表变化时由测试提醒同步）。
EXECUTION_CONFIGURATION_FIELDS = (
    "runtime_image",
    "capture_root",
    "capture_tool_root",
    "capture_container",
    "synthetic_capture_container",
    "claude_bin",
    "model",
    "upstream_ip",
    "credentials_file",
    "ca_signing_pem",
    "ca_cert",
)
# 跨工具修复可比较的执行坐标（共享流程 ``_contract_reuse_identity`` 按它判定前序结果能否复用）。
CONTRACT_REUSE_FIELDS = (
    "schema_version",
    "client",
    "target_version",
    "target_sha256",
    "official_identity_sha256",
    "suite",
    "configuration",
)
CONTRACT_FIELDS = frozenset(
    {
        *CONTRACT_REUSE_FIELDS,
        "tool_files_sha256",
        "wire_producer_sha256",
        "policy_sha256",
        "target_scenario_sha256",
        "evidence_labels_sha256",
        "job_count",
        "phase_counts",
        "job_ids",
        "job_phases",
        "step_counts",
        "job_templates_sha256",
    }
)
CONTRACT_SHA256_FIELDS = (
    "target_sha256",
    "official_identity_sha256",
    "tool_files_sha256",
    "wire_producer_sha256",
    "policy_sha256",
    "target_scenario_sha256",
    "evidence_labels_sha256",
    "job_templates_sha256",
)

# 宿主只负责 ``docker exec``；两个容器里中继要 openssl（CA）、tcpdump（P 通道 pcap），取证动作体与核验用 sha256sum、test。
HOST_REQUIRED_COMMANDS = ("docker", "python3")
CONTAINER_REQUIRED_COMMANDS = ("openssl", "python3", "sha256sum", "tcpdump", "test")

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
BINARY_VERIFICATION_FIELDS = frozenset(
    {
        "schema_version",
        "target_version",
        "target_sha256",
        "container_binary",
        "container_probe",
        "synthetic_container",
        "host_binary",
        "runtime_image",
        "verified_at_utc",
    }
)

# 每一步在执行容器里核对的文件：受管中继（编译检查语法，不写字节码）、FW-F v5 目录模块与版本事实文件。
_STEP_FILES_PROBE = r'''
import hashlib,json,pathlib,sys
request=json.loads(sys.argv[1])
output=[]
for item in request:
    path=pathlib.Path(item["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SystemExit("missing:"+item["path"])
    raw=path.read_bytes()
    if item["kind"]=="container_python_script":
        compile(raw,str(path),"exec",dont_inherit=True)
    output.append({"kind":item["kind"],"path":str(path),"sha256":hashlib.sha256(raw).hexdigest()})
print(json.dumps(output,sort_keys=True))
'''

# 证据 runs 根：两个容器里都必须是可写的真实目录，且是同一个目录（同一设备与 inode）。只读 stat，不写任何文件。
_RUNS_ROOT_PROBE = r'''
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
info={"path":str(root),"is_symlink":root.is_symlink()}
if root.is_dir() and not root.is_symlink():
    metadata=root.stat()
    info.update({"is_dir":True,"writable":os.access(root,os.W_OK|os.X_OK),"device":metadata.st_dev,"inode":metadata.st_ino})
else:
    info.update({"is_dir":False,"writable":False,"device":None,"inode":None})
print(json.dumps(info,sort_keys=True))
'''


def _error(message: str) -> job_rehearsal.JobRehearsalReceiptError:
    return job_rehearsal.JobRehearsalReceiptError(message)


def _sha256_text(value: Any) -> bool:
    return bool(job_rehearsal.SHA256_RE.fullmatch(str(value or "")))


def _configuration_error_type() -> type[BaseException]:
    from tools.official_client_capture.capturelib.model import ConfigurationError

    return ConfigurationError


# ---------------------------------------------------------------------------
# 适配器成员：执行合同构造、二进制表与合同重放
# ---------------------------------------------------------------------------


def build_job_contract(
    manifest: Mapping[str, Any],
    target_scenario: Mapping[str, Any],
    extra_jobs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """适配器成员 ``build_job_contract`` 的 Claude 实现：preflight 与 Formal 之间不含 Campaign ID 的稳定执行合同。

    目标身份是 ``target_sha256``（B2-01 冻结的 ``binary_sha256``，总计划 ``official_identity.binary_sha256`` 必须等于它）与
    整份 ``official_identity`` 的摘要（Formal 的官方身份带 npm 平台包与 GCS 两路摘要，演练 Campaign 只有二进制摘要）；
    工具身份取总计划 ``tool_identity`` 的整树、wire 与策略三摘要；配置字段按 Claude 总计划；证据标签必须逐 Job、逐 probe
    覆盖场景清单。Claude 没有 extra_jobs 输入。
    """

    from tools.official_client_capture import claude_upgrade_capture
    from tools.official_client_capture import claude_upgrade_plan

    if extra_jobs is not None:
        raise _error("Claude Campaign 没有 extra_jobs 输入")
    target_version = manifest.get("target_version")
    target_sha256 = manifest.get("target_sha256")
    if not isinstance(target_version, str) or not VERSION_RE.fullmatch(target_version):
        raise _error("target_version 非法")
    if not _sha256_text(target_sha256):
        raise _error("target_sha256 不是 SHA-256")
    official = manifest.get("official_identity")
    if not isinstance(official, Mapping) or official.get("binary_sha256") != target_sha256:
        raise _error("总计划 official_identity.binary_sha256 与 target_sha256 不一致")
    tool = manifest.get("tool_identity")
    if not isinstance(tool, Mapping) or not all(_sha256_text(tool.get(field)) for field in TOOL_IDENTITY_FIELDS):
        raise _error("总计划 tool_identity 缺少整树、wire 或策略摘要")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise _error("总计划缺少 configuration")
    frozen_configuration = {field: configuration.get(field) for field in EXECUTION_CONFIGURATION_FIELDS}
    missing = [field for field, value in frozen_configuration.items() if not isinstance(value, str) or not value]
    if missing:
        raise _error("Job 执行配置存在空值：" + "、".join(missing))
    try:
        suite = claude_upgrade_capture._campaign_suite(manifest)
        labels_path = claude_upgrade_plan.evidence_labels_path(target_version)
        claude_upgrade_plan.load_evidence_labels(
            labels_path,
            expected_version=target_version,
            scenario_payload=target_scenario,
        )
    except (_configuration_error_type(), KeyError, TypeError) as error:
        raise _error(f"目标版本 {target_version} 的 suite 或证据标签声明非法：{error}") from error
    templates = [
        item for item in job_rehearsal._job_templates(target_scenario, None, suite) if item["phase"] in JOB_PHASES
    ]
    if not templates:
        raise _error("目标场景清单在本 suite 下没有 official Job")
    job_phases = {str(item["id"]): str(item["phase"]) for item in templates}
    step_counts = {str(item["id"]): len(item["steps"]) for item in templates}
    return {
        "schema_version": EXECUTION_CONTRACT_SCHEMA,
        "client": CLIENT,
        "target_version": target_version,
        "target_sha256": str(target_sha256),
        "official_identity_sha256": job_rehearsal._fingerprint(dict(official)),
        "suite": suite,
        "tool_files_sha256": str(tool["files_sha256"]),
        "wire_producer_sha256": str(tool["wire_producer_sha256"]),
        "policy_sha256": str(tool["policy_sha256"]),
        "target_scenario_sha256": job_rehearsal._fingerprint(target_scenario),
        "evidence_labels_sha256": job_rehearsal._sha256_file(labels_path),
        "configuration": frozen_configuration,
        "job_count": len(templates),
        "phase_counts": {phase: sum(value == phase for value in job_phases.values()) for phase in JOB_PHASES},
        "job_ids": sorted(job_phases),
        "job_phases": dict(sorted(job_phases.items())),
        "step_counts": dict(sorted(step_counts.items())),
        "job_templates_sha256": job_rehearsal._fingerprint(templates),
    }


def validate_execution_contract(contract: Any) -> dict[str, Any]:
    """Claude 执行合同的严格校验（字段闭集、合同名与客户端、摘要、配置与 Job 集）。"""

    job_rehearsal._expect(contract, set(CONTRACT_FIELDS), "execution_contract")
    if contract.get("schema_version") != EXECUTION_CONTRACT_SCHEMA or contract.get("client") != CLIENT:
        raise _error("execution_contract.schema_version 不匹配")
    for field in CONTRACT_SHA256_FIELDS:
        if not _sha256_text(contract.get(field)):
            raise _error(f"execution_contract.{field} 非法")
    if not isinstance(contract.get("target_version"), str) or not VERSION_RE.fullmatch(contract["target_version"]):
        raise _error("execution_contract.target_version 非法")
    if contract.get("suite") not in {"core", "full"}:
        raise _error("execution_contract.suite 非法")
    configuration = contract.get("configuration")
    if not isinstance(configuration, dict) or set(configuration) != set(EXECUTION_CONFIGURATION_FIELDS):
        raise _error("execution_contract.configuration 不闭合")
    if any(not isinstance(value, str) or not value for value in configuration.values()):
        raise _error("execution_contract.configuration 含空值")
    job_ids = contract.get("job_ids")
    phases = contract.get("job_phases")
    counts = contract.get("step_counts")
    phase_counts = contract.get("phase_counts")
    if (
        not isinstance(job_ids, list)
        or not job_ids
        or job_ids != sorted(job_ids)
        or len(job_ids) != len(set(job_ids))
        or not all(isinstance(item, str) and job_rehearsal.SAFE_ID_RE.fullmatch(item) for item in job_ids)
        or not isinstance(phases, dict)
        or set(phases) != set(job_ids)
        or not all(value in JOB_PHASES for value in phases.values())
        or not isinstance(counts, dict)
        or set(counts) != set(job_ids)
        or not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in counts.values())
        or not isinstance(phase_counts, dict)
        or set(phase_counts) != set(JOB_PHASES)
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in phase_counts.values())
        or contract.get("job_count") != len(job_ids)
        or sum(phase_counts.values()) != len(job_ids)
        or any(phase_counts[phase] != sum(value == phase for value in phases.values()) for phase in phase_counts)
    ):
        raise _error("execution_contract Job 集非法")
    return contract


def binary_facts(contract: Mapping[str, Any]) -> dict[str, Any]:
    """适配器成员 ``binary_facts`` 的 Claude 实现：一套目标客户端在抓包容器、断网容器与宿主三处的路径与期望摘要。"""

    from tools.official_client_capture import claude_upgrade_capture

    version = str(contract["target_version"])
    expected = str(contract["target_sha256"])
    configuration = contract["configuration"]
    container_path = str(configuration["claude_bin"])
    host_path = claude_upgrade_capture.HOST_CLIENT_BINARY_TEMPLATE.format(version=version)
    return {
        "expected_version": version,
        "identities": {
            f"capture_container:{container_path}": expected,
            f"synthetic_container:{container_path}": expected,
            f"host:{host_path}": expected,
        },
        "container_binary_path": container_path,
        "host_binary_path": host_path,
        "synthetic_container": str(configuration["synthetic_capture_container"]),
        "runtime_image_reference": str(configuration["runtime_image"]),
    }


def job_contract_replay(contract: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    """适配器成员 ``job_contract_replay`` 的 Claude 实现：按执行合同核对 facts 里的目标客户端核验事实。

    事实来自编排器服务面 ``_verify_official_binaries``（``claude-upgrade-official-binary-verification/v2``）：抓包容器与
    断网容器里同一路径的客户端、宿主 ``/opt/claude-<v>/claude`` 三处摘要都等于 ``target_sha256``，断网容器 network_mode 为
    none 且与运行镜像同一 image ID，运行镜像引用等于合同登记的不可变引用。
    """

    from tools.official_client_capture import claude_upgrade_capture

    expected = binary_facts(contract)
    target = str(contract["target_sha256"])
    binary = facts.get("binary_verification")
    message = "Claude 目标客户端或运行镜像核验事实非法"
    if not isinstance(binary, dict) or set(binary) != BINARY_VERIFICATION_FIELDS:
        raise _error(message)
    container_binary = binary.get("container_binary")
    synthetic = binary.get("synthetic_container")
    host_binary = binary.get("host_binary")
    runtime_image = binary.get("runtime_image")
    synthetic_binary = synthetic.get("client_binary") if isinstance(synthetic, Mapping) else None
    if (
        binary.get("schema_version") != claude_upgrade_capture.BINARY_VERIFICATION_SCHEMA
        or binary.get("target_version") != expected["expected_version"]
        or binary.get("target_sha256") != target
        or not isinstance(container_binary, Mapping)
        or container_binary.get("path") != expected["container_binary_path"]
        or container_binary.get("sha256") != target
        or container_binary.get("version") != expected["expected_version"]
        or not isinstance(binary.get("container_probe"), Mapping)
        or not isinstance(synthetic, Mapping)
        or synthetic.get("name") != expected["synthetic_container"]
        or synthetic.get("network_mode") != "none"
        or not isinstance(synthetic_binary, Mapping)
        or dict(synthetic_binary) != dict(container_binary)
        or host_binary != {"path": expected["host_binary_path"], "sha256": target}
        or not isinstance(runtime_image, Mapping)
        or set(runtime_image) != {"reference", "image_id"}
        or runtime_image.get("reference") != expected["runtime_image_reference"]
        or not job_rehearsal.IMAGE_ID_RE.fullmatch(str(runtime_image.get("image_id", "")))
        or synthetic.get("image_id") != runtime_image.get("image_id")
    ):
        raise _error(message)
    job_rehearsal._rfc3339(binary.get("verified_at_utc"), "binary_verification.verified_at_utc")
    return {"binary_facts": expected}


# ---------------------------------------------------------------------------
# 画像：执行副本、工具树、容器、依赖、探针与采集器身份
# ---------------------------------------------------------------------------


def rehearsal_execution_roots(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[Path, Path, str]:
    """执行副本坐标：总计划 ``capture_tool_root``（容器坐标）按 B2-06 的坐标布局映射到宿主；核验根取宿主副本上两级
    （编排器 ``_verify_execution_tree`` 的约定），容器内副本就是 ``capture_tool_root``（两个容器挂载相同）。"""

    from tools.official_client_capture import claude_upgrade_capture

    try:
        layout = claude_upgrade_capture._capture_layout(campaign_dir, manifest)
        execution_root = claude_upgrade_capture._host_path(layout.capture_tool_root, layout)
    except _configuration_error_type() as error:
        raise _error(f"Claude 采集坐标布局非法：{error}") from error
    return execution_root.parents[1], execution_root, str(layout.capture_tool_root)


def rehearsal_container_tool_trees(configuration: Mapping[str, Any], container_root: str) -> dict[str, Any]:
    """抓包容器与断网容器里执行副本的工具树（同一挂载，必须与受管树逐文件一致）。"""

    return {
        name: job_rehearsal._container_tool_tree(
            str(configuration[field]), container_root, label=str(configuration[field])
        )
        for name, field in CONTAINER_TREE_FIELDS.items()
    }


def rehearsal_tree_matches_contract(tree: Mapping[str, Any], contract: Mapping[str, Any]) -> bool:
    """受管树与执行合同的绑定不在这里比：共享流程的工具树摘要只覆盖 ``official_client_capture`` 一个根，Claude 总计划的
    整树摘要按 v3 身份引擎覆盖两个受管根，二者口径不同。绑定改由 facts 额外记录的有效工具身份承担
    （``rehearsal_validate_extra``：wire 与策略身份必须等于合同登记的值，与采集时 ``_contract_tool_matches`` 同一口径）。"""

    del tree, contract
    return True


def rehearsal_extra_facts(contract: Mapping[str, Any], adapter: Any) -> dict[str, Any]:
    """采集当时 Claude 受管工具的有效身份（整树、wire、策略三摘要），供重放时与执行合同核对。"""

    from tools.official_client_capture import claude_upgrade

    del contract, adapter
    try:
        identity = claude_upgrade._tool_identity(include_git=False)
    except _configuration_error_type() as error:
        raise _error(f"Claude 工具身份无法计算：{error}") from error
    return {"tool_identity": {field: identity[field] for field in TOOL_IDENTITY_FIELDS}}


def _runs_root_probe(configuration: Mapping[str, Any]) -> dict[str, Any]:
    runs_root = str(PurePosixPath(str(configuration["capture_root"])) / "runs")
    observations: dict[str, Any] = {}
    for launcher, field in LAUNCHER_CONTAINER_FIELDS.items():
        container = str(configuration[field])
        raw = job_rehearsal._run(
            ["docker", "exec", container, "python3", "-c", _RUNS_ROOT_PROBE, runs_root],
            f"{container} 证据 runs 根探针",
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise _error(f"{container} 证据 runs 根探针输出非法") from error
        if not isinstance(payload, dict):
            raise _error(f"{container} 证据 runs 根探针输出非法")
        observations[launcher] = {"container": container, **payload}
    probe = {
        "status": "passed",
        "runs_root": runs_root,
        "containers": observations,
        "same_directory": True,
    }
    _validate_runs_root_probe(probe, configuration)
    return probe


def _validate_runs_root_probe(value: Any, configuration: Mapping[str, Any]) -> dict[str, Any]:
    probe = job_rehearsal._expect(value, {"status", "runs_root", "containers", "same_directory"}, "probes.storage")
    containers = job_rehearsal._expect(probe.get("containers"), set(LAUNCHER_CONTAINER_FIELDS), "probes.storage.containers")
    identities = set()
    for launcher, field in LAUNCHER_CONTAINER_FIELDS.items():
        item = job_rehearsal._expect(
            containers.get(launcher),
            {"container", "path", "is_symlink", "is_dir", "writable", "device", "inode"},
            f"probes.storage.containers.{launcher}",
        )
        if (
            item.get("container") != configuration[field]
            or item.get("path") != probe.get("runs_root")
            or item.get("is_symlink") is not False
            or item.get("is_dir") is not True
            or item.get("writable") is not True
            or not isinstance(item.get("device"), int)
            or isinstance(item.get("device"), bool)
            or not isinstance(item.get("inode"), int)
            or isinstance(item.get("inode"), bool)
        ):
            raise _error(f"{item.get('container')} 的证据 runs 根不是可写的真实目录")
        identities.add((item["device"], item["inode"]))
    if (
        probe.get("status") != "passed"
        or probe.get("runs_root") != str(PurePosixPath(str(configuration["capture_root"])) / "runs")
        or probe.get("same_directory") is not True
        or len(identities) != 1
    ):
        raise _error("抓包容器与断网容器的证据 runs 根不是同一个目录")
    return probe


def rehearsal_early_probes(jobs: list[Any], configuration: Mapping[str, Any]) -> dict[str, Any]:
    """二进制核验之前的运行时探针：两个容器的证据 runs 根（真实上游与合成步骤的证据都写在这里）。"""

    del jobs
    return {"storage": _runs_root_probe(configuration)}


def _argument_value(argv: Iterable[str], flag: str) -> str | None:
    from tools.official_client_capture import claude_upgrade_plan

    return claude_upgrade_plan._argument_value(list(argv), flag)


def _tool_path(value: str | None, tool_root: PurePosixPath, label: str) -> str:
    if value is None:
        raise _error(f"{label} 缺失或重复")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise _error(f"{label} 不是规范绝对路径：{value}")
    try:
        path.relative_to(tool_root)
    except ValueError as error:
        raise _error(f"{label} 不在执行副本 {tool_root} 下：{value}") from error
    return value


def _step_probe(
    job_id: str,
    index: int,
    step: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """一步的启动器与路径探针：执行容器与步骤种类相符，受管中继与目录文件在执行容器里可用，运行期占位与声明一致。"""

    from tools.official_client_capture import claude_upgrade_plan

    label = f"{job_id}:step-{index}"
    argv = [str(item) for item in step["argv"]]
    if len(argv) < 5 or argv[0] != "docker" or argv[1] != "exec":
        raise _error(f"{label} 启动器未登记：{argv[:3]}")
    # 真实上游步骤冻结了 --upstream-ip，只能在联网的抓包容器里执行；合成步骤只能在断网容器里执行（B2-05／B2-06 同一规则）。
    launcher = "capture_container" if _argument_value(argv, "--upstream-ip") is not None else "synthetic_container"
    container = str(configuration[LAUNCHER_CONTAINER_FIELDS[launcher]])
    if argv[2] != container:
        raise _error(f"{label} docker exec 未绑定执行容器 {container}（{launcher}）")
    tool_root = PurePosixPath(str(configuration["capture_tool_root"]))
    script = str(tool_root / claude_upgrade_plan.RELAY_SCRIPT)
    if argv[3] != "python3" or argv[4] != script or "capture" not in argv[5:6]:
        raise _error(f"{label} 不是受管中继 {script} capture 动作体")
    catalog = _tool_path(_argument_value(argv, claude_upgrade_plan.FW_F_CATALOG_FLAG), tool_root, f"{label} 目录模块")
    version_facts = _tool_path(
        _argument_value(argv, claude_upgrade_plan.FW_F_VERSION_FACTS_FLAG), tool_root, f"{label} 版本事实"
    )
    version_facts_sha256 = _argument_value(argv, claude_upgrade_plan.FW_F_VERSION_FACTS_SHA256_FLAG)
    if not _sha256_text(version_facts_sha256):
        raise _error(f"{label} 版本事实摘要缺失或非法")
    residual = {
        name
        for value in [*argv, *(str(item) for item in dict(step["environment"]).values())]
        for name in claude_upgrade_plan.TEMPLATE_FIELD_RE.findall(value)
    }
    declared = step.get("runtime_variables")
    if not isinstance(declared, Mapping) or set(declared) != residual:
        raise _error(f"{label} 运行期变量声明与残留占位不一致：声明 {sorted(declared or {})}，占位 {sorted(residual)}")
    request = [
        {"kind": "container_python_script", "path": script},
        {"kind": "container_file", "path": catalog},
        {"kind": "container_file", "path": version_facts},
    ]
    raw = job_rehearsal._run(
        ["docker", "exec", container, "python3", "-c", _STEP_FILES_PROBE, json.dumps(request, sort_keys=True)],
        f"{label} 执行容器内动作体与目录文件",
    )
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _error(f"{label} 执行容器文件探针输出非法") from error
    if (
        not isinstance(checks, list)
        or [(item.get("kind"), item.get("path")) for item in checks if isinstance(item, dict)]
        != [(item["kind"], item["path"]) for item in request]
        or not all(isinstance(item, dict) and set(item) == {"kind", "path", "sha256"} and _sha256_text(item["sha256"]) for item in checks)
    ):
        raise _error(f"{label} 执行容器文件探针结果不闭合")
    if checks[2]["sha256"] != version_facts_sha256:
        raise _error(f"{label} 版本事实文件摘要与步骤声明不一致")
    return launcher, checks


def rehearsal_job_probe(
    job: Any,
    configuration: Mapping[str, Any],
    *,
    component_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """逐 Job 的启动器与路径探针（结果形状与 Codex 相同；Claude 没有 C2PA 正负 Job，``c2pa_identity`` 恒为 None）。"""

    document = job_rehearsal._job_document(job)
    incremental = job_rehearsal._job_incremental_metadata(document, component_summary)
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(document["steps"], 1):
        argv = step.get("argv")
        environment = step.get("environment")
        timeout = step.get("timeout")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(environment, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise _error(f"{job.job_id} 第 {index} 步命令、环境或超时非法")
        launcher, path_checks = _step_probe(str(job.job_id), index, step, configuration)
        steps.append(
            {
                "index": index,
                "status": "passed",
                "launcher": launcher,
                "argv_sha256": job_rehearsal._fingerprint(argv),
                "environment_sha256": job_rehearsal._fingerprint(environment),
                "timeout_seconds": timeout,
                "path_checks": path_checks,
                "dependencies": ["host:docker", *(f"{launcher}:{name}" for name in CONTAINER_REQUIRED_COMMANDS)],
            }
        )
    return {
        "id": job.job_id,
        "phase": job.phase,
        "status": "passed",
        "job_contract_sha256": job_rehearsal._fingerprint(document),
        "step_count": len(steps),
        "steps": steps,
        "c2pa_identity": None,
        "tool_components": incremental["components"],
        "tool_component_digests": incremental["component_digests"],
        "input_sha256": incremental["input_sha256"],
        "environment_sha256": incremental["environment_sha256"],
        "dependency_sha256": incremental["dependency_sha256"],
        "incremental_result_key": incremental["result_key"],
        "disposition": "executed",
    }


def rehearsal_container_facts(configuration: Mapping[str, Any]) -> list[dict[str, Any]]:
    """两个采集容器的运行状态与身份。"""

    return job_rehearsal._container_facts(configuration, CONTAINER_FIELDS)


def rehearsal_dependency_facts(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """宿主与两个采集容器的命令依赖（先宿主后容器）。"""

    return {
        "host": job_rehearsal._host_dependencies(HOST_REQUIRED_COMMANDS),
        **{
            launcher: job_rehearsal._container_dependencies(
                str(configuration[field]), CONTAINER_REQUIRED_COMMANDS, label=str(configuration[field])
            )
            for launcher, field in LAUNCHER_CONTAINER_FIELDS.items()
        },
    }


def rehearsal_probe_facts(configuration: Mapping[str, Any], container_root: str, early: Mapping[str, Any]) -> dict[str, Any]:
    """facts 的 probes：只有二进制核验之前跑好的证据 runs 根探针。"""

    del configuration, container_root
    return {"storage": early["storage"]}


def rehearsal_producer() -> dict[str, str]:
    """采集器身份：共享流程模块与本画像模块。"""

    tool = Path(job_rehearsal.__file__).resolve()
    profile = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(tool),
        "tool_sha256": job_rehearsal._sha256_file(tool),
        "version": PRODUCER_VERSION,
        "profile": str(profile),
        "profile_sha256": job_rehearsal._sha256_file(profile),
    }


def rehearsal_validate_containers(containers: Any, contract: Mapping[str, Any]) -> None:
    """facts 的容器集合必须正好是两个采集容器，且都在运行。"""

    configuration = contract["configuration"]
    expected = sorted({str(configuration[field]) for field in CONTAINER_FIELDS})
    if not isinstance(containers, list):
        raise _error("containers 必须是数组")
    names: list[str] = []
    for item in containers:
        current = job_rehearsal._expect(item, {"name", "container_id", "image_id", "running"}, "containers")
        if (
            current.get("running") is not True
            or not job_rehearsal.CONTAINER_ID_RE.fullmatch(str(current.get("container_id", "")))
            or not job_rehearsal.IMAGE_ID_RE.fullmatch(str(current.get("image_id", "")))
        ):
            raise _error("容器未运行或身份非法")
        names.append(str(current.get("name", "")))
    if names != expected:
        raise _error("容器集合未完整覆盖 Job 依赖")


def rehearsal_validate_dependencies(value: Any) -> None:
    """宿主与两个采集容器的命令依赖闭集。"""

    dependencies = job_rehearsal._expect(value, {"host", *LAUNCHER_CONTAINER_FIELDS}, "dependencies")
    job_rehearsal._validate_dependencies(dependencies["host"], HOST_REQUIRED_COMMANDS, "dependencies.host")
    for launcher in LAUNCHER_CONTAINER_FIELDS:
        job_rehearsal._validate_dependencies(
            dependencies[launcher], CONTAINER_REQUIRED_COMMANDS, f"dependencies.{launcher}"
        )


def rehearsal_validate_probes(
    raw_probes: Any,
    contract: Mapping[str, Any],
    *,
    allow_collector_drift: bool,
) -> tuple[str | None, str | None]:
    """probes 只有证据 runs 根一项；返回它的摘要（作为 ``storage_probe_sha256``），没有失败生命周期探针。"""

    del allow_collector_drift
    probes = job_rehearsal._expect(raw_probes, {"storage"}, "probes")
    storage = _validate_runs_root_probe(probes.get("storage"), contract["configuration"])
    return job_rehearsal._fingerprint(storage), None


def rehearsal_validate_extra(facts: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    """采集当时的有效 wire 与策略身份必须等于执行合同登记的值（整树摘要只作审计，与 Codex v2 合同同一口径）。"""

    identity = job_rehearsal._expect(facts.get("tool_identity"), set(TOOL_IDENTITY_FIELDS), "tool_identity")
    if not all(_sha256_text(identity.get(field)) for field in TOOL_IDENTITY_FIELDS):
        raise _error("tool_identity 摘要非法")
    if (
        identity["wire_producer_sha256"] != contract["wire_producer_sha256"]
        or identity["policy_sha256"] != contract["policy_sha256"]
    ):
        raise _error("演练时的有效 wire／策略身份与执行合同不一致")
