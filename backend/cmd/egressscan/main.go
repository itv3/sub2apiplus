// Command egressscan 是官方出站发送面的类型感知扫描器。
//
// 它服务于方案 §9 对变更集 0 的要求：用 go/packages + go/types + AST 按包路径、
// 接收者类型和方法识别网络发送调用，产出带 bootstrap_commit 的机器可读候选快照，
// 并在 CI 中持续复算新增、消失与调用目标变化。
//
// 与被它取代的正则实现的区别不是"更准一点"，而是能力上的：正则按 selector 名匹配，
// 无法区分 net/http.Client.Do 与业务代码里任意名为 Do 的方法，也看不见
// http.DefaultClient.Do、经接口转发的 RoundTrip、以及常量折叠后的 URL。
//
// 用法：
//
//	egressscan -mode bootstrap -out <baseline.json>   生成不含迁移证据的候选全集快照
//	egressscan -mode snapshot -migration-receipts <paths> -out <snapshot.json> 生成当前受审发送面快照
//	egressscan -mode check -baseline <baseline.json>  与基线比对，有漂移则非零退出
//	egressscan -mode replay -baseline <baseline.json>  在 bootstrap 源码上回放并验证遗漏补录
//	egressscan -mode stats -baseline <baseline.json> -out <stats.md> 生成文档统计
//	egressscan -mode self-test                        验证判据仍能发现各类形态
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/types"
	"os"
	"reflect"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

const (
	scanPattern           = "github.com/Wei-Shaw/sub2api/..."
	modulePackagePrefix   = "github.com/Wei-Shaw/sub2api/"
	legacyBootstrapCommit = "38a9929eac35a39c86de2f27de8f7a805d7dae52"
)

type scanBuildContext struct {
	id     string
	goos   string
	goarch string
}

// scanBuildContexts 是当前部署与开发需要覆盖的生产构建矩阵。
// build-tag 专用测试与 wireinject 生成入口不属于生产发送面。
var scanBuildContexts = []scanBuildContext{
	{id: "darwin/arm64", goos: "darwin", goarch: "arm64"},
	{id: "linux/amd64", goos: "linux", goarch: "amd64"},
}

// TargetResolution 记录调用目标的静态可解析程度。
//
// 方案 §9 第 2 点要求 literal、const、constructed、dynamic、unknown 全部输出，
// 不能只保留已经能解析为官方 host 的调用——只留可解析的那部分，等于把最危险的
// 动态目标（例如 blob 上传的 HostFromResponse）排除在视野之外。
type TargetResolution string

const (
	TargetLiteral     TargetResolution = "literal"
	TargetConst       TargetResolution = "const"
	TargetConstructed TargetResolution = "constructed"
	TargetDynamic     TargetResolution = "dynamic"
	TargetUnknown     TargetResolution = "unknown"
)

