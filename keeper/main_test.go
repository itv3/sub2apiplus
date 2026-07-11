package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestWithAuthRequiresTokenWhenWebCredentialsEmpty(t *testing.T) {
	k := &Keeper{cfg: Config{Sub2APIPlus: Sub2APIPlusConfig{InternalToken: "secret"}}}
	handler := k.withAuth(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	handler(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("without auth status = %d, want %d", rec.Code, http.StatusUnauthorized)
	}

	req = httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("x-api-key", "secret")
	rec = httptest.NewRecorder()
	handler(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("token auth status = %d, want %d", rec.Code, http.StatusNoContent)
	}
}

func TestWithAuthAcceptsConfiguredBasicAuth(t *testing.T) {
	k := &Keeper{cfg: Config{Web: WebConfig{Username: "admin", Password: "pass"}}}
	handler := k.withAuth(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.SetBasicAuth("admin", "pass")
	rec := httptest.NewRecorder()
	handler(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("basic auth status = %d, want %d", rec.Code, http.StatusNoContent)
	}
}

func TestWorkspacePathStaysInsideProjectsRoot(t *testing.T) {
	k := &Keeper{cfg: Config{ProjectsRoot: "/workspace/projects"}}

	if got := k.workspacePath("homeproxy"); got != "/workspace/projects/homeproxy" {
		t.Fatalf("workspacePath(homeproxy) = %q", got)
	}
	for _, value := range []string{"/tmp/homeproxy", "../homeproxy", "team/homeproxy", "."} {
		if got := k.workspacePath(value); got != "" {
			t.Fatalf("workspacePath(%q) = %q, want empty", value, got)
		}
	}
}

func TestTargetStateUsesAccountIDAndMigratesLegacyNameKey(t *testing.T) {
	k := &Keeper{
		state: State{Targets: map[string]*TargetState{
			"shared": {Name: "shared", AccountID: 1, Running: true},
		}},
	}

	first := k.targetStateLocked(TargetConfig{ID: "1", Name: "shared", AccountID: 1, Model: "gpt-5.5"})
	second := k.targetStateLocked(TargetConfig{ID: "2", Name: "shared", AccountID: 2, Model: "gpt-5.5"})

	if first == second {
		t.Fatal("same-name targets shared one state")
	}
	if _, ok := k.state.Targets["shared"]; ok {
		t.Fatal("legacy name key was not migrated")
	}
	if got := k.state.Targets["1"]; got != first || !got.Running {
		t.Fatal("legacy state was not preserved under account id")
	}
	if got := k.state.Targets["2"]; got != second {
		t.Fatal("second target was not stored under its account id")
	}
}

func TestSnapshotOverviewIncludesLastStatusAndLastError(t *testing.T) {
	startedAt := time.Date(2026, time.July, 9, 12, 34, 44, 0, time.UTC)
	completedAt := startedAt.Add(21 * time.Second)
	k := &Keeper{
		cfg: Config{
			ProjectsRoot: "/workspace/projects",
			Targets: []TargetConfig{{
				ID:          "3",
				Name:        "BWG_OpenAI",
				AccountID:   3,
				Platform:    "openai",
				AccountType: "apikey",
				Executor:    "codex",
				Model:       "gpt-5.5",
				Enabled:     true,
			}},
		},
		state: State{Targets: map[string]*TargetState{
			"3": {
				Name:                   "BWG_OpenAI",
				AccountID:              3,
				Model:                  "gpt-5.5",
				Enabled:                true,
				LastKeepaliveStartedAt: startedAt,
				ConsecutiveFailures:    2,
				LastStatus:             "error",
				LastMessageSummary:     "当前命令环境启动沙箱时报 Permission denied",
				LastError:              "当前命令环境启动沙箱时报 Permission denied",
				Sessions: []Session{{
					ID:          "20260709T123444.452416264",
					AccountID:   3,
					TargetName:  "BWG_OpenAI",
					Model:       "gpt-5.5",
					Status:      "error",
					Summary:     "当前命令环境启动沙箱时报 Permission denied",
					Error:       "当前命令环境启动沙箱时报 Permission denied",
					StartedAt:   startedAt,
					CompletedAt: completedAt,
				}},
			},
		}},
		location: time.UTC,
	}

	k.mu.Lock()
	snapshot := k.snapshotLocked()
	k.mu.Unlock()

	raw, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatalf("marshal snapshot: %v", err)
	}

	var payload struct {
		Overview []struct {
			AccountID     int64  `json:"account_id"`
			LastStatus    string `json:"last_status"`
			LastError     string `json:"last_error"`
			StatusDetail  string `json:"status_detail"`
			CurrentStatus string `json:"current_status"`
		} `json:"overview"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("unmarshal snapshot: %v", err)
	}
	if len(payload.Overview) != 1 {
		t.Fatalf("overview rows = %d, want 1", len(payload.Overview))
	}
	row := payload.Overview[0]
	if row.AccountID != 3 {
		t.Fatalf("AccountID = %d, want 3", row.AccountID)
	}
	if row.LastStatus != "error" {
		t.Fatalf("LastStatus = %q, want error", row.LastStatus)
	}
	if row.LastError != "当前命令环境启动沙箱时报 Permission denied" {
		t.Fatalf("LastError = %q", row.LastError)
	}
	if row.StatusDetail != row.LastError {
		t.Fatalf("StatusDetail = %q, want match LastError", row.StatusDetail)
	}
	if row.CurrentStatus != "异常" {
		t.Fatalf("CurrentStatus = %q, want 异常", row.CurrentStatus)
	}
}

func TestSnapshotSessionsOmitLargeProcessFieldsWithoutMutatingState(t *testing.T) {
	startedAt := time.Date(2026, time.July, 11, 10, 20, 30, 0, time.UTC)
	statePath := filepath.Join(t.TempDir(), "state.json")
	originalSession := Session{
		ID:              "session-1",
		TargetName:      "account-1",
		AccountID:       1,
		AccountName:     "OpenAI Account",
		Platform:        "openai",
		AccountType:     "apikey",
		Model:           "gpt-5.5",
		Mode:            "fresh",
		Prompt:          "请分析当前项目。",
		ReplyText:       "分析完成。",
		ExitCode:        1,
		Command:         []string{"codex", "exec", "--json"},
		Summary:         "执行失败摘要",
		CommandText:     "codex exec --json",
		WorkDir:         "/workspace/projects/demo",
		Stdout:          strings.Repeat("stdout", 1024),
		Stderr:          strings.Repeat("stderr", 1024),
		StdoutPath:      "/app/data/logs/session-1.stdout.log",
		StderrPath:      "/app/data/logs/session-1.stderr.log",
		LastMessagePath: "/app/data/sessions/session-1.last.txt",
		Status:          "error",
		Error:           "上游请求失败",
		Usage: Usage{
			InputTokens:  120,
			OutputTokens: 30,
			TotalTokens:  150,
		},
		Billing: Billing{
			Available:      true,
			RateMultiplier: 1,
			ActualCost:     0.0123,
			PricingSource:  "test",
		},
		StartedAt:         startedAt,
		CompletedAt:       startedAt.Add(5 * time.Second),
		LatencyMS:         5000,
		UpstreamRequestID: "request-1",
	}
	k := &Keeper{
		cfg: Config{StatePath: statePath},
		state: State{Targets: map[string]*TargetState{
			"1": {
				Name:      "account-1",
				AccountID: 1,
				Sessions:  []Session{originalSession},
			},
		}},
		location: time.UTC,
	}

	k.mu.Lock()
	snapshot := k.snapshotLocked().(map[string]any)
	k.mu.Unlock()

	targets := snapshot["targets"].([]*TargetState)
	if len(targets) != 1 {
		t.Fatalf("snapshot targets = %d, want 1", len(targets))
	}
	if len(targets[0].Sessions) != 1 {
		t.Fatalf("snapshot sessions = %d, want 1", len(targets[0].Sessions))
	}
	snapshotSession := targets[0].Sessions[0]
	if snapshotSession.Stdout != "" || snapshotSession.Stderr != "" || snapshotSession.Command != nil || snapshotSession.CommandText != "" {
		t.Fatalf("snapshot retained large process fields: stdout=%d stderr=%d command=%v command_text=%q", len(snapshotSession.Stdout), len(snapshotSession.Stderr), snapshotSession.Command, snapshotSession.CommandText)
	}
	if snapshotSession.Prompt != originalSession.Prompt || snapshotSession.ReplyText != originalSession.ReplyText || snapshotSession.Summary != originalSession.Summary || snapshotSession.Error != originalSession.Error {
		t.Fatal("snapshot lost prompt, reply, summary, or error fields")
	}
	if snapshotSession.Usage != originalSession.Usage || snapshotSession.Billing != originalSession.Billing {
		t.Fatal("snapshot lost usage or billing fields")
	}
	if snapshotSession.StdoutPath != originalSession.StdoutPath || snapshotSession.StderrPath != originalSession.StderrPath || snapshotSession.LastMessagePath != originalSession.LastMessagePath || snapshotSession.WorkDir != originalSession.WorkDir {
		t.Fatal("snapshot lost retained path or workspace fields")
	}

	stateSession := k.state.Targets["1"].Sessions[0]
	if stateSession.Stdout != originalSession.Stdout || stateSession.Stderr != originalSession.Stderr || stateSession.CommandText != originalSession.CommandText {
		t.Fatal("snapshot creation mutated persisted state text fields")
	}
	if len(stateSession.Command) != len(originalSession.Command) {
		t.Fatalf("persisted command length = %d, want %d", len(stateSession.Command), len(originalSession.Command))
	}
	for i := range originalSession.Command {
		if stateSession.Command[i] != originalSession.Command[i] {
			t.Fatalf("persisted command[%d] = %q, want %q", i, stateSession.Command[i], originalSession.Command[i])
		}
	}

	k.mu.Lock()
	k.saveStateLocked()
	k.mu.Unlock()
	raw, err := os.ReadFile(statePath)
	if err != nil {
		t.Fatalf("read persisted state: %v", err)
	}
	var persistedState State
	if err := json.Unmarshal(raw, &persistedState); err != nil {
		t.Fatalf("unmarshal persisted state: %v", err)
	}
	persistedSession := persistedState.Targets["1"].Sessions[0]
	if persistedSession.Stdout != originalSession.Stdout || persistedSession.Stderr != originalSession.Stderr || persistedSession.CommandText != originalSession.CommandText {
		t.Fatal("persisted state lost large process text fields")
	}
	if len(persistedSession.Command) != len(originalSession.Command) {
		t.Fatalf("persisted state command length = %d, want %d", len(persistedSession.Command), len(originalSession.Command))
	}
	for i := range originalSession.Command {
		if persistedSession.Command[i] != originalSession.Command[i] {
			t.Fatalf("persisted state command[%d] = %q, want %q", i, persistedSession.Command[i], originalSession.Command[i])
		}
	}
}

func TestPrepareCodexLaunchPathCopiesReleaseIntoRuntime(t *testing.T) {
	srcRoot := t.TempDir()
	releaseDir := filepath.Join(srcRoot, "standalone", "0.142.5-aarch64-unknown-linux-musl")
	if err := os.MkdirAll(filepath.Join(releaseDir, "bin"), 0755); err != nil {
		t.Fatalf("mkdir bin: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(releaseDir, "codex-resources"), 0755); err != nil {
		t.Fatalf("mkdir resources: %v", err)
	}
	if err := os.WriteFile(filepath.Join(releaseDir, "bin", "codex"), []byte("#!/bin/sh\necho ok\n"), 0755); err != nil {
		t.Fatalf("write codex: %v", err)
	}
	if err := os.WriteFile(filepath.Join(releaseDir, "codex-resources", "bwrap"), []byte("bwrap"), 0755); err != nil {
		t.Fatalf("write bwrap: %v", err)
	}
	if err := os.WriteFile(filepath.Join(releaseDir, "codex-package.json"), []byte("{}"), 0644); err != nil {
		t.Fatalf("write package json: %v", err)
	}
	if err := os.Symlink("bin/codex", filepath.Join(releaseDir, "codex")); err != nil {
		t.Fatalf("symlink root codex: %v", err)
	}

	layout := runtimeLayout{
		CodexHome: filepath.Join(t.TempDir(), "worker", ".codex"),
	}
	got, err := prepareCodexLaunchPath(filepath.Join(releaseDir, "bin", "codex"), layout)
	if err != nil {
		t.Fatalf("prepareCodexLaunchPath: %v", err)
	}

	want := filepath.Join(layout.CodexHome, "standalone", filepath.Base(releaseDir), "bin", "codex")
	if got != want {
		t.Fatalf("launch path = %q, want %q", got, want)
	}
	if _, err := os.Stat(got); err != nil {
		t.Fatalf("stat copied codex: %v", err)
	}
	releaseInfo, err := os.Stat(filepath.Dir(filepath.Dir(got)))
	if err != nil {
		t.Fatalf("stat copied release dir: %v", err)
	}
	if gotPerm := releaseInfo.Mode().Perm(); gotPerm != 0755 {
		t.Fatalf("copied release dir perm = %o, want 755", gotPerm)
	}
	if err := os.Chmod(filepath.Dir(filepath.Dir(got)), 0700); err != nil {
		t.Fatalf("chmod copied release dir: %v", err)
	}
	got, err = prepareCodexLaunchPath(filepath.Join(releaseDir, "bin", "codex"), layout)
	if err != nil {
		t.Fatalf("prepareCodexLaunchPath second call: %v", err)
	}
	releaseInfo, err = os.Stat(filepath.Dir(filepath.Dir(got)))
	if err != nil {
		t.Fatalf("restat copied release dir: %v", err)
	}
	if gotPerm := releaseInfo.Mode().Perm(); gotPerm != 0755 {
		t.Fatalf("repaired release dir perm = %o, want 755", gotPerm)
	}
	linkTarget, err := os.Readlink(filepath.Join(filepath.Dir(filepath.Dir(got)), "codex"))
	if err != nil {
		t.Fatalf("read copied symlink: %v", err)
	}
	if linkTarget != "bin/codex" {
		t.Fatalf("copied symlink target = %q, want bin/codex", linkTarget)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(filepath.Dir(got)), "codex-resources", "bwrap")); err != nil {
		t.Fatalf("stat copied bwrap: %v", err)
	}
}

func TestRepairCodexArg0PermissionsSets755(t *testing.T) {
	layout := runtimeLayout{
		CodexHome: filepath.Join(t.TempDir(), "worker", ".codex"),
	}
	arg0Dir := filepath.Join(layout.CodexHome, "tmp", "arg0")
	if err := os.MkdirAll(arg0Dir, 0700); err != nil {
		t.Fatalf("mkdir arg0: %v", err)
	}
	if err := os.Chmod(arg0Dir, 0700); err != nil {
		t.Fatalf("chmod arg0: %v", err)
	}
	if err := repairCodexArg0Permissions(layout); err != nil {
		t.Fatalf("repairCodexArg0Permissions: %v", err)
	}
	info, err := os.Stat(arg0Dir)
	if err != nil {
		t.Fatalf("stat arg0: %v", err)
	}
	if got := info.Mode().Perm(); got != 0755 {
		t.Fatalf("arg0 perm = %o, want 755", got)
	}
}

func TestTargetStateMergesCompatibleLegacyNameKeyWhenCanonicalExists(t *testing.T) {
	olderStarted := time.Date(2026, 7, 7, 23, 49, 47, 0, time.UTC)
	olderCompleted := time.Date(2026, 7, 7, 23, 52, 46, 0, time.UTC)
	newerStarted := time.Date(2026, 7, 8, 5, 55, 48, 0, time.UTC)
	newerCompleted := time.Date(2026, 7, 8, 5, 58, 47, 0, time.UTC)
	k := &Keeper{
		state: State{Targets: map[string]*TargetState{
			"8": {
				Name:                    "anyrouter_Anthropic_czs",
				AccountID:               8,
				LastStatus:              "error",
				LastError:               "new error",
				LastKeepaliveReceivedAt: newerCompleted,
				Sessions: []Session{{
					ID:          "new",
					Status:      "error",
					StartedAt:   newerStarted,
					CompletedAt: newerCompleted,
					Error:       "new error",
				}},
			},
			"anyrouter_Anthropic_czs": {
				Name:                    "anyrouter_Anthropic_czs",
				AccountID:               8,
				LastStatus:              "error",
				LastError:               "old error",
				LastKeepaliveReceivedAt: olderCompleted,
				Sessions: []Session{{
					ID:          "old",
					Status:      "error",
					StartedAt:   olderStarted,
					CompletedAt: olderCompleted,
					Error:       "old error",
				}},
			},
		}},
	}

	state := k.targetStateLocked(TargetConfig{ID: "8", Name: "anyrouter_Anthropic_czs", AccountID: 8, Model: "claude-opus-4-8[1m]"})

	if _, ok := k.state.Targets["anyrouter_Anthropic_czs"]; ok {
		t.Fatal("compatible legacy name key was not removed")
	}
	if got := k.state.Targets["8"]; got != state {
		t.Fatal("canonical account id state was not preserved")
	}
	if len(state.Sessions) != 2 {
		t.Fatalf("merged sessions = %d, want 2", len(state.Sessions))
	}
	if state.Sessions[0].ID != "old" || state.Sessions[1].ID != "new" {
		t.Fatalf("merged sessions order = [%s %s], want [old new]", state.Sessions[0].ID, state.Sessions[1].ID)
	}
	if state.LastError != "new error" {
		t.Fatalf("last error = %q, want newest canonical error", state.LastError)
	}
}

func TestTargetStateDoesNotMergeLegacyNameKeyForDifferentAccountID(t *testing.T) {
	k := &Keeper{
		state: State{Targets: map[string]*TargetState{
			"8":      {Name: "shared", AccountID: 8},
			"shared": {Name: "shared", AccountID: 9, Running: true},
		}},
	}

	state := k.targetStateLocked(TargetConfig{ID: "8", Name: "shared", AccountID: 8, Model: "claude-opus-4-8[1m]"})

	if state.Running {
		t.Fatal("different-account legacy state was merged into canonical state")
	}
	if _, ok := k.state.Targets["shared"]; !ok {
		t.Fatal("different-account legacy name key was removed")
	}
}

func TestPrepareRuntimeUsesTargetIDForWorkerDir(t *testing.T) {
	k := &Keeper{cfg: Config{RuntimeRoot: t.TempDir()}}

	first, err := k.prepareRuntime(TargetConfig{ID: "1", Name: "shared", AccountID: 1})
	if err != nil {
		t.Fatalf("prepareRuntime(first) error = %v", err)
	}
	second, err := k.prepareRuntime(TargetConfig{ID: "2", Name: "shared", AccountID: 2})
	if err != nil {
		t.Fatalf("prepareRuntime(second) error = %v", err)
	}

	if first.RootDir == second.RootDir {
		t.Fatal("same-name targets shared one runtime root")
	}
	if got := filepath.Base(first.RootDir); got != "1" {
		t.Fatalf("first runtime dir = %q, want account id 1", got)
	}
	if got := filepath.Base(second.RootDir); got != "2" {
		t.Fatalf("second runtime dir = %q, want account id 2", got)
	}
}

func TestTargetMatchesSelectorAcceptsIDAccountIDAndName(t *testing.T) {
	target := TargetConfig{ID: "target-42", AccountID: 42, Name: "keeper-openai"}
	for _, selector := range []string{"target-42", "42", "keeper-openai"} {
		if !targetMatchesSelector(target, selector) {
			t.Fatalf("targetMatchesSelector(%q) = false", selector)
		}
	}
	if targetMatchesSelector(target, "other") {
		t.Fatal("targetMatchesSelector(other) = true")
	}
}

func TestHandleManualRunRequiresPost(t *testing.T) {
	k := &Keeper{}
	req := httptest.NewRequest(http.MethodGet, "/api/run?target=keeper-openai", nil)
	rec := httptest.NewRecorder()

	k.handleManualRun(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusMethodNotAllowed)
	}
}

func TestParseClaudeStreamLineExtractsReplyUsageAndResult(t *testing.T) {
	message := parseClaudeStreamLine(`{"type":"assistant","message":{"content":[{"type":"text","text":" hello "}],"usage":{"input_tokens":2,"cache_read_input_tokens":3,"cache_creation_input_tokens":5,"output_tokens":7}}}`)
	if message.reply != "hello" {
		t.Fatalf("reply = %q, want hello", message.reply)
	}
	if message.usage == nil || message.usage.InputTokens != 10 || message.usage.OutputTokens != 7 {
		t.Fatalf("usage = %+v, want input 10 output 7", message.usage)
	}

	result := parseClaudeStreamLine(`{"type":"result","subtype":"success","result":" done "}`)
	if !result.done || result.isError || result.reply != "done" {
		t.Fatalf("result = %+v, want done success", result)
	}
}

func TestParseCodexItemCompletedExtractsAgentMessage(t *testing.T) {
	msg := parseCodexItemCompleted([]byte(`{"turnId":"turn-1","item":{"type":"agentMessage","text":" hello ","phase":"final"}}`))
	if msg.TurnID != "turn-1" || msg.Text != "hello" || msg.Phase != "final" {
		t.Fatalf("agent message = %+v", msg)
	}

	ignored := parseCodexItemCompleted([]byte(`{"turnId":"turn-2","item":{"type":"reasoning","text":"hidden"}}`))
	if ignored.TurnID != "turn-2" || ignored.Text != "" {
		t.Fatalf("ignored message = %+v", ignored)
	}
}

func TestIsRetryableCodexPersistentError(t *testing.T) {
	if !isRetryableCodexPersistentError(io.EOF) {
		t.Fatal("io.EOF should be retryable")
	}
	if !isRetryableCodexPersistentError(errors.New("failed to read frame header: closed")) {
		t.Fatal("frame read error should be retryable")
	}
	if isRetryableCodexPersistentError(errors.New("model not found")) {
		t.Fatal("business error should not be retryable")
	}
}

func TestScanPreservesRunningStateBeforeNextKeepalive(t *testing.T) {
	now := time.Now().UTC()
	target := TargetConfig{
		Name:            "BWG_OpenAI",
		AccountID:       3,
		Enabled:         true,
		NextKeepaliveAt: now.Add(10 * time.Minute),
		WorkStart:       "00:00",
		WorkEnd:         "24:00",
	}
	k := &Keeper{
		cfg: Config{
			StatePath: filepath.Join(t.TempDir(), "state.json"),
			Targets:   []TargetConfig{target},
		},
		state: State{Targets: map[string]*TargetState{
			"3": {
				Name:    target.Name,
				Running: true,
				Sessions: []Session{{
					ID:        "running-session",
					Status:    "running",
					StartedAt: now,
				}},
			},
		}},
		location: time.UTC,
	}

	k.scan(context.Background())

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	if !state.Running {
		t.Fatal("scan cleared an active running target before next keepalive")
	}
	if len(state.Sessions) != 1 || state.Sessions[0].Status != "running" {
		t.Fatalf("sessions after scan = %+v, want one running session", state.Sessions)
	}
}

func TestFetchTargetsUsesConfiguredMaxOutputTokens(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("x-api-key"); got != "secret" {
			t.Fatalf("x-api-key = %q, want secret", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"accounts":[{"id":3,"name":"BWG_OpenAI","platform":"openai","type":"apikey","enabled":true,"executor":"codex","model":"gpt-5.5","mode":"fresh","workspace":"homeproxy","interval_minutes":8,"work_start":"00:00","work_end":"24:00","max_output_tokens":256,"proxy_token":"proxy-openai"},{"id":4,"name":"BWG_Anthropic","platform":"anthropic","type":"apikey","enabled":true,"executor":"claude","model":"claude-opus-4-8","mode":"fresh","workspace":"homeproxy","interval_minutes":8,"work_start":"00:00","work_end":"24:00","proxy_token":"proxy-anthropic"}]}}`))
	}))
	defer server.Close()

	k := &Keeper{
		cfg: Config{
			ProjectsRoot: t.TempDir(),
			Sub2APIPlus:  Sub2APIPlusConfig{BaseURL: server.URL, InternalToken: "secret"},
		},
		httpClient: server.Client(),
	}

	targets, err := k.fetchTargets(context.Background())
	if err != nil {
		t.Fatalf("fetchTargets error = %v", err)
	}
	if len(targets) != 2 {
		t.Fatalf("targets len = %d, want 2", len(targets))
	}
	if got := targets[0].MaxOutputTokens; got != 256 {
		t.Fatalf("configured MaxOutputTokens = %d, want 256", got)
	}
	if got := targets[0].APIKey; got != "proxy-openai" {
		t.Fatalf("openai target APIKey = %q, want proxy token", got)
	}
	if got := targets[1].MaxOutputTokens; got != defaultKeepaliveMaxTokens {
		t.Fatalf("default MaxOutputTokens = %d, want %d", got, defaultKeepaliveMaxTokens)
	}
	if got := targets[1].APIKey; got != "proxy-anthropic" {
		t.Fatalf("anthropic target APIKey = %q, want proxy token", got)
	}
}

