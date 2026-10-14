"""命令单元声明级承接 ``declared-inputs/v1`` 的入口门禁一侧（步骤 5 的 WP2，``tools/ci/entry_gates.py`` 的新增部分）。

* 登记表读取与自检（T19）：缺失即旧声明逐字不变；登记 pre-a3、与 audit_policy／--input-scope 重复、证据或命令或启动前缀摘要
  不符、登记时覆盖不 ok、rerun-only 缺原因、合同未评审或缺 make／Ryuk 配套，一律 ValueError；
* 合同探测：T9（工具链版本、镜像 Id 变化）、T10（缓存写热前后条目不变）、T14（git 历史随树 ID、不随 HEAD）、
  T15（make 展开只随目标实际命令变化，含解析期求值的 Makefile 转 rerun-only）、T16（HOME 配置存在性与内容）；
* plan_gates 接线与清单汇总，生成的清单能被执行器的闭合校验接受；
* T-C10：登记了声明级承接的清单上，control_read_audit_for_plan 仍只认 control 四链。

unit_records.py 里的两个纯函数（validate_declared_scope、declared_coverage）属于 WP1：这里用同签名的桩隔离，WP1 合入后
``RealWp1IntegrationTests`` 用真实函数联调。合同探测全部用假 runner 与临时目录，不依赖真实的 docker／go／node／pnpm；
make 展开与 git 历史各有一条用真实 make／git 的用例，验证规范化与内容寻址确实成立。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_unit_records as scaffold

try:  # WP4 的采样器：用它真实产出的证据与登记表草案做互操作验证；缺失时对应用例跳过。
    from tools.ci import declared_sampler as ds
except ImportError:  # pragma: no cover - 取决于基线是否已含 WP4
    ds = None

REPO_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ["unshare", "-m", "--propagation", "private", "bash", "-c", "mount -t tmpfs tmpfs /root/oauth-capture && exec \"$@\"", "entry-gates"]
CHECKS = ["check-egress-spec-local-source", "test-official-client-control", "egress-spec-go-test", "egress-spec-version-leak"]
CI_WORKFLOW = """jobs:
  shell:
    steps:
      - name: Check deploy scripts
        run: |
          /bin/bash -n deploy/apple-container.sh
          /bin/bash deploy/tests/apple-container-test.sh
          /bin/sh deploy/tests/docker-compose-security-test.sh
"""
HARNESS = """//go:build integration

package repository

const (
\tredisImageTag    = "redis:8.4-alpine"
\tpostgresImageTag = "postgres:18.1-alpine3.23"
)
"""
GO_MOD = """module example.com/sub2api

go 1.27.0

require (
\tgithub.com/testcontainers/testcontainers-go/modules/postgres v0.40.0
)