// SinkRecord 是机器可读基线的单条记录。字段集对应方案 §9 的最低要求。
type SinkRecord struct {
	// ScanCandidateID 标识**一个具体的调用表达式**，由扫描器生成。
	// 它不是业务身份：同一次用量请求会同时产生 factory 与 terminal 两个候选。
	ScanCandidateID string           `json:"scan_candidate_id"`
	File            string           `json:"file"`
	Func            string           `json:"func"`
	Package         string           `json:"package"`
	Callee          string           `json:"callee"`
	Receiver        string           `json:"receiver,omitempty"`
	SinkKind        string           `json:"sink_kind"`
	Protocol        string           `json:"protocol"`
	SinkType        string           `json:"sink_type"` // terminal | facade | factory
	ASTFingerprint  string           `json:"ast_fingerprint"`
	Resolution      TargetResolution `json:"target_resolution"`
	BuildContexts   []string         `json:"build_contexts"`
	ResolvedHosts   []string         `json:"resolved_hosts,omitempty"`
	ResolvedMethods []string         `json:"resolved_methods,omitempty"`
	ResolvedPaths   []string         `json:"resolved_paths,omitempty"`

	// ResolvedTargets 是能静态解析出的完整目标字符串（含 path）。
	//
	// 只记 host 不够：同 host 下把 /codex/responses 改成 /codex/models 不会改变
	// host，基线不会漂移——而那恰恰是一次 route 变更，会让 SinkCatalog 的
	// RouteKey 与实际不符。
	ResolvedTargets []string `json:"resolved_targets,omitempty"`
	OfficialHost    bool     `json:"official_host"`

	// 以下字段由分类规则补齐。方案 §9 第 5 点：扫描命中本身不等于身份分类已经正确。
	//
	// RuntimeSinkID 才是 Guard 使用的业务身份，多个 ScanCandidateID 可映射到同一个。
	// 它对应一个业务调用点，贯穿 Plan → Executor → backend 整条链路。
	RuntimeSinkID string `json:"runtime_sink_id"`
	Purpose       string `json:"purpose"`
	Persona       string `json:"persona"`
	// EndpointEvidence 说明该业务 Sink 是否真的有端点级画像证据。
	// 同 route 不代表同契约：OAuth authorization_code 与 refresh_token 都请求
	// POST /oauth/token，但当前画像只承载 refresh 的 Header/Body 规则。
	EndpointEvidence string `json:"endpoint_evidence"`
	// Routes 是该 sink 可能命中的**精确** route 集合。
	//
	// 用列表而非单值：doCodexQuotaRequest 一个函数按参数打三个 wham 端点，
	// forwardOpenAIImagesOAuth 打 generations 与 edits 两个端点。用 "wham/*"
	// 这类通配符伪 RouteKey 会让 Guard 无法精确匹配 RouteKey 四元组，
	// 等于把这些 sink 排除在 enforce 之外。
	//
	// facade 的 Routes 为空并由 IsFacade 标记：它的 route 由调用方的
	// RuntimeSinkID 决定，自身不承载 route。
	Routes   []string `json:"routes"`
	IsFacade bool     `json:"is_facade"`
	// Backend 是**当前实际**使用的传输后端；TargetBackend 是迁移后应当使用的。
	// 分列的原因：三条 P0 旁路当前用的是裸 net/http（httpclient.GetClient），
	// 记成 http_upstream 会掩盖问题本身——那正是要修的东西。
	Backend            string `json:"backend"`
	TargetBackend      string `json:"target_backend"`
	EnforcementState   string `json:"enforcement_state"`
	Owner              string `json:"owner"`
	MigrationChangeset string `json:"migration_changeset"`
	ExpiryCondition    string `json:"expiry_condition"`
	Rationale          string `json:"rationale"`

	// Line 仅供人阅读定位，不参与 SinkID 与指纹，避免行号漂移造成基线抖动。
	Line int `json:"line_hint"`
}

type Baseline struct {
	BootstrapCommit string       `json:"bootstrap_commit"`
	ScanPattern     string       `json:"scan_pattern"`
	BuildContexts   []string     `json:"build_contexts"`
	PackagesLoaded  int          `json:"packages_loaded"`
	SyntaxFallback  []string     `json:"syntax_fallback_files,omitempty"`
	Sinks           []SinkRecord `json:"sinks"`
}

func main() {
	mode := flag.String("mode", "bootstrap", "bootstrap | snapshot | check | replay | stats | self-test")
	out := flag.String("out", "", "输出基线文件路径")
	baselinePath := flag.String("baseline", "", "check/stats 模式下的基线文件路径")
	supplementsPath := flag.String("supplements", "", "1A 受审 pre-bootstrap 补录清单")
	removalsPath := flag.String("removals", "", "逗号分隔的受审候选移除/迁移收据清单")
	migrationReceiptsPath := flag.String("migration-receipts", "", "逗号分隔的受审 MigrationReceipt 清单")
	catalogAmendmentsPath := flag.String("catalog-amendments", "", "1A 受审 Catalog amendment 清单")
	inventoryLockPath := flag.String("inventory-lock", "", "bootstrap inventory 与扫描算法锁")
	scannerSourceRoot := flag.String("scanner-source-root", "", "参与算法摘要的 egressscan 源码目录")
	flag.Parse()

	switch *mode {
	case "self-test":
		os.Exit(runSelfTest())
	case "bootstrap":
		os.Exit(runBootstrap(*out))
	case "snapshot":
		os.Exit(runSnapshot(*out, *migrationReceiptsPath))
	case "check":
		os.Exit(runCheck(
			*baselinePath, *supplementsPath, *removalsPath, *migrationReceiptsPath,
			*catalogAmendmentsPath, *inventoryLockPath, *scannerSourceRoot,
		))
	case "replay":
		os.Exit(runBootstrapReplay(
			*baselinePath, *supplementsPath, *removalsPath, *migrationReceiptsPath,
			*catalogAmendmentsPath, *inventoryLockPath, *scannerSourceRoot,
		))
	case "stats":
		os.Exit(runStats(*baselinePath, *out))
	default:
		fmt.Fprintf(os.Stderr, "未知 mode: %s\n", *mode)
		os.Exit(2)
	}
}

