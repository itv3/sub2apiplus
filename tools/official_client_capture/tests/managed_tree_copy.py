"""测试基座：受管工具树副本（改造 5 M1 审核修正，老板拍板 4.1）。

受管代码不加任何测试钩子。"evaluator 修复"以**副本受管树**表达：把 ``tools/official_client_capture``
复制到临时仓库根 ``<dest>/tools/official_client_capture``，在副本里做真实修改（修 checker＝改
``candidate_rule_assertion.py``；注入 accept／compare 缺陷＝改副本 ``codex_upgrade.py``），随后所有受管
子进程（CLI、builder、checker、夹具驱动）以 ``PYTHONPATH=<dest>``、``cwd=<dest>`` 运行，模块 ``__file__``
必须落在副本树内（``assert_tree_binding``），禁止回落导入原仓库。

本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = REPO_ROOT / "tools" / "official_client_capture"
PACKAGE_RELATIVE = Path("tools") / "official_client_capture"
CHECKER_RELATIVE = "candidate_rule_assertion.py"
BUILDER_RELATIVE = "build_rule_assertion_results.py"
ORCHESTRATOR_RELATIVE = "codex_upgrade.py"

# 副本 A 的 checker 缺陷：让 SPEC-EP-006 的每条 check 都误判为失败（候选证据本身合规）。
CHECKER_DEFECT_ANCHOR = '        passed, actual = _evaluate_assertion(matched, check["assertion"])\n'
CHECKER_DEFECT_INJECTION = (
    CHECKER_DEFECT_ANCHOR
    + '        if rule_id == "SPEC-EP-006":\n'
    + '            passed = False  # 测试副本注入的 checker 误判（改造 5 M1 用例 1）\n'
)
# 副本 C／D：accept／compare 读侧缺陷——入口直接抛错（只改各自根函数体，闭包摘要只变本侧）。
ACCEPT_DEFECT_ANCHOR = 'def accept_campaign(\n'
COMPARE_DEFECT_ANCHOR = 'def compare_campaign(campaign_dir: Path, candidate_id: str) -> dict[str, Any]:\n'


class ManagedTreeCopyError(RuntimeError):
    pass


def copy_managed_tree(destination: Path, *, include_tests: bool = True) -> Path:
    """把受管树复制到 ``destination/tools/official_client_capture``，返回 ``destination``（仓库根副本）。"""

    destination = Path(destination)
    target = destination / PACKAGE_RELATIVE
    if target.exists():
        raise ManagedTreeCopyError(f"副本目标已存在：{target}")
    ignore = ["__pycache__", "versions", "*.pyc"]
    if not include_tests:
        ignore.append("tests")
    shutil.copytree(TOOL_ROOT, target, ignore=shutil.ignore_patterns(*ignore))
    # 夹具按仓库相对路径读取两份指南文档（source_spec 摘要、场景清单绑定）；文档不进受管身份。
    docs = destination / "docs"
    docs.mkdir(mode=0o700, exist_ok=True)
    for name in ("CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md"):
        source = REPO_ROOT / "docs" / name
        if source.is_file():
            shutil.copy2(source, docs / name)
    return destination


def tool_root(tree_root: Path) -> Path:
    return Path(tree_root) / PACKAGE_RELATIVE


def replace_once(tree_root: Path, relative: str, old: str, new: str) -> Path:
    """在副本文件里做恰好一次的文本替换（真实修改，不是 mock）。"""

    path = tool_root(tree_root) / relative
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise ManagedTreeCopyError(f"{relative} 中锚点出现 {source.count(old)} 次，无法唯一替换")
    path.write_text(source.replace(old, new), encoding="utf-8")
    return path


def append_comment(tree_root: Path, relative: str, comment: str) -> Path:
    """在副本文件末尾追加一行注释：行为不变、文件摘要变化（模拟"无害的 evaluator 修复提交"）。"""

    path = tool_root(tree_root) / relative
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"# {comment}\n")
    return path


def inject_checker_defect(tree_root: Path) -> Path:
    return replace_once(tree_root, CHECKER_RELATIVE, CHECKER_DEFECT_ANCHOR, CHECKER_DEFECT_INJECTION)


def _inject_after_docstring(tree_root: Path, anchor: str, statement: str) -> Path:
    """在副本 ``codex_upgrade.py`` 某个 def 的 docstring 之后插入一条语句（只改该函数体）。"""

    path = tool_root(tree_root) / ORCHESTRATOR_RELATIVE
    source = path.read_text(encoding="utf-8")
    if source.count(anchor) != 1:
        raise ManagedTreeCopyError(f"锚点出现 {source.count(anchor)} 次：{anchor!r}")
    index = source.index(anchor)
    signature_end = source.index("\n", index + len(anchor) - 1) + 1 if not anchor.endswith("\n") else index + len(anchor)
    # def 行可能跨多行（多参数签名）：定位到 ') -> ...:\n' 结束的那一行。
    if not source[index:signature_end].rstrip().endswith(":"):
        signature_end = source.index(":\n", signature_end) + 2
    rest = source[signature_end:]
    line_end = signature_end
    if rest.lstrip().startswith('"""'):
        doc_open = source.index('"""', signature_end)
        doc_close = source.index('"""', doc_open + 3) + 3
        line_end = source.index("\n", doc_close) + 1
    path.write_text(source[:line_end] + f"    {statement}\n" + source[line_end:], encoding="utf-8")
    return path


def inject_accept_defect(tree_root: Path, marker: str = "injected-accept-defect") -> Path:
    return _inject_after_docstring(tree_root, ACCEPT_DEFECT_ANCHOR, f'raise ConfigurationError("{marker}")')


