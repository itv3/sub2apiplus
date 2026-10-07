// Claude 候选目录的 Go 加载核对（Claude 升级 B3-04a，本机候选链 claude_catalog_chain.py 在 backend 内的临时目录 go run）。
// 用候选源码里的加载器 LoadEmbeddedClaudeReleaseCatalog 读嵌入的 catalogdata/claude（编译时嵌入，读的就是刚写的新目录），
// 不让 Python 复刻 Release 摘要、选择器解析与画像／Wire 绑定的算法；输出两个选择器解析出的 Release 与目录摘要，由调用方
// 核对 validation_candidate 指向暂存 Release、production_active 不变。加载失败即非零退出（错误原因写标准错误）。
package main

import (
	"encoding/json"
	"fmt"
	"os"

	o "github.com/Wei-Shaw/sub2api/internal/officialegress"
)

type releaseView struct {
	Version       string `json:"version"`
	Platform      string `json:"platform"`
	ReleaseSHA256 string `json:"release_sha256"`
	ProfileSHA256 string `json:"profile_sha256"`
	WireSHA256    string `json:"wire_sha256"`
	Changeset     string `json:"changeset"`
}

func view(selection o.ResolvedClaudeReleaseSelection) releaseView {
	release := selection.Release()
	return releaseView{
		Version:       release.Version(),
		Platform:      release.Platform(),
		ReleaseSHA256: release.ReleaseDigest(),
		ProfileSHA256: release.ProfileDigest(),
		WireSHA256:    release.WireDigest(),
		Changeset:     selection.Changeset(),
	}
}

func main() {
	catalog, err := o.LoadEmbeddedClaudeReleaseCatalog()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	output := map[string]any{
		"status":               "passed",
		"catalog_sha256":       catalog.Digest(),
		"validation_candidate": view(catalog.ValidationCandidate()),
		"production_active":    view(catalog.ProductionActive()),
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
