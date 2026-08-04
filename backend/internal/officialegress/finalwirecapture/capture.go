// Package finalwirecapture 在正式 adapter 与本地确定性 terminal 的边界记录
// 脱敏 final-wire。它只用于变更集 3 证据测试，不发送外部流量。
package finalwirecapture

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/klauspost/compress/zstd"
)

type Header struct {
	Name        string `json:"name"`
	WireName    string `json:"wire_name"`
	Present     bool   `json:"present,omitempty"`
	Source      string `json:"source,omitempty"`
	ValueKind   string `json:"value_kind"`
	SafeValue   string `json:"safe_value,omitempty"`
	ValueSHA256 string `json:"value_sha256,omitempty"`
}

type Body struct {
	Encoding           profilecontract.BodyEncoding `json:"encoding"`
	OrderedFields      []string                     `json:"ordered_fields"`
	TypeShape          []string                     `json:"type_shape"`
	SyntheticSHA256    string                       `json:"synthetic_sha256"`
	Compression        string                       `json:"compression"`
	AppliedCompression string                       `json:"applied_compression,omitempty"`
	CompressionLevel   int                          `json:"compression_level"`
	FinalWireBytes     int64                        `json:"final_wire_bytes,omitempty"`
	CredentialBearing  bool                         `json:"credential_bearing"`
}

type WebSocketEvent struct {
	EventType       string   `json:"event_type"`
	FrameType       string   `json:"frame_type"`
	Policy          string   `json:"policy"`
	FrameOrdinal    uint64   `json:"frame_ordinal,omitempty"`
	OrderedFields   []string `json:"ordered_fields"`
	TypeShapeSHA256 string   `json:"type_shape_sha256"`
	BodySHA256      string   `json:"body_sha256,omitempty"`
}

type WebSocket struct {
	HandshakeGeneratedHeaders []string         `json:"handshake_generated_headers"`
	CompressionOffer          string           `json:"compression_offer"`
	ContextTakeover           bool             `json:"context_takeover"`
	EventMatrix               []WebSocketEvent `json:"event_matrix"`
}

type DynamicTarget struct {
	SyntheticReturnedURL string `json:"synthetic_returned_url"`
	ValidationResult     string `json:"validation_result"`
}

type Capture struct {
	SinkID                    string                               `json:"sink_id"`
	Anchor                    bool                                 `json:"anchor"`
	ReleaseMode               officialegress.ReleaseMode           `json:"release_mode"`
	Method                    string                               `json:"method"`
	HostTemplate              string                               `json:"host_template"`
	FinalHost                 string                               `json:"final_host"`
	PathTemplate              string                               `json:"path_template"`
	FinalPath                 string                               `json:"final_path"`
	Protocol                  officialegress.WireProtocol          `json:"protocol"`
	Purpose                   officialegress.Purpose               `json:"purpose"`
	EndpointID                string                               `json:"endpoint_id"`
	AuthorityID               officialegress.ExecutorID            `json:"authority_id"`
	HasFinalizationToken      bool                                 `json:"has_finalization_token"`
	TerminalGuardAllow        bool                                 `json:"terminal_guard_allow"`
	TerminalGuardReason       officialegress.GuardReason           `json:"terminal_guard_reason,omitempty"`
	ProfileValidationResult   string                               `json:"profile_validation_result"`
	AttemptOrdinal            uint32                               `json:"attempt_ordinal"`
	AttemptReason             string                               `json:"attempt_reason"`
	ReleaseDigest             string                               `json:"release_digest"`
	ProfileDigest             string                               `json:"profile_digest"`
	BundleDigest              string                               `json:"bundle_digest"`
	Backend                   officialegress.BackendKind           `json:"backend"`
	AdapterID                 officialegress.AdapterID             `json:"adapter_id"`
	TransportID               string                               `json:"transport_id"`
	ConnectionIdentityDigest  string                               `json:"connection_identity_digest"`
	ConnectionPoolDigest      string                               `json:"connection_pool_digest"`
	TLSProfileDigest          string                               `json:"tls_profile_digest"`
	Normalization             officialegress.WireNormalizationPlan `json:"normalization"`
	OrderedHeaders            []Header                             `json:"ordered_headers"`
	Body                      Body                                 `json:"body"`
	WebSocket                 *WebSocket                           `json:"websocket,omitempty"`
	DynamicTarget             *DynamicTarget                       `json:"dynamic_target,omitempty"`
	SingleUseRawStream        bool                                 `json:"single_use_raw_stream"`
	SingleUseConsumptionCount int                                  `json:"single_use_consumption_count"`
}

