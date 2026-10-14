"""声明级承接 ``declared-inputs/v1`` 的记录层测试（步骤 5 工作包 WP1：``tools/ci/unit_records.py`` 的新增部分）。

被测对象只有 ``unit_records.py`` 新增的验证侧代码——声明范围校验、输入展开、覆盖检查（C1～C7）、``check_record`` 头部包装、
读集消费者核验与合同凭证核验。执行器、登记表、采样器是别的工作包，这里不碰；需要「执行侧产物」时由本文件的夹具按冻结的
记录格式手工构造（范围条目、证据文件、read_audit、合同条目与运行前凭证），所以测试纯 Python、不依赖 strace、docker、真实 go／pnpm。

测试分五组：

1. 范围校验与输入展开（T1、T2）：合法／非法矩阵，条目名、类别、顺序、无 head，以及「改一个无关文件不改摘要」的记录层正例；
2. 覆盖检查（T3）：C1～C7 每条规则一正一反；
3. 消费者核验（T13、T17、T18）：合同凭证、篡改证据与结论、历史重放不读外部事实；
4. ``derived_pass`` 对新读集范围成立、旧范围判据不变；
5. control 四链不变回归（T-C 系列）：新版 ``unit_records.py`` 与基线提交的同名文件并排，对 control 风格的声明、记录、清单逐字比较，
   另有用 ARM64 H3 九链真实记录库样本（仓库外证据目录，存在才跑）的重放。

测试里的路径、提交号、摘要都是合成值。
"""

from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import difflib
import hashlib
import ipaddress  # noqa: F401 - 先导入，避免在「禁止读磁盘」的区段里才触发延迟导入
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.ci import control_read_audit as control_audit
from tools.ci import unit_records as ur
from tools.official_client_capture.tests.test_ci_unit_records import _commit, _repo, _write

try:  # 采样器（步骤 5 的 WP4）及其真实 strace 夹具；不在的分支只跳过互操作用例
    from tools.ci import declared_sampler as sampler
    from tools.official_client_capture.tests import declared_sampler_trace_fixture as real_strace
except ImportError:  # pragma: no cover
    sampler = real_strace = None

try:  # 入口门禁侧（步骤 5 的 WP2）及其测试脚手架（假 runner 的合成工具链世界）；不在的分支只跳过互操作用例
    from tools.ci import entry_gates
    from tools.official_client_capture.tests import test_ci_declared_inputs_entry as entry_scaffold
except ImportError:  # pragma: no cover
    entry_gates = entry_scaffold = None

REPO_ROOT = Path(__file__).resolve().parents[3]
RECORDS_FILE = REPO_ROOT / "tools" / "ci" / "unit_records.py"

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
SAMPLER = "5" * 64
SHA = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731 - 测试里随手造摘要

# 仓库里的文件：既有 backend／docs/egress／frontend 的合成内容，也有「无关的 Claude 测试文件」。
WORLD_FILES = {
    "VERSION": "0.0.1\n",
    "Makefile": "FRONTEND_CRITICAL_VITEST = a b\ntest-demo:\n\techo demo\n",
    "backend/go.mod": "module example.test/backend\n",
    "backend/go.sum": "example.test/dep v1.0.0 h1:AAAA=\n",
    "backend/a.go": "package main\n",
    "backend/internal/b.go": "package internal\n",
    "docs/egress/foundation/sink-stats.md": "统计\n",
    "docs/other.md": "无关文档\n",
    "frontend/package.json": "{}\n",
    "frontend/pnpm-lock.yaml": "lockfileVersion: 9\n",
    "frontend/src/x.ts": "export const x = 1\n",
    "tools/tool_a.py": "A = 1\n",
    "pkg/tests/test_claude_unrelated.py": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
}


# ---------------------------------------------------------------------------
# 构造器：声明范围、采样证据、合同条目
# ---------------------------------------------------------------------------


def make_scope() -> dict:
    """后端风格的声明范围：目录范围、单文件、枚举目录、输出区、git 历史、make 展开、六类 class 的仓库外前缀、五个合同。"""

    return {
        "schema_version": ur.DECLARED_SCOPE_SCHEMA,
        "repo": {
            "ranges": [{"name": "backend", "include": ["backend/"], "exclude": ["backend/bin/"]},
                       {"name": "egress-docs", "include": ["docs/egress/"], "exclude": []}],
            "files": ["VERSION", "backend/go.mod", "backend/go.sum"],
            "list_ok": ["tools/"],
            "repo_ignored": ["backend/bin/"],
            "head": False,
            "git_history": {"commits": [COMMIT_A], "reason": "台账测试用 git show 读固定的历史提交"},
        },
        "make_expansion": ["test-demo"],
        "outside": [
            {"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"},
            {"prefix": "{WORK}/go-mod-cache/", "class": "dependency-content", "contract": "cache:go-mod-cache"},
            {"prefix": "{WORK}/go-build-cache/", "class": "performance-cache", "contract": "cache:go-build-cache"},
            {"prefix": "{WORK}/go-tmp/", "class": "scratch"},
            {"prefix": "{TMP}/", "class": "scratch"},
            {"prefix": "/usr/lib/", "class": "system"},
            {"prefix": "/usr/lib/secret/", "class": "virtual"},
            {"prefix": "/etc/", "class": "system"},
            {"prefix": "/proc/", "class": "virtual"},
            {"prefix": "{HOME}/.gitconfig", "class": "home-config", "contract": "host-config:gitconfig"},
            {"prefix": "/var/run/docker.sock", "class": "system"},
        ],
        "contracts": ["toolchain:go", "cache:go-build-cache", "cache:go-mod-cache", "service-image:postgres", "host-config:gitconfig"],
        "network": "loopback-only",
        "unix_sockets": ["/var/run/docker.sock"],
    }


def make_sample(unit_id: str = "backend:go-test", *, launcher: list | None = None, tree: str = "/var/tmp/gate/tree") -> dict:
    """与 ``make_scope`` 配套、全部落在声明范围内的采样证据（C1～C7 全部通过）。"""

    argv = ["go", "test", "./...", "-count=1"]
    return {
        "schema_version": ur.DECLARED_SAMPLE_SCHEMA,
        "unit_id": unit_id,
        "command": {"launcher": list(launcher or []), "argv": argv, "cwd_rel": "backend",
                    "sha256": ur.declared_command_sha256(argv, "backend")},
        "sampled": {"commit": COMMIT_B, "utc": "2026-10-15T00:00:00Z", "kernel": "6.8.0", "strace": "6.8", "sampler_sha256": SAMPLER,
                    "runs": [{"cache": "cold", "exit_code": 0, "lines": 900, "seconds": 400}, {"cache": "warm", "exit_code": 0, "lines": 334, "seconds": 300}]},
        "aliases": {"TREE": tree, "WORK": "/var/tmp/gate/work", "HOME": "/root", "TMP": "/tmp"},
        "coverage": {"stream_complete": True, "unresolved": 1, "unresolved_reasons": {"symlink-metadata-not-bound:newfstatat": 1},
                     "unfinished_calls": 0, "unresolved_process_events": 0, "lines": 1234, "detached_markers": 3},
        "repo_accesses": [
            ["{TREE}/backend/go.mod", ["read", "stat"], True],
            ["{TREE}/backend/a.go", ["read"], True],
            ["{TREE}/backend/internal/b.go", ["read"], True],
            ["{TREE}/backend/bin/tool", ["write", "exec"], False],
            ["{TREE}/docs/egress/foundation/sink-stats.md", ["read"], True],
            ["{TREE}/VERSION", ["read"], True],
            ["{TREE}/Makefile", ["read"], True],
            ["{TREE}/backend", ["dir", "stat", "list"], False],
            ["{TREE}/docs", ["stat", "dir"], False],
            ["{TREE}/tools", ["list", "stat"], False],
            ["{TREE}/go.work", ["missing"], False],
            ["{TREE}/.git/objects/ab/cdef", ["read"], False],
        ],
        "outside_accesses": [
            ["/usr/local/go/src/", ["read", "stat"], 1000],
            ["/usr/local/go/bin/go", ["exec", "read"], 1],
            ["{WORK}/go-mod-cache/github.com/", ["read", "stat"], 500],
            ["{WORK}/go-build-cache/ab/", ["read", "write"], 700],
            ["{WORK}/go-tmp/", ["read", "write", "exec"], 30],
            ["{TMP}/go-test-123/", ["write", "read"], 9],
            ["/usr/lib/x86_64-linux-gnu/", ["read", "exec"], 40],
            ["/etc/ssl/", ["read"], 3],
            ["/proc/self/", ["read"], 4],
            ["{HOME}/.gitconfig", ["missing"], 1],
            ["/usr/local", ["stat"], 1],
            ["/var/tmp/gate/go.work", ["missing"], 1],
        ],
        "git_commands": [["git", "show", f"{COMMIT_A}:docs/egress/x.json"]],
        "network": [{"family": "AF_INET", "target": "127.0.0.1:5432"}, {"family": "AF_UNIX", "target": "/var/run/docker.sock"}],
        "exit_code": 0,
    }


def contract_entry(kind: str, name: str, sha: str | None = None, **detail) -> dict:
    """合同条目 ``contract:<类型>:<名字>``（生产侧探测后由执行器追加；这里手工按调研 4.x 的格式构造）。"""

    return {"category": "environment", "name": f"contract:{kind}:{name}", "sha256": sha or SHA(f"{kind}:{name}"),
            "detail": {"schema_version": ur.DECLARED_CONTRACT_SCHEMA, "type": kind, "id": name, **detail}}


GO_SUM_SHA = SHA("example.test/dep v1.0.0 h1:AAAA=\n")
IMAGE_ID = "sha256:" + "d" * 64


def make_contract_entries(go_sum_sha: str = GO_SUM_SHA) -> list[dict]:
    return [
        contract_entry("toolchain", "go", version_line="go version go1.26.0 linux/arm64", executable_sha256="e" * 64,
                       goroot={"path": "/usr/local/go"}, env={"GOFLAGS": "", "GOPROXY": "off"}),
        contract_entry("cache", "go-build-cache", role="performance-cache", location="work", rule="any-content-addressed",
                       requires={"argv_contains": ["-count=1"]}),
        contract_entry("cache", "go-mod-cache", role="dependency-content", location="work", rule="lock-derived-go-sum",
                       lock_file="backend/go.sum"),
        contract_entry("service-image", "postgres", reference="postgres:18.1-alpine3.23", image_id=IMAGE_ID),
        contract_entry("host-config", "gitconfig", path="{HOME}/.gitconfig", state="missing"),
        contract_entry("git-history", COMMIT_A, tree="c" * 40),
        contract_entry("make-expansion", "test-demo", text="echo demo"),
    ]


def make_credentials(go_sum_sha: str = GO_SUM_SHA) -> list[dict]:
    stamp = "2026-10-15T00:00:05Z"
    return [
        {"id": "cache:go-build-cache", "kind": "cache", "verified_before_run": True, "verified_at_utc": stamp,
         "observed": {"root_ok": True, "entries_hint": 12}},
        {"id": "cache:go-mod-cache", "kind": "cache", "verified_before_run": True, "verified_at_utc": stamp,
         "observed": {"go_sum_sha256": go_sum_sha, "verify": "skipped"}},
        {"id": "service-image:postgres", "kind": "service-image", "verified_before_run": True, "verified_at_utc": stamp,
         "observed": {"image_id": IMAGE_ID}},
    ]


