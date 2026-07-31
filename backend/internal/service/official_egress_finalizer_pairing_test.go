package service

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 画像端点的 attach 与终态定型必须逐调用点配对。漏调定型会发出一个 header 线序不符
// 合画像的请求，而单元测试通常盖不到——只有抓包比对才看得出。本文件用 Go AST 锁住
// 现有正确实现，替代成本更高的统一执行器重构。
//
// 分析器刻意避开三种会造成假绿的弱实现：
//   - 文件级共现：files.go 有两组独立调用，live.go 同时有 HTTP 与 WS 调用，
//     按文件判断时删掉其中一组仍会通过；
//   - 函数级集合：包装函数只要调用过任意 finalizer 就整体算“已定型”，
//     即使它定型的是另一个 context、传入的 egressContext 从未被使用；
//   - 计数比较：body 定型与回写只比较次数时，删掉 finalizer 反而使计数关系成立。
const (
	officialCodexHTTPAttachFunc  = "attachOfficialCodex0145EndpointRequest"
	officialCodexWSAttachFunc    = "attachOfficialCodex0145EndpointWebSocketContext"
	officialCodexHeaderFinalizer = "officialCodex0145FinalizeEndpointHeaders"
	officialCodexBodyFinalizer   = "officialCodex0145FinalizeEndpointJSONBody"
	officialCodexBodyReset       = "resetOfficialEgressRequestBody"
)

// attachCallSite 描述一个画像 attach 调用点及其定型配对结论。
type attachCallSite struct {
	File      string
	Function  string
	Kind      string // http | ws
	Line      int
	Context   string // 接收 egressContext 的变量名；无法静态追踪时为空
	Finalized bool
	Reason    string
}

// key 是清单断言用的稳定标识，刻意不含行号，避免无关改动导致基线漂移。
func (s attachCallSite) key() string {
	return fmt.Sprintf("%s|%s|%s", s.File, s.Function, s.Kind)
}

func (s attachCallSite) String() string {
	return fmt.Sprintf("%s:%d %s() [%s] context=%q %s",
		s.File, s.Line, s.Function, s.Kind, s.Context, s.Reason)
}

// bodyCallSite 描述一个 JSON body 定型或请求体回写调用点。
type bodyCallSite struct {
	File     string
	Function string
	Kind     string // finalize | reset
	Line     int
}

func (s bodyCallSite) key() string {
	return fmt.Sprintf("%s|%s|%s", s.File, s.Function, s.Kind)
}

func calleeName(call *ast.CallExpr) string {
	switch fun := call.Fun.(type) {
	case *ast.Ident:
		return fun.Name
	case *ast.SelectorExpr:
		return fun.Sel.Name
	}
	return ""
}

func attachKind(name string) string {
	switch name {
	case officialCodexHTTPAttachFunc:
		return "http"
	case officialCodexWSAttachFunc:
		return "ws"
	}
	return ""
}

// paramIndex 返回 name 在函数形参列表中的下标；receiver 不计入，与调用点实参下标对齐。
func paramIndex(fn *ast.FuncDecl, name string) int {
	if fn.Type == nil || fn.Type.Params == nil {
		return -1
	}
	index := 0
	for _, field := range fn.Type.Params.List {
		if len(field.Names) == 0 {
			index++
			continue
		}
		for _, ident := range field.Names {
			if ident.Name == name {
				return index
			}
			index++
		}
	}
	return -1
}

// finalizedParams 记录「函数名 → 确实完成 header 定型的形参下标集合」。
type finalizedParams map[string]map[int]bool

func (p finalizedParams) has(function string, index int) bool {
	indexes, ok := p[function]
	return ok && indexes[index]
}

func (p finalizedParams) add(function string, index int) bool {
	if p.has(function, index) {
		return false
	}
	if p[function] == nil {
		p[function] = make(map[int]bool)
	}
	p[function][index] = true
	return true
}

