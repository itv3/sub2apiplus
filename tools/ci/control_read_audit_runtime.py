#!/usr/bin/env python3
"""四条Claude control的固定当前运行时；历史runtime与gate读取器原字节守恒。

普通命令直接委派历史runtime。只有原完整四control命令能进入本轮新网络空间、
准确内核读取与生成产物扩展；当前执行器及消费验证器共同冻结这份sidecar。
"""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
_HERE = Path(__file__).resolve()
_LEGACY_KEY = "_control_runtime_legacy_" + str(_HERE.parent).replace("/", "_")
if _LEGACY_KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_LEGACY_KEY, _HERE.with_name("read_audit_runtime.py"))
    _legacy = importlib.util.module_from_spec(_spec)
    sys.modules[_LEGACY_KEY] = _legacy
    _spec.loader.exec_module(_legacy)
else:
    _legacy = sys.modules[_LEGACY_KEY]
# 复用原函数对象及环境合同，不改原函数globals，也不改历史worker预载序列。
globals().update({name: value for name, value in vars(_legacy).items()
                  if not name.startswith("__") and name not in {"HERE", "main"}})
HERE = _HERE
import socket
CONTROL_DATA_ROOT = "/root/docker/capture-cli/data"
CONTROL_CONTEXT_ENV = "UNIT_EXECUTOR_READ_AUDIT_CONTROL_CONTEXT"
CONTROL_CONTEXT_DIGEST_ENV = "UNIT_EXECUTOR_READ_AUDIT_CONTROL_CONTEXT_SHA256"
CONTROL_SCENARIOS = {"vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution"}
CONTROL_MODULE = "tools.official_client_capture.codex_upgrade_pre_a3_certification"


def _read_audit():
    key = "_control_runtime_audit_" + str(HERE.parent).replace("/", "_")
    if key not in sys.modules:
        path = HERE.with_name("control_read_audit.py")
        if path.is_symlink() or not path.is_file():
            raise RuntimeContractError("固定control审计adapter缺失或为链接")
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def validate_context(context, inputs, *, historical=False):
    contract = _legacy.validate_context(context, inputs, historical=historical)
    _validate_control_context(context, contract)
    return contract


def control_scenario(argv: list[str], temporary_root: str) -> str | None:
    """仅识别原四条 control 的完整入口；固定79、VC1与故障世界不能借用空盘合同。"""
    command = list(argv)
    if len(command) > 1 and command[1] == "-B":
        command.pop(1)
    if (len(command) == 10 and command[0] == sys.executable and command[1:7] ==
            ["-m", CONTROL_MODULE, "run-scenario", "--client", "claude", "--name"]
            and command[7] in CONTROL_SCENARIOS and command[8] == "--staging-parent"
            and command[9] == str(Path(temporary_root) / "staging")):
        return command[7]
    return None


