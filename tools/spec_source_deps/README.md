# 规格表 L2 依赖源码

本目录保存第二部分 L2 规则所需的最小上游源码快照，使源码证据可以离线复核。
版本、上游提交、Cargo.lock 绑定值及文件 SHA-256 统一记录在 `manifest.json`。

目录只包含被规格表直接引用的源码、包清单和许可证；校验入口为：

```bash
python3 tools/check_spec_refs.py --symbol --cfg-test
```