def mutate_accept_reader(tree_root: Path) -> Path:
    """accept 读侧无害改动（只让 accept_reader 闭包摘要变化，行为不变）。"""

    return _inject_after_docstring(tree_root, ACCEPT_DEFECT_ANCHOR, '_evaluator_fix_marker = "accept reader fix"')


def mutate_compare_reader(tree_root: Path) -> Path:
    return _inject_after_docstring(tree_root, COMPARE_DEFECT_ANCHOR, '_evaluator_fix_marker = "compare reader fix"')


def inject_compare_defect(tree_root: Path, marker: str = "injected-compare-defect") -> Path:
    return _inject_after_docstring(tree_root, COMPARE_DEFECT_ANCHOR, f'raise ConfigurationError("{marker}")')


# 采集执行位置是合同常量（execution_contract.capture_root＝/root/oauth-capture）。有生产执行副本的机器上，
# 副本受管树建 Campaign 时 _verify_execution_tree 会拿该位置与副本树逐字比对；不放宽校验，而是让每个
# 受管子进程在自己的 mount namespace 里把该固定路径绑定到当前副本树（老板 2026-09-20 二次拍板）。
PRODUCTION_CAPTURE_ROOT = Path("/root/oauth-capture")
PRODUCTION_EXECUTION_TREE = PRODUCTION_CAPTURE_ROOT / "tools" / "official_client_capture"
GO_TOOLCHAIN_BIN = Path("/usr/local/go/bin")


def execution_tree_binding_required() -> bool:
    return PRODUCTION_EXECUTION_TREE.is_dir()


def execution_tree_binding_available() -> bool:
    return execution_tree_binding_required() and os.geteuid() == 0 and shutil.which("unshare") is not None and sys.platform.startswith("linux")


def subprocess_env(tree_root: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """副本子进程环境：PYTHONPATH 只含副本仓库根，禁止回落导入原仓库；go 工具链不在 PATH 时前置。"""

    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(Path(tree_root))
    # 字节码缓存写到副本树之外（按源文件绝对路径镜像，各副本树互不串用）：副本树内不出现 __pycache__，
    # 又免去每个受管子进程重新编译 codex_upgrade.py 的开销（ARM64 上每步约 20 秒）。
    env["PYTHONPYCACHEPREFIX"] = str(Path(tree_root).parent / ".pycache-prefix")
    if shutil.which("go") is None and (GO_TOOLCHAIN_BIN / "go").is_file():
        env["PATH"] = f"{GO_TOOLCHAIN_BIN}:{env.get('PATH', '')}"
    if extra:
        env.update(extra)
    return env


def python_command(tree_root: Path, arguments: Sequence[str]) -> list[str]:
    """副本子进程命令：需要绑定执行位置时以独立 mount namespace 把固定 capture_root 绑定到本副本树。"""

    command = [sys.executable, *arguments]
    if execution_tree_binding_required():
        if not execution_tree_binding_available():
            raise ManagedTreeCopyError(
                f"本机存在固定采集执行副本 {PRODUCTION_EXECUTION_TREE}，但无法建立 mount namespace（需要 root 与 unshare）"
            )
        return [
            "unshare", "-m", "--propagation", "private", "sh", "-c",
            f'mount --bind "$0" {PRODUCTION_CAPTURE_ROOT} && exec "$@"',
            str(Path(tree_root)), *command,
        ]
    return command


def run_python(
    tree_root: Path,
    arguments: Sequence[str],
    *,
    extra_env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """在副本仓库根下运行 ``python3 <arguments>``（cwd＝副本根；必要时在绑定了执行位置的 mount namespace 内）。"""

    return subprocess.run(
        python_command(tree_root, arguments),
        capture_output=True,
        text=True,
        cwd=str(Path(tree_root)),
        env=subprocess_env(tree_root, extra_env),
        input=input_text,
        timeout=timeout,
    )


TREE_BINDING_SNIPPET = (
    "import sys, pathlib\n"
    "from tools.official_client_capture import codex_upgrade, build_rule_assertion_results, candidate_rule_assertion\n"
    "root = pathlib.Path(sys.argv[1]).resolve()\n"
    "for module in (codex_upgrade, build_rule_assertion_results, candidate_rule_assertion):\n"
    "    path = pathlib.Path(module.__file__).resolve()\n"
    "    assert root in path.parents, f'{module.__name__} 回落到副本外：{path}'\n"
    "print('bound')\n"
)


def assert_tree_binding(tree_root: Path) -> None:
    """子进程内断言三个关键受管模块的 ``__file__`` 都在副本树下。"""

    completed = run_python(tree_root, ["-c", TREE_BINDING_SNIPPET, str(tree_root)])
    if completed.returncode != 0 or completed.stdout.strip() != "bound":
        raise ManagedTreeCopyError(f"副本树绑定失败：{completed.stderr[-800:]}")


def evaluator_digests(tree_root: Path) -> dict[str, str]:
    """副本树的 evaluator 四项摘要（与副本内 ``evaluator_dependency_digests()`` 同口径）。"""

    from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy

    return dict(policy.evaluator_dependency_digests(tool_root(tree_root)))


def tool_identity(tree_root: Path) -> dict[str, Any]:
    """副本树的受管身份（策略 v2 五摘要＋files_sha256），供测试生成部署收据 fixture。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy

    root = tool_root(tree_root)
    entries = codex_upgrade._tool_tree_entries(root)
    active = policy.load_policy(root / policy.POLICY_FILENAME)
    identity = policy.compute_identity_v2(active, root, entries)
    return {
        "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
        "policy_version": identity["policy_version"],
        "policy_sha256": identity["policy_sha256"],
        "wire_producer_sha256": identity["wire_producer_sha256"],
        "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
        "control_sha256": identity["control_sha256"],
    }
