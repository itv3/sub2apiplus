"""tcpdump、mitmdump 与全局锁的安全生命周期。"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import ipaddress
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from .model import CaptureCase, ConfigurationError
from .security import argv_manifest_view, ensure_private_directory


PR_SET_PDEATHSIG = 1


class CaptureProcessError(RuntimeError):
    """抓包子进程未按约束启动或停止。"""


class CaptureCleanupError(CaptureProcessError):
    """抓包进程或端口未能恢复干净。"""


class CampaignLock:
    """阻止 OAuth/API 或两个 case 同时污染同一容器样本。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: IO[str] | None = None

    def __enter__(self) -> "CampaignLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise CaptureProcessError("拒绝使用符号链接锁文件。")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            os.close(descriptor)
            raise CaptureProcessError("锁文件必须归当前用户所有且为独立普通文件。")
        if metadata.st_mode & 0o777 != 0o600:
            os.close(descriptor)
            raise CaptureProcessError("已有锁文件权限必须为 0600。")
        self.stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.stream.close()
            self.stream = None
            raise CaptureProcessError("已有官方客户端抓包任务运行。") from error
        self.stream.seek(0)
        existing_content = self.stream.read()
        if existing_content and not existing_content.startswith(
            "official-client-capture-lock/v1\n"
        ):
            self.stream.close()
            self.stream = None
            raise CaptureProcessError("已有锁文件不属于本工具，拒绝覆盖。")
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write("official-client-capture-lock/v1\n")
        self.stream.write(f"pid={os.getpid()}\n")
        self.stream.flush()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.stream:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


def resolve_target_addresses(
    target_hosts: tuple[str, ...],
    *,
    target_port: int = 443,
    resolver: Any = socket.getaddrinfo,
) -> tuple[str, ...]:
    """在启动 direct 抓包前解析并校验目标 IP，避免域名或参数注入 BPF。"""

    if not target_hosts:
        raise ConfigurationError("direct 抓包缺少目标主机。")
    if not 1 <= target_port <= 65535:
        raise ConfigurationError("direct 目标端口超出合法范围。")

    addresses: set[str] = set()
    for host in target_hosts:
        try:
            results = resolver(
                host,
                target_port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except (OSError, socket.gaierror) as error:
            raise CaptureProcessError(f"无法解析 direct 目标主机：{host}") from error

        host_addresses: set[str] = set()
        for family, _socket_type, _protocol, _canonical_name, socket_address in results:
            if family not in {socket.AF_INET, socket.AF_INET6} or not socket_address:
                continue
            raw_address = str(socket_address[0]).split("%", maxsplit=1)[0]
            try:
                normalized = str(ipaddress.ip_address(raw_address))
            except ValueError:
                continue
            host_addresses.add(normalized)
        if not host_addresses:
            raise CaptureProcessError(f"direct 目标主机没有可用的 IP 地址：{host}")
        addresses.update(host_addresses)

    return tuple(
        sorted(
            addresses,
            key=lambda value: (
                ipaddress.ip_address(value).version,
                int(ipaddress.ip_address(value)),
            ),
        )
    )


def _direct_bpf(target_addresses: tuple[str, ...], target_port: int) -> str:
    """构造 tcpdump 过滤器。

    ⚠ **按 host 过滤会让"只访问某域名"这类命题变成循环论证。**
    本函数默认按目标 IP 过滤（省流量），但验证 SPEC-EP-002 那类**全称命题**时
    必须设 `CAPTURE_BPF_ALL_HOSTS=1`——只按端口抓，才可能看到
    api.openai.com / auth.openai.com 之类的其他域名。

    2026-07-28 之前 SPEC-EP-002 的 pcap 证据就栽在这里：BPF 写死
    `host <chatgpt.com 的 4 个 IP>`，那份 pcap 物理上记录不到别的域名，
    用它证明"只访问 chatgpt.com"永远为真。
    """

    if os.environ.get("CAPTURE_BPF_ALL_HOSTS") == "1":
        if not 1 <= target_port <= 65535:
            raise ConfigurationError("direct 目标端口超出合法范围。")
        return f"tcp port {target_port}"

    if not target_addresses:
        raise ConfigurationError("direct 抓包缺少已解析的目标 IP。")
    if not 1 <= target_port <= 65535:
        raise ConfigurationError("direct 目标端口超出合法范围。")
    normalized: list[str] = []
    for value in target_addresses:
        try:
            address = str(ipaddress.ip_address(value))
        except ValueError as error:
            raise ConfigurationError("direct 目标 IP 格式不合法。") from error
        if address not in normalized:
            normalized.append(address)
    hosts = " or ".join(f"host {address}" for address in normalized)
    return f"tcp port {target_port} and ({hosts})"


def _case_target_port(case: CaptureCase) -> int:
    """从已校验的 API Base URL 读取端口；官方 OAuth 固定使用 443。"""

    if not case.base_url:
        return 443
    try:
        parsed_port = urllib.parse.urlsplit(case.base_url).port
        return 443 if parsed_port is None else parsed_port
    except ValueError as error:
        raise ConfigurationError("API Base URL 的端口格式不合法。") from error


def _arm_linux_parent_death_signal(expected_parent_pid: int) -> None:
    """让 Linux 子进程在当前 Python 父进程退出时收到 SIGTERM。"""

    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise OSError("当前 Linux 运行库不支持 prctl。")
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)
    # Popen 发生在父进程短暂屏蔽 SIGINT/SIGTERM 的登记窗口内，子进程会继承
    # 该 mask。PDEATHSIG 武装完成后必须清空，否则正常 stop 信号无法生效。
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_SETMASK, set())


