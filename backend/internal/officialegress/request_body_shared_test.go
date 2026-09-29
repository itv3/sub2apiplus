package officialegress

import (
	"bytes"
	"io"
	"math/rand"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"unsafe"
)

// 本文件锁定“全链共享同一份不可变正文字节”变更集（问题四 M1 第一项）：
// NewSharedReplayableRequestBody 不复制入参、PrepareOfficialCodexAttemptRequestBody 复用
// 句柄字节，二者与复制入口在读取结果、抽取字段、document 状态与错误文本上逐项一致；
// encodeNames 输出与改造前逐字节一致且一次分配到位。

// legacyEncodeNames 逐字保留改造前按 source 长度预留容量、写满后扩容的实现，只作差分基准。
func legacyEncodeNames(d *orderedJSONDocument, names []string) []byte {
	capacity := 2
	if d != nil {
		capacity += len(d.source)
	}
	out := bytes.NewBuffer(make([]byte, 0, capacity))
	_ = out.WriteByte('{')
	written := 0
	for _, name := range names {
		value, present := d.value(name)
		if !present {
			continue
		}
		if written > 0 {
			_ = out.WriteByte(',')
		}
		quotedName := strconv.AppendQuote(nil, name)
		_, _ = out.Write(quotedName)
		_ = out.WriteByte(':')
		_, _ = out.Write(value)
		written++
	}
	_ = out.WriteByte('}')
	return out.Bytes()
}

func sharedBodyMeasureAllocated(run func()) uint64 {
	best := ^uint64(0)
	for attempt := 0; attempt < 3; attempt++ {
		var before, after runtime.MemStats
		runtime.GC()
		runtime.ReadMemStats(&before)
		run()
		runtime.ReadMemStats(&after)
		if allocated := after.TotalAlloc - before.TotalAlloc; allocated < best {
			best = allocated
		}
	}
	return best
}

// sharedBodyLargeCodexBody 构造带 compiler-owned 字段的大正文：client_metadata 与
// prompt_cache_key 会进入删除 overlay，input 中的大段文本只应被引用、不应被复制。
func sharedBodyLargeCodexBody(items int, textBytes int) []byte {
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.6-luna","stream":true,"prompt_cache_key":"k",`)
	_, _ = b.WriteString(`"client_metadata":{"session_id":"s","turn_id":"t"},"input":[`)
	for i := 0; i < items; i++ {
		if i > 0 {
			_ = b.WriteByte(',')
		}
		_, _ = b.WriteString(`{"type":"reasoning","summary":[],"encrypted_content":"`)
		_, _ = b.WriteString(strings.Repeat("QUJD", textBytes/4))
		_, _ = b.WriteString(`"}`)
	}
	_, _ = b.WriteString(`],"store":false}`)
	return []byte(b.String())
}

func TestNewSharedReplayableRequestBodySharesBackingWithoutCopy(t *testing.T) {
	backing := make([]byte, 0, 64)
	backing = append(backing, `{"model":"gpt-5"}`...)
	body := NewSharedReplayableRequestBody(backing)
	view, ok := body.replayableView()
	if !ok || len(view) != len(backing) || unsafe.SliceData(view) != unsafe.SliceData(backing) {
		t.Fatalf("共享 Body 必须直接引用调用方字节，不得复制")
	}
	if cap(view) != len(view) {
		t.Fatalf("共享 Body 的容量必须截到长度，避免包内追加写入调用方空闲区间：cap=%d len=%d", cap(view), len(view))
	}
	if body.ContentLength() != int64(len(backing)) || body.Mode() != RequestBodyReplayable {
		t.Fatalf("共享 Body 的长度或模式不正确")
	}
	copied, ok := body.ReplayableBytes()
	if !ok || !bytes.Equal(copied, backing) || unsafe.SliceData(copied) == unsafe.SliceData(backing) {
		t.Fatalf("公开读取仍必须返回独立副本")
	}
	// 包内对视图的追加必须重新分配，不能写进调用方 backing 的空闲区间。
	extended := append(view, 'X')
	if unsafe.SliceData(extended) == unsafe.SliceData(backing) {
		t.Fatalf("对共享视图的追加写入了调用方 backing")
	}

	empty := NewSharedReplayableRequestBody(nil)
	reference := NewReplayableRequestBody(nil)
	emptyBytes, emptyOK := empty.ReplayableBytes()
	referenceBytes, referenceOK := reference.ReplayableBytes()
	if emptyOK != referenceOK || !bytes.Equal(emptyBytes, referenceBytes) ||
		empty.ContentLength() != reference.ContentLength() || empty.Mode() != reference.Mode() {
		t.Fatalf("空正文的共享入口必须与复制入口一致")
	}
}

