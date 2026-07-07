package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html/template"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

const version = "0.1.141-0.14"

const (
	defaultClientTimeoutSeconds = 2700
	defaultTimeoutSeconds       = defaultClientTimeoutSeconds
	minClaudeTimeoutSeconds     = 2700
	defaultMaxOutputBytes       = 1 << 20
	defaultKeepaliveMode        = "resume_last"
)

type Config struct {
	Timezone            string            `yaml:"timezone"`
	ScanIntervalSeconds int               `yaml:"scan_interval_seconds"`
	StatePath           string            `yaml:"state_path"`
	ProjectsRoot        string            `yaml:"projects_root"`
	RuntimeRoot         string            `yaml:"runtime_root"`
	PromptGuard         string            `yaml:"prompt_guard"`
	PromptBank          []PromptQuestion  `yaml:"prompt_bank"`
	Sub2APIPlus         Sub2APIPlusConfig `yaml:"sub2apiplus"`
	Web                 WebConfig         `yaml:"web"`
	Targets             []TargetConfig    `yaml:"targets"`
}

type Sub2APIPlusConfig struct {
	BaseURL        string `yaml:"base_url"`
	InternalToken  string `yaml:"internal_token"`
	TimeoutSeconds int    `yaml:"timeout_seconds"`
}

type WebConfig struct {
	Enabled  bool   `yaml:"enabled"`
	Listen   string `yaml:"listen"`
	Username string `yaml:"username"`
	Password string `yaml:"password"`
}

type PromptQuestion struct {
	ID          string `yaml:"id" json:"id"`
	Scope       string `yaml:"scope" json:"scope"`
	ProjectPath string `yaml:"project_path" json:"project_path"`
	Text        string `yaml:"text" json:"text"`
	Enabled     bool   `yaml:"enabled" json:"enabled"`
}

type TargetConfig struct {
	ID               string    `yaml:"id" json:"id"`
	Name             string    `yaml:"name" json:"name"`
	Enabled          bool      `yaml:"enabled" json:"enabled"`
	AccountID        int64     `yaml:"account_id" json:"account_id"`
	Platform         string    `yaml:"platform" json:"platform"`
	AccountType      string    `yaml:"account_type" json:"account_type"`
	Executor         string    `yaml:"executor" json:"executor"`
	ClientType       string    `yaml:"client_type" json:"client_type"`
	BaseURL          string    `yaml:"base_url" json:"base_url,omitempty"`
	APIKey           string    `yaml:"api_key" json:"-"`
	Model            string    `yaml:"model" json:"model"`
	IntervalMinutes  int       `yaml:"interval_minutes" json:"interval_minutes"`
	WorkStart        string    `yaml:"work_start" json:"work_start"`
	WorkEnd          string    `yaml:"work_end" json:"work_end"`
	WorkspacePath    string    `yaml:"workspace_path" json:"workspace_path"`
	PromptText       string    `yaml:"prompt_text" json:"prompt_text,omitempty"`
	Mode             string    `yaml:"mode" json:"mode"`
	TimeoutSeconds   int       `yaml:"timeout_seconds" json:"timeout_seconds"`
	MaxDailyRuns     int       `yaml:"max_daily_runs" json:"max_daily_runs,omitempty"`
	MaxOutputTokens  int       `yaml:"max_output_tokens" json:"max_output_tokens,omitempty"`
	PromptProfile    string    `yaml:"prompt_profile" json:"prompt_profile,omitempty"`
	InitialDelaySecs int       `yaml:"initial_delay_seconds" json:"initial_delay_seconds,omitempty"`
	Due              bool      `json:"due"`
	NextKeepaliveAt  time.Time `json:"next_keepalive_at,omitempty"`
}

type State struct {
	Version               string                  `json:"version"`
	ConfiguredPromptGuard *string                 `json:"configured_prompt_guard,omitempty"`
	ConfiguredPromptBank  *[]PromptQuestion       `json:"configured_prompt_bank,omitempty"`
	Targets               map[string]*TargetState `json:"targets"`
}

type TargetState struct {
	Name                    string    `json:"name"`
	AccountID               int64     `json:"account_id"`
	Model                   string    `json:"model"`
	Enabled                 bool      `json:"enabled"`
	LastKeepaliveStartedAt  time.Time `json:"last_keepalive_started_at,omitempty"`
	LastKeepaliveReceivedAt time.Time `json:"last_keepalive_received_at,omitempty"`
	NextRunAt               time.Time `json:"next_run_at,omitempty"`
	DailyDate               string    `json:"daily_date,omitempty"`
	DailyKeepaliveCount     int       `json:"daily_keepalive_count"`
	ConsecutiveFailures     int       `json:"consecutive_failures"`
	LastStatus              string    `json:"last_status,omitempty"`
	LastMessageSummary      string    `json:"last_message_summary,omitempty"`
	LastError               string    `json:"last_error,omitempty"`
	ClientStatus            string    `json:"client_status,omitempty"`
	ClientStatusDetail      string    `json:"client_status_detail,omitempty"`
	ClientConnectedAt       time.Time `json:"client_connected_at,omitempty"`
	Running                 bool      `json:"running"`
	Sessions                []Session `json:"sessions"`
}

type Session struct {
	ID                string    `json:"id"`
	TargetName        string    `json:"target_name"`
	AccountID         int64     `json:"account_id"`
	AccountName       string    `json:"account_name,omitempty"`
	Platform          string    `json:"platform,omitempty"`
	AccountType       string    `json:"account_type,omitempty"`
	Model             string    `json:"model"`
	Mode              string    `json:"mode,omitempty"`
	Prompt            string    `json:"prompt"`
	ReplyText         string    `json:"reply_text,omitempty"`
	ExitCode          int       `json:"exit_code,omitempty"`
	Command           []string  `json:"command,omitempty"`
	Summary           string    `json:"summary,omitempty"`
	CommandText       string    `json:"command_text,omitempty"`
	WorkDir           string    `json:"work_dir,omitempty"`
	Stdout            string    `json:"stdout,omitempty"`
	Stderr            string    `json:"stderr,omitempty"`
	StdoutPath        string    `json:"stdout_path,omitempty"`
	StderrPath        string    `json:"stderr_path,omitempty"`
	LastMessagePath   string    `json:"last_message_path,omitempty"`
	Status            string    `json:"status"`
	Error             string    `json:"error,omitempty"`
	Usage             Usage     `json:"usage"`
	Billing           Billing   `json:"billing"`
	StartedAt         time.Time `json:"started_at"`
	CompletedAt       time.Time `json:"completed_at,omitempty"`
	LatencyMS         int64     `json:"latency_ms"`
	UpstreamRequestID string    `json:"upstream_request_id,omitempty"`
}

type Usage struct {
	InputTokens              int `json:"input_tokens"`
	OutputTokens             int `json:"output_tokens"`
	TotalTokens              int `json:"total_tokens"`
	CacheCreationTokens      int `json:"cache_creation_tokens,omitempty"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens,omitempty"`
	CacheReadTokens          int `json:"cache_read_tokens,omitempty"`
	CacheCreation5mTokens    int `json:"cache_creation_5m_tokens,omitempty"`
	CacheCreation1hTokens    int `json:"cache_creation_1h_tokens,omitempty"`
}

type Billing struct {
	Available            bool    `json:"available"`
	RateMultiplier       float64 `json:"rate_multiplier"`
	InputCost            float64 `json:"input_cost,omitempty"`
	OutputCost           float64 `json:"output_cost,omitempty"`
	CacheCreationCost    float64 `json:"cache_creation_cost,omitempty"`
	CacheReadCost        float64 `json:"cache_read_cost,omitempty"`
	TotalCost            float64 `json:"total_cost,omitempty"`
	ActualCost           float64 `json:"actual_cost,omitempty"`
	BillingMode          string  `json:"billing_mode,omitempty"`
	PricingSource        string  `json:"pricing_source"`
	UnavailableReason    string  `json:"unavailable_reason,omitempty"`
	UnavailableReasonRaw string  `json:"unavailable_reason_raw,omitempty"`
}

type keepaliveRequest struct {
	Model                    string  `json:"model,omitempty"`
	Prompt                   string  `json:"prompt,omitempty"`
	MaxOutputTokens          int     `json:"max_output_tokens,omitempty"`
	Status                   string  `json:"status,omitempty"`
	SessionID                string  `json:"session_id,omitempty"`
	Summary                  string  `json:"summary,omitempty"`
	Error                    string  `json:"error,omitempty"`
	InputTokens              int64   `json:"input_tokens,omitempty"`
	OutputTokens             int64   `json:"output_tokens,omitempty"`
	CachedInputTokens        int64   `json:"cached_input_tokens,omitempty"`
	CacheCreationInputTokens int64   `json:"cache_creation_input_tokens,omitempty"`
	TotalTokens              int64   `json:"total_tokens,omitempty"`
	TotalCost                float64 `json:"total_cost,omitempty"`
	LocalClientError         bool    `json:"local_client_error,omitempty"`
}

type apiEnvelope struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

type keeperAccountsData struct {
	Accounts []keeperAccountConfig `json:"accounts"`
}

