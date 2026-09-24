"""测试基座：最小完整 0.154 候选身份事实（老板 2026-09-20 二次拍板：选项 A 受限版）。

让真实评估链的 ``accept`` 走到 VC-5 completion 需要 0.154 起候选身份必须绑定的 VC-4 构建收据
（``record-candidate-build``）及其全部前置绑定：classify 的画像派生与 post-promotion 门禁需求、VC-3
Catalog stage 收据、门禁执行计划、source transition、实现测试收据，以及严格构建收据复算所需的
实物——真实 git 源码树（clean）、build tree／Docker context／dist 三棵互不嵌套的目录、``go build`` 出的
候选二进制（tags＝embed,candidatecapture、vcs.modified=false）、可 ``docker image inspect`` 且带
RepoDigests 的候选镜像（经一次性本地 registry push）、镜像内能力探针。全部由受管构造函数与真实
``plan_candidate_gates``／``record_candidate_build`` 生成，不放宽任何产品校验。

``docker``／``go`` 不可用的机器（开发机）上 ``available()`` 为 False，真实链的 accept 段据此跳过。
本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

GO_CANDIDATES = ("/usr/local/go/bin/go", "/usr/bin/go", "/opt/homebrew/bin/go")
REGISTRY_IMAGE = "registry:2"
BASE_IMAGE = "alpine:3.21"
CAPABILITY_JSON = (
    '{"schema_version":"sub2api-candidate-capture-capability/v1","capability":"candidatecapture",'
    '"status":"available","provider_check_passed":true,"provider_generate_passed":true,"live_request_count":0}'
)
TARGET_ARCHITECTURE = "linux/arm64"


class CandidateIdentityError(RuntimeError):
    pass


def _run(arguments: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None, timeout: float = 600.0) -> subprocess.CompletedProcess:
    completed = subprocess.run(arguments, cwd=str(cwd) if cwd else None, env=dict(env) if env else None, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise CandidateIdentityError(f"{' '.join(arguments[:3])} 失败：{completed.stderr[-1500:]}")
    return completed


def go_binary() -> str | None:
    found = shutil.which("go")
    if found:
        return found
    for candidate in GO_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        completed = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def available() -> bool:
    """docker 守护进程可用且 go 工具链可用，且宿主架构是 linux/arm64（候选合同固定该架构）。"""

    if not docker_available() or go_binary() is None:
        return False
    return os.uname().sysname == "Linux" and os.uname().machine in {"aarch64", "arm64"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _write_json(path: Path, payload: Any, mode: int = 0o644) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(stat.S_IMODE(source.stat().st_mode))


def _copy_tree_files(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(stat.S_IMODE(path.stat().st_mode))
        else:
            _copy_file(path, target)


def _binding(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": _sha(path), "bytes": path.stat().st_size}


def _git(source: Path, *arguments: str) -> str:
    return _run(["git", "-C", str(source), *arguments]).stdout.strip()


class CandidateIdentityFixture:
    """在 ``work`` 下合成候选实物并对已批准分类的 Campaign 执行真实 ``record-candidate-build``。"""

    def __init__(self, work: Path, *, candidate_id: str, target_version: str, baseline_version: str) -> None:
        self.work = Path(work)
        self.candidate_id = candidate_id
        self.target_version = target_version
        self.baseline_version = baseline_version
        self.source = self.work / "candidate-source"
        self.build_tree = self.work / "build-tree"
        self.context = self.work / "docker-context"
        self.dist_source = self.work / "dist-source"
        self.binary = self.work / "candidate-binary" / "sub2api"
        self.builder_receipt_path = self.work / "frontend-builder-receipt.json"
        self.transition_path = self.work / "source-transition.json"
        self.implementation_root = self.work / "implementation-tests"
        self.nonce = secrets.token_hex(4)
        self.registry_name = f"eval-registry-{self.nonce}"
        self.registry_port = 5000 + 1 + int.from_bytes(secrets.token_bytes(2), "big") % 20000
        self.image_tag = f"localhost:{self.registry_port}/sub2api-eval:{candidate_id}-{self.nonce}"
        self.image_id: str | None = None
        self.runtime_image: str | None = None
        self.base_commit: str | None = None
        self.current_commit: str | None = None

    # ---- 源码树（git，两个 commit）----------------------------------------------

    def create_source_tree(self) -> str:
        source = self.source
        (source / "cmd" / "server").mkdir(parents=True)
        (source / "deploy").mkdir()
        (source / "frontend").mkdir()
        (source / "catalog").mkdir()
        (source / "gates").mkdir()
        _write(source / "go.mod", "module evalfixture\n\ngo 1.27\n")
        _write(
            source / "cmd" / "server" / "main.go",
            "package main\n\nimport (\n\t\"fmt\"\n\t\"os\"\n)\n\nfunc main() {\n"
            "\tif len(os.Args) > 1 && os.Args[1] == \"--candidate-capture-capability-probe\" {\n"
            f"\t\tfmt.Println(`{CAPABILITY_JSON}`)\n\t\treturn\n\t}}\n"
            "\tfmt.Println(\"sub2api evaluation fixture\")\n}\n",
        )
        _write(source / "deploy" / "docker-entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n", 0o755)
        _write(source / "deploy" / "container-healthcheck.sh", "#!/bin/sh\nexit 0\n", 0o755)
        _write(source / "frontend" / "package.json", '{"scripts":{"build":"vite build"}}\n')
        _write(source / "frontend" / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        _write(source / "managed.txt", "baseline\n")
        # VC-3 Catalog stage：目录内文件 + 收据（inventory 精确覆盖）。
        _write(source / "catalog" / "runtime-catalog.json", json.dumps({"profile": self.candidate_id, "version": self.target_version}) + "\n")
        inventory = [
            {"path": "runtime-catalog.json", "sha256": _sha(source / "catalog" / "runtime-catalog.json"), "size": (source / "catalog" / "runtime-catalog.json").stat().st_size}
        ]
        from tools.official_client_capture import codex_upgrade

        catalog_receipt = {
            "schema_version": "codex-upgrade-candidate-catalog-stage/v1",
            "candidate_id": self.candidate_id,
            "target_version": self.target_version,
            "inventory": inventory,
            "inventory_sha256": codex_upgrade._fingerprint(inventory),
            "live_request_count": 0,
        }
        _write_json(source / "catalog" / "catalog-stage-receipt.json", catalog_receipt)
        _git(source, "init", "-q")
        _git(source, "config", "user.email", "codex-eval@example.invalid")
        _git(source, "config", "user.name", "Codex Eval Fixture")
        _git(source, "add", "-A")
        _git(source, "commit", "-q", "-m", "baseline")
        self.base_commit = _git(source, "rev-parse", "HEAD")
        return self.base_commit

    @property
    def catalog_receipt_path(self) -> Path:
        return self.source / "catalog" / "catalog-stage-receipt.json"

    def write_gate_mapping(self, requirements: Mapping[str, Any]) -> Path:
        """4 个公共门（affected 为空）的映射：每门唯一 test_id 与字面命令。"""

        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

        gates = []
        for row in requirements["requirements"]:
            gates.append(
                {
                    "gate_id": row["gate_id"],
                    "test_id": f"test-{row['gate_id']}",
                    "working_directory": "backend" if row["gate_id"] == "catalog-projection" else ".",
                    "command": ["python3", "-m", f"gates.{row['gate_id'].replace('-', '_')}"],
                    "requirement_sha256": artifacts.digest(row),
                }
            )
        mapping = {"schema_version": artifacts.GATE_MAPPING_SCHEMA, "requirements_sha256": requirements["requirements_sha256"], "gates": gates}
        path = self.source / "gates" / "mapping.json"
        _write_json(path, mapping)
        return path

    def commit_candidate(self) -> str:
        """第二个 commit：门禁计划已写入源码树、managed.txt 变化（source transition 的摘要边）。"""

        _write(self.source / "managed.txt", "candidate\n")
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "candidate")
        self.current_commit = _git(self.source, "rev-parse", "HEAD")
        return self.current_commit

    # ---- 实物：二进制、build tree、context、dist、镜像 ---------------------------------

    def build_binary(self) -> Path:
        go = go_binary()
        if go is None:
            raise CandidateIdentityError("go 工具链不可用")
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="arm64", GOFLAGS="-mod=vendor", PATH=f"{Path(go).parent}:{os.environ.get('PATH', '')}")
        env.setdefault("HOME", str(self.work))
        _run([go, "build", "-tags=embed,candidatecapture", "-o", str(self.binary), "./cmd/server"], cwd=self.source, env=env)
        self.binary.chmod(0o755)
        return self.binary

    def assemble_trees(self) -> None:
        assert self.current_commit is not None
        dockerfile = (
            f"FROM {BASE_IMAGE}\n"
            "COPY deploy/docker-entrypoint.sh /app/docker-entrypoint.sh\n"
            "COPY deploy/container-healthcheck.sh /app/container-healthcheck.sh\n"
            "COPY sub2api /app/sub2api\n"
            "RUN chmod 755 /app/docker-entrypoint.sh /app/container-healthcheck.sh /app/sub2api\n"
            f"LABEL org.opencontainers.image.revision={self.current_commit} org.opencontainers.image.version={self.version_label()}\n"
            'ENTRYPOINT ["/app/docker-entrypoint.sh"]\n'
        )
        for path in (self.build_tree / "deploy", self.build_tree / "backend" / "resources", self.build_tree / "backend" / "internal" / "web" / "dist" / "assets", self.context / "deploy", self.context / "backend" / "resources", self.dist_source / "assets"):
            path.mkdir(parents=True, exist_ok=True, mode=0o755)
        _write(self.build_tree / "Dockerfile.goreleaser", dockerfile, 0o644)
        _copy_file(self.source / "deploy" / "docker-entrypoint.sh", self.build_tree / "deploy" / "docker-entrypoint.sh")
        _copy_file(self.source / "deploy" / "container-healthcheck.sh", self.build_tree / "deploy" / "container-healthcheck.sh")
        _write(self.build_tree / "backend" / "resources" / "models.json", "{}\n", 0o644)
        _write(self.dist_source / "index.html", "<main>ok</main>\n", 0o644)
        _write(self.dist_source / "assets" / "app.js", "export default 1\n", 0o644)
        _copy_tree_files(self.dist_source, self.build_tree / "backend" / "internal" / "web" / "dist")
        _write(self.context / "Dockerfile", dockerfile, 0o644)
        _copy_file(self.build_tree / "deploy" / "docker-entrypoint.sh", self.context / "deploy" / "docker-entrypoint.sh")
        _copy_file(self.build_tree / "deploy" / "container-healthcheck.sh", self.context / "deploy" / "container-healthcheck.sh")
        _copy_file(self.build_tree / "backend" / "resources" / "models.json", self.context / "backend" / "resources" / "models.json")
        _copy_file(self.binary, self.context / "sub2api")

    def version_label(self) -> str:
        return f"{self.target_version}-eval-{self.candidate_id}"

    def build_image(self) -> tuple[str, str]:
        """构建候选镜像并推到一次性本地 registry，得到带 RepoDigests 的 image_id／runtime_image。"""

        _run(["docker", "run", "-d", "--name", self.registry_name, "-p", f"127.0.0.1:{self.registry_port}:5000", REGISTRY_IMAGE], timeout=120)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.registry_name], capture_output=True, text=True)
            if probe.stdout.strip() == "true":
                break
            time.sleep(0.5)
        _run(["docker", "build", "-q", "-t", self.image_tag, str(self.context)], timeout=600)
        for attempt in range(10):
            pushed = subprocess.run(["docker", "push", "-q", self.image_tag], capture_output=True, text=True, timeout=300)
            if pushed.returncode == 0:
                break
            time.sleep(1.0)
        else:
            raise CandidateIdentityError(f"docker push 失败：{pushed.stderr[-800:]}")
        inspected = json.loads(_run(["docker", "image", "inspect", self.image_tag]).stdout)[0]
        self.image_id = str(inspected["Id"])
        digests = [item for item in inspected.get("RepoDigests", []) if item.startswith(self.image_tag.rsplit(":", 1)[0] + "@sha256:")]
        if not digests:
            raise CandidateIdentityError(f"镜像没有 RepoDigests：{inspected.get('RepoDigests')}")
        self.runtime_image = digests[0]
        return self.image_id, self.runtime_image

    def cleanup_docker(self) -> None:
        cleanup_identity({"registry": self.registry_name, "image_tag": self.image_tag, "runtime_image": self.runtime_image})

    def identity_state(self) -> dict[str, str | None]:
        """供驱动写入链状态、父进程测试结束时清理一次性 registry 与镜像。"""

        return {"registry": self.registry_name, "image_tag": self.image_tag, "runtime_image": self.runtime_image, "image_id": self.image_id}

    # ---- 收据与参数 ---------------------------------------------------------------------

    def write_builder_receipt(self) -> Path:
        from tools.official_client_capture import codex_upgrade_candidate_build as build

        dist = build.scan_tree_inventory(self.dist_source)
        payload = {
            "schema_version": build.FRONTEND_BUILDER_SCHEMA,
            "status": "complete",
            "builder_identity": "github-actions:release-frontend",
            "source_git_commit": self.current_commit,
            "build_command": ["pnpm", "run", "build"],
            "node_version": "v20.19.4",
            "pnpm_version": "9.15.9",
            "package_manifest_sha256": build.file_sha256(self.source / "frontend" / "package.json"),
            "lockfile_sha256": build.file_sha256(self.source / "frontend" / "pnpm-lock.yaml"),
            "dist_inventory_sha256": dist["inventory_sha256"],
            "live_request_count": 0,
            "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        payload["receipt_digest"] = build.digest(payload)
        _write_json(self.builder_receipt_path, payload, 0o600)
        return self.builder_receipt_path

    def build_parameters(self) -> Path:
        from tools.official_client_capture import codex_upgrade_candidate_build as build

        assert self.image_id and self.current_commit
        parameters = {
            "schema_version": build.BUILD_PARAMETERS_SCHEMA,
            "candidate_id": self.candidate_id,
            "source": {"root": str(self.source.resolve()), "git_commit": self.current_commit},
            "build_tree": {"root": str(self.build_tree.resolve()), "git_commit": self.current_commit, "umask": "0022"},
            "frontend": {
                "source_root": "frontend",
                "package_manifest": "package.json",
                "lockfile": "pnpm-lock.yaml",
                "build_command": ["pnpm", "run", "build"],
                "node_version": "v20.19.4",
                "pnpm_version": "9.15.9",
                "builder": {"kind": "release_pipeline", "identity": "github-actions:release-frontend", "receipt": _binding(self.builder_receipt_path)},
                "dist_source_root": str(self.dist_source.resolve()),
                "dist_build_tree_path": "backend/internal/web/dist",
                "toolchain_policy": {"required_node_major": 20, "deviation_approval": None},
            },
            "go_build": {
                "command": ["go", "build", "-tags=embed,candidatecapture", "./cmd/server"],
                "working_directory": ".",
                "environment": {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64", "GOFLAGS": "-mod=vendor"},
                "required_tags": ["candidatecapture", "embed"],
            },
            "docker_build": {
                "context_root": str(self.context.resolve()),
                "dockerfile": "Dockerfile",
                "platform": TARGET_ARCHITECTURE,
                "image_id": self.image_id,
                "labels": {"org.opencontainers.image.revision": self.current_commit, "org.opencontainers.image.version": self.version_label()},
                "entrypoint": ["/app/docker-entrypoint.sh"],
                "assembly": [
                    {"context_path": "Dockerfile", "source_kind": "build_tree", "source_path": "Dockerfile.goreleaser"},
                    {"context_path": "backend/resources", "source_kind": "build_tree", "source_path": "backend/resources"},
                    {"context_path": "deploy", "source_kind": "build_tree", "source_path": "deploy"},
                    {"context_path": "sub2api", "source_kind": "binary", "source_path": str(self.binary.resolve())},
                ],
            },
            "binary": _binding(self.binary),
        }
        path = self.work / "build-parameters.json"
        _write_json(path, parameters, 0o600)
        return path

    def write_source_transition(self) -> Path:
        from tools.official_client_capture import codex_upgrade

        assert self.base_commit and self.current_commit
        before = hashlib.sha256(_run(["git", "-C", str(self.source), "show", f"{self.base_commit}:managed.txt"]).stdout.encode("utf-8")).hexdigest()
        after = _sha(self.source / "managed.txt")
        payload: dict[str, Any] = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "base_commit": self.base_commit,
            "current_commit": self.current_commit,
            "scope": "upstream-codex-0-154-candidate-freeze-successor",
            "mode": "commit",
            "extra_worktree_paths": [],
            "frozen_path_count": 1,
            "frozen_edge_count": 1,
            "changed_path_count": 1,
            "transitions": [
                {
                    "path": "managed.txt",
                    "old_path": "",
                    "status": "M",
                    "predecessor_sha256s": [before],
                    "to_sha256": after,
                    "source_receipts": ["docs/egress/maintenance/baseline.json"],
                    "reason": "登记评估链候选源码后继摘要",
                }
            ],
            "unregistered_path_count": 0,
            "unregistered_paths": [],
            "deleted_frozen_paths": [],
            "required_manual_actions": [],
            "verification": ["make check-egress-spec"],
            "safety": {
                "live_account_used": False,
                "official_egress_profile_changed": False,
                "production_config_changed": False,
                "wire_or_persona_selection_changed": False,
                "deployment_performed": False,
            },
            "result": "passed_local_evidence_successor",
        }
        payload["identity_sha256"] = codex_upgrade._fingerprint(payload)
        _write_json(self.transition_path, payload, 0o600)
        return self.transition_path

    def write_implementation_receipt(self, *, upgrade_id: str, campaign_id: str, campaign_purpose: str, source_tree_sha256: str) -> tuple[Path, Path]:
        from tools.official_client_capture import codex_upgrade_vc_receipt as receipts

        root = self.implementation_root
        root.mkdir(mode=0o700, exist_ok=True)
        (root / "logs").mkdir(mode=0o700, exist_ok=True)
        _write(root / "logs" / "check-egress-spec.log", "passed\n", 0o600)
        _write(root / "logs" / "implementation.log", "passed\n", 0o600)
        gates = [
            {"gate_id": "check-egress-spec", "kind": "public", "command": ["make", "check-egress-spec"], "exit_code": 0, "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0},
        ]
        facts = {
            "schema_version": receipts.FACTS_SCHEMA,
            "kind": "implementation_tests",
            "subject": {
                "upgrade_id": upgrade_id,
                "campaign_id": campaign_id,
                "campaign_purpose": campaign_purpose,
                "baseline_version": self.baseline_version,
                "target_version": self.target_version,
                "candidate_id": self.candidate_id,
                "attempt_id": None,
            },
            "assertions": {"git_commit": self.current_commit, "source_tree_sha256": source_tree_sha256, "target_architecture": TARGET_ARCHITECTURE, "gates": gates},
            "evidence": [
                {"role": "check_egress_spec", "path": "logs/check-egress-spec.log"},
                {"role": "implementation_tests", "path": "logs/implementation.log"},
            ],
        }
        _write_json(root / "facts.json", facts, 0o600)
        receipts.finalize(root, "facts.json", "receipt.json")
        return root, root / "receipt.json"

    # ---- 正式命令 ---------------------------------------------------------------------

    def plan_gates(self, campaign_dir: Path, mapping_path: Path) -> Path:
        from tools.official_client_capture import codex_upgrade

        output = self.source / "gates" / "gate-plan.json"
        codex_upgrade.plan_candidate_gates(
            argparse.Namespace(campaign_dir=campaign_dir, candidate_id=self.candidate_id, candidate_source=self.source.resolve(), mapping=mapping_path.resolve(), output=output)
        )
        output.chmod(0o644)
        return output

    def record_build(self, campaign_dir: Path, manifest: Mapping[str, Any], *, build_parameters: Path, implementation_root: Path, implementation_receipt: Path, build_id: str | None = None) -> dict[str, Any]:
        """以正式入口登记构建；``build_id`` 省略时使用本夹具固定的构建标识。"""

        from tools.official_client_capture import codex_upgrade

        assert self.image_id and self.runtime_image and self.current_commit
        return codex_upgrade.record_candidate_build(
            argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id=self.candidate_id,
                candidate_purpose=manifest["campaign_purpose"],
                deployed_version=manifest["target_version"],
                target_architecture=TARGET_ARCHITECTURE,
                build_id=build_id or f"build-eval-{self.nonce}",
                runtime_image=self.runtime_image,
                candidate_image_id=self.image_id,
                candidate_source=self.source.resolve(),
                candidate_binary=self.binary.resolve(),
                build_parameters=build_parameters,
                build_tree=self.build_tree.resolve(),
                docker_context=self.context.resolve(),
                frontend_dist_source=self.dist_source.resolve(),
                catalog_stage_dir=(self.source / "catalog").resolve(),
                source_transition=self.transition_path.resolve(),
                gate_plan=(self.source / "gates" / "gate-plan.json").resolve(),
                implementation_test_root=implementation_root.resolve(),
                implementation_test_receipt=implementation_receipt.resolve(),
            )
        )


def cleanup_identity(identity: Mapping[str, Any]) -> None:
    """删除一次性 registry 容器与候选镜像（尽力而为，不抛错）。"""

    if identity.get("registry"):
        subprocess.run(["docker", "rm", "-f", str(identity["registry"])], capture_output=True)
    for key in ("image_tag", "runtime_image"):
        if identity.get(key):
            subprocess.run(["docker", "rmi", "-f", str(identity[key])], capture_output=True)


def classification_extras(campaign_dir: Path, approved_root: Path, *, manifest: Mapping[str, Any], migration_path: Path, profile_manifest_path: Path, joint_manifest_sha256: str, migration_reference: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """0.154 分类批准的两份派生制品：画像派生收据（active 副本 + 版本字段 + 空补丁）与 post-promotion 门禁需求。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
    from tools.official_client_capture import incremental_recovery

    target_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
    target_payload = target_manifest["profile_payload"]
    active = json.loads(json.dumps(target_payload))
    active["Version"] = manifest["baseline_version"]
    active.pop("Digest", None)
    active_path = approved_root / "active-profile.json"
    _write_json(active_path, active, 0o600)
    patch_path = approved_root / "profile-rule-patches.json"
    _write_json(
        patch_path,
        {
            "schema_version": "codex-upgrade-profile-rule-patches/v1",
            "baseline_version": manifest["baseline_version"],
            "target_version": manifest["target_version"],
            "active_profile_sha256": _sha(active_path),
            "rule_patches": [],
        },
        0o600,
    )
    derivation = codex_upgrade.validate_profile_derivation(
        active_profile_path=active_path, target_profile_path=profile_manifest_path, migration_path=migration_path, patch_manifest_path=patch_path
    )
    derivation_path = approved_root / "profile-derivation.json"
    _write_json(derivation_path, derivation, 0o600)
    partition = incremental_recovery.canonical_rule_partition(json.loads(migration_path.read_text(encoding="utf-8")))
    requirements = artifacts.build_gate_requirements(
        campaign_id=str(manifest["campaign_id"]),
        target_version=str(manifest["target_version"]),
        joint_manifest_sha256=joint_manifest_sha256,
        affected_rule_ids=partition["affected_rule_ids"],
        inherited_rule_ids=partition["inherited_rule_ids"],
        migration_manifest=migration_reference,
    )
    requirements_path = approved_root / "post-promotion-gate-requirements.json"
    _write_json(requirements_path, requirements, 0o600)
    return {
        "profile_derivation": {"path": derivation_path.relative_to(campaign_dir).as_posix(), "sha256": _sha(derivation_path)},
        "post_promotion_gate_requirements": {"path": requirements_path.relative_to(campaign_dir).as_posix(), "sha256": _sha(requirements_path)},
        "requirements": requirements,  # type: ignore[dict-item]
    }
