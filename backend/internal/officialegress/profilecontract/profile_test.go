package profilecontract_test

// 0A 验收。四个顶层测试，全部属于 ProfileSpec，不混入执行层。

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strings"
	"testing"

	p "github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func loadRaw(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("testdata/snapshots/0.145.0/343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431.json")
	if err != nil {
		t.Fatalf("读取快照失败（先跑 go run ./cmd/egressprofiledump）: %v", err)
	}
	return raw
}

func mustSpec(t *testing.T, raw []byte) p.ProfileSpec {
	t.Helper()
	doc, err := p.ParseSnapshot(raw)
	if err != nil {
		t.Fatalf("严格解析失败: %v", err)
	}
	spec, err := p.NewProfileSpec(doc)
	if err != nil {
		t.Fatalf("构造 ProfileSpec 失败: %v", err)
	}
	return spec
}

// TestCanonicalRoundTripAgainstRawFile 是 0A 的唯一验收。
//
// 比较的左侧是**原始文件字节**规范化后的结果，不是 ParseSnapshot 的产物。
// 上一版比的是 canonical(ParseSnapshot(raw)) vs canonical(ToSnapshot())——
// 两边都经过同一套 omitempty，源里的显式零值被一起丢掉，于是一起变绿。
func TestCanonicalRoundTripAgainstRawFile(t *testing.T) {
	raw := loadRaw(t)

	var srcGeneric any
	if err := json.Unmarshal(raw, &srcGeneric); err != nil {
		t.Fatalf("解析原始文件: %v", err)
	}
	srcCanon, err := json.Marshal(srcGeneric)
	if err != nil {
		t.Fatal(err)
	}

	spec := mustSpec(t, raw)
	backCanon, err := p.CanonicalJSON(spec.ToSnapshot())
	if err != nil {
		t.Fatalf("规范化往返结果: %v", err)
	}

	if string(srcCanon) != string(backCanon) {
		diffs := jsonDiff(srcCanon, backCanon)
		t.Errorf("往返与原始文件不相等，%d 处差异：", len(diffs))
		for i, d := range diffs {
			if i >= 25 {
				t.Errorf("  ...（还有 %d 处）", len(diffs)-25)
				break
			}
			t.Errorf("  %s", d)
		}
	}
}

// TestProfileSpecIsDeeplyImmutable 用**反射**遍历所有返回对象并就地改写。
//
// 上一版手写字段列表，漏掉了 SignatureAlgorithms、SupportedVersions、KeyShareGroups
// 以及 ToSnapshot() 的多个引用字段——当前实现恰好都深拷贝了，但新增一个 slice 字段
// 就会静默漏测。反射遍历不依赖作者记得哪些字段。
func TestProfileSpecIsDeeplyImmutable(t *testing.T) {
	spec := mustSpec(t, loadRaw(t))
	before, err := spec.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}

	// 覆盖全部 getter 与 ToSnapshot()
	targets := []any{
		spec.Endpoints(),
		spec.Transports(),
		spec.CrossSections(),
		spec.ToSnapshot(),
	}
	mutated := 0
	for _, tgt := range targets {
		mutated += corruptAllLeaves(reflect.ValueOf(tgt))
	}
	if mutated == 0 {
		t.Fatal("反射未改写任何叶子，测试无效")
	}
	t.Logf("反射改写了 %d 个叶子", mutated)

	after, err := spec.ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatalf("修改返回对象后 digest 改变——ProfileSpec 未做全量深拷贝\n前: %s\n后: %s", before, after)
	}
}

