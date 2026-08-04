package repository

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
)

func TestProvideOfficialEgressGuardRejectsMalformedRuntimeControls(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.OfficialEgressGuard.UnknownRoutePolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.UnregisteredSinkPolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.MaxUniqueLogSamples = 16
	cfg.Gateway.OfficialEgressGuard.SinkControlsJSON =
		`{"schema_version":1,"controls":[{"sink_id":"missing.sink","state":"canary_enforce"}]}`
	if _, err := ProvideOfficialEgressGuard(cfg); err == nil {
		t.Fatal("生产 wiring 未拒绝未登记 Sink 的运行时控制")
	}
}

func TestProvideOfficialEgressGuardRejectsZeroPercentCanaryDowngrade(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.OfficialEgressGuard.UnknownRoutePolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.UnregisteredSinkPolicy = "enforce"
	cfg.Gateway.OfficialEgressGuard.CanaryPercent = 0
	cfg.Gateway.OfficialEgressGuard.MaxUniqueLogSamples = 16
	cfg.Gateway.OfficialEgressGuard.SinkControlsJSON =
		`{"schema_version":1,"controls":[{"sink_id":"codex.admin_test.compact","state":"canary_enforce"}]}`
	if _, err := ProvideOfficialEgressGuard(cfg); err == nil {
		t.Fatal("生产 wiring 接受了 canary_enforce + 0% 的无期限全量 observe")
	}
}
