from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_environment_probe as probe
from tools.official_client_capture import codex_upgrade_receipt_finalizer as finalizer


class DockerFixture:
    """模拟 docker inspect/exec，并在返回值中混入不得落盘的秘密。"""

    secret = "sk-proj-environment-probe-secret-value"

    # Docker 生成的真实 /etc/hosts：末尾两行是同一容器在两个网络上的地址记录。
    default_hosts_lines = (
        "127.0.0.1\tlocalhost",
        "::1\tlocalhost ip6-localhost ip6-loopback",
        "fe00::\tip6-localnet",
        "ff00::\tip6-mcastprefix",
        "ff02::1\tip6-allnodes",
        "ff02::2\tip6-allrouters",
        "172.18.1.2\t04d138013b9a",
        "172.21.0.4\t04d138013b9a",
    )

    def __init__(
        self,
        *,
        fail_first: bool = False,
        duplicate_account_key: bool = False,
        hosts_lines: tuple[str, ...] | None = None,
    ) -> None:
        self.fail_first = fail_first
        self.duplicate_account_key = duplicate_account_key
        self.hosts_lines = (
            self.default_hosts_lines if hosts_lines is None else hosts_lines
        )
        self.calls: list[list[str]] = []

    def _completed(
        self,
        arguments: list[str],
        *,
        stdout: str = "",
        stderr: str | None = None,
        returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            returncode,
            stdout,
            self.secret if stderr is None else stderr,
        )

    def __call__(
        self,
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if not isinstance(arguments, list):
            raise AssertionError("命令必须使用参数数组")
        if kwargs.get("shell") is not False:
            raise AssertionError("禁止启用 shell")
        if self.fail_first:
            self.fail_first = False
            return self._completed(arguments, returncode=125)

        if arguments[:2] == ["docker", "inspect"]:
            container = arguments[2]
            suffix = hashlib.sha256(container.encode("utf-8")).hexdigest()
            document = {
                "Id": suffix,
                "Image": f"sha256:{'1' * 64}",
                "Config": {
                    "Env": [
                        "POSTGRES_USER=sub2api",
                        "POSTGRES_DB=sub2api",
                        f"POSTGRES_PASSWORD={self.secret}",
                        f"API_KEY={self.secret}",
                    ],
                    "Labels": {"secret": self.secret},
                },
                "Mounts": [
                    {
                        "Destination": "/app/data",
                        "RW": True,
                        "Source": f"/private/{self.secret}/{container}",
                        "Type": "volume",
                    }
                ],
                "State": {
                    "Health": {"Status": "healthy"},
                    "Running": True,
                    "Status": "running",
                },
            }
            return self._completed(arguments, stdout=json.dumps([document]))

        if arguments[:2] != ["docker", "exec"]:
            raise AssertionError(f"未预期的命令：{arguments!r}")
        inner = arguments[3:]
        if inner[:1] in (["sh"], ["bash"]):
            raise AssertionError("禁止通过容器内 shell 拼接命令")
        if inner[:1] == ["pg_isready"]:
            return self._completed(arguments, stdout="accepting connections\n")
        if inner[:3] == ["redis-cli", "--raw", "PING"]:
            return self._completed(arguments, stdout="PONG\n")
        if inner[:2] == ["test", "-f"]:
            return self._completed(arguments)
        if inner == ["cat", "/etc/hosts"]:
            return self._completed(
                arguments,
                stdout="".join(f"{line}\n" for line in self.hosts_lines),
            )
        if inner[:1] == ["sha256sum"]:
            digest = hashlib.sha256(
                f"{arguments[2]}:{inner[1]}".encode("utf-8")
            ).hexdigest()
            return self._completed(arguments, stdout=f"{digest}  {inner[1]}\n")
        if inner[:1] != ["psql"]:
            raise AssertionError(f"未预期的容器命令：{arguments!r}")

        sql = inner[-1]
        if "to_regclass" in sql:
            table = sql.split("public.", 1)[1].split("'", 1)[0]
            return self._completed(
                arguments,
                stdout=json.dumps({"exists": table != "proxies"}) + "\n",
            )
        if "primary_key_rows" in sql:
            table = sql.split("FROM public.", 1)[1].split(";", 1)[0]
            rows = (
                [[1, 5], [2, 5], [3, 8]]
                if table == "account_groups"
                else [[1], [2], [3]]
            )
            if table == "settings":
                rows = [[self.secret]]
            if table == "accounts" and self.duplicate_account_key:
                rows = [[1], [1]]
            return self._completed(
                arguments,
                stdout=json.dumps(
                    {"primary_key_rows": rows, "row_count": len(rows)}
                )
                + "\n",
            )
        if "'max_id'" in sql:
            return self._completed(
                arguments,
                stdout=json.dumps({"max_id": 19, "row_count": 17}) + "\n",
            )
        if "FROM public.accounts AS account_row" in sql:
            return self._completed(
                arguments,
                stdout=json.dumps(
                    {
                        "credentials_present": True,
                        "exists": True,
                        "extra_digest": "b" * 32,
                        "id": 41,
                        "parent_account_id": None,
                        "platform": "openai",
                        "proxy_fallback_origin_id": None,
                        "proxy_id": 3,
                        "schedulable": True,
                        "status": "active",
                        "type": "oauth",
                    }
                )
                + "\n",
            )
        if "FROM public.api_keys AS key_row" in sql:
            return self._completed(
                arguments,
                stdout=json.dumps(
                    {
                        "exists": True,
                        "group_id": 5,
                        "id": 73,
                        "status": "active",
                        "user_id": 11,
                    }
                )
                + "\n",
            )
        raise AssertionError(f"未识别的 SQL：{sql}")


class EnvironmentProbeTest(unittest.TestCase):
    def _arguments(
        self,
        output_dir: Path,
        phase: str = "before",
    ) -> probe.ProbeArguments:
        return probe.ProbeArguments(
            output_dir=output_dir,
            service_container="sub2apiplus",
            keeper_container="sub2apiplus-keeper",
            postgres_container="sub2apiplus-postgres",
            redis_container="sub2apiplus-redis",
            capture_container="oauth-capture",
            account_id=41,
            api_key_id=73,
            phase=phase,
        )

    def test_probe_does_not_persist_environment_or_secret_values(self) -> None:
        fixture = DockerFixture()
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "probe"
            started_at = datetime.now(timezone.utc)
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                manifest = probe.run_probe(self._arguments(output_dir))
            completed_at = datetime.now(timezone.utc)

            persisted = b"".join(
                path.read_bytes() for path in sorted(output_dir.iterdir())
            )
            self.assertNotIn(fixture.secret.encode("utf-8"), persisted)
            self.assertNotIn(b"POSTGRES_PASSWORD", persisted)
            self.assertNotIn(b"Config.Env", persisted)
            self.assertEqual(manifest["selected_account_id"], 41)
            observed_at = datetime.fromisoformat(
                manifest["observed_at_utc"].replace("Z", "+00:00")
            )
            self.assertTrue(manifest["observed_at_utc"].endswith("Z"))
            self.assertEqual(observed_at.utcoffset(), timezone.utc.utcoffset(observed_at))
            self.assertLessEqual(started_at, observed_at)
            self.assertLessEqual(observed_at, completed_at)
            persisted_manifest = json.loads(
                (output_dir / "probe-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted_manifest["observed_at_utc"],
                manifest["observed_at_utc"],
            )
            account = json.loads((output_dir / "account-state.json").read_text())
            self.assertTrue(account["state"]["selected_account"]["credentials_present"])
            self.assertEqual(
                account["state"]["selected_account"]["extra_digest"],
                "b" * 32,
            )
            self.assertNotIn("credentials", account["state"]["selected_account"])
            self.assertTrue(
                all(call[:2] in (["docker", "inspect"], ["docker", "exec"])
                    for call in fixture.calls)
            )

    def test_command_failure_is_fail_closed_and_does_not_leak_stderr(self) -> None:
        fixture = DockerFixture(fail_first=True)
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "probe"
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                with self.assertRaises(probe.EnvironmentProbeError) as caught:
                    probe.run_probe(self._arguments(output_dir))
            self.assertNotIn(fixture.secret, str(caught.exception))
            self.assertFalse(output_dir.exists())

    def test_duplicate_primary_key_fingerprint_is_fail_closed(self) -> None:
        fixture = DockerFixture(duplicate_account_key=True)
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "probe"
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                with self.assertRaises(probe.EnvironmentProbeError):
                    probe.run_probe(self._arguments(output_dir))
            self.assertFalse(output_dir.exists())

    def test_outputs_are_exclusive_private_and_cannot_be_overwritten(self) -> None:
        fixture = DockerFixture()
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "probe"
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                probe.run_probe(self._arguments(output_dir))
            original = {
                path.name: path.read_bytes() for path in sorted(output_dir.iterdir())
            }
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                with self.assertRaises(probe.EnvironmentProbeError):
                    probe.run_probe(self._arguments(output_dir))
            self.assertEqual(
                original,
                {path.name: path.read_bytes() for path in sorted(output_dir.iterdir())},
            )
            self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
            for path in output_dir.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def _configuration_snapshot(self, hosts_lines: tuple[str, ...], root: Path) -> bytes:
        fixture = DockerFixture(hosts_lines=hosts_lines)
        with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
            probe.run_probe(self._arguments(root))
        return (root / "configuration-state.json").read_bytes()

    def test_hosts_line_order_alone_does_not_break_configuration_restore(self) -> None:
        """docker restart 重建 /etc/hosts 时地址行会换序，不得判成环境未恢复。"""

        before_lines = DockerFixture.default_hosts_lines
        after_lines = (
            *before_lines[:6],
            before_lines[7],
            before_lines[6],
        )
        self.assertNotEqual(before_lines, after_lines)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = self._configuration_snapshot(before_lines, root / "before")
            after = self._configuration_snapshot(after_lines, root / "after")
            self.assertEqual(before, after)
            record = next(
                item
                for item in json.loads(before)["state"]["records"]
                if item["role"] == "service"
            )
            self.assertEqual(record["hosts_digest_mode"], "sorted_lines_sha256")
            self.assertEqual(
                record["hosts_sha256"],
                hashlib.sha256(
                    "".join(f"{line}\n" for line in sorted(before_lines)).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            )

    def test_hosts_entry_drift_is_still_detected(self) -> None:
        """新增、删除或改写条目仍必须改变摘要，规范化不得吸收真实漂移。"""

        baseline = DockerFixture.default_hosts_lines
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = self._configuration_snapshot(baseline, root / "before")
            drifted = {
                "added": (*baseline, "172.21.0.9\tchatgpt.com"),
                "removed": baseline[:-1],
                "rewritten": (*baseline[:-1], "172.21.0.5\t04d138013b9a"),
            }
            for name, lines in drifted.items():
                with self.subTest(drift=name):
                    self.assertNotEqual(
                        before,
                        self._configuration_snapshot(lines, root / name),
                    )

    def test_normalized_structure_is_stable_and_marks_watermarks_non_byte_equal(
        self,
    ) -> None:
        fixture = DockerFixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            with mock.patch.object(probe.subprocess, "run", side_effect=fixture):
                probe.run_probe(self._arguments(first))
                probe.run_probe(self._arguments(second))

            for filename in probe.STATE_FILES.values():
                self.assertEqual(
                    (first / filename).read_bytes(),
                    (second / filename).read_bytes(),
                )
                document = json.loads((first / filename).read_text())
                self.assertEqual(
                    document["schema_version"],
                    "codex-candidate-normalized-state/v1",
                )
                self.assertEqual(set(document), {"schema_version", "state"})

            database = json.loads((first / "database-state.json").read_text())[
                "state"
            ]
            self.assertEqual(
                [item["name"] for item in database["protected_tables"]],
                sorted(spec.name for spec in probe.PROTECTED_TABLES),
            )
            proxy_record = next(
                item
                for item in database["protected_tables"]
                if item["name"] == "proxies"
            )
            self.assertFalse(proxy_record["exists"])
            self.assertEqual(proxy_record["primary_key_fingerprints"], [])
            accounts_record = next(
                item
                for item in database["protected_tables"]
                if item["name"] == "accounts"
            )
            expected_fingerprints = sorted(
                hashlib.sha256(
                    json.dumps([value], separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                for value in (1, 2, 3)
            )
            self.assertEqual(
                accounts_record["primary_key_fingerprints"],
                expected_fingerprints,
            )
            self.assertTrue(
                all(
                    len(value) == 64
                    for value in accounts_record["primary_key_fingerprints"]
                )
            )
            self.assertEqual(
                database["comparison_policy"]["protected_table_rule"],
                "before_primary_key_fingerprints_subset",
            )

            manifest = json.loads((first / "probe-manifest.json").read_text())
            self.assertEqual(
                [item["kind"] for item in manifest["snapshots"]],
                ["service", "containers", "database", "account", "configuration"],
            )
            for binding in manifest["snapshots"]:
                payload = (first / binding["path"]).read_bytes()
                self.assertEqual(
                    binding["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )
            database_binding = manifest["snapshots"][2]
            self.assertEqual(
                database_binding["comparison"]["mode"],
                "protected_plus_watermarks",
            )
            root_context = finalizer._validate_root(first)
            database_document = finalizer._load_document(
                root_context,
                Path("database-state.json"),
                "database-state",
            )
            parsed_database = finalizer._database_state(
                database_document,
                "database-state",
            )
            self.assertEqual(
                set(parsed_database.protected_tables),
                set(finalizer.DATABASE_PROTECTED_TABLES),
            )
            self.assertEqual(
                set(parsed_database.watermarks),
                finalizer.DATABASE_WATERMARK_TABLES,
            )


if __name__ == "__main__":
    unittest.main()
