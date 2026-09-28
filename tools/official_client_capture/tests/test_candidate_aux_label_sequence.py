"""候选辅助采集 A09 的逐连接证据标签，必须与采集脚本按目标版本实际发出的请求序列一致。

背景（修好接着跑第 54 项）：9fd2cf139 让 ``run_candidate_aux_capture.sh`` 从 0.156.1 起不再发
legacy compact 四轮（目标画像删除 SPEC-EP-007／014／020），A09 的 relay 连接从 9 条变成 5 条；
但 0.156.1／0.157.0 的证据标签声明仍按 9 条连接逐位贴标签。编目器按设计只凭 glob 贴标签、不读
被测内容，于是 conn002～005（两次 alpha-search 与两次图像请求）被贴成 compact 的
prime／default／beta／turn_state，原本给 alpha-search 与图像预留的 conn006～009 规则落空。
VC-5 预演 assertion bundle 时只有“落空”以 glob-unmatched 暴露，错标部分完全静默，会让
SPEC-EP-022（图像线序）选不到样本、SPEC-HDR-006／EP-005（辅助端点）漏掉 alpha-search。

第 55 项：删 compact 也删掉了首轮 prime compact 建立账号 Cookie jar 的作用，官方 alpha-search／图像请求都带
Cookie，候选因此在 EP-015／EP-022 头序上失配。0.156.1 起 A09 在 models 之后加一次 Responses 冷请求预热
（relay 候选扩展只对它下发 _cfuvid），连接变为 models、预热、alpha-search×2、images×2。

本测试把三份事实对齐，任何一方单独改动都会失败：

1. 桩环境里真实执行采集脚本的 A09 触发段，得到每个目标版本的有序入口请求序列；
2. 采集脚本收尾校验块按版本冻结的 A09 动作计数（relay 对每条上游连接恰好记一次合成动作）；
3. 每个版本证据标签声明里 candidate-frozen-aux 的 A09 relay 逐连接规则。
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Mapping
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

import build_evidence_catalog as catalog  # noqa: E402

SCRIPT = TOOL_ROOT / "run_candidate_aux_capture.sh"
JOB_ID = "candidate-frozen-aux"
A09_RELAY_PREFIX = "scenarios/A09/relay/"
A09_RELAY_SUFFIX = ".client_to_upstream.bin"
GATEWAY = "http://gateway.invalid"

# 入口路径 → 采集脚本收尾校验块里的动作名。网关把每次入口调用转成恰好一条上游连接，
# relay 按连接顺序编号 conn001、conn002……；出现未登记的入口路径说明采集序列变了，
# 必须同步本表与各版本证据标签声明。
TRIGGER_ACTIONS = {
    "/backend-api/codex/models": "models_manifest",
    "/v1/responses/compact": "legacy_compact",
    "/v1/responses": "responses_cookie_prime",
    "/v1/alpha/search": "alpha_search",
    "/v1/images/generations": "images_generation",
    "/v1/images/edits": "images_edit",
}

# 桩：只记录入口请求（最后一个参数是 URL；compact 轮从 X-Codex-Turn-Metadata 取驱动变体），
# 其余采集动作（relay、抓包、等待动作、账号闸门）一律空操作。刻意不用 set -u 与 ${@: -1}，
# 让 macOS 自带 bash 3.2 与 CI 的 bash 5 行为一致。
HARNESS = r"""
set -e -o pipefail
codex_version=$1
SEQUENCE_LOG=$2
work_dir=$3
model=gpt-6-astra
image_model=gpt-image-2
api_key=stub-key
service_base_url=http://gateway.invalid
common_gateway_headers=(-H 'Originator: codex_exec')
start_capture() { mkdir -p "$work_dir/scenarios/$1/trigger" "$work_dir/scenarios/$1/relay-private"; }
stop_capture() { :; }
wait_action() { :; }
clear_account_gate() { :; }
assert_2xx() { [[ $2 == 200 ]]; }
request_with_token() {
  shift
  local arg last="" variant=""
  for arg in "$@"; do
    case "$arg" in
      X-Codex-Turn-Metadata:*capture_variant*)
        variant=${arg##*\"capture_variant\":\"}
        variant=${variant%%\"*}
        ;;
    esac
    last=$arg
  done
  printf '%s\t%s\n' "$last" "$variant" >>"$SEQUENCE_LOG"
  printf 200
}
"""


def _a09_trigger_section() -> str:
    """截取采集脚本从版本切换到 A09 等待动作为止的触发段（锚点必须唯一）。"""

    source = SCRIPT.read_text(encoding="utf-8")
    start_anchor = "target_workspace_routing=$("
    end_anchor = "wait_action A09 alpha_search 2\n"
    for anchor in (start_anchor, end_anchor):
        if source.count(anchor) != 1:
            raise AssertionError(f"采集脚本锚点不唯一或缺失：{anchor!r}")
    start = source.index(start_anchor)
    end = source.index(end_anchor, start) + len(end_anchor)
    section = source[start:end]
    starts = [line for line in section.splitlines() if line.startswith("start_capture ")]
    if starts != ["start_capture A09"]:
        raise AssertionError(f"A09 触发段混入了其他场景：{starts}")
    return section


def a09_trigger_sequence(codex_version: str) -> list[tuple[str, str]]:
    """在桩环境执行 A09 触发段，返回 [(动作名, compact 驱动变体或空串), ...]。"""

    with tempfile.TemporaryDirectory(prefix="a09-sequence-") as tmp:
        log = Path(tmp) / "sequence.tsv"
        log.touch()
        result = subprocess.run(
            ["bash", "-c", HARNESS + "\n" + _a09_trigger_section(), "a09-harness",
             codex_version, str(log), tmp],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"A09 触发段在桩环境执行失败：{result.stderr[-2000:]}")
        sequence: list[tuple[str, str]] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            url, _, variant = line.partition("\t")
            if not url.startswith(GATEWAY):
                raise AssertionError(f"A09 入口请求没有打到网关：{url}")
            path = url[len(GATEWAY):].split("?", 1)[0]
            action = TRIGGER_ACTIONS.get(path)
            if action is None:
                raise AssertionError(
                    f"A09 出现未登记的入口请求 {path}：采集序列已变化，须同步本测试与各版本证据标签"
                )
            sequence.append((action, variant))
        return sequence


def frozen_action_counts(codex_version: str) -> Mapping[str, Mapping[str, int]]:
    """执行采集脚本收尾校验块里 expected 字典之前的部分，取按版本冻结的动作计数。"""

    source = SCRIPT.read_text(encoding="utf-8")
    heredoc = "python3 - \"$work_dir\" \"$codex_version\" <<'PY'\n"
    loop = "for scenario, wanted in expected.items():"
    for anchor in (heredoc, loop):
        if source.count(anchor) != 1:
            raise AssertionError(f"采集脚本校验块锚点不唯一或缺失：{anchor!r}")
    body_start = source.index(heredoc) + len(heredoc)
    code = source[body_start:source.index(loop, body_start)]
    namespace: dict[str, object] = {}
    with mock.patch.object(sys, "argv", ["capture-check", "/nonexistent-root", codex_version]):
        exec(compile(code, str(SCRIPT), "exec"), namespace)  # noqa: S102 - 只执行常量构造
    expected = namespace["expected"]
    assert isinstance(expected, dict)
    return expected


def label_mismatch(action: str, variant: str, labels: Mapping[str, str]) -> str | None:
    """按动作语义核对规则标签；一致返回 None，否则返回原因。"""

    keys = set(labels)
    compact_only = {"variant", "session_header_scope", "cookie_state"}
    if action in {"models_manifest", "alpha_search"}:
        if labels.get("endpoint_class") != "auxiliary":
            return "辅助端点必须标 endpoint_class=auxiliary"
        if keys & compact_only:
            return f"辅助端点不得带 compact 专属标签 {sorted(keys & compact_only)}"
        return None
    if action in {"images_generation", "images_edit"}:
        if labels.get("endpoint_class") != "images":
            return "图像端点必须标 endpoint_class=images"
        extra = keys & (compact_only | {"track", "mode"})
        if extra:
            return f"图像端点与 Lite／compact 语义无关，不得带 {sorted(extra)}"
        return None
    if action == "responses_cookie_prime":
        # 第 55 项的 Cookie 预热：冷 jar、Responses 会话头作用域，不属于辅助／图像端点面，也不是任何官方变体。
        if labels.get("session_header_scope") != "responses_or_compact":
            return "Cookie 预热必须标 session_header_scope=responses_or_compact"
        if labels.get("cookie_state") != "absent":
            return "Cookie 预热是冷 jar，必须标 cookie_state=absent"
        extra = keys & {"endpoint_class", "variant"}
        if extra:
            return f"Cookie 预热不得带 {sorted(extra)}"
        return None
    if action == "legacy_compact":
        if labels.get("session_header_scope") != "responses_or_compact":
            return "compact 轮必须标 session_header_scope=responses_or_compact"
        if "endpoint_class" in labels:
            return "compact 轮不属于辅助或图像端点面"
        if variant == "prime":
            # cookie_state 标签自 0.149.1 起才有；0.145.0 的冻结声明不写它是合法的，写了就必须是 absent。
            if "variant" in labels or labels.get("cookie_state", "absent") != "absent":
                return "prime 轮不属于官方变体且 Cookie jar 尚未建立"
            return None
        if labels.get("variant") != variant:
            return f"compact 驱动变体是 {variant}，标签却是 {labels.get('variant')}"
        return None
    return f"未知动作 {action}"


def _declarations() -> list[tuple[Path, dict]]:
    paths = sorted(TOOL_ROOT.glob("codex_upgrade_evidence_labels_*.json"))
    if not paths:
        raise AssertionError("找不到任何证据标签声明")
    return [(path, catalog.load_label_declaration(path)) for path in paths]


def _a09_relay_rules(declaration: Mapping) -> list[Mapping]:
    entry = next(item for item in declaration["entries"] if item["job_id"] == JOB_ID)
    return [
        rule
        for rule in entry["rules"]
        if rule["glob"].startswith(A09_RELAY_PREFIX) and rule["glob"].endswith(A09_RELAY_SUFFIX)
    ]


def _connection(index: int) -> str:
    return f"{A09_RELAY_PREFIX}conn{index:03d}{A09_RELAY_SUFFIX}"


def _concrete_path(glob: str) -> str:
    """把非 A09 规则的 glob 落成一个必然命中的具体路径（* 取 001，字符类取首个可用字符）。"""

    def bracket(match: re.Match[str]) -> str:
        body = match.group(1)
        if body.startswith("!"):
            return next(ch for ch in "0123456789" if ch not in body[1:])
        return body[0]

    return re.sub(r"\[([^\]]+)\]", bracket, glob).replace("*", "001")


class A09TriggerSequenceTest(unittest.TestCase):
    def test_workspace_routing_switch_drops_legacy_compact_connections(self) -> None:
        """0.156.1 之前发 prime／default／beta／turn_state 四轮 compact；之后不发 compact，改在 models 后发一次 Cookie 预热。"""

        before = a09_trigger_sequence("0.154.0")
        self.assertEqual(
            [action for action, _ in before],
            ["models_manifest"] + ["legacy_compact"] * 4
            + ["alpha_search", "alpha_search", "images_generation", "images_edit"],
        )
        self.assertEqual(
            [variant for action, variant in before if action == "legacy_compact"],
            ["prime", "default", "beta", "turn_state"],
        )
        for version in ("0.156.1", "0.157.0"):
            self.assertEqual(
                [action for action, _ in a09_trigger_sequence(version)],
                ["models_manifest", "responses_cookie_prime", "alpha_search", "alpha_search",
                 "images_generation", "images_edit"],
            )

    def test_trigger_sequence_matches_frozen_action_counts(self) -> None:
        """入口请求序列与收尾校验块的冻结动作计数逐版本一致（每条连接恰好一次动作）。"""

        for _path, declaration in _declarations():
            version = declaration["codex_version"]
            with self.subTest(version=version):
                counts = Counter(action for action, _ in a09_trigger_sequence(version))
                self.assertEqual(counts, Counter(frozen_action_counts(version)["A09"]))


class A09LabelSequenceTest(unittest.TestCase):
    def test_every_declaration_labels_a09_connections_in_capture_order(self) -> None:
        """每条实际连接恰好命中一条规则，且规则标签与该连接的动作语义一致。"""

        for path, declaration in _declarations():
            version = declaration["codex_version"]
            with self.subTest(declaration=path.name):
                rules = _a09_relay_rules(declaration)
                for index, (action, variant) in enumerate(a09_trigger_sequence(version), start=1):
                    name = _connection(index)
                    hits = [rule for rule in rules if fnmatch.fnmatch(name, rule["glob"])]
                    self.assertEqual(
                        len(hits), 1,
                        f"{path.name} 的 {name}（{action}）命中 {len(hits)} 条规则：{[r['glob'] for r in hits]}",
                    )
                    reason = label_mismatch(action, variant, hits[0]["labels"])
                    self.assertIsNone(reason, f"{path.name} 的 {name}（{action} {variant}）标签错位：{reason}")

    def test_no_a09_rule_points_beyond_actual_connections(self) -> None:
        """规则不得指向实际不存在的连接号（编目会以 glob-unmatched 失败），也不得有死规则。"""

        for path, declaration in _declarations():
            version = declaration["codex_version"]
            with self.subTest(declaration=path.name):
                count = len(a09_trigger_sequence(version))
                actual = [_connection(index) for index in range(1, count + 1)]
                beyond = [_connection(index) for index in range(count + 1, 1000)]
                for rule in _a09_relay_rules(declaration):
                    self.assertTrue(
                        any(fnmatch.fnmatch(name, rule["glob"]) for name in actual),
                        f"{path.name} 的规则 {rule['glob']} 没有命中实际 {count} 条连接中的任何一条",
                    )
                    self.assertFalse(
                        [name for name in beyond if fnmatch.fnmatch(name, rule["glob"])],
                        f"{path.name} 的规则 {rule['glob']} 指向超出实际 {count} 条的连接",
                    )


class A09CatalogCompletenessTest(unittest.TestCase):
    def test_catalog_completes_on_capture_shaped_root_and_keeps_a09_labels(self) -> None:
        """按采集序列造出与真实证据同形的根，编目必须完整，A09 各连接取到对应动作的标签。"""

        for path, declaration in _declarations():
            version = declaration["codex_version"]
            entry = next(item for item in declaration["entries"] if item["job_id"] == JOB_ID)
            with self.subTest(declaration=path.name), tempfile.TemporaryDirectory(prefix="a09-catalog-") as tmp:
                self.assertFalse([rule for rule in entry["rules"] if rule.get("root_suffix")])
                root = Path(tmp) / f"run-{JOB_ID}"
                sequence = a09_trigger_sequence(version)
                files = {_connection(index) for index in range(1, len(sequence) + 1)}
                files.update(
                    _concrete_path(rule["glob"])
                    for rule in entry["rules"]
                    if rule not in _a09_relay_rules(declaration)
                )
                for relative in files:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"x")
                result = catalog.build_catalog(declaration, {JOB_ID: [(root.name, root)]}, side="candidate")
                artifacts = {item["path"]: item for item in result["manifest_draft"]["artifacts"]}
                for index, (action, variant) in enumerate(sequence, start=1):
                    artifact = artifacts[f"{root.name}/{_connection(index)}"]
                    self.assertIsNone(label_mismatch(action, variant, artifact["labels"]))


if __name__ == "__main__":
    unittest.main()
