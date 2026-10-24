package service

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/net/html"
)

var (
	ollamaUsagePercentPattern  = regexp.MustCompile(`(?i)([0-9]+(?:\.[0-9]+)?)\s*%`)
	ollamaUsageWidthPattern    = regexp.MustCompile(`(?i)(?:^|;)\s*width\s*:\s*([0-9]+(?:\.[0-9]+)?)%`)
	ollamaBalancePattern       = regexp.MustCompile(`(?i)(?:balance|credits?)(?:\s+[[:alpha:]]+){0,4}\s*[:\n]?\s*((?:USD\s*)?\$?\s*-?[0-9][0-9,]*(?:\.[0-9]{1,4})?)`)
	ollamaResetPattern         = regexp.MustCompile(`(?i)\breset(?:s|ting)?\s*(?:at|in|on)?\s*[:\-]?\s*([^\n|]+)`)
	ollamaModelFallbackPattern = regexp.MustCompile(`(?i)^(.+?)\s+([0-9][0-9,]*)\s+requests?$`)

	// 新旧版设置页的 5 小时 / 7 天窗口标签并存：旧版为「Session usage」「Weekly usage」，
	// 新版文案未必沿用，故多收几个常见变体（均要求标签文本整体等于别名或以别名加空格开头）。
	ollamaFiveHourUsageAliases = []string{"session usage", "hourly usage", "session limit", "5 hour usage", "5-hour usage", "5h usage", "5 hour limit", "5-hour limit"}
	ollamaSevenDayUsageAliases = []string{"weekly usage", "week usage", "7 day usage", "7-day usage", "7d usage", "weekly limit", "7 day limit"}

	// 免费计划页面的「Free usage」窗口标签；同页还有一句以 “Free usage credits can be used …”
	// 开头的说明文字，必须排除，否则会把说明句当成窗口标签。
	ollamaFreeUsageAliases = []string{"free usage"}

	// 套餐徽章后面可能紧跟的非套餐文字，截断到这些词之前。
	ollamaPlanStopPattern = regexp.MustCompile(`(?i)(\$|[0-9]|\badd funds\b|\bauto-?reload\b|\bmanage\b|\bbilling\b|\bbalance\b|\bbuy\b|\bupgrade\b|\btop[ -]?up\b)`)
	// 套餐文字里不应出现的虚词：出现即认为取到的是一句说明而不是徽章。
	ollamaPlanSentenceWords = map[string]struct{}{
		"can": {}, "will": {}, "are": {}, "is": {}, "be": {}, "for": {}, "to": {}, "the": {}, "and": {}, "you": {}, "your": {},
		"with": {}, "of": {}, "a": {}, "an": {}, "in": {}, "on": {}, "or": {}, "used": {}, "use": {}, "remaining": {},
	}
	// 新版付费计划页面的「Monthly credits used」月度额度行。
	ollamaMonthlyUsageAliases = []string{"monthly credits used", "monthly usage", "credits used"}
	// 金额：必须带 $ 或 USD 标记（可有 <、≈、~ 前缀），避免把普通数字当金额。
	ollamaMoneyPattern = regexp.MustCompile(`(?i)(?:\bUSD\s*)?[<≈~]?\s*\$\s*-?[0-9][0-9,]*(?:\.[0-9]{1,4})?|\bUSD\s*-?[0-9][0-9,]*(?:\.[0-9]{1,4})?`)
	// 「Refills to $60 on November 24, 2026.」：额度重置的精确日期（只有日期，没有时刻）。
	ollamaRefillDatePattern = regexp.MustCompile(`(?i)\b(?:refills?|resets?)\s+(?:to\s+\S+\s+)?on\s+([A-Za-z]{3,9}\.?\s+[0-9]{1,2}(?:st|nd|rd|th)?,?\s+[0-9]{4})`)
	// 进度条 aria-label 里的「Monthly credits used: $0 of $60」，取 of 后面的月度额度总额。
	ollamaOfLimitPattern = regexp.MustCompile(`(?i)\bof\s+((?:USD\s*)?[<≈~]?\s*\$\s*-?[0-9][0-9,]*(?:\.[0-9]{1,4})?)`)
	// 提示框或页面文本里明确写着 Total 的金额。
	ollamaTotalMoneyPattern = regexp.MustCompile(`(?i)\btotal\s*:?\s*((?:USD\s*)?[<≈~]?\s*\$\s*-?[0-9][0-9,]*(?:\.[0-9]{1,4})?)`)
	// 「Monthly credits refill in 4 weeks」「Refills in 2 weeks.」一类的相对重置文案。
	ollamaRefillPattern   = regexp.MustCompile(`(?i)\brefills?\s+(in\s+(?:about\s+|around\s+|~)?(?:an?|one|[0-9]+)\s+(?:minute|hour|day|week|month|year)s?)`)
	ollamaRelativePattern = regexp.MustCompile(`(?i)\bin\s+(?:about\s+|around\s+|~)?(an?|one|[0-9]+)\s+(minute|hour|day|week|month|year)s?\b`)
	// 进度条宽度的 Tailwind 任意值写法，如 w-[12.5%]。
	ollamaBarClassPattern = regexp.MustCompile(`(?:^|\s)w-\[([0-9]+(?:\.[0-9]+)?)%\]`)
	// 最近请求行的时间列（「28 minutes ago」「just now」），整格匹配。
	ollamaRecentTimePattern  = regexp.MustCompile(`(?i)^(?:just now|yesterday|(?:an?|[0-9]+)\s+[[:alpha:]]+\s+ago)$`)
	ollamaEmailPattern       = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	ollamaFreeModelPattern   = regexp.MustCompile(`^[a-z0-9][a-z0-9._:/\-]{0,127}$`)
	ollamaFreeModelStopWords = map[string]struct{}{"and": {}, "or": {}, "more": {}, "others": {}, "usage": {}, "used": {}, "free": {}, "etc": {}}
)

func parseOllamaCloudUsageHTML(body []byte) (*OllamaCloudUsageData, error) {
	return parseOllamaCloudUsageHTMLAt(body, time.Now())
}

