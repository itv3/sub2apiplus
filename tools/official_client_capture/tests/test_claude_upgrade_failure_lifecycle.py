"""Claude 的失败生命周期探针（Claude 升级控制面设计 B2-11 第二部分）。

覆盖：

* 合同常量：合同名在 ``official-client-failure-lifecycle-*`` 下，补跑上限等于 Claude 编排器的生产值，父监督器必需事件数；
* 校验：按 Claude 合同与当前登记坐标构造的合规事实通过，身份、坐标、三次归档、最终结果、父监督器终态与清理逐项篡改都拒绝；
  容器别名取 ``capture_root`` 与登记 runs 别名的上级，``capture_root`` 的 runs 不是登记别名即拒绝；
* 端到端：真实 ``campaign-run`` 父监督器（Claude 新合同清单）派发本模块的 worker 子进程，worker 在 Claude 父租约里调 Claude 的
  有限补跑执行必然失败的合成作业。``docker`` 替身在本机执行容器内命令，并把失败脚本读的 ``/proc/net/route`` 换成一份没有
  默认路由的路由表（模拟断网容器）；第二个容器别名用指向数据根的符号链接模拟 bind mount。结束后四个精确目录与夹具都被清掉；
* worker 拒绝不可信参数（别名表不含 ``capture_root``、路径碰撞、私有目录权限），父监督器失败时夹具同样清掉。

``fake_probe`` 也供 Claude Job 演练的测试使用：真实父监督器的演练只在端到端用例里跑一次。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_failure_lifecycle as lifecycle
from tools.official_client_capture import claude_upgrade_job_rehearsal as claude_profile
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as receipt
from tools.official_client_capture import codex_upgrade_supervisor

CAPTURE = "capture-cli"
SYNTHETIC = "capture-cli-synthetic"

# docker 替身：exec 在本机执行容器内命令并记下调用；失败脚本读的 /proc/net/route 换成替身路由表（模拟断网容器只有回环）。
FAKE_DOCKER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args[:3]) + "\n")
if len(args) < 3 or args[0] != "exec":
    print("fake docker 只接受 exec", file=sys.stderr)
    sys.exit(125)
command = args[2:]
if command[:2] == ["python3", "-c"]:
    command = [sys.executable, "-c", command[2].replace("/proc/net/route", os.environ["FAKE_ROUTE_TABLE"]), *command[3:]]
os.execvp(command[0], command)
'''
# 断网容器的路由表：只有表头和一条回环路由，没有目的地址为 00000000 的默认路由。
ROUTE_TABLE = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\nlo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0\n"


def fake_probe(configuration: Mapping[str, Any], *, nonce: str = "f" * 24) -> dict[str, Any]:
    """按 Claude 合同与当前登记坐标构造一份合规的失败生命周期探针，并按 ``validate_probe`` 校验后返回。"""

    source = lifecycle.SOURCE_PREFIX + nonce
    aliases = lifecycle.container_aliases(configuration["capture_root"])
    host_runs = lifecycle.registered_host_runs_root()
    runs = PurePosixPath(str(configuration["capture_root"])) / "runs"
    archives = []
    for index in range(1, lifecycle.ATTEMPT_COUNT + 1):
        name = f"{source}.failed-attempt{index}"
        marker = {"schema_version": lifecycle.MARKER_SCHEMA, "source_name": source, "attempt_index": index,
                  "network_isolated": True, "live_request_count": 0}
        archives.append({
            "attempt_index": index, "archive_name": name, "host_archive": str(host_runs / name),
            "container_archives": [str(alias / "runs" / name) for alias in aliases], "device": 41, "inode": 1000 + index,
            "marker_sha256": receipt._sha256_bytes(receipt._canonical(marker)), "marker": marker,
        })
    probe = {
        "schema_version": lifecycle.SCHEMA, "status": "passed", "client": "claude",
        "campaign_run_schema_version": codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA,
        "campaign_id": lifecycle.CAMPAIGN_PREFIX + nonce, "job_id": lifecycle.JOB_ID, "source_name": source,
        "execution_container": configuration["synthetic_capture_container"],
        "verified_containers": [configuration["capture_container"], configuration["synthetic_capture_container"]],
        "capture_root": str(configuration["capture_root"]), "container_aliases": [str(alias) for alias in aliases],
        "host_runs_root": str(host_runs), "retry_limit": lifecycle.RETRY_LIMIT, "attempt_count": lifecycle.ATTEMPT_COUNT,
        "network_isolated": True, "live_request_count": 0, "archives": archives,
        "final_result": {
            "status": "failed", "attempt_index": lifecycle.ATTEMPT_COUNT,
            "evidence_roots": [str(runs / f"{source}.failed-attempt{lifecycle.ATTEMPT_COUNT}")],
            "archived_evidence_roots": [str(runs / f"{source}.failed-attempt{index}") for index in range(1, lifecycle.ATTEMPT_COUNT)],
            "environment_contaminated": False,
        },
        "parent_supervisor": {"run_state": "stopped", "audit_incomplete": False, "event_count": 30,
                              "required_event_count": lifecycle.REQUIRED_EVENT_COUNT,
                              "actions": [{"action_id": lifecycle.ACTION_ID, "returncode": 0, "status": "passed"}]},
        "cleanup_verified": True,
    }
    return lifecycle.validate_probe(probe, configuration)


class LifecycleWorld(unittest.TestCase):
    """临时数据根（runs、runtime 两个 0700 目录）与登记坐标：两个容器别名，第二个是指向数据根的符号链接。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        for name in ("runs", "runtime"):
            (self.data / name).mkdir(parents=True, mode=0o700)
        self.alias = self.root / "oauth-alias"
        self.alias.symlink_to(self.data, target_is_directory=True)
        self.configuration = {"capture_root": str(self.data), "capture_container": CAPTURE, "synthetic_capture_container": SYNTHETIC}

    @contextlib.contextmanager
    def registered(self) -> Iterator[None]:
        with mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", self.data / "runs"), mock.patch.object(
            claude_upgrade,
            "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS",
            (PurePosixPath(str(self.data / "runs")), PurePosixPath(str(self.alias / "runs"))),
        ):
            yield


