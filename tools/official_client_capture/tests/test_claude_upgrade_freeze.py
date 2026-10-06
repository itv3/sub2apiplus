"""Claude 官方制品冻结（Claude 升级控制面设计 B2-01）的离线测试。

网络读取全部由替身提供：测试现场生成一把 ECDSA P-256 密钥，按 npm Registry 的签名口径
（``<package>@<version>:<integrity>``）给合成 tarball 签名，所以 sha512 SRI、sha1 与签名校验走的是真实代码路径；
合成的 ELF64 二进制只带冻结判定需要的 ELF 头与 ``PT_INTERP``。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import struct
import subprocess
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
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
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


if __name__ == "__main__":
    unittest.main()
