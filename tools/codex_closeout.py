#!/usr/bin/env python3
"""Codex 升级收尾：候选制品、指南人工批准、完整门禁和可恢复发布。

候选制品只写入隔离目录；正式冻结承接、终态签发与推送必须消费同一份指南批准和门禁结论。
"""

from __future__ import annotations

import argparse
import ast
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

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.arm64_capture_driver.driver import fix_safety as safety

GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
PART2 = "# 第二部分 Codex CLI 客户端规则画像"
SHA256 = re.compile(r"[0-9a-f]{64}")
SCHEMA = "codex-upgrade-closeout/v1"


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


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def part2_sha(text):
    """与正式 source_spec_section_sha256 保持相同的章节边界和原始换行。"""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == PART2]
    if len(starts) != 1:
        raise CloseoutError("指南必须恰有一个第二部分")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    return sha("".join(lines[start:end]).encode())


def source_updates(repo, guide_text):
    """只更新指南、十二类现存清单的来源摘要与实际引用它们的检查器常量。"""
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


def collect_guide_material(campaign, candidate):
    """在独立解释器中使用 Campaign 的受管读侧，避免把源码副本误当成证据数据根。"""
    campaign = safety.plain(campaign)
    if campaign.parent.name != "campaigns" or campaign.parent.parent.name != "evidence":
        raise CloseoutError("Campaign 必须位于受管根的 evidence/campaigns 下")
    reader_root = campaign.parents[2]
    safety.file_binding(reader_root / "tools/official_client_capture/codex_upgrade.py")
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "read-material",
                             "--campaign", str(campaign), "--candidate", candidate],
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


def guide_draft(original, material, baseline_profile):
    """生成受影响规则的可审核草稿，不把候选内部判据的零官方观测称为实测通过。"""
    entries = material["migration"]["entries"]
    current = {row["rule_id"]: row for row in material["profile"]["rules"]}
    baseline = {row["rule_id"]: row for row in baseline_profile["rules"]}
    affected = []
    start = original.index(PART2)
    end = re.search(r"(?m)^# ", original[start + len(PART2):])
    end = start + len(PART2) + end.start() if end else len(original)
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
            start = next(i for i, line in enumerate(lines) if line.rstrip() == PART2)
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


