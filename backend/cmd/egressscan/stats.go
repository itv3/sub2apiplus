package main

import (
	"fmt"
	"os"
	"sort"
	"strings"
)

// runStats 从已经严格校验的发送面基线生成 Markdown 统计。
// 文档只引用这一份生成物，避免 README、inventory 与 JSON 各自手写数字后漂移。
func runStats(baselinePath, out string) int {
	if baselinePath == "" || out == "" {
		fmt.Fprintln(os.Stderr, "stats 模式需要 -baseline 与 -out")
		return 2
	}
	raw, err := os.ReadFile(baselinePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取基线失败：%v\n", err)
		return 1
	}
	baseline, err := decodeBaseline(raw)
	if err != nil {
		fmt.Fprintf(os.Stderr, "解析基线失败：%v\n", err)
		return 1
	}
	if err := os.WriteFile(out, renderBaselineStats(baseline), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "写入统计失败：%v\n", err)
		return 1
	}
	return 0
}

func renderBaselineStats(baseline Baseline) []byte {
	personaCounts := make(map[string]int)
	migrationCounts := make(map[string]int)
	evidenceCounts := make(map[string]int)
	ownerCounts := make(map[string]int)
	runtimeSinkIDs := make(map[string]struct{})
	inScopeCandidates := 0
	resolvedMethodCandidates := 0
	resolvedTargetCandidates := 0

	for _, sink := range baseline.Sinks {
		personaCounts[sink.Persona]++
		evidenceCounts[sink.EndpointEvidence]++
		if len(sink.ResolvedMethods) > 0 {
			resolvedMethodCandidates++
		}
		if len(sink.ResolvedTargets) > 0 {
			resolvedTargetCandidates++
		}
		if !isInScopePersona(sink.Persona) {
			continue
		}
		inScopeCandidates++
		migrationCounts[sink.MigrationChangeset]++
		ownerCounts[sink.Owner]++
		if sink.RuntimeSinkID != "" {
			runtimeSinkIDs[sink.RuntimeSinkID] = struct{}{}
		}
	}

	var builder strings.Builder
	_, _ = builder.WriteString("<!-- 此文件由 backend/cmd/egressscan -mode stats 生成，请勿手工修改。 -->\n")
	_, _ = builder.WriteString("# 发送面基线统计\n\n")
	fmt.Fprintf(&builder, "> 数据源：`sink-baseline.json`；bootstrap commit：`%s`。\n\n", baseline.BootstrapCommit)
	fmt.Fprintf(&builder, "- 基线记录：%d 条；\n", len(baseline.Sinks))
	fmt.Fprintf(&builder, "- 构建上下文：%s；包-上下文：%d 个；类型检查兜底文件：%d 个；\n",
		strings.Join(baseline.BuildContexts, "、"), baseline.PackagesLoaded, len(baseline.SyntaxFallback))
	fmt.Fprintf(&builder, "- 可静态确定 method：%d 条；可确定完整 target/host/path：%d 条；\n",
		resolvedMethodCandidates, resolvedTargetCandidates)
	fmt.Fprintf(&builder, "- in-scope 候选：%d 条；RuntimeSinkID：%d 个。\n\n",
		inScopeCandidates, len(runtimeSinkIDs))

	writeCountTable(&builder, "Persona", "persona", personaCounts)
	writeCountTable(&builder, "In-scope 迁移归属", "变更集", migrationCounts)
	writeCountTable(&builder, "端点证据状态", "endpoint_evidence", evidenceCounts)
	writeCountTable(&builder, "In-scope 责任人", "owner", ownerCounts)
	return []byte(builder.String())
}

func isInScopePersona(persona string) bool {
	return persona != "out-of-scope" && persona != "infrastructure"
}

func writeCountTable(builder *strings.Builder, title, keyHeader string, counts map[string]int) {
	keys := make([]string, 0, len(counts))
	for key := range counts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	fmt.Fprintf(builder, "## %s\n\n", title)
	fmt.Fprintf(builder, "| %s | 候选数 |\n|---|---:|\n", keyHeader)
	for _, key := range keys {
		fmt.Fprintf(builder, "| `%s` | %d |\n", key, counts[key])
	}
	_, _ = builder.WriteString("\n")
}