def _popen_safety_options(platform_name: str | None = None) -> dict[str, Any]:
    """隔离进程组，并在 Linux 上补充父进程死亡联动。"""

    options: dict[str, Any] = {"start_new_session": True}
    current_platform = platform_name or sys.platform
    if current_platform.startswith("linux"):
        expected_parent_pid = os.getpid()

        def arm_parent_death_signal() -> None:
            _arm_linux_parent_death_signal(expected_parent_pid)

        options["preexec_fn"] = arm_parent_death_signal
    elif hasattr(signal, "pthread_sigmask"):

        def clear_inherited_signal_mask() -> None:
            signal.pthread_sigmask(signal.SIG_SETMASK, set())

        options["preexec_fn"] = clear_inherited_signal_mask
    return options


def process_command(
    *,
    case: CaptureCase,
    output_dir: Path,
    tcpdump_bin: str,
    mitmdump_bin: str,
    mitm_addon: Path,
    mitm_confdir: Path,
    mitm_port: int,
    interface: str,
    target_addresses: tuple[str, ...] = (),
    target_port: int = 443,
) -> list[str]:
    """生成不含凭据的抓包命令，供运行与单元测试共用。"""

    if case.evidence == "direct":
        return [
            tcpdump_bin,
            "-i",
            interface,
            "-U",
            "-s",
            "0",
            "-w",
            str(output_dir / "traffic.pcap"),
            _direct_bpf(target_addresses, target_port),
        ]
    return [
        mitmdump_bin,
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(mitm_port),
        "--set",
        f"confdir={mitm_confdir}",
        "--set",
        "block_global=false",
        "-s",
        str(mitm_addon),
    ]


