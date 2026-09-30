package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// 上游合并 source-transition 节点的通用复算：维护目录下每一份上游合并节点都按同一规则逐项
// 复算，取代按上游版本各手写一份测试的做法，新合并的节点无需再加测试。节点格式有 v2（历史，无签发时间）
// 与 v3（写明签发时间 issued_at_utc）两种，按同一规则复算并各自核对签发时间。早期格式（v1、计划式
// v2、扫描器单跳收据）由各自的冻结测试负责，这里只确认它们仍可识别，避免新节点被误判为旧
// 格式而漏检。
const (
	upstreamSourceTransitionGenericGlobService     = "docs/egress/maintenance/upstream-v*-source-transition.json"
	upstreamSourceTransitionGenericSchemaService   = "official-egress-upstream-source-transition/v2"
	upstreamSourceTransitionGenericSchemaV3Service = "official-egress-upstream-source-transition/v3"
	upstreamSourceTransitionLegacySchemaService    = "official-egress-upstream-source-transition/v1"
	upstreamScannerSuccessorSchemaSuffixService    = "-scanner-successor-source-transition/v1"
)

type upstreamSourceTransitionGenericEntryService struct {
	Path              string  `json:"path"`
	OldPath           string  `json:"old_path"`
	Status            string  `json:"status"`
	PredecessorSHA256 *string `json:"predecessor_sha256"`
	CurrentSHA256     *string `json:"current_sha256"`
	Reason            string  `json:"reason"`
}

type upstreamSourceTransitionGenericNodeService struct {
	SchemaVersion       string                                        `json:"schema_version"`
	BaseCommit          string                                        `json:"base_commit"`
	CurrentCommit       string                                        `json:"current_commit"`
	BaseTree            string                                        `json:"base_tree"`
	CurrentTree         string                                        `json:"current_tree"`
	ChainSequence       int                                           `json:"chain_sequence"`
	PredecessorRegister json.RawMessage                               `json:"predecessor_register"`
	Entries             []upstreamSourceTransitionGenericEntryService `json:"entries"`
	EntryCount          int                                           `json:"entry_count"`
	ReasonPolicy        string                                        `json:"reason_policy"`
	Result              string                                        `json:"result"`
	IssuedAtUTC         *string                                       `json:"issued_at_utc"`
	IdentitySHA256      string                                        `json:"identity_sha256"`
}

// upstreamSourceTransitionGenericNodePathsService 返回全部节点格式的收据路径；无法识别的格式直接失败。
func upstreamSourceTransitionGenericNodePathsService(t *testing.T) []string {
	t.Helper()
	matches, err := filepath.Glob(filepath.Join("../../..", upstreamSourceTransitionGenericGlobService))
	if err != nil {
		t.Fatal(err)
	}
	var nodes []string
	for _, path := range matches {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var probe map[string]json.RawMessage
		if err := json.Unmarshal(raw, &probe); err != nil {
			t.Fatalf("上游 source-transition 收据不是 JSON 对象：%s：%v", path, err)
		}
		var schema string
		if err := json.Unmarshal(probe["schema_version"], &schema); err != nil {
			t.Fatalf("上游 source-transition 收据缺少 schema_version：%s", path)
		}
		_, hasEntries := probe["entries"]
		_, hasPlanTransitions := probe["source_transitions"]
		switch {
		case (schema == upstreamSourceTransitionGenericSchemaService || schema == upstreamSourceTransitionGenericSchemaV3Service) && hasEntries:
			nodes = append(nodes, path)
		case schema == upstreamSourceTransitionGenericSchemaService && hasPlanTransitions,
			schema == upstreamSourceTransitionLegacySchemaService,
			strings.HasSuffix(schema, upstreamScannerSuccessorSchemaSuffixService):
			// 早期格式由各自的冻结测试复算。
		default:
			t.Fatalf("无法识别的上游 source-transition 收据格式：%s（%s）", path, schema)
		}
	}
	if len(nodes) < 2 {
		t.Fatalf("上游 source-transition 节点数量异常：%d", len(nodes))
	}
	return nodes
}