type Terminal struct {
	guard  *officialegress.Guard
	bundle officialegress.ReleaseBundle
	base   Capture

	mu       sync.Mutex
	captures []Capture
}

func NewTerminal(
	guard *officialegress.Guard,
	base Capture,
) (*Terminal, error) {
	if guard == nil || strings.TrimSpace(base.EndpointID) == "" {
		return nil, errors.New("final-wire terminal 输入不完整")
	}
	return &Terminal{guard: guard, base: base}, nil
}

func (t *Terminal) Captures() []Capture {
	t.mu.Lock()
	defer t.mu.Unlock()
	result := make([]Capture, len(t.captures))
	for index, capture := range t.captures {
		raw, _ := json.Marshal(capture)
		_ = json.Unmarshal(raw, &result[index])
	}
	return result
}

func (t *Terminal) SendHTTPUpstream(
	_ context.Context,
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	return t.captureHTTP(prepared)
}

func (t *Terminal) SendReqProfile(
	_ context.Context,
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	return t.captureHTTP(prepared)
}

func (t *Terminal) captureHTTP(
	prepared officialegress.PreparedRequest,
) (*http.Response, error) {
	bundle := prepared.Bundle()
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	transport := prepared.Transport()
	applyNormalization(request, transport)
	decision := t.guard.Evaluate(request, transport.Backend, transport.Protocol)
	if !decision.Allow {
		return nil, fmt.Errorf("final-wire terminal Guard 拒绝：%s", decision.RejectionReason)
	}
	if err := t.appendCapture(request, transport, bundle, decision); err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: http.StatusOK, Status: "200 OK", Header: make(http.Header),
		Body: io.NopCloser(bytes.NewReader([]byte(`{}`))), Request: request,
	}, nil
}

func (t *Terminal) AcquireWebSocket(
	_ context.Context,
	prepared officialegress.PreparedRequest,
) (officialegress.WebSocketConnection, error) {
	bundle := prepared.Bundle()
	request, err := prepared.TakeHTTPRequest()
	if err != nil {
		return nil, err
	}
	transport := prepared.Transport()
	applyNormalization(request, transport)
	decision := t.guard.Evaluate(request, transport.Backend, transport.Protocol)
	if !decision.Allow {
		return nil, fmt.Errorf("final-wire WS terminal Guard 拒绝：%s", decision.RejectionReason)
	}
	if err := t.appendCapture(request, transport, bundle, decision); err != nil {
		return nil, err
	}
	return &webSocketConnection{terminal: t}, nil
}

func (t *Terminal) appendCapture(
	request *http.Request,
	transport officialegress.TransportSpec,
	bundle officialegress.ReleaseBundle,
	decision officialegress.GuardDecision,
) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if bundle.BundleDigest() == "" {
		return errors.New("final-wire terminal 缺少 ReleaseBundle")
	}
	t.bundle = bundle
	capture := t.base
	identity, ok := officialegress.AttemptIdentityFromContext(request.Context())
	if !ok || !identity.HasFinalizationToken {
		return errors.New("final-wire terminal 缺少 FinalizationToken")
	}
	capture.AuthorityID = identity.ExecutorID
	capture.HasFinalizationToken = true
	capture.TerminalGuardAllow = decision.Allow
	capture.TerminalGuardReason = decision.RejectionReason
	capture.AttemptOrdinal = identity.AttemptOrdinal
	capture.AttemptReason = identity.AttemptReason
	capture.ReleaseDigest = identity.ReleaseDigest
	capture.ProfileDigest = identity.ProfileDigest
	capture.BundleDigest = identity.BundleDigest
	capture.Backend = transport.Backend
	capture.AdapterID = transport.Adapter
	capture.TransportID = transport.ID
	capture.ConnectionPoolDigest = transport.ConnectionPoolDigest
	capture.Normalization = transport.Normalization
	capture.FinalHost = request.URL.Hostname()
	capture.FinalPath = request.URL.EscapedPath()
	capture.TLSProfileDigest = Digest(transport.TLS)
	capture.ConnectionIdentityDigest = connectionDigest(capture, request.URL)
	capture.OrderedHeaders = finalHeaders(request, transport, bundle)
	body, err := readBody(request)
	if err != nil {
		return err
	}
	if capture.SingleUseRawStream {
		capture.SingleUseConsumptionCount = 1
	}
	capture.Body, err = bodyEvidence(bundle, capture.EndpointID, request, body)
	if err != nil {
		return err
	}
	if capture.Protocol == officialegress.WireProtocolWebSocket {
		capture.WebSocket = webSocketEvidence(transport, bundle)
	}
	capture.ProfileValidationResult = "passed"
	t.captures = append(t.captures, capture)
	return nil
}

