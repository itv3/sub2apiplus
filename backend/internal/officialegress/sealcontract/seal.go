package sealcontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"
)

const (
	SchemaVersion   = 1
	BootstrapCommit = "38a9929eac35a39c86de2f27de8f7a805d7dae52"
	BootstrapTree   = "a8c3dee18a01a6138bfcea60860bb5ad11548c3a"
)

type Lifecycle string

const (
	LifecycleProvisional Lifecycle = "provisional"
	LifecycleSealed      Lifecycle = "sealed"
)

// Receipt 冻结正式封存时的 ceiling 与补录清单原文。Receipt 自身的不可变性
// 由 CI 与受保护目标分支中的上一份 sealed Receipt 逐字节比较来保证。
type Receipt struct {
	SchemaVersion                 int       `json:"schema_version"`
	BootstrapCommit               string    `json:"bootstrap_commit"`
	BootstrapTree                 string    `json:"bootstrap_tree"`
	Lifecycle                     Lifecycle `json:"lifecycle"`
	SealedAt                      *string   `json:"sealed_at"`
	ProtectedBaseCommit           *string   `json:"protected_base_commit"`
	LegacyCeilingSHA256           *string   `json:"legacy_ceiling_sha256"`
	PreBootstrapSupplementsSHA256 *string   `json:"pre_bootstrap_supplements_sha256"`
	ReviewedBy                    *string   `json:"reviewed_by"`
	ReviewRef                     *string   `json:"review_ref"`
	Rationale                     *string   `json:"rationale"`
}

type legacyCeilingDocument struct {
	SchemaVersion   int       `json:"schema_version"`
	BootstrapCommit string    `json:"bootstrap_commit"`
	Lifecycle       Lifecycle `json:"lifecycle"`
	SealedAt        *string   `json:"sealed_at"`
	SinkIDs         []string  `json:"sink_ids"`
}

type legacyBaselineDocument struct {
	SchemaVersion        int       `json:"schema_version"`
	BootstrapCommit      string    `json:"bootstrap_commit"`
	Lifecycle            Lifecycle `json:"lifecycle"`
	ObservationStartedAt string    `json:"observation_started_at"`
	SealedAt             *string   `json:"sealed_at"`
	SinkIDs              []string  `json:"sink_ids"`
}

type supplementDocument struct {
	SchemaVersion   int               `json:"schema_version"`
	BootstrapCommit string            `json:"bootstrap_commit"`
	BootstrapTree   string            `json:"bootstrap_tree"`
	Lifecycle       Lifecycle         `json:"lifecycle"`
	Supplements     []json.RawMessage `json:"supplements"`
}

// Assets 是当前工作区或受保护 Git 基准中的封存资产原文。Receipt、ceiling 与
// supplements 在 sealed 后逐字节不变；baseline 只能相对受保护基准单调减少。
type Assets struct {
	ReceiptRaw     []byte
	CeilingRaw     []byte
	SupplementsRaw []byte
	BaselineRaw    []byte
}

func ParseReceipt(raw []byte) (Receipt, error) {
	receipt, err := decodeStrict[Receipt](raw, "legacy seal receipt")
	if err != nil {
		return Receipt{}, err
	}
	if receipt.SchemaVersion != SchemaVersion || receipt.BootstrapCommit != BootstrapCommit ||
		receipt.BootstrapTree != BootstrapTree {
		return Receipt{}, errors.New("legacy seal receipt schema/bootstrap 非法")
	}
	switch receipt.Lifecycle {
	case LifecycleProvisional:
		if receipt.SealedAt != nil || receipt.ProtectedBaseCommit != nil || receipt.LegacyCeilingSHA256 != nil ||
			receipt.PreBootstrapSupplementsSHA256 != nil || receipt.ReviewedBy != nil ||
			receipt.ReviewRef != nil || receipt.Rationale != nil {
			return Receipt{}, errors.New("provisional seal receipt 不得提前填写封存字段")
		}
	case LifecycleSealed:
		if !validTimestamp(receipt.SealedAt) || !validCommit(receipt.ProtectedBaseCommit) ||
			!validDigest(receipt.LegacyCeilingSHA256) ||
			!validDigest(receipt.PreBootstrapSupplementsSHA256) || !nonEmpty(receipt.ReviewedBy) ||
			!nonEmpty(receipt.ReviewRef) || !nonEmpty(receipt.Rationale) {
			return Receipt{}, errors.New("sealed seal receipt 缺少完整摘要或审核字段")
		}
	default:
		return Receipt{}, fmt.Errorf("legacy seal receipt lifecycle 非法: %s", receipt.Lifecycle)
	}
	return receipt, nil
}

