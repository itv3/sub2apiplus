"""声明级承接采样器测试用的真实 strace 样本（ARM64 摸底，2026-10-14，strace 6.8，go test ./internal/pkg/...）。

每一段都是从冷缓存样本里按原始行号抽出的连续行，原样保留（含采样机上的真实路径）：

* STARTUP：第 1 到 60 行，go 命令启动阶段。execve 的参数是指针而不是字符串数组（当时用了 -e verbose=none），
  还有 6 条 AT_SYMLINK_NOFOLLOW 的 newfstatat（返回结构被缩略，严格解析会记 symlink-metadata-not-bound）；
* DIRECTORY_LISTING：第 95 到 98 行，getdents64 目录枚举；
* DETACHED_WINDOW、DETACHED_WINDOW_LATE：线程在系统调用中途被分离，strace 打印的 ``???( <detached ...>`` 标记行；
* UNFINISHED_WINDOW：含 ``???( <unfinished ...>`` 标记行，以及两对完整的 openat unfinished 与 resumed。

它们不含任何密钥或用户数据；路径里只有采样根、Go 工具链和 go-build 临时目录。
"""

from __future__ import annotations

ROOT = "/var/tmp/declared-probe-20261014t120210z"
TREE = ROOT + "/test-tree"

STARTUP = """\
3106409 execve("/usr/local/go/bin/go", 0xffffd2a979c8, 0xffffd2a979f0) = 0
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/sys/kernel/mm/transparent_hugepage/hpage_pmd_size", O_RDONLY) = 3</sys/kernel/mm/transparent_hugepage/hpage_pmd_size>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/proc/self/cgroup", O_RDONLY|O_CLOEXEC) = 3</proc/3106409/cgroup>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/proc/self/mountinfo", O_RDONLY|O_CLOEXEC) = 3</proc/3106409/mountinfo>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/sys/fs/cgroup/user.slice/user-0.slice/session-30658.scope/cpu.max", O_RDONLY|O_CLOEXEC) = 3</sys/fs/cgroup/user.slice/user-0.slice/session-30658.scope/cpu.max>
3106409 clone(child_stack=0x66fbb67cc000, flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM) = 3106411
3106409 clone(child_stack=0x66fbb6808000, flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM) = 3106414
3106409 clone(child_stack=0x66fbb6804000, flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM) = 3106415
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/env", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
3106409 readlinkat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/proc/self/exe", "/usr/local/go/bin/go", 128) = 20
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go/pkg/tool", 0x66fbb67bc0f8, 0) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "", 0x66fbb67bc1b8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go", 0x66fbb67bc278, 0) = 0
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go/go.env", O_RDONLY|O_CLOEXEC) = 4</usr/local/go/go.env>
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go/bin/gcc", 0x66fbb67bc3f8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/opt/node-v20/bin/gcc", 0x66fbb67bc4b8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/sbin/gcc", 0x66fbb67bc578, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/bin/gcc", 0x66fbb67bc638, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/sbin/gcc", 0x66fbb67bc6f8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/bin/gcc", 0x66fbb67bc7b8, 0) = 0
3106409 faccessat2(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/bin/gcc", X_OK, AT_EACCESS) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go/bin/gccgo", 0x66fbb67bc878, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/opt/node-v20/bin/gccgo", 0x66fbb67bc938, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/sbin/gccgo", 0x66fbb67bc9f8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/bin/gccgo", 0x66fbb67bcab8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/sbin/gccgo", 0x66fbb67bcb78, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/bin/gccgo", 0x66fbb67bcc38, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/sbin/gccgo", 0x66fbb67bccf8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/bin/gccgo", 0x66fbb67bcdb8, 0) = -1 ENOENT (No such file or directory)
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/mode", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/mode", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local/weekends", O_RDONLY|O_CLOEXEC) = 4</root/.config/go/telemetry/local/weekends>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local/weekends", O_RDONLY|O_CLOEXEC) = 4</root/.config/go/telemetry/local/weekends>
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local", 0x66fbb67bcff8, 0) = 0
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local/go@go1.27.1-go1.27.1-linux-arm64-2026-10-14.v1.count", O_RDWR|O_CREAT|O_CLOEXEC, 0666) = 4</root/.config/go/telemetry/local/go@go1.27.1-go1.27.1-linux-arm64-2026-10-14.v1.count>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/mode", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local", 0x66fbb67bd238, 0) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/local/upload.token", 0x66fbb67bd2f8, 0) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/usr/local/go", 0x66fbb67bd3b8, 0) = 0
3106409 getcwd("/var/tmp/declared-probe-20261014t120210z/test-tree/backend", 4096) = 59
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/backend/go.work", 0x66fbb67bd478, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/go.work", 0x66fbb67bd538, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go.work", 0x66fbb67bd5f8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/go.work", 0x66fbb67bd6b8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/go.work", 0x66fbb67bd778, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/go.work", 0x66fbb67bd838, 0) = -1 ENOENT (No such file or directory)
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/root/.config/go/telemetry/mode", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/backend/go.work", 0x66fbb67bd9b8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/go.work", 0x66fbb67bda78, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go.work", 0x66fbb67bdb38, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/go.work", 0x66fbb67bdbf8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/go.work", 0x66fbb67bdcb8, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/go.work", 0x66fbb67bdd78, 0) = -1 ENOENT (No such file or directory)
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/backend/go.mod", 0x66fbb67bde38, 0) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var", 0x66fbb6876038, AT_SYMLINK_NOFOLLOW) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp", 0x66fbb68760f8, AT_SYMLINK_NOFOLLOW) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z", 0x66fbb68761b8, AT_SYMLINK_NOFOLLOW) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree", 0x66fbb6876278, AT_SYMLINK_NOFOLLOW) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/test-tree/backend", 0x66fbb6876338, AT_SYMLINK_NOFOLLOW) = 0
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/tmp", 0x66fbb68763f8, AT_SYMLINK_NOFOLLOW) = 0
"""

