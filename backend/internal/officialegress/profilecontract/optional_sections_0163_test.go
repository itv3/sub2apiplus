package profilecontract

import (
	"encoding/json"
	"strings"
	"testing"
)

// 基础指令载体与 access_programs 两个可选节（BaseInstructions、AccessPrograms）：
//  1. 合法取值能严格解码并进入执行投影；
//  2. 取值闭集、排序去重、显式 null 与空串逐项失败关闭；
//  3. 与端点 Body.Fields 的交叉校验：声明节却仍发顶层 instructions、声明字段却没有值域等自相矛盾一律拒绝，
//     节缺省的画像不被施加反向要求；
//  4. 两节进入官方摘要、ProfileDigest 与 executable 摘要，字段序固定；
//  5. 节缺省的历史快照各类摘要逐字节不变，见 optional_sections_legacy_baseline_test.go。

const (
	baseInstructionsSectionJSON = `{"Carrier":"input_developer_message","ContentItemKind":"model.base_instructions","EndpointIDs":["responses_http","responses_ws"],"IDPrefix":"msg"}`
	accessProgramsSectionJSON   = `{"AllowedCyberPrograms":["standard"]}`
)

// withEndpointBodyFields 返回端点切片的拷贝，并用 mutate 改写指定端点的 Body.Fields；入参不被修改。
func withEndpointBodyFields(endpoints []SnapshotEndpoint, endpointID string, mutate func([]SnapshotBodyField) []SnapshotBodyField) []SnapshotEndpoint {
	out := make([]SnapshotEndpoint, len(endpoints))
	copy(out, endpoints)
	for index := range out {
		if out[index].ID == endpointID {
			out[index].Body.Fields = mutate(append([]SnapshotBodyField(nil), endpoints[index].Body.Fields...))
		}
	}
	return out
}

// bodyFieldsWithout 返回去掉指定字段后的新切片。
func bodyFieldsWithout(fields []SnapshotBodyField, name string) []SnapshotBodyField {
	out := make([]SnapshotBodyField, 0, len(fields))
	for _, field := range fields {
		if field.Name != name {
			out = append(out, field)
		}
	}
	return out
}

// bodyFieldsWithInstructions 把顶层 instructions 加回字段表首位，模拟较早版本的字段形态。
func bodyFieldsWithInstructions(fields []SnapshotBodyField) []SnapshotBodyField {
	return append([]SnapshotBodyField{{Name: "instructions", OmitWhen: "empty_string"}}, fields...)
}

// endpointsWithNewerBodyContract 把两个 Responses 端点的 Body.Fields 改成较新版本的形态：
// 去掉顶层 instructions，在末尾追加 access_programs。入参不被修改，其余端点与字段原样保留。
func endpointsWithNewerBodyContract(endpoints []SnapshotEndpoint) []SnapshotEndpoint {
	newer := func(fields []SnapshotBodyField) []SnapshotBodyField {
		return append(bodyFieldsWithout(fields, "instructions"), SnapshotBodyField{Name: "access_programs", OmitWhen: "none"})
	}
	out := withEndpointBodyFields(endpoints, "responses_http", newer)
	return withEndpointBodyFields(out, "responses_ws", newer)
}

