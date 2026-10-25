#!/usr/bin/env python3
"""后端三个 Go 单元的外部依赖登记（Codex 升级工具优化第 8 项的剩余部分）。

三个单元：``backend:go-test``（无标签）、``backend:unit``（``-tags=unit``）、``backend:integration``（``-tags=integration``）。它们原来都是
``rerun-only``，因为测试会解析并连接真实的外网域名、integration 还要 Docker 守护进程与 testcontainers 镜像。测试类分支已经把 DNS 改成注入的解析器桩、
把 ``tlsfingerprint`` 的外网用例改成本地 TLS 桩；剩下的是 integration 的 Docker 依赖，和“还没有 ARM64 断网沙箱下的声明级采样证据”。

本模块把这些写成**机器可核的清单**，不让登记表里的自然语言原因悄悄过时：

* ``DEPENDENCIES``：逐项依赖及其状态——``cleared``（已清掉）、``contract-ready``（声明级合同已就绪、待 ARM64 取摘要与采样）、
  ``not-inheritable``（清不掉或还没证据，明确不可承接）；
* ``contracts_for`` ／ ``unix_sockets_for``：每个单元登记为 ``declared`` 时 ``scope.contracts`` 必须包含的合同（integration：``toolchain:docker``、
  ``service-image:postgres``／``redis``／``ryuk``，不然 testcontainers 起的容器就成了不在输入里的隐藏依赖）和允许连接的 AF_UNIX 目标；
* ``reason_for``：登记表里 ``rerun-only`` 原因的唯一来源，``tools/ci/command_input_scopes.json`` 里的文字必须与它逐字一致（``registry_problems`` 核对），
  依赖状态一变，原因就必须跟着改；
* ``command_tags``：认得单元命令的两种写法——裸 ``go test -tags=…`` 和按包运行的包装器 ``go_test_packages.py --tags …``（优化第 3 项把三个单元的命令首词换成了包装器）；
  ``plan_problems`` 核对计划里的命令确实是这三个单元各自该有的标签；
* ``digests``：现取三个镜像（postgres、redis、ryuk）的镜像 Id 与 Docker 版本——**拿不到摘要、摘要形状不对、配置了 Hub 前缀、引用解析不了都失败关闭**。

命令行（ARM64 上由调度者执行，变量一律 ``${VAR}``）::

    python3 -B ${DRV}/backend_dependencies.py inventory                       # 打印依赖清单
    python3 -B ${DRV}/backend_dependencies.py digests --tree ${TREE}          # 现取镜像摘要；退出码 0 全部拿到、3 有拿不到的（失败关闭，不得登记）
    python3 -B ${DRV}/backend_dependencies.py check --tree ${TREE}            # 登记表与本清单一致；加 --manifest 另核计划里的命令
    python3 -B ${DRV}/backend_dependencies.py apply-draft --draft ${SAMPLE}/command_input_scopes.draft.json --out ${SAMPLE}/command_input_scopes.json

``apply-draft`` 把本清单要求的合同与 AF_UNIX 目标并进采样器草案里这三个单元的 ``scope``（草案里已是 ``declared`` 的才动，``rerun-only`` 不动），再核一遍。
本模块只依赖标准库；探测复用同目录 ``entry_gates.py`` 的合同探测（``declared_probe``），不另写第二套。
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
SCHEMA = "backend-dependencies/v1"
WRAPPER_NAME = "go_test_packages.py"
DOCKER_SOCKET = "/var/run/docker.sock"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")

STATE_CLEARED = "cleared"
STATE_CONTRACT_READY = "contract-ready"
STATE_NOT_INHERITABLE = "not-inheritable"
STATES = (STATE_CLEARED, STATE_CONTRACT_READY, STATE_NOT_INHERITABLE)
STATE_LABELS = {STATE_CLEARED: "已清掉", STATE_CONTRACT_READY: "合同已就绪待取摘要", STATE_NOT_INHERITABLE: "仍不可承接"}

# 三个单元：门禁项、构建标签、是否需要 Docker、10-14 ARM64 真实采样时的历史中位耗时（秒，原因文字沿用）。
UNITS: dict[str, dict[str, Any]] = {
    "backend:go-test": {"gate": "backend-go-test", "tags": "", "docker": False, "median_seconds": 559.8,
                        "observed": "连向外部 DNS 解析器（端口 53）与外网 HTTPS（端口 443，样本里有 Cloudflare 地址段）"},
    "backend:unit": {"gate": "backend-unit", "tags": "unit", "docker": False, "median_seconds": 618.6,
                     "observed": "连向外部 DNS 解析器（端口 53）与外网 HTTPS（端口 443，样本里约 30 个非回环 IP，含 Google、Cloudflare 地址段；"
                                 "代码见 urlvalidator.ValidateResolvedIP、channel_monitor_ssrf）"},
    "backend:integration": {"gate": "backend-integration", "tags": "integration", "docker": True, "median_seconds": 578.0,
                            "observed": "连向外部 DNS 解析器（端口 53）与外网 HTTPS（端口 443，含 205.185.123.167:443），另连 /var/run/docker.sock，"
                                        "需 Docker 守护进程与 testcontainers 镜像"},
}
ALL_UNITS = tuple(UNITS)
DOCKER_UNITS = tuple(unit for unit, info in UNITS.items() if info["docker"])

# 单元登记为 declared 时都要有的基础合同（go 命令与它的缓存；离线要求由 entry_gates 在清单 env 里注入）。
BASE_CONTRACTS = ("toolchain:go", "cache:go-build-cache", "cache:go-mod-cache")
DOCKER_CONTRACTS = ("toolchain:docker", "service-image:postgres", "service-image:redis", "service-image:ryuk")
IMAGE_CONTRACTS = ("service-image:postgres", "service-image:redis", "service-image:ryuk")

# 依赖清单。每项：id、标题、涉及的单元、状态、怎么处理的、还差什么。
DEPENDENCIES: tuple[dict[str, Any], ...] = (
    {"id": "dns", "title": "外部 DNS 解析（端口 53）", "units": ALL_UNITS, "state": STATE_CLEARED,
     "how": "测试类分支把 DNS 解析改成注入的解析器桩，用例不再连真实 DNS 解析器；原先会解析 example.com 等外网域名的用例结果依赖外部网络，是后端三单元永远不能承接的主因",
     "remaining": "待 ARM64 断网沙箱复验"},
    {"id": "external-https", "title": "外网 HTTPS（端口 443，tlsfingerprint 的外网用例）", "units": ALL_UNITS, "state": STATE_CLEARED,
     "how": "测试类分支把 tlsfingerprint 的外网用例改成本地 TLS 桩，JA3／JA4 套件哈希期望值在本地复现，不再连真实主机",
     "remaining": "待 ARM64 断网沙箱复验"},
    {"id": "docker-engine", "title": "Docker 命令行与守护进程（/var/run/docker.sock；脚手架 TestMain 执行 docker info）", "units": DOCKER_UNITS,
     "state": STATE_CONTRACT_READY,
     "how": "合同 toolchain:docker（docker 可执行文件摘要＋客户端与守护进程版本行，守护进程不可达时取不到版本、失败关闭）；scope.unix_sockets 登记 /var/run/docker.sock；"
            "守护进程所在内核已在执行器环境指纹里",
     "remaining": "待 ARM64 采样（--docker-hooks、--allow-unix /var/run/docker.sock）取得证据"},
    {"id": "image-postgres", "title": "测试镜像 postgres（引用取自脚手架常量 postgresImageTag，现为 postgres:18.1-alpine3.23）", "units": DOCKER_UNITS,
     "state": STATE_CONTRACT_READY, "how": "合同 service-image:postgres（引用＋镜像 Id；可被 SUB2API_TEST_POSTGRES_IMAGE 覆盖，覆盖值进摘要）",
     "remaining": "待 ARM64 现取镜像 Id（digests 子命令），取不到失败关闭"},
    {"id": "image-redis", "title": "测试镜像 redis（现为 redis:8.4-alpine，引用在脚手架、中间件限流集成测试、路由限流集成测试三处各写一遍）", "units": DOCKER_UNITS,
     "state": STATE_CONTRACT_READY,
     "how": "合同 service-image:redis（引用＋镜像 Id）；三处源码的 redis 引用由交叉核对保证一致，任何一处单独改了都失败关闭",
     "remaining": "待 ARM64 现取镜像 Id，取不到失败关闭"},
    {"id": "image-ryuk", "title": "testcontainers 回收容器镜像 ryuk（testcontainers-go v0.40.0 内置，现为 testcontainers/ryuk:0.13.0）", "units": DOCKER_UNITS,
     "state": STATE_CONTRACT_READY,
     "how": "合同 service-image:ryuk（引用取自 testcontainers-go 的 ReaperDefaultImage，不在测试源码里；起任何容器都先起它）",
     "remaining": "待 ARM64 现取镜像 Id，取不到失败关闭"},
    {"id": "docker-missing-skips", "title": "Docker 缺失时整包跳过且判通过（脚手架 TestMain：未设 CI 变量就 os.Exit(0)，约 216 个 integration 用例一个都不跑）",
     "units": DOCKER_UNITS, "state": STATE_NOT_INHERITABLE,
     "how": "镜像合同取不到摘要时记录不可承接，所以空跑的“通过”不会被承接",
     "remaining": "运行本身仍可能空绿；是否给 integration 单元固定 CI=true（缺 Docker 时直接失败）会改变门禁通过语义，待调度者决定"},
    {"id": "sampling-evidence", "title": "ARM64 断网沙箱下的声明级采样证据（declared-sample/v1）", "units": ALL_UNITS, "state": STATE_NOT_INHERITABLE,
     "how": "登记为 declared 需要有效的采样证据（命令摘要、启动前缀摘要、采样器摘要、读集覆盖），本机不能代替 ARM64 采样",
     "remaining": "取到证据前保持 rerun-only"},
)


def dependencies_for(unit_id: str) -> list[dict[str, Any]]:
    return [item for item in DEPENDENCIES if unit_id in item["units"]]


def contracts_for(unit_id: str) -> list[str]:
    """单元登记为 declared 时 ``scope.contracts`` 必须包含的合同（升序）。"""

    if unit_id not in UNITS:
        raise ValueError(f"不是后端 Go 单元：{unit_id!r}")
    return sorted({*BASE_CONTRACTS, *(DOCKER_CONTRACTS if UNITS[unit_id]["docker"] else ())})


def unix_sockets_for(unit_id: str) -> list[str]:
    if unit_id not in UNITS:
        raise ValueError(f"不是后端 Go 单元：{unit_id!r}")
    return [DOCKER_SOCKET] if UNITS[unit_id]["docker"] else []


def reason_for(unit_id: str) -> str:
    """登记表里该单元 ``rerun-only`` 原因的唯一来源：逐项依赖按状态分段列出，末尾保留历史中位耗时。"""

    if unit_id not in UNITS:
        raise ValueError(f"不是后端 Go 单元：{unit_id!r}")
    parts = []
    for state in STATES:
        rows = [item for item in dependencies_for(unit_id) if item["state"] == state]
        if rows:
            parts.append(f"【{STATE_LABELS[state]}】" + "；".join(f"{item['title']}：{item['how']}（{item['remaining']}）" for item in rows))
    return (f"10-14 ARM64 真实采样证实该单元{UNITS[unit_id]['observed']}，结果依赖外部 DNS 与网络，当时不可声明级承接。"
            "现按外部依赖清单（见 tools/ci/backend_dependencies.py）逐项登记：" + " ".join(parts)
            + f"。因此登记为 rerun-only，取到采样证据后改 declared（历史中位 {UNITS[unit_id]['median_seconds']} 秒）")


# ---------------------------------------------------------------------------
# 命令识别（裸 go test 与包装器）
# ---------------------------------------------------------------------------


def command_tags(argv: Sequence[str]) -> str | None:
    """Go 测试命令的构建标签：裸 ``go test -tags=X`` 或 ``-tags X``，包装器 ``go_test_packages.py --tags X`` 或 ``--tags=X``；
    不带标签返回空串；不是 Go 测试命令（也不是包装器）返回 None。argv 可以带启动前缀。"""

    words = [str(word) for word in argv]
    start = next((index + 2 for index in range(len(words) - 1) if words[index:index + 2] == ["go", "test"]), None)
    flags = ("-tags", "--tags")
    if start is None:
        wrapper = next((index for index, word in enumerate(words) if Path(word).name == WRAPPER_NAME), None)
        if wrapper is None:
            return None
        start = wrapper + 1
    tags = ""
    index = start
    while index < len(words):
        word = words[index]
        if word == "--":
            break
        if word in flags and index + 1 < len(words):
            tags, index = words[index + 1], index + 2
            continue
        for flag in flags:
            if word.startswith(flag + "="):
                tags = word.split("=", 1)[1]
        index += 1
    return tags


def plan_problems(units: Sequence[Mapping[str, Any]]) -> list[str]:
    """计划（清单 ``units``）里的三个后端单元：命令必须是 Go 测试（裸 go test 或包装器）且标签与该单元的约定一致；缺单元不报（别的组合只规划一部分）。"""

    problems: list[str] = []
    for unit in units:
        unit_id = str(unit.get("unit_id"))
        if unit_id not in UNITS:
            continue
        tags = command_tags(unit.get("argv") or [])
        if tags is None:
            problems.append(f"{unit_id}：命令不是 go test，也不是按包运行的包装器 {WRAPPER_NAME}")
        elif tags != UNITS[unit_id]["tags"]:
            problems.append(f"{unit_id}：命令的构建标签是 {tags!r}，该单元约定为 {UNITS[unit_id]['tags']!r}（登记的合同与采样都按约定的标签）")
    return problems


# ---------------------------------------------------------------------------
# 登记表核对与草案处理
# ---------------------------------------------------------------------------


def registry_problems(registry: Any) -> list[str]:
    """登记表里三个后端单元与本清单一致：rerun-only 的原因逐字等于 ``reason_for``；declared 的 scope 必须带齐 ``contracts_for``、
    docker 单元带 AF_UNIX 目标，非 docker 单元不得多声明 Docker 合同与目标。"""

    units = registry.get("units") if isinstance(registry, Mapping) else None
    if not isinstance(units, Mapping):
        return ["登记表格式非法（没有 units）"]
    problems: list[str] = []
    for unit_id in ALL_UNITS:
        entry = units.get(unit_id)
        if not isinstance(entry, Mapping):
            problems.append(f"{unit_id}：登记表里没有这个单元")
            continue
        if entry.get("status") == "rerun-only":
            if entry.get("reason") != reason_for(unit_id):
                problems.append(f"{unit_id}：rerun-only 的原因与依赖清单不一致（依赖状态变了，原因必须跟着改；应由 reason_for 生成）")
        elif entry.get("status") == "declared":
            scope = entry.get("scope") if isinstance(entry.get("scope"), Mapping) else {}
            contracts, sockets = set(scope.get("contracts") or []), set(scope.get("unix_sockets") or [])
            missing = sorted(set(contracts_for(unit_id)) - contracts)
            if missing:
                problems.append(f"{unit_id}：declared 的 scope.contracts 缺 {missing}")
            if not UNITS[unit_id]["docker"]:
                extra = sorted(item for item in contracts if item.startswith("service-image:") or item == "toolchain:docker")
                if extra or sockets & {DOCKER_SOCKET}:
                    problems.append(f"{unit_id}：不需要 Docker 的单元不得声明 Docker 合同或 AF_UNIX 目标（{extra or sorted(sockets)}）")
            elif DOCKER_SOCKET not in sockets:
                problems.append(f"{unit_id}：declared 的 scope.unix_sockets 缺 {DOCKER_SOCKET}")
        else:
            problems.append(f"{unit_id}：status 必须是 declared 或 rerun-only")
    return problems


def apply_to_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    """把本清单要求的合同与 AF_UNIX 目标并进采样器草案里**已是 declared** 的后端单元（rerun-only 与其它单元不动），返回新草案。"""

    result = copy.deepcopy(dict(draft))
    for unit_id, entry in (result.get("units") or {}).items():
        if unit_id in UNITS and isinstance(entry, dict) and entry.get("status") == "declared":
            scope = entry["scope"]
            scope["contracts"] = sorted({*(scope.get("contracts") or []), *contracts_for(unit_id)})
            sockets = sorted({*(scope.get("unix_sockets") or []), *unix_sockets_for(unit_id)})
            if sockets:
                scope["unix_sockets"] = sockets
    return result


# ---------------------------------------------------------------------------
# 现取摘要（失败关闭）
# ---------------------------------------------------------------------------


def _entry_gates() -> Any:
    name = "entry_gates_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "entry_gates.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"找不到同目录的 entry_gates.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def digests(tree: Path, *, environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """现取 integration 单元的 Docker 依赖事实：三个镜像的引用与镜像 Id、docker 版本行与可执行文件摘要。

    ``ok`` 为真当且仅当全部拿到：镜像引用解析得出（源码交叉核对通过）、没有配置 Hub 前缀、镜像 Id 是 ``sha256:<64 位十六进制>``、docker 版本取得到。
    任何一项拿不到都 ``ok=False`` 并写进 ``problems``——调用方（命令行退出码 3）不得据此登记或放行。"""

    gates = _entry_gates()
    scope = {"schema_version": gates.DECLARED_SCOPE_SCHEMA, "contracts": [*IMAGE_CONTRACTS, "toolchain:docker"]}
    entries = gates.declared_contract_entries(scope, tree=Path(tree), environ=environ, runner=runner)
    problems = list(gates.declared_contract_problems(entries))
    images: dict[str, Any] = {}
    docker: dict[str, Any] = {}
    for entry in entries:
        detail = entry["detail"]
        if detail.get("type") == "service-image":
            image_id = detail.get("image_id")
            images[detail["id"]] = {"reference": detail.get("reference"), "image_id": image_id, "ok": isinstance(image_id, str) and bool(IMAGE_ID.fullmatch(image_id))}
        elif detail.get("type") == "toolchain":
            docker = {"version_line": detail.get("version_line"), "executable_sha256": detail.get("executable_sha256")}
    for name in ("postgres", "redis", "ryuk"):
        if not images.get(name, {}).get("ok"):
            problems.append(f"镜像 {name} 没有拿到 sha256 镜像 Id：{images.get(name, {}).get('image_id')!r}")
    if not docker.get("version_line") or str(docker.get("version_line")).startswith("unavailable("):
        problems.append(f"拿不到 Docker 版本（守护进程不可达或没装 docker）：{docker.get('version_line')!r}")
    ok = not problems
    return {"schema_version": SCHEMA, "ok": ok, "images": images, "docker": docker, "problems": sorted(set(problems)),
            "contract_digests": {entry["name"]: entry["sha256"] for entry in entries}}


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _inventory_text() -> str:
    lines = []
    for unit_id in ALL_UNITS:
        lines.append(f"{unit_id}（标签 {UNITS[unit_id]['tags'] or '无'}）")
        for item in dependencies_for(unit_id):
            lines.append(f"  [{STATE_LABELS[item['state']]}] {item['title']}：{item['how']}（{item['remaining']}）")
    return "\n".join(lines)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("inventory", help="打印依赖清单")
    inventory.add_argument("--json", action="store_true")
    contracts = sub.add_parser("contracts", help="打印某单元登记为 declared 时要求的合同与 AF_UNIX 目标")
    contracts.add_argument("--unit", required=True, choices=ALL_UNITS)
    sub.add_parser("reasons", help="打印登记表里三个单元 rerun-only 原因应有的文字（JSON）")
    digest = sub.add_parser("digests", help="现取镜像 Id 与 Docker 版本（退出码 0 全部拿到、3 有拿不到的）")
    digest.add_argument("--tree", type=Path, required=True)
    check = sub.add_parser("check", help="登记表与依赖清单一致；给 --manifest 另核计划里的命令")
    check.add_argument("--tree", type=Path, required=True)
    check.add_argument("--registry", type=Path, default=None)
    check.add_argument("--manifest", type=Path, default=None)
    draft = sub.add_parser("apply-draft", help="把要求的合同与 AF_UNIX 目标并进采样器草案")
    draft.add_argument("--draft", type=Path, required=True)
    draft.add_argument("--out", type=Path, required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command == "inventory":
        print(json.dumps({"schema_version": SCHEMA, "units": {unit: {**UNITS[unit], "dependencies": dependencies_for(unit)} for unit in ALL_UNITS}},
                         ensure_ascii=False, indent=2) if args.json else _inventory_text())
        return 0
    if args.command == "contracts":
        print(json.dumps({"unit": args.unit, "contracts": contracts_for(args.unit), "unix_sockets": unix_sockets_for(args.unit)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "reasons":
        print(json.dumps({unit: reason_for(unit) for unit in ALL_UNITS}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "digests":
        report = digests(args.tree)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            print("后端 Docker 依赖的镜像摘要没有全部拿到：失败关闭，不得登记或放行：\n" + "\n".join(f"  {problem}" for problem in report["problems"]), file=sys.stderr)
        return 0 if report["ok"] else 3
    if args.command == "check":
        gates = _entry_gates()
        registry_file = args.registry or (args.tree / gates.DECLARED_REGISTRY_PATH)
        problems = registry_problems(json.loads(registry_file.read_text(encoding="utf-8")))
        if args.manifest is not None:
            problems += plan_problems(json.loads(args.manifest.read_text(encoding="utf-8")).get("units") or [])
        for problem in problems:
            print(problem)
        print("一致" if not problems else f"{len(problems)} 处不一致")
        return 0 if not problems else 3
    draft = json.loads(args.draft.read_text(encoding="utf-8"))
    merged = apply_to_draft(draft)
    problems = registry_problems(merged)
    args.out.write_text(json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for problem in problems:
        print(problem)
    print(f"已写 {args.out}" + ("" if not problems else f"；仍有 {len(problems)} 处不一致"))
    return 0 if not problems else 3


if __name__ == "__main__":
    sys.exit(main())
