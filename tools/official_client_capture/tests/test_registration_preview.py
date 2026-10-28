"""登记阶段离线预演（registration_preview）：用上一版真实候选证据回放标签与判据。

夹具是上一版正式候选 Campaign 证据包的一小部分（A06 WebSocket 出站流、A04 两条 Responses POST 的中继原始字节），
见 ``fixtures/prior_candidate_trace/SOURCE.txt``。测试在临时目录里用真实的编目→收口→派生→回填 manifest 路径先生成一份
“上一版证据包”，再对不同的标签声明与期望清单跑预演，覆盖这次要防的两类缺陷：

* 标签按原始帧序号写：候选网关把压缩消息拆成多帧，首个业务帧的标签落在预热消息的续帧上，7 条判据检查选不到观测
  （上一版写成序号 1，实际起始帧在 7）；
* 判据写严：A04 入站是第三方形态，每个请求恰有 1 条用户消息没有逐项 passthrough，判据要求 0。

两类缺陷必须在**同一次预演**里一起报出（上一版是撞一个、修一个，合计在关键路径上耗了约 3 小时）。
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import build_assertion_bundle as bundle_module
from tools.official_client_capture import build_evidence_catalog as catalog_module
from tools.official_client_capture import candidate_rule_assertion as cra
from tools.official_client_capture import derive_official_observations as derive_module
from tools.official_client_capture.tests import chain_rehearsal_blindspots as blindspots
from tools.official_client_capture.tests import registration_preview as preview

TOOL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prior_candidate_trace"
VERSION = "9.9.9"
JOB_ID = "candidate-frozen-core"
A06_GLOB = "scenarios/A06/relay/conn001.client_to_upstream.bin"
# 真实声明里 candidate-frozen-core 的这三条规则（A04 两条、A06 一条），来自上一版正式声明，去掉了与夹具无关的其余规则。
SOURCE_DECLARATION = TOOL_ROOT / "codex_upgrade_evidence_labels_0_164_0.json"
SOURCE_PROFILE = TOOL_ROOT / "candidate_rule_expectations_0_164_0.json"
KEPT_GLOBS = {
    "scenarios/A04/relay/conn002.client_to_upstream.bin",
    "scenarios/A04/relay/conn004.client_to_upstream.bin",
    A06_GLOB,
}


def mini_declaration(frame_labels: dict) -> dict:
    """真实声明的子集；A06 的 frame_labels 换成调用方给的写法。"""

    document = json.loads(SOURCE_DECLARATION.read_text(encoding="utf-8"))
    document["codex_version"] = VERSION
    entry = next(item for item in document["entries"] if item["job_id"] == JOB_ID)
    entry["rules"] = [rule for rule in entry["rules"] if rule["glob"] in KEPT_GLOBS]
    for rule in entry["rules"]:
        if rule["glob"] == A06_GLOB:
            rule["frame_labels"] = frame_labels
    document["entries"] = [entry]
    return document


def mini_profile(*, body_value: int) -> dict:
    """真实期望清单的子集：SPEC-WS-005 里只选 A06 的检查，加 SPEC-BODY-003 的 main-messages-keep-passthrough。"""

    document = json.loads(SOURCE_PROFILE.read_text(encoding="utf-8"))
    document["codex_version"] = VERSION
    rules = {rule["rule_id"]: rule for rule in document["rules"]}
    ws = copy.deepcopy(rules["SPEC-WS-005"])
    ws["scenario_ids"] = ["A06"]
    # 只留读 A06 帧事实的检查：预热两条与首个业务帧七条（field-slots、incremental-prefix-reuse 读别的证据，夹具里没有）。
    ws["checks"] = [
        check for check in ws["checks"]
        if check["id"].startswith(("warmup-", "first-business-frame-"))
    ]
    body = copy.deepcopy(rules["SPEC-BODY-003"])
    body["scenario_ids"] = ["A04"]
    body["checks"] = [check for check in body["checks"] if check["id"] == "main-messages-keep-passthrough"]
    body["checks"][0]["assertion"]["value"] = body_value
    document["rules"] = [ws, body]
    document["scenarios"] = [item for item in document["scenarios"] if item["scenario_id"] in {"A04", "A06"}]
    return document


class PreviewCase(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="registration-preview-test-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.prior = self.build_prior_evidence()

    def write(self, name: str, document: dict) -> Path:
        path = self.work / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def build_prior_evidence(self) -> Path:
        """用真实路径生成“上一版证据包”：声明里 A06 的标签是上一版的写法（原始帧序号 1）。"""

        roots = self.work / "roots"
        root_name = next((FIXTURE / "bundle").glob("*-candidate-frozen-core")).name
        shutil.copytree(FIXTURE / "bundle" / root_name, roots / root_name)
        attempt = self.work / "prior-attempt"
        bundle = attempt / "evidence" / "assertion-bundle"
        bundle.parent.mkdir(parents=True)
        declaration = mini_declaration({"0": {"frame_role": "warmup"}, "1": {"frame_role": "first_business"}})
        built = catalog_module.build_catalog(declaration, {JOB_ID: [(root_name, roots / root_name)]}, side="candidate")
        bundle_module.build_bundle({root_name: roots / root_name}, built["bundle_plan"]["entries"], bundle)
        derive_module.derive_observations(bundle, built["derivation_plan"]["entries"])
        manifest = catalog_module.finalize_manifest(
            built["manifest_draft"], bundle, codex_version=VERSION, capture_id="prior-candidate"
        )
        (bundle / "capture-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (attempt / "attempt.json").write_text(
            json.dumps({"results": [{"id": JOB_ID, "status": "complete", "evidence_roots": [str(roots / root_name)]}]}),
            encoding="utf-8",
        )
        self.roots = roots
        return attempt

    def preview(self, frame_labels: dict, *, body_value: int) -> dict:
        labels = self.write("labels.json", mini_declaration(frame_labels))
        profile = self.write("profile.json", mini_profile(body_value=body_value))
        return preview.run_preview(
            candidate_evidence=self.prior,
            labels=labels,
            profile_path=profile,
            candidate_roots_dir=self.roots,
            candidate_only=True,
        )

    @staticmethod
    def defects(result: dict, kind: str | None = None) -> list[dict]:
        return [item for item in result["defects"] if kind is None or item["kind"] == kind]


ORIGINAL_LABELS = {"0": {"frame_role": "warmup"}, "1": {"frame_role": "first_business"}}
CORRECTED_LABELS = {"0": {"frame_role": "warmup"}, "7": {"frame_role": "first_business"}}
BY_EVENT_LABELS = {
    "event:response.create:0": {"frame_role": "warmup"},
    "event:response.create:1": {"frame_role": "first_business"},
}


class ReplayOfThePriorTwoDefectsTest(PreviewCase):
    def test_original_labels_and_criteria_report_both_defects_in_one_run(self) -> None:
        result = self.preview(ORIGINAL_LABELS, body_value=0)
        self.assertEqual(result["verdict"], "defects_found")
        unreachable = self.defects(result, "selector-unreachable")
        self.assertTrue(unreachable)
        self.assertEqual({item["rule"] for item in unreachable}, {"SPEC-WS-005"})
        self.assertTrue(all(item["check"].startswith("first-business-frame-") for item in unreachable))
        # 诊断给出“原写 1、实际 7”和可直接替换的选择键。
        hints = {item["hint"] for item in unreachable}
        self.assertEqual(len(hints), 1)
        hint = hints.pop()
        self.assertIn("原写 1、实际 7", hint)
        self.assertIn("event:response.create:1", hint)
        self.assertIn("续帧", hint)
        # 第二处缺陷：判据过严，同一次预演里一并报出。
        mismatch = [
            item for item in self.defects(result, "assertion-mismatch")
            if item["rule"] == "SPEC-BODY-003"
        ]
        self.assertEqual([item["check"] for item in mismatch], ["main-messages-keep-passthrough"])
        self.assertIn("[1, 1]", mismatch[0]["message"])
        self.assertIn("判据与实际行为不一致", mismatch[0]["hint"])
        self.assertIsNone(mismatch[0]["same_origin_as"])
        # SPEC-WS-005 的求值失败与选择器未命中同源，单独标注。
        origin = [
            item for item in self.defects(result, "assertion-mismatch") if item["rule"] == "SPEC-WS-005"
        ]
        self.assertEqual(len(origin), len(unreachable))
        self.assertTrue(all(item["same_origin_as"] for item in origin))
        self.assertTrue(self.defects(result, "frame-label-on-continuation"))
        steps = {step["name"]: step for step in result["sides"][0]["steps"]}
        self.assertTrue(steps["selector-reachability"]["details"]["consistent_with_real_gate"])

    def test_corrected_raw_index_and_revised_criteria_pass(self) -> None:
        result = self.preview(CORRECTED_LABELS, body_value=1)
        self.assertEqual(result["verdict"], "clean", result["defects"])
        self.assertEqual(result["defect_count"], 0)

    def test_message_and_event_selectors_pass_without_knowing_the_fragment_count(self) -> None:
        result = self.preview(BY_EVENT_LABELS, body_value=1)
        self.assertEqual(result["verdict"], "clean", result["defects"])
        drift = result["sides"][0]["steps"][0]["details"]["drift"]
        # 对上一版证据包来说，只有 A06 的帧标签变了（"1" → event 键）。
        self.assertEqual(len(drift["changed"]), 1)
        self.assertEqual(drift["changed"][0]["fields"], ["frame_labels"])

    def test_wrong_labels_with_revised_criteria_report_only_the_selector_defect(self) -> None:
        result = self.preview(ORIGINAL_LABELS, body_value=1)
        self.assertEqual({item["rule"] for item in self.defects(result, "selector-unreachable")}, {"SPEC-WS-005"})
        self.assertEqual(
            [item for item in self.defects(result, "assertion-mismatch") if not item["same_origin_as"]], []
        )

    def test_corrected_labels_with_strict_criteria_report_only_the_criteria_defect(self) -> None:
        result = self.preview(CORRECTED_LABELS, body_value=0)
        self.assertEqual(self.defects(result, "selector-unreachable"), [])
        mismatch = self.defects(result, "assertion-mismatch")
        self.assertEqual([(item["rule"], item["check"]) for item in mismatch], [("SPEC-BODY-003", "main-messages-keep-passthrough")])

    def test_selector_beyond_the_stream_is_a_load_failure_not_a_silent_pass(self) -> None:
        result = self.preview({"event:response.create:5": {"frame_role": "x"}}, body_value=1)
        self.assertEqual(result["verdict"], "defects_found")
        self.assertEqual([item["kind"] for item in result["defects"]], ["manifest-load"])
        self.assertIn("没有命中任何消息", result["defects"][0]["message"])


def declared_a06_frame_labels(path: Path) -> dict | None:
    """声明里候选 A06 首条 WS 连接那条规则的 frame_labels；没有这条规则返回 None。"""

    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in document["entries"]:
        if entry["side"] != "candidate":
            continue
        for rule in entry["rules"]:
            if rule["glob"] == A06_GLOB and rule.get("frame_labels"):
                return rule["frame_labels"]
    return None


class TreeDeclarationReplayTest(PreviewCase):
    """本树里的证据标签声明，对真实候选证据回放必须可达。

    整链空跑把本类当作“标签声明对上一版真实候选证据”的探针运行；上一版标签写错帧序号的缺陷放回去，这里就红。
    """

    def test_declared_frame_labels_reach_the_business_message_in_the_real_candidate_stream(self) -> None:
        labels = declared_a06_frame_labels(SOURCE_DECLARATION)
        self.assertIsNotNone(labels, "声明里找不到候选 A06 的 frame_labels 规则")
        result = self.preview(labels, body_value=1)
        self.assertEqual(result["verdict"], "clean", preview.render_text(result))

    def test_every_declared_label_of_the_newest_declaration_lands_on_a_response_create_message_start(self) -> None:
        """登记测试改用真实候选 A06 观测，核对每个 frame_labels 键都落在 response.create 消息的起始帧上
        （手造观测从不核对标签声明与真实分片布局，是上一版标签错位溢出到封存预览的盲区）。"""

        newest_labels, _ = blindspots.newest_target_files(TOOL_ROOT.parent.parent)
        declared = declared_a06_frame_labels(newest_labels)
        if declared is None:
            self.skipTest("最新声明里没有候选 A06 首条 WS 连接的 frame_labels 规则")
        trace = FIXTURE / "bundle/derived/A06/websocket_trace/A06_relay_conn001.client_to_upstream.bin.observation.ndjson"
        facts = [
            dict(record["data"]) for record in map(json.loads, trace.read_text(encoding="utf-8").splitlines())
            if record["record_type"] == "websocket_frame"
        ]
        by_index = {fact["frame_index"]: fact for fact in facts}
        for key, labels in declared.items():
            for target in cra.resolve_frame_label_selectors({key: labels}, facts, "fixture"):
                with self.subTest(key=key, target=target):
                    self.assertEqual(by_index[target].get("opcode"), "TEXT")
                    self.assertEqual(by_index[target].get("event_type"), "response.create")


class EngineSelfConsistencyTest(PreviewCase):
    def test_unreachable_list_matches_the_real_seal_gate_first_failure(self) -> None:
        labels = mini_declaration(ORIGINAL_LABELS)
        profile = mini_profile(body_value=1)
        declaration_path = self.write("labels.json", labels)
        profile_path = self.write("profile.json", profile)
        result = preview.run_preview(
            candidate_evidence=self.prior, labels=declaration_path, profile_path=profile_path,
            candidate_roots_dir=self.roots, candidate_only=True,
        )
        step = next(step for step in result["sides"][0]["steps"] if step["name"] == "selector-reachability")
        self.assertTrue(step["details"]["consistent_with_real_gate"])
        self.assertIn("SPEC-WS-005", step["details"]["real_gate"])
        self.assertIn(step["details"]["unreachable"][0]["check"], step["details"]["real_gate"])

    def test_without_roots_dir_the_prior_bundle_copy_is_used_and_new_globs_fail_closed(self) -> None:
        # 新声明选中了上一版证据包里没有的文件：编目阶段如实报错，而不是悄悄少测。
        declaration = mini_declaration(CORRECTED_LABELS)
        entry = declaration["entries"][0]
        extra = copy.deepcopy(next(rule for rule in entry["rules"] if rule["glob"] == A06_GLOB))
        extra["glob"] = "scenarios/A06/relay/conn002.client_to_upstream.bin"
        extra.pop("frame_labels")
        entry["rules"].append(extra)
        labels = self.write("labels.json", declaration)
        profile = self.write("profile.json", mini_profile(body_value=1))
        result = preview.run_preview(
            candidate_evidence=self.prior, labels=labels, profile_path=profile, candidate_only=True,
        )
        self.assertEqual([item["kind"] for item in result["defects"]], ["catalog-error"])
        self.assertIn("conn002", result["defects"][0]["message"])


class CommandLineTest(PreviewCase):
    def run_cli(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = preview.main(["preview", *extra])
        return code, out.getvalue(), err.getvalue()

    def arguments(self, frame_labels: dict, body_value: int, *more: str) -> list[str]:
        labels = self.write("labels.json", mini_declaration(frame_labels))
        profile = self.write("profile.json", mini_profile(body_value=body_value))
        return [
            "--candidate-evidence", str(self.prior), "--labels", str(labels), "--profile", str(profile),
            "--candidate-roots-dir", str(self.roots), *more,
        ]

    def test_exit_codes_and_json_output(self) -> None:
        json_out = self.work / "result.json"
        code, out, _ = self.run_cli(*self.arguments(ORIGINAL_LABELS, 0, "--candidate-only", "--json-out", str(json_out)))
        self.assertEqual(code, 1)
        self.assertIn("原写 1、实际 7", out)
        self.assertIn("判据与实际行为不一致", out)
        document = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], preview.SCHEMA)
        self.assertEqual(document["verdict"], "defects_found")

        code, out, _ = self.run_cli(*self.arguments(CORRECTED_LABELS, 1, "--candidate-only"))
        self.assertEqual(code, 0, out)
        # 没给官方证据也没声明只跑候选侧：不能当通过，退出码 3。
        code, out, _ = self.run_cli(*self.arguments(CORRECTED_LABELS, 1))
        self.assertEqual(code, 3, out)
        self.assertIn("not_run", out)

    def test_bad_inputs_exit_two(self) -> None:
        code, _, err = self.run_cli(
            "--candidate-evidence", str(self.work / "missing"), "--labels", str(self.work / "x.json"),
            "--profile", str(self.work / "y.json"),
        )
        self.assertEqual(code, 2)
        self.assertTrue(err)


class VersionRewriteTest(unittest.TestCase):
    def test_rewrites_every_string_in_nested_structures(self) -> None:
        value = {"a": "version: 2.0.0", "b": ["codex/2.0.0", {"c": "x2.0.0y"}], "d": 3}
        self.assertEqual(
            preview.rewrite_versions(value, {"2.0.0": "1.0.0"}),
            {"a": "version: 1.0.0", "b": ["codex/1.0.0", {"c": "x1.0.0y"}], "d": 3},
        )

    def test_rewrite_argument_must_be_new_equals_old(self) -> None:
        with self.assertRaises(preview.PreviewInputError):
            preview._parse_rewrites(["2.0.0"])
        self.assertEqual(preview._parse_rewrites(["2.0.0=1.0.0"]), {"2.0.0": "1.0.0"})


if __name__ == "__main__":
    unittest.main()
