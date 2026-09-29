package profilecontract

import (
	"errors"
	"fmt"
	"sort"
)

type EnumDomain string

const (
	EnumDomainBodyEncoding    EnumDomain = "body_encoding"
	EnumDomainCompressionKind EnumDomain = "compression_kind"
	EnumDomainConditionKind   EnumDomain = "condition_kind"
	EnumDomainHeaderOrderKind EnumDomain = "header_order_kind"
	EnumDomainLifecycleKind   EnumDomain = "lifecycle_kind"
	EnumDomainOmitCondition   EnumDomain = "omit_condition"
	EnumDomainValueSource     EnumDomain = "value_source"
)

var allEnumDomains = []EnumDomain{
	EnumDomainBodyEncoding,
	EnumDomainCompressionKind,
	EnumDomainConditionKind,
	EnumDomainHeaderOrderKind,
	EnumDomainLifecycleKind,
	EnumDomainOmitCondition,
	EnumDomainValueSource,
}

func (d EnumDomain) Valid() bool {
	for _, candidate := range allEnumDomains {
		if d == candidate {
			return true
		}
	}
	return false
}

// EnumCatalog 是不可变枚举集合。
//
// Observed 由全部快照生成；EngineSupported 由执行引擎显式维护，两者不能共用一张表。
type EnumCatalog struct {
	values map[EnumDomain]map[string]struct{}
}

func NewEnumCatalog(input map[EnumDomain][]string) (EnumCatalog, error) {
	if input == nil {
		return EnumCatalog{}, errors.New("枚举目录为空")
	}
	values := make(map[EnumDomain]map[string]struct{}, len(input))
	for domain, items := range input {
		if !domain.Valid() {
			return EnumCatalog{}, fmt.Errorf("未知枚举域: %s", domain)
		}
		set := make(map[string]struct{}, len(items))
		for _, item := range items {
			if _, exists := set[item]; exists {
				return EnumCatalog{}, fmt.Errorf("枚举域 %s 包含重复值 %q", domain, item)
			}
			set[item] = struct{}{}
		}
		values[domain] = set
	}
	return EnumCatalog{values: values}, nil
}

func mustEnumCatalog(input map[EnumDomain][]string) EnumCatalog {
	catalog, err := NewEnumCatalog(input)
	if err != nil {
		panic(err)
	}
	return catalog
}

func (c EnumCatalog) Contains(domain EnumDomain, value string) bool {
	_, ok := c.values[domain][value]
	return ok
}

func (c EnumCatalog) Values(domain EnumDomain) []string {
	set := c.values[domain]
	out := make([]string, 0, len(set))
	for value := range set {
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func (c EnumCatalog) Domains() []EnumDomain {
	out := make([]EnumDomain, 0, len(c.values))
	for domain := range c.values {
		out = append(out, domain)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (c EnumCatalog) AsMap() map[EnumDomain][]string {
	out := make(map[EnumDomain][]string, len(c.values))
	for domain := range c.values {
		out[domain] = c.Values(domain)
	}
	return out
}

// ValidateObservedSubset 是 0B 的升级门禁：所有观测值都必须被执行引擎明确支持。
func ValidateObservedSubset(observed, supported EnumCatalog) error {
	var missing []string
	for _, domain := range observed.Domains() {
		for _, value := range observed.Values(domain) {
			if !supported.Contains(domain, value) {
				missing = append(missing, fmt.Sprintf("%s=%q", domain, value))
			}
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return fmt.Errorf("画像含执行引擎未支持的枚举值: %v", missing)
	}
	return nil
}

// 执行引擎已声明、但任何入库快照都未观测到的枚举值（生成器只为观测值生成常量，见 enums_gen.go）。
// guardian 审阅两个条件与 prompt_cache_key 来源已随 0.157.0 目标快照纳入 testdata、由 enums_gen.go 生成，
// 本段只剩首期失败关闭的工作区路由 override 条件：目标画像不引用它，将来某个快照观测到它时，
// 重新生成 enums_gen.go 会产生同名常量，届时删除本段（遗留会因重复定义编译失败）。
const (
	// ConditionAccountRoutingOverridePresent：工作区路由返回需要 override 的结果（首期失败关闭，不会成立）。
	ConditionAccountRoutingOverridePresent ConditionKind = "account_routing_override_present"
)

// EngineSupportedEnumValues 是执行引擎认可的显式闭集。
//
// 新快照加入观测值时，不能由生成器自动扩充本表；维护者必须确认消费语义后手工登记。
func EngineSupportedEnumValues() EnumCatalog {
	return mustEnumCatalog(map[EnumDomain][]string{
		EnumDomainBodyEncoding: {
			string(BodyFormUrlencoded),
			string(BodyJson),
			string(BodyNone),
			string(BodyRawBytes),
			string(BodyWebsocketDiscriminatedEvents),
			string(BodyWebsocketJson),
		},
		EnumDomainCompressionKind: {
			string(CompressionNone),
			string(CompressionPermessageDeflateContextTakeover),
			string(CompressionZstdWhenFeatureEnabled),
		},
		EnumDomainConditionKind: {
			string(ConditionUnconditional),
			string(ConditionAlways),
			string(ConditionAttestationPresent),
			string(ConditionAuto),
			string(ConditionBetaFeaturesPresent),
			string(ConditionCookiePresent),
			string(ConditionCreditIdPresent),
			string(ConditionFedrampAccount),
			string(ConditionHostedFileUpload),
			string(ConditionManagedResidencyPresent),
			string(ConditionMemoryGeneration),
			string(ConditionParentThreadPresent),
			string(ConditionRemoteCompactionV2),
			string(ConditionRequestCompressionEnabled),
			string(ConditionResponsesLite),
			string(ConditionRuntimeMetrics),
			string(ConditionSessionIdPresent),
			string(ConditionSubagentPresent),
			string(ConditionTurnStatePresent),
			string(ConditionLunaReservePresent),
			string(ConditionGuardianReviewRequest),
			string(ConditionNotGuardianReviewRequest),
			string(ConditionAccountRoutingOverridePresent),
		},
		EnumDomainHeaderOrderKind: {
			string(HeaderOrderExplicitOrder),
			string(HeaderOrderH1HeaderMapFinalOrder),
			string(HeaderOrderWsFixedPrefixThenHeaderMapSwapRemove),
		},
		EnumDomainLifecycleKind: {
			string(LifecycleBackendClientLongLived),
			string(LifecyclePerUpperApiCall),
			string(LifecycleReturnedUploadUrlCall),
			string(LifecycleWebsocketConnection),
		},
		EnumDomainOmitCondition: {
			string(OmitNever),
			string(OmitEmptyString),
			string(OmitNone),
			string(OmitNoneOrUnreusablePrefix),
		},
		EnumDomainValueSource: {
			string(SourceAccount),
			string(SourceAuthentication),
			string(SourceConstant),
			string(SourceFeature),
			string(SourceGenerated),
			string(SourceManagedConfig),
			string(SourceModelManifest),
			string(SourceProcess),
			string(SourceRequestBody),
			string(SourceServerResponse),
			string(SourceSession),
			string(SourceTurn),
			string(SourcePromptCacheKey),
		},
	})
}
