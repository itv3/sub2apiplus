"""Claude 评估器（升级控制面设计 B4-03）的 2.1.226 官方证据夹具适配。

本机只读的 2.1.226 官方录制：环境变量 ``CLAUDE_2_1_226_RELAY_FIXTURE`` 指向
``local-analysis/fw-f/claude-code-2.1.226/complete-v21-78fae770cbb5``（77 个 probe），同级目录
``thinking-display-supplement-v1-94f7d00d704f`` 是 thinking.display 补充取证。两者按封存链的同一口径合成评估用 manifest：

1. 逐 probe（``attempts/<probe>/attempt-001``）用 ``claude_upgrade_evidence.relay_probe_view`` 取有效连接视图，再用
   2.1.285 的证据标签计划（``claude_upgrade_evidence_labels_2_1_285.json``，probe 集合覆盖 2.1.226 的 v4 场景目录）逐连接
   贴标签，得到 capture manifest。补充取证只收 ``v4-thinking-display-omitted``：``v4-thinking-display-summarized`` 的中继
   清单缺 ``dimension_evidence.actual_wire_inventory``，封存链的视图建不起来（FW-F 的 40 条规则本来也不用补充证据）；
2. 按封存深度扫描的口径给每个 probe 目录的文件算 sha256／字节数作封存绑定（只读 ``relay-manifest.json``、``relay/``、
   ``results/debug.log``、``tls-clienthello.pcap``，不碰凭据目录 ``state/``、``recovery/``）；
3. ``claude_rule_assertion.bind_capture_manifest`` 合成评估用 manifest（补绑上游响应字节与中继连接索引）。

证据根是两份录制的共同上级目录 ``claude-code-2.1.226``，artifact 路径形如
``complete-v21-78fae770cbb5/attempts/<probe>/attempt-001/relay/conn001.client_to_upstream.bin``。不联网，不写录制目录。
断言画像由仓库里的判据模板库、2.1.226 RequiredRules、2.1.285 场景清单与 catalogdata 里 2.1.226 的 Release
Profile／Wire 生成（与仓库基线画像 ``claude_rule_expectations_2_1_226.json`` 同一组输入）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_plan as plan_module

FIXTURE_ENV = "CLAUDE_2_1_226_RELAY_FIXTURE"
TARGET_VERSION = "2.1.226"
LABEL_VERSION = "2.1.285"
CAPTURE_ID = "complete-v21-78fae770cbb5"
SUPPLEMENT_DIRECTORY = "thinking-display-supplement-v1-94f7d00d704f"
SUPPLEMENT_PROBES = ("v4-thinking-display-omitted",)
TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
LABELS_PATH = TOOL_ROOT / "claude_upgrade_evidence_labels_2_1_285.json"
SCENARIOS_PATH = TOOL_ROOT / "claude_upgrade_scenarios_2_1_285.json"
REQUIRED_RULES_PATH = TOOL_ROOT / "claude_required_rules_2_1_226.json"
TEMPLATES_PATH = TOOL_ROOT / "claude_rule_expectations_templates.json"
PROFILE_PATH = TOOL_ROOT / "claude_rule_expectations_2_1_226.json"
CATALOG_ROOT = REPOSITORY_ROOT / "backend" / "internal" / "officialegress"
RELEASE_CATALOG_PATH = CATALOG_ROOT / "catalogdata" / "claude" / "release-catalog.json"
# 封存深度扫描里本夹具需要的文件：中继清单、relay/ 下全部文件、调试日志与 P 通道 pcap。
SEALED_RELATIVE = ("relay-manifest.json", "results/debug.log", "tls-clienthello.pcap")


def fixture_root() -> Path | None:
    """complete-v21 录制目录（未设置环境变量时为 None）。"""

    value = os.environ.get(FIXTURE_ENV)
    return Path(value).resolve() if value else None


def evidence_root(campaign_root: Path) -> Path:
    """证据根：complete-v21 与补充取证的共同上级目录。"""

    return Path(campaign_root).resolve().parent


def probe_attempts(campaign_root: Path) -> list[tuple[str, Path]]:
    """参与评估的 probe 与其 attempt 目录：complete-v21 全部 probe，加补充取证里登记的 probe。"""

    attempts = [(item.name, item / "attempt-001") for item in sorted((Path(campaign_root) / "attempts").iterdir())]
    supplement = evidence_root(campaign_root) / SUPPLEMENT_DIRECTORY / "attempts"
    attempts.extend((probe, supplement / probe / "attempt-001") for probe in SUPPLEMENT_PROBES)
    return attempts


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def release_artifacts(version: str = TARGET_VERSION) -> tuple[dict[str, Any], dict[str, Any]]:
    """catalogdata 里目标版本 Release 的 Profile 与 Wire 制品（按 Release 目录登记的路径与摘要读取并核对）。"""

    catalog = _load(RELEASE_CATALOG_PATH)
    release = next(item for item in catalog["releases"] if item["version"] == version)
    loaded = []
    for key in ("profile", "wire"):
        path = CATALOG_ROOT / release[key]["path"]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != release[key]["sha256"]:
            raise AssertionError(f"Release {key} 制品摘要与目录登记不一致：{path}")
        loaded.append(json.loads(raw))
    return loaded[0], loaded[1]


def build_profile(templates: Any = None) -> dict[str, Any]:
    """用仓库输入生成 2.1.226 断言画像（模板库可替换，开发期按子集试跑时用）。"""

    required = _load(REQUIRED_RULES_PATH)
    profile, wire = release_artifacts()
    return assertion.build_expectations(
        claude_version=TARGET_VERSION,
        ledger=required,
        scenarios_payload=_load(SCENARIOS_PATH),
        templates=templates if templates is not None else _load(TEMPLATES_PATH),
        release_profile=profile,
        release_wire=wire,
        rule_manifest=required,
    )


def label_campaign(campaign_root: Path) -> dict[str, Any]:
    """参与评估的 probe 按 2.1.285 标签计划贴标签，返回 capture manifest（目标版本记 2.1.226）。"""

    scenarios = _load(SCENARIOS_PATH)
    labels = plan_module.load_evidence_labels(LABELS_PATH, expected_version=LABEL_VERSION, scenario_payload=scenarios)
    job_rules = {entry["job_id"]: entry["rules"] for entry in labels["document"]["entries"]}
    # 只取 official Job：候选 Job（B4-01）镜像同一 probe ID，按 probe 单键会把官方证据错配到候选标签。
    probe_job = {
        step["probe_id"]: job["id"] for job in scenarios["capture_jobs"] if job["phase"] == "official" for step in job["steps"]
    }
    base = evidence_root(campaign_root)
    artifacts: list[dict[str, Any]] = []
    for probe, attempt in probe_attempts(campaign_root):
        view = evidence.relay_probe_view(attempt, base=base)
        view["probe_id"] = probe
        job = probe_job[probe]
        artifacts.extend(evidence._label_artifacts(job, view, labels["plans"][job][probe], job_rules[job]))
    artifacts.sort(key=lambda item: (item["path"], json.dumps(item["labels"], sort_keys=True)))
    return {
        "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
        "campaign_id": CAPTURE_ID,
        "target_version": TARGET_VERSION,
        "attempt": {"attempt_id": "attempt-001"},
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "label_mismatches": [],
    }


def sealed_bindings(campaign_root: Path) -> dict[str, dict[str, Any]]:
    """每个 probe 目录里中继清单、relay/ 全部文件、调试日志与 pcap 的 sha256／字节数（相对证据根）。"""

    base = evidence_root(campaign_root)
    bindings: dict[str, dict[str, Any]] = {}
    for _probe, attempt in probe_attempts(campaign_root):
        files = [attempt / relative for relative in SEALED_RELATIVE] + sorted((attempt / "relay").iterdir())
        for path in files:
            if path.is_file() and not path.is_symlink():
                raw = path.read_bytes()
                bindings[path.relative_to(base).as_posix()] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return bindings


def write_evaluation_manifest(campaign_root: Path, output: Path) -> dict[str, Any]:
    manifest = assertion.bind_capture_manifest(label_campaign(campaign_root), sealed_bindings(campaign_root), capture_id=CAPTURE_ID)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
