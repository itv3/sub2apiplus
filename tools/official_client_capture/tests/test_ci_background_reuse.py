"""同树重部署沿用测试结论（优化第 5 项）。

执行器端：``binding.json`` 在真实运行结束与 ``--decide-only`` 时写出同一份绑定（同一棵树、同一份环境上两次必须相同），门禁集合、调度策略、输入清单、平台环境
任一项变了绑定就变。后台验证端：通过的结论带沿用绑定；部署收据只换时间戳（五摘要不变）时 ``reuse`` 现算新部署上的绑定逐项比对，全部相同才沿用，写一份绑定新部署的
通过结论；任一项变了或过了有效期就拒绝并逐项写明；``check``（批次边界、VC-5 前核对、统一准入）对沿用的结论重放来源冻结副本、有效期与工具身份。
入口门禁用替身脚本（支持 ``--bind-only``），后台进程是真实的新会话子进程。
"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools.ci import background_validation as bv
from tools.ci import unit_executor as ue
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase, _config

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
COMMIT = "c" * 40

# 替身入口门禁：--bind-only 时把 $FAKE_BINDING_JSON 写成 OUT/executor/binding.json（模拟现算的绑定）；否则把 $FAKE_GATES_FIXTURE 拷进 OUT（真实运行的产物）。
FAKE_ENTRY_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""; BIND=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --bind-only) BIND=true; shift ;;
    --profile|--mode|--record-store|--work|--wait-lock|--parallel|--declared-sample) shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$OUT"
if [ "$BIND" = true ]; then
  mkdir -p "$OUT/executor"
  echo "$@" > /dev/null
  cp "$FAKE_BINDING_JSON" "$OUT/executor/binding.json"
  echo "ENTRY_GATES_BIND_ONLY $OUT/executor/binding.json"
else
  cp -R "$FAKE_GATES_FIXTURE"/. "$OUT"/
fi
"""


