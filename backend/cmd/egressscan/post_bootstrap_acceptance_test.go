package main

import "testing"

func postBootstrapAcceptanceTestRecord(id, function, rationale string) SinkRecord {
	return SinkRecord{
		ScanCandidateID:    id,
		File:               "backend/internal/service/openai_gateway_chat_completions.go",
		Func:               function,
		Package:            "github.com/Wei-Shaw/sub2api/internal/service",
		Callee:             "(*github.com/Wei-Shaw/sub2api/internal/service.OpenAIGatewayService).doOpenAIHTTPUpstreamForRequest",
		Receiver:           "*github.com/Wei-Shaw/sub2api/internal/service.OpenAIGatewayService",
		SinkKind:           "facade_openai_upstream_req",
		Protocol:           "http",
		SinkType:           "facade",
		ASTFingerprint:     "4fa501ed584c",
		Resolution:         TargetUnknown,
		BuildContexts:      []string{"darwin/arm64", "linux/amd64"},
		RuntimeSinkID:      "codex.responses.chat_completions",
		Purpose:            "user_request.chat_completions",
		Persona:            "codex-cli",
		EndpointEvidence:   "codex_profile",
		Routes:             []string{"POST chatgpt.com/backend-api/codex/responses"},
		Backend:            "http_upstream",
		TargetBackend:      "http_upstream",
		EnforcementState:   "legacy_observe",
		Owner:              "czs",
		MigrationChangeset: "3",
		ExpiryCondition:    "变更集 3 迁入 Executor",
		Rationale:          rationale,
	}
}

func postBootstrapAcceptanceFixture() (map[string]SinkRecord, map[string]SinkRecord) {
	transition := reviewedPostBootstrapSinkTransitions[0]
	before := postBootstrapAcceptanceTestRecord(transition.beforeID,
		"*OpenAIGatewayService.ForwardAsChatCompletions", "旧实现")
	after := postBootstrapAcceptanceTestRecord(transition.afterID,
		"*OpenAIGatewayService.forwardAsChatCompletions", "上游内部 helper")
	addition := reviewedPostBootstrapSinkAdditions[0]
	image := SinkRecord{
		ScanCandidateID:  addition.candidateID,
		Persona:          "out-of-scope",
		EndpointEvidence: "not_applicable",
		SinkKind:         "facade_http_upstream_do",
		Backend:          "-",
		TargetBackend:    "-",
		EnforcementState: "not_applicable",
		Rationale:        addition.rationale,
	}
	return map[string]SinkRecord{before.ScanCandidateID: before}, map[string]SinkRecord{
		after.ScanCandidateID: after, image.ScanCandidateID: image,
	}
}

func TestReviewedPostBootstrapSinkTransitionAcceptsExactSuccessor(t *testing.T) {
	transition := reviewedPostBootstrapSinkTransitions[0]
	before := postBootstrapAcceptanceTestRecord(transition.beforeID,
		"*OpenAIGatewayService.ForwardAsChatCompletions", "旧实现")
	after := postBootstrapAcceptanceTestRecord(transition.afterID,
		"*OpenAIGatewayService.forwardAsChatCompletions", "上游内部 helper")
	oldByID, currentByID := postBootstrapAcceptanceFixture()
	accepted, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) != 0 {
		t.Fatalf("精确 successor 不应失败：%v", problems)
	}
	if _, ok := accepted.acceptedAdded[after.ScanCandidateID]; !ok {
		t.Fatal("successor 未登记为 accepted addition")
	}
	if _, ok := accepted.acceptedRemoved[before.ScanCandidateID]; !ok {
		t.Fatal("旧 candidate 未登记为 accepted removal")
	}
}

func TestReviewedPostBootstrapSinkTransitionRejectsSemanticMutation(t *testing.T) {
	transition := reviewedPostBootstrapSinkTransitions[0]
	after := postBootstrapAcceptanceTestRecord(transition.afterID,
		"*OpenAIGatewayService.forwardAsChatCompletions", "上游内部 helper")
	after.RuntimeSinkID = "codex.responses.other"
	oldByID, currentByID := postBootstrapAcceptanceFixture()
	currentByID[after.ScanCandidateID] = after
	_, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) == 0 {
		t.Fatal("RuntimeSinkID 漂移未被拒绝")
	}
}

func TestReviewedPostBootstrapSinkTransitionRejectsBothGenerations(t *testing.T) {
	transition := reviewedPostBootstrapSinkTransitions[0]
	before := postBootstrapAcceptanceTestRecord(transition.beforeID,
		"*OpenAIGatewayService.ForwardAsChatCompletions", "旧实现")
	oldByID, currentByID := postBootstrapAcceptanceFixture()
	currentByID[before.ScanCandidateID] = before
	_, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) == 0 {
		t.Fatal("新旧两代 candidate 并存未被拒绝")
	}
}

