"""B5a-08 验收：加入逐单元承接（unit-inherit）之后，Codex 现有的重新执行全集（re-execute）与 B-09 全集通过（full-set-pass）
路径的行为与产物和改造前逐字相同。

改造前的源码取自 B5a-08 开工时本分支的 HEAD（``BASELINE_COMMIT``，即 B5a-06 的提交），从真实仓库的 git 历史读出：

* 执行器：把基线的 tools/ci 整目录导出到临时目录，与改造后的执行器在同一个临时 git 仓库上跑同一串运行（重新执行全集、
  全集通过的逐单元旧承接、修好失败后的全集通过、带读不出的 B-09 请求回退、只判定），各自一个记录库；汇总、运行清单、
  计划、判定与 B-09 判定逐字相同——只把每次运行都不同的值（运行 ID、时间与用时、路径、执行器文件摘要及由它决定的记录
  与清单摘要）换成占位，单元规格、输入、环境与调度策略的摘要照原样比较；
* 记录层、P0 v2 证据重验、pre-A3 从记录签发与核验：基线模块作为独立模块对象加载，与改造后的模块在同一批合成记录库与
  变异上各算一次，结果或报错逐字相同；
* 源码残差：各文件相对基线删掉或改写的行只有登记的那几处（每处写明为什么 Codex 下等价），入口门禁脚本代入 Codex 的客户端表
  取值后与基线的差异只有登记的新增与改写行；Codex 的入口步骤表与门禁项输入与基线逐字相同（Claude 的步骤声明只有
  ``STEP_CHANGES_AFTER_B5A08`` 登记的设计变化）。
基线读不到（浅克隆）时跳过并写明原因；某个文件在 B5a-08 之后又改过（摘要不再是 ``SOURCE_DIGESTS`` 登记的那份）时，它的残差
对照跳过——以新的基线提交重新对照：B5a-09、B5a-10 改过的入口步骤分别以该批开工时的提交为新基线再核一次删改的行
（``LATER_SOURCE_BASELINES``；入口门禁脚本在 B5a-10 之后的对照在 B5a-06 的对照模块里）。本模块读真实仓库的 git 历史，按
``unit_records.GIT_READERS`` 登记。
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from tools.ci import entry_steps as es
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur
from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
from tools.official_client_capture.tests import test_codex_upgrade_p0_manifest_evidence as p0_tests
from tools.official_client_capture.tests import test_codex_upgrade_pre_a3_certification as pre_a3_tests
from tools.official_client_capture.tests import test_driver_client_table_codex_parity as b5a06
from tools.official_client_capture.tests.test_ci_unit_records import FAIL, PASS, _commit, _config, _repo, _write

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_COMMIT = "31f7c0308"


def git_source(relative: str, commit: str = BASELINE_COMMIT) -> bytes | None:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{commit}:{relative}"],
                              capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def load_baseline(relative: str, name: str) -> types.ModuleType | None:
    """基线源码作为独立模块对象：``__file__`` 指向仓库同一位置（同目录的兄弟模块按路径照常加载）。"""

    source = git_source(relative)
    if source is None:
        return None
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / relative)
    with mock.patch.dict(sys.modules, {name: module}):
        exec(compile(source, f"{BASELINE_COMMIT}:{relative}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


def export_baseline_tree(relative: str, target: Path) -> bool:
    """``git archive`` 导出基线的一个目录（只含跟踪文件），解到 ``target`` 下同样的相对位置。"""

    try:
        archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", BASELINE_COMMIT, relative],
                                 capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        members = [member for member in bundle.getmembers() if member.name.startswith(relative.rstrip("/") + "/")]
        if any(member.issym() or member.islnk() or ".." in Path(member.name).parts for member in members):
            return False
        bundle.extractall(target, members=members, filter="data")
    return True


def outcome(call: Callable[[], Any]) -> tuple[str, Any]:
    """一次调用的结果或报错（异常类名＋消息）：两边逐字比较用。"""

    try:
        return "ok", call()
    except Exception as error:  # noqa: BLE001 - 比较的就是报错本身
        return type(error).__name__, str(error)


# ------------------------------------------------------------------------------------------------ 执行器产物

# 每次运行都不同、与 Codex 路径的行为无关的值：运行 ID、时间与用时、路径、执行器文件摘要及由它决定的记录与清单摘要。
VOLATILE_KEYS = frozenset({
    "run_id", "decided_at_utc", "completed_at_utc", "started_at_utc", "seconds", "cpu_seconds", "max_rss_mb", "elapsed_seconds",
    "unit_seconds", "record_sha256", "manifest_sha256", "executor", "executor_sha256", "log", "record_path", "out_dir",
    "record_store", "path", "max_cores_in_use",
})
_RUN_ID = re.compile(r"\d{8}t\d{6}z-\d+-[0-9a-f]{6}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


def normalize(value: Any, base: Path) -> Any:
    if isinstance(value, dict):
        return {key: "<易变>" if key in VOLATILE_KEYS else normalize(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, base) for item in value]
    if isinstance(value, str):
        text = re.sub(r"<base>/(out|store|state)-(old|new)(-\d+)?", r"<\1>", value.replace(str(base), "<base>"))
        return _TIME.sub("<时间>", _RUN_ID.sub("<运行>", text))
    return value


class ExecutorArtifactCodexParityTests(unittest.TestCase):
    ARTIFACTS = ("summary.json", "unit-manifest.json", "plan.json", "decisions.json", "full-set-decision.json")

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        if not export_baseline_tree("tools/ci", self.base / "old"):
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的 tools/ci（浅克隆或不在仓库内）")
        self.executors = {"old": self.base / "old" / "tools" / "ci" / "unit_executor.py", "new": REPO_ROOT / "tools" / "ci" / "unit_executor.py"}
        self.root = _repo(self.base / "repo", {"pkg/tests/test_fail_a.py": FAIL})
        self.config = _config(self.base)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
        self.environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)})
        self.runs = 0

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

    def _run(self, side: str, mode: str, *extra: str) -> tuple[int, dict[str, Any]]:
        self.runs += 1
        out = self.base / f"out-{side}-{self.runs}"
        command = [sys.executable, str(self.executors[side]), "run-gates", "--manifest", str(self._manifest()),
                   "--config", str(self.config), "--weights", str(self.base / "none.json"), "--durations", str(self.base / "none.json"),
                   "--parallel", "1", "--cores", "4", "--state-dir", str(self.base / f"state-{side}"), "--out-dir", str(out),
                   "--shared-caches", "off", "--record-store", str(self.base / f"store-{side}"), "--mode", mode, *extra]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300, env=self.environment)
        artifacts = {name: json.loads((out / name).read_text(encoding="utf-8")) for name in self.ARTIFACTS if (out / name).is_file()}
        if not artifacts:
            self.fail(f"{side} 执行器没有产物：{completed.stderr[-2000:]}")
        return completed.returncode, artifacts

    def assert_same(self, label: str, mode: str, *extra: str) -> dict[str, Any]:
        old_rc, old = self._run("old", mode, *extra)
        new_rc, new = self._run("new", mode, *extra)
        self.assertEqual(new_rc, old_rc, label)
        self.assertEqual(sorted(new), sorted(old), f"{label}：产物文件集合相同")
        for name in old:
            with self.subTest(label=label, artifact=name):
                self.assertEqual(normalize(new[name], self.base), normalize(old[name], self.base))
        return new

    def test_reexecute_full_set_pass_b09_fallback_and_decide_only_are_word_for_word(self) -> None:
        first = self.assert_same("重新执行全集（有失败）", "re-execute")
        self.assertEqual(first["unit-manifest.json"]["mode"], "re-execute")
        second = self.assert_same("全集通过的逐单元旧承接", "full-set-pass")
        self.assertTrue(any(entry["disposition"] == "inherited" for entry in second["unit-manifest.json"]["units"]))
        _write(self.root, {"pkg/tests/test_fail_a.py": PASS})
        _commit(self.root, "修好失败的测试")
        self.assert_same("修好之后的全集通过", "full-set-pass")
        fallback = self.assert_same("读不出的 B-09 请求回退", "full-set-pass", "--full-set-request", str(self.base / "missing-request.json"))
        self.assertEqual(fallback["unit-manifest.json"]["full_set_decision"]["action"], "reexecute-all")
        self.assert_same("只判定", "full-set-pass", "--decide-only")


# ------------------------------------------------------------------------------------------------ 记录层与消费端

class RecordsAndConsumersCodexParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_records = load_baseline("tools/ci/unit_records.py", "unit_records_before_b5a08")
        self.old_receipts = load_baseline("tools/official_client_capture/codex_upgrade_vc_receipt.py", "vc_receipt_before_b5a08")
        self.old_pre_a3 = load_baseline("tools/official_client_capture/codex_upgrade_pre_a3_certification.py", "pre_a3_before_b5a08")
        if None in (self.old_records, self.old_receipts, self.old_pre_a3):
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的记录层或消费端（浅克隆或不在仓库内）")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.cases = 0

    def fixture(self) -> p0_tests._Fixture:
        self.cases += 1
        return p0_tests._Fixture(self.base / f"p0-{self.cases}")

    def test_verify_manifest_is_word_for_word_on_reexecute_and_full_set_pass_manifests(self) -> None:
        fixture = self.fixture()
        fixture.build()
        store = fixture.store
        manifests = {run_id: json.loads(store.manifest_path(run_id).read_text(encoding="utf-8")) for run_id in (fixture.RUN1, fixture.RUN2)}
        run2 = manifests[fixture.RUN2]
        reseal = lambda **changes: ur.seal({**{k: v for k, v in run2.items() if k != "manifest_sha256"}, **changes}, "manifest_sha256")  # noqa: E731
        cases = {
            "重新执行全集": manifests[fixture.RUN1],
            "全集通过": run2,
            "改成重新执行全集却有承接项": reseal(mode="re-execute"),
            "模式不认识": reseal(mode="whatever"),
            "承接期限超限": reseal(inheritance_max_age_hours=200.0),
            "缺一个单元": reseal(units=run2["units"][1:]),
            "没重新封装": {**run2, "mode": "re-execute"},
            "带了 unit_inherit_decision 字段的全集通过": reseal(unit_inherit_decision={"path": "/x"}),
        }
        for label, manifest in cases.items():
            for with_store in (True, False):
                with self.subTest(label=label, with_store=with_store):
                    new = ur.verify_manifest(manifest, store=store if with_store else None)
                    old = self.old_records.verify_manifest(manifest, store=self.old_records.RecordStore(store.root) if with_store else None)
                    self.assertEqual(new, old)

    def test_p0_manifest_evidence_reverification_is_word_for_word(self) -> None:
        mutations = {
            "通过": {}, "不闭合": {"extra_egress": True}, "缺测试 ID": {"missing_test_id": True}, "重复测试 ID": {"duplicate_test_id": True},
            "超期": {"stale": True}, "清单没发布": {"publish": False}, "两份证据形状不同": {"egress_v1": True},
            "五摘要不同": {"identity": dict(p0_tests.IDENTITY, control_sha256="9" * 64)}, "断言通过数不符": {"passed_offset": 1},
        }
        for label, mutation in mutations.items():
            with self.subTest(label):
                root = self.fixture().build(**mutation)
                facts = json.loads((root / "p0-facts.json").read_text(encoding="utf-8"))
                new = outcome(lambda: receipts.verify_p0_manifest_evidence(root, facts))
                old = outcome(lambda: self.old_receipts.verify_p0_manifest_evidence(root, facts))
                self.assertEqual(new, old)
        fixture = self.fixture()
        root = fixture.build()
        record = fixture.records["test_beta"]
        path = fixture.store.record_path("test_beta", record["record_sha256"])
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "seconds": 99.0}), encoding="utf-8")
        facts = json.loads((root / "p0-facts.json").read_text(encoding="utf-8"))
        self.assertEqual(outcome(lambda: receipts.verify_p0_manifest_evidence(root, facts)),
                         outcome(lambda: self.old_receipts.verify_p0_manifest_evidence(root, facts)))

    def test_pre_a3_issue_and_verify_from_records_are_word_for_word(self) -> None:
        with mock.patch.object(certification, "SCENARIOS", pre_a3_tests.FAKE_SCENARIOS), \
                mock.patch.object(self.old_pre_a3, "SCENARIOS", pre_a3_tests.FAKE_SCENARIOS):
            deployment, activation = pre_a3_tests.PreA3CertificationTests._bindings(self, self.base)  # type: ignore[arg-type]
            store = pre_a3_tests._SyntheticStore(self.base / "pre-a3")
            store.root.mkdir()
            names = certification.scenario_names()
            first, records = store.run("run-1", names)
            inherited = store.manifest("run-2", [store.entry(record, "inherited") for record in records], decided="2026-10-02T05:00:00Z")
            orphan = store.record(names[0], "run-never-published")
            cases = {
                "本次执行": first,
                "全部承接": inherited,
                "超期": store.manifest("run-3", [store.entry(record, "inherited") for record in records], decided="2026-10-09T01:00:01Z"),
                "重新执行全集里有承接项": store.manifest("run-4", [store.entry(record, "inherited") for record in records], mode="re-execute"),
                "原运行清单不存在": store.manifest("run-5", [store.entry(orphan, "inherited"), *[store.entry(r, "inherited") for r in records[1:]]]),
                "清单没发布": store.manifest("run-6", [store.entry(record) for record in records], publish=False),
            }

            def issue(module: types.ModuleType, manifest: Path) -> dict:
                return module.issue_certification_from_records(
                    store.root / "data" / "staging" / "pre-a3-scenarios", unit_manifest=manifest, record_store=store.store.root,
                    deployment_receipt=deployment, policy_activation=activation, observed_at_utc="2026-10-07T00:00:00Z")

            for label, manifest in cases.items():
                with self.subTest(label):
                    new = outcome(lambda: issue(certification, manifest))
                    self.assertEqual(new, outcome(lambda: issue(self.old_pre_a3, manifest)))
                    if new[0] == "ok" and new[1]["status"] == "passed":
                        output = certification.write_certification(self.base / f"{label}.json", new[1])
                        self.assertEqual(outcome(lambda: certification.verify_certification(output)),
                                         outcome(lambda: self.old_pre_a3.verify_certification(output)))


# ------------------------------------------------------------------------------------------------ 源码残差

# B5a-08 改动后各文件的内容摘要：之后又改过就跳过残差对照（以新的基线提交重新对照）。
SOURCE_DIGESTS: dict[str, str] = {
    "tools/arm64_capture_driver/driver/entry-gates.sh": "bca7b015ba199b7d4d29e130c3a287695750ad49c747ebc061181699168c7851",
    "tools/ci/entry_gates.py": "7903c17a9c1fc86a788b87027ffacb6fabe0a8b1a649fdcf1dbd3012f65ad8f0",
    "tools/ci/entry_orchestrator.py": "415247550f158d589de606310fbb209228c39d72de4e2c0b25a74e08f4d67713",
    "tools/ci/entry_steps.py": "819328b6c5fe2e450d2a5a4bfbf8840d152c943c340f91babbce069f61675a6c",
    "tools/ci/unit_executor.py": "61f83789666a8851203ac519c98b21bc80ab57c7c23ad5a28181921593c89c4c",
    "tools/ci/unit_records.py": "9bdc1f8ea5831945781585b0129337566a0cd4284b6c637b01fb34ce41995c31",
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "5ec405a8422b86fccf5e996d9f209442355470409f0c41d0a285b2dcd97ae9ab",
    "tools/official_client_capture/codex_upgrade_vc_receipt.py": "019f19b5cf6acf56e07b04981a9001e6b1ff33a3805a12da489928ce254661a5",
}
# 相对基线删掉或改写的行（unified diff 的删除侧），每处 Codex 下等价：
# * unit_executor.py：模式选项与帮助加 unit-inherit；读集合同、读集审计两处准入条件多一个「不是逐单元承接」的豁免（Codex 的
#   条件值不变）；B-11 严格审计的强制条件多一个「逐单元承接被拒」的分支（Codex 恒为假）；运行目录先看逐单元承接是否已提前
#   确定（Codex 为空，照原样调用 _out_dir）；读集审计器的覆盖判定改按「本次执行的单元」（Codex 没有承接单元，集合相同），
#   报告在有承接单元时才多一项 inherited_units；
# * unit_records.py：模式与标签表加 unit-inherit；执行器版本文件表加 unit_inherit.py；清单自检的承接项判定改成允许承接的模式
#   集合（含全集通过，Codex 判定不变），逐单元承接清单才要求读集闭合；
# * entry_orchestrator.py：入口门禁的模式表达式改为 _gates_mode()（Codex 取值不变）；构造与命令行入口多一个可选的承接请求；
#   读集审计的用法检查多一个「或给了承接请求」（Codex 不给，检查不变）；
# * entry_steps.py：按参数选择步骤表（Codex 不给承接请求，仍是原表）；导入 replace；
# * entry_gates.py：v2 P0 证据的说明文字取自模式（全集通过模式的文字不变）；
# * codex_upgrade_vc_receipt.py：逐条核验与门禁重验多一个缺省为全集通过的 inheriting_mode 参数；入口按清单模式分派（Codex 的
#   两种模式仍走 _p0_v2_manifest）；返回值先放进变量，逐单元承接时才多一项 unit_inherit；
# * codex_upgrade_pre_a3_certification.py：逐场景核验多一个缺省为全集通过的 inheriting_mode 参数；签发与核验按清单模式分派
#   （Codex 的两种模式仍走 _published_manifest，承接判定不变）。
SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/unit_executor.py": [
        "        missing = sorted(set(self.currents) - set(self.results))",
        "        complete = (self.strict and bool(self.currents) and set(self.results) == set(self.currents)",
        "        enforced = (self.strict and bool(self.currents) and not missing and set(self.results) == set(self.currents)",
        '        _write_json(path, {"schema_version": self.module.REPORT_SCHEMA, "repo_root": self.repo_root, "data_root": self.data_root,',
        '                           "data_root_exempt": dict(self.module.DATA_ROOT_EXEMPT),',
        '                           "data_root_exempt_patterns": dict(self.module.DATA_ROOT_EXEMPT_PATTERNS), "units": self.results,',
        '                           "units_with_findings": flagged, "undeclared_total": total, "skipped_whole_repo": skipped,',
        '                           "coverage_complete": complete, "strict": self.strict, "missing_units": missing,',
        '                           "reexecute_required_units": reexecute, "reuse_boundary_enforced": enforced})',
        '            p.add_argument("--mode", choices=("re-execute", "full-set-pass"), default="re-execute",',
        '                           help="re-execute：重新执行全集，不承接；full-set-pass：全集通过，承接有效记录、只执行其余单元（要给记录库）")',
        "        if not args.decide_only and not request_path and (args.mode != records.RE_EXECUTE or not args.audit_reads or not args.audit_strict):",
        "        if args.mode != records.RE_EXECUTE:",
        "    # B-11 登记策略下，B-09 拒绝后仍须按原严格审计合同重新执行。",
        "    if audit_policy is not None and full_set_verdict is not None and args.mode == records.RE_EXECUTE and not args.decide_only:",
        "    out_dir = _out_dir(args)",
    ],
    "tools/ci/unit_records.py": [
        "MODES = (FULL_SET_PASS, RE_EXECUTE)",
        'MODE_LABELS = {FULL_SET_PASS: "全集通过", RE_EXECUTE: "重新执行全集"}',
        'EXECUTOR_FILES = ("unit_executor.py", "unit_records.py", "read_audit.py", "read_audit_runtime.py", "entry_steps.py", "entry_gates.py", "entry-gates.sh", "full_set_receipt.py", "target_platform_gate.py")',
        '                     now=parse_utc(manifest.get("decided_at_utc")) or 0.0, historical_read_audit=historical)',
        '        if disposition == "inherited" and mode != FULL_SET_PASS:',
    ],
    "tools/ci/entry_orchestrator.py": [
        "                 full_set_request: Path | None = None, full_set_work: Path | None = None) -> None:",
        '                                          "--mode", "full-set-pass" if self.full_set_request and not self.reexecute_gates else "re-execute",',
        '                                "--pre-a3-mode", "run", "--mode", "full-set-pass" if self.full_set_request and not self.reexecute_gates else "re-execute",',
        "    if args.audit_reads and not args.reexecute_gates:",
        "                                full_set_work=args.full_set_work)",
    ],
    "tools/ci/entry_steps.py": [
        "from dataclasses import dataclass",
        "    for client, steps in STEPS_BY_CLIENT.items():",
        '        """本轮客户端的步骤表（步骤与依赖两客户端相同，输入声明按客户端）。"""',
        "",
        "        return STEPS_BY_CLIENT[self.client]",
        "        steps = STEP_BY_ID_BY_CLIENT[self.client]",
    ],
    "tools/ci/entry_gates.py": [
        "                withheld[gate_id] = (f\"本次运行承接了 {len(records[gate_id]['inherited_units'])} 个单元（全集通过模式），要写以运行清单为证据的\"",
        '                            executed_as="入口门禁一次运行（全集通过模式）：测试组全集由本次执行的单元与承接以往运行正式执行记录的单元"',
        '                            executed_as="入口门禁一次运行（全集通过模式）：EGRESS_SPEC_CHECKS 子检查各一个单元，本次执行的与承接以往运行"',
    ],
    "tools/official_client_capture/codex_upgrade_vc_receipt.py": [
        "def _p0_v2_record(store: _UnitRecordStore, manifest: Mapping[str, Any], entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:",
        '    """核验运行清单里的一个单元项，返回（执行记录，问题）。问题非空即拒绝签发。"""',
        '    if disposition == "inherited" and manifest.get("mode") != UNIT_FULL_SET_PASS:',
        '    """一份 v2 门禁证据的逐条重验（规则见本节说明），返回按记录重算的结果。"""',
        "        record, found = _p0_v2_record(store, manifest, entry)",
        '    manifest = _p0_v2_manifest(store, egress.get("unit_manifest"))',
        "        gate_id: _verify_p0_gate_v2(store, manifest, shapes[role], gate_id, release_identity)",
        "    return {",
    ],
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": [
        "                           name: str, client: str = DEFAULT_CLIENT) -> tuple[dict[str, Any] | None, int, list[str]]:",
        '    场景通过。单元 ID 按客户端的前缀拼（Claude 的记录只认 ``pre-a3:claude:`` 单元，不承接 Codex 的同名记录）。"""',
        '    if disposition == "inherited" and manifest.get("mode") != "full-set-pass":',
        "    store: _RecordStore, manifest: Mapping[str, Any], client: str = DEFAULT_CLIENT,",
        "        row, count, problems = _scenario_from_records(store, manifest, found[0], name, client)",
        "    manifest = _published_manifest(store, Path(unit_manifest))",
        "    report, failed, attempts = _scenarios_from_records(store, manifest, client)",
        "    report, failed, attempts = _scenarios_from_records(store, manifest, client)",
    ],
}
# 入口门禁脚本代入 Codex 的客户端表取值后与基线的差异（- 基线，+ 改动后）：用法说明多三项；新增的选项、互斥、回退与只写提案
# 分支只在给了承接请求、提案目录或 --mode unit-inherit 时生效；读集审计与宿主输入两条用法检查多一个逐单元承接的豁免（Codex
# 的条件值不变）；工作区沿用、Go 预热与测试树保留三处条件多一个「或是逐单元承接模式」（Codex 不进入这个模式，条件不变）；
# 执行器命令末尾追加的承接参数在 Codex 下为空；执行器之后的提案分支只在写提案时生效。
ENTRY_GATES_RESIDUAL: list[str] = [
    "-usage() { echo \"用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--with-gates <门禁项,…>] [--require-deployed] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute] [--full-set-request <JSON>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]\" >&2; }",
    "+usage() { echo \"用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--with-gates <门禁项,…>] [--require-deployed] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]\" >&2; }",
    "+UNIT_INHERIT_REQUEST=\"\"; UNIT_INHERIT_STAGE=pre-ledger; UNIT_INHERIT_ARGS=(); UNIT_INHERIT_PROPOSE=\"\"",
    "+    --unit-inherit-request) UNIT_INHERIT_REQUEST=\"${2:-}\"; shift 2 || { usage; exit 2; } ;;",
    "+    --unit-inherit-stage) UNIT_INHERIT_STAGE=\"${2:-}\"; shift 2 || { usage; exit 2; } ;;",
    "+    --unit-inherit-propose) UNIT_INHERIT_PROPOSE=\"${2:-}\"; shift 2 || { usage; exit 2; } ;;",
    "-case \"$MODE\" in full-set-pass|re-execute) ;; *) echo \"未知的模式：$MODE\" >&2; exit 2 ;; esac",
    "+case \"$MODE\" in full-set-pass|re-execute|unit-inherit) ;; *) echo \"未知的模式：$MODE\" >&2; exit 2 ;; esac",
    "+if [ -n \"$UNIT_INHERIT_REQUEST\" ] && [ -n \"$FULL_SET_REQUEST\" ]; then echo \"逐单元承接请求不能与 B-09 全集请求同用\" >&2; exit 2; fi",
    "+if [ -n \"$UNIT_INHERIT_PROPOSE\" ]; then",
    "+  if [[ \"$UNIT_INHERIT_PROPOSE\" != /* ]] || [ -e \"$UNIT_INHERIT_PROPOSE\" ]; then echo \"承接提案目录必须是尚不存在的绝对路径：$UNIT_INHERIT_PROPOSE\" >&2; exit 2; fi",
    "+  if [ -n \"$UNIT_INHERIT_REQUEST\" ] || [ -n \"$FULL_SET_REQUEST\" ]; then echo \"写承接提案时不给承接请求\" >&2; exit 2; fi",
    "+  if [ \"$MODE_EXPLICIT\" = true ] && [ \"$MODE\" != unit-inherit ]; then echo \"--unit-inherit-propose 只与 --mode unit-inherit 同用\" >&2; exit 2; fi",
    "+  MODE=unit-inherit; REQUIRE_DEPLOYED=true",
    "+fi",
    "-if [ \"$AUDIT_READS\" = true ] && [ \"$MODE\" != re-execute ]; then echo \"读集审计要真跑才有读集：--audit-reads 只许与 --mode re-execute 同用\" >&2; exit 2; fi",
    "-if { [ \"$AUDIT_STRICT\" = true ] || [ -n \"$AUDIT_HOST_INPUTS\" ] || [ \"${#INPUT_SCOPE_ARGS[@]}\" -gt 0 ] || [ \"${#AUDIT_POLICY_ARGS[@]}\" -gt 0 ]; } && { [ \"$AUDIT_READS\" != true ] || [ \"$AUDIT_STRICT\" != true ] || [ \"$MODE\" != re-execute ]; } && [ -z \"$FULL_SET_REQUEST\" ]; then",
    "+if [ -n \"$UNIT_INHERIT_REQUEST\" ]; then",
    "+  if [[ \"$UNIT_INHERIT_REQUEST\" != /* ]]; then echo \"逐单元承接请求必须是绝对路径：$UNIT_INHERIT_REQUEST\" >&2; exit 2; fi",
    "+  REQUIRE_DEPLOYED=true",
    "+  if [ \"$MODE_EXPLICIT\" = false ]; then MODE=unit-inherit; fi",
    "+  if [ \"$MODE\" != unit-inherit ]; then echo \"--unit-inherit-request 只与 --mode unit-inherit 同用\" >&2; exit 2; fi",
    "+elif [ \"$MODE\" = unit-inherit ] && [ -z \"$UNIT_INHERIT_PROPOSE\" ]; then",
    "+  echo \"B5a-08：未提供逐单元承接请求，回退重新执行全集\"",
    "+  MODE=re-execute",
    "+fi",
    "+if [ \"$MODE\" = unit-inherit ] && ! [[ \"$UNIT_INHERIT_STAGE\" =~ ^(pre-ledger|post-ledger|formal:[A-Za-z0-9][A-Za-z0-9._-]*)$ ]]; then",
    "+  echo \"未知的入口阶段：$UNIT_INHERIT_STAGE\" >&2; exit 2",
    "+fi",
    "+if [ \"$AUDIT_READS\" = true ] && [ \"$MODE\" != re-execute ] && [ \"$MODE\" != unit-inherit ]; then echo \"读集审计要真跑才有读集：--audit-reads 只许与 --mode re-execute 同用\" >&2; exit 2; fi",
    "+if { [ \"$AUDIT_STRICT\" = true ] || [ -n \"$AUDIT_HOST_INPUTS\" ] || [ \"${#INPUT_SCOPE_ARGS[@]}\" -gt 0 ] || [ \"${#AUDIT_POLICY_ARGS[@]}\" -gt 0 ]; } && { [ \"$AUDIT_READS\" != true ] || [ \"$AUDIT_STRICT\" != true ] || [ \"$MODE\" != re-execute ]; } && [ -z \"$FULL_SET_REQUEST\" ] && [ \"$MODE\" != unit-inherit ]; then",
    "+fi",
    "+if [ \"$MODE\" = unit-inherit ]; then",
    "+  if [ -n \"$AUDIT_HOST_INPUTS\" ] || [ \"${#INPUT_SCOPE_ARGS[@]}\" -gt 0 ]; then echo \"逐单元承接不接受宿主输入清单与收窄提案（只许 --mode re-execute）\" >&2; exit 2; fi",
    "+  if [ \"$AUDIT_STRICT\" = true ] && [ \"$AUDIT_READS\" != true ]; then echo \"严格读集审计必须同时启用 --audit-reads\" >&2; exit 2; fi",
    "-if [ -n \"$FULL_SET_REQUEST\" ] && [ -d \"$TREE\" ]; then",
    "+if { [ -n \"$FULL_SET_REQUEST\" ] || [ \"$MODE\" = unit-inherit ]; } && [ -d \"$TREE\" ]; then",
    "+  elif [ \"$MODE\" = unit-inherit ]; then",
    "+    echo \"B5a-08：原工作区无法重放，重建测试树（登记了宿主快照的来源记录按实际输入判失效，只重跑这些单元）\"",
    "-if [ \"$GO_WARMUP\" = auto ] && [ -z \"$FULL_SET_REQUEST\" ] && { [ \"$PLAN_PROFILE\" = entry ] || [ \"$PLAN_PROFILE\" = full-gates ]; }; then",
    "+if [ \"$GO_WARMUP\" = auto ] && [ -z \"$FULL_SET_REQUEST\" ] && [ \"$MODE\" != unit-inherit ] && { [ \"$PLAN_PROFILE\" = entry ] || [ \"$PLAN_PROFILE\" = full-gates ]; }; then",
    "+if [ \"$MODE\" = unit-inherit ]; then",
    "+  UNIT_INHERIT_ARGS=(--unit-inherit-client codex --unit-inherit-stage \"$UNIT_INHERIT_STAGE\" --unit-inherit-control \"$D/control\")",
    "+  if [ -n \"$UNIT_INHERIT_PROPOSE\" ]; then",
    "+    UNIT_INHERIT_ARGS+=(--decide-only --unit-inherit-propose \"$UNIT_INHERIT_PROPOSE\")",
    "+  else",
    "+    UNIT_INHERIT_ARGS+=(--unit-inherit-request \"$UNIT_INHERIT_REQUEST\")",
    "+  fi",
    "+fi",
    "-    --mode \"$MODE\" --record-store \"$STORE\" ${AUDIT_ARGS[@]+\"${AUDIT_ARGS[@]}\"} ${FULL_SET_ARGS[@]+\"${FULL_SET_ARGS[@]}\"} ) > \"$OUT/executor.log\" 2>&1 < /dev/null || EXEC_RC=$?",
    "+    --mode \"$MODE\" --record-store \"$STORE\" ${AUDIT_ARGS[@]+\"${AUDIT_ARGS[@]}\"} ${FULL_SET_ARGS[@]+\"${FULL_SET_ARGS[@]}\"} ${UNIT_INHERIT_ARGS[@]+\"${UNIT_INHERIT_ARGS[@]}\"} ) > \"$OUT/executor.log\" 2>&1 < /dev/null || EXEC_RC=$?",
    "+if [ -n \"$UNIT_INHERIT_PROPOSE\" ]; then",
    "+  if [ \"$EXEC_RC\" -ne 0 ] || [ ! -f \"$UNIT_INHERIT_PROPOSE/binding.json\" ]; then",
    "+    echo \"ENTRY_GATES_ABORTED：承接提案没写出（rc=${EXEC_RC}）；日志 ${OUT}/executor.log\"; exit 3",
    "+  fi",
    "+  echo \"UNIT_INHERIT_PROPOSAL ${UNIT_INHERIT_PROPOSE}：binding.json 的 binding_sha256 交两类批准签字（测试树与缓存保留在 ${WORK}）\"",
    "+  exit 0",
    "+fi",
    "-  if [ \"$AUDIT_STRICT\" = true ] || [ -n \"$FULL_SET_REQUEST\" ]; then",
    "+  if [ \"$AUDIT_STRICT\" = true ] || [ -n \"$FULL_SET_REQUEST\" ] || [ \"$MODE\" = unit-inherit ]; then",
]
ENTRY_GATES = "tools/arm64_capture_driver/driver/entry-gates.sh"
# B5a-09 改过的文件（上面 B5a-08 的残差对照因此跳过）：以 B5a-09 开工时的提交为新基线再核一次删改的行，每处 Codex 下等价：
# * entry_steps.py：零请求 smoke 一步登记的身份摘要改为取客户端表（Codex 的取值就是原来的 control_sha256，Codex 步骤声明逐字
#   不变，见 test_codex_step_table_and_gate_inputs_match_the_baseline）；注释补一句说明。
B5A09_BASELINE_COMMIT = "5ad660473"
B5A09_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_steps.py": "f164437889a9620f26c089c313af3c61f03bf912c8846fc79a65981477f33335",
}
B5A09_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_steps.py": [
        "        # 零请求 smoke：经 provenance 传递导入编排器但只导入不调用（control 层），运行时导入测试模块与夹具。",
        '            Input("managed", "identity", "control_sha256"), *tests_trees,',
    ],
}
# B5a-10 又改了入口步骤（上面 B5a-09 那一项因此跳过）：以 B5a-10 开工时的提交为新基线，只新增了部署核对的函数与子命令
# （deploy-check，入口门禁 --require-deployed-all 用），没有删改任何已有的行。
B5A10_BASELINE_COMMIT = "3cf9c165e"
B5A10_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_steps.py": "b6f4f66fba26cd448e6e590d7cf216ebcf7fe8dfacb5dfc296a410f2ac501be4",
}
B5A10_SOURCE_REMOVED_LINES: dict[str, list[str]] = {"tools/ci/entry_steps.py": []}
LATER_SOURCE_BASELINES: tuple[tuple[str, str, dict[str, str], dict[str, list[str]]], ...] = (
    ("B5a-09", B5A09_BASELINE_COMMIT, B5A09_SOURCE_DIGESTS, B5A09_SOURCE_REMOVED_LINES),
    ("B5a-10", B5A10_BASELINE_COMMIT, B5A10_SOURCE_DIGESTS, B5A10_SOURCE_REMOVED_LINES),
)
# B5a-08 之后按设计改过的入口步骤声明：客户端 → 步骤 → (新增的输入名, 移除的输入名, 理由)。Codex 一侧不得出现。
STEP_CHANGES_AFTER_B5A08: dict[str, dict[str, tuple[list[str], list[str], str]]] = {
    "claude": {
        "zero-request-smoke": (["identity:evidence_semantics_sha256"], [],
                               "B5a-09：Claude smoke 依赖的 provenance 在 Claude 身份的 evidence 层，登记的身份摘要取客户端表"),
        "client-launch-probe": (["driver:claude_launch_probe_runner.py"], ["driver:client_launch_probe_runner.py"],
                                "B2-10：启动探测运行器按客户端表取（launch_probe_runner），Claude 用 claude_launch_probe_runner.py；"
                                "Codex 仍是 client_launch_probe_runner.py"),
    },
}


