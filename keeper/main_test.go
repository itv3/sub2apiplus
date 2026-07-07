package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
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

func TestRunTargetUpdatesMatchingSessionIDWhenAnotherRunIsQueued(t *testing.T) {
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

	var req persistentExecution
	select {
	case req = <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("persistent executor did not start")
	}
	if req.SessionID == "" {
		t.Fatal("session id is empty")
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

type testPersistentResult struct {
	result persistentExecutionResult
	err    error
}

type testPersistentExecutor struct {
	started chan persistentExecution
	release chan testPersistentResult
}

func (e *testPersistentExecutor) Ensure(context.Context, persistentExecution) error {
	return nil
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
