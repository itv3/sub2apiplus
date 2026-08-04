package main

// 本文件定义 sink 与 facade 的**完整限定签名**清单。
//
// 方案 §9 明确要求「按包路径、接收者类型和方法识别调用，不能只按 Do、Send 等
// selector 名做文本匹配」。前一版正则实现正是按 selector 名匹配，实测会：
//   - 漏掉 http.DefaultClient.Do、任意变量 hc.Do、RoundTripper.RoundTrip、DialContext
//   - 把业务代码里任意名为 Do 的方法误当成 HTTP 发送
//   - facade 回溯按裸函数名关联，混入大量同名符号
//
// 这里的每一项都是 go/types 解析出的完整限定名，形如：
//   (*net/http.Client).Do
//   net/http.Get
//   (github.com/imroc/req/v3.Request).Get

// terminalSinks 是真正把字节送出进程的调用点。
//
// 命中即意味着「此处会发生一次网络发送」，无论目标是不是官方 host——目标解析是
// 另一个维度，dynamic/unknown 同样要输出（方案 §9 第 2 点）。
var terminalSinks = map[string]sinkKindMeta{
	// ---- net/http 标准库 ----
	"(*net/http.Client).Do":             {kind: "net_http_client_do", protocol: "http"},
	"(*net/http.Client).Get":            {kind: "net_http_client_get", protocol: "http"},
	"(*net/http.Client).Post":           {kind: "net_http_client_post", protocol: "http"},
	"(*net/http.Client).PostForm":       {kind: "net_http_client_postform", protocol: "http"},
	"(*net/http.Client).Head":           {kind: "net_http_client_head", protocol: "http"},
	"net/http.Get":                      {kind: "net_http_pkg_get", protocol: "http"},
	"net/http.Post":                     {kind: "net_http_pkg_post", protocol: "http"},
	"net/http.PostForm":                 {kind: "net_http_pkg_postform", protocol: "http"},
	"net/http.Head":                     {kind: "net_http_pkg_head", protocol: "http"},
	"(*net/http.Transport).RoundTrip":   {kind: "transport_roundtrip", protocol: "http"},
	"(net/http.RoundTripper).RoundTrip": {kind: "roundtripper_roundtrip", protocol: "http"},

	// ---- req/v3 ----
	// Request 上的终态方法：真正触发发送。
	"(*github.com/imroc/req/v3.Request).Get":         {kind: "reqv3_get", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Post":        {kind: "reqv3_post", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Patch":       {kind: "reqv3_patch", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Put":         {kind: "reqv3_put", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Delete":      {kind: "reqv3_delete", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Head":        {kind: "reqv3_head", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Options":     {kind: "reqv3_options", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Send":        {kind: "reqv3_send", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).Do":          {kind: "reqv3_do", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustGet":     {kind: "reqv3_mustget", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustPost":    {kind: "reqv3_mustpost", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustPut":     {kind: "reqv3_mustput", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustPatch":   {kind: "reqv3_mustpatch", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustDelete":  {kind: "reqv3_mustdelete", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustHead":    {kind: "reqv3_musthead", protocol: "http"},
	"(*github.com/imroc/req/v3.Request).MustOptions": {kind: "reqv3_mustoptions", protocol: "http"},
	"(*github.com/imroc/req/v3.Client).Do":           {kind: "reqv3_client_do", protocol: "http"},

	// ---- WebSocket ----
	"github.com/coder/websocket.Dial":                    {kind: "ws_coder_dial", protocol: "websocket"},
	"github.com/gorilla/websocket.(*Dialer).Dial":        {kind: "ws_gorilla_dial", protocol: "websocket"},
	"(*github.com/gorilla/websocket.Dialer).Dial":        {kind: "ws_gorilla_dial", protocol: "websocket"},
	"(*github.com/gorilla/websocket.Dialer).DialContext": {kind: "ws_gorilla_dialctx", protocol: "websocket"},

	// ---- 裸传输 ----
	"net.Dial":                         {kind: "raw_net_dial", protocol: "raw"},
	"net.DialTimeout":                  {kind: "raw_net_dial_timeout", protocol: "raw"},
	"(*net.Dialer).Dial":               {kind: "raw_dialer_dial", protocol: "raw"},
	"(*net.Dialer).DialContext":        {kind: "raw_dialer_dialctx", protocol: "raw"},
	"crypto/tls.Dial":                  {kind: "raw_tls_dial", protocol: "raw"},
	"crypto/tls.DialWithDialer":        {kind: "raw_tls_dial_dialer", protocol: "raw"},
	"(*crypto/tls.Dialer).DialContext": {kind: "raw_tls_dialer_dialctx", protocol: "raw"},
}

// projectFacades 是项目内部转发发送的中间层。
//
// 它们本身不是 terminal sink，但业务身份在此处被折叠：实测
// doOpenAIHTTPUpstreamWithProfile 一个 facade 承载了 11 个语义不同的业务调用点，
// 若只登记 terminal sink，全部业务路径会塌缩成同一个 SinkID，per-sink
// enforcement 与 per-sink 回滚同时失效。
//
// 因此 facade 的**调用点**才是 SinkID 的锚点。
var projectFacades = map[string]sinkKindMeta{
	"(github.com/Wei-Shaw/sub2api/internal/service.HTTPUpstream).Do":                                      {kind: "facade_http_upstream_do", protocol: "http"},
	"(github.com/Wei-Shaw/sub2api/internal/service.HTTPUpstream).DoWithTLS":                               {kind: "facade_http_upstream_do_tls", protocol: "http"},
	"github.com/Wei-Shaw/sub2api/internal/service.doOpenAIHTTPUpstreamWithProfile":                        {kind: "facade_openai_upstream", protocol: "http"},
	"(*github.com/Wei-Shaw/sub2api/internal/service.OpenAIGatewayService).doOpenAIHTTPUpstreamForRequest": {kind: "facade_openai_upstream_req", protocol: "http"},
	// 项目自己的 WS dialer 接口。
	//
	// 必须显式登记：*coderOpenAIWSClientDialer.Dial 是具体类型的方法，能被扫到，
	// 但连接池与 v2 passthrough 是通过 openAIWSClientDialer **接口**发起拨号的
	// （p.clientDialer.Dial / dialer.Dial）。只登记具体类型会漏掉这些真实的
	// WS 出站调用点，而它们恰恰是业务调用点、是 RuntimeSinkID 的锚点。
	"(github.com/Wei-Shaw/sub2api/internal/service.openAIWSClientDialer).Dial":                                     {kind: "facade_ws_dialer", protocol: "websocket"},
	"(github.com/Wei-Shaw/sub2api/internal/service.openAIWSTransportMetricsDialer).Dial":                           {kind: "facade_ws_dialer_metrics", protocol: "websocket"},
	"(*github.com/Wei-Shaw/sub2api/internal/service.OfficialEgressTransitionRuntime).DispatchCodexLegacyHTTP":      {kind: "facade_legacy_compiled_http", protocol: "http"},
	"(*github.com/Wei-Shaw/sub2api/internal/service.OfficialEgressTransitionRuntime).DispatchCodexLegacyWebSocket": {kind: "facade_legacy_compiled_ws", protocol: "websocket"},
	"(github.com/Wei-Shaw/sub2api/internal/repository.openAIOAuthCompiledTransport).Do":                            {kind: "facade_legacy_compiled_req_profile", protocol: "http"},
	"github.com/Wei-Shaw/sub2api/internal/repository.dispatchOpenAIOAuthLegacy":                                    {kind: "facade_legacy_compiled_req_profile", protocol: "http"},
	"github.com/Wei-Shaw/sub2api/internal/pkg/httpclient.GetClient":                                                {kind: "factory_httpclient_pool", protocol: "http"},
	"github.com/Wei-Shaw/sub2api/internal/repository.CreatePrivacyReqClient":                                       {kind: "factory_privacy_chrome", protocol: "http"},
	"github.com/Wei-Shaw/sub2api/internal/repository.getSharedReqClient":                                           {kind: "factory_shared_req", protocol: "http"},
}

type sinkKindMeta struct {
	kind     string
	protocol string
}

// officialHosts 是判定 sink 是否进入身份约束范围的 host 闭集。
//
// 子域必须分列：auth.openai.com 承载 OAuth/PAT/Agent 身份端点，api.openai.com 既有
// 第三方兼容层也有 realtime sideband WS，语义完全不同，不能合并成一条 openai.com 规则。
// *.oaiusercontent.com 的 blob 上传 host 由上游响应返回（HostFromResponse），
// 静态不可解析，只能标记为 dynamic 并依赖运行时 Guard。
var officialHosts = []string{
	"chatgpt.com",
	"auth.openai.com",
	"api.openai.com",
	"oaiusercontent.com",
	"api.anthropic.com",
	"console.anthropic.com",
}
