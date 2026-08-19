"""Claude FW-F 完整 Campaign 宿主编排器测试。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_f_complete_runner import (
    CompleteCampaignRunnerError,
    _capture_command,
    _validate_container,
)


class ClaudeFWFCompleteRunnerTests(unittest.TestCase):
    def _container(self, root: Path, *, synthetic: bool) -> dict:
        network = "none" if synthetic else "claude-fw-f-v17-net"
        networks = (
            {"none": {"Gateway": "", "IPAddress": ""}}
            if synthetic
            else {network: {"NetworkID": "a" * 64}}
        )
        return {
            "State": {"Running": True},
            "HostConfig": {
                "NetworkMode": network,
                "Tmpfs": {"/tmp": "rw", "/dev/shm": "rw"},
            },
            "NetworkSettings": {"Networks": networks},
            "Mounts": [
                {"Destination": "/campaign", "Source": str(root), "RW": True},
                {"Destination": "/campaign/source", "Source": str(root / "source"), "RW": False},
                {"Destination": "/campaign/campaign.json", "Source": str(root / "campaign.json"), "RW": False},
                {"Destination": "/campaign/scenario-catalog.json", "Source": str(root / "scenario-catalog.json"), "RW": False},
                {"Destination": "/campaign/candidate-denominator.json", "Source": str(root / "candidate-denominator.json"), "RW": False},
                {"Destination": "/opt/claude", "Source": "/runtime/claude", "RW": False},
                {"Destination": "/run/claude-secrets", "Source": f"/dev/shm/{root.name}-secret", "RW": False},
                {"Destination": "/run/mitm/ca.pem", "Source": "/state/ca.pem", "RW": False},
                {"Destination": "/run/mitm/ca-cert.pem", "Source": "/state/ca-cert.pem", "RW": False},
                {"Destination": "/etc/hosts", "Source": f"/dev/shm/{root.name}-hosts", "RW": True},
            ],
        }

    def test_container_rejects_production_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self._container(root, synthetic=False)
            item["HostConfig"]["NetworkMode"] = "proxy-network"
            item["NetworkSettings"]["Networks"] = {"proxy-network": {}}
            with self.assertRaises(CompleteCampaignRunnerError):
                _validate_container(
                    item=item,
                    campaign_root=root,
                    synthetic=False,
                    forbidden_networks={"proxy-network"},
                )

    def test_synthetic_container_requires_network_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self._container(root, synthetic=True)
            _validate_container(
                item=item,
                campaign_root=root,
                synthetic=True,
                forbidden_networks={"proxy-network"},
            )
            item["HostConfig"]["NetworkMode"] = "bridge"
            with self.assertRaises(CompleteCampaignRunnerError):
                _validate_container(
                    item=item,
                    campaign_root=root,
                    synthetic=True,
                    forbidden_networks={"proxy-network"},
                )

    def test_capture_command_separates_live_and_synthetic(self) -> None:
        common = {
            "attempt_root": "/campaign/attempts/p/attempt-001",
            "receipt": "/campaign/runtime-receipts/p.json",
            "receipt_sha256": "a" * 64,
            "nonce": "b" * 64,
            "runtime_image": "capture@sha256:" + "c" * 64,
            "expected_version": "2.1.226",
            "expected_sha256": "d" * 64,
            "upstream_ip": "160.79.104.10",
            "model": "claude-sonnet-5",
            "timeout": 300,
        }
        live = _capture_command(
            container="real",
            probe={"probe_id": "p", "response_plan": None},
            **common,
        )
        synthetic = _capture_command(
            container="synth",
            probe={"probe_id": "p", "response_plan": "retry-529"},
            **common,
        )
        self.assertIn("--acknowledge-live-requests", live)
        self.assertIn("--upstream-ip", live)
        self.assertNotIn("--acknowledge-synthetic-responses", live)
        self.assertIn("--acknowledge-synthetic-responses", synthetic)
        self.assertNotIn("--upstream-ip", synthetic)


if __name__ == "__main__":
    unittest.main()