// parseOllamaCloudUsageHTMLAt 与 parseOllamaCloudUsageHTML 相同，now 用于把页面上
// 「in 4 weeks」这类相对重置时间换算成近似绝对时间（仅新版月度额度与免费额度窗口，
// 且仅在页面没有给出精确 data-time 时使用）。
func parseOllamaCloudUsageHTMLAt(body []byte, now time.Time) (*OllamaCloudUsageData, error) {
	doc, err := html.Parse(strings.NewReader(string(body)))
	if err != nil {
		return nil, fmt.Errorf("parse settings HTML: %w", err)
	}
	pageText := normalizedNodeText(doc)
	lowerPage := strings.ToLower(pageText)
	if containsAny(lowerPage, "sign in to ollama", "log in to ollama", "continue to sign in") {
		return nil, errOllamaCloudUsageUnauthorizedHTML
	}

	data := &OllamaCloudUsageData{}
	data.Plan = valueBesideLabel(doc, []string{"cloud usage"}, 80)
	if data.Plan == "" {
		// 新版页面（含免费计划）：套餐以徽章形式跟在「Usage credits」标题旁。
		data.Plan = capitalizeOllamaPlan(parseOllamaPlanFromUsageCredits(doc))
	}
	if data.Plan == "" {
		data.Plan = valueBesideLabel(doc, []string{"plan", "subscription"}, 80)
	}
	data.Email = parseOllamaAccountEmail(doc)
	data.FiveHour = parseOllamaUsageWindow(doc, ollamaFiveHourUsageAliases)
	data.SevenDay = parseOllamaUsageWindow(doc, ollamaSevenDayUsageAliases)
	data.Balance = valueBesideLabel(doc, []string{"balance remaining"}, 80)
	if data.Balance == "" {
		// 新版页面：余额取「合计」，即「Usage credits」标题行右侧的主金额（如 $60）。
		data.Balance = parseOllamaTotalCredits(doc, pageText)
	}
	// 追加额度（Added credits）：id 为 extra-usage-balance 的元素，与合计不是同一个数。
	data.AddedCredits = parseOllamaAddedCredits(doc)
	if data.Balance == "" {
		if match := ollamaBalancePattern.FindStringSubmatch(pageText); len(match) == 2 {
			data.Balance = strings.Join(strings.Fields(match[1]), "")
		}
	}
	data.Models = parseOllamaModels(doc)
	// 免费计划特有：免费额度窗口与可用免费模型清单。付费页面没有这些内容，结果为空。
	data.FreeUsage = parseOllamaFreeUsageWindow(doc, now)
	data.FreeModels = parseOllamaFreeModels(doc)
	// 新版付费计划：月度额度窗口与最近请求列表（取代旧版 5 小时 / 7 天窗口）。
	data.Monthly = parseOllamaMonthlyWindow(doc, pageText, data.Balance, now)
	data.RecentRequests = parseOllamaRecentRequests(doc)

	if data.Plan == "" && data.FiveHour == nil && data.SevenDay == nil && data.FreeUsage == nil && data.Monthly == nil &&
		data.Balance == "" && data.AddedCredits == "" && len(data.Models) == 0 && len(data.FreeModels) == 0 && len(data.RecentRequests) == 0 {
		return nil, fmt.Errorf("settings HTML does not contain recognizable usage fields")
	}
	return data, nil
}

func parseOllamaUsageWindow(root *html.Node, aliases []string) *OllamaCloudUsageWindow {
	return parseOllamaUsageWindowWith(findLabelElement(root, aliases), nil, false)
}

// parseOllamaFreeUsageWindow 解析免费计划的「Free usage」窗口。标签排除以
// “free usage credits” 开头的说明句；进度条没有 data-usage-track 标记时，
// 允许退回到块内任意带 width 百分比样式的元素（免费页只有一条进度条）。
func parseOllamaFreeUsageWindow(root *html.Node, now time.Time) *OllamaCloudUsageWindow {
	label := findLabelElementFiltered(root, ollamaFreeUsageAliases, func(lowerText string) bool {
		return strings.HasPrefix(lowerText, "free usage credits")
	})
	return parseOllamaUsageWindowWith(label, &now, true)
}

// parseOllamaUsageWindowWith 从标签元素向上最多 6 层寻找「x% used」文本或进度条宽度。
// anyBar 为 true 时，文本与 data-usage-track 都取不到则再试任意进度条写法；
// relativeNow 非空表示新版页面窗口：重置文案取「Refills in …」并在没有精确时间时按相对时间换算。
func parseOllamaUsageWindowWith(label *html.Node, relativeNow *time.Time, anyBar bool) *OllamaCloudUsageWindow {
	if label == nil {
		return nil
	}
	var candidate *OllamaCloudUsageWindow
	block := label
	for depth := 0; block != nil && depth < 6; depth, block = depth+1, block.Parent {
		text := normalizedNodeText(block)
		if len(text) > 600 {
			break
		}
		percent, ok := ollamaUsagePercentFromText(text)
		if !ok {
			percent, ok = ollamaUsagePercentFromTrack(block)
		}
		if !ok && anyBar {
			percent, ok = ollamaUsagePercentFromAnyBar(block)
		}
		if !ok {
			continue
		}
		if strings.Contains(strings.ToLower(text), "remaining") && !strings.Contains(strings.ToLower(text), "used") {
			percent = 100 - percent
		}
		window := &OllamaCloudUsageWindow{UsedPercent: percent}
		window.ResetAt = timeElementValue(block)
		if reset := ollamaResetPattern.FindStringSubmatch(text); len(reset) == 2 {
			window.ResetText = strings.TrimSpace(reset[1])
			if window.ResetAt == nil {
				window.ResetAt = parseOllamaResetTime(window.ResetText)
			}
		}
		if relativeNow != nil {
			applyOllamaRefillReset(window, text, *relativeNow)
		}
		if candidate == nil {
			candidate = window
		}
		if window.ResetAt != nil || window.ResetText != "" {
			return window
		}
	}
	return candidate
}

