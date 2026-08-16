package service

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

const compatibilityCodeRetirementClosureSHA256 = "82144fed8f781291c8b8c48aeba33da7dcc2237f892faa424fadec102b8122b0"

type compatibilityClosureArtifact struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type compatibilityClosureOccurrence struct {
	Path    string `json:"path"`
	Pattern string `json:"pattern"`
	Count   int    `json:"count"`
}

type compatibilityClosureRetiredCandidate struct {
	ID                        string   `json:"id"`
	RetiredSymbols            []string `json:"retired_symbols"`
	Replacement               string   `json:"replacement"`
	ProductionConsumersBefore int      `json:"production_consumers_before"`
	TestConsumersBefore       int      `json:"test_consumers_before"`
	ProductionConsumersAfter  int      `json:"production_consumers_after"`
	TestConsumersAfter        int      `json:"test_consumers_after"`
}

type compatibilityClosureRetainedCandidate struct {
	ID                  string                           `json:"id"`
	Reason              string                           `json:"reason"`
	RequiredOccurrences []compatibilityClosureOccurrence `json:"required_occurrences"`
}

type compatibilityClosureForbiddenOccurrence struct {
	Path    string `json:"path"`
	Pattern string `json:"pattern"`
}

type compatibilityClosureMarkerOccurrence struct {
	Path   string `json:"path"`
	Marker string `json:"marker"`
}

type compatibilityClosureSourceTransition struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
}

type compatibilityCodeRetirementClosure struct {
	SchemaVersion            string                                    `json:"schema_version"`
	Date                     string                                    `json:"date"`
	BaseCommit               string                                    `json:"base_commit"`
	Scope                    string                                    `json:"scope"`
	PriorRetirementReceipts  []compatibilityClosureArtifact            `json:"prior_retirement_receipts"`
	RetiredCandidates        []compatibilityClosureRetiredCandidate    `json:"retired_candidates"`
	RetainedCandidates       []compatibilityClosureRetainedCandidate   `json:"retained_candidates"`
	ForbiddenOccurrences     []compatibilityClosureForbiddenOccurrence `json:"forbidden_current_occurrences"`
	DiscoveryMarkers         []string                                  `json:"discovery_markers"`
	AllowedMarkerOccurrences []compatibilityClosureMarkerOccurrence    `json:"allowed_marker_occurrences"`
	SourceTransitions        []compatibilityClosureSourceTransition    `json:"source_transitions"`
	CatalogItemRemovals      []string                                  `json:"catalog_item_removals"`
	RemovalReceiptRequired   bool                                      `json:"removal_receipt_required"`
	AllowedWireDeltas        []string                                  `json:"allowed_wire_deltas"`
	ClosureResult            string                                    `json:"closure_result"`
	Verification             []string                                  `json:"verification"`
}

func TestCompatibilityCodeRetirementClosureIsComplete(t *testing.T) {
	repoRoot := filepath.Clean("../../..")
	receiptPath := filepath.Join(
		repoRoot,
		"docs/egress/maintenance/compatibility-code-retirement-closure.json",
	)
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := compatibilityClosureDigest(raw); got != compatibilityCodeRetirementClosureSHA256 {
		t.Fatalf("兼容代码退休闭集收据漂移：got=%s want=%s", got, compatibilityCodeRetirementClosureSHA256)
	}

	var closure compatibilityCodeRetirementClosure
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&closure); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("兼容代码退休闭集收据尾部存在额外 JSON")
	}
	if closure.SchemaVersion != "official-egress.compatibility-code-retirement-closure/v1" ||
		closure.Date != "2026-08-16" || closure.BaseCommit == "" || closure.Scope == "" ||
		len(closure.PriorRetirementReceipts) != 6 || len(closure.RetiredCandidates) != 2 ||
		len(closure.RetainedCandidates) != 6 || len(closure.ForbiddenOccurrences) != 7 ||
		len(closure.DiscoveryMarkers) != 6 || len(closure.AllowedMarkerOccurrences) != 6 ||
		len(closure.SourceTransitions) != 21 || len(closure.CatalogItemRemovals) != 0 ||
		closure.RemovalReceiptRequired || len(closure.AllowedWireDeltas) != 0 ||
		closure.ClosureResult != "complete" || len(closure.Verification) != 7 {
		t.Fatalf("兼容代码退休闭集事实不完整：%+v", closure)
	}

	for _, retired := range closure.RetiredCandidates {
		if retired.ID == "" || len(retired.RetiredSymbols) == 0 || retired.Replacement == "" ||
			retired.ProductionConsumersBefore < 0 || retired.TestConsumersBefore < 0 ||
			retired.ProductionConsumersAfter != 0 || retired.TestConsumersAfter != 0 {
			t.Fatalf("退休候选闭集非法：%+v", retired)
		}
	}

	for _, artifact := range closure.PriorRetirementReceipts {
		assertCompatibilityClosureFileDigest(t, repoRoot, artifact.Path, artifact.SHA256)
	}
	for _, transition := range closure.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" || transition.ToSHA256 == "" {
			t.Fatalf("兼容代码源码 transition 不完整：%+v", transition)
		}
		assertCompatibilityClosureFileDigest(t, repoRoot, transition.Path, transition.ToSHA256)
	}

	for _, forbidden := range closure.ForbiddenOccurrences {
		source := readCompatibilityClosureSource(t, repoRoot, forbidden.Path)
		if bytes.Contains(source, []byte(forbidden.Pattern)) {
			t.Fatalf("已退休兼容代码重新出现：path=%s pattern=%q", forbidden.Path, forbidden.Pattern)
		}
	}

	for _, retained := range closure.RetainedCandidates {
		if retained.ID == "" || retained.Reason == "" || len(retained.RequiredOccurrences) == 0 {
			t.Fatalf("必须保留候选缺少机器事实：%+v", retained)
		}
		for _, occurrence := range retained.RequiredOccurrences {
			source := readCompatibilityClosureSource(t, repoRoot, occurrence.Path)
			if got := bytes.Count(source, []byte(occurrence.Pattern)); got != occurrence.Count {
				t.Fatalf("必须保留候选消费者闭集漂移：id=%s path=%s pattern=%q got=%d want=%d", retained.ID, occurrence.Path, occurrence.Pattern, got, occurrence.Count)
			}
		}
	}

	assertCompatibilityDiscoveryMarkerClosure(t, repoRoot, closure)

	oauthService := readCompatibilityClosureSource(t, repoRoot, "backend/internal/service/openai_oauth_service.go")
	for _, required := range [][]byte{
		[]byte("OPENAI_OAUTH_REFRESH_DECODER_MISSING"),
		[]byte("OPENAI_OAUTH_RELEASE_RUNTIME_MISSING"),
		[]byte("decoder.DecodeRefreshResponse"),
		[]byte("s.executeOAuthRefresh("),
	} {
		if !bytes.Contains(oauthService, required) {
			t.Fatalf("OAuth refresh 当前失败关闭链缺失：%s", required)
		}
	}
	oauthTests := readCompatibilityClosureSource(t, repoRoot, "backend/internal/service/openai_oauth_service_refresh_test.go")
	for _, required := range [][]byte{
		[]byte("TestOpenAIOAuthService_RefreshMissingDecoderFailsClosed"),
		[]byte("TestOpenAIOAuthService_RefreshMissingRuntimeFailsClosed"),
		[]byte("newOfficialEgressTransitionRuntimeWithExecutor("),
	} {
		if !bytes.Contains(oauthTests, required) {
			t.Fatalf("OAuth refresh 退休正反测试缺失：%s", required)
		}
	}
}

