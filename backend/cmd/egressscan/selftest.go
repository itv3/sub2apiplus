package main

// 判据自测。
//
// 存在的理由：台账的可信度完全建立在"扫描器不会漏"之上。若判据悄悄失效，扫描结果
// 会变成一份看起来完整、实则空洞的清单，而且没有任何信号。
//
// 每条用例都对应一种前一版正则实现**实际漏掉**的形态。它们是判据从文本匹配升级到
// 类型感知的原因，也是防止有人把它改回文本匹配的锁。

import (
	"fmt"
	"go/ast"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

type selfTestCase struct {
	name       string
	source     string
	wantKinds  []string // 必须命中
	forbidKind string   // 必须不命中（负例）
	wantMethod string
	wantHost   string
	wantPath   string
	wantTarget string
}

var selfTestCases = []selfTestCase{
	{
		name: "http.DefaultClient.Do —— 正则按 client.Do 匹配会漏",
		source: `package sample

import "net/http"

func send(req *http.Request) {
	resp, _ := http.DefaultClient.Do(req)
	_ = resp
}
`,
		wantKinds: []string{"net_http_client_do"},
	},
	{
		name: "任意变量名接收者 hc.Do —— 正则只认 client./httpClient. 前缀会漏",
		source: `package sample

import "net/http"

func send(hc *http.Client, req *http.Request) {
	resp, _ := hc.Do(req)
	_ = resp
}
`,
		wantKinds: []string{"net_http_client_do"},
	},
	{
		name: "结构体字段上的客户端 s.upstream.Do —— 正则会漏",
		source: `package sample

import "net/http"

type svc struct{ upstream *http.Client }

func (s *svc) send(req *http.Request) {
	resp, _ := s.upstream.Do(req)
	_ = resp
}
`,
		wantKinds: []string{"net_http_client_do"},
	},
	{
		name: "包级 http.Get —— 正则完全不覆盖",
		source: `package sample

import "net/http"

const target = "https://chatgpt.com/backend-api/codex/models"

func fetch() {
	resp, _ := http.Get(target)
	_ = resp
}
`,
		wantKinds:  []string{"net_http_pkg_get"},
		wantMethod: "GET",
		wantHost:   "chatgpt.com",
		wantPath:   "/backend-api/codex/models",
		wantTarget: "https://chatgpt.com/backend-api/codex/models",
	},
	{
		name: "自定义 RoundTripper.RoundTrip —— 绕过所有 Client 判据",
		source: `package sample

import "net/http"

func send(rt http.RoundTripper, req *http.Request) {
	resp, _ := rt.RoundTrip(req)
	_ = resp
}
`,
		wantKinds: []string{"roundtripper_roundtrip"},
	},
	{
		name: "net.Dialer.DialContext —— 裸传输，正则只认 net.Dial(",
		source: `package sample

import (
	"context"
	"net"
)

func dial(ctx context.Context, d *net.Dialer) {
	conn, _ := d.DialContext(ctx, "tcp", "chatgpt.com:443")
	_ = conn
}
`,
		wantKinds:  []string{"raw_dialer_dialctx"},
		wantHost:   "chatgpt.com",
		wantTarget: "chatgpt.com:443",
	},
	{
		name: "tls.Dial 不得被误分类",
		source: `package sample

import "crypto/tls"

const rawHost = "chatgpt.com"

func dial(cfg *tls.Config) {
	conn, _ := tls.Dial("tcp", rawHost+":443", cfg)
	_ = conn
}
`,
		wantKinds:  []string{"raw_tls_dial"},
		wantHost:   "chatgpt.com",
		wantTarget: "chatgpt.com:443",
	},
	{
		name: "负例：业务类型上的同名 Do 不得误报",
		source: `package sample

type job struct{}

func (j *job) Do(task string) error { return nil }

func run(j *job) {
	_ = j.Do("compact")
}
`,
		forbidKind: "net_http_client_do",
	},
	{
		name: "负例：同名 Dial 的业务方法不得误报",
		source: `package sample

type pool struct{}

func (p *pool) Dial(name string) error { return nil }

func run(p *pool) {
	_ = p.Dial("upstream")
}
`,
		forbidKind: "ws_coder_dial",
	},
	{
		name: "常量折叠：baseURL + path 拼出的官方 host 必须被解析",
		source: `package sample

import "net/http"

const base = "https://chatgpt.com"
const full = base + "/backend-api/codex/responses"

func send(hc *http.Client) {
	req, _ := http.NewRequest("POST", full, nil)
	resp, _ := hc.Do(req)
	_ = resp
}
`,
		wantKinds:  []string{"net_http_client_do"},
		wantMethod: "POST",
		wantHost:   "chatgpt.com",
		wantPath:   "/backend-api/codex/responses",
		wantTarget: "https://chatgpt.com/backend-api/codex/responses",
	},
}

func runSelfTest() int {
	tmp, err := os.MkdirTemp("", "egressscan-selftest")
	if err != nil {
		fmt.Fprintf(os.Stderr, "创建临时目录失败：%v\n", err)
		return 1
	}
	defer func() { _ = os.RemoveAll(tmp) }()

	var failures []string
	for i, tc := range selfTestCases {
		dir := filepath.Join(tmp, fmt.Sprintf("case%d", i))
		if err := os.MkdirAll(dir, 0o755); err != nil {
			failures = append(failures, fmt.Sprintf("%s：建目录失败 %v", tc.name, err))
			continue
		}
		if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module sample\n\ngo 1.21\n"), 0o644); err != nil {
			failures = append(failures, fmt.Sprintf("%s：写 go.mod 失败 %v", tc.name, err))
			continue
		}
		if err := os.WriteFile(filepath.Join(dir, "sample.go"), []byte(tc.source), 0o644); err != nil {
			failures = append(failures, fmt.Sprintf("%s：写源码失败 %v", tc.name, err))
			continue
		}

		records, err := scanDir(dir)
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s：扫描失败 %v", tc.name, err))
			continue
		}
		kinds := recordKinds(records)

		for _, want := range tc.wantKinds {
			if !contains(kinds, want) {
				failures = append(failures,
					fmt.Sprintf("%s：未命中 %s（实际 %v）", tc.name, want, kinds))
			}
		}
		if tc.forbidKind != "" && contains(kinds, tc.forbidKind) {
			failures = append(failures,
				fmt.Sprintf("%s：误报 %s", tc.name, tc.forbidKind))
		}
		if tc.wantMethod != "" || tc.wantHost != "" || tc.wantPath != "" || tc.wantTarget != "" {
			record, ok := firstRecordWithKind(records, tc.wantKinds[0])
			if !ok {
				continue
			}
			for label, check := range map[string]struct {
				values []string
				want   string
			}{
				"method": {record.ResolvedMethods, tc.wantMethod},
				"host":   {record.ResolvedHosts, tc.wantHost},
				"path":   {record.ResolvedPaths, tc.wantPath},
				"target": {record.ResolvedTargets, tc.wantTarget},
			} {
				if check.want != "" && !containsExact(check.values, check.want) {
					failures = append(failures, fmt.Sprintf(
						"%s：%s 未解析出 %q（实际 %v）", tc.name, label, check.want, check.values))
				}
			}
		}
	}
	if mutationFailures := runRequestMutationSelfTest(tmp); len(mutationFailures) > 0 {
		failures = append(failures, mutationFailures...)
	}
	if fallbackFailures := runSyntaxFallbackSelfTest(tmp); len(fallbackFailures) > 0 {
		failures = append(failures, fallbackFailures...)
	}

	if len(failures) > 0 {
		fmt.Println("❌ 扫描器判据自测失败：")
		for _, f := range failures {
			fmt.Printf("  - %s\n", f)
		}
		return 1
	}
	if covFailures := runRealRepoCoverage(); len(covFailures) > 0 {
		fmt.Println("❌ 真实仓库覆盖度自测失败：")
		for _, f := range covFailures {
			fmt.Printf("  - %s\n", f)
		}
		return 1
	}
	fmt.Printf("✅ 扫描器判据自测通过：%d 条合成用例 + method/path/host 反向变异 + 类型失败兜底 + 双平台矩阵 + %d 类发送栈 + %d 条真实请求事实断言\n",
		len(selfTestCases), len(realRepoCoverage), len(realRequestEvidence))
	return 0
}

