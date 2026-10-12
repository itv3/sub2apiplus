#!/usr/bin/env python3
"""四条 Claude control 的当前严格审计适配器。

历史 read_audit.py 与 runtime 原字节、固定 gate pin 和独立 worker 维持不变。
这里只复用同目录原函数对象与常量，并接本轮已冻结的四条 control 扩展；普通记录
始终委派历史实现。新采集与消费必须同时绑定两份 sidecar 的实际摘要。
"""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_LEGACY_KEY = "_control_audit_legacy_" + str(_HERE.parent).replace("/", "_")
if _LEGACY_KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_LEGACY_KEY, _HERE.with_name("read_audit.py"))
    _legacy = importlib.util.module_from_spec(_spec)
    sys.modules[_LEGACY_KEY] = _legacy
    _spec.loader.exec_module(_legacy)
else:
    _legacy = sys.modules[_LEGACY_KEY]
# 复用原 API 引用，不修改原模块的 globals；独立历史 worker 不安装本适配器。
globals().update({name: value for name, value in vars(_legacy).items()
                  if not name.startswith("__") and name not in {"HERE", "main"}})
HERE = _HERE

CONTROL_UNIT_IDS = frozenset({"pre-a3:claude:" + name for name in (
    "vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution")})
SIDECAR_FILES = ("control_read_audit.py", "control_read_audit_runtime.py")


