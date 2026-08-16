package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// officialCodexEndpointURLInput 是端点 URL 的唯一动态输入面。
//
// 版本画像负责 host、固定 path 与固定 query；调用方只允许填充画像明确声明的
// path/query 占位符。文件直传是唯一例外：它必须完整使用服务端返回的绝对 URL，
// 不能把签名 query 拆开后重新编码。
type officialCodexEndpointURLInput struct {
	PathValues  map[string]string
	QueryValues map[string]string
	ReturnedURL string
}

// officialCodexHeaderField 是 header 契约执行后的有序字段。
//
// Name 是逻辑名，WireName 是画像要求的线形名；普通 HTTP 两者均为小写，WS 的
// 前五项则由 WireName 保留 tungstenite 的固定大小写。Slot/Sequence 让测试和
// wire 编译器都能验证“槽位优先、槽内序号其次”的稳定顺序。
type officialCodexHeaderField struct {
	Slot     int
	Sequence int
	Name     string
	WireName string
	Value    string
}

// officialCodexNormalizeDerivedToolPresentation 检查第三方派生路径是否已经
// 提供 Codex 的 namespace/imagegen 形态。hosted image_generation 没有等价的
// 本地工具调度闭环，不能只改一个 type 就冒充官方行为，因此明确拒绝。
func officialCodexNormalizeDerivedToolPresentation(
	version string,
	endpointID codexEndpointID,
	payload map[string]any,
) (bool, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return false, err
	}
	contract := profile.ToolPresentation
	if !officialCodexToolPresentationApplies(contract, endpointID) || payload == nil {
		return false, nil
	}
	rawTools, exists := payload["tools"]
	if !exists || rawTools == nil {
		return false, nil
	}
	tools, ok := rawTools.([]any)
	if !ok {
		return false, errors.New("Codex Responses tools 必须是数组")
	}

	for index, rawTool := range tools {
		tool, object := rawTool.(map[string]any)
		if !object || officialCodexMapString(tool, "type") != contract.HostedImageGenerationType {
			continue
		}
		if !contract.HostedImageGenerationAllowed {
			return false, fmt.Errorf(
				"Codex tools[%d] 不支持 hosted %s；必须由客户端提供完整 %s/%s 工具定义",
				index,
				contract.HostedImageGenerationType,
				contract.NamespaceName,
				contract.FunctionName,
			)
		}
	}
	return false, nil
}

// officialCodexValidateToolPresentation 在最终 JSON 闭包之后执行语义校验。
// 顶层字段闭集不能识别 tools 内部的协议差异，因此这条检查仍由同一版本画像驱动，
// 不按模型名、路由名或 header 临时猜测。
func officialCodexValidateToolPresentation(
	version string,
	endpointID codexEndpointID,
	body []byte,
	responsesLite bool,
) error {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return err
	}
	contract := profile.ToolPresentation
	if !officialCodexToolPresentationApplies(contract, endpointID) {
		return nil
	}
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return fmt.Errorf("解析 Codex 工具呈现 body：%w", err)
	}
	if err := officialCodexValidateToolList(contract, payload["tools"], !responsesLite, "tools"); err != nil {
		return err
	}
	input, _ := payload["input"].([]any)
	for index, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || officialCodexMapString(item, "type") != contract.LiteCarrierItemType {
			continue
		}
		if err := officialCodexValidateToolList(
			contract,
			item["tools"],
			responsesLite,
			fmt.Sprintf("input[%d].tools", index),
		); err != nil {
			return err
		}
	}
	return nil
}

func officialCodexToolPresentationApplies(
	contract officialCodexToolPresentationProfile,
	endpointID codexEndpointID,
) bool {
	for _, candidate := range contract.EndpointIDs {
		if candidate == string(endpointID) {
			return true
		}
	}
	return false
}

func officialCodexValidateToolList(
	contract officialCodexToolPresentationProfile,
	rawTools any,
	allowImageNamespace bool,
	location string,
) error {
	if rawTools == nil {
		return nil
	}
	tools, ok := rawTools.([]any)
	if !ok {
		return fmt.Errorf("Codex %s 必须是数组", location)
	}
	for index, rawTool := range tools {
		tool, object := rawTool.(map[string]any)
		if !object {
			continue
		}
		toolType := officialCodexMapString(tool, "type")
		if toolType == contract.HostedImageGenerationType && !contract.HostedImageGenerationAllowed {
			return fmt.Errorf("Codex %s[%d] 禁止 hosted %s 工具", location, index, toolType)
		}
		if toolType != contract.NamespaceType || officialCodexMapString(tool, "name") != contract.NamespaceName {
			continue
		}
		if !allowImageNamespace {
			return fmt.Errorf("Codex %s[%d] 的 image_gen namespace 位于错误载体", location, index)
		}
		if err := officialCodexValidateImageNamespaceDefinition(contract, tool); err != nil {
			return fmt.Errorf("Codex %s[%d] 的 image_gen 定义无效：%w", location, index, err)
		}
	}
	return nil
}