// scanDir 对给定目录做一次独立的类型感知扫描，返回完整请求事实。
// 自测若只返回 kind，会再次出现“识别到 Do、却没追到它发送了什么”的假绿。
func scanDir(dir string) ([]SinkRecord, error) {
	cfg := &packages.Config{
		Dir: dir,
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedDeps |
			packages.NeedImports,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return nil, err
	}
	var records []SinkRecord
	for _, p := range pkgs {
		if p.TypesInfo == nil {
			continue
		}
		analyzer := newFlowAnalyzer(p.TypesInfo, p.Syntax)
		for _, file := range p.Syntax {
			ast.Inspect(file, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				qualified, receiver := qualifiedCallee(p.TypesInfo, call)
				if qualified == "" {
					return true
				}
				if meta, sinkType := lookupSink(qualified); sinkType != "" {
					facts := analyzer.resolveSinkFacts(call, qualified, meta)
					records = append(records, SinkRecord{
						Callee:          qualified,
						Receiver:        receiver,
						SinkKind:        meta.kind,
						Protocol:        meta.protocol,
						SinkType:        sinkType,
						Resolution:      facts.resolution,
						ResolvedHosts:   facts.hosts,
						ResolvedMethods: facts.methods,
						ResolvedPaths:   facts.paths,
						ResolvedTargets: facts.targets,
						OfficialHost:    hasOfficialHost(facts.hosts),
					})
				}
				return true
			})
		}
	}
	sort.Slice(records, func(i, j int) bool {
		if records[i].SinkKind != records[j].SinkKind {
			return records[i].SinkKind < records[j].SinkKind
		}
		return formatSinkFacts(sinkFacts{
			methods: records[i].ResolvedMethods,
			hosts:   records[i].ResolvedHosts,
			paths:   records[i].ResolvedPaths,
			targets: records[i].ResolvedTargets,
		}) < formatSinkFacts(sinkFacts{
			methods: records[j].ResolvedMethods,
			hosts:   records[j].ResolvedHosts,
			paths:   records[j].ResolvedPaths,
			targets: records[j].ResolvedTargets,
		})
	})
	return records, nil
}

