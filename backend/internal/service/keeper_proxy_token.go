package service

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	KeeperInternalAuthContextKey      = "sub2apiplus_keeper_internal_token_auth"
	KeeperProxyTokenCapability        = "keeper_proxy"
	KeeperProxyTokenTTL               = 6 * time.Hour
	KeeperProxyMaxOutputTokensHardCap = 1024

	keeperProxyTokenVersion = "skp1"
)

type keeperProxyTokenClaims struct {
	AccountID  int64  `json:"account_id"`
	Platform   string `json:"platform"`
	Capability string `json:"capability"`
	ExpiresAt  int64  `json:"exp"`
}

func IssueKeeperProxyToken(secret string, accountID int64, platform string, now time.Time) (string, error) {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return "", errors.New("keeper internal token is not configured")
	}
	claims := keeperProxyTokenClaims{
		AccountID:  accountID,
		Platform:   normalizeKeeperProxyPlatform(platform),
		Capability: KeeperProxyTokenCapability,
		ExpiresAt:  keeperProxyTokenExpiresAt(now).Unix(),
	}
	if claims.AccountID <= 0 || claims.Platform == "" {
		return "", errors.New("keeper proxy token claims are incomplete")
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	encodedPayload := base64.RawURLEncoding.EncodeToString(payload)
	signature := signKeeperProxyTokenPayload(secret, encodedPayload)
	return keeperProxyTokenVersion + "." + encodedPayload + "." + signature, nil
}

func ValidateKeeperProxyToken(rawToken string, secret string, accountID int64, platform string, now time.Time) error {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return errors.New("keeper internal token is not configured")
	}
	rawToken = strings.TrimSpace(rawToken)
	parts := strings.Split(rawToken, ".")
	if len(parts) != 3 || parts[0] != keeperProxyTokenVersion {
		return errors.New("invalid keeper proxy token format")
	}
	expectedSignature := signKeeperProxyTokenPayload(secret, parts[1])
	if subtle.ConstantTimeCompare([]byte(parts[2]), []byte(expectedSignature)) != 1 {
		return errors.New("invalid keeper proxy token signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return fmt.Errorf("decode keeper proxy token: %w", err)
	}
	var claims keeperProxyTokenClaims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return fmt.Errorf("parse keeper proxy token: %w", err)
	}
	if claims.Capability != KeeperProxyTokenCapability {
		return errors.New("keeper proxy token capability is not allowed")
	}
	if claims.AccountID != accountID {
		return errors.New("keeper proxy token account does not match route")
	}
	if claims.Platform != normalizeKeeperProxyPlatform(platform) {
		return errors.New("keeper proxy token platform does not match route")
	}
	if claims.ExpiresAt <= now.UTC().Unix() {
		return errors.New("keeper proxy token has expired")
	}
	return nil
}

func keeperProxyTokenExpiresAt(now time.Time) time.Time {
	return now.UTC().Truncate(time.Hour).Add(KeeperProxyTokenTTL)
}

func signKeeperProxyTokenPayload(secret string, payload string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func normalizeKeeperProxyPlatform(platform string) string {
	switch strings.ToLower(strings.TrimSpace(platform)) {
	case PlatformOpenAI:
		return PlatformOpenAI
	case PlatformAnthropic:
		return PlatformAnthropic
	default:
		return ""
	}
}