type webSocketConnection struct {
	terminal *Terminal
	ordinal  uint64
}

func (c *webSocketConnection) ReadMessage(context.Context) ([]byte, error) {
	return nil, io.EOF
}

func (c *webSocketConnection) WriteMessage(_ context.Context, payload []byte) error {
	return c.captureFrame(officialegress.WebSocketFrameText, payload)
}

func (c *webSocketConnection) WriteWebSocketFrame(
	_ context.Context,
	frameType officialegress.WebSocketFrameType,
	payload []byte,
) error {
	return c.captureFrame(frameType, payload)
}

func (c *webSocketConnection) captureFrame(
	frameType officialegress.WebSocketFrameType,
	payload []byte,
) error {
	if c == nil || c.terminal == nil {
		return errors.New("final-wire WS frame 缺少 terminal")
	}
	c.terminal.mu.Lock()
	defer c.terminal.mu.Unlock()
	if len(c.terminal.captures) == 0 {
		return errors.New("final-wire WS frame 缺少握手 capture")
	}
	c.ordinal++
	eventType := "binary.transparent"
	var orderedFields []string
	var typeShape []string
	if frameType == officialegress.WebSocketFrameText {
		pairs, err := decodeOrderedObject(payload)
		if err != nil {
			return err
		}
		for _, pair := range pairs {
			orderedFields = append(orderedFields, pair.name)
			typeShape = append(typeShape, pair.name+":"+jsonType(pair.value))
			if pair.name == "type" {
				_ = json.Unmarshal(pair.value, &eventType)
			}
		}
	}
	policy := "profile_finalized"
	if c.terminal.base.EndpointID == "realtime_sideband" {
		policy = "profile_declared_transparent_text"
		if frameType == officialegress.WebSocketFrameBinary {
			policy = "profile_declared_transparent_binary"
		}
	}
	event := WebSocketEvent{
		EventType: eventType, FrameType: string(frameType), Policy: policy,
		FrameOrdinal: c.ordinal, OrderedFields: orderedFields,
		TypeShapeSHA256: Digest(struct {
			Fields []string
			Shape  []string
		}{orderedFields, typeShape}),
		BodySHA256: SHA256(payload),
	}
	index := len(c.terminal.captures) - 1
	capture := &c.terminal.captures[index]
	capture.WebSocket.EventMatrix = append(capture.WebSocket.EventMatrix, event)
	return nil
}

func (c *webSocketConnection) Close() error { return nil }

func applyNormalization(request *http.Request, transport officialegress.TransportSpec) {
	if transport.Normalization.HeaderMode == officialegress.HeaderNormalizationLowercase {
		headers := make(http.Header, len(request.Header)+1)
		for name, values := range request.Header {
			lower := strings.ToLower(name)
			current, exists := headers[lower]
			if !exists || (allEmpty(current) && !allEmpty(values)) {
				headers[lower] = append([]string(nil), values...)
			}
		}
		if transport.Normalization.SuppressDefaultUserAgent {
			headers["User-Agent"] = []string{""}
		}
		request.Header = headers
	}
	if offer := strings.TrimSpace(transport.Normalization.WebSocketCompressionOffer); offer != "" {
		request.Header.Set("Sec-WebSocket-Extensions", offer)
	}
	if transport.Protocol == officialegress.WireProtocolWebSocket {
		request.Host = request.URL.Host
		request.Header.Set("Connection", "Upgrade")
		request.Header.Set("Upgrade", "websocket")
		request.Header.Set("Sec-WebSocket-Version", "13")
		request.Header.Set("Sec-WebSocket-Key", "c3ludGhldGljLXdpcmUta2V5")
	}
}

