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

import contextlib
import hashlib
import io
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
import unittest.mock
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
        # 假 entry-dryrun.sh：记录调用；disk-watch 回放一份可回收清单（真实 disk-watch 另有 test_ci_entry_housekeeping 覆盖）。
        _write(d / "entry-dryrun.sh", f"""
            #!/bin/bash
            echo "HOUSEKEEPING $*" >> "{self.log}"
            if [ "$1" = disk-watch ]; then
              echo "根盘水位：已用 66%（停线 69%），可用 47 GiB（下限 40 GiB）；已到预警线"
              echo "可回收清单（按 dry-run 估算，未删除任何东西）："
              echo "  - Go 编译缓存旧条目（6 小时以上没用过）：120 项，约 900 MiB（housekeeping 自动清）"
              echo "  - 部署回滚备份与文档事务目录：40 项，约 1500 MiB（housekeeping 自动清）"
              [ -f "{self.root}/disk_watch.rc" ] && exit "$(cat "{self.root}/disk_watch.rc")"
            fi
            exit 0
        """)
        # 假 vc5-precheck.sh：--consume 恒通过（＝准入收据已存在）；--dry-run 回放 precheck_dryrun.json（缺省为库表前置全绿的 ready 报告），
        # 退出码取 precheck_dryrun.rc（缺省 0）。写 precheck_consume.rc 可让 --consume 失败（＝收据不存在）。
        _write(d / "vc5-precheck.sh", f"""
            #!/bin/bash
            echo "PRECHECK $*" >> "{self.log}"
            case "$1" in
              --consume) [ -f "{self.root}/precheck_consume.rc" ] && exit "$(cat "{self.root}/precheck_consume.rc")" ;;
              --dry-run)
                if [ -f "{self.root}/precheck_dryrun.json" ]; then cat "{self.root}/precheck_dryrun.json"
                else echo '{{"status": "ready", "checks": [], "candidate_prerequisites": [{{"check": "model_mapping_empty", "status": "passed"}}]}}'; fi
                echo "人读摘要：库表前置如何改" >&2
                [ -f "{self.root}/precheck_dryrun.rc" ] && exit "$(cat "{self.root}/precheck_dryrun.rc")" ;;
            esac
            exit 0
        """)
        # 假 vc5-all.sh：先报 Kilo 完成，隔一会儿才打 VC5_ALL_DONE（真实流程里 seal／compare／accept 还要跑很久）
        _write(d / "vc5-all.sh", f"""
            #!/bin/bash
            echo "VC5_ALL_START"
            echo "RENEW_APPROVER=${{VC5_RENEW_APPROVER:-}}" >> "{self.log}"
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

    def _renew_approver_seen_by_vc5_all(self, *argv: str, **extra: str) -> str:
        """跑一遍 VC5 阶段，返回假 vc5-all.sh 看到的 VC5_RENEW_APPROVER（没设时为空串）。"""

        self._install_account_stubs()
        self._prepare_vc5()
        result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5", *argv, **extra)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        seen = [line for line in self.sb.events() if line.startswith("RENEW_APPROVER=")]
        self.assertEqual(len(seen), 1, seen)
        return seen[0].split("=", 1)[1]

    def test_approver_is_handed_to_vc5_all_for_token_auto_renewal(self) -> None:
        """0.165 实战：流水线起 vc5-all 时没传 VC5_RENEW_APPROVER，consume 窗口过期后 canonical 前不会自动续签（手工 --renew 才通过）。
        起跑传了 --approver 就必须把同一批准人交给 vc5-all。"""

        self.assertEqual(self._renew_approver_seen_by_vc5_all("--approver", "老板·测试批准依据"), "老板·测试批准依据")

    def test_existing_vc5_renew_approver_environment_wins_over_pipeline_approver(self) -> None:
        self.assertEqual(
            self._renew_approver_seen_by_vc5_all("--approver", "流水线批准人", VC5_RENEW_APPROVER="环境里的续签批准人"),
            "环境里的续签批准人",
        )

    def test_without_any_approver_vc5_all_gets_empty_value_not_inherited_garbage(self) -> None:
        self.assertEqual(self._renew_approver_seen_by_vc5_all(), "")

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

    def _run_paused_vc5_all(self) -> tuple[subprocess.CompletedProcess, Path]:
        """vc5-all 打出 KILO_DONE 与 VC5_ALL_PAUSED 后不再前进（真实场景：Claude 的 seal 暂停等人处置）。"""

        account_log = self._install_account_stubs()
        self._prepare_vc5()
        _write(self.sb.drv / "vc5-all.sh", "#!/bin/bash\necho 'KILO_DONE'\necho 'VC5_ALL_PAUSED seal 暂停等人处置'\nexit 0\n")
        return self.sb.stage_pipeline("--from", "VC5", "--to", "VC5"), account_log

    def test_account_not_closed_when_vc5_all_is_paused(self) -> None:
        """VC5_ALL_PAUSED 分支：流水线 STOPPED 等人处置，不替人关采集账号。
        暂停点可能在 seal 对比点之前，此时关账号会让之后的“Kilo 后环境恢复门禁”拿到不一致的账号状态；
        人处置完继续时账号还要在开着。变异“先关账号再停”原先没有测试能抓到（V1 验收的存活变异）。"""

        result, account_log = self._run_paused_vc5_all()
        self.assertEqual(result.returncode, 1, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertEqual(self._off_lines(account_log), [], account_log.read_text(encoding="utf-8"))
        text = "\n".join(self.sb.status_lines())
        self.assertIn("STOPPED VC5", text)
        self.assertIn("vc5-all 暂停等人处置", text)
        self.assertIn("采集账号仍是开启状态", text, "停下时要提示账号仍开着、需手工处置")

    def test_paused_branch_close_is_caught_by_this_test_mutation(self) -> None:
        """变异验证：把 VC5_ALL_PAUSED 分支改成“先 capture_off 再 stop”，上一条测试的不变量必须被打破。"""

        script = self.sb.pipe / "vc-pipeline.sh"
        text = script.read_text(encoding="utf-8")
        needle = """if grep -q 'VC5_ALL_PAUSED' "${RR}/vc5-all.out"; then stop VC5"""
        self.assertEqual(text.count(needle), 1, "VC5_ALL_PAUSED 分支的写法变了，请同步更新变异点")
        script.write_text(text.replace(needle, """if grep -q 'VC5_ALL_PAUSED' "${RR}/vc5-all.out"; then capture_off; stop VC5"""), encoding="utf-8")
        result, account_log = self._run_paused_vc5_all()
        self.assertEqual(result.returncode, 1, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertTrue(self._off_lines(account_log), "变异版应当在暂停时关了账号")

    # ---- 准入收据已存在时的库表前置复查 ----
    BLOCKED_REPORT = {"status": "blocked", "checks": [], "candidate_prerequisites": [
        {"check": "model_mapping_empty", "status": "blocked", "reason": "model_mapping_not_empty", "detail": "怎么改"},
        {"check": "group_allow_live", "status": "passed"}]}

    def _stage_vc5_with_existing_receipt(self, report: object | None = None, rc: int | None = None) -> subprocess.CompletedProcess:
        self._install_account_stubs()
        self._prepare_vc5()
        if report is not None:
            (self.root / "precheck_dryrun.json").write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
        if rc is not None:
            (self.root / "precheck_dryrun.rc").write_text(str(rc), encoding="utf-8")
        return self.sb.stage_pipeline("--from", "VC5", "--to", "VC5")

    def test_existing_receipt_still_rechecks_db_prerequisites_and_passes_when_ok(self) -> None:
        result = self._stage_vc5_with_existing_receipt()
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        calls = self.sb.log.read_text(encoding="utf-8")
        self.assertIn("PRECHECK --consume", calls)
        self.assertIn("PRECHECK --dry-run", calls, "收据已存在也要跑一次只读 dry-run 复查库表前置")
        self.assertNotIn("PRECHECK --apply", calls)
        self.assertIn("库表前置复查通过", "\n".join(self.sb.status_lines()))

    def test_existing_receipt_but_db_prerequisite_reverted_stops_before_vc5_all(self) -> None:
        """V1 验收建议：准入收据存在时 consume 直接返回，库表被改回去的重启场景拦不住。现在在起 vc5-all 之前停下并指出是哪一项。"""

        result = self._stage_vc5_with_existing_receipt(self.BLOCKED_REPORT, rc=3)
        self.assertEqual(result.returncode, 1, result.stdout[-1500:] + result.stderr[-1500:])
        text = "\n".join(self.sb.status_lines())
        self.assertIn("STOPPED VC5", text)
        self.assertIn("库表前置被改回去了", text)
        self.assertIn("model_mapping_empty", text)
        self.assertIn("vc5-admission-recheck.err", text, "停下的提示要指向人读摘要文件")
        self.assertNotIn("vc5-all 已起", text)
        self.assertNotIn("VC5ALL_FINISHED", self.sb.events())
        account_log = self.root / "account.log"   # 开关桩只在被调用时才写它
        self.assertFalse(account_log.exists() and "ON" in account_log.read_text(encoding="utf-8"), "库表前置不过时不得先打开采集账号")

    def test_existing_receipt_ignores_other_blocked_checks_outside_db_prerequisites(self) -> None:
        """复查只看 candidate_prerequisites：dry-run 报告顶层 blocked 但库表前置全绿（别的检查项状态）不拦。"""

        report = {"status": "blocked", "checks": [{"check": "x", "status": "blocked"}], "candidate_prerequisites": [{"check": "a", "status": "passed"}]}
        result = self._stage_vc5_with_existing_receipt(report, rc=3)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])

    def test_existing_receipt_with_unparseable_dryrun_fails_closed(self) -> None:
        result = self._stage_vc5_with_existing_receipt("这不是 JSON", rc=0)
        self.assertEqual(result.returncode, 1)
        text = "\n".join(self.sb.status_lines())
        self.assertIn("STOPPED VC5", text)
        self.assertIn("dry-run 输出不是 JSON", text)

    def test_blocked_stop_message_points_to_dryrun_err(self) -> None:
        """没有收据（consume 不通过）走完整准入：blocked 的停止提示要指出详细“怎么改”在 vc5-admission-dryrun.err。"""

        self._install_account_stubs()
        self._prepare_vc5()
        (self.root / "precheck_consume.rc").write_text("2", encoding="utf-8")
        (self.root / "precheck_dryrun.json").write_text(json.dumps(self.BLOCKED_REPORT), encoding="utf-8")
        (self.root / "precheck_dryrun.rc").write_text("3", encoding="utf-8")
        result = self.sb.stage_pipeline("--from", "VC5", "--to", "VC5")
        self.assertEqual(result.returncode, 1, result.stdout[-1500:] + result.stderr[-1500:])
        text = "\n".join(self.sb.status_lines())
        self.assertIn("VC-5 准入 blocked", text)
        self.assertIn("vc5-admission-dryrun.err", text)

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
# 二之二、根盘预警：达到水位（停线前 5 个百分点或可用不足 45 GiB）时提前提示并给出可回收清单，不停也不删
# ======================================================================================================
class DiskEarlyWarningTests(SandboxCase):
    """每个阶段起跑前的磁盘检查通过之后，离停线（69%、可用不低于 40 GiB）已不远时，流水线写 DISKNOTE 并调 entry-dryrun.sh disk-watch。"""

    def _df(self, used_percent: int, avail_gib: int) -> None:
        _write(self.sb.bin / "df", f"""
            #!/bin/bash
            echo "Filesystem Size Used Avail Use% Mounted"
            echo "/dev/stub 100G {used_percent}G {avail_gib}G {used_percent}% /"
        """)

    def _run_vc6(self, **extra: str) -> subprocess.CompletedProcess:
        sb = self.sb
        sb.runroot.mkdir(parents=True, exist_ok=True)
        sb.checkpoint(5)
        (sb.runroot / "vc5-admission.json").write_text("{}", encoding="utf-8")
        _write(sb.pipe / "vc6" / "ok.sh", f"""
            #!/bin/bash
            touch "{sb.newdir}/control/vc/vc-6-checkpoint.json"
            exit 0
        """)
        (sb.prep / "vc6").mkdir(parents=True, exist_ok=True)
        (sb.prep / "vc6" / "ORDER.txt").write_text("run     ok    ok.sh\n", encoding="utf-8")
        return sb.stage_pipeline("--from", "VC6", "--to", "VC6", **extra)

    def _notes(self) -> list[str]:
        return [line for line in self.sb.status_lines() if " DISKNOTE " in line]

    def test_plenty_of_room_prints_no_note_and_does_not_call_disk_watch(self) -> None:
        self._df(40, 60)
        result = self._run_vc6()
        self.assertEqual(result.returncode, 0, result.stdout[-800:] + result.stderr[-800:])
        self.assertEqual(self._notes(), [])
        self.assertNotIn("HOUSEKEEPING disk-watch", self.sb.events())

    def test_close_to_the_stop_line_notes_the_margin_and_lists_what_to_reclaim_but_keeps_going(self) -> None:
        """根盘 66%（停线 69%）、可用 47G：越线前提示，清单取自 disk-watch；流水线照常通过，不跑 housekeeping、不删。"""

        self._df(66, 47)
        result = self._run_vc6()
        self.assertEqual(result.returncode, 0, result.stdout[-800:] + result.stderr[-800:])
        text = "\n".join(self._notes())
        self.assertIn("已到预警线", text)
        self.assertIn("停线 69%", text)
        self.assertIn("距停线约 3 个百分点", text)
        self.assertIn("可用比下限多 7G", text)
        self.assertIn("可回收清单", text)
        self.assertIn("Go 编译缓存旧条目", text)
        self.assertIn("部署回滚备份与文档事务目录", text)
        self.assertEqual(self.sb.events().count("HOUSEKEEPING disk-watch"), 1)
        self.assertNotIn("HOUSEKEEPING housekeeping", self.sb.events(), "预警只提示，不替人清盘")
        self.assertNotIn("DISKWARN", "\n".join(self.sb.status_lines()))
        self.assertTrue((self.sb.prep / "pipeline" / "disk-watch-VC6.out").is_file())
        self.assertIn("PASS VC6", "\n".join(self.sb.status_lines()))

    def test_low_free_space_alone_also_triggers_the_note(self) -> None:
        self._df(50, 44)   # 已用只有 50%，但可用 44G 比 40G 下限只多 4G（< 5G）
        result = self._run_vc6()
        self.assertEqual(result.returncode, 0, result.stdout[-800:] + result.stderr[-800:])
        self.assertIn("可用比下限多 4G", "\n".join(self._notes()))

    def test_margins_are_configurable_and_a_failing_disk_watch_does_not_stop_the_pipeline(self) -> None:
        self._df(66, 47)
        result = self._run_vc6(PIPE_DISK_WARN_PCT_MARGIN="1", PIPE_DISK_WARN_GIB_MARGIN="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._notes(), [], "预警线改成停线前 1 个百分点／多 1G 后，66%／47G 不再提示")
        shutil.rmtree(self.sb.prep, ignore_errors=True)
        (self.sb.prep / "pipeline").mkdir(parents=True)
        (self.root / "disk_watch.rc").write_text("5", encoding="utf-8")   # disk-watch 自己失败：只记 rc，流水线照常
        again = self._run_vc6()
        self.assertEqual(again.returncode, 0, again.stdout[-800:])
        self.assertIn("disk-watch rc=5", "\n".join(self._notes()))

    def test_over_the_line_still_stops_after_housekeeping_and_prints_no_early_note(self) -> None:
        self._df(70, 60)
        result = self._run_vc6()
        self.assertEqual(result.returncode, 1)
        text = "\n".join(self.sb.status_lines())
        self.assertIn("DISKWARN VC6", text)
        self.assertIn("STOPPED VC6", text)
        self.assertEqual(self._notes(), [])
        self.assertIn("HOUSEKEEPING housekeeping", self.sb.events())


# ======================================================================================================
# 二之三、VC-5 库表前置：摘要排除运行时列，改前行级快照、恢复后逐列比较（0.165 恢复后 7 项摘要 3 项不同、原因未核实）
# ======================================================================================================
def _load_prereq():
    import importlib.util

    spec = importlib.util.spec_from_file_location("vc5_prereq_under_test", PIPELINE / "vc5_prereq.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeDatabase:
    """内存里的三张表，接口与 ``PsqlDatabase`` 相同；语句的效果与真实 SQL 一致（这里不连 Postgres）。"""

    ACCOUNT, GROUP, KEY = 90, 3, 3

    def __init__(self) -> None:
        mapping = {"gpt-6-luna": "gpt-6-luna", "gpt-6-astra": "gpt-6-astra", "gpt-6.1-sol": "gpt-6.1-sol", "codex-auto-review": "gpt-6-luna"}
        self.tables = {
            "accounts": [
                {"id": 90, "name": "采集号", "status": "inactive", "schedulable": False, "priority": 1, "deleted_at": None,
                 "credentials": {"model_mapping": mapping, "access_token": "tok-90", "refresh_token": "ref-90", "plan": "pro"},
                 "extra": {"codex_5h_used_percent": 3, "codex_usage_updated_at": "2026-10-29T10:00:00Z", "privacy_mode": "on"},
                 "last_used_at": "2026-10-29T01:00:00Z", "updated_at": "2026-10-29T01:00:00Z", "session_window_start": None,
                 "session_window_end": None, "session_window_status": None, "rate_limited_at": None, "error_message": None}]
            + [{"id": 100 + n, "name": f"账号{n}", "status": "active" if n % 2 else "error", "schedulable": bool(n % 3), "priority": n,
                "deleted_at": None, "credentials": {"access_token": f"tok-{n}", "refresh_token": f"ref-{n}", "expires_at": "2026-10-30T00:00:00Z"},
                "extra": {"codex_7d_used_percent": n, "model_rate_limits": {}, "note": f"n{n}"},
                "last_used_at": f"2026-10-29T0{n}:00:00Z", "updated_at": f"2026-10-29T0{n}:00:00Z", "session_window_start": None,
                "session_window_end": None, "session_window_status": "allowed", "rate_limited_at": None, "error_message": None}
               for n in range(1, 8)],
            "groups": [
                {"id": 3, "name": "采集组", "allow_live": False, "codex_models_manifest_config": {}, "model_allowlist": {}, "rate_multiplier": 1.0,
                 "updated_at": "2026-10-20T00:00:00Z", "deleted_at": None},
                {"id": 4, "name": "其它组", "allow_live": True, "codex_models_manifest_config": {"a": 1}, "model_allowlist": {"b": 2},
                 "rate_multiplier": 1.5, "updated_at": "2026-10-21T00:00:00Z", "deleted_at": None}],
            "api_keys": [
                {"id": 2, "group_id": 3, "status": "inactive", "deleted_at": "2026-10-28T00:00:00Z", "key": "sk-secret-2", "quota_used": 0.0,
                 "usage_5h": 0.0, "last_used_at": None, "updated_at": "2026-10-28T00:00:00Z", "window_5h_start": None},
                {"id": 3, "group_id": 3, "status": "active", "deleted_at": None, "key": "sk-secret-3", "quota_used": 1.5, "usage_5h": 1.5,
                 "last_used_at": "2026-10-29T00:00:00Z", "updated_at": "2026-10-29T00:00:00Z", "window_5h_start": None},
                {"id": 131, "group_id": 3, "status": "active", "deleted_at": None, "key": "sk-secret-131", "quota_used": 0.0, "usage_5h": 0.0,
                 "last_used_at": None, "updated_at": "2026-10-29T00:00:00Z", "window_5h_start": None}]}

    def row(self, table: str, row_id: int) -> dict:
        return next(item for item in self.tables[table] if item["id"] == row_id)

    # ---- 与 PsqlDatabase 相同的接口
    def table(self, name: str) -> list:
        return json.loads(json.dumps(self.tables[name]))

    def remove_model_mapping(self, account: int) -> None:
        self.row("accounts", account)["credentials"].pop("model_mapping", None)

    def set_allow_live(self, group: int, value: bool) -> None:
        self.row("groups", group)["allow_live"] = value

    def set_key_status(self, key: int, old: str, new: str) -> None:
        row = self.row("api_keys", key)
        if row["status"] == old:
            row["status"] = new

    def restore_model_mapping(self, account: int, mapping) -> None:
        self.row("accounts", account)["credentials"]["model_mapping"] = mapping

    def restore_group_fields(self, group: int, row: dict) -> None:
        target = self.row("groups", group)
        for column in ("allow_live", "codex_models_manifest_config", "model_allowlist"):
            target[column] = row[column]

    # ---- 模拟运行时流量：VC-6 金丝雀与生产的真实请求会写这些列
    def runtime_traffic(self) -> None:
        for item in self.tables["accounts"]:
            item["last_used_at"] = "2026-10-30T03:16:00Z"
            item["updated_at"] = "2026-10-30T03:16:01Z"
            item["session_window_start"], item["session_window_end"] = "2026-10-30T03:00:00Z", "2026-10-30T08:00:00Z"
            item["session_window_status"] = "allowed_warning"
            item["extra"] = {**item["extra"], "codex_5h_used_percent": 77, "codex_usage_updated_at": "2026-10-30T03:16:00Z"}
            item["credentials"] = {**item["credentials"], "access_token": "tok-new", "expires_at": "2026-10-31T00:00:00Z"}
        for item in self.tables["groups"]:
            item["updated_at"] = "2026-10-30T03:16:00Z"
        for item in self.tables["api_keys"]:
            item["last_used_at"], item["quota_used"], item["usage_5h"] = "2026-10-30T03:16:00Z", 9.5, 9.5


class Vc5PrereqTests(unittest.TestCase):
    IDS = {"account": 90, "group": 3, "key": 3}

    def setUp(self) -> None:
        self.mod = _load_prereq()
        self.db = FakeDatabase()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.backup = Path(self._tmp.name).resolve() / "backup" / "vc5-prereq"

    def digests(self, db=None) -> dict:
        db = db or self.db
        return self.mod.compute_digests({t: db.table(t) for t in self.mod.TABLES}, **self.IDS)

    def test_digests_ignore_runtime_columns_but_not_real_changes(self) -> None:
        before = self.digests()
        self.assertEqual(list(before), list(self.mod.DIGEST_NAMES))
        self.db.runtime_traffic()
        self.assertEqual(self.digests(), before, "运行时列（last_used_at/updated_at/会话窗口/用量快照键/令牌刷新键/Key 用量）变了，7 项摘要都不该变")
        changes = {
            "other_accounts": lambda d: d.row("accounts", 103).update(schedulable=not d.row("accounts", 103)["schedulable"]),
            "account_other_cols": lambda d: d.row("accounts", 90).update(priority=9),
            "account_cred_without_mapping": lambda d: d.row("accounts", 90)["credentials"].update(plan="free"),
            "group_without_live": lambda d: d.row("groups", 3).update(rate_multiplier=2.0),
            "other_groups": lambda d: d.row("groups", 4).update(allow_live=False),
            "key_without_status": lambda d: d.row("api_keys", 3).update(group_id=4),
            "other_keys": lambda d: d.row("api_keys", 131).update(status="inactive"),
        }
        for name, change in changes.items():
            db = FakeDatabase()
            change(db)
            moved = [key for key, value in self.digests(db).items() if value != before[key]]
            self.assertEqual(moved, [name], f"{name} 对应的真实改动只应让这一项摘要变化")

    def test_the_old_whole_row_digest_would_have_false_alarmed_after_runtime_traffic(self) -> None:
        """复现 0.165 的误报：旧口径只去掉 credentials／updated_at，last_used_at、会话窗口、extra 用量快照留在摘要里，运行时流量一来就变。"""

        def old_style(db) -> str:
            rows = [{k: v for k, v in item.items() if k != "updated_at"} for item in db.table("accounts") if item["id"] != 90]
            return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

        old_before, new_before = old_style(self.db), self.digests()["other_accounts"]
        self.db.runtime_traffic()
        self.assertNotEqual(old_style(self.db), old_before, "旧口径：运行时流量使 other_accounts 变化（误报）")
        self.assertEqual(self.digests()["other_accounts"], new_before, "新口径：不变")

    def test_apply_snapshots_every_row_changes_only_three_places_and_restore_compares_column_by_column(self) -> None:
        original_mapping = json.loads(json.dumps(self.db.row("accounts", 90)["credentials"]["model_mapping"]))
        applied = self.mod.apply(self.db, self.backup, **self.IDS)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["digests_changed"], [])
        self.assertEqual(sorted((item["table"], item["id"], item["column"]) for item in applied["expected_changes"]),
                         [("accounts", 90, "credentials.model_mapping"), ("api_keys", 3, "status"), ("groups", 3, "allow_live")])
        self.assertEqual((applied["counts"]["runtime"], applied["counts"]["unexpected"]), (0, 0))
        # 临时值已写入；备份目录 0700、文件 0600，行级快照含三张表的全部行。
        self.assertNotIn("model_mapping", self.db.row("accounts", 90)["credentials"])
        self.assertTrue(self.db.row("groups", 3)["allow_live"])
        self.assertEqual(self.db.row("api_keys", 3)["status"], "inactive")
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        for name in ("snapshot-before.json", "snapshot-after.json", "digests-before.txt", "digests-after.txt", "group-row.json", "applied"):
            self.assertEqual(stat.S_IMODE((self.backup / name).stat().st_mode), 0o600, name)
        snapshot = json.loads((self.backup / "snapshot-before.json").read_text(encoding="utf-8"))
        self.assertEqual({table: len(rows) for table, rows in snapshot.items()}, {"accounts": 8, "groups": 2, "api_keys": 3})
        # VC-6 期间真实请求写运行时列 → 恢复 → 逐列比较：运行时列只报告，其余全一致。
        self.db.runtime_traffic()
        restored = self.mod.restore(self.db, self.backup, **self.IDS)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual((restored["unexpected"], restored["digests_changed"]), ([], []))
        self.assertEqual(restored["digests_unchanged"], list(self.mod.DIGEST_NAMES))
        self.assertGreater(restored["counts"]["runtime"], 20)
        self.assertIn("accounts.last_used_at", restored["runtime_columns_touched"])
        self.assertIn("accounts.extra.codex_5h_used_percent", restored["runtime_columns_touched"])
        self.assertIn("api_keys.quota_used", restored["runtime_columns_touched"])
        self.assertEqual(self.db.row("accounts", 90)["credentials"]["model_mapping"], original_mapping)
        self.assertFalse(self.db.row("groups", 3)["allow_live"])
        self.assertEqual(self.db.row("api_keys", 3)["status"], "active")
        self.assertTrue((self.backup / "snapshot-restored.json").is_file())

    def test_restore_reports_a_real_change_to_another_account_by_table_row_and_column(self) -> None:
        self.mod.apply(self.db, self.backup, **self.IDS)
        self.db.row("accounts", 104)["schedulable"] = not self.db.row("accounts", 104)["schedulable"]   # 有人动了别的账号的调度开关
        self.db.row("accounts", 105)["extra"]["note"] = "被改过"
        self.db.row("accounts", 106)["status"] = "disabled"
        report = self.mod.restore(self.db, self.backup, **self.IDS)
        self.assertFalse(report["ok"])
        found = sorted((item["table"], item["id"], item["column"]) for item in report["unexpected"])
        self.assertEqual(found, [("accounts", 104, "schedulable"), ("accounts", 105, "extra.note"), ("accounts", 106, "status")])
        self.assertEqual(report["digests_changed"], ["other_accounts"])

    def test_incomplete_restore_and_row_loss_are_caught(self) -> None:
        self.mod.apply(self.db, self.backup, **self.IDS)
        self.db.restore_model_mapping = lambda *args: None            # 恢复漏了 model_mapping
        self.db.set_key_status = lambda *args: None                   # 也漏了 Key 状态
        del self.db.tables["api_keys"][2]                              # Key 131 被删
        report = self.mod.restore(self.db, self.backup, **self.IDS)
        self.assertFalse(report["ok"])
        found = sorted((item["table"], item["id"], item["column"]) for item in report["unexpected"])
        self.assertEqual(found, [("accounts", 90, "credentials.model_mapping"), ("api_keys", 3, "status"), ("api_keys", 131, "<row>")])

    def test_secret_values_never_reach_the_report(self) -> None:
        self.mod.apply(self.db, self.backup, **self.IDS)
        self.db.runtime_traffic()
        self.db.row("accounts", 101)["credentials"]["plan"] = "leaked-plan-value"      # 非运行时的凭据键变了：报键名，不报值
        self.db.row("api_keys", 2)["key"] = "sk-new-secret-value"
        report = self.mod.restore(self.db, self.backup, **self.IDS)
        text = json.dumps(report, ensure_ascii=False)
        for secret in ("leaked-plan-value", "sk-new-secret-value", "tok-new", "tok-90", "ref-90", "sk-secret-3"):
            self.assertNotIn(secret, text)
        self.assertIn("credentials.plan", text)
        self.assertEqual(sorted((item["table"], item["id"], item["column"]) for item in report["unexpected"]),
                         [("accounts", 101, "credentials.plan"), ("api_keys", 2, "key")])

    def test_state_guards_and_cli_exit_codes(self) -> None:
        argv = ["apply", "--account", "90", "--group", "3", "--key", "3", "--backup-dir", str(self.backup)]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.mod.main(argv, database=self.db), 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.mod.main(argv, database=self.db), 3)
        self.assertIn("已应用过", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.main(["verify", *argv[1:]], database=self.db), 1, "临时值还没恢复：逐列比较不通过")
            self.assertEqual(self.mod.main(["restore", *argv[1:]], database=self.db), 0)
            self.assertEqual(self.mod.main(["verify", *argv[1:]], database=self.db), 0)
            self.assertEqual(self.mod.main(["show", *argv[1:]], database=self.db), 0)
        fresh = Path(self._tmp.name) / "never-applied"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.mod.main(["restore", "--account", "90", "--group", "3", "--key", "3", "--backup-dir", str(fresh)],
                                           database=self.db), 3)
        self.assertIn("没有应用记录", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.mod.main(["show", "--account", "90", "--group", "3", "--key", "3", "--backup-dir", "rel"], database=self.db), 2)

    def test_psql_statements_match_the_ones_used_on_the_host_in_the_previous_upgrade(self) -> None:
        calls = []

        def fake_run(command, input=None, **_kwargs):
            calls.append((command, input))
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        db = self.mod.PsqlDatabase("pg-box", "u", "d")
        with unittest.mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            db.table("accounts")
            db.remove_model_mapping(90)
            db.set_allow_live(3, True)
            db.set_key_status(3, "active", "inactive")
            db.restore_model_mapping(90, {"a": "b"})
            db.restore_group_fields(3, {"allow_live": False, "codex_models_manifest_config": {}, "model_allowlist": {"x'y": 1}})
        sql = [item[1] for item in calls]
        self.assertEqual(calls[0][0][:8], ["docker", "exec", "-i", "pg-box", "psql", "-U", "u", "-d"])
        self.assertIn("to_jsonb(t)", sql[0])
        self.assertEqual(sql[1], "update accounts set credentials = credentials - 'model_mapping' where id=90 and jsonb_typeof(credentials->'model_mapping')='object'")
        self.assertEqual(sql[2], "update groups set allow_live=true where id=3")
        self.assertEqual(sql[3], "update api_keys set status='inactive' where id=3 and status='active'")
        self.assertIn("jsonb_set(credentials, '{model_mapping}', :'m'::jsonb) where id=90", sql[4])
        self.assertEqual(calls[4][0][-2:], ["-v", 'm={"a": "b"}'])
        self.assertIn("model_allowlist='{\"x''y\": 1}'::jsonb", sql[5])

    def test_wrapper_script_and_files_are_in_the_driver_manifest(self) -> None:
        manifest = {row["path"]: row for row in json.loads((DRIVER_ROOT / "manifest.json").read_text(encoding="utf-8"))["files"]}
        for relative in ("driver/pipeline/vc5_prereq.py", "driver/pipeline/vc5-prereq.sh"):
            self.assertEqual(manifest[relative]["mode"], "0700")
        result = subprocess.run(["bash", "-n", str(PIPELINE / "vc5-prereq.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (PIPELINE / "vc5_prereq.py").read_text(encoding="utf-8")
        for forbidden in ("= 90", "= 131", "id=90", "id=3 "):
            self.assertNotIn(forbidden, text, "账号、分组、Key 号一律来自参数，不写死")


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

    def _run_with_relative_path(self, cwd: Path, script_arg: str, *args: str) -> subprocess.CompletedProcess:
        """以“相对路径”调用清理脚本：脚本参数是相对 cwd 的路径，而不是绝对路径。"""

        extra = {"VC6_CLEANUP_APPROVED_BY": "测试批准依据", "VC6_ARCHIVE_DIR": str(self.archive_dir), "PYTHONPATH": str(self.sb.data)}
        return subprocess.run(["bash", script_arg, *args], env=self.sb.environ(D=str(self.sb.data), **extra), cwd=cwd,
                              capture_output=True, text=True, errors="replace", timeout=60, stdin=subprocess.DEVNULL)

    def test_relative_path_invocation_from_sandbox_root_does_not_fail(self) -> None:
        """脚本先 cd 到数据根、再按脚本目录 source vc6-env.sh：用相对路径调用时，旧写法 dirname 是相对的，cd 之后就找不到 vc6-env.sh。"""

        self._docker(running=self.FINAL)
        relative = os.path.relpath(self.script, self.root)
        self.assertFalse(os.path.isabs(relative))
        result = self._run_with_relative_path(self.root, relative, "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("No such file", result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["problems"], [])

    def test_relative_path_invocation_from_script_dir_with_dot_slash(self) -> None:
        """在脚本所在目录用 ./脚本名 调用（dirname 为 "."）：同样不能报错，并且能走到真正的执行（不只是预检）。"""

        self._docker(running=self.FINAL)
        result = self._run_with_relative_path(self.script.parent, "./vc6-cleanup-execute.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._execution()["post_checks"]["running_image_kind"], "final")
        self.assertFalse(self.junk.exists(), "清单内目录应已删除")

    def test_script_dir_is_resolved_before_cd_to_data_root(self) -> None:
        """静态合同：解析脚本目录的那行必须出现在 cd 到数据根之前，source 用解析后的变量，不再直接用 BASH_SOURCE 的相对 dirname。"""

        lines = list(_code_lines(self.script))
        resolve = next(n for n, line in lines if "VC6_SELF_DIR=" in line and "BASH_SOURCE" in line)
        cd_data = next(n for n, line in lines if 'cd "$D"' in line)
        source = next((n, line) for n, line in lines if "vc6-env.sh" in line and "source" in line)
        self.assertLess(resolve, cd_data)
        self.assertGreater(source[0], cd_data)
        self.assertNotIn("BASH_SOURCE", source[1])

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


class WaitMarkerFailureWordsTests(unittest.TestCase):
    """vc-pipeline.sh 的 wait_marker 扫日志里的失败字样；脚本自己打印的“=== ”阶段标题行不能算失败。

    0.165 事故：VC2 阶段用 'Traceback|拒绝|失败|…' 扫 vc23.out，vc23-seq.sh 的标题行“=== VC-3 前重放离线报告，拒绝批准后的输入漂移”
    含“拒绝”被当成失败，流水线写 STOPPED VC2（实际脚本没出错、后面照常跑完）。
    """

    BAD_VC23 = "Traceback|拒绝|失败|rc=[1-9]|断言|Error"
    BAD_VC4 = "Traceback|拒绝|失败|BATCH FAILED"

    def _wait(self, text: str, bad: str) -> int:
        script = (PIPELINE / "vc-pipeline.sh").read_text(encoding="utf-8")
        match = re.search(r"^wait_marker\(\) \{\n.*?^\}\n", script, re.S | re.M)
        self.assertIsNotNone(match, "找不到 wait_marker 函数")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "out.log").write_text(text, encoding="utf-8")
            wrapper = _write(root / "call.sh", f"""
                #!/bin/bash
                set -u -o pipefail
                log() {{ :; }}
                disk_txt() {{ echo x; }}
                sleep() {{ :; }}
                {match.group(0)}
                wait_marker "{root}/out.log" 'VC_DONE' '{bad}' 1 t ''
                echo "rc=$?"
            """)
            done = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
            self.assertEqual(done.returncode, 0, done.stderr)
            return int(done.stdout.strip().rsplit("rc=", 1)[1])

    def test_real_stage_titles_are_not_failures(self) -> None:
        """用驱动脚本里真实的标题行当输入：命中“拒绝”的标题不再触发失败，等不到成功标记时只是超时（rc=1）。"""

        title = next(line for line in (REAL_DRIVER / "vc2-approve-and-stage.sh").read_text(encoding="utf-8").splitlines()
                     if line.startswith('echo "=== VC-3 前重放离线报告'))
        printed = title.split('echo "', 1)[1].rsplit('"', 1)[0]
        self.assertIn("拒绝", printed, "标题行措辞变了，请换一条仍含失败字样的真实标题")
        self.assertEqual(self._wait(printed + "\n", self.BAD_VC23), 1)
        self.assertEqual(self._wait(printed + "\n=== 管理 token\nVC23_DONE-not-yet\n", self.BAD_VC4), 1)

    def test_real_failures_are_still_caught(self) -> None:
        title = "=== VC-3 前重放离线报告，拒绝批准后的输入漂移\n"
        self.assertEqual(self._wait(title + "本轮客户端（claude）的 VC-2／VC-3 派发脚本是 x，拒绝\n", self.BAD_VC23), 3)
        self.assertEqual(self._wait(title + "Traceback (most recent call last):\n", self.BAD_VC23), 3)
        self.assertEqual(self._wait(title + "step rc=3\n", self.BAD_VC23), 3)
        self.assertEqual(self._wait("BATCH FAILED 4\n", self.BAD_VC4), 3)

    def test_success_marker_wins_even_with_titles_present(self) -> None:
        self.assertEqual(self._wait("=== 拒绝 失败 标题\nVC_DONE\n", self.BAD_VC23), 0)

    def test_failure_words_in_pipeline_calls_match_this_test(self) -> None:
        """本测试用的两组失败正则必须与流水线里实际传给 wait_marker 的一致，否则测的不是真实调用。"""

        text = (PIPELINE / "vc-pipeline.sh").read_text(encoding="utf-8")
        self.assertIn(f"'{self.BAD_VC23}'", text)
        self.assertIn(f"'{self.BAD_VC4}'", text)


class EntryCoordinatesNotInheritedByGatesTests(unittest.TestCase):
    """门禁里的测试不得继承入口坐标 ENTRY_COMMIT／ENTRY_BUNDLE／ENTRY_BRANCH。

    parse_env 把本轮参数文件里的全部键导出，其中三个入口坐标是入口编排器自己的参数；``source lib.sh`` 之后起 ``make test`` 的脚本若不清掉，
    测试进程继承到它们，test_ci_entry_preflight_skip 等用例把它们当成“入口正在运行”的信号而红（0.165 的 VC-4 门禁因此红过一次）。
    """

    NAMES = ("ENTRY_COMMIT", "ENTRY_BUNDLE", "ENTRY_BRANCH")
    # 在驱动脚本里直接起门禁（make）的脚本：新增这类脚本时本集合会对不上，逼着作者想清楚要不要清入口坐标。
    GATE_LAUNCHERS = {"arm64-vc4-gates.sh", "gates.sh", "vc5-gate-target.sh"}

    def _code(self, path: Path) -> list[str]:
        return [line for _, line in _code_lines(path)]

    def test_isolated_run_does_not_pass_entry_coordinates_to_the_gate(self) -> None:
        """行为：取 lib.sh 里真实的 isolated_run，把 unshare 换成只打印环境的桩，父 shell 里导出三个入口坐标。"""

        text = (REAL_DRIVER / "lib.sh").read_text(encoding="utf-8")
        match = re.search(r"^isolated_run\(\) \{\n.*?^\}\n", text, re.S | re.M)
        self.assertIsNotNone(match, "找不到 isolated_run")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tree").mkdir()
            _write(root / "bin" / "unshare", f"#!/bin/bash\nenv > '{root}/seen.env'\n")
            wrapper = _write(root / "call.sh", f"""
                #!/bin/bash
                HISTORICAL_SOURCE_ROOT=/x
                export ENTRY_COMMIT={'1' * 40} ENTRY_BUNDLE=/b/entry.bundle ENTRY_BRANCH=feature/entry CODEX_UPGRADE_IDENTITY_MEMO=/memo KEEP_ME=yes
                {match.group(0)}
                isolated_run "{root}/tree" . "{root}/pyc" "{root}/out" "{root}/err" make test
            """)
            done = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env={"PATH": f"{root}/bin:/usr/bin:/bin"})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            seen = (root / "seen.env").read_text(encoding="utf-8")
        for name in self.NAMES + ("CODEX_UPGRADE_IDENTITY_MEMO",):
            self.assertNotIn(f"{name}=", seen, f"{name} 不得进入门禁进程的环境")
        self.assertIn("KEEP_ME=yes", seen, "只清指定的变量，其余环境照旧")
        self.assertIn("PYTHONPYCACHEPREFIX=", seen)

    def test_every_gate_launcher_clears_the_entry_coordinates(self) -> None:
        launchers = set()
        for path in sorted(REAL_DRIVER.glob("*.sh")):
            code = "\n".join(self._code(path))
            if re.search(r"exec make |isolated_run [^\n]* make ", code):
                launchers.add(path.name)
        self.assertEqual(launchers, self.GATE_LAUNCHERS, "驱动里起门禁的脚本变了：新增的要清入口坐标，并登记到本测试")
        # arm64-vc4-gates.sh 经 lib.sh 的 isolated_run 起门禁（上一条行为测试覆盖）；另外两个自己用 env -u 清。
        for name in ("gates.sh", "vc5-gate-target.sh"):
            code = "\n".join(self._code(REAL_DRIVER / name))
            line = next(item for item in code.splitlines() if "exec make" in item)
            for variable in self.NAMES:
                self.assertIn(f"-u {variable}", line.split("unshare")[0], f"{name} 起 make test 前必须 env -u {variable}")
        isolated = "\n".join(self._code(REAL_DRIVER / "lib.sh"))
        self.assertIn("unset CODEX_UPGRADE_IDENTITY_MEMO ENTRY_COMMIT ENTRY_BUNDLE ENTRY_BRANCH", isolated)

    def test_vc6_gates_clear_them_right_after_sourcing_vc6_env(self) -> None:
        """VC-6 post-promotion 门禁直接起计划里的 go test：vc6-env.sh 要用 ENTRY_COMMIT 做占位断言，用完立刻清。"""

        code = self._code(PIPELINE / "vc6" / "vc6-gates.sh")
        source = next(i for i, line in enumerate(code) if "vc6-env.sh" in line and line.lstrip().startswith("source"))
        unset = next(i for i, line in enumerate(code) if line.strip() == "unset ENTRY_COMMIT ENTRY_BUNDLE ENTRY_BRANCH")
        self.assertGreater(unset, source)
        run = next(i for i, line in enumerate(code) if "subprocess.run" in line)
        self.assertGreater(run, unset, "清除必须在起门禁命令之前")


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


class StartEntryPrecheckTests(unittest.TestCase):
    """start-entry.sh 起跑前自检新增的 C10（Go 编译缓存容量）、C11（本轮输入已准备）及相关开关。

    start-entry.sh 本体读 /proc 与真实 docker／psql，不在开发机上整体运行；检查逻辑放在 pipeline-lib.sh 的函数里单测，
    脚本接线用静态合同守。
    """

    START_ENTRY = PIPELINE / "start-entry.sh"

    def _call(self, root: Path, call: str, stat_type: str = "tmpfs", df_m: str = "4096") -> subprocess.CompletedProcess:
        """stat／df 换成桩（开发机的 stat -f -c 与 df -BM 参数不兼容），然后 source 真实 pipeline-lib.sh 调一个函数。"""

        stubs = root / "bin"
        _write(stubs / "stat", f"#!/bin/bash\necho '{stat_type}'\n")
        _write(stubs / "df", f"#!/bin/bash\nprintf 'Filesystem 1048576-blocks Used Available Capacity Mounted\\ntmpfs {df_m}M 1M 1M 1%% /x\\n'\n")
        wrapper = _write(root / "call.sh", f"""
            #!/bin/bash
            source "{PIPELINE}/pipeline-lib.sh"
            {call}
        """)
        return subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, cwd=root,
                              env={"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"})

    def test_go_cache_on_small_tmpfs_is_refused_with_remount_hint(self) -> None:
        """0.165 事故：4 GiB 的 tmpfs 在腾盘清空 Go 缓存后冷启动写满。小于 10 GiB 的内存盘必须在起跑前拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cache").mkdir()
            done = self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 10', "tmpfs", "4096")
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("4 GiB < 10 GiB", done.stdout)
            self.assertIn("remount,size=10G", done.stdout)

    def test_go_cache_on_big_enough_tmpfs_passes_and_threshold_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cache").mkdir()
            ok = self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 10', "tmpfs", "10240")
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("10 GiB ≥ 10 GiB", ok.stdout)
            low = self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 4', "tmpfs", "4096")
            self.assertEqual(low.returncode, 0, "阈值可调：要求 4 GiB 时 4 GiB 的内存盘通过")
            self.assertEqual(self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 10', "tmpfs", "10239").returncode, 1)

    def test_go_cache_on_disk_or_missing_dir_is_not_applicable(self) -> None:
        """缓存在磁盘上（含目录还不存在、父目录在磁盘上）由根盘检查约束，本检查不拦。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cache").mkdir()
            disk = self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 10', "ext2/ext3", "1024")
            self.assertEqual(disk.returncode, 0, disk.stdout + disk.stderr)
            self.assertIn("在磁盘上", disk.stdout)
            missing = self._call(root, f'pl_check_go_cache_capacity "{root}/not/yet/created" 10', "ext2/ext3", "1024")
            self.assertEqual(missing.returncode, 0, missing.stdout + missing.stderr)

    def test_go_cache_on_tmpfs_with_unreadable_size_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cache").mkdir()
            done = self._call(root, f'pl_check_go_cache_capacity "{root}/cache" 10', "tmpfs", "")
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("读不出容量", done.stdout)

    def test_round_inputs_check_requires_both_prepare_outputs(self) -> None:
        """漏跑 prepare-round-inputs.sh：P0 回退依据与基线画像都缺，入口便宜检查两项会红；起跑前自检要先拦下并指向该脚本。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rr, data = root / "run", root / "data"
            call = f'pl_check_round_inputs "{rr}" "{data}" in-x'
            both_missing = self._call(root, call)
            self.assertEqual(both_missing.returncode, 1)
            self.assertIn("rollback-catalog-promotion-receipt.json", both_missing.stdout)
            self.assertIn("baseline-profile.json", both_missing.stdout)
            self.assertIn("prepare-round-inputs.sh", both_missing.stdout)
            rr.mkdir()
            (rr / "rollback-catalog-promotion-receipt.json").write_text("{}", encoding="utf-8")
            one_missing = self._call(root, call)
            self.assertEqual(one_missing.returncode, 1)
            self.assertNotIn("rollback-catalog-promotion-receipt.json", one_missing.stdout)
            self.assertIn("baseline-profile.json", one_missing.stdout)
            (data / "control" / "in-x").mkdir(parents=True)
            (data / "control" / "in-x" / "baseline-profile.json").write_text("", encoding="utf-8")
            self.assertEqual(self._call(root, call).returncode, 1, "空文件不算就绪")
            (data / "control" / "in-x" / "baseline-profile.json").write_text("{}", encoding="utf-8")
            ready = self._call(root, call)
            self.assertEqual(ready.returncode, 0, ready.stdout)
            self.assertIn("prepare-round-inputs.sh 已跑", ready.stdout)

    def test_round_inputs_check_honours_explicit_rollback_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "control" / "in-x").mkdir(parents=True)
            (root / "data" / "control" / "in-x" / "baseline-profile.json").write_text("{}", encoding="utf-8")
            (root / "elsewhere.json").write_text("{}", encoding="utf-8")
            done = self._call(root, f'pl_check_round_inputs "{root}/run" "{root}/data" in-x "{root}/elsewhere.json"')
            self.assertEqual(done.returncode, 0, done.stdout)

    def test_start_entry_wires_the_new_checks_and_switches(self) -> None:
        """脚本接线（静态）：C10／C11 调用函数且失败计 FAIL；--check 不建目录；SKIP_CAPTURE_ACCOUNT_ON 先于 capture-account-on.sh 判断。"""

        code = [line for _, line in _code_lines(self.START_ENTRY)]
        text = "\n".join(code)
        self.assertRegex(text, r'pl_check_go_cache_capacity "\$\(dirname "\$\{D\}"\)/entry-gates-work/go-build-cache"')
        self.assertRegex(text, r'else fail "C10 ')
        self.assertRegex(text, r'pl_check_round_inputs "\$\{RUNROOT\}" "\$\{D\}" "\$\{IN\}"')
        self.assertRegex(text, r'else fail "C11 ')
        self.assertIn('[ "${MODE}" = check ] || mkdir -p "${P}"', text)
        self.assertNotRegex(text, r"(?m)^mkdir -p \"\$\{P\}\"")
        skip = text.index('"${SKIP_CAPTURE_ACCOUNT_ON:-0}" = 1')
        self.assertLess(skip, text.index('bash "${PIPE_HOME}/capture-account-on.sh"'), "跳过判断必须在调用 capture-account-on.sh 之前")
        self.assertIn("warn \"SKIP_CAPTURE_ACCOUNT_ON=1", text, "跳过要留 WARN，不静默")

    def test_start_entry_documents_the_switches_in_header(self) -> None:
        head = "\n".join(self.START_ENTRY.read_text(encoding="utf-8").splitlines()[:30])
        self.assertIn("SKIP_CAPTURE_ACCOUNT_ON=1", head)
        self.assertIn("GO_CACHE_TMPFS_MIN_GIB", head)


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


class Vc6RelativePathInvocationTests(SandboxCase):
    """VC-6 里“先 cd 到数据根、再 source 同目录 vc6-env.sh”的脚本：用相对路径调用时不能找不到 vc6-env.sh。

    旧写法是 cd "$D" 之后才 source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"：脚本用 ./xxx.sh 或 pipeline/vc6/xxx.sh 这类相对路径
    调用时，dirname 得到的是相对路径，cd 之后它指向数据根下并不存在的位置，source 报 No such file。
    修法与清理脚本一致：cd 之前把脚本目录解析成绝对路径（VC6_SELF_DIR），之后一律用它。
    """

    # 这 7 个脚本都是先 cd "$D" 再 source vc6-env.sh（清理脚本 vc6-cleanup-execute.sh 的同类测试见 Vc6CleanupTests）
    CD_FIRST_SCRIPTS = (
        "vc6-activation-receipt.sh", "vc6-build.sh", "vc6-gates.sh", "vc6-cleanup-decision.sh",
        "vc6-gate-receipt.sh", "vc6-retire-build.sh", "vc6-private-archive.sh",
    )

    def setUp(self) -> None:
        super().setUp()
        self.vc6 = self.sb.pipe / "vc6"
        # vc6-env.sh 换成桩：打印自己被 source 时的路径后直接结束脚本（真 vc6-env.sh 另有占位断言，见 Vc6EnvTests）
        _write(self.vc6 / "vc6-env.sh", """
            echo "VC6_ENV_STUB_SOURCED ${BASH_SOURCE[0]}"
            exit 0
        """, 0o644)

    def _run_relative(self, cwd: Path, script_arg: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", script_arg], env=self.sb.environ(D=str(self.sb.data), **extra), cwd=cwd,
                              capture_output=True, text=True, errors="replace", timeout=60, stdin=subprocess.DEVNULL)

    def test_relative_path_invocation_finds_vc6_env(self) -> None:
        """两种相对调用：在脚本所在目录用 ./脚本名（dirname 为 "."），以及在沙箱根目录用相对子路径。都要 source 到同目录的桩，且路径是绝对的。"""

        stub = str(self.vc6 / "vc6-env.sh")
        for name in self.CD_FIRST_SCRIPTS:
            for cwd, arg in ((self.vc6, f"./{name}"), (self.root, os.path.relpath(self.vc6 / name, self.root))):
                with self.subTest(script=name, cwd=str(cwd), arg=arg):
                    self.assertFalse(os.path.isabs(arg))
                    result = self._run_relative(cwd, arg)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("No such file", result.stderr)
                    self.assertIn(f"VC6_ENV_STUB_SOURCED {stub}", result.stdout)

    def _fake_python(self) -> Path:
        """假 python3：只回显参数并成功退出，用来看脚本把“脚本目录下的构造器”传成了什么路径。"""

        fake = self.root / "fakepy"
        _write(fake / "python3", """
            #!/bin/bash
            echo "FAKE_PY $*"
        """)
        return fake

    def _receipt_env(self, with_gate_attempt: bool) -> tuple[Path, Path]:
        """给两个收据脚本备齐 vc6-env.sh 之后要读的文件：V6 目录、证据根 R 及其里的前置证据。返回 (V6, R)。"""

        v6 = self.root / "v6work"
        r = self.root / "v6evidence"
        # 两个脚本共用同一组目录名：先清掉上一个子用例（含它失败中断）留下的
        shutil.rmtree(v6, ignore_errors=True)
        shutil.rmtree(r, ignore_errors=True)
        v6.mkdir()
        r.mkdir()
        (v6 / "vc6-root.txt").write_text(str(r), encoding="utf-8")
        _write(self.vc6 / "vc6-env.sh", f"""
            echo "VC6_ENV_STUB_SOURCED ${{BASH_SOURCE[0]}}"
            export V6="{v6}" CAND=cand-x
        """, 0o644)
        if with_gate_attempt:
            (v6 / "vc6-gates-attempt.txt").write_text("attempt-x", encoding="utf-8")
            for fname in ("acceptance-result.json", "catalog-promotion-receipt.json", "post-promotion-gates.receipt.json"):
                (r / fname).write_text("{}", encoding="utf-8")
        else:
            for stage in ("canary", "production_switch", "rollback", "target_restore"):
                (r / "stages" / stage).mkdir(parents=True)
                (r / "stages" / stage / "stage.json").write_text("{}", encoding="utf-8")
            (r / "activation-receipt.json").write_text("{}", encoding="utf-8")
        return v6, r

    def test_receipt_scripts_resolve_builder_next_to_script_when_called_relatively(self) -> None:
        """两个收据脚本在 source 之后还要用脚本目录找同目录的 facts 构造器（HERE）：相对调用时必须是绝对路径、且文件真实存在。"""

        cases = (("vc6-activation-receipt.sh", "build_activation_facts.py", False), ("vc6-gate-receipt.sh", "build_gate_facts_vc6.py", True))
        for name, builder, gate in cases:
            with self.subTest(script=name):
                self._receipt_env(gate)
                fake = self._fake_python()
                result = self._run_relative(self.vc6, f"./{name}", PATH=f"{fake}:{self.sb.path}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = self.vc6 / builder
                self.assertTrue(expected.is_file())
                self.assertIn(f"FAKE_PY -B {expected} ", result.stdout)

    def test_script_dir_is_resolved_before_cd_to_data_root(self) -> None:
        """静态合同（覆盖 vc6 目录下全部脚本，防止以后新增的脚本再犯）：任何脚本在 cd 到数据根之后，都不得再用 BASH_SOURCE 推导路径；
        先 cd 再 source 的脚本必须有 VC6_SELF_DIR 先于 cd 解析，source 用解析后的变量。"""

        scripts = sorted(PIPELINE.glob("vc6/*.sh"))
        self.assertGreaterEqual(len(scripts), 20)
        for path in scripts:
            lines = list(_code_lines(path))
            cd_lines = [(n, line) for n, line in lines if 'cd "$D"' in line]
            if not cd_lines:
                continue
            cd_number, cd_text = cd_lines[0]
            with self.subTest(script=path.name):
                late = [f"{n}: {line.strip()[:100]}" for n, line in lines
                        if "BASH_SOURCE" in (line[line.index('cd "$D"'):] if n == cd_number else line) and n >= cd_number]
                self.assertEqual(late, [], "cd 到数据根之后不得再用 BASH_SOURCE 的相对 dirname")
        for name in self.CD_FIRST_SCRIPTS:
            lines = list(_code_lines(PIPELINE / "vc6" / name))
            resolve = next(n for n, line in lines if "VC6_SELF_DIR=" in line and "BASH_SOURCE" in line)
            cd_data = next(n for n, line in lines if 'cd "$D"' in line)
            source = next((n, line) for n, line in lines if "vc6-env.sh" in line and "source" in line)
            with self.subTest(script=name, check="解析先于 cd"):
                self.assertLess(resolve, cd_data)
                self.assertGreater(source[0], cd_data)
                self.assertIn("VC6_SELF_DIR", source[1])


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

    def test_stage_stop_for_short_deadline_tells_how_to_retry_deadline_extend(self) -> None:
        """计时账本剩余不足时流水线停下交调度者延期；延期命令在批次执行期间会被设计内的互斥拒绝，停下提示里要写明怎么重试。"""

        sb = self.sb
        sb.runroot.mkdir(parents=True, exist_ok=True)
        sb.checkpoint(4)
        _write(sb.data / "tools/official_client_capture/codex_upgrade_vc_artifacts.py", """
            import datetime
            def effective_deadlines(path):
                soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return {"phase": "VC-5", "execution_deadline_at_utc": soon, "total_deadline_at_utc": soon,
                        "project_deadline_at_utc": soon, "paused_scopes": []}
        """, 0o644)
        result = sb.stage_pipeline("--from", "VC5", "--to", "VC5")
        self.assertEqual(result.returncode, 1, result.stdout[-800:])
        text = "\n".join(sb.status_lines())
        self.assertIn("计时账本剩余不足或已暂停", text)
        self.assertIn("设计内的互斥拒绝", text)
        self.assertIn("每 30～60 秒重试同一条命令", text)

    def _check_vc6(self, order: str, installed: dict[str, str], *, prep_vc6_env: bool = False) -> subprocess.CompletedProcess:
        """--check --from VC6：ORDER.txt 在 <PREP>/vc6/，脚本与 vc6-env.sh 在已装驱动 pipeline/vc6/（与运行体同一口径）。"""

        sb = self.sb
        (sb.prep / "vc6").mkdir(parents=True, exist_ok=True)
        (sb.prep / "vc6" / "ORDER.txt").write_text(order, encoding="utf-8")
        (sb.pipe / "vc6" / "vc6-env.sh").unlink()   # 沙箱里带的是真实副本；这里只留 installed 里写明的
        for name, body in installed.items():
            _write(sb.pipe / "vc6" / name, body)
        if prep_vc6_env:
            _write(sb.prep / "vc6" / "vc6-env.sh", "#!/bin/bash\n")
        return sb.run(["bash", str(sb.pipe / "vc-pipeline.sh"), "--check", "--from", "VC6", "--to", "VC6"])

    def test_check_vc6_resolves_relative_scripts_and_vc6_env_in_the_installed_driver(self) -> None:
        """0.165 实战：--check --from VC6 把 ORDER 里的相对脚本名当作当前目录的文件、vc6-env.sh 去 <PREP>/vc6/ 找，
        而真实运行用已装驱动 pipeline/vc6/ 里的同名文件，两边口径不一致（检查红、运行绿）。"""

        result = self._check_vc6("run     step-a    step-a.sh\n", {"step-a.sh": "#!/bin/bash\nexit 0\n", "vc6-env.sh": "#!/bin/bash\n"})
        self.assertIn("ORDER 引用的 1 个 run 脚本都在位且 bash -n 通过", result.stdout, result.stdout)
        self.assertIn("vc6-env.sh 在位", result.stdout)
        self.assertNotIn("缺脚本", result.stdout)
        self.assertNotRegex(result.stdout, r"\[缺\] .*vc6-env\.sh")

    def test_check_vc6_still_reports_missing_or_broken_scripts_and_missing_env(self) -> None:
        result = self._check_vc6("run     step-a    step-a.sh\nrun     step-b    step-b.sh\nrun     step-c    /nonexistent/abs.sh\n",
                                 {"step-b.sh": "#!/bin/bash\nif then\n"}, prep_vc6_env=True)
        self.assertIn(f"缺脚本：step-a -> {self.sb.pipe}/vc6/step-a.sh", result.stdout)
        self.assertIn(f"语法不通过：{self.sb.pipe}/vc6/step-b.sh", result.stdout)
        self.assertIn("缺脚本：step-c -> /nonexistent/abs.sh", result.stdout)
        self.assertIn("ORDER 引用的脚本有 3 个缺失或语法不通过", result.stdout)
        self.assertRegex(result.stdout, r"\[缺\] .*pipeline/vc6/vc6-env\.sh", "vc6-env.sh 只在 <PREP>/vc6/ 有、已装驱动里没有：运行会失败，检查必须报缺")

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
