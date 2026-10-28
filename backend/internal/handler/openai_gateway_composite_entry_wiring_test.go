//go:build unit

package handler

// 满载换号接入层的"接线"测试（补 SLIMV 验收缺口）。分两类：
//
//  1. 行为测试：用真实的 acquireResponsesAccountSlot＋真实网关服务＋记录粘性绑定的假缓存，
//     断言"临时借用时不改写粘性会话绑定、没借用时照常绑定"，并覆盖同一 gin.Context 上多轮选号时
//     标志必须每轮先复位（陈旧标志）。三个入口（Responses、Messages、ChatCompletions）各跑一遍。
//  2. 语法树守卫：钉住三个入口的调用点、实参来源，以及 acquireResponsesAccountSlot 把
//     compositeSlotSessionHash 的"结果"传给了 acquireOpenAIAccountSlot。
//
// 语法树守卫是源码锚定测试：上游合并时改了变量名、挪了调用点就会红，这是期望的响亮失败
// （逼人复核换号接线有没有被冲突覆盖），不是缺陷；确认接线无误后同步改本文件里的期望表即可。
//
// 局限：换号成功分支（switched==true）依赖真实调度器与候选池夹具，handler 包里没有现成的假实现，
// 所以"借用"轮用 st.apply 直接模拟（与入口方法在换号成功后执行的第一个动作相同），
// 入口方法里"成功就返回 apply 的结果"这一段胶水用语法树守卫钉住（TestCompositeSwitchMethodsGlueShape）。

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/Wei-Shaw/sub2api/internal/testutil"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// ---------------------------------------------------------------------------
// 行为测试
// ---------------------------------------------------------------------------

// compositeStickyRecorder 记录粘性会话绑定的写入，读取一律报"未绑定"。
type compositeStickyRecorder struct {
	testutil.StubGatewayCache
	mu   sync.Mutex
	sets []compositeStickySet
}

type compositeStickySet struct {
	key       string
	accountID int64
}

func (r *compositeStickyRecorder) GetSessionAccountID(context.Context, int64, string) (int64, error) {
	return 0, service.ErrStickySessionNotFound
}

func (r *compositeStickyRecorder) SetSessionAccountID(_ context.Context, _ int64, key string, accountID int64, _ time.Duration) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sets = append(r.sets, compositeStickySet{key: key, accountID: accountID})
	return nil
}

func (r *compositeStickyRecorder) snapshot() []compositeStickySet {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]compositeStickySet(nil), r.sets...)
}

// newCompositeStickyHandler 构造带真实网关服务（缓存换成记录器）与可快速抢槽的并发服务的处理器。
func newCompositeStickyHandler(t *testing.T) (*OpenAIGatewayHandler, *compositeStickyRecorder) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	recorder := &compositeStickyRecorder{}
	cfg := &config.Config{RunMode: config.RunModeSimple}
	concurrency := service.NewConcurrencyService(&profitCountingConcurrencyCache{})
	gateway := service.NewOpenAIGatewayService(nil, nil, nil, nil, nil, nil, recorder, cfg, nil, concurrency,
		nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	return &OpenAIGatewayHandler{
		gatewayService:    gateway,
		concurrencyHelper: NewConcurrencyHelper(concurrency, SSEPingFormatClaude, 0),
	}, recorder
}

// compositeEntryDriver 把三个入口的"不换号一轮"和"换号成功一轮"抽象成统一接口。
type compositeEntryDriver struct {
	name string
	// plainRound 调用真实的 switchCompositeXxx：网关服务没有换号计划，不会换号，只会复位标志。
	// fresh 为 false 时模拟"已有账号失败"，入口方法依旧必须复位标志。
	plainRound func(t *testing.T, h *OpenAIGatewayHandler, c *gin.Context, selection *service.AccountSelectionResult, fresh bool)
	// borrowRound 模拟换号成功的一轮：执行入口方法在换号成功后做的第一个动作 apply（写入 Temporary 标志）。
	borrowRound func(c *gin.Context, temporary bool)
}