func TestFetchTargetsSkipsInvalidWorkspace(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("x-api-key"); got != "secret" {
			t.Fatalf("x-api-key = %q, want secret", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"accounts":[{"id":3,"name":"valid","platform":"openai","type":"apikey","enabled":true,"executor":"codex","model":"gpt-5.5","mode":"fresh","workspace":"homeproxy","interval_minutes":8,"work_start":"00:00","work_end":"24:00","proxy_token":"proxy-valid"},{"id":4,"name":"parent","platform":"openai","type":"apikey","enabled":true,"executor":"codex","model":"gpt-5.5","mode":"fresh","workspace":"../bad","interval_minutes":8,"work_start":"00:00","work_end":"24:00","proxy_token":"proxy-parent"},{"id":5,"name":"nested","platform":"anthropic","type":"apikey","enabled":true,"executor":"claude","model":"claude-opus-4-8","mode":"fresh","workspace":"team/homeproxy","interval_minutes":8,"work_start":"00:00","work_end":"24:00","proxy_token":"proxy-nested"}]}}`))
	}))
	defer server.Close()

	projectsRoot := t.TempDir()
	k := &Keeper{
		cfg: Config{
			ProjectsRoot: projectsRoot,
			Sub2APIPlus:  Sub2APIPlusConfig{BaseURL: server.URL, InternalToken: "secret"},
		},
		httpClient: server.Client(),
	}

	targets, err := k.fetchTargets(context.Background())
	if err != nil {
		t.Fatalf("fetchTargets error = %v", err)
	}
	if len(targets) != 1 {
		t.Fatalf("targets len = %d, want 1", len(targets))
	}
	if targets[0].AccountID != 3 {
		t.Fatalf("target account id = %d, want 3", targets[0].AccountID)
	}
	if got := targets[0].WorkspacePath; got != filepath.Join(projectsRoot, "homeproxy") {
		t.Fatalf("workspace path = %q, want valid project path", got)
	}
}

func TestRenderRuntimeEnvUsesScopedProxyToken(t *testing.T) {
	k := &Keeper{}
	target := TargetConfig{
		AccountID:     3,
		Name:          "BWG_OpenAI",
		Platform:      "openai",
		Executor:      "codex",
		BaseURL:       "http://sub2api/api/v1/internal/keeper/openai/accounts/3/v1",
		APIKey:        "proxy-token",
		Model:         "gpt-5.5",
		WorkspacePath: "/workspace/projects/homeproxy",
	}
	env := k.renderRuntimeEnv(target, runtimeLayout{CodexHome: "/app/data/runtime/workers/3/codex/.codex"})

	if !strings.Contains(env, "OPENAI_API_KEY='proxy-token'") {
		t.Fatalf("runtime env did not include scoped OpenAI proxy token:\n%s", env)
	}
	if strings.Contains(env, "secret") {
		t.Fatalf("runtime env leaked internal token:\n%s", env)
	}
	// codex executor 不应注入 CLAUDE_CODE_MAX_OUTPUT_TOKENS（该变量仅对 claude CLI 有意义）。
	if strings.Contains(env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS") {
		t.Fatalf("codex runtime env must not set CLAUDE_CODE_MAX_OUTPUT_TOKENS:\n%s", env)
	}
}

func TestRenderRuntimeEnvInjectsClaudeMaxOutputTokens(t *testing.T) {
	k := &Keeper{}
	target := TargetConfig{
		AccountID:       7,
		Name:            "BWG_Anthropic",
		Platform:        "anthropic",
		Executor:        "claude",
		BaseURL:         "http://sub2api/api/v1/internal/keeper/anthropic/accounts/7",
		APIKey:          "proxy-token",
		Model:           "claude-opus-4-8",
		WorkspacePath:   "/workspace/projects/homeproxy",
		MaxOutputTokens: 800,
	}
	env := k.renderRuntimeEnv(target, runtimeLayout{})
	if !strings.Contains(env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS='800'") {
		t.Fatalf("claude runtime env did not inject configured max output tokens:\n%s", env)
	}

	// MaxOutputTokens 未设置（0）时回退到默认值，避免注入 0 让 CLI 拿到非法上限。
	target.MaxOutputTokens = 0
	env = k.renderRuntimeEnv(target, runtimeLayout{})
	if !strings.Contains(env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS='"+strconv.Itoa(defaultKeepaliveMaxTokens)+"'") {
		t.Fatalf("claude runtime env did not fall back to default max output tokens:\n%s", env)
	}
}

func TestClaudePersistentArgsUsePlanModeAndDenyWriteTools(t *testing.T) {
	args := claudePersistentArgs(persistentExecution{
		Account: TargetConfig{
			Model: "claude-opus-4-8",
			Mode:  "fresh",
		},
	})
	joined := strings.Join(args, " ")

	if !strings.Contains(joined, "--permission-mode plan") {
		t.Fatalf("claude args = %q, want plan permission mode", joined)
	}
	if !strings.Contains(joined, "--disallowed-tools Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch") {
		t.Fatalf("claude args = %q, want dangerous tools denied", joined)
	}
	if !strings.Contains(joined, "--betas context-1m-2025-08-07") {
		t.Fatalf("claude args = %q, want default 1M context beta", joined)
	}
}

func TestClaudePersistentEnvScrubsSubprocessEnvironment(t *testing.T) {
	env := claudePersistentEnv(persistentExecution{
		Account: TargetConfig{
			APIKey:  "proxy-token",
			BaseURL: "http://sub2api/api/v1/internal/keeper/anthropic/accounts/4",
			Model:   "claude-opus-4-8",
		},
		Layout: runtimeLayout{
			HomeDir:         "/app/data/runtime/workers/4/home",
			ClaudeConfigDir: "/app/data/runtime/workers/4/claude",
		},
	})

	found := false
	foundBetas := false
	for _, value := range env {
		if value == "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1" {
			found = true
		}
		if value == "ANTHROPIC_BETAS=context-1m-2025-08-07" {
			foundBetas = true
		}
	}
	if !found {
		t.Fatalf("claude env = %#v, want subprocess env scrub enabled", env)
	}
	if !foundBetas {
		t.Fatalf("claude env = %#v, want default 1M context beta", env)
	}
}

func TestClaudeDefaultBetasAlwaysEnables1MContext(t *testing.T) {
	for _, model := range []string{"claude-opus-4-8", "claude-opus-4-8[1m]", ""} {
		betas := claudeDefaultBetas()
		if len(betas) != 1 || betas[0] != "context-1m-2025-08-07" {
			t.Fatalf("claudeDefaultBetas() for model %q = %#v, want 1M context beta", model, betas)
		}
	}
}

func TestKeeperClientEnvDoesNotInheritProcessSecrets(t *testing.T) {
	t.Setenv("SUB2APIPLUS_KEEPER_INTERNAL_TOKEN", "global-secret")
	t.Setenv("HTTP_PROXY", "http://proxy.example")
	t.Setenv("PATH", "/usr/bin")

	env := buildKeeperClientEnv("OPENAI_API_KEY=scoped-token")

	joined := strings.Join(env, "\n")
	if strings.Contains(joined, "SUB2APIPLUS_KEEPER_INTERNAL_TOKEN") || strings.Contains(joined, "global-secret") {
		t.Fatalf("client env leaked keeper internal token: %#v", env)
	}
	if strings.Contains(joined, "HTTP_PROXY") || strings.Contains(joined, "proxy.example") {
		t.Fatalf("client env inherited process proxy settings: %#v", env)
	}
	if !strings.Contains(joined, "OPENAI_API_KEY=scoped-token") {
		t.Fatalf("client env missing scoped token: %#v", env)
	}
}

func TestRunTargetUpdatesMatchingSessionIDWhenAnotherRunIsQueued(t *testing.T) {
	tempDir := t.TempDir()
	target := TargetConfig{
		Name:            "BWG_OpenAI",
		AccountID:       3,
		Platform:        "openai",
		AccountType:     "apikey",
		Model:           "gpt-5.5",
		Enabled:         true,
		Executor:        "codex",
		MaxOutputTokens: 512,
	}
	executor := &testPersistentExecutor{
		started: make(chan persistentExecution, 1),
		release: make(chan testPersistentResult, 1),
	}
	k := &Keeper{
		cfg: Config{
			RuntimeRoot: filepath.Join(tempDir, "runtime"),
			StatePath:   filepath.Join(tempDir, "state.json"),
		},
		state:               State{Targets: map[string]*TargetState{}},
		location:            time.UTC,
		httpClient:          &http.Client{Timeout: time.Millisecond},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory: func(*Keeper, TargetConfig) (accountPersistentExecutor, error) {
			return executor, nil
		},
	}

	done := make(chan struct{})
	go func() {
		k.runTarget(context.Background(), target)
		close(done)
	}()

	var req persistentExecution
	select {
	case req = <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	if req.SessionID == "" {
		t.Fatal("session id is empty")
	}
	if req.MaxOutputTokens != target.MaxOutputTokens {
		t.Fatalf("persistent MaxOutputTokens = %d, want %d", req.MaxOutputTokens, target.MaxOutputTokens)
	}

	k.appendSession(target, Session{
		ID:        "queued-session",
		Status:    "running",
		StartedAt: time.Now().UTC().Add(time.Second),
	})
	executor.release <- testPersistentResult{
		result: persistentExecutionResult{
			ReplyText: "finished",
			Summary:   "finished",
			Usage: &usageInfo{
				InputTokens:  1,
				OutputTokens: 2,
				TotalTokens:  3,
			},
		},
	}

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runTarget did not finish")
	}

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	finishedIndex := findSessionIndexByID(state.Sessions, req.SessionID)
	if finishedIndex < 0 {
		t.Fatalf("finished session %q missing from %+v", req.SessionID, state.Sessions)
	}
	if got := state.Sessions[finishedIndex].Status; got != "success" {
		t.Fatalf("finished session status = %q, want success", got)
	}
	queuedIndex := findSessionIndexByID(state.Sessions, "queued-session")
	if queuedIndex < 0 {
		t.Fatalf("queued session was overwritten: %+v", state.Sessions)
	}
	if got := state.Sessions[queuedIndex].Status; got != "running" {
		t.Fatalf("queued session status = %q, want running", got)
	}
	if !state.Running {
		t.Fatal("target Running should stay true while another session is running")
	}
}

func TestRunTargetNonFailureErrorDoesNotIncrementConsecutiveFailures(t *testing.T) {
	tempDir := t.TempDir()
	target := TargetConfig{
		Name:        "BWG_OpenAI",
		AccountID:   3,
		Platform:    "openai",
		AccountType: "apikey",
		Model:       "gpt-5.5",
		Enabled:     true,
		Executor:    "codex",
	}
	executor := &testPersistentExecutor{
		started: make(chan persistentExecution, 1),
		release: make(chan testPersistentResult, 1),
	}
	k := &Keeper{
		cfg: Config{
			RuntimeRoot: filepath.Join(tempDir, "runtime"),
			StatePath:   filepath.Join(tempDir, "state.json"),
		},
		state:               State{Targets: map[string]*TargetState{}},
		location:            time.UTC,
		httpClient:          &http.Client{Timeout: time.Millisecond},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory: func(*Keeper, TargetConfig) (accountPersistentExecutor, error) {
			return executor, nil
		},
	}

	done := make(chan struct{})
	go func() {
		k.runTarget(context.Background(), target)
		close(done)
	}()

	select {
	case <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	executor.release <- testPersistentResult{
		result: persistentExecutionResult{ErrorText: "connection reset", Summary: "connection reset"},
		err:    wrapNonFailureRunError(errors.New("connection reset")),
	}

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runTarget did not finish")
	}

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	if got := state.ConsecutiveFailures; got != 0 {
		t.Fatalf("ConsecutiveFailures = %d, want 0", got)
	}
	if len(state.Sessions) == 0 || state.Sessions[0].Status != "error" {
		t.Fatalf("session not recorded as error: %+v", state.Sessions)
	}
}

func TestRunTargetTreatsSandboxBlockedReplyAsError(t *testing.T) {
	tempDir := t.TempDir()
	target := TargetConfig{
		Name:            "BWG_OpenAI",
		AccountID:       3,
		Platform:        "openai",
		AccountType:     "apikey",
		Model:           "gpt-5.5",
		Enabled:         true,
		Executor:        "codex",
		IntervalMinutes: 8,
	}
	executor := &testPersistentExecutor{
		started: make(chan persistentExecution, 1),
		release: make(chan testPersistentResult, 1),
	}
	k := &Keeper{
		cfg: Config{
			RuntimeRoot: filepath.Join(tempDir, "runtime"),
			StatePath:   filepath.Join(tempDir, "state.json"),
		},
		state:               State{Targets: map[string]*TargetState{}},
		location:            time.UTC,
		httpClient:          &http.Client{Timeout: time.Millisecond},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory: func(*Keeper, TargetConfig) (accountPersistentExecutor, error) {
			return executor, nil
		},
	}

	done := make(chan struct{})
	go func() {
		k.runTarget(context.Background(), target)
		close(done)
	}()

	var req persistentExecution
	select {
	case req = <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	executor.release <- testPersistentResult{
		result: persistentExecutionResult{
			ReplyText: "无法读取 `keeper.example.yaml`：当前沙箱启动任何命令前报 `bwrap: loopback: Failed RTM_NEWADDR`。未修改代码。",
			Summary:   "无法读取 `keeper.example.yaml`：当前沙箱启动任何命令前报 `bwrap: loopback: Failed RTM_NEWADDR`。未修改代码。",
			Stderr:    "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces.",
		},
	}

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runTarget did not finish")
	}

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	index := findSessionIndexByID(state.Sessions, req.SessionID)
	if index < 0 {
		t.Fatalf("session %q missing from %+v", req.SessionID, state.Sessions)
	}
	if got := state.Sessions[index].Status; got != "error" {
		t.Fatalf("session status = %q, want error", got)
	}
	if !state.Sessions[index].LocalClientError {
		t.Fatalf("session LocalClientError = false, want true")
	}
	if got := state.LastStatus; got != "error" {
		t.Fatalf("LastStatus = %q, want error", got)
	}
	if got := state.ConsecutiveFailures; got != 1 {
		t.Fatalf("ConsecutiveFailures = %d, want 1", got)
	}
	if state.DailyKeepaliveCount != 0 {
		t.Fatalf("DailyKeepaliveCount = %d, want 0", state.DailyKeepaliveCount)
	}
}

func TestRunTargetTreatsReadonlyExecutorFailureReplyAsError(t *testing.T) {
	tempDir := t.TempDir()
	target := TargetConfig{
		Name:            "BWG_OpenAI",
		AccountID:       3,
		Platform:        "openai",
		AccountType:     "apikey",
		Model:           "gpt-5.5",
		Enabled:         true,
		Executor:        "codex",
		IntervalMinutes: 8,
	}
	executor := &testPersistentExecutor{
		started: make(chan persistentExecution, 1),
		release: make(chan testPersistentResult, 1),
	}
	k := &Keeper{
		cfg: Config{
			RuntimeRoot: filepath.Join(tempDir, "runtime"),
			StatePath:   filepath.Join(tempDir, "state.json"),
		},
		state:               State{Targets: map[string]*TargetState{}},
		location:            time.UTC,
		httpClient:          &http.Client{Timeout: time.Millisecond},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory: func(*Keeper, TargetConfig) (accountPersistentExecutor, error) {
			return executor, nil
		},
	}

	done := make(chan struct{})
	go func() {
		k.runTarget(context.Background(), target)
		close(done)
	}()

	var req persistentExecution
	select {
	case req = <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	executor.release <- testPersistentResult{
		result: persistentExecutionResult{
			ReplyText: "当前无法做基于代码的有效判断：本会话读取仓库的只读执行器失效。若必须先给一个常见风险，通常是 WebSocket/流式输出缺少背压与断连清理。",
			Summary:   "当前无法做基于代码的有效判断：本会话读取仓库的只读执行器失效。若必须先给一个常见风险，通常是 WebSocket/流式输出缺少背压与断连清理。",
			Stderr:    "codex app-server (WebSockets)",
		},
	}

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runTarget did not finish")
	}

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	index := findSessionIndexByID(state.Sessions, req.SessionID)
	if index < 0 {
		t.Fatalf("session %q missing from %+v", req.SessionID, state.Sessions)
	}
	if got := state.Sessions[index].Status; got != "error" {
		t.Fatalf("session status = %q, want error", got)
	}
	if !state.Sessions[index].LocalClientError {
		t.Fatalf("session LocalClientError = false, want true")
	}
}

func TestRunTargetTreatsReadonlyPermissionRefusalReplyAsError(t *testing.T) {
	tempDir := t.TempDir()
	target := TargetConfig{
		Name:            "BWG_OpenAI",
		AccountID:       3,
		Platform:        "openai",
		AccountType:     "apikey",
		Model:           "gpt-5.5",
		Enabled:         true,
		Executor:        "codex",
		IntervalMinutes: 8,
	}
	executor := &testPersistentExecutor{
		started: make(chan persistentExecution, 1),
		release: make(chan testPersistentResult, 1),
	}
	k := &Keeper{
		cfg: Config{
			RuntimeRoot: filepath.Join(tempDir, "runtime"),
			StatePath:   filepath.Join(tempDir, "state.json"),
		},
		state:               State{Targets: map[string]*TargetState{}},
		location:            time.UTC,
		httpClient:          &http.Client{Timeout: time.Millisecond},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory: func(*Keeper, TargetConfig) (accountPersistentExecutor, error) {
			return executor, nil
		},
	}

	done := make(chan struct{})
	go func() {
		k.runTarget(context.Background(), target)
		close(done)
	}()

	var req persistentExecution
	select {
	case req = <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	executor.release <- testPersistentResult{
		result: persistentExecutionResult{
			ReplyText: "我现在无法验证代码：当前只读环境的本地读取命令被沙箱拒绝，没法打开项目里的 API / `workspacePath` 实现。请贴出相关文件或放开读取权限，我再按你要求在 150 字内说明。",
			Summary:   "我现在无法验证代码：当前只读环境的本地读取命令被沙箱拒绝，没法打开项目里的 API / `workspacePath` 实现。请贴出相关文件或放开读取权限，我再按你要求在 150 字内说明。",
			Stderr:    "codex app-server (WebSockets)",
		},
	}

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runTarget did not finish")
	}

	state := k.state.Targets["3"]
	if state == nil {
		t.Fatal("target state missing")
	}
	index := findSessionIndexByID(state.Sessions, req.SessionID)
	if index < 0 {
		t.Fatalf("session %q missing from %+v", req.SessionID, state.Sessions)
	}
	if got := state.Sessions[index].Status; got != "error" {
		t.Fatalf("session status = %q, want error", got)
	}
	if !state.Sessions[index].LocalClientError {
		t.Fatalf("session LocalClientError = false, want true")
	}
}

func TestKeeperSessionLooksLikeLocalClientErrorIgnoresOrdinarySandboxDiscussion(t *testing.T) {
	session := Session{
		ReplyText: "Docker 部署约束里提到 keeper 容器需要沙箱相关能力，但想彻底关 Web，不能只靠留空 listen。",
		Summary:   "Docker 部署约束里提到 keeper 容器需要沙箱相关能力，但想彻底关 Web，不能只靠留空 listen。",
		Stderr:    "codex app-server (WebSockets)\n  listening on: ws://127.0.0.1:34519",
	}
	if keeperSessionLooksLikeLocalClientError(session) {
		t.Fatal("ordinary sandbox discussion should not be treated as local client error")
	}
}

func TestKeeperSessionLooksLikeLocalClientErrorIgnoresEnvironmentDependencyAnalysis(t *testing.T) {
	session := Session{
		ReplyText: "我只做了静态分析，没有改代码。\n\n运行环境强绑定 Linux/Docker：镜像基于 Debian，安装 bash、bubblewrap、procps 等；这更像部署约束，建议文档明确 Linux 容器限定。",
		Summary:   "我只做了静态分析，没有改代码。运行环境强绑定 Linux/Docker：镜像基于 Debian，安装 bash、bubblewrap、procps 等。",
		Stdout:    "Dockerfile:24:      bubblewrap \\",
		Stderr:    "codex app-server (WebSockets)\n  listening on: ws://127.0.0.1:34519",
	}
	if keeperSessionLooksLikeLocalClientError(session) {
		t.Fatal("environment dependency analysis should not be treated as local client error")
	}
}

func TestScanPipeLinesReportsDroppedWhenPendingLimitExceeded(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 5200; i++ {
		b.WriteString(strings.Repeat("x", 1024))
		b.WriteByte('\n')
	}

	lines := make(chan string, 1)
	done := make(chan struct{})
	go func() {
		scanPipeLines(strings.NewReader(b.String()), lines)
		close(done)
	}()

	time.Sleep(20 * time.Millisecond)
	sawDropMarker := false
	for {
		select {
		case line := <-lines:
			if strings.Contains(line, "已截断") {
				sawDropMarker = true
			}
		case <-done:
			for {
				select {
				case line := <-lines:
					if strings.Contains(line, "已截断") {
						sawDropMarker = true
					}
				default:
					if !sawDropMarker {
						t.Fatal("expected dropped-line marker")
					}
					return
				}
			}
		case <-time.After(2 * time.Second):
			t.Fatal("scanPipeLines did not finish")
		}
	}
}

type testPersistentResult struct {
	result persistentExecutionResult
	err    error
}

type testPersistentExecutor struct {
	started chan persistentExecution
	release chan testPersistentResult
}

func (e *testPersistentExecutor) Execute(ctx context.Context, req persistentExecution) (persistentExecutionResult, error) {
	e.started <- req
	select {
	case result := <-e.release:
		return result.result, result.err
	case <-ctx.Done():
		return persistentExecutionResult{ErrorText: ctx.Err().Error()}, ctx.Err()
	}
}

func (e *testPersistentExecutor) Close() error {
	return nil
}
