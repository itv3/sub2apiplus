"""最终归档精确秘密扫描回执的离线测试。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import post_run_secret_scan_receipt as receipt


ACCESS_LABEL = "claude_oauth_runtime_access_token_value"
REFRESH_LABEL = "operator_scan_env:CLAUDE_CAPTURE_REFRESH_TOKEN"
ACCESS_ENV = "POST_SCAN_TEST_ACCESS"
REFRESH_ENV = "POST_SCAN_TEST_REFRESH"
ACCESS_SECRET = "access-secret-value-for-post-run-receipt-0123456789"
REFRESH_SECRET = "refresh-secret-value-for-post-run-receipt-9876543210"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_finalized_run(
    root: Path,
    *,
    run_id: str = "oauth-post-scan-test",
    files: dict[str, bytes] | None = None,
    scope: list[str] | None = None,
    status: str = "complete",
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(mode=0o700)
    payloads = files or {
        "analysis/summary.json": b'{"safe":true}\n',
        "results/s1/stdout.bin": b"safe binary payload\x00\xff",
    }
    artifacts = []
    for relative, payload in sorted(payloads.items()):
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        artifacts.append({
            "path": relative,
            "size": len(payload),
            "sha256": _sha256(payload),
            "mode": "0o600",
            "sensitivity": "raw_private",
        })
    manifest = {
        "schema_version": "official-client-capture/v1",
        "run_id": run_id,
        "status": status,
        "ended_at": "2026-08-01T00:00:00+00:00" if status == "complete" else None,
        "cleanup": {"attempted": True, "successful": True},
        "secret_scan": {
            "performed": True,
            "algorithm": "exact-byte-match/v1",
            "scope": scope or [ACCESS_LABEL, REFRESH_LABEL],
            "matches": [],
            "scan_errors": [],
            "passed": True,
        },
        "artifacts": artifacts,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _secret_specs() -> list[str]:
    return [
        f"{REFRESH_LABEL}={REFRESH_ENV}",
        f"{ACCESS_LABEL}={ACCESS_ENV}",
    ]


def _environment() -> dict[str, str]:
    return {
        ACCESS_ENV: ACCESS_SECRET,
        REFRESH_ENV: REFRESH_SECRET,
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _output_path(root: Path, run_dir: Path) -> Path:
    """建立运行目录外的回执父目录，并返回约定的扁平目标文件。"""
    output_parent = root / "post-run-secret-scan-receipts"
    output_parent.mkdir(mode=0o700)
    return output_parent / f"{run_dir.name}.json"


class SuccessfulReceiptTests(unittest.TestCase):
    def test_receipt_covers_final_manifest_and_has_fixed_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root)
            output = _output_path(root, run_dir)
            before = _tree_bytes(run_dir)

            result = receipt.generate_receipt(
                run_dir=run_dir,
                output=output,
                secret_specs=_secret_specs(),
                environ=_environment(),
            )

            self.assertTrue(result["passed"])
            self.assertEqual(_tree_bytes(run_dir), before)
            self.assertEqual(
                set(result),
                {
                    "schema_version",
                    "run_id",
                    "scan_root",
                    "algorithm",
                    "secret_labels",
                    "final_manifest_sha256",
                    "files",
                    "file_count",
                    "byte_count",
                    "inventory_sha256",
                    "manifest_artifact_inventory_verified",
                    "matches",
                    "scan_errors",
                    "passed",
                    "tool_sha256",
                },
            )
            self.assertEqual(result["schema_version"], receipt.SCHEMA_VERSION)
            self.assertEqual(result["scan_root"], ".")
            self.assertEqual(result["algorithm"], "exact-byte-match/v1")
            self.assertEqual(
                result["secret_labels"],
                sorted([ACCESS_LABEL, REFRESH_LABEL]),
            )
            paths = [item["path"] for item in result["files"]]
            self.assertEqual(paths, sorted(paths))
            self.assertIn("manifest.json", paths)
            self.assertEqual(result["file_count"], len(result["files"]))
            self.assertEqual(
                result["byte_count"],
                sum(item["size"] for item in result["files"]),
            )
            self.assertEqual(
                result["inventory_sha256"],
                _sha256(receipt._canonical_json_bytes(result["files"])),
            )
            manifest_bytes = (run_dir / "manifest.json").read_bytes()
            self.assertEqual(result["final_manifest_sha256"], _sha256(manifest_bytes))
            self.assertTrue(result["manifest_artifact_inventory_verified"])
            self.assertEqual(result["matches"], [])
            self.assertEqual(result["scan_errors"], [])
            self.assertRegex(result["tool_sha256"], r"^[0-9a-f]{64}$")

            stored = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stored, result)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_name_shorthand_uses_same_string_as_label_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(
                root,
                scope=["POST_SCAN_CANARY"],
            )
            output = _output_path(root, run_dir)
            result = receipt.generate_receipt(
                run_dir=run_dir,
                output=output,
                secret_specs=["POST_SCAN_CANARY"],
                environ={"POST_SCAN_CANARY": "nonempty-canary-value"},
            )
            self.assertEqual(result["secret_labels"], ["POST_SCAN_CANARY"])
            self.assertTrue(result["passed"])

    def test_final_manifest_is_parsed_from_single_and_multiple_chunks(self) -> None:
        for chunk_size in (10_000_000, 17):
            with self.subTest(chunk_size=chunk_size):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    run_dir = _write_finalized_run(root)
                    output = _output_path(root, run_dir)
                    with mock.patch.object(receipt, "CHUNK_SIZE", chunk_size):
                        result = receipt.generate_receipt(
                            run_dir=run_dir,
                            output=output,
                            secret_specs=_secret_specs(),
                            environ=_environment(),
                        )
                    self.assertTrue(result["passed"])
                    self.assertEqual(
                        result["final_manifest_sha256"],
                        _sha256((run_dir / "manifest.json").read_bytes()),
                    )


class SecretSafetyTests(unittest.TestCase):
    def test_match_returns_failure_and_receipt_never_contains_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(
                root,
                files={
                    "payload.bin": b"prefix-" + ACCESS_SECRET.encode() + b"-suffix",
                },
            )
            output = _output_path(root, run_dir)
            result = receipt.generate_receipt(
                run_dir=run_dir,
                output=output,
                secret_specs=_secret_specs(),
                environ=_environment(),
            )
            self.assertFalse(result["passed"])
            self.assertEqual(
                result["matches"],
                [{"path": "payload.bin", "secret_labels": [ACCESS_LABEL]}],
            )
            stored = output.read_bytes()
            self.assertNotIn(ACCESS_SECRET.encode(), stored)
            self.assertNotIn(REFRESH_SECRET.encode(), stored)
            self.assertNotIn(ACCESS_ENV.encode(), stored)
            self.assertNotIn(REFRESH_ENV.encode(), stored)

    def test_cli_mapping_returns_one_on_match_without_leaking_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(
                root,
                files={"payload.bin": REFRESH_SECRET.encode()},
            )
            output = _output_path(root, run_dir)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, _environment(), clear=False):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = receipt.main([
                        "--run-dir",
                        str(run_dir),
                        "--output",
                        str(output),
                        "--secret-env",
                        f"{ACCESS_LABEL}={ACCESS_ENV}",
                        "--secret-env",
                        f"{REFRESH_LABEL}={REFRESH_ENV}",
                    ])
            self.assertEqual(exit_code, 1)
            combined = (stdout.getvalue() + stderr.getvalue()).encode()
            self.assertNotIn(ACCESS_SECRET.encode(), combined)
            self.assertNotIn(REFRESH_SECRET.encode(), combined)
            self.assertFalse(json.loads(stdout.getvalue())["passed"])

    def test_exact_match_crosses_read_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = "cross-boundary-secret"
            run_dir = _write_finalized_run(
                root,
                files={"payload.bin": b"1234567" + secret.encode() + b"tail"},
                scope=["BOUNDARY_SECRET"],
            )
            output = _output_path(root, run_dir)
            with mock.patch.object(receipt, "CHUNK_SIZE", 8):
                result = receipt.generate_receipt(
                    run_dir=run_dir,
                    output=output,
                    secret_specs=["BOUNDARY_SECRET"],
                    environ={"BOUNDARY_SECRET": secret},
                )
            self.assertFalse(result["passed"])
            self.assertEqual(
                result["matches"],
                [{"path": "payload.bin", "secret_labels": ["BOUNDARY_SECRET"]}],
            )

    def test_empty_or_missing_secret_is_rejected_without_output(self) -> None:
        for environment in ({ACCESS_ENV: ""}, {}):
            with self.subTest(environment=environment):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    run_dir = _write_finalized_run(root)
                    output = _output_path(root, run_dir)
                    with self.assertRaises(receipt.ReceiptError) as caught:
                        receipt.generate_receipt(
                            run_dir=run_dir,
                            output=output,
                            secret_specs=[f"{ACCESS_LABEL}={ACCESS_ENV}"],
                            environ=environment,
                        )
                    self.assertEqual(caught.exception.code, "secret_value_empty")
                    self.assertFalse(output.exists())

    def test_labels_must_exactly_match_final_manifest_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root)
            output = _output_path(root, run_dir)
            with self.assertRaises(receipt.ReceiptError) as caught:
                receipt.generate_receipt(
                    run_dir=run_dir,
                    output=output,
                    secret_specs=[f"{ACCESS_LABEL}={ACCESS_ENV}"],
                    environ={ACCESS_ENV: ACCESS_SECRET},
                )
            self.assertEqual(caught.exception.code, "secret_scope_mismatch")
            self.assertFalse(output.exists())


class IntegrityAndPathTests(unittest.TestCase):
    def test_post_finalize_artifact_tamper_produces_failed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(
                root,
                files={"payload.bin": b"clean"},
            )
            (run_dir / "payload.bin").write_bytes(b"alter")
            output = _output_path(root, run_dir)
            result = receipt.generate_receipt(
                run_dir=run_dir,
                output=output,
                secret_specs=_secret_specs(),
                environ=_environment(),
            )
            self.assertFalse(result["passed"])
            self.assertFalse(result["manifest_artifact_inventory_verified"])
            self.assertIn("artifact_sha256_mismatch:payload.bin", result["scan_errors"])
            stored = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(stored["passed"])

    def test_read_error_produces_failed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root, files={"payload.bin": b"safe"})
            output = _output_path(root, run_dir)
            original = receipt._scan_file

            def fail_payload(entry, secrets):
                if entry.relative == "payload.bin":
                    raise OSError(5, "synthetic read failure")
                return original(entry, secrets)

            with mock.patch.object(receipt, "_scan_file", side_effect=fail_payload):
                result = receipt.generate_receipt(
                    run_dir=run_dir,
                    output=output,
                    secret_specs=_secret_specs(),
                    environ=_environment(),
                )
            self.assertFalse(result["passed"])
            self.assertIn("read_error:payload.bin:EIO", result["scan_errors"])
            self.assertIn("artifact_missing:payload.bin", result["scan_errors"])

    def test_symlink_and_non_regular_file_are_rejected(self) -> None:
        cases = ("symlink", "fifo")
        for kind in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    run_dir = _write_finalized_run(root)
                    if kind == "symlink":
                        (run_dir / "unsafe-link").symlink_to(run_dir / "manifest.json")
                        expected_code = "tree_symlink"
                    else:
                        os.mkfifo(run_dir / "unsafe-fifo")
                        expected_code = "tree_non_regular"
                    output = _output_path(root, run_dir)
                    with self.assertRaises(receipt.ReceiptError) as caught:
                        receipt.generate_receipt(
                            run_dir=run_dir,
                            output=output,
                            secret_specs=_secret_specs(),
                            environ=_environment(),
                        )
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root)
            output = _output_path(root, run_dir)
            receipt.generate_receipt(
                run_dir=run_dir,
                output=output,
                secret_specs=_secret_specs(),
                environ=_environment(),
            )
            before = output.read_bytes()
            with self.assertRaises(receipt.ReceiptError) as caught:
                receipt.generate_receipt(
                    run_dir=run_dir,
                    output=output,
                    secret_specs=_secret_specs(),
                    environ=_environment(),
                )
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(output.read_bytes(), before)

    def test_output_inside_run_and_non_final_manifest_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root)
            with self.assertRaises(receipt.ReceiptError) as caught:
                receipt.generate_receipt(
                    run_dir=run_dir,
                    output=run_dir / "receipt.json",
                    secret_specs=_secret_specs(),
                    environ=_environment(),
                )
            self.assertEqual(caught.exception.code, "output_inside_run")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = _write_finalized_run(root, status="running")
            output = _output_path(root, run_dir)
            with self.assertRaises(receipt.ReceiptError) as caught:
                receipt.generate_receipt(
                    run_dir=run_dir,
                    output=output,
                    secret_specs=_secret_specs(),
                    environ=_environment(),
                )
            self.assertEqual(caught.exception.code, "manifest_not_complete")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