def _validate_control_context(context: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any] | None:
    """四条 control 的实测空间与固定挂载凭证；不授予任意 namespace、proc或网络合同。"""
    control = context.get("control_chain")
    if control is None:
        return None
    fields = {"scenario", "parent_network_namespace", "network_namespace", "interfaces", "data_mount", "mask_mount",
              "context_path", "setup_trace", "setup_trace_sha256", "empty_mask", "filesystem_ids"}
    if not isinstance(control, dict) or set(control) != fields or control.get("scenario") not in CONTROL_SCENARIOS:
        raise RuntimeContractError("固定 control 隔离凭证格式非法")
    parent, child = control["parent_network_namespace"], control["network_namespace"]
    if (not all(isinstance(value, str) and re.fullmatch(r"net:\[\d+\]", value) for value in (parent, child))
            or parent == child or control["interfaces"] != ["lo"]
            or under(contract["temporary_root"], CONTROL_DATA_ROOT)
            or not isinstance(control["context_path"], str) or not control["context_path"].startswith("/")
            or str(Path(control["context_path"])) != control["context_path"]
            or ".." in Path(control["context_path"]).parts or under(control["context_path"], contract["temporary_root"])):
        raise RuntimeContractError("固定 control 未证明新网络空间、精确凭证坐标或独立临时盘")
    for key, target in (("data_mount", CONTROL_DATA_ROOT), ("mask_mount", MASK_ROOT)):
        row = control[key]
        if row is None:
            continue
        if (not isinstance(row, dict) or row.get("target") != target or type(row.get("mount_id")) is not int
                or row["mount_id"] <= 0 or "ro" not in row.get("options", [])
                or (key == "mask_mount" and (row.get("filesystem") != "tmpfs" or control["empty_mask"] is not True))):
            raise RuntimeContractError("固定 control 的只读数据根或空遮挡实测不符")
    if control["mask_mount"] is None and control["empty_mask"] is not None:
        raise RuntimeContractError("不存在的遮挡根不能有空盘凭证")
    expected_ids = {contract["temporary_root"], *[target for key, target in
                    (("data_mount", CONTROL_DATA_ROOT), ("mask_mount", MASK_ROOT)) if control[key] is not None]}
    if (not isinstance(control["filesystem_ids"], dict) or set(control["filesystem_ids"]) != expected_ids
            or not all(isinstance(value, str) and value.isdigit() for value in control["filesystem_ids"].values())):
        raise RuntimeContractError("固定 control 缺少实际可见文件系统身份")
    trace = control["setup_trace"]
    if not isinstance(trace, str) or hashlib.sha256(trace.encode()).hexdigest() != control["setup_trace_sha256"]:
        raise RuntimeContractError("固定 control 启动挂载轨迹摘要不符")
    calls = []
    for line in trace.splitlines():
        match = re.fullmatch(r'\d+\s+mount\("([^"\\]*)", "([^"\\]*)", (NULL|"[^"\\]*"), ([A-Z0-9_|]+), (NULL|"[^"\\]*")\)\s*= 0', line)
        if match is None:
            raise RuntimeContractError("固定 control 启动含失败、未知或额外挂载")
        calls.append(match.groups())
    expected = []
    if control["data_mount"] is not None:
        expected.extend([(CONTROL_DATA_ROOT, {"MS_BIND"}),
                         (CONTROL_DATA_ROOT, {"MS_RDONLY", "MS_REMOUNT", "MS_BIND"})])
    if control["mask_mount"] is not None and MASK_ROOT not in contract["mask_roots"]:
        expected.append((MASK_ROOT, {"MS_RDONLY"}))
    if len(calls) != len(expected):
        raise RuntimeContractError("固定 control 启动挂载数量不符")
    for call, (target, flags) in zip(calls, expected):
        source, actual, filesystem, actual_flags, options = call
        if actual != target or set(actual_flags.split("|")) != flags:
            raise RuntimeContractError("固定 control 启动挂载目标或标志不符")
        if target == CONTROL_DATA_ROOT:
            if source != target or filesystem != "NULL" or options != "NULL":
                raise RuntimeContractError("固定 control 只读 bind 的来源或参数不符")
        elif (source != "tmpfs" or filesystem != '"tmpfs"'
              or set(options.strip('"').split(",")) != {"size=64k", "mode=0755"}):
            raise RuntimeContractError("固定 control 空遮挡参数不符")
    return control


