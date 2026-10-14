"""验证器（``tools/ci/unit_records.py``）与采样器（``tools/ci/declared_sampler.py``）的同口径对拍（步骤 5 工作包 WP1f 的 F9）。

两个被测模块**互相不 import**（验证器是被钉住的纯函数，采样器是驱动目录里的独立脚本）；同口径靠测试交叉核对，本模块同时 import 两边：

1. 常量表逐项相等：git 固定用法的子命令→选项表、全局选项、数字前缀、对象类型词、格式占位符白名单、``--`` 规则子命令集合，以及安装目录表、
   scratch／virtual 前缀表、通配符上限、祖先探测名、容忍的未解析原因、未配对上限、游离标记上限等；
2. 采样器侧的规格 v2 语料（``test_ci_declared_sampler_git_spec.GIT_SPEC_V2_CORPUS``：2152 条 argv 及期望“固定／不固定”，期望值按规格手工构造，不是拿实现
   跑出来的）对验证器 ``_declared_git_usage_problem`` 逐条判定，**结论必须与期望逐条一致**（只比较固定／不固定，不比较原因文字）；
3. 另用固定种子的随机 argv 对采样器与验证器做差分，补上语料没有枚举到的组合；
4. 命令行校验的语法差异：采样器接受而验证器拒绝的（前缀 ``/``、``//x/``、含 ``..``／``.`` 分量、``{WORK}/``、``{HOME}/``、含反斜杠的路径）都是失败关闭，
   本轮不要求对齐；这里只钉住方向——验证器接受的，采样器也必须接受（反方向的差异说明两边漂移了）。

规格里两处歧义，采样器取保守解释、调度者同意、验证器照此：裸 ``--format=oneline`` 判不固定（git 把它当命名格式）；``--pretty=%H``（不带 format:／tformat:
前缀但含 ``%``）按自定义格式串处理，固定。
"""

from __future__ import annotations

import ast
import copy
import random
import unittest
from pathlib import Path

from tools.ci import declared_sampler as ds
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base
from tools.official_client_capture.tests import test_ci_declared_sampler_git_spec as spec

REPO_ROOT = Path(__file__).resolve().parents[3]
A = "a" * 40
B = "b" * 40


