import { describe, expect, it } from 'vitest'
import { truncateCreditsBalance } from '../codexCredits'

describe('truncateCreditsBalance — 点数只显示整数部分', () => {
  // 点数字符串到页面显示文本的对照表：直接舍去小数点及其后的内容，不四舍五入；
  // 全程按字符串处理，整数部分超过 2^53 也必须保持完整。
  // 前后空白的去除、负数走「可用」属于组件里的有效值检查，不在这个纯函数里，对应用例留在
  // components/account/__tests__/OpenAIQuotaResetCell.spark_shadow.spec.ts 的端到端用例中。
  const cases = [
    { name: '示例值 57333.8597510000 舍去小数', balance: '57333.8597510000', expected: '57333' },
    { name: '0.9 舍去后是 0，不四舍五入成 1', balance: '0.9', expected: '0' },
    { name: '1.9999 不进位', balance: '1.9999', expected: '1' },
    { name: '0.0000 显示 0', balance: '0.0000', expected: '0' },
    { name: '没有小数点的整数原样显示', balance: '42', expected: '42' },
    { name: '超长整数部分保持完整', balance: '12345678901234567890.0123', expected: '12345678901234567890' },
    { name: '超过 2^53 的整数不丢精度（Number 会变成 ...992）', balance: '9007199254740993', expected: '9007199254740993' },
    { name: '超长整数部分加超长小数部分', balance: '99999999999999999999999999999999.99999999999999999999', expected: '99999999999999999999999999999999' },
    { name: '整数部分为空的 .9 显示 0', balance: '.9', expected: '0' },
    { name: '小数点后为空的 5. 显示 5', balance: '5.', expected: '5' },
    { name: '带正号的 +7.8 去掉正号', balance: '+7.8', expected: '7' },
    { name: '多余前导零 007.5 规整为 7', balance: '007.5', expected: '7' },
    { name: '全是零的 000 规整为 0（至少保留一位）', balance: '000', expected: '0' },
    { name: '负零 -0.0 显示 0（通过非负检查的唯一负号写法）', balance: '-0.0', expected: '0' },
    // 不是普通十进制的写法无法安全地按字符串截断，保持改动前的行为：原样返回，不取整。
    { name: '科学计数法 1.5e3 原样返回', balance: '1.5e3', expected: '1.5e3' },
    { name: '科学计数法 1E3 原样返回', balance: '1E3', expected: '1E3' },
    { name: '科学计数法带负指数 25e-1 原样返回', balance: '25e-1', expected: '25e-1' },
    { name: '十六进制 0x10 原样返回', balance: '0x10', expected: '0x10' },
    { name: '二进制 0b101 原样返回', balance: '0b101', expected: '0b101' },
    { name: '八进制 0o17 原样返回', balance: '0o17', expected: '0o17' },
  ]

  it.each(cases)('$name', ({ balance, expected }) => {
    expect(truncateCreditsBalance(balance)).toBe(expected)
  })

  // 契约：本函数不负责去前后空白和有效值检查（调用方先 trim、先检查再调用），带空白的输入原样返回。
  // 钉住这一点是为了让「在哪一层 trim」不被悄悄挪动：挪走后组件里的空白用例会红。
  it('不处理前后空白：带空白的输入不匹配普通十进制，原样返回', () => {
    expect(truncateCreditsBalance('  57333.85  ')).toBe('  57333.85  ')
  })
})

// 源码锚定：除 OpenAIQuotaResetCell.vue 外，页面源码里不得再出现直接读取 Codex 点数快照／余额的地方。
//
// 这是有意设计的「响亮告警」，不是误报：取整只在 OpenAIQuotaResetCell.vue 的 creditsDisplay 里调用一次
// truncateCreditsBalance。如果上游合并或以后的新功能在别处也展示点数余额，那处不会自动取整，
// 页面上就会出现带小数的点数，而且编译、类型检查、组件测试都不会报错。
// 所以这里让「出现第二个展示点」直接导致测试变红，逼开发者当场决定：
// 新展示点必须调用 truncateCreditsBalance（并把文件加进下面的 allowedFiles），或改为复用现有组件。
describe('点数展示点锚定', () => {
  // 只扫页面源码（组件与视图），不含各目录下的 __tests__。?raw 以字符串形式读入文件内容。
  const sources = import.meta.glob(
    [
      '/src/components/**/*.vue',
      '/src/components/**/*.ts',
      '/src/views/**/*.vue',
      '/src/views/**/*.ts',
      '!/src/**/__tests__/**',
      '!/src/**/*.spec.ts',
      '!/src/**/*.test.ts',
    ],
    { query: '?raw', import: 'default', eager: true },
  ) as Record<string, string>

  // 唯一允许直接读取点数快照／余额的文件。
  const allowedFiles = ['/src/components/account/OpenAIQuotaResetCell.vue']
  // 命中任一写法即视为「直接展示或读取点数余额」：快照字段名，或 credits.balance／credits?.balance。
  const creditsReadPattern = /codex_credits_snapshot|credits\??\.balance/

  it('扫描范围有效，且唯一允许的展示点确实命中（防止 glob 失效导致空扫描通过）', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(100)
    for (const file of allowedFiles) {
      expect(sources[file], `${file} 应在扫描范围内`).toBeDefined()
      expect(creditsReadPattern.test(sources[file])).toBe(true)
    }
  })

  it('除 OpenAIQuotaResetCell.vue 外没有第二个直接展示点数余额的地方', () => {
    const hits = Object.entries(sources)
      .filter(([, content]) => creditsReadPattern.test(content))
      .map(([file]) => file)
      .sort()
    // 若在这里变红：说明出现了新的点数展示点，请让它调用 truncateCreditsBalance 后再把文件加入 allowedFiles。
    expect(hits).toEqual([...allowedFiles].sort())
  })
})
