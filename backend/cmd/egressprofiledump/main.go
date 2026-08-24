// Command egressprofiledump 把 active Codex 画像导出为 JSON 文件。
//
// 契约包不得 import service（依赖规则），因此用这个工具把 snapshot 落成文件，
// 契约测试读文件做「真实 Snapshot → ProfileSpec → Snapshot」往返验证。
// 命令本身是离线 main 包，不会链接进生产 server；读取的 contract 已提升为生产只读依赖。
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func main() {
	if len(os.Args) > 2 && os.Args[1] == "-enums" {
		// 包名由调用方给出，默认与 0A 契约包一致。
		// 上一版硬编码 package contract，CI 得用 sed 打补丁——
		// 按报错提示直接重跑会生成无法编译的文件。
		pkg := "profilecontract"
		if len(os.Args) > 4 {
			pkg = os.Args[4]
		}
		if err := genEnums(os.Args[2], os.Args[3], pkg); err != nil {
			fmt.Fprintf(os.Stderr, "生成枚举失败: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("已生成 %s\n", os.Args[3])
		return
	}
	out := "profile-snapshot.json"
	if len(os.Args) > 1 {
		out = os.Args[1]
	}
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		fmt.Fprintf(os.Stderr, "导出失败: %v\n", err)
		os.Exit(1)
	}
	data, err := json.Marshal(release.Profile().ToSnapshot())
	if err != nil {
		fmt.Fprintf(os.Stderr, "序列化失败: %v\n", err)
		os.Exit(1)
	}
	data = append(data, '\n')
	if err := os.WriteFile(out, data, 0o644); err != nil { //nolint:gosec // 离线导出路径由本地操作者显式指定。
		fmt.Fprintf(os.Stderr, "写入失败: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("已导出 %s（digest=%s，%d 字节）\n", out, release.ProfileDigest(), len(data))
}
