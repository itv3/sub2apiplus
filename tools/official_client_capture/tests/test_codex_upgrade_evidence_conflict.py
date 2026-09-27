"""修好接着跑第 24 项：续跑重跑"来源已完成"的作业不再破坏证据。

派发前把被其它 attempt 登记占据的固定证据根改名为同级 ``.superseded-<新 attempt>``（先写取代收据），旧 attempt
重放收口时按收据映射；已经被破坏的 attempt 由 evidence-conflict-quarantine 登记，受损的只读留档、其余作废，
两者都永不 seal、永不复用，续跑全部重跑。
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import codex_upgrade


def _reference(attempt_id: str) -> dict[str, object]:
    return {"phase": "candidate", "candidate_id": "cand", "attempt_id": attempt_id, "attempt_sha256": "1" * 64}


class _CampaignRoot(unittest.TestCase):
    """临时 Campaign 与 runs 根（逻辑 runs 与宿主 runs 为同一目录，宿主路由常量指向它）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.campaign_dir = self.root / "campaign"
        self.campaign_dir.mkdir(mode=0o700)
        (self.campaign_dir / "campaign.json").write_text(json.dumps({"campaign_id": "c1"}) + "\n", encoding="utf-8")
        self.runs = self.root / "runs"
        self.runs.mkdir(mode=0o700)
        patcher = mock.patch.multiple(
            codex_upgrade,
            FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS=(PurePosixPath(str(self.runs)),),
            FAILED_JOB_EVIDENCE_HOST_RUN_ROOT=self.runs,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.attempts = self.campaign_dir / "candidates" / "cand" / "attempts"

    def _run_root(self, name: str, content: str = "{}") -> Path:
        path = self.runs / name
        path.mkdir(mode=0o700)
        (path / "capture.json").write_text(content, encoding="utf-8")
        return path

    def _relocations(self, attempt_id: str) -> dict[str, str]:
        return codex_upgrade._evidence_root_relocations(
            self.campaign_dir, phase="candidate", candidate_id="cand", attempt_id=attempt_id
        )


class SupersessionTests(_CampaignRoot):
    def _supersede(
        self, attempt_id: str, jobs: list[SimpleNamespace], registrants: dict[str, list[dict[str, object]]]
    ) -> dict[str, object] | None:
        attempt_root = self.attempts / attempt_id
        attempt_root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(codex_upgrade, "_evidence_root_registrants", return_value=registrants):
            return codex_upgrade._supersede_occupied_evidence_roots(
                self.campaign_dir,
                {"campaign_id": "c1"},
                phase="candidate",
                candidate_id="cand",
                attempt_root=attempt_root,
                jobs=jobs,
            )

    def test_occupied_roots_are_renamed_after_the_receipt_and_mapped_for_their_registrants(self) -> None:
        direct = self._run_root("c1-cand-candidate-direct-core")
        mitm_http = self._run_root("c1-cand-candidate-mitm-core-codex-http-s1-a1-run")
        mitm_ws = self._run_root("c1-cand-candidate-mitm-core-codex-ws-s1-a1-run")
        orphan = self._run_root("c1-cand-candidate-h1")
        identity = (direct.lstat().st_dev, direct.lstat().st_ino)
        mitm_pattern = str(self.runs / "c1-cand-candidate-mitm-core-*-run")
        jobs = [
            SimpleNamespace(job_id="candidate-core-direct", evidence_roots=(str(direct),)),
            SimpleNamespace(job_id="candidate-core-mitm", evidence_roots=(mitm_pattern,)),
            SimpleNamespace(job_id="candidate-h1-wire", evidence_roots=(str(orphan),)),
        ]
        registrants = {
            str(direct): [_reference("a0")],
            # a1r 以复用承接登记同一根：它同样映射到归档目录。
            str(mitm_http): [_reference("a0"), _reference("a1r")],
            str(mitm_ws): [_reference("a0")],
        }
        receipt = self._supersede("a2", jobs, registrants)
        assert receipt is not None
        archived = {path: path.with_name(path.name + ".superseded-a2") for path in (direct, mitm_http, mitm_ws)}
        for original, target in archived.items():
            self.assertFalse(original.exists())
            self.assertTrue(target.is_dir())
        self.assertEqual((archived[direct].lstat().st_dev, archived[direct].lstat().st_ino), identity)
        # 孤儿目录（没有 attempt 登记）不动，保持原有拒绝覆盖／增量语义。
        self.assertTrue(orphan.is_dir())
        # 取代目录不再匹配 mitm 证据根模式：新 attempt 与 mitm 坐标 checkpoint 都看不到旧坐标。
        self.assertEqual(glob.glob(mitm_pattern), [])
        self.assertEqual(
            sorted((row["job_id"], Path(str(row["logical_root"])).name) for row in receipt["roots"]),
            sorted(
                [
                    ("candidate-core-direct", direct.name),
                    ("candidate-core-mitm", mitm_http.name),
                    ("candidate-core-mitm", mitm_ws.name),
                ]
            ),
        )
        stored = self.campaign_dir / "control" / "evidence-roots" / "supersession-a2.json"
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8")), receipt)
        self.assertEqual(codex_upgrade._evidence_root_supersessions(self.campaign_dir), [receipt])
        self.assertEqual(
            self._relocations("a0"),
            {str(path): str(target) for path, target in archived.items()},
        )
        self.assertEqual(self._relocations("a1r"), {str(mitm_http): str(archived[mitm_http])})
        self.assertEqual(self._relocations("a2"), {})

    def test_nothing_occupied_writes_nothing(self) -> None:
        orphan = self._run_root("c1-cand-candidate-h1")
        jobs = [SimpleNamespace(job_id="candidate-h1-wire", evidence_roots=(str(orphan),))]
        self.assertIsNone(self._supersede("a2", jobs, {}))
        self.assertIsNone(self._supersede("a3", jobs, {"/elsewhere": [_reference("a0")]}))
        self.assertFalse((self.campaign_dir / "control" / "evidence-roots").exists())
        self.assertTrue(orphan.is_dir())

    def test_interrupted_rename_is_not_mapped_until_a_later_rerun_takes_it_over(self) -> None:
        direct = self._run_root("c1-cand-candidate-direct-core")
        jobs = [SimpleNamespace(job_id="candidate-core-direct", evidence_roots=(str(direct),))]
        # 收据已落盘、rename 前中断：原路径仍是原目录，旧 attempt 照原路径重放。
        with mock.patch.object(Path, "rename", side_effect=OSError("合成：rename 前中断")):
            with self.assertRaises(OSError):
                self._supersede("a1", jobs, {str(direct): [_reference("a0")]})
        self.assertTrue(direct.is_dir())
        self.assertEqual(self._relocations("a0"), {})
        # 下一次续跑接管同一目录：前一张收据的行判为被接管，按接管那张映射。
        self._supersede("a2", jobs, {str(direct): [_reference("a0")]})
        self.assertEqual(self._relocations("a0"), {str(direct): str(direct.with_name(direct.name + ".superseded-a2"))})

    def test_inode_drift_or_tampered_receipt_fails_closed(self) -> None:
        direct = self._run_root("c1-cand-candidate-direct-core")
        jobs = [SimpleNamespace(job_id="candidate-core-direct", evidence_roots=(str(direct),))]
        self._supersede("a1", jobs, {str(direct): [_reference("a0")]})
        archived = direct.with_name(direct.name + ".superseded-a1")
        # 归档目录被替换：原目录被挪走、归档路径换成另一个目录——原目录既不在归档路径也不在原路径。旧目录仍占着
        # 原 inode，新目录的 inode 必然不同（直接删除后重建时 ext4 会立即复用同一 inode 号，那种情形 inode 判据识别
        # 不出，由随后的收口边界重放按条目 inode／mtime／size 失败关闭）。
        archived.rename(self.runs / "moved-away")
        archived.mkdir(mode=0o700)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "取代状态不一致"):
            self._relocations("a0")
        stored = self.campaign_dir / "control" / "evidence-roots" / "supersession-a1.json"
        stored.chmod(0o600)
        tampered = json.loads(stored.read_text(encoding="utf-8"))
        tampered["roots"][0]["registered_by"] = [_reference("a9")]
        stored.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "自摘要"):
            codex_upgrade._evidence_root_supersessions(self.campaign_dir)
        stray = stored.with_name("notes.txt")
        stored.unlink()
        stray.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "非收据条目"):
            codex_upgrade._evidence_root_supersessions(self.campaign_dir)

    def test_registrants_read_results_without_replay_and_skip_self_and_mapped_roots(self) -> None:
        payloads = {
            "a0": {"results": [{"id": "j", "evidence_roots": ["/r/x", "/r/y"]}]},
            "a1": {"results": [{"id": "j", "disposition": "reused", "evidence_roots": ["/r/x"]}]},
            "a2": {"results": [{"id": "j", "evidence_roots": ["/r/x"]}]},
        }
        for attempt_id in payloads:
            (self.attempts / attempt_id).mkdir(parents=True)
            (self.attempts / attempt_id / "attempt.json").write_text("{}", encoding="utf-8")
        load = mock.Mock(side_effect=lambda _c, _p, _cid, aid, **_kw: (self.attempts / aid, payloads[aid]))
        with mock.patch.multiple(
            codex_upgrade,
            _ordered_capture_attempts=mock.Mock(return_value=[(self.attempts / a, {}) for a in ("a2", "a1", "a0")]),
            _load_capture_attempt=load,
            _evidence_root_relocations=mock.Mock(
                side_effect=lambda _c, *, phase, candidate_id, attempt_id: (
                    {"/r/x": "/r/x.superseded-a1"} if attempt_id == "a0" else {}
                )
            ),
        ):
            owners = codex_upgrade._evidence_root_registrants(
                self.campaign_dir, {}, phase="candidate", candidate_id="cand", exclude_attempt_id="a2"
            )
        self.assertEqual({root: [item["attempt_id"] for item in items] for root, items in owners.items()},
                         {"/r/x": ["a1"], "/r/y": ["a0"]})
        self.assertTrue(all(call.kwargs.get("_replay_evidence_permissions") is False for call in load.call_args_list))


