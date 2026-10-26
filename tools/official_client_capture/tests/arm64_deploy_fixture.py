"""ARM64 受监督部署事务的离线测试夹具（Claude 升级 B5a-01）。

部署器只能由 root 在 aarch64 上运行，并且要用 renameat2、docker 与独立监督器。这里把这些外部前提换成替身，让
``main`` 在开发机上完整跑一遍真实的文件事务（复制候选、写意图日志、交换、核验、写收据、恢复）：

* 属主门禁只认 root：``pretend_root_owned`` 把测试根下路径的属主报告成 root:root，权限位照实；
* ``atomic_exchange``／``rename_noreplace``：用普通 rename 模拟最终结果（交换不是原子的，测试足够），可注入失败；
* 监督器替身逐个核对事件 metadata 能被真实监督器的 ``_metadata`` 接受，并记录事件序列；
* 外部命令、出口状态、场景清单章节与暂存身份按固定值返回；时间、时间戳与随机后缀固定，同一输入两次运行的
  收据逐字节可比。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import stat
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture import codex_upgrade_supervisor as supervisor

REPO_ROOT = Path(deploy.__file__).resolve().parents[1]
FIXED_STAMP = "20261007T120000Z"
FIXED_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
SUPERVISOR_SOURCE = "# 暂存监督器（测试替身）\nVALUE = 1\n"
NEW_PREPARER = "#!/bin/bash\n# 断言预处理器（新）\n"
OLD_PREPARER = "#!/bin/bash\n# 断言预处理器（旧）\n"
CODEX_IDENTITY_V2 = {
    "policy_version": 8,
    "policy_sha256": "1" * 64,
    "wire_producer_sha256": "2" * 64,
    "evidence_semantics_sha256": "3" * 64,
    "control_sha256": "4" * 64,
}
CONTAINER_OUTPUTS = {
    "enable:inspect-capture-network": json.dumps({"capture-network": {"IPAddress": "172.30.0.10"}}),
    "enable:inspect-service-network": json.dumps({"sub2apiplus_net": {"IPAddress": "172.25.0.3"}}),
    "enable:compile-production-supervisor": "",
    "enable:check-assertion-preparer": "",
    "enable:supervisor-help": "usage: codex_upgrade_supervisor.py",
    "enable:verify-container-tool": json.dumps({"sha256": "c" * 64, "uid": 0, "gid": 0, "mode": 0o644}),
    "enable:verify-codex-binaries": json.dumps([
        {"path": "/opt/codex-0.164.0/bin/codex", "version": "0.164.0", "returncode": 0, "stdout": "codex-cli 0.164.0"},
    ]),
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FixedDatetime(datetime):
    """固定「现在」：收据与意图里的时间字段在两次运行之间逐字相同。"""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


def exchange(first: Path, second: Path) -> None:
    """模拟 renameat2(RENAME_EXCHANGE) 的结果；前置检查与真实实现相同。"""

    if first.is_symlink() or second.is_symlink():
        raise deploy.DeploymentError("原子交换目标不得是符号链接。")
    if not first.exists() or not second.exists():
        raise deploy.DeploymentError("原子交换目标必须存在。")
    if first.is_dir() != second.is_dir():
        raise deploy.DeploymentError("原子交换目标类型不一致。")
    temporary = first.with_name(f".{first.name}.exchange-{secrets.token_hex(4)}")
    os.rename(first, temporary)
    os.rename(second, first)
    os.rename(temporary, second)


def rename_noreplace(source: Path, destination: Path) -> None:
    """模拟 renameat2(RENAME_NOREPLACE)：目标存在即 FileExistsError。"""

    if source.is_symlink() or destination.is_symlink():
        raise deploy.DeploymentError("不覆盖改名的两端不得是符号链接。")
    if not source.exists():
        raise deploy.DeploymentError(f"不覆盖改名的源不存在：{source}")
    if destination.exists():
        raise FileExistsError(str(destination))
    os.rename(source, destination)


@contextlib.contextmanager
def pretend_root_owned(base: Path) -> Iterator[None]:
    """把 ``base`` 之下路径的属主报告成 root:root（部署器的属主门禁只认 root）；权限位与类型照实。"""

    base_text = os.path.realpath(base)
    real_stat, real_lstat = Path.stat, Path.lstat

    def owned(path: Path, result: os.stat_result) -> os.stat_result:
        location = os.path.realpath(os.path.abspath(path))
        if location != base_text and not location.startswith(base_text + os.sep):
            return result
        values = list(result[:10])
        values[4] = 0
        values[5] = 0
        return os.stat_result(values)

    def fake_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        return owned(self, real_stat(self, follow_symlinks=follow_symlinks))

    def fake_lstat(self: Path) -> os.stat_result:
        return owned(self, real_lstat(self))

    with mock.patch.object(Path, "stat", fake_stat), mock.patch.object(Path, "lstat", fake_lstat):
        yield


def fake_claude_identity(repository_root: Path) -> dict[str, Any]:
    """Claude 身份替身：按两个根的完整内容摘要合成五摘要（暂存与切换后的生产内容相同时结果相同）。"""

    roots = {}
    for root in deploy.CLIENT_MANAGED_ROOTS["claude"]:
        path = Path(repository_root) / root
        if not path.is_dir():
            raise deploy.DeploymentError(f"受管根不存在或不可信：{root}")
        roots[root] = {"tool_files_sha256": deploy.tree_content_sha256(path), "entry_count": 1}
    whole = deploy.sha256_bytes(deploy.canonical(roots))
    return {
        "client": "claude",
        "policy_version": 1,
        "policy_sha256": "a" * 64,
        "wire_producer_sha256": sha256_text("wire:" + whole),
        "evidence_semantics_sha256": sha256_text("evidence:" + whole),
        "control_sha256": sha256_text("control:" + whole),
        "tool_files_sha256": whole,
        "roots": roots,
        "runtime_bindings": [],
    }


class DeployHarness:
    """一套可重复的部署现场：暂存根、生产数据根与替身环境。"""

    def __init__(self, root: Path, module: Any = deploy) -> None:
        self.root = Path(root)
        self.module = module
        self.events: list[tuple[str, str]] = []
        self.exchange_calls: list[tuple[str, str]] = []
        # 注入点：交换前／不覆盖改名前调用，抛错即模拟该步失败。
        self.before_exchange: Callable[[Path, Path], None] | None = None
        self.before_rename_noreplace: Callable[[Path, Path], None] | None = None
        # 注入点：交换／不覆盖改名完成之后调用，抛错即模拟「这一步已完成、下一步之前进程中断」。
        self.after_exchange: Callable[[Path, Path], None] | None = None
        self.after_rename_noreplace: Callable[[Path, Path], None] | None = None
        self.stamp = FIXED_STAMP
        self.claude_identity: Callable[[Path], dict[str, Any]] | None = fake_claude_identity
        self.claude_pre_a3: Callable[[Path], dict[str, Any]] | None = lambda _root: {
            "status": "not-registered", "scenario_count": 0, "real_chain_registration": [], "reason": "测试替身"}
        self.codex_identity_v2: dict[str, Any] | None = dict(CODEX_IDENTITY_V2)
        self.scenario_source_spec = True
        self.pre_a3 = True
        self.last_output = ""

    # ---- 坐标
    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def control(self) -> Path:
        return self.data / "control"

    @property
    def docs(self) -> Path:
        return self.data / "docs"

    @property
    def run_dir(self) -> Path:
        return self.root / "supervisor-run"

    def production(self, root: str) -> Path:
        return self.data / root

    # ---- 现场
    def build(
        self,
        *,
        control_root_old: bool | None = None,
        claude_documents_old: bool = False,
        missing_runtime_documents: int = 3,
        claude_staging_documents: bool = True,
    ) -> None:
        """建一套暂存与生产：两个受管根的新旧版本只差一个文件；Codex 文档全部已在生产，前 N 份运行时文档首次纳管。

        ``control_root_old``：None 表示生产没有 official_client_control（首次纳管），True 表示有旧副本。
        """

        if self.root.exists():
            for child in self.root.iterdir():
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        self.root.mkdir(parents=True, exist_ok=True)
        for side, label in ((self.staging, "new"), (self.data, "old")):
            capture = side / deploy.CAPTURE_ROOT
            write(capture / "codex_upgrade_supervisor.py", SUPERVISOR_SOURCE)
            write(capture / "capture.py", f"VERSION = {label!r}\n")
            write(capture / "codex_upgrade_scenarios_0_164_0.json", json.dumps({"label": label}))
            write(capture / "tests" / "test_tool.py", f"# {label}\n")
            write(side / "tools" / deploy.MANAGED_ASSERTION_PREPARER, NEW_PREPARER if label == "new" else OLD_PREPARER)
        write(self.staging / deploy.CONTROL_ROOT / "store.py", "STORE = 'new'\n")
        write(self.staging / deploy.CONTROL_ROOT / "schemas" / "campaign.schema.json", "{\"new\": true}\n")
        if control_root_old:
            write(self.data / deploy.CONTROL_ROOT / "store.py", "STORE = 'old'\n")
            write(self.data / deploy.CONTROL_ROOT / "schemas" / "campaign.schema.json", "{\"old\": true}\n")
        staging_docs = self.staging / "docs"
        for name in deploy.MANAGED_DOCUMENTS:
            write(staging_docs / name, f"new:{name}\n")
            write(staging_docs / "repository-docs" / name, f"new:{name}\n")
            write(self.docs / name, f"old:{name}\n")
            write(self.docs / "repository-docs" / name, f"old:{name}\n")
        if claude_staging_documents:
            for name in deploy.CLAUDE_MANAGED_DOCUMENTS:
                write(staging_docs / name, f"new:{name}\n")
                write(staging_docs / "repository-docs" / name, f"new:{name}\n")
        if claude_documents_old:
            for name in deploy.CLAUDE_MANAGED_DOCUMENTS:
                write(self.docs / name, f"old:{name}\n")
                write(self.docs / "repository-docs" / name, f"old:{name}\n")
        for index, name in enumerate(deploy.MANAGED_RUNTIME_DOCUMENTS):
            write(staging_docs / name, f"new:{name}\n")
            if index >= missing_runtime_documents:
                write(self.docs / name, f"old:{name}\n")
        self.control.mkdir(parents=True, exist_ok=True)
        self.control.chmod(0o700)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ---- 替身
    def _exchange(self, first: Path, second: Path) -> None:
        if self.before_exchange is not None:
            self.before_exchange(first, second)
        self.exchange_calls.append((str(first), str(second)))
        exchange(first, second)
        if self.after_exchange is not None:
            self.after_exchange(first, second)

    def _rename_noreplace(self, source: Path, destination: Path) -> None:
        if self.before_rename_noreplace is not None:
            self.before_rename_noreplace(source, destination)
        rename_noreplace(source, destination)
        if self.after_rename_noreplace is not None:
            self.after_rename_noreplace(source, destination)

    def _supervisor(self) -> Any:
        events, run_dir = self.events, self.run_dir

        class Client:
            attached = False

            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.run_dir = run_dir

            @classmethod
            def attach_from_environment(cls) -> None:
                return None

            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *_exc: Any) -> bool:
                return False

            def event_start(self, operation: str, **_kwargs: Any) -> int:
                supervisor._operation(operation)
                events.append(("start", operation))
                return len(events)

            def event_end(self, operation: str, *, metadata: Any = None, **_kwargs: Any) -> int:
                # 与真实监督器一样追加耗时字段后清洗：metadata 违反事件上限即在这里失败。
                supervisor._metadata({**dict(metadata or {}), "duration_seconds": 0.0})
                events.append(("end", operation))
                return len(events)

            def event_fail(self, operation: str, *, reason: str, **_kwargs: Any) -> int:
                events.append(("fail", operation))
                return len(events)

        return types.SimpleNamespace(SupervisorClient=Client)

    def _run_checked(self, _client: Any, operation: str, _argv: list[str], **_kwargs: Any) -> str:
        if operation not in CONTAINER_OUTPUTS:
            raise AssertionError(f"未预期的外部命令：{operation}")
        return CONTAINER_OUTPUTS[operation]

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        module = self.module
        egress = {"policy_sha256": "e" * 64, "status_sha256": "f" * 64, "nodes": {}, "services": ["capture-cli"]}
        source_spec = {"manifest": "m", "source_spec": "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md#第二章",
                       "source_spec_sha256": "d" * 64}
        with contextlib.ExitStack() as stack:
            stack.enter_context(pretend_root_owned(self.root))
            stack.enter_context(mock.patch.object(os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(os, "chown"))
            stack.enter_context(mock.patch.object(os, "umask", return_value=0o022))
            stack.enter_context(mock.patch.object(platform, "machine", return_value="aarch64"))
            stack.enter_context(mock.patch.object(secrets, "token_hex", side_effect=lambda nbytes=32: "5a" * nbytes))
            stack.enter_context(mock.patch.dict(os.environ, {}))
            stack.enter_context(mock.patch.object(sys, "dont_write_bytecode", True))
            stack.enter_context(mock.patch.object(module, "datetime", FixedDatetime))
            stack.enter_context(mock.patch.object(module, "utc_stamp", side_effect=lambda: self.stamp))
            stack.enter_context(mock.patch.object(module, "atomic_exchange", side_effect=self._exchange))
            if hasattr(module, "rename_noreplace"):
                stack.enter_context(mock.patch.object(module, "rename_noreplace", side_effect=self._rename_noreplace))
            stack.enter_context(mock.patch.object(module, "load_supervisor", return_value=self._supervisor()))
            stack.enter_context(mock.patch.object(module, "run_checked", side_effect=self._run_checked))
            stack.enter_context(mock.patch.object(module, "verify_runtime_egress", return_value=egress))
            if self.scenario_source_spec:
                stack.enter_context(mock.patch.object(module, "verify_scenario_source_spec", return_value=source_spec))
            if self.pre_a3:
                stack.enter_context(mock.patch.object(
                    module, "verify_pre_a3_scenario_entries",
                    return_value={"scenario_count": 1, "real_chain_registration": []}))
            stack.enter_context(mock.patch.object(module, "read_target_codex_version", return_value="0.164.0"))
            if self.codex_identity_v2 is not None:
                stack.enter_context(mock.patch.object(module, "staging_identity_v2", return_value=dict(self.codex_identity_v2)))
            stack.enter_context(mock.patch.object(module, "project_ledger_summary", return_value=None))
            if self.claude_identity is not None and hasattr(module, "claude_identity"):
                stack.enter_context(mock.patch.object(module, "claude_identity", side_effect=self.claude_identity))
            if self.claude_pre_a3 is not None and hasattr(module, "verify_claude_pre_a3_entries"):
                stack.enter_context(mock.patch.object(module, "verify_claude_pre_a3_entries", side_effect=self.claude_pre_a3))
            yield

    def argv(self, *extra: str) -> list[str]:
        return [
            "--staging-root", str(self.staging),
            "--production-root", str(self.production(deploy.CAPTURE_ROOT)),
            "--production-doc-root", str(self.docs),
            "--control-root", str(self.control),
            "--expected-supervisor-digest", sha256_text(SUPERVISOR_SOURCE),
            "--expected-assertion-preparer-digest", sha256_text(NEW_PREPARER),
            *extra,
        ]

    def run(self, *extra: str) -> int:
        return self.run_argv(self.argv(*extra))

    def run_argv(self, argv: list[str]) -> int:
        """在替身环境里跑 main；它打印的结果 JSON 存到 ``last_output``，不进测试输出。"""

        output = io.StringIO()
        try:
            with self.patched(), contextlib.redirect_stdout(output):
                return int(self.module.main(argv))
        finally:
            self.last_output = output.getvalue()

    # ---- 观测
    def receipts(self, client: str) -> list[Path]:
        return sorted(self.control.glob(deploy.DEPLOY_RECEIPT_GLOBS[client]))

    def journals(self) -> list[Path]:
        return sorted(self.control.glob(deploy.SWITCH_JOURNAL_GLOB))

    def retired_journals(self) -> list[Path]:
        return sorted(self.control.glob("managed-deploy-*.journal" + deploy.SWITCH_JOURNAL_RETIRED_SUFFIX + "*"))

    def tree(self, root: str) -> str | None:
        path = self.production(root)
        return deploy.tree_content_sha256(path) if path.is_dir() else None

    def staging_tree(self, root: str) -> str:
        return deploy.tree_content_sha256(self.staging / root)

    def operations(self) -> list[str]:
        return [operation for kind, operation in self.events if kind == "start"]

    def snapshot(self) -> dict[str, Any]:
        """生产侧全部受管对象的内容快照（恢复前后对照用）。"""

        result: dict[str, Any] = {root: self.tree(root) for root in deploy.MANAGED_ROOT_CLIENTS}
        preparer = self.data / "tools" / deploy.MANAGED_ASSERTION_PREPARER
        result["preparer"] = deploy.file_sha256(preparer) if preparer.is_file() else None
        for path in sorted(self.docs.rglob("*")):
            relative = path.relative_to(self.docs).as_posix()
            if path.is_file() and not relative.startswith("."):
                result[f"docs/{relative}"] = deploy.file_sha256(path)
        return result


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
