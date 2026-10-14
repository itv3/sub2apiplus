"""C-02：预热不运行测试，缓存和来源隔离，准备失败不得进入正式门禁。"""
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.ci import go_compile_warmup as warmup

REPO = Path(__file__).resolve().parents[3]


class GoCompileWarmupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tree = self.root / 'tree'
        (self.tree / 'backend').mkdir(parents=True)
        (self.tree / 'backend/go.mod').write_text('module warmup.invalid/example\n\ngo 1.22\n')
        (self.tree / 'backend/main.go').write_text('package main\nfunc main() {}\n')
        for command in (['init', '-q'], ['add', '.'], ['-c', 'user.name=验收', '-c', 'user.email=test@example.invalid', 'commit', '-qm', '验收夹具']):
            subprocess.run(['git', '-C', str(self.tree), *command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.work / 'calls.jsonl'
        self.fake_go('')
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'GOCACHE': str(self.work / 'build'), 'GOMODCACHE': str(self.work / 'modules'), 'GOTMPDIR': str(self.work / 'tmp'),
                    'PYTHONDONTWRITEBYTECODE': '1', 'UNIT_EXECUTOR_STATE_DIR': str(self.work / 'scheduler')}
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'gates': [{'gate_id': key} for key in sorted(warmup.GATES)]}))
        self.output = self.root / 'output/warmup.json'

    def fake_go(self, suffix, *, download='', verify='print("all modules verified")\n'):
        """生成假 go。suffix 是对 build 等非模块子命令附加执行的代码，原有用法不变。

        预热在 go build 之后会追加 go mod download 与 go mod verify，两者走专用分支：行为由 download、verify
        两段代码决定（默认下载静默成功、校验输出成功行）。这两类调用只记到 calls-detail.jsonl，不进 calls.jsonl，
        所以 calls.jsonl 仍只反映编译和测试类调用，原有用例据此断言“只编译一次、不跑测试”的含义不变。
        calls-detail.jsonl 每次调用记一行：参数、工作目录和 Go 相关环境变量，供新用例核对三步的顺序、目录与环境。
        """
        path = self.bin / 'go'
        detail = self.work / 'calls-detail.jsonl'
        branches = ''
        for name, code in (('download', download), ('verify', verify)):
            code = code + ('' if not code or code.endswith('\n') else '\n') + 'sys.exit(0)\n'
            branches += 'if sys.argv[1:3] == ["mod", "' + name + '"]:\n' + ''.join('    ' + line for line in code.splitlines(True))
        path.write_text('#!' + sys.executable + '\nimport sys,json,time,os\nfrom pathlib import Path\n'
                        + 'with Path(' + repr(str(detail)) + ').open("a") as f: f.write(json.dumps({"args": sys.argv[1:], '
                        '"cwd": os.getcwd(), "env": {k: v for k, v in os.environ.items() '
                        'if k.startswith(("GO", "CGO_", "WARMUP_"))}}) + "\\n")\n' + branches
                        + f'with Path({str(self.calls)!r}).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n' + suffix)
        path.chmod(0o700)

    def run_prepare(self, **kwargs):
        return warmup.prepare(tree=self.tree, work=self.work, manifest=self.manifest,
                              output=self.output, environment=self.env, **kwargs)

    def test_one_build_only_no_test_or_testmain_and_duplicate_refused(self):
        result = self.run_prepare()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual([json.loads(line) for line in self.calls.read_text().splitlines()], [['build', './...']])
        self.assertFalse(result['tests_executed'])
        self.assertFalse(result['test_result_inheritance'])
        self.assertEqual(json.loads(self.output.read_text())['source'], warmup.source_binding(self.tree))
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.run_prepare()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_off_preserves_original_caches_and_does_not_compile(self):
        self.env['GOCACHE'] = '/outside/caller/cache'
        self.assertEqual(self.run_prepare(mode='off')['status'], 'off')
        self.assertFalse(self.calls.exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_missing_three_groups_does_not_compile(self):
        self.manifest.write_text(json.dumps({'gates': [{'gate_id': 'backend-go-test'}]}))
        self.assertEqual(self.run_prepare()['status'], 'off')
        self.assertFalse(self.calls.exists())

    def test_external_source_symlink_or_nested_caches_refused(self):
        cases = [('outside', str(self.root / 'outside')), ('source', str(self.tree / 'backend/cache')),
                 ('nested', str(self.work / 'modules/nested'))]
        link = self.work / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        cases.append(('symlink', str(link / 'build')))
        for name, path in cases:
            with self.subTest(name=name):
                env = {**self.env, 'GOCACHE': path}
                with self.assertRaises(ValueError):
                    warmup.prepare(tree=self.tree, work=self.work, manifest=self.manifest,
                                   output=self.root / name / 'result.json', environment=env)
        self.assertFalse(self.calls.exists())

    def test_compile_failure_keeps_log_and_failed_receipt(self):
        self.fake_go('print("合成编译失败")\nsys.exit(9)\n')
        with self.assertRaisesRegex(ValueError, '预编译失败'):
            self.run_prepare()
        report = json.loads(self.output.read_text())
        self.assertEqual((report['status'], report['returncode']), ('failed', 9))
        self.assertEqual(report['log']['sha256'], warmup.digest(self.output.with_suffix('.log').read_bytes()))

    def test_dirty_source_is_rejected_before_compilation(self):
        (self.tree / 'backend/untracked.go').write_text('package main\n')
        with self.assertRaisesRegex(ValueError, '不干净'):
            self.run_prepare()
        self.assertFalse(self.calls.exists())

    def test_source_change_during_build_fails(self):
        self.fake_go(f'Path({str(self.tree / "backend/main.go")!r}).write_text("package main\\n")\n')
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertEqual(json.loads(self.output.read_text())['status'], 'failed')

    def test_timeout_keeps_failure_evidence(self):
        self.fake_go('time.sleep(10)\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_prepare(timeout=.2)
        report = json.loads(self.output.read_text())
        self.assertEqual(report['error_type'], 'TimeoutExpired')
        self.assertIn('log', report)

    def test_actual_scheduler_executes_prepare_once_and_closes_processes(self):
        plan = self.root / 'prepare-plan.json'
        warmup.write_plan(tree=self.tree, work=self.work, manifest=self.manifest, output=self.output,
                          plan_output=plan, launcher=[])
        command = [sys.executable, '-B', str(REPO / 'tools/ci/unit_executor.py'), 'run-commands',
                   '--manifest', str(plan), '--out-dir', str(self.root / 'executor'), '--shared-caches', 'off']
        result = subprocess.run(command, cwd=self.tree, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        summary = json.loads((self.root / 'executor/summary.json').read_text())
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['units'][0]['orphans'], 0)
        self.assertTrue(summary['units'][0]['exclusive'])
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_driver_copy_and_entry_fail_closed_order(self):
        self.assertEqual((REPO / 'tools/ci/go_compile_warmup.py').read_bytes(),
                         (REPO / 'tools/arm64_capture_driver/driver/go_compile_warmup.py').read_bytes())
        script = (REPO / 'tools/arm64_capture_driver/driver/entry-gates.sh').read_text()
        self.assertLess(script.index('--manifest "$OUT/go-compile-warmup-plan.json"'), script.index('run-gates --manifest "$OUT/gates-manifest.json"'))
        self.assertIn('Go 预热失败，正式门禁尚未派发', script)
        self.assertIn('[ -z "$FULL_SET_REQUEST" ]', script)

    # ------------------------------------------------------------------
    # 以下为新增用例：go build 成功后追加 go mod download（联网，一次）与 go mod verify（离线校验）。
    # 为什么要加这两步：命令单元在运行期要被强制离线，模块缓存必须在运行前就完整；而 go build ./... 只会下载
    # 生产构建用得上的模块，仅 go test 才用得上的依赖可能缺。夹具约定见 fake_go：模块子命令只记到
    # calls-detail.jsonl，不进 calls.jsonl，所以上面原有用例的断言含义不变。
    # ------------------------------------------------------------------

    def go_calls(self):
        """读取假 go 记下的全部调用（参数、工作目录、Go 相关环境变量），按发生顺序返回。"""
        path = self.work / 'calls-detail.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def go_args(self):
        return [call['args'] for call in self.go_calls()]

    def receipt(self):
        return json.loads(self.output.read_text())

    def fresh_run(self, name):
        """同一个收据路径不能重复使用：子用例换一个新位置，并清空上一轮的调用记录。"""
        self.output = self.root / name / 'warmup.json'
        for record in (self.calls, self.work / 'calls-detail.jsonl'):
            record.unlink(missing_ok=True)

    def add_go_sum(self, content=b'warmup.invalid/dep v1.0.0 h1:Zm9vYmFy\nwarmup.invalid/dep v1.0.0/go.mod h1:YmF6\n'):
        """补一个 go.sum 并入库：预热要求后端源码树干净，所以夹具里的 go.sum 必须是已提交的文件。"""
        (self.tree / 'backend/go.sum').write_bytes(content)
        for command in (['add', '.'], ['-c', 'user.name=验收', '-c', 'user.email=test@example.invalid', 'commit', '-qm', '补 go.sum']):
            subprocess.run(['git', '-C', str(self.tree), *command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return content

    def test_three_steps_run_in_order_in_backend_with_the_same_environment(self):
        # 正式门禁传入的网络相关变量必须原样到达三步：预热不设置、不改写其中任何一个
        self.env.update({'GOFLAGS': '-mod=readonly', 'GOPROXY': 'https://proxy.example.invalid', 'GONOSUMDB': '*.invalid',
                         'WARMUP_PROBE': 'same-environment'})
        result = self.run_prepare()
        self.assertEqual(result['status'], 'passed')
        calls = self.go_calls()
        self.assertEqual([call['args'] for call in calls], [['build', './...'], ['mod', 'download'], ['mod', 'verify']])
        for call in calls:
            self.assertEqual(call['cwd'], str(self.tree / 'backend'))
            self.assertEqual(call['env'], calls[0]['env'])
        for key in ('GOCACHE', 'GOMODCACHE', 'GOTMPDIR', 'GOFLAGS', 'GOPROXY', 'GONOSUMDB', 'WARMUP_PROBE'):
            self.assertEqual(calls[0]['env'][key], self.env[key], key)
        # 原有记录的含义不变：calls.jsonl 里仍只有一次编译，没有任何测试调用
        self.assertEqual([json.loads(line) for line in self.calls.read_text().splitlines()], [['build', './...']])
        self.assertFalse(result['tests_executed'])
        self.assertFalse(result['test_result_inheritance'])
        # 重复调用被拒绝，下载和校验也不会再多跑一次
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.run_prepare()
        self.assertEqual(len(self.go_calls()), 3)

    def test_prepare_adds_no_go_variables_of_its_own(self):
        for key in [key for key in self.env if key.startswith(('GO', 'CGO_', 'WARMUP_')) and key not in warmup.CACHE_NAMES]:
            del self.env[key]
        self.run_prepare()
        self.assertEqual(len(self.go_calls()), 3)
        for call in self.go_calls():
            self.assertEqual(sorted(call['env']), sorted(warmup.CACHE_NAMES))

    def test_receipt_records_module_steps_separate_logs_and_go_sum_digest(self):
        go_sum = self.add_go_sum()
        self.fake_go('print("build output")\n', download='print("download output")\n')
        self.run_prepare()
        report = self.receipt()
        build_log = self.output.with_suffix('.log')
        download_log = self.output.with_suffix('.mod-download.log')
        verify_log = self.output.with_suffix('.mod-verify.log')
        # 三步各写各的日志，互不混入，且都只有属主可读写
        self.assertEqual(build_log.read_bytes(), b'build output\n')
        self.assertEqual(download_log.read_bytes(), b'download output\n')
        self.assertEqual(verify_log.read_bytes(), b'all modules verified\n')
        for path in (build_log, download_log, verify_log):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        # 新增字段
        self.assertEqual(report['go_sum_sha256'], warmup.digest(go_sum))
        download, verify = report['module_download'], report['module_verify']
        self.assertEqual((download['command'], download['returncode']), (['go', 'mod', 'download'], 0))
        self.assertEqual(download['log'], {'path': str(download_log), 'sha256': warmup.digest(b'download output\n')})
        self.assertEqual((verify['command'], verify['returncode']), (['go', 'mod', 'verify'], 0))
        self.assertIs(verify['verified'], True)
        self.assertEqual(verify['output'], {'path': str(verify_log), 'sha256': warmup.digest(b'all modules verified\n')})
        for step in (download, verify):
            self.assertGreaterEqual(step['elapsed_seconds'], 0)
        # 旧字段含义不变：schema 不升版，command 仍是编译命令，log 仍只是编译日志，source 仍是源码绑定
        self.assertEqual(report['schema_version'], 'entry-go-compile-warmup/v1')
        self.assertEqual(report['command'], ['go', 'build', './...'])
        self.assertEqual((report['status'], report['returncode']), ('passed', 0))
        self.assertEqual(report['log'], {'path': str(build_log), 'sha256': warmup.digest(b'build output\n')})
        self.assertEqual(report['source'], warmup.source_binding(self.tree))

    def test_missing_go_sum_is_recorded_as_null_and_steps_still_run(self):
        # 没有外部依赖的模块没有 go.sum：不拒绝，摘要记 null，下载和校验照常执行
        self.run_prepare()
        self.assertIsNone(self.receipt()['go_sum_sha256'])
        self.assertEqual(self.go_args(), [['build', './...'], ['mod', 'download'], ['mod', 'verify']])

    def test_download_failure_fails_warmup_and_skips_verify(self):
        self.fake_go('', download='print("download failed")\nsys.exit(7)\n')
        with self.assertRaisesRegex(ValueError, '模块下载失败.*退出码 7.*正式门禁尚未派发'):
            self.run_prepare()
        report = self.receipt()
        download_log = self.output.with_suffix('.mod-download.log')
        self.assertEqual((report['status'], report['error_type']), ('failed', 'ValueError'))
        self.assertEqual(report['module_download']['returncode'], 7)
        self.assertEqual(download_log.read_bytes(), b'download failed\n')
        self.assertEqual(report['module_download']['log']['sha256'], warmup.digest(download_log.read_bytes()))
        # 校验这一步没有执行，收据里没有它的登记项，也没有它的日志
        self.assertNotIn('module_verify', report)
        self.assertFalse(self.output.with_suffix('.mod-verify.log').exists())
        self.assertEqual(self.go_args(), [['build', './...'], ['mod', 'download']])
        # 编译已经成功，编译结果和 go.sum 摘要仍留在失败收据里
        self.assertEqual(report['returncode'], 0)
        self.assertIn('go_sum_sha256', report)

    def test_verify_failure_fails_warmup_when_exit_nonzero_or_success_line_missing(self):
        cases = [
            ('exit-nonzero-with-line', 'print("all modules verified")\nsys.exit(1)\n', 1, '退出码 1'),
            ('exit-nonzero-with-error', 'print("warmup.invalid/dep v1.0.0: dir has been modified")\nsys.exit(1)\n', 1, '退出码 1'),
            ('silent-success', '', 0, '没有单独一行'),
            ('other-text', 'print("looks fine")\n', 0, '没有单独一行'),
            ('negated', 'print("not all modules verified")\n', 0, '没有单独一行'),
            ('suffixed', 'print("all modules verified!")\n', 0, '没有单独一行'),
        ]
        for name, code, returncode, reason in cases:
            with self.subTest(name):
                self.fresh_run(name)
                self.fake_go('', verify=code)
                with self.assertRaisesRegex(ValueError, '模块校验失败.*' + reason + '.*正式门禁尚未派发'):
                    self.run_prepare()
                report = self.receipt()
                verify = report['module_verify']
                self.assertEqual((report['status'], report['error_type']), ('failed', 'ValueError'))
                self.assertEqual(verify['returncode'], returncode)
                self.assertIs(verify['verified'], False)
                self.assertEqual(verify['output']['sha256'], warmup.digest(self.output.with_suffix('.mod-verify.log').read_bytes()))
                # 下载已成功，是校验判了失败；三步都执行过
                self.assertEqual(report['module_download']['returncode'], 0)
                self.assertEqual(self.go_args(), [['build', './...'], ['mod', 'download'], ['mod', 'verify']])

    def test_verify_accepts_hint_lines_before_the_success_line(self):
        self.fake_go('', verify='print("go: downloading warmup.invalid/dep v1.0.0")\nprint("all modules verified")\n')
        self.assertEqual(self.run_prepare()['status'], 'passed')
        self.assertIs(self.receipt()['module_verify']['verified'], True)

    def test_module_verified_requires_exit_zero_and_a_whole_success_line(self):
        ok = b'all modules verified\n'
        cases = [(0, ok, True), (0, b'all modules verified', True), (0, b'  all modules verified \r\n', True),
                 (0, b'go: downloading x v1.0.0\n' + ok, True), (0, ok + b'go: note\n', True),
                 (1, ok, False), (-9, ok, False), (0, b'', False), (0, b'not all modules verified\n', False),
                 (0, b'all modules verified!\n', False), (0, b'x all modules verified\n', False),
                 (0, '全部模块已校验\n'.encode(), False), (0, b'\xff\xfe all modules verified\n', False)]
        for returncode, output, expected in cases:
            with self.subTest(returncode=returncode, output=output):
                self.assertIs(warmup.module_verified(returncode, output), expected)

    def test_build_failure_skips_module_steps(self):
        self.fake_go('print("compile failed")\nsys.exit(9)\n')
        with self.assertRaisesRegex(ValueError, '预编译失败'):
            self.run_prepare()
        report = self.receipt()
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('module_download', report)
        self.assertNotIn('module_verify', report)
        self.assertEqual(self.go_args(), [['build', './...']])

    def test_source_change_during_build_skips_module_steps(self):
        self.fake_go(f'Path({str(self.tree / "backend/main.go")!r}).write_text("package main\\n")\n')
        with self.assertRaisesRegex(ValueError, '不干净'):
            self.run_prepare()
        report = self.receipt()
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('module_download', report)
        self.assertEqual(self.go_args(), [['build', './...']])

    # 下载若改动了后端源码（例如 -mod=mod 环境下补写 go.sum），缓存就不再由登记的 go.sum 约束，必须拒绝。
    # 改动有两种形态：留在工作区没提交，会被源码绑定直接判“不干净”；改完又提交，树是干净的但提交与文件摘要变了，
    # 要靠“下载与校验之后再核对一次”这条新增的比较才能发现。两种都要拒绝，所以分开验证。
    def test_uncommitted_source_change_during_module_steps_fails(self):
        self.fake_go('', download=f'Path({str(self.tree / "backend/main.go")!r}).write_text("package main\\n// dirty\\n")\n')
        with self.assertRaisesRegex(ValueError, '不干净'):
            self.run_prepare()
        report = self.receipt()
        self.assertEqual(report['status'], 'failed')
        # 两步本身都成功了，是最后的源码核对拒绝了它
        self.assertEqual(report['module_download']['returncode'], 0)
        self.assertIs(report['module_verify']['verified'], True)

    def test_committed_source_change_during_module_steps_fails(self):
        commit = ('import subprocess\n'
                  f'Path({str(self.tree / "backend/main.go")!r}).write_text("package main\\n// committed\\n")\n'
                  f'subprocess.run(["git", "-C", {str(self.tree)!r}, "-c", "user.name=验收", "-c", "user.email=test@example.invalid", '
                  '"commit", "-qam", "下载期间改动"], check=True)\n')
        self.fake_go('', download=commit)
        with self.assertRaisesRegex(ValueError, '模块下载或校验期间后端源码发生变化'):
            self.run_prepare()
        report = self.receipt()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['module_download']['returncode'], 0)
        self.assertIs(report['module_verify']['verified'], True)

    def test_module_step_timeouts_keep_failure_evidence(self):
        cases = [('download', {'download': 'time.sleep(10)\n'}, {'download_timeout': .2}, 'log'),
                 ('verify', {'verify': 'time.sleep(10)\n'}, {'verify_timeout': .2}, 'output')]
        for step, fake, options, digest_key in cases:
            with self.subTest(step):
                self.fresh_run(step)
                self.fake_go('', **fake)
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.run_prepare(**options)
                report = self.receipt()
                record = report['module_' + step]
                self.assertEqual((report['status'], report['error_type']), ('failed', 'TimeoutExpired'))
                self.assertIsNone(record['returncode'])
                self.assertIn('sha256', record[digest_key])
                if step == 'download':
                    self.assertNotIn('module_verify', report)
                else:
                    self.assertIs(record['verified'], False)

    def test_existing_module_log_is_never_overwritten_or_registered(self):
        stale = self.output.with_suffix('.mod-download.log')
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'stale evidence\n')
        with self.assertRaises(FileExistsError):
            self.run_prepare()
        report = self.receipt()
        self.assertEqual(stale.read_bytes(), b'stale evidence\n')
        self.assertEqual((report['status'], report['error_type']), ('failed', 'FileExistsError'))
        self.assertNotIn('log', report['module_download'])
        self.assertNotIn('module_verify', report)

    def test_off_and_missing_groups_leave_no_module_fields(self):
        cases = [('off', {'mode': 'off'}, None), ('missing-groups', {}, {'gates': [{'gate_id': 'backend-go-test'}]})]
        for name, options, gates in cases:
            with self.subTest(name):
                self.fresh_run(name)
                if gates is not None:
                    self.manifest.write_text(json.dumps(gates))
                self.assertEqual(self.run_prepare(**options)['status'], 'off')
                report = self.receipt()
                for key in ('module_download', 'module_verify', 'go_sum_sha256'):
                    self.assertNotIn(key, report)
                self.assertEqual(self.go_calls(), [])

    def test_plan_unit_timeout_is_2400_and_covers_the_three_internal_timeouts(self):
        plan = self.root / 'plan.json'
        payload = warmup.write_plan(tree=self.tree, work=self.work, manifest=self.manifest, output=self.output,
                                    plan_output=plan, launcher=[])
        unit = payload['units'][0]
        self.assertEqual(json.loads(plan.read_text())['units'][0], unit)
        self.assertEqual((unit['unit_id'], unit['timeout_seconds']), ('prepare:go-compile', 2400))
        # 其余约束沿用原样：独占、2 核、5120MB
        self.assertEqual((unit['cores'], unit['memory_mb'], unit['exclusive']), (2, 5120, True))
        parameters = inspect.signature(warmup.prepare).parameters
        internal = [parameters[name].default for name in ('timeout', 'download_timeout', 'verify_timeout')]
        self.assertEqual(internal, [1800, 420, 120])
        # 三步内部超时之和加 60 秒余量正好是单元超时：内部超时先触发，才能留下带失败原因的收据
        self.assertEqual(sum(internal) + 60, unit['timeout_seconds'])

    def run_scheduler(self):
        plan = self.root / 'prepare-plan.json'
        warmup.write_plan(tree=self.tree, work=self.work, manifest=self.manifest, output=self.output,
                          plan_output=plan, launcher=[])
        command = [sys.executable, '-B', str(REPO / 'tools/ci/unit_executor.py'), 'run-commands',
                   '--manifest', str(plan), '--out-dir', str(self.root / 'executor'), '--shared-caches', 'off']
        return subprocess.run(command, cwd=self.tree, env=self.env, capture_output=True, text=True, timeout=30)

    def test_actual_scheduler_runs_module_steps_once_and_leaves_a_complete_receipt(self):
        go_sum = self.add_go_sum()
        result = self.run_scheduler()
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        summary = json.loads((self.root / 'executor/summary.json').read_text())
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['units'][0]['orphans'], 0)
        self.assertEqual(self.go_args(), [['build', './...'], ['mod', 'download'], ['mod', 'verify']])
        report = self.receipt()
        self.assertEqual((report['status'], report['go_sum_sha256']), ('passed', warmup.digest(go_sum)))
        self.assertEqual((report['module_download']['returncode'], report['module_verify']['returncode']), (0, 0))
        self.assertIs(report['module_verify']['verified'], True)

    def test_actual_scheduler_fails_the_unit_when_verify_fails(self):
        self.fake_go('', verify='print("warmup.invalid/dep v1.0.0: dir has been modified")\nsys.exit(1)\n')
        result = self.run_scheduler()
        self.assertNotEqual(result.returncode, 0)
        summary = json.loads((self.root / 'executor/summary.json').read_text())
        self.assertNotEqual(summary['status'], 'passed')
        self.assertEqual(self.go_args(), [['build', './...'], ['mod', 'download'], ['mod', 'verify']])
        report = self.receipt()
        self.assertEqual(report['status'], 'failed')
        self.assertIs(report['module_verify']['verified'], False)


if __name__ == '__main__':
    unittest.main()