type keeperAccountConfig struct {
	ID              int64      `json:"id"`
	Name            string     `json:"name"`
	Platform        string     `json:"platform"`
	Type            string     `json:"type"`
	Enabled         bool       `json:"enabled"`
	Executor        string     `json:"executor"`
	Model           string     `json:"model"`
	Mode            string     `json:"mode"`
	Workspace       string     `json:"workspace"`
	Prompt          string     `json:"prompt"`
	IntervalMinutes int        `json:"interval_minutes"`
	WorkStart       string     `json:"work_start"`
	WorkEnd         string     `json:"work_end"`
	Due             bool       `json:"due"`
	NextKeepaliveAt *time.Time `json:"next_keepalive_at"`
}

type settingsRequest struct {
	PromptGuard *string           `json:"prompt_guard,omitempty"`
	PromptBank  *[]PromptQuestion `json:"prompt_bank,omitempty"`
}

type recordKeepaliveResponse struct {
	Billing *Billing `json:"billing,omitempty"`
}

type Keeper struct {
	cfg                 Config
	location            *time.Location
	httpClient          *http.Client
	mu                  sync.Mutex
	state               State
	persistentMu        sync.Mutex
	persistentExecutors map[string]*managedPersistentExecutor
	persistentFactory   persistentExecutorFactory
}

type runtimeLayout struct {
	RootDir         string
	LogDir          string
	SessionDir      string
	HomeDir         string
	CodexHome       string
	ClaudeConfigDir string
	RuntimeEnvPath  string
	LastMessagePath string
}

type usageInfo struct {
	InputTokens              int64
	CachedInputTokens        int64
	CacheCreationInputTokens int64
	OutputTokens             int64
	ReasoningOutputTokens    int64
	TotalTokens              int64
	UncachedInputTokens      int64
}

type dashboardStats struct {
	TotalTargets   int       `json:"total_targets"`
	EnabledTargets int       `json:"enabled_targets"`
	TodaySuccesses int       `json:"today_successes"`
	TodayFailures  int       `json:"today_failures"`
	RunningCount   int       `json:"running_count"`
	LastSuccessAt  time.Time `json:"last_success_at,omitempty"`
	LastFailureAt  time.Time `json:"last_failure_at,omitempty"`
}

type usageCostSummary struct {
	TotalTokens int     `json:"total_tokens"`
	Currency    string  `json:"currency,omitempty"`
	TotalCost   float64 `json:"total_cost"`
	HasCost     bool    `json:"has_cost"`
	Precise     bool    `json:"precise"`
}

type overviewRow struct {
	Name                string            `json:"name"`
	AccountID           int64             `json:"account_id"`
	Platform            string            `json:"platform"`
	AccountType         string            `json:"account_type"`
	Executor            string            `json:"executor"`
	Model               string            `json:"model"`
	Enabled             bool              `json:"enabled"`
	Running             bool              `json:"running"`
	CurrentStatus       string            `json:"current_status"`
	StatusClass         string            `json:"status_class"`
	StatusDetail        string            `json:"status_detail,omitempty"`
	LastMessageSummary  string            `json:"last_message_summary,omitempty"`
	ConsecutiveFailures int               `json:"consecutive_failures"`
	ExecutionCount      int               `json:"execution_count"`
	SuccessCount        int               `json:"success_count"`
	FailureCount        int               `json:"failure_count"`
	LastStartedAt       time.Time         `json:"last_started_at,omitempty"`
	LastFinishedAt      time.Time         `json:"last_finished_at,omitempty"`
	NextRunAt           time.Time         `json:"next_run_at,omitempty"`
	Usage24hCost        *usageCostSummary `json:"usage_24h_cost,omitempty"`
	TotalUsageCost      *usageCostSummary `json:"total_usage_cost,omitempty"`
}

func main() {
	configPath := flag.String("config", getenvDefault("KEEPER_CONFIG", "keeper.yaml"), "keeper config path")
	showVersion := flag.Bool("version", false, "show version")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("加载配置失败: %v", err)
	}
	keeper, err := NewKeeper(cfg)
	if err != nil {
		log.Fatalf("初始化失败: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if cfg.Web.Enabled {
		go func() {
			if err := keeper.serve(ctx); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Fatalf("网页服务退出: %v", err)
			}
		}()
	}
	keeper.Run(ctx)
}

func loadConfig(path string) (Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	expanded := os.ExpandEnv(string(raw))
	var cfg Config
	if err := yaml.Unmarshal([]byte(expanded), &cfg); err != nil {
		if jsonErr := json.Unmarshal([]byte(expanded), &cfg); jsonErr != nil {
			return Config{}, err
		}
	}
	if cfg.Timezone == "" {
		cfg.Timezone = "Asia/Shanghai"
	}
	if cfg.ScanIntervalSeconds <= 0 {
		cfg.ScanIntervalSeconds = 120
	}
	if cfg.StatePath == "" {
		cfg.StatePath = "data/state.json"
	}
	if cfg.ProjectsRoot == "" {
		cfg.ProjectsRoot = "/workspace/projects"
	}
	if cfg.RuntimeRoot == "" {
		cfg.RuntimeRoot = "/app/data/runtime"
	}
	if cfg.PromptGuard == "" {
		cfg.PromptGuard = "注意：仅进行代码分析，在没有得到我的明确同意前禁止修改任何代码。"
	}
	if cfg.Sub2APIPlus.TimeoutSeconds <= 0 {
		cfg.Sub2APIPlus.TimeoutSeconds = 180
	}
	if cfg.Web.Listen == "" {
		cfg.Web.Listen = "0.0.0.0:38090"
	}
	cfg.Sub2APIPlus.BaseURL = strings.TrimRight(strings.TrimSpace(cfg.Sub2APIPlus.BaseURL), "/")
	cfg.Sub2APIPlus.InternalToken = strings.TrimSpace(cfg.Sub2APIPlus.InternalToken)
	return cfg, nil
}

func NewKeeper(cfg Config) (*Keeper, error) {
	if cfg.Sub2APIPlus.BaseURL == "" {
		return nil, fmt.Errorf("sub2apiplus.base_url 不能为空")
	}
	if cfg.Sub2APIPlus.InternalToken == "" {
		return nil, fmt.Errorf("sub2apiplus.internal_token 不能为空")
	}
	location, err := time.LoadLocation(cfg.Timezone)
	if err != nil {
		return nil, err
	}
	k := &Keeper{
		cfg:      cfg,
		location: location,
		httpClient: &http.Client{
			Timeout: time.Duration(cfg.Sub2APIPlus.TimeoutSeconds) * time.Second,
		},
		state: State{
			Version: version,
			Targets: map[string]*TargetState{},
		},
		persistentExecutors: map[string]*managedPersistentExecutor{},
		persistentFactory:   newDefaultPersistentExecutor,
	}
	_ = k.loadState()
	k.normalizeRuntimeState()
	k.syncTargets()
	return k, nil
}

func (k *Keeper) normalizeRuntimeState() {
	k.mu.Lock()
	defer k.mu.Unlock()
	now := time.Now().In(k.location)
	for _, state := range k.state.Targets {
		if state == nil {
			continue
		}
		hadRunningSession := state.Running
		state.Running = false
		for i := range state.Sessions {
			if state.Sessions[i].Status != "running" {
				continue
			}
			hadRunningSession = true
			state.Sessions[i].Status = "error"
			state.Sessions[i].Error = "sub2apiplus-keeper 重启前会话未完成，已清理运行状态"
			if state.Sessions[i].CompletedAt.IsZero() {
				state.Sessions[i].CompletedAt = now
			}
			state.Sessions[i].LatencyMS = state.Sessions[i].CompletedAt.Sub(state.Sessions[i].StartedAt).Milliseconds()
			state.Sessions[i].Summary = firstNonEmpty(state.Sessions[i].Summary, state.Sessions[i].Error)
			state.LastStatus = "error"
			state.LastError = state.Sessions[i].Error
			state.LastMessageSummary = state.Sessions[i].Summary
			state.LastKeepaliveReceivedAt = state.Sessions[i].CompletedAt
		}
		if hadRunningSession {
			state.ConsecutiveFailures++
		}
	}
	k.saveStateLocked()
}

func (k *Keeper) Run(ctx context.Context) {
	k.refreshTargets(ctx)
	k.scan(ctx)
	ticker := time.NewTicker(time.Duration(k.cfg.ScanIntervalSeconds) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			k.refreshTargets(ctx)
			k.scan(ctx)
		}
	}
}

func (k *Keeper) scan(ctx context.Context) {
	now := time.Now().In(k.location)
	targets := k.currentTargets()
	for _, target := range targets {
		target := target
		if !target.Enabled || target.AccountID <= 0 {
			continue
		}
		if !withinWorkWindow(now, target.WorkStart, target.WorkEnd) {
			continue
		}
		k.mu.Lock()
		state := k.targetStateLocked(target)
		k.resetDailyLocked(state, now)
		if !target.Due && !target.NextKeepaliveAt.IsZero() && now.Before(target.NextKeepaliveAt.In(k.location)) {
			state.NextRunAt = target.NextKeepaliveAt.In(k.location)
			state.Running = false
			k.saveStateLocked()
			k.mu.Unlock()
			continue
		}
		ready := !state.Running && (state.NextRunAt.IsZero() || !now.Before(state.NextRunAt.In(k.location)))
		if target.MaxDailyRuns > 0 && state.DailyKeepaliveCount >= target.MaxDailyRuns {
			ready = false
		}
		if ready {
			state.Running = true
			k.saveStateLocked()
		}
		k.mu.Unlock()
		if !ready {
			continue
		}
		go k.runTarget(ctx, target)
	}
}

