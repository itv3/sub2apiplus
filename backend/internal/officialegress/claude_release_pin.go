package officialegress

import (
	"errors"
	"fmt"
)

// Claude 历史锚点与当前 selector 解耦。
//
// ClaudeFWG* 这组历史名称（版本与 Profile、Wire、Release、Bundle 摘要，以及 loadClaudeFWGProfile、
// loadClaudeFWGWire）被 FW-G、FW-H 的历史收据和测试当作固定事实引用。它们原先从 validation_candidate
// selector 派生，候选 selector 一换到新 Release，这些历史比对就整批失效。现在它们一律钉住
// claudeFWGHistoricalReleaseDigest 指向的那条不可变 Release：候选 selector 怎么换，历史锚点都不动。
// 运行时需要跟随 selector 的地方（候选与生产运行时、路由目录）仍按各自的 selector 解析；本文件只按摘要
// 取 Release，不授予、也不代替任何运行时选择权。

// claudeFWGHistoricalReleaseDigest 是 FW-G 三模型能力 Release 的内容寻址摘要，FW-H 生产迁移及其后的
// 历史收据引用的都是这一条。目录里的 Release 只追加不删除，这一条必须一直保留。
const claudeFWGHistoricalReleaseDigest = "48586f47e0abcde9ea357c08c1465253eaa296862153a89c20e4cf0f1e6f52f8"

// ReleaseByDigest 按 Release 摘要返回目录里已登记的不可变 Release。
//
// 摘要先按 selector 解析的同一判据校验格式。目录里的每条 Release 只经 loadClaudeCatalogRelease 写入：
// 内容寻址读取 Profile 与 Wire、身份交叉校验、复算 ReleaseArtifact 与 ReleaseBundle 摘要；selector 也只从
// 这张表里取 Release。因此按摘要取到的 Release 与 selector 取到的经过同一套校验。本方法只回答"这条
// Release 是什么"，不改变、也不代替任何 selector。
func (c ClaudeReleaseCatalog) ReleaseByDigest(releaseDigest string) (ResolvedClaudeRelease, error) {
	if !receiptSHA256(releaseDigest) {
		return ResolvedClaudeRelease{}, errors.New("Claude Release 摘要非法")
	}
	release, ok := c.releases[releaseDigest]
	if !ok {
		return ResolvedClaudeRelease{}, fmt.Errorf("Claude ReleaseCatalog 没有 Release：%s", releaseDigest)
	}
	return release, nil
}

// claudeFWGHistoricalReleaseFrom 从给定目录取钉住的历史 Release；目录里没有它时失败关闭。
func claudeFWGHistoricalReleaseFrom(catalog ClaudeReleaseCatalog) (ResolvedClaudeRelease, error) {
	release, err := catalog.ReleaseByDigest(claudeFWGHistoricalReleaseDigest)
	if err != nil {
		return ResolvedClaudeRelease{}, fmt.Errorf("Claude FW-G 历史 Release 不可用：%w", err)
	}
	return release, nil
}

// claudeFWGHistoricalRelease 从进程默认目录取钉住的历史 Release，供包级历史名称在初始化时求值。
//
// 目录里缺这条 Release 时返回零值而不 panic，生产进程不会因为只供历史收据与测试使用的锚点而起不来。
// 零值会让历史锚点为空、让 loadClaudeFWGProfile 与 loadClaudeFWGWire 失败关闭，引用它们的测试一律变红；
// claude_release_pin_test.go 另外显式断言这条 Release 存在。
func claudeFWGHistoricalRelease() ResolvedClaudeRelease {
	release, err := claudeFWGHistoricalReleaseFrom(DefaultClaudeReleaseCatalog())
	if err != nil {
		return ResolvedClaudeRelease{}
	}
	return release
}
