package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"nhooyr.io/websocket"
)

const (
	clientStatusConnected    = "connected"
	clientStatusWaiting      = "waiting"
	clientStatusExecuting    = "executing"
	clientStatusReconnecting = "reconnecting"
	clientStatusError        = "error"

	codexThreadIDFileName       = "codex-thread-id.txt"
	codexWebSocketReadLimitByte = 16 * 1024 * 1024
)

type persistentExecutorFactory func(*Keeper, TargetConfig) (accountPersistentExecutor, error)

type managedPersistentExecutor struct {
	key      string
	executor accountPersistentExecutor
}

type accountPersistentExecutor interface {
	Ensure(context.Context, persistentExecution) error
	Execute(context.Context, persistentExecution) (persistentExecutionResult, error)
	Close() error
}

type persistentExecution struct {
	Account         TargetConfig
	Layout          runtimeLayout
	Prompt          string
	SessionID       string
	StdoutPath      string
	StderrPath      string
	LastMessagePath string
	CanResume       bool
	Timeout         time.Duration
}

type persistentExecutionResult struct {
	ExitCode        int
	Command         []string
	CommandText     string
	WorkDir         string
	ReplyText       string
	Summary         string
	Stdout          string
	Stderr          string
	StdoutPath      string
	StderrPath      string
	LastMessagePath string
	Usage           *usageInfo
	ErrorText       string
}

type classifiedRunError struct {
	err            error
	countAsFailure bool
}

type commandError struct {
	Path     string
	ExitCode int
	Err      error
}

func (e *commandError) Error() string {
	if e == nil {
		return ""
	}
	if e.ExitCode != 0 {
		return fmt.Sprintf("命令执行失败: %s, 退出码 %d", e.Path, e.ExitCode)
	}
	if e.Err != nil {
		return fmt.Sprintf("命令执行失败: %s: %v", e.Path, e.Err)
	}
	return "命令执行失败: " + e.Path
}

func (e *classifiedRunError) Error() string {
	if e == nil || e.err == nil {
		return ""
	}
	return e.err.Error()
}

func (e *classifiedRunError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.err
}

func wrapNonFailureRunError(err error) error {
	if err == nil {
		return nil
	}
	return &classifiedRunError{err: err, countAsFailure: false}
}

func newDefaultPersistentExecutor(k *Keeper, account TargetConfig) (accountPersistentExecutor, error) {
	switch targetExecutor(account) {
	case "codex":
		return &codexPersistentExecutor{keeper: k, path: k.codexPath(), accountID: targetID(account)}, nil
	case "claude":
		return &claudePersistentExecutor{keeper: k, path: k.claudePath(), accountID: targetID(account)}, nil
	default:
		return nil, fmt.Errorf("暂不支持的执行器：%s", targetExecutor(account))
	}
}

func newCommandPersistentExecutor(k *Keeper, _ TargetConfig) (accountPersistentExecutor, error) {
	return commandPersistentExecutor{keeper: k}, nil
}

func (k *Keeper) codexPath() string {
	return "codex"
}

func (k *Keeper) claudePath() string {
	return "claude"
}

func (k *Keeper) ensurePersistentClient(ctx context.Context, account TargetConfig) {
	go func() {
		layout, err := k.prepareRuntime(account)
		if err != nil {
			k.updateClientStatus(account, clientStatusError, "运行时准备失败："+err.Error())
			return
		}
		executor, err := k.persistentExecutorFor(account)
		if err != nil {
			k.updateClientStatus(account, clientStatusError, err.Error())
			return
		}
		req := persistentExecution{
			Account:   account,
			Layout:    layout,
			CanResume: k.canResumeAccount(account),
			Timeout:   persistentTimeout(account),
		}
		if err := executor.Ensure(ctx, req); err != nil {
			k.updateClientStatus(account, clientStatusError, err.Error())
		}
	}()
}

func (k *Keeper) executeAccountPersistent(ctx context.Context, account TargetConfig, prompt string, sessionID string) (Session, error) {
	layout, err := k.prepareRuntime(account)
	if err != nil {
		return Session{Error: err.Error(), Summary: "运行时准备失败"}, err
	}

	stdoutPath := filepath.Join(layout.LogDir, sessionID+".persistent.stdout.log")
	stderrPath := filepath.Join(layout.LogDir, sessionID+".persistent.stderr.log")
	lastMessagePath := filepath.Join(layout.SessionDir, sessionID+".persistent.last.txt")

	executor, err := k.persistentExecutorFor(account)
	if err != nil {
		return Session{Error: err.Error(), Summary: "常驻客户端准备失败"}, err
	}

	req := persistentExecution{
		Account:         account,
		Layout:          layout,
		Prompt:          prompt,
		SessionID:       sessionID,
		StdoutPath:      stdoutPath,
		StderrPath:      stderrPath,
		LastMessagePath: lastMessagePath,
		CanResume:       k.canResumeAccount(account),
		Timeout:         persistentTimeout(account),
	}
	result, runErr := executor.Execute(ctx, req)
	if targetExecutor(account) == "codex" && runErr != nil && isRetryableCodexPersistentError(runErr) {
		k.updateClientStatus(account, clientStatusReconnecting, "Codex 常驻客户端连接异常，正在重建后重试")
		k.resetPersistentExecutor(targetID(account))
		executor, err = k.persistentExecutorFor(account)
		if err != nil {
			return Session{Error: err.Error(), Summary: "常驻客户端重建失败"}, err
		}
		result, runErr = executor.Execute(ctx, req)
		if runErr != nil && isRetryableCodexPersistentError(runErr) {
			runErr = wrapNonFailureRunError(runErr)
		}
	}

	replyText := readOptionalText(result.LastMessagePath)
	if replyText == "" {
		replyText = result.ReplyText
	}
	if replyText == "" {
		parsedReply, usage := parseClientOutput(targetExecutor(account), result.Stdout)
		replyText = parsedReply
		if result.Usage == nil {
			result.Usage = usage
		}
	}
	if replyText == "" && runErr == nil {
		replyText = firstNonEmptyMeaningful(result.Stdout, result.Stderr)
	}
	summary := result.Summary
	if summary == "" {
		summary = summarizeResult(replyText, result.Stdout, result.Stderr)
	}
	if runErr != nil {
		if structuredSummary := summarizeStructuredStdout(result.Stdout); structuredSummary != "" {
			summary = structuredSummary
		}
	}
	session := Session{
		TargetName:      account.Name,
		AccountID:       account.AccountID,
		AccountName:     account.Name,
		Platform:        account.Platform,
		AccountType:     account.AccountType,
		Model:           account.Model,
		Mode:            normalizeMode(account.Mode),
		Prompt:          prompt,
		ExitCode:        result.ExitCode,
		Command:         result.Command,
		CommandText:     result.CommandText,
		WorkDir:         firstNonEmptyMeaningful(result.WorkDir, account.WorkspacePath),
		ReplyText:       truncate(strings.TrimSpace(replyText), 12000),
		Summary:         summary,
		Stdout:          truncate(strings.TrimSpace(result.Stdout), 16000),
		Stderr:          truncate(strings.TrimSpace(result.Stderr), 16000),
		StdoutPath:      result.StdoutPath,
		StderrPath:      result.StderrPath,
		LastMessagePath: result.LastMessagePath,
		Usage:           usageToSessionUsage(result.Usage),
	}
	if len(session.Command) == 0 {
		session.Command = []string{targetExecutor(account)}
		session.CommandText = targetExecutor(account)
	}
	if runErr != nil {
		session.Error = firstNonEmptyMeaningful(result.ErrorText, session.Summary, runErr.Error(), result.Stderr, replyText, result.Stdout)
		return session, runErr
	}
	return session, nil
}