func TestReviewedPostBootstrapSinkAdditionIsOutOfScopeAndFailClosed(t *testing.T) {
	addition := reviewedPostBootstrapSinkAdditions[0]
	_, currentByID := postBootstrapAcceptanceFixture()
	current := currentByID[addition.candidateID]
	oldByID, _ := postBootstrapAcceptanceFixture()
	accepted, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) != 0 {
		t.Fatalf("范围外新增项不应失败：%v", problems)
	}
	if _, ok := accepted.acceptedAdded[current.ScanCandidateID]; !ok {
		t.Fatal("范围外新增项未被接受")
	}
	current.RuntimeSinkID = "unexpected.runtime"
	currentByID[current.ScanCandidateID] = current
	_, problems = validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) == 0 {
		t.Fatal("范围外新增项进入运行时身份后未被拒绝")
	}
}

// pendingAdditionRecords 按登记内容构造合并后应出现的发送点记录（分类字段与审核收据逐字一致），
// 按 mergeGroup 分组、组的顺序与登记顺序一致；每组各自独立地“整组缺失或整组齐全”。
func pendingAdditionRecords(t *testing.T) ([]string, map[string][]SinkRecord) {
	t.Helper()
	var groups []string
	byGroup := make(map[string][]SinkRecord)
	for _, addition := range reviewedPostBootstrapSinkAdditions {
		if !addition.absentBeforeMerge {
			continue
		}
		if _, seen := byGroup[addition.mergeGroup]; !seen {
			groups = append(groups, addition.mergeGroup)
		}
		byGroup[addition.mergeGroup] = append(byGroup[addition.mergeGroup], SinkRecord{
			ScanCandidateID:  addition.candidateID,
			Persona:          addition.persona,
			RuntimeSinkID:    addition.runtimeSinkID,
			Purpose:          addition.purpose,
			EndpointEvidence: addition.endpointEvidence,
			SinkKind:         addition.sinkKind,
			Backend:          addition.backend,
			TargetBackend:    addition.targetBackend,
			EnforcementState: addition.enforcementState,
			Rationale:        addition.rationale,
		})
	}
	return groups, byGroup
}

// 合并前整组缺失、合并后整组齐全都合法；只出现一部分、或出现后分类漂移都必须失败。
// 多个 mergeGroup 各自独立判定：一组齐全时，其余组仍可整组缺失。
func TestReviewedPostBootstrapSinkAdditionPendingGroupIsAllOrNothing(t *testing.T) {
	groups, byGroup := pendingAdditionRecords(t)
	if len(groups) == 0 {
		t.Fatal("没有合并前预先登记的上游新增发送点")
	}

	oldByID, currentByID := postBootstrapAcceptanceFixture()
	accepted, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
	if len(problems) != 0 {
		t.Fatalf("合并前各组整组缺失不应失败：%v", problems)
	}
	for _, group := range groups {
		for _, record := range byGroup[group] {
			if _, ok := accepted.acceptedAdded[record.ScanCandidateID]; ok {
				t.Fatalf("尚未出现的候选不应被接受：%s", record.ScanCandidateID)
			}
		}
	}

	for _, group := range groups {
		records := byGroup[group]
		oldByID, currentByID := postBootstrapAcceptanceFixture()
		for _, record := range records {
			currentByID[record.ScanCandidateID] = record
		}
		accepted, problems := validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID)
		if len(problems) != 0 {
			t.Fatalf("%s 合并后整组齐全（其余组缺失）不应失败：%v", group, problems)
		}
		for _, record := range records {
			if _, ok := accepted.acceptedAdded[record.ScanCandidateID]; !ok {
				t.Fatalf("%s 合并后出现的候选未被接受：%s", group, record.ScanCandidateID)
			}
		}

		// 只有一条的组没有“只出现一部分”的情形（删掉唯一一条就是整组缺失，合法）；sink-registration-draft
		// 按批次起草，上游一个版本只新增一个发送点时就是这种组（UM-25）。
		if len(records) >= 2 {
			delete(currentByID, records[0].ScanCandidateID)
			if _, problems = validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID); len(problems) == 0 {
				t.Fatalf("%s 整组只出现一部分未被拒绝", group)
			}
		}

		drifted := records[0]
		drifted.RuntimeSinkID = "unexpected.runtime"
		currentByID[drifted.ScanCandidateID] = drifted
		if _, problems = validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID); len(problems) == 0 {
			t.Fatalf("%s 出现后进入运行时身份未被拒绝", group)
		}
	}

	// 全部组同时齐全（连续两次合并都已完成）同样合法。
	oldByID, currentByID = postBootstrapAcceptanceFixture()
	for _, group := range groups {
		for _, record := range byGroup[group] {
			currentByID[record.ScanCandidateID] = record
		}
	}
	if _, problems = validateReviewedPostBootstrapSinkAcceptance(oldByID, currentByID); len(problems) != 0 {
		t.Fatalf("各组都已齐全不应失败：%v", problems)
	}
}