// mutatedSectionJSON 以合法节为底稿，只改动其中一个键，使每个负例只因目标原因被拒绝。
func mutatedSectionJSON(t *testing.T, valid string, mutate func(fields map[string]json.RawMessage)) json.RawMessage {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(valid), &fields); err != nil {
		t.Fatal(err)
	}
	mutate(fields)
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestBaseInstructionsAndAccessProgramsSectionsDecodeAndProject(t *testing.T) {
	decoded, err := DecodeOptionalSection(SectionBaseInstructions, json.RawMessage(baseInstructionsSectionJSON))
	if err != nil {
		t.Fatalf("合法的 BaseInstructions 节被拒绝：%v", err)
	}
	instructions, ok := decoded.(*BaseInstructionsSection)
	if !ok || instructions.Carrier != "input_developer_message" || instructions.ContentItemKind != "model.base_instructions" ||
		strings.Join(instructions.EndpointIDs, ",") != "responses_http,responses_ws" || instructions.IDPrefix != "msg" {
		t.Fatalf("BaseInstructions 解码结果错误：%#v", decoded)
	}
	// 单端点也是合法声明：节只约束它列出的端点。
	if _, err := DecodeOptionalSection(SectionBaseInstructions, mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
		fields["EndpointIDs"] = json.RawMessage(`["responses_ws"]`)
	})); err != nil {
		t.Fatalf("单端点的 BaseInstructions 节被拒绝：%v", err)
	}

	decoded, err = DecodeOptionalSection(SectionAccessPrograms, json.RawMessage(accessProgramsSectionJSON))
	if err != nil {
		t.Fatalf("合法的 AccessPrograms 节被拒绝：%v", err)
	}
	programs, ok := decoded.(*AccessProgramsSection)
	if !ok || strings.Join(programs.AllowedCyberPrograms, ",") != "standard" || programs.DefaultCyberProgram != "" {
		t.Fatalf("AccessPrograms 解码结果错误：%#v", decoded)
	}
	// 预留的默认值：存在时属于允许集合即合法。
	decoded, err = DecodeOptionalSection(SectionAccessPrograms, json.RawMessage(`{"AllowedCyberPrograms":["daybreak_blue","daybreak_red","standard"],"DefaultCyberProgram":"standard"}`))
	if err != nil {
		t.Fatalf("带默认值的 AccessPrograms 节被拒绝：%v", err)
	}
	programs, ok = decoded.(*AccessProgramsSection)
	if !ok || strings.Join(programs.AllowedCyberPrograms, ",") != "daybreak_blue,daybreak_red,standard" || programs.DefaultCyberProgram != "standard" {
		t.Fatalf("带默认值的 AccessPrograms 解码结果错误：%#v", decoded)
	}

	_, extended := withAllOptionalSections(t)
	_, executable, err := compileExecutableDoc(t, extended)
	if err != nil {
		t.Fatalf("含新节且端点字段自洽的画像编译失败：%v", err)
	}
	optional := executable.Optional()
	if optional.BaseInstructions == nil || optional.AccessPrograms == nil {
		t.Fatalf("新节必须进入执行投影：%#v", optional)
	}
	if optional.BaseInstructions.IDPrefix != "msg" || optional.BaseInstructions.Carrier != "input_developer_message" ||
		strings.Join(optional.AccessPrograms.AllowedCyberPrograms, ",") != "standard" {
		t.Fatalf("新节执行投影取值与快照原文不一致：%#v %#v", optional.BaseInstructions, optional.AccessPrograms)
	}
}

