#!/usr/bin/env python3
"""Codex 升级收尾工具包的参数解析与派生（closeout.sh 的第一步，也可单独 ``show`` 查看）。

做什么
    收尾（VC-6 通过之后，到“收尾全集门禁绿、候选备齐”）的所有版本号、Campaign 名、文件名前缀、分支名、ARM64 路径，
    过去靠把上一轮的 cenv.sh 复制后改字面量（约 30 分钟，还容易漏改）。本脚本把它们统一成“参数 → 派生”：
    给出目标版本、基线版本、退休版本和 Campaign 名，其余全部按固定规则算出来。

参数来源与优先级（高 → 低）
    1. 命令行选项（``--version 0.167.0`` 等）；
    2. 环境变量 ``CLOSEOUT_<参数名大写>``（例如 ``CLOSEOUT_CAMPAIGN``）；
    3. 总账（``--ledger <STATUS.md>`` 或环境变量 ``CLOSEOUT_LEDGER``）里约定的字段行：
           closeout.target_version = 0.170.0
           closeout.campaign = c01700-formal-vc1-r1-<日期>t000000z
       一行一个字段；行首可以有 ``-``／``*``／``>``，键名和值可以用反引号包起来，分隔符可用 ``=``、``:`` 或全角 ``：``。
       总账里没有这样的行就当没给，不会去猜；
    4. 派生默认值（见下面“派生规则”）。
    缺少某一步必需的参数时，退出码 2，并写明“缺哪个参数、怎么补”，不会带着猜测值继续。

派生规则（以 0.170.0 / 基线 0.169.0 / 退休 0.168.0 / Campaign c01700-formal-vc1-r1-<日期>t000000z 为例）
    * 版本标记 ``tag_of``：0.170.0 -> 0170，0.149.1 -> 01491（与既有终态收据文件名一致）；
    * 文件名前缀 ``prefix``：CODEX_CLI_<基线标记>_TO_<目标标记>，如 CODEX_CLI_0169_TO_0170；
    * Campaign 标记 ``ctag``：c<主版本><次版本三位><补丁>，如 c01700；Campaign 名必须是
      ``<ctag>-formal-vc1-<轮次>-<时间戳>``，轮次（r1）与时间戳（<日期>t000000z）从名字里拆出来；
    * 候选 ``candidate``：<ctag>-candidate-<轮次>；VC-2 输入目录 ``inputs``：<ctag>-<轮次>-inputs-<时间戳>；
      ARM64 轮次目录 ``rr``：/root/vc-rounds/<ctag>-<轮次>；收尾目录 ``close``：<rr>/closeout；准备目录 ``prep``：/root/codex-<目标标记>-prep；
    * 激活事实文件名 ``fact_name``：<ctag>-production-activation-fact.json；
    * 收尾 tag ``closeout_tag``：codex-<ctag 去掉 c>-closeout-<日期>；VC-6 Catalog 承接收据 tag：codex-<…>-vc6-catalog-promotion-retire-<日期>；
    * 分支约定：收尾分支 reg<目标标记>/closeout，集成分支 reg<目标标记>/integration，候选别名分支 codex/codex-<目标标记>-candidate-<轮次>；
    * 本机路径：主检出由 ``git rev-parse --git-common-dir`` 从“本脚本所在的仓库”推出（与运行时的当前目录无关；``--main`` 可改），收尾工作树 <主检出同级>/<主检出名>-wt/closeout<目标标记去前导 0>，
      产物目录 <主检出>/local-analysis/closeout-<目标版本>/{facts,logs}。
    这些“约定默认值”只是上一轮的命名习惯，轮次命名变了就用对应选项覆盖，不要改代码。

输入／输出／何时用
    输入：命令行选项、环境变量、总账字段（见上）；输出：``emit`` 给 bash 的 ``export`` 行，或 ``show`` 的 JSON；
    何时用：closeout.sh 每次启动时自动调用；想先核对某个参数的来源与派生值时单独跑 ``show``。

子命令
    ``emit <closeout.sh 的全部参数>``  给 closeout.sh 用：把解析结果写成 ``export 名=值`` 行与 ``STEP``、``STEP_ARGS`` 数组，由 bash ``eval``；
    ``show <同上>``                    以 JSON 打印解析结果及每个值的来源（cli/env/ledger/derived），便于核对；
    ``steps``                           打印步骤表（名称、是否写 ARM64、是否属于可选的第二部分组、必需参数）。

用法示例
    python3 -B closeout_params.py show --version 0.170.0 --baseline-version 0.169.0 --retire-version 0.168.0 \\
        --campaign c01700-formal-vc1-r1-<日期>t000000z b0-check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

sys.dont_write_bytecode = True

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
CAMPAIGN_RE = re.compile(r"(?P<ctag>c\d+)-formal-vc1-(?P<round>r\d+)-(?P<stamp>\d{8}t\d{6}z)")
# 总账字段行：``closeout.<键> = <值>``。行首的列表符号、引用符号、反引号都允许；值可以被反引号包住。
LEDGER_LINE = re.compile(r"^[\s>*\-]*`?closeout\.([a-z0-9_]+)`?\s*[=:：]\s*`?([^`\s][^`]*?)`?\s*$")


class ParamError(Exception):
    """参数缺失或不合法；main 里统一转成退出码 2。"""


# ---------------------------------------------------------------------------
# 参数表：键 -> (命令行选项, 说明)。键同时是环境变量后缀（CLOSEOUT_<大写键>）与总账字段名（closeout.<键>）。
# ---------------------------------------------------------------------------
PARAM_SPECS: dict[str, tuple[str, str]] = {
    "target_version": ("--version", "本轮目标版本，如 0.170.0（别名 --target-version）"),
    "baseline_version": ("--baseline-version", "本轮基线版本（晋升后的 Previous），如 0.169.0"),
    "retire_version": ("--retire-version", "本轮退休版本（原 Previous），如 0.168.0"),
    "campaign": ("--campaign", "Formal Campaign 全名 <ctag>-formal-vc1-<轮次>-<时间戳>；b0-check 缺省时可在 ARM64 上发现唯一一个"),
    "candidate": ("--candidate", "候选 id，缺省 <ctag>-candidate-<轮次>"),
    "inputs": ("--inputs", "VC-2 输入目录名，缺省 <ctag>-<轮次>-inputs-<时间戳>"),
    "prefix": ("--prefix", "阶段收据／审计索引／终态收据文件名前缀，缺省 CODEX_CLI_<基线标记>_TO_<目标标记>"),
    "tag_date": ("--tag-date", "收尾 tag 的日期部分 YYYYMMDD，缺省取本机今天"),
    "main": ("--main", "主检出目录（local-analysis、frontend/node_modules 所在），缺省由 git 公共目录推出"),
    "out_dir": ("--out-dir", "本机产物目录（其下 facts/ logs/），缺省 <主检出>/local-analysis/closeout-<目标版本>"),
    "wt": ("--wt", "收尾工作树，缺省 <主检出同级>/<主检出名>-wt/closeout<目标标记去前导0>"),
    "cbranch": ("--cbranch", "收尾分支名，缺省 reg<目标标记>/closeout"),
    "int_branch": ("--int-branch", "集成分支名，缺省 reg<目标标记>/integration"),
    "cand_branch": ("--cand-branch", "VC-6 交接用的候选别名分支，缺省 codex/codex-<目标标记>-candidate-<轮次>"),
    "ssh_host": ("--ssh-host", "ARM64 的 ssh 主机别名，缺省 ARM64"),
    "data": ("--data", "ARM64 抓包数据根，缺省 /root/docker/capture-cli/data"),
    "prep": ("--prep", "ARM64 准备目录（env.sh、pipeline/vc-status.txt 所在），缺省 /root/codex-<目标标记>-prep"),
    "rr": ("--rr", "ARM64 轮次目录，缺省 /root/vc-rounds/<ctag>-<轮次>"),
    "close": ("--close", "ARM64 收尾目录，缺省 <rr>/closeout"),
    "fact_name": ("--fact-name", "ARM64 激活事实文件名，缺省 <ctag>-production-activation-fact.json"),
    "gate_driver": ("--gate-driver", "ARM64 上的受管门禁驱动 entry-gates.sh 路径，缺省 /root/arm64-capture-driver/driver/entry-gates.sh"),
    "arm64_node_modules": ("--arm64-node-modules", "ARM64 上锁定前端依赖的目录（门禁用），没有缺省值：沿用上一轮配置里的 node_modules"),
    "spec_source_root": ("--spec-source-root", "本机 Codex 源码树，缺省 <主检出>/local-analysis/sources/codex-cli-<目标版本>"),
    "typescript_module": ("--typescript-module", "本机 TypeScript 解析器，缺省 <主检出>/frontend/node_modules/typescript/lib/typescript.js"),
    "i1_dir": ("--i1-dir", "登记集成工具目录（未入库；b3-regen 需要其中的 i1 系列工具，b13 的外部预跑脚本也可放这里）"),
    "ledger": ("--ledger", "总账文件（STATUS.md）；其中 ``closeout.<键> = 值`` 行作为参数来源"),
    "co_author": ("--co-author", "提交说明末尾的 Co-Authored-By 署名，如 'Name <mail>'；缺省不加"),
}
# 解析后一定存在（可能为空串）的布尔型开关。
FLAGS = ("with_part2",)

# 参数组：步骤表里按组引用。
V3 = ("target_version", "baseline_version", "retire_version")


class Step:
    """步骤元数据：名称、必需参数、是否写 ARM64、是否属于第二部分可选组、一句话说明。"""

    def __init__(self, name: str, requires: Sequence[str], doc: str, *, arm64_write: bool = False, part2: bool = False) -> None:
        self.name, self.requires, self.doc = name, tuple(requires), doc
        self.arm64_write, self.part2 = arm64_write, part2


STEPS: list[Step] = [
    Step("b0-check", ("target_version",), "只读：VC-6 PASS、PC／RPC、Campaign 与候选目录实名、官方 attempt 唯一（没给 --campaign 时只做发现）"),
    Step("b1-worktree", ("target_version",), "从 RPC（或候选别名分支头）建收尾分支与工作树；集成分支有新提交就预演后并入"),
    Step("b2-part2-lead", (*V3, "campaign"), "【可选，--with-part2；已作废】指南第二部分开头段最小改写（冻结章，之后必须接 b3-regen）", part2=True),
    Step("b3b-vc6-receipt", ("target_version", "retire_version"), "VC-6 两个 Catalog 提交（PC／RPC）的冻结承接收据；候选链已带就跳过"),
    Step("b3a-postpromo-fixes", V3, "晋升后断言修正（boundary 测试的 Active 版本），定向回归，提交"),
    Step("b3-check", ("target_version",), "只读：重封链干跑、归属表、驱动 manifest、晋升后形态定向回归"),
    Step("b3-regen", ("target_version", "i1_dir"), "【可选，--with-part2；已作废】第二章摘要连锁＋冻结承接收据＋重封链（约 40 分钟）", part2=True),
    Step("b4-stage-receipts", ("target_version", "baseline_version"), "ARM64 只读：取回四份阶段收据，逐字节核 sha256"),
    Step("b5-audit-index", (*V3, "campaign"), "ARM64 写：在收尾目录生成审计索引，取回并核摘要", arm64_write=True),
    Step("b6-commit-stage", ("target_version", "baseline_version"), "提交阶段收据＋审计索引"),
    Step("b7-terminal-facts", (*V3, "campaign"), "ARM64 只读取证据小文件 → 终态事实 → 终态候选校验"),
    Step("b8a-active-receipt", (*V3, "campaign"), "生成带新 4.9 值表（Active 终态收据坐标）的指南副本，并用 evidence_index 同一校验核对；不提交"),
    Step("b8-evidence-index", ("target_version", "baseline_version"), "临时放终态候选与 4.9 副本重建 docs/EVIDENCE_INDEX.md 并 --check，提交，立刻移除临时文件"),
    Step("b9-render-guide", (*V3, "campaign"), "渲染最终指南（第一、三、四部分的 Active 绑定位置），输出 facts/final-guide.md"),
    Step("b10-pre", ("target_version", "campaign"), "ARM64 写：可提前做的 seed／mirror.git／repo 准备", arm64_write=True),
    Step("b10-sync", (*V3, "campaign"), "ARM64 写：bundle 上 ARM64、克隆 ff 到本机头，送最终指南与配置生成器", arm64_write=True),
    Step("b10b-prepare", (*V3, "campaign", "arm64_node_modules"), "ARM64 写：draft → 配置 → prepare --dry-run → prepare", arm64_write=True),
    Step("b12-fetch-candidate", ("target_version",), "取回候选提交到本机工作树 <工作树>-cand，核对提交号与 bundle 摘要"),
    Step("b13-prerun", ("target_version",), "CI 等价预跑（候选提交上），脱离会话运行"),
    Step("b14-gates-start", ("target_version",), "ARM64 写：起收尾全集门禁（先 --preflight-only），核对 SigIgn", arm64_write=True),
    Step("b15-gates-status", ("target_version",), "ARM64 只读：门禁状态快照"),
    Step("b16-prepush", ("target_version",), "候选工作树上跑推送前预检（只读）"),
]
STEP_BY_NAME = {step.name: step for step in STEPS}


# ---------------------------------------------------------------------------
# 版本与命名派生（纯函数，测试直接调用）
# ---------------------------------------------------------------------------
def check_version(value: str, what: str) -> str:
    if not VERSION_RE.fullmatch(value or ""):
        raise ParamError(f"{what} 不是 X.Y.Z 形式的版本号：{value!r}")
    return value


def tag_of(version: str) -> str:
    """0.170.0 -> 0170；0.149.1 -> 01491（与既有终态收据文件名一致，补丁为 0 时省略）。"""

    major, minor, patch = (int(part) for part in check_version(version, "版本").split("."))
    return f"{major}{minor:03d}{patch if patch else ''}"


def campaign_tag_of(version: str) -> str:
    """0.170.0 -> c01700；0.149.1 -> c01491（补丁位始终保留）。"""

    major, minor, patch = (int(part) for part in check_version(version, "版本").split("."))
    return f"c{major}{minor:03d}{patch}"


def parse_ledger(text: str) -> dict[str, str]:
    """从总账文本里取 ``closeout.<键> = <值>`` 字段行；同一个键出现多次时取最后一次（总账是追加式的）。"""

    found: dict[str, str] = {}
    for line in text.splitlines():
        match = LEDGER_LINE.match(line)
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def detect_main(cwd: Path) -> Path:
    """主检出 = ``git rev-parse --git-common-dir`` 的上一级（链接工作树里运行也能找到）；不在 git 仓库里时报错，让调用方显式给 --main。"""

    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise ParamError("推不出主检出目录（当前不在 git 仓库里）：请用 --main 指定")
    return Path(result.stdout.strip()).parent


def collect(cli: Mapping[str, object], env: Mapping[str, str], ledger: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """按 命令行 > 环境变量 > 总账 的顺序取原始值；返回（值，来源）。空串视为没给。"""

    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in PARAM_SPECS:
        for source, getter in (("cli", lambda k: cli.get(k)), ("env", lambda k: env.get(f"CLOSEOUT_{k.upper()}")), ("ledger", lambda k: ledger.get(k))):
            value = getter(key)
            if value not in (None, ""):
                values[key] = str(value)
                sources[key] = source
                break
    return values, sources


def derive(values: dict[str, str], sources: dict[str, str], *, cwd: Path, today: str | None = None) -> None:
    """就地补全派生默认值（来源记为 derived）。只依赖已有的值；信息不够的派生项保持缺失，由步骤的必需参数检查报错。"""

    def put(key: str, value: str) -> None:
        if key not in values:
            values[key] = value
            sources[key] = "derived"

    for key in ("target_version", "baseline_version", "retire_version"):
        if key in values:
            check_version(values[key], f"--{key.replace('_', '-')}")
    target = values.get("target_version")
    if target:
        put("main", str(detect_main(cwd)) if "main" not in values else values["main"])
        ttag = tag_of(target)
        ctag = campaign_tag_of(target)
        values["target_tag"], sources["target_tag"] = ttag, "derived"
        values["ctag"], sources["ctag"] = ctag, "derived"
    if values.get("baseline_version"):
        values["baseline_tag"], sources["baseline_tag"] = tag_of(values["baseline_version"]), "derived"
    if values.get("retire_version"):
        values["retire_tag"], sources["retire_tag"] = tag_of(values["retire_version"]), "derived"
    if target and values.get("baseline_version"):
        put("prefix", f"CODEX_CLI_{values['baseline_tag']}_TO_{values['target_tag']}")
    # Campaign 名拆出标记、轮次、时间戳，并核对版本标记
    round_name = "r1"
    stamp = ""
    if values.get("campaign"):
        match = CAMPAIGN_RE.fullmatch(values["campaign"])
        if not match:
            raise ParamError(f"--campaign 不是 <ctag>-formal-vc1-<轮次>-<时间戳> 形式：{values['campaign']!r}")
        if target and match.group("ctag") != values["ctag"]:
            raise ParamError(f"Campaign 名里的版本标记 {match.group('ctag')} 与目标版本 {target}（应为 {values['ctag']}）不符：参数传错了版本或 Campaign")
        round_name, stamp = match.group("round"), match.group("stamp")
    values["round"], sources["round"] = round_name, "derived" if not values.get("campaign") else "campaign"
    if stamp:
        values["stamp"], sources["stamp"] = stamp, "campaign"
    if target:
        ctag, ttag = values["ctag"], values["target_tag"]
        put("candidate", f"{ctag}-candidate-{round_name}")
        if stamp:
            put("inputs", f"{ctag}-{round_name}-inputs-{stamp}")
        put("data", "/root/docker/capture-cli/data")
        put("prep", f"/root/codex-{ttag}-prep")
        put("rr", f"/root/vc-rounds/{ctag}-{round_name}")
        put("close", f"{values['rr']}/closeout")
        put("fact_name", f"{ctag}-production-activation-fact.json")
        put("gate_driver", "/root/arm64-capture-driver/driver/entry-gates.sh")
        put("ssh_host", "ARM64")
        put("tag_date", today or datetime.now().strftime("%Y%m%d"))
        if not re.fullmatch(r"\d{8}", values["tag_date"]):
            raise ParamError(f"--tag-date 须是 8 位日期 YYYYMMDD：{values['tag_date']!r}")
        digits = ctag[1:]
        values["closeout_tag"], sources["closeout_tag"] = f"codex-{digits}-closeout-{values['tag_date']}", "derived"
        values["vc6_receipt_tag"], sources["vc6_receipt_tag"] = f"codex-{digits}-vc6-catalog-promotion-retire-{values['tag_date']}", "derived"
        put("cbranch", f"reg{ttag}/closeout")
        put("int_branch", f"reg{ttag}/integration")
        put("cand_branch", f"codex/codex-{ttag}-candidate-{round_name}")
        main = Path(values["main"])
        put("out_dir", str(main / "local-analysis" / f"closeout-{target}"))
        put("wt", str(main.parent / f"{main.name}-wt" / f"closeout{ttag.lstrip('0') or '0'}"))
        put("spec_source_root", str(main / "local-analysis" / "sources" / f"codex-cli-{target}"))
        put("typescript_module", str(main / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"))
        put("facts", str(Path(values["out_dir"]) / "facts"))
        put("logs", str(Path(values["out_dir"]) / "logs"))


def missing_message(key: str, ledger_path: str | None) -> str:
    flag = PARAM_SPECS[key][0]
    ledger_hint = ledger_path or "总账 STATUS.md（用 --ledger 指定）"
    return (f"缺少参数 {key}：{PARAM_SPECS[key][1]}。请用 {flag} 传入，或设环境变量 CLOSEOUT_{key.upper()}，"
            f"或在 {ledger_hint} 里写一行 ``closeout.{key} = <值>``")


def require(step: Step, values: Mapping[str, str], ledger_path: str | None, *, with_part2: bool) -> None:
    """检查某一步的必需参数；缺哪个报哪个（一次列全）。part2 组步骤没开 --with-part2 时不检查（它们会被跳过）。"""

    if step.part2 and not with_part2:
        return
    problems = [missing_message(key, ledger_path) for key in step.requires if not values.get(key)]
    if problems:
        raise ParamError("\n".join(problems))


def resolve(cli: Mapping[str, object], env: Mapping[str, str], *, cwd: Path, step: str | None = None, today: str | None = None,
            ledger_text: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """完整解析：读总账 → 取原始值 → 派生 → 检查步骤必需参数。返回（值，来源）。"""

    ledger_path = str(cli.get("ledger") or env.get("CLOSEOUT_LEDGER") or "")
    if ledger_text is None and ledger_path:
        try:
            ledger_text = Path(ledger_path).read_text(encoding="utf-8")
        except OSError as error:
            raise ParamError(f"读不到总账 {ledger_path}：{error}") from error
    ledger = parse_ledger(ledger_text or "")
    values, sources = collect(cli, env, ledger)
    if ledger_path:
        values["ledger"], sources["ledger"] = ledger_path, "cli" if cli.get("ledger") else "env"
    derive(values, sources, cwd=cwd, today=today)
    values["with_part2"] = "1" if cli.get("with_part2") else ""
    if step is not None:
        if step not in STEP_BY_NAME:
            raise ParamError(f"未知步骤 {step!r}；可用步骤：{', '.join(STEP_BY_NAME)}")
        require(STEP_BY_NAME[step], values, ledger_path or None, with_part2=bool(values["with_part2"]))
    return values, sources


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="closeout_params.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, add_help=True)
    parser.add_argument("command", choices=("emit", "show", "steps"))
    for key, (flag, text) in PARAM_SPECS.items():
        names = [flag, "--target-version"] if key == "target_version" else [flag]
        parser.add_argument(*names, dest=key, default=None, metavar="值", help=text)
    parser.add_argument("--with-part2", dest="with_part2", action="store_true",
                        help="启用可选的“指南第二部分最小改写＋第二章摘要连锁”组（b2-part2-lead、b3-regen；现行流程已作废）；缺省关闭")
    return parser


def split_command_line(argv: Sequence[str]) -> tuple[list[str], str | None, list[str]]:
    """把命令行拆成（全局选项部分, 步骤名, 步骤参数）。

    argparse 的 REMAINDER 在“可选位置参数 + 其后还有选项”时会把步骤名吞掉，所以自己拆：第一个子命令之后，已知的全局选项（带值的连同值）
    原样留在前半部分，遇到第一个不是全局选项的记号就是步骤名，其后全部是步骤参数（步骤参数里允许出现与全局选项同名的记号，不再被解析）。
    """

    tokens = list(argv)
    if not tokens:
        return [], None, []
    value_flags = {flag for flag, _text in PARAM_SPECS.values()} | {"--target-version"}
    head, index = [tokens[0]], 1
    while index < len(tokens):
        token = tokens[index]
        if token in value_flags:
            head.extend(tokens[index:index + 2])
            index += 2
        elif token.split("=", 1)[0] in value_flags and "=" in token:
            head.append(token)
            index += 1
        elif token in ("--with-part2", "-h", "--help"):
            head.append(token)
            index += 1
        else:
            break
    step = tokens[index] if index < len(tokens) else None
    return head, step, tokens[index + 1:]


def shell_exports(values: Mapping[str, str], step: str, rest: Sequence[str]) -> str:
    lines = [f"export {key.upper()}={shlex.quote(value)}" for key, value in sorted(values.items())]
    lines.append(f"CL_STEP={shlex.quote(step)}")
    lines.append("CL_STEP_ARGS=(" + " ".join(shlex.quote(item) for item in rest) + ")")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    head, step, rest = split_command_line(raw)
    args = build_parser().parse_args(head)
    env = os.environ if env is None else env
    # 主检出必须从“本脚本所在的仓库”推出，而不是从当前目录推出：closeout.sh 的约定是“在任意目录运行”。
    # 以前取 Path.cwd()，在别的 git 仓库里运行会悄悄把那个仓库当成主检出（产物目录、收尾工作树、源码树路径全错），
    # 在非 git 目录里运行则直接报“推不出主检出”。调用方（测试）显式传 cwd 时仍按传入值。
    cwd = cwd or Path(__file__).resolve().parent
    if args.command == "steps":
        for item in STEPS:
            marks = ("[ARM64 写]" if item.arm64_write else "") + ("[可选 --with-part2]" if item.part2 else "")
            print(f"{item.name:22s} 必需参数：{','.join(item.requires):60s} {marks} {item.doc}")
        return 0
    if not step:
        print("缺少步骤名；用 ``steps`` 子命令查看步骤表", file=sys.stderr)
        return 2
    if step.startswith("-"):
        print(f"[closeout] 参数错误：未知选项 {step}（全局选项要写在步骤名之前；步骤自己的选项写在步骤名之后）", file=sys.stderr)
        return 2
    cli = {key: getattr(args, key) for key in (*PARAM_SPECS, *FLAGS)}
    try:
        values, sources = resolve(cli, env, cwd=cwd, step=step)
    except ParamError as error:
        print(f"[closeout] 参数错误：{error}", file=sys.stderr)
        return 2
    if args.command == "show":
        print(json.dumps({"step": step, "step_args": rest, "values": values, "sources": sources}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(shell_exports(values, step, rest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
