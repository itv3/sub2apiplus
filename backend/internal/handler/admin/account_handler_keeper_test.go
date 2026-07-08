package admin

import (
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

func TestBuildKeeperAccountConfigUsesLaterActivityTime(t *testing.T) {
	now := time.Date(2026, 7, 8, 12, 0, 0, 0, time.UTC)
	lastUsed := now.Add(-30 * time.Minute)
	lastKeepalive := now.Add(-5 * time.Minute)
	account := service.Account{
		ID:         1,
		Name:       "keeper-openai",
		Platform:   service.PlatformOpenAI,
		Type:       service.AccountTypeAPIKey,
		LastUsedAt: &lastUsed,
		Extra: map[string]any{
			"keeper_keepalive_enabled":          true,
			"keeper_keepalive_interval_minutes": 8,
			"keeper_last_keepalive_at":          lastKeepalive.Format(time.RFC3339),
		},
	}

	got := buildKeeperAccountConfig(account, now)

	if got.NextKeepaliveAt == nil {
		t.Fatal("NextKeepaliveAt is nil")
	}
	want := lastKeepalive.Add(8 * time.Minute)
	if !got.NextKeepaliveAt.Equal(want) {
		t.Fatalf("NextKeepaliveAt = %s, want %s", got.NextKeepaliveAt, want)
	}
	if got.Due {
		t.Fatal("Due = true, want false before later keepalive interval")
	}
}

func TestBuildKeeperAccountConfigDueWithoutActivity(t *testing.T) {
	account := service.Account{
		ID:       2,
		Name:     "keeper-anthropic",
		Platform: service.PlatformAnthropic,
		Type:     service.AccountTypeAPIKey,
		Extra: map[string]any{
			"keeper_keepalive_enabled": true,
		},
	}

	got := buildKeeperAccountConfig(account, time.Now().UTC())

	if got.NextKeepaliveAt != nil {
		t.Fatalf("NextKeepaliveAt = %s, want nil", got.NextKeepaliveAt)
	}
	if !got.Due {
		t.Fatal("Due = false, want true for enabled account without activity")
	}
}
