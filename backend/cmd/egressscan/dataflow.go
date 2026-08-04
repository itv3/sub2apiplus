package main

import (
	"fmt"
	"go/ast"
	"go/constant"
	"go/token"
	"go/types"
	"net"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

const maxResolvedValues = 64

// expressionDefinition 记录一个变量可能来自哪个表达式。
//
// 扫描器只做函数内、流不敏感的保守分析，不假装具备 SSA 能力。一个变量在分支中有
// 多个赋值时会保留全部候选；后续定义不会覆盖前一条证据。packageLevel 用于 Go 的
// 包级初始化依赖：包变量允许引用文本位置更靠后的另一个包变量，不能按 token.Pos 截断。
type expressionDefinition struct {
	expr         ast.Expr
	packageLevel bool
}

// flowAnalyzer 把 NewRequest(method, URL) 与随后发送的 Do(req) 绑定起来。
//
// 这里刻意不跨 helper 猜测返回值。跨函数、接口字段和运行期 URL 仍标为 dynamic，交由
// 显式 SinkCatalog 与后续运行时 Guard 约束；把它们猜成静态目标会制造比 unknown 更危险
// 的假证据。
type flowAnalyzer struct {
	info *types.Info
	defs map[types.Object][]expressionDefinition
}

type resolvedValues struct {
	values  map[string]struct{}
	dynamic bool
}

type requestFacts struct {
	methods resolvedValues
	targets resolvedValues
	found   bool
}

type sinkFacts struct {
	resolution TargetResolution
	methods    []string
	hosts      []string
	paths      []string
	targets    []string
}

func newFlowAnalyzer(info *types.Info, files []*ast.File) *flowAnalyzer {
	analyzer := &flowAnalyzer{
		info: info,
		defs: make(map[types.Object][]expressionDefinition),
	}
	for _, file := range files {
		for _, decl := range file.Decls {
			switch node := decl.(type) {
			case *ast.GenDecl:
				analyzer.collectDefinitions(node, true)
			case *ast.FuncDecl:
				analyzer.collectDefinitions(node, false)
			}
		}
	}
	return analyzer
}

func (a *flowAnalyzer) collectDefinitions(root ast.Node, packageLevel bool) {
	ast.Inspect(root, func(node ast.Node) bool {
		switch value := node.(type) {
		case *ast.ValueSpec:
			a.collectValueSpec(value, packageLevel)
		case *ast.AssignStmt:
			a.collectAssignStmt(value)
		}
		return true
	})
}

func (a *flowAnalyzer) collectValueSpec(spec *ast.ValueSpec, packageLevel bool) {
	if len(spec.Values) == 0 {
		return
	}
	for index, name := range spec.Names {
		if index >= len(spec.Values) {
			// 多返回值调用只把第一个结果绑定到调用表达式。当前分析只关心
			// NewRequest 的第一个 *http.Request 返回值。
			if len(spec.Values) != 1 || index != 0 {
				continue
			}
		}
		obj := a.info.Defs[name]
		if obj == nil {
			continue
		}
		expr := spec.Values[index]
		a.defs[obj] = append(a.defs[obj], expressionDefinition{expr: expr, packageLevel: packageLevel})
	}
}

func (a *flowAnalyzer) collectAssignStmt(assign *ast.AssignStmt) {
	if len(assign.Rhs) == 0 {
		return
	}
	for index, lhs := range assign.Lhs {
		ident, ok := lhs.(*ast.Ident)
		if !ok {
			continue
		}
		obj := a.info.Defs[ident]
		if obj == nil {
			obj = a.info.Uses[ident]
		}
		if obj == nil {
			continue
		}

		var expr ast.Expr
		switch {
		case len(assign.Rhs) == len(assign.Lhs):
			expr = assign.Rhs[index]
		case len(assign.Rhs) == 1 && index == 0:
			// req, err := http.NewRequest(...) 的第一个结果。
			expr = assign.Rhs[0]
		default:
			continue
		}
		a.defs[obj] = append(a.defs[obj], expressionDefinition{expr: expr})
	}
}

// resolveSinkFacts 只从当前调用的真实参数解析 method/target。它不会再扫描整个函数体
// 搜索任意官方 host，避免同一函数里两个请求互相污染身份。
func (a *flowAnalyzer) resolveSinkFacts(call *ast.CallExpr, qualified string, meta sinkKindMeta) sinkFacts {
	methods := newResolvedValues()
	targets := newResolvedValues()
	foundRequest := false

	requestArg, hasRequestArg := requestArgumentIndex(qualified, meta.kind)
	if hasRequestArg && requestArg < len(call.Args) {
		request := a.resolveRequest(call.Args[requestArg], call.Pos(), make(map[types.Object]bool))
		methods.merge(request.methods)
		targets.merge(request.targets)
		foundRequest = request.found
	}

	directMethod, methodArg, targetArg, hasDirectTarget := directCallShape(qualified, meta.kind)
	if directMethod != "" {
		methods.add(directMethod)
	}
	if methodArg >= 0 && methodArg < len(call.Args) {
		methods.merge(a.resolveStrings(call.Args[methodArg], call.Pos(), make(map[types.Object]bool)))
	}
	if hasDirectTarget && targetArg < len(call.Args) {
		targets.merge(a.resolveStrings(call.Args[targetArg], call.Pos(), make(map[types.Object]bool)))
	}

	resolution := TargetUnknown
	switch {
	case len(targets.values) > 0:
		if hasDirectTarget && targetArg < len(call.Args) && isStringLiteral(call.Args[targetArg]) {
			resolution = TargetLiteral
		} else if hasDirectTarget && targetArg < len(call.Args) && isCompileTimeString(a.info, call.Args[targetArg]) {
			resolution = TargetConst
		} else {
			resolution = TargetConstructed
		}
	case targets.dynamic || foundRequest:
		resolution = TargetDynamic
	}

	methodList := methods.sorted()
	for index := range methodList {
		methodList[index] = strings.ToUpper(strings.TrimSpace(methodList[index]))
	}
	methodList = uniqueSorted(methodList)

	targetList := targets.sorted()
	hosts, paths, official := targetComponents(targetList)
	_ = official // official 状态由调用方基于 hosts 计算，保留解析函数的单一出口。

	return sinkFacts{
		resolution: resolution,
		methods:    methodList,
		hosts:      hosts,
		paths:      paths,
		targets:    targetList,
	}
}

func requestArgumentIndex(qualified, kind string) (int, bool) {
	switch {
	case qualified == "(*net/http.Client).Do":
		return 0, true
	case qualified == "(*net/http.Transport).RoundTrip" || qualified == "(net/http.RoundTripper).RoundTrip":
		return 0, true
	case qualified == "(*github.com/imroc/req/v3.Client).Do":
		return 0, true
	case strings.HasPrefix(kind, "facade_http_upstream_do"):
		return 0, true
	case kind == "facade_openai_upstream":
		// 包级 doOpenAIHTTPUpstreamWithProfile 的 req 是第 2 个参数；其余
		// facade_openai_upstream 调用的 req 都是第 1 个参数。
		if qualified == "github.com/Wei-Shaw/sub2api/internal/service.doOpenAIHTTPUpstreamWithProfile" {
			return 1, true
		}
		return 0, true
	case kind == "facade_openai_upstream_req":
		return 0, true
	case kind == "roundtripper_roundtrip" || kind == "transport_roundtrip":
		return 0, true
	}
	return -1, false
}

// directCallShape 返回固定 method、method 参数位置、目标参数位置以及是否存在直接目标。
func directCallShape(qualified, kind string) (string, int, int, bool) {
	methodByKind := map[string]string{
		"net_http_client_get":      "GET",
		"net_http_client_post":     "POST",
		"net_http_client_postform": "POST",
		"net_http_client_head":     "HEAD",
		"net_http_pkg_get":         "GET",
		"net_http_pkg_post":        "POST",
		"net_http_pkg_postform":    "POST",
		"net_http_pkg_head":        "HEAD",
		"reqv3_get":                "GET",
		"reqv3_post":               "POST",
		"reqv3_patch":              "PATCH",
		"reqv3_put":                "PUT",
		"reqv3_delete":             "DELETE",
		"reqv3_head":               "HEAD",
		"reqv3_options":            "OPTIONS",
		"reqv3_mustget":            "GET",
		"reqv3_mustpost":           "POST",
		"reqv3_mustput":            "PUT",
		"reqv3_mustpatch":          "PATCH",
		"reqv3_mustdelete":         "DELETE",
		"reqv3_musthead":           "HEAD",
		"reqv3_mustoptions":        "OPTIONS",
		"ws_coder_dial":            "GET",
		"ws_gorilla_dial":          "GET",
		"ws_gorilla_dialctx":       "GET",
		"facade_ws_dialer":         "GET",
		"facade_ws_dialer_metrics": "GET",
	}
	if method, ok := methodByKind[kind]; ok {
		targetArg := 0
		switch kind {
		case "ws_coder_dial", "ws_gorilla_dialctx", "facade_ws_dialer", "facade_ws_dialer_metrics":
			targetArg = 1
		}
		return method, -1, targetArg, true
	}
	if kind == "reqv3_send" {
		return "", 0, 1, true
	}
	if strings.HasPrefix(kind, "raw_") {
		switch qualified {
		case "net.Dial", "net.DialTimeout", "(*net.Dialer).Dial", "crypto/tls.Dial":
			return "", -1, 1, true
		case "(*net.Dialer).DialContext", "crypto/tls.DialWithDialer", "(*crypto/tls.Dialer).DialContext":
			return "", -1, 2, true
		}
	}
	return "", -1, -1, false
}

func (a *flowAnalyzer) resolveRequest(expr ast.Expr, limit token.Pos, visiting map[types.Object]bool) requestFacts {
	switch value := unparen(expr).(type) {
	case *ast.Ident:
		obj := a.info.Uses[value]
		if obj == nil {
			obj = a.info.Defs[value]
		}
		if obj == nil || visiting[obj] {
			return requestFacts{}
		}
		visiting[obj] = true
		defer delete(visiting, obj)
		result := requestFacts{}
		for _, definition := range a.defs[obj] {
			if !definition.packageLevel && definition.expr.Pos() >= limit {
				continue
			}
			facts := a.resolveRequest(definition.expr, definition.expr.Pos(), visiting)
			result.methods.merge(facts.methods)
			result.targets.merge(facts.targets)
			result.found = result.found || facts.found
		}
		return result
	case *ast.CallExpr:
		qualified, _ := qualifiedCallee(a.info, value)
		switch qualified {
		case "net/http.NewRequest":
			return a.requestFromConstructor(value, 0, 1)
		case "net/http.NewRequestWithContext":
			return a.requestFromConstructor(value, 1, 2)
		}
		if selector, ok := value.Fun.(*ast.SelectorExpr); ok {
			switch selector.Sel.Name {
			case "WithContext", "Clone":
				return a.resolveRequest(selector.X, value.Pos(), visiting)
			}
		}
	case *ast.UnaryExpr:
		if value.Op == token.AND {
			return a.resolveRequest(value.X, limit, visiting)
		}
	}
	return requestFacts{}
}

func (a *flowAnalyzer) requestFromConstructor(call *ast.CallExpr, methodArg, targetArg int) requestFacts {
	methods := newResolvedValues()
	targets := newResolvedValues()
	if methodArg < len(call.Args) {
		methods = a.resolveStrings(call.Args[methodArg], call.Pos(), make(map[types.Object]bool))
	} else {
		methods.dynamic = true
	}
	if targetArg < len(call.Args) {
		targets = a.resolveStrings(call.Args[targetArg], call.Pos(), make(map[types.Object]bool))
	} else {
		targets.dynamic = true
	}
	return requestFacts{methods: methods, targets: targets, found: true}
}

func (a *flowAnalyzer) resolveStrings(expr ast.Expr, limit token.Pos, visiting map[types.Object]bool) resolvedValues {
	result := newResolvedValues()
	expr = unparen(expr)
	if tv, ok := a.info.Types[expr]; ok && tv.Value != nil && tv.Value.Kind() == constant.String {
		result.add(constant.StringVal(tv.Value))
		return result
	}

	switch value := expr.(type) {
	case *ast.BasicLit:
		if value.Kind == token.STRING {
			decoded, err := strconv.Unquote(value.Value)
			if err == nil {
				result.add(decoded)
				return result
			}
		}
	case *ast.Ident:
		obj := a.info.Uses[value]
		if obj == nil {
			obj = a.info.Defs[value]
		}
		if obj == nil || visiting[obj] {
			result.dynamic = true
			return result
		}
		if constantObject, ok := obj.(*types.Const); ok && constantObject.Val().Kind() == constant.String {
			result.add(constant.StringVal(constantObject.Val()))
			return result
		}
		definitions := a.defs[obj]
		if len(definitions) == 0 {
			result.dynamic = true
			return result
		}
		visiting[obj] = true
		defer delete(visiting, obj)
		matched := false
		for _, definition := range definitions {
			if !definition.packageLevel && definition.expr.Pos() >= limit {
				continue
			}
			matched = true
			result.merge(a.resolveStrings(definition.expr, definition.expr.Pos(), visiting))
		}
		if !matched {
			result.dynamic = true
		}
		return result
	case *ast.BinaryExpr:
		if value.Op == token.ADD {
			left := a.resolveStrings(value.X, value.Pos(), visiting)
			right := a.resolveStrings(value.Y, value.Pos(), visiting)
			result.dynamic = left.dynamic || right.dynamic
			for l := range left.values {
				for r := range right.values {
					if len(result.values) >= maxResolvedValues {
						result.dynamic = true
						return result
					}
					result.add(l + r)
				}
			}
			return result
		}
	case *ast.CallExpr:
		if selector, ok := value.Fun.(*ast.SelectorExpr); ok && selector.Sel.Name == "String" {
			return a.resolveURLValue(selector.X, value.Pos(), visiting)
		}
		qualified, _ := qualifiedCallee(a.info, value)
		switch qualified {
		case "strings.TrimSpace":
			return a.mapUnaryStringCall(value, limit, visiting, strings.TrimSpace)
		case "strings.TrimRight":
			return a.mapBinaryStringCall(value, limit, visiting, strings.TrimRight)
		case "strings.TrimSuffix":
			return a.mapBinaryStringCall(value, limit, visiting, strings.TrimSuffix)
		case "net/url.JoinPath":
			return a.resolveJoinPath(value, limit, visiting)
		}
	case *ast.SelectorExpr:
		// 导入包的 const 已由 go/types 常量分支处理；结构体字段与外部包 var
		// 没有本地定义，必须保持 dynamic。
		result.dynamic = true
		return result
	}

	if tv, ok := a.info.Types[expr]; ok {
		if basic, ok := tv.Type.Underlying().(*types.Basic); ok && basic.Kind() == types.String {
			result.dynamic = true
		}
	}
	return result
}

func (a *flowAnalyzer) resolveURLValue(expr ast.Expr, limit token.Pos, visiting map[types.Object]bool) resolvedValues {
	result := newResolvedValues()
	switch value := unparen(expr).(type) {
	case *ast.Ident:
		obj := a.info.Uses[value]
		if obj == nil {
			obj = a.info.Defs[value]
		}
		if obj == nil || visiting[obj] {
			result.dynamic = true
			return result
		}
		visiting[obj] = true
		defer delete(visiting, obj)
		for _, definition := range a.defs[obj] {
			if !definition.packageLevel && definition.expr.Pos() >= limit {
				continue
			}
			result.merge(a.resolveURLValue(definition.expr, definition.expr.Pos(), visiting))
		}
		if len(result.values) == 0 {
			result.dynamic = true
		}
		return result
	case *ast.CallExpr:
		qualified, _ := qualifiedCallee(a.info, value)
		if (qualified == "net/url.Parse" || qualified == "net/url.ParseRequestURI") && len(value.Args) > 0 {
			return a.resolveStrings(value.Args[0], value.Pos(), visiting)
		}
	case *ast.UnaryExpr:
		if value.Op == token.AND {
			return a.resolveURLValue(value.X, limit, visiting)
		}
	case *ast.CompositeLit:
		return a.resolveURLComposite(value, limit, visiting)
	}
	result.dynamic = true
	return result
}

func (a *flowAnalyzer) resolveURLComposite(literal *ast.CompositeLit, limit token.Pos, visiting map[types.Object]bool) resolvedValues {
	parts := map[string]resolvedValues{}
	for _, element := range literal.Elts {
		pair, ok := element.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		key, ok := pair.Key.(*ast.Ident)
		if !ok {
			continue
		}
		switch key.Name {
		case "Scheme", "Host", "Path", "RawPath", "RawQuery", "Fragment":
			parts[key.Name] = a.resolveStrings(pair.Value, limit, visiting)
		}
	}
	for _, name := range []string{"Scheme", "Host", "Path", "RawPath", "RawQuery", "Fragment"} {
		part, ok := parts[name]
		if !ok {
			part = newResolvedValues()
			part.add("")
			parts[name] = part
		}
		if len(part.values) != 1 || part.dynamic {
			result := newResolvedValues()
			result.dynamic = true
			return result
		}
	}
	first := func(name string) string {
		for value := range parts[name].values {
			return value
		}
		return ""
	}
	parsed := url.URL{
		Scheme:   first("Scheme"),
		Host:     first("Host"),
		Path:     first("Path"),
		RawPath:  first("RawPath"),
		RawQuery: first("RawQuery"),
		Fragment: first("Fragment"),
	}
	result := newResolvedValues()
	result.add(parsed.String())
	return result
}

func (a *flowAnalyzer) mapUnaryStringCall(call *ast.CallExpr, limit token.Pos, visiting map[types.Object]bool, fn func(string) string) resolvedValues {
	if len(call.Args) != 1 {
		return resolvedValues{values: map[string]struct{}{}, dynamic: true}
	}
	input := a.resolveStrings(call.Args[0], limit, visiting)
	result := newResolvedValues()
	result.dynamic = input.dynamic
	for value := range input.values {
		result.add(fn(value))
	}
	return result
}

func (a *flowAnalyzer) mapBinaryStringCall(call *ast.CallExpr, limit token.Pos, visiting map[types.Object]bool, fn func(string, string) string) resolvedValues {
	if len(call.Args) != 2 {
		return resolvedValues{values: map[string]struct{}{}, dynamic: true}
	}
	left := a.resolveStrings(call.Args[0], limit, visiting)
	right := a.resolveStrings(call.Args[1], limit, visiting)
	result := newResolvedValues()
	result.dynamic = left.dynamic || right.dynamic
	for l := range left.values {
		for r := range right.values {
			result.add(fn(l, r))
		}
	}
	return result
}

func (a *flowAnalyzer) resolveJoinPath(call *ast.CallExpr, limit token.Pos, visiting map[types.Object]bool) resolvedValues {
	result := newResolvedValues()
	result.add("")
	for _, arg := range call.Args {
		part := a.resolveStrings(arg, limit, visiting)
		if part.dynamic || len(part.values) == 0 {
			result.dynamic = true
		}
		next := newResolvedValues()
		for base := range result.values {
			for value := range part.values {
				joined, err := url.JoinPath(base, value)
				if err != nil {
					next.dynamic = true
					continue
				}
				next.add(joined)
			}
		}
		next.dynamic = next.dynamic || result.dynamic || part.dynamic
		result = next
	}
	return result
}

func newResolvedValues() resolvedValues {
	return resolvedValues{values: make(map[string]struct{})}
}

func (v *resolvedValues) add(value string) {
	if v.values == nil {
		v.values = make(map[string]struct{})
	}
	if len(v.values) >= maxResolvedValues {
		v.dynamic = true
		return
	}
	v.values[value] = struct{}{}
}

func (v *resolvedValues) merge(other resolvedValues) {
	if v.values == nil {
		v.values = make(map[string]struct{})
	}
	for value := range other.values {
		v.add(value)
	}
	v.dynamic = v.dynamic || other.dynamic
}

func (v resolvedValues) sorted() []string {
	result := make([]string, 0, len(v.values))
	for value := range v.values {
		if strings.TrimSpace(value) != "" {
			result = append(result, value)
		}
	}
	sort.Strings(result)
	return result
}

func targetComponents(targets []string) (hosts, paths []string, official bool) {
	hostSet := make(map[string]struct{})
	pathSet := make(map[string]struct{})
	for _, target := range targets {
		target = strings.TrimSpace(target)
		if target == "" {
			continue
		}
		parsed, err := url.Parse(target)
		if err == nil {
			host := strings.ToLower(parsed.Hostname())
			if host != "" {
				hostSet[host] = struct{}{}
				if isOfficialHostname(host) {
					official = true
				}
			}
			isURLTarget := parsed.Host != "" || strings.HasPrefix(target, "/") ||
				parsed.Scheme == "http" || parsed.Scheme == "https" ||
				parsed.Scheme == "ws" || parsed.Scheme == "wss"
			if isURLTarget {
				path := parsed.EscapedPath()
				if path == "" && parsed.Host != "" {
					path = "/"
				}
				if path != "" {
					pathSet[path] = struct{}{}
				}
				continue
			}
		}

		// 裸 TCP/TLS sink 的目标通常是 host:port，不是 URL。
		host := target
		if splitHost, _, splitErr := net.SplitHostPort(target); splitErr == nil {
			host = splitHost
		}
		host = strings.ToLower(strings.Trim(host, "[]"))
		if host != "" && !strings.ContainsAny(host, "/ ") {
			hostSet[host] = struct{}{}
			if isOfficialHostname(host) {
				official = true
			}
		}
	}
	for host := range hostSet {
		hosts = append(hosts, host)
	}
	for path := range pathSet {
		paths = append(paths, path)
	}
	sort.Strings(hosts)
	sort.Strings(paths)
	return hosts, paths, official
}

func isOfficialHostname(host string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	for _, official := range officialHosts {
		if host == official || strings.HasSuffix(host, "."+official) {
			return true
		}
	}
	return false
}

func hasOfficialHost(hosts []string) bool {
	for _, host := range hosts {
		if isOfficialHostname(host) {
			return true
		}
	}
	return false
}

func isCompileTimeString(info *types.Info, expr ast.Expr) bool {
	tv, ok := info.Types[expr]
	return ok && tv.Value != nil && tv.Value.Kind() == constant.String
}

func isStringLiteral(expr ast.Expr) bool {
	literal, ok := unparen(expr).(*ast.BasicLit)
	return ok && literal.Kind == token.STRING
}

func unparen(expr ast.Expr) ast.Expr {
	for {
		paren, ok := expr.(*ast.ParenExpr)
		if !ok {
			return expr
		}
		expr = paren.X
	}
}

func uniqueSorted(values []string) []string {
	set := make(map[string]struct{}, len(values))
	for _, value := range values {
		if value != "" {
			set[value] = struct{}{}
		}
	}
	result := make([]string, 0, len(set))
	for value := range set {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func formatSinkFacts(facts sinkFacts) string {
	return fmt.Sprintf("method=%v host=%v path=%v target=%v", facts.methods, facts.hosts, facts.paths, facts.targets)
}