// corruptAllLeaves 递归破坏所有可寻址的标量叶子，返回改写计数。
func corruptAllLeaves(v reflect.Value) int {
	switch v.Kind() {
	case reflect.Pointer, reflect.Interface:
		if v.IsNil() {
			return 0
		}
		return corruptAllLeaves(v.Elem())
	case reflect.Slice, reflect.Array:
		n := 0
		for i := 0; i < v.Len(); i++ {
			n += corruptAllLeaves(v.Index(i))
		}
		return n
	case reflect.Struct:
		n := 0
		for i := 0; i < v.NumField(); i++ {
			n += corruptAllLeaves(v.Field(i))
		}
		return n
	case reflect.String:
		if v.CanSet() {
			v.SetString(v.String() + "_MUT")
			return 1
		}
	case reflect.Bool:
		if v.CanSet() {
			v.SetBool(!v.Bool())
			return 1
		}
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		if v.CanSet() {
			v.SetInt(v.Int() + 1)
			return 1
		}
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		if v.CanSet() {
			v.SetUint(v.Uint() + 1)
			return 1
		}
	}
	return 0
}

// TestParseRejectsUnknownFieldAndTrailingData 覆盖两类解析放行。
func TestParseRejectsUnknownFieldAndTrailingData(t *testing.T) {
	raw := loadRaw(t)

	t.Run("未知字段", func(t *testing.T) {
		mutated := append([]byte(nil), raw...)
		for i := range mutated {
			if mutated[i] == '{' {
				mutated = append(mutated[:i+1],
					append([]byte(`"BrandNewFieldFromNextVersion":123,`), mutated[i+1:]...)...)
				break
			}
		}
		if _, err := p.ParseSnapshot(mutated); err == nil {
			t.Fatal("未知字段必须失败，否则画像新增字段会被静默忽略")
		}
	})

	t.Run("尾随数据", func(t *testing.T) {
		mutated := append(append([]byte(nil), raw...), []byte(`{"second":1}`)...)
		if _, err := p.ParseSnapshot(mutated); err == nil {
			t.Fatal("尾随第二个 JSON 必须失败")
		}
	})
}

// mutationStats 分类统计，用于证明"每个叶子都真正进入了 ProfileSpec"。
type mutationStats struct {
	collected          int
	mutated            int
	parseRejected      int
	validationRejected int
	digestChanged      int
	skipped            []string // 未能变异或变异后 digest 不变
}

// TestEveryLeafEntersProfileSpec 逐叶变异，要求 skipped 为零。
//
// 上一版把"解析失败""枚举拒绝"也算作检出，并且对 49 个 null 直接 continue，
// 于是"2451 个叶子全部检出"实际只覆盖了 2402 个，而且其中一部分只证明了
// 解析层能拒绝，没证明字段进入了 ProfileSpec 与 digest。
//
// 这里分开统计：只有**成功构造后 digest 改变**才算该字段真正被承载。
func TestEveryLeafEntersProfileSpec(t *testing.T) {
	raw := loadRaw(t)
	baseDigest, err := mustSpec(t, raw).ProfileDigest()
	if err != nil {
		t.Fatal(err)
	}

	var root any
	if err := json.Unmarshal(raw, &root); err != nil {
		t.Fatal(err)
	}

	// 收集每个字段名在快照中出现过的全部值。
	// 变异枚举字段时从中取另一个值——它必然合法（快照里存在），
	// 因此不会被枚举校验挡住，从而能真正验证该字段进入了 digest。
	observed := map[string]map[string]bool{}
	collectObservedValues(root, nil, observed)
	arrayTemplates = map[string]any{}
	collectArrayTemplates(root, arrayTemplates)
	nonNullTemplates = map[string]any{}
	collectNonNullTemplates(root, nonNullTemplates)

	st := mutationStats{}
	paths := collectLeafPaths(root, nil)
	st.collected = len(paths)

	for _, path := range paths {
		label := strings.Join(path, ".")

		// 依次尝试多种变异策略，直到某一种能被成功构造并改变 digest。
		// 只有「成功构造 + digest 改变」才算该字段真正被 ProfileSpec 承载；
		// 解析拒绝与校验拒绝只说明防线有效，不能替代承载证明。
		var (
			accepted  bool
			lastParse bool
			lastValid bool
		)
		for _, gen := range mutationStrategies(path, observed) {
			mutatedRaw, ok := gen(raw, path)
			if !ok {
				continue
			}
			doc, err := p.ParseSnapshot(mutatedRaw)
			if err != nil {
				lastParse = true
				continue
			}
			spec, err := p.NewProfileSpec(doc)
			if err != nil {
				lastValid = true
				continue
			}
			d, err := spec.ProfileDigest()
			if err != nil {
				continue
			}
			if d != baseDigest {
				accepted = true
				break
			}
		}
		st.mutated++
		switch {
		case accepted:
			st.digestChanged++
		case lastValid:
			st.validationRejected++
			st.skipped = append(st.skipped, label+"（所有变异都被校验拒绝，未能证明进入 digest）")
		case lastParse:
			st.parseRejected++
			st.skipped = append(st.skipped, label+"（所有变异都被解析拒绝，未能证明进入 digest）")
		default:
			st.skipped = append(st.skipped, label+"（无法构造有效变异）")
		}
	}

	t.Logf("叶子 %d | 已变异 %d | 解析拒绝 %d | 校验拒绝 %d | digest 改变 %d | 未检出 %d",
		st.collected, st.mutated, st.parseRejected, st.validationRejected,
		st.digestChanged, len(st.skipped))

	if len(st.skipped) > 0 {
		sort.Strings(st.skipped)
		t.Errorf("%d 个叶子变异后未被检出——这些字段没有真正进入 ProfileSpec：", len(st.skipped))
		for i, s := range st.skipped {
			if i >= 30 {
				t.Errorf("  ...（还有 %d 个）", len(st.skipped)-30)
				break
			}
			t.Errorf("  %s", s)
		}
	}
	if st.mutated != st.collected {
		t.Errorf("有 %d 个叶子未能变异，覆盖不完整", st.collected-st.mutated)
	}
}

