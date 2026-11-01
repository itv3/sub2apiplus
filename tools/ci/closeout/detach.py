#!/usr/bin/env python3
"""本机（macOS 没有 setsid）脱离会话跑长任务：``Popen(start_new_session=True)``，输出进日志，结束时追加 ``EXIT=<rc>``。

做什么：把一条 shell 命令串放到新会话里后台运行，stdout/stderr 全进日志文件，命令结束后日志末行追加 EXIT=<退出码>。
输入：detach.py <日志文件> <shell 命令串>
  例：detach.py logs/x.log 'bash post_promo_mods.sh . logs/pp'
输出：起跑后等 3 秒并确认日志文件出现；打印“pid=<子进程 pid> log_exists=<True|False>”（监视用 pgrep -f '[x]xx'，别用会匹配自身的写法）。
何时用：closeout.sh 的 b13-prerun、b3-regen 的重封链等本机长任务（超过 10 分钟的命令都用它）。
不使用 nohup：nohup 会让子进程继承“忽略 SIGHUP”，依赖挂断检测的用例（test_campaign_exec_session_hangup_is_detected）必红。
"""
import os
import subprocess
import sys
import time


def main(argv):
    if len(argv) != 3:
        print("用法：detach.py <日志文件> <shell 命令串>", file=sys.stderr)
        return 2
    log, cmd = argv[1], argv[2]
    wrapped = f'( {cmd} ) > "{log}" 2>&1; echo "EXIT=$?" >> "{log}"'
    proc = subprocess.Popen(["bash", "-c", wrapped], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(3)
    print(f"pid={proc.pid} log_exists={os.path.exists(log)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
