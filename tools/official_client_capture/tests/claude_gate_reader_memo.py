"""VC-6 真实批次链测试专用：跨进程复用「输入逐字相同」的独立 reader 结果（只在测试里，不进受管树）。

背景（2026-10-25 第 5 步分段计时）
==================================
``test_claude_upgrade_vc6_batch`` 的整条恢复链全程约 1000 秒（本机）／2100～2400 秒（CI），其中每次门禁材料
重放都要新开一个 ``python -I -B -S claude_gate_material_worker.py`` 私有进程（冷启动加导入加 ``sys.setprofile``
逐访问重放，单次约 0.8～1.2 秒）。实测整条用例共 205 次这样的新进程，**输入只有 3 种**（其中 203 次逐字相同：
同一世界、同一材料、同一预期 subject，只是由测试进程、父批次动作进程、守卫进程、finalize 动作进程分别各问了一遍），
合计 165～250 秒，占用例墙钟的 17%～21%。

产品里本来就有一条「同一来源内承接已经逐份实核的 reader 结果」的路径：``read_verified_gate_material`` 的
``prior_replays``（见 ``claude_gate_material_worker.read_verified_gate_material``）。它**不信任**旧结果本身：
命中时仍要（一）输入字节逐字相同，（二）重新完整实读每个材料文件并核 sha256／长度／模式，
（三）用 ``GateMaterialReadView`` 把旧结果记录的每一次元数据访问重做一遍并要求访问集逐项相同，
（四）核读取身份里的禁用 bytecode 前缀仍不存在。只有这条路径不跨进程、不跨 scope。

本模块把同一条路径**原样**搬到进程之间：第一次（未命中）仍然启动真实的独立 reader 进程，结果落到测试私有目录；
之后任何进程（测试进程、守卫进程、动作子进程）再遇到逐字相同的输入，就用产品自己的 ``prior_replays`` 路径
复核这份旧结果，**复核通过才复用，复核失败（材料漂移、访问集变化、前缀出现等）一律回退到真实 reader**，
不会掩盖任何一次失败，也不会改变任何返回值的形状。

范围与不降低覆盖的论证
----------------------
* 只在 ``Vc6BatchChainTests`` 的 ``setUp`` 里显式开启，另有一个环境变量把缓存目录传给动作子进程；
  其他测试模块（包括直接测 reader 的 ``test_claude_sealed_gate_materials``）完全不受影响，仍每次真实启动 reader。
* 本链的断言（交付阶段序列、失败／强杀／对账／失效重跑／finalize／VC-6 checkpoint／零请求）都不依赖
  「每次都新开 reader 进程」这一实现细节；reader 进程本身的隔离、越界拒绝、漂移拒绝由
  ``test_claude_sealed_gate_materials`` 等专项测试覆盖，没有改动。
* 命中路径复用的是产品自己的复核函数，不是测试里重写的一份；因此材料字节或模式一旦变化、访问集一旦变化，
  命中路径同样抛出 ``ReaderError``，随后回退真实 reader 并由它给出权威结论。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_gate_material_worker as worker
from tools.official_client_capture import claude_qualification_material_import as capture_import

# 缓存目录经环境变量传给 VC-6 动作子进程（子进程里由 claude_typed_vc6_fixture.install 开启）。
MEMO_ENV = "CLAUDE_VC6_GATE_READER_MEMO_DIR"
# 统计文件：每次命中追加 "hit"，每次真实启动追加 "run"（含进程号），只供事后核对，不参与任何断言。
STATS_FILE = "stats.log"

_local = threading.local()


def _specification(reader: Any, files: list[dict[str, Any]], directories: list[dict[str, Any]], missing: list[str],
                   gate_root: str, receipt_relative: str, view: str, expected_subject: Any) -> bytes:
    """与 ``read_verified_gate_material`` 内部完全相同的输入字节（含 view），既是缓存键，也是 prior 的比对对象。"""

    return worker._canonical({"view": view, **worker._reader_specification(
        reader, files, directories, missing, gate_root, receipt_relative, expected_subject)})


def _note(directory: Path, kind: str) -> None:
    """追加一行统计；统计失败绝不影响测试结果。"""

    with contextlib.suppress(OSError):
        with (directory / STATS_FILE).open("a", encoding="utf-8") as stream:
            stream.write(f"{kind} {os.getpid()}\n")


def _load(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _store(path: Path, raw: bytes) -> None:
    """原子落盘（临时文件加 rename）；并发写同一个键时内容逐字相同，后到的覆盖无害。"""

    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _memoized(real: Any, directory: Path) -> Any:
    """包装 ``run_gate_material_reader``：未命中走真实 reader 并落盘，命中则用产品的 prior 路径复核后复用。"""

    def run(reader_identity: Any, files: list[dict[str, Any]], directories: list[dict[str, Any]], missing: list[str],
            gate_root: str, receipt_relative: str, *, view: str, expected_subject: Any) -> dict[str, Any]:
        # 复核期间产品代码会在 prior 不匹配时再次回调本函数：此时直接走真实 reader，避免递归。
        # 类型、令牌、视图不合法的调用也原样交给真实函数，由它抛出权威的拒绝。
        if getattr(_local, "replaying", False):
            # 产品的 prior 复核没有承接（例如 scope 不匹配）而回调到这里：记下「实际启动了真实 reader」。
            _local.fell_back = True
            return real(reader_identity, files, directories, missing, gate_root, receipt_relative,
                        view=view, expected_subject=expected_subject)
        if (not isinstance(reader_identity, worker.VerifiedReaderIdentity)
                or reader_identity.token is not worker._IDENTITY_TOKEN
                or view not in {"original", "imported"}):
            return real(reader_identity, files, directories, missing, gate_root, receipt_relative,
                        view=view, expected_subject=expected_subject)
        specification = _specification(reader_identity, files, directories, missing, gate_root, receipt_relative,
                                       view, expected_subject)
        path = directory / f"{hashlib.sha256(specification).hexdigest()}.json"
        cached = _load(path)
        if cached is not None:
            _local.replaying = True
            _local.fell_back = False
            try:
                # 在产品自己的 scope 里走产品自己的 prior_replays 复核：实读全部材料、重做全部元数据访问。
                with worker.gate_material_reader_scope():
                    prior = worker.VerifiedMaterialReplay(specification, cached, worker._REPLAY_TOKEN,
                                                          worker._REPLAY_SCOPE.get())
                    result, _ = worker.read_verified_gate_material(
                        reader_identity, files, directories, missing, gate_root, receipt_relative,
                        view=view, expected_subject=expected_subject, prior_replays=(prior,))
                _note(directory, "run" if _local.fell_back else "hit")
                return result
            except worker.ReaderError:
                # 复核不通过（材料、访问集或前缀变化）：不使用旧结果，回退到真实 reader，由它给出结论。
                pass
            finally:
                _local.replaying = False
        result = real(reader_identity, files, directories, missing, gate_root, receipt_relative,
                      view=view, expected_subject=expected_subject)
        _note(directory, "run")
        _store(path, worker._canonical(result))
        return result

    return run


@contextlib.contextmanager
def gate_reader_memo(directory: Path) -> Iterator[Path]:
    """在本进程内启用跨进程 reader 结果复用；退出即恢复原函数。

    ``claude_qualification_material_import`` 是以名字导入 ``run_gate_material_reader`` 的，也要一并替换，
    否则它的两处直接调用会绕过缓存。
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    real = worker.run_gate_material_reader
    wrapper = _memoized(real, directory)
    with mock.patch.object(worker, "run_gate_material_reader", wrapper), \
            mock.patch.object(capture_import, "run_gate_material_reader", wrapper):
        yield directory


def read_stats(directory: Path) -> dict[str, int]:
    """事后汇总：命中次数与真实启动次数（供人工核对与回归观察）。"""

    counts = {"hit": 0, "run": 0}
    with contextlib.suppress(OSError):
        for line in (Path(directory) / STATS_FILE).read_text(encoding="utf-8").splitlines():
            kind = line.split(" ", 1)[0]
            if kind in counts:
                counts[kind] += 1
    return counts


def environment(directory: Path) -> dict[str, str]:
    """交给动作子进程的环境变量。"""

    return {MEMO_ENV: str(directory)}


__all__ = ["MEMO_ENV", "STATS_FILE", "environment", "gate_reader_memo", "read_stats"]
