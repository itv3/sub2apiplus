#!/usr/bin/env python3
"""C-02：入口三组后端测试前预编译一次生产包；不执行测试或 TestMain。

仅由显式入口调用。预热和正式测试使用同一工作目录内的 Go 缓存；失败留档并停止，
不会签发测试通过记录。关闭预热仍执行原三组测试，不能承接本文件的编译结果。
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

GATES = frozenset({'backend-go-test', 'backend-unit', 'backend-integration'})
CACHE_NAMES = ('GOCACHE', 'GOMODCACHE', 'GOTMPDIR')
SCHEMA = 'entry-go-compile-warmup/v1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def source_binding(tree):
    """绑定实际后端源码，脏树和未登记后端文件不能作为预热输入。"""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tree), *args], stderr=subprocess.PIPE)
    if git('status', '--porcelain', '--untracked-files=all', '--', 'backend').strip():
        raise ValueError('后端源码树不干净')
    files = []
    for name in git('ls-files', '-z', '--', 'backend').decode().split('\0'):
        if not name:
            continue
        path = tree / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('后端输入不是普通文件：' + name)
        files.append({'path': name, 'sha256': digest(path.read_bytes())})
    if not files or not (tree / 'backend/go.mod').is_file():
        raise ValueError('缺少后端模块和源码')
    return {'commit': git('rev-parse', 'HEAD').decode().strip(), 'files_sha256': digest(canonical(files)), 'file_count': len(files)}


def cache_paths(tree, work, environment):
    """只允许本次工作根内的独立缓存，拒绝源码内、共享外部目录和符号链接。"""
    paths = {}
    for name in CACHE_NAMES:
        raw = environment.get(name, '')
        path = Path(raw)
        if not raw or not path.is_absolute() or path != path.resolve():
            raise ValueError(name + ' 必须是无符号链接的绝对路径')
        if path == work or not path.is_relative_to(work) or path.is_relative_to(tree) or tree.is_relative_to(path):
            raise ValueError(name + ' 必须是工作根内、源码树外的独立子目录')
        paths[name] = path
    values = list(paths.values())
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(values) for b in values[i + 1:]):
        raise ValueError('三种 Go 缓存目录必须互不包含')
    return paths


def prepare(*, tree, work, manifest, output, mode='auto', environment=None, timeout=1800):
    """每份门禁运行只调用一次；编译失败、来源漂移、外部缓存均在派发测试前拒绝。"""
    environment = dict(os.environ if environment is None else environment)
    tree, work = Path(tree).resolve(), Path(work).resolve()
    manifest, output = Path(manifest), Path(output)
    plan = json.loads(manifest.read_text())
    gates = {row['gate_id'] for row in plan['gates']}
    if mode not in ('auto', 'off'):
        raise ValueError('未知预热模式')
    if output.exists() or output.is_symlink():
        raise ValueError('预热收据已存在，禁止重复编译或覆盖证据')
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = output.open('x')
    os.chmod(output, 0o600)
    report = {'schema_version': SCHEMA, 'mode': mode, 'status': 'running',
              'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'manifest_sha256': digest(manifest.read_bytes()), 'tool_sha256': digest(Path(__file__).read_bytes()),
              'command': ['go', 'build', './...'], 'tests_executed': False, 'test_result_inheritance': False,
              'tree': str(tree), 'work': str(work)}
    started = time.monotonic()
    lock = None
    try:
        if mode == 'off' or not GATES.issubset(gates):
            report.update(status='off', reason='显式关闭' if mode == 'off' else '本次不含三组后端测试')
            return report
        paths = cache_paths(tree, work, environment)
        report['cache_roots'] = {name: str(path) for name, path in paths.items()}
        before = source_binding(tree)
        report['source'] = before
        go = shutil.which('go', path=environment.get('PATH'))
        if go is None:
            raise ValueError('缺少 Go 编译器')
        report['go_binary_sha256'] = digest(Path(go).resolve().read_bytes())
        report['environment_sha256'] = digest(canonical({key: value for key, value in environment.items()
            if key.startswith(('GO', 'CGO_')) or key in ('PATH', 'CC', 'CXX')}))
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = work / '.go-compile-warmup.lock'
        lock = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a+')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        log = output.with_suffix('.log')
        with log.open('x') as handle:
            os.chmod(log, 0o600)
            # GOFLAGS 等由正式门禁传入同一份环境；只编译包，不调用测试入口。
            result = subprocess.run(report['command'], cwd=tree / 'backend', env=environment,
                                    stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        report.update(returncode=result.returncode, log={'path': str(log), 'sha256': digest(log.read_bytes())})
        if result.returncode:
            raise ValueError('Go 预编译失败，正式门禁尚未派发')
        if source_binding(tree) != before:
            raise ValueError('预编译期间后端源码发生变化')
        report['status'] = 'passed'
        return report
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        report['elapsed_seconds'] = time.monotonic() - started
        report['completed_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if lock is not None:
            lock.close()
        log = output.with_suffix('.log')
        if log.is_file():
            report['log'] = {'path': str(log), 'sha256': digest(log.read_bytes())}
        with receipt:
            receipt.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def write_plan(*, tree, work, manifest, output, plan_output, launcher):
    """使用现有命令调度器独占执行预热，沿用预约、进程回收和日志合同。"""
    if not GATES.issubset({row['gate_id'] for row in json.loads(Path(manifest).read_text())['gates']}):
        raise ValueError('预热入口要求本次包含三组后端测试')
    command = [*launcher, sys.executable, '-B', str(Path(__file__).resolve())]
    for name, value in (('tree', tree), ('work', work), ('manifest', manifest), ('output', output)):
        command += ['--' + name, str(Path(value).resolve())]
    payload = {'schema_version': 'unit-executor-commands/v1', 'units': [{
        'unit_id': 'prepare:go-compile', 'argv': command, 'cwd': str(Path(tree).resolve()),
        'cores': 2, 'memory_mb': 5120, 'exclusive': True, 'timeout_seconds': 1860,
        'env': {'GOMAXPROCS': '2'}, 'weight': 1.0}]}
    with Path(plan_output).open('x') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ('tree', 'work', 'manifest', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=('auto', 'off'), default='auto')
    parser.add_argument('--plan-output', type=Path)
    parser.add_argument('--launcher-json', default='[]')
    args = parser.parse_args()
    try:
        if args.plan_output:
            launcher = json.loads(args.launcher_json)
            if not isinstance(launcher, list) or not all(isinstance(value, str) for value in launcher):
                raise ValueError('隔离启动参数必须是字符串数组')
            write_plan(tree=args.tree, work=args.work, manifest=args.manifest, output=args.output,
                       plan_output=args.plan_output, launcher=launcher)
            print(json.dumps({'status': 'planned', 'plan': str(args.plan_output)}, ensure_ascii=False))
            return 0
        report = prepare(tree=args.tree, work=args.work, manifest=args.manifest, output=args.output, mode=args.mode)
        print(json.dumps({'status': report['status'], 'elapsed_seconds': report['elapsed_seconds'], 'tests_executed': False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Go 预热拒绝：' + str(error), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
