"""ARM64 抓包驱动链（tools/arm64_capture_driver）的离线测试（2026-09-22 驱动脚本入库）。

覆盖老板修订方案第 3～5 条：
* 清单闭合：manifest.json 与目录内容一致（改脚本忘刷清单即红）、篡改即拒绝；
* 组合安装收据：install 绑定 control/ 下最新的受管工具部署收据，verify 在工具重新部署后失败、
  文件漂移／多余文件失败；
* 权限收口幂等：manifest 不存在时重复执行 chmod/chown 调用为 0；manifest 生成后二次进入 → 所有绑定
  stat（按 manifest 动态条目集比较，不写死条目数）全等且 chmod/chown 调用为 0；manifest 存在时发现
  不合规条目只报告不修改；manifest 生成前中断可续作；
* 漂移后读侧诊断为 evidence-integrity（模块层，端到端见 test_codex_upgrade_evidence_integrity）；
* vc5-all 按阶段状态续跑：compare／acceptance 结果存在时不再进入 seal／accept 链，目标平台门禁重跑不进 seal 链；
* 2026-09-22 审核三条：参数文件经 parse_env.py 安全解析（命令替换／反引号／分号／未知键／缺键一律拒绝且不执行）；
  manifest 存在时 vc5-seal.sh 不再派发任何写动作、前置缺失即失败关闭；install.py 顶层精确闭合、manifest 自身 0600。
* 修好接着跑第 67 项：目标平台门禁与 ARM64 全量回归门禁在 make test 前用 bytecode_cache.py 把标准库与测试树 tools
  预编译进树外缓存再只读使用（空前缀会让标准库 .pyc 也读不到、不设前缀要每次编译受管模块，子进程启动慢会把监督器
  计时用例拖红）；缓存建不成即停。
* VC-0 预跑目标平台门禁（vc0-gate-target.sh）：复用 vc5-gate-target.sh 同一套执行方式，独立主体标识、门禁根与字节码
  缓存都在 $RUNROOT/vc0-preflight 下，不写候选门禁目录；测试树与 VC-5 的 gates.sh prepare 共用 lib.sh 的 clone_test_tree。
* 测试一律在采集主机执行：ARM64 全量门禁（arm64-full-gates.sh）与 ARM64 版 VC-4 门禁（arm64-vc4-gates.sh，替代本机
  local-vc4.sh）经 lib.sh 的 isolated_run 执行，隔离方式与目标平台门禁逐字相同；VC-4 门禁的交付物与本机上传逐字段同格式。

bash 用例只调用脚本本身，chmod／chown 经 PATH 注入的计数包装（记录调用后转调真实命令）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER_ROOT = REPO_ROOT / "tools" / "arm64_capture_driver"
SCRIPTS = DRIVER_ROOT / "driver"


def _load_driver():
    import importlib.util

    spec = importlib.util.spec_from_file_location("arm64_capture_driver_install", DRIVER_ROOT / "install.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _counting_bin(root: Path) -> tuple[Path, Path]:
    """PATH 前置目录：chmod／chown 包装记录每次调用（每行一次）后转调真实命令；返回 (bin 目录, 记录文件)。"""

    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    log = root / "calls.log"
    log.touch()
    for name in ("chmod", "chown"):
        real = shutil.which(name)
        assert real, name
        wrapper = bin_dir / name
        wrapper.write_text(
            "#!/bin/bash\n"
            f"echo \"{name} $*\" >> '{log}'\n"
            f"if [ -n \"${{CLOSEOUT_TEST_FAIL_FIRST:-}}\" ] && [ ! -e '{root}/failed-once' ]; then touch '{root}/failed-once'; exit 1; fi\n"
            f"exec {real} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
    return bin_dir, log


def _run(script: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, errors="replace", env=merged, cwd=str(cwd) if cwd else None)


class DriverManifestTests(unittest.TestCase):
    def test_manifest_matches_directory(self) -> None:
        """manifest.json 必须与目录内容逐字一致（改脚本必须重新 build-manifest）。"""

        current = driver.load_manifest(DRIVER_ROOT)
        rebuilt = driver.build_manifest(DRIVER_ROOT)
        self.assertEqual(current, rebuilt, "manifest.json 与 tools/arm64_capture_driver 目录内容不一致，请执行 install.py build-manifest")
        paths = [row["path"] for row in current["files"]]
        for required in ("install.py", "README.md", "driver/lib.sh", "driver/guard.sh", "driver/vc5-all.sh", "driver/vc5-permission-closeout.sh", "driver/vc5-seal-receipts.sh"):
            self.assertIn(required, paths)
        for row in current["files"]:
            self.assertEqual(row["mode"], "0700" if row["path"].endswith((".sh", ".py")) else "0600")

    def test_manifest_rejects_tampering(self) -> None:
        payload = driver.load_manifest(DRIVER_ROOT)
        tampered = json.loads(json.dumps(payload))
        tampered["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(driver.DriverError, "自摘要不一致"):
            driver.validate_manifest(tampered)
        extra = json.loads(json.dumps(payload))
        extra["files"].append({"path": "driver/zzz.sh", "sha256": "1" * 64, "mode": "0700", "bytes": 1})
        extra["file_count"] += 1
        with self.assertRaises(driver.DriverError):
            driver.validate_manifest(extra)

    def test_bash_scripts_parse(self) -> None:
        for script in sorted(SCRIPTS.rglob("*.sh")):
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")


class DriverInstallTests(unittest.TestCase):
    def _deploy_receipt(self, control: Path, stamp: str, tool_sha: str) -> Path:
        path = control / f"codex-0154-supervisor-enable-{stamp}.json"
        _write_json(path, {"status": "enabled", "tool_files_sha256": tool_sha, "supervisor_sha256": "s" * 64})
        return path

    def test_install_and_verify_bind_latest_deployment_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            data_root = root / "data"
            control = data_root / "control"
            control.mkdir(parents=True, mode=0o700)
            self._deploy_receipt(control, "20260922t000000z", "a" * 64)
            latest = self._deploy_receipt(control, "20260922t010000z", "b" * 64)
            target = root / "arm64-capture-driver"
            with mock.patch.object(driver, "_running_as_root", return_value=False), mock.patch.object(driver, "_require_root", return_value=None):
                # 未安装：verify 失败
                with self.assertRaisesRegex(driver.DriverError, "不存在或不可信"):
                    driver.verify_install(target, data_root)
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = driver.main(["install", "--source", str(DRIVER_ROOT), "--target", str(target), "--data-root", str(data_root)])
                self.assertEqual(rc, 0)
                receipts = sorted(control.glob("arm64-capture-driver-install-*.json"))
                self.assertEqual(len(receipts), 1)
                receipt = driver.validate_install_receipt(json.loads(receipts[0].read_text(encoding="utf-8")))
                self.assertEqual(receipt["deployment_receipt"]["path"], latest.name)
                self.assertEqual(receipt["deployment_receipt"]["tool_files_sha256"], "b" * 64)
                self.assertEqual(receipt["manifest_sha256"], driver.load_manifest(DRIVER_ROOT)["manifest_sha256"])
                self.assertEqual(receipt["file_count"], len(receipt["files"]))
                # 安装模式：脚本 0700、其余 0600、目录 0700
                for row in receipt["files"]:
                    mode = stat.S_IMODE((target / row["path"]).stat().st_mode)
                    self.assertEqual(f"{mode:04o}", row["mode"], row["path"])
                for sub in (target, target / "driver", target / "driver" / "local"):
                    self.assertEqual(stat.S_IMODE(sub.stat().st_mode), 0o700)
                self.assertFalse(any(p.name == "__pycache__" for p in target.rglob("*")))
                verified = driver.verify_install(target, data_root)
                self.assertEqual((verified["status"], verified["deployment_receipt"]), ("verified", latest.name))
                # 受管工具重新部署（更新的 enable 收据出现）→ 旧安装收据的绑定不再是最新 → verify 失败
                newest = self._deploy_receipt(control, "20260922t020000z", "c" * 64)
                with self.assertRaisesRegex(driver.DriverError, "不是当前最新"):
                    driver.verify_install(target, data_root)
                # 重新 install → 绑定最新 → 通过；旧目标被保留为 previous-*
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(driver.main(["install", "--source", str(DRIVER_ROOT), "--target", str(target), "--data-root", str(data_root)]), 0)
                verified = driver.verify_install(target, data_root)
                self.assertEqual(verified["deployment_receipt"], newest.name)
                self.assertEqual(len(list(root.glob("arm64-capture-driver.previous-*"))), 1)
                # 文件漂移 → verify 失败
                guard = target / "driver" / "guard.sh"
                original = guard.read_bytes()
                guard.write_bytes(original + b"\n# drift\n")
                with self.assertRaisesRegex(driver.DriverError, "摘要或字节数漂移"):
                    driver.verify_install(target, data_root)
                guard.write_bytes(original)
                self.assertEqual(driver.verify_install(target, data_root)["status"], "verified")
                # 模式漂移 → verify 失败
                guard.chmod(0o600)
                with self.assertRaisesRegex(driver.DriverError, "安装模式不符"):
                    driver.verify_install(target, data_root)
                guard.chmod(0o700)
                # 多余文件 → verify 失败
                stray = target / "driver" / "stray.sh"
                stray.write_text("#!/bin/bash\n", encoding="utf-8")
                with self.assertRaisesRegex(driver.DriverError, "不闭合"):
                    driver.verify_install(target, data_root)
                stray.unlink()
                self.assertEqual(driver.verify_install(target, data_root)["status"], "verified")
                # 顶层多余文件（审核 P2：清单之外的 unexpected.py）→ verify 失败
                unexpected = target / "unexpected.py"
                unexpected.write_text("print('x')\n", encoding="utf-8")
                with self.assertRaisesRegex(driver.DriverError, "清单之外的顶层条目"):
                    driver.verify_install(target, data_root)
                unexpected.unlink()
                stray_dir = target / "__pycache__"
                stray_dir.mkdir()
                with self.assertRaisesRegex(driver.DriverError, "清单之外的顶层条目"):
                    driver.verify_install(target, data_root)
                stray_dir.rmdir()
                # manifest 自身模式（审核 P2：安装态必须 0600）
                self.assertEqual(stat.S_IMODE((target / "manifest.json").stat().st_mode), 0o600)
                (target / "manifest.json").chmod(0o644)
                with self.assertRaisesRegex(driver.DriverError, "驱动清单模式不是 0600"):
                    driver.verify_install(target, data_root)
                (target / "manifest.json").chmod(0o600)
                self.assertEqual(driver.verify_install(target, data_root)["status"], "verified")


class PermissionCloseoutTests(unittest.TestCase):
    CLOSEOUT = SCRIPTS / "vc5-permission-closeout.sh"

    def _attempt(self, root: Path) -> tuple[Path, Path]:
        attempt = root / "attempts" / "20260922T000000Z-0123456789abcdef"
        client = attempt / "evidence" / "client"
        for sub in ("raw", "generated/kilo", "receipts"):
            (client / sub).mkdir(parents=True)
        for directory in (root / "attempts", attempt, attempt / "evidence"):
            directory.chmod(0o700)
        files = {
            "raw/kilo-facts.json": {"observations": {}},
            "raw/profile-activation-fact.json": {"profile_id": "x"},
            "generated/kilo/kilo-installation.json": {"client_version": "7.7.501"},
            "generated/observed-profile-runtime-audit.json": {"audit": 1},
            "receipts/observed-profile-receipt.json": {"receipt": 1},
            "receipts/kilo-compatible-receipt.json": {"receipt": 2},
            "receipts/kilo-responses-receipt.json": {"receipt": 3},
        }
        for name, payload in files.items():
            path = client / name
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            path.chmod(0o644)  # 故意不合规
        for directory in [client, *(p for p in client.rglob("*") if p.is_dir())]:
            directory.chmod(0o755)  # 故意不合规
        return attempt, client

    def _env(self, bin_dir: Path) -> dict[str, str]:
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CLOSEOUT_OWNER": pwd.getpwuid(os.getuid()).pw_name}

    @staticmethod
    def _stat_rows(evidence_root: Path) -> list[dict]:
        rows = evidence_manifest.preflight_evidence_roots([evidence_root])["entries"]
        return [{k: v for k, v in row.items() if k != "absolute_path"} for row in rows]

    def test_closeout_is_idempotent_and_manifest_is_a_hard_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            bin_dir, log = _counting_bin(root)
            env = self._env(bin_dir)
            attempt, client = self._attempt(root)
            evidence_root = attempt / "evidence"

            # 1. 首次收口：不合规条目被修正
            first = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stdout.splitlines()[0])
            self.assertEqual(summary["mode"], "closeout")
            self.assertGreater(summary["changed_dirs"], 0)
            self.assertGreater(summary["changed_files"], 0)
            self.assertIn("PERMISSION_CLOSEOUT_DONE", first.stdout)
            for path in client.rglob("*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)
            calls_after_first = log.read_text(encoding="utf-8").count("\n")
            self.assertGreater(calls_after_first, 0)

            # 2. manifest 尚不存在时重复执行：零调用、零变化
            second = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(second.returncode, 0, second.stderr)
            summary = json.loads(second.stdout.splitlines()[0])
            self.assertEqual((summary["changed_dirs"], summary["changed_files"], summary["changed_owners"]), (0, 0, 0))
            self.assertEqual(log.read_text(encoding="utf-8").count("\n"), calls_after_first, "重复收口不得再调用 chmod/chown")

            # 3. 生成 EvidenceManifest（真实工具），记录绑定的全部 stat 条目（动态条目集，不写死数量）
            manifest = evidence_manifest.build_evidence_manifest(
                [evidence_root], checkpoint_path=attempt / "evidence-manifest.checkpoint.json", secret_env_names=()
            )
            _write_json(attempt / "evidence-manifest.json", manifest)
            bound_before = self._stat_rows(evidence_root)
            self.assertEqual(len(bound_before), manifest["entry_count"])
            self.assertGreaterEqual(len(bound_before), 6)

            # 4. 二次进入（manifest 存在）：只核对，chmod/chown 零调用，全部绑定 stat 全等，读侧复核通过
            third = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(third.returncode, 0, third.stderr)
            summary = json.loads(third.stdout.splitlines()[0])
            self.assertEqual((summary["mode"], summary["manifest_present"]), ("verify-only", True))
            self.assertEqual((summary["noncompliant_dirs"], summary["noncompliant_files"], summary["noncompliant_owners"]), (0, 0, 0))
            self.assertIn("PERMISSION_CLOSEOUT_VERIFIED", third.stdout)
            self.assertEqual(log.read_text(encoding="utf-8").count("\n"), calls_after_first, "manifest 存在后不得调用 chmod/chown")
            self.assertEqual(self._stat_rows(evidence_root), bound_before)
            verdict = evidence_manifest.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual((verdict["status"], verdict["scanned_bytes"]), ("passed", 0))

            # 5a. manifest 存在时出现不合规条目：只报告（退出 3），不修改
            drifted = client / "receipts" / "kilo-responses-receipt.json"
            drifted.chmod(0o640)
            fourth = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(fourth.returncode, 3)
            summary = json.loads(fourth.stdout.splitlines()[0])
            self.assertEqual(summary["noncompliant_files"], 1)
            self.assertIn("PERMISSION_CLOSEOUT_VERIFY_FAILED", fourth.stdout)
            self.assertEqual(stat.S_IMODE(drifted.stat().st_mode), 0o640, "manifest 存在时脚本不得修改条目")
            self.assertEqual(log.read_text(encoding="utf-8").count("\n"), calls_after_first)
            # 5b. 事故形态：模式改回 0600（与 manifest 一致）但 ctime 已漂移 → 第三批 R3 起读侧判可恢复的
            # evidence-metadata-drift（内容未变，rebind-boundary 复算内容后可继续），不再是 evidence-integrity 永久停线。
            drifted.chmod(0o600)
            fifth = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(fifth.returncode, 0, fifth.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").count("\n"), calls_after_first)
            with self.assertRaises(evidence_manifest.EvidenceManifestMetadataDriftError) as caught:
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual(caught.exception.failure_class, evidence_manifest.METADATA_DRIFT_FAILURE_CLASS)
            self.assertEqual(
                caught.exception.failure_observations,
                [{"check_id": evidence_manifest.BOUNDARY_DRIFT_CHECK_ID, "failure_code": evidence_manifest.METADATA_DRIFT_FAILURE_CODE}],
            )
            self.assertEqual([entry["path"] for entry in caught.exception.drifted_entries], [drifted.relative_to(attempt).as_posix()])

    def test_closeout_resumes_after_interruption_before_manifest(self) -> None:
        """manifest 生成前收口中断（chmod 第一次调用失败）：再次执行即完成，不留半成品状态。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            bin_dir, log = _counting_bin(root)
            env = self._env(bin_dir)
            attempt, client = self._attempt(root)
            interrupted = _run(self.CLOSEOUT, str(attempt), env={**env, "CLOSEOUT_TEST_FAIL_FIRST": "1"})
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertNotIn("PERMISSION_CLOSEOUT_DONE", interrupted.stdout)
            self.assertTrue(any(stat.S_IMODE(p.stat().st_mode) != (0o700 if p.is_dir() else 0o600) for p in client.rglob("*")), "中断后应仍有未收口条目")
            resumed = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("PERMISSION_CLOSEOUT_DONE", resumed.stdout)
            for path in client.rglob("*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)
            again = _run(self.CLOSEOUT, str(attempt), env=env)
            summary = json.loads(again.stdout.splitlines()[0])
            self.assertEqual((summary["changed_dirs"], summary["changed_files"]), (0, 0))

    def test_seal_receipts_verify_only_when_manifest_exists(self) -> None:
        """vc5-seal-receipts.sh：manifest 存在时不生成、不 mkdir、不 chmod，只核对收据齐全（缺失即退出 3）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            bin_dir, log = _counting_bin(root)
            fixture = _DriverFixture(root)
            attempt, client = self._attempt(fixture.newdir / "candidates" / fixture.cand)
            for path in client.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            client.chmod(0o700)
            _write_json(attempt / "attempt.json", {
                "campaign_id": fixture.new, "run_nonce": "n" * 64, "started_at_utc": "2026-09-22T00:00:00Z",
                "identity": {"image_id": "sha256:" + "0" * 64, "image_reference": "x@sha256:" + "0" * 64, "source_tree_sha256": "t" * 64, "build_id": "b", "deployed_version": "0.154.0", "profile_id": "p", "profile_digest": "d" * 64, "source_root": "/src"},
            })
            manifest = evidence_manifest.build_evidence_manifest([attempt / "evidence"], checkpoint_path=attempt / "evidence-manifest.checkpoint.json", secret_env_names=())
            _write_json(attempt / "evidence-manifest.json", manifest)
            bound_before = self._stat_rows(attempt / "evidence")
            env = {**fixture.env, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CLOSEOUT_OWNER": pwd.getpwuid(os.getuid()).pw_name}
            result = _run(SCRIPTS / "vc5-seal-receipts.sh", attempt.name, "2026-09-22T00:10:00Z", env=env, cwd=fixture.data_root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SEAL_RECEIPTS_VERIFIED", result.stdout)
            self.assertEqual(log.read_text(encoding="utf-8"), "", "manifest 存在时 seal-receipts 不得调用 chmod/chown")
            self.assertEqual(self._stat_rows(attempt / "evidence"), bound_before)
            self.assertEqual(evidence_manifest.verify_manifest_boundary(manifest, [attempt / "evidence"])["status"], "passed")
            # 收据缺失：不可补写，退出 3
            (client / "receipts" / "kilo-responses-receipt.json").unlink()
            missing = _run(SCRIPTS / "vc5-seal-receipts.sh", attempt.name, "2026-09-22T00:10:00Z", env=env, cwd=fixture.data_root)
            self.assertEqual(missing.returncode, 3, missing.stdout + missing.stderr)
            self.assertIn("收据缺失", missing.stdout)
            self.assertEqual(log.read_text(encoding="utf-8"), "")


class _DriverFixture:
    """一个最小的"采集主机"布局：数据根、Campaign 目录、参数文件；供脚本级用例 source lib.sh。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.round = "vtest"
        self.stamp = "20260922t000000z"
        self.data_root = root / "data"
        self.runroot = root / "runroot"
        self.new = f"c0154-formal-vc5-{self.round}-{self.stamp}"
        self.inputs = f"c0154-vc5-{self.round}-inputs-{self.stamp}"
        self.up = f"codex-0151-to-0154-vc5-{self.round}-{self.stamp}"
        self.cand = f"c0154-candidate-{self.round}"
        self.newdir = self.data_root / "evidence" / "campaigns" / self.new
        for sub in ("control", "evidence/campaigns", "candidates", "staging", "tools/official_client_capture"):
            (self.data_root / sub).mkdir(parents=True, exist_ok=True)
        (self.newdir / "control" / "vc" / "batches").mkdir(parents=True)
        self.candidate_dir = self.data_root / "candidates" / self.cand
        self.candidate_dir.mkdir(parents=True)
        self.runroot.mkdir(mode=0o700)
        self.env_file = self.runroot / "env.sh"
        values = {
            "ROUND": self.round, "STAMP": self.stamp, "D": str(self.data_root), "RUNROOT": str(self.runroot),
            "NEW": self.new, "IN": self.inputs, "UP": self.up, "CAND": self.cand, "B": str(self.candidate_dir),
            "PREV_CANDIDATE": str(self.data_root / "candidates" / "prev"), "HISTORY_TEST_TREE": str(self.data_root / "candidates" / "hist"),
            "C": "c" * 40, "DC": "d" * 40, "RECEIPT": "docs/egress/maintenance/x.json",
            "BUNDLE": str(self.data_root / "staging" / "x.bundle"), "BUNDLE_BRANCH": "codex/x",
            "OFFICIAL_CAMPAIGN": str(self.data_root / "evidence" / "campaigns" / "official"), "OFFICIAL_STOP_LEDGER": "/x", "OFFICIAL_STOP_RECEIPT": "r.json",
            "INPUT_RULE_MIGRATION": "/x/m.json", "INPUT_TARGET_SNAPSHOT": "/x/s.json", "PROJECT_DEADLINE_UTC": "2026-09-28T15:59:00Z",
            "STAGE_BUDGETS": "VC-0=45 VC-5=600", "MIN_FREE_GIB": "40", "FRONTEND_DEVIATION_APPROVED_BY": "test",
            "PROFILE_ID": "codex-0.156.1-official", "PROFILE_DIGEST": "3" * 64,
            "KILO_BIN": "/x/kilo", "KILO_VERSION": "7.7.501", "KILO_SHA256": "c" * 64,
            "COMPOSE_DIR": str(root / "compose"), "COMPOSE_BACKUP": str(root / "compose" / "backup.yml"), "PRODUCTION_IMAGE": "ghcr.io/x:1",
        }
        values.update({
            "BASELINE_VERSION": "0.154.0", "TARGET_VERSION": "0.156.1", "TARGET_PROFILE_ID": values["PROFILE_ID"],
            "CODEX_BIN": "/opt/codex-0.156.1/bin/codex", "CODEX_BIN_SHA256": "a" * 64, "OFFICIAL_ASSET_SHA256": "b" * 64,
            "MAIN_MODEL": "fixture-main", "LITE_MODEL": "fixture-lite", "CODEX_ACCOUNT_ID": "91", "API_KEY_ID": "92",
            "PREDECESSOR_CAMPAIGN": values["OFFICIAL_CAMPAIGN"], "POLICY_COMPAT_RECEIPT": "/x/compatibility.json",
            "POLICY_ACTIVATION": "/x/activation.json", "RELEASE_CERTIFICATION": "/x/release.json",
        })
        self.env_file.write_text("".join(f"{k}=\"{v}\"\n" for k, v in values.items()), encoding="utf-8")
        self.env_file.chmod(0o600)
        self.env = {"ARM64_VC_ENV": str(self.env_file)}


class Vc5AllResumeTests(unittest.TestCase):
    """vc5-all.sh 以 stub 子脚本运行：只验证阶段续跑分支，不触碰任何受管工具。"""

    STUBS = ("vc5-start.sh", "vc5-seal.sh", "vc5-accept.sh", "vc5-canonical2.sh", "gates.sh", "guard.sh")

    def _stub_driver(self, root: Path, fixture: _DriverFixture, *, accept_creates_result: bool) -> tuple[Path, Path]:
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "wait_state.py", "vc5-all.sh"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        calls = root / "stub-calls.log"
        calls.touch()
        for name in self.STUBS:
            body = f"#!/bin/bash\necho \"{name} $*\" >> '{calls}'\n"
            if name == "vc5-accept.sh" and accept_creates_result:
                body += f"mkdir -p '{fixture.newdir}/acceptance/{fixture.cand}' && echo '{{}}' > '{fixture.newdir}/acceptance/{fixture.cand}/result.json'\n"
            if name == "vc5-canonical2.sh":
                body += f"mkdir -p '{fixture.newdir}/control/vc/receipts/{fixture.cand}' && echo '{{}}' > '{fixture.newdir}/control/vc/receipts/{fixture.cand}/vc5-completion.json'\n"
            body += "echo CANONICAL2_DONE STUB_OK\n"
            (drv / name).write_text(body, encoding="utf-8")
            (drv / name).chmod(0o700)
        return drv, calls

    def _prepare_stage(self, fixture: _DriverFixture, *, compare_done: bool, acceptance_done: bool) -> None:
        gates = fixture.data_root / "control" / f"{fixture.new}-candidate-gates" / "local"
        gates.mkdir(parents=True)
        _write_json(gates / "full-regression.gate.json", {"exit_code": 0, "tree_head": "d" * 40, "host": "h", "architecture": "a", "started_at_utc": "s", "completed_at_utc": "e"})
        (fixture.runroot / "vc5-run-batch.out").write_text("rc=0\nRUN_BATCH_DONE 2026-09-22T00:00:00Z\n", encoding="utf-8")
        attempt = fixture.newdir / "candidates" / fixture.cand / "attempts" / "20260922T000000Z-0123456789abcdef"
        attempt.mkdir(parents=True)
        _write_json(attempt / "attempt.json", {"status": "awaiting_receipts", "attempt_id": attempt.name, "started_at_utc": "2026-09-22T00:00:00Z", "results": [{"status": "complete"}] * 10})
        if compare_done:
            _write_json(fixture.newdir / "comparisons" / fixture.cand / "result.json", {"status": "complete"})
        if acceptance_done:
            _write_json(fixture.newdir / "acceptance" / fixture.cand / "result.json", {"status": "complete"})

    def test_skips_seal_and_accept_when_results_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            drv, calls = self._stub_driver(root, fixture, accept_creates_result=False)
            self._prepare_stage(fixture, compare_done=True, acceptance_done=True)
            result = _run(drv / "vc5-all.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            called = [line.split()[0] for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(called, ["vc5-canonical2.sh"], "compare／acceptance 结果存在时只允许进入 canonical 交接")
            self.assertIn("seal 链已完成", result.stdout)
            self.assertIn("accept 已完成", result.stdout)
            self.assertIn("VC5_ALL_DONE", result.stdout)

    def test_reruns_accept_only_when_compare_exists(self) -> None:
        """目标平台门禁重跑场景：compare 已封存、acceptance 缺失 → 只进 accept（门禁在其内重跑），不进 seal 链。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            drv, calls = self._stub_driver(root, fixture, accept_creates_result=True)
            self._prepare_stage(fixture, compare_done=True, acceptance_done=False)
            result = _run(drv / "vc5-all.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            called = [line.split()[0] for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(called, ["vc5-accept.sh", "vc5-canonical2.sh"])
            self.assertNotIn("vc5-seal.sh", called)
            self.assertNotIn("vc5-start.sh", called)

    def test_lib_rejects_env_with_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            with fixture.env_file.open("a", encoding="utf-8") as handle:
                handle.write("rm -rf /\n")
            drv, _calls = self._stub_driver(root, fixture, accept_creates_result=False)
            result = _run(drv / "vc5-all.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 2)
            self.assertIn("参数文件拒绝加载", result.stdout + result.stderr)


class EnvFileParserTests(unittest.TestCase):
    """parse_env.py：绝不执行参数文件里的任何内容（审核 P1）。"""

    PARSER = SCRIPTS / "parse_env.py"

    @staticmethod
    def _template() -> str:
        text = (SCRIPTS / "env.example.sh").read_text()
        for key in ("REPLACE_APPROVED_PROFILE_SHA256", "REPLACE_OFFICIAL_CODEX_SHA256", "REPLACE_OFFICIAL_PACKAGE_SHA256", "REPLACE_KILO_SHA256"):
            text = text.replace(key, "a" * 64)
        return text.replace("REPLACE_CODEX_ACCOUNT_ID", "91").replace("REPLACE_API_KEY_ID", "92")

    def _parse(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            path = handle.name
        try:
            return subprocess.run([sys.executable, str(self.PARSER), path], capture_output=True, text=True)
        finally:
            os.unlink(path)

    def test_template_parses_and_expands_references(self) -> None:
        result = self._parse(self._template())
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        exported = dict(line[len("export "):].split("=", 1) for line in lines if line.startswith("export "))
        parser = load_script("parse_env")
        values = parser.parse(self._template())
        self.assertEqual(set(exported), set(values) | set(parser.derive(values)))
        # 参数文件里没有、也没有派生默认值的可选键一律 unset：清掉外层环境残留的旧值（E2-06 验收时发现会被继承回来）。
        unset = {line[len("unset "):] for line in lines if line.startswith("unset ")}
        self.assertEqual(unset, set(parser.OPTIONAL_KEYS) - set(exported))
        self.assertIn("ENTRY_COMMIT", unset)
        self.assertEqual(exported["NEW"], "codex-9.1.0-formal-round1-YYYYMMDDtHHMMSSz")
        self.assertEqual(exported["B"], "/root/docker/capture-cli/data/candidates/codex-9.1.0-candidate-round1")
        # R20：示例阶段预算按实测标定（VC-0 接入目标平台门禁预跑后为 165 分钟起）；这里只验证带空格的值被原样加引号导出。
        self.assertTrue(exported["STAGE_BUDGETS"].startswith("'VC-0=165 "))
        # 输出的每一行都是可安全 eval 的单一赋值或单一 unset
        for line in lines:
            self.assertRegex(line, r"^(export [A-Z_][A-Z0-9_]*=('[^']*'|[A-Za-z0-9_./:@%+=,-]+)|unset [A-Z_][A-Z0-9_]*)$")

    def test_rejects_command_forms_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            template = self._template()
            cases = {
                "命令替换": template.replace("ROUND=round1", f"ROUND=$(touch {marker})"),
                "反引号": template.replace("ROUND=round1", f"ROUND=`touch {marker}`"),
                "分号": template.replace("ROUND=round1", f"ROUND=round1; touch {marker}"),
                "算术展开": template.replace("MIN_FREE_GIB=40", "MIN_FREE_GIB=$((40))"),
                "未知键": template + f"EXTRA=$(touch {marker})\n",
                "缺键": template.replace("KILO_VERSION=REPLACE_KILO_VERSION\n", ""),
                "引用未定义键": template.replace("ROUND=round1", "ROUND=$LATER"),
                "非赋值行": template + f"touch {marker}\n",
                "内嵌引号": template.replace("ROUND=round1", "ROUND=v14'r5"),
                "重复键": template + "ROUND=v14r6\n",
                "错误 sha": template.replace("C=0000000000000000000000000000000000000000", "C=abc"),
            }
            for name, text in cases.items():
                result = self._parse(text)
                self.assertEqual(result.returncode, 2, f"{name} 应被拒绝：{result.stdout}")
                self.assertIn("参数文件拒绝加载", result.stderr, name)
                self.assertFalse(marker.exists(), f"{name} 不得执行参数文件内容")

    def test_lib_never_executes_env_file(self) -> None:
        """端到端：任何一个驱动脚本 source lib.sh 时，参数文件里的命令也绝不会跑。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            marker = root / "marker"
            drv = root / "drv"
            drv.mkdir(mode=0o700)
            for name in ("lib.sh", "parse_env.py", "vc5-permission-closeout.sh"):
                (drv / name).write_bytes((SCRIPTS / name).read_bytes())
            probe = drv / "probe.sh"
            probe.write_text("#!/bin/bash\nset -Eeuo pipefail\nsource \"$(dirname \"${BASH_SOURCE[0]}\")/lib.sh\"\necho \"PROBE_OK ROUND=$ROUND\"\n", encoding="utf-8")
            fixture = _DriverFixture(root)
            with fixture.env_file.open("a", encoding="utf-8") as handle:
                handle.write(f"EXTRA=$(touch {marker})\n")
            result = _run(probe, env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("参数文件拒绝加载", result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            # 合法文件：正常加载并展开引用
            fixture2 = _DriverFixture(root / "second")
            ok = _run(probe, env=fixture2.env, cwd=root)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("PROBE_OK ROUND=vtest", ok.stdout)
            # 外层环境里残留的可选参数（上一份参数文件导出的）不会被带进本轮：参数文件里没有的一律清掉。
            stale = drv / "stale.sh"
            stale.write_text("#!/bin/bash\nset -Eeuo pipefail\nsource \"$(dirname \"${BASH_SOURCE[0]}\")/lib.sh\"\n"
                             "echo \"STALE=[${TARGET_CODE_MODE_HOST_SHA256-清掉了}] [${ENTRY_COMMIT-清掉了}]\"\n", encoding="utf-8")
            inherited = {**fixture2.env, "TARGET_CODE_MODE_HOST_SHA256": "0" * 64, "ENTRY_COMMIT": "1" * 40}
            cleared = _run(stale, env=inherited, cwd=root)
            self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
            self.assertIn("STALE=[清掉了] [清掉了]", cleared.stdout)


def load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("driver_test_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"D": str(REPO_ROOT)}), mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
        spec.loader.exec_module(module)
    return module


def driver_keys() -> tuple[str, ...]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("arm64_capture_driver_parse_env", SCRIPTS / "parse_env.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return tuple(module.REQUIRED_KEYS)


class Vc5SealReadOnlyTests(unittest.TestCase):
    """vc5-seal.sh：evidence-manifest.json 存在后不再派发任何写动作（审核 P1）。"""

    WRITE_STUBS = ("vc-batch.sh", "vc5-kilo.sh")

    def _stub_driver(self, root: Path) -> tuple[Path, Path]:
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "vc5-seal.sh"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        calls = root / "stub-calls.log"
        calls.touch()
        for name in (*self.WRITE_STUBS, "vc5-seal-receipts.sh"):
            (drv / name).write_text(f"#!/bin/bash\necho \"{name} $*\" >> '{calls}'\necho STUB_OK\n", encoding="utf-8")
            (drv / name).chmod(0o700)
        (drv / "gen_vc5_plans.py").write_text(
            "import os, sys\n"
            f"open('{calls}', 'a').write('gen_vc5_plans.py ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "os.makedirs(sys.argv[1], exist_ok=True)\n"
            "open(os.path.join(sys.argv[1], 'action-plan-vc5-stub.json'), 'w').write('{}\\n')\n"
            "print('plans stub')\n",
            encoding="utf-8",
        )
        (drv / "gen_vc5_plans.py").chmod(0o700)
        return drv, calls

    def _attempt(self, fixture: _DriverFixture, *, sealed: bool, complete: bool) -> Path:
        attempt = fixture.newdir / "candidates" / fixture.cand / "attempts" / "20260922T000000Z-0123456789abcdef"
        evidence = attempt / "evidence"
        (evidence / "client" / "raw").mkdir(parents=True)
        _write_json(attempt / "attempt.json", {"status": "awaiting_receipts", "attempt_id": attempt.name})
        _write_json(fixture.candidate_dir / "artifacts" / "build-parameters.json", {"docker_build": {"image_id": "sha256:" + "0" * 64}})
        _write_json(fixture.newdir / "candidates" / fixture.cand / "build-receipt.json", {"build": {"build_id": "b1"}})
        (fixture.data_root / "control" / fixture.inputs).mkdir(parents=True, exist_ok=True)
        _write_json(evidence / "client" / "raw" / "kilo-facts.json", {"observations": {}})
        if complete:
            _write_json(evidence / "environment" / "client-after" / "probe-manifest.json", {"observed_at_utc": "2026-09-22T00:10:00Z"})
            _write_json(evidence / "assertion-bundle" / "capture-manifest.json", {"m": 1})
            _write_json(attempt / "seal-preview.json", {"review_sha256": "r" * 64})
            _write_json(fixture.newdir / "candidates" / fixture.cand / "result.json", {"status": "sealed", "package_digest": "p", "attempt_id": attempt.name, "candidate_id": fixture.cand})
            _write_json(fixture.newdir / "comparisons" / fixture.cand / "result.json", {"status": "complete"})
        if sealed:
            _write_json(attempt / "evidence-manifest.json", {"schema_version": "codex-upgrade-evidence-manifest/v1"})
        return attempt

    def test_sealed_attempt_with_missing_prerequisite_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            drv, calls = self._stub_driver(root)
            attempt = self._attempt(fixture, sealed=True, complete=False)
            result = _run(drv / "vc5-seal.sh", attempt.name, env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("SEAL_ABORT: manifest 已存在但前置产物缺失", result.stdout)
            self.assertEqual(calls.read_text(encoding="utf-8"), "", "失败关闭之前不得调用任何写动作或生成计划")

    def test_sealed_attempt_only_reverifies_and_never_dispatches_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            drv, calls = self._stub_driver(root)
            attempt = self._attempt(fixture, sealed=True, complete=True)
            result = _run(drv / "vc5-seal.sh", attempt.name, env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            called = [line.split()[0] for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([name for name in called if name in self.WRITE_STUBS], [], "manifest 存在后不得派发 seal checkpoint／assertion／preview／Kilo")
            self.assertIn("vc5-seal-receipts.sh", called)
            self.assertIn("SEAL_DONE", result.stdout)
            self.assertIn("SEALED=1", result.stdout)

    def test_unsealed_attempt_dispatches_missing_steps(self) -> None:
        """对照：manifest 不存在时按产物缺失逐步派发（stub 不产生产物，脚本在 seal checkpoint 之后读取 probe-manifest 失败即停）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture = _DriverFixture(root)
            drv, calls = self._stub_driver(root)
            attempt = self._attempt(fixture, sealed=False, complete=False)
            result = _run(drv / "vc5-seal.sh", attempt.name, env=fixture.env, cwd=root)
            called = [line.split()[0] for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertIn("vc-batch.sh", called, result.stdout + result.stderr)
            self.assertNotIn("SEAL_ABORT: manifest 已存在", result.stdout)


class DriverParameterizationTests(unittest.TestCase):
    """R7：逐轮身份、动态集合与 Catalog 装配均走实际解析／验证函数。"""

    def test_all_new_identity_keys_are_required(self):
        new_keys = ('BASELINE_VERSION TARGET_VERSION TARGET_PROFILE_ID CODEX_BIN CODEX_BIN_SHA256 '
                    'OFFICIAL_ASSET_SHA256 MAIN_MODEL LITE_MODEL CODEX_ACCOUNT_ID API_KEY_ID '
                    'PREDECESSOR_CAMPAIGN POLICY_COMPAT_RECEIPT POLICY_ACTIVATION RELEASE_CERTIFICATION').split()
        parser = load_script('parse_env')
        with tempfile.TemporaryDirectory() as directory:
            fixture = _DriverFixture(Path(directory).resolve())
            source = fixture.env_file.read_text()
            for key in new_keys:
                with self.subTest(key=key), self.assertRaises(parser.EnvFileError):
                    parser.parse('\n'.join(line for line in source.splitlines() if not line.startswith(key + '=')))
            for key, value in [('TARGET_VERSION', 'latest'), ('CODEX_BIN_SHA256', 'bad'), ('API_KEY_ID', '0'),
                               ('TARGET_PROFILE_ID', 'other'), ('NEW', '../escape')]:
                with self.subTest(key=key), self.assertRaises(parser.EnvFileError):
                    parser.parse(re.sub('^' + key + '=.*$', key + '=' + value, source, flags=re.M))
        with self.assertRaises(parser.EnvFileError):
            parser.parse((SCRIPTS / 'env.example.sh').read_text())

    def test_driver_contains_no_version_digest_or_account_literals(self):
        patterns = {
            '版本': r'0[._]15[0-9]|c015[0-9]|codex-015[0-9]',
            '摘要': r'(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])',
            '账号': r'(?:--(?:codex-account-id|api-key-id)\s+|(?:CODEX_ACCOUNT_ID|API_KEY_ID)\s*=\s*[\"\x27]?|WHERE\s+id\s*=\s*|sched:acc:|账号\s+)[0-9]+',
        }
        for kind, pattern in patterns.items():
            for path in SCRIPTS.rglob('*'):
                if path.is_file():
                    with self.subTest(kind=kind, path=path.name):
                        self.assertIsNone(re.search(pattern, path.read_text(), re.I))
        for kind, sample in [('版本', 'codex-0.154.0'), ('摘要', 'a' * 64), ('账号', '--api-key-id 91'), ('账号', 'sched:acc:22'), ('账号', '账号 22 调度投影')]:
            self.assertIsNotNone(re.search(patterns[kind], sample, re.I))

    def test_environment_collect_binds_round_target_version(self):
        """每一处环境事实采集都必须把本轮参数 TARGET_VERSION 交给 Rust TLS 探针，不能写死或省略。"""

        calls = []
        for path in sorted(SCRIPTS.rglob('*.sh')):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if 'codex_upgrade_arm64_environment_receipt collect' in line:
                    calls.append((path.name, number, line))
        self.assertGreaterEqual(len(calls), 3, calls)
        for name, number, line in calls:
            with self.subTest(script=name, line=number):
                self.assertIn('--rust-tls-codex-version "$TARGET_VERSION"', line)

    def test_target_parameters_generate_vc2_vc4_and_vc5_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = _DriverFixture(root)
            output = root / 'plans'
            output.mkdir()
            environment = {**os.environ, **fixture.env, 'CANDIDATE_DIR': str(fixture.candidate_dir), 'PYTHONDONTWRITEBYTECODE': '1'}
            image_id = 'sha256:' + 'a' * 64
            commands = [
                ['gen_vc2_plans.py', str(output), fixture.new, fixture.inputs, 'b' * 64],
                ['gen_vc4_record_plan.py', image_id, 'build-test', str(root / 'evidence'), str(output / 'vc4.json'), fixture.new, fixture.cand, str(fixture.candidate_dir)],
                ['gen_vc5_plans.py', str(output), fixture.new, fixture.cand, image_id, 'build-test'],
            ]
            for name, *args in commands:
                result = subprocess.run([sys.executable, str(SCRIPTS / name), *args], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            vc2 = json.loads((output / 'action-plan-vc2-approve.json').read_text())['actions'][0]['command']
            self.assertEqual(vc2[vc2.index('--profile-patch-manifest') + 1], str(fixture.data_root / 'tools/official_client_capture/profile_rule_patches_0_156_1.json'))
            for name in ('vc4.json', 'action-plan-vc5-run.json'):
                command = json.loads((output / name).read_text())['actions'][0]['command']
                self.assertEqual(command[command.index('--deployed-version') + 1], '0.156.1')
                self.assertEqual(command[command.index('--runtime-image') + 1], 'sub2apiplus-c01561-candidate@' + image_id)
                self.assertNotIn('0.154.0', json.dumps(command))
            vc4 = json.loads((output / 'vc4.json').read_text())['actions'][0]['command']
            self.assertIn(str(fixture.candidate_dir / 'source/docs/egress/lifecycle/codex-01561-candidate/gate-plan.json'), vc4)
            # 没有 Campaign／批准集合时，seal 计划必须拒绝，不能退回旧的十个 Job。
            result = subprocess.run([sys.executable, str(SCRIPTS / 'gen_vc5_plans.py'), str(output), fixture.new, fixture.cand, image_id, 'build-test', 'attempt-test'],
                                    env={**environment, 'PYTHONPATH': str(REPO_ROOT)}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((output / 'action-plan-vc5-seal-checkpoint.json').exists())

    def test_latest_deployment_uses_timestamp_across_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            earlier = root / 'codex-0999-supervisor-enable-20260922t000000z.json'
            later = root / 'codex-01561-supervisor-enable-20260924t000000z.json'
            for path in (earlier, later):
                _write_json(path, {'tool_files_sha256': 'a' * 64})
            self.assertEqual(driver.latest_deploy_receipt(root)[0], later)
            duplicate = root / 'codex-other-supervisor-enable-20260924t000000z.json'
            _write_json(duplicate, {'tool_files_sha256': 'a' * 64})
            with self.assertRaisesRegex(driver.DriverError, '两份部署收据'):
                driver.latest_deploy_receipt(root)

    def test_candidate_jobs_follow_approved_manifest_and_reject_drift(self):
        from tools.official_client_capture import codex_upgrade as upgrade
        config = load_script('driver_config')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = REPO_ROOT / 'tools/official_client_capture'
            rules = source / 'codex_upgrade_rules_0_154_0.json'
            scenario = json.loads((source / 'codex_upgrade_scenarios_0_154_0.json').read_text())
            # 改名说明集合来自批准输入；保留真实场景校验、模板展开与规则闭集校验。
            for row in scenario['capture_jobs']:
                if row['id'] == 'candidate-compact-direct':
                    row['id'] = 'candidate-approved-renamed'
            path = root / 'scenario.json'
            _write_json(path, scenario)
            shutil.copy2(rules, root / 'rules.json')
            binding = {'path': path.name, 'sha256': upgrade.file_sha256(path)}
            manifest = {'campaign_id': 'fixture', 'baseline_version': '0.151.0', 'target_version': '0.154.0',
                        'campaign_mode': 'formal', 'campaign_purpose': 'production_replacement', 'suite': 'full', 'target_sha256': 'a' * 64,
                        'official_identity': {'package': {'asset_sha256': 'a' * 64, 'code_mode_host_sha256': 'b' * 64}},
                        'inputs': {'baseline_rules': {'path': 'rules.json'}, 'discovery_scenarios': binding, 'target_discovery_scenarios': binding}}
            cfg = {key: str(root / key) for key in ('baseline_source', 'target_source', 'baseline_evidence', 'target_package', 'capture_root')}
            cfg.update({key: key for key in ('capture_container', 'service_container', 'keeper_container', 'postgres_container', 'redis_container')})
            cfg.update({key: '/opt/test/bin/' + key for key in ('capture_codex_bin', 'relay_codex_bin', 'capture_code_mode_host_bin', 'relay_code_mode_host_bin')})
            cfg.update(runtime_image='capture@sha256:' + 'b' * 64, model='gpt-5.4', lite_model='gpt-5.6-luna', codex_account_id=91, api_key_id=92)
            manifest['configuration'] = cfg
            classification = {'status': 'complete', 'scenario_manifest': binding,
                              'target_rule_manifest': {'path': 'rules.json', 'sha256': upgrade.file_sha256(root / 'rules.json')}}
            with mock.patch.object(upgrade, 'load_campaign_manifest', return_value=manifest), mock.patch.object(upgrade, '_load_stage_result', return_value=classification):
                expected = sorted(row['id'] for row in scenario['capture_jobs'] if row['phase'] == 'candidate' and 'full' in row['suites'])
                self.assertEqual(config.candidate_job_ids(root, 'candidate'), expected)
                path.write_text(path.read_text() + '\n')
                with self.assertRaisesRegex(upgrade.ConfigurationError, '目标场景清单摘要不一致'):
                    config.candidate_job_ids(root, 'candidate')


class DynamicDriverGateTests(unittest.TestCase):
    """门禁函数不打桩：只替换 Campaign 读取，真实进程、摘要、计划与统一收据 producer 都执行。"""

    def setUp(self):
        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
        self.artifacts = artifacts
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base = self.root / 'candidate'
        for name in ('source', 'gate-tree'):
            (self.base / name / 'backend').mkdir(parents=True)
            (self.base / name / 'backend/fixture.txt').write_text('真实门禁输入\n')
        self.requirements = artifacts.build_gate_requirements(campaign_id='fixture', target_version='0.156.1', joint_manifest_sha256='a' * 64,
            affected_rule_ids=['SPEC-HDR-005', 'SPEC-EP-007'], inherited_rule_ids=['SPEC-BODY-001'], migration_manifest={'path':'rules/migration.json', 'sha256':'b' * 64})
        self.mapping = {'schema_version':artifacts.GATE_MAPPING_SCHEMA, 'requirements_sha256':self.requirements['requirements_sha256'],
            'gates':[{'gate_id':row['gate_id'], 'test_id':'test-' + str(index), 'working_directory':'backend',
                      'command':[sys.executable, '-c', f'print("门禁 {index} 通过")'], 'requirement_sha256':artifacts.digest(row)}
                     for index, row in enumerate(self.requirements['requirements'])]}
        self.lifecycle = Path('docs/egress/lifecycle/fixture')
        self.update_plan()
        self.manifest = {'campaign_id':'fixture', 'campaign_purpose':'production_replacement', 'baseline_version':'0.154.0', 'target_version':'0.156.1'}
        self.gates = load_script('implementation_gates')
        patches = [mock.patch.dict(os.environ, {'D':str(self.root), 'B':str(self.base), 'NEW':'fixture', 'C':'c' * 40, 'CAND':'candidate', 'UP':'upgrade', 'LIFECYCLE_DIR':str(self.lifecycle)}),
                   mock.patch.object(self.gates.upgrade, 'load_campaign_manifest', return_value=self.manifest),
                   mock.patch.object(self.gates.upgrade, '_load_stage_result', return_value={}),
                   mock.patch.object(self.gates.upgrade, '_load_vc3_gate_requirements', return_value=(None, None, self.requirements))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir(mode=0o700)

    def update_plan(self):
        import hashlib
        path = self.base / 'source' / self.lifecycle / 'gate-mapping.json'
        _write_json(path, self.mapping)
        self.plan = self.artifacts.build_gate_plan(self.requirements, self.mapping, mapping_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        _write_json(path.with_name('gate-plan.json'), self.plan)

    def test_multiple_affected_gates_execute_and_finalize_real_receipt(self):
        from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
        self.gates.run_gates(self.evidence)
        tree = self.gates.upgrade._directory_tree_digest(self.base / 'gate-tree')
        (self.evidence / 'logs/check-egress-spec.log').write_text('## make check-egress-spec\nexit_code=0\n')
        self.gates.make_facts(self.evidence, tree)
        original = (self.evidence / 'facts.json').read_bytes()
        self.gates.make_facts(self.evidence, tree)
        self.assertEqual((self.evidence / 'facts.json').read_bytes(), original)
        facts = json.loads((self.evidence / 'facts.json').read_text())
        self.assertEqual({row['gate_id'] for row in facts['assertions']['gates']}, {row['gate_id'] for row in self.requirements['requirements']} | {'check-egress-spec'})
        self.assertEqual(sum(row['kind'] == 'affected' for row in facts['assertions']['gates']), 2)
        receipts.finalize(self.evidence, 'facts.json', 'receipt.json')
        receipts.replay(self.evidence, 'receipt.json')

    def test_failed_gate_never_produces_completion(self):
        self.mapping['gates'][0]['command'] = [sys.executable, '-c', 'raise SystemExit(9)']
        self.update_plan()
        with self.assertRaisesRegex(ValueError, '门禁失败'):
            self.gates.run_gates(self.evidence)
        self.assertNotIn('GATES_DONE', (self.evidence / 'logs/implementation.log').read_text())

    def test_mapping_and_log_tampering_are_rejected(self):
        self.gates.run_gates(self.evidence)
        tree = self.gates.upgrade._directory_tree_digest(self.base / 'gate-tree')
        log = self.evidence / 'logs/implementation.log'
        original = log.read_text()
        log.write_text(original.replace('exit_code=0', 'exit_code=9', 1))
        with self.assertRaisesRegex(ValueError, '门禁未成功'):
            self.gates.make_facts(self.evidence, tree)
        self.mapping['gates'][0]['command'] = ['true']
        _write_json(self.base / 'source' / self.lifecycle / 'gate-mapping.json', self.mapping)
        with self.assertRaisesRegex(ValueError, '计划与本轮门禁映射不一致'):
            self.gates.load_plan()
        self.mapping['gates'].pop()
        with self.assertRaisesRegex(self.artifacts.VCArtifactError, '未精确覆盖'):
            self.update_plan()

    def test_catalog_assembly_uses_inventory_and_preserves_existing_blobs(self):
        catalog = load_script('catalog_chain')
        source = self.root / 'catalog'
        source.mkdir()
        blob = 'catalogdata/runtime/profiles/0.156.1/' + 'a' * 64 + '.json'
        paths = sorted(catalog.MUTABLE | {blob})
        rows = []
        for relative in paths:
            path = source / relative
            _write_json(path, {'path':relative})
            rows.append({'path':relative, 'size':path.stat().st_size, 'sha256':self.gates.upgrade.file_sha256(path)})
        receipt = {'inventory':rows, 'inventory_sha256':self.gates.upgrade._fingerprint(rows), 'campaign_id':'fixture', 'target_version':'0.156.1',
                   'post_promotion_gate_requirements_sha256':self.requirements['requirements_sha256']}
        _write_json(source / 'catalog-stage-receipt.json', receipt)
        requirements = self.root / 'requirements.json'
        _write_json(requirements, self.requirements)
        mapping = self.base / 'source' / self.lifecycle / 'gate-mapping.json'
        repository = self.root / 'repository'
        catalog.assemble(source, repository, self.lifecycle, requirements, mapping)
        before = {relative:(repository / 'backend/internal/officialegress' / relative).read_bytes() for relative in paths}
        catalog.assemble(source, repository, self.lifecycle, requirements, mapping)
        self.assertEqual(before, {relative:(repository / 'backend/internal/officialegress' / relative).read_bytes() for relative in paths})
        (repository / 'backend/internal/officialegress' / blob).write_text('非法覆盖')
        with self.assertRaisesRegex(ValueError, '不可变 Catalog blob'):
            catalog.assemble(source, repository, self.lifecycle, requirements, mapping)


class LocalVc4HeartbeatTests(unittest.TestCase):
    """修好接着跑第 20 项：本机门禁一开始就向采集主机发上传心跳，门禁与全量回归都完成才上传。

    此前心跳要到 local-upload.sh 才开始，本机门禁一旦超过 300 秒，vc4-all.sh 就因"上传心跳一直缺失"退出。
    这里用假 ssh 与假门禁脚本记录事件顺序（心跳间隔调成 1 秒），不连任何主机。
    """

    def test_heartbeat_starts_before_gates_and_upload_waits_for_both(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local"
            local.mkdir()
            shutil.copy(SCRIPTS / "local" / "local-vc4.sh", local / "local-vc4.sh")
            events = root / "events.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            scripts = {
                fake_bin / "ssh": f'#!/bin/bash\necho "ssh ${{@: -1}}" >> "{events}"\n',
                local / "local-gate.sh": (
                    f'#!/bin/bash\necho "gate-start" >> "{events}"\n'
                    'rm -rf "$5/local-gates" "$5/impl-logs"; mkdir -p "$5/local-gates" "$5/impl-logs/cross-check"\n'
                    f'sleep 3\necho "gate-end" >> "{events}"\n'
                ),
                local / "local-full-regression.sh": (
                    f'#!/bin/bash\n[ -d "$2/impl-logs/cross-check" ] && echo "regression-start dir-ready" >> "{events}" '
                    f'|| echo "regression-start dir-missing" >> "{events}"\nsleep 1\necho "regression-end" >> "{events}"\n'
                ),
                local / "local-upload.sh": f'#!/bin/bash\necho "upload" >> "{events}"\n',
            }
            for path, text in scripts.items():
                path.write_text(text, encoding="utf-8")
                path.chmod(0o700)
            environment = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", LOCAL_VC4_HEARTBEAT_SECONDS="1")
            result = subprocess.run(
                ["bash", str(local / "local-vc4.sh"), "r1", "c" * 40, "d" * 40, "receipt.json", str(root / "out"), "/root/vc-rounds/t"],
                env=environment, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("LOCAL_VC4_DONE", result.stdout)
            lines = events.read_text(encoding="utf-8").splitlines()
            kinds = [line.split()[0] for line in lines]
            # 第一件事就是建远端目录、清 READY 并 touch 心跳，早于门禁开始。
            self.assertEqual(kinds[0], "ssh")
            self.assertIn("HEARTBEAT", lines[0])
            self.assertIn("rm -f", lines[0])
            gate_start, gate_end = kinds.index("gate-start"), kinds.index("gate-end")
            # 门禁进行中持续有心跳。
            self.assertTrue(any(kind == "ssh" for kind in kinds[gate_start:gate_end]), lines)
            # 全量回归在门禁清空并重建输出目录之后才启动（否则产物会被清掉）。
            self.assertIn("regression-start dir-ready", lines)
            # 上传在门禁与全量回归都结束之后；交接后包装自身的心跳已停（至多一条恰在停止时发出的）。
            upload = kinds.index("upload")
            self.assertGreater(upload, gate_end)
            self.assertGreater(upload, kinds.index("regression-end"))
            self.assertLessEqual(sum(1 for kind in kinds[upload + 1:] if kind == "ssh"), 1)

    def test_rejects_unsafe_remote_root(self) -> None:
        for runroot in ("/", "/root", "relative/path", "/root/../etc", "/root/x;rm"):
            with self.subTest(runroot=runroot):
                # PATH 指向不存在的目录：校验失败必须发生在任何 ssh 之前（bash 用绝对路径启动）。
                result = subprocess.run(
                    [shutil.which("bash") or "/bin/bash", str(SCRIPTS / "local" / "local-vc4.sh"), "r1", "c", "d", "r.json", "/tmp/out", runroot],
                    capture_output=True, text=True, timeout=30, env=dict(os.environ, PATH="/nonexistent"),
                )
                self.assertEqual(result.returncode, 3)


class PreA3OrderingTests(unittest.TestCase):
    """修好接着跑第 18、19 项：pre-A3 认证在 stage1 建账本之前完成；stage1 建账本前按复用同一口径核验本轮认证。"""

    def test_stage1_checks_certification_before_ledger_and_pre_a3_never_opens_a_ledger(self) -> None:
        stage1 = (SCRIPTS / "stage1.sh").read_text(encoding="utf-8")
        check = stage1.index("codex_upgrade_pre_a3_certification find-reusable --certification")
        self.assertLess(check, stage1.index("codex_upgrade_zero_request_smoke"))
        self.assertLess(check, stage1.index("codex_upgrade_timing_ledger create"))
        pre_a3 = (SCRIPTS / "pre-a3.sh").read_text(encoding="utf-8")
        self.assertIn("find-reusable --search-root", pre_a3)
        self.assertIn("find-reusable --certification", pre_a3)
        self.assertNotIn("codex_upgrade_timing_ledger", pre_a3)
        # 复用或新跑之后按 stage1 同一口径复核本轮坐标。
        # E2-03：新跑交给 lib.sh 的 issue_pre_a3_certification（plan → 驱动随附执行器 run-commands → issue），签发在复核之前；
        # stage2 兜底用同一个函数，两处都不再在一个进程里串行跑全部场景。
        self.assertLess(pre_a3.index("issue_pre_a3_certification"), pre_a3.rindex("find-reusable --certification"))
        lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
        body = lib[lib.index("issue_pre_a3_certification() {"):]
        body = body[: body.index("\n}\n")]
        plan = body.index("pre_a3_certification plan --staging-root \"$D/staging/pre-a3-certification-$STAMP\"")
        execute = body.index('python3 "$DRV/unit_executor.py" run-commands --manifest "$units" --out-dir "$root/executor"')
        issue = body.index('pre_a3_certification issue --staging-root "$root" --executor-summary "$root/executor/summary.json"')
        self.assertTrue(plan < execute < issue)
        self.assertIn('--output "$PRE_A3_CERTIFICATION"', body[issue:])
        stage2 = (SCRIPTS / "stage2.sh").read_text(encoding="utf-8")
        self.assertIn('[ -f "$PRE_A3_CERTIFICATION" ] || issue_pre_a3_certification', stage2)
        self.assertLess(stage2.index("issue_pre_a3_certification"), stage2.index("codex_upgrade_pre_a3_certification record-reuse"))
        for script in ("pre-a3.sh", "stage2.sh", "lib.sh"):
            self.assertNotIn("pre_a3_certification run ", (SCRIPTS / script).read_text(encoding="utf-8"), script)

    def test_reuse_receipt_recorded_before_ledger_and_bound_by_release_certification(self) -> None:
        """修好接着跑第 19 项：跨部署复用的复用收据在 pre-a3.sh 复核后登记、stage1 建账本前补齐，stage2 发布认证绑定它。"""

        pre_a3 = (SCRIPTS / "pre-a3.sh").read_text(encoding="utf-8")
        record = "codex_upgrade_pre_a3_certification record-reuse --certification"
        self.assertIn(record, pre_a3)
        self.assertGreater(pre_a3.index(record), pre_a3.rindex("find-reusable --certification"))
        self.assertIn('--receipt-root "$(dirname "$PRE_A3_CERTIFICATION")"', pre_a3)
        stage1 = (SCRIPTS / "stage1.sh").read_text(encoding="utf-8")
        self.assertGreater(stage1.index(record), stage1.index("find-reusable --certification"))
        self.assertLess(stage1.index(record), stage1.index("codex_upgrade_zero_request_smoke"))
        self.assertLess(stage1.index(record), stage1.index("codex_upgrade_timing_ledger create"))
        stage2 = (SCRIPTS / "stage2.sh").read_text(encoding="utf-8")
        self.assertLess(stage2.index(record), stage2.index("certify_release issue"))
        issue = stage2[stage2.index("certify_release issue"):]
        issue = issue[: issue.index("\n")]
        self.assertIn('--pre-a3-certification "$PRE_A3_CERTIFICATION"', issue)
        self.assertIn('${PRE_A3_REUSE:+--pre-a3-reuse-receipt "$PRE_A3_REUSE"}', issue)
        # 复用收据的登记与旧坐标、账本无关：pre-a3.sh 仍不碰计时账本。
        self.assertNotIn("codex_upgrade_timing_ledger", pre_a3)


# 受管环境收据 CLI 的替身：只按 --evidence-root／--output 写出一个空 JSON，供门禁脚本走完 before／after 收据。
_ENVIRONMENT_RECEIPT_STUB = '''import sys
from pathlib import Path

arguments = sys.argv[1:]
root = Path(arguments[arguments.index("--evidence-root") + 1])
output = root / arguments[arguments.index("--output") + 1]
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("{}\\n", encoding="utf-8")
print("环境收据替身", arguments[0], output.name)
'''


# 门禁 make test 的替身里运行的探针：按测试进程同一方式（物理 cwd 拼路径）导入测试树模块与标准库，记录解释器实际
# 查找的 .pyc 路径是否已在缓存里，以及收到的环境。
_CACHE_PROBE = """import json
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
import probe_pkg.probe as probe

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"env": dict(os.environ), "prefix": sys.pycache_prefix, "cached": probe.__cached__,
               "cached_exists": os.path.isfile(probe.__cached__), "stdlib_cached": json.__cached__,
               "stdlib_cached_exists": os.path.isfile(json.__cached__)}, handle)
"""


class GateBytecodeEnvironmentTests(unittest.TestCase):
    """修好接着跑第 67 项：ARM64 上跑 make test 的门禁只读使用预编译的树外字节码缓存。

    lib.sh 全局导出 PYTHONDONTWRITEBYTECODE=1。2026-09-28 0.157 VC-5 accept 的目标平台门禁导出了一个空的
    PYTHONPYCACHEPREFIX：解释器改到前缀下查找全部 .pyc（含标准库自带的）而全部落空，每个子进程都从源码重编，ARM64
    监督器 CLI 启动 584 毫秒，候选树监督器 4 条计时用例连续两次确定性失败；不设前缀也要每次编译受管模块（约 323
    毫秒），心跳间隔用例只剩约 20 毫秒余量（ARM64 16 次失败 2 次）。修法：make test 前用 bytecode_cache.py 把
    标准库与测试树 tools 预编译进重建的缓存目录，再只读使用（约 187 毫秒，15 次零失败）。
    """

    # 各门禁脚本给缓存目录赋值的那一行（逐字）：目标平台门禁默认 $RUNROOT/pycache-target-platform，VC-0 预跑经第 4 个
    # 参数改放预跑目录（vc0-gate-target.sh），VC-5 调用只传三个参数，行为不变。
    GATE_SCRIPTS = {
        "vc5-gate-target.sh": 'PYC="${4:-$RUNROOT/pycache-target-platform}"',
        "gates.sh": 'PYC="$RUNROOT/pycache-full-regression"',
    }
    HELPER = SCRIPTS / "bytecode_cache.py"

    def _run_helper(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONPYCACHEPREFIX"}}
        result = subprocess.run([sys.executable, str(self.HELPER), *arguments], capture_output=True, text=True,
                                env={**environment, "PYTHONDONTWRITEBYTECODE": "1"})
        lines = result.stdout.strip().splitlines()
        return result, (json.loads(lines[-1]) if lines else {})

    def test_helper_prebuilds_readonly_cache_outside_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "tree" / "tools"
            (source / "pkg").mkdir(parents=True)
            (source / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (source / "pkg" / "mod.py").write_text("VALUE = 67\n", encoding="utf-8")
            prefix = root / "pycache-helper"
            prefix.mkdir()
            (prefix / "stale-marker").write_text("上一轮的内容", encoding="utf-8")
            result, summary = self._run_helper(str(prefix), str(source))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(summary["status"], "ready")
            self.assertTrue(result.stdout.strip().splitlines()[-1].startswith('{"status": "ready"'))
            self.assertFalse((prefix / "stale-marker").exists(), "缓存目录必须重建，不带上一轮内容")
            self.assertGreater(summary["stdlib_pyc"], 100)
            self.assertEqual(summary["sources_pyc"], {str(source): 2})
            self.assertEqual(list((root / "tree").rglob("__pycache__")), [])
            before = sorted(str(path) for path in prefix.rglob("*"))
            # 解释器以同一前缀、禁写方式导入：查找的 .pyc 路径正是预编译产物，缓存目录一个文件都不增加（只读使用）。
            probe = subprocess.run(
                [sys.executable, "-c", "import json, os, sys; sys.path.insert(0, sys.argv[1]); import pkg.mod as m; "
                 "print(json.dumps([m.__cached__, os.path.isfile(m.__cached__), json.__cached__, os.path.isfile(json.__cached__)]))",
                 str(source)],
                capture_output=True, text=True,
                env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "PYTHONPYCACHEPREFIX": str(prefix),
                     "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(probe.returncode, 0, probe.stderr)
            cached, cached_exists, stdlib_cached, stdlib_exists = json.loads(probe.stdout)
            self.assertTrue(cached.startswith(str(prefix)) and cached_exists, cached)
            self.assertTrue(stdlib_cached.startswith(str(prefix)) and stdlib_exists, stdlib_cached)
            self.assertEqual(sorted(str(path) for path in prefix.rglob("*")), before)
            self.assertEqual(list((root / "tree").rglob("__pycache__")), [])

    def test_helper_compiles_sources_by_content_hash_so_same_second_edits_run_new_code(self) -> None:
        """E2-02：源码目录按内容摘要失效（PEP 552 checked-hash，头部标志位 3），标准库仍按时间戳（标志位 0）；
        同一秒内对源码做长度不变的修改，禁写的子进程从源码编译、执行新代码（按时间戳校验会执行旧字节码）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "tree" / "tools"
            (source / "pkg").mkdir(parents=True)
            (source / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            module = source / "pkg" / "mod.py"
            module.write_text('VALUE = "AAAA"\n', encoding="utf-8")
            prefix = root / "pycache-hash"
            result, summary = self._run_helper(str(prefix), str(source))
            self.assertEqual((result.returncode, summary["status"]), (0, "ready"), result.stdout + result.stderr)

            def flags(path: Path) -> int:
                return int.from_bytes(path.read_bytes()[4:8], "little")

            cached = prefix / module.parent.relative_to(module.anchor) / f"mod.{sys.implementation.cache_tag}.pyc"
            self.assertEqual(flags(cached), 3, "源码目录必须按内容摘要编译并在加载时核对")
            stdlib_json = Path(json.__file__)
            stdlib_cached = prefix / stdlib_json.parent.relative_to(stdlib_json.anchor) / f"__init__.{sys.implementation.cache_tag}.pyc"
            self.assertEqual(flags(stdlib_cached), 0, "标准库仍按时间戳校验")
            module.write_text(module.read_text(encoding="utf-8").replace("AAAA", "BBBB"), encoding="utf-8")
            probe = subprocess.run(
                [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import pkg.mod as m; print(m.VALUE)", str(source)],
                capture_output=True, text=True,
                env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "PYTHONPYCACHEPREFIX": str(prefix),
                     "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual((probe.returncode, probe.stdout.strip()), (0, "BBBB"), probe.stderr)
            self.assertEqual(flags(cached), 3, "禁写：缓存保持原样，不被改写")

    def test_helper_refuses_unsafe_prefix_without_deleting_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "tree" / "tools"
            source.mkdir(parents=True)
            (source / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
            outer = root / "pycache-outer"
            inner_source = outer / "tools"
            inner_source.mkdir(parents=True)
            (inner_source / "keep.py").write_text("KEEP = 2\n", encoding="utf-8")
            cases = {
                "相对路径": ("pycache-relative", source),
                "名字不含 pycache": (str(root / "cache-dir"), source),
                "缓存目录在源码树内": (str(source / "pycache-inside"), source),
                "源码树在缓存目录内": (str(outer), inner_source),
            }
            for label, (prefix, target) in cases.items():
                with self.subTest(label):
                    result, summary = self._run_helper(prefix, str(target))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(summary["status"], "failed")
            self.assertTrue((source / "keep.py").is_file())
            self.assertTrue((inner_source / "keep.py").is_file(), "拒绝时不得清空包含源码树的目录")
            self.assertFalse((source / "pycache-inside").exists())
            self.assertFalse((root / "cache-dir").exists())

    def test_isolated_gate_run_unsets_the_production_identity_memo(self) -> None:
        """E2-02：门禁跑测试树，身份记忆化交给统一调度执行器在记录目录里新建，不混用生产缓存。"""

        lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
        body = lib[lib.index("isolated_run() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("unset CODEX_UPGRADE_IDENTITY_MEMO", body)
        # 自己调 make test 的两个门禁（目标平台门禁、gates.sh 的全量回归）同样不能带着 lib.sh 导出的生产目录进去。
        for name in ("vc5-gate-target.sh", "gates.sh"):
            make = [line for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines() if "exec make test" in line]
            self.assertEqual(len(make), 1, name)
            self.assertIn("env -u CODEX_UPGRADE_IDENTITY_MEMO unshare -m", make[0], name)

    def test_gate_scripts_prepare_cache_then_export_before_make_test(self) -> None:
        lib_code = [line for line in (SCRIPTS / "lib.sh").read_text(encoding="utf-8").splitlines()
                    if line.startswith("export ")]
        self.assertTrue(any("PYTHONDONTWRITEBYTECODE=1" in line.split() for line in lib_code),
                        "lib.sh 必须全局禁写字节码（门禁不写 __pycache__ 靠它）")
        for name, assignment in self.GATE_SCRIPTS.items():
            code = [line.strip() for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#")]
            assign = [index for index, line in enumerate(code) if line == assignment]
            prepare = [index for index, line in enumerate(code)
                       if line.startswith('env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYC" "$T/tools" |')]
            export = [index for index, line in enumerate(code) if line == 'export PYTHONPYCACHEPREFIX="$PYC"']
            other = [line for line in code if re.search(r"PYTHONPYCACHEPREFIX\s*=", line) and line != 'export PYTHONPYCACHEPREFIX="$PYC"']
            make = [index for index, line in enumerate(code) if "exec make test" in line]
            self.assertEqual((len(assign), len(prepare), len(export), len(make)), (1, 1, 1, 1), name)
            self.assertEqual(other, [], name)
            self.assertTrue(assign[0] < prepare[0] < export[0] < make[0], f"{name}：必须先建缓存、再导出前缀、最后 make test")

    def _gate_fixture(self, root: Path) -> tuple[_DriverFixture, Path, Path, Path, dict[str, str]]:
        fixture = _DriverFixture(root)
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "vc5-gate-target.sh", "bytecode_cache.py"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        package = fixture.data_root / "tools" / "official_client_capture"
        (fixture.data_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "codex_upgrade_arm64_environment_receipt.py").write_text(_ENVIRONMENT_RECEIPT_STUB, encoding="utf-8")
        tree = root / "test-tree"
        (tree / "tools" / "probe_pkg").mkdir(parents=True)
        (tree / "tools" / "probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
        (tree / "tools" / "probe_pkg" / "probe.py").write_text("VALUE = 67\n", encoding="utf-8")
        probe = root / "cache_probe.py"
        probe.write_text(_CACHE_PROBE, encoding="utf-8")
        record = root / "make-test-environment.json"
        # unshare 垫片代替“私有挂载命名空间 + make test”：在测试树里跑探针，记录环境与缓存命中情况。
        bin_dir = root / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "unshare"
        shim.write_text(f"#!/bin/bash\npython3 '{probe}' '{record}'\necho make-test-stub-ok\n", encoding="utf-8")
        shim.chmod(0o700)
        env = {**fixture.env, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
               "PYTHONPYCACHEPREFIX": str(root / "inherited-pycache-prefix"),
               "CODEX_UPGRADE_IDENTITY_MEMO": str(root / "inherited-identity-memo")}
        return fixture, drv, tree, record, env

    def test_target_gate_make_test_reads_prebuilt_cache_readonly(self) -> None:
        """脚本级：调用方带着别的前缀进入目标平台门禁；make test 看到的前缀是本次预编译的缓存，测试树模块与标准库
        的 .pyc 都已在缓存里，禁写照旧，测试树与数据根不留 __pycache__；生产的身份记忆化目录不带进 make test。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, drv, tree, record, env = self._gate_fixture(root)
            gate = root / "gate"
            result = _run(drv / "vc5-gate-target.sh", "20260928T000000Z-0123456789abcdef", str(gate), str(tree), env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("GATE_TARGET_DONE rc=0", result.stdout)
            self.assertIn('{"status": "ready"', result.stdout)
            cache = str(fixture.runroot / "pycache-target-platform")
            seen = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(seen["env"].get("PYTHONPYCACHEPREFIX"), cache)
            self.assertNotIn("CODEX_UPGRADE_IDENTITY_MEMO", seen["env"])
            self.assertEqual(seen["prefix"], cache)
            self.assertEqual(seen["env"].get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertTrue(seen["cached"].startswith(cache) and seen["cached_exists"], seen["cached"])
            self.assertTrue(seen["stdlib_cached"].startswith(cache) and seen["stdlib_cached_exists"], seen["stdlib_cached"])
            self.assertEqual(json.loads((gate / "logs" / "target-platform.gate.json").read_text(encoding="utf-8"))["exit_code"], 0)
            self.assertEqual(sorted(path.name for path in (gate / "environment").iterdir()), [
                "20260928T000000Z-0123456789abcdef-after-facts.json", "20260928T000000Z-0123456789abcdef-after.json",
                "20260928T000000Z-0123456789abcdef-before-facts.json", "20260928T000000Z-0123456789abcdef-before.json",
            ])
            self.assertEqual(list(tree.rglob("__pycache__")), [])
            self.assertEqual(list(fixture.data_root.rglob("__pycache__")), [])
            self.assertFalse((root / "inherited-pycache-prefix").exists())

    def test_target_gate_stops_before_make_test_when_cache_cannot_be_built(self) -> None:
        """测试树里有编译不了的文件：缓存建不成即停，不进入 make test、不写门禁结果（失败关闭）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, drv, tree, record, env = self._gate_fixture(root)
            (tree / "tools" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            gate = root / "gate"
            result = _run(drv / "vc5-gate-target.sh", "20260928T000000Z-0123456789abcdef", str(gate), str(tree), env=env, cwd=root)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('{"status": "failed"', result.stdout)
            self.assertFalse(record.exists(), "缓存建不成时不得进入 make test")
            self.assertFalse((gate / "logs" / "target-platform.gate.json").exists())
            self.assertEqual(list(tree.rglob("__pycache__")), [])


def _git(cwd: Path, *arguments: str) -> str:
    """测试用 git：固定提交身份、关掉签名与钩子，不读开发机的个人配置差异。"""

    result = subprocess.run(
        ["git", "-c", "user.name=vc0-test", "-c", "user.email=vc0-test@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} 失败：{result.stdout}{result.stderr}")
    return result.stdout.strip()


# 历史测试树第一个提交里的文件：前端 lockfile（VC-0 预跑按它核对 node_modules 来源）与缓存探针要导入的 tools 包。
_HISTORY_FILES = {
    ".gitignore": "node_modules/\n",
    "README.md": "history\n",
    "frontend/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    "tools/probe_pkg/__init__.py": "",
    "tools/probe_pkg/probe.py": "VALUE = 67\n",
}


def _history_repo(path: Path, *, commits: int) -> None:
    """用 git fast-import 造一条 main 线性历史（门禁测试树要求完整历史，提交数 >10000），检出到最新提交。"""

    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    chunks: list[bytes] = []
    for index in range(1, commits + 1):
        message = f"c{index}".encode()
        chunks.append(b"commit refs/heads/main\n" + f"mark :{index}\n".encode()
                      + f"committer vc0-test <vc0-test@example.invalid> {1700000000 + index} +0000\n".encode()
                      + f"data {len(message)}\n".encode() + message + b"\n")
        if index > 1:
            chunks.append(f"from :{index - 1}\n".encode())
        else:
            for name, content in _HISTORY_FILES.items():
                data = content.encode()
                chunks.append(f"M 100644 inline {name}\ndata {len(data)}\n".encode() + data + b"\n")
        chunks.append(b"\n")
    subprocess.run(["git", "fast-import", "--quiet"], cwd=str(path), input=b"".join(chunks), check=True)
    _git(path, "reset", "-q", "--hard", "main")


# 字节码预编译工具的替身：记录调用与它收到的前缀环境，按开关成功（清空重建前缀）或失败。
_FAKE_BYTECODE_HELPER = """import json, os, shutil, sys
from pathlib import Path
prefix = Path(sys.argv[1])
with open(os.environ["FAKE_BYTECODE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "prefix_env": os.environ.get("PYTHONPYCACHEPREFIX")}) + "\\n")
if os.environ.get("FAKE_BYTECODE_FAIL"):
    # 失败原因放在一长串路径之后：调用方截断输出就看不到它。
    print(json.dumps({"status": "failed", "leaked_pycache": ["/data/tools/" + "x" * 400 + "/__pycache__"],
                      "error": "替身：预编译失败"}, ensure_ascii=False))
    sys.exit(1)
if prefix.exists():
    shutil.rmtree(prefix)
prefix.mkdir(parents=True)
print(json.dumps({"status": "ready", "prefix": str(prefix)}))
"""


class ManagedSharedCacheTests(unittest.TestCase):
    """E2-02：lib.sh 为入口各子命令导出共享缓存；prepare_managed_bytecode 按数据根受管树内容沿用或重建。"""

    PROBE = 'echo "PREFIX=${PYTHONPYCACHEPREFIX:-}"; echo "MEMO=${CODEX_UPGRADE_IDENTITY_MEMO:-}"'

    def _driver(self, root: Path) -> tuple[_DriverFixture, Path]:
        fixture = _DriverFixture(root)
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        (drv / "bytecode_cache.py").write_text(_FAKE_BYTECODE_HELPER, encoding="utf-8")
        (fixture.data_root / "tools" / "official_client_capture" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        return fixture, drv

    def _bash(self, fixture: _DriverFixture, drv: Path, script: str, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        calls = fixture.root / "bytecode-calls.jsonl"
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPYCACHEPREFIX", "CODEX_UPGRADE_IDENTITY_MEMO"}}
        environment.update({**fixture.env, "FAKE_BYTECODE_CALLS": str(calls), **extra})
        # lib.sh 只能被脚本 source（它检查 BASH_SOURCE[1]），所以把探针写成驱动目录里的脚本再运行。
        probe = drv / "probe.sh"
        probe.write_text(f'#!/bin/bash\nset -Eeuo pipefail\nsource "$(dirname "${{BASH_SOURCE[0]}}")/lib.sh"\n{script}\n', encoding="utf-8")
        completed = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=environment, timeout=120)
        lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return completed, [json.loads(line) for line in lines]

    def test_prepare_rebuilds_only_when_the_managed_tree_changes_and_exports_the_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, drv = self._driver(root)
            prefix, memo = root / "pycache-managed", root / "identity-memo"
            completed, calls = self._bash(fixture, drv, self.PROBE)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("PREFIX=\n", completed.stdout, "缓存还没建时不导出前缀")
            self.assertIn(f"MEMO={memo}\n", completed.stdout, "身份记忆化目录在数据根之外，总是导出")
            completed, calls = self._bash(fixture, drv, f"prepare_managed_bytecode; {self.PROBE}")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual([call["argv"] for call in calls], [[str(prefix), str(fixture.data_root / "tools")]])
            self.assertIsNone(calls[0]["prefix_env"], "预编译进程自己不带旧前缀")
            self.assertIn(f"PREFIX={prefix}\n", completed.stdout)
            self.assertTrue((prefix / ".tools-digest").is_file())
            completed, calls = self._bash(fixture, drv, f"prepare_managed_bytecode; {self.PROBE}")
            self.assertEqual(len(calls), 1, "数据根受管树没变：沿用，不重建")
            self.assertIn("沿用", completed.stdout)
            self.assertIn(f"PREFIX={prefix}\n", completed.stdout)
            completed, _calls = self._bash(fixture, drv, self.PROBE)
            self.assertIn(f"PREFIX={prefix}\n", completed.stdout, "建好之后 source lib.sh 的脚本直接导出前缀")
            (fixture.data_root / "tools" / "official_client_capture" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
            completed, calls = self._bash(fixture, drv, f"prepare_managed_bytecode; {self.PROBE}")
            self.assertEqual(len(calls), 2, "数据根受管树变了：重建")
            self.assertEqual(sorted(str(path) for path in fixture.data_root.rglob("__pycache__")), [], "缓存在数据根之外")

    def test_prepare_failure_warns_and_leaves_no_prefix_without_stopping_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, drv = self._driver(root)
            completed, calls = self._bash(fixture, drv, f"prepare_managed_bytecode; {self.PROBE}; echo 接着跑", FAKE_BYTECODE_FAIL="1")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(calls), 1)
            self.assertIn("PREFIX=\n", completed.stdout)
            self.assertIn("接着跑", completed.stdout)
            self.assertIn("字节码共享层重建失败", completed.stderr)
            self.assertIn("替身：预编译失败", completed.stderr, "失败原因原样输出、不截断")

    def test_entry_preflight_prepares_the_shared_layer_first_outside_any_pipeline(self) -> None:
        code = [line.strip() for line in (SCRIPTS / "entry-preflight.sh").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        prepare = [index for index, line in enumerate(code) if line.startswith("prepare_managed_bytecode")]
        checks = [index for index, line in enumerate(code) if line.startswith("check ")]
        self.assertEqual(len(prepare), 1)
        self.assertNotIn("|", code[prepare[0]], "要在本 shell 里导出前缀，不能放进管道")
        self.assertTrue(checks and prepare[0] < min(checks), "字节码共享层在各检查项之前准备")

    def test_entry_scripts_reuse_the_shared_layer_right_after_the_cheap_checks(self) -> None:
        """便宜检查在子进程里准备共享层，pre-A3（认证进程内跑真实链）与 stage1 紧接着在自己的 shell 里导出前缀。"""

        for name in ("pre-a3.sh", "stage1.sh"):
            code = [line.strip() for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
            preflight = [index for index, line in enumerate(code) if line == 'bash "$DRV/entry-preflight.sh"']
            self.assertEqual(len(preflight), 1, name)
            self.assertTrue(code[preflight[0] + 1].startswith("use_managed_bytecode"), name)


class TestTreeAndVc0PreflightTests(unittest.TestCase):
    """VC-0 预跑目标平台门禁（vc0-gate-target.sh）与 VC-5 测试树准备（gates.sh prepare）共用 lib.sh 的 clone_test_tree。

    * E2-04 起预跑是入口门禁 entry-gates.sh（组合 preflight＝make test 的组成）的一次运行，前后照旧采集 gate_before／
      gate_after 环境收据；独立主体标识 vc0-preflight-<时间戳>、独立门禁根与字节码缓存都在 $RUNROOT/vc0-preflight 下，
      绝不写候选门禁目录、候选目录与 VC-5 的缓存；
    * 测试树与 VC-5 同一做法（完整历史克隆 → bundle 取分支 → 检出 → 断言），前端依赖 lockfile 不同即拒绝、不开跑；
    * 退出码：通过 0（删测试树与缓存）、门禁未通过 1（保留测试树与记录位置）、用法 2、准备失败或并发 3；
    * gates.sh prepare 改用共用函数后，VC-5 行为不变（node_modules 首次取本轮前端构建、重建时经缓存目录搬回）。

    执行器用替身（驱动随附的 unit_executor.py 换成 _EXECUTOR_STUB：在测试树里导入探针模块核对字节码缓存、记下 HEAD 与
    清单，按清单合成结果），受管环境收据 CLI 用替身。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._template_root = tempfile.TemporaryDirectory()
        cls.template = Path(cls._template_root.name).resolve() / "history"
        _history_repo(cls.template, commits=10001)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._template_root.cleanup()

    def _fixture(self, root: Path, *, fail_units: str = "", history: Path | None = None):
        fixture = _DriverFixture(root)
        hist = fixture.data_root / "candidates" / "hist"
        _git(root, "clone", "-q", str(history or self.template), str(hist))
        node_modules = hist / "frontend" / "node_modules" / "typescript" / "lib"
        node_modules.mkdir(parents=True)
        (node_modules / "typescript.js").write_text("// 前序测试树的 TypeScript\n", encoding="utf-8")
        work = root / "work"
        _git(root, "clone", "-q", str(hist), str(work))
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "vc0-gate-target.sh", "vc5-gate-target.sh", "gates.sh", "bytecode_cache.py",
                     "entry-gates.sh", "entry_gates.py", "entry_steps.py"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        (drv / "unit_executor.py").write_text(_EXECUTOR_STUB, encoding="utf-8")
        package = fixture.data_root / "tools" / "official_client_capture"
        (fixture.data_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "codex_upgrade_arm64_environment_receipt.py").write_text(_ENVIRONMENT_RECEIPT_STUB, encoding="utf-8")
        record = root / "executor-calls.jsonl"
        env = {**fixture.env, "SHIM_RECORD": str(record), "STUB_FAIL_UNITS": fail_units}
        return fixture, hist, work, drv, record, env

    @staticmethod
    def _bundle(work: Path, branch: str, target: Path, *, change_lockfile: bool = False) -> str:
        _git(work, "checkout", "-q", "-B", branch, "main")
        (work / "README.md").write_text("候选源码的改动\n", encoding="utf-8")
        (work / "Makefile").write_text(_CANDIDATE_MAKEFILE, encoding="utf-8")
        if change_lockfile:
            (work / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.1'\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "--no-verify", "-m", "candidate")
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(work, "bundle", "create", "-q", str(target), f"main..{branch}")
        return _git(work, "rev-parse", "HEAD")

    @staticmethod
    def _calls(record: Path) -> list[dict]:
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()] if record.exists() else []

    def _untouched_vc5_locations(self, fixture: _DriverFixture) -> None:
        self.assertFalse((fixture.data_root / "control" / f"{fixture.new}-candidate-gates").exists(), "预跑不得写候选门禁目录")
        self.assertFalse((fixture.candidate_dir / "test-tree").exists(), "预跑不得写候选测试树")
        self.assertFalse((fixture.runroot / "pycache-target-platform").exists(), "预跑不得用 VC-5 的字节码缓存")
        self.assertFalse((fixture.runroot / "node_modules-cache").exists())

    def test_preflight_passes_with_independent_subject_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, hist, work, drv, record, env = self._fixture(root)
            commit = self._bundle(work, "codex/vc0-preflight", root / "upload" / "vc0.bundle")
            result = _run(drv / "vc0-gate-target.sh", str(root / "upload" / "vc0.bundle"), "codex/vc0-preflight", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("VC0_GATE_TARGET_DONE rc=0", result.stdout)
            pre = fixture.runroot / "vc0-preflight"
            subjects = [path for path in pre.iterdir() if path.name.startswith("vc0-preflight-")]
            self.assertEqual(len(subjects), 1, sorted(path.name for path in pre.iterdir()))
            subject = subjects[0]
            self.assertRegex(subject.name, r"^vc0-preflight-\d{8}t\d{6}z(-\d+)?$")
            summary = json.loads((subject / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["purpose"], summary["accept_gate_receipt"], summary["status"]), ("vc0-preflight", False, "passed"))
            self.assertEqual((summary["subject_id"], summary["source"]["commit"], summary["source"]["tree_head"]), (subject.name, commit, commit))
            self.assertEqual((summary["gate"]["exit_code"], summary["gate"]["gate_json"]), (0, "logs/target-platform.gate.json"))
            self.assertTrue(summary["test_tree_removed"])
            gate = json.loads((subject / "logs" / "target-platform.gate.json").read_text(encoding="utf-8"))
            self.assertEqual((gate["gate_id"], gate["command"], gate["exit_code"], gate["composed_of"]),
                             ("target-platform", ["make", "test"], 0, ["backend-go-test", "backend-lint", "frontend-lint", "frontend-typecheck",
                                                                       "frontend-critical", "test-capture-tools", "test-official-client-control",
                                                                       "check-egress-spec"]))
            self.assertEqual(sorted(path.name for path in (subject / "environment").iterdir()), sorted(
                f"{subject.name}-{role}.json" for role in ("before-facts", "before", "after-facts", "after")))
            # 一次运行：make test 的全部组成在所要求的提交上执行，缓存是预跑目录里重建的那份（与 VC-5 分开）。
            (call,) = self._calls(record)
            self.assertEqual((call["head"], call["manifest"]["profile"]), (commit, "preflight"))
            self.assertEqual([gate["gate_id"] for gate in call["manifest"]["gates"]],
                             ["backend-go-test", "backend-lint", "frontend-lint", "frontend-typecheck", "frontend-critical",
                              "test-capture-tools", "test-official-client-control", "check-egress-spec"])
            cache = str(pre / "pycache-target-platform")
            self.assertEqual(call["pycache"], cache)
            self.assertTrue(call["cached"].startswith(cache) and call["cached_exists"], call["cached"])
            self.assertIsNone(call["identity_memo"], "测试树门禁不用生产的身份记忆化目录")
            self._untouched_vc5_locations(fixture)
            # 通过后删掉测试树与缓存、释放锁；数据根与历史测试树不留字节码。
            self.assertFalse((pre / "test-tree").exists())
            self.assertFalse((pre / "pycache-target-platform").exists())
            self.assertFalse((pre / ".lock").exists())
            self.assertFalse((pre / ".entry-gates.lock").exists())
            self.assertEqual(list(fixture.data_root.rglob("__pycache__")), [])
            self.assertEqual(stat.S_IMODE(pre.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(subject.stat().st_mode), 0o700)

    def test_failed_gate_exits_1_and_keeps_tree_and_record_locations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, hist, work, drv, record, env = self._fixture(root, fail_units="egress-spec:egress-spec-a")
            commit = self._bundle(work, "codex/vc0-preflight", root / "upload" / "vc0.bundle")
            result = _run(drv / "vc0-gate-target.sh", str(root / "upload" / "vc0.bundle"), "codex/vc0-preflight", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("VC0_GATE_TARGET_FAILED rc=1", result.stdout)
            self.assertIn("executor.log", result.stdout)
            pre = fixture.runroot / "vc0-preflight"
            subject = next(path for path in pre.iterdir() if path.name.startswith("vc0-preflight-"))
            summary = json.loads((subject / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["status"], summary["gate"]["exit_code"], summary["test_tree_removed"]), ("failed", 1, False))
            regression = json.loads((subject / "logs" / "full-regression.gate.json").read_text(encoding="utf-8"))
            self.assertEqual(regression["failed_gates"], ["check-egress-spec"])
            self.assertEqual(_git(pre / "test-tree", "rev-parse", "HEAD"), commit, "未通过时保留测试树供排查")
            self.assertFalse((pre / ".lock").exists())
            self._untouched_vc5_locations(fixture)

    def test_mismatched_lockfile_is_rejected_before_any_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, hist, work, drv, record, env = self._fixture(root)
            commit = self._bundle(work, "codex/vc0-preflight", root / "upload" / "vc0.bundle", change_lockfile=True)
            result = _run(drv / "vc0-gate-target.sh", str(root / "upload" / "vc0.bundle"), "codex/vc0-preflight", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("前端依赖不可用", result.stdout)
            self.assertEqual(self._calls(record), [], "前端依赖不可用时不得开跑任何门禁")
            self.assertEqual(list((fixture.runroot / "vc0-preflight").rglob("target-platform.gate.json")), [])
            self.assertFalse((fixture.runroot / "vc0-preflight" / ".lock").exists())

    def test_usage_errors_and_concurrent_run_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, hist, work, drv, record, env = self._fixture(root)
            commit = self._bundle(work, "codex/vc0-preflight", root / "upload" / "vc0.bundle")
            bundle = str(root / "upload" / "vc0.bundle")
            for label, arguments in {
                "参数个数": (bundle, "codex/vc0-preflight"),
                "提交不是 40 位": (bundle, "codex/vc0-preflight", commit[:12]),
                "bundle 相对路径": ("upload/vc0.bundle", "codex/vc0-preflight", commit),
                "分支名含空格": (bundle, "codex/vc0 preflight", commit),
            }.items():
                with self.subTest(label):
                    result = _run(drv / "vc0-gate-target.sh", *arguments, env=env, cwd=root)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            lock = fixture.runroot / "vc0-preflight" / ".lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            result = _run(drv / "vc0-gate-target.sh", bundle, "codex/vc0-preflight", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("拒绝并发", result.stdout)
            self.assertTrue(lock.is_dir(), "不得删除正在运行的另一次预跑的锁")
            self.assertEqual(self._calls(record), [])

    def test_vc5_prepare_keeps_behaviour_with_shared_clone_function(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, hist, work, drv, record, env = self._fixture(root)
            # 参数文件里的 VC-5 坐标：BUNDLE=$D/staging/x.bundle、BUNDLE_BRANCH=codex/x。
            commit = self._bundle(work, "codex/x", fixture.data_root / "staging" / "x.bundle")
            built = fixture.candidate_dir / "frontend-build" / "frontend" / "node_modules" / "typescript" / "lib"
            built.mkdir(parents=True)
            (built / "typescript.js").write_text("// 本轮前端构建的 TypeScript\n", encoding="utf-8")
            result = _run(drv / "gates.sh", "prepare", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tree = fixture.candidate_dir / "test-tree"
            self.assertIn(f"test-tree HEAD={commit} status=[]", result.stdout)
            self.assertEqual(_git(tree, "rev-list", "--count", "HEAD"), "10002")
            self.assertEqual((tree / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js").read_text(encoding="utf-8"),
                             "// 本轮前端构建的 TypeScript\n")
            # 重建同一棵树：已有 node_modules 经缓存目录搬回，不再从前端构建复制。
            marker = tree / "frontend" / "node_modules" / "marker.txt"
            marker.write_text("搬回的依赖\n", encoding="utf-8")
            again = _run(drv / "gates.sh", "prepare", commit, env=env, cwd=root)
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertTrue(marker.is_file())
            self.assertFalse((fixture.runroot / "node_modules-cache").exists())
            self.assertFalse((fixture.runroot / "vc0-preflight").exists())

    def test_short_history_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            short = root / "short-history"
            _history_repo(short, commits=3)
            fixture, hist, work, drv, record, env = self._fixture(root, history=short)
            commit = self._bundle(work, "codex/vc0-preflight", root / "upload" / "vc0.bundle")
            result = _run(drv / "vc0-gate-target.sh", str(root / "upload" / "vc0.bundle"), "codex/vc0-preflight", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("测试树不是完整历史", result.stdout + result.stderr)
            self.assertIn("VC0_GATE_TARGET_ABORTED", result.stdout)
            self.assertEqual(self._calls(record), [])


# unshare 垫片：isolated_run 调用形如 unshare -m --propagation private bash -c '<遮挡脚本>' isolated-gate <命令…>，
# 垫片跳过前 7 个参数，把实际命令、工作目录与隔离环境变量逐行记成 JSON；命令里含 SHIM_FAIL_PATTERN 时以 SHIM_FAIL_RC 退出。
_ISOLATION_SHIM = """#!/bin/bash
shift 7
python3 - "$SHIM_RECORD" "$@" <<'PY'
import json, os, sys
record, *command = sys.argv[1:]
with open(record, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"cwd": os.getcwd(), "command": command,
                             "pycache": os.environ.get("PYTHONPYCACHEPREFIX"),
                             "source_root": os.environ.get("CODEX_0_149_1_SOURCE_ROOT"),
                             "typescript": os.environ.get("CAPTURE_TYPESCRIPT_MODULE")}, ensure_ascii=False) + "\\n")
PY
echo "gate-stub-stdout $*"
echo "gate-stub-stderr" >&2
if [ -n "${SHIM_FAIL_PATTERN:-}" ] && [[ "$*" == *"$SHIM_FAIL_PATTERN"* ]]; then exit "${SHIM_FAIL_RC:-2}"; fi
exit 0
"""

# 候选提交里的 CI 定义：部署脚本测试行（ARM64 全量门禁从这里逐行取出，不在脚本里写死）。
_CI_WORKFLOW = """jobs:
  shell:
    steps:
      - name: Check deploy scripts
        run: |
          /bin/bash -n deploy/apple-container.sh
          /bin/bash deploy/tests/apple-container-test.sh
          /bin/sh deploy/tests/docker-compose-security-test.sh
  test:
    steps:
      - name: Check Docker Compose simple mode environment
        run: /bin/sh deploy/tests/docker-compose-simple-mode-env-test.sh
"""


# 候选提交里的最小 Makefile：入口门禁按 print-egress-spec-checks 读 check-egress-spec 的子检查清单（与真实 Makefile 同一入口）。
_CANDIDATE_MAKEFILE = "print-egress-spec-checks:\n\t@echo check-egress-spec-local-source test-official-client-control egress-spec-a\n"

# 执行器替身（驱动随附的 unit_executor.py 换成它）：只支持 run-gates。在测试树里导入探针模块（核对 .pyc 来自预编译的
# 树外缓存），把工作目录、HEAD、环境与清单逐行记成 JSON，按清单逐单元合成执行器汇总；STUB_FAIL_UNITS（逗号分隔）里的
# 单元判失败。执行器自身的调度、额度与汇总由 test_ci_unit_executor 实测。
_EXECUTOR_STUB = """import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "run-gates", args
manifest = json.loads(Path(args[args.index("--manifest") + 1]).read_text(encoding="utf-8"))
out = Path(args[args.index("--out-dir") + 1])
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
try:
    import probe_pkg.probe as probe
    cached = probe.__cached__
except ImportError:
    cached = None
with open(os.environ["SHIM_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"cwd": os.getcwd(), "head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
                             "pycache": os.environ.get("PYTHONPYCACHEPREFIX"), "cached": cached, "cached_exists": bool(cached) and os.path.isfile(cached),
                             "typescript": os.environ.get("CAPTURE_TYPESCRIPT_MODULE"), "source_root": os.environ.get("CODEX_0_149_1_SOURCE_ROOT"),
                             "identity_memo": os.environ.get("CODEX_UPGRADE_IDENTITY_MEMO"), "manifest": manifest}, ensure_ascii=False) + "\\n")
failing = {item for item in os.environ.get("STUB_FAIL_UNITS", "").split(",") if item}
stamp = ("2026-10-01T10:00:00Z", "2026-10-01T10:05:00Z")
rows = [{"type": "command", "unit_id": unit["unit_id"], "kind": "formal", "passed": unit["unit_id"] not in failing,
         "exit_code": 1 if unit["unit_id"] in failing else 0, "signal": None, "timed_out": False, "seconds": 1.0, "cpu_seconds": 1.0,
         "max_rss_mb": 10.0, "orphans": 0, "log": str(out / "logs" / (unit["unit_id"].replace(":", "-") + ".log")), "argv": unit["argv"],
         "cwd": unit["cwd"], "started_at_utc": stamp[0], "completed_at_utc": stamp[1]} for unit in manifest["units"]]
by_id = {row["unit_id"]: row for row in rows}
groups = {group["group_id"]: {"status": "passed", "start": group["start"], "pattern": group["pattern"], "expected_tests": 3, "reported_tests": 3,
          "counts": {"passed": 2, "failed": 0, "error": 0, "skipped": 1, "expected_failure": 0, "unexpected_success": 0},
          "full_set": {"missing": [], "duplicated": [], "unexpected": [], "units_not_run": []}, "failed_units": [], "units": ["test_probe"],
          "skipped": [{"test_id": "test_probe.T.test_linux_only", "reason": "需要 Linux root"}]} for group in manifest["test_groups"]}
gates = []
for gate in manifest["gates"]:
    failed = [unit for unit in gate["units"] if not by_id[unit]["passed"]]
    gates.append({"gate_id": gate["gate_id"], "status": "failed" if failed else "passed", "units": gate["units"],
                  "test_groups": gate.get("test_groups", []), "failed_units": failed, "not_executed": gate.get("not_executed", []),
                  "started_at_utc": stamp[0], "completed_at_utc": stamp[1], "unit_seconds": float(len(gate["units"]))})
status = "failed" if any(gate["status"] != "passed" for gate in gates) else "passed"
out.mkdir(parents=True, exist_ok=True)
(out / "summary.json").write_text(json.dumps({"schema_version": "unit-executor-gates-summary/v1", "status": status, "policy_sha256": "0" * 64,
    "elapsed_seconds": 300.0, "gates": gates, "test_groups": groups, "units": rows, "units_not_run": [], "failed_units": sorted(failing),
    "diagnostic": [], "max_cores_in_use": 4.0}, ensure_ascii=False), encoding="utf-8")
print("OK" if status == "passed" else "FAILED (gates=1)", file=sys.stderr)
sys.exit(0 if status == "passed" else 1)
"""


class Arm64GateScriptTests(unittest.TestCase):
    """ARM64 全量门禁（arm64-full-gates.sh）与 ARM64 版 VC-4 门禁（arm64-vc4-gates.sh）：测试一律在采集主机执行。

    * 全量门禁：make test、backend test-unit／test-integration、golangci-lint unit／integration 与 CI 里的部署脚本测试依次全部执行，
      都经 isolated_run（与目标平台门禁同一隔离方式）；任一未通过退出 1 且其余照跑；结论只写 $RUNROOT/full-gates；
    * VC-4 门禁：DC 上 check-egress-spec 与 make test、C 上只跑 check-egress-spec-ci；产物与本机上传逐字段同格式，
      上传清单可被 vc4-all.sh 同一核验通过，READY 写在最后；DC 必须恰好是 C 加承接收据，否则不交付；
    * 用法错误退出 2，准备失败与并发退出 3。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._template_root = tempfile.TemporaryDirectory()
        cls.template = Path(cls._template_root.name).resolve() / "history"
        _history_repo(cls.template, commits=10001)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._template_root.cleanup()

    def _fixture(self, root: Path, *, fail_pattern: str = "", fail_rc: int = 2, fail_units: str = ""):
        fixture = _DriverFixture(root)
        hist = fixture.data_root / "candidates" / "hist"
        _git(root, "clone", "-q", str(self.template), str(hist))
        typescript = hist / "frontend" / "node_modules" / "typescript" / "lib"
        typescript.mkdir(parents=True)
        (typescript / "typescript.js").write_text("// 前序测试树的 TypeScript\n", encoding="utf-8")
        work = root / "work"
        _git(root, "clone", "-q", str(hist), str(work))
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "arm64-full-gates.sh", "arm64-vc4-gates.sh", "bytecode_cache.py", "upload_manifest.py",
                     "entry-gates.sh", "entry_gates.py", "entry_steps.py"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        # 全量门禁经入口门禁一次运行：执行器用替身（记下清单与环境、按清单合成结论）；VC-4 门禁仍经 isolated_run（unshare 垫片）。
        (drv / "unit_executor.py").write_text(_EXECUTOR_STUB, encoding="utf-8")
        bin_dir = root / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "unshare"
        shim.write_text(_ISOLATION_SHIM, encoding="utf-8")
        shim.chmod(0o700)
        record = root / "gate-commands.jsonl"
        env = {**fixture.env, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "SHIM_RECORD": str(record),
               "SHIM_FAIL_PATTERN": fail_pattern, "SHIM_FAIL_RC": str(fail_rc), "STUB_FAIL_UNITS": fail_units}
        return fixture, work, drv, record, env

    @staticmethod
    def _commands(record: Path) -> list[dict]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _candidate(work: Path, branch: str, *, with_ci: bool = True) -> str:
        _git(work, "checkout", "-q", "-B", branch, "main")
        (work / "backend").mkdir(exist_ok=True)
        (work / "backend" / "Makefile").write_text("test-unit:\n\ttrue\n", encoding="utf-8")
        (work / "Makefile").write_text(_CANDIDATE_MAKEFILE, encoding="utf-8")
        if with_ci:
            workflow = work / ".github" / "workflows" / "backend-ci.yml"
            workflow.parent.mkdir(parents=True, exist_ok=True)
            workflow.write_text(_CI_WORKFLOW, encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "--no-verify", "-m", "candidate")
        return _git(work, "rev-parse", "HEAD")

    @staticmethod
    def _set_env(fixture: _DriverFixture, **overrides: str) -> None:
        lines = []
        for line in fixture.env_file.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0]
            lines.append(f"{key}=\"{overrides[key]}\"" if key in overrides else line)
        fixture.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- ARM64 全量门禁 ----

    def test_full_gates_run_every_ci_gate_once_and_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root)
            commit = self._candidate(work, "codex/full-gates")
            bundle = root / "upload" / "full.bundle"
            bundle.parent.mkdir()
            _git(work, "bundle", "create", "-q", str(bundle), "main..codex/full-gates")
            result = _run(drv / "arm64-full-gates.sh", str(bundle), "codex/full-gates", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FULL_GATES_DONE rc=0", result.stdout)
            fg = fixture.runroot / "full-gates"
            subject = next(path for path in fg.iterdir() if path.name.startswith("full-gates-"))
            summary = json.loads((subject / "summary.json").read_text(encoding="utf-8"))
            expected = ["full-regression", "backend-unit", "backend-integration", "lint-unit", "lint-integration", "deploy-scripts"]
            self.assertEqual([gate["gate_id"] for gate in summary["gates"]], expected)
            self.assertEqual((summary["status"], summary["campaign_receipt"], summary["source"]["tree_head"], summary["test_tree_removed"]),
                             ("passed", False, commit, True))
            # 一次运行：全部门禁项的单元在同一份清单里交给执行器，测试树单元都套隔离前缀，环境与目标平台门禁相同。
            (call,) = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
            tree = str(fg / "test-tree")
            self.assertEqual((call["cwd"], call["head"], call["pycache"], call["typescript"], call["identity_memo"]),
                             (tree, commit, str(fg / "pycache"), f"{tree}/frontend/node_modules/typescript/lib/typescript.js", None))
            self.assertTrue(call["cached_exists"], "执行器在预编译好的树外缓存上运行")
            manifest = call["manifest"]
            self.assertEqual(manifest["profile"], "full-gates")
            units = {unit["unit_id"]: unit for unit in manifest["units"]}
            launcher = ["unshare", "-m", "--propagation", "private", "bash", "-c"]
            self.assertTrue(all(unit["argv"][:6] == launcher for unit in units.values()), "测试树单元都在私有挂载命名空间里遮住生产别名")
            self.assertEqual((units["backend:unit"]["argv"][-5:], units["backend:unit"]["cwd"]), (["go", "test", "-tags=unit", "./...", "-count=1"], f"{tree}/backend"))
            self.assertEqual(units["backend:integration"]["env"], {"GOMAXPROCS": "2", "CI": "true"})
            self.assertEqual([units[key]["argv"][-1] for key in ("backend:lint-unit", "backend:lint-integration")], ["--build-tags=unit", "--build-tags=integration"])
            # 多行 run 块与 `run:` 同一行的单行写法都要取到，每条单独一个单元；macOS 专用的那条在 Linux（ARM64、CI）上写明不在
            # 本平台执行，在 macOS 上照常执行。
            macos_only = "/bin/bash deploy/tests/apple-container-test.sh"
            on_macos = sys.platform == "darwin"
            deploy = next(gate for gate in manifest["gates"] if gate["gate_id"] == "deploy-scripts")
            self.assertEqual([" ".join(units[unit]["argv"][8:]) for unit in deploy["units"]],
                             ["/bin/bash -n deploy/apple-container.sh", *([macos_only] if on_macos else []),
                              "/bin/sh deploy/tests/docker-compose-security-test.sh", "/bin/sh deploy/tests/docker-compose-simple-mode-env-test.sh"])
            self.assertEqual([" ".join(item["command"]) for item in deploy["not_executed"]], [] if on_macos else [macos_only])
            gate = json.loads((subject / "logs" / "backend-unit.gate.json").read_text(encoding="utf-8"))
            self.assertEqual((gate["gate_id"], gate["command"], gate["working_directory"], gate["exit_code"], gate["tree_head"]),
                             ("backend-unit", ["go", "test", "-tags=unit", "./...", "-count=1"], "backend", 0, commit))
            self.assertEqual(len(json.loads((subject / "logs" / "deploy-scripts.gate.json").read_text(encoding="utf-8"))["not_executed"]),
                             0 if on_macos else 1, "不在本平台执行的项写进门禁记录")
            for name in ("check-egress-spec", "test-capture-tools"):
                self.assertEqual(json.loads((subject / "p0" / f"{name}.json").read_text(encoding="utf-8"))["status"], "passed")
            self.assertFalse((fg / "test-tree").exists())
            self.assertFalse((fg / "pycache").exists())
            self.assertFalse((fg / ".lock").exists())
            self.assertEqual(list(fixture.data_root.rglob("__pycache__")), [])
            self.assertFalse((fixture.runroot / "local-gates").exists(), "全量门禁不得写 VC-4 交付目录")

    def test_full_gates_report_failure_but_run_every_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root, fail_units="backend:integration")
            commit = self._candidate(work, "codex/full-gates")
            bundle = root / "upload" / "full.bundle"
            bundle.parent.mkdir()
            _git(work, "bundle", "create", "-q", str(bundle), "main..codex/full-gates")
            result = _run(drv / "arm64-full-gates.sh", str(bundle), "codex/full-gates", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("FULL_GATES_FAILED", result.stdout)
            self.assertIn("failed=backend-integration", result.stdout)
            fg = fixture.runroot / "full-gates"
            subject = next(path for path in fg.iterdir() if path.name.startswith("full-gates-"))
            summary = json.loads((subject / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "failed")
            self.assertEqual({gate["gate_id"]: gate["exit_code"] for gate in summary["gates"]},
                             {"full-regression": 0, "backend-unit": 0, "backend-integration": 1, "lint-unit": 0, "lint-integration": 0, "deploy-scripts": 0},
                             "未通过的一项不影响其余门禁的结论")
            self.assertEqual(_git(fg / "test-tree", "rev-parse", "HEAD"), commit, "未通过时保留测试树供排查")
            self.assertFalse(summary["test_tree_removed"])

    def test_full_gates_reject_missing_ci_deploy_tests_usage_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root)
            commit = self._candidate(work, "codex/full-gates", with_ci=False)
            bundle = root / "upload" / "full.bundle"
            bundle.parent.mkdir()
            _git(work, "bundle", "create", "-q", str(bundle), "main..codex/full-gates")
            result = _run(drv / "arm64-full-gates.sh", str(bundle), "codex/full-gates", commit, env=env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("FULL_GATES_ABORTED", result.stdout)
            self.assertFalse(record.exists(), "取不到部署脚本测试时不得开跑任何门禁")
            for label, arguments in {"参数个数": (str(bundle), "codex/full-gates"), "提交不是 40 位": (str(bundle), "codex/full-gates", commit[:12]),
                                     "bundle 相对路径": ("upload/full.bundle", "codex/full-gates", commit)}.items():
                with self.subTest(label):
                    self.assertEqual(_run(drv / "arm64-full-gates.sh", *arguments, env=env, cwd=root).returncode, 2)
            lock = fixture.runroot / "full-gates" / ".lock"
            lock.mkdir(parents=True, exist_ok=True)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            again = _run(drv / "arm64-full-gates.sh", str(bundle), "codex/full-gates", commit, env=env, cwd=root)
            self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
            self.assertIn("拒绝并发", again.stdout)
            self.assertTrue(lock.is_dir())

    # ---- ARM64 版 VC-4 门禁 ----

    def _vc4_chain(self, fixture: _DriverFixture, work: Path, *, extra_in_dc: bool = False) -> tuple[str, str, str]:
        c = self._candidate(work, "codex/x")
        receipt = "docs/egress/maintenance/upstream-codex-test-candidate-freeze-successor.json"
        (work / receipt).parent.mkdir(parents=True, exist_ok=True)
        (work / receipt).write_text("{}\n", encoding="utf-8")
        if extra_in_dc:
            (work / "README.md").write_text("承接提交夹带的改动\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "--no-verify", "-m", "freeze successor")
        dc = _git(work, "rev-parse", "HEAD")
        bundle = fixture.data_root / "staging" / "x.bundle"
        _git(work, "bundle", "create", "-q", str(bundle), "main..codex/x")
        self._set_env(fixture, C=c, DC=dc, RECEIPT=receipt, BUNDLE=str(bundle), BUNDLE_BRANCH="codex/x")
        return c, dc, receipt

    def test_vc4_gates_deliver_local_gate_contract_for_vc4_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root)
            c, dc, receipt = self._vc4_chain(fixture, work)
            stale = fixture.runroot / "impl-logs"
            stale.mkdir(mode=0o700)
            (stale / "READY").write_text("", encoding="utf-8")
            (stale / "stale.log").write_text("上一轮残留\n", encoding="utf-8")
            result = _run(drv / "arm64-vc4-gates.sh", env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ARM64_VC4_GATES_DONE rc=0", result.stdout)
            work_root = fixture.runroot / "arm64-vc4-gates"
            commands = self._commands(record)
            self.assertEqual([(Path(entry["cwd"]).name, entry["command"]) for entry in commands], [
                ("wt-D", ["make", "check-egress-spec"]), ("wt-C", ["make", "check-egress-spec-ci"]), ("wt-D", ["make", "test"])])
            gates = fixture.runroot / "local-gates"
            self.assertEqual(sorted(path.name for path in gates.iterdir()), sorted(
                f"{gate}.{kind}" for gate in ("check-egress-spec", "full-regression") for kind in ("gate.json", "stdout.log", "stderr.log")))
            for gate_id, command in (("check-egress-spec", ["make", "check-egress-spec"]), ("full-regression", ["make", "test"])):
                meta = json.loads((gates / f"{gate_id}.gate.json").read_text(encoding="utf-8"))
                self.assertEqual((meta["gate_id"], meta["command"], meta["working_directory"], meta["exit_code"], meta["tree_head"]),
                                 (gate_id, command, ".", 0, dc))
                for field in ("host", "architecture", "started_at_utc", "completed_at_utc"):
                    self.assertTrue(meta[field], field)
            impl = fixture.runroot / "impl-logs"
            spec = (impl / "check-egress-spec.log").read_text(encoding="utf-8")
            self.assertIn(f"candidate_commit={c}（", spec)
            self.assertIn(f"executed_on_commit={dc}（", spec)
            # implementation_gates.py 与 vc4-all.sh 的读法：标题之后恰好一行 exit_code=0。
            self.assertEqual(re.findall(r"^exit_code=(-?\d+)$", spec.split("## make check-egress-spec", 1)[1], re.M), ["0"])
            cross = (impl / "cross-check" / "check-egress-spec.C-only.local.log").read_text(encoding="utf-8")
            self.assertIn(f"commit={c}\n", cross)
            self.assertTrue((impl / "READY").is_file())
            verify = subprocess.run([sys.executable, str(drv / "upload_manifest.py"), "verify", str(fixture.runroot)],
                                    capture_output=True, text=True, env={**os.environ, "C": c, "DC": dc})
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            subject = next(path for path in work_root.iterdir() if path.name.startswith("arm64-vc4-gates-"))
            self.assertEqual((subject / "superseded" / "impl-logs" / "stale.log").read_text(encoding="utf-8"), "上一轮残留\n")
            self.assertFalse((impl / "stale.log").exists(), "旧产物必须整体归档，不能混进本次交付")
            summary = json.loads((subject / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["status"], summary["candidate_commit"], summary["executed_on_commit"]), ("passed", c, dc))
            for path in (gates, impl):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertFalse((work_root / "wt-D").exists())
            self.assertFalse((work_root / ".lock").exists())

    def test_vc4_gates_deliver_failed_conclusion_with_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root, fail_pattern="make test", fail_rc=2)
            c, dc, receipt = self._vc4_chain(fixture, work)
            result = _run(drv / "arm64-vc4-gates.sh", env=env, cwd=root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("ARM64_VC4_GATES_FAILED", result.stdout)
            meta = json.loads((fixture.runroot / "local-gates" / "full-regression.gate.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["exit_code"], 2)
            self.assertTrue((fixture.runroot / "impl-logs" / "READY").is_file(), "门禁结论照常交付，由 VC-4／VC-5 判定")
            self.assertEqual(_git(fixture.runroot / "arm64-vc4-gates" / "wt-D", "rev-parse", "HEAD"), dc)

    def test_vc4_gates_refuse_chain_drift_and_placeholder_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, work, drv, record, env = self._fixture(root)
            self._vc4_chain(fixture, work, extra_in_dc=True)
            result = _run(drv / "arm64-vc4-gates.sh", env=env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("C..DC 的改动必须只有", result.stdout)
            self.assertFalse((fixture.runroot / "impl-logs" / "READY").exists(), "准备失败不得写 READY")
            self.assertEqual(self._commands(record), [])
            self._set_env(fixture, C="0" * 40)
            self.assertEqual(_run(drv / "arm64-vc4-gates.sh", env=env, cwd=root).returncode, 2)

    def test_isolation_matches_target_platform_gate(self) -> None:
        """isolated_run 的遮挡命令必须与 vc5-gate-target.sh 目标平台门禁逐字相同，避免两套隔离方式漂移。"""

        mount = "mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture"
        self.assertIn(f"unshare -m --propagation private bash -c '{mount} && exec make test'",
                      (SCRIPTS / "vc5-gate-target.sh").read_text(encoding="utf-8"))
        self.assertIn(f"unshare -m --propagation private bash -c '{mount} && exec \"$@\"' isolated-gate",
                      (SCRIPTS / "lib.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
