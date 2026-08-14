package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// EndpointBindingKey 把已审核业务身份连接到中立物理路由。EndpointID 从此键解析，
// 业务调用方不能直接提交一个 EndpointID 后获得信任。
type EndpointBindingKey struct {
	SinkID          SinkID
	Purpose         Purpose
	PhysicalRouteID PhysicalRouteID
	Protocol        WireProtocol
}

func (k EndpointBindingKey) identity() string {
	return strings.Join([]string{
		string(k.SinkID), string(k.Purpose), string(k.PhysicalRouteID), string(k.Protocol),
	}, "\x00")
}

// EndpointBinding 是静态证据连接，不包含部署期事实。
type EndpointBinding struct {
	key            EndpointBindingKey
	endpointID     string
	releasePurpose string
	evidenceDigest string
}

func (b EndpointBinding) Key() EndpointBindingKey { return b.key }
func (b EndpointBinding) EndpointID() string      { return b.endpointID }
func (b EndpointBinding) ReleasePurpose() string  { return b.releasePurpose }
func (b EndpointBinding) EvidenceDigest() string  { return b.evidenceDigest }

type EndpointBindingCatalog struct {
	byIdentity map[string]EndpointBinding
}

func NewEndpointBindingCatalog(
	sinks SinkCatalog,
	physical PhysicalRouteCatalog,
	profile profilecontract.ExecutableProfile,
) (EndpointBindingCatalog, error) {
	catalog := EndpointBindingCatalog{byIdentity: make(map[string]EndpointBinding)}
	for _, sink := range sinks.Bindings() {
		if sink.Persona() != PersonaCodexCLI ||
			sink.EndpointEvidence() != EndpointEvidenceCodexProfile {
			continue
		}
		for _, route := range sink.Routes() {
			physicalID, physicalKey, ok := physical.ResolveRoute(route)
			if !ok {
				return EndpointBindingCatalog{}, fmt.Errorf("Sink %s 缺少物理路由", sink.ID())
			}
			endpoint, present, err := resolveProfileEndpointForBinding(sink, physicalID, physicalKey, profile)
			if err != nil {
				return EndpointBindingCatalog{}, err
			}
			if !present {
				// 版本新增端点允许只在包含它的画像中生成 binding。跨画像至少命中一次
				// 的不变量由 BundleResolver 与 RouteCatalog 在同时看到两种 mode 时校验；
				// 此处只吸收“零匹配”，多匹配和 override 错误仍然失败关闭。
				continue
			}
			releasePurpose := RegistryPurposeOpenAIOAuthHTTP
			if route.Protocol == WireProtocolWebSocket {
				releasePurpose = RegistryPurposeOpenAIOAuthWS
			}
			digest, err := endpointEvidenceDigest(sink, physicalID, endpoint, releasePurpose)
			if err != nil {
				return EndpointBindingCatalog{}, err
			}
			binding := EndpointBinding{
				key: EndpointBindingKey{
					SinkID: sink.ID(), Purpose: sink.Purpose(),
					PhysicalRouteID: physicalID, Protocol: route.Protocol,
				},
				endpointID: endpoint.ID, releasePurpose: releasePurpose, evidenceDigest: digest,
			}
			identity := binding.key.identity()
			if _, duplicate := catalog.byIdentity[identity]; duplicate {
				return EndpointBindingCatalog{}, fmt.Errorf("EndpointBinding 重复: %s", identity)
			}
			catalog.byIdentity[identity] = binding
		}
	}
	return catalog, nil
}

