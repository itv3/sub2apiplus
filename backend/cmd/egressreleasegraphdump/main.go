// Command egressreleasegraphdump 把 official-client registry 的 Codex OAuth 发布图导出为 JSON。
package main

import (
	"fmt"
	"os"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func main() {
	out := "release-graph.json"
	if len(os.Args) > 1 {
		out = os.Args[1]
	}
	raw, err := officialegress.DefaultReleaseCatalog().GraphJSON()
	if err != nil {
		fmt.Fprintf(os.Stderr, "导出失败: %v\n", err)
		os.Exit(1)
	}
	if err := os.WriteFile(out, raw, 0o644); err != nil { //nolint:gosec // 导出路径由本地操作者显式指定。
		fmt.Fprintf(os.Stderr, "写入失败: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("已导出 %s（%d 字节）\n", out, len(raw))
}
