"""C-02：预热不运行测试，缓存和来源隔离，准备失败不得进入正式门禁。"""
import json
import os
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

    def fake_go(self, suffix):
        path = self.bin / 'go'
        path.write_text('#!' + sys.executable + '\nimport sys,json,time\nfrom pathlib import Path\n'
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


if __name__ == '__main__':
    unittest.main()