class OutputTracker(_legacy.OutputTracker):
    """按调用顺序证明生成对象归属；仅接普通文件发布及失败证据的同父目录归档。"""

    def __init__(self, contract: dict[str, Any]) -> None:
        super().__init__(contract)
        # 原子发布必须来自同一条调用的相邻源／目标事件，不能用另一条写入补齐半个调用。
        self.pending_move: tuple[str, str, str, str, bool, dict[str, str] | None] | None = None
        # 回退只能恢复本轮已经完整观察的归档，不能凭目录名称后缀取得恢复资格。
        self.directory_archives: dict[str, str] = {}

    def _archive_subtree(self, source: str, destination: str) -> dict[str, str] | None:
        """限定真实失败证据归档及其对称回退，返回闭合的本轮普通对象子树。"""
        if (source == self.root or self.created.get(source) != "directory"
                or Path(source).parent != Path(destination).parent
                or self.created.get(str(Path(source).parent)) != "directory"
                or any(under(item, destination) for item in self.created)):
            return None
        restoring = self.directory_archives.get(source) == destination
        if not restoring and (source in self.directory_archives or not re.fullmatch(
                re.escape(Path(source).name) + r"\.failed-attempt[1-9][0-9]*(?:-(?:[2-9]|[1-9][0-9]+))?",
                Path(destination).name)):
            return None
        subtree = {item: object_kind for item, object_kind in self.created.items() if under(item, source)}
        if any(object_kind not in {"file", "directory"} or (item != source
               and subtree.get(str(Path(item).parent)) != "directory") for item, object_kind in subtree.items()):
            return None
        return subtree

    def observe(self, path: str, kind: str, name: str, body: str, succeeded: bool, target: str | None = None) -> None:
        if not under(path, self.root):
            return
        self.covered.add(path)
        if self.pending_move is not None:
            source, destination, operation, flags, success, subtree = self.pending_move
            self.pending_move = None
            if (path, target, name, body, succeeded, kind) != (destination, source, operation, flags, success, "write"):
                self.problems.add("原子移动缺少同调用的目标事件：" + source)
                return
            if success:
                if subtree is not None:
                    restoring = self.directory_archives.get(source) == destination
                    for item in subtree:
                        self.created.pop(item)
                    self.created.update({destination + item[len(source):]: object_kind
                                         for item, object_kind in subtree.items()})
                    # 若闭合子树内部已有归档，随物理目录迁移其准确坐标，不能留下旧根映射。
                    self.directory_archives = {
                        destination + archived[len(source):] if under(archived, source) else archived:
                        destination + original[len(source):] if under(original, source) else original
                        for archived, original in self.directory_archives.items() if archived != source}
                    if not restoring:
                        self.directory_archives[destination] = source
                    return
                if operation in {"rename", "renameat", "renameat2"}:
                    self.created.pop(source, None)
                self.created[destination] = "file"
            return
        if name in {"open", "openat", "creat"} and succeeded and (target is None or not under(target, self.root)):
            self.problems.add("文件描述符未绑定到私有临时盘：" + path)
            return
        if name in {"rename", "renameat", "renameat2", "link", "linkat"}:
            linking = name in {"link", "linkat"}
            expected_flags = "RENAME_FLAGS_0" if name == "renameat2" else "LINK_FLAGS_0" if name == "linkat" else ""
            if linking and not succeeded:
                expected_flags = "|".join(sorted(filter(None, (expected_flags, "OUTPUT_FAILED_EEXIST"))))
            if (name == "rename" and kind == "write" and succeeded and body == ""
                    and isinstance(target, str) and str(Path(target)) == target
                    and ".." not in Path(target).parts and under(target, self.root)
                    and self.created.get(path) == "directory"):
                subtree = self._archive_subtree(path, target)
                if subtree is None:
                    self.problems.add("失败证据目录归档缺少同父、缺失目标或闭合生成子树证明：" + path)
                else:
                    self.pending_move = (path, target, name, body, succeeded, subtree)
                return
            if (kind != "write" or (not succeeded and not linking) or not isinstance(target, str)
                    or str(Path(target)) != target or ".." in Path(target).parts or not under(target, self.root)
                    or self.created.get(path) != "file"
                    or self.created.get(str(Path(target).parent)) != "directory"
                    or (self.created.get(target) != (None if succeeded else "file") if linking
                        else self.created.get(target) not in (None, "file"))
                    or body != expected_flags):
                self.problems.add("原子发布只允许同盘已生成普通文件与明确标志：" + path)
            else:
                self.pending_move = (path, target, name, body, succeeded, None)
            return
        if name in {"symlink", "symlinkat"}:
            self.problems.add("链接或移动操作尚无输出归属证明：" + path)
            return
        known = self.created.get(path)
        if kind == "write" and not succeeded and name in {"mkdir", "mkdirat"} and (
                (body == "OUTPUT_FAILED_EEXIST" and known == "directory")
                or (body == "OUTPUT_FAILED_ENOENT" and known is None
                    and under(str(Path(path).parent), self.root) and self.created.get(str(Path(path).parent)) is None)):
            return
        if kind == "write" and succeeded and known and name in {
                "chmod", "fchmodat", "fchmodat2", "chown", "lchown", "fchownat", "utime", "utimes", "utimensat", "truncate", "truncate64"}:
            return
        super().observe(path, kind, name, body, succeeded, target)
        if succeeded and name in {"unlink", "unlinkat", "rmdir"} and path not in self.created:
            self.directory_archives.pop(path, None)

    def report(self) -> dict[str, Any]:
        problems = set(self.problems)
        if self.pending_move is not None:
            problems.add("原子移动缺少同调用的目标事件：" + self.pending_move[0])
        return {"root": self.root, "paths": sorted(self.covered), "problems": sorted(problems)}


