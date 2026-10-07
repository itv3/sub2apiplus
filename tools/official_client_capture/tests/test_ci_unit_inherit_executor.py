"""B5a-08 逐单元承接在真实执行器进程里走一遍（``unit_executor.py run-gates --mode unit-inherit``）。

临时 git 仓库里的小测试组与两个命令单元（一个输入是整个仓库，一个 pre-A3 示例单元不可承接）：先重新执行全集一次，把它的
可承接记录换成严格读集审计过的来源运行（合成轨迹只读一个宿主文件，记录登记它的精确快照——macOS 没有 strace，读集审计
本身由 test_ci_read_audit 覆盖）；再按执行器写出的提案组装请求、两类批准签字，跑逐单元承接：
* 来源有效的单元全部承接，不可承接的单元执行；决策收据随清单发布，清单自检与消费端重验都通过；
* 改一个测试文件提交后重新提案：只有输入声明失效（该测试单元、输入是整个仓库的命令单元）与没有有效记录的单元执行；
* 没给请求、请求读不出：整次重新执行，清单保持 re-execute 的原形状；参数组合非法时直接报用法错误。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from tools.ci import read_audit as ra
from tools.ci import unit_executor as ue
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests.test_ci_unit_records import _commit, _config, _repo, _write

fs = ui.fs
REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
RECORDS = REPO_ROOT / "tools" / "ci" / "unit_records.py"
INHERIT = REPO_ROOT / "tools" / "ci" / "unit_inherit.py"
INHERITABLE = {"test_leaf", "test_other", "test_uses_helper", "spec:check"}


class UnitInheritExecutorEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = _repo(self.base / "repo")
        self.store = self.base / "store"
        self.config = _config(self.base)
        self.control = self.base / "data" / "control"
        self.control.mkdir(parents=True)
        (self.control / "codex-e2e-supervisor-enable-20261007t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.host = self.base / "host" / "input.txt"
        self.host.parent.mkdir()
        self.host.write_text("单元实际读到的宿主文件\n", encoding="utf-8")
        self.clock = self.base / "clock" / "sample.json"
        self.clock.parent.mkdir()
        self.runs = 0
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
        self.environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)})

    # ------------------------------------------------------------------ 执行器与来源

    def _manifest(self) -> Path:
        ranges = ur.standard_ranges("pkg")
        payload = {
            "schema_version": ue.GATES_SCHEMA, "profile": "entry",
            "test_groups": [{"group_id": "capture", "start": "pkg/tests", "pattern": "test_*.py"}],
            "units": [{"unit_id": "spec:check", "argv": ["true"], "cwd": str(self.root),
                       "inputs": {"ranges": [ranges[key] for key in ur.COMMAND_RANGES], "head": True}},
                      {"unit_id": "pre-a3:scenario", "argv": ["true"], "cwd": str(self.root), "inheritable": False,
                       "not_inheritable_reason": "示例：pre-A3 暂不承接"}],
            "gates": [{"gate_id": "test-capture-tools", "test_groups": ["capture"]}, {"gate_id": "check", "units": ["spec:check"]},
                      {"gate_id": "pre-a3", "units": ["pre-a3:scenario"]}],
            "scheduling": {"example": 1},
        }
        path = self.base / "gates.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _exec(self, *extra: str, mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        self.runs += 1
        out = self.base / f"out-{self.runs}"
        command = [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(self._manifest()), "--config", str(self.config),
                   "--weights", str(self.base / "none.json"), "--durations", str(self.base / "none.json"), "--parallel", "4",
                   "--cores", "4", "--state-dir", str(self.base / "state"), "--out-dir", str(out), "--shared-caches", "off",
                   "--record-store", str(self.store), "--mode", mode, *extra]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300, env=self.environment), out

    def _run(self, *extra: str, mode: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
        completed, out = self._exec(*extra, mode=mode)
        if not (out / "summary.json").exists():
            self.fail(completed.stderr[-3000:])
        return (completed.returncode, json.loads((out / "summary.json").read_text(encoding="utf-8")),
                json.loads((out / "unit-manifest.json").read_text(encoding="utf-8")))

    def _inherit_args(self, request: Path | None = None) -> list[str]:
        args = ["--unit-inherit-client", "codex", "--unit-inherit-control", str(self.control)]
        return args + (["--unit-inherit-request", str(request)] if request is not None else [])

    @staticmethod
    def _split(manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
        return ({entry["unit_id"] for entry in manifest["units"] if entry["disposition"] == "inherited"},
                {entry["unit_id"] for entry in manifest["units"] if entry["disposition"] == "executed"})

    def _strict_source(self, manifest: dict[str, Any], run_id: str) -> None:
        """把一次重新执行全集运行里可承接的记录换成严格读集审计过的来源运行（合成轨迹只读宿主文件，记录登记它的快照）。"""

        store = ur.RecordStore(self.store)
        trace = ra.filter_stream([f'1 openat(AT_FDCWD<{self.base}>, "{self.host}", O_RDONLY) = 3<{self.host}>\n'], ["/"], strict=True)
        trace_path = self.base / f"trace-{run_id}.json"
        trace_path.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
        digest = ur.file_sha256(trace_path)
        store.put_log(trace_path, digest)
        entries = []
        for entry in manifest["units"]:
            record = json.loads(Path(entry["record_path"]).read_text(encoding="utf-8"))
            if record["inheritable"] is not True:
                continue
            inputs = ur._unique([*record["inputs"], ra.host_snapshot(str(self.host)),
                                 ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])
            body = {key: value for key, value in record.items() if key != "record_sha256"}
            body.update(run={"run_id": run_id, "mode": "re-execute", "out_dir": "/strict"}, inputs=inputs,
                        inputs_sha256=ur.entries_sha256(inputs),
                        read_audit={"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1",
                                    "undeclared_count": 0, "undeclared": [], "inputs_sha256": ur.entries_sha256(inputs),
                                    "trace": {"sha256": digest}, "repo_root": str(self.root), "data_root": None})
            strict = ur.seal_record(body)
            path = store.put_record(strict)
            entries.append({**{key: entry[key] for key in ("unit_id", "unit_type", "gates", "test_group", "passed", "spec_sha256",
                                                            "inheritable")},
                            "disposition": "executed", "record_sha256": strict["record_sha256"], "record_path": str(path),
                            "inputs_sha256": strict["inputs_sha256"], "reasons": ["严格读集审计的来源运行"]})
        source = ur.build_manifest(**{key: value for key, value in manifest.items()
                                      if key not in ("schema_version", "manifest_sha256", "run_id", "units", "planned_units", "counts",
                                                     "diagnostic", "mode")},
                                   run_id=run_id, mode="re-execute", planned_units=[entry["unit_id"] for entry in entries], units=entries,
                                   diagnostic=[], counts={"planned": len(entries), "executed": len(entries), "inherited": 0,
                                                          "inherited_tests": 0})
        self.assertEqual(ur.verify_manifest(source, store=store), [], "合成的严格来源运行本身自检通过")
        store.put_manifest(source)

    def _request(self, name: str, *, approve: bool = True) -> Path:
        """执行器只判定写提案 → 校时样本、撤销查询、两份批准 → 组装请求。"""

        proposal = self.base / f"proposal-{name}"
        completed, _out = self._exec("--decide-only", "--unit-inherit-propose", str(proposal), *self._inherit_args(), mode="unit-inherit")
        self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
        binding = json.loads((proposal / "binding.json").read_text(encoding="utf-8"))
        sources = json.loads((proposal / "sources.json").read_text(encoding="utf-8"))
        now = time.time()
        self._sample()
        clock = self.base / f"request-clock-{name}.json"
        clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0,
                                     "sampled_at_utc": fs.timestamp(now)}), encoding="utf-8")
        revocation = self.base / f"request-revocation-{name}.json"
        revocation.write_text(json.dumps(ui.query_revocation(self.store, sorted(sources["runs"]), "pre-ledger", now)), encoding="utf-8")
        approvals = []
        for role in sorted(fs.APPROVAL_ROLES):
            path = self.base / f"approval-{name}-{role}.json"
            path.write_text(json.dumps({"role": role, "status": "approved" if approve else "withdrawn", "account": "替身审核人",
                                        "scope": ui.CONTRACT, "binding_sha256": binding["binding_sha256"],
                                        "approved_at_utc": fs.timestamp(now - 3600), "expires_at_utc": fs.timestamp(now + 86400)}),
                            encoding="utf-8")
            approvals.append(path)
        request = self.base / f"request-{name}.json"
        ui.assemble_request(proposal, clock=clock, revocation=revocation, approvals=approvals, reuse_enabled=True, output=request,
                            clock_source=f"file:{self.clock}")
        return request

    def _sample(self) -> None:
        self.clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0,
                                          "sampled_at_utc": fs.timestamp(time.time())}), encoding="utf-8")

    def _green_source(self) -> None:
        rc, _summary, manifest = self._run(mode="re-execute")
        self.assertEqual(rc, 0, _summary.get("failed_units"))
        self._strict_source(manifest, "20261007t000000z-1-strict")

    # ------------------------------------------------------------------ 验收

    def test_valid_sources_are_inherited_and_a_test_change_reruns_only_affected_units(self) -> None:
        self._green_source()
        request = self._request("first")
        self._sample()
        rc, summary, manifest = self._run(*self._inherit_args(request), mode="unit-inherit")
        self.assertEqual(rc, 0, summary.get("unit_manifest"))
        self.assertEqual(manifest["mode"], "unit-inherit", summary.get("unit_inherit_decision"))
        inherited, executed = self._split(manifest)
        self.assertEqual((inherited, executed), (INHERITABLE, {"pre-a3:scenario"}))
        self.assertEqual(summary["unit_inherit_decision"]["action"], "inherit-matching-units")
        self.assertEqual([item["checkpoint"] for item in summary["unit_inherit_decision"]["checkpoints"]], list(ui.CHECKPOINTS))
        decision = ur.RecordStore(self.store).decision_path(manifest["run_id"])
        self.assertTrue(decision.is_file(), "决策收据随清单发布进记录库")
        self.assertEqual(manifest["unit_inherit_decision"]["decision_sha256"], json.loads(decision.read_text())["decision_sha256"])
        verified = subprocess.run([sys.executable, str(RECORDS), "verify", "--manifest", summary["unit_manifest"]["path"]],
                                  capture_output=True, text=True, env=self.environment)
        self.assertEqual(verified.returncode, 0, verified.stdout)
        self._sample()
        consumed = subprocess.run([sys.executable, str(INHERIT), "verify", "--manifest", summary["unit_manifest"]["path"],
                                   "--store", str(self.store), "--purpose", "e2e"], capture_output=True, text=True, env=self.environment)
        self.assertEqual(consumed.returncode, 0, consumed.stdout + consumed.stderr)
        self.assertEqual(json.loads(consumed.stdout)["result"]["inherited"], len(INHERITABLE))
        # 只改一个测试文件（注释）并提交：上下文的提交变了，按新提交重新提案并签字。
        _write(self.root, {"pkg/tests/test_leaf.py": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n# 改了\n"})
        _commit(self.root, "只改一个测试")
        stale = self._run(*self._inherit_args(request), mode="unit-inherit")[1]["unit_inherit_decision"]
        self.assertEqual(stale["action"], "reexecute-all", "旧请求绑定的是旧提交：整次重新执行")
        self.assertIn("commit", "；".join(stale["checkpoints"][0]["reasons"]))
        request = self._request("second")
        self._sample()
        rc, summary, manifest = self._run(*self._inherit_args(request), mode="unit-inherit")
        self.assertEqual(rc, 0, summary.get("unit_manifest"))
        inherited, executed = self._split(manifest)
        self.assertEqual(inherited, {"test_other", "test_uses_helper"})
        self.assertEqual(executed, {"test_leaf", "spec:check", "pre-a3:scenario"},
                         "只重跑按输入声明失效（改动的测试、输入是整个仓库的命令单元）或没有有效记录的单元")

    def test_missing_or_unreadable_request_reexecutes_everything_in_the_original_shape(self) -> None:
        self._green_source()
        for label, extra in (("没给请求", []), ("请求读不出", ["--unit-inherit-request", str(self.base / "nope.json")])):
            with self.subTest(label):
                rc, summary, manifest = self._run(*self._inherit_args(), *extra, mode="unit-inherit")
                self.assertEqual(rc, 0)
                self.assertEqual(manifest["mode"], "re-execute")
                self.assertEqual(self._split(manifest)[0], set())
                self.assertNotIn("unit_inherit_decision", manifest)
                self.assertEqual(summary["unit_inherit_decision"]["action"], "reexecute-all")
        request = self._request("withdrawn", approve=False)
        self._sample()
        rc, summary, manifest = self._run(*self._inherit_args(request), mode="unit-inherit")
        self.assertEqual((rc, manifest["mode"]), (0, "re-execute"))
        self.assertIn("专项批准", "；".join(summary["unit_inherit_decision"]["checkpoints"][0]["reasons"]))

    def test_invalid_argument_combinations_are_usage_errors(self) -> None:
        request = self.base / "request.json"
        request.write_text("{}", encoding="utf-8")
        cases = {
            "请求配错模式": (["--unit-inherit-request", str(request), *self._inherit_args()], "re-execute", "只与 --mode unit-inherit 同用"),
            "与 B-09 请求混用": (["--unit-inherit-request", str(request), "--full-set-request", str(request), *self._inherit_args()],
                            "unit-inherit", "不能与 B-09／B-10 请求同时使用"),
            "提案不配只判定": (["--unit-inherit-propose", str(self.base / "p"), *self._inherit_args()], "unit-inherit", "只与 --decide-only 同用"),
            "缺客户端与 control": (["--unit-inherit-request", str(request)], "unit-inherit", "要给客户端与 control 目录"),
        }
        for label, (extra, mode, message) in cases.items():
            with self.subTest(label):
                completed, _out = self._exec(*extra, mode=mode)
                self.assertEqual(completed.returncode, 2, completed.stderr[-2000:])
                self.assertIn(message, completed.stderr)


class EntryGatesScriptTests(unittest.TestCase):
    """入口门禁脚本的模式判定与执行器参数：把脚本里的两段原样截出来在 bash 里跑（不跑门禁本身），逐种参数组合核对。
    Codex 不给承接参数时结论与改造前相同（B-09 的回退照旧），逐行对照见 test_unit_inherit_codex_parity。"""

    SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh"

    def _segment(self, start: str, end: str) -> str:
        text = self.SCRIPT.read_text(encoding="utf-8")
        begin = text.index(start)
        return text[begin:text.index(end, begin)]

    def parse(self, *args: str) -> tuple[int, str, str]:
        segment = self._segment('PROFILE=entry; OUT="";', 'for file in "$POLICY_ACTIVATION" "$PRE_A3_CERTIFICATION"; do')
        snippet = ('set -Eeuo pipefail\nusage() { echo "用法" >&2; }\nD=/data; HISTORY_TEST_TREE=/hist\n' + segment +
                   'printf "MODE=%s REQUIRE=%s REQUEST=%s PROPOSE=%s STAGE=%s\\n" "$MODE" "$REQUIRE_DEPLOYED" "$UNIT_INHERIT_REQUEST" '
                   '"$UNIT_INHERIT_PROPOSE" "$UNIT_INHERIT_STAGE"\n')
        completed = subprocess.run(["bash", "-c", snippet, "entry-gates", *args, "/bundle", "codex/x", "c" * 40],
                                   capture_output=True, text=True, timeout=30)
        return completed.returncode, completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "", completed.stderr

    def test_mode_decision_for_every_argument_combination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = str(Path(directory).resolve())
            accepted = {
                "Codex 缺省": ([], "MODE=re-execute REQUIRE=false REQUEST= PROPOSE= STAGE=pre-ledger"),
                "B-09 没给请求照旧回退": (["--mode", "full-set-pass"], "MODE=re-execute REQUIRE=false REQUEST= PROPOSE= STAGE=pre-ledger"),
                "B-09 带读集审计照旧回退": (["--audit-reads", "--mode", "full-set-pass"],
                                       "MODE=re-execute REQUIRE=false REQUEST= PROPOSE= STAGE=pre-ledger"),
                "逐单元承接没给请求回退": (["--mode", "unit-inherit"], "MODE=re-execute REQUIRE=false REQUEST= PROPOSE= STAGE=pre-ledger"),
                "给了请求即逐单元承接": (["--unit-inherit-request", "/r.json"],
                                   "MODE=unit-inherit REQUIRE=true REQUEST=/r.json PROPOSE= STAGE=pre-ledger"),
                "Formal 阶段": (["--mode", "unit-inherit", "--unit-inherit-request", "/r.json", "--unit-inherit-stage", "formal:c-1"],
                              "MODE=unit-inherit REQUIRE=true REQUEST=/r.json PROPOSE= STAGE=formal:c-1"),
                "可带严格读集审计": (["--unit-inherit-request", "/r.json", "--audit-reads", "--audit-strict"],
                               "MODE=unit-inherit REQUIRE=true REQUEST=/r.json PROPOSE= STAGE=pre-ledger"),
                "只写提案": (["--unit-inherit-propose", "/drill/proposal"],
                         "MODE=unit-inherit REQUIRE=true REQUEST= PROPOSE=/drill/proposal STAGE=pre-ledger"),
            }
            for label, (args, expected) in accepted.items():
                with self.subTest(label):
                    rc, line, stderr = self.parse(*args)
                    self.assertEqual((rc, line), (0, expected), stderr)
            refused = {
                "请求配错模式": (["--unit-inherit-request", "/r.json", "--mode", "re-execute"], "只与 --mode unit-inherit 同用"),
                "与 B-09 请求混用": (["--unit-inherit-request", "/r.json", "--full-set-request", "/f.json"], "不能与 B-09 全集请求同用"),
                "请求不是绝对路径": (["--unit-inherit-request", "r.json"], "必须是绝对路径"),
                "阶段不认识": (["--unit-inherit-request", "/r.json", "--unit-inherit-stage", "later"], "未知的入口阶段"),
                "宿主输入清单": (["--unit-inherit-request", "/r.json", "--audit-reads", "--audit-strict", "--audit-host-inputs", "/h.json"],
                           "不接受宿主输入清单"),
                "严格审计缺读集审计": (["--unit-inherit-request", "/r.json", "--audit-strict"], "严格读集审计必须同时启用 --audit-reads"),
                "提案目录已存在": (["--unit-inherit-propose", existing], "尚不存在的绝对路径"),
                "提案与请求同给": (["--unit-inherit-propose", "/drill/p", "--unit-inherit-request", "/r.json"], "写承接提案时不给承接请求"),
                "提案配错模式": (["--unit-inherit-propose", "/drill/p", "--mode", "re-execute"], "只与 --mode unit-inherit 同用"),
                "模式不认识": (["--mode", "inherit-everything"], "未知的模式"),
            }
            for label, (args, message) in refused.items():
                with self.subTest(label):
                    rc, _line, stderr = self.parse(*args)
                    self.assertEqual(rc, 2, stderr)
                    self.assertIn(message, stderr)

    def test_executor_arguments_follow_the_mode(self) -> None:
        segment = self._segment('# B5a-08：承接上下文要本轮客户端、入口阶段与 control 目录', '( cd "$TREE" && "${EXEC_ENV[@]}" python3 "$DRV/unit_executor.py" run-gates')

        def arguments(**values: str) -> list[str]:
            assignments = "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())
            snippet = ("set -Eeuo pipefail\nCT_CLIENT=claude; D=/data; UNIT_INHERIT_ARGS=()\n" + assignments + segment +
                       'printf "%s\\n" "${#UNIT_INHERIT_ARGS[@]}"\n'
                       'for item in ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"}; do printf "[%s]" "$item"; done\n')
            completed = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            count, _, rest = completed.stdout.partition("\n")
            items = re.findall(r"\[([^]]*)\]", rest)
            self.assertEqual(len(items), int(count))
            return items

        self.assertEqual(arguments(MODE="re-execute", UNIT_INHERIT_STAGE="pre-ledger", UNIT_INHERIT_PROPOSE="", UNIT_INHERIT_REQUEST=""), [],
                         "Codex 与其它模式不追加任何参数")
        common = ["--unit-inherit-client", "claude", "--unit-inherit-stage", "pre-ledger", "--unit-inherit-control", "/data/control"]
        self.assertEqual(arguments(MODE="unit-inherit", UNIT_INHERIT_STAGE="pre-ledger", UNIT_INHERIT_PROPOSE="", UNIT_INHERIT_REQUEST="/r.json"),
                         [*common, "--unit-inherit-request", "/r.json"])
        self.assertEqual(arguments(MODE="unit-inherit", UNIT_INHERIT_STAGE="pre-ledger", UNIT_INHERIT_PROPOSE="/p", UNIT_INHERIT_REQUEST=""),
                         [*common, "--decide-only", "--unit-inherit-propose", "/p"])


if __name__ == "__main__":
    unittest.main()