// VerifyCurrent 校验当前四份资产的生命周期、结构和原文摘要。它防止只修改
// ceiling 或 supplements；baseline 的跨提交单调性由 VerifyProtectedBase 保证。
func VerifyCurrent(assets Assets) (Receipt, error) {
	receipt, err := ParseReceipt(assets.ReceiptRaw)
	if err != nil {
		return Receipt{}, err
	}
	ceiling, err := decodeStrict[legacyCeilingDocument](assets.CeilingRaw, "legacy ceiling")
	if err != nil {
		return Receipt{}, err
	}
	supplements, err := decodeStrict[supplementDocument](assets.SupplementsRaw, "pre-bootstrap supplements")
	if err != nil {
		return Receipt{}, err
	}
	baseline, err := decodeLegacyBaseline(assets.BaselineRaw)
	if err != nil {
		return Receipt{}, err
	}
	if ceiling.SchemaVersion != 1 || ceiling.BootstrapCommit != BootstrapCommit {
		return Receipt{}, errors.New("legacy ceiling schema/bootstrap 非法")
	}
	if supplements.SchemaVersion != 2 || supplements.BootstrapCommit != BootstrapCommit ||
		supplements.BootstrapTree != BootstrapTree {
		return Receipt{}, errors.New("pre-bootstrap supplements schema/bootstrap 非法")
	}
	if ceiling.Lifecycle != receipt.Lifecycle || supplements.Lifecycle != receipt.Lifecycle ||
		baseline.Lifecycle != receipt.Lifecycle {
		return Receipt{}, errors.New("seal receipt、ceiling、supplements 与 baseline 生命周期不一致")
	}
	if err := validateSortedUniqueSinkIDs("legacy ceiling", ceiling.SinkIDs); err != nil {
		return Receipt{}, err
	}
	if receipt.Lifecycle == LifecycleProvisional {
		if ceiling.SealedAt != nil || baseline.SealedAt != nil {
			return Receipt{}, errors.New("provisional legacy ceiling/baseline 不得设置 sealed_at")
		}
		if !equalStrings(baseline.SinkIDs, ceiling.SinkIDs) {
			return Receipt{}, errors.New("provisional legacy baseline 与 ceiling 必须一致")
		}
		return receipt, nil
	}
	if ceiling.SealedAt == nil || strings.TrimSpace(*ceiling.SealedAt) != strings.TrimSpace(*receipt.SealedAt) {
		return Receipt{}, errors.New("sealed receipt 与 legacy ceiling 的 sealed_at 不一致")
	}
	if baseline.SealedAt == nil || strings.TrimSpace(*baseline.SealedAt) != strings.TrimSpace(*receipt.SealedAt) {
		return Receipt{}, errors.New("sealed receipt 与 legacy baseline 的 sealed_at 不一致")
	}
	if missing := firstMissingSinkID(baseline.SinkIDs, ceiling.SinkIDs); missing != "" {
		return Receipt{}, fmt.Errorf("sealed legacy baseline 不属于 ceiling: %s", missing)
	}
	if digest(assets.CeilingRaw) != *receipt.LegacyCeilingSHA256 {
		return Receipt{}, errors.New("legacy ceiling 原文摘要与 seal receipt 不一致")
	}
	if digest(assets.SupplementsRaw) != *receipt.PreBootstrapSupplementsSHA256 {
		return Receipt{}, errors.New("pre-bootstrap supplements 原文摘要与 seal receipt 不一致")
	}
	return receipt, nil
}

// VerifyProtectedBase 使用受保护目标分支作为工作区之外的信任锚。第一次从
// provisional 转为 sealed 时，Receipt 必须绑定实际 base commit；一旦基准已
// sealed，Receipt、ceiling 与 supplements 逐字节不变，baseline 只能单调减少。
func VerifyProtectedBase(current Assets, protectedBase Assets, protectedBaseCommit string) error {
	if !ValidGitObjectID(protectedBaseCommit) {
		return errors.New("受保护 base commit object ID 非法")
	}
	currentReceipt, err := VerifyCurrent(current)
	if err != nil {
		return fmt.Errorf("当前封存资产: %w", err)
	}
	baseReceipt, err := VerifyCurrent(protectedBase)
	if err != nil {
		return fmt.Errorf("受保护基准封存资产: %w", err)
	}
	if baseReceipt.Lifecycle == LifecycleProvisional {
		if currentReceipt.Lifecycle == LifecycleSealed &&
			(currentReceipt.ProtectedBaseCommit == nil ||
				*currentReceipt.ProtectedBaseCommit != protectedBaseCommit) {
			return errors.New("首次 sealed receipt 未绑定实际受保护 base commit")
		}
		return nil
	}
	if currentReceipt.Lifecycle != LifecycleSealed {
		return errors.New("受保护基准已 sealed，禁止降级为 provisional")
	}
	if !bytes.Equal(current.ReceiptRaw, protectedBase.ReceiptRaw) {
		return errors.New("sealed legacy seal receipt 已偏离受保护基准")
	}
	if !bytes.Equal(current.CeilingRaw, protectedBase.CeilingRaw) {
		return errors.New("sealed legacy ceiling 已偏离受保护基准")
	}
	if !bytes.Equal(current.SupplementsRaw, protectedBase.SupplementsRaw) {
		return errors.New("sealed pre-bootstrap supplements 已偏离受保护基准")
	}
	currentBaseline, err := decodeLegacyBaseline(current.BaselineRaw)
	if err != nil {
		return fmt.Errorf("当前 legacy baseline: %w", err)
	}
	baseBaseline, err := decodeLegacyBaseline(protectedBase.BaselineRaw)
	if err != nil {
		return fmt.Errorf("受保护基准 legacy baseline: %w", err)
	}
	if readded := firstMissingSinkID(currentBaseline.SinkIDs, baseBaseline.SinkIDs); readded != "" {
		return fmt.Errorf("sealed legacy baseline 禁止重新加入已移除 SinkID: %s", readded)
	}
	return nil
}