func finalHeaders(
	request *http.Request,
	transport officialegress.TransportSpec,
	bundle officialegress.ReleaseBundle,
) []Header {
	order := headerOrder(request, transport)
	endpointID := ""
	if metadata, ok := officialegress.AttemptIdentityFromContext(request.Context()); ok {
		endpointID = metadata.EndpointID
	}
	endpoint, _ := endpointByID(bundle.Release().Profile(), endpointID)
	sources := make(map[string]string)
	wireNames := make(map[string]string)
	for _, slot := range endpoint.Headers {
		name := slot.WireName
		if name == "" {
			name = slot.Name
		}
		sources[strings.ToLower(name)] = string(slot.Source)
		wireNames[strings.ToLower(name)] = name
	}
	result := make([]Header, 0, len(order))
	for _, orderedName := range order {
		name := strings.ToLower(orderedName)
		wireName := orderedName
		if transport.Protocol == officialegress.WireProtocolWebSocket {
			switch name {
			case "host":
				wireName = "Host"
			case "connection":
				wireName = "Connection"
			case "upgrade":
				wireName = "Upgrade"
			case "sec-websocket-version":
				wireName = "Sec-WebSocket-Version"
			case "sec-websocket-key":
				wireName = "Sec-WebSocket-Key"
			default:
				if frozen := wireNames[name]; frozen != "" {
					wireName = frozen
				}
			}
		}
		value, present := exactHeader(request.Header, name)
		entry := Header{Name: name, WireName: wireName, Present: present, Source: sources[name]}
		if name == "host" {
			entry.Present, entry.Source, value = true, "transport_generated", request.URL.Host
		}
		if name == "content-length" && request.ContentLength > 0 {
			entry.Present, entry.Source = true, "request_body"
			value = strconv.FormatInt(request.ContentLength, 10)
		}
		switch name {
		case "authorization", "x-oai-attestation", "cookie":
			entry.ValueKind = "attempt_authentication"
		case "chatgpt-account-id", "session-id", "x-session-id", "conversation-id",
			"thread-id", "x-client-request-id", "x-codex-window-id",
			"x-codex-turn-metadata", "x-ms-client-request-id":
			entry.ValueKind = "synthetic_dynamic_identity"
		case "sec-websocket-key":
			entry.ValueKind, entry.Source = "transport_generated_synthetic_placeholder", "transport_generated"
		default:
			entry.ValueKind = "static_or_synthetic_safe"
			if entry.Present {
				entry.SafeValue, entry.ValueSHA256 = value, SHA256([]byte(value))
			}
		}
		result = append(result, entry)
	}
	return result
}

func bodyEvidence(
	bundle officialegress.ReleaseBundle,
	endpointID string,
	request *http.Request,
	finalWireBody []byte,
) (Body, error) {
	endpoint, ok := endpointByID(bundle.Release().Profile(), endpointID)
	if !ok {
		return Body{}, errors.New("final-wire Body 缺少 Endpoint Profile")
	}
	evidence := Body{
		Encoding: endpoint.Body.Encoding, SyntheticSHA256: SHA256(finalWireBody),
		Compression:      string(endpoint.Compression),
		CompressionLevel: bundle.Release().Profile().Features().RequestCompressionLevel,
		FinalWireBytes:   int64(len(finalWireBody)), CredentialBearing: endpointID == "oauth_refresh",
	}
	semantic := finalWireBody
	if value, present := exactHeader(request.Header, "content-encoding"); present &&
		strings.EqualFold(strings.TrimSpace(value), "zstd") {
		evidence.AppliedCompression = "zstd"
		decoder, err := zstd.NewReader(nil)
		if err != nil {
			return Body{}, err
		}
		semantic, err = decoder.DecodeAll(finalWireBody, nil)
		decoder.Close()
		if err != nil {
			return Body{}, err
		}
	} else {
		evidence.AppliedCompression = "none"
	}
	if endpoint.Body.Encoding == profilecontract.BodyJson ||
		endpoint.Body.Encoding == profilecontract.BodyWebsocketJson ||
		endpoint.Body.Encoding == profilecontract.BodyWebsocketDiscriminatedEvents {
		pairs, err := decodeOrderedObject(semantic)
		if err == nil {
			for _, pair := range pairs {
				evidence.OrderedFields = append(evidence.OrderedFields, pair.name)
				evidence.TypeShape = append(evidence.TypeShape, pair.name+":"+jsonType(pair.value))
			}
		}
	}
	return evidence, nil
}