func loadPackages(build scanBuildContext) ([]*packages.Package, []string, error) {
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedDeps |
			packages.NeedImports | packages.NeedModule,
		Tests: false,
		Env:   scanEnvironment(build),
	}
	pkgs, err := packages.Load(cfg, scanPattern)
	if err != nil {
		return nil, nil, err
	}
	if err := validateLoadedPackages(pkgs, build.id); err != nil {
		return nil, nil, err
	}

	fallback := collectTypecheckFallback(pkgs, build.id)
	return pkgs, fallback, nil
}

func scanEnvironment(build scanBuildContext) []string {
	environment := make([]string, 0, len(os.Environ())+3)
	for _, item := range os.Environ() {
		if strings.HasPrefix(item, "GOOS=") || strings.HasPrefix(item, "GOARCH=") ||
			strings.HasPrefix(item, "CGO_ENABLED=") {
			continue
		}
		environment = append(environment, item)
	}
	return append(environment,
		"GOOS="+build.goos,
		"GOARCH="+build.goarch,
		"CGO_ENABLED=0",
	)
}

// collectTypecheckFallback 把类型检查失败包中的生产文件显式登记，避免“类型信息为空”
// 被误解释成“没有网络发送”。contextID 进入记录，使构建矩阵中的失败可定位。
func collectTypecheckFallback(pkgs []*packages.Package, contextID string) []string {
	var fallback []string
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		if len(p.Errors) == 0 {
			return
		}
		for _, file := range p.GoFiles {
			if strings.HasSuffix(file, "_test.go") {
				continue
			}
			fallback = append(fallback, contextID+":"+relPath(file))
		}
	})
	return uniqueSorted(fallback)
}

// validateLoadedPackages 区分“有源码且仍能取得局部类型信息”的普通类型错误，和
// go/packages 因缓存、权限或 pattern 加载失败而返回的空壳 package。前者继续进入
// SyntaxFallback 并扫描仍可解析的调用；后者没有任何文件可登记，若继续执行会把
// “没有加载到源码”误报成“没有网络发送”，因此必须失败关闭。
func validateLoadedPackages(pkgs []*packages.Package, contextID string) error {
	var problems []string
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		if p == nil {
			problems = append(problems, "package=<nil>")
			return
		}
		if len(p.Errors) > 0 && len(p.GoFiles) == 0 && len(p.Syntax) == 0 {
			problems = append(problems, packageLoadProblem(p, "错误 package 没有可审计源码"))
			return
		}
		if strings.HasPrefix(p.PkgPath, modulePackagePrefix) && len(p.GoFiles) > 0 &&
			(p.Types == nil || p.TypesInfo == nil || len(p.Syntax) == 0) {
			problems = append(problems, packageLoadProblem(p, "仓库 package 类型或语法信息不完整"))
		}
	})
	problems = uniqueSorted(problems)
	if len(problems) > 0 {
		return fmt.Errorf("package 加载未完整关闭（%s）：%s", contextID, strings.Join(problems, "; "))
	}
	return nil
}

func packageLoadProblem(p *packages.Package, reason string) string {
	messages := make([]string, 0, len(p.Errors))
	for _, packageError := range p.Errors {
		message := strings.Join(strings.Fields(packageError.Error()), " ")
		if message != "" {
			messages = append(messages, message)
		}
	}
	messages = uniqueSorted(messages)
	return fmt.Sprintf("package=%q path=%q reason=%s errors=%q",
		p.ID, p.PkgPath, reason, strings.Join(messages, " | "))
}

// qualifiedCallee 返回调用目标的完整限定名。
//
// 这是整个扫描器的核心：只有拿到接收者的具体类型，才能区分
// (*net/http.Client).Do 与任意业务类型上的同名 Do。
func qualifiedCallee(info *types.Info, call *ast.CallExpr) (qualified, receiver string) {
	switch fun := call.Fun.(type) {
	case *ast.SelectorExpr:
		if sel, ok := info.Selections[fun]; ok {
			// 方法调用（含接口方法）：接收者类型 + 方法名。
			recv := sel.Recv()
			recvStr := types.TypeString(recv, nil)
			return fmt.Sprintf("(%s).%s", recvStr, fun.Sel.Name), recvStr
		}
		// 包级函数：pkg.Func
		if obj, ok := info.Uses[fun.Sel]; ok {
			if fn, ok := obj.(*types.Func); ok {
				if fn.Pkg() != nil {
					return fn.Pkg().Path() + "." + fn.Name(), ""
				}
				return fn.Name(), ""
			}
		}
	case *ast.Ident:
		if obj, ok := info.Uses[fun]; ok {
			if fn, ok := obj.(*types.Func); ok {
				if fn.Pkg() != nil {
					return fn.Pkg().Path() + "." + fn.Name(), ""
				}
				return fn.Name(), ""
			}
		}
	}
	return "", ""
}