// finalizedArgIndexes 返回调用 call 时「被真正定型」的实参下标集合。
// 直接调用 header finalizer 时是首参；调用包装函数时按已知的形参位置展开。
func finalizedArgIndexes(call *ast.CallExpr, known finalizedParams) []int {
	name := calleeName(call)
	if name == officialCodexHeaderFinalizer {
		if len(call.Args) > 0 {
			return []int{0}
		}
		return nil
	}
	indexes, ok := known[name]
	if !ok {
		return nil
	}
	result := make([]int, 0, len(indexes))
	for index := range indexes {
		if index < len(call.Args) {
			result = append(result, index)
		}
	}
	return result
}

// shadowedNames 返回函数体内被重新声明的标识符。传播按标识符文本匹配，内层作用域
// 一旦遮蔽同名形参就可能认错对象，因此对被遮蔽的名字保守地放弃传播。
func shadowedNames(body *ast.BlockStmt) map[string]bool {
	shadowed := make(map[string]bool)
	ast.Inspect(body, func(node ast.Node) bool {
		switch stmt := node.(type) {
		case *ast.AssignStmt:
			if stmt.Tok != token.DEFINE {
				return true
			}
			for _, lhs := range stmt.Lhs {
				if ident, ok := lhs.(*ast.Ident); ok && ident.Name != "_" {
					shadowed[ident.Name] = true
				}
			}
		case *ast.ValueSpec:
			for _, ident := range stmt.Names {
				shadowed[ident.Name] = true
			}
		}
		return true
	})
	return shadowed
}

// collectFinalizedParams 按参数位置做不动点传播，识别真正把某个形参交付定型的包装
// 函数。只看「函数调用过 finalizer」是不够的：它可能定型的是另一个 context。
func collectFinalizedParams(files map[string]*ast.File) finalizedParams {
	known := make(finalizedParams)
	for {
		grew := false
		for _, file := range files {
			for _, decl := range file.Decls {
				fn, ok := decl.(*ast.FuncDecl)
				if !ok || fn.Body == nil {
					continue
				}
				shadowed := shadowedNames(fn.Body)
				ast.Inspect(fn.Body, func(node ast.Node) bool {
					call, ok := node.(*ast.CallExpr)
					if !ok {
						return true
					}
					for _, argIndex := range finalizedArgIndexes(call, known) {
						ident, ok := call.Args[argIndex].(*ast.Ident)
						if !ok || shadowed[ident.Name] {
							continue
						}
						if index := paramIndex(fn, ident.Name); index >= 0 {
							if known.add(fn.Name.Name, index) {
								grew = true
							}
						}
					}
					return true
				})
			}
		}
		if !grew {
			return known
		}
	}
}

// callSiteContext 描述一个调用在函数体内的执行语境。仅靠源码位置晚于 attach 不足以
// 证明定型会发生：条件分支、闭包、defer 以及发送之后的调用都不在“所有成功路径上先于
// 发送完成定型”这一要求之内。
type callSiteContext struct {
	Pos       token.Pos
	CondDepth int // 条件执行嵌套层数；0 表示在函数体内无条件执行
	InDefer   bool
	InClosure bool
}

func withinInit(call *ast.CallExpr, init ast.Stmt) bool {
	return init != nil && call.Pos() >= init.Pos() && call.End() <= init.End()
}

