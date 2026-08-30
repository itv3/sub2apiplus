package service

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"
)

const codex01491TerminalStateServicePath = "docs/egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json"

type codex01491TerminalServiceTransition struct {
	Path          string   `json:"path"`
	State         string   `json:"state"`
	PriorSHA256s  []string `json:"prior_sha256s"`
	CurrentSHA256 string   `json:"current_sha256"`
}

type codex01491TerminalServiceReceipt struct {
	SchemaVersion string                                `json:"schema_version"`
	Transitions   []codex01491TerminalServiceTransition `json:"transitions"`
	Result        string                                `json:"result"`
}

var (
	codex01491TerminalServiceOnce    sync.Once
	codex01491TerminalServiceCached  codex01491TerminalServiceReceipt
	codex01491TerminalServiceLoadErr error
)

func loadCodex01491TerminalServiceState() (codex01491TerminalServiceReceipt, error) {
	codex01491TerminalServiceOnce.Do(func() {
		raw, err := os.ReadFile(filepath.Join("../../..", codex01491TerminalStateServicePath))
		if err != nil {
			codex01491TerminalServiceLoadErr = err
			return
		}
		if err := json.Unmarshal(raw, &codex01491TerminalServiceCached); err != nil {
			codex01491TerminalServiceLoadErr = err
			return
		}
		if codex01491TerminalServiceCached.SchemaVersion !=
			"official-client-codex-0.149.1-terminal-state/v1" ||
			codex01491TerminalServiceCached.Result != "passed" {
			codex01491TerminalServiceLoadErr = errors.New("0.149.1 终态收据顶层事实非法")
			return
		}
		for _, transition := range codex01491TerminalServiceCached.Transitions {
			current, readErr := os.ReadFile(filepath.Join(
				"../../..", filepath.FromSlash(transition.Path),
			))
			switch transition.State {
			case "present":
				if readErr != nil ||
					upstreamMergeFrameworkServiceDigest(current) != transition.CurrentSHA256 {
					codex01491TerminalServiceLoadErr = errors.New(
						"0.149.1 终态当前摘要不一致：" + transition.Path,
					)
					return
				}
			case "deleted":
				if !os.IsNotExist(readErr) || transition.CurrentSHA256 != "" {
					codex01491TerminalServiceLoadErr = errors.New(
						"0.149.1 终态删除状态不一致：" + transition.Path,
					)
					return
				}
			default:
				codex01491TerminalServiceLoadErr = errors.New("0.149.1 终态文件状态非法")
				return
			}
		}
	})
	return codex01491TerminalServiceCached, codex01491TerminalServiceLoadErr
}

func codex01491TerminalStateSupersedesService(path, priorDigest, currentDigest string) bool {
	receipt, err := loadCodex01491TerminalServiceState()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.CurrentSHA256 == currentDigest &&
			slices.Contains(transition.PriorSHA256s, priorDigest) {
			return true
		}
	}
	return false
}

func TestCodex01491TerminalServiceStateIsFrozen(t *testing.T) {
	if _, err := loadCodex01491TerminalServiceState(); err != nil {
		t.Fatal(err)
	}
}