// astFingerprint 对调用做规范化指纹，用于在 check 模式下发现"同一位置的调用被换了目标"。
// 指纹不含行号，但包含 callee、实参语法种类与标识符路径。请求变量从 req 换成
// otherReq 可能意味着从已定型请求换成未定型请求，因此必须产生漂移。
func astFingerprint(qualified string, call *ast.CallExpr) string {
	var b strings.Builder
	_, _ = b.WriteString(qualified)
	for _, arg := range call.Args {
		_, _ = b.WriteString("|")
		_, _ = b.WriteString(fmt.Sprintf("%T", arg))
		// 同时纳入实参的标识符/选择器路径。
		//
		// 只取语法节点类型的话，Do(req) 与 Do(otherReq) 指纹相同——把一个已定型的
		// 请求换成另一个未定型的请求，这类改动恰恰是最需要被发现的。
		_, _ = b.WriteString(":")
		_, _ = b.WriteString(exprIdentPath(arg))
	}
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])[:12]
}

// exprIdentPath 提取表达式的标识符路径（req、s.upstream、cfg.Client 等）。
// 字面量与复杂表达式返回其种类，保证指纹稳定但能区分不同的实参对象。
func exprIdentPath(expr ast.Expr) string {
	switch e := expr.(type) {
	case *ast.Ident:
		return e.Name
	case *ast.SelectorExpr:
		return exprIdentPath(e.X) + "." + e.Sel.Name
	case *ast.StarExpr:
		return "*" + exprIdentPath(e.X)
	case *ast.UnaryExpr:
		return "&" + exprIdentPath(e.X)
	case *ast.CallExpr:
		return exprIdentPath(e.Fun) + "()"
	case *ast.BasicLit:
		return e.Value
	case *ast.IndexExpr:
		return exprIdentPath(e.X) + "[]"
	}
	return "?"
}

func scan() (*Baseline, error) {
	return scanWithCodexRouteEvidence(nil)
}

// scanWithCodexRouteEvidence 允许已经迁入统一 Executor、因而不再直接命中物理
// network candidate 的业务 route 由 MigrationReceipt 提供覆盖证明。新增物理发送点
// 仍必须经过 classify；收据不能替代 terminal/facade 的新增候选检查。
func scanWithCodexRouteEvidence(reviewedRoutes []string) (*Baseline, error) {
	return scanWithReviewedHistory(reviewedRoutes, nil)
}

// scanWithReviewedHistory 在 bootstrap 历史源码回放时接收 RemovalReceipt 冻结的
// 分类。它不会用于当前生产树扫描，因此不能把收据当成新增发送点的分类豁免。
func scanWithReviewedHistory(
	reviewedRoutes []string,
	historicalClassifications map[string]SinkRecord,
) (*Baseline, error) {
	merged := make(map[string]SinkRecord)
	var fallback []string
	loaded := 0
	contextIDs := make([]string, 0, len(scanBuildContexts))
	for _, build := range scanBuildContexts {
		contextIDs = append(contextIDs, build.id)
		records, contextLoaded, contextFallback, err := scanBuild(build)
		if err != nil {
			return nil, err
		}
		loaded += contextLoaded
		fallback = append(fallback, contextFallback...)
		for _, record := range records {
			existing, exists := merged[record.ScanCandidateID]
			if !exists {
				merged[record.ScanCandidateID] = record
				continue
			}
			if !sameSinkAcrossContexts(existing, record) {
				return nil, fmt.Errorf(
					"候选 %s 在构建上下文 %v 与 %v 的请求事实不同；请拆分平台实现或扩展基线模型",
					record.ScanCandidateID, existing.BuildContexts, record.BuildContexts)
			}
			existing.BuildContexts = uniqueSorted(append(existing.BuildContexts, record.BuildContexts...))
			merged[record.ScanCandidateID] = existing
		}
	}

	records := make([]SinkRecord, 0, len(merged))
	for _, record := range merged {
		records = append(records, record)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].ScanCandidateID < records[j].ScanCandidateID })

	// 分类必须在基线落盘前完成：未分类的 sink 不允许静默进入 SinkCatalog。
	records, unclassified := applyClassificationWithHistory(records, historicalClassifications)
	problems := validateClassification(records, reviewedRoutes)
	if len(unclassified) > 0 || len(problems) > 0 {
		return nil, &unclassifiedError{sinks: unclassified, problems: problems}
	}

	contextIDs = uniqueSorted(contextIDs)
	fallback = uniqueSorted(fallback)
	return &Baseline{
		// bootstrap commit 是 legacy 存量边界，不随正常基线重算前移。
		// 如需重建边界，必须同时修改固定锚点并单独审核。
		BootstrapCommit: legacyBootstrapCommit,
		ScanPattern:     scanPattern,
		BuildContexts:   contextIDs,
		PackagesLoaded:  loaded,
		SyntaxFallback:  fallback,
		Sinks:           records,
	}, nil
}

