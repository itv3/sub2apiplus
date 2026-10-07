"""Claude 官方制品冻结（Claude 升级控制面设计 B2-01）的离线测试。

网络读取全部由替身提供：测试现场生成一把 ECDSA P-256 密钥，按 npm Registry 的签名口径
（``<package>@<version>:<integrity>``）给合成 tarball 签名，所以 sha512 SRI、sha1 与签名校验走的是真实代码路径；
合成的 ELF64 二进制只带冻结判定需要的 ELF 头与 ``PT_INTERP``。

复核（B5a-09）：``verify_freeze_receipt`` 对真实冻结流程的产物逐项通过且不写文件；本轮登记值（目标版本、目标二进制
摘要、冻结收据摘要）不符、两渠道不一致、冻结目录里任一登记文件被改、缺失、换成符号链接或登记路径越出冻结目录时
都拒绝；``verify`` 子命令与原有不带子命令的冻结用法并存。``freeze_fixture`` 供 plan 审计测试复用。
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from tools.official_client_capture import claude_fw_e
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_freeze as freeze_module

VERSION = "2.1.300"
PLATFORM_PACKAGE = "@anthropic-ai/claude-code-linux-arm64"
BASE = freeze_module.NATIVE_BASE_URL
GLIBC = "/lib/ld-linux-aarch64.so.1"
MUSL = "/lib/ld-musl-aarch64.so.1"


def _elf(interpreter: str | None, *, machine: int = 183, payload: bytes = b"bun-sea") -> bytes:
    """最小 ELF64 小端可执行文件：一个程序头（有解释器时为 PT_INTERP），其后是解释器字符串与填充内容。"""

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<HHI", header, 16, 2, machine, 1)
    struct.pack_into("<Q", header, 32, 64)  # e_phoff
    struct.pack_into("<HHHHHH", header, 52, 64, 56, 1, 0, 0, 0)
    program = bytearray(56)
    body = b""
    if interpreter is not None:
        body = interpreter.encode("ascii") + b"\0"
        struct.pack_into("<I", program, 0, freeze_module.PT_INTERP)
        struct.pack_into("<Q", program, 8, 64 + 56)
        struct.pack_into("<Q", program, 32, len(body))
    else:
        struct.pack_into("<I", program, 0, 1)  # PT_LOAD，不含解释器
    return bytes(header) + bytes(program) + body + payload


def _tarball(files: dict[str, bytes]) -> bytes:
    """确定性的 tar.gz：同样的文件每次打出逐字节相同的包。

    ``tarfile`` 的 ``w:gz`` 会把当前时间写进 gzip 头；Registry 替身每次请求都重新打包，packument 里的 integrity 与随后
    下载的 tarball 若恰好跨过秒边界，字节就对不上，测试因此偶发失败。这里显式用 ``GzipFile(mtime=0)``；tar 成员的
    ``TarInfo.mtime`` 缺省就是 0。
    """

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name.endswith("/claude") else 0o644
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class _Signer:
    """测试用 Registry 签名：openssl 生成 P-256 密钥，签名口径与 npm 相同。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = root / "key.pem"
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(self.key)],
                       check=True, capture_output=True)
        public = root / "pub.der"
        subprocess.run(["openssl", "ec", "-in", str(self.key), "-pubout", "-outform", "DER", "-out", str(public)],
                       check=True, capture_output=True)
        self.keys = {"keys": [{"keyid": "SHA256:test-key", "scheme": "ecdsa-sha2-nistp256",
                               "key": base64.b64encode(public.read_bytes()).decode(), "expires": None}]}

    def sign(self, package: str, version: str, integrity: str) -> list[dict[str, str]]:
        message = self.root / "message.txt"
        signature = self.root / "signature.der"
        message.write_bytes(f"{package}@{version}:{integrity}".encode("utf-8"))
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.key), "-out", str(signature), str(message)],
                       check=True, capture_output=True)
        return [{"keyid": "SHA256:test-key", "sig": base64.b64encode(signature.read_bytes()).decode()}]


