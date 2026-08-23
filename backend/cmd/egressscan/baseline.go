package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"sort"
	"strings"
)

// decodeBaseline 严格解析人工审核后的基线。未知字段、尾随 JSON、重复 ID 或字段间
// 自相矛盾都必须失败，不能让 map 覆盖或 json.Unmarshal 忽略字段制造假绿。
func decodeBaseline(raw []byte, reviewedRouteSets ...[]string) (Baseline, error) {
	var baseline Baseline
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&baseline); err != nil {
		return Baseline{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return Baseline{}, errors.New("基线 JSON 尾部存在额外数据")
		}
		return Baseline{}, fmt.Errorf("基线 JSON 尾部非法: %w", err)
	}
	if err := validateBaseline(baseline, reviewedRouteSets...); err != nil {
		return Baseline{}, err
	}
	return baseline, nil
}

func validateBaseline(baseline Baseline, reviewedRouteSets ...[]string) error {
	if len(baseline.BootstrapCommit) != 40 {
		return fmt.Errorf("bootstrap_commit 长度非法: %q", baseline.BootstrapCommit)
	}
	if _, err := hex.DecodeString(baseline.BootstrapCommit); err != nil {
		return fmt.Errorf("bootstrap_commit 不是十六进制: %w", err)
	}
	if baseline.ScanPattern == "" || baseline.PackagesLoaded <= 0 || len(baseline.Sinks) == 0 {
		return errors.New("基线元数据不完整")
	}
	if len(baseline.BuildContexts) == 0 || !sort.StringsAreSorted(baseline.BuildContexts) ||
		hasDuplicateOrEmpty(baseline.BuildContexts) {
		return errors.New("build_contexts 必须非空、去重并排序")
	}
	allowedContexts := make(map[string]struct{}, len(baseline.BuildContexts))
	for _, contextID := range baseline.BuildContexts {
		allowedContexts[contextID] = struct{}{}
	}
	if !sort.StringsAreSorted(baseline.SyntaxFallback) || hasDuplicateOrEmpty(baseline.SyntaxFallback) {
		return errors.New("syntax_fallback_files 必须去重并排序，且不能包含空项")
	}

	seen := make(map[string]struct{}, len(baseline.Sinks))
	previousID := ""
	for _, sink := range baseline.Sinks {
		if sink.ScanCandidateID == "" {
			return errors.New("存在空 scan_candidate_id")
		}
		if _, exists := seen[sink.ScanCandidateID]; exists {
			return fmt.Errorf("scan_candidate_id 重复: %s", sink.ScanCandidateID)
		}
		seen[sink.ScanCandidateID] = struct{}{}
		if previousID != "" && previousID > sink.ScanCandidateID {
			return errors.New("sinks 必须按 scan_candidate_id 排序")
		}
		previousID = sink.ScanCandidateID
		if sink.File == "" || sink.Func == "" || sink.Package == "" || sink.Callee == "" ||
			sink.SinkKind == "" || sink.Protocol == "" || sink.SinkType == "" ||
			sink.ASTFingerprint == "" || sink.EndpointEvidence == "" || sink.Line <= 0 {
			return fmt.Errorf("候选 %s 的结构字段不完整", sink.ScanCandidateID)
		}
		if len(sink.BuildContexts) == 0 || !sort.StringsAreSorted(sink.BuildContexts) ||
			hasDuplicateOrEmpty(sink.BuildContexts) {
			return fmt.Errorf("候选 %s 的 build_contexts 必须非空、去重并排序", sink.ScanCandidateID)
		}
		for _, contextID := range sink.BuildContexts {
			if _, ok := allowedContexts[contextID]; !ok {
				return fmt.Errorf("候选 %s 引用了基线未声明的构建上下文 %q", sink.ScanCandidateID, contextID)
			}
		}
		if len(sink.ASTFingerprint) != 12 {
			return fmt.Errorf("候选 %s 的 AST 指纹长度非法", sink.ScanCandidateID)
		}
		if _, err := hex.DecodeString(sink.ASTFingerprint); err != nil {
			return fmt.Errorf("候选 %s 的 AST 指纹非法: %w", sink.ScanCandidateID, err)
		}
		switch sink.Resolution {
		case TargetLiteral, TargetConst, TargetConstructed, TargetDynamic, TargetUnknown:
		default:
			return fmt.Errorf("候选 %s 的 target_resolution 非法: %q", sink.ScanCandidateID, sink.Resolution)
		}
		for field, values := range map[string][]string{
			"resolved_hosts":   sink.ResolvedHosts,
			"resolved_methods": sink.ResolvedMethods,
			"resolved_paths":   sink.ResolvedPaths,
			"resolved_targets": sink.ResolvedTargets,
			"routes":           sink.Routes,
		} {
			if !sort.StringsAreSorted(values) || hasDuplicateOrEmpty(values) {
				return fmt.Errorf("候选 %s 的 %s 必须非空、去重并排序", sink.ScanCandidateID, field)
			}
		}
		for _, method := range sink.ResolvedMethods {
			if method != strings.ToUpper(method) {
				return fmt.Errorf("候选 %s 的 method 未规范化: %q", sink.ScanCandidateID, method)
			}
		}
		for _, path := range sink.ResolvedPaths {
			if !strings.HasPrefix(path, "/") {
				return fmt.Errorf("候选 %s 的 path 非法: %q", sink.ScanCandidateID, path)
			}
		}
		hosts, paths, official := targetComponents(sink.ResolvedTargets)
		if !reflect.DeepEqual(normalizeNilStrings(hosts), normalizeNilStrings(sink.ResolvedHosts)) ||
			!reflect.DeepEqual(normalizeNilStrings(paths), normalizeNilStrings(sink.ResolvedPaths)) {
			return fmt.Errorf("候选 %s 的 host/path 与 resolved_targets 不一致", sink.ScanCandidateID)
		}
		if official != sink.OfficialHost {
			return fmt.Errorf("候选 %s 的 official_host 与 resolved_targets 不一致", sink.ScanCandidateID)
		}
		if len(sink.ResolvedTargets) > 0 &&
			(sink.Resolution == TargetDynamic || sink.Resolution == TargetUnknown) {
			return fmt.Errorf("候选 %s 已解析 target 却标记为 %s", sink.ScanCandidateID, sink.Resolution)
		}
	}
	var reviewedRoutes []string
	if len(reviewedRouteSets) > 0 {
		reviewedRoutes = reviewedRouteSets[0]
	}
	if problems := validateClassification(baseline.Sinks, reviewedRoutes); len(problems) > 0 {
		return fmt.Errorf("基线分类非法: %s", strings.Join(problems, "; "))
	}
	return nil
}

func hasDuplicateOrEmpty(values []string) bool {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) == "" {
			return true
		}
		if _, exists := seen[value]; exists {
			return true
		}
		seen[value] = struct{}{}
	}
	return false
}

func normalizeNilStrings(values []string) []string {
	if len(values) == 0 {
		return []string{}
	}
	return values
}