func (k *Keeper) persistentExecutorFor(account TargetConfig) (accountPersistentExecutor, error) {
	if k.persistentFactory == nil {
		k.persistentFactory = newDefaultPersistentExecutor
	}
	key := persistentExecutorKey(account)
	k.persistentMu.Lock()
	defer k.persistentMu.Unlock()
	if k.persistentExecutors == nil {
		k.persistentExecutors = map[string]*managedPersistentExecutor{}
	}
	if managed := k.persistentExecutors[targetID(account)]; managed != nil && managed.key == key {
		return managed.executor, nil
	}
	if managed := k.persistentExecutors[targetID(account)]; managed != nil {
		_ = managed.executor.Close()
		delete(k.persistentExecutors, targetID(account))
	}
	executor, err := k.persistentFactory(k, account)
	if err != nil {
		return nil, err
	}
	k.persistentExecutors[targetID(account)] = &managedPersistentExecutor{key: key, executor: executor}
	return executor, nil
}

func persistentExecutorKey(account TargetConfig) string {
	parts := []string{
		targetID(account),
		targetExecutor(account),
		account.BaseURL,
		account.Model,
		account.WorkspacePath,
		normalizeMode(account.Mode),
		fmt.Sprintf("%x", stableHash(account.APIKey)),
	}
	return strings.Join(parts, "\x00")
}

func persistentTimeout(account TargetConfig) time.Duration {
	timeoutSeconds := maxInt(account.TimeoutSeconds, defaultTimeoutSeconds)
	if targetExecutor(account) == "claude" && timeoutSeconds < minClaudeTimeoutSeconds {
		timeoutSeconds = minClaudeTimeoutSeconds
	}
	return time.Duration(timeoutSeconds) * time.Second
}

func (k *Keeper) canResumeAccount(account TargetConfig) bool {
	k.mu.Lock()
	defer k.mu.Unlock()
	if state := k.targetStateForPersistentLocked(account); state != nil {
		return hasSuccessfulSession(state.Sessions)
	}
	return false
}

func (k *Keeper) targetStateForPersistentLocked(target TargetConfig) *TargetState {
	if state := k.state.Targets[target.Name]; state != nil {
		return state
	}
	return k.targetStateByPersistentIDLocked(targetID(target))
}

func (k *Keeper) targetStateByPersistentIDLocked(id string) *TargetState {
	if k.state.Targets == nil {
		return nil
	}
	for _, state := range k.state.Targets {
		if state == nil {
			continue
		}
		if strconv.FormatInt(state.AccountID, 10) == id || state.Name == id {
			return state
		}
	}
	return k.state.Targets[id]
}

func (k *Keeper) stopInactivePersistentExecutors(active map[string]bool) {
	var toClose []struct {
		id       string
		executor accountPersistentExecutor
	}
	k.persistentMu.Lock()
	for id, managed := range k.persistentExecutors {
		if active[id] || k.accountRunning(id) {
			continue
		}
		toClose = append(toClose, struct {
			id       string
			executor accountPersistentExecutor
		}{id: id, executor: managed.executor})
		delete(k.persistentExecutors, id)
	}
	k.persistentMu.Unlock()
	for _, item := range toClose {
		_ = item.executor.Close()
		k.clearClientStatusByID(item.id)
	}
}

func (k *Keeper) resetPersistentExecutor(accountID string) {
	var executor accountPersistentExecutor
	k.persistentMu.Lock()
	if managed := k.persistentExecutors[accountID]; managed != nil {
		executor = managed.executor
		delete(k.persistentExecutors, accountID)
	}
	k.persistentMu.Unlock()
	if executor != nil {
		_ = executor.Close()
	}
}

func (k *Keeper) reconcilePersistentExecutors(accounts []TargetConfig) {
	active := map[string]bool{}
	for _, account := range accounts {
		if account.Enabled {
			active[targetID(account)] = true
		}
	}
	k.stopInactivePersistentExecutors(active)
}

func (k *Keeper) closeAllPersistentExecutors() {
	k.persistentMu.Lock()
	executors := make([]accountPersistentExecutor, 0, len(k.persistentExecutors))
	for id, managed := range k.persistentExecutors {
		executors = append(executors, managed.executor)
		delete(k.persistentExecutors, id)
	}
	k.persistentMu.Unlock()
	for _, executor := range executors {
		_ = executor.Close()
	}
}

func (k *Keeper) accountRunning(accountID string) bool {
	k.mu.Lock()
	defer k.mu.Unlock()
	if state := k.targetStateByPersistentIDLocked(accountID); state != nil {
		return state.Running
	}
	return false
}

