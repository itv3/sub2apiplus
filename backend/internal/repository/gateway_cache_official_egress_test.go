package repository

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/require"
)

func TestGatewayCacheAnthropicOfficialEgressResponseMapping(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() {
		_ = client.Close()
	})
	cache := &gatewayCache{rdb: client}

	const (
		sessionKey = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		requestID  = "req_official_egress"
	)
	value, err := cache.GetAnthropicPreviousRequestID(context.Background(), sessionKey)
	require.NoError(t, err)
	require.Empty(t, value)

	require.NoError(t, cache.SetAnthropicPreviousRequestID(
		context.Background(),
		sessionKey,
		requestID,
		time.Hour,
	))
	value, err = cache.GetAnthropicPreviousRequestID(context.Background(), sessionKey)
	require.NoError(t, err)
	require.Equal(t, requestID, value)
	require.Equal(t, time.Hour, server.TTL(anthropicPreviousRequestPrefix+sessionKey))
}
