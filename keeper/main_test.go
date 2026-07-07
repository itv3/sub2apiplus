package main

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
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
