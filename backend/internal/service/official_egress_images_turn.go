package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// 独立 images 端点（images_generations／images_edits）的回合头。
//
// 官方 ext/image-generation 的 image_request_headers 会写两个头：
//   - x-codex-image-turn-id：生图工具调用所在回合的 sub_id（0.163 起就有，0.163 画像缺失）；
//   - x-codex-turn-metadata：当前回合 turn metadata 的 JSON 字符串（0.164 新增）。
//
// 两个头是否出站只由画像是否为该端点声明了对应槽位决定：旧画像没有槽位，本文件的逻辑一个
// 字节都不改，身份事实也不多生成（避免旧画像的 FinalizationToken 身份摘要漂移）。
// 两个槽位彼此独立，登记时可以只声明其中之一。

const (
	officialCodexImageTurnIDHeader  = "x-codex-image-turn-id"
	officialCodexTurnMetadataHeader = "x-codex-turn-metadata"
)

// officialCodexEndpointIsImages 判断端点是否是独立 images 端点。
func officialCodexEndpointIsImages(endpointID string) bool {
	return endpointID == officialCodexEndpointImagesGenerations ||
		endpointID == officialCodexEndpointImagesEdits
}

// officialCodexImagesHeaderDeclarations 读取 mode 对应画像里 images 端点是否声明了两个回合头槽位，
// 并返回画像版本（供 turn metadata 的 codex_version 使用）。mode 为空或画像不可解析时一律视为
// “未声明”，保持旧逻辑；正式请求在其他环节会因同一解析失败而失败关闭。
func officialCodexImagesHeaderDeclarations(mode, endpointID string) (imageTurnID, turnMetadata bool, version string) {
	if strings.TrimSpace(mode) == "" {
		return false, false, ""
	}
	profile, err := officialCodexExecutableProfileForMode(mode)
	if err != nil {
		return false, false, ""
	}
	endpoint, found := profile.Endpoint(endpointID)
	if !found {
		return false, false, ""
	}
	for _, slot := range endpoint.Headers {
		name := slot.WireName
		if name == "" {
			name = slot.Name
		}
		switch strings.ToLower(strings.TrimSpace(name)) {
		case officialCodexImageTurnIDHeader:
			imageTurnID = true
		case officialCodexTurnMetadataHeader:
			turnMetadata = true
		}
	}
	return imageTurnID, turnMetadata, profile.Version()
}

// officialCodexGeneratedUUIDLikeValue 由 invocation seed 与用途后缀确定性派生 8-4-4-4-12 形态的标识，
// 与 completeOfficialCodexGeneratedIdentityFacts 里 Responses 会话标识的派生口径一致。
func officialCodexGeneratedUUIDLikeValue(identitySeed, suffix string) string {
	sum := sha256.Sum256([]byte(identitySeed + "\x00" + suffix))
	return fmt.Sprintf(
		"%s-%s-%s-%s-%s",
		hex.EncodeToString(sum[0:4]), hex.EncodeToString(sum[4:6]),
		hex.EncodeToString(sum[6:8]), hex.EncodeToString(sum[8:10]),
		hex.EncodeToString(sum[10:16]),
	)
}

// marshalOfficialImagesTurnMetadata 生成 images 工具请求的 turn metadata JSON。
//
// 官方形态来自 current_meta_value_for_extension_request：与 Responses 的 turn metadata 不同，
// 它没有 request_kind、installation_id、window_id，去掉 agent_name、parent_turn_id、root_turn_id，
// 并追加 codex_version。这里只写有可信取值的键：session_id、thread_id、turn_id、thread_source、
// sandbox（常量与既有 Responses 兜底一致）和 codex_version（取画像版本，官方为编译期
// CARGO_PKG_VERSION）。
//
// 已知差距（需 0.164 生图实采校准，不在没有实采时臆造取值）：官方还会写 auto_review_enabled、
// node_repl_auto_review_required、node_repl_disabled、turn_started_at_unix_ms、model、
// reasoning_effort 等键，且键序取决于 serde_json 是否启用 preserve_order 以及 Map::remove 的换位语义。
func marshalOfficialImagesTurnMetadata(sessionID, threadID, turnID, codexVersion string) (string, error) {
	if strings.TrimSpace(turnID) == "" {
		return "", errors.New("images turn metadata 缺少 turn_id")
	}
	raw, err := json.Marshal(struct {
		SessionID    string `json:"session_id"`
		ThreadID     string `json:"thread_id"`
		TurnID       string `json:"turn_id"`
		ThreadSource string `json:"thread_source"`
		Sandbox      string `json:"sandbox"`
		CodexVersion string `json:"codex_version,omitempty"`
	}{
		SessionID: sessionID, ThreadID: threadID, TurnID: turnID,
		ThreadSource: "user", Sandbox: "seccomp", CodexVersion: codexVersion,
	})
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// completeOfficialCodexImagesTurnFacts 为 images 端点补齐画像声明的回合头所需的身份事实。
// 已由结构化身份或调用登记提供的事实原样保留，只补缺失的。
func completeOfficialCodexImagesTurnFacts(
	facts *officialegress.CodexIdentityFacts,
	endpointID string,
	identitySeed string,
	profileMode string,
) error {
	if facts == nil {
		return errors.New("Codex 身份事实为空")
	}
	imageTurnID, turnMetadata, version := officialCodexImagesHeaderDeclarations(profileMode, endpointID)
	if !imageTurnID && !turnMetadata {
		return nil
	}
	if facts.TurnID.Value == "" {
		turnID, err := officialegress.NewCodexIdentityValue(
			officialCodexGeneratedUUIDLikeValue(identitySeed, "turn"),
			officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleTurn,
		)
		if err != nil {
			return err
		}
		facts.TurnID = turnID
	}
	if !turnMetadata || facts.TurnMetadata.Value != "" {
		return nil
	}
	sessionID := facts.SessionID.Value
	if sessionID == "" {
		sessionID = officialCodexGeneratedUUIDLikeValue(identitySeed, "session")
	}
	threadID := facts.ThreadID.Value
	if threadID == "" {
		threadID = officialCodexGeneratedUUIDLikeValue(identitySeed, "thread")
	}
	metadata, err := marshalOfficialImagesTurnMetadata(sessionID, threadID, facts.TurnID.Value, version)
	if err != nil {
		return err
	}
	facts.TurnMetadata, err = officialegress.NewCodexIdentityValue(
		metadata, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn,
	)
	return err
}
