package officialegress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// WebSocketFrameType 是 ProfileSpec 可授权的出站帧类别。
type WebSocketFrameType string

const (
	WebSocketFrameText   WebSocketFrameType = "text"
	WebSocketFrameBinary WebSocketFrameType = "binary"
)

// WebSocketFramePlan 是尚未定型的单帧业务语义。IdentityFacts 只能承载
// 当前 session/turn 的结构化事实，不能携带认证材料。
type WebSocketFramePlan struct {
	Type           WebSocketFrameType
	EventType      string
	Payload        []byte
	Body           RequestBody
	IdentityFacts  CodexIdentityFacts
	BodyConditions BodyRuntimeConditions
}

type preparedWebSocketFrameState struct {
	mu        sync.Mutex
	session   *ExecutorWebSocketSession
	payload   []byte
	frameType WebSocketFrameType
	consumed  bool
}

// PreparedWebSocketFrame 是 ExecutorWebSocketSession 唯一签发的一次性帧能力。
// 业务层无法构造有效 state，也不能通过值拷贝绕过单次消费。
type PreparedWebSocketFrame struct {
	state               *preparedWebSocketFrameState
	bundleDigest        string
	endpointID          string
	invocationID        string
	ordinal             uint64
	eventType           string
	bodyDigest          string
	identityFactsDigest string
}

func (f PreparedWebSocketFrame) Ordinal() uint64      { return f.ordinal }
func (f PreparedWebSocketFrame) EventType() string    { return f.eventType }
func (f PreparedWebSocketFrame) BodyDigest() string   { return f.bodyDigest }
func (f PreparedWebSocketFrame) EndpointID() string   { return f.endpointID }
func (f PreparedWebSocketFrame) InvocationID() string { return f.invocationID }

// ExecutorWebSocketSession 将握手后的连接继续约束在同一个 Bundle、Endpoint 与
// Invocation 下。正式调用方只能准备并消费帧，拿不到底层任意写接口。
type ExecutorWebSocketSession struct {
	connection WebSocketConnection
	bundle     ReleaseBundle
	endpoint   ResolvedEndpointPlan
	identity   CodexIdentityFacts
	token      FinalizationToken

	mu          sync.Mutex
	nextOrdinal uint64
	closed      bool
}

func newExecutorWebSocketSession(
	connection WebSocketConnection,
	request PreparedRequest,
) (*ExecutorWebSocketSession, error) {
	state, codexState := request.dialect.(codexPreparedState)
	if connection == nil || request.endpoint.EndpointID() == "" ||
		request.token.payload.Protocol != WireProtocolWebSocket ||
		request.token.payload.InvocationID == "" ||
		request.token.payload.Persona != PersonaCodexCLI || !codexState {
		return nil, errors.New("Executor WebSocket session 输入不完整")
	}
	return &ExecutorWebSocketSession{
		connection: connection, bundle: state.bundle, endpoint: request.endpoint,
		identity: state.identity, token: request.token,
	}, nil
}

func (s *ExecutorWebSocketSession) BundleDigest() string {
	if s == nil {
		return ""
	}
	return s.bundle.BundleDigest()
}

func (s *ExecutorWebSocketSession) EndpointID() string {
	if s == nil {
		return ""
	}
	return s.endpoint.EndpointID()
}

func (s *ExecutorWebSocketSession) InvocationID() string {
	if s == nil {
		return ""
	}
	return s.token.payload.InvocationID
}

func (s *ExecutorWebSocketSession) IdentityFacts() CodexIdentityFacts {
	if s == nil {
		return CodexIdentityFacts{}
	}
	return s.identity
}