func compositeEntryDrivers() []compositeEntryDriver {
	apiKey := &service.APIKey{Group: &service.Group{Platform: service.PlatformComposite}}
	resolve := func(context.Context) service.ChannelMappingResult { return service.ChannelMappingResult{} }
	return []compositeEntryDriver{
		{
			name: "Responses",
			plainRound: func(t *testing.T, h *OpenAIGatewayHandler, c *gin.Context, selection *service.AccountSelectionResult, fresh bool) {
				platform, capability := service.PlatformOpenAI, service.OpenAIEndpointCapabilityChatCompletions
				var mapping service.ChannelMappingResult
				forwardBody, forwardModel := []byte("{}"), "m"
				got := h.switchCompositeResponses(c, apiKey, nil, zap.NewNop(), selection, &compositeResponsesSwitch{
					Fresh: fresh, ReqModel: "m", Body: forwardBody, Platform: &platform, Capability: &capability,
					Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
				})
				require.Same(t, selection, got)
			},
			borrowRound: func(c *gin.Context, temporary bool) {
				platform, capability := service.PlatformOpenAI, service.OpenAIEndpointCapabilityChatCompletions
				var mapping service.ChannelMappingResult
				forwardBody, forwardModel := []byte("{}"), "m"
				st := &compositeResponsesSwitch{
					ReqModel: "m", Body: forwardBody, Platform: &platform, Capability: &capability,
					Mapping: &mapping, ForwardBody: &forwardBody, ForwardModel: &forwardModel,
				}
				st.apply(c, fakeCompositeSwitch(c, service.PlatformKimi, temporary), resolve, nil)
			},
		},
		{
			name: "Messages",
			plainRound: func(t *testing.T, h *OpenAIGatewayHandler, c *gin.Context, selection *service.AccountSelectionResult, fresh bool) {
				platform, mapped := service.PlatformOpenAI, ""
				var mapping service.ChannelMappingResult
				got := h.switchCompositeMessages(c, apiKey, nil, zap.NewNop(), selection, &compositeMessagesSwitch{
					Fresh: fresh, ReqModel: "m", RoutingModel: "r", CurrentRoutingModel: "r",
					Platform: &platform, EffectiveMappedModel: &mapped, Mapping: &mapping,
				})
				require.Same(t, selection, got)
			},
			borrowRound: func(c *gin.Context, temporary bool) {
				platform, mapped := service.PlatformOpenAI, ""
				var mapping service.ChannelMappingResult
				st := &compositeMessagesSwitch{ReqModel: "m", Platform: &platform, EffectiveMappedModel: &mapped, Mapping: &mapping}
				st.apply(c, apiKey, fakeCompositeSwitch(c, service.PlatformKimi, temporary), resolve)
			},
		},
		{
			name: "ChatCompletions",
			plainRound: func(t *testing.T, h *OpenAIGatewayHandler, c *gin.Context, selection *service.AccountSelectionResult, fresh bool) {
				platform, forwardModel := service.PlatformOpenAI, "m"
				var mapping service.ChannelMappingResult
				got := h.switchCompositeChat(c, apiKey, nil, zap.NewNop(), selection, &compositeChatSwitch{
					Fresh: fresh, ReqModel: "m", Platform: &platform, Mapping: &mapping, ForwardModel: &forwardModel,
				})
				require.Same(t, selection, got)
			},
			borrowRound: func(c *gin.Context, temporary bool) {
				platform, forwardModel := service.PlatformOpenAI, "m"
				var mapping service.ChannelMappingResult
				st := &compositeChatSwitch{ReqModel: "m", Platform: &platform, Mapping: &mapping, ForwardModel: &forwardModel}
				st.apply(c, fakeCompositeSwitch(c, service.PlatformKimi, temporary), resolve)
			},
		},
	}
}

