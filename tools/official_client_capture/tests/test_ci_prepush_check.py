"""推送前一键预检（优化第 2 项，``tools/ci/prepush_check.py``）。

用合成仓库（系统临时目录里 ``git init`` 出来的小仓库）复现非代码坑，确认预检在几秒内报红、指出原因和修法；绿的项给出证据，缺工具的项报“跳过：原因”而不是静默通过：

* 指南第二部分被改（10-30 白跑约 25 分钟那次）：默认报红并写明新旧摘要和改法；``--allow-part2-change`` 放行时仍查连锁是否做全（登记值同步、无文件残留旧摘要）；
* 冻结承接：范围没触及冻结路径写“未触及”；触及且没有承接收据报红，并给出带完整 40 位提交号、绝对 ``--output`` 的生成命令；补了收据（另一个提交）转绿；
* 前端审计：假 ``pnpm`` 输出高危公告，有例外绿、无例外或例外过期红、拿不到有效结果（联网失败）跳过；不碰受跟踪的 ``frontend/audit.json``；
* ``govulncheck``：假可执行文件覆盖 0（绿）、3（红并列出编号）、其他（跳过）与未安装（跳过）；工作流 go 版本与 go.mod 不一致红；
* 本机收据：用真实的 ``opening_checks`` 写收据再核验，缺收据、红收据、别的提交的收据都红；范围为空跳过；
* 环境变量与路径：主检出里有现成的 TypeScript 解析器／Codex 源码树而当前工作树没有时，红并给出应当 export 的值；
* 只读：整套预检跑完，合成仓库的工作树与索引状态不变，也不在里面留下文件。

测试里的所有临时目录都建在系统临时目录（``tempfile``），不在真实工作树里造任何东西；``git`` 调用关掉自动维护，避免与临时目录清理竞争。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_CI = REPO_ROOT / "tools" / "ci"


def _load(name: str):
    """按路径加载 tools/ci 下的模块（与运行时同一种加载方式，不写字节码）。"""

    spec = importlib.util.spec_from_file_location(f"{name}_prepush_test", TOOLS_CI / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pc = _load("prepush_check")
oc = _load("opening_checks")

GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
SCENARIOS = "tools/official_client_capture"
SHELL_PATH = "/usr/bin:/bin"
# 合成仓库里的假 candidate_rule_assertion：与真实模块同一算法（由 test_stub_algorithm_matches_the_real_function 钉住）。
STUB_ASSERTION = '''\
import hashlib


def source_spec_section_sha256(source_path, fragment):
    lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    heading = "# 第二部分 Codex CLI 客户端规则画像"
    start = next(index for index, line in enumerate(lines) if line.rstrip("\\r\\n") == heading)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("# ")), len(lines))
    return hashlib.sha256("".join(lines[start:end]).encode("utf-8")).hexdigest()
'''


def guide_text(rule: str = "规则一", tail: str = "实施一") -> str:
    return f"# 第一部分 总览\n甲\n# 第二部分 Codex CLI 客户端规则画像\n{rule}\n# 第三部分 实施\n{tail}\n"


def second_chapter_digest(text: str) -> str:
    """第二章（从第二部分标题到下一个一级标题）的摘要，用于在测试里造登记值。"""

    lines = text.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.rstrip("\r\n") == "# 第二部分 Codex CLI 客户端规则画像")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("# ")), len(lines))
    return hashlib.sha256("".join(lines[start:end]).encode("utf-8")).hexdigest()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 关掉 git 自动维护：后台维护进程会与 TemporaryDirectory 的清理竞争（CI 的 git 2.55 偶发 Directory not empty）。
GIT_ENV = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto",
           "GIT_CONFIG_VALUE_1": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-c", "user.name=预检测试", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                             "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main", *arguments],
                            cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL, env={**os.environ, **GIT_ENV})
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} 失败：{result.stdout}{result.stderr}")
    return result.stdout.strip()


def write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Env:
    """合成仓库夹具：main 分支是基点，``work`` 分支是被推送的分支；用完随临时目录一起删。"""

    def __init__(self, case: unittest.TestCase) -> None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repo"
        self.ts_text = "// 假 typescript.js\n"
        self.ts_sha = sha(self.ts_text)
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo / GUIDE, guide_text())
        write(self.repo / SCENARIOS / "candidate_rule_assertion.py", STUB_ASSERTION)
        self.write_scenarios(("1_0_0", "1_1_0"), second_chapter_digest(guide_text()))
        write(self.repo / "Makefile", f"CODEX_SPEC_SOURCE_VERSION ?= 9.9.9\nCAPTURE_TYPESCRIPT_SHA256 := {self.ts_sha}\n")
        write(self.repo / "backend" / "go.mod", "module example.invalid/backend\n\ngo 1.27.3\n")
        write(self.repo / ".github" / "workflows" / "security-scan.yml", "      - run: go version | grep -q 'go1.27.3'\n")
        write(self.repo / "frontend" / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        write(self.repo / "frontend" / "audit.json", "{}\n")
        write(self.repo / ".github" / "audit-exceptions.yml", "version: 1\nexceptions: []\n")
        write(self.repo / "tools" / "check_pnpm_audit_exceptions.py", (REPO_ROOT / "tools" / "check_pnpm_audit_exceptions.py").read_text(encoding="utf-8"))
        (self.repo / "docs" / "egress" / "maintenance").mkdir(parents=True)
        write(self.repo / "docs" / "egress" / "maintenance" / "keep.json", "{}\n")
        commit_all(self.repo, "基线")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

    def write_scenarios(self, versions: tuple[str, ...], digest: str) -> None:
        for version in versions:
            write(self.repo / SCENARIOS / f"codex_upgrade_scenarios_{version}.json",
                  json.dumps({"source_spec": {"path": GUIDE, "fragment": "第二章", "sha256": digest}}, ensure_ascii=False))

    def branch(self) -> None:
        git(self.repo, "switch", "-q", "-c", "work")

    def fake(self, name: str, script: str) -> Path:
        path = self.bin / name
        write(path, "#!/bin/sh\n" + script, 0o755)
        return path

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": f"{self.bin}:{SHELL_PATH}", "HOME": str(self.home), **extra}

    def context(self, *, base: str = "main", tip: str = "HEAD", runner=None, env: dict[str, str] | None = None, **options) -> "pc.Context":
        return pc.build_context(str(self.repo), base, tip, pc.Options(**options), self.env() if env is None else env, runner=runner or pc.run_process)

    def run(self, *arguments: str, env: dict[str, str] | None = None, runner=None) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            code = pc.main(["--repo", str(self.repo), "--base", "main", *arguments], env=self.env() if env is None else env,
                           runner=runner or pc.run_process)
        return code, buffer.getvalue()


class Stub:
    """只替换命中的命令，其余照常真跑（git、python）。"""

    def __init__(self, *rules) -> None:
        self.rules = rules
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, timeout, env=None):
        self.calls.append(list(argv))
        for matcher, response in self.rules:
            if matcher(list(argv)):
                return response
        return pc.run_process(argv, cwd=cwd, timeout=timeout, env=env)


def done(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class StubAlgorithmTests(unittest.TestCase):
    def test_stub_algorithm_matches_the_real_function(self) -> None:
        # 合成仓库里的假函数必须与真实的 source_spec_section_sha256 同一算法，否则其余测试证明不了什么。
        real = _load_real_assertion()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            for text in (guide_text(), guide_text("改过的规则", "改过的实施"), "# 第二部分 Codex CLI 客户端规则画像\n只有一章\n"):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(real(path, "第二章"), second_chapter_digest(text))

    def test_the_real_guide_digest_equals_the_registered_one(self) -> None:
        # 反证：预检在真实仓库的 HEAD 上算出的第二章摘要等于登记值——不会把正常的主线头误拦（登记处与算法都取自真实仓库）。
        ctx = pc.build_context(str(REPO_ROOT), "HEAD", "HEAD", pc.Options(), os.environ)
        registered = pc.latest_registered_digest(ctx, ctx.tip)
        self.assertIsNotNone(registered)
        self.assertEqual(pc.compute_guide_digest(ctx, ctx.tip), registered[1])


def _load_real_assertion():
    from tools.official_client_capture.candidate_rule_assertion import source_spec_section_sha256

    return source_spec_section_sha256


class GuidePart2Tests(unittest.TestCase):
    def test_unchanged_second_chapter_is_green_with_evidence(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / GUIDE, guide_text(tail="只改了第三部分"))
        commit_all(env.repo, "改第三部分")
        result = pc.check_guide_part2(env.context())
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn(second_chapter_digest(guide_text())[:12], result.summary)

    def test_editing_the_second_part_is_red_with_both_digests_and_the_fix(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / GUIDE, guide_text(rule="第二部分被改了"))
        commit_all(env.repo, "改第二部分")
        code, output = env.run("--only", "guide-part2-digest")
        self.assertEqual(code, pc.EXIT_RED, output)
        old, new = second_chapter_digest(guide_text()), second_chapter_digest(guide_text(rule="第二部分被改了"))
        self.assertIn("指南第二部分变了", output)
        self.assertIn(old, output)
        self.assertIn(new, output)
        self.assertIn("3.9", output)
        self.assertIn("--allow-part2-change", output)

    def test_allow_flag_demands_the_chain_be_complete(self) -> None:
        env = Env(self)
        env.branch()
        changed = guide_text(rule="第二部分被改了")
        write(env.repo / GUIDE, changed)
        commit_all(env.repo, "改第二部分，连锁还没做")
        result = pc.check_guide_part2(env.context(allow_part2_change=True))
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("连锁没做全", result.summary)
        self.assertTrue(any("还是" in reason for reason in result.reasons), result.reasons)

    def test_allow_flag_catches_a_file_still_holding_the_old_digest(self) -> None:
        env = Env(self)
        env.branch()
        changed = guide_text(rule="第二部分被改了")
        write(env.repo / GUIDE, changed)
        env.write_scenarios(("1_1_0",), second_chapter_digest(changed))   # 最新一份换了，旧的 1_0_0 还留着旧摘要
        commit_all(env.repo, "连锁只做了一半")
        result = pc.check_guide_part2(env.context(allow_part2_change=True))
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertTrue(any("codex_upgrade_scenarios_1_0_0.json" in reason for reason in result.reasons), result.reasons)

    def test_allow_flag_with_a_complete_chain_is_green(self) -> None:
        env = Env(self)
        env.branch()
        changed = guide_text(rule="第二部分被改了")
        write(env.repo / GUIDE, changed)
        env.write_scenarios(("1_0_0", "1_1_0"), second_chapter_digest(changed))
        commit_all(env.repo, "连锁做全")
        result = pc.check_guide_part2(env.context(allow_part2_change=True))
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn("--allow-part2-change", result.summary)

    def test_registered_value_changed_while_the_guide_did_not_is_red(self) -> None:
        env = Env(self)
        env.branch()
        env.write_scenarios(("1_0_0", "1_1_0"), "f" * 64)
        commit_all(env.repo, "只改登记值")
        result = pc.check_guide_part2(env.context())
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("登记值被改了", result.summary)

    def test_missing_registration_files_fail_closed(self) -> None:
        env = Env(self)
        env.branch()
        for version in ("1_0_0", "1_1_0"):
            (env.repo / SCENARIOS / f"codex_upgrade_scenarios_{version}.json").unlink()
        commit_all(env.repo, "删清单")
        result = pc.run_check(env.context(), "guide-part2-digest")
        self.assertEqual(result.status, pc.FAIL, result)

    def test_unusable_digest_function_is_red_not_a_pass(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / SCENARIOS / "candidate_rule_assertion.py", "raise SystemExit('坏了')\n")
        commit_all(env.repo, "弄坏摘要函数")
        result = pc.run_check(env.context(), "guide-part2-digest")
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("算不出", " ".join(result.reasons))


class WorktreeStateAndHygieneTests(unittest.TestCase):
    def test_clean_checkout_is_green(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "a.txt", "x\n")
        commit_all(env.repo, "加文件")
        result = pc.check_worktree_state(env.context())
        self.assertEqual(result.status, pc.PASS, result)

    def test_dirty_tree_is_red_unless_allowed(self) -> None:
        env = Env(self)
        write(env.repo / "untracked.txt", "x\n")
        self.assertEqual(pc.check_worktree_state(env.context()).status, pc.FAIL)
        self.assertEqual(pc.check_worktree_state(env.context(allow_dirty=True)).status, pc.PASS)

    def test_head_not_the_pushed_commit_is_red_and_dependent_checks_skip(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "a.txt", "x\n")
        tip = commit_all(env.repo, "加文件")
        git(env.repo, "switch", "-q", "main")
        ctx = env.context(tip=tip)
        self.assertEqual(pc.check_worktree_state(ctx).status, pc.FAIL)
        for name in ("freeze-successor", "frontend-audit", "govulncheck"):
            result = pc.run_check(ctx, name)
            self.assertEqual(result.status, pc.SKIP, (name, result))
            self.assertIn("不是被推送的提交", result.summary)

    def test_behind_base_is_red(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "a.txt", "x\n")
        commit_all(env.repo, "work 上的提交")
        git(env.repo, "switch", "-q", "main")
        write(env.repo / "b.txt", "y\n")
        commit_all(env.repo, "main 往前走了")
        git(env.repo, "switch", "-q", "work")
        result = pc.check_worktree_state(env.context())
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("non-fast-forward", " ".join(result.reasons))

    def test_pycache_residue_is_red(self) -> None:
        env = Env(self)
        (env.repo / "tools" / "x" / "__pycache__").mkdir(parents=True)
        (env.repo / "tools" / "x" / "__pycache__" / "a.pyc").write_bytes(b"\0")
        result = pc.check_worktree_state(env.context(allow_dirty=True))
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("__pycache__", " ".join(result.reasons))

    def test_skip_ci_literal_in_a_message_is_red(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "a.txt", "x\n")
        commit_all(env.repo, "回写 VERSION（[skip ci]）")
        result = pc.check_commit_hygiene(env.context())
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("skip ci", " ".join(result.reasons))

    def test_markdown_under_maintenance_is_red(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "docs" / "egress" / "maintenance" / "notes.md", "# 笔记\n")
        commit_all(env.repo, "放了 md")
        result = pc.check_commit_hygiene(env.context())
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("notes.md", " ".join(result.reasons))

    def test_clean_messages_are_green(self) -> None:
        env = Env(self)
        env.branch()
        write(env.repo / "a.txt", "x\n")
        commit_all(env.repo, "正常说明")
        result = pc.check_commit_hygiene(env.context())
        self.assertEqual((result.status, "1 个被推提交" in result.summary), (pc.PASS, True), result)


FROZEN_DRY_RUN_HIT = json.dumps({"dry_run": True, "transitions": [{"path": "frozen/data.txt"}], "required_manual_actions": [], "deleted_frozen_paths": [],
                                 "changed_path_count": 1, "result": "passed_local_evidence_successor"})
FROZEN_DRY_RUN_MISS = json.dumps({"dry_run": True, "transitions": [], "required_manual_actions": [], "deleted_frozen_paths": [], "changed_path_count": 3,
                                  "result": "passed_local_evidence_successor"})


def is_dry_run(argv: list[str]) -> bool:
    return "freeze-successor-generate" in argv and "--dry-run" in argv


def make_frozen_repo(env: Env) -> None:
    """合成仓库里放一份真实的 tools.upstream_merge 和一张承接收据：frozen/data.txt（v1）登记在台账里，冻结覆盖算法跑的是真实代码。"""

    shutil.copytree(REPO_ROOT / "tools" / "upstream_merge", env.repo / "tools" / "upstream_merge", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    write(env.repo / "tools" / "__init__.py", "")
    write(env.repo / "frozen" / "data.txt", "v1\n")
    receipt = {"schema_version": "official-egress-upstream-freeze-successor/v1",
               "transitions": [{"path": "frozen/data.txt", "reason": "示例承接", "predecessor_sha256": sha("v0\n"), "to_sha256": sha("v1\n")}]}
    write(env.repo / "docs" / "egress" / "maintenance" / "demo-freeze-successor.json", json.dumps(receipt, ensure_ascii=False))
    commit_all(env.repo, "冻结台账基线")
    env.base = git(env.repo, "rev-parse", "HEAD")


class FreezeSuccessorTests(unittest.TestCase):
    def test_empty_range_is_green_not_touched(self) -> None:
        env = Env(self)
        result = pc.check_freeze_successor(env.context())
        self.assertEqual(result.status, pc.PASS)
        self.assertIn("未触及冻结范围", result.summary)

    def test_range_that_does_not_touch_frozen_paths_says_not_touched(self) -> None:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "other.txt", "x\n")
        commit_all(env.repo, "改非冻结文件")
        stub = Stub((is_dry_run, done(FROZEN_DRY_RUN_MISS)))
        result = pc.check_freeze_successor(env.context(runner=stub))
        self.assertEqual((result.status, "未触及冻结范围" in result.summary), (pc.PASS, True), result)
        self.assertTrue(any(is_dry_run(call) for call in stub.calls))
        self.assertTrue(all("--before" in call for call in stub.calls if is_dry_run(call)))

    def test_changed_frozen_file_without_a_receipt_is_red_with_a_usable_command(self) -> None:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "frozen" / "data.txt", "v2 源码已改\n")
        tip = commit_all(env.repo, "改冻结文件")
        ctx = env.context(runner=Stub((is_dry_run, done(FROZEN_DRY_RUN_HIT))))
        result = pc.check_freeze_successor(ctx)
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("frozen/data.txt", " ".join(result.reasons))
        command = " ".join(result.fixes)
        self.assertIn(f"--before {env.base}", command)
        self.assertIn(f"--after {tip}", command)
        self.assertIn(f"--output {env.repo}/docs/egress/maintenance/work-freeze-successor.json", command)
        self.assertIn("单独提交", command)

    def test_a_receipt_in_a_separate_commit_turns_it_green(self) -> None:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "frozen" / "data.txt", "v2 源码已改\n")
        commit_all(env.repo, "改冻结文件")
        receipt = {"schema_version": "official-egress-upstream-freeze-successor/v1",
                   "transitions": [{"path": "frozen/data.txt", "reason": "示例承接", "predecessor_sha256": sha("v1\n"), "to_sha256": sha("v2 源码已改\n")}]}
        write(env.repo / "docs" / "egress" / "maintenance" / "demo2-freeze-successor.json", json.dumps(receipt, ensure_ascii=False))
        commit_all(env.repo, "承接收据（单独提交）")
        stub = Stub((is_dry_run, done("", 2, "上游合并工具拒绝：冻结 successor 引用的收据在本区间内发生变化，存在自引用风险")))
        result = pc.check_freeze_successor(env.context(runner=stub))
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn("范围内已含 1 个承接收据文件，dry-run 不适用", result.summary)
        # 源码与收据一起推时 dry-run 必被工具按自引用拒绝：不能因此报红，也根本不该去跑它
        self.assertFalse(any(is_dry_run(call) for call in stub.calls))

    def test_receipt_for_another_path_does_not_hide_an_uncovered_frozen_file(self) -> None:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "frozen" / "data.txt", "v2 源码已改\n")
        commit_all(env.repo, "改冻结文件")
        write(env.repo / "docs" / "egress" / "maintenance" / "other-freeze-successor.json",
              json.dumps({"schema_version": "official-egress-upstream-freeze-successor/v1", "transitions": [
                  {"path": "elsewhere.txt", "reason": "别的文件", "predecessor_sha256": sha("a"), "to_sha256": sha("b")}]}, ensure_ascii=False))
        commit_all(env.repo, "别的收据")
        result = pc.check_freeze_successor(env.context(runner=Stub((is_dry_run, done("", 2, "自引用")))))
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("frozen/data.txt", " ".join(result.reasons))
        self.assertIn("自引用拒绝", " ".join(result.fixes))

    def test_tool_rejection_is_red_and_missing_tool_is_skipped(self) -> None:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "other.txt", "x\n")
        commit_all(env.repo, "改非冻结文件")
        rejected = pc.check_freeze_successor(env.context(runner=Stub((is_dry_run, done("", 2, "上游合并工具拒绝：冻结路径的前序摘要不在任何已登记收据中，链断裂")))))
        self.assertEqual(rejected.status, pc.FAIL)
        self.assertIn("链断裂", " ".join(rejected.reasons))
        missing = pc.check_freeze_successor(env.context(runner=Stub((is_dry_run, done("", 127, "找不到可执行文件")))))
        self.assertEqual(missing.status, pc.SKIP)

    def test_real_dry_run_output_shape_is_understood(self) -> None:
        # 真实 dry-run 的输出字段名（transitions、required_manual_actions、deleted_frozen_paths、changed_path_count）与本检查读取的一致：
        # 取真实代码里文档构造处的键，防止上游改名后预检悄悄读到空值而“未触及”。
        source = (REPO_ROOT / "tools" / "upstream_merge" / "freeze.py").read_text(encoding="utf-8")
        for key in ("transitions", "required_manual_actions", "deleted_frozen_paths", "changed_path_count"):
            self.assertIn(f'"{key}"', source)


AUDIT_HIGH = {"advisories": {"1": {"module_name": "xlsx", "severity": "high", "github_advisory_id": "GHSA-aaaa-bbbb-cccc", "title": "示例"}}, "metadata": {}}


def exceptions_yml(expires: str) -> str:
    return ("version: 1\nexceptions:\n  - package: xlsx\n    advisory: \"GHSA-aaaa-bbbb-cccc\"\n    severity: high\n"
            f"    mitigation: \"只写不读\"\n    expires_on: \"{expires}\"\n")


class FrontendAuditTests(unittest.TestCase):
    def fake_pnpm(self, env: Env, payload: dict | str, code: int = 1) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        env.fake("pnpm", f"cat <<'EOF'\n{body}\nEOF\nexit {code}\n")

    def test_covered_high_advisory_is_green_and_tracked_audit_json_is_untouched(self) -> None:
        env = Env(self)
        write(env.repo / ".github" / "audit-exceptions.yml", exceptions_yml("2999-01-01"))
        commit_all(env.repo, "例外")
        self.fake_pnpm(env, AUDIT_HIGH)
        result = pc.check_frontend_audit(env.context())
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn("高危以上 1 个公告", result.summary)
        self.assertEqual((env.repo / "frontend" / "audit.json").read_text(encoding="utf-8"), "{}\n")
        self.assertEqual(git(env.repo, "status", "--porcelain"), "")

    def test_missing_exception_is_red_with_the_advisory_named(self) -> None:
        env = Env(self)
        self.fake_pnpm(env, AUDIT_HIGH)
        result = pc.check_frontend_audit(env.context())
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("GHSA-aaaa-bbbb-cccc", " ".join(result.reasons))
        self.assertIn("冻结台账", " ".join(result.fixes))

    def test_expired_exception_is_red(self) -> None:
        env = Env(self)
        write(env.repo / ".github" / "audit-exceptions.yml", exceptions_yml("2000-01-01"))
        commit_all(env.repo, "过期例外")
        self.fake_pnpm(env, AUDIT_HIGH)
        result = pc.check_frontend_audit(env.context())
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("expired", " ".join(result.reasons))

    def test_no_pnpm_is_a_skip_with_the_reason(self) -> None:
        env = Env(self)
        result = pc.check_frontend_audit(env.context())
        self.assertEqual(result.status, pc.SKIP)
        self.assertIn("没有 pnpm", result.summary)

    def test_network_failure_is_a_skip_not_a_pass(self) -> None:
        env = Env(self)
        self.fake_pnpm(env, {"error": {"code": "ERR_PNPM_AUDIT_BAD_RESPONSE", "message": "网络不通"}})
        result = pc.check_frontend_audit(env.context())
        self.assertEqual(result.status, pc.SKIP, result)
        self.assertIn("网络不通", result.summary)
        self.fake_pnpm(env, "这不是 JSON")
        self.assertEqual(pc.check_frontend_audit(env.context()).status, pc.SKIP)

    def test_fast_skips_both_networked_checks(self) -> None:
        env = Env(self)
        for check in (pc.check_frontend_audit, pc.check_govulncheck):
            result = check(env.context(fast=True))
            self.assertEqual((result.status, "--fast" in result.summary), (pc.SKIP, True))


class GovulncheckTests(unittest.TestCase):
    def test_clean_scan_is_green_with_evidence(self) -> None:
        env = Env(self)
        env.fake("go", "exit 0\n")
        env.fake("govulncheck", "echo '=== Symbol Results ==='\necho 'No vulnerabilities found.'\necho 'Your code is affected by 0 vulnerabilities.'\n"
                                "echo 'This scan also found 0 vulnerabilities in packages you import and 7'\necho 'vulnerabilities in modules you require, but your code'\nexit 0\n")
        with mock.patch.dict(os.environ, {"HOME": str(env.home)}):
            result = pc.check_govulncheck(env.context())
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn("调用到 0 个漏洞", result.summary)
        self.assertIn("1.27.3", result.summary)

    def test_called_vulnerability_is_red_with_the_ids_and_fixed_versions(self) -> None:
        env = Env(self)
        env.fake("go", "exit 0\n")
        env.fake("govulncheck", "echo 'Vulnerability #1: GO-2026-9999'\necho '    Fixed in: go1.27.4'\nexit 3\n")
        with mock.patch.dict(os.environ, {"HOME": str(env.home)}):
            result = pc.check_govulncheck(env.context())
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("GO-2026-9999（修复版本 go1.27.4）", result.reasons)

    def test_tool_error_is_a_skip_and_missing_tool_is_a_skip(self) -> None:
        env = Env(self)
        env.fake("go", "exit 0\n")
        with mock.patch.dict(os.environ, {"HOME": str(env.home)}):
            self.assertIn("没有 govulncheck", pc.check_govulncheck(env.context()).summary)
            env.fake("govulncheck", "echo 'dial tcp: lookup vuln.go.dev: no such host' >&2\nexit 2\n")
            result = pc.check_govulncheck(env.context())
        self.assertEqual(result.status, pc.SKIP, result)
        self.assertIn("vuln.go.dev", result.summary)

    def test_workflow_version_mismatch_is_red_before_scanning(self) -> None:
        env = Env(self)
        write(env.repo / "backend" / "go.mod", "module example.invalid/backend\n\ngo 1.27.4\n")
        commit_all(env.repo, "升 go.mod 没同步工作流")
        result = pc.check_govulncheck(env.context())
        self.assertEqual(result.status, pc.FAIL, result)
        self.assertIn("security-scan.yml", " ".join(result.reasons))


class LocalReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env(self)
        self.env.branch()
        write(self.env.repo / "a.txt", "x\n")
        self.tip = commit_all(self.env.repo, "被推提交")
        self.receipts = self.env.root / "receipts"

    def receipt(self, commit: str | None = None, *, code: int = 0, clean: bool = True) -> None:
        oc.write_capture_receipt(self.receipts, commit=commit or self.tip, exit_code=code, started_at_utc="2026-10-29T01:00:00Z",
                                 finished_at_utc="2026-10-29T01:30:00Z", host="mac", tree_clean=clean, log_tail="OK")

    def check(self):
        return pc.check_local_receipt(self.env.context(receipt_dirs=[self.receipts]))

    def test_green_receipt_for_the_pushed_commit_passes(self) -> None:
        self.receipt()
        result = self.check()
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn(f"capture-tools-{self.tip}.json", result.summary)
        self.assertIn("未核", result.summary)   # 是否已上传 ARM64 不核，要在证据行里说清

    def test_missing_receipt_is_red_with_the_script_and_directory(self) -> None:
        result = self.check()
        self.assertEqual(result.status, pc.FAIL)
        text = " ".join(result.reasons + result.fixes)
        self.assertIn("local-capture-tools-receipt.sh", text)
        self.assertIn(str(self.receipts), text)

    def test_red_dirty_or_other_commit_receipts_are_red(self) -> None:
        self.receipt(code=1)
        self.assertEqual(self.check().status, pc.FAIL)
        self.receipt(code=0, clean=False)
        self.assertEqual(self.check().status, pc.FAIL)
        shutil.rmtree(self.receipts)
        self.receipt(commit="a" * 40)   # 别的提交的收据不算数
        self.assertEqual(self.check().status, pc.FAIL)

    def test_empty_range_is_skipped(self) -> None:
        result = pc.check_local_receipt(self.env.context(base="HEAD", receipt_dirs=[self.receipts]))
        self.assertEqual(result.status, pc.SKIP)

    def test_directory_order_option_then_env_then_main_checkout(self) -> None:
        ctx = self.env.context(receipt_dirs=[Path("/a")], env={**self.env.env(), "LOCAL_RECEIPT_DIR": "/b"})
        directories = pc.receipt_dirs(ctx)
        self.assertEqual(directories[:2], [Path("/a"), Path("/b")])


class EnvPathsTests(unittest.TestCase):
    def build(self) -> tuple[Env, Path]:
        """主检出（有 typescript 与源码树）＋ 它的链接工作树（什么都没有）：新工作树缺依赖的现场。"""

        env = Env(self)
        main = env.repo
        write(main / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js", env.ts_text)
        write(main / "local-analysis" / "sources" / "codex-cli-9.9.9" / "codex-rs" / "Cargo.lock", "# lock\n")
        git(main, "switch", "-q", "-c", "work")
        linked = env.root / "linked"
        git(main, "worktree", "add", "-q", "--detach", str(linked), "work")
        return env, linked

    def context(self, env: Env, linked: Path, **variables: str):
        return pc.build_context(str(linked), "main", "HEAD", pc.Options(), env.env(**variables))

    def test_new_worktree_without_dependencies_is_red_and_tells_what_to_export(self) -> None:
        env, linked = self.build()
        ctx = self.context(env, linked)
        self.assertEqual(ctx.main_root, env.repo)
        result = pc.check_env_paths(ctx)
        self.assertEqual(result.status, pc.FAIL, result)
        text = " ".join(result.fixes)
        self.assertIn(f"export CAPTURE_TYPESCRIPT_MODULE={env.repo}/frontend/node_modules/typescript/lib/typescript.js", text)
        self.assertIn(f"export CODEX_SPEC_SOURCE_ROOT={env.repo}/local-analysis/sources/codex-cli-9.9.9", text)

    def test_valid_variables_are_green(self) -> None:
        env, linked = self.build()
        ctx = self.context(env, linked, CAPTURE_TYPESCRIPT_MODULE=str(env.repo / "frontend/node_modules/typescript/lib/typescript.js"),
                           CODEX_SPEC_SOURCE_ROOT=str(env.repo / "local-analysis/sources/codex-cli-9.9.9"), LOCAL_RECEIPT_DIR=str(env.root / "r"))
        result = pc.check_env_paths(ctx)
        self.assertEqual(result.status, pc.PASS, result)
        self.assertIn("codex-cli-9.9.9", result.summary)

    def test_bad_values_are_red_each_with_its_reason(self) -> None:
        env, linked = self.build()
        good_ts = str(env.repo / "frontend/node_modules/typescript/lib/typescript.js")
        good_root = str(env.repo / "local-analysis/sources/codex-cli-9.9.9")
        cases = {
            "相对路径": {"CAPTURE_TYPESCRIPT_MODULE": "frontend/ts.js", "CODEX_SPEC_SOURCE_ROOT": good_root},
            "摘要不符": {"CAPTURE_TYPESCRIPT_MODULE": str(env.repo / "Makefile"), "CODEX_SPEC_SOURCE_ROOT": good_root},
            "版本目录不符": {"CAPTURE_TYPESCRIPT_MODULE": good_ts, "CODEX_SPEC_SOURCE_ROOT": str(env.repo / "local-analysis/sources")},
            "收据目录相对": {"CAPTURE_TYPESCRIPT_MODULE": good_ts, "CODEX_SPEC_SOURCE_ROOT": good_root, "LOCAL_RECEIPT_DIR": "relative/dir"},
        }
        for label, variables in cases.items():
            with self.subTest(label):
                result = pc.check_env_paths(self.context(env, linked, **variables))
                self.assertEqual(result.status, pc.FAIL, result)
        link = env.root / "ts-link.js"
        link.symlink_to(good_ts)
        result = pc.check_env_paths(self.context(env, linked, CAPTURE_TYPESCRIPT_MODULE=str(link), CODEX_SPEC_SOURCE_ROOT=good_root))
        self.assertEqual(result.status, pc.FAIL)   # 与 Makefile 前置检查一致：本身是符号链接不接受

    def test_legacy_source_root_variable_is_honoured(self) -> None:
        env, linked = self.build()
        ctx = self.context(env, linked, CAPTURE_TYPESCRIPT_MODULE=str(env.repo / "frontend/node_modules/typescript/lib/typescript.js"),
                           CODEX_0_149_1_SOURCE_ROOT=str(env.repo / "local-analysis/sources/codex-cli-9.9.9"))
        self.assertEqual(pc.check_env_paths(ctx).status, pc.PASS)

    def test_makefile_variable_parser(self) -> None:
        text = "A ?= 1.2.3\nB := abc\nC = x\n# D := no\n  E := indented\n"
        self.assertEqual([pc.parse_make_variable(text, name) for name in "ABCDE"], ["1.2.3", "abc", "x", None, None])


class MainTests(unittest.TestCase):
    def prepare_green(self) -> Env:
        env = Env(self)
        make_frozen_repo(env)
        env.branch()
        write(env.repo / "other.txt", "x\n")
        commit_all(env.repo, "正常提交")
        write(env.repo / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js", env.ts_text)
        write(env.repo / "local-analysis" / "sources" / "codex-cli-9.9.9" / "codex-rs" / "Cargo.lock", "# lock\n")
        write(env.repo / ".gitignore", "frontend/node_modules/\nlocal-analysis/\n")
        commit_all(env.repo, "忽略依赖目录")
        env.fake("go", "exit 0\n")
        env.fake("govulncheck", "echo 'Your code is affected by 0 vulnerabilities.'\nexit 0\n")
        env.fake("pnpm", "echo '{\"advisories\":{},\"metadata\":{}}'\nexit 0\n")
        oc.write_capture_receipt(env.root / "receipts", commit=git(env.repo, "rev-parse", "HEAD"), exit_code=0, started_at_utc="2026-10-29T01:00:00Z",
                                 finished_at_utc="2026-10-29T01:30:00Z", host="mac", tree_clean=True, log_tail="OK")
        return env

    def run_green(self, env: Env, *extra: str) -> tuple[int, str]:
        runner = Stub((is_dry_run, done(FROZEN_DRY_RUN_MISS)))
        return env.run("--receipt-dir", str(env.root / "receipts"), *extra, runner=runner)

    def test_all_green_exits_zero_and_leaves_the_repository_untouched(self) -> None:
        env = self.prepare_green()
        before = sorted(str(path.relative_to(env.repo)) for path in env.repo.rglob("*") if ".git" not in path.relative_to(env.repo).parts)
        status_before = git(env.repo, "status", "--porcelain", "--ignored")
        env_vars = env.env(CAPTURE_TYPESCRIPT_MODULE=str(env.repo / "frontend/node_modules/typescript/lib/typescript.js"),
                           CODEX_SPEC_SOURCE_ROOT=str(env.repo / "local-analysis/sources/codex-cli-9.9.9"))
        code, output = env.run("--receipt-dir", str(env.root / "receipts"), env=env_vars, runner=Stub((is_dry_run, done(FROZEN_DRY_RUN_MISS))))
        self.assertEqual(code, pc.EXIT_OK, output)
        self.assertIn("8 绿、0 红、0 跳过", output)
        after = sorted(str(path.relative_to(env.repo)) for path in env.repo.rglob("*") if ".git" not in path.relative_to(env.repo).parts)
        self.assertEqual(before, after)
        self.assertEqual(git(env.repo, "status", "--porcelain", "--ignored"), status_before)

    def test_a_red_item_gives_exit_one_and_every_red_is_reported_together(self) -> None:
        env = self.prepare_green()
        write(env.repo / GUIDE, guide_text(rule="第二部分被改了"))
        commit_all(env.repo, "改第二部分 [skip ci]")
        code, output = self.run_green(env)
        self.assertEqual(code, pc.EXIT_RED, output)
        for name in ("guide-part2-digest", "commit-hygiene"):
            self.assertIn(f"[红] {name}", output)

    def test_skipped_items_do_not_fail_unless_strict(self) -> None:
        env = self.prepare_green()
        env_vars = env.env(CAPTURE_TYPESCRIPT_MODULE=str(env.repo / "frontend/node_modules/typescript/lib/typescript.js"),
                           CODEX_SPEC_SOURCE_ROOT=str(env.repo / "local-analysis/sources/codex-cli-9.9.9"))
        runner = Stub((is_dry_run, done(FROZEN_DRY_RUN_MISS)))
        code, output = env.run("--fast", "--receipt-dir", str(env.root / "receipts"), env=env_vars, runner=runner)
        self.assertEqual((code, "2 跳过" in output), (pc.EXIT_OK, True), output)
        code, _ = env.run("--fast", "--strict", "--receipt-dir", str(env.root / "receipts"), env=env_vars, runner=runner)
        self.assertEqual(code, pc.EXIT_RED)

    def test_explicit_skip_needs_a_reason_and_prints_it(self) -> None:
        env = self.prepare_green()
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = env.run("--skip", "local-receipt")
        self.assertEqual(code, pc.EXIT_USAGE)
        code, output = self.run_green(env, "--only", "local-receipt", "--skip", "local-receipt", "--skip-reason", "只推文档，不进门禁")
        self.assertEqual(code, pc.EXIT_OK, output)
        self.assertIn("只推文档，不进门禁", output)

    def test_usage_errors_exit_two(self) -> None:
        env = Env(self)
        repo = ["--repo", str(env.repo)]
        for arguments in (["--only", "没有这一项"], ["--range", "main...HEAD"], ["--range", "main..HEAD", "--base", "main"], ["--base", "不存在的基点"]):
            with self.subTest(arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(pc.main([*repo, *arguments], env=env.env()), pc.EXIT_USAGE)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pc.main(["--repo", str(env.root)], env=env.env()), pc.EXIT_USAGE)   # 不在 git 仓库里

    def test_json_output_and_list(self) -> None:
        env = self.prepare_green()
        code, output = self.run_green(env, "--json", "--only", "commit-hygiene")
        document = json.loads(output)
        self.assertEqual((code, document["schema"], [item["name"] for item in document["results"]]), (0, pc.SCHEMA, ["commit-hygiene"]))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(pc.main(["--list"]), pc.EXIT_OK)
        for name in pc.CHECK_NAMES:
            self.assertIn(name, buffer.getvalue())
        self.assertEqual(len(pc.CHECK_NAMES), 8)

    def test_a_crashing_check_is_red_not_silently_green(self) -> None:
        env = Env(self)
        with mock.patch.dict(pc.CHECK_FUNCTIONS, {"env-paths": lambda ctx: 1 / 0}):
            result = pc.run_check(env.context(), "env-paths")
        self.assertEqual(result.status, pc.FAIL)
        self.assertIn("失败关闭", result.summary)

    def test_serial_and_parallel_runs_agree(self) -> None:
        env = self.prepare_green()
        ctx = env.context(runner=Stub((is_dry_run, done(FROZEN_DRY_RUN_MISS))))
        names = ["commit-hygiene", "env-paths", "guide-part2-digest"]
        serial = [(item.name, item.status) for item in pc.run_checks(ctx, names, jobs=1)]
        parallel = [(item.name, item.status) for item in pc.run_checks(ctx, names, jobs=4)]
        self.assertEqual(serial, parallel)
        self.assertEqual([name for name, _ in parallel], names)


if __name__ == "__main__":
    unittest.main()