func scanBuild(build scanBuildContext) ([]SinkRecord, int, []string, error) {
	pkgs, fallback, err := loadPackages(build)
	if err != nil {
		return nil, 0, nil, fmt.Errorf("加载构建上下文 %s: %w", build.id, err)
	}
	var records []SinkRecord
	loaded := 0
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		if p.Types == nil || p.TypesInfo == nil {
			return
		}
		// 只扫描本仓库的生产代码；依赖包内部的发送与本方案无关。
		if !strings.HasPrefix(p.PkgPath, "github.com/Wei-Shaw/sub2api/") {
			return
		}
		loaded++
		analyzer := newFlowAnalyzer(p.TypesInfo, p.Syntax)

		for i, file := range p.Syntax {
			filename := ""
			if i < len(p.GoFiles) {
				filename = p.GoFiles[i]
			}
			if strings.HasSuffix(filename, "_test.go") {
				continue
			}

			emit := func(scopeName string, root ast.Node, counter map[string]int) {
				ast.Inspect(root, func(inner ast.Node) bool {
					call, ok := inner.(*ast.CallExpr)
					if !ok {
						return true
					}
					qualified, receiver := qualifiedCallee(p.TypesInfo, call)
					if qualified == "" {
						return true
					}
					meta, sinkType := lookupSink(qualified)
					if sinkType == "" {
						return true
					}
					counter[qualified]++
					seq := counter[qualified]
					fileKey := relPath(filename)
					candidateID := fmt.Sprintf("%s.%s@%s#%s#%d",
						p.PkgPath, scopeName, fileKey, meta.kind, seq)

					facts := analyzer.resolveSinkFacts(call, qualified, meta)
					pos := p.Fset.Position(call.Pos())

					records = append(records, SinkRecord{
						ScanCandidateID: candidateID,
						File:            relPath(filename),
						Func:            scopeName,
						Package:         p.PkgPath,
						Callee:          qualified,
						Receiver:        receiver,
						SinkKind:        meta.kind,
						Protocol:        meta.protocol,
						SinkType:        sinkType,
						ASTFingerprint:  astFingerprint(qualified, call),
						Resolution:      facts.resolution,
						BuildContexts:   []string{build.id},
						ResolvedHosts:   facts.hosts,
						ResolvedMethods: facts.methods,
						ResolvedPaths:   facts.paths,
						ResolvedTargets: facts.targets,
						OfficialHost:    hasOfficialHost(facts.hosts),
						Line:            pos.Line,
					})
					return true
				})
			}

			packageCounter := map[string]int{}
			for _, decl := range file.Decls {
				switch d := decl.(type) {
				case *ast.FuncDecl:
					emit(funcDisplayName(d), d, map[string]int{})
				case *ast.GenDecl:
					// 包级 var/const 的初始化表达式。
					emit("<package-init>", d, packageCounter)
				}
			}
		}
	})
	sort.Slice(records, func(i, j int) bool { return records[i].ScanCandidateID < records[j].ScanCandidateID })
	return records, loaded, fallback, nil
}

func sameSinkAcrossContexts(left, right SinkRecord) bool {
	left.BuildContexts = nil
	right.BuildContexts = nil
	return reflect.DeepEqual(left, right)
}

func lookupSink(qualified string) (sinkKindMeta, string) {
	if meta, ok := terminalSinks[qualified]; ok {
		return meta, "terminal"
	}
	if meta, ok := projectFacades[qualified]; ok {
		if strings.HasPrefix(meta.kind, "factory_") {
			return meta, "factory"
		}
		return meta, "facade"
	}
	return sinkKindMeta{}, ""
}

