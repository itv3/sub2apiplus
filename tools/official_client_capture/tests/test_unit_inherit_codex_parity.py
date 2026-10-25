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
基线读不到（浅克隆）时整条用例跳过（父级 ``skipTest``）并写明原因。源码残差按批登记：B5a-08 以 ``BASELINE_COMMIT`` 为基线
（``SOURCE_DIGESTS``／``SOURCE_REMOVED_LINES``），B5a-09、B5a-10 改过的入口步骤以该批开工时的提交为新基线
（``LATER_SOURCE_BASELINES``）；5a 收口（B5a-11）把对照基线统一换成收口时的主线头（与驱动脚本同一个
``B5A11_BASELINE_COMMIT``），七个文件都登记在这一批、删改行为空，之后改动这些文件的批次照样在末尾追加一项。入口门禁脚本在
B5a-08 之后的对照在 B5a-06 的对照模块里。

某个文件在某一批之后又改过（摘要不再是该批登记的那份）时，那一批的残差对照不再执行，改为核对接管：当前内容必须由另一批
登记过（入口门禁脚本也认 B5a-06 对照模块里登记的批次），没有任何一批登记就失败。子测试里一律不跳过（见
``ParitySkipDisciplineTests``）。本模块读真实仓库的 git 历史，按 ``unit_records.GIT_READERS`` 登记。
"""

from __future__ import annotations

import ast
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
import textwrap
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

# B5a-08 改动后各文件的内容摘要：之后又改过就不再对照残差，改为核对接管（当前内容由之后的某一批登记）。
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
# 5a 收口（B5a-11）的统一对照基线（与驱动脚本同一个提交）：上面登记过的七个文件都登记在这一批，基线就是当前内容，删改行为空。
# B5a-08 到这里没有逐行登记的几段（B2-10、B2-11 第二部分、B5a-09 续改入口编排器与入口步骤，B5a-05 与 5a 收口改 pre-A3 认证模块）
# 在收口时一次性核对过：入口编排器只在命令末尾追加 Codex 为空的客户端参数、新增 Codex 模板为空时不进的分支并改写注释；入口步骤
# 的启动探测运行器改取客户端表（Codex 取值就是原文件名）、新增 Codex 为空的表内输入（Codex 步骤表逐字不变由
# test_codex_step_table_and_gate_inputs_match_the_baseline 守住）；pre-A3 认证模块的三段由 test_pre_a3_codex_parity 逐项守住。
B5A11_BASELINE_COMMIT = b5a06.B5A11_BASELINE_COMMIT
B5A11_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_gates.py": "7903c17a9c1fc86a788b87027ffacb6fabe0a8b1a649fdcf1dbd3012f65ad8f0",
    "tools/ci/entry_orchestrator.py": "269c9c7f9fca94e96f7a0f6dcea7d1a23155cb3a067229e34df25ac78464fcaf",
    "tools/ci/entry_steps.py": "f0dbc163254f4a3f2b4e8147c5f6b82b15d9ab5b5748f36186074b65345421ae",
    "tools/ci/unit_executor.py": "61f83789666a8851203ac519c98b21bc80ab57c7c23ad5a28181921593c89c4c",
    "tools/ci/unit_records.py": "9bdc1f8ea5831945781585b0129337566a0cd4284b6c637b01fb34ce41995c31",
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "04131954074a36e6e02d42622da707fb52d2097578da66b3731bc039d29ef0b5",
    "tools/official_client_capture/codex_upgrade_vc_receipt.py": "019f19b5cf6acf56e07b04981a9001e6b1ff33a3805a12da489928ce254661a5",
}
B5A11_SOURCE_REMOVED_LINES: dict[str, list[str]] = {relative: [] for relative in B5A11_SOURCE_DIGESTS}
# B5b-01a（Claude 的 Campaign 用途按参数 CAMPAIGN_PURPOSE）改了入口编排器与入口步骤：以 B5b-01a 开工时的提交为基线（与驱动
# 脚本同一个 B5B01A_BASELINE_COMMIT；它到收口基线之间这两个文件没有变化），各只改写一行，Codex 下等价：
# * entry_orchestrator.py：建计时账本的 --campaign-purpose 改为「客户端表登记了用途参数键就取参数，否则 production_replacement」，
#   Codex 没有用途参数键，命令行逐字不变（B5a-06 对照模块的 EntryLedgerAndPreflightCodexParityTests 逐字核对）；
# * entry_steps.py：计时账本一步的参数输入末尾追加客户端表的用途参数键，Codex 为空，步骤声明逐字不变
#   （test_codex_step_table_and_gate_inputs_match_the_baseline 守住）。
B5B01A_BASELINE_COMMIT = b5a06.B5B01A_BASELINE_COMMIT
B5B01A_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_orchestrator.py": "232ed80834e96d35a2aaa930af64db484595a81ff9328a23a47699d52965723d",
    "tools/ci/entry_steps.py": "810272e0b0a663e9d2fa588d33b378080b840a2d54a09fcf5bb19e4013ba0904",
}
B5B01A_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_orchestrator.py": [
        '                                        "--campaign-purpose", "production_replacement",',
    ],
    "tools/ci/entry_steps.py": [
        '            *_params("UP", "BASELINE_VERSION", "TARGET_VERSION", "EVIDENCE_DECISION", "STAGE_BUDGETS"),',
    ],
}
# B5b-03（Claude 故障恢复链 recovery-chain）改了 pre-A3 认证模块：以 B5b-03 开工时的主线头（B4-06b 合入后）为基线，只在 Claude
# 的两张字面量登记表 CLAUDE_REAL_CHAIN_IDS、CLAUDE_SCENARIOS 与派发秒数表 SCENARIO_SECONDS 末尾追加 vc-chain.claude-recovery-chain
# 一项（连同说明注释），没有删改任何已有的行；Codex 场景表、单元、命令、秒数与收据不变（test_pre_a3_codex_parity 逐项守住）。
B5B03_BASELINE_COMMIT = "322d3ea62ba141227a6f485d0440cfd4e7f22360"
B5B03_SOURCE_DIGESTS: dict[str, str] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "4e58938dae2753cfb666f517e229d7212a66c6a26a9d9ae9c4693d61134dd484",
}
B5B03_SOURCE_REMOVED_LINES: dict[str, list[str]] = {"tools/official_client_capture/codex_upgrade_pre_a3_certification.py": []}
# B4-05b-1 把修复轮最终的候选恢复脚本改为取客户端表，Codex 表值仍是 vc5-recover.sh，命令与幂等跳过文案不变。
# 下列删改行含说明文字与原常量；完整修复轮行为由 test_driver_client_table_codex_parity 的
# FixAndContinueCodexParityTests 对照 B5b-01a 基线，Claude 分派由 test_arm64_driver_fix_and_continue 覆盖。
B405B1_BASELINE_COMMIT = "3d114ee8973f2d07090c72f2c3ef3773d166dd9b"
B405B1_SOURCE_DIGESTS: dict[str, str] = {
    "tools/arm64_capture_driver/driver/fix_and_continue.py": "1148d8ba53d46bd460e2c68c30cbc6cc11e08a0463a2ac1557a8e3e95a73a3a9",
}
B405B1_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/arm64_capture_driver/driver/fix_and_continue.py": [
        "受管命令据此调用。",
        'CANDIDATE_RECOVER_SCRIPT = "vc5-recover.sh"',
        '''        return skip(params, step, f"本轮已启动过 {CANDIDATE_RECOVER_SCRIPT}（日志 {summary.get('log')}），不重复派发{changed}",''',
        '    script = Path(params["DRIVER_TARGET"]) / "driver" / CANDIDATE_RECOVER_SCRIPT',
    ],
}
# B5b-03 B 组仍并入既有 recovery-chain：只扩展 Claude 场景说明，并把它的预算估计从 85 秒调为 340 秒。
# 不新增链、不改 Codex 的任何登记值；A 组的第六条链等其实现及验收完成再登记。
B5B03B_BASELINE_COMMIT = "3d114ee8973f2d07090c72f2c3ef3773d166dd9b"
B5B03B_SOURCE_DIGESTS: dict[str, str] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "ef339a76a7843793fd0fab7d18b18248b518c3476fd64dc25cf2ab2324901a19",
}
B5B03B_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": [
        '        "多根部署各故障点与四类跨客户端误用，逐场景核对可信结果保留、合法闭集、不重复入账、累计时间不重置、历史不覆盖与续跑入口",',
        '    # B5b-03：recovery-chain 暂按本机降级模式实测（约 85 秒，取整）登记，ARM64 私有命名空间预跑后按实测替换。',
        '    "vc-chain.claude-recovery-chain": 85.0,',
    ],
}
# B5b-05a 只修正 Claude 场景名的完整前缀解析；Codex 仍移除原 pre-a3:。
# 实测表与调度策略表保持原值，全部 Codex 场景的规划额度由客户端分组测试逐项核对。
B5B05A_BASELINE_COMMIT = "bfa0b1923e15d4c68b5fdfc1d721b6d04eaa1d7b"
B5B05A_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_gates.py": "8d220e8011560be5a8b8fcc78921070adf1ad98957219e2596f7ecd0679b2c7b",
}
B5B05A_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_gates.py": [
        '                cores, seconds = pre_a3_quota(str(unit["unit_id"]).removeprefix(PRE_A3_PREFIX))',
    ],
}
# H2 承接保护仅在真实测试当前事实中追加两模块的拒绝原因，输入、规格、调度与 Codex 其他单元保持原值。
# 本次只有新增行，固定基线的删除侧残差为空；两模块的拆块和独占保护由执行器边界专项核对。
H2_INHERIT_GUARD_BASELINE_COMMIT = "f7b09b60a2d1e3c5256f96122e4ddb396df39a89"
H2_INHERIT_GUARD_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/unit_executor.py": "f86da3f3e781b116be3cd040ef4396a6868dd96164b7398319ced17b0ead9e41",
}
H2_INHERIT_GUARD_SOURCE_REMOVED_LINES: dict[str, list[str]] = {"tools/ci/unit_executor.py": []}
# cfc3ae053 的三条 Claude 可执行入口以实际父提交为基准；只新增 Claude 三张表的条目和待验说明。
# 前序 f3a9dd3b0 的恢复链说明与预算已由 B5b-03 B 组登记，不改其历史摘要或残差。
# 当前新增登记不宣称三条链已认证，Codex 场景表、命令、预算与收据由既有行为对照继续守住。
CLAUDE_ENTRY_REGISTRATION_BASELINE_COMMIT = "d9d9787e18988cb579189fd3db5134d6550765a9"
CLAUDE_ENTRY_REGISTRATION_SOURCE_DIGESTS: dict[str, str] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "b24980b37b5e6dce2a46ca78e7a2380fdde665b70f177ce811ac4e9914d59ae6",
}
CLAUDE_ENTRY_REGISTRATION_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": [],
}
# 828b25627 的请求上界占用/结清以实际父提交为基准，保留完整源码摘要并逐行登记五处删改。
# 新派发余额仅在 Claude 新事件出现时扣除活动占用；无新事件的历史 head 与 Codex 旧路径保持原字节形态。
# 这份登记只接管已经批准的共享实现字节，不开启正式来源或产品执行；预算语义由对应专项另行校验。
CLAUDE_REQUEST_RESERVATION_BASELINE_COMMIT = "d009c87fbf3eb701a6b6d508ae7795752ceb107e"
CLAUDE_REQUEST_RESERVATION_SOURCE_DIGESTS: dict[str, str] = {
    "tools/official_client_capture/codex_upgrade_project_ledger.py": "6bec6c17d2db583b3eb1042f7861ec3c376d6107e7f949e232a2806b2ec8bf95",
}
CLAUDE_REQUEST_RESERVATION_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/official_client_capture/codex_upgrade_project_ledger.py": [
        '    if head["remaining_live_requests"] is not None and head["remaining_live_requests"] <= 0:',
        "        remaining = self.remaining_live_requests",
        '        remaining = current.get("remaining_live_requests")',
        '    remaining = head.get("remaining_live_requests")',
        '        if head["remaining_live_requests"] is not None and head["remaining_live_requests"] <= 0:',
    ],
}
# revision 八场景接入准备只追加 Claude 表项及未实测占位；基线是本任务实际父提交。
# 此登记只核对源码归属与删除侧残差，不证明故障链、ARM64 实测或发布认证已通过。
CLAUDE_REVISION_ENTRY_BASELINE_COMMIT = "0dc1bcf724372bdc8afe464d8ed9bf8ee9bfb3f0"
CLAUDE_REVISION_ENTRY_SOURCE_DIGESTS: dict[str, str] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": "5573a0ecb649a83651a06359204a42a0c9af7e71da6b4b3bd7c7e6a68876d227",
    "tools/official_client_capture/certify_release.py": "a6b2c1f66dd2b86c4318f777e2d53ca6c1c0fdd1bb3d1ab5fb64ab71e3756ea8",
}
CLAUDE_REVISION_ENTRY_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py": ["        \"r2 与评估器修复仍在私有待验链，登记八条不代表 B5b-03 完整覆盖\","],
    "tools/official_client_capture/certify_release.py": [],
}
# B5b-03 时间合同 v2：仅三条 Claude 故障链独立预算，Codex 所有单元逐字不变。
CLAUDE_TIME_BASELINE_COMMIT = '8e4713ad8180c6f82b279bad58a66b60602dceb9'
CLAUDE_TIME_SOURCE_DIGESTS = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': '73ddf7c5a2fe1637a2873e922380876159608d4ce79dbc223514ac533ee3cae0'}
CLAUDE_TIME_SOURCE_REMOVED_LINES = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': ['            "timeout_seconds": REAL_CHAIN_TIMEOUT_SECONDS if name in real_chains else SCENARIO_TIMEOUT_SECONDS,']}
# B5b-02：录制链也采用同一方法预算与外层余量，保留 Codex 和四条基础链原上限。
CLAUDE_RECORDED_TIME_BASELINE = '622b0b5f7f214eaece78c48830b90c0f81cb498d'
CLAUDE_RECORDED_TIME_DIGESTS = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': 'ce9ba9c06ff59a9a1f0c5766bff61dd431b0490821d532ad27934718eec158ce'}
CLAUDE_RECORDED_TIME_REMOVED = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': []}
# 连续世界按 ARM64 交付实测扩大至两小时，其余方法和 Codex 上限不变。
CLAUDE_CONTINUOUS_TIME_BASELINE = '980272b7114f8daf046bf761a84ba7d544ee6ab8'
CLAUDE_CONTINUOUS_TIME_DIGESTS = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': '2a115f4de8606ae20020dcbdc849eaca2b3e24263a9aded0272756dccaa0379c'}
CLAUDE_CONTINUOUS_TIME_REMOVED = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': ['# 七／八场景连续链各算一个方法，仍在同一 Campaign 内完整执行。三层各留一分钟收尾，', '    """返回三条 Claude 故障链的唯一时间合同；正式入口与外层执行器消费相同数值。"""', '', '    count = CLAUDE_FAULT_METHOD_COUNTS[name]', '    work_seconds = count * CLAUDE_FAULT_METHOD_SECONDS', '        "method_timeout_seconds": CLAUDE_FAULT_METHOD_SECONDS,', '    return {**claude_fault_time_budget("vc-chain.claude-late-stage-faults"),']}
# a0c564377 的实际父提交为 4cc10d585：入口门禁与步骤同时绑定 Claude 三份来源，并给录制产物独立目录。
# Codex 的来源键表为空，新增来源解析与录制目录分支不进入；原有参数、步骤声明及门禁输入由逐字行为对照守住。
# e104a6aae 只登记执行器配置中的十三方法拆分与驱动清单，未修改这里的 Python 文件，不以它重置源码基线。
CLAUDE_FIXTURE_WIRING_BASELINE_COMMIT = "4cc10d5857177f60e5ad48f85bc1e8f572ee9f68"
CLAUDE_FIXTURE_WIRING_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_gates.py": "445e7893b3dad54c8a98ac69a0a3dd7417d08f7b728eed85dee538d25f0920d8",
    "tools/ci/entry_steps.py": "50405f68c271f27e877365cf517564e35b7dc37923a0b711d230e5f6cd9652ba",
}
CLAUDE_FIXTURE_WIRING_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_gates.py": [
        "from typing import Any, Sequence",
        'def pre_a3_data_root_inputs(data_root: Path, client: str = "codex") -> list[dict[str, Any]]:',
        '    ctx = steps.Context(params={"D": str(Path(data_root).resolve())}, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))',
        '                    merged["inputs"] = pre_a3_inputs(test_file, pre_a3_data_inputs, pre_a3_unit_client(unit["unit_id"]) or "codex")',
        '            data_inputs = pre_a3_data_root_inputs(args.pre_a3_data_root, client) if args.pre_a3_data_root is not None else None',
        '                                  pre_a3_units=args.pre_a3_units, pre_a3_env=_pairs(args.pre_a3_env, "--pre-a3-env") or None,',
    ],
    "tools/ci/entry_steps.py": [
        "    return PRE_A3_DATA_ROOT + tuple(item for item in probes if item not in PRE_A3_DATA_ROOT)",
        "        digest, count = ctx.tree(root, item.option)",
    ],
}
# 0ba4a80f2 的可选并发透传以实际父提交为基线：不带选项时不生成新参数、后台字段或执行器 argv。
# Context.steps 的缺省分支仍返回原表，Context.step 仅从同一张表建索引；Codex 缺省规格与行为继续逐字对照。
# 指定选项时两步追加的输入由并发专项验证；背景模块只有新增行，原全集请求与承接批准规则不变。
ENTRY_PARALLEL_BASELINE_COMMIT = "5b4663bc3ac79d76ddc99b9570d696d76cfb0714"
ENTRY_PARALLEL_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_steps.py": "cefd4e263af2cdc861ee10420fd96f5f83cb98de47f6a4fa21c8554b59d83b95",
    "tools/ci/entry_orchestrator.py": "98116185d5d37eed26d876dc2847745799faeca416ea66f730bf5f79b1d669c7",
    "tools/ci/background_validation.py": "cbf1bf9b18bdd2cf3780e4b101f41154d7bee74194fc3114dd089d9a5198b716",
}
ENTRY_PARALLEL_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_steps.py": [
        "        return (STEPS_BY_CLIENT_UNIT_INHERIT if self.unit_inherit else STEPS_BY_CLIENT)[self.client]",
        "        steps = (STEP_BY_ID_BY_CLIENT_UNIT_INHERIT if self.unit_inherit else STEP_BY_ID_BY_CLIENT)[self.client]",
    ],
    "tools/ci/entry_orchestrator.py": [
        "                 unit_inherit_request: Path | None = None) -> None:",
        "                                full_set_work=args.full_set_work, unit_inherit_request=args.unit_inherit_request)",
    ],
    "tools/ci/background_validation.py": [],
}
# f1ce9dcd3 已把 Claude 私有场景清单全文绑定到两处实际读侧；只追加 Claude 输入，Codex 规格与门禁输入不变。
# 用实际父提交、完整源码摘要与两条删除侧残差接管；不能用当前实现动态生成预期或略过其它字节。
CLAUDE_SCENARIO_FILE_BASELINE_COMMIT = "1409dd43e336b14f049ef980abe37c73555ef361"
CLAUDE_SCENARIO_FILE_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_steps.py": "4bf5e272a16fddea009d5cdb5cce7d4e4e72f5e98bf65842f7c98eb765bf104e",
}
CLAUDE_SCENARIO_FILE_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_steps.py": [
        "            *IDENTITY, DEPLOY_RECEIPT, GUIDE_PART2,",
        '            *_params(*spec["plan_params"]), *IDENTITY, GUIDE_PART2, DEPLOY_TOOL_FILES,',
    ],
}
# 同一入口的实际组合从 entry 切到 full-gates 时，只在显式配套请求下选择另一份独立批准文件。
# 缺省不增加步骤输入，主请求与 B-09 路径保持原样；两份请求的来源授权由真实合同短回归守住。
ENTRY_PROFILE_REQUESTS_BASELINE_COMMIT = "70050bbd61734d36d6fc9ebeaa60e56a31ca5c2f"
ENTRY_PROFILE_REQUESTS_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/entry_steps.py": "34af52b410fe3df68b0a3c2f40ad61d03fad5e913e95a32f01cab34b20018c1c",
    "tools/ci/entry_orchestrator.py": "01518a4b82e37a774cb0425ccdecb2790c4543a1bcfee2edc9c05695f614ec0b",
}
ENTRY_PROFILE_REQUESTS_SOURCE_REMOVED_LINES: dict[str, list[str]] = {
    "tools/ci/entry_steps.py": [
        '        """本轮客户端的步骤表（步骤与依赖两客户端相同，输入声明按客户端；逐单元承接时入口门禁与 pre-A3 另有两项输入）。"""',
    ],
    "tools/ci/entry_orchestrator.py": [
        "                 unit_inherit_request: Path | None = None, gate_parallel: int | None = None) -> None:",
        '                                          *(["--unit-inherit-request", str(self.unit_inherit_request)] if self.unit_inherit_request else []),',
        "                                full_set_work=args.full_set_work, unit_inherit_request=args.unit_inherit_request, gate_parallel=args.gate_parallel)",
    ],
}
# 空跑审计策略透传以实际开工提交为基线：后台只新增显式策略分支，无策略的自动空跑命令与结论不变。
# 该分支不补造逐单元批准、不丢掉策略再跑全集，由原空跑测试内的双客户端边界断言守住。
ENTRY_DRYRUN_AUDIT_BASELINE_COMMIT = "9502304179ecdd5dcc7d02460d1f90fb63ad3e02"
ENTRY_DRYRUN_AUDIT_SOURCE_DIGESTS: dict[str, str] = {
    "tools/ci/background_validation.py": "5232ffa04deef6c3fe5ecae3bc235d5efe33417271c1b9361d1744f34c006ec9",
}
ENTRY_DRYRUN_AUDIT_SOURCE_REMOVED_LINES: dict[str, list[str]] = {"tools/ci/background_validation.py": []}
# df90c329e接线测试单元的明确登记与收窄提案；无policy、无scope仍按原静态输入与Codex旧消费路径。
# 策略校验的ID集合扩至实际测试单元，错误文案相应说明；旧承接、旧记录及未审计执行由原行为对照守住。
READ_SCOPE_HISTORY_BASELINE = '8d72f39f69d4d71b8fef9d8ae56dfa7ab1c7fc63'
READ_SCOPE_HISTORY_DIGESTS = {'tools/ci/entry_gates.py': '8c6e5bc6a4d77985b2114fa01242a5492b01ec9b003c27d9e278ce6e051ca4e4',
 'tools/ci/unit_executor.py': 'f68b1891a6d8e4a499f2e261a9757ead0f1e8bd448d4edbd435cc58c69b663c2',
 'tools/ci/unit_records.py': '5fe0b37ab7f6cb118da98c2123eb1b552bb621dec43c7d644b22f394cb3af2b6'}
READ_SCOPE_HISTORY_REMOVED = {'tools/ci/entry_gates.py': ['        _unit_records_module().validate_audit_policy(audit_policy, {unit["unit_id"] for unit in '
                             'units})',
                             'def apply_input_scopes(manifest: dict[str, Any], scopes: Sequence[dict[str, Any]]) -> None:',
                             '    """收窄提案只用于新一轮验证；重放来源并逐项核对当前命令，保留原门禁集合。"""',
                             '        if unit_id in seen or unit_id not in units:',
                             '            raise ValueError("收窄提案重复或不属于当前命令单元")',
                             '    plan.add_argument("--input-scope", type=Path, action="append", default=[], '
                             'help="本轮命令单元的输入收窄提案，可重复指定；必须重新执行严格审计")'],
 'tools/ci/unit_executor.py': ['    采集工具测试单元的输入由执行器按静态依赖闭包算（同一模块的拆块与独占单元共用一份）；命令单元的输入由门禁清单',
                               '                        if module not in cache:',
                               '                            cache[module] = records.test_unit_inputs(repo, deps, module_file)',
                               '                        inputs = cache[module]',
                               '    scopes = [item["input_scope"] for item in manifest.get("units", []) if "input_scope" in '
                               'item]',
                               '    if scopes:',
                               '        if args.mode != records.RE_EXECUTE or not args.audit_reads or not args.audit_strict:',
                               '            raise ExecutorError("收窄提案必须重新执行严格审计，不能直接承接来源记录")',
                               '        # 清单只是运输载体：执行前再次重放提案，防止绕过 plan 或篡改 inputs。',
                               '        scope_spec = importlib.util.spec_from_file_location("_unit_executor_entry_gates", '
                               'Path(__file__).resolve().with_name("entry_gates.py"))',
                               '        scope_module = importlib.util.module_from_spec(scope_spec)',
                               '        scope_spec.loader.exec_module(scope_module)',
                               '        expected_inputs = {item["unit_id"]: item.get("inputs") for item in manifest["units"] if '
                               '"input_scope" in item}',
                               '        scope_module.apply_input_scopes(manifest, scopes)',
                               '        for item in manifest["units"]:',
                               '            if "input_scope" not in item:',
                               '                continue',
                               '            scope = item["input_scope"]',
                               '            if (item["inputs"] != expected_inputs[item["unit_id"]]',
                               '                    or scope["environment_sha256"] != records.entries_sha256(environment)',
                               '                    or scope["executor_sha256"] != executor["sha256"]):',
                               '                raise ExecutorError("收窄提案的输入、平台环境或执行器与来源不同，须重新建立读集")',
                               '            records.validate_audit_policy(audit_policy, {unit.unit_id for unit in '
                               'command_units})',
                               '    if audit_policy is not None:',
                               '        entry = records.value_entry("policy", "reexecute-only-policy", _sha256(audit_policy))',
                               '        for unit_id, current in currents.items():',
                               '            if unit_id not in audit_policy["units"]:',
                               '                current.inputs = records._unique([*(current.inputs or []), entry])',
                               '                current.inputs_sha256 = records.entries_sha256(current.inputs)',
                               '                current.inheritable = False',
                               '                current.reason = "输入合同未闭合：只验证本次执行结果，禁止承接"'],
 'tools/ci/unit_records.py': ['    """门禁清单为命令单元声明的输入：',
                              'def validate_audit_policy(policy: Any, command_ids: set[str]) -> dict[str, Any]:',
                              '    """登记已建合同的命令；所有未登记单元只能重跑，配置不能静默放开新增单元。"""',
                              '            or not isinstance(policy.get("units"), dict) or set(policy["units"]) - command_ids):',
                              '        raise RecordsError("读集策略须默认重跑，且只能登记当前真实命令单元")']}

