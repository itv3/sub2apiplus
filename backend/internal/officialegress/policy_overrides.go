package officialegress

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

const GuardPolicyOverrideSchemaVersion = 2

const MissingSinkIDOverrideTarget SinkID = "@missing"

type GuardPolicyOverrideKind string

const (
	GuardPolicyOverrideUnknownRoute     GuardPolicyOverrideKind = "unknown_route"
	GuardPolicyOverrideUnregisteredSink GuardPolicyOverrideKind = "unregistered_sink"
)

func (k GuardPolicyOverrideKind) Valid() bool {
	return k == GuardPolicyOverrideUnknownRoute || k == GuardPolicyOverrideUnregisteredSink
}

// GuardPolicyRouteScope 将临时例外限制到一个确定的物理 route。
// Path 是可安全记录的模板；unknown route 还必须携带精确 EscapedPath 的 SHA-256，
// 从而区分同一命名空间内的兄弟路径，同时避免把资源 ID 写入配置或日志。
type GuardPolicyRouteScope struct {
	Method     string       `json:"method"`
	Host       string       `json:"host"`
	Path       string       `json:"path"`
	PathSHA256 string       `json:"path_sha256,omitempty"`
	Protocol   WireProtocol `json:"protocol"`
}

func (s GuardPolicyRouteScope) Validate() error {
	if s.Method == "" || s.Method != strings.ToUpper(s.Method) ||
		normalizeRouteHost(s.Host) == "" || !strings.HasPrefix(s.Path, "/") || !s.Protocol.Valid() {
		return errors.New("策略覆盖的 route scope 非法")
	}
	return nil
}

// GuardPolicyOverride 是 unknown/unregistered 策略唯一允许的紧急 observe 输入。
// 每项都绑定部署实例和精确 route；无效 SinkBinding 还必须绑定具体 SinkID，
// 缺失 SinkID 使用保留值 @missing，禁止 route 级无限扩张。
type GuardPolicyOverride struct {
	Policy       GuardPolicyOverrideKind `json:"policy"`
	InstanceID   string                  `json:"instance_id"`
	SinkID       SinkID                  `json:"sink_id,omitempty"`
	Route        GuardPolicyRouteScope   `json:"route"`
	ObserveUntil time.Time               `json:"observe_until"`
	Owner        string                  `json:"owner"`
	ReasonCode   string                  `json:"reason_code"`
}

type guardPolicyOverrideManifest struct {
	SchemaVersion int                   `json:"schema_version"`
	Overrides     []GuardPolicyOverride `json:"overrides"`
}

func (o GuardPolicyOverride) Validate() error {
	if !o.Policy.Valid() || strings.TrimSpace(o.InstanceID) == "" || o.ObserveUntil.IsZero() ||
		strings.TrimSpace(o.Owner) == "" || strings.TrimSpace(o.ReasonCode) == "" {
		return errors.New("策略覆盖缺少 policy、instance_id、到期时间、责任人或 reason code")
	}
	if err := o.Route.Validate(); err != nil {
		return err
	}
	switch o.Policy {
	case GuardPolicyOverrideUnknownRoute:
		if !validGuardPolicyPathSHA256(o.Route.PathSHA256) {
			return errors.New("unknown_route 策略覆盖必须绑定精确 path_sha256")
		}
	case GuardPolicyOverrideUnregisteredSink:
		if strings.TrimSpace(string(o.SinkID)) == "" {
			return errors.New("unregistered_sink 策略覆盖必须绑定 sink_id；缺失 ID 使用 @missing")
		}
		if strings.TrimSpace(o.Route.PathSHA256) != "" {
			return errors.New("unregistered_sink 策略覆盖不得使用 unknown route 的 path_sha256")
		}
	}
	return nil
}

func validGuardPolicyPathSHA256(value string) bool {
	value = strings.TrimSpace(value)
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func (o GuardPolicyOverride) sortKey() string {
	return strings.Join([]string{
		string(o.Policy), strings.TrimSpace(o.InstanceID), o.Route.Method,
		normalizeRouteHost(o.Route.Host), o.Route.Path, o.Route.PathSHA256,
		string(o.Route.Protocol), string(o.SinkID),
	}, "\x00")
}

func (o GuardPolicyOverride) active(now time.Time) bool {
	return now.Before(o.ObserveUntil)
}

func (o GuardPolicyOverride) matches(
	policy GuardPolicyOverrideKind,
	instanceID string,
	method string,
	host string,
	path string,
	pathSHA256 string,
	protocol WireProtocol,
	sinkID SinkID,
) bool {
	if o.Policy != policy || strings.TrimSpace(o.InstanceID) != strings.TrimSpace(instanceID) ||
		o.Route.Method != strings.ToUpper(method) || normalizeRouteHost(o.Route.Host) != normalizeRouteHost(host) ||
		o.Route.Path != path || o.Route.Protocol != protocol {
		return false
	}
	if o.Policy == GuardPolicyOverrideUnknownRoute {
		return o.Route.PathSHA256 == pathSHA256 && (o.SinkID == "" || o.SinkID == sinkID)
	}
	if o.SinkID == MissingSinkIDOverrideTarget {
		return sinkID == ""
	}
	return o.SinkID == sinkID
}

// ParseGuardPolicyOverrides 严格解析按作用域排序的限时策略覆盖清单。
func ParseGuardPolicyOverrides(raw string) ([]GuardPolicyOverride, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var manifest guardPolicyOverrideManifest
	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("解析 Guard policy overrides：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("Guard policy overrides 尾部存在额外 JSON")
		}
		return nil, err
	}
	if manifest.SchemaVersion != GuardPolicyOverrideSchemaVersion {
		return nil, errors.New("Guard policy overrides schema_version 非法")
	}
	previous := ""
	for index, override := range manifest.Overrides {
		if err := override.Validate(); err != nil {
			return nil, fmt.Errorf("Guard policy override[%d]：%w", index, err)
		}
		key := override.sortKey()
		if previous >= key {
			return nil, errors.New("Guard policy overrides 必须按作用域严格排序且不得重复")
		}
		previous = key
	}
	return append([]GuardPolicyOverride(nil), manifest.Overrides...), nil
}
