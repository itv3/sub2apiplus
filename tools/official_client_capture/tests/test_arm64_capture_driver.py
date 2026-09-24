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
            # 5b. 事故形态：模式改回 0600（与 manifest 一致）但 ctime 已漂移 → 读侧判 evidence-integrity，不可恢复
            drifted.chmod(0o600)
            fifth = _run(self.CLOSEOUT, str(attempt), env=env)
            self.assertEqual(fifth.returncode, 0, fifth.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").count("\n"), calls_after_first)
            with self.assertRaises(evidence_manifest.EvidenceManifestBoundaryDriftError) as caught:
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual(caught.exception.failure_class, "evidence-integrity")
            self.assertEqual(caught.exception.failure_observations, [{"check_id": "evidence-manifest.boundary", "failure_code": "stat-boundary-drift"}])

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
        exported = dict(line[len("export "):].split("=", 1) for line in result.stdout.splitlines())
        parser = load_script("parse_env")
        values = parser.parse(self._template())
        self.assertEqual(set(exported), set(values) | set(parser.derive(values)))
        self.assertEqual(exported["NEW"], "codex-9.1.0-formal-round1-YYYYMMDDtHHMMSSz")
        self.assertEqual(exported["B"], "/root/docker/capture-cli/data/candidates/codex-9.1.0-candidate-round1")
        self.assertTrue(exported["STAGE_BUDGETS"].startswith("'VC-0=45 "))
        # 输出的每一行都是可安全 eval 的单一赋值
        for line in result.stdout.splitlines():
            self.assertRegex(line, r"^export [A-Z_][A-Z0-9_]*=('[^']*'|[A-Za-z0-9_./:@%+=,-]+)$")

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


if __name__ == "__main__":
    unittest.main()