# f99137c82仅在显式runtime测试载荷下启用私有结果写入与首次失败保存；原tests-file仍由原JSON读取与写入。
# ReadAuditor仅给测试合同追加test-spec，无runtime-contract不追加输入；原Codex产物用原对照实跑核验。
PRIVATE_RESULT_HISTORY_BASELINE = 'f58e9f6308376f4616aa5b249985beaa6b2eee56'
PRIVATE_RESULT_HISTORY_DIGESTS = {'tools/ci/unit_executor.py': '0413d4c3fc0b0f31acd51c7945e2b56e44aacf5f1dd723b7a5ddde589799dd0c'}
PRIVATE_RESULT_HISTORY_REMOVED = {'tools/ci/unit_executor.py': ['    def __init__(self, *args: Any, failure_path: Path | None = None, unit_id: str = "", '
                               '**kwargs: Any) -> None:',
                               '        if self.failure_path is not None and outcome in {"failed", "error", '
                               '"unexpected_success"}:',
                               '            _write_json(self.failure_path, {"schema_version": UNIT_RESULT_SCHEMA, "unit_id": '
                               'self.unit_id,',
                               '                                           "tests": self.records, "tests_run": self.testsRun,',
                               '                                           "successful": False, "partial": True})',
                               'def run_unit(start: Path, tests_file: Path, result_path: Path) -> int:',
                               '    payload = json.loads(Path(tests_file).read_text(encoding="utf-8"))',
                               "        *args, failure_path=result_path.with_suffix('.observed-failure.json'), "
                               "unit_id=payload['unit_id'], **kwargs))",
                               '    _write_json(',
                               '                                   inputs=current.inputs)',
                               '    unit.add_argument("--tests-file", type=Path, required=True)',
                               '            return run_unit(args.start, args.tests_file, args.result)']}

