// Package finalwirecontract 提供变更集 3 final-wire 证据唯一的结构化比较器。
// 包保持中立，不依赖 officialegress 或 service，两个包的测试与收据工具可共用。
package finalwirecontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

const missingFingerprint = "missing"

// ApprovedDelta 精确登记一个 capture 的一个字段路径允许发生的变化。
// before/after 摘要均绑定规范 JSON 子树；不支持通配 capture 或通配路径。
type ApprovedDelta struct {
	CaptureKey   string `json:"capture_key"`
	Path         string `json:"path"`
	BeforeSHA256 string `json:"before_sha256"`
	AfterSHA256  string `json:"after_sha256"`
	Reason       string `json:"reason"`
}

// Difference 是比较器发现的单个精确差异。
type Difference struct {
	Path         string `json:"path"`
	BeforeSHA256 string `json:"before_sha256"`
	AfterSHA256  string `json:"after_sha256"`
}

// Result 同时返回未批准差异、实际应用的批准项和未被使用的批准项。
type Result struct {
	Unexpected []Difference    `json:"unexpected"`
	Applied    []ApprovedDelta `json:"applied"`
	Unused     []ApprovedDelta `json:"unused"`
}

func (r Result) OK() bool { return len(r.Unexpected) == 0 && len(r.Unused) == 0 }

// Compare 是 final-wire pre/post、mutation、冻结 manifest 和收据重建唯一允许
// 使用的比较入口。它比较序列化后的全部字段；数组顺序属于证据的一部分。
func Compare(captureKey string, before any, after any, approved []ApprovedDelta) (Result, error) {
	if strings.TrimSpace(captureKey) == "" {
		return Result{}, fmt.Errorf("final-wire capture key 为空")
	}
	left, err := normalizeJSON(before)
	if err != nil {
		return Result{}, fmt.Errorf("规范化 before final-wire：%w", err)
	}
	right, err := normalizeJSON(after)
	if err != nil {
		return Result{}, fmt.Errorf("规范化 after final-wire：%w", err)
	}
	var differences []Difference
	compareValue("", left, true, right, true, &differences)

	matching := make([]ApprovedDelta, 0)
	seenApproval := make(map[string]bool)
	for _, delta := range approved {
		if delta.CaptureKey != captureKey {
			continue
		}
		if err := validateApprovedDelta(delta); err != nil {
			return Result{}, err
		}
		identity := strings.Join([]string{
			delta.CaptureKey, delta.Path, delta.BeforeSHA256, delta.AfterSHA256,
		}, "\x00")
		if seenApproval[identity] {
			return Result{}, fmt.Errorf("final-wire approved delta 重复：%s %s", captureKey, delta.Path)
		}
		seenApproval[identity] = true
		matching = append(matching, delta)
	}
	used := make([]bool, len(matching))
	result := Result{}
	for _, difference := range differences {
		matched := -1
		for index, delta := range matching {
			if used[index] {
				continue
			}
			if delta.Path == difference.Path &&
				delta.BeforeSHA256 == difference.BeforeSHA256 &&
				delta.AfterSHA256 == difference.AfterSHA256 {
				matched = index
				break
			}
		}
		if matched < 0 {
			result.Unexpected = append(result.Unexpected, difference)
			continue
		}
		used[matched] = true
		result.Applied = append(result.Applied, matching[matched])
	}
	for index, delta := range matching {
		if !used[index] {
			result.Unused = append(result.Unused, delta)
		}
	}
	return result, nil
}

// Fingerprint 返回 approved delta 使用的规范 JSON 摘要。missing 字段使用固定值。
func Fingerprint(value any) (string, error) {
	normalized, err := normalizeJSON(value)
	if err != nil {
		return "", err
	}
	return fingerprintNormalized(normalized, true)
}

func validateApprovedDelta(delta ApprovedDelta) error {
	if strings.TrimSpace(delta.CaptureKey) == "" || strings.TrimSpace(delta.Path) == "" ||
		strings.TrimSpace(delta.Reason) == "" {
		return fmt.Errorf("final-wire approved delta 缺少 capture/path/reason")
	}
	for _, digest := range []string{delta.BeforeSHA256, delta.AfterSHA256} {
		if digest == missingFingerprint {
			continue
		}
		decoded, err := hex.DecodeString(digest)
		if err != nil || len(decoded) != sha256.Size {
			return fmt.Errorf("final-wire approved delta 摘要非法：%s %s", delta.CaptureKey, delta.Path)
		}
	}
	return nil
}

func normalizeJSON(value any) (any, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var normalized any
	if err := decoder.Decode(&normalized); err != nil {
		return nil, err
	}
	if decoder.More() {
		return nil, fmt.Errorf("规范 JSON 后存在额外值")
	}
	return normalized, nil
}

func compareValue(
	path string,
	left any,
	leftPresent bool,
	right any,
	rightPresent bool,
	differences *[]Difference,
) {
	if !leftPresent || !rightPresent {
		appendDifference(path, left, leftPresent, right, rightPresent, differences)
		return
	}
	leftObject, leftIsObject := left.(map[string]any)
	rightObject, rightIsObject := right.(map[string]any)
	if leftIsObject || rightIsObject {
		if !leftIsObject || !rightIsObject {
			appendDifference(path, left, true, right, true, differences)
			return
		}
		keys := make(map[string]bool, len(leftObject)+len(rightObject))
		for key := range leftObject {
			keys[key] = true
		}
		for key := range rightObject {
			keys[key] = true
		}
		ordered := make([]string, 0, len(keys))
		for key := range keys {
			ordered = append(ordered, key)
		}
		sort.Strings(ordered)
		for _, key := range ordered {
			leftValue, leftExists := leftObject[key]
			rightValue, rightExists := rightObject[key]
			compareValue(path+"/"+escapeJSONPointer(key), leftValue, leftExists, rightValue, rightExists, differences)
		}
		return
	}
	leftArray, leftIsArray := left.([]any)
	rightArray, rightIsArray := right.([]any)
	if leftIsArray || rightIsArray {
		if !leftIsArray || !rightIsArray {
			appendDifference(path, left, true, right, true, differences)
			return
		}
		leftRaw, _ := json.Marshal(leftArray)
		rightRaw, _ := json.Marshal(rightArray)
		if !bytes.Equal(leftRaw, rightRaw) {
			// Header、Body OrderedFields 与 WS EventMatrix 都是线序证据。
			// 数组以完整有序子树比较，既避免索引插入造成伪级联差异，也不会
			// 忽略任一名称、casing、值、摘要、ordinal 或 frame shape。
			appendDifference(path, left, true, right, true, differences)
		}
		return
	}
	leftRaw, _ := json.Marshal(left)
	rightRaw, _ := json.Marshal(right)
	if !bytes.Equal(leftRaw, rightRaw) {
		appendDifference(path, left, true, right, true, differences)
	}
}

func appendDifference(
	path string,
	left any,
	leftPresent bool,
	right any,
	rightPresent bool,
	differences *[]Difference,
) {
	if path == "" {
		path = "/"
	}
	leftDigest, _ := fingerprintNormalized(left, leftPresent)
	rightDigest, _ := fingerprintNormalized(right, rightPresent)
	*differences = append(*differences, Difference{
		Path: path, BeforeSHA256: leftDigest, AfterSHA256: rightDigest,
	})
}

func fingerprintNormalized(value any, present bool) (string, error) {
	if !present {
		return missingFingerprint, nil
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func escapeJSONPointer(value string) string {
	value = strings.ReplaceAll(value, "~", "~0")
	return strings.ReplaceAll(value, "/", "~1")
}