// acquireAndReportBinding 用真实的 acquireResponsesAccountSlot 抢一次槽（排队计划快速抢到），
// 返回这一次抢槽新增的粘性绑定写入。
func acquireAndReportBinding(t *testing.T, h *OpenAIGatewayHandler, recorder *compositeStickyRecorder, c *gin.Context, sessionHash string, accountID int64) []compositeStickySet {
	t.Helper()
	before := len(recorder.snapshot())
	groupID := int64(7)
	streamStarted := false
	selection := &service.AccountSelectionResult{
		Account:  &service.Account{ID: accountID, Platform: service.PlatformOpenAI, Concurrency: 2},
		WaitPlan: &service.AccountWaitPlan{AccountID: accountID, MaxConcurrency: 2, Timeout: time.Second, MaxWaiting: 2},
	}
	release, result := h.acquireResponsesAccountSlot(c, &groupID, sessionHash, selection, false, &streamStarted, zap.NewNop())
	require.Equal(t, openAISlotAcquireOK, result)
	require.NotNil(t, release)
	release()
	return recorder.snapshot()[before:]
}

func newCompositeEntryWaitingSelection() *service.AccountSelectionResult {
	return &service.AccountSelectionResult{
		Account:  &service.Account{ID: 1, Platform: service.PlatformOpenAI},
		WaitPlan: &service.AccountWaitPlan{AccountID: 1, MaxConcurrency: 1},
	}
}

// 临时借用：抢槽与准入后绑定不得带会话哈希，粘性绑定不被改写；没借用：照常按会话哈希绑定。
func TestCompositeEntryAcquireBindsStickyOnlyWhenNotBorrowing(t *testing.T) {
	const hash = "sess-hash"
	wantBound := []compositeStickySet{{key: "openai:" + hash, accountID: 9}}
	for _, d := range compositeEntryDrivers() {
		t.Run(d.name, func(t *testing.T) {
			h, recorder := newCompositeStickyHandler(t)

			// 借用（临时）：不写绑定。
			c := newCompositeEntryTestContext()
			d.borrowRound(c, true)
			require.Empty(t, acquireAndReportBinding(t, h, recorder, c, hash, 9), "临时借用不得改写粘性会话绑定")

			// 换号成功但不是临时借用（没命中粘性）：照常绑定到新账号。
			c = newCompositeEntryTestContext()
			d.borrowRound(c, false)
			require.Equal(t, wantBound, acquireAndReportBinding(t, h, recorder, c, hash, 9))

			// 没换号：照常绑定。
			c = newCompositeEntryTestContext()
			d.plainRound(t, h, c, newCompositeEntryWaitingSelection(), true)
			require.Equal(t, wantBound, acquireAndReportBinding(t, h, recorder, c, hash, 9))
		})
	}
}