func funcDisplayName(fn *ast.FuncDecl) string {
	if fn.Recv != nil && len(fn.Recv.List) > 0 {
		var buf strings.Builder
		if err := printRecv(&buf, fn.Recv.List[0].Type); err == nil {
			return buf.String() + "." + fn.Name.Name
		}
	}
	return fn.Name.Name
}

func printRecv(b *strings.Builder, expr ast.Expr) error {
	switch t := expr.(type) {
	case *ast.StarExpr:
		_, _ = b.WriteString("*")
		return printRecv(b, t.X)
	case *ast.Ident:
		_, _ = b.WriteString(t.Name)
		return nil
	case *ast.IndexExpr:
		return printRecv(b, t.X)
	}
	return fmt.Errorf("unsupported receiver")
}

func relPath(abs string) string {
	if idx := strings.Index(abs, "/backend/"); idx >= 0 {
		return "backend" + abs[idx+len("/backend"):]
	}
	return abs
}

func runBootstrap(out string) int {
	return runBaselineOutput(out, nil)
}

// runSnapshot 只读取当前源码和受审 MigrationReceipt，生成包含已迁入统一
// Executor 路由覆盖证明的完整当前发送面。它不校验或改写历史 bootstrap 基线。
func runSnapshot(out, migrationReceiptsPath string) int {
	if strings.TrimSpace(out) == "" {
		fmt.Fprintln(os.Stderr, "snapshot 模式需要 -out")
		return 2
	}
	migrations, err := loadMigrationReceiptIndex(migrationReceiptsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 MigrationReceipt 清单失败：%v\n", err)
		return 1
	}
	return runBaselineOutput(out, migrations.codexRoutes())
}

func runBaselineOutput(out string, reviewedRoutes []string) int {
	baseline, err := scanWithCodexRouteEvidence(reviewedRoutes)
	if err != nil {
		fmt.Fprintf(os.Stderr, "扫描失败：%v\n", err)
		return 1
	}
	data, err := json.MarshalIndent(baseline, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "序列化失败：%v\n", err)
		return 1
	}
	if out == "" {
		fmt.Println(string(data))
		return 0
	}
	if err := os.WriteFile(out, append(data, '\n'), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "写入失败：%v\n", err)
		return 1
	}

	official := 0
	dynamic := 0
	for _, s := range baseline.Sinks {
		if s.OfficialHost {
			official++
		}
		if s.Resolution == TargetDynamic || s.Resolution == TargetUnknown {
			dynamic++
		}
	}
	fmt.Printf("已写入 %s\n", out)
	fmt.Printf("  bootstrap_commit=%s\n", baseline.BootstrapCommit)
	fmt.Printf("  构建上下文 %s，包-上下文 %d 个，sink %d 条（官方 host %d，目标不可静态解析 %d）\n",
		strings.Join(baseline.BuildContexts, ", "), baseline.PackagesLoaded,
		len(baseline.Sinks), official, dynamic)
	if len(baseline.SyntaxFallback) > 0 {
		fmt.Printf("  ⚠ 类型检查失败需语法兜底的文件 %d 个\n", len(baseline.SyntaxFallback))
	}
	return 0
}

