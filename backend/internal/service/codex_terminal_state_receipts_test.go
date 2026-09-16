package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// 通用终态收据门禁（Framework §5.7）：0.151 起每次官方客户端升级都必须在
// docs/egress/maintenance 留下 CODEX_CLI_*_TERMINAL_STATE_RECEIPT.json，并且
// 自摘要一致、四份阶段收据与审计索引逐字在库、当前 active 版本必须有收据且
// Runtime Catalog 的 source 指向其 Campaign 链。0.149.1 由专用测试覆盖。
// 候选中间态（VC-4 候选 Catalog 入库后、VC-6 激活前）：下一版本候选已入库而 active
// 未变时，Runtime Catalog 的 source 允许指向候选 Campaign 的 classification 摘要，
// 前提是该 Campaign 不在 active 终态链上，且 ReleaseGraph 所有 previous 候选节点的
// source 与之逐字一致；active 节点 source 仍必须落在终态链上。

const codexTerminalStateHistoricalReceipt = "CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json"

var codexTerminalStateSchemaPattern = regexp.MustCompile(`^official-client-codex-(\d+\.\d+\.\d+)-terminal-state/v1$`)

var codexTerminalStateCandidateSourcePattern = regexp.MustCompile(`^campaign:([^/]+)/classification:[0-9a-f]{64}$`)

// codexTerminalStateCandidateNode 是 ReleaseGraph 中一个非 active 节点的版本与来源。
type codexTerminalStateCandidateNode struct {
	Mode    string
	Version string
	Source  string
}

// codexTerminalStateCandidateCatalogSource 判定候选中间态：Runtime Catalog source 指向尚未
// 激活的候选 Campaign classification，该 Campaign 不在 active 终态链上，且 ReleaseGraph 中
// 所有版本不等于 active 版本的 previous 节点 source 都与之逐字一致（至少一个）。
func codexTerminalStateCandidateCatalogSource(
	catalogSource string,
	activeVersion string,
	chainIDs []string,
	candidates []codexTerminalStateCandidateNode,
) bool {
	matched := codexTerminalStateCandidateSourcePattern.FindStringSubmatch(catalogSource)
	if matched == nil {
		return false
	}
	for _, campaignID := range chainIDs {
		if matched[1] == campaignID {
			return false
		}
	}
	bound := 0
	for _, candidate := range candidates {
		if candidate.Mode != "previous" || candidate.Version == activeVersion {
			continue
		}
		if candidate.Source != catalogSource {
			return false
		}
		bound++
	}
	return bound > 0
}

func codexTerminalStateRepoPath(relative string) string {
	return filepath.Join("../../..", filepath.FromSlash(relative))
}

func codexTerminalStateDigestService(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// codexTerminalStateCanonical 按 Python json.dumps(sort_keys, compact, ensure_ascii=False)
// 的形态重新编码；不转义 HTML 字符，保持与生成端一致。
func codexTerminalStateCanonical(document map[string]any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(document); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buffer.Bytes(), "\n"), nil
}

func codexTerminalStateBinding(t *testing.T, document map[string]any, key string) (string, string) {
	t.Helper()
	raw, ok := document[key].(map[string]any)
	if !ok {
		t.Fatalf("终态收据缺少 %s 坐标", key)
	}
	path, _ := raw["path"].(string)
	digest, _ := raw["sha256"].(string)
	if strings.TrimSpace(path) == "" || len(digest) != 64 {
		t.Fatalf("终态收据 %s 坐标非法", key)
	}
	return path, digest
}

func codexTerminalStateActiveVersion(t *testing.T) (string, []string, string, []codexTerminalStateCandidateNode) {
	t.Helper()
	catalogRaw, err := os.ReadFile(codexTerminalStateRepoPath("backend/internal/officialegress/catalogdata/runtime/release-catalog.json"))
	if err != nil {
		t.Fatal(err)
	}
	var catalog struct {
		ReleaseGraph struct {
			Path string `json:"path"`
		} `json:"release_graph"`
		Source string `json:"source"`
	}
	if err := json.Unmarshal(catalogRaw, &catalog); err != nil {
		t.Fatal(err)
	}
	graphRaw, err := os.ReadFile(codexTerminalStateRepoPath("backend/internal/officialegress/" + catalog.ReleaseGraph.Path))
	if err != nil {
		t.Fatal(err)
	}
	var graph struct {
		Nodes []struct {
			Mode  string `json:"mode"`
			Build struct {
				Version string `json:"version"`
				Source  string `json:"source"`
			} `json:"build"`
		} `json:"nodes"`
	}
	if err := json.Unmarshal(graphRaw, &graph); err != nil {
		t.Fatal(err)
	}
	versions := map[string]struct{}{}
	sources := []string{}
	candidates := []codexTerminalStateCandidateNode{}
	for _, node := range graph.Nodes {
		if node.Mode != "active" {
			candidates = append(candidates, codexTerminalStateCandidateNode{
				Mode:    node.Mode,
				Version: node.Build.Version,
				Source:  node.Build.Source,
			})
			continue
		}
		versions[node.Build.Version] = struct{}{}
		sources = append(sources, node.Build.Source)
	}
	if len(versions) != 1 {
		t.Fatalf("Runtime Catalog active 版本不唯一：%v", versions)
	}
	for version := range versions {
		return version, sources, catalog.Source, candidates
	}
	return "", nil, "", nil
}

