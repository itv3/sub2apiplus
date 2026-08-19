package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
)

// ManagedEgressPolicy 描述不主张官方客户端 wire 等价、但仍必须受管的出站。
// route 与 backend 继续由 SinkBinding 冻结；本策略补齐认证和运行治理责任。
type ManagedEgressPolicy struct {
	ID             string `json:"id"`
	Source         string `json:"source"`
	Authentication string `json:"authentication"`
	Endpoint       string `json:"endpoint"`
	Client         string `json:"client"`
	TimeoutPolicy  string `json:"timeout_policy"`
	RetryPolicy    string `json:"retry_policy"`
	SecretPolicy   string `json:"secret_policy"`
	AuditPolicy    string `json:"audit_policy"`
}

func (p ManagedEgressPolicy) validate() error {
	for _, value := range []string{
		p.ID, p.Source, p.Authentication, p.Endpoint, p.Client,
		p.TimeoutPolicy, p.RetryPolicy, p.SecretPolicy, p.AuditPolicy,
	} {
		if strings.TrimSpace(value) == "" {
			return errors.New("ManagedEgressPolicy 字段不完整")
		}
	}
	return nil
}

func (p ManagedEgressPolicy) Digest() string {
	raw, _ := json.Marshal(p)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
