#!/usr/bin/env python3
"""VC-4 驱动的客户端绑定（Claude 升级 B3-04a）：续跑凭证 ``vc4_resume.py``、实现门禁 ``implementation_gates.py`` 与 Claude
的采集主机小步骤 ``vc4-requirements.sh`` 按本轮客户端取同一组成员，不写「如果是某客户端」的分支。

为什么要有：VC-4 驱动原来按 Codex 写死——导入 ``codex_upgrade``、用它的 VC-3 门禁需求、进程内调 ``open_candidate_revision``、
认 Codex 候选构建收据合同、走 Codex 的实现测试复用。Claude 的 VC-4 由 ``claude_upgrade`` 承接（设计 B3-04 第三步）：门禁
需求由 VC-2／VC-3 阶段结果确定性推出，候选 revision 有编排器与 Store 两份登记、只能经 ``claude_upgrade revision-open``
写，候选构建收据是客户端中立合同 ``official-client-upgrade-candidate-build-receipt/v1``。

绑定按客户端表（参数 ``CLIENT``，缺省 codex）在 ``BINDINGS`` 里查：

* ``upgrade``：本轮客户端的编排器模块（客户端表 ``orchestrator_module``）。两编排器同名同义的服务面成员——总计划加载
  ``load_campaign_manifest``、目录树摘要 ``_directory_tree_digest`` 与跳过目录 ``SKIP_DIRECTORIES``、指纹 ``_fingerprint``、
  当前候选 revision ``_current_candidate_revision_record``——由调用方直接经它取：Codex 即 ``codex_upgrade``，调用与测试
  打桩和改造前相同；
* 差异成员（两个绑定类各给一份）：门禁需求 ``requirements``、revision 开启 ``open_revision``、前序构建收据合同
  ``previous_build_receipt``、本候选属于当前 revision 的核对 ``require_current_revision``、实现测试复用开关
  ``implementation_reuse``、需求预览 ``requirements_preview``（只 Claude 有）。Codex 一侧逐个转调改造前直接调用的那个
  ``codex_upgrade`` 成员（按属性在调用时取，打桩照样生效）。

实现测试复用（r≥2 只重跑受影响的门禁或整体复用）只有 Codex：共享的 ``codex_upgrade_vc_receipt`` 复用证明只认 Codex 构建
收据。Claude 一律全量重跑（登记为后续项），所以 Claude 绑定的 ``implementation_reuse`` 为假：``early-test-mode`` 恒为
``full``，重跑计划不读前序构建收据。

子命令（采集主机小步骤 ``vc4-requirements.sh`` 用）：

* ``requirements --output <文件>``：经编排器 CLI 做门禁需求预览（``plan-candidate-gates`` 只给 Campaign 与候选：零写入、
  退出码 2），核对状态、模式、Campaign、候选、零请求，需求要能按共享合同复算、并与本模块只读推出的需求逐字相同，再写
  0600 文件（已存在时内容必须逐字相同：同一轮重跑收敛，另一份即拒绝）。Codex 的门禁需求由本机候选链在采集主机现算，
  这里拒绝。

退出码：0 成功；3 拒绝。只用标准库与受管模块；同目录的客户端表经 ``parse_env.client_table`` 按路径加载（与参数解析同一份
模块对象）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_env  # noqa: E402

from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts  # noqa: E402

# 需求预览的合同（claude_upgrade_candidate 的命令结果）：状态与模式。
PREVIEW_STATUS = "awaiting_gate_mapping"
PREVIEW_MODE = "gate_requirements"
# 编排器 CLI 的单次上限（revision-open 与需求预览都只读写少量文件）。
CLI_TIMEOUT_SECONDS = 1800


def client() -> str:
    """本轮客户端：参数 ``CLIENT``（缺省 codex），不认识即拒绝（``ClientTableError`` 是 ``ValueError``）。"""

    table = parse_env.client_table()
    return table.resolve_client(os.environ.get(table.CLIENT_PARAM))


class _Binding:
    """两客户端共用的部分：本轮 Campaign 坐标与编排器模块（客户端表 ``orchestrator_module``）。"""

    client = ""
    implementation_reuse = False

    def __init__(self) -> None:
        self.row = parse_env.client_table().spec(self.client)
        self.upgrade = importlib.import_module(self.row["orchestrator_module"])

    @staticmethod
    def campaign() -> Path:
        """本轮 Campaign 目录（两客户端同一布局：数据根 evidence/campaigns/<NEW>）。"""

        return Path(os.environ["D"]) / "evidence" / "campaigns" / os.environ["NEW"]


class CodexBinding(_Binding):
    """Codex：逐个转调改造前直接调用的 ``codex_upgrade`` 成员（调用时按属性取，测试打桩照样生效）。"""

    client = "codex"
    implementation_reuse = True

    def requirements(self, campaign: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
        classification = self.upgrade._load_stage_result(campaign, "classify")
        _, _, requirements = self.upgrade._load_vc3_gate_requirements(campaign, manifest, classification)
        return requirements

    def open_revision(self, campaign: Path, candidate_id: str, *, initial: bool, supersedes: str | None) -> dict[str, Any]:
        return self.upgrade.open_candidate_revision(
            argparse.Namespace(campaign_dir=campaign, candidate_id=candidate_id, initial=initial, supersedes=supersedes)
        )

    def previous_build_receipt(self, payload: Any) -> dict[str, Any]:
        return artifacts.validate_candidate_build_receipt(payload)

    def require_current_revision(self, campaign: Path, manifest: Mapping[str, Any], candidate_id: str, *, action: str) -> tuple[int, Any]:
        return self.upgrade._require_candidate_in_current_revision(campaign, manifest, candidate_id, action=action)

    def requirements_preview(self, campaign: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
        raise ValueError("Codex 的门禁需求由本机候选链（local-candidate-chain.sh）在采集主机现算，没有需求预览步骤")


class ClaudeBinding(_Binding):
    """Claude：门禁需求由 VC-2／VC-3 阶段结果推出，revision 与需求预览经 ``claude_upgrade`` CLI，构建收据认客户端中立合同。"""

    client = "claude"
    implementation_reuse = False

    def requirements(self, campaign: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """与 record-candidate-build 的入口门同一推法（``claude_upgrade_candidate.derive_gate_requirements``），只读。"""

        from tools.official_client_capture import claude_upgrade_candidate

        classification = self.upgrade._load_stage_result(campaign, "classify", _verified_campaign_manifest=manifest)
        staged = self.upgrade._load_stage_result(campaign, "stage-profile", _verified_campaign_manifest=manifest)
        return claude_upgrade_candidate.derive_gate_requirements(manifest, classification, staged)

    def cli(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        """受管 Python 直接调编排器脚本（与当前进程同一解释器、同一环境）；返回退出码与 stdout 的 JSON 结果。"""

        script = Path(self.upgrade.__file__).resolve()
        completed = subprocess.run(
            [sys.executable, str(script), *argv], capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL
        )
        if completed.returncode not in {0, 2}:
            raise ValueError(f"claude_upgrade {argv[0]} 失败（退出码 {completed.returncode}）：{completed.stderr.strip()[-800:]}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ValueError(f"claude_upgrade {argv[0]} 的输出不是 JSON：{error}") from error
        if not isinstance(result, dict):
            raise ValueError(f"claude_upgrade {argv[0]} 的输出不是对象")
        return completed.returncode, result

    def open_revision(self, campaign: Path, candidate_id: str, *, initial: bool, supersedes: str | None) -> dict[str, Any]:
        """编排器与 Store 两份登记只能经 ``revision-open``（共享流程之外还写 Store 侧）；同一参数重跑幂等。"""

        mode = ["--initial"] if initial else ["--supersedes", str(supersedes)]
        code, result = self.cli(["revision-open", "--campaign-dir", str(campaign), "--candidate-id", candidate_id, *mode])
        if code != 0 or result.get("status") != "complete" or result.get("candidate_id") != candidate_id:
            raise ValueError(f"revision-open 没有完成：退出码 {code}，状态 {result.get('status')}，候选 {result.get('candidate_id')}")
        return result

    def previous_build_receipt(self, payload: Any) -> dict[str, Any]:
        return artifacts.validate_official_client_candidate_build_receipt(payload)

    def require_current_revision(self, campaign: Path, manifest: Mapping[str, Any], candidate_id: str, *, action: str) -> tuple[int, Any]:
        """与 Codex ``_require_candidate_in_current_revision`` 同一组判据：两份登记一致的当前 revision 属于本候选，本候选
        没有作废与取代标记（同名文件），计时账本 active。"""

        from tools.official_client_capture import claude_upgrade_candidate

        revision, record = self.upgrade._require_candidate_revision(campaign, manifest, action=action)
        if record is None or record.get("candidate_id") != candidate_id:
            current = record.get("candidate_id") if record else None
            raise ValueError(f"{action} 拒绝：候选 {candidate_id} 不属于当前 revision r{revision}（当前候选 {current}）")
        root = campaign / "candidates" / candidate_id
        for name, meaning in (("invalidation.json", "已作废"), ("superseded-by.json", "已被取代")):
            marker = root / name
            if marker.exists() or marker.is_symlink():
                raise ValueError(f"{action} 拒绝：候选 {candidate_id} {meaning}（存在 {name}），只读")
        claude_upgrade_candidate.require_timing_ledger_active(campaign, manifest, action=action)
        return revision, record

    def requirements_preview(self, campaign: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
        """需求预览（零写入）并逐项核对；返回预览结果（写进 $RUNROOT 交本机候选链）。"""

        code, result = self.cli(["plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", candidate_id])
        if (code, result.get("status"), result.get("mode")) != (2, PREVIEW_STATUS, PREVIEW_MODE):
            raise ValueError(f"需求预览没有给出门禁需求：退出码 {code}，状态 {result.get('status')}，模式 {result.get('mode')}")
        if (result.get("campaign_id"), result.get("candidate_id"), result.get("live_request_count")) != (
            manifest["campaign_id"], candidate_id, 0
        ):
            raise ValueError("需求预览的 Campaign、候选或请求计数与本轮不一致")
        requirements = artifacts.validate_gate_requirements(result.get("requirements"))
        if result.get("requirements_sha256") != requirements["requirements_sha256"]:
            raise ValueError("需求预览登记的需求摘要与需求本身不一致")
        if result.get("requirement_sha256s") != {str(row["gate_id"]): artifacts.digest(row) for row in requirements["requirements"]}:
            raise ValueError("需求预览的逐项需求摘要无法复算")
        if requirements != self.requirements(campaign, manifest):
            raise ValueError("需求预览与按 VC-2／VC-3 阶段结果只读推出的门禁需求不同")
        return result


BINDINGS: Mapping[str, type[_Binding]] = {"codex": CodexBinding, "claude": ClaudeBinding}


def current() -> _Binding:
    """本轮客户端的 VC-4 绑定（客户端表与 ``BINDINGS`` 的客户端集合必须相同）。"""

    table = parse_env.client_table()
    if set(BINDINGS) != set(table.CLIENTS):
        raise ValueError(f"VC-4 绑定与客户端表的客户端不一致：{sorted(BINDINGS)} ≠ {sorted(table.CLIENTS)}")
    return BINDINGS[client()]()


def write_once_or_same(path: Path, payload: Mapping[str, Any]) -> bool:
    """写 0600 文件（indent 2、键排序、尾换行）；已存在时字节必须相同。返回是否新写。"""

    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ValueError(f"已有一份不同的门禁需求文件：{path}（同一轮重跑才收敛；换轮次先清理 RUNROOT）")
        return False
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VC-4 驱动的客户端绑定（B3-04a）")
    sub = parser.add_subparsers(dest="action", required=True)
    requirements = sub.add_parser("requirements", help="门禁需求预览（只 arm64-preview 来源的客户端）写进本轮 RUNROOT")
    requirements.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        row = parse_env.client_table().spec(client())
        if row["vc4_requirements_source"] != "arm64-preview":
            raise ValueError(f"本轮客户端（{row['client']}）的门禁需求来源是 {row['vc4_requirements_source']}，不经采集主机预览")
        binding = current()
        output = args.output
        if not output.is_absolute() or output.parent.resolve(strict=True) != Path(os.environ["RUNROOT"]).resolve(strict=True):
            raise ValueError("门禁需求文件只能写在本轮 RUNROOT 下")
        campaign = binding.campaign()
        manifest = binding.upgrade.load_campaign_manifest(campaign)
        preview = binding.requirements_preview(campaign, manifest, os.environ["CAND"])
        written = write_once_or_same(output, preview)
        print(
            f"VC4_REQUIREMENTS {'已写' if written else '已存在且相同'} {output}"
            f" requirements_sha256={preview['requirements_sha256']} gates={len(preview['requirements']['requirements'])}"
            f" revision=r{preview.get('candidate_revision')}"
        )
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, artifacts.VCArtifactError) as error:
        print(f"VC-4 门禁需求预览拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
