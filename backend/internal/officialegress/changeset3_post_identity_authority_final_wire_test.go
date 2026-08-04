package officialegress

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/finalwirecontract"
)

type changeset3PostFinalWireManifest struct {
	SchemaVersion      string                                  `json:"schema_version"`
	CaptureKind        string                                  `json:"capture_kind"`
	CaptureBoundary    string                                  `json:"capture_boundary"`
	ExternalTraffic    bool                                    `json:"external_traffic"`
	CredentialMaterial string                                  `json:"credential_material"`
	RouteCount         int                                     `json:"route_count"`
	ReleaseModes       []ReleaseMode                           `json:"release_modes"`
	CaptureCount       int                                     `json:"capture_count"`
	SourceMaterial     []changeset3ReferenceSource             `json:"source_material"`
	Comparison         changeset3PostReferenceComparison       `json:"pre_refactor_comparison"`
	AnchorComparison   changeset3PostAnchorComparison          `json:"existing_anchor_comparison"`
	Captures           []changeset3PreIdentityReferenceCapture `json:"captures"`
	Redaction          string                                  `json:"redaction"`
}

type changeset3PostReferenceComparison struct {
	ReferencePath         string `json:"reference_path"`
	ReferenceSHA256       string `json:"reference_sha256"`
	ReferenceScope        string `json:"reference_scope"`
	ComparedCaptureCount  int    `json:"compared_capture_count"`
	UnexpectedDiffCount   int    `json:"unexpected_diff_count"`
	ApprovedDeltaPath     string `json:"approved_delta_path"`
	ApprovedDeltaSHA256   string `json:"approved_delta_sha256"`
	ApprovedDeltaCount    int    `json:"approved_delta_count"`
	AppliedApprovedDeltas int    `json:"applied_approved_deltas"`
	Result                string `json:"result"`
}

type changeset3PostAnchorComparison struct {
	FixtureCount         int                              `json:"fixture_count"`
	ComparedCaptureCount int                              `json:"compared_capture_count"`
	Result               string                           `json:"result"`
	Fixtures             []changeset3PostAnchorFixtureRef `json:"fixtures"`
}

type changeset3PostAnchorFixtureRef struct {
	SinkID string `json:"sink_id"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

func changeset3PostCaptureKey(capture changeset3PreIdentityReferenceCapture) string {
	return strings.Join([]string{
		string(capture.ReleaseMode),
		capture.SinkID,
		capture.Method,
		capture.HostTemplate,
		capture.PathTemplate,
		string(capture.Protocol),
	}, "\x00")
}

func TestChangeset3FinalWireComparatorRejectsRequiredMutations(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/changeset3/post_identity_authority_refactor_final_wire/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var manifest changeset3PostFinalWireManifest
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	find := func(sinkID SinkID, protocol WireProtocol) changeset3PreIdentityReferenceCapture {
		for _, capture := range manifest.Captures {
			if capture.SinkID == string(sinkID) && capture.ReleaseMode == ReleaseModeActive &&
				capture.Protocol == protocol {
				return capture
			}
		}
		t.Fatalf("缺少 mutation 基准 capture：%s/%s", sinkID, protocol)
		return changeset3PreIdentityReferenceCapture{}
	}
	httpCapture := find(SinkCodexResponsesForward, WireProtocolHTTP)
	wsCapture := find(SinkCodexResponsesWS, WireProtocolWebSocket)
	uploadCapture := find(SinkCodexFilesBlobUpload, WireProtocolHTTP)

	mutateHeaderValue := func(name string, value string) func(*changeset3PreIdentityReferenceCapture) {
		return func(capture *changeset3PreIdentityReferenceCapture) {
			for index := range capture.OrderedHeaders {
				if strings.EqualFold(capture.OrderedHeaders[index].Name, name) {
					capture.OrderedHeaders[index].SafeValue = value
					capture.OrderedHeaders[index].ValueSHA256 = strings.Repeat("a", 64)
					return
				}
			}
			t.Fatalf("mutation 基准缺少 Header：%s", name)
		}
	}
	tests := []struct {
		name   string
		base   changeset3PreIdentityReferenceCapture
		mutate func(*changeset3PreIdentityReferenceCapture)
	}{
		{"user_agent", httpCapture, mutateHeaderValue("user-agent", "mutated-user-agent")},
		{"originator", httpCapture, mutateHeaderValue("originator", "mutated-originator")},
		{"version", httpCapture, mutateHeaderValue("version", "999.0.0")},
		{"header_order", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.OrderedHeaders[0], capture.OrderedHeaders[1] = capture.OrderedHeaders[1], capture.OrderedHeaders[0]
		}},
		{"body_order", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.Body.OrderedFields[0], capture.Body.OrderedFields[1] = capture.Body.OrderedFields[1], capture.Body.OrderedFields[0]
		}},
		{"body_digest", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.Body.SyntheticSHA256 = strings.Repeat("9", 64)
		}},
		{"compression", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.Body.AppliedCompression = "mutated-compression"
		}},
		{"tls", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.TLSProfileDigest = strings.Repeat("0", 64)
		}},
		{"profile", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.ProfileDigest = strings.Repeat("1", 64)
		}},
		{"pool", httpCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.ConnectionPoolDigest = strings.Repeat("2", 64)
		}},
		{"ws_compression", wsCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.WebSocket.CompressionOffer = "mutated-compression-offer"
		}},
		{"ws_event_shape", wsCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.WebSocket.EventMatrix[0].TypeShapeSHA256 = strings.Repeat("3", 64)
		}},
		{"ws_frame_digest", wsCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.WebSocket.EventMatrix[0].BodySHA256 = strings.Repeat("4", 64)
		}},
		{"upload_host", uploadCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.FinalHost = "attacker.example"
		}},
		{"upload_path", uploadCapture, func(capture *changeset3PreIdentityReferenceCapture) {
			capture.FinalPath = "/mutated/upload"
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			mutated := changeset3CloneFinalWireCapture(t, test.base)
			test.mutate(&mutated)
			result, compareErr := finalwirecontract.Compare(
				changeset3PostCaptureKey(test.base), test.base, mutated, nil,
			)
			if compareErr != nil {
				t.Fatal(compareErr)
			}
			if result.OK() || len(result.Unexpected) == 0 {
				t.Fatalf("正式比较器未捕获 %s mutation：%+v", test.name, result)
			}
		})
	}
}

func changeset3CloneFinalWireCapture(
	t *testing.T,
	capture changeset3PreIdentityReferenceCapture,
) changeset3PreIdentityReferenceCapture {
	t.Helper()
	raw, err := json.Marshal(capture)
	if err != nil {
		t.Fatal(err)
	}
	var cloned changeset3PreIdentityReferenceCapture
	if err := json.Unmarshal(raw, &cloned); err != nil {
		t.Fatal(err)
	}
	return cloned
}