// 同一个 gin.Context 上先后两轮选号：标志必须每轮先复位，不能把上一轮的借用状态带到下一轮。
func TestCompositeEntryTemporaryFlagIsResetEachRound(t *testing.T) {
	const hash = "sess-hash"
	wantBound := func(id int64) []compositeStickySet {
		return []compositeStickySet{{key: "openai:" + hash, accountID: id}}
	}
	for _, d := range compositeEntryDrivers() {
		t.Run(d.name, func(t *testing.T) {
			h, recorder := newCompositeStickyHandler(t)

			// 第 1 轮借用、第 2 轮不借：第 2 轮必须照常绑定。
			c := newCompositeEntryTestContext()
			d.borrowRound(c, true)
			require.Empty(t, acquireAndReportBinding(t, h, recorder, c, hash, 11), "第 1 轮临时借用不绑定")
			d.plainRound(t, h, c, newCompositeEntryWaitingSelection(), true)
			require.Equal(t, wantBound(12), acquireAndReportBinding(t, h, recorder, c, hash, 12), "第 2 轮没借用，必须照常绑定，不能沿用上一轮的借用标志")

			// 第 1 轮借用、第 2 轮"已有账号失败"（入口方法不换号，直接放行）：标志同样要复位。
			c = newCompositeEntryTestContext()
			d.borrowRound(c, true)
			require.Empty(t, acquireAndReportBinding(t, h, recorder, c, hash, 13))
			d.plainRound(t, h, c, newCompositeEntryWaitingSelection(), false)
			require.Equal(t, wantBound(14), acquireAndReportBinding(t, h, recorder, c, hash, 14))

			// 第 1 轮不借、第 2 轮借用：第 2 轮不绑定。
			c = newCompositeEntryTestContext()
			d.plainRound(t, h, c, newCompositeEntryWaitingSelection(), true)
			require.Equal(t, wantBound(15), acquireAndReportBinding(t, h, recorder, c, hash, 15))
			d.plainRound(t, h, c, newCompositeEntryWaitingSelection(), true)
			d.borrowRound(c, true)
			require.Empty(t, acquireAndReportBinding(t, h, recorder, c, hash, 16), "第 2 轮临时借用不得绑定")
		})
	}
}

// 其它从不经过换号入口的调用方（生图、搜索、grok 等）：没有标志，照常绑定。
func TestCompositeEntryAcquireWithoutEntryRoundBindsNormally(t *testing.T) {
	h, recorder := newCompositeStickyHandler(t)
	c := newCompositeEntryTestContext()
	require.Equal(t, []compositeStickySet{{key: "openai:sess-hash", accountID: 21}},
		acquireAndReportBinding(t, h, recorder, c, "sess-hash", 21))
}

// ---------------------------------------------------------------------------
// 语法树守卫
// ---------------------------------------------------------------------------

func parseCompositeGuardFile(t *testing.T, name string) *ast.File {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), name, nil, 0)
	require.NoError(t, err)
	return file
}

func findCompositeGuardFunc(t *testing.T, file *ast.File, name string) *ast.FuncDecl {
	t.Helper()
	for _, decl := range file.Decls {
		if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == name && fn.Body != nil {
			return fn
		}
	}
	require.FailNowf(t, "找不到函数", "%s", name)
	return nil
}

// compositeSwitchCallName 返回 call 若是 h.switchCompositeXxx(...) 则为方法名，否则为空。
func compositeSwitchCallName(call *ast.CallExpr) string {
	if sel, ok := call.Fun.(*ast.SelectorExpr); ok && strings.HasPrefix(sel.Sel.Name, "switchComposite") {
		return sel.Sel.Name
	}
	return ""
}

