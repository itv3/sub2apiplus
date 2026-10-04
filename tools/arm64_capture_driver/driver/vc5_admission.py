#!/usr/bin/env python3
"""VC-5 统一准入：只读预演、显式批准补齐、派发前重放同一准入收据。

不创建采集预约，不切换网关，不发送采集请求。批准绑定当前环境、输入与准确动作集合；
管理 JWT 使用既有签发合同，不添加 iss/sub/aud/jti 或 Campaign 声明。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import importlib
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCHEMA = "codex-vc5-admission/v1"
APPROVAL_SCHEMA = "codex-vc5-admission-approval/v1"
CONTRACT_SCHEMA = "sub2api-admin-jwt/v1"
REQUIRED_CLAIMS = ("user_id", "email", "role", "token_version", "iat", "exp", "nbf")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")


class AdmissionError(ValueError):
    """只包含静态错误码，不能带凭证内容或签发器原始输出。"""


class TokenExpiry(AdmissionError):
    """签名及身份通过、但有效期不足；必须另持明确续签批准。"""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain_path(path: Path) -> None:
    """拒绝整条路径中的符号链接，而非只检查末级文件。"""
    if not path.is_absolute() or ".." in path.parts:
        raise AdmissionError("path_not_absolute_or_contains_parent")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise AdmissionError("path_contains_symlink")


def read_private(path: Path, *, modes: tuple[int, ...] = (0o600,)) -> bytes:
    plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise AdmissionError("file_owner_or_type_invalid")
        if stat.S_IMODE(info.st_mode) not in modes:
            raise AdmissionError("file_mode_invalid")
        if info.st_size > 8 * 1024 * 1024:
            raise AdmissionError("private_file_too_large")
        return handle.read()


@contextlib.contextmanager
def dispatch_lock(path: Path):
    """补齐与派发共用锁；既有锁的属主／模式不合约时直接拒绝。"""
    plain_path(path)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r+b") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AdmissionError("dispatch_lock_owner_mode_or_type_invalid")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AdmissionError("dispatch_or_recovery_already_running") from error
        yield


def read_json(path: Path, *, private: bool = False) -> dict[str, Any]:
    plain_path(path)
    value = json.loads(read_private(path) if private else path.read_bytes())
    if not isinstance(value, dict):
        raise AdmissionError("json_not_object")
    return value


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    plain_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".admission-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical(value) + b"\n")


def read_signing_config(path: Path) -> dict[str, str]:
    """只解释 dotenv 字面量，绝不 source 或执行其中内容。"""
    plain_path(path)
    if not path.is_file() or path.stat().st_uid != os.geteuid():
        raise AdmissionError("signing_config_owner_invalid")
    wanted = {"JWT_SECRET", "JWT_EXPIRE_HOUR", "ADMIN_EMAIL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"(?:export )?([A-Z_][A-Z0-9_]*)=(.*)", line.strip())
        if not match or match[1] not in wanted:
            continue
        key, raw = match.groups()
        if key in values:
            raise AdmissionError("signing_config_duplicate_key")
        words = shlex.split(raw, comments=False)
        if len(words) != 1 or "$" in words[0] or chr(96) in words[0]:
            raise AdmissionError("signing_config_requires_literal_values")
        values[key] = words[0]
    if any(not values.get(key) for key in ("JWT_SECRET", "ADMIN_EMAIL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")):
        raise AdmissionError("signing_config_missing_required_values")
    return values


def verify_token(raw: bytes, secret: str, email: str, minimum: int, *, now: int | None = None) -> dict[str, Any]:
    """验签、身份和生效时间先通过，再检查 TTL；只返回脱敏元数据。"""
    now = int(time.time()) if now is None else now
    if not 0 < len(raw) <= 8192:
        raise AdmissionError("token_size_invalid")
    try:
        token = raw.decode().strip()
        parts = token.split(".")
        if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise AdmissionError("jwt_structure_invalid")
        decoded = [base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)) for part in parts]
        header, claims = json.loads(decoded[0]), json.loads(decoded[1])
    except (ValueError, UnicodeError) as error:
        raise AdmissionError("jwt_structure_invalid") from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AdmissionError("jwt_structure_invalid")
    if header.get("alg") != "HS256" or header.get("typ") not in (None, "JWT") or header.get("crit"):
        raise AdmissionError("jwt_algorithm_not_approved")
    expected = hmac.new(secret.encode(), ".".join(parts[:2]).encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(decoded[2], expected):
        raise AdmissionError("jwt_signature_invalid")
    if any(field not in claims for field in REQUIRED_CLAIMS):
        raise AdmissionError("jwt_required_claim_missing")
    if any(field in claims for field in ("iss", "sub", "aud", "jti")):
        # 当前核验的签发合同没有这些声明；出现新声明必须先登记新合同。
        raise AdmissionError("jwt_contract_has_undeclared_standard_claim")
    for field in ("user_id", "token_version", "iat", "exp", "nbf"):
        if not isinstance(claims[field], int) or isinstance(claims[field], bool):
            raise AdmissionError("jwt_claim_type_invalid")
    if claims["user_id"] <= 0 or claims["token_version"] < 0 or claims["email"] != email or claims["role"] != "admin":
        raise AdmissionError("jwt_admin_identity_invalid")
    if claims["nbf"] > now or claims["iat"] > now or claims["exp"] <= claims["iat"]:
        raise AdmissionError("jwt_time_contract_invalid")
    if claims["exp"] - now < max(1800, minimum):
        raise TokenExpiry("jwt_ttl_insufficient")
    return {
        "credential_sha256": hashlib.sha256(raw).hexdigest(),
        "expires_at_utc": datetime.fromtimestamp(claims["exp"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class ManagedFacts:
    """正式路径只调用现有收据重放器；测试替身不能由命令行或环境启用。"""

    def __init__(self, config: dict[str, str]):
        self.config = config
        sys.path.insert(0, config["D"])
        os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
        self.upgrade = importlib.import_module("tools.official_client_capture.codex_upgrade")

    def catalog(self) -> dict[str, Any]:
        c, cu = self.config, self.upgrade
        campaign = Path(c["D"]) / "evidence/campaigns" / c["NEW"]
        manifest = cu._require_formal_campaign(campaign)
        classification = cu._load_stage_result(campaign, "classify")
        plan = cu._vc_campaign_plan(campaign, manifest)
        _, checkpoint = cu._replay_vc_checkpoint(campaign, plan, "VC-3")
        ref = checkpoint["stage_receipt"]
        receipt_path = Path(ref["path"])
        if not receipt_path.is_absolute():
            receipt_path = campaign / receipt_path
        plain_path(receipt_path)
        if cu.file_sha256(receipt_path) != ref["sha256"]:
            raise AdmissionError("vc3_receipt_binding_invalid")
        stage = read_json(receipt_path)
        cu._verify_catalog_stage_output(receipt_path.parent, stage)
        profile_id, profile_digest = cu._profile_binding_from_manifest(campaign, classification)
        if (stage.get("campaign_id"), stage.get("target_version"), stage.get("profile_id"), stage.get("target_profile_digest")) != (
            c["NEW"], c["TARGET_VERSION"], c["PROFILE_ID"], profile_digest,
        ) or profile_id != c["PROFILE_ID"] or stage.get("classification_sha256") != classification.get("joint_manifest_sha256"):
            raise AdmissionError("vc3_profile_binding_invalid")
        build_path = campaign / "candidates" / c["CAND"] / "build-receipt.json"
        build, binding = cu._replay_candidate_build_receipt(campaign, manifest, c["CAND"], build_path)
        revision, record = cu._current_candidate_revision_record(campaign, manifest)
        if revision is None or (record is not None and record.get("candidate_id") != c["CAND"]):
            raise AdmissionError("active_candidate_revision_invalid")
        cu._replay_vc_checkpoint(campaign, plan, "VC-4", revision=revision)
        args = argparse.Namespace(
            campaign_dir=campaign, candidate_id=c["CAND"], runtime_image=build["image"]["reference"],
            candidate_image_id=build["image"]["image_id"], candidate_source=Path(c["B"]) / "source",
            build_id=build["build"]["build_id"], deployed_version=c["TARGET_VERSION"],
            profile_id=profile_id, profile_digest=profile_digest, candidate_purpose="production_replacement",
        )
        identity = cu._candidate_identity_for_run(args, manifest, classification, verify_image=False)
        identity = cu._bind_candidate_identity_to_build_receipt(args, identity, build, binding)
        if identity["git_commit"] != c["C"]:
            raise AdmissionError("candidate_commit_invalid")
        source_receipt = Path(c["B"]) / "source" / c["LIFECYCLE_DIR"] / "catalog-stage/catalog-stage-receipt.json"
        plain_path(source_receipt)
        if source_receipt.read_bytes() != receipt_path.read_bytes():
            raise AdmissionError("candidate_catalog_receipt_drift")
        relative = f"catalogdata/runtime/profiles/{c['TARGET_VERSION']}/{profile_digest}.json"
        matching = [item for item in stage["inventory"] if item["path"] == relative]
        if len(matching) != 1:
            raise AdmissionError("target_profile_inventory_invalid")
        raw = (receipt_path.parent / relative).read_bytes()
        if json.loads(raw).get("Digest") != profile_digest:
            raise AdmissionError("target_profile_content_digest_invalid")
        # 内容 Digest 和文件 SHA-256 是两项独立合同，不能互相替代。
        return {
            "profile_bytes": raw, "profile_sha256": matching[0]["sha256"], "profile_digest": profile_digest,
            "profile_id": profile_id, "vc3_receipt_sha256": ref["sha256"],
            "build_receipt_sha256": cu.file_sha256(build_path), "build_id": args.build_id,
            "image_id": args.candidate_image_id,
        }

    def state(self) -> dict[str, Any]:
        from background_validation import check, latest_deployment
        c, cu = self.config, self.upgrade
        campaign = Path(c["D"]) / "evidence/campaigns" / c["NEW"]
        manifest = cu._require_formal_campaign(campaign)
        ledger = importlib.import_module("tools.official_client_capture.codex_upgrade_timing_ledger")
        project = importlib.import_module("tools.official_client_capture.codex_upgrade_project_ledger")
        supervisor = importlib.import_module("tools.official_client_capture.codex_upgrade_supervisor")
        summary = ledger.inspect_ledger(cu._campaign_timing_ledger_dir(campaign, manifest))
        if summary.get("status") != "active":
            raise AdmissionError("campaign_ledger_not_active")
        if summary.get("evidence_decision") != c["EVIDENCE_DECISION"]:
            raise AdmissionError("evidence_decision_not_bound_to_ledger")
        project_root = project.find_project_ledger(campaign)
        if project_root is None:
            raise AdmissionError("project_ledger_missing")
        plain_path(project_root)
        # 与现有计时账本只读入口同一口径；不用会刷新 head 缓存的 replay_head。
        project_plan, _ = project._load_plan(project_root)
        head = project._replay(project_root, project_plan, project._load_events(project_root), rebuild_cache=False)
        remaining = head.get("remaining_live_requests")
        if head.get("blocked") or project.root_causes_at_limit_for(head, c["TARGET_VERSION"]):
            raise AdmissionError("project_ledger_blocked")
        if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining <= 0):
            raise AdmissionError("project_request_budget_invalid")
        state_root = Path(os.environ.get("VC_STATE_DIR", f"{c['D']}/control/{c['NEW']}-supervisor"))
        plain_path(state_root)
        if not state_root.is_dir():
            raise AdmissionError("supervisor_state_missing")
        for directory in sorted(state_root.glob("run-*")):
            plain_path(directory)
            state = supervisor._read_state(directory)
            if state.get("state") in supervisor.ACTIVE_STATES:
                raise AdmissionError("supervisor_run_active")
        pid_path = Path(c["RUNROOT"]) / "vc5-run-batch.pid"
        plain_path(pid_path)
        if pid_path.exists():
            text = pid_path.read_text().strip()
            if not text.isdigit() or int(text) <= 0:
                raise AdmissionError("driver_pid_state_unknown")
            try:
                os.kill(int(text), 0)
            except ProcessLookupError:
                pass
            else:
                raise AdmissionError("driver_run_active")
        rc, _ = check(Path(c["RUNROOT"]), Path(c["D"]), require_passed=True)
        if rc:
            raise AdmissionError("background_validation_not_passed")
        return {**latest_deployment(Path(c["D"])), "supervisor_root": str(state_root), "background_validation": "passed"}

    def container(self) -> dict[str, Any]:
        c = self.config
        completed = subprocess.run(["docker", "inspect", "capture-cli"], capture_output=True, text=True, timeout=20)
        if completed.returncode:
            raise AdmissionError("capture_container_unavailable")
        value = json.loads(completed.stdout)[0]
        mounts = [item for item in value["Mounts"] if item.get("Destination") == "/capture"]
        if len(mounts) != 1 or mounts[0].get("Type") != "bind" or Path(mounts[0]["Source"]).resolve() != Path(c["D"]).resolve():
            raise AdmissionError("capture_mount_not_same_data_root")
        if value.get("State", {}).get("Running") is not True:
            raise AdmissionError("capture_container_not_running")
        return {"container_id": value["Id"], "image_id": value["Image"], "mount_destination": "/capture"}

    def container_profile(self, path: Path) -> dict[str, Any] | None:
        relative = path.relative_to(Path(self.config["D"]))
        script = (
            "import hashlib,json,os,stat,sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "parents=[p,*p.parents]; "
            "print(json.dumps(None if not p.exists() else "
            "{'symlink':any(q.is_symlink() for q in parents),'uid':p.stat().st_uid,"
            "'mode':stat.S_IMODE(p.stat().st_mode),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
        )
        result = subprocess.run(["docker", "exec", "capture-cli", "python3", "-B", "-c", script, f"/capture/{relative}"],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AdmissionError("capture_profile_check_failed")
        return json.loads(result.stdout)

    def issue_token(self, signing: dict[str, str]) -> bytes:
        c = self.config
        address = subprocess.run(
            ["docker", "inspect", "sub2apiplus-postgres", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, timeout=20,
        )
        if address.returncode or not re.fullmatch(r"[0-9a-fA-F:.]+", address.stdout.strip()):
            raise AdmissionError("signing_database_address_invalid")
        environment = {
            "PATH": os.environ.get("PATH", ""), "DATABASE_HOST": address.stdout.strip(), "DATABASE_PORT": "5432",
            "DATABASE_USER": signing["POSTGRES_USER"], "DATABASE_PASSWORD": signing["POSTGRES_PASSWORD"],
            "DATABASE_DBNAME": signing["POSTGRES_DB"], "DATABASE_SSLMODE": "disable",
            "JWT_SECRET": signing["JWT_SECRET"], "JWT_EXPIRE_HOUR": signing.get("JWT_EXPIRE_HOUR", "24"),
        }
        completed = subprocess.run([c["JWTGEN_BIN"], "-email", signing["ADMIN_EMAIL"]], cwd=c["COMPOSE_DIR"],
                                   env=environment, capture_output=True, timeout=30)
        # 签发器的原始输出全部留在内存；错误时不向日志复制 stderr 或 token。
        tokens = [line[4:] for line in completed.stdout.splitlines() if line.startswith(b"JWT=")]
        if completed.returncode or len(tokens) != 1:
            raise AdmissionError("token_signing_failed")
        return tokens[0]


class Admission:
    """补齐动作仅修改规定路径；失败恢复原字节／模式并保留失败事件。"""

    def __init__(self, config: dict[str, str], facts: Any):
        self.config, self.facts = config, facts
        self.root = Path(config["RUNROOT"])
        self.profile = Path(config["D"]) / "runtime" / f"codex-profile-{config['TARGET_VERSION']}.json"
        self.token = Path(config["D"]) / "state" / config["UP"] / "admin-token"
        self.receipt = self.root / "vc5-admission.json"
        self.effective = self.root / "vc5-effective-parameters.json"
        self.minimum = max(1800, 21600 + 300, int(config.get("VC5_ADMIN_TOKEN_MIN_SECONDS", "43200")))
        for path in (self.root, self.profile, self.token, self.receipt, self.effective):
            plain_path(path)

    def inspect(self) -> dict[str, Any]:
        c = self.config
        checks: list[dict[str, str]] = []
        values: dict[str, Any] = {}
        for name, operation in (("catalog", self.facts.catalog), ("state", self.facts.state), ("container", self.facts.container)):
            try:
                values[name] = operation()
                checks.append({"check": name, "status": "passed"})
            except Exception as error:
                checks.append({"check": name, "status": "blocked", "reason": str(error) if isinstance(error, AdmissionError) else f"{name}_replay_failed"})
        try:
            signing = read_signing_config(Path(c["COMPOSE_DIR"]) / ".env")
            signer = Path(c["JWTGEN_BIN"])
            plain_path(signer)
            if not signer.is_file() or signer.stat().st_uid != os.geteuid():
                raise AdmissionError("signer_owner_or_type_invalid")
            if not os.access(signer, os.X_OK) or signer.stat().st_mode & 0o022:
                raise AdmissionError("signer_permissions_invalid")
            contract = {
                "schema_version": CONTRACT_SCHEMA, "allowed_algorithms": ["HS256"], "required_claims": list(REQUIRED_CLAIMS),
                "signer_sha256": hashlib.sha256(signer.read_bytes()).hexdigest(),
                "signing_config_sha256": hashlib.sha256((Path(c["COMPOSE_DIR"]) / ".env").read_bytes()).hexdigest(),
                "credential_owner_uid": os.geteuid(), "credential_mode": "0600",
            }
            values["signing"], values["contract"] = signing, contract
            checks.append({"check": "signing_contract", "status": "passed"})
        except Exception as error:
            checks.append({"check": "signing_contract", "status": "blocked",
                           "reason": str(error) if isinstance(error, AdmissionError) else "signing_contract_unavailable"})
        actions: list[str] = []
        if "catalog" in values:
            catalog = values["catalog"]
            try:
                plain_path(self.profile)
                current = None if not self.profile.exists() else read_private(self.profile)
                if current != catalog["profile_bytes"]:
                    actions.append("install_profile")
                container = self.facts.container_profile(self.profile)
                if container is not None and (container["symlink"] or container["uid"] != os.geteuid() or container["mode"] != 0o600):
                    raise AdmissionError("capture_profile_owner_mode_or_link_invalid")
                if current == catalog["profile_bytes"] and (container is None or container["sha256"] != catalog["profile_sha256"]):
                    raise AdmissionError("host_container_profile_drift")
                checks.append({"check": "runtime_profile", "status": "needs_apply" if "install_profile" in actions else "passed"})
            except Exception as error:
                checks.append({"check": "runtime_profile", "status": "blocked",
                               "reason": str(error) if isinstance(error, AdmissionError) else "runtime_profile_unreadable"})
        credential = None
        if "signing" in values:
            try:
                plain_path(self.token)
                if not self.token.exists():
                    actions.append("issue_token")
                else:
                    raw = read_private(self.token, modes=(0o400, 0o600))
                    credential = verify_token(raw, values["signing"]["JWT_SECRET"], values["signing"]["ADMIN_EMAIL"], self.minimum)
                    if stat.S_IMODE(self.token.stat().st_mode) != 0o600:
                        actions.append("normalize_token_mode")
                checks.append({"check": "admin_token", "status": "needs_apply" if any(x.endswith("token") or x == "normalize_token_mode" for x in actions) else "passed"})
            except TokenExpiry:
                actions.append("renew_token")
                checks.append({"check": "admin_token", "status": "needs_apply"})
            except Exception as error:
                checks.append({"check": "admin_token", "status": "blocked",
                               "reason": str(error) if isinstance(error, AdmissionError) else "admin_token_unreadable"})
        blocked = any(row["status"] == "blocked" for row in checks)
        bindings: dict[str, Any] = {}
        effective: dict[str, str] = {}
        if not blocked:
            catalog = values["catalog"]
            official = Path(c["D"]) / "evidence/campaigns" / c["NEW"]
            if c["EVIDENCE_DECISION"] == "reuse":
                official = Path(c["PREDECESSOR_CAMPAIGN"])
                if not official.is_absolute():
                    official = Path(c["D"]) / "evidence/campaigns" / official
                try:
                    plain_path(official)
                    source = self.facts.upgrade._require_formal_campaign(official)
                    if source.get("target_version") != c["TARGET_VERSION"]:
                        raise AdmissionError("official_reuse_target_mismatch")
                    current_manifest = self.facts.upgrade._require_formal_campaign(Path(c["D"]) / "evidence/campaigns" / c["NEW"])
                    predecessor = current_manifest.get("predecessor") or {}
                    if (Path(str(predecessor.get("campaign_dir", ""))) != official
                            or predecessor.get("campaign_id") != source.get("campaign_id")
                            or predecessor.get("campaign_manifest_sha256") != self.facts.upgrade.file_sha256(official / "campaign.json")):
                        raise AdmissionError("official_reuse_not_current_predecessor")
                except Exception:
                    checks.append({"check": "official_campaign", "status": "blocked", "reason": "official_reuse_binding_invalid"})
                    blocked = True
            effective = {"PROFILE_ID": catalog["profile_id"], "PROFILE_DIGEST": catalog["profile_digest"],
                         "OFFICIAL_CAMPAIGN": str(official)}
            environment = {"host": platform.node(), "architecture": platform.machine(), "kernel": platform.release(),
                           "capture_container": values["container"], "deployment": values["state"],
                           "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                           "issuer_contract_sha256": digest(values["contract"])}
            bindings = {"campaign": c["NEW"], "candidate": c["CAND"], "target_profile_digest": catalog["profile_digest"],
                        "environment_fingerprint": digest(environment), "vc3_receipt_sha256": catalog["vc3_receipt_sha256"],
                        "build_receipt_sha256": catalog["build_receipt_sha256"], "build_id": catalog["build_id"],
                        "image_id": catalog["image_id"], "effective_parameters_sha256": digest(effective),
                        "minimum_token_ttl_seconds": self.minimum,
                        "paths_sha256": digest({"profile": str(self.profile), "token": str(self.token), "runroot": str(self.root),
                                                "source": c["B"], "signer": c["JWTGEN_BIN"], "compose": c["COMPOSE_DIR"]})}
        report = {"schema_version": SCHEMA, "checked_at_utc": utc_now(), "checks": checks, "bindings": bindings,
                  "admission_key": digest(bindings) if bindings else None, "effective_parameters": effective,
                  "credential": credential, "issuer_contract": values.get("contract"), "actions": sorted(actions),
                  "profile_file_sha256": values.get("catalog", {}).get("profile_sha256"),
                  "environment": environment if bindings else None,
                  "status": "blocked" if blocked else ("needs_apply" if actions else "ready")}
        self.values, self.report = values, report
        return report

    def consume(self) -> dict[str, Any]:
        report = self.inspect()
        if report["status"] != "ready":
            raise AdmissionError("admission_not_ready")
        receipt = read_json(self.receipt, private=True)
        unsigned = dict(receipt)
        receipt_digest = unsigned.pop("receipt_sha256", None)
        if digest(unsigned) != receipt_digest or receipt.get("schema_version") != SCHEMA:
            raise AdmissionError("admission_receipt_integrity_invalid")
        if receipt.get("bindings") != report["bindings"] or receipt.get("credential") != report["credential"]:
            raise AdmissionError("admission_receipt_binding_drift")
        effective = read_json(self.effective, private=True)
        if effective != report["effective_parameters"] or digest(effective) != report["bindings"]["effective_parameters_sha256"]:
            raise AdmissionError("effective_parameters_drift")
        approval_digest = receipt.get("approval_sha256")
        if not isinstance(approval_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_digest):
            raise AdmissionError("admission_approval_reference_invalid")
        event = read_json(self.root / "vc5-admission-events" / f"{approval_digest}.json", private=True)
        if (event.get("status") != "passed" or event.get("receipt_sha256") != receipt_digest
                or event.get("approval_sha256") != approval_digest or event.get("admission_key") != report["admission_key"]):
            raise AdmissionError("admission_apply_event_invalid")
        return receipt

    def apply(self, approval_path: Path) -> dict[str, Any]:
        # 已有合法收据时只读复核；不得靠重复 apply 隐式续签。
        with contextlib.suppress(OSError, ValueError):
            return self.consume()
        report = self.inspect()
        if report["status"] == "blocked":
            raise AdmissionError("admission_has_blockers")
        approval_bytes = read_private(approval_path)
        approval = json.loads(approval_bytes)
        if not isinstance(approval, dict) or approval.get("schema_version") != APPROVAL_SCHEMA:
            raise AdmissionError("approval_schema_invalid")
        try:
            approved = datetime.fromisoformat(approval["approved_at_utc"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(approval["expires_at_utc"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if approved.tzinfo is None or expires.tzinfo is None or not approved <= now < expires:
                raise AdmissionError("approval_time_invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise AdmissionError("approval_time_invalid") from error
        if not approval.get("approved_by") or not SAFE_ID.fullmatch(str(approval.get("approval_id", ""))):
            raise AdmissionError("approval_identity_missing")
        if (approval.get("admission_key") != report["admission_key"] or approval.get("actions") != report["actions"]
                or approval.get("seal_admission") is not True):
            raise AdmissionError("approval_binding_or_actions_invalid")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.geteuid() or stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise AdmissionError("runroot_owner_or_mode_invalid")
        lock_path = self.root / "vc5-dispatch.lock"
        with dispatch_lock(lock_path):
            if not approved <= datetime.now(timezone.utc) < expires:
                raise AdmissionError("approval_time_invalid")
            # 获取锁后重算全部事实，不能消费等待锁前的旧状态。
            current = self.inspect()
            if current["admission_key"] != report["admission_key"] or current["actions"] != report["actions"] or current["status"] == "blocked":
                raise AdmissionError("admission_changed_before_apply")
            event_path = self.root / "vc5-admission-events" / f"{hashlib.sha256(approval_bytes).hexdigest()}.json"
            if event_path.exists():
                raise AdmissionError("approval_already_attempted_requires_review")
            event = {"approval_sha256": hashlib.sha256(approval_bytes).hexdigest(), "admission_key": report["admission_key"],
                     "actions": report["actions"], "approval": approval, "started_at_utc": utc_now(), "status": "started"}
            write_json(event_path, event)
            backups: dict[Path, tuple[bytes, int] | None] = {}
            written: dict[Path, bytes] = {}
            try:
                for path in (self.profile, self.token, self.receipt, self.effective):
                    backups[path] = (read_private(path, modes=(0o400, 0o600)), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
                if "install_profile" in report["actions"]:
                    raw = self.values["catalog"]["profile_bytes"]
                    atomic_write(self.profile, raw)
                    written[self.profile] = raw
                if any(action in report["actions"] for action in ("issue_token", "renew_token")):
                    raw = self.facts.issue_token(self.values["signing"])
                    verify_token(raw, self.values["signing"]["JWT_SECRET"], self.values["signing"]["ADMIN_EMAIL"], self.minimum)
                    atomic_write(self.token, raw)
                    written[self.token] = raw
                elif "normalize_token_mode" in report["actions"]:
                    raw = read_private(self.token, modes=(0o400,))
                    atomic_write(self.token, raw)
                    written[self.token] = raw
                final = self.inspect()
                if final["status"] != "ready" or final["bindings"] != report["bindings"]:
                    raise AdmissionError("post_apply_verification_failed")
                receipt = {**final, "approved_by": approval["approved_by"], "approval_sha256": event["approval_sha256"],
                           "applied_at_utc": utc_now(), "applied_actions": report["actions"]}
                receipt["receipt_sha256"] = digest(receipt)
                effective_bytes = canonical(final["effective_parameters"]) + b"\n"
                atomic_write(self.effective, effective_bytes)
                written[self.effective] = effective_bytes
                receipt_bytes = canonical(receipt) + b"\n"
                atomic_write(self.receipt, receipt_bytes)
                written[self.receipt] = receipt_bytes
                event.update(status="passed", completed_at_utc=utc_now(), receipt_sha256=receipt["receipt_sha256"])
                write_json(event_path, event)
                return self.consume()
            except Exception as error:
                restored = True
                for path, ours in reversed(list(written.items())):
                    try:
                        if path.read_bytes() != ours:
                            raise AdmissionError("compensation_detected_foreign_write")
                        old = backups[path]
                        if old is None:
                            path.unlink()
                        else:
                            atomic_write(path, old[0], old[1])
                    except Exception:
                        restored = False
                event.update(status="failed", completed_at_utc=utc_now(), compensation_passed=restored,
                             reason=str(error) if isinstance(error, AdmissionError) else "apply_failed")
                write_json(event_path, event)
                raise AdmissionError("apply_failed_compensated" if restored else "apply_failed_requires_manual_recovery") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--consume", action="store_true")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--export-parameters", action="store_true")
    args = parser.parse_args(argv)
    try:
        from driver_config import load_config
        config = load_config()
        admission = Admission(config, ManagedFacts(config))
        if args.dry_run:
            report = admission.inspect()
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["status"] == "ready" else 3
        if args.apply:
            approval = args.approval or (Path(config["VC5_ADMISSION_APPROVAL"]) if config.get("VC5_ADMISSION_APPROVAL") else None)
            if approval is None:
                raise AdmissionError("explicit_approval_required")
            result = admission.apply(approval)
        else:
            result = admission.consume()
        if args.export_parameters:
            for key, value in result["effective_parameters"].items():
                print(f"export {key}={shlex.quote(value)}")
        else:
            print(json.dumps({"status": "passed", "receipt_sha256": result["receipt_sha256"],
                              "receipt": str(admission.receipt)}, ensure_ascii=False))
        return 0
    except Exception as error:
        # 外部库、签发器或 JSON 解码异常也不能向控制台回显凭证。
        reason = str(error) if isinstance(error, AdmissionError) else "admission_verification_failed"
        print(json.dumps({"status": "blocked", "reason": reason}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
