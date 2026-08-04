// egressconflictinventory 生成变更集 5 的上游冲突变更单元清单。
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/scanner"
	"go/token"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

const (
	upstreamBase       = "26d894ef4f50645a4bf1030e378ac892f17d0223"
	observedRemoteHead = "825ca7b1fc9335f904bc077f051de815fb61e47f"
)

var conflictPaths = []string{
	"Dockerfile",
	"backend/cmd/server/wire_gen.go",
	"backend/internal/handler/openai_codex_models_handler.go",
	"backend/internal/handler/openai_gateway_handler.go",
	"backend/internal/handler/openai_live.go",
	"backend/internal/pkg/openaiidentity/codex.go",
	"backend/internal/pkg/tlsfingerprint/dialer.go",
	"backend/internal/platform/liveattestation/attestation.go",
	"backend/internal/repository/gateway_cache.go",
	"backend/internal/repository/openai_oauth_service.go",
	"backend/internal/repository/req_client_pool.go",
	"backend/internal/server/routes/gateway.go",
	"backend/internal/service/account_test_service.go",
	"backend/internal/service/account_usage_service.go",
	"backend/internal/service/openai_alpha_search.go",
	"backend/internal/service/openai_apikey_mimic_profile.go",
	"backend/internal/service/openai_codex_models_service.go",
	"backend/internal/service/openai_compact_probe.go",
	"backend/internal/service/openai_gateway_chat_completions.go",
	"backend/internal/service/openai_gateway_count_tokens.go",
	"backend/internal/service/openai_gateway_forward.go",
	"backend/internal/service/openai_gateway_messages.go",
	"backend/internal/service/openai_gateway_passthrough.go",
	"backend/internal/service/openai_images_responses.go",
	"backend/internal/service/openai_live.go",
	"backend/internal/service/openai_quota_service.go",
	"backend/internal/service/openai_upstream_http.go",
	"backend/internal/service/openai_ws_client.go",
	"backend/internal/service/openai_ws_forwarder_ingress.go",
	"backend/internal/service/openai_ws_forwarder_payload.go",
	"backend/internal/service/openai_ws_forwarder_support.go",
	"backend/internal/service/openai_ws_forwarder_v2.go",
	"backend/internal/service/openai_ws_http_bridge.go",
	"backend/internal/service/openai_ws_pool.go",
	"backend/internal/service/openai_ws_v2_passthrough_adapter.go",
	"backend/internal/service/wire.go",
}

var officialEgressSignal = regexp.MustCompile(
	`(?i)(officialcodex|codexegress|officialegress|openai.*official.*egress|` +
		`officialopenaihttpbodycontract|openaiforwardplan|internal/officialegress)`,
)

type declaration struct {
	Receiver  string
	Name      string
	Signature string
	AST       string
	Token     string
	Source    string
}

type lineRange struct {
	UpstreamStart int `json:"upstream_start"`
	UpstreamCount int `json:"upstream_count"`
	LocalStart    int `json:"local_start"`
	LocalCount    int `json:"local_count"`
}

type changeUnit struct {
	ID                            string      `json:"id"`
	Path                          string      `json:"path"`
	UnitType                      string      `json:"unit_type"`
	Receiver                      string      `json:"receiver"`
	Name                          string      `json:"name"`
	Signature                     string      `json:"signature"`
	UpstreamASTSHA256             string      `json:"upstream_ast_sha256"`
	UpstreamTokenSHA256           string      `json:"upstream_token_sha256"`
	PreRefactorLocalASTSHA256     string      `json:"pre_refactor_local_ast_sha256"`
	PreRefactorLocalTokenSHA256   string      `json:"pre_refactor_local_token_sha256"`
	UpstreamFileSHA256            string      `json:"upstream_file_sha256"`
	PreRefactorLocalFileSHA256    string      `json:"pre_refactor_local_file_sha256"`
	NormalizedDiffSHA256          string      `json:"normalized_diff_sha256"`
	HunkSHA256                    string      `json:"hunk_sha256,omitempty"`
	HunkRanges                    []lineRange `json:"hunk_ranges,omitempty"`
	OfficialEgressOwnership       string      `json:"official_egress_ownership"`
	AllowedFinalState             string      `json:"allowed_final_state"`
	MigrationTargetOrRetainReason string      `json:"migration_target_or_retain_reason"`
	Generated                     bool        `json:"generated"`
}

