package main

import "fmt"

// postBootstrapSinkTransition 描述上游合并后同一发送语义的精确 successor。
//
// 它与 infrastructure transition 不同：successor 可以仍然是业务 persona，
// 例如上游把一个公开 facade 重构为内部 helper。只有指定的旧 candidate 被
// 当前指定的新 candidate 完整替换，且稳定发送语义保持不变时，才能接受这类变化。
type postBootstrapSinkTransition struct {
	name        string
	beforeID    string
	afterID     string
	evidenceRef string
	rationale   string
}

// postBootstrapSinkAddition 描述上游合并后新增、但不进入官方 OAuth Catalog 的
// 发送点。它不能用“按文件全部 out-of-scope”替代，必须精确到 ScanCandidateID，
// 并冻结运行时身份为空、not_applicable 等安全边界。
//
// absentBeforeMerge 为真表示该发送点随尚未合入的上游版本新增：合并前主干不可能出现
// 这些调用点，扫描器又属于合并期间冻结的工具闭集、不能在候选分支里补登记，因此只能
// 在主干预先登记。同一 mergeGroup 的候选合并前必须全部缺失、合并后必须全部出现，
// 只出现一部分即失败关闭；出现后照常按本条冻结的分类与后端边界逐字段校验。
type postBootstrapSinkAddition struct {
	name              string
	candidateID       string
	persona           string
	runtimeSinkID     string
	purpose           string
	endpointEvidence  string
	sinkKind          string
	backend           string
	targetBackend     string
	enforcementState  string
	evidenceRef       string
	rationale         string
	absentBeforeMerge bool
	mergeGroup        string
}

type postBootstrapAcceptance struct {
	acceptedAdded   map[string]struct{}
	acceptedRemoved map[string]struct{}
}

var upstreamScannerSuccessorEvidence = fmt.Sprintf(
	"docs/egress/maintenance/upstream-v%d.%d.%d-scanner-successor-source-transition.json",
	0, 2, 3,
)

// upstreamPendingAdditionsEvidence 是合并前在主干预先登记上游新增发送点的承接收据。
var upstreamPendingAdditionsEvidence = "docs/egress/maintenance/upstream-v0210-scanner-pending-additions-20260930-freeze-successor.json"

// upstreamV0213PendingAdditionsEvidence 是合并 v0213 批次前在主干预先登记新增发送点的承接收据。
var upstreamV0213PendingAdditionsEvidence = "docs/egress/maintenance/upstream-v0213-scanner-pending-additions-20261004-freeze-successor.json"

var reviewedPostBootstrapSinkTransitions = []postBootstrapSinkTransition{
	{
		name:        "upstream-chat-completions-facade-successor",
		beforeID:    "github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.ForwardAsChatCompletions@backend/internal/service/openai_gateway_chat_completions.go#facade_openai_upstream_req#1",
		afterID:     "github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.forwardAsChatCompletions@backend/internal/service/openai_gateway_chat_completions.go#facade_openai_upstream_req#1",
		evidenceRef: upstreamScannerSuccessorEvidence,
		rationale:   "本次上游版本将 Chat Completions 出站实现移入内部 helper；RuntimeSinkID、路由、后端与 AST 指纹保持不变。",
	},
}

