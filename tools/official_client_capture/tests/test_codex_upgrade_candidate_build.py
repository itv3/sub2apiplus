"""VC-4 严格构建收据的实物复算与负例。"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_candidate_build as build


IMAGE_ID = "sha256:" + "8" * 64
RUNTIME_IMAGE = "registry/sub2api@sha256:" + "7" * 64
COMMIT = "5" * 40
CANDIDATE_ID = "c0154-candidate-v8"


class CandidateBuildReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.build_tree = self.root / "build-tree"
        self.context = self.root / "context"
        self.dist_source = self.root / "dist-source"
        self.binary = self.root / "sub2api"
        for path in (
            self.source / "frontend",
            self.source / "deploy",
            self.build_tree / "deploy",
            self.build_tree / "backend/resources",
            self.build_tree / "backend/internal/web/dist/assets",
            self.context / "deploy",
            self.context / "backend/resources",
            self.dist_source / "assets",
        ):
            path.mkdir(parents=True, mode=0o755)
        self._write(self.source / "frontend/package.json", '{"scripts":{"build":"vite build"}}\n')
        self._write(self.source / "frontend/pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self._write(self.build_tree / "Dockerfile.goreleaser", "FROM scratch\n", 0o644)
        self._write(self.build_tree / "deploy/docker-entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n", 0o755)
        self._write(self.build_tree / "deploy/container-healthcheck.sh", "#!/bin/sh\nexit 0\n", 0o755)
        self._copy_file(
            self.build_tree / "deploy/docker-entrypoint.sh",
            self.source / "deploy/docker-entrypoint.sh",
        )
        self._copy_file(
            self.build_tree / "deploy/container-healthcheck.sh",
            self.source / "deploy/container-healthcheck.sh",
        )
        self._write(self.build_tree / "backend/resources/models.json", "{}\n", 0o644)
        self._write(self.binary, "candidate-binary\n", 0o755)
        self._write(self.dist_source / "index.html", "<main>ok</main>\n", 0o644)
        self._write(self.dist_source / "assets/app.js", "export default 1\n", 0o644)
        self._copy_tree_files(
            self.dist_source,
            self.build_tree / "backend/internal/web/dist",
        )
        self._write(self.context / "Dockerfile", "FROM scratch\n", 0o644)
        self._copy_file(
            self.build_tree / "deploy/docker-entrypoint.sh",
            self.context / "deploy/docker-entrypoint.sh",
        )
        self._copy_file(
            self.build_tree / "deploy/container-healthcheck.sh",
            self.context / "deploy/container-healthcheck.sh",
        )
        self._copy_file(
            self.build_tree / "backend/resources/models.json",
            self.context / "backend/resources/models.json",
        )
        self._copy_file(self.binary, self.context / "sub2api")
        self.builder_receipt_path = self.root / "frontend-builder.json"
        self.parameters = self._parameters()
        self._write_builder_receipt()
        self.parameters = self._parameters()

    @staticmethod
    def _write(path: Path, text: str, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        destination.chmod(stat.S_IMODE(source.stat().st_mode))

    def _copy_tree_files(self, source: Path, destination: Path) -> None:
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            target = destination / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(stat.S_IMODE(path.stat().st_mode))
            else:
                self._copy_file(path, target)

    @staticmethod
    def _binding(path: Path) -> dict[str, object]:
        return {
            "path": str(path.resolve()),
            "sha256": build.file_sha256(path),
            "bytes": path.stat().st_size,
        }

    def _parameters(self) -> dict[str, object]:
        builder_binding = (
            self._binding(self.builder_receipt_path)
            if self.builder_receipt_path.exists()
            else {"path": str(self.builder_receipt_path), "sha256": "0" * 64, "bytes": 1}
        )
        return {
            "schema_version": build.BUILD_PARAMETERS_SCHEMA,
            "candidate_id": CANDIDATE_ID,
            "source": {"root": str(self.source), "git_commit": COMMIT},
            "build_tree": {
                "root": str(self.build_tree),
                "git_commit": COMMIT,
                "umask": "0022",
            },
            "frontend": {
                "source_root": "frontend",
                "package_manifest": "package.json",
                "lockfile": "pnpm-lock.yaml",
                "build_command": ["pnpm", "run", "build"],
                "node_version": "v20.19.4",
                "pnpm_version": "9.15.9",
                "builder": {
                    "kind": "release_pipeline",
                    "identity": "github-actions:release-frontend",
                    "receipt": builder_binding,
                },
                "dist_source_root": str(self.dist_source),
                "dist_build_tree_path": "backend/internal/web/dist",
                "toolchain_policy": {
                    "required_node_major": 20,
                    "deviation_approval": None,
                },
            },
            "go_build": {
                "command": ["go", "build", "-tags=embed,candidatecapture", "./cmd/server"],
                "working_directory": "backend",
                "environment": {
                    "CGO_ENABLED": "0",
                    "GOOS": "linux",
                    "GOARCH": "arm64",
                    "GOFLAGS": "-mod=vendor",
                },
                "required_tags": ["candidatecapture", "embed"],
            },
            "docker_build": {
                "context_root": str(self.context),
                "dockerfile": "Dockerfile",
                "platform": "linux/arm64",
                "image_id": IMAGE_ID,
                "labels": {
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.version": f"0.2.4-4-{CANDIDATE_ID}",
                },
                "entrypoint": ["/app/docker-entrypoint.sh"],
                "assembly": [
                    {
                        "context_path": "Dockerfile",
                        "source_kind": "build_tree",
                        "source_path": "Dockerfile.goreleaser",
                    },
                    {
                        "context_path": "backend/resources",
                        "source_kind": "build_tree",
                        "source_path": "backend/resources",
                    },
                    {
                        "context_path": "deploy",
                        "source_kind": "build_tree",
                        "source_path": "deploy",
                    },
                    {
                        "context_path": "sub2api",
                        "source_kind": "binary",
                        "source_path": str(self.binary),
                    },
                ],
            },
            "binary": self._binding(self.binary),
        }

    def _write_builder_receipt(self) -> None:
        dist = build.scan_tree_inventory(self.dist_source)
        payload = {
            "schema_version": build.FRONTEND_BUILDER_SCHEMA,
            "status": "complete",
            "builder_identity": "github-actions:release-frontend",
            "source_git_commit": COMMIT,
            "build_command": ["pnpm", "run", "build"],
            "node_version": "v20.19.4",
            "pnpm_version": "9.15.9",
            "package_manifest_sha256": build.file_sha256(self.source / "frontend/package.json"),
            "lockfile_sha256": build.file_sha256(self.source / "frontend/pnpm-lock.yaml"),
            "dist_inventory_sha256": dist["inventory_sha256"],
            "live_request_count": 0,
            "built_at_utc": "2026-09-17T00:00:00Z",
        }
        payload["receipt_digest"] = build.digest(payload)
        self.builder_receipt_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.builder_receipt_path.chmod(0o600)

    def _validate_parameters(self, parameters: dict[str, object] | None = None) -> dict[str, object]:
        return build.validate_build_parameters(
            parameters or self.parameters,
            candidate_id=CANDIDATE_ID,
            source_root=self.source,
            git_commit=COMMIT,
            binary_path=self.binary,
            binary_sha256=build.file_sha256(self.binary),
            binary_bytes=self.binary.stat().st_size,
            build_tree=self.build_tree,
            docker_context=self.context,
            frontend_dist_source=self.dist_source,
            target_architecture="linux/arm64",
            image_id=IMAGE_ID,
        )

    def _image_runner(
        self,
        *,
        wrong_label: bool = False,
        wrong_binary: bool = False,
        wrong_entrypoint: bool = False,
        wrong_architecture: bool = False,
        wrong_script_mode: bool = False,
        wrong_script_hash: bool = False,
        missing_tag: bool = False,
    ):
        entrypoint = self.context / "deploy/docker-entrypoint.sh"
        healthcheck = self.context / "deploy/container-healthcheck.sh"

        def run(arguments):
            arguments = list(arguments)
            if arguments[:3] == ["docker", "image", "inspect"]:
                version = (
                    "0.2.4-4-wrong-candidate"
                    if wrong_label
                    else f"0.2.4-4-{CANDIDATE_ID}"
                )
                output = json.dumps(
                    [
                        {
                            "Id": IMAGE_ID,
                            "RepoDigests": [RUNTIME_IMAGE],
                            "Os": "linux",
                            "Architecture": "amd64" if wrong_architecture else "arm64",
                            "Config": {
                                "Entrypoint": (
                                    ["/app/sub2api"]
                                    if wrong_entrypoint
                                    else ["/app/docker-entrypoint.sh"]
                                ),
                                "Labels": {
                                    "org.opencontainers.image.revision": COMMIT,
                                    "org.opencontainers.image.version": version,
                                },
                            },
                        }
                    ]
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")
            if arguments[:2] == ["go", "version"]:
                output = (
                    f"{self.binary}: go1.27.1\n"
                    f"\tbuild\t-tags={'embed' if missing_tag else 'embed,candidatecapture'}\n"
                    "\tbuild\tCGO_ENABLED=0\n"
                    "\tbuild\tGOARCH=arm64\n"
                    "\tbuild\tGOOS=linux\n"
                    "\tbuild\tvcs=git\n"
                    f"\tbuild\tvcs.revision={COMMIT}\n"
                    "\tbuild\tvcs.modified=false\n"
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")
            if "/bin/sh" in arguments:
                binary_sha = "0" * 64 if wrong_binary else build.file_sha256(self.binary)
                entrypoint_mode = "775" if wrong_script_mode else "755"
                entrypoint_sha = (
                    "1" * 64
                    if wrong_script_hash
                    else build.file_sha256(entrypoint)
                )
                output = "\n".join(
                    [
                        f"/app/docker-entrypoint.sh\t{entrypoint_mode}\t"
                        f"{entrypoint.stat().st_size}\t{entrypoint_sha}",
                        f"/app/container-healthcheck.sh\t755\t"
                        f"{healthcheck.stat().st_size}\t{build.file_sha256(healthcheck)}",
                        f"/app/sub2api\t755\t{self.binary.stat().st_size}\t{binary_sha}",
                    ]
                ) + "\n"
                return subprocess.CompletedProcess(arguments, 0, output, "")
            if "/app/sub2api" in arguments:
                output = json.dumps(
                    {
                        "schema_version": build.CAPABILITY_OUTPUT_SCHEMA,
                        "capability": "candidatecapture",
                        "status": "available",
                        "provider_check_passed": True,
                        "provider_generate_passed": True,
                        "live_request_count": 0,
                    }
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")
            raise AssertionError(arguments)

        return run

    def test_build_parameters_schema_file_matches_runtime_version(self) -> None:
        schema_path = Path(build.__file__).with_name(
            "codex_upgrade_candidate_build_parameters.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            build.BUILD_PARAMETERS_SCHEMA,
        )

    def test_inventory_replay_detects_content_and_mode_drift(self) -> None:
        parameters = self._validate_parameters()
        receipt = build.build_inventory_receipt(
            parameters,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            source_root=self.source,
            build_tree=self.build_tree,
            docker_context=self.context,
            binary_path=self.binary,
        )
        build.replay_inventory_receipt(
            receipt,
            parameters,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            source_root=self.source,
            build_tree=self.build_tree,
            docker_context=self.context,
            binary_path=self.binary,
        )
        target = self.context / "backend/resources/models.json"
        target.chmod(0o600)
        with self.assertRaisesRegex(build.CandidateBuildError, "装配内容或 mode 漂移"):
            build.replay_inventory_receipt(
                receipt,
                parameters,
                candidate_id=CANDIDATE_ID,
                image_id=IMAGE_ID,
                source_root=self.source,
                build_tree=self.build_tree,
                docker_context=self.context,
                binary_path=self.binary,
            )

    def test_inventory_rejects_symlink_even_when_target_is_regular(self) -> None:
        link = self.build_tree / "linked-resource"
        os.symlink(self.build_tree / "backend/resources/models.json", link)
        with self.assertRaisesRegex(build.CandidateBuildError, "禁止符号链接"):
            build.scan_tree_inventory(self.build_tree)

    def test_inventory_replay_detects_approved_entrypoint_source_drift(self) -> None:
        parameters = self._validate_parameters()
        receipt = build.build_inventory_receipt(
            parameters,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            source_root=self.source,
            build_tree=self.build_tree,
            docker_context=self.context,
            binary_path=self.binary,
        )
        self._write(
            self.source / "deploy/docker-entrypoint.sh",
            "#!/bin/sh\nexit 99\n",
            0o755,
        )
        with self.assertRaisesRegex(build.CandidateBuildError, "批准源码"):
            build.replay_inventory_receipt(
                receipt,
                parameters,
                candidate_id=CANDIDATE_ID,
                image_id=IMAGE_ID,
                source_root=self.source,
                build_tree=self.build_tree,
                docker_context=self.context,
                binary_path=self.binary,
            )

    def test_frontend_provenance_replays_complete_proof_chain(self) -> None:
        parameters = self._validate_parameters()
        receipt = build.build_frontend_provenance(
            parameters,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            source_root=self.source,
            git_commit=COMMIT,
            build_tree=self.build_tree,
            frontend_dist_source=self.dist_source,
        )
        build.replay_frontend_provenance(
            receipt,
            parameters,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            source_root=self.source,
            git_commit=COMMIT,
            build_tree=self.build_tree,
            frontend_dist_source=self.dist_source,
        )
        self._write(self.source / "frontend/pnpm-lock.yaml", "tampered: true\n")
        with self.assertRaisesRegex(build.CandidateBuildError, "builder 收据未绑定"):
            build.replay_frontend_provenance(
                receipt,
                parameters,
                candidate_id=CANDIDATE_ID,
                image_id=IMAGE_ID,
                source_root=self.source,
                git_commit=COMMIT,
                build_tree=self.build_tree,
                frontend_dist_source=self.dist_source,
            )

    def test_non_node20_or_local_builder_requires_exact_approval(self) -> None:
        parameters = copy.deepcopy(self.parameters)
        parameters["frontend"]["node_version"] = "v25.8.2"
        with self.assertRaisesRegex(build.CandidateBuildError, "deviation_approval"):
            self._validate_parameters(parameters)

    def test_image_inspection_and_capability_are_bound_to_same_image(self) -> None:
        parameters = self._validate_parameters()
        runner = self._image_runner()
        inspection = build.build_image_inspection(
            parameters,
            candidate_id=CANDIDATE_ID,
            runtime_image=RUNTIME_IMAGE,
            image_id=IMAGE_ID,
            binary_path=self.binary,
            docker_context=self.context,
            git_commit=COMMIT,
            target_architecture="linux/arm64",
            runner=runner,
        )
        capability = build.build_capability_probe(
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            runner=runner,
        )
        build.validate_image_inspection(inspection, candidate_id=CANDIDATE_ID, image_id=IMAGE_ID)
        build.validate_capability_probe(capability, candidate_id=CANDIDATE_ID, image_id=IMAGE_ID)
        build.replay_image_inspection(
            inspection,
            parameters,
            candidate_id=CANDIDATE_ID,
            runtime_image=RUNTIME_IMAGE,
            image_id=IMAGE_ID,
            binary_path=self.binary,
            docker_context=self.context,
            git_commit=COMMIT,
            target_architecture="linux/arm64",
            runner=runner,
        )
        build.replay_capability_probe(
            capability,
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            runner=runner,
        )
        self.assertEqual(inspection["files"]["/app/sub2api"]["sha256"], build.file_sha256(self.binary))

    def test_declared_labels_do_not_mask_wrong_actual_image_label(self) -> None:
        parameters = self._validate_parameters()
        with self.assertRaisesRegex(build.CandidateBuildError, "OCI 标签"):
            build.build_image_inspection(
                parameters,
                candidate_id=CANDIDATE_ID,
                runtime_image=RUNTIME_IMAGE,
                image_id=IMAGE_ID,
                binary_path=self.binary,
                docker_context=self.context,
                git_commit=COMMIT,
                target_architecture="linux/arm64",
                runner=self._image_runner(wrong_label=True),
            )

    def test_receipt_binary_does_not_mask_wrong_image_binary(self) -> None:
        parameters = self._validate_parameters()
        with self.assertRaisesRegex(build.CandidateBuildError, "镜像内 /app/sub2api"):
            build.build_image_inspection(
                parameters,
                candidate_id=CANDIDATE_ID,
                runtime_image=RUNTIME_IMAGE,
                image_id=IMAGE_ID,
                binary_path=self.binary,
                docker_context=self.context,
                git_commit=COMMIT,
                target_architecture="linux/arm64",
                runner=self._image_runner(wrong_binary=True),
            )

    def test_image_inspection_rejects_entrypoint_arch_script_and_tag_drift(self) -> None:
        parameters = self._validate_parameters()
        cases = {
            "entrypoint": (self._image_runner(wrong_entrypoint=True), "Entrypoint"),
            "architecture": (self._image_runner(wrong_architecture=True), "架构"),
            "script mode": (self._image_runner(wrong_script_mode=True), "0755"),
            "script content": (self._image_runner(wrong_script_hash=True), "入口脚本"),
            "go tag": (self._image_runner(missing_tag=True), "build tags"),
        }
        for name, (runner, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                build.CandidateBuildError,
                message,
            ):
                build.build_image_inspection(
                    parameters,
                    candidate_id=CANDIDATE_ID,
                    runtime_image=RUNTIME_IMAGE,
                    image_id=IMAGE_ID,
                    binary_path=self.binary,
                    docker_context=self.context,
                    git_commit=COMMIT,
                    target_architecture="linux/arm64",
                    runner=runner,
                )

    def test_capability_probe_rejects_image_or_request_count_drift(self) -> None:
        receipt = build.build_capability_probe(
            candidate_id=CANDIDATE_ID,
            image_id=IMAGE_ID,
            runner=self._image_runner(),
        )
        wrong_image = copy.deepcopy(receipt)
        wrong_image["image_id"] = "sha256:" + "9" * 64
        unsigned = dict(wrong_image)
        unsigned.pop("receipt_digest")
        wrong_image["receipt_digest"] = build.digest(unsigned)
        with self.assertRaisesRegex(build.CandidateBuildError, "未绑定当前 image"):
            build.validate_capability_probe(wrong_image, candidate_id=CANDIDATE_ID, image_id=IMAGE_ID)

        live_request = copy.deepcopy(receipt)
        live_request["live_request_count"] = 1
        unsigned = dict(live_request)
        unsigned.pop("receipt_digest")
        live_request["receipt_digest"] = build.digest(unsigned)
        with self.assertRaisesRegex(build.CandidateBuildError, "live request"):
            build.validate_capability_probe(live_request, candidate_id=CANDIDATE_ID, image_id=IMAGE_ID)


if __name__ == "__main__":
    unittest.main()