func officialCodexValidateImageNamespaceDefinition(
	contract officialCodexToolPresentationProfile,
	namespace map[string]any,
) error {
	for _, field := range contract.NamespaceRequiredFields {
		if _, exists := namespace[field]; !exists {
			return fmt.Errorf("缺少 namespace 字段 %s", field)
		}
	}
	if officialCodexMapString(namespace, "description") == "" {
		return errors.New("namespace description 为空")
	}
	tools, _ := namespace["tools"].([]any)
	for _, rawTool := range tools {
		tool, ok := rawTool.(map[string]any)
		if ok && officialCodexMapString(tool, "type") == contract.FunctionType &&
			officialCodexMapString(tool, "name") == contract.FunctionName {
			for _, field := range contract.FunctionRequiredFields {
				if _, exists := tool[field]; !exists {
					return fmt.Errorf("缺少 imagegen 字段 %s", field)
				}
			}
			if officialCodexMapString(tool, "description") == "" {
				return errors.New("imagegen description 为空")
			}
			if _, ok := tool["strict"].(bool); !ok {
				return errors.New("imagegen strict 必须是布尔值")
			}
			if _, ok := tool["parameters"].(map[string]any); !ok {
				return errors.New("imagegen parameters 必须是 object")
			}
			return nil
		}
	}
	return errors.New("缺少 image_gen/imagegen 工具")
}

func officialCodexMapString(values map[string]any, name string) string {
	value, _ := values[name].(string)
	return strings.TrimSpace(value)
}

// officialCodexResolveTLSProfile 把不可变版本画像编译成一次调用独占的 TLS
// 与 HTTP/1.1 wire 画像。返回值中的所有切片都是新分配的，调用方修改它不会污染
// 后续解析，也不会改变版本画像摘要。
func officialCodexResolveTLSProfile(version, transportID string) (*tlsfingerprint.Profile, error) {
	versionProfile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexTLSProfile(versionProfile, transportID)
}

func resolveOfficialCodexTLSProfile(
	versionProfile *officialCodexVersionProfile,
	transportID string,
) (*tlsfingerprint.Profile, error) {
	if versionProfile == nil {
		return nil, errors.New("Codex 传输画像为空")
	}
	var transport *officialCodexTransportProfile
	for index := range versionProfile.Transports {
		if versionProfile.Transports[index].ID == transportID {
			transport = &versionProfile.Transports[index]
			break
		}
	}
	if transport == nil {
		return nil, fmt.Errorf("Codex %s 不支持传输画像：%q", versionProfile.Version, transportID)
	}

	rules, err := officialCodexCompileH1Rules(versionProfile, *transport)
	if err != nil {
		return nil, err
	}
	preserveHeaderCase := []string(nil)
	lowercaseHeaders := transport.LowercaseHTTPHeaders
	protocolLabel := "HTTP"
	if transport.WebSocket != nil {
		preserveHeaderCase = append(preserveHeaderCase, transport.WebSocket.FixedHandshakePrefix...)
		// tungstenite 只保留固定前缀的大小写，其余 HeaderMap 名称仍必须小写。
		lowercaseHeaders = true
		protocolLabel = "WebSocket"
	}

	return &tlsfingerprint.Profile{
		Name: fmt.Sprintf("Official Codex CLI %s %s (%s)", versionProfile.Version, protocolLabel, transport.ID),
		Transport: tlsfingerprint.TransportOptions{
			DisableCompression: true,
			LowercaseHeaders:   lowercaseHeaders,
			PreserveHeaderCase: preserveHeaderCase,
			H1HeaderOrders:     rules,
			StrictH1Wire:       true,
		},
		CipherSuites:        append([]uint16(nil), transport.CipherSuites...),
		Curves:              append([]uint16(nil), transport.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), transport.SignatureAlgorithms...),
		ALPNProtocols:       append([]string(nil), transport.ALPN...),
		SupportedVersions:   append([]uint16(nil), transport.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), transport.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), transport.PSKModes...),
		Extensions:          append([]uint16(nil), transport.Extensions...),
		RandomizeExtensions: transport.RandomizeExtensions,
		TLSVersMin:          transport.TLSMinVersion,
		TLSVersMax:          transport.TLSMaxVersion,
	}, nil
}

// officialCodexResolveEndpointTLSProfile 先严格解析端点，再通过端点声明的
// TransportID 编译传输。未知端点不会退回默认 HTTP 画像。
func officialCodexResolveEndpointTLSProfile(
	version string,
	endpointID codexEndpointID,
) (*tlsfingerprint.Profile, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexEndpointTLSProfile(profile, string(endpointID), nil)
}