func (s *ExecutorWebSocketSession) PrepareFrame(
	plan WebSocketFramePlan,
) (PreparedWebSocketFrame, error) {
	if s == nil || s.connection == nil {
		return PreparedWebSocketFrame{}, errors.New("Executor WebSocket session 未初始化")
	}
	if plan.Type == "" {
		plan.Type = WebSocketFrameText
	}
	if plan.Type != WebSocketFrameText && plan.Type != WebSocketFrameBinary {
		return PreparedWebSocketFrame{}, errors.New("WebSocket frame type 非法")
	}
	facts := plan.IdentityFacts
	if facts.Digest() == (CodexIdentityFacts{}).Digest() {
		facts = s.identity
	}
	if err := facts.Validate(); err != nil {
		return PreparedWebSocketFrame{}, err
	}
	if err := validateWebSocketFrameIdentity(s.identity, facts); err != nil {
		return PreparedWebSocketFrame{}, err
	}
	var payload []byte
	eventType := strings.TrimSpace(plan.EventType)
	if plan.Type == WebSocketFrameBinary {
		payload = append([]byte(nil), plan.Payload...)
		if !endpointAllowsTransparentBinaryFrame(s.endpoint) {
			return PreparedWebSocketFrame{}, errors.New("Endpoint Profile 未声明二进制透明帧策略")
		}
		if eventType != "binary.transparent" || len(payload) == 0 {
			return PreparedWebSocketFrame{}, errors.New("二进制透明帧声明非法")
		}
	} else {
		body := plan.Body.clone()
		if body.state == nil {
			body = NewReplayableRequestBody(plan.Payload)
		}
		var err error
		payload, eventType, err = compileWebSocketFrame(
			s.endpoint, body, plan.EventType, facts, plan.BodyConditions,
			s.bundle.release.ExecutableProfile().Features(),
		)
		if err != nil {
			return PreparedWebSocketFrame{}, err
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return PreparedWebSocketFrame{}, errors.New("Executor WebSocket session 已关闭")
	}
	s.nextOrdinal++
	bodySum := sha256.Sum256(payload)
	state := &preparedWebSocketFrameState{
		session: s, payload: payload, frameType: plan.Type,
	}
	return PreparedWebSocketFrame{
		state: state, bundleDigest: s.bundle.BundleDigest(),
		endpointID: s.endpoint.EndpointID(), invocationID: s.token.payload.InvocationID,
		ordinal: s.nextOrdinal, eventType: eventType,
		bodyDigest: hex.EncodeToString(bodySum[:]), identityFactsDigest: facts.Digest(),
	}, nil
}

// endpointAllowsTransparentBinaryFrame 把 ProfileSpec 中
// websocket_discriminated_events + open body 解释为显式透明事件通道；严格
// responses_ws 的 websocket_json + closed body 永远不具备该能力。
func endpointAllowsTransparentBinaryFrame(endpoint ResolvedEndpointPlan) bool {
	body := endpoint.template.endpoint.Body
	return body.Encoding == profilecontract.BodyWebsocketDiscriminatedEvents &&
		!body.Closed && body.Discriminator == "type"
}

func validateWebSocketFrameIdentity(base, current CodexIdentityFacts) error {
	stable := []struct {
		name string
		base CodexIdentityValue
		now  CodexIdentityValue
	}{
		{"account", base.AccountIdentityProjection, current.AccountIdentityProjection},
		{"chatgpt-account", base.ChatGPTAccountID, current.ChatGPTAccountID},
		{"workspace", base.WorkspaceID, current.WorkspaceID},
		{"process-surface", base.ProcessSurface, current.ProcessSurface},
		{"process-phase", base.ProcessPhase, current.ProcessPhase},
		{"terminal-token", base.TerminalToken, current.TerminalToken},
		{"installation", base.InstallationID, current.InstallationID},
		{"session", base.SessionID, current.SessionID},
		{"conversation", base.ConversationID, current.ConversationID},
		{"thread", base.ThreadID, current.ThreadID},
		{"window", base.WindowID, current.WindowID},
	}
	for _, field := range stable {
		if field.base != field.now {
			return fmt.Errorf("WebSocket frame 更换了 invocation 冻结的 %s 身份", field.name)
		}
	}
	if base.UserAgentSuffixEnabled != current.UserAgentSuffixEnabled {
		return errors.New("WebSocket frame 更换了 invocation 冻结的 user-agent suffix 身份")
	}
	return nil
}

func compileWebSocketFrame(
	endpoint ResolvedEndpointPlan,
	body RequestBody,
	declaredEventType string,
	facts CodexIdentityFacts,
	bodyConditions BodyRuntimeConditions,
	features profilecontract.FeatureDefaults,
) ([]byte, string, error) {
	profile := endpoint.template.endpoint
	if profile.Body.Encoding != profilecontract.BodyWebsocketJson &&
		profile.Body.Encoding != profilecontract.BodyWebsocketDiscriminatedEvents {
		return nil, "", errors.New("Endpoint Profile 未声明受控 WebSocket 帧")
	}
	payload, replayable := body.replayableView()
	if !replayable {
		return nil, "", errors.New("WebSocket 帧必须是 replayable Body")
	}
	if len(bytes.TrimSpace(payload)) == 0 {
		return nil, "", errors.New("WebSocket 帧 Body 为空")
	}
	document := body.jsonDocument()
	if document == nil {
		var err error
		document, err = newOrderedJSONDocument(payload)
		if err != nil {
			return nil, "", fmt.Errorf("解析 WebSocket 帧事件：%w", err)
		}
	}
	typeRaw, present := document.value("type")
	if !present {
		return nil, "", errors.New("WebSocket 帧缺少 event type")
	}
	var eventType string
	if err := json.Unmarshal(typeRaw, &eventType); err != nil {
		return nil, "", errors.New("WebSocket 帧 event type 非法")
	}
	eventType = strings.TrimSpace(eventType)
	declaredEventType = strings.TrimSpace(declaredEventType)
	if eventType == "" || declaredEventType == "" || eventType != declaredEventType {
		return nil, "", errors.New("WebSocket 帧 event type 与结构化声明不一致")
	}
	if profile.Body.Discriminator != "" && strings.Contains(profile.Body.Discriminator, "=") {
		parts := strings.SplitN(profile.Body.Discriminator, "=", 2)
		if parts[0] != "type" || eventType != parts[1] {
			return nil, "", errors.New("WebSocket 帧不匹配 Endpoint discriminator")
		}
	}
	if err := injectCompilerOwnedBodyFields(
		profile, document, AttemptAuthenticationInput{}, facts,
	); err != nil {
		return nil, "", err
	}
	compiled, err := orderJSONDocumentWithPolicy(
		document, profile.Body, features, facts.Conditions,
		bodyConditions, AttemptAuthenticationInput{},
	)
	if err != nil {
		return nil, "", err
	}
	return compiled, eventType, nil
}

func (s *ExecutorWebSocketSession) WritePreparedFrame(
	ctx context.Context,
	frame PreparedWebSocketFrame,
) error {
	if s == nil || frame.state == nil || frame.state.session != s ||
		frame.bundleDigest != s.bundle.BundleDigest() || frame.endpointID != s.endpoint.EndpointID() ||
		frame.invocationID != s.token.payload.InvocationID {
		return errors.New("PreparedWebSocketFrame 不属于当前 session")
	}
	frame.state.mu.Lock()
	defer frame.state.mu.Unlock()
	if frame.state.consumed {
		return errors.New("PreparedWebSocketFrame 已消费，禁止重放")
	}
	frame.state.consumed = true
	payload := frame.state.payload
	frame.state.payload = nil
	if writer, ok := s.connection.(WebSocketFrameWriter); ok {
		return writer.WriteWebSocketFrame(ctx, frame.state.frameType, payload)
	}
	if frame.state.frameType == WebSocketFrameBinary {
		return errors.New("受信 transport port 不支持二进制帧")
	}
	return s.connection.WriteMessage(ctx, payload)
}

func (s *ExecutorWebSocketSession) ReadMessage(ctx context.Context) ([]byte, error) {
	if s == nil || s.connection == nil {
		return nil, errors.New("Executor WebSocket session 未初始化")
	}
	return s.connection.ReadMessage(ctx)
}

func (s *ExecutorWebSocketSession) ReadFrame(
	ctx context.Context,
) (WebSocketFrameType, []byte, error) {
	if s == nil || s.connection == nil {
		return WebSocketFrameText, nil, errors.New("Executor WebSocket session 未初始化")
	}
	if reader, ok := s.connection.(WebSocketFrameReader); ok {
		return reader.ReadWebSocketFrame(ctx)
	}
	payload, err := s.connection.ReadMessage(ctx)
	return WebSocketFrameText, payload, err
}

func (s *ExecutorWebSocketSession) connectionState() WebSocketConnectionState {
	if s == nil {
		return nil
	}
	state, _ := s.connection.(WebSocketConnectionState)
	return state
}

func (s *ExecutorWebSocketSession) ConnectionID() string {
	if state := s.connectionState(); state != nil {
		return state.ConnectionID()
	}
	return ""
}

func (s *ExecutorWebSocketSession) HandshakeStatus() int {
	if state := s.connectionState(); state != nil {
		return state.HandshakeStatus()
	}
	return 0
}

func (s *ExecutorWebSocketSession) QueueWaitDuration() time.Duration {
	if state := s.connectionState(); state != nil {
		return state.QueueWaitDuration()
	}
	return 0
}

func (s *ExecutorWebSocketSession) ConnectionPickDuration() time.Duration {
	if state := s.connectionState(); state != nil {
		return state.ConnectionPickDuration()
	}
	return 0
}

func (s *ExecutorWebSocketSession) Reused() bool {
	if state := s.connectionState(); state != nil {
		return state.Reused()
	}
	return false
}

func (s *ExecutorWebSocketSession) HandshakeHeader(name string) string {
	if state := s.connectionState(); state != nil {
		return state.HandshakeHeader(name)
	}
	return ""
}

func (s *ExecutorWebSocketSession) HandshakeHeaders() http.Header {
	if state := s.connectionState(); state != nil {
		return state.HandshakeHeaders().Clone()
	}
	return nil
}

func (s *ExecutorWebSocketSession) IsPrewarmed() bool {
	if state := s.connectionState(); state != nil {
		return state.IsPrewarmed()
	}
	return false
}

func (s *ExecutorWebSocketSession) MarkPrewarmed() {
	if state := s.connectionState(); state != nil {
		state.MarkPrewarmed()
	}
}

func (s *ExecutorWebSocketSession) MarkUnusable() {
	if state := s.connectionState(); state != nil {
		state.MarkUnusable()
	}
}

func (s *ExecutorWebSocketSession) Ping(ctx context.Context) error {
	if state := s.connectionState(); state != nil {
		return state.Ping(ctx)
	}
	return errors.New("Executor WebSocket session 不支持 Ping")
}

func (s *ExecutorWebSocketSession) SupportsIdlePingWithoutReader() bool {
	if state := s.connectionState(); state != nil {
		return state.SupportsIdlePingWithoutReader()
	}
	return false
}

func (s *ExecutorWebSocketSession) Close() error {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.mu.Unlock()
	if s.connection == nil {
		return nil
	}
	return s.connection.Close()
}
