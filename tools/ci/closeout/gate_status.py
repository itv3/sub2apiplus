#!/usr/bin/env python3
"""收尾全集门禁状态快照（只读，在 ARM64 上跑；closeout.sh 的 b15-gates-status 通过 ssh 调用）。

做什么
    汇总一次 ``codex_closeout.py gates`` 的当前状态：退出标记、进程是否存活、最近阶段标记、失败提示、
    统一执行器的单元完成数与非零退出单元、根盘占用百分比。
输入
    gate_status.py <收尾目录> [工作根，缺省 <收尾目录>/work-1]
    读取 <收尾目录>/gates-run.out（里面 exit= 行是门禁结束标记）、<工作根>/gates.log、<工作根>/gates/executor/{plan.json,events.jsonl}。
输出（每行一个字段，便于 grep 或监视器匹配）
    EXIT <exit=…>   ALIVE <pid|none>   MARK <最近阶段行>   LOGFAIL <失败提示行>   UNITS <完成>/<总数> BAD <非零数> <前几个单元>   DISK <根盘%>
何时用
    b14-gates-start 起跑之后，监视器每约 28 分钟看一次；覆盖终态（exit=）、进程消失、失败单元三种情况。
"""
import json
import os
import re
import shutil
import subprocess
import sys


def tail_match(path, pattern, n=1):
    """返回文件里匹配 pattern 的最后 n 行（文件不存在返回空列表）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle if re.search(pattern, line)]
    except FileNotFoundError:
        return []
    return lines[-n:]


def snapshot(closeout_dir, work):
    """生成状态快照的各行文本（不打印），便于单元测试。"""
    out = []
    for line in tail_match(closeout_dir + "/gates-run.out", r"^exit="):
        out.append("EXIT " + line)
    alive = subprocess.run(["pgrep", "-f", "[c]odex_closeout.py gates"], capture_output=True, text=True).stdout.split()
    out.append("ALIVE " + (alive[0] if alive else "none"))
    for line in tail_match(work + "/gates.log", r"^=== "):
        out.append("MARK " + line[:120])
    for line in tail_match(work + "/gates.log", r"未通过|FAILED|Traceback", 2):
        out.append("LOGFAIL " + line[:200])
    events_path = work + "/gates/executor/events.jsonl"
    done = bad = 0
    bad_units = []
    total = None
    try:
        with open(work + "/gates/executor/plan.json", encoding="utf-8") as handle:
            total = len(json.load(handle)["units"])
    except Exception:  # noqa: BLE001 - 计划文件还没写出来时只是不知道总数
        pass
    if os.path.exists(events_path):
        with open(events_path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("event") == "exit":
                    done += 1
                    if event.get("exit_code") != 0:
                        bad += 1
                        bad_units.append("%s(exit=%s,timeout=%s,signal=%s)" % (
                            event.get("unit"), event.get("exit_code"), event.get("timed_out"), event.get("signal")))
    out.append("UNITS %d/%s BAD %d %s" % (done, total, bad, " ".join(bad_units[:4])))
    usage = shutil.disk_usage("/")
    out.append("DISK %d" % (-(-usage.used * 100 // (usage.used + usage.free))))
    return out


def main(argv):
    if len(argv) < 2:
        print("用法：gate_status.py <收尾目录> [工作根，缺省 <收尾目录>/work-1]", file=sys.stderr)
        return 2
    closeout_dir = argv[1]
    work = argv[2] if len(argv) > 2 else closeout_dir + "/work-1"
    for line in snapshot(closeout_dir, work):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
