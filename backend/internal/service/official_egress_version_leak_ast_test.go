package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"unicode"
)

// 本测试用 Go AST 判定版本字面量的归属，取代基于正则与花括号配对的近似实现。
//
// §3.2 要求稳定执行引擎不包含按版本散落的常量，§4.3.2 要求新版本原则上只新增画像。
// 判断某个三段版本字面量是不是"泄漏"，取决于它属于谁：Codex/OpenAI 的配置写死版本是
// 泄漏，Anthropic 画像或其他供应商的版本则不在本规格范围内（§1.1）。
//
// 正则模型回答不了这个归属问题。它能配对花括号，却区分不了同一 ValueSpec 里的多个名称
// 与值、同一切片里的多个 CallExpr、同一行上的多个表达式——每补一条规则就在假绿和误报之间
// 摆动一次。AST 可以直接回答：这个字面量的父节点是哪个 CallExpr、属于 ValueSpec 的第几个
// 值、位于哪个 CompositeLit 字段。
//
// 分工：本测试负责版本字面量（含常量拼接与注释）；版本化符号名（Codex0145 之类）由
// tools/check_version_leak.py 的文本规则覆盖，那类模式不需要语法归属。

var (
	// 三段版本字面量。前后不能紧邻数字或点，避免匹配到四段版本或时间戳片段。
	versionLiteralRE = regexp.MustCompile(`(?:^|[^\d.])(\d+\.\d+\.\d+)(?:[^\d.]|$)`)

	// 归属证据：标识符里出现这些词表示该节点属于 Codex/OpenAI 出站配置。
	// Codex 侧保留子串匹配——codex／openai 足够长，且 PlatformOpenAI 分词后会被拆成
	// [platform, open, ai]，改成分词反而匹配不到。
	codexOwnerRE = regexp.MustCompile(`(?i)PlatformOpenAI|codex|openai`)

	// §1.1 已排除的其他供应商。按标识符分词后精确匹配，不能用子串：`xai` 是
	// `maxAIVersion` 的子串，子串匹配会把它当成 xAI 版本豁免，从而绕过两侧门禁。
	otherVendorTokens = map[string]bool{
		"anthropic":   true,
		"claude":      true,
		"stainless":   true,
		"grok":        true,
		"xai":         true,
		"gemini":      true,
		"antigravity": true,
		"localhost":   true,
	}

	// xAI 品牌缩写单独识别，不依赖通用 camelCase 分词——连续大写缩写相邻时分词会把
	// XAIHTTPClient、XAISSOClient、XAI2Client 粘连，得不到独立的 xai 词。
	//
	// 两个分支的边界要求不同，这是一个刻意的取舍：
	//   - 全大写 XAI：任何词边界都识别，因此 newXAIHTTPClient、buildXAI2Client 都命中；
	//   - 小写 xAI：只在标识符开头识别。`newxAIClient` 与 `maxAIVersion` 在词法上完全
	//     同形（都是"小写字母 + xAI"），支持前者必然误伤后者，而 maxAI/minAI 这类写法
	//     远比 newxAI 常见，且驼峰里的品牌缩写按惯例应写成 newXAI。
	// 因此 `newxAIClient` 这种写法归入已知限制：真出现时改名或走基线，不放宽判据。
	xaiBrandRE = regexp.MustCompile(`(?:^|[^A-Za-z])xAI(?:[A-Z0-9_]|$)|XAI(?:[A-Z0-9_]|$)`)

	// 与三段版本同形但不是版本号的内容：本地回环、内网地址。
	nonVersionLiteralRE = regexp.MustCompile(`^\d{1,3}(\.\d{1,3}){3}$|^v?\d+\.\d+\.\d+$`)

	// 版本快照文件按定义必须携带版本字面量，这是设计要求而非泄漏。
	versionSnapshotFileRE = regexp.MustCompile(`official_egress_codex_\d+_profile\.go$`)
)

const versionLeakASTBaselinePath = "testdata/official_egress_version_leak_ast.json"

// versionLeakEntry 记录一个指纹的出现次数与首次位置。基线只序列化次数，
// 行号仅用于报错时定位。
type versionLeakEntry struct {
	Count     int
	FirstLine int
}

// TestOfficialEgressVersionLeakAST 扫描生产代码中归属于 Codex/OpenAI 的版本命中，
// 并与基线比对。基线记录指纹而非计数：只比数量时，同一文件删一处旧泄漏、加一处新泄漏
// 即可蒙混过关。
func TestOfficialEgressVersionLeakAST(t *testing.T) {
	hits, err := scanOfficialEgressVersionLiterals("../..")
	if err != nil {
		t.Fatalf("扫描版本字面量失败：%v", err)
	}

	if os.Getenv("UPDATE_VERSION_LEAK_AST_BASELINE") != "" {
		writeVersionLeakASTBaseline(t, hits)
		// 重建后立即回读比对：只写不校验时，残留的旧指纹会继续被允许，
		// 同一处泄漏日后原样重新引入也不会报警。
		reloaded := readVersionLeakASTBaseline(t)
		if diff := diffVersionLeakBaseline(hits, reloaded); diff != "" {
			t.Fatalf("基线重建后与当前扫描不一致：%s", diff)
		}
		t.Logf("已更新 AST 版本泄漏基线：%d 个文件，合计 %d 处",
			len(hits), totalVersionLeakHits(hits))
		return
	}

	baseline := readVersionLeakASTBaseline(t)
	var failures []string
	for file, fingerprints := range hits {
		allowed := baseline[file]
		for fingerprint, entry := range fingerprints {
			if entry.Count > allowed[fingerprint] {
				failures = append(failures, fmt.Sprintf(
					"%s:%d 出现基线外的版本泄漏内容（指纹 %s，基线 %d 次、当前 %d 次）",
					file, entry.FirstLine, fingerprint, allowed[fingerprint], entry.Count,
				))
			}
		}
	}
	sort.Strings(failures)
	for _, failure := range failures {
		t.Errorf("🔴 %s", failure)
	}
	if len(failures) > 0 {
		t.Log("确认改动属实后，用 UPDATE_VERSION_LEAK_AST_BASELINE=1 go test 更新基线")
	}

	// 报告已下降的条目。基线只判"不得上升"，残留的旧指纹会一直被允许，
	// 必须显式提示收紧，否则同一处泄漏日后原样回归时门禁依然全绿。
	var improved []string
	for file, allowed := range baseline {
		current := hits[file]
		for fingerprint, permitted := range allowed {
			now := 0
			if current != nil && current[fingerprint] != nil {
				now = current[fingerprint].Count
			}
			if now < permitted {
				improved = append(improved, fmt.Sprintf(
					"%s 指纹 %s：基线 %d → 当前 %d", file, fingerprint, permitted, now,
				))
			}
		}
	}
	sort.Strings(improved)
	for _, item := range improved {
		t.Logf("⬇️  %s", item)
	}
	if len(improved) > 0 {
		t.Log("提示：命中已下降，可用 UPDATE_VERSION_LEAK_AST_BASELINE=1 go test 收紧基线")
	}
}