class ConflictQuarantineTests(_CampaignRoot):
    SHARED = "/root/oauth-capture/runs/c1-cand-candidate-mitm-core-codex-http-s1-a1-run"

    def _payloads(self) -> dict[str, dict[str, object]]:
        return {
            "a0": {
                "status": "failed",
                "results": [
                    {"id": "candidate-core-mitm", "status": "complete", "disposition": "executed",
                     "evidence_roots": [self.SHARED]},
                    {"id": "candidate-trace-test", "status": "complete", "disposition": "executed",
                     "evidence_roots": ["/root/oauth-capture/runs/c1-cand-candidate-trace-test"]},
                ],
            },
            "a1": {
                "status": "awaiting_receipts",
                "results": [
                    {"id": "candidate-core-mitm", "status": "complete", "disposition": "executed",
                     "evidence_roots": [self.SHARED]},
                    # 复用承接与来源共享证据根是合法的，不计冲突。
                    {"id": "candidate-trace-test", "status": "complete", "disposition": "reused",
                     "evidence_roots": ["/root/oauth-capture/runs/c1-cand-candidate-trace-test"]},
                ],
            },
        }

    def _patched(self, payloads: dict[str, dict[str, object]], *, replay_failures: set[str],
                 relocations: dict[str, dict[str, str]] | None = None) -> contextlib.ExitStack:
        for attempt_id in payloads:
            root = self.attempts / attempt_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "attempt.json").write_text(json.dumps({"attempt_id": attempt_id}), encoding="utf-8")

        def replay(attempt_root: Path, _payload: object, *, root_relocations: object = None) -> dict[str, object]:
            del root_relocations
            if attempt_root.name in replay_failures:
                raise codex_upgrade.ConfigurationError("证据权限收口收据未通过：权限收口后的证据元数据边界漂移。")
            return {}

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.multiple(
            codex_upgrade,
            _ordered_capture_attempts=mock.Mock(
                return_value=[(self.attempts / attempt_id, {}) for attempt_id in sorted(payloads, reverse=True)]
            ),
            _load_capture_attempt=mock.Mock(
                side_effect=lambda _c, _p, _cid, aid, **_kw: (self.attempts / aid, payloads[aid])
            ),
            _replay_attempt_evidence_permissions=mock.Mock(side_effect=replay),
            _evidence_root_relocations=mock.Mock(
                side_effect=lambda _c, *, phase, candidate_id, attempt_id: dict((relocations or {}).get(attempt_id, {}))
            ),
            _require_formal_campaign=mock.Mock(return_value={"campaign_id": "c1"}),
            _require_tool_evolution_registered=mock.Mock(return_value=None),
            _tool_evolution_quiescence_problems=mock.Mock(return_value=[]),
        ))
        return stack

    def _arguments(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = dict(
            campaign_dir=self.campaign_dir, candidate_id="cand", reason="续跑重跑时证据根被覆写",
            approve_sha256=None, approved_by=None,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_conflicting_attempts_are_quarantined_by_boundary_replay(self) -> None:
        payloads = self._payloads()
        logs = self.attempts / "a1" / "logs"
        logs.mkdir(parents=True)
        (logs / "candidate-core-mitm-1.log").write_text(
            "incremental_noop=true executed=0 reused=6 pcap_scanned_bytes=0\nrun_ids=…\n", encoding="utf-8"
        )
        with self._patched(payloads, replay_failures={"a0"}):
            conflicts = codex_upgrade._evidence_root_conflicts(
                self.campaign_dir, {}, phase="candidate", candidate_id="cand"
            )
            self.assertEqual(conflicts, [{
                "evidence_root": self.SHARED,
                "attempts": [
                    {"attempt_id": "a0", "job_ids": ["candidate-core-mitm"]},
                    {"attempt_id": "a1", "job_ids": ["candidate-core-mitm"]},
                ],
            }])
            preview = codex_upgrade._evidence_conflict_quarantine_command(self._arguments())
            self.assertEqual(preview["status"], "approval_required")
            self.assertEqual(preview["live_request_count"], 0)
            by_id = {item["attempt_id"]: item for item in preview["attempts"]}
            self.assertEqual({key: item["disposition"] for key, item in by_id.items()},
                             {"a0": "compromised", "a1": "invalidated"})
            self.assertEqual(by_id["a0"]["boundary_replay"]["status"], "failed")
            self.assertEqual(by_id["a1"]["incremental_noop_job_ids"], ["candidate-core-mitm"])
            self.assertEqual(by_id["a0"]["incremental_noop_job_ids"], [])
            self.assertFalse((self.campaign_dir / "control" / "evidence-conflict").exists())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "批准摘要与重算"):
                codex_upgrade._evidence_conflict_quarantine_command(
                    self._arguments(approve_sha256="0" * 64, approved_by="老板")
                )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "--approved-by"):
                codex_upgrade._evidence_conflict_quarantine_command(
                    self._arguments(approve_sha256=preview["review_sha256"])
                )
            done = codex_upgrade._evidence_conflict_quarantine_command(
                self._arguments(approve_sha256=preview["review_sha256"], approved_by="老板")
            )
            self.assertEqual((done["status"], done["reused"], done["quarantine_index"]), ("quarantined", False, 1))
            again = codex_upgrade._evidence_conflict_quarantine_command(
                self._arguments(approve_sha256=preview["review_sha256"], approved_by="老板")
            )
            self.assertEqual((again["status"], again["reused"]), ("quarantined", True))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有未隔离的证据根冲突"):
                codex_upgrade._evidence_conflict_quarantine_command(self._arguments())
        both = {("candidate", "cand", "a0"), ("candidate", "cand", "a1")}
        self.assertEqual(codex_upgrade._conflict_quarantined_attempts(self.campaign_dir), both)
        self.assertEqual(codex_upgrade._conflict_compromised_attempts(self.campaign_dir), {("candidate", "cand", "a0")})
        self.assertTrue(both <= codex_upgrade._rerun_all_invalidated_attempts(self.campaign_dir))
        found = codex_upgrade._attempt_conflict_quarantine_receipt(self.campaign_dir, "candidate", "cand", "a1")
        self.assertEqual(found[1]["disposition"], "invalidated")
        self.assertIsNone(codex_upgrade._attempt_conflict_quarantine_receipt(self.campaign_dir, "candidate", "cand", "a9"))

    def test_mapped_away_roots_no_conflict_and_sealed_stage_are_refused(self) -> None:
        payloads = self._payloads()
        # 正常续跑：派发前来源的根已取代归档，映射后两者不再相同。
        with self._patched(payloads, replay_failures=set(),
                           relocations={"a0": {self.SHARED: self.SHARED + ".superseded-a1"}}):
            self.assertEqual(
                codex_upgrade._evidence_root_conflicts(self.campaign_dir, {}, phase="candidate", candidate_id="cand"),
                [],
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有未隔离的证据根冲突"):
                codex_upgrade._evidence_conflict_quarantine_command(self._arguments())
        stage = codex_upgrade._stage_path(self.campaign_dir, "capture-candidate", "cand")[1]
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_text("{}", encoding="utf-8")
        with self._patched(payloads, replay_failures={"a0"}):
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已封存"):
                codex_upgrade._evidence_conflict_quarantine_command(self._arguments())

    def test_quarantine_chain_is_verified(self) -> None:
        payloads = self._payloads()
        with self._patched(payloads, replay_failures={"a0"}):
            preview = codex_upgrade._evidence_conflict_quarantine_command(self._arguments())
            codex_upgrade._evidence_conflict_quarantine_command(
                self._arguments(approve_sha256=preview["review_sha256"], approved_by="老板")
            )
        stored = self.campaign_dir / "control" / "evidence-conflict" / "quarantine-01.json"
        stored.chmod(0o600)
        receipt = json.loads(stored.read_text(encoding="utf-8"))
        receipt["attempts"][0]["disposition"] = "invalidated"
        stored.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "自摘要"):
            codex_upgrade._conflict_quarantined_attempts(self.campaign_dir)
        stored.rename(stored.with_name("quarantine-02.json"))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "序号不连续"):
            codex_upgrade._conflict_quarantined_attempts(self.campaign_dir)


