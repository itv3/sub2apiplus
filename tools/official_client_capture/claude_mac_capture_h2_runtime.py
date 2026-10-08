"""本机回归服务的真实解释器及已加载 OpenSSL 绑定；不按收据执行路径。"""
from __future__ import annotations
import ctypes
import hashlib
import platform
import ssl
import sys
import _ssl
from pathlib import Path
from tools.official_client_capture import claude_mac_capture_contract as c

def _file(path):
    target = Path(path).resolve(strict=True)
    raw = target.read_bytes()
    return {"path": str(target), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

def collect_runtime():
    """用 _ssl 的已加载符号定位它实际使用的库，不能猜默认安装路径。"""
    c.require(sys.platform == "darwin" or sys.platform.startswith("linux"), "尚未证明该平台 SSL 运行来源")
    class Info(ctypes.Structure):
        _fields_ = [("filename", ctypes.c_char_p), ("base", ctypes.c_void_p),
                    ("symbol", ctypes.c_char_p), ("address", ctypes.c_void_p)]
    library = ctypes.CDLL(None)
    library.dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(Info)]
    library.dladdr.restype = ctypes.c_int
    module = ctypes.CDLL(_ssl.__file__)
    def loaded_file(source, symbol):
        info = Info()
        address = ctypes.cast(getattr(source, symbol), ctypes.c_void_p)
        c.require(library.dladdr(address, ctypes.byref(info)) != 0 and bool(info.filename),
                  "实际运行库符号来源不能证明")
        return {"symbol": symbol, **_file(info.filename.decode())}
    images = [loaded_file(module, symbol) for symbol in ("SSL_CTX_new", "OpenSSL_version")]
    # 某些解释器 executable 只是启动 stub；另绑定已加载的真正 Python 运行库。
    python_image = loaded_file(ctypes.pythonapi, "Py_GetVersion")
    module.OpenSSL_version.argtypes = [ctypes.c_int]
    module.OpenSSL_version.restype = ctypes.c_char_p
    c.require(module.OpenSSL_version(0).decode() == ssl.OPENSSL_VERSION,
              "实际 OpenSSL 库与 _ssl 版本不一致")
    return {"schema_version": "claude-upgrade-native-runtime/v2", "source_scope": c.SCOPE,
            "formal_ready": False, "launch_allowed": False,
            "python": {"implementation": sys.implementation.name, "version": sys.version,
                       "architecture": platform.machine(), "executable": _file(sys.executable),
                       "loaded_runtime": python_image},
            "ssl": {"extension": _file(_ssl.__file__), "version": ssl.OPENSSL_VERSION,
                    "version_info": list(ssl.OPENSSL_VERSION_INFO), "has_alpn": ssl.HAS_ALPN,
                    "loaded_libraries": images}}

def verify_runtime(expected):
    c.require(expected == collect_runtime(), "冻结解释器或实际加载 OpenSSL 已漂移")
    c.require(expected["ssl"]["has_alpn"] is True, "实际 SSL 不具备 ALPN")

def verify_binding(root, binding):
    expected = c.read_json(c.check_binding(root, binding))
    verify_runtime(expected)
    return expected
