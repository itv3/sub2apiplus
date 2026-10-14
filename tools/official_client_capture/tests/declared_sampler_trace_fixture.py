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
