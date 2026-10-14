"""声明级承接的 ``C7-detached`` 规则（步骤 5 工作包 WP1d；被测代码在 ``tools/ci/unit_records.py`` 的 ``declared_coverage``）。

背景：采样器的过滤器会把 strace 对「中途消失的线程」打印的垃圾行（不带名字的 ``<pid> ???( <detached ...>``，以及行尾是
``<detached ...>`` 或 `` = ?`` 的乱码行）丢弃并计入 ``coverage.detached_markers``。个位数无害（Go、node 运行时的线程被杀或退出就会这样），
大面积出现则说明 strace 流被截断或大批线程消失，丢掉的行里可能有真实的读取。采样器的 ``merge_raws`` 在合并后总数超过 64 时判采样
无效，但**逐次采样（β）的证据由执行器直接从过滤器的 raw 组装、不经 merge_raws**（``valid`` 恒为真），那条上限管不到它。按「别两套标准」，
上限放进登记表证据与逐次证据共用的 ``declared_coverage``：新规则 ``C7-detached``——``detached_markers``（缺省 0）大于 64 就报一条未覆盖
（只报一条，reason 写明上限与实际数），属于「证据不完整」类（C7 开头，执行器据此让记录不可承接、而不让整次运行失败）。

测试分三组：

1. 纯函数：恰好 64 通过、65 触发；缺省不触发；与其它 C7 规则、与真实的未声明访问各自独立报告；格式非法时只报 C7-format；
2. 记录层：registry 来源（登记表里的证据）与 run 来源（这一次运行自己的证据）经 ``check_record`` 都生效；
3. 与采样器、执行器互操作：同一串 strace 行，经采样器合并（登记表那条路）与经执行器组装（逐次采样那条路），两边在 64／65 处划线的位置一致，
   而逐次采样那条路只有这条新规则拦得住。

复用 ``test_ci_declared_inputs_records`` 的夹具；那个文件的用例、``test_ci_declared_inputs_run_origin`` 里除规则词表一行外的用例一个字没改。
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base

try:  # 执行器（步骤 5 的 WP3）：互操作用例要用它组装逐次采样的证据；不在的分支只跳过那一组
    from tools.ci import unit_executor as ue
except ImportError:  # pragma: no cover
    ue = None

LIMIT = 64


def sample_with(detached: int | None = 3, **coverage) -> dict:
    """基准证据（全部落在 ``make_scope`` 范围内，C1～C7 全部通过）：``detached_markers`` 设成 ``detached``（None＝删掉这个键），
    ``coverage`` 里其余键按关键字参数覆盖。"""

    sample = base.make_sample()
    if detached is None:
        del sample["coverage"]["detached_markers"]
    else:
        sample["coverage"]["detached_markers"] = detached
    sample["coverage"].update(coverage)
    return sample


class DetachedRuleTests(unittest.TestCase):
    """``declared_coverage`` 的 C7-detached：纯函数。"""

    def cover(self, sample: dict, scope: dict | None = None, **kwargs) -> dict:
        return ur.declared_coverage(sample, scope if scope is not None else base.make_scope(), **kwargs)

    def rules(self, sample: dict, scope: dict | None = None, **kwargs) -> set[str]:
        return {item["rule"] for item in self.cover(sample, scope, **kwargs)["undeclared"]}

    # ---- 上限 ----

    def test_the_limit_is_sixty_four_and_exactly_sixty_four_still_passes(self) -> None:
        self.assertEqual(ur._DECLARED_MAX_DETACHED_MARKERS, LIMIT)
        for count in (0, 1, 3, 10, 63, 64):
            with self.subTest(count=count):
                result = self.cover(sample_with(count))
                self.assertTrue(result["ok"], result["undeclared"])
                self.assertEqual(result["undeclared"], [])
                self.assertEqual(result["counts"]["undeclared"], 0)

    def test_sixty_five_and_more_trigger_exactly_one_c7_detached_finding(self) -> None:
        for count in (65, 66, 100, 4096, 10 ** 9, 2 ** 70):
            with self.subTest(count=count):
                result = self.cover(sample_with(count))
                self.assertFalse(result["ok"])
                self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-detached"])
                self.assertEqual(result["counts"]["undeclared"], 1, "只报一条，不随垃圾行的数量膨胀")
                item = result["undeclared"][0]
                self.assertEqual((item["path"], item["kinds"]), ("", []), "与 C7-stream、C7-lines 同形：没有路径、没有访问类型")

    def test_the_reason_states_the_limit_and_the_actual_count(self) -> None:
        for count in (65, 70, 123456, 2 ** 70):
            with self.subTest(count=count):
                reason = self.cover(sample_with(count))["undeclared"][0]["reason"]
                self.assertIn(f"共 {count} 条", reason)
                self.assertIn(f"上限 {LIMIT}", reason)

    def test_the_rule_is_not_tied_to_one_scope_or_sample(self) -> None:
        for scope_factory, sample_factory in ((base.make_scope, base.make_sample), (base.frontend_scope, base.frontend_sample)):
            for count, expected in ((0, set()), (LIMIT, set()), (LIMIT + 1, {"C7-detached"}), (10 ** 6, {"C7-detached"})):
                sample = sample_factory()
                sample["coverage"]["detached_markers"] = count
                with self.subTest(sample=sample_factory.__name__, count=count):
                    self.assertEqual(self.rules(sample, scope_factory()), expected)

    def test_an_absent_detached_markers_key_defaults_to_zero_and_does_not_trigger(self) -> None:
        sample = sample_with(None)
        self.assertNotIn("detached_markers", sample["coverage"])
        result = self.cover(sample)
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertTrue(self.cover(sample_with(0))["ok"], "显式写 0 与缺省同样通过")

    def test_the_validator_does_not_enforce_the_limit_so_the_rule_is_the_only_place_it_lives(self) -> None:
        """格式核验只看类型（非负整数）：超过上限的证据格式上是合法的，上限只在覆盖规则里管——两条路径（登记表证据、逐次证据）都走覆盖规则。"""

        sample = sample_with(10 ** 6)
        self.assertEqual(ur._declared_validate_sample(sample)["coverage"]["detached_markers"], 10 ** 6)
        self.assertEqual(self.rules(sample), {"C7-detached"})

    # ---- 格式非法时不报这条 ----

    def test_a_malformed_count_or_a_malformed_sample_is_only_a_format_problem(self) -> None:
        for label, bad in {"负数": -1, "布尔真": True, "布尔假": False, "浮点": 1.5, "整数值的浮点": 65.0, "字符串": "65", "null": None,
                           "数组": [65], "对象": {"n": 65}}.items():
            sample = sample_with(0)
            sample["coverage"]["detached_markers"] = bad  # 直接赋值：sample_with 的 None 表示「删掉这个键」，不是 null
            with self.subTest(label):
                self.assertEqual(self.rules(sample), {"C7-format"})
        broken = sample_with(100)
        broken.pop("network")
        self.assertEqual(self.rules(broken), {"C7-format"}, "证据格式非法时提前返回，只报 C7-format（其它 C7 规则同样如此）")
        self.assertEqual(self.rules({}), {"C7-format"})

    # ---- 与其它规则各自独立 ----

    OTHER_C7 = {
        "C7-stream": (lambda s: s["coverage"].update(stream_complete=False), {}),
        "C7-unresolved": (lambda s: s["coverage"].update(unresolved=1, unresolved_reasons={"unsupported-call:ptrace": 1}), {}),
        "C7-lines": (lambda s: s["coverage"].update(lines=0), {}),
        "C7-exit": (lambda s: s.update(exit_code=1), {}),
        "C7-invalid": (lambda s: s.update(valid=False, invalid_reasons=["[exit] cold 退出码 1，不是 0"]), {}),
        "C7-command": (lambda s: None, {"command_sha256": "0" * 64}),
    }

    def test_it_is_reported_independently_of_every_other_c7_rule(self) -> None:
        for other, (change, kwargs) in self.OTHER_C7.items():
            with self.subTest(other):
                without = sample_with(LIMIT)
                change(without)
                with_markers = sample_with(LIMIT + 1)
                change(with_markers)
                alone = self.cover(without, **kwargs)["undeclared"]
                both = self.cover(with_markers, **kwargs)["undeclared"]
                self.assertEqual({item["rule"] for item in alone}, {other}, "只有那一条证据问题时只报它，64 条标记不额外触发")
                self.assertEqual({item["rule"] for item in both}, {other, "C7-detached"}, "两条各自独立报告")
                self.assertEqual([item for item in both if item["rule"] != "C7-detached"], alone, "新规则不改变别的未覆盖项的内容")

    def test_all_the_c7_problems_together_are_all_visible(self) -> None:
        sample = sample_with(LIMIT + 1)
        for change, _kwargs in self.OTHER_C7.values():
            change(sample)
        self.assertEqual(self.rules(sample, command_sha256="0" * 64),
                         {"C7-stream", "C7-unresolved", "C7-lines", "C7-exit", "C7-invalid", "C7-command", "C7-detached"})
        self.assertTrue(all(rule.startswith("C7") for rule in self.rules(sample, command_sha256="0" * 64)), "全部是证据完整性类，一律 C7 开头")

    def test_a_sampler_merged_invalid_sample_with_too_many_markers_reports_both_invalid_and_detached(self) -> None:
        """采样器合并判无效时（登记表证据的那条路）：valid 为假带 [detached] 原因，覆盖规则也独立地数一遍标记行。"""

        sample = sample_with(LIMIT + 1)
        sample.update(valid=False, invalid_reasons=[f"[detached] 被丢弃的 strace 标记行共 {LIMIT + 1} 条，超过容忍上限 {LIMIT}"])
        self.assertEqual(self.rules(sample), {"C7-invalid", "C7-detached"})

    # ---- 与真实的未声明访问各自独立 ----

    def test_it_is_independent_of_real_undeclared_accesses(self) -> None:
        access = ["{TREE}/frontend/src/x.ts", ["read"], True]
        flagged = sample_with(LIMIT + 1)
        flagged["repo_accesses"].append(access)
        self.assertEqual(self.rules(flagged), {"C1-undeclared", "C7-detached"}, "证据问题与真访问问题同时出现：两类前缀都看得见")
        quiet = sample_with(LIMIT)
        quiet["repo_accesses"].append(access)
        self.assertEqual(self.rules(quiet), {"C1-undeclared"}, "访问问题不会引出 C7-detached")

    def test_the_single_finding_stays_a_single_one_next_to_hundreds_of_others(self) -> None:
        sample = sample_with(10 ** 9)
        sample["repo_accesses"].extend([f"{{TREE}}/frontend/f{index:04d}.ts", ["read"], True] for index in range(300))
        result = self.cover(sample)
        self.assertEqual(result["counts"]["undeclared"], 301, "300 条访问问题＋恰好 1 条 C7-detached")
        self.assertEqual(len(result["undeclared"]), 200, "照旧截断到前 200 条")


class RecordLevelTests(unittest.TestCase):
    """记录层：登记表里的证据（registry 来源）与这一次运行自己的证据（run 来源）一视同仁。"""

    HISTORICAL = {"historical_read_audit": True, "require_read_audit": True}

    def world(self, detached: int | None = 3, **coverage) -> base.DeclaredWorld:
        """登记表证据带 ``detached`` 个标记行的一个完整世界（临时仓库＋记录库＋一条合格记录）。"""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return base.DeclaredWorld(Path(directory.name), sample=sample_with(detached, **coverage))

    def run_item(self, world: base.DeclaredWorld, detached: int | None, **coverage) -> tuple[dict, str]:
        """这一次运行自己采样出的证据（字节不同于登记证据，带 ``detached`` 个标记行）封存进记录库，造一份 run 来源的记录；
        ``sample_binding`` 沿用夹具默认值：登记的证据摘要。"""

        sample = copy.deepcopy(world.sample)
        sample["sampled"] = {**sample["sampled"], "utc": "2026-10-16T08:00:00Z",
                             "runs": [{"cache": "warm", "exit_code": 0, "lines": 321, "seconds": 99}]}
        sample["coverage"] = {**sample["coverage"], "lines": 321, **coverage}
        if detached is None:
            sample["coverage"].pop("detached_markers", None)
        else:
            sample["coverage"]["detached_markers"] = detached
        digest = world.put_evidence(sample)
        overrides = {"sample_origin": "run", "trace": {"path": "evidence.json", "sha256": digest, "stored": "x"}}
        return world.build(audit_overrides=overrides), digest

    # ---- registry 来源 ----

    def test_registry_origin_passes_up_to_the_limit_and_is_rejected_above_it(self) -> None:
        for count in (None, 0, 3, LIMIT):
            with self.subTest(count=count):
                world = self.world(count)
                self.assertEqual(world.check(), [])
                self.assertEqual(world.check(**self.HISTORICAL), [])
        for count in (LIMIT + 1, 1000):
            with self.subTest(count=count):
                world = self.world(count)
                for kwargs in ({}, self.HISTORICAL):
                    problems = world.check(**kwargs)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn("重算读集覆盖不成立", problems[0])
                    self.assertIn("C7-detached", problems[0])
                    self.assertIn("共 1 处未覆盖", problems[0])

    def test_registry_origin_reports_it_next_to_another_evidence_problem(self) -> None:
        world = self.world(LIMIT + 1, stream_complete=False)
        problems = world.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("共 2 处未覆盖", problems[0])
        self.assertIn("C7-detached", problems[0])
        self.assertIn("C7-stream", problems[0])

    # ---- run 来源 ----

    def test_run_origin_judges_the_evidence_of_that_run_not_the_registered_one(self) -> None:
        world = self.world(3)
        self.assertEqual(world.check(), [], "登记证据本身合格：下面的红只可能来自这一次运行自己的证据")
        for count in (None, 0, 3, LIMIT):
            with self.subTest(count=count):
                item, digest = self.run_item(world, count)
                self.assertNotEqual(digest, world.evidence_digest)
                self.assertEqual(item["record"]["read_audit"]["sample_origin"], "run")
                self.assertEqual(world.check(item), [])
                self.assertEqual(world.check(item, **self.HISTORICAL), [])
        for count in (LIMIT + 1, 1000):
            with self.subTest(count=count):
                item, _digest = self.run_item(world, count)
                for kwargs in ({}, self.HISTORICAL):
                    problems = world.check(item, **kwargs)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn("重算读集覆盖不成立", problems[0])
                    self.assertIn("C7-detached", problems[0])

    def test_run_origin_reports_it_next_to_another_evidence_problem(self) -> None:
        world = self.world(3)
        item, _digest = self.run_item(world, LIMIT + 1, stream_complete=False)
        problems = world.check(item)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("共 2 处未覆盖", problems[0])
        self.assertIn("C7-detached", problems[0])
        self.assertIn("C7-stream", problems[0])


@unittest.skipIf(base.sampler is None or base.real_strace is None or ue is None,
                 "声明级采样器、真实 strace 夹具或执行器不在本分支：互操作用例整组跳过")
class SamplerAndExecutorEvidenceTests(unittest.TestCase):
    """同一串 strace 行走两条路：采样器合并（登记表证据）与执行器直接组装（逐次采样，不经 merge_raws）——两边在 64／65 处划同一条线。"""

    UNIT = "backend:go-test"
    ARGV = ["go", "test", "./internal/pkg/...", "-count=1"]

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tree = base.sampler_repo(Path(directory.name).resolve())
        self.aliases = {"TREE": base.real_strace.TREE, "WORK": base.real_strace.ROOT, "HOME": "/root", "TMP": "/tmp"}
        self.lines = "".join(base.real_strace.ALL_SEGMENTS).splitlines(keepends=True)
        # 真实片段自带几条标记行：要凑出的总数从它起算。
        self.baseline = base.sampler.filter_stream(self.lines, self.aliases, unit_id=self.UNIT)["coverage"]["detached_markers"]

    def stream(self, total: int) -> list[str]:
        """真实片段再插入若干条不带名字的 ``???( <detached ...>`` 裸标记，使过滤器数出的标记行总数恰为 ``total``。"""

        self.assertGreaterEqual(total, self.baseline)
        extra = [f"{900000 + index} ???( <detached ...>\n" for index in range(total - self.baseline)]
        return self.lines[:10] + extra + self.lines[10:]

    def registry_evidence(self, total: int) -> dict:
        """登记表那条路：过滤器 → 采样器自己的 merge_raws → 落盘再读回。"""

        return base.written(base.sampler_sample(self.tree, self.stream(total), self.aliases,
                                                class_prefixes=[base.sampler.parse_class_prefix("{HOME}/.config/go/telemetry/=scratch")]))

    def run_evidence(self, total: int) -> dict:
        """逐次采样那条路：过滤器的 raw 由执行器直接组装成证据，不经 merge_raws，valid 恒为真。"""

        raw = base.sampler.filter_stream(self.stream(total), self.aliases, unit_id=self.UNIT)
        self.assertEqual(raw["coverage"]["detached_markers"], total)
        return ue._declared_sample_from_raw(raw, unit_id=self.UNIT, launcher=[], command=self.ARGV, cwd_rel="backend", exit_code=0, seconds=1.0,
                                            tracked=base.sampler.tracked_sets(self.tree), commit="c" * 40, strace_version="6.8",
                                            sampler=base.sampler, records=ue._records_module())

    def rules(self, sample: dict) -> set[str]:
        return {item["rule"] for item in ur.declared_coverage(sample, base.reviewed_scope())["undeclared"]}

    def test_both_paths_pass_up_to_the_limit(self) -> None:
        for total in (self.baseline, 20, LIMIT - 1, LIMIT):
            with self.subTest(total=total):
                registry = self.registry_evidence(total)
                self.assertEqual((registry["valid"], registry["coverage"]["detached_markers"]), (True, total))
                self.assertEqual(self.rules(registry), set())
                self.assertEqual(self.rules(self.run_evidence(total)), set())

    def test_above_the_limit_the_sampler_merge_and_the_verifier_agree_and_the_run_path_is_caught_only_by_the_new_rule(self) -> None:
        for total in (LIMIT + 1, LIMIT + 2, 200):
            with self.subTest(total=total):
                registry = self.registry_evidence(total)
                self.assertFalse(registry["valid"], "采样器合并在超过 64 时判无效（登记表证据那条路自己拦）")
                self.assertTrue(any("[detached]" in reason for reason in registry["invalid_reasons"]), registry["invalid_reasons"])
                self.assertEqual(self.rules(registry), {"C7-invalid", "C7-detached"}, "验证侧对同一份证据独立地数一遍，不依赖采样器的判断")
                run = self.run_evidence(total)
                self.assertTrue(run["valid"], "执行器组装的证据 valid 恒为真、invalid_reasons 恒空——没有采样器的那条上限拦它")
                self.assertEqual(run["invalid_reasons"], [])
                self.assertEqual(self.rules(run), {"C7-detached"}, "只有覆盖规则 C7-detached 能拦住逐次采样的证据")


if __name__ == "__main__":
    unittest.main()