func (k *Keeper) runTarget(ctx context.Context, target TargetConfig) {
	startedAt := time.Now().In(k.location)
	prompt := k.buildPrompt(target)
	session := Session{
		ID:         startedAt.Format("20060102T150405.000000000"),
		TargetName: target.Name,
		AccountID:  target.AccountID,
		Model:      target.Model,
		Mode:       normalizeMode(target.Mode),
		Prompt:     prompt,
		Status:     "running",
		StartedAt:  startedAt,
	}
	k.appendSession(target, session)

	result, err := k.executeTarget(ctx, target, prompt, session.ID)
	completedAt := time.Now().In(k.location)
	k.mu.Lock()
	state := k.targetStateLocked(target)
	state.Running = false
	state.LastKeepaliveStartedAt = startedAt
	state.LastKeepaliveReceivedAt = completedAt
	k.resetDailyLocked(state, completedAt)

	last := len(state.Sessions) - 1
	if last < 0 {
		return
	}
	if err != nil {
		result.ID = session.ID
		result.TargetName = target.Name
		result.AccountID = target.AccountID
		result.AccountName = target.Name
		result.Platform = target.Platform
		result.AccountType = target.AccountType
		result.Model = target.Model
		result.Mode = normalizeMode(target.Mode)
		result.Prompt = prompt
		result.Status = "error"
		result.Error = firstNonEmpty(result.Error, result.Summary, err.Error())
		result.StartedAt = startedAt
		result.CompletedAt = completedAt
		result.LatencyMS = completedAt.Sub(startedAt).Milliseconds()
		state.Sessions[last] = result
		state.Sessions[last].Status = "error"
		state.LastStatus = "error"
		state.LastError = result.Error
		state.LastMessageSummary = firstNonEmpty(result.Summary, result.ReplyText, result.Error)
		state.ConsecutiveFailures++
		state.NextRunAt = completedAt.Add(time.Duration(maxInt(target.IntervalMinutes, 1)) * time.Minute)
		recorded := state.Sessions[last]
		k.saveStateLocked()
		k.mu.Unlock()
		if billing, err := k.recordKeepalive(context.Background(), target, recorded); err == nil && billing != nil {
			k.applySessionBilling(target, session.ID, *billing)
		}
		return
	}

	state.Sessions[last] = result
	state.Sessions[last].ID = session.ID
	state.Sessions[last].TargetName = target.Name
	state.Sessions[last].AccountID = target.AccountID
	state.Sessions[last].AccountName = target.Name
	state.Sessions[last].Platform = target.Platform
	state.Sessions[last].AccountType = target.AccountType
	state.Sessions[last].Model = firstNonEmpty(result.Model, target.Model)
	state.Sessions[last].Mode = normalizeMode(target.Mode)
	state.Sessions[last].Prompt = prompt
	state.Sessions[last].Status = "success"
	state.Sessions[last].StartedAt = startedAt
	state.Sessions[last].CompletedAt = completedAt
	if state.Sessions[last].LatencyMS == 0 {
		state.Sessions[last].LatencyMS = completedAt.Sub(startedAt).Milliseconds()
	}
	state.LastError = ""
	state.LastStatus = "success"
	state.LastMessageSummary = firstNonEmpty(state.Sessions[last].Summary, state.Sessions[last].ReplyText)
	state.ConsecutiveFailures = 0
	state.DailyKeepaliveCount++
	state.NextRunAt = completedAt.Add(time.Duration(maxInt(target.IntervalMinutes, 1)) * time.Minute)
	k.trimSessionsLocked(state)
	k.saveStateLocked()
	recorded := state.Sessions[last]
	k.mu.Unlock()
	if billing, err := k.recordKeepalive(context.Background(), target, recorded); err == nil && billing != nil {
		k.applySessionBilling(target, session.ID, *billing)
	}
}