// officialCodexResolveEndpointTLSProfileForURL 为带 path 占位符或服务端返回
// URL 的端点生成“本次请求精确路径”规则。通用传输画像不能把 {file_id} 当作通配
// 模式，否则 strict H1 会重新引入模糊匹配；因此文件端点必须在 URL 定型后调用此
// 函数，把画像中的模板规则替换为当前 URL 的 EscapedPath。
func officialCodexResolveEndpointTLSProfileForURL(
	version string,
	endpointID codexEndpointID,
	target *url.URL,
) (*tlsfingerprint.Profile, error) {
	versionProfile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexEndpointTLSProfile(versionProfile, string(endpointID), target)
}

func resolveOfficialCodexEndpointTLSProfile(
	versionProfile *officialCodexVersionProfile,
	endpointID string,
	target *url.URL,
) (*tlsfingerprint.Profile, error) {
	if versionProfile == nil {
		return nil, errors.New("Codex 端点传输画像为空")
	}
	endpoint, err := versionProfile.ResolveEndpoint(endpointID)
	if err != nil {
		return nil, err
	}
	if target == nil {
		return resolveOfficialCodexTLSProfile(versionProfile, endpoint.TransportID)
	}
	if target == nil || target.Host == "" || target.EscapedPath() == "" {
		return nil, fmt.Errorf("Codex 端点 %s 缺少已解析绝对 URL", endpoint.ID)
	}
	expectedScheme := "https"
	if endpoint.Upgrade == "websocket" {
		expectedScheme = "wss"
	}
	if target.Scheme != expectedScheme || target.User != nil || target.Fragment != "" {
		return nil, fmt.Errorf("Codex 端点 %s 的 URL 必须是无 userinfo/fragment 的 %s 绝对 URL", endpoint.ID, expectedScheme)
	}
	if !officialCodexHostMatches(endpoint.Host, target.Hostname()) {
		return nil, fmt.Errorf("Codex 端点 %s 的 URL host %q 不匹配画像 %q", endpoint.ID, target.Hostname(), endpoint.Host)
	}
	if !endpoint.HostFromResponse &&
		!officialCodexEscapedPathMatchesTemplate(endpoint.Path, target.EscapedPath()) {
		return nil, fmt.Errorf("Codex 端点 %s 的 URL path %q 不匹配画像 %q", endpoint.ID, target.EscapedPath(), endpoint.Path)
	}

	profile, err := resolveOfficialCodexTLSProfile(versionProfile, endpoint.TransportID)
	if err != nil {
		return nil, err
	}
	replaced := false
	for index := range profile.Transport.H1HeaderOrders {
		rule := &profile.Transport.H1HeaderOrders[index]
		if rule.Method == endpoint.Method && rule.Path == endpoint.Path {
			rule.Path = target.EscapedPath()
			replaced = true
			break
		}
	}
	if !replaced {
		return nil, fmt.Errorf("Codex 端点 %s 缺少可替换的精确 H1 规则", endpoint.ID)
	}
	return profile, nil
}

func officialCodexEscapedPathMatchesTemplate(template, escapedPath string) bool {
	remainingTemplate := template
	remainingPath := escapedPath
	for {
		start := strings.IndexByte(remainingTemplate, '{')
		if start < 0 {
			return remainingPath == remainingTemplate
		}
		endOffset := strings.IndexByte(remainingTemplate[start+1:], '}')
		if endOffset < 0 || !strings.HasPrefix(remainingPath, remainingTemplate[:start]) {
			return false
		}
		end := start + 1 + endOffset
		remainingPath = remainingPath[start:]
		remainingTemplate = remainingTemplate[end+1:]
		nextStart := strings.IndexByte(remainingTemplate, '{')
		nextLiteral := remainingTemplate
		if nextStart >= 0 {
			nextLiteral = remainingTemplate[:nextStart]
		}
		if nextLiteral == "" {
			if nextStart < 0 {
				return remainingPath != ""
			}
			return false
		}
		nextIndex := strings.Index(remainingPath, nextLiteral)
		if nextIndex <= 0 {
			return false
		}
		remainingPath = remainingPath[nextIndex:]
	}
}