func valueBesideLabel(root *html.Node, aliases []string, maxLen int) string {
	label := findLabelElement(root, aliases)
	if label == nil {
		return ""
	}
	if sibling := nextElementSibling(label); sibling != nil {
		value := strings.Trim(normalizedNodeText(sibling), ":-| ")
		if value != "" && len(value) <= maxLen && !strings.EqualFold(value, "manage") {
			return value
		}
	}
	for depth, block := 0, label.Parent; block != nil && depth < 4; depth, block = depth+1, block.Parent {
		text := normalizedNodeText(block)
		if text == "" || len(text) > maxLen {
			continue
		}
		value := text
		for _, alias := range aliases {
			if index := strings.Index(strings.ToLower(value), alias); index >= 0 {
				value = strings.TrimSpace(value[:index] + " " + value[index+len(alias):])
				break
			}
		}
		value = strings.Trim(value, ":-| ")
		if value != "" && !strings.EqualFold(value, "manage") {
			return value
		}
	}
	return ""
}

func findLabelElement(root *html.Node, aliases []string) *html.Node {
	return findLabelElementFiltered(root, aliases, nil)
}

// findLabelElementFiltered 与 findLabelElement 相同，但允许调用方按小写文本排除候选。
func findLabelElementFiltered(root *html.Node, aliases []string, reject func(lowerText string) bool) *html.Node {
	var best *html.Node
	bestLen := int(^uint(0) >> 1)
	walkHTML(root, func(node *html.Node) {
		if node.Type != html.ElementNode || !isOllamaParserContainer(node.Data) {
			return
		}
		text := strings.ToLower(normalizedNodeText(node))
		if text == "" || (reject != nil && reject(text)) {
			return
		}
		for _, alias := range aliases {
			if (text == alias || strings.HasPrefix(text, alias+" ") || strings.HasPrefix(text, alias+":")) && len(text) < bestLen {
				best, bestLen = node, len(text)
			}
		}
	})
	return best
}

func parseOllamaModels(root *html.Node) []OllamaCloudUsageModel {
	models := make([]OllamaCloudUsageModel, 0)
	seen := make(map[string]struct{})
	appendModel := func(node *html.Node, modelValue, requestsValue string) {
		model := strings.TrimSpace(modelValue)
		requests, ok := parseOllamaRequestCount(requestsValue)
		window := ollamaModelWindow(node)
		if !ok || model == "" || len(model) > 128 || window == "" {
			return
		}
		key := model + "\x00" + string(window)
		if _, duplicate := seen[key]; duplicate {
			return
		}
		seen[key] = struct{}{}
		models = append(models, OllamaCloudUsageModel{Model: model, Window: window, Requests: requests})
	}

	walkHTML(root, func(node *html.Node) {
		if node.Type != html.ElementNode {
			return
		}
		if _, segment := htmlAttribute(node, "data-usage-segment"); !segment {
			return
		}
		model, modelOK := htmlAttributeInSubtree(node, "data-model")
		requests, requestsOK := htmlAttributeInSubtree(node, "data-requests")
		if modelOK && requestsOK {
			appendModel(node, model, requests)
		}
	})

	// Older settings variants may expose the same narrow attributes without the
	// segment marker. Do not infer request counts from percentages or limits.
	if len(models) == 0 {
		walkHTML(root, func(node *html.Node) {
			if node.Type != html.ElementNode {
				return
			}
			model, modelOK := htmlAttribute(node, "data-model")
			requests, requestsOK := htmlAttribute(node, "data-requests")
			if modelOK && requestsOK {
				appendModel(node, model, requests)
			}
		})
	}

	if len(models) == 0 {
		heading := findLabelElement(root, []string{"models", "available models"})
		for depth, block := 0, parentNode(heading); block != nil && depth < 4; depth, block = depth+1, block.Parent {
			walkHTML(block, func(node *html.Node) {
				if node.Type != html.ElementNode || (node.Data != "li" && node.Data != "code") {
					return
				}
				match := ollamaModelFallbackPattern.FindStringSubmatch(strings.TrimSpace(normalizedNodeText(node)))
				if len(match) == 3 {
					appendModel(node, match[1], match[2])
				}
			})
			if len(models) > 0 {
				break
			}
		}
	}

	sort.Slice(models, func(i, j int) bool {
		if models[i].Window != models[j].Window {
			return models[i].Window < models[j].Window
		}
		return models[i].Model < models[j].Model
	})
	if len(models) > 100 {
		models = models[:100]
	}
	return models
}

func ollamaModelWindow(node *html.Node) OllamaCloudUsageModelWindow {
	for block := parentNode(node); block != nil; block = block.Parent {
		if value, ok := htmlAttribute(block, "data-usage-window"); ok {
			normalized := strings.ToLower(strings.NewReplacer("-", " ", "_", " ").Replace(value))
			if containsAny(normalized, "five hour", "5 hour", "5h", "session") {
				return OllamaCloudUsageModelWindowFiveHour
			}
			if containsAny(normalized, "seven day", "7 day", "7d", "weekly") {
				return OllamaCloudUsageModelWindowSevenDay
			}
			if containsAny(normalized, "monthly", "month") {
				return OllamaCloudUsageModelWindowMonthly
			}
		}
		text := strings.ToLower(normalizedNodeText(block))
		fiveHour := containsAny(text, ollamaFiveHourUsageAliases...)
		sevenDay := containsAny(text, ollamaSevenDayUsageAliases...)
		if fiveHour && !sevenDay {
			return OllamaCloudUsageModelWindowFiveHour
		}
		if sevenDay && !fiveHour {
			return OllamaCloudUsageModelWindowSevenDay
		}
		// 新版页面没有 5 小时 / 7 天窗口，进度条旁的模型清单标题是「Models used this month」。
		if !fiveHour && !sevenDay && containsAny(text, "models used this month", "monthly credits used") {
			return OllamaCloudUsageModelWindowMonthly
		}
	}
	return ""
}

func parseOllamaRequestCount(value string) (int64, bool) {
	value = strings.ReplaceAll(strings.TrimSpace(value), ",", "")
	requests, err := strconv.ParseInt(value, 10, 64)
	return requests, err == nil && requests >= 0
}

func parentNode(node *html.Node) *html.Node {
	if node == nil {
		return nil
	}
	return node.Parent
}

func nextElementSibling(node *html.Node) *html.Node {
	if node == nil {
		return nil
	}
	for sibling := node.NextSibling; sibling != nil; sibling = sibling.NextSibling {
		if sibling.Type == html.ElementNode {
			return sibling
		}
	}
	return nil
}