class ConflictQuarantinedSourceRecoveryScopeTests(unittest.TestCase):
    """被证据根冲突隔离的 attempt 作为续跑来源：已完成作业全部作废、全部计划作业重跑，环境边界照常计算。"""

    PLANNED = ("a", "b", "c")
    QUARANTINE = ({"index": 1, "quarantine_sha256": "8" * 64}, {"disposition": "invalidated"})

    def _scope(self, *, status: str, quarantine: tuple[dict, dict] | None, isolation: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            (campaign_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            attempt_root = campaign_dir / "a1"
            attempt_root.mkdir()
            attempt = {
                "status": status,
                "phase": "candidate",
                "candidate_id": "cand",
                "campaign_id": "c",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(campaign_dir / "campaign.json"),
                "attempt_id": "a1",
                "run_nonce": "1" * 64,
                "attempt_digest": "2" * 64,
                "results": [
                    {"id": job_id, "execution_sha256": job_id * 64, "status": "complete", "required": True}
                    for job_id in ("a", "b")
                ],
                "job_checkpoint": {"path": "checkpoints", "record_count": 2, "last_sequence": 2, "last_sha256": "3" * 64},
            }
            store = mock.Mock()
            store.records.return_value = [1, 2]
            with mock.patch.multiple(
                codex_upgrade,
                _attempt_isolation_receipt=mock.Mock(return_value=isolation),
                _attempt_conflict_quarantine_receipt=mock.Mock(return_value=quarantine),
                _load_capture_reservation=mock.Mock(return_value={
                    "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]
                }),
                _attempt_evolution_impact=mock.Mock(
                    return_value={"index": 0, "affected_job_ids": [], "changed_paths": [], "evolution_indexes": []}
                ),
                _resolve_attempt_binding=mock.Mock(return_value=attempt_root / "checkpoints"),
                _validate_checkpoint_records=mock.Mock(),
                # 冲突隔离源的前后环境收据完好：环境边界照常按源 attempt 计算。
                _phase_evaluation_environment_boundary=mock.Mock(return_value="e" * 64),
            ), mock.patch.object(codex_upgrade.incremental_recovery, "CheckpointStore", mock.Mock(return_value=store)):
                return codex_upgrade._phase_evaluation_recovery_scope(
                    campaign_dir, {"campaign_id": "c"}, phase="candidate", candidate_id="cand",
                    attempt_root=attempt_root, attempt=attempt, allow_evolution_invalidated=True,
                )

    def _validate(self, scope: dict) -> tuple[set[str], set[str]]:
        with mock.patch.object(codex_upgrade, "_load_capture_reservation", return_value={
            "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]
        }), mock.patch.object(codex_upgrade, "_job_execution_sha256", side_effect=lambda job: job.job_id * 64):
            return codex_upgrade._validate_recovery_scope_plan(
                Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                scope=scope, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in self.PLANNED],
            )

    def test_quarantined_source_reruns_every_planned_job(self) -> None:
        for status in ("failed", "awaiting_receipts"):
            with self.subTest(status=status):
                scope = self._scope(status=status, quarantine=self.QUARANTINE)
                self.assertEqual(scope["execute_job_ids"], ["a", "b", "c"])
                self.assertEqual(scope["completed_job_ids"], [])
                self.assertEqual(
                    scope["evidence_conflict_quarantine"],
                    {"quarantine_index": 1, "quarantine_sha256": "8" * 64, "invalidated_job_ids": ["a", "b"]},
                )
                self.assertEqual(scope["environment_boundary_sha256"], "e" * 64)
                self.assertNotIn("continuity_drift", scope)
                self.assertEqual(self._validate(scope), (set(), {"a", "b", "c"}))

    def test_unquarantined_awaiting_source_is_refused(self) -> None:
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只有 failed attempt"):
            self._scope(status="awaiting_receipts", quarantine=None)

    def test_conflict_scope_tampering_or_mixing_breaks_the_invariant(self) -> None:
        scope = self._scope(status="failed", quarantine=self.QUARANTINE)
        tampered = dict(scope, evidence_conflict_quarantine=dict(scope["evidence_conflict_quarantine"], invalidated_job_ids=["a"]))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "Job 闭集不一致"):
            self._validate(tampered)
        mixed = dict(scope, environment_isolation={"isolation_index": 1, "isolation_sha256": "9" * 64,
                                                   "invalidated_job_ids": []})
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "evidence_conflict_quarantine 作废作业非法"):
            self._validate(mixed)