def replay_runtime(document: dict[str, Any], inputs: list[dict[str, Any]], *, historical: bool = False) -> set[str]:
    """重放输出生成顺序、隔离身份和结束凭证；只返回确已证明的路径，不能按目录整段放行。"""
    runtime = document.get("runtime") or {}
    if runtime.get("context", {}).get("control_chain") is None:
        return _legacy.replay_runtime(document, inputs, historical=historical)
    contract = validate_context(runtime.get("context"), inputs, historical=historical)
    completion = runtime.get("completion") or {}
    if any(completion.get(key) is not True for key in
           ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged", "command_finished")):
        raise RuntimeContractError("隔离执行未结束或输入／环境在执行期间变化")
    control = _validate_control_context(runtime["context"], contract)
    if control is not None and completion.get("control_isolation_unchanged") is not True:
        raise RuntimeContractError("固定 control 的网络、实际挂载或可信凭证在执行期间变化")
    host_schema = _read_audit().HOST_SCHEMA
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == host_schema]
    if completion.get("host_entries_sha256") != digest(entries):
        raise RuntimeContractError("实际命名空间中的输入快照未绑定执行记录")
    tracker = OutputTracker(contract)
    events = runtime.get("output_events")
    if not isinstance(events, list) or len(events) > 200000:
        raise RuntimeContractError("输出归属轨迹缺失或超过上限")
    seen: dict[str, set[str]] = {}
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"path", "kind", "name", "succeeded", "flags", "target"}
                or not all(isinstance(event[key], str) for key in ("path", "kind", "name"))
                or not isinstance(event["succeeded"], bool) or not isinstance(event["flags"], list)
                or not all(isinstance(flag, str) and (re.fullmatch(r"O_[A-Z0-9_]+", flag)
                           or flag in {"RENAME_FLAGS_0", "RENAME_FLAGS_UNSUPPORTED", "LINK_FLAGS_0", "LINK_FLAGS_UNSUPPORTED",
                                       "OUTPUT_FAILED_EEXIST", "OUTPUT_FAILED_ENOENT"}) for flag in event["flags"])
                or (event["target"] is not None and not isinstance(event["target"], str))
                or not under(event["path"], tracker.root)):
            raise RuntimeContractError("输出归属事件格式错误或越界")
        tracker.observe(event["path"], event["kind"], event["name"], "|".join(event["flags"]), event["succeeded"], event["target"])
        seen.setdefault(event["path"], set()).add(event["kind"])
    if tracker.report() != runtime.get("outputs") or tracker.report()["problems"]:
        raise RuntimeContractError("生成输出存在先读后写、来源不明或链接越界")
    paths = dict(document.get("accesses") or [])
    expected = {path: set(kinds) for path, kinds in paths.items() if under(path, tracker.root)}
    if seen != expected:
        raise RuntimeContractError("输出归属与完整路径集合不闭合")
    covered = set(seen)
    control_events = runtime.get("control_events", [])
    if not isinstance(control_events, list) or len(control_events) > 200000 or (control is None and control_events):
        raise RuntimeContractError("固定 control 的精确内核／启动凭证事件非法")
    control_seen: dict[str, set[str]] = {}
    for event in control_events:
        if (not isinstance(event, dict) or set(event) != {"path", "kind", "name", "pid", "succeeded", "errno", "target"}
                or not all(isinstance(event[key], str) for key in ("path", "kind", "name", "pid"))
                or not event["pid"].isdigit() or event["succeeded"] is not True or event["errno"] is not None):
            raise RuntimeContractError("固定 control 精确事件格式或实际结果不符")
        path, name, kind, pid = event["path"], event["name"], event["kind"], event["pid"]
        namespace = re.fullmatch(r"/proc/(self|" + re.escape(pid) + r")/ns/(mnt|net)", path)
        if namespace:
            expected = runtime["context"]["namespace"] if namespace[2] == "mnt" else control["network_namespace"]
            if name not in {"readlink", "readlinkat"} or kind != "stat" or event["target"] != expected:
                raise RuntimeContractError("固定 control 只能读取自身已实测命名空间链接")
        elif path == control["context_path"]:
            if (kind not in {"read", "stat"} or name not in {"open", "openat", "stat", "lstat", "newfstatat", "statx"}
                    or (name in {"open", "openat"} and event["target"] != path)
                    or (name not in {"open", "openat"} and event["target"] is not None)):
                raise RuntimeContractError("固定 control 可信启动凭证只许精确只读访问")
        else:
            raise RuntimeContractError("固定 control 不支持其它内核路径或启动侧件")
        control_seen.setdefault(path, set()).add(kind)
    for path, kinds in control_seen.items():
        if kinds != set(paths.get(path, [])):
            raise RuntimeContractError("固定 control 精确事件与完整读集不闭合")
        covered.add(path)
    terminal_events = runtime.get("terminal_events", [])
    terminal_kinds = set()
    for event in terminal_events:
        if not isinstance(event, dict) or set(event) != {"name", "kind", "errno", "succeeded"}:
            raise RuntimeContractError("控制终端观察格式错误")
        terminal_kinds.add(event["kind"])
        if event["name"] in {"open", "openat"} and event["errno"] == "ENXIO" and event["succeeded"] is False:
            continue
        if event["name"] in {"stat", "lstat", "newfstatat", "statx", "access", "faccessat", "faccessat2"} and event["kind"] == "stat" and event["succeeded"] is True:
            continue
        raise RuntimeContractError("实际控制终端访问超出无终端探测合同")
    if terminal_kinds != set(paths.get("/dev/tty", [])):
        raise RuntimeContractError("控制终端轨迹与访问集合不一致")
    if terminal_kinds:
        covered.add("/dev/tty")
    environment = {entry["path"]: entry for entry in runtime["context"]["contract_entry"]["detail"]["environment"]}
    for path, kinds in paths.items():
        if path == "/dev/null" and set(kinds) <= {"read", "write", "stat"}:
            covered.add(path)
        elif path in environment:
            allowed = {"missing"} if environment[path]["kind"] == "missing" else {"read", "stat"}
            if set(kinds) <= allowed:
                covered.add(path)
        else:
            for mask in contract["mask_roots"]:
                if path == mask and set(kinds) <= {"dir", "stat", "list"}:
                    covered.add(path)
                elif under(path, mask) and set(kinds) <= {"missing"}:
                    covered.add(path)
            if control is not None and control["mask_mount"] is not None:
                if path == MASK_ROOT and set(kinds) <= {"dir", "stat", "list", "read"}:
                    covered.add(path)
                elif under(path, MASK_ROOT) and set(kinds) <= {"missing"}:
                    covered.add(path)
    _replay_test_export(document, inputs, contract, covered)
    return covered


