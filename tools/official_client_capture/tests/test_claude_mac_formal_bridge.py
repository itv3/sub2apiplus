"""正式后端的无账号边界短验；私有夹具不授来源资格或产品启动权限。

路径／派发／WAL与分块运行真实合同。安装信任和产品副作用仅在各测试的
显式替身作用域内提供；这些成绩不能登记正式账号、签名或现场材料资格。
"""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import http.server
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_formal_capture_contract as capture
from tools.official_client_capture import claude_mac_formal_capture_service as service
from tools.official_client_capture import claude_mac_agent_bridge as bridge
from tools.official_client_capture import claude_upgrade_mac_budget as budget
from tools.official_client_capture.tests import claude_client_receipt_fixture as f


class FormalBridgeTests(unittest.TestCase):
    def setUp(self):
        outer=tempfile.TemporaryDirectory(); self.addCleanup(outer.cleanup)
        self.outer=Path(outer.name).resolve()
        self.campaign=self.outer/'staging/data/evidence/campaigns/campaign-1'
        self.attempt=self.campaign/'candidates/candidate-1/attempts/attempt-1'
        f.write(self.campaign/'campaign.json',{'campaign_id':'campaign-1','client':'claude'})
        self.task_path,self.bundle,self.capture_root=f.material(self.attempt,'claude-desktop',
                {'campaign_id':'campaign-1','candidate_id':'candidate-1','attempt_id':'attempt-1','run_nonce':'nonce-1'})
        self.task=c.read_json(self.task_path); self.now=datetime.now(timezone.utc)
        self.at=lambda offset:(self.now+timedelta(seconds=offset)).isoformat()
        self.task['issued_at_utc']=self.at(-120)
        self.task['execution'].update(source_scope='formal',recorder_id=capture.RECORDER_ID)
        self.task['execution']['request_policy']['deadline_utc']=self.at(600)
        self.task['task_sha256']=c.digest({k:v for k,v in self.task.items() if k!='task_sha256'})
        f.write(self.task_path,self.task)
        self.key=b'fixture-bridge-authentication-key-32-bytes'
        self.physical=c.private_directory(self.outer/'user-state')
        self.state=c.read_json(c.check_binding(self.bundle,self.task['execution']['isolation_recipe']))['state_paths']
        self.mapping_rows=[{'logical':name,'physical':str(self.physical/(str(index)+'.json'))}
                           for index,name in enumerate(self.state)]
        self.capture_install=self.installation('capture')
        self.mac_install=self.installation('mac')
        self.engine=bridge.MacBridgeEngine(self.task,self.mac_install)
        self.install_guard=mock.patch.object(bridge.BridgeInstallation,'revalidate',return_value=None)
        self.install_guard.start(); self.addCleanup(self.install_guard.stop)

    def installation(self,side):
        value={'side':side,'staging_root':str(self.outer/'mac-staging'),
               'logical_data_root':str(self.outer/'staging/data'),
               'state_paths':{'claude-desktop':self.mapping_rows,'claude-vscode':self.mapping_rows},
               'materials_relative':'materials','endpoint':{'host':'127.0.0.1','port':1}}
        return bridge.BridgeInstallation(c.canonical(value),self.outer/'fixture-install','a'*64,self.key,bridge._TOKEN)

    def envelope(self,*,observed=None):
        bound={'schema_version':budget.BOUND_SCHEMA,'client':'claude',
               **{name:self.task[name] for name in ('campaign_id','candidate_id','attempt_id','run_nonce','candidate_revision','task_sha256')},
               **{name:self.task['execution'][name] for name in ('capture_id','producer_run_id','request_budget')},
               'deadline_utc':self.task['execution']['request_policy']['deadline_utc'],
               'task_binding':c.binding(self.outer,self.task_path),
               'attempt_reservation_binding':{'path':'attempt-reservation.json','sha256':'b'*64,'bytes':1}}
        receipt={'schema_version':budget.RECEIPT_SCHEMA,'project_ledger':self.task['execution']['project_ledger'],
                 'bound':bound,'reservation_event':{'operation_id':budget.reservation_operation_id(self.task['task_sha256']),
                 'event_sha256':'c'*64,'sequence':1},'reserved_at_utc':self.at(-4)}
        f.write(self.capture_root/'budget-reservation.json',receipt)
        reservation={'schema_version':capture.source.RESERVATION_SCHEMA,'task_sha256':self.task['task_sha256'],
                     'candidate':self.task['candidate'],
                     **{name:self.task['execution'][name] for name in ('capture_id','producer_run_id','endpoint','request_budget','source_deployments')},
                     'reserved_at_utc':self.at(-3),'deadline_utc':bound['deadline_utc'],
                     'budget_reservation':c.binding(self.capture_root,self.capture_root/'budget-reservation.json')}
        f.write(self.capture_root/'reservation.json',reservation)
        ready={'schema_version':capture.source.READY_SCHEMA,'task_sha256':self.task['task_sha256'],
               'capture_id':self.task['execution']['capture_id'],
               'reservation':c.binding(self.capture_root,self.capture_root/'reservation.json'),
               'deployment':self.task['execution']['source_deployments']['capture'],'ready_at_utc':self.at(-2)}
        f.write(self.capture_root/'ready-observation.json',ready)
        ready['observation']=c.binding(self.capture_root,self.capture_root/'ready-observation.json')
        core={'schema_version':capture.DISPATCH_SCHEMA,'task':self.task,'task_sha256':self.task['task_sha256'],
              'task_nonce':self.task['task_nonce'],'capture_deployment':self.task['execution']['source_deployments']['capture'],
              'mac_deployment':self.task['execution']['source_deployments']['mac'],'deadline_utc':bound['deadline_utc'],
              'observed_at_utc':observed or c.utc_now(),'ready':ready,'reservation':reservation,'budget_reservation':receipt}
        return copy.deepcopy({**core,'mac':capture._mac(self.key,core)})

    def request(self,operation,payload,*,identifier=None):
        core={'schema_version':bridge.RPC_SCHEMA,'task':self.task,'logical_bundle_root':str(self.bundle),
              'operation':operation,'operation_id':identifier or operation,'payload':payload,
              'nonce':secrets.token_hex(24),'observed_at_utc':c.utc_now()}
        return {**core,'mac':capture._mac(self.key,core)}

    def prepared(self):
        c.private_directory(self.engine.control); c.private_directory(self.engine.root)
        ref=self.task['execution']['isolation_recipe']
        c.write_once(c.managed_path(self.engine.root,ref['path']),c.read_bytes(c.check_binding(self.bundle,ref)))
        self.engine._mapping(self.bundle,ref)
        c.write_json_once(self.bundle/bridge._MAP_NAME,c.read_json(self.engine.root/bridge._MAP_NAME))

    def export(self,raw=b'x'*150000):
        self.prepared(); c.write_once(self.engine.root/'wire.bin',raw)
        c.write_json_once(self.engine.root/'product-preseal.json',{'fixture_only':True})
        files=[c.binding(self.engine.root,path) for path in sorted(self.engine.root.rglob('*')) if path.is_file()]
        core={'schema_version':bridge.EXPORT_SCHEMA,'task_sha256':self.task['task_sha256'],
              'capture_deployment':self.task['execution']['source_deployments']['capture'],
              'mac_deployment':self.task['execution']['source_deployments']['mac'],
              'mapping':c.binding(self.engine.root,self.engine.root/bridge._MAP_NAME),
              'preseal':c.binding(self.engine.root,self.engine.root/'product-preseal.json'),'files':files}
        value={**core,'export_id':c.digest(core)}; value['mac']=capture._mac(self.key,value)
        c.write_json_once(self.engine.control/'sealed-export.json',value)
        return value

    def test_dispatch_handle_returns_deep_copies_of_authenticated_bytes(self):
        envelope=self.envelope()
        handle=capture._verify_dispatch_envelope(self.task,envelope,self.key,bundle_root=self.bundle)
        facts=handle.facts; facts['ready']['deployment']['deployment_id']='forged'; facts['task']['client_id']='other'
        envelope['ready']['deployment']['deployment_id']='changed'
        reference=handle.envelope_binding; reference['sha256']='d'*64
        actual=capture.verify_capture_dispatch(self.task,handle)
        self.assertEqual(actual['capture_deployment'],self.task['execution']['source_deployments']['capture'])
        self.assertNotEqual(handle.envelope_binding['sha256'],'d'*64)

    def test_ordinary_dispatch_dict_and_external_type_have_no_capability(self):
        for value in (self.envelope(),mock.Mock(facts=self.envelope(),_token=capture._DISPATCH_TOKEN)):
            with self.subTest(value=type(value).__name__),self.assertRaises(capture.FormalCaptureError):
                capture.verify_capture_dispatch(self.task,value)

    def test_copy_or_dataclass_replace_cannot_forge_authenticated_handle(self):
        handle=capture._verify_dispatch_envelope(self.task,self.envelope(),self.key)
        value=handle.facts; value['ready']['deployment']['deployment_id']='forged'
        forged=replace(handle,_facts_bytes=c.canonical(value))
        for value in (forged,replace(handle),copy.copy(handle)):
            with self.subTest(value=id(value)),self.assertRaises(capture.FormalCaptureError):
                capture.verify_capture_dispatch(self.task,value)

    def expired_original_dispatch(self):
        """构造历史夹具原件；不登记安装、账号或产品资格。"""
        self.task['execution']['request_policy']['deadline_utc']=self.at(-10)
        self.task['task_sha256']=c.digest({name:value for name,value in self.task.items() if name!='task_sha256'})
        self.now-=timedelta(seconds=60)
        self.engine=bridge.MacBridgeEngine(self.task,self.mac_install)
        self.prepared(); c.private_directory(self.engine.control)
        envelope=self.envelope(observed=self.at(0))
        original=capture._verify_dispatch_envelope(self.task,envelope,self.key,
                    require_live=False,bundle_root=self.engine.root)
        c.write_json_once(self.engine.control/'original-dispatch.json',original.envelope_binding)
        return envelope,original

    def test_cold_dispatch_keeps_original_binding_through_product_origin_and_preseal(self):
        """原生资格/产品只替身化；实际镜像、来源读写和冷预封入口保持原绑定。"""
        from tools.official_client_capture import claude_mac_product_source as product
        from tools.official_client_capture import claude_mac_product_completion as completion
        from tools.official_client_capture import claude_mac_product_recorder as recorder
        from tools.official_client_capture.claude_mac_state_journal import SnapshotLimits
        from tools.official_client_capture.tests.test_claude_mac_product_sources import process
        envelope,original=self.expired_original_dispatch()
        root=self.engine.root; context=bridge._mapping_context(self.task,root,self.mac_install)
        provider=SimpleNamespace(_root=root)
        qualified=product.QualifiedMacProductSource()
        services=SimpleNamespace(_dispatch=original)
        qualified._owners[id(provider)]={'provider':provider,'task':self.task,'root':root,
                    'services':services,'paths':context,'qualification':{'raw_sha256':'f'*64}}
        artifacts={row['role']:row for row in self.task['client_artifacts']}
        histories={}
        for pid,role,parent in ((100,'application_executable',99),(101,'embedded_cli',100)):
            instance=process(pid,artifacts[role]['content']['path'],parent)
            histories[tuple(instance['audit_token'])]={
                'start':{'event':'exec','process':instance,'observed_at_utc':self.at(pid-100)},
                'end':{'observed_at_utc':self.at(6)},'end_reason':'exit'}
        observed={'task_sha256':self.task['task_sha256'],'native':{'frames':[], 'lifecycles':histories},
                  'network_flows':[],'session_id':'fixture-cold-binding','scenarios':[]}
        for name in ('product-native-attestation.json','network-attestation.json','product-materials.json'):
            c.write_json_once(root/name,{'fixture_only':True,'formal_qualification':False})
        c.write_json_once(self.engine.control/'materials-receipt.json',c.binding(root,root/'product-materials.json'))
        with mock.patch.object(qualified,'_originals',return_value=observed):
            inventory=qualified.export_product_observations(self.task,provider,[],{})
            attestation=c.read_json(root/'product-origin-attestation.json')
            self.assertEqual(attestation['dispatch'],original.envelope_binding)
            files=[c.binding(root,path) for path in sorted(root.rglob('*')) if path.is_file()]
            c.write_json_once(root/'product-preseal.json',{'schema_version':'claude-mac-product-preseal/v1',
                'task_sha256':self.task['task_sha256'],'task_nonce':self.task['task_nonce'],
                'status':'complete','files':files,'origin':inventory['origin']})
            reader=object.__new__(completion.ProductEvidenceRecorder)
            reader._task,reader._root,reader._root_instance=self.task,root,recorder._directory(root)
            reader._plan=SimpleNamespace(limits=SnapshotLimits())
            reader._adapter,reader._provider,reader._journal=qualified,provider,None
            # 现场工厂/资格只在本测试替身；预封、原件绑定、产品来源消费者执行本体。
            reader._guard=lambda task,bundle_root:self.assertEqual((task,bundle_root),(self.task,root))
            cold=bridge.MacBridgeEngine(self.task,self.mac_install)
            def fixture_factory(task,*,bundle_root,materials_receipt,path_context,dispatch):
                self.assertEqual((task,bundle_root,path_context.mapping_binding),
                                 (self.task,root,context.mapping_binding))
                self.assertEqual(dispatch.envelope_binding,original.envelope_binding)
                services._dispatch=dispatch
                return reader
            before={path.relative_to(root).as_posix():path.read_bytes() for path in root.rglob('*') if path.is_file()}
            with mock.patch.object(product,'create_qualified_source',side_effect=fixture_factory):
                exported=cold.handle(self.request('seal_sources',{'status':'complete'}))
            self.assertEqual(exported['preseal'],c.binding(root,root/'product-preseal.json'))
            self.assertEqual(before,{path.relative_to(root).as_posix():path.read_bytes()
                                     for path in root.rglob('*') if path.is_file()})
            qualified.verify_product_origin(self.task,root,provider,inventory['origin'])
            with self.assertRaisesRegex(capture.FormalCaptureError,'已过'):
                capture.verify_capture_dispatch(self.task,services._dispatch)
            with mock.patch.object(cold,'_recorder') as factory,self.assertRaisesRegex(capture.FormalCaptureError,'过期'):
                cold.handle(self.request('launch_desktop',{'recipe':c.read_json(root/'isolation-recipe.json'),
                                                         'dispatch':envelope}))
            factory.assert_not_called()

    def test_historical_dispatch_alias_and_binding_tamper_do_not_create_new_members(self):
        _,original=self.expired_original_dispatch()
        reference=original.envelope_binding
        alias=self.engine.root/'renamed-dispatch.json'
        c.write_once(alias,c.read_bytes(c.check_binding(self.engine.root,reference)))
        for replacement in (c.binding(self.engine.root,alias),{**reference,'sha256':'0'*64}):
            f.write(self.engine.control/'original-dispatch.json',replacement)
            before={path.relative_to(self.engine.root).as_posix():path.read_bytes()
                    for path in self.engine.root.rglob('*') if path.is_file()}
            with self.subTest(path=replacement['path']),self.assertRaises((bridge.MacBridgeError,c.CaptureError)):
                self.engine._historical_dispatch()
            self.assertEqual(before,{path.relative_to(self.engine.root).as_posix():path.read_bytes()
                                     for path in self.engine.root.rglob('*') if path.is_file()})

    def test_tampered_hmac_or_coordinates_are_rejected(self):
        for field in ('mac','task_sha256','task_nonce','capture_deployment','mac_deployment'):
            value=self.envelope(); value[field]='forged'
            with self.subTest(field=field),self.assertRaises((ValueError,capture.FormalCaptureError)):
                capture._verify_dispatch_envelope(self.task,value,self.key)

    def test_stale_dispatch_does_not_create_source_files(self):
        empty=self.outer/'never-written'
        with self.assertRaisesRegex(capture.FormalCaptureError,'过期'):
            capture._verify_dispatch_envelope(self.task,self.envelope(observed=self.at(-60)),self.key,bundle_root=empty)
        self.assertFalse(empty.exists())

    def test_underspecified_or_wrong_budget_mirror_is_rejected(self):
        for mutate in (lambda v:v['ready'].pop('observation'),
                       lambda v:v['budget_reservation']['bound'].update(capture_id='other'),
                       lambda v:v['reservation'].update(endpoint='http://127.0.0.1:1')):
            value=self.envelope(); mutate(value); value['mac']=capture._mac(self.key,{k:v for k,v in value.items() if k!='mac'})
            with self.subTest(mutate=mutate),self.assertRaises((ValueError,capture.FormalCaptureError)):
                capture._verify_dispatch_envelope(self.task,value,self.key)

    def test_authenticated_path_context_preserves_complete_task(self):
        self.prepared(); before=c.canonical(self.task)
        with mock.patch.object(bridge,'require_mac_bridge_installation',return_value=self.capture_install):
            context=bridge.verify_bridge_path_context(self.task,self.bundle)
            self.assertEqual(bridge.validate_bridge_path_context(self.task,context,self.bundle),self.engine.session)
        self.assertEqual(c.canonical(self.task),before)
        self.assertEqual({a for a,_b in context.state_paths},set(self.state))

    def test_json_or_foreign_context_is_rejected(self):
        for context in ({'task_sha256':self.task['task_sha256']},mock.Mock(_token=bridge._PATH_TOKEN)):
            with self.subTest(context=type(context).__name__),self.assertRaises(bridge.MacBridgeError):
                bridge.validate_bridge_path_context(self.task,context,self.bundle)

    def test_partial_unknown_overlapping_or_replaced_mapping_is_rejected(self):
        self.prepared(); original=c.read_json(self.bundle/bridge._MAP_NAME)
        mutations=(lambda v:v.update(staging_bundle_root=str(self.outer/'other')),
                   lambda v:v.update(state_paths=[]),
                   lambda v:v['state_paths'].append({'logical':'/unknown','physical':str(self.physical/'other')}),
                   lambda v:v['state_paths'][0].update(physical=str(self.engine.root/'overlap')),
                   lambda v:v.update(logical_bundle_root=str(self.outer/'outside')))
        for mutate in mutations:
            value=copy.deepcopy(original); mutate(value); value['mac']=capture._mac(self.key,{k:v for k,v in value.items() if k!='mac'})
            f.write(self.bundle/bridge._MAP_NAME,value)
            with self.subTest(mutate=mutate),self.assertRaises((ValueError,bridge.MacBridgeError)):
                bridge._mapping_context(self.task,self.bundle,self.capture_install)

    def test_missing_installation_refuses_before_budget_process_or_directory(self):
        missing=self.outer/'absent-fixed-install'
        with mock.patch.object(capture,'INSTALL_ROOT',missing),mock.patch.object(bridge,'MAC_INSTALL_ROOT',missing),\
             mock.patch.object(service.subprocess,'Popen') as spawn,\
             mock.patch.object(budget,'verify_bound_receipt') as ledger_read:
            with self.assertRaises((OSError,ValueError,capture.FormalCaptureUnavailable)):
                service.create_formal_capture(self.task,attempt_root=self.attempt,services=object())
            with self.assertRaises((OSError,ValueError,capture.FormalCaptureUnavailable)):
                bridge.create_mac_agent_bridge(self.task,bundle_root=self.bundle,services=object(),capture_handle=object())
        spawn.assert_not_called(); ledger_read.assert_not_called(); self.assertFalse(missing.exists())

    def test_missing_fixed_material_export_refuses_before_staging(self):
        with self.assertRaises((OSError,ValueError)):
            bridge._read_materials_export(self.task,self.outer/'missing-material-install',
                                         {'materials_relative':'materials'})
        self.assertFalse((self.outer/'missing-material-install').exists())

    def test_data_entry_reads_only_identical_fixed_installation_code(self):
        """真实复制并导入data/tools；不mock __file__，仅替身root信任边界。"""
        from tools.official_client_capture.tests import claude_h2_dependency_fixture as dependency
        fixed=c.private_directory(self.outer/'fixture-fixed-install')
        data=self.outer/'staging/data'
        source_root=Path(capture.__file__).parent
        for target in (fixed/'code/tools/official_client_capture',data/'tools/official_client_capture'):
            shutil.copytree(source_root,target,ignore=shutil.ignore_patterns('__pycache__','tests','*.pyc'))
        for path in (fixed/'code').rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        dependency.prepare(fixed/'dependencies')
        manifest_path=fixed/'dependencies'/dependency.PRODUCTION['path']
        manifest=c.read_json(manifest_path)
        manifest.update(schema_version=capture.FormalDecoderDependencies.manifest_schema,source_scope='formal')
        f.write(manifest_path,manifest)
        c.write_once(fixed/'channel.key',self.key)
        c.write_json_once(fixed/'runtime.json',capture.collect_formal_runtime())
        qualification={}
        for kind in ('capture_probe','mac_probe','reviewed_source'):
            path=fixed/(kind+'.json')
            # 这里只替身安装的root信任边界，不能用这些JSON登记正式资格。
            c.write_json_once(path,{'schema_version':'claude-upgrade-source-qualification/v1',
                 'source_scope':'formal','status':'passed','kind':kind,
                 'capture_deployment':self.task['execution']['source_deployments']['capture'],
                 'mac_deployment':self.task['execution']['source_deployments']['mac']})
            qualification[kind]=c.binding(fixed,path)
        original={'data_root':str(data),'listener':{'host':'127.0.0.1','port':12345,'tls':None},
                  'upstream':{'host':'127.0.0.1','port':12346,'tls':None},
                  'limits':{name:32 for name in capture.h2.COMMON_LIMITS|capture.h2.EXTRA_LIMITS},
                  'protocol_policy':{'mode':'cleartext_h1','allow_no_alpn_h1':False},
                  'h2_dependencies':{'root_relative':'dependencies','manifest':c.binding(fixed/'dependencies',manifest_path)},
                  'channel_key_binding':c.binding(fixed,fixed/'channel.key'),
                  'runtime_environment_binding':c.binding(fixed,fixed/'runtime.json')}
        original['limits'].update(max_frame_bytes=16384,max_hpack_table_bytes=4096,
                                 drain_seconds=1,socket_timeout_seconds=1)
        c.write_json_once(fixed/'installation.json',{'schema_version':capture.INSTALL_SCHEMA,
                  'deployment':self.task['execution']['source_deployments']['capture'],
                  'mac_deployment':self.task['execution']['source_deployments']['mac'],
                  'code_root':str(fixed/'code/tools/official_client_capture'),
                  'runtime_code':capture.runtime_bindings(),'qualification':qualification,'configuration':original})
        task_file=self.outer/'data-entry-task.json'; c.write_json_once(task_file,self.task)
        program="""import json,sys
from pathlib import Path
from tools.official_client_capture import claude_mac_formal_capture_contract as p
from tools.official_client_capture import claude_mac_capture_contract as c
p.INSTALL_ROOT=Path(sys.argv[1])
# 明示fixture：只替身root安装读取，不改模块路径，不改代码闭包与部署判断。
p.trusted_bytes=lambda path,**kwargs:c.read_bytes(Path(path))
task=c.read_json(Path(sys.argv[2]))
try:
 value=p.require_formal_capture_installation(task)
 print(json.dumps({'accepted':True,'origin':p.__file__,'identity':value.sha256}))
except Exception as error:
 print(json.dumps({'accepted':False,'error':str(error)}))
"""
        def run(root):
            result=subprocess.run([sys.executable,'-B','-S','-c',program,str(fixed),str(task_file)],
                      cwd=root,env={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(root)},
                      capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        result=run(data)
        self.assertTrue(result['accepted'],result)
        self.assertEqual(Path(result['origin']).parent,data/'tools/official_client_capture')
        changed=data/'tools/official_client_capture/claude_mac_capture_wire.py'
        with changed.open('ab') as handle:handle.write('\n# 夹具漂移\n'.encode())
        self.assertFalse(run(data)['accepted'])
        checkout=c.private_directory(self.outer/'developer-checkout')
        shutil.copytree(source_root,checkout/'tools/official_client_capture',ignore=shutil.ignore_patterns('__pycache__','tests','*.pyc'))
        self.assertFalse(run(checkout)['accepted'])

    def test_formal_byte_core_loopback_and_offline_replay_keep_originals(self):
        """安装与预算明确用fixture替身；网络、WAL、原件和子进程复算是真实路径。"""
        # 该私有根不与其他回归的固定解码根共享导入实例；测试后恢复原导入缓存。
        package_names = {'hpack', 'h2', 'hyperframe'}
        prior_modules = {name: module for name, module in sys.modules.items()
                         if name.split('.', 1)[0] in package_names}
        prior_finders = list(sys.meta_path)
        def restore_decoders():
            for name in tuple(sys.modules):
                if name.split('.', 1)[0] in package_names:
                    sys.modules.pop(name, None)
            sys.modules.update(prior_modules)
            sys.meta_path[:] = prior_finders
        self.addCleanup(restore_decoders)
        for name in prior_modules:
            sys.modules.pop(name, None)
        from tools.official_client_capture.tests import claude_h2_dependency_fixture as dependency
        fixed=c.private_directory(self.outer/'fixture-native-install')
        shutil.copytree(Path(capture.__file__).parent,fixed/'code/tools/official_client_capture',
                        ignore=shutil.ignore_patterns('__pycache__','tests','*.pyc'))
        data=self.outer/'staging/data'
        dependency.prepare(data/'formal-dependencies')
        manifest_path=data/'formal-dependencies'/dependency.PRODUCTION['path']
        manifest=c.read_json(manifest_path)
        manifest.update(schema_version=capture.FormalDecoderDependencies.manifest_schema,source_scope='formal')
        f.write(manifest_path,manifest)
        c.write_once(data/'formal-channel.key',self.key); c.write_once(fixed/'channel.key',self.key)
        c.write_json_once(data/'formal-runtime.json',capture.collect_formal_runtime())
        class Peer(http.server.BaseHTTPRequestHandler):
            def do_POST(peer):
                raw=peer.rfile.read(int(peer.headers['Content-Length']))
                body=c.canonical({'fixture_only':True,'received':json.loads(raw)})
                peer.send_response(200);peer.send_header('Content-Length',str(len(body)));peer.end_headers();peer.wfile.write(body)
            def log_message(peer,*arguments):pass
        upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Peer)
        peer=threading.Thread(target=upstream.serve_forever,daemon=True);peer.start()
        self.addCleanup(upstream.server_close);self.addCleanup(upstream.shutdown)
        with socket.socket() as port:
            port.bind(('127.0.0.1',0));listener_port=port.getsockname()[1]
        self.capture_root=data/'formal-native-fixture'
        self.task['execution'].update(capture_root=str(self.capture_root),endpoint=f'http://127.0.0.1:{listener_port}')
        self.task['task_sha256']=c.digest({k:v for k,v in self.task.items() if k!='task_sha256'})
        f.write(self.task_path,self.task)
        configuration={'schema_version':capture.CONFIG_SCHEMA,'source_scope':'formal','data_root':str(data),
             'capture_relative':self.capture_root.relative_to(data).as_posix(),'task_binding':c.binding(data,self.task_path),
             'deployment':self.task['execution']['source_deployments']['capture'],'control_socket_relative':'formal-control/capture.sock',
             'channel_key_binding':c.binding(data,data/'formal-channel.key'),
             'listener':{'host':'127.0.0.1','port':listener_port,'tls':None},
             'upstream':{'host':'127.0.0.1','port':upstream.server_port,'tls':None},
             'limits':{name:32 for name in capture.h2.COMMON_LIMITS|capture.h2.EXTRA_LIMITS},
             'protocol_policy':{'mode':'cleartext_h1','allow_no_alpn_h1':False},
             'h2_dependencies':{'root_relative':'formal-dependencies','manifest':c.binding(data/'formal-dependencies',manifest_path)},
             'runtime_environment_binding':c.binding(data,data/'formal-runtime.json'),'installation_sha256':'a'*64}
        configuration['limits'].update(max_header_bytes=65536,max_body_bytes=1048576,max_wire_bytes=2097152,
             max_frame_bytes=16384,max_hpack_table_bytes=4096,drain_seconds=2,socket_timeout_seconds=1)
        authority=capture.CaptureInstallation(c.canonical({'configuration':{'channel_key_binding':c.binding(fixed,fixed/'channel.key')}}),'a'*64,capture._TOKEN)
        with mock.patch.object(capture,'INSTALL_ROOT',fixed),\
             mock.patch.object(capture,'trusted_bytes',side_effect=lambda path,**kwargs:c.read_bytes(Path(path))),\
             mock.patch.object(capture,'require_formal_capture_installation',return_value=authority),\
             mock.patch.object(capture.CaptureInstallation,'configuration',return_value=configuration),\
             mock.patch.object(capture,'verify_budget'):
            engine=service._FormalCaptureEngine(configuration)
            try:
                engine.reserve(self.task,{'fixture_only':True,'no_live_qualification':True})
                engine.wait_ready(self.task['task_sha256'])
                request=c.canonical({'model':self.task['scenarios'][0]['model'],'messages':[{'role':'user','content':self.task['scenarios'][0]['challenge']}]})
                with socket.create_connection(('127.0.0.1',listener_port),timeout=3) as connection:
                    connection.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: fixture\r\nContent-Length: '+str(len(request)).encode()+b'\r\nConnection: close\r\n\r\n'+request)
                    received=b''
                    while block:=connection.recv(65536):received+=block
                self.assertIn(b'200 OK',received)
                until=time.monotonic()+3
                while any(worker.is_alive() for worker in engine._workers.values()) and time.monotonic()<until:time.sleep(0.01)
                inventory=engine.close(self.task['task_sha256'])
            finally:
                engine.dispose()
            native=capture.verify_export(self.capture_root,inventory,task_sha256=self.task['task_sha256'])
            self.assertEqual(native['status'],'complete')
            original=c.read_bytes(self.capture_root/'native-capture-manifest.json')
            origin=capture.verify_formal_capture_origin(self.task,self.capture_root)
            self.assertEqual(origin['phase'],'committed')
            self.assertEqual(c.read_bytes(self.capture_root/'native-capture-manifest.json'),original)
            rows=capture._json_lines(self.capture_root,native['requests'])
            self.assertEqual(base64.b64decode(rows[0]['request_body_base64']),request)

    def test_prepare_intent_is_written_before_material_or_product_side_effect(self):
        raw=c.read_bytes(c.check_binding(self.bundle,self.task['execution']['isolation_recipe']))
        payload={'recipe_base64':base64.b64encode(raw).decode(),'dispatch':self.envelope()}
        def refuse():
            self.assertTrue((self.engine.control/'prepare_state.intent.json').is_file())
            raise RuntimeError('fixture-material-failure')
        with mock.patch.object(self.engine,'_stage_materials',side_effect=refuse) as material,\
             mock.patch.object(self.engine,'_recorder') as recorder:
            with self.assertRaisesRegex(RuntimeError,'fixture-material'):
                self.engine.handle(self.request('prepare_state',payload))
            with self.assertRaisesRegex(bridge.MacBridgeError,'pending'):
                self.engine.handle(self.request('prepare_state',payload))
        self.assertEqual(material.call_count,1); recorder.assert_not_called()

    def test_first_prepare_creates_private_nonce_parent_for_real_journal(self):
        """实际准备入口创建目录；仅物料与产品工厂为无账号替身，journal 执行本体。"""
        from tools.official_client_capture.claude_mac_state_journal import StateJournal
        parent=self.engine.root.parent
        self.assertFalse(parent.exists())
        raw=c.read_bytes(c.check_binding(self.bundle,self.task['execution']['isolation_recipe']))
        payload={'recipe_base64':base64.b64encode(raw).decode(),'dispatch':self.envelope()}
        identity={'task_sha256':self.task['task_sha256'],'task_nonce':self.task['task_nonce'],
                  'producer_run_id':self.task['execution']['producer_run_id'],
                  'deadline_utc':self.task['execution']['request_policy']['deadline_utc']}
        recorder=mock.Mock()
        def prepare(task,root):
            self.assertEqual((task,root),(self.task,self.engine.root))
            self.assertEqual(parent.stat().st_mode & 0o777,0o700)
            self.assertEqual(parent.stat().st_uid,os.getuid())
            with StateJournal(identity,parent/'product-state-journal',self.engine.session,
                              ['private-state.json']) as journal:
                journal.prepare()
                result=journal.restore()
                self.assertEqual(result['status'],'restored')
                self.assertFalse(result['formal_ready'])
                self.assertFalse(result['launch_allowed'])
        recorder.prepare_state.side_effect=prepare
        with mock.patch.object(self.engine,'_stage_materials'),\
             mock.patch.object(self.engine,'_recorder',side_effect=lambda *_args,**_kwargs:setattr(self.engine,'recorder',recorder)):
            result=self.engine.handle(self.request('prepare_state',payload))
        recorder.prepare_state.assert_called_once_with(self.task,self.engine.root)
        self.assertEqual(result['mapping']['staging_session_root'],str(self.engine.session))
        self.assertTrue((self.engine.control/'prepare_state.result.json').is_file())
        self.assertEqual(parent.stat().st_mode & 0o777,0o700)

    def test_prepare_refuses_existing_nonprivate_nonce_parent_before_side_effect(self):
        parent=self.engine.root.parent; parent.mkdir(mode=0o755,parents=True); parent.chmod(0o755)
        before=parent.stat()
        raw=c.read_bytes(c.check_binding(self.bundle,self.task['execution']['isolation_recipe']))
        payload={'recipe_base64':base64.b64encode(raw).decode(),'dispatch':self.envelope()}
        with mock.patch.object(self.engine,'_stage_materials') as materials,\
             mock.patch.object(self.engine,'_recorder') as recorder,\
             self.assertRaisesRegex(c.CaptureError,'私有目录'):
            self.engine.handle(self.request('prepare_state',payload))
        materials.assert_not_called(); recorder.assert_not_called()
        after=parent.stat()
        self.assertEqual((after.st_ino,after.st_mode,after.st_mtime_ns),
                         (before.st_ino,before.st_mode,before.st_mtime_ns))
        self.assertEqual(list(parent.iterdir()),[])

    def test_forged_request_or_stale_mirror_has_zero_side_effect(self):
        raw=c.read_bytes(c.check_binding(self.bundle,self.task['execution']['isolation_recipe']))
        for stale in (False,True):
            payload={'recipe_base64':base64.b64encode(raw).decode(),'dispatch':self.envelope(observed=self.at(-60) if stale else None)}
            request=self.request('prepare_state',payload)
            if not stale: request['mac']='0'*64
            with self.subTest(stale=stale),self.assertRaises((ValueError,bridge.MacBridgeError,capture.FormalCaptureError)):
                self.engine.handle(request)
            self.assertFalse(self.engine.control.exists())

    def test_alternate_operation_id_cannot_repeat_product(self):
        with self.assertRaisesRegex(bridge.MacBridgeError,'另一个ID'):
            self.engine.handle(self.request('start_monitoring',{'dispatch':self.envelope()},identifier='start-again'))
        self.assertFalse(self.engine.control.exists())

    def test_export_and_closed_recover_do_not_construct_recorder(self):
        export=self.export()
        with mock.patch.object(self.engine,'_recorder',side_effect=AssertionError('must stay offline')):
            row=self.engine.handle(self.request('export',{'export_id':export['export_id'],'path':'wire.bin',
                      'offset':65536,'limit':65536,'prefix_bytes':65536}))
            self.assertEqual(len(base64.b64decode(row['data_base64'])),65536)
            result=self.engine.handle(self.request('recover',{}))
            self.assertEqual(result['export']['export_id'],export['export_id'])

    def test_export_wrong_identity_or_mutated_original_is_rejected(self):
        export=self.export(); payload={'export_id':export['export_id'],'path':'wire.bin','offset':0,'limit':1,'prefix_bytes':0}
        self.engine._chunk(payload)
        with self.assertRaises(bridge.MacBridgeError): self.engine._chunk({**payload,'export_id':'d'*64})
        (self.engine.root/'wire.bin').write_bytes(b'y'*150000)
        with self.assertRaisesRegex(bridge.MacBridgeError,'漂移'): self.engine._chunk(payload)

    def test_same_export_interrupted_sync_resumes_only_missing_chunks(self):
        export=self.export(); handle=bridge.MacAgentBridge(self.task,bundle_root=self.bundle,
                 services=None,capture_handle=None,installation=self.capture_install,_token=bridge._TOKEN)
        chunks=[]; fail=[True]
        def rpc(_installation,_task,_root,operation,identifier,payload):
            if payload['path']=='wire.bin' and payload['limit']:
                chunks.append(payload['offset'])
                if fail[0] and payload['offset']==65536:
                    fail[0]=False; raise OSError('fixture interrupted')
            return self.engine.handle(self.request(operation,payload,identifier=identifier))
        with mock.patch.object(bridge,'_request',side_effect=rpc),\
             mock.patch.object(bridge,'require_mac_bridge_installation',return_value=self.capture_install):
            with self.assertRaisesRegex(OSError,'fixture interrupted'): handle._sync(export)
            handle._sync(export)
        self.assertEqual(chunks.count(0),1)
        self.assertEqual(c.read_bytes(self.bundle/'wire.bin'),b'x'*150000)
        self.assertEqual(c.read_json(self.bundle/bridge._ROOT_NAME)['export_id'],export['export_id'])

    def test_corrupt_partial_prefix_is_rejected_without_overwrite(self):
        export=self.export(); handle=bridge.MacAgentBridge(self.task,bundle_root=self.bundle,
                 services=None,capture_handle=None,installation=self.capture_install,_token=bridge._TOKEN)
        leaf=self.bundle.parent/'bridge-download'/export['export_id']/'wire.bin.part'
        c.write_once(leaf,b'forged-prefix')
        before=c.read_bytes(leaf)
        def rpc(_installation,_task,_root,operation,identifier,payload):
            return self.engine.handle(self.request(operation,payload,identifier=identifier))
        with mock.patch.object(bridge,'_request',side_effect=rpc),self.assertRaisesRegex(bridge.MacBridgeError,'前缀'):
            handle._sync(export)
        self.assertEqual(c.read_bytes(leaf),before)

    def test_single_agent_lock_refuses_second_instance(self):
        with bridge._claim_agent(self.engine):
            with self.assertRaisesRegex(bridge.MacBridgeError,'持锁'):
                with bridge._claim_agent(self.engine): pass
        with bridge._claim_agent(self.engine): pass

    def test_first_agent_claim_creates_private_nonce_parent(self):
        parent=self.engine.root.parent
        self.assertFalse(parent.exists())
        with bridge._claim_agent(self.engine):
            self.assertEqual(parent.stat().st_mode & 0o777,0o700)
            self.assertEqual(parent.stat().st_uid,os.getuid())
            self.assertTrue((self.engine.control/'agent-owner.lock').is_file())
            self.assertFalse(self.engine.root.exists())
            self.assertFalse(self.engine.session.exists())
        with bridge._claim_agent(self.engine):
            self.assertEqual(parent.stat().st_mode & 0o777,0o700)

    def test_agent_claim_refuses_existing_nonprivate_nonce_parent(self):
        parent=self.engine.root.parent; parent.mkdir(mode=0o755,parents=True); parent.chmod(0o755)
        before=parent.stat()
        with self.assertRaisesRegex(c.CaptureError,'私有目录'):
            with bridge._claim_agent(self.engine):
                self.fail('宽权限 nonce 父不能取得 agent 锁')
        after=parent.stat()
        self.assertEqual((after.st_ino,after.st_mode,after.st_mtime_ns),
                         (before.st_ino,before.st_mode,before.st_mtime_ns))
        self.assertEqual(list(parent.iterdir()),[])

    def test_closed_capture_recovers_offline_without_spawn_or_rpc(self):
        c.write_json_once(self.outer/'closed-capture'/'capture-origin-committed.json',{'fixture_only':True})
        c.write_json_once(self.outer/'closed-capture'/'export-inventory.json',{'export_id':'a'*64})
        handle=service.FormalCaptureHandle.__new__(service.FormalCaptureHandle)
        handle.capture_root=self.outer/'closed-capture'; handle._task=self.task; handle.task_sha256=self.task['task_sha256']
        with mock.patch.object(handle,'_guard'),mock.patch.object(handle,'_call',side_effect=AssertionError('no rpc')) as rpc,\
             mock.patch.object(service.subprocess,'run') as spawn,\
             mock.patch.object(capture,'verify_export'),mock.patch.object(capture,'verify_formal_capture_origin'):
            self.assertEqual(handle.recover(self.task)['export_id'],'a'*64)
            self.assertEqual(handle.close_and_sync(self.task)['export_id'],'a'*64)
        spawn.assert_not_called(); rpc.assert_not_called()

    def test_expired_recovery_main_only_seals_existing_reservation(self):
        config=c.write_json_once(self.outer/'recover-config.json',{'fixture_only':True})
        c.write_json_once(self.outer/'recover-hold.json',{'fixture_only':True})
        engine=mock.Mock(); engine.root=self.capture_root
        argv=['formal','--managed-daemon',str(self.outer/'recover-config.json'),'--hold',str(self.outer/'recover-hold.json'),'--recover']
        with mock.patch.object(service.sys,'argv',argv),mock.patch.object(service.signal,'signal'),mock.patch.object(capture,'validate_configuration',return_value={}),\
             mock.patch.object(capture,'verify_task_configuration',return_value=self.task),\
             mock.patch.object(capture,'verify_budget') as verify, mock.patch.object(service,'_FormalCaptureEngine',return_value=engine):
            self.assertEqual(service.main(),0)
        self.assertFalse(verify.call_args.kwargs['require_live'])
        engine.reserve.assert_not_called(); engine.wait_ready.assert_not_called(); engine.recover.assert_called_once()


