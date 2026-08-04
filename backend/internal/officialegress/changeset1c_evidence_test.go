package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"testing"
	"time"
)

type changeset1CEvidenceItem struct {
	ID          string   `json:"exercise_id,omitempty"`
	Validation  string   `json:"validation_id,omitempty"`
	Mode        string   `json:"mode,omitempty"`
	Environment string   `json:"environment"`
	Result      string   `json:"result"`
	Evidence    []string `json:"evidence"`
}

type changeset1CEvidenceManifest struct {
	SchemaVersion  int                       `json:"schema_version"`
	Changeset      string                    `json:"changeset"`
	Result         string                    `json:"result"`
	ObservedAt     string                    `json:"observed_at"`
	Exercises      []changeset1CEvidenceItem `json:"exercises"`
	Validations    []changeset1CEvidenceItem `json:"validations"`
	ReviewedBy     string                    `json:"reviewed_by"`
	ReviewRef      string                    `json:"review_ref"`
	ApprovalStatus string                    `json:"approval_status"`
	Verification   changeset1CArtifactRef    `json:"verification"`
}

type changeset1CArtifactRef struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type changeset1CVerification struct {
	SchemaVersion int    `json:"schema_version"`
	Changeset     string `json:"changeset"`
	Environment   string `json:"environment"`
	Window        struct {
		StartedAt       string `json:"started_at"`
		EndedAt         string `json:"ended_at"`
		DurationSeconds int64  `json:"duration_seconds"`
		ImageTag        string `json:"image_tag"`
		ImageID         string `json:"image_id"`
		BinarySHA256    string `json:"binary_sha256"`
		GuardLogPath    string `json:"guard_log_path"`
		GuardLogSHA256  string `json:"guard_log_sha256"`
	} `json:"window"`
	FinalState struct {
		UnknownRoutePolicy     string `json:"unknown_route_policy"`
		UnregisteredSinkPolicy string `json:"unregistered_sink_policy"`
		CanaryPercent          int    `json:"canary_percent"`
		SinkControlsEmpty      bool   `json:"sink_controls_empty"`
		PolicyOverridesEmpty   bool   `json:"policy_overrides_empty"`
	} `json:"final_state"`
	ExerciseIDs      []string          `json:"exercise_ids"`
	GuardEventCounts map[string]uint64 `json:"guard_event_counts"`
	RollbackDrill    struct {
		Canary struct {
			SinkID          string `json:"sink_id"`
			BeforeState     string `json:"before_state"`
			RollbackState   string `json:"rollback_state"`
			CanaryPercent   int    `json:"canary_percent"`
			LiveHTTPSuccess bool   `json:"live_http_success"`
			ObservedReason  string `json:"observed_reason"`
		} `json:"canary"`
		Override struct {
			SinkID                  string `json:"sink_id"`
			ObserveUntil            string `json:"observe_until"`
			Owner                   string `json:"owner"`
			ReasonCode              string `json:"reason_code"`
			LiveHTTPSuccess         bool   `json:"live_http_success"`
			ObservedReason          string `json:"observed_reason"`
			OriginalViolationLogged bool   `json:"original_violation_logged"`
			ExpiryFailCloseVerified bool   `json:"expiry_fail_close_verified"`
		} `json:"override"`
	} `json:"rollback_drill"`
	PanicFatalCount uint64 `json:"panic_fatal_count"`
	ReviewedBy      string `json:"reviewed_by"`
	ReviewRef       string `json:"review_ref"`
	ApprovalStatus  string `json:"approval_status"`
}

type changeset1CGuardLog struct {
	SchemaVersion int    `json:"schema_version"`
	Environment   string `json:"environment"`
	Events        []struct {
		ObservedAt       string `json:"observed_at"`
		ReasonCode       string `json:"reason_code"`
		SinkID           string `json:"sink_id"`
		Method           string `json:"method"`
		HostTemplate     string `json:"host_template"`
		PathTemplate     string `json:"path_template"`
		Backend          string `json:"backend"`
		Protocol         string `json:"protocol"`
		EnforcementState string `json:"enforcement_state"`
	} `json:"events"`
	FinalScan struct {
		StartedAt       string            `json:"started_at"`
		EndedAt         string            `json:"ended_at"`
		ReasonCounts    map[string]uint64 `json:"reason_counts"`
		PanicFatalCount uint64            `json:"panic_fatal_count"`
	} `json:"final_scan"`
}

