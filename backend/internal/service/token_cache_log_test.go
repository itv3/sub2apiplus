package service

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestTokenCacheFailureLogLevel(t *testing.T) {
	require.Equal(t, slog.LevelDebug, tokenCacheFailureLogLevel(context.Canceled))
	require.Equal(
		t,
		slog.LevelDebug,
		tokenCacheFailureLogLevel(fmt.Errorf("缓存操作已取消：%w", context.Canceled)),
	)
	require.Equal(t, slog.LevelWarn, tokenCacheFailureLogLevel(context.DeadlineExceeded))
	require.Equal(t, slog.LevelWarn, tokenCacheFailureLogLevel(errors.New("redis unavailable")))
}
