package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/imroc/req/v3"
	"github.com/stretchr/testify/require"
)

type privacyResponseTransport struct {
	status int
	body   string
}

func (t privacyResponseTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return &http.Response{
		StatusCode: t.status,
		Status:     http.StatusText(t.status),
		Header:     http.Header{"Content-Type": []string{"text/html"}},
		Body:       io.NopCloser(strings.NewReader(t.body)),
		Request:    request,
	}, nil
}

func TestShouldSkipOpenAIPrivacyEnsureHonorsFailureCooldown(t *testing.T) {
	now := time.Date(2026, 8, 2, 9, 0, 0, 0, time.UTC)
	extra := map[string]any{
		"privacy_mode":            PrivacyModeCFBlocked,
		privacyRetryAfterExtraKey: now.Add(time.Hour).Format(time.RFC3339),
	}
	require.True(t, shouldSkipOpenAIPrivacyEnsureAt(extra, now),
		"冷却期内不得每轮 token 刷新重打")
	require.False(t, shouldSkipOpenAIPrivacyEnsureAt(extra, now.Add(2*time.Hour)),
		"冷却到期后应允许下一次受控重试")
}

func TestDisableOpenAITrainingRecordsPersonaCooldownAndMetrics(t *testing.T) {
	client := req.NewClient()
	client.GetClient().Transport = privacyResponseTransport{
		status: http.StatusServiceUnavailable,
		body:   "Cloudflare: Just a moment",
	}
	factory := func(request PrivacyClientRequest) (*PrivacyHTTPClient, error) {
		return &PrivacyHTTPClient{
			Client:          client,
			Persona:         "chrome_133_xhr",
			FailureCooldown: time.Hour,
		}, nil
	}
	metricKey := "disable_training|chrome_133_xhr|cf_blocked"
	before := SnapshotOpenAIPrivacyPersonaMetrics().Results[metricKey]

	rolloutKey := buildOpenAIPrivacyRolloutKey("account-stable-test")
	result := ensureOpenAITraining(
		context.Background(),
		factory,
		"access-test",
		"",
		openAIPrivacyEnsureInput{RolloutKey: rolloutKey},
	)
	require.Equal(t, PrivacyModeCFBlocked, result.Mode)
	require.Equal(t, "chrome_133_xhr", result.Persona)
	require.WithinDuration(t, time.Now().UTC().Add(time.Hour), result.RetryAfter, 5*time.Second)
	updates := result.ExtraUpdates()
	require.Equal(t, PrivacyModeCFBlocked, updates["privacy_mode"])
	require.Equal(t, "chrome_133_xhr", updates[privacyPersonaExtraKey])
	require.NotEmpty(t, updates[privacyRetryAfterExtraKey])
	require.Equal(t, rolloutKey, updates[privacyRolloutKeyExtraKey])
	require.Equal(t, before+1, SnapshotOpenAIPrivacyPersonaMetrics().Results[metricKey])
}
