"""Candidate 就绪静态故障的 reservation 前失败矩阵。"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_candidate_readiness as readiness


IMAGE_ID = "sha256:" + "1" * 64
BUILD_SHA256 = "2" * 64


class _Admission:
    """只读项目 head；静态失败时不应触发任何入账。"""

    head_sequence = 7
    head_sha256 = "3" * 64


class _Deadline:
    def check(self, _operation: str) -> None:
        return None


def _checks_with_failure(check_id: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for current_id, failure_code in readiness.STATIC_CHECK_FAILURE_CODES.items():
        identity: dict[str, object] = {"check_id": current_id}
        if current_id == "candidate-readiness.image":
            identity.update(
                {
                    "candidate_id": "candidate-a",
                    "image_id": IMAGE_ID,
                    "build_receipt_sha256": BUILD_SHA256,
                }
            )
        failed = current_id == check_id
        checks.append(
            {
                "check_id": current_id,
                "failure_code": failure_code,
                "status": "failed" if failed else "passed",
                "error_type": "RuntimeError" if failed else None,
                "evidence": {} if failed else {"identity": identity},
            }
        )
    return checks


def _valid_storage_probe() -> dict[str, object]:
    return {
        "status": "passed",
        "capture_container": "capture",
        "capture_root": "/capture",
        "host_data_root": {
            "path": "/root/docker/capture-cli/data",
            "mode": 0o700,
            "uid": 0,
            "gid": 0,
            "device": 1,
            "inode": 2,
        },
        "writable_namespaces": [
            {
                "name": name,
                "source": f"/host/{name}",
                "source_mode": 0o700,
                "source_uid": 0,
                "source_gid": 0,
                "source_device": 1,
                "source_inode": index,
                "cleanup_verified": True,
            }
            for index, name in enumerate(("runs", "runtime"), 10)
        ],
        "job_roots_sha256": "4" * 64,
    }


def _valid_routing_snapshot() -> dict[str, object]:
    return {
        "account_id": 22,
        "account_platform": "openai",
        "account_type": "oauth",
        "account_status": "active",
        "account_schedulable": True,
        "parent_account_id": None,
        "token_present": True,
        "api_key_id": 9,
        "api_key_status": "active",
        "group_id": 7,
        "group_platform": "openai",
        "group_status": "active",
        "eligible_account_ids": [22],
        "model_mapping_type": "missing",
        "model_mapping_count": 0,
    }


class CandidateReadinessFailureMatrixTests(unittest.TestCase):
    def test_collector_injects_each_historical_fault_as_its_own_check(self) -> None:
        """实际静态采集器必须把六类故障分别固化，不得合并根因。"""

        cases = (
            "candidate-readiness.storage",
            "candidate-readiness.runtime-files",
            "candidate-readiness.root-filesystem",
            "candidate-readiness.routing-platform",
            "candidate-readiness.model-mapping",
            "candidate-readiness.image",
        )
        configuration = {
            "capture_container": "capture",
            "service_container": "candidate-service",
            "codex_account_id": 22,
            "api_key_id": 9,
        }
        for check_id in cases:
            with self.subTest(check_id=check_id):
                storage_probe = _valid_storage_probe()
                routing_snapshot = _valid_routing_snapshot()
                runtime_fact: object = {"identity": {"files": []}}
                root_fact: object = {"identity": {"watermark_policy": "fixture"}}
                image_fact: object = {
                    "identity": {
                        "candidate_id": "candidate-a",
                        "image_id": IMAGE_ID,
                        "build_receipt_sha256": BUILD_SHA256,
                    }
                }
                if check_id == "candidate-readiness.storage":
                    storage_probe["status"] = "failed"
                elif check_id == "candidate-readiness.runtime-files":
                    runtime_fact = RuntimeError("runtime 文件缺失")
                elif check_id == "candidate-readiness.root-filesystem":
                    root_fact = RuntimeError("磁盘水位")
                elif check_id == "candidate-readiness.routing-platform":
                    routing_snapshot["group_platform"] = "anthropic"
                elif check_id == "candidate-readiness.model-mapping":
                    routing_snapshot["model_mapping_type"] = "object"
                    routing_snapshot["model_mapping_count"] = 1
                elif check_id == "candidate-readiness.image":
                    image_fact = RuntimeError("镜像缺少构建标签")

                def fact(value: object):
                    if isinstance(value, BaseException):
                        raise value
                    return value

                with (
                    mock.patch.object(
                        readiness,
                        "_runtime_file_fact",
                        side_effect=lambda *_args: fact(runtime_fact),
                    ),
                    mock.patch.object(
                        readiness,
                        "_root_filesystem_fact",
                        side_effect=lambda: fact(root_fact),
                    ),
                    mock.patch.object(
                        readiness,
                        "_routing_snapshot",
                        return_value=routing_snapshot,
                    ),
                    mock.patch.object(
                        readiness,
                        "_service_image_fact",
                        side_effect=lambda *_args: fact(image_fact),
                    ),
                ):
                    checks = readiness.collect_static_checks(
                        configuration=configuration,
                        target_version="0.154.0",
                        candidate_id="candidate-a",
                        identity={"image_id": IMAGE_ID},
                        build_receipt_sha256=BUILD_SHA256,
                        storage_probe=storage_probe,
                    )

                failed = [
                    row for row in checks if row.get("status") == "failed"
                ]
                self.assertEqual([row["check_id"] for row in failed], [check_id])
                self.assertEqual(
                    failed[0]["failure_code"],
                    readiness.STATIC_CHECK_FAILURE_CODES[check_id],
                )

    def test_six_static_faults_stop_before_probe_reservation_and_project_head(
        self,
    ) -> None:
        """六类历史故障均必须保持零请求、零 attempt 与 head 不变。"""

        cases = (
            "candidate-readiness.storage",
            "candidate-readiness.runtime-files",
            "candidate-readiness.root-filesystem",
            "candidate-readiness.routing-platform",
            "candidate-readiness.model-mapping",
            "candidate-readiness.image",
        )
        manifest = {
            "campaign_id": "campaign-a",
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "target_version": "0.154.0",
            "configuration": {
                "capture_container": "capture",
                "service_container": "candidate-service",
                "codex_account_id": 22,
                "api_key_id": 9,
            },
        }
        identity = {"image_id": IMAGE_ID}

        for check_id in cases:
            with self.subTest(check_id=check_id), tempfile.TemporaryDirectory() as directory:
                campaign_dir = Path(directory).resolve() / "campaign"
                campaign_dir.mkdir(mode=0o700)
                admission = _Admission()

                @contextlib.contextmanager
                def admission_scope(*_args: object, **_kwargs: object):
                    yield admission

                with (
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_project_ledger,
                        "runtime_admission_scope",
                        side_effect=admission_scope,
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_project_ledger_required",
                        return_value=True,
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_failed_capture_attempts",
                        return_value=[],
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_candidate_readiness_checks",
                        return_value=_checks_with_failure(check_id),
                    ),
                    mock.patch.object(
                        readiness,
                        "ensure_models_probe",
                    ) as probe,
                    mock.patch.object(
                        codex_upgrade,
                        "_reserve_capture_attempt",
                    ) as reserve,
                ):
                    with self.assertRaises(readiness.CandidateReadinessError) as caught:
                        codex_upgrade._reserve_candidate_capture_attempt(
                            campaign_dir,
                            manifest=manifest,
                            candidate_id="candidate-a",
                            identity=identity,
                            jobs=[mock.Mock()],
                            build_receipt_binding={"sha256": BUILD_SHA256},
                            allow_failed_rerun=False,
                            deadline=_Deadline(),
                            lease=None,
                        )

                self.assertEqual(
                    caught.exception.failure_observations,
                    [
                        {
                            "check_id": check_id,
                            "failure_code": readiness.STATIC_CHECK_FAILURE_CODES[
                                check_id
                            ],
                        }
                    ],
                )
                probe.assert_not_called()
                reserve.assert_not_called()
                self.assertEqual(admission.head_sequence, 7)
                self.assertEqual(admission.head_sha256, "3" * 64)
                self.assertEqual(list(campaign_dir.rglob("reservation.json")), [])
                self.assertEqual(list(campaign_dir.rglob("attempt.json")), [])

                receipts = list(campaign_dir.rglob("static-*.json"))
                self.assertEqual(len(receipts), 1)
                payload = json.loads(receipts[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["live_request_count"], 0)


if __name__ == "__main__":
    unittest.main()