func timeElementValue(root *html.Node) *time.Time {
	var parsed *time.Time
	walkHTML(root, func(node *html.Node) {
		if parsed != nil || node.Type != html.ElementNode {
			return
		}
		if node.Data == "time" {
			if value, ok := htmlAttribute(node, "datetime"); ok {
				parsed = parseOllamaResetTime(value)
			}
			if parsed != nil {
				return
			}
		}
		if node.Data == "local-time" || htmlClassToken(node, "local-time") {
			if value, ok := htmlAttribute(node, "data-time"); ok {
				parsed = parseOllamaResetTime(value)
			}
		}
	})
	return parsed
}

func ollamaUsagePercentFromText(value string) (float64, bool) {
	match := ollamaUsagePercentPattern.FindStringSubmatch(value)
	if len(match) != 2 {
		return 0, false
	}
	percent, err := strconv.ParseFloat(match[1], 64)
	return percent, err == nil && percent >= 0 && percent <= 100
}

func ollamaUsagePercentFromTrack(root *html.Node) (float64, bool) {
	var percent float64
	var found bool
	walkHTML(root, func(node *html.Node) {
		if found || node.Type != html.ElementNode {
			return
		}
		if _, ok := htmlAttribute(node, "data-usage-track"); !ok {
			return
		}
		var segmentTotal float64
		var segmentFound bool
		walkHTML(node, func(segment *html.Node) {
			if segment.Type != html.ElementNode {
				return
			}
			if _, ok := htmlAttribute(segment, "data-usage-segment"); !ok {
				return
			}
			if value, ok := cssWidthPercent(segment); ok {
				segmentTotal += value
				segmentFound = true
			}
		})
		if segmentFound && segmentTotal >= 0 && segmentTotal <= 100 {
			percent, found = segmentTotal, true
			return
		}
		walkHTML(node, func(child *html.Node) {
			if found || child.Type != html.ElementNode {
				return
			}
			if value, ok := cssWidthPercent(child); ok {
				percent, found = value, true
			}
		})
	})
	return percent, found
}

func cssWidthPercent(node *html.Node) (float64, bool) {
	style, ok := htmlAttribute(node, "style")
	if !ok {
		return 0, false
	}
	match := ollamaUsageWidthPattern.FindStringSubmatch(style)
	if len(match) != 2 {
		return 0, false
	}
	percent, err := strconv.ParseFloat(match[1], 64)
	return percent, err == nil && percent >= 0 && percent <= 100
}

func htmlAttribute(node *html.Node, key string) (string, bool) {
	if node == nil {
		return "", false
	}
	for _, attr := range node.Attr {
		if strings.EqualFold(attr.Key, key) {
			return attr.Val, true
		}
	}
	return "", false
}

func htmlAttributeInSubtree(root *html.Node, key string) (string, bool) {
	var value string
	var found bool
	walkHTML(root, func(node *html.Node) {
		if found || node.Type != html.ElementNode {
			return
		}
		value, found = htmlAttribute(node, key)
	})
	return value, found
}

func htmlClassToken(node *html.Node, token string) bool {
	value, ok := htmlAttribute(node, "class")
	if !ok {
		return false
	}
	for _, className := range strings.Fields(value) {
		if className == token {
			return true
		}
	}
	return false
}

func parseOllamaResetTime(value string) *time.Time {
	value = strings.TrimSpace(value)
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339, "Jan 2, 2006 3:04 PM MST", "January 2, 2006 3:04 PM MST"} {
		if parsed, err := time.Parse(layout, value); err == nil && !parsed.IsZero() {
			parsed = parsed.UTC()
			return &parsed
		}
	}
	return nil
}

func normalizedNodeText(node *html.Node) string {
	if node == nil {
		return ""
	}
	var parts []string
	var collect func(*html.Node)
	collect = func(current *html.Node) {
		if current.Type == html.ElementNode && (current.Data == "script" || current.Data == "style" || current.Data == "noscript") {
			return
		}
		if current.Type == html.TextNode {
			if value := strings.TrimSpace(current.Data); value != "" {
				parts = append(parts, value)
			}
		}
		for child := current.FirstChild; child != nil; child = child.NextSibling {
			collect(child)
		}
	}
	collect(node)
	return strings.Join(strings.Fields(strings.Join(parts, "\n")), " ")
}

func walkHTML(node *html.Node, visit func(*html.Node)) {
	if node == nil {
		return
	}
	visit(node)
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		walkHTML(child, visit)
	}
}

func isOllamaParserContainer(tag string) bool {
	switch tag {
	case "div", "section", "article", "li", "p", "span", "dt", "dd", "h1", "h2", "h3", "h4":
		return true
	default:
		return false
	}
}

func containsAny(value string, candidates ...string) bool {
	for _, candidate := range candidates {
		if strings.Contains(value, candidate) {
			return true
		}
	}
	return false
}

// parseOllamaPlanFromUsageCredits 读取「Usage credits」标题旁的套餐徽章（Free、Pro、Max 等）。
// 徽章可能是标题的兄弟元素、标题同一容器内的文字，或需要从父容器文本里扣掉标题得到，
// 依次尝试，每个候选都要经过 cleanOllamaPlanCandidate 校验，取不到返回空串。
func parseOllamaPlanFromUsageCredits(root *html.Node) string {
	label := findLabelElement(root, []string{"usage credits"})
	if label == nil {
		return ""
	}
	const alias = "usage credits"
	afterAlias := func(text string) string {
		if index := strings.Index(strings.ToLower(text), alias); index >= 0 {
			return text[index+len(alias):]
		}
		return text
	}
	if plan := cleanOllamaPlanCandidate(afterAlias(normalizedNodeText(label))); plan != "" {
		return plan
	}
	if plan := cleanOllamaPlanCandidate(normalizedNodeText(nextElementSibling(label))); plan != "" {
		return plan
	}
	for depth, block := 0, label.Parent; block != nil && depth < 3; depth, block = depth+1, block.Parent {
		text := normalizedNodeText(block)
		if len(text) > 200 {
			break
		}
		if plan := cleanOllamaPlanCandidate(afterAlias(text)); plan != "" {
			return plan
		}
	}
	return ""
}

