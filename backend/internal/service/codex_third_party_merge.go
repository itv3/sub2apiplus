package service

// codex_third_party_merge.go 是目录正文的原始 JSON 合并工具：
//
//   - 顶层字段按原顺序和原始字节保留，不经结构体往返，未知字段不丢；
//   - models 数组的每个条目保留为原始字节，原生条目顺序与内容逐字节不变；
//   - 只有被替换、移除或追加的条目发生变化。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// codexCatalogEnvelope 是解析后的目录信封：保留顶层键顺序与原始值。
type codexCatalogEnvelope struct {
	keys   []string
	values map[string]json.RawMessage
}

// codexCatalogEntry 是 models 数组里的一个原始条目。
type codexCatalogEntry struct {
	raw  json.RawMessage
	slug string
}

// parseCodexCatalogBody 解析目录正文，返回信封与 models 条目。
func parseCodexCatalogBody(body []byte) (*codexCatalogEnvelope, []codexCatalogEntry, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	token, err := dec.Token()
	if err != nil {
		return nil, nil, err
	}
	if delim, ok := token.(json.Delim); !ok || delim != '{' {
		return nil, nil, fmt.Errorf("catalog body is not a JSON object")
	}
	env := &codexCatalogEnvelope{values: map[string]json.RawMessage{}}
	for dec.More() {
		keyToken, err := dec.Token()
		if err != nil {
			return nil, nil, err
		}
		key, ok := keyToken.(string)
		if !ok {
			return nil, nil, fmt.Errorf("catalog body has a non-string key")
		}
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			return nil, nil, err
		}
		if _, dup := env.values[key]; !dup {
			env.keys = append(env.keys, key)
		}
		env.values[key] = raw
	}
	if _, err := dec.Token(); err != nil {
		return nil, nil, err
	}
	rawModels, ok := env.values["models"]
	if !ok {
		return nil, nil, fmt.Errorf("catalog body has no models array")
	}
	var rawEntries []json.RawMessage
	if err := json.Unmarshal(rawModels, &rawEntries); err != nil {
		return nil, nil, fmt.Errorf("decode models array: %w", err)
	}
	entries := make([]codexCatalogEntry, 0, len(rawEntries))
	for _, raw := range rawEntries {
		var probe struct {
			Slug string `json:"slug"`
		}
		_ = json.Unmarshal(raw, &probe)
		entries = append(entries, codexCatalogEntry{raw: raw, slug: strings.TrimSpace(probe.Slug)})
	}
	return env, entries, nil
}

// emptyCodexCatalogEnvelope 返回只含空 models 的信封（没有 OpenAI 基础目录时使用）。
func emptyCodexCatalogEnvelope() *codexCatalogEnvelope {
	return &codexCatalogEnvelope{keys: []string{"models"}, values: map[string]json.RawMessage{"models": json.RawMessage("[]")}}
}

// render 用给定条目重新拼出正文：顶层其它字段保持原始字节与顺序。
func (e *codexCatalogEnvelope) render(entries []json.RawMessage) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, key := range e.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		buf.Write(encodedKey)
		buf.WriteByte(':')
		if key == "models" {
			buf.WriteByte('[')
			for j, entry := range entries {
				if j > 0 {
					buf.WriteByte(',')
				}
				buf.Write(entry)
			}
			buf.WriteByte(']')
			continue
		}
		buf.Write(e.values[key])
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

// mergeCodexCatalogEntries 按规则合并：
//   - keep 为 true 的基础条目原样保留并保持顺序；
//   - keep 为 false 的基础条目：若 published 里有同 slug 的新条目，就在原位置替换，否则移除；
//   - 其余 published 条目按 slug 稳定排序后追加到末尾。
//
// published 的 key 是 slug，value 是整对象字节；appendOrder 是已排序的待发布 slug 列表。
func mergeCodexCatalogEntries(base []codexCatalogEntry, keep func(codexCatalogEntry) bool, published map[string][]byte, appendOrder []string) []json.RawMessage {
	out := make([]json.RawMessage, 0, len(base)+len(appendOrder))
	placed := map[string]struct{}{}
	for _, entry := range base {
		if keep(entry) {
			out = append(out, entry.raw)
			continue
		}
		if replacement, ok := published[entry.slug]; ok && entry.slug != "" {
			if _, already := placed[entry.slug]; !already {
				out = append(out, replacement)
				placed[entry.slug] = struct{}{}
			}
		}
	}
	for _, slug := range appendOrder {
		if _, already := placed[slug]; already {
			continue
		}
		out = append(out, published[slug])
		placed[slug] = struct{}{}
	}
	return out
}
