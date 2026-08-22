package officialegress

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

var (
	ErrClaudeStateRequestOwnerConflict = errors.New("Claude request-id 已属于其他会话")
	ErrClaudeStateObservedOwnerChanged = errors.New("Claude previous request 所有权已变化")
)

// ClaudeStateSnapshot 是 Persona 私有状态的一次带版本只读快照。
type ClaudeStateSnapshot struct {
	Found   bool
	Version uint64
	Payload []byte
}

// ClaudeStateMutation 描述一次原子状态提交及可选的 request-id 所有权声明。
type ClaudeStateMutation struct {
	ExpectedVersion      uint64
	Payload              []byte
	TTL                  time.Duration
	RequestID            string
	RequestOwner         string
	ObservedRequestID    string
	ObservedRequestOwner string
}

// ClaudeStateStore 只保存 Claude Persona 的不透明状态，不向共享框架暴露厂商字段。
type ClaudeStateStore interface {
	Load(context.Context, string) (ClaudeStateSnapshot, error)
	LookupRequestOwner(context.Context, string) (string, error)
	CompareAndSwap(context.Context, string, ClaudeStateMutation) (bool, error)
}

type claudeScopedStateStore struct {
	base      ClaudeStateStore
	namespace string
}

func newClaudeScopedStateStore(base ClaudeStateStore, namespace string) ClaudeStateStore {
	return &claudeScopedStateStore{base: base, namespace: namespace}
}

func (s *claudeScopedStateStore) Load(
	ctx context.Context,
	key string,
) (ClaudeStateSnapshot, error) {
	return s.base.Load(ctx, s.scopedKey("state", key))
}

func (s *claudeScopedStateStore) LookupRequestOwner(
	ctx context.Context,
	requestID string,
) (string, error) {
	return s.base.LookupRequestOwner(ctx, s.scopedKey("request", requestID))
}

func (s *claudeScopedStateStore) CompareAndSwap(
	ctx context.Context,
	key string,
	mutation ClaudeStateMutation,
) (bool, error) {
	if mutation.RequestID != "" {
		mutation.RequestID = s.scopedKey("request", mutation.RequestID)
	}
	if mutation.ObservedRequestID != "" {
		mutation.ObservedRequestID = s.scopedKey("request", mutation.ObservedRequestID)
	}
	return s.base.CompareAndSwap(ctx, s.scopedKey("state", key), mutation)
}

func (s *claudeScopedStateStore) scopedKey(kind string, value string) string {
	return claudeAttestationDigest("claude-state-scope", s.namespace, kind, value)
}

type claudeMemoryStateRecord struct {
	version   uint64
	payload   []byte
	expiresAt time.Time
}

type claudeMemoryOwnerRecord struct {
	owner     string
	expiresAt time.Time
}

type claudeMemoryStateStore struct {
	mu     sync.Mutex
	states map[string]claudeMemoryStateRecord
	owners map[string]claudeMemoryOwnerRecord
	now    func() time.Time
}

type claudePersistedSessionLineState struct {
	PreviousRequestID    string `json:"previous_request_id,omitempty"`
	FallbackPrimaryModel string `json:"fallback_primary_model,omitempty"`
	FallbackModel        string `json:"fallback_model,omitempty"`
	FallbackLatchedBy    string `json:"fallback_latched_by,omitempty"`
	InFlightLeaseID      string `json:"in_flight_lease_id,omitempty"`
	InFlightExpiresAt    int64  `json:"in_flight_expires_at,omitempty"`
}

type claudePersistedAgentLineageState struct {
	ParentAgentID string `json:"parent_agent_id,omitempty"`
	Depth         int    `json:"depth"`
}