# 8d72f39f6的显式严格审计与冻结policy参数进入两步输入；缺省不增加参数、输入或命令选项。
# Context.steps返回对象身份不是合同；缺省内容和spec与原Codex逐项对照，不为旧assertIs调整产品实现。
ENTRY_AUDIT_HISTORY_BASELINE = '7af814e5846b632c4af083dabae01384f196d843'
ENTRY_AUDIT_HISTORY_DIGESTS = {'tools/ci/entry_orchestrator.py': 'b648c8f6c894e2c139d972353505cfe418614442493a03af4affb22475205c6d',
 'tools/ci/entry_steps.py': '005a8404d50d75da59e76c7d00ff77aaf85cf5d75b320348c9900ce727856f8f'}
ENTRY_AUDIT_HISTORY_REMOVED = {'tools/ci/entry_orchestrator.py': ['记录、单元一个都不承接；不带时入口门禁用全集通过模式（承接有效的单元执行记录，E3-01）。``--audit-reads``（要和',
                                    '``--reexecute-gates`` 一起带）：入口门禁带读集审计（E3-04），每个单元的实际读取都要落在声明的输入范围里。',
                                    '                 unit_inherit_full_gates_request: Path | None = None) -> None:',
                                    '        # 读集审计（E3-04）：只随重新执行全集一起带（真跑才有读集）。',
                                    '                                          *(["--audit-reads"] if self.audit_reads else []), '
                                    '*source])',
                                    '                                *(["--audit-reads"] if self.audit_reads else []), *source])',
                                    '                        help="入口门禁带读集审计（E3-04，要和 --reexecute-gates 一起带）：实际读取超出声明的输入范围即判失败")',
                                    '                                '
                                    'unit_inherit_full_gates_request=args.unit_inherit_full_gates_request)'],
 'tools/ci/entry_steps.py': ['        if GATE_PARALLEL_PARAM not in self.params:',
                             '            return steps',
                             '        # 显式并发数是这两步实际执行的参数，续跑必须比较它；其它步骤及缺省规格不增加输入。',
                             '        return tuple(replace(step, inputs=step.inputs + _params(GATE_PARALLEL_PARAM))']}