func (k *Keeper) updateClientStatus(account TargetConfig, status string, detail string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	state := k.targetStateLocked(account)
	if state.ClientStatus == status && state.ClientStatusDetail == detail && !(status == clientStatusConnected && state.ClientConnectedAt.IsZero()) {
		return
	}
	state.ClientStatus = status
	state.ClientStatusDetail = strings.TrimSpace(detail)
	if status == clientStatusConnected || status == clientStatusWaiting || status == clientStatusExecuting {
		if state.ClientConnectedAt.IsZero() {
			state.ClientConnectedAt = time.Now().In(k.location)
		}
	}
	if status == clientStatusError {
		state.ClientConnectedAt = time.Time{}
	}
	k.saveStateLocked()
}

func (k *Keeper) clearClientStatusByID(accountID string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if state := k.targetStateByPersistentIDLocked(accountID); state != nil {
		state.ClientStatus = ""
		state.ClientStatusDetail = ""
		state.ClientConnectedAt = time.Time{}
		k.saveStateLocked()
	}
}

func (k *Keeper) updateClientStatusByID(accountID string, status string, detail string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if state := k.targetStateByPersistentIDLocked(accountID); state != nil {
		state.ClientStatus = status
		state.ClientStatusDetail = strings.TrimSpace(detail)
		if status == clientStatusError {
			state.ClientConnectedAt = time.Time{}
		}
		k.saveStateLocked()
	}
}

type commandPersistentExecutor struct {
	keeper *Keeper
}

func (e commandPersistentExecutor) Ensure(context.Context, persistentExecution) error {
	return nil
}

func (e commandPersistentExecutor) Execute(ctx context.Context, req persistentExecution) (persistentExecutionResult, error) {
	_ = ctx
	_ = req
	return persistentExecutionResult{}, fmt.Errorf("短命令执行器已禁用")
}

func (e commandPersistentExecutor) Close() error {
	return nil
}

func persistentResultFromSession(session Session) persistentExecutionResult {
	return persistentExecutionResult{
		ExitCode:        session.ExitCode,
		Command:         session.Command,
		CommandText:     session.CommandText,
		WorkDir:         session.WorkDir,
		ReplyText:       session.ReplyText,
		Summary:         session.Summary,
		Stdout:          session.Stdout,
		Stderr:          session.Stderr,
		StdoutPath:      session.StdoutPath,
		StderrPath:      session.StderrPath,
		LastMessagePath: session.LastMessagePath,
	}
}

type lineFiles struct {
	stdoutFile *os.File
	stderrFile *os.File
	stdoutBuf  limitedBuffer
	stderrBuf  limitedBuffer
}

func openLineFiles(stdoutPath, stderrPath string) (*lineFiles, error) {
	if err := os.MkdirAll(filepath.Dir(stdoutPath), 0755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(stderrPath), 0755); err != nil {
		return nil, err
	}
	stdoutFile, err := os.Create(stdoutPath)
	if err != nil {
		return nil, err
	}
	stderrFile, err := os.Create(stderrPath)
	if err != nil {
		_ = stdoutFile.Close()
		return nil, err
	}
	files := &lineFiles{
		stdoutFile: stdoutFile,
		stderrFile: stderrFile,
	}
	files.stdoutBuf.max = defaultMaxOutputBytes
	files.stderrBuf.max = defaultMaxOutputBytes
	return files, nil
}

func (f *lineFiles) Close() {
	if f == nil {
		return
	}
	if f.stdoutFile != nil {
		_ = f.stdoutFile.Close()
	}
	if f.stderrFile != nil {
		_ = f.stderrFile.Close()
	}
}

func (f *lineFiles) writeStdoutLine(line string) {
	if f == nil {
		return
	}
	_, _ = io.WriteString(io.MultiWriter(f.stdoutFile, &f.stdoutBuf), line+"\n")
}

func (f *lineFiles) writeStderrLine(line string) {
	if f == nil {
		return
	}
	_, _ = io.WriteString(io.MultiWriter(f.stderrFile, &f.stderrBuf), line+"\n")
}

func scanPipeLines(reader io.Reader, lines chan<- string) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		select {
		case lines <- scanner.Text():
		default:
		}
	}
}

func drainLines(lines <-chan string, write func(string)) {
	for {
		select {
		case line, ok := <-lines:
			if !ok {
				return
			}
			write(line)
		default:
			return
		}
	}
}

func processExitError(path string, err error) error {
	if err == nil {
		return fmt.Errorf("常驻客户端已退出：%s", path)
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return &commandError{Path: path, ExitCode: exitErr.ExitCode(), Err: err}
	}
	return &commandError{Path: path, Err: err}
}

func isRetryableCodexPersistentError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, net.ErrClosed) {
		return true
	}
	if websocket.CloseStatus(err) != -1 {
		return true
	}
	text := strings.ToLower(strings.TrimSpace(err.Error()))
	for _, marker := range []string{
		"failed to get reader",
		"unexpected rsv bits",
		"failed to read frame header",
		"read limited",
		"failed to write frame",
		"use of closed network connection",
		"connection reset by peer",
		"broken pipe",
		"codex websocket 未连接",
		"websocket closed",
	} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

type claudePersistentExecutor struct {
	keeper    *Keeper
	path      string
	accountID string

	runMu sync.Mutex
	mu    sync.Mutex

	cmd         *exec.Cmd
	cancel      context.CancelFunc
	stdin       io.WriteCloser
	stdoutLines chan string
	stderrLines chan string
	exitErr     error
	closed      bool
	args        []string
	env         []string
}

func (c *claudePersistentExecutor) Ensure(ctx context.Context, req persistentExecution) error {
	c.runMu.Lock()
	defer c.runMu.Unlock()
	return c.ensureStarted(ctx, req)
}

