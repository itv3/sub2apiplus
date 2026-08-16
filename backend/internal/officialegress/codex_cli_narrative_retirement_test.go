package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

const codexCLINarrativeRetirementSHA256 = "5c4eec66d9f1d05bc0a651cace3d9bc4470a008e8bc88abff5dbb591f17832cf"

type codexCLINarrativeRemoval struct {
	Path                string   `json:"path"`
	SHA256BeforeRemoval string   `json:"sha256_before_removal"`
	ReplacementAnchors  []string `json:"replacement_anchors"`
}

type codexCLINonSourceRemoval struct {
	Path                string `json:"path"`
	SHA256BeforeRemoval string `json:"sha256_before_removal"`
}

type codexCLIRetainedProof struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

func TestCodexCLINarrativeRetirementIsFrozenAndComplete(t *testing.T) {
	repositoryRoot := filepath.Clean("../../..")
	receiptPath := filepath.Join(repositoryRoot, "docs/egress/maintenance/codex-cli-0145-to-0147-narrative-retirement.json")
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != codexCLINarrativeRetirementSHA256 {
		t.Fatalf("Codex CLI 历史叙事退役回执漂移：got=%s want=%s", got, codexCLINarrativeRetirementSHA256)
	}

	var receipt struct {
		SchemaVersion string `json:"schema_version"`
		Date          string `json:"date"`
		Scope         struct {
			FromVersion string `json:"from_version"`
			ToVersion   string `json:"to_version"`
		} `json:"scope"`
		Authority struct {
			Path  string `json:"path"`
			Title string `json:"title"`
			Role  string `json:"role"`
		} `json:"authority"`
		RemovedNarrativeFiles   []codexCLINarrativeRemoval `json:"removed_narrative_files"`
		RemovedNonSourceFiles   []codexCLINonSourceRemoval `json:"removed_non_source_files"`
		RetainedMachineProofs   []codexCLIRetainedProof    `json:"retained_machine_proofs"`
		RetainedGeneratedAssets []string                   `json:"retained_generated_assets"`
		RuntimeBehaviorChanged  bool                       `json:"runtime_behavior_changed"`
		Result                  string                     `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "codex-cli-emulation-narrative-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.Scope.FromVersion != "0.145.0" ||
		receipt.Scope.ToVersion != "0.147.0" || receipt.RuntimeBehaviorChanged ||
		receipt.Result != "passed" {
		t.Fatalf("Codex CLI 历史叙事退役回执顶层事实非法：%+v", receipt)
	}
	if receipt.Authority.Path != "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md" ||
		receipt.Authority.Title != "Codex CLI 客户端仿真与版本演进手册" ||
		receipt.Authority.Role == "" {
		t.Fatalf("唯一人类可读权威声明非法：%+v", receipt.Authority)
	}

	guideRaw, err := os.ReadFile(filepath.Join(repositoryRoot, filepath.FromSlash(receipt.Authority.Path)))
	if err != nil {
		t.Fatal(err)
	}
	guideText := string(guideRaw)
	if !bytes.Contains(guideRaw, []byte("# "+receipt.Authority.Title)) {
		t.Fatalf("主手册标题与退役回执不一致：%s", receipt.Authority.Title)
	}

	expectedRemoved := map[string]string{
		"docs/egress/maintenance/ACCEPT_PIPELINE_DEFECTS.md":                "1ca6f85e3248081fda5fee4ba858bef693ef333f9b6377a835222226db5880f1",
		"docs/egress/maintenance/CAPTURE_OPERATIONS_NOTES.md":               "09ecbd65f1a090c57b9377f828ac9b7ba140e786cbb1dfb9d386c1538d4b3068",
		"docs/egress/maintenance/CHG-01_COMPILER_STATIC_URL_CLOSURE.md":     "187b2fc7c82a1bec7c38947bc0527023447bd78a50ce21a76624a4474d15832c",
		"docs/egress/maintenance/CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md":  "44a1f3203e89dcad7dac9c9f353b3fce6edb133359f1c81edcc929211a33b5a3",
		"docs/egress/maintenance/CODEX_CLI_0145_TO_0147_P0_REPORT.md":       "7ff70c3be27832a744e696f4fd2fbd0eb048b568f18d825f7a83c773952204fc",
		"docs/egress/maintenance/CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md":    "3b00f0539b5fbd29912570c1415d20642549b9615e889de50efdae52a76c0f16",
		"docs/egress/maintenance/OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md": "78e2632764718a60ef6df17dae88e13a3980683d29d40617284e24944f952c75",
		"docs/egress/maintenance/SCN_REALITY_01_SCENARIO_REALITY_GATE.md":   "1939161af64cbe53a0416d9730c65f13e7199fdcf49c4db20d0dfc54d3ee649d",
		"docs/egress/maintenance/SPEC_EP_002_EVIDENCE_BLOCKER.md":           "b567efba9bfd0a73cdfcbdadc5b4830dfffeed62d616292da7d17782d0bd243f",
		"docs/egress/maintenance/SPEC_TLS_003_JUDGMENT_DEFECT.md":           "bca1b6502d169c6d8b9ec4440ff3de83a8d48c783217a8f9025962c372d62ce7",
		"docs/egress/maintenance/画像升级评审.md":                                 "9e2900d2a413ff8bbb7c8fdb76bfb17450464f2d4152e894038be649f6f88198",
	}
	for _, removed := range receipt.RemovedNarrativeFiles {
		expectedSHA, ok := expectedRemoved[removed.Path]
		if !ok || removed.SHA256BeforeRemoval != expectedSHA || len(removed.ReplacementAnchors) == 0 {
			t.Fatalf("历史叙事退役条目非法：%+v", removed)
		}
		delete(expectedRemoved, removed.Path)
		if _, statErr := os.Stat(filepath.Join(repositoryRoot, filepath.FromSlash(removed.Path))); !errors.Is(statErr, os.ErrNotExist) {
			t.Fatalf("已退役历史叙事仍存在或状态异常：path=%s err=%v", removed.Path, statErr)
		}
		for _, anchor := range removed.ReplacementAnchors {
			if !bytes.Contains([]byte(guideText), []byte(anchor)) {
				t.Fatalf("主手册缺少退役内容替代锚点：path=%s anchor=%s", removed.Path, anchor)
			}
		}
	}
	if len(expectedRemoved) != 0 {
		t.Fatalf("退役回执缺少历史叙事路径：%v", expectedRemoved)
	}

	if len(receipt.RemovedNonSourceFiles) != 1 ||
		receipt.RemovedNonSourceFiles[0].Path != "docs/egress/maintenance/.DS_Store" ||
		receipt.RemovedNonSourceFiles[0].SHA256BeforeRemoval != "403e549f847831e4e273bbd817968c05198e66f81dcec7c3821364f842c58b6b" {
		t.Fatalf("非源码文件退役条目非法：%+v", receipt.RemovedNonSourceFiles)
	}
	if _, statErr := os.Stat(filepath.Join(repositoryRoot, "docs/egress/maintenance/.DS_Store")); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("已退役 .DS_Store 仍存在或状态异常：%v", statErr)
	}

	for _, proof := range receipt.RetainedMachineProofs {
		proofRaw, readErr := os.ReadFile(filepath.Join(repositoryRoot, filepath.FromSlash(proof.Path)))
		if readErr != nil {
			t.Fatalf("读取保留机器回执 %s：%v", proof.Path, readErr)
		}
		if got := changeset3ReferenceSHA256(proofRaw); got != proof.SHA256 {
			t.Fatalf("保留机器回执漂移：path=%s got=%s want=%s", proof.Path, got, proof.SHA256)
		}
	}
	for _, asset := range receipt.RetainedGeneratedAssets {
		if _, statErr := os.Stat(filepath.Join(repositoryRoot, filepath.FromSlash(asset))); statErr != nil {
			t.Fatalf("保留生成资产不存在：path=%s err=%v", asset, statErr)
		}
	}
	if matches, globErr := filepath.Glob(filepath.Join(repositoryRoot, "docs/egress/maintenance/*.md")); globErr != nil || len(matches) != 0 {
		t.Fatalf("maintenance 目录不得恢复单次升级叙事文档：matches=%v err=%v", matches, globErr)
	}
}
