"""劫持 chatgpt.com 的采集脚本必须恢复账号临时熔断状态。

合成 relay 与 HTTP/1.1 探针把 chatgpt.com 指向容器内端口；探针停止后仍在途的真实
出站会拿到 connection refused，Sub2API 据此写入 temp_unschedulable_until。该熔断是
脚手架自身的副作用，不恢复就会让同一 attempt 的后续任务全部拿到 503 no available
accounts —— k15～k18、k23 的候选采集都因此从中途开始连续失败。
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SCRIPTS = (
    "run_h1_wire_probe.sh",
    "run_images_wire_probe.sh",
    "run_candidate_core_capture.sh",
    "run_candidate_aux_capture.sh",
)


class AccountGateRestorationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        cls.sources = {
            name: (root / name).read_text(encoding="utf-8") for name in SCRIPTS
        }
        cls.paths = {name: root / name for name in SCRIPTS}

    def test_shell_syntax_is_valid(self) -> None:
        for name, path in self.paths.items():
            with self.subTest(script=name):
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_每个脚本都在运行前冻结调度门状态(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("account_gate_state()", source)
                self.assertIn("temp_unschedulable_until", source)
                self.assertIn("temp_unschedulable_reason", source)
                # 原值必须先读入变量，否则恢复无基准。
                self.assertRegex(source, r"(account_gate_before|original_gate_state)=\$\(account_gate_state\)")

    def test_恢复写回按运行前值而不是无条件清空(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("restore_account_gate()", source)
                self.assertIn(
                    "temp_unschedulable_until = nullif(convert_from(decode(",
                    source,
                )
                # 退出路径必须按原值回写：无条件置空会掩盖运行前就存在的真实熔断。
                restore_body = source.split("restore_account_gate() {", 1)[1].split("\n}", 1)[0]
                self.assertNotIn(
                    "set temp_unschedulable_until = null, temp_unschedulable_reason = null",
                    restore_body,
                )

    def test_恢复结果失败关闭(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("restore_account_gate", source)
                if name.startswith("run_candidate_"):
                    self.assertIn("restored_gate_equal=true", source)
                    self.assertIn("restore_failed=1", source)
                else:
                    self.assertIn("status=97", source)

    def test_读取失败时拒绝继续(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertRegex(source, r"调度门(初始)?状态")
                self.assertIn("exit 1", source)

    def test_运行期主动清除熔断且退出仍按原值恢复(self) -> None:
        """劫持生效后账号必须保持可调度，否则采集自己会把自己打成 503。"""

        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("clear_account_gate()", source)
                self.assertIn(
                    "set temp_unschedulable_until = null, temp_unschedulable_reason = null",
                    source,
                )
                # 清除只能发生在运行期；退出路径必须仍走按原值回写。
                self.assertIn("restore_account_gate", source)
                if name.startswith("run_candidate_"):
                    # frozen 类每个场景开始时清一次。
                    self.assertRegex(source, r"current_scenario=\$scenario\n\s*clear_account_gate")
                    # 场景级还不够：relay 上线后后台流量会立刻再次熔断，必须紧贴每次触发请求。
                    self.assertRegex(
                        source,
                        r"request_with_token\(\) \{[\s\S]{0,400}?clear_account_gate[\s\S]{0,120}?curl",
                    )
                else:
                    # 探针类在 hosts 劫持生效后清一次。
                    self.assertRegex(source, r"hosts_patched=1[\s\S]{0,200}?clear_account_gate")


if __name__ == "__main__":
    unittest.main()


class ImagesWireModelMappingTest(unittest.TestCase):
    """images-wire 必须自己准备图片模型映射，且按原值恢复。

    aux 脚本会临时把图片模型写进账号的显式 model_mapping 白名单并由 EXIT 钩子恢复，
    images-wire 此前没有这一步：生图请求在入口就被判 model_not_found（HTTP 404）、
    根本不出站，h1 探针永远等不到数据，job 只留下一行 `h1-wire.json 不存在`。
    """

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        cls.path = root / "run_images_wire_probe.sh"
        cls.source = cls.path.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.path)], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_运行前冻结原值并武装恢复(self) -> None:
        self.assertIn("original_model_mapping_state=$(db_query", self.source)
        self.assertIn("model_mapping_restore_armed=1", self.source)
        # 原值必须区分「本来就有」与「本来没有」，否则恢复会凭空造出该字段。
        self.assertIn("'present:' || encode(convert_to(", self.source)
        self.assertIn("else 'missing:' end", self.source)

    def test_写入映射早于服务重启(self) -> None:
        """model_mapping 变更要重启才进入进程；顺序反了等于没写。"""

        write = self.source.index("jsonb_build_object('$image_model','$image_model')")
        restart = self.source.index('docker restart "$service_container" >/dev/null\n')
        self.assertLess(write, restart)

    def test_恢复按原值回写而不是删键了事(self) -> None:
        self.assertIn("convert_from(decode('${original_model_mapping_state#present:}'", self.source)
        self.assertIn("coalesce(credentials,'{}'::jsonb) - 'model_mapping'", self.source)
        # 恢复失败必须失败关闭，不能静默留下白名单。
        self.assertIn("status=97", self.source)

    def test_模型名参数化且做字符校验(self) -> None:
        self.assertIn("image_model=${IMAGE_MODEL:-gpt-image-1}", self.source)
        self.assertIn("^[A-Za-z0-9._-]+$", self.source)