func (c *claudePersistentExecutor) Execute(ctx context.Context, req persistentExecution) (persistentExecutionResult, error) {
	c.runMu.Lock()
	defer c.runMu.Unlock()
	result := c.commandResult(req)
	files, err := openLineFiles(req.StdoutPath, req.StderrPath)
	if err != nil {
		return result, err
	}
	defer files.Close()

	if err := c.ensureStarted(ctx, req); err != nil {
		result.Stderr = files.stderrBuf.String()
		result.ErrorText = err.Error()
		result.Summary = summarizeResult(result.ErrorText, result.Stdout, result.Stderr)
		return result, err
	}
	result = c.commandResult(req)
	c.keeper.updateClientStatus(req.Account, clientStatusExecuting, "Claude 常驻客户端执行中")
	runCtx, cancel := context.WithCancel(ctx)
	if req.Timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, req.Timeout)
	}
	defer cancel()

	stdin, stdoutLines, stderrLines := c.processPipes()
	drainLines(stderrLines, files.writeStderrLine)
	input := map[string]any{
		"type": "user",
		"message": map[string]any{
			"role":    "user",
			"content": req.Prompt,
		},
	}
	rawInput, _ := json.Marshal(input)
	if _, err := stdin.Write(append(rawInput, '\n')); err != nil {
		c.stopProcess()
		c.keeper.updateClientStatus(req.Account, clientStatusError, "Claude 常驻客户端写入失败："+err.Error())
		result.Stderr = files.stderrBuf.String()
		return result, err
	}

	var reply string
	var usage *usageInfo
	var upstreamErr string
	for {
		select {
		case <-runCtx.Done():
			c.stopProcess()
			errorText := "Claude 常驻客户端超时：" + runCtx.Err().Error()
			c.keeper.updateClientStatus(req.Account, clientStatusError, errorText)
			result.Stdout = files.stdoutBuf.String()
			result.Stderr = files.stderrBuf.String()
			result.ErrorText = firstNonEmptyMeaningful(summarizeStructuredStdoutForClient("claude", result.Stdout), upstreamErr, errorText)
			result.Summary = summarizeResult(firstNonEmptyMeaningful(result.ErrorText, reply), result.Stdout, result.Stderr)
			return result, runCtx.Err()
		case line, ok := <-stderrLines:
			if !ok {
				stderrLines = nil
				continue
			}
			files.writeStderrLine(line)
		case line, ok := <-stdoutLines:
			if !ok {
				err := processExitError(c.path, c.currentExitErr())
				c.keeper.updateClientStatus(req.Account, clientStatusError, err.Error())
				result.Stdout = files.stdoutBuf.String()
				result.Stderr = files.stderrBuf.String()
				result.ErrorText = firstNonEmptyMeaningful(upstreamErr, summarizeStructuredStdoutForClient("claude", result.Stdout), err.Error())
				result.Summary = summarizeResult(firstNonEmptyMeaningful(reply, result.ErrorText), result.Stdout, result.Stderr)
				return result, err
			}
			files.writeStdoutLine(line)
			event := parseClaudeStreamLine(line)
			if event.reply != "" {
				reply = event.reply
			}
			if event.usage != nil {
				usage = event.usage
			}
			if event.done {
				if event.isError {
					upstreamErr = firstNonEmptyMeaningful(event.reply, event.errorText)
					result.ExitCode = 1
				}
				result.Stdout = files.stdoutBuf.String()
				result.Stderr = files.stderrBuf.String()
				result.ReplyText = reply
				result.Usage = usage
				result.ErrorText = upstreamErr
				result.Summary = summarizeResult(firstNonEmptyMeaningful(reply, upstreamErr), result.Stdout, result.Stderr)
				result.StdoutPath = req.StdoutPath
				result.StderrPath = req.StderrPath
				result.LastMessagePath = req.LastMessagePath
				if reply != "" {
					_ = os.MkdirAll(filepath.Dir(req.LastMessagePath), 0755)
					_ = os.WriteFile(req.LastMessagePath, []byte(reply), 0600)
				}
				c.keeper.updateClientStatus(req.Account, clientStatusWaiting, "客户端已连接，等待下次")
				if upstreamErr != "" {
					return result, &commandError{Path: c.path, ExitCode: 1}
				}
				return result, nil
			}
		}
	}
}

func (c *claudePersistentExecutor) Close() error {
	c.stopProcess()
	return nil
}

func (c *claudePersistentExecutor) ensureStarted(ctx context.Context, req persistentExecution) error {
	c.mu.Lock()
	if c.cmd != nil {
		c.mu.Unlock()
		c.keeper.updateClientStatus(req.Account, clientStatusConnected, "Claude 常驻客户端已连接")
		return nil
	}
	c.closed = false
	c.exitErr = nil
	c.mu.Unlock()

	c.keeper.updateClientStatus(req.Account, clientStatusReconnecting, "正在启动 Claude 常驻客户端")
	path, err := exec.LookPath(c.path)
	if err != nil {
		return fmt.Errorf("找不到命令：%s", c.path)
	}
	args := claudePersistentArgs(req)
	env := claudePersistentEnv(req)
	processCtx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(processCtx, path, args...)
	cmd.Dir = req.Account.WorkspacePath
	cmd.Env = append(os.Environ(), env...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		cancel()
		return err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return err
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		cancel()
		return err
	}
	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}
	stdoutLines := make(chan string, 1024)
	stderrLines := make(chan string, 1024)
	go func() {
		scanPipeLines(stdoutPipe, stdoutLines)
		close(stdoutLines)
	}()
	go func() {
		scanPipeLines(stderrPipe, stderrLines)
		close(stderrLines)
	}()

	c.mu.Lock()
	c.cmd = cmd
	c.cancel = cancel
	c.stdin = stdin
	c.stdoutLines = stdoutLines
	c.stderrLines = stderrLines
	c.args = args
	c.env = env
	c.mu.Unlock()

	go c.waitProcess(cmd)
	c.keeper.updateClientStatus(req.Account, clientStatusConnected, "Claude 常驻客户端已连接")
	_ = ctx
	return nil
}

func claudePersistentArgs(req persistentExecution) []string {
	args := []string{"--bare", "--verbose", "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--model", req.Account.Model}
	if betas := claudeBetasForModel(req.Account.Model); len(betas) > 0 {
		args = append(args, "--betas")
		args = append(args, betas...)
	}
	if normalizeMode(req.Account.Mode) == "resume_last" && req.CanResume {
		args = append(args, "-c")
	}
	return args
}

func claudePersistentEnv(req persistentExecution) []string {
	env := []string{
		"HOME=" + req.Layout.HomeDir,
		"CLAUDE_CONFIG_DIR=" + req.Layout.ClaudeConfigDir,
		"ANTHROPIC_API_KEY=" + req.Account.APIKey,
		"ANTHROPIC_AUTH_TOKEN=" + req.Account.APIKey,
		"ANTHROPIC_BASE_URL=" + req.Account.BaseURL,
		"ANTHROPIC_MODEL=" + req.Account.Model,
	}
	if betas := claudeBetasForModel(req.Account.Model); len(betas) > 0 {
		env = append(env, "ANTHROPIC_BETAS="+strings.Join(betas, ","))
	}
	return env
}

