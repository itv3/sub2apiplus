"""收尾工具包里“产出文档与事实”的脚本（``tools/ci/closeout/``）的单元测试，全部用合成数据。

覆盖：
* ``gen_terminal_facts.py``：合成仓库（阶段收据、审计索引、Runtime Catalog、晋升中间快照）＋合成证据根（Campaign、检查点、VC-6 收据、激活与退休切换阶段凭证）
  生成终态事实；关键字段取自哪里、各类前置不符（Catalog 未裁剪、版本对不上、摘要对不上）直接退出、已有输出拒绝覆盖；
* ``render_guide.py``：合成指南按终态事实改写五处 Active 绑定位置，第二部分摘要不变，变化规则与迁移清单交叉核对，回滚标签推导与一致性，
  ``--behavior-summary`` 必填并支持 ``@文件``，``--part2-mode`` 影响横幅措辞，输出已存在拒绝覆盖；
* ``finalize_part4.py``：经验条目并入、时间线整句插入／替换占位、第二部分不变的自检、找不到唯一锚点就失败且不写文件；
* ``update_active_receipt.py``：指南 4.9 小节值表按终态事实重写，幂等，表结构被改过／小节缺失／官方来源不唯一都报错，
  ``--verify-repo`` 调用仓库里 evidence_index 的校验函数（桩模块）并把失败与“旧版跳过”区分开；
* ``part2_minimal.py``（可选、已作废的第二部分改写）：占位符缺参数一次列全、旧文命中次数不是 1 失败、``--edits-file`` 覆盖内置表；
* ``apply_postpromo_fixes.py``：两个锚点各命中一次才改、幂等；
* ``rebind_guide_ch2.py``：合成 git 仓库里把清单与检查器绑定的旧第二章摘要换成新摘要，差异恰为一行摘要替换。

临时目录都建在系统临时目录；不依赖 ARM64、网络与 ``local-analysis``。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
KIT = REPO_ROOT / "tools" / "ci" / "closeout"
GIT_ENV = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto",
           "GIT_CONFIG_VALUE_1": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_closeout_render_test", KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def run_script(name: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(KIT / name), *args], capture_output=True, text=True, cwd=cwd, check=False,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


# 合成的“下一轮”：目标 9.9.0 / 基线 9.8.0 / 退休 9.7.0（不是任何真实 Codex 版本）
T, B, R = "9.9.0", "9.8.0", "9.7.0"
CAMPAIGN = "c99900-formal-vc1-r1-20990101t000000z"
CANDIDATE = "c99900-candidate-r1"
PREFIX = "CODEX_CLI_9008_TO_9009"   # 前缀＝tag_of(基线)_TO_tag_of(目标)：9.8.0 -> 9008，9.9.0 -> 9009
ATTEMPT = "20990101T000000Z-aaaaaaaaaaaaaaaa"
MAINT = "docs/egress/maintenance"
RUNTIME = "backend/internal/officialegress/catalogdata/runtime"
D_T, D_B = "d" * 64, "e" * 64


# ---------------------------------------------------------------------------
# gen_terminal_facts：合成仓库与证据根
# ---------------------------------------------------------------------------
def build_repo(root: Path, *, retired_still_in_catalog: bool = False) -> Path:
    stage = {"CATALOG_PROMOTION_RECEIPT": {"inventory": [{"path": "catalogdata/runtime/snapshot-catalogs/" + "a" * 64 + ".json"}]},
             "POST_PROMOTION_GATE_RECEIPT": {"k": 1}, "PRODUCTION_ACTIVATION_RECEIPT": {"k": 2}, "RUNTIME_PROFILE_REMOVAL_RECEIPT": {"k": 3}}
    for name, body in stage.items():
        write(root / MAINT / f"{PREFIX}_{name}.json", json.dumps(body))
    audit = {"target_version": T, "baseline_version": B, "campaign_id": CAMPAIGN, "identity_sha256": "9" * 64,
             "totals": {"file_count": 12, "ledgers_by_kind": {"campaign_run": 1, "supervisor_run": 2, "upgrade_timing_ledger": 3}}}
    write(root / MAINT / f"{PREFIX}_AUDIT_INDEX.json", json.dumps(audit))
    profile_t, profile_b, profile_r = b'{"p":"T"}', b'{"p":"B"}', b'{"p":"R"}'
    final_name, middle_name = "b" * 64, "a" * 64
    for rel, data in ((f"profiles/{T}/active.json", profile_t), (f"profiles/{B}/previous.json", profile_b), (f"profiles/{R}/retired.json", profile_r)):
        write(root / RUNTIME / rel, data)
    rows = {T: {"version": T, "file": f"profiles/{T}/active.json", "blob_sha256": sha(profile_t), "digest": D_T},
            B: {"version": B, "file": f"profiles/{B}/previous.json", "blob_sha256": sha(profile_b), "digest": D_B},
            R: {"version": R, "file": f"profiles/{R}/retired.json", "blob_sha256": sha(profile_r), "digest": "f" * 64}}
    final_rows = [rows[T], rows[B]] + ([rows[R]] if retired_still_in_catalog else [])
    write(root / RUNTIME / f"snapshot-catalogs/{final_name}.json", json.dumps({"snapshots": final_rows}))
    write(root / RUNTIME / f"snapshot-catalogs/{middle_name}.json", json.dumps({"snapshots": [rows[T], rows[B], rows[R]]}))
    write(root / RUNTIME / "release-catalog.json", json.dumps({
        "release_graph": {"path": "catalogdata/runtime/release-graph.json"},
        "snapshot_catalog": {"path": f"catalogdata/runtime/snapshot-catalogs/{final_name}.json"}}))
    write(root / RUNTIME / "release-graph.json", json.dumps({"nodes": [
        {"mode": "active", "build": {"version": T, "id": f"codex-{T}-official-r1-r1-0123456789ab"}},
        {"mode": "previous", "build": {"version": B, "id": f"codex-{B}-official-r1-r1-0123456789ab"}}]}))
    return root


def build_evidence(root: Path) -> tuple[Path, Path]:
    """返回（证据根, 轮次 vc6 目录）。"""

    campaign = root / "data" / "evidence" / "campaigns" / CAMPAIGN
    body = json.dumps({"created_at_utc": "2099-01-01T00:00:00.000Z"})
    write(campaign / "campaign.json", body)
    write(campaign / "campaign.sha256", sha(body) + "\n")
    items = [{"item_id": "production-activation", "status": "complete"}, {"item_id": f"retire-{R}", "status": "complete"},
             {"item_id": "rollback-verification", "status": "complete"}, {"item_id": "other", "status": "complete"}]
    write(campaign / "canonical/checkpoints/00000001.json", json.dumps({"checkpoint_sequence": 1, "phase": "done", "items": items[:1]}))
    write(campaign / "canonical/checkpoints/00000007.json", json.dumps({"checkpoint_sequence": 7, "phase": "done", "items": items}))
    receipts = campaign / "control/vc/receipts" / CANDIDATE
    write(receipts / "vc6-completion.json", json.dumps({"issued_at_utc": "2099-01-02T03:04:05.678Z", "receipt_digest": "c" * 64, "subject": {
        "campaign_id": CAMPAIGN, "target_version": T, "baseline_version": B, "attempt_id": "ATT", "upgrade_id": "UP"}}))
    write(receipts / "cleanup-decision.json", json.dumps({"receipt_digest": "1" * 64, "assertions": {"decision": "deferred", "target_count": 3}}))
    write(campaign / "delivery" / CANDIDATE / "receipt.json", "{}")
    vc6_root = campaign / "control" / "vc6-arm64-x"
    retire_root = campaign / "control" / "vc6-retire-deploy-y"
    write(vc6_root / "activation-receipt.json", json.dumps({"final_state": {"active_version": T, "profile_digest": D_T, "image_id": "sha256:aaa"},
                                                           "target": {"source_tree_sha256": "5" * 64},
                                                           "rollback": {"image_reference": "img@sha256:" + "7" * 64, "version": B}}))
    write(retire_root / "stages/retire_switch/stage.json", json.dumps({"architecture": "arm64", "image_id": "sha256:bbb", "status": "pass",
                                                                      "checks": {"active_version": T, "profile_digest": D_T, "source_tree_sha256": "6" * 64}}))
    run = root / "run" / "vc6"
    # 轮次目录里写的是“ARM64 上的绝对路径”，生成器按 control/ 之后的相对部分重新定位
    write(run / "vc6-root.txt", f"/root/docker/capture-cli/data/evidence/campaigns/{CAMPAIGN}/control/vc6-arm64-x\n")
    write(run / "vc6-retire-root.txt", f"/root/docker/capture-cli/data/evidence/campaigns/{CAMPAIGN}/control/vc6-retire-deploy-y\n")
    write(run / "vc6-pc.txt", "1" * 40 + "\n")
    write(run / "vc6-retire-pc.txt", "2" * 40 + "\n")
    return root / "data", run


class GenTerminalFacts(unittest.TestCase):
    mod = load("gen_terminal_facts")

    def run_gen(self, repo: Path, data: Path, run: Path, out: Path, **override) -> dict:
        argv = ["gen", "--repo", str(repo), "--evidence-root", str(data), "--campaign", CAMPAIGN, "--candidate", CANDIDATE, "--baseline-version", B,
                "--target-version", T, "--retire-version", R, "--run-vc6", str(run), "--output", str(out)]
        for key, value in override.items():
            argv[argv.index(f"--{key.replace('_', '-')}") + 1] = value
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.mod.main()
        return json.loads(out.read_text())

    def test_generates_facts_from_receipts_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, (data, run) = build_repo(tmp / "repo"), build_evidence(tmp / "ev")
            facts = self.run_gen(repo, data, run, tmp / "facts.json")
        self.assertEqual(facts["schema_version"], f"official-client-codex-{T}-terminal-state/v1")
        self.assertEqual(facts["target"], {"previous_profile_sha256": D_B, "previous_version": B, "profile_id": f"codex-{T}-official-r1-r1", "profile_sha256": D_T, "version": T})
        self.assertEqual(facts["completed_at_utc"], "2099-01-02T03:04:05Z")
        self.assertEqual(facts["canonical"]["final_checkpoint_sequence"], 7)
        self.assertEqual(facts["canonical"]["vc6_items"], ["production-activation", f"retire-{R}", "rollback-verification"])
        self.assertEqual(facts["production_deployment"]["authoritative_commit"], "2" * 40)
        self.assertEqual(facts["production_deployment"]["activation_build_commit"], "1" * 40)
        self.assertEqual(facts["production_deployment"]["rollback_version"], B)
        self.assertEqual(facts["audit_index"]["path"], f"{MAINT}/{PREFIX}_AUDIT_INDEX.json")
        self.assertEqual(facts["campaign_chain"][0]["role"], "official_evidence_source")
        self.assertEqual(facts["campaign_chain"][1]["candidate_id"], CANDIDATE)
        self.assertEqual(facts["retained_runtime_profiles"][0]["path"], f"{RUNTIME}/profiles/{R}/retired.json")
        self.assertEqual(sorted(facts["runtime_catalog"]), ["active_profile", "catalog", "release_graph", "snapshot_catalog"])
        self.assertEqual(facts["evidence_root"], str(data))
        self.assertEqual(set(facts) - {"identity_sha256"}, set(facts))

    def test_evidence_root_label_is_what_gets_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, (data, run) = build_repo(tmp / "repo"), build_evidence(tmp / "ev")
            argv = ["gen", "--repo", str(repo), "--evidence-root", str(data), "--campaign", CAMPAIGN, "--candidate", CANDIDATE, "--baseline-version", B,
                    "--target-version", T, "--retire-version", R, "--run-vc6", str(run), "--output", str(tmp / "o.json"), "--evidence-root-label", "/root/x"]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.mod.main()
            self.assertEqual(json.loads((tmp / "o.json").read_text())["evidence_root"], "/root/x")

    def test_refuses_overwrite_and_unpruned_catalog_and_wrong_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, (data, run) = build_repo(tmp / "repo"), build_evidence(tmp / "ev")
            out = tmp / "facts.json"
            self.run_gen(repo, data, run, out)
            with self.assertRaises(SystemExit) as raised:  # 输出已存在
                self.run_gen(repo, data, run, out)
            self.assertIn("拒绝覆盖", str(raised.exception))
            completion = data / "evidence/campaigns" / CAMPAIGN / "control/vc/receipts" / CANDIDATE / "vc6-completion.json"
            good = completion.read_text()
            completion.write_text(good.replace(f'"target_version": "{T}"', '"target_version": "9.9.1"'))
            with self.assertRaises(SystemExit) as raised:  # VC-6 完成收据的主体与参数对不上
                self.run_gen(repo, data, run, tmp / "o2.json")
            self.assertIn("主体与参数不一致", str(raised.exception))
            completion.write_text(good)
            bad_repo = build_repo(tmp / "bad", retired_still_in_catalog=True)
            with self.assertRaises(SystemExit) as raised:  # Catalog 还没裁剪掉退休版本
                self.run_gen(bad_repo, data, run, tmp / "o3.json")
            self.assertIn("尚未裁剪", str(raised.exception))

    def test_campaign_checksum_mismatch_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, (data, run) = build_repo(tmp / "repo"), build_evidence(tmp / "ev")
            write(data / "evidence/campaigns" / CAMPAIGN / "campaign.sha256", "0" * 64 + "\n")
            with self.assertRaises(SystemExit) as raised:
                self.run_gen(repo, data, run, tmp / "o.json")
            self.assertIn("campaign.sha256", str(raised.exception))

    def test_non_commit_ids_are_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, (data, run) = build_repo(tmp / "repo"), build_evidence(tmp / "ev")
            write(run / "vc6-pc.txt", "abc\n")
            with self.assertRaises(SystemExit) as raised:
                self.run_gen(repo, data, run, tmp / "o.json")
            self.assertIn("40 位提交号", str(raised.exception))


# ---------------------------------------------------------------------------
# render_guide / finalize_part4 / update_active_receipt：合成指南
# ---------------------------------------------------------------------------
ACTIVE_ROW_TAIL = "16 个静态端点（含 `wham_settings_user`、`wham_accounts_check`，与 {prev} 相同）+ 1 个 ReturnedURL 动态端点；可选节相对 {prev} 无新增字段"
SECTION_49 = f"""<a id="codex-active-receipt"></a>
## 4.9 当前 Active 终态收据坐标（升级收尾只改这里）

