package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"strings"
)

// PhysicalRouteID 是物理路由五元组的稳定内容地址。它不包含业务 purpose，且不由
// 数组位置或展示名称生成；Guard 日志仍输出可读的 method/host/path/protocol。
type PhysicalRouteID string

// PhysicalRouteKey 只描述网络可观察的 route，不携带业务身份。
type PhysicalRouteKey struct {
	Method   string
	Host     string
	Path     string
	Protocol WireProtocol
}

func (k PhysicalRouteKey) Validate() error {
	if strings.TrimSpace(k.Method) == "" || k.Method != strings.ToUpper(k.Method) ||
		strings.TrimSpace(k.Host) == "" || !strings.HasPrefix(k.Path, "/") || !k.Protocol.Valid() {
		return errors.New("PhysicalRouteKey 字段非法")
	}
	return nil
}

func (k PhysicalRouteKey) identity() string {
	return strings.Join([]string{
		strings.ToUpper(strings.TrimSpace(k.Method)),
		normalizeRouteHost(k.Host), strings.TrimSpace(k.Path), string(k.Protocol),
	}, "\x00")
}

func physicalRouteID(key PhysicalRouteKey) PhysicalRouteID {
	sum := sha256.Sum256([]byte(key.identity()))
	return PhysicalRouteID("physical:" + hex.EncodeToString(sum[:16]))
}

func physicalRouteFromCatalogRoute(route CatalogRoute) PhysicalRouteKey {
	return PhysicalRouteKey{
		Method: route.Key.Method, Host: route.Key.Host,
		Path: route.Key.Path, Protocol: route.Protocol,
	}
}

// PhysicalRouteCatalog 是进程级不可变物理路由闭集。
type PhysicalRouteCatalog struct {
	byIdentity map[string]PhysicalRouteKey
	byID       map[PhysicalRouteID]PhysicalRouteKey
}

func NewPhysicalRouteCatalog(sinks SinkCatalog) (PhysicalRouteCatalog, error) {
	catalog := PhysicalRouteCatalog{
		byIdentity: make(map[string]PhysicalRouteKey),
		byID:       make(map[PhysicalRouteID]PhysicalRouteKey),
	}
	for _, binding := range sinks.Bindings() {
		for _, route := range binding.Routes() {
			key := physicalRouteFromCatalogRoute(route)
			if err := key.Validate(); err != nil {
				return PhysicalRouteCatalog{}, fmt.Errorf("Sink %s: %w", binding.ID(), err)
			}
			identity := key.identity()
			id := physicalRouteID(key)
			if existing, ok := catalog.byID[id]; ok && existing.identity() != identity {
				return PhysicalRouteCatalog{}, fmt.Errorf("PhysicalRouteID 摘要冲突: %s", id)
			}
			catalog.byIdentity[identity] = key
			catalog.byID[id] = key
		}
	}
	if len(catalog.byID) == 0 {
		return PhysicalRouteCatalog{}, errors.New("PhysicalRouteCatalog 为空")
	}
	return catalog, nil
}

func (c PhysicalRouteCatalog) ResolveRoute(route CatalogRoute) (PhysicalRouteID, PhysicalRouteKey, bool) {
	key := physicalRouteFromCatalogRoute(route)
	registered, ok := c.byIdentity[key.identity()]
	if !ok {
		return "", PhysicalRouteKey{}, false
	}
	return physicalRouteID(registered), registered, true
}

func (c PhysicalRouteCatalog) ResolveID(id PhysicalRouteID) (PhysicalRouteKey, bool) {
	key, ok := c.byID[id]
	return key, ok
}

func (c PhysicalRouteCatalog) Match(
	method string,
	target *url.URL,
	protocol WireProtocol,
) (PhysicalRouteID, PhysicalRouteKey, bool) {
	if target == nil || !protocol.Valid() {
		return "", PhysicalRouteKey{}, false
	}
	method = strings.ToUpper(strings.TrimSpace(method))
	host := normalizeRouteHost(target.Hostname())
	path := target.EscapedPath()
	if path == "" {
		path = "/"
	}
	ids := make([]string, 0, len(c.byID))
	for id := range c.byID {
		ids = append(ids, string(id))
	}
	sort.Strings(ids)
	for _, rawID := range ids {
		id := PhysicalRouteID(rawID)
		key := c.byID[id]
		if key.Method == method && key.Protocol == protocol &&
			matchRouteHost(key.Host, host) && matchRoutePath(key.Path, path) {
			return id, key, true
		}
	}
	return "", PhysicalRouteKey{}, false
}

func (c PhysicalRouteCatalog) Routes() []struct {
	ID  PhysicalRouteID
	Key PhysicalRouteKey
} {
	ids := make([]string, 0, len(c.byID))
	for id := range c.byID {
		ids = append(ids, string(id))
	}
	sort.Strings(ids)
	out := make([]struct {
		ID  PhysicalRouteID
		Key PhysicalRouteKey
	}, 0, len(ids))
	for _, rawID := range ids {
		id := PhysicalRouteID(rawID)
		out = append(out, struct {
			ID  PhysicalRouteID
			Key PhysicalRouteKey
		}{ID: id, Key: c.byID[id]})
	}
	return out
}