func totalVersionLeakHits(hits map[string]map[string]*versionLeakEntry) int {
	total := 0
	for _, fingerprints := range hits {
		for _, entry := range fingerprints {
			total += entry.Count
		}
	}
	return total
}

// diffVersionLeakBaseline 比对扫描结果与基线，返回首个差异描述。
func diffVersionLeakBaseline(
	hits map[string]map[string]*versionLeakEntry,
	baseline map[string]map[string]int,
) string {
	for file, fingerprints := range hits {
		for fingerprint, entry := range fingerprints {
			if baseline[file][fingerprint] != entry.Count {
				return fmt.Sprintf("%s 指纹 %s 当前 %d、基线 %d",
					file, fingerprint, entry.Count, baseline[file][fingerprint])
			}
		}
	}
	for file, allowed := range baseline {
		for fingerprint, permitted := range allowed {
			current := 0
			if hits[file] != nil && hits[file][fingerprint] != nil {
				current = hits[file][fingerprint].Count
			}
			if current != permitted {
				return fmt.Sprintf("%s 指纹 %s 基线残留 %d、当前 %d",
					file, fingerprint, permitted, current)
			}
		}
	}
	return ""
}

// scanOfficialEgressVersionLiterals 返回 文件 → 指纹 → 命中。
func scanOfficialEgressVersionLiterals(
	root string,
) (map[string]map[string]*versionLeakEntry, error) {
	hits := map[string]map[string]*versionLeakEntry{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			// ent 生成代码不由人编写，其版本常量与出站形态无关。
			if info.Name() == "ent" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") ||
			strings.HasSuffix(path, "_test.go") ||
			versionSnapshotFileRE.MatchString(path) {
			return nil
		}
		source, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		if isEgressContractOnlyBuildSource(source) {
			return nil
		}
		// 不做文本预筛：归属由 AST 的父节点判定，预筛反而会放过全新文件——一个只写
		// `Provider: PlatformOpenAI` 与 `Version: "0.146.0"` 的新文件既不含 officialCodex
		// 标记（AST 不解析），也不含版本化符号（文本门禁不匹配），两侧都查不到。
		fileHits, scanErr := scanFileVersionLiterals(path, source)
		if scanErr != nil {
			return scanErr
		}
		if len(fileHits) > 0 {
			// 规范成相对仓库根的路径，基线不受测试工作目录影响。
			relative, relErr := filepath.Rel(root, path)
			if relErr != nil {
				relative = path
			}
			hits["backend/"+filepath.ToSlash(relative)] = fileHits
		}
		return nil
	})
	return hits, err
}

func TestOfficialEgressVersionLeakASTBuildTagBoundary(t *testing.T) {
	tests := []struct {
		name string
		src  string
		want bool
	}{
		{name: "纯契约文件", src: "//go:build egresscontract\n\npackage fixture\n", want: true},
		{name: "生产或契约", src: "//go:build egresscontract || linux\n\npackage fixture\n", want: false},
		{name: "正文同形注释不算", src: "package fixture\n\n//go:build egresscontract\n", want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := isEgressContractOnlyBuildSource([]byte(test.src)); got != test.want {
				t.Fatalf("匹配结果=%v，期望 %v", got, test.want)
			}
		})
	}
}

func isEgressContractOnlyBuildSource(source []byte) bool {
	for _, rawLine := range bytes.Split(source, []byte{'\n'}) {
		line := strings.TrimSpace(strings.TrimSuffix(string(rawLine), "\r"))
		if strings.HasPrefix(line, "package ") {
			return false
		}
		// 只接受文件头中单一、精确的构建标签。组合表达式仍可能进入生产构建；
		// package 声明后的同形正文注释也不能获得豁免。
		if line == "//go:build egresscontract" {
			return true
		}
	}
	return false
}

func scanFileVersionLiterals(
	path string,
	source []byte,
) (map[string]*versionLeakEntry, error) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, source, parser.ParseComments)
	if err != nil {
		return nil, err
	}
	hits := map[string]*versionLeakEntry{}

	// 指纹取语义内容而不是起始行：跨行注释、多行拼接、多行原始字符串改动的往往是后续行，
	// 起始行纹丝不动——按起始行做指纹会让"等量替换"重新绕过门禁。行号只用于报错定位。
	//
	// 指纹还必须带归属上下文：只按 kind + 值做指纹时，把同一个版本从一处挪到另一处，
	// 指纹与次数都不变，"删一处旧泄漏、在新位置加一处同值泄漏"又能全绿。
	record := func(pos token.Pos, kind, content string, stack []ast.Node) {
		key := fingerprintContent(kind, ownerContext(stack), content)
		entry := hits[key]
		if entry == nil {
			entry = &versionLeakEntry{}
			hits[key] = entry
		}
		entry.Count++
		if entry.FirstLine == 0 {
			entry.FirstLine = fset.Position(pos).Line
		}
	}

	// 注释按整个 CommentGroup 判定：`// Codex version:` 与 `// 0.146.0` 分处两行时，
	// 逐行匹配两边都不命中，跨行写法因此能绕过文本规则。
	for _, group := range file.Comments {
		text := group.Text()
		if !codexOwnerRE.MatchString(text) || !versionLiteralRE.MatchString(text) {
			continue
		}
		record(group.Pos(), "comment", text, nil)
	}

	var stack []ast.Node
	ast.Inspect(file, func(node ast.Node) bool {
		if node == nil {
			if len(stack) > 0 {
				stack = stack[:len(stack)-1]
			}
			return true
		}
		stack = append(stack, node)

		// 常量字符串拼接整体求值：`"0.146." + "0"` 的两个 BasicLit 各自都不含完整三段
		// 版本，只看单个字面量会被绕过。求值后跳过子节点，避免重复计数。
		if binary, ok := node.(*ast.BinaryExpr); ok && binary.Op == token.ADD {
			if joined, resolved := staticStringConcat(binary); resolved {
				if isCodexVersionLiteral(joined) && literalBelongsToCodex(stack) {
					record(binary.Pos(), "concat", joined, stack)
				}
				stack = stack[:len(stack)-1]
				return false
			}
		}

		lit, ok := node.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		value, unquoteErr := strconv.Unquote(lit.Value)
		if unquoteErr != nil {
			value = lit.Value
		}
		if !isCodexVersionLiteral(value) {
			return true
		}
		if !literalBelongsToCodex(stack) {
			return true
		}
		record(lit.Pos(), "literal", value, stack)
		return true
	})
	return hits, nil
}