func (c *claudePersistentExecutor) commandResult(req persistentExecution) persistentExecutionResult {
	args := c.args
	env := c.env
	if len(args) == 0 {
		args = claudePersistentArgs(req)
		env = claudePersistentEnv(req)
	}
	_ = env
	return persistentExecutionResult{
		Command:         append([]string{c.path}, args...),
		CommandText:     shellJoin(c.path, args),
		WorkDir:         req.Account.WorkspacePath,
		StdoutPath:      req.StdoutPath,
		StderrPath:      req.StderrPath,
		LastMessagePath: req.LastMessagePath,
	}
}

func (c *claudePersistentExecutor) processPipes() (io.WriteCloser, <-chan string, <-chan string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stdin, c.stdoutLines, c.stderrLines
}

func (c *claudePersistentExecutor) waitProcess(cmd *exec.Cmd) {
	err := cmd.Wait()
	c.mu.Lock()
	if c.cmd == cmd {
		c.cmd = nil
		c.cancel = nil
		c.stdin = nil
		c.exitErr = err
	}
	closed := c.closed
	c.mu.Unlock()
	if !closed {
		c.keeper.updateClientStatusByID(c.accountID, clientStatusError, processExitError(c.path, err).Error())
	}
}

func (c *claudePersistentExecutor) currentExitErr() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.exitErr
}

func (c *claudePersistentExecutor) stopProcess() {
	c.mu.Lock()
	cancel := c.cancel
	stdin := c.stdin
	c.closed = true
	c.cmd = nil
	c.cancel = nil
	c.stdin = nil
	c.mu.Unlock()
	if stdin != nil {
		_ = stdin.Close()
	}
	if cancel != nil {
		cancel()
	}
}

type claudeStreamParseResult struct {
	reply     string
	errorText string
	usage     *usageInfo
	done      bool
	isError   bool
}

type claudeStreamUsage struct {
	InputTokens              int64 `json:"input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
}

func parseClaudeStreamLine(line string) claudeStreamParseResult {
	var event struct {
		Type    string             `json:"type"`
		Subtype string             `json:"subtype"`
		IsError bool               `json:"is_error"`
		Result  string             `json:"result"`
		Session string             `json:"session_id"`
		Usage   *claudeStreamUsage `json:"usage"`
		Message *struct {
			Usage   *claudeStreamUsage `json:"usage"`
			Content []struct {
				Type string `json:"type"`
				Text string `json:"text"`
			} `json:"content"`
		} `json:"message"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal([]byte(line), &event); err != nil {
		return claudeStreamParseResult{}
	}
	var out claudeStreamParseResult
	if event.Message != nil {
		for _, content := range event.Message.Content {
			if content.Type == "text" && strings.TrimSpace(content.Text) != "" {
				out.reply = strings.TrimSpace(content.Text)
			}
		}
		if event.Message.Usage != nil {
			out.usage = usageFromClaudeStream(event.Message.Usage)
		}
	}
	if event.Usage != nil {
		out.usage = usageFromClaudeStream(event.Usage)
	}
	if event.Type == "result" {
		out.done = true
		out.isError = event.IsError || event.Subtype == "error"
		out.reply = strings.TrimSpace(event.Result)
		out.errorText = strings.TrimSpace(event.Error)
	}
	return out
}