class _Registry:
    """npm Registry 与原生分发两渠道的替身：URL → 正文；可按调用次数改写主包 packument。"""

    def __init__(self, signer: _Signer, *, npm_binary: bytes, native_binary: bytes) -> None:
        self.signer = signer
        self.npm_binary = npm_binary
        self.native_binary = native_binary
        self.tags = {"stable": VERSION, "latest": "2.1.301"}
        self.optional = {PLATFORM_PACKAGE: VERSION, "@anthropic-ai/claude-code-linux-x64": VERSION}
        self.manifest_overrides: dict[str, Any] = {}
        self.platform_overrides: dict[str, Any] = {}
        self.tamper_main_tarball = False
        self.end_tags: dict[str, str] | None = None
        self.main_calls = 0
        self.requests: list[str] = []

    def _tarball_url(self, package: str) -> str:
        return f"{claude_fw_e.REGISTRY_BASE}/{package}/-/{package.rsplit('/', 1)[-1]}-{VERSION}.tgz"

    def _version_entry(self, package: str, tarball: bytes, **extra: Any) -> dict[str, Any]:
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
        return {"name": package, "version": VERSION, **extra,
                "dist": {"tarball": self._tarball_url(package), "integrity": integrity,
                         "shasum": hashlib.sha1(tarball).hexdigest(),
                         "signatures": self.signer.sign(package, VERSION, integrity)}}

    def tarballs(self) -> dict[str, bytes]:
        main = _tarball({"package/package.json": json.dumps(
            {"name": freeze_module.MAIN_PACKAGE, "version": VERSION, "optionalDependencies": self.optional}).encode()})
        platform = _tarball({
            "package/package.json": json.dumps({"name": PLATFORM_PACKAGE, "version": VERSION}).encode(),
            "package/claude": self.npm_binary,
        })
        return {freeze_module.MAIN_PACKAGE: main, PLATFORM_PACKAGE: platform}

    def __call__(self, url: str, *, accept: str) -> tuple[bytes, dict[str, str]]:
        del accept
        self.requests.append(url)
        tarballs = self.tarballs()
        main_url = claude_fw_e._package_url(freeze_module.MAIN_PACKAGE)
        if url == main_url:
            self.main_calls += 1
            tags = self.end_tags if (self.end_tags is not None and self.main_calls > 1) else self.tags
            entry = self._version_entry(freeze_module.MAIN_PACKAGE, tarballs[freeze_module.MAIN_PACKAGE],
                                        optionalDependencies=self.optional)
            return json.dumps({"name": freeze_module.MAIN_PACKAGE, "dist-tags": tags,
                               "versions": {VERSION: entry}}).encode(), {"etag": "main"}
        if url == claude_fw_e._package_url(PLATFORM_PACKAGE):
            entry = self._version_entry(PLATFORM_PACKAGE, tarballs[PLATFORM_PACKAGE])
            return json.dumps({"name": PLATFORM_PACKAGE, "dist-tags": self.tags,
                               "versions": {VERSION: entry}}).encode(), {}
        if url == claude_fw_e.REGISTRY_KEYS_URL:
            return json.dumps(self.signer.keys).encode(), {}
        for package, tarball in tarballs.items():
            if url == self._tarball_url(package):
                if self.tamper_main_tarball and package == freeze_module.MAIN_PACKAGE:
                    return tarball + b"x", {}
                return tarball, {}
        if url == f"{BASE}/{VERSION}/manifest.json":
            platform = {"binary": "claude", "checksum": hashlib.sha256(self.native_binary).hexdigest(),
                        "size": len(self.native_binary), **self.platform_overrides}
            manifest = {"version": VERSION, "manifestSignatureEnforcement": "flag", "commit": "c" * 40,
                        "modsCommit": "d" * 40, "buildDate": "2026-10-01T00:00:00Z",
                        "platforms": {"linux-arm64": platform, "linux-x64": {"binary": "claude", "checksum": "0" * 64, "size": 1}},
                        **self.manifest_overrides}
            return json.dumps(manifest).encode(), {}
        if url == f"{BASE}/{VERSION}/linux-arm64/claude":
            return self.native_binary, {}
        if url == f"{BASE}/latest":
            return b"2.1.301\n", {}
        if url == f"{BASE}/stable":
            return f"{VERSION}\n".encode(), {}
        raise freeze_module.FreezeError(f"替身没有登记的地址：{url}")