// officialCodexCompileH1Rules 为同一传输编译完整端点矩阵，不产生空路径兜底。
// 普通 HTTP 直接使用画像最终槽位序；WS 使用画像声明的真实 HeaderMap 初始序，
// wire 层先按实际存在的条件头建表，再依次 swap_remove 固定五项，最后追加
// tungstenite 在删除之后生成的扩展头。不能从完整样本的最终顺序反推一个“无扰动”
// 初序，否则条件头缺席时只会原位删除，无法复刻官方的整体线序变化。
func officialCodexCompileH1Rules(
	versionProfile *officialCodexVersionProfile,
	transport officialCodexTransportProfile,
) ([]tlsfingerprint.H1HeaderOrderRule, error) {
	if versionProfile == nil {
		return nil, errors.New("Codex H1 规则缺少版本画像")
	}
	rules := make([]tlsfingerprint.H1HeaderOrderRule, 0, len(versionProfile.Endpoints))
	for _, endpoint := range versionProfile.Endpoints {
		if endpoint.TransportID != transport.ID {
			continue
		}
		if endpoint.Method == "" || endpoint.Path == "" {
			return nil, fmt.Errorf("Codex 端点 %s 缺少精确 method/path", endpoint.ID)
		}
		orderedHeaders := endpoint.OrderedHeaders()
		desired := make([]string, 0, len(orderedHeaders))
		for _, slot := range orderedHeaders {
			name := strings.ToLower(strings.TrimSpace(slot.Name))
			if name == "" {
				return nil, fmt.Errorf("Codex 端点 %s 包含空 header 槽位", endpoint.ID)
			}
			desired = append(desired, name)
		}

		rule := tlsfingerprint.H1HeaderOrderRule{
			Method:         endpoint.Method,
			Path:           endpoint.Path,
			RejectUnlisted: true,
		}
		if endpoint.Upgrade == "" {
			if endpoint.HeaderOrderMode != officialCodexHeaderOrderH1HeaderMap &&
				endpoint.HeaderOrderMode != officialCodexHeaderOrderExplicit {
				return nil, fmt.Errorf("Codex HTTP 端点 %s 使用未知 header 次序模式：%s", endpoint.ID, endpoint.HeaderOrderMode)
			}
			rule.Mode = tlsfingerprint.H1HeaderOrderModeStatic
			rule.Order = desired
			rules = append(rules, rule)
			continue
		}

		if transport.WebSocket == nil || endpoint.HeaderOrderMode != officialCodexHeaderOrderWSSwapRemove {
			return nil, fmt.Errorf("Codex WS 端点 %s 缺少 swap_remove 传输画像", endpoint.ID)
		}
		prefix := officialCodexLowerHeaderNames(transport.WebSocket.FixedHandshakePrefix)
		if len(prefix) == 0 || len(desired) < len(prefix) {
			return nil, fmt.Errorf("Codex WS 端点 %s 的固定前缀不完整", endpoint.ID)
		}
		for index := range prefix {
			if desired[index] != prefix[index] {
				return nil, fmt.Errorf("Codex WS 端点 %s 的第 %d 个固定头不是 %s", endpoint.ID, index+1, prefix[index])
			}
		}

		initialOrder, appendHeaders, err := officialCodexCompileWSHeaderConstruction(
			endpoint,
			desired,
			prefix,
		)
		if err != nil {
			return nil, err
		}
		rule.Mode = tlsfingerprint.H1HeaderOrderModeSwapRemove
		rule.Order = initialOrder
		rule.PrefixHeaders = append([]string(nil), prefix...)
		rule.RemoveHeaders = append([]string(nil), prefix...)
		rule.AppendHeaders = appendHeaders
		rules = append(rules, rule)
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("Codex %s 传输 %s 没有端点 H1 规则", versionProfile.Version, transport.ID)
	}
	return rules, nil
}

// officialCodexCompileWSHeaderConstruction 把端点画像中的构造阶段数据编译为
// wire 执行规则，并验证“初始 HeaderMap + 删除后追加”恰好覆盖端点 header 闭集。
// 这使新增版本必须显式给出源码/抓包可追溯的构造序，不能退回完整样本反推。
func officialCodexCompileWSHeaderConstruction(
	endpoint officialCodexEndpointProfile,
	desired []string,
	prefix []string,
) ([]string, []string, error) {
	initialOrder := officialCodexLowerHeaderNames(endpoint.HeaderMapInsertionOrder)
	appendHeaders := officialCodexLowerHeaderNames(endpoint.PostRemoveHeaders)
	if len(initialOrder) < len(prefix) {
		return nil, nil, fmt.Errorf("Codex WS 端点 %s 缺少 HeaderMap 构造序", endpoint.ID)
	}
	for index := range prefix {
		if initialOrder[index] != prefix[index] {
			return nil, nil, fmt.Errorf(
				"Codex WS 端点 %s 的 HeaderMap 第 %d 项不是 %s",
				endpoint.ID,
				index+1,
				prefix[index],
			)
		}
	}

	expected := make(map[string]struct{}, len(desired))
	for _, name := range desired {
		expected[name] = struct{}{}
	}
	seen := make(map[string]struct{}, len(desired))
	for _, group := range [][]string{initialOrder, appendHeaders} {
		for _, name := range group {
			if name == "" {
				return nil, nil, fmt.Errorf("Codex WS 端点 %s 的 HeaderMap 构造序包含空项", endpoint.ID)
			}
			if _, duplicate := seen[name]; duplicate {
				return nil, nil, fmt.Errorf("Codex WS 端点 %s 的 HeaderMap 构造序重复：%s", endpoint.ID, name)
			}
			if _, ok := expected[name]; !ok {
				return nil, nil, fmt.Errorf("Codex WS 端点 %s 的 HeaderMap 构造序包含未知头：%s", endpoint.ID, name)
			}
			seen[name] = struct{}{}
		}
	}
	for name := range expected {
		if _, ok := seen[name]; !ok {
			return nil, nil, fmt.Errorf("Codex WS 端点 %s 的 HeaderMap 构造序缺少：%s", endpoint.ID, name)
		}
	}
	return initialOrder, appendHeaders, nil
}

