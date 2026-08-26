package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"golang.org/x/net/http/httpguts"
)

// CodexRoutingHintFacts 是从最终 Responses 语义 Body 中提取的路由事实。
// 字段保持私有，service 只能通过有序 JSON 解析器取得该值，不能把普通
// Header、账号 override 或入站自报身份直接提升为官方路由提示。
type CodexRoutingHintFacts struct {
	model              string
	serviceTier        string
	serviceTierPresent bool
}

// ParseOfficialCodexRoutingHintFacts 从顶层 JSON object 提取 model 和
// service_tier，并沿用官方 Body 的重复键失败关闭规则。
func ParseOfficialCodexRoutingHintFacts(
	endpointID string,
	body []byte,
) (CodexRoutingHintFacts, error) {
	if !officialCodexRoutingHintEndpoint(endpointID) {
		return CodexRoutingHintFacts{}, fmt.Errorf("端点 %s 不允许 Codex routing hint", endpointID)
	}
	document, err := newOrderedJSONDocument(body)
	if err != nil {
		return CodexRoutingHintFacts{}, fmt.Errorf("解析 Codex routing hint Body：%w", err)
	}
	return codexRoutingHintFactsFromDocument(document)
}

func officialCodexRoutingHintEndpoint(endpointID string) bool {
	switch endpointID {
	case "responses_http", "responses_compact", "responses_ws":
		return true
	default:
		return false
	}
}

func codexRoutingHintFactsFromDocument(
	document *orderedJSONDocument,
) (CodexRoutingHintFacts, error) {
	return parseCodexRoutingHintFactsFromDocument(document, true)
}

// optionalCodexRoutingHintFactsFromDocument 允许旧画像的合成请求不携带
// model。是否必须生成 routing hint 仍由目标 Profile 的 Header 槽位决定；
// 目标画像声明该槽位时，零值最终会在 Compiler 中失败关闭。
func optionalCodexRoutingHintFactsFromDocument(
	document *orderedJSONDocument,
) (CodexRoutingHintFacts, error) {
	return parseCodexRoutingHintFactsFromDocument(document, false)
}

func parseCodexRoutingHintFactsFromDocument(
	document *orderedJSONDocument,
	requireModel bool,
) (CodexRoutingHintFacts, error) {
	if document == nil || !document.duplicatesChecked {
		return CodexRoutingHintFacts{}, errors.New("Codex routing hint Body 未完成 duplicate 校验")
	}
	modelRaw, present := document.value("model")
	if !present {
		if !requireModel {
			return CodexRoutingHintFacts{}, nil
		}
		return CodexRoutingHintFacts{}, errors.New("Codex routing hint Body 缺少 model")
	}
	var model string
	if err := json.Unmarshal(modelRaw, &model); err != nil || strings.TrimSpace(model) == "" {
		return CodexRoutingHintFacts{}, errors.New("Codex routing hint model 非法")
	}

	facts := CodexRoutingHintFacts{model: model}
	if tierRaw, tierPresent := document.value("service_tier"); tierPresent {
		if strings.EqualFold(strings.TrimSpace(string(tierRaw)), "null") {
			return facts, facts.Validate()
		}
		if err := json.Unmarshal(tierRaw, &facts.serviceTier); err != nil {
			return CodexRoutingHintFacts{}, errors.New("Codex routing hint service_tier 非法")
		}
		facts.serviceTierPresent = true
	}
	return facts, facts.Validate()
}

func (f CodexRoutingHintFacts) IsZero() bool {
	return f.model == "" && f.serviceTier == "" && !f.serviceTierPresent
}

func (f CodexRoutingHintFacts) Model() string { return f.model }

func (f CodexRoutingHintFacts) ServiceTier() (string, bool) {
	return f.serviceTier, f.serviceTierPresent
}

func (f CodexRoutingHintFacts) HeaderValue() (string, error) {
	if err := f.Validate(); err != nil {
		return "", err
	}
	value := "model=" + f.model
	if f.serviceTierPresent {
		value += ";tier=" + f.serviceTier
	}
	if !httpguts.ValidHeaderFieldValue(value) {
		return "", errors.New("Codex routing hint 不是合法 Header 值")
	}
	return value, nil
}

func (f CodexRoutingHintFacts) Validate() error {
	if strings.TrimSpace(f.model) == "" {
		return errors.New("Codex routing hint 缺少 model")
	}
	value := "model=" + f.model
	if f.serviceTierPresent {
		value += ";tier=" + f.serviceTier
	}
	if !httpguts.ValidHeaderFieldValue(value) {
		return errors.New("Codex routing hint 包含非法 Header 字节")
	}
	return nil
}

func (f CodexRoutingHintFacts) Digest() string {
	if f.IsZero() {
		return ""
	}
	presence := "absent"
	if f.serviceTierPresent {
		presence = "present"
	}
	sum := sha256.Sum256([]byte(strings.Join(
		[]string{"codex-routing-hint", f.model, presence, f.serviceTier},
		"\x00",
	)))
	return hex.EncodeToString(sum[:])
}