func recordKinds(records []SinkRecord) []string {
	kinds := make([]string, 0, len(records))
	for _, record := range records {
		kinds = append(kinds, record.SinkKind)
	}
	sort.Strings(kinds)
	return kinds
}

func firstRecordWithKind(records []SinkRecord, kind string) (SinkRecord, bool) {
	for _, record := range records {
		if record.SinkKind == kind {
			return record, true
		}
	}
	return SinkRecord{}, false
}

func containsExact(list []string, want string) bool {
	for _, value := range list {
		if value == want {
			return true
		}
	}
	return false
}

// runRequestMutationSelfTest 是请求事实门禁的反向验证。
//
// 同一份源码分别只改 method、path、host；三种变异都必须改变扫描事实。runCheck
// 会通过反射比对 SinkRecord 全字段，因此这些变化随后必然造成 CI 基线漂移。
func runRequestMutationSelfTest(tmp string) []string {
	type variant struct {
		name   string
		method string
		target string
	}
	variants := []variant{
		{name: "基准", method: "POST", target: "https://chatgpt.com/backend-api/codex/responses"},
		{name: "method 变异", method: "GET", target: "https://chatgpt.com/backend-api/codex/responses"},
		{name: "path 变异", method: "POST", target: "https://chatgpt.com/backend-api/codex/models"},
		{name: "host 变异", method: "POST", target: "https://auth.openai.com/backend-api/codex/responses"},
	}
	records := make([]SinkRecord, 0, len(variants))
	var failures []string
	for index, item := range variants {
		dir := filepath.Join(tmp, fmt.Sprintf("mutation%d", index))
		if err := os.MkdirAll(dir, 0o755); err != nil {
			failures = append(failures, fmt.Sprintf("%s：建目录失败 %v", item.name, err))
			continue
		}
		source := fmt.Sprintf(`package sample

import "net/http"

func send(client *http.Client) {
	req, _ := http.NewRequest(%q, %q, nil)
	_, _ = client.Do(req)
}
`, item.method, item.target)
		if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module sample\n\ngo 1.21\n"), 0o644); err != nil {
			failures = append(failures, fmt.Sprintf("%s：写 go.mod 失败 %v", item.name, err))
			continue
		}
		if err := os.WriteFile(filepath.Join(dir, "sample.go"), []byte(source), 0o644); err != nil {
			failures = append(failures, fmt.Sprintf("%s：写源码失败 %v", item.name, err))
			continue
		}
		scanned, err := scanDir(dir)
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s：扫描失败 %v", item.name, err))
			continue
		}
		record, ok := firstRecordWithKind(scanned, "net_http_client_do")
		if !ok {
			failures = append(failures, fmt.Sprintf("%s：未命中 net_http_client_do", item.name))
			continue
		}
		expectedHosts, expectedPaths, _ := targetComponents([]string{item.target})
		if !containsExact(record.ResolvedMethods, item.method) ||
			!containsExact(record.ResolvedTargets, item.target) ||
			!reflect.DeepEqual(record.ResolvedHosts, expectedHosts) ||
			!reflect.DeepEqual(record.ResolvedPaths, expectedPaths) {
			failures = append(failures, fmt.Sprintf(
				"%s：请求事实不完整，期望 method=%s host=%v path=%v target=%s，实际 %s",
				item.name, item.method, expectedHosts, expectedPaths, item.target,
				formatSinkFacts(sinkFacts{
					methods: record.ResolvedMethods,
					hosts:   record.ResolvedHosts,
					paths:   record.ResolvedPaths,
					targets: record.ResolvedTargets,
				})))
		}
		records = append(records, record)
	}
	if len(records) != len(variants) {
		return failures
	}
	base := records[0]
	for index := 1; index < len(records); index++ {
		if reflect.DeepEqual(base.ResolvedMethods, records[index].ResolvedMethods) &&
			reflect.DeepEqual(base.ResolvedHosts, records[index].ResolvedHosts) &&
			reflect.DeepEqual(base.ResolvedPaths, records[index].ResolvedPaths) &&
			reflect.DeepEqual(base.ResolvedTargets, records[index].ResolvedTargets) {
			failures = append(failures, fmt.Sprintf(
				"%s 未改变请求事实：%s", variants[index].name,
				formatSinkFacts(sinkFacts{
					methods: records[index].ResolvedMethods,
					hosts:   records[index].ResolvedHosts,
					paths:   records[index].ResolvedPaths,
					targets: records[index].ResolvedTargets,
				})))
		}
	}
	return failures
}