DIRECTORY_LISTING = """\
3106409 getdents64(7</var/tmp/declared-probe-20261014t120210z/test-tree/backend/internal/pkg>, 0x66fbb699a000, 8192) = 1088
3106409 getdents64(7</var/tmp/declared-probe-20261014t120210z/test-tree/backend/internal/pkg>, 0x66fbb699a000, 8192) = 0
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "./internal/pkg/", O_RDONLY|O_CLOEXEC|O_DIRECTORY) = 7</var/tmp/declared-probe-20261014t120210z/test-tree/backend/internal/pkg>
3106409 getdents64(7</var/tmp/declared-probe-20261014t120210z/test-tree/backend/internal/pkg>, 0x66fbb699a000, 8192) = 1088
"""

DETACHED_WINDOW = """\
3106942 newfstatat(AT_FDCWD</usr/local/go/src/internal/abi>, ".", 0x111814da6c38, 0) = 0
3106942 newfstatat(AT_FDCWD</usr/local/go/src/internal/abi>, "/usr/local/go/src/internal/abi", 0x111814da6cf8, 0) = 0
3106942 openat(AT_FDCWD</usr/local/go/src/internal/abi>, "/usr/local/go/src/internal/abi/stub.s", O_RDONLY|O_CLOEXEC) = 8</usr/local/go/src/internal/abi/stub.s>
3106942 newfstatat(AT_FDCWD</usr/local/go/src/internal/abi>, ".", 0x111814da6db8, 0) = 0
3106942 newfstatat(AT_FDCWD</usr/local/go/src/internal/abi>, "/usr/local/go/src/internal/abi", 0x111814da6e78, 0) = 0
3106947 ???( <detached ...>
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/_pkg_.a", 0x66fbb6abf3b8, 0) = 0
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/_pkg_.a", O_WRONLY|O_APPEND|O_CLOEXEC) = 9</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/_pkg_.a>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/abi_test.o", O_RDONLY|O_CLOEXEC) = 14</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/abi_test.o>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/stub.o", O_RDONLY|O_CLOEXEC) = 14</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b005/stub.o>
"""