// =============================================================================
// 通用 JSON 工具
// =============================================================================

func collectLeafPaths(v any, prefix []string) [][]string {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var out [][]string
		for _, k := range keys {
			out = append(out, collectLeafPaths(t[k], append(append([]string(nil), prefix...), k))...)
		}
		return out
	case []any:
		if len(t) == 0 {
			return [][]string{append([]string(nil), prefix...)}
		}
		var out [][]string
		for i, e := range t {
			out = append(out, collectLeafPaths(e,
				append(append([]string(nil), prefix...), fmt.Sprintf("[%d]", i)))...)
		}
		return out
	default:
		return [][]string{append([]string(nil), prefix...)}
	}
}

func setLeaf(root any, path []string, fn func(any) (any, bool)) bool {
	cur := root
	for i, seg := range path {
		last := i == len(path)-1
		if strings.HasPrefix(seg, "[") {
			idx := 0
			if _, err := fmt.Sscanf(seg, "[%d]", &idx); err != nil {
				return false
			}
			arr, ok := cur.([]any)
			if !ok || idx >= len(arr) {
				return false
			}
			if last {
				nv, ok := fn(arr[idx])
				if !ok {
					return false
				}
				arr[idx] = nv
				return true
			}
			cur = arr[idx]
			continue
		}
		m, ok := cur.(map[string]any)
		if !ok {
			return false
		}
		if last {
			nv, ok := fn(m[seg])
			if !ok {
				return false
			}
			m[seg] = nv
			return true
		}
		cur = m[seg]
	}
	return false
}

func mutateLeaf(raw []byte, path []string) ([]byte, bool) {
	var root any
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, false
	}
	if !setLeaf(root, path, mutateScalar) {
		return nil, false
	}
	out, err := json.Marshal(root)
	if err != nil {
		return nil, false
	}
	return out, true
}

// collectObservedValues 记录每个字段名出现过的全部字符串值。
func collectObservedValues(v any, prefix []string, out map[string]map[string]bool) {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if s, ok := val.(string); ok {
				if out[k] == nil {
					out[k] = map[string]bool{}
				}
				out[k][s] = true
			}
			collectObservedValues(val, append(prefix, k), out)
		}
	case []any:
		for _, e := range t {
			collectObservedValues(e, prefix, out)
		}
	}
}

