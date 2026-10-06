#!/usr/bin/env python3
"""只读核对本轮原生 WS 证据、测量口径、字节摘要及门禁结果。"""

import argparse
import gzip
import hashlib
import json
import statistics
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_rows(path):
    data = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
    return [json.loads(line) for line in data.decode().splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, help='可选：同时核对当前源码摘要')
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    matrix = read_rows(directory / 'results.jsonl')
    baseline = read_rows(directory / 'baseline-results.jsonl')
    performance = read_rows(directory / 'performance-results.jsonl')
    previous = read_rows(directory / 'performance-baseline-results.jsonl')
    require([len(rows) for rows in (matrix, baseline, performance, previous)] == [19, 7, 6, 6],
            '必须保留全部 38 场测量')
    for rows, name, repeats in ((matrix, 'cases.json', 1), (baseline, 'baseline-cases.json', 1),
                               (performance, 'performance-cases.json', 3), (previous, 'performance-cases.json', 3)):
        require([row['case'] for row in rows] == json.loads((directory / name).read_text()) * repeats,
                f'用例顺序或内容不符：{name}')
    fixtures = json.loads((directory / 'fixture-manifests.json').read_text())
    all_rows = matrix + baseline + performance + previous
    for row in all_rows:
        name = row['case']['name']
        require(row['container_state']['ExitCode'] == 0 and not row['container_state']['OOMKilled'],
                f'{name} 退出异常或 OOM')
        require(row['owned_memory_before_bytes'] == row['owned_memory_after_release_bytes'] == 0,
                f'{name} 基线或结束后仍持有系统内存映射')
        require(row['go_version'] == 'go1.27.1' and row['goarch'] == 'arm64' and row['gogc'] == 100
                and row['gomemlimit_mib'] == 512 and row['container_memory_limit_bytes'] == 768 << 20,
                f'{name} 实验运行参数不符')
        command = row['command']
        require(command[command.index('--cpus') + 1] == '2' and 'GOMAXPROCS=2' in command
                and command[command.index('--network') + 1] == 'none', f'{name} 容器参数不符')
        require(row['fixture_prepared_externally'] and row['cold_session'], f'{name} 样本或会话基线不符')
        turns, concurrency = row['requests_per_slot'], row['concurrency']
        manifest = fixtures['ws-fixtures/' + row['case']['ws_session_fixture_dir'] + '/manifest.json']
        require(row['source_body_sha256'] == manifest['source_body_sha256']
                and row['frame_sha256'] == manifest['frame_sha256'][:turns]
                and row['frame_bytes'] == manifest['frame_bytes'][:turns], f'{name} 固定样本不符')
        require(row['body_bytes_total'] == max(row['frame_bytes']) * concurrency, f'{name} 分母不符')
        ratio = row['request_memory_peak_bytes'] / row['body_bytes_total']
        require(abs(row['resident_multiple_with_body'] - ratio) <= 0.0051, f'{name} 倍率不符')
        if name.startswith('http-bridge-'):
            require(row['actual_transport'] == 'http_bridge' and row['ws_upstream_frames'] == 0
                    and row['http_upstream_requests'] == turns * concurrency, f'{name} 桥接计数不符')
            require(ratio <= 2.5 and row['large_body_target_met'], f'{name} 大正文目标回退')
        else:
            require(row['actual_transport'] == 'native_websocket' and row['http_upstream_requests'] == 0
                    and row['ws_upstream_frames'] == turns * concurrency, f'{name} 未实际经过原生 WS')
    matrix_index = {row['case']['name']: row for row in matrix}
    for current, old in list(zip(performance, previous)) + [(matrix_index[row['case']['name']], row) for row in baseline]:
        require(current['case'] == old['case'] and current['frame_sha256'] == old['frame_sha256']
                and current['body_bytes_total'] == old['body_bytes_total'], '新旧对照样本或配置不同')
    summary = json.loads((directory / 'summary.json').read_text())
    for name, comparison in summary['comparisons'].items():
        for version, rows in (('before', previous), ('after', performance)):
            selected = [row for row in rows if row['case']['name'] == name]
            require(len(selected) == 3, f'{name} 必须有三次独立测量')
            for key, value in comparison[version].items():
                require(statistics.median(row[key] for row in selected) == value, f'{name}/{key} 中位数不符')
    require(summary['native_websocket_meets_2_5'] is False, '原生 WS 不得误记为已达 2.5 倍')
    ci = json.loads((directory / 'ci-summary.json').read_text())
    records = read_rows(directory / 'ci-unit-records.jsonl')
    require(ci['status'] == 'passed' and ci['unit_count'] == len(records) == 48 and not ci['failed_units']
            and all(row['exit_code'] == 0 for row in records), 'CI 必须 48 项全通过')
    logs = {}
    for name in ('container-logs.jsonl.gz', 'ci-logs.jsonl.gz', 'validation-logs.jsonl.gz'):
        logs[name] = read_rows(directory / name)
        for row in logs[name]:
            require(hashlib.sha256(row['content'].encode()).hexdigest() == row['sha256'],
                    f'日志摘要不符：{row["name"]}')
    container_logs = {(row['measurement_label'], row['case']): row for row in logs['container-logs.jsonl.gz']}
    require(len(container_logs) == 38, '测量原始日志不完整')
    for row in all_rows:
        log = container_logs[(row['measurement_label'], row['case']['name'])]
        raw = [json.loads(line.split('MEMPROFILE_RESULT ', 1)[1]) for line in log['content'].splitlines()
               if 'MEMPROFILE_RESULT ' in line]
        require(len(raw) == 1 and all(row.get(key) == value for key, value in raw[0].items()),
                f'汇总结果与原始测量日志不同：{row["case"]["name"]}')
    ci_logs = {row['path']: row for row in logs['ci-logs.jsonl.gz']}
    require(len(ci_logs) == 48, 'CI 原始日志不完整')
    for row in records:
        require(ci_logs[row['log']['path']]['sha256'] == row['log']['sha256'], 'CI 记录与日志摘要不符')
    runtime = json.loads((directory / 'runtime.json').read_text())
    require(hashlib.sha256((directory / 'run_container_matrix.py').read_bytes()).hexdigest() == runtime['runner_sha256'],
            '容器执行器与实测版本不符')
    manifest = json.loads((directory / 'evidence-sha256.json').read_text())
    for name, digest in manifest.items():
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, f'证据摘要不符：{name}')
    if args.repository:
        for name, digest in json.loads((directory / 'source-sha256.json').read_text()).items():
            require(hashlib.sha256((args.repository / name).read_bytes()).hexdigest() == digest, f'源码摘要不符：{name}')
    print(json.dumps({'状态': '核对通过', '测量场次': len(all_rows), 'CI通过': len(records),
                      '原生WS达到2.5倍': False, '三次对照中位数': summary['comparisons']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