class ContractTests(LifecycleWorld):
    def test_constants(self) -> None:
        for name in (lifecycle.SCHEMA, lifecycle.WORKER_SCHEMA, lifecycle.MARKER_SCHEMA, lifecycle.PREDECESSOR_SCHEMA):
            self.assertTrue(name.startswith("official-client-failure-lifecycle-"), name)
        self.assertEqual(lifecycle.RETRY_LIMIT, claude_upgrade.JOB_RETRY_LIMIT, "补跑上限是 Claude 编排器的生产值")
        self.assertEqual(lifecycle.ATTEMPT_COUNT, 3)
        self.assertEqual(lifecycle.REQUIRED_EVENT_COUNT, len(lifecycle._required_event_pairs()))
        self.assertEqual(dict(lifecycle.VERIFY_CONTAINER_FIELDS), dict(claude_profile.LAUNCHER_CONTAINER_FIELDS),
                         "两个容器与 Job 演练画像的启动器同一组")
        self.assertEqual(lifecycle.EXECUTION_CONTAINER_FIELD, claude_profile.LAUNCHER_CONTAINER_FIELDS["synthetic_container"],
                         "失败作业在断网容器里执行")
        self.assertTrue(lifecycle.FAILURE_SCRIPT.rstrip().endswith(f"raise SystemExit({lifecycle.FAILURE_EXIT_CODE})"),
                        "失败脚本的退出码与常量一致")
        self.assertIn("'schema_version':'official-client-failure-lifecycle-marker/v1'", lifecycle.FAILURE_SCRIPT,
                      "失败脚本写的 marker 合同名与常量一致")

    def test_container_aliases_follow_the_registered_runs_aliases(self) -> None:
        with self.registered():
            self.assertEqual(lifecycle.container_aliases(str(self.data)), sorted([PurePosixPath(str(self.data)), PurePosixPath(str(self.alias))], key=str))
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "不是登记的容器 runs 别名"):
                lifecycle.container_aliases("/capture")
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "规范绝对路径"):
                lifecycle.container_aliases("relative/path")
        self.assertEqual(lifecycle.container_aliases("/capture"),
                         [PurePosixPath("/capture"), PurePosixPath("/root/oauth-capture")], "生产登记：/capture 与 /root/oauth-capture")

    def test_validate_probe_accepts_the_contract_and_rejects_tampering(self) -> None:
        with self.registered():
            probe = fake_probe(self.configuration)

            def archive(index: int, key: str, value: Any):
                return lambda current: current["archives"][index].__setitem__(key, value)

            cases = {
                "合同名": (lambda current: current.__setitem__("schema_version", "codex-upgrade-failure-lifecycle-probe/v1"), "身份"),
                "Codex 清单": (lambda current: current.__setitem__("campaign_run_schema_version", "codex-upgrade-campaign-run/v2"), "身份"),
                "客户端": (lambda current: current.__setitem__("client", "codex"), "身份"),
                "执行容器": (lambda current: current.__setitem__("execution_container", CAPTURE), "身份"),
                "核对容器": (lambda current: current.__setitem__("verified_containers", [SYNTHETIC, CAPTURE]), "身份"),
                "宿主 runs 根": (lambda current: current.__setitem__("host_runs_root", "/root/docker/capture-cli/data/runs"), "身份"),
                "别名表": (lambda current: current.__setitem__("container_aliases", [str(self.data)]), "身份"),
                "补跑上限": (lambda current: current.__setitem__("retry_limit", 1), "身份"),
                "联网": (lambda current: current.__setitem__("live_request_count", 1), "身份"),
                "清理": (lambda current: current.__setitem__("cleanup_verified", False), "身份"),
                "随机名不同源": (lambda current: current.__setitem__("campaign_id", lifecycle.CAMPAIGN_PREFIX + "0" * 24), "身份"),
                "多字段": (lambda current: current.__setitem__("bubblewrap", {}), "字段不闭合"),
                "归档数量": (lambda current: current["archives"].pop(), "归档数量"),
                "归档 inode 重复": (archive(1, "inode", 1001), "第 2 次归档"),
                "归档名": (archive(0, "archive_name", "x"), "第 1 次归档"),
                "归档别名": (archive(2, "container_archives", []), "第 3 次归档"),
                "marker 摘要": (archive(0, "marker_sha256", "0" * 64), "第 1 次归档"),
                "marker 次序": (lambda current: current["archives"][1]["marker"].__setitem__("attempt_index", 1), "第 2 次归档"),
                "最终结果": (lambda current: current["final_result"].__setitem__("archived_evidence_roots", []), "最终作业结果"),
                "环境污染": (lambda current: current["final_result"].__setitem__("environment_contaminated", True), "最终作业结果"),
                "必需事件数": (lambda current: current["parent_supervisor"].__setitem__("required_event_count", 16), "父监督器终态"),
                "事件不足": (lambda current: current["parent_supervisor"].__setitem__("event_count", 3), "父监督器终态"),
                "动作失败": (lambda current: current["parent_supervisor"].__setitem__("actions", []), "父监督器终态"),
            }
            for label, (mutate, message) in cases.items():
                with self.subTest(label=label):
                    current = json.loads(json.dumps(probe))
                    mutate(current)
                    # 字段闭合由共享收据模块的 _expect 报（Job 演练错误），其余由本探针报（它的子类）：都按 Job 演练错误处理。
                    with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                        lifecycle.validate_probe(current, self.configuration)
            self.assertTrue(issubclass(lifecycle.FailureLifecycleError, receipt.JobRehearsalReceiptError),
                            "Job 演练的采集与重放把它当 Job 演练错误处理")