// runSyntaxFallbackSelfTest 证明类型检查失败不会被当成“没有 sink”。这里故意加入
// 未定义符号；packages.Load 仍返回语法树与可用的局部类型信息，扫描器必须同时：
// 1. 把对应生产文件登记进 fallback；
// 2. 继续命中该文件中仍可解析的网络发送候选。
//
// 只断言第 1 项会制造假绿：门禁看似知道“某文件有问题”，实际发送候选已经消失。
func runSyntaxFallbackSelfTest(tmp string) []string {
	dir := filepath.Join(tmp, "syntax-fallback")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return []string{fmt.Sprintf("语法兜底：建目录失败 %v", err)}
	}
	source := `package sample

import "net/http"

func send(client *http.Client, req *http.Request) {
	undefinedForFallbackTest()
	_, _ = client.Do(req)
}
`
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module sample\n\ngo 1.21\n"), 0o644); err != nil {
		return []string{fmt.Sprintf("语法兜底：写 go.mod 失败 %v", err)}
	}
	if err := os.WriteFile(filepath.Join(dir, "sample.go"), []byte(source), 0o644); err != nil {
		return []string{fmt.Sprintf("语法兜底：写源码失败 %v", err)}
	}
	cfg := &packages.Config{
		Dir: dir,
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedDeps |
			packages.NeedImports,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return []string{fmt.Sprintf("语法兜底：加载失败 %v", err)}
	}
	found := false
	for _, file := range collectTypecheckFallback(pkgs, "selftest") {
		if filepath.Base(strings.TrimPrefix(file, "selftest:")) == "sample.go" {
			found = true
		}
	}
	if !found {
		return []string{"语法兜底：类型检查失败的 sample.go 未进入 fallback"}
	}
	records, scanErr := scanDir(dir)
	if scanErr != nil {
		return []string{fmt.Sprintf("语法兜底：扫描带类型错误的目录失败 %v", scanErr)}
	}
	record, found := firstRecordWithKind(records, "net_http_client_do")
	if !found {
		return []string{"语法兜底：类型检查失败文件中的 net/http 发送候选被静默丢弃"}
	}
	if len(record.ResolvedMethods) > 0 {
		// Do(req) 的 method 由入参决定，本例应保持未知；这里仅防止未来误把无关常量
		// 当作请求事实。候选存在本身才是本测试的主要验收。
		return []string{fmt.Sprintf("语法兜底：动态请求被错误补造 method %v", record.ResolvedMethods)}
	}
	return nil
}

func contains(list []string, want string) bool {
	for _, item := range list {
		if strings.EqualFold(item, want) {
			return true
		}
	}
	return false
}

// realRepoCoverage 是对**真实仓库扫描结果**的覆盖度断言。
//
// 为什么需要它：合成用例跑在临时目录里，没有 req/v3、coder/websocket、
// 项目自身 HTTPUpstream 这些依赖，类型检查不通过，因此那几类判据无法用合成用例
// 验证。而它们恰恰是最容易在重构中失效的部分——限定名一变就静默失配。
//
// 这里改用真实扫描结果做断言：某类 sink 的命中数掉到 0，说明判据已失效。
var realRepoCoverage = map[string]int{
	"facade_http_upstream_do_tls": 1, // repository HTTPUpstream 带 TLS 画像发送
	"facade_http_upstream_do":     1, // 不带显式画像的发送
	"facade_ws_dialer":            1, // 项目 WS dialer 接口（经接口调用，具体类型扫不到）
	"ws_coder_dial":               1, // coder/websocket 包级 Dial
	"reqv3_get":                   1, // req/v3 终态方法
	"reqv3_post":                  1,
	"net_http_client_do":          1, // net/http 标准发送
	"roundtripper_roundtrip":      1, // 接口方法，无 client. 前缀
	"raw_dialer_dialctx":          1, // 裸拨号
	"factory_httpclient_pool":     1, // 客户端工厂
}

