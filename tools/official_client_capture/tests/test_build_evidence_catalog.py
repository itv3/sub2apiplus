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
        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        for entry in self.declaration["entries"]:
            job = by_id[entry["job_id"]]
            allowed = set(job["scenario_ids"])
            for rule in entry["rules"]:
                extra = set(rule["scenario_ids"]) - allowed
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


if __name__ == "__main__":
    unittest.main()