type mutationGen func(raw []byte, path []string) ([]byte, bool)

// mutationStrategies 按顺序返回可尝试的变异方式。
func mutationStrategies(path []string, observed map[string]map[string]bool) []mutationGen {
	gens := []mutationGen{mutateLeaf}
	// 对字符串叶子，追加"换成同字段的另一个已观测值"——必然合法。
	if len(path) > 0 {
		key := path[len(path)-1]
		if strings.HasPrefix(key, "[") && len(path) > 1 {
			key = path[len(path)-2]
		}
		if vals := observed[key]; len(vals) > 1 {
			gens = append(gens, func(raw []byte, p2 []string) ([]byte, bool) {
				return replaceWithObserved(raw, p2, vals)
			})
		}
	}
	gens = append(gens, replaceLeafWithSentinel)
	if arrayTemplates != nil {
		gens = append(gens, replaceNullArrayWithTemplate(arrayTemplates))
	}
	if nonNullTemplates != nil {
		gens = append(gens, replaceNullWithTemplate(nonNullTemplates))
	}
	// 兜底：字符串数组。ALPN 在两个 transport 里都是 null，取不到模板。
	gens = append(gens, func(raw []byte, path []string) ([]byte, bool) {
		return replaceNullWithValue(raw, path, []any{"sentinel"})
	})
	return gens
}

// nonNullTemplates 收集每个字段名出现过的**任意非 null 值**，
// 用于给 null 对象字段（如 WebSocket）构造合法变异。
var nonNullTemplates map[string]any

func collectNonNullTemplates(v any, out map[string]any) {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if val != nil {
				if _, exists := out[k]; !exists {
					out[k] = val
				}
			}
			collectNonNullTemplates(val, out)
		}
	case []any:
		for _, e := range t {
			collectNonNullTemplates(e, out)
		}
	}
}

func replaceNullWithTemplate(templates map[string]any) mutationGen {
	return func(raw []byte, path []string) ([]byte, bool) {
		if len(path) == 0 {
			return nil, false
		}
		tmpl, ok := templates[path[len(path)-1]]
		if !ok {
			return nil, false
		}
		return replaceNullWithValue(raw, path, tmpl)
	}
}

func replaceNullWithValue(raw []byte, path []string, val any) ([]byte, bool) {
	var root any
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, false
	}
	set := setLeaf(root, path, func(old any) (any, bool) {
		if old == nil {
			return val, true
		}
		return nil, false
	})
	if !set {
		return nil, false
	}
	out, err := json.Marshal(root)
	if err != nil {
		return nil, false
	}
	return out, true
}

// arrayTemplates 由 TestEveryLeafEntersProfileSpec 初始化。
var arrayTemplates map[string]any

func replaceWithObserved(raw []byte, path []string, vals map[string]bool) ([]byte, bool) {
	var root any
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, false
	}
	ok := setLeaf(root, path, func(old any) (any, bool) {
		cur, isStr := old.(string)
		if !isStr {
			return nil, false
		}
		names := make([]string, 0, len(vals))
		for v := range vals {
			if v != cur {
				names = append(names, v)
			}
		}
		if len(names) == 0 {
			return nil, false
		}
		sort.Strings(names)
		return names[0], true
	})
	if !ok {
		return nil, false
	}
	out, err := json.Marshal(root)
	if err != nil {
		return nil, false
	}
	return out, true
}

// collectArrayTemplates 记录每个字段名出现过的非空数组的首元素。
//
// null 的 slice 字段（Body.Fields、HeaderMapInsertionOrder、Query 等）不能简单换成
// [1]——元素类型不匹配，解析层会拒绝，于是"未能证明进入 digest"。用同名字段在别处
// 的真实元素做模板，变异必然合法。
func collectArrayTemplates(v any, out map[string]any) {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if arr, ok := val.([]any); ok && len(arr) > 0 {
				if _, exists := out[k]; !exists {
					out[k] = arr[0]
				}
			}
			collectArrayTemplates(val, out)
		}
	case []any:
		for _, e := range t {
			collectArrayTemplates(e, out)
		}
	}
}