// visitCalls 遍历函数体内全部调用并给出执行语境。if/for/switch 的 Init 语句无条件
// 执行，不计入条件深度；分支体、循环体、case/select 子句与闭包体计入。
func visitCalls(body ast.Node, visit func(*ast.CallExpr, callSiteContext)) {
	stack := make([]ast.Node, 0, 16)
	ast.Inspect(body, func(node ast.Node) bool {
		if node == nil {
			stack = stack[:len(stack)-1]
			return false
		}
		stack = append(stack, node)
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		ctx := callSiteContext{Pos: call.Pos()}
		for _, ancestor := range stack {
			switch stmt := ancestor.(type) {
			case *ast.FuncLit:
				ctx.InClosure = true
			case *ast.DeferStmt:
				ctx.InDefer = true
			case *ast.IfStmt:
				if !withinInit(call, stmt.Init) {
					ctx.CondDepth++
				}
			case *ast.ForStmt:
				if !withinInit(call, stmt.Init) {
					ctx.CondDepth++
				}
			case *ast.RangeStmt:
				ctx.CondDepth++
			case *ast.CaseClause:
				ctx.CondDepth++
			case *ast.CommClause:
				ctx.CondDepth++
			}
		}
		visit(call, ctx)
		return true
	})
}

// upstreamSendFuncs 是发送动作。定型必须先于发送完成，否则发出去的仍是未定型请求。
var upstreamSendFuncs = map[string]bool{
	"Do":               true,
	"DoWithTLS":        true,
	"Dial":             true,
	"DialLiveSideband": true,
}

// finalizationOf 返回函数体内每个「定型动作」作用到的变量名及其执行语境。
func finalizationOf(body *ast.BlockStmt, known finalizedParams) map[string][]callSiteContext {
	actions := make(map[string][]callSiteContext)
	visitCalls(body, func(call *ast.CallExpr, ctx callSiteContext) {
		for _, argIndex := range finalizedArgIndexes(call, known) {
			if ident, ok := call.Args[argIndex].(*ast.Ident); ok {
				actions[ident.Name] = append(actions[ident.Name], ctx)
			}
		}
	})
	return actions
}

// reassignmentsOf 返回函数体内各标识符被赋值的位置，用于界定同名变量的有效区间。
func reassignmentsOf(body *ast.BlockStmt) map[string][]token.Pos {
	assigns := make(map[string][]token.Pos)
	ast.Inspect(body, func(node ast.Node) bool {
		assign, ok := node.(*ast.AssignStmt)
		if !ok {
			return true
		}
		for _, lhs := range assign.Lhs {
			if ident, ok := lhs.(*ast.Ident); ok && ident.Name != "_" {
				assigns[ident.Name] = append(assigns[ident.Name], ident.Pos())
			}
		}
		return true
	})
	return assigns
}

// firstSendPos 返回函数体内最早的发送调用位置；没有发送时返回 token.NoPos。
func firstSendPos(body *ast.BlockStmt) token.Pos {
	earliest := token.NoPos
	visitCalls(body, func(call *ast.CallExpr, ctx callSiteContext) {
		if !upstreamSendFuncs[calleeName(call)] {
			return
		}
		if earliest == token.NoPos || ctx.Pos < earliest {
			earliest = ctx.Pos
		}
	})
	return earliest
}