class EndToEndTests(LifecycleWorld):
    """真实父监督器（Claude 新合同）与 worker 子进程：失败作业三次执行、三份归档、两个容器核对、受监督清理。"""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(0o700)
        self.calls = self.root / "docker-calls.jsonl"
        self.route = self.root / "route"
        self.route.write_text(ROUTE_TABLE, encoding="ascii")

    @contextlib.contextmanager
    def environment(self) -> Iterator[None]:
        values = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_DOCKER_CALLS": str(self.calls),
            "FAKE_ROUTE_TABLE": str(self.route),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with self.registered(), mock.patch.dict(os.environ, values):
            yield

    def docker_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()] if self.calls.exists() else []

    def test_real_parent_supervisor_runs_the_claude_retry_and_archive_chain(self) -> None:
        with self.environment():
            probe = lifecycle.capture_probe(self.configuration)
            self.assertEqual(lifecycle.validate_probe(json.loads(json.dumps(probe)), self.configuration), probe)
        self.assertEqual(probe["status"], "passed")
        self.assertEqual((probe["client"], probe["campaign_run_schema_version"]),
                         ("claude", codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA))
        self.assertEqual([item["attempt_index"] for item in probe["archives"]], [1, 2, 3])
        self.assertEqual(probe["container_aliases"], sorted([str(self.data), str(self.alias)]))
        self.assertGreaterEqual(probe["parent_supervisor"]["event_count"], lifecycle.REQUIRED_EVENT_COUNT)
        calls = self.docker_calls()
        self.assertEqual(len([call for call in calls if call[1] == SYNTHETIC]), lifecycle.ATTEMPT_COUNT + 1,
                         "断网容器：三次失败作业加一次只读核对")
        self.assertEqual(len([call for call in calls if call[1] == CAPTURE]), 1, "抓包容器只做一次只读核对")
        self.assertEqual(sorted(path.name for path in (self.data / "runs").iterdir()), [], "固定证据根与三份归档都已清掉")
        self.assertEqual(list((self.data / "runtime").iterdir()), [], "夹具目录已删除")

    def test_parent_failure_cleans_the_fixture(self) -> None:
        with self.environment(), mock.patch.object(lifecycle, "_run_campaign", side_effect=lifecycle.FailureLifecycleError("替身：父监督器失败")):
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "父监督器失败"):
                lifecycle.capture_probe(self.configuration)
        self.assertEqual(list((self.data / "runtime").iterdir()), [])
        self.assertEqual(list((self.data / "runs").iterdir()), [])

    def test_untrusted_roots_fail_before_any_run(self) -> None:
        with self.environment():
            (self.data / "runtime").rmdir()
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "runtime 根不可信"):
                lifecycle.capture_probe(self.configuration)
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "必须不同"):
                lifecycle.capture_probe({**self.configuration, "synthetic_capture_container": CAPTURE})
        self.assertEqual(self.docker_calls(), [])


