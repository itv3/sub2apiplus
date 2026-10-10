"""正式ARM64独立捕获薄壳及固定句柄，不重标旧fixture类。

安装与完整原任务预检只读。只有原总账hold已经持久生效，才复制已验受管
材料、启动固定独立daemon和监听；失联不自动重发产品、预约或续开producer。
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import hmac
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_formal_capture_contract as policy
from tools.official_client_capture.claude_mac_capture_engine_core import RawCaptureCore, receive, serve

_TOKEN = object()


def _parents(root: Path, target: Path) -> None:
    c.require(target.is_relative_to(root), '正式材料目录越出固定数据根')
    current = c.private_directory(root)
    for name in target.relative_to(root).parts:
        current = c.private_directory(current/name)


def _stage_materials(authority: policy.CaptureInstallation, configuration: Mapping[str, Any]) -> None:
    """只在原hold已验证后执行；不从task或上传包下载/安装依赖。"""
    original = authority.value['configuration']
    root = Path(configuration['data_root'])
    task = policy.verify_task_configuration(configuration)
    prefix = root/'runtime'/'formal-capture'/task['task_sha256']/'materials'
    references = [original['runtime_environment_binding'],original['channel_key_binding']]
    for endpoint in (original['listener'],original['upstream']):
        if endpoint['tls']:
            references.extend(ref for ref in endpoint['tls'].values() if isinstance(ref,dict))
    for ref in references:
        target = c.managed_path(prefix,ref['path']); _parents(root,target.parent)
        c.write_once(target,policy.trusted_binding(policy.INSTALL_ROOT,ref))
    dependency = original['h2_dependencies']
    bundle = policy.FormalDecoderDependencies(policy.INSTALL_ROOT/dependency['root_relative'],
                                               dependency['manifest'],artifact_only=True)
    for ref in bundle.read_set:
        target = c.managed_path(prefix/dependency['root_relative'],ref['path']); _parents(root,target.parent)
        c.write_once(target,policy.trusted_bytes(c.managed_path(bundle.root,ref['path'])))


class _FormalCaptureEngine(RawCaptureCore):
    """固定正式策略独立实例；不继承旧CaptureEngine或CaptureClient。"""
    _policy = policy

    def _attest(self,phase: str) -> None:
        names=['reservation.json','budget-reservation.json','daemon-owner.json']
        if (self.root/'ready.json').exists():
            names += ['ready.json','ready-observation.json','ready-environment.json']
        if phase=='committed':
            names += ['native-capture-manifest.json','export-inventory.json','export-commit.json']
        task_path=c.check_binding(self.data_root,self.configuration['task_binding'])
        attempt=task_path.parents[4]
        dispatch=attempt/'client-evidence'/self.task['client_id']/'runs'/self.task['task_nonce']/'dispatch-intent.json'
        undispatched=(phase=='committed' and not dispatch.exists() and not dispatch.is_symlink()
                      and not c.read_bytes(self.root/'native-requests.jsonl')
                      and not c.read_bytes(self.root/'native-connections.jsonl'))
        core={'schema_version':policy.ATTEST_SCHEMA,'task_sha256':self.task_sha,
              'capture_id':self.task['execution']['capture_id'],'producer_run_id':self.task['execution']['producer_run_id'],
              'deployment':self.configuration['deployment'],'installation_sha256':self.configuration['installation_sha256'],
              'phase':phase,'dispatch_state':'not_dispatched' if undispatched else 'may_be_dispatched',
              'originals':[c.binding(self.root,self.root/name) for name in names]}
        path=self.root/('capture-origin-'+phase+'.json')
        if path.exists():
            previous=c.read_json(path)
            c.require(all(previous[name]==value for name,value in core.items()),'原捕获来源认证内容漂移')
        else:
            c.write_json_once(path,{**core,'mac':policy._mac(self.key,core)})
        if undispatched:
            c.write_json_once(self.root/'undispatched.json',{'schema_version':'claude-upgrade-client-undispatched/v2',
               'task_sha256':self.task_sha,'producer_run_id':self.task['execution']['producer_run_id'],
               'capture_id':self.task['execution']['capture_id'],'status':'undispatched','request_count':0,
               'requests':c.binding(self.root,self.root/'native-requests.jsonl'),'attestation':c.binding(self.root,path)})

    def reserve(self,task,hold):
        value=super().reserve(task,hold); self._attest('reserved'); return value

    def wait_ready(self,task_sha256):
        value=super().wait_ready(task_sha256); self._attest('ready'); return value

    def close(self,task_sha256,reason='normal'):
        value=super().close(task_sha256,reason); self._attest('committed'); self.stop_event.set(); return value

    def recover(self,task_sha256):
        value=super().recover(task_sha256); self._attest('committed'); self.stop_event.set(); return value


class FormalCaptureHandle:
    """构造只读；所有RPC只去原受管daemon，不接任意命令或程序路径。"""
    source_scope='formal'

    def __init__(self,task,*,attempt_root,services,authority,_token):
        c.require(_token is _TOKEN and type(authority) is policy.CaptureInstallation,'正式句柄须由固定工厂构造')
        self._task=copy.deepcopy(policy._formal_task(task)); self._attempt=c.plain_path(Path(attempt_root))
        self._services=services; self._authority=authority; self._token=_token
        self.configuration=authority.configuration(self._task,self._attempt)
        self.task_sha256=self._task['task_sha256']; self.capture_id=self._task['execution']['capture_id']
        self.producer_run_id=self._task['execution']['producer_run_id']; self.capture_root=Path(self._task['execution']['capture_root'])
        self.deployment=copy.deepcopy(self._task['execution']['source_deployments']['capture'])
        self._process=None

    def _guard(self,task):
        c.require(type(self) is FormalCaptureHandle and self._token is _TOKEN
                  and policy._formal_task(task)==self._task,'完整正式task或句柄所有权不符')
        self._authority.revalidate(self._task)
        c.require(self.configuration==self._authority.configuration(self._task,self._attempt),'原正式配置发生变化')

    @property
    def _socket(self):
        return c.managed_path(Path(self.configuration['data_root']),self.configuration['control_socket_relative'])

    def _call(self,action,payload):
        self._guard(self._task)
        path=self._socket; info=path.lstat()
        c.require(stat.S_ISSOCK(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,
                  '正式daemon socket类型/所有者/权限非法')
        key=policy.trusted_binding(policy.INSTALL_ROOT,self._authority.value['configuration']['channel_key_binding'])
        nonce=secrets.token_hex(24)
        core={'protocol':policy.contract_versions(self.configuration)['rpc'],'nonce':nonce,'action':action,'payload':payload}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(30); connection.connect(str(path))
            connection.sendall(c.canonical({**core,'mac':policy._mac(key,core)})+b'\n')
            reply=c.exact(c.parse_json(receive(connection)),{'protocol','nonce','body','mac'},'正式daemon响应')
        body={name:reply[name] for name in ('protocol','nonce','body')}
        c.require(reply['protocol']==core['protocol'] and reply['nonce']==nonce and isinstance(reply['mac'],str)
                  and hmac.compare_digest(reply['mac'],policy._mac(key,body)),'正式daemon响应认证失败')
        c.exact(reply['body'],{'ok','value'},'正式daemon返回体')
        c.require(reply['body']['ok'] is True,str(reply['body']['value']))
        c.require(isinstance(reply['body']['value'],dict),'正式daemon返回必须是对象')
        return reply['body']['value']

    def reserve(self,task,hold):
        self._guard(task); policy.verify_budget(self._task,hold)
        if not self._socket.exists():
            control=self._socket.parent; root=Path(self.configuration['data_root'])
            _parents(root,control)
            config_path=control/'configuration.json'; hold_path=control/'hold.json'
            intent=control/'service-start-intent.json'
            c.require(not intent.exists(),'旧独立daemon启动状态未决，只可显式恢复，不能自动重新创建producer')
            c.write_json_once(config_path,self.configuration); c.write_json_once(hold_path,dict(hold))
            c.write_json_once(intent,{'task_sha256':self.task_sha256,'configuration':c.binding(control,config_path),
                    'budget_reservation':c.binding(control,hold_path),'module':__name__})
            _stage_materials(self._authority,self.configuration)
            repository=policy.INSTALL_ROOT/'code'
            log=os.open(control/'daemon-private.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            try:
                info=os.fstat(log)
                c.require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1
                          and stat.S_IMODE(info.st_mode)==0o600,'正式daemon诊断文件实例或权限非法')
                self._process=subprocess.Popen([str(Path(sys.executable).resolve()),'-B','-S','-m',__name__,
                              '--managed-daemon',str(config_path),'--hold',str(hold_path)],cwd=repository,
                             env={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(repository)},stdin=subprocess.DEVNULL,
                             stdout=log,stderr=log,close_fds=True,start_new_session=True)
            finally:
                os.close(log)
            from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import process_identity
            c.write_json_once(control/'service-process.json',process_identity(self._process.pid))
            until=time.monotonic()+min(15,self.configuration['limits']['socket_timeout_seconds']+5)
            while not self._socket.exists() and time.monotonic()<until:
                c.require(self._process.poll() is None,'正式独立daemon初始化失败，原hold保留未决')
                time.sleep(0.02)
            c.require(self._socket.exists(),'正式独立daemon未在有界时间内就绪，原hold保留未决')
        result=self._call('reserve',{'task':self._task,'hold':dict(hold)})
        policy.verify_formal_capture_origin(self._task,self.capture_root)
        return result

    def wait_ready(self,task):
        self._guard(task)
        result=self._call('ready',{'task_sha256':self.task_sha256})
        policy.verify_formal_capture_origin(self._task,self.capture_root)
        return result

    def close_and_sync(self,task):
        self._guard(task)
        if (self.capture_root/'capture-origin-committed.json').exists():
            inventory=c.read_json(self.capture_root/'export-inventory.json')
        else:
            inventory=self._call('shutdown',{'task_sha256':self.task_sha256,'reason':'normal'})
            self._wait_daemon_exit()
        policy.verify_export(self.capture_root,inventory,task_sha256=self.task_sha256)
        policy.verify_formal_capture_origin(self._task,self.capture_root)
        return {'status':'committed','export_id':inventory['export_id'],'root':str(self.capture_root)}

    def recover(self,task):
        self._guard(task)
        if (self.capture_root/'capture-origin-committed.json').exists():
            # 已关闭原件纯读重验，不要求daemon活着，也不重复预约或监听。
            inventory=c.read_json(self.capture_root/'export-inventory.json')
        elif self._socket.exists():
            inventory=self._call('shutdown',{'task_sha256':self.task_sha256,'reason':'recover'})
            self._wait_daemon_exit()
        else:
            from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import owner_alive
            control=self._socket.parent
            owner_path=control/'service-process.json'
            c.require(owner_path.exists(),'旧daemon启动状态缺准确进程实例，不能猜测消失后重开')
            c.require(not owner_alive(c.read_json(owner_path)),'旧daemon仍存活，禁止启动另一个producer')
            reservation=policy.source.verify_reservation_body_v2(self._task,self.capture_root)
            config=control/'configuration.json'; hold=control/'hold.json'
            c.require(c.read_json(config)==self.configuration,'恢复配置不再是原冻结实例')
            original=c.read_json(hold); policy.verify_budget(self._task,original,require_live=False)
            c.require(c.read_json(c.check_binding(self.capture_root,reservation['budget_reservation']))==original,
                      '恢复必须沿同预算原件，不能补开hold')
            repository=policy.INSTALL_ROOT/'code'
            result=subprocess.run([str(Path(sys.executable).resolve()),'-B','-S','-m',__name__,
                  '--managed-daemon',str(config),'--hold',str(hold),'--recover'],cwd=repository,
                  env={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(repository)},
                  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,check=False)
            c.require(result.returncode==0,'原daemon恢复只收口失败；预算和原件继续保留未决')
            inventory=c.read_json(self.capture_root/'export-inventory.json')
        policy.verify_export(self.capture_root,inventory,task_sha256=self.task_sha256)
        policy.verify_formal_capture_origin(self._task,self.capture_root)
        return {'status':'committed','export_id':inventory['export_id'],'root':str(self.capture_root)}

    def _wait_daemon_exit(self):
        """收到终态后等待原实例释放锁；已有离线终态不走这个等待。"""
        from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import owner_alive
        owner=c.read_json(self._socket.parent/'service-process.json')
        until=time.monotonic()+5
        while owner_alive(owner) and time.monotonic()<until:
            if self._process is not None and self._process.poll() is not None:
                break
            time.sleep(0.02)
        c.require(not owner_alive(owner),'原daemon终态已提交但实例尚未退出，保留原件待只读复核')

    def dispatch_envelope(self,task,*,channel_key):
        """原锁内重新核原预算及实时ready后形成15秒镜像，不另建lease。"""
        self._guard(task)
        self._services.recheck_dispatch(self._task)
        from tools.official_client_capture import claude_upgrade_mac_budget as budget
        receipt=budget.read_task_bound_receipt(Path(self._task['execution']['project_ledger']),self._task)
        policy.verify_budget(self._task,receipt)
        reservation,ready=policy.source.verify_reservation_v2(self._task,self.capture_root)
        core={'schema_version':policy.DISPATCH_SCHEMA,'task':self._task,'task_sha256':self.task_sha256,
              'task_nonce':self._task['task_nonce'],'capture_deployment':self.deployment,
              'mac_deployment':self._task['execution']['source_deployments']['mac'],
              'deadline_utc':self._task['execution']['request_policy']['deadline_utc'],'observed_at_utc':c.utc_now(),
              'ready':ready,'reservation':reservation,'budget_reservation':receipt}
        return {**core,'mac':policy._mac(channel_key,core)}

    def finalize_product_capture(self,task,bundle_root):
        """已关闭独立捕获与真实Mac原件联合后，才发布v2高层清单。"""
        self._guard(task)
        origin=policy.verify_formal_capture_origin(self._task,self.capture_root)
        c.require(origin['phase']=='committed','联合导出前独立捕获尚未关闭')
        inventory=c.read_json(self.capture_root/'export-inventory.json')
        native=policy.verify_export(self.capture_root,inventory,task_sha256=self.task_sha256)
        from tools.official_client_capture.claude_mac_agent_bridge import materialize_transport_map
        joint=materialize_transport_map(self._task,capture_root=self.capture_root,product_root=Path(bundle_root),native_manifest=native)
        c.write_json_once(self.capture_root/'source-origin.json',{
            'task_sha256':self.task_sha256,'deployment':self.deployment,
            'attestation':c.binding(self.capture_root,self.capture_root/'capture-origin-committed.json')})
        manifest={'schema_version':policy.source.CAPTURE_SCHEMA,'task_sha256':self.task_sha256,
              'capture_id':self.capture_id,'producer_run_id':self.producer_run_id,'candidate':self._task['candidate'],
              'endpoint':self._task['execution']['endpoint'],'status':native['status'],
              'requests':joint['requests'],'connections':joint['connections'],'transport_map':joint['transport_map'],
              'reservation':c.binding(self.capture_root,self.capture_root/'reservation.json'),
              'ready':c.binding(self.capture_root,self.capture_root/'ready.json'),'source_origin':c.binding(self.capture_root,
                  self.capture_root/'source-origin.json')}
        c.write_json_once(self.capture_root/'capture-manifest.json',manifest)
        return manifest


def create_formal_capture(task,*,attempt_root,services):
    authority=policy.require_formal_capture_installation(task)
    return FormalCaptureHandle(task,attempt_root=attempt_root,services=services,authority=authority,_token=_TOKEN)


def owns_formal_capture(handle,task=None,attempt_root=None):
    if type(handle) is not FormalCaptureHandle or handle._token is not _TOKEN:
        return False
    return (task is None or handle._task==task) and (attempt_root is None or handle._attempt==Path(attempt_root))


def verify_formal_capture_handle(task,handle,attempt_root):
    c.require(owns_formal_capture(handle,task,attempt_root),'正式捕获句柄不是同task的原工厂实例')
    handle._guard(task)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--managed-daemon',type=Path,required=True)
    parser.add_argument('--hold',type=Path,required=True); parser.add_argument('--recover',action='store_true')
    args=parser.parse_args()
    configuration=policy.validate_configuration(c.read_json(args.managed_daemon))
    task=policy.verify_task_configuration(configuration)
    hold=c.read_json(args.hold); policy.verify_budget(task,hold,require_live=not args.recover)
    if args.recover:
        c.require((Path(task['execution']['capture_root'])/'reservation.json').exists(),
                  '恢复没有原预约，不能补造零捕获')
    engine=_FormalCaptureEngine(configuration)
    def request_stop(_number,_frame):
        if (engine.root/'reservation.json').exists():
            engine.close(engine.task_sha,'shutdown')
        engine.stop_event.set()
    signal.signal(signal.SIGTERM,request_stop); signal.signal(signal.SIGINT,request_stop)
    try:
        if args.recover:
            engine.recover(task['task_sha256'])
        else:
            engine.reserve(task,hold)
            serve(engine)
    finally:
        engine.dispose()
    return 0

if __name__=='__main__':
    sys.exit(main())