func resolveProfileEndpointForBinding(
	sink SinkBinding,
	physicalID PhysicalRouteID,
	physical PhysicalRouteKey,
	profile profilecontract.ExecutableProfile,
) (profilecontract.ExecutableEndpointProfile, bool, error) {
	matches := make([]profilecontract.ExecutableEndpointProfile, 0, 1)
	for _, endpoint := range profile.Endpoints() {
		protocol := WireProtocolHTTP
		if strings.EqualFold(strings.TrimSpace(endpoint.Upgrade), "websocket") {
			protocol = WireProtocolWebSocket
		}
		path := endpoint.Path
		if !strings.HasPrefix(path, "/") {
			path = "/" + path
		}
		if endpoint.Method == physical.Method && protocol == physical.Protocol &&
			normalizeRouteHost(endpoint.Host) == normalizeRouteHost(physical.Host) && path == physical.Path {
			matches = append(matches, endpoint)
		}
	}

	// `/oauth/token` 是已知同物理 route 多逻辑端点。refresh 的授权来自完整 binding
	// tuple；exchange 在正式证据落库前是 transport_only，根本不会进入本 Catalog。
	overrideID := endpointBindingOverride(sink, physicalID, physical)
	if overrideID != "" {
		for _, endpoint := range matches {
			if endpoint.ID == overrideID {
				return endpoint, true, nil
			}
		}
		return profilecontract.ExecutableEndpointProfile{}, false, fmt.Errorf(
			"Sink %s 的 EndpointBinding override %s 不在 ProfileSpec",
			sink.ID(), overrideID,
		)
	}
	if len(matches) == 0 {
		return profilecontract.ExecutableEndpointProfile{}, false, nil
	}
	if len(matches) != 1 {
		return profilecontract.ExecutableEndpointProfile{}, false, fmt.Errorf(
			"Sink %s 的业务 route 必须唯一连接 EndpointBinding，实际匹配 %d 个",
			sink.ID(), len(matches),
		)
	}
	return matches[0], true, nil
}

func uniqueProfileEndpointForPhysical(
	profile profilecontract.ExecutableProfile,
	physical PhysicalRouteKey,
) (profilecontract.ExecutableEndpointProfile, bool) {
	var found profilecontract.ExecutableEndpointProfile
	count := 0
	for _, endpoint := range profile.Endpoints() {
		protocol := WireProtocolHTTP
		if strings.EqualFold(strings.TrimSpace(endpoint.Upgrade), "websocket") {
			protocol = WireProtocolWebSocket
		}
		path := endpoint.Path
		if !strings.HasPrefix(path, "/") {
			path = "/" + path
		}
		if endpoint.Method == physical.Method && protocol == physical.Protocol &&
			normalizeRouteHost(endpoint.Host) == normalizeRouteHost(physical.Host) && path == physical.Path {
			found = endpoint
			count++
		}
	}
	return found, count == 1
}

func endpointBindingOverride(
	sink SinkBinding,
	_ PhysicalRouteID,
	physical PhysicalRouteKey,
) string {
	if physical.Method == "POST" && normalizeRouteHost(physical.Host) == "auth.openai.com" &&
		physical.Path == "/oauth/token" && physical.Protocol == WireProtocolHTTP &&
		sink.ID() == SinkCodexOAuthRefresh && sink.Purpose() == Purpose("oauth_refresh") {
		return "oauth_refresh"
	}
	return ""
}

func endpointEvidenceDigest(
	sink SinkBinding,
	physicalID PhysicalRouteID,
	endpoint profilecontract.ExecutableEndpointProfile,
	releasePurpose string,
) (string, error) {
	payload := struct {
		SinkID          SinkID
		Purpose         Purpose
		PhysicalRouteID PhysicalRouteID
		EndpointID      string
		ReleasePurpose  string
		Endpoint        profilecontract.ExecutableEndpointProfile
		Source          string
	}{
		SinkID: sink.ID(), Purpose: sink.Purpose(), PhysicalRouteID: physicalID,
		EndpointID: endpoint.ID, ReleasePurpose: releasePurpose, Endpoint: endpoint,
		Source: "compiled-executable-profile+effective-sink-catalog",
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func (c EndpointBindingCatalog) Resolve(key EndpointBindingKey) (EndpointBinding, bool) {
	binding, ok := c.byIdentity[key.identity()]
	return binding, ok
}

func (c EndpointBindingCatalog) ResolveBindingRoute(
	sink SinkBinding,
	route CatalogRoute,
	physical PhysicalRouteCatalog,
) (EndpointBinding, bool) {
	physicalID, _, ok := physical.ResolveRoute(route)
	if !ok {
		return EndpointBinding{}, false
	}
	return c.Resolve(EndpointBindingKey{
		SinkID: sink.ID(), Purpose: sink.Purpose(),
		PhysicalRouteID: physicalID, Protocol: route.Protocol,
	})
}

func (c EndpointBindingCatalog) Bindings() []EndpointBinding {
	keys := make([]string, 0, len(c.byIdentity))
	for key := range c.byIdentity {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]EndpointBinding, 0, len(keys))
	for _, key := range keys {
		out = append(out, c.byIdentity[key])
	}
	return out
}