说明文字一。

| 项 | 当前值（{B}） |
|---|---|
| Active／Previous | `codex-cli {B}`／`codex-cli {R}` |
| 终态收据 | [`CODEX_CLI_9700_TO_9800_TERMINAL_STATE_RECEIPT.json`](egress/maintenance/CODEX_CLI_9700_TO_9800_TERMINAL_STATE_RECEIPT.json) |
| 审计索引 | [`CODEX_CLI_9700_TO_9800_AUDIT_INDEX.json`](egress/maintenance/CODEX_CLI_9700_TO_9800_AUDIT_INDEX.json)（私有归档坐标取自它） |
| 官方证据来源 Campaign | `c99800-formal-vc1-r1-20980101t000000z`（attempt `OLDATT`） |
| 末级（canonical）Campaign | `c99800-formal-vc1-r1-20980101t000000z` |

说明文字二。
"""


def make_guide(*, with_49: bool = True, rule_text: str = "规则一") -> str:
    """上一轮收尾之后的合成指南：Active＝B（基线），Previous＝R（退休）。"""

    prev = B
    active_row = f"| active | {B}；`{'3' * 64}` | {ACTIVE_ROW_TAIL.format(prev=R)} | 生产默认 |"
    parts = [
        f"# Codex 手册\n\n> **当前版本**：Active 为 `codex-cli {B}`，Previous 为 `codex-cli {R}`；`9.6.0` 与 `0.149.1` 已退出 Runtime Catalog。\n",
        f"# 第二部分 Codex CLI 客户端规则画像\n{rule_text}\n",
        "# 第三部分 实施\n\n"
        f"在 3.1 里，当前 active strict wire 是 Codex CLI {prev}；其余。\n\n"
        f"## 3.2 Codex {prev} active 画像与发布执行契约\n\n表前说明。\n\n| mode | 版本 | 端点 | 用途 |\n|---|---|---|---|\n"
        f"{active_row}\n| previous | {R}；`{'4' * 64}` | 旧行 | 受控回滚和历史复算 |\n\n"
        f"当前 Active 的官方目标身份……退休 {R} 在这里略；9.6.0 按 §4.6.7 第 1 类退休，Catalog 已移除。此前退休的 9.5.0、9.4.0\n"
        "与 0.149.1 两份画像同样原地保留，分别仍由既往终态收据批准。\n\n"
        "启动期解码、结构校验或摘要核对失败即阻止启动。\n\n"
        f"| 实现 | 9.6.0／{prev} 不可变 Snapshot |\n|---|---|\n",
        "# 第四部分 流程\n\n"
        f"（9.6.0／{R}／{B} 轮用 `lite-model` 经 `/v1/chat/completions`：主线 `main-model` 从未经该接口验证，\n回滚镜像以 {R} 等旧身份出站也未必能用）\n\n"
        "失败矩阵补充（9.8 实测，复核时逐条对照）：\n- 旧条目\n\n#### 暂停种类与放行\n\n正文。\n\n"
        "9.8 轮实测（按上述口径）：旧时间线。\n",
    ]
    text = "".join(parts)
    if with_49:
        text += "\n" + SECTION_49
    return text + "\n---\n\n# 第五部分 附加门禁\n\n尾。\n"


def terminal_facts() -> dict:
    return {"target": {"version": T, "previous_version": B, "profile_sha256": D_T, "previous_profile_sha256": D_B},
            "retained_runtime_profiles": [{"path": f"{RUNTIME}/profiles/{R}/retired.json", "sha256": "8" * 64, "state": "x"}],
            "campaign_chain": [{"campaign_id": CAMPAIGN, "order": 1, "role": "official_evidence_source"},
                               {"campaign_id": CAMPAIGN, "candidate_id": CANDIDATE, "attempt_id": "ATT", "order": 2, "role": "production_replacement"}],
            "production_deployment": {"rollback_image_reference": "img@sha256:" + "7" * 64, "rollback_version": B,
                                      "activation_image_id": "sha256:aaa", "final_image_id": "sha256:bbb"},
            "canonical": {"final_checkpoint_sequence": 7, "execute_item_ids_remaining": []},
            "audit_index": {"path": f"{MAINT}/{PREFIX}_AUDIT_INDEX.json", "sha256": "0" * 64, "bytes": 1}}


def build_render_repo(root: Path, *, rules: int = 3, patches: list | None = None) -> Path:
    ver = T.replace(".", "_")
    write(root / f"tools/official_client_capture/profile_rule_patches_{ver}.json", json.dumps({"rule_patches": patches or []}))
    write(root / f"tools/official_client_capture/codex_upgrade_rules_{ver}.json", json.dumps({"required_rules": [f"SPEC-X-{i}" for i in range(rules)]}))
    write(root / RUNTIME / "release-catalog.json", json.dumps({"snapshot_catalog": {"path": "catalogdata/runtime/snaps.json"}}))
    write(root / RUNTIME / "snaps.json", json.dumps({"snapshots": [{"version": T, "file": "active.json"}]}))
    endpoints = [{"name": f"e{i}"} for i in range(16)] + [{"name": "dyn", "HostFromResponse": True}]
    write(root / RUNTIME / "active.json", json.dumps({"Endpoints": endpoints}))
    return root


def render_args(tmp: Path, repo: Path, guide: Path, **extra) -> list[str]:
    facts, campaign = tmp / "facts.json", tmp / "campaign.json"
    facts.write_text(json.dumps(terminal_facts()))
    campaign.write_text(json.dumps({"official_identity": {"git_commit": "c" * 40, "package": {"target": "aarch64", "asset_sha256": "a" * 64}, "binary_sha256": "b" * 64}}))
    argv = ["--repo", str(repo), "--guide", str(guide), "--facts", str(facts), "--campaign-json", str(campaign), "--official-attempt", ATTEMPT,
            "--official-jobs", "52", "--behavior-summary", "行为概述一句。", "--output", str(tmp / "final.md")]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return argv


class RenderGuide(unittest.TestCase):
    def render(self, extra_args=(), **kwargs):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo", **kwargs)
        guide = write(tmp / "guide.md", make_guide())
        argv = render_args(tmp, repo, guide)
        argv += list(extra_args)
        result = run_script("render_guide.py", *argv)
        return result, tmp

    def test_rewrites_the_five_active_bound_places(self) -> None:
        result, tmp = self.render(["--rollback-label", "v0.1.0-1"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = (tmp / "final.md").read_text()
        self.assertIn(f"Active 为 `codex-cli {T}`，Previous 为 `codex-cli {B}`", text)                      # 1 横幅
        self.assertIn(f"`{R}`、`9.6.0` 与 `0.149.1` 已退出", text)                                        # 退休版本前插
        self.assertIn(f"当前 active strict wire 是 Codex CLI {T}；", text)                                 # 2
        self.assertIn(f"## 3.2 Codex {T} active 画像与发布执行契约", text)                                 # 3
        self.assertIn(f"| active | {T}；`{D_T}` | 16 个静态端点", text)
        self.assertIn(f"| previous | {B}；`{D_B}` |", text)
        self.assertIn(f"{B}／{T} 不可变 Snapshot", text)                                                   # 4
        self.assertIn(f"（9.6.0／{R}／{B}／{T} 轮用 `lite-model` 经", text)                                # 5
        self.assertIn(f"回滚镜像以 {B} 等旧身份", text)
        self.assertIn("固定回滚镜像为 v0.1.0-1", text)
        self.assertIn(f"attempt `{ATTEMPT}`", text)
        self.assertIn("52 个官方 Job 全部 complete", text)
        self.assertIn(f"{R} 按 §4.6.7 第 1 类退休", text)
        self.assertIn("此前退休的 9.6.0、9.5.0、9.4.0\n与 0.149.1", text)                                   # 退休清单自动前插
        self.assertIn("行为概述一句。", text)
        self.assertIn("第二部分摘要未变", result.stdout)
        self.assertIn("变化规则 0 条、继承 3 条", result.stdout)

    def test_second_part_digest_is_unchanged(self) -> None:
        result, tmp = self.render(["--rollback-label", "v0.1.0-1"])
        old, new = make_guide(), (tmp / "final.md").read_text()
        cut = lambda t: t[t.index("# 第二部分"):t.index("# 第三部分")]
        self.assertEqual(cut(old), cut(new))

    def test_rollback_label_derived_checked_and_required(self) -> None:
        result, _ = self.render()  # 镜像引用里没有“:X.Y.Z-N@sha256”形式的标签 -> 必须手给
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("没给 --rollback-label", result.stderr)

    def test_rollback_label_mismatch_with_image_reference(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        guide = write(tmp / "guide.md", make_guide())
        argv = render_args(tmp, repo, guide, rollback_label="v9.9.9-9")
        facts = terminal_facts()
        facts["production_deployment"]["rollback_image_reference"] = "ghcr.io/x/y:0.2.0-3@sha256:" + "7" * 64
        (tmp / "facts.json").write_text(json.dumps(facts))
        result = run_script("render_guide.py", *argv)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不一致", result.stderr)
        argv[argv.index("--rollback-label") + 1] = "v0.2.0-3"
        (tmp / "final.md").unlink(missing_ok=True)
        self.assertEqual(run_script("render_guide.py", *argv).returncode, 0)

    def test_behavior_summary_is_required_and_accepts_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        guide = write(tmp / "guide.md", make_guide())
        argv = render_args(tmp, repo, guide, rollback_label="v0.1.0-1")
        index = argv.index("--behavior-summary")
        without = argv[:index] + argv[index + 2:]
        result = run_script("render_guide.py", *without)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--behavior-summary", result.stderr)
        note = write(tmp / "note.txt", "来自文件的概述。\n")
        argv[index + 1] = f"@{note}"
        self.assertEqual(run_script("render_guide.py", *argv).returncode, 0)
        self.assertIn("来自文件的概述。", (tmp / "final.md").read_text())

    def test_part2_mode_changes_banner_wording_only(self) -> None:
        outputs = {}
        for mode in ("untouched", "lead-only", "rewritten"):
            result, tmp = self.render(["--rollback-label", "v0.1.0-1", "--part2-mode", mode])
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[mode] = (tmp / "final.md").read_text()
        self.assertIn("规则正文未做本轮收尾改写", outputs["untouched"])
        self.assertIn("只在开头段补写了当前 Active", outputs["lead-only"])
        self.assertIn(f"第二部分描述当前 Active {T} 的规则", outputs["rewritten"])

    def test_default_part2_mode_is_untouched(self) -> None:
        result, tmp = self.render(["--rollback-label", "v0.1.0-1"])
        self.assertIn("规则正文未做本轮收尾改写", (tmp / "final.md").read_text())

    def test_migration_list_must_agree_with_patch_list(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        guide = write(tmp / "guide.md", make_guide())
        migration = write(tmp / "mig.json", json.dumps({"entries": [{"target_rule": "SPEC-X-0", "classification": "inherit"},
                                                                     {"target_rule": "SPEC-X-1", "classification": "inherit"},
                                                                     {"target_rule": "SPEC-X-2", "classification": "change"}]}))
        result = run_script("render_guide.py", *render_args(tmp, repo, guide, rollback_label="v0.1.0-1", rule_migration=str(migration)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不一致", result.stderr)

    def test_endpoint_count_must_match_previous_row(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        write(repo / RUNTIME / "active.json", json.dumps({"Endpoints": [{"name": "a"}, {"name": "dyn", "HostFromResponse": True}]}))
        guide = write(tmp / "guide.md", make_guide())
        result = run_script("render_guide.py", *render_args(tmp, repo, guide, rollback_label="v0.1.0-1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("端点数", result.stderr)

    def test_refuses_to_overwrite_output(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        guide = write(tmp / "guide.md", make_guide())
        argv = render_args(tmp, repo, guide, rollback_label="v0.1.0-1")
        write(tmp / "final.md", "已有")
        result = run_script("render_guide.py", *argv)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝覆盖", result.stderr)

    def test_anchor_missing_fails_without_writing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = build_render_repo(tmp / "repo")
        guide = write(tmp / "guide.md", make_guide().replace("当前 active strict wire 是", "当前 strict wire 是"))
        result = run_script("render_guide.py", *render_args(tmp, repo, guide, rollback_label="v0.1.0-1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("命中 0 次", result.stderr)
        self.assertFalse((tmp / "final.md").exists())


class FinalizePart4(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_lessons_and_timeline_are_merged(self) -> None:
        guide = write(self.tmp / "g.md", make_guide())
        lessons = write(self.tmp / "lessons.md", "- **新教训**：一句。\n")
        line = "9.9 轮实测（按上述口径）：新的一句。"
        result = run_script("finalize_part4.py", "--guide", str(guide), "--target", T, "--lessons", str(lessons), "--timeline-line", line)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = guide.read_text()
        self.assertIn("失败矩阵补充（9.8／9.9.0 实测，复核时逐条对照）：", text)
        self.assertLess(text.index("- **新教训**：一句。"), text.index("#### 暂停种类与放行"))
        self.assertIn(f"9.8 轮实测（按上述口径）：旧时间线。\n\n{line}", text)

    def test_placeholder_line_is_replaced_in_place(self) -> None:
        text = make_guide().replace("9.8 轮实测（按上述口径）：旧时间线。\n", "9.8 轮实测（按上述口径）：旧时间线。\n\n9.9 轮实测（按上述口径，收尾时回填）：占位。\n")
        guide = write(self.tmp / "g.md", text)
        result = run_script("finalize_part4.py", "--guide", str(guide), "--target", T, "--timeline-line", "9.9 轮实测（按上述口径）：填好了。")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = guide.read_text()
        self.assertIn("填好了。", out)
        self.assertNotIn("占位。", out)

    def test_existing_timeline_sentence_is_not_duplicated(self) -> None:
        text = make_guide().replace("9.8 轮实测（按上述口径）：旧时间线。\n", "9.9 轮实测（按上述口径）：已经有了。\n")
        guide = write(self.tmp / "g.md", text)
        before = guide.read_text()
        result = run_script("finalize_part4.py", "--guide", str(guide), "--target", T, "--timeline-line", "9.9 轮实测（按上述口径）：又一句。")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(guide.read_text(), before)

    def test_missing_anchor_fails_and_keeps_file(self) -> None:
        guide = write(self.tmp / "g.md", make_guide().replace("#### 暂停种类与放行", "#### 别的标题"))
        before = guide.read_text()
        lessons = write(self.tmp / "l.md", "- x\n")
        result = run_script("finalize_part4.py", "--guide", str(guide), "--target", T, "--lessons", str(lessons))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(guide.read_text(), before)

    def test_target_is_required(self) -> None:
        guide = write(self.tmp / "g.md", make_guide())
        result = run_script("finalize_part4.py", "--guide", str(guide))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--target", result.stderr)

    def test_partial_manifest_sentence(self) -> None:
        text = make_guide().replace("正文。\n", "该流程在真实执行器上未做实战（旧原因）；其余。\n", 1)
        guide = write(self.tmp / "g.md", text)
        result = run_script("finalize_part4.py", "--guide", str(guide), "--target", T, "--partial-manifest-sentence", "该流程已在真实执行器上演练通过。")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("该流程已在真实执行器上演练通过；其余。", guide.read_text())


class UpdateActiveReceipt(unittest.TestCase):
    mod = load("update_active_receipt")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.facts = write(self.tmp / "facts.json", json.dumps(terminal_facts()))

    def coords(self) -> dict:
        return self.mod.coordinates_from_facts(terminal_facts(), ATTEMPT)

    def test_coordinates_from_facts(self) -> None:
        c = self.coords()
        self.assertEqual((c["version"], c["previous"]), (T, B))
        self.assertEqual(c["audit"], f"{PREFIX}_AUDIT_INDEX.json")
        self.assertEqual(c["receipt"], f"{PREFIX}_TERMINAL_STATE_RECEIPT.json")
        self.assertEqual((c["official"], c["canonical"], c["attempt"]), (CAMPAIGN, CAMPAIGN, ATTEMPT))
        self.assertEqual(self.mod.coordinates_from_facts(terminal_facts(), ATTEMPT, "X.json")["receipt"], "X.json")

    def test_updates_only_the_table(self) -> None:
        old = make_guide()
        new, changed = self.mod.update_text(old, self.coords())
        self.assertTrue(changed)
        self.assertIn(f"| 项 | 当前值（{T}） |", new)
        self.assertIn(f"| Active／Previous | `codex-cli {T}`／`codex-cli {B}` |", new)
        self.assertIn(f"| 终态收据 | [`{PREFIX}_TERMINAL_STATE_RECEIPT.json`](egress/maintenance/{PREFIX}_TERMINAL_STATE_RECEIPT.json) |", new)
        self.assertIn(f"（私有归档坐标取自它） |", new)
        self.assertIn(f"| 官方证据来源 Campaign | `{CAMPAIGN}`（attempt `{ATTEMPT}`） |", new)
        self.assertIn(f"| 末级（canonical）Campaign | `{CAMPAIGN}` |", new)
        # 表以外一个字不动：把两份文本里的表行删掉后相同
        strip = lambda t: "\n".join(line for line in t.split("\n") if not line.startswith("|") or "mode" in line or "active |" in line or "previous |" in line or "实现" in line or "---" in line)
        self.assertEqual(strip(old).replace("OLDATT", ""), strip(new).replace(ATTEMPT, ""))
        self.assertIn("说明文字一。", new)
        self.assertIn("说明文字二。", new)
        self.assertEqual(new[:old.index("| 项 |")], old[:old.index("| 项 |")])

    def test_idempotent(self) -> None:
        once, _ = self.mod.update_text(make_guide(), self.coords())
        twice, changed = self.mod.update_text(once, self.coords())
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_errors(self) -> None:
        with self.assertRaises(self.mod.SectionError) as raised:
            self.mod.update_text(make_guide(with_49=False), self.coords())
        self.assertIn("恰好出现 1 次", str(raised.exception))
        doubled = make_guide() + "\n" + SECTION_49
        with self.assertRaises(self.mod.SectionError):
            self.mod.update_text(doubled, self.coords())
        renamed = make_guide().replace("| 审计索引 |", "| 审计索引文件 |")
        with self.assertRaises(self.mod.SectionError) as raised:
            self.mod.update_text(renamed, self.coords())
        self.assertIn("行标签与约定不符", str(raised.exception))
        no_sep = make_guide().replace("|---|---|\n| Active", "| Active")
        with self.assertRaises(self.mod.SectionError):
            self.mod.update_text(no_sep, self.coords())

    def test_facts_errors(self) -> None:
        facts = terminal_facts()
        facts["campaign_chain"].append({"campaign_id": "other", "role": "official_evidence_source"})
        with self.assertRaises(self.mod.SectionError) as raised:
            self.mod.coordinates_from_facts(facts, ATTEMPT)
        self.assertIn("应恰好 1 个", str(raised.exception))
        with self.assertRaises(self.mod.SectionError):
            self.mod.coordinates_from_facts({"target": {}}, ATTEMPT)
        with self.assertRaises(self.mod.SectionError) as raised:
            self.mod.coordinates_from_facts(terminal_facts(), "")
        self.assertIn("官方 attempt", str(raised.exception))

    def test_cli_in_place_and_output_modes(self) -> None:
        guide = write(self.tmp / "g.md", make_guide())
        out = self.tmp / "out.md"
        result = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT, "--output", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已更新", result.stdout)
        self.assertIn(f"当前值（{T}）", out.read_text())
        self.assertIn(f"当前值（{B}）", guide.read_text())  # 源文件没动
        again = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT, "--output", str(out))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("拒绝覆盖", again.stderr)
        in_place = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT, "--in-place")
        self.assertEqual(in_place.returncode, 0, in_place.stderr)
        self.assertEqual(guide.read_text(), out.read_text())
        both = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT)
        self.assertEqual(both.returncode, 2)

    def test_cli_reports_section_problem(self) -> None:
        guide = write(self.tmp / "g.md", make_guide(with_49=False))
        result = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT, "--in-place")
        self.assertEqual(result.returncode, 1)
        self.assertIn("4.9", result.stderr)

    def test_verify_repo_calls_evidence_index_function(self) -> None:
        repo = self.tmp / "repo"
        stub = ("MISSING = []\n"
                "def active_receipt_section(text):\n    return text\n"
                "def active_campaign_evidence(text):\n"
                f"    for item in ({T!r}, {PREFIX + '_TERMINAL_STATE_RECEIPT.json'!r}, {CAMPAIGN!r}):\n"
                "        if item not in text:\n            raise ValueError('桩校验：缺 ' + item)\n")
        write(repo / "tools/evidence_index.py", stub)
        guide = write(self.tmp / "g.md", make_guide())
        result = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT,
                            "--output", str(self.tmp / "o.md"), "--verify-repo", str(repo))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("校验通过", result.stdout)
        # 校验失败（桩要求的收据文件名对不上）-> 退出码 1
        write(repo / "tools/evidence_index.py", stub.replace(PREFIX + "_TERMINAL_STATE_RECEIPT.json", "NOT_THERE.json"))
        result = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT,
                            "--output", str(self.tmp / "o2.md"), "--verify-repo", str(repo))
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT_THERE.json", result.stderr)
        # 旧版 evidence_index（没有 active_receipt_section）-> 退出码 3，明确说跳过
        write(repo / "tools/evidence_index.py", "def active_campaign_evidence(text):\n    return None\n")
        result = run_script("update_active_receipt.py", "--guide", str(guide), "--facts", str(self.facts), "--official-attempt", ATTEMPT,
                            "--output", str(self.tmp / "o3.md"), "--verify-repo", str(repo))
        self.assertEqual(result.returncode, self.mod.EXIT_VERIFY_SKIPPED)
        self.assertIn("跳过", result.stderr)


class Part2MinimalOptional(unittest.TestCase):
    mod = load("part2_minimal")
    ARGS = ["--target-version", T, "--baseline-version", B, "--retire-version", R, "--last-retired-version", "9.6.0", "--campaign", CAMPAIGN,
            "--attempt", ATTEMPT, "--jobs", "52", "--prev-campaign", "c99800-formal-vc1-r1-20980101t000000z", "--prev-jobs", "50",
            "--lite-main-model", "main-model", "--lite-track-model", "lite-model", "--rules-total", "43"]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def old_text(self) -> str:
        values = self.mod.build_values(self.mod.argparse.Namespace(
            target=T, baseline=B, retire=R, last_retired="9.6.0", campaign=CAMPAIGN, attempt=ATTEMPT, jobs="52", prev_campaign="c99800-formal-vc1-r1-20980101t000000z",
            prev_jobs="50", lite_main_model="main-model", lite_track_model="lite-model", rules_total="43"))
        return "前文\n" + "\n".join(old.format(**values) for old, _new in self.mod.DEFAULT_EDITS) + "\n后文\n"

    def test_default_edits_apply_once_each(self) -> None:
        guide = write(self.tmp / "g.md", self.old_text())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.main([*self.ARGS, "--guide", str(guide)]), 0)
        text = guide.read_text()
        self.assertIn(f"当前生产 Active 为 {T}（previous 为\n{B}）", text)
        self.assertIn(f"Campaign `{CAMPAIGN}` 的 attempt `{ATTEMPT}`（52 个官方 Job 全部 complete）", text)
        self.assertIn(f"[`9.9 终态收据`](egress/maintenance/{PREFIX}_TERMINAL_STATE_RECEIPT.json)", text)
        self.assertIn(f"（{B} 轮官方证据全部重新采集，50 个官方 Job 全部 complete）", text)
        self.assertIn("43 条目标规则全部继承", text)
        # 第二次再跑：旧文已不存在，命中 0 次 -> 幂等保护
        with self.assertRaises(SystemExit) as raised:
            self.mod.main([*self.ARGS, "--guide", str(guide)])
        self.assertIn("命中 0 次", str(raised.exception))

    def test_missing_parameters_are_listed_together(self) -> None:
        guide = write(self.tmp / "g.md", self.old_text())
        argv = ["--target-version", T, "--baseline-version", B, "--retire-version", R, "--campaign", CAMPAIGN, "--attempt", ATTEMPT, "--jobs", "52", "--guide", str(guide)]
        with self.assertRaises(SystemExit) as raised:
            self.mod.main(argv)
        message = str(raised.exception)
        for flag in ("--last-retired-version", "--prev-campaign", "--prev-jobs", "--lite-main-model", "--lite-track-model", "--rules-total"):
            self.assertIn(flag, message)

    def test_edits_file_replaces_builtin_table_and_only_needs_its_own_parameters(self) -> None:
        guide = write(self.tmp / "g.md", "头 {不是占位符？} 旧文 {x}\n")
        edits = write(self.tmp / "e.json", json.dumps([{"old": "旧文", "new": "新文 {target} / {campaign}"}]))
        argv = ["--target-version", T, "--baseline-version", B, "--retire-version", R, "--campaign", CAMPAIGN, "--attempt", ATTEMPT, "--jobs", "1",
                "--edits-file", str(edits), "--guide", str(guide)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.main(argv), 0)
        self.assertEqual(guide.read_text(), f"头 {{不是占位符？}} 新文 {T} / {CAMPAIGN} {{x}}\n")

    def test_bad_edits_file(self) -> None:
        edits = write(self.tmp / "e.json", json.dumps({"old": "x"}))
        with self.assertRaises(SystemExit):
            self.mod.load_edits(str(edits))

    def test_header_marks_the_step_as_obsolete(self) -> None:
        self.assertIn("已作废", (KIT / "part2_minimal.py").read_text(encoding="utf-8").split('"""')[1])