func readUpstreamSourceTransitionGenericNodeService(path string) (upstreamSourceTransitionGenericNodeService, error) {
	var node upstreamSourceTransitionGenericNodeService
	raw, err := os.ReadFile(path)
	if err != nil {
		return node, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&node); err != nil {
		return node, fmt.Errorf("%s：%w", path, err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return node, errors.New("上游 source-transition 节点尾部存在额外 JSON：" + path)
	}
	var identityDocument map[string]any
	if err := json.Unmarshal(raw, &identityDocument); err != nil {
		return node, err
	}
	delete(identityDocument, "identity_sha256")
	canonical, err := json.Marshal(identityDocument)
	if err != nil || upstreamMergeFrameworkServiceDigest(append(canonical, '\n')) != node.IdentitySHA256 {
		return node, errors.New("上游 source-transition 节点自摘要不一致：" + path)
	}
	return node, nil
}

func validateUpstreamSourceTransitionGenericNodeService(node upstreamSourceTransitionGenericNodeService) error {
	if err := upstreamSourceTransitionGenericIssuedAtService(node.SchemaVersion, node.IssuedAtUTC, time.Now()); err != nil {
		return err
	}
	if (node.SchemaVersion != upstreamSourceTransitionGenericSchemaService && node.SchemaVersion != upstreamSourceTransitionGenericSchemaV3Service) ||
		!upstreamSourceTransitionGenericGitObjectService(node.BaseCommit) ||
		!upstreamSourceTransitionGenericGitObjectService(node.CurrentCommit) ||
		!upstreamSourceTransitionGenericGitObjectService(node.BaseTree) ||
		!upstreamSourceTransitionGenericGitObjectService(node.CurrentTree) ||
		node.EntryCount != len(node.Entries) || node.EntryCount == 0 ||
		strings.TrimSpace(node.ReasonPolicy) == "" || node.Result != "generated" ||
		!validOpenAIReplayOOMRepairServiceSHA(node.IdentitySHA256) {
		return errors.New("上游 source-transition 节点顶层事实非法")
	}
	// 每次合并独立成节；带前序登记的链式节点尚无先例，出现时须先补充链尾校验再放行。
	if string(node.PredecessorRegister) != "null" || node.ChainSequence != 1 {
		return errors.New("上游 source-transition 节点带前序登记，通用复算尚未覆盖")
	}
	baseTree, err := upstreamSourceTransitionGenericGitOutputService("rev-parse", node.BaseCommit+"^{tree}")
	if err != nil || baseTree != node.BaseTree {
		return errors.New("上游 source-transition 节点基准 tree 不一致")
	}
	currentTree, err := upstreamSourceTransitionGenericGitOutputService("rev-parse", node.CurrentCommit+"^{tree}")
	if err != nil || currentTree != node.CurrentTree {
		return errors.New("上游 source-transition 节点当前 tree 不一致")
	}
	if upstreamSourceTransitionGenericGitAncestorService(node.BaseCommit, node.CurrentCommit) != nil {
		return errors.New("上游 source-transition 节点提交关系非法")
	}
	if upstreamSourceTransitionGenericGitAncestorService(node.CurrentCommit, "HEAD") != nil {
		return errors.New("上游 source-transition 节点未被当前 HEAD 承接")
	}
	paths := make([]string, 0, len(node.Entries))
	for _, entry := range node.Entries {
		if err := validateUpstreamSourceTransitionGenericEntryService(entry); err != nil {
			return err
		}
		paths = append(paths, entry.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("上游 source-transition 节点路径未严格排序")
	}
	return nil
}

func validateUpstreamSourceTransitionGenericEntryService(entry upstreamSourceTransitionGenericEntryService) error {
	if strings.TrimSpace(entry.Path) == "" || filepath.IsAbs(filepath.FromSlash(entry.Path)) ||
		strings.HasPrefix(filepath.ToSlash(entry.Path), "../") || strings.TrimSpace(entry.Reason) == "" {
		return errors.New("上游 source-transition 节点条目非法：" + entry.Path)
	}
	if entry.OldPath != "" && entry.Status != "R" && entry.Status != "C" {
		return errors.New("上游 source-transition 节点 old_path 非法：" + entry.Path)
	}
	switch entry.Status {
	case "A":
		if entry.PredecessorSHA256 != nil || !upstreamSourceTransitionGenericDigestService(entry.CurrentSHA256) {
			return errors.New("上游 source-transition 节点新增条目非法：" + entry.Path)
		}
	case "D":
		if !upstreamSourceTransitionGenericDigestService(entry.PredecessorSHA256) || entry.CurrentSHA256 != nil {
			return errors.New("上游 source-transition 节点删除条目非法：" + entry.Path)
		}
	case "M", "R", "C", "T":
		if !upstreamSourceTransitionGenericDigestService(entry.PredecessorSHA256) ||
			!upstreamSourceTransitionGenericDigestService(entry.CurrentSHA256) {
			return errors.New("上游 source-transition 节点修改条目非法：" + entry.Path)
		}
	default:
		return errors.New("上游 source-transition 节点状态非法：" + entry.Path)
	}
	currentPath := filepath.Join("../../..", filepath.FromSlash(entry.Path))
	if entry.CurrentSHA256 == nil {
		if _, err := os.Stat(currentPath); !errors.Is(err, os.ErrNotExist) {
			return errors.New("上游 source-transition 节点删除路径仍存在：" + entry.Path)
		}
		return nil
	}
	raw, err := os.ReadFile(currentPath)
	if err != nil {
		return errors.New("上游 source-transition 节点路径无法读取：" + entry.Path)
	}
	// 合并之后该路径又被改过时，当前摘要必须能沿已登记的后继边从节点摘要走到。
	digest := upstreamMergeFrameworkServiceDigest(raw)
	if digest != *entry.CurrentSHA256 && !auditedSourceSuccessorReachesService(entry.Path, *entry.CurrentSHA256, digest) {
		return errors.New("上游 source-transition 节点当前摘要不一致：" + entry.Path)
	}
	return nil
}

// upstreamSourceTransitionGenericIssuedAtService 核对签发时间：v3 节点必须写明 UTC 秒级签发时间且不晚于校验时刻，
// v2 历史节点没有签发时间，也不得补写。
func upstreamSourceTransitionGenericIssuedAtService(schema string, issuedAt *string, now time.Time) error {
	switch schema {
	case upstreamSourceTransitionGenericSchemaService:
		if issuedAt != nil {
			return errors.New("上游 source-transition v2 节点不得带签发时间")
		}
		return nil
	case upstreamSourceTransitionGenericSchemaV3Service:
		if issuedAt == nil {
			return errors.New("上游 source-transition v3 节点缺少签发时间")
		}
		issued, err := time.Parse("2006-01-02T15:04:05Z", *issuedAt)
		if err != nil {
			return errors.New("上游 source-transition v3 节点签发时间格式非法：" + *issuedAt)
		}
		if issued.After(now) {
			return errors.New("上游 source-transition v3 节点签发时间晚于当前时刻：" + *issuedAt)
		}
		return nil
	default:
		return errors.New("上游 source-transition 节点格式非法：" + schema)
	}
}

func upstreamSourceTransitionGenericDigestService(value *string) bool {
	return value != nil && validOpenAIReplayOOMRepairServiceSHA(*value)
}

func upstreamSourceTransitionGenericGitObjectService(value string) bool {
	return len(value) == 40 && strings.Trim(value, "0123456789abcdef") == ""
}

func upstreamSourceTransitionGenericGitOutputService(arguments ...string) (string, error) {
	command := exec.Command("git", arguments...)
	command.Dir = filepath.Join("../../..")
	raw, err := command.Output()
	return strings.TrimSpace(string(raw)), err
}

func upstreamSourceTransitionGenericGitAncestorService(before, after string) error {
	command := exec.Command("git", "merge-base", "--is-ancestor", before, after)
	command.Dir = filepath.Join("../../..")
	return command.Run()
}

func TestUpstreamSourceTransitionNodesAreFrozenService(t *testing.T) {
	for _, path := range upstreamSourceTransitionGenericNodePathsService(t) {
		node, err := readUpstreamSourceTransitionGenericNodeService(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateUpstreamSourceTransitionGenericNodeService(node); err != nil {
			t.Fatalf("%s：%v", filepath.Base(path), err)
		}
	}
}

func TestUpstreamSourceTransitionNodesRejectMutationService(t *testing.T) {
	mutatedDigest := sha256.Sum256([]byte("上游 source-transition 变异摘要"))
	for _, path := range upstreamSourceTransitionGenericNodePathsService(t) {
		node, err := readUpstreamSourceTransitionGenericNodeService(path)
		if err != nil {
			t.Fatal(err)
		}
		mutations := map[string]func(*upstreamSourceTransitionGenericNodeService){
			"路径越界": func(n *upstreamSourceTransitionGenericNodeService) { n.Entries[0].Path = "../越界" },
			"条目计数": func(n *upstreamSourceTransitionGenericNodeService) { n.EntryCount++ },
			"当前摘要": func(n *upstreamSourceTransitionGenericNodeService) {
				for index := range n.Entries {
					if n.Entries[index].CurrentSHA256 != nil {
						value := hex.EncodeToString(mutatedDigest[:])
						n.Entries[index].CurrentSHA256 = &value
						return
					}
				}
			},
			"签发时间": func(n *upstreamSourceTransitionGenericNodeService) {
				// v2 节点补写签发时间、v3 节点删去签发时间都必须被拒绝。
				if n.IssuedAtUTC == nil {
					value := "2026-10-01T00:00:00Z"
					n.IssuedAtUTC = &value
				} else {
					n.IssuedAtUTC = nil
				}
			},
			"当前提交": func(n *upstreamSourceTransitionGenericNodeService) { n.CurrentCommit = n.BaseCommit },
		}
		for name, mutate := range mutations {
			mutated := node
			mutated.Entries = append([]upstreamSourceTransitionGenericEntryService(nil), node.Entries...)
			mutate(&mutated)
			if err := validateUpstreamSourceTransitionGenericNodeService(mutated); err == nil {
				t.Fatalf("%s 的 %s 变异被错误接受", filepath.Base(path), name)
			}
		}
	}
}

func TestUpstreamSourceTransitionIssuedAtContractService(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	valid := "2026-10-01T11:59:00Z"
	future := "2026-10-01T12:01:00Z"
	malformed := "2026-10-01 11:59:00"
	cases := []struct {
		name     string
		schema   string
		issuedAt *string
		accept   bool
	}{
		{"v2 无签发时间", upstreamSourceTransitionGenericSchemaService, nil, true},
		{"v2 带签发时间", upstreamSourceTransitionGenericSchemaService, &valid, false},
		{"v3 带签发时间", upstreamSourceTransitionGenericSchemaV3Service, &valid, true},
		{"v3 缺签发时间", upstreamSourceTransitionGenericSchemaV3Service, nil, false},
		{"v3 签发时间晚于当前", upstreamSourceTransitionGenericSchemaV3Service, &future, false},
		{"v3 签发时间格式非法", upstreamSourceTransitionGenericSchemaV3Service, &malformed, false},
		{"未知格式", "official-egress-upstream-source-transition/v4", &valid, false},
	}
	for _, testCase := range cases {
		err := upstreamSourceTransitionGenericIssuedAtService(testCase.schema, testCase.issuedAt, now)
		if (err == nil) != testCase.accept {
			t.Fatalf("%s：期望接受=%v，实际错误=%v", testCase.name, testCase.accept, err)
		}
	}
}
