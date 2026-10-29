"""ARM64 流水线脚本集（tools/arm64_capture_driver/driver/pipeline）的离线测试。

脚本集把上一版只存在于采集主机和本机分析目录的手工流水线收进驱动目录。这里用 Python unittest 驱动真实的 shell 脚本，
把 ssh／scp／docker／df／pgrep／setsid／sleep 全部换成沙箱里的桩，采集主机的驱动（parse_env.py、vc5-all.sh、round_context.py 等）
也换成假的；脚本本体复制进沙箱后运行（这样可以替换账号开关脚本、也可以做“改坏一处看测试会不会红”的变异验证）。

覆盖的缺陷与合同（对应上一版升级里真实发生过的问题）：
* 采集账号必须在 vc5-all 打出 VC5_ALL_DONE 之后才关（Kilo 一结束就关会让 seal 判环境污染）——含一条变异测试，证明测试真能抓到过早关；
* VC-4 按 $NEWDIR/candidates/$CAND/build-receipt.json 核对构建收据（按构建目录核对曾误停一次）；
* 取 bundle 一律 refs/heads/<分支>，不取 HEAD（bundle 里只有分支引用）；
* 有限重试：只对可证明幂等的步骤重试，其余失败一次就停；逐项断言“哪些重试、哪些不重试”；
* VC-6 清理脚本：运行镜像是“VC-6 最终镜像”或“其后的发布镜像”都放行，收据如实写明当前运行的是哪个，先发版后清理的顺序有测试；
* 脚本里不写死版本、轮次、账号；round-env 模板能渲染并通过真实 parse_env.py；vc2 的 -seq 脚本与生成器输出逐字一致。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER_ROOT = REPO_ROOT / "tools" / "arm64_capture_driver"
PIPELINE = DRIVER_ROOT / "driver" / "pipeline"
REAL_DRIVER = DRIVER_ROOT / "driver"

ZERO_DIGEST = "0" * 64
FAR_FUTURE = "2099-01-01T00:00:00Z"


def _write(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    path.chmod(mode)
    return path


class Sandbox:
    """一次测试用的沙箱：假采集主机目录树 + PATH 里的桩命令 + 流水线脚本副本。"""

    def __init__(self, root: Path, *, release_image: str = "") -> None:
        self.root = root
        self.bin = root / "bin"
        self.data = root / "data"
        self.runroot = root / "rounds" / "roundA"
        self.prep = root / "prep"
        self.drv = root / "drv"           # 假驱动目录（PIPE_DRIVER_DIR）
        self.pipe = root / "pipeline"     # 流水线脚本副本（PIPE_HOME）
        self.log = root / "events.log"    # 桩命令与假脚本共同追加的事件日志
        self.counters = root / "counters"
        self.counters.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PIPELINE, self.pipe, ignore=shutil.ignore_patterns("__pycache__"))
        self._make_stubs()
        self._make_driver()
        self._make_round()

    # ------------------------------------------------------------------ 桩命令
    def _make_stubs(self) -> None:
        b = self.bin
        # 磁盘：永远宽裕（40% 已用，可用 60G）。脚本用 df / 、df -BG / 、df -h / 三种写法，同一份输出都能解析。
        _write(b / "df", """
            #!/bin/bash
            echo "Filesystem Size Used Avail Use% Mounted"
            echo "/dev/stub 100G 40G 60G 40% /"
        """)
        # setsid -f：去掉 -f 后台运行（macOS 没有 setsid）。调用方已把输出重定向到文件。
        _write(b / "setsid", """
            #!/bin/bash
            [ "$1" = "-f" ] && shift
            args=()
            for a in "$@"; do [ -f "$a" ] && case "$a" in /*) ;; *) a="$PWD/$a" ;; esac; args+=("$a"); done
            "${args[@]}" &
            exit 0
        """)
        # sleep：把 30 秒的轮询压成瞬间
        _write(b / "sleep", """
            #!/bin/bash
            exec /bin/sleep 0.05
        """)
        # pgrep：只在“命令行里含沙箱路径”的真实进程里匹配，避免碰到开发机上无关的进程
        _write(b / "pgrep", f"""
            #!/bin/bash
            show=0; pat=""
            for a in "$@"; do
              case "$a" in -af|-fa) show=1 ;; -a) show=1 ;; -f|-x) ;; *) pat="$a" ;; esac
            done
            out=$(ps -axo pid=,command= | grep -F -- "{self.root}" | grep -E -- "$pat" | grep -v -E "grep|pgrep" || true)
            [ -n "$out" ] || exit 1
            if [ "$show" = 1 ]; then echo "$out"; else echo "$out" | awk '{{print $1}}'; fi
        """)
        _write(b / "sha256sum", """
            #!/bin/bash
            exec shasum -a 256 "$@"
        """)
        # ssh／scp：只记录，不真连。ssh 对 parse_env.py 的调用返回假的 export 行（供 local/ 脚本测试）
        _write(b / "ssh", f"""
            #!/bin/bash
            echo "SSH $*" >> "{self.log}"
            case "$*" in
              *parse_env.py*) cat "{self.root}/remote_exports.txt" 2>/dev/null ;;
            esac
            exit 0
        """)
        _write(b / "scp", f"""
            #!/bin/bash
            echo "SCP $*" >> "{self.log}"
        """)
        self.docker_state = self.root / "docker_state.json"
        _write(b / "docker", f"""
            #!/usr/bin/env python3
            import json, sys
            S = "{self.docker_state}"
            state = json.load(open(S))
            args = sys.argv[1:]
            open("{self.log}", "a").write("DOCKER " + " ".join(args) + "\\n")
            def out(text="", rc=0):
                sys.stdout.write(text + ("\\n" if text else "")); raise SystemExit(rc)
            if args[0] == "exec":
                if "-i" in args:
                    import base64, json as _j, time
                    mode = state.get("refresh", "ok")
                    fake = "eyJhbGciOiJub25lIn0." + "A" * 40 + ".sig"
                    sys.stdout.write("refresh driver token=" + fake + "\\n")
                    if mode == "ok":
                        seg = base64.urlsafe_b64encode(_j.dumps({{"exp": int(time.time()) + 10 * 86400}}).encode()).decode().rstrip("=")
                        _j.dump({{"last_refresh": "now", "tokens": {{"access_token": "h." + seg + ".s", "refresh_token": "r" * 20}}}}, open(state["auth_path"], "w"))
                        raise SystemExit(0)
                    raise SystemExit(3 if mode == "unchanged" else 2)
                raise SystemExit(0)
            if args[:2] == ["image", "inspect"]:
                ref = args[2]
                if ref in state["removed"]: out(rc=1)
                if ref in state["images"]: out(state["images"][ref])
                if ref in state["images"].values(): out(ref)
                out(rc=1)
            if args[0] == "ps":
                out(" ".join(state["containers"]))
            if args[0] == "rmi":
                ref = args[1]
                state["removed"] += [ref, state["images"].get(ref, ref)]
                json.dump(state, open(S, "w")); out(ref)
            if args[0] == "stop":
                state["canary"][args[1]] = "exited"; json.dump(state, open(S, "w")); out(args[1])
            if args[0] == "inspect":
                fmt = args[args.index("--format") + 1] if "--format" in args else ""
                name = [a for a in args[1:] if a != "--format" and a != fmt][0]
                if fmt == "{{{{.State.Health.Status}}}}": out(state["health"])
                if fmt == "{{{{.State.Status}}}}":
                    if name in state["canary"]: out(state["canary"][name])
                    out(rc=1)
                if fmt == "{{{{.Image}}}}":
                    if name == "sub2apiplus": out(state["running_image"])
                    out(state["container_images"].get(name, ""))
                out(rc=1)
            out(rc=1)
        """)
        self.path = f"{self.bin}:{os.environ['PATH']}"

    def set_docker(self, **overrides) -> None:
        state = {"health": "healthy", "running_image": "", "images": {}, "removed": [], "containers": [], "container_images": {},
                 "canary": {"vc6-canary-app": "running", "vc6-canary-postgres": "running", "vc6-canary-redis": "running"}}
        state.update(overrides)
        self.docker_state.write_text(json.dumps(state), encoding="utf-8")

    # ------------------------------------------------------------------ 假驱动
    def _make_driver(self) -> None:
        d = self.drv
        # 假 parse_env.py：把参数文件当 KEY=VALUE 逐行读，输出 export KEY='VALUE'（不做任何校验，真解析器另有专门测试）
        _write(d / "parse_env.py", """
            #!/usr/bin/env python3
            import sys
            for line in open(sys.argv[1], encoding="utf-8"):
                line = line.rstrip("\\n")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                print("export %s='%s'" % (key, value))
        """)
        _write(d / "round_context.py", f"""
            #!/usr/bin/env python3
            import os, sys
            open("{self.log}", "a").write("ROUND_CONTEXT\\n")
            n = "{self.counters}/replay"
            c = int(open(n).read()) if os.path.exists(n) else 0
            open(n, "w").write(str(c + 1))
            fails = int(os.environ.get("STUB_REPLAY_FAILS", "0"))
            sys.exit(1 if c < fails else 0)
        """)
        _write(d / "background-validate.sh", f"""
            #!/bin/bash
            echo "BGV $*" >> "{self.log}"
            exit 0
        """)
        _write(d / "entry-dryrun.sh", f"""
            #!/bin/bash
            echo "HOUSEKEEPING $*" >> "{self.log}"
            exit 0
        """)
        _write(d / "vc5-precheck.sh", f"""
            #!/bin/bash
            echo "PRECHECK $*" >> "{self.log}"
            exit 0
        """)
        # 假 vc5-all.sh：先报 Kilo 完成，隔一会儿才打 VC5_ALL_DONE（真实流程里 seal／compare／accept 还要跑很久）
        _write(d / "vc5-all.sh", f"""
            #!/bin/bash
            echo "VC5_ALL_START"
            /bin/sleep 0.2
            echo "KILO_DONE"
            /bin/sleep 0.5
            echo "VC5_ALL_DONE"
            echo "VC5ALL_FINISHED" >> "{self.log}"
        """)

    # ------------------------------------------------------------------ 假轮次（参数文件与数据根）
    def _make_round(self) -> None:
        self.newdir = self.data / "evidence" / "campaigns" / "formal-x"
        env = {
            "D": str(self.data), "RUNROOT": str(self.runroot), "NEW": "formal-x", "NEWDIR": str(self.newdir), "IN": "inputs-x",
            "CAND": "cand-x", "B": str(self.data / "candidates" / "cand-x"), "L": str(self.root / "ledger"),
            "W": str(self.data / "control" / "inputs-x"), "ROUND": "roundA", "STAMP": "20990101t000000z",
            "CAMPAIGN_PREFIX": "cx", "BASELINE_VERSION": "1.0.0", "TARGET_VERSION": "1.1.0", "PROFILE_DIGEST": "ab" * 32,
            "TARGET_PROFILE_ID": "p-x", "CODEX_ACCOUNT_ID": "7", "API_KEY_ID": "9", "BUNDLE": str(self.root / "bundle.bundle"),
            "BUNDLE_BRANCH": "feature/x", "ENTRY_BUNDLE": str(self.root / "entry.bundle"), "ENTRY_BRANCH": "feature/y",
            "ENTRY_COMMIT": "1" * 40, "C": "2" * 40, "DC": "3" * 40, "RECEIPT": "docs/egress/maintenance/x.json",
            "COMPOSE_DIR": str(self.root / "compose"), "COMPOSE_BACKUP": str(self.root / "compose.backup"),
            "PRODUCTION_IMAGE": "ghcr.io/org/app:1@sha256:" + "c" * 64, "HISTORY_TEST_TREE": str(self.root / "hist"),
            "RETIRE_VERSION": "0.9.0", "PROJECT_DEADLINE_UTC": FAR_FUTURE, "MIN_FREE_GIB": "40",
        }
        self.env_file = self.runroot / "env.sh"
        _write(self.env_file, "\n".join(f"{k}={v}" for k, v in env.items()) + "\n", 0o600)
        self.env = env
        (self.root / "compose").mkdir(parents=True, exist_ok=True)
        (self.root / "compose" / "docker-compose.yml").write_text(f"services:\n  app:\n    image: {env['PRODUCTION_IMAGE']}\n", encoding="utf-8")
        # 计时账本：假的 effective_deadlines（脚本在 cwd=数据根、PYTHONPATH=数据根下 import 它）
        for pkg in ("tools", "tools/official_client_capture"):
            _write(self.data / pkg / "__init__.py", "", 0o644)
        _write(self.data / "tools/official_client_capture/codex_upgrade_vc_artifacts.py", f"""
            def effective_deadlines(path):
                return {{"phase": "VC-5", "execution_deadline_at_utc": "{FAR_FUTURE}", "total_deadline_at_utc": "{FAR_FUTURE}",
                        "project_deadline_at_utc": "{FAR_FUTURE}", "paused_scopes": []}}
        """, 0o644)
        (self.newdir / "control" / "vc").mkdir(parents=True, exist_ok=True)
        (self.prep / "pipeline").mkdir(parents=True, exist_ok=True)

    def set_env(self, **values: str) -> None:
        """改写或追加参数文件里的键（假 parse_env 逐行读取）。"""

        lines = [line for line in self.env_file.read_text(encoding="utf-8").splitlines() if line.split("=", 1)[0] not in values]
        lines += [f"{k}={v}" for k, v in values.items()]
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.env.update(values)

    def checkpoint(self, number: int) -> None:
        (self.newdir / "control" / "vc" / f"vc-{number}-checkpoint.json").write_text("{}", encoding="utf-8")

    # ------------------------------------------------------------------ 运行
    def environ(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPE_", "CAPTURE_", "ARM64_", "STUB_"))}
        env.update({
            "PATH": self.path, "PIPE_DRIVER_DIR": str(self.drv), "PIPE_PREP": str(self.prep), "ARM64_VC_ENV": str(self.env_file),
            "PIPE_RETRY_SLEEP": "0", "PIPELINE_ENV": "", "PIPE_LOCAL_REPO": str(REPO_ROOT), "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.root),
        })
        env.pop("PIPELINE_ENV")
        env.update(extra)
        return env

    def run(self, argv: list[str], timeout: int = 60, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=self.environ(**extra), cwd=self.root, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def status_lines(self) -> list[str]:
        path = self.prep / "pipeline" / "vc-status.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def events(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def stage_pipeline(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return self.run(["bash", str(self.pipe / "vc-pipeline.sh"), "--run", *args], **extra)


class SandboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.sb = Sandbox(self.root)


def _account_stub(sb: Sandbox, name: str, body: str) -> None:
    _write(sb.pipe / name, body)


# ======================================================================================================
# 一、采集账号关闭时机：必须在 VC5_ALL_DONE 之后
# ======================================================================================================
class CaptureAccountOrderTests(SandboxCase):
    def _install_account_stubs(self) -> Path:
        """开关脚本换成桩：每次调用在 account.log 追加一行，off 同时记下此刻 vc5-all.out 里有没有 VC5_ALL_DONE。"""

        account_log = self.root / "account.log"
        _account_stub(self.sb, "capture-account-on.sh", f"""
            #!/bin/bash
            echo "ON" >> "{account_log}"
        """)
        _account_stub(self.sb, "capture-account-off.sh", f"""
            #!/bin/bash
            done=0; grep -q VC5_ALL_DONE "{self.sb.runroot}/vc5-all.out" 2>/dev/null && done=1
            kilo=0; grep -q KILO_DONE "{self.sb.runroot}/vc5-all.out" 2>/dev/null && kilo=1
            echo "OFF done=$done kilo=$kilo" >> "{account_log}"
        """)
        return account_log

    def _prepare_vc5(self) -> None:
        sb = self.sb
        sb.runroot.mkdir(parents=True, exist_ok=True)
        sb.checkpoint(4)
        sb.checkpoint(5)
        (sb.newdir / "control" / "vc" / "receipts" / "cand-x").mkdir(parents=True, exist_ok=True)
        (sb.newdir / "control" / "vc" / "receipts" / "cand-x" / "vc5-completion.json").write_text("{}", encoding="utf-8")

    def _off_lines(self, account_log: Path) -> list[str]:
        return [line for line in account_log.read_text(encoding="utf-8").splitlines() if line.startswith("OFF")]

    def test_account_closed_only_after_vc5_all_done(self) -> None:
        """上一版的缺陷：Kilo 双入口一结束就关采集账号，seal 的“Kilo 后环境恢复门禁”拿 Kilo 后探针对比候选采集后探针，
        账号 status／schedulable 不一致，判环境污染失败。正确顺序：开 → vc5-all 全部跑完（VC5_ALL_DONE）→ 才关。"""

        account_log = self._install_account_stubs()
        self._prepare_vc5()
        result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        lines = account_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "ON", lines)
        offs = self._off_lines(account_log)
        self.assertEqual(len(offs), 1, lines)
        self.assertEqual(offs[0], "OFF done=1 kilo=1", "关账号时 vc5-all 必须已经打出 VC5_ALL_DONE")
        self.assertIn("PASS VC5", "\n".join(self.sb.status_lines()))
        self.assertIn("VC5ALL_FINISHED", self.sb.events())

    def test_premature_close_is_caught_by_this_test_mutation(self) -> None:
        """变异验证：把脚本改回上一版的错误行为（见到 KILO_DONE 就关账号），上面那条不变量必须被打破——
        证明 test_account_closed_only_after_vc5_all_done 真能抓到“过早关”，不是摆设。"""

        account_log = self._install_account_stubs()
        self._prepare_vc5()
        script = self.sb.pipe / "vc-pipeline.sh"
        text = script.read_text(encoding="utf-8")
        needle = """    grep -q 'VC5_ALL_DONE' "${RR}/vc5-all.out" && break\n    if grep -q 'VC5_ALL_PAUSED'"""
        self.assertEqual(text.count(needle), 1, "监视循环的写法变了，请同步更新变异点")
        mutated = text.replace(needle, """    grep -q 'KILO_DONE' "${RR}/vc5-all.out" && [ "${CAPTURE_STATE}" = on ] && capture_off\n""" + needle)
        script.write_text(mutated, encoding="utf-8")
        result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        offs = self._off_lines(account_log)
        self.assertTrue(offs, "变异后应当出现关账号记录")
        self.assertEqual(offs[0], "OFF done=0 kilo=1", "变异版在 VC5_ALL_DONE 之前就关了账号，正确的测试必须据此判红")
        with self.assertRaises(AssertionError):
            self.assertEqual(offs[0], "OFF done=1 kilo=1")

    def test_account_not_closed_when_vc5_all_never_finishes(self) -> None:
        """vc5-all 异常退出（没有 VC5_ALL_DONE）时流水线 STOPPED，不替人关账号，只留一行 NOTE 提示手工关——
        因为此时是否已经过了 seal 的对比点无法判断，宁可留着不关，也不能在半路关。"""

        account_log = self._install_account_stubs()
        self._prepare_vc5()
        _write(self.sb.drv / "vc5-all.sh", "#!/bin/bash\necho 'KILO_DONE'\nexit 1\n")
        result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self._off_lines(account_log), account_log.read_text(encoding="utf-8"))
        text = "\n".join(self.sb.status_lines())
        self.assertIn("STOPPED VC5", text)
        self.assertIn("采集账号仍是开启状态", text)
        self.assertTrue(text.splitlines()[-1].split(" ", 1)[1].startswith("EXIT rc=1"), text.splitlines()[-1])

    def test_capture_on_retry_classification(self) -> None:
        """采集账号开关脚本文档保证幂等：退出码 5（管理接口失败）可重试；退出码 4（访问令牌快过期，确定性拒绝）不重试。"""

        counter = self.root / "on_count"
        for code, expect_calls, expect_pass in ((5, 3, False), (4, 1, False)):
            with self.subTest(exit_code=code):
                counter.write_text("0")
                _account_stub(self.sb, "capture-account-on.sh", f"""
                    #!/bin/bash
                    n=$(cat "{counter}"); echo $((n + 1)) > "{counter}"
                    exit {code}
                """)
                self._prepare_vc5()
                shutil.rmtree(self.sb.prep, ignore_errors=True)
                (self.sb.prep / "pipeline").mkdir(parents=True)
                result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5", PIPE_RETRY_MAX="2")
                self.assertEqual(result.returncode, 1, result.stdout[-800:])
                self.assertEqual(int(counter.read_text()), expect_calls, "retry 次数不对")
                text = "\n".join(self.sb.status_lines())
                self.assertIn("STOPPED VC5", text)
                if code == 5:
                    self.assertEqual(text.count("RETRY capture-account on"), 2, text)
                else:
                    self.assertNotIn("RETRY capture-account on", text)


# ======================================================================================================
# 二、有限重试：pl_retry 语义，以及 VC6 的“默认不重试、ORDER 声明 retry 才重试”
# ======================================================================================================
class RetryTests(SandboxCase):
    def _retry(self, script: str, **extra: str) -> subprocess.CompletedProcess:
        harness = self.root / "harness.sh"
        _write(harness, f"""
            #!/bin/bash
            source "{self.sb.pipe}/pipeline-lib.sh"
            log() {{ echo "$*"; }}
            counter="{self.root}/calls"; echo 0 > "$counter"
            attempt() {{ calls_so_far=$(cat "$counter"); n=$((calls_so_far + 1)); echo $n > "$counter"; {script}; }}
        """)
        return self.sb.run(["bash", "-c", f'source "{harness}"; pl_retry "步骤" "$CODES" attempt; echo "rc=$?"; echo "calls=$(cat "$counter")"'],
                           PIPE_RETRY_MAX="2", **extra)

    def test_retry_until_success(self) -> None:
        result = self._retry('[ "$n" -ge 3 ] && return 0; return 5', CODES="")
        self.assertIn("rc=0", result.stdout)
        self.assertIn("calls=3", result.stdout)
        self.assertEqual(result.stdout.count("RETRY 步骤"), 2)

    def test_retry_exhausted_returns_last_exit_code(self) -> None:
        result = self._retry("return 7", CODES="")
        self.assertIn("rc=7", result.stdout)
        self.assertIn("calls=3", result.stdout, "上限 2 次重试＝最多调用 3 次")

    def test_retry_skips_deterministic_exit_codes(self) -> None:
        result = self._retry("return 4", CODES="3,5,6")
        self.assertIn("rc=4", result.stdout)
        self.assertIn("calls=1", result.stdout, "不在可重试退出码列表里的失败必须立刻返回")

    def _source_lib(self, body: str, **extra: str) -> subprocess.CompletedProcess:
        wrapper = _write(self.root / "source_lib.sh", f"""
            #!/bin/bash
            source "{self.sb.pipe}/pipeline-lib.sh"
            {body}
        """)
        return self.sb.run(["bash", str(wrapper)], **extra)

    def test_unknown_pipeline_env_key_is_rejected(self) -> None:
        cfg = self.root / "p.env"
        cfg.write_text("PIPE_NO_SUCH_KEY=1\n", encoding="utf-8")
        result = self._source_lib("echo loaded", PIPELINE_ENV=str(cfg))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未知键", result.stderr)

    def test_pipeline_env_rejects_command_substitution_and_keeps_environment_priority(self) -> None:
        bad = self.root / "bad.env"
        bad.write_text("PIPE_PREP=$(touch /tmp/pwned)\n", encoding="utf-8")
        result = self._source_lib("echo loaded", PIPELINE_ENV=str(bad))
        self.assertNotEqual(result.returncode, 0)
        good = self.root / "good.env"
        good.write_text("PIPE_RETRY_MAX=5\nPIPE_RETRY_SLEEP=1\n", encoding="utf-8")
        result = self._source_lib('echo "$PIPE_RETRY_MAX/$PIPE_RETRY_SLEEP"', PIPELINE_ENV=str(good), PIPE_RETRY_MAX="9")
        self.assertEqual(result.stdout.strip(), "9/0", "环境变量优先于 pipeline.env（测试沙箱里 PIPE_RETRY_SLEEP 已设为 0）")


class Vc6RetryPolicyTests(SandboxCase):
    """VC6 阶段：run 步骤默认失败一次就停；只有 ORDER 行带 retry 标志的步骤才重试。"""

    def _vc6(self, order: str, scripts: dict[str, str], **extra: str) -> subprocess.CompletedProcess:
        sb = self.sb
        sb.runroot.mkdir(parents=True, exist_ok=True)
        sb.checkpoint(5)
        (sb.runroot / "vc5-admission.json").write_text("{}", encoding="utf-8")
        for name, body in scripts.items():
            _write(sb.pipe / "vc6" / name, body)
        (sb.prep / "vc6").mkdir(parents=True, exist_ok=True)
        (sb.prep / "vc6" / "ORDER.txt").write_text(order, encoding="utf-8")
        # 跑完 ORDER 后流水线还会核对 vc-6-checkpoint；步骤全成功时由最后一个脚本“写出”它
        return sb.stage_pipeline("--from", "VC6", "--to", "VC6", PIPE_RETRY_MAX="2", **extra)

    def _counting(self, name: str, fail_first: int, final_checkpoint: bool = False) -> str:
        counter = self.root / f"{name}.count"
        counter.write_text("0")
        extra = f'touch "{self.sb.newdir}/control/vc/vc-6-checkpoint.json"' if final_checkpoint else ":"
        return f"""
            #!/bin/bash
            n=$(cat "{counter}"); echo $((n + 1)) > "{counter}"
            [ "$n" -lt {fail_first} ] && exit 9
            {extra}
            exit 0
        """

    def _calls(self, name: str) -> int:
        return int((self.root / f"{name}.count").read_text())

    def test_step_without_retry_flag_stops_after_single_failure(self) -> None:
        scripts = {"step-a.sh": self._counting("step-a", fail_first=1, final_checkpoint=True)}
        result = self._vc6("run     step-a    step-a.sh\n", scripts)
        self.assertEqual(result.returncode, 1, result.stdout[-800:])
        self.assertEqual(self._calls("step-a"), 1, "没声明 retry 的 run 步骤失败一次就停，绝不重试")
        text = "\n".join(self.sb.status_lines())
        self.assertIn("STOPPED VC6", text)
        self.assertIn("没有声明可重试", text)
        self.assertNotIn("RETRY", text)

    def test_step_with_retry_flag_is_retried_then_passes(self) -> None:
        scripts = {"step-b.sh": self._counting("step-b", fail_first=2, final_checkpoint=True)}
        result = self._vc6("run     step-b    step-b.sh    retry\n", scripts)
        self.assertEqual(result.returncode, 0, result.stdout[-800:] + result.stderr[-800:])
        self.assertEqual(self._calls("step-b"), 3)
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(text.count("RETRY vc6 step-b"), 2)
        self.assertIn("PASS VC6", text)

    def test_retry_flag_is_bounded(self) -> None:
        scripts = {"step-c.sh": self._counting("step-c", fail_first=99)}
        result = self._vc6("run     step-c    step-c.sh    retry\n", scripts)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self._calls("step-c"), 3, "PIPE_RETRY_MAX=2 → 最多 3 次调用")
        self.assertIn("STOPPED VC6", "\n".join(self.sb.status_lines()))

    def test_done_marker_skips_completed_steps_on_resume(self) -> None:
        scripts = {"step-d.sh": self._counting("step-d", fail_first=0, final_checkpoint=True)}
        first = self._vc6("run     step-d    step-d.sh\n", scripts)
        self.assertEqual(first.returncode, 0)
        shutil.rmtree(self.sb.prep / "pipeline" / "vc-pipeline.lock", ignore_errors=True)
        second = self.sb.stage_pipeline("--from", "VC6", "--to", "VC6")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self._calls("step-d"), 1, "已成功的步骤续跑时不重跑（这些脚本多数拒绝覆盖已完成阶段）")

    def test_replay_check_retries_but_only_a_bounded_number_of_times(self) -> None:
        """round_context 只读重放可证明幂等：瞬时失败会重试；持续失败则在上限内停下。"""

        scripts = {"ok.sh": self._counting("ok", fail_first=0, final_checkpoint=True)}
        result = self._vc6("run     ok    ok.sh\n", scripts, STUB_REPLAY_FAILS="2")
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        self.assertIn("RETRY round_context 只读重放", "\n".join(self.sb.status_lines()))
        shutil.rmtree(self.sb.prep, ignore_errors=True)
        (self.sb.prep / "pipeline").mkdir(parents=True)
        (self.sb.prep / "vc6").mkdir(parents=True)
        (self.sb.prep / "vc6" / "ORDER.txt").write_text("run     ok    ok.sh\n", encoding="utf-8")
        (self.root / "replay").unlink(missing_ok=True) if (self.root / "replay").exists() else None
        (self.sb.counters / "replay").write_text("0")
        result = self.sb.stage_pipeline("--from", "VC6", "--to", "VC6", PIPE_RETRY_MAX="1", STUB_REPLAY_FAILS="99")
        self.assertEqual(result.returncode, 1)
        self.assertIn("STOPPED VC6", "\n".join(self.sb.status_lines()))


# ======================================================================================================
# 三、VC-6 清理脚本：运行镜像是最终镜像或其后的发布镜像都放行，收据如实写明
# ======================================================================================================
class Vc6CleanupTests(SandboxCase):
    FINAL = "sha256:" + "f1" * 32
    ROLLBACK_ID = "sha256:" + "a1" * 32
    ACTIVATION = "sha256:" + "b2" * 32
    CANDIDATE = "sha256:" + "c3" * 32
    RELEASE = "sha256:" + "d4" * 32
    RELEASE_REF = "ghcr.io/org/app:2.0.0"

    def setUp(self) -> None:
        super().setUp()
        sb = self.sb
        self.script = sb.pipe / "vc6" / "vc6-cleanup-execute.sh"
        self.archive_dir = self.root / "archives"
        self.archive_dir.mkdir()
        (self.archive_dir / "codex-1.1.0-20990101.tar.gz").write_bytes(b"x")
        # vc6-env.sh 换成桩：只导出清理脚本要用的坐标（真 vc6-env.sh 另有占位断言，见 Vc6EnvTests）
        _write(sb.pipe / "vc6" / "vc6-env.sh", f"""
            export D="{sb.data}" RUNROOT="{sb.runroot}" NEW=formal-x CAND=cand-x TARGET_VERSION=1.1.0 ROLLBACK_IMAGE_ID={self.ROLLBACK_ID}
        """, 0o644)
        self.camp = sb.data / "evidence" / "campaigns" / "formal-x"
        receipts = self.camp / "control" / "vc" / "receipts" / "cand-x"
        receipts.mkdir(parents=True)
        (receipts / "vc5-completion.json").write_text(json.dumps({"subject": {"candidate_id": "cand-x"}}), encoding="utf-8")
        v6 = sb.runroot / "vc6"
        retire_root = sb.data / "evidence" / "campaigns" / "formal-x" / "control" / "vc6-retire-deploy-x"
        prod_root = sb.data / "control" / "prod-root"
        for path in (v6, retire_root / "cleanup", retire_root / "build", prod_root / "build"):
            path.mkdir(parents=True, exist_ok=True)
        (v6 / "vc6-retire-root.txt").write_text(str(retire_root), encoding="utf-8")
        (v6 / "vc6-root.txt").write_text(str(prod_root), encoding="utf-8")
        (retire_root / "build" / "retire-build-parameters.json").write_text(json.dumps({"docker_build": {"image_id": self.FINAL}}), encoding="utf-8")
        (prod_root / "build" / "production-build-parameters.json").write_text(json.dumps({"docker_build": {"image_id": self.ACTIVATION}}), encoding="utf-8")
        self.junk = sb.data / "candidates" / "cand-x" / "retire-build-tree"
        self.junk.mkdir(parents=True)
        (self.junk / "f.txt").write_text("x", encoding="utf-8")
        self.manifest = {
            "delete_candidates": [{"path": str(self.junk), "file_count": 1, "bytes": 1}],
            "candidate_images": [{"reference": "cand-img:1", "image_id": self.CANDIDATE, "size": "1MB"}],
        }
        (retire_root / "cleanup" / "cleanup-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.retire_root = retire_root
        # 假的收据工具：finalize 把 facts 原样落成收据文件，replay 只打印
        tool = sb.data / "tools" / "official_client_capture" / "codex_upgrade_vc_receipt.py"
        _write(tool, """
            import json, sys
            from pathlib import Path
            args = sys.argv[1:]
            if args[0] == "finalize":
                root = Path(args[args.index("--evidence-root") + 1]); facts = root / args[args.index("--facts") + 1]
                out = root / args[args.index("--output") + 1]
                out.write_text(json.dumps({"facts": json.loads(facts.read_text())}), encoding="utf-8"); print("finalized")
            else:
                print("replayed")
        """, 0o644)

    def _docker(self, running: str, images: dict[str, str] | None = None, **extra) -> None:
        mapping = {self.RELEASE_REF: self.RELEASE, self.FINAL: self.FINAL, "cand-img:1": self.CANDIDATE, self.ROLLBACK_ID: self.ROLLBACK_ID,
                   self.ACTIVATION: self.ACTIVATION}
        mapping.update(images or {})
        self.sb.set_docker(running_image=running, images=mapping, **extra)

    def _run(self, *args: str, release: str = "", approved: str = "测试批准依据") -> subprocess.CompletedProcess:
        extra = {"VC6_CLEANUP_APPROVED_BY": approved, "VC6_ARCHIVE_DIR": str(self.archive_dir), "PYTHONPATH": str(self.sb.data)}
        if release:
            extra["VC6_RELEASE_IMAGE"] = release
        return self.sb.run(["bash", str(self.script), *args], D=str(self.sb.data), **extra)

    def _execution(self) -> dict:
        return json.loads((self.retire_root / "cleanup" / "cleanup-execution.json").read_text(encoding="utf-8"))

    def _executed_receipt(self) -> dict:
        path = self.camp / "control" / "vc" / "receipts" / "cand-x" / "cleanup-decision-executed.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_check_passes_when_running_final_image(self) -> None:
        self._docker(running=self.FINAL)
        result = self._run("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["running_image_kind"], "final")

    def test_check_passes_after_release_image_swap_when_release_image_declared(self) -> None:
        """0.164 的真实顺序：先发版、生产换成 ghcr 发布镜像，之后才清理。预检唯一的问题曾是“生产镜像已换成发布镜像”。"""

        self._docker(running=self.RELEASE)
        result = self._run("--check", release=self.RELEASE_REF)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["running_image_kind"], "release")
        self.assertEqual(report["running_image_ref"], self.RELEASE_REF)

    def test_check_refuses_unknown_running_image_without_declaration(self) -> None:
        """没声明发布镜像时，运行镜像不是最终镜像就拒绝（不猜）。"""

        self._docker(running=self.RELEASE)
        result = self._run("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("既不是 VC-6 最终镜像", result.stdout)

    def test_check_refuses_when_declared_release_image_is_not_what_is_running(self) -> None:
        self._docker(running="sha256:" + "99" * 32)
        result = self._run("--check", release=self.RELEASE_REF)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("既不是 VC-6 最终镜像", result.stdout)

    def test_check_refuses_when_release_image_is_in_delete_list(self) -> None:
        self.manifest["candidate_images"].append({"reference": "ghcr-copy", "image_id": self.RELEASE, "size": "1MB"})
        (self.retire_root / "cleanup" / "cleanup-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self._docker(running=self.RELEASE, images={"ghcr-copy": self.RELEASE})
        result = self._run("--check", release=self.RELEASE_REF)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("受保护镜像", result.stdout)

    def test_execute_in_original_order_writes_final_image_receipt(self) -> None:
        self._docker(running=self.FINAL)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        execution = self._execution()
        self.assertEqual(execution["post_checks"]["running_image_kind"], "final")
        reason = self._executed_receipt()["facts"]["assertions"]["reason"]
        self.assertIn("运行镜像仍是退休后最终镜像", reason)
        self.assertNotIn("发布镜像", reason)
        self.assertFalse(self.junk.exists(), "清单内目录应已删除")

    def test_still_running_final_image_never_records_a_release_image_ref(self) -> None:
        """容器仍跑最终镜像、但环境里设了 VC6_RELEASE_IMAGE（先清理后发版时提前声明）：执行证据 running_image_ref 必须为空。"""

        self._docker(running=self.FINAL)
        check = self._run("--check", release=self.RELEASE_REF)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        report = json.loads(check.stdout.splitlines()[0])
        self.assertEqual(report["running_image_kind"], "final")
        self.assertEqual(report["running_image_ref"], "", "预检报告同样不得带发布镜像引用")
        result = self._run(release=self.RELEASE_REF)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        post = self._execution()["post_checks"]
        self.assertEqual(post["running_image_kind"], "final")
        self.assertEqual(post["running_image_ref"], "")
        self.assertEqual(post["production_image_id"], self.FINAL)
        reason = self._executed_receipt()["facts"]["assertions"]["reason"]
        self.assertIn("运行镜像仍是退休后最终镜像", reason)
        self.assertNotIn(self.RELEASE_REF, reason)

    def test_execute_after_release_swap_writes_truthful_receipt(self) -> None:
        """先发版后清理：脚本放行，生成“已执行”收据，收据如实写明运行的是发布镜像（引用与 ID），而不是“仍是退休后最终镜像”。"""

        self._docker(running=self.RELEASE)
        result = self._run(release=self.RELEASE_REF)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        execution = self._execution()
        post = execution["post_checks"]
        self.assertEqual(post["running_image_kind"], "release")
        self.assertEqual(post["running_image_ref"], self.RELEASE_REF)
        self.assertEqual(post["production_image_id"], self.RELEASE)
        self.assertEqual(post["production_image_expected"], self.FINAL, "最终镜像仍作为期望值记录，便于与现实对照")
        reason = self._executed_receipt()["facts"]["assertions"]["reason"]
        self.assertIn("发布镜像", reason)
        self.assertIn(self.RELEASE_REF, reason)
        self.assertIn(self.RELEASE, reason)
        self.assertNotIn("运行镜像仍是退休后最终镜像", reason, "收据不得写出与现实不符的“仍是最终镜像”")
        self.assertFalse(self.junk.exists())
        removed = json.loads(self.sb.docker_state.read_text())["removed"]
        self.assertIn("cand-img:1", removed, "候选镜像应被删除")
        self.assertNotIn(self.RELEASE_REF, removed, "发布镜像绝不能被删")
        self.assertEqual(execution["post_checks"]["rollback_image_present"], True)

    def test_second_execution_is_refused(self) -> None:
        self._docker(running=self.FINAL)
        self.assertEqual(self._run().returncode, 0)
        again = self._run()
        self.assertEqual(again.returncode, 2)
        self.assertIn("拒绝重复执行", again.stdout)

    def test_missing_approval_is_refused(self) -> None:
        self._docker(running=self.FINAL)
        result = self.sb.run(["bash", str(self.script), "--check"], D=str(self.sb.data), VC6_ARCHIVE_DIR=str(self.archive_dir))
        self.assertNotEqual(result.returncode, 0)


# ======================================================================================================
# 四、静态合同：构建收据路径、bundle 取分支引用、不用 nohup、不写死版本
# ======================================================================================================
def _code_lines(path: Path):
    """去掉整行注释后的代码行（含行号）。"""

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield number, line


class StaticContractTests(unittest.TestCase):
    def test_vc4_build_receipt_is_checked_under_campaign_candidate_dir(self) -> None:
        """VC-4 的构建收据由 Campaign 写在 $NEWDIR/candidates/$CAND/ 下，不在构建目录 $B；按 $B 核对曾让流水线误停。"""

        code = "\n".join(line for _, line in _code_lines(PIPELINE / "vc-pipeline.sh"))
        self.assertIn('"${NEWDIR}/candidates/${CAND}/build-receipt.json"', code)
        self.assertNotRegex(code, r'\$\{?B\}?/build-receipt\.json')

    def test_bundles_are_fetched_by_branch_ref_never_head(self) -> None:
        """git bundle create "<基点>..<分支>" 的 bundle 里只有分支引用、没有 HEAD，`git fetch <bundle> HEAD` 会报 couldn't find remote ref HEAD。"""

        offenders = []
        for path in sorted(PIPELINE.rglob("*.sh")):
            for number, line in _code_lines(path):
                if re.search(r"\bfetch\b.*(bundle).*\bHEAD\b", line) and "refs/heads" not in line:
                    offenders.append(f"{path.relative_to(PIPELINE)}:{number}: {line.strip()[:120]}")
        self.assertEqual(offenders, [])
        for rel in ("vc6/vc6-build.sh", "vc6/vc6-retire-build.sh"):
            text = (PIPELINE / rel).read_text(encoding="utf-8")
            self.assertIn('refs/heads/${BUNDLE_BRANCH}:refs/heads/', text, rel)
        self.assertIn("refs/heads/${BRANCH}:refs/heads/pc-check", (PIPELINE / "local/vc6-local-commit.sh").read_text(encoding="utf-8"))

    def test_account_off_call_sits_after_vc5_all_done_loop(self) -> None:
        """静态版的顺序守卫：run_VC5 里 capture_off 调用必须出现在“grep VC5_ALL_DONE 超时判断”之后。"""

        text = (PIPELINE / "vc-pipeline.sh").read_text(encoding="utf-8")
        body = text[text.index("run_VC5() {"):text.index("run_VC6() {")]
        done_check = body.index("vc5-all 超时（20 小时）未结束")
        calls = [m.start() for m in re.finditer(r"^\s*capture_off\b", body, re.M)]
        self.assertTrue(calls)
        self.assertTrue(all(position > done_check for position in calls), "capture_off 不得出现在监视循环里")

    def test_no_nohup_in_executable_lines(self) -> None:
        for path in sorted(PIPELINE.rglob("*.sh")):
            for number, line in _code_lines(path):
                if re.search(r"(^|[;&|(]\s*|setsid -f\s+|^\s*)nohup\s", line):
                    self.fail(f"{path.relative_to(PIPELINE)}:{number} 使用了 nohup：{line.strip()[:100]}")

    def test_pipeline_has_no_hardcoded_round_literals(self) -> None:
        patterns = [r"/root/vc-rounds/[A-Za-z0-9_-]+-r[0-9]", r"/root/(?!pipeline-prep)[A-Za-z0-9-]*-prep\b", r"reg0\d+/", r"\bmaint-history-\d+"]
        for path in sorted(PIPELINE.rglob("*")):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for pattern in patterns:
                    with self.subTest(path=str(path.relative_to(PIPELINE)), pattern=pattern):
                        self.assertIsNone(re.search(pattern, text))

    def test_all_shell_scripts_parse_and_python_compiles(self) -> None:
        for path in sorted(PIPELINE.rglob("*.sh")):
            with self.subTest(path=str(path.relative_to(PIPELINE))):
                self.assertEqual(subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode, 0)
                if path.name != "pipeline-lib.sh":
                    self.assertTrue(path.stat().st_mode & stat.S_IXUSR, "脚本应带可执行位")
        for path in sorted(PIPELINE.rglob("*.py")):
            with self.subTest(path=str(path.relative_to(PIPELINE))):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_vc2_seq_scripts_match_generator_output(self) -> None:
        """vc23-seq.sh／vc2-approve-and-stage-seq.sh 是用生成器从驱动自带的 vc23.sh／vc2-approve-and-stage.sh 机械改写出来的：
        驱动那两个脚本一变、已入库的 -seq 版没跟着重生成，这条就红（改驱动忘了重生成会让 VC-2 批次序号错位）。"""

        with tempfile.TemporaryDirectory() as out:
            done = subprocess.run([sys.executable, "-B", str(PIPELINE / "vc2" / "gen_vc2_seq_scripts.py"), str(REAL_DRIVER), out],
                                  capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(done.returncode, 0, done.stderr)
            for name in ("vc23-seq.sh", "vc2-approve-and-stage-seq.sh"):
                self.assertEqual((Path(out) / name).read_text(encoding="utf-8"), (PIPELINE / "vc2" / name).read_text(encoding="utf-8"), name)


# ======================================================================================================
# 五、轮次参数模板、真实 parse_env、vc6-env 占位断言
# ======================================================================================================
class RoundEnvTemplateTests(unittest.TestCase):
    OVERRIDES = {
        "PREV_ROUND": None,
    }

    def _render(self, out: Path, extra: list[str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(PIPELINE / "render_round_env.py"), "--target", "0.165.0", "--baseline", "0.164.0",
                               "--round", "r1", "--stamp", "20990101t000000z", "--out", str(out), *(extra or [])],
                              capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_render_derives_names_from_versions_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "env.sh"
            done = self._render(out)
            self.assertEqual(done.returncode, 0, done.stderr)
            text = out.read_text(encoding="utf-8")
            self.assertIn("TARGET_VERSION=0.165.0", text)
            self.assertIn("BASELINE_VERSION=0.164.0", text)
            self.assertIn("RUNROOT=/root/vc-rounds/c01650-$ROUND", text)
            self.assertIn("UP=codex-0164-to-01650-$ROUND-$STAMP", text)
            self.assertIn("CAND=c01650-candidate-$ROUND", text)
            self.assertIn("TARGET_PROFILE_ID=codex-0.165.0-official-r1650-r1", text)
            self.assertIn("PREV_CANDIDATE=$D/candidates/c01640-candidate-REPLACE_PREVIOUS_ROUND", text)
            self.assertNotIn("@", text.replace("@sha256", ""), "不应残留未渲染的占位")
            self.assertIn("待填键", done.stderr)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_unfilled_template_is_rejected_by_real_parse_env(self) -> None:
        """模板里 REPLACE_ 开头的值必须由人填：没填就交给真 parse_env.py，被拒绝，提醒还没填。"""

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "env.sh"
            self.assertEqual(self._render(out).returncode, 0)
            done = subprocess.run([sys.executable, "-B", str(REAL_DRIVER / "parse_env.py"), str(out)], capture_output=True, text=True,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertNotEqual(done.returncode, 0)

    def test_set_only_replaces_replace_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "env.sh"
            bad = self._render(out, ["--set", "TARGET_VERSION=1.0.0"])
            self.assertNotEqual(bad.returncode, 0, "只许替换 REPLACE_ 开头的值，不许改已渲染好的键")
            good = self._render(out, ["--set", "MAIN_MODEL=model-a"])
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn("MAIN_MODEL=model-a", out.read_text(encoding="utf-8"))
            self.assertNotIn("MAIN_MODEL", good.stderr.split("待填键", 1)[1])

    def test_pipeline_env_example_only_uses_allowed_keys(self) -> None:
        allowed = set(re.search(r'PL_ALLOWED_KEYS="([^"]*)"', (PIPELINE / "pipeline-lib.sh").read_text(encoding="utf-8")).group(1).split())
        keys = set()
        for line in (PIPELINE / "pipeline.env.example").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line)
            if match:
                keys.add(match.group(1))
        self.assertTrue(keys)
        self.assertEqual(keys - allowed, set(), "pipeline.env.example 里出现了库不认的键")
        for key in ("ARM64_VC_ENV", "PIPE_RETRY_MAX", "PIPE_RETRY_VC4_GATES", "PIPE_SSH_HOST"):
            self.assertIn(key, keys)


class PlRemoteVarTests(unittest.TestCase):
    """pl_remote_var 要同时认 parse_env.py 的两种输出形态。

    parse_env.py 用 shlex.quote 输出：值只含安全字符时不带引号（export RUNROOT=/root/x），含空格等字符才带单引号。
    首次部署时曾只认带引号的形态，local/ 下所有本机脚本都因“取不到 RUNROOT”起不来；流水线测试的 ssh 桩恰好返回带引号形态，没抓到。
    这里用真实 parse_env.py 的输出当输入。
    """

    FILL = {
        "API_KEY_ID": "1", "CAPTURE_RUNTIME_IMAGE": "img", "CODEX_ACCOUNT_ID": "2", "CODEX_BIN_SHA256": "a" * 64,
        "COMPOSE_BACKUP": "/root/x", "ENTRY_BRANCH": "b", "FRONTEND_DEVIATION_APPROVED_BY": "boss", "HISTORY_TEST_TREE": "/root/h",
        "JWTGEN_BIN": "/root/j", "KILO_BIN": "/root/k", "KILO_SHA256": "a" * 64, "KILO_VERSION": "1.0.0", "LITE_MODEL": "m1",
        "MAIN_MODEL": "m2", "OFFICIAL_ASSET_SHA256": "a" * 64, "OFFICIAL_STOP_LEDGER": "/root/l", "OFFICIAL_STOP_RECEIPT": "/root/r",
        "POLICY_ACTIVATION": "/root/p", "POLICY_COMPAT_RECEIPT": "/root/c", "PREDECESSOR_CAMPAIGN": "pc", "PREVIOUS_POLICY": "/root/pp",
        "PREV_CANDIDATE": "/root/pcand", "PRODUCTION_IMAGE": "pi", "PROJECT_DEADLINE_UTC": "2099-01-01T00:00:00Z",
        "RELEASE_CERTIFICATION": "/root/rc", "RETIRE_VERSION": "0.163.0", "TARGET_CODE_MODE_HOST_SHA256": "a" * 64,
    }

    def _remote_var(self, remote_vars: str, key: str) -> str:
        """在一个干净的 HOME 下 source 真实 pipeline-lib.sh，对给定的 REMOTE_VARS 调 pl_remote_var。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = _write(root / "call.sh", f"""
                #!/bin/bash
                source "{PIPELINE}/pipeline-lib.sh"
                REMOTE_VARS=$(cat "{root}/vars.txt")
                pl_remote_var "{key}"
            """)
            (root / "vars.txt").write_text(remote_vars, encoding="utf-8")
            done = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, cwd=root,
                                  env={"PATH": os.environ["PATH"], "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

    def test_reads_unquoted_value_from_real_parse_env_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "env.sh"
            sets = [item for key, value in self.FILL.items() for item in ("--set", f"{key}={value}")]
            rendered = subprocess.run([sys.executable, "-B", str(PIPELINE / "render_round_env.py"), "--target", "0.165.0", "--baseline",
                                       "0.164.0", "--round", "r1", "--stamp", "20990101t000000z", "--out", str(env), *sets],
                                      capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            parsed = subprocess.run([sys.executable, "-B", str(REAL_DRIVER / "parse_env.py"), str(env)], capture_output=True, text=True,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            self.assertIn("export RUNROOT=/root/vc-rounds/c01650-r1\n", parsed.stdout, "真实输出里安全字符的值本来就不带引号")
            self.assertEqual(self._remote_var(parsed.stdout, "RUNROOT"), "/root/vc-rounds/c01650-r1")
            self.assertEqual(self._remote_var(parsed.stdout, "TARGET_VERSION"), "0.165.0")

    def test_still_reads_quoted_value(self) -> None:
        remote_vars = "export RUNROOT='/root/vc rounds/c01650-r1'\nexport ROUND=r1\n"
        self.assertEqual(self._remote_var(remote_vars, "RUNROOT"), "/root/vc rounds/c01650-r1")
        self.assertEqual(self._remote_var(remote_vars, "ROUND"), "r1")

    def test_key_match_is_exact_and_missing_key_is_empty(self) -> None:
        remote_vars = "export RUNROOT_OLD=/root/old\nexport RUNROOT=/root/new\n"
        self.assertEqual(self._remote_var(remote_vars, "RUNROOT"), "/root/new", "前缀相同的键不能串")
        self.assertEqual(self._remote_var(remote_vars, "NO_SUCH_KEY"), "")


class Vc6EnvTests(SandboxCase):
    def test_vc6_env_refuses_placeholder_values(self) -> None:
        """vc6-env.sh 在 VC-3 之后才有真值的键（画像摘要、候选链 C）仍是占位时拒绝，不带着占位值往下跑。"""

        sb = self.sb
        env = dict(sb.env, PROFILE_DIGEST=ZERO_DIGEST, P0_ROLLBACK_EVIDENCE=str(sb.root / "none.json"), CAMPAIGN_PREFIX="cx")
        sb.env_file.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8")
        wrapper = _write(sb.root / "source_vc6_env.sh", f"""
            #!/bin/bash
            source "{sb.pipe}/vc6/vc6-env.sh"
            echo SOURCED
        """)
        result = sb.run(["bash", str(wrapper)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("占位", result.stdout)


# ======================================================================================================
# 六、采集账号包装脚本、本机侧脚本
# ======================================================================================================
class CaptureAdminTests(SandboxCase):
    def test_admin_script_refuses_without_account_id(self) -> None:
        result = self.sb.run([sys.executable, "-B", str(self.sb.pipe / "capture_admin.py"), "account", "on"], PREP=str(self.sb.prep))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CAPTURE_ACCOUNT_ID", result.stdout + result.stderr)

    def test_admin_script_has_no_account_literals(self) -> None:
        text = (PIPELINE / "capture_admin.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"ACCOUNT_ID\s*=\s*\d+")
        self.assertNotRegex(text, r"/accounts/\d+")

    def test_wrapper_passes_account_id_from_round_env(self) -> None:
        """包装脚本把本轮参数文件的采集账号补给 capture_admin.py：这里用桩 capture_admin.py 记录收到的环境。"""

        _write(self.sb.pipe / "capture_admin.py", f"""
            #!/usr/bin/env python3
            import os, sys
            open("{self.root}/admin.out", "w").write(" ".join([os.environ.get("CAPTURE_ACCOUNT_ID", ""), os.environ.get("PREP", ""), *sys.argv[1:]]))
        """)
        result = self.sb.run(["bash", str(self.sb.pipe / "capture-account-off.sh")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "admin.out").read_text(encoding="utf-8"), f"7 {self.sb.prep} account off")


class LocalScriptTests(SandboxCase):
    def setUp(self) -> None:
        super().setUp()
        exports = "\n".join(f"export {k}='{v}'" for k, v in {"RUNROOT": "/remote/rounds/roundA", "BUNDLE": "/remote/data/staging/roundA.bundle",
                                                             "BUNDLE_BRANCH": "feature/x", "HISTORY_TEST_TREE": "/remote/hist"}.items())
        (self.root / "remote_exports.txt").write_text(exports + "\n", encoding="utf-8")

    def test_vc4_handoff_takes_branch_and_bundle_from_remote_round_env(self) -> None:
        """交接文件里的分支必须等于采集主机参数文件的 BUNDLE_BRANCH（流水线 VC4 阶段会核对）；不能在本机脚本里写死。"""

        bundle = self.root / "local.bundle"
        bundle.write_bytes(b"bundle-bytes")
        result = self.sb.run(["bash", str(self.sb.pipe / "local" / "vc4-handoff.sh"), str(bundle), "4" * 40], PIPE_SSH_HOST="hostx")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = "\n".join(self.sb.events())
        self.assertIn("SCP -q " + str(bundle) + " hostx:/remote/data/staging/roundA.bundle.part", events)
        handoff = [line for line in events.splitlines() if "vc4-chain.json" in line and "printf" in line]
        self.assertTrue(handoff, events)
        self.assertIn("feature/x", handoff[0])
        self.assertIn("4" * 40, handoff[0])
        self.assertIn("/remote/rounds/roundA/handoff", handoff[0])

    def test_vc4_handoff_rejects_bad_dc(self) -> None:
        bundle = self.root / "local.bundle"
        bundle.write_bytes(b"x")
        result = self.sb.run(["bash", str(self.sb.pipe / "local" / "vc4-handoff.sh"), str(bundle), "short"])
        self.assertEqual(result.returncode, 2)

    def test_upload_entry_bundle_dry_run_prints_set_env_command_without_upload(self) -> None:
        repo = self.root / "repo"
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True)
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "f").write_text("x")
        git("add", "f")
        git("commit", "-q", "-m", "init")
        git("branch", "-M", "integ")
        commit = git("rev-parse", "HEAD").stdout.strip()
        result = self.sb.run(["bash", str(self.sb.pipe / "local" / "upload-entry-bundle.sh"), "integ", "--dry-run"], PIPE_LOCAL_REPO=str(repo))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ENTRY_BUNDLE=/remote/rounds/roundA/roundA-deploy-{commit[:9]}.bundle", result.stdout)
        self.assertIn("ENTRY_BRANCH=integ", result.stdout)
        self.assertIn(f"ENTRY_COMMIT={commit}", result.stdout)
        self.assertFalse([e for e in self.sb.events() if e.startswith("SCP")], "--dry-run 不得上传")

    def test_evo_local_requires_base_and_branch_tip(self) -> None:
        result = self.sb.run(["bash", str(self.sb.pipe / "local" / "evo-local.sh"), "5" * 40], PIPE_LOCAL_BRANCH="integ", PIPE_LOCAL_REPO=str(self.root))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--base", result.stdout)


# ======================================================================================================
# 七、整链：VC4 阶段的 bundle 推导与构建收据路径、others_running 不把只读 preflight 当编排
# ======================================================================================================
class PipelineStageTests(SandboxCase):
    def test_check_mode_is_read_only_and_reports(self) -> None:
        result = self.sb.run(["bash", str(self.sb.pipe / "vc-pipeline.sh"), "--check", "--from", "VC6", "--to", "VC6"])
        self.assertIn("== 阶段 VC6", result.stdout)
        self.assertFalse((self.sb.prep / "pipeline" / "vc-status.txt").exists(), "--check 不得写状态文件")

    def test_background_validation_preflight_is_not_counted_as_other_orchestration(self) -> None:
        """上一版第一次停顿：操作员的 --check 还在后台跑 background_validation.py preflight，被流水线的 others_running 当成别的编排而停。
        preflight 是只读检查，不该算；start／run 才算。"""

        sb = self.sb
        decoy_dir = sb.root / "decoys"
        _write(decoy_dir / "background_validation.py", "#!/bin/bash\n/bin/sleep 20\n")
        sb.runroot.mkdir(parents=True, exist_ok=True)
        sb.checkpoint(4)
        text = (sb.pipe / "vc-pipeline.sh").read_text(encoding="utf-8")
        pattern_line = next(line for line in text.splitlines() if "[e]ntry_orchestrator.py" in line and "pgrep" in line)
        self.assertIn("[b]ackground_validation.py (start|run|reuse)", pattern_line)
        for sub, expect_stop in (("preflight", False), ("start", True)):
            with self.subTest(sub=sub):
                proc = subprocess.Popen(["bash", str(decoy_dir / "background_validation.py"), sub], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    shutil.rmtree(sb.prep, ignore_errors=True)
                    (sb.prep / "pipeline").mkdir(parents=True)
                    result = sb.stage_pipeline("--from", "BGV", "--to", "BGV")
                    stopped = "机器上有别的编排在跑" in "\n".join(sb.status_lines())
                    self.assertEqual(stopped, expect_stop, result.stdout[-600:])
                finally:
                    proc.kill()
                    proc.wait()


# ======================================================================================================
# 八、VC4 阶段：从 bundle 推出候选链、构建收据按 Campaign 目录核对、两个耗时门禁步骤默认不重试
# ======================================================================================================
class Vc4StageTests(SandboxCase):
    def setUp(self) -> None:
        super().setUp()
        sb = self.sb
        repo = sb.root / "hist"
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                                        check=True, capture_output=True, text=True)
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a").write_text("a")
        git("add", "a"); git("commit", "-q", "-m", "base")
        (repo / "b").write_text("b")
        git("add", "b"); git("commit", "-q", "-m", "C")
        self.c = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "-b", "feature/x")
        receipt = repo / "docs" / "egress" / "maintenance" / "freeze-successor-test.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{}")
        git("add", "."); git("commit", "-q", "-m", "DC")
        self.dc = git("rev-parse", "HEAD").stdout.strip()
        bundle = sb.root / "bundle.bundle"
        git("bundle", "create", str(bundle), f"{self.c}..feature/x")
        digest = subprocess.run(["shasum", "-a", "256", str(bundle)], capture_output=True, text=True).stdout.split()[0]
        (sb.runroot / "handoff").mkdir(parents=True, exist_ok=True)
        (sb.runroot / "handoff" / "vc4-chain.json").write_text(json.dumps({"bundle_sha256": digest, "branch": "feature/x", "dc": self.dc}), encoding="utf-8")
        approval = sb.root / "network-approval.json"
        approval.write_text("{}", encoding="utf-8")
        sb.set_env(VC4_BUILD_NETWORK="host", VC4_BUILD_NETWORK_APPROVAL=str(approval))
        sb.checkpoint(3)
        stage_dir = sb.data / "control" / "cx-vc3-candidate-catalog-roundA-20990101t000000z"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "catalog-stage-receipt.json").write_text("{}", encoding="utf-8")
        _write(sb.drv / "vc4_contract.py", "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        self.gate_calls = sb.counters / "gates"
        self.gate_calls.write_text("0")

    def _stubs(self, *, gate_fail_first: int, receipt_location: str) -> None:
        sb = self.sb
        _write(sb.drv / "arm64-vc4-gates.sh", f"""
            #!/bin/bash
            n=$(cat "{self.gate_calls}"); echo $((n + 1)) > "{self.gate_calls}"
            if [ "$n" -lt {gate_fail_first} ]; then echo "ARM64_VC4_GATES_FAILED"; exit 1; fi
            echo "ARM64_VC4_GATES_DONE rc=0"
        """)
        target = sb.newdir / "candidates" / "cand-x" if receipt_location == "campaign" else sb.data / "candidates" / "cand-x"
        _write(sb.drv / "vc4-all.sh", f"""
            #!/bin/bash
            mkdir -p "{target}" "{sb.newdir}/control/vc"
            echo '{{}}' > "{target}/build-receipt.json"
            echo '{{}}' > "{sb.newdir}/control/vc/vc-4-checkpoint.json"
            echo "VC4_ALL_DONE"
        """)

    def _run(self, **extra: str) -> subprocess.CompletedProcess:
        return self.sb.stage_pipeline("--from", "VC4", "--to", "VC4", **extra)

    def test_vc4_passes_when_build_receipt_is_under_campaign_candidate_dir(self) -> None:
        self._stubs(gate_fail_first=0, receipt_location="campaign")
        result = self._run()
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(result.returncode, 0, text[-1500:])
        self.assertIn("PASS VC4", text)
        self.assertIn(f"C={self.c[:9]} DC={self.dc[:9]} RECEIPT=docs/egress/maintenance/freeze-successor-test.json", text)
        env_text = self.sb.env_file.read_text(encoding="utf-8")
        self.assertIn(f"C={self.c}\n", env_text)
        self.assertIn(f"DC={self.dc}\n", env_text)

    def test_vc4_stops_when_build_receipt_only_exists_in_build_dir(self) -> None:
        """上一版误停：按构建目录 $B 找构建收据。收据其实写在 Campaign 的 candidates/<CAND>/ 下；
        只在 $B 下有收据（而 Campaign 下没有）时，流水线必须认为 VC-4 没完成。"""

        self._stubs(gate_fail_first=0, receipt_location="builddir")
        result = self._run()
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺 vc-4-checkpoint 或 build-receipt.json", text)

    def test_vc4_gates_failure_is_not_retried_by_default(self) -> None:
        self._stubs(gate_fail_first=1, receipt_location="campaign")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(int(self.gate_calls.read_text()), 1, "耗时门禁默认失败一次就停，不自动补跑")
        self.assertIn("STOPPED VC4", "\n".join(self.sb.status_lines()))

    def test_vc4_gates_retried_once_when_explicitly_enabled(self) -> None:
        self._stubs(gate_fail_first=1, receipt_location="campaign")
        result = self._run(PIPE_RETRY_VC4_GATES="1")
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(result.returncode, 0, text[-1500:])
        self.assertEqual(int(self.gate_calls.read_text()), 2)
        self.assertIn("RETRY arm64-vc4-gates", text)


# ======================================================================================================
# 九、官方采集登录态令牌：剩余有效期检查与直连刷新（preflight-refresh.sh），以及 VC1 / 起跑前的自动检查
# ======================================================================================================
def _jwt(exp: float) -> str:
    import base64
    seg = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode().rstrip("=")
    return "h." + seg + ".s"


class PreflightRefreshTests(SandboxCase):
    def setUp(self) -> None:
        super().setUp()
        sb = self.sb
        self.auth = sb.data / "state" / "codex" / "auth.json"
        self.auth.parent.mkdir(parents=True)
        # 刷新驱动脚本只需存在；真正的驱动在容器里跑，这里由 docker 桩模拟
        driver = sb.data / "tools" / "official_client_capture" / "drive_codex_auth_refresh.py"
        _write(driver, "# placeholder\n", 0o644)
        sb.set_env(CLIENT_BIN="/opt/codex-1.1.0/bin/codex")

    def _auth(self, minutes: float) -> None:
        import time
        self.auth.write_text(json.dumps({"last_refresh": "x", "tokens": {"access_token": _jwt(time.time() + minutes * 60), "refresh_token": "r" * 20}}), encoding="utf-8")

    def _docker(self, mode: str = "ok") -> None:
        self.sb.set_docker(refresh=mode, auth_path=str(self.auth))

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return self.sb.run(["bash", str(self.sb.pipe / "preflight-refresh.sh"), *args])

    def test_remaining_prints_minutes_and_negative_when_expired(self) -> None:
        self._auth(100)
        self.assertIn(int(self._run("--remaining").stdout.strip()), (98, 99, 100))
        self._auth(-30)
        self.assertLess(int(self._run("--remaining").stdout.strip()), 0)

    def test_remaining_fails_clearly_when_token_unreadable(self) -> None:
        self.auth.write_text("not json", encoding="utf-8")
        result = self._run("--remaining")
        self.assertEqual(result.returncode, 4)

    def test_ensure_does_nothing_when_enough_time_left(self) -> None:
        self._auth(600)
        self._docker()
        result = self._run("--ensure", "--min", "150")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TOKEN_OK", result.stdout)
        self.assertFalse([e for e in self.sb.events() if e.startswith("DOCKER exec")], "剩余足够时不得动 capture-cli")

    def test_ensure_refreshes_directly_when_short_and_rechecks(self) -> None:
        self._auth(20)
        self._docker("ok")
        result = self._run("--ensure", "--min", "150")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [e for e in self.sb.events() if e.startswith("DOCKER exec -i capture-cli python3 -")]
        self.assertEqual(len(calls), 1)
        self.assertIn("--codex-bin /opt/codex-1.1.0/bin/codex --codex-version 1.1.0 --disable plugins --disable apps", calls[0])
        self.assertIn("TOKEN_OK 刷新后", result.stdout)
        self.assertGreater(int(self._run("--remaining").stdout.strip()), 1000)
        self.assertNotIn("A" * 40, result.stdout + result.stderr, "令牌形态必须在输出里截断")
        self.assertTrue(list((self.sb.runroot).glob("preflight-refresh-*/before.txt")))

    def test_ensure_fails_clearly_when_refresh_is_rejected(self) -> None:
        self._auth(20)
        self._docker("unchanged")
        result = self._run("--ensure", "--min", "150")
        self.assertEqual(result.returncode, 3)
        self.assertIn("直连刷新失败 rc=3", result.stderr)

    def test_ensure_fails_when_still_short_after_refresh(self) -> None:
        self._auth(20)
        self._docker("ok")
        result = self._run("--ensure", "--min", str(60 * 24 * 30))
        self.assertEqual(result.returncode, 5)
        self.assertIn("仍只剩", result.stderr)

    def test_ensure_uses_default_threshold_from_pipeline_env_key(self) -> None:
        self._auth(120)
        self._docker("ok")
        result = self.sb.run(["bash", str(self.sb.pipe / "preflight-refresh.sh"), "--ensure"], PIPE_TOKEN_MIN_MINUTES="60")
        self.assertEqual(result.returncode, 0)
        self.assertFalse([e for e in self.sb.events() if e.startswith("DOCKER exec")])
        result = self.sb.run(["bash", str(self.sb.pipe / "preflight-refresh.sh"), "--ensure"], PIPE_TOKEN_MIN_MINUTES="300")
        self.assertTrue([e for e in self.sb.events() if e.startswith("DOCKER exec")], "阈值调高后应触发刷新")