def _runtime_module():
    """固定同目录 control runtime，不接受清单、环境或证据中的代码路径。"""
    key = "_control_audit_runtime_" + str(HERE.parent).replace("/", "_")
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, sidecar_paths()[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _records_module():
    """当前消费者自己的原记录模块实例；不替换历史 gate 共用的模块缓存。"""
    key = "_control_audit_records_" + str(HERE.parent).replace("/", "_")
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE.with_name("unit_records.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
        module._audit_module = lambda: sys.modules[__name__]
    return sys.modules[key]


def sidecar_paths():
    """执行器与承接消费者只冻结这两份固定普通文件。"""
    paths = [HERE.with_name(name) for name in SIDECAR_FILES]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise AuditError("四control固定sidecar缺失或为链接")
    return paths


def selected_plan(plan):
    """仅登记的四control准确完整命令可以取得当前适配器，其他项保持原合同。"""
    policy = (plan.get("audit_policy") or {}).get("units") or {}
    commands = {item["unit_id"]: item for item in plan.get("units", [])}
    selected = sorted(CONTROL_UNIT_IDS & set(policy))
    enabled = False
    for unit_id in selected:
        declaration = policy[unit_id]
        if "runtime_contract" not in declaration:
            continue
        contract = _legacy._runtime_module().validate_contract(declaration["runtime_contract"])
        command = commands.get(unit_id) or {}
        if _runtime_module().control_scenario(command.get("argv") or [], contract["temporary_root"]) != unit_id.removeprefix("pre-a3:claude:"):
            raise AuditError("四control当前适配器只接受原完整命令与准确空盘坐标")
        enabled = True
    return enabled


def frozen_executor(executor):
    """消费只认本次执行器登记的两份 sidecar 实际摘要，不据模块名称或客户端猜资格。"""
    files = executor.get("files") or {}
    selected = set(SIDECAR_FILES) & set(files)
    if not selected:
        return False
    if selected != set(SIDECAR_FILES) or any(hashlib.sha256(path.read_bytes()).hexdigest() != files[path.name]
                                           for path in sidecar_paths()):
        raise AuditError("当前四control执行器缺完整sidecar绑定或实际字节漂移")
    return True



class TraceParser(_legacy.TraceParser):
    """四control复用原解析器，只扩展可重放的准确调用事件。"""
    def __init__(self, *, strict=False, runtime_context=None):
        super().__init__(strict=strict)
        self.control_events = []
        self.event_pid = "0"
        self.runtime_context = runtime_context
        if not strict or runtime_context is None or runtime_context.get("control_chain") is None:
            raise AuditError("四control解析器必须具有本轮固定隔离凭证")
        contract = _runtime_module().validate_context(runtime_context, [runtime_context["contract_entry"]])
        self.output_tracker = _runtime_module().OutputTracker(contract)

    def feed(self, raw):
        # 原 feed 已处理拼接与父子次序；新 namespace flags 只增加失败关闭，不建立任意子空间合同。
        super().feed(raw)
        name, body, _success = self.event
        if self.strict and name in _SPAWN and re.search(r"\bCLONE_NEW(?!NS\b)[A-Z0-9_]*\b", body):
            self._unresolved("unobservable-child:" + name)

    def _add(self, path: str | None, kind: str, line: str, *, move_target: str | None = None,
             move_flags: set[str] | None = None) -> None:
        match = _LINE.match(line)
        self.event_pid = match.group(1) if match else "0"
        name, body, succeeded = self.event
        if name in {"rename", "renameat", "renameat2", "link", "linkat"}:
            arguments = _call_arguments(body, _RESULT.search(body))
            resolved = []
            if name in {"rename", "link"}:
                resolved = [_join(self.cwd.get(self.event_pid), _path_argument(arguments, index)) for index in (0, 1)]
            else:
                for index in (0, 2):
                    directory = _ANNOTATION.fullmatch(arguments[index]) if index < len(arguments) else None
                    value = _path_argument(arguments, index + 1)
                    resolved.append(_join(unescape(directory[2]), value) if directory and value is not None else None)
            if len(resolved) == 2 and path in resolved and all(resolved):
                move_target = resolved[1] if path == resolved[0] else resolved[0]
            if name in {"renameat2", "linkat"}:
                prefix = "RENAME" if name == "renameat2" else "LINK"
                move_flags = {prefix + ("_FLAGS_0" if len(arguments) == 5 and arguments[4] == "0" else "_FLAGS_UNSUPPORTED")}
        if not path:
            if self.strict:
                self._unresolved("path-not-resolved")
            return
        self.accesses.setdefault(path, set()).add(kind)
        self.samples.setdefault(path, line[:SAMPLE_CHARS])
        control = (self.runtime_context or {}).get("control_chain")
        if control and (path == control["context_path"] or re.fullmatch(
                r"/proc/(self|" + re.escape(self.event_pid) + r")/ns/(mnt|net)", path)):
            name, body, succeeded = self.event
            result = _RESULT.search(body)
            arguments = _call_arguments(body, result)
            descriptor = re.search(r'\)\s*=\s*\d+<(' + _ANN + r')>\s*$', body)
            target = unescape(descriptor[1]) if descriptor else None
            if name in {"readlink", "readlinkat"}:
                target = _path_argument(arguments, 2 if name == "readlinkat" else 1)
            if len(self.control_events) < 200000:
                self.control_events.append({"path": path, "kind": kind, "name": name, "pid": self.event_pid,
                                            "succeeded": succeeded, "errno": result.group(2) if result else None, "target": target})
            else:
                self._unresolved("control-event-limit")
        if self.output_tracker is not None and path == "/dev/tty":
            name, body, succeeded = self.event
            result = _RESULT.search(body)
            self.terminal_events.append({"name": name, "kind": kind, "errno": result.group(2) if result else None,
                                         "succeeded": succeeded})
        if self.output_tracker is not None and _under(path, self.output_tracker.root):
            name, body, succeeded = self.event
            descriptor = re.search(r'\)\s*=\s*\d+<(' + _ANN + r')>\s*$', body)
            target = unescape(descriptor[1]) if descriptor else None
            flags = _operation_flags(body)
            if name in {"rename", "renameat", "renameat2", "link", "linkat"}:
                target, flags = move_target, move_flags or set()
            result = _RESULT.search(body)
            if result and not succeeded and name in {"mkdir", "mkdirat", "link", "linkat"}:
                if result.group(2) in {"EEXIST", "ENOENT"}:
                    flags.add("OUTPUT_FAILED_" + result.group(2))
            self.output_tracker.observe(path, kind, name, "|".join(sorted(flags)), succeeded, target)
            # 仅保存归属重放所需字段，不能把完整执行参数或文件内容写进轨迹。
            if len(self.output_events) < 200000:
                self.output_events.append({"path": path, "kind": kind, "name": name, "succeeded": succeeded,
                                           "flags": sorted(flags), "target": target})
            else:
                self._unresolved("output-event-limit")




def trace_document(parser, roots):
    value = _legacy.trace_document(parser, roots)
    if parser.runtime_context.get("control_chain") is not None:
        value["runtime"]["control_events"] = parser.control_events
    return value


def filter_stream(stream, roots, *, strict=False, runtime_context=None):
    if runtime_context is None or runtime_context.get("control_chain") is None:
        return _legacy.filter_stream(stream, roots, strict=strict, runtime_context=runtime_context)
    parser = TraceParser(strict=strict, runtime_context=runtime_context)
    for line in stream:
        parser.feed(line)
    return trace_document(parser, roots)


def strace_argv(argv: Sequence[str], *, output: Path, roots: Sequence[str], python: str | None = None,
                strict: bool = False, runtime_context: Path | None = None) -> list[str]:
    """把一个单元的命令包在 strace 下：输出先经 grep 预筛，再交给本模块 ``filter``，只留各个根之下的路径。"""

    if runtime_context is None or json.loads(runtime_context.read_text()).get("control_chain") is None:
        return _legacy.strace_argv(argv, output=output, roots=roots, python=python, strict=strict, runtime_context=runtime_context)
    command = [python or sys.executable, "-B", str(HERE), "filter", "--output", str(output)]
    if strict:
        roots = ["/"]
        command.append("--strict")
    if runtime_context is not None:
        command += ["--runtime-context", str(runtime_context)]
    for root in roots:
        command += ["--root", str(root)]
    for part in command:
        if any(ch.isspace() or ch in "'\"\\$`|;&<>" for ch in part):
            raise AuditError(f"审计输出命令里的路径带空白或 shell 特殊字符，不能交给 strace 的管道：{part}")
    pattern = prefilter_pattern(roots)
    if "'" in pattern:
        raise AuditError("预筛表达式里有单引号，不能交给 shell")
    pipe = " ".join(command) if strict else f"grep --line-buffered -E '{pattern}' | " + " ".join(command)
    # 文件描述符切目录、命名空间变更和未建模的外部通信必须可见；未知调用保持失败关闭。
    options = ["-s", "4096", "-e", "verbose=%%stat", "-e", "abbrev=!%%stat",
               "-e", "trace=%file,getdents64,clone,clone3,fork,vfork,fchdir,unshare,setns,"
               "open_by_handle_at,io_uring_setup,socket,connect,accept,accept4,sendmsg,recvmsg"] if strict else []
    return ["strace", *STRACE_OPTIONS, *options, "-o", "|" + pipe, "--", *argv]


def strict_audit_reads(document: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]] | None,
                       *, repo_root: str, data_root: str | None, historical: bool = False) -> dict[str, Any]:
    """完整观察与可承接分开判断：所有实际路径必须有事前绑定且运行后未变的精确快照。"""
    if (document.get("runtime") or {}).get("context", {}).get("control_chain") is None:
        return _legacy.strict_audit_reads(document, inputs, repo_root=repo_root, data_root=data_root, historical=historical)
    findings = []
    snapshots = {}
    coverage = document.get("coverage") or {}
    if (document.get("schema_version") not in {STRICT_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA} or document.get("roots") != ["/"]
            or coverage.get("all_host_paths") is not True or coverage.get("stream_complete") is not True
            or coverage.get("unresolved") != 0 or not document.get("lines") or not document.get("accesses")):
        findings.append({"root": "audit", "path": "", "kinds": [], "sample": "", "suggestion": "完整轨迹缺失、未结束或含未解析调用，不可承接"})
    runtime_paths: set[str] = set()
    if document.get("schema_version") == RUNTIME_TRACE_SCHEMA:
        try:
            runtime_paths = _runtime_module().replay_runtime(document, list(inputs or []), historical=historical)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            findings.append({"root": "runtime", "path": "", "kinds": [], "sample": "", "suggestion": str(error)})
    for entry in inputs or []:
        detail = entry.get("detail") or {}
        if detail.get("schema_version") != HOST_SCHEMA:
            continue
        path = detail.get("path")
        try:
            if historical:
                # 历史重放校验封存快照自身；当前承接仍须重读宿主实物。
                encoded = json.dumps(detail["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                current = {"category": "environment", "name": "host:" + path, "sha256": hashlib.sha256(encoded).hexdigest(),
                           "detail": detail}
            else:
                current = host_snapshot(path)
            if current != entry:
                raise AuditError("宿主输入快照漂移")
        except (OSError, ValueError, TypeError, AuditError) as error:
            findings.append({"root": "host", "path": str(path), "kinds": [], "sample": "", "suggestion": str(error)})
            continue
        snapshots[path] = detail["snapshot"]
    counts = {"repo_paths": 0, "data_paths": 0, "host_paths": 0, "metadata_only": 0, "import_probes": 0}
    for path, kinds in document.get("accesses") or []:
        root = "repo" if _under(path, repo_root) else "data" if data_root and _under(path, data_root) else "host"
        counts[root + "_paths"] += 1
        if path in runtime_paths:
            continue
        binding = snapshots.get(path)
        if binding is not None:
            kind = binding["kind"]
            if ((kind == "file" and set(kinds) <= {"read", "exec", "stat"})
                    # libc 也会不带 O_DIRECTORY 只读打开目录；快照已证明该精确路径是目录，直接目录项已绑定。
                    # 子文件仍须单独快照，不能因为打开了父目录就获得递归覆盖。
                    or (kind == "directory" and set(kinds) <= METADATA_KINDS | {"read"})
                    or (kind == "missing" and set(kinds) <= {"missing"})):
                continue
        findings.append({"root": root, "path": path, "kinds": kinds, "sample": path,
                         "suggestion": "实际路径缺少精确快照或访问类型超出绑定；目录声明不能覆盖子文件"})
    return {"undeclared_count": len(findings), "undeclared": findings[:MAX_FINDINGS], **counts,
            "coverage_complete": not findings, "coverage_scope": "all-file-paths/v1", "status": "failed" if findings else "passed"}



def audit_reads(document, inputs, *, repo_root, data_root=None, in_data_root=False, head_id_only=False, strict=False):
    if strict:
        return strict_audit_reads(document, inputs, repo_root=repo_root, data_root=data_root)
    return _legacy.audit_reads(document, inputs, repo_root=repo_root, data_root=data_root,
                               in_data_root=in_data_root, head_id_only=head_id_only, strict=False)


def _checked_json(path: Path) -> dict[str, Any]:
    """准备材料只读规范绝对路径；链接、相对路径及非对象文件不能充当正式来源。"""
    path = Path(path)
    if (not path.is_absolute() or str(path) != os.path.normpath(str(path))
            or any(item.is_symlink() for item in (path, *path.parents))):
        raise AuditError("策略准备来源须为无符号链接的规范绝对路径")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError("策略准备来源必须是 JSON 对象")
    return value


def _policy_plan(plan: Mapping[str, Any], unit_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """消费原门禁规划的真实命令；不猜测试发现集合，不接受凭空登记的单元。"""
    if (plan.get("schema_version") != "unit-executor-gates/v1" or not isinstance(plan.get("units"), list)
            or not unit_ids or len(unit_ids) != len(set(unit_ids))):
        raise AuditError("策略准备必须选择正规门禁规划中的非重复命令单元")
    if set(unit_ids) - CONTROL_UNIT_IDS:
        raise AuditError("当前producer只登记四条control，其他场景与普通命令不取得该合同")
    items = plan["units"]
    if (not all(isinstance(item, dict) and isinstance(item.get("unit_id"), str) for item in items)
            or len({item["unit_id"] for item in items}) != len(items)):
        raise AuditError("正规规划的命令单元非法或重复")
    commands = {item["unit_id"]: item for item in items}
    if set(unit_ids) - set(commands):
        raise AuditError("选择的命令不在正规规划中")
    for unit_id in unit_ids:
        item = commands[unit_id]
        if (not isinstance(item.get("argv"), list) or not item["argv"]
                or not all(isinstance(part, str) and part for part in item["argv"])
                or not isinstance(item.get("cwd"), str) or not item["cwd"].startswith("/")
                or item.get("inheritable", True) is not True):
            raise AuditError("选择的正规命令规格缺失或明确不可承接：" + unit_id)
    copied = json.loads(json.dumps(plan, ensure_ascii=False))
    return copied, {item["unit_id"]: item for item in copied["units"]}


def _apply_prepared_policy(plan: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """生成能直接交给原 run-gates 的新规划；仅替换选中项的输入声明，不改变旧文件或原记录。"""
    _records_module().validate_audit_policy(policy, {item["unit_id"] for item in plan["units"]})
    plan["audit_policy"] = policy
    for item in plan["units"]:
        if item["unit_id"] in policy["units"]:
            item["inputs"] = policy["units"][item["unit_id"]]
    return plan


def prepare_audit_policy(plan_path: Path, unit_ids: Sequence[str], temporary_root: str) -> dict[str, Any]:
    """首轮只启用实际完整采集；空宿主声明会使有读取的首轮失败，不能因此承接其记录。"""
    plan, commands = _policy_plan(_checked_json(plan_path), unit_ids)
    runtime = _runtime_module()
    policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
    redirected = {}
    for unit_id in sorted(unit_ids):
        command = commands[unit_id]
        if unit_id.startswith("pre-a3:claude:") and unit_id.removeprefix("pre-a3:claude:") in runtime.CONTROL_SCENARIOS:
            candidate = list(command["argv"])
            candidate[-1] = str(Path(temporary_root) / "staging")
            if runtime.control_scenario(candidate, temporary_root) != unit_id.removeprefix("pre-a3:claude:"):
                raise AuditError("四条 control 只能接原完整 run-scenario 命令，不能改写其它命令")
            redirected[unit_id] = {"original_staging_parent": command["argv"][-1], "temporary_staging_parent": candidate[-1]}
            command["argv"] = candidate
            if "CLAUDE_RECORDED_CHAIN_RECORD_DIR" in command.get("env", {}):
                command["env"]["CLAUDE_RECORDED_CHAIN_RECORD_DIR"] = str(Path(temporary_root) / "staging" /
                        "claude-chain-records" / hashlib.sha256(unit_id.encode()).hexdigest()[:16])
        masked = commands[unit_id]["argv"][:len(runtime.LAUNCHER)] == runtime.LAUNCHER
        contract = runtime.validate_contract({"schema_version": runtime.SCHEMA, "temporary_root": temporary_root,
                                             "mask_roots": [runtime.MASK_ROOT] if masked else [],
                                             "environment_paths": sorted(runtime.ENVIRONMENT_PATHS)})
        policy["units"][unit_id] = {"host_paths": [], "require_read_audit": True, "runtime_contract": contract}
    return {"policy": policy, "plan": _apply_prepared_policy(plan, policy),
            "provenance": {"status": "prepared_only", "requires_validation": True, "reuse_enabled": False,
                           "source_plan": {"path": str(plan_path), "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest()},
                           "selected_units": sorted(unit_ids),
                           "control_output_redirections": redirected,
                           "statement": "首轮须由原执行器实际采集；不追认旧记录，未选中项保持只能重跑。"}}


def bind_audit_policy(plan_path: Path, manifest_path: Path, unit_ids: Sequence[str]) -> dict[str, Any]:
    """已发布正式首轮清单只提供路径准备；第二轮仍须真实严格执行，不能聚合别 run 或补录旧轨迹。"""
    plan, commands = _policy_plan(_checked_json(plan_path), unit_ids)
    records, runtime = _records_module(), _runtime_module()
    manifest = _checked_json(manifest_path)
    store_root = manifest.get("record_store")
    if not isinstance(store_root, str) or not Path(store_root).is_absolute():
        raise AuditError("策略绑定缺少同一正式记录库")
    if any(path.is_symlink() for path in (Path(store_root), *Path(store_root).parents)):
        raise AuditError("正式记录库不能经过符号链接")
    store = records.RecordStore(Path(store_root))
    if str(store.root) != store_root:
        raise AuditError("正式记录库必须使用规范绝对坐标")
    run_id = manifest.get("run_id")
    if (not isinstance(run_id, str) or re.fullmatch(r"[0-9A-Za-z._-]+", run_id) is None
            or manifest.get("mode") != "re-execute"
            or _checked_json(store.manifest_path(run_id)) != manifest):
        raise AuditError("首轮须是同库已发布的重新执行清单，不能拼接别 run")
    problems = records.verify_manifest(manifest, store=store)
    if problems:
        raise AuditError("正式首轮清单不能复核：" + "；".join(problems))
    entries = {item["unit_id"]: item for item in manifest["units"]}
    if set(unit_ids) - set(entries):
        raise AuditError("选择的单元不在正式首轮清单中")
    policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
    prepared = {}
    for unit_id in sorted(unit_ids):
        item = entries[unit_id]
        path = Path(item["record_path"])
        record = _checked_json(path)
        audit, spec = record.get("read_audit") or {}, record.get("spec") or {}
        expected_env = {key: value for key, value in commands[unit_id].get("env", {}).items() if key not in records.ENV_CACHE_ONLY}
        if (item.get("disposition") != "executed" or record.get("unit_type") != "command"
                or record.get("run", {}).get("run_id") != run_id
                or store.record_path(unit_id, record["record_sha256"]) != path
                or spec.get("type") != "command" or spec.get("unit_id") != unit_id
                or spec.get("argv") != commands[unit_id]["argv"] or spec.get("cwd") != commands[unit_id]["cwd"]
                or spec.get("env") != expected_env or records.sha256_json(spec) != record.get("spec_sha256")
                or any(spec.get(key) != commands[unit_id][key] for key in
                       ("cores", "memory_mb", "exclusive", "timeout_seconds", "reservation_restartable") if key in commands[unit_id])
                or records.entries_sha256(record.get("inputs")) != record.get("inputs_sha256")
                or records.entries_sha256(record.get("environment")) != record.get("environment_sha256")
                or record.get("timed_out") is not False or record.get("signal") is not None
                or records.parse_utc(record.get("completed_at_utc")) is None
                or audit.get("inputs_sha256") != record.get("inputs_sha256")):
            raise AuditError("正式首轮记录、规划规格或摘要不闭合：" + unit_id)
        trace_sha256 = audit.get("trace", {}).get("sha256")
        if not isinstance(trace_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", trace_sha256) is None:
            raise AuditError("首轮缺少原始完整轨迹：" + unit_id)
        trace_path = store.log_path(trace_sha256)
        document = _checked_json(trace_path)
        coverage = document.get("coverage") or {}
        if (hashlib.sha256(trace_path.read_bytes()).hexdigest() != trace_sha256
                or document.get("schema_version") != RUNTIME_TRACE_SCHEMA or document.get("roots") != ["/"]
                or coverage.get("all_host_paths") is not True or coverage.get("stream_complete") is not True
                or type(coverage.get("unresolved")) is not int or coverage["unresolved"] != 0
                or type(coverage.get("unfinished_calls")) is not int or coverage["unfinished_calls"] != 0
                or type(coverage.get("unresolved_process_events")) is not int or coverage["unresolved_process_events"] != 0
                or coverage.get("unresolved_reasons") != {} or type(document.get("lines")) is not int or document["lines"] <= 0):
            raise AuditError("首轮轨迹未完整解析；不能补轨迹或追认：" + unit_id)
        covered = runtime.replay_runtime(document, record["inputs"])
        accesses = document.get("accesses")
        if (not isinstance(accesses, list) or not accesses
                or any(not isinstance(access, list) or len(access) != 2 or not isinstance(access[0], str)
                       or not isinstance(access[1], list) or not access[1] for access in accesses)
                or len({access[0] for access in accesses}) != len(accesses)):
            raise AuditError("首轮完整路径集合缺失或重复：" + unit_id)
        snapshots = []
        for actual, kinds in accesses:
            if actual in covered:
                continue
            entry = host_snapshot(actual)
            kind = entry["detail"]["snapshot"]["kind"]
            allowed = {"read", "exec", "stat"} if kind == "file" else METADATA_KINDS | {"read"} if kind == "directory" else {"missing"}
            if set(kinds) - allowed:
                raise AuditError("实际访问类型超出精确宿主合同：" + unit_id + ":" + actual)
            snapshots.append(entry)
        contract = document["runtime"]["context"]["contract_entry"]["detail"]["contract"]
        control = document["runtime"]["context"].get("control_chain")
        if (control is None or control["scenario"] != unit_id.removeprefix("pre-a3:claude:")
                or runtime.control_scenario(spec["argv"], contract["temporary_root"]) != control["scenario"]):
            raise AuditError("正式首轮四链规格与可信隔离场景不符：" + unit_id)
        policy["units"][unit_id] = {"host_paths": sorted(entry["detail"]["path"] for entry in snapshots),
                                   "require_read_audit": True, "runtime_contract": contract}
        prepared[unit_id] = {"source_record": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                             "source_trace": {"path": str(trace_path), "sha256": trace_sha256},
                             "host_snapshots": snapshots, "runtime_paths": sorted(covered)}
    return {"policy": policy, "plan": _apply_prepared_policy(plan, policy),
            "provenance": {"status": "prepared_only", "requires_validation": True, "reuse_enabled": False,
                           "source_manifest": {"path": str(store.manifest_path(run_id)), "sha256": manifest["manifest_sha256"]},
                           "selected_units": sorted(unit_ids), "units": prepared,
                           "statement": "只绑定真实路径准备；新严格执行通过原校验后才形成可承接来源。"}}


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter", help="读标准输入的 strace 输出，只留各个根之下的路径，写紧凑 JSON")
    filt.add_argument("--output", type=Path, required=True)
    filt.add_argument("--root", action="append", default=[], required=True)
    filt.add_argument("--strict", action="store_true")
    filt.add_argument("--runtime-context", type=Path)
    for name in ("prepare-policy", "bind-policy"):
        policy = sub.add_parser(name, help="准备真实采集策略或绑定正式首轮轨迹，并生成供原 run-gates 消费的新清单")
        policy.add_argument("--plan", type=Path, required=True)
        policy.add_argument("--unit", action="append", required=True)
        policy.add_argument("--output", type=Path, required=True)
        policy.add_argument("--output-plan", type=Path, required=True)
        policy.add_argument("--report", type=Path, required=True)
        if name == "prepare-policy":
            policy.add_argument("--temporary-root", required=True)
        else:
            policy.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command in {"prepare-policy", "bind-policy"}:
        try:
            targets = (args.output, args.output_plan, args.report)
            if len(set(targets)) != len(targets) or any(target.exists() or target.is_symlink() for target in targets):
                raise AuditError("策略、执行清单与报告必须是三个尚不存在的新文件")
            result = (prepare_audit_policy(args.plan, args.unit, args.temporary_root) if args.command == "prepare-policy"
                      else bind_audit_policy(args.plan, args.manifest, args.unit))
            for target, value in zip(targets, (result["policy"], result["plan"], result["provenance"])):
                with target.open("x", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
            return 0
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            print("读集策略准备失败：" + str(error), file=sys.stderr)
            return 2
    if args.command == "filter":
        stream = (line.decode("utf-8", "surrogateescape") for line in sys.stdin.buffer)
        runtime_context = json.loads(args.runtime_context.read_text()) if args.runtime_context else None
        document = filter_stream(stream, args.root, strict=args.strict, runtime_context=runtime_context)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8", errors="surrogateescape")
        os.replace(temporary, args.output)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
