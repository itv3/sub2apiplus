"""B5a-01 验收 ⑨（Codex）：同一输入下，改造后的部署器与改造前写出的 Codex 收据逐字节相同。

改造前的脚本取自 B5a-01 开工时本分支的起点（批次 1 收口提交 ``BASELINE_COMMIT``），从真实仓库的 git 历史读出，与改造后
的脚本在同一套替身环境、同一份暂存与生产现场上各跑一次缺省部署（只交换 official_client_capture、只签 Codex）：

* Codex 收据文件名相同、内容逐字节相同（时间、时间戳与随机后缀由夹具固定，所以连 ``created_at_utc`` 也相同）；
* 部署后的数据根逐对象相同（类型、内容与权限位），只多出部署锁文件；
* 打印的结果相同；监督器事件序列去掉写意图、收据正文入日志与删除日志三步后与改造前相同。

基线提交读不到（例如浅克隆）或基线之后受管文档清单已变（同一输入不再成立）时跳过并写明原因。本模块读真实仓库的
git 历史，按 ``unit_records.GIT_READERS`` 登记。
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture.tests.arm64_deploy_fixture import REPO_ROOT, DeployHarness

BASELINE_COMMIT = "afa2c624e"
BASELINE_PATH = "tools/arm64_supervised_deploy.py"
B5A01_EVENTS = {"enable:write-switch-journal", "enable:prepare-receipts", "enable:remove-switch-journal"}


def load_baseline() -> types.ModuleType | None:
    """从 git 历史读出改造前的部署器，作为独立模块对象加载（不写入 sys.modules）。"""

    try:
        source = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{BASELINE_COMMIT}:{BASELINE_PATH}"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    module = types.ModuleType("arm64_supervised_deploy_before_b5a01")
    module.__file__ = deploy.__file__
    exec(compile(source, f"{BASELINE_COMMIT}:{BASELINE_PATH}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


def site_objects(root: Path) -> dict[str, tuple[str, str | None, int]]:
    """数据根下全部对象：相对路径 → (类型, 内容摘要, 权限位)；部署锁文件是 B5a-01 新增的，不计。"""

    objects: dict[str, tuple[str, str | None, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == f"control/{deploy.DEPLOY_LOCK_NAME}":
            continue
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        if path.is_symlink():
            objects[relative] = ("symlink", os.readlink(path), mode)
        elif path.is_dir():
            objects[relative] = ("directory", None, mode)
        else:
            objects[relative] = ("file", deploy.file_sha256(path), mode)
    return objects


class CodexReceiptParityTests(unittest.TestCase):
    def test_default_deploy_matches_pre_b5a01_script(self) -> None:
        baseline = load_baseline()
        if baseline is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的部署脚本（浅克隆或不在仓库内）")
        if (baseline.MANAGED_DOCUMENTS, baseline.MANAGED_RUNTIME_DOCUMENTS) != (
            deploy.MANAGED_DOCUMENTS,
            deploy.MANAGED_RUNTIME_DOCUMENTS,
        ):
            self.skipTest("基线之后受管文档清单已变，同一输入不再成立；以新的基线提交重新对照")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "site"
            runs: dict[str, dict] = {}
            for label, module in (("before", baseline), ("after", deploy)):
                harness = DeployHarness(root, module=module)
                harness.build()
                self.assertEqual(harness.run(), 0, label)
                receipts = harness.receipts("codex")
                self.assertEqual(len(receipts), 1, label)
                runs[label] = {
                    "name": receipts[0].name,
                    "bytes": receipts[0].read_bytes(),
                    "site": site_objects(harness.data),
                    "events": harness.operations(),
                    "output": harness.last_output,
                }
            before, after = runs["before"], runs["after"]
            self.assertEqual(after["name"], before["name"], "Codex 收据文件名（含 Codex 自己的版本标签）不变")
            self.assertEqual(after["bytes"], before["bytes"], "Codex 收据逐字节相同")
            self.assertEqual(after["site"], before["site"], "部署后的数据根逐对象相同（只多出部署锁）")
            self.assertEqual(after["output"], before["output"], "打印的结果相同")
            self.assertEqual([name for name in after["events"] if name not in B5A01_EVENTS], before["events"])
            self.assertEqual(set(after["events"]) - set(before["events"]), B5A01_EVENTS)


if __name__ == "__main__":
    unittest.main()
