"""新工作树跑门禁的包装（优化第 2 项，``tools/ci/worktree_gate.py`` 与 ``worktree_gate.sh``）。

在系统临时目录里造一个合成主检出（带一个假的重负载锁脚本、假的 TypeScript 解析器与 Codex 源码树），再用包装跑几个小 make 目标，核对：

* 成功：环境变量（``CAPTURE_TYPESCRIPT_MODULE``、``CODEX_SPEC_SOURCE_ROOT``、``PYTHONDONTWRITEBYTECODE``）按主检出配好，make 在临时工作树里跑，经过重负载锁脚本，
  跑完临时工作树被清掉、主检出的工作树登记里只剩它自己，日志最后一行是 ``EXIT=0``；
* 失败：make 的退出码原样进 ``EXIT=``，临时工作树照样清掉；
* 缺环境：Codex 源码树、TypeScript 解析器不存在或摘要不符，或重负载锁脚本找不到，都在跑 make 之前明确报错（退出码 64），不留临时工作树；
* 重负载锁路径从 ``git rev-parse --git-common-dir`` 推主检出（从链接工作树里运行也找得到），``HEAVY_RUN`` 可改，``--no-lock`` 显式不走锁；
* 前端依赖：目标名带 ``frontend`` 才把主检出的 ``frontend/node_modules`` 接成符号链接；
* 清理兜底：只读文件让 git 删不干净时放开权限再删；不是 ``gate-`` 开头、不在父目录下的路径不会被误删；
* 信号：SIGTERM 转给 make 所在进程组，等它退出、清掉临时工作树，``EXIT=143``；
* ``.sh`` 入口能跑通，且不在 ``tools/ci`` 里留下 ``__pycache__``。

所有临时目录都建在系统临时目录（``tempfile``），不在真实工作树里造任何东西；``git`` 调用关掉自动维护，避免与临时目录清理竞争。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_CI = REPO_ROOT / "tools" / "ci"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_wg_test", TOOLS_CI / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wg = _load("worktree_gate")

TS_TEXT = "// 假 typescript.js\n"
GIT_ENV = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto",
           "GIT_CONFIG_VALUE_1": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
MAKEFILE = (
    "CODEX_SPEC_SOURCE_VERSION ?= 9.9.9\n"
    "CAPTURE_TYPESCRIPT_SHA256 := {sha}\n"
    "\n"
    "ok:\n"
    "\t@echo TS=$$CAPTURE_TYPESCRIPT_MODULE\n"
    "\t@echo SRC=$$CODEX_SPEC_SOURCE_ROOT\n"
    "\t@echo PYBC=$$PYTHONDONTWRITEBYTECODE\n"
    "\t@echo CWD=$$(pwd -P)\n"
    "\t@if [ -e frontend/node_modules ]; then echo NODE_MODULES=present; else echo NODE_MODULES=absent; fi\n"
    "\n"
    "frontend-check:\n"
    "\t@if [ -L frontend/node_modules ]; then echo LINKED=yes; else echo LINKED=no; fi\n"
    "\n"
    "fail:\n"
    "\t@echo about-to-fail\n"
    "\t@exit 7\n"
    "\n"
    "args:\n"
    "\t@echo GREETING=$(GREETING)\n"
    "\n"
    "readonly:\n"
    "\t@mkdir ro && echo x > ro/f && chmod -R a-w ro\n"
    "\n"
    "hold:\n"
    "\t@echo $$$$ > $(MARKER)\n"
    "\t@sleep 120\n"
)


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-c", "user.name=包装测试", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
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


class Fixture:
    """合成主检出 ``<root>/repo``：被忽略的 node_modules、Codex 源码树、重负载锁脚本都只存在于这里，新建的工作树里没有——正是要包装补上的现场。"""

    def __init__(self, case: unittest.TestCase) -> None:
        for tool in ("make", "bash", "git"):
            if shutil.which(tool) is None:
                case.skipTest(f"本机没有 {tool}，没法演练包装（显式跳过，不算通过）")
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.main = self.root / "repo"
        self.parent = self.root / "wt"
        self.log = self.root / "gate.log"
        self.record = self.root / "heavy-record.txt"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        write(self.main / "Makefile", MAKEFILE.format(sha=hashlib.sha256(TS_TEXT.encode()).hexdigest()))
        write(self.main / ".gitignore", "frontend/node_modules/\nlocal-analysis/\n")
        write(self.main / "frontend" / "package.json", "{}\n")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "基线")
        self.commit = git(self.main, "rev-parse", "HEAD")
        write(self.main / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js", TS_TEXT)
        write(self.main / "local-analysis" / "sources" / "codex-cli-9.9.9" / "codex-rs" / "Cargo.lock", "# lock\n")
        self.heavy = self.main / "local-analysis" / "opt-0165" / "heavy-run.sh"
        write(self.heavy, '#!/bin/bash\necho "$*" >> "${HEAVY_RECORD}"\nexec "$@"\n')

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root), "WORKTREE_GATE_PARENT": str(self.parent), "HEAVY_RECORD": str(self.record),
                **GIT_ENV, **extra}

    def run(self, *arguments: str, env: dict[str, str] | None = None, repo: Path | None = None) -> int:
        """在进程内跑包装（终端回显丢掉），返回退出码。"""

        with contextlib.redirect_stderr(io.StringIO()):
            return wg.main(["--repo", str(repo or self.main), "--log", str(self.log), *arguments], env=self.env() if env is None else env)

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.parent.glob("gate-*")) if self.parent.exists() else []

    def registered_worktrees(self) -> list[str]:
        return [line for line in git(self.main, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


class RunTests(unittest.TestCase):
    def test_success_configures_the_environment_runs_under_the_lock_and_cleans_up(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "ok"), 0, fixture.log_text())
        text = fixture.log_text()
        lines = text.splitlines()
        self.assertEqual(lines[-1], "EXIT=0")
        self.assertIn(f"TS={fixture.main}/frontend/node_modules/typescript/lib/typescript.js", text)
        self.assertIn(f"SRC={fixture.main}/local-analysis/sources/codex-cli-9.9.9", text)
        self.assertIn("PYBC=1", text)
        self.assertIn("NODE_MODULES=absent", text)   # 目标 ok 不需要前端依赖，不接链接
        self.assertIn(f"CWD={fixture.parent}/gate-{fixture.commit[:12]}-", text)   # make 跑在临时工作树里
        self.assertEqual(fixture.record.read_text(encoding="utf-8").strip(), "make ok")   # 经过了重负载锁脚本
        self.assertEqual(fixture.leftovers(), [])
        self.assertEqual(len(fixture.registered_worktrees()), 1)
        self.assertEqual(git(fixture.main, "status", "--porcelain"), "")

    def test_make_failure_exit_code_is_logged_and_the_worktree_is_still_removed(self) -> None:
        fixture = Fixture(self)
        code = fixture.run(fixture.commit, "fail")
        self.assertEqual(code, 2)   # 配方失败时 make 自己退出 2
        text = fixture.log_text()
        self.assertEqual(text.splitlines()[-1], "EXIT=2")
        self.assertIn("about-to-fail", text)
        self.assertIn("Error 7", text)
        self.assertEqual(fixture.leftovers(), [])
        self.assertEqual(len(fixture.registered_worktrees()), 1)

    def test_branch_names_are_resolved_to_full_commits_and_bad_refs_are_rejected(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run("main", "ok"), 0)
        self.assertIn(f"提交 {fixture.commit}", fixture.log_text())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(wg.main(["--repo", str(fixture.main), "--log", str(fixture.log), "没有这个提交", "ok"], env=fixture.env()), wg.EXIT_SETUP)
        self.assertIn(str(fixture.log), stderr.getvalue())   # 准备阶段出错时终端上也有日志路径
        self.assertEqual(fixture.log_text().splitlines()[-1], f"EXIT={wg.EXIT_SETUP}")

    def test_make_arguments_after_double_dash_are_passed_through(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "args", "--", "GREETING=你好"), 0, fixture.log_text())
        self.assertIn("GREETING=你好", fixture.log_text())

    def test_log_is_appended_and_always_ends_with_exit(self) -> None:
        fixture = Fixture(self)
        fixture.run(fixture.commit, "ok")
        fixture.run(fixture.commit, "fail")
        text = fixture.log_text()
        self.assertEqual(text.count("EXIT="), 2)
        self.assertEqual(text.splitlines()[-1], "EXIT=2")


class EnvironmentErrorTests(unittest.TestCase):
    def assert_setup_error(self, fixture: Fixture, fragment: str, code: int | None = None, *arguments: str, **env: str) -> None:
        fixture.log.unlink(missing_ok=True)   # 日志是追加写的，先清掉前一次运行留下的内容再断言“这一次 make 根本没起”
        self.assertEqual(fixture.run(fixture.commit, "ok", *arguments, env=fixture.env(**env)), wg.EXIT_SETUP if code is None else code)
        text = fixture.log_text()
        self.assertIn(fragment, text)
        self.assertEqual(text.splitlines()[-1], f"EXIT={wg.EXIT_SETUP}")
        self.assertNotIn("TS=", text)   # make 根本没起
        self.assertEqual(fixture.leftovers(), [])
        self.assertEqual(len(fixture.registered_worktrees()), 1)

    def test_missing_codex_source_tree_is_a_clear_error_before_make(self) -> None:
        fixture = Fixture(self)
        os.rename(fixture.main / "local-analysis" / "sources" / "codex-cli-9.9.9", fixture.root / "moved")
        self.assert_setup_error(fixture, "CODEX_SPEC_SOURCE_ROOT 不可用")

    def test_missing_or_mismatched_typescript_module_is_a_clear_error(self) -> None:
        fixture = Fixture(self)
        target = fixture.main / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"
        write(target, "// 被换掉的内容\n")
        self.assert_setup_error(fixture, "摘要不符")
        target.unlink()
        self.assert_setup_error(fixture, "文件不存在")

    def test_environment_overrides_win_and_bad_overrides_are_not_silently_replaced(self) -> None:
        fixture = Fixture(self)
        elsewhere = fixture.root / "elsewhere" / "codex-cli-9.9.9"
        write(elsewhere / "codex-rs" / "Cargo.lock", "# lock\n")
        self.assertEqual(fixture.run(fixture.commit, "ok", env=fixture.env(CODEX_SPEC_SOURCE_ROOT=str(elsewhere))), 0)
        self.assertIn(f"SRC={elsewhere}", fixture.log_text())
        self.assert_setup_error(fixture, "CODEX_SPEC_SOURCE_ROOT 不可用", None, CODEX_SPEC_SOURCE_ROOT=str(fixture.root / "不存在"))

    def test_lock_script_missing_is_an_error_unless_no_lock_is_explicit(self) -> None:
        fixture = Fixture(self)
        fixture.heavy.unlink()
        self.assert_setup_error(fixture, "HEAVY_RUN")
        self.assertEqual(fixture.run(fixture.commit, "ok", "--no-lock"), 0, fixture.log_text())
        self.assertIn("不走重负载锁", fixture.log_text())
        self.assertFalse(fixture.record.exists())

    def test_heavy_run_environment_variable_overrides_the_path(self) -> None:
        fixture = Fixture(self)
        other = fixture.root / "other-heavy.sh"
        write(other, '#!/bin/bash\necho "另一把锁 $*" >> "${HEAVY_RECORD}"\nexec "$@"\n')
        self.assertEqual(fixture.run(fixture.commit, "ok", env=fixture.env(HEAVY_RUN=str(other))), 0)
        self.assertEqual(fixture.record.read_text(encoding="utf-8").strip(), "另一把锁 make ok")
        self.assert_setup_error(fixture, "HEAVY_RUN 要是存在的绝对路径文件", None, HEAVY_RUN="relative/heavy.sh")

    def test_lock_and_main_checkout_are_found_from_a_linked_worktree(self) -> None:
        fixture = Fixture(self)
        linked = fixture.root / "linked"
        git(fixture.main, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
        self.assertEqual(fixture.run(fixture.commit, "ok", repo=linked), 0, fixture.log_text())
        self.assertIn(f"TS={fixture.main}/frontend/node_modules", fixture.log_text())   # 依赖取自主检出，不是链接工作树
        self.assertEqual(fixture.record.read_text(encoding="utf-8").strip(), "make ok")

    def test_default_parent_is_the_sibling_wt_directory_of_the_main_checkout(self) -> None:
        fixture = Fixture(self)
        environment = fixture.env()
        del environment["WORKTREE_GATE_PARENT"]
        self.assertEqual(fixture.run(fixture.commit, "ok", env=environment), 0, fixture.log_text())
        self.assertIn(f"CWD={fixture.root}/repo-wt/gate-", fixture.log_text())
        self.assertEqual(list((fixture.root / "repo-wt").glob("gate-*")), [])

    def test_relative_log_path_is_rejected(self) -> None:
        fixture = Fixture(self)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wg.main(["--repo", str(fixture.main), "--log", "relative.log", fixture.commit, "ok"], env=fixture.env()), wg.EXIT_SETUP)


class NodeModulesTests(unittest.TestCase):
    def test_frontend_targets_get_the_main_checkout_node_modules_linked(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "frontend-check"), 0, fixture.log_text())
        self.assertIn("LINKED=yes", fixture.log_text())
        self.assertEqual(fixture.leftovers(), [])   # 符号链接随工作树一起清掉
        self.assertTrue((fixture.main / "frontend" / "node_modules" / "typescript").is_dir())   # 主检出的依赖没被动

    def test_modes_override_the_automatic_choice(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "ok", "--node-modules", "link"), 0)
        self.assertIn("NODE_MODULES=present", fixture.log_text())
        self.assertEqual(fixture.run(fixture.commit, "frontend-check", "--node-modules", "none"), 0)
        self.assertIn("LINKED=no", fixture.log_text())

    def test_automatic_choice_table(self) -> None:
        self.assertTrue(wg.wants_node_modules("test-frontend", "auto"))
        self.assertTrue(wg.wants_node_modules("test", "auto"))
        self.assertFalse(wg.wants_node_modules("check-egress-spec", "auto"))
        self.assertFalse(wg.wants_node_modules("test-capture-tools", "auto"))


class CleanupTests(unittest.TestCase):
    def test_read_only_files_do_not_leave_the_worktree_behind(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "readonly"), 0, fixture.log_text())
        self.assertEqual(fixture.leftovers(), [])
        self.assertEqual(len(fixture.registered_worktrees()), 1)
        self.assertEqual(fixture.log_text().splitlines()[-1], "EXIT=0")

    def test_keep_retains_the_worktree_for_debugging(self) -> None:
        fixture = Fixture(self)
        self.assertEqual(fixture.run(fixture.commit, "ok", "--keep"), 0)
        kept = fixture.leftovers()
        self.assertEqual(len(kept), 1)
        self.assertIn("--keep", fixture.log_text())
        git(fixture.main, "worktree", "remove", "--force", str(fixture.parent / kept[0]))

    def test_fallback_never_deletes_paths_outside_the_gate_directory(self) -> None:
        fixture = Fixture(self)
        stranger = fixture.parent / "not-a-gate-dir"
        write(stranger / "keep.txt", "不能删\n")
        log = wg.Log(fixture.log, echo=False)
        self.assertFalse(wg.remove_worktree(fixture.main, stranger, fixture.parent, log))
        self.assertTrue((stranger / "keep.txt").exists())
        elsewhere = fixture.root / "gate-looks-right"
        write(elsewhere / "keep.txt", "也不能删\n")
        self.assertFalse(wg.remove_worktree(fixture.main, elsewhere, fixture.parent, log))
        self.assertTrue((elsewhere / "keep.txt").exists())


class SignalAndLauncherTests(unittest.TestCase):
    def wait_for(self, predicate, seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_sigterm_is_forwarded_to_make_then_the_worktree_is_removed(self) -> None:
        fixture = Fixture(self)
        marker = fixture.root / "marker"
        process = subprocess.Popen([sys.executable, "-B", str(TOOLS_CI / "worktree_gate.py"), "--repo", str(fixture.main), "--log", str(fixture.log),
                                    fixture.commit, "hold", "--", f"MARKER={marker}"], env={**fixture.env(), "PYTHONDONTWRITEBYTECODE": "1"},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertTrue(self.wait_for(marker.exists), "make 没有起来：" + (fixture.log.read_text(encoding="utf-8") if fixture.log.exists() else ""))
        holder = int(marker.read_text(encoding="utf-8").strip())
        process.send_signal(signal.SIGTERM)
        # 转发生效的话 make 马上收到 SIGTERM 退出；没转发的话要等 KILL_GRACE_SECONDS（30 秒）后被 SIGKILL，这里 15 秒内必须结束。
        self.assertEqual(process.wait(timeout=15), 128 + signal.SIGTERM)
        self.assertEqual(fixture.log_text().splitlines()[-1], f"EXIT={128 + signal.SIGTERM}")
        self.assertEqual(fixture.leftovers(), [])
        self.assertEqual(len(fixture.registered_worktrees()), 1)

        def gone() -> bool:
            try:
                os.kill(holder, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return False

        self.assertTrue(self.wait_for(gone, 10.0), "make 的配方进程还活着")

    def test_shell_launcher_runs_end_to_end_without_leaving_bytecode(self) -> None:
        fixture = Fixture(self)
        before = (TOOLS_CI / "__pycache__").exists()
        completed = subprocess.run(["bash", str(TOOLS_CI / "worktree_gate.sh"), "--repo", str(fixture.main), "--log", str(fixture.log), fixture.commit, "ok"],
                                   env=fixture.env(), capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(fixture.log_text().splitlines()[-1], "EXIT=0")
        self.assertIn("EXIT=0", completed.stderr)   # 终端上也看得到末行
        self.assertEqual((TOOLS_CI / "__pycache__").exists(), before)
        self.assertEqual(fixture.leftovers(), [])

    def test_launcher_is_executable_and_documented_usage_exits_nonzero_on_bad_arguments(self) -> None:
        self.assertTrue(os.access(TOOLS_CI / "worktree_gate.sh", os.X_OK))
        completed = subprocess.run(["bash", str(TOOLS_CI / "worktree_gate.sh")], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
