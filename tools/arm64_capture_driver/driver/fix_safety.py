#!/usr/bin/env python3
"""修复轮的打包、人工批准和不可覆盖凭证；不代签批准，不调用生产工具。"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

APPROVAL_SCHEMA = "arm64-fix-operation-approval/v1"
OPERATIONS = {"deploy", "evolution", "deadline-extension", "recovery-approve", "recovery-authorize"}


class SafetyError(ValueError):
    """输入、批准或恢复状态不满足合同。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plain(path):
    path = Path(path)
    if (not path.is_absolute() or ".." in path.parts
            or any(item.is_symlink() for item in (path, *path.parents))):
        raise SafetyError("修复轮路径不得含链接、相对路径或父目录跳转")
    return path


def file_binding(path):
    path = plain(path)
    if not path.is_file():
        raise SafetyError(f"缺少修复轮输入文件：{path}")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return {"path": str(path), "sha256": result.hexdigest()}


def read(path):
    file_binding(path)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SafetyError("修复轮凭证含重复字段")
            value[key] = item
        return value
    value = json.loads(Path(path).read_text(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise SafetyError("修复轮凭证必须为对象")
    return value


def write_once(path, value):
    """只发布本次创建的完整文件；竞争者已写入时逐内容复核，禁止覆盖历史。"""
    path = plain(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        if read(path) != value:
            raise SafetyError("不可覆盖已有修复轮凭证")
        return path
    fd, name = tempfile.mkstemp(prefix=".fix-receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if read(path) != value:
                raise SafetyError("并发修复凭证不一致")
    finally:
        Path(name).unlink()
        sync_directory(path.parent)
    return path


def sync_directory(path):
    """将新增或删除目录项落盘，避免断电后丢失已确认的派发占位。"""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_inherited(fd, path, legacy):
    """锁定父 shell 打开的描述符；锁随父进程退出释放，锁文件始终保留。"""
    path, legacy = plain(path), plain(legacy)
    info, opened = path.stat(), os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
        raise SafetyError("修复轮锁的文件类型、属主或描述符绑定不符")
    # 兼容升级前仍持有目录锁的实例；空 PID 或无法判断的旧锁必须人工核对。
    if legacy.exists():
        pid_file = plain(legacy / "pid")
        pid = pid_file.read_text().strip() if pid_file.is_file() else ""
        if not pid.isdecimal() or int(pid) < 1:
            raise SafetyError("旧修复轮锁身份不明，须先核对旧实例")
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            pass
        else:
            raise BlockingIOError("旧修复轮实例仍在运行")
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def package(params, *, apply):
    """已有 bundle 严格复核；缺失时仅从指定本地提交打包，不下载、不修改 Git 引用。"""
    source, bundle = plain(params["SRC"]), plain(params["BUNDLE"])
    ref = "refs/heads/" + params["BUNDLE_BRANCH"]
    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True,
                              text=True, timeout=300).stdout.strip()
    if not bundle.exists():
        if git("rev-parse", ref) != params["HEAD_COMMIT"]:
            raise SafetyError("打包源分支不是本轮指定提交")
        if not apply:
            return {"action": "package_required", "bundle": str(bundle), "git_commit": params["HEAD_COMMIT"]}
        bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".fix-bundle-", dir=bundle.parent)
        os.close(fd)
        Path(temporary).unlink()
        try:
            git("bundle", "create", temporary, ref)
            Path(temporary).chmod(0o600)
            os.link(temporary, bundle)
        finally:
            Path(temporary).unlink(missing_ok=True)
    git("bundle", "verify", str(bundle))
    heads = dict(line.split(maxsplit=1)[::-1] for line in git("bundle", "list-heads", str(bundle)).splitlines())
    if heads.get(ref) != params["HEAD_COMMIT"]:
        raise SafetyError("bundle 分支未绑定本轮指定提交")
    value = {"schema_version": "arm64-fix-package/v1", "git_commit": params["HEAD_COMMIT"], "branch": params["BUNDLE_BRANCH"],
             "files": {key: file_binding(params[key]) for key in ("BUNDLE", "EXPECT", "ENTRY_GREPS")}}
    path = Path(params["OUT"]) / "packages" / (digest(value) + ".json")
    if apply:
        write_once(path, value)
    return {"action": "verified", "package_sha256": digest(value), "receipt": str(path), "package": value}


def approval_binding(params, operation, subject):
    if operation not in OPERATIONS or not isinstance(subject, str) or not subject:
        raise SafetyError("人工批准的操作或待批准对象非法")
    return {"operation": operation, "subject": subject,
            "parameters": {key: value for key, value in params.items()
                           if key not in {"PARAMS_PATH", "PARAMS_SHA256", "APPROVALS_DIR"}},
            "files": {key: file_binding(params[key]) for key in ("VC_ENV", "BUNDLE", "EXPECT", "ENTRY_GREPS")}}


def approval_directory(params):
    return plain(params.get("APPROVALS_DIR", str(Path(params["RUNROOT"]) / "fix-and-continue-approvals" / params["ROUND"])))


def require_approval(params, operation, subject, *, now=None):
    """只消费已由人提交的精确范围批准；缺失时只生成意向，返回待人工处理。"""
    binding = approval_binding(params, operation, subject)
    key = digest(binding)
    intent = Path(params["OUT"]) / "approval-intents" / (key + ".json")
    write_once(intent, {"schema_version": "arm64-fix-approval-intent/v1", "binding": binding, "review_sha256": key,
                        "required_approval_path": str(approval_directory(params) / (key + ".json"))})
    path = approval_directory(params) / (key + ".json")
    if not path.exists():
        raise SafetyError(f"缺少 {operation} 专项人工批准；批准意向：{intent}")
    value = read(path)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SafetyError("人工批准文件须由运行账号拥有且权限为 0600")
    if (set(value) != {"schema_version", "status", "binding", "review_sha256", "approved_by", "approved_at_utc", "expires_at_utc", "proof_ref"}
            or value["schema_version"] != APPROVAL_SCHEMA or value["status"] != "approved"
            or value["binding"] != binding or value["review_sha256"] != key
            or value["approved_by"] != params["APPROVER"] or not str(value["approved_by"]).strip()
            or not isinstance(value["proof_ref"], str) or not value["proof_ref"].strip()):
        raise SafetyError("人工批准的身份、范围、预览摘要或凭证来源不一致")
    def utc(text):
        if not isinstance(text, str) or not text.endswith("Z"):
            raise SafetyError("批准时间必须是以 Z 结尾的 UTC 时间")
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    if not utc(value["approved_at_utc"]) <= (now or datetime.now(timezone.utc)) < utc(value["expires_at_utc"]):
        raise SafetyError("人工批准尚未生效或已过期")
    return {**file_binding(path), "review_sha256": key, "operation": operation,
            "approved_by": value["approved_by"], "proof_ref": value["proof_ref"]}


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="修复轮入口的继承描述符锁")
    parser.add_argument("fd", type=int)
    parser.add_argument("path")
    parser.add_argument("legacy")
    options = parser.parse_args()
    try:
        lock_inherited(options.fd, options.path, options.legacy)
    except BlockingIOError:
        print("本轮已有 fix-and-continue 在运行，拒绝并发", file=sys.stderr)
        raise SystemExit(3)
    except (SafetyError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