def _checked_context_file(path: Path, context: dict[str, Any]) -> bool:
    """载荷可读的可信启动凭证在结束时须逐对象相同；不能让本轮改写的侧件维持隔离声明。"""
    return not path.is_symlink() and json.loads(path.read_text()) == context


def _control_mount(source: str, target: str, filesystem: str, flags: int, options: str, path: Path) -> str:
    """可信启动端直接调用固定 mount 参数；不调用载荷脚本，不容许 fstab 或任意 shell 改写挂载来源。"""
    script = ("import ctypes, os, sys\n"
              "library = ctypes.CDLL(None, use_errno=True)\n"
              "library.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]\n"
              "values = [value.encode() if value else None for value in sys.argv[1:4]]\n"
              "result = library.mount(*values, int(sys.argv[4]), sys.argv[5].encode() if sys.argv[5] else None)\n"
              "if result: raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))\n")
    result = subprocess.run(["strace", "-f", "-qq", "-s", "4096", "-e", "trace=mount", "-o", str(path), "--",
                             sys.executable, "-B", "-c", script, source, target, filesystem, str(flags), options],
                            stdin=subprocess.DEVNULL, check=False)
    trace = path.read_text()
    if result.returncode:
        raise RuntimeContractError("固定 control 可信启动挂载失败")
    return trace


