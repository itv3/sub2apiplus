package service

import "testing"

// 较旧槽位的形态守卫。
//
// 一批用例把期望写成「增量窗口方案出现之前」的线上形态（预热帧带 additional_tools、基础指令消息 id 为
// UUIDv5 公式、images 端点没有回合头、逐项 passthrough 整体剥离等），并用「较旧槽位」夹具固定到当初批准这些
// 语义的发布上重放。较旧槽位本身已经是增量窗口方案（BaseInstructions.IDScheme=uuidv7）之后，上述形态在线上
// 不可达：断言没有错，是重放的前提不存在。这时按原因跳过，与旧画像槽位消失时的做法一致；新方案的对应语义由
// 按结构事实定位目标槽位的晋升后门禁（official_egress_codex_promotion_gate_*）覆盖。
//
// 判据只看画像的结构事实，不看版本号：集成态（较旧槽位仍是旧方案）不触发，行为与加入守卫之前逐项一致。

// olderSlotIsPreUUIDv7Form 报告 Active／Previous 里画像版本较旧的槽位是否仍是增量窗口方案出现之前的形态。
func olderSlotIsPreUUIDv7Form(t *testing.T) bool {
	t.Helper()
	section := codexGateExecutableProfile(t, codexGateOlderReleaseMode(t)).Optional().BaseInstructions
	return section == nil || !section.UsesUUIDv7IDs()
}

// skipUnlessSlotIsPreUUIDv7Form 在 mode 槽位的画像已是增量窗口方案时跳过当前用例。
func skipUnlessSlotIsPreUUIDv7Form(t *testing.T, mode string) {
	t.Helper()
	section := codexGateExecutableProfile(t, mode).Optional().BaseInstructions
	if section != nil && section.UsesUUIDv7IDs() {
		t.Skipf("槽位 %s 的画像已是增量窗口方案（IDScheme=uuidv7）：本用例断言的是该方案出现之前的线上形态，在此槽位不可达；"+
			"新方案的对应语义由按结构事实定位目标槽位的晋升后门禁覆盖", mode)
	}
}

// skipUnlessOlderSlotIsPreUUIDv7Form 在较旧槽位已是增量窗口方案时跳过当前用例（用于固定较旧槽位的用例）。
func skipUnlessOlderSlotIsPreUUIDv7Form(t *testing.T) {
	t.Helper()
	if !olderSlotIsPreUUIDv7Form(t) {
		t.Skip("较旧槽位的画像已是增量窗口方案（IDScheme=uuidv7）：本用例断言的是该方案出现之前的线上形态，在此槽位不可达；" +
			"新方案的对应语义由按结构事实定位目标槽位的晋升后门禁覆盖")
	}
}
