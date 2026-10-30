"""版本登记「重封链」一键化（``tools/ci/reseal_chain.py``）的单元测试。

全部用合成的小仓库：测试在系统临时目录里 ``git init`` 一个只有几个桩文件的仓库（事实映射、候选追踪脚本、身份守恒测试桩、
冻结承接收据生成器桩、驱动 manifest 桩、依赖追踪器桩），不碰真实工作树，也不在真实工作树里建任何临时目录。
真实仓库上的整链演练（构造"事实映射需要重绑"的场景一条命令走完）见工具优化任务 3 的报告，不在单元测试里。

覆盖：计划与前置检查（dry-run 不改任何东西、脏树／主干／缺确认被拒）、整链按序提交且提交说明格式固定、各步骤无变化时跳过、
失败时停在原地并还原工作树、``--from`` 续跑（含 HEAD 对不上被拒）、``--only``、追踪任务的复用与新增 unobserved 的失败关闭、
manifest 重建、REGISTERED_CONTROL_CHANGES 清空、载荷审核对比函数，以及"脚本里没有 push"。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import reseal_chain as rc

REPO_ROOT = Path(__file__).resolve().parents[3]
CHAIN_SOURCE = REPO_ROOT / "tools" / "ci" / "reseal_chain.py"

MAP_REL = "tools/official_client_capture/candidate_test_fact_map_9_9_9.json"
TRACE_REL = rc.TRACE_FILE
CONSERVATION_REL = rc.CONSERVATION_FILE
BASELINE_REL = f"{rc.BASELINE_DIR}/codex_identity_conservation_baseline_9_9_9.json"
SOURCE_REL = "backend/a.go"
TEST_FILE_REL = "backend/a_test.go"
FIRST_RECEIPT = f"{rc.RECEIPT_DIR}/upstream-init-freeze-successor.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 合成仓库里的桩文件
# ---------------------------------------------------------------------------

TRACE_STUB = '''"""候选追踪脚本桩。"""

DEFAULT_MAPPING_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_test_fact_map_9_9_9.json"
)
# 历史注释
FROZEN_MAPPING_SHA256 = (
    "{frozen}"
)
'''

# 与真实 test_candidate_test_trace 同构：常量等于映射文件摘要，映射里每个被钉住文件的摘要等于现状。
TEST_TRACE_STUB = '''import hashlib, json, re, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class FrozenInputs(unittest.TestCase):
    def test_frozen_input_digests_match_checked_in_assets(self):
        text = (ROOT / "tools/official_client_capture/candidate_test_trace.py").read_text(encoding="utf-8")
        frozen = re.search(r'FROZEN_MAPPING_SHA256 = \\(\\n    "([0-9a-f]{64})"\\n\\)', text).group(1)
        mapping_path = ROOT / "tools/official_client_capture/candidate_test_fact_map_9_9_9.json"
        self.assertEqual(frozen, hashlib.sha256(mapping_path.read_bytes()).hexdigest())
        for test in json.loads(mapping_path.read_text(encoding="utf-8"))["tests"]:
            files = [{"path": test["test_file"], "sha256": test["test_file_sha256"]}, *test["source_files"]]
            for item in files:
                self.assertEqual(item["sha256"], hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest(), item["path"])
'''

TEST_TRACE_TEST_STUB = '''import unittest


class Script(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
'''

CONSERVATION_STUB = '''"""身份守恒测试桩：基线 = 受管文件（不含 tests）逐文件摘要的快照，和真实模块一样提供 --seal-baseline 入口。"""
import argparse, hashlib, json, os, subprocess, sys, unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
BASELINE_CODEX_VERSION = "9.9.9"
BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "codex_identity_conservation_baseline_9_9_9.json"
BASELINE_SHA256 = "{baseline_sha}"
# 说明性注释：重封时清空。
REGISTERED_CONTROL_CHANGES: dict[str, str] = {{
    "stale_entry.py": "旧的登记项，重封时应被清掉",
}}


def snapshot():
    entries = []
    for path in sorted(TOOL_ROOT.rglob("*")):
        relative = path.relative_to(TOOL_ROOT)
        if path.is_file() and path.suffix in (".py", ".json", ".sh") and "tests" not in relative.parts and "__pycache__" not in relative.parts:
            entries.append({{"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}})
    files = hashlib.sha256(json.dumps(entries).encode()).hexdigest()
    return {{"files_sha256": files, "evidence_semantics_sha256": files[::-1], "wire_producer_sha256": "w" * 64, "entries": entries}}


class Conservation(unittest.TestCase):
    def test_baseline_file_is_pinned(self):
        self.assertEqual(hashlib.sha256(BASELINE_PATH.read_bytes()).hexdigest(), BASELINE_SHA256)

    def test_snapshot_agrees_with_baseline(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(snapshot()["files_sha256"], baseline["files_sha256"])


def _seal_main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal-baseline", required=True, type=Path)
    parser.add_argument("--replace", action="store_true")
    arguments = parser.parse_args(argv)
    if os.environ.get("STUB_SEAL_FAIL"):
        print("封存失败：桩按要求失败", file=sys.stderr)
        return 2
    output = arguments.seal_baseline
    if output.exists() and not arguments.replace:
        print("封存失败：输出文件已存在", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    document = {{"schema_version": "stub/v1", "source_commit": commit, "policy_sha256": "p" * 64, **snapshot()}}
    raw = (json.dumps(document, ensure_ascii=False, indent=1) + "\\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print(f"已封存 Codex 身份守恒基线：{{output}}")
    print(f"  source_commit = {{commit}}")
    print(f"  受管文件 {{len(document['entries'])}} 个：wire 0／evidence {{len(document['entries'])}}／control 0／忽略 0／默认归层 {{os.environ.get('STUB_DEFAULTED', '0')}}")
    print(f"  文件摘要（填入 BASELINE_SHA256）= {{hashlib.sha256(raw).hexdigest()}}")
    return 0


if __name__ == "__main__":
    if "--seal-baseline" in sys.argv[1:]:
        raise SystemExit(_seal_main(sys.argv[1:]))
    unittest.main()
'''

UPSTREAM_MERGE_STUB = '''import argparse, json, os, subprocess, sys

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
gen = sub.add_parser("freeze-successor-generate")
for name in ("--repository", "--before", "--after", "--tag", "--output", "--reason", "--timing-ledger"):
    gen.add_argument(name)
gen.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
if args.before == args.after:
    print("上游合并工具拒绝：freeze before/after 不得相同", file=sys.stderr)
    sys.exit(1)
for commit in (args.before, args.after):
    probe = subprocess.run(["git", "-C", args.repository, "cat-file", "-e", commit + "^{commit}"], capture_output=True)
    if probe.returncode:
        print("桩：提交不存在 " + commit, file=sys.stderr)
        sys.exit(1)
changed = subprocess.run(["git", "-C", args.repository, "diff", "--name-only", args.before, args.after], capture_output=True, text=True, check=True).stdout.split()
# 桩的"冻结路径"：受管工具目录里的 .py 文件；json 算台账外未登记。
frozen = [p for p in changed if p.startswith("tools/official_client_capture/") and p.endswith(".py")]
unregistered = [p for p in changed if p not in frozen and p.startswith("tools/official_client_capture/")]
manual = [{"action_kind": "manual_required", "path": frozen[0]}] if os.environ.get("STUB_MANUAL") and frozen else []
document = {
    "base_commit": args.before, "current_commit": args.after, "changed_path_count": len(changed), "frozen_path_count": 10,
    "transitions": [{"old_path": "", "path": p, "predecessor_sha256s": ["a" * 64], "reason": args.reason} for p in frozen],
    "unregistered_paths": unregistered, "unregistered_path_count": len(unregistered), "required_manual_actions": manual,
    "result": "manual_actions_required" if manual else "passed_local_evidence_successor",
    "identity_sha256": "i" * 64, "verification": ["make check-egress-spec-ci"], "scope": "upstream-" + args.tag + "-freeze-successor",
}
if args.dry_run:
    document["dry_run"] = True
    print(json.dumps(document, ensure_ascii=False))
else:
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\\n")
    print(json.dumps({"result": document["result"], "output": args.output}))
'''

INSTALL_STUB = '''import argparse, hashlib, json, sys
from pathlib import Path

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
build = sub.add_parser("build-manifest")
build.add_argument("--root", required=True)
build.add_argument("--check", action="store_true")
args = parser.parse_args()
root = Path(args.root).resolve()
files = []
for path in sorted(root.rglob("*")):
    if path.is_file() and path.name != "manifest.json":
        data = path.read_bytes()
        files.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "mode": "0600", "bytes": len(data)})
manifest = {"schema_version": "stub/v1", "file_count": len(files), "files": files}
manifest["manifest_sha256"] = hashlib.sha256(json.dumps(files).encode()).hexdigest()
target = root / "manifest.json"
if args.check:
    if not target.exists() or json.loads(target.read_text(encoding="utf-8")) != manifest:
        print("驱动清单／安装失败：manifest.json 与目录内容不一致，请重新 build-manifest。", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"status": "unchanged"}))
else:
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"status": "written"}))
'''

TRACE_TOOL_STUB = '''import argparse, json, os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--repo")
parser.add_argument("--out")
parser.add_argument("--jobs")
args = parser.parse_args()
counter = os.environ.get("STUB_TRACE_COUNT")
if counter:
    with open(counter, "a", encoding="utf-8") as handle:
        handle.write("traced\\n")
for module in ("mod_a", "mod_b"):
    folder = Path(args.out) / module
    folder.mkdir(parents=True, exist_ok=True)
    code = int(os.environ.get("STUB_TRACE_RC_" + module, "0"))
    (folder / "_result.json").write_text(json.dumps({"module": module, "rc": code, "seconds": 0.1, "tail": "boom" if code else ""}), encoding="utf-8")
    (folder / "1.json").write_text(json.dumps({"opened": ["tools/official_client_capture/candidate_test_trace.py"], "listed": [], "spawned": []}), encoding="utf-8")
'''

OWNERSHIP_STUB = '''import argparse, json, subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
table = sub.add_parser("dependency-table")
for name in ("--tree", "--trace-dir", "--python", "--platform", "--output"):
    table.add_argument(name)
args = parser.parse_args()
commit = subprocess.run(["git", "-C", args.tree, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
files = ["tools/official_client_capture/candidate_test_trace.py"]
modules, unobserved = {}, {}
for folder in sorted(Path(args.trace_dir).iterdir()):
    result = json.loads((folder / "_result.json").read_text(encoding="utf-8"))
    if result["rc"] != 0:
        unobserved[folder.name] = "追踪运行退出码 " + str(result["rc"])
    else:
        modules[folder.name] = {"opened": [0], "listed": []}
document = {"schema_version": "unit-dependencies/v1", "model": "stub", "generated_at_commit": commit,
            "tracer": {"python": args.python, "platform": args.platform}, "files": files, "modules": modules, "unobserved": unobserved}
Path(args.output).write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\\n", encoding="utf-8")
'''


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def build_repo(root: Path) -> Path:
    """在 root 下建合成仓库：一次初始提交，里面有桩文件、一份初始收据和一份旧依赖表。"""

    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "work", str(root)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        git(root, "config", key, value)
    write(root, SOURCE_REL, "package backend\n")
    write(root, TEST_FILE_REL, "package backend\n")
    mapping = {
        "schema_version": "stub/v1", "codex_version": "9.9.9",
        "tests": [{
            "package": "./backend", "name": "TestA", "test_file": TEST_FILE_REL,
            "test_file_sha256": sha((root / TEST_FILE_REL).read_bytes()),
            "source_files": [{"path": SOURCE_REL, "sha256": sha((root / SOURCE_REL).read_bytes())}], "facts": [],
        }],
    }
    raw = (json.dumps(mapping, ensure_ascii=False, indent=2) + "\n").encode()
    (root / MAP_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / MAP_REL).write_bytes(raw)
    write(root, TRACE_REL, TRACE_STUB.format(frozen=sha(raw)))
    write(root, "tools/official_client_capture/__init__.py", "")
    write(root, "tools/official_client_capture/tests/__init__.py", "")
    write(root, "tools/official_client_capture/tests/test_candidate_test_trace.py", TEST_TRACE_STUB)
    write(root, "tools/official_client_capture/tests/test_candidate_trace_test.py", TEST_TRACE_TEST_STUB)
    write(root, CONSERVATION_REL, CONSERVATION_STUB.format(baseline_sha="0" * 64))
    write(root, "tools/upstream_merge/__init__.py", "")
    write(root, "tools/upstream_merge/__main__.py", UPSTREAM_MERGE_STUB)
    write(root, rc.DRIVER_INSTALL, INSTALL_STUB)
    write(root, f"{rc.DRIVER_ROOT}/driver/run.sh", "echo driver\n")
    write(root, rc.DEPENDENCY_TRACE, TRACE_TOOL_STUB)
    write(root, rc.OWNERSHIP, OWNERSHIP_STUB)
    write(root, FIRST_RECEIPT, json.dumps({"result": "passed_local_evidence_successor"}) + "\n")
    old_table = {"schema_version": "unit-dependencies/v1", "model": "stub", "generated_at_commit": "0" * 40,
                 "tracer": {"python": "3.0.0", "platform": "桩平台"}, "files": ["tools/official_client_capture/candidate_test_trace.py"],
                 "modules": {"mod_a": {"opened": [0], "listed": []}, "mod_b": {"opened": [0], "listed": []}}, "unobserved": {}}
    write(root, rc.DEPENDENCY_TABLE, json.dumps(old_table, ensure_ascii=False, indent=1) + "\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    # 先用桩的封存入口造出初始基线，并回填摘要，再做初始提交的修订，保证初始状态下桩测试全绿、驱动 manifest 一致。
    subprocess.run(["python3", "-B", "-m", rc.CONSERVATION_MODULE, "--seal-baseline", BASELINE_REL],
                   cwd=root, check=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    digest = sha((root / BASELINE_REL).read_bytes())
    path = root / CONSERVATION_REL
    path.write_text(path.read_text(encoding="utf-8").replace("0" * 64, digest), encoding="utf-8")
    subprocess.run(["python3", "-B", rc.DRIVER_INSTALL, "build-manifest", "--root", rc.DRIVER_ROOT], cwd=root, check=True, capture_output=True,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    git(root, "add", "-A")
    git(root, "commit", "-q", "--amend", "-m", "init")
    return root


def change_source(root: Path) -> str:
    """构造"事实映射需要重绑"的场景：改被映射钉住的源码并提交。返回该提交号。"""

    with open(root / SOURCE_REL, "a", encoding="utf-8") as handle:
        handle.write("// 无害改动\n")
    git(root, "add", SOURCE_REL)
    git(root, "commit", "-q", "-m", "源码改动")
    return git(root, "rev-parse", "HEAD")


class ChainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.repo = build_repo(base / "repo")
        self.logs = base / "logs"
        self.counter = base / "trace-count.txt"
        patcher = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "STUB_TRACE_COUNT": str(self.counter)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_chain(self, *extra: str, log_name: str = "run", acknowledge: bool = True) -> tuple[int, str]:
        arguments = ["--repo", str(self.repo), "--log-dir", str(self.logs / log_name), "--no-heavy-lock", "--poll-seconds", "0.1",
                     "--label", "t", "--reason", "测试原因", "--date", "2026-10-31", "--python-version", "3.14.0", "--platform", "测试平台"]
        if acknowledge:
            arguments.append("--ack-no-campaign")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = rc.main(arguments + list(extra))
        return code, output.getvalue()

    def subjects(self, since: str) -> list[str]:
        return git(self.repo, "log", "--format=%s", f"{since}..HEAD").splitlines()[::-1]

    def state(self, log_name: str = "run") -> dict:
        return json.loads((self.logs / log_name / rc.STATE_NAME).read_text(encoding="utf-8"))

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD")

    def clean(self) -> bool:
        return git(self.repo, "status", "--porcelain", "--untracked-files=all") == ""


class DryRunAndPreflightTests(ChainTestCase):
    def test_dry_run_prints_plan_and_changes_nothing(self) -> None:
        change_source(self.repo)
        head = self.head()
        code, output = self.run_chain("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.head(), head)
        self.assertTrue(self.clean())
        self.assertIn("重封链计划", output)
        self.assertIn("需要重绑 1 处", output)
        self.assertIn("dry-run 结束", output)
        self.assertFalse((self.logs / "run" / rc.STATE_NAME).exists(), "dry-run 不写状态")
        self.assertEqual(list((self.logs / "run").glob("audit-*.txt")), [], "dry-run 不落盘审核报告")

    def test_dirty_tree_blocks_everything(self) -> None:
        change_source(self.repo)
        (self.repo / "stray.txt").write_text("x", encoding="utf-8")
        head = self.head()
        code, output = self.run_chain()
        self.assertEqual(code, 2, output)
        self.assertIn("工作树不干净", output)
        self.assertEqual(self.head(), head)
        # dry-run 不走"每步开始前再查一次"的那道防线，只靠前置检查拦住脏树。
        code, output = self.run_chain("--dry-run", log_name="run2")
        self.assertEqual(code, 2, output)
        self.assertIn("前置检查未通过", output)
        self.assertIn("工作树不干净", output)

    def test_main_branch_is_refused_unless_allowed(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "main")
        code, output = self.run_chain("--dry-run")
        self.assertEqual(code, 2, output)
        self.assertIn("不直接在主干上做", output)
        code, output = self.run_chain("--dry-run", "--allow-main", log_name="run2")
        self.assertEqual(code, 0, output)

    def test_real_run_requires_campaign_acknowledgement(self) -> None:
        change_source(self.repo)
        head = self.head()
        code, output = self.run_chain(acknowledge=False)
        self.assertEqual(code, 2, output)
        self.assertIn("--ack-no-campaign", output)
        self.assertEqual(self.head(), head)

    def test_detached_head_and_mid_merge_are_refused(self) -> None:
        git(self.repo, "checkout", "-q", "--detach")
        code, output = self.run_chain("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("游离", output)
        git(self.repo, "checkout", "-q", "work")
        (Path(git(self.repo, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").write_text(self.head() + "\n", encoding="utf-8")
        code, output = self.run_chain("--dry-run", log_name="run2")
        self.assertEqual(code, 2)
        self.assertIn("合并", output)

    def test_pycache_in_tools_is_refused(self) -> None:
        (self.repo / "tools" / "__pycache__").mkdir()
        (self.repo / "tools" / "__pycache__" / "x.pyc").write_bytes(b"")
        code, output = self.run_chain("--dry-run")
        self.assertEqual(code, 2, output)
        self.assertIn("__pycache__", output)

    def test_log_dir_inside_repo_is_refused(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = rc.main(["--repo", str(self.repo), "--log-dir", str(self.repo / "logs"), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("不能放在工作树里面", output.getvalue())
        self.assertTrue(self.clean())

    def test_unknown_step_and_conflicting_selectors_are_usage_errors(self) -> None:
        code, output = self.run_chain("--from", "nope", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("未知步骤", output)
        code, output = self.run_chain("--from", "reseal", "--only", "trace", "--dry-run", log_name="run2")
        self.assertEqual(code, 2)
        self.assertIn("不能同时用", output)


class FullChainTests(ChainTestCase):
    def test_full_chain_makes_ordered_fixed_format_commits(self) -> None:
        base = change_source(self.repo)
        code, output = self.run_chain("--co-author", "测试者 <t@example.invalid>")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.clean())
        subjects = self.subjects(base)
        self.assertEqual(len(subjects), 5, subjects)
        self.assertRegex(subjects[0], r"^chore\(codex-99\): 事实映射重绑 1 个 sha256 叶子（a\.go）$")
        self.assertEqual(subjects[1], "chore(egress): 登记事实映射重绑的冻结承接收据（t）")
        self.assertRegex(subjects[2], r"^chore\(codex-99\): 重封 9\.9\.9 身份守恒基线（t）$")
        self.assertEqual(subjects[3], "chore(egress): 登记身份守恒基线重封的冻结承接收据（t）")
        self.assertRegex(subjects[4], r"^chore\(ci\): 重新追踪依赖表（追踪提交 [0-9a-f]{10}，2 个模块，未观察到 0 个）$")
        body = git(self.repo, "log", "-1", "--format=%B", "HEAD~4")
        self.assertIn("测试原因", body)
        self.assertIn("未经自动签字", body)
        self.assertTrue(body.rstrip().endswith("Co-Authored-By: 测试者 <t@example.invalid>"))
        # 每个收据提交只含收据文件（单独提交）。
        for offset in (3, 1):
            files = git(self.repo, "show", "--name-only", "--format=", f"HEAD~{offset}").split()
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].startswith(rc.RECEIPT_DIR) and files[0].endswith("-freeze-successor.json"), files)
        # 重绑提交只含映射与追踪脚本；重封提交只含基线与守恒测试；追踪提交只含依赖表。
        self.assertEqual(sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD~4").split()), sorted([MAP_REL, TRACE_REL]))
        self.assertEqual(sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD~2").split()), sorted([BASELINE_REL, CONSERVATION_REL]))
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), [rc.DEPENDENCY_TABLE])

    def test_chain_turns_the_pinned_tests_green_and_table_points_at_ancestor(self) -> None:
        base = change_source(self.repo)
        red = subprocess.run(["python3", "-B", "-m", "unittest", "tools.official_client_capture.tests.test_candidate_test_trace"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(red.returncode, 0, "场景构造：源码改动后钉住摘要的测试必须是红的")
        code, output = self.run_chain()
        self.assertEqual(code, 0, output)
        for module in rc.VERIFY_MODULES:
            result = subprocess.run(["python3", "-B", "-m", "unittest", module], cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, module + result.stderr)
        table = json.loads((self.repo / rc.DEPENDENCY_TABLE).read_text(encoding="utf-8"))
        traced = table["generated_at_commit"]
        self.assertEqual(traced, git(self.repo, "rev-parse", "HEAD~1"), "追踪提交是追踪表提交的上一个提交")
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", traced, "HEAD"]).returncode, 0)
        self.assertTrue(traced != base)
        self.assertEqual(table["tracer"], {"python": "3.14.0", "platform": "测试平台"})
        self.assertEqual(self.state()["steps"]["trace"]["status"], "done")

    def test_audit_reports_are_written_and_describe_payload(self) -> None:
        change_source(self.repo)
        code, output = self.run_chain()
        self.assertEqual(code, 0, output)
        reports = sorted(path.name for path in (self.logs / "run").glob("audit-*.txt"))
        self.assertEqual(reports, ["audit-01-rebind.txt", "audit-02-rebind-receipt.txt", "audit-03-reseal.txt", "audit-04-reseal-receipt.txt",
                                   "audit-05-manifest.txt", "audit-06-trace.txt", "audit-07-verify.txt"])
        rebind = (self.logs / "run" / "audit-01-rebind.txt").read_text(encoding="utf-8")
        self.assertIn("tests[0].source_files[0].sha256", rebind)
        self.assertIn("非快照叶子变化：0 处", rebind)
        self.assertIn("不签字、不推送", rebind)
        reseal = (self.logs / "run" / "audit-03-reseal.txt").read_text(encoding="utf-8")
        self.assertIn("修改 candidate_test_fact_map_9_9_9.json", reseal)
        self.assertIn("修改 candidate_test_trace.py", reseal)
        self.assertIn("已清空，原有 1 项登记被移除", reseal)
        self.assertIn("本工具没有签字、没有推送", output)

    def test_registered_control_changes_is_cleared_and_constants_backfilled(self) -> None:
        change_source(self.repo)
        code, output = self.run_chain("--only", "rebind")
        self.assertEqual(code, 0, output)
        code, output = self.run_chain("--only", "rebind-receipt")
        self.assertEqual(code, 0, output)
        code, output = self.run_chain("--only", "reseal")
        self.assertEqual(code, 0, output)
        text = (self.repo / CONSERVATION_REL).read_text(encoding="utf-8")
        self.assertIn("REGISTERED_CONTROL_CHANGES: dict[str, str] = {}\n", text)
        self.assertNotIn("stale_entry.py", text)
        digest = sha((self.repo / BASELINE_REL).read_bytes())
        self.assertIn(f'BASELINE_SHA256 = "{digest}"', text)
        self.assertIn("# 说明性注释：重封时清空。", text, "常量以外的内容不动")

    def test_steps_with_nothing_to_do_are_noops_and_chain_still_commits_trace(self) -> None:
        base = self.head()
        code, output = self.run_chain()
        self.assertEqual(code, 0, output)
        steps = self.state()["steps"]
        for name in ("rebind", "rebind-receipt", "reseal", "reseal-receipt", "manifest"):
            self.assertEqual(steps[name]["status"], "noop", name)
        self.assertEqual(len(self.subjects(base)), 1, "只剩追踪表提交")

    def test_manifest_is_rebuilt_only_when_driver_files_changed(self) -> None:
        write(self.repo, f"{rc.DRIVER_ROOT}/driver/run.sh", "echo changed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "改驱动脚本")
        base = self.head()
        code, output = self.run_chain("--only", "manifest")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.subjects(base), ["chore(driver): 重建驱动 manifest（run.sh）"])
        audit = (self.logs / "run" / "audit-05-manifest.txt").read_text(encoding="utf-8")
        self.assertIn("修改 driver/run.sh", audit)
        code, output = self.run_chain("--only", "manifest", log_name="run2")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.state("run2")["steps"]["manifest"]["status"], "noop")

    def test_rebind_refuses_when_frozen_constant_is_out_of_sync(self) -> None:
        change_source(self.repo)
        text = (self.repo / TRACE_REL).read_text(encoding="utf-8")
        write(self.repo, TRACE_REL, re.sub(r'(FROZEN_MAPPING_SHA256 = \(\n    ")[0-9a-f]{64}', r"\g<1>" + "1" * 64, text))
        git(self.repo, "commit", "-q", "-am", "常量被改乱")
        head = self.head()
        code, output = self.run_chain("--only", "rebind")
        self.assertEqual(code, 1, output)
        self.assertIn("映射被改过却没同步常量", output)
        self.assertEqual(self.head(), head)
        self.assertTrue(self.clean())

    def test_receipt_with_manual_actions_is_a_failure(self) -> None:
        change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        with mock.patch.dict(os.environ, {"STUB_MANUAL": "1"}):
            code, output = self.run_chain("--only", "rebind-receipt")
        self.assertEqual(code, 1, output)
        self.assertIn("人工动作", output)
        self.assertTrue(self.clean())


class FailureAndResumeTests(ChainTestCase):
    def test_failure_stops_in_place_restores_tree_and_resume_with_from_succeeds(self) -> None:
        base = change_source(self.repo)
        bogus = "f" * 40
        code, output = self.run_chain("--receipt-before", bogus)
        self.assertEqual(code, 1, output)
        self.assertIn("✗ 步骤 rebind-receipt 失败", output)
        self.assertIn("--from rebind-receipt", output)
        self.assertTrue(self.clean(), "失败后工作树必须还原干净")
        self.assertEqual(len(self.subjects(base)), 1, "rebind 的提交保留，后面的什么都没做")
        state = self.state()["steps"]
        self.assertEqual(state["rebind"]["status"], "done")
        self.assertEqual(state["rebind-receipt"]["status"], "failed")
        self.assertTrue((self.logs / "run" / "failed-rebind-receipt.diff").is_file())
        self.assertIn("rc=", (self.logs / "run" / rc.COMMAND_LOG).read_text(encoding="utf-8"))
        # 续跑：不带坏参数，从失败的那一步往后。
        code, output = self.run_chain("--from", "rebind-receipt")
        self.assertEqual(code, 0, output)
        subjects = self.subjects(base)
        self.assertEqual(len(subjects), 5, subjects)
        self.assertEqual(sum(1 for item in subjects if "事实映射重绑 1 个" in item), 1, "rebind 没有被重复提交")
        self.assertEqual(self.state()["steps"]["rebind-receipt"]["status"], "done")

    def test_failure_inside_a_step_discards_partial_edits_and_keeps_a_diff(self) -> None:
        change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        self.assertEqual(self.run_chain("--only", "rebind-receipt")[0], 0)
        head = self.head()
        with mock.patch.dict(os.environ, {"STUB_SEAL_FAIL": "1"}):
            code, output = self.run_chain("--only", "reseal")
        self.assertEqual(code, 1, output)
        self.assertIn("封存失败", output)
        self.assertEqual(self.head(), head)
        self.assertTrue(self.clean())
        self.assertTrue((self.logs / "run" / "failed-reseal.diff").is_file())
        code, output = self.run_chain("--only", "reseal")
        self.assertEqual(code, 0, output)

    def test_default_layer_count_nonzero_fails_reseal(self) -> None:
        change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        self.assertEqual(self.run_chain("--only", "rebind-receipt")[0], 0)
        with mock.patch.dict(os.environ, {"STUB_DEFAULTED": "3"}):
            code, output = self.run_chain("--only", "reseal")
        self.assertEqual(code, 1, output)
        self.assertIn("默认归层不为 0", output)
        self.assertTrue(self.clean())

    def test_from_refuses_when_head_moved_unless_accepted(self) -> None:
        base = change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        write(self.repo, "notes.txt", "手工提交\n")
        git(self.repo, "add", "notes.txt")
        git(self.repo, "commit", "-q", "-m", "手工提交")
        code, output = self.run_chain("--from", "rebind-receipt")
        self.assertEqual(code, 2, output)
        self.assertIn("--accept-head", output)
        code, output = self.run_chain("--from", "rebind-receipt", "--accept-head")
        self.assertEqual(code, 0, output)
        self.assertGreater(len(self.subjects(base)), 3)

    def test_from_refuses_unrelated_head(self) -> None:
        base = change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        git(self.repo, "reset", "-q", "--hard", base)
        code, output = self.run_chain("--from", "rebind-receipt")
        self.assertEqual(code, 2, output)
        self.assertIn("对不上", output)

    def test_fresh_run_refuses_a_used_log_dir(self) -> None:
        change_source(self.repo)
        self.assertEqual(self.run_chain("--only", "rebind")[0], 0)
        code, output = self.run_chain()
        self.assertEqual(code, 2, output)
        self.assertIn("续跑用 --from", output)

    def test_only_runs_exactly_one_step(self) -> None:
        base = change_source(self.repo)
        code, output = self.run_chain("--only", "rebind")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.subjects(base)), 1)
        self.assertEqual(list(self.state()["steps"]), ["rebind"])

    def test_resume_command_drops_selectors_and_dry_run(self) -> None:
        options = rc.build_parser().parse_args(["--log-dir", "/x", "--from", "reseal", "--dry-run", "--label", "a b"])
        chain = rc.Chain(options, ["--log-dir", "/x", "--from", "reseal", "--dry-run", "--label", "a b"])
        command = chain.resume_command("trace")
        self.assertIn("--log-dir /x", command)
        self.assertIn("--label 'a b'", command)
        self.assertTrue(command.endswith("--from trace"))
        self.assertNotIn("--dry-run", command)
        self.assertNotIn("--from reseal", command)

    def test_concurrent_use_of_one_log_dir_is_refused(self) -> None:
        import fcntl

        (self.logs / "run").mkdir(parents=True)
        with open(self.logs / "run" / "chain.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, output = self.run_chain("--dry-run")
        self.assertEqual(code, 2, output)
        self.assertIn("另一个重封链进程", output)


class TraceStepTests(ChainTestCase):
    def prepare(self) -> str:
        return self.head()

    def test_finished_trace_job_for_same_head_is_reused(self) -> None:
        self.assertEqual(self.run_chain("--only", "trace")[0], 0)
        self.assertEqual(self.counter.read_text(encoding="utf-8").count("traced"), 1)
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")        # 回到追踪前，模拟"表提交被丢弃后重做"
        code, output = self.run_chain("--only", "trace", log_name="run")
        self.assertEqual(code, 0, output)
        self.assertIn("已经追踪完成", output)
        self.assertEqual(self.counter.read_text(encoding="utf-8").count("traced"), 1, "同一提交不重追")

    def test_new_unobserved_module_fails_then_allowed_resume_reuses_results(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_TRACE_RC_mod_b": "1"}):
            head = self.head()
            code, output = self.run_chain("--only", "trace")
            self.assertEqual(code, 1, output)
            self.assertIn("新增 1 个 unobserved", output)
            self.assertIn("mod_b", output)
            self.assertEqual(self.head(), head)
            self.assertTrue(self.clean())
            code, output = self.run_chain("--only", "trace", "--allow-new-unobserved")
            self.assertEqual(code, 0, output)
        table = json.loads((self.repo / rc.DEPENDENCY_TABLE).read_text(encoding="utf-8"))
        self.assertEqual(list(table["unobserved"]), ["mod_b"])
        self.assertEqual(self.counter.read_text(encoding="utf-8").count("traced"), 1, "放行后复用上次追踪结果")

    def test_trace_worktree_is_removed_afterwards(self) -> None:
        self.assertEqual(self.run_chain("--only", "trace")[0], 0)
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0, "只剩主工作树")
        self.assertFalse((self.logs / "run" / "trace" / "tree").exists())

    def test_trace_without_lock_script_fails_before_any_work(self) -> None:
        arguments = ["--repo", str(self.repo), "--log-dir", str(self.logs / "x"), "--ack-no-campaign", "--only", "trace", "--heavy-run", "/nonexistent/heavy-run.sh"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = rc.main(arguments)
        self.assertEqual(code, 2, output.getvalue())
        self.assertIn("heavy-run.sh", output.getvalue())
        self.assertFalse(self.counter.exists())

    def test_trace_runs_under_heavy_run_wrapper_when_given(self) -> None:
        wrapper = self.logs.parent / "heavy-run.sh"
        wrapper.write_text('#!/bin/bash\necho "$@" > "$(dirname "$0")/wrapper-args.txt"\nexec "$@"\n', encoding="utf-8")
        arguments = ["--repo", str(self.repo), "--log-dir", str(self.logs / "x"), "--ack-no-campaign", "--only", "trace", "--heavy-run", str(wrapper),
                     "--poll-seconds", "0.1", "--python-version", "3.14.0", "--platform", "p"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = rc.main(arguments)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("dependency_trace.py", (self.logs.parent / "wrapper-args.txt").read_text(encoding="utf-8"))


class HelperTests(unittest.TestCase):
    def test_detach_runs_in_its_own_session_and_writes_exit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "job.log"
            command = ["python3", "-B", "-c", "import os; print('sid', os.getsid(0)); raise SystemExit(3)"]
            pid = rc.detach(command, log, cwd=Path(directory), env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            deadline = time.time() + 30
            while rc.exit_marker(log) is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(rc.exit_marker(log), 3)
            session = int(re.search(r"^sid (\d+)$", log.read_text(encoding="utf-8"), flags=re.M).group(1))
            self.assertNotEqual(session, os.getsid(0), "任务在自己的新会话里，不随本进程的会话结束")
            self.assertGreater(pid, 1)

    def test_exit_marker_needs_a_complete_exit_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "x.log"
            self.assertIsNone(rc.exit_marker(log))
            log.write_text("EXIT=0 但这行不是结尾\n正在跑\n", encoding="utf-8")
            self.assertIsNone(rc.exit_marker(log))
            log.write_text("a\nEXIT=0\nb\nEXIT=2\n", encoding="utf-8")
            self.assertEqual(rc.exit_marker(log), 2)

    def test_plan_rebind_rejects_non_snapshot_leaf_and_non_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "f.go").write_text("x", encoding="utf-8")
            document = {"tests": [{"test_file": "f.go", "test_file_sha256": "0" * 64, "source_files": [{"path": "f.go", "sha256": "1" * 64}]}]}
            (root / "m.json").write_bytes(rc.canonical_json(document))
            plan = rc.plan_rebind(root, "m.json")
            self.assertEqual(len(plan.changes), 2)
            self.assertEqual(plan.non_snapshot, [])
            self.assertTrue(plan.structure_same)
            self.assertEqual({item.new for item in plan.changes}, {sha(b"x")})
            (root / "m.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(rc.StepFailed):
                rc.plan_rebind(root, "m.json")
            document["tests"][0]["source_files"][0]["path"] = "missing.go"
            (root / "m.json").write_bytes(rc.canonical_json(document))
            self.assertEqual(rc.plan_rebind(root, "m.json").missing, ["tests[0].source_files[0].sha256 -> missing.go"])

    def test_diff_baselines_warns_when_identity_digests_change(self) -> None:
        old = {"source_commit": "a" * 40, "files_sha256": "1" * 64, "wire_producer_sha256": "2" * 64, "evidence_semantics_sha256": "3" * 64,
               "policy_sha256": "p" * 64, "orchestrator_closure_sha256": {"wire_producer": "4" * 64}, "layer_counts": {"control": 1},
               "closure_symbols": {"wire_producer": {"functions": ["f"]}}, "defaulted_paths": [], "entries": [{"path": "a.py", "sha256": "5" * 64}]}
        new = json.loads(json.dumps(old))
        new["files_sha256"] = "9" * 64
        new["evidence_semantics_sha256"] = "8" * 64
        new["entries"] = [{"path": "a.py", "sha256": "6" * 64}, {"path": "b.py", "sha256": "7" * 64}]
        lines, warnings = rc.diff_baselines(old, new, {"a.py": "evidence_semantics"})
        text = "\n".join(lines)
        self.assertIn("变化 files_sha256", text)
        self.assertIn("修改 a.py [evidence_semantics]", text)
        self.assertIn("新增 b.py [层未知]", text)
        self.assertEqual(warnings, [], "只有证据层与文件清单摘要变化时不报警")
        new["wire_producer_sha256"] = "a" * 64
        new["closure_symbols"]["wire_producer"]["functions"] = ["f", "g"]
        lines, warnings = rc.diff_baselines(old, new, {})
        self.assertTrue(any("wire_producer_sha256" in item for item in warnings))
        self.assertIn("新增 ['g']", "\n".join(lines))

    def test_table_diff_reports_new_and_recovered_unobserved(self) -> None:
        base = {"generated_at_commit": "a" * 40, "tracer": {}, "files": ["x"], "modules": {"m1": {"opened": [0], "listed": []}}, "unobserved": {"u1": "r"}}
        fresh = {"generated_at_commit": "b" * 40, "tracer": {}, "files": ["x", "y"], "modules": {"m1": {"opened": [0, 1], "listed": []}}, "unobserved": {"u2": "boom"}}
        lines, new_unobserved = rc.table_diff(base, fresh)
        self.assertEqual(new_unobserved, ["u2"])
        text = "\n".join(lines)
        self.assertIn("依赖集合变化 m1: opened +1/-0", text)
        self.assertIn("恢复 ['u1']", text)

    def test_rebind_comment_is_stable_and_names_digests(self) -> None:
        plan = rc.RebindPlan(10, [rc.Change("l", "backend/x.go", "a" * 64, "b" * 64)], [], [], True, b"old", b"new")
        text = rc.rebind_comment("2026-10-31", "原因", plan)
        self.assertEqual(text.count("\n"), 3)
        self.assertIn("x.go 1", text)
        self.assertIn("aaaaaaaa… 变为 bbbbbbbb…", text)
        self.assertTrue(all(line.startswith("# ") for line in text.splitlines()))

    def test_scope_from_version(self) -> None:
        self.assertEqual(rc.Chain.scope("0.165.0"), "codex-0165")
        self.assertEqual(rc.Chain.scope("9.9.9"), "codex-99")

    def test_commit_refuses_files_outside_the_step(self) -> None:
        """每个提交只含本步的文件：暂存区里混进别的改动时拒绝提交（收据必须单独提交的保障）。"""

        with tempfile.TemporaryDirectory() as directory:
            repo = build_repo(Path(directory).resolve() / "repo")
            options = rc.build_parser().parse_args(["--repo", str(repo), "--log-dir", str(Path(directory) / "logs")])
            chain = rc.Chain(options, [])
            (repo / "a.txt").write_text("a", encoding="utf-8")
            (repo / "b.txt").write_text("b", encoding="utf-8")
            git(repo, "add", "b.txt")
            head = git(repo, "rev-parse", "HEAD")
            with self.assertRaises(rc.StepFailed) as raised:
                chain.commit(["a.txt"], "测试提交", ["正文"])
            self.assertIn("b.txt", str(raised.exception))
            self.assertEqual(git(repo, "rev-parse", "HEAD"), head)

    def test_script_never_pushes_or_uses_nohup(self) -> None:
        """安全守则：脚本不 push、不合并、不打 tag、不用 nohup，也不含自动签字语义。"""

        import ast

        tree = ast.parse(CHAIN_SOURCE.read_text(encoding="utf-8"))
        strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for forbidden in ("push", "tag", "merge", "nohup", "remote", "rebase", "cherry-pick"):
            self.assertNotIn(forbidden, strings, f"代码里不应出现 git/shell 子命令字符串 {forbidden!r}")
        forbidden = {"push", "tag", "merge", "remote", "rebase", "cherry-pick", "pull", "fetch"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "git" and node.args:
                first = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
                self.assertNotIn(first, forbidden, ast.dump(node))
            if isinstance(node, ast.List) and len(node.elts) >= 2 and all(isinstance(item, ast.Constant) for item in node.elts[:2]) and node.elts[0].value == "git":
                self.assertNotIn(node.elts[1].value, forbidden, ast.dump(node))


if __name__ == "__main__":
    unittest.main()