type inventory struct {
	SchemaVersion              string       `json:"schema_version"`
	InventoryKind              string       `json:"inventory_kind"`
	ClassificationUpstreamBase string       `json:"classification_upstream_base"`
	ObservedRemoteHead         string       `json:"observed_remote_head"`
	ConflictFileCount          int          `json:"conflict_file_count"`
	UnitCount                  int          `json:"unit_count"`
	Units                      []changeUnit `json:"units"`
	Rules                      []string     `json:"rules"`
}

type receipt struct {
	SchemaVersion              string `json:"schema_version"`
	Changeset                  string `json:"changeset"`
	FullInventoryPath          string `json:"full_inventory_path"`
	FullInventorySHA256        string `json:"full_inventory_sha256"`
	FullUnitCount              int    `json:"full_unit_count"`
	GovernableInventoryPath    string `json:"governable_inventory_path"`
	GovernableInventorySHA256  string `json:"governable_inventory_sha256"`
	GovernableUnitCount        int    `json:"governable_unit_count"`
	StrictSubset               bool   `json:"governable_is_strict_subset"`
	ClassificationUpstreamBase string `json:"classification_upstream_base"`
}

func main() {
	output := flag.String("output", "docs/changeset5/conflict-inventory", "输出目录（相对仓库根或绝对路径）")
	flag.Parse()
	root, err := repositoryRoot()
	check(err)
	units, err := buildUnits(root)
	check(err)
	full := inventory{
		SchemaVersion:              "changeset5-conflict-unit-inventory/v1",
		InventoryKind:              "full_upstream_overlap",
		ClassificationUpstreamBase: upstreamBase, ObservedRemoteHead: observedRemoteHead,
		ConflictFileCount: len(conflictPaths), UnitCount: len(units), Units: units,
		Rules: []string{
			"三类历史描述已改为 file_added、declaration_added、declaration_modified、file_structure_or_non_go_modified 四类变更单元",
			"同一文件可同时拥有多个类别；declaration 包含函数、方法、类型、变量和常量",
			"上游 declaration 删除或重命名仍登记为 declaration_modified，不计入冲突减少",
			"wire_gen.go 只能由生成器重建；非 Go 文件使用 hunk 摘要与行区间所有权",
		},
	}
	governableUnits := make([]changeUnit, 0, len(units))
	for _, unit := range units {
		if unit.OfficialEgressOwnership != "non_official" {
			governableUnits = append(governableUnits, unit)
		}
	}
	governable := inventory{
		SchemaVersion:              "changeset5-conflict-unit-inventory/v1",
		InventoryKind:              "official_egress_governable_subset",
		ClassificationUpstreamBase: upstreamBase, ObservedRemoteHead: observedRemoteHead,
		ConflictFileCount: countPaths(governableUnits), UnitCount: len(governableUnits), Units: governableUnits,
		Rules: []string{
			"本清单必须是全量冲突清单的严格子集",
			"exclusive 单元可移动或删除；mixed 单元只允许收缩官方出站片段并冻结非官方 fork 片段",
			"任何新增被修改的上游 declaration 都必须失败",
		},
	}
	if len(governable.Units) == 0 || len(governable.Units) >= len(full.Units) {
		check(errors.New("可治理清单不是全量冲突清单的严格非空子集"))
	}
	out := *output
	if !filepath.IsAbs(out) {
		out = filepath.Join(root, filepath.FromSlash(out))
	}
	check(os.MkdirAll(out, 0o755))
	fullRaw := marshal(full)
	governableRaw := marshal(governable)
	check(os.WriteFile(filepath.Join(out, "full.json"), fullRaw, 0o644))
	check(os.WriteFile(filepath.Join(out, "governable.json"), governableRaw, 0o644))
	relativeOutput, err := filepath.Rel(root, out)
	check(err)
	relativeOutput = filepath.ToSlash(relativeOutput)
	receiptValue := receipt{
		SchemaVersion: "changeset5-conflict-unit-inventory-receipt/v1", Changeset: "5",
		FullInventoryPath:   relativeOutput + "/full.json",
		FullInventorySHA256: digest(fullRaw), FullUnitCount: len(full.Units),
		GovernableInventoryPath:   relativeOutput + "/governable.json",
		GovernableInventorySHA256: digest(governableRaw), GovernableUnitCount: len(governable.Units),
		StrictSubset: true, ClassificationUpstreamBase: upstreamBase,
	}
	check(os.WriteFile(filepath.Join(out, "receipt.json"), marshal(receiptValue), 0o644))
	fmt.Printf("全量冲突单元=%d，可治理单元=%d，冲突文件=%d\n", len(full.Units), len(governable.Units), len(conflictPaths))
}