// identifierTokens 把标识符按 camelCase 与分隔符切词，供供应商匹配使用。
// 连续大写视为一个词，因此 XAIClient → [xai, client]，而 maxAIVersion → [max, ai, version]。
func identifierTokens(text string) []string {
	var tokens []string
	var current []rune
	flush := func() {
		if len(current) > 0 {
			tokens = append(tokens, strings.ToLower(string(current)))
			current = nil
		}
	}
	runes := []rune(text)
	for index, char := range runes {
		switch {
		case !unicode.IsLetter(char) && !unicode.IsDigit(char):
			flush()
		case unicode.IsUpper(char):
			if index > 0 && !unicode.IsUpper(runes[index-1]) {
				flush()
			} else if index+1 < len(runes) && unicode.IsLower(runes[index+1]) && len(current) > 1 {
				flush()
			}
			current = append(current, char)
		default:
			current = append(current, char)
		}
	}
	flush()
	// 品牌标准写法 xAI 会被切成 [x, ai]，合并成 xai；`maxAIVersion` 的 [max, ai]
	// 前一项不是单字母 x，不合并。
	merged := make([]string, 0, len(tokens))
	for index := 0; index < len(tokens); index++ {
		if tokens[index] == "x" && index+1 < len(tokens) && tokens[index+1] == "ai" {
			merged = append(merged, "xai")
			index++
			continue
		}
		merged = append(merged, tokens[index])
	}
	return merged
}

// hasOtherVendorToken 判断归属证据里是否出现其他供应商的完整词。
func hasOtherVendorToken(owner string) bool {
	// xAI 先按品牌边界匹配：通用分词在 XAIHTTPClient 这类连续缩写上不稳定。
	if xaiBrandRE.MatchString(owner) {
		return true
	}
	for _, token := range identifierTokens(owner) {
		if otherVendorTokens[token] {
			return true
		}
	}
	return false
}

// literalBelongsToCodex 从最近的语法父节点向外查找归属证据。
//
// 归属无法确认时按泄漏处理：宁可要求显式标注供应商，也不放过写死的 Codex 版本。
// 不按文件名兜底豁免——那等于取消了内容预筛，又用文件名恢复了另一种文件级跳过，
// 而仓库中本就存在大量以其他供应商命名的兼容文件。
func literalBelongsToCodex(stack []ast.Node) bool {
	for index := len(stack) - 1; index >= 0; index-- {
		owner, ambiguous := ownerEvidence(stack, index)
		if ambiguous {
			// 语法层无法确定该值归属谁（如多名称单值声明），保守判为泄漏。
			return true
		}
		if owner == "" {
			continue
		}
		// 同一节点同时带两类证据时 Codex/OpenAI 立即胜出，不再向外查找：
		// 否则 `func newAnthropicWrapper() { _ = T{Provider: PlatformOpenAI, ...} }`
		// 会被外层函数名覆盖成 Anthropic 归属。
		if codexOwnerRE.MatchString(owner) {
			return true
		}
		if hasOtherVendorToken(owner) {
			return false
		}
	}
	return true
}

// ownerEvidence 提取单个语法节点能提供的归属证据。
// 第二个返回值为真表示该节点的归属存在语法歧义，调用方应保守判为泄漏。
func ownerEvidence(stack []ast.Node, index int) (string, bool) {
	switch node := stack[index].(type) {
	case *ast.CallExpr:
		// 调用表达式按被调用者判定，同一切片里的兄弟调用互不影响。
		return exprIdentText(node.Fun), false
	case *ast.KeyValueExpr:
		return exprIdentText(node.Key), false
	case *ast.ValueSpec:
		return valueSpecOwnerName(node, stack, index)
	case *ast.CompositeLit:
		return compositeLitOwner(node), false
	case *ast.File:
		// 包名是最外层的归属证据：pkg/claude、pkg/xai 这类供应商包内的版本与 Codex
		// 出站无关，不应因为"找不到更近的证据"就默认判为泄漏。
		return node.Name.Name, false
	case *ast.FuncDecl:
		// 方法必须连同接收者类型一起判定：`func (c *AnthropicClient) Version() string`
		// 的归属写在接收者上，只看方法名会把它误判成 Codex 泄漏。
		parts := []string{node.Name.Name}
		if node.Recv != nil {
			for _, field := range node.Recv.List {
				parts = append(parts, exprIdentText(field.Type))
			}
		}
		return strings.Join(parts, " "), false
	}
	return "", false
}

func valueSpecOwnerName(spec *ast.ValueSpec, stack []ast.Node, at int) (string, bool) {
	// 只有名称与值一一对应时才能按序号归属。多名称单值（tuple 赋值、多返回值调用）
	// 在语法层无法确定哪个值对应哪个名称，例如
	// `var anthropicVersion, activeVersion = versions("2.1.220", "0.146.0")`——
	// 按位置映射会把两个字面量都归给第一个名称。这类声明按歧义处理。
	if len(spec.Names) != len(spec.Values) {
		if len(spec.Values) > 0 {
			return "", true
		}
		return "", false
	}
	var child ast.Node
	if at+1 < len(stack) {
		child = stack[at+1]
	}
	for position, value := range spec.Values {
		if child != nil && (value == child || astNodeContains(value, child)) {
			return spec.Names[position].Name, false
		}
	}
	if len(spec.Names) == 1 {
		return spec.Names[0].Name, false
	}
	return "", false
}

func astNodeContains(root, target ast.Node) bool {
	found := false
	ast.Inspect(root, func(node ast.Node) bool {
		if node == target {
			found = true
		}
		return !found
	})
	return found
}

// compositeLitOwner 用字面量的类型名与其标识符字段作为归属证据。
// 只取标识符值（如 Provider: PlatformAnthropic），字符串字段是数据不是证据。
func compositeLitOwner(lit *ast.CompositeLit) string {
	var parts []string
	if lit.Type != nil {
		parts = append(parts, exprIdentText(lit.Type))
	}
	for _, element := range lit.Elts {
		keyValue, ok := element.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		switch value := keyValue.Value.(type) {
		case *ast.Ident:
			parts = append(parts, value.Name)
		case *ast.SelectorExpr:
			parts = append(parts, exprIdentText(value))
		}
	}
	return strings.Join(parts, " ")
}

func exprIdentText(expr ast.Expr) string {
	switch node := expr.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.SelectorExpr:
		return exprIdentText(node.X) + "." + node.Sel.Name
	case *ast.StarExpr:
		return exprIdentText(node.X)
	case *ast.ArrayType:
		return exprIdentText(node.Elt)
	case *ast.UnaryExpr:
		return exprIdentText(node.X)
	case *ast.IndexExpr:
		return exprIdentText(node.X)
	case *ast.IndexListExpr:
		// 多类型参数的泛型调用：newAnthropicProfile[A, B](...)
		return exprIdentText(node.X)
	case *ast.ParenExpr:
		return exprIdentText(node.X)
	}
	return ""
}

// ownerContext 汇总父节点链上最内侧的若干层归属证据，作为指纹的一部分。
//
// 只取有限层数：太深会把整个文件的结构卷进指纹，任何无关改动都会让基线失效；
// 只取一层又不足以区分"同名字段在不同类型里"。三层足以区分调用者、字段与所属声明。
func ownerContext(stack []ast.Node) string {
	const maxLevels = 3
	var parts []string
	for index := len(stack) - 1; index >= 0 && len(parts) < maxLevels; index-- {
		owner, ambiguous := ownerEvidence(stack, index)
		if ambiguous {
			parts = append(parts, "<ambiguous>")
			continue
		}
		if owner == "" {
			continue
		}
		parts = append(parts, owner)
	}
	// 局部上下文封顶三层就够区分字段与调用者，但深层嵌套里三层可能全落在内层结构上：
	// 把同一段代码从 oldOwner() 挪到 newOwner()，三层上下文与语义值都不变，同值迁移
	// 又能绕过基线。这里固定补一个声明锚点，既能区分声明，又不会把整个文件结构卷进来。
	if anchor := declarationAnchor(stack); anchor != "" {
		parts = append(parts, "@"+anchor)
	}
	return strings.Join(parts, "|")
}