// analyzeAttachSites 逐调用点分析 attach 与定型的配对，保序返回全部调用点。
func analyzeAttachSites(fset *token.FileSet, files map[string]*ast.File) []attachCallSite {
	known := collectFinalizedParams(files)
	sites := make([]attachCallSite, 0)
	for path, file := range files {
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			actions := finalizationOf(fn.Body, known)
			assigns := reassignmentsOf(fn.Body)
			sendPos := firstSendPos(fn.Body)
			attachContexts := make(map[token.Pos]callSiteContext)
			visitCalls(fn.Body, func(call *ast.CallExpr, ctx callSiteContext) {
				if attachKind(calleeName(call)) != "" {
					attachContexts[call.Pos()] = ctx
				}
			})
			ast.Inspect(fn.Body, func(node ast.Node) bool {
				assign, ok := node.(*ast.AssignStmt)
				if !ok || len(assign.Rhs) != 1 || len(assign.Lhs) < 2 {
					return true
				}
				call, ok := assign.Rhs[0].(*ast.CallExpr)
				if !ok {
					return true
				}
				kind := attachKind(calleeName(call))
				if kind == "" {
					return true
				}
				site := attachCallSite{
					File:     path,
					Function: fn.Name.Name,
					Kind:     kind,
					Line:     fset.Position(call.Pos()).Line,
				}
				attachCtx := attachContexts[call.Pos()]
				ident, ok := assign.Lhs[1].(*ast.Ident)
				switch {
				case !ok:
					site.Reason = "egressContext 接收形式无法静态追踪，需显式改为局部变量"
				case ident.Name == "_":
					site.Reason = "egressContext 被丢弃，无法完成终态定型"
				default:
					site.Context = ident.Name
					// 该变量在下一次被重新赋值前才代表本次 attach 的上下文；越过
					// 边界的定型属于后一个 attach，不能回头认领本次调用点。
					boundary := token.NoPos
					for _, pos := range assigns[ident.Name] {
						if pos <= call.Pos() {
							continue
						}
						if boundary == token.NoPos || pos < boundary {
							boundary = pos
						}
					}
					site.Reason = "attach 之后没有对该 egressContext 的终态 header 定型"
					for _, action := range actions[ident.Name] {
						switch {
						case action.Pos <= call.Pos():
							continue // 属于上一个调用点
						case boundary != token.NoPos && action.Pos >= boundary:
							continue // 已跨过同名变量的重新赋值边界
						case action.InClosure:
							site.Reason = "定型位于闭包内，无法证明其在发送前执行"
							continue
						case action.InDefer:
							site.Reason = "定型位于 defer 中，执行时请求已发出"
							continue
						case action.CondDepth > attachCtx.CondDepth:
							site.Reason = "定型只存在于条件分支，未覆盖 attach 的所有成功路径"
							continue
						case sendPos != token.NoPos && action.Pos > sendPos:
							site.Reason = "定型发生在发送之后，发出的仍是未定型请求"
							continue
						}
						site.Finalized = true
						site.Reason = ""
						break
					}
				}
				sites = append(sites, site)
				return true
			})
		}
	}
	sort.Slice(sites, func(i, j int) bool {
		if sites[i].File != sites[j].File {
			return sites[i].File < sites[j].File
		}
		return sites[i].Line < sites[j].Line
	})
	return sites
}

// analyzeBodySites 收集 JSON body 定型与回写调用点。用清单而非计数比较：删除
// finalizer 会让计数关系反而“成立”，只有清单能发现调用点消失。
func analyzeBodySites(fset *token.FileSet, files map[string]*ast.File) []bodyCallSite {
	sites := make([]bodyCallSite, 0)
	for path, file := range files {
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			ast.Inspect(fn.Body, func(node ast.Node) bool {
				call, ok := node.(*ast.CallExpr)
				if !ok {
					return true
				}
				kind := ""
				switch calleeName(call) {
				case officialCodexBodyFinalizer:
					kind = "finalize"
				case officialCodexBodyReset:
					kind = "reset"
				default:
					return true
				}
				sites = append(sites, bodyCallSite{
					File:     path,
					Function: fn.Name.Name,
					Kind:     kind,
					Line:     fset.Position(call.Pos()).Line,
				})
				return true
			})
		}
	}
	sort.Slice(sites, func(i, j int) bool {
		if sites[i].File != sites[j].File {
			return sites[i].File < sites[j].File
		}
		return sites[i].Line < sites[j].Line
	})
	return sites
}

func parseOfficialEgressPackage(t *testing.T, fset *token.FileSet) map[string]*ast.File {
	t.Helper()
	entries, err := os.ReadDir(".")
	require.NoError(t, err)
	files := make(map[string]*ast.File)
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		parsed, parseErr := parser.ParseFile(fset, name, nil, parser.SkipObjectResolution)
		require.NoError(t, parseErr, name)
		files[name] = parsed
	}
	require.NotEmpty(t, files)
	return files
}

func parseProbe(t *testing.T, src string) (*token.FileSet, map[string]*ast.File) {
	t.Helper()
	fset := token.NewFileSet()
	parsed, err := parser.ParseFile(fset, "probe.go", src, parser.SkipObjectResolution)
	require.NoError(t, err)
	return fset, map[string]*ast.File{"probe.go": parsed}
}