func (k *Keeper) callKeepalive(ctx context.Context, target TargetConfig, prompt string) (Session, error) {
	reqBody, _ := json.Marshal(keepaliveRequest{
		Model:           target.Model,
		Prompt:          prompt,
		MaxOutputTokens: target.MaxOutputTokens,
	})
	url := fmt.Sprintf("%s/api/v1/internal/keeper/accounts/%d/keepalive", k.cfg.Sub2APIPlus.BaseURL, target.AccountID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(reqBody))
	if err != nil {
		return Session{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", k.cfg.Sub2APIPlus.InternalToken)

	resp, err := k.httpClient.Do(req)
	if err != nil {
		return Session{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return Session{}, fmt.Errorf("sub2apiplus 返回 %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var envelope apiEnvelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		return Session{}, err
	}
	if envelope.Code != 0 {
		return Session{}, fmt.Errorf("sub2apiplus 返回错误: %s", envelope.Message)
	}
	var session Session
	if err := json.Unmarshal(envelope.Data, &session); err != nil {
		return Session{}, err
	}
	return session, nil
}

func (k *Keeper) recordKeepalive(ctx context.Context, target TargetConfig, session Session) (*Billing, error) {
	body := keepaliveRequest{
		Model:                    target.Model,
		Status:                   session.Status,
		SessionID:                session.ID,
		Summary:                  summarizeResult(session.ReplyText, session.Stdout, session.Stderr),
		Error:                    session.Error,
		InputTokens:              int64(session.Usage.InputTokens),
		OutputTokens:             int64(session.Usage.OutputTokens),
		CachedInputTokens:        int64(session.Usage.CacheReadTokens),
		CacheCreationInputTokens: int64(session.Usage.CacheCreationTokens),
		TotalTokens:              int64(session.Usage.TotalTokens),
		TotalCost:                session.Billing.TotalCost,
	}
	return k.postKeepalive(ctx, target.AccountID, body)
}

func (k *Keeper) postKeepalive(ctx context.Context, accountID int64, body keepaliveRequest) (*Billing, error) {
	reqBody, _ := json.Marshal(body)
	url := fmt.Sprintf("%s/api/v1/internal/keeper/accounts/%d/keepalive", k.cfg.Sub2APIPlus.BaseURL, accountID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(reqBody))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", k.cfg.Sub2APIPlus.InternalToken)
	resp, err := k.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("sub2apiplus 返回 %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var envelope apiEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, err
	}
	if envelope.Code != 0 {
		return nil, fmt.Errorf("sub2apiplus 返回错误: %s", envelope.Message)
	}
	var data recordKeepaliveResponse
	if len(envelope.Data) > 0 {
		if err := json.Unmarshal(envelope.Data, &data); err != nil {
			return nil, err
		}
	}
	return data.Billing, nil
}

func (k *Keeper) applySessionBilling(target TargetConfig, sessionID string, billing Billing) {
	k.mu.Lock()
	defer k.mu.Unlock()
	state := k.targetStateLocked(target)
	for i := range state.Sessions {
		if state.Sessions[i].ID != sessionID {
			continue
		}
		state.Sessions[i].Billing = billing
		k.saveStateLocked()
		return
	}
}

func (k *Keeper) executeTarget(ctx context.Context, target TargetConfig, prompt string, sessionID string) (Session, error) {
	executor := strings.ToLower(strings.TrimSpace(target.Executor))
	if executor == "" {
		executor = defaultExecutorForPlatform(target.Platform)
	}
	switch executor {
	case "codex", "claude":
		target.Executor = executor
		target.ClientType = executor
		return k.executeAccountPersistent(ctx, target, prompt, sessionID)
	default:
		return Session{Summary: "暂不支持的保活执行器: " + executor}, fmt.Errorf("暂不支持的保活执行器: %s", executor)
	}
}

func (k *Keeper) executeCodex(ctx context.Context, target TargetConfig, prompt string, sessionID string) (Session, error) {
	if strings.TrimSpace(target.WorkspacePath) == "" {
		return Session{Summary: "工作目录未配置"}, errors.New("工作目录未配置")
	}
	layout, err := k.prepareRuntime(target)
	if err != nil {
		return Session{Summary: "运行时准备失败", Error: err.Error()}, err
	}
	stdoutPath := filepath.Join(layout.LogDir, sessionID+".codex.stdout.log")
	stderrPath := filepath.Join(layout.LogDir, sessionID+".codex.stderr.log")
	lastMessagePath := filepath.Join(layout.SessionDir, sessionID+".codex.last.txt")
	timeout := time.Duration(maxInt(target.TimeoutSeconds, defaultClientTimeoutSeconds)) * time.Second

	args := []string{
		"-s", "read-only",
		"-a", "never",
		"exec",
		"--json",
		"--skip-git-repo-check",
		"--output-last-message", lastMessagePath,
		"-C", target.WorkspacePath,
		"-m", target.Model,
		prompt,
	}
	startedAt := time.Now()
	result, runErr := runCommand(ctx, commandSpec{
		Path:       "codex",
		Args:       args,
		Dir:        target.WorkspacePath,
		Env:        []string{"CODEX_HOME=" + layout.CodexHome, "OPENAI_API_KEY=" + k.cfg.Sub2APIPlus.InternalToken},
		Timeout:    timeout,
		StdoutPath: stdoutPath,
		StderrPath: stderrPath,
	})
	replyText := strings.TrimSpace(readOptionalText(lastMessagePath))
	parsedReply, usage := parseCodexJSONOutput(result.Stdout)
	if usage == nil {
		usage = parseCodexRolloutUsage(layout.CodexHome, startedAt)
	}
	if replyText == "" {
		replyText = parsedReply
	}
	if replyText == "" {
		replyText = firstNonEmpty(result.Stdout, result.Stderr)
	}
	session := Session{
		AccountID:       target.AccountID,
		AccountName:     target.Name,
		Platform:        target.Platform,
		AccountType:     target.AccountType,
		Model:           target.Model,
		Prompt:          prompt,
		ReplyText:       truncate(replyText, 12000),
		CommandText:     shellJoin("codex", args),
		WorkDir:         target.WorkspacePath,
		Stdout:          truncate(strings.TrimSpace(result.Stdout), 16000),
		Stderr:          truncate(strings.TrimSpace(result.Stderr), 16000),
		StdoutPath:      stdoutPath,
		StderrPath:      stderrPath,
		LastMessagePath: lastMessagePath,
		Summary:         summarizeResult(replyText, result.Stdout, result.Stderr),
		Usage:           usageToSessionUsage(usage),
	}
	if runErr != nil {
		session.Error = commandErrorMessage(runErr, session.Summary, replyText, result.Stderr, result.Stdout)
		return session, runErr
	}
	return session, nil
}

func (k *Keeper) executeClaude(ctx context.Context, target TargetConfig, prompt string, sessionID string) (Session, error) {
	if strings.TrimSpace(target.WorkspacePath) == "" {
		return Session{Summary: "工作目录未配置"}, errors.New("工作目录未配置")
	}
	layout, err := k.prepareRuntime(target)
	if err != nil {
		return Session{Summary: "运行时准备失败", Error: err.Error()}, err
	}
	stdoutPath := filepath.Join(layout.LogDir, sessionID+".claude.stdout.log")
	stderrPath := filepath.Join(layout.LogDir, sessionID+".claude.stderr.log")
	timeoutSeconds := maxInt(target.TimeoutSeconds, minClaudeTimeoutSeconds)
	timeout := time.Duration(timeoutSeconds) * time.Second

	args := []string{"--bare", "--print", "--output-format", "json", "--model", target.Model}
	if betas := claudeBetasForModel(target.Model); len(betas) > 0 {
		args = append(args, "--betas")
		args = append(args, betas...)
	}
	args = append(args, "-p", prompt)

	baseURL := fmt.Sprintf("%s/api/v1/internal/keeper/anthropic/accounts/%d", k.cfg.Sub2APIPlus.BaseURL, target.AccountID)
	env := []string{
		"HOME=" + layout.HomeDir,
		"CLAUDE_CONFIG_DIR=" + layout.ClaudeConfigDir,
		"ANTHROPIC_API_KEY=" + k.cfg.Sub2APIPlus.InternalToken,
		"ANTHROPIC_AUTH_TOKEN=" + k.cfg.Sub2APIPlus.InternalToken,
		"ANTHROPIC_BASE_URL=" + baseURL,
		"ANTHROPIC_MODEL=" + target.Model,
	}
	if betas := claudeBetasForModel(target.Model); len(betas) > 0 {
		env = append(env, "ANTHROPIC_BETAS="+strings.Join(betas, ","))
	}

	result, runErr := runCommand(ctx, commandSpec{
		Path:       "claude",
		Args:       args,
		Dir:        target.WorkspacePath,
		Env:        env,
		Timeout:    timeout,
		StdoutPath: stdoutPath,
		StderrPath: stderrPath,
	})
	parsedReply, usage := parseClaudeJSONOutput(result.Stdout)
	replyText := firstNonEmpty(parsedReply, result.Stdout, result.Stderr)
	session := Session{
		AccountID:   target.AccountID,
		AccountName: target.Name,
		Platform:    target.Platform,
		AccountType: target.AccountType,
		Model:       target.Model,
		Prompt:      prompt,
		ReplyText:   truncate(replyText, 12000),
		CommandText: shellJoin("claude", args),
		WorkDir:     target.WorkspacePath,
		Stdout:      truncate(strings.TrimSpace(result.Stdout), 16000),
		Stderr:      truncate(strings.TrimSpace(result.Stderr), 16000),
		StdoutPath:  stdoutPath,
		StderrPath:  stderrPath,
		Summary:     summarizeResult(replyText, result.Stdout, result.Stderr),
		Usage:       usageToSessionUsage(usage),
	}
	if runErr != nil {
		session.Error = commandErrorMessage(runErr, session.Summary, replyText, result.Stderr, result.Stdout)
		return session, runErr
	}
	return session, nil
}

func (k *Keeper) prepareRuntime(target TargetConfig) (runtimeLayout, error) {
	name := strings.TrimSpace(target.Name)
	if name == "" {
		name = fmt.Sprintf("account-%d", target.AccountID)
	}
	root := filepath.Join(k.cfg.RuntimeRoot, "workers", slugify(name))
	layout := runtimeLayout{
		RootDir:         root,
		LogDir:          filepath.Join(root, "logs"),
		SessionDir:      filepath.Join(root, "sessions"),
		HomeDir:         filepath.Join(root, "home"),
		CodexHome:       filepath.Join(root, "codex", ".codex"),
		ClaudeConfigDir: filepath.Join(root, "claude"),
		RuntimeEnvPath:  filepath.Join(root, "runtime.env"),
	}
	for _, dir := range []string{layout.RootDir, layout.LogDir, layout.SessionDir, layout.HomeDir, layout.CodexHome, layout.ClaudeConfigDir} {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return runtimeLayout{}, err
		}
	}
	if err := os.WriteFile(layout.RuntimeEnvPath, []byte(k.renderRuntimeEnv(target, layout)), 0600); err != nil {
		return runtimeLayout{}, err
	}
	if defaultExecutorForPlatform(target.Platform) == "codex" || strings.EqualFold(strings.TrimSpace(target.Executor), "codex") {
		configPath := filepath.Join(layout.CodexHome, "config.toml")
		if err := os.WriteFile(configPath, []byte(k.renderCodexConfig(target)), 0600); err != nil {
			return runtimeLayout{}, err
		}
	}
	return layout, nil
}

func (k *Keeper) renderRuntimeEnv(target TargetConfig, layout runtimeLayout) string {
	lines := []string{
		"SUB2APIPLUS_KEEPER_ACCOUNT_ID=" + shellQuote(strconv.FormatInt(target.AccountID, 10)),
		"SUB2APIPLUS_KEEPER_ACCOUNT_NAME=" + shellQuote(target.Name),
		"SUB2APIPLUS_KEEPER_PLATFORM=" + shellQuote(target.Platform),
		"SUB2APIPLUS_KEEPER_CLIENT_TYPE=" + shellQuote(targetExecutor(target)),
		"SUB2APIPLUS_KEEPER_BASE_URL=" + shellQuote(target.BaseURL),
		"SUB2APIPLUS_KEEPER_MODEL=" + shellQuote(target.Model),
		"SUB2APIPLUS_KEEPER_WORKSPACE_PATH=" + shellQuote(target.WorkspacePath),
		"CODEX_HOME=" + shellQuote(layout.CodexHome),
		"OPENAI_API_KEY=" + shellQuote(k.cfg.Sub2APIPlus.InternalToken),
		"ANTHROPIC_API_KEY=" + shellQuote(k.cfg.Sub2APIPlus.InternalToken),
		"ANTHROPIC_AUTH_TOKEN=" + shellQuote(k.cfg.Sub2APIPlus.InternalToken),
		"ANTHROPIC_BASE_URL=" + shellQuote(target.BaseURL),
		"ANTHROPIC_MODEL=" + shellQuote(target.Model),
	}
	return strings.Join(lines, "\n") + "\n"
}

func (k *Keeper) renderCodexConfig(target TargetConfig) string {
	baseURL := fmt.Sprintf("%s/api/v1/internal/keeper/openai/accounts/%d/v1", k.cfg.Sub2APIPlus.BaseURL, target.AccountID)
	lines := []string{
		`model_provider = "sub2apiplus_keeper_openai"`,
		fmt.Sprintf("model = %q", target.Model),
		"notify = []",
		`sandbox_mode = "read-only"`,
		"",
		`[model_providers.sub2apiplus_keeper_openai]`,
		`name = "Sub2APIPlus Keeper OpenAI proxy"`,
		fmt.Sprintf("base_url = %q", baseURL),
		`env_key = "OPENAI_API_KEY"`,
		`wire_api = "responses"`,
	}
	return strings.Join(lines, "\n") + "\n"
}

func (k *Keeper) buildPrompt(target TargetConfig) string {
	prompt := strings.TrimSpace(target.PromptText)
	if prompt == "" {
		prompt = k.pickPromptQuestion(target)
	}
	if guard := strings.TrimSpace(k.cfg.PromptGuard); guard != "" {
		return guard + "\n\n" + prompt
	}
	return prompt
}

func (k *Keeper) pickPromptQuestion(target TargetConfig) string {
	targetPath := normalizeProjectPath(target.WorkspacePath)
	candidates := make([]string, 0, len(k.cfg.PromptBank))
	for _, item := range k.cfg.PromptBank {
		if !item.Enabled {
			continue
		}
		text := strings.TrimSpace(item.Text)
		if text == "" {
			continue
		}
		scope := strings.TrimSpace(item.Scope)
		projectPath := normalizeProjectPath(item.ProjectPath)
		if scope == "" || scope == "global" || projectPath == "" || (targetPath != "" && projectPath == targetPath) {
			candidates = append(candidates, text)
		}
	}
	if len(candidates) > 0 {
		return candidates[rand.Intn(len(candidates))]
	}
	switch strings.TrimSpace(target.PromptProfile) {
	case "test_design":
		return "请为当前项目中的一个配置解析函数设计 3 个边界测试用例，只说明输入、预期输出和测试理由。"
	case "refactor_compare":
		return "请在当前项目中找一个适合重构的小函数，比较直接 if/else 与表驱动映射两种实现方式的优缺点，并给出简短结论。"
	default:
		return "请快速浏览当前项目，指出 2-3 个值得后续关注的代码结构、边界处理或测试覆盖问题。只做分析，不要修改代码。"
	}
}

func (k *Keeper) serve(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", k.withAuth(k.handleIndex))
	mux.HandleFunc("/api/state", k.withAuth(k.handleState))
	mux.HandleFunc("/api/settings", k.withAuth(k.handleSettings))
	mux.HandleFunc("/api/run", k.withAuth(k.handleManualRun))
	server := &http.Server{Addr: k.cfg.Web.Listen, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()
	log.Printf("keeper 网页监听: %s", k.cfg.Web.Listen)
	return server.ListenAndServe()
}

func (k *Keeper) handleIndex(w http.ResponseWriter, r *http.Request) {
	k.mu.Lock()
	view := k.snapshotLocked()
	k.mu.Unlock()
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_ = indexTemplate.Execute(w, view)
}

func (k *Keeper) handleState(w http.ResponseWriter, r *http.Request) {
	_ = k.refreshTargets(r.Context())
	k.mu.Lock()
	view := k.snapshotLocked()
	k.mu.Unlock()
	writeJSON(w, view)
}

func (k *Keeper) handleSettings(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		k.mu.Lock()
		payload := map[string]any{
			"version":      version,
			"prompt_guard": k.cfg.PromptGuard,
			"prompt_bank":  clonePromptBank(k.cfg.PromptBank),
		}
		k.mu.Unlock()
		writeJSON(w, payload)
	case http.MethodPost:
		var req settingsRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}
		k.mu.Lock()
		if req.PromptGuard != nil {
			k.cfg.PromptGuard = strings.TrimSpace(*req.PromptGuard)
			k.state.ConfiguredPromptGuard = stringPtr(k.cfg.PromptGuard)
		}
		if req.PromptBank != nil {
			k.cfg.PromptBank = normalizePromptBank(*req.PromptBank)
			k.state.ConfiguredPromptBank = promptBankPtr(k.cfg.PromptBank)
		}
		k.saveStateLocked()
		payload := map[string]any{
			"version":      version,
			"prompt_guard": k.cfg.PromptGuard,
			"prompt_bank":  clonePromptBank(k.cfg.PromptBank),
			"status":       "saved",
		}
		k.mu.Unlock()
		writeJSON(w, payload)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (k *Keeper) handleManualRun(w http.ResponseWriter, r *http.Request) {
	_ = k.refreshTargets(r.Context())
	name := strings.TrimSpace(r.URL.Query().Get("target"))
	if name == "" {
		http.Error(w, "target required", http.StatusBadRequest)
		return
	}
	for _, target := range k.currentTargets() {
		if target.Name == name {
			k.mu.Lock()
			state := k.targetStateLocked(target)
			if state.Running {
				k.mu.Unlock()
				writeJSON(w, map[string]string{"status": "running"})
				return
			}
			state.Running = true
			k.saveStateLocked()
			k.mu.Unlock()
			go k.runTarget(context.Background(), target)
			writeJSON(w, map[string]string{"status": "accepted"})
			return
		}
	}
	http.Error(w, "target not found", http.StatusNotFound)
}

func (k *Keeper) withAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if token := strings.TrimSpace(r.Header.Get("x-api-key")); token != "" &&
			k.cfg.Sub2APIPlus.InternalToken != "" &&
			subtle.ConstantTimeCompare([]byte(token), []byte(k.cfg.Sub2APIPlus.InternalToken)) == 1 {
			next(w, r)
			return
		}
		if k.cfg.Web.Username == "" && k.cfg.Web.Password == "" {
			next(w, r)
			return
		}
		username, password, ok := r.BasicAuth()
		if !ok || subtle.ConstantTimeCompare([]byte(username), []byte(k.cfg.Web.Username)) != 1 ||
			subtle.ConstantTimeCompare([]byte(password), []byte(k.cfg.Web.Password)) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="sub2apiplus-keeper"`)
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func (k *Keeper) loadState() error {
	raw, err := os.ReadFile(k.cfg.StatePath)
	if err != nil {
		return err
	}
	var state State
	if err := json.Unmarshal(raw, &state); err != nil {
		return err
	}
	if state.Targets == nil {
		state.Targets = map[string]*TargetState{}
	}
	if state.ConfiguredPromptGuard != nil {
		k.cfg.PromptGuard = strings.TrimSpace(*state.ConfiguredPromptGuard)
	}
	if state.ConfiguredPromptBank != nil {
		k.cfg.PromptBank = normalizePromptBank(*state.ConfiguredPromptBank)
	}
	k.state = state
	return nil
}

func (k *Keeper) syncTargets() {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.syncTargetsLocked()
}

func (k *Keeper) syncTargetsLocked() {
	for _, target := range k.cfg.Targets {
		if target.Name == "" {
			continue
		}
		state := k.targetStateLocked(target)
		state.AccountID = target.AccountID
		state.Model = target.Model
		state.Enabled = target.Enabled
		if state.NextRunAt.IsZero() && target.InitialDelaySecs > 0 {
			state.NextRunAt = time.Now().In(k.location).Add(time.Duration(target.InitialDelaySecs) * time.Second)
		}
	}
	k.saveStateLocked()
}

func (k *Keeper) appendSession(target TargetConfig, session Session) {
	k.mu.Lock()
	defer k.mu.Unlock()
	state := k.targetStateLocked(target)
	state.Sessions = append(state.Sessions, session)
	k.trimSessionsLocked(state)
	k.saveStateLocked()
}

func (k *Keeper) targetStateLocked(target TargetConfig) *TargetState {
	if k.state.Targets == nil {
		k.state.Targets = map[string]*TargetState{}
	}
	name := target.Name
	if name == "" {
		name = strconv.FormatInt(target.AccountID, 10)
	}
	state := k.state.Targets[name]
	if state == nil {
		state = &TargetState{Name: name}
		k.state.Targets[name] = state
	}
	state.Name = name
	state.AccountID = target.AccountID
	state.Model = target.Model
	state.Enabled = target.Enabled
	return state
}

func (k *Keeper) currentTargets() []TargetConfig {
	k.mu.Lock()
	defer k.mu.Unlock()
	return cloneTargets(k.cfg.Targets)
}

func (k *Keeper) resetDailyLocked(state *TargetState, now time.Time) {
	date := now.In(k.location).Format("2006-01-02")
	if state.DailyDate != date {
		state.DailyDate = date
		state.DailyKeepaliveCount = 0
	}
}

func (k *Keeper) trimSessionsLocked(state *TargetState) {
	const maxSessionsPerTarget = 500
	if len(state.Sessions) > maxSessionsPerTarget {
		state.Sessions = append([]Session(nil), state.Sessions[len(state.Sessions)-maxSessionsPerTarget:]...)
	}
}

func (k *Keeper) saveStateLocked() {
	if err := os.MkdirAll(filepath.Dir(k.cfg.StatePath), 0755); err != nil {
		log.Printf("创建状态目录失败: %v", err)
		return
	}
	raw, err := json.MarshalIndent(k.state, "", "  ")
	if err != nil {
		log.Printf("序列化状态失败: %v", err)
		return
	}
	tmp := k.cfg.StatePath + ".tmp"
	if err := os.WriteFile(tmp, raw, 0600); err != nil {
		log.Printf("写状态文件失败: %v", err)
		return
	}
	if err := os.Rename(tmp, k.cfg.StatePath); err != nil {
		log.Printf("替换状态文件失败: %v", err)
	}
}

func (k *Keeper) snapshotLocked() any {
	targets := make([]*TargetState, 0, len(k.state.Targets))
	for _, target := range k.state.Targets {
		cp := *target
		cp.Sessions = append([]Session(nil), target.Sessions...)
		sort.Slice(cp.Sessions, func(i, j int) bool {
			return cp.Sessions[i].StartedAt.After(cp.Sessions[j].StartedAt)
		})
		targets = append(targets, &cp)
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i].Name < targets[j].Name })
	overview := k.overviewRowsLocked()
	return map[string]any{
		"version":            version,
		"now":                time.Now().In(k.location),
		"dashboard":          k.dashboardStatsLocked(),
		"overview":           overview,
		"projects_root":      k.cfg.ProjectsRoot,
		"prompt_bank_count":  len(k.cfg.PromptBank),
		"prompt_guard":       k.cfg.PromptGuard,
		"prompt_bank":        clonePromptBank(k.cfg.PromptBank),
		"configured_targets": cloneTargets(k.cfg.Targets),
		"targets":            targets,
	}
}

func (k *Keeper) dashboardStatsLocked() dashboardStats {
	now := time.Now().In(k.location)
	today := now.Format("2006-01-02")
	stats := dashboardStats{TotalTargets: len(k.cfg.Targets)}
	configured := map[string]TargetConfig{}
	for _, target := range k.cfg.Targets {
		configured[target.Name] = target
		if target.Enabled {
			stats.EnabledTargets++
		}
		state := k.targetStateLocked(target)
		if state.Running {
			stats.RunningCount++
		}
		for _, session := range state.Sessions {
			sessionTime := session.CompletedAt
			if sessionTime.IsZero() {
				sessionTime = session.StartedAt
			}
			if !sessionTime.IsZero() && sessionTime.In(k.location).Format("2006-01-02") == today {
				switch session.Status {
				case "success":
					stats.TodaySuccesses++
				case "error":
					stats.TodayFailures++
				}
			}
			switch session.Status {
			case "success":
				if stats.LastSuccessAt.IsZero() || sessionTime.After(stats.LastSuccessAt) {
					stats.LastSuccessAt = sessionTime
				}
			case "error":
				if stats.LastFailureAt.IsZero() || sessionTime.After(stats.LastFailureAt) {
					stats.LastFailureAt = sessionTime
				}
			}
		}
	}
	_ = configured
	return stats
}

func (k *Keeper) overviewRowsLocked() []overviewRow {
	rows := make([]overviewRow, 0, len(k.cfg.Targets))
	usageSince := time.Now().In(k.location).Add(-24 * time.Hour)
	for _, target := range k.cfg.Targets {
		state := k.targetStateLocked(target)
		executionCount, successCount, failureCount := sessionCounts(state.Sessions)
		lastFinishedAt := time.Time{}
		if len(state.Sessions) > 0 {
			last := state.Sessions[len(state.Sessions)-1]
			lastFinishedAt = last.CompletedAt
			if lastFinishedAt.IsZero() {
				lastFinishedAt = last.StartedAt
			}
		}
		usage24hCost := accountUsageCostSummary(state.Sessions, usageSince)
		nextRunAt := state.NextRunAt
		if !target.NextKeepaliveAt.IsZero() {
			nextRunAt = target.NextKeepaliveAt.In(k.location)
		}
		row := overviewRow{
			Name:                target.Name,
			AccountID:           target.AccountID,
			Platform:            target.Platform,
			AccountType:         target.AccountType,
			Executor:            firstNonEmpty(target.Executor, defaultExecutorForPlatform(target.Platform)),
			Model:               target.Model,
			Enabled:             target.Enabled,
			Running:             state.Running,
			LastMessageSummary:  state.LastMessageSummary,
			ConsecutiveFailures: state.ConsecutiveFailures,
			ExecutionCount:      executionCount,
			SuccessCount:        successCount,
			FailureCount:        failureCount,
			LastStartedAt:       state.LastKeepaliveStartedAt,
			LastFinishedAt:      lastFinishedAt,
			NextRunAt:           nextRunAt,
			Usage24hCost:        usage24hCost,
			TotalUsageCost:      usage24hCost,
		}
		switch {
		case state.Running:
			row.CurrentStatus = "执行中"
			row.StatusClass = "warn"
		case state.LastStatus == "error":
			row.CurrentStatus = "异常"
			row.StatusClass = "err"
			row.StatusDetail = state.LastError
		case state.LastStatus == "success":
			row.CurrentStatus = "正常"
			row.StatusClass = "ok"
		case target.Enabled:
			row.CurrentStatus = "等待下次"
			row.StatusClass = "ok"
		default:
			row.CurrentStatus = "关闭"
			row.StatusClass = "muted"
		}
		rows = append(rows, row)
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Platform != rows[j].Platform {
			return rows[i].Platform < rows[j].Platform
		}
		return rows[i].Name < rows[j].Name
	})
	return rows
}

func accountUsageCostSummary(sessions []Session, since time.Time) *usageCostSummary {
	summary := usageCostSummary{Currency: "USD", Precise: true}
	for _, session := range sessions {
		sessionTime := session.CompletedAt
		if sessionTime.IsZero() {
			sessionTime = session.StartedAt
		}
		if !since.IsZero() && (sessionTime.IsZero() || sessionTime.Before(since)) {
			continue
		}
		summary.TotalTokens += session.Usage.TotalTokens
		if session.Billing.Available {
			summary.TotalCost += firstFloat(session.Billing.ActualCost, session.Billing.TotalCost)
			summary.HasCost = true
		}
	}
	if summary.TotalTokens == 0 && !summary.HasCost {
		return nil
	}
	return &summary
}

func sessionCounts(sessions []Session) (int, int, int) {
	var executionCount, successCount, failureCount int
	for _, session := range sessions {
		switch session.Status {
		case "success":
			executionCount++
			successCount++
		case "error":
			executionCount++
			failureCount++
		}
	}
	return executionCount, successCount, failureCount
}

func (k *Keeper) refreshTargets(ctx context.Context) error {
	targets, err := k.fetchTargets(ctx)
	if err != nil {
		log.Printf("同步 sub2apiplus 保活账号失败: %v", err)
		return err
	}
	k.mu.Lock()
	k.cfg.Targets = targets
	k.syncTargetsLocked()
	k.mu.Unlock()
	return nil
}

func (k *Keeper) fetchTargets(ctx context.Context) ([]TargetConfig, error) {
	url := k.cfg.Sub2APIPlus.BaseURL + "/api/v1/internal/keeper/accounts"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("x-api-key", k.cfg.Sub2APIPlus.InternalToken)
	resp, err := k.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("sub2apiplus 返回 %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var envelope apiEnvelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, err
	}
	if envelope.Code != 0 {
		return nil, fmt.Errorf("sub2apiplus 返回错误: %s", envelope.Message)
	}
	var data keeperAccountsData
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return nil, err
	}
	targets := make([]TargetConfig, 0, len(data.Accounts))
	for _, account := range data.Accounts {
		if !account.Enabled || account.ID <= 0 {
			continue
		}
		executor := firstNonEmpty(account.Executor, defaultExecutorForPlatform(account.Platform))
		targets = append(targets, TargetConfig{
			ID:              strconv.FormatInt(account.ID, 10),
			Name:            account.Name,
			Enabled:         account.Enabled,
			AccountID:       account.ID,
			Platform:        account.Platform,
			AccountType:     account.Type,
			Executor:        executor,
			ClientType:      executor,
			BaseURL:         k.internalClientBaseURL(account.ID, executor),
			APIKey:          k.cfg.Sub2APIPlus.InternalToken,
			Model:           account.Model,
			IntervalMinutes: maxInt(account.IntervalMinutes, 1),
			WorkStart:       account.WorkStart,
			WorkEnd:         account.WorkEnd,
			WorkspacePath:   k.workspacePath(account.Workspace),
			PromptText:      account.Prompt,
			Mode:            normalizeMode(account.Mode),
			TimeoutSeconds:  defaultClientTimeoutSeconds,
			MaxOutputTokens: 512,
			PromptProfile:   "project_overview",
			Due:             account.Due,
			NextKeepaliveAt: timeValue(account.NextKeepaliveAt),
		})
	}
	return targets, nil
}

func timeValue(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return *value
}

func (k *Keeper) workspacePath(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if filepath.IsAbs(value) {
		return filepath.Clean(value)
	}
	return filepath.Join(k.cfg.ProjectsRoot, value)
}

func withinWorkWindow(now time.Time, start, end string) bool {
	startMinute, okStart := parseClockMinute(start)
	endMinute, okEnd := parseClockMinute(end)
	if !okStart || !okEnd {
		return true
	}
	current := now.Hour()*60 + now.Minute()
	if endMinute == 24*60 {
		endMinute = 24*60 - 1
	}
	if startMinute <= endMinute {
		return current >= startMinute && current <= endMinute
	}
	return current >= startMinute || current <= endMinute
}

func parseClockMinute(value string) (int, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	parts := strings.Split(value, ":")
	if len(parts) != 2 {
		return 0, false
	}
	hour, err1 := strconv.Atoi(parts[0])
	minute, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil || minute < 0 || minute > 59 || hour < 0 || hour > 24 {
		return 0, false
	}
	if hour == 24 && minute != 0 {
		return 0, false
	}
	return hour*60 + minute, true
}

type commandSpec struct {
	Path       string
	Args       []string
	Dir        string
	Env        []string
	Timeout    time.Duration
	StdoutPath string
	StderrPath string
}

type commandResult struct {
	ExitCode int
	Stdout   string
	Stderr   string
}

type limitedBuffer struct {
	max int
	buf bytes.Buffer
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if b.max <= 0 {
		return len(p), nil
	}
	if remaining := b.max - b.buf.Len(); remaining > 0 {
		if len(p) > remaining {
			p = p[:remaining]
		}
		_, _ = b.buf.Write(p)
	}
	return len(p), nil
}

func (b *limitedBuffer) String() string {
	return b.buf.String()
}

func runCommand(ctx context.Context, spec commandSpec) (commandResult, error) {
	path, err := exec.LookPath(spec.Path)
	if err != nil {
		return commandResult{}, fmt.Errorf("找不到命令: %s", spec.Path)
	}
	runCtx := ctx
	cancel := func() {}
	if spec.Timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, spec.Timeout)
	}
	defer cancel()

	cmd := exec.CommandContext(runCtx, path, spec.Args...)
	cmd.Dir = spec.Dir
	cmd.Env = append(os.Environ(), spec.Env...)

	stdoutFile, err := os.Create(spec.StdoutPath)
	if err != nil {
		return commandResult{}, err
	}
	defer func() { _ = stdoutFile.Close() }()
	stderrFile, err := os.Create(spec.StderrPath)
	if err != nil {
		return commandResult{}, err
	}
	defer func() { _ = stderrFile.Close() }()

	var stdoutBuf, stderrBuf limitedBuffer
	stdoutBuf.max = defaultMaxOutputBytes
	stderrBuf.max = defaultMaxOutputBytes
	cmd.Stdout = io.MultiWriter(stdoutFile, &stdoutBuf)
	cmd.Stderr = io.MultiWriter(stderrFile, &stderrBuf)

	runErr := cmd.Run()
	result := commandResult{Stdout: stdoutBuf.String(), Stderr: stderrBuf.String()}
	if runErr == nil {
		return result, nil
	}
	if runCtx.Err() != nil {
		if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
			return result, fmt.Errorf("命令执行超时: %s, 超过 %s 后已终止", spec.Path, spec.Timeout)
		}
		return result, runCtx.Err()
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		result.ExitCode = exitErr.ExitCode()
		detail := summarizeCommandOutput(result.Stderr, result.Stdout)
		if detail != "" {
			return result, fmt.Errorf("命令执行失败: %s, 退出码 %d: %s", spec.Path, exitErr.ExitCode(), detail)
		}
		return result, fmt.Errorf("命令执行失败: %s, 退出码 %d", spec.Path, exitErr.ExitCode())
	}
	return result, fmt.Errorf("命令执行失败: %s: %w", spec.Path, runErr)
}

func readOptionalText(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

func parseCodexJSONOutput(stdout string) (string, *usageInfo) {
	if strings.TrimSpace(stdout) == "" {
		return "", nil
	}
	var reply string
	var usage *usageInfo
	for _, line := range strings.Split(stdout, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var event struct {
			Type string `json:"type"`
			Item struct {
				Type  string `json:"type"`
				Text  string `json:"text"`
				Phase string `json:"phase"`
			} `json:"item"`
			Usage *struct {
				InputTokens           int64 `json:"input_tokens"`
				CachedInputTokens     int64 `json:"cached_input_tokens"`
				OutputTokens          int64 `json:"output_tokens"`
				ReasoningOutputTokens int64 `json:"reasoning_output_tokens"`
			} `json:"usage"`
		}
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			continue
		}
		if event.Type == "item.completed" && event.Item.Type == "agent_message" && !strings.EqualFold(strings.TrimSpace(event.Item.Phase), "commentary") {
			reply = strings.TrimSpace(event.Item.Text)
		}
		if event.Type == "turn.completed" && event.Usage != nil {
			usage = normalizeUsage(usageInfo{
				InputTokens:           event.Usage.InputTokens,
				CachedInputTokens:     event.Usage.CachedInputTokens,
				OutputTokens:          event.Usage.OutputTokens,
				ReasoningOutputTokens: event.Usage.ReasoningOutputTokens,
			})
		}
	}
	return reply, usage
}

func parseClaudeJSONOutput(stdout string) (string, *usageInfo) {
	stdout = strings.TrimSpace(stdout)
	if stdout == "" {
		return "", nil
	}
	var event struct {
		Result string `json:"result"`
		Usage  *struct {
			InputTokens              int64 `json:"input_tokens"`
			CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
			CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
			OutputTokens             int64 `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal([]byte(stdout), &event); err == nil {
		return strings.TrimSpace(event.Result), claudeUsageInfo(event.Usage)
	}

	var reply string
	var usage *usageInfo
	for _, line := range strings.Split(stdout, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var streamEvent struct {
			Type    string `json:"type"`
			Result  string `json:"result"`
			Message *struct {
				Usage *struct {
					InputTokens              int64 `json:"input_tokens"`
					CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
					CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
					OutputTokens             int64 `json:"output_tokens"`
				} `json:"usage"`
				Content []struct {
					Type string `json:"type"`
					Text string `json:"text"`
				} `json:"content"`
			} `json:"message"`
			Usage *struct {
				InputTokens              int64 `json:"input_tokens"`
				CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
				CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
				OutputTokens             int64 `json:"output_tokens"`
			} `json:"usage"`
		}
		if err := json.Unmarshal([]byte(line), &streamEvent); err != nil {
			continue
		}
		if streamEvent.Result != "" {
			reply = strings.TrimSpace(streamEvent.Result)
		}
		if streamEvent.Message != nil {
			for _, content := range streamEvent.Message.Content {
				if content.Type == "text" && strings.TrimSpace(content.Text) != "" {
					reply = strings.TrimSpace(content.Text)
				}
			}
			if streamEvent.Message.Usage != nil {
				usage = claudeUsageInfo(streamEvent.Message.Usage)
			}
		}
		if streamEvent.Usage != nil {
			usage = claudeUsageInfo(streamEvent.Usage)
		}
	}
	return reply, usage
}

func claudeUsageInfo(usage *struct {
	InputTokens              int64 `json:"input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
}) *usageInfo {
	if usage == nil {
		return nil
	}
	return normalizeUsage(usageInfo{
		InputTokens:              usage.InputTokens + usage.CacheReadInputTokens + usage.CacheCreationInputTokens,
		CachedInputTokens:        usage.CacheReadInputTokens,
		CacheCreationInputTokens: usage.CacheCreationInputTokens,
		OutputTokens:             usage.OutputTokens,
	})
}

func parseCodexRolloutUsage(codexHome string, startedAt time.Time) *usageInfo {
	root := filepath.Join(codexHome, "sessions")
	type candidate struct {
		path    string
		modTime time.Time
	}
	candidates := make([]candidate, 0, 4)
	cutoff := startedAt.Add(-2 * time.Minute)
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d == nil || d.IsDir() || !strings.HasSuffix(path, ".jsonl") {
			return nil
		}
		info, err := d.Info()
		if err != nil || info.ModTime().Before(cutoff) {
			return nil
		}
		candidates = append(candidates, candidate{path: path, modTime: info.ModTime()})
		return nil
	})
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].modTime.After(candidates[j].modTime)
	})
	for _, item := range candidates {
		if usage := parseCodexRolloutUsageFile(item.path); usage != nil {
			return usage
		}
	}
	return nil
}

func parseCodexRolloutUsageFile(path string) *usageInfo {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var usage *usageInfo
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var event struct {
			Type    string `json:"type"`
			Payload struct {
				Type string `json:"type"`
				Info struct {
					TotalTokenUsage struct {
						InputTokens           int64 `json:"input_tokens"`
						CachedInputTokens     int64 `json:"cached_input_tokens"`
						OutputTokens          int64 `json:"output_tokens"`
						ReasoningOutputTokens int64 `json:"reasoning_output_tokens"`
					} `json:"total_token_usage"`
				} `json:"info"`
			} `json:"payload"`
		}
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			continue
		}
		if event.Type == "event_msg" && event.Payload.Type == "token_count" {
			total := event.Payload.Info.TotalTokenUsage
			usage = normalizeUsage(usageInfo{
				InputTokens:           total.InputTokens,
				CachedInputTokens:     total.CachedInputTokens,
				OutputTokens:          total.OutputTokens,
				ReasoningOutputTokens: total.ReasoningOutputTokens,
			})
		}
	}
	return usage
}

func normalizeUsage(usage usageInfo) *usageInfo {
	if usage.InputTokens < 0 {
		usage.InputTokens = 0
	}
	if usage.CachedInputTokens < 0 {
		usage.CachedInputTokens = 0
	}
	if usage.CacheCreationInputTokens < 0 {
		usage.CacheCreationInputTokens = 0
	}
	if usage.OutputTokens < 0 {
		usage.OutputTokens = 0
	}
	if usage.CachedInputTokens > usage.InputTokens {
		usage.CachedInputTokens = usage.InputTokens
	}
	maxCacheCreation := usage.InputTokens - usage.CachedInputTokens
	if usage.CacheCreationInputTokens > maxCacheCreation {
		usage.CacheCreationInputTokens = maxCacheCreation
	}
	usage.UncachedInputTokens = usage.InputTokens - usage.CachedInputTokens - usage.CacheCreationInputTokens
	usage.TotalTokens = usage.InputTokens + usage.OutputTokens
	if usage.TotalTokens == 0 {
		return nil
	}
	return &usage
}

func usageToSessionUsage(usage *usageInfo) Usage {
	if usage == nil {
		return Usage{}
	}
	return Usage{
		InputTokens:              int(usage.UncachedInputTokens),
		OutputTokens:             int(usage.OutputTokens),
		TotalTokens:              int(usage.TotalTokens),
		CacheCreationTokens:      int(usage.CacheCreationInputTokens),
		CacheCreationInputTokens: int(usage.CacheCreationInputTokens),
		CacheReadTokens:          int(usage.CachedInputTokens),
	}
}

func commandErrorMessage(runErr error, values ...string) string {
	if runErr != nil {
		return runErr.Error()
	}
	return firstNonEmpty(values...)
}

func summarizeCommandOutput(values ...string) string {
	text := firstNonEmpty(values...)
	if text == "" {
		return ""
	}
	text = strings.ReplaceAll(strings.TrimSpace(text), "\r", "\n")
	lines := make([]string, 0, 3)
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		lines = append(lines, line)
		if len(lines) >= 3 {
			break
		}
	}
	return truncate(strings.Join(lines, " "), 500)
}

func summarizeResult(reply, stdout, stderr string) string {
	switch {
	case strings.TrimSpace(reply) != "":
		return truncate(strings.ReplaceAll(strings.TrimSpace(reply), "\n", " "), 160)
	case strings.TrimSpace(stdout) != "":
		return truncate(strings.ReplaceAll(strings.TrimSpace(stdout), "\n", " "), 160)
	case strings.TrimSpace(stderr) != "":
		return truncate(strings.ReplaceAll(strings.TrimSpace(stderr), "\n", " "), 160)
	default:
		return ""
	}
}

func truncate(value string, maxLen int) string {
	runes := []rune(value)
	if len(runes) <= maxLen {
		return value
	}
	return string(runes[:maxLen]) + "..."
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func firstNonEmptyMeaningful(values ...string) string {
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed == "" || isUnhelpfulStructuredLine(trimmed) {
			continue
		}
		return value
	}
	return ""
}

func parseClientOutput(clientType string, stdout string) (string, *usageInfo) {
	switch strings.ToLower(strings.TrimSpace(clientType)) {
	case "claude":
		return parseClaudeJSONOutput(stdout)
	default:
		return parseCodexJSONOutput(stdout)
	}
}

func summarizeStructuredStdout(stdout string) string {
	reply, _ := parseClientOutput("codex", stdout)
	if reply != "" {
		return summarizeResult(reply, "", "")
	}
	return ""
}

func summarizeStructuredStdoutForClient(clientType string, stdout string) string {
	reply, _ := parseClientOutput(clientType, stdout)
	if reply != "" {
		return summarizeResult(reply, "", "")
	}
	return ""
}

func isUnhelpfulStructuredLine(value string) bool {
	if !strings.HasPrefix(value, "{") {
		return false
	}
	var event struct {
		Type    string `json:"type"`
		Subtype string `json:"subtype"`
		Result  string `json:"result"`
		Error   string `json:"error"`
	}
	if err := json.Unmarshal([]byte(value), &event); err != nil {
		return false
	}
	return event.Type == "system" && strings.TrimSpace(event.Result) == "" && strings.TrimSpace(event.Error) == ""
}

func hasSuccessfulSession(sessions []Session) bool {
	for i := len(sessions) - 1; i >= 0; i-- {
		if sessions[i].Status == "success" {
			return true
		}
	}
	return false
}

func stableHash(value string) uint64 {
	var h uint64 = 1469598103934665603
	for i := 0; i < len(value); i++ {
		h ^= uint64(value[i])
		h *= 1099511628211
	}
	return h
}

func defaultExecutorForPlatform(platform string) string {
	if strings.EqualFold(strings.TrimSpace(platform), "anthropic") {
		return "claude"
	}
	return "codex"
}

func normalizeMode(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "fresh":
		return "fresh"
	default:
		return defaultKeepaliveMode
	}
}

func targetExecutor(target TargetConfig) string {
	executor := strings.ToLower(strings.TrimSpace(firstNonEmpty(target.ClientType, target.Executor)))
	if executor == "" {
		executor = defaultExecutorForPlatform(target.Platform)
	}
	return executor
}

func targetID(target TargetConfig) string {
	if id := strings.TrimSpace(target.ID); id != "" {
		return id
	}
	if target.AccountID > 0 {
		return strconv.FormatInt(target.AccountID, 10)
	}
	return slugify(target.Name)
}

func (k *Keeper) internalClientBaseURL(accountID int64, executor string) string {
	switch strings.ToLower(strings.TrimSpace(executor)) {
	case "claude":
		return fmt.Sprintf("%s/api/v1/internal/keeper/anthropic/accounts/%d", k.cfg.Sub2APIPlus.BaseURL, accountID)
	default:
		return fmt.Sprintf("%s/api/v1/internal/keeper/openai/accounts/%d/v1", k.cfg.Sub2APIPlus.BaseURL, accountID)
	}
}

func claudeBetasForModel(model string) []string {
	if strings.Contains(strings.ToLower(strings.TrimSpace(model)), "[1m]") {
		return []string{"context-1m-2025-08-07"}
	}
	return nil
}

func shellJoin(path string, args []string) string {
	all := append([]string{path}, args...)
	quoted := make([]string, 0, len(all))
	for _, item := range all {
		quoted = append(quoted, shellQuote(item))
	}
	return strings.Join(quoted, " ")
}

func shellQuote(value string) string {
	if value == "" {
		return "''"
	}
	return "'" + strings.ReplaceAll(value, "'", `'"'"'`) + "'"
}

func slugify(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	var b strings.Builder
	lastDash := false
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			lastDash = false
			continue
		}
		if !lastDash {
			b.WriteByte('-')
			lastDash = true
		}
	}
	out := strings.Trim(b.String(), "-")
	if out == "" {
		return "target"
	}
	return out
}

func normalizeProjectPath(value string) string {
	value = filepath.Clean(strings.TrimSpace(value))
	if value == "." {
		return ""
	}
	return value
}

func cloneTargets(targets []TargetConfig) []TargetConfig {
	out := make([]TargetConfig, len(targets))
	copy(out, targets)
	return out
}

func normalizePromptBank(items []PromptQuestion) []PromptQuestion {
	out := make([]PromptQuestion, 0, len(items))
	for _, item := range items {
		item.ID = strings.TrimSpace(item.ID)
		item.Scope = strings.TrimSpace(item.Scope)
		if item.Scope != "project" {
			item.Scope = "global"
			item.ProjectPath = ""
		} else {
			item.ProjectPath = normalizeProjectPath(item.ProjectPath)
		}
		item.Text = strings.TrimSpace(item.Text)
		if item.Text == "" {
			continue
		}
		if item.ID == "" {
			item.ID = fmt.Sprintf("prompt-%d", len(out)+1)
		}
		out = append(out, item)
	}
	return out
}

func clonePromptBank(items []PromptQuestion) []PromptQuestion {
	out := make([]PromptQuestion, len(items))
	copy(out, items)
	return out
}

func stringPtr(value string) *string {
	return &value
}

func promptBankPtr(items []PromptQuestion) *[]PromptQuestion {
	cp := clonePromptBank(items)
	return &cp
}

func getenvDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func firstFloat(values ...float64) float64 {
	for _, value := range values {
		if value != 0 {
			return value
		}
	}
	return 0
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(value)
}

var indexTemplate = template.Must(template.New("index").Funcs(template.FuncMap{
	"cost": func(target *TargetState) float64 {
		var total float64
		for _, s := range target.Sessions {
			if s.Status == "success" {
				total += s.Billing.ActualCost
			}
		}
		return total
	},
	"tokens": func(target *TargetState) int {
		var total int
		for _, s := range target.Sessions {
			if s.Status == "success" {
				total += s.Usage.TotalTokens
			}
		}
		return total
	},
}).Parse(`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>sub2apiplus-keeper</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f7f7f8;color:#1f2328}
    header{padding:16px 24px;background:#101828;color:#fff;display:flex;justify-content:space-between;align-items:center}
    main{padding:20px 24px}
    table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb}
    th,td{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top;font-size:14px}
    th{background:#f3f4f6}
    details{margin:8px 0}
    pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #e5e7eb;padding:10px;border-radius:6px}
    .ok{color:#047857}.err{color:#b42318}.muted{color:#667085}.pill{padding:2px 8px;border-radius:999px;background:#eef2ff}
    button{border:1px solid #d0d5dd;background:#fff;border-radius:6px;padding:6px 10px;cursor:pointer}
  </style>
</head>
<body>
<header><strong>sub2apiplus-keeper</strong><span>v{{.version}} · {{.now}}</span></header>
<main>
{{range .targets}}
  <h2>{{.Name}} <span class="pill">account {{.AccountID}}</span></h2>
  <p class="muted">model={{.Model}} · enabled={{.Enabled}} · running={{.Running}} · today={{.DailyKeepaliveCount}} · failures={{.ConsecutiveFailures}} · next={{.NextRunAt}}</p>
  <p>累计 tokens：{{tokens .}} · 累计费用：{{printf "%.8f" (cost .)}}</p>
  <button onclick="fetch('/api/run?target={{.Name}}',{method:'POST'}).then(()=>location.reload())">立即保活一次</button>
  {{if .LastError}}<p class="err">{{.LastError}}</p>{{end}}
  <table>
    <thead><tr><th>时间</th><th>状态</th><th>模型</th><th>用量/费用</th><th>对话内容</th></tr></thead>
    <tbody>
    {{range .Sessions}}
      <tr>
        <td>{{.StartedAt}}<br><span class="muted">{{.CompletedAt}}</span></td>
        <td class="{{if eq .Status "success"}}ok{{else if eq .Status "error"}}err{{end}}">{{.Status}}<br>{{.Error}}</td>
        <td>{{.Model}}<br><span class="muted">{{.Platform}} {{.AccountType}}</span></td>
        <td>in {{.Usage.InputTokens}} / out {{.Usage.OutputTokens}} / total {{.Usage.TotalTokens}}<br>cost {{printf "%.8f" .Billing.ActualCost}}</td>
        <td>
          <details><summary>prompt</summary><pre>{{.Prompt}}</pre></details>
          <details><summary>reply</summary><pre>{{.ReplyText}}</pre></details>
        </td>
      </tr>
    {{end}}
    </tbody>
  </table>
{{else}}
  <p>还没有配置 target。</p>
{{end}}
</main>
</body>
</html>`))