func usageFromClaudeStream(usage *claudeStreamUsage) *usageInfo {
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

type codexPersistentExecutor struct {
	keeper    *Keeper
	path      string
	accountID string

	runMu sync.Mutex
	mu    sync.Mutex

	cmd          *exec.Cmd
	cancel       context.CancelFunc
	conn         *websocket.Conn
	listenURL    string
	stderrLines  chan string
	exitErr      error
	closed       bool
	rpcID        int
	threadID     string
	threadLoaded bool
}

type codexAgentMessage struct {
	TurnID string
	Text   string
	Phase  string
}

func (c *codexPersistentExecutor) Ensure(ctx context.Context, req persistentExecution) error {
	c.runMu.Lock()
	defer c.runMu.Unlock()
	return c.ensureStarted(ctx, req)
}

func (c *codexPersistentExecutor) Execute(ctx context.Context, req persistentExecution) (persistentExecutionResult, error) {
	c.runMu.Lock()
	defer c.runMu.Unlock()
	result := c.commandResult(req)
	files, err := openLineFiles(req.StdoutPath, req.StderrPath)
	if err != nil {
		return result, err
	}
	defer files.Close()

	if err := c.ensureStarted(ctx, req); err != nil {
		result.Stderr = files.stderrBuf.String()
		return result, err
	}
	result = c.commandResult(req)
	c.keeper.updateClientStatus(req.Account, clientStatusExecuting, "Codex 常驻客户端执行中")
	runCtx, cancel := context.WithCancel(ctx)
	if req.Timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, req.Timeout)
	}
	defer cancel()
	if err := c.ensureThread(runCtx, req); err != nil {
		c.stopProcess()
		c.keeper.updateClientStatus(req.Account, clientStatusError, "Codex thread 准备失败："+err.Error())
		result.Stderr = files.stderrBuf.String()
		result.ErrorText = err.Error()
		result.Summary = summarizeResult(result.ErrorText, result.Stdout, result.Stderr)
		return result, err
	}
	turnID, err := c.startTurn(runCtx, req, files)
	if err != nil {
		c.stopProcess()
		c.keeper.updateClientStatus(req.Account, clientStatusError, "Codex turn 启动失败："+err.Error())
		result.Stdout = files.stdoutBuf.String()
		result.Stderr = files.stderrBuf.String()
		result.ErrorText = err.Error()
		result.Summary = summarizeResult(result.ErrorText, result.Stdout, result.Stderr)
		return result, err
	}

	var reply string
	var usage *usageInfo
	var turnErr string
	stderrLines := c.processLines()
	for {
		select {
		case <-runCtx.Done():
			c.stopProcess()
			c.keeper.updateClientStatus(req.Account, clientStatusError, "Codex 常驻客户端超时："+runCtx.Err().Error())
			result.Stdout = files.stdoutBuf.String()
			result.Stderr = files.stderrBuf.String()
			result.ReplyText = reply
			result.Usage = usage
			result.ErrorText = runCtx.Err().Error()
			result.Summary = summarizeResult(firstNonEmptyMeaningful(reply, result.ErrorText), result.Stdout, result.Stderr)
			return result, runCtx.Err()
		case line, ok := <-stderrLines:
			if !ok {
				stderrLines = nil
				continue
			}
			files.writeStderrLine(line)
		default:
			raw, msg, err := c.readRPC(runCtx)
			if err != nil {
				c.stopProcess()
				c.keeper.updateClientStatus(req.Account, clientStatusError, "Codex 常驻客户端连接异常："+err.Error())
				result.Stdout = files.stdoutBuf.String()
				result.Stderr = files.stderrBuf.String()
				result.ReplyText = reply
				result.Usage = usage
				result.ErrorText = err.Error()
				result.Summary = summarizeResult(firstNonEmptyMeaningful(reply, result.ErrorText), result.Stdout, result.Stderr)
				return result, err
			}
			files.writeStdoutLine(raw)
			if msg.Method == "" {
				continue
			}
			method := normalizeRPCMethod(msg.Method)
			switch method {
			case "item/completed":
				event := parseCodexItemCompleted(msg.Params)
				if event.Text != "" && !strings.EqualFold(event.Phase, "commentary") && (event.TurnID == "" || event.TurnID == turnID) {
					reply = event.Text
				}
			case "thread/tokenUsage/updated":
				eventTurnID, eventUsage := parseCodexTokenUsage(msg.Params)
				if eventUsage != nil && (eventTurnID == "" || eventTurnID == turnID) {
					usage = eventUsage
				}
			case "error":
				eventTurnID, message, willRetry := parseCodexErrorNotification(msg.Params)
				if message != "" && !willRetry && (eventTurnID == "" || eventTurnID == turnID) {
					turnErr = message
				}
			case "turn/completed":
				eventTurnID, status, message := parseCodexTurnCompleted(msg.Params)
				if eventTurnID != "" && eventTurnID != turnID {
					continue
				}
				if message != "" {
					turnErr = message
				} else if status == "completed" {
					turnErr = ""
				}
				if status == "failed" || turnErr != "" {
					result.ExitCode = 1
				}
				result.Stdout = files.stdoutBuf.String()
				result.Stderr = files.stderrBuf.String()
				result.ReplyText = reply
				result.Usage = usage
				result.ErrorText = turnErr
				result.Summary = summarizeResult(firstNonEmptyMeaningful(reply, turnErr), result.Stdout, result.Stderr)
				result.StdoutPath = req.StdoutPath
				result.StderrPath = req.StderrPath
				result.LastMessagePath = req.LastMessagePath
				if reply != "" {
					_ = os.MkdirAll(filepath.Dir(req.LastMessagePath), 0755)
					_ = os.WriteFile(req.LastMessagePath, []byte(reply), 0600)
				}
				c.keeper.updateClientStatus(req.Account, clientStatusWaiting, "客户端已连接，等待下次")
				if turnErr != "" {
					return result, &commandError{Path: c.path, ExitCode: 1}
				}
				return result, nil
			}
		}
	}
}

func (c *codexPersistentExecutor) Close() error {
	c.stopProcess()
	return nil
}

func (c *codexPersistentExecutor) ensureStarted(ctx context.Context, req persistentExecution) error {
	c.mu.Lock()
	if c.cmd != nil && c.conn != nil {
		c.mu.Unlock()
		c.keeper.updateClientStatus(req.Account, clientStatusConnected, "Codex 常驻客户端已连接")
		return nil
	}
	c.closed = false
	c.exitErr = nil
	c.mu.Unlock()

	c.keeper.updateClientStatus(req.Account, clientStatusReconnecting, "正在启动 Codex app-server")
	path, err := exec.LookPath(c.path)
	if err != nil {
		return fmt.Errorf("找不到命令：%s", c.path)
	}
	listenURL, err := freeLoopbackWebSocketURL()
	if err != nil {
		return err
	}
	processCtx, cancel := context.WithCancel(context.Background())
	args := []string{"app-server", "--listen", listenURL}
	cmd := exec.CommandContext(processCtx, path, args...)
	cmd.Dir = req.Account.WorkspacePath
	cmd.Env = append(os.Environ(),
		"HOME="+req.Layout.HomeDir,
		"CODEX_HOME="+req.Layout.CodexHome,
		"OPENAI_API_KEY="+req.Account.APIKey,
	)
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return err
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		cancel()
		return err
	}
	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}
	processLines := make(chan string, 1024)
	go scanPipeLines(stdoutPipe, processLines)
	go scanPipeLines(stderrPipe, processLines)

	c.mu.Lock()
	c.cmd = cmd
	c.cancel = cancel
	c.listenURL = listenURL
	c.stderrLines = processLines
	c.threadLoaded = false
	c.mu.Unlock()

	go c.waitProcess(cmd)
	conn, err := c.dial(ctx, listenURL)
	if err != nil {
		c.stopProcess()
		return err
	}
	c.mu.Lock()
	c.conn = conn
	c.mu.Unlock()
	if err := c.initialize(ctx); err != nil {
		c.stopProcess()
		return err
	}
	c.keeper.updateClientStatus(req.Account, clientStatusConnected, "Codex 常驻客户端已连接")
	return nil
}

func (c *codexPersistentExecutor) commandResult(req persistentExecution) persistentExecutionResult {
	args := []string{"app-server"}
	if c.listenURL != "" {
		args = append(args, "--listen", c.listenURL)
	}
	return persistentExecutionResult{
		Command:         append([]string{c.path}, args...),
		CommandText:     shellJoin(c.path, args),
		WorkDir:         req.Account.WorkspacePath,
		StdoutPath:      req.StdoutPath,
		StderrPath:      req.StderrPath,
		LastMessagePath: req.LastMessagePath,
	}
}

func (c *codexPersistentExecutor) dial(ctx context.Context, listenURL string) (*websocket.Conn, error) {
	deadline := time.Now().Add(15 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		dialCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		conn, _, err := websocket.Dial(dialCtx, listenURL, nil)
		cancel()
		if err == nil {
			conn.SetReadLimit(codexWebSocketReadLimitByte)
			return conn, nil
		}
		lastErr = err
		time.Sleep(200 * time.Millisecond)
	}
	if lastErr == nil {
		lastErr = context.DeadlineExceeded
	}
	return nil, lastErr
}