class SharedConstantTablesTests(unittest.TestCase):
    """常量表逐项相等（验证器 = 采样器 = 规格 v2 字面量）。"""

    def test_the_git_subcommand_table_is_identical_in_all_three_places(self) -> None:
        self.assertEqual(ur._DECLARED_GIT_FIXED_SUBCOMMANDS, ds._GIT_FIXED_SUBCOMMANDS)
        self.assertEqual(ur._DECLARED_GIT_FIXED_SUBCOMMANDS, spec.GIT_SPEC_V2_SUBCOMMANDS)
        self.assertEqual(list(ur._DECLARED_GIT_FIXED_SUBCOMMANDS), list(ds._GIT_FIXED_SUBCOMMANDS), "子命令顺序也一致（报错信息里的清单顺序相同）")
        for name, (exact, prefixes, mode) in ur._DECLARED_GIT_FIXED_SUBCOMMANDS.items():
            with self.subTest(name):
                self.assertEqual((exact, prefixes, mode), spec.GIT_SPEC_V2_SUBCOMMANDS[name])
                self.assertNotIn("--oneline", exact)

    def test_plain_globals_numeric_prefixes_and_object_types_are_identical(self) -> None:
        self.assertEqual(ur._DECLARED_GIT_PLAIN_GLOBALS, ds._GIT_PLAIN_GLOBALS)
        self.assertEqual(ur._DECLARED_GIT_PLAIN_GLOBALS, spec.GIT_SPEC_V2_PLAIN_GLOBALS)
        self.assertEqual(ur._DECLARED_GIT_NUMERIC_PREFIXES, ds._GIT_NUMERIC_PREFIXES)
        self.assertEqual(ur._DECLARED_GIT_OBJECT_TYPES, ds._GIT_OBJECT_TYPES)

    def test_the_dashdash_subcommand_sets_are_identical_and_complementary(self) -> None:
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_REVISION_COMMANDS, ds._GIT_DASHDASH_REVISION_SUBCOMMANDS)
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_REVISION_COMMANDS, spec.GIT_SPEC_V2_DASHDASH_REVISION_SUBCOMMANDS)
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_PATH_COMMANDS, set(spec.GIT_SPEC_V2_SUBCOMMANDS) - spec.GIT_SPEC_V2_DASHDASH_REVISION_SUBCOMMANDS,
                         "其余五个子命令里 -- 之后才是路径")

    def test_the_format_placeholder_whitelist_is_identical(self) -> None:
        self.assertEqual(set(ur._DECLARED_GIT_FORMAT_PLACEHOLDERS), set(ds._GIT_FORMAT_PLACEHOLDERS))
        self.assertEqual(set(ur._DECLARED_GIT_FORMAT_PLACEHOLDERS), set(spec.GIT_SPEC_V2_FORMAT_PLACEHOLDERS))
        self.assertEqual(len(ur._DECLARED_GIT_FORMAT_PLACEHOLDERS), len(set(ur._DECLARED_GIT_FORMAT_PLACEHOLDERS)), "验证器的占位符表没有重复项")
        self.assertTrue(ur._DECLARED_GIT_FORMAT_PLACEHOLDER.pattern.endswith("|" + spec.GIT_SPEC_V2_FORMAT_HEX_RE + ")"),
                        "%x 加恰好两位十六进制：验证器的正则以规格的写法结尾")
        self.assertEqual(ds._GIT_FORMAT_HEX.pattern, spec.GIT_SPEC_V2_FORMAT_HEX_RE)
        self.assertEqual(ur._DECLARED_GIT_FORMAT_PREFIXES, ("--format=", "--pretty="))
        self.assertEqual(tuple(ds._GIT_FORMAT_PREFIXES), spec.GIT_SPEC_V2_FORMAT_PREFIXES)

    def test_the_format_value_forms_behave_as_the_spec_tables_say(self) -> None:
        """验证器没有 ``raw``／``format:``／``tformat:`` 的常量（写在函数里），所以按行为核对规格表：命名格式只有 raw；三种前缀写法的串语义相同。"""

        for name in spec.GIT_SPEC_V2_FORMAT_NAMES:
            for option in ("--format=", "--pretty="):
                self.assertIsNone(ur._declared_git_format_problem(option + name), name)
        for prefix in spec.GIT_SPEC_V2_FORMAT_PREFIXES:
            for option in ("--format=", "--pretty="):
                self.assertIsNone(ur._declared_git_format_problem(f"{option}{prefix}%H%n%an"), prefix)
                self.assertIsNotNone(ur._declared_git_format_problem(f"{option}{prefix}%D"), prefix)
                self.assertIsNone(ur._declared_git_format_problem(f"{option}{prefix}"), "前缀后面是空串也是（空的）自定义格式串")
        for name in ("oneline", "short", "medium", "full", "fuller", "reference", "email", "mboxrd", "myalias", ""):
            self.assertIsNotNone(ur._declared_git_format_problem("--format=" + name), name)
            self.assertIsNotNone(ur._declared_git_format_problem("--pretty=" + name), name)

    def test_the_review_tables_and_limits_are_identical(self) -> None:
        self.assertEqual(ur._DECLARED_INSTALLED_PREFIXES, ds.INSTALLED_DIR_PREFIXES)
        self.assertEqual(ur._DECLARED_SCRATCH_PREFIXES, ds.SCRATCH_CLASS_PREFIXES)
        self.assertEqual(ur._DECLARED_VIRTUAL_PREFIXES, ds.VIRTUAL_CLASS_PREFIXES)
        self.assertEqual(ur._DECLARED_MAX_GLOB_WILDCARDS, ds.MAX_GLOB_WILDCARDS)
        self.assertEqual(ur._DECLARED_CACHE_CONTRACT_LOCKS, ds.CACHE_CONTRACT_LOCK_FILES)

    def test_the_other_shared_vocabularies_and_limits_are_identical(self) -> None:
        self.assertEqual(ur._DECLARED_ANCESTOR_PROBES, ds.ANCESTOR_PROBES)
        self.assertEqual(ur.DECLARED_TOLERATED_UNRESOLVED, ds.DECLARED_TOLERATED_UNRESOLVED)
        self.assertEqual(ur._DECLARED_MAX_UNFINISHED_AT_EOF, ds.DECLARED_MAX_UNFINISHED_AT_EOF)
        self.assertEqual(ur._DECLARED_MAX_DETACHED_MARKERS, ds.DETACHED_MARKER_LIMIT)
        self.assertEqual(ur._DECLARED_MAX_UNFINISHED_SAMPLES, ds.MAX_UNFINISHED_SAMPLES)
        self.assertEqual(ur._DECLARED_MAX_SAMPLE_CHARS, ds.SAMPLE_LINE_CHARS)
        self.assertEqual(set(ur.DECLARED_KINDS), set(ds.KIND_BITS))
        self.assertEqual(set(ur.DECLARED_CLASSES), set(ds.OUTSIDE_CLASSES))
        self.assertEqual(ur.DECLARED_SAMPLE_SCHEMA, ds.SAMPLE_SCHEMA)
        self.assertEqual(ur.DECLARED_SCOPE_SCHEMA, ds.SCOPE_SCHEMA)
        self.assertEqual(ur.DECLARED_REGISTRY_SCHEMA, ds.REGISTRY_SCHEMA)

    def test_the_two_modules_do_not_import_each_other_at_module_level(self) -> None:
        """验证器是被钉住的纯函数，不得 import 采样器（任何位置的 import 语句，以及以采样器名字为参数的 importlib 调用都不行）；
        采样器不在模块顶层 import 验证器（它只按文件路径动态加载，用于 draft 自检，不在本轮范围）。"""

        validator_tree = ast.parse((REPO_ROOT / "tools/ci/unit_records.py").read_text(encoding="utf-8"))
        for node in ast.walk(validator_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any("declared_sampler" in alias.name for alias in node.names), ast.dump(node))
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("declared_sampler", node.module or "", ast.dump(node))
                self.assertFalse(any("declared_sampler" in alias.name for alias in node.names), ast.dump(node))
            elif isinstance(node, ast.Call):
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                    self.assertFalse(isinstance(argument, ast.Constant) and isinstance(argument.value, str) and "declared_sampler" in argument.value,
                                     f"不得以采样器的名字为参数动态加载：{ast.dump(node)[:120]}")
        sampler_tree = ast.parse((REPO_ROOT / "tools/ci/declared_sampler.py").read_text(encoding="utf-8"))
        for node in sampler_tree.body:
            if isinstance(node, ast.Import):
                self.assertFalse(any("unit_records" in alias.name for alias in node.names), ast.dump(node))
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("unit_records", node.module or "", ast.dump(node))
                self.assertFalse(any("unit_records" in alias.name for alias in node.names), ast.dump(node))