// cleanOllamaPlanCandidate 把候选文字截断到金额、按钮文案之前，并要求结果像一个
// 简短的套餐名（至多两个词、不含虚词）；不像则返回空串，交给下一个候选。
func cleanOllamaPlanCandidate(value string) string {
	value = strings.TrimSpace(value)
	if loc := ollamaPlanStopPattern.FindStringIndex(value); loc != nil {
		value = value[:loc[0]]
	}
	value = strings.Trim(value, ":-|·• \t")
	words := strings.Fields(value)
	if len(words) == 0 || len(words) > 2 || len(value) > 24 || strings.Contains(value, ":") {
		return ""
	}
	for _, word := range words {
		if _, sentence := ollamaPlanSentenceWords[strings.ToLower(word)]; sentence {
			return ""
		}
	}
	return strings.Join(words, " ")
}

// parseOllamaFreeModels 解析免费计划页面「Free usage credits can be used with the
// following cloud models: …」后面的模型清单。清单可能与说明句同属一个元素，也可能在
// 紧随其后的兄弟元素里；遇到不像模型名的词即停止。结果去重并限制 100 个。
func parseOllamaFreeModels(root *html.Node) []string {
	var holder *html.Node
	holderLen := int(^uint(0) >> 1)
	walkHTML(root, func(node *html.Node) {
		if node.Type != html.ElementNode || !isOllamaParserContainer(node.Data) {
			return
		}
		text := strings.ToLower(normalizedNodeText(node))
		if strings.Contains(text, "following cloud models") && len(text) < holderLen {
			holder, holderLen = node, len(text)
		}
	})
	if holder == nil {
		return nil
	}
	text := normalizedNodeText(holder)
	index := strings.Index(strings.ToLower(text), "following cloud models")
	list := strings.TrimLeft(text[index+len("following cloud models"):], ": ")
	if strings.TrimSpace(list) == "" {
		list = normalizedNodeText(nextElementSibling(holder))
	}
	models := make([]string, 0)
	seen := make(map[string]struct{})
	for _, token := range strings.FieldsFunc(list, func(r rune) bool { return r == ',' || r == ' ' || r == '\n' || r == '\t' }) {
		token = strings.Trim(token, ".;…")
		if token == "" {
			break
		}
		if _, stop := ollamaFreeModelStopWords[strings.ToLower(token)]; stop {
			if len(models) > 0 {
				continue
			}
			break
		}
		if !ollamaFreeModelPattern.MatchString(token) {
			break
		}
		if _, duplicate := seen[token]; duplicate {
			continue
		}
		seen[token] = struct{}{}
		models = append(models, token)
		if len(models) >= 100 {
			break
		}
	}
	if len(models) == 0 {
		return nil
	}
	return models
}

// ollamaUsagePercentFromAnyBar 取块内第一个能读出百分比的进度条：支持 style 的 width
// 百分比、Tailwind 的 w-[x%] 类、progress/meter 元素的 value/max，以及 aria-valuenow。
// 先序遍历，外层条的宽度先于内层分段（分段宽度是相对已填充部分的比例，不能当总进度）。
// 仅供免费额度、月度额度窗口在没有文本百分比、也没有 data-usage-track 标记时兜底。
func ollamaUsagePercentFromAnyBar(root *html.Node) (float64, bool) {
	var percent float64
	var found bool
	walkHTML(root, func(node *html.Node) {
		if found || node.Type != html.ElementNode {
			return
		}
		if value, ok := cssWidthPercent(node); ok {
			percent, found = value, true
			return
		}
		if class, ok := htmlAttribute(node, "class"); ok {
			if match := ollamaBarClassPattern.FindStringSubmatch(class); len(match) == 2 {
				if value, err := strconv.ParseFloat(match[1], 64); err == nil && value >= 0 && value <= 100 {
					percent, found = value, true
					return
				}
			}
		}
		current, hasNow := htmlAttribute(node, "aria-valuenow")
		if !hasNow && (node.Data == "progress" || node.Data == "meter") {
			current, hasNow = htmlAttribute(node, "value")
		}
		if !hasNow {
			return
		}
		now, err := strconv.ParseFloat(strings.TrimSpace(current), 64)
		if err != nil {
			return
		}
		maximum := 100.0
		maxAttr, hasMax := htmlAttribute(node, "aria-valuemax")
		if !hasMax {
			maxAttr, hasMax = htmlAttribute(node, "max")
		}
		if hasMax {
			if parsed, parseErr := strconv.ParseFloat(strings.TrimSpace(maxAttr), 64); parseErr == nil && parsed > 0 {
				maximum = parsed
			}
		}
		if value := now / maximum * 100; value >= 0 && value <= 100 {
			percent, found = value, true
		}
	})
	return percent, found
}

// applyOllamaRefillReset 给新版窗口补重置信息：文案取块文本里的「Refills in 2 weeks」
// （去掉前缀 Refills 只留 in 2 weeks）；窗口还没有精确时间时，按相对时间换算近似值。
func applyOllamaRefillReset(window *OllamaCloudUsageWindow, text string, now time.Time) {
	if match := ollamaRefillPattern.FindStringSubmatch(text); len(match) == 2 && window.ResetText == "" {
		window.ResetText = strings.TrimSpace(match[1])
	}
	if window.ResetAt == nil && window.ResetText != "" {
		if window.ResetAt = parseOllamaRelativeTime(window.ResetText, now); window.ResetAt != nil {
			window.ResetApprox = true
		}
	}
}

// parseOllamaRelativeTime 把「in 4 weeks」「in about 2 hours」换算成相对 now 的近似时间。
// 月按 30 天、年按 365 天，只用于展示重置倒计时，不参与任何计费判断；无法解析返回 nil。
func parseOllamaRelativeTime(text string, now time.Time) *time.Time {
	match := ollamaRelativePattern.FindStringSubmatch(text)
	if len(match) != 3 {
		return nil
	}
	count := 1
	if quantity := strings.ToLower(match[1]); quantity != "a" && quantity != "an" && quantity != "one" {
		parsed, err := strconv.Atoi(quantity)
		if err != nil || parsed <= 0 || parsed > 1000 {
			return nil
		}
		count = parsed
	}
	unit := map[string]time.Duration{
		"minute": time.Minute, "hour": time.Hour, "day": 24 * time.Hour,
		"week": 7 * 24 * time.Hour, "month": 30 * 24 * time.Hour, "year": 365 * 24 * time.Hour,
	}[strings.ToLower(match[2])]
	if unit == 0 {
		return nil
	}
	result := now.UTC().Add(time.Duration(count) * unit)
	return &result
}