require github.com/testcontainers/testcontainers-go v0.40.0 // indirect
"""
SHA_A, SHA_B = "a" * 64, "b" * 64


def makefile_text(vitest: tuple[str, ...] = ("src/a.spec.ts", "src/b.spec.ts"), extra: str = "") -> str:
    """合成 Makefile（配方行必须是 TAB 开头）：子检查清单、FRONTEND_CRITICAL_VITEST 变量、两个会被展开合同绑定的目标。"""

    listing = " \\\n".join(f"\t{spec}" for spec in vitest)
    return (f"print-egress-spec-checks:\n\t@echo {' '.join(CHECKS)}\n"
            f"FRONTEND_CRITICAL_VITEST := \\\n{listing}\n"
            "test-frontend-critical:\n\t@pnpm --dir frontend exec vitest run $(FRONTEND_CRITICAL_VITEST)\n"
            "egress-spec-go-test:\n\t@cd backend && go test ./internal/officialegress/... -count=1\n\t@echo workdir=$(CURDIR)\n" + extra)


def make_tree(root: Path, *, makefile: str | None = None, extra_files: dict[str, str] | None = None) -> Path:
    """最小测试树：Makefile、CI 定义（部署脚本测试）、采集工具测试目录、后端模块与服务镜像常量、前端锁文件。"""

    tree = root / "tree"
    (tree / ".github" / "workflows").mkdir(parents=True)
    (tree / ".github" / "workflows" / "backend-ci.yml").write_text(CI_WORKFLOW, encoding="utf-8")
    (tree / "tools" / "official_client_capture" / "tests").mkdir(parents=True)
    (tree / "Makefile").write_text(makefile if makefile is not None else makefile_text(), encoding="utf-8")
    (tree / "backend" / "internal" / "repository").mkdir(parents=True)
    (tree / "backend" / "go.mod").write_text(GO_MOD, encoding="utf-8")
    (tree / "backend" / "go.sum").write_text("example.com/dep v1.0.0 h1:abc=\n", encoding="utf-8")
    (tree / "backend" / "internal" / "repository" / "integration_harness_test.go").write_text(HARNESS, encoding="utf-8")
    (tree / "frontend").mkdir()
    (tree / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    for relative, body in (extra_files or {}).items():
        (tree / relative).write_text(body, encoding="utf-8")
    return tree.resolve()


def default_scope(contracts: tuple[str, ...] = ("toolchain:go",), make_expansion: tuple[str, ...] = (),
                  git_commits: tuple[str, ...] = (), outside: tuple[dict, ...] = ()) -> dict:
    """合成 declared-scope/v1（形状取自调研 3.2 的示例）。"""

    repo: dict = {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}],
                  "files": ["backend/go.mod", "backend/go.sum"], "list_ok": [], "repo_ignored": [], "head": False}
    if git_commits:
        repo["git_history"] = {"commits": list(git_commits), "reason": "测试用固定提交"}
    return {"schema_version": eg.DECLARED_SCOPE_SCHEMA, "repo": repo, "make_expansion": list(make_expansion), "outside": list(outside),
            "contracts": list(contracts), "network": "loopback-only"}


# ---- WP1（unit_records.py）两个函数的同签名桩 ----


def stub_validate_declared_scope(scope):
    """桩：只核最外形（真实的闭合 schema 校验是 WP1 的）。"""

    if not isinstance(scope, dict) or scope.get("schema_version") != eg.DECLARED_SCOPE_SCHEMA or not isinstance(scope.get("repo"), dict):
        raise ValueError("桩：scope 形状非法")
    return scope


def stub_declared_coverage(sample, scope):
    """桩：仓库内访问必须落在 ranges 的 include 或 files 里（真实的 C1～C7 覆盖检查是 WP1 的）。"""

    includes = [prefix for item in scope["repo"].get("ranges", []) for prefix in item["include"]]
    files = {item if isinstance(item, str) else item["path"] for item in scope["repo"].get("files", [])}
    undeclared = []
    for path, _kinds, _tracked in sample.get("repo_accesses", []):
        relative = path.removeprefix("{TREE}/")
        if relative not in files and not any(relative.startswith(prefix) for prefix in includes):
            undeclared.append(relative)
    return {"ok": not undeclared, "undeclared": undeclared, "counts": {"repo_paths": len(sample.get("repo_accesses", []))}}


def install_wp1_stubs(case: unittest.TestCase) -> None:
    """把桩装进 entry_gates 实际使用的 unit_records 实例（按路径加载的同目录副本）；用例结束自动卸下。"""

    records = eg._unit_records_module()
    for name, stub in (("validate_declared_scope", stub_validate_declared_scope), ("declared_coverage", stub_declared_coverage)):
        patcher = mock.patch.object(records, name, stub, create=True)
        patcher.start()
        case.addCleanup(patcher.stop)


# ---- 假 runner 与合成世界 ----


class FakeRunner:
    """entry_steps.Context 的 runner 注入点：按完整 argv 返回预置的（退出码, 标准输出），没预置的当作命令不存在（127）；记录每次调用。"""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[tuple[str, ...], str | None, dict | None]] = []

    def __call__(self, argv, env, cwd):
        self.calls.append((tuple(argv), None if cwd is None else str(cwd), None if env is None else dict(env)))
        value = self.responses.get(tuple(argv))
        value = value() if callable(value) else value
        return value if value is not None else (127, "FileNotFoundError: 命令不存在")

    def count(self, *argv: str) -> int:
        return sum(1 for call in self.calls if call[0] == tuple(argv))


class Toolchain:
    """假工具链世界：PATH 里是四个普通可执行文件（内容就是它们的「二进制」），GOROOT 是带 VERSION 与 pkg/tool 的目录，
    HOME 是空目录；runner 按这些状态现算 go version／go env／docker／git 的输出，所以改状态就是改探测结果。"""

    IMAGES = ("postgres:18.1-alpine3.23", "redis:8.4-alpine", "testcontainers/ryuk:0.13.0")

    def __init__(self, root: Path, tree: Path) -> None:
        self.tree = tree
        self.bin = root / "bin"
        self.bin.mkdir()
        self.goroot = root / "goroot"
        self.tool_dir = self.goroot / "pkg" / "tool" / "linux_arm64"
        self.tool_dir.mkdir(parents=True)
        (self.goroot / "bin").mkdir()
        self.home = root / "home"
        self.home.mkdir()
        for name in ("go", "node", "pnpm", "make", "git", "docker"):
            self.set_binary(name, f"{name}-binary-1")
        (self.goroot / "VERSION").write_text("go1.27.0\n", encoding="utf-8")
        (self.goroot / "bin" / "go").write_text("goroot-go-1", encoding="utf-8")
        for tool in eg.DECLARED_GO_TOOLS:
            (self.tool_dir / tool).write_text(f"{tool}-1", encoding="utf-8")
        self.versions = {"go": "go version go1.27.0 linux/arm64", "node": "v20.19.0", "pnpm": "10.4.1", "make": "GNU Make 4.3"}
        self.go_env = {"GOFLAGS": "", "GOPROXY": "https://proxy.golang.org,direct", "GOTOOLCHAIN": "auto", "GOEXPERIMENT": "",
                       "CGO_ENABLED": "1", "GOENV": str(self.home / ".config" / "go" / "env")}
        self.image_ids = {image: "sha256:" + hashlib.sha256(image.encode()).hexdigest() for image in self.IMAGES}
        self.environ = {"PATH": str(self.bin), "HOME": str(self.home)}
        self.runner = FakeRunner(self.responses())

    def set_binary(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def responses(self) -> dict:
        keys = ("GOROOT", "GOTOOLDIR", *eg.DECLARED_GO_ENV_KEYS)
        table: dict = {
            ("go", "version"): lambda: (0, self.versions["go"] + "\n"),
            ("go", "env", "-json", *keys): lambda: (0, json.dumps({"GOROOT": str(self.goroot), "GOTOOLDIR": str(self.tool_dir), **self.go_env})),
            ("node", "--version"): lambda: (0, self.versions["node"] + "\n"),
            ("pnpm", "--version"): lambda: (0, self.versions["pnpm"] + "\n"),
            ("make", "--version"): lambda: (0, self.versions["make"] + "\nBuilt for x86_64\n"),
        }
        for image in self.IMAGES:
            table[("docker", "image", "inspect", "--format", "{{.Id}}", image)] = lambda image=image: (0, self.image_ids[image] + "\n")
        return table

    def probe(self, **kwargs) -> eg.DeclaredProbe:
        return eg.declared_probe(runner=kwargs.pop("runner", self.runner), environ=kwargs.pop("environ", self.environ), tree=self.tree, **kwargs)


def entry_digests(entries: list[dict]) -> dict[str, str]:
    return {entry["name"]: entry["sha256"] for entry in entries}


def host_state(entry: dict) -> str:
    """host-config 条目里（只绑定一个文件时）那个文件的状态：内容摘要、missing、not-a-file 或 unavailable(…)。"""

    (item,) = entry["detail"]["files"]
    return item["state"]


def declaration(scope: dict) -> dict:
    return {"declared_scope": scope, "declared_sample": {"evidence_sha256": SHA_A, "command_sha256": SHA_A, "sampler_sha256": SHA_A}}


class World:
    """合成世界：测试树、无登记表时的计划、登记表与采样证据的构造器。登记表写到测试树的默认位置，plan_gates 自己读到。"""

    def __init__(self, case: unittest.TestCase, *, launcher: list[str] | None = None, profile: str = "full-gates",
                 makefile: str | None = None, extra_files: dict[str, str] | None = None) -> None:
        self.directory = tempfile.TemporaryDirectory()
        case.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.tree = make_tree(self.root, makefile=makefile, extra_files=extra_files)
        self.launcher = LAUNCHER if launcher is None else launcher
        self.profile = profile
        self.plan_args = {"profile": profile, "launcher": self.launcher, "platform": "linux"}
        self.units = {unit["unit_id"]: unit for unit in eg.plan_gates(self.tree, **self.plan_args)["units"]}
        self.registry: dict = {"schema_version": eg.DECLARED_REGISTRY_SCHEMA, "units": {}}

    @property
    def registry_path(self) -> Path:
        return self.tree / eg.DECLARED_REGISTRY_PATH

    def command_of(self, unit_id: str) -> tuple[list[str], str]:
        unit = self.units[unit_id]
        return unit["argv"][len(self.launcher):], Path(unit["cwd"]).relative_to(self.tree).as_posix()

    def declare(self, unit_id: str, scope: dict | None = None, *, accesses: list | None = None, sampler: str = SHA_B) -> dict:
        """写证据文件、登记一个 declared 单元，返回登记项（测试可再改它来制造不一致）。"""

        scope = scope if scope is not None else default_scope()
        command, cwd_rel = self.command_of(unit_id)
        evidence = {
            "schema_version": eg.DECLARED_SAMPLE_SCHEMA, "unit_id": unit_id,
            "command": {"launcher": list(self.launcher), "argv": command, "cwd_rel": cwd_rel,
                        "sha256": ur.sha256_json({"argv": command, "cwd_rel": cwd_rel})},
            "sampled": {"commit": "c" * 40, "utc": "2026-10-15T00:00:00Z", "sampler_sha256": sampler, "runs": []},
            "repo_accesses": accesses if accesses is not None else [["{TREE}/backend/go.mod", ["read"], True]],
            "outside_accesses": [], "coverage": {"stream_complete": True, "unresolved": 0}, "exit_code": 0,
        }
        # 按调研 3.2 的 declared-sample/v1 补全其余字段：桩不看它们，WP1 的真实覆盖检查（C7 等）要看。
        evidence["aliases"] = {"TREE": "{采样时的绝对路径，仅留痕}", "WORK": "{采样时的绝对路径，仅留痕}"}
        evidence["coverage"] = {"stream_complete": True, "unresolved": 0, "unresolved_reasons": {}, "unfinished_calls": 0,
                                "unresolved_process_events": 0, "lines": 1000}
        evidence["git_commands"] = []
        evidence["network"] = []
        evidence["valid"] = True  # WP1e 起验证侧要求 valid 存在且为 True（缺失同样判 C7-invalid）：真实采样器的证据一定带它
        relative = f"{eg.DECLARED_SAMPLES_DIR}/{unit_id.replace(':', '-')}.sample.json"
        path = self.tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
        path.write_bytes(raw)
        entry = {"status": "declared", "scope": scope,
                 "sample": {"evidence": relative, "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                            "command_sha256": ur.sha256_json({"argv": command, "cwd_rel": cwd_rel}),
                            "launcher_sha256": ur.sha256_json(list(self.launcher)), "sampler_sha256": sampler,
                            "sampled_commit": "c" * 40, "sampled_at_utc": "2026-10-15T00:00:00Z", "runs": 3}}
        self.registry["units"][unit_id] = entry
        return entry

    def rerun_only(self, unit_id: str, reason: str = "实时检查，重跑成本低于承接复杂度", **extra) -> dict:
        entry = {"status": "rerun-only", "reason": reason, **extra}
        self.registry["units"][unit_id] = entry
        return entry

    def write(self) -> Path:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(self.registry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return self.registry_path

    def plan(self, **kwargs) -> dict:
        return eg.plan_gates(self.tree, **{**self.plan_args, **kwargs})


def inputs_of(manifest: dict) -> dict[str, dict]:
    return {unit["unit_id"]: unit for unit in manifest["units"]}


def rewrite_evidence(world: World, entry: dict, mutate) -> None:
    """改证据文件内容，并把登记的 evidence_sha256 同步成新字节的摘要——这样前面的摘要核对都能过，只有被改的那一项会触发。"""

    path = world.tree / entry["sample"]["evidence"]
    evidence = json.loads(path.read_text(encoding="utf-8"))
    mutate(evidence)
    raw = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    path.write_bytes(raw)
    entry["sample"]["evidence_sha256"] = hashlib.sha256(raw).hexdigest()


class RegistryLoadTests(unittest.TestCase):
    def test_missing_registry_is_none_and_explicit_missing_path_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory).resolve()
            self.assertIsNone(eg.load_declared_registry(tree), "登记表缺失＝没有启用声明级承接，行为即旧声明")
            with self.assertRaisesRegex(ValueError, "不存在"):
                eg.load_declared_registry(tree, path=tree / "none.json")

    def test_malformed_registry_is_refused_and_a_valid_one_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory).resolve()
            path = tree / eg.DECLARED_REGISTRY_PATH
            path.parent.mkdir(parents=True)
            good = {"schema_version": eg.DECLARED_REGISTRY_SCHEMA, "units": {}}
            for text in ("{不是 json", "[]", json.dumps({**good, "schema_version": "x/v9"}), json.dumps({**good, "extra": 1}),
                         json.dumps({"schema_version": eg.DECLARED_REGISTRY_SCHEMA}), json.dumps({**good, "units": []})):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    eg.load_declared_registry(tree)
            path.write_text(json.dumps(good), encoding="utf-8")
            self.assertEqual(eg.load_declared_registry(tree), good)
            link = tree / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                eg.load_declared_registry(tree, path=link)


class CommandIdentityTests(unittest.TestCase):
    def test_command_digest_convention_is_pinned(self) -> None:
        """command_sha256 ＝ 规范化 JSON {argv（去掉启动前缀）, cwd_rel（相对测试树，树根记 "."）} 的 sha256。采样器（WP4）与
        执行器（WP3）必须用同一约定；这里用独立实现的 hashlib 把它钉死。"""

        def canonical(value) -> str:
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        tree = Path("/work/tree")
        argv = ["go", "test", "./...", "-count=1"]
        self.assertEqual(eg.declared_command_sha256(argv, "/work/tree/backend", tree), canonical({"argv": argv, "cwd_rel": "backend"}))
        root = canonical({"argv": ["make"], "cwd_rel": "."})
        self.assertEqual(eg.declared_command_sha256(["make"], "/work/tree", tree), root)
        self.assertEqual(eg.declared_command_sha256(["make"], None, tree), root)
        self.assertNotEqual(eg.declared_command_sha256(["make"], "/work/tree/backend", tree), root, "工作目录属命令身份")
        with self.assertRaises(ValueError):
            eg.declared_command_sha256(["make"], "/elsewhere", tree)

    def test_launcher_digest_is_the_canonical_digest_of_the_list(self) -> None:
        self.assertEqual(eg.declared_launcher_sha256([]), hashlib.sha256(b"[]").hexdigest(), "本机 plan 的启动前缀为空时是空数组的摘要")
        self.assertEqual(eg.declared_launcher_sha256(LAUNCHER), ur.sha256_json(LAUNCHER))
        self.assertNotEqual(eg.declared_launcher_sha256(["a"]), eg.declared_launcher_sha256([]))


class RegistryValidationTests(unittest.TestCase):
    """T19：plan_gates 的登记表自检。"""

    def setUp(self) -> None:
        install_wp1_stubs(self)
        self.world = World(self)

    def refused(self, pattern: str, **plan_kwargs) -> None:
        self.world.write()
        with self.assertRaisesRegex(ValueError, pattern):
            self.world.plan(**plan_kwargs)

    def test_t19_a_consistent_registry_is_accepted(self) -> None:
        self.world.declare("backend:go-test")
        self.world.rerun_only("capture:prerequisites")
        self.world.write()
        manifest = self.world.plan()
        self.assertEqual(manifest["declared_inputs"]["declared_units"], ["backend:go-test"])

    def test_t19_pre_a3_units_cannot_be_registered(self) -> None:
        """control 四链就在 pre-A3 单元里：任何状态、任何 pre-a3 单元都拒绝，不管它在不在本次计划里。"""

        for unit_id in ("pre-a3:claude:vc-chain.claude-reconcile", "pre-a3:claude:vc-chain.claude-tool-evolution", "pre-a3:vc-chain.vc1-capture"):
            for register in (lambda uid: self.world.rerun_only(uid), lambda uid: self.world.registry["units"].update({uid: {"status": "declared"}})):
                with self.subTest(unit_id=unit_id):
                    self.world.registry["units"].clear()
                    register(unit_id)
                    self.refused("pre-A3")

    def test_t19_a_unit_in_audit_policy_cannot_also_be_registered(self) -> None:
        tree = self.world.tree
        declaration = {"host_paths": [str(tree / "Makefile")], "require_read_audit": True,
                       "runtime_contract": {"schema_version": "unit-runtime-contract/v1", "temporary_root": str(self.world.root / "private" / "output"),
                                            "mask_roots": ["/root/oauth-capture"], "environment_paths": []}}
        policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {"backend:go-test": declaration}}
        for register in (lambda: self.world.declare("backend:go-test"), lambda: self.world.rerun_only("backend:go-test")):
            self.world.registry["units"].clear()
            register()
            self.refused("audit_policy", audit_policy=policy)
        self.world.registry["units"].clear()
        self.world.declare("backend:unit")
        self.world.write()
        manifest = self.world.plan(audit_policy=policy)
        self.assertEqual(manifest["audit_policy"], policy, "登记的是别的单元：audit_policy 原样保留")
        self.assertEqual(inputs_of(manifest)["backend:go-test"]["inputs"], declaration)

    def test_t19_a_unit_with_an_input_scope_proposal_cannot_also_be_registered(self) -> None:
        self.world.declare("backend:go-test")
        self.refused("input-scope", input_scopes=[{"unit_id": "backend:go-test"}])

    def test_t19_digest_mismatches_are_refused(self) -> None:
        cases = {
            "证据文件被改动": (lambda entry: (self.world.tree / entry["sample"]["evidence"]).write_bytes(
                (self.world.tree / entry["sample"]["evidence"]).read_bytes() + b" "), "证据文件摘要"),
            "登记的证据摘要不对": (lambda entry: entry["sample"].update(evidence_sha256=SHA_A), "证据文件摘要"),
            "登记的命令摘要不对": (lambda entry: entry["sample"].update(command_sha256=SHA_A), "command_sha256"),
            "登记的启动前缀摘要不对": (lambda entry: entry["sample"].update(launcher_sha256=SHA_A), "launcher_sha256"),
            "证据里记录的命令被改": (lambda entry: rewrite_evidence(self.world, entry, lambda e: e["command"].update(argv=["go", "vet"])), "证据文件里记录的命令"),
            "证据属于别的单元": (lambda entry: rewrite_evidence(self.world, entry, lambda e: e.update(unit_id="backend:unit")), "不是属于本单元"),
            "证据里的采样器摘要与登记不同": (lambda entry: rewrite_evidence(self.world, entry, lambda e: e["sampled"].update(sampler_sha256=SHA_A)), "采样器摘要"),
            "证据 schema 不对": (lambda entry: rewrite_evidence(self.world, entry, lambda e: e.update(schema_version="declared-sample/v0")), "不是属于本单元"),
        }
        for label, (tamper, pattern) in cases.items():
            with self.subTest(label):
                self.world.registry["units"].clear()
                tamper(self.world.declare("backend:go-test"))
                self.refused(pattern)

    def test_t19_evidence_the_sampler_marked_invalid_cannot_back_a_declared_unit(self) -> None:
        """采样器（declared_sampler）自己判过采样有效性；判无效的证据只能登记 rerun-only。"""

        entry = self.world.declare("backend:go-test")
        rewrite_evidence(self.world, entry, lambda e: e.update(valid=False, invalid_reasons=["运行前后 git status 不为空"]))
        self.refused("采样无效.*git status")
        rewrite_evidence(self.world, entry, lambda e: e.update(valid=True, invalid_reasons=[]))
        self.world.write()
        self.world.plan()

    def test_a_local_plan_without_a_launcher_works_the_same_way(self) -> None:
        """本机 plan 的启动前缀为空：登记表里的 launcher_sha256 是空数组的摘要，三参数形式也能反查出空前缀。"""

        world = World(self, launcher=[])
        world.declare("backend:go-test")
        world.rerun_only("capture:prerequisites")
        world.write()
        self.assertEqual(world.plan()["declared_inputs"]["declared_units"], ["backend:go-test"])
        self.assertIsNone(eg.validate_declared_registry(world.registry, list(world.units.values()), world.tree))
        with self.assertRaisesRegex(ValueError, "launcher_sha256"):
            world.plan(launcher=LAUNCHER)

    def test_t19_a_changed_plan_command_or_launcher_invalidates_the_evidence(self) -> None:
        self.world.declare("backend:go-test")
        self.world.write()
        self.world.plan()
        with self.assertRaisesRegex(ValueError, "launcher_sha256"):
            self.world.plan(launcher=["other-isolation"])
        with self.assertRaisesRegex(ValueError, "launcher_sha256"):
            self.world.plan(launcher=[])
        # 计划里命令变了（argv 或工作目录）：证据不再适用。
        units = copy.deepcopy(list(self.world.units.values()))
        for unit in units:
            if unit["unit_id"] == "backend:go-test":
                unit["argv"] = [*unit["argv"], "-race"]
        with self.assertRaisesRegex(ValueError, "command_sha256"):
            eg.validate_declared_registry(self.world.registry, units, self.world.tree, launcher=self.world.launcher)
        units = copy.deepcopy(list(self.world.units.values()))
        for unit in units:
            if unit["unit_id"] == "backend:go-test":
                unit["cwd"] = str(self.world.tree)
        with self.assertRaisesRegex(ValueError, "command_sha256"):
            eg.validate_declared_registry(self.world.registry, units, self.world.tree, launcher=self.world.launcher)

    def test_t19_coverage_must_be_ok_when_registering(self) -> None:
        """登记时就核覆盖：证据里有声明范围外的读取，说明登记表自相矛盾，拒绝生成清单。"""

        self.world.declare("backend:go-test", accesses=[["{TREE}/backend/go.mod", ["read"], True], ["{TREE}/docs/other.md", ["read"], True]])
        self.refused("覆盖检查不通过.*docs/other.md")

    def test_t19_coverage_and_scope_come_from_the_unit_records_functions(self) -> None:
        """自检不自己实现范围与覆盖规则：scope 非法（WP1 的 validate_declared_scope 抛 ValueError）也拒绝。"""

        entry = self.world.declare("backend:go-test", {**default_scope(), "schema_version": "declared-scope/v0"})
        self.refused("scope 不合法")
        for broken in (5, None, [], "scope", {}):
            entry["scope"] = broken
            self.refused("scope 不合法")
        # 覆盖检查函数对畸形证据抛 TypeError／KeyError 时也是 ValueError（plan 失败而不是带着回溯崩掉）。
        entry["scope"] = default_scope()
        with mock.patch.object(eg._unit_records_module(), "declared_coverage", side_effect=KeyError("repo_accesses")):
            self.refused("覆盖检查无法完成")

    def test_t19_unknown_units_are_refused_where_the_whole_command_set_is_planned(self) -> None:
        self.world.rerun_only("backend:no-such-unit")
        self.refused("不在本次规划")
        self.world.registry["units"].clear()
        self.world.rerun_only("backend:unit")
        self.world.write()
        manifest = self.world.plan(profile="preflight")
        self.assertEqual(manifest["declared_inputs"]["not_in_plan_units"], ["backend:unit"], "preflight 只规划一部分：登记了但没规划的跳过并报告")
        self.assertEqual(manifest["declared_inputs"]["rerun_only"], 0)

    def test_t19_macos_only_deploy_tests_may_be_registered_on_either_platform(self) -> None:
        self.world.rerun_only("deploy:tests-apple-container-test.sh", "只能在 macOS 上执行")
        self.world.write()
        linux = self.world.plan()
        self.assertEqual(linux["declared_inputs"]["not_in_plan_units"], ["deploy:tests-apple-container-test.sh"])
        darwin = self.world.plan(platform="darwin")
        self.assertFalse(inputs_of(darwin)["deploy:tests-apple-container-test.sh"]["inheritable"])

    def test_t19_entry_shapes_are_closed(self) -> None:
        units = self.world.registry["units"]
        bad_entries = {
            "rerun-only 缺原因": {"status": "rerun-only"},
            "rerun-only 原因为空": {"status": "rerun-only", "reason": "  "},
            "rerun-only 原因不是字符串": {"status": "rerun-only", "reason": 3},
            "rerun-only 多余键": {"status": "rerun-only", "reason": "x", "scope": {}},
            "rerun-only head 不是布尔": {"status": "rerun-only", "reason": "x", "head": "yes"},
            "status 非法": {"status": "inherit", "reason": "x"},
            "不是对象": "rerun-only",
            "declared 缺 sample": {"status": "declared", "scope": default_scope()},
            "declared 多余键": {"status": "declared", "scope": default_scope(), "sample": {}, "reason": "x"},
        }
        for label, entry in bad_entries.items():
            with self.subTest(label):
                units.clear()
                units["backend:go-test"] = entry
                self.refused("登记表")
        units.clear()
        entry = self.world.declare("backend:go-test")
        for label, mutate in {"sample 缺必填": lambda sample: sample.pop("sampler_sha256"), "sample 多余键": lambda sample: sample.update(extra=1),
                              "摘要不是十六进制": lambda sample: sample.update(command_sha256="XYZ")}.items():
            with self.subTest(label):
                broken = copy.deepcopy(entry)
                mutate(broken["sample"])
                units["backend:go-test"] = broken
                self.refused("sample")

    def test_t19_evidence_path_must_be_a_plain_file_in_the_samples_directory(self) -> None:
        entry = self.world.declare("backend:go-test")
        evidence = self.world.tree / entry["sample"]["evidence"]
        for label, relative in {"目录之外": "docs/x.json", "含 ..": "tools/ci/command_input_samples/../x.json", "绝对路径": str(evidence),
                                "不是 json": "tools/ci/command_input_samples/x.txt", "多一层目录": "tools/ci/command_input_samples/sub/x.json",
                                "不存在": "tools/ci/command_input_samples/none.sample.json", "反斜杠": "tools\\ci\\x.json"}.items():
            with self.subTest(label):
                self.world.registry["units"]["backend:go-test"] = {**entry, "sample": {**entry["sample"], "evidence": relative}}
                self.refused("证据")
        link = evidence.with_name("link.sample.json")
        link.symlink_to(evidence)
        self.world.registry["units"]["backend:go-test"] = {**entry, "sample": {**entry["sample"], "evidence": f"{eg.DECLARED_SAMPLES_DIR}/link.sample.json"}}
        self.refused("符号链接")

    def test_t19_contract_rules(self) -> None:
        def contracts_refused(pattern: str, **scope_kwargs) -> None:
            self.world.registry["units"].clear()
            self.world.declare("backend:go-test", default_scope(**scope_kwargs))
            self.refused(pattern)

        contracts_refused("未评审", contracts=("toolchain:go", "toolchain:rust"))
        contracts_refused("未评审", contracts=("toolchain:go", "cache:no-such-cache"))
        contracts_refused("toolchain:make", contracts=("toolchain:go",), make_expansion=("test-frontend-critical",))
        contracts_refused("ryuk", contracts=("toolchain:go", "service-image:postgres", "service-image:redis"))
        contracts_refused("40 位", git_commits=("abc",))
        contracts_refused("目标名非法", contracts=("toolchain:go", "toolchain:make"), make_expansion=("-n",))
        contracts_refused("没有对应的 .*HOME", contracts=("toolchain:go", "host-config:no-such-file"))
        contracts_refused("未评审", contracts=("toolchain:go", "host-config:Bad_Name"))
        for bad in ("{HOME}/.config/", "{HOME}/../etc/passwd", "/etc/passwd"):
            contracts_refused("精确文件", contracts=("toolchain:go", "host-config:x"),
                              outside=({"prefix": bad, "class": "home-config", "contract": "host-config:x"},))
        # HOME 配置合同的路径写在 scope.outside 的 home-config 行里（采样器按路径派生合同编号），内置的常见配置不需要行。
        self.world.registry["units"].clear()
        self.world.declare("backend:go-test", default_scope(
            contracts=("toolchain:go", "host-config:ssh-config", "host-config:gitconfig"),
            outside=({"prefix": "{HOME}/.ssh/config", "class": "home-config", "contract": "host-config:ssh-config"},)))
        self.world.write()
        self.world.plan()
        # 通过的完整写法：make 展开配 toolchain:make，镜像配 Ryuk，提交号完整。make 展开合同只配 make 命令的单元（步骤 5 WP3b 起入口自检要求命令第一个词是
        # make，所以这里用 frontend:critical，不再用 go test 命令的 backend:go-test）。
        self.world.registry["units"].clear()
        self.world.declare("frontend:critical", default_scope(contracts=("toolchain:go", "toolchain:make", "service-image:postgres", "service-image:redis",
                                                                         "service-image:ryuk"), make_expansion=("test-frontend-critical",),
                                                           git_commits=("d" * 40,)))
        self.world.write()
        self.world.plan()

    def test_t19_service_image_references_must_resolve_from_the_sources(self) -> None:
        """镜像引用从源码常量解析，解析失败 plan 失败；Ryuk 版本不在登记表里也失败（登记方法见 DECLARED_RYUK_DEFAULT_IMAGES）。"""

        images = default_scope(contracts=("toolchain:go", "service-image:postgres", "service-image:redis", "service-image:ryuk"))
        harness = self.world.tree / eg.DECLARED_IMAGE_SOURCE_FILE
        self.world.declare("backend:go-test", images)
        harness.write_text(HARNESS.replace("redisImageTag", "renamedImageTag"), encoding="utf-8")
        self.refused("redis.*解析不了")
        harness.write_text(HARNESS, encoding="utf-8")
        go_mod = self.world.tree / "backend" / "go.mod"
        go_mod.write_text(GO_MOD.replace("testcontainers-go v0.40.0", "testcontainers-go v9.9.9"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"GOMODCACHE": str(self.world.root / "empty-modcache")}):
            self.refused("ryuk.*DECLARED_RYUK_DEFAULT_IMAGES")
        go_mod.write_text(GO_MOD.replace("testcontainers-go v0.40.0 // indirect", ""), encoding="utf-8")
        self.refused("ryuk.*go.mod")

    def test_validate_declared_registry_has_the_frozen_three_argument_form(self) -> None:
        """冻结签名 validate_declared_registry(registry, units, tree)：启动前缀不用另给，按登记的 launcher_sha256 在 argv 前缀里反查。"""

        self.world.declare("backend:go-test")
        self.world.rerun_only("capture:prerequisites")
        units = list(self.world.units.values())
        self.assertIsNone(eg.validate_declared_registry(self.world.registry, units, self.world.tree))
        self.world.registry["units"]["backend:gone"] = {"status": "rerun-only", "reason": "x"}
        with self.assertRaisesRegex(ValueError, "不在本次规划"):
            eg.validate_declared_registry(self.world.registry, units, self.world.tree)
        self.assertIsNone(eg.validate_declared_registry(self.world.registry, units, self.world.tree, strict=False))
        other = [{**unit, "argv": unit["argv"][len(LAUNCHER):]} for unit in units]
        self.world.registry["units"].pop("backend:gone")
        with self.assertRaisesRegex(ValueError, "launcher_sha256"):
            eg.validate_declared_registry(self.world.registry, other, self.world.tree)

    def test_t19_without_a_registry_the_plan_is_byte_identical_to_before(self) -> None:
        with mock.patch.object(eg, "apply_declared_inputs"):
            before = self.world.plan()
        after = self.world.plan()
        self.assertEqual(json.dumps(after, sort_keys=True), json.dumps(before, sort_keys=True))
        self.assertNotIn("declared_inputs", after, "登记表缺失时清单不多出任何键")

    def test_an_empty_registry_changes_nothing_but_adds_the_zero_count_summary(self) -> None:
        with mock.patch.object(eg, "apply_declared_inputs"):
            before = self.world.plan()
        self.world.write()
        after = self.world.plan()
        self.assertEqual({key: value for key, value in after.items() if key != "declared_inputs"}, before)
        summary = after["declared_inputs"]
        self.assertEqual((summary["registered"], summary["declared"], summary["rerun_only"], summary["not_in_plan"]), (0, 0, 0, 0))
        self.assertEqual(summary["registry"]["sha256"], hashlib.sha256(self.world.registry_path.read_bytes()).hexdigest())

    def test_t19_unregistered_units_keep_their_inputs_verbatim(self) -> None:
        with mock.patch.object(eg, "apply_declared_inputs"):
            before = self.world.plan()
        self.world.declare("backend:go-test")
        self.world.rerun_only("capture:prerequisites")
        self.world.write()
        after = self.world.plan()
        untouched = {unit_id: unit for unit_id, unit in inputs_of(after).items() if unit_id not in ("backend:go-test", "capture:prerequisites")}
        self.assertEqual(untouched, {unit_id: unit for unit_id, unit in inputs_of(before).items() if unit_id in untouched},
                         "未登记的单元：整个单元字典（含 inputs、额度、环境）与改前逐字相同")
        for key in set(before) - {"units"}:
            self.assertEqual(after[key], before[key], f"清单顶层键 {key} 不变")
        self.assertEqual(set(after) - set(before), {"declared_inputs"})


class ApplyDeclaredInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        install_wp1_stubs(self)
        self.world = World(self)

    def test_declared_unit_gets_exactly_the_frozen_inputs_shape(self) -> None:
        with mock.patch.object(eg, "apply_declared_inputs"):
            before = inputs_of(self.world.plan())
        entry = self.world.declare("backend:go-test")
        self.world.write()
        unit = inputs_of(self.world.plan())["backend:go-test"]
        self.assertEqual(set(unit["inputs"]), {"declared_scope", "declared_sample"}, "不含 ranges／head／resolved")
        self.assertEqual(unit["inputs"]["declared_scope"], entry["scope"])
        self.assertEqual(unit["inputs"]["declared_sample"],
                         {key: entry["sample"][key] for key in ("evidence_sha256", "command_sha256", "sampler_sha256")})
        # 命令、额度不变；声明了 toolchain:go 合同的单元，清单 env 只新增三个离线变量（步骤 5 WP3：命令运行期离线首发就开）。
        self.assertEqual({key: value for key, value in unit.items() if key not in ("inputs", "env")},
                         {key: value for key, value in before["backend:go-test"].items() if key not in ("inputs", "env")}, "命令、额度不变")
        self.assertEqual(unit.get("env"), {**(before["backend:go-test"].get("env") or {}), **eg.DECLARED_OFFLINE_ENV}, "env 只新增三个离线变量")
        self.assertTrue(unit.get("inheritable", True))

    def test_rerun_only_units_are_marked_not_inheritable_with_their_reason(self) -> None:
        with mock.patch.object(eg, "apply_declared_inputs"):
            before = inputs_of(self.world.plan())
        self.world.rerun_only("capture:prerequisites", "实时检查，1 秒")
        self.world.rerun_only("deploy:tests-docker-compose-security-test.sh", "读 git 历史", head=True)
        self.world.write()
        after = inputs_of(self.world.plan())
        for unit_id, reason in (("capture:prerequisites", "实时检查，1 秒"), ("deploy:tests-docker-compose-security-test.sh", "读 git 历史")):
            self.assertIs(after[unit_id]["inheritable"], False)
            self.assertEqual(after[unit_id]["not_inheritable_reason"], reason)
        self.assertNotIn("head", after["capture:prerequisites"]["inputs"], "没有 head:true 的 rerun-only 去掉 head")
        self.assertEqual(after["capture:prerequisites"]["inputs"]["ranges"], before["capture:prerequisites"]["inputs"]["ranges"])
        self.assertIs(after["deploy:tests-docker-compose-security-test.sh"]["inputs"]["head"], True, "head:true 保留旧的 head 条目")
        self.assertEqual(after["deploy:tests-docker-compose-security-test.sh"]["inputs"], before["deploy:tests-docker-compose-security-test.sh"]["inputs"])

    def test_summary_lists_counts_and_every_rerun_only_reason(self) -> None:
        self.world.declare("backend:go-test")
        self.world.declare("backend:unit")
        self.world.rerun_only("capture:prerequisites", "实时检查")
        self.world.rerun_only("backend:backend-lint", "缓存在 HOME 下")
        self.world.rerun_only("backend:ghost-in-preflight-only")
        self.world.write()
        summary = self.world.plan(profile="preflight")["declared_inputs"]
        self.assertEqual(summary["schema_version"], eg.DECLARED_SUMMARY_SCHEMA)
        self.assertEqual((summary["registered"], summary["declared"], summary["rerun_only"]), (5, 1, 2),
                         "登记 5 项；preflight 只规划 backend:go-test 与两个 rerun-only，另两项没规划")
        self.assertEqual(summary["declared_units"], ["backend:go-test"])
        self.assertEqual(summary["rerun_only_reasons"], {"backend:backend-lint": "缓存在 HOME 下", "capture:prerequisites": "实时检查"})
        self.assertEqual(summary["not_in_plan_units"], ["backend:ghost-in-preflight-only", "backend:unit"])
        self.assertEqual(summary["not_in_plan"], 2)
        self.assertEqual(summary["registry"]["sha256"], hashlib.sha256(self.world.registry_path.read_bytes()).hexdigest())
        self.assertEqual(summary["registry"]["path"], eg.DECLARED_REGISTRY_PATH)

    def test_generated_manifest_is_accepted_by_the_executor_loader(self) -> None:
        self.world.declare("backend:go-test")
        self.world.rerun_only("capture:prerequisites")
        self.world.write()
        manifest = self.world.plan()
        path = self.world.root / "gates-manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        _groups, units, gates, payload = ue.load_gates_manifest(path, machine_cores=4)
        self.assertEqual(len(units), len(manifest["units"]))
        self.assertEqual(payload["declared_inputs"]["declared"], 1)

    def test_make_expansion_units_become_rerun_only_when_the_makefile_evaluates_at_parse_time(self) -> None:
        """T15 的 plan 部分：展开探测永远只读。Makefile 含解析期求值的写法（调研指定的 $(shell|wildcard|eval|call)、include，
        以及同样有副作用的 $(file/$(load/shell 赋值 !=），声明了 make 展开合同的单元转 rerun-only，原因写进汇总。"""

        scope = default_scope(contracts=("toolchain:go", "toolchain:make"), make_expansion=("test-frontend-critical",))
        constructs = {"shell": "BUILD_ID := $(shell git rev-parse HEAD)\n", "wildcard": "SPECS := $(wildcard src/*.ts)\n",
                      "eval": "$(eval EXTRA := 1)\n", "call": "NAME := $(call helper,1)\n", "file": "$(file >out.txt,hello)\n",
                      "include": "include other.mk\n", "-include": "-include other.mk\n", "感叹号等于": "STAMP != date\n",
                      "大括号写法": "BUILD_ID := ${shell date}\n"}
        for label, line in constructs.items():
            with self.subTest(label):
                world = World(self, makefile=makefile_text(extra=line), extra_files={"other.mk": "OTHER := 1\n"})
                world.declare("frontend:critical", scope)
                world.write()
                manifest = world.plan()
                unit = inputs_of(manifest)["frontend:critical"]
                self.assertIs(unit["inheritable"], False)
                self.assertIn("make -n 展开探测不是只读", unit["not_inheritable_reason"])
                self.assertEqual(manifest["declared_inputs"]["converted_to_rerun_only"], {"frontend:critical": unit["not_inheritable_reason"]})
                self.assertEqual((manifest["declared_inputs"]["declared"], manifest["declared_inputs"]["rerun_only"]), (0, 1))
        # 注释里的、配方行里的 != 不算；没有展开合同的单元不受 Makefile 影响。
        harmless = "# $(shell ls) 只是注释\n  # include foo.mk\ncheck:\n\t@if [ \"$$A\" != \"$$B\" ]; then echo x; fi\n"
        world = World(self, makefile=makefile_text(extra=harmless))
        world.declare("frontend:critical", scope)
        world.write()
        self.assertEqual(set(inputs_of(world.plan())["frontend:critical"]["inputs"]), {"declared_scope", "declared_sample"})
        world = World(self, makefile=makefile_text(extra=constructs["shell"]))
        world.declare("backend:go-test")
        world.write()
        self.assertEqual(set(inputs_of(world.plan())["backend:go-test"]["inputs"]), {"declared_scope", "declared_sample"})

    def test_the_make_scan_reads_the_makefile_make_would_read_and_ignores_lookalikes(self) -> None:
        tree = self.world.tree
        self.assertIsNone(eg._declared_make_problem(tree))
        # make 优先读 GNUmakefile：它含解析期求值就算数，即使 Makefile 本身干净。
        (tree / "GNUmakefile").write_text("X := $(shell date)\n", encoding="utf-8")
        self.assertIn("GNUmakefile", eg._declared_make_problem(tree))
        (tree / "GNUmakefile").unlink()
        # 长得像、但不是解析期求值的写法不误伤：函数名后没有空白、include 开头的目标名、叫 file_list 的变量、配方里的 $$(…)。
        harmless = ("include-check:\n\t@echo ok\nload-test:\n\t@echo ok\nFILES := $(file_list) $(filter a,b) $(shellcheck) $(call_helper)\n"
                    "SORTED := $(sort b a)\nCMD = $(if $(X),yes,no)\nok:\n\t@x=$$(echo hi); echo $$x\n\t@echo include foo\n")
        (tree / "Makefile").write_text(makefile_text(extra=harmless), encoding="utf-8")
        self.assertIsNone(eg._declared_make_problem(tree))
        # 配方行里的 $(shell …) 仍然抓得到：make 打印配方时也会展开它。
        (tree / "Makefile").write_text(makefile_text(extra="bad:\n\t@echo $(shell date)\n"), encoding="utf-8")
        self.assertIn("$(shell", eg._declared_make_problem(tree))
        (tree / "Makefile").unlink()
        self.assertIn("没有 Makefile", eg._declared_make_problem(tree))

    def test_cli_declared_registry_option(self) -> None:
        self.world.declare("backend:go-test")
        registry = self.world.root / "elsewhere-registry.json"
        registry.write_text(json.dumps(self.world.registry, ensure_ascii=False), encoding="utf-8")
        output = self.world.root / "manifest.json"
        base = ["plan", "--tree", str(self.world.tree), "--profile", "full-gates", "--launcher-json", json.dumps(LAUNCHER), "--output", str(output)]
        with mock.patch.object(eg, "environment_facts", return_value=[]), mock.patch.object(eg, "historical_source_input", return_value={
                "category": "environment", "name": "tree:none", "sha256": SHA_A, "detail": {}}), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(eg.main([*base, "--declared-registry", str(registry)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["declared_inputs"]["declared_units"], ["backend:go-test"])
            output.unlink()
            self.assertEqual(eg.main(base), 0, "测试树默认位置没有登记表：不启用，也不报错")
            self.assertNotIn("declared_inputs", json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(eg.main([*base, "--declared-registry", str(self.world.root / "none.json")]), 2, "显式指定的登记表必须存在")


class ContractProbeTests(unittest.TestCase):
    """合同探测：工具链、缓存、服务镜像、git 历史、make 展开、HOME 配置。全部用假 runner 与临时目录（T14、T15 各有一条真实 git／make）。"""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.tree = make_tree(self.root)
        self.tc = Toolchain(self.root, self.tree)
        self.counter = 0

    def fresh(self) -> Toolchain:
        """另起一套假工具链世界（互不影响的目录），让每个子用例从同一个起点出发。"""

        self.counter += 1
        base = self.root / f"world{self.counter}"
        base.mkdir()
        return Toolchain(base, self.tree)

    def entries(self, scope: dict, tc: Toolchain | None = None, **kwargs) -> list[dict]:
        return (tc or self.tc).probe(**kwargs).entries(declaration(scope))

    # ---- T9：合同变化只让相关条目变化 ----

    def test_t9_a_toolchain_change_changes_only_that_toolchains_entry(self) -> None:
        scope = default_scope(contracts=("toolchain:go", "toolchain:node", "toolchain:pnpm", "toolchain:make", "cache:go-build-cache", "cache:go-mod-cache"))
        cases = [
            ("go 版本行", lambda tc: tc.versions.update(go="go version go1.27.1 linux/arm64"), "go"),
            ("go 可执行文件", lambda tc: tc.set_binary("go", "go-binary-2"), "go"),
            ("GOROOT/VERSION", lambda tc: (tc.goroot / "VERSION").write_text("go1.27.1\n", encoding="utf-8"), "go"),
            ("GOROOT/bin/go", lambda tc: (tc.goroot / "bin" / "go").write_text("goroot-go-2", encoding="utf-8"), "go"),
            *[(f"工具 {tool}", lambda tc, tool=tool: (tc.tool_dir / tool).write_text(f"{tool}-2", encoding="utf-8"), "go") for tool in eg.DECLARED_GO_TOOLS],
            *[(f"go 环境 {key}", lambda tc, key=key: tc.go_env.update({key: "changed"}), "go") for key in eg.DECLARED_GO_ENV_KEYS],
            ("node 版本", lambda tc: tc.versions.update(node="v20.19.1"), "node"),
            ("node 可执行文件", lambda tc: tc.set_binary("node", "node-binary-2"), "node"),
            ("pnpm 版本", lambda tc: tc.versions.update(pnpm="10.4.2"), "pnpm"),
            ("pnpm 可执行文件", lambda tc: tc.set_binary("pnpm", "pnpm-binary-2"), "pnpm"),
            ("make 版本", lambda tc: tc.versions.update(make="GNU Make 4.4"), "make"),
            ("make 可执行文件", lambda tc: tc.set_binary("make", "make-binary-2"), "make"),
        ]
        for label, mutate, tool in cases:
            with self.subTest(label):
                tc = self.fresh()
                before = entry_digests(self.entries(scope, tc))
                self.assertEqual(len(before), 6)
                mutate(tc)
                after = entry_digests(self.entries(scope, tc))
                self.assertEqual({name for name in before if before[name] != after[name]}, {f"contract:toolchain:{tool}"})

    def test_t9_a_changed_image_id_changes_only_the_integration_inputs(self) -> None:
        """image_id 变 → 仅 integration 单元的输入明细总摘要变；只声明工具链与缓存的单元（go test）不受影响。"""

        go_test = default_scope(contracts=("toolchain:go", "cache:go-build-cache", "cache:go-mod-cache"))
        integration = default_scope(contracts=("toolchain:go", "cache:go-build-cache", "cache:go-mod-cache", "service-image:postgres",
                                               "service-image:redis", "service-image:ryuk"))

        def inputs_digests(tc: Toolchain) -> dict[str, str]:
            session = tc.probe()
            return {"go-test": ur.entries_sha256(session(declaration(go_test))), "integration": ur.entries_sha256(session(declaration(integration)))}

        for image in Toolchain.IMAGES:
            with self.subTest(image):
                tc = self.fresh()
                before = inputs_digests(tc)
                tc.image_ids[image] = "sha256:" + "9" * 64
                after = inputs_digests(tc)
                self.assertEqual(after["go-test"], before["go-test"])
                self.assertNotEqual(after["integration"], before["integration"])
        tc = self.fresh()
        # 本用例只关心镜像 Id；离线要求（常量缺省已开）由 test_offline_requirement_switch 单独覆盖，这里显式关掉。
        entries = self.entries(integration, tc, require_offline=False)
        postgres = next(entry for entry in entries if entry["name"] == "contract:service-image:postgres")
        self.assertEqual(postgres["detail"]["image_id"], tc.image_ids["postgres:18.1-alpine3.23"])
        self.assertEqual(postgres["detail"]["reference"], "postgres:18.1-alpine3.23")
        self.assertEqual(eg.declared_contract_problems(entries), [])

    def test_go_probe_runs_in_the_module_directory(self) -> None:
        """go 的工具链选择（GOTOOLCHAIN=auto 时按 go.mod 的 go 行切换）看工作目录：必须在 go.mod 所在目录探测。"""

        self.entries(default_scope(contracts=("toolchain:go", "toolchain:node")))
        by_argv = {call[0]: call[1] for call in self.tc.runner.calls}
        self.assertEqual(by_argv[("go", "version")], str(self.tree / "backend"))
        self.assertEqual(by_argv[("node", "--version")], str(self.tree), "其余工具在测试树根探测")
        self.assertTrue(all(call[2] == self.tc.environ for call in self.tc.runner.calls), "所有探测命令都带本会话的环境")

    def test_offline_requirement_switch(self) -> None:
        """命令运行期离线要求（可选项）：开关打开后 toolchain:go 条目带 requires.env_equals 与逐项核对结果。"""

        scope = default_scope(contracts=("toolchain:go",))
        self.assertIs(eg.DECLARED_REQUIRE_OFFLINE, True, "首发就开（步骤 5 WP3）：常量缺省为开")
        off = self.entries(scope, require_offline=False)[0]
        self.assertNotIn("requires", off["detail"])
        self.assertNotIn("requirements", off["detail"])
        on = self.entries(scope, require_offline=True)[0]
        self.assertEqual(on["detail"]["requires"], {"env_equals": eg.DECLARED_OFFLINE_ENV})
        self.assertIs(on["detail"]["requirements"]["met"], False)
        self.assertEqual(set(on["detail"]["requirements"]["unmet"]), {"GOFLAGS", "GOPROXY", "GOTOOLCHAIN"})
        self.assertEqual(on["detail"]["requirements"]["unmet"]["GOPROXY"], {"expected": "off", "actual": "https://proxy.golang.org,direct"})
        self.assertNotEqual(on["sha256"], off["sha256"], "要求本身是合同声明的一部分：开关切换会让条目变化")
        self.assertTrue(any("离线" in problem for problem in eg.declared_contract_problems([on])))
        self.tc.go_env.update(GOFLAGS="-mod=readonly", GOPROXY="off", GOTOOLCHAIN="local")
        met = self.entries(scope, require_offline=True)[0]
        self.assertEqual(met["detail"]["requirements"], {"met": True, "unmet": {}})
        self.assertEqual(eg.declared_contract_problems([met]), [])
        self.assertNotEqual(met["sha256"], on["sha256"])
        # 常量就是默认开关；显式参数覆盖常量。
        with mock.patch.object(eg, "DECLARED_REQUIRE_OFFLINE", True):
            self.assertIn("requirements", self.entries(scope)[0]["detail"])
            self.assertNotIn("requirements", self.entries(scope, require_offline=False)[0]["detail"])
        # go env 本身取不到时，要求也算未满足。
        broken = FakeRunner({key: value for key, value in self.tc.responses().items() if key[:2] != ("go", "env")})
        entry = self.entries(scope, require_offline=True, runner=broken)[0]
        self.assertIs(entry["detail"]["requirements"]["met"], False)

    def test_failed_probes_are_unavailable_never_exceptions_and_are_reported(self) -> None:
        scope = default_scope(contracts=("toolchain:go", "toolchain:node", "service-image:postgres", "service-image:redis", "service-image:ryuk",
                                         "host-config:gitconfig"), make_expansion=("test-frontend-critical",), git_commits=("a" * 40,))
        empty_path = self.root / "empty-path"
        empty_path.mkdir()
        failing = self.entries(scope, runner=FakeRunner(), environ={"PATH": str(empty_path), "HOME": str(self.tc.home)})
        details = {entry["name"]: entry["detail"] for entry in failing}
        self.assertEqual(details["contract:toolchain:go"]["version_line"], "unavailable(rc=127)")
        self.assertEqual(details["contract:toolchain:go"]["executable_sha256"], "missing")
        self.assertEqual(details["contract:toolchain:go"]["env"], "unavailable(rc=127)")
        for image in ("postgres", "redis", "ryuk"):
            self.assertEqual(details[f"contract:service-image:{image}"]["image_id"], "unavailable(rc=127)")
        self.assertEqual(details["contract:git-history:" + "a" * 40]["tree"], "unavailable(rc=127)")
        self.assertEqual(details["contract:make-expansion:test-frontend-critical"]["expansion_sha256"], "unavailable(rc=127)")
        problems = eg.declared_contract_problems(failing)
        self.assertGreaterEqual(len(problems), 7)
        self.assertTrue(all(problem.startswith("contract:") for problem in problems))
        self.tc.runner.responses[("make", "-s", "--no-print-directory", "-n", "test-frontend-critical")] = (0, "echo hi\n")
        self.tc.runner.responses[("git", "rev-parse", "--verify", "a" * 40 + "^{tree}")] = (0, "b" * 40 + "\n")
        working = entry_digests(self.entries(scope))
        self.assertTrue(all(entry["sha256"] != working[entry["name"]] for entry in failing if "host-config" not in entry["name"]),
                        "失败值与成功值不同：不会把失败时的记录错认成成功时的")

    # ---- T10：缓存合同 ----

    def test_t10_cache_entries_are_stable_declarations_that_do_not_change_when_the_caches_are_written(self) -> None:
        work = self.root / "work"
        caches = {"GOCACHE": work / "go-build-cache", "GOMODCACHE": work / "go-mod-cache", "GOTMPDIR": work / "go-tmp"}
        for path in caches.values():
            path.mkdir(parents=True)
        environ = {**self.tc.environ, **{key: str(path) for key, path in caches.items()}}
        ids = tuple(eg.DECLARED_CACHES)
        scope = default_scope(contracts=tuple(f"cache:{name}" for name in ids))
        before = self.entries(scope, environ=environ)
        self.assertEqual([entry["name"] for entry in before], sorted(f"contract:cache:{name}" for name in ids))
        for path in caches.values():
            (path / "ab").mkdir()
            (path / "ab" / "entry").write_text("热缓存的内容" * 50, encoding="utf-8")
            os.utime(path, (1, 1))
        (self.tree / "frontend" / "node_modules" / ".vite").mkdir(parents=True)
        (self.tree / "frontend" / "node_modules" / ".vite" / "results.json").write_text("{}", encoding="utf-8")
        (self.tree / "frontend" / "tsconfig.tsbuildinfo").write_text("{}", encoding="utf-8")
        after = self.entries(scope, environ=environ)
        self.assertEqual(after, before, "缓存由冷变热（内容、大小、mtime 都变）：条目逐字不变，所以未受影响的单元仍可承接")
        text = json.dumps(before, ensure_ascii=False)
        for absolute in (str(work), str(self.tree), str(self.root)):
            self.assertNotIn(absolute, text, "稳定声明里不含绝对路径（位置只写类别），换工作根不失效")
        node_modules = next(entry for entry in before if entry["name"] == "contract:cache:node-modules")
        self.assertEqual((node_modules["detail"]["role"], node_modules["detail"]["rule"], node_modules["detail"]["location"]),
                         ("dependency-install", "lock-derived-pnpm", "tree"))
        self.assertEqual(node_modules["detail"]["lock_file"], "frontend/pnpm-lock.yaml")

    def test_cache_declaration_changes_with_the_definition_table(self) -> None:
        """声明本身变化（改规则、改角色）必须让条目变化：这是「声明变了就失效」的来源。"""

        scope = default_scope(contracts=("cache:go-build-cache",))
        before = self.entries(scope)[0]
        changed = {**eg.DECLARED_CACHES, "go-build-cache": {**eg.DECLARED_CACHES["go-build-cache"], "rule": "snapshot"}}
        with mock.patch.object(eg, "DECLARED_CACHES", changed):
            self.assertNotEqual(self.entries(scope)[0]["sha256"], before["sha256"])

    def test_cache_locations_reuse_the_warmup_validator(self) -> None:
        work = self.root / "work"
        names = {"GOCACHE": "go-build-cache", "GOMODCACHE": "go-mod-cache", "GOTMPDIR": "go-tmp"}
        environ = {key: str(work / name) for key, name in names.items()}
        for name in names.values():
            (work / name).mkdir(parents=True)
        located = eg.declared_cache_locations(self.tree, work, environ)
        self.assertEqual({key: str(path) for key, path in located.items()}, environ)
        (work / "real").mkdir()
        (work / "linked").symlink_to(work / "real")
        bad_layouts = {
            "缓存根在测试树内": {**environ, "GOCACHE": str(self.tree / "cache")},
            "缓存根在工作根之外": {**environ, "GOCACHE": str(self.root / "elsewhere")},
            "缓存根是符号链接": {**environ, "GOCACHE": str(work / "linked")},
            "两个缓存互相包含": {**environ, "GOTMPDIR": str(work / "go-build-cache" / "tmp")},
            "缓存根是相对路径": {**environ, "GOCACHE": "relative/cache"},
            "缺少缓存变量": {key: value for key, value in environ.items() if key != "GOMODCACHE"},
        }
        for label, layout in bad_layouts.items():
            with self.subTest(label), self.assertRaises(ValueError):
                eg.declared_cache_locations(self.tree, work, layout)

    # ---- T14：git 历史合同 ----

    def test_t14_git_history_entry_follows_the_commits_tree_and_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = scaffold._repo(Path(directory).resolve())
            first = scaffold._git(repo, "rev-parse", "HEAD").strip()
            scaffold._write(repo, {"pkg/mod.py": "VALUE = 2\n"})
            scaffold._commit(repo, "第二次")
            second = scaffold._git(repo, "rev-parse", "HEAD").strip()
            environ = {"PATH": os.environ["PATH"], "HOME": directory}

            def entry_for(commit: str) -> dict:
                entries = eg.declared_probe(environ=environ, tree=repo).entries(declaration(default_scope(contracts=(), git_commits=(commit,))))
                self.assertEqual([entry["name"] for entry in entries], [f"contract:git-history:{commit}"])
                return entries[0]

            first_entry, second_entry = entry_for(first), entry_for(second)
            self.assertNotEqual(first_entry["sha256"], second_entry["sha256"], "两个提交的树对象不同：条目不同")
            self.assertEqual(first_entry["detail"]["tree"], scaffold._git(repo, "rev-parse", f"{first}^{{tree}}").strip())
            scaffold._write(repo, {"pkg/mod.py": "VALUE = 3\n"})
            scaffold._commit(repo, "第三次：HEAD 移动")
            self.assertNotEqual(scaffold._git(repo, "rev-parse", "HEAD").strip(), second)
            self.assertEqual((entry_for(first), entry_for(second)), (first_entry, second_entry), "HEAD 移动、新提交不改变旧提交的条目")
            missing = entry_for("e" * 40)
            self.assertTrue(missing["detail"]["tree"].startswith("unavailable("), "提交缺失（浅克隆）：记 unavailable，不抛异常")
            self.assertTrue(eg.declared_contract_problems([missing]))

    def test_t14_the_entry_changes_with_the_tree_id_the_runner_reports(self) -> None:
        commit = "f" * 40
        scope = default_scope(contracts=(), git_commits=(commit,))
        argv = ("git", "rev-parse", "--verify", f"{commit}^{{tree}}")
        digests = []
        for tree_id in ("1" * 40, "2" * 40, "1" * 40):
            runner = FakeRunner({argv: (0, tree_id + "\n")})
            entry = eg.declared_probe(runner=runner, environ=self.tc.environ, tree=self.tree).entries(declaration(scope))[0]
            digests.append(entry["sha256"])
            self.assertEqual(entry["detail"]["tree"], tree_id)
            self.assertEqual(runner.calls[0][1], str(self.tree), "在测试树里取")
        self.assertEqual(digests[0], digests[2])
        self.assertNotEqual(digests[0], digests[1])
        with self.assertRaisesRegex(ValueError, "40 位"):
            eg.declared_probe(runner=FakeRunner(), tree=self.tree).entries(declaration(default_scope(contracts=(), git_commits=("HEAD",))))

    # ---- T15：make 展开合同 ----

    @unittest.skipUnless(shutil.which("make"), "需要 make")
    def test_t15_make_expansion_changes_only_for_the_target_that_uses_the_changed_variable(self) -> None:
        targets = ("test-frontend-critical", "egress-spec-go-test")
        scope = default_scope(contracts=(), make_expansion=targets)
        environ = {"PATH": os.environ["PATH"]}

        def digests(tree: Path) -> dict[str, str]:
            return entry_digests(eg.declared_probe(environ=environ, tree=tree).entries(declaration(scope)))

        one = make_tree(self.root / "one")
        critical, egress = (f"contract:make-expansion:{target}" for target in targets)
        before = digests(one)
        self.assertEqual(set(before), {critical, egress})
        (one / "Makefile").write_text(makefile_text(vitest=("src/b.spec.ts", "src/a.spec.ts")), encoding="utf-8")
        reordered = digests(one)
        self.assertNotEqual(reordered[critical], before[critical], "FRONTEND_CRITICAL_VITEST 换序：frontend:critical 的展开变")
        self.assertEqual(reordered[egress], before[egress], "egress-spec-go-test 的展开不含该变量：不变")
        (one / "Makefile").write_text(makefile_text(vitest=("src/b.spec.ts", "src/a.spec.ts"),
                                                    extra="# 注释变化\nunrelated:\n\t@echo 无关目标的配方\n"), encoding="utf-8")
        self.assertEqual(digests(one), reordered, "改无关目标、改注释：两个展开都不变")
        # 同一份 Makefile 放在另一个路径：$(CURDIR) 规范化为 {TREE}，摘要相同。
        two = make_tree(self.root / "two", makefile=makefile_text(vitest=("src/b.spec.ts", "src/a.spec.ts")))
        self.assertEqual(digests(two), reordered)
        entries = eg.declared_probe(environ=environ, tree=two).entries(declaration(scope))
        text = {entry["name"]: entry["detail"]["text"] for entry in entries}
        self.assertIn("workdir={TREE}", text[egress])
        self.assertRegex(text[critical], r"vitest run\s+src/b\.spec\.ts\s+src/a\.spec\.ts")
        self.assertNotIn(str(two), json.dumps(entries, ensure_ascii=False))
        # 目标不存在：make 非零退出，记 unavailable。
        missing = eg.declared_probe(environ=environ, tree=two).entries(declaration(default_scope(contracts=(), make_expansion=("no-such-target",))))[0]
        self.assertTrue(missing["detail"]["expansion_sha256"].startswith("unavailable("))

    def test_t15_make_expansion_is_taken_in_the_tree_with_the_session_environment(self) -> None:
        argv = ("make", "-s", "--no-print-directory", "-n", "test-frontend-critical")
        runner = FakeRunner({argv: (0, f"pnpm --dir frontend exec vitest run src/a.spec.ts\necho {self.tree}/frontend\n")})
        scope = default_scope(contracts=(), make_expansion=("test-frontend-critical",))
        first = eg.declared_probe(runner=runner, environ={"PATH": "/x"}, tree=self.tree).entries(declaration(scope))[0]
        # 会话环境照给，另清空 MAKEFLAGS 与 MAKEFILES（步骤 5 WP3b：调用方环境里的 -j、-t 或额外 makefile 不该改变展开语义）。
        self.assertEqual(runner.calls[0][1:], (str(self.tree), {"PATH": "/x", "MAKEFLAGS": "", "MAKEFILES": ""}))
        self.assertIn("echo {TREE}/frontend", first["detail"]["text"])
        # 换一个路径不同的树（模拟换工作根）、展开相同：规范化后摘要相同。
        other_tree = make_tree(self.root / "other")
        moved_runner = FakeRunner({argv: (0, f"pnpm --dir frontend exec vitest run src/a.spec.ts\necho {other_tree}/frontend\n")})
        moved = eg.declared_probe(runner=moved_runner, environ={"PATH": "/x"}, tree=other_tree).entries(declaration(scope))[0]
        self.assertEqual(moved["sha256"], first["sha256"])
        # 共享前缀的另一个目录不被截坏。
        sibling = FakeRunner({argv: (0, f"echo {self.tree}-hist/frontend\n")})
        text = eg.declared_probe(runner=sibling, environ={"PATH": "/x"}, tree=self.tree).entries(declaration(scope))[0]["detail"]["text"]
        self.assertIn(f"{self.tree}-hist", text)
        self.assertNotIn("{TREE}", text)

    # ---- T16：HOME 配置 ----

    def test_t16_host_config_follows_existence_and_content(self) -> None:
        ids = ("gitconfig", "docker-config-json", "npmrc", "config-go-env", "testcontainers-properties")
        scope = default_scope(contracts=tuple(f"host-config:{name}" for name in ids))
        home = self.tc.home
        before = self.entries(scope)
        self.assertEqual({host_state(entry) for entry in before}, {"missing"}, "缺失也是一种状态")
        self.assertEqual(eg.declared_contract_problems(before), [], "配置文件缺失不是问题")
        (home / ".gitconfig").write_text("[user]\n\tname = 甲\n", encoding="utf-8")
        appeared = entry_digests(self.entries(scope))
        self.assertEqual({name for name, digest in appeared.items() if digest != entry_digests(before)[name]}, {"contract:host-config:gitconfig"})
        (home / ".gitconfig").write_text("[user]\n\tname = 乙\n", encoding="utf-8")
        edited = entry_digests(self.entries(scope))
        self.assertNotEqual(edited["contract:host-config:gitconfig"], appeared["contract:host-config:gitconfig"])
        (home / ".gitconfig").unlink()
        self.assertEqual(self.entries(scope), before, "删掉后回到缺失状态：条目与最初逐字相同")
        (home / ".docker").mkdir()
        (home / ".docker" / "config.json").write_text('{"auths": {}}', encoding="utf-8")
        (home / ".config" / "go").mkdir(parents=True)
        (home / ".config" / "go" / "env").write_text("GOFLAGS=-mod=mod\n", encoding="utf-8")
        nested = {entry["name"]: host_state(entry) for entry in self.entries(scope)}
        self.assertEqual(nested["contract:host-config:docker-config-json"], hashlib.sha256(b'{"auths": {}}').hexdigest())
        self.assertEqual(nested["contract:host-config:config-go-env"], hashlib.sha256(b"GOFLAGS=-mod=mod\n").hexdigest())
        # 符号链接读目标内容；目录不是配置文件；HOME 不可用时失败关闭。
        (home / "real.rc").write_text("registry=https://example.invalid\n", encoding="utf-8")
        (home / ".npmrc").symlink_to(home / "real.rc")
        self.assertEqual({entry["name"]: host_state(entry) for entry in self.entries(scope)}["contract:host-config:npmrc"],
                         hashlib.sha256(b"registry=https://example.invalid\n").hexdigest())
        (home / ".testcontainers.properties").mkdir()
        self.assertEqual({entry["name"]: host_state(entry) for entry in self.entries(scope)}["contract:host-config:testcontainers-properties"], "not-a-file")
        no_home = self.entries(scope, environ={"PATH": self.tc.environ["PATH"]})
        self.assertEqual({host_state(entry) for entry in no_home}, {"unavailable(no-home)"})
        self.assertEqual(len(eg.declared_contract_problems(no_home)), len(ids))
        self.assertNotIn(str(home), json.dumps(before, ensure_ascii=False), "位置只写 {HOME} 别名")
        self.assertEqual(before[0]["detail"]["files"][0]["alias"].startswith("{HOME}/"), True)

    def test_host_config_paths_come_from_the_scope_so_a_newly_sampled_home_file_needs_no_code_change(self) -> None:
        """采样器按采到的路径派生合同编号（{HOME}/.config/git/config → host-config:config-git-config），路径写在
        scope.outside 的 home-config 行里：探测从 scope 取路径；一个合同可以绑定多个文件。"""

        home = self.tc.home
        rows = [{"prefix": "{HOME}/.config/git/config", "class": "home-config", "contract": "host-config:config-git-config"},
                {"prefix": "{HOME}/.config/git/ignore", "class": "home-config", "contract": "host-config:config-git-config"},
                {"prefix": "{HOME}/.ssh/config", "class": "home-config", "contract": "host-config:ssh-config"},
                {"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"}]
        scope = {**default_scope(contracts=("host-config:config-git-config", "host-config:ssh-config")), "outside": rows}
        before = {entry["name"]: entry for entry in self.entries(scope)}
        git = before["contract:host-config:config-git-config"]["detail"]
        self.assertEqual([item["alias"] for item in git["files"]], ["{HOME}/.config/git/config", "{HOME}/.config/git/ignore"])
        self.assertEqual({item["state"] for item in git["files"]}, {"missing"})
        (home / ".config" / "git").mkdir(parents=True)
        (home / ".config" / "git" / "ignore").write_text("*.swp\n", encoding="utf-8")
        after = {entry["name"]: entry for entry in self.entries(scope)}
        self.assertNotEqual(after["contract:host-config:config-git-config"]["sha256"], before["contract:host-config:config-git-config"]["sha256"])
        self.assertEqual(after["contract:host-config:ssh-config"], before["contract:host-config:ssh-config"], "别的合同的文件没变：条目不变")
        self.assertEqual(after["contract:host-config:config-git-config"]["detail"]["files"][1]["state"], hashlib.sha256(b"*.swp\n").hexdigest())
        # 没有 outside 行、也不在内置表里的编号：探测报错（plan 阶段已拒绝，见 test_t19_contract_rules）。
        with self.assertRaisesRegex(ValueError, "未知的 HOME 配置合同"):
            self.entries(default_scope(contracts=("host-config:no-such-file",)))
        for bad in ("{HOME}/.config/", "/etc/passwd", "{HOME}/../etc/passwd", "{HOME}/"):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "精确文件"):
                self.entries({**default_scope(contracts=("host-config:x",)),
                              "outside": [{"prefix": bad, "class": "home-config", "contract": "host-config:x"}]})

    # ---- 服务镜像：引用来源、覆盖、Ryuk、Hub 前缀 ----

    def test_ryuk_reference_is_resolved_and_the_digest_does_not_depend_on_cache_warmth(self) -> None:
        scope = default_scope(contracts=("service-image:ryuk",))
        cold = self.entries(scope)[0]
        self.assertEqual(cold["detail"]["reference"], "testcontainers/ryuk:0.13.0")
        self.assertEqual(cold["detail"]["resolved_from"], "builtin-table", "plan 在预热编译之前，模块缓存还是空的：用登记表")
        self.assertEqual(cold["detail"]["source"], {"module": "github.com/testcontainers/testcontainers-go", "version": "v0.40.0", "const": "ReaperDefaultImage"})
        modcache = self.root / "modcache"
        source = modcache / "github.com" / "testcontainers" / "testcontainers-go@v0.40.0" / "internal" / "config" / "config.go"
        source.parent.mkdir(parents=True)
        source.write_text('package config\n\nconst ReaperDefaultImage = "testcontainers/ryuk:0.13.0"\n', encoding="utf-8")
        warm = self.entries(scope, environ={**self.tc.environ, "GOMODCACHE": str(modcache)})[0]
        self.assertEqual(warm["detail"]["resolved_from"], "module-cache")
        self.assertEqual(warm["sha256"], cold["sha256"], "缓存由冷变热不改变摘要：解析来源只进 detail，不进摘要")
        # 模块缓存里的源码与登记表不一致：以源码为准（升级后第一次执行不会沿用过期的表）。
        source.write_text('const ReaperDefaultImage = "testcontainers/ryuk:0.14.0"\n', encoding="utf-8")
        self.tc.runner.responses[("docker", "image", "inspect", "--format", "{{.Id}}", "testcontainers/ryuk:0.14.0")] = (0, "sha256:" + "7" * 64 + "\n")
        newer = self.entries(scope, environ={**self.tc.environ, "GOMODCACHE": str(modcache)})[0]
        self.assertEqual(newer["detail"]["reference"], "testcontainers/ryuk:0.14.0")
        self.assertEqual(newer["detail"]["image_id"], "sha256:" + "7" * 64)
        self.assertNotEqual(newer["sha256"], cold["sha256"])

    def test_an_unresolvable_ryuk_reference_fails_closed_at_probe_time(self) -> None:
        (self.tree / "backend" / "go.mod").write_text(GO_MOD.replace("testcontainers-go v0.40.0 // indirect", "testcontainers-go v9.9.9 // indirect"), encoding="utf-8")
        entry = self.entries(default_scope(contracts=("service-image:ryuk",)))[0]
        self.assertEqual((entry["detail"]["reference"], entry["detail"]["image_id"]), ("unresolved", "unavailable(unresolved-reference)"))
        self.assertIn("DECLARED_RYUK_DEFAULT_IMAGES", entry["detail"]["error"])
        self.assertTrue(eg.declared_contract_problems([entry]))

    def test_the_ryuk_table_covers_the_real_testcontainers_version_of_this_repository(self) -> None:
        """升级 testcontainers-go 时这条会红，提醒把新版本的 ReaperDefaultImage 登记进 DECLARED_RYUK_DEFAULT_IMAGES；
        本机模块缓存里有源码时顺便核对登记值与源码一致。"""

        go_mod = (REPO_ROOT / "backend" / "go.mod").read_text(encoding="utf-8")
        found = re.search(r"^\s*(?:require\s+)?github\.com/testcontainers/testcontainers-go\s+(v\S+)", go_mod, re.M)
        self.assertIsNotNone(found, "backend/go.mod 里没有 testcontainers-go")
        version = found.group(1)
        self.assertIn(version, eg.DECLARED_RYUK_DEFAULT_IMAGES,
                      f"testcontainers-go 升到 {version}：请把它的 ReaperDefaultImage 登记进 entry_gates.DECLARED_RYUK_DEFAULT_IMAGES")
        modcache = os.environ.get("GOMODCACHE") or str(Path.home() / "go" / "pkg" / "mod")
        source = Path(modcache) / "github.com" / "testcontainers" / f"testcontainers-go@{version}" / "internal" / "config" / "config.go"
        if source.is_file():
            declared = re.search(r'ReaperDefaultImage\s*=\s*"([^"\n]+)"', source.read_text(encoding="utf-8"))
            self.assertIsNotNone(declared)
            self.assertEqual(eg.DECLARED_RYUK_DEFAULT_IMAGES[version], declared.group(1))

    def test_real_harness_sources_resolve(self) -> None:
        """真实仓库的集成测试脚手架：两个镜像常量能解析（Go 源码结构变了这里先红，而不是等到 plan）。"""

        for name, prefix in (("postgres", "postgres:"), ("redis", "redis:")):
            reference, source, resolved_from = eg._declared_image_default(REPO_ROOT, name, {}, None)
            self.assertTrue(reference.startswith(prefix), reference)
            self.assertEqual((source["file"], resolved_from), (eg.DECLARED_IMAGE_SOURCE_FILE, "source"))
        reference, _source, _resolved_from = eg._declared_image_default(REPO_ROOT, "ryuk", {}, None)
        self.assertTrue(reference.startswith("testcontainers/ryuk:"), reference)

    def test_postgres_override_is_honored_with_the_harness_semantics(self) -> None:
        scope = default_scope(contracts=("service-image:postgres", "service-image:redis"))
        plain = {entry["name"]: entry for entry in self.entries(scope)}
        override = "postgres:15-alpine"
        self.tc.runner.responses[("docker", "image", "inspect", "--format", "{{.Id}}", override)] = (0, "sha256:" + "5" * 64 + "\n")
        environ = {**self.tc.environ, "SUB2API_TEST_POSTGRES_IMAGE": f"  {override}  "}
        overridden = {entry["name"]: entry for entry in self.entries(scope, environ=environ)}
        postgres = overridden["contract:service-image:postgres"]["detail"]
        self.assertEqual((postgres["reference"], postgres["default_reference"], postgres["override_value"]), (override, "postgres:18.1-alpine3.23", override))
        self.assertEqual(postgres["image_id"], "sha256:" + "5" * 64)
        self.assertNotEqual(overridden["contract:service-image:postgres"]["sha256"], plain["contract:service-image:postgres"]["sha256"])
        self.assertEqual(overridden["contract:service-image:redis"], plain["contract:service-image:redis"], "redis 固定，不受覆盖变量影响")
        self.assertEqual(plain["contract:service-image:postgres"]["detail"]["override_value"], None)
        # 覆盖指向的镜像本地没有：image_id 记 unavailable，该次不得承接。
        absent = {entry["name"]: entry for entry in self.entries(scope, environ={**self.tc.environ, "SUB2API_TEST_POSTGRES_IMAGE": "postgres:99"})}
        self.assertTrue(absent["contract:service-image:postgres"]["detail"]["image_id"].startswith("unavailable("))

    def test_a_testcontainers_hub_prefix_makes_the_image_entries_fail_closed(self) -> None:
        scope = default_scope(contracts=("service-image:postgres", "service-image:ryuk"))
        properties = self.tc.home / ".testcontainers.properties"
        for text, configured in (("hub.image.name.prefix=mirror.example.com/cache\n", True), ("hub.image.name.prefix: mirror.example.com\n", True),
                                 ("hub.image.name.prefix mirror.example.com\n", True), ("hub.image.name.prefix=\nryuk.disabled=false\n", False),
                                 ("# hub.image.name.prefix=mirror.example.com\n", False), ("ryuk.disabled=false\n", False)):
            with self.subTest(text=text):
                properties.write_text(text, encoding="utf-8")
                for entry in self.entries(scope):
                    self.assertIs(entry["detail"]["hub_prefix_configured"], configured)
                    self.assertEqual(entry["detail"]["image_id"].startswith("unavailable("), configured)
                    self.assertEqual(bool(eg.declared_contract_problems([entry])), configured)
        properties.unlink()
        environ = {**self.tc.environ, eg.DECLARED_HUB_PREFIX_ENV: "mirror.example.com"}
        self.assertTrue(all(entry["detail"]["hub_prefix_configured"] for entry in self.entries(scope, environ=environ)))

    # ---- 会话与条目形状 ----

    def test_each_fact_is_probed_once_per_session_and_every_session_probes_again(self) -> None:
        scope_a = default_scope(contracts=("toolchain:go", "toolchain:node", "service-image:postgres", "cache:go-build-cache"))
        scope_b = default_scope(contracts=("toolchain:go", "service-image:postgres", "service-image:redis"))
        runner = self.tc.runner
        session = self.tc.probe()
        hashed: list[str] = []
        real_sha256 = eg._declared_file_sha256
        with mock.patch.object(eg, "_declared_file_sha256", lambda path: hashed.append(str(path)) or real_sha256(path)):
            first, second = session(declaration(scope_a)), session(declaration(scope_b))
        self.assertEqual(hashed.count(str(Path(os.path.realpath(self.tc.bin / "go")))), 1, "可执行文件整个算摘要很贵：同一会话只算一次")
        self.assertEqual((runner.count("go", "version"), runner.count("node", "--version")), (1, 1), "两个单元共用的工具链条目只探测一次")
        self.assertEqual(runner.count("docker", "image", "inspect", "--format", "{{.Id}}", "postgres:18.1-alpine3.23"), 1)
        shared = "contract:toolchain:go"
        self.assertEqual(entry_digests(first)[shared], entry_digests(second)[shared])
        first[0]["detail"]["tampered"] = True
        self.assertNotIn("tampered", session(declaration(scope_a))[0]["detail"], "会话返回副本：调用方改它不影响缓存")
        self.tc.probe()(declaration(scope_a))
        self.assertEqual(runner.count("go", "version"), 2, "新会话＝重新探测（锁后、发布前复核必须看到此刻的真实事实）")
        self.tc.versions["go"] = "go version go1.99 linux/arm64"
        go_entry = next(entry for entry in session(declaration(scope_a)) if entry["name"] == shared)
        self.assertEqual(go_entry["detail"]["version_line"], "go version go1.27.0 linux/arm64", "同一会话沿用本会话已探测的值")
        go_entry = next(entry for entry in self.tc.probe()(declaration(scope_a)) if entry["name"] == shared)
        self.assertEqual(go_entry["detail"]["version_line"], "go version go1.99 linux/arm64")

    def test_one_shot_function_and_the_probe_callable_agree(self) -> None:
        scope = default_scope(contracts=("toolchain:go", "cache:go-mod-cache"), make_expansion=("test-frontend-critical",))
        self.tc.runner.responses[("make", "-s", "--no-print-directory", "-n", "test-frontend-critical")] = (0, "echo hi\n")
        direct = eg.declared_contract_entries(declaration(scope), runner=self.tc.runner, environ=self.tc.environ, tree=self.tree)
        self.assertEqual(direct, self.tc.probe()(declaration(scope)))
        self.assertEqual(direct, self.tc.probe().entries(scope), "也接受直接给 scope")
        with self.assertRaisesRegex(ValueError, "declared_scope"):
            self.tc.probe().entries({"inputs": 1})

    def test_contract_entry_shape(self) -> None:
        scope = default_scope(contracts=("toolchain:go", "cache:tmp", "service-image:redis", "host-config:npmrc"), make_expansion=("test-frontend-critical",),
                              git_commits=("a" * 40,))
        self.tc.runner.responses[("make", "-s", "--no-print-directory", "-n", "test-frontend-critical")] = (0, "echo hi\n")
        self.tc.runner.responses[("git", "rev-parse", "--verify", "a" * 40 + "^{tree}")] = (0, "b" * 40 + "\n")
        entries = self.entries(scope)
        self.assertEqual([entry["name"] for entry in entries], sorted(entry["name"] for entry in entries))
        for entry in entries:
            self.assertEqual(set(entry), {"category", "name", "sha256", "detail"})
            self.assertEqual(entry["category"], "environment")
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            detail = entry["detail"]
            self.assertEqual(detail["schema_version"], eg.DECLARED_CONTRACT_SCHEMA)
            self.assertEqual(entry["name"], f"contract:{detail['type']}:{detail['id']}")
            self.assertLessEqual(len(str(detail["value"])), 300)
            json.dumps(entry)
        self.assertEqual({entry["detail"]["type"] for entry in entries}, {"toolchain", "cache", "service-image", "host-config", "make-expansion", "git-history"})

        def facts_digest(entry: dict) -> str:
            skipped = ("schema_version", "type", "id", *eg.DECLARED_INFO_KEYS)
            return ur.sha256_json({"schema_version": eg.DECLARED_CONTRACT_SCHEMA, "type": entry["detail"]["type"], "id": entry["detail"]["id"],
                                   "facts": {key: value for key, value in entry["detail"].items() if key not in skipped}})

        self.assertTrue(all(entry["sha256"] == facts_digest(entry) for entry in entries), "摘要覆盖全部事实、不含只供人读的字段")
        self.assertEqual(len(ur._unique(entries)), len(entries), "条目名唯一，能并进单元的输入明细")

    def test_unknown_contracts_are_programming_errors(self) -> None:
        for ident in ("toolchain:rust", "cache:none", "service-image:mysql", "host-config:ssh", "other:thing", "plain"):
            with self.subTest(ident), self.assertRaisesRegex(ValueError, "未知"):
                self.entries(default_scope(contracts=(ident,)))

    def test_cache_requirement_for_go_test_arguments(self) -> None:
        entries = self.entries(default_scope(contracts=("cache:go-build-cache", "cache:go-mod-cache")))
        self.assertEqual(eg.declared_contract_problems(entries, argv=["go", "test", "./...", "-count=1"]), [])
        self.assertEqual(len(eg.declared_contract_problems(entries, argv=["go", "test", "-tags=unit", "./..."])), 1, "缺 -count=1：测试结果缓存会成为隐藏输入")
        self.assertEqual(eg.declared_contract_problems(entries, argv=["make", "--no-print-directory", "egress-spec-go-test"]), [], "不是 go test 命令不适用")
        self.assertEqual(eg.declared_contract_problems(entries), [])
        # argv 带不带启动前缀都行：在其中找相邻的 go test。
        self.assertEqual(eg.declared_contract_problems(entries, argv=[*LAUNCHER, "go", "test", "./...", "-count=1"]), [])
        self.assertEqual(len(eg.declared_contract_problems(entries, argv=[*LAUNCHER, "go", "test", "./..."])), 1)
        self.assertEqual(eg.declared_contract_problems(entries, argv=[*LAUNCHER, "go", "run", "./cmd/x", "test"]), [], "go run … test 不是 go test")

    def test_an_empty_go_env_answer_is_treated_as_unavailable(self) -> None:
        """go env 退出 0 却没给 GOROOT／GOTOOLDIR（输出残缺）：不能拿空路径去算摘要，失败关闭。"""

        keys = ("GOROOT", "GOTOOLDIR", *eg.DECLARED_GO_ENV_KEYS)
        for payload in ({}, {"GOROOT": "", "GOTOOLDIR": ""}, {"GOROOT": "/x", "GOTOOLDIR": ""}, [1]):
            with self.subTest(payload=payload):
                runner = FakeRunner({**self.tc.responses(), ("go", "env", "-json", *keys): (0, json.dumps(payload))})
                entry = self.entries(default_scope(contracts=("toolchain:go",)), runner=runner)[0]
                self.assertEqual((entry["detail"]["goroot"], entry["detail"]["env"]), ("unavailable(rc=0)", "unavailable(rc=0)"))
                self.assertTrue(eg.declared_contract_problems([entry]))


class ControlInvariantTests(unittest.TestCase):
    """T-C10：登记了声明级承接的清单上，control_read_audit_for_plan 仍只认 control 四链。"""

    CONTROL_IDS = ("pre-a3:claude:vc-chain.claude-reconcile", "pre-a3:claude:vc-chain.claude-deadline",
                   "pre-a3:claude:vc-chain.claude-root-cause", "pre-a3:claude:vc-chain.claude-tool-evolution")

    def setUp(self) -> None:
        install_wp1_stubs(self)
        self.world = World(self)

    def control_manifest(self) -> dict:
        """真实的 control 规划：九个 Claude 场景命令＋四链的 audit_policy（与 test_ci_read_audit_runtime 同一做法），
        再并入完整门禁的命令单元，模拟 entry 组合的清单。"""

        from tools.ci import control_read_audit as control_audit
        from tools.ci import control_read_audit_runtime as control_runtime
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        raw = certification.plan_scenario_commands(base / "original" / "staging", client="claude")
        path = base / "nine-plan.json"
        path.write_text(json.dumps({"schema_version": "unit-executor-gates/v1", "units": raw["units"], "test_groups": [], "gates": []}), encoding="utf-8")
        selected = ["pre-a3:claude:" + scenario for scenario in sorted(control_runtime.CONTROL_SCENARIOS)]
        self.assertEqual(set(selected), set(self.CONTROL_IDS))
        prepared = control_audit.prepare_audit_policy(path, selected, str(base / "private" / "output"))
        manifest = copy.deepcopy(prepared["plan"])
        manifest["units"] = manifest["units"] + copy.deepcopy(self.world.plan()["units"])
        manifest["profile"] = "entry"
        return manifest

    def test_t_c10_the_adapter_selection_and_the_control_units_are_untouched_by_declared_registrations(self) -> None:
        manifest = self.control_manifest()
        control_before = {unit["unit_id"]: copy.deepcopy(unit) for unit in manifest["units"] if unit["unit_id"] in self.CONTROL_IDS}
        policy_before = copy.deepcopy(manifest["audit_policy"])
        self.assertEqual(set(control_before), set(self.CONTROL_IDS))
        adapter_before = eg.control_read_audit_for_plan(manifest)
        self.assertIsNotNone(adapter_before)
        self.world.declare("backend:go-test")
        self.world.declare("backend:unit", default_scope(contracts=("toolchain:go", "cache:go-build-cache")))
        self.world.rerun_only("capture:prerequisites")
        self.world.write()
        eg.apply_declared_inputs(manifest, self.world.tree, launcher=LAUNCHER)
        adapter = eg.control_read_audit_for_plan(manifest)
        self.assertIsNotNone(adapter)
        self.assertIs(adapter, adapter_before, "同一个固定 adapter 模块")
        self.assertTrue(adapter.selected_plan(manifest))
        self.assertEqual({unit["unit_id"]: unit for unit in manifest["units"] if unit["unit_id"] in self.CONTROL_IDS}, control_before,
                         "四链单元（含 inputs）逐字不变")
        self.assertEqual(manifest["audit_policy"], policy_before)
        self.assertEqual(set(manifest["audit_policy"]["units"]) & adapter.CONTROL_UNIT_IDS, set(self.CONTROL_IDS))
        self.assertEqual(set(inputs_of(manifest)["backend:go-test"]["inputs"]), {"declared_scope", "declared_sample"})
        self.assertIn("declared_inputs", manifest, "声明级汇总是独立顶层键")

    def test_t_c10_control_units_cannot_be_registered_in_either_status_and_a_plain_manifest_gets_no_adapter(self) -> None:
        manifest = self.control_manifest()
        for unit_id in self.CONTROL_IDS:
            for entry in ({"status": "rerun-only", "reason": "x"}, {"status": "declared", "scope": {}, "sample": {}}):
                with self.subTest(unit_id=unit_id, status=entry["status"]):
                    self.world.registry["units"] = {unit_id: entry}
                    self.world.write()
                    with self.assertRaisesRegex(ValueError, "pre-A3"):
                        eg.apply_declared_inputs(copy.deepcopy(manifest), self.world.tree, launcher=LAUNCHER)
        self.world.registry["units"] = {}
        self.world.declare("backend:go-test")
        self.world.write()
        plain = self.world.plan()
        self.assertIsNone(eg.control_read_audit_for_plan(plain), "没有 audit_policy／四链的清单：仍不取得 control adapter")
        self.assertIn("declared_inputs", plain)


@unittest.skipIf(ds is None, "WP4 的 declared_sampler 尚未合入：无采样器可联调")
class Wp4InteroperabilityTests(unittest.TestCase):
    """WP4（declared_sampler）真实产出的证据与登记表草案，直接喂给本模块的自检与探测：摘要约定、证据形状、合同编号都要对得上。
    覆盖检查仍用桩（真实的是 WP1 的），所以这里验证的是格式与约定，不是覆盖规则。"""

    def setUp(self) -> None:
        install_wp1_stubs(self)
        self.world = World(self)
        self.tree = self.world.tree
        scaffold._git(self.tree, "init", "-q")
        scaffold._commit(self.tree, "初始")
        self.work = self.world.root / "work"
        self.work.mkdir()
        self.home = str(self.world.root / "home")

    def sample(self, unit_id: str = "backend:go-test", *, exit_code: int = 0) -> dict:
        """用采样器自己的 filter／merge 把一份合成的 go test 读集（工具链、go-build 缓存、HOME 里的 go env 探测、系统文件、仓库内
        两个跟踪文件）变成 declared-sample/v1，写进测试树的证据目录。命令取自计划，启动前缀取计划的启动前缀。"""

        cwd = f"{self.tree}/backend"

        def opened(path: str) -> str:
            return f'4242 openat(AT_FDCWD<{cwd}>, "{path}", O_RDONLY|O_CLOEXEC) = 3<{path}>'

        lines = [opened(f"{self.tree}/backend/go.mod"), opened(f"{self.tree}/backend/go.sum"), opened("/usr/local/go/src/fmt/print.go"),
                 opened(f"{self.work}/go-build-cache/ab/abcd-d"), opened("/etc/passwd"),
                 f'4242 openat(AT_FDCWD<{cwd}>, "{self.home}/.config/go/env", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)']
        aliases = {"TREE": str(self.tree), "WORK": str(self.work), "HOME": self.home, "TMP": str(self.world.root / "tmp")}
        raw = ds.filter_stream((line + "\n" for line in lines), aliases, unit_id=unit_id)
        command, cwd_rel = self.world.command_of(unit_id)
        recorded = {"launcher": list(self.world.launcher), "argv": command, "cwd_rel": cwd_rel}
        recorded["sha256"] = ds.command_digest(recorded["argv"], recorded["cwd_rel"])
        raw["run"] = {"label": "cold", "cache": "cold", "exit_code": exit_code, "signal": None, "timed_out": False, "seconds": 1.5,
                      "started_utc": "2026-10-14T00:00:00Z", "finished_utc": "2026-10-14T00:00:02Z", "command": recorded,
                      "git_status_before": [], "git_status_after": [], "commit": scaffold._git(self.tree, "rev-parse", "HEAD").strip(),
                      "kernel": "6.8.0", "strace": "6.8", "sampler_sha256": SHA_B, "env_overrides": {}, "docker": None,
                      "filter_wait_seconds": 0.0}
        sample = ds.merge_raws([raw], tree=self.tree)
        ds.write_document(self.tree / eg.DECLARED_SAMPLES_DIR / f"{ds.safe_name(unit_id)}.sample.json", sample)
        return sample

    def draft(self) -> dict:
        registry, _review = ds.draft_registry(self.tree / eg.DECLARED_SAMPLES_DIR, self.tree)
        self.world.registry = registry
        self.world.write()
        return registry

    def test_a_registry_drafted_by_the_sampler_passes_the_plan_time_checks_and_its_contracts_are_probed(self) -> None:
        sample = self.sample()
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        registry = self.draft()
        entry = registry["units"]["backend:go-test"]
        self.assertEqual(entry["status"], "declared")
        self.assertEqual(set(entry["scope"]["contracts"]), {"toolchain:go", "cache:go-build-cache", "host-config:config-go-env"},
                         "采样器按路径派生的 HOME 合同编号（{HOME}/.config/go/env → host-config:config-go-env）")
        manifest = self.world.plan()
        self.assertEqual(manifest["declared_inputs"]["declared_units"], ["backend:go-test"])
        unit = inputs_of(manifest)["backend:go-test"]
        # 摘要约定：两边用同一套算法。
        command, _cwd_rel = self.world.command_of("backend:go-test")
        self.assertEqual(unit["inputs"]["declared_sample"]["command_sha256"], eg.declared_command_sha256(command, self.tree / "backend", self.tree))
        self.assertEqual(entry["sample"]["launcher_sha256"], eg.declared_launcher_sha256(LAUNCHER))
        self.assertEqual(entry["sample"]["evidence_sha256"], hashlib.sha256((self.tree / entry["sample"]["evidence"]).read_bytes()).hexdigest())
        self.assertEqual(unit["inputs"]["declared_sample"]["sampler_sha256"], SHA_B)
        # 探测：路径取自 scope.outside 的 home-config 行。
        (self.world.root / "tc").mkdir()
        entries = Toolchain(self.world.root / "tc", self.tree).probe().entries(unit["inputs"])
        self.assertEqual([item["name"] for item in entries],
                         ["contract:cache:go-build-cache", "contract:host-config:config-go-env", "contract:toolchain:go"])
        self.assertEqual(entries[1]["detail"]["files"], [{"alias": "{HOME}/.config/go/env", "state": "missing"}])

    def test_a_sample_the_sampler_judged_invalid_becomes_rerun_only_and_cannot_back_a_declared_entry(self) -> None:
        sample = self.sample(exit_code=1)
        self.assertFalse(sample["valid"])
        registry = self.draft()
        self.assertEqual(registry["units"]["backend:go-test"]["status"], "rerun-only")
        self.assertIn("采样无效", registry["units"]["backend:go-test"]["reason"])
        self.assertEqual(inputs_of(self.world.plan())["backend:go-test"]["inheritable"], False)
        # 把无效的证据硬塞进 declared 登记项（其余摘要都对得上）：自检按证据里的 valid 标记拒绝。
        relative = f"{eg.DECLARED_SAMPLES_DIR}/backend-go-test.sample.json"
        command, cwd_rel = self.world.command_of("backend:go-test")
        self.world.registry["units"]["backend:go-test"] = {
            "status": "declared", "scope": default_scope(),
            "sample": {"evidence": relative, "evidence_sha256": hashlib.sha256((self.tree / relative).read_bytes()).hexdigest(),
                       "command_sha256": ds.command_digest(command, cwd_rel), "launcher_sha256": eg.declared_launcher_sha256(LAUNCHER),
                       "sampler_sha256": SHA_B}}
        self.world.write()
        with self.assertRaisesRegex(ValueError, "采样无效"):
            self.world.plan()


@unittest.skipUnless(all(hasattr(ur, name) for name in ("validate_declared_scope", "declared_coverage")), "WP1 尚未合入 unit_records：无真实函数可联调")
class RealWp1IntegrationTests(unittest.TestCase):
    """WP1 合入后联调：不装桩，用 unit_records 的真实 validate_declared_scope／declared_coverage 走一遍 plan。"""

    def test_frozen_names_agree_with_unit_records(self) -> None:
        for name in ("DECLARED_REGISTRY_SCHEMA", "DECLARED_SCOPE_SCHEMA", "DECLARED_SAMPLE_SCHEMA", "DECLARED_CONTRACT_SCHEMA"):
            if hasattr(ur, name):
                self.assertEqual(getattr(ur, name), getattr(eg, name), name)

    def test_plan_with_the_real_validators(self) -> None:
        world = World(self)
        world.declare("backend:go-test")
        world.rerun_only("capture:prerequisites")
        world.write()
        manifest = world.plan()
        self.assertEqual(manifest["declared_inputs"]["declared_units"], ["backend:go-test"])


if __name__ == "__main__":
    unittest.main()
