"""ARM64 与 Mac 的固定认证桥、原路径投影和不可变原件同步。

桥只消费原 task 和原项目预算的短期镜像，不新增总账或租约。普通 JSON、
任意路径映射、命令和回调不能取得来源权限；副作用阶段有持久意图，断线后
只允许显式恢复。Mac 只预封存产品原件，最终捕获和联合读者留在 ARM64。
"""
from __future__ import annotations

import base64
import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import ipaddress
import os
import platform
import secrets
import signal
import socket
import ssl
import stat
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_formal_capture_contract as capture

MAC_INSTALL_ROOT = Path('/opt/sub2apiplus/claude-mac-client-agent')
INSTALL_SCHEMA = 'claude-upgrade-mac-bridge-installation/v1'
MAPPING_SCHEMA = 'claude-upgrade-mac-bridge-paths/v1'
EXPORT_SCHEMA = 'claude-upgrade-mac-bridge-export/v1'
RPC_SCHEMA = 'claude-upgrade-mac-bridge-rpc/v1'
ORIGIN_SCHEMA = 'claude-upgrade-mac-bridge-origin/v1'
_TOKEN = object()
_PATH_TOKEN = object()
_MAP_NAME = 'bridge-path-mapping.json'
_ROOT_NAME = 'bridge-export.json'
_MAX_FRAME = 1_048_576
_STAGES = frozenset({'prepare_state', 'start_monitoring', 'recheck_dispatch',
                    'launch_desktop', 'launch_vscode', 'drive_scenario', 'stop_product',
                    'restore_state', 'seal_sources', 'recover', 'export'})
_CODE_NAMES = ('claude_mac_agent_bridge.py', 'claude_mac_formal_capture_contract.py',
               'claude_mac_formal_capture_service.py', 'claude_mac_client_source.py',
               'claude_client_receipt_contract.py', 'claude_mac_product_completion.py',
               'claude_mac_product_provider.py', 'claude_mac_product_native.py',
               'claude_mac_product_recipe.py', 'claude_mac_product_authority.py',
               'claude_mac_product_source.py', 'claude_mac_product_broker.py',
               'claude_mac_product_origins.py', 'claude_mac_product_flows.py',
               'claude_mac_product_modules.py', 'claude_mac_product_logs.py',
               'claude_mac_product_materials.py', 'claude_mac_product_recorder.py',
               'claude_mac_product_state.py',
               'claude_mac_state_journal.py', 'claude_mac_product_native.m',
               'claude_mac_product_flows_native.m',
               'claude_mac_product_broker_native.m')


