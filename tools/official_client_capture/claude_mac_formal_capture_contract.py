"""正式独立捕获的固定安装、原预算镜像及原件重放合同。

正式身份来自固定受管安装及实际运行代码闭包，不从上传JSON或旧夹具对象
推导。构造及安装读取只读；原预算验证不新增总账，也不把镜像当新的租约。
"""
from __future__ import annotations
import copy
import hashlib
import hmac
import ipaddress
import os
import platform
import stat
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_h2_contract as h2
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture.claude_mac_capture_dependency_core import FrozenDependencyBytes
from tools.official_client_capture.claude_mac_capture_h2_core import RawH2Parser, LIMIT_FIELDS
from tools.official_client_capture.claude_mac_capture_runtime_core import collect_runtime_facts

INSTALL_ROOT = Path('/opt/sub2apiplus/claude-client-capture')
INSTALL_SCHEMA = 'claude-upgrade-formal-capture-installation/v1'
CONFIG_SCHEMA = 'claude-upgrade-formal-capture-configuration/v1'
NATIVE_SCHEMA = 'claude-upgrade-formal-native-capture/v1'
INVENTORY_SCHEMA = 'claude-upgrade-formal-capture-export/v1'
ATTEST_SCHEMA = 'claude-upgrade-formal-capture-origin/v1'
DISPATCH_SCHEMA = 'claude-upgrade-capture-dispatch/v1'
RECORDER_ID = 'claude-mac-managed-products-v2'
SCOPE = 'formal'
timestamp = source.c.timestamp
_TOKEN = object()
_DISPATCH_TOKEN = object()
_DISPATCH_HANDLES = weakref.WeakSet()
_CODE_NAMES = (
    'claude_mac_formal_capture_contract.py', 'claude_mac_formal_capture_service.py',
    'claude_mac_agent_bridge.py', 'claude_mac_capture_engine_core.py',
    'claude_mac_capture_dependency_core.py', 'claude_mac_capture_h2_core.py',
    'claude_mac_capture_runtime_core.py', 'claude_mac_capture_h2_contract.py',
    'claude_mac_capture_h2.py', 'claude_mac_capture_h2_dependencies.py',
    'claude_mac_capture_h2_runtime.py', 'claude_mac_capture_fixture_policy.py',
    'claude_mac_capture_contract.py', 'claude_mac_capture_wire.py',
    'claude_client_receipt_contract.py', 'claude_mac_client_source.py',
    'claude_upgrade_mac_budget.py', 'codex_upgrade_project_ledger.py',
    'claude_upgrade_delivery_recovery_guard.py', 'upstream_byte_relay.py')
CONFIG_FIELDS = h2.CONFIG_FIELDS | {'installation_sha256'}
NATIVE_FIELDS = h2.NATIVE_FIELDS - {'formal_ready', 'launch_allowed'} | {'installation_sha256'}


class FormalCaptureError(c.CaptureError):
    """正式捕获身份、预算、来源或原件不符。"""