// firstOllamaMoney 取文本里第一个带 $ 或 USD 标记的金额，去掉内部空白（USD $9.5 → USD$9.5）。
func firstOllamaMoney(text string) string {
	return strings.Join(strings.Fields(ollamaMoneyPattern.FindString(text)), "")
}

// parseOllamaMoneyAmount 把「$60」「<$0.01」「USD$9.50」转成数值；无法解析返回 false。
func parseOllamaMoneyAmount(value string) (float64, bool) {
	var digits strings.Builder
	for _, char := range value {
		if (char >= '0' && char <= '9') || char == '.' || char == '-' {
			_, _ = digits.WriteRune(char)
		}
	}
	amount, err := strconv.ParseFloat(digits.String(), 64)
	return amount, err == nil
}

// ollamaAddedCreditsNode 返回 id 为 extra-usage-balance 的元素。真实页面里它是
// 「追加额度」（Added credits）：免费计划没有月度额度，它恰好等于页面主金额；
// 付费计划里它只是追加充值的部分，不是合计。
func ollamaAddedCreditsNode(root *html.Node) *html.Node {
	var found *html.Node
	walkHTML(root, func(node *html.Node) {
		if found != nil || node.Type != html.ElementNode {
			return
		}
		if id, ok := htmlAttribute(node, "id"); ok && id == "extra-usage-balance" {
			found = node
		}
	})
	return found
}

// parseOllamaAddedCredits 读取追加额度金额原文，没有该元素返回空串。
func parseOllamaAddedCredits(root *html.Node) string {
	node := ollamaAddedCreditsNode(root)
	if node == nil {
		return ""
	}
	return firstOllamaMoney(normalizedNodeText(node))
}

// parseOllamaTotalCredits 读取余额「合计」。优先级：
//  1. 页面文本里明确写着 Total 的金额（提示框若服务端渲染则有）；
//  2. 「Usage credits」标题向上最多三层、文本不超过 300 字符的块里，扣掉追加额度元素后的第一个
//     带货币标记的金额（即标题行右侧的主金额）；
//  3. 都没有时回退到追加额度元素（免费计划里它就是主金额）。
func parseOllamaTotalCredits(root *html.Node, pageText string) string {
	// 付费计划页面标题行右侧的主金额有专门的 id usage-credits-balance；
	// 提示框的「Total」行与它相同，作为第二来源。
	var byID string
	walkHTML(root, func(node *html.Node) {
		if byID != "" || node.Type != html.ElementNode {
			return
		}
		if id, ok := htmlAttribute(node, "id"); ok && id == "usage-credits-balance" {
			byID = firstOllamaMoney(normalizedNodeText(node))
		}
	})
	if byID != "" {
		return byID
	}
	if totalLabel := findLabelElement(root, []string{"total"}); totalLabel != nil {
		if sibling := nextElementSibling(totalLabel); sibling != nil {
			if money := firstOllamaMoney(normalizedNodeText(sibling)); money != "" {
				return money
			}
		}
	}
	if match := ollamaTotalMoneyPattern.FindStringSubmatch(pageText); len(match) == 2 {
		if money := firstOllamaMoney(match[1]); money != "" {
			return money
		}
	}
	added := ollamaAddedCreditsNode(root)
	if label := findLabelElement(root, []string{"usage credits"}); label != nil {
		for depth, block := 0, label; block != nil && depth < 3; depth, block = depth+1, block.Parent {
			text := ollamaTextWithout(block, added)
			if len(text) > 300 {
				break
			}
			lower := strings.ToLower(text)
			if index := strings.Index(lower, "usage credits"); index >= 0 {
				text, lower = text[index+len("usage credits"):], lower[index+len("usage credits"):]
			}
			// 标题行之后的其他区块（自动充值、月度已用、用量明细、最近请求）里的金额不是合计。
			for _, marker := range []string{"auto-reload", "monthly credits used", "usage breakdown", "recent requests", "free usage"} {
				if index := strings.Index(lower, marker); index >= 0 {
					text, lower = text[:index], lower[:index]
				}
			}
			if money := firstOllamaMoney(text); money != "" {
				return money
			}
		}
	}
	if added != nil {
		return firstOllamaMoney(normalizedNodeText(added))
	}
	return ""
}

// ollamaTextWithout 与 normalizedNodeText 相同，但整个跳过 skip 子树。
func ollamaTextWithout(node, skip *html.Node) string {
	if skip == nil {
		return normalizedNodeText(node)
	}
	var parts []string
	var collect func(*html.Node)
	collect = func(current *html.Node) {
		if current == skip {
			return
		}
		if current.Type == html.ElementNode && (current.Data == "script" || current.Data == "style" || current.Data == "noscript") {
			return
		}
		if current.Type == html.TextNode {
			if value := strings.TrimSpace(current.Data); value != "" {
				parts = append(parts, value)
			}
		}
		for child := current.FirstChild; child != nil; child = child.NextSibling {
			collect(child)
		}
	}
	collect(node)
	return strings.Join(strings.Fields(strings.Join(parts, "\n")), " ")
}

// ollamaFutureResetTime 在页面里找额度重置的精确时间：带时间属性（data-time、datetime、
// data-reset-at 等）、晚于 now 的元素，排除「Recent requests」表格里的历史时间，取最早的一个。
// 新版页面的「refill in …」文案可能由脚本生成，服务端 HTML 里只剩这个时间属性。
func ollamaFutureResetTime(root *html.Node, now time.Time) *time.Time {
	var best *time.Time
	var visit func(node *html.Node, inRecent bool)
	visit = func(node *html.Node, inRecent bool) {
		if node.Type == html.ElementNode {
			switch node.Data {
			case "table", "tbody", "tr", "td":
				inRecent = true
			}
			if id, ok := htmlAttribute(node, "id"); ok && strings.HasPrefix(id, "recent-requests") {
				inRecent = true
			}
			if !inRecent {
				for _, key := range []string{"data-time", "datetime", "data-reset-at", "data-resets-at", "data-refill-at"} {
					value, ok := htmlAttribute(node, key)
					if !ok {
						continue
					}
					if parsed := parseOllamaResetTime(value); parsed != nil && parsed.After(now) && (best == nil || parsed.Before(*best)) {
						best = parsed
					}
				}
			}
		}
		for child := node.FirstChild; child != nil; child = child.NextSibling {
			visit(child, inRecent)
		}
	}
	visit(root, false)
	return best
}

