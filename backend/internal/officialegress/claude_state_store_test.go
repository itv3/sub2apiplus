package officialegress

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
)

type claudeObservedOwnerRaceStore struct {
	base         *claudeMemoryStateStore
	initialOwner string
	injected     bool
}

func (s *claudeObservedOwnerRaceStore) Load(
	ctx context.Context,
	key string,
) (ClaudeStateSnapshot, error) {
	return s.base.Load(ctx, key)
}

func (s *claudeObservedOwnerRaceStore) LookupRequestOwner(
	ctx context.Context,
	requestID string,
) (string, error) {
	if !s.injected {
		return s.initialOwner, nil
	}
	return s.base.LookupRequestOwner(ctx, requestID)
}

func (s *claudeObservedOwnerRaceStore) CompareAndSwap(
	ctx context.Context,
	key string,
	mutation ClaudeStateMutation,
) (bool, error) {
	if mutation.ObservedRequestID != "" && !s.injected {
		s.injected = true
		if _, err := s.base.CompareAndSwap(ctx, "race-owner", ClaudeStateMutation{
			ExpectedVersion: 0,
			Payload:         []byte(`{"owner":"race"}`),
			TTL:             time.Hour,
			RequestID:       mutation.ObservedRequestID,
			RequestOwner:    "different-session",
		}); err != nil {
			return false, err
		}
		return false, ErrClaudeStateObservedOwnerChanged
	}
	return s.base.CompareAndSwap(ctx, key, mutation)
}

func TestClaudeStateStoreScopesCandidateAndProduction(t *testing.T) {
	ctx := context.Background()
	base := newClaudeMemoryStateStore()
	candidate := newClaudeScopedStateStore(base, "candidate")
	production := newClaudeScopedStateStore(base, "production")

	for name, store := range map[string]ClaudeStateStore{
		"candidate":  candidate,
		"production": production,
	} {
		committed, err := store.CompareAndSwap(ctx, "same-session", ClaudeStateMutation{
			ExpectedVersion: 0,
			Payload:         []byte(name),
			TTL:             time.Hour,
			RequestID:       "req_SameUpstreamID",
			RequestOwner:    name,
		})
		if err != nil || !committed {
			t.Fatalf("%s 状态作用域写入失败：committed=%t err=%v", name, committed, err)
		}
	}

	candidateSnapshot, err := candidate.Load(ctx, "same-session")
	if err != nil || string(candidateSnapshot.Payload) != "candidate" {
		t.Fatalf("Candidate 状态作用域串线：snapshot=%+v err=%v", candidateSnapshot, err)
	}
	productionSnapshot, err := production.Load(ctx, "same-session")
	if err != nil || string(productionSnapshot.Payload) != "production" {
		t.Fatalf("Production 状态作用域串线：snapshot=%+v err=%v", productionSnapshot, err)
	}
	candidateOwner, err := candidate.LookupRequestOwner(ctx, "req_SameUpstreamID")
	if err != nil || candidateOwner != "candidate" {
		t.Fatalf("Candidate request-id 所有权串线：owner=%q err=%v", candidateOwner, err)
	}
	productionOwner, err := production.LookupRequestOwner(ctx, "req_SameUpstreamID")
	if err != nil || productionOwner != "production" {
		t.Fatalf("Production request-id 所有权串线：owner=%q err=%v", productionOwner, err)
	}
}

func TestClaudeSessionRetriesAtomicPreviousOwnerChange(t *testing.T) {
	facts := claudeTestTrustedFacts()
	facts.Session.SessionID = "88888888-8888-4888-8888-888888888888"
	facts.Session.Source = ClaudeSessionSourceOfficialConsistent
	facts.Session.PreviousRequestID = "req_ConcurrentOwner"
	identity := mustClaudeTestIdentity(t, facts)
	sessionKey := claudeAttestationDigest(
		"session-state", ClaudeFWGReleaseDigest, identity.accountScope,
		identity.sessionID, identity.entrypoint,
	)
	store := &claudeObservedOwnerRaceStore{
		base: newClaudeMemoryStateStore(), initialOwner: sessionKey,
	}
	runtime := &ClaudeRuntime{stateStore: store, newStateLeaseID: uuid.NewString}

	lease, err := runtime.prepareClaudeSessionRequest(
		&identity, ClaudeCanonicalRequest{}, claudeMessageRelations{},
	)
	if err != nil {
		t.Fatalf("所有权并发变化后未重新读取并收敛：%v", err)
	}
	if !identity.forked || !store.injected {
		t.Fatalf("所有权并发变化未按新事实识别 fork：identity=%+v", identity)
	}
	if err := runtime.finalizeClaudeSessionRequest(lease, false, 0, ""); err != nil {
		t.Fatal(err)
	}
}