func TestChangeset1CActiveExerciseEvidenceIsComplete(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/changeset1c/active-exercises.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest changeset1CEvidenceManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("1C 主动演练清单尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 1 || manifest.Changeset != "1C" || manifest.Result != "passed" ||
		manifest.ReviewedBy == "" || manifest.ReviewRef == "" ||
		manifest.ApprovalStatus != "accepted" {
		t.Fatalf("1C 主动演练清单元数据非法：%+v", manifest)
	}
	if _, err := time.Parse(time.RFC3339, manifest.ObservedAt); err != nil {
		t.Fatalf("1C observed_at 非 RFC3339：%v", err)
	}

	wantExercises := []string{
		"agent_task_recovery", "oauth_refresh", "pat_whoami", "privacy",
		"scheduled_account_tests", "spark_shadow", "ws_http_fallback",
	}
	gotExercises := make([]string, 0, len(manifest.Exercises))
	for _, item := range manifest.Exercises {
		if item.ID == "" || item.Validation != "" || item.Mode == "" ||
			item.Environment == "" || item.Result != "passed" || len(item.Evidence) < 2 {
			t.Fatalf("1C 主动演练项不完整：%+v", item)
		}
		gotExercises = append(gotExercises, item.ID)
	}
	if !slices.Equal(gotExercises, wantExercises) {
		t.Fatalf("1C 主动演练项缺失、重复或未排序：got=%v want=%v", gotExercises, wantExercises)
	}

	wantValidations := []string{
		"dmit_enforced_live", "facade_signature_scan", "runtime_fail_close", "single_sink_rollback_contract",
	}
	gotValidations := make([]string, 0, len(manifest.Validations))
	for _, item := range manifest.Validations {
		if item.Validation == "" || item.ID != "" || item.Mode != "" ||
			item.Environment == "" || item.Result != "passed" || len(item.Evidence) < 2 {
			t.Fatalf("1C 验证项不完整：%+v", item)
		}
		gotValidations = append(gotValidations, item.Validation)
	}
	if !slices.Equal(gotValidations, wantValidations) {
		t.Fatalf("1C 验证项缺失、重复或未排序：got=%v want=%v", gotValidations, wantValidations)
	}

	verificationRaw := readChangeset1CVerificationArtifact(t, manifest.Verification)
	var verification changeset1CVerification
	verificationDecoder := json.NewDecoder(bytes.NewReader(verificationRaw))
	verificationDecoder.DisallowUnknownFields()
	if err := verificationDecoder.Decode(&verification); err != nil {
		t.Fatal(err)
	}
	if err := verificationDecoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("1C 可复算验证产物尾部存在额外 JSON")
	}
	verifyChangeset1CStructuredEvidence(t, verification, wantExercises)
}

func readChangeset1CVerificationArtifact(t *testing.T, ref changeset1CArtifactRef) []byte {
	t.Helper()
	if ref.Path != "verification.json" || len(ref.SHA256) != sha256.Size*2 {
		t.Fatalf("1C 验证产物引用非法：%+v", ref)
	}
	raw, err := os.ReadFile("../../../docs/changeset1c/" + ref.Path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != ref.SHA256 {
		t.Fatal("1C 验证产物摘要不一致")
	}
	return raw
}

func verifyChangeset1CStructuredEvidence(
	t *testing.T,
	verification changeset1CVerification,
	wantExercises []string,
) {
	t.Helper()
	if verification.SchemaVersion != 1 || verification.Changeset != "1C" ||
		verification.Environment != "DMIT+local-ci" ||
		verification.ReviewedBy != "codex-audit-changeset-1c-remediation" ||
		verification.ReviewRef == "" || verification.ApprovalStatus != "accepted" {
		t.Fatalf("1C 可复算验证元数据非法：%+v", verification)
	}
	startedAt, err := time.Parse(time.RFC3339, verification.Window.StartedAt)
	if err != nil {
		t.Fatal(err)
	}
	endedAt, err := time.Parse(time.RFC3339, verification.Window.EndedAt)
	if err != nil {
		t.Fatal(err)
	}
	duration := endedAt.Sub(startedAt)
	if duration < 10*time.Minute || verification.Window.DurationSeconds != int64(duration.Seconds()) ||
		verification.Window.ImageTag == "" || !validChangeset1CSHA256(verification.Window.ImageID) ||
		!validChangeset1CSHA256(verification.Window.BinarySHA256) || verification.Window.GuardLogPath != "guard-events.json" ||
		!validChangeset1CSHA256(verification.Window.GuardLogSHA256) {
		t.Fatalf("1C 观察窗口不足或产物摘要非法：%+v", verification.Window)
	}
	guardLog := readChangeset1CGuardLog(t, verification.Window.GuardLogPath, verification.Window.GuardLogSHA256)
	verifyChangeset1CGuardLog(t, guardLog, verification.GuardEventCounts)
	if verification.FinalState.UnknownRoutePolicy != "enforce" ||
		verification.FinalState.UnregisteredSinkPolicy != "enforce" ||
		verification.FinalState.CanaryPercent != 100 || !verification.FinalState.SinkControlsEmpty ||
		!verification.FinalState.PolicyOverridesEmpty {
		t.Fatalf("1C 最终状态不是 fail-close 空控制清单：%+v", verification.FinalState)
	}
	if !slices.Equal(verification.ExerciseIDs, wantExercises) {
		t.Fatalf("1C 结构化验证未覆盖全部主动演练：got=%v want=%v", verification.ExerciseIDs, wantExercises)
	}
	for _, reason := range []string{
		"unknown_route", "missing_sink_id", "unregistered_sink", "sink_binding_mismatch",
		"wrong_backend", "release_digest_mismatch",
	} {
		if total, ok := verification.GuardEventCounts[reason]; !ok || total != 0 {
			t.Fatalf("1C 观察窗口存在未处置 Guard 事件或缺少计数：%s=%d", reason, total)
		}
	}
	if verification.GuardEventCounts[string(ReasonCanaryObservePassthrough)] == 0 ||
		verification.GuardEventCounts[string(ReasonSinkOverrideObserved)] == 0 ||
		verification.GuardEventCounts[string(ReasonWrongExecutor)] == 0 {
		t.Fatalf("1C 回滚演练没有形成可观察 Guard 事件：%+v", verification.GuardEventCounts)
	}
	canary := verification.RollbackDrill.Canary
	if canary.SinkID != string(SinkCodexAdminTestCompact) || canary.BeforeState != string(SinkStateEnforced) ||
		canary.RollbackState != string(SinkStateCanaryEnforce) || canary.CanaryPercent != 1 ||
		!canary.LiveHTTPSuccess || canary.ObservedReason != string(ReasonCanaryObservePassthrough) {
		t.Fatalf("1C 单 Sink canary 回滚没有行为差异：%+v", canary)
	}
	override := verification.RollbackDrill.Override
	observeUntil, err := time.Parse(time.RFC3339, override.ObserveUntil)
	if err != nil {
		t.Fatal(err)
	}
	if override.SinkID != string(SinkCodexAdminTestResponses) || override.Owner == "" ||
		override.ReasonCode == "" || !override.LiveHTTPSuccess ||
		override.ObservedReason != string(ReasonSinkOverrideObserved) ||
		!override.OriginalViolationLogged || !override.ExpiryFailCloseVerified ||
		!observeUntil.After(startedAt) || !observeUntil.Before(endedAt) {
		t.Fatalf("1C 限时覆盖演练不完整：%+v", override)
	}
	if verification.PanicFatalCount != 0 {
		t.Fatalf("1C 观察窗口出现 panic/fatal：%d", verification.PanicFatalCount)
	}
}

func readChangeset1CGuardLog(t *testing.T, path, wantSHA256 string) changeset1CGuardLog {
	t.Helper()
	if path != "guard-events.json" {
		t.Fatalf("1C Guard 日志路径非法：%s", path)
	}
	raw, err := os.ReadFile("../../../docs/changeset1c/" + path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != wantSHA256 {
		t.Fatal("1C Guard 日志摘要不一致")
	}
	var log changeset1CGuardLog
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&log); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("1C Guard 日志尾部存在额外 JSON")
	}
	return log
}

