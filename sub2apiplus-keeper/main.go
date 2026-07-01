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
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

const version = "0.1.141-0.02"

type Config struct {
	Enabled             bool              `yaml:"enabled"`
	Timezone            string            `yaml:"timezone"`
	ScanIntervalSeconds int               `yaml:"scan_interval_seconds"`
	MaxWorkers          int               `yaml:"max_workers"`
	StatePath           string            `yaml:"state_path"`
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

type TargetConfig struct {
	Name             string `yaml:"name"`
	Enabled          bool   `yaml:"enabled"`
	AccountID        int64  `yaml:"account_id"`
	Model            string `yaml:"model"`
	IntervalMinutes  int    `yaml:"interval_minutes"`
	WorkStart        string `yaml:"work_start"`
	WorkEnd          string `yaml:"work_end"`
	MaxDailyRuns     int    `yaml:"max_daily_runs"`
	MaxOutputTokens  int    `yaml:"max_output_tokens"`
	PromptProfile    string `yaml:"prompt_profile"`
	InitialDelaySecs int    `yaml:"initial_delay_seconds"`
}

type State struct {
	Version string                  `json:"version"`
	Targets map[string]*TargetState `json:"targets"`
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
	LastError               string    `json:"last_error,omitempty"`
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
	Prompt            string    `json:"prompt"`
	ReplyText         string    `json:"reply_text,omitempty"`
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
	InputTokens           int `json:"input_tokens"`
	OutputTokens          int `json:"output_tokens"`
	TotalTokens           int `json:"total_tokens"`
	CacheCreationTokens   int `json:"cache_creation_tokens,omitempty"`
	CacheReadTokens       int `json:"cache_read_tokens,omitempty"`
	CacheCreation5mTokens int `json:"cache_creation_5m_tokens,omitempty"`
	CacheCreation1hTokens int `json:"cache_creation_1h_tokens,omitempty"`
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
	Model           string `json:"model"`
	Prompt          string `json:"prompt"`
	MaxOutputTokens int    `json:"max_output_tokens"`
}

type apiEnvelope struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

type Keeper struct {
	cfg        Config
	location   *time.Location
	httpClient *http.Client
	mu         sync.Mutex
	state      State
	sem        chan struct{}
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
		return Config{}, err
	}
	if cfg.Timezone == "" {
		cfg.Timezone = "Asia/Shanghai"
	}
	if cfg.ScanIntervalSeconds <= 0 {
		cfg.ScanIntervalSeconds = 120
	}
	if cfg.MaxWorkers <= 0 {
		cfg.MaxWorkers = 1
	}
	if cfg.StatePath == "" {
		cfg.StatePath = "data/state.json"
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
		sem: make(chan struct{}, cfg.MaxWorkers),
	}
	_ = k.loadState()
	k.syncTargets()
	return k, nil
}

func (k *Keeper) Run(ctx context.Context) {
	if k.cfg.Enabled {
		k.scan(ctx)
	} else {
		log.Println("keeper 全局开关关闭，仅启动网页")
	}
	ticker := time.NewTicker(time.Duration(k.cfg.ScanIntervalSeconds) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if k.cfg.Enabled {
				k.scan(ctx)
			}
		}
	}
}