# 本次固定79桥接以真实开工c126为基线；仅显式Claude参数进入新分支，不换Codex缺省参数、规格与产物。
# 预存在认证只调用原消费者，失败拒绝；不生成承接资格、不改旧单元记录。短验与原Codex行为对照分别留证。
ENTRY_RECORDED_BRIDGE_BASELINE = 'c12674dcc0c10e0b26a3d3ed1ae4e6784fd0a986'
ENTRY_RECORDED_BRIDGE_DIGESTS = {'tools/ci/entry_gates.py': '617075c7370c631bcb4a479a23e8cda588b15ccbd1e638ddf05a67508572ee84',
 'tools/ci/entry_orchestrator.py': 'a7cb93ac7ab2abbedf7f7e978c3a0444af028555eb94a054dbb188a51f396236',
 'tools/ci/entry_steps.py': '6fce2bd35f80d658af46b86826388a8f29dae7ce828e34a92299ddf2a6f344c8'}
ENTRY_RECORDED_BRIDGE_REMOVED = {'tools/ci/entry_gates.py': [],
 'tools/ci/entry_orchestrator.py': ['        steps = [step for step, needed in (("entry-gates", gates_run), ("pre-a3", '
                                    'pre_a3_run)) if needed]',
                                    '            certification = (fresh(Path(self.p("PRE_A3_CERTIFICATION"))) if pre_a3_run',
                                    '        except (OrchestratorError, OSError) as error:',
                                    '            inherit_request = self._unit_inherit_request_for("entry" if pre_a3_run else '
                                    '"full-gates")',
                                    '                                          "--pre-a3-mode", "run" if pre_a3_run else '
                                    '"present",',
                                    '            self.run("pre-a3", ["bash", str(self.driver_dir / "entry-gates.sh"), '
                                    '"--profile", "pre-a3", "--out", str(out),',
                                    '                                "--pre-a3-mode", "run", "--mode", self._gates_mode(),',
                                    '        if pre_a3_run:',
                                    '            self._finish("pre-a3", certification.is_file(), {"certification": '
                                    'str(certification)})'],
 'tools/ci/entry_steps.py': []}
# 5626c055719 的固定场景根支持按真实父提交登记：仅 Claude 完整79显式传参进入，
# Codex 的临时根创建、通过后清理与原命令保持不变；不能用当前 HEAD 空残差追认。
CLAUDE_FIXED_SCENARIO_ROOT_BASELINE = '91bc47d3edd83b67b15049d3391dcf4ecdf81709'
CLAUDE_FIXED_SCENARIO_ROOT_DIGESTS = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': '4c87682d5fab7b03a71c06013cc0f82ef9e46ffdff8a0398598d86b3415d1cdf'}
CLAUDE_FIXED_SCENARIO_ROOT_REMOVED = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': ['# 每次在父目录下新建，通过后删除、失败保留供排查。结论不再写结果文件，而是单元标准输出里恰好一行',
                                                                         '    场景结果行的合同两客户端相同（不在附录 B 之列）：场景归属由执行记录的单元 '
                                                                         'ID（``pre-a3:claude:`` 前缀）区分。"""',
                                                                         '    prefix = ("diagnostic-" if diagnostic else "") + '
                                                                         '_scenario_file_name(name) + "-"',
                                                                         '    root = Path(tempfile.mkdtemp(prefix=prefix, '
                                                                         'dir=parent))',
                                                                         '    if result["scenario"].get("status") == "passed" and '
                                                                         'result["network_attempts"] == 0:',
                                                                         '                result = run_scenario_unit(arguments.name, '
                                                                         'arguments.staging_parent, diagnostic=diagnostic, '
                                                                         'client=client)']}

# 前台与空跑显式复用后台工作区，以真实父提交7ab登记；默认与B-09不新增工作区步骤输入或缓存注入。
ENTRY_SOURCE_WORKSPACE_BASELINE = '7ab894b24b6770d96190eea6b6b30c2a8baeb7fa'
ENTRY_SOURCE_WORKSPACE_DIGESTS = {'tools/ci/entry_dryrun.py': 'd18da3e4429a0be4796fc28b8d57ca9e9f871bccf61cf97f54039553cbd167c2',
 'tools/ci/entry_orchestrator.py': 'c5e7c69def778aaca9fb1bdf63fa19fd26d2c33be306e1ba99504d3eee110362',
 'tools/ci/entry_steps.py': 'dc9482c0f91a8d557af74aa7e47de92c5321e37b21a19dda75da611f895d3e69'}
ENTRY_SOURCE_WORKSPACE_REMOVED = {'tools/ci/entry_dryrun.py': ['                unit_inherit_full_gates: str | None = None, audit_policy: str | None = '
                              'None) -> Path:',
                              '                       audit_policy=policy_reference["sha256"] if policy_reference else '
                              'None)'],
 'tools/ci/entry_orchestrator.py': ['                                          *audit_args, *source])',
                                    '                                *audit_args, *source])',
                                    '    if args.full_set_work and (not args.full_set_request or not '
                                    'args.full_set_work.is_absolute() or ".." in args.full_set_work.parts):'],
 'tools/ci/entry_steps.py': []}