class Vc1TokenGuardTests(SandboxCase):
    """VC1 阶段自带的第二道检查：令牌不足且首批没开始 → 先直连刷新；刷新失败 → 明确报错停下。"""

    def _prepare(self, remaining: str, refresh_rc: int) -> Path:
        sb = self.sb
        sb.runroot.mkdir(parents=True, exist_ok=True)
        (sb.runroot / "entry-steps").mkdir(exist_ok=True)
        (sb.runroot / "entry-steps" / "vc0-closeout.json").write_text("{}", encoding="utf-8")
        sb.checkpoint(1)   # 首批已产出 checkpoint，VC1 阶段不必再等
        calls = self.root / "refresh_calls"
        _write(sb.pipe / "preflight-refresh.sh", f"""
            #!/bin/bash
            echo "$*" >> "{calls}"
            case "$1" in
              --remaining) echo {remaining}; exit 0 ;;
              --ensure) exit {refresh_rc} ;;
            esac
        """)
        return calls

    def test_short_token_triggers_direct_refresh_before_waiting(self) -> None:
        calls = self._prepare("20", 0)
        result = self.sb.stage_pipeline("--from", "VC1", "--to", "VC1")
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(result.returncode, 0, text[-1200:])
        self.assertEqual(calls.read_text().split("\n")[:2], ["--remaining", "--ensure --min 150"])
        self.assertIn("先直连刷新一次", text)
        self.assertIn("PASS VC1", text)

    def test_enough_token_skips_refresh(self) -> None:
        calls = self._prepare("900", 0)
        result = self.sb.stage_pipeline("--from", "VC1", "--to", "VC1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.read_text().strip(), "--remaining")

    def test_refresh_failure_stops_with_clear_message(self) -> None:
        self._prepare("20", 3)
        result = self.sb.stage_pipeline("--from", "VC1", "--to", "VC1")
        text = "\n".join(self.sb.status_lines())
        self.assertEqual(result.returncode, 1)
        self.assertIn("STOPPED VC1 :: 直连刷新官方采集登录态失败", text)
        self.assertNotIn("PASS VC1", text)

    def test_threshold_comes_from_pipeline_env_key(self) -> None:
        calls = self._prepare("20", 0)
        result = self.sb.stage_pipeline("--from", "VC1", "--to", "VC1", PIPE_TOKEN_MIN_MINUTES="10")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.read_text().strip(), "--remaining", "剩余 20 ≥ 阈值 10，不应刷新")


if __name__ == "__main__":
    unittest.main()
