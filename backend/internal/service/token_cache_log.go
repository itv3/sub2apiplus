package service

import (
	"context"
	"errors"
	"log/slog"
)

func tokenCacheFailureLogLevel(err error) slog.Level {
	if errors.Is(err, context.Canceled) {
		return slog.LevelDebug
	}
	return slog.LevelWarn
}

func logTokenCacheFailure(message string, accountID int64, err error) {
	if err == nil {
		return
	}
	if tokenCacheFailureLogLevel(err) == slog.LevelDebug {
		slog.Debug(message, "account_id", accountID, "error", err)
		return
	}
	slog.Warn(message, "account_id", accountID, "error", err)
}
