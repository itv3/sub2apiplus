#!/usr/bin/env python3
"""Codex 升级收尾：候选制品、指南人工批准、完整门禁和可恢复发布。

候选制品只写入隔离目录；正式冻结承接、终态签发与推送必须消费同一份指南批准和门禁结论。

收尾门禁的两个可选配置（Codex 升级工具优化第 10a 项 D1 与第 17 项；``gate`` 里不写这些键时一切与改造前逐字相同）：

* ``gate.work``：固定测试树坐标——与后台验证同一个位置 ``<数据根的上一级>/entry-gates-source-work/fixed/<客户端>``（见
  ``background_validation.fixed_work_root``）。承接要求当前测试树的绝对路径等于来源记录的 ``read_audit.repo_root``；收尾原先的
  ``<work_root>/gate-work`` 每次收尾换一个路径，后台验证的记录永远承接不过来。
* ``gate.unit_inherit``（须同时给 ``gate.work`` 与 ``gate.record_store``）：允许前序后台验证的提交向收尾提交**逐单元**承接——收尾 ``prepare`` 会在候选
  副本里改指南、写终态收据、生成冻结收据并提交出新的候选提交，所以收尾提交永远不是跑过后台验证的那个提交，复用规则不能写“同提交”。
  条件是该单元的输入（按第 10a 项的承接视图）、环境和验证规则在两个提交之间一致；只重跑被收尾改动（指南、终态收据、冻结收据及其摘要
  连锁）影响到的单元。门禁以 ``--mode unit-inherit`` 运行，收尾校验改为：运行清单经 ``unit_inherit.verify_published(purpose="closeout")``
  重放通过、承接项的 ``inherit_check.repo_root`` 等于固定测试树；收尾收据逐项列出承接单元、来源运行、来源记录摘要、视图摘要，以及被重跑的单元与原因
  （语义变化：“一次全绿”变成“全绿，允许逐单元承接”）。
  一票否决（补承接视图发现不了的类型翻转，已知缺口 R2）：每个来源运行的提交到候选提交之间只要有类型变化、符号链接（120000）、子模块（160000）、
  ``.gitmodules`` 变更，或同名的文件↔目录翻转，整轮就不承接任何单元——门禁启动之前先拦（``run_gates``），门禁结果出来后 ``check_gate_result``
  再独立复核一次；来源提交解析不出来、变更集读不出来同样按失败关闭拒绝（要完整执行就去掉 ``gate.unit_inherit``）。

承接请求后置（第 17 项正式启用）：承接请求的上下文绑定收尾候选提交，而候选提交由 ``prepare`` 生成（冻结收据带分钟级时间戳，不能靠预知提交号），
所以请求不可能在 ``prepare`` 之前就位。``gate.unit_inherit`` 因此允许只写 ``{"stage": ...}``（请求后置）：``prepare`` 之后先 ``inherit-propose``
（让执行器对候选提交写承接提案）、再 ``inherit-request``（提案＋两份批准＋校时与撤销凭证组装请求，并立刻对照方案复核），最后
``gates --unit-inherit-request <请求>``。起跑前核对请求绑定的提交、客户端、阶段、记录库、门禁组合与批准都对得上本方案，对不上直接拒绝（不然执行器会静默
回退成整轮重跑）；通过后把请求的路径与摘要写进 ``journal/gates-inherit-request.json``（不覆盖），后续的门禁结果校验、收据与恢复都以这个占位为准。
执行器只让能证明的单元承接，其余（含收尾改动影响到的）回退重跑，并在运行清单里写明原因。原来的 ``{"request": ..., "stage": ...}`` 写法保持不变。

Claude 升级共用这一份收尾编排（Claude 升级控制面设计 B5b-06）：配置合同名是 ``claude-upgrade-closeout/v1`` 时（客户端表
``closeout_schema``），随客户端变的三件事——指南材料的封存读侧、第二部分摘要连锁、终态收据的生成与校验——转给
``tools/claude_closeout.py``，指南文件与第二部分标题、候选分支名、终态收据文件名前缀按客户端表取值；候选隔离、人工批准、
完整门禁、步骤日志、清理与推送逐字共用。Codex 配置的输入、输出、报错与改造前逐字相同（客户端表的 Codex 取值就是原字面）。
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.arm64_capture_driver.driver import fix_safety as safety
from tools.ci import client_table
from tools.ci import retry_policy

GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
PART2 = "# 第二部分 Codex CLI 客户端规则画像"
SHA256 = re.compile(r"[0-9a-f]{64}")
SCHEMA = "codex-upgrade-closeout/v1"
# 收尾配置与方案的合同名 → 客户端（B5b-06，客户端表 ``closeout_schema``；Codex 一行就是上面的 ``SCHEMA``）。
CLIENT_SCHEMAS = {client_table.spec(name)["closeout_schema"]: name for name in client_table.CLIENTS}
# Claude 收尾另绑定的工具文件（相对本文件所在目录）：客户端部分、终态合同与客户端表。
CLIENT_TOOL_FILES = ("claude_closeout.py", "claude_terminal_state.py", "ci/client_table.py")


class CloseoutError(ValueError):
    """收尾输入、批准、门禁或不可逆步骤的状态不满足合同。"""


def digest(value):
    return safety.digest(value)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inside(root, relative):
    """仓库坐标不允许链接、父目录跳转和 Git 元数据。"""
    item = Path(relative)
    if item.is_absolute() or not item.parts or any(part in {"..", ".git"} for part in item.parts):
        raise CloseoutError("收尾文件必须使用仓库内普通相对路径")
    return safety.plain(Path(root) / item)


def client_of(config):
    """收尾配置（或方案）属于哪个客户端：按合同名查客户端表（未知合同名先由 ``validate_config`` 拒绝）。"""
    return CLIENT_SCHEMAS[config["schema_version"]]


def _claude():
    from tools import claude_closeout
    return claude_closeout


def _base():
    """传给客户端部分的本模块（共用路径、摘要、Git 与错误类型；直跑与按包导入都取到正在运行的这一份）。"""
    return sys.modules[__name__]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def part2_sha(text, heading=PART2):
    """与正式 source_spec_section_sha256 保持相同的章节边界和原始换行（``heading`` 缺省为 Codex 第二部分标题）。"""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == heading]
    if len(starts) != 1:
        raise CloseoutError("指南必须恰有一个第二部分")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    return sha("".join(lines[start:end]).encode())


def source_updates(repo, guide_text, client="codex"):
    """只更新指南、十二类现存清单的来源摘要与实际引用它们的检查器常量（Claude 转 ``claude_closeout.source_updates``）。"""
    if client != "codex":
        return _claude().source_updates(repo, guide_text, base=_base())
    repo = safety.plain(repo)
    guide = inside(repo, GUIDE)
    before = guide.read_text()
    old_sha, new_sha = part2_sha(before), part2_sha(guide_text)
    updates = {}

    def add(relative, content):
        path = inside(repo, relative)
        original = path.read_bytes()
        if original != content:
            updates[relative] = {"before_sha256": sha(original), "after_sha256": sha(content), "text": content.decode()}

    add(GUIDE, guide_text.encode())
    if old_sha == new_sha:
        return updates
    profile_updates = {}
    history_hashes = None
    directory = repo / "tools/official_client_capture"
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            continue
        source = value.get("source_spec")
        if isinstance(source, dict) and source.get("path") == GUIDE and source.get("fragment") in {"第二章", "第二部分", "第二部分-规则"}:
            expected = source.get("sha256")
            pattern = r'("source_spec"\s*:\s*\{[^{}]*?"sha256"\s*:\s*")' + re.escape(old_sha) + r'(")'
        elif isinstance(source, str) and source in {GUIDE + "#" + fragment for fragment in ("第二章", "第二部分", "第二部分-规则")}:
            expected = value.get("source_spec_sha256")
            pattern = r'("source_spec_sha256"\s*:\s*")' + re.escape(old_sha) + r'(")'
        else:
            continue
        if expected != old_sha:
            # 老版本画像保留历史指南绑定；只有 Git 历史能重放该章节摘要时才允许保持原样。
            if history_hashes is None:
                history_hashes = set()
                for commit in git(repo, "log", "--format=%H", "--", GUIDE).stdout.splitlines():
                    historical = git(repo, "show", commit + ":" + GUIDE, check=False)
                    if historical.returncode == 0 and PART2 in historical.stdout:
                        history_hashes.add(part2_sha(historical.stdout))
            committed = git(repo, "show", "HEAD:" + path.relative_to(repo).as_posix(), check=False)
            if expected not in history_hashes or committed.returncode or committed.stdout != path.read_text():
                raise CloseoutError(f"已有来源摘要漂移且无历史绑定，不能批量覆盖：{path.name}")
            continue
        content, count = re.subn(pattern, lambda m: m[1] + new_sha + m[2], path.read_text())
        if count != 1:
            raise CloseoutError(f"来源摘要字段不能唯一定位：{path.name}")
        relative = path.relative_to(repo).as_posix()
        add(relative, content.encode())
        profile_updates[relative] = (sha(path.read_bytes()), sha(content.encode()))
    if not profile_updates:
        raise CloseoutError("没有发现与指南第二部分绑定的清单")
    for path in sorted(directory.glob("*.py")):
        text = path.read_text()
        constants = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"DEFAULT_PROFILE_RELATIVE_PATH", "FROZEN_PROFILE_SHA256"}:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
        profile = constants.get("DEFAULT_PROFILE_RELATIVE_PATH")
        if profile not in profile_updates or "FROZEN_PROFILE_SHA256" not in constants:
            continue
        old, new = profile_updates[profile]
        if constants["FROZEN_PROFILE_SHA256"] != old:
            raise CloseoutError(f"检查器的原画像摘要不符：{path.name}")
        pattern = r'(\bFROZEN_PROFILE_SHA256\s*=\s*\(?\s*[\'"])' + old + r'([\'"])'
        changed, count = re.subn(pattern, lambda m: m[1] + new + m[2], text)
        if count != 1:
            raise CloseoutError(f"检查器摘要常量不能唯一定位：{path.name}")
        add(path.relative_to(repo).as_posix(), changed.encode())
    return updates


def collect_guide_material(campaign, candidate, client="codex"):
    """在独立解释器中使用 Campaign 的受管读侧，避免把源码副本误当成证据数据根。"""
    campaign = safety.plain(campaign)
    if campaign.parent.name != "campaigns" or campaign.parent.parent.name != "evidence":
        raise CloseoutError("Campaign 必须位于受管根的 evidence/campaigns 下")
    reader_root = campaign.parents[2]
    if client == "codex":
        safety.file_binding(reader_root / "tools/official_client_capture/codex_upgrade.py")
    else:
        _claude().reader_binding(reader_root, _base())
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "read-material",
            "--campaign", str(campaign), "--candidate", candidate]
    if client != "codex":
        argv += ["--client", client]
    result = subprocess.run(argv,
                            cwd=reader_root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True)
    if result.returncode:
        raise CloseoutError("受管读侧材料重放失败：" + result.stderr.strip()[-1600:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("reader_root") != str(reader_root):
        raise CloseoutError("材料读侧没有绑定受管数据根")
    return value


def read_guide_material(campaign, candidate):
    """使用正式封存读侧取得 VC-2、VC-5 批准修订和 VC-1 原始观测；不发请求。"""
    campaign = safety.plain(campaign)
    if campaign.parent.name != "campaigns" or campaign.parent.parent.name != "evidence":
        raise CloseoutError("Campaign 路径不是受管布局")
    reader_root = campaign.parents[2]
    sys.path.insert(0, str(reader_root))
    from tools.official_client_capture import codex_upgrade as upgrade
    from tools.official_client_capture import candidate_rule_assertion as checker
    if Path(upgrade.__file__).resolve().parents[2] != reader_root or Path(checker.__file__).resolve().parents[2] != reader_root:
        raise CloseoutError("材料重放须使用独立解释器，不能混入其他源码根的模块")
    reader_files = {str(path): safety.file_binding(path)["sha256"]
                    for path in sorted((reader_root / "tools/official_client_capture").rglob("*"))
                    if path.is_file() and path.suffix in {".py", ".json", ".sh"} and "__pycache__" not in path.parts}
    manifest = upgrade.load_campaign_manifest(campaign)
    classified = upgrade._load_stage_result(campaign, "classify")
    if classified.get("status") != "complete":
        raise CloseoutError("VC-2 尚未完整批准")
    effective = upgrade._effective_classification_view(campaign, candidate, classified)
    references = {key: effective[key] for key in ("migration_manifest", "assertion_profile_manifest", "target_rule_manifest")}
    documents = {}
    for key, binding in references.items():
        path = inside(campaign, binding["path"])
        if sha(path.read_bytes()) != binding["sha256"]:
            raise CloseoutError("批准清单摘要漂移")
        documents[key] = safety.read(path)
    official = upgrade._load_stage_result(campaign, "capture-official")
    if official.get("status") != "complete":
        raise CloseoutError("VC-1 官方证据尚未完整封存")
    context = official["assertion_context"]
    capture = safety.plain(context["capture_manifest_path"])
    if sha(capture.read_bytes()) != context["capture_manifest"]["sha256"]:
        raise CloseoutError("VC-1 断言包摘要漂移")
    _capture, observations = checker.load_observations(capture, safety.plain(context["evidence_root"]), manifest["target_version"])
    profile = documents["assertion_profile_manifest"]
    counts = {}
    for rule in profile["rules"]:
        counts[rule["rule_id"]] = {check["id"]: len(checker._select_observations(observations, check["select"], rule["scenario_ids"]))
                                    for check in rule["checks"]}
    if any(safety.file_binding(path)["sha256"] != expected for path, expected in reader_files.items()):
        raise CloseoutError("受管读侧在重放期间变化")
    return {"reader_root": str(reader_root), "reader_files": reader_files,
            "campaign_path": str(campaign), "campaign_id": manifest["campaign_id"], "candidate_id": candidate, "target_version": manifest["target_version"],
            "classification_sha256": digest(classified), "effective_classification_sha256": digest(effective),
            "official_stage_sha256": digest(official), "capture_manifest": safety.file_binding(capture),
            "references": references, "migration": documents["migration_manifest"], "profile": profile,
            "official_observation_counts": counts}


def guide_draft(original, material, baseline_profile, heading=PART2):
    """生成受影响规则的可审核草稿，不把候选内部判据的零官方观测称为实测通过（``heading`` 缺省为 Codex 第二部分标题）。"""
    entries = material["migration"]["entries"]
    current = {row["rule_id"]: row for row in material["profile"]["rules"]}
    baseline = {row["rule_id"]: row for row in baseline_profile["rules"]}
    affected = []
    start = original.index(heading)
    end = re.search(r"(?m)^# ", original[start + len(heading):])
    end = start + len(heading) + end.start() if end else len(original)
    prefix, text, suffix = original[:start], original[start:end], original[end:]
    for entry in entries:
        rule_id = entry.get("target_rule") or entry.get("baseline_rule")
        rule = current.get(rule_id)
        if entry["classification"] == "inherit" and rule == baseline.get(rule_id):
            continue
        if entry["classification"] == "blocked" or not str(entry.get("rationale", "")).strip():
            raise CloseoutError("迁移分类尚未闭合或缺少理由")
        affected.append(rule_id)
        pattern = re.compile(r"(?m)^### " + re.escape(rule_id) + r"[^\n]*\n(?:(?!^#{1,3} ).|\n)*")
        found = list(pattern.finditer(text))
        if len(found) > 1:
            raise CloseoutError(f"指南规则标题重复：{rule_id}")
        title = found[0].group().splitlines()[0] if found else "### " + rule_id
        body = title + "\n\n- **迁移分类**：" + entry["classification"] + "。\n- **迁移理由**：" + entry["rationale"] + "\n"
        body += "- **批准证据引用**：" + json.dumps(entry.get("evidence_refs", []), ensure_ascii=False) + "\n"
        if rule is not None:
            body += "- **场景**：" + "、".join(rule["scenario_ids"]) + "。\n- **判据与 VC-1 观测**（匹配数不是去重请求数；内部判据仍以 VC-5 为准）：\n\n"
            for check in rule["checks"]:
                count = material["official_observation_counts"][rule_id][check["id"]]
                body += "  - `" + check["id"] + "`：匹配 " + str(count) + " 条；选择条件 `" + json.dumps(check.get("select"), ensure_ascii=False, sort_keys=True) + "`；判据 `" + json.dumps(check["assertion"], ensure_ascii=False, sort_keys=True) + "`。\n"
        body += "\n"
        if found:
            text = text[:found[0].start()] + body + text[found[0].end():]
        else:
            lines = text.splitlines(keepends=True)
            start = next(i for i, line in enumerate(lines) if line.rstrip() == heading)
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
            text = "".join(lines[:end]) + body + "".join(lines[end:])
    return {"guide_text": prefix + text + suffix, "affected_rule_ids": sorted(set(affected)), "material_sha256": digest(material)}


def terminal_candidate(repo, facts, receipt_relative):
    """从结构化终态事实生成候选字节，并通过正式终态校验器；不写入正式坐标。"""
    path = inside(repo, receipt_relative)
    if path.parent != Path(repo) / "docs/egress/maintenance" or not path.name.startswith("CODEX_CLI_") or not path.name.endswith("_TERMINAL_STATE_RECEIPT.json"):
        raise CloseoutError("终态收据坐标不符合正式合同")
    ledger = load_module(Path(repo) / "tools/check_ledger_completeness.py", "closeout_terminal_validator")
    if facts.get("target", {}).get("version") != ledger._runtime_catalog_active_state()[0]:
        raise CloseoutError("新签终态必须对应当前 active 版本")
    value = {key: item for key, item in facts.items() if key != "identity_sha256"}
    value["identity_sha256"] = ledger.codex_terminal_state_identity(value, trailing_newline=False)
    ledger.validate_codex_terminal_state_document(value, receipt_relative)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def terminal_for(client):
    """终态候选生成器：Codex 是本文件的 ``terminal_candidate``（调用时取，便于隔离替身），Claude 转 ``claude_closeout``。"""
    if client == "codex":
        return terminal_candidate
    claude = _claude()
    return lambda repo, facts, relative: claude.terminal_candidate(repo, facts, relative, base=_base())


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise CloseoutError("Git 操作失败：" + result.stderr.strip()[-1500:])
    return result


def tree_manifest(repo):
    """记录提交与全部已跟踪文件字节，忽略 Git 元数据及门禁输出目录。"""
    paths = git(repo, "ls-files", "-z").stdout.split("\0")
    return {name: safety.file_binding(inside(repo, name))["sha256"] for name in sorted(paths) if name}


@contextmanager
def run_lock(root):
    root = safety.plain(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".closeout.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise CloseoutError("本次收尾已有实例运行") from error
    finally:
        os.close(fd)


def write_text_once(path, text):
    """使用内容寻址字节收据防止覆盖；候选目录内也不能悄悄改写旧提案。"""
    path = safety.plain(path)
    if path.exists():
        if path.read_text() != text:
            raise CloseoutError("已有收尾文件内容不同，须开新轮次")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".closeout-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if path.read_text() != text:
                raise CloseoutError("并发写入的收尾文件不同")
    finally:
        Path(name).unlink()
        safety.sync_directory(path.parent)


def approval(path, plan, scope):
    """只消费人工提交的批准，绑定候选提交、全部制品与完整发布动作。"""
    if not path:
        raise CloseoutError("缺少人工批准：" + scope)
    value = safety.read(path)
    info = Path(path).stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise CloseoutError("批准须由运行账号拥有且权限为 0600")
    schema = "guide-review-approval/v1" if scope == "guide-review" else "codex-closeout-operation-approval/v1"
    if (value.get("schema_version") != schema or value.get("status") != "approved"
            or value.get("review_sha256") != plan["review_sha256"] or value.get("scope") != scope
            or not str(value.get("approved_by", "")).strip()):
        raise CloseoutError("指南批准的身份、范围或摘要不一致")
    verify_binding(value.get("proof"))
    def utc(item):
        if not isinstance(item, str) or not item.endswith("Z"):
            raise CloseoutError("批准时间必须是 UTC Z 时间")
        return datetime.fromisoformat(item.replace("Z", "+00:00"))
    if not utc(value.get("approved_at_utc")) <= datetime.now(timezone.utc) < utc(value.get("expires_at_utc")):
        raise CloseoutError("指南批准未生效或已过期")
    return safety.file_binding(path)


def collect_cleanup_context(config):
    """独立只读进程从本轮受管工具重放清理参数，禁止混入收尾仓库的另一份读侧。"""
    gate = config["gate"]
    script = safety.plain(Path(gate["driver"]).parent / "cleanup_context.py")
    inputs = {str(path): safety.file_binding(path) for path in (script, gate["vc_env"])}
    result = subprocess.run([sys.executable, "-B", str(script), "--env", gate["vc_env"]],
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
    if result.returncode:
        raise CloseoutError("当前清理参数无法重放：" + result.stderr.strip()[-1200:])
    report = json.loads(result.stdout)
    if (report.get("schema_version") != "arm64-cleanup-context/v1"
            or not isinstance(report.get("parameters"), dict) or not report.get("bindings")):
        raise CloseoutError("清理参数报告不完整")
    if any(safety.file_binding(path) != value for path, value in inputs.items()):
        raise CloseoutError("清理参数入口或轮次文件发生漂移")
    for value in report["bindings"].values():
        verify_binding({key: value[key] for key in ("path", "sha256")})
    return report


def resolve_cleanup(config):
    """只展开清理字段中的显式参数引用；原配置保持不变，不执行 shell 替换。"""
    if config["cleanup"].get("mode") != "execute":
        return config, None
    context = collect_cleanup_context(config)
    parameters = context["parameters"]
    material = safety.read(config["material"])
    for name, key in (("campaign_path", "NEWDIR"), ("campaign_id", "NEW"),
                      ("candidate_id", "CAND"), ("target_version", "TARGET_VERSION")):
        if material.get(name) != parameters.get(key):
            raise CloseoutError("清理参数与收尾材料的当前身份不一致：" + name)
    def expand(value):
        if not isinstance(value, str):
            raise CloseoutError("清理路径和参数必须为字符串")
        def replace(match):
            name = match[1]
            if name not in parameters or not str(parameters[name]):
                raise CloseoutError("未知或空清理参数：" + name)
            return str(parameters[name])
        text = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", replace, value)
        if "${" in text or "\x00" in text:
            raise CloseoutError("清理参数引用格式非法")
        return text
    resolved = deepcopy(config)
    action = resolved["cleanup"]
    for field in ("script", "result"):
        action[field] = expand(action[field])
    for field in ("arguments", "inputs", "targets"):
        if not isinstance(action[field], list):
            raise CloseoutError("清理参数、输入及目标必须为数组")
        action[field] = [expand(value) for value in action[field]]
    for field in ("backup", "restore_check"):
        action[field]["path"] = expand(action[field]["path"])
    assertions = {"--campaign-dir": "NEWDIR", "--campaign-id": "NEW", "--candidate-id": "CAND",
                  "--attempt-id": "ATT", "--evaluation-baseline": "EVALUATION_BASELINE",
                  "--vc5": "VC5_RECEIPT", "--vc5-receipt": "VC5_RECEIPT",
                  "--vc6": "VC6_RECEIPT", "--vc6-receipt": "VC6_RECEIPT",
                  "--activation-receipt": "ACTIVATION_RECEIPT", "--rollback-receipt": "ROLLBACK_RECEIPT",
                  "--retire-receipt": "RETIRE_RECEIPT"}
    for index, word in enumerate(action["arguments"]):
        option, separator, inline = word.partition("=")
        if option in assertions:
            value = inline if separator else (action["arguments"][index + 1] if index + 1 < len(action["arguments"]) else None)
            if value is None or not parameters.get(assertions[option]) or value != parameters[assertions[option]]:
                raise CloseoutError("清理脚本参数未绑定当前阶段：" + option)
    if not action["targets"] or len(set(action["targets"])) != len(action["targets"]):
        raise CloseoutError("清理目标为空或重复")
    # 脚本仍由专项批准固定；此处防止删除其运行输入、源码、收据或任一保护路径的祖先。
    protected = [config["repo"], config["work_root"], parameters["NEWDIR"], parameters["SOURCE_ROOT"],
                 str(Path(parameters["D"]) / "tools"), str(Path(parameters["D"]) / "evidence"),
                 str(Path(parameters["B"]) / "production-tree"), parameters["W"],
                 action["script"], action["backup"]["path"], action["restore_check"]["path"],
                 *action["inputs"], *[value["path"] for value in context["bindings"].values()]]
    for value in action["targets"]:
        if not value or any(c in value for c in "*?[]\n\r"):
            raise CloseoutError("清理目标必须明确，不能使用模式或空值")
        if value.startswith("/"):
            target = safety.plain(value)
            if target.is_relative_to(Path(parameters["D"]) / "candidates") and not target.is_relative_to(Path(parameters["B"])):
                raise CloseoutError("清理目标属于其他 Candidate")
            if any(target.is_relative_to(safety.plain(path)) or safety.plain(path).is_relative_to(target) for path in protected):
                raise CloseoutError("清理目标与必须保留的输入、源码或证据重叠")
        elif not re.fullmatch(r"(?:image@sha256:[0-9a-f]{64}|(?:container|volume|network)@[a-zA-Z0-9][a-zA-Z0-9_.-]*)", value):
            raise CloseoutError("资源清理目标必须明确类型及完整镜像摘要或资源标识")
    restore = safety.read(verify_binding(action["restore_check"]))
    if (restore.get("status") != "passed" or restore.get("targets") != action["targets"]
            or restore.get("backup") != action["backup"]
            or restore.get("cleanup_context_sha256") != digest(context)):
        raise CloseoutError("恢复演练未绑定当前清理上下文、对象或备份")
    verify_binding(action["backup"])
    return resolved, context


def check_cleanup_context(plan):
    """审核后的清理计划只作相等断言，禁止悄悄替换为新轮次后继续删除。"""
    basis = plan["basis"]
    if basis["config"]["cleanup"].get("mode") != "execute":
        return
    source = {**basis["config"], "cleanup": basis.get("cleanup_source", {})}
    resolved, context = resolve_cleanup(source)
    if context != basis.get("cleanup_context") or resolved != basis["config"]:
        raise CloseoutError("清理上下文或展开参数已漂移，须重新准备并批准")


INHERIT_STAGE = re.compile(r"pre-ledger|post-ledger|formal:[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_gate_inherit(config, repo, work):
    """``gate`` 里 D1／第 17 项的可选键校验：固定测试树坐标、记录库、逐单元承接请求。三个键都不写时什么也不检查（与改造前相同）。"""
    gate = config["gate"]
    fixed = gate.get("work")
    if fixed is not None:
        path = safety.plain(fixed)
        if path.is_relative_to(repo) or path.is_relative_to(work) or work.is_relative_to(path) or repo.is_relative_to(path):
            raise CloseoutError("固定测试树坐标必须在权威仓库与收尾工作根之外")
        if path.parts[-3:] != ("entry-gates-source-work", "fixed", client_of(config)):
            raise CloseoutError("收尾门禁的固定测试树坐标必须是 entry-gates-source-work/fixed/<客户端>（与后台验证同一个位置）")
    store = gate.get("record_store")
    if store is not None:
        safety.plain(store)
    inherit = gate.get("unit_inherit")
    if inherit is None:
        if store is not None:
            raise CloseoutError("门禁记录库只与逐单元承接同用")
        return
    if (not isinstance(inherit, dict) or set(inherit) not in ({"request", "stage"}, {"stage"}) or fixed is None or store is None
            or not isinstance(inherit["stage"], str) or not INHERIT_STAGE.fullmatch(inherit["stage"])):
        raise CloseoutError("收尾逐单元承接须同时给固定测试树坐标、记录库，以及只含阶段（请求后置）或请求加阶段的承接配置")
    if "request" in inherit:
        safety.file_binding(inherit["request"])


def gate_inherit(config):
    """收尾门禁是否按逐单元承接运行（配置里有 ``gate.unit_inherit``）。"""
    return config["gate"].get("unit_inherit")


def _unit_inherit():
    """同仓库 tools/ci 的逐单元承接合同（按路径加载，与后台验证同法）；收尾不另写批准、来源或决策验证规则。"""
    name = "closeout_unit_inherit_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "ci" / "unit_inherit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _inherit_predict():
    """同仓库 tools/ci 的影响集预览模块（收尾只用它的一票否决函数，不另写类型变化规则）。"""
    name = "closeout_inherit_predict_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "ci" / "inherit_predict.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _background_preflight():
    """同仓库 tools/ci 的后台验证起跑前置检查（收尾复用其中的“冻结收据”一项，不另写一套冻结覆盖判定）。"""
    name = "closeout_background_preflight_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "ci" / "background_preflight.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


# ---------------------------------------------------------------------------
# 收尾两道预检（OPT-17 冻结覆盖、OPT-18 门禁环境注入）：都是秒级，必须赶在两小时的全量门禁起跑之前报出
# ---------------------------------------------------------------------------

# 拆提交的做法（上一次升级的先例）：候选链与收尾分支都改了同一个冻结文件时，git 自动合并出的内容没有任何承接收据登记，
# 收尾候选因此带着未承接的冻结路径，全量门禁约两小时后才在冻结测试处变红。合并后树内容不变，只是把一个提交拆成三个。
FREEZE_SPLIT_ADVICE = (
    "处理办法（合并结果文件拆成三个提交，合并后树内容不变）：①把这些路径还原成收尾侧（合并前收尾分支那一侧）的版本，单独提交；"
    "②再恢复合并结果，单独提交；③对②生成承接收据：python3 -B -m tools.upstream_merge freeze-successor-generate --before <①的提交> "
    "--after <②的提交> --tag <标识> --reason <原因> --dry-run，结果须是 passed_local_evidence_successor 且人工待办为空，去掉 --dry-run 加 --output "
    "docs/egress/maintenance/<标识>-freeze-successor.json 生成后单独提交（源码与收据不能同一提交）。"
    "先例提交：ed4fca062（还原）、05c47bdc7（恢复合并结果）、9076cffe0（承接收据）。处理完在新工作根重新 prepare。"
)


def frozen_coverage(tree):
    """某棵树里有没有“当前内容不在任何承接收据登记里”的冻结路径（秒级，只读）。

    直接复用后台验证起跑前置检查的“冻结收据”一项（``background_preflight.check_freeze_receipts``，即 ``test_ci_background_preflight`` 里
    “真实仓库头无未承接冻结路径”那一项的实现，与门禁里 Go／Python 冻结测试同一张承接图），不在收尾里另写判定。返回它的结果字典：
    ``passed``、``problems``、``detail``（含 ``registered`` 登记路径数、``uncovered`` 未承接路径）。"""
    return _background_preflight().check_freeze_receipts(Path(tree))


def require_frozen_coverage(tree, *, subject):
    """冻结覆盖预检；不通过就抛错并就地给出拆提交的办法，通过返回 ``{"registered": 登记路径数, "uncovered": []}``。"""
    report = frozen_coverage(tree)
    if not report["passed"]:
        raise CloseoutError(f"{subject}没有通过冻结覆盖预检（不起跑，免得全量门禁两小时后才在冻结测试处变红）：" + "；".join(report["problems"])
                            + "。" + FREEZE_SPLIT_ADVICE)
    return {"registered": report["detail"].get("registered"), "uncovered": list(report["detail"].get("uncovered") or [])}


# 收尾给门禁进程显式注入的环境变量白名单：名称 → (用途分类, 说明)。分类决定该变量会不会被测试嵌套执行器继承：
#   driver：只给驱动脚本自己读，驱动的白名单环境不把它转发给单元（单元与嵌套执行器都看不到）；
#   harmless：单元看得到，但不改变任何测试行为；
#   units：驱动显式转发给执行器，单元继承它，测试里嵌套起的临时执行器（``{**os.environ, ...}``）也会继承——候选的测试包必须在导入时把它剔除
#          （``tools/official_client_capture/tests/__init__.py`` 的 ``GATE_EXECUTOR_ENV_NAMES``），否则嵌套执行器会改用外层门禁的持久状态而改变行为。
# 以后收尾要给门禁再加环境变量，必须先在这里登记并给出分类；没登记的注入一律在起跑前报错（上一次升级就是因为给执行器多设了一个变量、
# 测试包没剔除，整轮全量门禁白跑约两小时，三个单元变红）。
GATE_ENV_WHITELIST = {
    "ARM64_VC_ENV": ("driver", "驱动参数文件路径，只给驱动脚本自己读，不转发给单元"),
    "PYTHONDONTWRITEBYTECODE": ("harmless", "不写字节码缓存（树里不能留 __pycache__），不改变测试行为"),
    "UNIT_EXECUTOR_RETRY_LEDGER": ("units", "补跑累计账本目录，驱动转发给执行器、单元继承；测试包须在导入时剔除"),
}
# 候选的测试包里登记“要剔除的门禁注入变量”的文件与常量名。
GATE_SCRUB_FILE = "tools/official_client_capture/tests/__init__.py"
GATE_SCRUB_NAME = "GATE_EXECUTOR_ENV_NAMES"


def gate_injected_env(config, ledger):
    """收尾显式注入门禁进程的环境变量（只列收尾自己加的，不含从操作员环境原样带过去的那些）。"""
    return {
        "ARM64_VC_ENV": config["gate"]["vc_env"],
        "PYTHONDONTWRITEBYTECODE": "1",
        # 补跑累计账本跨收尾共用（见 gate_retry_ledger）；驱动的白名单环境显式转发这一个变量。
        "UNIT_EXECUTOR_RETRY_LEDGER": str(ledger),
    }


def gate_env(config, ledger):
    """门禁进程的完整环境：操作员环境原样带过去，再叠加收尾显式注入的那几个。返回 ``(环境, 显式注入)``。"""
    injected = gate_injected_env(config, ledger)
    return {**os.environ, **injected}, injected


def candidate_scrubbed_env_names(plan):
    """候选提交的测试包登记的“导入时剔除的门禁注入变量”；读不到、解析不了返回 ``None``（调用方按失败关闭处理）。"""
    shown = git(plan["candidate"], "show", f"{plan['candidate_commit']}:{GATE_SCRUB_FILE}", check=False)
    if shown.returncode:
        return None
    try:
        tree = ast.parse(shown.stdout)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == GATE_SCRUB_NAME for target in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)) and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.value.elts):
                return {item.value for item in node.value.elts}
            return None
    return None


def check_gate_env(plan, injected):
    """起跑前核对门禁环境注入：每个注入变量都得在白名单里；会被单元继承的那类，候选测试包必须已登记剔除。

    返回逐变量的核对行 ``[{name, value, role, note, ok, problem}]``；不会抛错，由调用方汇总 ``problem`` 后决定是否停下。"""
    scrubbed = candidate_scrubbed_env_names(plan)
    rows = []
    for name in sorted(injected):
        row = {"name": name, "value": injected[name], "role": None, "note": "", "ok": True, "problem": None}
        entry = GATE_ENV_WHITELIST.get(name)
        if entry is None:
            row.update(ok=False, problem=f"{name} 不在收尾门禁环境注入的白名单里：多余的注入可能被单元与测试嵌套起的执行器继承而改变行为；"
                                         "要注入请先在 GATE_ENV_WHITELIST 登记并分类，必要时让候选测试包在导入时剔除它")
        else:
            row.update(role=entry[0], note=entry[1])
            if entry[0] == "units":
                if scrubbed is None:
                    row.update(ok=False, problem=f"{name} 会被单元继承，但候选提交里读不到测试包的剔除清单 {GATE_SCRUB_FILE}::{GATE_SCRUB_NAME}")
                elif name not in scrubbed:
                    row.update(ok=False, problem=f"{name} 会被单元继承，候选测试包的 {GATE_SCRUB_NAME} 没有登记它：测试里嵌套起的临时执行器会改用外层门禁的持久状态，"
                                                 "相关单元只在收尾门禁里变红（整轮约两小时白跑）；把它加进该清单再重新 prepare")
        rows.append(row)
    return rows


def render_gate_env(rows):
    """把环境注入核对结果打印成人能读的几行（写到 stderr，stdout 留给 JSON 结果）。"""
    lines = [f"收尾门禁环境变量注入核对（白名单 {len(GATE_ENV_WHITELIST)} 项，本次注入 {len(rows)} 项）："]
    for row in rows:
        lines.append(f"  [{'通过' if row['ok'] else '不通过'}] {row['name']}={row['value']}  分类 {row['role'] or '未登记'}"
                     + (f"：{row['note']}" if row["note"] else ""))
        if row["problem"]:
            lines.append(f"      问题：{row['problem']}")
    return "\n".join(lines)


def gate_preflight(plan, ledger):
    """全量门禁起跑前的两道秒级预检：①候选里没有未承接的冻结路径；②注入的环境变量都在白名单、会被继承的都已被测试包剔除。

    两道都过才返回核对摘要；任一道不过就抛 ``CloseoutError``（把两道的问题一起报全），此时门禁还没起、没写任何“开始”占位。"""
    config = plan["basis"]["config"]
    problems, summary = [], {}
    report = frozen_coverage(plan["candidate"])
    summary["frozen_coverage"] = {"registered": report["detail"].get("registered"), "uncovered": list(report["detail"].get("uncovered") or []),
                                  "passed": report["passed"]}
    if not report["passed"]:
        problems.append("冻结覆盖：" + "；".join(report["problems"]) + "。" + FREEZE_SPLIT_ADVICE)
    rows = check_gate_env(plan, gate_injected_env(config, ledger))
    summary["injected_env"] = rows
    print(render_gate_env(rows), file=sys.stderr)
    problems += [row["problem"] for row in rows if not row["ok"]]
    if problems:
        raise CloseoutError("收尾门禁起跑前预检不通过，全量门禁没有起：" + "；".join(f"（{index}）{text}" for index, text in enumerate(problems, 1)))
    return summary


def inherit_veto(plan, run_ids):
    """逐单元承接的类型变化一票否决（补承接视图发现不了的“同名文件变目录”，已知缺口 R2）：每个来源运行的提交到收尾候选提交之间有类型变化、
    符号链接、子模块或 ``.gitmodules`` 变更，或来源提交解析不出、变更集读不出，就拒绝承接（失败关闭；要完整执行就去掉 ``gate.unit_inherit``）。"""
    config = plan["basis"]["config"]
    # 变更集在候选克隆里取：它是权威仓库的完整克隆（含全部历史）再加上候选提交，来源提交与候选提交都在里面。
    reasons = _inherit_predict().veto_for_runs(safety.plain(config["gate"]["record_store"]), run_ids, plan["candidate"], plan["candidate_commit"])
    if reasons:
        raise CloseoutError("收尾逐单元承接被一票否决（来源提交到候选提交之间的类型变化无法被承接视图发现，本轮不承接任何单元）：" + "；".join(reasons[:3]))


# ---------------------------------------------------------------------------
# 收尾逐单元承接：请求后置（第 17 项正式启用）。请求绑定候选提交，所以只能在 prepare 之后生成，再由 gates 绑定进方案的占位
# ---------------------------------------------------------------------------

# 承接请求的绑定占位（``journal/gates-inherit-request.json``）的阶段名。
INHERIT_REQUEST_PHASE = "inherit-request"


def plan_inherit(plan):
    """本方案实际生效的承接配置 ``{"request": 请求文件, "stage": 阶段}``；方案没配置承接时返回 ``None``。

    原写法（``gate.unit_inherit`` 里带 ``request``）直接用配置；请求后置的写法（只有 ``stage``）以 ``gates`` 绑定时写下的占位为准，并复核请求文件
    仍是绑定时的那份字节。要承接却还没绑定请求，一律失败关闭——不能退化成按“没配置承接”去校验一个重跑结果。"""
    inherit = gate_inherit(plan["basis"]["config"])
    if not inherit:
        return None
    if "request" in inherit:
        return inherit
    placeholder = journal_read(plan, "gates", INHERIT_REQUEST_PHASE)
    if placeholder is None:
        raise CloseoutError("方案要求逐单元承接，但还没有绑定承接请求：先 inherit-propose、inherit-request，再 gates --unit-inherit-request <请求>")
    if safety.file_binding(placeholder["request"]["path"]) != placeholder["request"]:
        raise CloseoutError("已绑定的承接请求文件被改动，拒绝继续")
    return {"request": placeholder["request"]["path"], "stage": inherit["stage"]}


def check_inherit_request(plan, request_path, *, now=None):
    """起跑前对照本方案复核一份承接请求（只读，不写任何占位）；不一致直接抛错。

    请求的上下文由执行器按“当时的候选提交、客户端、阶段、记录库、门禁组合”现算并签批；运行时执行器在三个复核点重新现算上下文，只要与请求里的
    不逐字段相同就**静默回退成整轮重跑**——约两小时的全量门禁白白失去承接。所以这里先把请求绑定的提交、客户端、阶段、记录库、门禁组合与本方案
    逐项对照（最常见的错：请求是针对别的提交生成的），再复核两份批准此刻仍在有效期内、请求引用的原文件没被改，并过一遍类型变化一票否决。"""
    from tools.ci import entry_gates
    config = plan["basis"]["config"]
    inherit = gate_inherit(config)
    if not inherit:
        raise CloseoutError("方案没有配置 gate.unit_inherit，不接受承接请求（要逐单元承接须在 prepare 的配置里写 gate.unit_inherit.stage）")
    module = _unit_inherit()
    try:
        reference, request = module._read_request(safety.plain(request_path))
        context = module.context_check(module.fs.replay_ref(request["context"]))
        sources = module.sources_check(request["sources"])
        approvals = tuple(module.fs.replay_ref(item) for item in request["approvals"])
        module.approvals_check(approvals, module.binding_sha256(context, sources), module.current_time() if now is None else now)
    except (*module.ERRORS, safety.SafetyError) as error:
        raise CloseoutError(f"承接请求不可用：{error}") from error
    if context["commit"] != plan["candidate_commit"]:
        raise CloseoutError(f"承接请求绑定的提交 {context['commit'][:12]} 不是本方案的收尾候选提交 {plan['candidate_commit'][:12]}：请求必须在 prepare 之后、"
                            "针对候选提交生成（inherit-propose 再 inherit-request），否则执行器会静默回退成整轮重跑")
    expected = {"client": client_of(config), "stage": inherit["stage"], "profile": "full-gates",
                "record_store": str(safety.plain(config["gate"]["record_store"]).resolve()), "gates": sorted(entry_gates.profile_gates("full-gates"))}
    mismatched = [f"{key}（请求 {context[key]!r}，本方案 {value!r}）" for key, value in expected.items() if context[key] != value]
    if request["reuse_enabled"] is not True:
        mismatched.append("reuse_enabled（请求没有开启承接）")
    if mismatched:
        raise CloseoutError("承接请求与本收尾方案不一致，拒绝：" + "；".join(mismatched))
    inherit_veto(plan, set(sources["runs"]))
    return {"request": reference, "commit": context["commit"], "stage": context["stage"], "context_sha256": module.fs.digest(context),
            "sources_sha256": module.fs.digest(sources), "units": len(sources["units"]), "runs": len(sources["runs"])}


def bind_inherit_request(plan, request_path, *, write=True):
    """把 ``gates --unit-inherit-request`` 给的请求绑定进方案，返回生效的承接配置（没配置承接时返回 ``None``）。

    * 方案没配置承接却给了请求：拒绝（要承接须在配置里写 ``gate.unit_inherit.stage``）；
    * 原写法（配置里带 ``request``）：不给请求就用配置的，给了必须是同一个文件；
    * 请求后置的写法：必须给请求（或此前已绑定过占位）；复核通过后把请求的路径与摘要写进 ``journal/gates-inherit-request.json``（不覆盖），
      已有占位时给的请求必须是同一份字节。``write=False`` 只复核不落占位（``gates --preflight-only`` 用）。"""
    config = plan["basis"]["config"]
    inherit = gate_inherit(config)
    if not inherit:
        if request_path is not None:
            raise CloseoutError("方案没有配置 gate.unit_inherit，不接受 --unit-inherit-request")
        return None
    if "request" in inherit:
        if request_path is not None and safety.plain(request_path) != safety.plain(inherit["request"]):
            raise CloseoutError("配置里已写死承接请求，与 --unit-inherit-request 给的不是同一个文件")
        return inherit
    existing = journal_read(plan, "gates", INHERIT_REQUEST_PHASE)
    if request_path is None:
        if existing is None:
            raise CloseoutError("方案要求逐单元承接，但没有给承接请求：先 inherit-propose、inherit-request，再 gates --unit-inherit-request <请求>"
                                "（要完整重跑请在新方案里去掉 gate.unit_inherit）")
        return plan_inherit(plan)
    checked = check_inherit_request(plan, request_path)
    if existing is not None:
        if existing["request"] != checked["request"]:
            raise CloseoutError("本方案已绑定另一份承接请求，不能换：在新工作根里重来")
    elif write:
        journal(plan, "gates", INHERIT_REQUEST_PHASE, {**checked, "candidate_commit": plan["candidate_commit"]})
    return {"request": checked["request"]["path"], "stage": inherit["stage"]}


def inherit_propose(plan_path):
    """对收尾候选提交写承接提案（执行器只判定、不跑单元，几分钟）：上下文绑定候选提交，来源集合是记录库里能证明可承接的单元。

    提案目录在 ``<工作根>/inherit/proposal``（必须尚不存在，不覆盖）。提案里的 ``binding_sha256`` 交两类批准签字，之后用 ``inherit-request`` 组装请求。"""
    plan = load_plan(plan_path)
    config = plan["basis"]["config"]
    inherit = gate_inherit(config)
    if not inherit:
        raise CloseoutError("方案没有配置 gate.unit_inherit，不需要承接提案")
    work = Path(config["work_root"])
    with run_lock(work):
        ledger = gate_retry_ledger(config)
        gate_preflight(plan, ledger)
        root = work / "inherit"
        proposal, out = root / "proposal", root / "propose-out"
        if proposal.exists() or out.exists():
            raise CloseoutError("承接提案目录已存在，不覆盖；要重新生成请在新工作根里重来")
        root.mkdir(mode=0o700, exist_ok=True)
        argv = ["bash", config["gate"]["driver"], "--profile", "full-gates", "--mode", "unit-inherit", "--unit-inherit-stage", inherit["stage"],
                "--unit-inherit-propose", str(proposal), "--out", str(out), "--work", config["gate"]["work"],
                "--record-store", config["gate"]["record_store"], plan["bundle"]["path"],
                client_table.spec(client_of(config))["closeout_candidate_branch"], plan["candidate_commit"], config["gate"]["node_modules"]]
        env, _injected = gate_env(config, ledger)
        with (root / "propose.log").open("xb") as log:
            os.chmod(log.name, 0o600)
            result = subprocess.run(argv, cwd=config["repo"], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        if result.returncode or not (proposal / "binding.json").is_file():
            raise CloseoutError("承接提案没有生成（执行器失败，或记录库里没有任何可承接的单元：这种情况整轮都得重跑，不如去掉 gate.unit_inherit）；"
                                f"日志 {root / 'propose.log'}")
        binding, context = safety.read(proposal / "binding.json"), safety.read(proposal / "context.json")
        if context.get("commit") != plan["candidate_commit"]:
            raise CloseoutError(f"提案绑定的提交 {str(context.get('commit'))[:12]} 不是本方案的收尾候选提交，拒绝使用")
        return {"status": "proposal_written", "proposal": str(proposal), "commit": context["commit"], "binding_sha256": binding["binding_sha256"],
                "units": binding["units"], "runs": binding["runs"], "log": str(root / "propose.log")}


def inherit_request(plan_path, *, proposal, approvals, output=None, clock=None, revocation=None, clock_source=None,
                    campaign_status_source=None, max_age_hours=None):
    """提案＋两份批准（对提案里的 ``binding_sha256`` 签字）＋校时与撤销查询凭证，组装成绑定候选提交的承接请求，并立刻对照方案复核。

    校时与撤销查询凭证没给就当场取（校时来源缺省 chronyc；ARM64 没有 chronyc，要传 ``file:<绝对路径>``）。批准必须在提案之后签、有效期要盖住凭证采样时刻。
    请求写到 ``output``（缺省 ``<工作根>/inherit/request.json``，不覆盖）；对不上本方案（提案是别的提交的、批准过期……）直接拒绝并报原因。"""
    plan = load_plan(plan_path)
    config = plan["basis"]["config"]
    inherit = gate_inherit(config)
    if not inherit:
        raise CloseoutError("方案没有配置 gate.unit_inherit，不需要承接请求")
    module = _unit_inherit()
    proposal = safety.plain(proposal)
    try:
        committed = safety.read(proposal / "context.json").get("commit")
    except safety.SafetyError as error:
        raise CloseoutError(f"承接提案不可用：{error}") from error
    if committed != plan["candidate_commit"]:
        raise CloseoutError(f"提案绑定的提交 {str(committed)[:12]} 不是本方案的收尾候选提交 {plan['candidate_commit'][:12]}：用 inherit-propose 对候选提交重新生成")
    work = Path(config["work_root"])
    with run_lock(work):
        root = work / "inherit"
        root.mkdir(mode=0o700, exist_ok=True)
        target = safety.plain(output) if output is not None else root / "request.json"
        try:
            sources = module.sources_check(safety.read(proposal / "sources.json"))
            now = module.current_time()
            if clock is None:
                clock = root / "clock.json"
                module._write_once(clock, module.sample_clock(clock_source or module.DEFAULT_CLOCK_SOURCE, now))
            if revocation is None:
                revocation = root / "revocation.json"
                module._write_once(revocation, module.query_revocation(safety.plain(config["gate"]["record_store"]), sorted(sources["runs"]),
                                                                       inherit["stage"], now, campaign_status_source))
            module.assemble_request(proposal, clock=clock, revocation=revocation, approvals=list(approvals), reuse_enabled=True, output=target,
                                    max_age_hours=max_age_hours, clock_source=clock_source, campaign_status_source=campaign_status_source)
        except (*module.ERRORS, safety.SafetyError) as error:
            raise CloseoutError(f"承接请求组装失败：{error}") from error
    try:
        checked = check_inherit_request(plan, target)
    except CloseoutError as error:
        raise CloseoutError(f"请求已写出但与本方案不符，不得使用（{target}）：{error}") from error
    return {"status": "request_written", **checked}


def validate_config(config):
    required = {"schema_version", "repo", "work_root", "guide", "material", "terminal_facts", "terminal_relative",
                "tag", "gate", "cleanup", "push", "deployment_check"}
    if set(config) != required or config["schema_version"] not in CLIENT_SCHEMAS:
        raise CloseoutError("收尾配置字段不完整或存在未知字段")
    repo, work = safety.plain(config["repo"]), safety.plain(config["work_root"])
    if work.is_relative_to(repo) or repo.is_relative_to(work):
        raise CloseoutError("收尾工作根必须与权威仓库分离")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", config["tag"]):
        raise CloseoutError("收尾标识非法")
    for key in ("guide", "material", "terminal_facts"):
        safety.file_binding(config[key])
    inside(repo, config["terminal_relative"])
    gate = config["gate"]
    required_gate, optional_gate = {"driver", "vc_env", "node_modules"}, {"work", "record_store", "unit_inherit"}
    if not required_gate <= set(gate) or set(gate) - required_gate - optional_gate:
        raise CloseoutError("门禁必须提供受管驱动、参数和锁定前端依赖")
    for key in ("driver", "vc_env"):
        safety.file_binding(gate[key])
    safety.plain(gate["node_modules"])
    validate_gate_inherit(config, repo, work)
    for name in ("cleanup", "deployment_check"):
        operation = config[name]
        if name == "cleanup" and operation.get("mode") == "defer":
            if set(operation) != {"mode", "reason", "inputs"} or not str(operation["reason"]).strip():
                raise CloseoutError("延期清理必须登记原因和输入凭证")
            if not operation["inputs"]:
                raise CloseoutError("延期清理缺少凭证")
            for path in operation["inputs"]:
                safety.file_binding(path)
            continue
        expected = {"script", "arguments", "inputs", "result"}
        if name == "cleanup":
            expected |= {"mode", "targets", "backup", "restore_check"}
            if operation.get("mode") != "execute" or not operation.get("targets"):
                raise CloseoutError("清理必须明确操作模式和目标")
            for key in ("backup", "restore_check"):
                verify_binding(operation.get(key))
        if set(operation) != expected:
            raise CloseoutError(f"{name} 必须登记脚本、参数、输入凭证和结果路径")
        safety.file_binding(operation["script"])
        if not isinstance(operation["arguments"], list) or not all(isinstance(arg, str) for arg in operation["arguments"]):
            raise CloseoutError("操作参数必须是字符串数组，不能是 shell 文本")
        if not isinstance(operation["inputs"], list) or not operation["inputs"]:
            raise CloseoutError("清理决定及部署核验必须有输入凭证")
        for path in operation["inputs"]:
            safety.file_binding(path)
        if not safety.plain(operation["result"]).is_relative_to(work):
            raise CloseoutError("操作结果必须位于本轮工作根内")
    push = config["push"]
    if set(push) != {"remote", "ref", "expected_tip"} or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", push["remote"]):
        raise CloseoutError("推送必须固定远端、分支及原完整提交")
    if not push["ref"].startswith("refs/heads/") or git(repo, "check-ref-format", push["ref"], check=False).returncode:
        raise CloseoutError("只允许推送明确的分支引用")
    if not re.fullmatch(r"[0-9a-f]{40}", push["expected_tip"]):
        raise CloseoutError("推送原提交必须是完整对象 ID")
    return repo, work


def config_bindings(config):
    paths = [config[name] for name in ("guide", "material", "terminal_facts")]
    paths += [config["gate"][name] for name in ("driver", "vc_env")]
    paths += [str(Path(config["gate"]["node_modules"]) / "pnpm-lock.yaml")]
    if gate_inherit(config) and "request" in config["gate"]["unit_inherit"]:
        paths.append(config["gate"]["unit_inherit"]["request"])
    paths.append(str(Path(__file__).resolve()))
    if client_of(config) != "codex":
        paths += [str(Path(__file__).resolve().parent / name) for name in CLIENT_TOOL_FILES]
    # 入口脚本会加载同目录工具；只绑定一个 shell 文件不足以防止门禁实现漂移。
    driver = Path(config["gate"]["driver"])
    if driver.name != "entry-gates.sh":
        raise CloseoutError("收尾门禁必须使用标准 entry-gates.sh")
    paths += [str(path) for path in driver.parent.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".sh", ".json"}]
    for key in ("cleanup", "deployment_check"):
        operation = config[key]
        paths += operation["inputs"]
        if "script" in operation:
            paths.append(operation["script"])
        for proof in ("backup", "restore_check"):
            if proof in operation:
                paths.append(operation[proof]["path"])
    return {path: safety.file_binding(path)["sha256"] for path in sorted(set(paths))}


def prepare(config, *, dry_run=False):
    """构建私有候选快照；发布操作不在此阶段执行，原仓库逐字保持不变。"""
    started = datetime.now(timezone.utc).isoformat()
    cleanup_source = deepcopy(config["cleanup"])
    config, cleanup_context = resolve_cleanup(config)
    repo, work = validate_config(config)
    client, row = client_of(config), client_table.spec(client_of(config))
    bound = config_bindings(config)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if git(repo, "status", "--porcelain").stdout.strip():
        raise CloseoutError("权威仓库必须先提交并保持干净，才能绑定收尾方案")
    # 冻结覆盖预检第一道（OPT-17，秒级、只读，dry-run 也跑）：权威仓库头本身不能带未承接的冻结路径。合并候选链进收尾分支时两边都改了同一冻结文件，
    # 自动合并出的内容没有承接收据，收尾候选就会带着它，全量门禁约两小时后才在冻结测试处变红；这里先拦，错误里直接给拆提交的办法。
    head_coverage = require_frozen_coverage(repo, subject="权威仓库头")
    updates = source_updates(repo, Path(config["guide"]).read_text(), client=client)
    material = safety.read(config["material"])
    if not all(material.get(key) for key in ("campaign_id", "candidate_id", "target_version", "classification_sha256", "official_stage_sha256")):
        raise CloseoutError("指南缺少正式 VC-2 与 VC-1 材料绑定")
    if client != "codex" and material.get("client") != client:
        raise CloseoutError("指南材料不是本客户端的封存链材料")
    if collect_guide_material(material["campaign_path"], material["candidate_id"], client=client) != material:
        raise CloseoutError("指南材料不能从封存链重放")
    facts = safety.read(config["terminal_facts"])
    if facts.get("target", {}).get("version") != material["target_version"] or facts.get("campaign_chain", [{}])[-1].get("campaign_id") != material["campaign_id"]:
        raise CloseoutError("终态事实和指南材料不是同一 Campaign／版本")
    terminal_text = terminal_for(client)(repo, facts, config["terminal_relative"])
    terminal_path = inside(repo, config["terminal_relative"])
    if terminal_path.exists():
        raise CloseoutError("终态坐标已存在，不允许重签旧升级；使用下一轮真实事实")
    for existing in terminal_path.parent.glob(row["terminal_receipt_prefix"] + "*_TERMINAL_STATE_RECEIPT.json"):
        if safety.read(existing).get("target", {}).get("version") == material["target_version"]:
            raise CloseoutError("目标版本已有终态收据，不允许换路径重复签发")
    basis = {"config": config, "input_files": bound, "base_commit": head, "branch": branch,
             "source_tree": git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
             "remote_url_sha256": sha(git(repo, "remote", "get-url", config["push"]["remote"]).stdout.encode()),
             "source_updates": updates, "terminal_text": terminal_text}
    if cleanup_context is not None:
        basis.update(cleanup_source=cleanup_source, cleanup_context=cleanup_context)
    key = digest(basis)
    if dry_run:
        return {"status": "dry_run", "input_sha256": key, "changed_paths": sorted([*updates, config["terminal_relative"]]),
                "manual_gates": ["guide-review", "cleanup-authorization", "release-publication"], "work_root": str(work),
                "frozen_coverage": head_coverage}
    with run_lock(work):
        path = work / "plan.json"
        if path.exists():
            plan = safety.read(path)
            if plan.get("input_sha256") != key:
                raise CloseoutError("本轮输入已变化，必须新建收尾工作根")
            return plan
        candidate = work / "candidate"
        if candidate.exists():
            raise CloseoutError("发现准备中断的候选目录，保留现场并使用新工作根")
        subprocess.run(["git", "clone", "--no-hardlinks", "--quiet", str(repo), str(candidate)], check=True)
        git(candidate, "checkout", "--detach", head)
        for name in ("user.name", "user.email"):
            value = git(repo, "config", "--get", name).stdout.strip()
            if not value:
                raise CloseoutError("候选提交需要已有的 Git 作者配置")
            git(candidate, "config", name, value)
        for relative, update in updates.items():
            inside(candidate, relative).write_text(update["text"])
        write_text_once(inside(candidate, config["terminal_relative"]), terminal_text)
        if client != "codex":
            _claude().verify_candidate(candidate, config["terminal_relative"], base=_base())
        from tools.upstream_merge.freeze import generate_freeze_successor
        freeze_relative = "docs/egress/maintenance/closeout-" + config["tag"] + "-freeze-successor.json"
        freeze = generate_freeze_successor(candidate, head, None, inside(candidate, freeze_relative), tag=config["tag"],
                                           reason="收尾候选：指南及来源摘要连锁，正式发布仍待人工批准和完整门禁", dry_run=True)
        if freeze.get("required_manual_actions") or freeze.get("deleted_frozen_paths"):
            raise CloseoutError("冻结链含未处理事项，禁止自动收尾")
        outputs = [*updates, config["terminal_relative"]]
        if freeze.get("transitions"):
            document = {k: v for k, v in freeze.items() if k not in {"dry_run", "output"}}
            write_text_once(inside(candidate, freeze_relative), json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            outputs.append(freeze_relative)
        git(candidate, "add", "--", *outputs)
        git(candidate, "commit", "--no-gpg-sign", "-m", "收尾候选：" + config["tag"])
        commit = git(candidate, "rev-parse", "HEAD").stdout.strip()
        # 冻结覆盖预检第二道（OPT-17）：出候选提交后立刻复查——全量门禁实际跑的就是这个提交；候选自己生成的冻结收据没盖住的路径也在这里拦下。
        # 不通过时不写方案、不建 bundle，候选目录保留现场（要重来就用新工作根）。
        require_frozen_coverage(candidate, subject="收尾候选提交 " + commit[:12])
        git(candidate, "branch", row["closeout_candidate_branch"], commit)
        bundle = work / "candidate.bundle"
        git(candidate, "bundle", "create", str(bundle), row["closeout_candidate_branch"])
        candidate_manifest = tree_manifest(candidate)
        plan = {"schema_version": config["schema_version"], "input_sha256": key, "basis": basis, "candidate": str(candidate),
                "candidate_commit": commit, "candidate_tree": git(candidate, "rev-parse", "HEAD^{tree}").stdout.strip(),
                "candidate_files": candidate_manifest, "bundle": safety.file_binding(bundle), "outputs": sorted(outputs),
                "candidate_only": True, "prepared_at_utc": datetime.now(timezone.utc).isoformat()}
        plan["started_at_utc"] = started
        plan["review_sha256"] = digest(plan)
        if config_bindings(config) != bound:
            raise CloseoutError("准备期间输入文件变化，候选不可用于发布")
        check_cleanup_context(plan)
        safety.write_once(path, plan)
        return plan


def verify_binding(binding):
    """按完整文件摘要复核外部证据，不信任仅有路径或布尔标记的结论。"""
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"} or not SHA256.fullmatch(str(binding["sha256"])):
        raise CloseoutError("证据必须登记普通文件绝对路径和完整 SHA-256")
    if safety.file_binding(binding["path"]) != binding:
        raise CloseoutError("收尾证据摘要漂移")
    return safety.plain(binding["path"])


def load_plan(path):
    """所有入口重新核对候选、bundle 和输入，外部修改必须新建方案及批准。"""
    plan = safety.read(path)
    if plan.get("schema_version") not in CLIENT_SCHEMAS or not plan.get("candidate_only"):
        raise CloseoutError("不是受管候选方案")
    if plan.get("review_sha256") != digest({k: v for k, v in plan.items() if k != "review_sha256"}):
        raise CloseoutError("方案自摘要不一致")
    basis = plan["basis"]
    if plan["input_sha256"] != digest(basis):
        raise CloseoutError("方案输入摘要不一致")
    config = basis["config"]
    repo, work = validate_config(config)
    if config["schema_version"] != plan["schema_version"]:
        raise CloseoutError("不是受管候选方案")
    if safety.plain(path) != work / "plan.json" or Path(plan["candidate"]) != work / "candidate":
        raise CloseoutError("方案与工作根绑定不一致")
    if config_bindings(config) != basis["input_files"]:
        raise CloseoutError("收尾输入或工具已改变")
    check_cleanup_context(plan)
    candidate = safety.plain(plan["candidate"])
    if (git(candidate, "rev-parse", "HEAD").stdout.strip() != plan["candidate_commit"]
            or git(candidate, "rev-parse", "HEAD^{tree}").stdout.strip() != plan["candidate_tree"]
            or git(candidate, "status", "--porcelain").stdout.strip()
            or tree_manifest(candidate) != plan["candidate_files"]):
        raise CloseoutError("候选源码或制品被改写")
    verify_binding(plan["bundle"])
    if git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() != basis["branch"]:
        raise CloseoutError("权威仓库分支已变化")
    if git(repo, "status", "--porcelain").stdout.strip():
        raise CloseoutError("权威仓库有未提交改动，停止恢复")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if head not in {basis["base_commit"], plan["candidate_commit"]}:
        raise CloseoutError("权威仓库提交已变化，禁止覆盖")
    if sha(git(repo, "remote", "get-url", config["push"]["remote"]).stdout.encode()) != basis["remote_url_sha256"]:
        raise CloseoutError("远端地址已改变")
    return plan


def operation_key(plan, name):
    return digest({"review_sha256": plan["review_sha256"], "operation": name})


def journal(plan, name, phase, value):
    """每一步先持久化意向、再追加结果；旧记录绝不覆盖。"""
    path = Path(plan["basis"]["config"]["work_root"]) / "journal" / (name + "-" + phase + ".json")
    document = {"operation_key": operation_key(plan, name), "review_sha256": plan["review_sha256"], **value}
    safety.write_once(path, document)
    return document


def journal_read(plan, name, phase):
    path = Path(plan["basis"]["config"]["work_root"]) / "journal" / (name + "-" + phase + ".json")
    if not path.exists():
        return None
    value = safety.read(path)
    if value.get("operation_key") != operation_key(plan, name) or value.get("review_sha256") != plan["review_sha256"]:
        raise CloseoutError("步骤占位不是本轮方案")
    return value


def check_gate_result(plan):
    """完整门禁集合与标准命令逐项核对，不能把某条命令退出零当成全量通过。

    配置了 ``gate.unit_inherit`` 时（第 17 项）运行模式是 ``unit-inherit``，允许逐单元承接：承接项必须被运行清单、决策收据与
    ``unit_inherit.verify_published`` 重放证明，测试树坐标必须是固定坐标；没配置时一字不变地要求 ``re-execute`` 且没有承接单元。"""
    from tools.ci import entry_gates
    config = plan["basis"]["config"]
    inherit = plan_inherit(plan)  # 请求后置的写法以 gates 绑定的占位为准；没绑定就失败关闭
    mode = "unit-inherit" if inherit else "re-execute"
    root = Path(config["work_root"]) / "gates"
    entry = safety.read(root / "entry-gates.json")
    expected = set(entry_gates.profile_gates("full-gates"))
    rows = entry.get("gates", [])
    if (entry.get("schema_version") != entry_gates.ENTRY_SUMMARY_SCHEMA or entry.get("profile") != "full-gates"
            or entry.get("status") != "passed" or entry.get("mode") != mode
            or entry.get("source", {}).get("tree_head") != plan["candidate_commit"]
            or entry.get("source", {}).get("commit") != plan["candidate_commit"]
            or len(rows) != len(expected) or {r.get("gate_id") for r in rows} != expected):
        raise CloseoutError("收尾全量门禁未通过或候选提交／集合不匹配")
    files = [root / "entry-gates.json", root / "gates-manifest.json"]
    manifest = safety.read(files[-1])
    if (manifest.get("schema_version") != entry_gates.GATES_SCHEMA or manifest.get("profile") != "full-gates"
            or {r.get("gate_id") for r in manifest.get("gates", [])} != expected):
        raise CloseoutError("门禁执行清单不完整")
    # 按客户端筛选（优化第 9 项）：收尾 full-gates 是发布前最后一道全量门禁，保持全集（拍板）。带筛选的运行只是某个客户端计划的子集，
    # 清单里的 selection、总摘要里的 client_selection 任何一处出现都不能当作收尾全量通过——哪怕筛选时回退成了全集，收尾也要求干净地不带筛选。
    if manifest.get("selection") is not None or entry.get("client_selection") is not None:
        raise CloseoutError("收尾全量门禁是按客户端筛选的子集，不是全集：收尾必须全集重跑（不带 --gate-scope）")
    gate_retry_passed = set()
    gate_inherited = set()
    for row in rows:
        if row.get("status") != "passed" or row.get("exit_code") != 0:
            raise CloseoutError("存在未通过的收尾门禁")
        path = inside(root, row["gate_json"])
        result = safety.read(path)
        gate_retry_passed.update(result.get("retry_passed_units") or [])
        gate_inherited.update(result.get("inherited_units") or [])
        command, directory = entry_gates.GATE_COMMANDS[row["gate_id"]]
        if (result.get("gate_id") != row["gate_id"] or result.get("status") != "passed"
                or result.get("exit_code") != 0 or result.get("failed_units")
                or (result.get("inherited_units") and not inherit) or result.get("mode") != mode
                or result.get("tree_head") != plan["candidate_commit"]
                or result.get("command") != command or result.get("working_directory") != directory
                or not result.get("started_at_utc") or not result.get("completed_at_utc")):
            raise CloseoutError("门禁分项命令、结果或提交身份不一致")
        files.append(path)
    executor = safety.plain(entry["executor_summary"])
    if not executor.is_relative_to(root):
        raise CloseoutError("执行器结果不在本轮目录")
    summary = safety.read(executor)
    allowed_dispositions = {"executed", "inherited"} if inherit else {"executed"}
    if (summary.get("schema_version") != entry_gates.GATES_SUMMARY_SCHEMA or summary.get("status") != "passed"
            or summary.get("mode") != mode or summary.get("units_not_run") or summary.get("failed_units")
            or any(row.get("passed") is not True or row.get("disposition", "executed") not in allowed_dispositions
                   or row.get("exit_code") != 0 or row.get("orphans") != 0 or row.get("timed_out")
                   or row.get("missing") for row in summary.get("units", []))
            or not summary.get("units")):
        raise CloseoutError("执行器存在未运行、失败或承接单元")
    if summary.get("client_selection") is not None:
        raise CloseoutError("执行器汇总带按客户端筛选的标记：收尾全量门禁必须全集")
    inherited_ids = {row["unit_id"] for row in summary["units"] if row.get("disposition", "executed") == "inherited"}
    if inherit:
        if gate_inherited - inherited_ids:
            raise CloseoutError("门禁记录登记的承接单元不在执行器汇总的承接单元里")
        files.extend(check_inherited_units(plan, root, summary, inherited_ids))
    # 失败单元补跑（优化第 2 项）：补跑通过的单元算通过，但必须带自洽的标注、两次日志都在盘上且摘要相符、汇总与门禁记录一致；
    # 不静默放行——任何一处对不上都按收尾门禁未通过处理。发布汇报从 gate_retry_summary 取单列的补跑通过名单。
    retry_problems = retry_policy.summary_problems(summary, require_files=True)
    retry_block = summary.get("retry") if isinstance(summary.get("retry"), dict) else {}
    if retry_problems or sorted(retry_block.get("retry_passed") or []) != sorted(gate_retry_passed):
        raise CloseoutError("收尾门禁的补跑标注不自洽或与门禁记录不一致：" + "；".join(retry_problems[:5] or ["补跑通过名单不一致"]))
    if retry_block.get("retry_failed") or retry_block.get("retry_denied"):
        raise CloseoutError("收尾门禁存在补跑仍失败或被拒绝补跑的单元")
    group_ids = {row["group_id"] for row in manifest.get("test_groups", [])}
    groups = summary.get("test_groups", {})
    if set(groups) != group_ids:
        raise CloseoutError("测试组全集不一致")
    expected_units = {row["unit_id"] for row in manifest.get("units", [])}
    for group in groups.values():
        if (group.get("status") != "passed" or not group.get("expected_tests")
                or group.get("expected_tests") != group.get("reported_tests")
                or any(group.get("full_set", {}).get(key) for key in ("missing", "duplicated", "unexpected", "units_not_run"))
                or any(group.get("counts", {}).get(key) for key in ("failed", "error", "unexpected_success"))):
            raise CloseoutError("测试组缺报、重复、失败或存在额外用例")
        expected_units.update(group["units"])
    actual_units = [row["unit_id"] for row in summary["units"]]
    if len(actual_units) != len(set(actual_units)) or set(actual_units) != expected_units:
        raise CloseoutError("执行器单元全集不一致")
    files.append(executor)
    # 逐单元日志也进入不可变证据索引；重启后必须再次核对同一批字节。
    for row in summary["units"]:
        log = safety.plain(row["log"])
        if not log.is_relative_to(root):
            raise CloseoutError("单元日志不在本轮门禁目录")
        files.append(log)
        # 补跑通过的单元：首次失败那次的日志也是证据，同样进索引（两次日志都留）。
        first_log = ((row.get("retry") or {}).get("first_attempt") or {}).get("log")
        if row.get("retry") and first_log:
            first = safety.plain(first_log)
            if not first.is_relative_to(root):
                raise CloseoutError("补跑单元的首次尝试日志不在本轮门禁目录")
            files.append(first)
    return {str(path): safety.file_binding(path)["sha256"] for path in sorted(set(files))}


def _inherit_manifest(plan, root, summary):
    """执行器汇总指向的运行清单：在本轮门禁目录里、摘要与汇总登记的相符、模式是逐单元承接、运行 ID 与汇总一致、记录库就是配置的那一个。"""
    config = plan["basis"]["config"]
    reference = summary.get("unit_manifest") or {}
    path = safety.plain(reference.get("path") or "")
    if not path.is_relative_to(root):
        raise CloseoutError("运行清单不在本轮门禁目录")
    manifest = safety.read(path)
    store = safety.plain(config["gate"]["record_store"])
    if (manifest.get("manifest_sha256") != reference.get("manifest_sha256") or manifest.get("mode") != "unit-inherit"
            or manifest.get("run_id") != summary.get("run_id") or manifest.get("record_store") != str(store)
            or summary.get("record_store") != str(store)):
        raise CloseoutError("运行清单与执行器汇总不一致，或不是配置记录库里的逐单元承接运行")
    return path, manifest, store


def check_inherited_units(plan, root, summary, inherited_ids):
    """收尾逐单元承接的校验（第 17 项）：承接单元与运行清单逐一相符，运行清单经 ``unit_inherit.verify_published(purpose="closeout")`` 历史重放
    通过（决策收据三个复核点、批准、来源运行、每条承接记录重做 ``check_record``），承接项的测试树坐标是固定坐标。返回要进不可变证据索引的文件。"""
    config = plan["basis"]["config"]
    path, manifest, store = _inherit_manifest(plan, root, summary)
    from_manifest = {entry["unit_id"] for entry in manifest.get("units", []) if entry.get("disposition") == "inherited"}
    if from_manifest != inherited_ids:
        raise CloseoutError("运行清单与执行器汇总的承接单元集合不同")
    module = _unit_inherit()
    try:
        module.verify_published(manifest, store, purpose="closeout", historical=True)
    except module.ContractError as error:
        raise CloseoutError(f"收尾承接的决策收据或来源核验不通过：{error}") from error
    # 独立于执行器的一票否决复核：承接项的来源运行到候选提交之间不能有类型变化（门禁结果被改写或执行器漏判时在这里拦下）。
    if inherited_ids:
        inherit_veto(plan, {(entry.get("basis") or {}).get("run_id") for entry in manifest["units"] if entry.get("disposition") == "inherited"})
    tree = str(safety.plain(config["gate"]["work"]) / "test-tree")
    for entry in manifest["units"]:
        check = entry.get("inherit_check")
        if entry.get("disposition") == "inherited" and isinstance(check, dict) and check.get("repo_root") != tree:
            raise CloseoutError(f"承接单元 {entry['unit_id']} 的测试树坐标不是固定坐标：{check.get('repo_root')}")
    files = [path]
    decision = (manifest.get("unit_inherit_decision") or {}).get("path")
    if decision:
        files.append(safety.plain(decision))
    return files


def gate_inherit_summary(plan):
    """收尾收据逐项列出的承接信息（第 17 项）：每个承接单元的来源运行、来源记录摘要与视图摘要，以及被重跑的单元和原因。没有按承接运行时返回 None。"""
    config = plan["basis"]["config"]
    if not gate_inherit(config):
        return None
    check_gate_result(plan)
    bound = journal_read(plan, "gates", INHERIT_REQUEST_PHASE)
    root = Path(config["work_root"]) / "gates"
    entry = safety.read(root / "entry-gates.json")
    _path, manifest, _store = _inherit_manifest(plan, root, safety.read(safety.plain(entry["executor_summary"])))
    inherited, rerun = [], []
    for item in manifest["units"]:
        if item.get("disposition") == "inherited":
            check = item.get("inherit_check") or {}
            inherited.append({"unit_id": item["unit_id"], "source_run_id": (item.get("basis") or {}).get("run_id"),
                              "source_record_sha256": item.get("record_sha256"),
                              "view_digest": check.get("inherit_inputs_sha256"), "repo_root": check.get("repo_root")})
        else:
            rerun.append({"unit_id": item["unit_id"], "reasons": list(item.get("reasons") or [])})
    # 请求后置的写法在收据里多写一条“绑定的承接请求”（路径与摘要）；原写法的汇总与改造前逐字相同。
    return {"inherited": inherited, "rerun": rerun, "decision": manifest.get("unit_inherit_decision"),
            **({"request": bound["request"]} if bound is not None else {})}


def consume_inherited(plan):
    """收尾门禁刚跑完的那一次：按当前时刻做一次真消费（取消费时的校时与撤销快照、批准此刻仍有效、写消费登记）。之后的重放都是历史重放。"""
    config = plan["basis"]["config"]
    root = Path(config["work_root"]) / "gates"
    entry = safety.read(root / "entry-gates.json")
    _path, manifest, store = _inherit_manifest(plan, root, safety.read(safety.plain(entry["executor_summary"])))
    module = _unit_inherit()
    try:
        return module.verify_published(manifest, store, purpose="closeout")
    except module.ContractError as error:
        raise CloseoutError(f"收尾承接的消费核验不通过：{error}") from error


def gate_retry_summary(plan):
    """收尾门禁里补跑通过的单元（优化第 2 项）：发布汇报要单列写明。先走 ``check_gate_result`` 的全部校验，再取名单与两次尝试的日志。"""
    check_gate_result(plan)
    root = Path(plan["basis"]["config"]["work_root"]) / "gates"
    entry = safety.read(root / "entry-gates.json")
    summary = safety.read(safety.plain(entry["executor_summary"]))
    block = summary.get("retry") or {}
    return [{"unit_id": unit_id, "first_attempt_log": block["units"][unit_id]["first_attempt"]["log"],
             "retry_attempt_log": block["units"][unit_id]["retry_attempt"]["log"],
             "window_rounds": block.get("window_rounds"), "pass_limit": block.get("pass_limit")}
            for unit_id in block.get("retry_passed", [])]


def gate_retry_ledger(config):
    """收尾门禁的补跑累计账本目录（优化第 2 项）。

    收尾门禁的记录库按方案独立（``<工作根>/gate-records``），账本若放在记录库里，每次收尾都是一本新账，“最近 5 轮同一单元只许补跑通过 1 次”
    就跨不了收尾。所以账本放在工作根的**上一级**（每份收尾方案各有自己的工作根，上一级是它们共有的持久位置），经环境变量
    ``UNIT_EXECUTOR_RETRY_LEDGER`` 交给驱动，驱动再转发给执行器；账本按单元 ID 分文件、只追加，工作根被清理也不会带走它。"""
    return Path(config["work_root"]).parent / "gate-retry-ledger"


def gate_preflight_only(plan_path, unit_inherit_request=None):
    """只做起跑前的预检（不起门禁、不写占位）：给操作员在起全量门禁之前随时自查，结果与 ``gates`` 起跑前的预检逐字相同；
    配置了逐单元承接时同时对照本方案复核承接请求（``unit_inherit_request`` 或已绑定的占位）。"""
    plan = load_plan(plan_path)
    summary = gate_preflight(plan, gate_retry_ledger(plan["basis"]["config"]))
    inherit = bind_inherit_request(plan, unit_inherit_request, write=False)
    if inherit:
        summary["unit_inherit_request"] = check_inherit_request(plan, inherit["request"])
    return {"status": "preflight_passed", **summary}


def run_gates(plan_path, unit_inherit_request=None):
    """在独立进程会话中跑完整门禁；中断后只对账，绝不自动启动第二份全量作业。

    ``unit_inherit_request``：请求后置写法下的承接请求（``inherit-request`` 的产出）；起跑前对照本方案复核并绑定进方案占位，对不上直接拒绝。"""
    plan = load_plan(plan_path)
    config = plan["basis"]["config"]
    work = Path(config["work_root"])
    with run_lock(work):
        previous = journal_read(plan, "gates", "done")
        if previous:
            if previous["files"] != check_gate_result(plan):
                raise CloseoutError("门禁证据被改写")
            return previous
        if journal_read(plan, "gates", "started"):
            exited = journal_read(plan, "gates", "exit")
            if exited and exited.get("exit_code") != 0:
                raise CloseoutError("原门禁已失败；保留旧日志，在新工作根重跑")
            # 输出完备则确认原运行；不完备保留现场，由新方案新工作根重跑。
            files = check_gate_result(plan)
        else:
            # 起跑前两道秒级预检（OPT-17、OPT-18）：候选里没有未承接的冻结路径、注入门禁的环境变量都在白名单且会被继承的都已被测试包剔除。
            # 放在写“开始”占位之前：不通过就一个门禁进程都不起，不留任何占位，修好后同一方案可以直接重跑。
            ledger = gate_retry_ledger(config)
            preflight = gate_preflight(plan, ledger)
            inherit = bind_inherit_request(plan, unit_inherit_request)
            if inherit:
                # 跑之前先过一票否决：来源提交到候选提交之间有类型变化就别白跑一轮承接（门禁结果出来后 check_gate_result 还会独立复核一次）。
                module = _unit_inherit()
                inherit_veto(plan, module.sources_check(module._read_request(Path(inherit["request"]))[1]["sources"])["runs"])
            argv = ["bash", config["gate"]["driver"], "--profile", "full-gates", "--mode", "unit-inherit" if inherit else "re-execute",
                    "--out", str(work / "gates"), "--work", config["gate"].get("work") or str(work / "gate-work"),
                    "--record-store", config["gate"].get("record_store") or str(work / "gate-records"),
                    *(["--unit-inherit-request", inherit["request"], "--unit-inherit-stage", inherit["stage"]] if inherit else []),
                    plan["bundle"]["path"], client_table.spec(client_of(config))["closeout_candidate_branch"], plan["candidate_commit"],
                    config["gate"]["node_modules"]]
            journal(plan, "gates", "started", {"argv": argv, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                                                 "retry_ledger": str(ledger), "preflight": preflight})
            env, _injected = gate_env(config, ledger)
            started = time.monotonic()
            with (work / "gates.log").open("xb") as log:
                os.chmod(log.name, 0o600)
                result = subprocess.run(argv, cwd=config["repo"], env=env, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            journal(plan, "gates", "exit", {"exit_code": result.returncode, "elapsed_seconds": time.monotonic() - started,
                                              "log": safety.file_binding(work / "gates.log")})
            if result.returncode:
                raise CloseoutError("收尾全量门禁失败，正式发布仍关闭；日志已保留")
            files = check_gate_result(plan)
        load_plan(plan_path)
        # 补跑通过的单元（优化第 2 项）单列进完成占位，发布汇报据此写明“全绿，允许单次抖动补跑”；没有补跑时占位与改造前逐字相同。
        retried = gate_retry_summary(plan)
        # 逐单元承接（第 17 项）：门禁刚跑完，做一次真消费，并把承接单元与被重跑单元逐项列进完成占位；没配置承接时占位与改造前逐字相同。
        extra = {}
        if plan_inherit(plan):
            consumed = consume_inherited(plan)
            extra = {"unit_inherit": {**gate_inherit_summary(plan), "consumption": consumed.get("consumption")}}
        return journal(plan, "gates", "done", {"status": "passed", "files": files, **({"retry_passed": retried} if retried else {}), **extra})


def check_action_result(plan, name):
    """外部受管动作须返回绑定本次意向的结构化凭证，脚本退出零不构成删除证明。"""
    action = plan["basis"]["config"][name]
    result = safety.read(action["result"])
    if (result.get("schema_version") != "codex-closeout-action-result/v1" or result.get("status") != "passed"
            or result.get("operation_key") != operation_key(plan, name)
            or result.get("review_sha256") != plan["review_sha256"]):
        raise CloseoutError("收尾动作结果未通过或绑定不符")
    verify_binding(result.get("execution_receipt"))
    if name == "cleanup":
        if (result.get("targets") != action["targets"] or result.get("backup") != action["backup"]
                or result.get("restore_check") != action["restore_check"]):
            raise CloseoutError("清理对象、备份或恢复演练凭证不一致")
        verify_binding(result.get("deletion_verification"))
        started = journal_read(plan, name, "started")
        if (result.get("cleanup_context_sha256") != digest(plan["basis"]["cleanup_context"])
                or not started or result.get("approval") != started.get("approval")):
            raise CloseoutError("清理结果未绑定当前上下文或实际消费的批准")
        verify_binding(result["approval"])
        verify_binding(safety.read(result["approval"]["path"]).get("proof"))
        context_path = verify_binding(started.get("context"))
        if safety.read(context_path) != plan["basis"]["cleanup_context"]:
            raise CloseoutError("动作实际消费的清理上下文与审核内容不一致")
        verification = safety.read(result["deletion_verification"]["path"])
        if (verification.get("operation_key") != operation_key(plan, name) or verification.get("targets") != action["targets"]
                or verification.get("cleanup_context_sha256") != result["cleanup_context_sha256"]
                or verification.get("status") != "passed" or verification.get("remaining_targets") != []):
            raise CloseoutError("删除后的目标核验未闭合")
    return safety.file_binding(action["result"])


def run_action(plan, name, *, cleanup_approval=None):
    """副作用动作先占位；中断后只能复核既有结果，不重复执行未知状态的脚本。"""
    config = plan["basis"]["config"]
    action = config[name]
    if name == "cleanup" and action["mode"] == "defer":
        return journal(plan, name, "done", {"status": "deferred", "reason": action["reason"], "inputs": action["inputs"]})
    if name == "cleanup":
        check_cleanup_context(plan)
        if config_bindings(config) != plan["basis"]["input_files"]:
            raise CloseoutError("清理脚本、输入或备份已漂移")
    previous = journal_read(plan, name, "done")
    if previous:
        if check_action_result(plan, name) != previous["result"]:
            raise CloseoutError("动作结果被修改")
        return previous
    started = journal_read(plan, name, "started")
    if not started:
        if Path(action["result"]).exists():
            raise CloseoutError("动作前已存在结果，拒绝误用旧收据")
        argv = [action["script"], *action["arguments"]]
        extra = {}
        if name == "cleanup":
            extra["approval"] = approval(cleanup_approval, plan, "cleanup")
            context_path = Path(config["work_root"]) / "cleanup-context.json"
            safety.write_once(context_path, plan["basis"]["cleanup_context"])
            extra["context"] = safety.file_binding(context_path)
        journal(plan, name, "started", {"argv": argv, "started_at_utc": datetime.now(timezone.utc).isoformat(), **extra})
        env = {**os.environ, "CODEX_CLOSEOUT_OPERATION_KEY": operation_key(plan, name),
               "CODEX_CLOSEOUT_REVIEW_SHA256": plan["review_sha256"], "CODEX_CLOSEOUT_RESULT": action["result"]}
        if name == "cleanup":
            env.update(CODEX_CLOSEOUT_CLEANUP_CONTEXT=str(context_path),
                       CODEX_CLOSEOUT_CLEANUP_CONTEXT_SHA256=digest(plan["basis"]["cleanup_context"]),
                       CODEX_CLOSEOUT_CLEANUP_APPROVAL=extra["approval"]["path"])
        log_path = Path(config["work_root"]) / (name + ".log")
        with log_path.open("xb") as log:
            os.chmod(log.name, 0o600)
            result = subprocess.run(argv, cwd=config["repo"], env=env, stdout=log, stderr=subprocess.STDOUT)
        journal(plan, name, "exit", {"exit_code": result.returncode, "log": safety.file_binding(log_path)})
        if result.returncode:
            raise CloseoutError("收尾动作失败，先对账原结果；禁止自动重派")
    result_binding = check_action_result(plan, name)
    return journal(plan, name, "done", {"status": "passed", "result": result_binding})


def remote_tip(plan):
    config = plan["basis"]["config"]
    push = config["push"]
    lines = git(config["repo"], "ls-remote", "--refs", push["remote"], push["ref"]).stdout.splitlines()
    if len(lines) != 1 or lines[0].split()[1] != push["ref"]:
        raise CloseoutError("远端分支不存在或结果不唯一")
    return lines[0].split()[0]


def publish(plan_path, approvals):
    """批准与全量门禁通过后快进权威分支、消费清理决定并推送；中断只对账既有动作。"""
    plan = load_plan(plan_path)
    config = plan["basis"]["config"]
    repo, work = Path(config["repo"]), Path(config["work_root"])
    with run_lock(work):
        previous = journal_read(plan, "publish", "done")
        if previous:
            if git(repo, "rev-parse", "HEAD").stdout.strip() != plan["candidate_commit"] or remote_tip(plan) != plan["candidate_commit"]:
                raise CloseoutError("发布后本地或远端发生漂移")
            for item in previous["receipts"].values():
                verify_binding(item)
            if journal_read(plan, "gates", "done")["files"] != check_gate_result(plan):
                raise CloseoutError("已完成发布的门禁凭证漂移")
            for name in ("deployment_check", "cleanup"):
                run_action(plan, name)
            return previous
        scopes = ["guide-review", "release-publication"]
        if config["cleanup"]["mode"] == "execute":
            scopes.append("cleanup")
        evidence = {scope: approval(approvals.get(scope), plan, scope) for scope in scopes}
        passed = journal_read(plan, "gates", "done")
        if not passed or passed.get("files") != check_gate_result(plan):
            raise CloseoutError("尚无复核通过的完整收尾门禁")
        # 部署核验是只读动作，失败阻断后续正式写入；动作脚本与凭证本身已绑定人工批准。
        run_action(plan, "deployment_check")
        load_plan(plan_path)
        client = client_of(config)
        material = safety.read(config["material"])
        if collect_guide_material(material["campaign_path"], material["candidate_id"], client=client) != material:
            raise CloseoutError("发布前封存链发生漂移")
        terminal_for(client)(plan["candidate"], safety.read(config["terminal_facts"]), config["terminal_relative"])
        if client != "codex":
            _claude().verify_candidate(plan["candidate"], config["terminal_relative"], base=_base())
        tip = remote_tip(plan)
        if tip not in {config["push"]["expected_tip"], plan["candidate_commit"]}:
            raise CloseoutError("远端提交变化，停止发布")
        for scope in scopes:
            if approval(approvals[scope], plan, scope) != evidence[scope]:
                raise CloseoutError("批准在执行前变化")
        if not journal_read(plan, "integrate", "started"):
            if git(repo, "rev-parse", "HEAD").stdout.strip() != plan["basis"]["base_commit"]:
                raise CloseoutError("缺少权威分支推进占位")
            journal(plan, "integrate", "started", {"approvals": evidence, "commit": plan["candidate_commit"]})
        if git(repo, "rev-parse", "HEAD").stdout.strip() == plan["basis"]["base_commit"]:
            git(repo, "fetch", "--no-tags", "--no-write-fetch-head", plan["candidate"],
                "refs/heads/" + client_table.spec(client)["closeout_candidate_branch"])
            git(repo, "merge", "--ff-only", "--no-edit", plan["candidate_commit"])
        if tree_manifest(repo) != plan["candidate_files"]:
            raise CloseoutError("权威分支与通过门禁的候选不一致")
        journal(plan, "integrate", "done", {"status": "passed", "commit": plan["candidate_commit"],
                                               "outputs": {p: safety.file_binding(inside(repo, p)) for p in plan["outputs"]}})
        if config["cleanup"]["mode"] == "execute":
            approval(approvals["cleanup"], plan, "cleanup")
        run_action(plan, "cleanup", cleanup_approval=approvals.get("cleanup"))
        load_plan(plan_path)
        for scope in ("guide-review", "release-publication"):
            approval(approvals[scope], plan, scope)
        started = journal_read(plan, "push", "started")
        tip = remote_tip(plan)
        if started:
            if tip != plan["candidate_commit"]:
                raise CloseoutError("上次推送结果不明或失败；须人工对账，不自动重推")
        else:
            if tip != config["push"]["expected_tip"]:
                raise CloseoutError("推送前远端已变化且没有本轮占位")
            # 只使用普通快进推送，不使用 force；远端并发更新由 Git 再次拒绝。
            journal(plan, "push", "started", {"approvals": evidence, "remote_tip": tip, "commit": plan["candidate_commit"]})
            git(repo, "push", "--porcelain", config["push"]["remote"], plan["candidate_commit"] + ":" + config["push"]["ref"])
            if remote_tip(plan) != plan["candidate_commit"]:
                raise CloseoutError("推送结果未确认，保留现场并停止")
        journal(plan, "push", "done", {"status": "passed", "commit": plan["candidate_commit"]})
        receipts = {name: safety.file_binding(work / "journal" / (name + "-done.json"))
                    for name in ("gates", "deployment_check", "integrate", "cleanup", "push")}
        completed = datetime.now(timezone.utc)
        retried = (journal_read(plan, "gates", "done") or {}).get("retry_passed")
        inherited = (journal_read(plan, "gates", "done") or {}).get("unit_inherit")
        return journal(plan, "publish", "done", {"status": "passed", "receipts": receipts,
                     "completed_at_utc": completed.isoformat(), "started_at_utc": plan["started_at_utc"],
                     "wall_seconds": (completed - datetime.fromisoformat(plan["started_at_utc"])).total_seconds(),
                     "performance_claim": False,
                     # 全绿但其中有补跑通过的单元（允许单次抖动补跑，优化第 2 项）：发布汇报必须写明，不静默放行。
                     **({"gate_retry_passed": retried} if retried else {}),
                     # 全绿但其中有逐单元承接的单元（第 17 项）：发布汇报逐项写明来源与被重跑的单元，不静默放行。
                     **({"gate_unit_inherit": {"inherited": len(inherited["inherited"]), "rerun": len(inherited["rerun"])}} if inherited else {})})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reader = sub.add_parser("read-material", help="独立进程按受管数据根重放正式材料")
    reader.add_argument("--campaign", required=True)
    reader.add_argument("--candidate", required=True)
    reader.add_argument("--client", choices=client_table.CLIENTS, default="codex", help="缺省 codex（与改造前相同）")
    draft = sub.add_parser("draft", help="从正式封存链生成指南材料和草稿，不签发批准")
    for name in ("campaign", "candidate", "guide", "baseline-profile", "output"):
        draft.add_argument("--" + name, required=True)
    draft.add_argument("--client", choices=client_table.CLIENTS, default="codex", help="缺省 codex（与改造前相同）")
    pre = sub.add_parser("prepare", help="生成可审核隔离候选；dry-run 零写入")
    pre.add_argument("--config", required=True)
    pre.add_argument("--dry-run", action="store_true")
    gate = sub.add_parser("gates", help="按标准 full-gates 合同重跑所有单元")
    gate.add_argument("--plan", required=True)
    gate.add_argument("--preflight-only", action="store_true",
                      help="只跑起跑前预检（冻结覆盖、门禁环境注入核对，配置了承接时再加承接请求复核）并打印，不起门禁、不写任何占位")
    gate.add_argument("--unit-inherit-request", help="逐单元承接请求（请求后置的配置必须给；由 inherit-request 针对本方案候选提交生成）")
    propose = sub.add_parser("inherit-propose", help="对收尾候选提交写逐单元承接提案（执行器只判定，不跑单元）")
    propose.add_argument("--plan", required=True)
    request = sub.add_parser("inherit-request", help="提案＋两份批准＋凭证组装绑定候选提交的承接请求，并对照方案复核")
    request.add_argument("--plan", required=True)
    request.add_argument("--proposal", required=True, help="inherit-propose 写出的提案目录")
    request.add_argument("--approval", action="append", required=True, help="对提案 binding_sha256 的批准文件（两类角色各一份，可重复）")
    request.add_argument("--output", help="请求写到哪里（缺省 <工作根>/inherit/request.json，不覆盖）")
    request.add_argument("--clock", help="校时样本文件（缺省当场取）")
    request.add_argument("--revocation", help="撤销查询文件（缺省当场取）")
    request.add_argument("--clock-source", help="校时来源：chronyc（缺省）或 file:<绝对路径>（ARM64 没有 chronyc）")
    request.add_argument("--campaign-status-source")
    request.add_argument("--max-age-hours", type=float)
    release = sub.add_parser("publish", help="消费人工批准、核对门禁并恢复收尾")
    release.add_argument("--plan", required=True)
    release.add_argument("--guide-approval", required=True)
    release.add_argument("--publication-approval", required=True)
    release.add_argument("--cleanup-approval")
    args = parser.parse_args(argv)
    try:
        if args.command == "read-material":
            if args.client == "codex":
                result = read_guide_material(args.campaign, args.candidate)
            else:
                result = _claude().read_guide_material(args.campaign, args.candidate, base=_base())
        elif args.command == "draft":
            material = collect_guide_material(args.campaign, args.candidate, client=args.client)
            heading = PART2 if args.client == "codex" else client_table.spec(args.client)["guide_part2_heading"]
            generated = guide_draft(safety.plain(args.guide).read_text(), material, safety.read(args.baseline_profile), heading=heading)
            output = safety.plain(args.output)
            safety.write_once(output / "material.json", material)
            write_text_once(output / "guide.md", generated["guide_text"])
            result = {"status": "review_required", "material": safety.file_binding(output / "material.json"),
                      "guide": safety.file_binding(output / "guide.md"), "affected_rule_ids": generated["affected_rule_ids"]}
        elif args.command == "prepare":
            result = prepare(safety.read(args.config), dry_run=args.dry_run)
        elif args.command == "gates":
            result = (gate_preflight_only(args.plan, args.unit_inherit_request) if args.preflight_only
                      else run_gates(args.plan, args.unit_inherit_request))
        elif args.command == "inherit-propose":
            result = inherit_propose(args.plan)
        elif args.command == "inherit-request":
            result = inherit_request(args.plan, proposal=args.proposal, approvals=args.approval, output=args.output, clock=args.clock,
                                     revocation=args.revocation, clock_source=args.clock_source,
                                     campaign_status_source=args.campaign_status_source, max_age_hours=args.max_age_hours)
        else:
            result = publish(args.plan, {"guide-review": args.guide_approval, "release-publication": args.publication_approval,
                                        "cleanup": args.cleanup_approval})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