// expectedAttachSites 是画像 attach 调用点的完整清单。新增端点或删除既有调用点都会
// 使本清单不符——这正是目的：调用点消失必须被人工确认，而不是让计数阈值悄悄放行。
// 当前为 8 个 HTTP 调用点与 1 个 WS 调用点。清单同时锁定数量与归属，删除整个 WS
// 调用点及其定型不会因为“总数仍达阈值”而蒙混过关。
var expectedAttachSites = []string{
	"account_test_service.go|testOpenAIImageOAuth|http",
	"official_egress_codex_files.go|executeJSON|http",
	"official_egress_codex_files.go|uploadBlob|http",
	"openai_alpha_search.go|buildOpenAIAlphaSearchRequest|http",
	"openai_codex_models_service.go|fetchCodexModelsManifestUpstream|http",
	"openai_images_responses.go|buildOpenAICodexImagesRequest|http",
	"openai_live.go|createUpstreamLiveCall|http",
	"openai_live.go|dialLiveSideband|ws",
	"openai_quota_service.go|doCodexQuotaRequest|http",
}

// TestOfficialEgressAttachSitesAreFullyPaired 锁住现有实现：每个 attach 调用点都必须
// 在其后完成终态 header 定型，且调用点清单与预期完全一致。
func TestOfficialEgressAttachSitesAreFullyPaired(t *testing.T) {
	fset := token.NewFileSet()
	files := parseOfficialEgressPackage(t, fset)
	sites := analyzeAttachSites(fset, files)

	keys := make([]string, 0, len(sites))
	unpaired := make([]string, 0)
	for _, site := range sites {
		keys = append(keys, site.key())
		if !site.Finalized {
			unpaired = append(unpaired, site.String())
		}
	}
	sort.Strings(keys)

	require.Empty(t, unpaired, "存在未配对终态 header 定型的画像 attach 调用点")
	require.Equal(t, expectedAttachSites, keys,
		"画像 attach 调用点清单变化：新增 attach 调用点需登记，删除调用点需人工确认。"+
			"注意本清单只覆盖已 attach 的调用点，不能证明某个端点没有绕过 attach")
}

// expectedBodySites 是 JSON body 定型与回写调用点清单。删除任一 body finalizer 都会
// 使清单不符——单纯比较定型与回写的次数无法发现这种删除。
// 清单保留重复条目（同一函数内多次定型会各占一行），因此删除其中任意一次调用都会
// 被发现。范围覆盖全部调用点而非仅 Codex 路径：`resetOfficialEgressRequestBody` 由
// Anthropic 路径共用，登记它只是锁住共用回写函数的调用面，不构成对 Anthropic 画像
// 的规则声明（§1.1 的范围不变）。
var expectedBodySites = []string{
	"account_test_service.go|testOpenAIImageOAuth|finalize",
	"account_test_service.go|testOpenAIImageOAuth|reset",
	"official_egress_anthropic.go|finalizeAnthropicOfficialEgressRequest|reset",
	"official_egress_codex_files.go|executeJSON|finalize",
	"official_egress_codex_files.go|executeJSON|reset",
	"official_egress_openai_http.go|compressOfficialOpenAIHTTPRequest|reset",
	"official_egress_openai_http.go|finalizeOpenAIOfficialEgressHTTPRequest|finalize",
	"official_egress_openai_http.go|finalizeOpenAIOfficialEgressHTTPRequest|reset",
	"official_egress_openai_ws.go|finalizeDerivedOpenAIOfficialEgressWSFrame|finalize",
	"official_egress_openai_ws.go|finalizeOpenAIOfficialEgressWSFrame|finalize",
	"official_egress_openai_ws.go|finalizeOpenAIOfficialEgressWSFrame|finalize",
	"openai_alpha_search.go|buildOpenAIAlphaSearchRequest|finalize",
	"openai_alpha_search.go|buildOpenAIAlphaSearchRequest|reset",
	"openai_images_responses.go|buildOpenAICodexImagesRequest|finalize",
	"openai_images_responses.go|buildOpenAICodexImagesRequest|reset",
	"openai_live.go|createUpstreamLiveCall|finalize",
	"openai_live.go|createUpstreamLiveCall|reset",
	"openai_quota_service.go|doCodexQuotaRequest|finalize",
	"openai_quota_service.go|doCodexQuotaRequest|reset",
}