// 三个入口的选号循环里各有且只有一次 switchComposite* 调用，返回值落到 selection，
// 实参（含资格相关实参）与期望表逐项一致，并且位于 account := selection.Account 之前。
func TestCompositeEntryCallSitesAreWired(t *testing.T) {
	const freshExpr = "len(failedAccountIDs) == 0 && switchCount == 0"
	cases := []struct {
		file, fn, method, literal string
		want                      map[string]string
	}{
		{
			// Responses：资格实参里必须包含 failedAccountIDs、switchCount、previousResponseID、imageIntent、legacyCompact、nativeV2。
			file: "openai_gateway_handler.go", fn: "Responses", method: "switchCompositeResponses", literal: "compositeResponsesSwitch",
			want: map[string]string{
				"Fresh": freshExpr, "PreviousResponseID": "previousResponseID", "ImageIntent": "imageIntent",
				"LegacyCompact": "legacyCompact", "NativeV2": "nativeV2", "NeedsResponses": "needsResponses",
				"RequireCompact": "requireCompact", "SessionHash": "sessionHash", "ReqModel": "reqModel", "Body": "body",
				"Platform": "&requestPlatform", "Capability": "&requiredCapability", "Mapping": "&channelMapping",
				"ForwardBody": "&forwardBody", "ForwardModel": "&forwardModel",
			},
		},
		{
			// Messages：RoutingModel 与 CurrentRoutingModel 不能对调。
			file: "openai_gateway_handler.go", fn: "Messages", method: "switchCompositeMessages", literal: "compositeMessagesSwitch",
			want: map[string]string{
				"Fresh": freshExpr, "SessionHash": "sessionHash", "ReqModel": "reqModel",
				"RoutingModel": "routingModel", "CurrentRoutingModel": "currentRoutingModel",
				"Platform": "&requestPlatform", "EffectiveMappedModel": "&effectiveMappedModel", "Mapping": "&channelMappingMsg",
			},
		},
		{
			file: "openai_chat_completions.go", fn: "ChatCompletions", method: "switchCompositeChat", literal: "compositeChatSwitch",
			want: map[string]string{
				"Fresh": freshExpr, "SessionHash": "sessionHash", "ReqModel": "reqModel",
				"Platform": "&requestPlatform", "Mapping": "&channelMapping", "ForwardModel": "&forwardModel",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.fn, func(t *testing.T) {
			fn := findCompositeGuardFunc(t, parseCompositeGuardFile(t, tc.file), tc.fn)

			// 整个入口函数里 switchComposite* 调用恰好一次（任何位置）。
			total := 0
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				if call, ok := n.(*ast.CallExpr); ok && compositeSwitchCallName(call) != "" {
					total++
				}
				return true
			})
			require.Equal(t, 1, total, "%s 里 switchComposite* 调用必须恰好一次", tc.fn)

			// 它必须是某个 for 循环体的直接子语句：selection = h.switchCompositeXxx(c, apiKey, subscription, reqLog, selection, &literal{...})
			found := false
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				loop, ok := n.(*ast.ForStmt)
				if !ok {
					return true
				}
				switchIdx, accountIdx := -1, -1
				for i, stmt := range loop.Body.List {
					assign, ok := stmt.(*ast.AssignStmt)
					if !ok || len(assign.Lhs) != 1 || len(assign.Rhs) != 1 {
						continue
					}
					if lhs, ok := assign.Lhs[0].(*ast.Ident); ok && lhs.Name == "account" && assign.Tok == token.DEFINE {
						if types.ExprString(assign.Rhs[0]) == "selection.Account" && accountIdx < 0 {
							accountIdx = i
						}
						continue
					}
					call, ok := assign.Rhs[0].(*ast.CallExpr)
					if !ok || compositeSwitchCallName(call) != tc.method {
						continue
					}
					require.Equal(t, token.ASSIGN, assign.Tok, "返回值必须赋给已有的 selection")
					require.Equal(t, "selection", types.ExprString(assign.Lhs[0]), "返回值必须落到 selection")
					require.Len(t, call.Args, 6)
					for idx, want := range []string{"c", "apiKey", "subscription", "reqLog", "selection"} {
						require.Equal(t, want, types.ExprString(call.Args[idx]), "第 %d 个实参", idx+1)
					}
					unary, ok := call.Args[5].(*ast.UnaryExpr)
					require.True(t, ok && unary.Op == token.AND, "最后一个实参必须是 &%s{...}", tc.literal)
					lit, ok := unary.X.(*ast.CompositeLit)
					require.True(t, ok)
					require.Equal(t, tc.literal, types.ExprString(lit.Type))
					got := map[string]string{}
					for _, elt := range lit.Elts {
						kv, ok := elt.(*ast.KeyValueExpr)
						require.True(t, ok, "字段必须写成 key: value")
						got[types.ExprString(kv.Key)] = types.ExprString(kv.Value)
					}
					require.Equal(t, tc.want, got, "%s 调用点实参与期望表不一致", tc.fn)
					switchIdx = i
				}
				if switchIdx >= 0 {
					found = true
					require.True(t, accountIdx < 0 || switchIdx < accountIdx, "换号必须在 account := selection.Account 之前")
				}
				return true
			})
			require.True(t, found, "%s 的选号循环里缺少 selection = h.%s(...)", tc.fn, tc.method)
		})
	}
}

