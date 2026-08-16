package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

const (
	changeset4TransportTransitionPath   = "catalogdata/changeset4-transport-receipt-transitions.json"
	changeset4TransportTransitionSHA256 = "d9481311e72d81d17732731c49bdf8063aec8f570f3856020724ff4d4b642f35"
	changeset4BaseReceiptDigest         = "95c11c24707242adca40456495c6be2113a56d37d5964696a00936c2973020bf"
)

// transportReceiptTransition 只允许已冻结迁移收据中的旧 transport claim 精确连接到
// 后续已验收 executable 的新 transport。它不改变 authority、route、endpoint、backend、
// adapter 或 EnforcementState，也不能成为任意 release 漂移的通配豁免。
type transportReceiptTransition struct {
	SinkID              string                        `json:"sink_id"`
	Route               receiptcontract.RouteIdentity `json:"route"`
	EvidenceID          string                        `json:"evidence_id"`
	PreviousTransportID string                        `json:"previous_transport_id"`
	CurrentTransportID  string                        `json:"current_transport_id"`
	CurrentLifecycle    profilecontract.LifecycleKind `json:"current_lifecycle"`
	Reason              string                        `json:"reason"`
}

type transportReceiptTransitionManifest struct {
	SchemaVersion     string                       `json:"schema_version"`
	Changeset         string                       `json:"changeset"`
	BaseReceiptSHA256 string                       `json:"base_receipt_sha256"`
	Entries           []transportReceiptTransition `json:"entries"`
}

func applyTransportReceiptTransitions(inputs []SinkBindingInput) ([]SinkBindingInput, error) {
	raw, err := migrationReceiptFS.ReadFile(changeset4TransportTransitionPath)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != changeset4TransportTransitionSHA256 {
		return nil, errors.New("变更集 4 transport 过渡收据摘要漂移")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var manifest transportReceiptTransitionManifest
	if err := decoder.Decode(&manifest); err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("变更集 4 transport 过渡收据含多余数据")
	}
	if manifest.SchemaVersion != "changeset4-transport-receipt-transitions/v1" ||
		manifest.Changeset != "4" || manifest.BaseReceiptSHA256 != changeset4BaseReceiptDigest ||
		len(manifest.Entries) != 3 {
		return nil, errors.New("变更集 4 transport 过渡收据顶层事实非法")
	}

	indexBySink := make(map[SinkID]int, len(inputs))
	for index := range inputs {
		indexBySink[inputs[index].ID] = index
	}
	seen := make(map[string]bool, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		if err := applyTransportReceiptTransitionEntry(
			inputs,
			indexBySink,
			entry,
			seen,
			DefaultReleaseCatalog(),
		); err != nil {
			return nil, err
		}
	}
	return inputs, nil
}

func applyTransportReceiptTransitionEntry(
	inputs []SinkBindingInput,
	indexBySink map[SinkID]int,
	entry transportReceiptTransition,
	seen map[string]bool,
	releaseCatalog ReleaseCatalog,
) error {
	if strings.TrimSpace(entry.Reason) == "" ||
		entry.CurrentLifecycle != profilecontract.LifecycleBackendClientLongLived ||
		entry.PreviousTransportID == "" || entry.CurrentTransportID == "" ||
		entry.PreviousTransportID == entry.CurrentTransportID {
		return errors.New("transport 过渡条目字段非法")
	}
	sinkID := SinkID(entry.SinkID)
	index, ok := indexBySink[sinkID]
	if !ok || inputs[index].migrationReceipt == nil ||
		inputs[index].EnforcementState != SinkStateEnforced {
		return fmt.Errorf("transport 过渡条目没有已 enforced 的历史收据：%s", entry.SinkID)
	}
	route := CatalogRoute{Key: RouteKey{
		Method: entry.Route.Method, Host: entry.Route.Host, Path: entry.Route.Path,
		Purpose: Purpose(entry.Route.Purpose),
	}, Protocol: WireProtocol(entry.Route.Protocol)}
	identity := entry.SinkID + "\x00" + catalogRouteIdentity(route)
	if seen[identity] {
		return fmt.Errorf("transport 过渡条目重复：%s", identity)
	}
	seen[identity] = true

	receipt := inputs[index].migrationReceipt
	claimIndex := -1
	for candidateIndex := range receipt.routeClaims {
		candidate := receipt.routeClaims[candidateIndex]
		if catalogRouteIdentity(candidate.route) == catalogRouteIdentity(route) {
			claimIndex = candidateIndex
			if candidate.evidenceID != entry.EvidenceID ||
				candidate.transportID != entry.PreviousTransportID {
				return fmt.Errorf("transport 过渡条目与历史收据不一致：%s", identity)
			}
			break
		}
	}
	if claimIndex < 0 {
		return fmt.Errorf("transport 过渡条目未命中历史 route claim：%s", identity)
	}

	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := releaseCatalog.Resolve(mode)
		if err != nil {
			return err
		}
		matched := false
		for _, endpoint := range release.ExecutableProfile().Endpoints() {
			if endpoint.ID != entry.EvidenceID {
				continue
			}
			matched = true
			if endpoint.ResourceLifecycle.Lifecycle != entry.CurrentLifecycle {
				return fmt.Errorf("transport 过渡条目与 %s executable 不一致：%s", mode, identity)
			}
		}
		if !matched {
			return fmt.Errorf("transport 过渡条目在 %s executable 缺少 endpoint：%s", mode, entry.EvidenceID)
		}
	}
	// CurrentTransportID 属于摘要冻结的变更集 4 历史锚点；当前 Active/Previous 的
	// transport 由各自完整画像决定，并按 ReleaseDigest 精确绑定，避免未来换版仍被
	// Previous 的 transport ID 锁死，也禁止两套 Bundle 跨版本混用 transport。
	receipt.routeClaims[claimIndex].transportID = entry.CurrentTransportID
	return nil
}
