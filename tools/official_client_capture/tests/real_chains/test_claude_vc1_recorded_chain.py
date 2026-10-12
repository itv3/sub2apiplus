"""B5b-02 的显式 ARM64 录制链外壳，不进入默认单元发现。

每条要求 mount/net namespace、只读历史来源和真实阶段结果；跳过只表示未验证。
CLAUDE_RECORDED_CHAIN_RECORD_DIR 保存世界原坐标；世界与摘要绑定报告持久保留在实际系统临时根，
CLAUDE_FAULT_CHAIN_RECORDS 沿用既有逐场景 JSONL；仅设置后者时也在它旁边保存世界位置。
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_chain_diagnostic_budget as diagnostic_budget

RECORD_DIR_ENV = "CLAUDE_RECORDED_CHAIN_RECORD_DIR"
RECORDS_ENV = "CLAUDE_FAULT_CHAIN_RECORDS"


@contextlib.contextmanager
def staging_directory(prefix: str):
    """世界直接建在实际系统临时根；有记录配置时保留原位置并写旁侧坐标，无配置时正常清理。"""

    configured = os.environ.get(RECORD_DIR_ENV)
    legacy = os.environ.get(RECORDS_ENV)
    if configured or legacy:
        root = Path(configured) if configured else Path(legacy).parent / (Path(legacy).stem + "-recorded-artifacts")
        if root.is_symlink():
            raise ValueError("录制链记录目录不能是符号链接")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        actual_temp = Path(tempfile.gettempdir()).resolve()
        directory = Path(tempfile.mkdtemp(prefix=prefix, dir=actual_temp))
        # 完整链来源只允许实际临时根首层的 <world>/staging；外部记录目录不参与世界装配。
        # 只增加位置原件，原世界和已形成的绝对路径绑定不搬迁、不改写。
        source.write(root / (directory.name + "-location.json"), {
            "world_root": str(directory), "data_root": str(directory / "staging/data"),
            "system_temp_root": str(actual_temp),
        })
        yield directory
    else:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            yield Path(directory)


def save_record(directory: Path, record: dict, *, append: bool = True) -> None:
    """保存逐字结果与结果摘要；成功或失败的外壳日志均早于断言落盘。"""

    # 待验与最终记录分开一次性写入，不覆盖任何已形成的验收凭证。
    name = "record" if append else "record-pending"
    path = source.write(directory / (name + ".json"), record)
    source.write(directory / (name + "-binding.json"), source.binding(path))
    configured = os.environ.get(RECORDS_ENV)
    if configured and append:
        with Path(configured).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**record, "record_binding": source.binding(path)}, ensure_ascii=False, sort_keys=True) + "\n")


class ClaudeRecordedVC1ChainTests(unittest.TestCase):
    """显式启用的真实连续控制链；缺条件不会降级成 mock 判定通过。"""

    def test_historical_307_sink_sources_remain_independent(self) -> None:
        configured = os.environ.get("CLAUDE_2_1_226_STATIC_FIXTURE")
        if not configured:
            self.skipTest("未验证：未设置历史 307 sink 静态来源")
        result = source.verify_historical_static(Path(configured))
        self.assertEqual(result["sink_count"], 307)
        self.assertEqual(result["files_written"], 0)
        self.assertFalse(result["vc_chain_certified"])

    def test_real_vc0_closeout_supervised_copy_and_vc1_seal(self) -> None:
        self.assertIsNone(diagnostic_budget.diagnostic_seconds(), "正式录制链不能继承诊断预算")
        recorded = os.environ.get("CLAUDE_2_1_226_RELAY_FIXTURE")
        freeze = os.environ.get("CLAUDE_2_1_226_FREEZE_FIXTURE")
        if not recorded or not freeze:
            self.skipTest("未验证：需显式登记 2.1.226 relay 与旧 freeze 来源")
        if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"} or os.geteuid() != 0:
            self.skipTest("未验证：真实链要求 Linux ARM64 root")
        if shutil.which("unshare") is None or shutil.which("mount") is None:
            self.skipTest("未验证：缺少 unshare 或 mount")
        probe = subprocess.run(["unshare", "-m", "-n", "true"], capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest("未验证：宿主不允许建立 mount/net namespace")
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver
        time_budget = certification.claude_recorded_time_budget("vc-chain.claude-vc1-capture")
        started = time.monotonic()
        with staging_directory("claude-vc1-recorded-") as directory:
            data = directory / "staging/data"
            command = ["unshare", "-m", "-n", sys.executable, "-m",
                       "tools.official_client_capture.tests.claude_vc1_recorded_chain_driver",
                       "--data", str(data), "--recorded", recorded, "--freeze", freeze,
                       "--parent-namespaces", json.dumps({kind: os.readlink(f"/proc/self/ns/{kind}") for kind in ("mnt", "net")})]
            try:
                completed = subprocess.run(command, cwd=driver.REPOSITORY, capture_output=True, text=True, timeout=time_budget["chain_timeout_seconds"],
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(directory.parent)})
            except subprocess.TimeoutExpired as error:
                for filename, content in (("stdout.log", error.stdout or ""), ("stderr.log", error.stderr or "")):
                    value = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
                    (directory / filename).write_text(value, encoding="utf-8")
                save_record(directory, {"scenario": "vc-chain.claude-vc1-capture", "status": "unverified", "timeout": True,
                                        "seconds": time.monotonic() - started,
                                        "stdout": source.binding(directory / "stdout.log"),
                                        "stderr": source.binding(directory / "stderr.log")})
                raise
            (directory / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (directory / "stderr.log").write_text(completed.stderr, encoding="utf-8")
            report_path = data / "control/recorded-chain-result.json"
            report = source.read(report_path) if report_path.is_file() else None
            seconds = time.monotonic() - started
            record = {"scenario": "vc-chain.claude-vc1-capture", "seconds": seconds,
                      "returncode": completed.returncode, "stdout": source.binding(directory / "stdout.log"),
                      "stderr": source.binding(directory / "stderr.log"), "status": "unverified",
                      "report": source.binding(report_path) if report is not None else None, "report_payload": report}
            save_record(directory, record, append=False)
            self.assertEqual(completed.returncode, 0, completed.stderr[-12000:] + completed.stdout[-12000:])
            self.assertIsNotNone(report, "真实链未形成报告")
            self.assertEqual(report["registered_chains"], ["vc-chain.claude-vc1-capture"])
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["full_vc0_vc6_certified"])
            self.assertEqual(report["network_requests_this_run"], 0)
            self.assertLessEqual(seconds, time_budget["chain_budget_seconds"], "ARM64 录制链超过当前独立预算")
            self.assertGreater(len(report["copy_projections"]), 0)
            self.assertTrue(all(item["runtime"]["run_nonce"] != item["origin"]["run_nonce"] for item in report["copy_projections"]))
            source.bound(report["continuation"], root=data)
            self.real_chain_metrics = {"network_isolated": True, "live_request_count": 0,
                                       "seconds": seconds, "time_budget": time_budget, "report": source.binding(report_path)}
            save_record(directory, {**record, "status": "passed", "metrics": self.real_chain_metrics})

    def test_real_vc0_through_vc6_recorded_validation(self) -> None:
        """pre-A3 的完整录制链入口；委托既有 CLI 外壳，缺条件与失败不能转记通过。"""

        self._run_pending_full_validation()

    def _run_pending_full_validation(self) -> None:
        """保留原显式定向入口，由完整录制链的公共场景转调；本方法不重复进入测试发现。

        世界分配在本进程实际系统临时根，CLI 子进程显式沿用同一 TMPDIR；外部记录目录只保存原位置。本方法只分配一次世界，
        然后委托正式 full CLI；报告缺失、失败或超时均保存真实日志和未验证记录，不能改写为成功。
        """

        self.assertIsNone(diagnostic_budget.diagnostic_seconds(), "正式录制链不能继承诊断预算")
        recorded = os.environ.get("CLAUDE_2_1_226_RELAY_FIXTURE")
        freeze = os.environ.get("CLAUDE_2_1_226_FREEZE_FIXTURE")
        if not recorded or not freeze:
            self.skipTest("未验证：完整链需显式登记 2.1.226 relay 与旧 freeze 来源")
        if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"} or os.geteuid() != 0:
            self.skipTest("未验证：完整链要求 Linux ARM64 root")
        if shutil.which("unshare") is None or shutil.which("mount") is None:
            self.skipTest("未验证：完整链缺少 unshare 或 mount")
        probe = subprocess.run(["unshare", "-m", "-n", "true"], capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest("未验证：宿主不允许建立完整链 mount/net namespace")
        from tools.official_client_capture.tests import claude_recorded_full_validation_driver as driver

        time_budget = source.full79_time_budget()
        started = time.monotonic()
        @contextlib.contextmanager
        def successor_directory():
            # 当前公共完整方法只选择79探针后继；旧57世界和旧位置原件不改签。
            configured = os.environ.get(RECORD_DIR_ENV)
            legacy = os.environ.get(RECORDS_ENV)
            records = Path(configured) if configured else (Path(legacy).parent / (Path(legacy).stem + "-recorded-artifacts") if legacy else None)
            intent_path, intent_digest = os.environ.get(driver.BUDGET_CARRY_INTENT_ENV), os.environ.get(driver.BUDGET_CARRY_DIGEST_ENV)
            intent_binding = None
            if intent_path is not None:
                raw = source.read(Path(intent_path))
                data = Path(raw.get("data_root", ""))
                _intent, intent_binding = driver._carry_intent(intent_path, intent_digest, data)
                directory = data.parent.parent
                driver._require_client_staging(data)
                if data != directory / "staging/data":
                    raise source.replay.ReplayError("完整外壳预算意图须绑定原首层staging/data坐标")
            elif intent_digest is not None:
                raise source.replay.ReplayError("完整后继预算意图缺路径")
            else:
                directory = None
            location = records / "complete-main-model-location.json" if records is not None else None
            previous = None
            if location is not None:
                if records.is_symlink():
                    raise ValueError("完整后继记录目录不能是链接")
                if location.exists():
                    previous = source.read(source.bound(source.binding(location), root=records))
                    if (previous["recorded"] != recorded or previous["freeze"] != freeze
                            or previous["budget_carry_intent"] != intent_binding):
                        raise ValueError("完整后继原位置与来源／预算意图漂移")
                    saved_directory = Path(previous["world_root"])
                    if directory is not None and directory != saved_directory:
                        raise ValueError("完整后继不能换稳定数据根")
                    directory = saved_directory
                    if not directory.is_dir() or directory.is_symlink():
                        raise ValueError("完整后继原世界已缺失，禁止重建")
                    saved_data = directory / "staging/data"
                    if (not saved_data.is_dir() or saved_data.is_symlink()
                            or saved_data.resolve() != saved_data):
                        raise ValueError("完整后继原数据根已缺失或为链接，禁止重建")
                # 已有位置先核 W/D 完整性；首次无位置原件才允许创建记录目录与世界。
                records.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory is None:
                directory = Path(tempfile.mkdtemp(prefix="claude-full-recorded-", dir=Path(tempfile.gettempdir()).resolve()))
            elif previous is None:
                # 已有位置原件必须先核原世界存在，不能先 mkdir 把缺失世界补成空壳。
                directory.mkdir(mode=0o700, exist_ok=True)
            first_started = previous["started_at_epoch"] if previous else time.time()
            if location is not None and previous is None:
                source.write(location, {"world_root": str(directory), "recorded": recorded, "freeze": freeze,
                    "budget_carry_intent": intent_binding, "started_at_epoch": first_started})
            if records is not None:
                # 稳定入口之外仍保留原外壳的位置凭证，便于既有记录消费者定位同一新世界。
                original_location = records / (directory.name + "-location.json")
                coordinates = {"world_root": str(directory), "data_root": str(directory / "staging/data"),
                               "system_temp_root": str(Path(tempfile.gettempdir()).resolve())}
                if original_location.exists():
                    if source.read(source.bound(source.binding(original_location), root=records)) != coordinates:
                        raise ValueError("完整后继原位置凭证坐标漂移")
                else:
                    source.write(original_location, coordinates)
            yield directory, intent_path, intent_digest, first_started

        with successor_directory() as (directory, intent_path, intent_digest, first_started):
            data = directory / "staging/data"
            driver._require_client_staging(data)
            invocation = directory
            if any((directory / name).exists() for name in ("stdout.log", "stderr.log", "record.json", "record-pending.json")):
                invocations = directory / "invocations"
                invocations.mkdir(mode=0o700, exist_ok=True)
                invocation = invocations / f"{len(list(invocations.iterdir())) + 1:04d}"
                invocation.mkdir(mode=0o700)
            command = ["unshare", "-m", "-n", sys.executable, "-m", driver.ACTIVATION_MODULE,
                       "--data", str(data), "--recorded", recorded, "--freeze", freeze, "--through", "VC-6",
                       "--complete-main-model",
                       "--parent-namespaces", json.dumps({kind: os.readlink(f"/proc/self/ns/{kind}") for kind in ("mnt", "net")})]
            if intent_path is not None:
                command.extend(["--budget-carry-intent", intent_path, "--budget-carry-intent-sha256", intent_digest])
            try:
                completed = subprocess.run(command, cwd=driver.vc1.REPOSITORY, capture_output=True, text=True,
                                           # 本次挂死预算只计本轮实际调用；原两账、业务截止和首跑时刻保持不变。
                                           timeout=min(time_budget["chain_timeout_seconds"], max(1, time_budget["chain_budget_seconds"] - (time.monotonic() - started))),
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(directory.parent)})
            except subprocess.TimeoutExpired as error:
                for filename, content in (("stdout.log", error.stdout or ""), ("stderr.log", error.stderr or "")):
                    value = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
                    (invocation / filename).write_text(value, encoding="utf-8")
                save_record(invocation, {"scenario": "vc-chain.claude-full-validation-only", "fixture_only": True,
                                        "status": "unverified", "timeout": True, "command": command,
                                        "seconds": time.time() - first_started, "first_started_at_epoch": first_started,
                                        "invocation_seconds": time.monotonic() - started,
                                        "stdout": source.binding(invocation / "stdout.log"),
                                        "stderr": source.binding(invocation / "stderr.log"),
                                        "formal_version_semantics_certified": False})
                raise
            (invocation / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (invocation / "stderr.log").write_text(completed.stderr, encoding="utf-8")
            report_path = data / "control/recorded-full-chain-result.json"
            report, report_error = None, None
            try:
                if report_path.is_file():
                    report = source.read(report_path)
            except Exception as error:
                report_error = str(error)
            seconds = time.time() - first_started
            invocation_seconds = time.monotonic() - started
            record = {"scenario": "vc-chain.claude-full-validation-only", "fixture_only": True,
                      "seconds": seconds, "first_started_at_epoch": first_started, "invocation_seconds": invocation_seconds,
                      "command": command, "returncode": completed.returncode,
                      "stdout": source.binding(invocation / "stdout.log"), "stderr": source.binding(invocation / "stderr.log"),
                      "status": "unverified", "report": source.binding(report_path) if report_path.is_file() else None,
                      "report_payload": report, "report_error": report_error, "formal_version_semantics_certified": False}
            save_record(invocation, record, append=False)
            try:
                self.assertEqual(completed.returncode, 0, completed.stderr[-12000:] + completed.stdout[-12000:])
                self.assertIsNone(report_error, "真实完整链报告不可读取")
                self.assertIsNotNone(report, "真实完整链未形成报告")
                self.assertEqual(report["schema_version"], driver.FULL_SCHEMA)
                self.assertEqual(report["registered_chains"], ["vc-chain.claude-full-validation-only"])
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["scope"], "VC-0→VC-6")
                self.assertTrue(report["fixture_only"])
                self.assertTrue(report["full_vc0_vc6_certified"])
                self.assertFalse(report["formal_version_semantics_certified"])
                self.assertEqual(report["network_requests_this_run"], 0)
                self.assertEqual(set(report["checkpoints"]), {f"VC-{index}" for index in range(7)})
                self.assertLessEqual(invocation_seconds, time_budget["chain_budget_seconds"], "ARM64 录制完整链本次调用超过当前独立预算")
                source.bound(report["source_state"], root=data)
                source.bound(report["continuation"], root=data)
            except BaseException:
                save_record(invocation, record)
                raise
            self.real_chain_metrics = {"network_isolated": True, "live_request_count": 0,
                                       "seconds": invocation_seconds, "cumulative_wall_seconds": seconds,
                                       "first_started_at_epoch": first_started,
                                       "time_budget": time_budget, "report": source.binding(report_path)}
            save_record(invocation, {**record, "status": "passed", "metrics": self.real_chain_metrics})


if __name__ == "__main__":
    unittest.main()