// CeilingLifecycle 只用于兼容首次引入 seal receipt：若受保护基准还没有
// Receipt，调用方仍可识别一个已经 sealed 的旧 ceiling 并拒绝降级。
func CeilingLifecycle(raw []byte) (Lifecycle, error) {
	document, err := decodeStrict[legacyCeilingDocument](raw, "legacy ceiling")
	if err != nil {
		return "", err
	}
	if document.SchemaVersion != 1 || document.BootstrapCommit != BootstrapCommit {
		return "", errors.New("legacy ceiling schema/bootstrap 非法")
	}
	if document.Lifecycle != LifecycleProvisional && document.Lifecycle != LifecycleSealed {
		return "", errors.New("legacy ceiling lifecycle 非法")
	}
	return document.Lifecycle, nil
}

func decodeLegacyBaseline(raw []byte) (legacyBaselineDocument, error) {
	document, err := decodeStrict[legacyBaselineDocument](raw, "legacy baseline")
	if err != nil {
		return legacyBaselineDocument{}, err
	}
	if document.SchemaVersion != 1 || document.BootstrapCommit != BootstrapCommit ||
		strings.TrimSpace(document.ObservationStartedAt) == "" {
		return legacyBaselineDocument{}, errors.New("legacy baseline schema/bootstrap 非法")
	}
	if document.Lifecycle != LifecycleProvisional && document.Lifecycle != LifecycleSealed {
		return legacyBaselineDocument{}, errors.New("legacy baseline lifecycle 非法")
	}
	if err := validateSortedUniqueSinkIDs("legacy baseline", document.SinkIDs); err != nil {
		return legacyBaselineDocument{}, err
	}
	return document, nil
}

func validateSortedUniqueSinkIDs(name string, sinkIDs []string) error {
	if !sort.StringsAreSorted(sinkIDs) {
		return fmt.Errorf("%s sink_ids 必须排序", name)
	}
	for index, sinkID := range sinkIDs {
		if strings.TrimSpace(sinkID) == "" || strings.TrimSpace(sinkID) != sinkID {
			return fmt.Errorf("%s 含空白 SinkID", name)
		}
		if index > 0 && sinkIDs[index-1] == sinkID {
			return fmt.Errorf("%s 含重复 SinkID: %s", name, sinkID)
		}
	}
	return nil
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func firstMissingSinkID(candidateSubset, candidateSuperset []string) string {
	superset := make(map[string]struct{}, len(candidateSuperset))
	for _, sinkID := range candidateSuperset {
		superset[sinkID] = struct{}{}
	}
	for _, sinkID := range candidateSubset {
		if _, exists := superset[sinkID]; !exists {
			return sinkID
		}
	}
	return ""
}

func decodeStrict[T any](raw []byte, name string) (T, error) {
	var value T
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		return value, fmt.Errorf("解析%s: %w", name, err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return value, fmt.Errorf("%s尾部存在额外 JSON", name)
	}
	return value, nil
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func validDigest(value *string) bool {
	if value == nil || len(*value) != sha256.Size*2 || strings.ToLower(*value) != *value {
		return false
	}
	_, err := hex.DecodeString(*value)
	return err == nil
}

func validTimestamp(value *string) bool {
	if !nonEmpty(value) {
		return false
	}
	_, err := time.Parse(time.RFC3339, *value)
	return err == nil
}

func validCommit(value *string) bool {
	return value != nil && ValidGitObjectID(*value)
}

// ValidGitObjectID 接受 Git SHA-1（40 位）和 SHA-256（64 位）仓库的完整、
// 小写十六进制 object ID。调用方不能再分别维护互相矛盾的长度规则。
func ValidGitObjectID(value string) bool {
	if (len(value) != 40 && len(value) != 64) || strings.ToLower(value) != value {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func nonEmpty(value *string) bool {
	return value != nil && strings.TrimSpace(*value) != ""
}
