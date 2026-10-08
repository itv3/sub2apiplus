"""Mac client-run 到既有 Candidate 门禁的适配，以及尚缺 live 能力的关闭边界。

本模块只复用原 Candidate formal_admission、活动 CampaignLease、工具演进登记、
清理时间预算和 RuntimeAdmission 的真实总账重放／reservation_cas。调用者传入的
RuntimeAdmission 不是新权限对象，其 plan 与 Campaign plan 必须与受管原件相等，
读取和 CAS 必须仍在既有项目锁内。

现有接口没有给原 awaiting_receipts attempt 原子占用 Mac 请求上界的操作，也没有
独立 capture 的预约／关闭及真实 ProductRecorder 工厂。reserve 等方法对此显式
失败关闭；不能把剩余额度检查、已有 ready 文件或测试回调当作真实预约／后端。
因此这是实际控制门禁与准确的未完成边界，不是完整 live 服务或现场能力认证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.capturelib.model import ConfigurationError


MISSING_LIVE_CAPABILITIES = (
    "original_attempt_mac_request_budget_reservation",
    "independent_capture_reserve_and_close",
    "registered_product_recorder_factory",
)


class CandidateServicesUnavailable(ConfigurationError):
    """正式 live 能力尚未实现；不补造预约、终态或零请求事实。"""


class CandidateMacClientServices:
    """满足五方法形状，真实预约／后端缺失时在 dispatch-intent 前拒绝。

    admit 与 recheck_dispatch 是可用的控制门禁；其通过不授予启动权。真正的
    reserve 能力完成之前，本类永不返回 recorder，不启动产品，不创建第二本
    账、不重建 attempt、不获取新 lease，也不释放调用者持有的监督器。
    """

    def __init__(self, campaign_dir: Path, runtime_admission: ledger.RuntimeAdmission) -> None:
        self.campaign_dir = Path(campaign_dir)
        self.runtime_admission = runtime_admission

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
        problems = ledger._runtime_admission_problems(plan, head, campaign_plan, now=None)
        problems.extend(ledger._campaign_deadline_problems(self.campaign_dir, plan, head))
        contract.require(not problems, "Mac 候选准入拒绝：" + "；".join(problems))
        return admission

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
        """真实 head CAS 可用；它不是请求上界占用，缺失后续能力必须拒绝。"""
        self.admit(task, attempt_root)
        admission = self.runtime_admission
        head = admission.reservation_cas(
            expected_sequence=admission.head_sequence, expected_head_sha256=admission.head_sha256)
        remaining = head.get("remaining_live_requests")
        contract.require(remaining is None or task["execution"]["request_budget"] <= remaining,
                         "原项目剩余额度不足以覆盖冻结 Mac 请求上界")
        raise CandidateServicesUnavailable(
            "既有 Candidate 缺原 attempt 的 Mac 请求上界占用及独立 capture 预约 API；"
            "head CAS 不产生预算预约，正式 client-run 零启动停止")

    def wait_capture_ready(self, task: Mapping[str, Any]) -> None:
        """不能拿已有 ready 文件代替真实请求预约与独立捕获生命周期。"""
        self._source(contract.validate_task(task))
        raise CandidateServicesUnavailable("真实预算预约和独立 capture ready 等待接线未完成")

    def recorder(self, task: Mapping[str, Any], bundle_root: Path) -> Any:
        """没有静态登记的真实 ProductRecorder 工厂，绝不返回替身启动产品。"""
        self._source(contract.validate_task(task))
        raise CandidateServicesUnavailable("静态来源没有已验收的真实 ProductRecorder 工厂")

    def close_capture(self, task: Mapping[str, Any]) -> None:
        """没有受管关闭／同步 API 时保持预约未决，不补写零请求或已关闭。"""
        self._source(contract.validate_task(task))
        raise CandidateServicesUnavailable("独立 capture 关闭／同步 API 未接线，预约保持未决")

    def recheck_dispatch(self, task: Mapping[str, Any]) -> None:
        """准备耗时后重验原 Candidate 门禁；单独通过不会绕过 reserve 的关闭。"""
        from tools.official_client_capture import claude_upgrade_client_receipts as importer

        root = self.campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
        with importer._client_control_lock(self.campaign_dir):
            self.admit(task, root)
            self._lease(task).check("mac-client:dispatch-recheck")