func TestOfficialEgressBodyFinalizerSitesMatchLedger(t *testing.T) {
	fset := token.NewFileSet()
	files := parseOfficialEgressPackage(t, fset)
	sites := analyzeBodySites(fset, files)

	keys := make([]string, 0, len(sites))
	for _, site := range sites {
		keys = append(keys, site.key())
	}
	sort.Strings(keys)
	require.Equal(t, expectedBodySites, keys,
		"JSON body 定型／回写调用点清单变化：删除或新增都需人工确认")
}

// TestFinalizerPairingAnalyzerRejectsFalseGreens 是分析器自身的负例测试。只断言真实
// 源码通过会掩盖规则失效，必须证明每一类删除都能被抓到。
func TestFinalizerPairingAnalyzerRejectsFalseGreens(t *testing.T) {
	for _, testCase := range []struct {
		name     string
		src      string
		unpaired int
	}{
		{
			name: "两组独立调用均已定型",
			src: `package service
func uploadBlob() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}
func executeJSON() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`,
			unpaired: 0,
		},
		{
			name: "删除其中一组定型",
			src: `package service
func uploadBlob() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}
func executeJSON() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_, _ = request, egressContext
	return err
}`,
			unpaired: 1,
		},
		{
			name: "WS 经包装函数按参数位置完成定型",
			src: `package service
func liveSidebandHeaders(ctx context.Context, egressContext *OfficialEgressContext) (http.Header, error) {
	headers := make(http.Header)
	officialCodex0145FinalizeEndpointHeaders(egressContext, headers, nil)
	return headers, nil
}
func dialLiveSideband() error {
	wsContext, egressContext, err := attachOfficialCodex0145EndpointWebSocketContext(ctx, account, id)
	_ = err
	liveSidebandHeaders(wsContext, egressContext)
	return nil
}`,
			unpaired: 0,
		},
		{
			name: "包装函数定型被删除",
			src: `package service
func liveSidebandHeaders(ctx context.Context, egressContext *OfficialEgressContext) (http.Header, error) {
	return make(http.Header), nil
}
func dialLiveSideband() error {
	wsContext, egressContext, err := attachOfficialCodex0145EndpointWebSocketContext(ctx, account, id)
	_ = err
	liveSidebandHeaders(wsContext, egressContext)
	return nil
}
func unrelatedHTTPCall() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`,
			unpaired: 1,
		},
		{
			name: "包装函数定型的是另一个 context",
			src: `package service
func wrapperFinalizesOther(ctx context.Context, egressContext *OfficialEgressContext) (http.Header, error) {
	headers := make(http.Header)
	otherContext := unrelated()
	officialCodex0145FinalizeEndpointHeaders(otherContext, headers, nil)
	return headers, nil
}
func callerViaWrapper() error {
	wsContext, egressContext, err := attachOfficialCodex0145EndpointWebSocketContext(ctx, account, id)
	_ = err
	wrapperFinalizesOther(wsContext, egressContext)
	return nil
}`,
			unpaired: 1,
		},
		{
			name: "同一函数内两个 attach 复用同名变量",
			src: `package service
func twoCalls() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, first, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	request, egressContext, err = attachOfficialCodex0145EndpointRequest(req, account, second, "")
	_, _ = request, egressContext
	return err
}`,
			unpaired: 1,
		},
		{
			name: "定型出现在 attach 之前",
			src: `package service
func finalizeBeforeAttach() error {
	officialCodex0145FinalizeEndpointHeaders(egressContext, header, nil)
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_, _ = request, egressContext
	return err
}`,
			unpaired: 1,
		},
		{
			name: "egressContext 被丢弃",
			src: `package service
func discardsContext() error {
	request, _, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = request
	return err
}`,
			unpaired: 1,
		},
		{
			// 一处 finalizer 不能同时认领两个 attach：第一个 attach 的上下文在被
			// 重新赋值时就已失效，该 finalizer 只对第二个成立。
			name: "一处定型试图覆盖两次 attach",
			src: `package service
func twoAttachesOneFinalize() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, first, "")
	_ = err
	request, egressContext, err = attachOfficialCodex0145EndpointRequest(req, account, second, "")
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return err
}`,
			unpaired: 1,
		},
		{
			name: "定型只存在于条件分支",
			src: `package service
func finalizeInBranch() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	if shouldFinalize {
		officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	}
	return nil
}`,
			unpaired: 1,
		},
		{
			name: "定型位于未执行的闭包",
			src: `package service
func finalizeInClosure() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	later := func() {
		officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	}
	_ = later
	return nil
}`,
			unpaired: 1,
		},
		{
			name: "定型被 defer 到函数返回",
			src: `package service
func finalizeDeferred() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	defer officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`,
			unpaired: 1,
		},
		{
			name: "定型发生在发送之后",
			src: `package service
func finalizeAfterSend() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	resp, err := upstream.DoWithTLS(request, proxyURL, id, 1, profile)
	_ = resp
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return err
}`,
			unpaired: 1,
		},
		{
			// attach 与定型同处一个条件块时合法：alpha-search 就是这种形态。
			name: "attach 与定型同在条件分支内",
			src: `package service
func attachAndFinalizeInBranch() error {
	if account.IsOpenAIOAuth() {
		request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
		_ = err
		officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	}
	return nil
}`,
			unpaired: 0,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			fset, files := parseProbe(t, testCase.src)
			sites := analyzeAttachSites(fset, files)
			unpaired := make([]string, 0)
			for _, site := range sites {
				if !site.Finalized {
					unpaired = append(unpaired, site.String())
				}
			}
			require.Len(t, unpaired, testCase.unpaired, "%v", unpaired)
		})
	}
}

