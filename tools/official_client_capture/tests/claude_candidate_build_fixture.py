"""Claude 候选构建的合成夹具（Claude 升级控制面设计 B3-04 第三步测试用）。

全部本地、离线：用本机 git 建一个候选源码仓库（基线提交 = Active 目录，候选提交 = VC-3 暂存 Release 纳入仓库 +
门禁映射与计划），按 Codex 严格构建参数合同造 build tree／Docker context／前端 dist／二进制与前端 builder 收据，
手工构造可从 Git 两端复算的 source transition，用 ``codex_upgrade_vc_receipt.finalize`` 签发实现测试收据，并给出
docker／go 命令的替身：进程内执行器 ``runner()``，以及给父监督器子进程用的假 ``docker``／``go`` 可执行文件
（``install_fake_commands``，同一组输出）。不运行 docker、go，不联网。

候选树布局与 Codex 驱动一致：lifecycle 目录 ``docs/egress/lifecycle/claude-<版本去点>-candidate/`` 下放 Catalog stage
（``catalog-stage/``，恰好两个文件）、门禁映射 ``gate-mapping.json`` 与门禁计划 ``gate-plan.json``。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OFFICIAL_INGRESS_CATALOG_RELATIVE = "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json"
IMAGE_ID = "sha256:" + "8" * 64
RUNTIME_IMAGE = "registry.local/sub2api@sha256:" + "7" * 64
BUILD_ID = "claude-build-0001"
GO_VERSION = "go1.27.1"
TARGET_ARCHITECTURE = "linux/arm64"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def _write(path: Path, data: bytes | str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class CandidateBuildFixture:
    """一次候选构建的全部输入；``argv()`` 给出 record-candidate-build 的完整参数。"""

    def __init__(self, root: Path, campaign: Path, candidate_id: str, *, run_main: Callable[[list[str]], tuple[int, Any, str]]) -> None:
        self.root = root
        self.campaign = campaign
        self.candidate_id = candidate_id
        self.run_main = run_main
        self.manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.version = str(self.manifest["target_version"])
        self.compact = self.version.replace(".", "")
        self.source = root / "candidate-source"
        self.build_tree = root / "build-tree"
        self.context = root / "docker-context"
        self.dist_source = root / "dist-source"
        self.binary = root / "artifacts" / "sub2api"
        self.builder_receipt = root / "artifacts" / "frontend-builder.json"
        self.parameters_path = root / "artifacts" / "build-parameters.json"
        self.transition_path = root / "artifacts" / "source-transition.json"
        self.implementation_root = root / "implementation-evidence"
        self.lifecycle_dir = self.source / "docs" / "egress" / "lifecycle" / f"claude-{self.compact}-candidate"
        self.stage_dir = self.lifecycle_dir / "catalog-stage"
        self.gate_dir = self.lifecycle_dir

    # -- 源码仓库 ---------------------------------------------------------------

    def _active_catalog(self) -> dict[str, Any]:
        stage = profile.load_stage_result(self.campaign, self.manifest)
        prepared = profile.load_prepare_result(self.campaign, self.manifest, stage["prepared_revision"])
        path = self.campaign / prepared["prepared"][profile.PREPARED_EXTRA_FILES["active_release_catalog"]]["path"]
        return json.loads(path.read_text(encoding="utf-8"))

    def build_source(self, *, preview: Mapping[str, Any], marker: str | None = None) -> None:
        """基线提交：Active 目录与构建相关源码；候选提交：暂存 Release、更新后的 release-catalog、投影策略、门禁映射与计划。

        ``marker``：在基线里多放一个带标记的源码文件，让另一个候选的源码树与提交不同（r≥2 的 revision seal 要求）。
        """

        self.source.mkdir(parents=True)
        _git(self.source, "init", "-q")
        catalog_path = self.source / candidate.RELEASE_CATALOG_RELATIVE
        _write(catalog_path, _json_bytes(self._active_catalog()))
        # Mac 读侧核对候选实际 Catalog；原件必须先进入基线提交，再参与源码树和构建摘要。
        _write(self.source / OFFICIAL_INGRESS_CATALOG_RELATIVE, (REPOSITORY_ROOT / OFFICIAL_INGRESS_CATALOG_RELATIVE).read_bytes())
        if marker is not None:
            _write(self.source / "backend" / "internal" / "officialegress" / "claude_candidate_marker.go", f"package officialegress\n\n// {marker}\n")
        _write(self.source / "backend" / "go.mod", "module github.com/example/sub2apiplus\n\ngo 1.27\n")
        _write(self.source / "backend" / "go.sum", "example.invalid/mod v1.0.0 h1:fixture=\n")
        _write(self.source / "backend" / "internal" / "officialegress" / "claude_wire_test.go", "package officialegress\n")
        _write(self.source / "frontend" / "package.json", '{"scripts":{"build":"vite build"}}\n')
        _write(self.source / "frontend" / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        _write(self.source / "deploy" / "docker-entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n", 0o755)
        _write(self.source / "deploy" / "container-healthcheck.sh", "#!/bin/sh\nexit 0\n", 0o755)
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "基线：Active 目录")
        self.base_commit = _git(self.source, "rev-parse", "HEAD")
        # 候选：暂存 Release 纳入仓库。
        staged_root = self.campaign / claude_upgrade_release.STAGED_RELEASE_ROOT
        receipt = json.loads((staged_root / claude_upgrade_release.STAGED_RECEIPT_FILENAME).read_text(encoding="utf-8"))
        entry = receipt["release_entry"]
        for item in receipt["inventory"]:
            relative = Path(item["path"])
            data = (staged_root / relative).read_bytes()
            if relative.parts[0] == "catalogdata":
                _write(self.source / "backend" / "internal" / "officialegress" / relative, data)
            elif relative.name == claude_upgrade_release.PROJECTION_POLICY_COPY_FILENAME:
                name = claude_upgrade_release.PROJECTION_POLICY_FILENAME.format(underscored=self.version.replace(".", "_"))
                _write(self.source / "tools" / "official_client_capture" / name, data)
        _write(self.stage_dir / claude_upgrade_release.STAGED_RECEIPT_FILENAME, (staged_root / claude_upgrade_release.STAGED_RECEIPT_FILENAME).read_bytes())
        _write(self.stage_dir / claude_upgrade_release.RELEASE_ENTRY_FILENAME, (staged_root / claude_upgrade_release.RELEASE_ENTRY_FILENAME).read_bytes())
        catalog = self._active_catalog()
        catalog["releases"].append(entry)
        catalog["selectors"]["validation_candidate"] = receipt["validation_candidate_selector"]
        _write(catalog_path, _json_bytes(catalog))
        # 门禁映射（按需求预览逐项绑定）。
        self.mapping_path = _write(self.gate_dir / "gate-mapping.json", _json_bytes(self.mapping_for(preview)))
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "候选：纳入 VC-3 暂存 Release 与门禁映射")
        self.plan_path = self.gate_dir / "gate-plan.json"
        code, result, stderr = self.run_main(
            [
                "plan-candidate-gates", "--campaign-dir", str(self.campaign), "--candidate-id", self.candidate_id,
                "--candidate-source", str(self.source), "--mapping", str(self.mapping_path), "--output", str(self.plan_path),
            ]
        )
        if code != 0:
            raise AssertionError(f"plan-candidate-gates 失败：{stderr}")
        self.gate_plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "候选：门禁执行计划")
        self.commit = _git(self.source, "rev-parse", "HEAD")

    @staticmethod
    def mapping_for(preview: Mapping[str, Any]) -> dict[str, Any]:
        gates = [
            {
                "gate_id": row["gate_id"],
                "test_id": f"test-{row['gate_id']}",
                "working_directory": "backend" if row["kind"] == "affected_rule" else ".",
                "command": ["go", "test", "./internal/officialegress", "-run", f"TestGate{index:02d}"],
                "requirement_sha256": preview["requirement_sha256s"][row["gate_id"]],
            }
            for index, row in enumerate(preview["requirements"]["requirements"])
        ]
        return {"schema_version": vc_artifacts.GATE_MAPPING_SCHEMA, "requirements_sha256": preview["requirements_sha256"], "gates": gates}

    def share_build_of(self, other: "CandidateBuildFixture") -> None:
        """沿用另一个候选的源码提交、构建实物、门禁计划与 source transition，只换候选名（候选名进构建参数与镜像版本标签）：
        源码层与构建层全同的「改名候选」，r≥2 的 revision seal 必须拒绝它。"""

        for name in (
            "source", "base_commit", "commit", "build_tree", "context", "dist_source", "binary", "builder_receipt",
            "lifecycle_dir", "stage_dir", "gate_dir", "mapping_path", "plan_path", "gate_plan", "transition_path",
        ):
            setattr(self, name, getattr(other, name))
        self.parameters = self.build_parameters()
        _write(self.parameters_path, json.dumps(self.parameters, ensure_ascii=False, sort_keys=True) + "\n", 0o600)

    # -- 构建实物（Codex 严格构建合同） ---------------------------------------------

    def build_artifacts(self) -> None:
        for path in (
            self.build_tree / "deploy",
            self.build_tree / "backend" / "resources",
            self.build_tree / "backend" / "internal" / "web" / "dist" / "assets",
            self.context / "deploy",
            self.context / "backend" / "resources",
            self.dist_source / "assets",
        ):
            path.mkdir(parents=True, mode=0o755, exist_ok=True)
        _write(self.build_tree / "Dockerfile.goreleaser", "FROM scratch\n")
        for name in ("docker-entrypoint.sh", "container-healthcheck.sh"):
            data = (self.source / "deploy" / name).read_bytes()
            _write(self.build_tree / "deploy" / name, data, 0o755)
            _write(self.context / "deploy" / name, data, 0o755)
        _write(self.build_tree / "backend" / "resources" / "models.json", "{}\n")
        _write(self.binary, "candidate-binary\n", 0o755)
        _write(self.dist_source / "index.html", "<main>ok</main>\n")
        _write(self.dist_source / "assets" / "app.js", "export default 1\n")
        for path in self.dist_source.rglob("*"):
            if path.is_file():
                _write(self.build_tree / "backend" / "internal" / "web" / "dist" / path.relative_to(self.dist_source), path.read_bytes())
        _write(self.context / "Dockerfile", "FROM scratch\n")
        _write(self.context / "backend" / "resources" / "models.json", "{}\n")
        _write(self.context / "sub2api", self.binary.read_bytes(), 0o755)
        dist = build.scan_tree_inventory(self.dist_source)
        builder = {
            "schema_version": build.FRONTEND_BUILDER_SCHEMA,
            "status": "complete",
            "builder_identity": "github-actions:release-frontend",
            "source_git_commit": self.commit,
            "build_command": ["pnpm", "run", "build"],
            "node_version": "v20.19.4",
            "pnpm_version": "9.15.9",
            "package_manifest_sha256": build.file_sha256(self.source / "frontend" / "package.json"),
            "lockfile_sha256": build.file_sha256(self.source / "frontend" / "pnpm-lock.yaml"),
            "dist_inventory_sha256": dist["inventory_sha256"],
            "live_request_count": 0,
            "built_at_utc": "2026-10-07T00:00:00Z",
        }
        builder["receipt_digest"] = build.digest(builder)
        _write(self.builder_receipt, json.dumps(builder, ensure_ascii=False, sort_keys=True) + "\n", 0o600)
        self.parameters = self.build_parameters()
        _write(self.parameters_path, json.dumps(self.parameters, ensure_ascii=False, sort_keys=True) + "\n", 0o600)

    def build_parameters(self) -> dict[str, Any]:
        def binding(path: Path) -> dict[str, Any]:
            return {"path": str(path.resolve()), "sha256": build.file_sha256(path), "bytes": path.stat().st_size}

        return {
            "schema_version": build.BUILD_PARAMETERS_SCHEMA,
            "candidate_id": self.candidate_id,
            "source": {"root": str(self.source.resolve()), "git_commit": self.commit},
            "build_tree": {"root": str(self.build_tree.resolve()), "git_commit": self.commit, "umask": "0022"},
            "frontend": {
                "source_root": "frontend",
                "package_manifest": "package.json",
                "lockfile": "pnpm-lock.yaml",
                "build_command": ["pnpm", "run", "build"],
                "node_version": "v20.19.4",
                "pnpm_version": "9.15.9",
                "builder": {"kind": "release_pipeline", "identity": "github-actions:release-frontend", "receipt": binding(self.builder_receipt)},
                "dist_source_root": str(self.dist_source.resolve()),
                "dist_build_tree_path": "backend/internal/web/dist",
                "toolchain_policy": {"required_node_major": 20, "deviation_approval": None},
            },
            "go_build": {
                "command": ["go", "build", "-tags=embed,candidatecapture", "./cmd/server"],
                "working_directory": "backend",
                "environment": {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64", "GOFLAGS": "-mod=vendor"},
                "required_tags": ["candidatecapture", "embed"],
            },
            "docker_build": {
                "context_root": str(self.context.resolve()),
                "dockerfile": "Dockerfile",
                "platform": TARGET_ARCHITECTURE,
                "image_id": IMAGE_ID,
                "labels": {
                    "org.opencontainers.image.revision": self.commit,
                    "org.opencontainers.image.version": f"{self.version}-{self.candidate_id}",
                },
                "entrypoint": ["/app/docker-entrypoint.sh"],
                "assembly": [
                    {"context_path": "Dockerfile", "source_kind": "build_tree", "source_path": "Dockerfile.goreleaser"},
                    {"context_path": "backend/resources", "source_kind": "build_tree", "source_path": "backend/resources"},
                    {"context_path": "deploy", "source_kind": "build_tree", "source_path": "deploy"},
                    {"context_path": "sub2api", "source_kind": "binary", "source_path": str(self.binary.resolve())},
                ],
            },
            "binary": binding(self.binary),
        }

    def runner(self) -> Callable[[list[str]], subprocess.CompletedProcess]:
        """docker／go 命令的替身：镜像检查、二进制构建信息、镜像内文件清单与能力探针。"""

        entrypoint = self.context / "deploy" / "docker-entrypoint.sh"
        healthcheck = self.context / "deploy" / "container-healthcheck.sh"

        def run(arguments: Any) -> subprocess.CompletedProcess:
            arguments = list(arguments)
            if arguments[:3] == ["docker", "image", "inspect"]:
                output = json.dumps(
                    [
                        {
                            "Id": IMAGE_ID,
                            "RepoDigests": [RUNTIME_IMAGE],
                            "Os": "linux",
                            "Architecture": "arm64",
                            "Config": {
                                "Entrypoint": ["/app/docker-entrypoint.sh"],
                                "Labels": {
                                    "org.opencontainers.image.revision": self.commit,
                                    "org.opencontainers.image.version": f"{self.version}-{self.candidate_id}",
                                },
                            },
                        }
                    ]
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")
            if arguments[:2] == ["go", "version"]:
                output = (
                    f"{self.binary}: {GO_VERSION}\n\tbuild\t-tags=embed,candidatecapture\n\tbuild\tCGO_ENABLED=0\n\tbuild\tGOARCH=arm64\n"
                    f"\tbuild\tGOOS=linux\n\tbuild\tvcs=git\n\tbuild\tvcs.revision={self.commit}\n\tbuild\tvcs.modified=false\n"
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")
            if "/bin/sh" in arguments:
                rows = [
                    f"/app/docker-entrypoint.sh\t755\t{entrypoint.stat().st_size}\t{build.file_sha256(entrypoint)}",
                    f"/app/container-healthcheck.sh\t755\t{healthcheck.stat().st_size}\t{build.file_sha256(healthcheck)}",
                    f"/app/sub2api\t755\t{self.binary.stat().st_size}\t{build.file_sha256(self.binary)}",
                ]
                return subprocess.CompletedProcess(arguments, 0, "\n".join(rows) + "\n", "")
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
            raise AssertionError(f"未预期的构建命令：{arguments}")

        return run

    def install_fake_commands(self, bin_dir: Path) -> Path:
        """给父监督器子进程用的假 ``docker``／``go``：输出与 ``runner()`` 逐字相同（预先算好写进配置文件）；返回目录。

        把目录放到 ``PATH`` 最前面，子进程里构建复算的缺省执行器（按 ``PATH`` 找 docker／go）就落到它们。
        """

        run = self.runner()
        outputs = {
            "inspect": run(["docker", "image", "inspect", IMAGE_ID]).stdout,
            "go_version": run(["go", "version", "-m", str(self.binary)]).stdout,
            "listing": run(["docker", "run", "/bin/sh"]).stdout,
            "capability": run(["docker", "run", "/app/sub2api"]).stdout,
        }
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = _write(bin_dir / "fake-commands.json", json.dumps(outputs, ensure_ascii=False), 0o600)
        script = (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"config = json.load(open({str(config)!r}, encoding='utf-8'))\n"
            "name = sys.argv[0].rsplit('/', 1)[-1]\n"
            "args = sys.argv[1:]\n"
            "if name == 'go' and args[:1] == ['version']:\n"
            "    sys.stdout.write(config['go_version'])\n"
            "elif name == 'docker' and args[:2] == ['image', 'inspect']:\n"
            "    sys.stdout.write(config['inspect'])\n"
            "elif name == 'docker' and '/bin/sh' in args:\n"
            "    sys.stdout.write(config['listing'])\n"
            "elif name == 'docker' and '/app/sub2api' in args:\n"
            "    sys.stdout.write(config['capability'])\n"
            "else:\n"
            "    sys.stderr.write('未预期的构建命令：%r\\n' % ([name, *args],))\n"
            "    sys.exit(2)\n"
        )
        for name in ("docker", "go"):
            _write(bin_dir / name, script, 0o755)
        return bin_dir

    # -- source transition 与实现测试收据 ---------------------------------------------

    def build_transition(self) -> None:
        """冻结后继收据：一条可从 Git 两端复算的 release-catalog.json 摘要边（基线 → 候选提交）。"""

        relative = candidate.RELEASE_CATALOG_RELATIVE.as_posix()
        before = candidate._git_blob_sha256(self.source, self.base_commit, relative)
        after = candidate._git_blob_sha256(self.source, self.commit, relative)
        transition = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": "2026-10-07T00:00:00Z",
            "base_commit": self.base_commit,
            "current_commit": self.commit,
            "scope": "candidate",
            "mode": "commit",
            "extra_worktree_paths": [],
            "frozen_path_count": 1,
            "frozen_edge_count": 1,
            "changed_path_count": 1,
            "transitions": [
                {
                    "path": relative,
                    "old_path": None,
                    "status": "M",
                    "predecessor_sha256s": [before],
                    "to_sha256": after,
                    "source_receipts": [],
                    "reason": "纳入 VC-3 暂存 Release 并把 validation_candidate 指向它。",
                }
            ],
            "unregistered_path_count": 0,
            "unregistered_paths": [],
            "deleted_frozen_paths": [],
            "required_manual_actions": [],
            "verification": ["make check-egress-spec"],
            "safety": {
                "deployment_performed": False,
                "live_account_used": False,
                "official_egress_profile_changed": False,
                "production_config_changed": False,
                "wire_or_persona_selection_changed": False,
            },
            "result": candidate.FREEZE_SUCCESSOR_RESULT_PASSED,
        }
        transition["identity_sha256"] = candidate._fingerprint(transition)
        _write(self.transition_path, json.dumps(transition, ensure_ascii=False, sort_keys=True) + "\n", 0o600)

    def build_implementation_receipt(self, requirements: Mapping[str, Any], upgrade_id: str) -> Path:
        root = self.implementation_root
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        _write(root / "logs" / "check-egress-spec.log", "exit_code=0\n", 0o600)
        _write(root / "logs" / "implementation.log", "ok\n", 0o600)
        gate = {"exit_code": 0, "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0}
        gates = [{"gate_id": "check-egress-spec", "kind": "public", "command": ["make", "check-egress-spec"], **gate}]
        gates += [
            {"gate_id": row["gate_id"], "kind": "affected", "command": ["go", "test", "./internal/officialegress"], **gate}
            for row in requirements["requirements"]
            if row["kind"] == "affected_rule"
        ]
        gates.sort(key=lambda item: item["gate_id"])
        facts = {
            "schema_version": vc_receipt.FACTS_SCHEMA,
            "kind": "implementation_tests",
            "subject": {
                "upgrade_id": upgrade_id,
                "campaign_id": self.manifest["campaign_id"],
                "campaign_purpose": self.manifest["campaign_purpose"],
                "baseline_version": self.manifest["baseline_version"],
                "target_version": self.version,
                "candidate_id": self.candidate_id,
                "attempt_id": None,
            },
            "assertions": {
                "git_commit": self.commit,
                "source_tree_sha256": claude_upgrade._directory_tree_digest(self.source.resolve()),
                "target_architecture": TARGET_ARCHITECTURE,
                "gates": gates,
            },
            "evidence": [
                {"role": "check_egress_spec", "path": "logs/check-egress-spec.log"},
                {"role": "implementation_tests", "path": "logs/implementation.log"},
            ],
        }
        _write(root / "facts.json", json.dumps(facts, ensure_ascii=False), 0o600)
        vc_receipt.finalize(root, "facts.json", "receipt.json")
        return root / "receipt.json"

    # -- 参数 -------------------------------------------------------------------

    def argv(self, **overrides: str) -> list[str]:
        values = {
            "--campaign-dir": str(self.campaign),
            "--candidate-id": self.candidate_id,
            "--candidate-purpose": str(self.manifest["campaign_purpose"]),
            "--candidate-source": str(self.source),
            "--candidate-binary": str(self.binary),
            "--runtime-image": RUNTIME_IMAGE,
            "--candidate-image-id": IMAGE_ID,
            "--build-id": BUILD_ID,
            "--deployed-version": self.version,
            "--target-architecture": TARGET_ARCHITECTURE,
            "--build-parameters": str(self.parameters_path),
            "--build-tree": str(self.build_tree),
            "--docker-context": str(self.context),
            "--frontend-dist-source": str(self.dist_source),
            "--catalog-stage-dir": str(self.stage_dir),
            "--source-transition": str(self.transition_path),
            "--gate-plan": str(self.plan_path),
            "--implementation-test-root": str(self.implementation_root),
            "--implementation-test-receipt": str(self.implementation_root / "receipt.json"),
        }
        values.update(overrides)
        argv = ["record-candidate-build"]
        for flag, value in values.items():
            argv += [flag, value]
        return argv


def make_writable(path: Path) -> None:
    """测试改动 git 仓库里的文件前恢复写权限（夹具写的普通文件是 0644／0755）。"""

    path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
