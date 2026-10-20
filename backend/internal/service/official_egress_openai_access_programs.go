package service

import (
	"slices"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
)

// 请求体条件字段 access_programs：画像 AccessPrograms 节。
//
// 较新版本的 Responses 请求体可携带 access_programs（形如 {"cyber":"standard"}）。网关没有账号资格信息，
// 所以入站取值只有落在画像节 AllowedCyberPrograms 之内才允许出站；其余取值（子集外的程序、非对象、多余的键、
// 非字符串）一律整字段删除并告警，既不原样透传，也不试图“修复”成某个允许的取值。多余的键意味着画像没有覆盖的
// 程序类型，网关无法判断它是否影响账号资格，所以连同合法的 cyber 一起删除。
//
// 第一版只透传、不生成：入站没带就不带，节里即使写了 DefaultCyberProgram 也不使用——是否给某类账号默认开启
// 交给将来的账号级策略。画像没有这个节时本文件的函数都是恒等操作，access_programs 仍按旧逻辑被顶层字段闭集
// 剔除（并按“画像闭集外字段”告警），出站字节与引入该节之前相同。
//
// 调用点：HTTP 定型、WS 派生首帧。预热帧、链帧与工具续接帧都由已经归一化的首帧改写而来，原样继承其中的
// access_programs，不需要再各自处理。

const (
	officialCodexAccessProgramsField    = "access_programs"
	officialCodexAccessProgramsCyberKey = "cyber"
)

// 入站取值被拒绝的原因，固定的小集合，用作告警去重键的一部分，不含入站内容。
const (
	officialCodexAccessProgramsRejectNotObject      = "not_object"
	officialCodexAccessProgramsRejectUnexpectedKeys = "unexpected_keys"
	officialCodexAccessProgramsRejectMissingCyber   = "missing_cyber"
	officialCodexAccessProgramsRejectCyberNotString = "cyber_not_string"
	officialCodexAccessProgramsRejectNotAllowed     = "program_not_allowed"
)

// officialCodexAccessProgramsWarnLimit 是告警去重表的封顶：键里的程序名来自入站内容，必须有上限，
// 否则构造流量可以让表无限增长。真实场景里被拒绝的取值种类很少，触顶本身说明遇到的是构造流量。
const officialCodexAccessProgramsWarnLimit = 64

// officialCodexAccessProgramsLogValueLimit 限制日志里回显的入站程序名长度。
const officialCodexAccessProgramsLogValueLimit = 32

var (
	officialCodexAccessProgramsWarnMu sync.Mutex
	officialCodexAccessProgramsWarned = map[string]struct{}{}
)

// officialCodexAccessProgramsSection 返回 mode 对应画像的 AccessPrograms 节；画像没有该节时返回 nil，
// 调用方保持旧逻辑。返回的节是画像内的共享只读数据，调用方不得修改。
func officialCodexAccessProgramsSection(mode string) *profilecontract.AccessProgramsSection {
	return officialCodexOptionalSectionsForMode(mode).AccessPrograms
}

// officialCodexAccessProgramsReject 判断入站 access_programs 是否越出画像节的值域：合法时 reason 为空串，
// 否则返回固定集合里的原因；program 是入站的 cyber 取值（仅当它是字符串时有值），只用于告警回显。
func officialCodexAccessProgramsReject(
	value any,
	section *profilecontract.AccessProgramsSection,
) (reason string, program string) {
	object, ok := value.(map[string]any)
	if !ok {
		return officialCodexAccessProgramsRejectNotObject, ""
	}
	cyber, hasCyber := object[officialCodexAccessProgramsCyberKey]
	if len(object) > 1 || (len(object) == 1 && !hasCyber) {
		return officialCodexAccessProgramsRejectUnexpectedKeys, ""
	}
	if !hasCyber {
		return officialCodexAccessProgramsRejectMissingCyber, ""
	}
	program, ok = cyber.(string)
	if !ok {
		return officialCodexAccessProgramsRejectCyberNotString, ""
	}
	if !slices.Contains(section.AllowedCyberPrograms, program) {
		return officialCodexAccessProgramsRejectNotAllowed, program
	}
	return "", program
}

// normalizeOfficialCodexAccessPrograms 按画像节的值域归一化 payload 顶层的 access_programs，返回 payload 是否
// 被改动（即字段被删除）。section 为 nil、字段不存在或取值合法时不做任何改动；绝不生成缺席的字段。
func normalizeOfficialCodexAccessPrograms(
	payload map[string]any,
	section *profilecontract.AccessProgramsSection,
) bool {
	if section == nil || payload == nil {
		return false
	}
	value, exists := payload[officialCodexAccessProgramsField]
	if !exists {
		return false
	}
	reason, program := officialCodexAccessProgramsReject(value, section)
	if reason == "" {
		return false
	}
	delete(payload, officialCodexAccessProgramsField)
	warnOfficialCodexAccessProgramsDropped(reason, program, section.AllowedCyberPrograms)
	return true
}

// isOfficialCodexAccessProgramsToken 判断程序名是否只含小写字母、数字与下划线且不超过回显上限：
// 只有这样的取值才会进入去重键与日志回显，其余统一记为 other。
func isOfficialCodexAccessProgramsToken(program string) bool {
	if program == "" || len(program) > officialCodexAccessProgramsLogValueLimit {
		return false
	}
	for i := 0; i < len(program); i++ {
		c := program[i]
		if (c < 'a' || c > 'z') && (c < '0' || c > '9') && c != '_' {
			return false
		}
	}
	return true
}

// warnOfficialCodexAccessProgramsDropped 报告一次被删除的 access_programs。同一（原因，程序名）只告警一次：
// 重复出现并不增加信息量，而且每个请求都打印会淹没日志。
func warnOfficialCodexAccessProgramsDropped(reason, program string, allowed []string) {
	shown := "other"
	if isOfficialCodexAccessProgramsToken(program) {
		shown = program
	}
	key := reason + "|" + shown
	officialCodexAccessProgramsWarnMu.Lock()
	if _, seen := officialCodexAccessProgramsWarned[key]; seen ||
		len(officialCodexAccessProgramsWarned) >= officialCodexAccessProgramsWarnLimit {
		officialCodexAccessProgramsWarnMu.Unlock()
		return
	}
	officialCodexAccessProgramsWarned[key] = struct{}{}
	officialCodexAccessProgramsWarnMu.Unlock()
	logger.LegacyPrintf(
		"service.official_egress_openai",
		"[Codex] 入站 access_programs 不在画像值域内，已整字段删除：reason=%s program=%q allowed=%v",
		reason,
		shown,
		allowed,
	)
}