class MacBridgeError(c.CaptureError):
    """认证、完整原任务、路径合同或持久阶段不符。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise MacBridgeError(message)


def _absolute(value: Any) -> Path:
    _require(isinstance(value, str) and value and not any(ord(ch) < 32 for ch in value), '映射路径须为规范字符串')
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts and str(path) == value, '映射路径不规范或越根')
    return path


def _runtime_code() -> list[dict[str, Any]]:
    root = Path(__file__).parent
    return [{'file': name, 'sha256': hashlib.sha256((root/name).read_bytes()).hexdigest(),
             'bytes': (root/name).stat().st_size} for name in _CODE_NAMES]


@dataclass(frozen=True)
class BridgeInstallation:
    _value_bytes: bytes
    root: Path
    sha256: str
    key: bytes
    _token: object

    @property
    def value(self) -> dict[str, Any]:
        """安装封套保存原字节，外部只能读取副本。"""
        return c.parse_json(self._value_bytes)

    def revalidate(self, task: Mapping[str, Any]) -> None:
        _require(type(self) is BridgeInstallation and self._token is _TOKEN, '桥安装句柄不是固定原件读取器构造')
        actual = _read_bridge_installation(task)
        _require(actual.root == self.root and actual.sha256 == self.sha256 and actual.key == self.key
                 and actual.value == self.value, '桥原受管安装或认证材料漂移')


def _read_bridge_installation(task: Mapping[str, Any]) -> BridgeInstallation:
    task = capture._formal_task(task)
    side = 'mac' if platform.system() == 'Darwin' else 'capture'
    root = MAC_INSTALL_ROOT if side == 'mac' else capture.INSTALL_ROOT
    try:
        raw = capture.trusted_bytes(root/'bridge-installation.json', maximum=262144)
    except (OSError, ValueError) as error:
        raise capture.FormalCaptureUnavailable('固定认证桥尚无可信安装，零副作用拒绝') from error
    value = c.exact(c.parse_json(raw), {'schema_version', 'side', 'capture_deployment', 'mac_deployment',
                    'code_root', 'runtime_code', 'staging_root', 'logical_data_root', 'state_paths',
                    'endpoint', 'tls', 'channel_key', 'native_public_key', 'flow_public_key',
                    'materials_relative', 'qualification'}, '桥受管安装')
    _require(value['schema_version'] == INSTALL_SCHEMA and value['side'] == side
             and value['capture_deployment'] == task['execution']['source_deployments']['capture']
             and value['mac_deployment'] == task['execution']['source_deployments']['mac'], '桥安装未绑定完整双方部署')
    expected = root/'code'/'tools'/'official_client_capture'
    deployed = Path(value['logical_data_root'])/'tools'/'official_client_capture'
    allowed = {expected, deployed} if side == 'capture' else {expected}
    _require(value['code_root'] == str(expected) and Path(__file__).parent in allowed,
             '认证桥仅接受固定安装或冻结数据根的相同部署字节，开发检出没有来源权')
    _require(value['runtime_code'] == _runtime_code(), '桥实际运行代码闭包漂移')
    for reference in value['runtime_code']:
        code = capture.trusted_bytes(expected/reference['file'])
        _require(len(code) == reference['bytes'] and hashlib.sha256(code).hexdigest() == reference['sha256'], '桥源码原件漂移')
    staging, logical = _absolute(value['staging_root']), _absolute(value['logical_data_root'])
    _require(staging != Path('/') and logical != Path('/') and not staging.is_relative_to(logical)
             and not logical.is_relative_to(staging), '桥逻辑数据根与Mac物理根未分离')
    if side == 'mac':
        c.plain_path(staging)
    else:
        c.plain_path(logical)
    endpoint = c.exact(value['endpoint'], {'host', 'port'}, '桥固定TLS端点')
    ipaddress.ip_address(endpoint['host'])
    _require(type(endpoint['port']) is int and 0 < endpoint['port'] <= 65535, '桥端点端口非法')
    tls = c.exact(value['tls'], {'certificate', 'private_key', 'ca', 'peer_certificate_sha256'}, '桥双向TLS材料')
    _require(isinstance(tls['peer_certificate_sha256'], str) and c.SHA_RE.fullmatch(tls['peer_certificate_sha256']), '桥缺固定对端证书pin')
    for name in ('certificate', 'private_key', 'ca'):
        capture.trusted_binding(root, tls[name])
    key_path = c.managed_path(root, value['channel_key']['path'])
    key = capture.trusted_binding(root, value['channel_key'])
    _require(32 <= len(key) <= 128 and not stat.S_IMODE(key_path.stat().st_mode) & 0o007,
             '桥认证key无界或可由所有人读取')
    for name in ('native_public_key', 'flow_public_key'):
        _require(bool(capture.trusted_binding(root, value[name])), '桥缺独立原生签名公钥')
    _read_materials_export(task, root, value)
    qualification = c.parse_json(capture.trusted_binding(root, value['qualification']))
    _require(qualification.get('schema_version') == 'claude-upgrade-source-qualification/v1'
             and qualification.get('source_scope') == 'formal' and qualification.get('status') == 'passed'
             and qualification.get('kind') == 'bridge'
             and qualification.get('capture_deployment') == value['capture_deployment']
             and qualification.get('mac_deployment') == value['mac_deployment'], '桥没有当前双方来源资格原件')
    _require(isinstance(value['state_paths'], dict) and set(value['state_paths']) == {'claude-desktop', 'claude-vscode'}, '桥状态映射没有客户端闭集')
    return BridgeInstallation(c.canonical(value), root, hashlib.sha256(raw).hexdigest(), key, _TOKEN)


def _read_materials_export(task, install_root: Path, installation: Mapping[str, Any]):
    """预算和监听之前实读固定物料库存，不把现场缺件延迟到产品启动。"""
    origin = c.managed_path(install_root, installation['materials_relative'])
    value = c.exact(c.parse_json(capture.trusted_bytes(origin/'materials-export.json')),
                    {'client_id','files','materials_receipt'}, '桥公开物料库存')
    _require(value['client_id'] == task['client_id'] and isinstance(value['files'], list)
             and 0 < len(value['files']) <= 65536, '桥公开物料客户端或库存无界')
    names = []
    for reference in value['files']:
        c.exact(reference, {'path','sha256','bytes'}, '桥公开物料原件')
        names.append(reference['path'])
        capture.trusted_binding(origin, reference)
    _require(names == sorted(set(names), key=lambda name: PurePosixPath(name).parts)
             and value['materials_receipt'] in value['files'],
             '桥物料库存重复、缺真实读取收据或顺序漂移')
    return origin, value


def require_mac_bridge_installation(task: Mapping[str, Any]) -> BridgeInstallation:
    """根据实际运行宿主选择固定安装，绝不由上传任务选择宿主路径。"""
    return _read_bridge_installation(task)


def require_mac_agent_installation(task: Mapping[str, Any]) -> BridgeInstallation:
    _require(platform.system() == 'Darwin', 'Mac产品后端不能在ARM64代理位置装配')
    return _read_bridge_installation(task)


def _session_roots(task: Mapping[str, Any], installation: BridgeInstallation) -> tuple[Path, Path]:
    # 以冻结前已知的nonce/producer形成私有目录，避免制品路径→task摘要→路径
    # 的自引用。recipe/task冻结之后不再更改这些路径或复制品身份。
    base = _absolute(installation.value['staging_root'])/task['client_id']/task['execution']['producer_run_id']/task['task_nonce']
    return base/'source', base/'session'


@dataclass(frozen=True)
class BridgePathContext:
    """实际认证映射的opaque能力；字段相同的外部dataclass仍没有读取权。"""
    task_sha256: str
    logical_bundle_root: Path
    staging_bundle_root: Path
    logical_session_root: Path
    staging_session_root: Path
    state_paths: tuple[tuple[str, str], ...]
    mapping_binding: Mapping[str, Any]
    _token: object


def _mapping_context(task: Mapping[str, Any], bundle_root: Path, installation: BridgeInstallation) -> BridgePathContext:
    task = capture._formal_task(task)
    root = c.plain_path(Path(bundle_root))
    path = root/_MAP_NAME
    value = c.exact(c.read_json(path), {'schema_version', 'task', 'capture_deployment', 'mac_deployment',
                    'logical_bundle_root', 'staging_bundle_root', 'logical_session_root',
                    'staging_session_root', 'state_paths', 'recipe', 'mac'}, '认证路径映射')
    core = {name:item for name,item in value.items() if name != 'mac'}
    _require(value['schema_version'] == MAPPING_SCHEMA and value['task'] == task
             and value['capture_deployment'] == task['execution']['source_deployments']['capture']
             and value['mac_deployment'] == task['execution']['source_deployments']['mac']
             and isinstance(value['mac'], str) and hmac.compare_digest(value['mac'], capture._mac(installation.key, core)),
             '路径映射未由固定双方来源认证完整原task')
    logical = _absolute(value['logical_bundle_root'])
    staging, session = _session_roots(task, installation)
    _require(value['staging_bundle_root'] == str(staging) and value['staging_session_root'] == str(session)
             and logical.is_relative_to(_absolute(installation.value['logical_data_root']))
             and root == (staging if installation.value['side'] == 'mac' else logical), '认证映射staging或逻辑根被替换/越根')
    recipe_ref = task['execution']['isolation_recipe']
    recipe_path = c.check_binding(root, recipe_ref)
    recipe = c.read_json(recipe_path)
    _require(value['recipe'] == recipe_ref and value['logical_session_root'] == recipe['private_session_root'], '映射未绑定准确原recipe及逻辑会话根')
    original_session = _absolute(value['logical_session_root'])
    _require(original_session.is_relative_to(logical.parent) and original_session != logical.parent
             and not original_session.is_relative_to(logical) and not logical.is_relative_to(original_session), '原recipe私有会话未与来源根分离')
    rows = value['state_paths']
    _require(isinstance(rows, list) and 0 < len(rows) <= 32
             and all(isinstance(row, dict) and set(row) == {'logical', 'physical'} for row in rows), '映射状态位置闭集非法')
    pairs = tuple((_absolute(row['logical']), _absolute(row['physical'])) for row in rows)
    _require(len({a for a,_b in pairs}) == len(pairs) and len({b for _a,b in pairs}) == len(pairs)
             and {str(a) for a,_b in pairs} == set(recipe['state_paths']), '映射状态位置未知、重复或部分缺失')
    expected = installation.value['state_paths'][task['client_id']]
    _require(rows == expected, '映射状态路径不是固定安装冻结的原路径全集')
    for index, (_logical, physical) in enumerate(pairs):
        _require(not any(physical == other or physical.is_relative_to(other) or other.is_relative_to(physical)
                         for _a,other in pairs[index+1:]), '映射状态位置相互重叠')
        _require(not physical.is_relative_to(staging) and not staging.is_relative_to(physical)
                 and not physical.is_relative_to(session) and not session.is_relative_to(physical), '原状态映射与私有副本重叠')
        if installation.value['side'] == 'mac':
            c.plain_path(physical)
    return BridgePathContext(task['task_sha256'], logical, staging, original_session, session,
             tuple((str(a), str(b)) for a,b in pairs), c.binding(root, path), _PATH_TOKEN)


def verify_bridge_path_context(task: Mapping[str, Any], bundle_root: Path) -> BridgePathContext:
    installation = require_mac_bridge_installation(task)
    return _mapping_context(task, Path(bundle_root), installation)


def validate_bridge_path_context(task: Mapping[str, Any], context: Any, bundle_root: Path) -> Path:
    _require(type(context) is BridgePathContext and context._token is _PATH_TOKEN,
             '普通JSON或外部dataclass不能授跨机路径权限')
    actual = verify_bridge_path_context(task, bundle_root)
    _require(context == actual and context.task_sha256 == task['task_sha256'], '原task或staging路径上下文漂移')
    return actual.staging_session_root


def verify_bridge_isolation_recipe(task: Mapping[str, Any], recipe: Mapping[str, Any], bundle_root: Path) -> None:
    context = verify_bridge_path_context(task, bundle_root)
    actual = c.read_json(c.check_binding(Path(bundle_root), task['execution']['isolation_recipe']))
    _require(dict(recipe) == actual and actual['private_session_root'] == str(context.logical_session_root)
             and set(actual['state_paths']) == {a for a,_b in context.state_paths}, '原隔离recipe与完整认证映射不同源')


def _read_frame(connection: socket.socket) -> dict[str, Any]:
    blocks, size = [], 0
    while True:
        block = connection.recv(min(65536, _MAX_FRAME-size+1))
        _require(bool(block), '桥响应裂尾')
        blocks.append(block); size += len(block)
        _require(size <= _MAX_FRAME, '桥帧超过固定上界')
        if b'\n' in block:
            raw = b''.join(blocks)
            _require(raw.endswith(b'\n') and raw.count(b'\n') == 1, '桥只接受一份规范认证帧')
            return c.parse_json(raw[:-1])


def _tls_context(installation: BridgeInstallation, *, server: bool) -> ssl.SSLContext:
    value = installation.value['tls']
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    if not server:
        context.check_hostname = False
    for name in ('certificate', 'private_key', 'ca'):
        capture.trusted_binding(installation.root, value[name])
    context.load_verify_locations(cafile=str(c.managed_path(installation.root, value['ca']['path'])))
    context.load_cert_chain(str(c.managed_path(installation.root, value['certificate']['path'])),
                            str(c.managed_path(installation.root, value['private_key']['path'])))
    return context


def _peer(connection: ssl.SSLSocket, installation: BridgeInstallation) -> None:
    certificate = connection.getpeercert(binary_form=True)
    _require(certificate and hashlib.sha256(certificate).hexdigest()
             == installation.value['tls']['peer_certificate_sha256'], '桥TLS对端不是冻结证书')


def _request(installation: BridgeInstallation, task: Mapping[str, Any], logical: Path,
             operation: str, operation_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    installation.revalidate(task)
    _require(operation in _STAGES, '桥没有任意命令入口')
    nonce = secrets.token_hex(24)
    core = {'schema_version': RPC_SCHEMA, 'task': task, 'logical_bundle_root': str(logical),
            'operation': operation, 'operation_id': operation_id, 'payload': dict(payload),
            'nonce': nonce, 'observed_at_utc': c.utc_now()}
    endpoint = installation.value['endpoint']
    with socket.create_connection((endpoint['host'], endpoint['port']), timeout=10) as raw:
        with _tls_context(installation, server=False).wrap_socket(raw, server_hostname=endpoint['host']) as connection:
            connection.settimeout(30); _peer(connection, installation)
            connection.sendall(c.canonical({**core, 'mac': capture._mac(installation.key, core)})+b'\n')
            reply = c.exact(_read_frame(connection), {'schema_version', 'task_sha256', 'operation_id', 'nonce', 'ok', 'value', 'mac'}, '桥认证响应')
    signed = {name:value for name,value in reply.items() if name != 'mac'}
    _require(reply['schema_version'] == RPC_SCHEMA and reply['task_sha256'] == task['task_sha256']
             and reply['operation_id'] == operation_id and reply['nonce'] == nonce
             and isinstance(reply['mac'], str) and hmac.compare_digest(reply['mac'], capture._mac(installation.key,signed)), '桥响应认证/原task/操作不符')
    _require(reply['ok'] is True and isinstance(reply['value'], dict), '桥阶段失败；保留原意图并显式恢复')
    return reply['value']


class MacAgentBridge:
    """原 ProductRecorder 九方法的固定跨机实现，两种launch仍互斥。"""
    source_scope = 'formal'

    def __init__(self, task, *, bundle_root, services, capture_handle, installation, _token):
        _require(_token is _TOKEN and type(installation) is BridgeInstallation, '桥须由固定工厂构造')
        self._task = copy.deepcopy(capture._formal_task(task)); self._root = c.plain_path(Path(bundle_root))
        self._services, self._capture, self._installation, self._token = services, capture_handle, installation, _token

    def _guard(self, task, bundle_root=None):
        _require(type(self) is MacAgentBridge and self._token is _TOKEN and capture._formal_task(task) == self._task
                 and (bundle_root is None or Path(bundle_root) == self._root), '桥所有权、原task或来源根不符')
        self._installation.revalidate(self._task)

    def _call(self, operation, payload, *, operation_id=None, live=False):
        self._guard(self._task)
        request = copy.deepcopy(dict(payload))
        if live:
            self._services.recheck_dispatch(self._task)
            request['dispatch'] = self._capture.dispatch_envelope(self._task, channel_key=self._installation.key)
        operation_id = operation_id or operation
        control = self._root.parent/'bridge-control'; c.private_directory(control)
        intent = control/(operation_id+'.intent.json'); result = control/(operation_id+'.result.json')
        frozen = {'task_sha256': self._task['task_sha256'], 'operation': operation, 'operation_id': operation_id,
                  'payload_sha256': c.digest(dict(payload))}
        if result.exists():
            saved = c.read_json(result)
            _require(saved['intent'] == frozen, '桥旧返回不属于相同原阶段')
            return saved['value']
        _require(not intent.exists(), '桥阶段已持久派发但结果未知，只可显式recover，不能自动重发产品')
        c.write_json_once(intent, frozen)
        value = _request(self._installation,self._task,self._root,operation,operation_id,request)
        c.write_json_once(result, {'intent': frozen, 'value': value})
        return value

    def prepare_state(self, task, bundle_root):
        self._guard(task,bundle_root)
        # 只传完整冻结附件闭包，remote读取固定安装物料，不接宿主任意路径。
        recipe = c.read_bytes(c.check_binding(self._root,self._task['execution']['isolation_recipe']))
        value = self._call('prepare_state', {'recipe_base64': base64.b64encode(recipe).decode()}, live=True)
        c.write_json_once(self._root/_MAP_NAME,value['mapping'])
        verify_bridge_path_context(self._task,self._root)

    def start_monitoring(self, task, bundle_root):
        self._guard(task,bundle_root); self._call('start_monitoring',{},live=True)

    def recheck_dispatch(self, task):
        self._guard(task)
        # 每次重新实时核验，不能消费上一次便宜检查的返回。
        dispatch = self._capture.dispatch_envelope(task,channel_key=self._installation.key)
        return _request(self._installation, self._task, self._root, 'recheck_dispatch',
                        'recheck-'+secrets.token_hex(12), {'dispatch': dispatch})

    def launch_desktop(self, task, recipe):
        self._guard(task); _require(task['client_id']=='claude-desktop','客户端launch类型不符')
        self._call('launch_desktop', {'recipe': dict(recipe)},live=True)

    def launch_vscode(self, task, recipe):
        self._guard(task); _require(task['client_id']=='claude-vscode','客户端launch类型不符')
        self._call('launch_vscode', {'recipe': dict(recipe)},live=True)

    def drive_scenario(self, task, scenario):
        self._guard(task); _require(scenario in self._task['scenarios'],'场景不属于冻结闭集')
        self._call('drive_scenario', {'scenario': dict(scenario)}, operation_id='scenario-'+scenario['id'],live=True)

    def stop_product(self, task):
        self._guard(task); self._call('stop_product',{})

    def restore_state(self, task, bundle_root):
        self._guard(task,bundle_root); self._call('restore_state',{})

    def seal_sources(self, task, bundle_root, *, status):
        self._guard(task,bundle_root)
        native=c.read_json(self._capture.capture_root/'native-capture-manifest.json')
        if native['status']=='truncated':
            status='truncated'
        elif native['status']=='failed' and status=='complete':
            status='failed'
        export = self._call('seal_sources', {'status': status})
        self._sync(export)
        self._capture.finalize_product_capture(self._task,self._root)
        _finalize_source(self._task,self._root)

    def recover(self, task, bundle_root):
        self._guard(task,bundle_root)
        # recover自己是安全的显式收口：不重新prepare/launch/drive，原预算不续期。
        reply = _request(self._installation,self._task,self._root,'recover',
                          'recover-'+secrets.token_hex(12),{})
        if reply.get('export') is not None:
            self._sync(reply['export'])
        return reply

    def _sync(self, export):
        _verify_export_envelope(self._task,export,self._installation)
        partial = c.private_directory(self._root.parent/'bridge-download'/export['export_id'])
        for reference in export['files']:
            target = c.managed_path(self._root,reference['path'])
            if target.exists():
                c.check_binding(self._root,reference); continue
            leaf = c.managed_path(partial,reference['path']+'.part'); c.private_directory(leaf.parent)
            previous = c.read_bytes(leaf) if leaf.exists() else b''
            _require(len(previous) <= reference['bytes'],'桥同步已有前缀超长')
            blocks=[previous]; amount=len(previous); prefix_hash=hashlib.sha256(previous)
            prefix = _request(self._installation,self._task,self._root,'export','read-prefix-'+secrets.token_hex(12),
                              {'export_id':export['export_id'],'path':reference['path'],'offset':0,'limit':0,'prefix_bytes':amount})
            _require(prefix['prefix_sha256'] == prefix_hash.hexdigest(),'桥同步前缀不是同一不可变export')
            while amount < reference['bytes']:
                chunk = _request(self._installation,self._task,self._root,'export','read-'+secrets.token_hex(12),
                     {'export_id':export['export_id'],'path':reference['path'],'offset':amount,
                      'limit':min(65536,reference['bytes']-amount),'prefix_bytes':amount})
                _require(chunk['prefix_sha256'] == prefix_hash.hexdigest(),'桥同步原前缀漂移')
                raw = base64.b64decode(chunk['data_base64'],validate=True)
                _require(0 < len(raw) <= 65536 and amount+len(raw)<=reference['bytes'],'桥同步分块边界非法')
                descriptor = os.open(leaf,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
                try:
                    info=os.fstat(descriptor)
                    _require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                             and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600
                             and info.st_size==amount,'桥部分同步原件实例、权限或前缀长度漂移')
                    with os.fdopen(descriptor,'wb',closefd=False) as out:
                        out.write(raw); out.flush(); os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                blocks.append(raw); amount+=len(raw); prefix_hash.update(raw)
            previous=b''.join(blocks)
            _require(prefix_hash.hexdigest()==reference['sha256'],'桥同步完整原件摘要漂移')
            c.private_directory(target.parent); c.write_once(target,previous)
        c.write_json_once(self._root/_ROOT_NAME,export)
        verify_bridge_path_context(self._task,self._root)


def create_mac_agent_bridge(task, *, bundle_root, services, capture_handle):
    installation = require_mac_bridge_installation(task)
    _require(installation.value['side']=='capture','ARM64工厂不能从Mac物理位置发起另一份捕获')
    from tools.official_client_capture import claude_mac_formal_capture_service as service
    from tools.official_client_capture.claude_upgrade_mac_candidate_services import CandidateMacClientServices
    _require(type(services) is CandidateMacClientServices and service.owns_formal_capture(capture_handle,task),
             '桥须复用原Candidate服务及原捕获句柄')
    return MacAgentBridge(task,bundle_root=bundle_root,services=services,capture_handle=capture_handle,
                          installation=installation,_token=_TOKEN)


def owns_mac_agent_bridge(handle, task, bundle_root) -> bool:
    return (type(handle) is MacAgentBridge and handle._token is _TOKEN and handle._task==task
            and handle._root==Path(bundle_root))


def _verify_export_envelope(task, export, installation):
    c.exact(export, {'schema_version','task_sha256','capture_deployment','mac_deployment','mapping',
                    'preseal','files','export_id','mac'},'Mac认证原件库存')
    core = {name:value for name,value in export.items() if name not in {'export_id','mac'}}
    _require(export['schema_version']==EXPORT_SCHEMA and export['task_sha256']==task['task_sha256']
             and export['capture_deployment']==task['execution']['source_deployments']['capture']
             and export['mac_deployment']==task['execution']['source_deployments']['mac']
             and export['export_id']==c.digest(core) and isinstance(export['mac'],str)
             and hmac.compare_digest(export['mac'],capture._mac(installation.key,{**core,'export_id':export['export_id']})), 'Mac库存来源认证或任务不符')
    _require(isinstance(export['files'],list) and 0<len(export['files'])<=65536,'Mac原件库存无界')
    names=[]
    for reference in export['files']:
        c.exact(reference,{'path','sha256','bytes'},'Mac库存原件')
        c.managed_path(Path('/private/tmp'),reference['path'])
        _require(type(reference['bytes']) is int and 0<=reference['bytes']<=67108864
                 and isinstance(reference['sha256'],str) and c.SHA_RE.fullmatch(reference['sha256']), 'Mac库存原件大小/摘要非法')
        names.append(reference['path'])
    _require(names == sorted(set(names), key=lambda name: PurePosixPath(name).parts)
             and export['mapping'] in export['files'] and export['preseal'] in export['files'],
             'Mac库存不含原mapping/preseal或有重复路径')


def verify_mac_bridge_origin(task, bundle_root):
    task=capture._formal_task(task); root=c.plain_path(Path(bundle_root))
    installation=require_mac_bridge_installation(task)
    export=c.read_json(root/_ROOT_NAME)
    _verify_export_envelope(task,export,installation)
    for reference in export['files']:
        c.check_binding(root,reference)
    _require(c.check_binding(root,export['mapping'])==root/_MAP_NAME,'Mac来源不含精确认证路径原件')
    context=_mapping_context(task,root,installation)
    from tools.official_client_capture import claude_mac_product_origins as product
    originals=product.verify_product_originals(task,root,
        native_public_key=capture.trusted_binding(installation.root,installation.value['native_public_key']),
        flow_public_key=capture.trusted_binding(installation.root,installation.value['flow_public_key']),
        path_context=context)
    capture_root=Path(task['execution']['capture_root'])
    if (capture_root/'capture-manifest.json').exists():
        process=product.verify_process_session(task,root,originals)
        native=c.read_json(capture_root/'native-capture-manifest.json')
        expected=_derive_transport_rows(task,capture._json_lines(capture_root,native['connections']),
                     capture._json_lines(capture_root,native['requests']),originals,process)
        for key,base,name in (('connections',capture_root,'server-connections.jsonl'),
                              ('socket_calls',root,'socket-calls.jsonl'),
                              ('transport_map',capture_root,'transport-map.jsonl')):
            actual=[{k:v for k,v in row.items() if k!='observation'} for row in
                    capture._json_lines(base,c.binding(base,base/name))]
            _require(actual==expected[key],'联合'+key+'并非真实签名原件与独立capture的精确派生')
        _require(capture._json_lines(capture_root,c.binding(capture_root,capture_root/'server-requests.jsonl'))==
                 expected['requests'],'联合requests不是独立原字节的精确派生')
    return {'export_id':export['export_id'],'path_context':context,'preseal':export['preseal'],'originals':originals}


def _finalize_source(task, root):
    """联合后发布原v2清单；Mac预封存不能提前宣称完整成功。"""
    verify_mac_bridge_origin(task,root)
    # 产品原生签名封套由Mac预封存生成；认证桥库存是另一份独立来源。
    # 不覆盖它，也不能用ARM64生成的HMAC替代真实产品／NE签名原件。
    origin=c.exact(c.read_json(root/'source-origin.json'),{'task_sha256','deployment','attestation'},'产品来源原件')
    _require(origin['task_sha256']==task['task_sha256']
             and origin['deployment']==task['execution']['source_deployments']['mac']
             and c.check_binding(root,origin['attestation']).name=='product-origin-attestation.json', '联合导出缺真实产品签名来源')
    native=c.read_json(Path(task['execution']['capture_root'])/'capture-manifest.json')
    files=[]
    for path in sorted(root.rglob('*')):
        _require(not path.is_symlink(),'联合来源包含链接')
        if path.is_file() and path.name!='source-manifest.json':
            files.append(c.binding(root,path))
        else:
            _require(path.is_dir() or path==root/'source-manifest.json','来源包含特殊原件')
    status=c.read_json(root/'recorder-execution.json')['status']
    value={'schema_version':capture.source.BUNDLE_SCHEMA,'task_sha256':task['task_sha256'],
           'run_id':task['execution']['producer_run_id'],'source_scope':'formal','status':status,'files':files}
    c.write_json_once(root/'source-manifest.json',value)
    capture.source.verify_mac_source_v2(task,root,Path(task['execution']['capture_root']))


def _scenario_windows(task, originals):
    windows={}
    for scenario in originals['scenarios']:
        _require(scenario['scenario_id'] in {row['id'] for row in task['scenarios']},'原产品日志含未知场景')
        events=scenario['session_events']
        inputs=[row for row in events if row['kind']=='input']
        completions=[row for row in events if row['kind']=='completed']
        _require(len(inputs)==len(completions)==1 and inputs[0]['session_id']==completions[0]['session_id']==originals['session_id'],
                 '真实场景缺唯一同会话输入与完成日志')
        start,end=capture.source.c.timestamp(inputs[0]['observed_at_utc']),capture.source.c.timestamp(completions[0]['observed_at_utc'])
        _require(start<=end and scenario['scenario_id'] not in windows,'场景时间重复或逆序')
        windows[scenario['scenario_id']]=(start,end)
    return windows


def _derive_transport_rows(task, native_connections, native_requests, originals, process):
    """纯投影只认实际endpoint与进程实例；NAT、未知或多义均拒绝。

    NE从不提供HTTP ordinal／H2 stream。ordinal完全取独立捕获原件；
    真实产品日志仅确定场景窗口。端点不同不能用近似时间或客户端标签猜配。
    """
    windows=_scenario_windows(task,originals)
    session=process['process_session']; instance_ids=process['process_instances']
    flows=originals['network_flows']; mapped_flows=set(); calls=[]; maps=[]; connections=[]
    requests_by_connection={}
    for row in native_requests:
        requests_by_connection.setdefault(row['connection_id'],[]).append(row)
    modules=session.get('loaded_modules',[])
    parents={row['process_instance_id']:row['parent_instance_id'] for row in session['processes']}
    for connection in native_connections:
        _require(connection.get('closed_at_utc') is not None,'未关闭连接不能伪造联合终态')
        opened,closed=capture.source.c.timestamp(connection['opened_at_utc']),capture.source.c.timestamp(connection['closed_at_utc'])
        candidates=[flow for flow in flows if flow['client_endpoint']==connection['client_endpoint']
                    and flow['server_endpoint']==connection['server_endpoint']
                    and flow['closed_at_utc'] is not None
                    and capture.source.c.timestamp(flow['opened_at_utc'])<=closed
                    and opened<=capture.source.c.timestamp(flow['closed_at_utc'])]
        _require(len(candidates)==1,'独立连接与真实NE端点不能唯一闭合；NAT/多匹配不猜通过')
        flow=candidates[0]
        _require(flow['flow_id'] not in mapped_flows,'一个真实NE流不能冒充两个独立连接')
        mapped_flows.add(flow['flow_id'])
        token=tuple(flow['process']['audit_token'])
        process_id=instance_ids.get(token)
        _require(process_id is not None,'真实NE流未绑定已签进程实例')
        ancestors=set(); at=process_id
        while at is not None:
            _require(at in parents and at not in ancestors,'联合进程祖先缺失或循环')
            ancestors.add(at); at=parents[at]
        active_modules=[row['module_instance_id'] for row in modules
                        if row['host_process_instance_id'] in ancestors
                        and capture.source.c.timestamp(row['activated_at_utc'])<=opened]
        groups={}
        for row in requests_by_connection.get(connection['connection_id'],[]):
            observed=capture.source.c.timestamp(row['observed_at_utc'])
            _require(max(opened,capture.source.c.timestamp(flow['opened_at_utc']))<=observed<=
                     min(closed,capture.source.c.timestamp(flow['closed_at_utc'])),'真实请求不在两端共同连接生命周期')
            matches=[identifier for identifier,(start,end) in windows.items() if start<=observed<=end]
            model=row['method']=='POST' and row['path'].split('?',1)[0] in capture.source.c.MODEL_ENDPOINTS
            _require(len(matches)<=1 and (not model or len(matches)==1),'模型请求没有唯一真实场景日志窗口')
            identifier=matches[0] if matches else None
            groups.setdefault(identifier,[]).append(row['request_ordinal'])
        connections.append({name:connection[name] for name in
                   ('connection_id','client_endpoint','server_endpoint','opened_at_utc','closed_at_utc')})
        for identifier,ordinals in groups.items():
            call_id='flow-'+c.digest({'flow_id':flow['flow_id'],'connection_id':connection['connection_id'],
                                     'scenario_id':identifier})[:32]
            call={'socket_call_id':call_id,'process_instance_id':process_id,
                  'module_instance_ids':active_modules,'session_id':originals['session_id'],'scenario_id':identifier,
                  'client_endpoint':flow['client_endpoint'],'server_endpoint':flow['server_endpoint'],
                  'opened_at_utc':flow['opened_at_utc'],'closed_at_utc':flow['closed_at_utc'],
                  'request_ordinals':ordinals}
            calls.append(call)
            maps.append({'socket_call_id':call_id,'connection_id':connection['connection_id'],'hops':[],
                         'observed_at_utc':native_requests[0]['observed_at_utc'] if not ordinals else
                            next(row['observed_at_utc'] for row in requests_by_connection[connection['connection_id']]
                                 if row['request_ordinal']==ordinals[0])})
    _require(mapped_flows=={flow['flow_id'] for flow in flows},'真实NE产品流缺独立捕获，不得漏计为零')
    _require(set(requests_by_connection)<={row['connection_id'] for row in native_connections},'原请求缺真实连接')
    rows=[{name:row[name] for name in ('connection_id','request_ordinal','observed_at_utc','host','method','path',
                                      'request_body_base64','response_body_base64')} for row in native_requests]
    return {'connections':connections,'requests':rows,'socket_calls':calls,'transport_map':maps}


def materialize_transport_map(task, *, capture_root, product_root, native_manifest):
    task=capture._formal_task(task); root=c.plain_path(Path(capture_root)); product_root=c.plain_path(Path(product_root))
    _require(root==Path(task['execution']['capture_root']),'联合捕获不是原ARM64根')
    origin=verify_mac_bridge_origin(task,product_root)
    installation=require_mac_bridge_installation(task)
    from tools.official_client_capture import claude_mac_product_origins as product
    originals=origin['originals']
    process=product.build_process_session(task,product_root,originals)
    connections=capture._json_lines(root,native_manifest['connections'])
    requests=capture._json_lines(root,native_manifest['requests'])
    rows=_derive_transport_rows(task,connections,requests,originals,process)

    def observed(base: Path, key: str, values):
        output=[]
        directory=c.private_directory(base/'joint-observations')
        for index,value in enumerate(values):
            path=directory/(key+f'-{index:05d}.json'); c.write_json_once(path,value)
            output.append({**value,'observation':c.binding(base,path)})
        target=base/(key+'.jsonl'); c.write_once(target,b''.join(c.canonical(row)+b'\n' for row in output))
        return c.binding(base,target)

    result={'connections':observed(root,'server-connections',rows['connections']),
            'requests':None,'transport_map':observed(root,'transport-map',rows['transport_map'])}
    observed(product_root,'socket-calls',rows['socket_calls'])
    requests_path=root/'server-requests.jsonl'; c.write_once(requests_path,b''.join(c.canonical(row)+b'\n' for row in rows['requests']))
    result['requests']=c.binding(root,requests_path)
    # 最终场景结果由原v2读者按真实日志、挑战、请求与实际响应定稿，
    # 不用请求计数或NE流单独补出场景成功。
    return result


class MacBridgeEngine:
    """Mac固定端只调用已验原ProductRecorder；上传参数不选择实现或命令。"""

    def __init__(self, task, installation: BridgeInstallation):
        self.task=copy.deepcopy(capture._formal_task(task)); self.installation=installation
        _require(installation.value['side']=='mac','产品服务只可在固定Mac部署运行')
        self.root,self.session=_session_roots(self.task,installation)
        self.control=self.root.parent/'agent-control'
        self.recorder=None
        self._nonces=set()
        self._prefixes={}
        self._lock=threading.RLock()

    def _export(self, *, validate_files=True):
        path=self.control/'sealed-export.json'
        value=c.read_json(path)
        _verify_export_envelope(self.task,value,self.installation)
        if validate_files:
            for reference in value['files']:
                c.check_binding(self.root,reference)
        return value

    def _mapping(self, logical: Path, recipe_ref: Mapping[str,Any]):
        recipe=c.read_json(c.check_binding(self.root,recipe_ref))
        core={'schema_version':MAPPING_SCHEMA,'task':self.task,
              'capture_deployment':self.task['execution']['source_deployments']['capture'],
              'mac_deployment':self.task['execution']['source_deployments']['mac'],
              'logical_bundle_root':str(logical),'staging_bundle_root':str(self.root),
              'logical_session_root':recipe['private_session_root'],'staging_session_root':str(self.session),
              'state_paths':copy.deepcopy(self.installation.value['state_paths'][self.task['client_id']]),
              'recipe':dict(recipe_ref)}
        value={**core,'mac':capture._mac(self.installation.key,core)}
        c.write_json_once(self.root/_MAP_NAME,value)
        return _mapping_context(self.task,self.root,self.installation)

    def _stage_materials(self):
        """复制固定安装审核的公开附件全集，不扫描HOME、不解引用凭据。"""
        origin,value=_read_materials_export(self.task,self.installation.root,self.installation.value)
        for reference in value['files']:
            raw=capture.trusted_binding(origin,reference)
            target=c.managed_path(self.root,reference['path']); c.private_directory(target.parent)
            c.write_once(target,raw)
        _require(value['materials_receipt'] in value['files'],'桥物料缺实际读取收据')
        c.write_json_once(self.control/'materials-receipt.json',value['materials_receipt'])
        return c.check_binding(self.root,value['materials_receipt'])

    def _recorder(self, dispatch, *, fresh):
        from tools.official_client_capture import claude_mac_product_source as product
        context=_mapping_context(self.task,self.root,self.installation)
        if self.recorder is None:
            receipt=c.read_json(self.control/'materials-receipt.json')
            self.recorder=product.create_qualified_source(self.task,bundle_root=self.root,
                materials_receipt=c.check_binding(self.root,receipt),path_context=context,dispatch=dispatch)
        elif fresh:
            product.refresh_capture_dispatch(self.recorder,self.task,dispatch)
        return self.recorder

    def _historical_dispatch(self):
        references=c.read_json(self.control/'original-dispatch.json')
        raw=c.read_json(c.check_binding(self.root,references))
        # 冷恢复只读取最初持久化的镜像成员；过期句柄也保留原件绑定供来源封存重验。
        _require(references['path'] == 'capture-dispatch-'+c.digest(raw)+'.json',
                 '历史派发绑定不是原镜像固定成员')
        dispatch=capture._verify_dispatch_envelope(self.task,raw,self.installation.key,
                    require_live=False,bundle_root=self.root)
        _require(dispatch.envelope_binding == references,'历史派发原件绑定漂移')
        return dispatch

    def _sealed(self, status):
        self.recorder.seal_for_bridge(self.task,self.root,status=status)
        preseal_path=self.root/'product-preseal.json'
        _require(preseal_path.is_file(),'产品固定预封存缺准确原件')
        files=[c.binding(self.root,path) for path in sorted(self.root.rglob('*')) if path.is_file()]
        core={'schema_version':EXPORT_SCHEMA,'task_sha256':self.task['task_sha256'],
              'capture_deployment':self.task['execution']['source_deployments']['capture'],
              'mac_deployment':self.task['execution']['source_deployments']['mac'],
              'mapping':c.binding(self.root,self.root/_MAP_NAME),'preseal':c.binding(self.root,preseal_path),'files':files}
        value={**core,'export_id':c.digest(core)}; value['mac']=capture._mac(self.installation.key,value)
        c.write_json_once(self.control/'sealed-export.json',value)
        return self._export()

    def _chunk(self, payload):
        """认证库存保持不变时仅增量哈希前缀；不为每个64KB块全库重验。"""
        c.exact(payload,{'export_id','path','offset','limit','prefix_bytes'},'桥原件分块参数')
        export=self._export(validate_files=False)
        _require(payload['export_id']==export['export_id'],'桥export身份漂移')
        matches=[row for row in export['files'] if row['path']==payload['path']]
        _require(len(matches)==1,'桥禁止读库存以外原件')
        reference=matches[0]; offset,limit,prefix=(payload[name] for name in ('offset','limit','prefix_bytes'))
        _require(all(type(item) is int for item in (offset,limit,prefix))
                 and 0<=offset<=reference['bytes'] and 0<=prefix<=reference['bytes']
                 and 0<=limit<=65536,'桥分块坐标无界')
        target=c.managed_path(self.root,reference['path'])
        identity_fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_nlink')
        cache_key=(export['export_id'],reference['path'])
        state=self._prefixes.get(cache_key)
        if state is None:
            c.check_binding(self.root,reference)
            info=target.lstat()
            state={'identity':tuple(getattr(info,name) for name in identity_fields),
                   'amount':0,'hash':hashlib.sha256()}
            self._prefixes[cache_key]=state
        descriptor=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(descriptor)
            _require(tuple(getattr(before,name) for name in identity_fields)==state['identity'],
                     '桥分块原件实例或元数据漂移')
            if prefix<state['amount']:
                state['hash']=hashlib.sha256(); state['amount']=0
            while state['amount']<prefix:
                block=os.pread(descriptor,min(65536,prefix-state['amount']),state['amount'])
                _require(bool(block),'桥前缀原件截断')
                state['hash'].update(block); state['amount']+=len(block)
            block=os.pread(descriptor,limit,offset)
            after,current=os.fstat(descriptor),target.lstat()
            _require(all(getattr(before,name)==getattr(after,name)==getattr(current,name)
                         for name in identity_fields),'桥分块读取时原件变化')
            return {'prefix_sha256':state['hash'].hexdigest(),'data_base64':base64.b64encode(block).decode()}
        finally:
            os.close(descriptor)

    def handle(self, request: Mapping[str,Any]) -> dict[str,Any]:
        """校验、原意图、固定副作用依次执行；pending绝不自动再次启动。"""
        with self._lock:
            self.installation.revalidate(self.task)
            value=c.exact(dict(request),{'schema_version','task','logical_bundle_root','operation',
                            'operation_id','payload','nonce','observed_at_utc','mac'},'桥请求')
            core={name:item for name,item in value.items() if name!='mac'}
            _require(value['schema_version']==RPC_SCHEMA and value['task']==self.task
                     and isinstance(value['mac'],str) and hmac.compare_digest(value['mac'],capture._mac(self.installation.key,core)),
                     '桥请求没有认证完整原task')
            at=capture.source.c.timestamp(value['observed_at_utc']); now=datetime.now(timezone.utc)
            _require(0<=(now-at).total_seconds()<=30,'桥请求时效已过')
            nonce=value['nonce']
            _require(isinstance(nonce,str) and len(nonce)==48
                     and all(ch in '0123456789abcdef' for ch in nonce) and nonce not in self._nonces
                     and len(self._nonces)<65536,'桥nonce重放或无界')
            self._nonces.add(nonce)
            operation=value['operation']; payload=value['payload']; identifier=value['operation_id']
            _require(operation in _STAGES and isinstance(identifier,str) and capture.source.c.ID_RE.fullmatch(identifier),
                     '桥操作或固定ID非法')
            fields={'prepare_state':{'recipe_base64','dispatch'},'start_monitoring':{'dispatch'},
                    'recheck_dispatch':{'dispatch'},'launch_desktop':{'recipe','dispatch'},
                    'launch_vscode':{'recipe','dispatch'},'drive_scenario':{'scenario','dispatch'},
                    'stop_product':set(),'restore_state':set(),'seal_sources':{'status'},'recover':set(),
                    'export':{'export_id','path','offset','limit','prefix_bytes'}}
            c.exact(payload,fields[operation],'桥固定阶段参数')
            if operation=='drive_scenario':
                _require(payload['scenario'] in self.task['scenarios']
                         and identifier=='scenario-'+payload['scenario']['id'],'未知场景或替换操作ID')
            elif operation not in {'export','recover','recheck_dispatch'}:
                _require(identifier==operation,'副作用阶段不能通过另一个ID重复派发')
            logical=_absolute(value['logical_bundle_root'])
            _require(logical.is_relative_to(_absolute(self.installation.value['logical_data_root'])),'ARM64来源根越过固定数据根')
            fresh=operation in {'prepare_state','start_monitoring','recheck_dispatch','launch_desktop','launch_vscode','drive_scenario'}
            if fresh:
                # 先只读验证原预算镜像，副作用意图之后才落镜像或装配产品。
                capture._verify_dispatch_envelope(self.task,payload['dispatch'],self.installation.key)
            if operation=='prepare_state':
                raw=base64.b64decode(payload['recipe_base64'],validate=True)
                ref=self.task['execution']['isolation_recipe']
                _require(len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'],'桥原recipe字节漂移')
                # nonce 父承载会话与 sibling journal；先独立创建并核验，不能沿用宽权限父目录。
                c.private_directory(self.root.parent)
                c.private_directory(self.control)
            else:
                _require(self.control.is_dir(),'桥没有原准备状态，不能补造恢复或库存')
                context=_mapping_context(self.task,self.root,self.installation)
                _require(context.logical_bundle_root==logical,'桥阶段替换原逻辑来源根')
            if operation=='export':
                # 纯导出不装配provider、不读取产品现场，也不要求旧预算仍在时限内。
                return self._chunk(payload)
            if operation=='recover' and (self.control/'sealed-export.json').exists():
                return {'status':'committed','export':self._export()}
            operation_core={'task_sha256':self.task['task_sha256'],'logical_bundle_root':str(logical),
                            'operation':operation,'operation_id':identifier,
                            'payload':{name:item for name,item in payload.items() if name!='dispatch'}}
            intent=self.control/(identifier+'.intent.json'); response=self.control/(identifier+'.result.json')
            if operation not in {'recover','recheck_dispatch'}:
                if response.exists():
                    result=c.read_json(response)
                    _require(c.read_json(intent)==result['intent']==operation_core,'旧桥阶段参数或意图漂移')
                    return result['value']
                _require(not intent.exists(),'Mac桥阶段pending，只能显式恢复；禁止自动再派发产品')
                c.write_json_once(intent,operation_core)
            if operation=='prepare_state':
                # 意图已经持久化；后续任一步失败都会保留pending，不补造新producer。
                c.private_directory(self.root); c.private_directory(self.session)
                target=c.managed_path(self.root,ref['path']); c.private_directory(target.parent); c.write_once(target,raw)
                self._stage_materials(); self._mapping(logical,ref)
                dispatch=capture._verify_dispatch_envelope(self.task,payload['dispatch'],self.installation.key,bundle_root=self.root)
                c.write_json_once(self.control/'original-dispatch.json',dict(dispatch.envelope_binding))
            else:
                dispatch=(capture._verify_dispatch_envelope(self.task,payload['dispatch'],self.installation.key,bundle_root=self.root)
                          if fresh else self._historical_dispatch())
            self._recorder(dispatch,fresh=fresh)
            if operation=='recheck_dispatch':
                self.recorder.recheck_dispatch(self.task); return {'status':'passed'}
            if operation=='recover':
                result=self.recorder.recover(self.task,self.root)
                return {**result,'export':self._export() if (self.control/'sealed-export.json').exists() else None}
            if operation=='prepare_state':
                self.recorder.prepare_state(self.task,self.root); result={'mapping':c.read_json(self.root/_MAP_NAME)}
            elif operation=='start_monitoring':
                self.recorder.start_monitoring(self.task,self.root); result={'status':'ready'}
            elif operation in {'launch_desktop','launch_vscode'}:
                verify_bridge_isolation_recipe(self.task,payload['recipe'],self.root)
                if operation=='launch_desktop':
                    _require(self.task['client_id']=='claude-desktop','不能跨客户端launch'); self.recorder.launch_desktop(self.task,payload['recipe'])
                else:
                    _require(self.task['client_id']=='claude-vscode','不能跨客户端launch'); self.recorder.launch_vscode(self.task,payload['recipe'])
                result={'status':'started'}
            elif operation=='drive_scenario':
                self.recorder.drive_scenario(self.task,payload['scenario']); result={'status':'driven'}
            elif operation=='stop_product':
                self.recorder.stop_product(self.task); result={'status':'stopped'}
            elif operation=='restore_state':
                self.recorder.restore_state(self.task,self.root); result={'status':'restored'}
            elif operation=='seal_sources':
                _require(payload['status'] in {'complete','failed','truncated'},'未知预封存终态')
                result=self._sealed(payload['status'])
            else:
                raise MacBridgeError('未实现的固定阶段')
            c.write_json_once(response,{'intent':operation_core,'value':result})
            return result


@contextmanager
def _claim_agent(engine):
    """每个原任务只允许一个Mac服务实例；重启后原pending阶段仍保留。"""
    c.private_directory(engine.root.parent)
    c.private_directory(engine.control)
    descriptor=os.open(engine.control/'agent-owner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1
                 and stat.S_IMODE(info.st_mode)==0o600,'Mac桥所有权锁类型或权限非法')
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            raise MacBridgeError('原Mac任务已有桥服务持锁，禁止重复实例') from error
        from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import process_identity
        core={'task_sha256':engine.task['task_sha256'],'installation_sha256':engine.installation.sha256,
              'process':process_identity(os.getpid())}
        c.write_json_once(engine.control/'service-instances'/(c.digest(core)+'.json'),core)
        yield
    finally:
        os.close(descriptor)


def serve_mac_agent(task, stop_event: threading.Event | None = None):
    """固定mTLS监听只接闭集阶段；此入口不提供shell、任意目标或动态handler。"""
    installation=require_mac_agent_installation(task)
    engine=MacBridgeEngine(task,installation); stop_event=stop_event or threading.Event()
    endpoint=installation.value['endpoint']
    context=_tls_context(installation,server=True)
    with _claim_agent(engine), socket.socket(socket.AF_INET6 if ':' in endpoint['host'] else socket.AF_INET,socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind((endpoint['host'],endpoint['port'])); listener.listen(8); listener.settimeout(0.2)
        while not stop_event.is_set():
            try:
                raw,_address=listener.accept()
            except socket.timeout:
                continue
            with raw:
                try:
                    with context.wrap_socket(raw,server_side=True) as connection:
                        connection.settimeout(10); _peer(connection,installation)
                        request=_read_frame(connection)
                        try:
                            value=engine.handle(request); ok=True
                        except Exception:
                            value={'status':'pending','reason':'固定阶段失败，保留原件与占用，显式恢复'}; ok=False
                        reply={'schema_version':RPC_SCHEMA,'task_sha256':task['task_sha256'],
                               'operation_id':request.get('operation_id'),'nonce':request.get('nonce'),'ok':ok,'value':value}
                        connection.sendall(c.canonical({**reply,'mac':capture._mac(installation.key,reply)})+b'\n')
                except (OSError,ValueError,ssl.SSLError,c.CaptureError):
                    # 来源认证失败不调用产品、不覆盖原阶段，也不暴露请求或凭据。
                    continue


def main():
    """受管服务固定入口；只接受完整任务文件，操作由认证闭集RPC给出。"""
    parser=argparse.ArgumentParser()
    parser.add_argument('--task',type=Path,required=True)
    args=parser.parse_args()
    task=capture._formal_task(c.read_json(args.task))
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda _number,_frame:stop.set())
    signal.signal(signal.SIGINT,lambda _number,_frame:stop.set())
    serve_mac_agent(task,stop)
    return 0


if __name__=='__main__':
    sys.exit(main())