// declarationAnchor 返回该字面量的绑定名与所属函数，作为指纹的稳定锚点。
//
// 绑定名必须定位到"实际包含该字面量的那一项"：
//   - 分组 `var (...)` 固定取 Specs[0] 时，同组内两项之间迁移会得到相同锚点；
//   - 局部短赋值不进入指纹时，同一函数内改个变量名也不改指纹。
//
// 两种情况都会让"删一处旧泄漏、在新位置加一处同值泄漏"通过基线。
func declarationAnchor(stack []ast.Node) string {
	// 收集链上**每一层**绑定名，而不是只取最内层：匿名函数自身的绑定名写在外层
	// `oldFn := func() {...}` 上，只取最内层时两个 FuncLit 会得到相同锚点，
	// 把版本代码从一个匿名函数挪到另一个就能保持指纹不变。
	var bindings []string
	scope := ""
	for index := len(stack) - 1; index >= 0; index-- {
		switch node := stack[index].(type) {
		case *ast.ValueSpec:
			if name := valueSpecBindingNames(node, stack, index); name != "" {
				bindings = append(bindings, name)
			}
		case *ast.AssignStmt:
			if name := assignStmtBindingNames(node, stack, index); name != "" {
				bindings = append(bindings, name)
			}
		case *ast.KeyValueExpr:
			// 指纹定位用规范化后的键：路由表、处理器注册表把回调挂在键上，键就是槽位
			// 身份，否则两个 map 项之间迁移不会改变指纹。键不限于字符串字面量——
			// `routeKey("old"): func(){...}` 这类计算型键同样是槽位身份。
			// 归属判定仍不看字符串（见 compositeLitOwner），这里只用于定位。
			if text := anchorExprText(node.Key); text != "" {
				bindings = append(bindings, "key:"+text)
			}
		case *ast.CallExpr:
			// 回调直接作为实参时没有绑定名：`register("old", func(){...})`。
			// 用"被调用者 + 参数序号 + 该参数之前的静态字符串"定位槽位。
			if index+1 < len(stack) {
				if slot := callArgumentSlot(node, stack[index+1]); slot != "" {
					bindings = append(bindings, slot)
				}
			}
		case *ast.FuncDecl:
			parts := []string{node.Name.Name}
			if node.Recv != nil {
				for _, field := range node.Recv.List {
					parts = append(parts, exprIdentText(field.Type))
				}
			}
			scope = strings.Join(parts, " ")
		}
	}
	if scope != "" {
		bindings = append(bindings, scope)
	}
	return strings.Join(bindings, "@")
}

// anchorExprText 把表达式规范化成 Go 源码文本，用于指纹锚点。
//
// 与供应商归属用的 exprIdentText 刻意分开：归属只认标识符（字符串是数据，不是"这段
// 代码属于某供应商"的证据），而锚点需要索引键、字面量、计算式才能区分槽位——
// `handlers[routeKey("old")]` 与 `handlers[routeKey("new")]` 在归属上毫无差别，
// 在定位上却是两个位置。
//
// 实现刻意不做逐节点枚举。Go 表达式的形态是开放的（调用、二元、类型断言、切片、
// 复合字面量、泛型实例化…），switch 漏掉任何一种都会静默返回空串、槽位身份随之消失，
// 而这类遗漏只能靠一个个反例发现。改用 go/printer 输出规范化源码，覆盖面由语言本身
// 保证；长度封顶并附内容哈希，避免大段表达式撑爆指纹又不失去区分度。
func anchorExprText(expr ast.Expr) string {
	if expr == nil {
		return ""
	}
	var buffer bytes.Buffer
	if err := printer.Fprint(&buffer, token.NewFileSet(), expr); err != nil {
		return ""
	}
	// 不再对输出做空白折叠。go/printer 本身已给出稳定格式，而 strings.Fields 不理解
	// Go 字符串边界，会把字面量内部的连续空白也压掉——`"old  key"` 与 `"old key"`
	// 于是变成同一个锚点，两个槽位之间迁移不再改变指纹。
	text := buffer.String()
	const maxAnchorLength = 120
	if len(text) > maxAnchorLength {
		sum := sha256.Sum256([]byte(text))
		return text[:maxAnchorLength] + "~" + hex.EncodeToString(sum[:])[:8]
	}
	return text
}

// callArgumentSlot 返回该子节点在调用中的槽位标识，用于指纹锚点。
//
// 注册式 API 的回调没有绑定名，槽位身份写在"第几个参数"和"同一调用里的其他参数"上：
// `register("old", cb)` 与 `register("new", cb)` 必须区分开，否则把版本代码在两个回调
// 之间挪一下就能保持指纹不变。
//
// 必须扫描同一调用内的**全部**其他参数，不能只看前置：`register(cb, "old")` 的槽位键
// 写在回调之后；也不能只认字符串字面量，`register(oldKey, cb)` 的键是标识符。
// 范围限制在同一个 CallExpr 内，避免把无关的外层表达式卷进指纹。
func callArgumentSlot(call *ast.CallExpr, child ast.Node) string {
	for position, arg := range call.Args {
		if arg != child && !astNodeContains(arg, child) {
			continue
		}
		// 调用者也必须走锚点规范化：exprIdentText 是归属用的，会把
		// `registries["old"].Register` 归一成 `registries.Register`、把
		// `register[Old]` 归一成 `register`，索引与类型参数随之丢失。
		parts := []string{fmt.Sprintf("%s#arg%d", anchorExprText(call.Fun), position)}
		for other, expr := range call.Args {
			if other == position {
				continue
			}
			if text := anchorExprText(expr); text != "" {
				parts = append(parts, fmt.Sprintf("arg%d=%s", other, text))
			}
		}
		return strings.Join(parts, "#")
	}
	return ""
}

// valueSpecBindingNames 返回用于指纹锚点的绑定名。
//
// 与归属判定不同：归属遇到多名称单值时必须按歧义保守判泄漏，但锚点不能因此留空——
// 留空会让 `oldA, oldB := versions(...)` 与 `newA, newB := versions(...)` 得到相同指纹。
// 无法定位到具体名称时，退回全部名称的规范化组合。
func valueSpecBindingNames(spec *ast.ValueSpec, stack []ast.Node, at int) string {
	if name, ambiguous := valueSpecOwnerName(spec, stack, at); !ambiguous && name != "" {
		return name
	}
	names := make([]string, 0, len(spec.Names))
	for _, ident := range spec.Names {
		names = append(names, ident.Name)
	}
	return strings.Join(names, ",")
}