# 后台显式来源坐标保持原CLI与结果形状；两处校验同步按真实父提交登记。
ENTRY_SOURCE_STABILITY_BASELINE = 'cf691738ba83d7fe6474ffdf134cd680372b21b8'
ENTRY_SOURCE_STABILITY_DIGESTS = {'tools/ci/background_validation.py': '4fc569275ba6737e1fe9b5354f5f86ed07322565b5cbacb9502af93b927b78f8'}
ENTRY_SOURCE_STABILITY_REMOVED = {'tools/ci/background_validation.py': ['本轮 background-validation/work 的独立目录，输入合同变化不能继续复用旧结论，启动以文件锁去重。',
                                       '    isolated_root = Path(args.runroot) / DIRECTORY / "work"',
                                       '    bound_root = isolated_root / f"{args.commit}-{binding_key(deployment, '
                                       'deployments)}"',
                                       '    work = args.work or bound_root',
                                       '    if (not work.is_absolute() or ".." in work.parts or not '
                                       'work.is_relative_to(bound_root)',
                                       '        isolated_root = Path(payload["runroot"]) / DIRECTORY / "work"',
                                       '        bound_root = isolated_root / '
                                       'f"{payload[\'commit\']}-{binding_key(payload[\'deployment\'], '
                                       'payload.get(\'deployments\'))}"',
                                       '                or not work.is_relative_to(bound_root) or ".." in '
                                       'work.parts):']}

# 后台失败的显式逐单元后继只接原合同；默认 B-09 命令与收据由原后台回归守住。
BACKGROUND_INHERIT_BASELINE = 'efb72f9cac3a0a5a57560a15d9f6e16d6ee9167b'
BACKGROUND_INHERIT_DIGESTS = {'tools/ci/background_validation.py': '9411fe531352825b4ca4d0c1395cb2de290856d8773c34f3538cc4b6b2bbe890'}
BACKGROUND_INHERIT_REMOVED = {'tools/ci/background_validation.py': ['    argv = ["bash", payload["entry_gates"], "--profile", payload["profile"], '
                                       '"--mode", "re-execute", require,',
                                       '    except (OSError, ValidationError, KeyError, TypeError) as error:']}

# >>> 收尾登记常量（自动生成，勿手改）
CLOSEOUT_PY_1_BASELINE = 'a421a444f64faa6f9e6f49ccce23ca9bcf7aeeda'
CLOSEOUT_PY_1_DIGESTS = {'tools/ci/background_validation.py': '9f43541ab3a83eff9fab5f8e513b50ec7a0ab92370d7249c5e5878e0a312293a'}
CLOSEOUT_PY_1_REMOVED = {'tools/ci/background_validation.py': ['* ``start --runroot --data-root --bundle --branch --commit [--profile] [--record-store] [--work] [--vc-env] [--then-dryrun]``：',
        '  本次带 ``--audit-policy`` 时记为 skipped：策略来源需要当前逐单元批准，操作员应显式调用',
        'def _terminate(payload: Mapping[str, Any]) -> None:',
        '    """终止后台运行的整个进程组：先 SIGTERM（执行器收到后终止全部单元会话），等整组退出、超过宽限再 SIGKILL。',
        '    终止单元会话就被杀掉，排在后面的单元（各自成会话、不在本组里）留在后台继续跑（E4-02 验收实测）。"""',
        '',
        '        return',
        '    with contextlib.suppress(ProcessLookupError, PermissionError):',
        '        os.killpg(int(pid), signal.SIGTERM)',
        '    deadline = time.monotonic() + STOP_GRACE_SECONDS',
        '    while _group_alive(pid) and time.monotonic() < deadline:',
        '        time.sleep(0.2)',
        '    with contextlib.suppress(ProcessLookupError, PermissionError):',
        '        os.killpg(int(pid), signal.SIGKILL)',
        '',
        '',
        'def stop(runroot: Path, reason: str, *, keep: Path | None = None, with_dryruns: bool = True) -> list[str]:',
        '    发起的）那一次也停（B5a-10）。"""',
        '        _terminate(current)',
        'def stop_registered(data_root: Path, reason: str, *, keep: Path) -> list[str]:',
        '        _terminate(current)',
        '        inherit_reference = _inherit_reference(Path(inherit_request), vars(args))',
        '    if inherit_reference is not None:',
        '    superseded = stop(args.runroot, f"被提交 {args.commit[:12]} 在部署 {deployment[\'receipt\']} 上的后台验证取代", keep=path)',
        '                                      keep=path)',
        '        _write(path, {**current, "pid": process.pid})',
        '    return {"action": "started", "result": str(path), "pid": process.pid, "out": str(out), "superseded": superseded}',
        '                _inherit_completion(payload, historical=True)',
        '        return 0, f"后台验证通过（{where}）"',
        '    p_start.add_argument("--work", type=Path, default=None, help="只允许本轮完整提交及部署摘要目录或其子目录；缺省自动派生")',
        '            print(json.dumps({"stopped": stop(args.runroot, args.reason)}, ensure_ascii=False))']}
CLOSEOUT_PY_2_BASELINE = 'cf691738ba83d7fe6474ffdf134cd680372b21b8'
CLOSEOUT_PY_2_DIGESTS = {'tools/ci/entry_dryrun.py': '192d34fa355265e8d094425df699a07b0eb2c1632d6d2bc55280da7b18e0c84c'}
CLOSEOUT_PY_2_REMOVED = {'tools/ci/entry_dryrun.py': []}
CLOSEOUT_PY_3_BASELINE = 'eecf1845e3159257db55314ba20a13702a414e8e'
CLOSEOUT_PY_3_DIGESTS = {'tools/ci/entry_gates.py': '28af68821b1593248ecd42817c30330a90a42000895dcb5e7c52a6a976aac1cb'}
CLOSEOUT_PY_3_REMOVED = {'tools/ci/entry_gates.py': ['            certification.claude_recorded_time_budget("vc-chain.claude-full-validation-only")["unit_timeout_seconds"]):']}
CLOSEOUT_PY_4_BASELINE = 'cf691738ba83d7fe6474ffdf134cd680372b21b8'
CLOSEOUT_PY_4_DIGESTS = {'tools/ci/entry_orchestrator.py': '4f45830872e05e732b507db210bf861f34f4bd7b752f4d2763e0c1e5732e1f2b'}
CLOSEOUT_PY_4_REMOVED = {'tools/ci/entry_orchestrator.py': ['* 沿用只看步骤记录（产物存在不等于可用）：没有记录的旧产物一律不沿用；只写一次的正式坐标被占用（上次失败的残留、',
        '  或没有记录的旧产物）时换带序号的新坐标（``-r2``、``-r3`` …），实际坐标写进步骤记录，下游从记录取。',
        '                 audit_strict: bool = False, audit_policy: Path | None = None) -> None:',
        '    def _recorded_reuse_products(self, out: Path, certification: Path) -> dict[str, str]:',
        '        result = json.loads((out / "pre-a3-reuse.json").read_text(encoding="utf-8"))',
        '        refresh_reuse = gates_run and self.ctx.client == "claude" and bool(self.params.get("CLAUDE_ENTRY_FULL79_SPEC"))',
        '            external_present = bridge and pre_a3_run and Path(self.p("PRE_A3_CERTIFICATION")).exists()',
        '        except (OrchestratorError, ES.EntryStepsError, OSError) as error:',
        '                                audit_strict=args.audit_strict, audit_policy=args.audit_policy)']}
