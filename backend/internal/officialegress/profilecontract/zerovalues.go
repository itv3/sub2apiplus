package profilecontract

// 零值语义常量。
//
// 真实画像里 70 个 body 字段的 Condition 为空、42 个 OmitWhen 为空。这些不是
// "缺失数据"，而是**有明确语义的零值**：
//
//   - 无 Condition：无条件出现；
//   - 无 OmitWhen：从不省略。
//
// 注意 body 字段**没有 Source** 这个维度——真实 officialCodexBodyField 只有
// Name/Required/OmitWhen/Condition 四个字段。上一版拿"body 字段没有 Source"
// 当作零值语义的例证，前提本身就是错的。
//
// 必须在契约里显式定义，不能让转换器临时猜测——猜测会把"画像没写"和"画像写错"
// 混为一谈。这两个常量会进入 Observed 与 EngineSupported 的双层目录。
const (
	// ConditionUnconditional 表示无条件出现。
	ConditionUnconditional ConditionKind = ""
	// OmitNever 表示从不省略。
	OmitNever OmitCondition = ""
)

// 目标解析的整体委托标记。
//
// blob 上传端点的 Path 是 "{server_returned_path}"、Query 是 [{Name:"*"}]——
// 它们表示 **整个 path/query 来自服务端响应**，不是"带一个占位符的固定模板"。
// 上一版用 pathMatchesTemplate 拿多段真实 path 去比单段占位符，必然拒绝合法请求。
const (
	// WholePathFromServer 是 Path 模板的整体委托标记。
	WholePathFromServer = "{server_returned_path}"
	// WholeQueryFromServer 是 Query 的整体委托标记（Name 为 "*"）。
	WholeQueryFromServer = "*"
)
