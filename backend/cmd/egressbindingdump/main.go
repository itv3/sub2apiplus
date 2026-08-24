// egressbindingdump 把 egressscan 基线确定性转换为变更集 0C 的绑定目录。
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "用法: egressbindingdump <sink-baseline.json> <binding-catalog.json>")
		os.Exit(2)
	}
	raw, err := os.ReadFile(os.Args[1]) //nolint:gosec // 本地受审工具只读取操作者显式给出的基线文件路径。
	if err != nil {
		fail(err)
	}
	doc, err := bindingcontract.BuildBindingCatalogDoc(raw)
	if err != nil {
		fail(err)
	}
	out, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		fail(err)
	}
	out = append(out, '\n')
	if err := os.WriteFile(os.Args[2], out, 0o644); err != nil { //nolint:gosec // 输出路径同样由本地操作者显式指定。
		fail(err)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