class ExecutorBindingTests(RetryRunBase):
    """真实执行器：绑定文件在判定与真实运行时一致；各项变化都反映到绑定里。"""

    def make_world(self) -> Path:
        probe = self.root / "probe.py"
        probe.write_text("print('probe')\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("*\n!.gitignore\n!probe.py\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "add", "probe.py", ".gitignore"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=沿用验收", "-c", "user.email=reuse@example.invalid", "commit", "-q", "-m", "固定输入"], cwd=self.root, check=True)
        return probe

    def run_executor(self, *, units: list[dict], out: str, decide_only: bool = False, env: dict[str, str] | None = None,
                     parallel: int = 2, retry: object = None) -> Path:
        gates = [{"gate_id": f"gate-{unit['unit_id']}", "units": [unit["unit_id"]]} for unit in units]
        manifest = self.root / "gates.json"
        manifest.write_text(json.dumps({"schema_version": ue.GATES_SCHEMA, "units": units, "gates": gates}, ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "-B", str(EXECUTOR), "run-gates", "--manifest", str(manifest), "--config", str(_config(self.root, **({"retry": retry} if retry else {}))),
                   "--weights", str(self.root / "no-weights.json"), "--durations", str(self.root / "no-durations.json"), "--cores", "4", "--parallel", str(parallel),
                   "--state-dir", str(self.root / f"state-{out}"), "--out-dir", str(self.root / out), "--record-store", str(self.store), "--shared-caches", "off"]
        if decide_only:
            command.append("--decide-only")
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT), **(env or {})})
        self.assertIn(completed.returncode, (0, 1), completed.stderr[-1500:])
        return self.root / out / "binding.json"

    def unit(self, name: str = "probe:a", **extra: object) -> dict:
        return {"unit_id": name, "argv": [sys.executable, "-B", str(self.root / "probe.py")], "cwd": str(self.root),
                "inputs": {"files": [{"category": "tests", "path": "probe.py"}]}, **extra}

    def test_decide_only_and_real_runs_write_the_same_binding(self) -> None:
        self.make_world()
        units = [self.unit("probe:a"), self.unit("probe:b")]
        decided = json.loads(self.run_executor(units=units, out="decide", decide_only=True).read_text(encoding="utf-8"))
        real = json.loads(self.run_executor(units=units, out="real").read_text(encoding="utf-8"))
        self.assertEqual(decided, real, "判定时现算的绑定必须等于真实运行结束时登记的绑定，沿用才比得起来")
        self.assertEqual(decided["schema_version"], ue.BINDING_SCHEMA)
        self.assertEqual(decided["units"], 2)
        for key in ("policy_sha256", "environment_sha256", "executor_sha256", "gate_set_sha256", "inputs_manifest_sha256"):
            self.assertRegex(decided[key], r"^[0-9a-f]{64}$", key)

    def test_each_component_reacts_to_its_own_change(self) -> None:
        probe = self.make_world()
        units = [self.unit("probe:a")]
        base = json.loads(self.run_executor(units=units, out="base", decide_only=True).read_text(encoding="utf-8"))
        again = json.loads(self.run_executor(units=units, out="again", decide_only=True).read_text(encoding="utf-8"))
        self.assertEqual(base, again, "同一输入两次现算相同")
        gate_set = json.loads(self.run_executor(units=[*units, self.unit("probe:extra")], out="gates", decide_only=True).read_text(encoding="utf-8"))
        policy = json.loads(self.run_executor(units=units, out="policy", decide_only=True, parallel=3).read_text(encoding="utf-8"))
        environment = json.loads(self.run_executor(units=units, out="env", decide_only=True, env={"REUSE_ENV_PROBE": "变了"}).read_text(encoding="utf-8"))
        probe.write_text("print('probe 变了')\n", encoding="utf-8")
        subprocess.run(["git", "-c", "user.name=沿用验收", "-c", "user.email=reuse@example.invalid", "commit", "-q", "-am", "改输入"], cwd=self.root, check=True)
        inputs = json.loads(self.run_executor(units=units, out="inputs", decide_only=True).read_text(encoding="utf-8"))
        changed = lambda other: sorted(key for key in base if base[key] != other[key])   # noqa: E731
        self.assertIn("gate_set_sha256", changed(gate_set))
        self.assertEqual(changed(policy), ["policy_sha256"])
        self.assertEqual(changed(environment), ["environment_sha256"])
        self.assertIn("inputs_manifest_sha256", changed(inputs))

    def test_the_binding_is_a_separate_file_not_part_of_the_summary_or_manifest(self) -> None:
        self.make_world()
        self.run_executor(units=[self.unit()], out="real")
        summary = json.loads((self.root / "real" / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.root / "real" / "unit-manifest.json").read_text(encoding="utf-8"))
        for document in (summary, manifest):
            self.assertNotIn("binding", document)
            self.assertNotIn("inputs_manifest_sha256", document)


def deploy_receipt(data: Path, stamp: str, *, files: str = "f") -> Path:
    path = data / "control" / f"codex-01630-supervisor-enable-{stamp}.json"
    # 真实部署收据带部署时间戳：同一棵树重部署时只有它（以及收据文件名）变，工具身份五摘要不变，所以收据摘要不同、结论的绑定键随之不同。
    path.write_text(json.dumps({"status": "passed", "tool_files_sha256": files * 64, "policy_version": 7, "policy_sha256": "a" * 64,
                                "wire_producer_sha256": "b" * 64, "evidence_semantics_sha256": "c" * 64, "created_at_utc": stamp}), encoding="utf-8")
    return path


BINDING = {"schema_version": "unit-executor-binding/v1", "mode": "re-execute", "policy_sha256": "1" * 64, "environment_sha256": "2" * 64,
           "executor_sha256": "3" * 64, "gate_set_sha256": "4" * 64, "inputs_manifest_sha256": "5" * 64, "units": 3}


class ReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data, self.runroot = self.root / "data", self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.work = self.root / "work"
        (self.root / "x.bundle").write_bytes(b"isolated-bundle")
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text(FAKE_ENTRY_GATES, encoding="utf-8")
        self.gates.chmod(self.gates.stat().st_mode | stat.S_IXUSR)
        self.first_receipt = deploy_receipt(self.data, "20261025t000000z")
        self.binding_file = self.root / "binding.json"
        self.fixture = self.root / "fixture"
        (self.fixture / "executor").mkdir(parents=True)
        (self.fixture / "executor" / "binding.json").write_text(json.dumps(BINDING), encoding="utf-8")
        (self.fixture / "executor" / "summary.json").write_text(json.dumps({"schema_version": ue.GATES_SUMMARY_SCHEMA, "status": "passed", "units": [],
                                                                            "gates": [], "diagnostic": []}), encoding="utf-8")
        (self.fixture / "entry-gates.json").write_text(json.dumps({"status": "passed", "gates": [{"gate_id": "g", "status": "passed"}],
                                                                  "source": {"commit": COMMIT, "deploy_receipt": self.first_receipt.name},
                                                                  "executor_summary": "SUMMARY"}), encoding="utf-8")
        self.set_binding(BINDING)
        os.environ["FAKE_GATES_FIXTURE"] = str(self.fixture)
        os.environ["FAKE_BINDING_JSON"] = str(self.binding_file)
        self.addCleanup(os.environ.pop, "FAKE_GATES_FIXTURE", None)
        self.addCleanup(os.environ.pop, "FAKE_BINDING_JSON", None)

    def set_binding(self, document: dict) -> None:
        self.binding_file.write_text(json.dumps(document), encoding="utf-8")

    def source_conclusion(self) -> Path:
        """用 run() 产出一份通过的真实形态结论（带沿用绑定）。"""

        deployment = bv.latest_deployment(self.data)
        path = bv.result_path(self.runroot, COMMIT, deployment)
        out = self.root / "bv-out"
        # 入口总摘要的 executor_summary 指向 out 里的真实文件：run() 先让替身把 fixture 拷进 out，这里在拷之前把指针改成 out 下的路径。
        document = json.loads((self.fixture / "entry-gates.json").read_text(encoding="utf-8"))
        document["executor_summary"] = str(out / "executor" / "summary.json")
        (self.fixture / "entry-gates.json").write_text(json.dumps(document), encoding="utf-8")
        payload = {"schema_version": bv.SCHEMA, "commit": COMMIT, "branch": "main", "bundle": str(self.root / "x.bundle"), "deployment": deployment,
                   "profile": "full-gates", "mode": "re-execute", "status": "running", "pid": None, "started_at_utc": "2026-10-25T00:00:00Z",
                   "completed_at_utc": None, "out": str(out), "log": str(self.root / "bv.log"), "record_store": None, "work": str(self.work),
                   "input_contract": {"work": str(self.work)}, "cache_roots": {"GOCACHE": str(self.work / "go-build-cache"),
                                                                               "GOMODCACHE": str(self.work / "go-mod-cache"), "GOTMPDIR": str(self.work / "go-tmp")},
                   "vc_env": None, "entry_gates": str(self.gates), "data_root": str(self.data), "runroot": str(self.runroot), "then_dryrun": False,
                   "failed_gates": [], "reason": None, "returncode": None, "dryrun": None}
        bv._write(path, payload)
        with unittest.mock.patch.object(bv, "_bound_work_roots", return_value=(self.work,)):
            import signal
            previous = signal.getsignal(signal.SIGTERM)
            try:
                bv.run(path)
            finally:
                signal.signal(signal.SIGTERM, previous)
        self.assertEqual(bv._read(path)["status"], "passed", bv._read(path).get("reason"))
        return path

    def redeploy_same_tree(self, stamp: str = "20261025t120000z") -> Path:
        return deploy_receipt(self.data, stamp)

    def args(self, mode: str = "only", *flags: str, **extra: object):
        return bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"), "--branch", "main",
                          "--commit", COMMIT, "--reuse", mode, "--entry-gates", str(self.gates), "--preflight", "off", *flags])

    def vc_env(self) -> Path:
        path = self.root / "env.sh"
        path.write_text("# 隔离参数\n", encoding="utf-8")
        return path

    def test_a_passed_conclusion_carries_the_reuse_binding(self) -> None:
        path = self.source_conclusion()
        binding = bv._read(path)["reuse_binding"]
        self.assertEqual(bv.reuse_binding_problems(binding), [])
        self.assertEqual(binding["components"]["commit"], COMMIT)
        self.assertEqual(binding["components"]["tool_identity"]["codex"]["tool_files_sha256"], "f" * 64)
        self.assertEqual({key: binding["components"][key] for key in BINDING if key.endswith("_sha256")}, {key: value for key, value in BINDING.items() if key.endswith("_sha256")})
        self.assertEqual(binding["validity_hours"], bv.REUSE_VALIDITY_HOURS)

    def test_same_tree_redeploy_reuses_the_conclusion_and_admission_passes(self) -> None:
        source = self.source_conclusion()
        self.redeploy_same_tree()
        self.assertEqual(bv.check(self.runroot, self.data, require_passed=True)[0], 3, "沿用之前新部署上没有结论")
        outcome = bv.start(self.args())
        self.assertEqual(outcome["action"], "reused", outcome)
        code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual(code, 0, message)
        self.assertIn("沿用自", message)
        reused = next(payload for path, payload in bv.results(self.runroot) if payload.get("reuse"))
        self.assertEqual(reused["deployment"]["receipt"], "codex-01630-supervisor-enable-20261025t120000z.json")
        self.assertEqual(reused["reuse"]["source_result"], str(source))
        self.assertEqual(reused["reuse"]["source_deployment"], self.first_receipt.name)
        self.assertEqual(reused["reuse_binding"], bv._read(source)["reuse_binding"], "有效期沿用来源的，不延长")
        self.assertEqual(reused["status"], "passed")

    def test_every_changed_component_requires_a_revalidation_and_names_itself(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        cases = {
            "门禁集合摘要": {"gate_set_sha256": "9" * 64},
            "执行器摘要": {"executor_sha256": "9" * 64},
            "调度策略摘要": {"policy_sha256": "9" * 64},
            "输入清单摘要": {"inputs_manifest_sha256": "9" * 64},
            "平台环境摘要": {"environment_sha256": "9" * 64},
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.set_binding({**BINDING, **change})
                outcome = bv.start(self.args())
                self.assertEqual(outcome["action"], "refused", outcome)
                self.assertIn(f"{label}变了", "；".join(outcome["reasons"]))
                self.assertEqual(bv.check(self.runroot, self.data, require_passed=True)[0], 3, "拒绝时不写任何新结论")

    def test_changed_tool_identity_or_commit_is_refused(self) -> None:
        self.source_conclusion()
        deploy_receipt(self.data, "20261025t120000z", files="e")   # 换了一个受管文件：整树摘要变了
        outcome = bv.start(self.args())
        self.assertEqual(outcome["action"], "refused")
        self.assertIn("工具身份五摘要变了：codex 的 tool_files_sha256", "；".join(outcome["reasons"]))
        other = bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"), "--branch", "main",
                           "--commit", "d" * 40, "--reuse", "only", "--entry-gates", str(self.gates)])
        self.assertEqual(bv.start(other)["action"], "refused", "别的提交没有可沿用的结论")

    def test_expired_binding_is_refused_and_a_reused_conclusion_stops_passing_after_expiry(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        self.assertEqual(bv.start(self.args())["action"], "reused")
        self.assertEqual(bv.check(self.runroot, self.data, require_passed=True)[0], 0)
        later = bv.time.time() + (bv.REUSE_VALIDITY_HOURS + 1) * 3600
        with unittest.mock.patch.object(bv.time, "time", return_value=later):
            code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual(code, 3)
        self.assertIn("已过有效期", message)
        self.redeploy_same_tree("20261026t130000z")
        with unittest.mock.patch.object(bv.time, "time", return_value=later):
            refused = bv.start(self.args())
        self.assertEqual(refused["action"], "refused")
        self.assertIn("已过有效期", "；".join(refused["reasons"]))

    def test_tampered_frozen_source_copy_makes_check_refuse(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        self.assertEqual(bv.start(self.args())["action"], "reused")
        reused = next(payload for _path, payload in bv.results(self.runroot) if payload.get("reuse"))
        frozen = Path(reused["reuse"]["source_frozen"]["path"])
        document = json.loads(frozen.read_text(encoding="utf-8"))
        document["commit"] = "e" * 40
        frozen.write_text(json.dumps(document), encoding="utf-8")
        code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual(code, 3)
        self.assertIn("沿用不能重放", message)

    def test_a_failed_bind_run_refuses_instead_of_guessing(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        self.gates.write_text("#!/bin/bash\necho '数据根部署的受管树不是本提交' >&2\nexit 3\n", encoding="utf-8")
        outcome = bv.start(self.args())
        self.assertEqual(outcome["action"], "refused")
        self.assertIn("没有产出绑定", "；".join(outcome["reasons"]))

    def test_auto_mode_falls_back_to_a_full_validation_and_reports_why(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        self.set_binding({**BINDING, "policy_sha256": "9" * 64})
        self.gates.write_text(FAKE_ENTRY_GATES.replace('cp -R "$FAKE_GATES_FIXTURE"/. "$OUT"/', 'sleep 30'), encoding="utf-8")
        self.addCleanup(bv.stop, self.runroot, "测试收尾")
        started = bv.start(self.args(mode="auto"))
        self.assertEqual(started["action"], "started", started)
        self.assertIn("调度策略摘要变了", "；".join(started["reuse_refused"]))

    def test_auto_mode_reports_a_successful_reuse_as_exists_for_the_repair_loop(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        outcome = bv.start(self.args(mode="auto"))
        self.assertEqual((outcome["action"], outcome["status"], outcome["reused"]), ("exists", "passed", True), outcome)
        self.assertEqual(bv.check(self.runroot, self.data, require_passed=True)[0], 0)

    def test_a_successful_reuse_still_chains_the_post_deploy_dryrun_like_a_fresh_run(self) -> None:
        """验收修正必修 6：沿用只省掉测试，新部署上的入口空跑（then_dryrun）按原逻辑照接，不能因沿用而跳过。"""

        self.source_conclusion()
        self.redeploy_same_tree()
        env = self.vc_env()
        with unittest.mock.patch.object(bv, "_chain_dryrun", return_value={"action": "started", "result": "dryrun.json"}) as chain:
            outcome = bv.start(self.args("only", "--then-dryrun", "--vc-env", str(env)))
        self.assertEqual(outcome["action"], "reused", outcome)
        self.assertEqual(chain.call_count, 1, "沿用成功之后接了一次空跑")
        payload = chain.call_args.args[0]
        self.assertEqual((payload["commit"], payload["vc_env"], payload["runroot"], payload["data_root"]),
                         (COMMIT, str(env), str(self.runroot), str(self.data)), "空跑用本次调用的坐标，不是来源结论里记的")
        self.assertEqual(outcome["dryrun"], {"action": "started", "result": "dryrun.json"})
        reused = next(payload for _path, payload in bv.results(self.runroot) if payload.get("reuse"))
        self.assertTrue(reused["then_dryrun"])
        self.assertEqual(reused["dryrun"], {"action": "started", "result": "dryrun.json"}, "空跑状态记进结论")

    def test_reuse_without_then_dryrun_chains_nothing_exactly_like_a_fresh_run_without_it(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        with unittest.mock.patch.object(bv, "_chain_dryrun") as chain:
            outcome = bv.start(self.args("only"))
        self.assertEqual(outcome["action"], "reused")
        chain.assert_not_called()
        self.assertNotIn("dryrun", outcome)
        reused = next(payload for _path, payload in bv.results(self.runroot) if payload.get("reuse"))
        self.assertEqual((reused["then_dryrun"], reused["dryrun"]), (False, None))

    def test_auto_mode_reuse_also_chains_the_dryrun_and_reports_it(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        with unittest.mock.patch.object(bv, "_chain_dryrun", return_value={"action": "started"}) as chain:
            outcome = bv.start(self.args("auto", "--then-dryrun", "--vc-env", str(self.vc_env())))
        self.assertEqual((outcome["action"], outcome["status"], outcome["reused"], outcome["dryrun"]), ("exists", "passed", True, {"action": "started"}))
        self.assertEqual(chain.call_count, 1)

    def test_the_reuse_subcommand_passes_then_dryrun_through(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        base = ["reuse", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"), "--branch", "main",
                "--commit", COMMIT, "--entry-gates", str(self.gates), "--vc-env", str(self.vc_env())]
        with unittest.mock.patch.object(bv, "_chain_dryrun", return_value={"action": "started"}) as chain, unittest.mock.patch("builtins.print"):
            self.assertEqual(bv.main([*base, "--then-dryrun"]), 0)
        self.assertEqual(chain.call_count, 1)

    def test_two_client_deployments_chain_the_other_clients_dryrun_after_reuse_too(self) -> None:
        """两客户端都纳管时，沿用成功后也和真实运行一样把另一客户端登记的空跑一并接上（只接别人的，不重复接自己的）。"""

        path = self.root / "result.json"
        reused = {"schema_version": bv.SCHEMA, "commit": COMMIT, "client": "codex", "then_dryrun": True, "data_root": str(self.data), "runroot": str(self.runroot), "vc_env": "/e",
                  "deployments": {"codex": {"receipt": "a"}, "claude": {"receipt": "b"}}}
        bv._write(path, reused)
        peers = [{"client": "codex", "vc_env": "/own", "runroot": "/own-run", "data_root": "/own-data"},
                 {"client": "claude", "vc_env": "/peer-env", "runroot": "/peer-run", "data_root": "/peer-data"}]
        with unittest.mock.patch.object(bv, "peers", return_value=peers), unittest.mock.patch.object(bv, "registry_path", return_value=self.root / "reg"), \
                unittest.mock.patch.object(bv, "_chain_dryrun", return_value={"action": "started"}) as chain:
            outcome = bv._dryrun_after_reuse(path, reused)
        self.assertEqual(chain.call_count, 2)   # 自己的空跑一次，claude 那份一次
        peer_payload = chain.call_args_list[1].args[0]
        self.assertEqual((peer_payload["vc_env"], peer_payload["runroot"], peer_payload["data_root"]), ("/peer-env", "/peer-run", "/peer-data"))
        self.assertEqual(list(outcome["peer_dryruns"]), ["claude"])
        self.assertEqual(bv._read(path)["peer_dryruns"], outcome["peer_dryruns"])

    def test_the_driver_wrapper_asks_for_the_dryrun_by_default_on_reuse(self) -> None:
        """驱动入口 ``background-validate.sh reuse`` 与 ``start`` 同一语义：缺省带 --then-dryrun，--no-dryrun 才关；真实执行脚本里 reuse 那一段看实际传给 Python 的词。"""

        text = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background-validate.sh").read_text(encoding="utf-8")
        start = text.index("  reuse)\n")
        block = text[start + len("  reuse)\n"):text.index(";;", start)]

        def words(*extra: str) -> list[str]:
            script = ("RUNROOT=/r; D=/d; ARM64_VC_ENV=/e; BV=(printf '%s\\n'); CT_CLIENT_ARGV=(); "
                      f"set -- /b main {COMMIT} " + " ".join(extra) + "\nshift 0\n" + "bundle=$1; branch=$2; commit=$3; shift 3\n"
                      + block.split("bundle=$1; branch=$2; commit=$3; shift 3\n", 1)[1])
            completed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout.split()

        default = words()
        self.assertIn("--then-dryrun", default)
        self.assertEqual(default[0], "reuse")
        off = words("--no-dryrun")
        self.assertNotIn("--then-dryrun", off)
        self.assertNotIn("--no-dryrun", off, "--no-dryrun 是驱动入口的开关，不传给 Python")

    def test_the_python_command_line_defaults_to_auto_and_off_is_explicit(self) -> None:
        """验收修正建议 12：沿用开关在 Python 命令行也缺省打开（auto），与驱动入口一致；--reuse off 显式不沿用。"""

        parsed = bv._parse(["start", "--runroot", "/r", "--data-root", "/d", "--bundle", "/b", "--branch", "x", "--commit", COMMIT])
        self.assertEqual(parsed.reuse, "auto")
        self.assertEqual(bv._parse(["start", "--runroot", "/r", "--data-root", "/d", "--bundle", "/b", "--branch", "x", "--commit", COMMIT,
                                    "--reuse", "off"]).reuse, "off")
        wrapper = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background-validate.sh").read_text(encoding="utf-8")
        self.assertIn("--reuse auto", wrapper)
        self.assertIn("--no-reuse", wrapper)

    def test_a_default_start_tries_to_reuse_without_being_asked(self) -> None:
        self.source_conclusion()
        self.redeploy_same_tree()
        args = bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"), "--branch", "main",
                          "--commit", COMMIT, "--entry-gates", str(self.gates), "--preflight", "off"])
        outcome = bv.start(args)
        self.assertEqual((outcome["action"], outcome["status"], outcome["reused"]), ("exists", "passed", True), outcome)

    def test_component_differences_cover_every_client_and_digest(self) -> None:
        source = {"tool_identity": {"codex": {"tool_files_sha256": "a", "policy_sha256": "b"}, "claude": {"tool_files_sha256": "c"}}}
        current = {"tool_identity": {"codex": {"tool_files_sha256": "a", "policy_sha256": "X"}}}
        text = "；".join(bv.component_differences(source, current))
        self.assertIn("codex 的 policy_sha256", text)
        self.assertIn("已纳管客户端集合不同（claude）", text)
        self.assertEqual(bv.component_differences(source, copy.deepcopy(source)), [])


class EntryGatesBindOnlyScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh"
        self.text = self.path.read_text(encoding="utf-8")

    def test_bind_only_flag_runs_the_executor_decide_only_and_skips_the_summary_check(self) -> None:
        self.assertIn("--bind-only) BIND_ONLY=true; BIND_ARGS=(--decide-only); shift ;;", self.text)
        self.assertIn('${BIND_ARGS[@]+"${BIND_ARGS[@]}"} ) > "$OUT/executor.log"', self.text)
        bind_branch = self.text.index('if [ "$BIND_ONLY" = true ]; then')
        summary_check = self.text.index('if [ "$EXEC_RC" -gt 1 ] || [ ! -f "$OUT/executor/summary.json" ]')
        self.assertLess(bind_branch, summary_check, "绑定分支必须在“没有 summary.json 就中止”的检查之前")
        self.assertIn("ENTRY_GATES_BIND_ONLY", self.text)

    def test_script_is_syntactically_valid_and_copies_are_identical(self) -> None:
        self.assertEqual(subprocess.run(["bash", "-n", str(self.path)], capture_output=True, text=True).returncode, 0)
        ci = REPO_ROOT / "tools" / "ci"
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("background_validation.py", "unit_executor.py"):
            self.assertEqual((driver / name).read_bytes(), (ci / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
