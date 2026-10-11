"""A 组连续链的同世界接口；只构造外部来源，不代写候选结果或认证。

批准规则与原子 ID 保留，首次冻结时明确使用离线控制面画像接缝。官方字节、宿主和门禁环境是夹具来源；
候选 attempt、run、seal、compare、builder、accept、Store 与两本账均走产品实现。不能据此宣称全规则真实语义通过。
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import tempfile
import contextlib
import contextvars

from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_assertion_profile as profile
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_store as store_module
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_vc5_chain_fixture as sources

# 仅原故障链内部使用；不是产品成功收据，也不新增认证入口。
METHOD_RECORD_ENV = 'CLAUDE_FAULT_METHOD_RECORD_DIR'
METHOD_RESUME_ENV = 'CLAUDE_FAULT_METHOD_RESUME'
CHAIN_RESUME_ENV = 'CLAUDE_FAULT_CHAIN_RESUME'
EXECUTOR_SOURCE_ENV = 'CLAUDE_FAULT_EXECUTOR_SOURCE'
_ACTIVE_COMMAND_STEP = contextvars.ContextVar('claude_fault_active_command_step', default=None)
_VERIFIED_READ_ONLY_WORLD = contextvars.ContextVar('claude_fault_verified_read_only_world', default=None)


def record_root() -> Path | None:
    """原 runner 为每个原方法分配唯一记录坐标；普通单元测试保持原清理行为。"""

    value = os.environ.get(METHOD_RECORD_ENV)
    return Path(value).resolve(strict=True) if value else None


def _encoded(value: Any) -> Any:
    """只允许明示 JSON、路径、元组及原字节；不保存运行对象或可执行对象。"""

    if isinstance(value, Path):
        return {'$path': str(value)}
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, tuple):
        return {'$tuple': [_encoded(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {'$frozenset' if isinstance(value, frozenset) else '$set':
            sorted((_encoded(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))}
    if isinstance(value, list):
        return [_encoded(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return {'$pairs': [[_encoded(key), _encoded(item)] for key, item in value.items()]}
        return {key: _encoded(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'故障链停点不支持运行对象：{type(value).__name__}')


def _decoded(value: Any) -> Any:
    if isinstance(value, list):
        return [_decoded(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'$path'}:
            return Path(value['$path'])
        if set(value) == {'$bytes'}:
            return base64.b64decode(value['$bytes'], validate=True)
        if set(value) == {'$tuple'}:
            return tuple(_decoded(item) for item in value['$tuple'])
        if set(value) == {'$set'}:
            return {_decoded(item) for item in value['$set']}
        if set(value) == {'$frozenset'}:
            return frozenset(_decoded(item) for item in value['$frozenset'])
        if set(value) == {'$pairs'}:
            return {_decoded(key): _decoded(item) for key, item in value['$pairs']}
        return {key: _decoded(item) for key, item in value.items()}
    return value


def _state_path(name: str) -> Path | None:
    root = record_root()
    return root / (hashlib.sha256(name.encode()).hexdigest() + '.json') if root else None


def load_state(name: str) -> dict[str, Any] | None:
    """读同方法的明示停点；缺失不推导旧状态，损坏失败关闭。"""

    path = _state_path(name)
    if path is None or not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError('故障链停点路径不可信')
    envelope = json.loads(path.read_bytes())
    payload = envelope['payload']
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if envelope.get('sha256') != digest or payload.get('name') != name:
        raise ValueError('故障链停点摘要或名称不一致')
    return _decoded(payload['state'])


def save_state(name: str, state: dict[str, Any]) -> None:
    """在原方法记录目录原子保存必需状态，不代写任何 Campaign 产物。"""

    path = _state_path(name)
    if path is None:
        return
    payload = {'name': name, 'state': _encoded(state)}
    envelope = {'payload': payload, 'sha256': hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    descriptor, temporary = tempfile.mkstemp(prefix='.stop-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # 临时文件已落盘还不等于目录项已持久化；发布后同步父目录，保留断电恢复边界。
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _executor_path(relative: str) -> bool:
    """本轮只更新 capture 测试执行器；夹具原件、第二根和产品源码均不是执行器。"""

    path = Path(relative)
    return (relative == path.as_posix() and not path.is_absolute() and '..' not in path.parts
        and relative.startswith('tools/official_client_capture/tests/') and path.suffix == '.py'
        and not {'fixtures', 'versions', '__pycache__', '.git'}.intersection(path.parts))


def _executor_file(root: Path, relative: str, *, required: bool = True) -> Path:
    """沿准确目录核普通文件，不能把测试运输变成符号链接写入或宿主导入旁路。"""

    if not _executor_path(relative):
        raise ValueError(f'不是允许运输的测试执行器：{relative}')
    path = root / relative
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f'测试执行器目录缺失或含符号链接：{relative}')
    if path.is_symlink() or (path.exists() and not path.is_file()) or (required and not path.is_file()):
        raise ValueError(f'测试执行器不是普通文件：{relative}')
    return path


def _executor_product_files(tree: Path) -> dict[str, str]:
    """保存本次运输前后的实际非执行器字节；不把该快照当成工具演进或产品成功证明。"""

    files = {}
    for directory, folders, names in os.walk(tree, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in {'.git', '__pycache__', 'versions'}
                            and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(tree).as_posix()
            if path.suffix != '.pyc' and path.is_file() and not path.is_symlink() and not _executor_path(relative):
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def validate_executor_inputs(original_source: dict[str, Any], current_source: dict[str, Any]) -> None:
    """只分离本轮测试执行器变化；其它原冻结输入不允许被此本地运输掩盖。"""

    for key in ('repository', 'fixtures'):
        if original_source.get(key) != current_source.get(key):
            raise ValueError(f'原链来源坐标变化：{key}；不能通过测试执行器运输放行')
    original, current = original_source['files'], current_source['files']
    changed = sorted(path for path in set(original) | set(current) if original.get(path) != current.get(path))
    refused = [path for path in changed if not _executor_path(path) or path not in current]
    if refused:
        raise ValueError('原链非执行器输入变化：' + '、'.join(refused[:20]) + '；不重建世界、不自动重跑全集')


def prepare_executor_source(tree: Path, original_source: dict[str, Any], current_source: dict[str, Any],
                            executor_paths: list[str]) -> dict[str, Any]:
    """把准确测试修复送入原 tree，原 source、世界和产品字节保持不变。

    这是原 runner 的本地执行记录，不是新产品合同。修改旧测试执行器后必须真实运行当前重验，
    旧 unittest 输出不获当前认证。运输意图先保存，半途退出只能继续同一字节意图，不能趁机换源码。
    """

    if record_root() is None or tree.is_symlink() or not tree.is_dir():
        raise ValueError('测试执行器运输必须持有原链记录坐标与原普通 tree')
    validate_executor_inputs(original_source, current_source)
    original, current = original_source['files'], current_source['files']
    wanted = sorted(set(executor_paths))
    if not wanted or any(not _executor_path(path) or path not in current for path in wanted):
        raise ValueError('测试执行器静态闭包为空、缺源码或越过允许范围')
    source_root = Path(current_source['repository']).resolve(strict=True)
    tree = tree.resolve(strict=True)
    key = 'executor-source:' + str(tree)
    journal = load_state(key) or {'history': [], 'pending': None}
    previous = {}
    for binding in journal['history']:
        previous = _decoded(json.loads(read_cut(binding)))
        if previous['tree'] != str(tree):
            raise ValueError('测试执行器历史不属于原 tree')
        for row in previous.get('updates', []):
            for label, original_binding in row['bindings'].items():
                expected = row['before_sha256'] if label == 'before' else previous['files'][row['path']]
                if hashlib.sha256(read_cut(original_binding)).hexdigest() != expected:
                    raise ValueError('测试执行器历史源码原件变化')
    products = _executor_product_files(tree)
    desired = {path: current[path] for path in wanted}
    pending = journal['pending']
    if pending is None:
        rows = []
        for relative in wanted:
            target = _executor_file(tree, relative, required=False)
            before = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            known = previous.get('files', {}).get(relative, original.get(relative))
            if before != known:
                raise ValueError(f'原测试执行器字节与已记录来源不同：{relative}；禁止覆盖未知修改')
            origin = _executor_file(source_root, relative)
            raw = origin.read_bytes()
            if hashlib.sha256(raw).hexdigest() != current[relative]:
                raise ValueError(f'测试执行器源码在冻结后变化：{relative}')
            if before != current[relative]:
                rows.append((relative, before, raw, target.read_bytes() if target.is_file() else None))
        document = {'tree': str(tree), 'files': desired, 'product_files': products}
        if previous and all(previous.get(name) == value for name, value in document.items()):
            verify_executor_source(journal['history'][-1], tree)
            return journal['history'][-1]
        archive = Path(tempfile.mkdtemp(prefix='executor-source-', dir=record_root()))
        updates = []
        for number, (relative, before, raw, old_raw) in enumerate(rows):
            bindings = {}
            for label, content in (('before', old_raw), ('after', raw)):
                if content is not None:
                    path = archive / f'{number}-{label}.py'
                    with path.open('xb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    path.chmod(0o600)
                    bindings[label] = file_cut(path)
            updates.append({'path': relative, 'before_sha256': before, 'bindings': bindings})
        document['updates'] = updates
        pending = {'document': document, 'updates': updates, 'archive': archive}
        directory = os.open(archive, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        journal['pending'] = pending
        save_state(key, journal)
    if (pending['document']['tree'] != str(tree) or pending['document']['files'] != desired
            or pending['document']['product_files'] != products):
        raise ValueError('原测试运输尚未完成且本次意图或产品字节变化；保留停点，禁止换意图')
    for row in pending['updates']:
        target = _executor_file(tree, row['path'], required=False)
        raw = read_cut(row['bindings']['after'])
        after = desired[row['path']]
        if hashlib.sha256(raw).hexdigest() != after:
            raise ValueError('测试运输新字节与冻结意图不同')
        before = row['before_sha256']
        if before is not None and hashlib.sha256(read_cut(row['bindings']['before'])).hexdigest() != before:
            raise ValueError('测试运输原字节与冻结来源不同')
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if actual == after:
            continue
        if actual != before:
            raise ValueError(f'未完成测试运输目标被修改：{row["path"]}；禁止覆盖')
        descriptor, temporary = tempfile.mkstemp(prefix='.executor-', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if _executor_product_files(tree) != products:
        raise ValueError('测试运输改变了非执行器字节；保留现场，不执行原方法')
    path = pending['archive'] / 'executor.json'
    raw = (json.dumps(_encoded(pending['document']), sort_keys=True, ensure_ascii=False) + '\n').encode()
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise ValueError('测试执行器原清单已存在但与同次运输不同')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    binding = file_cut(path)
    verify_executor_source(binding, tree)
    journal['history'].append(binding)
    journal['pending'] = None
    save_state(key, journal)
    return binding


def verify_executor_source(binding: dict[str, Any], tree: Path, *, verify_product: bool = True) -> dict[str, Any]:
    """当前 invocation 实读运输原件和准确执行器字节；产品演进仍由原世界消费者判断。"""

    document = _decoded(json.loads(read_cut(binding)))
    tree = tree.resolve(strict=True)
    if document.get('tree') != str(tree) or not document.get('files'):
        raise ValueError('测试执行器绑定不属于本次原 tree')
    for relative, expected in document['files'].items():
        if hashlib.sha256(_executor_file(tree, relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f'当前测试执行器字节变化：{relative}')
    if verify_product and _executor_product_files(tree) != document['product_files']:
        raise ValueError('本次测试运输的产品或夹具字节变化')
    return document


def executor_invocation_binding(tree: Path) -> dict[str, Any] | None:
    """只消费原外层提供的内部绑定，不新增 CI 环境旁路；未设记录模式保持原入口。"""

    encoded = os.environ.get(EXECUTOR_SOURCE_ENV)
    if not encoded:
        return None
    binding = _decoded(json.loads(encoded))
    verify_executor_source(binding, tree, verify_product=False)
    return binding


_INITIALIZING: set[str] = set()


def initialization_allowed(name: str) -> bool:
    """只接受原游标明确声明尚未初始化的当前阶段，不能救活缺失或损坏停点。"""

    return name in _INITIALIZING


@contextlib.contextmanager
def initialization_scope(name: str, *, cursor: str, phase: str):
    """恢复到尚未首次建场的阶段时，先签正在初始化，再调用原构造器。"""

    if record_root() is None or not os.environ.get(METHOD_RESUME_ENV):
        yield
        return
    saved = load_state('cursor:' + cursor)
    initial = load_state('initialization:' + name)
    if (saved is None or saved.get('phase') != phase
            or saved.get('initializations', {}).get(name) != 'uninitialized' or initial is not None):
        raise ValueError('原游标没有精确的首次初始化许可，禁止重建缺失世界')
    save_state('initialization:' + name, {'cursor': cursor, 'phase': phase, 'state': 'started'})
    _INITIALIZING.add(name)
    try:
        yield
    finally:
        _INITIALIZING.discard(name)
    save_state('initialization:' + name, {'cursor': cursor, 'phase': phase, 'state': 'completed'})


def fixture_directory(case: Any, name: str) -> tuple[Path, bool]:
    """各层夹具只在首次创建根；恢复绑定原坐标，不移动、重建或清空世界。"""

    saved = load_state('fixture:' + name)
    if saved is not None:
        root = saved['root']
        if not os.environ.get(METHOD_RESUME_ENV) or root.is_symlink() or not root.is_dir():
            raise ValueError('旧夹具只能由原方法恢复入口绑定')
        return root, True
    if os.environ.get(METHOD_RESUME_ENV) and not initialization_allowed('fixture:' + name):
        raise ValueError('原方法缺少夹具停点，禁止重建旧世界')
    if record_root() is None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        return Path(directory.name).resolve(), False
    # 记录目录可能位于只读来源旁侧；世界只建在原可写 TMPDIR，记录仅保存坐标。
    root = Path(tempfile.mkdtemp(prefix='cfw-', dir=tempfile.gettempdir())).resolve()
    save_state('fixture:' + name, {'root': root})
    return root, False


# 夹具对象通过原构造/作用域绑定；这里只保存它们原有的值字段。
CASE_FIELDS = ('root', 'data', 'tool_copy', 'fake_bin', 'control_file', 'env_file', 'calls_file',
    'docker_calls_file', 'fake_host', 'candidate_control', 'candidate_calls_file', 'gateway_file',
    'admin_token', 'source', 'activation_fact', 'mutations_file', 'failures_file', 'call_log',
    'ledger_dir', 'project_root', 'state_dir', 'store_root', 'campaign', 'tool_identity',
    'relay_control', 'mutations', 'relay_sequences', 'build', 'checker_path', 'fixed_checker',
    'defective_evaluator', 'active_candidate', 'active_revision', 'wrong_deployment_refusals',
    'execution_tree_calls', 'state_path', 'state_digest', 'workspace', '_locked',
    'failed_evaluation_run', 'assertion_failure_causes', 'inventory_path', 'containment_path',
    'dispositions_path')


def save_case(case: Any, name: str = 'case') -> None:
    values = {key: getattr(case, key) for key in CASE_FIELDS if key in case.__dict__}
    if 'host' in case.__dict__:
        host = case.host
        values['host_values'] = {'files_present': host.files_present, 'missing': list(host.missing),
            'global_state_digests': host.global_state_digests, 'calls': host.calls}
    save_state(name, values)


def restore_case(case: Any, name: str = 'case') -> bool:
    values = load_state(name)
    if values is None:
        return False
    values = dict(values)
    host_values = values.pop('host_values', None)
    for key, value in values.items():
        setattr(case, key, value)
    if host_values is not None:
        from tools.official_client_capture.tests.test_claude_upgrade_capture import FakeHost
        case.host = FakeHost(files_present=host_values['files_present'], missing=frozenset(host_values['missing']),
                             global_state_digests=host_values['global_state_digests'])
        case.host.calls = host_values['calls']
    return True


def active_seconds(name: str, seconds: float = 0.0) -> float:
    """累加真实执行段；操作者停机间隔不混入执行耗时，两账正式截止仍由原 API 核验。"""

    saved = load_state('time:' + name) or {'seconds': 0.0}
    total = float(saved['seconds']) + seconds
    if seconds:
        save_state('time:' + name, {'seconds': total})
    return total


def file_cut(path: Path, *, offset: int = 0, length: int | None = None) -> dict[str, Any]:
    """绑定原普通文件的准确区间与此前缀；允许后续追加，禁止改写旧字节。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f'故障链原件缺失或不是普通文件：{path}')
    raw = path.read_bytes()
    length = len(raw) - offset if length is None else length
    if offset < 0 or length < 0 or offset + length > len(raw):
        raise ValueError('故障链原件切点越界')
    return {'path': path, 'offset': offset, 'length': length,
        'sha256': hashlib.sha256(raw[offset:offset + length]).hexdigest(),
        'prefix_sha256': hashlib.sha256(raw[:offset + length]).hexdigest()}


