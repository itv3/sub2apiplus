#!/usr/bin/env python3
"""Claude 资格门禁材料的配置冻结前准备入口。

preview 只在新的受管 intake 坐标保存可审核范围，输出摘要。prepare
必须明确带该摘要及批准人，重新核对原参数、历史 entry、安装与源码，
再复制真实材料并生成新的配置 v2。两处旧配置、父收据和历史 world
不覆盖；本入口不注册正式交付执行器，不启动守卫、Docker 或采集。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys

sys.dont_write_bytecode = True


def _managed_modules(data_root: Path):
    """在已核固定安装的实际双受管根加载，拒绝另一个 checkout 的模块。"""
    target = Path("/root/arm64-capture-driver")
    if platform.system() != "Linux" or platform.machine().lower() not in {"arm64", "aarch64"} or os.geteuid() != 0:
        raise ValueError("formal_intake_not_ready：必须在 ARM64 受管 root 安装运行")
    # 已装布局没有 tools/arm64_capture_driver 包，安装器从固定顶层取。
    # 主入口本身、安装器与清单均要求 root 私有文件；随后 verify_tree
    # 对完整实际安装清单、权限、owner 和部署绑定做原合同核验。
    import importlib.util, stat
    if target.is_symlink() or target.stat().st_uid != 0 or stat.S_IMODE(target.stat().st_mode) != 0o700:
        raise ValueError("formal_intake_not_ready：驱动根不是 root 私有安装")
    path = target / "install.py"
    if path.is_symlink() or path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("formal_intake_not_ready：安装器不是固定 root 私有源码")
    specification = importlib.util.spec_from_file_location("_qualification_material_installer", path)
    install = importlib.util.module_from_spec(specification)
    # 首个安装器同样直接执行刚实读的固定源码，不消费未核 cache。
    with path.open("rb") as stream:
        install_raw = stream.read()
    exec(compile(install_raw, str(path), "exec", dont_inherit=True), install.__dict__)
    with path.open("rb") as stream:
        if stream.read() != install_raw:
            raise ValueError("formal_intake_not_ready：安装器加载期间漂移")
    sys.modules[specification.name] = install
    manifest = install.load_manifest(target)
    install.verify_tree(target, manifest, check_mode=True, require_root_owner=True)
    install.verify_install(target, data_root, "claude")
    if Path(__file__) != target / "driver/qualification_materials.py":
        raise ValueError("formal_intake_not_ready：来源准备入口不是固定安装原件")
    sys.path.insert(0, str(data_root))
    from tools.official_client_capture import claude_qualification_material_source as source
    from tools.official_client_capture import claude_qualification_material_import as importer
    from tools.official_client_capture import claude_gate_material_worker as reader
    from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
    from tools.official_client_control import canonical
    from tools.official_client_control.store import ControlStore
    for module in (source, importer, reader, executor):
        if Path(module.__file__).parent != data_root / "tools/official_client_capture":
            raise ValueError("formal_intake_not_ready：来源模块不属于实际已部署受管根")
    return source, importer, reader, executor, canonical, ControlStore


def _write_exact(importer, data_root: Path, relative: str, raw: bytes):
    """同字节的 preview 可纯重验；冲突、链接或旧世界不补写。"""
    from tools.official_client_capture.claude_gate_material_worker import _plain_file
    path = data_root / relative
    if path.exists() or path.is_symlink():
        previous, _ = _plain_file(path)
        if previous != raw:
            raise ValueError("来源准备原件已存在不同内容，须先审核新的 intake")
        from tools.official_client_control.canonical import sha256_bytes
        return {"path": relative, "sha256": sha256_bytes(raw), "bytes": len(raw)}
    return importer._write_once(data_root, path, raw)


def prepare(arguments):
    data_root = Path(arguments.data_root)
    source, importer, reader, executor, canonical, ControlStore = _managed_modules(data_root)
    draft_raw, _ = reader._plain_file(Path(arguments.configuration))
    draft = executor.validate_configuration(canonical.parse_json_bytes(draft_raw))
    if draft["source_scope"] != "formal" or draft["schema_version"] != executor.CONFIGURATION_SCHEMA:
        raise ValueError("准备只接受未冻结的正式配置 v1 草稿，不覆盖历史 v2 配置")
    store = ControlStore.open_read_only(data_root / draft["candidate"]["qualification"]["store_relative"])
    identities = reader.compute_gate_reader_identity(data_root,
        formed_context={"qualification_roots": {role: source.qualification_roots(draft[role]["qualification"]) for role in ("candidate", "rollback")}})
    if identities.document["tool_identity"]["policy_version"] < 12:
        raise ValueError("正式材料准备要求已部署后继策略至少12")
    contexts, originals, entries, forms = {}, {}, {}, {}
    for role in ("candidate", "rollback"):
        q = draft[role]["qualification"]
        parent = canonical.parse_json_bytes(reader._plain_file(data_root / q["sealed_result_binding"]["path"])[0])
        # 精确 revision 来源随后由原 VC-5/VC-4/accept 链验证；它不会
        # 单凭 parent 字符串得到根权限，也不读取当前最大 revision。
        from tools.official_client_capture import claude_upgrade_delivery as delivery
        from tools.official_client_capture import claude_upgrade as upgrade
        campaign = data_root / "evidence/campaigns" / q["campaign_id"]
        manifest = upgrade._require_formal_campaign(campaign)
        handoff = {"validation_completed_ref": q["validation_ref"], "acceptance_ref": q["acceptance_ref"],
                   "candidate_evidence_package_ref": parent["store"]["candidate_evidence_package_ref"],
                   "candidate_image_digest": store.load_fact(q["candidate_ref"])["payload"]["image_digest"]}
        entries[role] = delivery._vc5_entry_replay(campaign, manifest, q["candidate_id"], parent["candidate_revision"], handoff)
        forms[role] = {"candidate_revision": parent["candidate_revision"], "evaluation_baseline": parent["evaluation_baseline"]}
        contexts[role], originals[role] = source.managed_source_context(data_root, Path(getattr(arguments, role + "_env")), q, entries[role], formed_context=forms[role])
    preview = {"schema_version": "claude-qualification-intake-preview/v1", "configuration_descriptor": source._descriptor(Path(arguments.configuration)),
        "contexts": contexts, "tool_identity": identities.document["tool_identity"], "runtime_manifest": identities.document["runtime_manifest"]}
    preview_sha = canonical.canonical_sha256(preview)
    if arguments.mode == "preview":
        binding = _write_exact(importer, data_root, f"control/qualification-intake/previews/{preview_sha}.json", canonical.canonical_json_bytes(preview))
        return {"status": "review_required", "preview_sha256": preview_sha, "preview_binding": binding}
    if arguments.approve_sha256 != preview_sha or not arguments.approved_by or not arguments.approved_by.strip():
        raise ValueError("没有批准当前完整来源范围摘要，零复制拒绝")
    preview_binding_path = data_root / f"control/qualification-intake/previews/{preview_sha}.json"
    if canonical.parse_json_bytes(reader._plain_file(preview_binding_path)[0]) != preview:
        raise ValueError("已审核 preview 原件与本次重算结果不同")
    sources = []
    for role in ("candidate", "rollback"):
        context = contexts[role]
        context_sha = canonical.canonical_sha256(context)
        context_binding = _write_exact(importer, data_root, f"control/qualification-intake/{context_sha}/context.json", canonical.canonical_json_bytes(context))
        approval = {"schema_version": "claude-qualification-intake-approval/v1", "context_binding": context_binding,
            "context_sha256": context_sha, "scope": "read-copy-exact-qualified-layout/v1", "approved_by": arguments.approved_by}
        approval_binding = _write_exact(importer, data_root, f"control/qualification-intake/{context_sha}/approval.json", canonical.canonical_json_bytes(approval))
        # 两份资格形成期不同；实际执行 reader 使用同一次固定准备身份。
        session = {"target": "/root/arm64-capture-driver", "data_root": str(data_root), "parameter_path": str(getattr(arguments, role + "_env")),
            "public_context_binding": context_binding, "approval_binding": approval_binding,
            "runtime_manifest": identities.document["runtime_manifest"], "tool_identity": identities.document["tool_identity"],
            "formed_context": identities.document["formed_context"], "qualification": draft[role]["qualification"], "entry_references": entries[role]}
        intake = source.build_managed_qualification_intake_context(session, context)
        sources.append(source.prepare_qualification_source(store, draft[role]["qualification"], role=role,
            intake_context=intake, entry_references=entries[role]))
    proof = importer.prepare_qualification_material_import(store, sources, managed_data_root=data_root)
    new_configuration = {**draft, "schema_version": executor.MATERIAL_CONFIGURATION_SCHEMA, "qualification_materials_binding": proof}
    executor.validate_configuration(new_configuration)
    staged = _write_exact(importer, data_root, f"control/qualification-staging/{canonical.canonical_sha256(new_configuration)}/configuration.json", canonical.canonical_json_bytes(new_configuration))
    return {"status": "prepared", "configuration_binding": staged, "qualification_materials_binding": proof,
            "formal_executor_registered": False, "runtime_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "prepare"))
    for name in ("data-root", "configuration", "candidate-env", "rollback-env"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--approve-sha256")
    parser.add_argument("--approved-by")
    arguments = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(arguments), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        sys.stderr.write(str(error) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