UNFINISHED_WINDOW = """\
3110285 openat(AT_FDCWD</usr/local/go/src/testing>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b031/vet.out", O_RDONLY|O_CLOEXEC) = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b031/vet.out>
3110295 openat(AT_FDCWD</usr/local/go/src/encoding/json/internal/jsonwire>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b110/vet.out", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666 <unfinished ...>
3110285 openat(AT_FDCWD</usr/local/go/src/testing>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b069/vet.out", O_RDONLY|O_CLOEXEC) = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b069/vet.out>
3110295 <... openat resumed>)           = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b110/vet.out>
3110285 openat(AT_FDCWD</usr/local/go/src/testing>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b073/vet.out", O_RDONLY|O_CLOEXEC <unfinished ...>
3110293 ???( <unfinished ...>
3110285 <... openat resumed>)           = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b073/vet.out>
3110285 openat(AT_FDCWD</usr/local/go/src/testing>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b074/vet.out", O_RDONLY|O_CLOEXEC) = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b074/vet.out>
3106409 openat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b110/vet.out", O_RDONLY|O_CLOEXEC) = 7</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b110/vet.out>
3106409 newfstatat(AT_FDCWD</var/tmp/declared-probe-20261014t120210z/test-tree/backend>, "/var/tmp/declared-probe-20261014t120210z/gc-c/e3/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855-d", 0x66fbb6f9e878, 0) = 0
"""

DETACHED_WINDOW_LATE = """\
3118208 openat(AT_FDCWD</usr/local/go/src/text/tabwriter>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b064/vet.out", O_RDONLY|O_CLOEXEC) = 8</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b064/vet.out>
3118208 openat(AT_FDCWD</usr/local/go/src/text/tabwriter>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b039/vet.out", O_RDONLY|O_CLOEXEC) = 8</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b039/vet.out>
3118208 openat(AT_FDCWD</usr/local/go/src/text/tabwriter>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b045/vet.out", O_RDONLY|O_CLOEXEC) = 8</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b045/vet.out>
3118208 openat(AT_FDCWD</usr/local/go/src/text/tabwriter>, "/var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b131/vet.out", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 1</var/tmp/declared-probe-20261014t120210z/go-tmp/go-build820955247/b131/vet.out>
3118211 ???( <detached ...>
"""

ALL_SEGMENTS = (STARTUP, DIRECTORY_LISTING, DETACHED_WINDOW, UNFINISHED_WINDOW, DETACHED_WINDOW_LATE)

# ---------------------------------------------------------------------------
# 步骤 5 的 WP4c：strace 对“中途消失的线程”打印的垃圾行（ARM64，strace 6.8，egress 自测，2026-10-14 的“一份落盘、一份喂过滤器”对照实验）。
# Go 运行时的线程被杀或退出时，strace 把它当作还在系统调用里，用残留寄存器解码出一行名字、参数都乱七八糟的东西。四种形态各取一段原样的
# 真实行（含前后相邻的正常行；每行一个原始字符串，反斜杠原样保留），不在 ALL_SEGMENTS 里（那组断言钉的是旧行为）：
#   A：名字是随机的系统调用名，以 <detached ...> 收尾；  B：以 “ = ?” 收尾，第一个参数是指针常量、第二个参数恰好像路径；
#   C：路径参数是指针的 <unfinished ...> 起始行，永远没有 resumed（按 unfinished-calls-at-eof 容忍）；  D：不带名字的裸标记。
# ---------------------------------------------------------------------------

# 形态 A：第 3 行是 lsetxattr 垃圾行，以 <detached ...> 结尾（rawtext-2 第 296833 到 296837 行）；第 4 行是 pid 711354 悬着不再回来的 newfstatat（路径参数有效，但永远没有 resumed）
GARBLED_DETACHED_A = "\n".join([
    r'711354 newfstatat(AT_FDCWD</tmp/egressscan-selftest753375486/mutation0>, "/var/tmp/declared-probe-cmds-20261014t125522z/hunt/gc-f2/65/659b16cba3f504b3e9ab34d6742f8329f8962daa141d2526e728ea6214f45557-d", 0x60f8db85b3b8, 0) = 0',
    r'711354 openat(AT_FDCWD</tmp/egressscan-selftest753375486/mutation0>, "/var/tmp/declared-probe-cmds-20261014t125522z/hunt/gc-f2/65/659b16cba3f504b3e9ab34d6742f8329f8962daa141d2526e728ea6214f45557-d", O_RDONLY|O_CLOEXEC) = 6</var/tmp/declared-probe-cmds-20261014t125522z/hunt/gc-f2/65/659b16cba3f504b3e9ab34d6742f8329f8962daa141d2526e728ea6214f45557-d>',
    r'711353 lsetxattr("\270J\321", 0x17, 0x23fdca6b7008, 39572929737628, XATTR_CREATE <detached ...>',
    r'711354 newfstatat(AT_FDCWD</tmp/egressscan-selftest753375486/mutation0>, "/usr/local/go/src/vendor/golang.org/x/net/http/httpguts",  <unfinished ...>',
    r'711357 openat(AT_FDCWD</tmp/egressscan-selftest753375486/mutation0>, "/usr/local/go/src/bufio", O_RDONLY|O_CLOEXEC|O_DIRECTORY) = 6</usr/local/go/src/bufio>',
]) + "\n"