func officialCodexLowerHeaderNames(names []string) []string {
	result := make([]string, len(names))
	for index, name := range names {
		result[index] = strings.ToLower(strings.TrimSpace(name))
	}
	return result
}

// officialCodexBuildEndpointURL 根据端点画像生成完整 URL。固定 query 只能来自
// 画像，动态 query 必须逐项提供；任何画像外字段都会失败，禁止把入站 query 整体
// 透传到官方端点。
func officialCodexBuildEndpointURL(
	version string,
	endpointID codexEndpointID,
	input officialCodexEndpointURLInput,
) (*url.URL, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return buildOfficialCodexEndpointURL(profile, string(endpointID), input)
}

func buildOfficialCodexEndpointURL(
	profile *officialCodexVersionProfile,
	endpointID string,
	input officialCodexEndpointURLInput,
) (*url.URL, error) {
	if profile == nil {
		return nil, errors.New("Codex URL 画像为空")
	}
	endpoint, err := profile.ResolveEndpoint(endpointID)
	if err != nil {
		return nil, err
	}
	if endpoint.HostFromResponse {
		return officialCodexValidateReturnedURL(endpoint, input)
	}
	if strings.TrimSpace(input.ReturnedURL) != "" {
		return nil, fmt.Errorf("Codex 端点 %s 不接受服务端返回 URL", endpoint.ID)
	}

	expandedPath, rawPath, usedPathValues, err := officialCodexExpandEndpointPath(endpoint.Path, input.PathValues)
	if err != nil {
		return nil, fmt.Errorf("构造 Codex 端点 %s path：%w", endpoint.ID, err)
	}
	for name := range input.PathValues {
		if !usedPathValues[name] {
			return nil, fmt.Errorf("Codex 端点 %s 不允许 path 参数：%s", endpoint.ID, name)
		}
	}

	queryParts := make([]string, 0, len(endpoint.Query))
	usedQueryValues := make(map[string]bool, len(endpoint.Query))
	for _, field := range endpoint.Query {
		if field.Name == "*" {
			return nil, fmt.Errorf("Codex 端点 %s 的通配 query 只能来自完整返回 URL", endpoint.ID)
		}
		value := field.Value
		if field.Source == officialCodexSourceConstant {
			if value == "" && field.Required {
				return nil, fmt.Errorf("Codex 端点 %s 的固定 query %s 为空", endpoint.ID, field.Name)
			}
			if _, exists := input.QueryValues[field.Name]; exists {
				return nil, fmt.Errorf("Codex 端点 %s 的固定 query %s 不允许覆盖", endpoint.ID, field.Name)
			}
		} else {
			value = input.QueryValues[field.Name]
			usedQueryValues[field.Name] = true
		}
		if value == "" {
			if field.Required {
				return nil, fmt.Errorf("Codex 端点 %s 缺少 query：%s", endpoint.ID, field.Name)
			}
			continue
		}
		queryParts = append(queryParts, url.QueryEscape(field.Name)+"="+url.QueryEscape(value))
	}
	for name := range input.QueryValues {
		if !usedQueryValues[name] {
			return nil, fmt.Errorf("Codex 端点 %s 不允许 query：%s", endpoint.ID, name)
		}
	}

	scheme := "https"
	if endpoint.Upgrade == "websocket" {
		scheme = "wss"
	}
	return &url.URL{
		Scheme:   scheme,
		Host:     endpoint.Host,
		Path:     expandedPath,
		RawPath:  rawPath,
		RawQuery: strings.Join(queryParts, "&"),
	}, nil
}