func repositoryRoot() (string, error) {
	cmd := exec.Command("git", "rev-parse", "--show-toplevel")
	raw, err := cmd.Output()
	return strings.TrimSpace(string(raw)), err
}

func buildUnits(root string) ([]changeUnit, error) {
	paths := append([]string(nil), conflictPaths...)
	sort.Strings(paths)
	units := make([]changeUnit, 0)
	for _, path := range paths {
		local, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(path)))
		if err != nil {
			return nil, fmt.Errorf("读取本地冲突文件 %s：%w", path, err)
		}
		upstream, upstreamExists, err := gitShow(root, upstreamBase, path)
		if err != nil {
			return nil, err
		}
		if !upstreamExists {
			units = append(units, newFileUnit(path, local))
			continue
		}
		if filepath.Ext(path) != ".go" {
			unit, err := nonGoUnit(root, path, upstream, local)
			if err != nil {
				return nil, err
			}
			units = append(units, unit)
			continue
		}
		fileUnits, err := goFileUnits(path, upstream, local)
		if err != nil {
			return nil, err
		}
		units = append(units, fileUnits...)
	}
	sort.Slice(units, func(i, j int) bool { return units[i].ID < units[j].ID })
	return units, nil
}

func gitShow(root, commit, path string) ([]byte, bool, error) {
	cmd := exec.Command("git", "show", commit+":"+path)
	cmd.Dir = root
	raw, err := cmd.Output()
	if err == nil {
		return raw, true, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return nil, false, nil
	}
	return nil, false, fmt.Errorf("读取上游 %s：%w", path, err)
}

func newFileUnit(path string, local []byte) changeUnit {
	ownership := ownershipFor("", string(local), true)
	unit := changeUnit{
		Path: path, UnitType: "file_added", Name: "<file>", Signature: path,
		UpstreamASTSHA256: "missing", UpstreamTokenSHA256: "missing",
		PreRefactorLocalASTSHA256: digest(local), PreRefactorLocalTokenSHA256: tokenDigest(local),
		UpstreamFileSHA256: "missing", PreRefactorLocalFileSHA256: digest(local),
		NormalizedDiffSHA256:    digest([]byte("missing\x00" + digest(local))),
		OfficialEgressOwnership: ownership, Generated: isGenerated(path),
	}
	setFinalPolicy(&unit)
	unit.ID = unitIdentity(unit)
	return unit
}

func nonGoUnit(root, path string, upstream, local []byte) (changeUnit, error) {
	patch, err := gitDiff(root, path)
	if err != nil {
		return changeUnit{}, err
	}
	unit := changeUnit{
		Path: path, UnitType: "file_structure_or_non_go_modified", Name: "<non_go_file>", Signature: path,
		UpstreamASTSHA256: digest(upstream), UpstreamTokenSHA256: tokenDigest(upstream),
		PreRefactorLocalASTSHA256: digest(local), PreRefactorLocalTokenSHA256: tokenDigest(local),
		UpstreamFileSHA256: digest(upstream), PreRefactorLocalFileSHA256: digest(local),
		NormalizedDiffSHA256: digest([]byte(digest(upstream) + "\x00" + digest(local))),
		HunkSHA256:           digest(patch), HunkRanges: parseHunkRanges(patch),
		OfficialEgressOwnership: ownershipFor(string(upstream), string(local), false),
		Generated:               isGenerated(path),
	}
	setFinalPolicy(&unit)
	unit.ID = unitIdentity(unit)
	return unit, nil
}

func gitDiff(root, path string) ([]byte, error) {
	cmd := exec.Command("git", "diff", "--unified=0", upstreamBase, "--", path)
	cmd.Dir = root
	return cmd.Output()
}