type claudePersistedSessionState struct {
	SchemaVersion                 int                                         `json:"schema_version"`
	Lines                         map[string]claudePersistedSessionLineState  `json:"lines"`
	AgentLineages                 map[string]claudePersistedAgentLineageState `json:"agent_lineages"`
	RequestIDs                    []string                                    `json:"request_ids"`
	TUITitleCompleted             bool                                        `json:"tui_title_completed,omitempty"`
	TUITitleLeaseID               string                                      `json:"tui_title_lease_id,omitempty"`
	TUITitleLeaseExpiresAt        int64                                       `json:"tui_title_lease_expires_at,omitempty"`
	WebSearchParentRequestID      string                                      `json:"web_search_parent_request_id,omitempty"`
	WebSearchServerCompleted      bool                                        `json:"web_search_server_completed,omitempty"`
	WebSearchServerLeaseID        string                                      `json:"web_search_server_lease_id,omitempty"`
	WebSearchServerLeaseExpiresAt int64                                       `json:"web_search_server_lease_expires_at,omitempty"`
	UpdatedAt                     int64                                       `json:"updated_at"`
}

func marshalClaudeSessionState(state *claudeSessionState) ([]byte, error) {
	if state == nil {
		return nil, errors.New("Claude 会话状态为空")
	}
	persisted := claudePersistedSessionState{
		SchemaVersion:                 1,
		Lines:                         make(map[string]claudePersistedSessionLineState, len(state.lines)),
		AgentLineages:                 make(map[string]claudePersistedAgentLineageState, len(state.agentLineages)),
		RequestIDs:                    make([]string, 0, len(state.requestIDs)),
		TUITitleCompleted:             state.tuiTitleCompleted,
		TUITitleLeaseID:               state.tuiTitleLeaseID,
		TUITitleLeaseExpiresAt:        state.tuiTitleLeaseExpiresAt.UnixMilli(),
		WebSearchParentRequestID:      state.webSearchParentRequestID,
		WebSearchServerCompleted:      state.webSearchServerCompleted,
		WebSearchServerLeaseID:        state.webSearchServerLeaseID,
		WebSearchServerLeaseExpiresAt: state.webSearchLeaseExpiresAt.UnixMilli(),
		UpdatedAt:                     state.updatedAt.UnixMilli(),
	}
	for key, line := range state.lines {
		if line == nil {
			return nil, fmt.Errorf("Claude 会话谱系为空：%s", key)
		}
		persisted.Lines[key] = claudePersistedSessionLineState{
			PreviousRequestID:    line.previousRequestID,
			FallbackPrimaryModel: line.fallbackPrimaryModel,
			FallbackModel:        line.fallbackModel,
			FallbackLatchedBy:    line.fallbackLatchedBy,
			InFlightLeaseID:      line.inFlightLeaseID,
			InFlightExpiresAt:    line.inFlightExpiresAt.UnixMilli(),
		}
	}
	for key, lineage := range state.agentLineages {
		persisted.AgentLineages[key] = claudePersistedAgentLineageState{
			ParentAgentID: lineage.parentAgentID, Depth: lineage.depth,
		}
	}
	for requestID := range state.requestIDs {
		persisted.RequestIDs = append(persisted.RequestIDs, requestID)
	}
	sort.Strings(persisted.RequestIDs)
	return json.Marshal(persisted)
}

func unmarshalClaudeSessionState(payload []byte) (*claudeSessionState, error) {
	var persisted claudePersistedSessionState
	if err := json.Unmarshal(payload, &persisted); err != nil {
		return nil, fmt.Errorf("解析 Claude 会话状态：%w", err)
	}
	if persisted.SchemaVersion != 1 {
		return nil, errors.New("Claude 会话状态 Schema 版本不受支持")
	}
	state := newClaudeSessionState()
	state.tuiTitleCompleted = persisted.TUITitleCompleted
	state.tuiTitleLeaseID = persisted.TUITitleLeaseID
	state.tuiTitleLeaseExpiresAt = time.UnixMilli(persisted.TUITitleLeaseExpiresAt)
	state.webSearchParentRequestID = persisted.WebSearchParentRequestID
	state.webSearchServerCompleted = persisted.WebSearchServerCompleted
	state.webSearchServerLeaseID = persisted.WebSearchServerLeaseID
	state.webSearchLeaseExpiresAt = time.UnixMilli(persisted.WebSearchServerLeaseExpiresAt)
	state.updatedAt = time.UnixMilli(persisted.UpdatedAt)
	for key, line := range persisted.Lines {
		state.lines[key] = &claudeSessionLineState{
			previousRequestID:    line.PreviousRequestID,
			fallbackPrimaryModel: line.FallbackPrimaryModel,
			fallbackModel:        line.FallbackModel,
			fallbackLatchedBy:    line.FallbackLatchedBy,
			inFlightLeaseID:      line.InFlightLeaseID,
			inFlightExpiresAt:    time.UnixMilli(line.InFlightExpiresAt),
		}
	}
	for key, lineage := range persisted.AgentLineages {
		state.agentLineages[key] = claudeAgentLineageState{
			parentAgentID: lineage.ParentAgentID, depth: lineage.Depth,
		}
	}
	for _, requestID := range persisted.RequestIDs {
		if requestID == "" {
			return nil, errors.New("Claude 会话状态含空 request-id")
		}
		state.requestIDs[requestID] = struct{}{}
	}
	return state, nil
}