// parseOllamaMonthlyWindow 解析新版付费计划的「Monthly credits used」月度额度：
// 已用金额文本（行右侧的 $x）、进度条百分比、「Monthly credits refill in 4 weeks」重置文案。
// 进度条百分比读不到时：已用为 0 则记 0；否则按 已用/(已用+余额) 近似，仅用于画进度条。
func parseOllamaMonthlyWindow(root *html.Node, pageText, balance string, now time.Time) *OllamaCloudUsageWindow {
	label := findLabelElement(root, ollamaMonthlyUsageAliases)
	if label == nil {
		return nil
	}
	labelText := normalizedNodeText(label)
	alias := ""
	for _, candidate := range ollamaMonthlyUsageAliases {
		if strings.HasPrefix(strings.ToLower(labelText), candidate) {
			alias = candidate
			break
		}
	}
	// 已用金额：标题自身文字之后、紧邻的兄弟元素、再向上三层父块（扣掉标题）依次尝试。
	candidates := []string{labelText[len(alias):], normalizedNodeText(nextElementSibling(label))}
	for depth, block := 0, label.Parent; block != nil && depth < 3; depth, block = depth+1, block.Parent {
		text := normalizedNodeText(block)
		if len(text) > 200 {
			break
		}
		if index := strings.Index(strings.ToLower(text), alias); index >= 0 {
			text = text[index+len(alias):]
		}
		candidates = append(candidates, text)
	}
	usedText := ""
	for _, candidate := range candidates {
		if usedText = firstOllamaMoney(candidate); usedText != "" {
			break
		}
	}

	var percent float64
	percentFound := false
	for depth, block := 0, label; block != nil && depth < 6; depth, block = depth+1, block.Parent {
		text := normalizedNodeText(block)
		if len(text) > 600 {
			break
		}
		if value, ok := ollamaUsagePercentFromText(text); ok {
			percent, percentFound = value, true
			break
		}
		if value, ok := ollamaUsagePercentFromAnyBar(block); ok {
			percent, percentFound = value, true
			break
		}
	}
	limitText := parseOllamaMonthlyLimit(root)
	if !percentFound && usedText != "" {
		if used, ok := parseOllamaMoneyAmount(usedText); ok {
			if limit, limitOK := parseOllamaMoneyAmount(limitText); limitOK && limit > 0 && used >= 0 && used <= limit {
				// 月度额度总额已知：直接按 已用/总额 计算。
				percent, percentFound = used/limit*100, true
			} else if used <= 0 {
				percent, percentFound = 0, true
			} else if remaining, ok := parseOllamaMoneyAmount(balance); ok && remaining >= 0 {
				percent, percentFound = used/(used+remaining)*100, true
			}
		}
	}
	if !percentFound && usedText == "" {
		return nil
	}
	window := &OllamaCloudUsageWindow{UsedPercent: percent, UsedText: usedText, LimitText: limitText}
	// 重置时间，按可靠程度依次取：
	//  1. 页面句子里的精确日期「Refills to $60 on November 24, 2026」（只有日期，按当天中午 UTC 记）；
	//  2. 「Refills in …」文案所在最小元素旁的未来 data-time；
	//  3. 页面里最早的未来时间属性（排除最近请求表）；
	//  4. 按「in 4 weeks」相对推算的约数（标 reset_approx）。
	// ResetText 保留页面可见的相对文案，没有时保留日期原文。
	if match := ollamaRefillPattern.FindStringSubmatch(pageText); len(match) == 2 {
		window.ResetText = strings.TrimSpace(match[1])
	}
	if match := ollamaRefillDatePattern.FindStringSubmatch(pageText); len(match) == 2 {
		if date := parseOllamaTextDate(match[1]); date != nil {
			window.ResetAt = date
			if window.ResetText == "" {
				window.ResetText = strings.TrimSpace(match[1])
			}
		}
	}
	if window.ResetAt == nil {
		var best *html.Node
		bestLen := int(^uint(0) >> 1)
		walkHTML(root, func(node *html.Node) {
			if node.Type != html.ElementNode {
				return
			}
			text := normalizedNodeText(node)
			if len(text) < bestLen && ollamaRefillPattern.MatchString(text) {
				best, bestLen = node, len(text)
			}
		})
		if best != nil {
			window.ResetAt = ollamaDataTimeNear(best, now)
		}
	}
	if window.ResetAt == nil {
		window.ResetAt = ollamaFutureResetTime(root, now)
	}
	if window.ResetAt == nil && window.ResetText != "" {
		if window.ResetAt = parseOllamaRelativeTime(window.ResetText, now); window.ResetAt != nil {
			window.ResetApprox = true
		}
	}
	return window
}