func goFileUnits(path string, upstream, local []byte) ([]changeUnit, error) {
	upDecls, upStructure, err := parseDeclarations(path, upstream)
	if err != nil {
		return nil, fmt.Errorf("解析上游 %s：%w", path, err)
	}
	localDecls, localStructure, err := parseDeclarations(path, local)
	if err != nil {
		return nil, fmt.Errorf("解析本地 %s：%w", path, err)
	}
	upFileDigest, localFileDigest := digest(upstream), digest(local)
	keys := make(map[string]bool, len(upDecls)+len(localDecls))
	for key := range upDecls {
		keys[key] = true
	}
	for key := range localDecls {
		keys[key] = true
	}
	ordered := make([]string, 0, len(keys))
	for key := range keys {
		ordered = append(ordered, key)
	}
	sort.Strings(ordered)
	units := make([]changeUnit, 0)
	for _, key := range ordered {
		up, hasUp := upDecls[key]
		current, hasLocal := localDecls[key]
		if hasUp && hasLocal && up.AST == current.AST {
			continue
		}
		unitType := "declaration_modified"
		if !hasUp {
			unitType = "declaration_added"
		}
		selected := current
		if !hasLocal {
			selected = up
		}
		unit := changeUnit{
			Path: path, UnitType: unitType, Receiver: selected.Receiver,
			Name: selected.Name, Signature: selected.Signature,
			UpstreamASTSHA256:           missingOrDigest(hasUp, up.AST),
			UpstreamTokenSHA256:         missingOrDigest(hasUp, up.Token),
			PreRefactorLocalASTSHA256:   missingOrDigest(hasLocal, current.AST),
			PreRefactorLocalTokenSHA256: missingOrDigest(hasLocal, current.Token),
			UpstreamFileSHA256:          upFileDigest, PreRefactorLocalFileSHA256: localFileDigest,
			OfficialEgressOwnership: ownershipFor(up.Source, current.Source, !hasUp),
			Generated:               isGenerated(path),
		}
		unit.NormalizedDiffSHA256 = digest([]byte(unit.UpstreamASTSHA256 + "\x00" + unit.PreRefactorLocalASTSHA256))
		setFinalPolicy(&unit)
		if hasUp && !hasLocal {
			unit.AllowedFinalState = "deletion_or_rename_requires_explicit_review"
			unit.MigrationTargetOrRetainReason = "上游 declaration 已删除或重命名，不计入冲突减少，必须保持显式登记"
		}
		unit.ID = unitIdentity(unit)
		units = append(units, unit)
	}
	if upStructure != localStructure || (len(units) == 0 && upFileDigest != localFileDigest) {
		unit := changeUnit{
			Path: path, UnitType: "file_structure_or_non_go_modified", Name: "<file_structure>",
			Signature:         "package/import/build-tag/comment structure",
			UpstreamASTSHA256: digest([]byte(upStructure)), UpstreamTokenSHA256: tokenDigest([]byte(upStructure)),
			PreRefactorLocalASTSHA256: digest([]byte(localStructure)), PreRefactorLocalTokenSHA256: tokenDigest([]byte(localStructure)),
			UpstreamFileSHA256: upFileDigest, PreRefactorLocalFileSHA256: localFileDigest,
			NormalizedDiffSHA256:    digest([]byte(upStructure + "\x00" + localStructure)),
			OfficialEgressOwnership: "mixed", Generated: isGenerated(path),
		}
		setFinalPolicy(&unit)
		unit.ID = unitIdentity(unit)
		units = append(units, unit)
	}
	return units, nil
}

func parseDeclarations(path string, raw []byte) (map[string]declaration, string, error) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, raw, parser.ParseComments)
	if err != nil {
		return nil, "", err
	}
	result := make(map[string]declaration)
	imports := make([]string, 0, len(file.Imports))
	for _, item := range file.Imports {
		alias := ""
		if item.Name != nil {
			alias = item.Name.Name
		}
		imports = append(imports, alias+"\x00"+item.Path.Value)
	}
	sort.Strings(imports)
	structure := file.Name.Name + "\n" + strings.Join(imports, "\n")
	for _, declNode := range file.Decls {
		switch decl := declNode.(type) {
		case *ast.FuncDecl:
			receiver := ""
			if decl.Recv != nil && len(decl.Recv.List) > 0 {
				receiver = formatNode(fset, decl.Recv.List[0].Type)
			}
			signature := "func " + receiver + "." + decl.Name.Name + formatNode(fset, decl.Type)
			addDeclaration(result, receiver, decl.Name.Name, signature, formatNode(fset, decl))
		case *ast.GenDecl:
			if decl.Tok == token.IMPORT {
				continue
			}
			for _, specNode := range decl.Specs {
				switch spec := specNode.(type) {
				case *ast.TypeSpec:
					source := decl.Tok.String() + " " + formatNode(fset, spec)
					addDeclaration(result, "", spec.Name.Name, sourceSignature(decl.Tok.String(), spec.Name.Name, spec.Type), source)
				case *ast.ValueSpec:
					source := decl.Tok.String() + " " + formatNode(fset, spec)
					for _, name := range spec.Names {
						addDeclaration(result, "", name.Name, decl.Tok.String()+" "+name.Name, source)
					}
				}
			}
		}
	}
	return result, structure, nil
}