CLOSEOUT_PY_5_BASELINE = 'cf691738ba83d7fe6474ffdf134cd680372b21b8'
CLOSEOUT_PY_5_DIGESTS = {'tools/ci/entry_steps.py': '33d47a41b10527ce5911cf794906cd2bc8f6148cfc9a6f100c064a537ad4a44d'}
CLOSEOUT_PY_5_REMOVED = {'tools/ci/entry_steps.py': []}
CLOSEOUT_PY_6_BASELINE = 'f99137c82d44adfab8d95b53005b65f5b8de401c'
CLOSEOUT_PY_6_DIGESTS = {'tools/ci/unit_executor.py': 'fbeb5554173c3a847b4f8d757dd556f3d4e9ee6f547c344ce48b81a9492f5fc5'}
CLOSEOUT_PY_6_REMOVED = {'tools/ci/unit_executor.py': ['        for pid in list(self.running):',
        '            for member in [pid, *_session_members(pid)]:',
        '                    os.kill(member, signal.SIGKILL)',
        '            with contextlib.suppress(ChildProcessError):',
        '                os.waitpid(pid, 0)',
        '            self.running.pop(pid, None)',
        '    def _clean_orphans(self, session_id: int, *, interrupted: bool = False) -> list[int]:',
        '        members = _session_members(session_id)',
        '            members = _session_members(session_id)',
        '        for sig in (signal.SIGTERM, signal.SIGKILL):',
        '            for pid in _session_members(session_id):',
        '                with contextlib.suppress(ProcessLookupError, PermissionError):',
        '                    os.kill(pid, sig)',
        '            if sig == signal.SIGTERM and _session_members(session_id):',
        '        return members',
        '                # 单元自带超时（命令单元）优先，否则用调度配置的统一超时；先 SIGTERM，30 秒后仍在就 SIGKILL。',
        '                elif item.timed_out and elapsed > limit + 30:',
        '                    with contextlib.suppress(ProcessLookupError, PermissionError):',
        '                        os.killpg(pid, signal.SIGKILL)',
        '                orphans = self._clean_orphans(pid, interrupted=item.interruption is not None)',
        '                    # 无论原单元是否已经失败，都须核对会话清理后才能授予整机。',
        '                    active = []',
        '                    for member in _session_members(pid):',
        '                        try:',
        "                            state = (Path('/proc') / str(member) / 'stat').read_text().rsplit(')', 1)[-1].split()[0]",
        '                        except FileNotFoundError:',
        '                            continue',
        "                        if state not in ('Z', 'X'):",
        '                            active.append(member)',
        '                        executor=records.executor_version([args.bytecode_helper]), environment=None,',
        '    executor = records.executor_version([args.bytecode_helper])',
        '            current.inputs = records._unique([*(current.inputs or []), *[_audit_module().host_snapshot(path) for path in paths],',
        '                                                     sources=inherit_module.propose(store.root, currents, facts, max_age_hours=max_age))',
        '                if name.startswith("host:") and _audit_module().host_snapshot(name[5:]) != entry:',
        '                or records.executor_version([args.bytecode_helper]) != recorder.executor',
        '        # 读集未闭合不能靠一次无审计的重跑补齐；仅执行本身失败才诊断重跑。',
        '        diagnostic = scheduler.run_alone([o.unit for o in formal if not o.execution_passed], "diagnostic")',
        '                            executor=records.executor_version([args.bytecode_helper]), environment=None,']}
CLOSEOUT_PY_7_BASELINE = 'df90c329e3ba2b659ac7667b897e38a7a35d6269'
CLOSEOUT_PY_7_DIGESTS = {'tools/ci/unit_records.py': '04056c95428c8d94d10290e14f403a36352935088e94b230a33976caea9312fc'}
CLOSEOUT_PY_7_REMOVED = {'tools/ci/unit_records.py': ['    allowed = {"ranges", "files", "test_modules", "head", "resolved", "host_paths", "require_read_audit", "runtime_contract"}',
        '        entries.append(_audit_module().host_snapshot(path))',
        '    """只登记本轮真实规划的命令与测试单元；未登记单元只能重跑，未知单元拒绝。"""',
        '        if (not isinstance(declaration, dict) or set(declaration) != {"host_paths", "require_read_audit", "runtime_contract"}',
        '                replay = _audit_module().strict_audit_reads(json.loads(stored_trace.read_text()), current.inputs,']}
