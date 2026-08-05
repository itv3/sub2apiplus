package officialegress

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	changeset6BenchmarkPostMetadataSHA256 = "0ec6d4c81cff93d8d6d2661016dc1a79c688e8bb2e4ccf1047a9b4c461ed6f5b"
	changeset6BenchmarkCalculationSHA256  = "076e4de68bf2f222a795fb2965e05abd10316677ad8b9a2442ee2a36e5c5240b"
	changeset6BenchmarkFixtureSHA256      = "7698cddeadace650567e46e7be9b66286212e26e983edb29e78da423ac713e08"
	changeset6BenchmarkPostDriverSHA256   = "bce3b5c5aace49adee353ce45d893a048b3c577d4244242fa34a911f1f847b4f"
)

func TestChangeset6BenchmarkPostEvidenceIsFrozen(t *testing.T) {
	metadataPath := "../../../docs/egress/validation/post/benchmark-metadata.json"
	raw, err := os.ReadFile(metadataPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset6BenchmarkPostMetadataSHA256 {
		t.Fatalf("变更集 6 benchmark post 元数据漂移：got=%s", got)
	}
	var metadata struct {
		SchemaVersion string `json:"schema_version"`
		Environment   struct {
			GoLauncherVersion   string `json:"go_launcher_version"`
			SelectedGoToolchain string `json:"selected_go_toolchain"`
			GOOS                string `json:"goos"`
			GOARCH              string `json:"goarch"`
			CPU                 string `json:"cpu"`
			GOMAXPROCS          int    `json:"gomaxprocs"`
		} `json:"environment"`
		Git struct {
			HEAD     string `json:"head"`
			HEADTree string `json:"head_tree"`
		} `json:"git"`
		Fixture struct {
			GeneratorSHA256 string `json:"generator_sha256"`
			SHA256          string `json:"fixture_sha256"`
		} `json:"fixture"`
		Results []struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"results"`
		Acceptance struct {
			Result string `json:"result"`
		} `json:"acceptance"`
	}
	if err := json.Unmarshal(raw, &metadata); err != nil {
		t.Fatal(err)
	}
	if metadata.SchemaVersion != "changeset6-benchmark-post/v1" ||
		metadata.Environment.GoLauncherVersion != "go1.26.4" ||
		metadata.Environment.SelectedGoToolchain != "go1.26.5" ||
		metadata.Environment.GOOS != "darwin" || metadata.Environment.GOARCH != "arm64" ||
		metadata.Environment.CPU != "Apple M4" || metadata.Environment.GOMAXPROCS != 10 ||
		metadata.Git.HEAD != "38a9929eac35a39c86de2f27de8f7a805d7dae52" ||
		metadata.Git.HEADTree != "a8c3dee18a01a6138bfcea60860bb5ad11548c3a" ||
		metadata.Fixture.GeneratorSHA256 != changeset6BenchmarkPostDriverSHA256 ||
		metadata.Fixture.SHA256 != changeset6BenchmarkFixtureSHA256 ||
		metadata.Acceptance.Result != "passed" || len(metadata.Results) != 6 {
		t.Fatalf("变更集 6 benchmark post 元数据非法：%+v", metadata)
	}
	for _, result := range metadata.Results {
		path := strings.Replace(result.Path, "docs/changeset6/", "docs/egress/validation/", 1)
		resultRaw, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := changeset3ReferenceSHA256(resultRaw); got != result.SHA256 {
			t.Fatalf("变更集 6 benchmark 结果漂移：path=%s got=%s want=%s", result.Path, got, result.SHA256)
		}
	}
	for path, expected := range map[string]string{
		"../../../docs/egress/validation/post/benchmark-drivers/body-post_test.go":    changeset6BenchmarkPostDriverSHA256,
		"../../../docs/egress/validation/post/benchmark-drivers/catalog-post_test.go": "f7731b9f5a2999e94ab869f245ba74e20654c22b018af74ab7a6f430f1822aed",
		"../../../docs/egress/validation/post/benchmark-drivers/profile-post_test.go": "66e0775b72be6456d71c5527b21664c1b06b9c743fdfb83515a808812331a846",
	} {
		driverRaw, readErr := os.ReadFile(path)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := changeset3ReferenceSHA256(driverRaw); got != expected {
			t.Fatalf("变更集 6 post benchmark driver 摘要漂移：path=%s got=%s", path, got)
		}
	}
	calculationRaw, err := os.ReadFile("../../../docs/egress/validation/post/benchmark-calculation.json")
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(calculationRaw); got != changeset6BenchmarkCalculationSHA256 {
		t.Fatalf("变更集 6 benchmark 原始结果计算证据漂移：got=%s", got)
	}
	var calculation struct {
		SchemaVersion     string `json:"schema_version"`
		DriverEquivalence struct {
			BodyAllowedDeltaCount int  `json:"body_allowed_delta_count"`
			CatalogByteEqual      bool `json:"catalog_byte_equal"`
			ProfileByteEqual      bool `json:"profile_byte_equal"`
			CommandsByteEqual     bool `json:"commands_byte_equal"`
		} `json:"driver_equivalence"`
		BodyCases []struct {
			Result string `json:"result"`
		} `json:"body_cases"`
		CatalogAndProfileCases []struct {
			Result string `json:"result"`
		} `json:"catalog_and_profile_cases"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal(calculationRaw, &calculation); err != nil {
		t.Fatal(err)
	}
	if calculation.SchemaVersion != "changeset6-benchmark-calculation/v1" ||
		calculation.DriverEquivalence.BodyAllowedDeltaCount != 1 ||
		!calculation.DriverEquivalence.CatalogByteEqual ||
		!calculation.DriverEquivalence.ProfileByteEqual ||
		!calculation.DriverEquivalence.CommandsByteEqual ||
		len(calculation.BodyCases) != 4 || len(calculation.CatalogAndProfileCases) != 3 ||
		calculation.Result != "passed" {
		t.Fatalf("变更集 6 benchmark 计算证据非法：%+v", calculation)
	}
	for _, item := range append(calculation.BodyCases, calculation.CatalogAndProfileCases...) {
		if item.Result != "passed" {
			t.Fatalf("变更集 6 benchmark case 未通过原始结果复算：%+v", item)
		}
	}
}