def _port_is_open(host: str, port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((host, port)) == 0


def ensure_mitm_port_available(port: int) -> None:
    """确认 MITM 端口未被占用，占用即抛错。

    两处共用：开跑前的整轮预检（capture.py 的 _preflight_dependencies）与单个 mitm
    case 启动前的兜底检查（CaptureProcess.start）。此前只有后者，而 mitm case 排在
    队列中间，撞上时前面的 direct case 已经跑掉——前轮残留的 mitmdump 占着端口，要到
    采集中途才暴露（k61 因此报废一轮）。预检提前到整轮开跑前，占用就拒绝启动。
    """

    if _port_is_open("127.0.0.1", port):
        raise CaptureProcessError(f"MITM 端口 {port} 已被其他进程占用。")


class CaptureProcess:
    """只管理由当前 Python 进程直接创建的抓包子进程。"""

    def __init__(
        self,
        *,
        case: CaptureCase,
        output_dir: Path,
        command: list[str],
        environment: dict[str, str],
        mitm_port: int,
        metadata: dict[str, Any] | None = None,
        popen_factory: Any = subprocess.Popen,
    ) -> None:
        self.case = case
        self.output_dir = output_dir
        self.command = command
        self.environment = environment
        self.mitm_port = mitm_port
        self.metadata = dict(metadata or {})
        self.popen_factory = popen_factory
        self.process: subprocess.Popen[bytes] | None = None
        self.log_stream: IO[bytes] | None = None
        self.cleanup_successful = True

    def start(self) -> None:
        """启动进程，并验证 direct 存活或 MITM 端口由本进程启动后就绪。"""

        if self.process is not None:
            raise CaptureProcessError("同一个抓包进程不能重复启动。")
        if self.output_dir.exists():
            raise CaptureProcessError(f"输出目录已存在，拒绝覆盖：{self.output_dir}")
        ensure_private_directory(self.output_dir)
        log_path = self.output_dir / (
            "tcpdump.log" if self.case.evidence == "direct" else "mitmdump.log"
        )
        descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.log_stream = os.fdopen(descriptor, "wb", buffering=0)

        if self.case.evidence == "mitm":
            try:
                ensure_mitm_port_available(self.mitm_port)
            except CaptureProcessError:
                self.log_stream.close()
                self.log_stream = None
                raise

        try:
            safety_options = _popen_safety_options()
            self.process = self.popen_factory(
                self.command,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log_stream,
                stderr=subprocess.STDOUT,
                **safety_options,
            )
            if self.case.evidence == "direct":
                time.sleep(0.5)
                if self.process.poll() is not None:
                    raise CaptureProcessError("tcpdump 启动后立即退出。")
                return

            for _ in range(100):
                if self.process.poll() is not None:
                    raise CaptureProcessError("mitmdump 启动后立即退出。")
                if _port_is_open("127.0.0.1", self.mitm_port):
                    return
                time.sleep(0.05)
            raise CaptureProcessError("mitmdump 未在限定时间内监听指定端口。")
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """按 INT/TERM/KILL 阶梯结束当前拥有的进程，并确认退出。"""

        process = self.process
        if process is None:
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
            return
        try:
            if process.poll() is None:
                first_signal = (
                    signal.SIGINT if self.case.evidence == "direct" else signal.SIGTERM
                )
                try:
                    os.killpg(process.pid, first_signal)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired as error:
                            self.cleanup_successful = False
                            raise CaptureCleanupError("抓包进程无法终止。") from error
            if self.case.evidence == "mitm" and _port_is_open(
                "127.0.0.1", self.mitm_port
            ):
                self.cleanup_successful = False
                raise CaptureCleanupError("mitmdump 已退出，但监听端口仍未释放。")
        except BaseException:
            self.cleanup_successful = False
            raise
        finally:
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
            self.process = None if process.poll() is not None else process

    def __enter__(self) -> "CaptureProcess":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def build_capture_process(
    *,
    fault_spec: str = "",
    case: CaptureCase,
    output_dir: Path,
    base_environment: dict[str, str],
    tcpdump_bin: str,
    mitmdump_bin: str,
    mitm_addon: Path,
    mitm_confdir: Path,
    mitm_port: int,
    interface: str,
    scenario: str | None = None,
    address_resolver: Any = socket.getaddrinfo,
) -> CaptureProcess:
    """创建带动态目标 allowlist 的 direct 或 forward-MITM 进程。"""

    if not 1 <= mitm_port <= 65535:
        raise ConfigurationError("MITM 端口超出合法范围。")
    environment = dict(base_environment)
    if case.evidence == "mitm":
        environment.update(
            {
                "CAPTURE_TASK": case.task,
                "CAPTURE_BOUNDARY": case.boundary,
                "CAPTURE_RUN_ID": case.run_id,
                "CAPTURE_SUBJECT": case.subject,
                "CAPTURE_SCENARIO": scenario or "unspecified",
                "CAPTURE_TARGET_HOSTS": ",".join(case.target_hosts),
                "CAPTURE_OUTPUT_DIR": str(output_dir),
            }
        )
        # 受控故障注入只在显式声明时生效；空值等价于不注入。
        if fault_spec:
            environment["CAPTURE_FAULT_SPEC"] = fault_spec
    target_port = _case_target_port(case)
    target_addresses = (
        resolve_target_addresses(
            case.target_hosts,
            target_port=target_port,
            resolver=address_resolver,
        )
        if case.evidence == "direct"
        else ()
    )
    command = process_command(
        case=case,
        output_dir=output_dir,
        tcpdump_bin=tcpdump_bin,
        mitmdump_bin=mitmdump_bin,
        mitm_addon=mitm_addon,
        mitm_confdir=mitm_confdir,
        mitm_port=mitm_port,
        interface=interface,
        target_addresses=target_addresses,
        target_port=target_port,
    )
    metadata = {
        "interface": interface,
        "target_hosts": list(case.target_hosts),
        "target_port": target_port,
        "target_addresses": list(target_addresses),
        "bpf": command[-1] if case.evidence == "direct" else None,
        "mitm_port": mitm_port if case.evidence == "mitm" else None,
        "invocation": argv_manifest_view(command),
    }
    return CaptureProcess(
        case=case,
        output_dir=output_dir,
        command=command,
        environment=environment,
        mitm_port=mitm_port,
        metadata=metadata,
    )
