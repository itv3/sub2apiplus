#!/usr/bin/env python3
"""C-02：入口三组后端测试前预编译一次生产包并补齐 Go 模块缓存；不执行测试或 TestMain。

仅由显式入口调用。预热和正式测试使用同一工作目录内的 Go 缓存；失败留档并停止，
不会签发测试通过记录。关闭预热仍执行原三组测试，不能承接本文件的编译结果。

预热在 backend 目录、同一份环境、同一组三个缓存目录下依次做三步，任一步失败即整体失败：
1. ``go build ./...``：只编译生产包，把编译缓存写热。
2. ``go mod download``：联网把构建清单里全部模块的压缩包下载齐。第 1 步只会取到生产构建用得上的模块，
   仅 ``go test`` 才用得上的测试专用依赖可能还缺；而后面的命令单元要求运行期离线（GOPROXY=off），
   所以模块缓存必须在运行之前就完整。联网只发生在这一步，本文件不设置也不改写任何 Go 网络相关环境变量，
   代理、校验库等沿用正式门禁传入的同一份环境。
3. ``go mod verify``：不联网，校验缓存里已有模块的内容与 go.sum 一致，必须退出 0 且输出 all modules verified。
   注意它只核对“已经在缓存里的模块”：实测（Go 1.26.6）当某个模块只有 go.mod 记录、压缩包从未下载时，
   它照样输出 all modules verified，所以它不证明缓存完整；完整性由第 2 步成功来保证，两步缺一不可。
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
# 回执只新增字段（module_download、module_verify、go_sum_sha256），旧字段含义不变，所以版本号不升。
SCHEMA = 'entry-go-compile-warmup/v1'

# 三步各自的内部超时（秒）。它们之和再加余量必须不超过调度器给整个预热单元的超时（见 UNIT_TIMEOUT_SECONDS）：
# 内部超时先触发，才会留下带失败原因的收据；调度器强杀进程则来不及写收据。
BUILD_TIMEOUT_SECONDS = 1800
# go mod download：ARM64 上 H4 缓存副本联网补齐只用约 1 秒、增量约 100MB。即使出口只有 1MB/s，
# 100MB 也只要约 100 秒，420 秒留约 4 倍余量。
MODULE_DOWNLOAD_TIMEOUT_SECONDS = 420
# go mod verify：本机离线实测真实 backend 约 2 秒；ARM64 磁盘较慢，按 10 倍保守估计也只要约 20 秒，120 秒留约 6 倍余量。
MODULE_VERIFY_TIMEOUT_SECONDS = 120
# 预热单元（调度器侧）总超时 = 1800 + 420 + 120 + 60。最后的 60 秒是进程启动、日志与收据落盘的余量，
# 沿用原来 1860 = 1800 + 60 的那 60 秒。原来只有编译一步，现在多了下载和校验两步，所以由 1860 放宽到 2400。
UNIT_TIMEOUT_SECONDS = 2400

MODULE_DOWNLOAD_COMMAND = ('go', 'mod', 'download')
MODULE_VERIFY_COMMAND = ('go', 'mod', 'verify')
# go mod verify 成功时单独占一行输出的文字；判据见 module_verified。
MODULE_VERIFIED_LINE = 'all modules verified'


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


def module_verified(returncode, output):
    """go mod verify 通过的判据：退出码为 0，并且输出里有一整行恰好是 all modules verified。

    退出码和输出两者都要满足：只看退出码会放过“什么都没校验”的静默成功（例如 go 被换成空壳程序），
    只看文字则可能被失败时夹带的同名文字误导。按整行比较，是为了拒绝 not all modules verified 之类的变体；
    允许它前面有别的提示行（例如 go: downloading），因为真实工具链可能在补取 go.mod 时先打印那类行。
    """
    if returncode != 0:
        return False
    return any(line.strip() == MODULE_VERIFIED_LINE for line in output.decode('utf-8', 'replace').splitlines())


def run_module_step(report, key, command, *, cwd, environment, log, timeout, digest_key, initial=None):
    """运行一个模块缓存步骤：输出（含 stderr）写入独立日志，命令、退出码、耗时与日志摘要登记到 report[key]。

    登记项在运行之前就挂到 report 上，超时或异常时收据里仍有命令和已写出的那部分日志，便于取证。
    每步一份独立日志，摘要才能各自核对；编译日志仍是原来的 .log，不混入这里的输出，原有 log 字段含义不变。
    返回日志全文，供调用方判定输出内容（go mod verify 需要）。
    """
    record = report[key] = {'command': list(command), 'returncode': None, **(initial or {})}
    began = time.monotonic()
    created = False
    data = b''
    try:
        # 独占创建且不跟随符号链接，权限一开始就是 0600：日志是取证材料，不覆盖已有文件，也没有先宽后窄的权限窗口。
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, 'wb') as handle:
            result = subprocess.run(list(command), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        record['returncode'] = result.returncode
    finally:
        record['elapsed_seconds'] = time.monotonic() - began
        if created:
            # 只登记本函数自己创建的日志，不把别人遗留的同名文件当成本步骤的证据。
            data = log.read_bytes()
            record[digest_key] = {'path': str(log), 'sha256': digest(data)}
    return data


def prepare(*, tree, work, manifest, output, mode='auto', environment=None, timeout=BUILD_TIMEOUT_SECONDS,
            download_timeout=MODULE_DOWNLOAD_TIMEOUT_SECONDS, verify_timeout=MODULE_VERIFY_TIMEOUT_SECONDS):
    """每份门禁运行只调用一次；编译失败、模块下载或校验失败、来源漂移、外部缓存均在派发测试前拒绝。

    timeout 仍是 go build 的超时；download_timeout、verify_timeout 分别约束后两步，三者之和见 UNIT_TIMEOUT_SECONDS。
    """
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
        # 模块缓存的内容由 go.sum 约束，这里单独登记它的摘要，供消费者核验“缓存初态由 go.sum 约束”。
        # 真实仓库里 go.sum 是 backend 下的受跟踪文件，已被上面的源码绑定覆盖，编译之后、下载与校验之后会各核对一次。
        # 没有 go.sum 只可能出现在没有外部依赖的模块里（有依赖却缺 go.sum 时，go build 要么报缺条目，
        # 要么补写出 go.sum 而被源码变化检查拦下），此时记为 null，不拒绝。
        go_sum = tree / 'backend/go.sum'
        report['go_sum_sha256'] = digest(go_sum.read_bytes()) if go_sum.is_file() else None
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
        # 第二步：go mod download 联网把构建清单里全部模块的压缩包补齐（含仅 go test 才用得上的依赖），
        # 之后的命令单元要求运行期离线，缓存必须在这里一次补完。联网只发生在这一步：
        # 这里不设置也不改写任何 Go 网络相关环境变量，代理等沿用正式门禁传入的同一份环境；
        # 同一份环境、同一组缓存目录、同一个工作目录，补齐的模块才会落进后面命令实际使用的缓存。
        run_module_step(report, 'module_download', MODULE_DOWNLOAD_COMMAND, cwd=tree / 'backend', environment=environment,
                        log=output.with_suffix('.mod-download.log'), timeout=download_timeout, digest_key='log')
        if report['module_download']['returncode']:
            raise ValueError('Go 模块下载失败（go mod download 退出码 %d），正式门禁尚未派发'
                             % report['module_download']['returncode'])
        # 第三步：go mod verify 不联网，校验缓存里已有模块的内容与 go.sum 一致。退出码和输出两者为准，
        # 见 module_verified。它不证明缓存完整（从未下载的模块它不会报错），完整性靠上一步的下载成功保证。
        output_bytes = run_module_step(report, 'module_verify', MODULE_VERIFY_COMMAND, cwd=tree / 'backend',
                                       environment=environment, log=output.with_suffix('.mod-verify.log'),
                                       timeout=verify_timeout, digest_key='output', initial={'verified': False})
        report['module_verify']['verified'] = module_verified(report['module_verify']['returncode'], output_bytes)
        if not report['module_verify']['verified']:
            code = report['module_verify']['returncode']
            reason = '退出码 %d' % code if code else '输出里没有单独一行 ' + MODULE_VERIFIED_LINE
            raise ValueError('Go 模块校验失败（go mod verify %s），正式门禁尚未派发' % reason)
        # 下载与校验之后再核对一次源码：go mod download 在 -mod=mod 之类的环境下可能改写 go.mod 或 go.sum，
        # 那样缓存就不是由登记的 go.sum 约束出来的了，必须拒绝。
        if source_binding(tree) != before:
            raise ValueError('模块下载或校验期间后端源码发生变化')
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
    # 单元超时 UNIT_TIMEOUT_SECONDS = 2400（原 1860，只有编译一步）：编译 1800 + 下载 420 + 校验 120 + 余量 60，
    # 各项的依据见文件开头的常量说明。它必须大于 prepare 里三步内部超时之和，内部超时才会先触发并留下失败收据。
    payload = {'schema_version': 'unit-executor-commands/v1', 'units': [{
        'unit_id': 'prepare:go-compile', 'argv': command, 'cwd': str(Path(tree).resolve()),
        'cores': 2, 'memory_mb': 5120, 'exclusive': True, 'timeout_seconds': UNIT_TIMEOUT_SECONDS,
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
