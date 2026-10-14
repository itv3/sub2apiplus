"""声明级承接的 run 来源（逐次采样）与覆盖规则的前缀约定（步骤 5 工作包 WP1b；被测代码在 ``tools/ci/unit_records.py``）。

两组内容：

1. **run 来源**：记录的 ``read_audit.sample_origin`` 为 ``run`` 时，执行器每次执行都带着 strace 采样，产出的是这一次运行自己的
   ``declared-sample/v1``——它的字节摘要必然不同于登记表里的证据摘要。消费者对这种来源不再要求 ``trace.sha256`` 等于登记的证据摘要，
   改核「这一次运行自己的证据」：封存且摘要对得上、格式合格、属于本单元、命令摘要与登记一致、采样器就是登记的那一个（缺失或不等
   都算问题）、命令绑定照旧、覆盖由证据重算；``sample_binding`` 仍是登记的证据摘要。registry 来源（缺省）的行为逐字不变。
2. **规则前缀约定**：``declared_coverage`` 返回的未覆盖明细里，证据完整性类问题的规则一律以 ``C7`` 开头，真有未声明的访问
   一律不以 ``C7`` 开头——执行器靠这个前缀区分「只因证据不完整」和「真有未声明的访问」。词表是闭合的：新增规则必须先归类。

复用 ``test_ci_declared_inputs_records`` 的夹具（临时 git 仓库＋记录库＋一条合格的声明级记录），不重复造；那个文件的用例一个字没改。
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import re
import unittest
from pathlib import Path

from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base

REGISTERED_SAMPLER = base.SAMPLER


class RunOriginTests(base.DeclaredTestCase):
    """``sample_origin == "run"`` 的消费者核验。"""

    # ---- 夹具 ----

    def run_sample(self, **overrides) -> dict:
        """这一次运行自己采样出的证据：与登记证据同一条命令、同一个采样器、覆盖照样 ok，但字节不同（运行时间、行数是这次的）。"""

        sample = copy.deepcopy(self.world.sample)
        sample["sampled"] = {**sample["sampled"], "utc": "2026-10-16T08:00:00Z",
                             "runs": [{"cache": "warm", "exit_code": 0, "lines": 321, "seconds": 99}]}
        sample["coverage"] = {**sample["coverage"], "lines": 321}
        sample.update(overrides)
        return sample

    def run_item(self, run_sample: dict | None = None, *, audit: dict | None = None, **build_kwargs) -> tuple[dict, str]:
        """把本次运行证据封存进记录库（``logs/<字节摘要>.log``），造一份 ``sample_origin`` 为 run 的记录；返回（记录项, 本次证据摘要）。
        ``sample_binding`` 沿用夹具默认值：登记的证据摘要。"""

        run_sample = run_sample if run_sample is not None else self.run_sample()
        digest = self.world.put_evidence(run_sample)
        overrides = {"sample_origin": "run", "trace": {"path": "evidence.json", "sha256": digest, "stored": "x"}, **(audit or {})}
        return self.world.build(audit_overrides=overrides, **build_kwargs), digest

    def reject(self, item: dict, fragment: str) -> list[str]:
        problems = self.world.check(item)
        self.assertTrue(any(fragment in problem for problem in problems), f"应报「{fragment}」：{problems}")
        return problems

    # ---- 正例 ----

    def test_a_run_origin_record_with_its_own_evidence_passes_in_every_mode(self) -> None:
        item, digest = self.run_item()
        self.assertNotEqual(digest, self.world.evidence_digest, "本次证据与登记证据字节不同——这正是现行检查会误拒的情形")
        self.assertEqual(item["record"]["read_audit"]["sample_origin"], "run")
        self.assertEqual(item["record"]["read_audit"]["trace"]["sha256"], digest)
        self.assertEqual(item["record"]["read_audit"]["sample_binding"]["evidence_sha256"], self.world.evidence_digest, "绑定的仍是登记证据")
        self.assertEqual(self.world.check(item), [])
        self.assertEqual(self.world.check(item, historical_read_audit=True, require_read_audit=True), [])
        self.assertEqual(self.world.check(item, thorough=False), [])

    def test_a_run_origin_record_whose_evidence_is_byte_identical_to_the_registered_one_passes(self) -> None:
        """本次运行证据恰好与登记证据同字节（trace 指向登记摘要，且记录库里确有这一份）：同样通过。"""

        item = self.world.build(audit_overrides={"sample_origin": "run"})
        self.assertEqual(item["record"]["read_audit"]["trace"]["sha256"], self.world.evidence_digest)
        self.assertEqual(self.world.check(item), [])
        self.assertEqual(self.world.check(item, historical_read_audit=True, require_read_audit=True), [])

    def test_a_run_origin_record_verifies_in_a_manifest_and_replays_without_external_reads(self) -> None:
        world = self.world
        item, _digest = self.run_item()
        record = item["record"]
        entry = {"unit_id": world.unit_id, "unit_type": "command", "disposition": "inherited", "record_path": str(item["path"]),
                 "record_sha256": record["record_sha256"], "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"],
                 "test_group": None}
        manifest = ur.build_manifest(run_id="verify-run-origin", mode=ur.FULL_SET_PASS, planned_units=[world.unit_id], units=[entry], test_groups={},
                                     diagnostic=[], policy_sha256="p" * 64, environment=world.environment,
                                     environment_sha256=ur.entries_sha256(world.environment), executor=world.executor,
                                     inheritance_max_age_hours=168.0, decided_at_utc="2026-10-16T01:00:00Z")
        for historical in (False, True):
            self.assertEqual(ur.verify_manifest(manifest, store=world.store, historical=historical), [], historical)
        with base.no_external_reads(world.store.root):
            for require in (False, True):
                facts = dataclasses.replace(item["facts"], historical_read_audit=True, require_read_audit=require)
                self.assertEqual(ur.check_record(world.store, item["path"], item["record"], item["current"], facts), [], require)

    # ---- 反例：本次运行证据本身 ----

    def test_the_run_evidence_must_be_sealed_and_untampered(self) -> None:
        # 本次证据没有封存进记录库（摘要是个从未入库的值）。
        missing = {"path": "evidence.json", "sha256": base.SHA("这一次运行的证据从没入库"), "stored": "x"}
        self.reject(self.world.build(audit_overrides={"sample_origin": "run", "trace": missing}), "封存的采样证据缺失或摘要漂移")
        # 入库后被改了一个字节：文件名里的摘要与内容对不上。
        item, digest = self.run_item()
        stored = self.world.store.log_path(digest)
        stored.chmod(0o600)
        stored.write_bytes(stored.read_bytes() + b" ")
        self.reject(item, "封存的采样证据缺失或摘要漂移")
        # 摘要格式不对。
        for bad in ("short", "G" * 64, "", None, 7):
            with self.subTest(bad=bad):
                trace = {"path": "evidence.json", "sha256": bad, "stored": "x"}
                self.reject(self.world.build(audit_overrides={"sample_origin": "run", "trace": trace}), "trace.sha256 缺失或不是 64 位十六进制")

    def test_a_run_record_pointing_at_the_registered_evidence_needs_that_evidence_in_the_store(self) -> None:
        """trace 指向登记证据，而记录库里没有这一份（逐次采样的记录库只存本次运行的证据）：取不到封存的证据，不可承接。"""

        item = self.world.build(audit_overrides={"sample_origin": "run"})
        stored = self.world.store.log_path(self.world.evidence_digest)
        stored.chmod(0o600)
        stored.unlink()
        self.reject(item, "封存的采样证据缺失或摘要漂移")

    def test_the_run_evidence_must_be_well_formed_belong_to_the_unit_and_name_the_registered_command(self) -> None:
        run = self.run_sample()
        run["unit_id"] = "backend:another-unit"
        self.reject(self.run_item(run)[0], "封存的采样证据属于别的单元")
        # 命令变了：证据自己是一致的（命令摘要重算过），但与范围条目登记的命令摘要不符。
        run = self.run_sample()
        argv = ["go", "test", "./...", "-count=2"]
        run["command"].update(argv=argv, sha256=ur.declared_command_sha256(argv, "backend"))
        self.reject(self.run_item(run)[0], "采样证据的命令摘要与范围条目登记的不符")
        # 工作目录变了同样是另一条命令。
        run = self.run_sample()
        run["command"].update(cwd_rel=".", sha256=ur.declared_command_sha256(run["command"]["argv"], "."))
        self.reject(self.run_item(run)[0], "采样证据的命令摘要与范围条目登记的不符")
        # 证据格式不合格：缺字段、多字段、命令摘要与自己的命令内容不符。
        for label, change in {"缺字段": lambda s: s.pop("network"), "多字段": lambda s: s.update(extra=1),
                              "命令摘要与内容不符": lambda s: s["command"].update(sha256="0" * 64)}.items():
            with self.subTest(label):
                run = self.run_sample()
                change(run)
                self.reject(self.run_item(run)[0], "封存的采样证据读不出来或格式非法")

    def test_the_recorded_command_must_be_the_one_the_run_evidence_describes(self) -> None:
        wrong_argv = {**self.world.spec(), "argv": ["go", "test", "./...", "-count=1", "-v"]}
        self.reject(self.run_item(spec=wrong_argv)[0], "spec.argv 不等于证据的 launcher＋argv")
        wrong_cwd = {**self.world.spec(), "cwd": str(self.world.root / "frontend")}
        self.reject(self.run_item(spec=wrong_cwd)[0], "spec.cwd 不符")
        # 启动前缀也是命令的一部分：证据里的 launcher 与记录执行的不一致。
        run = self.run_sample()
        run["command"]["launcher"] = ["unshare", "-m"]
        self.reject(self.run_item(run)[0], "spec.argv 不等于证据的 launcher＋argv")

    # ---- 反例：采样器 ----

    def test_the_sampler_must_be_present_and_registered_for_run_origin_but_registry_origin_is_unchanged(self) -> None:
        other = self.run_sample()
        other["sampled"]["sampler_sha256"] = "6" * 64
        self.reject(self.run_item(other)[0], "缺少采样器摘要，或与范围条目登记的不一致")
        absent = self.run_sample()
        del absent["sampled"]["sampler_sha256"]
        self.reject(self.run_item(absent)[0], "缺少采样器摘要，或与范围条目登记的不一致")
        # 对照：registry 来源——证据没写采样器摘要照样通过（行为不变），写了但不等仍是问题。
        directory = self._directory.name
        silent = base.make_sample()
        del silent["sampled"]["sampler_sha256"]
        registry_world = base.DeclaredWorld(Path(directory) / "silent", sample=silent)
        self.assertEqual(registry_world.check(), [], "registry 来源：证据没自称采样器，放行（既有行为）")
        wrong = base.make_sample()
        wrong["sampled"]["sampler_sha256"] = "6" * 64
        registry_wrong = base.DeclaredWorld(Path(directory) / "wrong", sample=wrong)
        self.assertTrue(any("采样器摘要" in problem for problem in registry_wrong.check()), "registry 来源：自称的采样器不等，仍是问题")

    # ---- 反例：覆盖由本次证据重算 ----

    def test_coverage_is_recomputed_from_the_run_evidence_not_from_the_recorded_conclusion(self) -> None:
        run = self.run_sample()
        run["repo_accesses"].append(["{TREE}/frontend/src/x.ts", ["read"], True])  # 声明范围外的读取
        item, _digest = self.run_item(run)
        self.assertEqual(item["record"]["read_audit"]["undeclared_count"], 0, "记录里写的结论是 0——不能采信")
        problems = self.reject(item, "重算读集覆盖不成立")
        self.assertTrue(any("C1-undeclared" in problem for problem in problems), problems)
        # 采样器自己判这份采样无效（valid 为假）：证据不完整，同样不可承接。
        invalid = self.run_sample(valid=False, invalid_reasons=["[unresolved] 3 行未解析"])
        self.reject(self.run_item(invalid)[0], "重算读集覆盖不成立")
        # 被采样命令的退出码不是 0。
        failed = self.run_sample(exit_code=1)
        self.reject(self.run_item(failed)[0], "重算读集覆盖不成立")
        # 登记证据本身是好的也没用：run 来源看的是本次证据。
        self.assertEqual(self.world.check(), [])

    # ---- 反例：绑定与来源 ----

    def test_the_sample_binding_must_still_name_the_registered_evidence_and_scope(self) -> None:
        item, digest = self.run_item()
        good = dict(item["record"]["read_audit"]["sample_binding"])
        self.assertEqual(good["evidence_sha256"], self.world.evidence_digest)
        for field, value in (("evidence_sha256", digest), ("command_sha256", "0" * 64), ("scope_sha256", "0" * 64), ("unit_id", "backend:other")):
            with self.subTest(field=field):
                binding = {**good, field: value}
                forged, _ = self.run_item(audit={"sample_binding": binding})
                self.reject(forged, "run 来源）的 sample_binding 与范围条目登记")
        extra = self.run_item(audit={"sample_binding": {**good, "extra": 1}})[0]
        self.reject(extra, "字段集合是闭合的")

    def test_registry_origin_still_requires_the_trace_to_be_the_registered_evidence(self) -> None:
        """对照：来源缺省或写 registry，trace 指向另一份（哪怕封存了、也合格）仍然不可承接——放宽只属于 run 来源。"""

        run = self.run_sample()
        digest = self.world.put_evidence(run)
        trace = {"path": "evidence.json", "sha256": digest, "stored": "x"}
        for overrides in ({"trace": trace}, {"trace": trace, "sample_origin": "registry"}):
            with self.subTest(overrides=sorted(overrides)):
                self.reject(self.world.build(audit_overrides=overrides), "证据绑定（sample_binding、trace.sha256）")
        self.assertEqual(self.world.check(self.world.build(audit_overrides={"trace": trace, "sample_origin": "run"})), [], "同一份证据按 run 来源通过")

    def test_sample_origin_must_be_registry_or_run(self) -> None:
        for bad in ("weird", "", "Run", None, 5, ["run"], {"run": 1}):
            with self.subTest(bad=bad):
                item, _digest = self.run_item(audit={"sample_origin": bad})
                self.reject(item, "sample_origin 只能是 registry 或 run")

    def test_the_other_read_audit_fields_still_apply_to_run_origin(self) -> None:
        for label, overrides, fragment in (("未声明数不为零", {"undeclared_count": 1}, "read_audit 不是通过的"),
                                           ("覆盖未闭合", {"coverage_complete": False}, "read_audit 不是通过的"),
                                           ("状态未通过", {"status": "failed"}, "read_audit 不是通过的"),
                                           ("读集范围不对", {"coverage_scope": "all-file-paths/v1"}, "read_audit 不是通过的"),
                                           ("绑定了别的输入摘要", {"inputs_sha256": "0" * 64}, "绑定的 inputs_sha256 与记录不符"),
                                           ("仓库根不是绝对路径", {"repo_root": "relative/root"}, "repo_root 缺失或不是绝对路径")):
            with self.subTest(label):
                item, _digest = self.run_item(audit=overrides)
                self.reject(item, fragment)


class RulePrefixTests(unittest.TestCase):
    """``declared_coverage`` 的规则名约定：C7 开头＝证据完整性（执行器据此判「只因证据不完整」），C0～C6＝其它。"""

    # 证据完整性类（一律 C7 开头）。C7-detached（被丢弃的 strace 垃圾行超过上限 64）是 WP1d 新增的第 8 条，全表因此由 29 条变为 30 条。
    EVIDENCE = frozenset({"C7-command", "C7-detached", "C7-exit", "C7-format", "C7-invalid", "C7-lines", "C7-stream", "C7-unresolved"})
    # 访问被判未声明（C1～C6，一律不以 C7 开头）。
    # WP1e 新增两条：C1-git-state（读了 .git 下的引用库／索引状态而没有固定的 git 子进程解释）、C6-git-unfixed（git 命令不是固定用法，C6 白名单）；
    # 全表因此由 30 条变为 32 条。
    ACCESS = frozenset({"C1-git-kinds", "C1-git-state", "C1-git-undeclared", "C1-ignored-tracked", "C1-list-undeclared", "C1-undeclared",
                        "C1-untracked-read", "C2-kinds", "C2-outside-undeclared", "C3-missing-undeclared", "C4-write-class", "C4-write-declared",
                        "C4-write-installed", "C5-network", "C5-unix-undeclared", "C6-commit-undeclared", "C6-git",
                        "C6-git-argv-hidden", "C6-git-unfixed", "C6-head"})
    # 不以 C7 开头、但与证据形态或声明范围有关的：证据行里的未知访问类型、不规范的路径（C0-kind、C1-path、C2-path），以及声明范围本身非法
    # （C0-scope，与证据无关）。它们在采样器正常产出的证据里不会出现，出现就说明证据被改过或采样器有缺陷，按「真有问题」处理。
    SHAPE_OR_SCOPE = frozenset({"C0-kind", "C0-scope", "C1-path", "C2-path"})

    def rules(self, sample: dict, scope: dict | None = None, **kwargs) -> set[str]:
        result = ur.declared_coverage(sample, scope if scope is not None else base.make_scope(), **kwargs)
        return {item["rule"] for item in result["undeclared"]}

    def sample(self, change=None) -> dict:
        sample = base.make_sample()
        if change is not None:
            change(sample)
        return sample

    def test_the_rule_vocabulary_is_closed_and_classified_by_prefix(self) -> None:
        source = inspect.getsource(ur.declared_coverage)
        found = set(re.findall(r'"(C[0-9]-[a-z0-9-]+)"', source))
        classified = self.EVIDENCE | self.ACCESS | self.SHAPE_OR_SCOPE
        self.assertEqual(found, classified, "declared_coverage 里出现了没归类的规则名（或归类表里有已不存在的）：新增规则必须先决定属于哪一类")
        self.assertEqual({rule for rule in found if rule.startswith("C7")}, set(self.EVIDENCE), "C7 开头的恰好是证据完整性类")
        self.assertFalse(any(rule.startswith("C7") for rule in self.ACCESS | self.SHAPE_OR_SCOPE))
        self.assertEqual(len(found), len(self.EVIDENCE) + len(self.ACCESS) + len(self.SHAPE_OR_SCOPE), "三类互不重叠")

    def test_evidence_problems_are_reported_only_under_c7_rules(self) -> None:
        def coverage(**changes):
            return lambda s: s["coverage"].update(**changes)

        cases = {
            "采样输出流没有完整结束": (self.sample(coverage(stream_complete=False)), {}, {"C7-stream"}),
            "出现不可容忍的未解析原因": (self.sample(coverage(unresolved=2, unresolved_reasons={"unsupported-call:ptrace": 1,
                                                                                          "symlink-metadata-not-bound:newfstatat": 1})),
                                       {}, {"C7-unresolved"}),
            "流结束时悬着的调用超过上限": (self.sample(coverage(unresolved=65, unresolved_reasons={"unfinished-calls-at-eof": 65}, unfinished_calls=65)),
                                         {}, {"C7-unresolved"}),
            "有未归属的进程事件": (self.sample(coverage(unresolved_process_events=2)), {}, {"C7-unresolved"}),
            "采样没有产生任何输出行": (self.sample(coverage(lines=0)), {}, {"C7-lines"}),
            "被采样命令退出码不是 0": (self.sample(lambda s: s.update(exit_code=1)), {}, {"C7-exit"}),
            "被采样命令超时被终止（退出码 null）": (self.sample(lambda s: s.update(exit_code=None)), {}, {"C7-exit"}),
            "采样器自己判无效": (self.sample(lambda s: s.update(valid=False, invalid_reasons=["[unresolved] 3 行未解析"])), {}, {"C7-invalid"}),
            "证据格式非法（缺字段）": (self.sample(lambda s: s.pop("network")), {}, {"C7-format"}),
            "证据格式非法（整份不是证据）": ({}, {}, {"C7-format"}),
            "证据的命令摘要与登记的不符": (self.sample(), {"command_sha256": "0" * 64}, {"C7-command"}),
        }
        for label, (sample, kwargs, expected) in cases.items():
            with self.subTest(label):
                rules = self.rules(sample, **kwargs)
                self.assertEqual(rules, expected, label)
                self.assertTrue(all(rule.startswith("C7") for rule in rules))
        everything = self.sample(lambda s: (s["coverage"].update(stream_complete=False, lines=0), s.update(exit_code=1, valid=False,
                                                                                                           invalid_reasons=["x"])))
        self.assertEqual(self.rules(everything, command_sha256="0" * 64), {"C7-stream", "C7-lines", "C7-exit", "C7-invalid", "C7-command"},
                         "证据的毛病叠在一起，仍然全是 C7")

    def test_real_undeclared_accesses_are_never_reported_under_c7(self) -> None:
        scope_without_history = base.make_scope()
        del scope_without_history["repo"]["git_history"]

        def repo(*rows):
            return lambda s: s["repo_accesses"].extend(rows)

        def outside(*rows):
            return lambda s: s["outside_accesses"].extend(rows)

        def git(*commands):
            return lambda s: s.update(git_commands=[*s["git_commands"], *commands])

        def network(*items):
            return lambda s: s.update(network=[*s["network"], *items])

        cases = {
            "读了声明范围外的仓库内路径": (repo(["{TREE}/frontend/src/x.ts", ["read"], True]), None, "C1-undeclared"),
            "读了声明范围内的未跟踪文件": (repo(["{TREE}/backend/new.go", ["read"], False]), None, "C1-untracked-read"),
            "写了声明范围内的文件": (repo(["{TREE}/backend/c.go", ["write"], True]), None, "C4-write-declared"),
            "枚举了声明范围外的目录": (repo(["{TREE}/frontend", ["list"], False]), None, "C1-list-undeclared"),
            "探测了声明范围外不存在的路径": (repo(["{TREE}/frontend/nothing", ["missing"], False]), None, "C3-missing-undeclared"),
            "读了 .git 但没登记 git 历史": (repo(["{TREE}/.git/HEAD", ["read"], False]), scope_without_history, "C1-git-undeclared"),
            "写了 .git": (repo(["{TREE}/.git/index", ["write"], False]), None, "C1-git-kinds"),
            "访问了没声明前缀的仓库外路径": (outside(["/opt/secret/", ["read"], 1]), None, "C2-outside-undeclared"),
            "按类别不允许的访问类型": (outside(["/usr/local/go/pkg/", ["write"], 1]), None, "C4-write-class"),
            "外连非回环地址": (network({"family": "AF_INET", "target": "8.8.8.8:53"}), None, "C5-network"),
            "连了没登记的 AF_UNIX 套接字": (network({"family": "AF_UNIX", "target": "/run/other.sock"}), None, "C5-unix-undeclared"),
            "git 命令引用 HEAD": (git(["git", "show", "HEAD:Makefile"]), None, "C6-head"),
            "git 命令引用没登记的提交": (git(["git", "show", "c" * 40 + ":x"]), None, "C6-commit-undeclared"),
            "git 子进程参数不可见": (git(["git", "<argv-not-visible>"]), None, "C6-git-argv-hidden"),
            "git_commands 里混入别的命令": (git(["rm", "-rf", "x"]), None, "C6-git"),
        }
        for label, (change, scope, expected) in cases.items():
            with self.subTest(label):
                rules = self.rules(self.sample(change), scope)
                self.assertIn(expected, rules, label)
                self.assertFalse(any(rule.startswith("C7") for rule in rules), (label, rules))

    def test_shape_and_scope_problems_are_not_c7(self) -> None:
        bad_scope = base.make_scope()
        bad_scope["network"] = "any"
        self.assertEqual(self.rules(self.sample(), bad_scope), {"C0-scope"})
        cases = {
            "证据行里的未知访问类型": (lambda s: s["repo_accesses"].append(["{TREE}/backend/x.go", ["teleport"], True]), {"C0-kind"}),
            "仓库内路径不规范": (lambda s: s["repo_accesses"].append(["{TREE}/backend/../x", ["read"], True]), {"C1-path"}),
            "仓库外路径不规范": (lambda s: s["outside_accesses"].append(["/usr/../etc/", ["read"], 1]), {"C2-path"}),
        }
        for label, (change, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.rules(self.sample(change)), expected, label)

    def test_a_mix_of_both_families_keeps_both_prefixes_visible(self) -> None:
        """既有证据问题又有真实的未声明访问：两类规则同时出现，执行器据此判断「不只是证据不完整」。"""

        sample = self.sample(lambda s: (s["coverage"].update(stream_complete=False), s["repo_accesses"].append(["{TREE}/frontend/src/x.ts", ["read"], True])))
        rules = self.rules(sample)
        self.assertEqual(rules, {"C7-stream", "C1-undeclared"})
        self.assertEqual({rule for rule in rules if rule.startswith("C7")}, {"C7-stream"})
        self.assertEqual({rule for rule in rules if not rule.startswith("C7")}, {"C1-undeclared"})


if __name__ == "__main__":
    unittest.main()