var reviewedPostBootstrapSinkAdditions = []postBootstrapSinkAddition{
	{
		name:             "upstream-image-url-b64-backfill",
		candidateID:      "github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.fetchOpenAIImageURLBase64@backend/internal/service/openai_images_b64_backfill.go#facade_http_upstream_do#1",
		persona:          "out-of-scope",
		runtimeSinkID:    "",
		purpose:          "",
		endpointEvidence: "not_applicable",
		sinkKind:         "facade_http_upstream_do",
		backend:          "-",
		targetBackend:    "-",
		enforcementState: "not_applicable",
		evidenceRef:      upstreamScannerSuccessorEvidence,
		rationale:        "本次上游版本新增图片 URL 到 b64_json 的兼容回填下载，不承载官方 OAuth 出站。",
	},
	{
		name:              "upstream-typesafe-content-moderation",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/pkg/typesafe.Evaluate@backend/internal/pkg/typesafe/client.go#net_http_client_do#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "net_http_client_do",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamPendingAdditionsEvidence,
		rationale:         "本次上游同步新增的 TypeSafe 内容审核请求，目标为管理员配置的第三方审核 API，不承载官方 OAuth 出站。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-typesafe-seedance-opencode-go-claude-reset",
	},
	{
		name:              "upstream-seedance-video-forward",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.ForwardSeedance@backend/internal/service/seedance.go#facade_http_upstream_do#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "facade_http_upstream_do",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamPendingAdditionsEvidence,
		rationale:         "本次上游同步新增的 Seedance 视频任务转发，目标为账号 base_url 配置的第三方上游，不承载官方 OAuth 出站。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-typesafe-seedance-opencode-go-claude-reset",
	},
	{
		name:              "upstream-opencode-go-usage-refresh",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/service.*OpenCodeGoUsageService.refreshLoadedAccount@backend/internal/service/opencode_go_usage.go#facade_http_upstream_do#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "facade_http_upstream_do",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamPendingAdditionsEvidence,
		rationale:         "本次上游同步新增的 OpenCode Go 用量查询，目标 opencode.ai，属于第三方 API Key 平台，不承载官方 OAuth 出站。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-typesafe-seedance-opencode-go-claude-reset",
	},
	{
		name:              "upstream-claude-oauth-reset-credits",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/service.NewClaudeResetCreditService@backend/internal/service/claude_reset_credits.go#factory_httpclient_pool#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "factory_httpclient_pool",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamPendingAdditionsEvidence,
		rationale:         "本次上游同步新增的 Claude OAuth 账号重置额度查询与兑换客户端（api.anthropic.com），与既有 Claude 用量查询同属管理端辅助请求，不承载 Claude Code persona 推理；Claude 出站 Inventory 登记为 non_persona_managed。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-typesafe-seedance-opencode-go-claude-reset",
	},
	{
		name:              "upstream-typesafe-account-test",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testTypeSafeAccountConnection@backend/internal/service/account_test_service_typesafe.go#facade_http_upstream_do#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "facade_http_upstream_do",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamV0213PendingAdditionsEvidence,
		rationale:         "v0213 批次上游新增的 TypeSafe 账号连通性测试，向账号 base_url 发最小 System One 请求，属于第三方 API Key 平台，不承载官方 OAuth 出站。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-v0213-typesafe-systemone",
	},
	{
		name:              "upstream-systemone-gateway-forward",
		candidateID:       "github.com/Wei-Shaw/sub2api/internal/service.*GatewayService.ForwardSystemOne@backend/internal/service/gateway_systemone.go#facade_http_upstream_do#1",
		persona:           "out-of-scope",
		runtimeSinkID:     "",
		purpose:           "",
		endpointEvidence:  "not_applicable",
		sinkKind:          "facade_http_upstream_do",
		backend:           "-",
		targetBackend:     "-",
		enforcementState:  "not_applicable",
		evidenceRef:       upstreamV0213PendingAdditionsEvidence,
		rationale:         "v0213 批次上游新增的 System One 网关转发，TypeSafe 账号经账号 base_url 转发请求，属于第三方 API Key 平台，不承载官方 OAuth 出站。",
		absentBeforeMerge: true,
		mergeGroup:        "upstream-v0213-typesafe-systemone",
	},
}

