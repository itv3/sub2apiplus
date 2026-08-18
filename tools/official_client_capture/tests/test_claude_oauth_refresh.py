"""Claude OAuth 受管刷新工具的 tmpfs、轮换和秘密门禁测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.claude_oauth_refresh import (
    load_claude_credentials,
    refresh_claude_credentials,
)


OLD_ACCESS = "old-access-secret-value-for-managed-refresh"
OLD_REFRESH = "old-refresh-secret-value-for-managed-refresh"
NEW_ACCESS = "new-access-secret-value-for-managed-refresh"
NEW_REFRESH = "new-refresh-secret-value-for-managed-refresh"


def write_credentials(path: Path) -> None:
    now_ms = int(time.time() * 1000)
    payload = {
        "claudeAiOauth": {
            "accessToken": OLD_ACCESS,
            "refreshToken": OLD_REFRESH,
            "expiresAt": now_ms - 60_000,
            "refreshTokenExpiresAt": now_ms + 3_600_000,
            "scopes": ["user:profile", "user:inference"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }
    }
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def write_fake_claude(path: Path) -> None:
    source = f'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("2.1.226 (Claude Code)")
    raise SystemExit(0)
if sys.argv[1:] != ["auth", "login"]:
    raise SystemExit(3)
if os.environ.get("CLAUDE_CODE_OAUTH_REFRESH_TOKEN") != {OLD_REFRESH!r}:
    raise SystemExit(4)
scopes = os.environ["CLAUDE_CODE_OAUTH_SCOPES"].split()
target = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
payload = {{
    "claudeAiOauth": {{
        "accessToken": {NEW_ACCESS!r},
        "refreshToken": {NEW_REFRESH!r},
        "expiresAt": int(time.time() * 1000) + 3_600_000,
        "refreshTokenExpiresAt": int(time.time() * 1000) + 7_200_000,
        "scopes": scopes,
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
    }}
}}
target.write_text(json.dumps(payload), encoding="utf-8")
target.chmod(0o600)
print("Login successful.")
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


class ClaudeOAuthRefreshTests(unittest.TestCase):
    def test_official_driver_rotates_atomically_without_persisting_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "state" / ".credentials.json"
            evidence = root / "evidence"
            memory = root / "memory"
            evidence.mkdir(mode=0o700)
            memory.mkdir(mode=0o700)
            write_credentials(credentials)
            binary = root / "claude"
            write_fake_claude(binary)
            expected_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
            receipt = evidence / "oauth-refresh-receipt.json"
            arguments = argparse.Namespace(
                execute=True,
                acknowledge_credential_rotation=True,
                expected_version="2.1.226",
                expected_sha256=expected_sha256,
                scan_root=evidence,
                receipt=receipt,
                memory_root=memory,
                credentials_file=credentials,
                claude_bin=binary,
            )

            with mock.patch(
                "tools.official_client_capture.claude_oauth_refresh._is_memory_backed",
                return_value=True,
            ):
                result = refresh_claude_credentials(arguments)

            self.assertEqual(
                load_claude_credentials(credentials), (NEW_ACCESS, NEW_REFRESH)
            )
            self.assertTrue(result["credential_update"]["access_token_rotated"])
            self.assertTrue(result["credential_update"]["refresh_token_rotated"])
            self.assertTrue(result["secret_scan"]["passed"])
            content = receipt.read_bytes()
            for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH):
                self.assertNotIn(secret.encode("utf-8"), content)
            self.assertEqual(list(memory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