// acquireResponsesAccountSlot 必须把 compositeSlotSessionHash(c, sessionHash) 的"结果"
// 作为第 3 个实参传给 acquireOpenAIAccountSlot；调用了却丢掉结果、仍传原哈希，必须红。
func TestCompositeAcquireSlotPassesSlotSessionHashResult(t *testing.T) {
	fn := findCompositeGuardFunc(t, parseCompositeGuardFile(t, "openai_gateway_handler.go"), "acquireResponsesAccountSlot")
	acquireCalls := 0
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "acquireOpenAIAccountSlot" {
			return true
		}
		acquireCalls++
		require.GreaterOrEqual(t, len(call.Args), 3)
		hashArg, ok := call.Args[2].(*ast.CallExpr)
		require.True(t, ok, "第 3 个实参必须是 compositeSlotSessionHash(...) 的调用结果，实际为 %s", types.ExprString(call.Args[2]))
		require.Equal(t, "compositeSlotSessionHash(c, sessionHash)", types.ExprString(hashArg))
		return true
	})
	require.Equal(t, 1, acquireCalls, "acquireResponsesAccountSlot 里必须恰好一次调用 acquireOpenAIAccountSlot")
}

// 三个 switchComposite* 方法的胶水形状：先复位标志；带 tryCompositeCapacitySwitch；
// 没换号时原样返回 selection；换号成功时返回 st.apply 的结果。
func TestCompositeSwitchMethodsGlueShape(t *testing.T) {
	file := parseCompositeGuardFile(t, "openai_gateway_composite_entry.go")
	for _, name := range []string{"switchCompositeResponses", "switchCompositeMessages", "switchCompositeChat"} {
		t.Run(name, func(t *testing.T) {
			fn := findCompositeGuardFunc(t, file, name)
			stmts := fn.Body.List
			require.GreaterOrEqual(t, len(stmts), 4)

			first, ok := stmts[0].(*ast.ExprStmt)
			require.True(t, ok)
			require.Equal(t, "setCompositeTemporarySwap(c, false)", types.ExprString(first.X), "第一句必须先复位临时借用标志")

			tryCalls := 0
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				if call, ok := n.(*ast.CallExpr); ok {
					if sel, ok := call.Fun.(*ast.SelectorExpr); ok && sel.Sel.Name == "tryCompositeCapacitySwitch" {
						tryCalls++
					}
				}
				return true
			})
			require.Equal(t, 1, tryCalls)

			// if !switched { return selection }
			var guard *ast.IfStmt
			for _, stmt := range stmts {
				if ifStmt, ok := stmt.(*ast.IfStmt); ok && types.ExprString(ifStmt.Cond) == "!switched" {
					guard = ifStmt
				}
			}
			require.NotNil(t, guard, "缺少 if !switched")
			require.Len(t, guard.Body.List, 1)
			ret, ok := guard.Body.List[0].(*ast.ReturnStmt)
			require.True(t, ok)
			require.Len(t, ret.Results, 1)
			require.Equal(t, "selection", types.ExprString(ret.Results[0]), "没换号必须原样返回 selection")

			// 最后一句：return st.apply(...)
			last, ok := stmts[len(stmts)-1].(*ast.ReturnStmt)
			require.True(t, ok)
			require.Len(t, last.Results, 1)
			call, ok := last.Results[0].(*ast.CallExpr)
			require.True(t, ok)
			require.True(t, strings.HasPrefix(types.ExprString(call.Fun), "st.apply"), "换号成功必须返回 st.apply 的结果，实际为 %s", types.ExprString(last.Results[0]))
		})
	}
}