// validateReviewedPostBootstrapSinkAcceptance 只接受明确登记的 upstream successor。
// 任何缺失、并存、语义变化或安全边界变化都会返回问题；调用方不能通过分类规则
// 或 removal receipt 静默绕过这些检查。
func validateReviewedPostBootstrapSinkAcceptance(
	oldByID, currentByID map[string]SinkRecord,
) (postBootstrapAcceptance, []string) {
	accepted := postBootstrapAcceptance{
		acceptedAdded:   make(map[string]struct{}),
		acceptedRemoved: make(map[string]struct{}),
	}
	var problems []string

	for _, transition := range reviewedPostBootstrapSinkTransitions {
		if transition.name == "" || transition.beforeID == "" || transition.afterID == "" ||
			transition.beforeID == transition.afterID || transition.evidenceRef == "" ||
			transition.rationale == "" {
			problems = append(problems, "post-bootstrap sink transition 定义不完整")
			continue
		}
		before, beforeExists := oldByID[transition.beforeID]
		after, afterExists := currentByID[transition.afterID]
		if !beforeExists {
			problems = append(problems, fmt.Sprintf(
				"%s 旧 candidate 不在 bootstrap 基线中: %s", transition.name, transition.beforeID))
			continue
		}
		if !afterExists {
			problems = append(problems, fmt.Sprintf(
				"%s successor candidate 不在当前发送面中: %s", transition.name, transition.afterID))
			continue
		}
		if _, stillPresent := currentByID[transition.beforeID]; stillPresent {
			problems = append(problems, fmt.Sprintf(
				"%s 新旧 candidate 不能同时存在", transition.name))
			continue
		}
		if err := validatePostBootstrapSinkSuccessor(before, after); err != nil {
			problems = append(problems, fmt.Sprintf("%s successor 语义不一致：%v", transition.name, err))
			continue
		}
		accepted.acceptedAdded[transition.afterID] = struct{}{}
		accepted.acceptedRemoved[transition.beforeID] = struct{}{}
	}

	// 合并前预先登记的上游新增发送点按 mergeGroup 统计出现条数，整组只能全缺或全在。
	pendingTotal := make(map[string]int)
	pendingPresent := make(map[string]int)
	for _, addition := range reviewedPostBootstrapSinkAdditions {
		if addition.name == "" || addition.candidateID == "" || addition.evidenceRef == "" ||
			addition.rationale == "" || (addition.absentBeforeMerge && addition.mergeGroup == "") {
			problems = append(problems, "post-bootstrap sink addition 定义不完整")
			continue
		}
		if _, existed := oldByID[addition.candidateID]; existed {
			problems = append(problems, fmt.Sprintf(
				"%s 被错误登记为 bootstrap 后新增，但 candidate 已存在: %s",
				addition.name, addition.candidateID))
			continue
		}
		current, exists := currentByID[addition.candidateID]
		if addition.absentBeforeMerge {
			pendingTotal[addition.mergeGroup]++
			if exists {
				pendingPresent[addition.mergeGroup]++
			}
		}
		if !exists {
			if addition.absentBeforeMerge {
				// 上游新增发送点合并前尚未出现：合法状态，整组是否齐全在循环后统一判定。
				continue
			}
			problems = append(problems, fmt.Sprintf(
				"%s 新增 candidate 不在当前发送面中: %s", addition.name, addition.candidateID))
			continue
		}
		if err := validatePostBootstrapSinkAddition(addition, current); err != nil {
			problems = append(problems, fmt.Sprintf("%s 新增 candidate 不安全：%v", addition.name, err))
			continue
		}
		accepted.acceptedAdded[addition.candidateID] = struct{}{}
	}
	for group, total := range pendingTotal {
		if present := pendingPresent[group]; present != 0 && present != total {
			problems = append(problems, fmt.Sprintf(
				"%s 上游新增发送点只出现 %d/%d 条：合并前必须全部缺失、合并后必须全部出现",
				group, present, total))
		}
	}

	return accepted, problems
}

// validatePostBootstrapSinkSuccessor 比较除 candidate 身份、函数名、行号和审阅
// 说明外的全部字段。这样允许上游做机械 helper 重命名，但不能偷偷改变 route、
// RuntimeSinkID、AST 指纹、后端、Persona 或构建矩阵。
func validatePostBootstrapSinkSuccessor(before, after SinkRecord) error {
	left := before
	right := after
	left.ScanCandidateID, right.ScanCandidateID = "", ""
	left.Func, right.Func = "", ""
	left.Rationale, right.Rationale = "", ""
	left.Line, right.Line = 0, 0
	if !sameFrozenCandidate(left, right) {
		return fmt.Errorf("稳定字段发生变化")
	}
	if after.RuntimeSinkID == "" || after.Persona == "" || after.EnforcementState == "" {
		return fmt.Errorf("successor 缺少运行时身份或分类状态")
	}
	return nil
}

func validatePostBootstrapSinkAddition(expected postBootstrapSinkAddition, current SinkRecord) error {
	if current.ScanCandidateID != expected.candidateID || current.Persona != expected.persona ||
		current.RuntimeSinkID != expected.runtimeSinkID || current.Purpose != expected.purpose ||
		current.EndpointEvidence != expected.endpointEvidence || current.SinkKind != expected.sinkKind ||
		current.Backend != expected.backend || current.TargetBackend != expected.targetBackend ||
		current.EnforcementState != expected.enforcementState || current.Rationale == "" {
		return fmt.Errorf("分类、运行时身份或后端边界与审核收据不一致")
	}
	if current.Persona == "out-of-scope" &&
		(current.RuntimeSinkID != "" || current.EnforcementState != "not_applicable") {
		return fmt.Errorf("out-of-scope candidate 不能进入运行时 Catalog")
	}
	return nil
}