func TestNewSharedReplayableRequestBodyDoesNotCopyLargeBody(t *testing.T) {
	source := sharedBodyLargeCodexBody(64, 128*1024)
	var shared, copied RequestBody
	sharedAllocated := sharedBodyMeasureAllocated(func() { shared = NewSharedReplayableRequestBody(source) })
	copiedAllocated := sharedBodyMeasureAllocated(func() { copied = NewReplayableRequestBody(source) })
	t.Logf("正文 %.1f MiB：共享入口 %d 字节，复制入口 %.1f MiB",
		float64(len(source))/(1<<20), sharedAllocated, float64(copiedAllocated)/(1<<20))
	if shared.ContentLength() != copied.ContentLength() {
		t.Fatalf("两个入口的长度必须一致")
	}
	if sharedAllocated > 4*1024 {
		t.Fatalf("共享入口分配 %d 字节，不得再复制整段正文（%d 字节）", sharedAllocated, len(source))
	}
	if copiedAllocated < uint64(len(source)) {
		t.Fatalf("对照组的复制入口应至少分配一份正文：%d < %d", copiedAllocated, len(source))
	}
}

// sharedBodyRandomCodexDocument 生成带 compiler-owned 字段与各类边界值的随机顶层对象。
func sharedBodyRandomCodexDocument(rng *rand.Rand) []byte {
	candidates := []string{
		`"model":"gpt-5.6-luna"`, `"model":""`, `"model":7`,
		`"service_tier":"priority"`, `"service_tier":null`, `"service_tier":1`,
		`"prompt_cache_key":"k"`, `"prompt_cache_key":null`,
		`"client_metadata":{"session_id":" s ","turn_id":"t","n":1}`, `"client_metadata":[1]`,
		`"client_metadata":{"a":"b","a":"c"}`,
		`"input":[{"type":"message","content":[{"type":"input_text","text":"x","prompt_cache_breakpoint":{"ttl":"5m"}}]}]`,
		`"input":[{"type":"message","content":"prompt_cache_breakpoint"}]`,
		`"input":"plain"`, `"input":[{"prompt_cache_breakpoint":1},2,"s",null]`,
		`"type":"response.create"`, `"type":""`, `"type":1`,
		`"previous_response_id":"resp_1"`, `"previous_response_id":null`,
		`"refresh_token":"rt"`, `"refresh_token":""`, `"refresh_token":1`,
		`"codex_connector_id":"c"`, `"codex_action_name":"a"`, `"codex_model":"m"`, `"codex_model":""`,
		`"tool_choice":"auto"`, `"store":false`, `"stream":true`, `"stream_options":{}`,
		`"include":["reasoning.encrypted_content"]`, `"text":{"verbosity":"low"}`,
		`"kéy":"escaped"`, "\"bad\":\"\xff\"",
	}
	count := rng.Intn(9)
	parts := make([]string, 0, count)
	for i := 0; i < count; i++ {
		parts = append(parts, candidates[rng.Intn(len(candidates))])
	}
	space := []string{"", " ", "\n"}
	return []byte("{" + space[rng.Intn(3)] + strings.Join(parts, ","+space[rng.Intn(3)]) + "}")
}

func sharedBodyDocumentState(body RequestBody) string {
	document := body.jsonDocument()
	if document == nil {
		return "<nil>"
	}
	var b strings.Builder
	for _, field := range document.fields {
		_, _ = b.WriteString(field.name + "=" + string(field.value) + ";")
	}
	_, _ = b.WriteString("|order=" + strings.Join(document.overlayOrder, ","))
	for _, name := range document.overlayOrder {
		overlay := document.overlay[name]
		_, _ = b.WriteString("|" + name + "=" + string(overlay.value) + ":" + strconv.FormatBool(overlay.omitted))
	}
	_, _ = b.WriteString("|checked=" + strconv.FormatBool(document.duplicatesChecked))
	return b.String()
}