func TestCodexTerminalStateReceiptsCloseTheUpgradeLoop(t *testing.T) {
	activeVersion, activeSources, catalogSource, candidateNodes := codexTerminalStateActiveVersion(t)
	matches, err := filepath.Glob(codexTerminalStateRepoPath("docs/egress/maintenance/CODEX_CLI_*_TERMINAL_STATE_RECEIPT.json"))
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, receiptPath := range matches {
		if filepath.Base(receiptPath) == codexTerminalStateHistoricalReceipt {
			continue
		}
		raw, err := os.ReadFile(receiptPath)
		if err != nil {
			t.Fatal(err)
		}
		var document map[string]any
		if err := json.Unmarshal(raw, &document); err != nil {
			t.Fatalf("%s：%v", receiptPath, err)
		}
		schema, _ := document["schema_version"].(string)
		matched := codexTerminalStateSchemaPattern.FindStringSubmatch(schema)
		if matched == nil {
			t.Fatalf("终态收据 schema 非法：%s", receiptPath)
		}
		version := matched[1]
		identity, _ := document["identity_sha256"].(string)
		delete(document, "identity_sha256")
		canonical, err := codexTerminalStateCanonical(document)
		if err != nil {
			t.Fatal(err)
		}
		// 0.151 起不带尾换行；0.149.1 风格带尾换行。两种都认，避免同一事实两种算法互斥。
		if identity != codexTerminalStateDigestService(canonical) &&
			identity != codexTerminalStateDigestService(append(append([]byte{}, canonical...), '\n')) {
			t.Fatalf("终态收据自摘要不一致：%s", receiptPath)
		}
		if result, _ := document["result"].(string); result != "passed" {
			t.Fatalf("终态收据 result 非 passed：%s", receiptPath)
		}
		for _, key := range []string{"catalog_promotion", "production_activation", "post_promotion_gate", "runtime_profile_removal", "audit_index"} {
			path, digest := codexTerminalStateBinding(t, document, key)
			if !strings.HasPrefix(path, "docs/egress/maintenance/") {
				t.Fatalf("终态收据 %s 不在 maintenance 目录：%s", key, path)
			}
			current, err := os.ReadFile(codexTerminalStateRepoPath(path))
			if err != nil {
				t.Fatalf("终态收据 %s 引用的文件缺失：%s", key, path)
			}
			if codexTerminalStateDigestService(current) != digest {
				t.Fatalf("终态收据 %s 引用的文件摘要漂移：%s", key, path)
			}
		}
		chain, _ := document["campaign_chain"].([]any)
		if len(chain) == 0 {
			t.Fatalf("终态收据缺少 Campaign 承接链：%s", receiptPath)
		}
		chainIDs := make([]string, 0, len(chain))
		for _, link := range chain {
			entry, _ := link.(map[string]any)
			campaignID, _ := entry["campaign_id"].(string)
			if strings.TrimSpace(campaignID) == "" {
				t.Fatalf("终态收据 Campaign 链条目缺少 campaign_id：%s", receiptPath)
			}
			chainIDs = append(chainIDs, campaignID)
		}
		if version == activeVersion {
			runtimeCatalog, _ := document["runtime_catalog"].(map[string]any)
			for _, key := range []string{"catalog", "release_graph", "snapshot_catalog", "active_profile"} {
				path, digest := codexTerminalStateBinding(t, runtimeCatalog, key)
				current, err := os.ReadFile(codexTerminalStateRepoPath(path))
				if err != nil {
					t.Fatalf("当前 active 终态 runtime %s 缺失：%s", key, path)
				}
				currentDigest := codexTerminalStateDigestService(current)
				if currentDigest != digest && !auditedSourceSuccessorReachesService(path, digest, currentDigest) {
					t.Fatalf("当前 active 终态 runtime %s 摘要漂移且无 successor 承接：%s", key, path)
				}
			}
			if !strings.HasPrefix(catalogSource, "campaign:"+chainIDs[len(chainIDs)-1]+"/") &&
				!codexTerminalStateCandidateCatalogSource(catalogSource, activeVersion, chainIDs, candidateNodes) {
				t.Fatalf("Runtime Catalog source 未指向 %s 终态收据的末级 Campaign：%s", version, catalogSource)
			}
			for _, source := range activeSources {
				onChain := false
				for _, campaignID := range chainIDs {
					if strings.HasPrefix(source, "campaign:"+campaignID+"/") {
						onChain = true
						break
					}
				}
				if !onChain {
					t.Fatalf("ReleaseGraph active 节点 source 不在 %s 终态收据的 Campaign 链上：%s", version, source)
				}
			}
		}
		seen[version] = true
	}
	if activeVersion != "0.149.1" && !seen[activeVersion] {
		t.Fatalf("当前 active 版本 %s 缺少终态收据（Framework §5.7）", activeVersion)
	}
}