func (c *codexPersistentExecutor) initialize(ctx context.Context) error {
	_, err := c.rpcCall(ctx, "initialize", map[string]any{
		"clientInfo": map[string]any{
			"name":    "ai-keeper",
			"title":   "ai-keeper",
			"version": version,
		},
		"capabilities": map[string]any{
			"experimentalApi":    true,
			"requestAttestation": false,
		},
	})
	if err != nil {
		return err
	}
	return c.rpcNotify(ctx, "initialized", map[string]any{})
}

func (c *codexPersistentExecutor) ensureThread(ctx context.Context, req persistentExecution) error {
	if normalizeMode(req.Account.Mode) == "fresh" {
		threadID, err := c.startThread(ctx, req)
		if err != nil {
			return err
		}
		c.mu.Lock()
		c.threadID = threadID
		c.mu.Unlock()
		return nil
	}

	c.mu.Lock()
	threadID := c.threadID
	threadLoaded := c.threadLoaded
	c.mu.Unlock()
	if threadID != "" && threadLoaded {
		return nil
	}
	if threadID == "" {
		threadID = strings.TrimSpace(readOptionalText(filepath.Join(req.Layout.SessionDir, codexThreadIDFileName)))
	}
	if threadID != "" {
		if err := c.resumeThread(ctx, req, threadID); err == nil {
			c.mu.Lock()
			c.threadID = threadID
			c.threadLoaded = true
			c.mu.Unlock()
			return nil
		} else if isRetryableCodexPersistentError(err) {
			c.discardThread(req, threadID)
			return err
		} else {
			c.discardThread(req, threadID)
		}
	}
	threadID, err := c.startThread(ctx, req)
	if err != nil {
		return err
	}
	c.mu.Lock()
	c.threadID = threadID
	c.threadLoaded = true
	c.mu.Unlock()
	_ = os.MkdirAll(req.Layout.SessionDir, 0755)
	_ = os.WriteFile(filepath.Join(req.Layout.SessionDir, codexThreadIDFileName), []byte(threadID+"\n"), 0600)
	return nil
}

func (c *codexPersistentExecutor) startThread(ctx context.Context, req persistentExecution) (string, error) {
	result, err := c.rpcCall(ctx, "thread/start", codexThreadParams(req, ""))
	if err != nil {
		return "", err
	}
	threadID := parseCodexThreadID(result)
	if threadID == "" {
		return "", fmt.Errorf("Codex thread/start 未返回 thread id")
	}
	return threadID, nil
}

func (c *codexPersistentExecutor) resumeThread(ctx context.Context, req persistentExecution, threadID string) error {
	_, err := c.rpcCall(ctx, "thread/resume", codexThreadParams(req, threadID))
	return err
}

func (c *codexPersistentExecutor) discardThread(req persistentExecution, threadID string) {
	c.mu.Lock()
	if c.threadID == threadID || c.threadID == "" {
		c.threadID = ""
		c.threadLoaded = false
	}
	c.mu.Unlock()

	threadPath := filepath.Join(req.Layout.SessionDir, codexThreadIDFileName)
	savedThreadID := strings.TrimSpace(readOptionalText(threadPath))
	if savedThreadID == "" || savedThreadID == threadID {
		_ = os.Remove(threadPath)
	}
}

func codexThreadParams(req persistentExecution, threadID string) map[string]any {
	params := map[string]any{
		"model":          req.Account.Model,
		"modelProvider":  "sub2apiplus_keeper_openai",
		"cwd":            req.Account.WorkspacePath,
		"approvalPolicy": "never",
		"sandbox":        "read-only",
	}
	if threadID != "" {
		params["threadId"] = threadID
	}
	return params
}

func (c *codexPersistentExecutor) startTurn(ctx context.Context, req persistentExecution, files *lineFiles) (string, error) {
	c.mu.Lock()
	threadID := c.threadID
	c.mu.Unlock()
	params := map[string]any{
		"threadId": threadID,
		"input": []map[string]any{
			{
				"type":          "text",
				"text":          req.Prompt,
				"text_elements": []any{},
			},
		},
		"cwd":            req.Account.WorkspacePath,
		"approvalPolicy": "never",
		"sandboxPolicy": map[string]any{
			"type":          "readOnly",
			"networkAccess": false,
		},
		"model": req.Account.Model,
	}
	raw, err := c.rpcCallWithLog(ctx, "turn/start", params, files)
	if err != nil {
		return "", err
	}
	turnID := parseCodexTurnStartID(raw)
	if turnID == "" {
		return "", fmt.Errorf("Codex turn/start 未返回 turn id")
	}
	return turnID, nil
}

type codexRPCMessage struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *codexRPCError  `json:"error,omitempty"`
}

type codexRPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *codexRPCError) Error() string {
	if e == nil {
		return ""
	}
	if e.Code != 0 {
		return fmt.Sprintf("Codex RPC 错误 %d：%s", e.Code, e.Message)
	}
	return "Codex RPC 错误：" + e.Message
}

func (c *codexPersistentExecutor) rpcNotify(ctx context.Context, method string, params any) error {
	req := map[string]any{
		"jsonrpc": "2.0",
		"method":  method,
		"params":  params,
	}
	raw, err := json.Marshal(req)
	if err != nil {
		return err
	}
	conn := c.currentConn()
	if conn == nil {
		return fmt.Errorf("Codex WebSocket 未连接")
	}
	return conn.Write(ctx, websocket.MessageText, raw)
}

func (c *codexPersistentExecutor) rpcCall(ctx context.Context, method string, params any) (json.RawMessage, error) {
	return c.rpcCallWithLog(ctx, method, params, nil)
}

func (c *codexPersistentExecutor) rpcCallWithLog(ctx context.Context, method string, params any, files *lineFiles) (json.RawMessage, error) {
	id := c.nextRPCID()
	req := map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"method":  method,
		"params":  params,
	}
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	conn := c.currentConn()
	if conn == nil {
		return nil, fmt.Errorf("Codex WebSocket 未连接")
	}
	if err := conn.Write(ctx, websocket.MessageText, raw); err != nil {
		return nil, err
	}
	for {
		rawLine, msg, err := c.readRPC(ctx)
		if err != nil {
			return nil, err
		}
		if files != nil {
			files.writeStdoutLine(rawLine)
		}
		if !rpcIDEquals(msg.ID, id) {
			continue
		}
		if msg.Error != nil {
			return nil, msg.Error
		}
		return msg.Result, nil
	}
}