func TestBaseInstructionsSectionStrictDecoding(t *testing.T) {
	setField := func(key, raw string) func(map[string]json.RawMessage) {
		return func(fields map[string]json.RawMessage) { fields[key] = json.RawMessage(raw) }
	}
	dropField := func(key string) func(map[string]json.RawMessage) {
		return func(fields map[string]json.RawMessage) { delete(fields, key) }
	}
	cases := []struct {
		label   string
		mutate  func(map[string]json.RawMessage)
		wantErr string
	}{
		{"Carrier 缺省", dropField("Carrier"), "Carrier 只支持 input_developer_message"},
		{"Carrier 非法取值", setField("Carrier", `"top_level_instructions"`), "Carrier 只支持 input_developer_message"},
		{"Carrier 大小写不符", setField("Carrier", `"Input_Developer_Message"`), "Carrier 只支持 input_developer_message"},
		{"ContentItemKind 缺省", dropField("ContentItemKind"), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 没有点", setField("ContentItemKind", `"modelbaseinstructions"`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 含大写", setField("ContentItemKind", `"Model.base_instructions"`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 前导点", setField("ContentItemKind", `".model.base_instructions"`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 尾随点", setField("ContentItemKind", `"model.base_instructions."`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 连续点", setField("ContentItemKind", `"model..base_instructions"`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 含数字", setField("ContentItemKind", `"model.base2"`), "ContentItemKind 必须是点分小写标识"},
		{"ContentItemKind 含空白", setField("ContentItemKind", `"model. base_instructions"`), "ContentItemKind 必须是点分小写标识"},
		{"EndpointIDs 缺省", dropField("EndpointIDs"), "EndpointIDs 不能为空"},
		{"EndpointIDs 为空", setField("EndpointIDs", `[]`), "EndpointIDs 不能为空"},
		{"EndpointIDs 未排序", setField("EndpointIDs", `["responses_ws","responses_http"]`), "EndpointIDs 必须排序"},
		{"EndpointIDs 重复", setField("EndpointIDs", `["responses_http","responses_http"]`), "EndpointIDs 含重复取值"},
		{"EndpointIDs 含空串", setField("EndpointIDs", `[""]`), "EndpointIDs 含非法取值"},
		{"EndpointIDs 含大写", setField("EndpointIDs", `["Responses_HTTP"]`), "EndpointIDs 含非法取值"},
		{"EndpointIDs 含未知端点", setField("EndpointIDs", `["responses_http","responses_zzz"]`), "EndpointIDs 含不支持的端点"},
		{"EndpointIDs 只含闭集之外的端点", setField("EndpointIDs", `["responses_compact"]`), "EndpointIDs 含不支持的端点"},
		{"EndpointIDs 类型错误", setField("EndpointIDs", `"responses_http"`), "解析可选节"},
		{"IDPrefix 缺省", dropField("IDPrefix"), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 过短", setField("IDPrefix", `"m"`), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 过长", setField("IDPrefix", `"abcdefghi"`), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 含大写", setField("IDPrefix", `"MSG"`), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 含数字", setField("IDPrefix", `"ms1"`), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 含下划线", setField("IDPrefix", `"m_g"`), "IDPrefix 必须是 2 到 8 位小写字母"},
		{"IDPrefix 类型错误", setField("IDPrefix", `1`), "解析可选节"},
		{"未知字段", setField("Extra", `true`), "解析可选节"},
		{"Carrier 显式 null", setField("Carrier", `null`), "不得显式为 null"},
		{"ContentItemKind 显式 null", setField("ContentItemKind", `null`), "不得显式为 null"},
		{"EndpointIDs 显式 null", setField("EndpointIDs", `null`), "不得显式为 null"},
		{"IDPrefix 显式 null", setField("IDPrefix", `null`), "不得显式为 null"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			raw := mutatedSectionJSON(t, baseInstructionsSectionJSON, item.mutate)
			_, err := DecodeOptionalSection(SectionBaseInstructions, raw)
			if err == nil {
				t.Fatalf("应被拒绝：%s", raw)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
	// 整节为 null 与尾随数据不经过字段变异，单独验证。
	if _, err := DecodeOptionalSection(SectionBaseInstructions, json.RawMessage(`null`)); err == nil {
		t.Fatal("整节显式 null 应被拒绝")
	}
	if _, err := DecodeOptionalSection(SectionBaseInstructions, json.RawMessage(baseInstructionsSectionJSON+`{}`)); err == nil {
		t.Fatal("尾随数据应被拒绝")
	}
}

func TestAccessProgramsSectionStrictDecoding(t *testing.T) {
	setField := func(key, raw string) func(map[string]json.RawMessage) {
		return func(fields map[string]json.RawMessage) { fields[key] = json.RawMessage(raw) }
	}
	cases := []struct {
		label   string
		mutate  func(map[string]json.RawMessage)
		wantErr string
	}{
		{"AllowedCyberPrograms 缺省", func(fields map[string]json.RawMessage) { delete(fields, "AllowedCyberPrograms") }, "AllowedCyberPrograms 不能为空"},
		{"AllowedCyberPrograms 为空", setField("AllowedCyberPrograms", `[]`), "AllowedCyberPrograms 不能为空"},
		{"AllowedCyberPrograms 未排序", setField("AllowedCyberPrograms", `["standard","daybreak_blue"]`), "AllowedCyberPrograms 必须排序"},
		{"AllowedCyberPrograms 重复", setField("AllowedCyberPrograms", `["standard","standard"]`), "AllowedCyberPrograms 含重复取值"},
		{"AllowedCyberPrograms 含未知程序", setField("AllowedCyberPrograms", `["standard","unlimited"]`), "AllowedCyberPrograms 含未知程序"},
		{"AllowedCyberPrograms 含大写", setField("AllowedCyberPrograms", `["Standard"]`), "AllowedCyberPrograms 含非法取值"},
		{"AllowedCyberPrograms 含空串", setField("AllowedCyberPrograms", `[""]`), "AllowedCyberPrograms 含非法取值"},
		{"AllowedCyberPrograms 类型错误", setField("AllowedCyberPrograms", `"standard"`), "解析可选节"},
		{"AllowedCyberPrograms 显式 null", setField("AllowedCyberPrograms", `null`), "不得显式为 null"},
		{"DefaultCyberProgram 不属于允许集合", setField("DefaultCyberProgram", `"daybreak_blue"`), "DefaultCyberProgram 必须属于 AllowedCyberPrograms"},
		{"DefaultCyberProgram 取值未知", setField("DefaultCyberProgram", `"unlimited"`), "DefaultCyberProgram 必须属于 AllowedCyberPrograms"},
		{"DefaultCyberProgram 显式空串", setField("DefaultCyberProgram", `""`), "DefaultCyberProgram 存在时不得为空串"},
		{"DefaultCyberProgram 显式 null", setField("DefaultCyberProgram", `null`), "不得显式为 null"},
		{"DefaultCyberProgram 类型错误", setField("DefaultCyberProgram", `1`), "解析可选节"},
		{"未知字段", setField("Extra", `true`), "解析可选节"},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			raw := mutatedSectionJSON(t, accessProgramsSectionJSON, item.mutate)
			_, err := DecodeOptionalSection(SectionAccessPrograms, raw)
			if err == nil {
				t.Fatalf("应被拒绝：%s", raw)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
	if _, err := DecodeOptionalSection(SectionAccessPrograms, json.RawMessage(`null`)); err == nil {
		t.Fatal("整节显式 null 应被拒绝")
	}
	if _, err := DecodeOptionalSection(SectionAccessPrograms, json.RawMessage(accessProgramsSectionJSON+`{}`)); err == nil {
		t.Fatal("尾随数据应被拒绝")
	}
}

// 快照层（PrepareSnapshotForManifest）与编译层（CompileExecutableProfile）都必须拒绝非法的新节。
func TestNewerSectionsAreRejectedInSnapshotAndCompileStages(t *testing.T) {
	invalid := map[string]func(t *testing.T, doc *SnapshotDoc){
		"基础指令节 Carrier 非法": func(t *testing.T, doc *SnapshotDoc) {
			doc.BaseInstructions = mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
				fields["Carrier"] = json.RawMessage(`"top_level_instructions"`)
			})
		},
		"访问程序节含未知程序": func(t *testing.T, doc *SnapshotDoc) {
			doc.AccessPrograms = json.RawMessage(`{"AllowedCyberPrograms":["unlimited"]}`)
		},
		"基础指令节显式 null": func(t *testing.T, doc *SnapshotDoc) { doc.BaseInstructions = json.RawMessage(`null`) },
		"访问程序节显式 null": func(t *testing.T, doc *SnapshotDoc) { doc.AccessPrograms = json.RawMessage(`null`) },
	}
	for label, mutate := range invalid {
		t.Run(label, func(t *testing.T) {
			_, doc := withAllOptionalSections(t)
			mutate(t, &doc)
			if _, err := PrepareSnapshotForManifest(doc); err == nil {
				t.Fatal("快照规范化阶段应拒绝")
			}
			if _, _, err := compileExecutableDoc(t, doc); err == nil {
				t.Fatal("编译阶段应拒绝")
			}
		})
	}
}

func TestNewerSectionsCrossReferenceEndpointBodyFields(t *testing.T) {
	dropAccessPrograms := func(fields []SnapshotBodyField) []SnapshotBodyField {
		return bodyFieldsWithout(fields, "access_programs")
	}
	dropInput := func(fields []SnapshotBodyField) []SnapshotBodyField { return bodyFieldsWithout(fields, "input") }
	cases := []struct {
		label   string
		mutate  func(t *testing.T, doc *SnapshotDoc)
		wantErr string // 空串表示应当通过
	}{
		{"端点字段与两节自洽", func(t *testing.T, doc *SnapshotDoc) {}, ""},
		{"http 端点仍含 instructions", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_http", bodyFieldsWithInstructions)
		}, "BaseInstructions 与端点 responses_http 的 Body.Fields 矛盾"},
		{"ws 端点仍含 instructions", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_ws", bodyFieldsWithInstructions)
		}, "BaseInstructions 与端点 responses_ws 的 Body.Fields 矛盾"},
		{"http 端点缺 input", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_http", dropInput)
		}, "BaseInstructions 所列端点 responses_http 的 Body.Fields 缺少 input"},
		{"ws 端点缺 input", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_ws", dropInput)
		}, "BaseInstructions 所列端点 responses_ws 的 Body.Fields 缺少 input"},
		{"访问程序节缺省而 http 与 ws 都声明了字段", func(t *testing.T, doc *SnapshotDoc) { doc.AccessPrograms = nil },
			"端点 responses_http 的 Body.Fields 声明了 access_programs，但画像缺少 AccessPrograms 节"},
		{"访问程序节缺省而仅 ws 声明了字段", func(t *testing.T, doc *SnapshotDoc) {
			doc.AccessPrograms = nil
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_http", dropAccessPrograms)
		}, "端点 responses_ws 的 Body.Fields 声明了 access_programs，但画像缺少 AccessPrograms 节"},
		{"访问程序节存在而 http 缺字段", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_http", dropAccessPrograms)
		}, "AccessPrograms 节存在，但端点 responses_http 的 Body.Fields 没有声明 access_programs"},
		{"访问程序节存在而 ws 缺字段", func(t *testing.T, doc *SnapshotDoc) {
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_ws", dropAccessPrograms)
		}, "AccessPrograms 节存在，但端点 responses_ws 的 Body.Fields 没有声明 access_programs"},
		// 反向要求不得施加：节只约束它列出的端点，节缺省的画像 Body.Fields 含不含 instructions 都通过。
		{"基础指令节只列 http 时 ws 可保留 instructions", func(t *testing.T, doc *SnapshotDoc) {
			doc.BaseInstructions = mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
				fields["EndpointIDs"] = json.RawMessage(`["responses_http"]`)
			})
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_ws", bodyFieldsWithInstructions)
		}, ""},
		{"基础指令节缺省时端点可以含 instructions", func(t *testing.T, doc *SnapshotDoc) {
			doc.BaseInstructions = nil
			doc.Endpoints = withEndpointBodyFields(doc.Endpoints, "responses_http", bodyFieldsWithInstructions)
		}, ""},
		{"基础指令节缺省时端点也可以不含 instructions", func(t *testing.T, doc *SnapshotDoc) { doc.BaseInstructions = nil }, ""},
	}
	for _, item := range cases {
		t.Run(item.label, func(t *testing.T) {
			_, doc := withAllOptionalSections(t)
			item.mutate(t, &doc)
			_, _, err := compileExecutableDoc(t, doc)
			if item.wantErr == "" {
				if err != nil {
					t.Fatalf("应当通过，实际：%v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("应被拒绝，期望错误含 %q", item.wantErr)
			}
			if !strings.Contains(err.Error(), item.wantErr) {
				t.Fatalf("错误原因不符：期望含 %q，实际 %v", item.wantErr, err)
			}
		})
	}
}

// 节缺省的画像（含 Body.Fields 里有、没有 instructions 两种形态）不受交叉校验约束。
func TestSectionsAbsentLeaveEndpointBodyFieldsUnconstrained(t *testing.T) {
	base, _ := withAllOptionalSections(t)
	if _, _, err := compileExecutableDoc(t, base); err != nil {
		t.Fatalf("节缺省且含 instructions 的画像应当通过：%v", err)
	}
	withoutInstructions := base
	dropInstructions := func(fields []SnapshotBodyField) []SnapshotBodyField { return bodyFieldsWithout(fields, "instructions") }
	withoutInstructions.Endpoints = withEndpointBodyFields(withEndpointBodyFields(base.Endpoints, "responses_http", dropInstructions), "responses_ws", dropInstructions)
	if _, _, err := compileExecutableDoc(t, withoutInstructions); err != nil {
		t.Fatalf("节缺省且不含 instructions 的画像应当通过：%v", err)
	}
}

// 交叉校验函数的两个直接分支：画像里根本没有节所列的端点。真实画像里这类缺口会先被
// ToolPresentation 等其他引用检查拦下，所以只能对校验函数本身做单元测试。
func TestNewerSectionsCrossReferencesRejectMissingEndpoints(t *testing.T) {
	httpOnly := ExecutableProfile{
		endpoints: []ExecutableEndpointProfile{{
			ID: "responses_http",
			Body: BodyContractProfile{Fields: []BodyFieldProfile{
				{Name: "input"}, {Name: "access_programs"},
			}},
		}},
	}
	endpointIDs := map[string]struct{}{"responses_http": {}}

	baseInstructionsProfile := httpOnly
	baseInstructionsProfile.optional.BaseInstructions = &BaseInstructionsSection{EndpointIDs: []string{"responses_http", "responses_ws"}}
	if err := validateBaseInstructionsCrossReferences(baseInstructionsProfile, endpointIDs); err == nil ||
		!strings.Contains(err.Error(), "BaseInstructions 引用了未知 endpoint: responses_ws") {
		t.Fatalf("基础指令节引用画像之外的端点应被拒绝，实际：%v", err)
	}

	accessProgramsProfile := httpOnly
	accessProgramsProfile.optional.AccessPrograms = &AccessProgramsSection{AllowedCyberPrograms: []string{"standard"}}
	if err := validateAccessProgramsCrossReferences(accessProgramsProfile); err == nil ||
		!strings.Contains(err.Error(), "AccessPrograms 节存在，但画像缺少端点 responses_ws") {
		t.Fatalf("访问程序节要求的端点不在画像里应被拒绝，实际：%v", err)
	}
}

// 两节进入规范化快照：字段序（官方摘要的计算顺序）固定，节内字节规范化，严格解码往返稳定。
func TestNewerSectionsSnapshotRoundTripKeepsFieldOrder(t *testing.T) {
	_, extended := withAllOptionalSections(t)
	prepared, err := PrepareSnapshotForManifest(extended)
	if err != nil {
		t.Fatalf("规范化含新节的快照失败：%v", err)
	}
	encoded, err := json.Marshal(prepared)
	if err != nil {
		t.Fatal(err)
	}
	text := string(encoded)
	// 字段序就是官方摘要的计算顺序：新节紧跟已有可选节之后、Transports 之前。
	order := []string{`"ImageGeneration":`, `"BaseInstructions":`, `"AccessPrograms":`, `"Transports":`}
	previous := -1
	for _, key := range order {
		position := strings.Index(text, key)
		if position < 0 || position <= previous {
			t.Fatalf("规范化快照里 %s 的位置 %d 不在预期顺序（前一个位置 %d）", key, position, previous)
		}
		previous = position
	}
	// 节内字节：键按字典序，等价写法规范化后相同。
	if !strings.Contains(text, `"BaseInstructions":`+baseInstructionsSectionJSON) ||
		!strings.Contains(text, `"AccessPrograms":`+accessProgramsSectionJSON) {
		t.Fatalf("新节的规范化字节与预期不符：%s", text)
	}
	// 严格解码（模拟候选画像在冻结环境里被 ParseSnapshot 读取）往返稳定。
	parsed, err := ParseSnapshot(encoded)
	if err != nil {
		t.Fatalf("含新节的快照被严格解码拒绝：%v", err)
	}
	if string(parsed.BaseInstructions) != baseInstructionsSectionJSON || string(parsed.AccessPrograms) != accessProgramsSectionJSON {
		t.Fatalf("解码后新节字节漂移：%s %s", parsed.BaseInstructions, parsed.AccessPrograms)
	}
	digest, err := OfficialSnapshotDigest(parsed)
	if err != nil || digest != prepared.Digest {
		t.Fatalf("官方摘要往返不稳定：%s != %s (%v)", digest, prepared.Digest, err)
	}
	spec, err := NewProfileSpec(parsed)
	if err != nil {
		t.Fatal(err)
	}
	roundTrip := spec.ToSnapshot()
	if string(roundTrip.BaseInstructions) != baseInstructionsSectionJSON || string(roundTrip.AccessPrograms) != accessProgramsSectionJSON {
		t.Fatalf("ProfileSpec 往返丢失或改写了新节：%s %s", roundTrip.BaseInstructions, roundTrip.AccessPrograms)
	}

	// 键序不同的等价写法，规范化后得到同一份官方摘要。
	_, reordered := withAllOptionalSections(t)
	reordered.BaseInstructions = json.RawMessage(`{"IDPrefix":"msg","EndpointIDs":["responses_http","responses_ws"],"ContentItemKind":"model.base_instructions","Carrier":"input_developer_message"}`)
	reorderedPrepared, err := PrepareSnapshotForManifest(reordered)
	if err != nil {
		t.Fatal(err)
	}
	if reorderedPrepared.Digest != prepared.Digest {
		t.Fatalf("键序不同的等价写法应得到同一份官方摘要：%s != %s", reorderedPrepared.Digest, prepared.Digest)
	}

	// 未知顶层键仍被拒绝：新增两个已知键不等于放宽严格解码。
	withUnknown := append([]byte(nil), encoded[:len(encoded)-1]...)
	withUnknown = append(withUnknown, []byte(`,"UnknownSection":{}}`)...)
	if _, err := ParseSnapshot(withUnknown); err == nil {
		t.Fatal("快照含未知顶层键应被拒绝")
	}
}

// 两节的每个字段都进入 ProfileDigest 与 executable 摘要：改任何一处都得到互不相同的新摘要。
func TestNewerSectionsEnterEveryDigestAndEachFieldMatters(t *testing.T) {
	_, extended := withAllOptionalSections(t)
	baseSpec, baseExecutable, err := compileExecutableDoc(t, extended)
	if err != nil {
		t.Fatal(err)
	}
	baseProfileDigest, err := baseSpec.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	mutations := []struct {
		label  string
		mutate func(doc *SnapshotDoc)
	}{
		{"ContentItemKind", func(doc *SnapshotDoc) {
			doc.BaseInstructions = mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
				fields["ContentItemKind"] = json.RawMessage(`"model.other_kind"`)
			})
		}},
		{"IDPrefix", func(doc *SnapshotDoc) {
			doc.BaseInstructions = mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
				fields["IDPrefix"] = json.RawMessage(`"item"`)
			})
		}},
		{"EndpointIDs", func(doc *SnapshotDoc) {
			doc.BaseInstructions = mutatedSectionJSON(t, baseInstructionsSectionJSON, func(fields map[string]json.RawMessage) {
				fields["EndpointIDs"] = json.RawMessage(`["responses_http"]`)
			})
		}},
		{"AllowedCyberPrograms", func(doc *SnapshotDoc) {
			doc.AccessPrograms = json.RawMessage(`{"AllowedCyberPrograms":["daybreak_blue","standard"]}`)
		}},
		{"DefaultCyberProgram", func(doc *SnapshotDoc) {
			doc.AccessPrograms = json.RawMessage(`{"AllowedCyberPrograms":["standard"],"DefaultCyberProgram":"standard"}`)
		}},
	}
	seenExecutable := map[string]string{baseExecutable.Digest(): "基准"}
	seenProfile := map[string]string{baseProfileDigest: "基准"}
	for _, item := range mutations {
		_, doc := withAllOptionalSections(t)
		item.mutate(&doc)
		spec, executable, err := compileExecutableDoc(t, doc)
		if err != nil {
			t.Fatalf("%s：变异后的画像应当仍然合法：%v", item.label, err)
		}
		profileDigest, err := spec.ProfileDigest()
		if err != nil {
			t.Fatal(err)
		}
		if other, dup := seenExecutable[executable.Digest()]; dup {
			t.Fatalf("%s：executable 摘要与 %s 相同，该字段没有进入摘要", item.label, other)
		}
		if other, dup := seenProfile[profileDigest]; dup {
			t.Fatalf("%s：ProfileDigest 与 %s 相同，该字段没有进入摘要", item.label, other)
		}
		seenExecutable[executable.Digest()] = item.label
		seenProfile[profileDigest] = item.label
	}
}