// replaceNullArrayWithTemplate 用同名字段的真实元素填充 null 数组。
func replaceNullArrayWithTemplate(templates map[string]any) mutationGen {
	return func(raw []byte, path []string) ([]byte, bool) {
		if len(path) == 0 {
			return nil, false
		}
		key := path[len(path)-1]
		tmpl, ok := templates[key]
		if !ok {
			return nil, false
		}
		var root any
		if err := json.Unmarshal(raw, &root); err != nil {
			return nil, false
		}
		set := setLeaf(root, path, func(old any) (any, bool) {
			if old == nil {
				return []any{tmpl}, true
			}
			return nil, false
		})
		if !set {
			return nil, false
		}
		out, err := json.Marshal(root)
		if err != nil {
			return nil, false
		}
		return out, true
	}
}

// replaceLeafWithSentinel 处理 null 与空数组：换成一个有区分度的非零值。
func replaceLeafWithSentinel(raw []byte, path []string) ([]byte, bool) {
	var root any
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, false
	}
	ok := setLeaf(root, path, func(old any) (any, bool) {
		switch old.(type) {
		case nil:
			return "SENTINEL_WAS_NULL", true
		case []any:
			return []any{"SENTINEL_WAS_EMPTY"}, true
		}
		return nil, false
	})
	if !ok {
		return nil, false
	}
	out, err := json.Marshal(root)
	if err != nil {
		return nil, false
	}
	return out, true
}

func mutateScalar(v any) (any, bool) {
	switch t := v.(type) {
	case string:
		return t + "_MUT", true
	case bool:
		return !t, true
	case float64:
		return t + 1, true
	}
	return nil, false
}

func jsonDiff(a, b []byte) []string {
	var va, vb any
	if err := json.Unmarshal(a, &va); err != nil {
		return []string{"源解析失败: " + err.Error()}
	}
	if err := json.Unmarshal(b, &vb); err != nil {
		return []string{"往返结果解析失败: " + err.Error()}
	}
	var diffs []string
	walkDiff(va, vb, nil, &diffs)
	return diffs
}

func walkDiff(a, b any, path []string, out *[]string) {
	if reflect.DeepEqual(a, b) {
		return
	}
	loc := strings.Join(path, ".")
	am, aok := a.(map[string]any)
	bm, bok := b.(map[string]any)
	if aok && bok {
		keys := map[string]bool{}
		for k := range am {
			keys[k] = true
		}
		for k := range bm {
			keys[k] = true
		}
		sorted := make([]string, 0, len(keys))
		for k := range keys {
			sorted = append(sorted, k)
		}
		sort.Strings(sorted)
		for _, k := range sorted {
			av, aHas := am[k]
			bv, bHas := bm[k]
			switch {
			case aHas && !bHas:
				*out = append(*out, fmt.Sprintf("%s.%s: 往返后丢失", loc, k))
			case !aHas && bHas:
				*out = append(*out, fmt.Sprintf("%s.%s: 往返后凭空出现", loc, k))
			default:
				walkDiff(av, bv, append(path, k), out)
			}
		}
		return
	}
	aa, aok2 := a.([]any)
	ba, bok2 := b.([]any)
	if aok2 && bok2 {
		if len(aa) != len(ba) {
			*out = append(*out, fmt.Sprintf("%s: 长度 %d → %d", loc, len(aa), len(ba)))
			return
		}
		for i := range aa {
			walkDiff(aa[i], ba[i], append(path, fmt.Sprintf("[%d]", i)), out)
		}
		return
	}
	*out = append(*out, fmt.Sprintf("%s: %v → %v", loc, a, b))
}