class ApplyPostpromoFixes(unittest.TestCase):
    mod = load("apply_postpromo_fixes")

    def make_root(self, tmp: Path, prev=B, retired=R) -> Path:
        body = ("class T:\n    def test_real_codex_runtime_catalog_resolves_active(self):\n"
                f"        # {prev} 收尾：VC-6 晋升后 Runtime Catalog 的 Active 为 {prev}（Previous {retired}）。\n"
                f"        self.assertEqual(active[\"version\"], \"{prev}\")\n")
        write(tmp / self.mod.TEST_FILE, body)
        return tmp

    def test_applies_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(Path(tmp))
            changed, message = self.mod.apply(root, T, B, R)
            self.assertTrue(changed, message)
            text = (root / self.mod.TEST_FILE).read_text()
            self.assertIn(f"# {T} 收尾：VC-6 晋升后 Runtime Catalog 的 Active 为 {T}（Previous {B}）。", text)
            self.assertIn(f'self.assertEqual(active["version"], "{T}")', text)
            changed, message = self.mod.apply(root, T, B, R)
            self.assertFalse(changed)
            self.assertIn("不改", message)

    def test_anchor_must_hit_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(Path(tmp), prev="9.1.0")
            with self.assertRaises(SystemExit) as raised:
                self.mod.apply(root, T, B, R)
            self.assertIn("命中 0 次", str(raised.exception))

    def test_cli_requires_versions(self) -> None:
        result = run_script("apply_postpromo_fixes.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--target", result.stderr)