def read_cut(binding: dict[str, Any]) -> bytes:
    current = file_cut(binding['path'], offset=binding['offset'], length=binding['length'])
    if current != binding:
        raise ValueError(f"故障链原件切点已改变：{binding['path']}")
    raw = binding['path'].read_bytes()
    return raw[binding['offset']:binding['offset'] + binding['length']]


def begin_command(argv: list[Any]) -> dict[str, Any] | None:
    """副作用前保存真实 argv（包括调用方已冻结的批次序号）；不授权重新派发。"""

    root = record_root()
    if root is None:
        return None
    directory = Path(tempfile.mkdtemp(prefix='command-', dir=root))
    path = directory / 'invocation.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'argv': [str(item) for item in argv]}, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    pending = {'directory': directory, 'invocation_binding': file_cut(path)}
    logical_name = _ACTIVE_COMMAND_STEP.get()
    if logical_name is not None:
        retry_field = None
        if isinstance(logical_name, tuple):
            logical_name, retry_field = logical_name
        original = load_state(logical_name)
        saved = original.get(retry_field) if retry_field and original is not None else original
        if (saved is None or saved.get('status') != 'pending'
                or saved.get('argv') != [str(item) for item in argv]
                or saved.get('invocation_binding') is not None):
            raise ValueError('原逻辑命令没有唯一的副作用前调用绑定，禁止派发')
        bound = {**saved, 'invocation_binding': pending['invocation_binding']}
        save_state(logical_name, {**original, retry_field: bound} if retry_field else bound)
    return pending