def _clock() -> Callable[[], str]:
    ticks = iter(f"2026-10-07T00:00:{second:02d}Z" for second in range(60))
    return lambda: next(ticks)


class FreezeOfficialReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = _Signer(Path(cls._keys.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._keys.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.binary = _elf(GLIBC)

    def _freeze(self, registry: _Registry, *, name: str = "freeze", **kwargs: Any) -> dict[str, Any]:
        with mock.patch.object(freeze_module, "_http_get", registry):
            return freeze_module.freeze_official_release(self.root / "out" / name, clock=_clock(), **kwargs)

    def test_equivalent_channels_freeze_single_binary(self) -> None:
        registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
        freeze = self._freeze(registry)
        output = self.root / "out" / "freeze"
        self.assertEqual(freeze["schema_version"], "claude-code-fw-e-official-freeze/v2")
        self.assertEqual((freeze["target_version"], freeze["dist_tag"], freeze["platforms"]), (VERSION, "stable", ["linux/arm64"]))
        self.assertEqual(freeze["binary_equivalence"], "npm=gcs")
        self.assertEqual(freeze["binary_sha256"], hashlib.sha256(self.binary).hexdigest())
        rows = freeze["binaries"]["linux/arm64"]
        self.assertEqual(rows["npm"], rows["gcs"])
        self.assertEqual(rows["npm"]["elf"], {"class": "ELF64", "machine": "aarch64", "interpreter": GLIBC, "libc": "glibc"})
        self.assertEqual((output / rows["npm"]["path"]).read_bytes(), self.binary)
        self.assertEqual(freeze["gcs"]["checksum"], freeze["binary_sha256"])
        self.assertEqual(freeze["gcs"]["channel_versions"], {"latest": "2.1.301", "stable": VERSION})
        self.assertEqual(freeze["gcs"]["manifest_sha256"], hashlib.sha256((output / "native/manifest.json").read_bytes()).hexdigest())
        self.assertEqual(sorted(item["package"] for item in freeze["npm"]["artifacts"]), sorted([freeze_module.MAIN_PACKAGE, PLATFORM_PACKAGE]))
        self.assertTrue(all(item["registry_signatures"][0]["verified"] for item in freeze["npm"]["artifacts"]))
        written = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
        self.assertEqual(written, freeze)
        self.assertEqual(freeze["registry_snapshot_sha256"], claude_fw_e.sha256_file(output / "registry-snapshot.json"))
        self.assertEqual(sorted(path.name for path in (self.root / "out").iterdir()), ["freeze"])  # 暂存目录已改名

    def test_divergent_channels_keep_both_binaries_without_single_identity(self) -> None:
        other = _elf(GLIBC, payload=b"different-build")
        freeze = self._freeze(_Registry(self.signer, npm_binary=self.binary, native_binary=other))
        self.assertEqual(freeze["binary_equivalence"], "divergent")
        self.assertIsNone(freeze["binary_sha256"])
        rows = freeze["binaries"]["linux/arm64"]
        self.assertNotEqual(rows["npm"]["path"], rows["gcs"]["path"])
        self.assertEqual(rows["gcs"]["sha256"], hashlib.sha256(other).hexdigest())

    def test_dist_tag_override_selects_that_tag(self) -> None:
        registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
        registry.tags = {"stable": "2.1.299", "next": VERSION}
        freeze = self._freeze(registry, dist_tag="next")
        self.assertEqual((freeze["target_version"], freeze["dist_tag"]), (VERSION, "next"))

    def test_rejections_leave_no_output(self) -> None:
        cases: dict[str, tuple[Callable[[_Registry], None], str]] = {
            "npm 主包未绑定平台包": (lambda r: setattr(r, "optional", {}), "精确绑定"),
            "npm tarball 完整性": (lambda r: setattr(r, "tamper_main_tarball", True), "integrity"),
            "原生分发 checksum": (lambda r: r.platform_overrides.update(checksum="0" * 64), "checksum"),
            "原生分发字节数": (lambda r: r.platform_overrides.update(size=3), "字节数"),
            "原生分发缺平台": (lambda r: r.manifest_overrides.update(platforms={}), "缺少平台"),
            "manifest 版本不符": (lambda r: r.manifest_overrides.update(version="2.1.299"), "version"),
            "stable 中途漂移": (lambda r: setattr(r, "end_tags", {"stable": "2.1.301"}), "作废"),
        }
        for index, (label, (mutate, message)) in enumerate(cases.items()):
            with self.subTest(label=label):
                registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
                mutate(registry)
                with self.assertRaisesRegex(freeze_module.FreezeError, message):
                    self._freeze(registry, name=f"case-{index}")
                self.assertFalse((self.root / "out" / f"case-{index}").exists())
        leftovers = [path.name for path in (self.root / "out").iterdir()]
        self.assertEqual(leftovers, [])

    def test_elf_identity_distinguishes_libc_and_machine(self) -> None:
        self.assertEqual(freeze_module.elf_identity(_elf(MUSL), "linux/arm64")["libc"], "musl")
        self.assertEqual(freeze_module.elf_identity(_elf(None), "linux/arm64")["libc"], "static")
        self.assertEqual(freeze_module.elf_identity(_elf("/lib/other.so"), "linux/arm64")["libc"], "unknown")
        with self.assertRaisesRegex(freeze_module.FreezeError, "机器号"):
            freeze_module.elf_identity(_elf(GLIBC, machine=62), "linux/arm64")
        with self.assertRaisesRegex(freeze_module.FreezeError, "不是 ELF"):
            freeze_module.elf_identity(b"MZ" + b"\0" * 80, "linux/arm64")

    def test_musl_or_foreign_binary_is_rejected(self) -> None:
        for label, npm_binary, native_binary in (
            ("npm musl", _elf(MUSL), self.binary),
            ("原生分发 musl", self.binary, _elf(MUSL)),
            ("x86_64 二进制", _elf("/lib64/ld-linux-x86-64.so.2", machine=62), self.binary),
        ):
            with self.subTest(label=label):
                with self.assertRaises(freeze_module.FreezeError):
                    self._freeze(_Registry(self.signer, npm_binary=npm_binary, native_binary=native_binary),
                                 name=label.replace(" ", "-"))

    def test_output_and_platform_guards(self) -> None:
        registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
        existing = self.root / "out" / "exists"
        existing.mkdir(parents=True)
        with mock.patch.object(freeze_module, "_http_get", registry):
            with self.assertRaisesRegex(freeze_module.FreezeError, "已存在"):
                freeze_module.freeze_official_release(existing)
            with self.assertRaisesRegex(freeze_module.FreezeError, "绝对路径"):
                freeze_module.freeze_official_release(Path("relative/out"))
            with self.assertRaisesRegex(freeze_module.FreezeError, "不支持的冻结平台"):
                freeze_module.freeze_official_release(self.root / "out" / "x", platform="linux/amd64")
            with self.assertRaisesRegex(freeze_module.FreezeError, "dist-tag 非法"):
                freeze_module.freeze_official_release(self.root / "out" / "y", dist_tag="stable;rm")
        self.assertEqual(registry.requests, [])  # 参数不合法时不发任何网络读取

    def test_orchestrator_freeze_subcommand(self) -> None:
        registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
        output = io.StringIO()
        target = self.root / "cli-freeze"
        with mock.patch.object(freeze_module, "_http_get", registry), contextlib.redirect_stdout(output):
            code = claude_upgrade.main(["freeze", "--output-root", str(target)])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["target_version"], summary["binary_equivalence"]), (VERSION, "npm=gcs"))
        self.assertEqual(summary["freeze_sha256"], claude_fw_e.sha256_file(target / "freeze.json"))