CLOSEOUT_PY_8_BASELINE = '5626c055719f02a8574e4d8969caea84b85ad83b'
CLOSEOUT_PY_8_DIGESTS = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': '8f9ed69f6e15ae20ab2ec9bb1d56b0c841507fb17b94fd8aa473268cea73f3e7'}
CLOSEOUT_PY_8_REMOVED = {'tools/official_client_capture/codex_upgrade_pre_a3_certification.py': ['                                claude_recorded_time_budget(name)["unit_timeout_seconds"]']}
# <<< 收尾登记常量
LATER_SOURCE_BASELINES: tuple[tuple[str, str, dict[str, str], dict[str, list[str]]], ...] = (
    ("后台失败的受控逐单元后继", BACKGROUND_INHERIT_BASELINE, BACKGROUND_INHERIT_DIGESTS, BACKGROUND_INHERIT_REMOVED),
    ("入口来源目录与持久协调锁", ENTRY_SOURCE_STABILITY_BASELINE, ENTRY_SOURCE_STABILITY_DIGESTS, ENTRY_SOURCE_STABILITY_REMOVED),
    ("入口承接后台来源工作区", ENTRY_SOURCE_WORKSPACE_BASELINE, ENTRY_SOURCE_WORKSPACE_DIGESTS, ENTRY_SOURCE_WORKSPACE_REMOVED),
    ("5626固定完整79场景根真实接管", CLAUDE_FIXED_SCENARIO_ROOT_BASELINE, CLAUDE_FIXED_SCENARIO_ROOT_DIGESTS, CLAUDE_FIXED_SCENARIO_ROOT_REMOVED),
    ("df90c329e真实历史接管", READ_SCOPE_HISTORY_BASELINE, READ_SCOPE_HISTORY_DIGESTS, READ_SCOPE_HISTORY_REMOVED),
    ("f99137c82真实历史接管", PRIVATE_RESULT_HISTORY_BASELINE, PRIVATE_RESULT_HISTORY_DIGESTS, PRIVATE_RESULT_HISTORY_REMOVED),
    ("8d72f39f6真实历史接管", ENTRY_AUDIT_HISTORY_BASELINE, ENTRY_AUDIT_HISTORY_DIGESTS, ENTRY_AUDIT_HISTORY_REMOVED),
    ("B5a-09固定79正式入口接线", ENTRY_RECORDED_BRIDGE_BASELINE, ENTRY_RECORDED_BRIDGE_DIGESTS, ENTRY_RECORDED_BRIDGE_REMOVED),
    ("Claude 连续世界两小时安全网", CLAUDE_CONTINUOUS_TIME_BASELINE, CLAUDE_CONTINUOUS_TIME_DIGESTS, CLAUDE_CONTINUOUS_TIME_REMOVED),
    ("Claude 录制链时间合同 v2", CLAUDE_RECORDED_TIME_BASELINE, CLAUDE_RECORDED_TIME_DIGESTS, CLAUDE_RECORDED_TIME_REMOVED),
    ("Claude 故障链时间合同 v2", CLAUDE_TIME_BASELINE_COMMIT, CLAUDE_TIME_SOURCE_DIGESTS, CLAUDE_TIME_SOURCE_REMOVED_LINES),
    ("B5a-09", B5A09_BASELINE_COMMIT, B5A09_SOURCE_DIGESTS, B5A09_SOURCE_REMOVED_LINES),
    ("B5a-10", B5A10_BASELINE_COMMIT, B5A10_SOURCE_DIGESTS, B5A10_SOURCE_REMOVED_LINES),
    ("B5a-11", B5A11_BASELINE_COMMIT, B5A11_SOURCE_DIGESTS, B5A11_SOURCE_REMOVED_LINES),
    ("B5b-01a", B5B01A_BASELINE_COMMIT, B5B01A_SOURCE_DIGESTS, B5B01A_SOURCE_REMOVED_LINES),
    ("B5b-03", B5B03_BASELINE_COMMIT, B5B03_SOURCE_DIGESTS, B5B03_SOURCE_REMOVED_LINES),
    ("B4-05b-1", B405B1_BASELINE_COMMIT, B405B1_SOURCE_DIGESTS, B405B1_SOURCE_REMOVED_LINES),
    ("B5b-03 B 组", B5B03B_BASELINE_COMMIT, B5B03B_SOURCE_DIGESTS, B5B03B_SOURCE_REMOVED_LINES),
    ("B5b-05a", B5B05A_BASELINE_COMMIT, B5B05A_SOURCE_DIGESTS, B5B05A_SOURCE_REMOVED_LINES),
    ("H2 承接保护", H2_INHERIT_GUARD_BASELINE_COMMIT, H2_INHERIT_GUARD_SOURCE_DIGESTS, H2_INHERIT_GUARD_SOURCE_REMOVED_LINES),
    ("Claude 三条可执行入口补登", CLAUDE_ENTRY_REGISTRATION_BASELINE_COMMIT,
     CLAUDE_ENTRY_REGISTRATION_SOURCE_DIGESTS, CLAUDE_ENTRY_REGISTRATION_SOURCE_REMOVED_LINES),
    ("Claude 原任务预算占用与结清", CLAUDE_REQUEST_RESERVATION_BASELINE_COMMIT,
     CLAUDE_REQUEST_RESERVATION_SOURCE_DIGESTS, CLAUDE_REQUEST_RESERVATION_SOURCE_REMOVED_LINES),
    ("Claude revision 八场景接入准备", CLAUDE_REVISION_ENTRY_BASELINE_COMMIT,
     CLAUDE_REVISION_ENTRY_SOURCE_DIGESTS, CLAUDE_REVISION_ENTRY_SOURCE_REMOVED_LINES),
    ("Claude 三来源输入及录制目录接线", CLAUDE_FIXTURE_WIRING_BASELINE_COMMIT,
     CLAUDE_FIXTURE_WIRING_SOURCE_DIGESTS, CLAUDE_FIXTURE_WIRING_SOURCE_REMOVED_LINES),
    ("升级入口可选并发透传", ENTRY_PARALLEL_BASELINE_COMMIT,
     ENTRY_PARALLEL_SOURCE_DIGESTS, ENTRY_PARALLEL_SOURCE_REMOVED_LINES),
    ("Claude 私有目标场景文件输入绑定", CLAUDE_SCENARIO_FILE_BASELINE_COMMIT,
     CLAUDE_SCENARIO_FILE_SOURCE_DIGESTS, CLAUDE_SCENARIO_FILE_SOURCE_REMOVED_LINES),
    ("入口实际组合分别批准请求", ENTRY_PROFILE_REQUESTS_BASELINE_COMMIT,
     ENTRY_PROFILE_REQUESTS_SOURCE_DIGESTS, ENTRY_PROFILE_REQUESTS_SOURCE_REMOVED_LINES),
    ("空跑审计策略透传", ENTRY_DRYRUN_AUDIT_BASELINE_COMMIT,
     ENTRY_DRYRUN_AUDIT_SOURCE_DIGESTS, ENTRY_DRYRUN_AUDIT_SOURCE_REMOVED_LINES),
    # >>> 收尾登记行（自动生成，勿手改）
    ('收尾登记 background_validation.py', CLOSEOUT_PY_1_BASELINE, CLOSEOUT_PY_1_DIGESTS, CLOSEOUT_PY_1_REMOVED),
    ('收尾登记 entry_dryrun.py', CLOSEOUT_PY_2_BASELINE, CLOSEOUT_PY_2_DIGESTS, CLOSEOUT_PY_2_REMOVED),
    ('收尾登记 entry_gates.py', CLOSEOUT_PY_3_BASELINE, CLOSEOUT_PY_3_DIGESTS, CLOSEOUT_PY_3_REMOVED),
    ('收尾登记 entry_orchestrator.py', CLOSEOUT_PY_4_BASELINE, CLOSEOUT_PY_4_DIGESTS, CLOSEOUT_PY_4_REMOVED),
    ('收尾登记 entry_steps.py', CLOSEOUT_PY_5_BASELINE, CLOSEOUT_PY_5_DIGESTS, CLOSEOUT_PY_5_REMOVED),
    ('收尾登记 unit_executor.py', CLOSEOUT_PY_6_BASELINE, CLOSEOUT_PY_6_DIGESTS, CLOSEOUT_PY_6_REMOVED),
    ('收尾登记 unit_records.py', CLOSEOUT_PY_7_BASELINE, CLOSEOUT_PY_7_DIGESTS, CLOSEOUT_PY_7_REMOVED),
    ('收尾登记 codex_upgrade_pre_a3_certification.py', CLOSEOUT_PY_8_BASELINE, CLOSEOUT_PY_8_DIGESTS, CLOSEOUT_PY_8_REMOVED),
    # <<< 收尾登记行
)
# B5a-08 之后按设计改过的入口步骤声明：客户端 → 步骤 → (新增的输入名, 移除的输入名, 理由)。Codex 一侧不得出现。
CLAUDE_FIXTURE_STEP_INPUTS = (
    "param:CLAUDE_2_1_226_RELAY_FIXTURE", "param:CLAUDE_2_1_226_FREEZE_FIXTURE", "param:CLAUDE_2_1_226_STATIC_FIXTURE",
    "tree:{CLAUDE_2_1_226_RELAY_FIXTURE}#parent_pycache", "tree:{CLAUDE_2_1_226_FREEZE_FIXTURE}#pycache",
    "tree:{CLAUDE_2_1_226_STATIC_FIXTURE}#pycache",
)
STEP_CHANGES_AFTER_B5A08: dict[str, dict[str, tuple[list[str], list[str], str]]] = {
    "claude": {
        "entry-preflight": (["file:{SCENARIOS_JSON}"], [],
                            "f1ce9dcd3：Claude 便宜检查实际读取私有目标场景清单，绑定文件全文；Codex 不新增输入"),
        "entry-gates": (list(CLAUDE_FIXTURE_STEP_INPUTS), [],
                        "a0c564377：Claude 三份来源的路径与内容进入门禁步骤输入；relay 覆盖父根的同级补充录制，Codex 空表不增加输入"),
        "pre-a3": (list(CLAUDE_FIXTURE_STEP_INPUTS), [],
                   "a0c564377：认证步骤消费同一份三来源输入声明，防止工程来源变更仍沿用旧认证；Codex 步骤声明不变"),
        "zero-request-smoke": (["identity:evidence_semantics_sha256"], [],
                               "B5a-09：Claude smoke 依赖的 provenance 在 Claude 身份的 evidence 层，登记的身份摘要取客户端表"),
        "client-launch-probe": (["driver:claude_launch_probe_runner.py"], ["driver:client_launch_probe_runner.py"],
                                "B2-10：启动探测运行器按客户端表取（launch_probe_runner），Claude 用 claude_launch_probe_runner.py；"
                                "Codex 仍是 client_launch_probe_runner.py"),
        "preflight-plan": (["file:{SCENARIOS_JSON}", "file:{ACTIVE_RELEASE_CATALOG}", "file:{STATIC_BASELINE_ANCHORS}", "file:{STATIC_BASELINE_REACHABILITY}",
                            "tree:{PRODUCTION_INVENTORY_STORE}#pycache", "tree:{PREDECESSOR_STORE}#pycache"], [],
                           "B2-11 第二部分：Claude 预检 plan 另读的文件与 Store 目录取客户端表 preflight_plan_inputs（Active Release 目录、"
                            "静态基线两份、生产现状清单所在的前序 Store 与只读复用的前序 Store）；f1ce9dcd3 另绑定私有目标场景清单全文；"
                            "Codex 这些项为空，步骤表不变"),
        "ledger": (["param:CAMPAIGN_PURPOSE"], [],
                   "B5b-01a：Claude 的计时账本按参数 CAMPAIGN_PURPOSE 建（用途一变账本重建）；Codex 没有用途参数键，步骤表不变"),
    },
}


def removed_lines(relative: str, old: str) -> list[str]:
    new = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
    return [line[1:] for line in difflib.unified_diff(old.splitlines(), new, lineterm="", n=0)
            if line.startswith("-") and not line.startswith("---")]


def source_batches_registering(relative: str, digest: str) -> list[str]:
    """登记了文件 ``relative`` 这份内容（摘要）的批次：B5a-08 与之后各批（含 5a 收口的统一基线）；入口门禁脚本另认 B5a-06
    对照模块里登记的批次。"""

    owners = ["B5a-08"] if SOURCE_DIGESTS.get(relative) == digest else []
    owners += [batch for batch, _commit, digests, _removed in LATER_SOURCE_BASELINES if digests.get(relative) == digest]
    if relative == ENTRY_GATES:
        owners += b5a06.shell_batches_registering(Path(relative).name, digest)
    return owners


def current_digest(relative: str) -> str:
    return hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()