class GitCorpusParityTests(unittest.TestCase):
    """用采样器侧的规格 v2 语料逐条核对验证器的 git 固定用法判定。"""

    def test_the_corpus_is_the_big_one_with_both_verdicts(self) -> None:
        corpus = spec.GIT_SPEC_V2_CORPUS
        self.assertGreaterEqual(len(corpus), 2000)
        fixed = sum(1 for _argv, expected in corpus if expected)
        self.assertGreaterEqual(fixed, 600)
        self.assertGreaterEqual(len(corpus) - fixed, 1400)

    def test_the_validator_judges_every_corpus_entry_as_the_spec_expects(self) -> None:
        wrong = []
        for argv, expected in spec.GIT_SPEC_V2_CORPUS:
            problem = ur._declared_git_usage_problem(argv)
            if (problem is None) != expected:
                wrong.append((argv, "期望固定" if expected else "期望不固定", problem))
        self.assertEqual(wrong, [], f"{len(wrong)} 条与规格期望不一致，前 10 条：{wrong[:10]}")

    def test_the_sampler_and_the_validator_agree_on_every_corpus_entry(self) -> None:
        disagreements = [argv for argv, _expected in spec.GIT_SPEC_V2_CORPUS if (ds.git_usage_problem(argv) is None) != (ur._declared_git_usage_problem(argv) is None)]
        self.assertEqual(disagreements, [])

    def test_every_unfixed_verdict_of_the_validator_comes_with_a_reason(self) -> None:
        for argv, expected in spec.GIT_SPEC_V2_CORPUS:
            problem = ur._declared_git_usage_problem(argv)
            if expected:
                self.assertIsNone(problem, argv)
            else:
                self.assertIsInstance(problem, str, argv)
                self.assertTrue(problem, argv)

    def test_ls_tree_abbrev_is_unfixed_in_both_modules_and_in_the_corpus(self) -> None:
        """WP1g：``ls-tree --abbrev=<n>`` 去掉（``<n>`` 只是缩写长度的下限，对象库出现碰撞时输出会更长）：两个模块都判不固定，语料里有这条期望，
        常量表里 ls-tree 没有带值前缀、数字前缀只剩 ``--max-count=``。"""

        index = {tuple(argv): expected for argv, expected in spec.GIT_SPEC_V2_CORPUS}
        for argv in (["git", "ls-tree", "--abbrev=7", A], ["git", "ls-tree", "--abbrev=40", A], ["git", "ls-tree", "-r", "--abbrev=7", A]):
            with self.subTest(argv=argv):
                self.assertIs(index.get(tuple(argv)), False, "语料里有这条且期望不固定")
                self.assertIsNotNone(ds.git_usage_problem(argv))
                self.assertIsNotNone(ur._declared_git_usage_problem(argv))
        self.assertIs(index[("git", "ls-tree", "-r", "--name-only", A)], True, "去掉 --abbrev= 的同一条命令仍然固定")
        self.assertEqual(ds._GIT_FIXED_SUBCOMMANDS["ls-tree"][1], ())
        self.assertEqual(ur._DECLARED_GIT_FIXED_SUBCOMMANDS["ls-tree"][1], ())
        self.assertEqual(ds._GIT_NUMERIC_PREFIXES, ("--max-count=",))
        self.assertEqual(ur._DECLARED_GIT_NUMERIC_PREFIXES, ("--max-count=",))

    def test_the_two_spec_ambiguities_are_resolved_the_conservative_way(self) -> None:
        self.assertGreaterEqual(len(spec.GIT_SPEC_V2_UNSPECIFIED), 20)
        for argv, expected, _note in spec.GIT_SPEC_V2_UNSPECIFIED:
            self.assertEqual(ur._declared_git_usage_problem(argv) is None, expected, argv)
        self.assertIsNotNone(ur._declared_git_usage_problem(["git", "show", "--format=oneline", A]))
        self.assertIsNone(ur._declared_git_usage_problem(["git", "show", "--pretty=%H", A]))
        self.assertIsNone(ur._declared_git_usage_problem(["git", "show", "--format=format:%H", A]))
        self.assertIsNone(ur._declared_git_usage_problem(["git", "show", "--format=tformat:%H", A]))

    def test_every_placeholder_example_of_the_spec_is_judged_the_same_way(self) -> None:
        for placeholder, text in spec.GIT_SPEC_V2_ALLOWED_PLACEHOLDERS.items():
            for option in (f"--format={text}", f"--pretty=format:{text}", f"--pretty=tformat:{text}"):
                with self.subTest(option=option):
                    self.assertIsNone(ur._declared_git_usage_problem(["git", "show", "-s", option, A]), placeholder)
        for placeholder, text in spec.GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS.items():
            for option in (f"--format={text}", f"--pretty=format:{text}", f"--pretty=tformat:{text}"):
                with self.subTest(option=option):
                    self.assertIsNotNone(ur._declared_git_usage_problem(["git", "show", "-s", option, A]), placeholder)
        for sequence in spec.GIT_SPEC_V2_OTHER_UNLISTED_SEQUENCES:
            with self.subTest(sequence=sequence):
                self.assertIsNotNone(ur._declared_git_usage_problem(["git", "show", f"--format=x{sequence}y", A]), sequence)

    def test_a_seeded_random_differential_finds_no_disagreement(self) -> None:
        """语料之外再补一份固定种子的随机 argv 差分：子命令、选项、修订、格式值、数字、``--`` 随意组合，采样器与验证器的固定／不固定判定必须一致。"""

        subcommands = ["show", "cat-file", "rev-parse", "merge-base", "ls-tree", "diff-tree", "log", "rev-list", "status", "diff", "blame", "describe"]
        options = ["-s", "--no-patch", "--quiet", "--stat", "--name-only", "-p", "-t", "-e", "-r", "-d", "-l", "-z", "--verify", "-q", "--is-ancestor", "--all", "--long",
                   "--full-name", "--full-tree", "--root", "-M", "-C", "--raw", "--parents", "--no-walk", "--reverse", "--topo-order", "--first-parent", "--no-merges",
                   "--count", "--objects", "--not", "-n", "-3", "-٣", "--oneline", "--abbrev=8", "--abbrev=²", "--abbrev=", "--max-count=3", "--max-count=x",
                   "--max-count=", "--max-count=٣", "--format=%H", "--format=%D", "--format=raw", "--format=oneline", "--format=", "--format=%%D", "--format=%x00",
                   "--format=%x0", "--format=%xZZ", "--format=%H %an", "--pretty=%H", "--pretty=format:%h", "--pretty=tformat:%s", "--pretty=", "--pretty=raw",
                   "--pretty=short", "--pretty=format:", "--format=format:%D", "--format=tformat:%H", "--format=%", "--format=%(describe)", "--format=%aD",
                   "--format=%aI", "--format=100%%", "--format=%%%D", "--format=% H", "--format=%+H", "-c", "--no-pager", "--no-optional-locks", "--git-dir=x",
                   "--", "--", "--name-status"]
        revisions = [A, B, A + "^{tree}", A + "^{commit}", A + ":docs", A + ":HEAD", f"{A}..{B}", f"{A}...{B}", "^" + A, "HEAD", "main", "@", "abcdef0", A.upper(),
                     A + "0", "refs/heads/x", f"{A}..HEAD", "blob", "tree", "commit", "docs/egress", "HEAD~1", "HEAD@{1}", "ORIG_HEAD"]
        globals_ = ["--no-pager", "--no-optional-locks", "-C", "-c", "--paginate"]
        rng = random.Random(20261015)
        disagreements = []
        for _ in range(30000):
            argv = ["git"]
            if rng.random() < 0.25:
                argv += rng.sample(globals_, rng.randint(1, 2))
            argv.append(rng.choice(subcommands))
            for _index in range(rng.randint(0, 6)):
                argv.append(rng.choice(options) if rng.random() < 0.45 else rng.choice(revisions))
            if rng.random() < 0.05:
                argv.insert(rng.randint(1, len(argv)), "-n")
            if (ds.git_usage_problem(argv) is None) != (ur._declared_git_usage_problem(argv) is None):
                disagreements.append(argv)
        self.assertEqual(disagreements[:10], [], f"{len(disagreements)} 条判定不一致")


