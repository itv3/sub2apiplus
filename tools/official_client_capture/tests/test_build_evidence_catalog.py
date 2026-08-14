"""ACC-07 编目器必须确定性、不读文件内容、覆盖不全即失败关闭。

capture manifest 此前一直由执行者手写，是 §10.8.3 记录的标签语义错位与覆盖不全的
根因。编目器把它变成从冻结声明的确定性推导，因此负例必须覆盖：声明缺 job、glob 落空、
标签非法、派生契约不闭合、目标冲突。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_evidence_catalog as catalog  # noqa: E402
import derive_official_observations as derive  # noqa: E402


DECLARATION = {
    "schema_version": catalog.LABELS_SCHEMA,
    "codex_version": "0.145.0",
    "entries": [
        {
            "job_id": "official-core",
            "side": "official",
            "rules": [
                {
                    "glob": "direct/codex-http/*/traffic.pcap",
                    "scenario_ids": ["A01"],
                    "kind": "pcap",
                    "parser": "pcap_client_hello",
                    "labels": {"transport": "http", "ca_mode": "system"},
                    "rationale": "direct 面未配置自定义 CA。",
                },
                {
                    "glob": "mitm/codex-http/*/models-http.jsonl",
                    "scenario_ids": ["A09"],
                    "kind": "wire_dump",
                    "parser": "opaque_bound_source",
                    "derive": {"parser": "mitm_http_jsonl", "kind": "process_trace"},
                    "labels": {"transport": "http", "endpoint_class": "auxiliary"},
                    "rationale": "models 属辅助端点面。",
                },
            ],
        }
    ],
}


class CatalogFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="acc07-"))
        self.addCleanup(self._cleanup)
        self.root = self.workdir / "run"
        for relative in (
            "direct/codex-http/s1/traffic.pcap",
            "direct/codex-http/s2/traffic.pcap",
            "mitm/codex-http/s1/models-http.jsonl",
            "mitm/codex-http/s1/mitmdump.log",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        self.job_roots = {"official-core": [("oauth-run", self.root)]}
        self.declaration = json.loads(json.dumps(DECLARATION))

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    def _build(self) -> dict:
        return catalog.build_catalog(
            self.declaration, self.job_roots, side="official"
        )


class CatalogBuildTest(CatalogFixture):
    def test_catalog_covers_declared_globs_only(self) -> None:
        result = self._build()
        targets = [e["target"] for e in result["bundle_plan"]["entries"]]
        self.assertEqual(
            targets,
            [
                "oauth-run/direct/codex-http/s1/traffic.pcap",
                "oauth-run/direct/codex-http/s2/traffic.pcap",
                "oauth-run/mitm/codex-http/s1/models-http.jsonl",
            ],
        )
        # mitmdump.log 未被任何 glob 声明，不得进入 bundle。
        self.assertNotIn("oauth-run/mitm/codex-http/s1/mitmdump.log", targets)

    def test_labels_come_from_declaration(self) -> None:
        result = self._build()
        by_path = {a["path"]: a for a in result["manifest_draft"]["artifacts"]}
        pcap = by_path["oauth-run/direct/codex-http/s1/traffic.pcap"]
        self.assertEqual(pcap["labels"], {"transport": "http", "ca_mode": "system"})
        self.assertEqual(pcap["scenario_ids"], ["A01"])
        self.assertEqual(pcap["parser"], "pcap_client_hello")

    def test_derive_entries_and_artifacts_paired(self) -> None:
        result = self._build()
        derives = result["derivation_plan"]["entries"]
        self.assertEqual(len(derives), 1)
        entry = derives[0]
        self.assertEqual(entry["source"], "oauth-run/mitm/codex-http/s1/models-http.jsonl")
        self.assertEqual(entry["parser"], "mitm_http_jsonl")
        self.assertEqual(entry["kind"], "process_trace")
        self.assertTrue(entry["target"].startswith(catalog.DERIVED_PREFIX))
        paths = {a["path"] for a in result["manifest_draft"]["artifacts"]}
        self.assertIn(entry["target"], paths)

    def test_derived_plan_is_accepted_by_deriver(self) -> None:
        """编目器产出的派生清单必须被 ACC-02b 原样接受。"""

        result = self._build()
        plan_path = self.workdir / "derive-plan.json"
        plan_path.write_text(
            json.dumps(result["derivation_plan"], ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = derive.load_derivation_plan(plan_path)
        self.assertEqual(len(loaded), 1)

    def test_catalog_is_deterministic(self) -> None:
        self.assertEqual(
            json.dumps(self._build(), sort_keys=True),
            json.dumps(self._build(), sort_keys=True),
        )


class CatalogNegativeTest(CatalogFixture):
    def test_job_without_declaration_fails_closed(self) -> None:
        self.job_roots["official-extra"] = [("extra-run", self.root)]
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "缺少证据标签声明"):
            self._build()

    def test_glob_matching_nothing_fails_closed(self) -> None:
        self.declaration["entries"][0]["rules"][0]["glob"] = "direct/absent/*.pcap"
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "未命中任何证据"):
            self._build()

    def test_missing_rationale_rejected(self) -> None:
        del self.declaration["entries"][0]["rules"][0]["rationale"]
        with self.assertRaises(catalog.EvidenceCatalogError):
            catalog.load_label_declaration(self._write_declaration())

    def test_non_string_label_rejected(self) -> None:
        self.declaration["entries"][0]["rules"][0]["labels"] = {"transport": 1}
        with self.assertRaises(catalog.EvidenceCatalogError):
            catalog.load_label_declaration(self._write_declaration())

    def test_derive_requires_opaque_source_parser(self) -> None:
        rule = self.declaration["entries"][0]["rules"][1]
        rule["parser"] = "observation_jsonl"
        with self.assertRaisesRegex(
            catalog.EvidenceCatalogError, "opaque_bound_source"
        ):
            catalog.load_label_declaration(self._write_declaration())

    def test_internal_derive_kind_rejected(self) -> None:
        self.declaration["entries"][0]["rules"][1]["derive"]["kind"] = "surface_identity"
        with self.assertRaises(catalog.EvidenceCatalogError):
            catalog.load_label_declaration(self._write_declaration())

    def test_glob_path_escape_rejected(self) -> None:
        self.declaration["entries"][0]["rules"][0]["glob"] = "../secret/*.pcap"
        with self.assertRaises(catalog.EvidenceCatalogError):
            catalog.load_label_declaration(self._write_declaration())

    def test_duplicate_job_declaration_rejected(self) -> None:
        self.declaration["entries"].append(
            json.loads(json.dumps(self.declaration["entries"][0]))
        )
        with self.assertRaises(catalog.EvidenceCatalogError):
            catalog.load_label_declaration(self._write_declaration())

    def _write_declaration(self) -> Path:
        path = self.workdir / "declaration.json"
        path.write_text(
            json.dumps(self.declaration, ensure_ascii=False), encoding="utf-8"
        )
        return path


class RepositoryDeclarationTest(unittest.TestCase):
    """仓库冻结声明必须自洽，并与场景清单的 job 对齐。"""

    @classmethod
    def setUpClass(cls) -> None:
        base = Path(__file__).resolve().parents[1]
        cls.declaration = catalog.load_label_declaration(
            base / "codex_upgrade_evidence_labels_0_145_0.json"
        )
        cls.scenarios = json.loads(
            (base / "codex_upgrade_scenarios_0_145_0.json").read_text(encoding="utf-8")
        )

    def test_declared_jobs_exist_in_scenarios(self) -> None:
        known = {job["id"] for job in self.scenarios["capture_jobs"]}
        declared = {entry["job_id"] for entry in self.declaration["entries"]}
        self.assertTrue(
            declared <= known, f"声明引用了不存在的 job：{sorted(declared - known)}"
        )

    def test_declared_scenarios_belong_to_job(self) -> None:
        """wire 证据的场景归属必须落在 job 的冻结场景内。

        唯一例外是 ``stdout_log``：候选侧的 ``candidate-go-test.jsonl`` 是源码快照
        的**全量** ``go test -json`` 输出，本身不是任何单个场景的 wire 证据，而是
        internal record type 的跨场景来源——它由 candidate_test_trace 按冻结事实
        映射投影到各场景，覆盖面由映射决定而非产出它的 job 决定。这类来源仍受
        两道约束：场景必须是冻结场景清单里的真实场景，且投影结果必须逐条绑定
        同场景的原始 relay 字节（由 load_observations 的跨场景来源校验强制）。
        """

        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        known_scenarios = {
            scenario["scenario_id"] for scenario in self.scenarios["evidence_scenarios"]
        }
        for entry in self.declaration["entries"]:
            job = by_id[entry["job_id"]]
            allowed = set(job["scenario_ids"])
            for rule in entry["rules"]:
                declared = set(rule["scenario_ids"])
                if rule["kind"] == "stdout_log":
                    unknown = declared - known_scenarios
                    self.assertFalse(
                        unknown,
                        f"{entry['job_id']} 的 {rule['glob']} 引用了冻结清单外的场景："
                        f"{sorted(unknown)}",
                    )
                    continue
                extra = declared - allowed
                self.assertFalse(
                    extra,
                    f"{entry['job_id']} 的 {rule['glob']} 引用了 job 未声明的场景：{sorted(extra)}",
                )

    def test_label_keys_are_known_to_profile(self) -> None:
        """声明用到的 label 键必须都是断言画像 selector 认识的，或纯描述性。"""

        profile = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "candidate_rule_expectations_0_145_0.json"
            ).read_text(encoding="utf-8")
        )
        used = {
            where["path"].split(".", 1)[1]
            for rule in profile["rules"]
            for check in rule["checks"]
            for where in (check["select"].get("where") or [])
            if where["path"].startswith("labels.")
        }
        descriptive = {"surface"}
        for entry in self.declaration["entries"]:
            for rule in entry["rules"]:
                unknown = set(rule["labels"]) - used - descriptive
                self.assertFalse(
                    unknown,
                    f"{entry['job_id']} 的 {rule['glob']} 使用了画像未知的标签键："
                    f"{sorted(unknown)}",
                )

    def test_ca_mode_only_on_reconstructed_or_tls_faces(self) -> None:
        """``ca_mode`` 只允许出现在 pcap 与代理重构面。

        判据凡要求**客户端发出的原始字节**者，一律写 ``labels.ca_mode absent``
        来排除 mitm／ingress 这类由代理重构的观测（protocol 报 HTTP/2.0、host 落在
        ``:authority``）。因此 relay 与 probe 面绝不能标 ``ca_mode``——k56 的候选侧
        曾给所有 relay 规则统一标 ``ca_mode: custom``，结果 35 条 selector 一条都
        选不中，且不报错（§10.9.3）。这条测试把该语义钉死在两类面上。
        """

        # 重构面由证据的产出路径决定，不由标签自述——mitm/ 与 ingress/ 下的记录
        # 都经过代理解密重放，其余（relay/、h1-wire.json、request.bin）是客户端字节。
        for entry in self.declaration["entries"]:
            for rule in entry["rules"]:
                if "ca_mode" not in rule["labels"]:
                    continue
                glob = rule["glob"]
                reconstructed = glob.startswith(("mitm/", "ingress/"))
                self.assertTrue(
                    rule["kind"] == "pcap" or reconstructed,
                    f"{entry['job_id']} 的 {glob} 标了 ca_mode，但既不是 pcap "
                    "也不产自 mitm／ingress 重构面——"
                    "原始字节面标 ca_mode 会被判据的 absent 约束整体排除",
                )

    def test_candidate_relay_and_probe_wire_faces_carry_no_ca_mode(self) -> None:
        """反向锁定：候选侧 relay／probe 的**非 pcap** 面必须不带 ca_mode。

        同一 relay 跳会同时产出两类证据：``egress.pcap`` 记录 TLS 握手（ca_mode 是
        它的真实实验条件），``conn*.client_to_upstream.bin`` 记录解密后的客户端原始
        字节（判据以 ``ca_mode absent`` 选它）。只有后者不得标。
        """

        for entry in self.declaration["entries"]:
            if entry["side"] != "candidate":
                continue
            for rule in entry["rules"]:
                if rule["kind"] == "pcap":
                    continue
                if rule["labels"].get("surface") not in {"relay", "probe"}:
                    continue
                self.assertNotIn(
                    "ca_mode",
                    rule["labels"],
                    f"{entry['job_id']} 的 {rule['glob']} 是客户端原始字节面，"
                    "不得标 ca_mode",
                )

    def test_frame_labels_only_on_websocket_trace_derivation(self) -> None:
        """帧级标签只能挂在能产出 websocket_frame 观测的规则上。"""

        for entry in self.declaration["entries"]:
            for rule in entry["rules"]:
                if "frame_labels" not in rule:
                    continue
                derive_kind = (rule.get("derive") or {}).get("kind")
                self.assertTrue(
                    derive_kind == "websocket_trace"
                    or rule["parser"] == "h1_request_stream",
                    f"{entry['job_id']} 的 {rule['glob']} 声明了 frame_labels，"
                    f"但既不派生 websocket_trace 也不是 h1_request_stream",
                )


if __name__ == "__main__":
    unittest.main()