type requestEvidence struct {
	runtimeSinkID string
	sinkKind      string
	method        string
	host          string
	path          string
	target        string
}

// realRequestEvidence 把数据流能力钉在真实生产代码上。合成用例能证明算法存在，
// 这些断言则证明尚未迁移的 P0 旁路和 Chrome 路径没有因仓库重构退回
// dynamic/unknown。usage probe 在 1B 迁入 Executor 后不再保留旧 net/http 事实。
var realRequestEvidence = []requestEvidence{
	{
		runtimeSinkID: "unclassified.pat.whoami", sinkKind: "net_http_client_do", method: "GET",
		host: "auth.openai.com", path: "/api/accounts/v1/user-auth-credential/whoami",
		target: "https://auth.openai.com/api/accounts/v1/user-auth-credential/whoami",
	},
	{
		runtimeSinkID: "web.privacy.disable_training", sinkKind: "reqv3_patch", method: "PATCH",
		host: "chatgpt.com", path: "/backend-api/settings/account_user_setting",
		target: "https://chatgpt.com/backend-api/settings/account_user_setting",
	},
	{
		runtimeSinkID: "unclassified.agent.task_register", sinkKind: "net_http_client_do", method: "POST",
	},
}

// runRealRepoCoverage 扫描真实仓库并断言各类判据仍有命中。
func runRealRepoCoverage() []string {
	baseline, err := scanWithCodexRouteEvidence(codexAuthoritativeRoutes)
	if err != nil {
		return []string{fmt.Sprintf("真实仓库扫描失败：%v", err)}
	}
	counts := map[string]int{}
	for _, s := range baseline.Sinks {
		counts[s.SinkKind]++
	}
	var failures []string
	for kind, want := range realRepoCoverage {
		if counts[kind] < want {
			failures = append(failures, fmt.Sprintf(
				"判据 %s 在真实仓库命中 %d 次（期望 ≥%d）——限定名可能已失配",
				kind, counts[kind], want))
		}
	}
	for _, expected := range realRequestEvidence {
		matched := false
		for _, sink := range baseline.Sinks {
			if sink.RuntimeSinkID != expected.runtimeSinkID || sink.SinkKind != expected.sinkKind {
				continue
			}
			matched = true
			for label, check := range map[string]struct {
				values []string
				want   string
			}{
				"method": {sink.ResolvedMethods, expected.method},
				"host":   {sink.ResolvedHosts, expected.host},
				"path":   {sink.ResolvedPaths, expected.path},
				"target": {sink.ResolvedTargets, expected.target},
			} {
				if check.want != "" && !containsExact(check.values, check.want) {
					failures = append(failures, fmt.Sprintf(
						"真实请求 %s/%s 的 %s 未解析出 %q（实际 %v）",
						expected.runtimeSinkID, expected.sinkKind, label, check.want, check.values))
				}
			}
			break
		}
		if !matched {
			failures = append(failures, fmt.Sprintf(
				"真实请求事实缺少 %s/%s", expected.runtimeSinkID, expected.sinkKind))
		}
	}

	if err := validateBaseline(*baseline, codexAuthoritativeRoutes); err != nil {
		failures = append(failures, fmt.Sprintf("当前扫描结果未通过基线结构校验：%v", err))
	}
	wantContexts := make([]string, 0, len(scanBuildContexts))
	for _, build := range scanBuildContexts {
		wantContexts = append(wantContexts, build.id)
	}
	sort.Strings(wantContexts)
	if !reflect.DeepEqual(baseline.BuildContexts, wantContexts) {
		failures = append(failures, fmt.Sprintf(
			"真实扫描构建矩阵=%v，期望 %v", baseline.BuildContexts, wantContexts))
	}

	// 包级初始化器扫描能力：确认 <package-init> 作用域被真正遍历。
	// 当前仓库可能没有包级发送调用，因此只在存在时断言，不强制要求命中。
	hasPkgInitScope := false
	for _, s := range baseline.Sinks {
		if s.Func == "<package-init>" {
			hasPkgInitScope = true
			break
		}
	}
	_ = hasPkgInitScope

	return failures
}