def freeze_fixture(output_root: Path, *, signer: _Signer, binary: bytes | None = None, version: str | None = None) -> Path:
    """在 ``output_root``（不存在的绝对路径）按真实冻结流程冻结一份两渠道等价的合成官方制品，返回 ``freeze.json``。

    ``version`` 缺省为本模块的 ``VERSION``；给出时 Registry 替身整体换成该版本（plan 审计测试按真实场景清单的目标版本冻结）。"""

    data = binary if binary is not None else _elf(GLIBC)
    with mock.patch.object(sys.modules[__name__], "VERSION", version or VERSION):
        registry = _Registry(signer, npm_binary=data, native_binary=data)
        with mock.patch.object(freeze_module, "_http_get", registry):
            freeze_module.freeze_official_release(output_root, clock=_clock())
    return output_root / "freeze.json"


class FreezeVerificationTests(unittest.TestCase):
    """B5a-09：只读复核已有冻结目录（入口便宜检查 official-package 与 plan --audit-only 共用），不联网、不写文件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = _Signer(Path(cls._keys.name))
        cls._frozen = tempfile.TemporaryDirectory()
        cls.binary = _elf(GLIBC)
        cls.source = freeze_fixture(Path(cls._frozen.name).resolve() / "freeze", signer=cls.signer, binary=cls.binary)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._frozen.cleanup()
        cls._keys.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.copies = 0

    def _fresh(self) -> Path:
        """冻结产物的一份独立副本（每个篡改用例各用一份），返回副本里的 freeze.json。"""

        self.copies += 1
        root = self.base / f"freeze-{self.copies}"
        shutil.copytree(self.source.parent, root, symlinks=True)
        return root / "freeze.json"

    @staticmethod
    def _rewrite(freeze: Path, change: Callable[[dict[str, Any]], None]) -> None:
        payload = json.loads(freeze.read_text(encoding="utf-8"))
        change(payload)
        freeze.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _tree(root: Path) -> dict[str, tuple[str, int]]:
        return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
                for path in sorted(root.rglob("*")) if path.is_file()}

    def _expected(self, freeze: Path) -> dict[str, str]:
        return {"expected_version": VERSION, "expected_binary_sha256": hashlib.sha256(self.binary).hexdigest(),
                "expected_freeze_sha256": hashlib.sha256(freeze.read_bytes()).hexdigest()}

    def test_verifies_real_freeze_output_without_writes(self) -> None:
        freeze = self._fresh()
        before = self._tree(freeze.parent)
        result = freeze_module.verify_freeze_receipt(freeze, **self._expected(freeze))
        self.assertEqual(self._tree(freeze.parent), before)
        self.assertEqual((result["schema_version"], result["status"]), (freeze_module.FREEZE_VERIFICATION_SCHEMA, "passed"))
        self.assertEqual((result["target_version"], result["platform"], result["binary_equivalence"]), (VERSION, "linux/arm64", "npm=gcs"))
        self.assertEqual((result["files_written"], result["network_requests"]), (0, 0))
        self.assertEqual(result["binary_bytes"], len(self.binary))
        self.assertEqual(result["elf"]["libc"], "glibc")
        self.assertEqual([item["role"] for item in result["verified_files"]], [
            "Registry 快照", f"npm tarball {freeze_module.MAIN_PACKAGE}", f"npm tarball {PLATFORM_PACKAGE}",
            "原生分发 manifest", "平台二进制",
        ])
        # 不给本轮登记值时只核收据与冻结目录自身的一致性。
        self.assertEqual(freeze_module.verify_freeze_receipt(freeze)["binary_sha256"], hashlib.sha256(self.binary).hexdigest())

    def test_rejections(self) -> None:
        dashed = "linux-arm64"

        def tamper(relative: str) -> Callable[[Path], None]:
            return lambda freeze: (freeze.parent / relative).write_bytes((freeze.parent / relative).read_bytes() + b"x")

        def remove(relative: str) -> Callable[[Path], None]:
            return lambda freeze: (freeze.parent / relative).unlink()

        def symlink(relative: str) -> Callable[[Path], None]:
            def change(freeze: Path) -> None:
                target = freeze.parent / relative
                moved = freeze.parent / "moved.json"
                target.rename(moved)
                target.symlink_to(moved)
            return change

        def rewrite(change: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
            return lambda freeze: self._rewrite(freeze, change)

        def both_rows(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
            def change(payload: dict[str, Any]) -> None:
                for channel in ("npm", "gcs"):
                    payload["binaries"]["linux/arm64"][channel][field] = value
            return change

        tarball = f"official/{claude_fw_e._safe_package_name(PLATFORM_PACKAGE)}-{VERSION}.tgz"
        cases: dict[str, tuple[Callable[[Path], None] | None, dict[str, Any], str]] = {
            "目标版本不符": (None, {"expected_version": "2.1.299"}, "不是本轮目标版本"),
            "目标二进制摘要不符": (None, {"expected_binary_sha256": "0" * 64}, "不是本轮登记的目标二进制摘要"),
            "冻结收据摘要不符": (None, {"expected_freeze_sha256": "0" * 64}, "与登记值"),
            "登记值不是摘要": (None, {"expected_binary_sha256": "REPLACE_ME"}, "不是完整的小写 SHA-256"),
            "两渠道不一致": (rewrite(lambda p: p.update(binary_equivalence="divergent")), {}, "两渠道二进制不一致"),
            "合同不符": (rewrite(lambda p: p.update(schema_version="claude-code-fw-e-official-freeze/v1")), {}, "合同不是"),
            "平台不符": (rewrite(lambda p: p.update(platforms=["darwin/arm64"])), {}, "平台"),
            "dist-tag 首尾不符": (rewrite(lambda p: p.update(dist_tag_at_end="2.1.301")), {}, "dist-tag 首尾"),
            "producer 不是冻结工具": (rewrite(lambda p: p["producer"].update(path="tools/x.py")), {}, "producer"),
            "二进制被改": (tamper(f"binaries/{dashed}/claude"), {}, "平台二进制 摘要"),
            "Registry 快照被改": (tamper("registry-snapshot.json"), {}, "Registry 快照 摘要"),
            "manifest 被改": (tamper("native/manifest.json"), {}, "原生分发 manifest 摘要"),
            "tarball 缺失": (remove(tarball), {}, "缺少"),
            "manifest 换成符号链接": (symlink("native/manifest.json"), {}, "符号链接"),
            "登记路径越出冻结目录": (rewrite(lambda p: p.update(registry_snapshot_path="../registry-snapshot.json")), {}, "规范相对路径"),
            "ELF 记录不符": (rewrite(both_rows("elf", {"class": "ELF64", "machine": "aarch64", "interpreter": MUSL, "libc": "musl"})), {}, "ELF 判定"),
            "两渠道登记不同二进制": (rewrite(lambda p: p["binaries"]["linux/arm64"]["gcs"].update(bytes=1)), {}, "同一个二进制"),
            "npm 制品缺平台包": (rewrite(lambda p: p["npm"].update(artifacts=p["npm"]["artifacts"][:1])), {}, "主包与平台包"),
            "manifest checksum 不符": (rewrite(lambda p: p["gcs"].update(checksum="0" * 64)), {}, "checksum"),
        }
        for label, (mutate, expected, message) in cases.items():
            with self.subTest(label=label):
                freeze = self._fresh()
                if mutate is not None:
                    mutate(freeze)
                with self.assertRaisesRegex(freeze_module.FreezeError, message):
                    freeze_module.verify_freeze_receipt(freeze, **expected)
        with self.assertRaisesRegex(freeze_module.FreezeError, "绝对路径"):
            freeze_module.verify_freeze_receipt(Path("relative/freeze.json"))
        with self.assertRaisesRegex(freeze_module.FreezeError, "不存在或不是普通文件"):
            freeze_module.verify_freeze_receipt(self.base / "missing" / "freeze.json")

    def test_verify_subcommand_and_legacy_freeze_usage(self) -> None:
        freeze = self._fresh()
        expected = self._expected(freeze)
        argv = ["verify", "--freeze", str(freeze), "--target-version", VERSION,
                "--binary-sha256", expected["expected_binary_sha256"], "--freeze-sha256", expected["expected_freeze_sha256"]]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(freeze_module.main(argv), 0)
            self.assertEqual(freeze_module.main([*argv[:4], "2.1.299"]), 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "passed")
        self.assertIn("冻结目录复核未通过", stderr.getvalue())
        registry = _Registry(self.signer, npm_binary=self.binary, native_binary=self.binary)
        target = self.base / "legacy-freeze"
        with mock.patch.object(freeze_module, "_http_get", registry), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(freeze_module.main(["--output-root", str(target)]), 0)
        self.assertEqual(freeze_module.verify_freeze_receipt(target / "freeze.json")["status"], "passed")


if __name__ == "__main__":
    unittest.main()