func officialCodexExpandEndpointPath(
	template string,
	values map[string]string,
) (string, string, map[string]bool, error) {
	used := make(map[string]bool)
	rawPath := template
	for {
		start := strings.IndexByte(rawPath, '{')
		if start < 0 {
			break
		}
		endOffset := strings.IndexByte(rawPath[start+1:], '}')
		if endOffset < 0 {
			return "", "", nil, errors.New("path 占位符未闭合")
		}
		end := start + 1 + endOffset
		name := rawPath[start+1 : end]
		if name == "" || strings.ContainsAny(name, "{}") {
			return "", "", nil, errors.New("path 占位符名称无效")
		}
		value := values[name]
		if value == "" {
			return "", "", nil, fmt.Errorf("缺少 path 参数：%s", name)
		}
		used[name] = true
		rawPath = rawPath[:start] + url.PathEscape(value) + rawPath[end+1:]
	}
	if strings.ContainsAny(rawPath, "{}") {
		return "", "", nil, errors.New("path 包含未解析占位符")
	}
	path, err := url.PathUnescape(rawPath)
	if err != nil {
		return "", "", nil, fmt.Errorf("解码 path：%w", err)
	}
	if rawPath == path {
		rawPath = ""
	}
	return path, rawPath, used, nil
}

func officialCodexValidateReturnedURL(
	endpoint officialCodexEndpointProfile,
	input officialCodexEndpointURLInput,
) (*url.URL, error) {
	if len(input.PathValues) != 0 || len(input.QueryValues) != 0 {
		return nil, fmt.Errorf("Codex 端点 %s 的返回 URL 不允许局部覆盖 path/query", endpoint.ID)
	}
	rawURL := strings.TrimSpace(input.ReturnedURL)
	if rawURL == "" {
		return nil, fmt.Errorf("Codex 端点 %s 缺少服务端返回 URL", endpoint.ID)
	}
	target, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("解析 Codex 端点 %s 返回 URL：%w", endpoint.ID, err)
	}
	if target.Scheme != "https" || target.Host == "" || target.User != nil || target.Fragment != "" {
		return nil, fmt.Errorf("Codex 端点 %s 的返回 URL 必须是无 userinfo/fragment 的 HTTPS 绝对 URL", endpoint.ID)
	}
	for _, field := range endpoint.Query {
		if field.Name == "*" && field.Required && strings.TrimSpace(target.RawQuery) == "" {
			return nil, fmt.Errorf("Codex 端点 %s 的服务端返回 URL 缺少必需 query", endpoint.ID)
		}
	}
	if !officialCodexHostMatches(endpoint.Host, target.Hostname()) {
		return nil, fmt.Errorf("Codex 端点 %s 的返回 host %q 不匹配画像 %q", endpoint.ID, target.Hostname(), endpoint.Host)
	}
	cloned := *target
	return &cloned, nil
}

func officialCodexHostMatches(pattern, host string) bool {
	pattern = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(pattern), "."))
	host = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	if !strings.HasPrefix(pattern, "*.") {
		return host == pattern
	}
	suffix := strings.TrimPrefix(pattern, "*")
	return strings.HasSuffix(host, suffix) && len(host) > len(suffix)
}

// officialCodexApplyHeaderContract 把 http.Header 收敛为端点画像的闭集，并
// 返回画像槽位顺序。常量由画像覆盖写入；动态必需值缺失、画像外字段、多值字段
// 都会失败。conditions 只表达运行态开关，不能改变槽位或线序。
//
// AlternateGroup 同时命中时选择画像顺序中靠后的项。compact 画像用这一规则
// 表达“已有 turn-state 时第三槽使用 turn-state，否则使用 beta features”。
func officialCodexApplyHeaderContract(
	version string,
	endpointID codexEndpointID,
	headers http.Header,
	conditions map[string]bool,
) ([]officialCodexHeaderField, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return applyOfficialCodexHeaderContract(profile, string(endpointID), headers, conditions)
}

