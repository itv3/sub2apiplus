package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	//nolint:depguard // 该文件本身就是 ClaudeStateStore 的 Redis 基础设施适配器。
	"github.com/redis/go-redis/v9"
)

const claudeRedisStatePrefix = "official-egress:claude-code:"

var claudeRedisStateCompareAndSwap = redis.NewScript(`
local current = redis.call("HGET", KEYS[1], "version")
if not current then
  current = "0"
end
if current ~= ARGV[1] then
  return 0
end
if ARGV[7] == "1" then
  local observed_owner = redis.call("GET", KEYS[3])
  if not observed_owner then
    observed_owner = ""
  end
  if observed_owner ~= ARGV[6] then
    return -2
  end
end
if ARGV[5] == "1" then
  local owner = redis.call("GET", KEYS[2])
  if owner and owner ~= ARGV[4] then
    return -1
  end
end
local next_version = tostring(tonumber(ARGV[1]) + 1)
redis.call("HSET", KEYS[1], "version", next_version, "payload", ARGV[2])
redis.call("PEXPIRE", KEYS[1], ARGV[3])
if ARGV[5] == "1" then
  redis.call("SET", KEYS[2], ARGV[4], "PX", ARGV[3])
end
return tonumber(next_version)
`)

type claudeRedisStateStore struct {
	client *redis.Client
	prefix string
}

func newClaudeRedisStateStore(client *redis.Client) officialegress.ClaudeStateStore {
	return &claudeRedisStateStore{client: client, prefix: claudeRedisStatePrefix}
}

func (s *claudeRedisStateStore) Load(
	ctx context.Context,
	key string,
) (officialegress.ClaudeStateSnapshot, error) {
	if s == nil || s.client == nil {
		return officialegress.ClaudeStateSnapshot{}, errors.New("Claude Redis 状态存储未配置")
	}
	key = strings.TrimSpace(key)
	if key == "" {
		return officialegress.ClaudeStateSnapshot{}, errors.New("Claude Redis 会话键为空")
	}
	values, err := s.client.HMGet(ctx, s.sessionKey(key), "version", "payload").Result()
	if err != nil {
		return officialegress.ClaudeStateSnapshot{}, err
	}
	if len(values) != 2 || values[0] == nil || values[1] == nil {
		return officialegress.ClaudeStateSnapshot{}, nil
	}
	version, err := strconv.ParseUint(fmt.Sprint(values[0]), 10, 64)
	if err != nil || version == 0 {
		return officialegress.ClaudeStateSnapshot{}, errors.New("Claude Redis 状态版本非法")
	}
	payload := []byte(fmt.Sprint(values[1]))
	if len(payload) == 0 {
		return officialegress.ClaudeStateSnapshot{}, errors.New("Claude Redis 状态 Payload 为空")
	}
	return officialegress.ClaudeStateSnapshot{
		Found: true, Version: version, Payload: payload,
	}, nil
}

func (s *claudeRedisStateStore) LookupRequestOwner(
	ctx context.Context,
	requestID string,
) (string, error) {
	if s == nil || s.client == nil {
		return "", errors.New("Claude Redis 状态存储未配置")
	}
	requestID = strings.TrimSpace(requestID)
	if requestID == "" {
		return "", nil
	}
	owner, err := s.client.Get(ctx, s.ownerKey(requestID)).Result()
	if errors.Is(err, redis.Nil) {
		return "", nil
	}
	return owner, err
}

func (s *claudeRedisStateStore) CompareAndSwap(
	ctx context.Context,
	key string,
	mutation officialegress.ClaudeStateMutation,
) (bool, error) {
	if s == nil || s.client == nil {
		return false, errors.New("Claude Redis 状态存储未配置")
	}
	key = strings.TrimSpace(key)
	if key == "" || mutation.TTL <= 0 || len(mutation.Payload) == 0 {
		return false, errors.New("Claude Redis 状态提交参数不完整")
	}
	claimOwner := mutation.RequestID != "" || mutation.RequestOwner != ""
	if (mutation.RequestID == "") != (mutation.RequestOwner == "") {
		return false, errors.New("Claude Redis request-id 所有权声明不完整")
	}
	if mutation.ObservedRequestID == "" && mutation.ObservedRequestOwner != "" {
		return false, errors.New("Claude Redis previous request 所有权观察不完整")
	}
	ownerKey := s.ownerKey("no-owner")
	claimFlag := "0"
	if claimOwner {
		ownerKey = s.ownerKey(mutation.RequestID)
		claimFlag = "1"
	}
	observedOwnerKey := s.ownerKey("no-observed-owner")
	observeFlag := "0"
	if mutation.ObservedRequestID != "" {
		observedOwnerKey = s.ownerKey(mutation.ObservedRequestID)
		observeFlag = "1"
	}
	result, err := claudeRedisStateCompareAndSwap.Run(
		ctx,
		s.client,
		[]string{s.sessionKey(key), ownerKey, observedOwnerKey},
		strconv.FormatUint(mutation.ExpectedVersion, 10),
		mutation.Payload,
		mutation.TTL.Milliseconds(),
		mutation.RequestOwner,
		claimFlag,
		mutation.ObservedRequestOwner,
		observeFlag,
	).Int64()
	if err != nil {
		return false, err
	}
	if result == -1 {
		return false, officialegress.ErrClaudeStateRequestOwnerConflict
	}
	if result == -2 {
		return false, officialegress.ErrClaudeStateObservedOwnerChanged
	}
	return result > 0, nil
}

func (s *claudeRedisStateStore) sessionKey(key string) string {
	return s.prefix + "session:" + strings.TrimSpace(key)
}

func (s *claudeRedisStateStore) ownerKey(requestID string) string {
	digest := sha256.Sum256([]byte(strings.TrimSpace(requestID)))
	return s.prefix + "request-owner:" + hex.EncodeToString(digest[:])
}
