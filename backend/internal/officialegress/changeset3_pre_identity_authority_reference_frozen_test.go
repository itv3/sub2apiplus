package officialegress

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

const changeset3PreIdentityReferenceSHA256 = "95ae1518698f0b26beead7a7374f9cd372b3c44c176ff774df4ac7b6aabc017a"

func TestChangeset3PreIdentityAuthorityReferenceIsFrozen(t *testing.T) {
	manifestPath := "../../../docs/changeset3/pre_identity_authority_refactor_reference/manifest.json"
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != changeset3PreIdentityReferenceSHA256 {
		t.Fatalf("迁移前参考摘要漂移：got=%s want=%s", got, changeset3PreIdentityReferenceSHA256)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var reference changeset3PreIdentityReference
	if err := decoder.Decode(&reference); err != nil {
		t.Fatal(err)
	}
	if reference.SchemaVersion != "changeset3-pre-identity-authority-reference/v1" ||
		reference.ReferenceKind != "pre_identity_authority_refactor_reference" ||
		reference.ExternalTraffic || reference.CredentialMaterial != "synthetic_only" ||
		reference.RouteCount != 28 || reference.CaptureCount != 56 ||
		len(reference.Captures) != 56 || len(reference.SourceMaterial) != 9 ||
		len(reference.KnownPreRefFindings) != 1 {
		t.Fatalf("迁移前参考顶层事实不完整：%+v", reference)
	}
	routesByMode := map[ReleaseMode]map[string]bool{
		ReleaseModeActive: {}, ReleaseModePrevious: {},
	}
	anchorRoutes := make(map[string]bool)
	newRoutes := make(map[string]bool)
	guardRejections := 0
	for _, capture := range reference.Captures {
		if !capture.ReleaseMode.Valid() || !capture.Protocol.Valid() ||
			!capture.HasFinalizationToken || capture.AuthorityID != "codex.executor.changeset1b" ||
			capture.AttemptOrdinal != 1 || capture.AttemptReason != string(AttemptReasonInitial) ||
			capture.ProfileValidationResult != "passed" || capture.ReleaseDigest == "" ||
			capture.ProfileDigest == "" || capture.BundleDigest == "" ||
			capture.ConnectionIdentityDigest == "" || capture.ConnectionPoolDigest == "" ||
			capture.TLSProfileDigest == "" {
			t.Fatalf("迁移前参考 capture 不完整：%+v", capture)
		}
		routeKey := strings.Join([]string{
			capture.SinkID, capture.Method, capture.HostTemplate,
			capture.PathTemplate, string(capture.Protocol), string(capture.Purpose),
		}, "\x00")
		if routesByMode[capture.ReleaseMode][routeKey] {
			t.Fatalf("迁移前参考 route-mode 重复：%s %s", capture.ReleaseMode, routeKey)
		}
		routesByMode[capture.ReleaseMode][routeKey] = true
		if capture.Anchor {
			anchorRoutes[routeKey] = true
		} else {
			newRoutes[routeKey] = true
		}
		if !capture.TerminalGuardAllow {
			guardRejections++
			if capture.TerminalGuardReason != ReasonRequestModifiedAfterFinalize ||
				capture.EndpointID != "responses_ws" {
				t.Fatalf("迁移前参考含未登记 Guard 拒绝：%+v", capture)
			}
		}
		if capture.SingleUseRawStream && capture.SingleUseConsumptionCount != 1 {
			t.Fatalf("single-use 参考未证明单次消费：%+v", capture)
		}
	}
	if len(routesByMode[ReleaseModeActive]) != 28 ||
		len(routesByMode[ReleaseModePrevious]) != 28 ||
		len(anchorRoutes) != 4 || len(newRoutes) != 24 || guardRejections != 4 {
		t.Fatalf(
			"迁移前参考矩阵错误：active=%d previous=%d anchors=%d new=%d guard_rejections=%d",
			len(routesByMode[ReleaseModeActive]), len(routesByMode[ReleaseModePrevious]),
			len(anchorRoutes), len(newRoutes), guardRejections,
		)
	}
	secretRaw, err := os.ReadFile(
		"../../../docs/changeset3/pre_identity_authority_refactor_reference/secret-scan.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	var scan struct {
		SchemaVersion  string         `json:"schema_version"`
		Artifact       string         `json:"artifact"`
		ArtifactSHA256 string         `json:"artifact_sha256"`
		Matches        map[string]int `json:"matches"`
		Result         string         `json:"result"`
	}
	secretDecoder := json.NewDecoder(bytes.NewReader(secretRaw))
	secretDecoder.DisallowUnknownFields()
	if err := secretDecoder.Decode(&scan); err != nil {
		t.Fatal(err)
	}
	if scan.SchemaVersion != "changeset3-secret-scan/v1" || scan.Artifact != "manifest.json" ||
		scan.ArtifactSHA256 != changeset3PreIdentityReferenceSHA256 || scan.Result != "passed" ||
		scan.Matches["Bearer "] != 0 || scan.Matches["synthetic-access-token"] != 0 ||
		scan.Matches["synthetic-refresh-token"] != 0 {
		t.Fatalf("迁移前参考 secret scan 无效：%+v", scan)
	}
}