func sharedBodyComparePrepared(
	t *testing.T,
	name string,
	legacyBody RequestBody, legacyFields CompilerOwnedBodyFields, legacyErr error,
	currentBody RequestBody, currentFields CompilerOwnedBodyFields, currentErr error,
) {
	t.Helper()
	if (legacyErr == nil) != (currentErr == nil) {
		t.Fatalf("%s：错误有无不一致 legacy=%v current=%v", name, legacyErr, currentErr)
	}
	if legacyErr != nil {
		if legacyErr.Error() != currentErr.Error() {
			t.Fatalf("%s：错误文本不一致\nlegacy=%v\ncurrent=%v", name, legacyErr, currentErr)
		}
		return
	}
	if !reflect.DeepEqual(legacyFields, currentFields) {
		t.Fatalf("%s：compiler-owned 字段不一致\nlegacy=%#v\ncurrent=%#v", name, legacyFields, currentFields)
	}
	legacyBytes, legacyOK := legacyBody.ReplayableBytes()
	currentBytes, currentOK := currentBody.ReplayableBytes()
	if legacyOK != currentOK || !bytes.Equal(legacyBytes, currentBytes) {
		t.Fatalf("%s：ReplayableBytes 不一致\nlegacy=%q\ncurrent=%q", name, legacyBytes, currentBytes)
	}
	if legacyBody.ContentLength() != currentBody.ContentLength() || legacyBody.Mode() != currentBody.Mode() {
		t.Fatalf("%s：长度或模式不一致", name)
	}
	if sharedBodyDocumentState(legacyBody) != sharedBodyDocumentState(currentBody) {
		t.Fatalf("%s：document 状态不一致\nlegacy=%s\ncurrent=%s", name,
			sharedBodyDocumentState(legacyBody), sharedBodyDocumentState(currentBody))
	}
}

func TestPrepareOfficialCodexAttemptRequestBodyMatchesBytesVariant(t *testing.T) {
	endpoints := []string{
		"responses_http", "responses_compact", "responses_ws", "oauth_refresh", "files_create", "models",
	}
	bodies := map[string][]byte{
		"empty":        nil,
		"blank":        []byte(" \n "),
		"object_empty": []byte(`{}`),
		"large":        sharedBodyLargeCodexBody(4, 1024),
		"top_array":    []byte(`[1]`),
		"truncated":    []byte(`{"model":`),
		"duplicate":    []byte(`{"model":"a","model":"b"}`),
		"trailing":     []byte(`{"a":1} x`),
	}
	for i, fixture := range scanFixtures {
		bodies["scan_fixture_"+strconv.Itoa(i)] = []byte(fixture)
	}
	rng := rand.New(rand.NewSource(20260929))
	for round := 0; round < 400; round++ {
		document := sharedBodyRandomCodexDocument(rng)
		bodies["random_"+strconv.Itoa(round)] = document
		bodies["mutated_"+strconv.Itoa(round)] = scanMutate(rng, document)
	}
	for bodyName, body := range bodies {
		for _, endpointID := range endpoints {
			name := bodyName + "@" + endpointID
			legacyBody, legacyFields, legacyErr := PrepareOfficialCodexAttemptBody(endpointID, body)
			currentBody, currentFields, currentErr := PrepareOfficialCodexAttemptRequestBody(
				endpointID, NewSharedReplayableRequestBody(body),
			)
			sharedBodyComparePrepared(t, name, legacyBody, legacyFields, legacyErr, currentBody, currentFields, currentErr)
			if legacyErr != nil {
				continue
			}
			// 已带 overlay 的句柄按其定型字节重新准备，必须与字节入口一致。
			replayed, _ := legacyBody.ReplayableBytes()
			againLegacy, againLegacyFields, againLegacyErr := PrepareOfficialCodexAttemptBody(endpointID, replayed)
			againCurrent, againCurrentFields, againCurrentErr := PrepareOfficialCodexAttemptRequestBody(endpointID, legacyBody)
			sharedBodyComparePrepared(t, name+"#document", againLegacy, againLegacyFields, againLegacyErr,
				againCurrent, againCurrentFields, againCurrentErr)
		}
	}
	singleUse, err := NewSingleUseRequestBody(io.NopCloser(bytes.NewReader([]byte(`{}`))), 2)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := PrepareOfficialCodexAttemptRequestBody("responses_http", singleUse); err == nil {
		t.Fatalf("single-use Body 不得进入 replayable 语义准备")
	}
}

func TestPrepareOfficialCodexAttemptRequestBodyDoesNotCopyLargeBody(t *testing.T) {
	source := sharedBodyLargeCodexBody(64, 128*1024)
	shared := NewSharedReplayableRequestBody(source)
	var prepared RequestBody
	sharedAllocated := sharedBodyMeasureAllocated(func() {
		var err error
		prepared, _, err = PrepareOfficialCodexAttemptRequestBody("responses_http", shared)
		if err != nil {
			t.Fatal(err)
		}
	})
	copiedAllocated := sharedBodyMeasureAllocated(func() {
		if _, _, err := PrepareOfficialCodexAttemptBody("responses_http", source); err != nil {
			t.Fatal(err)
		}
	})
	t.Logf("正文 %.1f MiB：共享准备 %.1f KiB，复制准备 %.1f MiB",
		float64(len(source))/(1<<20), float64(sharedAllocated)/1024, float64(copiedAllocated)/(1<<20))
	view, _ := prepared.replayableView()
	if unsafe.SliceData(view) != unsafe.SliceData(source) {
		t.Fatalf("共享准备必须直接引用原正文")
	}
	if sharedAllocated > uint64(len(source))/64 {
		t.Fatalf("共享准备分配 %d 字节，不得再按正文体积（%d 字节）复制", sharedAllocated, len(source))
	}
	if copiedAllocated < uint64(len(source)) {
		t.Fatalf("对照组的复制准备应至少分配一份正文：%d < %d", copiedAllocated, len(source))
	}
}