def frontend_scope() -> dict:
    """前端风格：node_modules 是未跟踪的依赖安装目录，登记为 installed_dirs，内容由 lock-derived-pnpm 合同保证（锁文件作为单文件条目
    列出）；vitest 缓存在 node_modules/.vite/ 里，同时登记为输出区（允许写）。range 不排除 node_modules，与采样器草案一致。"""

    return {
        "schema_version": ur.DECLARED_SCOPE_SCHEMA,
        "repo": {"ranges": [{"name": "frontend", "include": ["frontend/"], "exclude": []}],
                 "files": ["frontend/pnpm-lock.yaml", "frontend/package.json", "Makefile"],
                 "repo_ignored": ["frontend/node_modules/.vite/"],
                 "installed_dirs": [{"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}]},
        "outside": [{"prefix": "/opt/node-v20/", "class": "toolchain", "contract": "toolchain:node"},
                    {"prefix": "{TMP}/", "class": "scratch"}, {"prefix": "/usr/lib/", "class": "system"}],
        "contracts": ["toolchain:node", "toolchain:pnpm", "cache:node-modules"],
    }


def frontend_sample() -> dict:
    argv = ["pnpm", "--dir", "frontend", "run", "lint:check"]
    return {
        "schema_version": ur.DECLARED_SAMPLE_SCHEMA, "unit_id": "frontend:lint",
        "command": {"launcher": [], "argv": argv, "cwd_rel": ".", "sha256": ur.declared_command_sha256(argv, ".")},
        "sampled": {"sampler_sha256": SAMPLER}, "aliases": {"TREE": "/var/tmp/gate/tree"},
        "coverage": {"stream_complete": True, "unresolved": 2, "unresolved_reasons": {"symlink-metadata-not-bound:lstat": 2},
                     "unfinished_calls": 0, "unresolved_process_events": 0, "lines": 50},
        "repo_accesses": [["{TREE}/frontend/src/x.ts", ["read"], True], ["{TREE}/frontend/package.json", ["read"], True],
                          ["{TREE}/frontend", ["dir", "stat", "list"], True], ["{TREE}/frontend/node_modules", ["dir", "stat", "list"], False],
                          ["{TREE}/frontend/node_modules/.modules.yaml", ["read"], False],
                          ["{TREE}/frontend/node_modules/eslint/index.js", ["read", "stat"], False],
                          ["{TREE}/frontend/node_modules/.bin/eslint", ["exec", "stat"], False],
                          ["{TREE}/frontend/node_modules/.vite/results.json", ["read", "write"], False]],
        "outside_accesses": [["/opt/node-v20/bin/node", ["exec"], 1], ["/usr/lib/x/", ["read"], 2]],
        "git_commands": [], "network": [], "exit_code": 0,
    }


FRONTEND_LOCK_SHA = SHA("lockfileVersion: 9\n")


def frontend_contract_entries(installed: str = FRONTEND_LOCK_SHA) -> list[dict]:
    return [
        contract_entry("toolchain", "node", version_line="v20.19.0", executable_sha256="1" * 64),
        contract_entry("toolchain", "pnpm", version_line="9.15.0", executable_sha256="2" * 64),
        contract_entry("cache", "node-modules", role="dependency-install", location="tree", rule="lock-derived-pnpm",
                       lock_file="frontend/pnpm-lock.yaml", installed_lock="frontend/node_modules/.pnpm/lock.yaml"),
    ]


def frontend_credentials(installed: str, lock: str) -> list[dict]:
    return [{"id": "cache:node-modules", "kind": "cache", "verified_before_run": True, "verified_at_utc": "2026-10-15T00:00:05Z",
             "observed": {"installed_lock_sha256": installed, "lock_file_sha256": lock}}]


# ---------------------------------------------------------------------------
# 穷举式单点畸形：兜住“不抛异常”的合同
# ---------------------------------------------------------------------------

# 畸形值里刻意有不可哈希的 []／{}：用它们当字典键或集合元素的写法（``x in 某个dict``）会抛 TypeError，随机变异很难稳定碰到。
MALFORMED_VALUES = (None, True, 0, -1, 2 ** 70, 1.5, "", "x", "/", [], {}, [[]], {"a": 1}, ["read"], "sha256:" + "d" * 64)
_DELETE_POINT = object()


def single_point_variants(value: Any, *, extra: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """把 ``value`` 里每一个位置（字典键、列表下标，含根以外的所有子节点）依次换成每个畸形值，再各删除一次；逐个产出
    （位置, 改后的深拷贝）。``extra`` 追加畸形值（例如只有证据文件能带、封存记录带不了的孤立代理字符）。"""

    def positions(node: Any, prefix: tuple = ()):
        if prefix:
            yield prefix
        if isinstance(node, dict):
            for key, item in node.items():
                yield from positions(item, prefix + (key,))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from positions(item, prefix + (index,))

    for place in positions(value):
        for malformed in (*MALFORMED_VALUES, *extra, _DELETE_POINT):
            copied = copy.deepcopy(value)
            parent = copied
            for step in place[:-1]:
                parent = parent[step]
            if malformed is _DELETE_POINT:
                del parent[place[-1]]
            else:
                parent[place[-1]] = copy.deepcopy(malformed)
            yield place, copied


# ---------------------------------------------------------------------------
# 夹具：一个完整的、可通过 check_record 的声明级记录
# ---------------------------------------------------------------------------


class DeclaredWorld:
    """临时 git 仓库＋记录库＋一条合格的声明级记录。``variant(...)`` 在其上做单点篡改并重新封存。"""

    STARTED = "2026-10-15T00:00:10Z"
    COMPLETED = "2026-10-15T00:05:00Z"

    def __init__(self, directory: Path, *, scope: dict | None = None, sample: dict | None = None,
                 contracts=None, credentials=None, unit_id: str = "backend:go-test", cwd_rel: str = "backend",
                 evidence_bytes: bytes | None = None) -> None:
        self.directory = Path(directory).resolve()
        self.root = _repo(self.directory / "repo", WORLD_FILES)
        self.repo = ur.RepoIndex.load(self.root)
        self.store = ur.RecordStore(self.directory / "store")
        self.unit_id = unit_id
        self.scope = scope if scope is not None else make_scope()
        self.sample = sample if sample is not None else make_sample(unit_id)
        self.contracts = contracts if contracts is not None else make_contract_entries()
        self.credentials = credentials if credentials is not None else make_credentials()
        self.cwd_rel = cwd_rel
        self.environment = [ur.value_entry("environment", "executor:arch", "arm64"),
                            {"category": "environment", "name": "envvar:GOMAXPROCS", "sha256": ur.sha256_bytes(b"GOMAXPROCS=2"), "detail": {"name": "GOMAXPROCS"}}]
        self.executor = {"files": {"unit_records.py": "9" * 64}, "sha256": "x" * 64}
        self.runs = 0
        self.log_digest = self._store_log("命令输出\n")
        self.evidence_digest = self.put_evidence(self.sample, raw_bytes=evidence_bytes)
        self.base = self.build()

    # ---- 基础设施 ----

    def _store_log(self, text: str) -> str:
        path = self.directory / f"log-{self.runs}-{abs(hash(text)) % 10**6}.txt"
        path.write_text(text, encoding="utf-8")
        digest = ur.file_sha256(path)
        self.store.put_log(path, digest)
        return digest

    def put_evidence(self, sample: dict, raw_bytes: bytes | None = None) -> str:
        """把证据写进记录库，返回它的字节摘要（同时是登记摘要与 trace.sha256）。缺省按规范化 JSON 字节写（此时字节摘要也是
        规范化摘要）；``raw_bytes`` 给定时原样写（例如采样器自己的半展开 JSON 字节）。"""

        path = self.directory / f"evidence-{self.runs}-{len(self.store.root.parts)}.json"
        path.write_bytes(ur.canonical(sample) if raw_bytes is None else raw_bytes)
        digest = ur.file_sha256(path)
        self.store.put_log(path, digest)
        self.runs += 1
        return digest

    def declaration(self, scope: dict | None = None, sample: dict | None = None, evidence_digest: str | None = None) -> dict:
        sample = sample if sample is not None else self.sample
        return {"declared_scope": scope if scope is not None else self.scope,
                "declared_sample": {"evidence_sha256": evidence_digest or self.evidence_digest,
                                    "command_sha256": sample["command"]["sha256"], "sampler_sha256": SAMPLER}}

    def inputs(self, declaration: dict | None = None, contracts=None) -> list[dict]:
        entries = ur.declared_inputs(self.repo, declaration or self.declaration())
        entries = entries + list(self.contracts if contracts is None else contracts)
        return ur._unique(entries)

    def spec(self, sample: dict | None = None) -> dict:
        sample = sample if sample is not None else self.sample
        return {"type": "command", "unit_id": self.unit_id, "env": {"GOMAXPROCS": "2"}, "cores": 0.7, "memory_mb": 5120, "exclusive": False,
                "timeout_seconds": 3600, "reservation_restartable": True,
                "argv": sample["command"]["launcher"] + sample["command"]["argv"], "cwd": str(self.root / self.cwd_rel).rstrip("/")}

    def build(self, *, inputs=None, spec=None, sample=None, declaration=None, audit_overrides=None, credentials="default",
              record_overrides=None) -> dict:
        """组装一份「执行侧会写下」的记录体与对应的当前事实；返回 dict(record, path, current, facts)。"""

        sample = sample if sample is not None else self.sample
        declaration = declaration or self.declaration(sample=sample)
        entries = inputs if inputs is not None else self.inputs(declaration)
        spec = spec if spec is not None else self.spec(sample)
        inputs_sha = ur.entries_sha256(entries)
        scope = declaration["declared_scope"]
        audit = {"status": "passed", "coverage_complete": True, "coverage_scope": ur.DECLARED_COVERAGE_SCOPE, "undeclared_count": 0, "undeclared": [],
                 "inputs_sha256": inputs_sha, "repo_root": str(self.root), "data_root": None, "sample_origin": "registry",
                 "trace": {"path": "evidence.json", "sha256": declaration["declared_sample"]["evidence_sha256"], "stored": "x"},
                 "sample_binding": {"unit_id": self.unit_id, "command_sha256": declaration["declared_sample"]["command_sha256"],
                                    "scope_sha256": ur.sha256_json(scope), "evidence_sha256": declaration["declared_sample"]["evidence_sha256"]},
                 "coverage": {"repo_paths": 12, "outside_paths": 12, "classes": {}}}
        audit.update(audit_overrides or {})
        body = {"unit_id": self.unit_id, "unit_type": "command", "kind": "formal", "run": {"run_id": f"run-{self.runs}-{len(self.store.root.parts)}"},
                "executor": self.executor, "policy_sha256": "p" * 64, "environment": self.environment,
                "environment_sha256": ur.entries_sha256(self.environment), "spec": spec, "spec_sha256": ur.sha256_json(spec), "inputs": entries,
                "inputs_sha256": inputs_sha, "inheritable": True, "not_inheritable_reason": None, "test_ids": [], "tests": None,
                "passed": True, "exit_code": 0, "signal": None, "timed_out": False, "seconds": 300.0, "log": {"sha256": self.log_digest},
                "started_at_utc": self.STARTED, "completed_at_utc": self.COMPLETED, "read_audit": audit}
        if credentials == "default":
            body["declared_contracts"] = copy.deepcopy(self.credentials)
        elif credentials is not None:
            body["declared_contracts"] = credentials
        body.update(record_overrides or {})
        return self.seal(body, entries, spec)

    def seal(self, body: dict, entries: list, spec: dict, current: "ur.Current | None" = None) -> dict:
        """封存并入库一份记录（附带一份把它列为正式执行的原运行清单）；``current`` 缺省时按给定的 entries／spec 算当前事实。"""

        record = ur.seal_record(body)
        self.store.put_record(record)
        run_id = record["run"]["run_id"]
        if self.store.manifest(run_id) is None:
            self.store.put_manifest(ur.build_manifest(run_id=run_id, units=[
                {"unit_id": self.unit_id, "disposition": "executed", "record_sha256": record["record_sha256"]}]))
        self.runs += 1
        if current is None:
            current = ur.Current(unit_id=self.unit_id, unit_type="command", spec=spec, spec_sha256=ur.sha256_json(spec), inputs=entries,
                                 inputs_sha256=ur.entries_sha256(entries), inheritable=True)
        facts = ur.RunFacts(policy_sha256="p" * 64, environment=self.environment, environment_sha256=ur.entries_sha256(self.environment),
                            executor=self.executor, max_age_hours=168, now=ur.parse_utc(self.COMPLETED) + 3600)
        return {"record": record, "path": self.store.record_path(self.unit_id, record["record_sha256"]), "current": current, "facts": facts}

    def check(self, item: dict | None = None, *, thorough: bool = True, **facts_changes) -> list[str]:
        item = item or self.base
        facts = item["facts"]
        for name, value in facts_changes.items():
            facts = ur.RunFacts(**{**vars(facts), name: value}) if hasattr(facts, name) else facts
        return ur.check_record(self.store, item["path"], item["record"], item["current"], facts, thorough=thorough)

    def reseal(self, mutate, *, current: bool = False) -> dict:
        """复制基准记录体，用 ``mutate`` 原地改，重新封存入库；``current`` 为真时把「当前输入」换成改后的明细。"""

        body = {key: copy.deepcopy(value) for key, value in self.base["record"].items() if key not in {"record_sha256", "schema_version"}}
        body["run"] = {"run_id": f"run-{self.runs}-{len(self.store.root.parts)}"}
        mutate(body)
        # 改坏的输入明细可能连摘要都算不出来：默认沿用基准的「当前事实」，只有 current=True 才按改后的明细重算。
        return self.seal(body, body["inputs"], body["spec"], None if current else self.base["current"])


class DeclaredTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.world = DeclaredWorld(Path(self._directory.name))


# ---------------------------------------------------------------------------
# T1：声明范围校验
# ---------------------------------------------------------------------------


class ScopeValidationTests(unittest.TestCase):
    def test_valid_scopes_pass_and_the_result_is_an_independent_copy(self) -> None:
        scope = make_scope()
        checked = ur.validate_declared_scope(scope)
        self.assertEqual(checked, scope)
        checked["repo"]["files"].append("changed")
        self.assertNotIn("changed", scope["repo"]["files"], "返回的是深拷贝，不是同一个对象")
        self.assertEqual(ur.validate_declared_scope(frontend_scope())["contracts"], ["toolchain:node", "toolchain:pnpm", "cache:node-modules"])
        minimal = {"schema_version": ur.DECLARED_SCOPE_SCHEMA, "repo": {"files": ["VERSION"]}}
        self.assertEqual(ur.validate_declared_scope(minimal), minimal, "可选字段缺省＝空，最小范围合法")
        with_requires = make_scope()
        with_requires["requires"] = {"argv_contains": ["-count=1"], "env_equals": {"GOPROXY": "off", "GOFLAGS": "-mod=readonly"}}
        self.assertEqual(ur.validate_declared_scope(with_requires), with_requires, "命令运行期要求（离线）是合同可选项，合法即可")
        go_test_only = make_scope()
        go_test_only["requires"] = {"go_test_argv_contains": ["-count=1"]}
        self.assertEqual(ur.validate_declared_scope(go_test_only), go_test_only, "go_test_argv_contains：入口门禁侧的 go-build-cache 合同用它，须被接受")
        plus_target = make_scope()
        plus_target["make_expansion"] = ["test-demo", "check+x/y"]
        self.assertEqual(ur.validate_declared_scope(plus_target), plus_target, "make 目标名允许加号（与入口门禁侧的目标名判据一致）")
        sockets ={"schema_version": ur.DECLARED_SCOPE_SCHEMA, "repo": {"files": ["VERSION"]}, "network": "loopback-only",
                   "unix_sockets": ["/var/run/docker.sock", "/run/containerd/containerd.sock"]}
        self.assertEqual(ur.validate_declared_scope(sockets), sockets, "AF_UNIX 目标登记在顶层 unix_sockets，network 仍是 loopback-only 字符串")
        self.assertEqual(ur.validate_declared_scope({**sockets, "unix_sockets": []})["unix_sockets"], [], "可以显式写空")
        self.assertNotIn("unix_sockets", ur.validate_declared_scope({k: v for k, v in sockets.items() if k != "unix_sockets"}), "缺省＝空，不会自动补键")

    def test_invalid_scopes_are_rejected_with_the_rule(self) -> None:
        def mutate(change):
            scope = make_scope()
            change(scope)
            return scope

        cases = {
            "顶层多一个键": (lambda s: s.update(extra=1), "字段集合是闭合的"),
            "缺 repo": (lambda s: s.pop("repo"), "字段集合是闭合的"),
            "schema_version 不对": (lambda s: s.update(schema_version="declared-scope/v0"), "schema_version"),
            "range 多键": (lambda s: s["repo"]["ranges"][0].update(extra=1), "字段集合是闭合的"),
            "range 缺 exclude": (lambda s: s["repo"]["ranges"][0].pop("exclude"), "字段集合是闭合的"),
            "range 名带大写": (lambda s: s["repo"]["ranges"][0].update(name="Backend"), "name"),
            "range 名重复": (lambda s: s["repo"]["ranges"][1].update(name="backend"), "name 必须唯一"),
            "include 为空数组": (lambda s: s["repo"]["ranges"][0].update(include=[]), "不得为空"),
            "include 含空串（整个仓库）": (lambda s: s["repo"]["ranges"][0].update(include=[""]), "非空"),
            "include 是绝对路径": (lambda s: s["repo"]["ranges"][0].update(include=["/etc/"]), "不以 / 开头"),
            "include 含 ..": (lambda s: s["repo"]["ranges"][0].update(include=["backend/../x/"]), "不得含空、. 或 .. 分量"),
            "include 含空分量": (lambda s: s["repo"]["ranges"][0].update(include=["backend//x/"]), "不得含空、. 或 .. 分量"),
            "exclude 含反斜杠": (lambda s: s["repo"]["ranges"][0].update(exclude=["backend\\bin/"]), "反斜杠"),
            "include 重复": (lambda s: s["repo"]["ranges"][0].update(include=["backend/", "backend/"]), "重复"),
            "files 以 / 结尾": (lambda s: s["repo"].update(files=["backend/"]), "单个文件"),
            "files 重复": (lambda s: s["repo"].update(files=["VERSION", "VERSION"]), "重复"),
            "list_ok 不以 / 结尾": (lambda s: s["repo"].update(list_ok=["tools"]), "以 / 结尾"),
            "head 为真": (lambda s: s["repo"].update(head=True), "repo.head 只能是 false"),
            "git_history 缺 reason": (lambda s: s["repo"]["git_history"].pop("reason"), "字段集合是闭合的"),
            "git_history reason 为空": (lambda s: s["repo"]["git_history"].update(reason=" "), "reason"),
            "git_history 提交号大写": (lambda s: s["repo"]["git_history"].update(commits=["A" * 40]), "40 位小写"),
            "git_history 提交号太短": (lambda s: s["repo"]["git_history"].update(commits=["abc123"]), "40 位小写"),
            "git_history 为空": (lambda s: s["repo"]["git_history"].update(commits=[]), "不得为空"),
            "make 展开与 files 同含 Makefile": (lambda s: s["repo"]["files"].append("Makefile"), "Makefile 由展开合同覆盖"),
            "make 展开与 range 同含 Makefile": (lambda s: s["repo"]["ranges"].append({"name": "mk", "include": ["Makefile"], "exclude": []}), "Makefile 由展开合同覆盖"),
            "make 目标名非法": (lambda s: s.update(make_expansion=["bad target"]), "make 目标名"),
            "outside 前缀是 {TREE}": (lambda s: s["outside"][0].update(prefix="{TREE}/x/"), "只能以"),
            "outside 前缀是根目录": (lambda s: s["outside"][0].update(prefix="/"), "根目录"),
            "outside 前缀是整个 {WORK}": (lambda s: s["outside"][3].update(prefix="{WORK}/"), "范围过宽"),
            "outside 前缀是相对路径": (lambda s: s["outside"][5].update(prefix="usr/lib/"), "只能以"),
            "outside 前缀含 ..": (lambda s: s["outside"][5].update(prefix="/usr/../lib/"), "不得含空、. 或 .. 分量"),
            "outside class 未知": (lambda s: s["outside"][3].update(**{"class": "weird"}), "class 未知"),
            "outside class 是数组（不可哈希，不得抛 TypeError）": (lambda s: s["outside"][3].update(**{"class": ["scratch"]}), "class 未知"),
            "outside class 是对象（不可哈希，不得抛 TypeError）": (lambda s: s["outside"][3].update(**{"class": {}}), "class 未知"),
            "outside class 是 None": (lambda s: s["outside"][3].update(**{"class": None}), "class 未知"),
            "toolchain 缺合同": (lambda s: s["outside"][0].pop("contract"), "必须带 contract"),
            "scratch 带合同": (lambda s: s["outside"][3].update(contract="cache:x"), "不绑定合同"),
            "合同不在 contracts 里": (lambda s: s["outside"][0].update(contract="toolchain:other"), "出现在 contracts 里"),
            "合同类型与 class 不符": (lambda s: s["outside"][0].update(contract="cache:go-mod-cache"), "toolchain:<名字>"),
            "outside 前缀重复": (lambda s: s["outside"].append(dict(s["outside"][5])), "互不重复"),
            "outside 项多键": (lambda s: s["outside"][3].update(extra=1), "字段集合是闭合的"),
            "合同类型未知": (lambda s: s["contracts"].append("docker:x"), "合同类型未知"),
            "contracts 里列 git-history": (lambda s: s["contracts"].append("git-history:abc"), "合同类型未知"),
            "合同 ID 缺类型": (lambda s: s["contracts"].append("noprefix"), "类型:名字"),
            "合同名带空格": (lambda s: s["contracts"].append("cache:bad name"), "合同名字"),
            "合同重复": (lambda s: s["contracts"].append("toolchain:go"), "重复"),
            "toolchain:go 缺依赖锁": (lambda s: s["repo"].update(files=["VERSION"]), "必须在 repo.files 里单独列出依赖锁"),
            "network 不是 loopback-only": (lambda s: s.update(network="any"), "loopback-only"),
            "network 改成对象（unix 目标不放在 network 里）": (lambda s: s.update(network={"mode": "loopback-only", "unix": ["/a.sock"]}), "loopback-only"),
            "unix_sockets 不是数组": (lambda s: s.update(unix_sockets="/var/run/docker.sock"), "字符串数组"),
            "unix_sockets 元素不是字符串": (lambda s: s.update(unix_sockets=[1]), "字符串数组"),
            "unix_sockets 是相对路径": (lambda s: s.update(unix_sockets=["var/run/docker.sock"]), "规范绝对路径"),
            "unix_sockets 以 / 结尾": (lambda s: s.update(unix_sockets=["/var/run/"]), "规范绝对路径"),
            "unix_sockets 是根目录": (lambda s: s.update(unix_sockets=["/"]), "规范绝对路径"),
            "unix_sockets 含 ..": (lambda s: s.update(unix_sockets=["/var/../docker.sock"]), "规范绝对路径"),
            "unix_sockets 含 .": (lambda s: s.update(unix_sockets=["/var/./docker.sock"]), "规范绝对路径"),
            "unix_sockets 含空分量": (lambda s: s.update(unix_sockets=["/var//docker.sock"]), "规范绝对路径"),
            "unix_sockets 含反斜杠": (lambda s: s.update(unix_sockets=["/var\\docker.sock"]), "规范绝对路径"),
            "unix_sockets 是别名路径": (lambda s: s.update(unix_sockets=["{WORK}/docker.sock"]), "规范绝对路径"),
            "unix_sockets 是抽象套接字": (lambda s: s.update(unix_sockets=["@docker"]), "规范绝对路径"),
            "unix_sockets 重复": (lambda s: s.update(unix_sockets=["/a.sock", "/a.sock"]), "重复"),
            "requires 为空": (lambda s: s.update(requires={}), "至少要有"),
            "requires 未知键": (lambda s: s.update(requires={"other": 1}), "字段集合是闭合的"),
            "requires 环境变量名非法": (lambda s: s.update(requires={"env_equals": {"bad name": "x"}}), "env_equals"),
            "requires 参数含空串": (lambda s: s.update(requires={"argv_contains": [""]}), "不得为空串"),
            "requires 的 go_test 参数含空串": (lambda s: s.update(requires={"go_test_argv_contains": [""]}), "不得为空串"),
            "requires 的 go_test 参数为空数组": (lambda s: s.update(requires={"go_test_argv_contains": []}), "不得为空"),
            "requires 的 go_test 参数重复": (lambda s: s.update(requires={"go_test_argv_contains": ["-count=1", "-count=1"]}), "重复"),
            "既无 range 也无 file": (lambda s: (s["repo"].update(ranges=[], files=[]), s.update(contracts=[], outside=[])), "至少要声明"),
            "元组冒充数组": (lambda s: s["repo"].update(files=("VERSION",)), "字符串数组"),
        }
        for label, (change, fragment) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(mutate(change))
            self.assertIn(fragment, str(caught.exception), label)
        for label, bad in {"不是对象": ["x"], "None": None}.items():
            with self.subTest(label), self.assertRaises(ValueError):
                ur.validate_declared_scope(bad)
        with self.assertRaises(ValueError, msg="toolchain:pnpm 同样要求列出锁文件"):
            scope = frontend_scope()
            scope["repo"]["files"] = ["Makefile"]
            ur.validate_declared_scope(scope)

    def test_repo_ignored_accepts_directory_prefixes_and_filename_globs(self) -> None:
        """repo_ignored 的每一项：以 / 结尾的目录前缀（语义不变），或仓库相对 glob（只支持 * 与 ?，不跨 /）。"""

        def with_ignored(*items) -> dict:
            scope = make_scope()
            scope["repo"]["repo_ignored"] = list(items)
            return scope

        good = ["backend/ent/schema/.entc/", "frontend/vitest.config.ts.timestamp-*", "out/*.tmp", "backend/a?c.bin", "backend/bin/tool",
                "frontend/*/cache-?", "*.log"]
        self.assertEqual(ur.validate_declared_scope(with_ignored(*good))["repo"]["repo_ignored"], good)
        cases = {
            "glob 含 ..": ("frontend/../x-*", "不得含空、. 或 .. 分量"), "glob 含 .. 分量在后": ("frontend/x-*/..", "不得含空、. 或 .. 分量"),
            "glob 含 **": ("frontend/**/x", "不支持 **"), "glob 末尾 **": ("frontend/x**", "不支持 **"), "glob 含三个星号": ("frontend/***", "不支持 **"),
            "绝对路径 glob": ("/frontend/x-*", "不以 / 开头"), "含字符类 [": ("frontend/[ab].js", "只支持 * 与 ?"), "含字符类 ]": ("frontend/a].js", "只支持 * 与 ?"),
            "空串": ("", "非空"), "含空分量": ("frontend//x-*", "不得含空、. 或 .. 分量"), "含 . 分量": ("frontend/./x-*", "不得含空、. 或 .. 分量"),
            "含反斜杠": ("frontend\\x-*", "反斜杠"), "含换行": ("frontend/x\n*", "控制字符"),
            "目录前缀含 *": ("frontend/tmp-*/", "目录前缀，不支持通配符"), "目录前缀含 ?": ("frontend/tmp-?/", "目录前缀，不支持通配符"),
            "目录前缀含 ..": ("frontend/../tmp/", "不得含空、. 或 .. 分量"), "目录前缀是绝对路径": ("/tmp/", "不以 / 开头"),
            "只有一个 /": ("/", "不以 / 开头"), "不是字符串": (7, "字符串数组"),
        }
        for label, (item, fragment) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(with_ignored(item))
            self.assertIn(fragment, str(caught.exception), label)
        with self.assertRaises(ValueError):
            ur.validate_declared_scope(with_ignored("out/*.tmp", "out/*.tmp"))

    def test_installed_dirs_validation(self) -> None:
        """repo.installed_dirs：依赖安装目录（pnpm 的 node_modules 是未跟踪路径），内容由 cache:* 合同绑定。"""

        base = frontend_scope()
        self.assertEqual(ur.validate_declared_scope(base)["repo"]["installed_dirs"], [{"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}])
        twice = frontend_scope()
        twice["repo"]["installed_dirs"].append({"prefix": "tools/node_modules/", "contract": "cache:node-modules"})
        self.assertEqual(len(ur.validate_declared_scope(twice)["repo"]["installed_dirs"]), 2, "同一份合同可以绑定多个安装目录")
        for value in ([], None):
            scope = frontend_scope()
            if value is None:
                del scope["repo"]["installed_dirs"]
            else:
                scope["repo"]["installed_dirs"] = value
            ur.validate_declared_scope(scope)  # 缺省、空数组都合法

        def with_installed(*items, files=None, ranges=None, list_ok=None) -> dict:
            scope = frontend_scope()
            scope["repo"]["installed_dirs"] = list(items)
            if files is not None:
                scope["repo"]["files"] = files
            if ranges is not None:
                scope["repo"]["ranges"] = ranges
            if list_ok is not None:
                scope["repo"]["list_ok"] = list_ok
            return scope

        good = {"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}
        cases = {
            "不是数组": (with_installed(), "必须是数组", lambda s: s["repo"].update(installed_dirs="frontend/node_modules/")),
            "项不是对象": (with_installed("frontend/node_modules/"), "必须是对象", None),
            "缺 contract": (with_installed({"prefix": "frontend/node_modules/"}), "字段集合是闭合的", None),
            "缺 prefix": (with_installed({"contract": "cache:node-modules"}), "字段集合是闭合的", None),
            "多一个键": (with_installed({**good, "note": "x"}), "字段集合是闭合的", None),
            "合同不在 contracts 里": (with_installed({**good, "contract": "cache:other"}), "出现在 contracts 里", None),
            "合同不是 cache 类": (with_installed({**good, "contract": "toolchain:node"}), "cache:<名字>", None),
            "合同 ID 形式非法": (with_installed({**good, "contract": "node-modules"}), "类型:名字", None),
            "合同类型未知": (with_installed({**good, "contract": "dir:node-modules"}), "合同类型未知", None),
            "前缀不以 / 结尾": (with_installed({**good, "prefix": "frontend/node_modules"}), "以 / 结尾的目录前缀", None),
            "前缀是绝对路径": (with_installed({**good, "prefix": "/frontend/node_modules/"}), "不以 / 开头", None),
            "前缀含 ..": (with_installed({**good, "prefix": "frontend/../node_modules/"}), "不得含空、. 或 .. 分量", None),
            "前缀含空分量": (with_installed({**good, "prefix": "frontend//node_modules/"}), "不得含空、. 或 .. 分量", None),
            "前缀为空串": (with_installed({**good, "prefix": ""}), "非空", None),
            "前缀不是字符串": (with_installed({**good, "prefix": ["frontend/node_modules/"]}), "非空", None),
            "前缀重复": (with_installed(good, dict(good)), "互不重复", None),
            "前缀等于 range 的 include": (with_installed({**good, "prefix": "frontend/"}), "不得等于或包含已声明的", None),
            "前缀盖住 range": (with_installed({**good, "prefix": "frontend/"}, ranges=[{"name": "src", "include": ["frontend/src/"], "exclude": []}],
                                              files=["frontend/pnpm-lock.yaml", "frontend/package.json"]), "不得等于或包含已声明的", None),
            "前缀盖住单文件": (with_installed({**good, "prefix": "frontend/"}, ranges=[{"name": "b", "include": ["backend/"], "exclude": []}]),
                               "不得等于或包含已声明的", None),
            "前缀盖住 list_ok": (with_installed({**good, "prefix": "tools/"}, list_ok=["tools/"]), "不得等于或包含已声明的", None),
        }
        for label, (scope, fragment, change) in cases.items():
            if change is not None:
                change(scope)
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(scope)
            self.assertIn(fragment, str(caught.exception), label)

    def test_the_documented_class_table_matches_the_design(self) -> None:
        """class 表是代码常量（改它＝改验证器身份），这里钉住设计文档 3.2 的取值，改动必须有意为之。"""

        self.assertEqual(set(ur.DECLARED_CLASSES), {"toolchain", "dependency-content", "performance-cache", "scratch", "system", "virtual", "home-config"})
        self.assertNotIn("write", ur.DECLARED_CLASSES["dependency-content"], "依赖内容不许写：写就是在联网下载")
        self.assertNotIn("write", ur.DECLARED_CLASSES["toolchain"])
        self.assertIn("write", ur.DECLARED_CLASSES["performance-cache"])
        self.assertEqual(ur.DECLARED_CLASSES["home-config"], frozenset({"read", "stat", "missing"}))
        self.assertEqual(ur.DECLARED_TOLERATED_UNRESOLVED, ("symlink-metadata-not-bound:", "unfinished-calls-at-eof"),
                         "容忍集合：符号链接元数据未绑定（按前缀）、流结束时悬着的未配对调用（精确名字，另有数量上限）")
        self.assertEqual((ur.DECLARED_SCOPE_ENTRY, ur.DECLARED_COVERAGE_SCOPE), ("declared-inputs-scope", "declared-inputs/v1"))


# ---------------------------------------------------------------------------
# T2：输入展开
# ---------------------------------------------------------------------------


class ExpansionTests(DeclaredTestCase):
    def test_entries_names_categories_order_and_no_head(self) -> None:
        entries = self.world.inputs(contracts=[])
        names = [entry["name"] for entry in entries]
        self.assertEqual(names, ["declared-inputs-scope", "file:VERSION", "file:backend/go.mod", "file:backend/go.sum", "scope:backend", "scope:egress-docs"])
        self.assertEqual(names, sorted(names), "条目按名字排序，顺序稳定")
        self.assertNotIn("head", names, "声明级输入不含 HEAD")
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(by_name["declared-inputs-scope"]["category"], "policy")
        self.assertEqual({by_name[name]["category"] for name in names if name != "declared-inputs-scope"}, {"tests"})
        declaration = self.world.declaration()
        binding = {"scope": declaration["declared_scope"], "sample": declaration["declared_sample"]}
        self.assertEqual(by_name["declared-inputs-scope"]["sha256"], ur.sha256_json(binding))
        self.assertEqual(by_name["declared-inputs-scope"]["detail"], binding, "detail 带范围全文与采样引用")
        self.assertEqual(by_name["scope:backend"]["detail"]["exclude"], ["backend/bin/"])
        self.assertEqual(self.world.inputs(contracts=[]), entries, "同一声明展开两次完全相同")

    def test_the_scope_entry_digest_follows_scope_sample_and_command(self) -> None:
        base = {entry["name"]: entry["sha256"] for entry in self.world.inputs(contracts=[])}
        scope = make_scope()
        scope["repo"]["list_ok"] = []
        changed_scope = {entry["name"]: entry["sha256"] for entry in self.world.inputs(self.world.declaration(scope=scope), contracts=[])}
        self.assertNotEqual(changed_scope["declared-inputs-scope"], base["declared-inputs-scope"], "改范围使范围条目变")
        self.assertEqual({k: v for k, v in changed_scope.items() if k != "declared-inputs-scope"}, {k: v for k, v in base.items() if k != "declared-inputs-scope"})
        for key in ("evidence_sha256", "command_sha256", "sampler_sha256"):
            declaration = self.world.declaration()
            declaration["declared_sample"][key] = "7" * 64
            changed = {entry["name"]: entry["sha256"] for entry in self.world.inputs(declaration, contracts=[])}
            self.assertNotEqual(changed["declared-inputs-scope"], base["declared-inputs-scope"], f"改 {key} 使范围条目变（改证据、改命令、换采样器都使单元失效）")

    def test_an_unrelated_commit_keeps_the_digest_and_a_commit_inside_the_scope_changes_it(self) -> None:
        """方案 6.4 正例的记录层形态：改一个无关的 Claude 测试文件并提交——HEAD 与 repo:tests 都变了，但声明级输入摘要不变；
        改声明范围内的 backend 文件，范围条目才变。旧式整仓库声明在同样改动下整体失效。"""

        world = self.world
        legacy = {"ranges": [ur.standard_ranges("pkg")[key] for key in ur.COMMAND_RANGES], "head": True}
        before_declared = ur.entries_sha256(world.inputs())
        before_legacy = ur.entries_sha256(ur.declared_inputs(world.repo, legacy))

        def commit_and_reload(files: dict) -> ur.RepoIndex:
            _write(world.root, files)
            _commit(world.root, "改动")
            return ur.RepoIndex.load(world.root)

        repo = commit_and_reload({"pkg/tests/test_claude_unrelated.py": WORLD_FILES["pkg/tests/test_claude_unrelated.py"] + "# 追加注释\n"})
        world.repo = repo
        self.assertEqual(ur.entries_sha256(world.inputs()), before_declared, "无关测试文件改动不改声明级输入摘要")
        self.assertNotEqual(ur.entries_sha256(ur.declared_inputs(repo, legacy)), before_legacy, "旧式整仓库声明同样改动下整体失效（head、repo:tests）")
        for relative, changed in {"docs/other.md": "docs 里与 egress 无关的文档\n", "Makefile": "FRONTEND_CRITICAL_VITEST = b a\n",
                                  "frontend/src/x.ts": "export const x = 2\n"}.items():
            world.repo = commit_and_reload({relative: changed})
            self.assertEqual(ur.entries_sha256(world.inputs()), before_declared, f"{relative} 不在声明范围里（Makefile 由展开合同覆盖，不在此处）")
        for relative in ("backend/a.go", "docs/egress/foundation/sink-stats.md", "backend/go.sum", "VERSION"):
            world.repo = commit_and_reload({relative: "改了\n"})
            changed = ur.entries_sha256(world.inputs())
            self.assertNotEqual(changed, before_declared, f"{relative} 在声明范围里，改动必须使输入摘要变")
            before_declared = changed

    def test_declaration_must_be_closed_and_legacy_declarations_are_untouched(self) -> None:
        world = self.world
        with self.assertRaises(ur.RecordsError):
            ur.declared_inputs(world.repo, {"declared_scope": make_scope()})
        for extra in ({"head": True}, {"ranges": []}, {"resolved": []}):
            with self.subTest(extra), self.assertRaises(ur.RecordsError):
                ur.declared_inputs(world.repo, {**world.declaration(), **extra})
        bad_scope = make_scope()
        bad_scope["network"] = "any"
        with self.assertRaises(ur.RecordsError) as caught:
            ur.declared_inputs(world.repo, world.declaration(scope=bad_scope))
        self.assertIn("声明级输入声明非法", str(caught.exception))
        bad_sample = world.declaration()
        bad_sample["declared_sample"]["evidence_sha256"] = "XYZ"
        with self.assertRaises(ur.RecordsError):
            ur.declared_inputs(world.repo, bad_sample)
        # 声明里带无法编码成 UTF-8 的字符（孤立代理字符，例如 JSON 里写的 \udcff）：算摘要时 UnicodeEncodeError，要按「声明非法」报，
        # 不能让别的异常类型漏出去。
        surrogate = make_scope()
        surrogate["repo"]["repo_ignored"] = ["backend/bin/\udcff/"]
        with self.assertRaises(ur.RecordsError) as caught:
            ur.declared_inputs(world.repo, world.declaration(scope=surrogate))
        self.assertIn("声明级输入声明非法", str(caught.exception))
        legacy = ur.declared_inputs(world.repo, {"head": True, "files": [{"category": "managed", "path": "Makefile"}]})
        self.assertEqual([entry["name"] for entry in legacy], ["file:Makefile", "head"], "没有 declared_scope 键，走原逻辑")

    def test_every_single_point_malformation_of_a_declaration_is_valid_or_a_records_error(self) -> None:
        """穷举式单点畸形（见 single_point_variants）：声明字典里任何位置变畸形（含孤立代理字符），declared_inputs 只允许成功或抛
        RecordsError——登记表加载时按它报错，别的异常类型会让整张表的加载崩掉。"""

        checked = 0
        for place, declaration in single_point_variants(self.world.declaration(), extra=("\udcff",)):
            try:
                ur.declared_inputs(self.world.repo, declaration)
            except ur.RecordsError:
                pass
            except Exception as error:  # noqa: BLE001 - 目标就是任何异常
                self.fail(f"声明位置 {place} 的畸形值让 declared_inputs 抛了 {type(error).__name__}：{error}")
            checked += 1
        self.assertGreater(checked, 800, "穷举的规模不应悄悄缩水")


# ---------------------------------------------------------------------------
# T3：覆盖检查 C1～C7
# ---------------------------------------------------------------------------


class CoverageTests(unittest.TestCase):
    def cover(self, sample: dict | None = None, scope: dict | None = None, **kwargs) -> dict:
        return ur.declared_coverage(sample if sample is not None else make_sample(), scope if scope is not None else make_scope(), **kwargs)

    def sample_with(self, *, extra_repo=(), extra_outside=(), repo=None, outside=None, git=None, network=None, coverage=None, aliases=None,
                    exit_code=None) -> dict:
        sample = make_sample()
        if repo is not None:
            sample["repo_accesses"] = copy.deepcopy(repo)
        if outside is not None:
            sample["outside_accesses"] = copy.deepcopy(outside)
        sample["repo_accesses"] += copy.deepcopy(list(extra_repo))
        sample["outside_accesses"] += copy.deepcopy(list(extra_outside))
        if git is not None:
            sample["git_commands"] = git
        if network is not None:
            sample["network"] = network
        if coverage is not None:
            sample["coverage"].update(coverage)
        if aliases is not None:
            sample["aliases"] = aliases
        if exit_code is not None:
            sample["exit_code"] = exit_code
        return sample

    def expect(self, rule: str, sample: dict | None = None, scope: dict | None = None, label: str = "", **kwargs) -> dict:
        result = self.cover(sample, scope, **kwargs)
        self.assertFalse(result["ok"], f"{label or rule} 应当判未覆盖")
        self.assertIn(rule, {item["rule"] for item in result["undeclared"]}, f"{label or rule}：{result['undeclared']}")
        return result

    def test_the_baseline_sample_is_fully_covered(self) -> None:
        result = self.cover()
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertEqual(result["undeclared"], [])
        counts = result["counts"]
        self.assertEqual((counts["repo_paths"], counts["outside_paths"], counts["undeclared"]), (12, 12, 0))
        self.assertEqual(counts["classes"], {"toolchain": 2, "dependency-content": 1, "performance-cache": 1, "scratch": 2, "system": 2, "virtual": 1,
                                             "home-config": 1})
        self.assertEqual((counts["ignored_paths"], counts["git_paths"], counts["git_commands"], counts["network"]), (1, 1, 1, 2))
        self.assertEqual(counts["exempt_paths"], 5, "docs（祖先目录）、tools（list_ok）、go.work（树根探测）、/usr/local（祖先）、祖先目录里的 go.work 探测")

    def test_c1_repo_paths(self) -> None:
        sample = self.sample_with
        self.expect("C1-undeclared", sample(extra_repo=[["{TREE}/frontend/src/x.ts", ["read"], True]]), label="声明范围外的读取")
        self.expect("C1-undeclared", sample(extra_repo=[["{TREE}/docs/other.md", ["read"], True]]), label="docs 里不在 egress 下的文件")
        self.expect("C1-untracked-read", sample(extra_repo=[["{TREE}/backend/untracked.go", ["read"], False]]), label="读了范围内的未跟踪文件")
        self.expect("C1-untracked-read", sample(extra_repo=[["{TREE}/backend/script.sh", ["exec"], False]]), label="执行了范围内的未跟踪文件")
        self.expect("C4-write-declared", sample(extra_repo=[["{TREE}/backend/c.go", ["write"], True]]), label="写入范围内的跟踪文件")
        self.expect("C4-write-declared", sample(extra_repo=[["{TREE}/backend/new.go", ["write"], False]]), label="范围内新建未跟踪文件也是写入范围")
        self.expect("C1-ignored-tracked", sample(extra_repo=[["{TREE}/backend/bin/x", ["read"], True]]), label="输出区里出现已跟踪文件")
        self.expect("C1-git-kinds", sample(extra_repo=[["{TREE}/.git/index", ["write"], False]]), label="写 .git")
        self.expect("C1-git-kinds", sample(extra_repo=[["{TREE}/.git/hooks/pre-commit", ["exec"], False]]), label="执行 .git 下的文件")
        no_history = make_scope()
        del no_history["repo"]["git_history"]
        self.expect("C1-git-undeclared", sample(git=[]), no_history, "读了 .git 但没有登记 git_history")
        self.expect("C1-list-undeclared", sample(extra_repo=[["{TREE}/frontend", ["list"], False]]), label="枚举了范围外的目录")
        self.expect("C1-list-undeclared", sample(repo=[["{TREE}/tools", ["list", "read"], False]]), label="list_ok 目录只放行纯枚举，不放行读取")
        self.expect("C1-undeclared", sample(extra_repo=[["{TREE}", ["list"], False]]), label="枚举测试树根")
        self.expect("C1-undeclared", sample(extra_repo=[["{TREE}/frontend", ["read"], False]]), label="祖先豁免只给 stat／dir，不给读取")
        no_make = make_scope()
        del no_make["make_expansion"]
        self.expect("C1-undeclared", sample(), no_make, "没有 make 展开合同时 Makefile 不在范围里")
        for label, path in {"缺 {TREE}": "backend/x.go", "含 ..": "{TREE}/backend/../x", "尾部斜杠": "{TREE}/backend/", "含空分量": "{TREE}/backend//x",
                            "含反斜杠": "{TREE}/backend\\x"}.items():
            self.expect("C1-path", sample(extra_repo=[[path, ["read"], True]]), label=f"路径不规范：{label}")
        self.expect("C0-kind", sample(extra_repo=[["{TREE}/backend/odd.go", ["frobnicate"], True]]), label="未知访问类型")
        # 正例：声明内容的上级目录只看元数据、范围内的 missing、输出区读写、树根 stat。
        self.assertTrue(self.cover(sample(extra_repo=[["{TREE}/backend/internal", ["dir", "stat"], False], ["{TREE}/docs/egress", ["dir"], False],
                                                      ["{TREE}/backend/nope.go", ["missing"], False], ["{TREE}", ["stat", "dir"], False],
                                                      ["{TREE}/backend/bin/y", ["read", "write"], False]]))["ok"])

    def test_c2_outside_paths_and_class_kinds(self) -> None:
        sample = self.sample_with
        self.expect("C2-outside-undeclared", sample(extra_outside=[["/opt/random/", ["read"], 1]]), label="没有前缀")
        self.expect("C2-outside-undeclared", sample(extra_outside=[["{HOME}/.npmrc", ["read"], 1]]), label="HOME 下没登记的配置")
        self.expect("C2-outside-undeclared", sample(extra_outside=[["/usr", ["read"], 1]]), label="祖先目录的豁免只给 stat／dir")
        self.expect("C4-write-class", sample(extra_outside=[["{WORK}/go-mod-cache/x/", ["write"], 1]]), label="写依赖内容＝联网下载")
        self.expect("C4-write-class", sample(extra_outside=[["/usr/local/go/bin/", ["write"], 1]]), label="写工具链")
        self.expect("C4-write-class", sample(extra_outside=[["/etc/passwd", ["write"], 1]]), label="写系统配置")
        self.expect("C4-write-class", sample(outside=[["{HOME}/.gitconfig", ["write"], 1]]), label="写 HOME 配置")
        self.expect("C2-kinds", sample(extra_outside=[["/proc/x/", ["exec"], 1]]), label="虚拟文件系统不许执行")
        self.expect("C2-kinds", sample(outside=[["{HOME}/.gitconfig", ["read", "list"], 1]]), label="home-config 不许枚举")
        # 最长前缀优先：/usr/lib/secret/ 比 /usr/lib/ 更具体，class 是 virtual（不许执行）。
        self.expect("C2-kinds", sample(extra_outside=[["/usr/lib/secret/key", ["exec"], 1]]), label="最长前缀的 class 生效")
        self.assertTrue(self.cover(sample(extra_outside=[["/usr/lib/other/lib.so", ["exec", "read"], 1], ["/usr/lib/secret/key", ["read"], 1]]))["ok"])
        for label, path in {"相对路径": "relative/x", "含 ..": "/a/../b", "{TREE} 路径": "{TREE}/x", "含空分量": "/a//b", "未登记别名": "{FOO}/x"}.items():
            self.expect("C2-path", sample(extra_outside=[[path, ["read"], 1]]), label=f"仓库外路径不规范：{label}")
        # 正例：祖先目录 stat／dir、目录自身、根目录 stat、精确文件前缀。
        self.assertTrue(self.cover(sample(extra_outside=[["/usr", ["stat", "dir"], 1], ["/", ["stat"], 1], ["/usr/local/go", ["stat", "dir"], 1],
                                                         ["{WORK}", ["stat"], 1], ["/var/run/docker.sock", ["read"], 1]]))["ok"])

    def test_c3_missing_probes(self) -> None:
        sample = self.sample_with
        self.expect("C3-missing-undeclared", sample(extra_repo=[["{TREE}/other/go.mod", ["missing"], False]]), label="范围外的 missing 探测")
        self.expect("C3-missing-undeclared", sample(extra_repo=[["{TREE}/surprise", ["missing"], False]]), label="树根下不在探测名单里的名字")
        # WP1c 起，系统目录（/usr、/var、/home 等）与别名根之下的 missing 探测按「噪声探测」放行（见 test_ci_declared_inputs_probe_rules），
        # 下面几条改用不在放行根、也不在别名根之下的路径，继续钉住「其余仓库外 missing 探测仍要有声明前缀」。
        self.expect("C3-missing-undeclared", sample(extra_outside=[["/data/x/go.work", ["missing"], 1]]), label="不是测试树祖先的目录里的探测")
        self.assertTrue(self.cover(sample(extra_outside=[["/var/tmp/gate/tree/sub/go.mod", ["missing"], 1]]))["ok"],
                        "测试树内部的 missing 探测：位于证据别名 TREE 之下，按噪声探测放行（WP1c 之前判违规）")
        far = {"TREE": "/data/gate/tree", "WORK": "/data/gate/work", "HOME": "/root", "TMP": "/tmp"}
        self.expect("C3-missing-undeclared", sample(extra_outside=[["/data/gate/other", ["missing"], 1]], aliases=far), label="祖先目录里的非探测名")
        self.expect("C3-missing-undeclared", sample(extra_outside=[["/data/go.work", ["missing"], 1]], aliases={"TREE": "relative/tree"}),
                    label="证据没给出可信的测试树绝对路径时不豁免")
        for name in sorted(ur._DECLARED_ANCESTOR_PROBES):
            self.assertTrue(self.cover(sample(repo=[[f"{{TREE}}/{name}", ["missing"], False]],
                                              outside=[[f"/var/tmp/{name}", ["missing"], 1]]))["ok"], name)
        self.assertTrue(self.cover(sample(extra_repo=[["{TREE}/backend/go.work", ["missing"], False], ["{TREE}/docs/egress/nothing.json", ["missing"], False]]))["ok"],
                        "范围内的 missing 探测被范围覆盖")

    def test_c5_network_and_registered_unix_sockets(self) -> None:
        """缺口 1：AF_UNIX 目标必须列在 scope.unix_sockets 里（network 仍是 loopback-only 字符串）；回环地址照旧放行。"""

        sample = self.sample_with
        ok = lambda network, scope=None: self.cover(sample(network=network), scope)["ok"]  # noqa: E731
        for label, item in {"外连公网 IPv4": {"family": "AF_INET", "target": "93.184.216.34:443"}, "外连公网 IPv6": {"family": "AF_INET6", "target": "[2606:4700::1]:443"},
                            "解析不出": {"family": "AF_INET", "target": "localhost:80"}, "没登记的 unix 套接字": {"family": "AF_UNIX", "target": "/run/other.sock"},
                            "抽象 unix 套接字": {"family": "AF_UNIX", "target": "@abstract"}, "unix 路径是别名写法": {"family": "AF_UNIX", "target": "{WORK}/x.sock"},
                            "netlink": {"family": "AF_NETLINK", "target": ""}, "采样器认不出的目标": {"family": "unparsed", "target": "0xef08dd1fe598"},
                            "0.0.0.0": {"family": "AF_INET", "target": "0.0.0.0:80"}, "AF_UNSPEC 带了目标": {"family": "AF_UNSPEC", "target": "x"}}.items():
            result = self.cover(sample(network=[item]))
            self.assertFalse(result["ok"], label)
            self.assertTrue({"C5-network", "C5-unix-undeclared"} & {entry["rule"] for entry in result["undeclared"]}, label)
        for label, item in {"回环 IPv4": {"family": "AF_INET", "target": "127.0.0.1:5432"}, "回环 IPv4 不带端口": {"family": "AF_INET", "target": "127.8.8.8"},
                            "回环 IPv6": {"family": "AF_INET6", "target": "[::1]:80"}, "回环 IPv6 不带端口": {"family": "AF_INET6", "target": "::1"},
                            "映射到回环的 IPv6": {"family": "AF_INET6", "target": "[::ffff:127.0.0.1]:80"},
                            "登记的 docker.sock": {"family": "AF_UNIX", "target": "/var/run/docker.sock"},
                            "UDP 套接字断开关联（AF_UNSPEC，不是外连）": {"family": "AF_UNSPEC", "target": ""}}.items():
            self.assertTrue(ok([item]), label)
        self.assertTrue(ok([]))
        # 登记是按 scope.unix_sockets 逐路径精确匹配：没登记就违规，登记了就通过；outside 里有同名前缀不等于登记。
        bare = make_scope()
        bare["unix_sockets"] = []
        self.expect("C5-unix-undeclared", sample(), bare, "unix_sockets 为空时 docker.sock 违规（outside 里有它的前缀也不算登记）")
        del bare["unix_sockets"]
        self.expect("C5-unix-undeclared", sample(), bare, "缺省 unix_sockets 等于空")
        wider = make_scope()
        wider["unix_sockets"] = ["/var/run/docker.sock", "/run/other.sock"]
        self.assertTrue(ok([{"family": "AF_UNIX", "target": "/run/other.sock"}, {"family": "AF_UNIX", "target": "/var/run/docker.sock"}], wider))
        self.expect("C5-unix-undeclared", sample(network=[{"family": "AF_UNIX", "target": "/var/run/docker.sock.bak"}]), label="只精确匹配，不按前缀")

    def test_c6_git_commands(self) -> None:
        sample = self.sample_with
        self.expect("C6-commit-undeclared", sample(git=[["git", "show", f"{COMMIT_B}:docs/egress/x.json"]]), label="引用了没登记的提交")
        self.expect("C6-commit-undeclared", sample(git=[["git", "merge-base", "--is-ancestor", COMMIT_A, COMMIT_B]]), label="只有一端登记")
        self.expect("C6-commit-undeclared", sample(git=[["/usr/bin/git", "cat-file", "-e", f"{COMMIT_B.upper()}^{{tree}}"]]), label="大写提交号照样识别")
        self.expect("C6-head", sample(git=[["git", "rev-parse", "HEAD"]]), label="引用 HEAD")
        self.expect("C6-head", sample(git=[["git", "diff", f"{COMMIT_A}..HEAD"]]), label="范围写法里的 HEAD")
        self.expect("C6-head", sample(git=[["git", "show", "ORIG_HEAD:x"]]), label="ORIG_HEAD")
        self.expect("C6-git", sample(git=[["curl", "http://x"]]), label="混入非 git 命令")
        self.expect("C6-git-argv-hidden", sample(git=[["git", "<argv-not-visible>"]]), label="采样器没解码到 git 的 argv（哨兵）：读了哪个提交说不清")
        self.expect("C6-git-argv-hidden", sample(git=[["git", "show", f"{COMMIT_A}:x", "<argv-not-visible>"]]), label="哨兵在任何位置都违规")
        self.assertTrue(self.cover(sample(git=[["git", "show", f"{COMMIT_A}:a.json"], ["git", "merge-base", "--is-ancestor", COMMIT_A, COMMIT_A],
                                               ["/usr/bin/git", "cat-file", "-e", f"{COMMIT_A}^{{tree}}"], ["git", "log", "origin/HEAD", "--format=%H"],
                                               ["git", "ls-tree", "-z", COMMIT_A, "--", ".git/HEAD"], ["git", "init"]]))["ok"],
                        "已登记的提交、origin/HEAD、.git/HEAD 路径、不带提交号的 git init 都不违规")

    def test_c7_evidence_integrity(self) -> None:
        sample = self.sample_with
        self.expect("C7-stream", sample(coverage={"stream_complete": False}), label="输出流没结束")
        self.expect("C7-unresolved", sample(coverage={"unresolved": 1, "unresolved_reasons": {"unsupported-call:ptrace": 1}}), label="不可容忍的原因")
        self.expect("C7-unresolved", sample(coverage={"unresolved": 2, "unresolved_reasons": {"symlink-metadata-not-bound:lstat": 1}}), label="总数与分项不符")
        self.expect("C7-unresolved", sample(coverage={"unresolved": 3, "unresolved_reasons": {"symlink-metadata-not-bound:lstat": 1}, "unfinished_calls": 2}),
                    label="有未配对的调用")
        self.expect("C7-unresolved", sample(coverage={"unresolved": 2, "unresolved_reasons": {"symlink-metadata-not-bound:lstat": 1},
                                                      "unresolved_process_events": 1}), label="有未归属的进程事件")
        self.expect("C7-lines", sample(coverage={"lines": 0}), label="没有输出行")
        self.expect("C7-exit", sample(exit_code=1), label="退出码非零")
        timed_out = make_sample()
        timed_out["exit_code"] = None
        self.expect("C7-exit", timed_out, label="采样超时被终止时采样器写 null：格式合法，但不是 0")
        for label, change in {"valid 为假": {"valid": False, "invalid_reasons": ["[exit] cold 退出码 1，不是 0"]},
                              "只有 valid 为假": {"valid": False}, "只有 invalid_reasons 非空": {"invalid_reasons": ["[network] 非本机地址"]}}.items():
            result = self.expect("C7-invalid", {**make_sample(), **change}, label=f"采样器自己判无效（{label}）：验证侧不得采信")
            self.assertTrue(result["undeclared"][0]["reason"].startswith("采样器自己判这份采样无效"))
        self.assertTrue(self.cover({**make_sample(), "valid": True, "invalid_reasons": [], "review": {"anything": ["不解释"]}})["ok"],
                        "采样器写出的 valid／invalid_reasons／review 三个键可以出现；valid 为真时不影响覆盖结论")
        self.expect("C7-command", None, None, "命令摘要与登记的不符", command_sha256="0" * 64)
        self.assertTrue(self.cover(command_sha256=make_sample()["command"]["sha256"])["ok"], "登记的命令摘要一致时通过")
        good = sample(coverage={"unresolved": 3, "unresolved_reasons": {"symlink-metadata-not-bound:lstat": 2, "symlink-metadata-not-bound:newfstatat": 1}})
        self.assertTrue(self.cover(good)["ok"], "符号链接元数据未绑定是容忍的原因，只增加计数")
        no_marker = make_sample()
        del no_marker["coverage"]["detached_markers"]
        self.assertTrue(self.cover(no_marker)["ok"], "detached_markers 可有可无")
        self.assertTrue(self.cover(sample(coverage={"detached_markers": 0}))["ok"])

    def test_malformed_evidence_or_scope_is_a_finding_not_an_exception(self) -> None:
        broken = {
            "缺字段": lambda s: s.pop("network"), "多字段": lambda s: s.update(extra=1), "schema 不对": lambda s: s.update(schema_version="declared-sample/v0"),
            "unit_id 为空": lambda s: s.update(unit_id=""), "命令摘要与内容不符": lambda s: s["command"].update(sha256="0" * 64),
            "argv 为空": lambda s: s["command"].update(argv=[]), "cwd_rel 非法": lambda s: s["command"].update(cwd_rel="/abs"),
            "coverage 多键": lambda s: s["coverage"].update(extra=1), "coverage 缺键": lambda s: s["coverage"].pop("lines"),
            "detached_markers 为负": lambda s: s["coverage"].update(detached_markers=-1),
            "detached_markers 是布尔": lambda s: s["coverage"].update(detached_markers=True),
            "stream_complete 不是布尔": lambda s: s["coverage"].update(stream_complete=1),
            "reasons 计数为零": lambda s: s["coverage"].update(unresolved_reasons={"x": 0}),
            "仓库内行长度不对": lambda s: s["repo_accesses"].append(["{TREE}/a", ["read"]]),
            "仓库内 tracked 不是布尔": lambda s: s["repo_accesses"].append(["{TREE}/a", ["read"], 1]),
            "仓库内路径重复": lambda s: s["repo_accesses"].append(list(s["repo_accesses"][0])),
            "仓库外计数为负": lambda s: s["outside_accesses"].append(["/x", ["read"], -1]),
            "访问类型为空": lambda s: s["repo_accesses"].append(["{TREE}/a", [], True]),
            "git 命令为空": lambda s: s["git_commands"].append([]), "network 项多键": lambda s: s["network"].append({"family": "AF_INET", "target": "x", "e": 1}),
            "exit_code 是布尔": lambda s: s.update(exit_code=True),
        }
        for label, change in broken.items():
            sample = make_sample()
            change(sample)
            with self.subTest(label):
                result = self.cover(sample)
                self.assertFalse(result["ok"])
                self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-format"], result["undeclared"])
        bad_scope = make_scope()
        bad_scope["network"] = "any"
        result = self.cover(scope=bad_scope)
        self.assertEqual([item["rule"] for item in result["undeclared"]], ["C0-scope"])
        self.assertFalse(self.cover({}, make_scope())["ok"])

    def test_every_single_point_malformation_gives_findings_never_an_exception(self) -> None:
        """穷举式单点畸形（见 single_point_variants）：证据或范围里任何一个位置变成任何一种畸形值（含不可哈希的 []／{}）或被删除，
        declared_coverage 都返回结构完整的结果，validate_declared_scope 只抛 ValueError。这条合同对调用方很重要：声明范围与证据来自
        封存记录，畸形输入必须落成“不可承接”的问题，而不是让核验进程崩掉。"""

        checked = 0
        for scope_factory, sample_factory in ((make_scope, make_sample), (frontend_scope, frontend_sample)):
            base_scope, base_sample = scope_factory(), sample_factory()
            for place, sample in single_point_variants(base_sample, extra=("\udcff",)):
                try:
                    result = ur.declared_coverage(sample, base_scope)
                except Exception as error:  # noqa: BLE001 - 目标就是任何异常
                    self.fail(f"证据位置 {place} 的畸形值让覆盖检查抛了 {type(error).__name__}：{error}")
                self.assertEqual(set(result), {"ok", "undeclared", "counts"}, place)
                self.assertEqual(result["ok"], not result["undeclared"], place)
                checked += 1
            for place, scope in single_point_variants(base_scope, extra=("\udcff",)):
                try:
                    result = ur.declared_coverage(base_sample, scope)
                except Exception as error:  # noqa: BLE001
                    self.fail(f"范围位置 {place} 的畸形值让覆盖检查抛了 {type(error).__name__}：{error}")
                self.assertEqual(result["ok"], not result["undeclared"], place)
                try:
                    ur.validate_declared_scope(scope)
                except ValueError:
                    pass  # 对非法范围的约定异常
                except Exception as error:  # noqa: BLE001
                    self.fail(f"范围位置 {place} 的畸形值让校验抛了 {type(error).__name__}：{error}")
                checked += 1
        self.assertGreater(checked, 6000, "穷举的规模不应悄悄缩水")

    def test_findings_are_sorted_deterministic_and_capped(self) -> None:
        rows = [[f"{{TREE}}/frontend/f{index:04d}.ts", ["read"], True] for index in range(500)]
        result = self.cover(self.sample_with(extra_repo=rows))
        self.assertEqual(len(result["undeclared"]), 200)
        self.assertEqual(result["counts"]["undeclared"], 500)
        self.assertEqual(result["undeclared"], sorted(result["undeclared"], key=lambda item: (item["rule"], item["path"], item["reason"])))
        self.assertEqual(result, self.cover(self.sample_with(extra_repo=list(reversed(rows)))), "与证据里的行顺序无关")
        self.assertEqual(set(result["undeclared"][0]), {"rule", "path", "kinds", "reason"})

    def test_lone_surrogates_from_non_utf8_paths_never_reach_a_report_unescaped(self) -> None:
        """strace 里的非 UTF-8 路径经 surrogateescape 读入后是孤立代理字符；报告里换成转义，保证能按 UTF-8 落盘。"""

        weird = "{TREE}/frontend/\udce4\udcb8.ts"
        result = self.cover(self.sample_with(extra_repo=[[weird, ["read"], True]]))
        self.assertFalse(result["ok"])
        shown = result["undeclared"][0]["path"]
        self.assertEqual(shown, "{TREE}/frontend/\\udce4\\udcb8.ts")
        json.dumps(result, ensure_ascii=False).encode("utf-8")  # 不抛 UnicodeEncodeError
        # 访问类型名里的孤立代理字符（只可能来自被篡改的证据）同样要转义：未知类型 C0-kind 的明细会把类型名原样带进报告。
        odd_kind = self.cover(self.sample_with(extra_repo=[["{TREE}/backend/x.go", ["read", "\udcff"], True]]))
        self.assertEqual([item["rule"] for item in odd_kind["undeclared"]], ["C0-kind"])
        self.assertEqual(odd_kind["undeclared"][0]["kinds"], ["\\udcff", "read"])
        json.dumps(odd_kind, ensure_ascii=False).encode("utf-8")

    # ---- 缺口 2：repo_ignored 前缀下自产物读回 ----

    def test_c1_repo_ignored_allows_writes_and_reading_back_what_the_command_wrote(self) -> None:
        """repo_ignored 前缀（例如 ent 代码生成写进测试树的临时目录，运行结束即删、git status 零变化、不在 .gitignore 里）下：
        允许写；允许 read／exec／list **同一路径本次采样里也出现了 write** 的路径（命令自己写出再读回，不是外部输入）；只读不写
        的未跟踪路径仍然违规。"""

        scope = make_scope()
        scope["repo"]["repo_ignored"] = ["backend/ent/schema/.entc/"]
        entc = "{TREE}/backend/ent/schema/.entc"

        def check(*rows):
            return self.cover(self.sample_with(repo=[["{TREE}/backend/go.mod", ["read"], True], *rows], git=[]), scope)

        good = check([entc, ["write", "list", "stat", "dir"], False], [entc + "/gen-1/schema.go", ["write", "read"], False],
                     [entc + "/gen-1/main", ["write", "exec"], False], [entc + "/gen-1", ["write", "list"], False],
                     [entc + "/only-written", ["write"], False], [entc + "/probe", ["missing"], False], [entc + "/meta", ["stat", "dir"], False])
        self.assertTrue(good["ok"], good["undeclared"])
        self.assertEqual(good["counts"]["ignored_paths"], 7)
        for label, row in {"只读": [entc + "/stale.go", ["read"], False], "只执行": [entc + "/stale-bin", ["exec"], False],
                           "只枚举": [entc + "/stale-dir", ["list"], False], "读加枚举也不写": [entc + "/stale-both", ["read", "list", "stat"], False],
                           "目录自身只读不写": [entc, ["read"], False]}.items():
            result = check(row)
            self.assertFalse(result["ok"], label)
            self.assertEqual([item["rule"] for item in result["undeclared"]], ["C1-untracked-read"], f"{label}：{result['undeclared']}")
            self.assertEqual(result["undeclared"][0]["path"], row[0])
        self.assertEqual([item["rule"] for item in check([entc + "/tracked.go", ["write", "read"], True])["undeclared"]], ["C1-ignored-tracked"],
                         "已跟踪文件出现在输出区是登记错误，即使有写")
        # 边界：同一次采样里别的路径写过不算数；前缀之外的未跟踪读取仍按范围规则违规；没登记 repo_ignored 时写入同样违规。
        result = check([entc + "/a.go", ["write"], False], [entc + "/b.go", ["read"], False])
        self.assertEqual([item["path"] for item in result["undeclared"]], [entc + "/b.go"])
        outside_prefix = self.cover(self.sample_with(repo=[["{TREE}/backend/go.mod", ["read"], True], ["{TREE}/backend/ent/other.go", ["read", "write"], False]],
                                                     git=[]), scope)
        self.assertEqual([item["rule"] for item in outside_prefix["undeclared"]], ["C4-write-declared"], "前缀之外、范围之内的写入照旧违规")
        unregistered = self.cover(self.sample_with(repo=[["{TREE}/backend/go.mod", ["read"], True], [entc + "/a.go", ["write"], False]], git=[]))
        self.assertEqual([item["rule"] for item in unregistered["undeclared"]], ["C4-write-declared"])

    # ---- 安装目录：repo.installed_dirs ----

    def test_c1_installed_dirs_exempt_untracked_reads_under_a_contract_bound_prefix(self) -> None:
        """pnpm 的 node_modules 是仓库内未跟踪路径，一条前端命令能读几百个。内容靠 cache:node-modules 合同（lock-derived-pnpm）绑定，
        不靠范围摘要：installed_dirs 前缀下 read／exec／stat／dir／list／missing 不要求已跟踪；写入不允许，除非同时落在 repo_ignored 前缀
        内（node_modules/.vite/ 的 vitest 缓存）；前缀之外的未跟踪读取照旧违规。"""

        node = "{TREE}/frontend/node_modules"

        def check(*rows, scope=None, only=False):
            base = [] if only else [["{TREE}/frontend/src/x.ts", ["read"], True]]
            return ur.declared_coverage({**frontend_sample(), "repo_accesses": [*base, *rows]}, scope if scope is not None else frontend_scope())

        installed_rows = [[node, ["dir", "stat", "list"], False], [node + "/eslint/index.js", ["read", "stat"], False], [node + "/.bin/eslint", ["exec", "stat"], False],
                          [node + "/.modules.yaml", ["read"], False], [node + "/@types", ["stat", "dir", "list"], False], [node + "/vue/probe.json", ["missing"], False],
                          [node + "/eslint", ["read", "dir"], False]]
        good = check(*installed_rows)
        self.assertTrue(good["ok"], good["undeclared"])
        self.assertEqual(good["counts"]["installed_paths"], len(installed_rows))
        # 同样的访问，没有登记安装目录：范围不排除 node_modules 时，读和执行是未跟踪读取；范围排除它时是范围外的访问。
        bare = frontend_scope()
        del bare["repo"]["installed_dirs"]
        bare["repo"]["repo_ignored"] = []
        unbound = check(*installed_rows, scope=bare)
        self.assertEqual({item["rule"] for item in unbound["undeclared"]}, {"C1-untracked-read"}, unbound["undeclared"])
        self.assertEqual({item["path"] for item in unbound["undeclared"]},
                         {node + "/eslint/index.js", node + "/.bin/eslint", node + "/.modules.yaml", node + "/eslint"}, "只有 read／exec 的行违规；stat／dir／list／missing 本来就放行")
        excluded = frontend_scope()
        del excluded["repo"]["installed_dirs"]
        excluded["repo"]["repo_ignored"] = []
        excluded["repo"]["ranges"][0]["exclude"] = ["frontend/node_modules/"]
        outside_range = check(*installed_rows, scope=excluded)
        self.assertTrue({"C1-undeclared", "C1-list-undeclared", "C3-missing-undeclared"} <= {item["rule"] for item in outside_range["undeclared"]})
        # 写入：安装目录里不许写；落在 repo_ignored 前缀内（vitest 缓存）才允许，且那里只读也行（安装目录放行读取）。
        self.assertEqual([item["rule"] for item in check([node + "/eslint/cache.json", ["write"], False])["undeclared"]], ["C4-write-installed"])
        self.assertEqual([item["rule"] for item in check([node + "/eslint/cache.json", ["read", "write"], False])["undeclared"]], ["C4-write-installed"])
        self.assertTrue(check([node + "/.vite/results.json", ["write"], False], [node + "/.vite/old.json", ["read"], False],
                              [node + "/.vite/cache", ["read", "write", "list"], False])["ok"])
        # 已跟踪文件：在纯安装目录里无所谓（内容本来就在摘要里）；在输出区里仍是登记错误。
        self.assertTrue(check([node + "/tracked.js", ["read"], True])["ok"])
        self.assertEqual([item["rule"] for item in check([node + "/.vite/tracked.json", ["read"], True])["undeclared"]], ["C1-ignored-tracked"])
        # 前缀之外的未跟踪读取照旧违规：别的目录、名字相近但不是这个目录（前缀以 / 结尾）的路径。
        for label, row in {"范围内别的未跟踪目录": ["{TREE}/frontend/dist/bundle.js", ["read"], False],
                           "名字相近的目录": ["{TREE}/frontend/node_modules-extra/x.js", ["read"], False],
                           "名字相近的文件": ["{TREE}/frontend/node_modulesX", ["exec"], False]}.items():
            result = check(row)
            self.assertEqual([(item["rule"], item["path"]) for item in result["undeclared"]], [("C1-untracked-read", row[0])], label)
        # 安装目录的豁免不改变其它规则：.git 与范围外的路径不受影响。
        self.assertEqual([item["rule"] for item in check(["{TREE}/docs/other.md", ["read"], True])["undeclared"]], ["C1-undeclared"])
        self.assertEqual([item["rule"] for item in check(["{TREE}/.git/config", ["read"], False])["undeclared"]], ["C1-git-undeclared"])

    # ---- repo_ignored 的文件名 glob ----

    def test_c1_repo_ignored_filename_globs(self) -> None:
        """vite／vitest 会在 frontend/ 根下写、读回、再删一个随机名临时文件 vitest.config.ts.timestamp-<毫秒>-<哈希>.mjs（git status
        前后为空），按目录前缀穷举不了：repo_ignored 的项可以是 glob。匹配到的路径：允许写；read／list 仅限本次样本里同一路径也出现了
        write 的（自产物读回）；glob 只支持 * 与 ?，不跨 /，整条路径完全匹配。"""

        scope = frontend_scope()
        scope["repo"]["repo_ignored"] = ["frontend/vitest.config.ts.timestamp-*", "frontend/node_modules/.vite/", "frontend/a?c.tmp"]
        temp = "{TREE}/frontend/vitest.config.ts.timestamp-1699999999999-0a1b2c3d4e5f.mjs"

        def check(*rows):
            return ur.declared_coverage({**frontend_sample(), "repo_accesses": [["{TREE}/frontend/src/x.ts", ["read"], True], *rows]}, scope)

        good = check([temp, ["write", "read"], False], ["{TREE}/frontend/vitest.config.ts.timestamp-2-bb.mjs", ["write"], False],
                     ["{TREE}/frontend/vitest.config.ts.timestamp-3-cc.mjs", ["write", "exec"], False],
                     ["{TREE}/frontend/vitest.config.ts.timestamp-4-dd.mjs", ["stat", "missing"], False],
                     ["{TREE}/frontend/vitest.config.ts.timestamp-", ["write", "list"], False], ["{TREE}/frontend/abc.tmp", ["write", "read"], False],
                     ["{TREE}/frontend/node_modules/.vite/results.json", ["read", "write"], False])
        self.assertTrue(good["ok"], good["undeclared"])
        self.assertEqual(good["counts"]["ignored_paths"], 6, "6 行只命中输出区（前 5 行 glob、abc.tmp）")
        self.assertEqual(good["counts"]["installed_paths"], 1, ".vite/ 下的 results.json 同时在安装目录里，按安装目录计")
        # 命中的路径只读不写：不是命令自己产出的，仍然违规；只枚举同理。
        for label, row in {"只读": [temp, ["read"], False], "只执行": [temp, ["exec"], False], "只枚举": [temp, ["list", "stat"], False]}.items():
            result = check(row)
            self.assertEqual([(item["rule"], item["path"]) for item in result["undeclared"]], [("C1-untracked-read", row[0])], label)
        self.assertEqual([item["rule"] for item in check([temp, ["write"], True])["undeclared"]], ["C1-ignored-tracked"], "已跟踪文件命中输出区是登记错误")
        # 没命中的路径按范围规则：frontend/ 里写入违规、读未跟踪文件违规。
        for label, path in {"少了连字符": "{TREE}/frontend/vitest.config.ts.timestamp", "点是字面量不是任意字符": "{TREE}/frontend/vitestXconfig.ts.timestamp-1",
                            "* 不跨 /": "{TREE}/frontend/sub/vitest.config.ts.timestamp-1-aa.mjs", "不含子路径": "{TREE}/frontend/vitest.config.ts.timestamp-1/inner.mjs",
                            "? 少一个字符": "{TREE}/frontend/ac.tmp", "? 多一个字符": "{TREE}/frontend/abbc.tmp", "? 不匹配斜杠": "{TREE}/frontend/a/c.tmp"}.items():
            written_row = check([path, ["write"], False])
            self.assertEqual([item["rule"] for item in written_row["undeclared"]], ["C4-write-declared"], f"{label}（写）：{written_row['undeclared']}")
            read_row = check([path, ["read"], False])
            self.assertEqual([item["rule"] for item in read_row["undeclared"]], ["C1-untracked-read"], f"{label}（读）")
        # 范围之外的路径：glob 只豁免命中的路径，不改变别的规则。
        outside_scope = check(["{TREE}/other/vitest.config.ts.timestamp-1-aa.mjs", ["write"], False])
        self.assertEqual([item["rule"] for item in outside_scope["undeclared"]], ["C1-undeclared"])

    def test_c1_repo_ignored_globs_in_directories_and_without_wildcards(self) -> None:
        scope = make_scope()
        scope["repo"]["repo_ignored"] = ["backend/*/gen-?.tmp", "backend/exact.bin"]

        def check(*rows):
            return self.cover(self.sample_with(repo=[["{TREE}/backend/go.mod", ["read"], True], *rows], git=[]), scope)

        self.assertTrue(check(["{TREE}/backend/x/gen-1.tmp", ["write", "read"], False], ["{TREE}/backend/y/gen-z.tmp", ["write"], False],
                              ["{TREE}/backend/exact.bin", ["write", "read"], False])["ok"])
        for path in ("{TREE}/backend/gen-1.tmp", "{TREE}/backend/x/y/gen-1.tmp", "{TREE}/backend/x/gen-12.tmp", "{TREE}/backend/exact.bin2"):
            self.assertEqual([item["rule"] for item in check([path, ["write"], False])["undeclared"]], ["C4-write-declared"], path)

    def test_c1_repo_ignored_glob_registers_its_literal_directories_as_stat_only_ancestors(self) -> None:
        """glob 的目录部分里、第一个通配符之前的字面目录（这里是 build/cache）是命令自己创建并写入的输出目录：对它们的 stat／dir 探测按
        祖先目录放行，读写其中的其它文件仍然违规（它们不在任何声明里）。"""

        scope = make_scope()
        scope["repo"]["repo_ignored"] = ["build/cache/*.tmp"]

        def check(*rows):
            return self.cover(self.sample_with(repo=[["{TREE}/backend/go.mod", ["read"], True], *rows], git=[]), scope)

        ok = check(["{TREE}/build", ["stat"], False], ["{TREE}/build/cache", ["dir", "stat"], False], ["{TREE}/build/cache/a.tmp", ["write", "read"], False])
        self.assertTrue(ok["ok"], ok["undeclared"])
        for path, kinds in (("{TREE}/build", ["read"]), ("{TREE}/build/cache", ["list"]), ("{TREE}/build/other", ["stat"]), ("{TREE}/build/cache/a.txt", ["write"])):
            self.assertFalse(check([path, kinds, False])["ok"], (path, kinds))

    # ---- 容忍集合：unfinished-calls-at-eof ----

    def test_c7_unfinished_calls_at_eof_are_tolerated_up_to_sixty_four(self) -> None:
        """多线程进程（node／vitest／go）退出时，strace 不会为还在系统调用里的线程打印 resumed，流结束时留下少量永远配不上的
        <unfinished ...>：这是正常现象，被打断的调用没有返回成功、不贡献读集。容忍，但最多 64 个，超过即证据无效；其余不在容忍集合内的
        原因仍然无效。"""

        sample = self.sample_with

        def eof(count: int, extra: dict | None = None, **coverage):
            reasons = {"unfinished-calls-at-eof": count, **(extra or {})}
            return sample(coverage={"unresolved": sum(reasons.values()), "unresolved_reasons": reasons, "unfinished_calls": count, **coverage})

        for count in (1, 3, 63, 64):
            result = self.cover(eof(count))
            self.assertTrue(result["ok"], (count, result["undeclared"]))
        self.assertTrue(self.cover(eof(64, {"symlink-metadata-not-bound:lstat": 9}))["ok"], "与符号链接元数据的计数并存")
        over = self.expect("C7-unresolved", eof(65), label="65 个：超过上限")
        self.assertIn("超过容忍上限 64", over["undeclared"][0]["reason"])
        self.expect("C7-unresolved", eof(1000), label="大面积丢失的流不能当成正常现象")
        # 容忍的只是这一个名字（精确匹配），名字相近的别种原因、其它未解析原因照旧无效。
        self.expect("C7-unresolved", eof(2, {"unfinished-calls-at-eof-extra": 1}, unfinished_calls=2), label="名字相近的别种原因")
        self.expect("C7-unresolved", eof(2, {"unsupported-call:ptrace": 1}, unfinished_calls=2), label="混着不可容忍的原因")
        self.expect("C7-unresolved", eof(2, {"sampler:connect-unfinished-at-eof": 1}, unfinished_calls=3), label="connect 悬着不在容忍集合里")
        self.expect("C7-unresolved", eof(2, {"deferred-process-events-at-eof": 2}, unfinished_calls=2, unresolved_process_events=2),
                    label="子进程事件没等到父进程的派生记录，读集不完整")
        # 数字字段不得超出被容忍的原因能解释的部分；总数必须等于各原因计数之和。
        self.expect("C7-unresolved", eof(3, unfinished_calls=4), label="unfinished_calls 比 unfinished-calls-at-eof 还多")
        self.expect("C7-unresolved", sample(coverage={"unresolved": 5, "unresolved_reasons": {"unfinished-calls-at-eof": 3}, "unfinished_calls": 3}),
                    label="unresolved 总数与各原因之和不符")
        self.expect("C7-unresolved", eof(3, unresolved_process_events=1), label="有未归属的进程事件")
        # 采样器的写法：unresolved 等于各原因之和，unfinished_calls 等于悬着的未配对调用数（这里没有 connect 悬着）。
        self.assertTrue(self.cover(eof(7))["ok"])

    def test_c7_unfinished_samples_are_checked_for_type_and_length_only(self) -> None:
        sample = self.sample_with
        base = {"unresolved": 2, "unresolved_reasons": {"unfinished-calls-at-eof": 2}, "unfinished_calls": 2}
        lines = ['4242 openat(AT_FDCWD</x>, "/etc/shadow", O_RDONLY <unfinished ...>', '4243 read(3</x>, "..."']
        self.assertTrue(self.cover(sample(coverage={**base, "unfinished_samples": lines}))["ok"], "样例内容不参与放行或拒绝")
        self.assertTrue(self.cover(sample(coverage={**base, "unfinished_samples": []}))["ok"])
        self.assertTrue(self.cover(sample(coverage={**base, "unfinished_samples": ["x" * 300] * 8}))["ok"], "上限：8 条、每条 300 字符")
        for label, value in {"超过 8 条": ["x"] * 9, "单条超过 300 字符": ["x" * 301], "不是数组": "x", "条目不是字符串": [1], "None": None}.items():
            result = self.cover(sample(coverage={**base, "unfinished_samples": value}))
            self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-format"], label)
        # 样例里写了什么都不能让一份无效的证据变有效。
        invalid = sample(coverage={"unresolved": 66, "unresolved_reasons": {"unfinished-calls-at-eof": 66}, "unfinished_calls": 66,
                                   "unfinished_samples": ["看起来完全无害的样例"]})
        self.assertFalse(self.cover(invalid)["ok"])

    # ---- 缺口 3：仓库外祖先目录探测 ----

    def test_c2_ancestor_directories_of_declared_prefixes_and_aliases_are_covered_for_metadata_only(self) -> None:
        """仓库外路径若是已声明 outside 前缀、{TREE}、{WORK}、{HOME}、{TMP} 的祖先目录（如 /、/root、/usr、/root/.config），且只有
        stat／dir，视为覆盖通过（与仓库内「声明内容的上级目录算覆盖」同构）；读取、枚举、其余无前缀路径照旧违规。（WP1c 起：测试树的
        严格祖先目录另许枚举与读取，系统目录与别名根之下的 missing／stat／dir 按「噪声探测」放行——见下面逐行说明和
        test_ci_declared_inputs_probe_rules。）"""

        scope = make_scope()
        scope["outside"].append({"prefix": "{HOME}/.config/go/telemetry/", "class": "scratch"})
        # WORK、HOME、TMP 故意放在测试树祖先链之外，这样它们各自的祖先目录只能靠「别名根的祖先」这条规则放行。
        aliases = {"TREE": "/srv/ci/gate/tree", "WORK": "/data/ci/work", "HOME": "/home/runner", "TMP": "/var/scratch/tmp"}

        def check(*rows, aliases=aliases):
            return self.cover(self.sample_with(outside=list(rows), aliases=aliases), scope)

        good = check(["/", ["stat"], 3], ["/usr/", ["stat", "dir"], 1], ["/usr/local/", ["stat"], 1],
                     ["/srv/", ["stat"], 1], ["/srv/ci/", ["stat", "dir"], 2], ["/srv/ci/gate/", ["stat"], 2],
                     ["/data/", ["stat"], 1], ["/data/ci/", ["stat", "dir"], 1], ["/home/", ["stat"], 1], ["/var/", ["stat"], 1],
                     ["/var/scratch/", ["stat", "dir"], 1], ["{WORK}", ["stat", "dir"], 1], ["{HOME}", ["stat"], 1], ["{HOME}/", ["stat"], 1],
                     ["{HOME}/.config/", ["stat", "dir"], 1], ["{HOME}/.config/go/", ["stat"], 1],
                     ["{HOME}/.config/go/telemetry/local/", ["read", "write"], 3], ["{HOME}/.config/go/telemetry/mode", ["missing"], 1])
        self.assertTrue(good["ok"], good["undeclared"])
        repo_only = check()["counts"]["exempt_paths"]  # 样本里仓库内路径本来就有的豁免（祖先目录、list_ok、树根探测）
        self.assertEqual(good["counts"]["exempt_paths"] - repo_only, 16, "上面 18 行里 2 行被前缀覆盖，其余 16 行是祖先目录豁免")
        # 同样的祖先目录，一旦读取、枚举（不在测试树祖先链上）或带了 missing 而路径不在放行根与别名根之下，照旧违规。
        # WP1c 起有几条放行了，改成了下面的正例：测试树的严格祖先可以被枚举、被打开（/srv/ci/ 的枚举、/ 的读取），工作根的祖先、系统目录
        # （/usr/、/opt/）与别名根之下的 missing／stat 按「噪声探测」放行；这里的违规例改用不在放行根、不在别名根之下的目录。
        for label, row, rule in (("祖先读取", ["/usr/", ["stat", "read"], 1], "C2-outside-undeclared"),
                                 ("工作根祖先枚举（不在测试树祖先链上）", ["/data/ci/", ["stat", "list"], 1], "C2-outside-undeclared"),
                                 ("工作根祖先读取", ["/data/ci/", ["stat", "read"], 1], "C2-outside-undeclared"),
                                 ("HOME 祖先读取", ["{HOME}/.config/", ["read"], 1], "C2-outside-undeclared"),
                                 ("无关目录的 stat（不在放行根与别名根之下）", ["/data/other/", ["stat"], 1], "C2-outside-undeclared"),
                                 ("无关目录的 missing（不在放行根与别名根之下）", ["/data/other/", ["missing"], 1], "C3-missing-undeclared"),
                                 ("工作根的兄弟目录", ["/data/ci/other/", ["stat"], 1], "C2-outside-undeclared"),
                                 ("测试树祖先带写入", ["/srv/ci/", ["stat", "write"], 1], "C2-outside-undeclared"),
                                 ("测试树祖先带执行", ["/", ["stat", "exec"], 1], "C2-outside-undeclared")):
            result = check(row)
            self.assertFalse(result["ok"], label)
            self.assertEqual([item["rule"] for item in result["undeclared"]], [rule], f"{label}：{result['undeclared']}")
        for label, row in (("测试树祖先枚举（WP1c 起放行）", ["/srv/ci/", ["stat", "list"], 1]), ("根目录读取（测试树的严格祖先，WP1c 起放行）", ["/", ["read"], 1]),
                           ("工作根祖先带 missing（别名根的祖先，WP1c 起放行）", ["/data/", ["stat", "missing"], 1]),
                           ("系统目录的 stat（WP1c 起放行）", ["/opt/", ["stat"], 1]), ("系统目录的 missing（WP1c 起放行）", ["/opt/", ["missing"], 1]),
                           ("声明前缀的祖先带 missing（WP1c 起放行）", ["/usr/", ["stat", "missing"], 1])):
            self.assertTrue(check(row)["ok"], label)
        # 证据没给出可信的别名绝对路径时，别名祖先不放行（只剩声明前缀的祖先）。
        # （WP1c 起 /srv/ 之类的系统目录之下 stat 按噪声探测放行，所以这里的第二条路径换成不在放行根下的 /data2/。）
        broken = check(["/data/ci/", ["stat"], 1], ["/data2/ci/gate/", ["stat"], 1], ["/usr/local/", ["stat"], 1],
                       aliases={"TREE": "relative", "WORK": "", "HOME": "~", "TMP": "/"})
        self.assertEqual([item["path"] for item in broken["undeclared"]], ["/data/ci/", "/data2/ci/gate/"], "声明前缀的祖先 /usr/local/ 仍然放行")

    def test_c3_the_test_tree_ancestor_chain_may_show_missing_probes_in_directory_aggregates(self) -> None:
        """Go 往上找 go.work 与模块目录、版本控制目录时逐级探测祖先目录里的名字。采样器按目录聚合，名字丢了，只剩「这个祖先目录里有
        不存在的探测」——放宽到 stat／dir／missing，且只限测试树的祖先链（含 {WORK} 恰好是测试树祖先的情形）。"""

        scope = make_scope()
        aliases = {"TREE": "/var/tmp/gate/tree", "WORK": "/var/tmp/gate", "HOME": "/root", "TMP": "/tmp"}

        def check(*rows):
            return self.cover(self.sample_with(outside=list(rows), aliases=aliases), scope)

        good = check(["/", ["missing", "stat"], 5], ["/var/", ["missing", "stat"], 5], ["/var/tmp/", ["missing", "stat", "dir"], 5], ["{WORK}/", ["missing"], 1])
        self.assertTrue(good["ok"], good["undeclared"])
        # WP1c 之前这里的违规例是「非祖先目录的 missing」「祖先里有读取／枚举」「测试树之下的目录」「别的别名根的 missing」；现在系统目录与别名根之下的
        # missing 按噪声探测放行，测试树的严格祖先可以被枚举、被打开，这几条改成了下面的正例，违规例换成真正还要声明前缀的情形：
        # 非祖先目录的读取与枚举、祖先里的写入与执行、测试树之下目录的读取与枚举、别的别名根的枚举。
        for label, row in (("非祖先目录里的读取", ["/opt/", ["read"], 1]), ("非祖先目录里的枚举", ["/opt/", ["missing", "list"], 1]),
                           ("祖先里有写入", ["/var/", ["missing", "write"], 1]), ("祖先里有执行", ["/var/tmp/", ["missing", "exec"], 1]),
                           ("测试树之下的目录不算祖先（读取）", ["{WORK}/tree/", ["missing", "read"], 1]),
                           ("测试树之下的目录不算祖先（枚举）", ["{WORK}/tree/", ["list"], 1]), ("别的别名根的枚举", ["{HOME}/", ["missing", "list"], 1])):
            result = check(row)
            self.assertFalse(result["ok"], label)
        for label, row in (("非祖先目录里的 missing（系统目录，噪声探测）", ["/opt/", ["missing"], 1]), ("祖先里有读取（严格祖先，目录自身被打开）", ["/var/", ["missing", "read"], 1]),
                           ("祖先里有枚举（严格祖先，目录自身被枚举）", ["/var/tmp/", ["missing", "list"], 1]),
                           ("测试树之下的目录里的 missing（别名根之下，噪声探测）", ["{WORK}/tree/", ["missing"], 1]),
                           ("别的别名根的 missing（别名根之下，噪声探测）", ["{HOME}/", ["missing"], 1])):
            self.assertTrue(check(row)["ok"], label)

    def test_git_object_store_aggregate_key_and_the_other_trailing_slash_keys(self) -> None:
        sample = self.sample_with
        aggregate = ["{TREE}/.git/objects/", ["read", "stat", "dir", "list"], False]
        self.assertTrue(self.cover(sample(extra_repo=[aggregate]))["ok"], "采样器把对象库聚合成这一个键；声明了 git_history 时放行")
        no_history = make_scope()
        del no_history["repo"]["git_history"]
        self.expect("C1-git-undeclared", sample(repo=[aggregate], git=[]), no_history, "没有 git_history 时对象库读取违规")
        self.expect("C1-git-kinds", sample(extra_repo=[["{TREE}/.git/objects/", ["write"], False]]), label="对象库聚合键里有写入")
        for key in ("{TREE}/backend/", "{TREE}/.git/", "{TREE}/.git/objects/ab/", "{TREE}/docs/egress/"):
            self.expect("C1-path", sample(extra_repo=[[key, ["read"], True]]), label=f"除对象库聚合键外，仓库内路径不许带尾部斜杠：{key}")

    def test_the_sampler_style_command_cwd_and_extra_keys_are_accepted(self) -> None:
        sample = make_sample()
        root_sample = make_sample()
        root_sample["command"] = {"launcher": [], "argv": ["go", "version"], "cwd_rel": ".", "sha256": ur.declared_command_sha256(["go", "version"], ".")}
        self.assertTrue(self.cover(root_sample)["ok"], "测试树根作 cwd 时采样器写 .")
        for key in ("valid", "invalid_reasons", "review"):
            self.assertNotIn(key, sample)
        bad = {"valid": "yes"}, {"invalid_reasons": "oops"}, {"invalid_reasons": [1]}, {"review": []}, {"valid": True, "invalid_reasons": ["x"]}, \
            {"valid": False, "invalid_reasons": []}
        for extra in bad:
            result = self.cover({**sample, **extra})
            self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-format"], extra)


# ---------------------------------------------------------------------------
# 与采样器（declared_sampler.py）互操作：它写出的证据，验证侧必须原样认得
#
# 用采样器自己的 filter／merge 处理 ARM64 摸底时抓到的真实 strace 片段（tests/declared_sampler_trace_fixture.py），把产出的
# declared-sample/v1 落盘再读回（和登记进仓库的证据文件一样是半展开的 ASCII JSON），交给验证侧的覆盖检查与消费者核验。
# 采样器不在的分支整组跳过。
# ---------------------------------------------------------------------------


def sampler_repo(root: Path) -> Path:
    """采样器 merge 要用的测试树：几个跟踪文件，.gitignore 忽略 out/ 与 ent 代码生成的临时目录。"""

    tree = root / "sampler-tree"
    tree.mkdir()
    files = {".gitignore": "out/\nbackend/ent/schema/.entc/\nfrontend/vitest.config.ts.timestamp-*\n", "backend/go.mod": "module x\n", "backend/go.sum": "s\n",
             "backend/a.go": "package a\n",
             "backend/internal/pkg/pkg.go": "package pkg\n", "docs/egress/foundation/sink-stats.md": "统计\n"}
    for relative, text in files.items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    environment = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True, env=environment)
    return tree.resolve()


def sampler_sample(tree: Path, lines: list[str], aliases: dict, *, argv: list[str] | None = None, cwd_rel: str = "backend",
                   unit: str = "backend:go-test", **merge) -> dict:
    """strace 行 → 采样器 filter → 补上 run 信息 → 采样器 merge → declared-sample/v1（与采样器自己的测试同一套路）。"""

    document = sampler.filter_stream(lines, aliases, unit_id=unit)
    command = {"launcher": [], "argv": argv or ["go", "test", "./internal/pkg/...", "-count=1"], "cwd_rel": cwd_rel}
    command["sha256"] = sampler.command_digest(command["argv"], command["cwd_rel"])
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    document["run"] = {"label": "cold", "cache": "cold", "exit_code": 0, "signal": None, "timed_out": False, "seconds": 1.5,
                       "started_utc": "2026-10-14T00:00:00Z", "finished_utc": "2026-10-14T00:00:02Z", "command": command, "git_status_before": [],
                       "git_status_after": [], "commit": head, "kernel": "6.8.0", "strace": "6.8", "sampler_sha256": SAMPLER, "env_overrides": {},
                       "docker": None, "filter_wait_seconds": 0.0}
    return sampler.merge_raws([document], tree=tree, **merge)


def written(sample: dict) -> dict:
    """证据落盘（采样器的 dumps_document）再读回：与登记进仓库的证据文件完全同形。"""

    return json.loads(sampler.dumps_document(sample))


def reviewed_scope() -> dict:
    """评审过的后端 go test 声明范围：对应真实 strace 片段里读到的工具链、缓存、PATH 搜索目录、/proc、/sys 与 HOME 下的 Go 配置。"""

    return {
        "schema_version": ur.DECLARED_SCOPE_SCHEMA,
        "repo": {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}], "files": ["backend/go.mod", "backend/go.sum"]},
        "outside": [
            {"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"},
            {"prefix": "/opt/node-v20/", "class": "toolchain", "contract": "toolchain:node"},
            {"prefix": "{WORK}/gc-c/", "class": "performance-cache", "contract": "cache:go-build-cache"},
            {"prefix": "{WORK}/go-tmp/", "class": "scratch"}, {"prefix": "{TMP}/", "class": "scratch"},
            {"prefix": "{HOME}/.config/go/telemetry/", "class": "scratch"},
            {"prefix": "{HOME}/.config/go/env", "class": "home-config", "contract": "host-config:config-go-env"},
            {"prefix": "/usr/bin/", "class": "system"}, {"prefix": "/usr/sbin/", "class": "system"}, {"prefix": "/usr/local/bin/", "class": "system"},
            {"prefix": "/usr/local/sbin/", "class": "system"}, {"prefix": "/bin/", "class": "system"}, {"prefix": "/sbin/", "class": "system"},
            {"prefix": "/proc/", "class": "virtual"}, {"prefix": "/sys/", "class": "virtual"}],
        "contracts": ["toolchain:go", "toolchain:node", "cache:go-build-cache", "host-config:config-go-env"],
    }


@unittest.skipIf(sampler is None, "声明级采样器（tools/ci/declared_sampler.py）不在本分支：互操作用例整组跳过")
class SamplerInteroperabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.tree = sampler_repo(self.root)
        self.aliases = {"TREE": real_strace.TREE, "WORK": real_strace.ROOT, "HOME": "/root", "TMP": "/tmp"}
        self.lines = "".join(real_strace.ALL_SEGMENTS).splitlines(keepends=True)

    def real_sample(self) -> dict:
        return written(sampler_sample(self.tree, self.lines, self.aliases, class_prefixes=[sampler.parse_class_prefix("{HOME}/.config/go/telemetry/=scratch")]))

    def test_the_real_arm64_strace_written_by_the_sampler_passes_with_a_reviewed_scope(self) -> None:
        sample = self.real_sample()
        self.assertEqual((sample["valid"], sample["invalid_reasons"]), (True, []))
        self.assertEqual(sampler.validate_sample(sample), [])
        self.assertEqual(set(sample) - set(ur._DECLARED_SAMPLE_KEYS), {"valid", "invalid_reasons", "review"}, "采样器多写的三个键验证侧都认")
        self.assertEqual(sample["coverage"]["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 6}, "真实片段里只有容忍集合里的原因")
        self.assertEqual(sample["coverage"]["detached_markers"], 3)
        result = ur.declared_coverage(sample, reviewed_scope())
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertEqual(result["counts"]["classes"], {"system": 6, "toolchain": 6, "virtual": 5, "home-config": 1, "scratch": 7, "performance-cache": 1})
        self.assertEqual(result["counts"]["exempt_paths"], 8, "{TREE} 的 stat、树根下的 go.work、/、/var/、/var/tmp/、{WORK}/、/usr/local/、{WORK} 自身")

    def test_go_work_probes_up_the_ancestor_chain_show_up_as_missing_in_directory_aggregates(self) -> None:
        """为什么测试树祖先链上的目录要放宽到 stat／dir／missing：Go 往上找 go.work 逐级探测不存在的名字，采样器按目录聚合后
        名字丢了，/、/var/、/var/tmp/、{WORK}/ 这些键的 kinds 里就带 missing。只认 stat／dir 的话，任何 Go 单元都过不了。"""

        sample = self.real_sample()
        chain = {key: kinds for key, kinds, _count in sample["outside_accesses"] if key in ("/", "/var/", "/var/tmp/", "{WORK}/")}
        self.assertEqual(set(chain), {"/", "/var/", "/var/tmp/", "{WORK}/"})
        self.assertTrue(all("missing" in kinds for kinds in chain.values()), chain)
        self.assertTrue(ur.declared_coverage(sample, reviewed_scope())["ok"])
        # 证据声称的测试树不在这条链上时：纯 missing／stat／dir 的探测由 WP1c 的「噪声探测」放行（/var/ 在系统目录放行根下、{WORK}/ 在别名根
        # 之下），所以只剩读取、枚举这类超出探测的访问才是没有声明前缀的违规——给链上的键各加一个 read 来验证。
        moved = copy.deepcopy(sample)
        moved["aliases"]["TREE"] = "/elsewhere/ci/tree"
        self.assertTrue(ur.declared_coverage(moved, reviewed_scope())["ok"], "只有 missing／stat／dir：噪声探测放行")
        for index, (key, kinds, count) in enumerate(moved["outside_accesses"]):
            if key in ("/", "/var/", "/var/tmp/", "{WORK}/"):
                moved["outside_accesses"][index] = [key, sorted({*kinds, "read"}), count]
        result = ur.declared_coverage(moved, reviewed_scope())
        self.assertFalse(result["ok"])
        self.assertEqual({item["path"] for item in result["undeclared"]}, {"/var/", "/var/tmp/", "{WORK}/"}, "根目录 / 永远在任何测试树的祖先链上，所以不报")

    def test_every_reviewed_prefix_is_load_bearing_for_the_real_sample(self) -> None:
        """每个评审过的前缀都不是摆设：它下面有 read／write／exec／list 这类超出探测的访问时，去掉它就有未覆盖的访问；只有 missing／
        stat／dir 的前缀（PATH 搜索目录、不存在的 HOME 配置）在 WP1c 起属于「噪声探测」，去掉后仍然放行——声明它们只是为了绑定合同。"""

        sample = self.real_sample()
        probe_only = {"missing", "stat", "dir"}
        load_bearing = 0
        for index, entry in enumerate(reviewed_scope()["outside"]):
            prefix = entry["prefix"]
            if prefix == "{TMP}/":
                continue  # 样本里 {TMP} 只有一次 stat：别名根自身的 stat 本来就放行，删掉前缀不影响结论
            scope = reviewed_scope()
            del scope["outside"][index]
            beyond_probes = [key for key, kinds, _count in sample["outside_accesses"] if ur._declared_covers(prefix, key) and set(kinds) - probe_only]
            with self.subTest(prefix):
                result = ur.declared_coverage(sample, scope)
                if not beyond_probes:
                    self.assertTrue(result["ok"], f"{prefix} 下只有 missing／stat／dir 的探测：去掉后仍放行（噪声探测）{result['undeclared']}")
                    continue
                load_bearing += 1
                self.assertFalse(result["ok"], f"去掉 {prefix} 后应当有未覆盖的访问")
                self.assertTrue(any(item["path"].startswith(prefix) or item["path"] + "/" == prefix for item in result["undeclared"]),
                                (prefix, result["undeclared"]))
        self.assertGreaterEqual(load_bearing, 5, "真实片段里要有足够多的前缀确实在承重（工具链、缓存、系统库、/proc、/sys、遥测目录）")

    def codegen_lines(self, *, read_back: bool) -> tuple[list[str], dict]:
        t, w = str(self.tree), str(self.root / "work")
        cwd = f"{t}/backend"
        lines = [
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/backend/go.mod", O_RDONLY|O_CLOEXEC) = 3<{t}/backend/go.mod>',
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/backend/a.go", O_RDONLY|O_CLOEXEC) = 3<{t}/backend/a.go>',
            f'4242 mkdir("{t}/backend/ent/schema/.entc/gen", 0777) = 0',
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/backend/ent/schema/.entc/gen/x.go", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 3<{t}/backend/ent/schema/.entc/gen/x.go>',
            f'4242 execve("/usr/bin/git", ["git", "show", "{COMMIT_A}:docs/egress/x.json"], 0x1 /* 17 vars */) = 0',
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/.git/objects/ab/cdef", O_RDONLY|O_CLOEXEC) = 3<{t}/.git/objects/ab/cdef>',
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/.git/objects/pack/pack-1.idx", O_RDONLY|O_CLOEXEC) = 3<{t}/.git/objects/pack/pack-1.idx>',
            f'4242 openat(AT_FDCWD<{cwd}>, "{t}/.git/HEAD", O_RDONLY|O_CLOEXEC) = 3<{t}/.git/HEAD>',
            '4242 connect(3<socket:[1]>, {sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}, 23) = 0',
            '4242 connect(3<socket:[2]>, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
            '4242 connect(3<socket:[3]>, {sa_family=AF_UNSPEC, sa_data="\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0"}, 16) = 0',
        ]
        if read_back:
            lines.append(f'4242 openat(AT_FDCWD<{cwd}>, "{t}/backend/ent/schema/.entc/gen/x.go", O_RDONLY|O_CLOEXEC) = 3<{t}/backend/ent/schema/.entc/gen/x.go>')
        return lines, {"TREE": t, "WORK": w, "HOME": "/home/sampler", "TMP": "/tmp/sampler-tmp"}

    def codegen_scope(self) -> dict:
        return {"schema_version": ur.DECLARED_SCOPE_SCHEMA,
                "repo": {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}], "repo_ignored": ["backend/ent/schema/.entc/"],
                         "git_history": {"commits": [COMMIT_A], "reason": "台账测试读固定提交"}},
                "outside": [{"prefix": "/usr/bin/", "class": "system"}], "network": "loopback-only", "unix_sockets": ["/var/run/docker.sock"]}

    def test_unix_sockets_git_history_and_output_directories_written_by_the_sampler(self) -> None:
        """缺口 1～2 的端到端：docker.sock 登记在 unix_sockets；ent 代码生成在测试树里写临时目录，目录登记为 repo_ignored；git 子进程读
        固定提交；对象库读取被采样器聚合成一个键；UDP 断开关联（AF_UNSPEC）不算外连。"""

        lines, aliases = self.codegen_lines(read_back=False)
        sample = written(sampler_sample(self.tree, lines, aliases, allow_unix=["/var/run/docker.sock"]))
        self.assertEqual((sample["valid"], sample["invalid_reasons"]), (True, []))
        rows = {path: (kinds, tracked) for path, kinds, tracked in sample["repo_accesses"]}
        self.assertEqual(rows["{TREE}/.git/objects/"][0], ["read"], "对象库读取被采样器聚合成一个键（尾部带 /）")
        self.assertEqual(rows["{TREE}/backend/ent/schema/.entc/gen/x.go"], (["write"], False))
        self.assertEqual(rows["{TREE}/backend/ent/schema/.entc/gen"], (["write"], False))
        self.assertEqual({item["family"] for item in sample["network"]}, {"AF_UNIX", "AF_INET", "AF_UNSPEC"})
        scope = self.codegen_scope()
        result = ur.declared_coverage(sample, scope)
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertEqual((result["counts"]["git_paths"], result["counts"]["ignored_paths"], result["counts"]["network"]), (2, 2, 3))
        without_socket = copy.deepcopy(scope)
        del without_socket["unix_sockets"]
        self.assertEqual([item["rule"] for item in ur.declared_coverage(sample, without_socket)["undeclared"]], ["C5-unix-undeclared"])
        without_ignored = copy.deepcopy(scope)
        without_ignored["repo"]["repo_ignored"] = []
        self.assertEqual({item["rule"] for item in ur.declared_coverage(sample, without_ignored)["undeclared"]}, {"C4-write-declared"},
                         "输出区没登记：写在声明范围内就是违规（范围是只读输入）")
        without_history = copy.deepcopy(scope)
        del without_history["repo"]["git_history"]
        self.assertTrue({"C1-git-undeclared", "C6-commit-undeclared"} <= {item["rule"] for item in ur.declared_coverage(sample, without_history)["undeclared"]})

    def test_reading_back_a_self_produced_file_is_allowed_by_the_verifier_and_by_the_sampler_once_registered(self) -> None:
        """缺口 2：验证侧放行「同一路径写出再读回」。采样器的 merge 不登记时仍把输出区里的读回记成 untracked-read、整份采样判无效
        （valid 为假，验证侧不采信，C7-invalid）；用 merge --transient 登记输出区之后放行读回，整份证据通过（步骤 5 的 WP4b）。"""

        lines, aliases = self.codegen_lines(read_back=True)
        raw = sampler_sample(self.tree, lines, aliases, allow_unix=["/var/run/docker.sock"])
        self.assertFalse(raw["valid"])
        self.assertEqual(len(raw["invalid_reasons"]), 1)
        self.assertIn("[untracked-read]", raw["invalid_reasons"][0])
        sample = written(raw)
        rows = {path: kinds for path, kinds, _tracked in sample["repo_accesses"]}
        self.assertEqual(rows["{TREE}/backend/ent/schema/.entc/gen/x.go"], ["read", "write"], "判据：同一路径的 kinds 同时含 write 与 read")
        result = ur.declared_coverage(sample, self.codegen_scope())
        self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-invalid"], "只剩采样器自己的无效标记；覆盖规则本身放行读回")
        forced = {**sample, "valid": True, "invalid_reasons": []}
        self.assertTrue(ur.declared_coverage(forced, self.codegen_scope())["ok"], "采样器放行读回后（valid 为真），整份证据通过")
        registered = written(sampler_sample(self.tree, lines, aliases, allow_unix=["/var/run/docker.sock"], transient=["backend/ent/schema/.entc/"]))
        self.assertEqual((registered["valid"], registered["invalid_reasons"]), (True, []), "登记了输出区，读回不再判无效")
        result = ur.declared_coverage(registered, self.codegen_scope())
        self.assertTrue(result["ok"], result["undeclared"])

    def test_unfinished_calls_at_eof_and_globbed_temp_files_with_the_sampler_options(self) -> None:
        """采样器改进（步骤 5 的 WP4b）之后：多线程进程退出时悬着的 <unfinished ...>（unfinished-calls-at-eof）被采样器容忍；vite 写再读回的
        随机名临时文件用 merge --transient 登记后放行。两项合起来这份采样有效，验证侧覆盖检查通过；不登记 --transient 时读回仍被判
        untracked-read（valid 为假，验证侧不采信）。只钉这几个不随采样器细节变化的结论，不钉计数与样例条数（采样器还会继续改）。"""

        t = str(self.tree)
        cwd = f"{t}/frontend"
        temp = f"{t}/frontend/vitest.config.ts.timestamp-1699999999999-0a1b2c3d4e5f.mjs"
        lines = [f'4242 openat(AT_FDCWD<{cwd}>, "{t}/backend/go.mod", O_RDONLY|O_CLOEXEC) = 3<{t}/backend/go.mod>',
                 f'4242 openat(AT_FDCWD<{cwd}>, "{temp}", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 3<{temp}>',
                 f'4242 openat(AT_FDCWD<{cwd}>, "{temp}", O_RDONLY|O_CLOEXEC) = 3<{temp}>',
                 f'4243 openat(AT_FDCWD<{cwd}>, "{t}/backend/a.go", O_RDONLY|O_CLOEXEC <unfinished ...>',
                 f'4244 openat(AT_FDCWD<{cwd}>, "{t}/backend/go.sum", O_RDONLY|O_CLOEXEC <unfinished ...>']
        aliases = {"TREE": t, "WORK": str(self.root / "work"), "HOME": "/home/sampler", "TMP": "/tmp/sampler-tmp"}
        argv = ["pnpm", "--dir", "frontend", "exec", "vitest", "run"]
        raw = sampler_sample(self.tree, lines, aliases, argv=argv, cwd_rel=".")
        self.assertIn("unfinished-calls-at-eof", raw["coverage"]["unresolved_reasons"], "构造的悬着调用确实被采样器识别出来")
        self.assertFalse(raw["valid"], "没登记 --transient：读回仍判无效")
        self.assertFalse(any("[unresolved]" in reason for reason in raw["invalid_reasons"]), "unfinished-calls-at-eof 已在容忍集合里")
        self.assertTrue(any("[untracked-read]" in reason for reason in raw["invalid_reasons"]), raw["invalid_reasons"])
        registered = sampler_sample(self.tree, lines, aliases, argv=argv, cwd_rel=".", transient=["frontend/vitest.config.ts.timestamp-*"])
        self.assertEqual((registered["valid"], registered["invalid_reasons"]), (True, []))
        sample = written(registered)
        rows = {path: kinds for path, kinds, _tracked in sample["repo_accesses"]}
        self.assertEqual(rows["{TREE}/frontend/vitest.config.ts.timestamp-1699999999999-0a1b2c3d4e5f.mjs"], ["read", "write"])
        scope = {"schema_version": ur.DECLARED_SCOPE_SCHEMA,
                 "repo": {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}, {"name": "frontend", "include": ["frontend/"], "exclude": []}],
                          "repo_ignored": ["frontend/vitest.config.ts.timestamp-*"]}}
        result = ur.declared_coverage(sample, scope)
        self.assertTrue(result["ok"], result["undeclared"])
        without_glob = copy.deepcopy(scope)
        without_glob["repo"]["repo_ignored"] = []
        self.assertEqual({item["rule"] for item in ur.declared_coverage(sample, without_glob)["undeclared"]}, {"C4-write-declared"},
                         "没有 glob 登记：随机名临时文件写在 frontend/ 范围内，违规")

    def test_the_sampler_command_digest_and_evidence_bytes_agree_with_the_record_layer(self) -> None:
        argv, cwd_rel = ["go", "test", "./...", "-count=1"], "backend"
        self.assertEqual(sampler.command_digest(argv, cwd_rel), ur.declared_command_sha256(argv, cwd_rel))
        sample = self.real_sample()
        self.assertEqual(sample["command"]["sha256"], ur.declared_command_sha256(sample["command"]["argv"], sample["command"]["cwd_rel"]))
        path = self.root / "evidence.json"
        sampler.write_document(path, sample)
        self.assertEqual(sampler.file_sha256(path), ur.file_sha256(path), "证据摘要是文件字节的 sha256：两边算法一致")

    def test_non_utf8_paths_in_the_evidence_round_trip_into_safe_reports(self) -> None:
        t = str(self.tree)
        lines = [f'4242 openat(AT_FDCWD<{t}/backend>, "{t}/out/\\377\\376.bin", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 3<{t}/out/\\377\\376.bin>',
                 f'4242 openat(AT_FDCWD<{t}/backend>, "{t}/backend/go.mod", O_RDONLY|O_CLOEXEC) = 3<{t}/backend/go.mod>']
        aliases = {"TREE": t, "WORK": str(self.root / "work"), "HOME": "/home/sampler", "TMP": "/tmp/sampler-tmp"}
        sample = written(sampler_sample(self.tree, lines, aliases))
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        scope = {"schema_version": ur.DECLARED_SCOPE_SCHEMA, "repo": {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}],
                                                                     "repo_ignored": ["out/"]}}
        self.assertTrue(ur.declared_coverage(sample, scope)["ok"], "输出区里的非 UTF-8 文件名只写不读，通过")
        scope["repo"]["repo_ignored"] = []
        result = ur.declared_coverage(sample, scope)
        self.assertEqual([item["rule"] for item in result["undeclared"]], ["C1-undeclared"])
        self.assertIn("\\udcff\\udcfe", result["undeclared"][0]["path"], "孤立代理字符在报告里是转义文本")
        json.dumps(result, ensure_ascii=False).encode("utf-8")

    def test_the_whole_chain_from_the_sampler_file_to_check_record(self) -> None:
        """采样器写的证据文件字节 → 登记摘要 → 记录 → 消费者核验，一路无损；改一个字节就拒绝。"""

        sample = self.real_sample()
        contracts = [contract_entry("toolchain", "go", version_line="go version go1.27.1 linux/arm64", executable_sha256="e" * 64),
                     contract_entry("toolchain", "node", version_line="v20.19.0", executable_sha256="1" * 64),
                     contract_entry("cache", "go-build-cache", role="performance-cache", location="work", rule="any-content-addressed"),
                     contract_entry("host-config", "config-go-env", path="{HOME}/.config/go/env", state="missing")]
        credentials = [{"id": "cache:go-build-cache", "kind": "cache", "verified_before_run": True, "verified_at_utc": "2026-10-15T00:00:05Z",
                        "observed": {"root_ok": True}}]
        raw = sampler.dumps_document(sample).encode("utf-8")
        world = DeclaredWorld(self.root / "world", scope=reviewed_scope(), sample=sample, contracts=contracts, credentials=credentials,
                              unit_id=sample["unit_id"], evidence_bytes=raw)
        self.assertEqual(world.evidence_digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(world.check(), [])
        self.assertEqual(world.check(historical_read_audit=True, require_read_audit=True), [])
        stored = world.store.log_path(world.evidence_digest)
        stored.chmod(0o600)
        stored.write_bytes(raw.replace(b"/usr/local/go/", b"/usr/local/gx/", 1))
        self.assertTrue(any("摘要漂移" in problem for problem in world.check()))


# ---------------------------------------------------------------------------
# 与入口门禁侧（WP2）的互操作：真实探测产出的合同条目 → 本章的消费者核验
# ---------------------------------------------------------------------------


@unittest.skipIf(entry_gates is None or entry_scaffold is None, "入口门禁侧（tools/ci/entry_gates.py 及其测试脚手架）不在本分支：互操作用例整组跳过")
class EntryGatesInteroperabilityTests(unittest.TestCase):
    """两边各自独立实现、各自提交：入口门禁侧负责探测并写出 ``contract:*`` 条目，本章负责在消费者侧核验它们的格式。约定靠这组用例
    对齐——例如 go-build-cache 合同的 ``requires.go_test_argv_contains``：只在命令是 go test 时才要求 ``-count=1``，同一个构建
    缓存也被 go vet、lint 一类命令使用。探测用入口门禁侧测试脚手架的假 runner 与合成工具链世界，不依赖真实的 docker、go、node。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        tree = entry_scaffold.make_tree(self.root)
        self.toolchain = entry_scaffold.Toolchain(self.root, tree)
        self.toolchain.runner.responses[("git", "rev-parse", "--verify", f"{COMMIT_A}^{{tree}}")] = (0, "c" * 40 + "\n")
        self.toolchain.runner.responses[("make", "-s", "--no-print-directory", "-n", "test-demo")] = (0, "echo demo\n")

    def credentials(self, entries: list[dict]) -> list[dict]:
        """运行前凭证：镜像 Id 取探测到的值（真实执行器启动前复核的就是同一个值），其余沿用夹具的默认凭证。"""

        image_id = next(entry for entry in entries if entry["name"] == "contract:service-image:postgres")["detail"]["image_id"]
        credentials = make_credentials()
        next(item for item in credentials if item["id"] == "service-image:postgres")["observed"]["image_id"] = image_id
        return credentials

    def test_real_probe_entries_pass_the_consumer_checks_end_to_end(self) -> None:
        scope = make_scope()
        # 离线要求（入口门禁侧常量缺省已开）由下面的离线用例单独覆盖；本用例只对齐格式，显式关掉。
        entries = self.toolchain.probe(require_offline=False).entries(entry_scaffold.declaration(scope))
        self.assertEqual(sorted(entry["name"] for entry in entries), sorted(entry["name"] for entry in make_contract_entries()),
                         "探测产出的合同条目名恰为声明范围要求的那一组（含由 git_history、make_expansion 派生的）")
        cache = next(entry for entry in entries if entry["name"] == "contract:cache:go-build-cache")["detail"]
        self.assertEqual(cache["requires"], {"go_test_argv_contains": ["-count=1"]}, "这就是两侧约定要对齐的形状")
        for entry in entries:
            _prefix, kind, name = entry["name"].split(":", 2)
            self.assertEqual(ur._declared_contract_entry_problems(entry, kind, name), [], entry["name"])
        world = DeclaredWorld(self.root / "world", contracts=entries, credentials=self.credentials(entries))
        self.assertEqual(world.check(), [], "命令是 go test ./... -count=1，满足 go-build-cache 合同的要求")
        self.assertEqual(world.check(historical_read_audit=True, require_read_audit=True), [])
        self.assertEqual(entry_gates.declared_contract_problems(entries, argv=world.spec()["argv"]), [], "入口门禁侧自己的前置判断同样通过")

    def test_both_sides_reject_a_go_test_command_without_the_required_flag(self) -> None:
        entries = self.toolchain.probe(require_offline=False).entries(entry_scaffold.declaration(make_scope()))
        argv = ["go", "test", "./..."]
        sample = make_sample()
        sample["command"]["argv"] = argv
        sample["command"]["sha256"] = ur.declared_command_sha256(argv, "backend")
        world = DeclaredWorld(self.root / "world", sample=sample, contracts=entries, credentials=self.credentials(entries))
        self.assertTrue(any("要求 go test 命令含 -count=1" in problem for problem in world.check()), "消费者侧拒绝")
        self.assertTrue(any("go test 命令缺少参数" in problem for problem in entry_gates.declared_contract_problems(entries, argv=argv)), "生产侧同样拒绝")
        vet = ["go", "vet", "./..."]
        sample["command"]["argv"] = vet
        sample["command"]["sha256"] = ur.declared_command_sha256(vet, "backend")
        other = DeclaredWorld(self.root / "other", sample=sample, contracts=entries, credentials=self.credentials(entries))
        self.assertEqual(other.check(), [], "不是 go test 的命令（go vet）两侧都不要求 -count=1")
        self.assertEqual(entry_gates.declared_contract_problems(entries, argv=vet), [])

    def test_offline_requirements_on_the_toolchain_contract_are_checked_against_the_sealed_environment(self) -> None:
        """打开离线要求时，入口门禁侧的 toolchain:go 条目带 requires.env_equals；消费者侧对照记录封存的单元环境，没真的注入就不可承接。"""

        self.toolchain.go_env.update({"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOTOOLCHAIN": "local"})
        entries = self.toolchain.probe(require_offline=True).entries(entry_scaffold.declaration(make_scope()))
        go = next(entry for entry in entries if entry["name"] == "contract:toolchain:go")["detail"]
        self.assertEqual(go["requires"], {"env_equals": entry_gates.DECLARED_OFFLINE_ENV})
        self.assertEqual(ur._declared_contract_entry_problems(next(e for e in entries if e["name"] == "contract:toolchain:go"), "toolchain", "go"), [])
        world = DeclaredWorld(self.root / "world", contracts=entries, credentials=self.credentials(entries))
        problems = world.check()
        self.assertTrue(any("要求环境变量 GOPROXY=off" in problem for problem in problems), "单元环境里没有注入：" + str(problems))
        spec = {**world.spec(), "env": {"GOMAXPROCS": "2", **entry_gates.DECLARED_OFFLINE_ENV}}
        self.assertEqual(world.check(world.build(spec=spec)), [], "单元环境注入了三个变量")


# ---------------------------------------------------------------------------
# 消费者核验：合格记录、合同凭证（T13）、篡改（T17）、历史重放（T18）、derived_pass、头部包装
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def no_external_reads(allowed_root: Path):
    """历史重放的断言工具：区段内读 ``allowed_root`` 之外的任何磁盘路径、起任何子进程、联网，都当场抛 AssertionError。"""

    root = str(Path(allowed_root).resolve())

    def check(path, what: str) -> None:
        if isinstance(path, int):
            return
        text = os.fspath(path)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if not (text == root or text.startswith(root + os.sep)):
            raise AssertionError(f"历史重放读了记录库之外的外部事实：{what}({text!r})")

    def guarded(original, name):
        def wrapper(path, *args, **kwargs):
            check(path, name)
            return original(path, *args, **kwargs)
        return wrapper

    def guarded_method(original, name):
        def wrapper(self, *args, **kwargs):
            check(self, name)
            return original(self, *args, **kwargs)
        return wrapper

    def forbidden(name):
        def wrapper(*args, **kwargs):
            raise AssertionError(f"历史重放不得调用 {name}")
        return wrapper

    patches = [mock.patch("builtins.open", guarded(open, "open")), mock.patch("io.open", guarded(open, "io.open")),
               mock.patch("os.open", guarded(os.open, "os.open")), mock.patch("os.stat", guarded(os.stat, "os.stat")),
               mock.patch("os.lstat", guarded(os.lstat, "os.lstat")), mock.patch("os.scandir", guarded(os.scandir, "os.scandir")),
               mock.patch("os.listdir", guarded(os.listdir, "os.listdir")), mock.patch("os.readlink", guarded(os.readlink, "os.readlink")),
               mock.patch("os.path.exists", guarded(os.path.exists, "os.path.exists")), mock.patch("os.path.isfile", guarded(os.path.isfile, "os.path.isfile")),
               mock.patch("os.path.isdir", guarded(os.path.isdir, "os.path.isdir")), mock.patch("os.path.islink", guarded(os.path.islink, "os.path.islink")),
               mock.patch("os.path.realpath", guarded(os.path.realpath, "os.path.realpath")),
               mock.patch("subprocess.run", forbidden("subprocess.run")), mock.patch("subprocess.Popen", forbidden("subprocess.Popen")),
               mock.patch("subprocess.check_output", forbidden("subprocess.check_output")), mock.patch("subprocess.call", forbidden("subprocess.call")),
               mock.patch("shutil.which", forbidden("shutil.which")), mock.patch("socket.socket.connect", forbidden("socket.connect"))]
    for name in ("exists", "is_file", "is_dir", "is_symlink", "stat", "lstat", "read_bytes", "read_text", "open", "iterdir", "resolve", "glob", "rglob"):
        patches.append(mock.patch.object(Path, name, guarded_method(getattr(Path, name), f"Path.{name}")))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


class AcceptanceTests(DeclaredTestCase):
    def test_a_well_formed_declared_record_is_accepted_in_every_mode(self) -> None:
        world = self.world
        self.assertEqual(world.check(), [])
        self.assertEqual(world.check(thorough=False), [])
        for require in (False, True):
            for historical in (False, True):
                with self.subTest(require=require, historical=historical):
                    self.assertEqual(world.check(require_read_audit=require, historical_read_audit=historical), [],
                                     "声明级记录的读集判据是声明覆盖，不再要求旧的 all-file-paths/v1")
        decision = ur.evaluate(world.store, world.base["current"], world.base["facts"])
        self.assertTrue(decision.inherit, decision.reasons)
        self.assertEqual(decision.record["record_sha256"], world.base["record"]["record_sha256"])

    def test_a_declared_record_in_a_manifest_verifies_historically(self) -> None:
        world = self.world
        record = world.base["record"]
        entry = {"unit_id": world.unit_id, "unit_type": "command", "disposition": "inherited", "record_path": str(world.base["path"]),
                 "record_sha256": record["record_sha256"], "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"],
                 "test_group": None}
        manifest = ur.build_manifest(run_id="verify-run", mode=ur.FULL_SET_PASS, planned_units=[world.unit_id], units=[entry], test_groups={},
                                     diagnostic=[], policy_sha256="p" * 64, environment=world.environment,
                                     environment_sha256=ur.entries_sha256(world.environment), executor=world.executor,
                                     inheritance_max_age_hours=168.0, decided_at_utc="2026-10-15T01:00:00Z")
        for historical in (False, True):
            self.assertEqual(ur.verify_manifest(manifest, store=world.store, historical=historical), [], historical)
        tampered = world.reseal(lambda body: body["read_audit"].update(undeclared_count=2))
        forged = dict(entry, record_path=str(tampered["path"]), record_sha256=tampered["record"]["record_sha256"])
        manifest = ur.build_manifest(run_id="verify-run-2", mode=ur.FULL_SET_PASS, planned_units=[world.unit_id], units=[forged], test_groups={},
                                     diagnostic=[], policy_sha256="p" * 64, environment=world.environment,
                                     environment_sha256=ur.entries_sha256(world.environment), executor=world.executor,
                                     inheritance_max_age_hours=168.0, decided_at_utc="2026-10-15T01:00:00Z")
        problems = ur.verify_manifest(manifest, store=world.store, historical=True)
        self.assertTrue(problems and "承接核验不通过" in problems[0], problems)

    def test_the_route_is_taken_only_for_the_scope_entry_and_never_recurses(self) -> None:
        world = self.world
        item = world.base
        with mock.patch.object(ur, "_check_declared_route", wraps=ur._check_declared_route) as route:
            self.assertEqual(world.check(), [])
            self.assertEqual(route.call_count, 1, "走一次头部包装，递归调用原函数时不再进入")
            legacy_inputs = [entry for entry in item["current"].inputs if entry["name"] != ur.DECLARED_SCOPE_ENTRY]
            legacy = dataclasses.replace(item["current"], inputs=legacy_inputs, inputs_sha256=ur.entries_sha256(legacy_inputs))
            ur.check_record(world.store, item["path"], item["record"], legacy, item["facts"])
            self.assertEqual(route.call_count, 1, "当前输入里没有 declared-inputs-scope 条目，不进新分支")
            flagged = dataclasses.replace(item["facts"], declared_route=True)
            ur.check_record(world.store, item["path"], item["record"], item["current"], flagged)
            self.assertEqual(route.call_count, 1, "declared_route 置位时不再进头部包装")

    def test_generic_checks_are_not_bypassed_and_facts_are_not_mutated(self) -> None:
        world = self.world
        item = world.base
        facts_before = dict(vars(item["facts"]))
        cases = {
            "策略版本": (dataclasses.replace(item["facts"], policy_sha256="q" * 64), "调度策略版本变了"),
            "环境指纹": (dataclasses.replace(item["facts"], environment_sha256="q" * 64), "环境变了"),
            "执行器": (dataclasses.replace(item["facts"], executor={"files": {}, "sha256": "other"}), "执行器变了"),
            "过期": (dataclasses.replace(item["facts"], now=ur.parse_utc(world.COMPLETED) + 3600 * 200), "超过承接期限"),
        }
        for label, (facts, fragment) in cases.items():
            with self.subTest(label):
                problems = ur.check_record(world.store, item["path"], item["record"], item["current"], facts)
                self.assertTrue(any(fragment in problem for problem in problems), problems)
        wrong_spec = dataclasses.replace(item["current"], spec_sha256="z" * 64)
        self.assertTrue(any("单元规格变了" in problem for problem in ur.check_record(world.store, item["path"], item["record"], wrong_spec, item["facts"])))
        wrong_inputs = dataclasses.replace(item["current"], inputs_sha256="z" * 64)
        self.assertTrue(any("输入变了" in problem for problem in ur.check_record(world.store, item["path"], item["record"], wrong_inputs, item["facts"])))
        diagnostic = world.reseal(lambda body: body.update(kind="diagnostic"))
        self.assertTrue(any("诊断执行的记录永不承接" in problem for problem in world.check(diagnostic)))
        self.assertEqual(dict(vars(item["facts"])), facts_before, "头部包装复制 facts 再改，不改调用方的对象")
        self.assertFalse(item["facts"].declared_route)

    def test_thorough_false_stops_at_the_cheap_generic_problems(self) -> None:
        """与原 check_record 一致：便宜的核对已有问题时不做贵的核对；声明级的证据读取与重算属于贵的核对。"""

        world = self.world
        item = world.base
        stored = world.store.log_path(world.evidence_digest)
        stored.chmod(0o600)
        stored.write_bytes(stored.read_bytes() + b" ")
        facts = dataclasses.replace(item["facts"], policy_sha256="q" * 64)
        cheap = ur.check_record(world.store, item["path"], item["record"], item["current"], facts, thorough=False)
        self.assertEqual(len(cheap), 1)
        self.assertFalse(any("声明级承接" in problem for problem in cheap), "便宜的核对已有问题时不再读证据")
        full = ur.check_record(world.store, item["path"], item["record"], item["current"], facts, thorough=True)
        self.assertTrue(any("调度策略版本变了" in problem for problem in full) and any("采样证据缺失或摘要漂移" in problem for problem in full), full)
        self.assertTrue(any("摘要漂移" in problem for problem in world.check(thorough=False)), "没有通用问题时，thorough=False 同样核验声明级证据")

    def test_a_missing_record_still_reports_the_original_reason(self) -> None:
        world = self.world
        item = world.base
        self.assertEqual(ur.check_record(world.store, item["path"], None, item["current"], item["facts"]), ["记录读不出来"])


class DerivedPassTests(DeclaredTestCase):
    def test_derived_pass_accepts_the_new_scope_and_keeps_the_old_rules(self) -> None:
        record = self.world.base["record"]
        self.assertEqual(ur.derived_pass(record), (True, ""))
        for label, change in {"有未声明项": {"undeclared_count": 1}, "状态失败": {"status": "failed"}, "计数缺失": {"undeclared_count": None},
                              "要求重跑": {"status": "reexecute_required"}}.items():
            with self.subTest(label):
                self.assertEqual(ur.derived_pass(dict(record, read_audit={**record["read_audit"], **change})), (False, "读集审计未通过"))
        old = {**record, "read_audit": {"status": "passed", "undeclared_count": 0, "coverage_scope": "all-file-paths/v1", "coverage_complete": True}}
        self.assertEqual(ur.derived_pass(old), (True, ""), "旧范围 all-file-paths/v1 的判据不变：未声明数为零且状态通过")
        self.assertEqual(ur.derived_pass({**old, "read_audit": {**old["read_audit"], "undeclared_count": 2}}), (False, "读集审计未通过"))
        self.assertEqual(ur.derived_pass({**record, "exit_code": 1}), (False, "退出状态不是成功"))


# ---------------------------------------------------------------------------
# T13：合同凭证与合同条目
# ---------------------------------------------------------------------------


class ContractCredentialTests(DeclaredTestCase):
    def problems(self, mutate) -> list[str]:
        return self.world.check(self.world.reseal(mutate))

    def credentials(self, change=None):
        items = copy.deepcopy(self.world.credentials)
        if change is not None:
            change(items)
        return lambda body: body.__setitem__("declared_contracts", items)

    def expect(self, mutate, fragment: str) -> list[str]:
        problems = self.problems(mutate)
        self.assertTrue(any(fragment in problem for problem in problems), f"应报「{fragment}」：{problems}")
        return problems

    def test_missing_credentials_are_refused(self) -> None:
        for label, mutate in {"整个键缺失": lambda body: body.pop("declared_contracts"), "空数组": lambda body: body.__setitem__("declared_contracts", []),
                              "None": lambda body: body.__setitem__("declared_contracts", None)}.items():
            with self.subTest(label):
                problems = self.problems(mutate)
                self.assertEqual(sum("缺少合同凭证" in problem for problem in problems), 3, problems)
        self.expect(self.credentials(lambda items: items.pop(0)), "缺少合同凭证 cache:go-build-cache")
        self.expect(self.credentials(lambda items: items.pop(2)), "缺少合同凭证 service-image:postgres")
        self.expect(lambda body: body.__setitem__("declared_contracts", "oops"), "declared_contracts 不是数组")

    def test_verified_before_run_must_be_true_and_precede_the_start(self) -> None:
        for label, value in {"false": False, "字符串 true": "true", "1": 1, "None": None}.items():
            with self.subTest(label):
                self.expect(self.credentials(lambda items, v=value: items[0].update(verified_before_run=v)), "verified_before_run 不是 true")
        self.expect(self.credentials(lambda items: items[0].update(verified_at_utc="2026-10-15T00:00:11Z")), "晚于单元启动时间")
        self.expect(self.credentials(lambda items: items[0].update(verified_at_utc="昨天")), "不可解析")
        self.assertEqual(self.problems(self.credentials(lambda items: items[0].update(verified_at_utc=self.world.STARTED))), [], "与启动同一秒算在启动前")

    def test_observed_must_satisfy_the_contract_rule(self) -> None:
        self.expect(self.credentials(lambda items: items[0]["observed"].update(root_ok=False)), "root_ok")
        self.expect(self.credentials(lambda items: items[0]["observed"].update(root_ok="yes")), "root_ok")
        self.expect(self.credentials(lambda items: items[0]["observed"].update(entries_hint=-1)), "entries_hint")
        self.expect(self.credentials(lambda items: items[1]["observed"].update(go_sum_sha256="0" * 64)), "锁文件摘要不等于记录输入里 file:backend/go.sum")
        self.expect(self.credentials(lambda items: items[1]["observed"].update(go_sum_sha256="short")), "64 位十六进制")
        self.expect(self.credentials(lambda items: items[1]["observed"].update(verify="maybe")), "verify")
        self.expect(self.credentials(lambda items: items[2]["observed"].update(image_id="sha256:" + "0" * 64)), "镜像 Id 复核值")
        self.expect(self.credentials(lambda items: items[2].update(observed={})), "镜像 Id 复核值")
        self.expect(self.credentials(lambda items: items[0].update(observed=[])), "observed 不是对象")
        self.expect(self.credentials(lambda items: items[0]["observed"].update(note="unavailable(rc=1)")), "探测失败")

    def test_credential_set_must_match_the_scope(self) -> None:
        self.expect(self.credentials(lambda items: items.append(dict(items[0]))), "重复")
        self.expect(self.credentials(lambda items: items.append({**items[0], "id": "cache:other"})), "不在声明范围的合同里")
        self.expect(self.credentials(lambda items: items[0].update(kind="mystery")), "kind／id 非法")
        self.expect(self.credentials(lambda items: items[0].update(extra=1)), "字段集合是闭合的")
        self.expect(self.credentials(lambda items: items[0].pop("observed")), "字段集合是闭合的")
        self.expect(self.credentials(lambda items: items[0].update(rule="lock-derived-pnpm")), "rule 与合同条目的规则不一致")
        self.assertEqual(self.problems(self.credentials(lambda items: items[0].update(rule="any-content-addressed"))), [], "rule 与合同条目一致时可带")

    def test_optional_credentials_are_checked_when_present(self) -> None:
        stamp = "2026-10-15T00:00:05Z"
        extra = [{"id": "toolchain:go", "kind": "toolchain", "verified_before_run": True, "verified_at_utc": stamp, "observed": {"ok": True}},
                 {"id": "host-config:gitconfig", "kind": "host-config", "verified_before_run": True, "verified_at_utc": stamp, "observed": {"state": "missing"}},
                 {"id": f"git-history:{COMMIT_A}", "kind": "git-history", "verified_before_run": True, "verified_at_utc": stamp, "observed": {"exists": True}},
                 {"id": "test-demo", "kind": "make-expansion", "verified_before_run": True, "verified_at_utc": stamp, "observed": {}}]
        self.assertEqual(self.problems(self.credentials(lambda items: items.extend(copy.deepcopy(extra)))), [], "工具链等合同的凭证可有可无，有则合格")
        self.expect(self.credentials(lambda items: items.append({**extra[0], "verified_before_run": False})), "verified_before_run")
        self.expect(self.credentials(lambda items: items.append({**extra[2], "id": f"git-history:{COMMIT_B}"})), "不在声明范围的合同里")
        self.expect(self.credentials(lambda items: items.append({**extra[0], "observed": "ok"})), "observed 不是对象")

    def test_requires_are_checked_against_the_sealed_spec_and_environment(self) -> None:
        def with_requires(requires: dict) -> list[dict]:
            entries = make_contract_entries()
            entries[1]["detail"]["requires"] = requires
            return entries

        def world_for(requires: dict, scope_requires: dict | None = None, environment=None) -> DeclaredWorld:
            scope = make_scope()
            if scope_requires is not None:
                scope["requires"] = scope_requires
            world = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)), scope=scope, contracts=with_requires(requires))
            if environment is not None:
                world.environment = environment
                world.base = world.build()
            return world

        self.assertEqual(world_for({"argv_contains": ["-count=1"]}).check(), [], "spec.argv 里有 -count=1")
        self.assertTrue(any("要求命令行含 -count=2" in problem for problem in world_for({"argv_contains": ["-count=2"]}).check()))
        self.assertEqual(world_for({"env_equals": {"GOMAXPROCS": "2"}}).check(), [], "spec.env 里的显式值")
        self.assertTrue(any("要求环境变量 GOMAXPROCS=3" in problem for problem in world_for({"env_equals": {"GOMAXPROCS": "3"}}).check()))
        self.assertTrue(any("环境指纹里不是这个值" in problem for problem in world_for({"env_equals": {"GOPROXY": "off"}}).check()), "两处都没有")
        fingerprint = [ur.value_entry("environment", "executor:arch", "arm64"),
                       {"category": "environment", "name": "envvar:GOPROXY", "sha256": ur.sha256_bytes(b"GOPROXY=off"), "detail": {"name": "GOPROXY"}}]
        self.assertEqual(world_for({"env_equals": {"GOPROXY": "off"}}, environment=fingerprint).check(), [], "单元环境没写时看环境指纹")
        self.assertTrue(any("环境指纹里不是这个值" in problem for problem in world_for({"env_equals": {"GOPROXY": "direct"}}, environment=fingerprint).check()))
        scope_level = {"argv_contains": ["-count=1"], "env_equals": {"GOMAXPROCS": "2"}}
        self.assertEqual(world_for({"argv_contains": ["-count=1"]}, scope_level).check(), [], "声明范围级 requires 通过")
        self.assertTrue(any("声明范围的 requires" in problem for problem in
                            world_for({"argv_contains": ["-count=1"]}, {"env_equals": {"GOFLAGS": "-mod=readonly"}}).check()))
        # go_test_argv_contains（入口门禁侧的 go-build-cache 合同用它）：命令是 go test 时才要求这些参数，且参数要出现在 go test 之后。
        def with_go_test(flags: list[str], argv: list[str] | None = None, launcher: list[str] | None = None) -> list[str]:
            sample = make_sample(launcher=launcher)
            if argv is not None:
                sample["command"]["argv"] = argv
                sample["command"]["sha256"] = ur.declared_command_sha256(argv, "backend")
            entries = make_contract_entries()
            entries[1]["detail"]["requires"] = {"go_test_argv_contains": flags}
            return DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)), sample=sample, contracts=entries).check()

        self.assertEqual(with_go_test(["-count=1"]), [], "go test ./... -count=1 满足")
        self.assertTrue(any("要求 go test 命令含 -count=2" in problem for problem in with_go_test(["-count=2"])))
        self.assertEqual(with_go_test(["-count=1"], argv=["go", "vet", "./..."]), [], "命令不是 go test：不要求（同一个构建缓存也被 go vet、lint 使用）")
        self.assertTrue(any("要求 go test 命令含 -count=1" in problem for problem in with_go_test(["-count=1"], argv=["go", "test", "./..."])))
        self.assertEqual(with_go_test(["-count=1"], argv=["go", "test", "./...", "-count=1"], launcher=["unshare", "-m"]), [], "有启动前缀也认")
        self.assertTrue(with_go_test(["-count=1"], argv=["go", "test", "./..."], launcher=["unshare", "-m"]), "有启动前缀同样要求")
        self.assertTrue(with_go_test(["-count=1"], argv=["-count=1", "go", "test", "./..."]), "参数出现在 go test 之前不算")
        self.assertEqual(with_go_test(["-count=1"], argv=["sh", "-c", "go test ./..."]), [], "go test 藏在 sh -c 的字符串里认不出来，不要求（与入口门禁侧同一判法）")

        # 合同条目里的 requires 不限缓存合同：打开离线要求时工具链合同带 env_equals，同样对照封存的环境。
        def with_toolchain_requires(requires: dict, environment=None) -> list[str]:
            entries = make_contract_entries()
            entries[0]["detail"]["requires"] = requires
            world = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)), contracts=entries)
            if environment is not None:
                world.environment = environment
                world.base = world.build()
            return world.check()

        offline = {"env_equals": {"GOPROXY": "off"}}
        problems = with_toolchain_requires(offline)
        self.assertTrue(any("合同 contract:toolchain:go 的 requires 要求环境变量 GOPROXY=off" in problem for problem in problems), problems)
        self.assertEqual(with_toolchain_requires(offline, environment=fingerprint), [], "环境指纹里有 GOPROXY=off")
        self.assertTrue(any("字段集合是闭合的" in problem for problem in with_toolchain_requires({"bad": 1})))

        # 记录里的 environment 被改成不是数组：不抛异常，requires 核对当作没有环境指纹，报「指纹里不是这个值」。
        world = world_for({"env_equals": {"GOPROXY": "off"}})
        for bad in (5, "x", {"a": 1}, None):
            item = world.reseal(lambda body, bad=bad: body.__setitem__("environment", bad))
            problems = world.check(item)
            self.assertTrue(any("环境指纹里不是这个值" in problem for problem in problems), (bad, problems))

    def test_lock_derived_pnpm_requires_the_installed_lock_to_equal_the_locked_file(self) -> None:
        directory = Path(tempfile.mkdtemp(dir=self._directory.name))

        def frontend(installed: str, lock: str) -> list[str]:
            world = DeclaredWorld(directory / f"w{len(list(directory.iterdir()))}", scope=frontend_scope(), sample=frontend_sample(),
                                  contracts=frontend_contract_entries(), credentials=frontend_credentials(installed, lock), unit_id="frontend:lint", cwd_rel=".")
            return world.check()

        self.assertEqual(frontend(FRONTEND_LOCK_SHA, FRONTEND_LOCK_SHA), [])
        other = SHA("另一份锁文件")
        self.assertTrue(any("安装结果与锁文件不一致" in problem for problem in frontend(other, FRONTEND_LOCK_SHA)), "node_modules 里的锁副本与锁文件不同")
        self.assertTrue(any("锁文件摘要不等于记录输入里 file:frontend/pnpm-lock.yaml" in problem for problem in frontend(other, other)),
                        "两者相等但不是输入里单文件条目的摘要：不是真实依赖")
        self.assertTrue(any("64 位十六进制" in problem for problem in frontend("short", FRONTEND_LOCK_SHA)))

    def test_installed_dirs_must_be_bound_to_a_dependency_install_contract(self) -> None:
        """node_modules 里的未跟踪读取，唯一的凭据是它绑定的合同；合同必须真的是「安装结果由锁文件决定」那一类，否则一个空洞的合同
        （比如 any-scratch）就能放行任意未跟踪读取。"""

        directory = Path(tempfile.mkdtemp(dir=self._directory.name))

        def frontend_world(entries, credentials, scope=None) -> DeclaredWorld:
            return DeclaredWorld(directory / f"w{len(list(directory.iterdir()))}", scope=scope or frontend_scope(), sample=frontend_sample(), contracts=entries,
                                 credentials=credentials, unit_id="frontend:lint", cwd_rel=".")

        real = frontend_credentials(FRONTEND_LOCK_SHA, FRONTEND_LOCK_SHA)
        self.assertEqual(frontend_world(frontend_contract_entries(), real).check(), [], "夹具有效：未跟踪读取 node_modules + lock-derived-pnpm 合同")
        stamp = "2026-10-15T00:00:05Z"
        for label, role, rule, observed in (("暂存", "scratch", "any-scratch", {"root_ok": True}),
                                            ("性能缓存", "performance-cache", "any-content-addressed", {"root_ok": True}),
                                            ("依赖内容（go.sum 那类）", "dependency-content", "lock-derived-go-sum", {"go_sum_sha256": FRONTEND_LOCK_SHA, "verify": "ok"})):
            entries = frontend_contract_entries()
            entries[2] = contract_entry("cache", "node-modules", role=role, location="tree", rule=rule, lock_file="frontend/pnpm-lock.yaml")
            credentials = [{"id": "cache:node-modules", "kind": "cache", "verified_before_run": True, "verified_at_utc": stamp, "observed": observed}]
            problems = frontend_world(entries, credentials).check()
            self.assertEqual([problem for problem in problems if "不是依赖安装结果合同" not in problem], [], f"{label}：凭证本身合格，唯一的问题是合同种类不对：{problems}")
            self.assertEqual(len(problems), 1, label)
        # 没有登记安装目录时同样的访问是未跟踪读取，整条采样的覆盖不成立。
        bare = frontend_scope()
        del bare["repo"]["installed_dirs"]
        problems = frontend_world(frontend_contract_entries(), real, bare).check()
        self.assertTrue(any("重算读集覆盖不成立" in problem and "C1-untracked-read" in problem for problem in problems), problems)

    def test_contract_entries_must_match_the_scope_and_be_well_formed(self) -> None:
        world = self.world
        entries = make_contract_entries()

        def with_entries(contracts: list[dict]) -> list[str]:
            inputs = world.inputs(contracts=contracts)
            return world.check(world.build(inputs=inputs))

        self.assertEqual(with_entries(entries), [])
        names = [entry["name"] for entry in entries]
        for index, name in enumerate(names):
            problems = with_entries(entries[:index] + entries[index + 1:])
            self.assertTrue(any(f"缺少声明范围要求的合同条目 {name}" in problem for problem in problems), name)
        extra = contract_entry("cache", "unlisted", role="scratch", location="tmp", rule="any-scratch")
        self.assertTrue(any("多出声明范围没有要求的合同条目 contract:cache:unlisted" in problem for problem in with_entries(entries + [extra])))

        def broken(index: int, **changes) -> list[str]:
            copies = copy.deepcopy(entries)
            copies[index]["detail"].update(changes)
            return with_entries(copies)

        cases = [
            ("工具链缺版本行", broken(0, version_line=""), "缺少版本行"), ("工具链可执行文件摘要不是十六进制", broken(0, executable_sha256="xyz"), "executable_sha256"),
            ("工具链探测失败", broken(0, version_line="unavailable(rc=127)"), "探测失败"),
            ("缓存规则未知", broken(1, rule="snapshot"), "规则 rule 未知"), ("缓存角色与规则不配", broken(1, role="scratch"), "角色 role"),
            ("缓存 location 非法", broken(1, location="moon"), "location"), ("缓存 requires 形状非法", broken(1, requires={"bad": 1}), "字段集合是闭合的"),
            ("缓存规则是数组（不可哈希，不得抛 TypeError）", broken(1, rule=["any-scratch"]), "规则 rule 未知"), ("缓存规则是对象", broken(1, rule={}), "规则 rule 未知"),
            ("缓存 location 是数组（不可哈希）", broken(1, location=["work"]), "location"), ("缓存 location 是对象", broken(1, location={}), "location"),
            ("缓存 lock_file 非法", broken(2, lock_file="/abs"), "lock_file"),
            ("镜像 Id 格式不对", broken(3, image_id="d" * 64), "image_id"), ("镜像探测失败", broken(3, image_id="unavailable(rc=1)"), "探测失败"),
            ("镜像缺引用", broken(3, reference=""), "reference"),
            ("detail 的 type 不符", broken(4, type="cache"), "detail 须带 schema_version"), ("detail 的 id 不符", broken(4, id="other"), "detail 须带 schema_version"),
            ("detail 的 schema 不符", broken(4, schema_version="declared-contract/v0"), "detail 须带 schema_version"),
            ("host-config 探测失败", broken(4, state="unavailable(rc=1)"), "探测失败"),
        ]
        for label, problems, fragment in cases:
            with self.subTest(label):
                self.assertTrue(any(fragment in problem for problem in problems), f"{label}：{problems}")
        for sha in ("", "short", "G" * 64, None):
            copies = copy.deepcopy(entries)
            copies[0]["sha256"] = sha
            self.assertTrue(any("摘要不是 64 位十六进制" in problem for problem in with_entries(copies)), repr(sha))
        copies = copy.deepcopy(entries)
        del copies[0]["detail"]
        self.assertTrue(any("缺少 detail" in problem for problem in with_entries(copies)))


# ---------------------------------------------------------------------------
# T17：篡改一律不承接
# ---------------------------------------------------------------------------


class TamperTests(DeclaredTestCase):
    def reject(self, item: dict, fragment: str, world: DeclaredWorld | None = None) -> list[str]:
        problems = (world or self.world).check(item)
        self.assertTrue(any(fragment in problem for problem in problems), f"应报「{fragment}」：{problems}")
        return problems

    def mutate_entry(self, mutate) -> dict:
        def apply(body):
            entry = next(item for item in body["inputs"] if item["name"] == ur.DECLARED_SCOPE_ENTRY)
            mutate(entry, body)
        return self.world.reseal(apply)

    def test_one_flipped_byte_in_the_stored_evidence_is_caught(self) -> None:
        stored = self.world.store.log_path(self.world.evidence_digest)
        raw = bytearray(stored.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        stored.chmod(0o600)
        stored.write_bytes(bytes(raw))
        self.reject(self.world.base, "采样证据缺失或摘要漂移")

    def test_missing_or_unreadable_evidence_is_caught(self) -> None:
        stored = self.world.store.log_path(self.world.evidence_digest)
        stored.chmod(0o600)
        stored.unlink()
        self.reject(self.world.base, "采样证据缺失或摘要漂移")
        # 摘要对但内容不是证据：登记摘要指向一份合法 JSON 之外的文件。
        other = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)))
        path = other.directory / "junk.bin"
        path.write_bytes(b"\xff\xfe not json")
        digest = ur.file_sha256(path)
        other.store.put_log(path, digest)
        item = other.build(declaration=other.declaration(evidence_digest=digest))
        self.reject(item, "读不出来或格式非法", other)

    def test_the_recorded_conclusion_is_never_trusted_only_the_recomputation(self) -> None:
        """证据里有一条未声明的读取，但记录自称 undeclared_count＝0、status＝passed、摘要和绑定全部自洽：重算覆盖不 ok，拒绝。"""

        sample = make_sample()
        sample["repo_accesses"].append(["{TREE}/frontend/src/secret.ts", ["read"], True])
        world = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)), sample=sample)
        self.assertEqual(world.base["record"]["read_audit"]["undeclared_count"], 0)
        problems = self.reject(world.base, "重算读集覆盖不成立", world)
        self.assertTrue(any("C1-undeclared" in problem for problem in problems), problems)
        # 没有这条未声明读取的同一世界是通过的（排除夹具本身的问题）。
        self.assertEqual(self.world.check(), [])

    def test_an_honest_failed_audit_is_not_inheritable_either(self) -> None:
        item = self.world.reseal(lambda body: body["read_audit"].update(undeclared_count=2, status="failed", coverage_complete=False))
        problems = self.world.check(item)
        self.assertTrue(any("读集审计未通过" in problem for problem in problems) and any("read_audit 不是通过的" in problem for problem in problems), problems)

    def test_read_audit_fields_are_checked_one_by_one(self) -> None:
        cases = {
            "覆盖范围写成旧值": ({"coverage_scope": "all-file-paths/v1"}, "read_audit 不是通过的"),
            "覆盖未闭合": ({"coverage_complete": False}, "read_audit 不是通过的"), "状态不是通过": ({"status": "failed"}, "read_audit 不是通过的"),
            "未声明数为布尔": ({"undeclared_count": False}, "read_audit 不是通过的"), "有未声明明细": ({"undeclared": [{"path": "/x"}]}, "read_audit 不是通过的"),
            "绑定了别的输入摘要": ({"inputs_sha256": "0" * 64}, "绑定的 inputs_sha256 与记录不符"), "采样来源不明": ({"sample_origin": "guess"}, "sample_origin"),
            "仓库根缺失": ({"repo_root": None}, "repo_root"), "仓库根是相对路径": ({"repo_root": "repo"}, "repo_root"),
            "轨迹摘要缺失": ({"trace": {"path": "x"}}, "trace.sha256"), "轨迹摘要太短": ({"trace": {"sha256": "abc"}}, "trace.sha256"),
        }
        for label, (change, fragment) in cases.items():
            with self.subTest(label):
                self.reject(self.world.reseal(lambda body, c=change: body["read_audit"].update(c)), fragment)
        self.reject(self.world.reseal(lambda body: body.pop("read_audit")), "记录没有 read_audit")
        self.reject(self.world.reseal(lambda body: body.__setitem__("read_audit", "passed")), "记录没有 read_audit")

    def test_sample_binding_must_match_the_registered_unit_command_scope_and_evidence(self) -> None:
        for key in ("unit_id", "command_sha256", "scope_sha256", "evidence_sha256"):
            with self.subTest(key):
                item = self.world.reseal(lambda body, k=key: body["read_audit"]["sample_binding"].update({k: "0" * 64 if k != "unit_id" else "other"}))
                self.reject(item, "证据绑定")
        self.reject(self.world.reseal(lambda body: body["read_audit"]["sample_binding"].pop("scope_sha256")), "字段集合是闭合的")
        self.reject(self.world.reseal(lambda body: body["read_audit"]["trace"].update(sha256="1" * 64)), "证据绑定")
        self.reject(self.world.reseal(lambda body: body["read_audit"].pop("sample_binding")), "必须是对象")

    def test_the_evidence_must_describe_the_command_that_actually_ran(self) -> None:
        world = self.world

        def with_spec(**changes):
            def apply(body):
                body["spec"] = {**body["spec"], **changes}
                body["spec_sha256"] = ur.sha256_json(body["spec"])
            return world.reseal(apply, current=True)

        self.reject(with_spec(argv=["go", "test", "./...", "-count=2"]), "不是采样证据描述的那条命令")
        self.reject(with_spec(argv=["unshare", *world.spec()["argv"]]), "不是采样证据描述的那条命令")
        self.reject(with_spec(cwd=str(world.root / "frontend")), "工作目录不是采样证据描述的那个")
        self.reject(with_spec(cwd=str(world.root)), "工作目录不是采样证据描述的那个")
        self.reject(with_spec(cwd=None), "工作目录不是采样证据描述的那个")
        self.reject(with_spec(type="test"), "只适用于命令单元")
        # spec 对象被改而 spec_sha256 没改：通用比较（摘要对摘要）发现不了，声明级核验用封存的 spec 重算摘要。
        def forged(body):
            body["spec"] = {**body["spec"], "argv": ["go", "test", "./..."]}
        self.reject(world.reseal(forged), "spec 与 spec_sha256 不符")
        # 证据属于另一个单元，或登记的命令摘要与证据不符。
        other = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)), sample=make_sample("backend:other"))
        self.reject(other.base, "属于别的单元", other)
        mismatch = DeclaredWorld(Path(tempfile.mkdtemp(dir=self._directory.name)))
        declaration = mismatch.declaration()
        declaration["declared_sample"]["command_sha256"] = "9" * 64
        self.reject(mismatch.build(declaration=declaration), "命令摘要与范围条目登记的不符", mismatch)
        sampler = copy.deepcopy(mismatch.sample)
        sampler["sampled"]["sampler_sha256"] = "8" * 64
        digest = mismatch.put_evidence(sampler)
        self.reject(mismatch.build(sample=sampler, declaration=mismatch.declaration(sample=sampler, evidence_digest=digest)), "采样器摘要", mismatch)

    def test_scope_entry_tampering(self) -> None:
        def swap_scope(entry, body):
            entry["detail"]["scope"]["repo"]["files"].append("docs/other.md")

        self.reject(self.mutate_entry(swap_scope), "摘要与它自带的范围全文、采样引用不符")
        self.reject(self.mutate_entry(lambda entry, body: entry["detail"]["sample"].update(evidence_sha256="3" * 64)), "摘要与它自带的范围全文、采样引用不符")
        self.reject(self.mutate_entry(lambda entry, body: entry["detail"].update(extra=1)), "条目的格式非法")
        self.reject(self.mutate_entry(lambda entry, body: entry.update(category="tests")), "条目的格式非法")
        self.reject(self.mutate_entry(lambda entry, body: entry["detail"]["scope"].update(network="any")), "声明范围或采样引用非法")
        self.reject(self.mutate_entry(lambda entry, body: entry["detail"].pop("scope")), "条目的格式非法")
        # 条目被删除或重复：输入摘要与明细不符，或缺条目。
        self.reject(self.world.reseal(lambda body: body["inputs"].remove(next(i for i in body["inputs"] if i["name"] == ur.DECLARED_SCOPE_ENTRY))),
                    "重算出的摘要与 inputs_sha256 不符")
        # 输入明细、摘要、当前输入全部一致地多了一份范围条目（摘要都能对上）：恰含一个、不得重名。
        world = self.world
        duplicated = world.base["record"]["inputs"] + [copy.deepcopy(next(i for i in world.base["record"]["inputs"] if i["name"] == ur.DECLARED_SCOPE_ENTRY))]
        self.reject(world.build(inputs=duplicated), "重名条目")
        missing = [item for item in world.base["record"]["inputs"] if item["name"] != ur.DECLARED_SCOPE_ENTRY]
        problems = world.check({**world.build(inputs=missing), "current": world.base["current"]})
        self.assertTrue(any("输入变了" in problem for problem in problems), "记录没有范围条目、当前输入有：通用的输入摘要比较先拒绝")

    def test_inputs_edited_without_updating_the_digest_are_caught(self) -> None:
        self.reject(self.world.reseal(lambda body: body["inputs"].pop()), "重算出的摘要与 inputs_sha256 不符")
        self.reject(self.world.reseal(lambda body: body["inputs"][0].pop("sha256")), "输入明细格式非法")
        self.reject(self.world.reseal(lambda body: body.__setitem__("inputs", "x")), "没有输入明细")

    def test_replacing_the_whole_scope_still_fails_the_consumer_input_comparison(self) -> None:
        """攻击者换一份合法范围、重算条目摘要、重算 inputs_sha256 并重新封存：消费者拿当前声明算出的输入摘要来比，不等。"""

        other_scope = make_scope()
        other_scope["repo"]["list_ok"] = []
        world = self.world
        replacement = world.build(declaration=world.declaration(scope=other_scope))
        problems = world.check({**replacement, "current": world.base["current"]})
        self.assertTrue(any("输入变了" in problem and ur.DECLARED_SCOPE_ENTRY in problem for problem in problems), problems)

    def test_a_declared_current_against_a_legacy_record_is_refused(self) -> None:
        world = self.world
        legacy_inputs = ur.declared_inputs(world.repo, {"ranges": [ur.standard_ranges("pkg")[key] for key in ur.COMMAND_RANGES], "head": True})
        legacy = world.build(inputs=legacy_inputs, record_overrides={"read_audit": None})
        problems = world.check({**legacy, "current": world.base["current"]})
        self.assertTrue(any("输入变了" in problem for problem in problems), problems)
        self.assertTrue(any("声明级承接：记录须恰含一个 declared-inputs-scope 条目" in problem for problem in problems),
                        "旧式记录没有范围条目：声明级核验同样拒绝，不会当成通用记录放过")

    def test_every_single_point_malformation_of_a_sealed_record_gives_problems_never_an_exception(self) -> None:
        """穷举式单点畸形（见 single_point_variants）：封存记录里的输入明细（含内嵌的声明范围与合同条目）、read_audit、运行前凭证
        的每个位置变成任何一种畸形值或被删除，check_record 都返回问题列表而不是抛异常——封存物是不可信输入，核验进程不能被它弄崩。
        后端风格（工具链、缓存、镜像、git 历史）与前端风格（安装目录、lock-derived-pnpm）各扫一遍；两种历史重放模式都扫。"""

        directory = Path(tempfile.mkdtemp(dir=self._directory.name))
        frontend = DeclaredWorld(directory / "frontend", scope=frontend_scope(), sample=frontend_sample(), contracts=frontend_contract_entries(),
                                 credentials=frontend_credentials(FRONTEND_LOCK_SHA, FRONTEND_LOCK_SHA), unit_id="frontend:lint", cwd_rel=".")
        self.assertEqual(frontend.check(), [], "前端夹具有效")
        checked = 0
        for world, keys in ((self.world, ("inputs", "read_audit", "declared_contracts")), (frontend, ("inputs", "declared_contracts"))):
            for key in keys:
                for place, replaced in single_point_variants(world.base["record"][key]):
                    item = world.reseal(lambda body, key=key, replaced=replaced: body.__setitem__(key, replaced))
                    for historical in (False, True):
                        facts = dataclasses.replace(item["facts"], historical_read_audit=historical)
                        try:
                            problems = ur.check_record(world.store, item["path"], item["record"], item["current"], facts, thorough=True)
                        except Exception as error:  # noqa: BLE001 - 目标就是任何异常
                            self.fail(f"记录 {key} 的位置 {place} 的畸形值让 check_record 抛了 {type(error).__name__}：{error}")
                        self.assertTrue(isinstance(problems, list) and all(isinstance(text, str) for text in problems), (key, place))
                        checked += 1
        self.assertGreater(checked, 4000, "穷举的规模不应悄悄缩水")


# ---------------------------------------------------------------------------
# T18：历史重放只核封存物
# ---------------------------------------------------------------------------


class HistoricalReplayTests(DeclaredTestCase):
    def test_historical_replay_reads_nothing_outside_the_record_store(self) -> None:
        world = self.world
        item = world.base
        # 先证明区段守卫有效：读记录库之外的路径、起子进程都会当场失败。
        with no_external_reads(world.store.root):
            with self.assertRaises(AssertionError):
                Path("/etc/hosts").exists()
            with self.assertRaises(AssertionError):
                subprocess.run(["git", "--version"])
            with self.assertRaises(AssertionError):
                world.root.joinpath("backend", "a.go").read_text()
        with no_external_reads(world.store.root):
            for require in (False, True):
                for historical in (True, False):
                    facts = dataclasses.replace(item["facts"], historical_read_audit=historical, require_read_audit=require)
                    self.assertEqual(ur.check_record(world.store, item["path"], item["record"], item["current"], facts), [], (require, historical))
                    self.assertEqual(ur._check_declared_audit(world.store, item["record"], item["current"], facts), [])
            self.assertEqual(ur.declared_coverage(world.sample, world.scope)["undeclared"], [], "覆盖检查是纯函数")

    def test_replay_does_not_need_the_repo_docker_git_or_a_path(self) -> None:
        world = self.world
        item = world.base
        shutil.rmtree(world.root)
        facts = dataclasses.replace(item["facts"], historical_read_audit=True, require_read_audit=True)
        with mock.patch.dict(os.environ, {"PATH": ""}), mock.patch.object(ur, "_git", side_effect=AssertionError("不得调用 git")), \
                mock.patch.object(ur.RepoIndex, "load", side_effect=AssertionError("不得读仓库")):
            self.assertEqual(ur.check_record(world.store, item["path"], item["record"], item["current"], facts), [])

    def test_a_tampered_record_is_still_refused_in_historical_replay(self) -> None:
        world = self.world
        stored = world.store.log_path(world.evidence_digest)
        stored.chmod(0o600)
        stored.write_bytes(stored.read_bytes() + b"\n")
        facts = dataclasses.replace(world.base["facts"], historical_read_audit=True, require_read_audit=True)
        with no_external_reads(world.store.root):
            problems = ur.check_record(world.store, world.base["path"], world.base["record"], world.base["current"], facts)
        self.assertTrue(any("摘要漂移" in problem for problem in problems), problems)


# ---------------------------------------------------------------------------
# control 四链不变回归（T-C 系列）
#
# control_read_audit.py 在运行时单独加载同目录 unit_records.py 的一份实例，并把该实例的 _audit_module 指到 control 适配器；
# 四条 control 链的声明与记录里既没有 declared_scope 键、也没有 declared-inputs-scope 条目，所以新分支永远走不到，既有函数与
# 常量的行为必须字节不变。这里把基线提交的 unit_records.py 与工作树新版并排加载（都按 control 的方式指到适配器），对同一批
# control 风格的声明、记录、清单逐字比较。
#
# 基线是「声明级承接合入前的主线头」（CONTROL_BASELINE_COMMIT，已含宿主快照 v3 对 unit_records.py 的改动）：之后若有别的
# 工作包也改了 unit_records.py，把这个常量换成合入时的主线头即可——比较的始终是「本章的新增有没有改变既有行为」。
# ---------------------------------------------------------------------------

CONTROL_BASELINE_COMMIT = "86d7b40a30cd299cd3b6cd9cbe7e58ba4f26013a"
# control 两个 sidecar 文件的字节被执行器登记并在消费时核对（改一个字节，历史 control 凭证重放就报「当前四control执行器缺完整
# sidecar 绑定或实际字节漂移」），所以写死摘要：谁改了它们，这个测试就红。
CONTROL_AUDIT_SHA256 = "dc9238ae696880cd12eb17a246a09ac354d6db330b788b66ecf35c3142da2172"
CONTROL_RUNTIME_SHA256 = "c779562bd4a09319ebb1a3d6ade888f65405c464386976a5192dd43bee824010"
CONTROL_SAMPLES = Path("/Users/czs/Developer/sub2apiplus-evidence/claude-control-plane/20261014/control-record-samples-h3")
CONTROL_UNIT = "pre-a3:claude:vc-chain.claude-reconcile"
CONTROL_DEADLINE_UNIT = "pre-a3:claude:vc-chain.claude-deadline"


def baseline_source() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{CONTROL_BASELINE_COMMIT}:tools/ci/unit_records.py"],
                              capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return None


def load_records_module(source: str, name: str, label: str, audit=None) -> types.ModuleType:
    """把一份 unit_records.py 源码作为独立模块实例加载（__file__ 仍指向仓库里的位置，兄弟模块按路径照常加载），并像
    control_read_audit._records_module 那样把实例的 _audit_module 指到审计模块（缺省是 control 适配器）。"""

    module = types.ModuleType(name)
    module.__file__ = str(RECORDS_FILE)
    with mock.patch.dict(sys.modules, {name: module}):
        exec(compile(source, label, "exec"), module.__dict__)  # noqa: S102 - 受控的仓库源码
    module._audit_module = lambda: audit if audit is not None else control_audit
    return module


def outcome(call):
    """一次调用的结果或报错（异常类名＋消息）：两边逐字比较用。"""

    try:
        return "ok", call()
    except Exception as error:  # noqa: BLE001 - 比较的就是报错本身
        return type(error).__name__, str(error)


class FakeAudit:
    """可控的审计模块替身：只实现 check_record 会调用的两个函数，并记下每次调用的参数，两个实例的调用必须逐字相同。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.result: dict = {"coverage_complete": True}
        self.raises: Exception | None = None
        self.host_problems: list[str] = []

    def strict_audit_reads(self, document, inputs, *, repo_root, data_root, historical=False):
        self.calls.append(("strict", json.dumps(document, sort_keys=True), json.dumps(inputs, sort_keys=True), repo_root, data_root, historical))
        if self.raises is not None:
            raise self.raises
        return dict(self.result)

    def host_inputs_inherit_problems(self, trace_document, record_inputs, current_inputs, audit):
        self.calls.append(("host", json.dumps(trace_document, sort_keys=True), json.dumps(record_inputs, sort_keys=True),
                           json.dumps(current_inputs, sort_keys=True), json.dumps(audit, sort_keys=True)))
        return list(self.host_problems)


class ControlInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = baseline_source()
        if source is None:
            raise unittest.SkipTest(f"读不到基线提交 {CONTROL_BASELINE_COMMIT} 的 tools/ci/unit_records.py（浅克隆或不在仓库内）")
        cls.base_source = source
        cls.new_source = RECORDS_FILE.read_text(encoding="utf-8")
        cls.base = load_records_module(cls.base_source, "unit_records_control_baseline", f"{CONTROL_BASELINE_COMMIT}:tools/ci/unit_records.py")
        cls.new = load_records_module(cls.new_source, "unit_records_control_new", str(RECORDS_FILE))

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.tmp = Path(self._directory.name).resolve()

    # ---- T-C1：名字与源码 ----

    def test_t_c1_only_declared_names_were_added_and_no_existing_line_was_rewritten(self) -> None:
        removed = [line for line in difflib.unified_diff(self.base_source.splitlines(), self.new_source.splitlines(), lineterm="", n=0)
                   if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(removed, [], "对 unit_records.py 只新增行：被改写或删除的行会让 Codex 对照批次登记出残差")
        base_names, new_names = set(dir(self.base)), set(dir(self.new))
        self.assertEqual(base_names - new_names, set(), "没有名字被删除")
        added = new_names - base_names
        self.assertTrue(added)
        self.assertTrue(all("declared" in name.lower() for name in added), f"新增的名字都属于声明级章节：{sorted(added)}")
        frozen = {"DECLARED_REGISTRY_SCHEMA", "DECLARED_SCOPE_SCHEMA", "DECLARED_SAMPLE_SCHEMA", "DECLARED_CONTRACT_SCHEMA", "DECLARED_SCOPE_ENTRY",
                  "DECLARED_COVERAGE_SCOPE", "DECLARED_CLASSES", "DECLARED_TOLERATED_UNRESOLVED", "validate_declared_scope", "declared_coverage",
                  "_declared_scope_inputs", "_check_declared_route", "_check_declared_audit"}
        self.assertLessEqual(frozen, added, "冻结接口的名字都在")

    def test_t_c1_existing_definitions_are_ast_identical_except_the_three_insertions(self) -> None:
        def top_level(tree: ast.Module) -> dict:
            found = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    found[node.name] = node
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            found[target.id] = node
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    found[node.target.id] = node
            return found

        base_tree, new_tree = ast.parse(self.base_source), ast.parse(self.new_source)
        base_nodes, new_nodes = top_level(base_tree), top_level(new_tree)
        imports = lambda tree: [ast.dump(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]  # noqa: E731
        self.assertEqual(imports(base_tree), imports(new_tree), "没有新增或改动顶层导入")
        self.assertEqual(ast.get_docstring(base_tree), ast.get_docstring(new_tree), "模块说明未改")
        insertions = {"declared_inputs": "_declared_scope_inputs", "check_record": "_check_declared_route"}
        for name, base_node in base_nodes.items():
            new_node = copy.deepcopy(new_nodes[name])
            if name in insertions:
                inserted = [stmt for stmt in new_node.body if isinstance(stmt, ast.If) and insertions[name] in ast.unparse(stmt)]
                self.assertEqual(len(inserted), 1, f"{name} 头部恰有一处声明级插入")
                self.assertIs(new_node.body[1 if ast.get_docstring(new_node) else 0], inserted[0], f"{name} 的插入紧接在说明之后")
                new_node.body.remove(inserted[0])
            elif name == "RunFacts":
                inserted = [stmt for stmt in new_node.body if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", "") == "declared_route"]
                self.assertEqual(len(inserted), 1)
                self.assertIs(new_node.body[-1], inserted[0], "新字段追加在最后")
                new_node.body.remove(inserted[0])
            self.assertEqual(ast.dump(base_node), ast.dump(new_node), f"{name} 除声明级插入外与基线逐节点相同")

    # ---- T-C2：declared_inputs 与策略校验 ----

    def test_t_c2_declared_inputs_outputs_and_errors_match_for_control_style_declarations(self) -> None:
        root = _repo(self.tmp / "repo", WORLD_FILES)
        host_file = self.tmp / "host-input.txt"
        host_file.write_text("宿主输入\n", encoding="utf-8")
        contract = {"schema_version": "unit-runtime-contract/v1", "temporary_root": str(self.tmp / "private" / "output"), "mask_roots": [],
                    "environment_paths": []}
        legacy_ranges = [ur.standard_ranges("pkg")[key] for key in ur.COMMAND_RANGES]
        legal = [
            {"host_paths": [str(host_file)], "require_read_audit": True, "runtime_contract": contract},
            {"host_paths": [], "require_read_audit": True, "runtime_contract": contract},
            {"host_paths": [str(host_file)], "require_read_audit": True},
            {"host_paths": [str(host_file)]},
            {"ranges": legacy_ranges, "head": True},
            {"files": [{"category": "managed", "path": "Makefile"}], "test_modules": ["pkg/tests/test_claude_unrelated.py"]},
            {"ranges": legacy_ranges, "head": True, "resolved": [{"category": "environment", "name": "resolved:x", "sha256": SHA("x")}]},
        ]
        illegal = [{"bogus": 1}, {"host_paths": "x"}, {"host_paths": ["a", "a"]}, {"host_paths": [1]}, {"require_read_audit": "yes"}, [], None, "x", {},
                   {"files": [{"path": "x"}]}, {"files": [{"category": "c", "path": "../x"}]}, {"files": ["x"]}, {"test_modules": ["/abs/t.py"]},
                   {"runtime_contract": {"bad": 1}}, {"resolved": [{"name": 1}]}, {"declared_samples": 1},
                   {"host_paths": [str(host_file)], "metadata_dependencies": [{"path": str(host_file), "fields": ["mtime"], "basis": "测试依据"}]}]

        def fake_snapshot(path: str) -> dict:
            return {"category": "environment", "name": "host:" + path, "sha256": SHA("snapshot:" + path), "detail": {"path": path}}

        with mock.patch.object(control_audit, "host_snapshot", side_effect=fake_snapshot):
            repo_base, repo_new = self.base.RepoIndex.load(root), self.new.RepoIndex.load(root)
            for declaration in legal + illegal:
                with self.subTest(declaration=str(declaration)[:90]):
                    before = outcome(lambda: self.base.declared_inputs(repo_base, declaration, {}))
                    after = outcome(lambda: self.new.declared_inputs(repo_new, declaration, {}))
                    self.assertEqual(before, after)
            self.assertEqual(outcome(lambda: self.new.declared_inputs(repo_new, legal[0]))[0], "ok", "control 风格声明本身是合法的（夹具有效）")
            self.assertTrue(all(outcome(lambda d=d: self.new.declared_inputs(repo_new, d))[0] == "RecordsError" for d in illegal), "非法声明都是同一种报错")

    def test_t_c2_validate_audit_policy_behaves_identically(self) -> None:
        contract = {"schema_version": "unit-runtime-contract/v1", "temporary_root": str(self.tmp / "private" / "output"), "mask_roots": [],
                    "environment_paths": []}
        unit = {"host_paths": ["/etc/hosts"], "require_read_audit": True, "runtime_contract": contract}
        policies = [{"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {"a": unit}},
                    {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {"a": {**unit, "extra": 1}}},
                    {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {"a": {**unit, "declared_scope": {}}}},
                    {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {"zzz": unit}},
                    {"schema_version": "unit-audit-policy/v1", "default": "other", "units": {}}, [], None]
        for policy in policies:
            with self.subTest(policy=str(policy)[:80]):
                self.assertEqual(outcome(lambda: self.base.validate_audit_policy(policy, {"a"})), outcome(lambda: self.new.validate_audit_policy(policy, {"a"})))

    # ---- control 风格的记录库夹具 ----

    def control_body(self, unit_id: str, run_id: str, trace_digest: str, log_digest: str, *, plain: bool = False) -> dict:
        """control 风格的记录体：all-file-paths/v1 读集、require-read-audit／host／runtime-contract 条目、封存的轨迹与日志；
        ``plain`` 为真时是普通命令记录（没有读集与这些条目）。"""

        inputs = [{"category": "managed", "name": "file:tools/x.py", "sha256": SHA("x"), "detail": {"path": "tools/x.py"}}]
        if not plain:
            inputs += [{"category": "environment", "name": "host:/etc/hosts", "sha256": SHA("host"),
                        "detail": {"schema_version": "unit-host-input/v2", "path": "/etc/hosts"}},
                       {"category": "environment", "name": "runtime-contract", "sha256": SHA("runtime"), "detail": {"schema_version": "unit-runtime-contract/v1"}},
                       self.new.value_entry("policy", "require-read-audit", "all-file-paths/v1")]
        inputs.sort(key=lambda entry: entry["name"])
        environment = [self.new.value_entry("environment", "executor:arch", "arm64")]
        spec = {"type": "command", "unit_id": unit_id, "argv": ["python3", "-m", "x"], "cwd": "/data", "env": {}}
        inputs_sha = self.new.entries_sha256(inputs)
        body = {"unit_id": unit_id, "unit_type": "command", "kind": "formal", "run": {"run_id": run_id},
                "executor": {"files": {"unit_records.py": "1" * 64}, "sha256": "e" * 64}, "policy_sha256": "p" * 64, "environment": environment,
                "environment_sha256": self.new.entries_sha256(environment), "spec": spec, "spec_sha256": self.new.sha256_json(spec), "inputs": inputs,
                "inputs_sha256": inputs_sha, "inheritable": True, "test_ids": [], "tests": None, "passed": True, "exit_code": 0, "signal": None,
                "timed_out": False, "log": {"sha256": log_digest}, "started_at_utc": "2026-10-15T00:00:10Z", "completed_at_utc": "2026-10-15T00:05:00Z"}
        if not plain:
            body["read_audit"] = {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1", "undeclared_count": 0,
                                  "inputs_sha256": inputs_sha, "repo_root": "/repo", "data_root": "/data",
                                  "trace": {"path": "trace.json", "sha256": trace_digest, "stored": "x"}}
        return body

    def control_store(self, store_root: Path, specs: list[dict], *, publish: tuple[str, ...] = ("control-run",)) -> tuple[Any, list[tuple[dict, Path]]]:
        """在 ``store_root`` 放一批 control 风格的记录（``specs`` 每项 ``{unit_id, run_id?, plain?, mutate?}``），再为 ``publish`` 里的
        运行各发布一份把本运行记录列为正式执行的清单。返回（记录库, [(记录, 路径)]）。"""

        store = self.new.RecordStore(store_root)
        scratch = store_root.parent / f"{store_root.name}-files"
        scratch.mkdir(parents=True, exist_ok=True)
        trace = scratch / "trace.json"
        trace.write_text(json.dumps({"schema_version": "read-audit-trace/v3", "roots": ["/"], "accesses": [["/etc/hosts", ["read"]]]}), encoding="utf-8")
        trace_digest = self.new.file_sha256(trace)
        store.put_log(trace, trace_digest)
        log = scratch / "log.txt"
        log.write_text("输出\n", encoding="utf-8")
        log_digest = self.new.file_sha256(log)
        store.put_log(log, log_digest)
        stored = []
        for spec in specs:
            body = self.control_body(spec["unit_id"], spec.get("run_id", "control-run"), trace_digest, log_digest, plain=spec.get("plain", False))
            if spec.get("mutate") is not None:
                spec["mutate"](body)
            record = self.new.seal_record(body)
            stored.append((record, store.put_record(record)))
        for run_id in publish:
            store.put_manifest(self.new.build_manifest(run_id=run_id, units=[
                {"unit_id": record["unit_id"], "disposition": "executed", "record_sha256": record["record_sha256"]}
                for record, _path in stored if record["run"]["run_id"] == run_id]))
        return store, stored

    def facts_and_current(self, module, record: dict, **overrides):
        current = module.Current(unit_id=CONTROL_UNIT, unit_type="command", spec={}, spec_sha256=record["spec_sha256"], inputs=list(record["inputs"]),
                                 inputs_sha256=record["inputs_sha256"], inheritable=True)
        values = {"policy_sha256": "p" * 64, "environment": list(record["environment"]), "environment_sha256": record["environment_sha256"],
                  "executor": dict(record["executor"]), "max_age_hours": 168.0, "now": module.parse_utc("2026-10-15T01:00:00Z")}
        values.update(overrides)
        return current, module.RunFacts(**values)

    # ---- T-C3：check_record 变异矩阵 ----

    def test_t_c3_check_record_problems_are_identical_over_a_mutation_matrix(self) -> None:
        mutations = {
            "原样": None, "诊断执行": lambda body: body.update(kind="diagnostic"), "来源不可承接": lambda body: body.update(inheritable=False),
            "单元类型": lambda body: body.update(unit_type="test"), "单元 ID": lambda body: body.update(unit_id="pre-a3:claude:other"),
            "未通过": lambda body: body.update(passed=False), "退出码": lambda body: body.update(exit_code=1), "超时": lambda body: body.update(timed_out=True),
            "被信号终止": lambda body: body.update(signal=9), "规格摘要": lambda body: body.update(spec_sha256="0" * 64),
            "输入摘要": lambda body: body.update(inputs_sha256="0" * 64), "策略摘要": lambda body: body.update(policy_sha256="0" * 64),
            "环境摘要": lambda body: body.update(environment_sha256="0" * 64), "执行器": lambda body: body["executor"].update(sha256="0" * 64),
            "读集范围是新值": lambda body: body["read_audit"].update(coverage_scope="declared-inputs/v1"),
            "读集范围未知": lambda body: body["read_audit"].update(coverage_scope="weird/v9"),
            "读集未闭合": lambda body: body["read_audit"].update(coverage_complete=False), "读集状态失败": lambda body: body["read_audit"].update(status="failed"),
            "未声明数": lambda body: body["read_audit"].update(undeclared_count=3),
            "读集绑定别的输入": lambda body: body["read_audit"].update(inputs_sha256="0" * 64), "无读集": lambda body: body.pop("read_audit"),
            "轨迹摘要指向缺失文件": lambda body: body["read_audit"]["trace"].update(sha256="a" * 64),
            "轨迹摘要非法": lambda body: body["read_audit"]["trace"].update(sha256="abc"), "无轨迹": lambda body: body["read_audit"].pop("trace"),
            "完成时间不可读": lambda body: body.update(completed_at_utc="昨天"), "已过期": lambda body: body.update(completed_at_utc="2026-01-01T00:00:00Z"),
            "完成时间在未来": lambda body: body.update(completed_at_utc="2026-10-16T00:00:00Z"), "日志缺失": lambda body: body["log"].update(sha256="f" * 64),
            "日志摘要缺失": lambda body: body.pop("log"), "无 require-read-audit 条目": lambda body: body.update(
                inputs=[entry for entry in body["inputs"] if entry["name"] != "require-read-audit"]),
            "带 declared_contracts 键（普通记录不该有）": lambda body: body.update(declared_contracts=[]),
        }
        scenarios = {"重放完整": {}, "重放不完整": {"result": {"coverage_complete": False}}, "重放抛 ValueError": {"raises": ValueError("轨迹坏了")},
                     "重放抛 RecordsError": {"raises": "records"}, "宿主条目补比有问题": {"host_problems": ["宿主快照访问方式漂移"]}}
        cases: list[tuple[str, dict, Path]] = []
        for index, (label, mutate) in enumerate(mutations.items()):
            _store, stored = self.control_store(self.tmp / f"store-{index}", [{"unit_id": CONTROL_UNIT, "mutate": mutate}])
            cases.append((label, stored[0][0], stored[0][1]))
        _store, stored = self.control_store(self.tmp / "store-nomanifest", [{"unit_id": CONTROL_UNIT}], publish=())
        cases.append(("原运行清单缺失", stored[0][0], stored[0][1]))
        _store, stored = self.control_store(self.tmp / "store-tampered", [{"unit_id": CONTROL_UNIT}])
        stored[0][1].chmod(0o600)
        stored[0][1].write_text(stored[0][1].read_text(encoding="utf-8").replace('"passed": true', '"passed": true, "seconds": 1'), encoding="utf-8")
        cases.append(("自摘要被改", stored[0][0], stored[0][1]))
        _store, stored = self.control_store(self.tmp / "store-other-run", [{"unit_id": CONTROL_UNIT}], publish=())
        self.new.RecordStore(self.tmp / "store-other-run").put_manifest(self.new.build_manifest(run_id="control-run", units=[]))
        cases.append(("原运行清单没列这条记录", stored[0][0], stored[0][1]))
        reasons: set[str] = set()
        passing = 0
        compared = 0
        for name, record, path in cases:
            root = path.parents[2]
            for scenario, spec in scenarios.items():
                for thorough in (False, True):
                    for require in (False, True):
                        for historical in (False, True):
                            results, audits = [], []
                            for module in (self.base, self.new):
                                fake = FakeAudit()
                                fake.result = spec.get("result", fake.result)
                                fake.host_problems = spec.get("host_problems", [])
                                fake.raises = module.RecordsError("轨迹坏了") if spec.get("raises") == "records" else spec.get("raises")
                                module._audit_module = lambda fake=fake: fake
                                current, facts = self.facts_and_current(module, record, require_read_audit=require, historical_read_audit=historical)
                                store = module.RecordStore(root)
                                results.append(outcome(lambda: module.check_record(store, path, record, current, facts, thorough=thorough)))
                                audits.append(fake.calls)
                            key = f"{name}／{scenario}／thorough={thorough}／require={require}／historical={historical}"
                            self.assertEqual(results[0], results[1], key)
                            self.assertEqual(audits[0], audits[1], f"审计模块收到的调用也必须相同：{key}")
                            compared += 1
                            if results[0][0] == "ok":
                                reasons.update(results[0][1])
                                passing += results[0][1] == []
        for module in (self.base, self.new):
            module._audit_module = lambda: control_audit
        self.assertEqual(compared, len(cases) * len(scenarios) * 8)
        self.assertGreater(passing, 0, "矩阵里必须有通过的组合，否则比较的是空气")
        self.assertGreater(len(reasons), 15, f"矩阵覆盖了足够多种不同的拒绝理由：{len(reasons)}")

    def test_t_c3_the_real_control_adapter_as_audit_module_gives_identical_results(self) -> None:
        """不用替身：审计模块就是 control 适配器本身（control_read_audit._records_module 的真实配置），轨迹是一份不合格的假轨迹，
        两个版本得到同一个（拒绝）结论。"""

        _store, stored = self.control_store(self.tmp / "store-real-adapter", [{"unit_id": CONTROL_UNIT}])
        record, path = stored[0]
        for module in (self.base, self.new):
            module._audit_module = lambda: control_audit
        results = []
        for module in (self.base, self.new):
            current, facts = self.facts_and_current(module, record, require_read_audit=True, historical_read_audit=True)
            results.append(outcome(lambda: module.check_record(module.RecordStore(path.parents[2]), path, record, current, facts)))
        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0][0] == "ok" and results[0][1], "假轨迹不能按当前输入重放：这个结论本身就是被比较的对象")

    # ---- T-C4：RunFacts ----

    def test_t_c4_runfacts_construction_styles_stay_valid(self) -> None:
        base_fields = [field.name for field in dataclasses.fields(self.base.RunFacts)]
        new_fields = [field.name for field in dataclasses.fields(self.new.RunFacts)]
        self.assertEqual(new_fields[:len(base_fields)], base_fields, "既有字段的顺序不变")
        self.assertEqual(new_fields[len(base_fields):], ["declared_route"], "只在末尾追加一个字段")
        self.assertIs(dataclasses.fields(self.new.RunFacts)[-1].default, False)
        positional = ["p" * 64, [], "e" * 64, {"files": {}, "sha256": "x"}, 168.0, 12.5]
        for module in (self.base, self.new):
            facts = module.RunFacts(*positional, True, True)
            self.assertEqual((facts.require_read_audit, facts.historical_read_audit), (True, True))
            short = module.RunFacts(*positional)
            self.assertEqual((short.require_read_audit, short.historical_read_audit), (False, False))
            keyword = module.RunFacts(policy_sha256="p", environment=[], environment_sha256="e", executor={}, max_age_hours=1.0, now=1.0,
                                      historical_read_audit=True)
            replaced = dataclasses.replace(keyword, require_read_audit=True, max_age_hours=24)
            self.assertEqual((replaced.require_read_audit, replaced.historical_read_audit, replaced.max_age_hours), (True, True, 24))
        self.assertFalse(self.new.RunFacts(*positional).declared_route)
        self.assertFalse(dataclasses.replace(self.new.RunFacts(*positional), require_read_audit=True).declared_route)

    # ---- T-C5：verify_manifest ----

    def test_t_c5_verify_manifest_is_identical_for_manifests_with_control_units(self) -> None:
        run_a, run_b = "control-run", "verify-run"
        store, stored = self.control_store(self.tmp / "store-manifest", [
            {"unit_id": CONTROL_DEADLINE_UNIT, "run_id": run_a},
            {"unit_id": CONTROL_UNIT, "run_id": run_b},
            {"unit_id": "egress-spec:x", "run_id": run_b, "plain": True}], publish=(run_a,))
        by_unit = {record["unit_id"]: (record, path) for record, path in stored}
        environment = by_unit[CONTROL_UNIT][0]["environment"]

        def manifest(mode: str, inherited: bool = True) -> dict:
            entries = []
            for unit_id in (CONTROL_UNIT, "egress-spec:x", CONTROL_DEADLINE_UNIT):
                record, path = by_unit[unit_id]
                disposition = "inherited" if inherited and unit_id == CONTROL_DEADLINE_UNIT else "executed"
                entries.append({"unit_id": unit_id, "unit_type": "command", "disposition": disposition, "record_path": str(path),
                                "record_sha256": record["record_sha256"], "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"],
                                "test_group": None})
            return self.new.build_manifest(run_id=run_b, mode=mode, planned_units=[entry["unit_id"] for entry in entries], units=entries, test_groups={},
                                           diagnostic=[], policy_sha256="p" * 64, environment=environment,
                                           environment_sha256=self.new.entries_sha256(environment), executor=by_unit[CONTROL_UNIT][0]["executor"],
                                           inheritance_max_age_hours=168.0, decided_at_utc="2026-10-15T01:00:00Z")

        incomplete = FakeAudit()
        incomplete.result = {"coverage_complete": False}
        compared = nonempty = 0
        for scenario, fake in {"完整": FakeAudit(), "不完整": incomplete}.items():
            for mode in (self.new.FULL_SET_PASS, self.new.RE_EXECUTE, self.new.UNIT_INHERIT):
                for historical in (False, True):
                    for with_store in (True, False):
                        results = []
                        for module in (self.base, self.new):
                            module._audit_module = lambda fake=fake: fake
                            fake.calls.clear()
                            check_store = module.RecordStore(store.root) if with_store else None
                            results.append((outcome(lambda: module.verify_manifest(manifest(mode), store=check_store, historical=historical)),
                                            list(fake.calls)))
                        self.assertEqual(results[0], results[1], f"{scenario}／{mode}／historical={historical}／store={with_store}")
                        compared += 1
                        nonempty += bool(results[0][0][1])
        for module in (self.base, self.new):
            module._audit_module = lambda: control_audit
        self.assertEqual(compared, 24)
        self.assertGreater(nonempty, 0, "矩阵里有产生问题的组合（逐单元承接缺决策收据、重新执行全集不得有承接项……）")
        # 夹具有效性：审计替身说重放完整时，全集通过模式的清单（含一个 control 承接项）自检通过；再造一个具体的拒绝理由。
        self.new._audit_module = lambda: FakeAudit()
        try:
            self.assertEqual(self.new.verify_manifest(manifest(self.new.FULL_SET_PASS), store=self.new.RecordStore(store.root)), [])
            problems = self.new.verify_manifest(manifest(self.new.RE_EXECUTE), store=self.new.RecordStore(store.root))
            self.assertTrue(any("重新执行全集模式不得有承接项" in problem for problem in problems), problems)
        finally:
            self.new._audit_module = lambda: control_audit

    # ---- T-C11：control sidecar 字节 ----

    def test_t_c11_control_sidecar_files_are_byte_for_byte_unchanged(self) -> None:
        for name, expected in (("control_read_audit.py", CONTROL_AUDIT_SHA256), ("control_read_audit_runtime.py", CONTROL_RUNTIME_SHA256)):
            with self.subTest(name):
                self.assertEqual(hashlib.sha256((REPO_ROOT / "tools" / "ci" / name).read_bytes()).hexdigest(), expected,
                                 f"{name} 的字节被执行器登记并在消费时核对，声明级承接不得改它（改它会让历史 control 凭证重放失败）")

    # ---- T-C12：不碰审计模块 ----

    def test_t_c12_the_declared_chapter_never_touches_the_audit_module(self) -> None:
        base_tree, new_tree = ast.parse(self.base_source), ast.parse(self.new_source)
        base_names = {node.name for node in base_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        added = [node for node in new_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name not in base_names]
        self.assertGreaterEqual(len(added), 15, "新章节的函数")

        def references(node: ast.AST) -> list[str]:
            found = []
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id in {"_audit_module", "read_audit"}:
                    found.append(child.id)
                elif isinstance(child, ast.Attribute) and child.attr in {"_audit_module", "_runtime_module", "host_snapshot", "strict_audit_reads"}:
                    found.append(child.attr)
                elif isinstance(child, ast.Import):
                    found += [alias.name for alias in child.names if "read_audit" in alias.name or "control" in alias.name]
                elif isinstance(child, ast.ImportFrom) and ("read_audit" in (child.module or "") or "control" in (child.module or "")):
                    found.append(child.module)
                elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in {"__import__", "exec", "eval"}:
                    found.append(child.func.id)
            return found

        for node in added:
            with self.subTest(node.name):
                self.assertEqual(references(node), [], f"{node.name} 不得引用 _audit_module、read_audit 或 control 适配器")
        count = lambda tree: sum(isinstance(node, ast.Name) and node.id == "_audit_module" for node in ast.walk(tree))  # noqa: E731
        self.assertEqual(count(base_tree), count(new_tree), "整个模块里 _audit_module 的引用数与基线相同（三处插入也没有新增引用）")

    def test_the_control_adapter_still_loads_the_new_file_with_its_own_audit_module(self) -> None:
        records = control_audit._records_module()
        self.assertIs(records._audit_module(), control_audit, "control 实例的审计模块仍是 control 适配器")
        self.assertTrue(callable(records.declared_coverage) and callable(records.validate_declared_scope))
        self.assertIsNot(records, ur, "control 实例是独立的一份，不是普通模块")
        self.assertEqual(records.DECLARED_SCOPE_ENTRY, ur.DECLARED_SCOPE_ENTRY)
        self.assertTrue(records.declared_coverage(make_sample(), make_scope())["ok"], "新章节在 control 实例里同样自足可用")


# ---------------------------------------------------------------------------
# T-C13：用 ARM64 H3 九链的真实记录库样本重放
# ---------------------------------------------------------------------------


@unittest.skipUnless(CONTROL_SAMPLES.is_dir(), f"H3 真实 control 记录库样本不在本机（仓库外证据目录 {CONTROL_SAMPLES}）：该用例只在有样本时运行")
class RealControlRecordReplayTests(unittest.TestCase):
    """样本来自 H3 九链（两次运行，各含四条 control 单元与五条其它 pre-A3 场景单元）。已知限制：这批记录没有 read_audit 字段
    （H3 当时没有开读集审计），所以「按封存轨迹重放严格读集」那一段无法用真实数据覆盖——它由 T-C3 的替身审计模块矩阵覆盖；
    这里重放的是通用判定（格式、自摘要、正式执行、单元、结论、规格、输入、策略、环境、执行器、期限、原运行清单、日志）
    与整份清单自检，用真实的记录、清单、日志文件逐字比较基线与新版的结果。"""

    @classmethod
    def setUpClass(cls) -> None:
        source = baseline_source()
        if source is None:
            raise unittest.SkipTest(f"读不到基线提交 {CONTROL_BASELINE_COMMIT} 的 tools/ci/unit_records.py")
        cls.base = load_records_module(source, "unit_records_real_baseline", f"{CONTROL_BASELINE_COMMIT}:tools/ci/unit_records.py")
        cls.new = control_audit._records_module()  # 真实的 control 加载路径：指向本工作树的 unit_records.py，审计模块是 control 适配器
        cls.records = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(CONTROL_SAMPLES.glob("records/*/*.json"))]
        cls.runs = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(CONTROL_SAMPLES.glob("runs/*.json"))}

    def test_the_sample_set_is_what_the_report_says(self) -> None:
        self.assertEqual(len(self.records), 20)
        self.assertEqual(len(self.runs), 2)
        control_ids = {record["unit_id"] for _path, record in self.records if record["unit_id"] in control_audit.CONTROL_UNIT_IDS}
        self.assertEqual(control_ids, set(control_audit.CONTROL_UNIT_IDS), "四条 control 单元都有真实记录")
        self.assertTrue(all("declared_contracts" not in record and "read_audit" not in record for _path, record in self.records))
        self.assertTrue(all(ur.DECLARED_SCOPE_ENTRY not in {entry["name"] for entry in record["inputs"]} for _path, record in self.records))

    def test_check_record_over_every_real_record_is_identical(self) -> None:
        store_base, store_new = self.base.RecordStore(CONTROL_SAMPLES), self.new.RecordStore(CONTROL_SAMPLES)
        compared = passing = 0
        for path, record in self.records:
            completed = ur.parse_utc(record["completed_at_utc"])
            for thorough in (False, True):
                for require in (False, True):
                    for historical in (False, True):
                        results = []
                        for module, store in ((self.base, store_base), (self.new, store_new)):
                            current = module.Current(unit_id=record["unit_id"], unit_type=record["unit_type"], spec={}, spec_sha256=record["spec_sha256"],
                                                     inputs=list(record["inputs"]), inputs_sha256=record["inputs_sha256"], inheritable=True)
                            facts = module.RunFacts(policy_sha256=record["policy_sha256"], environment=list(record["environment"]),
                                                    environment_sha256=record["environment_sha256"], executor=dict(record["executor"]), max_age_hours=168.0,
                                                    now=completed + 3600, require_read_audit=require, historical_read_audit=historical)
                            results.append(outcome(lambda: module.check_record(store, path, record, current, facts, thorough=thorough)))
                        self.assertEqual(results[0], results[1], f"{record['unit_id']} {path.stem[:12]} thorough={thorough} require={require} historical={historical}")
                        compared += 1
                        passing += results[0] == ("ok", [])
        self.assertEqual(compared, 20 * 8)
        self.assertGreater(passing, 0, "真实记录里有通过通用核对的（不要求读集时）")

    def test_verify_manifest_over_the_real_run_manifests_is_identical(self) -> None:
        """清单里的 record_path 是 ARM64 上的绝对路径：换成本机样本库路径并重新封存后，两个版本各自自检，结果必须逐字相同。"""

        arm64_prefix = "/root/claude-offline-tests/concentrated-resume-0a5360f59-20261013/h3-run/unit-records"

        def remap(value):
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items() if key != "manifest_sha256"}
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, str) and value.startswith(arm64_prefix):
                return str(CONTROL_SAMPLES) + value[len(arm64_prefix):]
            return value

        store_base, store_new = self.base.RecordStore(CONTROL_SAMPLES), self.new.RecordStore(CONTROL_SAMPLES)
        manifests = []
        for run_id, original in self.runs.items():
            manifests.append(ur.seal(remap(original), "manifest_sha256"))
            # 同一批真实记录再造一份「承接」清单（全集通过模式）：每个单元承接自己在原运行里的正式执行记录。
            inherited_units = [{**remap(entry), "disposition": "inherited"} for entry in original["units"]]
            manifests.append(ur.seal({**remap({key: value for key, value in original.items() if key not in {"units", "diagnostic", "counts"}}),
                                      "run_id": f"replay-{run_id}", "mode": "full-set-pass", "units": inherited_units, "diagnostic": []}, "manifest_sha256"))
        compared = 0
        outcomes = []
        for manifest in manifests:
            for historical in (False, True):
                results = [outcome(lambda: module.verify_manifest(manifest, store=store, historical=historical))
                           for module, store in ((self.base, store_base), (self.new, store_new))]
                self.assertEqual(results[0], results[1], f"{manifest['run_id']} historical={historical}")
                outcomes.append(results[0])
                compared += 1
        self.assertEqual(compared, 8)
        self.assertEqual(outcomes[0], ("ok", []), "原样的真实运行清单（重新执行全集）自检通过")
        self.assertTrue(any(result[0] == "ok" and result[1] for result in outcomes), "改成承接清单后有可见的问题（比较的不是空气）")


if __name__ == "__main__":
    unittest.main()
