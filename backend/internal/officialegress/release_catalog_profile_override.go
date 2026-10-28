package officialegress

import (
	"errors"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// WithActiveProfileOverrideForTest 返回把 Active 发布的画像换成 doc 的目录副本，仅供测试在“目标版本画像尚未入库”时，
// 用上一版画像加规则补丁合成目标画像，驱动真实 Executor 做线上字节断言。
//
// 约束：
//   - 副本只替换 Active 发布解析出的 ProfileSpec 与 ExecutableProfile；发布图、快照目录、发布摘要与画像摘要字段原样保留，
//     所以 Persona 发布坐标校验、端点绑定目录都按原画像的身份取值（doc 必须与原画像端点集合一致，只改可选节）。
//   - 原目录与进程级默认目录不被修改；生产代码不得调用本函数。
func (c ReleaseCatalog) WithActiveProfileOverrideForTest(doc profilecontract.SnapshotDoc) (ReleaseCatalog, error) {
	if c.resolvedActive.releaseDigest == "" {
		return ReleaseCatalog{}, errors.New("ReleaseCatalog 尚未预编译 active")
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	executable, err := profilecontract.CompileExecutableProfile(spec)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	out := c
	out.resolvedActive.profile = spec
	out.resolvedActive.executable = executable
	return out, nil
}