class ConflictReconcilerUnitTests(unittest.TestCase):
    def test_invalidation_facts_event_ids_and_cause(self) -> None:
        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with mock.patch.object(
            codex_upgrade, "_attempt_conflict_quarantine_receipt",
            return_value=({"index": 2, "quarantine_sha256": "7" * 64}, {"disposition": "invalidated"}),
        ):
            facts = reconciler._conflict_invalidation_facts(
                Path("/nonexistent"),
                {"results": [{"id": "b", "status": "complete"}, {"id": "a", "status": "complete"},
                             {"id": "c", "status": "failed"}]},
                phase="candidate", candidate_id="cand", attempt_id="a1",
            )
        self.assertEqual(facts, {"quarantine_index": 2, "quarantine_sha256": "7" * 64, "invalidated_job_ids": ["a", "b"]})
        with mock.patch.object(codex_upgrade, "_attempt_conflict_quarantine_receipt", return_value=None):
            self.assertIsNone(reconciler._conflict_invalidation_facts(
                Path("/nonexistent"), {"results": []}, phase="candidate", candidate_id="cand", attempt_id="a1"
            ))
        self.assertEqual(reconciler._invalidation_ledger_event_id("a1", None, facts), "reconcile-attempt-conflict-a1")
        self.assertEqual(reconciler._invalidation_ledger_event_id("a1", {"isolation_index": 1}), "reconcile-attempt-isolation-a1")
        self.assertEqual(reconciler._invalidation_ledger_event_id("a1", None), "reconcile-attempt-evolution-a1")
        cause = reconciler._conflict_invalidation_cause("candidate", facts)
        self.assertEqual(
            (cause["root_cause_id"], cause["stable_error_code"]),
            ("evidence-conflict-02", "attempt.evidence-conflict-quarantined"),
        )

    def test_environment_facts_marks_quarantined_attempt_not_reusable(self) -> None:
        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        attempt = {
            "phase": "candidate", "candidate_id": "cand", "attempt_id": "a1", "status": "awaiting_receipts",
            "environment": {"after_probe": {"path": "x"}, "restoration_report": {"path": "y"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = Path(directory)
            for quarantined, expected in (({("candidate", "cand", "a1")}, "conflict_quarantined"), (set(), "restored")):
                with self.subTest(expected=expected), mock.patch.object(
                    codex_upgrade, "_conflict_quarantined_attempts", return_value=quarantined
                ):
                    facts = reconciler._environment_facts(Path(directory), attempt_root, attempt, [])
                    self.assertEqual(facts["status"], expected)


if __name__ == "__main__":
    unittest.main()