func (c *codexPersistentExecutor) readRPC(ctx context.Context) (string, codexRPCMessage, error) {
	conn := c.currentConn()
	if conn == nil {
		return "", codexRPCMessage{}, fmt.Errorf("Codex WebSocket 未连接")
	}
	messageType, raw, err := conn.Read(ctx)
	if err != nil {
		return "", codexRPCMessage{}, err
	}
	if messageType != websocket.MessageText {
		return "", codexRPCMessage{}, nil
	}
	var msg codexRPCMessage
	if err := json.Unmarshal(raw, &msg); err != nil {
		return string(raw), codexRPCMessage{}, err
	}
	return string(raw), msg, nil
}

func (c *codexPersistentExecutor) nextRPCID() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rpcID++
	return c.rpcID
}

func (c *codexPersistentExecutor) currentConn() *websocket.Conn {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.conn
}

func (c *codexPersistentExecutor) processLines() <-chan string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stderrLines
}

func (c *codexPersistentExecutor) waitProcess(cmd *exec.Cmd) {
	err := cmd.Wait()
	c.mu.Lock()
	if c.cmd == cmd {
		c.cmd = nil
		c.cancel = nil
		c.conn = nil
		c.exitErr = err
	}
	closed := c.closed
	c.mu.Unlock()
	if !closed {
		c.keeper.updateClientStatusByID(c.accountID, clientStatusError, processExitError(c.path, err).Error())
	}
}

func (c *codexPersistentExecutor) stopProcess() {
	c.mu.Lock()
	cancel := c.cancel
	conn := c.conn
	c.closed = true
	c.cmd = nil
	c.cancel = nil
	c.conn = nil
	c.threadLoaded = false
	c.mu.Unlock()
	if conn != nil {
		_ = conn.Close(websocket.StatusNormalClosure, "ai-keeper stop")
	}
	if cancel != nil {
		cancel()
	}
}

func freeLoopbackWebSocketURL() (string, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	addr := listener.Addr().String()
	if err := listener.Close(); err != nil {
		return "", err
	}
	return "ws://" + addr, nil
}

func rpcIDEquals(raw json.RawMessage, id int) bool {
	if len(raw) == 0 {
		return false
	}
	return strings.Trim(string(raw), `"`) == strconv.Itoa(id)
}

func normalizeRPCMethod(method string) string {
	method = strings.TrimSpace(method)
	known := []string{
		"item/completed",
		"thread/tokenUsage/updated",
		"turn/completed",
		"error",
	}
	for _, item := range known {
		if method == item || strings.HasSuffix(method, "/"+item) {
			return item
		}
	}
	return method
}

func parseCodexThreadID(raw json.RawMessage) string {
	var response struct {
		Thread struct {
			ID string `json:"id"`
		} `json:"thread"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		return ""
	}
	return strings.TrimSpace(response.Thread.ID)
}

func parseCodexTurnStartID(raw json.RawMessage) string {
	var response struct {
		Turn struct {
			ID string `json:"id"`
		} `json:"turn"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		return ""
	}
	return strings.TrimSpace(response.Turn.ID)
}

func parseCodexItemCompleted(raw json.RawMessage) codexAgentMessage {
	var event struct {
		TurnID string `json:"turnId"`
		Item   struct {
			Type  string `json:"type"`
			Text  string `json:"text"`
			Phase string `json:"phase"`
		} `json:"item"`
	}
	if err := json.Unmarshal(raw, &event); err != nil {
		return codexAgentMessage{}
	}
	if event.Item.Type != "agentMessage" && event.Item.Type != "agent_message" {
		return codexAgentMessage{TurnID: event.TurnID}
	}
	return codexAgentMessage{
		TurnID: event.TurnID,
		Text:   strings.TrimSpace(event.Item.Text),
		Phase:  strings.TrimSpace(event.Item.Phase),
	}
}

func parseCodexTokenUsage(raw json.RawMessage) (string, *usageInfo) {
	var event struct {
		TurnID     string `json:"turnId"`
		TokenUsage struct {
			Last struct {
				InputTokens           int64 `json:"inputTokens"`
				CachedInputTokens     int64 `json:"cachedInputTokens"`
				OutputTokens          int64 `json:"outputTokens"`
				ReasoningOutputTokens int64 `json:"reasoningOutputTokens"`
			} `json:"last"`
		} `json:"tokenUsage"`
	}
	if err := json.Unmarshal(raw, &event); err != nil {
		return "", nil
	}
	usage := normalizeUsage(usageInfo{
		InputTokens:           event.TokenUsage.Last.InputTokens,
		CachedInputTokens:     event.TokenUsage.Last.CachedInputTokens,
		OutputTokens:          event.TokenUsage.Last.OutputTokens,
		ReasoningOutputTokens: event.TokenUsage.Last.ReasoningOutputTokens,
	})
	return event.TurnID, usage
}

func parseCodexErrorNotification(raw json.RawMessage) (string, string, bool) {
	var event struct {
		TurnID    string `json:"turnId"`
		WillRetry bool   `json:"willRetry"`
		Error     struct {
			Message           string `json:"message"`
			AdditionalDetails string `json:"additionalDetails"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &event); err != nil {
		return "", "", false
	}
	return event.TurnID, firstNonEmptyMeaningful(event.Error.Message, event.Error.AdditionalDetails), event.WillRetry
}

func parseCodexTurnCompleted(raw json.RawMessage) (string, string, string) {
	var event struct {
		Turn struct {
			ID     string `json:"id"`
			Status string `json:"status"`
			Error  *struct {
				Message           string `json:"message"`
				AdditionalDetails string `json:"additionalDetails"`
			} `json:"error"`
		} `json:"turn"`
	}
	if err := json.Unmarshal(raw, &event); err != nil {
		return "", "", ""
	}
	message := ""
	if event.Turn.Error != nil {
		message = firstNonEmptyMeaningful(event.Turn.Error.Message, event.Turn.Error.AdditionalDetails)
	}
	return event.Turn.ID, event.Turn.Status, message
}