def removed_lines(relative: str, old: str) -> list[str]:
    new = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
    return [line[1:] for line in difflib.unified_diff(old.splitlines(), new, lineterm="", n=0)
            if line.startswith("-") and not line.startswith("---")]


class SourceResidualCodexParityTests(unittest.TestCase):
    def _source(self, relative: str) -> str:
        current = (REPO_ROOT / relative).read_bytes()
        if relative in SOURCE_DIGESTS and hashlib.sha256(current).hexdigest() != SOURCE_DIGESTS[relative]:
            self.skipTest(f"{relative} 在 B5a-08 之后又改过：以新的基线提交重新对照")
        source = git_source(relative)
        if source is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的 {relative}")
        return source.decode("utf-8")

    def test_only_the_registered_lines_were_removed_or_rewritten(self) -> None:
        self.assertEqual(sorted(SOURCE_DIGESTS), sorted([*SOURCE_REMOVED_LINES, ENTRY_GATES]))
        for relative, expected in SOURCE_REMOVED_LINES.items():
            with self.subTest(relative):
                self.assertEqual(removed_lines(relative, self._source(relative)), expected)

    def test_later_batches_only_rewrote_the_registered_lines(self) -> None:
        self.assertNotIn("codex", STEP_CHANGES_AFTER_B5A08, "Codex 的步骤声明不得改")
        for batch, commit, digests, removed in LATER_SOURCE_BASELINES:
            self.assertEqual(sorted(digests), sorted(removed), batch)
            for relative, digest in digests.items():
                with self.subTest(batch=batch, source=relative):
                    if hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() != digest:
                        self.skipTest(f"{relative} 在 {batch} 之后又改过：以新的基线提交重新对照")
                    source = git_source(relative, commit)
                    if source is None:
                        self.skipTest(f"读不到基线提交 {commit} 的 {relative}")
                    self.assertEqual(removed_lines(relative, source.decode("utf-8")), removed[relative])

    def test_entry_gates_script_with_codex_values_differs_only_in_registered_lines(self) -> None:
        old = self._source(ENTRY_GATES)
        values = b5a06._codex_shell_values()
        new = (REPO_ROOT / ENTRY_GATES).read_text(encoding="utf-8")
        self.assertEqual(b5a06.residual(b5a06.codex_effective(old, values), b5a06.codex_effective(new, values)), ENTRY_GATES_RESIDUAL)

    def test_codex_step_table_and_gate_inputs_match_the_baseline(self) -> None:
        old = load_baseline("tools/ci/entry_steps.py", "entry_steps_before_b5a08")
        if old is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的入口步骤")
        for client in es.CT.CLIENTS:
            with self.subTest(client):
                current, baseline = es.STEPS_BY_CLIENT[client], old.STEPS_BY_CLIENT[client]
                self.assertEqual([step.step_id for step in current], [step.step_id for step in baseline])
                # B5a-08 之后按设计改过的步骤声明只有登记的那几项（只在 Claude 一侧）；其余步骤逐字相同。
                changed = STEP_CHANGES_AFTER_B5A08.get(client, {})
                for now, before in zip(current, baseline):
                    if now.step_id in changed:
                        # 基线模块的 Input 是另一个类对象：按声明内容比较。
                        now_items, before_items = [item.as_json() for item in now.inputs], [item.as_json() for item in before.inputs]
                        added = [item.name for item in now.inputs if item.as_json() not in before_items]
                        removed = [item.name for item in before.inputs if item.as_json() not in now_items]
                        self.assertEqual((added, removed), (changed[now.step_id][0], changed[now.step_id][1]), now.step_id)
                        continue
                    self.assertEqual(now.spec_sha256(), before.spec_sha256(), f"不用逐单元承接时步骤表逐字相同：{now.step_id}")
                as_json = lambda table: {key: [item.as_json() for item in value] for key, value in table.items()}  # noqa: E731
                self.assertEqual(as_json(es.GATE_INPUTS_BY_CLIENT[client]), as_json(old.GATE_INPUTS_BY_CLIENT[client]))
                inherit = es.STEPS_BY_CLIENT_UNIT_INHERIT[client]
                for plain, extended in zip(es.STEPS_BY_CLIENT[client], inherit):
                    added = [item.name for item in extended.inputs if item not in plain.inputs]
                    expected = ["file:{UNIT_INHERIT_REQUEST}", "driver:unit_inherit.py"] if plain.step_id in es.UNIT_INHERIT_STEPS else []
                    self.assertEqual(added, expected, plain.step_id)
        params = {"D": "/data", "CLIENT": "codex"}
        self.assertEqual(es.Context(params=params, driver_dir=Path("/drv"), steps_dir=Path("/steps")).steps, es.STEPS_BY_CLIENT["codex"])
        self.assertIs(es.Context(params={**params, "UNIT_INHERIT_REQUEST": "/r.json"}, driver_dir=Path("/drv"), steps_dir=Path("/steps")).steps,
                      es.STEPS_BY_CLIENT_UNIT_INHERIT["codex"])

    def test_orchestrator_gate_mode_matches_the_baseline_expression(self) -> None:
        from tools.ci import entry_orchestrator as eo

        for full_set, reexecute in ((None, False), (Path("/r.json"), False), (None, True), (Path("/r.json"), True)):
            with self.subTest(full_set=full_set, reexecute=reexecute):
                orchestrator = types.SimpleNamespace(full_set_request=full_set, reexecute_gates=reexecute, unit_inherit_request=None)
                baseline = "full-set-pass" if full_set and not reexecute else "re-execute"
                self.assertEqual(eo.Orchestrator._gates_mode(orchestrator), baseline)
        inherit = types.SimpleNamespace(full_set_request=None, reexecute_gates=False, unit_inherit_request=Path("/u.json"))
        self.assertEqual(eo.Orchestrator._gates_mode(inherit), "unit-inherit")


if __name__ == "__main__":
    unittest.main()
