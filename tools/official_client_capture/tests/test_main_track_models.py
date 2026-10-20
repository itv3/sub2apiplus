"""锁定双轨模型坐标在多处声明之间逐字一致。

主线模型是判据语义的根：每个版本在 capturelib.model 里声明主线模型是不是 Lite 模型
（0.160.0 及更早为非 Lite，0.163.0 起为 Lite）。模型集合分散声明在多个文件里——权威定义、
h1 探针的受控 /models 载荷、候选 relay 的合成 /models 清单、compact 证据的模型白名单——
任何一处漏改都不会当场报错，而是让采集条件与标签声明悄悄脱节。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade,
    extract_compaction_reason,
    h1_wire_probe,
    upstream_byte_relay,
)
from tools.official_client_capture.capturelib import model as capture_model
from tools.official_client_capture.capturelib.model import (
    LITE_TRACK_MODELS,
    MAIN_TRACK_MODELS,
    MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION,
    TRACK_MODELS_BY_CODEX_VERSION,
    ConfigurationError,
    main_track_uses_responses_lite,
    model_expected_use_responses_lite,
    track_expected_use_responses_lite,
    track_models_for_version,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]

# 候选 relay 合成 /models 清单接管后每条模型必须齐备的字段：缺任何一个都会让网关
# 按 serde 默认值定型（effort 为空、summary=auto），与真实上游 authoritative 清单
# 的形态脱节。
SYNTHETIC_MODEL_REQUIRED_FIELDS = (
    "visibility",
    "use_responses_lite",
    "supports_parallel_tool_calls",
    "default_reasoning_level",
    "default_reasoning_summary",
    "supports_reasoning_summary_parameter",
)
# 真实上游 0.154.0 清单（官方 lite-http-response conn001）的默认 effort；
# gpt-6.1-sol 取 0.163.0 客户端内置目录（models-manager/models.json）的 low，真实账号清单
# 的值须由 VC-1 目录证据复核，复核结果与此不同时必须同步合成目录与这里。
SYNTHETIC_MODEL_EXPECTED_EFFORT = {
    "gpt-6-astra": "medium",
    "gpt-5.6-sol": "low",
    "gpt-5.6-luna": "medium",
    "gpt-5.5": "medium",
    "gpt-6.1-sol": "low",
}
# 0.160.0 及更早的版本：主线模型是非 Lite 模型。
NON_LITE_MAIN_VERSIONS = (
    "0.147.0",
    "0.149.1",
    "0.151.0",
    "0.154.0",
    "0.156.1",
    "0.157.0",
    "0.159.2",
    "0.160.0",
)


def probe_models() -> dict[str, bool]:
    payload = json.loads(h1_wire_probe.MODELS_BODY.decode())
    return {item["slug"]: item["use_responses_lite"] for item in payload["models"]}


def synthetic_models(body: bytes) -> dict[str, dict]:
    payload = json.loads(body.decode())
    return {item["slug"]: item for item in payload["models"]}


def declared_lite_flags_by_slug() -> dict[str, set[bool]]:
    """按各版本的模型政策与主线 Lite 声明推出每个 slug 应有的 use_responses_lite 取值集合。

    探针、合成目录等"所有版本共用一份"的载荷，每个 slug 只能有一个取值，所以这里
    返回集合，由调用方断言集合恰有一个元素。
    """

    flags: dict[str, set[bool]] = {}
    for version in TRACK_MODELS_BY_CODEX_VERSION:
        for track in ("main", "lite"):
            expected = track_expected_use_responses_lite(version, track)
            for model in track_models_for_version(version, track):
                flags.setdefault(model, set()).add(expected)
    return flags


class MainTrackModelTests(unittest.TestCase):
    def test_track_sets_are_disjoint_and_nonempty(self) -> None:
        self.assertTrue(MAIN_TRACK_MODELS, "主线模型集合不得为空")
        self.assertTrue(LITE_TRACK_MODELS, "Lite 轨模型集合不得为空")
        self.assertFalse(set(MAIN_TRACK_MODELS) & set(LITE_TRACK_MODELS))
        # 同一版本内两条轨道的模型也不得相交。
        for version in TRACK_MODELS_BY_CODEX_VERSION:
            self.assertFalse(
                set(track_models_for_version(version, "main"))
                & set(track_models_for_version(version, "lite")),
                version,
            )

    def test_versioned_track_policies_are_frozen(self) -> None:
        self.assertEqual(
            track_models_for_version("0.147.0", "main"),
            ("gpt-5.4", "gpt-5.5"),
        )
        self.assertEqual(
            track_models_for_version("0.147.0", "lite"),
            ("gpt-5.6-luna",),
        )
        self.assertEqual(
            track_models_for_version("0.149.1", "main"),
            ("gpt-5.5", "gpt-5.4-mini"),
        )
        self.assertEqual(
            track_models_for_version("0.149.1", "lite"),
            ("gpt-5.6-terra", "gpt-5.6-luna"),
        )
        self.assertEqual(
            track_models_for_version("0.151.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.151.0", "lite"),
            ("gpt-5.6-terra",),
        )
        self.assertEqual(
            track_models_for_version("0.154.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.154.0", "lite"),
            ("gpt-6-astra",),
        )
        self.assertEqual(
            track_models_for_version("0.156.1", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.156.1", "lite"),
            ("gpt-6-astra",),
        )
        self.assertEqual(
            track_models_for_version("0.157.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.157.0", "lite"),
            ("gpt-6-astra",),
        )
        # 0.159.2 内置目录新增默认模型 gpt-6.1-sol（Lite），采集仍沿用两条既有轨道模型。
        self.assertEqual(
            track_models_for_version("0.159.2", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.159.2", "lite"),
            ("gpt-6-astra",),
        )
        # 0.160.0 内置目录与 0.159.2 逐字节相同，同样沿用两条既有轨道模型。
        self.assertEqual(
            track_models_for_version("0.160.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.160.0", "lite"),
            ("gpt-6-astra",),
        )
        # 0.163.0：内置目录相对 0.160.0 只少了 model_messages 里值为 null 的 instructions_variables 键（slug 集合与顺序相同），
        # 但 gpt-5.5 已在 ChatGPT 账号侧下线（上游拒绝），老板令主线改用内置目录排第一的默认模型 gpt-6.1-sol（Lite）；
        # Lite 轨沿用 gpt-6-astra。
        self.assertEqual(
            track_models_for_version("0.163.0", "main"),
            ("gpt-6.1-sol",),
        )
        self.assertEqual(
            track_models_for_version("0.163.0", "lite"),
            ("gpt-6-astra",),
        )

    def test_main_track_lite_declaration_is_explicit_and_frozen(self) -> None:
        """主线是不是 Lite 模型按版本显式声明：键集合与政策表逐版本相同，旧版本一字不改。"""

        self.assertEqual(
            set(MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION),
            set(TRACK_MODELS_BY_CODEX_VERSION),
            "新增版本必须在主线 Lite 声明里显式表态，不允许缺省",
        )
        for version in NON_LITE_MAIN_VERSIONS:
            with self.subTest(version=version):
                self.assertIs(main_track_uses_responses_lite(version), False)
                self.assertIs(track_expected_use_responses_lite(version, "main"), False)
        # 0.163.0 起主线是 Lite 模型。
        self.assertIs(main_track_uses_responses_lite("0.163.0"), True)
        self.assertIs(track_expected_use_responses_lite("0.163.0", "main"), True)
        # Lite 轨按定义恒为 True，不论主线声明。
        for version in TRACK_MODELS_BY_CODEX_VERSION:
            with self.subTest(version=version):
                self.assertIs(track_expected_use_responses_lite(version, "lite"), True)
        # 声明值必须是真正的布尔，不能是 0／1 之类可被隐式转换的值。
        for value in MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION.values():
            self.assertIsInstance(value, bool)

    def test_declaration_helpers_read_the_declaration_and_fail_closed(self) -> None:
        # 按模型反查：同一版本里主线模型随声明走，Lite 轨恒为 True。
        self.assertIs(model_expected_use_responses_lite("0.163.0", "gpt-6.1-sol"), True)
        self.assertIs(model_expected_use_responses_lite("0.163.0", "gpt-6-astra"), True)
        self.assertIs(model_expected_use_responses_lite("0.160.0", "gpt-5.5"), False)
        self.assertIs(model_expected_use_responses_lite("0.160.0", "gpt-6-astra"), True)
        # 不在该版本两条轨道里的模型一律失败关闭（gpt-5.5 在 0.163.0 已不是受管模型）。
        for version, model in (
            ("0.163.0", "gpt-5.5"),
            ("0.163.0", "gpt-6-luna"),
            ("0.160.0", "gpt-6.1-sol"),
        ):
            with self.subTest(version=version, model=model), self.assertRaises(ConfigurationError):
                model_expected_use_responses_lite(version, model)
        # 缺政策的版本、非法轨道失败关闭。
        for call in (
            lambda: main_track_uses_responses_lite("0.162.0"),
            lambda: track_expected_use_responses_lite("0.163.0", "side"),
            lambda: track_expected_use_responses_lite("0.145.0", "lite"),
        ):
            with self.assertRaises(ConfigurationError):
                call()
        # 政策里有、声明里没有的版本不得被当成"默认非 Lite"悄悄放过。
        trimmed = {
            version: flag
            for version, flag in MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION.items()
            if version != "0.163.0"
        }
        with mock.patch.object(
            capture_model, "MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION", trimmed
        ), self.assertRaisesRegex(ConfigurationError, "主线 use_responses_lite 声明"):
            main_track_uses_responses_lite("0.163.0")

    def test_slug_lite_flag_is_consistent_across_versions(self) -> None:
        """探针与合成目录是所有版本共用的一份载荷：同一个 slug 在各版本里的期望标志必须只有一个。"""

        for slug, flags in declared_lite_flags_by_slug().items():
            self.assertEqual(len(flags), 1, f"{slug} 在不同版本里的 Lite 期望互相矛盾：{flags}")

    def test_probe_models_cover_both_tracks_with_matching_lite_flag(self) -> None:
        """受控 /models 必须覆盖两条轨道，且 lite 标志与各版本的声明一致。

        覆盖不全时官方 CLI 查不到模型元数据会落到默认值；标志写反等于用受控上游
        伪造 Lite 条件——两者都会让 h1-wire 采到的样本与标签声明的 mode 对不上。
        主线模型的标志不再一律为 false，而是读各版本的主线 Lite 声明。
        """

        probe = probe_models()
        for version in TRACK_MODELS_BY_CODEX_VERSION:
            for track in ("main", "lite"):
                expected = track_expected_use_responses_lite(version, track)
                for model in track_models_for_version(version, track):
                    self.assertIn(
                        model, probe, f"h1 探针受控 /models 缺 {version} {track} 模型 {model}"
                    )
                    self.assertIs(
                        probe[model],
                        expected,
                        f"{version} 的 {track} 模型 {model} 声明 use_responses_lite="
                        f"{str(expected).lower()}，探针必须一致",
                    )
        # 0.163.0 起主线是 Lite 模型，探针必须给出 true；旧版本的主线仍是 false。
        self.assertIs(probe["gpt-6.1-sol"], True)
        self.assertIs(probe["gpt-5.5"], False)

    def test_relay_synthetic_models_are_authoritative_and_complete(self) -> None:
        """候选 relay 的 core／aux 合成 /models 必须与官方采集条件等价。

        官方采集走真实上游，清单含 visibility=list 而被整体接管；候选合成清单若缺
        visibility 或缺 Lite 轨模型，网关就回落 bundled 快照并把新模型当未知模型
        判成非 Lite（0.154 gpt-6-astra 的 VC-5 失败根因）。这里锁定：两份清单都
        覆盖受管版本的 Lite 轨，core 还覆盖主轨；每条模型五个能力位齐全且
        visibility=list；lite 标志与各版本声明一致，并与 h1 探针受控清单同 slug 一致；
        默认 effort 与真实上游一致。
        """

        probe = probe_models()
        declared = declared_lite_flags_by_slug()
        core = synthetic_models(upstream_byte_relay.SYNTHETIC_CORE_MODELS_BODY)
        aux = synthetic_models(upstream_byte_relay.SYNTHETIC_AUX_MODELS_BODY)
        # 0.154.0 是合成目录最初对齐的真实上游清单；0.163.0 起主线换成 Lite 模型 gpt-6.1-sol，
        # core 要覆盖它的两条轨道，aux 只覆盖 Lite 轨（frozen-aux 作业固定 Lite 模型）。
        core_required = set()
        aux_required = set()
        for version in ("0.154.0", "0.163.0"):
            core_required |= set(track_models_for_version(version, "lite")) | set(
                track_models_for_version(version, "main")
            )
            aux_required |= set(track_models_for_version(version, "lite"))
        for name, manifest, required in (
            ("core", core, core_required),
            ("aux", aux, aux_required),
        ):
            for model in required:
                self.assertIn(model, manifest, f"relay {name} 合成 /models 缺 {model}")
            self.assertTrue(
                any(item["visibility"] == "list" for item in manifest.values()),
                f"relay {name} 合成 /models 无 visibility=list，清单不会被接管",
            )
            for slug, item in manifest.items():
                for field in SYNTHETIC_MODEL_REQUIRED_FIELDS:
                    self.assertIn(field, item, f"relay {name} 合成 /models 的 {slug} 缺 {field}")
                self.assertEqual(item["visibility"], "list", slug)
                self.assertIs(item["supports_parallel_tool_calls"], True, slug)
                self.assertIs(item["supports_reasoning_summary_parameter"], True, slug)
                self.assertEqual(item["default_reasoning_summary"], "none", slug)
                self.assertEqual(
                    item["default_reasoning_level"],
                    SYNTHETIC_MODEL_EXPECTED_EFFORT[slug],
                    f"{slug} 默认 effort 与已核对的目录值不一致",
                )
                if slug in probe:
                    self.assertIs(item["use_responses_lite"], probe[slug], slug)
                # 已进入任一版本政策的模型，标志必须等于声明推出的唯一取值。
                if slug in declared:
                    (expected,) = declared[slug]
                    self.assertIs(item["use_responses_lite"], expected, slug)
        # 主线 Lite 模型在 core／aux 里都是 Lite；旧主线 gpt-5.5 仍保留非 Lite 条目（历史版本复算要用）。
        self.assertIs(core["gpt-6.1-sol"]["use_responses_lite"], True)
        self.assertIs(aux["gpt-6.1-sol"]["use_responses_lite"], True)
        self.assertIs(core["gpt-5.5"]["use_responses_lite"], False)

    def test_compaction_allowed_models_cover_both_tracks(self) -> None:
        missing = (set(MAIN_TRACK_MODELS) | set(LITE_TRACK_MODELS)) - (
            extract_compaction_reason.ALLOWED_MODELS
        )
        self.assertFalse(missing, f"compact 证据白名单缺模型：{sorted(missing)}")

    def test_scenario_manifests_follow_the_declaration(self) -> None:
        """每个受管版本的场景清单里，显式模型字段与步骤环境变量镜像都等于声明。"""

        for version in TRACK_MODELS_BY_CODEX_VERSION:
            path = TOOL_ROOT / f"codex_upgrade_scenarios_{version.replace('.', '_')}.json"
            if not path.is_file():
                # 0.159.2 从未生效，没有独立场景清单（由 0.160.0 改名承接）。
                continue
            with self.subTest(version=version):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                explicit_main = 0
                for job in manifest["capture_jobs"]:
                    if "track" not in job:
                        continue
                    declared = track_expected_use_responses_lite(version, job["track"])
                    self.assertIs(job["expected_use_responses_lite"], declared, job["id"])
                    explicit_main += job["track"] == "main"
                    for step in job["steps"]:
                        environment = step["environment"]
                        if "EXPECT_USE_RESPONSES_LITE" in environment:
                            self.assertEqual(
                                environment["EXPECT_USE_RESPONSES_LITE"],
                                str(declared).lower(),
                                job["id"],
                            )
                            self.assertEqual(environment["MODEL_TRACK"], job["track"], job["id"])
                        if "COMPACTION_FIRST_USE_RESPONSES_LITE" in environment:
                            self.assertEqual(
                                environment["COMPACTION_FIRST_USE_RESPONSES_LITE"],
                                str(track_expected_use_responses_lite(version, "main")).lower(),
                                job["id"],
                            )
                self.assertGreater(explicit_main, 0, f"{version} 清单应有显式主线轨道作业")
                codex_upgrade._validate_scenario_manifest_shape(manifest)

    def test_0163_main_jobs_are_lite_and_compaction_jobs_declare_the_first_model(self) -> None:
        """0.163.0 的 4 个显式主线作业改成 Lite；压缩场景的首模型（主线）显式注入 Lite 声明。"""

        manifest = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_163_0.json").read_text(encoding="utf-8")
        )
        by_id = {job["id"]: job for job in manifest["capture_jobs"]}
        explicit_main = sorted(
            job_id for job_id, job in by_id.items() if job.get("track") == "main"
        )
        self.assertEqual(
            explicit_main,
            [
                "official-relay-http-response",
                "official-relay-http-response-plain",
                "official-relay-legacy-compact-default",
                "official-relay-reasoning-numeric-http",
            ],
        )
        for job_id in explicit_main:
            self.assertIs(by_id[job_id]["expected_use_responses_lite"], True, job_id)
            self.assertEqual(by_id[job_id]["model_id"], "{model}", job_id)
        # 其余显式字段的作业都是 Lite 轨。
        for job_id, job in by_id.items():
            if job.get("track") == "lite":
                self.assertIs(job["expected_use_responses_lite"], True, job_id)
        # 压缩场景的第一模型就是主线模型，其 Lite 标志取声明（true），第二模型是 Lite 轨。
        for job_id in ("official-relay-comp-hash-changed", "official-relay-model-downshift"):
            environment = by_id[job_id]["steps"][0]["environment"]
            self.assertEqual(environment["MODEL"], "{model}")
            self.assertEqual(environment["COMPACTION_SECOND_MODEL"], "{lite_model}")
            self.assertEqual(environment["COMPACTION_FIRST_USE_RESPONSES_LITE"], "true")

    def test_lite_main_compaction_jobs_use_the_lite_threshold(self) -> None:
        """主线是 Lite 模型时，靠阈值触发自动压缩的主线作业不得比 Lite 轨作业的阈值更高。

        依据：采集脚本注释与提交 798f51d80 记载，Lite 模型的 legacy compact 自动触发不如非 Lite 稳（k44 成功、k46 整轮
        未触发，job 却因收据仍能从 WS 帧取到模型判 complete），Lite 专项因此下调到 2000。0.163.0 的主线 gpt-6.1-sol 与
        gpt-6-astra 同属 Lite（tool_mode=code_mode_only、同一 comp_hash 与窗口），走同一条触发路径，主线作业继续用脚本默认的
        4000 会重蹈 k46；阈值只决定“多早触发”，EXPECT_COMPACTION_REASON=context_limit 与反向路径断言都不变。
        """

        script = (TOOL_ROOT / "run_official_relay_scenario.sh").read_text(encoding="utf-8")
        default = int(re.search(r"compact_token_limit=\$\{COMPACT_TOKEN_LIMIT:-(\d+)\}", script).group(1))
        # 脚本里恰有三个场景把 compact_token_limit 传给 model_auto_compact_token_limit。
        self.assertEqual(script.count("model_auto_compact_token_limit=$compact_token_limit"), 3)
        scenarios = {"turnstate-compact", "legacy-compact-default", "legacy-compact-beta"}
        checked = []
        for version in TRACK_MODELS_BY_CODEX_VERSION:
            if not main_track_uses_responses_lite(version):
                continue
            path = TOOL_ROOT / f"codex_upgrade_scenarios_{version.replace('.', '_')}.json"
            if not path.is_file():
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
            by_id = {job["id"]: job for job in manifest["capture_jobs"]}
            lite_limit = int(by_id["official-lite-legacy-compact-default"]["steps"][0]["environment"]["COMPACT_TOKEN_LIMIT"])
            self.assertLess(lite_limit, default)
            main_jobs = [
                job for job in manifest["capture_jobs"]
                if job["steps"][0]["environment"].get("SCENARIO") in scenarios
                and job["steps"][0]["environment"].get("MODEL") == "{model}"
            ]
            self.assertEqual(len(main_jobs), 3, version)
            for job in main_jobs:
                limit = int(job["steps"][0]["environment"].get("COMPACT_TOKEN_LIMIT", default))
                self.assertLessEqual(limit, lite_limit, f"{version} {job['id']}")
            checked.append(version)
        self.assertEqual(checked, ["0.163.0"], "主线声明为 Lite 的版本都要被检查到")
        # 旧版本（主线非 Lite）的主线作业不受这条约束，仍用脚本默认值——一字不改。
        legacy = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json").read_text(encoding="utf-8"))
        for job in legacy["capture_jobs"]:
            environment = job["steps"][0]["environment"]
            if environment.get("SCENARIO") in scenarios and environment.get("MODEL") == "{model}":
                self.assertNotIn("COMPACT_TOKEN_LIMIT", environment, job["id"])

    def test_scenario_validation_rejects_declaration_drift(self) -> None:
        """清单手写的 Lite 期望与声明相反时，形状校验当场失败，而不是拖到模型条件收据环节。"""

        manifest = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_163_0.json").read_text(encoding="utf-8")
        )

        def mutate(change) -> dict:
            copy = json.loads(json.dumps(manifest))
            change({job["id"]: job for job in copy["capture_jobs"]})
            return copy

        cases = (
            (
                "主线作业字段写回 false",
                lambda jobs: jobs["official-relay-http-response"].update(
                    expected_use_responses_lite=False
                ),
                "expected_use_responses_lite 与 Codex 0.163.0 的 main 轨道声明",
            ),
            (
                "Lite 作业字段写成 false",
                lambda jobs: jobs["official-lite-http-response"].update(
                    expected_use_responses_lite=False
                ),
                "模型轨道契约非法",
            ),
            (
                "环境变量镜像写回 false",
                lambda jobs: jobs["official-relay-http-response"]["steps"][0][
                    "environment"
                ].update(EXPECT_USE_RESPONSES_LITE="false"),
                "EXPECT_USE_RESPONSES_LITE 与 Codex 0.163.0 的 main 轨道声明",
            ),
            (
                "Lite 作业环境变量镜像写成 false",
                lambda jobs: jobs["official-lite-http-response"]["steps"][0][
                    "environment"
                ].update(EXPECT_USE_RESPONSES_LITE="false"),
                "EXPECT_USE_RESPONSES_LITE 与 Codex 0.163.0 的 lite 轨道声明",
            ),
            (
                "压缩首模型声明写回 false",
                lambda jobs: jobs["official-relay-model-downshift"]["steps"][0][
                    "environment"
                ].update(COMPACTION_FIRST_USE_RESPONSES_LITE="false"),
                "COMPACTION_FIRST_USE_RESPONSES_LITE 与 Codex 0.163.0 的 main 轨道声明",
            ),
            (
                "镜像取值不是 true／false",
                lambda jobs: jobs["official-relay-http-response"]["steps"][0][
                    "environment"
                ].update(EXPECT_USE_RESPONSES_LITE="yes"),
                "必须是 true／false",
            ),
        )
        for name, change, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, message
            ):
                codex_upgrade._validate_scenario_manifest_shape(mutate(change))

        # 反向：0.160.0 的主线是非 Lite，把它的主线作业改成 true 同样被拒。
        older = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json").read_text(encoding="utf-8")
        )
        for job in older["capture_jobs"]:
            if job["id"] == "official-relay-http-response":
                job["expected_use_responses_lite"] = True
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "expected_use_responses_lite 与 Codex 0.160.0 的 main 轨道声明",
        ):
            codex_upgrade._validate_scenario_manifest_shape(older)

    def test_candidate_scripts_require_campaign_models(self) -> None:
        """候选脚本不得自行猜测目标版本的两条模型轨道。"""

        here = Path(__file__).resolve().parents[1]
        core = (here / "run_candidate_core_capture.sh").read_text()
        aux = (here / "run_candidate_aux_capture.sh").read_text()

        self.assertIn("main_model=${MAIN_MODEL:?", core)
        self.assertIn("lite_model=${LITE_MODEL:?", core)
        self.assertIn("model=${MODEL:?", aux)
        self.assertNotRegex(core, re.compile(r"^main_model=\$\{MAIN_MODEL:-", re.M))
        self.assertNotRegex(core, re.compile(r"^lite_model=\$\{LITE_MODEL:-", re.M))
        self.assertNotRegex(aux, re.compile(r"^model=\$\{MODEL:-", re.M))

    def test_candidate_core_script_has_no_hardcoded_model(self) -> None:
        """core 脚本的请求体不得再出现裸模型名，只能走两个变量。"""

        core = (Path(__file__).resolve().parents[1] / "run_candidate_core_capture.sh")
        bodies = re.findall(
            r'write_request_body\s+"[^"]+"\s+(\S+)', core.read_text()
        )
        self.assertTrue(bodies, "未解析到任何 write_request_body 调用")
        hardcoded = sorted({b for b in bodies if not b.startswith('"$')})
        self.assertFalse(hardcoded, f"仍有硬编码模型：{hardcoded}")

    def test_h1_probe_path_filter_ignores_startup_models(self) -> None:
        """images 证据槽不能被服务启动期 models 请求抢占。"""

        prefix = "/backend-api/codex/images/"
        self.assertFalse(
            h1_wire_probe.record_matches_path_prefix(
                {
                    "request_line": (
                        "GET /backend-api/codex/models?client_version=0.149.1 "
                        "HTTP/1.1"
                    )
                },
                prefix,
            )
        )
        self.assertTrue(
            h1_wire_probe.record_matches_path_prefix(
                {
                    "request_line": (
                        "POST /backend-api/codex/images/generations HTTP/1.1"
                    )
                },
                prefix,
            )
        )
        self.assertTrue(
            h1_wire_probe.record_matches_path_prefix(
                {"request_line": "GET /anything HTTP/1.1"}, ""
            )
        )

    def test_images_script_enables_image_path_filter(self) -> None:
        """images runner 必须显式启用端点过滤。"""

        script = (
            Path(__file__).resolve().parents[1] / "run_images_wire_probe.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "--record-path-prefix /backend-api/codex/images/",
            script,
        )


def scenario_context(manifest: dict, **overrides: str) -> dict[str, str]:
    """按场景清单自己的 variable_contract 造一份满足类型校验的 Campaign 上下文（测试夹具，不含真值）。"""

    by_type = {
        "string": "value",
        "integer": "1",
        "absolute_path": "/tmp/campaign-fixture",
        "sha256": "a" * 64,
        "image_reference": "capture-runtime@sha256:" + "b" * 64,
    }
    context: dict[str, str] = {}
    for item in manifest["variable_contract"]:
        context[item["name"]] = (
            str(item["default"]) if "default" in item else by_type[item["type"]]
        )
    context.update(overrides)
    return context


class ScenarioLoaderDeclarationTests(unittest.TestCase):
    """场景清单加载器给"没写模型字段"的作业补默认期望时，必须读声明而不是写死 False。"""

    def load_jobs(self, version: str, model: str, lite_model: str):
        path = TOOL_ROOT / f"codex_upgrade_scenarios_{version.replace('.', '_')}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        context = scenario_context(
            manifest, target_version=version, model=model, lite_model=lite_model
        )
        jobs = codex_upgrade.load_scenario_jobs(
            path, context, expected_version=version, require_bindings=True
        )
        explicit = {job["id"] for job in manifest["capture_jobs"] if "track" in job}
        return jobs, explicit

    def test_0163_defaults_and_explicit_jobs_follow_the_lite_declaration(self) -> None:
        jobs, explicit = self.load_jobs("0.163.0", "gpt-6.1-sol", "gpt-6-astra")
        defaulted = [job for job in jobs if job.job_id not in explicit]
        self.assertGreater(len(defaulted), 40, "大多数作业没写模型字段，走加载器默认")
        for job in defaulted:
            with self.subTest(job=job.job_id):
                # 没写模型字段的作业默认走主线轨道，其 Lite 期望取 0.163.0 的声明（True），不再固定 False。
                self.assertEqual(job.track, "main")
                self.assertIs(job.expected_use_responses_lite, True)
                self.assertFalse(job.required_model_receipt)
        for job in jobs:
            if job.job_id in explicit:
                with self.subTest(job=job.job_id):
                    self.assertIs(
                        job.expected_use_responses_lite,
                        track_expected_use_responses_lite("0.163.0", job.track),
                    )
        by_id = {job.job_id: job for job in jobs}
        self.assertEqual(by_id["official-relay-http-response"].model_id, "gpt-6.1-sol")
        self.assertEqual(by_id["official-lite-http-response"].model_id, "gpt-6-astra")
        self.assertTrue(by_id["official-relay-http-response"].required_model_receipt)

    def test_0160_defaults_stay_non_lite(self) -> None:
        """旧版本一字不改：0.160.0 的主线仍是非 Lite 模型，加载器默认仍是 False。"""

        jobs, explicit = self.load_jobs("0.160.0", "gpt-5.5", "gpt-6-astra")
        main_jobs = [job for job in jobs if job.track == "main"]
        self.assertGreater(len(main_jobs), 40)
        for job in main_jobs:
            with self.subTest(job=job.job_id):
                self.assertIs(job.expected_use_responses_lite, False)
        for job in jobs:
            if job.track == "lite":
                self.assertIs(job.expected_use_responses_lite, True, job.job_id)

    def test_pair_validation_reads_the_declaration(self) -> None:
        """升级对校验在 plan 阶段就读主线 Lite 声明，缺声明失败关闭。"""

        codex_upgrade._validate_upgrade_pair_models(
            baseline_version="0.160.0",
            target_version="0.163.0",
            model="gpt-6.1-sol",
            lite_model="gpt-6-astra",
        )
        trimmed = {
            version: flag
            for version, flag in MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION.items()
            if version != "0.163.0"
        }
        with mock.patch.object(
            capture_model, "MAIN_TRACK_RESPONSES_LITE_BY_CODEX_VERSION", trimmed
        ), self.assertRaisesRegex(ConfigurationError, "主线 use_responses_lite 声明"):
            codex_upgrade._validate_upgrade_pair_models(
                baseline_version="0.160.0",
                target_version="0.163.0",
                model="gpt-6.1-sol",
                lite_model="gpt-6-astra",
            )


if __name__ == "__main__":
    unittest.main()
