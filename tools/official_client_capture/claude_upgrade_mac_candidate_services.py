"""Mac client-run 的原预算消费与静态来源接线。

本模块只复用原 Candidate formal_admission、活动 CampaignLease、工具演进登记、
清理时间预算和 RuntimeAdmission 的真实总账重放／reservation_cas。调用者传入的
RuntimeAdmission 不是新权限对象，其 plan 与 Campaign plan 必须与受管原件相等，
读取和 CAS 必须仍在既有项目锁内。

预算占用使用已经落地的原 task 持久事件。捕获和产品工厂仍只来自已登记静态
适配器，当前登记为空，因而正式入口继续零启动关闭。本模块不把 fixture 的
NativeCapture、剩余额度检查或已有 ready 文件当作正式后端；关闭不释放预算，
释放由精确 COMMIT 之后的统一账务消费负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_mac_budget as budget
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.capturelib.model import ConfigurationError


MISSING_LIVE_CAPABILITIES = (
    "registered_independent_capture_factory",
    "registered_product_recorder_factory",
)


class CandidateServicesUnavailable(ConfigurationError):
    """正式 live 能力尚未实现；不补造预约、终态或零请求事实。"""


class CandidateMacClientServices:
    """消费原持久预算，静态真实工厂缺失时在占用和 dispatch-intent 前拒绝。

    admit 与 recheck_dispatch 是可用的控制门禁；其通过不授予启动权。真正的
    来源登记与工厂验收完成之前，本类永不返回 recorder，不启动产品，不创建第二本
    账、不重建 attempt、不获取新 lease，也不释放调用者持有的监督器。
    """

    def __init__(self, campaign_dir: Path, runtime_admission: ledger.RuntimeAdmission) -> None:
        self.campaign_dir = Path(campaign_dir)
        self.runtime_admission = runtime_admission
        self._captures: dict[str, tuple[Any, Any]] = {}
        self._closed: set[str] = set()

    def _source(self, task: Mapping[str, Any]) -> Any:
        adapter = contract.SOURCE_ADAPTERS.get(task["execution"]["recorder_id"])
        contract.require(adapter is not None, "真实 Mac 来源未登记，候选服务零启动拒绝")
        owns = getattr(adapter, "owns_services", None)
        contract.require(callable(owns) and owns(self), "候选服务不属于静态登记的真实来源")
        return adapter

    def _lease(self, task: Mapping[str, Any]) -> claude_upgrade.CampaignLease:
        lease = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
        contract.require(isinstance(lease, claude_upgrade.CampaignLease) and lease.acquired,
                         "Mac 运行必须复用已取得的 Candidate Campaign lease")
        contract.require(claude_upgrade._lease_identity_matches(
            lease, self.campaign_dir, phase="candidate", candidate_id=str(task["candidate_id"])),
            "活动 lease 的 Campaign／候选与 Mac task 不一致")
        payload = lease.payload
        contract.require(payload.get("state") == "active" and payload.get("campaign_id") == task["campaign_id"]
                         and payload.get("attempt_id") == task["attempt_id"], "lease 未绑定原候选 attempt")
        contract.require(lease._attached_parent and lease._supervisor is not None,
                         "Mac 运行必须附加现有父监督器，不能另建无人监督的 lease")
        lease.check("mac-client:admission")
        contract.require(contract.timestamp(task["execution"]["request_policy"]["deadline_utc"])
                         <= contract.timestamp(payload["deadline_at_utc"]), "Mac task deadline 超过原监督 lease")
        contract.require(contract.timestamp(claude_upgrade._utc_now())
                         < contract.timestamp(task["execution"]["request_policy"]["deadline_utc"]),
                         "Mac task 的冻结 deadline 已到，不得续算新窗口")
        return lease

    def _runtime(self, task: Mapping[str, Any]) -> ledger.RuntimeAdmission:
        admission = self.runtime_admission
        contract.require(isinstance(admission, ledger.RuntimeAdmission), "必须提供既有 RuntimeAdmission")
        contract.require(ledger._PROJECT_LOCK_DEPTH > 0, "Mac 准入和预约 CAS 必须在既有项目锁内")
        root = ledger.find_project_ledger(self.campaign_dir, client="claude")
        contract.require(root == admission.root == Path(task["execution"]["project_ledger"]),
                         "RuntimeAdmission 与 task 的项目总账不一致")
        plan, _raw = ledger._load_plan(admission.root)
        ledger._require_plan_client(plan, "claude", "Mac 候选服务")
        contract.require(plan == admission.plan, "RuntimeAdmission 的 plan 不是受管原件")
        campaign_plan = ledger._campaign_plan(self.campaign_dir / ledger.CAMPAIGN_LEDGER_DIR_NAME)
        contract.require(campaign_plan == admission.campaign_plan and campaign_plan is not None
                         and campaign_plan.get("campaign_id") == task["campaign_id"],
                         "RuntimeAdmission 的 Campaign plan 不是受管原件")
        ledger._check_fixture_only(admission.root, plan, self.campaign_dir)
        head = admission.refresh()
        receipt = budget.read_task_bound_receipt(admission.root, task)
        reservation_id = None
        if receipt is not None:
            row = budget.verify_bound_receipt(admission.root, receipt, task_sha256=task["task_sha256"], dispatch=True)
            contract.require(row["status"] == "reserved", "原预算已结清，不得重新派发")
            reservation_id = budget.reservation_operation_id(task["task_sha256"])
        problems = ledger._runtime_admission_problems(plan, head, campaign_plan, now=None,
                                                     reservation_operation_id=reservation_id)
        problems.extend(ledger._campaign_deadline_problems(self.campaign_dir, plan, head))
        contract.require(not problems, "Mac 候选准入拒绝：" + "；".join(problems))
        return admission

    def _capture(self, task: Mapping[str, Any], attempt_root: Path, *, create: bool = False) -> Any:
        """受管工厂只构造只读句柄；所有副作用须在真实预算占用之后发生。"""
        adapter = self._source(task)
        required = ("capture_factory", "owns_capture", "verify_capture_handle", "recorder_factory",
                    "owns_recorder", "verify_capture_origin")
        if not all(callable(getattr(adapter, name, None)) for name in required):
            raise CandidateServicesUnavailable("静态来源的捕获／产品工厂或独立核验未完成，预算占用前拒绝")
        saved = self._captures.get(task["task_sha256"])
        if saved is None:
            if not create:
                raise CandidateServicesUnavailable("缺同 task 的受管捕获句柄，不能猜后端或重建 producer")
            handle = adapter.capture_factory(task, attempt_root=attempt_root, services=self)
        else:
            previous_adapter, handle = saved
            contract.require(previous_adapter is adapter, "原捕获句柄的静态来源登记已变化")
        from tools.official_client_capture.claude_upgrade_mac_capture_client import CaptureClient
        from tools.official_client_capture.claude_upgrade_mac_capture_service import CaptureEngine
        contract.require(not isinstance(handle, (CaptureClient, CaptureEngine)),
                         "当前 NativeCapture 仅为 regression_fixture，禁止重标为 formal")
        contract.require(adapter.owns_capture(handle), "捕获句柄不属于受管静态来源")
        contract.require(getattr(handle, "source_scope", None) == "formal"
                         and getattr(handle, "task_sha256", None) == task["task_sha256"]
                         and getattr(handle, "capture_id", None) == task["execution"]["capture_id"]
                         and getattr(handle, "producer_run_id", None) == task["execution"]["producer_run_id"]
                         and getattr(handle, "capture_root", None) == Path(task["execution"]["capture_root"])
                         and getattr(handle, "deployment", None) == task["execution"]["source_deployments"]["capture"],
                         "捕获句柄没有冻结原任务、producer、根和部署")
        contract.require(all(callable(getattr(handle, name, None)) for name in
                             ("reserve", "wait_ready", "close_and_sync", "recover")), "捕获句柄生命周期接口不完整")
        adapter.verify_capture_handle(task, handle, attempt_root)
        self._captures[task["task_sha256"]] = (adapter, handle)
        return handle

    def _attempt_root(self, task: Mapping[str, Any]) -> Path:
        return self.campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]

    def _bound(self, task: Mapping[str, Any], *, dispatch: bool) -> dict[str, Any]:
        receipt = budget.read_task_bound_receipt(self.runtime_admission.root, task)
        contract.require(receipt is not None, "缺原 task 的真实持久预算占用")
        row = budget.verify_bound_receipt(self.runtime_admission.root, receipt,
                                         task_sha256=task["task_sha256"], dispatch=dispatch)
        contract.require(row["status"] == "reserved", "原预算已结清，不能再用于捕获或派发")
        return receipt

    def _reservation(self, task: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
        root = Path(task["execution"]["capture_root"])
        reservation = source.verify_reservation_body_v2(task, root)
        actual = contract.read_json(contract.bound_file(root, reservation["budget_reservation"]))
        contract.require(contract.canonical(actual) == contract.canonical(receipt), "捕获预约没有消费准确的原预算收据")
        contract.require(contract.timestamp(receipt["reserved_at_utc"]) <= contract.timestamp(reservation["reserved_at_utc"]),
                         "捕获预约早于原真实预算占用")
        self._source(task).verify_capture_origin(task, root)
        return reservation

    def admit(self, task: Mapping[str, Any], attempt_root: Path) -> None:
        """在调用者原项目锁→Campaign 锁内复核，不从 Campaign 锁反取项目锁。"""
        task = contract.validate_task(task)
        contract.require(task["execution"]["source_scope"] == "formal", "Mac 候选服务不接受离线夹具用途")
        self._source(task)
        root = Path(attempt_root)
        expected = self.campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
        contract.require(root == expected, "Mac task 必须留在原候选 attempt")
        contract.verify_task_attempt(root, task)
        attempt = contract.read_json(contract.secure_file(root, "attempt.json"))
        contract.require(attempt.get("status") == "awaiting_receipts", "原候选 attempt 已不在等待收据状态")
        lease = self._lease(task)
        self._runtime(task)
        manifest = claude_upgrade._require_formal_campaign(self.campaign_dir)
        contract.require(manifest.get("client") == "claude" and manifest.get("campaign_id") == task["campaign_id"],
                         "Mac 候选服务只消费显式 Claude Formal Campaign")
        claude_upgrade._reject_contaminated_campaign(self.campaign_dir)
        current = claude_upgrade._tool_identity(include_git=False)
        claude_upgrade._require_tool_evolution_registered(
            self.campaign_dir, manifest, current=current, action="Mac candidate client-run")
        candidate_capture.formal_admission(
            self.campaign_dir, manifest, str(task["candidate_id"]), deadline=lease.deadline, heartbeat=lease.heartbeat)
        capture._require_capture_budget(lease.deadline, operation="mac-client:reservation-admission", reservation=True)

    def reserve(self, task: Mapping[str, Any], attempt_root: Path) -> None:
        """原 hold 先持久追加和发布原件，再预约同 producer；不发布 ready。"""
        from tools.official_client_capture import claude_upgrade_client_receipts as importer

        task = contract.validate_task(task)
        self.admit(task, attempt_root)
        handle = self._capture(task, Path(attempt_root), create=True)
        capture_root = Path(task["execution"]["capture_root"])
        contract.require(not any(path.exists() or path.is_symlink() for path in
                                 (capture_root / "capture-manifest.json", capture_root / "undispatched.json",
                                  capture_root / "native-capture-manifest.json")),
                         "原 capture 已有终态或未派发原件，只能同步收账，不得重新预约产品")
        receipt = budget.reserve_task_bound(self.runtime_admission, Path(attempt_root), task)
        run = Path(attempt_root) / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"]
        importer._write(run / "budget-reservation.json", receipt)
        reply = handle.reserve(task, receipt)
        actual = self._reservation(task, receipt)
        contract.require(isinstance(reply, Mapping) and contract.canonical(dict(reply)) == contract.canonical(actual),
                         "捕获 reserve 响应与实际预约原件不符")

    def wait_capture_ready(self, task: Mapping[str, Any]) -> None:
        """原锁内复核真实活动预算，并向原捕获方核当前 listener，不能只读旧 ready。"""
        task = contract.validate_task(task)
        contract.require(ledger._PROJECT_LOCK_DEPTH > 0, "capture ready 必须在原项目锁内核验")
        handle = self._capture(task, self._attempt_root(task))
        receipt = self._bound(task, dispatch=True)
        self._reservation(task, receipt)
        reply = handle.wait_ready(task)
        _reservation, ready = source.verify_reservation_v2(task, Path(task["execution"]["capture_root"]))
        self._source(task).verify_capture_origin(task, Path(task["execution"]["capture_root"]))
        contract.require(isinstance(reply, Mapping) and contract.canonical(dict(reply)) == contract.canonical(ready),
                         "实时 capture ready 响应与独立原件不符")
        self._bound(task, dispatch=True)

    def recorder(self, task: Mapping[str, Any], bundle_root: Path) -> Any:
        """同一静态来源构造 recorder；不得由调用方传入任意工厂。"""
        task = contract.validate_task(task)
        self._capture(task, self._attempt_root(task))
        self._bound(task, dispatch=False)
        expected = self._attempt_root(task) / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"] / "source"
        contract.require(Path(bundle_root) == expected, "recorder 必须留在原 task 的来源根")
        adapter = self._source(task)
        recorder = adapter.recorder_factory(task, bundle_root=Path(bundle_root), services=self)
        contract.require(adapter.owns_recorder(recorder), "recorder 不属于原受管静态来源")
        return recorder

    def close_capture(self, task: Mapping[str, Any]) -> None:
        """认证关闭、排空和同步；到期仍可收口，绝不在这里结清占用。"""
        from tools.official_client_capture import claude_upgrade_client_receipts as importer

        task = contract.validate_task(task)
        with importer._client_control_lock(self.campaign_dir):
            budget.accounting_runtime(self.campaign_dir, task)
            self._bound(task, dispatch=False)
            handle = self._capture(task, self._attempt_root(task))
            root = Path(task["execution"]["capture_root"])
            if task["task_sha256"] not in self._closed:
                reply = handle.close_and_sync(task)
                actual = contract.read_json(contract.secure_file(root, "capture-manifest.json"))
                contract.require(isinstance(reply, Mapping) and contract.canonical(dict(reply)) == contract.canonical(actual),
                                 "capture close 响应与认证同步的真实终态不符")
            self._source(task).verify_capture_origin(task, root)
            source.collect_independent_requests_v2(task, root)
            self._closed.add(task["task_sha256"])

    def recheck_dispatch(self, task: Mapping[str, Any]) -> None:
        """准备耗时后重验原 Candidate 门禁；单独通过不会绕过 reserve 的关闭。"""
        from tools.official_client_capture import claude_upgrade_client_receipts as importer

        root = self.campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
        with importer._client_control_lock(self.campaign_dir):
            self.admit(task, root)
            self._lease(task).check("mac-client:dispatch-recheck")
            self.wait_capture_ready(task)