func newClaudeMemoryStateStore() *claudeMemoryStateStore {
	return &claudeMemoryStateStore{
		states: make(map[string]claudeMemoryStateRecord),
		owners: make(map[string]claudeMemoryOwnerRecord),
		now:    time.Now,
	}
}

func (s *claudeMemoryStateStore) Load(
	_ context.Context,
	key string,
) (ClaudeStateSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	record, ok := s.states[key]
	if !ok || !record.expiresAt.After(now) {
		delete(s.states, key)
		return ClaudeStateSnapshot{}, nil
	}
	return ClaudeStateSnapshot{
		Found: true, Version: record.version,
		Payload: append([]byte(nil), record.payload...),
	}, nil
}

func (s *claudeMemoryStateStore) LookupRequestOwner(
	_ context.Context,
	requestID string,
) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	record, ok := s.owners[requestID]
	if !ok || !record.expiresAt.After(now) {
		delete(s.owners, requestID)
		return "", nil
	}
	return record.owner, nil
}

func (s *claudeMemoryStateStore) CompareAndSwap(
	_ context.Context,
	key string,
	mutation ClaudeStateMutation,
) (bool, error) {
	if mutation.TTL <= 0 {
		return false, errors.New("Claude 状态提交缺少有效 TTL")
	}
	if (mutation.RequestID == "") != (mutation.RequestOwner == "") {
		return false, errors.New("Claude request-id 所有权声明不完整")
	}
	if mutation.ObservedRequestID == "" && mutation.ObservedRequestOwner != "" {
		return false, errors.New("Claude previous request 所有权观察不完整")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	record, exists := s.states[key]
	if exists && !record.expiresAt.After(now) {
		delete(s.states, key)
		exists = false
	}
	currentVersion := uint64(0)
	if exists {
		currentVersion = record.version
	}
	if currentVersion != mutation.ExpectedVersion {
		return false, nil
	}
	if mutation.ObservedRequestID != "" {
		observed, observedExists := s.owners[mutation.ObservedRequestID]
		if observedExists && !observed.expiresAt.After(now) {
			delete(s.owners, mutation.ObservedRequestID)
			observedExists = false
		}
		observedOwner := ""
		if observedExists {
			observedOwner = observed.owner
		}
		if observedOwner != mutation.ObservedRequestOwner {
			return false, ErrClaudeStateObservedOwnerChanged
		}
	}
	if mutation.RequestID != "" {
		owner, ownerExists := s.owners[mutation.RequestID]
		if ownerExists && !owner.expiresAt.After(now) {
			delete(s.owners, mutation.RequestID)
			ownerExists = false
		}
		if ownerExists && owner.owner != mutation.RequestOwner {
			return false, ErrClaudeStateRequestOwnerConflict
		}
	}
	expiresAt := now.Add(mutation.TTL)
	s.states[key] = claudeMemoryStateRecord{
		version:   mutation.ExpectedVersion + 1,
		payload:   append([]byte(nil), mutation.Payload...),
		expiresAt: expiresAt,
	}
	if mutation.RequestID != "" {
		s.owners[mutation.RequestID] = claudeMemoryOwnerRecord{
			owner: mutation.RequestOwner, expiresAt: expiresAt,
		}
	}
	return true, nil
}