func runCheck(
	baselinePath, supplementsPath, removalsPath, migrationReceiptsPath,
	catalogAmendmentsPath, inventoryLockPath, scannerSourceRoot string,
) int {
	if baselinePath == "" {
		fmt.Fprintln(os.Stderr, "check 模式需要 -baseline")
		return 2
	}
	raw, err := os.ReadFile(baselinePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取基线失败：%v\n", err)
		return 1
	}
	var old Baseline
	old, err = decodeBaseline(raw)
	if err != nil {
		fmt.Fprintf(os.Stderr, "解析基线失败：%v\n", err)
		return 1
	}
	if err := verifyBootstrapInventoryLock(inventoryLockPath, raw, scannerSourceRoot); err != nil {
		fmt.Fprintf(os.Stderr, "验证 bootstrap inventory lock 失败：%v\n", err)
		return 1
	}
	supplements, err := loadSupplementManifest(supplementsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取受审补录清单失败：%v\n", err)
		return 1
	}
	migrations, err := loadMigrationReceiptIndex(migrationReceiptsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 MigrationReceipt 清单失败：%v\n", err)
		return 1
	}
	removals, err := loadRemovalManifest(removalsPath, migrations)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取受审移除收据失败：%v\n", err)
		return 1
	}
	amendments, err := loadScannerCatalogAmendments(catalogAmendmentsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 Catalog amendment 清单失败：%v\n", err)
		return 1
	}
	current, err := scanWithCodexRouteEvidence(migrations.codexRoutes())
	if err != nil {
		fmt.Fprintf(os.Stderr, "扫描失败：%v\n", err)
		return 1
	}

	var added, removed, changed []string
	if old.BootstrapCommit != legacyBootstrapCommit {
		changed = append(changed, fmt.Sprintf(
			"[基线元数据] bootstrap_commit: %q → 固定锚点 %q",
			old.BootstrapCommit,
			legacyBootstrapCommit,
		))
	}

	// 当前生产树会合法新增包，不能拿 packages_loaded 与 bootstrap 快照硬比。
	// bootstrap 元数据及候选全集的可复现性由 replay 模式在 clean archive 中证明；
	// 当前树只固定扫描范围、构建矩阵与类型检查完整性。
	for _, f := range []struct{ name, a, b string }{
		{"scan_pattern", old.ScanPattern, current.ScanPattern},
		{"build_contexts", strings.Join(old.BuildContexts, ","), strings.Join(current.BuildContexts, ",")},
		{"syntax_fallback_files", strings.Join(old.SyntaxFallback, ","), strings.Join(current.SyntaxFallback, ",")},
	} {
		if f.a != f.b {
			changed = append(changed, fmt.Sprintf("[基线元数据] %s: %q → %q", f.name, f.a, f.b))
		}
	}

	oldByID := map[string]SinkRecord{}
	for _, s := range old.Sinks {
		oldByID[s.ScanCandidateID] = s
	}
	curByID := map[string]SinkRecord{}
	for _, s := range current.Sinks {
		curByID[s.ScanCandidateID] = s
	}
	supplementByID := supplements.byID()
	removalByID := removals.byID()
	delegationByTargetID := removals.delegationByTargetID()
	amendmentByCandidateID := amendments.byCandidateID()
	seenSupplements := make(map[string]bool, len(supplementByID))

	for id, cur := range curByID {
		prev, ok := oldByID[id]
		if !ok {
			if receipts := delegationByTargetID[id]; len(receipts) > 0 {
				for _, receipt := range receipts {
					if err := validateLegacyDelegationTarget(receipt, cur); err != nil {
						changed = append(changed, fmt.Sprintf("%s  legacy delegation 非法：%v", id, err))
					}
				}
				continue
			}
			if rationale, reviewed := reviewedPostBootstrapInfrastructure[id]; reviewed {
				if (cur.Persona != "infrastructure" && cur.Persona != "out-of-scope") ||
					cur.EnforcementState != "not_applicable" || cur.RuntimeSinkID != "" ||
					strings.TrimSpace(rationale) == "" {
					changed = append(changed, fmt.Sprintf(
						"%s  受审 post-bootstrap 基础设施分类不合法", id))
				}
				continue
			}
			if item, reviewed := supplementByID[id]; reviewed {
				seenSupplements[id] = true
				if !compareSupplementStructure(item.Candidate, cur) {
					changed = append(changed, fmt.Sprintf(
						"%s  当前调用点与受审 pre-bootstrap 冻结结构不一致", id))
				}
				continue
			}
			added = append(added, fmt.Sprintf("%s  (%s @ %s:%d)", id, cur.Callee, cur.File, cur.Line))
			continue
		}
		if amendment, amended := amendmentByCandidateID[id]; amended &&
			amendment.Kind == scannerAmendmentRuntimeScopeExclusion {
			if err := validateScopeCorrectedCandidate(amendment, prev, cur); err != nil {
				changed = append(changed, fmt.Sprintf("%s  scope correction 非法：%v", id, err))
			}
			continue
		}
		// 反射遍历**全部**字段比对。
		//
		// 手写字段清单必然漏：上一版列了 14 个字段，仍漏掉 Receiver、SinkKind、
		// Protocol、OfficialHost、Rationale——篡改这些仍能全绿。基线是人工审核过的
		// 产物，任何字段的静默漂移都会让审核结论失效，因此这里不做取舍：
		// 除 Line（行号漂移是正常的、不改变语义）外，一律纳入比对。
		//
		// 新增字段自动进入比对范围，不需要记得回来改这里。
		pv := reflect.ValueOf(prev)
		cv := reflect.ValueOf(cur)
		rt := pv.Type()
		for i := 0; i < rt.NumField(); i++ {
			name := rt.Field(i).Name
			if name == "Line" {
				continue // 仅供人阅读定位，不参与语义
			}
			a := fmt.Sprintf("%v", pv.Field(i).Interface())
			b := fmt.Sprintf("%v", cv.Field(i).Interface())
			if a != b {
				changed = append(changed, fmt.Sprintf("%s  %s: %q → %q",
					id, rt.Field(i).Tag.Get("json"), a, b))
			}
		}
	}
	for candidateID, amendment := range amendmentByCandidateID {
		frozen, exists := oldByID[candidateID]
		if !exists {
			changed = append(changed, fmt.Sprintf("%s  Catalog amendment 引用未知 bootstrap candidate", candidateID))
			continue
		}
		if err := validateAmendmentFrozenSource(amendment, frozen); err != nil {
			changed = append(changed, fmt.Sprintf("%s  Catalog amendment 历史证据不匹配：%v", candidateID, err))
		}
	}
	for id := range supplementByID {
		if _, existed := oldByID[id]; existed {
			changed = append(changed, fmt.Sprintf("%s  补录项已存在于 bootstrap 基线，不应重复补录", id))
			continue
		}
		if !seenSupplements[id] {
			if _, removedWithReceipt := removalByID[id]; removedWithReceipt {
				continue
			}
			changed = append(changed, fmt.Sprintf("%s  受审补录项在当前生产树中不存在", id))
		}
	}
	for id := range reviewedPostBootstrapInfrastructure {
		if _, exists := curByID[id]; !exists {
			changed = append(changed, fmt.Sprintf("%s  post-bootstrap 基础设施豁免已陈旧", id))
		}
	}
	for id, prev := range oldByID {
		if _, ok := curByID[id]; !ok {
			receipt, reviewed := removalByID[id]
			if !reviewed {
				removed = append(removed, fmt.Sprintf("%s  (%s)", id, prev.Callee))
				continue
			}
			if !sameFrozenCandidate(receipt.Candidate, prev) {
				changed = append(changed, fmt.Sprintf("%s  移除收据未冻结原始 inventory 候选", id))
			}
		}
	}
	for id, receipt := range removalByID {
		if _, stillPresent := curByID[id]; stillPresent {
			changed = append(changed, fmt.Sprintf("%s  移除收据已陈旧：调用点仍存在", id))
			continue
		}
		if receipt.Kind == "legacy_delegated" {
			current, exists := curByID[receipt.DelegationCandidateID]
			if !exists {
				changed = append(changed, fmt.Sprintf("%s  legacy delegation target 不存在", id))
			} else if err := validateLegacyDelegationTarget(receipt, current); err != nil {
				changed = append(changed, fmt.Sprintf("%s  legacy delegation target 非法：%v", id, err))
			}
		}
		if prev, inBootstrap := oldByID[id]; inBootstrap {
			if !sameFrozenCandidate(receipt.Candidate, prev) {
				changed = append(changed, fmt.Sprintf("%s  移除收据与 bootstrap inventory 不一致", id))
			}
			continue
		}
		if supplement, supplemented := supplementByID[id]; supplemented {
			if !sameFrozenCandidate(receipt.Candidate, supplement.Candidate) {
				changed = append(changed, fmt.Sprintf("%s  移除收据与 supplement inventory 不一致", id))
			}
			continue
		}
		changed = append(changed, fmt.Sprintf("%s  移除收据引用未知 inventory 候选", id))
	}
	sort.Strings(added)
	sort.Strings(removed)
	sort.Strings(changed)

	if len(added) == 0 && len(removed) == 0 && len(changed) == 0 {
		fmt.Printf("✅ 当前发送面通过：bootstrap=%d，受审补录=%d，受审移除=%d，1A 基础设施=%d\n",
			len(old.Sinks), len(supplementByID), len(removalByID), len(reviewedPostBootstrapInfrastructure))
		return 0
	}

	fmt.Println("❌ 发送面相对基线发生漂移：")
	for _, a := range added {
		fmt.Printf("  [新增] %s\n", a)
	}
	for _, c := range changed {
		fmt.Printf("  [变更] %s\n", c)
	}
	for _, r := range removed {
		fmt.Printf("  [消失] %s\n", r)
	}
	fmt.Println()
	fmt.Println("bootstrap_commit 之后新增的裸 sink 禁止进入 legacy，必须立即失败。")
	fmt.Println("仅 pre-bootstrap 遗漏可进入受审补录清单，并须通过 replay 证明其在锚点提交中已存在。")
	return 1
}