# 形态 B：第 2 行是 lsetxattr 垃圾行，以 = ? 结尾，第一个参数是指针常量、第二个参数恰好像路径（rawtext-5 第 28586 行及其前后的相邻行；第 28585 行是起始行在窗口外的 resumed，略去）
GARBLED_LOST_RESULT_B = "\n".join([
    r'751442 newfstatat(AT_FDCWD</usr/local/go/src/internal/abi>, "/usr/local/go/src/internal/abi", 0xa5a2750e78, 0) = 0',
    r'751440 lsetxattr(0xffffffffffffff00, "/usr/local/go/src/internal/abi", "", 0, 0) = ?',
    r'751274 utimensat(AT_FDCWD</var/tmp/declared-probe-cmds-20261014t125522z/test-tree/backend>, "/var/tmp/declared-probe-cmds-20261014t125522z/hunt/gc-f5/1b/1bd0e7b814a828e09535fb001f8a91c34b4494f123abb888db0c224bd74ab91b-d", 0x19e4988f11f8, 0) = 0',
    r'751169 newfstatat(AT_FDCWD</var/tmp/declared-probe-cmds-20261014t125522z/test-tree/backend>, "/var/tmp/declared-probe-cmds-20261014t125522z/hunt/go-tmp-f5/go-build1331692528/b005/_pkg_.a",  <unfinished ...>',
]) + "\n"

# 形态 D：第 3 行是不带名字的裸标记 ???( <detached ...>（rawtext-4 第 33677 到 33681 行）
GARBLED_BARE_MARKER_D = "\n".join([
    r'736021 newfstatat(AT_FDCWD</usr/local/go/src/math>, ".", 0xec3ef1b8f38, 0) = 0',
    r'736021 newfstatat(AT_FDCWD</usr/local/go/src/math>, "/usr/local/go/src/math", 0xec3ef1b8ff8, 0) = 0',
    r'736020 ???( <detached ...>',
    r'735255 openat(AT_FDCWD</var/tmp/declared-probe-cmds-20261014t125522z/test-tree/backend>, "/dev/null", O_RDONLY|O_CLOEXEC) = 6</dev/null>',
    r'735255 newfstatat(AT_FDCWD</var/tmp/declared-probe-cmds-20261014t125522z/test-tree/backend>, "/usr/local/go/src/math", 0x14b876932278, 0) = 0',
]) + "\n"

# 形态 C：第 2 行 newfstatat 的路径参数是指针、永远没有 resumed（rawtext-4 第 198726 行）；第 1 行与第 4 行是一对正常配对的 newfstatat
GARBLED_UNFINISHED_POINTER_C = "\n".join([
    r'740777 newfstatat(AT_FDCWD</tmp/egressscan-selftest2393283390/case2>, "/usr/local/go/src/math/arith_s390x.go",  <unfinished ...>',
    r'740774 newfstatat(AT_FDCWD</tmp/egressscan-selftest2393283390/case2>, 0x1948d3defda0,  <unfinished ...>',
    r'740780 openat(AT_FDCWD</tmp/egressscan-selftest2393283390/case2>, "/usr/local/go/src/math/rand/v2", O_RDONLY|O_CLOEXEC|O_DIRECTORY <unfinished ...>',
    r'740777 <... newfstatat resumed>0x1948d3f075f8, AT_SYMLINK_NOFOLLOW) = 0',
]) + "\n"
