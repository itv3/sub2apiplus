package service

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/alicebob/miniredis/v2"
	//nolint:depguard // 测试需要构造与生产适配器相同的 Redis 客户端。
	"github.com/redis/go-redis/v9"
)

func TestClaudeRedisStateStorePersistsCASAndRequestOwnership(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	ctx := context.Background()
	store := newClaudeRedisStateStore(client)

	committed, err := store.CompareAndSwap(ctx, "session-a", officialegress.ClaudeStateMutation{
		ExpectedVersion: 0,
		Payload:         []byte(`{"state":"outer"}`),
		TTL:             time.Hour,
	})
	if err != nil || !committed {
		t.Fatalf("首次 Claude Redis 状态提交失败：committed=%t err=%v", committed, err)
	}
	snapshot, err := newClaudeRedisStateStore(client).Load(ctx, "session-a")
	if err != nil || !snapshot.Found || snapshot.Version != 1 ||
		string(snapshot.Payload) != `{"state":"outer"}` {
		t.Fatalf("重建 Store 后 Claude 状态不完整：snapshot=%+v err=%v", snapshot, err)
	}
	committed, err = store.CompareAndSwap(ctx, "session-a", officialegress.ClaudeStateMutation{
		ExpectedVersion: 0,
		Payload:         []byte(`{"state":"stale"}`),
		TTL:             time.Hour,
	})
	if err != nil || committed {
		t.Fatalf("过期版本错误覆盖 Claude Redis 状态：committed=%t err=%v", committed, err)
	}
	committed, err = store.CompareAndSwap(ctx, "session-a", officialegress.ClaudeStateMutation{
		ExpectedVersion: snapshot.Version,
		Payload:         []byte(`{"state":"complete"}`),
		TTL:             time.Hour,
		RequestID:       "req_SharedOwner",
		RequestOwner:    "session-a",
	})
	if err != nil || !committed {
		t.Fatalf("Claude Redis 状态与 request-id 原子提交失败：committed=%t err=%v", committed, err)
	}
	owner, err := store.LookupRequestOwner(ctx, "req_SharedOwner")
	if err != nil || owner != "session-a" {
		t.Fatalf("Claude Redis request-id 所有权缺失：owner=%q err=%v", owner, err)
	}
	committed, err = store.CompareAndSwap(ctx, "session-b", officialegress.ClaudeStateMutation{
		ExpectedVersion: 0,
		Payload:         []byte(`{"state":"conflict"}`),
		TTL:             time.Hour,
		RequestID:       "req_SharedOwner",
		RequestOwner:    "session-b",
	})
	if committed || !errors.Is(err, officialegress.ErrClaudeStateRequestOwnerConflict) {
		t.Fatalf("跨会话 request-id 冲突未原子拒绝：committed=%t err=%v", committed, err)
	}
	conflictSnapshot, loadErr := store.Load(ctx, "session-b")
	if loadErr != nil || conflictSnapshot.Found {
		t.Fatalf("所有权冲突后写入了部分 Claude 状态：snapshot=%+v err=%v", conflictSnapshot, loadErr)
	}

	observedOwner, err := store.LookupRequestOwner(ctx, "req_ObservedRace")
	if err != nil || observedOwner != "" {
		t.Fatalf("Claude Redis 并发观察前状态非法：owner=%q err=%v", observedOwner, err)
	}
	committed, err = store.CompareAndSwap(ctx, "session-c", officialegress.ClaudeStateMutation{
		ExpectedVersion: 0,
		Payload:         []byte(`{"state":"claimed"}`),
		TTL:             time.Hour,
		RequestID:       "req_ObservedRace",
		RequestOwner:    "session-c",
	})
	if err != nil || !committed {
		t.Fatalf("Claude Redis 并发所有权准备失败：committed=%t err=%v", committed, err)
	}
	committed, err = store.CompareAndSwap(ctx, "session-d", officialegress.ClaudeStateMutation{
		ExpectedVersion:      0,
		Payload:              []byte(`{"state":"stale-observation"}`),
		TTL:                  time.Hour,
		ObservedRequestID:    "req_ObservedRace",
		ObservedRequestOwner: observedOwner,
	})
	if committed || !errors.Is(err, officialegress.ErrClaudeStateObservedOwnerChanged) {
		t.Fatalf("Claude Redis 未原子拒绝过期所有权观察：committed=%t err=%v", committed, err)
	}
	staleSnapshot, loadErr := store.Load(ctx, "session-d")
	if loadErr != nil || staleSnapshot.Found {
		t.Fatalf("过期所有权观察写入了部分 Claude 状态：snapshot=%+v err=%v", staleSnapshot, loadErr)
	}
}

func TestProvideOfficialEgressTransitionRuntimeRequiresRedis(t *testing.T) {
	_, err := ProvideOfficialEgressTransitionRuntime(nil, nil, nil, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "缺少 Redis 状态存储") {
		t.Fatalf("Claude production Persona 未在启动期拒绝空 Redis：%v", err)
	}
}