func applyOfficialCodexHeaderContract(
	profile *officialCodexVersionProfile,
	endpointID string,
	headers http.Header,
	conditions map[string]bool,
) ([]officialCodexHeaderField, error) {
	if profile == nil || headers == nil {
		return nil, errors.New("Codex header 契约输入为空")
	}
	endpoint, err := profile.ResolveEndpoint(endpointID)
	if err != nil {
		return nil, err
	}
	orderedSlots := endpoint.OrderedHeaders()
	slotsByName := make(map[string]officialCodexHeaderSlot, len(orderedSlots))
	for _, slot := range orderedSlots {
		slotsByName[strings.ToLower(slot.Name)] = slot
	}

	values := make(map[string]string, len(headers))
	for rawName, rawValues := range headers {
		name := strings.ToLower(strings.TrimSpace(rawName))
		if _, allowed := slotsByName[name]; !allowed {
			return nil, fmt.Errorf("Codex 端点 %s 不允许 header：%s", endpoint.ID, rawName)
		}
		if _, duplicateCase := values[name]; duplicateCase {
			return nil, fmt.Errorf("Codex 端点 %s 的 header 大小写重复：%s", endpoint.ID, rawName)
		}
		if len(rawValues) != 1 {
			return nil, fmt.Errorf("Codex 端点 %s 的 header %s 必须恰好一个值", endpoint.ID, rawName)
		}
		values[name] = rawValues[0]
	}

	active := make([]bool, len(orderedSlots))
	for index, slot := range orderedSlots {
		name := strings.ToLower(slot.Name)
		_, present := values[name]
		switch slot.Condition {
		case officialCodexConditionAlways:
			active[index] = true
		case officialCodexConditionAuto:
			active[index] = present
		default:
			if enabled, declared := conditions[slot.Condition]; declared {
				active[index] = enabled
			} else {
				// 未显式给运行态开关时，只允许已有字段激活可选槽，避免凭空推断功能。
				active[index] = present
			}
		}
	}

	groupWinner := make(map[string]int)
	for index, slot := range orderedSlots {
		if !active[index] || slot.AlternateGroup == "" {
			continue
		}
		// OrderedHeaders 已按 Slot/Sequence 排序，后遇到者即画像优先级更高者。
		groupWinner[slot.AlternateGroup] = index
	}
	for index, slot := range orderedSlots {
		if slot.AlternateGroup == "" || !active[index] {
			continue
		}
		active[index] = groupWinner[slot.AlternateGroup] == index
	}

	fields := make([]officialCodexHeaderField, 0, len(orderedSlots))
	for index, slot := range orderedSlots {
		name := strings.ToLower(slot.Name)
		if !active[index] {
			delete(values, name)
			continue
		}
		if officialCodexTransportGeneratesHeader(name) {
			// Host、长度及 WS 握手协议头由 net/http/coder-websocket 在真正写线时
			// 生成。端点画像仍声明其槽位，但应用层 Header 不能预置重复值。
			delete(values, name)
			continue
		}
		value, present := values[name]
		if slot.Value != "" {
			value = slot.Value
			present = true
		}
		if !present || strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("Codex 端点 %s 缺少必需 header：%s", endpoint.ID, name)
		}
		values[name] = value
		fields = append(fields, officialCodexHeaderField{
			Slot: slot.Slot, Sequence: slot.Sequence,
			Name: name, WireName: slot.WireName, Value: value,
		})
	}

	// 校验完成后再原子式替换，错误路径不留下“删了一半”的 header。
	for name := range headers {
		headers.Del(name)
	}
	for _, field := range fields {
		if field.Name == "host" || field.Name == "content-length" {
			continue
		}
		headers.Set(field.Name, field.Value)
	}
	return fields, nil
}

func officialCodexTransportGeneratesHeader(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "host", "content-length", "connection", "upgrade",
		"sec-websocket-version", "sec-websocket-key", "sec-websocket-extensions":
		return true
	default:
		return false
	}
}

// officialCodexValidateAndOrderJSONBody 对 JSON 类端点执行顶层闭集校验，并按
// 画像字段顺序重新编码。它不会通过 map 再 Marshal，因此字段顺序不受 Go map
// 迭代影响；每个字段值只做 JSON 压缩，不改变数字或字符串语义。
func officialCodexValidateAndOrderJSONBody(
	version string,
	endpointID codexEndpointID,
	body []byte,
	conditions map[string]bool,
) ([]byte, error) {
	profile, err := resolveOfficialCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	return validateAndOrderOfficialCodexJSONBody(profile, string(endpointID), body, conditions)
}