func assertCompatibilityClosureFileDigest(t *testing.T, repoRoot, path, want string) {
	t.Helper()
	source := readCompatibilityClosureSource(t, repoRoot, path)
	if got := compatibilityClosureDigest(source); got != want &&
		!versionLeakDebtTransitionSupersedes(path, want, got) &&
		!upstreamV0177SourceTransitionSupersedes(path, want, got) {
		t.Fatalf("兼容代码闭集文件摘要漂移：path=%s got=%s want=%s", path, got, want)
	}
}

func readCompatibilityClosureSource(t *testing.T, repoRoot, path string) []byte {
	t.Helper()
	source, err := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(path)))
	if err != nil {
		t.Fatal(err)
	}
	return source
}

func assertCompatibilityDiscoveryMarkerClosure(
	t *testing.T,
	repoRoot string,
	closure compatibilityCodeRetirementClosure,
) {
	t.Helper()
	want := make(map[string]int, len(closure.AllowedMarkerOccurrences))
	for _, occurrence := range closure.AllowedMarkerOccurrences {
		key := occurrence.Path + "\x00" + occurrence.Marker
		want[key]++
	}

	got := make(map[string]int)
	for _, root := range []string{
		"backend/internal/service",
		"backend/internal/officialegress",
	} {
		absoluteRoot := filepath.Join(repoRoot, filepath.FromSlash(root))
		if err := filepath.WalkDir(absoluteRoot, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.IsDir() || filepath.Ext(path) != ".go" || strings.HasSuffix(path, "_test.go") {
				return nil
			}
			file, openErr := os.Open(path)
			if openErr != nil {
				return openErr
			}
			defer func() { _ = file.Close() }()
			relativePath, relativeErr := filepath.Rel(repoRoot, path)
			if relativeErr != nil {
				return relativeErr
			}
			relativePath = filepath.ToSlash(relativePath)
			scanner := bufio.NewScanner(file)
			for scanner.Scan() {
				line := scanner.Text()
				for _, marker := range closure.DiscoveryMarkers {
					if strings.Contains(line, marker) {
						got[relativePath+"\x00"+marker]++
					}
				}
			}
			return scanner.Err()
		}); err != nil {
			t.Fatal(err)
		}
	}

	if !compatibilityClosureStringCountsEqual(got, want) {
		t.Fatalf("出现未分类兼容标记或批准标记消失：got=%v want=%v", sortedCompatibilityClosureCounts(got), sortedCompatibilityClosureCounts(want))
	}
}

func compatibilityClosureStringCountsEqual(left, right map[string]int) bool {
	if len(left) != len(right) {
		return false
	}
	for key, value := range left {
		if right[key] != value {
			return false
		}
	}
	return true
}

func sortedCompatibilityClosureCounts(values map[string]int) []string {
	result := make([]string, 0, len(values))
	for key, count := range values {
		result = append(result, strings.ReplaceAll(key, "\x00", " :: ")+"="+strconv.Itoa(count))
	}
	sort.Strings(result)
	return result
}

func compatibilityClosureDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