class TransportJointTests(unittest.TestCase):
    def setUp(self):
        at=lambda second:f'2026-10-10T00:00:{second:02d}+00:00'
        self.task={'scenarios':[{'id':'echo'}]}
        self.token=tuple(range(8))
        self.connection={'connection_id':'c1','client_endpoint':'127.0.0.1:2345','server_endpoint':'127.0.0.1:8080',
                         'opened_at_utc':at(2),'closed_at_utc':at(9)}
        self.request={'connection_id':'c1','request_ordinal':1,'observed_at_utc':at(5),'host':'127.0.0.1',
                      'method':'POST','path':'/v1/messages','request_body_base64':'e30=','response_body_base64':'e30='}
        self.flow={'flow_id':'flow-1','client_endpoint':self.connection['client_endpoint'],
                   'server_endpoint':self.connection['server_endpoint'],'opened_at_utc':at(1),'closed_at_utc':at(10),
                   'process':{'audit_token':list(self.token)}}
        self.originals={'session_id':'session-1','network_flows':[self.flow],'scenarios':[{'scenario_id':'echo',
                        'session_events':[{'session_id':'session-1','kind':'input','observed_at_utc':at(3)},
                                          {'session_id':'session-1','kind':'completed','observed_at_utc':at(7)}]}]}
        self.process={'process_instances':{self.token:'p1'},'process_session':{'processes':[{'process_instance_id':'p1','parent_instance_id':None}],
                      'loaded_modules':[{'module_instance_id':'m1','host_process_instance_id':'p1','activated_at_utc':at(1)}]}}

    def test_ordinal_and_modules_come_from_independent_bytes_and_signed_process(self):
        result=bridge._derive_transport_rows(self.task,[self.connection],[self.request],self.originals,self.process)
        self.assertEqual(result['socket_calls'][0]['request_ordinals'],[1])
        self.assertEqual(result['socket_calls'][0]['module_instance_ids'],['m1'])
        self.assertEqual(result['socket_calls'][0]['scenario_id'],'echo')
        self.assertEqual(result['requests'],[self.request])

    def test_nat_multiple_unknown_token_and_outside_scenario_fail_closed(self):
        for kind in ('nat','multiple','unknown-flow','token','window'):
            originals=copy.deepcopy(self.originals); request=copy.deepcopy(self.request)
            if kind=='nat': originals['network_flows'][0]['client_endpoint']='10.0.0.1:2345'
            if kind=='multiple': originals['network_flows'].append({**self.flow,'flow_id':'f2'})
            if kind=='unknown-flow': originals['network_flows'].append({**self.flow,'flow_id':'f2','client_endpoint':'other:1'})
            if kind=='token': originals['network_flows'][0]['process']['audit_token']=[1]*8
            if kind=='window': request['observed_at_utc']='2026-10-10T00:00:08+00:00'
            with self.subTest(kind=kind),self.assertRaises(bridge.MacBridgeError):
                bridge._derive_transport_rows(self.task,[self.connection],[request],originals,self.process)


if __name__=='__main__': unittest.main()