def run_child(request_path: Path, expected_digest: str) -> int:
    request = json.loads(request_path.read_text())
    if digest(request) != expected_digest:
        raise RuntimeContractError("启动请求摘要不符")
    entry = request["contract_entry"]
    contract = validate_contract(entry["detail"]["contract"])
    if entry != contract_entry(contract):
        raise RuntimeContractError("启动环境与登记时不一致")
    namespace = os.readlink("/proc/self/ns/mnt")
    if namespace == request["parent_namespace"]:
        raise RuntimeContractError("未进入独立挂载命名空间")
    root = Path(contract["temporary_root"])
    scenario = control_scenario(request["argv"], str(root))
    if (scenario is None or request.get("control_scenario") != scenario
            or (scenario is not None and under(str(root), CONTROL_DATA_ROOT))):
        raise RuntimeContractError("固定 control 请求与原入口或临时根不符")
    if any(path.is_symlink() for path in (root, *root.parents)) or any(root.iterdir()):
        raise RuntimeContractError("宿主临时挂载点非空或含链接")
    trace_path = Path(request["output"])
    test_transport = request.get("test_transport")
    bound_inputs = [entry, *request["host_entries"]]
    if test_transport is not None:
        if not isinstance(request.get("test_spec"), dict):
            raise RuntimeContractError("受管请求缺少完整测试规格输入")
        bound_inputs.append(request.get("test_spec"))
        _test_transport_check(test_transport, contract, bound_inputs)
        if (request["argv"] != _runtime_test_command(test_transport)
                or Path(test_transport["result_path"]).parent != trace_path.parent.parent / "units"):
            raise RuntimeContractError("受管请求的测试命令或导出坐标错绑")
    setup_parts, setup_commands = [], []
    operations = [("private", ["mount", "--make-rprivate", "/"])]
    operations += [(mask, ["mount", "-t", "tmpfs", "-o", "ro,size=64k,mode=0755", "tmpfs", mask]) for mask in contract["mask_roots"]]
    operations.append((str(root), ["mount", "-t", "tmpfs", "-o", "size=512m,mode=0700,nosuid,nodev", "tmpfs", str(root)]))
    for index, (label, command) in enumerate(operations):
        path = trace_path.with_name(trace_path.name + f".mount-{index}")
        result = subprocess.run(["strace", "-f", "-qq", "-s", "4096", "-e", "trace=mount,umount2,unshare,setns",
                                 "-o", str(path), "--", *command], stdin=subprocess.DEVNULL, check=False)
        setup_parts.append(path.read_text()); setup_commands.append(label)
        if result.returncode:
            raise RuntimeContractError("隔离挂载失败，停止被测命令")
    rows = _mount_rows()
    wanted = {str(root), *contract["mask_roots"]}
    mounts = _visible_mounts(rows, wanted)
    context = {"schema_version": CONTEXT_SCHEMA, "contract_entry": entry, "parent_namespace": request["parent_namespace"],
               "namespace": namespace, "empty_temporary_root": not any(root.iterdir()),
               "empty_masks": {mask: not any(Path(mask).iterdir()) for mask in contract["mask_roots"]},
               "private_mounts": not any(any(value.startswith(("shared:", "master:")) for value in row["propagation"]) for row in rows),
               "mounts": mounts, "setup_commands": setup_commands, "setup_success": True,
               "setup_trace": "".join(setup_parts), "setup_trace_sha256": hashlib.sha256("".join(setup_parts).encode()).hexdigest()}
    context_path = trace_path.with_name(trace_path.name + ".context.json")
    if scenario is not None:
        network_namespace = os.readlink("/proc/self/ns/net")
        interfaces = sorted(name for _index, name in socket.if_nameindex())
        if network_namespace == request.get("parent_network_namespace") or interfaces != ["lo"]:
            raise RuntimeContractError("固定 control 必须有新的仅 lo 网络命名空间")
        control_trace = []
        targets = set()
        if Path(CONTROL_DATA_ROOT).is_dir():
            if Path(CONTROL_DATA_ROOT).is_symlink():
                raise RuntimeContractError("固定 control 数据根不能是链接")
            targets.add(CONTROL_DATA_ROOT)
            for index, flags in enumerate((4096, 4096 | 32 | 1)):
                control_trace.append(_control_mount(CONTROL_DATA_ROOT, CONTROL_DATA_ROOT, "", flags, "",
                                     trace_path.with_name(trace_path.name + f".control-data-{index}")))
        if Path(MASK_ROOT).is_dir():
            targets.add(MASK_ROOT)
            if MASK_ROOT not in contract["mask_roots"]:
                control_trace.append(_control_mount("tmpfs", MASK_ROOT, "tmpfs", 1, "size=64k,mode=0755",
                                     trace_path.with_name(trace_path.name + ".control-mask")))
        control_mounts = {row["target"]: row for row in _visible_mounts(_mount_rows(), targets)}
        context["control_chain"] = {"scenario": scenario, "parent_network_namespace": request["parent_network_namespace"],
                                    "network_namespace": network_namespace, "interfaces": interfaces,
                                    "data_mount": control_mounts.get(CONTROL_DATA_ROOT), "mask_mount": control_mounts.get(MASK_ROOT),
                                    "empty_mask": not any(Path(MASK_ROOT).iterdir()) if MASK_ROOT in targets else None,
                                    "context_path": str(context_path), "setup_trace": "".join(control_trace),
                                    "setup_trace_sha256": hashlib.sha256("".join(control_trace).encode()).hexdigest(),
                                    "filesystem_ids": {target: str(os.statvfs(target).f_fsid) for target in {str(root), *targets}}}
    _write(context_path, context)
    validate_context(context, [entry])
    audit = _read_audit()
    before = [audit.host_snapshot(path) for path in request["host_paths"]]
    if before != request["host_entries"]:
        raise RuntimeContractError("实际命名空间中的输入与登记快照不同")
    argv = audit.strace_argv(request["argv"], output=trace_path, roots=["/"], strict=True, runtime_context=context_path)
    environment = {**os.environ, "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root)}
    for key in (CONTROL_CONTEXT_ENV, CONTROL_CONTEXT_DIGEST_ENV):
        environment.pop(key, None)
    if scenario is not None:
        # 现有共享字节码层会用链接或符号链接准备副本，不能把宿主旧字节码冒作空盘生成物。
        # 四条 control 的新严格来源从源码运行，所有副本前缀都落在其本轮 staging 内。
        environment.pop("PYTHONPYCACHEPREFIX", None)
        environment.update({CONTROL_CONTEXT_ENV: str(context_path), CONTROL_CONTEXT_DIGEST_ENV: digest(context),
                            "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(argv, cwd=request["cwd"], env=environment,
                            stdin=subprocess.DEVNULL, check=False)
    after = [audit.host_snapshot(path) for path in request["host_paths"]]
    receipt = {"context": context, "host_inputs_unchanged": before == after, "environment_unchanged": entry == contract_entry(contract),
               "namespace_unchanged": namespace == os.readlink("/proc/self/ns/mnt"), "returncode": result.returncode,
               "request_sha256": expected_digest, "host_entries": before}
    if scenario is not None:
        observed = {row["target"]: row for row in _visible_mounts(_mount_rows(), targets)}
        receipt["control_isolation_unchanged"] = (os.readlink("/proc/self/ns/net") == network_namespace
             and sorted(name for _index, name in socket.if_nameindex()) == ["lo"]
             and observed == control_mounts and _checked_context_file(context_path, context)
             and {target: str(os.statvfs(target).f_fsid) for target in {str(root), *targets}} == context["control_chain"]["filesystem_ids"]
             and (MASK_ROOT not in targets or not any(Path(MASK_ROOT).iterdir())))
    _write(trace_path.with_name(trace_path.name + ".runtime.json"), receipt)
    if trace_path.is_file():
        document = json.loads(trace_path.read_text())
        document.setdefault("runtime", {})["completion"] = {
            key: receipt[key] for key in ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged")}
        document["runtime"]["completion"].update(command_finished=True, host_entries_sha256=digest(before),
                                                returncode=result.returncode, request_sha256=expected_digest)
        if scenario is not None:
            document["runtime"]["completion"]["control_isolation_unchanged"] = receipt["control_isolation_unchanged"]
        if test_transport is not None:
            try:
                exported = export_test_results(test_transport, contract, document, bound_inputs, returncode=result.returncode)
            except (RuntimeContractError, OSError, ValueError, TypeError, KeyError) as error:
                document["runtime"]["completion"]["test_export"] = {
                    "schema_version": TEST_EXPORT_SCHEMA, "status": "failed", "transport": test_transport, "error": str(error)}
                _write(trace_path, document)
                print(f"专用测试结果运输失败：{error}", file=sys.stderr)
                return 2
            document["runtime"]["completion"]["test_export"] = exported
        _write(trace_path, document)
    elif test_transport is not None:
        raise RuntimeContractError("完整审计轨迹缺失，不能导出测试结果")
    return result.returncode


def prepare(contract: dict[str, Any], *, output: Path, argv: list[str], cwd: str,
            inputs: list[dict[str, Any]], test_spec: dict[str, Any] | None = None) -> list[str]:
    """启动前建立专用空挂载点，写受管请求；实际文件快照在子命名空间再次核对。"""
    if control_scenario(argv, validate_contract(contract)["temporary_root"]) is None:
        return _legacy.prepare(contract, output=output, argv=argv, cwd=cwd, inputs=inputs, test_spec=test_spec)
    if test_spec is not None:
        raise RuntimeContractError("四control完整命令不能同时取得测试运输合同")
    if not sys.platform.startswith("linux") or shutil.which("unshare") is None:
        raise RuntimeContractError("隔离合同只在具备 unshare 的 Linux 上可用")
    contract = validate_contract(contract)
    root = Path(contract["temporary_root"])
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise RuntimeContractError("临时根不能含符号链接")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(root.iterdir()) or stat.S_IMODE(root.stat().st_mode) != 0o700 or root.stat().st_uid != os.geteuid():
        raise RuntimeContractError("宿主挂载点必须为空、归运行账号所有且权限为 0700")
    host_schema = _read_audit().HOST_SCHEMA
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == host_schema]
    request = {"contract_entry": contract_entry(contract), "parent_namespace": os.readlink("/proc/self/ns/mnt"),
               "output": str(output), "argv": argv, "cwd": cwd, "host_entries": entries,
               "host_paths": [entry["detail"]["path"] for entry in entries]}
    scenario = control_scenario(argv, str(root))
    if scenario is not None:
        if under(str(root), CONTROL_DATA_ROOT) or under(str(output), CONTROL_DATA_ROOT):
            raise RuntimeContractError("四条 control 的空盘与执行器审计输出必须独立于只读宿主数据根")
        request.update(control_scenario=scenario, parent_network_namespace=os.readlink("/proc/self/ns/net"))
    if test_spec is not None:
        payload = {"unit_id": test_spec["unit_id"], "test_ids": test_spec["test_ids"]}
        transport = {"schema_version": TEST_TRANSPORT_SCHEMA, "spec": test_spec, "spec_sha256": digest(test_spec),
                     "payload_sha256": digest(payload), "temporary_root": contract["temporary_root"], "result_path": argv[-1]}
        _test_transport_check(transport, contract, inputs)
        request.update(test_transport=transport, test_spec=test_spec_entry(test_spec),
                       argv=test_transport_argv(transport, contract, argv, output=output))
    path = output.with_name(output.name + ".request.json")
    _write(path, request)
    return ["unshare", "-m", *(["-n"] if scenario is not None else []), "--propagation", "unchanged", sys.executable, "-B", str(HERE),
            "--request", str(path), "--request-sha256", digest(request)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        return run_child(args.request, args.request_sha256)
    except (RuntimeContractError, OSError, ValueError, KeyError) as error:
        print(f"隔离读集取证失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