func TestEncodeNamesMatchesLegacyWithExactCapacity(t *testing.T) {
	rng := rand.New(rand.NewSource(20260930))
	nameUniverse := []string{"model", "input", "a", "b", "key", "中", "\xff", "", "q\"uote", "missing"}
	for round := 0; round < 3000; round++ {
		source := []byte(scanRandomJSON(rng, 0))
		document, err := newOrderedJSONDocument(source)
		if err != nil {
			document = &orderedJSONDocument{duplicatesChecked: true}
		}
		for n := rng.Intn(4); n > 0; n-- {
			name := nameUniverse[rng.Intn(len(nameUniverse))]
			if rng.Intn(2) == 0 {
				document.omit(name)
			} else {
				document.set(name, []byte(scanRandomJSON(rng, 3)))
			}
		}
		var names []string
		if rng.Intn(2) == 0 {
			names = document.namesInSourceOrder()
		} else {
			for n := rng.Intn(6); n > 0; n-- {
				names = append(names, nameUniverse[rng.Intn(len(nameUniverse))])
			}
		}
		legacy := legacyEncodeNames(document, names)
		current := document.encodeNames(names)
		if !bytes.Equal(legacy, current) {
			t.Fatalf("round %d：encodeNames 输出与改造前不一致\nlegacy=%q\ncurrent=%q", round, legacy, current)
		}
		if cap(current) != len(current) {
			t.Fatalf("round %d：encodeNames 必须一次分配到精确长度 cap=%d len=%d", round, cap(current), len(current))
		}
	}
	var nilDocument *orderedJSONDocument
	if got := nilDocument.encodeNames([]string{"a"}); string(got) != "{}" || string(legacyEncodeNames(nilDocument, []string{"a"})) != "{}" {
		t.Fatalf("nil document 必须写出空对象：%q", got)
	}
}

func TestEncodeNamesDoesNotAmplifyLargeBody(t *testing.T) {
	source := sharedBodyLargeCodexBody(64, 128*1024)
	document, err := newOrderedJSONDocument(source)
	if err != nil {
		t.Fatal(err)
	}
	// 模拟编译器：删除调用方携带的 compiler-owned 字段后注入编译器自己的值，定型结果
	// 比 source 更长，改造前会在写入末尾时触发整段扩容。
	document.omit("client_metadata")
	document.omit("prompt_cache_key")
	document.set("prompt_cache_key", []byte(`"019f9577-d69f-7892-809e-8a3a4198c671"`))
	document.set("client_metadata", []byte(`{"session_id":"019f9577-d69f-7892-809e-8a3a4198c671","turn_id":"019f9577-d70a-7553-ad23-8de3ede39d8b","x-codex-installation-id":"bee3cd38-4511-4497-899e-f19f04f953fd","x-codex-window-id":"019f9577-d69f-7892-809e-8a3a4198c671:0"}`))
	names := []string{"model", "input", "prompt_cache_key", "stream", "store", "client_metadata"}
	var current []byte
	currentAllocated := sharedBodyMeasureAllocated(func() { current = document.encodeNames(names) })
	legacyAllocated := sharedBodyMeasureAllocated(func() { _ = legacyEncodeNames(document, names) })
	t.Logf("定型正文 %.1f MiB：精确分配 %.1f MiB，改造前 %.1f MiB",
		float64(len(current))/(1<<20), float64(currentAllocated)/(1<<20), float64(legacyAllocated)/(1<<20))
	if !bytes.Equal(current, legacyEncodeNames(document, names)) {
		t.Fatalf("大正文 encodeNames 输出与改造前不一致")
	}
	if currentAllocated > uint64(len(current))+64*1024 {
		t.Fatalf("encodeNames 分配 %d 字节，应只分配一份定型正文（%d 字节）", currentAllocated, len(current))
	}
	if legacyAllocated < 2*uint64(len(current)) {
		t.Fatalf("对照组应复现改造前的扩容复制：%d < 2×%d", legacyAllocated, len(current))
	}
}