class FormalCaptureUnavailable(FormalCaptureError):
    """受管安装或现场资格缺失，必须在写文件、预算占用和监听前拒绝。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise FormalCaptureError(message)


def trusted_bytes(path: Path, *, maximum: int = 67_108_864) -> bytes:
    """实读固定受管原件并核路径实例；普通用户可读但不可写的材料可用。"""
    path = c.plain_path(Path(path))
    for directory in path.parents:
        info = directory.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                 and not stat.S_IMODE(info.st_mode) & 0o022, '受管来源祖先目录可由普通用户修改')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                 and not stat.S_IMODE(before.st_mode) & 0o022 and before.st_size <= maximum,
                 '受管来源不是root拥有的有界单链接普通文件')
        blocks, size = [], 0
        while raw := os.read(fd, 65536):
            blocks.append(raw); size += len(raw)
            _require(size <= maximum, '受管来源读取超限')
        after, current = os.fstat(fd), path.lstat()
        names = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_nlink')
        _require(all(getattr(before, name) == getattr(after, name) == getattr(current, name)
                     for name in names), '受管来源读取时文件实例或字节发生变化')
        return b''.join(blocks)
    finally:
        os.close(fd)


def trusted_binding(root: Path, reference: Mapping[str, Any]) -> bytes:
    c.exact(reference, {'path', 'sha256', 'bytes'}, '受管原件绑定')
    raw = trusted_bytes(c.managed_path(root, reference['path']))
    _require(len(raw) == reference['bytes'] and hashlib.sha256(raw).hexdigest() == reference['sha256'],
             '受管原件摘要或长度漂移')
    return raw


def runtime_bindings(configuration: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    root = Path(__file__).parent
    return [{'file': name, 'sha256': hashlib.sha256((root/name).read_bytes()).hexdigest(),
             'bytes': (root/name).stat().st_size} for name in _CODE_NAMES]


def _formal_task(task: Mapping[str, Any]) -> dict[str, Any]:
    value = source.validate_task_v2(task)
    _require(value['execution']['source_scope'] == 'formal'
             and value['execution']['recorder_id'] == RECORDER_ID
             and value['client_id'] in {'claude-desktop', 'claude-vscode'}, '正式捕获只接完整原Mac产品任务')
    return value


@dataclass(frozen=True)
class CaptureInstallation:
    """只读固定安装句柄；每次实际操作前重读原件，不相信外部旗标。"""
    _value_bytes: bytes
    sha256: str
    _token: object

    @property
    def value(self) -> dict[str, Any]:
        """向调用者返回副本，不能通过嵌套容器改掉已验安装。"""
        return c.parse_json(self._value_bytes)

    def revalidate(self, task: Mapping[str, Any]) -> dict[str, Any]:
        _require(type(self) is CaptureInstallation and self._token is _TOKEN, '正式安装句柄不是固定读取器构造')
        actual = _read_capture_installation(task)
        _require(actual.sha256 == self.sha256 and actual.value == self.value, '原正式捕获安装发生变化')
        return copy.deepcopy(dict(self.value))

    def configuration(self, task: Mapping[str, Any], attempt_root: Path) -> dict[str, Any]:
        """只派生原任务配置，不创建材料目录或启动后台进程。"""
        task = _formal_task(task)
        install = self.revalidate(task)
        original = install['configuration']
        root = c.plain_path(Path(original['data_root']))
        attempt = c.plain_path(Path(attempt_root))
        task_path = attempt/'client-evidence'/task['client_id']/'tasks'/task['task_nonce']/'task.json'
        _require(source.validate_task_v2(c.read_json(task_path)) == task, '正式工厂未绑定受管位置的完整原task')
        capture = c.plain_path(Path(task['execution']['capture_root']))
        _require(attempt.is_relative_to(root) and capture.is_relative_to(root)
                 and not capture.is_relative_to(attempt/'client-evidence'/task['client_id']/'runs'),
                 '独立捕获根与任务不在固定数据根，或落入Mac上传包')
        prefix = f"runtime/formal-capture/{task['task_sha256']}/materials"
        def mapped(reference):
            return {**reference, 'path': prefix+'/'+reference['path']}
        listener, upstream = copy.deepcopy(original['listener']), copy.deepcopy(original['upstream'])
        for endpoint in (listener, upstream):
            if endpoint['tls']:
                endpoint['tls'] = {name: mapped(ref) if isinstance(ref, dict) else ref
                                   for name, ref in endpoint['tls'].items()}
        value = {'schema_version': CONFIG_SCHEMA, 'source_scope': 'formal', 'data_root': str(root),
                 'capture_relative': capture.relative_to(root).as_posix(),
                 'task_binding': c.binding(root, task_path), 'deployment': task['execution']['source_deployments']['capture'],
                 'control_socket_relative': f"runtime/formal-capture/{task['task_sha256']}/control/capture.sock",
                 'channel_key_binding': mapped(original['channel_key_binding']), 'listener': listener,
                 'upstream': upstream, 'limits': copy.deepcopy(original['limits']),
                 'protocol_policy': copy.deepcopy(original['protocol_policy']),
                 'h2_dependencies': {'root_relative': prefix+'/'+original['h2_dependencies']['root_relative'],
                                     'manifest': copy.deepcopy(original['h2_dependencies']['manifest'])},
                 'runtime_environment_binding': mapped(original['runtime_environment_binding']),
                 'installation_sha256': self.sha256}
        endpoint = urlsplit(task['execution']['endpoint'])
        _require(endpoint.scheme == ('https' if listener['tls'] else 'http')
                 and endpoint.hostname == listener['host'] and endpoint.port == listener['port']
                 and endpoint.path in {'', '/'} and not endpoint.query and not endpoint.fragment,
                 '正式捕获监听不是原候选endpoint，不能偷偷替换task或另走路由')
        return value


def _read_capture_installation(task: Mapping[str, Any]) -> CaptureInstallation:
    task = _formal_task(task)
    try:
        raw = trusted_bytes(INSTALL_ROOT/'installation.json', maximum=262144)
    except (OSError, ValueError) as error:
        raise FormalCaptureUnavailable('独立ARM64捕获尚无可信固定安装，零副作用拒绝') from error
    value = c.exact(c.parse_json(raw), {'schema_version', 'deployment', 'mac_deployment', 'code_root',
                    'runtime_code', 'qualification', 'configuration'}, '正式捕获安装')
    _require(value['schema_version'] == INSTALL_SCHEMA
             and value['deployment'] == task['execution']['source_deployments']['capture']
             and value['mac_deployment'] == task['execution']['source_deployments']['mac'],
             '正式安装未绑定任务双方部署')
    actual_root = c.plain_path(Path(__file__).parent)
    expected_root = INSTALL_ROOT/'code'/'tools'/'official_client_capture'
    deployed_root = Path(value['configuration']['data_root'])/'tools'/'official_client_capture'
    _require(value['code_root'] == str(expected_root) and actual_root in {expected_root, deployed_root},
             '正式捕获只接受固定安装或冻结数据根的部署副本，开发检出没有来源权')
    _require(value['runtime_code'] == runtime_bindings(), '实际捕获代码闭包与冻结安装身份不符')
    for reference in value['runtime_code']:
        raw_code = trusted_bytes(expected_root/reference['file'])
        _require(len(raw_code) == reference['bytes'] and hashlib.sha256(raw_code).hexdigest() == reference['sha256'],
                 '正式捕获实际代码原件漂移')
    qualification = c.exact(value['qualification'], {'capture_probe', 'mac_probe', 'reviewed_source'}, '捕获资格原件')
    for name, reference in qualification.items():
        evidence = c.parse_json(trusted_binding(INSTALL_ROOT, reference))
        _require(evidence.get('schema_version') == 'claude-upgrade-source-qualification/v1'
                 and evidence.get('source_scope') == 'formal' and evidence.get('status') == 'passed'
                 and evidence.get('capture_deployment') == value['deployment']
                 and evidence.get('mac_deployment') == value['mac_deployment']
                 and evidence.get('kind') == name, '正式安装缺准确来源资格原件')
    original = c.exact(value['configuration'], {'data_root', 'listener', 'upstream', 'limits', 'protocol_policy',
                    'h2_dependencies', 'runtime_environment_binding', 'channel_key_binding'}, '受管捕获配置')
    c.plain_path(Path(original['data_root']))
    for endpoint in (original['listener'], original['upstream']):
        _require(isinstance(endpoint, dict) and set(endpoint) == {'host', 'port', 'tls'}, '受管端点合同非法')
        _require(isinstance(endpoint['host'], str), '受管端点必须冻结IP')
        try:
            ipaddress.ip_address(endpoint['host'])
        except ValueError as error:
            raise FormalCaptureError('受管端点必须是固定IP，不能隐式DNS改路由') from error
        _require(type(endpoint['port']) is int and 0 < endpoint['port'] <= 65535, '受管端口未冻结')
        if endpoint['tls']:
            for ref in endpoint['tls'].values():
                if isinstance(ref, dict):
                    trusted_binding(INSTALL_ROOT, ref)
    key = trusted_binding(INSTALL_ROOT, original['channel_key_binding'])
    _require(32 <= len(key) <= 128, '正式通道key长度非法')
    runtime = c.parse_json(trusted_binding(INSTALL_ROOT, original['runtime_environment_binding']))
    _require(runtime == collect_formal_runtime(), '正式解释器或实际OpenSSL不符冻结运行原件')
    _validate_protocol_and_limits(original)
    dependency = c.exact(original['h2_dependencies'], {'root_relative','manifest'}, '正式依赖根')
    _require(dependency['manifest']['path'] == 'production_decoder-dependencies.json', '正式依赖manifest须取固定成员名')
    FormalDecoderDependencies(INSTALL_ROOT/dependency['root_relative'], dependency['manifest'], artifact_only=True)
    return CaptureInstallation(c.canonical(value), hashlib.sha256(raw).hexdigest(), _TOKEN)


def require_formal_capture_installation(task: Mapping[str, Any]) -> CaptureInstallation:
    return _read_capture_installation(task)


def collect_formal_runtime() -> dict[str, Any]:
    return {'schema_version': 'claude-upgrade-formal-capture-runtime/v1', **collect_runtime_facts()}


def _validate_protocol_and_limits(configuration: Mapping[str, Any]) -> None:
    limits = c.exact(configuration['limits'], h2.COMMON_LIMITS | h2.EXTRA_LIMITS, '正式捕获冻结上界')
    _require(all(type(limits[name]) is int and limits[name] > 0 for name in
                 (h2.COMMON_LIMITS | h2.EXTRA_LIMITS)-{'drain_seconds', 'socket_timeout_seconds'}), '正式捕获大小上界非法')
    for name in ('drain_seconds', 'socket_timeout_seconds'):
        _require(type(limits[name]) in {int, float} and 0 < limits[name] <= 30, '正式捕获时限非法')
    _require(limits['max_connections'] <= 64 and limits['max_header_bytes'] <= 65536
             and limits['max_wire_bytes'] <= 67108864 and 16384 <= limits['max_frame_bytes'] <= 0xffffff
             and 4096 <= limits['max_hpack_table_bytes'] <= 0xffffffff, '正式捕获超过有界能力')
    policy = c.exact(configuration['protocol_policy'], {'mode', 'allow_no_alpn_h1'}, '正式协议策略')
    _require(policy['mode'] in {'cleartext_h1', 'tls_alpn'} and type(policy['allow_no_alpn_h1']) is bool,
             '正式捕获不能猜协议或自动回退')
    _require((configuration['listener']['tls'] is not None and configuration['upstream']['tls'] is not None)
             if policy['mode'] == 'tls_alpn' else
             configuration['listener']['tls'] is None and configuration['upstream']['tls'] is None,
             '正式两腿TLS形态不一致')


class FormalDecoderDependencies(FrozenDependencyBytes):
    """另一份正式解码字节合同；本对象本身不授产品启动权限。"""
    source_scope = 'formal'
    manifest_schema = 'claude-upgrade-formal-h2-private-dependencies/v1'
    receipt_schema = 'claude-upgrade-formal-h2-dependency-read-set/v1'


def validate_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    result = c.exact(dict(value), CONFIG_FIELDS, '正式capture配置')
    _require(result['schema_version'] == CONFIG_SCHEMA and result['source_scope'] == 'formal',
             '正式后端不能消费夹具配置')
    task = source.validate_task_v2(c.read_json(c.check_binding(Path(result['data_root']), result['task_binding'])))
    authority = require_formal_capture_installation(task)
    task_path = c.check_binding(Path(result['data_root']), result['task_binding'])
    attempt = task_path.parents[4]
    _require(result == authority.configuration(task, attempt), '正式配置不是固定安装和原任务的精确派生')
    _validate_protocol_and_limits(result)
    for endpoint in (result['listener'], result['upstream']):
        if endpoint['tls']:
            for reference in endpoint['tls'].values():
                if isinstance(reference, dict):
                    c.check_binding(Path(result['data_root']), reference)
    c.check_binding(Path(result['data_root']), result['channel_key_binding'])
    verify_protocol_runtime(result)
    dependency = result['h2_dependencies']
    FormalDecoderDependencies(c.managed_path(Path(result['data_root']), dependency['root_relative']),
                              dependency['manifest'], artifact_only=True)
    return result


def contract_versions(configuration: Mapping[str, Any]) -> dict[str, Any]:
    _require(configuration.get('schema_version') == CONFIG_SCHEMA, '未知正式捕获配置版本')
    return {'version': 2, 'configuration': CONFIG_SCHEMA, 'native': NATIVE_SCHEMA,
            'inventory': INVENTORY_SCHEMA, 'commit': 'claude-upgrade-formal-capture-commit/v1',
            'rpc': 'claude-upgrade-formal-capture-rpc/v1', 'sync': 'claude-upgrade-formal-capture-sync/v1'}


def verify_task_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    task = _formal_task(c.read_json(c.check_binding(Path(configuration['data_root']), configuration['task_binding'])))
    _require(c.managed_path(Path(configuration['data_root']), configuration['capture_relative'])
             == Path(task['execution']['capture_root']), '正式根与ARM64原任务不符')
    _require(configuration['deployment'] == task['execution']['source_deployments']['capture'], '正式捕获部署不符')
    return task


def verify_protocol_runtime(configuration: Mapping[str, Any]) -> None:
    actual = c.read_json(c.check_binding(Path(configuration['data_root']), configuration['runtime_environment_binding']))
    _require(actual == collect_formal_runtime() and actual['ssl']['has_alpn'] is True, '正式实际解释器或OpenSSL已漂移')


def prepare_protocol_snapshot(configuration: Mapping[str, Any], root: Path) -> FormalDecoderDependencies:
    dependency = configuration['h2_dependencies']
    original = c.managed_path(Path(configuration['data_root']), dependency['root_relative'])
    bundle = FormalDecoderDependencies(original, dependency['manifest'], artifact_only=True)
    target_root = c.private_directory(root/'h2-dependencies')
    for reference in bundle.read_set:
        target = c.managed_path(target_root, reference['path'])
        current = target_root
        for name in target.parent.relative_to(target_root).parts:
            current = c.private_directory(current/name)
        c.write_once(target, c.read_bytes(c.check_binding(original, reference)))
    return FormalDecoderDependencies(target_root, dependency['manifest'])


def create_parser(connection_id, protocol, configuration, dependencies, on_header=None):
    _require(type(dependencies) is FormalDecoderDependencies and protocol in {'h1', 'h2'}, '正式解析器未绑定正式依赖闭包')
    if protocol == 'h2':
        return RawH2Parser(connection_id, {name: configuration['limits'][name] for name in LIMIT_FIELDS},
                           on_header, dependencies=dependencies)
    from tools.official_client_capture.claude_mac_capture_wire import H1Parser
    return H1Parser(connection_id, {name: configuration['limits'][name] for name in h2.COMMON_LIMITS}, on_header)


def verify_budget(task: Mapping[str, Any], receipt: Mapping[str, Any], *, require_live: bool = True) -> None:
    """只读原项目账链，捕获方不再预约或结清；到期仍保留占用。"""
    task = _formal_task(task)
    from tools.official_client_capture import claude_upgrade_mac_budget as budget
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    root = Path(task['execution']['project_ledger'])
    plan, _raw = ledger._load_plan(root)
    _require(plan.get('fixture_only') is False, '正式capture拒绝fixture_only预算')
    row = budget.verify_bound_receipt(root, receipt, task_sha256=task['task_sha256'], dispatch=False)
    _require(row.get('status') == 'reserved', '正式预算已结清或不再活动')
    for name in ('campaign_id', 'candidate_id', 'attempt_id', 'run_nonce', 'candidate_revision'):
        _require(row['bound'][name] == task[name], '正式预算与原task坐标不符')
    for name in ('request_budget', 'producer_run_id', 'capture_id'):
        _require(row['bound'][name] == task['execution'][name], '正式预算与请求边界不符')
    deadline = task['execution']['request_policy']['deadline_utc']
    _require(row['bound']['deadline_utc'] == deadline, '正式预算未绑定原期限')
    if require_live:
        _require(source.c.timestamp(c.utc_now()) < source.c.timestamp(deadline), '正式派发预算期限已过')
    require_formal_capture_installation(task)


def verify_request(task: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    if str(row['path']).split('?', 1)[0] in task['execution']['request_policy']['model_paths']:
        _require(row['method'] in task['execution']['request_policy']['methods'], '实际模型方法不在冻结请求边界')


def native_extra(configuration: Mapping[str, Any]) -> dict[str, Any]:
    return {'installation_sha256': configuration['installation_sha256']}


def validate_inventory(value: Mapping[str, Any], *, task_sha256: str, expected_version=None) -> dict[str, Any]:
    result = c.exact(dict(value), {'schema_version', 'source_scope', 'task_sha256', 'capture_id', 'producer_run_id',
                    'configuration_sha256', 'deployment', 'native_manifest', 'files', 'export_id'}, '正式原件库存')
    _require(result['schema_version'] == INVENTORY_SCHEMA and result['source_scope'] == 'formal'
             and result['task_sha256'] == task_sha256 and expected_version in {None, 2}, '正式库存用途或任务不符')
    _require(result['export_id'] == c.digest({k:v for k,v in result.items() if k != 'export_id'}), '正式export ID不符')
    _require(isinstance(result['files'], list) and len(result['files']) <= 65536, '正式库存无界')
    names = []
    for ref in result['files']:
        c.exact(ref, {'path','sha256','bytes'}, '正式库存原件')
        c.managed_path(Path('/private/tmp'), ref['path'])
        _require(type(ref['bytes']) is int and ref['bytes'] >= 0 and isinstance(ref['sha256'], str)
                 and c.SHA_RE.fullmatch(ref['sha256']), '正式库存原件长度或摘要非法')
        names.append(ref['path'])
    # 固定生产者按路径组件排序，依赖目录和同前缀文件也须按原库存顺序校验。
    _require(names == sorted(set(names), key=lambda name: PurePosixPath(name).parts)
             and result['native_manifest'] in result['files']
             and result['native_manifest']['path'] == 'native-capture-manifest.json', '正式库存闭包重复或终态不符')
    return result


def _json_lines(root: Path, reference: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = c.read_bytes(c.check_binding(root, reference))
    _require(not raw or raw.endswith(b'\n'), '正式原件行裂尾')
    return [c.parse_json(line) for line in raw.splitlines()]


def replay_native(root: Path, native: Mapping[str, Any]) -> None:
    """复用原字节重放，不执行材料声明的路径，不取得产品来源权。"""
    configuration = c.read_json(c.check_binding(root, native['configuration']))
    c.exact(configuration, CONFIG_FIELDS, '正式配置快照')
    _require(configuration['schema_version'] == CONFIG_SCHEMA and configuration['source_scope'] == 'formal'
             and c.digest(configuration) == native['configuration_sha256']
             and configuration['installation_sha256'] == native['installation_sha256'], '正式配置快照漂移')
    _validate_protocol_and_limits(configuration)
    _require(c.read_json(c.check_binding(root, native['runtime_environment'])) == collect_formal_runtime()
             and native['runtime_bindings'] == runtime_bindings(), '正式实运行解释器或代码身份漂移')
    manifest = configuration['h2_dependencies']['manifest']
    _require(native['dependency_manifest'] == c.binding(root, root/'h2-dependencies'/manifest['path']), '正式依赖原件路径不符')
    dependencies = FormalDecoderDependencies(root/'h2-dependencies', manifest)
    receipt = c.read_json(c.check_binding(root, native['dependency_read_set']))
    c.exact(receipt, {'schema_version','source_scope','formal_ready','launch_allowed','kind','manifest','packages','read_set','loaded_origins'}, '正式实际依赖读集')
    _require(receipt['schema_version'] == dependencies.receipt_schema and receipt['source_scope'] == 'formal'
             and receipt['formal_ready'] is False and receipt['launch_allowed'] is False
             and receipt['kind'] == 'production_decoder' and receipt['manifest'] == manifest
             and receipt['read_set'] == dependencies.read_set
             and receipt['packages'] == [{"name":name,"version":row['version'],"wheel_sha256":row['wheel']['sha256']}
                    for name,row in sorted(dependencies.packages.items())], '正式实际依赖读集不符')
    origins = receipt['loaded_origins']
    _require(isinstance(origins,list) and len(origins)==len({row.get('module') for row in origins})
             and {row.get('module') for row in origins} >= {'hpack','hpack.hpack','hpack.table'}, '正式实际导入闭包缺失或重复')
    for origin in receipt['loaded_origins']:
        c.exact(origin, {'module','origin'}, '正式加载origin')
        _require(origin['module'] in dependencies.sources
                 and Path(origin['origin']).parts[-len(dependencies.sources[origin['module']][0].relative_to(dependencies.root).parts):]
                 == dependencies.sources[origin['module']][0].relative_to(dependencies.root).parts
                 and Path(origin['origin']).is_relative_to(Path(configuration['data_root'])/
                        configuration['capture_relative']/'h2-dependencies'), '正式实际origin不是原冻结成员')
    if native['ready_environment'] is not None:
        ready_environment = c.exact(c.read_json(c.check_binding(root,native['ready_environment'])),
                    {'configuration_sha256','runtime_environment','dependencies'}, '正式ready运行原件')
        ready_receipt=ready_environment['dependencies']
        _require(ready_environment['configuration_sha256']==native['configuration_sha256']
                 and ready_environment['runtime_environment']==native['runtime_environment']
                 and all(ready_receipt[name]==receipt[name] for name in receipt if name!='loaded_origins')
                 and all(row in receipt['loaded_origins'] for row in ready_receipt['loaded_origins']),
                 '正式ready未核准确同配置与依赖原件')
    events = h2.read_events(root, native['journal'])
    reservation = c.read_json(c.check_binding(root,native['reservation']))
    reserved_events = [row for row in events if row['event']=='reserved']
    _require(len(reserved_events)==1 and reserved_events[0]['data']=={'reservation':native['reservation']},
             '正式WAL预约未绑定原预算原件')
    if native['ready'] is not None:
        ready = c.read_json(c.check_binding(root,native['ready']))
        ready_events = [row for row in events if row['event']=='ready']
        _require(len(ready_events)==1 and ready_events[0]['data']=={'ready':native['ready'],'listener':{
            'address':configuration['listener']['host'],'port':configuration['listener']['port']}}
                 and native['ready_environment'] is not None
                 and all(ready[name]==native[name] for name in ('task_sha256','capture_id','reservation','deployment'))
                 and c.read_json(c.check_binding(root,ready['observation']))==
                    {name:value for name,value in ready.items() if name!='observation'},
                 '正式WAL ready未绑定实时监听原件')
    parsers, connections, sizes, tails = h2.replay_events(root, configuration, events, dependencies, parser_factory=create_parser)
    for key, expected in zip(('requests','outcomes','h2_frames','h2_header_blocks'), h2.parser_rows(parsers)):
        _require(_json_lines(root, native[key]) == expected, '正式'+key+'与原字节/WAL复算不符')
    _require(_json_lines(root, native['connections']) == list(connections.values()), '正式连接原件与WAL不符')
    _require(native['protocols']==sorted({row.get('protocol') for row in connections.values()}-{None}), '正式原生协议集合漂移')
    for row in connections.values():
        for name in ('protocol_observation','transport_observation','observation'):
            if row.get(name) is not None:
                c.check_binding(root,row[name])
    _require(events and events[-1]['event'] == 'sealing' and events[-1]['data'] == {'status':native['status']}, '正式收口缺同一WAL终态')
    if native['status'] == 'complete':
        _require(native['ready'] is not None and native['ready_environment'] is not None and not tails and not native['reasons']
                 and all(row.get('protocol') and row.get('transport_observation') and row.get('closed_at_utc')
                         and row.get('observation') for row in connections.values())
                 and all(parser is not None and not parser.errors and not parser.has_pending()
                         and all(row['status']=='complete' for row in parser.outcome_rows()) for parser in parsers.values()),
                 '正式缺失/错误/截断不能完整成功')


def verify_export(root: Path, inventory: Mapping[str, Any], *, task_sha256: str, expected_version=None) -> dict[str, Any]:
    import subprocess, sys
    inventory = validate_inventory(inventory, task_sha256=task_sha256, expected_version=expected_version)
    for ref in inventory['files']:
        c.check_binding(root, ref)
    native = c.exact(c.read_json(c.check_binding(root, inventory['native_manifest'])), NATIVE_FIELDS, '正式native终态')
    _require(native['schema_version'] == NATIVE_SCHEMA and native['source_scope'] == 'formal'
             and native['status'] in {'complete','failed','truncated'}, '正式native终态用途非法')
    for name in ('task_sha256','capture_id','producer_run_id','configuration_sha256','deployment'):
        _require(native[name] == inventory[name], '正式native与认证库存不符')
    references = [native[name] for name in ('reservation','connections','requests','outcomes','journal','configuration',
                    'runtime_environment','dependency_manifest','dependency_read_set','h2_frames','h2_header_blocks')]
    references.extend(native[name] for name in ('ready','ready_environment') if native[name] is not None)
    references.extend(native['raw_files'])
    _require(native['raw_files']==[row for row in inventory['files'] if row['path'].endswith('.bin')], '正式原字节清单未闭合认证库存')
    configuration=c.read_json(c.check_binding(root,native['configuration']))
    dependencies=FormalDecoderDependencies(root/'h2-dependencies',configuration['h2_dependencies']['manifest'],artifact_only=True)
    references.extend({**row,'path':'h2-dependencies/'+row['path']} for row in dependencies.read_set)
    if native['ready'] is not None:
        references.append(c.read_json(c.check_binding(root,native['ready']))['observation'])
    for row in _json_lines(root,native['connections']):
        references.extend(row[name] for name in ('protocol_observation','transport_observation','observation')
                          if row.get(name) is not None)
        if row.get('protocol_observation') is not None and configuration['protocol_policy']['mode']=='tls_alpn':
            observation=c.check_binding(root,row['protocol_observation'])
            references.append(c.binding(root,observation.parent/'tls-client-hello.bin'))
    for ref in references:
        _require(ref in inventory['files'], '正式必需原件未在认证导出闭包')
        c.check_binding(root, ref)
    # data/tools 的入口读者可读相同冻结字节；独立复算始终从固定安装启动。
    repository = INSTALL_ROOT/'code'
    result = subprocess.run([str(Path(sys.executable).resolve()), '-B','-S','-m',__name__,'--replay',str(root)],
                cwd=repository, env={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(repository)},
                capture_output=True, timeout=30, check=False)
    _require(result.returncode == 0, '正式native独立字节复算未通过：'+result.stderr.decode(errors='replace')[-1200:])
    return native


def _mac(key: bytes, value: Mapping[str, Any]) -> str:
    return hmac.new(key, c.canonical(value), hashlib.sha256).hexdigest()


@dataclass(frozen=True, eq=False)
class VerifiedCaptureDispatch:
    """仅固定认证桥能构造的原ready和预算镜像，不是Mac本地账本。"""
    _facts_bytes: bytes
    _token: object
    _binding_bytes: bytes | None = None

    def __post_init__(self):
        _require(self._token is _DISPATCH_TOKEN and type(self._facts_bytes) is bytes
                 and (self._binding_bytes is None or type(self._binding_bytes) is bytes),
                 '派发能力只能保存固定读取器产生的不可变认证字节')

    @property
    def facts(self) -> dict[str, Any]:
        """只读认证字节的副本；修改返回的嵌套字段不会改变能力。"""
        return c.parse_json(self._facts_bytes)

    @property
    def envelope_binding(self) -> dict[str, Any] | None:
        return None if self._binding_bytes is None else c.parse_json(self._binding_bytes)


def _verify_dispatch_envelope(task, value, key, *, require_live=True, bundle_root: Path | None = None):
    task = _formal_task(task)
    envelope = c.exact(dict(value), {'schema_version','task','task_sha256','task_nonce','capture_deployment',
                   'mac_deployment','deadline_utc','observed_at_utc','ready','reservation','budget_reservation','mac'}, '认证派发镜像')
    core = {name:item for name,item in envelope.items() if name != 'mac'}
    _require(isinstance(envelope['mac'], str) and hmac.compare_digest(envelope['mac'], _mac(key,core)), '派发镜像来源认证失败')
    _require(envelope['schema_version'] == DISPATCH_SCHEMA and envelope['task'] == task
             and envelope['task_sha256'] == task['task_sha256'] and envelope['task_nonce'] == task['task_nonce']
             and envelope['capture_deployment'] == task['execution']['source_deployments']['capture']
             and envelope['mac_deployment'] == task['execution']['source_deployments']['mac']
             and envelope['deadline_utc'] == task['execution']['request_policy']['deadline_utc'], '派发镜像未绑定完整原task及双方部署')
    observed = source.c.timestamp(envelope['observed_at_utc'])
    _require(source.c.timestamp(task['issued_at_utc']) <= observed < source.c.timestamp(envelope['deadline_utc']), '派发镜像形成时间非法')
    if require_live:
        _require(datetime.now(timezone.utc) < source.c.timestamp(envelope['deadline_utc'])
                 and 0 <= (datetime.now(timezone.utc)-observed).total_seconds() <= 15,
                 '派发镜像已过期或非本次实时复核')
    ready, reservation, receipt = envelope['ready'], envelope['reservation'], envelope['budget_reservation']
    from tools.official_client_capture import claude_upgrade_mac_budget as budget
    c.exact(ready, {'schema_version','task_sha256','capture_id','reservation','deployment','ready_at_utc','observation'},
            '镜像独立ready')
    c.exact(reservation, {'schema_version','task_sha256','candidate','capture_id','producer_run_id','endpoint',
                        'request_budget','reserved_at_utc','deadline_utc','budget_reservation','source_deployments'},
            '镜像原预约')
    c.exact(receipt, {'schema_version','project_ledger','bound','reservation_event','reserved_at_utc'}, '镜像原预算收据')
    bound=budget.validate_bound(receipt['bound'])
    _require(ready['schema_version']==source.READY_SCHEMA and reservation['schema_version']==source.RESERVATION_SCHEMA
             and receipt['schema_version']==budget.RECEIPT_SCHEMA and receipt['project_ledger']==task['execution']['project_ledger']
             and reservation['candidate']==task['candidate'] and reservation['source_deployments']==task['execution']['source_deployments']
             and reservation['endpoint']==task['execution']['endpoint']
             and reservation['request_budget']==task['execution']['request_budget']
             and reservation['deadline_utc']==envelope['deadline_utc']
             and all(bound[name]==task[name] for name in ('campaign_id','candidate_id','attempt_id','run_nonce','candidate_revision'))
             and all(bound[name]==task['execution'][name] for name in ('producer_run_id','capture_id')), '镜像原预约与完整预算坐标不符')
    _require(source.c.timestamp(task['issued_at_utc']) <= source.c.timestamp(receipt['reserved_at_utc'])
             <= source.c.timestamp(reservation['reserved_at_utc']) <= source.c.timestamp(ready['ready_at_utc']) <= observed,
             '镜像预约、ready与实时复核顺序不符')
    for ref,name in ((ready['reservation'],'reservation.json'),(ready['observation'],'ready-observation.json'),
                     (reservation['budget_reservation'],'budget-reservation.json')):
        c.exact(ref, {'path','sha256','bytes'}, '镜像准确原件绑定')
        _require(ref['path']==name and isinstance(ref['sha256'],str) and c.SHA_RE.fullmatch(ref['sha256'])
                 and type(ref['bytes']) is int and ref['bytes']>0, '镜像原件坐标或摘要非法')
    _require(ready['task_sha256'] == reservation['task_sha256'] == task['task_sha256']
             and ready['capture_id'] == reservation['capture_id'] == task['execution']['capture_id']
             and ready['deployment'] == envelope['capture_deployment']
             and reservation['producer_run_id'] == task['execution']['producer_run_id']
             and receipt['bound']['task_sha256'] == task['task_sha256']
             and receipt['bound']['request_budget'] == task['execution']['request_budget']
             and receipt['bound']['deadline_utc'] == envelope['deadline_utc'], '镜像ready/预约/原预算没有闭合')
    reference = None
    if bundle_root is not None:
        root=c.plain_path(Path(bundle_root))
        path=root/('capture-dispatch-'+c.digest(envelope)+'.json')
        c.write_json_once(path,envelope)
        reference=c.binding(root,path)
    handle=VerifiedCaptureDispatch(c.canonical(envelope), _DISPATCH_TOKEN,
                                   c.canonical(reference) if reference is not None else None)
    _DISPATCH_HANDLES.add(handle)
    return handle


def verify_capture_dispatch(task, dispatch, *, require_live=True):
    _require(type(dispatch) is VerifiedCaptureDispatch and dispatch._token is _DISPATCH_TOKEN
             and dispatch in _DISPATCH_HANDLES,
             '普通JSON或外部类型不能授认证派发镜像权限')
    value = dispatch.facts
    _require(value['task'] == _formal_task(task), '派发句柄不是完整原task')
    if require_live:
        now = datetime.now(timezone.utc)
        _require(now < source.c.timestamp(value['deadline_utc'])
                 and 0 <= (now-source.c.timestamp(value['observed_at_utc'])).total_seconds() <= 15, '原派发镜像期限已过')
    return {name:copy.deepcopy(value[name]) for name in ('task_sha256','ready','budget_reservation',
            'capture_deployment','mac_deployment','deadline_utc','observed_at_utc')}


def verify_formal_capture_origin(task, capture_root):
    """离线读側实读原件及固定安装认证，不依赖活着的Mac或capture进程。"""
    task = _formal_task(task); root = c.plain_path(Path(capture_root))
    _require(root == Path(task['execution']['capture_root']), '来源不是原ARM64冻结根')
    authority = require_formal_capture_installation(task)
    paths = [root/('capture-origin-'+phase+'.json') for phase in ('committed','ready','reserved')]
    path = next((candidate for candidate in paths if candidate.is_file()), paths[0])
    value = c.exact(c.read_json(path), {'schema_version','task_sha256','capture_id','producer_run_id',
                    'deployment','installation_sha256','phase','dispatch_state','originals','mac'}, '正式捕获来源原件')
    key = trusted_binding(INSTALL_ROOT, authority.value['configuration']['channel_key_binding'])
    core = {name:item for name,item in value.items() if name != 'mac'}
    _require(value['schema_version'] == ATTEST_SCHEMA and value['task_sha256'] == task['task_sha256']
             and value['capture_id'] == task['execution']['capture_id']
             and value['producer_run_id'] == task['execution']['producer_run_id']
             and value['deployment'] == task['execution']['source_deployments']['capture']
             and value['installation_sha256'] == authority.sha256 and value['phase'] in {'reserved','ready','committed'}
             and isinstance(value['mac'], str) and hmac.compare_digest(value['mac'],_mac(key,core)), '正式捕获来源认证失败')
    for ref in value['originals']:
        c.check_binding(root, ref)
    required = {'reservation.json','budget-reservation.json','daemon-owner.json'}
    if value['phase'] == 'ready' or (root/'ready.json').is_file():
        required |= {'ready.json','ready-observation.json','ready-environment.json'}
    if value['phase'] == 'committed':
        required |= {'native-capture-manifest.json','export-inventory.json','export-commit.json'}
        inventory = c.read_json(root/'export-inventory.json')
        verify_export(root,inventory,task_sha256=task['task_sha256'])
    _require(required <= {ref['path'] for ref in value['originals']}, '捕获来源认证未闭合必要原件')
    return copy.deepcopy(value)


def verify_formal_undispatched(task, capture_root):
    """无真实未派发认证时拒绝，不能用空数组或旧ready推断零请求。"""
    origin = verify_formal_capture_origin(task, capture_root)
    proof = c.read_json(Path(capture_root)/'undispatched.json')
    c.exact(proof, {'schema_version','task_sha256','producer_run_id','capture_id','status','request_count','requests','attestation'}, '正式未派发证明')
    _require(proof['schema_version']=='claude-upgrade-client-undispatched/v2' and origin['phase']=='committed'
             and origin['dispatch_state']=='not_dispatched'
             and proof['task_sha256']==task['task_sha256'] and proof['capture_id']==task['execution']['capture_id']
             and proof['producer_run_id']==task['execution']['producer_run_id'] and proof['status']=='undispatched'
             and type(proof['request_count']) is int and proof['request_count']==0, '正式未派发身份不符')
    _require(not c.read_bytes(c.check_binding(Path(capture_root),proof['requests'])), '真实原件已有已发请求，不能证明零值')
    _require(c.check_binding(Path(capture_root),proof['attestation']).name=='capture-origin-committed.json', '未派发证明未绑定准确来源认证')
    return proof


if __name__ == '__main__':
    import sys
    try:
        _require(len(sys.argv)==3 and sys.argv[1]=='--replay','正式复算入口参数非法')
        root = c.plain_path(Path(sys.argv[2]))
        replay_native(root,c.read_json(root/'native-capture-manifest.json'))
        print('{"status":"passed"}')
    except Exception as error:
        print(str(error),file=sys.stderr); sys.exit(1)