class WorkerArgumentTests(LifecycleWorld):
    def namespace(self, **changes: Any) -> argparse.Namespace:
        fixture = self.root / "fixture"
        for name in ("campaign", "logs"):
            (fixture / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        values = {
            "campaign_dir": str(fixture / "campaign"), "campaign_id": lifecycle.CAMPAIGN_PREFIX + "a" * 24,
            "capture_container": CAPTURE, "synthetic_container": SYNTHETIC, "capture_root": str(self.data),
            "container_aliases": json.dumps(sorted([str(self.data), str(self.alias)])), "host_runs_root": str(self.data / "runs"),
            "source_name": lifecycle.SOURCE_PREFIX + "a" * 24, "log_root": str(fixture / "logs"),
            "output": str(fixture / "worker-result.json"),
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_worker_rejects_untrusted_arguments_without_running(self) -> None:
        with mock.patch.object(claude_upgrade, "CampaignLease", side_effect=AssertionError("不得取得租约")):
            cases = {
                "别名表不含 capture_root": (self.namespace(container_aliases=json.dumps([str(self.alias)])), "包含 capture_root"),
                "别名表未排序": (self.namespace(container_aliases=json.dumps([str(self.data), str(self.alias)][::-1])), "排序"),
                "同一容器": (self.namespace(synthetic_container=CAPTURE), "必须不同"),
                "名字形态": (self.namespace(source_name="other"), "形态"),
                "输出已存在": (self.namespace(output=str(self.data / "runs")), "输出路径"),
            }
            for label, (namespace, message) in cases.items():
                with self.subTest(label=label), self.assertRaisesRegex(lifecycle.FailureLifecycleError, message):
                    lifecycle.worker(namespace)
            loose = self.namespace()
            Path(loose.log_root).chmod(0o755)
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "0700"):
                lifecycle.worker(loose)
            Path(loose.log_root).chmod(0o700)
            collision = self.data / "runs" / (lifecycle.SOURCE_PREFIX + "a" * 24 + ".failed-attempt2")
            collision.mkdir(mode=0o700)
            with self.assertRaisesRegex(lifecycle.FailureLifecycleError, "碰撞"):
                lifecycle.worker(self.namespace())
            self.assertTrue(collision.is_dir(), "碰撞时不清理别人的目录")
        self.assertEqual(stat.S_IMODE((self.data / "runs").stat().st_mode), 0o700)

    def test_worker_cli_reports_failures_on_stderr(self) -> None:
        argv = [lifecycle.WORKER_COMMAND, *(item for key, value in vars(self.namespace(source_name="other")).items()
                                           for item in ("--" + key.replace("_", "-"), value))]
        # main 按 worker 进程的口径设 umask 0077：测试进程里替掉，免得影响同一进程里的其他用例。
        with mock.patch.object(codex_upgrade_supervisor, "write_campaign_run_action_diagnostic") as diagnostic, \
                mock.patch.object(os, "umask"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(lifecycle.main(argv), 1)
        self.assertIn("worker 未通过", stderr.getvalue())
        self.assertEqual(diagnostic.call_args.kwargs["failure_kind"], "handled-error")


if __name__ == "__main__":
    unittest.main()
