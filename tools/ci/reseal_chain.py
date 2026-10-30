#!/usr/bin/env python3
"""版本登记「重封链」一键化（工具优化第 3 条）。

每次 Codex 版本登记（或任何改动了事实映射已登记的源码、受管工具文件的变更）之后，要把一串生成值按严格顺序重新算一遍：
事实映射重绑 -> 身份守恒基线重封 -> 驱动 manifest 重建 -> 依赖追踪表重新追踪，并且每一步改了冻结文件之后都要补一份冻结承接收据
（收据必须单独提交）。过去全靠手工串，10-30 一天手工做了 3 次，顺序严、容易错。本脚本把这条链固化下来：

    步骤             做什么                                                           提交
    rebind           事实映射重绑：只改 sha256／test_file_sha256 叶子，同步           1 个
                     candidate_test_trace.py 的 FROZEN_MAPPING_SHA256 并追加载荷审核注释
    rebind-receipt   冻结承接收据（freeze-successor-generate），单独提交               1 个
    reseal           身份守恒基线重封（--seal-baseline --replace）、回填               1 个
                     BASELINE_SHA256、清空 REGISTERED_CONTROL_CHANGES
    reseal-receipt   冻结承接收据，单独提交                                            1 个
    manifest         驱动 manifest 重建（build-manifest；本来就一致则跳过）            0～1 个
    trace            重新追踪依赖表：detached 工作树 + 重负载锁 + 后台追踪，             1 个
                     追踪提交（generated_at_commit）是本步之前的 HEAD
    verify           只读复核：映射／守恒／候选追踪脚本三组测试 + manifest 校验          0 个

为什么追踪放在最后而不是第一步：追踪时退出码非 0 的模块会被记进 ``unobserved``（永远照跑、选择精度变差）。事实映射和基线没重封之前，
``test_candidate_test_trace``、身份守恒等模块必然是红的，在那种状态下追踪会白白多出一批 ``unobserved``。所以先把前面的生成值全部落定、
测试转绿，最后一步才追踪；追踪提交就是这时的 HEAD，之后只多一个追踪表提交（追踪提交是之后所有比对基点的祖先）。

设计约束（也是验收点）：

* 每一步开始前断言前置条件：工作树干净、不在合并／变基中途、分支不是 main（除非显式放行）、HEAD 与状态记录一致、本步需要的文件在；
  任何一条不满足，直接停下，什么都不改。
* 每一步成功才提交，失败就停在原地：工作树还原为上一步完成时的干净状态（失败时的改动存成 ``failed-<步骤>.diff``），日志写进 ``--log-dir``，
  并打印"从哪一步继续"的命令。续跑用 ``--from <步骤>``，只跑某一步用 ``--only <步骤>``，只看计划与前置检查用 ``--dry-run``。
* 每一步之后输出"载荷审核报告"（逐项 diff：哪些文件、哪些摘要、旧值->新值），同时写进 ``--log-dir/audit-*.txt``。
  **报告只给人看：本脚本绝不自动签字，也绝不 push、不合并、不打 tag；提交都只落在当前分支上。**
* 耗时长的追踪在后台脱离会话运行（``subprocess.Popen(start_new_session=True)``，等价 ``setsid -f``；本机 macOS 没有 setsid 命令；
  不用 nohup，它会让子进程继承"忽略 SIGHUP"），日志以 ``EXIT=<退出码>`` 结尾；本脚本被中断后再 ``--from trace`` 会接上同一个追踪任务，不会重追。
* 所有对仓库的修改都落成提交，提交说明固定格式（见 ``Chain.commit``）。

前提与边界（脚本不替你判断，只在计划里提醒）：

* 只能在 Campaign 之外做：本脚本会改 ``tools/official_client_capture`` 下的受管文件（事实映射、candidate_test_trace.py），会改变工具身份，
  已建的 Campaign 因此作废。真实运行必须加 ``--ack-no-campaign`` 确认当前没有在建 Campaign。
* 产生重封需求的源码改动本身（例如改了被映射钉住的后端文件）及其冻结承接收据，由改动者先提交；本脚本的第一份收据从"上一份收据所在提交"起算，
  区间内若还有别的冻结转移，会一并出现在收据与审核报告里，请人工核对。
* 门禁（egress-spec、capture-tools、Go 全包）不在本脚本内：重封链之后由人决定何时跑。

用法（在要处理的 git 工作树里；``--log-dir`` 放在工作树之外）::

    python3 -B tools/ci/reseal_chain.py --log-dir <日志目录> --label <批次名> --reason "<一句话原因>" --ack-no-campaign
    python3 -B tools/ci/reseal_chain.py --log-dir <日志目录> --dry-run          # 只看计划与前置检查
    python3 -B tools/ci/reseal_chain.py --log-dir <日志目录> --ack-no-campaign --from trace   # 失败后续跑
    python3 -B tools/ci/reseal_chain.py --log-dir <日志目录> --ack-no-campaign --only manifest

退出码：0 全部完成；1 某一步失败（已停在原地）；2 前置条件或用法错误；130 被中断。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

# ----------------------------------------------------------------------
# 常量：链上用到的仓库路径、命令与步骤表
# ----------------------------------------------------------------------

# 步骤表：名字 -> 一句话说明。顺序就是执行顺序，不能调换（理由见模块说明）。
STEPS: tuple[tuple[str, str], ...] = (
    ("rebind", "事实映射重绑（sha256 叶子 + FROZEN_MAPPING_SHA256 + 载荷审核注释）"),
    ("rebind-receipt", "事实映射重绑的冻结承接收据（单独提交）"),
    ("reseal", "身份守恒基线重封（--seal-baseline --replace、回填 BASELINE_SHA256、清空 REGISTERED_CONTROL_CHANGES）"),
    ("reseal-receipt", "身份守恒基线重封的冻结承接收据（单独提交）"),
    ("manifest", "驱动 manifest 重建（本来就一致则跳过）"),
    ("trace", "重新追踪依赖表（后台、重负载锁；追踪提交是本步之前的 HEAD）"),
    ("verify", "只读复核：映射／守恒／候选追踪脚本三组测试 + manifest 校验"),
)
STEP_NAMES = tuple(name for name, _ in STEPS)

TRACE_FILE = "tools/official_client_capture/candidate_test_trace.py"
CONSERVATION_FILE = "tools/official_client_capture/tests/test_codex_identity_conservation.py"
CONSERVATION_MODULE = "tools.official_client_capture.tests.test_codex_identity_conservation"
BASELINE_DIR = "tools/official_client_capture/tests/fixtures"
RECEIPT_DIR = "docs/egress/maintenance"
RECEIPT_PATHSPEC = "docs/egress/maintenance/*-freeze-successor.json"
DRIVER_ROOT = "tools/arm64_capture_driver"
DRIVER_INSTALL = "tools/arm64_capture_driver/install.py"
DEPENDENCY_TABLE = "tools/ci/unit_dependencies.json"
DEPENDENCY_TRACE = "tools/ci/dependency_trace.py"
OWNERSHIP = "tools/ci/unit_ownership.py"
# 重绑之后要转绿的两组：钉住的映射／源码快照摘要、候选追踪脚本；封存之后再加身份守恒。
REBIND_TEST_MODULES = (
    "tools.official_client_capture.tests.test_candidate_test_trace",
    "tools.official_client_capture.tests.test_candidate_trace_test",
)
VERIFY_MODULES = REBIND_TEST_MODULES + (CONSERVATION_MODULE,)
SNAPSHOT_KEYS = frozenset({"sha256", "test_file_sha256"})
HEX40 = re.compile(r"[0-9a-f]{40}")
FROZEN_RE = re.compile(r'FROZEN_MAPPING_SHA256 = \(\n    "([0-9a-f]{64})"\n\)')
MAPPING_PATH_RE = re.compile(r'DEFAULT_MAPPING_RELATIVE_PATH = \(\n    "([^"]+)"\n\)')
FROZEN_MARKER = "FROZEN_MAPPING_SHA256 = (\n"
ACCEPTED_RECEIPT_RESULT = "passed_local_evidence_successor"
BRANCH_REFUSED = ("main", "master")
STATE_NAME = "state.json"
COMMAND_LOG = "commands.log"
CHAIN_LOG = "chain.log"
POLL_SECONDS = 20.0
HEARTBEAT_SECONDS = 300.0
TAIL_CHARS = 1800


class ChainError(Exception):
    """前置条件或用法错误：还没动任何东西，退出码 2。"""


class StepFailed(Exception):
    """某一步执行失败：调用方把工作树还原到步骤开始前的状态，退出码 1。"""


# ----------------------------------------------------------------------
# 纯函数：摘要、叶子遍历、重绑计划、基线对比（便于单元测试）
# ----------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short(value: str, length: int = 8) -> str:
    return f"{value[:length]}…"


def leaves(value: Any, path: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """遍历 JSON 的全部叶子，路径是键／下标元组。"""

    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, path + (index,))
    else:
        yield path, value


def canonical_json(document: Any) -> bytes:
    """事实映射的规范序列化：缩进 2、保留键序、不转义中文、末尾换行（与仓库里的映射文件逐字节一致）。"""

    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclasses.dataclass
class Change:
    """一个被重绑的叶子：树内位置、被钉住的文件、旧新摘要。"""

    label: str
    path: str
    old: str
    new: str


@dataclasses.dataclass
class RebindPlan:
    leaf_count: int
    changes: list[Change]
    missing: list[str]
    non_snapshot: list[tuple]
    structure_same: bool
    old_bytes: bytes
    new_bytes: bytes

    @property
    def old_sha(self) -> str:
        return sha256_bytes(self.old_bytes)

    @property
    def new_sha(self) -> str:
        return sha256_bytes(self.new_bytes)


def plan_rebind(root: Path, map_rel: str) -> RebindPlan:
    """按工作树现状重算映射里每个被钉住文件的摘要，返回需要变的叶子（不写文件）。

    只允许 ``sha256`` 与 ``test_file_sha256`` 两种叶子变化；映射文件必须是规范序列化，否则拒绝（改写会连带改格式）。
    """

    raw = (root / map_rel).read_bytes()
    document = json.loads(raw)
    if canonical_json(document) != raw:
        raise StepFailed(f"映射文件不是规范序列化（缩进 2、末尾换行），拒绝改写：{map_rel}")
    before = dict(leaves(document))
    changes: list[Change] = []
    missing: list[str] = []
    for index, test in enumerate(document["tests"]):
        targets = [(f"tests[{index}].test_file_sha256", test["test_file"], test, "test_file_sha256")]
        for position, source in enumerate(test["source_files"]):
            targets.append((f"tests[{index}].source_files[{position}].sha256", source["path"], source, "sha256"))
        for label, relative, holder, key in targets:
            target = root / relative
            if not target.is_file():
                missing.append(f"{label} -> {relative}")
                continue
            current = sha256_bytes(target.read_bytes())
            if holder[key] != current:
                changes.append(Change(label, relative, holder[key], current))
                holder[key] = current
    after = dict(leaves(document))
    non_snapshot = [path for path in after if path in before and after[path] != before[path] and path[-1] not in SNAPSHOT_KEYS]
    return RebindPlan(len(before), changes, missing, non_snapshot, set(before) == set(after), raw, canonical_json(document))


def rebind_comment(date: str, reason: str, plan: RebindPlan) -> str:
    """追加到 FROZEN_MAPPING_SHA256 常量上方的载荷审核注释（与历次先例同写法）。"""

    counts: dict[str, int] = {}
    for change in plan.changes:
        name = change.path.rsplit("/", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    distribution = "、".join(f"{name} {count}" for name, count in sorted(counts.items()))
    # 同一个文件可能在多个叶子里出现，只记一次"旧摘要前缀 -> 新摘要前缀"，方便日后对账。
    moves = sorted({(change.path.rsplit("/", 1)[-1], short(change.old), short(change.new)) for change in plan.changes})
    snapshots = "；".join(f"{name} {old} 变为 {new}" for name, old, new in moves)
    return (
        f"# {date}（事实映射重绑）：{reason}；映射只重绑下列文件快照共 {len(plan.changes)} 个叶子（{distribution}），"
        "不改变测试、事实宇宙或规则语义。\n"
        f"# 载荷逐项审核：{plan.leaf_count} 个叶子里变化的 {len(plan.changes)} 处全是 sha256／test_file_sha256 叶子，非快照叶子 0 处变化，"
        "叶子键集与列表长度不变，\n"
        f"# 新值等于对应文件原始字节的现算 SHA-256（{snapshots}）；映射文件摘要 {short(plan.old_sha)} 变为 {short(plan.new_sha)}。\n"
    )


# 身份摘要类字段：它们变化意味着出站或评估读侧的身份变了，不是普通的"证据层重绑"。
IDENTITY_FIELDS = (
    "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "checker_sha256", "builder_sha256",
    "compare_reader_sha256", "accept_reader_sha256",
)


def diff_baselines(old: dict[str, Any] | None, new: dict[str, Any], layers: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """逐项对比两份身份守恒基线，返回（审核行, 警示行）。``layers`` 是受管文件相对路径 -> 层名（取不到就是空表）。"""

    lines: list[str] = []
    warnings: list[str] = []
    if old is None:
        lines.append("旧基线不存在（首次封存），无从逐项对比；新基线摘要见下。")
        return lines, warnings
    scalar_keys = [key for key in new if key not in ("entries", "closure_symbols", "layer_counts", "orchestrator_closure_sha256", "defaulted_paths")]
    lines.append("顶层字段：")
    for key in scalar_keys:
        if old.get(key) != new.get(key):
            lines.append(f"  变化 {key}: {short(str(old.get(key)), 12)} -> {short(str(new.get(key)), 12)}")
    unchanged = [key for key in scalar_keys if old.get(key) == new.get(key)]
    lines.append("  未变化：" + "、".join(unchanged))
    for key in ("orchestrator_closure_sha256", "layer_counts"):
        old_value, new_value = old.get(key, {}), new.get(key, {})
        for sub in sorted(set(old_value) | set(new_value)):
            if old_value.get(sub) != new_value.get(sub):
                lines.append(f"  变化 {key}.{sub}: {short(str(old_value.get(sub)), 12)} -> {short(str(new_value.get(sub)), 12)}")
    if old.get("defaulted_paths") != new.get("defaulted_paths"):
        lines.append(f"  变化 defaulted_paths: {old.get('defaulted_paths')} -> {new.get('defaulted_paths')}")
    old_symbols, new_symbols = old.get("closure_symbols", {}), new.get("closure_symbols", {})
    for group in sorted(set(old_symbols) | set(new_symbols)):
        for kind in sorted(set(old_symbols.get(group, {})) | set(new_symbols.get(group, {}))):
            before = set(old_symbols.get(group, {}).get(kind, ()))
            after = set(new_symbols.get(group, {}).get(kind, ()))
            if before != after:
                lines.append(f"  变化 closure_symbols.{group}.{kind}: 新增 {sorted(after - before)}、删除 {sorted(before - after)}")
    old_entries = {item["path"]: item["sha256"] for item in old.get("entries", ())}
    new_entries = {item["path"]: item["sha256"] for item in new.get("entries", ())}
    added = sorted(set(new_entries) - set(old_entries))
    removed = sorted(set(old_entries) - set(new_entries))
    modified = sorted(path for path in set(old_entries) & set(new_entries) if old_entries[path] != new_entries[path])
    lines.append(f"受管文件条目：共 {len(old_entries)} -> {len(new_entries)}；新增 {len(added)}、删除 {len(removed)}、修改 {len(modified)}")
    for tag, paths, source in (("新增", added, new_entries), ("删除", removed, old_entries), ("修改", modified, None)):
        for path in paths:
            layer = layers.get(path) or "层未知"
            if source is None:
                lines.append(f"  {tag} {path} [{layer}] {short(old_entries[path])} -> {short(new_entries[path])}")
            else:
                lines.append(f"  {tag} {path} [{layer}] {short(source[path])}")
    for key in IDENTITY_FIELDS:
        if key not in ("evidence_semantics_sha256", "control_sha256") and old.get(key) != new.get(key):
            warnings.append(f"!! 身份摘要 {key} 变化：出站或评估读侧身份改变，不是普通的证据层重绑，必须请调度者复核后才可采纳")
    for sub in ("wire_producer",):
        if old.get("orchestrator_closure_sha256", {}).get(sub) != new.get("orchestrator_closure_sha256", {}).get(sub):
            warnings.append("!! 编排器 wire 闭包摘要变化：出站身份改变，必须请调度者复核")
    if old.get("policy_sha256") != new.get("policy_sha256"):
        warnings.append("!! 策略摘要 policy_sha256 变化：层级归属规则被改过，需人工确认")
    return lines, warnings


def manifest_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    old_files = {item["path"]: item for item in (old or {}).get("files", ())}
    new_files = {item["path"]: item for item in new.get("files", ())}
    lines: list[str] = []
    for path in sorted(set(old_files) | set(new_files)):
        before, after = old_files.get(path), new_files.get(path)
        if before == after:
            continue
        if before is None:
            lines.append(f"  新增 {path} {short(after['sha256'])}（{after.get('bytes')} 字节）")
        elif after is None:
            lines.append(f"  删除 {path}")
        else:
            lines.append(f"  修改 {path} {short(before['sha256'])} -> {short(after['sha256'])}（{before.get('bytes')} -> {after.get('bytes')} 字节）")
    lines.append(f"  清单自摘要 {short(str((old or {}).get('manifest_sha256')))} -> {short(str(new.get('manifest_sha256')))}")
    return lines


def table_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> tuple[list[str], list[str]]:
    """依赖追踪表对比：返回（审核行, 新增的 unobserved 模块）。"""

    if old is None:
        return ["旧依赖表不存在，无从对比。"], sorted(new.get("unobserved", {}))
    old_names, new_names = old["files"], new["files"]
    old_modules, new_modules = old["modules"], new["modules"]
    lines = [
        f"generated_at_commit {old['generated_at_commit'][:12]} -> {new['generated_at_commit'][:12]}",
        f"追踪环境 {old.get('tracer', {}).get('python')} / {old.get('tracer', {}).get('platform')} -> "
        f"{new.get('tracer', {}).get('python')} / {new.get('tracer', {}).get('platform')}",
        f"文件表 {len(old_names)} -> {len(new_names)} 条；有记录的模块 {len(old_modules)} -> {len(new_modules)} 个",
    ]
    added = sorted(set(new_modules) - set(old_modules))
    gone = sorted(set(old_modules) - set(new_modules))
    if added:
        lines.append(f"  新增模块 {len(added)} 个：" + "、".join(added[:20]) + ("…" if len(added) > 20 else ""))
    if gone:
        lines.append(f"  消失模块 {len(gone)} 个：" + "、".join(gone[:20]) + ("…" if len(gone) > 20 else ""))
    changed: list[str] = []
    for module in sorted(set(old_modules) & set(new_modules)):
        old_row, new_row = old_modules[module], new_modules[module]
        old_sets = ({old_names[i] for i in old_row["opened"]}, {old_names[i] for i in old_row["listed"]})
        new_sets = ({new_names[i] for i in new_row["opened"]}, {new_names[i] for i in new_row["listed"]})
        if old_sets != new_sets:
            changed.append(f"  依赖集合变化 {module}: opened +{len(new_sets[0] - old_sets[0])}/-{len(old_sets[0] - new_sets[0])}、"
                           f"listed +{len(new_sets[1] - old_sets[1])}/-{len(old_sets[1] - new_sets[1])}")
    lines.append(f"依赖集合有变化的模块 {len(changed)} 个" + ("（前 15 个）：" if changed else ""))
    lines.extend(changed[:15])
    old_unobserved, new_unobserved = set(old.get("unobserved", {})), set(new.get("unobserved", {}))
    fresh = sorted(new_unobserved - old_unobserved)
    recovered = sorted(old_unobserved - new_unobserved)
    lines.append(f"unobserved（追踪失败、永远照跑）：{len(old_unobserved)} -> {len(new_unobserved)} 个；新增 {fresh or '无'}；恢复 {recovered or '无'}")
    for module in fresh:
        lines.append(f"  新增 unobserved {module}：{new['unobserved'][module]}")
    return lines, fresh


# ----------------------------------------------------------------------
# 后台脱离
# ----------------------------------------------------------------------


def detach(command: Sequence[str], log: Path, *, cwd: Path, env: dict[str, str]) -> int:
    """脱离会话在后台跑 ``command``，输出追加到 ``log``，结束时追加 ``EXIT=<退出码>``；返回后台任务的 pid。

    等价 ``setsid -f``（本机 macOS 没有 setsid 命令）：``start_new_session=True`` 让外层 bash 脱离当前会话与终端，
    它把真正的任务放进后台子 shell 后立刻退出（所以这里能 ``communicate`` 收尸，不会留下"仍在运行"的子进程对象），
    后台子 shell 与它的整条进程树继续活在新会话里，不随本进程或终端结束；``echo $!`` 把后台任务的 pid 交回来。
    子进程里把 SIGHUP 恢复为默认处理，避免从被 nohup 过的父进程继承"忽略 SIGHUP"（那会让依赖挂断检测的测试必红）。
    """

    quoted = shlex.quote(str(log))
    wrapped = f'( {shlex.join(list(command))} >> {quoted} 2>&1; echo "EXIT=$?" >> {quoted} ) < /dev/null > /dev/null 2>&1 & echo $!'
    process = subprocess.Popen(
        ["bash", "-c", wrapped], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True, text=True, preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
    )
    output, _ = process.communicate(timeout=30)
    return int(output.strip())


def exit_marker(log: Path) -> int | None:
    """日志末尾的 ``EXIT=<n>``：有就返回退出码，没有（还在跑或没起来）返回 None。"""

    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.findall(r"^EXIT=(-?\d+)\s*$", text, flags=re.M)
    return int(found[-1]) if found else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ----------------------------------------------------------------------
# 链本体
# ----------------------------------------------------------------------


class Chain:
    """一次重封链运行：持有仓库、日志目录、状态文件与命令行选项。"""

    def __init__(self, options: argparse.Namespace, passthrough: list[str]) -> None:
        self.options = options
        self.passthrough = passthrough
        self.repo = Path(options.repo).resolve()
        self.log_dir = Path(options.log_dir).resolve() if options.log_dir else None
        self.state: dict[str, Any] = {"steps": {}}
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        self.current_step = ""
        self.audit_index = 0
        self._lock_handle = None

    # -- 输出与命令 --------------------------------------------------------

    def say(self, message: str = "") -> None:
        print(message, flush=True)
        if self.log_dir is not None and self.log_dir.is_dir():
            with open(self.log_dir / CHAIN_LOG, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")

    def sh(self, args: Sequence[str], *, cwd: Path | None = None, check: bool = True, input_text: str | None = None,
           env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """跑一条命令：完整命令与输出追加到 commands.log；check 为真且非 0 退出时抛 StepFailed（带输出尾部）。"""

        result = subprocess.run(list(args), cwd=str(cwd or self.repo), env=env or self.env, input=input_text, capture_output=True, text=True)
        if self.log_dir is not None and self.log_dir.is_dir():
            with open(self.log_dir / COMMAND_LOG, "a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.datetime.now().isoformat(timespec='seconds')}] ({self.current_step or '-'}) $ "
                             f"{shlex.join(list(args))}\n--- rc={result.returncode}\n{result.stdout}")
                if result.stderr:
                    handle.write(f"--- stderr\n{result.stderr}")
        if check and result.returncode != 0:
            tail = (result.stderr.strip() or result.stdout.strip())[-TAIL_CHARS:]
            raise StepFailed(f"命令失败（rc={result.returncode}）：{shlex.join(list(args))}\n{tail}")
        return result

    def git(self, *args: str, check: bool = True) -> str:
        return self.sh(["git", *args], check=check).stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def python(self) -> str:
        return "python3" if shutil.which("python3", path=self.env.get("PATH")) else sys.executable

    def report(self, step: str, lines: list[str], warnings: list[str] | None = None) -> str:
        """输出并落盘一份载荷审核报告；返回报告文件路径。报告只给人看，没有任何自动签字语义。"""

        self.audit_index += 1
        kind = "现状预览（dry-run，未改任何文件）" if self.options.dry_run else "载荷审核报告"
        header = [f"======== {kind}：{step}（仅供人工核对；本工具不签字、不推送、不合并） ========"]
        body = header + lines + (warnings or []) + ["======== 报告结束 ========"]
        path = ""
        if self.log_dir is not None and self.log_dir.is_dir() and not self.options.dry_run:
            target = self.log_dir / f"audit-{STEP_NAMES.index(step) + 1:02d}-{step}.txt"
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
            path = str(target)
        for line in body:
            self.say(line)
        return path

    # -- 状态文件 -----------------------------------------------------------

    def load_state(self) -> None:
        path = self.log_dir / STATE_NAME
        if path.is_file():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            self.state.setdefault("steps", {})

    def save_state(self) -> None:
        path = self.log_dir / STATE_NAME
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def setting(self, name: str, default: str) -> str:
        """命令行值优先，其次状态里上次用过的值（续跑时收据标签等不能变），最后默认值。"""

        value = getattr(self.options, name, None)
        if value:
            return value
        return self.state.get("settings", {}).get(name) or default

    def dates(self) -> tuple[str, str]:
        """（YYYY-MM-DD, YYYYMMDD）：命令行 > 状态里首次记录的值 > 今天。续跑时日期不变，收据标签才稳定。"""

        iso = self.options.date_iso or self.state.get("settings", {}).get("date_iso") or datetime.date.today().isoformat()
        return iso, iso.replace("-", "")

    # -- 前置条件 -----------------------------------------------------------

    def in_progress_operation(self) -> str | None:
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir"))
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG"):
            if (git_dir / marker).exists():
                return marker
        return None

    def preflight(self, names: Sequence[str]) -> tuple[list[str], list[str]]:
        """返回（问题, 提醒）。问题不为空就不能开跑。"""

        problems: list[str] = []
        notes: list[str] = []
        top = self.sh(["git", "rev-parse", "--show-toplevel"], check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != self.repo:
            problems.append(f"--repo 必须是 git 工作树的根目录：{self.repo}")
            return problems, notes
        operation = self.in_progress_operation()
        if operation:
            problems.append(f"仓库处于合并／变基／拣选中途（{operation}），先处理完")
        status = self.git("status", "--porcelain", "--untracked-files=all")
        if status:
            problems.append("工作树不干净（重封链要求从干净状态开始，每步成功才提交）：\n    " + "\n    ".join(status.splitlines()[:12]))
        branch = self.sh(["git", "symbolic-ref", "--short", "-q", "HEAD"], check=False).stdout.strip()
        if not branch:
            problems.append("HEAD 是游离的（不在任何分支上）：重封链的提交必须落在一个工作分支上")
        elif branch in BRANCH_REFUSED and not self.options.allow_main:
            problems.append(f"当前分支是 {branch}：重封链不直接在主干上做，请在工作分支上做（确需放行加 --allow-main）")
        for relative in (Path(self.repo, "tools"),):
            for current, directories, _files in os.walk(relative):
                if "__pycache__" in directories:
                    problems.append(f"工作树里有 __pycache__：{Path(current, '__pycache__')}（门禁前置检查会因此退出 9，先清掉；脚本只用 PYTHONDONTWRITEBYTECODE=1 运行）")
                    break
        needed = {
            "rebind": [TRACE_FILE], "reseal": [CONSERVATION_FILE], "manifest": [DRIVER_INSTALL],
            "trace": [DEPENDENCY_TRACE, OWNERSHIP, DEPENDENCY_TABLE], "verify": [CONSERVATION_FILE],
            "rebind-receipt": [RECEIPT_DIR], "reseal-receipt": [RECEIPT_DIR],
        }
        for name in names:
            for relative in needed.get(name, ()):
                if not (self.repo / relative).exists():
                    problems.append(f"步骤 {name} 需要 {relative}，仓库里没有")
        if not self.options.dry_run and not self.options.ack_no_campaign:
            problems.append("真实运行必须加 --ack-no-campaign：确认当前没有在建的 Campaign（本链会改受管工具文件，在建 Campaign 会因工具身份变化作废）")
        if "trace" in names:
            for variable in ("CAPTURE_TYPESCRIPT_MODULE", "CODEX_SPEC_SOURCE_ROOT"):
                if not self.env.get(variable):
                    notes.append(f"环境变量 {variable} 未设置：依赖它的测试模块在追踪时可能失败而被记入 unobserved（10-30 的追踪是带着这两个变量跑的）")
            heavy = self.heavy_run()
            if heavy is None and not self.options.no_heavy_lock:
                problems.append("找不到重负载锁脚本 heavy-run.sh：用 --heavy-run 指定，或确认不需要锁时加 --no-heavy-lock")
        return problems, notes

    def heavy_run(self) -> Path | None:
        if self.options.heavy_run:
            path = Path(self.options.heavy_run)
            return path if path.is_file() else None
        common = Path(self.git("rev-parse", "--git-common-dir"))
        common = common if common.is_absolute() else (self.repo / common)
        candidate = common.resolve().parent / "local-analysis" / "opt-0165" / "heavy-run.sh"
        return candidate if candidate.is_file() else None

    # -- 事实映射辅助 ---------------------------------------------------------

    def mapping_path(self) -> str:
        text = (self.repo / TRACE_FILE).read_text(encoding="utf-8")
        found = MAPPING_PATH_RE.search(text)
        if not found:
            raise StepFailed(f"{TRACE_FILE} 里找不到 DEFAULT_MAPPING_RELATIVE_PATH 常量，不知道当前映射是哪一份")
        return found.group(1)

    def codex_version(self) -> str:
        if self.options.version:
            return self.options.version
        text = (self.repo / CONSERVATION_FILE).read_text(encoding="utf-8")
        found = re.search(r'^BASELINE_CODEX_VERSION = "([^"]+)"$', text, flags=re.M)
        if not found:
            raise StepFailed(f"{CONSERVATION_FILE} 里找不到 BASELINE_CODEX_VERSION，用 --version 指定")
        return found.group(1)

    @staticmethod
    def scope(version: str) -> str:
        parts = version.split(".")
        return f"codex-{parts[0]}{parts[1]}" if len(parts) >= 2 else f"codex-{version}"

    # -- 提交 -----------------------------------------------------------------

    def commit(self, paths: Sequence[str], subject: str, body: Sequence[str]) -> str:
        """固定格式提交：``<类型>(<范围>): <一句话>`` + 空行 + 正文 + 生成说明 + 可选 Co-Authored-By。只 add 给定的路径。"""

        self.sh(["git", "add", "--", *paths])
        staged = self.git("diff", "--cached", "--name-only").splitlines()
        extra = sorted(set(staged) - set(paths))
        if extra:
            raise StepFailed(f"暂存区里有不属于本步的文件，拒绝提交：{extra}")
        lines = [subject, ""] + list(body) + ["", "由 tools/ci/reseal_chain.py 生成提交；载荷审核报告需人工核对，未经自动签字。"]
        trailers = list(self.options.co_author or [])
        if trailers:
            lines.append("")
            lines.extend(f"Co-Authored-By: {item}" for item in trailers)
        self.sh(["git", "commit", "-q", "-F", "-"], input_text="\n".join(lines) + "\n")
        return self.head()

    # -- 步骤：事实映射重绑 ------------------------------------------------------

    def step_rebind(self) -> dict[str, Any]:
        map_rel = self.mapping_path()
        trace_text = (self.repo / TRACE_FILE).read_text(encoding="utf-8")
        frozen = FROZEN_RE.search(trace_text)
        if not frozen:
            raise StepFailed(f"{TRACE_FILE} 里找不到 FROZEN_MAPPING_SHA256 常量")
        plan = plan_rebind(self.repo, map_rel)
        lines = [f"映射文件：{map_rel}（{plan.leaf_count} 个叶子）"]
        if plan.missing:
            raise StepFailed("映射指向的文件不存在：\n    " + "\n    ".join(plan.missing))
        if frozen.group(1) != plan.old_sha:
            raise StepFailed(f"FROZEN_MAPPING_SHA256（{short(frozen.group(1))}）与重绑前映射文件摘要（{short(plan.old_sha)}）不符："
                             "映射被改过却没同步常量，先弄清谁改了它，本工具不在这种状态下重绑")
        if plan.non_snapshot or not plan.structure_same:
            raise StepFailed(f"除快照摘要外还有叶子或键集变化，拒绝继续：{plan.non_snapshot}")
        if not plan.changes:
            lines.append("没有需要重绑的叶子（每个被钉住文件的现算摘要都与映射一致），本步无变化。")
            self.report("rebind", lines)
            return {"status": "noop"}
        lines.append(f"需要重绑 {len(plan.changes)} 处（只改 sha256／test_file_sha256 叶子）：")
        lines += [f"  {item.label}  {item.path}  {short(item.old)} -> {short(item.new)}" for item in plan.changes]
        lines.append("非快照叶子变化：0 处；叶子键集与列表长度：不变")
        lines.append(f"映射文件摘要：{plan.old_sha} -> {plan.new_sha}")
        lines.append(f"FROZEN_MAPPING_SHA256：{frozen.group(1)} -> {plan.new_sha}")
        if self.options.dry_run:
            self.say("\n".join(["（计划）"] + lines))
            return {"status": "planned"}
        date = self.dates()[0]
        reason = self.setting("reason", "版本登记重封链")
        (self.repo / map_rel).write_bytes(plan.new_bytes)
        marker_count = trace_text.count(FROZEN_MARKER)
        if marker_count != 1:
            raise StepFailed(f"{TRACE_FILE} 里应恰有一处 `{FROZEN_MARKER.strip()}`，实际 {marker_count} 处")
        updated = trace_text.replace(frozen.group(1), plan.new_sha, 1)
        updated = updated.replace(FROZEN_MARKER, rebind_comment(date, reason, plan) + FROZEN_MARKER, 1)
        (self.repo / TRACE_FILE).write_text(updated, encoding="utf-8")
        lines.append(f"已改写：{map_rel}；{TRACE_FILE}（常量 + 载荷审核注释 3 行）")
        self.run_unittests(REBIND_TEST_MODULES, lines)
        files = sorted({item.path.rsplit('/', 1)[-1] for item in plan.changes})
        version = self.codex_version()
        commit = self.commit(
            [map_rel, TRACE_FILE],
            f"chore({self.scope(version)}): 事实映射重绑 {len(plan.changes)} 个 sha256 叶子（{'、'.join(files)}）",
            [reason, f"载荷审核：{plan.leaf_count} 个叶子里只有 {len(plan.changes)} 个 sha256／test_file_sha256 叶子变化，非快照叶子 0 处变化，"
                     "叶子键集与列表长度不变，新值等于对应文件原始字节的现算摘要；",
             f"映射文件摘要 {short(plan.old_sha)} 变为 {short(plan.new_sha)}，FROZEN_MAPPING_SHA256 同步。"],
        )
        self.report("rebind", lines)
        return {"status": "done", "commit": commit}

    def run_unittests(self, modules: Sequence[str], lines: list[str]) -> None:
        for module in modules:
            result = self.sh([self.python(), "-B", "-m", "unittest", module], check=False)
            summary = [line for line in result.stderr.strip().splitlines() if line.strip() and not set(line.strip()) <= {"-"}]
            if result.returncode != 0:
                tail = "\n".join(summary[-12:])
                raise StepFailed(f"测试未通过：{module}（rc={result.returncode}）\n{tail}")
            lines.append(f"测试通过：{module}（{' '.join(summary[-3:]).strip()}）")

    # -- 步骤：冻结承接收据 --------------------------------------------------------

    def last_receipt_commit(self) -> str:
        """HEAD 历史里最近一次新增冻结承接收据的提交（下一份收据的 --before）。"""

        if self.options.receipt_before:
            return self.git("rev-parse", "--verify", f"{self.options.receipt_before}^{{commit}}")
        found = self.git("log", "-1", "--format=%H", "--diff-filter=A", "--", RECEIPT_PATHSPEC)
        if not HEX40.fullmatch(found):
            raise StepFailed("HEAD 历史里找不到任何冻结承接收据，无法确定 --before；用 --receipt-before 指定")
        return found

    def generate_receipt(self, step: str, kind: str, what: str, dry: bool) -> dict[str, Any]:
        before = self.last_receipt_commit()
        after = self.head()
        label = self.setting("label", "reseal-chain")
        date = self.dates()[1]
        reason = self.setting("reason", "版本登记重封链")
        tag = f"{label}-{kind}-{date}"
        output = self.repo / RECEIPT_DIR / f"upstream-{tag}-freeze-successor.json"
        base = [self.python(), "-B", "-m", "tools.upstream_merge", "freeze-successor-generate", "--repository", str(self.repo),
                "--before", before, "--after", after, "--tag", tag, "--reason", f"{reason}（{what}）"]
        lines = [f"收据范围：--before {before}（上一份收据所在提交）", f"            --after  {after}（当前 HEAD）", f"收据标签：{tag}"]
        if before == after:
            # 上一份收据就在 HEAD 上：区间为空，生成器也会拒绝"前后相同"。dry-run 时前面的步骤还没提交，会走到这里；真实运行里说明前一步没产生提交。
            lines.append("HEAD 就是上一份收据所在提交，区间为空，没有需要承接的转移，本步无变化。")
            if dry:
                lines.append("（dry-run 只能按现状预览：前面的步骤还没提交；真实运行时区间以前一步的提交为终点。）")
            self.report(step, lines)
            return {"status": "noop"}
        if output.exists() and not dry:
            raise StepFailed(f"收据文件已存在，拒绝覆盖：{output}（换 --label 或 --date）")
        preview = self.sh([*base, "--dry-run"], check=False)
        if preview.returncode != 0:
            raise StepFailed("冻结承接收据干跑失败（常见原因：区间里已有收据在变化、按主干起算的自引用风险、冻结路径缺人工动作）：\n"
                             + (preview.stderr.strip() or preview.stdout.strip())[-TAIL_CHARS:])
        try:
            document = json.loads(preview.stdout)
        except ValueError as error:
            raise StepFailed(f"收据干跑输出不是 JSON：{error}") from error
        transitions = document.get("transitions", [])
        lines += [f"干跑结果：result={document.get('result')}；变更路径 {document.get('changed_path_count')}、冻结路径 {document.get('frozen_path_count')}、"
                  f"转移 {len(transitions)} 条、台账外未登记 {document.get('unregistered_path_count', len(document.get('unregistered_paths', [])))} 个、"
                  f"人工动作 {len(document.get('required_manual_actions', []))} 个"]
        if not transitions:
            lines.append("区间内没有冻结路径的转移，不需要收据，本步无变化。")
            self.report(step, lines)
            return {"status": "noop"}
        for item in transitions:
            lines.append(f"  转移 {item.get('path')}  前驱摘要 {', '.join(short(x) for x in item.get('predecessor_sha256s', []))}")
        for path in document.get("unregistered_paths", []):
            lines.append(f"  台账外未登记路径（收据不覆盖，需知晓）：{path}")
        if document.get("required_manual_actions"):
            raise StepFailed("收据要求人工动作，链不替你做：\n" + json.dumps(document["required_manual_actions"], ensure_ascii=False, indent=2)[:TAIL_CHARS])
        if document.get("result") != ACCEPTED_RECEIPT_RESULT:
            raise StepFailed(f"收据干跑 result={document.get('result')}，不是 {ACCEPTED_RECEIPT_RESULT}")
        if dry:
            self.say("\n".join(["（计划）"] + lines))
            return {"status": "planned"}
        ledger = self.log_dir / "timing-ledger.jsonl"
        self.sh([*base, "--output", str(output), "--timing-ledger", str(ledger)])
        written = json.loads(output.read_text(encoding="utf-8"))
        if written.get("result") != ACCEPTED_RECEIPT_RESULT or written.get("required_manual_actions"):
            raise StepFailed(f"生成的收据 result={written.get('result')}、人工动作 {written.get('required_manual_actions')}，拒绝提交")
        lines.append(f"收据已生成：{output.relative_to(self.repo)}（result={written['result']}，identity_sha256={short(written.get('identity_sha256', ''), 12)}）")
        lines.append("收据声明的待跑验证（本工具不跑，交由人安排）：" + "；".join(written.get("verification", [])))
        commit = self.commit(
            [str(output.relative_to(self.repo))],
            f"chore(egress): 登记{what}的冻结承接收据（{label}）",
            [f"freeze-successor-generate 生成（--before 取上一份收据所在提交 {before[:12]}，--after 取 {after[:12]}），",
             f"转移 {len(transitions)} 条，台账外未登记路径 {len(document.get('unregistered_paths', []))} 个，人工动作 0 个。"],
        )
        self.report(step, lines)
        return {"status": "done", "commit": commit}

    def step_rebind_receipt(self) -> dict[str, Any]:
        return self.generate_receipt("rebind-receipt", "factmap", "事实映射重绑", self.options.dry_run)

    def step_reseal_receipt(self) -> dict[str, Any]:
        return self.generate_receipt("reseal-receipt", "reseal", "身份守恒基线重封", self.options.dry_run)

    # -- 步骤：身份守恒基线重封 ------------------------------------------------------

    def classify_layers(self, paths: Sequence[str]) -> dict[str, str | None]:
        """用仓库自己的层级策略给受管文件归层（取不到就返回空表，审核报告里标"层未知"）。"""

        code = (
            "import json, sys\nfrom pathlib import Path\n"
            "from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip\n"
            "policy = tip.load_policy(Path('tools/official_client_capture') / tip.POLICY_FILENAME)\n"
            "print(json.dumps({p: tip.classify_path(policy, p) for p in json.load(sys.stdin)}))\n"
        )
        result = self.sh([self.python(), "-B", "-c", code], check=False, input_text=json.dumps(list(paths)))
        try:
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except ValueError:
            return {}

    def step_reseal(self) -> dict[str, Any]:
        version = self.codex_version()
        underscore = version.replace(".", "_")
        relative = f"{BASELINE_DIR}/codex_identity_conservation_baseline_{underscore}.json"
        lines = [f"目标版本：{version}；基线文件：{relative}"]
        probe = self.sh([self.python(), "-B", "-m", "unittest", CONSERVATION_MODULE], check=False)
        if probe.returncode == 0 and not self.options.force_reseal:
            lines.append("身份守恒模块全绿，当前基线仍然成立，无需重封（--force-reseal 可强制重封）。")
            if self.options.dry_run:
                lines.append("（dry-run 只能按现状判断：rebind 的改动还没发生；真实运行时在 rebind 之后重新判定。）")
            self.report("reseal", lines)
            return {"status": "noop"}
        failing = [line for line in probe.stderr.splitlines() if line.startswith(("FAIL:", "ERROR:"))]
        lines.append("身份守恒模块现状：" + (f"红（{len(failing)} 项：" + "；".join(f.split(' (')[0] for f in failing[:6]) + "）" if failing else "绿（强制重封）"))
        if self.options.dry_run:
            self.say("\n".join(["（计划）"] + lines + ["将执行：python3 -B -m <守恒模块> --seal-baseline <文件> --replace，回填 BASELINE_SHA256，清空 REGISTERED_CONTROL_CHANGES"]))
            return {"status": "planned"}
        target = self.repo / relative
        old_document = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
        sealed = [self.python(), "-B", "-m", CONSERVATION_MODULE, "--seal-baseline", relative] + (["--replace"] if target.exists() else [])
        result = self.sh(sealed)
        digest = re.search(r"文件摘要（填入 BASELINE_SHA256）= ([0-9a-f]{64})", result.stdout)
        source = re.search(r"source_commit = ([0-9a-f]{40})", result.stdout)
        defaulted = re.search(r"默认归层 (\d+)", result.stdout)
        if not digest or not source or not defaulted:
            raise StepFailed("没能从封存输出里读出文件摘要／source_commit／默认归层数：\n" + result.stdout[-TAIL_CHARS:])
        if defaulted.group(1) != "0":
            raise StepFailed(f"默认归层不为 0（{defaulted.group(1)}）：有受管文件没有归层，先在策略里登记层级再封存")
        if source.group(1) != self.head():
            raise StepFailed(f"封存的 source_commit（{source.group(1)[:12]}）不是当前 HEAD（{self.head()[:12]}）")
        text = (self.repo / CONSERVATION_FILE).read_text(encoding="utf-8")
        removed = self.registered_control_changes(text)
        for pattern, replacement in (
            (r'^BASELINE_CODEX_VERSION = "[^"]*"$', f'BASELINE_CODEX_VERSION = "{version}"'),
            (r'(^BASELINE_PATH = Path\(__file__\)\.resolve\(\)\.parent / "fixtures" / )"[^"]*"$',
             rf'\1"codex_identity_conservation_baseline_{underscore}.json"'),
            (r'^BASELINE_SHA256 = "[0-9a-f]{64}"$', f'BASELINE_SHA256 = "{digest.group(1)}"'),
        ):
            text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
            if count != 1:
                raise StepFailed(f"没能在 {CONSERVATION_FILE} 里定位常量：{pattern}")
        text = self.clear_registered_control_changes(text)
        (self.repo / CONSERVATION_FILE).write_text(text, encoding="utf-8")
        lines.append(f"已封存：source_commit={source.group(1)}，文件摘要 {digest.group(1)}")
        lines.append(f"已回填：BASELINE_CODEX_VERSION={version}、BASELINE_PATH={Path(relative).name}、BASELINE_SHA256={short(digest.group(1), 12)}")
        lines.append("REGISTERED_CONTROL_CHANGES：" + (f"已清空，原有 {len(removed)} 项登记被移除：{removed}" if removed else "本来为空，保持为空"))
        self.run_unittests((CONSERVATION_MODULE,), lines)
        new_document = json.loads(target.read_text(encoding="utf-8"))
        paths = sorted({item["path"] for item in new_document["entries"]})
        diff_lines, warnings = diff_baselines(old_document, new_document, self.classify_layers(paths))
        lines += ["基线逐项对比（旧 -> 新）："] + ["  " + item for item in diff_lines]
        commit = self.commit(
            [relative, CONSERVATION_FILE],
            f"chore({self.scope(version)}): 重封 {version} 身份守恒基线（{self.setting('label', 'reseal-chain')}）",
            [f"在 HEAD（封存 source_commit 为 {source.group(1)[:10]}）上重封；BASELINE_SHA256 回填 {short(digest.group(1), 12)}，REGISTERED_CONTROL_CHANGES 置空。",
             "载荷审核（相对旧基线）：" + (f"{len(diff_lines)} 行对比，见审核报告" if old_document else "首次封存")]
            + (["警示：" + "；".join(w.lstrip('! ') for w in warnings)] if warnings else []),
        )
        self.report("reseal", lines, warnings)
        return {"status": "done", "commit": commit}

    @staticmethod
    def _control_changes_span(text: str) -> tuple[int, int] | None:
        """REGISTERED_CONTROL_CHANGES 赋值语句的（起始行, 结束行），行号从 1 起；找不到返回 None。"""

        import ast

        tree = ast.parse(text)
        for node in tree.body:
            target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == "REGISTERED_CONTROL_CHANGES":
                return node.lineno, node.end_lineno or node.lineno
        return None

    def registered_control_changes(self, text: str) -> list[str]:
        import ast

        span = self._control_changes_span(text)
        if span is None:
            raise StepFailed(f"{CONSERVATION_FILE} 里找不到 REGISTERED_CONTROL_CHANGES")
        node = next(item for item in ast.parse(text).body if getattr(item, "lineno", 0) == span[0])
        try:
            value = ast.literal_eval(node.value)
        except ValueError as error:
            raise StepFailed(f"REGISTERED_CONTROL_CHANGES 不是字面量字典，无法安全清空：{error}") from error
        return sorted(value)

    def clear_registered_control_changes(self, text: str) -> str:
        span = self._control_changes_span(text)
        assert span is not None
        lines = text.splitlines(keepends=True)
        lines[span[0] - 1:span[1]] = ["REGISTERED_CONTROL_CHANGES: dict[str, str] = {}\n"]
        return "".join(lines)

    # -- 步骤：驱动 manifest ----------------------------------------------------------

    def step_manifest(self) -> dict[str, Any]:
        lines = [f"驱动目录：{DRIVER_ROOT}"]
        command = [self.python(), "-B", DRIVER_INSTALL, "build-manifest", "--root", DRIVER_ROOT]
        check = self.sh([*command, "--check"], check=False)
        if check.returncode == 0:
            lines.append("manifest.json 与目录内容一致（build-manifest --check 为 unchanged），无需重建。")
            self.report("manifest", lines)
            return {"status": "noop"}
        lines.append("build-manifest --check 不一致：" + (check.stderr.strip() or check.stdout.strip())[-300:])
        if self.options.dry_run:
            self.say("\n".join(["（计划）"] + lines + ["将执行 build-manifest 重建并提交"]))
            return {"status": "planned"}
        manifest_rel = f"{DRIVER_ROOT}/manifest.json"
        old = json.loads(self.git("show", f"HEAD:{manifest_rel}")) if self.sh(["git", "cat-file", "-e", f"HEAD:{manifest_rel}"], check=False).returncode == 0 else None
        self.sh(command)
        recheck = self.sh([*command, "--check"], check=False)
        if recheck.returncode != 0:
            raise StepFailed("重建后 build-manifest --check 仍不一致：" + (recheck.stderr.strip() or recheck.stdout.strip())[-400:])
        new = json.loads((self.repo / manifest_rel).read_text(encoding="utf-8"))
        diff = manifest_diff(old, new)
        lines += ["清单逐项对比（旧 -> 新）："] + diff
        names = [item.split()[1].rsplit("/", 1)[-1] for item in diff if item.startswith("  ") and "清单自摘要" not in item]
        commit = self.commit(
            [manifest_rel],
            f"chore(driver): 重建驱动 manifest（{'、'.join(names) if names else '清单自摘要'}）",
            [f"只有 {len(names)} 个文件的摘要或大小变化，另有清单自摘要；build-manifest --check 为 unchanged。"],
        )
        self.report("manifest", lines)
        return {"status": "done", "commit": commit}

    # -- 步骤：重新追踪依赖表 -----------------------------------------------------------

    def trace_environment(self) -> tuple[str, str]:
        """追踪表里记录的 Python 版本与平台说明：Python 取 PATH 里 python3 的实际版本，平台沿用现有追踪表的说明（同一台机器），可用参数覆盖。"""

        version = self.options.python_version
        if not version:
            result = self.sh([self.python(), "-c", "import platform; print(platform.python_version())"])
            version = result.stdout.strip()
        platform_text = self.options.platform
        if not platform_text:
            try:
                platform_text = json.loads((self.repo / DEPENDENCY_TABLE).read_text(encoding="utf-8")).get("tracer", {}).get("platform", "")
            except (OSError, ValueError):
                platform_text = ""
        if not platform_text:
            import platform as platform_module
            platform_text = f"{platform_module.system()} {platform_module.release()} {platform_module.machine()}（本机）"
        return version, platform_text

    def step_trace(self) -> dict[str, Any]:
        head = self.head()
        version, platform_text = self.trace_environment()
        heavy = self.heavy_run()
        trace_dir = self.log_dir / "trace"
        results = trace_dir / "results"
        tree = trace_dir / "tree"
        job_log = trace_dir / "job.log"
        meta_path = trace_dir / "job.json"
        lines = [f"追踪提交（generated_at_commit）：{head}", f"追踪环境：Python {version}；平台说明 {platform_text}；--jobs {self.options.jobs}",
                 "重负载锁：" + (str(heavy) if heavy else "不使用（--no-heavy-lock）"), f"追踪工作树（detached）：{tree}；结果目录：{results}"]
        if self.options.dry_run:
            self.say("\n".join(["（计划）"] + lines + ["将执行：detached 工作树 -> 后台追踪全部模块（约 20 分钟量级）-> dependency-table 汇总 -> 提交依赖表"]))
            return {"status": "planned"}
        if heavy is None and not self.options.no_heavy_lock:
            raise StepFailed("找不到重负载锁脚本 heavy-run.sh：用 --heavy-run 指定，或确认不需要锁时加 --no-heavy-lock")
        trace_dir.mkdir(parents=True, exist_ok=True)
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else None
        reuse = False
        if meta and meta.get("head") == head:
            code = exit_marker(job_log)
            if code == 0:
                self.say("发现同一提交上已经追踪完成的任务，直接使用其结果（不重追）。")
                reuse = True
            elif code is None and pid_alive(int(meta.get("pid", 0))):
                self.say(f"发现同一提交上仍在运行的追踪任务（pid {meta['pid']}），接着等待。")
                reuse = True
        if not reuse:
            # 追踪要花二十分钟量级：开跑前先确认前面几步的产物确实让三组测试和 manifest 都是绿的（红的模块会被记进 unobserved，白追一场）。
            self.say("追踪前复核：三组测试 + manifest 校验……")
            self.pretrace_check()
            self.cleanup_worktree(tree)
            if results.exists():
                shutil.rmtree(results, ignore_errors=True)
            if job_log.exists():
                job_log.unlink()
            results.mkdir(parents=True)
            self.sh(["git", "worktree", "add", "-q", "--detach", str(tree), head])
            command = [self.python(), "-B", str(tree / DEPENDENCY_TRACE), "--repo", str(tree), "--out", str(results), "--jobs", str(self.options.jobs)]
            if heavy is not None:
                command = ["bash", str(heavy), *command]
            pid = detach(command, job_log, cwd=tree, env=self.env)
            meta_path.write_text(json.dumps({"head": head, "pid": pid, "command": command, "started": time.time()}, ensure_ascii=False) + "\n", encoding="utf-8")
            self.say(f"追踪已在后台启动（pid {pid}），日志 {job_log}；本进程在此等待，期间中断不影响追踪，之后用 --from trace 接上。")
            time.sleep(min(3.0, self.options.poll_seconds))
            if not job_log.exists() and not pid_alive(pid):
                raise StepFailed(f"追踪进程没有起来（没有日志、进程已退出）：{shlex.join(command)}")
        try:
            self.wait_for_trace(job_log, results, meta_path)
            code = exit_marker(job_log)
            if code != 0:
                raise StepFailed(f"追踪任务以 EXIT={code} 结束，日志 {job_log}（结果目录保留在 {results}）")
        except StepFailed:
            self.cleanup_worktree(tree)
            raise
        if not tree.is_dir():
            self.sh(["git", "worktree", "add", "-q", "--detach", str(tree), head])
        try:
            old_table = json.loads(self.git("show", f"HEAD:{DEPENDENCY_TABLE}")) if self.sh(["git", "cat-file", "-e", f"HEAD:{DEPENDENCY_TABLE}"], check=False).returncode == 0 else None
            self.sh([self.python(), "-B", str(tree / OWNERSHIP), "dependency-table", "--tree", str(tree), "--trace-dir", str(results),
                     "--python", version, "--platform", platform_text, "--output", str(self.repo / DEPENDENCY_TABLE)])
        finally:
            self.cleanup_worktree(tree)
        new_table = json.loads((self.repo / DEPENDENCY_TABLE).read_text(encoding="utf-8"))
        if new_table.get("generated_at_commit") != head:
            raise StepFailed(f"新依赖表的 generated_at_commit（{new_table.get('generated_at_commit')}）不是追踪提交 {head}")
        count = len(list(results.glob("*/_result.json")))
        diff_lines, fresh = table_diff(old_table, new_table)
        lines += [f"追踪结果：{count} 个模块有 _result.json；表里有记录 {len(new_table['modules'])} 个，unobserved {len(new_table['unobserved'])} 个"]
        lines += ["依赖表逐项对比（旧 -> 新）："] + ["  " + item for item in diff_lines]
        warnings: list[str] = []
        if fresh and not self.options.allow_new_unobserved:
            self.report("trace", lines, [f"!! 新增 {len(fresh)} 个追踪失败的模块（追踪时测试没过，选择精度会变差）：{fresh}"])
            raise StepFailed(f"新增 {len(fresh)} 个 unobserved 模块 {fresh}：先弄清它们为什么在追踪提交上失败；确认无碍可加 --allow-new-unobserved 再 --from trace（追踪结果会复用）")
        commit = self.commit(
            [DEPENDENCY_TABLE],
            f"chore(ci): 重新追踪依赖表（追踪提交 {head[:10]}，{len(new_table['modules'])} 个模块，未观察到 {len(new_table['unobserved'])} 个）",
            [f"generated_at_commit={head[:12]}（追踪提交是之后所有比对基点的祖先），追踪环境 Python {version}、{platform_text}。",
             f"相对旧表：依赖集合有变化的模块见审核报告，新增 unobserved {len(fresh)} 个。"],
        )
        self.report("trace", lines, warnings)
        return {"status": "done", "commit": commit, "traced": head}

    def pretrace_check(self) -> None:
        self.run_unittests(VERIFY_MODULES, [])
        check = self.sh([self.python(), "-B", DRIVER_INSTALL, "build-manifest", "--root", DRIVER_ROOT, "--check"], check=False)
        if check.returncode != 0:
            raise StepFailed("追踪前复核：驱动 manifest 与目录不一致，先跑 manifest 步骤：" + (check.stderr.strip() or check.stdout.strip())[-300:])

    def cleanup_worktree(self, tree: Path) -> None:
        """移除追踪用的 detached 工作树。只动自己登记的这一棵：不跑全局 ``git worktree prune``，免得误清别人暂时不在位的工作树登记。"""

        listing = self.sh(["git", "worktree", "list", "--porcelain"], check=False).stdout
        registered = any(line == f"worktree {tree}" or line == f"worktree {tree.resolve()}" for line in listing.splitlines())
        if registered:
            self.sh(["git", "worktree", "remove", "--force", str(tree)], check=False)
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)
            if registered:
                self.sh(["git", "worktree", "prune"], check=False)

    def wait_for_trace(self, job_log: Path, results: Path, meta_path: Path) -> None:
        last_beat = time.time()
        pid = int(json.loads(meta_path.read_text(encoding="utf-8")).get("pid", 0))
        while exit_marker(job_log) is None:
            if not pid_alive(pid):
                time.sleep(1.0)
                if exit_marker(job_log) is None:
                    raise StepFailed(f"追踪进程 {pid} 已退出但日志里没有 EXIT= 结尾（被杀？）：{job_log}")
                break
            time.sleep(self.options.poll_seconds)
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                last_beat = time.time()
                self.say(f"  …追踪进行中：已有 {len(list(results.glob('*/_result.json')))} 个模块完成")

    # -- 步骤：只读复核 ---------------------------------------------------------------

    def step_verify(self) -> dict[str, Any]:
        lines = ["复核内容：映射／候选追踪脚本／身份守恒三组测试 + 驱动 manifest 校验（只读，不提交）"]
        if self.options.dry_run:
            self.say("\n".join(["（计划）"] + lines))
            return {"status": "planned"}
        self.run_unittests(VERIFY_MODULES, lines)
        check = self.sh([self.python(), "-B", DRIVER_INSTALL, "build-manifest", "--root", DRIVER_ROOT, "--check"], check=False)
        if check.returncode != 0:
            raise StepFailed("驱动 manifest 校验未通过：" + (check.stderr.strip() or check.stdout.strip())[-400:])
        lines.append("驱动 manifest：build-manifest --check 为 unchanged")
        self.report("verify", lines)
        return {"status": "done"}

    # -- 调度 -------------------------------------------------------------------------

    def select_steps(self) -> list[str]:
        for flag in (self.options.from_step, self.options.only):
            if flag and flag not in STEP_NAMES:
                raise ChainError(f"未知步骤 {flag}；可选：{'、'.join(STEP_NAMES)}")
        if self.options.from_step and self.options.only:
            raise ChainError("--from 与 --only 不能同时用")
        if self.options.only:
            return [self.options.only]
        if self.options.from_step:
            return list(STEP_NAMES[STEP_NAMES.index(self.options.from_step):])
        return list(STEP_NAMES)

    def check_resume(self, selected: list[str]) -> list[str]:
        """续跑前断言 HEAD 与状态记录一致；返回要提示的警告。"""

        notes: list[str] = []
        done = self.state.get("steps", {})
        first_index = STEP_NAMES.index(selected[0])
        if first_index == 0 and not self.options.only and not self.options.from_step and done and not self.options.dry_run:
            raise ChainError(f"--log-dir 里已有上次运行的状态（{', '.join(f'{k}={v.get('status')}' for k, v in done.items())}）："
                             "续跑用 --from <步骤>，要重新开始请换一个新的 --log-dir")
        previous = [name for name in STEP_NAMES[:first_index] if done.get(name, {}).get("status") in ("done", "noop")]
        missing = [name for name in STEP_NAMES[:first_index] if name not in previous]
        if previous:
            recorded = done[previous[-1]].get("head_after")
            head = self.head()
            if recorded and recorded != head:
                ancestor = self.sh(["git", "merge-base", "--is-ancestor", recorded, head], check=False).returncode == 0
                if not ancestor:
                    raise ChainError(f"HEAD（{head[:12]}）不是上一步 {previous[-1]} 完成时的提交（{recorded[:12]}）的后代：状态与仓库对不上，"
                                     "确认没有人动过分支再重来")
                if not self.options.accept_head:
                    count = self.git("rev-list", "--count", f"{recorded}..{head}")
                    raise ChainError(f"HEAD 比上一步 {previous[-1]} 完成时（{recorded[:12]}）多了 {count} 个提交（可能是你手工修好后提交的）：确认无误后加 --accept-head")
        if missing and first_index > 0:
            notes.append(f"状态里没有这些前置步骤的完成记录：{'、'.join(missing)}（--from／--only 直接从 {selected[0]} 开始，请自行确认前面已经做过）")
        return notes

    def describe(self, selected: list[str]) -> None:
        self.say("重封链计划：")
        for number, name in enumerate(STEP_NAMES, 1):
            description = dict(STEPS)[name]
            self.say(f"  {number}. {name:<15}{'<- 本次运行' if name in selected else '   （本次不跑）'}  {description}")

    def resume_command(self, step: str) -> str:
        base = [item for item in self.passthrough]
        cleaned: list[str] = []
        skip = False
        for item in base:
            if skip:
                skip = False
                continue
            if item in ("--from", "--only"):
                skip = True
                continue
            if item.startswith(("--from=", "--only=")) or item in ("--dry-run", "--accept-head"):
                continue
            cleaned.append(item)
        return "python3 -B tools/ci/reseal_chain.py " + shlex.join(cleaned) + f" --from {step}"

    def run(self) -> int:
        if self.log_dir is None:
            raise ChainError("需要 --log-dir（放在工作树之外的日志目录）")
        if str(self.log_dir).startswith(str(self.repo) + os.sep):
            raise ChainError(f"--log-dir 不能放在工作树里面（会弄脏工作树）：{self.log_dir}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock_handle = open(self.log_dir / "chain.lock", "w")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ChainError(f"另一个重封链进程正在使用这个 --log-dir：{self.log_dir}") from error
        self.load_state()
        selected = self.select_steps()
        problems, notes = self.preflight(selected)
        if not problems:
            notes += self.check_resume(selected)
        self.describe(selected)
        self.say(f"仓库：{self.repo}；分支：{self.git('symbolic-ref', '--short', '-q', 'HEAD', check=False) or '(游离)'}；HEAD：{self.git('rev-parse', 'HEAD', check=False)[:12]}")
        for note in notes:
            self.say(f"提醒：{note}")
        if problems:
            self.say("前置检查未通过：")
            for problem in problems:
                self.say(f"  - {problem}")
            return 2
        self.say("前置检查通过。")
        if self.options.dry_run:
            self.say("（--dry-run：以下是各步骤按当前仓库现状算出的计划，不改任何文件、不提交）")
        else:
            settings = self.state.setdefault("settings", {})
            for name in ("label", "reason"):
                if getattr(self.options, name, None):
                    settings[name] = getattr(self.options, name)
            settings.setdefault("date_iso", self.dates()[0])
            self.state.setdefault("repo", str(self.repo))
            self.state.setdefault("base_head", self.head())
            self.save_state()
        failures = 0
        for name in selected:
            self.say("")
            self.say(f"-------- 步骤 {STEP_NAMES.index(name) + 1}/{len(STEP_NAMES)}：{name} — {dict(STEPS)[name]} --------")
            head_before = self.head()
            if not self.options.dry_run and self.git("status", "--porcelain", "--untracked-files=all"):
                raise ChainError(f"步骤 {name} 开始前工作树不干净")
            self.current_step = name
            try:
                result = getattr(self, "step_" + name.replace("-", "_"))()
            except StepFailed as error:
                failures += 1
                self.fail_step(name, head_before, str(error))
                break
            except KeyboardInterrupt:
                self.say(f"\n被中断。HEAD 停在 {self.head()[:12]}；若中断时正在 {name} 且已有改动，先看 git status。续接：{self.resume_command(name)}")
                return 130
            if not self.options.dry_run:
                self.state["steps"][name] = {"status": result["status"], "head_after": self.head(), "commit": result.get("commit"),
                                             "finished": datetime.datetime.now().isoformat(timespec="seconds")}
                self.save_state()
        self.current_step = ""
        if failures:
            return 1
        self.summary(selected)
        return 0

    def fail_step(self, name: str, head_before: str, reason: str) -> None:
        """失败：保存现场 diff、把工作树还原到步骤开始前，状态记失败，打印续跑命令。"""

        if self.options.dry_run:
            self.say("")
            self.say(f"✗ 步骤 {name} 在计划阶段就不满足：{reason}")
            return
        diff_path = self.log_dir / f"failed-{name}.diff"
        diff_text = self.git("diff", "HEAD", check=False)
        untracked = self.git("ls-files", "--others", "--exclude-standard", check=False)
        diff_path.write_text(diff_text + ("\n\n# 未跟踪文件：\n" + untracked + "\n" if untracked else "\n"), encoding="utf-8")
        self.sh(["git", "reset", "-q", "--hard", "HEAD"], check=False)
        self.sh(["git", "clean", "-fdq"], check=False)
        moved = self.head() != head_before
        self.state["steps"][name] = {"status": "failed", "head_after": self.head(), "reason": reason[:2000]}
        if not self.options.dry_run:
            self.save_state()
        self.say("")
        self.say(f"✗ 步骤 {name} 失败：{reason}")
        self.say(f"现场：HEAD 停在 {self.head()[:12]}{'（注意：HEAD 在本步中移动过）' if moved else ''}，工作树已还原干净；失败时的改动存于 {diff_path}；"
                 f"命令与输出见 {self.log_dir / COMMAND_LOG}")
        self.say(f"处理后从这一步继续：{self.resume_command(name)}")

    def summary(self, selected: list[str]) -> None:
        self.say("")
        if self.options.dry_run:
            self.say("======== dry-run 结束：只打印了计划与现状，没有改任何文件、没有提交 ========")
            return
        self.say("======== 重封链完成 ========")
        base = self.state.get("base_head")
        if base and not self.options.dry_run:
            self.say(f"本链提交（{base[:12]}..HEAD）：")
            self.say(self.git("log", "--oneline", f"{base}..HEAD"))
        for name in selected:
            record = self.state["steps"].get(name, {})
            if record:
                self.say(f"  {name:<15}{record.get('status')}" + (f"  提交 {record['commit'][:10]}" if record.get("commit") else ""))
        self.say("本工具没有签字、没有推送。待人工：逐份核对上面的载荷审核报告（审核文件在 --log-dir/audit-*.txt）；之后由你决定何时跑门禁、是否推送。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="要处理的 git 工作树根目录（默认当前目录）")
    parser.add_argument("--log-dir", help="日志、状态、审核报告与追踪结果的目录，必须在工作树之外")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划与前置检查，不改任何文件、不提交")
    parser.add_argument("--from", dest="from_step", metavar="步骤", help="从这一步开始往后跑（失败后续跑）：" + "、".join(STEP_NAMES))
    parser.add_argument("--only", metavar="步骤", help="只跑这一步")
    parser.add_argument("--ack-no-campaign", action="store_true", help="确认当前没有在建的 Campaign（真实运行必需）")
    parser.add_argument("--allow-main", action="store_true", help="允许在 main／master 分支上提交（默认拒绝）")
    parser.add_argument("--accept-head", action="store_true", help="续跑时接受 HEAD 比状态记录多出的提交（你手工修好后提交的）")
    parser.add_argument("--label", help="批次名，进收据标签与提交说明（默认 reseal-chain；续跑沿用首次的值）")
    parser.add_argument("--reason", help="一句话原因，进载荷审核注释、提交说明与收据原因（默认\"版本登记重封链\"）")
    parser.add_argument("--date", dest="date_iso", help="日期 YYYY-MM-DD，进审核注释与收据标签（默认今天；续跑沿用首次的值）")
    parser.add_argument("--version", help="Codex 目标版本，如 x.y.z（默认取守恒测试里的 BASELINE_CODEX_VERSION）")
    parser.add_argument("--receipt-before", help="第一份收据的 --before 提交（默认：HEAD 历史里最近一次新增收据的提交）")
    parser.add_argument("--force-reseal", action="store_true", help="守恒模块已绿也强制重封基线")
    parser.add_argument("--jobs", type=int, default=3, help="依赖追踪并行数（默认 3，本机 16 GiB 别超过 4）")
    parser.add_argument("--heavy-run", help="重负载锁脚本 heavy-run.sh 的路径（默认在主检出的 local-analysis/opt-0165 下找）")
    parser.add_argument("--no-heavy-lock", action="store_true", help="追踪不走重负载锁（仅用于确认机器空闲或测试）")
    parser.add_argument("--python-version", help="写进依赖表的 Python 版本说明（默认取 PATH 里 python3 的版本）")
    parser.add_argument("--platform", help="写进依赖表的平台说明（默认沿用现有追踪表）")
    parser.add_argument("--allow-new-unobserved", action="store_true", help="接受追踪失败的新增模块（默认拒绝）")
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS, help="等待后台追踪时的轮询间隔秒数")
    parser.add_argument("--co-author", action="append", help="提交说明末尾追加 Co-Authored-By: <值>，可重复")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = build_parser().parse_args(arguments)
    chain = Chain(options, arguments)
    try:
        return chain.run()
    except ChainError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except StepFailed as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(Exception):
            if chain._lock_handle:
                chain._lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