func verifyChangeset1CGuardLog(
	t *testing.T,
	guardLog changeset1CGuardLog,
	wantCounts map[string]uint64,
) {
	t.Helper()
	if guardLog.SchemaVersion != 1 || guardLog.Environment != "DMIT" {
		t.Fatalf("1C Guard 日志元数据非法：%+v", guardLog)
	}
	actualCounts := make(map[string]uint64)
	for _, event := range guardLog.Events {
		if _, err := time.Parse(time.RFC3339, event.ObservedAt); err != nil || event.ReasonCode == "" ||
			event.SinkID == "" || event.Method == "" || event.HostTemplate == "" ||
			event.PathTemplate == "" || event.Backend == "" || event.Protocol == "" || event.EnforcementState == "" {
			t.Fatalf("1C Guard 事件字段不完整：%+v", event)
		}
		actualCounts[event.ReasonCode]++
		switch event.ReasonCode {
		case string(ReasonMissingFinalizationToken), string(ReasonWrongExecutor):
			if event.EnforcementState != string(SinkStateLegacyObserve) {
				t.Fatalf("已迁移 Sink 出现执行器身份违规：%+v", event)
			}
		}
	}
	for reason, total := range guardLog.FinalScan.ReasonCounts {
		actualCounts[reason] += total
	}
	if len(actualCounts) != len(wantCounts) {
		t.Fatalf("1C Guard 事件计数键集合不一致：got=%v want=%v", actualCounts, wantCounts)
	}
	for reason, want := range wantCounts {
		if actualCounts[reason] != want {
			t.Fatalf("1C Guard 事件计数不一致：%s=%d want=%d", reason, actualCounts[reason], want)
		}
	}
	scanStartedAt, err := time.Parse(time.RFC3339, guardLog.FinalScan.StartedAt)
	if err != nil {
		t.Fatal(err)
	}
	scanEndedAt, err := time.Parse(time.RFC3339, guardLog.FinalScan.EndedAt)
	if err != nil {
		t.Fatal(err)
	}
	if scanEndedAt.Sub(scanStartedAt) < 10*time.Minute || guardLog.FinalScan.PanicFatalCount != 0 {
		t.Fatalf("1C 最终稳定扫描窗口不足或出现 panic/fatal：%+v", guardLog.FinalScan)
	}
}

func validChangeset1CSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
