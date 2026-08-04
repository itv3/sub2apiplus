package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

const SinkRuntimeControlSchemaVersion = 1

// SinkRuntimeControl 是 1C 紧急回滚的运行时输入。
// State 只允许把已验收 enforced Sink 降为 canary_enforce；
// ObserveUntil 是正交的限时 observe 覆盖，不会改写 MigrationReceipt。
type SinkRuntimeControl struct {
	SinkID       SinkID               `json:"sink_id"`
	State        SinkEnforcementState `json:"state,omitempty"`
	ObserveUntil *time.Time           `json:"observe_until,omitempty"`
	Owner        string               `json:"owner,omitempty"`
	ReasonCode   string               `json:"reason_code,omitempty"`
}

type sinkRuntimeControlManifest struct {
	SchemaVersion int                  `json:"schema_version"`
	Controls      []SinkRuntimeControl `json:"controls"`
}

// ParseSinkRuntimeControls 严格解析已排序的运行时控制清单。
// 空值等价于没有控制，便于旧部署无损升级。
func ParseSinkRuntimeControls(raw string) ([]SinkRuntimeControl, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var manifest sinkRuntimeControlManifest
	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("解析 Sink runtime controls：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("Sink runtime controls 尾部存在额外 JSON")
		}
		return nil, err
	}
	if manifest.SchemaVersion != SinkRuntimeControlSchemaVersion {
		return nil, errors.New("Sink runtime controls schema_version 非法")
	}
	previous := ""
	for index, control := range manifest.Controls {
		if err := validateSinkRuntimeControl(control); err != nil {
			return nil, fmt.Errorf("Sink runtime control[%d]：%w", index, err)
		}
		if previous >= string(control.SinkID) {
			return nil, errors.New("Sink runtime controls 必须按 sink_id 严格排序且不得重复")
		}
		previous = string(control.SinkID)
	}
	return append([]SinkRuntimeControl(nil), manifest.Controls...), nil
}

func validateSinkRuntimeControl(control SinkRuntimeControl) error {
	if strings.TrimSpace(string(control.SinkID)) == "" {
		return errors.New("sink_id 为空")
	}
	if control.State != "" && control.State != SinkStateCanaryEnforce {
		return errors.New("state 只允许 canary_enforce 降级")
	}
	hasOverride := control.ObserveUntil != nil || strings.TrimSpace(control.Owner) != "" ||
		strings.TrimSpace(control.ReasonCode) != ""
	if control.State == "" && !hasOverride {
		return errors.New("控制项未声明降级或限时覆盖")
	}
	if hasOverride {
		if control.ObserveUntil == nil {
			return errors.New("限时覆盖缺少 observe_until")
		}
		override := SinkGuardOverride{
			ObserveUntil: *control.ObserveUntil,
			Owner:        strings.TrimSpace(control.Owner), ReasonCode: strings.TrimSpace(control.ReasonCode),
		}
		if err := override.Validate(); err != nil {
			return err
		}
	}
	return nil
}

// WithRuntimeControls 在不修改嵌入 Catalog 的前提下构造进程级快照。
// 它严格禁止升级、禁止将 legacy 借覆盖变成新的宽松基线。
func (c SinkCatalog) WithRuntimeControls(controls []SinkRuntimeControl) (SinkCatalog, error) {
	cloned := SinkCatalog{bindings: make(map[SinkID]SinkBinding, len(c.bindings))}
	for sinkID, binding := range c.bindings {
		cloned.bindings[sinkID] = binding
	}
	for _, control := range controls {
		if err := validateSinkRuntimeControl(control); err != nil {
			return SinkCatalog{}, fmt.Errorf("Sink %s：%w", control.SinkID, err)
		}
		binding, ok := cloned.bindings[control.SinkID]
		if !ok || !binding.RuntimeBindable() {
			return SinkCatalog{}, fmt.Errorf("Sink runtime control 引用未登记或不可绑定 Sink：%s", control.SinkID)
		}
		if binding.EnforcementState() != SinkStateEnforced || binding.migrationReceipt == nil {
			return SinkCatalog{}, fmt.Errorf("Sink %s 未处于可回滚的 enforced 状态", control.SinkID)
		}
		if control.State == SinkStateCanaryEnforce {
			binding.enforcementState = SinkStateCanaryEnforce
		}
		if control.ObserveUntil != nil {
			binding.override = &SinkGuardOverride{
				ObserveUntil: *control.ObserveUntil,
				Owner:        strings.TrimSpace(control.Owner), ReasonCode: strings.TrimSpace(control.ReasonCode),
			}
		}
		cloned.bindings[control.SinkID] = binding
	}
	return cloned, nil
}
