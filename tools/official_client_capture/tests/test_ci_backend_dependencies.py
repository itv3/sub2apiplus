"""后端三个 Go 单元的外部依赖登记（优化第 8 项的剩余部分，``tools/ci/backend_dependencies.py`` 与 ``entry_gates`` 的镜像相关新增）。

* 依赖清单的结构：每项依赖的状态、涉及的单元；每个单元登记为 declared 时要求的合同（integration 带 Docker 与三个镜像，另两个不带）；
* 登记表里 rerun-only 的原因由清单生成，登记表文字与清单逐字一致，一边变了另一边必须跟着变；
* 命令识别：裸 ``go test -tags=…`` 与按包运行的包装器两种写法；计划里三个单元的命令各是该有的标签；
* 失败关闭：镜像摘要拿不到（缺镜像、摘要形状不对、Docker 版本取不到、Hub 前缀、引用解析不了、redis 引用在三份源码里不一致、
  用了合同表里没有的 testcontainers 模块）一律不通过，命令行退出码 3；
* 真实仓库自检：三份 redis 引用一致，登记表与清单一致。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools.ci import backend_dependencies as bd
from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_entry as base
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_VERSION = ("docker", "version", "--format", "client={{.Client.Version}} server={{.Server.Version}}")
MIDDLEWARE_TEST = "backend/internal/middleware/rate_limiter_integration_test.go"
ROUTES_TEST = "backend/internal/server/routes/auth_rate_limit_integration_test.go"


def peer_source(constant: str, image: str, module: str = "redis") -> str:
    return (f'//go:build integration\n\npackage peer\n\nimport (\n\ttc "github.com/testcontainers/testcontainers-go/modules/{module}"\n)\n\n'
            f'const {constant} = "{image}"\n')


class InventoryStructureTests(unittest.TestCase):
    def test_every_dependency_row_is_well_formed(self) -> None:
        ids = [item["id"] for item in bd.DEPENDENCIES]
        self.assertEqual(len(ids), len(set(ids)), "依赖 ID 唯一")
        for item in bd.DEPENDENCIES:
            with self.subTest(item["id"]):
                self.assertIn(item["state"], bd.STATES)
                self.assertTrue(set(item["units"]) <= set(bd.ALL_UNITS))
                self.assertTrue(all(str(item[key]).strip() for key in ("title", "how", "remaining")))

    def test_the_cleared_items_cover_all_three_units_and_docker_items_only_the_integration_unit(self) -> None:
        by_id = {item["id"]: item for item in bd.DEPENDENCIES}
        for dependency in ("dns", "external-https"):
            self.assertEqual((by_id[dependency]["state"], tuple(by_id[dependency]["units"])), (bd.STATE_CLEARED, bd.ALL_UNITS))
        for dependency in ("docker-engine", "image-postgres", "image-redis", "image-ryuk"):
            self.assertEqual((by_id[dependency]["state"], tuple(by_id[dependency]["units"])), (bd.STATE_CONTRACT_READY, ("backend:integration",)))
        self.assertEqual(by_id["sampling-evidence"]["state"], bd.STATE_NOT_INHERITABLE, "没有采样证据就不能登记 declared")
        self.assertEqual(by_id["docker-missing-skips"]["state"], bd.STATE_CLEARED, "门禁里的 integration 单元固定带 CI=true，缺 Docker 直接失败")
        self.assertIn("CI=true", by_id["docker-missing-skips"]["how"])

    def test_required_contracts_per_unit(self) -> None:
        for unit_id in ("backend:go-test", "backend:unit"):
            self.assertEqual(bd.contracts_for(unit_id), sorted(bd.BASE_CONTRACTS))
            self.assertEqual(bd.unix_sockets_for(unit_id), [])
        integration = bd.contracts_for("backend:integration")
        self.assertEqual(integration, sorted({*bd.BASE_CONTRACTS, "toolchain:docker", "service-image:postgres", "service-image:redis", "service-image:ryuk"}))
        self.assertEqual(bd.unix_sockets_for("backend:integration"), ["/var/run/docker.sock"])
        for unit_id in bd.ALL_UNITS:
            for contract in bd.contracts_for(unit_id):
                self.assertTrue(eg._declared_known_contract(contract), f"{contract} 必须是评审过的合同")
        with self.assertRaises(ValueError):
            bd.contracts_for("backend:backend-lint")

    def test_the_reason_text_lists_every_dependency_of_the_unit_with_its_state(self) -> None:
        for unit_id in bd.ALL_UNITS:
            reason = bd.reason_for(unit_id)
            for item in bd.dependencies_for(unit_id):
                self.assertIn(item["title"], reason)
                self.assertIn(bd.STATE_LABELS[item["state"]], reason)
            self.assertIn(str(bd.UNITS[unit_id]["median_seconds"]), reason, "沿用原来的历史中位耗时")
        self.assertNotIn("postgres", bd.reason_for("backend:unit"), "不需要 Docker 的单元原因里不出现镜像")
        self.assertIn("redis:8.4-alpine", bd.reason_for("backend:integration"))
        self.assertIn("testcontainers/ryuk:0.13.0", bd.reason_for("backend:integration"))


class CommandRecognitionTests(unittest.TestCase):
    WRAPPER = "/root/driver/go_test_packages.py"

    def test_bare_go_test_and_the_wrapper_are_both_recognised(self) -> None:
        cases = {
            "裸 -tags=": (["go", "test", "-tags=unit", "./...", "-count=1"], "unit"),
            "裸 -tags 分开写": (["go", "test", "-tags", "integration", "./...", "-count=1"], "integration"),
            "裸 无标签": (["go", "test", "./...", "-count=1"], ""),
            "包装器 --tags": (["python3", "-B", self.WRAPPER, "--tags", "unit", "--pattern", "./..."], "unit"),
            "包装器 --tags=": (["python3", "-B", self.WRAPPER, "--tags=integration", "--pattern", "./..."], "integration"),
            "包装器 无标签": (["python3", "-B", self.WRAPPER, "--pattern", "./..."], ""),
            "包装器带启动前缀": (["unshare", "-m", "bash", "-c", "exec \"$@\"", "entry-gates", "python3", "-B", self.WRAPPER, "--tags", "unit"], "unit"),
        }
        for label, (argv, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(bd.command_tags(argv), expected)

    def test_other_commands_and_forwarded_arguments_are_not_mistaken(self) -> None:
        self.assertIsNone(bd.command_tags(["golangci-lint", "run", "--build-tags=unit"]))
        self.assertIsNone(bd.command_tags(["python3", "-B", "/root/driver/other.py", "--tags", "unit"]), "名字不是包装器的脚本不算")
        self.assertIsNone(bd.command_tags(["go", "run", "./cmd/x", "test"]))
        # 双横线之后是转给 go test 的附加参数，不是包装器自己的标签。
        self.assertEqual(bd.command_tags(["python3", "-B", self.WRAPPER, "--tags", "unit", "--", "-tags=integration"]), "unit")

    def test_the_real_gate_commands_in_both_forms_carry_each_units_tag(self) -> None:
        for unit_id, info in bd.UNITS.items():
            literal = eg.GATE_COMMANDS[info["gate"]][0]
            wrapped = eg.go_package_argv(literal)
            self.assertEqual(bd.command_tags(literal), info["tags"], unit_id)
            self.assertEqual(bd.command_tags(wrapped), info["tags"], unit_id)
            self.assertEqual(bd.command_tags([*base.LAUNCHER, *wrapped]), info["tags"], unit_id)

    def test_planned_units_are_checked_against_their_convention(self) -> None:
        world = base.World(self)
        self.assertEqual(sorted(unit for unit in world.units if unit in bd.UNITS), sorted(bd.ALL_UNITS), "计划里有这三个单元")
        self.assertEqual(bd.plan_problems(list(world.units.values())), [])
        bad = [{**world.units["backend:unit"], "argv": ["python3", "-B", self.WRAPPER, "--tags", "integration"]},
               {**world.units["backend:go-test"], "argv": ["make", "test-backend"]},
               {**world.units["backend:integration"], "argv": ["python3", "-B", self.WRAPPER, "--pattern", "./..."]}]
        problems = bd.plan_problems(bad)
        self.assertEqual(len(problems), 3, problems)
        self.assertIn("构建标签", "".join(problems))
        self.assertIn("不是 go test", "".join(problems))


class RegistryTests(unittest.TestCase):
    def registry(self, **entries) -> dict:
        units = {unit: {"status": "rerun-only", "reason": bd.reason_for(unit)} for unit in bd.ALL_UNITS}
        units.update(entries)
        return {"schema_version": eg.DECLARED_REGISTRY_SCHEMA, "units": units}

    def declared(self, unit_id: str, **overrides) -> dict:
        scope = {"contracts": bd.contracts_for(unit_id), **({"unix_sockets": bd.unix_sockets_for(unit_id)} if bd.unix_sockets_for(unit_id) else {})}
        return {"status": "declared", "scope": {**scope, **overrides}, "sample": {}}

    def test_the_real_registry_matches_the_inventory(self) -> None:
        registry = json.loads((REPO_ROOT / eg.DECLARED_REGISTRY_PATH).read_text(encoding="utf-8"))
        self.assertEqual(bd.registry_problems(registry), [])
        for unit_id in bd.ALL_UNITS:
            self.assertEqual(registry["units"][unit_id]["status"], "rerun-only", "没有 ARM64 采样证据前不能登记 declared")
            self.assertEqual(registry["units"][unit_id]["reason"], bd.reason_for(unit_id))

    def test_a_stale_missing_or_misstated_entry_is_reported(self) -> None:
        stale = self.registry(**{"backend:unit": {"status": "rerun-only", "reason": "外部 DNS，不可承接"}})
        self.assertTrue(any("backend:unit" in problem and "原因与依赖清单不一致" in problem for problem in bd.registry_problems(stale)))
        missing = self.registry()
        del missing["units"]["backend:integration"]
        self.assertTrue(any("没有这个单元" in problem for problem in bd.registry_problems(missing)))
        self.assertTrue(bd.registry_problems(self.registry(**{"backend:unit": {"status": "maybe"}})))
        self.assertTrue(bd.registry_problems({"units": []}))

    def test_declared_entries_must_carry_the_required_contracts(self) -> None:
        self.assertEqual(bd.registry_problems(self.registry(**{"backend:integration": self.declared("backend:integration")})), [])
        self.assertEqual(bd.registry_problems(self.registry(**{"backend:unit": self.declared("backend:unit")})), [])
        lacking_images = self.declared("backend:integration", contracts=[*bd.BASE_CONTRACTS])
        problems = bd.registry_problems(self.registry(**{"backend:integration": lacking_images}))
        self.assertTrue(any("缺 ['service-image:postgres'" in problem or "service-image:postgres" in problem for problem in problems), problems)
        no_socket = self.declared("backend:integration", unix_sockets=[])
        self.assertTrue(any("unix_sockets" in problem for problem in bd.registry_problems(self.registry(**{"backend:integration": no_socket}))))

    def test_units_without_docker_may_not_declare_docker_dependencies(self) -> None:
        over = self.declared("backend:unit", contracts=[*bd.contracts_for("backend:unit"), "service-image:redis"])
        self.assertTrue(any("不需要 Docker" in problem for problem in bd.registry_problems(self.registry(**{"backend:unit": over}))))
        socket = self.declared("backend:go-test", unix_sockets=["/var/run/docker.sock"])
        self.assertTrue(any("不需要 Docker" in problem for problem in bd.registry_problems(self.registry(**{"backend:go-test": socket}))))

    def test_apply_to_draft_adds_what_the_sampler_cannot_derive_and_leaves_the_rest_alone(self) -> None:
        sampled = {"contracts": ["cache:go-build-cache", "toolchain:go"], "repo": {"files": []}}
        draft = self.registry(**{"backend:integration": {"status": "declared", "scope": copy_of(sampled), "sample": {"x": 1}},
                                 "backend:unit": {"status": "declared", "scope": copy_of(sampled), "sample": {"x": 2}},
                                 "frontend:lint": {"status": "declared", "scope": copy_of(sampled), "sample": {}}})
        merged = bd.apply_to_draft(draft)
        self.assertEqual(bd.registry_problems(merged), [])
        self.assertEqual(merged["units"]["backend:integration"]["scope"]["contracts"], bd.contracts_for("backend:integration"))
        self.assertEqual(merged["units"]["backend:integration"]["scope"]["unix_sockets"], ["/var/run/docker.sock"])
        self.assertNotIn("unix_sockets", merged["units"]["backend:unit"]["scope"])
        self.assertEqual(merged["units"]["frontend:lint"], draft["units"]["frontend:lint"], "别的单元不动")
        self.assertEqual(merged["units"]["backend:go-test"], draft["units"]["backend:go-test"], "rerun-only 不动")
        self.assertEqual(bd.apply_to_draft(merged), merged, "幂等")
        self.assertEqual(draft["units"]["backend:integration"]["scope"], sampled, "不改传入的草案")

    def test_the_planner_accepts_the_generated_reasons_and_marks_the_units_not_inheritable_with_them(self) -> None:
        world = base.World(self)
        for unit_id in bd.ALL_UNITS:
            world.rerun_only(unit_id, bd.reason_for(unit_id))
        world.write()
        manifest = world.plan()
        units = base.inputs_of(manifest)
        for unit_id in bd.ALL_UNITS:
            self.assertIs(units[unit_id]["inheritable"], False)
            self.assertEqual(units[unit_id]["not_inheritable_reason"], bd.reason_for(unit_id))
            self.assertEqual(manifest["declared_inputs"]["rerun_only_reasons"][unit_id], bd.reason_for(unit_id))


def copy_of(value: dict) -> dict:
    return json.loads(json.dumps(value))


class DigestFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.tree = base.make_tree(self.root)
        self.tc = base.Toolchain(self.root, self.tree)
        self.tc.runner.responses[DOCKER_VERSION] = lambda: (0, "client=27.3.1 server=27.3.1\n")

    def digests(self, **overrides):
        return bd.digests(self.tree, environ=overrides.pop("environ", self.tc.environ), runner=overrides.pop("runner", self.tc.runner))

    def inspect_key(self, image: str) -> tuple:
        return ("docker", "image", "inspect", "--format", "{{.Id}}", image)

    def test_everything_available_gives_sha256_ids_and_a_docker_version(self) -> None:
        report = self.digests()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual({name: item["reference"] for name, item in report["images"].items()},
                         {"postgres": "postgres:18.1-alpine3.23", "redis": "redis:8.4-alpine", "ryuk": "testcontainers/ryuk:0.13.0"})
        for item in report["images"].values():
            self.assertRegex(item["image_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(report["docker"]["version_line"], "client=27.3.1 server=27.3.1")
        self.assertRegex(report["docker"]["executable_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(sorted(report["contract_digests"]), ["contract:service-image:postgres", "contract:service-image:redis",
                                                              "contract:service-image:ryuk", "contract:toolchain:docker"])

    def test_a_missing_image_fails_closed_for_each_of_the_three_images(self) -> None:
        for image, name in (("postgres:18.1-alpine3.23", "postgres"), ("redis:8.4-alpine", "redis"), ("testcontainers/ryuk:0.13.0", "ryuk")):
            with self.subTest(image=image):
                runner = base.FakeRunner({key: value for key, value in self.tc.runner.responses.items() if key != self.inspect_key(image)})
                report = self.digests(runner=runner)
                self.assertFalse(report["ok"])
                self.assertFalse(report["images"][name]["ok"])
                self.assertTrue(any(name in problem for problem in report["problems"]), report["problems"])

    def test_a_digest_of_the_wrong_shape_is_not_accepted(self) -> None:
        for label, value in {"空": "", "标签而不是摘要": "latest", "过短": "sha256:abc", "大写": "sha256:" + "A" * 64, "多余内容": "sha256:" + "a" * 64 + "x"}.items():
            with self.subTest(label):
                self.tc.image_ids["redis:8.4-alpine"] = value
                runner = base.FakeRunner(self.tc.responses())
                runner.responses[DOCKER_VERSION] = lambda: (0, "client=27.3.1 server=27.3.1\n")
                report = self.digests(runner=runner)
                self.assertFalse(report["ok"], report)
                self.assertFalse(report["images"]["redis"]["ok"])

    def test_an_unreachable_docker_daemon_fails_closed(self) -> None:
        self.tc.runner.responses[DOCKER_VERSION] = lambda: (1, "Cannot connect to the Docker daemon")
        report = self.digests()
        self.assertFalse(report["ok"])
        self.assertTrue(any("Docker 版本" in problem for problem in report["problems"]), report["problems"])

    def test_no_docker_at_all_fails_closed(self) -> None:
        empty = self.root / "empty-path"
        empty.mkdir()
        report = self.digests(environ={"PATH": str(empty), "HOME": str(self.tc.home)}, runner=base.FakeRunner())
        self.assertFalse(report["ok"])
        self.assertTrue(all(not item["ok"] for item in report["images"].values()))
        self.assertGreaterEqual(len(report["problems"]), 4)

    def test_a_testcontainers_hub_prefix_fails_closed(self) -> None:
        report = self.digests(environ={**self.tc.environ, "TESTCONTAINERS_HUB_IMAGE_NAME_PREFIX": "mirror.example/"})
        self.assertFalse(report["ok"])
        self.assertIn("hub-prefix-configured", report["images"]["postgres"]["image_id"])

    def test_an_unresolvable_reference_fails_closed(self) -> None:
        (self.tree / "backend/internal/repository/integration_harness_test.go").write_text("package repository\n", encoding="utf-8")
        report = self.digests()
        self.assertFalse(report["ok"])
        self.assertIn("unresolved", report["images"]["redis"]["image_id"])

    def test_the_cli_exits_3_when_any_digest_is_missing_and_0_when_all_are_there(self) -> None:
        good = {"schema_version": bd.SCHEMA, "ok": True, "images": {}, "docker": {}, "problems": [], "contract_digests": {}}
        bad = {**good, "ok": False, "problems": ["镜像 redis 没有拿到 sha256 镜像 Id：'unavailable(rc=1)'"]}
        for report, code in ((good, 0), (bad, 3)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(bd, "digests", return_value=report), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(bd.main(["digests", "--tree", str(self.tree)]), code)
            self.assertEqual(json.loads(out.getvalue())["ok"], report["ok"])
            self.assertEqual("失败关闭" in err.getvalue(), code == 3)

    def test_declared_contract_problems_flags_ids_that_are_not_sha256_digests(self) -> None:
        scope = base.default_scope(contracts=("service-image:redis", "service-image:ryuk", "service-image:postgres"))
        entries = eg.declared_probe(runner=self.tc.runner, environ=self.tc.environ, tree=self.tree).entries(base.declaration(scope))
        self.assertEqual(eg.declared_contract_problems(entries), [])
        self.tc.image_ids["redis:8.4-alpine"] = "latest"
        entries = eg.declared_probe(runner=base.FakeRunner(self.tc.responses()), environ=self.tc.environ, tree=self.tree).entries(base.declaration(scope))
        problems = eg.declared_contract_problems(entries)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("镜像 Id 不是 sha256", problems[0])
        # unavailable(…) 的只报一次（遍历已经报了），不重复。
        failing = eg.declared_probe(runner=base.FakeRunner(), environ=self.tc.environ, tree=self.tree).entries(base.declaration(scope))
        self.assertFalse(any("镜像 Id 不是 sha256" in problem for problem in eg.declared_contract_problems(failing)))


class ImageSourceConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.tree = base.make_tree(self.root)

    def write(self, relative: str, text: str) -> None:
        path = self.tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_real_repository_agrees_on_every_image_reference(self) -> None:
        for ident, expected in (("postgres", "postgres:18.1-alpine3.23"), ("redis", "redis:8.4-alpine"), ("ryuk", "testcontainers/ryuk:0.13.0")):
            self.assertEqual(eg._declared_image_default(REPO_ROOT, ident, {}, None)[0], expected)

    def test_the_real_repository_has_exactly_the_three_known_testcontainers_sources(self) -> None:
        found = sorted(path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "backend").rglob("*_test.go")
                       if "testcontainers" in path.read_text(encoding="utf-8"))
        self.assertEqual(found, ["backend/internal/middleware/rate_limiter_integration_test.go", "backend/internal/repository/integration_harness_test.go",
                                 "backend/internal/server/routes/auth_rate_limit_integration_test.go"],
                         "多出来的 testcontainers 使用者要先确认镜像合同覆盖它")

    def test_peer_files_that_agree_pass(self) -> None:
        self.write(MIDDLEWARE_TEST, peer_source("redisImageTag", "redis:8.4-alpine"))
        self.write(ROUTES_TEST, peer_source("authRouteRedisImageTag", "redis:8.4-alpine"))
        self.assertEqual(eg._declared_image_default(self.tree, "redis", {}, None)[0], "redis:8.4-alpine")

    def test_a_peer_that_drifted_makes_the_reference_unresolvable(self) -> None:
        self.write(MIDDLEWARE_TEST, peer_source("redisImageTag", "redis:8.4-alpine"))
        self.write(ROUTES_TEST, peer_source("authRouteRedisImageTag", "redis:9.0-alpine"))
        with self.assertRaisesRegex(ValueError, "redis:9.0-alpine.*不一致"):
            eg._declared_image_default(self.tree, "redis", {}, None)
        # 合同层面：声明了 service-image:redis 的单元在登记自检时失败；postgres 也受牵连（同一份扫描里的 redis 字面量不影响 postgres 的判定）。
        with self.assertRaisesRegex(ValueError, "引用解析不了"):
            eg._declared_check_contracts("backend:integration", {"contracts": ["service-image:redis", "service-image:ryuk"]}, self.tree)
        self.assertEqual(eg._declared_image_default(self.tree, "postgres", {}, None)[0], "postgres:18.1-alpine3.23")

    def test_an_unreviewed_testcontainers_module_is_refused(self) -> None:
        self.write("backend/internal/other/mysql_integration_test.go", peer_source("mysqlImageTag", "mysql:8.4", module="mysql"))
        for ident in ("postgres", "redis"):
            with self.subTest(ident), self.assertRaisesRegex(ValueError, "testcontainers 模块 mysql"):
                eg._declared_image_default(self.tree, ident, {}, None)

    def test_files_that_do_not_use_testcontainers_are_ignored(self) -> None:
        self.write("backend/internal/config/config_test.go", 'package config\n\nfunc f() { _ = "redis:6379"; _ = "redis:7.0-alpine" }\n')
        self.assertEqual(eg._declared_image_default(self.tree, "redis", {}, None)[0], "redis:8.4-alpine")

    def test_the_probe_reports_the_drift_as_unavailable_instead_of_binding_the_wrong_image(self) -> None:
        self.write(ROUTES_TEST, peer_source("authRouteRedisImageTag", "redis:9.0-alpine"))
        tc = base.Toolchain(self.root, self.tree)
        entry = tc.probe().entries(base.declaration(base.default_scope(contracts=("service-image:redis", "service-image:ryuk"))))[0]
        self.assertEqual(entry["detail"]["id"], "redis")
        self.assertIn("unavailable", entry["detail"]["image_id"])
        self.assertIn("不一致", entry["detail"]["error"])


HARNESS_GO = "backend/internal/repository/integration_harness_test.go"
WRAPPER = REPO_ROOT / "tools" / "ci" / "go_test_packages.py"
HAVE_GO = shutil.which("go") is not None


def mirrored_harness() -> str:
    """按真实脚手架里的两段原文拼一个最小的 Go 测试包：``dockerIsAvailable`` 函数，和 TestMain 里判断 Docker 可用性、按 CI 变量决定“失败还是整包跳过”的那一段。
    不复制逻辑，只摘原文；摘不到（脚手架改了写法）就让测试失败，提醒这里的模拟要重新对照。"""

    text = (REPO_ROOT / HARNESS_GO).read_text(encoding="utf-8")
    function = re.search(r"func dockerIsAvailable\(ctx context\.Context\) bool \{\n.*?\n\}\n", text, re.S)
    block = re.search(r"\tif !dockerIsAvailable\(ctx\) \{\n.*?\n\t\}\n", text, re.S)
    if function is None or block is None:
        raise AssertionError("integration_harness_test.go 里的 Docker 可用性判断写法变了，这里的模拟要重新对照")
    return ("//go:build integration\n\npackage harness\n\nimport (\n\t\"context\"\n\t\"log\"\n\t\"os\"\n\t\"os/exec\"\n\t\"testing\"\n)\n\n"
            "func TestMain(m *testing.M) {\n\tctx := context.Background()\n" + block.group(0) + "\tos.Exit(m.Run())\n}\n\n" + function.group(0)
            + "\nfunc TestNeedsDocker(t *testing.T) {}\n")


class IntegrationUnitCiEnvTests(unittest.TestCase):
    """门禁里 integration 单元的命令环境固定带 ``CI=true``（缺 Docker 时脚手架 TestMain 退出 1，而不是整包跳过并判通过）。
    这个变量走单元规格、环境指纹的现有规则，没有被当成可忽略的变量。"""

    def setUp(self) -> None:
        self.world = base.World(self)
        self.unit = self.world.units["backend:integration"]

    def test_only_the_integration_unit_carries_ci_true_in_its_command_environment(self) -> None:
        self.assertEqual(self.unit["env"].get("CI"), "true")
        self.assertEqual(self.unit["env"].get("GOMAXPROCS"), eg.GO_ENV["GOMAXPROCS"], "原有的 Go 内部并行度不变")
        for other in ("backend:go-test", "backend:unit"):
            self.assertNotIn("CI", self.world.units[other]["env"], other)
        self.assertEqual(bd.plan_problems(list(self.world.units.values())), [])

    def test_the_plan_check_reports_a_missing_or_stray_ci(self) -> None:
        units = {key: json.loads(json.dumps(value)) for key, value in self.world.units.items()}
        del units["backend:integration"]["env"]["CI"]
        units["backend:unit"]["env"]["CI"] = "true"
        problems = bd.plan_problems(list(units.values()))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("backend:integration" in problem and "CI=true" in problem for problem in problems))
        self.assertTrue(any("backend:unit" in problem and "不应带 CI" in problem for problem in problems))
        units["backend:integration"]["env"]["CI"] = "1"
        self.assertTrue(bd.plan_problems([units["backend:integration"]]), "只认 CI=true")

    def command_unit(self, env: dict) -> ue.Unit:
        return ue.Unit(unit_id="backend:integration", module="", test_ids=(), quota=ue.Quota(cores=0.7, memory_mb=5120), exclusive=False, weight=1.0,
                       command=tuple(self.unit["argv"]), cwd=self.unit["cwd"], env=tuple(sorted(env.items())))

    def test_ci_is_part_of_the_unit_spec_so_changing_it_makes_a_different_unit(self) -> None:
        with_ci = ue.unit_spec(self.command_unit(self.unit["env"]), start=None, pattern=None, timeout_seconds=3600)
        without = ue.unit_spec(self.command_unit({key: value for key, value in self.unit["env"].items() if key != "CI"}),
                               start=None, pattern=None, timeout_seconds=3600)
        self.assertEqual(with_ci["env"]["CI"], "true")
        self.assertNotIn("CI", without["env"])
        self.assertNotEqual(ur.sha256_json(with_ci), ur.sha256_json(without), "规格摘要不同：没带 CI=true 的旧记录不能被当成同一个单元承接")

    def test_ci_is_not_one_of_the_ignorable_variables(self) -> None:
        self.assertNotIn("CI", ur.ENV_CACHE_ONLY)
        self.assertEqual(sorted(ur.ENV_CACHE_ONLY), ["CODEX_UPGRADE_IDENTITY_MEMO", "PYTHONPYCACHEPREFIX"])
        names = {entry["name"] for entry in ur.executor_environment({"CI": "true", "UNIT_EXECUTOR_KIND": "retry", "PYTHONPYCACHEPREFIX": "/p"})}
        self.assertIn("envvar:CI", names, "执行器进程环境里的 CI 进环境指纹")
        self.assertNotIn("envvar:UNIT_EXECUTOR_KIND", names)
        self.assertNotIn("envvar:PYTHONPYCACHEPREFIX", names)

    def test_a_declared_integration_unit_keeps_ci_true_next_to_the_offline_variables(self) -> None:
        base.install_wp1_stubs(self)
        world = base.World(self)
        world.declare("backend:integration", base.default_scope(contracts=("toolchain:go",)))
        world.write()
        env = base.inputs_of(world.plan())["backend:integration"]["env"]
        self.assertEqual(env.get("CI"), "true")
        for key, value in eg.DECLARED_OFFLINE_ENV.items():
            self.assertEqual(env.get(key), value)

    def test_the_driver_whitelist_environment_does_not_pass_ci_so_it_must_live_in_the_unit_env(self) -> None:
        """驱动 entry-gates.sh 的白名单环境（env -i）不放行 CI——所以 CI=true 必须写在单元自己的命令环境里（上面几项核对的就是它），不能指望继承。"""

        script = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh").read_text(encoding="utf-8")
        start = script.index("EXEC_ENV=(env -i")
        block = script[start:script.index("EXEC_ENV+=(PYTHONPATH", start)]
        self.assertIsNone(re.search(r"\bCI\b", block))


@unittest.skipUnless(HAVE_GO, "需要本机装有 Go")
class DockerMissingFailsTheIntegrationUnitTests(unittest.TestCase):
    """缺 Docker 时该单元判失败而不是通过：用假 docker（PATH 垫片）和一份由真实脚手架原文拼出的最小 Go 测试包，经按包运行的包装器实际跑一遍。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.module = self.root / "module"
        (self.module / "harness").mkdir(parents=True)
        (self.module / "go.mod").write_text("module example.com/harness\n\ngo 1.18\n", encoding="utf-8")
        (self.module / "harness" / "harness_test.go").write_text(mirrored_harness(), encoding="utf-8")
        self.unit = base.World(self).units["backend:integration"]

    def shim(self, name: str, exit_code: int) -> str:
        directory = self.root / name
        directory.mkdir()
        (directory / "docker").write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        (directory / "docker").chmod(0o755)
        return f"{directory}:{os.environ['PATH']}"

    def unit_env(self, path: str, *, ci: bool = True) -> dict[str, str]:
        planned = {key: value for key, value in self.unit["env"].items() if ci or key != "CI"}
        return {**planned, "PATH": path, "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "", "GOCACHE": str(self.root / "gocache"),
                "HOME": str(self.root)}

    def run_wrapper(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(WRAPPER), "--tags", "integration", "--pattern", "./..."], cwd=self.module, capture_output=True,
                              text=True, timeout=300, env={**env, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_without_docker_the_planned_environment_makes_the_unit_fail(self) -> None:
        completed = self.run_wrapper(self.unit_env(self.shim("no-docker", 1)))
        self.assertEqual(completed.returncode, 1, completed.stdout[-1500:])
        self.assertIn("failing integration tests", completed.stdout + completed.stderr)

    def test_without_ci_the_same_missing_docker_would_silently_pass_which_is_the_gap_the_env_closes(self) -> None:
        completed = self.run_wrapper(self.unit_env(self.shim("no-docker", 1), ci=False))
        self.assertEqual(completed.returncode, 0, completed.stdout[-1500:])
        self.assertIn("skipping integration tests", completed.stdout + completed.stderr)

    def test_with_docker_available_the_unit_runs_its_tests_and_passes(self) -> None:
        completed = self.run_wrapper(self.unit_env(self.shim("docker-ok", 0)))
        self.assertEqual(completed.returncode, 0, completed.stdout[-1500:])


@unittest.skipUnless(HAVE_GO, "需要本机装有 Go")
class DockerMissingThroughTheExecutorTests(RetryRunBase):
    """同样的场景交给真实的执行器进程：单元带着计划里的命令环境（CI=true），缺 Docker 时汇总里是失败，补跑也救不回来。"""

    def test_the_executor_reports_the_unit_as_failed_when_docker_is_missing(self) -> None:
        module = self.root / "module"
        (module / "harness").mkdir(parents=True)
        (module / "go.mod").write_text("module example.com/harness\n\ngo 1.18\n", encoding="utf-8")
        (module / "harness" / "harness_test.go").write_text(mirrored_harness(), encoding="utf-8")
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (shim / "docker").chmod(0o755)
        planned = base.World(self).units["backend:integration"]
        env = {**planned["env"], "PATH": f"{shim}:{os.environ['PATH']}", "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "",
               "GOCACHE": str(self.root / "gocache"), "HOME": str(self.root)}
        unit = self.command_unit("backend:integration", [str(WRAPPER), "--tags", "integration", "--pattern", "./..."], cwd=str(module), env=env)
        completed, summary, _manifest = self.run_gates([unit])
        self.assertEqual(completed.returncode, 1, completed.stdout[-1500:] + completed.stderr[-800:])
        self.assertEqual(summary["status"], "failed")
        row = summary["units"][0]
        self.assertFalse(row["passed"])
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["retry"]["status"], "retry-failed", "补跑时 Docker 仍然缺失，补跑也是红的")


if __name__ == "__main__":
    unittest.main()