class SourceResidualCodexParityTests(unittest.TestCase):
    def _b5a08_source(self, relative: str) -> str | None:
        """B5a-08 登记的改后内容仍是当前内容时返回基线源码；之后又改过时核对接管（当前内容由之后的某一批登记）并返回 None。"""

        digest = current_digest(relative)
        if digest != SOURCE_DIGESTS[relative]:
            b5a06.assert_handed_over(self, relative, "B5a-08", source_batches_registering(relative, digest))
            return None
        source = git_source(relative)
        self.assertIsNotNone(source, f"基线提交 {BASELINE_COMMIT} 里没有 {relative}")
        return source.decode("utf-8")

    def test_only_the_registered_lines_were_removed_or_rewritten(self) -> None:
        if not b5a06.commit_readable(BASELINE_COMMIT):
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT}（浅克隆或不在仓库内）")
        self.assertEqual(sorted(SOURCE_DIGESTS), sorted([*SOURCE_REMOVED_LINES, ENTRY_GATES]))
        for relative, expected in SOURCE_REMOVED_LINES.items():
            with self.subTest(relative):
                source = self._b5a08_source(relative)
                if source is not None:
                    self.assertEqual(removed_lines(relative, source), expected)

    def test_later_batches_only_rewrote_the_registered_lines(self) -> None:
        self.assertNotIn("codex", STEP_CHANGES_AFTER_B5A08, "Codex 的步骤声明不得改")
        unreadable = sorted({commit for _batch, commit, _digests, _removed in LATER_SOURCE_BASELINES if not b5a06.commit_readable(commit)})
        if unreadable:
            self.skipTest(f"读不到基线提交 {'、'.join(unreadable)}（浅克隆或不在仓库内）")
        # 统一基线覆盖 B5a-08 登记的全部 Python 文件（入口门禁脚本的统一基线在 B5a-06 对照模块里）。
        self.assertEqual(sorted(B5A11_SOURCE_DIGESTS), sorted(SOURCE_REMOVED_LINES))
        self.assertIn(Path(ENTRY_GATES).name, b5a06.B5A11_SHELL_DIGESTS)
        for batch, commit, digests, removed in LATER_SOURCE_BASELINES:
            self.assertEqual(sorted(digests), sorted(removed), batch)
            for relative, digest in digests.items():
                with self.subTest(batch=batch, source=relative):
                    current = current_digest(relative)
                    if current != digest:
                        b5a06.assert_handed_over(self, relative, batch, source_batches_registering(relative, current))
                        continue
                    source = git_source(relative, commit)
                    self.assertIsNotNone(source, f"基线提交 {commit} 里没有 {relative}")
                    self.assertEqual(removed_lines(relative, source.decode("utf-8")), removed[relative])

    def test_entry_gates_script_with_codex_values_differs_only_in_registered_lines(self) -> None:
        if not b5a06.commit_readable(BASELINE_COMMIT):
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT}（浅克隆或不在仓库内）")
        old = self._b5a08_source(ENTRY_GATES)
        if old is None:
            return  # 之后又改过，已核对接管：当前内容由 B5a-06 对照模块里的某一批逐行对照
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
                expected_gates = as_json(old.GATE_INPUTS_BY_CLIENT[client])
                if client == "claude":
                    # a0c564377：pre-A3 门禁复用同名步骤输入。只准在旧工程目录声明后插入以下六项；
                    # 其余门禁项、输入内容与顺序仍按原基线逐字比较，不从当前实现生成期望值。
                    before = expected_gates["pre-a3"]
                    anchor = old.Input("environment", "tree", "{D}/upgrade-project-ledger-claude", "pycache").as_json()
                    position = before.index(anchor) + 1
                    additions = [
                        old.Input("param", "param", "CLAUDE_2_1_226_RELAY_FIXTURE"),
                        old.Input("param", "param", "CLAUDE_2_1_226_FREEZE_FIXTURE"),
                        old.Input("param", "param", "CLAUDE_2_1_226_STATIC_FIXTURE"),
                        old.Input("environment", "tree", "{CLAUDE_2_1_226_RELAY_FIXTURE}", "parent_pycache"),
                        old.Input("environment", "tree", "{CLAUDE_2_1_226_FREEZE_FIXTURE}", "pycache"),
                        old.Input("environment", "tree", "{CLAUDE_2_1_226_STATIC_FIXTURE}", "pycache"),
                    ]
                    before[position:position] = [item.as_json() for item in additions]
                self.assertEqual(as_json(es.GATE_INPUTS_BY_CLIENT[client]), expected_gates)
                inherit = es.STEPS_BY_CLIENT_UNIT_INHERIT[client]
                for plain, extended in zip(es.STEPS_BY_CLIENT[client], inherit):
                    added = [item.name for item in extended.inputs if item not in plain.inputs]
                    expected = ["file:{UNIT_INHERIT_REQUEST}", "driver:unit_inherit.py"] if plain.step_id in es.UNIT_INHERIT_STEPS else []
                    self.assertEqual(added, expected, plain.step_id)
        params = {"D": "/data", "CLIENT": "codex"}
        self.assertEqual(es.Context(params=params, driver_dir=Path("/drv"), steps_dir=Path("/steps")).steps, es.STEPS_BY_CLIENT["codex"])
        inherited_steps = es.Context(params={**params, "UNIT_INHERIT_REQUEST": "/r.json"}, driver_dir=Path("/drv"),
                                     steps_dir=Path("/steps")).steps
        self.assertEqual([step.spec_sha256() for step in inherited_steps],
                         [step.spec_sha256() for step in es.STEPS_BY_CLIENT_UNIT_INHERIT["codex"]],
                         "动态元组可以是新对象，Codex 的步骤规格、输入与顺序必须逐项相同")

    def test_orchestrator_gate_mode_matches_the_baseline_expression(self) -> None:
        from tools.ci import entry_orchestrator as eo

        for full_set, reexecute in ((None, False), (Path("/r.json"), False), (None, True), (Path("/r.json"), True)):
            with self.subTest(full_set=full_set, reexecute=reexecute):
                orchestrator = types.SimpleNamespace(full_set_request=full_set, reexecute_gates=reexecute, unit_inherit_request=None)
                baseline = "full-set-pass" if full_set and not reexecute else "re-execute"
                self.assertEqual(eo.Orchestrator._gates_mode(orchestrator), baseline)
        inherit = types.SimpleNamespace(full_set_request=None, reexecute_gates=False, unit_inherit_request=Path("/u.json"))
        self.assertEqual(eo.Orchestrator._gates_mode(inherit), "unit-inherit")



# ------------------------------------------------------------------------------------------------ 防回归：子测试里不跳过

# Codex 逐字对照模块的跳过纪律：子测试里跳过时 unittest 既不报父用例、又多报子测试编号，入口门禁的全集核对因此失败（全集核对
# 在 tools/ci 的 unit_records／full_set_receipt，摘要被 Codex 评估器读侧钉住，不改它）。跳过只许发生在父级（subTest 之外），
# 对照基线过期改为核对接管。
PARITY_MODULES = ("test_driver_client_table_codex_parity.py", "test_unit_inherit_codex_parity.py", "test_pre_a3_codex_parity.py")


def _direct_skips(node: ast.AST) -> list[int]:
    """节点里的跳过（行号）：任何 ``<对象>.skipTest(...)`` 调用，以及 ``raise SkipTest``／``raise unittest.SkipTest(...)``。"""

    lines: list[int] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "skipTest":
            lines.append(item.lineno)
        elif isinstance(item, ast.Raise) and item.exc is not None:
            target = item.exc.func if isinstance(item.exc, ast.Call) else item.exc
            if (target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)) == "SkipTest":
                lines.append(item.lineno)
    return lines


def subtest_skips(trees: dict[str, ast.Module]) -> list[str]:
    """``with self.subTest(...)`` 块里（含经本类方法、本模块函数与另一对照模块函数的传递调用）出现的跳过，记「模块:行号」。"""

    stems = {Path(name).stem: name for name in trees}
    functions = {name: {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)} for name, tree in trees.items()}
    aliases: dict[str, dict[str, str]] = {}
    for name, tree in trees.items():
        aliases[name] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in stems:
                        aliases[name][alias.asname or alias.name] = stems[alias.name]
    found: list[str] = []
    for name, tree in trees.items():
        for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
            methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}

            def resolve(call: ast.Call, module: str, methods: dict[str, ast.FunctionDef] = methods) -> tuple[str, ast.AST] | None:
                func = call.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == "self" and func.attr in methods and module == name:
                        return module, methods[func.attr]
                    target = aliases[module].get(func.value.id)
                    if target is not None and func.attr in functions[target]:
                        return target, functions[target][func.attr]
                if isinstance(func, ast.Name) and func.id in functions[module]:
                    return module, functions[module][func.id]
                return None

            for block in [node for node in ast.walk(cls) if isinstance(node, ast.With)]:
                if not any(isinstance(item.context_expr, ast.Call) and isinstance(item.context_expr.func, ast.Attribute)
                           and item.context_expr.func.attr == "subTest" for item in block.items):
                    continue
                pending: list[tuple[str, ast.AST]] = [(name, statement) for statement in block.body]
                seen: set[int] = set()
                while pending:
                    module, node = pending.pop()
                    found += [f"{module}:{line}" for line in _direct_skips(node)]
                    for call in [item for item in ast.walk(node) if isinstance(item, ast.Call)]:
                        target = resolve(call, module)
                        if target is not None and id(target[1]) not in seen:
                            seen.add(id(target[1]))
                            pending.append(target)
    return sorted(set(found), key=lambda item: (item.rpartition(":")[0], int(item.rpartition(":")[2])))


class ParitySkipDisciplineTests(unittest.TestCase):
    def test_parity_modules_never_skip_inside_subtests(self) -> None:
        here = Path(__file__).resolve().parent
        trees = {name: ast.parse((here / name).read_text(encoding="utf-8")) for name in PARITY_MODULES}
        self.assertEqual(subtest_skips(trees), [], "Codex 逐字对照模块的子测试里不许跳过：跳过放到父级，基线过期改为核对接管")
        # 规则本身认得出：子测试里直接跳过、经本类方法跳过、经本模块函数跳过；父级跳过不算。
        sample = textwrap.dedent('''
            def outer(case):
                case.skipTest("模块函数")
            class T:
                def helper(self):
                    raise unittest.SkipTest("本类方法")
                def test_parent(self):
                    self.skipTest("父级可以")
                    with self.subTest(0):
                        pass
                def test_direct(self):
                    with self.subTest(1):
                        self.skipTest("直接")
                def test_method(self):
                    for item in range(2):
                        with self.subTest(item):
                            self.helper()
                def test_function(self):
                    with self.subTest(3):
                        outer(self)
        ''')
        self.assertEqual(subtest_skips({"sample.py": ast.parse(sample)}), ["sample.py:3", "sample.py:6", "sample.py:13"])


if __name__ == "__main__":
    unittest.main()
