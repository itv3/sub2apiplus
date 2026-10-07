#!/usr/bin/env python3
"""Claude 的 VC-5 派发（Claude 升级 B5b-01b，``vc5-claude-start.sh``／``vc5-claude-switch.sh``／``vc5-claude-seal.sh``／
``vc5-claude-accept.sh`` 调用）：各批次的动作计划、候选网关的 compose 改写与激活事实核对、只读预检、seal 链状态、门禁执行主机
覆盖与外部门禁 subject。

为什么不沿用 Codex 的 ``gen_vc5_plans.py`` 与 ``vc5-seal.sh``／``vc5-accept.sh``：它们手写 ``codex_upgrade.py`` 的命令与 Codex 的
Kilo 双入口、assertion bundle、observed-profile 收据与 canonical 交接。Claude 的 VC-5 是：

* run：受管 ``capture-candidate run``（真实请求确认、最长墙钟），候选网关之前切到候选镜像（Claude 候选开关、隔离实例
  ``ARM64`` 与激活事实那组变量，见 ``switch_compose``），派发前只读调候选 run 的正式入口门（``precheck``）；
* seal 链（``seal-state`` 判定下一步）：两项 attempt 级边界核对（``capture-candidate boundary-check``，B4-01 f 段；每项一个
  批次，零请求的第三方入口拒绝在前；已在的收据经 f 段的 ``verify_boundary_receipt`` 核对，不合法即中止待人工审计）→
  Desktop／VS Code 客户端收据就位（Mac 侧产出；没就位即暂停）→ seal 预览（``--client-evidence`` 与
  ``--gate-execution-overrides``）→ 审核人批准（不自动批准；没给批准即暂停）→ 批准与 compare 一个批次；
* accept 链：assert 批次（builder 配置、输出与基线取比较收据登记的那一份）→ 目标平台门禁照旧 → 门禁 facts（subject 取
  ``claude_upgrade_compare.candidate_gate_identity`` 与 ``claude_upgrade_accept.expected_gate_subject``）→ accept 批次（写 VC-5
  checkpoint）。

动作一律经受管 ``claude_upgrade_batch.managed_action``／``action_plan`` 生成（受管 Python 直接调 ``claude_upgrade.py``），阶段项与
Codex 同名：``candidate-run``、``candidate-seal``、``compare``、``assert-rules``、``acceptance``。复用项与 Codex 的
``gen_vc5_plans`` 同一口径：候选 Job 之后的零请求批次（第三方边界核对、seal 预览、批准与 compare、compare、assert、accept）
的 ``reuse_item_ids`` 是本候选的全部 Job（监督器 post-run-tooling 五条判据的第 3 条要求复用项覆盖 attempt 的全部 Job），
run 批次为空。发真实请求的 codex-final-wire-isolation 批次复用项也为空：它失败时不得被判成零请求后处理，按
execution-failure 收账（它的 Codex 请求按 f 段只记在收据的 ``live_requests``，失败的运行只留运行目录、不进项目总账）。

边界核对的轮次参数（``boundary-parameters``）：codex-final-wire-isolation 要四个参数文件键（``BOUNDARY_PARAMETERS``），缺一或
格式不对即零写入拒绝；``PRODUCTION_COMMIT`` 另须在候选源码仓里（``git cat-file -e``，不刷新索引）。VC-5 启动预检也查一遍，
免得候选 run 跑完才发现缺参数。

门禁执行主机覆盖（``gate-overrides``）：B4-02 封存时冻结的门禁执行计划与门禁收据逐字比对主机与架构。两条本机门禁
（check-egress-spec、full-regression）在操作员本机跑、记录被注入 ``$GATE_ROOT/local``，取那两份 ``gate.json`` 的 ``host`` 与
``architecture``；目标平台门禁在采集主机由 ``vc5-gate-target.sh`` 经 ``lib.sh`` 的 ``write_gate_json`` 记录，按同一算法现算
（短主机名、``<系统>/<机器>``）。

子命令（只读，或只写本轮输入目录 W 下的动作计划、compose 文件与 ``$RUNROOT/vc5-pause.json``）：

* ``run-plan --output <W 下的计划> [--max-wall-seconds N]``；
* ``switch-compose --tag <镜像标签>``：从 compose 备份改写 ``$COMPOSE_DIR/docker-compose.yml``（Claude 候选变量块）；
* ``activation-check --fact <文件>``：服务写出的激活事实是候选 Runtime，画像链四项与部署身份等于 VC-4 构建收据；
* ``precheck``：边界核对轮次参数，再只读调候选 run 的正式入口门（``claude_upgrade_candidate_capture.formal_admission``）；
* ``boundary-plan --check <check_id> --output <计划>``：一项边界核对的批次计划；
* ``boundary-parameters``：打印 codex-final-wire-isolation 的入口旗标（核对参数文件键与生产提交）；
* ``seal-state``：JSON：边界核对收据是否就位且合法、客户端收据是否就位、当前输入下的 seal 预览与批准状态、下一步；
* ``seal-plan --step preview|approve --output <计划> [--approve-seal-sha256 --approved-by]``；
* ``gate-overrides``：打印门禁执行主机覆盖 JSON；
* ``assert-plan --output <计划>``、``accept-plan --output <计划>``；
* ``gate-subject --output <文件>``：外部门禁 subject（供 ``build_gate_facts.py --subject``）；
* ``recovery-plans --preview <已授权的恢复预览> [--dry-run]``：候选采集续跑的 N+1 预览与 N+2 补跑两份计划（B4-05b-1，见第四节）；
* ``pause --reason <原因> --next <下一步>``：写 ``$RUNROOT/vc5-pause.json``（``vc5-all.sh`` 读它暂停）；
* ``summary``：Campaign 状态、候选阶段、比较收据、断言结果、accept 与 VC-5 checkpoint。

只给 VC-5 派发脚本为 ``vc5-claude-*.sh`` 的客户端用（客户端表 ``vc5_start_script``），其余客户端拒绝。退出码：0 成功；3 拒绝。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_env  # noqa: E402

DISPATCH_SCRIPT = "vc5-claude-start.sh"
RUN_TIMEOUT_SECONDS = 21600
ACTION_TIMEOUT_SECONDS = 1800
# 两项 attempt 级边界核对（B4-01 f 段），按派发顺序：零上游请求的第三方入口拒绝在前，发真实请求的 Codex final wire 隔离在后
# （便宜的失败先暴露，不先花真实请求）。
BOUNDARY_CHECKS = ("third-party-ingress-rejection", "codex-final-wire-isolation")
# 有真实请求的边界核对（Codex mitm 最小场景）；另一项零上游请求。
LIVE_BOUNDARY_CHECKS = frozenset({"codex-final-wire-isolation"})
# 每项的（动作超时, 入口 --max-wall-seconds）：入口自己的墙钟比监督器动作超时早 10 分钟到期，失败由入口写运行目录与结论
# （入口缺省墙钟 90 分钟，Codex 那项要跑冻结的 candidate-core-mitm）。
BOUNDARY_TIMEOUTS = {
    "third-party-ingress-rejection": (3600, 3000),
    "codex-final-wire-isolation": (21600, 21000),
}
# codex-final-wire-isolation 的四个轮次参数（参数文件键 → 入口旗标；客户端表 Claude 的 exclusive_optional_keys）。
BOUNDARY_PARAMETERS = (
    ("BOUNDARY_CODEX_ACCOUNT_ID", "--codex-account-id"),
    ("BOUNDARY_CODEX_API_KEY_ID", "--codex-api-key-id"),
    ("BOUNDARY_CODEX_MODEL", "--codex-model"),
    ("PRODUCTION_COMMIT", "--production-commit"),
)
LOCAL_GATES = ("check-egress-spec", "full-regression")
TARGET_GATE = "target-platform"
GATE_RECEIPT_NAME = "candidate-gates.receipt.json"
PAUSE_FILENAME = "vc5-pause.json"
# 候选网关（Go）：候选开关只在登记的隔离候选实例开启（config.IsClaudeCandidateIsolatedInstance），ARM64 的登记值是 ARM64；
# 激活事实那组声明变量与 Go claude_official_egress_activation_fact.go 同名（不声明批准摘要：候选声明批准摘要会被拒绝）。
ISOLATED_INSTANCE_ID = "ARM64"
INSTANCE_ENV = "GATEWAY_OFFICIAL_EGRESS_GUARD_INSTANCE_ID"
CANDIDATE_SWITCH_ENV = "GATEWAY_CLAUDE_FW_G_CANDIDATE_ENABLED"
ACTIVATION_PATH_ENV = "GATEWAY_CLAUDE_EGRESS_ACTIVATION_FACT_PATH"
CANDIDATE_ENV_PREFIXES = ("GATEWAY_CLAUDE_", INSTANCE_ENV)
ENV_ANCHOR = "      - AUTO_SETUP=true\n"


def _row() -> dict[str, Any]:
    table = parse_env.client_table()
    return dict(table.spec(table.resolve_client(os.environ.get(table.CLIENT_PARAM))))


def _binding() -> dict[str, Any]:
    """本轮客户端的表行；VC-5 派发脚本不是 Claude 的客户端在加载任何受管模块之前就拒绝。"""

    row = _row()
    if row["vc5_start_script"] != DISPATCH_SCRIPT:
        raise ValueError(f"本轮客户端（{row['client']}）的 VC-5 派发脚本是 {row['vc5_start_script']}，不是 {DISPATCH_SCRIPT}")
    return row


def _managed(name: str) -> Any:
    return importlib.import_module(f"tools.official_client_capture.{name}")


def _batch() -> Any:
    return _managed("claude_upgrade_batch")


def _campaign() -> Path:
    return Path(os.environ["NEWDIR"])


def _candidate() -> str:
    return os.environ["CAND"]


def _manifest() -> dict[str, Any]:
    return _managed("claude_upgrade")._require_formal_campaign(_campaign())


def _write_plan(path: Path, plan: dict[str, Any]) -> None:
    """计划只写进本轮输入目录 W（同一路径重跑时覆盖，0600）。"""

    if path.parent.resolve(strict=True) != Path(os.environ["W"]).resolve(strict=True):
        raise ValueError(f"动作计划只能写在本轮输入目录 W 下：{path}")
    if path.is_symlink():
        raise ValueError(f"动作计划路径是符号链接：{path}")
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _plan(actions: list[dict[str, Any]], output: Path, *, reuse: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    payload = _batch().action_plan(actions, phase="VC-5", reuse_item_ids=reuse)
    _write_plan(output, payload)
    print(f"VC-5 批次计划 -> {output}：{[(item['action_id'], item['item_ids']) for item in payload['actions']]}")
    return payload


def _action(action_id: str, operation: str, argv: list[Any], item: str, timeout: float = ACTION_TIMEOUT_SECONDS) -> dict[str, Any]:
    return _batch().managed_action(action_id, operation, argv, item_ids=[item], timeout_seconds=timeout)


def _ref() -> list[str]:
    return ["--campaign-dir", str(_campaign()), "--candidate-id", _candidate()]


def _candidate_job_ids() -> list[str]:
    """本候选的全部 Job（与 Codex ``driver_config.candidate_job_ids`` 同一口径：Campaign 已批准的场景展开的候选 Job 全集）。"""

    return sorted(job.job_id for job in _managed("claude_upgrade")._campaign_jobs(_campaign(), _manifest(), "candidate", candidate_id=_candidate()))


def _attempt_id() -> str:
    value = os.environ.get("ATT", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("没有当前候选 attempt（ATT）：先由阶段解析 --mode attempt 给出")
    return value


# ---------------------------------------------------------------------------
# 一、run
# ---------------------------------------------------------------------------


def run_plan(output: Path, max_wall_seconds: int = RUN_TIMEOUT_SECONDS) -> dict[str, Any]:
    _binding()
    argv = ["capture-candidate", "run", *_ref(), "--acknowledge-live-requests", "--max-wall-seconds", str(int(max_wall_seconds))]
    return _plan([_action("candidate-run", "VC-5:capture-candidate-run", argv, "candidate-run", RUN_TIMEOUT_SECONDS)], output)


def _build_receipt() -> dict[str, Any]:
    receipt, _binding_value = _managed("claude_upgrade_compare").load_candidate_build_receipt(
        _campaign(), _manifest(), _candidate(), int(os.environ["CANDIDATE_REVISION"])
    )
    return receipt


def candidate_environment(receipt: dict[str, Any], candidate: str) -> list[tuple[str, str]]:
    """候选网关的环境变量（顺序固定）：候选开关、登记的隔离实例 ID、激活事实路径与声明身份（画像与 Release 摘要、镜像、
    源码树、构建与部署版本；声明值都取 VC-4 构建收据，Go 落盘激活事实前与运行时解析结果逐字比对）。"""

    return [
        (CANDIDATE_SWITCH_ENV, "true"),
        (INSTANCE_ENV, ISOLATED_INSTANCE_ID),
        (ACTIVATION_PATH_ENV, f"/app/data/{candidate}-claude-activation-fact.json"),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_PROFILE_DIGEST", str(receipt["profile"]["profile_digest"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_RELEASE_DIGEST", str(receipt["profile"]["release_sha256"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_ID", str(receipt["image"]["image_id"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_IMAGE_REFERENCE", str(receipt["image"]["reference"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_SOURCE_TREE_SHA256", str(receipt["source"]["tree_sha256"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_BUILD_ID", str(receipt["build"]["build_id"])),
        ("GATEWAY_CLAUDE_EGRESS_ACTIVATION_DEPLOYED_VERSION", str(receipt["deployed_version"])),
    ]


def rewrite_compose(text: str, *, production_image: str, tag: str, environment: list[tuple[str, str]]) -> str:
    """从 compose 备份文本改写候选 compose：生产镜像行换成候选标签，环境变量块插在 AUTO_SETUP 之后。备份里已有任何
    Claude 候选变量或实例 ID 即拒绝（恢复时直接拷回备份，这些变量随之撤回；不覆盖别人写的值）。"""

    old_image = f"    image: {production_image}\n    container_name: sub2apiplus\n"
    if text.count(old_image) != 1:
        raise ValueError("compose 备份中生产镜像行不唯一或不存在")
    if text.count(ENV_ANCHOR) != 1:
        raise ValueError("compose 备份中 AUTO_SETUP 锚点不唯一或不存在")
    present = sorted({prefix for prefix in CANDIDATE_ENV_PREFIXES for line in text.splitlines() if prefix in line})
    if present:
        raise ValueError(f"compose 备份里已经有候选或实例变量（{'、'.join(present)}）：备份必须是生产原样，拒绝改写")
    block = "".join(
        ["      # Claude 目标候选（VC-5，B5b-01b）：隔离实例 ARM64 上打开候选 Runtime，并落盘画像激活事实（声明身份取 VC-4 构建收据）\n"]
        + [f"      - {key}={value}\n" for key, value in environment]
    )
    text = text.replace(old_image, f"    image: {tag}\n    container_name: sub2apiplus\n")
    return text.replace(ENV_ANCHOR, ENV_ANCHOR + block)


def switch_compose(tag: str) -> Path:
    _binding()
    receipt = _build_receipt()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", tag):
        raise ValueError(f"候选镜像标签非法：{tag}")
    backup = Path(os.environ["COMPOSE_BACKUP"])
    target = Path(os.environ["COMPOSE_DIR"]) / "docker-compose.yml"
    text = rewrite_compose(
        backup.read_text(encoding="utf-8"),
        production_image=os.environ["PRODUCTION_IMAGE"],
        tag=tag,
        environment=candidate_environment(receipt, _candidate()),
    )
    target.write_text(text, encoding="utf-8")
    print("compose 已切换到", tag, "（Claude 候选开关、隔离实例 ARM64 与激活事实变量）")
    return target


def activation_check(fact_path: Path) -> dict[str, Any]:
    """服务写出的激活事实：合同与 event_id 复算（运行审计模块）、候选 Runtime、四项画像链摘要与部署身份等于构建收据。"""

    _binding()
    audit = _managed("claude_build_runtime_audit")
    receipt = _build_receipt()
    fact = audit.validate_fact(audit.load_fact(fact_path))
    expected = {
        "profile_mode": audit.PROFILE_MODE_CANDIDATE,
        "claude_version": str(receipt["target_version"]),
        "profile_digest": str(receipt["profile"]["profile_digest"]),
        "wire_digest": str(receipt["profile"]["wire_sha256"]),
        "release_digest": str(receipt["profile"]["release_sha256"]),
        "bundle_digest": str(receipt["profile"]["bundle_sha256"]),
        "approval_digest": "",
        "image_id": str(receipt["image"]["image_id"]),
        "image_reference": str(receipt["image"]["reference"]),
        "source_tree_sha256": str(receipt["source"]["tree_sha256"]),
        "build_id": str(receipt["build"]["build_id"]),
        "deployed_version": str(receipt["deployed_version"]),
    }
    drift = sorted(field for field, value in expected.items() if fact[field] != value)
    if drift:
        raise ValueError(f"候选网关的激活事实与 VC-4 构建收据不一致：{'、'.join(drift)}")
    print("激活事实：", {key: fact[key][:16] for key in ("profile_mode", "claude_version", "profile_digest", "release_digest", "image_id", "build_id")})
    return fact


def precheck() -> dict[str, Any]:
    """只读调候选 run 的正式入口门（VC-4 checkpoint、Store、构建收据重放、服务容器镜像、运行坐标覆盖与未封存）。"""

    _binding()
    flags = boundary_parameters()
    print("边界核对轮次参数：", " ".join(flags))
    capture = _managed("claude_upgrade_candidate_capture")
    admission = capture.formal_admission(_campaign(), _manifest(), _candidate())
    facts = admission.facts
    print("正式入口门通过：", {key: str(facts.get(key))[:24] for key in ("candidate_revision", "build_id", "image_id", "source_tree_sha256")})
    capture.require_admin_token_for(_managed("claude_upgrade")._campaign_jobs(_campaign(), _manifest(), "candidate", candidate_id=_candidate()))
    print("PRECHECK_DONE")
    return {"candidate_revision": facts.get("candidate_revision"), "image_id": facts.get("image_id")}


# ---------------------------------------------------------------------------
# 二、seal 链
# ---------------------------------------------------------------------------


def boundary_parameters() -> list[str]:
    """codex-final-wire-isolation 的入口旗标：四个参数文件键齐全、格式合法（与 parse_env 同一口径），生产提交在候选源码仓里。
    任一不成立即拒绝（零写入）。"""

    _binding()
    missing = [key for key, _flag in BOUNDARY_PARAMETERS if not os.environ.get(key)]
    if missing:
        raise ValueError(f"参数文件缺少边界核对 codex-final-wire-isolation 的轮次参数：{'、'.join(missing)}（见 env.example.sh）")
    for key, pattern, label in parse_env.BOUNDARY_FORMAT_KEYS:
        if not pattern.fullmatch(os.environ[key]):
            raise ValueError(f"{key} 必须是{label}")
    source_root = Path(str(_build_receipt()["source"]["root"]))
    commit = os.environ["PRODUCTION_COMMIT"]
    completed = subprocess.run(
        ["git", "-C", str(source_root), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"PRODUCTION_COMMIT {commit} 不在候选源码仓 {source_root} 里（boundary-check 的 structural 节要在这里比较）")
    return [item for key, flag in BOUNDARY_PARAMETERS for item in (flag, os.environ[key])]


def boundary_plan(check: str, output: Path) -> dict[str, Any]:
    """一项边界核对的批次（阶段项 candidate-seal）：零请求的第三方入口拒绝复用全部候选 Job；Codex final wire 隔离带真实请求
    确认与四个轮次参数、复用项为空（见模块说明）。失败后对账、重起 seal 链即按 N+1 逐字重派这一项。"""

    _binding()
    if check not in BOUNDARY_CHECKS:
        raise ValueError(f"未知边界核对：{check}（只有 {'、'.join(BOUNDARY_CHECKS)}）")
    timeout, wall = BOUNDARY_TIMEOUTS[check]
    argv = ["capture-candidate", "boundary-check", *_ref(), "--attempt-id", _attempt_id(), "--check", check]
    reuse: list[str] = []
    if check in LIVE_BOUNDARY_CHECKS:
        argv += ["--acknowledge-live-requests", *boundary_parameters()]
    else:
        reuse = _candidate_job_ids()
    argv += ["--max-wall-seconds", str(wall)]
    action = _action(f"candidate-boundary-{check}", "VC-5:capture-candidate-boundary-check", argv, "candidate-seal", timeout)
    return _plan([action], output, reuse=reuse)


def boundary_receipt_problems(files: dict[str, dict[str, Path | None]]) -> dict[str, str]:
    """已在的边界核对收据逐份经 f 段的 ``verify_boundary_receipt`` 核对（与 seal 同一判据）；返回不合法的那些及原因。"""

    checks = _managed("claude_upgrade_boundary_checks")
    attempt_root = Path(os.environ["A"])
    problems: dict[str, str] = {}
    for check, path in sorted(files["boundary_checks"].items()):
        if path is None:
            continue
        try:
            checks.verify_boundary_receipt(attempt_root, check, path)
        except Exception as error:  # noqa: BLE001 - 受管校验入口的拒绝一律记为不合法
            if type(error).__name__ != "ConfigurationError":
                raise
            problems[check] = str(error)
    return problems


def gate_overrides() -> dict[str, dict[str, str]]:
    """门禁执行主机覆盖（见模块说明）：两条本机门禁取 ``$GATE_ROOT/local`` 的门禁记录，目标平台门禁按 ``write_gate_json`` 现算。"""

    local = Path(os.environ["GATE_ROOT"]) / "local"
    overrides: dict[str, dict[str, str]] = {}
    for gate_id in LOCAL_GATES:
        path = local / f"{gate_id}.gate.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"本机门禁记录还没注入：{path}（vc5-all.sh 在启动前注入本机门禁六件套）")
        record = json.loads(path.read_text(encoding="utf-8"))
        overrides[gate_id] = {"host_id": str(record["host"]), "architecture": str(record["architecture"])}
    overrides[TARGET_GATE] = {
        "host_id": socket.gethostname().split(".")[0],
        "architecture": f"{platform.system().lower()}/{platform.machine()}",
    }
    return {gate: overrides[gate] for gate in sorted(overrides)}


def _client_files() -> dict[str, dict[str, Path | None]]:
    """场景清单登记的客户端收据与边界核对收据在 attempt 客户端证据目录里的文件（没有为 None）。"""

    seal = _managed("claude_upgrade_candidate_seal")
    plan_module = _managed("claude_upgrade_plan")
    manifest = _manifest()
    scenarios = plan_module.read_scenario_manifest(
        _managed("claude_upgrade_seal")._frozen_input(_campaign(), manifest, "target_scenarios"),
        expected_version=str(manifest["target_version"]),
        rules=tuple(manifest["required_rules"]),
        verify_source_spec=False,
    )
    directory = Path(os.environ["A"]) / seal.CLIENT_EVIDENCE_DIRNAME
    found: dict[str, dict[str, Path | None]] = {}
    for group, key in (("client_bindings", "required_client_bindings"), ("boundary_checks", "required_boundary_checks")):
        found[group] = {}
        for item in sorted(scenarios[key]):
            path = directory / f"{item}.json"
            if group == "boundary_checks":
                # 已存在的非法收据（含悬空符号链接、目录）必须交给正式校验入口拒绝，不能当成缺失自动重跑。
                found[group][str(item)] = path if path.exists() or path.is_symlink() else None
            else:
                found[group][str(item)] = path if path.is_file() and not path.is_symlink() else None
    return found


def _relative_binding(path: Path, base: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    import hashlib

    return {"path": path.relative_to(base).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _client_argv(files: dict[str, dict[str, Path | None]]) -> list[str]:
    argv: list[str] = []
    for client, path in sorted(files["client_bindings"].items()):
        if path is not None:
            argv += ["--client-evidence", f"{client}={path}"]
    return argv


def current_preview(files: dict[str, dict[str, Path | None]], overrides: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    """当前输入下的 seal 预览：attempt、客户端收据、门禁覆盖与当前工具身份都与预览草稿的代号键逐项相同的那一份；没有为
    None，多于一份即失败关闭（不猜选）。"""

    campaign, candidate = _campaign(), _candidate()
    data_root = Path(os.environ["D"]).resolve()
    attempt_path = Path(os.environ["A"]) / "attempt.json"
    upgrade = _managed("claude_upgrade")
    manifest = _manifest()
    store, store_campaign = _managed("claude_upgrade_store").open_campaign_store(campaign, manifest)
    expected = {
        "candidate_id": candidate,
        "attempt": _relative_binding(attempt_path, data_root),
        "tool_files_sha256": _managed("claude_upgrade_seal")._require_store_tool_current(store, store_campaign),
        "evidence_semantics_sha256": upgrade._tool_identity(include_git=False)["evidence_semantics_sha256"],
        "client_evidence": {
            group: {item: _relative_binding(path, data_root) for item, path in sorted(items.items()) if path is not None}
            for group, items in files.items()
        },
        "gate_execution_overrides": overrides,
    }
    seal = _managed("claude_upgrade_candidate_seal")
    matches = []
    for draft_path in sorted((campaign / "candidates" / candidate / seal.SEAL_DIRNAME).glob(f"*/{seal.SEAL_DRAFT_FILENAME}")):
        key = json.loads(draft_path.read_text(encoding="utf-8")).get("generation_key") or {}
        if all(key.get(field) == value for field, value in expected.items()):
            matches.append(draft_path.parent)
    if len(matches) > 1:
        raise ValueError(f"当前输入下有多份 seal 预览，拒绝猜选：{[path.name for path in matches]}")
    if not matches:
        return None
    preview = json.loads((matches[0] / seal.SEAL_PREVIEW_FILENAME).read_text(encoding="utf-8"))
    return {"generation": matches[0].name, "review_sha256": preview["review_sha256"], "closure": preview["closure"]}


def seal_state() -> dict[str, Any]:
    """seal 链的下一步：boundary_invalid（已在的边界核对收据不合法，人工审计）→ boundary（缺哪项边界核对）→ clients（缺客户端
    收据，暂停）→ preview（没有当前输入的预览）→ approve（等审核人批准）→ sealed（候选阶段结果已写，等 compare）。"""

    _binding()
    files = _client_files()
    overrides = gate_overrides()
    stage = Path(os.environ["CAPTURE_RESULT"])
    state: dict[str, Any] = {
        "attempt_id": _attempt_id(),
        # 按派发顺序（BOUNDARY_CHECKS）列出缺的边界核对；场景清单登记之外的不派发。
        "boundary_missing": [item for item in BOUNDARY_CHECKS if item in files["boundary_checks"] and files["boundary_checks"][item] is None],
        "boundary_invalid": {},
        "clients_missing": sorted(item for item, path in files["client_bindings"].items() if path is None),
        "client_argv": _client_argv(files),
        "gate_execution_overrides": overrides,
        "sealed": stage.is_file() and not stage.is_symlink(),
        "preview": None,
    }
    if not state["sealed"]:
        state["boundary_invalid"] = boundary_receipt_problems(files)
    if state["sealed"]:
        state["next"] = "compare"
    elif state["boundary_invalid"]:
        state["next"] = "boundary_invalid"
    elif state["boundary_missing"]:
        state["next"] = "boundary"
    elif state["clients_missing"]:
        state["next"] = "clients"
    else:
        state["preview"] = current_preview(files, overrides)
        if state["preview"] is None:
            state["next"] = "preview"
        elif state["preview"]["closure"].get("status") != "closed":
            state["next"] = "blocked"
        else:
            state["next"] = "approve"
    return state


def seal_plan(step: str, output: Path, *, approve: str | None = None, approved_by: str | None = None) -> dict[str, Any]:
    _binding()
    files = _client_files()
    missing = sorted(item for group in files.values() for item, path in group.items() if path is None)
    if missing:
        raise ValueError(f"客户端收据或边界核对收据还没就位：{'、'.join(missing)}")
    base = ["capture-candidate", "seal", *_ref(), "--attempt-id", _attempt_id(), *_client_argv(files),
            "--gate-execution-overrides", json.dumps(gate_overrides(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
    if step == "preview":
        if approve is not None or approved_by is not None:
            raise ValueError("seal 预览不接受批准参数")
        return _plan([_action("candidate-seal-preview", "VC-5:capture-candidate-seal-preview", base, "candidate-seal")], output, reuse=_candidate_job_ids())
    if step != "approve":
        raise ValueError(f"未知 seal 步骤：{step}")
    if not approve or not re.fullmatch(r"[0-9a-f]{64}", approve) or not approved_by or not approved_by.strip():
        raise ValueError("seal 批准必须给出审核人读过的 --approve-seal-sha256 与 --approved-by（驱动不替审核人批准）")
    # 批准与 compare 合并为一个零请求批次（两者都在 post-run-tooling 阶段项闭集内），与 Codex 同一做法。
    return _plan(
        [
            _action("candidate-seal-approve", "VC-5:capture-candidate-seal-approve", [*base, "--approve-seal-sha256", approve, "--approved-by", approved_by.strip()], "candidate-seal"),
            _action("compare", "VC-5:compare", ["compare", *_ref()], "compare"),
        ],
        output,
        reuse=_candidate_job_ids(),
    )


def compare_plan(output: Path) -> dict[str, Any]:
    _binding()
    return _plan([_action("compare", "VC-5:compare", ["compare", *_ref()], "compare")], output, reuse=_candidate_job_ids())


# ---------------------------------------------------------------------------
# 三、accept 链
# ---------------------------------------------------------------------------


def _comparison() -> dict[str, Any]:
    return _managed("claude_upgrade")._load_stage_result(_campaign(), "compare", _candidate())


def assert_plan(output: Path) -> dict[str, Any]:
    """assert 批次：真实 Claude builder（本轮受管树 ``$TOOLS`` 下比较收据登记的那个脚本）按比较收据登记的配置、输出、结果目录与
    评估基线跑（复用授权恒为 none）。"""

    _binding()
    builder = _comparison()["builder"]
    campaign = _campaign()
    action = {
        "action_id": "candidate-assert",
        "operation": "VC-5:assert",
        "timeout_seconds": float(ACTION_TIMEOUT_SECONDS),
        "command": [
            sys.executable, str(Path(os.environ["TOOLS"]) / Path(builder["script"]).name),
            "--config", str(campaign / builder["config"]["path"]), "--output", str(campaign / builder["output"]),
            "--results-dir", str(campaign / builder["results_dir"]), "--evaluation-baseline", str(builder["evaluation_baseline"]),
            "--reuse-authority", "none",
        ],
        "item_ids": ["assert-rules"],
    }
    return _plan([action], output, reuse=_candidate_job_ids())


def accept_plan(output: Path) -> dict[str, Any]:
    _binding()
    argv = ["accept", *_ref(), "--external-gate-root", os.environ["GATE_ROOT"], "--external-gate-receipt", GATE_RECEIPT_NAME]
    return _plan([_action("candidate-accept", "VC-5:accept", argv, "acceptance")], output, reuse=_candidate_job_ids())


def gate_subject() -> dict[str, Any]:
    """外部门禁 subject：五项候选身份取 ``candidate_gate_identity``（与 accept 同一处），其余取总计划与 Store 候选冻结事实。"""

    _binding()
    campaign, candidate = _campaign(), _candidate()
    manifest = _manifest()
    compare = _managed("claude_upgrade_compare")
    build, _binding_value = compare.load_candidate_build_receipt(campaign, manifest, candidate, int(os.environ["CANDIDATE_REVISION"]))
    identity = compare.candidate_gate_identity(campaign, manifest, candidate, build)
    store, store_campaign = _managed("claude_upgrade_store").open_campaign_store(campaign, manifest)
    baseline = _managed("claude_upgrade_baseline")
    revision, _current = baseline._store_revision(store, store_campaign)
    fact = baseline._store_candidate_fact(store, store_campaign, candidate, revision)
    if fact is None:
        raise ValueError(f"Store 没有候选 {candidate} 在 r{revision} 的冻结事实")
    subject = _managed("claude_upgrade_accept").expected_gate_subject(manifest, dict(fact["payload"]), identity)
    return {**subject, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}


# ---------------------------------------------------------------------------
# 四、候选采集续跑（B4-05b-1，vc5-claude-recover.sh 调用）
# ---------------------------------------------------------------------------

RECOVERY_PREVIEW_PLAN = "action-plan-vc5-recovery-preview.json"
RECOVERY_RUN_PLAN = "action-plan-vc5-recovery-run.json"
RECOVERY_PREVIEW_TIMEOUT_SECONDS = 1800


def _file_binding(path: Path) -> tuple[int, str]:
    import hashlib

    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


def recovery_plans(preview_path: Path, *, dry_run: bool = False) -> dict[str, dict[str, Any]]:
    """候选采集续跑的两份动作计划（与 Codex ``gen_vc5_recovery_plans.py`` 同一做法）：从本轮最新的、唯一动作为候选采集／
    续跑预览／续跑补跑的 VC-5 批次清单取解释器前缀、Campaign 目录与候选身份参数，用监督器后继协议同一组构造函数（按
    Claude 适配器，身份参数只有 ``--candidate-id``）生成 N+1 零请求恢复预览（``resume --rerun-failed --preview-recovery``）与
    N+2 按已批准预览的真实补跑（``resume --rerun-failed --recovery-preview <预览> --acknowledge-live-requests``）。两份计划的
    execute／reuse 与父批次相同（execute=candidate-run，reuse 为空）。只读 Campaign，不写账本、不派发；``dry_run`` 时不写计划。
    """

    _binding()
    supervisor = _managed("codex_upgrade_supervisor")
    adapter = _managed("upgrade_client_adapter").adapter_for("claude")
    campaign = _campaign().resolve(strict=True)
    candidate = _candidate()
    if not preview_path.is_absolute() or preview_path.is_symlink() or not preview_path.is_file():
        raise ValueError(f"恢复预览必须是可信绝对路径普通文件：{preview_path}")
    preview_binding = _file_binding(preview_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    if preview.get("phase") != "candidate" or preview.get("recovery_revision") is not None:
        raise ValueError("恢复预览不是候选采集非段模式的预览（恢复段 ar<k> 不走这条续跑）")
    if preview.get("candidate_id") != candidate:
        raise ValueError(f"恢复预览的候选 {preview.get('candidate_id')} 不是本轮候选 {candidate}")
    parents: list[tuple[int, Path, list[str], str, dict[str, str]]] = []
    revision = int(os.environ["CANDIDATE_REVISION"])
    for path in sorted((campaign / "control" / "vc" / "run-manifests").glob("*-vc-5.json")):
        if path.is_symlink():
            raise ValueError("恢复父批次清单不能是符号链接")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (payload.get("campaign_id"), payload.get("candidate_id"), payload.get("candidate_revision")) != (os.environ["NEW"], candidate, revision):
            continue
        try:
            prefix, parent_campaign, identity = supervisor.candidate_recovery_parent_identity(payload)
        except supervisor.SupervisorError:
            continue
        sequence = payload.get("batch_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ValueError("恢复父批次序号非法")
        parents.append((sequence, path, prefix, parent_campaign, identity))
    if not parents:
        raise ValueError("Campaign 没有本候选的候选采集或续跑批次清单，无法取候选身份参数")
    if len({row[0] for row in parents}) != len(parents):
        raise ValueError("恢复父批次序号不唯一")
    _sequence, parent_path, prefix, parent_campaign, identity = max(parents, key=lambda row: row[0])
    parent_binding = _file_binding(parent_path)
    if Path(parent_campaign).resolve(strict=True) != campaign:
        raise ValueError("父批次命令的 Campaign 目录与本轮不一致")
    if identity != {"--candidate-id": candidate}:
        raise ValueError(f"恢复父命令的候选身份参数不是本轮候选：{identity}")
    if prefix != _batch().managed_command([]):
        raise ValueError("恢复父命令不属于本轮受管工具（解释器或 claude_upgrade.py 路径不同）")

    def plan(action_id: str, command: list[str], timeout: int) -> dict[str, Any]:
        action = {
            "action_id": action_id,
            "operation": supervisor.CANDIDATE_RECOVERY_OPERATION,
            "timeout_seconds": timeout,
            "command": command,
            "item_ids": ["candidate-run"],
        }
        return _batch().action_plan([action], phase="VC-5")

    plans = {
        RECOVERY_PREVIEW_PLAN: plan(
            supervisor.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID,
            supervisor.candidate_recovery_preview_command(prefix, parent_campaign, identity, adapter=adapter),
            RECOVERY_PREVIEW_TIMEOUT_SECONDS,
        ),
        RECOVERY_RUN_PLAN: plan(
            supervisor.CANDIDATE_RECOVERY_RUN_ACTION_ID,
            supervisor.candidate_recovery_run_command(prefix, parent_campaign, identity, str(preview_path), adapter=adapter),
            RUN_TIMEOUT_SECONDS,
        ),
    }
    if _file_binding(preview_path) != preview_binding or _file_binding(parent_path) != parent_binding:
        raise ValueError("恢复预览或父批次在解析期间发生变化")
    if dry_run:
        print("恢复计划只读预检通过，未写入输出目录；父批次", parent_path.name)
        return plans
    for name, payload in plans.items():
        _write_plan(Path(os.environ["W"]) / name, payload)
    print("recovery plans ->", os.environ["W"], sorted(plans), "parent", parent_path.name,
          "execute", preview.get("execute_job_ids"), "reuse", preview.get("reuse_job_ids"))
    return plans


# ---------------------------------------------------------------------------
# 五、暂停与摘要
# ---------------------------------------------------------------------------


def pause(reason: str, next_step: str) -> Path:
    """写 ``$RUNROOT/vc5-pause.json``：``vc5-all.sh`` 读到它就打印 VC5_ALL_PAUSED 并退出 0（审核人处置后重起续跑）。"""

    path = Path(os.environ["RUNROOT"]) / PAUSE_FILENAME
    path.write_text(json.dumps({"reason": reason, "next": next_step}, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"=== 暂停：{reason}；下一步：{next_step}")
    return path


def summary() -> None:
    _binding()
    upgrade = _managed("claude_upgrade")
    campaign, candidate = _campaign(), _candidate()
    for stage in ("capture-candidate", "compare", "accept"):
        try:
            result = upgrade._load_stage_result(campaign, stage, candidate)
            print(f"{stage}：{result.get('status')}")
        except Exception as error:  # noqa: BLE001 - 摘要只报告读侧结论
            print(f"{stage}：{str(error)[:120]}")
    checkpoint = _managed("claude_upgrade_vc").vc_checkpoint_path(campaign, "VC-5", revision=int(os.environ["CANDIDATE_REVISION"]))
    print(f"VC-5 checkpoint：{'已写' if checkpoint.is_file() else '未写'}（{checkpoint}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 的 VC-5 派发（B5b-01b）")
    sub = parser.add_subparsers(dest="action", required=True)
    target = sub.add_parser("run-plan")
    target.add_argument("--output", type=Path, required=True)
    target.add_argument("--max-wall-seconds", type=int, default=RUN_TIMEOUT_SECONDS)
    sub.add_parser("switch-compose").add_argument("--tag", required=True)
    sub.add_parser("activation-check").add_argument("--fact", type=Path, required=True)
    sub.add_parser("precheck")
    target = sub.add_parser("boundary-plan")
    target.add_argument("--check", required=True)
    target.add_argument("--output", type=Path, required=True)
    sub.add_parser("boundary-parameters")
    sub.add_parser("seal-state")
    target = sub.add_parser("seal-plan")
    target.add_argument("--step", required=True, choices=("preview", "approve"))
    target.add_argument("--output", type=Path, required=True)
    target.add_argument("--approve-seal-sha256")
    target.add_argument("--approved-by")
    sub.add_parser("compare-plan").add_argument("--output", type=Path, required=True)
    sub.add_parser("gate-overrides")
    sub.add_parser("assert-plan").add_argument("--output", type=Path, required=True)
    sub.add_parser("accept-plan").add_argument("--output", type=Path, required=True)
    sub.add_parser("gate-subject").add_argument("--output", type=Path, required=True)
    target = sub.add_parser("pause")
    target.add_argument("--reason", required=True)
    target.add_argument("--next", dest="next_step", required=True)
    target = sub.add_parser("recovery-plans")
    target.add_argument("--preview", type=Path, required=True)
    target.add_argument("--dry-run", action="store_true")
    sub.add_parser("summary")
    args = parser.parse_args(argv)
    try:
        if args.action == "run-plan":
            run_plan(args.output, args.max_wall_seconds)
        elif args.action == "switch-compose":
            switch_compose(args.tag)
        elif args.action == "activation-check":
            activation_check(args.fact)
        elif args.action == "precheck":
            precheck()
        elif args.action == "boundary-plan":
            boundary_plan(args.check, args.output)
        elif args.action == "boundary-parameters":
            print(" ".join(boundary_parameters()))
        elif args.action == "seal-state":
            print(json.dumps(seal_state(), ensure_ascii=False, sort_keys=True))
        elif args.action == "seal-plan":
            seal_plan(args.step, args.output, approve=args.approve_seal_sha256, approved_by=args.approved_by)
        elif args.action == "compare-plan":
            compare_plan(args.output)
        elif args.action == "gate-overrides":
            print(json.dumps(gate_overrides(), ensure_ascii=False, sort_keys=True))
        elif args.action == "assert-plan":
            assert_plan(args.output)
        elif args.action == "accept-plan":
            accept_plan(args.output)
        elif args.action == "gate-subject":
            _binding()
            args.output.write_text(json.dumps(gate_subject(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            args.output.chmod(0o600)
            print("gate subject ->", args.output)
        elif args.action == "recovery-plans":
            recovery_plans(args.preview, dry_run=args.dry_run)
        elif args.action == "pause":
            pause(args.reason, args.next_step)
        else:
            summary()
    except (ValueError, KeyError, OSError) as error:
        print(f"Claude VC-5 派发拒绝：{error}", file=sys.stderr)
        return 3
    except Exception as error:  # noqa: BLE001 - 受管模块的配置错误一律按拒绝报出
        if type(error).__name__ in {"ConfigurationError", "ClaudeOrchestratorPending", "VCArtifactError"}:
            print(f"Claude VC-5 派发拒绝：{error}", file=sys.stderr)
            return 3
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
