"""FW-E 生产运行态 before/after 比较测试。"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.official_client_capture.claude_fw_e_runtime_snapshot import (
    _container_snapshot,
    compare_snapshots,
)


def container(name: str) -> dict:
    return {
        "name": name,
        "container_id": "a" * 64,
        "image_id": "sha256:" + "b" * 64,
        "configured_image": "sub2apiplus:fixed",
        "repo_digests": ["sub2apiplus@sha256:" + "c" * 64],
        "image_labels": {
            "org.opencontainers.image.revision": "1" * 40,
            "org.opencontainers.image.version": "0.1.177-4",
        },
        "state": {
            "status": "running",
            "running": True,
            "paused": False,
            "restarting": False,
            "oom_killed": False,
            "dead": False,
            "started_at_utc": "2026-08-17T00:00:00Z",
            "health": {"status": "healthy", "failing_streak": 0},
        },
        "restart_count": 0,
        "mounts": [],
        "networks": [{"name": "production"}],
        "ports": [{"container_port": "8080/tcp", "bindings": []}],
    }


class RuntimeSnapshotTests(unittest.TestCase):
    def test_records_only_artifact_identity_labels(self) -> None:
        container_payload = [
            {
                "Id": "a" * 64,
                "Image": "sha256:" + "b" * 64,
                "State": {"Running": True},
                "Config": {"Image": "sub2apiplus:fixed"},
                "NetworkSettings": {"Networks": {}, "Ports": {}},
                "Mounts": [],
            }
        ]
        image_payload = [
            {
                "RepoDigests": [],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": "1" * 40,
                        "org.opencontainers.image.version": "0.1.177-4",
                        "io.sub2apiplus.stage": "FW-E",
                        "com.example.secret": "must-not-be-recorded",
                    }
                },
            }
        ]
        with patch(
            "tools.official_client_capture.claude_fw_e_runtime_snapshot._docker_json",
            side_effect=[container_payload, image_payload],
        ):
            result = _container_snapshot(Path("/usr/bin/docker"), "sub2apiplus")
        self.assertEqual(
            result["image_labels"],
            {
                "io.sub2apiplus.stage": "FW-E",
                "org.opencontainers.image.revision": "1" * 40,
                "org.opencontainers.image.version": "0.1.177-4",
            },
        )

    def test_allows_health_observation_time_change_without_runtime_drift(self) -> None:
        before = {"containers": [container("sub2apiplus")]}
        after = copy.deepcopy(before)
        result = compare_snapshots(before, after, ["sub2apiplus"])
        self.assertEqual(result["result"], "passed")

    def test_image_labels_are_evidence_but_not_legacy_stable_projection(self) -> None:
        before = {"containers": [container("sub2apiplus")]}
        before["containers"][0].pop("image_labels")
        after = {"containers": [container("sub2apiplus")]}
        result = compare_snapshots(before, after, ["sub2apiplus"])
        self.assertEqual(result["result"], "passed")

    def test_rejects_image_or_restart_drift(self) -> None:
        before = {"containers": [container("sub2apiplus")]}
        after = copy.deepcopy(before)
        after["containers"][0]["image_id"] = "sha256:" + "d" * 64
        after["containers"][0]["restart_count"] = 1
        result = compare_snapshots(before, after, ["sub2apiplus"])
        self.assertEqual(result["result"], "failed")
        self.assertEqual(
            result["differences"],
            [{"container": "sub2apiplus", "field": "stable_runtime_projection"}],
        )

    def test_rejects_unhealthy_after_state(self) -> None:
        before = {"containers": [container("sub2apiplus")]}
        after = copy.deepcopy(before)
        after["containers"][0]["state"]["health"] = {
            "status": "unhealthy",
            "failing_streak": 2,
        }
        result = compare_snapshots(before, after, ["sub2apiplus"])
        self.assertEqual(result["result"], "failed")
        self.assertIn(
            {"container": "sub2apiplus", "field": "health"},
            result["differences"],
        )


if __name__ == "__main__":
    unittest.main()