// assignStmtBindingNames 返回用于指纹锚点的赋值左值名。
//
// 不能复用归属用的 assignStmtOwnerName：那个函数走 exprIdentText，会把
// `handlers["old"]` 的索引丢成 `handlers`，两个键的赋值于是得到相同锚点。
// 锚点一律用 anchorExprText 重新规范化。
func assignStmtBindingNames(assign *ast.AssignStmt, stack []ast.Node, at int) string {
	var child ast.Node
	if at+1 < len(stack) {
		child = stack[at+1]
	}
	if len(assign.Lhs) == len(assign.Rhs) {
		for position, rhs := range assign.Rhs {
			if child != nil && (rhs == child || astNodeContains(rhs, child)) {
				if text := anchorExprText(assign.Lhs[position]); text != "" {
					return text
				}
			}
		}
	}
	// 无法定位到具体左值（多返回值等）时退回全部左值的组合。
	names := make([]string, 0, len(assign.Lhs))
	for _, expr := range assign.Lhs {
		if text := anchorExprText(expr); text != "" {
			names = append(names, text)
		}
	}
	return strings.Join(names, ",")
}

// isCodexVersionLiteral 判断字符串值里是否含三段版本，并排除与之同形的 IP 地址。
func isCodexVersionLiteral(value string) bool {
	if !versionLiteralRE.MatchString(value) {
		return false
	}
	if nonVersionLiteralRE.MatchString(value) && strings.Count(value, ".") == 3 {
		return false
	}
	return true
}

// staticStringConcat 递归求值由字符串字面量与 + 组成的常量表达式。
func staticStringConcat(expr ast.Expr) (string, bool) {
	switch node := expr.(type) {
	case *ast.BasicLit:
		if node.Kind != token.STRING {
			return "", false
		}
		value, err := strconv.Unquote(node.Value)
		if err != nil {
			return "", false
		}
		return value, true
	case *ast.ParenExpr:
		return staticStringConcat(node.X)
	case *ast.BinaryExpr:
		if node.Op != token.ADD {
			return "", false
		}
		left, leftOK := staticStringConcat(node.X)
		right, rightOK := staticStringConcat(node.Y)
		if !leftOK || !rightOK {
			return "", false
		}
		return left + right, true
	}
	return "", false
}

// fingerprintContent 按各字段的原始内容生成指纹。
//
// 用长度前缀编码而不是分隔符拼接：字段本身可能含任意字符（表达式里有引号、注释里有
// 换行），分隔符拼接会产生边界歧义。也不再做空白折叠——strings.Fields 不理解 Go 字符串
// 边界，会把字面量内部的连续空白压掉，让 `"old  key"` 与 `"old key"` 得到同一指纹。
// 表达式的格式稳定性已由 go/printer 保证，无需二次清洗。
func fingerprintContent(parts ...string) string {
	var buffer bytes.Buffer
	for _, part := range parts {
		fmt.Fprintf(&buffer, "%d:%s", len(part), part)
	}
	sum := sha256.Sum256(buffer.Bytes())
	return hex.EncodeToString(sum[:])[:16]
}