// parseOllamaRecentRequests 解析新版页面「Recent requests」列表：每行三列，
// 依次是相对时间（28 minutes ago，td 的 data-time 带精确时间）、模型名、费用（<$0.01）。
// 既支持每行一个容器（tr 或三个子元素的 div），也支持单元格平铺在同一网格里按三个一组。
// 「Load more」之类的单格行、不是三列的行都会被跳过。
func parseOllamaRecentRequests(root *html.Node) []OllamaCloudUsageRecentRequest {
	heading := findLabelElement(root, []string{"recent requests"})
	if heading == nil {
		return nil
	}
	requests := make([]OllamaCloudUsageRecentRequest, 0)
	appendRow := func(cells []*html.Node) {
		timeText := normalizedNodeText(cells[0])
		model := normalizedNodeText(cells[1])
		cost := normalizedNodeText(cells[2])
		if !ollamaRecentTimePattern.MatchString(timeText) || model == "" || len(model) > 128 || len(cost) > 32 || len(requests) >= 50 {
			return
		}
		row := OllamaCloudUsageRecentRequest{Time: timeText, Model: model, Cost: cost}
		if value, ok := htmlAttribute(cells[0], "data-time"); ok {
			row.At = parseOllamaResetTime(value)
		}
		requests = append(requests, row)
	}
	for depth, block := 0, parentNode(heading); block != nil && depth < 4; depth, block = depth+1, block.Parent {
		walkHTML(block, func(node *html.Node) {
			if node.Type != html.ElementNode || node == heading {
				return
			}
			var cells []*html.Node
			for child := node.FirstChild; child != nil; child = child.NextSibling {
				if child.Type == html.ElementNode {
					cells = append(cells, child)
				}
			}
			switch {
			case len(cells) == 3:
				appendRow(cells)
			case len(cells) > 3 && len(cells)%3 == 0 &&
				ollamaRecentTimePattern.MatchString(normalizedNodeText(cells[0])) &&
				ollamaRecentTimePattern.MatchString(normalizedNodeText(cells[3])):
				for index := 0; index+2 < len(cells); index += 3 {
					appendRow(cells[index : index+3])
				}
			}
		})
		if len(requests) > 0 {
			break
		}
	}
	if len(requests) == 0 {
		return nil
	}
	return requests
}

// capitalizeOllamaPlan 把徽章文字首字母大写：真实页面里徽章源码是小写 free，
// 靠 CSS capitalize 显示成 Free，网关看不到样式，需要自己还原显示形态。
func capitalizeOllamaPlan(plan string) string {
	if plan == "" {
		return ""
	}
	words := strings.Fields(plan)
	for index, word := range words {
		words[index] = strings.ToUpper(word[:1]) + word[1:]
	}
	return strings.Join(words, " ")
}

// parseOllamaAccountEmail 读取设置页页头账号菜单（id=settings-account-menu）里的登录邮箱。
// 只认该菜单，其次认带 email 语义的输入框，不扫描全页文本，避免取到页脚的客服邮箱。
func parseOllamaAccountEmail(root *html.Node) string {
	var email string
	walkHTML(root, func(node *html.Node) {
		if email != "" || node.Type != html.ElementNode {
			return
		}
		if id, ok := htmlAttribute(node, "id"); ok && id == "settings-account-menu" {
			email = ollamaEmailPattern.FindString(normalizedNodeText(node))
		}
	})
	if email == "" {
		walkHTML(root, func(node *html.Node) {
			if email != "" || node.Type != html.ElementNode || node.Data != "input" {
				return
			}
			kind, _ := htmlAttribute(node, "type")
			name, _ := htmlAttribute(node, "name")
			if !strings.EqualFold(kind, "email") && !strings.EqualFold(name, "email") {
				return
			}
			if value, ok := htmlAttribute(node, "value"); ok {
				email = ollamaEmailPattern.FindString(value)
			}
		})
	}
	if len(email) > 254 {
		return ""
	}
	return email
}

// ollamaDataTimeNear 找「Refills in …」文案元素旁的精确时间：先看元素自身与子孙（排除最近请求表），
// 再看最多两层父元素自身的时间属性；只认晚于 now 的时间（过去的时间不可能是下次重置）。
// 都没有返回 nil，调用方继续找页面里的未来时间，再按文案相对换算兜底。
func ollamaDataTimeNear(node *html.Node, now time.Time) *time.Time {
	if parsed := ollamaFutureResetTime(node, now); parsed != nil {
		return parsed
	}
	for depth, parent := 0, node.Parent; parent != nil && depth < 2; depth, parent = depth+1, parent.Parent {
		for _, key := range []string{"data-time", "datetime", "data-reset-at", "data-resets-at", "data-refill-at"} {
			if value, ok := htmlAttribute(parent, key); ok {
				if parsed := parseOllamaResetTime(value); parsed != nil && parsed.After(now) {
					return parsed
				}
			}
		}
	}
	return nil
}

// parseOllamaTextDate 解析「November 24, 2026」「Nov 24 2026」「November 24th, 2026」这类英文日期，
// 只有日期没有时刻：按当天中午 UTC 记，这样本地时区在 UTC-12 到 UTC+11 之间显示的日期都是同一天。
func parseOllamaTextDate(value string) *time.Time {
	value = strings.NewReplacer(",", " ", ".", " ").Replace(value)
	fields := strings.Fields(value)
	if len(fields) != 3 {
		return nil
	}
	day := strings.TrimRight(strings.ToLower(fields[1]), "stndrh")
	normalized := fields[0] + " " + day + " " + fields[2]
	for _, layout := range []string{"January 2 2006", "Jan 2 2006"} {
		if parsed, err := time.Parse(layout, normalized); err == nil {
			noon := time.Date(parsed.Year(), parsed.Month(), parsed.Day(), 12, 0, 0, 0, time.UTC)
			return &noon
		}
	}
	return nil
}

// parseOllamaMonthlyLimit 读取月度额度总额（如 $60）：优先取提示框「Monthly credits」行右侧金额，
// 其次取进度条 aria-label 里「of $60」。读不到返回空串。
func parseOllamaMonthlyLimit(root *html.Node) string {
	label := findLabelElementFiltered(root, []string{"monthly credits"}, func(lowerText string) bool {
		return strings.HasPrefix(lowerText, "monthly credits used") || strings.HasPrefix(lowerText, "monthly credits refill")
	})
	if label != nil {
		if sibling := nextElementSibling(label); sibling != nil {
			if money := firstOllamaMoney(normalizedNodeText(sibling)); money != "" {
				return money
			}
		}
	}
	var limit string
	walkHTML(root, func(node *html.Node) {
		if limit != "" || node.Type != html.ElementNode {
			return
		}
		if _, ok := htmlAttribute(node, "data-usage-track"); !ok {
			return
		}
		if aria, ok := htmlAttribute(node, "aria-label"); ok {
			if match := ollamaOfLimitPattern.FindStringSubmatch(aria); len(match) == 2 {
				limit = firstOllamaMoney(match[1])
			}
		}
	})
	return limit
}