// TestBodyFinalizerLedgerDetectsDeletions 证明 body 清单能发现删除——这正是计数比较
// 做不到的：删掉 finalizer 后 0 > 1 不成立，两者同删时 0 > 0 也不成立。
func TestBodyFinalizerLedgerDetectsDeletions(t *testing.T) {
	const complete = `package service
func build() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	body, err = officialCodex0145FinalizeEndpointJSONBody(egressContext, body, nil)
	resetOfficialEgressRequestBody(request, body)
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`
	const finalizerDeleted = `package service
func build() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	resetOfficialEgressRequestBody(request, body)
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`
	const bothDeleted = `package service
func build() error {
	request, egressContext, err := attachOfficialCodex0145EndpointRequest(req, account, id, "")
	_ = err
	officialCodex0145FinalizeEndpointHeaders(egressContext, request.Header, nil)
	return nil
}`

	baseline := []string{"probe.go|build|finalize", "probe.go|build|reset"}

	collect := func(src string) []string {
		fset, files := parseProbe(t, src)
		keys := make([]string, 0)
		for _, site := range analyzeBodySites(fset, files) {
			keys = append(keys, site.key())
		}
		sort.Strings(keys)
		return keys
	}

	require.Equal(t, baseline, collect(complete))
	require.NotEqual(t, baseline, collect(finalizerDeleted), "删除 body finalizer 必须被清单发现")
	require.NotEqual(t, baseline, collect(bothDeleted), "同时删除定型与回写必须被清单发现")
}