func readVersionLeakASTBaseline(t *testing.T) map[string]map[string]int {
	t.Helper()
	raw, err := os.ReadFile(versionLeakASTBaselinePath)
	if err != nil {
		t.Fatalf("读取 AST 版本泄漏基线失败：%v", err)
	}
	var payload struct {
		Files map[string]map[string]int `json:"files"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("解析 AST 版本泄漏基线失败：%v", err)
	}
	return payload.Files
}

func writeVersionLeakASTBaseline(t *testing.T, hits map[string]map[string]*versionLeakEntry) {
	t.Helper()
	payload := struct {
		Comment string                    `json:"_comment"`
		Files   map[string]map[string]int `json:"files"`
	}{
		Comment: "归属于 Codex/OpenAI 的版本命中指纹 → 次数。覆盖字符串字面量、常量拼接" +
			"与注释三类；指纹由「命中类型 + 归属上下文 + 语义内容」生成。" +
			"注意「改动归属位置会产生新指纹」只适用于 AST 表达式命中（字面量与拼接）：" +
			"注释按 CommentGroup 全文管理，同一段注释在文件内移动不会改变指纹。" +
			"归属由 go/ast 的父节点链判定，见 official_egress_version_leak_ast_test.go。" +
			"门禁禁止出现基线外的新指纹，也禁止同一指纹次数上升；" +
			"重建基线用 UPDATE_VERSION_LEAK_AST_BASELINE=1。",
		Files: flattenVersionLeakHits(hits),
	}
	encoded, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		t.Fatalf("编码基线失败：%v", err)
	}
	if err := os.MkdirAll(filepath.Dir(versionLeakASTBaselinePath), 0o755); err != nil {
		t.Fatalf("创建基线目录失败：%v", err)
	}
	if err := os.WriteFile(versionLeakASTBaselinePath, append(encoded, '\n'), 0o644); err != nil {
		t.Fatalf("写入基线失败：%v", err)
	}
}

// flattenVersionLeakHits 把带诊断信息的命中压成基线所需的"指纹 → 次数"。
func flattenVersionLeakHits(
	hits map[string]map[string]*versionLeakEntry,
) map[string]map[string]int {
	flat := map[string]map[string]int{}
	for file, fingerprints := range hits {
		counts := map[string]int{}
		for fingerprint, entry := range fingerprints {
			counts[fingerprint] = entry.Count
		}
		flat[file] = counts
	}
	return flat
}

func countVersionLeakHits(t *testing.T, name, src string) int {
	t.Helper()
	hits, err := scanFileVersionLiterals(name, []byte(src))
	if err != nil {
		t.Fatalf("解析样本失败：%v", err)
	}
	total := 0
	for _, entry := range hits {
		total += entry.Count
	}
	return total
}

func versionLeakFingerprintSet(t *testing.T, src string) map[string]struct{} {
	t.Helper()
	hits, err := scanFileVersionLiterals("probe.go", []byte(src))
	if err != nil {
		t.Fatalf("解析样本失败：%v", err)
	}
	set := map[string]struct{}{}
	for fingerprint := range hits {
		set[fingerprint] = struct{}{}
	}
	return set
}

// assertVersionLeakFingerprintChanged 断言两份样本不共享任何指纹。
func assertVersionLeakFingerprintChanged(t *testing.T, before, after string) {
	t.Helper()
	beforeSet := versionLeakFingerprintSet(t, before)
	afterSet := versionLeakFingerprintSet(t, after)
	if len(beforeSet) == 0 || len(afterSet) == 0 {
		t.Fatalf("样本未被检出：before=%d after=%d", len(beforeSet), len(afterSet))
	}
	for fingerprint := range beforeSet {
		if _, same := afterSet[fingerprint]; same {
			t.Errorf("同值迁移后指纹未变（%s），等量替换可绕过基线", fingerprint)
		}
	}
}

// TestOfficialEgressVersionLeakASTOwnership 用内联样本锁定归属判定本身。
//
// 这些样本是正则模型逐轮暴露出来的边界：同一物理行的兄弟记录、同一 ValueSpec 的多个
// 名称与值、同一切片里的多个 CallExpr。判断归属需要语法结构，按行、按花括号配对或按
// 同行文本都会在假绿与误报之间摆动。
func TestOfficialEgressVersionLeakASTOwnership(t *testing.T) {
	testCases := []struct {
		name string
		src  string
		want int
	}{
		{
			name: "多名称 const 各自归属",
			src:  "package p\nconst anthropicVersion, activeVersion = \"2.1.220\", \"0.146.0\"\n",
			want: 1,
		},
		{
			name: "同行混合调用只有 Anthropic 字面量",
			src: "package p\nvar profiles = []W{" +
				"newAnthropicWireProfile(\"a\", \"2.1.209\"), " +
				"newOpenAIWireProfile(\"o\", activeVersion)}\n",
			want: 0,
		},
		{
			name: "同行混合调用含 OpenAI 字面量",
			src: "package p\nvar profiles = []W{" +
				"newAnthropicWireProfile(\"a\", \"2.1.209\"), " +
				"newOpenAIWireProfile(\"o\", \"0.145.0\")}\n",
			want: 1,
		},
		{
			name: "同一物理行的兄弟记录",
			src: "package p\nvar profiles = []T{" +
				"{Provider: PlatformAnthropic, Version: \"2.1.220\"}, " +
				"{Kind: \"active\", Version: \"0.146.0\"}}\n",
			want: 1,
		},
		{
			name: "同行并存两类标识时 Codex 胜出",
			src: "package p\nvar profile = T{Provider: PlatformOpenAI, " +
				"LegacyProvider: PlatformAnthropic, Version: \"0.146.0\"}\n",
			want: 1,
		},
		{
			name: "混合字段的 Codex 证据不被外层覆盖",
			src: "package p\nfunc newAnthropicWrapper() {\n\t_ = T{" +
				"Provider: PlatformOpenAI, LegacyProvider: PlatformAnthropic, " +
				"Version: \"0.146.0\"}\n}\n",
			want: 1,
		},
		{
			name: "归属写在外层记录上",
			src: "package p\nvar builds = []T{{Provider: PlatformAnthropic, Version: \"2.1.220\", " +
				"RuntimeHeaders: []H{{Name: \"X-Stainless-Package-Version\", Value: \"0.94.0\"}}}}\n",
			want: 0,
		},
		{
			name: "归属写在函数签名上",
			src: "package p\nfunc newAnthropicOfficialEgressTLSProfile() *P {\n" +
				"\treturn &P{Name: \"Official Claude Code 2.1.220 HTTP\"}\n}\n",
			want: 0,
		},
		{
			name: "泛型多类型参数调用归属",
			src:  "package p\nvar profile = newAnthropicProfile[A, B](\"2.1.220\")\n",
			want: 0,
		},
		{
			name: "方法接收者提供归属",
			src: "package p\nfunc (c *AnthropicClient) Version() string {\n" +
				"\treturn \"2.1.220\"\n}\n",
			want: 0,
		},
		{
			name: "其他供应商包级常量",
			src:  "package p\nconst grokCLIStableVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "Anthropic 传输常量",
			src: "package p\nconst officialEgressTransportProfileAnthropicHTTP = " +
				"\"anthropic-http-claude-code-2.1.220-direct\"\n",
			want: 0,
		},
		{
			name: "常量字符串拼接不得绕过",
			src:  "package p\nvar activeVersion = \"0.146.\" + \"0\"\n",
			want: 1,
		},
		{
			name: "跨行注释中的 Codex 版本",
			src:  "package p\n\n// Codex version:\n// 0.146.0\nvar x = 1\n",
			want: 1,
		},
		{
			name: "跨行块注释中的 Codex 版本",
			src:  "package p\n\n/*\nCodex version:\n0.146.0\n*/\nvar x = 1\n",
			want: 1,
		},
		{
			name: "字符串内容不构成归属证据",
			src: "package p\nvar cfg = Config{URL: \"https://example.com/claude\", " +
				"Version: \"0.146.0\"}\n",
			want: 1,
		},
		{
			name: "供应商包名提供归属",
			src:  "package claude\n\nconst CLICurrentVersion = \"2.1.220\"\n",
			want: 0,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			got := countVersionLeakHits(t, "probe.go", testCase.src)
			if got != testCase.want {
				t.Errorf("归属判定不符：命中 %d，期望 %d\n%s", got, testCase.want, testCase.src)
			}
		})
	}
}

// TestOfficialEgressVersionLeakASTMultiLineFingerprint 锁定多行结构的指纹随内容变化。
//
// 指纹一度按节点起始行生成：跨行注释、多行拼接、多行原始字符串改动的往往是后续行，
// 起始行纹丝不动，于是"删一处旧版本、加一处新版本"这种等量替换又能全绿通过。
func TestOfficialEgressVersionLeakASTMultiLineFingerprint(t *testing.T) {
	testCases := []struct {
		name   string
		before string
		after  string
	}{
		{
			name:   "跨行行注释",
			before: "package p\n\n// Codex version:\n// 0.145.0\nvar x = 1\n",
			after:  "package p\n\n// Codex version:\n// 0.146.0\nvar x = 1\n",
		},
		{
			name:   "跨行块注释",
			before: "package p\n\n/*\nCodex version:\n0.145.0\n*/\nvar x = 1\n",
			after:  "package p\n\n/*\nCodex version:\n0.146.0\n*/\nvar x = 1\n",
		},
		{
			name:   "多行常量拼接",
			before: "package p\n\nvar activeVersion =\n\t\"0.145.\" +\n\t\"0\"\n",
			after:  "package p\n\nvar activeVersion =\n\t\"0.145.\" +\n\t\"1\"\n",
		},
		{
			name:   "多行原始字符串",
			before: "package p\n\nvar activeVersion = `\ncodex 0.145.0\n`\n",
			after:  "package p\n\nvar activeVersion = `\ncodex 0.146.0\n`\n",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			assertVersionLeakFingerprintChanged(t, testCase.before, testCase.after)
		})
	}
}

// TestOfficialEgressVersionLeakASTOwnershipFingerprint 锁定"同值换归属"必须改变指纹。
//
// 版本值相同但归属不同，是两处独立的泄漏。指纹一度只由 kind 与语义值构成，于是删掉一处
// 旧泄漏、在新位置写一处同版本泄漏时，指纹与次数都不变，基线全绿。
func TestOfficialEgressVersionLeakASTOwnershipFingerprint(t *testing.T) {
	testCases := []struct {
		name   string
		before string
		after  string
	}{
		{
			name:   "调用迁移到记录",
			before: "package p\n\nvar oldProfile = newOpenAIProfile(\"0.145.0\")\n",
			after: "package p\n\nvar newProfile = T{\n\tProvider: PlatformOpenAI,\n" +
				"\tVersion:  \"0.145.0\",\n}\n",
		},
		{
			name: "深层嵌套跨函数迁移",
			before: "package p\n\nfunc oldOwner() {\n\t_ = Outer{\n\t\tInner: Profile{\n" +
				"\t\t\tProvider: PlatformOpenAI,\n\t\t\tVersion:  \"0.145.0\",\n\t\t},\n\t}\n}\n",
			after: "package p\n\nfunc newOwner() {\n\t_ = Outer{\n\t\tInner: Profile{\n" +
				"\t\t\tProvider: PlatformOpenAI,\n\t\t\tVersion:  \"0.145.0\",\n\t\t},\n\t}\n}\n",
		},
		{
			name: "分组声明内迁移",
			before: "package p\n\nvar (\n\tanchor     = 1\n\toldProfile = Outer{\n" +
				"\t\tInner: Profile{\n\t\t\tProvider: PlatformOpenAI,\n" +
				"\t\t\tVersion:  \"0.145.0\",\n\t\t},\n\t}\n)\n",
			after: "package p\n\nvar (\n\tanchor     = 1\n\tnewProfile = Outer{\n" +
				"\t\tInner: Profile{\n\t\t\tProvider: PlatformOpenAI,\n" +
				"\t\t\tVersion:  \"0.145.0\",\n\t\t},\n\t}\n)\n",
		},
		{
			name: "同一函数内局部变量改名",
			before: "package p\n\nfunc f() {\n\toldProfile := \"codex 0.145.0\"\n" +
				"\t_ = oldProfile\n}\n",
			after: "package p\n\nfunc f() {\n\tnewProfile := \"codex 0.145.0\"\n" +
				"\t_ = newProfile\n}\n",
		},
		{
			name: "匿名函数之间迁移",
			before: "package p\n\nfunc f() {\n\toldFn := func() {\n" +
				"\t\tprofile := \"codex 0.145.0\"\n\t\t_ = profile\n\t}\n" +
				"\tnewFn := func() {}\n\t_, _ = oldFn, newFn\n}\n",
			after: "package p\n\nfunc f() {\n\toldFn := func() {}\n" +
				"\tnewFn := func() {\n\t\tprofile := \"codex 0.145.0\"\n" +
				"\t\t_ = profile\n\t}\n\t_, _ = oldFn, newFn\n}\n",
		},
		{
			name: "多返回值短赋值改名",
			before: "package p\n\nfunc f() {\n\toldA, oldB := versions(\"codex 0.145.0\")\n" +
				"\t_, _ = oldA, oldB\n}\n",
			after: "package p\n\nfunc f() {\n\tnewA, newB := versions(\"codex 0.145.0\")\n" +
				"\t_, _ = newA, newB\n}\n",
		},
		{
			name: "注册式回调在参数槽位间迁移",
			before: "package p\n\nfunc f() {\n\tregister(\"old\", func() {\n" +
				"\t\tprofile := \"codex 0.145.0\"\n\t\t_ = profile\n\t})\n" +
				"\tregister(\"new\", func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(\"old\", func() {})\n" +
				"\tregister(\"new\", func() {\n\t\tprofile := \"codex 0.145.0\"\n" +
				"\t\t_ = profile\n\t})\n}\n",
		},
		{
			name: "map 回调在字符串键间迁移",
			before: "package p\n\nvar handlers = map[string]func(){\n\t\"old\": func() {\n" +
				"\t\tprofile := \"codex 0.145.0\"\n\t\t_ = profile\n\t},\n" +
				"\t\"new\": func() {},\n}\n",
			after: "package p\n\nvar handlers = map[string]func(){\n\t\"old\": func() {},\n" +
				"\t\"new\": func() {\n\t\tprofile := \"codex 0.145.0\"\n" +
				"\t\t_ = profile\n\t},\n}\n",
		},
		{
			name: "槽位键位于回调之后",
			before: "package p\n\nfunc f() {\n\tregister(func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t}, \"old\")\n" +
				"\tregister(func() {}, \"new\")\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(func() {}, \"old\")\n" +
				"\tregister(func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t}, \"new\")\n}\n",
		},
		{
			name: "注册键由标识符传入",
			before: "package p\n\nfunc f() {\n\tregister(oldKey, func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(newKey, func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(oldKey, func() {})\n" +
				"\tregister(newKey, func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "map 索引赋值键迁移",
			before: "package p\n\nfunc f() {\n\thandlers[\"old\"] = func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t}\n" +
				"\thandlers[\"new\"] = func() {}\n}\n",
			after: "package p\n\nfunc f() {\n\thandlers[\"old\"] = func() {}\n" +
				"\thandlers[\"new\"] = func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t}\n}\n",
		},
		{
			name: "调用表达式作为注册键",
			before: "package p\n\nfunc f() {\n\tregister(routeKey(\"old\"), func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(routeKey(\"new\"), func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(routeKey(\"old\"), func() {})\n" +
				"\tregister(routeKey(\"new\"), func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "二元表达式作为注册键",
			before: "package p\n\nfunc f() {\n\tregister(prefix+\"old\", func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(prefix+\"new\", func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(prefix+\"old\", func() {})\n" +
				"\tregister(prefix+\"new\", func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "计算型 map 索引赋值",
			before: "package p\n\nfunc f() {\n\thandlers[routeKey(\"old\")] = func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t}\n" +
				"\thandlers[routeKey(\"new\")] = func() {}\n}\n",
			after: "package p\n\nfunc f() {\n\thandlers[routeKey(\"old\")] = func() {}\n" +
				"\thandlers[routeKey(\"new\")] = func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t}\n}\n",
		},
		{
			name: "泛型类型参数作为注册键",
			before: "package p\n\nfunc f() {\n\tregister(routeKey[Old](), func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(routeKey[New](), func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(routeKey[Old](), func() {})\n" +
				"\tregister(routeKey[New](), func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "计算型 map 字面量键",
			before: "package p\n\nvar handlers = map[string]func(){\n" +
				"\trouteKey(\"old\"): func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t},\n" +
				"\trouteKey(\"new\"): func() {},\n}\n",
			after: "package p\n\nvar handlers = map[string]func(){\n" +
				"\trouteKey(\"old\"): func() {},\n" +
				"\trouteKey(\"new\"): func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t},\n}\n",
		},
		{
			name: "类型断言作为注册键",
			before: "package p\n\nfunc f() {\n\tregister(oldKey.(string), func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(newKey.(string), func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(oldKey.(string), func() {})\n" +
				"\tregister(newKey.(string), func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "索引接收者作为调用者",
			before: "package p\n\nfunc f() {\n\tregistries[\"old\"].Register(func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregistries[\"new\"].Register(func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregistries[\"old\"].Register(func() {})\n" +
				"\tregistries[\"new\"].Register(func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "泛型调用者",
			before: "package p\n\nfunc f() {\n\tregister[Old](func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister[New](func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister[Old](func() {})\n" +
				"\tregister[New](func() {\n\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n}\n",
		},
		{
			name: "字符串键内部空白差异",
			before: "package p\n\nfunc f() {\n\tregister(\"old  key\", func() {\n" +
				"\t\tv := \"codex 0.145.0\"\n\t\t_ = v\n\t})\n" +
				"\tregister(\"old key\", func() {})\n}\n",
			after: "package p\n\nfunc f() {\n\tregister(\"old  key\", func() {})\n" +
				"\tregister(\"old key\", func() {\n\t\tv := \"codex 0.145.0\"\n" +
				"\t\t_ = v\n\t})\n}\n",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			assertVersionLeakFingerprintChanged(t, testCase.before, testCase.after)
		})
	}
}

// TestOfficialEgressVersionLeakASTScansUnmarkedFiles 锁定不带 Codex 标记的新文件也被扫描。
//
// 曾按文本预筛决定是否解析：一个只写 `Provider: PlatformOpenAI` 与版本字面量的新文件，
// 既无 officialCodex 标记（AST 跳过），也无版本化符号（文本门禁不匹配），两侧都查不到。
func TestOfficialEgressVersionLeakASTScansUnmarkedFiles(t *testing.T) {
	src := "package service\n\nvar activeProfile = Profile{\n" +
		"\tProvider: PlatformOpenAI,\n\tVersion:  \"0.146.0\",\n}\n"
	if countVersionLeakHits(t, "probe.go", src) == 0 {
		t.Error("不含 Codex 标记的 OpenAI 新文件未被检出，预筛会放过全新泄漏")
	}
}

// TestOfficialEgressVersionLeakASTVendorTokenBoundary 锁定供应商匹配按词而不按子串。
//
// `xai` 只有三个字母，子串匹配会命中 `maxAIVersion`、`MaxAIRetry` 这类普通标识符，
// 把归属不明的 Codex 版本当成 xAI 版本豁免——两侧门禁同时失效。反过来，品牌标准写法
// `xAI` 被切成 [x, ai] 时又会漏掉真正的 xAI 代码，在新增 xAI 代码时误阻断 CI。
func TestOfficialEgressVersionLeakASTVendorTokenBoundary(t *testing.T) {
	testCases := []struct {
		name string
		src  string
		want int
	}{
		{
			name: "maxAIVersion 不是 xAI",
			src:  "package service\n\nvar maxAIVersion = \"0.146.0\"\n",
			want: 1,
		},
		{
			name: "xaiVersion 是 xAI",
			src:  "package service\n\nvar xaiVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "XAIClient 是 xAI",
			src:  "package service\n\nvar XAIClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "品牌写法 xAIClient 是 xAI",
			src:  "package service\n\nvar xAIClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "XaiClient 是 xAI",
			src:  "package service\n\nvar XaiClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "XAIHTTPClient 是 xAI",
			src:  "package service\n\nvar XAIHTTPClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "品牌写法 xAIHTTPClient 是 xAI",
			src:  "package service\n\nvar xAIHTTPClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "XAISSOClient 是 xAI",
			src:  "package service\n\nvar XAISSOClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "XAI2Client 是 xAI",
			src:  "package service\n\nvar XAI2ClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "MaxAIRetry 不是 xAI",
			src:  "package service\n\nvar MaxAIRetryVersion = \"0.146.0\"\n",
			want: 1,
		},
		{
			name: "newXAIHTTPClient 是 xAI",
			src:  "package service\n\nvar newXAIHTTPClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "buildXAI2Client 是 xAI",
			src:  "package service\n\nvar buildXAI2ClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "officialXAISSOClient 是 xAI",
			src:  "package service\n\nvar officialXAISSOClientVersion = \"0.2.93\"\n",
			want: 0,
		},
		{
			name: "供应商命名文件不豁免归属不明的版本",
			src:  "package service\n\nvar activeVersion = \"0.146.0\"\n",
			want: 1,
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			got := countVersionLeakHits(t, "claude_bridge.go", testCase.src)
			if got != testCase.want {
				t.Errorf("命中 %d，期望 %d\n%s", got, testCase.want, testCase.src)
			}
		})
	}
}

// TestOfficialEgressVersionLeakASTAnchorExprCoverage 锁定锚点规范化不对常见表达式返回空。
//
// 早期实现是逐节点 switch：任何未列入的表达式静默返回空串，槽位身份随之消失，而这类
// 遗漏只能靠一个个反例发现（调用键、二元键、类型断言键、泛型实例化…都是这样暴露的）。
// 改用 go/printer 后覆盖面由语言本身保证，这条测试守住"不再出现静默空串"这个性质。
func TestOfficialEgressVersionLeakASTAnchorExprCoverage(t *testing.T) {
	expressions := []string{
		`ident`,
		`"literal"`,
		`pkg.Selector`,
		`handlers["key"]`,
		`handlers[routeKey("old")]`,
		`register(a, "b")`,
		`prefix + "suffix"`,
		`oldKey.(string)`,
		`Profile{Provider: PlatformOpenAI}`,
		`items[1:2]`,
		`-count`,
		`*pointer`,
		`(wrapped)`,
		`routeKey[Old]()`,
		`routeKey[Old, New]()`,
		`[]string{"a", "b"}`,
		`map[string]int{"k": 1}`,
		`func() {}`,
		`<-channel`,
		`&Profile{}`,
	}
	for _, source := range expressions {
		t.Run(source, func(t *testing.T) {
			expr, err := parser.ParseExpr(source)
			if err != nil {
				t.Fatalf("样本无法解析：%v", err)
			}
			if anchorExprText(expr) == "" {
				t.Errorf("锚点规范化返回空串，槽位身份会丢失：%s", source)
			}
		})
	}
}

// TestOfficialEgressVersionLeakASTAnchorDistinguishes 锁定不同表达式产生不同锚点。
//
// 覆盖测试只断言"不返回空串"，防不住"两个不同表达式规范化成同一个锚点"——那同样会让
// 等量迁移绕过基线，而且更隐蔽：锚点非空，看上去一切正常。这里直接比对锚点文本，
// 端到端的指纹变化则由 …OwnershipFingerprint 覆盖。
func TestOfficialEgressVersionLeakASTAnchorDistinguishes(t *testing.T) {
	pairs := [][2]string{
		{`registries["old"].Register`, `registries["new"].Register`},
		{`register[Old]`, `register[New]`},
		{`register[Old, Shared]`, `register[New, Shared]`},
		{`"old  key"`, `"old key"`},
		{`routeKey("old")`, `routeKey("new")`},
		{`prefix + "old"`, `prefix + "new"`},
		{`handlers[routeKey("old")]`, `handlers[routeKey("new")]`},
		{`oldKey.(string)`, `newKey.(string)`},
		{"`raw\nold`", "`raw old`"},
	}
	for _, pair := range pairs {
		t.Run(pair[0], func(t *testing.T) {
			left, err := parser.ParseExpr(pair[0])
			if err != nil {
				t.Fatalf("左样本无法解析：%v", err)
			}
			right, err := parser.ParseExpr(pair[1])
			if err != nil {
				t.Fatalf("右样本无法解析：%v", err)
			}
			leftText := anchorExprText(left)
			rightText := anchorExprText(right)
			if leftText == "" || rightText == "" {
				t.Fatalf("锚点为空：%q / %q", leftText, rightText)
			}
			if leftText == rightText {
				t.Errorf("两个不同表达式得到相同锚点 %q：%s vs %s",
					leftText, pair[0], pair[1])
			}
		})
	}
}
