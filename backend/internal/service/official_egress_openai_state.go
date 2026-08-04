package service

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
)

// officialOpenAIIdentityProvenance 区分“值是什么”和“值从哪里来”。非空字符串本身
// 不能证明可跨请求复用。
type officialOpenAIIdentityProvenance string

const (
	officialOpenAIProvenanceExplicitIngress            officialOpenAIIdentityProvenance = "explicit_ingress"
	officialOpenAIProvenanceExplicitSessionDerivedTurn officialOpenAIIdentityProvenance = "explicit_session_derived_turn"
	officialOpenAIProvenanceContentFallback            officialOpenAIIdentityProvenance = "content_fallback"
	officialOpenAIProvenanceGeneratedRequestLocal      officialOpenAIIdentityProvenance = "generated_request_local"
)

func (p officialOpenAIIdentityProvenance) valid() bool {
	switch p {
	case officialOpenAIProvenanceExplicitIngress,
		officialOpenAIProvenanceExplicitSessionDerivedTurn,
		officialOpenAIProvenanceContentFallback,
		officialOpenAIProvenanceGeneratedRequestLocal:
		return true
	default:
		return false
	}
}

// officialOpenAITurnStateScope 是跨请求 store 的完整身份输入。所有字段都必须在
// ReleaseBundle、账号选择与 session/turn 来源冻结后构造。
type officialOpenAITurnStateScope struct {
	groupTenantID     int64
	localAccountID    int64
	upstreamAuthority string
	releaseDigest     string
	sessionIdentity   string
	turnIdentity      string
	sessionProvenance officialOpenAIIdentityProvenance
	turnProvenance    officialOpenAIIdentityProvenance
}

func newOfficialOpenAITurnStateScope(
	groupTenantID int64,
	localAccountID int64,
	upstreamAuthority string,
	releaseDigest string,
	identity officialOpenAIHTTPIdentity,
) officialOpenAITurnStateScope {
	return officialOpenAITurnStateScope{
		groupTenantID:     groupTenantID,
		localAccountID:    localAccountID,
		upstreamAuthority: strings.TrimSpace(upstreamAuthority),
		releaseDigest:     strings.TrimSpace(releaseDigest),
		sessionIdentity:   strings.TrimSpace(identity.sessionID),
		turnIdentity:      strings.TrimSpace(identity.turnID),
		sessionProvenance: identity.sessionProvenance,
		turnProvenance:    identity.turnProvenance,
	}
}

// persistentStoreKey 只批准两种跨请求来源组合：整组身份由官方入口显式给出，或
// turn 在显式会话内由内容推导。纯内容兜底和请求内生成身份永不进入 store。
func (s officialOpenAITurnStateScope) persistentStoreKey() string {
	if s.groupTenantID <= 0 || s.localAccountID <= 0 || s.sessionIdentity == "" ||
		s.turnIdentity == "" || !s.sessionProvenance.valid() || !s.turnProvenance.valid() ||
		!validOfficialTurnStateReleaseDigest(s.releaseDigest) ||
		!validNormalizedOfficialAuthority(s.upstreamAuthority) {
		return ""
	}
	approved := s.sessionProvenance == officialOpenAIProvenanceExplicitIngress &&
		(s.turnProvenance == officialOpenAIProvenanceExplicitIngress ||
			s.turnProvenance == officialOpenAIProvenanceExplicitSessionDerivedTurn)
	if !approved {
		return ""
	}
	parts := []string{
		"group_tenant", strconv.FormatInt(s.groupTenantID, 10),
		"local_account", strconv.FormatInt(s.localAccountID, 10),
		"upstream_authority", s.upstreamAuthority,
		"release_digest", s.releaseDigest,
		"session_identity", s.sessionIdentity,
		"turn_identity", s.turnIdentity,
		"session_provenance", string(s.sessionProvenance),
		"turn_provenance", string(s.turnProvenance),
	}
	hasher := sha256.New()
	_, _ = hasher.Write([]byte("sub2api.turn-state.v1\x00"))
	for _, part := range parts {
		_, _ = fmt.Fprintf(hasher, "%d:", len(part))
		_, _ = hasher.Write([]byte(part))
		_, _ = hasher.Write([]byte{0})
	}
	return "http-turn:v1:" + hex.EncodeToString(hasher.Sum(nil))
}

func validOfficialTurnStateReleaseDigest(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validNormalizedOfficialAuthority(value string) bool {
	parsed, err := url.Parse(value)
	if err != nil || parsed.User != nil || parsed.Path != "" || parsed.RawQuery != "" ||
		parsed.Fragment != "" || parsed.Hostname() == "" || parsed.Port() == "" {
		return false
	}
	scheme := strings.ToLower(parsed.Scheme)
	if scheme != "https" && scheme != "http" && scheme != "wss" && scheme != "ws" {
		return false
	}
	normalized := scheme + "://" + net.JoinHostPort(
		strings.ToLower(parsed.Hostname()), parsed.Port(),
	)
	return value == normalized
}