def validate_config(config):
    required = {"schema_version", "repo", "work_root", "guide", "material", "terminal_facts", "terminal_relative",
                "tag", "gate", "cleanup", "push", "deployment_check"}
    if set(config) != required or config["schema_version"] != SCHEMA:
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
    if set(gate) != {"driver", "vc_env", "node_modules"}:
        raise CloseoutError("门禁必须提供受管驱动、参数和锁定前端依赖")
    for key in ("driver", "vc_env"):
        safety.file_binding(gate[key])
    safety.plain(gate["node_modules"])
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
    paths.append(str(Path(__file__).resolve()))
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
    repo, work = validate_config(config)
    bound = config_bindings(config)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if git(repo, "status", "--porcelain").stdout.strip():
        raise CloseoutError("权威仓库必须先提交并保持干净，才能绑定收尾方案")
    updates = source_updates(repo, Path(config["guide"]).read_text())
    material = safety.read(config["material"])
    if not all(material.get(key) for key in ("campaign_id", "candidate_id", "target_version", "classification_sha256", "official_stage_sha256")):
        raise CloseoutError("指南缺少正式 VC-2 与 VC-1 材料绑定")
    if collect_guide_material(material["campaign_path"], material["candidate_id"]) != material:
        raise CloseoutError("指南材料不能从封存链重放")
    facts = safety.read(config["terminal_facts"])
    if facts.get("target", {}).get("version") != material["target_version"] or facts.get("campaign_chain", [{}])[-1].get("campaign_id") != material["campaign_id"]:
        raise CloseoutError("终态事实和指南材料不是同一 Campaign／版本")
    terminal_text = terminal_candidate(repo, facts, config["terminal_relative"])
    terminal_path = inside(repo, config["terminal_relative"])
    if terminal_path.exists():
        raise CloseoutError("终态坐标已存在，不允许重签旧升级；使用下一轮真实事实")
    for existing in terminal_path.parent.glob("CODEX_CLI_*_TERMINAL_STATE_RECEIPT.json"):
        if safety.read(existing).get("target", {}).get("version") == material["target_version"]:
            raise CloseoutError("目标版本已有终态收据，不允许换路径重复签发")
    basis = {"config": config, "input_files": bound, "base_commit": head, "branch": branch,
             "source_tree": git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
             "remote_url_sha256": sha(git(repo, "remote", "get-url", config["push"]["remote"]).stdout.encode()),
             "source_updates": updates, "terminal_text": terminal_text}
    key = digest(basis)
    if dry_run:
        return {"status": "dry_run", "input_sha256": key, "changed_paths": sorted([*updates, config["terminal_relative"]]),
                "manual_gates": ["guide-review", "cleanup-authorization", "release-publication"], "work_root": str(work)}
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
        git(candidate, "branch", "codex-closeout-candidate", commit)
        bundle = work / "candidate.bundle"
        git(candidate, "bundle", "create", str(bundle), "codex-closeout-candidate")
        candidate_manifest = tree_manifest(candidate)
        plan = {"schema_version": SCHEMA, "input_sha256": key, "basis": basis, "candidate": str(candidate),
                "candidate_commit": commit, "candidate_tree": git(candidate, "rev-parse", "HEAD^{tree}").stdout.strip(),
                "candidate_files": candidate_manifest, "bundle": safety.file_binding(bundle), "outputs": sorted(outputs),
                "candidate_only": True, "prepared_at_utc": datetime.now(timezone.utc).isoformat()}
        plan["started_at_utc"] = started
        plan["review_sha256"] = digest(plan)
        if config_bindings(config) != bound:
            raise CloseoutError("准备期间输入文件变化，候选不可用于发布")
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
    if plan.get("schema_version") != SCHEMA or not plan.get("candidate_only"):
        raise CloseoutError("不是受管候选方案")
    if plan.get("review_sha256") != digest({k: v for k, v in plan.items() if k != "review_sha256"}):
        raise CloseoutError("方案自摘要不一致")
    basis = plan["basis"]
    if plan["input_sha256"] != digest(basis):
        raise CloseoutError("方案输入摘要不一致")
    config = basis["config"]
    repo, work = validate_config(config)
    if safety.plain(path) != work / "plan.json" or Path(plan["candidate"]) != work / "candidate":
        raise CloseoutError("方案与工作根绑定不一致")
    if config_bindings(config) != basis["input_files"]:
        raise CloseoutError("收尾输入或工具已改变")
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
    """完整门禁集合与标准命令逐项核对，不能把某条命令退出零当成全量通过。"""
    from tools.ci import entry_gates
    root = Path(plan["basis"]["config"]["work_root"]) / "gates"
    entry = safety.read(root / "entry-gates.json")
    expected = set(entry_gates.profile_gates("full-gates"))
    rows = entry.get("gates", [])
    if (entry.get("schema_version") != entry_gates.ENTRY_SUMMARY_SCHEMA or entry.get("profile") != "full-gates"
            or entry.get("status") != "passed" or entry.get("mode") != "re-execute"
            or entry.get("source", {}).get("tree_head") != plan["candidate_commit"]
            or entry.get("source", {}).get("commit") != plan["candidate_commit"]
            or len(rows) != len(expected) or {r.get("gate_id") for r in rows} != expected):
        raise CloseoutError("收尾全量门禁未通过或候选提交／集合不匹配")
    files = [root / "entry-gates.json", root / "gates-manifest.json"]
    manifest = safety.read(files[-1])
    if (manifest.get("schema_version") != entry_gates.GATES_SCHEMA or manifest.get("profile") != "full-gates"
            or {r.get("gate_id") for r in manifest.get("gates", [])} != expected):
        raise CloseoutError("门禁执行清单不完整")
    for row in rows:
        if row.get("status") != "passed" or row.get("exit_code") != 0:
            raise CloseoutError("存在未通过的收尾门禁")
        path = inside(root, row["gate_json"])
        result = safety.read(path)
        command, directory = entry_gates.GATE_COMMANDS[row["gate_id"]]
        if (result.get("gate_id") != row["gate_id"] or result.get("status") != "passed"
                or result.get("exit_code") != 0 or result.get("failed_units")
                or result.get("inherited_units") or result.get("mode") != "re-execute"
                or result.get("tree_head") != plan["candidate_commit"]
                or result.get("command") != command or result.get("working_directory") != directory
                or not result.get("started_at_utc") or not result.get("completed_at_utc")):
            raise CloseoutError("门禁分项命令、结果或提交身份不一致")
        files.append(path)
    executor = safety.plain(entry["executor_summary"])
    if not executor.is_relative_to(root):
        raise CloseoutError("执行器结果不在本轮目录")
    summary = safety.read(executor)
    if (summary.get("schema_version") != entry_gates.GATES_SUMMARY_SCHEMA or summary.get("status") != "passed"
            or summary.get("mode") != "re-execute" or summary.get("units_not_run") or summary.get("failed_units")
            or any(row.get("passed") is not True or row.get("disposition", "executed") != "executed"
                   or row.get("exit_code") != 0 or row.get("orphans") != 0 or row.get("timed_out")
                   or row.get("missing") for row in summary.get("units", []))
            or not summary.get("units")):
        raise CloseoutError("执行器存在未运行、失败或承接单元")
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
    return {str(path): safety.file_binding(path)["sha256"] for path in sorted(set(files))}