func sourceSignature(kind, name string, expr ast.Expr) string {
	return kind + " " + name + " " + fmt.Sprintf("%T", expr)
}

func addDeclaration(result map[string]declaration, receiver, name, signature, source string) {
	// Go 的同一作用域不允许 declaration 重载，因此 receiver 与名称足以构成稳定身份。
	// 签名属于被比较内容，不能放入身份键；否则签名变化会被错误拆成删除和新增。
	key := receiver + "\x00" + name
	result[key] = declaration{
		Receiver: receiver, Name: name, Signature: signature,
		AST: source, Token: canonicalTokens([]byte(source)), Source: source,
	}
}

func formatNode(fset *token.FileSet, node any) string {
	var buffer bytes.Buffer
	if err := format.Node(&buffer, fset, node); err != nil {
		panic(err)
	}
	return buffer.String()
}

func canonicalTokens(raw []byte) string {
	fset := token.NewFileSet()
	file := fset.AddFile("unit.go", fset.Base(), len(raw))
	var scan scanner.Scanner
	scan.Init(file, raw, nil, scanner.ScanComments)
	var parts []string
	for {
		_, tok, literal := scan.Scan()
		if tok == token.EOF {
			break
		}
		parts = append(parts, tok.String()+"\x00"+literal)
	}
	return strings.Join(parts, "\n")
}

func tokenDigest(raw []byte) string { return digest([]byte(canonicalTokens(raw))) }

func ownershipFor(upstream, local string, added bool) string {
	localHit := officialEgressSignal.MatchString(local)
	upstreamHit := officialEgressSignal.MatchString(upstream)
	if !localHit && !upstreamHit {
		return "non_official"
	}
	if added || (localHit && strings.TrimSpace(upstream) == "") {
		return "official_egress_exclusive"
	}
	return "mixed"
}

func setFinalPolicy(unit *changeUnit) {
	if unit.Generated {
		unit.AllowedFinalState = "regenerate_only"
		unit.MigrationTargetOrRetainReason = "生成文件只能由声明的生成器确定性重建，禁止人工编辑"
		return
	}
	switch unit.OfficialEgressOwnership {
	case "official_egress_exclusive":
		unit.AllowedFinalState = "move_delete_or_retain_in_fork_owned_package"
		unit.MigrationTargetOrRetainReason = "迁入 internal/officialegress 或相应 fork 独立文件；版本证据值可原位保留"
	case "mixed":
		unit.AllowedFinalState = "retain_thin_hook_and_freeze_non_official_fragment"
		unit.MigrationTargetOrRetainReason = "共享 declaration 只保留 Snapshot、Plan 和 Executor 薄钩子；非官方 fork 片段不得变化"
	default:
		unit.AllowedFinalState = "preserve_exact_pre_refactor_local_declaration"
		unit.MigrationTargetOrRetainReason = "不属于变更集 5 可治理范围，规范化本地 declaration 必须保持不变"
	}
}

func isGenerated(path string) bool { return path == "backend/cmd/server/wire_gen.go" }

func unitIdentity(unit changeUnit) string {
	return strings.Join([]string{unit.Path, unit.UnitType, unit.Receiver, unit.Name, unit.Signature}, "\x00")
}

var hunkPattern = regexp.MustCompile(`(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

func parseHunkRanges(patch []byte) []lineRange {
	matches := hunkPattern.FindAllStringSubmatch(string(patch), -1)
	result := make([]lineRange, 0, len(matches))
	for _, match := range matches {
		result = append(result, lineRange{
			UpstreamStart: atoi(match[1]), UpstreamCount: countOrOne(match[2]),
			LocalStart: atoi(match[3]), LocalCount: countOrOne(match[4]),
		})
	}
	return result
}

func atoi(value string) int {
	var result int
	_, _ = fmt.Sscanf(value, "%d", &result)
	return result
}

func countOrOne(value string) int {
	if value == "" {
		return 1
	}
	return atoi(value)
}

func missingOrDigest(present bool, value string) string {
	if !present {
		return "missing"
	}
	return digest([]byte(value))
}

func countPaths(units []changeUnit) int {
	seen := make(map[string]bool)
	for _, unit := range units {
		seen[unit.Path] = true
	}
	return len(seen)
}

func marshal(value any) []byte {
	raw, err := json.MarshalIndent(value, "", "  ")
	check(err)
	return append(raw, '\n')
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func check(err error) {
	if err != nil && !errors.Is(err, io.EOF) {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