func (k *Keeper) scan(ctx context.Context) {
	now := time.Now().In(k.location)
	for _, target := range k.cfg.Targets {
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
	select {
	case k.sem <- struct{}{}:
		defer func() { <-k.sem }()
	case <-ctx.Done():
		return
	}
	startedAt := time.Now().In(k.location)
	prompt := k.buildPrompt(target)
	session := Session{
		ID:         startedAt.Format("20060102T150405.000000000"),
		TargetName: target.Name,
		AccountID:  target.AccountID,
		Model:      target.Model,
		Prompt:     prompt,
		Status:     "running",
		StartedAt:  startedAt,
	}
	k.appendSession(target, session)

	result, err := k.callKeepalive(ctx, target, prompt)
	completedAt := time.Now().In(k.location)
	k.mu.Lock()
	defer k.mu.Unlock()
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
		state.Sessions[last].Status = "error"
		state.Sessions[last].Error = err.Error()
		state.Sessions[last].CompletedAt = completedAt
		state.Sessions[last].LatencyMS = completedAt.Sub(startedAt).Milliseconds()
		state.LastError = err.Error()
		state.ConsecutiveFailures++
		state.NextRunAt = completedAt.Add(k.failureBackoff(state.ConsecutiveFailures))
		k.saveStateLocked()
		return
	}

	state.Sessions[last] = result
	state.Sessions[last].ID = session.ID
	state.Sessions[last].TargetName = target.Name
	state.Sessions[last].Status = "success"
	state.Sessions[last].StartedAt = startedAt
	state.Sessions[last].CompletedAt = completedAt
	if state.Sessions[last].LatencyMS == 0 {
		state.Sessions[last].LatencyMS = completedAt.Sub(startedAt).Milliseconds()
	}
	state.LastError = ""
	state.ConsecutiveFailures = 0
	state.DailyKeepaliveCount++
	state.NextRunAt = completedAt.Add(time.Duration(maxInt(target.IntervalMinutes, 1)) * time.Minute)
	k.trimSessionsLocked(state)
	k.saveStateLocked()
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
	req.Header.Set("Authorization", "Bearer "+k.cfg.Sub2APIPlus.InternalToken)

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

func (k *Keeper) buildPrompt(target TargetConfig) string {
	k.mu.Lock()
	defer k.mu.Unlock()
	state := k.targetStateLocked(target)
	lastReply := ""
	for i := len(state.Sessions) - 1; i >= 0; i-- {
		if state.Sessions[i].Status == "success" && strings.TrimSpace(state.Sessions[i].ReplyText) != "" {
			lastReply = strings.TrimSpace(state.Sessions[i].ReplyText)
			break
		}
	}
	if lastReply != "" {
		return fmt.Sprintf("上一轮建议是：%s\n\n请继续深入一层，补充 2 个更具体的实现或测试注意点。要求简洁，不要展开长篇背景。", truncate(lastReply, 600))
	}
	switch strings.TrimSpace(target.PromptProfile) {
	case "test_design":
		return "请为一个小型配置解析函数设计 3 个边界测试用例，只说明输入、预期输出和测试理由。"
	case "refactor_compare":
		return "比较两种简单实现方案的优缺点：直接 if/else 与表驱动映射。场景是把模型别名映射到真实模型名。请给出简短结论。"
	default:
		return "请审查下面这个小型 Go 函数，指出 2-3 个可读性或边界处理优化点。不要重写完整文件，只给出简短建议。\n\nfunc clampMinutes(v int) int {\n    if v < 1 { return 1 }\n    if v > 1440 { return 1440 }\n    return v\n}"
	}
}

func (k *Keeper) serve(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", k.withAuth(k.handleIndex))
	mux.HandleFunc("/api/state", k.withAuth(k.handleState))
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
	k.mu.Lock()
	view := k.snapshotLocked()
	k.mu.Unlock()
	writeJSON(w, view)
}

func (k *Keeper) handleManualRun(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimSpace(r.URL.Query().Get("target"))
	if name == "" {
		http.Error(w, "target required", http.StatusBadRequest)
		return
	}
	for _, target := range k.cfg.Targets {
		if target.Name == name {
			go k.runTarget(context.Background(), target)
			writeJSON(w, map[string]string{"status": "accepted"})
			return
		}
	}
	http.Error(w, "target not found", http.StatusNotFound)
}

func (k *Keeper) withAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
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
	k.state = state
	return nil
}

func (k *Keeper) syncTargets() {
	k.mu.Lock()
	defer k.mu.Unlock()
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
	return map[string]any{
		"version": version,
		"now":     time.Now().In(k.location),
		"targets": targets,
	}
}

func (k *Keeper) failureBackoff(failures int) time.Duration {
	minutes := 5
	if failures > 1 {
		minutes = minutes << min(failures-1, 4)
	}
	return time.Duration(minutes) * time.Minute
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

func truncate(value string, maxLen int) string {
	runes := []rune(value)
	if len(runes) <= maxLen {
		return value
	}
	return string(runes[:maxLen]) + "..."
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

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
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