def record_command(argv: list[Any], code: int | None, stdout: str | bytes, stderr: str | bytes,
                   *, pending: dict[str, Any] | None = None) -> None:
    """原 run_main 原样保存 stdout/stderr、命令与退出码，摘要不代替输出原件。"""

    root = record_root()
    if root is None:
        return
    directory = pending['directory'] if pending is not None else Path(tempfile.mkdtemp(prefix='command-', dir=root))
    if pending is not None and json.loads(read_cut(pending['invocation_binding'])) != {'argv': [str(item) for item in argv]}:
        raise ValueError('真实命令与副作用前冻结的 argv 不一致')
    bindings = {}
    for label, text in (('stdout', stdout), ('stderr', stderr)):
        path = directory / (label + '.log')
        with path.open('xb') as stream:
            stream.write(text if isinstance(text, bytes) else text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        bindings[label] = file_cut(path)
    rows = load_state('command-observations') or {'commands': []}
    row = {'argv': [str(item) for item in argv], 'returncode': code, **bindings}
    if pending is not None:
        row['invocation_binding'] = pending['invocation_binding']
    descriptor = directory / 'command.json'
    with descriptor.open('x', encoding='utf-8') as stream:
        json.dump(_encoded(row), stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor.chmod(0o600)
    rows['commands'].append({**row, 'command_binding': file_cut(descriptor)})
    save_state('command-observations', rows)


def record_json_read(path: Path) -> None:
    """原读侧实读的 JSON 原件直接绑定，不改字节，也不扫描推导其它来源。"""

    if record_root() is None:
        return
    rows = load_state('command-observations') or {'commands': []}
    rows['commands'].append({'argv': ['原 JSON 读侧', str(path)], 'returncode': 0,
                            'stdout': file_cut(path), 'stderr': None})
    save_state('command-observations', rows)


def verify_commands(prefix: list[dict[str, Any]] | None = None) -> None:
    """原phase跳过前实核完整命令原件，并核冻结观察列表只追加。"""

    if record_root() is None:
        return
    rows = (load_state('command-observations') or {}).get('commands', [])
    if prefix is not None and rows[:len(prefix)] != prefix:
        raise ValueError('原phase的冻结命令观察前缀已改变')
    for row in rows:
        for field in ('stdout', 'stderr', 'command_binding', 'invocation_binding'):
            if row.get(field) is not None:
                read_cut(row[field])
        if row.get('command_binding') is not None:
            descriptor = _decoded(json.loads(read_cut(row['command_binding'])))
            if descriptor != {key: value for key, value in row.items() if key != 'command_binding'}:
                raise ValueError('原phase的命令描述与完整输出绑定不一致')


def _original_command(row: dict[str, Any], argv: list[str], *, allow_interrupted: bool = False) -> tuple[int | None, Any, str]:
    """实读原调用、完整输出与退出码；摘要值和后续新输出都不能替代原件。"""

    if row.get('command_binding') is None or row.get('invocation_binding') is None:
        raise ValueError('原逻辑命令缺副作用前调用或完整输出原件')
    invocation = json.loads(read_cut(row['invocation_binding']))
    descriptor = _decoded(json.loads(read_cut(row['command_binding'])))
    expected = {key: value for key, value in row.items() if key != 'command_binding'}
    if invocation != {'argv': argv} or descriptor != expected or row['argv'] != argv:
        raise ValueError('原逻辑命令的 argv、退出码或输出绑定不一致')
    if type(row['returncode']) is not int and not (allow_interrupted and row['returncode'] is None):
        raise ValueError('原逻辑命令尚未得到真实退出码，不能承接未决动作')
    stdout = read_cut(row['stdout']).decode('utf-8')
    stderr = read_cut(row['stderr']).decode('utf-8')
    try:
        result = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        # 原修复／回归子进程可能只输出文本；完整原件仍绑定，但没有JSON返回对象。
        result = None
    return row['returncode'], result, stderr


def command_step(case: Any, cursor_name: str, key: str, argv: list[Any], invoke: Any,
                 *, campaign: Path | None = None,
                 enclosing_commands: list[dict[str, Any]] | None = None,
                 preflight_retry_verifier: Any | None = None) -> tuple[int, Any, str]:
    """原逻辑命令唯一承接接缝；不调度阶段、不替调用方裁定未决副作用。"""

    if record_root() is None:
        return invoke()
    arguments = [str(item) for item in argv]
    name = 'command-step:' + cursor_name + ':' + key
    saved = load_state(name)
    verified_scope = _VERIFIED_READ_ONLY_WORLD.get()
    if verified_scope is not None and (
            verified_scope[0] is not case or verified_scope[1] != campaign
            or saved is None or saved.get('status') != 'complete'
            or ('preflight_retry' in saved and saved['preflight_retry'].get('status') != 'complete')):
        raise ValueError('已重验世界的只读作用域只允许承接同世界已完成命令，禁止新动作或补写未决命令')
    registry = load_state('command-step-keys')
    if registry is None:
        # 此检查仅拒绝清单被删除的旧世界，不从散落文件重建清单或推导命令成果。
        for path in record_root().glob('*.json'):
            if len(path.stem) != 64 or any(char not in '0123456789abcdef' for char in path.stem):
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('原命令登记清单缺失且记录路径不可信')
            envelope = json.loads(path.read_bytes())
            if envelope.get('payload', {}).get('name', '').startswith('command-step:'):
                raise ValueError('原命令登记清单已缺失，禁止将旧命令当作首次派发')
        registry = {'keys': {}}
    keys = registry.get('keys')
    if not isinstance(keys, dict):
        raise ValueError('原命令登记清单损坏')
    for registered_name, registered in keys.items():
        original = load_state(registered_name)
        if (not registered_name.startswith('command-step:') or original is None
                or registered != {'argv': original.get('argv'), 'campaign': original.get('campaign')}):
            raise ValueError(f'已登记原命令停点缺失或不一致：{registered_name}；禁止重新派发')
    expected_registration = {'argv': arguments, 'campaign': campaign}
    if saved is not None and keys.get(name) != expected_registration:
        raise ValueError(f'原命令 {key} 缺准确登记清单，禁止承接或追认')
    if saved is None and name in keys:
        raise ValueError(f'已登记原命令 {key} 的停点已缺失，禁止重新派发')
    if saved is not None:
        world_verified = False
        if saved['argv'] != arguments or saved['campaign'] != campaign:
            raise ValueError(f'原命令 {key} 的 argv／Campaign 已改变')
        if not os.environ.get(METHOD_RESUME_ENV):
            raise ValueError(f'原命令 {key} 只能经原方法恢复入口消费；有意重复动作须用不同 key')
        observations = (load_state('command-observations') or {}).get('commands', [])
        prefix = saved.get('observation_prefix')
        begin = saved['observation_begin']
        if prefix is None or begin != len(prefix) or observations[:begin] != prefix:
            raise ValueError(f'原命令 {key} 的副作用前观测列表前缀已改变或缺失')
        for previous in prefix:
            for field in ('stdout', 'stderr', 'command_binding', 'invocation_binding'):
                if previous.get(field) is not None:
                    read_cut(previous[field])
        if saved['status'] != 'complete':
            rows = observations[begin:]
            invocation = saved.get('invocation_binding')
            if invocation is not None:
                # 定位依据是副作用前冻结的那一次调用，不是相同 argv。
                # 只允许调用方明示的外层 namespace 在内层输出之后追加，逐份重验原件。
                matches = [index for index, row in enumerate(rows) if row.get('invocation_binding') == invocation]
                if matches != [0] or rows[0]['argv'] != arguments:
                    raise ValueError(f'原命令 {key} 的唯一调用与输出未闭合，禁止选取其它同 argv 输出')
                tail = rows[1:]
                if len({str(row.get('invocation_binding')) for row in tail}) != len(tail):
                    raise ValueError(f'原命令 {key} 之后出现重复外层观测，禁止追认')
                for row in tail:
                    if not enclosing_commands or row not in enclosing_commands:
                        raise ValueError(f'原命令 {key} 之后出现未绑定的额外观测，禁止追认')
                    # 外层超时只是原中断事实；内层仍必须有真实整数退出码与正式读侧闭合。
                    _original_command(row, row['argv'], allow_interrupted=True)
                rows = rows[:1]
            if len(rows) != 1 or rows[0]['argv'] != arguments:
                raise ValueError(f'原命令 {key} 已进入 pending（原观测切点 {begin}）；'
                    '须按原 COMMIT／Store／收据对账裁定，禁止盲重派或重新注入故障')
            # 只补完整日志已形成、旧前缀未改且真实世界读侧已闭合的那次命令。
            # 缺日志、未绑定额外观测或读侧未闭合都拒绝，不能从多个同argv里挑一个。
            result = _original_command(rows[0], arguments)
            if campaign is not None:
                verify_case(case, campaign)
                world_verified = True
            saved = {**saved, 'status': 'complete', 'command': rows[0]}
            save_state(name, saved)
        result = _original_command(saved['command'], arguments)
        if saved.get('invocation_binding') is not None and saved['command'].get('invocation_binding') != saved['invocation_binding']:
            raise ValueError(f'原命令 {key} 的完成输出未绑定副作用前唯一调用')
        if campaign is not None and not world_verified and verified_scope is None:
            verify_case(case, campaign)
        if 'preflight_retry' in saved:
            if (preflight_retry_verifier is None
                    or preflight_retry_verifier(cursor_name, arguments, campaign, saved, result)
                       != saved['preflight_retry'].get('proof')):
                raise ValueError('原预约前子尝试缺调用方显式重验证明')
            return _verified_preflight_retry(case, name, saved, invoke,
                arguments, campaign, enclosing_commands, start=False)
        # 共享接缝不认识客户端、场景或错误码；只有原调用点显式提供的只读证明可授权一次子尝试。
        # 已重验只读作用域仍消费旧失败，不调用授权器，也不创建新的许可或动作。
        if preflight_retry_verifier is not None and verified_scope is None:
            proof = preflight_retry_verifier(cursor_name, arguments, campaign, saved, result)
            if proof is not None:
                if observations[begin:begin + 1] != [saved['command']]:
                    raise ValueError('原前检失败不是原观测切点的唯一调用')
                for row in observations[begin + 1:]:
                    if not enclosing_commands or row not in enclosing_commands:
                        raise ValueError('原前检失败后有未绑定的额外观测，禁止重派')
                    _original_command(row, row['argv'], allow_interrupted=True)
                saved = {**saved, 'preflight_retry': {
                    'status': 'pending', 'argv': arguments, 'campaign': campaign,
                    'original_command': saved['command'], 'proof': proof,
                    'observation_begin': len(observations), 'observation_prefix': observations}}
                save_state(name, saved)
                return _verified_preflight_retry(case, name, saved, invoke,
                    arguments, campaign, enclosing_commands, start=True)
        return result
    prefix = (load_state('command-observations') or {}).get('commands', [])
    begin = len(prefix)
    pending = {'status': 'pending', 'argv': arguments, 'campaign': campaign, 'observation_begin': begin,
        'observation_prefix': prefix}
    save_case(case)
    if hasattr(case, 'runtime'):
        save_case(case.runtime, 'candidate-case')
    # 必须先登记唯一逻辑键，再发布pending，最后才进入原副作用调用。
    # 两次发布之间中断会具体拒绝，不补造停点，也不重派尚未裁定的动作。
    keys[name] = expected_registration
    save_state('command-step-keys', registry)
    save_state(name, pending)
    token = _ACTIVE_COMMAND_STEP.set(name)
    try:
        result = invoke()
    finally:
        _ACTIVE_COMMAND_STEP.reset(token)
    pending = load_state(name)
    rows = (load_state('command-observations') or {}).get('commands', [])[begin:]
    if len(rows) != 1 or rows[0]['argv'] != arguments:
        raise ValueError(f'原命令 {key} 未形成唯一真实 argv 的完整输出；保留 pending，禁止拼接摘要')
    if pending.get('invocation_binding') is not None and rows[0].get('invocation_binding') != pending['invocation_binding']:
        raise ValueError(f'原命令 {key} 的完整输出不属于副作用前绑定的唯一调用')
    original = _original_command(rows[0], arguments)
    if original != result:
        raise ValueError(f'原命令 {key} 的返回值与完整原 stdout／stderr 不一致')
    save_case(case)
    if hasattr(case, 'runtime'):
        save_case(case.runtime, 'candidate-case')
    save_state(name, {**pending, 'status': 'complete', 'command': rows[0]})
    return original


def checkpoint(case: Any, name: str, **values: Any) -> None:
    """原方法在明确动作边界保存局部值，动态宿主字段也在这个边界保存。"""

    case._fault_cursor_name = name
    previous = load_state('cursor:' + name) or {}
    if previous.get('audit_snapshots'):
        snapshots = {**previous['audit_snapshots'], **values.get('audit_snapshots', {})}
        # 已签出的原 before 不能被调用方携带的旧局部值倒退为首次许可。
        for scenario, status in previous['audit_snapshots'].items():
            if status == 'recorded':
                snapshots[scenario] = status
        values['audit_snapshots'] = snapshots
    values['command_prefix'] = (load_state('command-observations') or {}).get('commands', [])
    save_case(case)
    if hasattr(case, 'runtime'):
        save_case(case.runtime, 'candidate-case')
    save_state('cursor:' + name, values)


def _verified_preflight_retry(case: Any, name: str, saved: dict[str, Any], invoke: Any,
                              argv: list[str], campaign: Path | None,
                              enclosing_commands: list[dict[str, Any]] | None,
                              *, start: bool) -> tuple[int, Any, str]:
    """固定前检的一次子尝试；顶层失败、原 key、登记项与原输出永久保留。"""

    retry = saved['preflight_retry']
    proof = retry.get('proof', {})
    if (retry.get('argv') != argv or retry.get('campaign') != campaign
            or retry.get('status') not in {'pending', 'complete'}
            or retry.get('original_command') != saved.get('command')
            or proof.get('logical_name') != name or proof.get('campaign') != campaign
            or proof.get('original_command') != saved['command']
            or not isinstance(proof.get('bindings'), list) or not proof['bindings']):
        raise ValueError('原命令预约前子尝试缺准确原命令／世界证明')
    for binding in proof['bindings']:
        read_cut(binding)
    prefix, begin = retry.get('observation_prefix'), retry.get('observation_begin')
    observations = (load_state('command-observations') or {}).get('commands', [])
    if prefix is None or begin != len(prefix) or observations[:begin] != prefix:
        raise ValueError('原命令预约前子尝试的原观测前缀已改变')
    if retry['status'] == 'complete':
        if (observations[begin:begin + 1] != [retry['command']]
                or retry['command'].get('invocation_binding') != retry.get('invocation_binding')
                or retry.get('invocation_binding') is None):
            raise ValueError('原命令预约前子尝试完成输出没有唯一副作用前绑定')
        return _original_command(retry['command'], argv)
    if _VERIFIED_READ_ONLY_WORLD.get() is not None:
        raise ValueError('只读作用域禁止原命令预约前重试或补写')
    if start:
        if retry['status'] != 'pending' or retry.get('invocation_binding') is not None:
            raise ValueError('原命令预约前子尝试已开始，禁止第二次派发')
        token = _ACTIVE_COMMAND_STEP.set((name, 'preflight_retry'))
        try:
            outcome = invoke()
        finally:
            _ACTIVE_COMMAND_STEP.reset(token)
        saved = load_state(name)
        retry = saved['preflight_retry']
        observations = (load_state('command-observations') or {}).get('commands', [])
    rows = observations[begin:]
    invocation = retry.get('invocation_binding')
    if (invocation is None or not rows or rows[0].get('invocation_binding') != invocation
            or rows[0]['argv'] != argv
            or [index for index, row in enumerate(rows) if row.get('invocation_binding') == invocation] != [0]):
        raise ValueError('原命令预约前子尝试已进入 pending；缺唯一完整输出，禁止盲重派')
    for row in rows[1:]:
        if start or not enclosing_commands or row not in enclosing_commands:
            raise ValueError('原命令预约前子尝试后有未绑定的额外观测，禁止追认')
        _original_command(row, row['argv'], allow_interrupted=True)
    result = _original_command(rows[0], argv)
    if start and result != outcome:
        raise ValueError('原命令预约前子尝试返回值与完整原件不一致')
    if campaign is not None:
        verify_case(case, campaign)
    save_case(case)
    if hasattr(case, 'runtime'):
        save_case(case.runtime, 'candidate-case')
    save_state(name, {**saved, 'preflight_retry': {**retry, 'status': 'complete', 'command': rows[0]}})
    return result


def verify_case(case: Any, campaign: Path) -> None:
    """坐标绑定后用原产品读侧重验实际世界，JSON 不是恢复授权或通过证明。"""

    from tools.official_client_control.store import ControlStore
    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import codex_upgrade_project_ledger as project
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing
    source_scope = (case.source_context if hasattr(case, 'source_context') else
                    case.chain_patched if hasattr(case, 'chain_patched') else contextlib.nullcontext)
    with source_scope(), contextlib.ExitStack() as stack:
        typed_name = 'typed-vc6:' + str(campaign)
        saved_typed = load_state(typed_name)
        if saved_typed is not None and os.environ.get(METHOD_RESUME_ENV):
            # 新进程在 Store 重放之前先用原 control/config 和完整活身份
            # 重绑已形成的回归来源；不启动守卫，也不修改正式空登记表。
            from tools.official_client_capture.tests import claude_typed_vc6_fixture as typed
            original = typed.TypedDeliveryFixture(campaign, saved_typed['candidate_id'],
                                                 control_path=saved_typed['control_path'])
            original._bind_live_guard()
            stack.enter_context(typed.source_scope(original.control_path))
        manifest = upgrade.load_campaign_manifest(campaign)
        store = ControlStore.open_read_only(store_module.campaign_store_root(campaign))
        store_campaign = store.load_campaign(manifest['campaign_id'])
        store_module.require_store_campaign_matches_plan(manifest, store_campaign)
        store.replay(external_root=capture._host_data_root(campaign), require_external=True)
        vc.validate_vc_control(campaign, manifest)
        if hasattr(case, 'project_root'):
            project.read_project_history_snapshot(case.project_root)
        if hasattr(case, 'ledger_dir'):
            timing.inspect_ledger(case.ledger_dir, project_ledger_optional=True)


@contextlib.contextmanager
def verified_read_only_world(case: Any, campaign: Path):
    """同一原方法持锁后，先完整重验一次世界，再逐条只读核原命令；离开即失效。"""

    verify_case(case, campaign)
    token = _VERIFIED_READ_ONLY_WORLD.set((case, campaign))
    try:
        yield
    finally:
        _VERIFIED_READ_ONLY_WORLD.reset(token)


def resume_checkpoint(case: Any, name: str) -> dict[str, Any] | None:
    """只恢复确有原游标的本方法；不从最后一个结果猜停点，不自动重建。"""

    case._fault_cursor_name = name
    saved = load_state('cursor:' + name)
    if saved is None:
        return None
    if 'command_prefix' not in saved:
        raise ValueError('原phase缺冻结命令观察前缀，不能承接旧游标摘要')
    verify_commands(saved['command_prefix'])
    if not restore_case(case):
        raise ValueError('原方法游标缺少夹具值字段')
    campaign = saved.get('campaign')
    if campaign is not None:
        verify_case(case, campaign)
    return saved


# 原世界中的值逐项明示；运行对象在恢复时经原构造器／reader 重新绑定。
WORLD_FIELDS = ('campaign', 'candidate_id', 'revision', 'evaluation', 'config_path', 'gate_root',
    'expected_client_requests', 'real_candidate', 'next_sequence', 'records', 'ledger_dir',
    'project_root', 'state_dir', 'profile_path', 'profile_sha256', 'rules_path', 'trusted_roots',
    'last_builder_sequence', 'acceptance', 'accepted_batch', 'original_attempt_root',
    'original_attempt', 'attempt_id', 'r1_invalidation', 'evaluation_recovery', 'recovery_revision',
    'recovery_root', 'recovery_summary', 'baseline_recovery', 'old_boundary', 'relay_control',
    'wrong_deployment_refusals', 'evaluation_index', 'capture_calls', 'precise_total')
WORLD_REBUILT_FIELDS = ('manifest', 'store', 'store_campaign', 'build', 'candidate_payload',
    'architecture', 'stage', 'attempt_root', 'attempt', 'candidate_revision', 'evaluation_baseline',
    'official', 'typed_delivery', 'comparison')


def save_world(name: str, world: dict[str, Any]) -> None:
    """只记显式原坐标与局部值；不序列化 Store、守卫或执行器对象。"""

    if record_root() is None:
        return
    unknown = set(world) - set(WORLD_FIELDS) - set(WORLD_REBUILT_FIELDS)
    if unknown:
        raise ValueError(f'故障链世界有未声明的恢复字段：{sorted(unknown)}')
    values = {key: world[key] for key in WORLD_FIELDS if key in world}
    side = world.get('official')
    if side is not None:
        values['official_values'] = {key: getattr(side, key)
            for key in ('evidence_root', 'manifest_path', 'capture_manifest')}
    bindings = [file_cut(world[key]) for key in ('config_path', 'profile_path', 'rules_path') if key in world]
    save_state('world:' + name, {'values': values, 'bindings': bindings})


def restore_world(case: Any, name: str) -> dict[str, Any]:
    """重验原 Store／VC／两账，再用原读侧重建当前 world；不创建任何 Campaign。"""

    saved = load_state('world:' + name)
    if saved is None or not os.environ.get(METHOD_RESUME_ENV):
        raise ValueError('原世界没有明示停点或未从原方法恢复入口进入')
    for binding in saved['bindings']:
        read_cut(binding)
    world = dict(saved['values'])
    side = world.pop('official_values', None)
    if side is not None:
        world['official'] = evaluator.SideEvidence(**side)
    verify_case(case, world['campaign'])
    refresh_world(case, world)
    # 不从最大序号猜下一动作；原 cursor 中保留的 sequence 不变。
    return world


def freeze_control_profile(campaign: Path) -> None:
    """在首次评估前冻结一次控制面夹具画像，之后不改批准清单或画像字节。"""

    with sources.profile_seam():
        profile.freeze_approved_assertion_profile(campaign, upgrade.load_campaign_manifest(campaign))


def refresh_world(case: Any, world: dict[str, Any], *, candidate_id: str | None = None, revision: int | None = None) -> dict[str, Any]:
    """基线或 revision 变化后重新读当前真实封存结果，禁止继续持有旧阶段或旧构建的快照。"""

    campaign = world['campaign']
    candidate = candidate_id or world['candidate_id']
    revision = int(revision if revision is not None else world['revision'])
    manifest = upgrade.load_campaign_manifest(campaign)
    store, store_campaign = store_module.open_campaign_store(campaign, manifest)
    frozen = next(fact for fact in reversed(store.list_facts(store_campaign['campaign_id']))
                  if fact['fact_kind'] == 'candidate_frozen' and fact['payload']['candidate_id'] == candidate)
    build, _binding = compare.load_candidate_build_receipt(campaign, manifest, candidate, revision)
    with case.source_context():
        baseline, _commit = upgrade._current_evaluation_baseline(campaign, candidate)
        stage = upgrade._load_stage_result(campaign, 'capture-candidate', candidate, _baseline=baseline)
        attempt_root, attempt = upgrade._capture_stage_attempt_context(campaign, stage, phase='candidate', candidate_id=candidate)
    world.update(manifest=manifest, candidate_id=candidate, revision=revision, candidate_revision=revision,
                 evaluation_baseline=baseline, store=store, store_campaign=store_campaign, build=build,
                 candidate_payload=dict(frozen['payload']), architecture=str(frozen['payload']['target_architecture']),
                 stage=stage, attempt_root=attempt_root, attempt=attempt)
    # 原始字节可能在受管数据根，不能只快照 Campaign 内的清单与收据。
    roots = [campaign / 'official', campaign / 'candidates']
    roots.extend(Path(value) for row in attempt['results'] for value in row['evidence_roots'])
    official = world.get('official')
    if official is not None:
        roots.extend([official.evidence_root, official.manifest_path])
    world['trusted_roots'] = list(dict.fromkeys(roots))
    return world


def real_candidate_world(case: Any, campaign: Path, *, candidate_id: str, revision: int, expected_client_requests: int,
                         capture_layout: dict[str, Any] | None = None) -> dict[str, Any]:
    """共享世界只绑定真实候选；配置中不允许出现替换 attempt 上下文的两个接缝字段。"""

    evaluation = campaign / 'continuous-sources'
    official_side = evaluator.write_side(evaluation / 'official', capture_id='continuous-official')
    official = upgrade._load_stage_result(campaign, 'capture-official')
    source_path = sources._write_json(evaluation / 'official-source.json', sources.official_source(official_side, official['store']['evidence_package_ref']))
    source_config: dict[str, Any] = {'official_source': str(source_path)}
    if capture_layout is not None:
        # 可选布局只登记测试运行源的真实映射，与首次来源配置一起写出；不改 Campaign 或候选证据。
        source_config['capture_layout'] = dict(capture_layout)
    config_path = sources._write_json(evaluation / 'source-config.json', source_config)
    world = {'campaign': campaign, 'candidate_id': candidate_id, 'revision': revision, 'evaluation': evaluation,
             'official': official_side, 'config_path': config_path, 'gate_root': case.root / 'gate-evidence',
             'expected_client_requests': expected_client_requests, 'real_candidate': True, 'next_sequence': 9,
             'records': [], 'ledger_dir': case.ledger_dir, 'project_root': case.project_root, 'state_dir': case.state_dir}
    refresh_world(case, world)
    classification = upgrade._load_stage_result(campaign, 'classify')
    view = upgrade._effective_classification_view(campaign, candidate_id, classification)
    profile_path, _profile, binding = compare._effective_profile(campaign, view)
    rules_path = compare._bound_campaign_file(campaign, classification['approved']['target_rules'], '批准目标规则')
    world.update(profile_path=profile_path, profile_sha256=binding['sha256'], rules_path=rules_path)
    return world


def prepare_delivery_sources(case: Any, context: dict[str, Any]) -> dict[str, Any]:
    """七／八场景连续链共用实读来源，在第一次 compare 前物化新原件，禁止改写已封存收据。

    历史请求仍来自明示的评估器夹具；门禁环境由原 finalizer 生成，供独立 reader 消费。
    不替换候选 attempt、Store 包或已有来源配置，两条链共享同一初始化规则。
    """

    campaign = context['campaign']
    original_config = json.loads(context['config_path'].read_bytes())
    case.assertNotIn('attempt', original_config, '候选上下文必须由正式封存读侧读取')
    case.assertNotIn('attempt_root', original_config, '不能用来源配置替换候选 attempt')
    official = upgrade._load_stage_result(campaign, 'capture-official')
    source = sources.materialize_official_source(campaign=campaign, data_root=case.data, side=context['official'],
        package_ref=official['store']['evidence_package_ref'])
    source_path = sources._write_json(context['evaluation'] / 'materialized-official-source.json', source)
    configuration = {**original_config, 'official_source': str(source_path),
                     'gate_environment_mode': sources.REAL_GATE_ENVIRONMENT}
    context['config_path'] = sources._write_json(context['evaluation'] / 'materialized-source-config.json', configuration)
    return context


def take_sequence(world: dict[str, Any], requested: int | None = None) -> int:
    """每次提交用同世界递增序号；故障重派仍由调用方显式保留原序号。"""

    sequence = max(int(world['next_sequence']), int(requested or 0))
    world['next_sequence'] = sequence + 1
    return sequence


def strict_accept(case: Any, world: dict[str, Any]) -> dict[str, Any]:
    """真实门禁收据绑定当前候选，严格 accept 写完 VC-5；不打桩规则、摘要或成功状态。"""

    refresh_world(case, world)
    campaign = world['campaign']
    name = f"continuous-r{world['revision']}-b{world['evaluation_baseline']}.json"
    if record_root() is None:
        receipt = case.gate_receipt(world, name)
        sequence = take_sequence(world)
    else:
        from tools.official_client_capture import codex_upgrade_gate_receipt as gate

        cursor = getattr(case, '_fault_cursor_name', None)
        if cursor is None:
            raise ValueError('strict_accept 缺原方法游标，不能登记孤立停点')
        key = f"strict-accept:{cursor}:{world['candidate_id']}:{world['revision']}:{world['evaluation_baseline']}"
        with sources.seams(world['config_path']):
            identity = compare.candidate_gate_identity(campaign, world['manifest'], world['candidate_id'], world['build'])
            subject = sources.expected_subject(world['manifest'], world['candidate_payload'], identity)
        expected = {'campaign': campaign, 'candidate_id': world['candidate_id'], 'revision': world['revision'],
                    'evaluation_baseline': world['evaluation_baseline'], 'gate_root': world['gate_root'],
                    'receipt': name, 'subject': subject}
        saved = load_state(key)
        if saved is None:
            sequence = take_sequence(world)
            saved = {**expected, 'sequence': sequence, 'next_sequence': world['next_sequence'], 'status': 'gate-pending'}
            # 收据生成前冻结原候选、门禁坐标和序号；同方法恢复不再次取序号。
            save_state(key, saved)
            save_world(key, world)
            receipt = case.gate_receipt(world, name)
            if receipt != name:
                raise ValueError('strict_accept 原门禁收据坐标改变')
        else:
            if not os.environ.get(METHOD_RESUME_ENV) or any(saved[field] != value for field, value in expected.items()):
                raise ValueError('strict_accept 原候选、门禁或评估基线已改变')
            sequence, receipt = saved['sequence'], saved['receipt']
            world['next_sequence'] = saved['next_sequence']
            verify_case(case, campaign)
            if saved['status'] == 'gate-pending' and not (world['gate_root'] / receipt).is_file():
                raise ValueError('strict_accept 原门禁生成未闭合；保留原现场，禁止覆盖或重建门禁原件')
        original_gate = gate.replay(world['gate_root'], receipt)
        case.assertEqual({field: original_gate['subject'][field] for field in subject}, subject,
                         '原门禁收据不属于冻结的候选与评估基线')
        if saved['status'] == 'gate-pending':
            # 只承接原 finalizer 已完整签出且原 reader 可重放的窄窗口。
            saved = {**saved, 'status': 'gate-ready', 'receipt_binding': file_cut(world['gate_root'] / receipt)}
            save_state(key, saved)
        read_cut(saved['receipt_binding'])
    action = case.accept_action(campaign, receipt, world['gate_root'], candidate_id=world['candidate_id'])
    argv = ['compile-and-run-vc-batch', '--campaign-dir', campaign, '--phase', 'VC-5', '--sequence', sequence,
            '--predecessor-checkpoint', vc.vc_checkpoint_path(campaign, 'VC-4', revision=world['revision']),
            '--action-plan', case.root / 'action-plans' / f'vc-5-{sequence:04d}.json', '--state-dir', case.state_dir]
    outcome = (case.dispatch_vc5(world, sequence, action) if record_root() is None else
               command_step(case, key, 'accept', argv, lambda: case.dispatch_vc5(world, sequence, action), campaign=campaign))
    result = case.assert_batch_passed(outcome, sequence, 'VC-5')
    case.assertTrue(vc.vc_checkpoint_path(campaign, 'VC-5', revision=world['revision']).is_file())
    with sources.seams(world['config_path']):
        accepted = upgrade._load_stage_result(campaign, 'accept', world['candidate_id'])
    comparison = upgrade._load_stage_result(campaign, 'compare', world['candidate_id'])
    case.assertEqual(comparison['rules'], list(upgrade._approved_rules(campaign, world['manifest'], require_approved=True)))
    case.assertEqual((comparison['candidate_revision'], comparison['evaluation_baseline']), (world['revision'], world['evaluation_baseline']))
    world['acceptance'] = accepted
    world['accepted_batch'] = result
    if record_root() is not None:
        save_state(key, {**saved, 'status': 'complete'})
        save_world(key, world)
    return accepted
