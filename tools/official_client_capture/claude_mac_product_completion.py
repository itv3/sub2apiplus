"""真实产品记录器的九方法与完整证据收口。

复用原 StateJournal 和生命周期 WAL；事务的 stage／trash 留在来源包之外，
来源包只保存普通原件和不可变状态快照。完整包必须由现有 v2 reader 重放，
再核原静态来源。缺产品、模块、完整 flow 或独立捕获时只封失败诊断，不写
成功来源清单。fixture 始终保持 regression_fixture，不可授正式资格。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_recipe as recipes
from tools.official_client_capture import claude_mac_product_recorder as base
from tools.official_client_capture.claude_mac_state_journal import StateJournal


class ProductEvidenceRecorder(base.NativeProductRecorder):
    """由已验证静态工厂拥有的完整记录器；构造不启动、不取锁、不写文件。

    只增加现有 ProductRecorder 的工程实现，不修改旧内部 fixture 桥的语义。
    formal 必须是实际 Darwin provider，并在每个阶段重新核真实来源资格。
    """

    def __init__(self, task: Mapping[str, Any], *, bundle_root: Path, services: Any,
                 provider: base.ProductObservationProvider, state_plan: base.RecorderStatePlan,
                 materials_receipt: Path, path_context: Any = None) -> None:
        super().__init__(task, bundle_root=bundle_root, services=services, provider=provider, state_plan=state_plan)
        self._materials = Path(materials_receipt)
        self._path_context = path_context
        self._scratch = self._root.parent / (".recorder-state-" + self._task["task_nonce"])
        self._frozen["schema_version"] = "claude-mac-product-recorder-frozen/v1"
        self._frozen["state_journal_root"] = str(self._scratch)
        if path_context is not None:
            recipes.physical_session_root(self._task, c.read_json(c.bound_file(self._root,
                self._task["execution"]["isolation_recipe"])), self._root, path_context)
            self._frozen["path_mapping"] = copy.deepcopy(path_context.mapping_binding)
            c.bound_file(self._root, self._frozen["path_mapping"])
        if self._scratch.exists():
            self._frozen["state_journal_instance"] = base._directory(self._scratch)

    def _guard(self, task: Mapping[str, Any], bundle_root: Path | None = None) -> None:
        base._require(source.validate_task_v2(task) == self._task
                      and (bundle_root is None or Path(bundle_root) == self._root), "完整原 task 或来源根已变化")
        adapter = c.source_adapter(self._task)
        if adapter is None or adapter is not self._adapter:
            raise base.ProductRecorderUnavailable("产品静态来源未登记或已替换，零启动拒绝")
        for name, args in (("owns_recorder", (self,)), ("owns_services", (self._services,)),
                           ("owns_observation_provider", (self._provider, self._task, self._root))):
            owns = getattr(adapter, name, None)
            base._require(callable(owns) and owns(*args) is True, "记录器或依赖不属于原静态来源工厂")
        if self._task["execution"]["source_scope"] == "formal":
            from tools.official_client_capture.claude_mac_product_provider import DarwinProductObservationProvider
            base._require(type(self._provider) is DarwinProductObservationProvider,
                          "夹具、外部 provider 或类型旗标不能取得 formal 权")
            self._provider._guard(self._task, self._root)
        base._require(base._directory(self._root) == self._root_instance
                      and base._directory(self._state) == self._state_instance, "原件根或私有状态根实例已变化")
        if "state_journal_instance" in self._frozen:
            base._require(base._directory(self._scratch) == self._frozen["state_journal_instance"],
                          "原状态事务根被替换")

    def _preflight(self) -> dict[str, Any]:
        verified = recipes.validate_material_private_launch_plan(self._task, self._plan.launch_plan,
                    bundle_root=self._root, materials_receipt=self._materials, path_context=self._path_context)
        recipe = verified["recipe"]
        expected = {str(self._state / relative) for relative in self._plan.frozen_paths}
        actual_paths = recipes.material_state_paths(self._task, recipe, self._root, self._path_context)
        base._require({str(path) for path in actual_paths} == expected and len(actual_paths) == len(expected)
                      and self._state == recipes.material_state_root(self._task, recipe, self._root, self._path_context)
                      and self._state_instance["dev"] == self._root_instance["dev"],
                      "恢复计划不是隔离 recipe 的准确物理状态全集与认证私有根")
        if self._path_context is not None and self._task["client_id"] == "claude-vscode":
            recipes.verify_bridge_private_user_settings(self._task, bundle_root=self._root,
                materials_receipt=self._materials, path_context=self._path_context)
        # 物料推导只证明固定参数；隔离能力仍由原来源合同消费，而非 false 旗标翻转。
        if self._path_context is None:
            base._require(source.verify_isolation_recipe_v2(self._task, self._root) == recipe,
                          "产品隔离 recipe 尚未通过来源核验")
        else:
            verify = getattr(self._adapter, "verify_isolation_recipe", None)
            base._require(callable(verify), "固定桥缺原产品隔离资格消费端")
            verify(self._task, recipe, self._root)
        return recipe

    def _new_journal(self) -> StateJournal:
        return StateJournal(self._identity, self._scratch, self._state, self._plan.frozen_paths,
                            symlink_targets=self._plan.symlink_targets, limits=self._plan.limits)

    def _copy_state_originals(self) -> None:
        """只复制本 StateJournal 的普通不可变原件，事务节点留在原恢复根。"""
        from tools.official_client_capture.claude_mac_product_authority import read_original
        self._mkdir("state-originals")
        names = ["frozen-state.json", "state-before.bin"]
        if (self._scratch / "state-after.bin").exists():
            names.append("state-after.bin")
        for name in names:
            raw = read_original(self._scratch / name, maximum=self._plan.limits.max_wal_bytes)
            self._create("state-originals/" + name, raw)
        # WAL 仍会追加，按不可变摘要命名每个快照，不覆盖准备阶段的历史字节。
        raw = read_original(self._scratch / "journal.jsonl", maximum=self._plan.limits.max_wal_bytes)
        self._create("state-originals/journal-" + base._sha(raw) + ".jsonl", raw)
        materials = self._scratch / "materials"
        if materials.exists():
            self._mkdir("state-originals/materials")
            for path in sorted(materials.iterdir()):
                base._require(path.is_file() and not path.is_symlink(), "状态 material 不是普通原件")
                self._create("state-originals/materials/" + path.name,
                             read_original(path, maximum=self._plan.limits.max_wal_bytes))

    def prepare_state(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        self._guard(task, bundle_root)
        try:
            if self._task["execution"]["source_scope"] == "formal":
                self._services.recheck_dispatch(self._task)
            base._require(self._journal is None and self._optional(base._WAL) is None,
                          "已有生命周期只能显式恢复，不能重新 prepare")
            if self._path_context is not None and self._task["client_id"] == "claude-vscode":
                recipes.prepare_bridge_private_user_settings(self._task, bundle_root=self._root,
                    materials_receipt=self._materials, path_context=self._path_context)
            self._preflight()
            journal = self._new_journal()
            journal.__enter__()
            self._journal = journal
            self._frozen["state_journal_instance"] = base._directory(self._scratch)
            self._mkdir("diagnostic")
            self._mkdir("fixture-observations")
            self._create(base._FROZEN, c.canonical(self._frozen))
            self._append("prepare_intent", {})
            self._prepared_here = True
            journal.prepare()
            self._copy_state_originals()
            self._create("state-before.bin", self._read("state-originals/state-before.bin"))
            prepared = next(row for row in source._json_lines(self._scratch / "journal.jsonl", "原恢复 WAL")
                            if row["event"] == "prepared")
            self._fixture_event("prepared", prepared["observed_at_utc"])
            self._append("prepared", journal.evidence())
            for operation in self._plan.setup_operations:
                journal.record_intent(operation.operation_id, operation.expected_after)
                journal.apply_intent(operation.operation_id)
        except BaseException as error:
            self._failure("prepare", error)
            self._release()
            raise

    def start_monitoring(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        if self._path_context is not None and self._task["client_id"] == "claude-vscode":
            self._guard(task, bundle_root)
            self._preflight()
        super().start_monitoring(task, bundle_root)
        if self._task["execution"]["source_scope"] == "formal":
            base._require(self._journal is not None, "缺原状态锁，不能授权产品写入 scope")
            self._journal.authorize_product_scope(self)

    def restore_state(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        self._guard(task, bundle_root)
        try:
            base._require(self._journal is not None and self._quiescent,
                          "没有原产品停止事实或活动状态锁，禁止恢复")
            self._append("state_restore_intent", {})
            if self._task["execution"]["source_scope"] == "formal" and any(row["event"] == "product_scope_intent"
                for row in self._journal._rows):
                if self._instance is None:
                    self._journal.abandon_product_scope()
                else:
                    self._journal.observe_product_writes()
            evidence = self._journal.restore()
            self._copy_state_originals()
            self._create("state-after.bin", self._read("state-originals/state-after.bin"))
            self._append("state_restored", evidence)
            if self._instance is not None:
                self._fixture_event("state_restored", base._now())
        except BaseException as error:
            self._failure("restore", error)
            self._release()
            raise

    def _snapshots(self, directory: str) -> list[dict[str, Any]]:
        result = super()._snapshots(directory)
        known = {row["origin_path"] for row in result}
        for path in sorted((self._root / "state-originals").rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self._root).as_posix()
            if relative in known:
                continue
            raw = self._read(relative)
            snapshot = f"{directory}/snapshots/state-{len(result):04d}.bin"
            self._create(snapshot, raw)
            result.append({"origin_path": relative, "snapshot": self._binding(snapshot)})
        base._require(len(result) <= self._plan.limits.max_entries, "状态诊断快照数量超限")
        return result

    def _result_once(self, phase: str, method: str, *args: Any) -> dict[str, Any]:
        """相同封存重入只核原返回和原件；不再次调产品、监测或来源工厂。"""
        rows = self._rows()
        results = [row["payload"] for row in rows if row["event"] == phase + "_result"]
        base._require(len(results) <= 1, "封存阶段出现重复结果")
        if results:
            reply = results[0]
            for reference in reply["raw_files"]:
                self._bound(reference)
            base._require(json.loads(self._bound(reply["raw_files"][0])) ==
                          {key: value for key, value in reply.items() if key != "raw_files"}, "封存旧返回漂移")
            return reply
        base._require(not any(row["event"] == phase + "_intent" for row in rows),
                      "原阶段意图已有但结果未知，禁止自动重发或补造")
        return self._call(phase, method, *args)

    def _manifest(self, status: str) -> dict[str, Any]:
        files = []
        for path in sorted(self._root.rglob("*")):
            base._require(not path.is_symlink(), "完整来源包含链接；事务节点不得进入来源包")
            if path.is_file() and path != self._root / "source-manifest.json":
                files.append(self._binding(path.relative_to(self._root).as_posix()))
            else:
                base._require(path.is_dir() or path == self._root / "source-manifest.json", "完整来源包含特殊文件")
        return {"schema_version": source.BUNDLE_SCHEMA, "task_sha256": self._task["task_sha256"],
                "run_id": self._task["execution"]["producer_run_id"],
                "source_scope": self._task["execution"]["source_scope"], "status": status, "files": files}

    def _product_receipts(self, status: str) -> None:
        """只投影已经存在的真实生命周期；不补独立捕获或场景成功事实。"""
        self._create("client-identity.json", c.canonical({"client_id": self._task["client_id"],
            "artifacts": self._task["client_artifacts"]}))
        journal = self._binding("recorder-journal.jsonl")
        ready = json.loads(self._read("monitor-ready.json"))
        self._create("recorder-execution.json", c.canonical({"recorder_id": self._task["execution"]["recorder_id"],
            "task_sha256": self._task["task_sha256"], "run_id": self._task["execution"]["producer_run_id"], "status": status,
            "deployment": self._task["execution"]["source_deployments"]["mac"],
            "monitor_started_at_utc": ready["started_at_utc"], "journal": journal}))
        self._create("state-restoration.json", c.canonical({"task_sha256": self._task["task_sha256"], "status": "restored",
            "before": self._binding("state-before.bin"), "after": self._binding("state-after.bin"), "journal": journal}))

    def seal_for_bridge(self, task: Mapping[str, Any], bundle_root: Path, *, status: str) -> dict[str, Any]:
        """只预封 Mac 原件；不读 ARM64 capture 根，不发布完整成功来源清单。"""
        try:
            self._guard(task, bundle_root)
            base._require(status in {"complete", "failed", "truncated"}, "预封上游终态非法")
            existing = self._optional("product-preseal.json")
            if existing is not None:
                value = json.loads(existing)
                base._require(value["task_sha256"] == self._task["task_sha256"]
                    and value["task_nonce"] == self._task["task_nonce"], "Mac 预封不属于原任务")
                for reference in value["files"]:
                    self._bound(reference)
                if value["status"] == "complete":
                    verify = getattr(self._adapter, "verify_product_origin", None)
                    base._require(callable(verify), "预封缺固定真实来源重验")
                    verify(self._task, self._root, self._provider, value["origin"])
                return {**value, "preseal": self._binding("product-preseal.json")}
            base._require(self._optional("source-manifest.json") is None, "Mac 桥不得同时存在完整成功来源清单")
            origin, result_status = None, status
            try:
                base._require(self._quiescent and self._optional("state-after.bin") is not None
                    and self._instance is not None, "真实产品停止或独立恢复原件不完整")
                if self._journal is not None:
                    base._require(self._journal.evidence()["status"] == "restored", "原恢复日志仍有未知或失败")
                else:
                    rows = self._rows()
                    base._require(any(row["event"] in {"state_restored", "recovery_complete"} for row in rows),
                                  "历史任务没有原恢复完成证明")
                closed = self._result_once("close", "close_observations", self._task)
                base._require(closed["completeness"]["status"] == "complete" and closed["facts"].get("closed") is True,
                              "产品监测或模块尾部未知")
                lifecycle = source._json_lines(self._root/"recorder-journal.jsonl", "产品生命周期")
                if not any(row["event"] == "closed" for row in lifecycle):
                    self._fixture_event("closed", base._now())
                inventory = self._result_once("inventory", "observation_inventory", self._task, self._root)
                facts = inventory["facts"]
                base._require(inventory["completeness"]["status"] == "complete" and facts.get("files_available") is True
                    and isinstance(facts.get("origin"), dict) and isinstance(facts.get("files"), list)
                    and {"process-session.json", "session-events.jsonl", "source-origin.json"}
                    <= {row["path"] for row in facts["files"]}, "Mac 原件或真实产品来源缺失")
                for reference in facts["files"]:
                    self._bound(reference)
                origin = json.loads(self._read("source-origin.json"))
                base._require(origin == facts["origin"], "Mac 来源返回与原件不符")
                verify = getattr(self._adapter, "verify_product_origin", None)
                base._require(callable(verify), "固定来源没有真实原件重验")
                verify(self._task, self._root, self._provider, origin)
                self._product_receipts(status)
            except (ValueError, OSError) as error:
                self._failure("bridge_preseal", error)
                result_status, origin = "failed", None
            files = self._manifest(result_status)["files"]
            base._require(not any(row["path"] in {"source-manifest.json", "product-preseal.json"} for row in files),
                          "预封库存不得自引用或发布完整来源")
            value = {"schema_version": "claude-mac-product-preseal/v1", "task_sha256": self._task["task_sha256"],
                "task_nonce": self._task["task_nonce"], "status": result_status, "files": files, "origin": origin}
            self._create("product-preseal.json", c.canonical(value))
            return {**value, "preseal": self._binding("product-preseal.json")}
        finally:
            self._release()

    def seal_sources(self, task: Mapping[str, Any], bundle_root: Path, *, status: str) -> None:
        try:
            self._guard(task, bundle_root)
        except BaseException:
            # 来源撤销／替换拒绝不能写入证据，但仍释放本实例已持有的状态锁。
            self._release()
            raise
        try:
            base._require(status in {"complete", "failed", "truncated"}, "上游终态非法")
            existing = self._optional("source-manifest.json")
            if existing is not None:
                base._require(json.loads(existing)["status"] == status, "同一来源不能改写终态")
                verified = source.verify_client_bundle_v2(self._task, self._root, Path(task["execution"]["capture_root"]))
                if self._task["execution"]["source_scope"] == "formal":
                    c.verify_origin(self._task, self._root, Path(task["execution"]["capture_root"]))
                base._require(status != "complete" or verified["status"] == "verified", "当前完整来源重放未通过")
                return
            base._require(self._journal is not None and self._quiescent
                          and self._optional("state-after.bin") is not None and self._instance is not None,
                          "产品启动、停止或状态恢复原件缺失，不能发布完整来源")
            closed = self._result_once("close", "close_observations", self._task)
            base._require(closed["completeness"]["status"] == "complete" and closed["facts"].get("closed") is True,
                          "原生流／模块或尾屏障不完整，不能发布来源清单")
            if not any(row["event"] == "closed" for row in source._json_lines(self._root / "recorder-journal.jsonl", "产品 journal")):
                self._fixture_event("closed", base._now())
            inventory = self._result_once("inventory", "observation_inventory", self._task, self._root)
            facts = inventory["facts"]
            base._require(inventory["completeness"]["status"] == "complete" and facts.get("files_available") is True
                          and isinstance(facts.get("files"), list) and isinstance(facts.get("origin"), dict),
                          "完整产品原件或真实来源证明缺失")
            for reference in facts["files"]:
                self._bound(reference)
            needed = {"process-session.json", "session-events.jsonl", "socket-calls.jsonl", "source-origin.json"}
            base._require(needed <= {row["path"] for row in facts["files"]}, "缺真实进程／会话／socket／来源原件")
            origin = json.loads(self._read("source-origin.json"))
            base._require(origin == facts["origin"], "产品来源返回与实际来源原件不同")
            verify = getattr(self._adapter, "verify_product_origin", None)
            if not callable(verify):
                raise base.ProductRecorderUnavailable("缺静态来源的原件重验，摘要不能授信")
            verify(self._task, self._root, self._provider, origin)
            self._create("client-identity.json", c.canonical({"client_id": self._task["client_id"],
                                                             "artifacts": self._task["client_artifacts"]}))
            journal = self._binding("recorder-journal.jsonl")
            ready = json.loads(self._read("monitor-ready.json"))
            self._create("recorder-execution.json", c.canonical({"recorder_id": task["execution"]["recorder_id"],
                "task_sha256": task["task_sha256"], "run_id": task["execution"]["producer_run_id"], "status": status,
                "deployment": task["execution"]["source_deployments"]["mac"],
                "monitor_started_at_utc": ready["started_at_utc"], "journal": journal}))
            self._create("state-restoration.json", c.canonical({"task_sha256": task["task_sha256"], "status": "restored",
                "before": self._binding("state-before.bin"), "after": self._binding("state-after.bin"), "journal": journal}))
            manifest = self._manifest(status)
            # 先用独立私有投影核完整 reader，未通过的包不能在原根发布成功清单。
            self._validate_projection(manifest)
            if self._task["execution"]["source_scope"] == "formal":
                c.verify_origin(self._task, self._root, Path(task["execution"]["capture_root"]))
            self._create("source-manifest.json", c.canonical(manifest))
        except BaseException as error:
            if self._optional("source-manifest.json") is None:
                self._failure("complete_seal", error)
                # 上游 complete 被拒绝时只封 partial；不把未知改成零请求或成功。
                super().seal_sources(self._task, self._root, status="failed")
                if status in {"failed", "truncated"}:
                    return
            raise
        finally:
            self._release()

    def _validate_projection(self, manifest: Mapping[str, Any]) -> None:
        """一次只读投影核验；不改 task、原材料或独立 capture 根。"""
        import tempfile
        from tools.official_client_capture.claude_mac_product_native import _new_file
        with tempfile.TemporaryDirectory(prefix=".product-source-replay-", dir=self._root.parent) as temporary:
            projection = Path(temporary)
            for binding in manifest["files"]:
                path = projection / binding["path"]
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _new_file(path, self._bound(binding))
            _new_file(projection / "source-manifest.json", c.canonical(manifest))
            verified = source.verify_client_bundle_v2(self._task, projection, Path(self._task["execution"]["capture_root"]))
            base._require(manifest["status"] != "complete" or verified["status"] == "verified",
                          "真实产品／独立捕获／场景／恢复完整重放失败")

    def recover(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
        """显式同 nonce 恢复状态，不派发产品或场景；缺原 owned 句柄即拒绝。"""
        self._guard(task, bundle_root)
        if self._task["execution"]["source_scope"] == "formal":
            return self._recover_formal(task, bundle_root)
        try:
            self._preflight()
            partial = self._verify_partial()
            base._require(partial.get("lifecycle_status") == "complete", "原生命周期未知，不能推断 no-launch")
            rows = self._rows()
            base._require(self._journal is None, "活动 recorder 不能同时 recover")
            journal = self._new_journal()
            journal.__enter__()
            self._journal = journal
            journal.prepare()
            if any(row["event"] == "launch_intent" for row in rows):
                originals = [row["payload"]["facts"]["process_instance"] for row in rows if row["event"] == "launch_result"]
                base._require(bool(originals), "缺完整原产品启动结果，不能回收未知实例")
                reply = self._call("recovery_owned", "recover_owned", self._task)
                base._require(reply["completeness"]["status"] == "complete"
                              and reply["facts"].get("stopped") is True
                              and reply["facts"].get("process_instance") == originals[0], "原 owned 产品仍未停止")
            evidence = journal.recover()
            self._copy_state_originals()
            self._guard(task, bundle_root)
            self._verify_partial()
            return self._recovery_receipt(evidence)
        except BaseException as error:
            self._failure("recover", error)
            raise
        finally:
            self._release()

    def _recover_formal(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
        """过期只恢复现存原 WAL 与目录实例；没有任何 prepare/launch/drive 派发。"""
        try:
            self._preflight()
            rows = self._rows()
            base._require(self._journal is None and self._scratch.exists(), "正式恢复须有现存原状态事务且没有另一活动锁")
            journal = self._new_journal()
            journal.__enter__()
            self._journal = journal
            journal.prepare()
            journal.bind_product_recorder(self)
            launched = [row["payload"] for row in rows if row["event"] == "launch_result"]
            if any(row["event"] == "launch_intent" for row in rows):
                base._require(len(launched) == 1, "启动意图没有唯一原结果，不能猜 no-launch 或回收未知进程")
                reply = self._result_once("recovery_owned", "recover_owned", self._task)
                if any(row["event"] == "recovery_owned_result" for row in rows):
                    # 历史结果可以复用，但新 daemon 的内存不能靠旧返回自行补齐。
                    # 固定 provider 只读重验已签原件，不重发回收或观测操作。
                    self._provider.rehydrate_recovery(self._task, reply)
                self._instance = copy.deepcopy(launched[0]["facts"]["process_instance"])
                base._require(reply["completeness"]["status"] == "complete" and reply["facts"].get("stopped") is True
                    and reply["facts"]["process_instance"] == self._instance, "恢复对象不是原已真实退出产品")
                self._quiescent = True
                if not any(row["event"] == "product_stopped" for row in source._json_lines(
                    self._root/"recorder-journal.jsonl", "原产品生命周期")):
                    self._fixture_event("product_stopped", reply["facts"]["stopped_at_utc"])
                if not any(row["event"] == "product_state_observed" for row in journal._rows):
                    journal.observe_product_writes()
            else:
                self._quiescent = True
                if any(row["event"] == "product_scope_intent" for row in journal._rows):
                    journal.abandon_product_scope()
            evidence = journal.restore()
            self._copy_state_originals()
            self._create("state-after.bin", self._read("state-originals/state-after.bin"))
            if self._instance is not None and not any(row["event"] == "state_restored" for row in source._json_lines(
                self._root/"recorder-journal.jsonl", "原产品生命周期")):
                self._fixture_event("state_restored", base._now())
            value = {"schema_version": "claude-mac-product-recovery/v1", "task_sha256": self._task["task_sha256"],
                "task_nonce": self._task["task_nonce"], "status": evidence["status"], "state_evidence": evidence,
                "launch_allowed": False, "new_request_dispatched": False}
            self._append("recovery_complete", value)
            self._create("diagnostic/product-recovery-"+c.digest(value)+".json", c.canonical(value))
            return value
        except BaseException as error:
            self._failure("recover", error)
            raise
        finally:
            self._release()