def run_gates(plan_path):
    """在独立进程会话中跑完整门禁；中断后只对账，绝不自动启动第二份全量作业。"""
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
            argv = ["bash", config["gate"]["driver"], "--profile", "full-gates", "--mode", "re-execute",
                    "--out", str(work / "gates"), "--work", str(work / "gate-work"),
                    "--record-store", str(work / "gate-records"), plan["bundle"]["path"],
                    "codex-closeout-candidate", plan["candidate_commit"], config["gate"]["node_modules"]]
            journal(plan, "gates", "started", {"argv": argv, "started_at_utc": datetime.now(timezone.utc).isoformat()})
            env = {**os.environ, "ARM64_VC_ENV": config["gate"]["vc_env"], "PYTHONDONTWRITEBYTECODE": "1"}
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
        return journal(plan, "gates", "done", {"status": "passed", "files": files})


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
        verification = safety.read(result["deletion_verification"]["path"])
        if (verification.get("operation_key") != operation_key(plan, name) or verification.get("targets") != action["targets"]
                or verification.get("status") != "passed" or verification.get("remaining_targets") != []):
            raise CloseoutError("删除后的目标核验未闭合")
    return safety.file_binding(action["result"])


def run_action(plan, name):
    """副作用动作先占位；中断后只能复核既有结果，不重复执行未知状态的脚本。"""
    config = plan["basis"]["config"]
    action = config[name]
    if name == "cleanup" and action["mode"] == "defer":
        return journal(plan, name, "done", {"status": "deferred", "reason": action["reason"], "inputs": action["inputs"]})
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
        journal(plan, name, "started", {"argv": argv, "started_at_utc": datetime.now(timezone.utc).isoformat()})
        env = {**os.environ, "CODEX_CLOSEOUT_OPERATION_KEY": operation_key(plan, name),
               "CODEX_CLOSEOUT_REVIEW_SHA256": plan["review_sha256"], "CODEX_CLOSEOUT_RESULT": action["result"]}
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
        material = safety.read(config["material"])
        if collect_guide_material(material["campaign_path"], material["candidate_id"]) != material:
            raise CloseoutError("发布前封存链发生漂移")
        terminal_candidate(plan["candidate"], safety.read(config["terminal_facts"]), config["terminal_relative"])
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
            git(repo, "fetch", "--no-tags", "--no-write-fetch-head", plan["candidate"], "refs/heads/codex-closeout-candidate")
            git(repo, "merge", "--ff-only", "--no-edit", plan["candidate_commit"])
        if tree_manifest(repo) != plan["candidate_files"]:
            raise CloseoutError("权威分支与通过门禁的候选不一致")
        journal(plan, "integrate", "done", {"status": "passed", "commit": plan["candidate_commit"],
                                               "outputs": {p: safety.file_binding(inside(repo, p)) for p in plan["outputs"]}})
        if config["cleanup"]["mode"] == "execute":
            approval(approvals["cleanup"], plan, "cleanup")
        run_action(plan, "cleanup")
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
        return journal(plan, "publish", "done", {"status": "passed", "receipts": receipts,
                     "completed_at_utc": completed.isoformat(), "started_at_utc": plan["started_at_utc"],
                     "wall_seconds": (completed - datetime.fromisoformat(plan["started_at_utc"])).total_seconds(),
                     "performance_claim": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reader = sub.add_parser("read-material", help="独立进程按受管数据根重放正式材料")
    reader.add_argument("--campaign", required=True)
    reader.add_argument("--candidate", required=True)
    draft = sub.add_parser("draft", help="从正式封存链生成指南材料和草稿，不签发批准")
    for name in ("campaign", "candidate", "guide", "baseline-profile", "output"):
        draft.add_argument("--" + name, required=True)
    pre = sub.add_parser("prepare", help="生成可审核隔离候选；dry-run 零写入")
    pre.add_argument("--config", required=True)
    pre.add_argument("--dry-run", action="store_true")
    gate = sub.add_parser("gates", help="按标准 full-gates 合同重跑所有单元")
    gate.add_argument("--plan", required=True)
    release = sub.add_parser("publish", help="消费人工批准、核对门禁并恢复收尾")
    release.add_argument("--plan", required=True)
    release.add_argument("--guide-approval", required=True)
    release.add_argument("--publication-approval", required=True)
    release.add_argument("--cleanup-approval")
    args = parser.parse_args(argv)
    try:
        if args.command == "read-material":
            result = read_guide_material(args.campaign, args.candidate)
        elif args.command == "draft":
            material = collect_guide_material(args.campaign, args.candidate)
            generated = guide_draft(safety.plain(args.guide).read_text(), material, safety.read(args.baseline_profile))
            output = safety.plain(args.output)
            safety.write_once(output / "material.json", material)
            write_text_once(output / "guide.md", generated["guide_text"])
            result = {"status": "review_required", "material": safety.file_binding(output / "material.json"),
                      "guide": safety.file_binding(output / "guide.md"), "affected_rule_ids": generated["affected_rule_ids"]}
        elif args.command == "prepare":
            result = prepare(safety.read(args.config), dry_run=args.dry_run)
        elif args.command == "gates":
            result = run_gates(args.plan)
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