func webSocketEvidence(
	transport officialegress.TransportSpec,
	bundle officialegress.ReleaseBundle,
) *WebSocket {
	evidence := &WebSocket{
		HandshakeGeneratedHeaders: []string{
			"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key",
		},
		CompressionOffer: transport.Normalization.WebSocketCompressionOffer,
	}
	for _, candidate := range bundle.Release().Profile().Transports() {
		if candidate.ID == transport.ID && candidate.WebSocket != nil {
			evidence.ContextTakeover = candidate.WebSocket.ContextTakeover
		}
	}
	return evidence
}

func endpointByID(
	profile profilecontract.ProfileSpec,
	endpointID string,
) (profilecontract.EndpointProfile, bool) {
	for _, endpoint := range profile.Endpoints() {
		if endpoint.ID == endpointID {
			return endpoint, true
		}
	}
	return profilecontract.EndpointProfile{}, false
}

func exactHeader(headers http.Header, name string) (string, bool) {
	if values, exists := headers[name]; exists && len(values) != 0 {
		return values[0], true
	}
	var candidates []string
	for candidate := range headers {
		if strings.EqualFold(candidate, name) {
			candidates = append(candidates, candidate)
		}
	}
	sort.Strings(candidates)
	for _, candidate := range candidates {
		values := headers[candidate]
		if len(values) != 0 && values[0] != "" {
			return values[0], true
		}
	}
	for _, candidate := range candidates {
		if values := headers[candidate]; len(values) != 0 {
			return values[0], true
		}
	}
	return "", false
}

func headerOrder(request *http.Request, transport officialegress.TransportSpec) []string {
	for _, rule := range transport.TLS.H1HeaderOrders {
		if strings.EqualFold(rule.Method, request.Method) &&
			(rule.Path == request.URL.EscapedPath() || strings.Contains(rule.Path, "{")) {
			order := append([]string(nil), rule.Order...)
			if rule.Mode == "swap_remove" {
				order = append(order, rule.AppendHeaders...)
			}
			return order
		}
	}
	names := make([]string, 0, len(request.Header))
	for name := range request.Header {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func readBody(request *http.Request) ([]byte, error) {
	if request == nil || request.Body == nil || request.Body == http.NoBody {
		return nil, nil
	}
	body, err := io.ReadAll(request.Body)
	if err != nil {
		return nil, err
	}
	request.Body = io.NopCloser(bytes.NewReader(body))
	return body, nil
}

type jsonPair struct {
	name  string
	value json.RawMessage
}

func decodeOrderedObject(raw []byte) ([]jsonPair, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, errors.New("不是 JSON object")
	}
	var pairs []jsonPair
	for decoder.More() {
		nameToken, tokenErr := decoder.Token()
		if tokenErr != nil {
			return nil, tokenErr
		}
		name, ok := nameToken.(string)
		if !ok {
			return nil, errors.New("JSON 字段名非法")
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		pairs = append(pairs, jsonPair{name: name, value: value})
	}
	return pairs, nil
}

func jsonType(raw json.RawMessage) string {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return "empty"
	}
	switch trimmed[0] {
	case '{':
		return "object"
	case '[':
		return "array"
	case '"':
		return "string"
	case 't', 'f':
		return "boolean"
	case 'n':
		return "null"
	default:
		return "number"
	}
}

func allEmpty(values []string) bool {
	for _, value := range values {
		if value != "" {
			return false
		}
	}
	return true
}

func connectionDigest(capture Capture, target *url.URL) string {
	return Digest(struct {
		ReleaseDigest string
		BundleDigest  string
		TransportID   string
		Scheme        string
		Host          string
	}{capture.ReleaseDigest, capture.BundleDigest, capture.TransportID, target.Scheme, target.Host})
}

func Digest(value any) string {
	raw, _ := json.Marshal(value)
	return SHA256(raw)
}

func SHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
