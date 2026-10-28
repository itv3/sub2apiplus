// Codex 点数（credits）的显示规则。
//
// 为什么单独放一个文件：点数展示的取整逻辑原先直接写在上游已有组件 OpenAIQuotaResetCell.vue 里，
// 而该组件上游更新频繁，逻辑越多合并上游时越容易冲突。这里把取整逻辑收进分叉自己的新文件，
// 组件只剩「一行 import + 一处调用」。
//
// 约束：这里只决定页面上怎么显示。接口返回、缓存快照里的原始点数字符串不会被改写，仍是完整精度。

// 普通十进制字符串：可带一个正负号，整数部分和小数部分都允许为空（如 ".9"、"5."）。
// 科学计数法（1e3）、十六进制（0x10）等写法不匹配，调用方保持改动前的行为：原样显示。
const plainDecimalPattern = /^[+-]?(\d*)(?:\.\d*)?$/

// 点数只显示整数部分：直接舍去小数点及其后的内容，不做四舍五入。
// 全程按字符串处理，不转成 Number，因为超过 2^53 的整数部分转 Number 会丢精度。
// 这里只决定页面上怎么显示；接口返回、缓存快照里的原始点数字符串不会被改写，仍是完整精度。
// 无法按普通十进制解析时返回 null。
const toIntegerPoints = (balance: string): string | null => {
  const match = plainDecimalPattern.exec(balance)
  if (!match) return null
  // 去掉多余的前导零（"007" -> "7"），但至少保留一位；整数部分为空（".9"）时显示 "0"。
  // 正负号一律丢弃：到这里时数值已经通过非负检查，出现 "-" 只可能是 "-0" 这种负零。
  const integerPart = match[1].replace(/^0+(?=\d)/, '')
  return integerPart === '' ? '0' : integerPart
}

/**
 * 把点数余额字符串转成页面上显示的整数文本（直接舍去小数，不四舍五入；全程按字符串处理，不转 Number）。
 *
 * 调用方必须先完成有效值检查（非空、能解析成有限数、且不为负），本函数不再重复检查。
 * 例：57333.8597510000 -> 57333；0.9 -> 0；12345678901234567890.0123 -> 12345678901234567890。
 * 科学计数法（1.5e3）、十六进制（0x10）等无法安全地按字符串截断的写法，原样返回（保持改动前的行为）。
 */
export function truncateCreditsBalance(balance: string): string {
  return toIntegerPoints(balance) ?? balance
}
