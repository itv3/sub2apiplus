"""收尾工具包里“跑流程与取证据”的脚本（``tools/ci/closeout/`` 的 shell 入口与 ARM64 侧脚本）的单元测试，全部离线、用合成数据。

做法：测试在系统临时目录里造一个“假 ARM64 根”，再放一个假 ``ssh`` 到 PATH 里——它忽略选项与主机名，把命令里的 ``/root/`` 前缀改写到假根下，在本机用 bash 执行。
这样 ``collect_stage_receipts.sh``、``fetch_evidence_subset.sh``、``audit_index.sh`` 与 ``closeout.sh`` 的只读步骤都能真跑，而不连任何远端。

覆盖：
* ``closeout.sh``：帮助与步骤表、缺参数报“缺哪个”并退出 2、未知步骤、可选组（--with-part2）缺省关闭而跳过、ARM64 写步骤不设 CLOSEOUT_ARM64_WRITE=1 时一次 ssh 都不发、
  b4 经假 ssh 取回四份阶段收据、b3-check 的四类检查失败都不被吞掉（用桩脚本和小测试模块）、源码里没有 push／nohup；
* ``collect_stage_receipts.sh``：逐字节取回并核摘要，目标已存在拒绝覆盖（退出码 3），摘要不一致时删除已写文件（退出码 3）；
* ``fetch_evidence_subset.sh``：只取需要的文件且只取最后一个检查点；
* ``audit_index.sh``：参数校验、找不到私有归档退出码 3、调用 generate 再 check，参数原样传给审计索引工具；
* ``make_closeout_config.py``：生成的配置字段集合与 ``codex_closeout.validate_config`` 的合同一致（用真实校验函数核）；拒绝覆盖、承接参数成组；
* ``write_approvals.py``：门禁未过不签、三份文件 0600、绑定 plan 的 review_sha256、不覆盖；
* ``gate_status.py``：状态快照各行；``deployment_check.py``：假 docker＋本机 HTTP 服务走通成功路径与镜像不符、凭证不全、拒绝覆盖等失败路径；
* ``post_promo_mods.sh``、``prerun.sh``：用桩命令验证汇总文件与 ALLDONE、环境变量缺失时明确报错；``detach.py``：日志里有输出与 EXIT=。

不依赖 ARM64、网络与 ``local-analysis``；临时目录都建在系统临时目录。
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
KIT = REPO_ROOT / "tools" / "ci" / "closeout"
SYSTEM_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
GIT_ENV = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto",
           "GIT_CONFIG_VALUE_1": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

T, B, R = "9.9.0", "9.8.0", "9.7.0"
CAMPAIGN = "c90090-formal-vc1-r1-20990101t000000z"   # 9.9.0 -> Campaign 标记 c90090
CANDIDATE = "c90090-candidate-r1"
PREFIX = "CODEX_CLI_9008_TO_9009"
RUNROUND = "/root/vc-rounds/c90090-r1"
DATA = "/root/docker/capture-cli/data"


def write(path: Path, text: str | bytes, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    if mode is not None:
        path.chmod(mode)
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    """每个用例一个：临时目录 + bin 目录（假 ssh、必要时假 sha256sum、python3 软链）+ 假 ARM64 根。"""

    def __init__(self, test: unittest.TestCase) -> None:
        self.root = Path(os.path.realpath(tempfile.mkdtemp()))   # macOS 的 /var 是符号链接，收尾工具的路径校验不接受链接
        test.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake = self.root / "fake-arm64"
        (self.fake / "root").mkdir(parents=True)
        os.symlink(sys.executable, self.bin / "python3")
        write(self.bin / "ssh",
              "#!/bin/bash\n"
              'echo "$@" >> "${FAKE_SSH_LOG:-/dev/null}"\n'
              'while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -n|-T|-t) shift ;; *) break ;; esac; done\n'
              "shift\n"
              'cmd="$*"\ncmd="${cmd//\\/root\\//${FAKE_ROOT}/root/}"\n'
              'if [ -n "${FAKE_BAD_SHA:-}" ] && [[ "${cmd}" == *sha256sum* ]]; then echo 0000000000000000000000000000000000000000000000000000000000000000; exit 0; fi\n'
              'exec bash -c "${cmd}"\n', 0o755)
        if shutil.which("sha256sum", path=SYSTEM_PATH) is None:  # macOS：ARM64 上的 sha256sum 用 shasum 顶替
            write(self.bin / "sha256sum", '#!/bin/bash\nshasum -a 256 "$@"\n', 0o755)

    def remote(self, path: str) -> Path:
        """ARM64 上的绝对路径 -> 假根下的本机路径。"""

        return self.fake / path.lstrip("/")

    def env(self, **extra: str) -> dict[str, str]:
        env = {"PATH": f"{self.bin}:{SYSTEM_PATH}", "PYTHONDONTWRITEBYTECODE": "1", "FAKE_ROOT": str(self.fake), "HOME": str(self.root),
               "FAKE_SSH_LOG": str(self.root / "ssh.log"), **GIT_ENV}
        env.update(extra)
        return env

    def run(self, argv: list[str], **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, env=self.env(**extra), check=False)

    def ssh_calls(self) -> list[str]:
        log = self.root / "ssh.log"
        return log.read_text().splitlines() if log.exists() else []


def build_stage_receipts(ws: Workspace) -> dict[str, bytes]:
    """假 ARM64 上 VC-6 留下的四份阶段收据，返回 {文件名后缀: 内容}。"""

    campaign = ws.remote(f"{DATA}/evidence/campaigns/{CAMPAIGN}")
    vc6, retire = campaign / "control/vc6-arm64-x", campaign / "control/vc6-retire-deploy-y"
    contents = {"CATALOG_PROMOTION_RECEIPT": b'{"a":1}\n', "POST_PROMOTION_GATE_RECEIPT": b'{"b":2}\n',
                "PRODUCTION_ACTIVATION_RECEIPT": b'{"c":3}\n', "RUNTIME_PROFILE_REMOVAL_RECEIPT": b'{"d":4}\n'}
    write(vc6 / "catalog-promotion-receipt.json", contents["CATALOG_PROMOTION_RECEIPT"])
    write(vc6 / "post-promotion-gates.receipt.json", contents["POST_PROMOTION_GATE_RECEIPT"])
    write(vc6 / "activation-receipt.json", contents["PRODUCTION_ACTIVATION_RECEIPT"])
    write(retire / "removal-receipt.json", contents["RUNTIME_PROFILE_REMOVAL_RECEIPT"])
    write(ws.remote(f"{RUNROUND}/vc6/vc6-root.txt"), f"{DATA}/evidence/campaigns/{CAMPAIGN}/control/vc6-arm64-x\n")
    write(ws.remote(f"{RUNROUND}/vc6/vc6-retire-root.txt"), f"{DATA}/evidence/campaigns/{CAMPAIGN}/control/vc6-retire-deploy-y\n")
    return contents


class CollectStageReceipts(unittest.TestCase):
    def test_copies_four_receipts_byte_for_byte_and_refuses_overwrite(self) -> None:
        ws = Workspace(self)
        contents = build_stage_receipts(ws)
        repo = ws.root / "repo"
        (repo / "docs/egress/maintenance").mkdir(parents=True)
        result = ws.run(["bash", str(KIT / "collect_stage_receipts.sh"), str(repo), RUNROUND, PREFIX])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STAGE_RECEIPTS_DONE", result.stdout)
        for suffix, data in contents.items():
            self.assertEqual((repo / f"docs/egress/maintenance/{PREFIX}_{suffix}.json").read_bytes(), data)
            self.assertIn(sha(data), result.stdout)
        again = ws.run(["bash", str(KIT / "collect_stage_receipts.sh"), str(repo), RUNROUND, PREFIX])
        self.assertEqual(again.returncode, 3)
        self.assertIn("拒绝覆盖", again.stdout)

    def test_digest_mismatch_removes_the_copy(self) -> None:
        ws = Workspace(self)
        build_stage_receipts(ws)
        repo = ws.root / "repo"
        (repo / "docs/egress/maintenance").mkdir(parents=True)
        result = ws.run(["bash", str(KIT / "collect_stage_receipts.sh"), str(repo), RUNROUND, PREFIX], FAKE_BAD_SHA="1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("摘要不一致", result.stdout)
        self.assertEqual(list((repo / "docs/egress/maintenance").iterdir()), [])

    def test_positional_arguments_are_required(self) -> None:
        ws = Workspace(self)
        result = ws.run(["bash", str(KIT / "collect_stage_receipts.sh"), str(ws.root), RUNROUND])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("文件名前缀", result.stderr)


class FetchEvidenceSubset(unittest.TestCase):
    def test_fetches_only_what_the_generator_needs(self) -> None:
        ws = Workspace(self)
        campaign = ws.remote(f"{DATA}/evidence/campaigns/{CAMPAIGN}")
        needed = {
            "campaign.json": b"{}", "campaign.sha256": b"x", "canonical/checkpoints/00000002.json": b"{2}",
            f"control/vc/receipts/{CANDIDATE}/vc6-completion.json": b"{c}", f"control/vc/receipts/{CANDIDATE}/cleanup-decision.json": b"{d}",
            f"delivery/{CANDIDATE}/receipt.json": b"{r}", "control/vc6-arm64-x/activation-receipt.json": b"{a}",
            "control/vc6-retire-deploy-y/stages/retire_switch/stage.json": b"{s}"}
        for rel, data in needed.items():
            write(campaign / rel, data)
        write(campaign / "canonical/checkpoints/00000001.json", b"{1}")          # 旧检查点不取
        write(campaign / "control/vc6-arm64-x/huge-unrelated.bin", b"x" * 1000)   # 无关文件不取
        for name in ("vc6-pc.txt", "vc6-retire-pc.txt", "vc6-tree.txt"):
            write(ws.remote(f"{RUNROUND}/vc6/{name}"), name + "\n")
        write(ws.remote(f"{RUNROUND}/vc6/vc6-root.txt"), f"{DATA}/evidence/campaigns/{CAMPAIGN}/control/vc6-arm64-x\n")
        write(ws.remote(f"{RUNROUND}/vc6/vc6-retire-root.txt"), f"{DATA}/evidence/campaigns/{CAMPAIGN}/control/vc6-retire-deploy-y\n")
        dest = ws.root / "subset"
        result = ws.run(["bash", str(KIT / "fetch_evidence_subset.sh"), str(dest), CAMPAIGN, CANDIDATE, RUNROUND])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("最后检查点 00000002.json", result.stdout)
        got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        self.assertEqual(len(got), 8 + 5)
        self.assertNotIn(f"data/evidence/campaigns/{CAMPAIGN}/canonical/checkpoints/00000001.json", got)
        self.assertFalse(any("huge-unrelated" in item for item in got))
        for rel, data in needed.items():
            self.assertEqual((dest / "data/evidence/campaigns" / CAMPAIGN / rel).read_bytes(), data)

    def test_arguments_required(self) -> None:
        ws = Workspace(self)
        self.assertNotEqual(ws.run(["bash", str(KIT / "fetch_evidence_subset.sh"), str(ws.root / "x")]).returncode, 0)


class AuditIndex(unittest.TestCase):
    STUB = ("import json, sys\nfrom pathlib import Path\nargs = sys.argv[1:]\n"
            "Path(sys.argv[0]).with_name('calls.log').open('a').write(json.dumps(args) + '\\n')\n"
            "if args[0] == 'generate':\n    out = args[args.index('--output') + 1]\n    Path(out).write_text('{}')\n")

    def args(self, ws: Workspace, **override) -> list[str]:
        tools = ws.root / "tools"
        write(tools / "codex_audit_index.py", self.STUB)
        archive = write(ws.root / "archive.tar.gz", "x")
        values = {"tools": str(tools), "outdir": str(ws.root / "out"), "campaign": CAMPAIGN, "target-version": T, "baseline-version": B, "prefix": PREFIX,
                  "runround": str(ws.root / "rr"), "data": str(ws.root / "data"), "archive": str(archive)}
        values.update({key.replace("_", "-"): value for key, value in override.items()})
        argv = ["bash", str(KIT / "audit_index.sh")]
        for key, value in values.items():
            argv += [f"--{key}", value]
        (ws.root / "out").mkdir(exist_ok=True)
        return argv

    def test_generate_then_check_with_given_parameters(self) -> None:
        ws = Workspace(self)
        result = ws.run(self.args(ws))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"AUDIT_INDEX_DONE {ws.root}/out/{PREFIX}_AUDIT_INDEX.json", result.stdout)
        calls = [json.loads(line) for line in (ws.root / "tools/calls.log").read_text().splitlines()]
        generate, check = calls
        self.assertEqual(generate[0], "generate")
        for flag, value in (("--target-version", T), ("--baseline-version", B), ("--campaign-id", CAMPAIGN), ("--evidence-root", str(ws.root / "data"))):
            self.assertEqual(generate[generate.index(flag) + 1], value)
        self.assertIn("--no-replay", generate)
        self.assertEqual(check[0], "check")
        self.assertEqual(check[check.index("--evidence-root") + 1], str(ws.root / "data"))

    def test_missing_arguments_and_missing_archive(self) -> None:
        ws = Workspace(self)
        argv = self.args(ws)
        cut = argv.index("--campaign")
        result = ws.run(argv[:cut] + argv[cut + 2:])
        self.assertEqual(result.returncode, 2)
        self.assertIn("CAMPAIGN", result.stderr)
        argv = self.args(ws, archive=str(ws.root / "nope.tar.gz"))
        result = ws.run(argv)
        self.assertEqual(result.returncode, 3)
        self.assertIn("找不到私有归档", result.stdout)
        self.assertEqual(ws.run(["bash", str(KIT / "audit_index.sh"), "--no-such"]).returncode, 2)


class CloseoutShell(unittest.TestCase):
    BASE = ["--version", T, "--baseline-version", B, "--retire-version", R, "--campaign", CAMPAIGN, "--tag-date", "20990101"]

    def run_closeout(self, ws: Workspace, args: list[str], *, wt: Path | None = None, main: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        wt = wt or ws.root / "wt"
        main = main or ws.root / "main"
        argv = ["bash", str(KIT / "closeout.sh"), *self.BASE, "--wt", str(wt), "--out-dir", str(ws.root / "out"), "--main", str(main), *args]
        return ws.run(argv, **env)

    def test_help_and_steps(self) -> None:
        ws = Workspace(self)
        for flag in ("--help", "-h", "help", ""):
            result = ws.run(["bash", str(KIT / "closeout.sh"), *([flag] if flag else [])])
            self.assertEqual(result.returncode, 0, flag)
            self.assertIn("步骤总表", result.stdout)
            self.assertIn("--with-part2", result.stdout)
        steps = ws.run(["bash", str(KIT / "closeout.sh"), "steps"])
        self.assertEqual(steps.returncode, 0)
        self.assertIn("b8a-active-receipt", steps.stdout)
        self.assertIn("[ARM64 写]", steps.stdout)

    def test_missing_parameter_is_named(self) -> None:
        ws = Workspace(self)
        result = ws.run(["bash", str(KIT / "closeout.sh"), "--version", T, "--main", str(ws.root), "b6-commit-stage"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("缺少参数 baseline_version", result.stderr)
        self.assertIn("--baseline-version", result.stderr)
        self.assertEqual(ws.ssh_calls(), [])

    def test_unknown_step(self) -> None:
        ws = Workspace(self)
        result = self.run_closeout(ws, ["b99-nothing"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("未知步骤", result.stderr)

    def test_part2_group_is_off_by_default(self) -> None:
        ws = Workspace(self)
        for step in ("b2-part2-lead", "b3-regen"):
            result = self.run_closeout(ws, [step])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("可选组", result.stdout)
            self.assertIn("跳过", result.stdout)
        self.assertEqual(ws.ssh_calls(), [])

    def test_part2_group_needs_its_own_parameters_when_enabled(self) -> None:
        ws = Workspace(self)
        result = self.run_closeout(ws, ["--with-part2", "b3-regen"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("i1_dir", result.stderr)

    def test_arm64_write_steps_do_not_touch_arm64_without_the_switch(self) -> None:
        ws = Workspace(self)
        wt = ws.root / "wt"
        wt.mkdir()
        write(wt / "tools/codex_audit_index.py", "")
        for step in ("b5-audit-index", "b14-gates-start", "b10b-prepare"):
            result = self.run_closeout(ws, ["--arm64-node-modules", "/x", step] if step == "b10b-prepare" else [step], wt=wt,
                                       **({"FAKE_SSH_LOG": str(ws.root / "ssh.log")}))
            if step == "b10b-prepare":
                self.assertNotEqual(result.returncode, 0)  # 先 b10-sync：没有 GitHub main 提示号
                continue
            self.assertEqual(result.returncode, 0, (step, result.stdout, result.stderr))
            self.assertIn("未设 CLOSEOUT_ARM64_WRITE=1，只打印不执行", result.stdout)
        self.assertEqual(ws.ssh_calls(), [], "不设 CLOSEOUT_ARM64_WRITE=1 时不许发出任何 ssh")

    def test_b10_pre_prints_only_and_reads_github_main_locally(self) -> None:
        ws = Workspace(self)
        origin = ws.root / "origin.git"
        main = ws.root / "main"
        env = {**GIT_ENV}
        for cmd in (["init", "-q", "--bare", str(origin)], ["init", "-q", "-b", "main", str(main)]):
            subprocess.run(["git", *cmd], check=True, capture_output=True, env={**env, "PATH": SYSTEM_PATH})
        write(main / "a.txt", "a")
        for cmd in (["add", "-A"], ["commit", "-q", "-m", "x"], ["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", "-C", str(main), *cmd], check=True, capture_output=True, env={**env, "PATH": SYSTEM_PATH})
        tip = subprocess.run(["git", "-C", str(main), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, env={**env, "PATH": SYSTEM_PATH}).stdout.strip()
        result = self.run_closeout(ws, ["b10-pre"], main=main)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(tip, result.stdout)
        self.assertEqual((ws.root / "out/facts/github-main-tip.txt").read_text().strip(), tip)
        self.assertEqual(ws.ssh_calls(), [])

    def test_b4_fetches_stage_receipts_through_ssh(self) -> None:
        ws = Workspace(self)
        contents = build_stage_receipts(ws)
        wt = ws.root / "wt"
        (wt / "docs/egress/maintenance").mkdir(parents=True)
        result = self.run_closeout(ws, ["b4-stage-receipts"], wt=wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for suffix, data in contents.items():
            self.assertEqual((wt / f"docs/egress/maintenance/{PREFIX}_{suffix}.json").read_bytes(), data)
        self.assertTrue(ws.ssh_calls())

    def test_b9_requires_behavior_summary_and_the_4_9_copy(self) -> None:
        ws = Workspace(self)
        wt = ws.root / "wt"
        wt.mkdir()
        facts = ws.root / "out/facts"
        write(facts / "terminal-facts-9009.json", "{}")
        result = self.run_closeout(ws, ["b9-render-guide"], wt=wt)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("b8a-active-receipt", result.stderr)

    def test_b8_refuses_without_4_9_copy_and_with_dirty_tree(self) -> None:
        ws = Workspace(self)
        wt = ws.root / "wt"
        wt.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=wt, check=True, env={**GIT_ENV, "PATH": SYSTEM_PATH})
        write(wt / ".gitignore", "")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, env={**GIT_ENV, "PATH": SYSTEM_PATH})
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=wt, check=True, capture_output=True, env={**GIT_ENV, "PATH": SYSTEM_PATH})
        result = self.run_closeout(ws, ["b8-evidence-index"], wt=wt)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("先 b8a-active-receipt", result.stderr)
        write(wt / "dirty.txt", "x")
        result = self.run_closeout(ws, ["b8-evidence-index"], wt=wt)
        self.assertIn("工作树不干净", result.stderr)

    def test_b8a_temp_receipt_guard_refuses_existing_file(self) -> None:
        """with_temp_receipt 只在收据入库之前临时放入、跑完会删除：工作树里已有同名文件时必须拒绝，免得删掉已入库的收据。"""

        source = (KIT / "closeout.sh").read_text(encoding="utf-8")
        guard = source.split("with_temp_receipt() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('[ ! -e "${receipt}" ] ||', guard)
        self.assertIn('[ ! -e "${WT}/local-analysis" ] ||', guard)
        self.assertLess(guard.index('[ ! -e "${receipt}" ]'), guard.index('cp "${FACTS}/terminal-state-receipt.candidate.json"'))

    # ---- b3-check：四类检查独立、失败不被吞掉、整步退出码如实反映 --------------------------------------------------------------
    # 背景：对着真实环境冒烟时发现旧实现把所有失败都吞了——重封链干跑只 grep“无变化”字样（前置检查没通过、整条没跑成时一行都不打印），
    # manifest 校验经管道 tail 丢了退出码，整步永远退出 0；末尾还打印一条与固定步骤顺序（b3a 在 b3-check 之前）矛盾的“预期红”。

    def b3_workspace(self, ws: Workspace) -> Path:
        """造一个只够 b3-check 跑的假工作树：三个桩脚本（行为由环境变量控制）和两个晋升后回归用的小测试模块。"""

        wt = ws.root / "wt"
        write(wt / "tools/ci/reseal_chain.py",
              "import os, sys\n"
              "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reseal-argv.txt'), 'w').write(' '.join(sys.argv[1:]))\n"
              "if os.environ.get('STUB_RESEAL') == 'fail':\n"
              "    print('前置检查未通过：\\n  - HEAD 是游离的（不在任何分支上）')\n"
              "    sys.exit(2)\n"
              "print('没有需要重绑的叶子，本步无变化。')\n")
        write(wt / "tools/ci/unit_ownership.py",
              "import os, sys\n"
              "if os.environ.get('STUB_OWNERSHIP') == 'fail':\n"
              "    print('归属表检查未通过：1 项')\n"
              "    sys.exit(1)\n"
              "print('归属表检查通过')\n")
        write(wt / "tools/arm64_capture_driver/install.py",
              "import os, sys\n"
              "if os.environ.get('STUB_MANIFEST') == 'fail':\n"
              "    print('manifest.json 与目录内容不一致，请重新 build-manifest。', file=sys.stderr)\n"
              "    sys.exit(1)\n"
              "print('{\"status\": \"unchanged\"}')\n")
        write(wt / "tools/official_client_capture/tests/test_stub_green.py",
              "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        write(wt / "tools/official_client_capture/tests/test_stub_red.py",
              "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.fail('x')\n")
        return wt

    def run_b3(self, ws: Workspace, wt: Path, modules: str, **env: str) -> subprocess.CompletedProcess:
        return self.run_closeout(ws, ["b3-check"], wt=wt, POST_PROMO_MODS=modules, **env)

    def test_b3_check_all_green_exits_zero_and_says_so(self) -> None:
        ws = Workspace(self)
        wt = self.b3_workspace(ws)
        result = self.run_b3(ws, wt, "test_stub_green")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("b3-check 全部通过", result.stdout)
        self.assertNotIn("!!", result.stdout)
        self.assertIn("本步无变化", result.stdout)
        self.assertNotIn("预期红", result.stdout, "固定顺序里 b3a 在 b3-check 之前，不再有“预期红”")

    def test_b3_check_reseal_dry_run_skips_trace(self) -> None:
        """收尾不重新追踪依赖表：干跑必须带 --skip-trace（否则 trace 的前置检查会硬找上一轮目录里的重负载锁脚本，下一轮就失败）。"""

        ws = Workspace(self)
        wt = self.b3_workspace(ws)
        result = self.run_b3(ws, wt, "test_stub_green")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = (wt / "tools/ci/reseal-argv.txt").read_text().split()
        for flag in ("--dry-run", "--skip-trace", "--ack-no-campaign"):
            self.assertIn(flag, argv)

    def test_b3_check_reseal_dry_run_failure_is_visible_and_fails_the_step(self) -> None:
        ws = Workspace(self)
        wt = self.b3_workspace(ws)
        result = self.run_b3(ws, wt, "test_stub_green", STUB_RESEAL="fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("重封链干跑没跑成（退出码 2）", result.stdout)
        self.assertIn("HEAD 是游离的", result.stdout, "必须把日志末尾的失败原因打印出来，不能只靠 grep 无变化字样")
        self.assertIn("test_stub_green rc=0", result.stdout, "一项没通过其余照跑，让操作者一次看全")
        self.assertIn("没通过的项", result.stderr)

    def test_b3_check_ownership_and_manifest_failures_fail_the_step(self) -> None:
        for variable, marker in (("STUB_OWNERSHIP", "归属表检查没通过"), ("STUB_MANIFEST", "驱动 manifest 校验没通过")):
            with self.subTest(variable=variable):
                ws = Workspace(self)
                wt = self.b3_workspace(ws)
                result = self.run_b3(ws, wt, "test_stub_green", **{variable: "fail"})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)

    def test_b3_check_red_module_fails_the_step_and_names_it(self) -> None:
        ws = Workspace(self)
        wt = self.b3_workspace(ws)
        result = self.run_b3(ws, wt, "test_stub_green test_stub_red")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test_stub_red rc=1", result.stdout)
        self.assertIn("晋升后形态回归有红", result.stdout)
        self.assertIn("b3a 还没做", result.stdout)

    def test_source_has_no_push_to_github_and_no_nohup(self) -> None:
        """推 main 不在工具里；本机后台不用 nohup（会让子进程继承忽略 SIGHUP，挂断检测用例必红）。ARM64 本地镜像仓的 push（目标是 ${CLOSE}/mirror.git）不算。"""

        push_to_remote = re.compile(r"git\s+(?:-C\s+\S+\s+)?push\s+(?:-\S+\s+)*origin\b")
        nohup_command = re.compile(r"(?:^|[;&|(]|\$\()\s*nohup\b")
        for path in sorted(KIT.iterdir()):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                code = line.lstrip()
                if code.startswith("#"):
                    continue
                self.assertIsNone(push_to_remote.search(line), (path.name, line))
                self.assertIsNone(nohup_command.search(line), (path.name, line))
        self.assertIn("setsid -f", (KIT / "closeout.sh").read_text(encoding="utf-8"))   # ARM64 上起门禁用 setsid -f


class MakeCloseoutConfig(unittest.TestCase):
    def build(self, tmp: Path):
        closeout = tmp / "rr" / "closeout"
        data = tmp / "data"
        campaign = data / "evidence/campaigns" / CAMPAIGN
        vc6 = campaign / "control/vc6-arm64-x"
        retire = campaign / "control/vc6-retire-deploy-y"
        rr = tmp / "rr"
        write(rr / "vc6/vc6-root.txt", f"{vc6}\n")
        write(rr / "vc6/vc6-retire-root.txt", f"{retire}\n")
        write(rr / "env.sh", "x=1\n", 0o600)
        write(vc6 / "activation-receipt.json", "{}", 0o600)
        write(retire / "stages/retire_switch/stage.json", "{}", 0o600)
        write(campaign / f"control/vc/receipts/{CANDIDATE}/cleanup-decision.json", "{}", 0o600)
        write(closeout / "final-guide.md", "g", 0o600)
        write(closeout / "draft/material.json", "{}", 0o600)
        write(closeout / "terminal-facts-9009.json", "{}", 0o600)
        write(closeout / "deployment_check.py", "#!/bin/bash\n", 0o700)
        write(tmp / "driver/entry-gates.sh", "#!/bin/bash\n", 0o700)
        write(tmp / "act/fact.json", "{}", 0o600)
        (tmp / "node_modules").mkdir()
        (closeout / "repo").mkdir()
        subprocess.run(["git", "init", "-q", str(closeout / "repo")], check=True, capture_output=True, env={**GIT_ENV, "PATH": SYSTEM_PATH})
        argv = ["--closeout-dir", str(closeout), "--target-version", T, "--baseline-version", B, "--runround", str(rr), "--campaign", CAMPAIGN,
                "--candidate", CANDIDATE, "--fact-name", "fact.json", "--data", str(data), "--gate-driver", str(tmp / "driver/entry-gates.sh"),
                "--node-modules", str(tmp / "node_modules"), "--tag", "codex-90090-closeout-20990101", "--expected-tip", "a" * 40,
                "--act-fact-dir", str(tmp / "act"), "--output", str(closeout / "closeout-config.json")]
        return closeout, argv

    def run_cfg(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(KIT / "make_closeout_config.py"), *argv], capture_output=True, text=True,
                              env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)

    def test_config_passes_the_real_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closeout, argv = self.build(Path(os.path.realpath(tmp)))
            result = self.run_cfg(argv)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = closeout / "closeout-config.json"
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            config = json.loads(output.read_text())
            self.assertEqual(config["terminal_relative"], f"docs/egress/maintenance/{PREFIX}_TERMINAL_STATE_RECEIPT.json")
            self.assertEqual(config["terminal_facts"], str(closeout / "terminal-facts-9009.json"))
            self.assertEqual(config["push"], {"remote": "origin", "ref": "refs/heads/main", "expected_tip": "a" * 40})
            self.assertEqual(config["cleanup"]["mode"], "defer")
            self.assertEqual(config["deployment_check"]["script"], str(closeout / "deployment_check.py"))
            self.assertTrue(config["deployment_check"]["arguments"][0].endswith("stages/retire_switch/stage.json"))
            self.assertTrue(config["deployment_check"]["arguments"][2].endswith("act/fact.json"))
            self.assertNotIn("unit_inherit", config["gate"])
            spec = importlib.util.spec_from_file_location("codex_closeout_for_config_test", REPO_ROOT / "tools" / "codex_closeout.py")
            sys.path.insert(0, str(REPO_ROOT))
            try:
                from tools import codex_closeout as cc
            finally:
                sys.path.remove(str(REPO_ROOT))
            del spec
            repo, work = cc.validate_config(config)  # 真实的配置合同校验：字段集合、路径、延期清理、部署核验、推送
            self.assertEqual(Path(repo), closeout / "repo")
            self.assertEqual(Path(work), closeout / "work-1")

    def test_refuses_overwrite_and_inherit_parameters_come_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closeout, argv = self.build(Path(tmp))
            self.assertEqual(self.run_cfg(argv).returncode, 0)
            again = self.run_cfg(argv)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("拒绝覆盖", again.stderr)
            bad = self.run_cfg([*argv[:-1], str(closeout / "other.json"), "--unit-inherit-stage", "pre-ledger"])
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("--gate-work", bad.stderr)
            good = self.run_cfg([*argv[:-1], str(closeout / "third.json"), "--unit-inherit-stage", "pre-ledger", "--gate-work", "/w", "--record-store", "/r"])
            self.assertEqual(good.returncode, 0, good.stderr)
            gate = json.loads((closeout / "third.json").read_text())["gate"]
            self.assertEqual((gate["work"], gate["record_store"], gate["unit_inherit"]), ("/w", "/r", {"stage": "pre-ledger"}))

    def test_work_name_changes_work_root_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closeout, argv = self.build(Path(tmp))
            result = self.run_cfg([*argv[:-1], str(closeout / "c2.json"), "--work-name", "work-2"])
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((closeout / "c2.json").read_text())
            self.assertEqual(config["work_root"], str(closeout / "work-2"))
            self.assertEqual(config["deployment_check"]["result"], str(closeout / "work-2/deployment-check.json"))

    def test_every_version_and_campaign_argument_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _closeout, argv = self.build(Path(tmp))
            for flag in ("--target-version", "--baseline-version", "--campaign", "--candidate", "--fact-name", "--tag", "--expected-tip", "--node-modules", "--runround"):
                index = argv.index(flag)
                result = self.run_cfg(argv[:index] + argv[index + 2:])
                self.assertNotEqual(result.returncode, 0, flag)
                self.assertIn(flag, result.stderr)


class WriteApprovals(unittest.TestCase):
    mod_path = KIT / "write_approvals.py"

    def prepare(self, tmp: Path, *, gates_done: bool = True) -> Path:
        work = tmp / "closeout" / "work-1"
        write(work / "plan.json", json.dumps({"review_sha256": "r" * 64}))
        if gates_done:
            write(work / "journal/gates-done.json", "{}")
        return work

    def run_it(self, work: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(self.mod_path), str(work), "--approved-by", "老板（对话原话）", "--authorization-scope", "只推 main",
                               "--guide-review", "已通读", "--instruction", "同意推送", *extra], capture_output=True, text=True, check=False,
                              env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_writes_two_approvals_and_proof_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self.prepare(Path(tmp))
            result = self.run_it(work, "--valid-hours", "2")
            self.assertEqual(result.returncode, 0, result.stderr)
            parent = work.parent
            proof = json.loads((parent / "approval-proof.json").read_text())
            self.assertEqual((proof["authorized_by"], proof["instructions"], proof["plan_review_sha256"]), ("老板（对话原话）", ["同意推送"], "r" * 64))
            for scope, schema in (("guide-review", "guide-review-approval/v1"), ("release-publication", "codex-closeout-operation-approval/v1")):
                path = parent / f"approval-{scope}.json"
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                value = json.loads(path.read_text())
                self.assertEqual((value["schema_version"], value["status"], value["review_sha256"], value["scope"]), (schema, "approved", "r" * 64, scope))
                self.assertEqual(value["proof"]["sha256"], sha((parent / "approval-proof.json").read_bytes()))
            again = self.run_it(work)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("拒绝覆盖", again.stderr)

    def test_no_approval_before_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self.prepare(Path(tmp), gates_done=False)
            result = self.run_it(work)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("门禁尚未通过", result.stderr)
            self.assertEqual([p.name for p in work.parent.iterdir()], ["work-1"])

    def test_approver_and_instruction_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self.prepare(Path(tmp))
            result = subprocess.run([sys.executable, "-B", str(self.mod_path), str(work)], capture_output=True, text=True,
                                    env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
            self.assertNotEqual(result.returncode, 0)
            for flag in ("--approved-by", "--instruction"):
                self.assertIn(flag, result.stderr)


class GateStatus(unittest.TestCase):
    def test_snapshot_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closeout = Path(tmp)
            work = closeout / "work-1"
            write(closeout / "gates-run.out", "进度\nexit=0\n")
            write(work / "gates.log", "=== 阶段一\n未通过：某项\n=== 阶段二\n")
            write(work / "gates/executor/plan.json", json.dumps({"units": [{}, {}, {}]}))
            events = [{"event": "start", "unit": "a"}, {"event": "exit", "unit": "a", "exit_code": 0},
                      {"event": "exit", "unit": "b", "exit_code": 1, "timed_out": False, "signal": None}]
            write(work / "gates/executor/events.jsonl", "\n".join(json.dumps(e) for e in events) + "\n坏行\n")
            result = subprocess.run([sys.executable, "-B", str(KIT / "gate_status.py"), str(closeout)], capture_output=True, text=True,
                                    env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertIn("EXIT exit=0", lines)
            self.assertTrue(any(line.startswith("ALIVE ") for line in lines))
            self.assertIn("MARK === 阶段二", lines)
            self.assertIn("LOGFAIL 未通过：某项", lines)
            self.assertTrue(any(line.startswith("UNITS 2/3 BAD 1 b(exit=1") for line in lines), lines)
            self.assertTrue(any(line.startswith("DISK ") for line in lines))

    def test_usage_and_empty_directory(self) -> None:
        result = subprocess.run([sys.executable, "-B", str(KIT / "gate_status.py")], capture_output=True, text=True, check=False,
                                env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-B", str(KIT / "gate_status.py"), tmp], capture_output=True, text=True, check=False,
                                    env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0)
            self.assertIn("UNITS 0/None BAD 0", result.stdout)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    body = b'{"status":"ok"}'

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定的方法名
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args) -> None:
        pass


class DeploymentCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _HealthHandler.body = b'{"status":"ok"}'
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        os.symlink(sys.executable, bin_dir / "python3")
        self.docker_json = self.tmp / "docker.json"
        write(bin_dir / "docker", f'#!/bin/bash\ncat "{self.docker_json}"\n', 0o755)
        self.bin = bin_dir
        self.stage = write(self.tmp / "stage.json", json.dumps({"status": "pass", "image_id": "sha256:img", "checks": {"active_version": T, "profile_digest": "p" * 64}}))
        self.activation = write(self.tmp / "activation.json", json.dumps({"final_state": {"active_version": T, "profile_digest": "p" * 64}}))
        self.fact = write(self.tmp / "fact.json", json.dumps({"profile_mode": "active", "codex_version": T, "profile_digest": "p" * 64, "image_id": "sha256:img"}))
        self.result = self.tmp / "work" / "deployment-check.json"
        self.result.parent.mkdir()
        self.set_container()

    def set_container(self, *, image: str = "sha256:img", status: str = "running", health: str = "healthy", ip: str = "127.0.0.1") -> None:
        self.docker_json.write_text(json.dumps({"Image": image, "State": {"Status": status, "Health": {"Status": health}},
                                                "NetworkSettings": {"Networks": {"net1": {"IPAddress": ip}}}}))

    def run_check(self, *extra: str, with_env: bool = True) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:{SYSTEM_PATH}", "PYTHONDONTWRITEBYTECODE": "1"}
        if with_env:
            env.update({"CODEX_CLOSEOUT_OPERATION_KEY": "op", "CODEX_CLOSEOUT_REVIEW_SHA256": "rv", "CODEX_CLOSEOUT_RESULT": str(self.result)})
        return subprocess.run([sys.executable, "-B", str(KIT / "deployment_check.py"), str(self.stage), str(self.activation), str(self.fact),
                               "--container", "gw", "--network", "net1", "--ip", "127.0.0.1", "--port", str(self.port), *extra],
                              capture_output=True, text=True, env=env, check=False)

    def test_success_writes_receipt_and_result(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads((self.result.parent / "deployment-check-receipt.json").read_text())
        self.assertEqual((receipt["image_id"], receipt["state"], receipt["health"], receipt["business_requests"], receipt["container"]), ("sha256:img", "running", "healthy", 0, "gw"))
        self.assertEqual(receipt["activation_fact"]["sha256"], sha(self.fact.read_bytes()))
        outcome = json.loads(self.result.read_text())
        self.assertEqual((outcome["status"], outcome["operation_key"], outcome["review_sha256"]), ("passed", "op", "rv"))
        self.assertEqual(outcome["execution_receipt"]["sha256"], sha((self.result.parent / "deployment-check-receipt.json").read_bytes()))
        self.assertEqual(stat.S_IMODE(self.result.stat().st_mode), 0o600)
        again = self.run_check()
        self.assertEqual(again.returncode, 1)
        self.assertIn("拒绝覆盖", again.stderr)

    def test_failures_exit_1_and_write_nothing(self) -> None:
        cases = {
            "镜像不符": lambda: self.set_container(image="sha256:other"),
            "未运行": lambda: self.set_container(status="exited"),
            "不健康": lambda: self.set_container(health="unhealthy"),
            "固定 IP 不符": lambda: self.set_container(ip="10.0.0.1"),
        }
        for label, mutate in cases.items():
            self.set_container()
            mutate()
            result = self.run_check()
            self.assertEqual(result.returncode, 1, label)
            self.assertIn("部署核验未通过", result.stderr, label)
            self.assertFalse(self.result.exists(), label)
        self.set_container()
        _HealthHandler.body = b'{"status":"bad"}'
        self.assertEqual(self.run_check().returncode, 1)
        self.assertFalse(self.result.exists())

    def test_input_credentials_must_agree(self) -> None:
        write(self.activation, json.dumps({"final_state": {"active_version": "9.9.9", "profile_digest": "p" * 64}}))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("不一致", result.stderr)
        write(self.activation, json.dumps({"final_state": {"active_version": T, "profile_digest": "p" * 64}}))
        write(self.fact, json.dumps({"profile_mode": "candidate", "codex_version": T, "profile_digest": "p" * 64, "image_id": "sha256:img"}))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("激活事实不符", result.stderr)
        write(self.stage, json.dumps({"status": "fail", "image_id": "sha256:img", "checks": {}}))
        self.assertIn("不完整或未通过", self.run_check().stderr)

    def test_environment_variables_are_required(self) -> None:
        result = self.run_check(with_env=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺少收尾动作环境变量", result.stderr)


class PostPromoMods(unittest.TestCase):
    def make_repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        write(repo / "tools/official_client_capture/tests/test_ok.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        write(repo / "tools/official_client_capture/tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.fail('x')\n")
        return repo

    def test_summarises_each_module_and_ends_with_alldone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = self.make_repo(tmp)
            result = subprocess.run(["bash", str(KIT / "post_promo_mods.sh"), str(repo), str(tmp / "log")], capture_output=True, text=True, check=False,
                                    env={"PATH": f"{Path(sys.executable).parent}:{SYSTEM_PATH}", "PYTHONDONTWRITEBYTECODE": "1", "CAPTURE_TYPESCRIPT_MODULE": "/x/ts.js",
                                         "CODEX_SPEC_SOURCE_ROOT": "/x/src", "POST_PROMO_MODS": "test_ok test_bad test_missing"})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = (tmp / "log/results.txt").read_text().splitlines()
            self.assertTrue(lines[0].startswith("test_ok rc=0"), lines)
            self.assertTrue(lines[1].startswith("test_bad rc=1"), lines)
            self.assertIn("test_missing rc=SKIP", lines[2])
            self.assertEqual(lines[-1], "ALLDONE")

    def test_environment_variables_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            result = subprocess.run(["bash", str(KIT / "post_promo_mods.sh"), str(tmp), str(tmp / "log")], capture_output=True, text=True, check=False,
                                    env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CAPTURE_TYPESCRIPT_MODULE", result.stderr)


class Prerun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        write(self.repo / "a.txt", "a")
        env = {**GIT_ENV, "PATH": SYSTEM_PATH}
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True, env=env)
        self.bin = self.tmp / "bin"
        write(self.bin / "make", '#!/bin/bash\necho "make $*"\n[ "${MAKE_FAIL:-}" = "$1" ] && exit 2\nexit 0\n', 0o755)
        write(self.bin / "heavy", '#!/bin/bash\nexec "$@"\n', 0o755)
        self.main = self.tmp / "main"
        self.main.mkdir()

    def run_prerun(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(KIT / "prerun.sh"), str(self.tmp / "out")], capture_output=True, text=True, check=False,
                              env={"PATH": f"{self.bin}:{SYSTEM_PATH}", "PYTHONDONTWRITEBYTECODE": "1", "PRERUN_REPO": str(self.repo),
                                   "PRERUN_MAIN": str(self.main), "HEAVY_RUN": str(self.bin / "heavy"), **env})

    def test_runs_selected_steps_and_writes_summary(self) -> None:
        result = self.run_prerun(PRERUN_ONLY="shard,egress", MAKE_FAIL="check-egress-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = (self.tmp / "out/SUMMARY.txt").read_text().splitlines()
        self.assertTrue(summary[0].startswith("0-shard rc=0"), summary)
        self.assertIn("0-zerodiff SKIP", summary)
        self.assertTrue(any(line.startswith("3-egress rc=2") for line in summary), summary)   # 红了不停，继续汇总
        self.assertIn("6-capture SKIP", summary)
        log = (self.tmp / "out/3-egress.log").read_text()
        self.assertTrue(log.startswith("START egress"))
        self.assertIn("EXIT=2", log)
        self.assertTrue((self.tmp / "out/ALLDONE").read_text().startswith("ALLDONE"))

    def test_skip_list_and_zerodiff_needs_a_base(self) -> None:
        result = self.run_prerun(PRERUN_SKIP="shard,gounit,gointeg,egress,lint,capture")
        self.assertEqual(result.returncode, 0)
        summary = (self.tmp / "out/SUMMARY.txt").read_text()
        self.assertIn("0-zerodiff rc=1", summary)   # 没设 PRERUN_BASE：无法判断零改动，如实红
        self.assertIn("4-lint SKIP", summary)

    def test_repo_and_main_are_required(self) -> None:
        result = subprocess.run(["bash", str(KIT / "prerun.sh"), str(self.tmp / "out2")], capture_output=True, text=True, check=False,
                                env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRERUN_REPO", result.stderr)


class Detach(unittest.TestCase):
    def test_runs_command_in_new_session_and_appends_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            result = subprocess.run([sys.executable, "-B", str(KIT / "detach.py"), str(log), "echo hi; exit 3"], capture_output=True, text=True, check=False,
                                    env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("log_exists=True", result.stdout)
            self.assertEqual(log.read_text().splitlines(), ["hi", "EXIT=3"])

    def test_usage(self) -> None:
        result = subprocess.run([sys.executable, "-B", str(KIT / "detach.py")], capture_output=True, text=True, check=False,
                                env={"PATH": SYSTEM_PATH, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