class RebindGuideChapter2(unittest.TestCase):
    """合成 git 仓库里的第二章摘要连锁（可选组，已作废，但逻辑保留，这里钉住它的载荷审核）。"""

    GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
    CODEX_CLOSEOUT_STUB = (
        "import hashlib\nPART2 = '# 第二部分'\n\n\ndef part2_sha(text):\n"
        "    lines = text.splitlines(keepends=True)\n    start = next(i for i, l in enumerate(lines) if l.rstrip('\\r\\n') == PART2)\n"
        "    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('# ')), len(lines))\n"
        "    return hashlib.sha256(''.join(lines[start:end]).encode()).hexdigest()\n")

    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env={**GIT_ENV, "PATH": "/usr/bin:/bin"}).stdout.strip()

    def test_rebinds_manifests_and_checker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide_v1 = "# 第一部分\n甲\n# 第二部分\n规则一\n# 第三部分\n乙\n"
            guide_v2 = "# 第一部分\n甲\n# 第二部分\n规则一（改）\n# 第三部分\n乙\n"
            cc = load("closeout_params")  # 只为拿到一个可 import 的模块对象占位，下面用桩的 part2_sha 算摘要
            del cc
            stub = write(root / "tools/codex_closeout.py", self.CODEX_CLOSEOUT_STUB)
            sys.path.insert(0, str(root))
            try:
                import importlib
                importlib.invalidate_caches()
                stub_mod = importlib.util.spec_from_file_location("stub_cc", stub)
                module = importlib.util.module_from_spec(stub_mod)
                stub_mod.loader.exec_module(module)
            finally:
                sys.path.remove(str(root))
            old_sha, new_sha = module.part2_sha(guide_v1), module.part2_sha(guide_v2)
            manifest_a = json.dumps({"source_spec": {"path": self.GUIDE, "fragment": "第二部分", "sha256": old_sha}, "rules": [1]}, indent=2) + "\n"
            manifest_b = json.dumps({"source_spec": f"{self.GUIDE}#第二部分", "source_spec_sha256": old_sha, "rules": [2]}, indent=2) + "\n"
            write(root / "tools/official_client_capture/a.json", manifest_a)
            write(root / "tools/official_client_capture/b.json", manifest_b)
            checker = ('DEFAULT_PROFILE_RELATIVE_PATH = "tools/official_client_capture/a.json"\n'
                       f'FROZEN_PROFILE_SHA256 = (\n    "{sha(manifest_a)}"\n)\n')
            write(root / "tools/official_client_capture/chk.py", checker)
            write(root / self.GUIDE, guide_v1)
            self.git(root, "init", "-q")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "base")
            base = self.git(root, "rev-parse", "HEAD")
            write(root / self.GUIDE, guide_v2)
            report = subprocess.run([sys.executable, "-B", str(KIT / "rebind_guide_ch2.py"), "--root", str(root), "--base", base],
                                    capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(report.returncode, 1, report.stdout + report.stderr)
            self.assertIn("待改清单 2 份、检查器 1 个", report.stdout)
            missing = subprocess.run([sys.executable, "-B", str(KIT / "rebind_guide_ch2.py"), "--root", str(root), "--base", base, "--apply"],
                                     capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(missing.returncode, 2)
            self.assertIn("--target-version", missing.stderr)
            applied = subprocess.run([sys.executable, "-B", str(KIT / "rebind_guide_ch2.py"), "--root", str(root), "--base", base, "--apply",
                                      "--target-version", T, "--note-date", "2099-01-01", "--note-tool-name", "step.py"],
                                     capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            new_a = (root / "tools/official_client_capture/a.json").read_text()
            self.assertEqual(new_a, manifest_a.replace(old_sha, new_sha))
            self.assertEqual((root / "tools/official_client_capture/b.json").read_text(), manifest_b.replace(old_sha, new_sha))
            new_checker = (root / "tools/official_client_capture/chk.py").read_text()
            self.assertIn(sha(new_a), new_checker)
            self.assertIn(f"# 2099-01-01（{T} 收尾）", new_checker)
            self.assertIn("step.py", new_checker)
            self.assertNotIn(sha(manifest_a), new_checker.split("FROZEN_PROFILE_SHA256 = (")[1])
            again = subprocess.run([sys.executable, "-B", str(KIT / "rebind_guide_ch2.py"), "--root", str(root), "--base", base],
                                   capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)  # 幂等：已是最新


if __name__ == "__main__":
    unittest.main()