class GlobMatcherParityTests(unittest.TestCase):
    def test_the_sampler_matcher_and_the_validator_matcher_agree_on_random_patterns(self) -> None:
        rng = random.Random(20261015)
        checked = 0
        for _ in range(4000):
            pattern = "".join(rng.choice("ab*?/.") for _ in range(rng.randint(1, 8))).strip("/") or "a"
            try:
                matcher = ds.PathMatcher(pattern)
                ur._declared_ignored_item(pattern, "repo.repo_ignored")
            except (ValueError, ds.SamplerError):
                continue
            text = "".join(rng.choice("ab/.*?") for _ in range(rng.randint(0, 9)))
            self.assertEqual(matcher.matches(text), ur._declared_glob(pattern).fullmatch(text) is not None, (pattern, text))
            checked += 1
        self.assertGreater(checked, 500)


class CommandLineSyntaxDirectionTests(unittest.TestCase):
    """命令行校验（采样器）与范围校验（验证器）的语法差异：只钉方向——验证器接受的，采样器也接受。

    已确认的差异（采样器接受而验证器拒绝，最终都被记录层拒绝＝失败关闭，不是安全问题，本轮不要求对齐）：outside 前缀 ``/``、``//x/``、含 ``..``／``.`` 分量的
    路径、``{WORK}/``、``{HOME}/``、含反斜杠的路径，以及 ``{TMP}/../x/``、``{TMP}//x/`` 这类别名路径里的坏分量。"""

    @staticmethod
    def accepted(function, *args) -> bool:
        try:
            function(*args)
        except (ValueError, ds.SamplerError):
            return False
        return True

    def test_every_unix_socket_and_transient_the_validator_accepts_the_sampler_accepts_too(self) -> None:
        pool = ["/run/x.sock", "/", "//x", "/x/", "/x//y", "/x/./y", "/x/../y", "x", "", "@abs", "/a\\b", "/a\nb", "/a\0b", "/run/a b.sock", "/run/ä.sock",
                "{WORK}/x.sock", "{HOME}/x.sock", "{TMP}/x", "/x/y.sock", "/tmp/x", "/var/run/docker.sock", "/."]
        for value in pool:
            with self.subTest(unix=value):
                self.assertEqual(self.accepted(ds.check_unix_socket, value), self.accepted(ur._declared_unix_socket, value, "unix_sockets"))
        ignored = ["frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*", "out/", "a/b/", "a/*", "a/**", "a/[x]", "a/?", "*", "?", "/", "/x", "x/", "//x/",
                   "x//y", "x/./y", "x/../y", "./x", "../x", "x\\y", "x\ny", "", "a/*/b/*/c/*/d/*/e", "a*a*a*a*b", "a*a*a*a*a*b", "node_modules/", ".git/", ".git/x",
                   "frontend/node_modules/", "frontend/*", "a b", "ä/"]
        for value in ignored:
            with self.subTest(transient=value):
                self.assertEqual(self.accepted(ds.check_transient, value), self.accepted(ur._declared_ignored_item, value, "repo.repo_ignored"))

    def test_every_outside_prefix_the_validator_accepts_the_sampler_accepts_too(self) -> None:
        def validator_accepts(prefix: str, cls: str) -> bool:
            scope = copy.deepcopy(base.make_scope())
            entry = {"prefix": prefix, "class": cls}
            if cls == "home-config":
                entry["contract"] = "host-config:x"
                scope["contracts"] = [*scope["contracts"], "host-config:x"]
            scope["outside"] = [entry]
            return self.accepted(ur.validate_declared_scope, scope)

        pool = ["/", "//x/", "/x/", "/x", "/x/../y/", "/x/./y/", "/a\\b/", "{WORK}/", "{HOME}/", "{TMP}/", "{TMP}/x/", "{WORK}/go-tmp/", "{WORK}/go-tmp/x/", "{WORK}/x/",
                "{HOME}/.config/go/telemetry/", "{HOME}/.config/go/telemetry/x", "{HOME}/.config/", "/dev/null", "/dev/null/x", "/dev/", "/dev/x", "/proc/",
                "/proc/self/", "/sys/", "/run/", "/run/user/", "/run/x", "/run", "/usr/", "/home/", "/etc/", "/etc/hosts", "relative/", "", "{TREE}/x/", "{X}/x/",
                "/x/y/z/", "{TMP}", "{WORK}", "{HOME}", "{TMP}/../x/", "{TMP}//x/"]
        for cls in ("scratch", "virtual", "system", "home-config"):
            for prefix in pool:
                with self.subTest(cls=cls, prefix=prefix):
                    spec_text = f"{prefix}={cls}" + (":host-config:x" if cls == "home-config" else "")
                    if validator_accepts(prefix, cls):
                        self.assertTrue(self.accepted(ds.parse_class_prefix, spec_text), "验证器接受的前缀，采样器也必须接受（否则两边漂移了）")

    def test_the_known_syntax_differences_are_all_in_the_failing_closed_direction(self) -> None:
        for prefix in ("/", "//x/", "/x/../y/", "/x/./y/", "/a\\b/", "{WORK}/", "{HOME}/", "{TMP}/../x/", "{TMP}//x/"):
            with self.subTest(prefix=prefix):
                scope = copy.deepcopy(base.make_scope())
                scope["outside"] = [{"prefix": prefix, "class": "system"}]
                with self.assertRaises(ValueError):
                    ur.validate_declared_scope(scope)


if __name__ == "__main__":
    unittest.main()