func validateAndOrderOfficialCodexJSONBody(
	profile *officialCodexVersionProfile,
	endpointID string,
	body []byte,
	conditions map[string]bool,
) ([]byte, error) {
	if profile == nil {
		return nil, errors.New("Codex JSON 画像为空")
	}
	endpoint, err := profile.ResolveEndpoint(endpointID)
	if err != nil {
		return nil, err
	}
	switch endpoint.Body.Encoding {
	case "json", "websocket_json", "websocket_discriminated_events":
		// 这三类都以单个 JSON object 为线协议单位。
	default:
		return nil, fmt.Errorf("Codex 端点 %s 的 body 编码 %s 不是 JSON object", endpoint.ID, endpoint.Body.Encoding)
	}

	pairs, err := officialCodexDecodeJSONObject(body)
	if err != nil {
		return nil, fmt.Errorf("Codex 端点 %s 的 JSON body 无效：%w", endpoint.ID, err)
	}
	values := make(map[string]json.RawMessage, len(pairs))
	inputOrder := make([]string, 0, len(pairs))
	for _, pair := range pairs {
		if _, duplicate := values[pair.name]; duplicate {
			return nil, fmt.Errorf("Codex 端点 %s 的 JSON 字段重复：%s", endpoint.ID, pair.name)
		}
		values[pair.name] = pair.value
		inputOrder = append(inputOrder, pair.name)
	}

	contractFields := make(map[string]officialCodexBodyField, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		contractFields[field.Name] = field
	}
	if endpoint.Body.Closed {
		for name := range values {
			if _, allowed := contractFields[name]; !allowed {
				return nil, fmt.Errorf("Codex 端点 %s 不允许 JSON 字段：%s", endpoint.ID, name)
			}
		}
	}

	for _, field := range endpoint.Body.Fields {
		value, present := values[field.Name]
		conditionActive := true
		if field.Condition != "" {
			if enabled, declared := conditions[field.Condition]; declared {
				conditionActive = enabled
			} else {
				conditionActive = present
			}
		}
		if !conditionActive {
			if present {
				return nil, fmt.Errorf("Codex 端点 %s 的条件 JSON 字段未启用：%s", endpoint.ID, field.Name)
			}
			continue
		}
		if present && officialCodexShouldOmitJSONField(field.OmitWhen, value) {
			delete(values, field.Name)
			present = false
		}
		if field.Required && !present {
			return nil, fmt.Errorf("Codex 端点 %s 缺少必需 JSON 字段：%s", endpoint.ID, field.Name)
		}
		if field.Required && bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return nil, fmt.Errorf("Codex 端点 %s 的必需 JSON 字段为空：%s", endpoint.ID, field.Name)
		}
	}
	if err := officialCodexValidateJSONDiscriminator(endpoint, values); err != nil {
		return nil, err
	}

	orderedNames := make([]string, 0, len(values))
	added := make(map[string]bool, len(values))
	for _, field := range endpoint.Body.Fields {
		if _, present := values[field.Name]; present {
			orderedNames = append(orderedNames, field.Name)
			added[field.Name] = true
		}
	}
	// 开放契约（realtime sideband event）把画像已知字段放前面，其余字段保持
	// 原输入顺序；闭集契约走不到此分支。
	for _, name := range inputOrder {
		if !added[name] {
			orderedNames = append(orderedNames, name)
			added[name] = true
		}
	}

	var output bytes.Buffer
	_ = output.WriteByte('{')
	wroteField := false
	for _, name := range orderedNames {
		value, present := values[name]
		if !present {
			continue
		}
		if wroteField {
			_ = output.WriteByte(',')
		}
		encodedName, _ := json.Marshal(name)
		_, _ = output.Write(encodedName)
		_ = output.WriteByte(':')
		var compacted bytes.Buffer
		if err := json.Compact(&compacted, value); err != nil {
			return nil, fmt.Errorf("Codex 端点 %s 的 JSON 字段 %s 无效：%w", endpoint.ID, name, err)
		}
		_, _ = output.Write(compacted.Bytes())
		wroteField = true
	}
	_ = output.WriteByte('}')
	return output.Bytes(), nil
}

type officialCodexJSONPair struct {
	name  string
	value json.RawMessage
}

func officialCodexDecodeJSONObject(body []byte) ([]officialCodexJSONPair, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	opening, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	if delimiter, ok := opening.(json.Delim); !ok || delimiter != '{' {
		return nil, errors.New("顶层必须是 JSON object")
	}
	pairs := make([]officialCodexJSONPair, 0)
	for decoder.More() {
		nameToken, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		name, ok := nameToken.(string)
		if !ok {
			return nil, errors.New("JSON object 字段名不是字符串")
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		pairs = append(pairs, officialCodexJSONPair{name: name, value: value})
	}
	closing, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	if delimiter, ok := closing.(json.Delim); !ok || delimiter != '}' {
		return nil, errors.New("JSON object 未正确闭合")
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, errors.New("JSON object 后存在额外值")
		}
		return nil, err
	}
	return pairs, nil
}

func officialCodexShouldOmitJSONField(omitWhen string, value json.RawMessage) bool {
	trimmed := bytes.TrimSpace(value)
	switch omitWhen {
	case "":
		return false
	case "empty_string":
		return bytes.Equal(trimmed, []byte(`""`))
	case "none", "none_or_unreusable_prefix":
		return bytes.Equal(trimmed, []byte("null"))
	default:
		// 未知省略语义应由画像校验负责；执行器保守地保留值，绝不擅自丢字段。
		return false
	}
}

func officialCodexValidateJSONDiscriminator(
	endpoint officialCodexEndpointProfile,
	values map[string]json.RawMessage,
) error {
	discriminator := endpoint.Body.Discriminator
	if discriminator == "" {
		return nil
	}
	name, expected, hasExpected := strings.Cut(discriminator, "=")
	rawValue, present := values[name]
	if !present {
		return fmt.Errorf("Codex 端点 %s 缺少 JSON discriminator：%s", endpoint.ID, name)
	}
	if !hasExpected {
		return nil
	}
	var actual string
	if err := json.Unmarshal(rawValue, &actual); err != nil || actual != expected {
		return fmt.Errorf("Codex 端点 %s 的 JSON discriminator %s 必须为 %q", endpoint.ID, name, expected)
	}
	return nil
}
