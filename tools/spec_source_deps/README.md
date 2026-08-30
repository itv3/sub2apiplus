# 规格表 L2 依赖源码

本目录保存第二部分 L2 规则所需的最小上游源码快照，使源码证据可以离线复核。
当前 Active 规格统一由 `manifest.json` 记录。版本升级只更新这一份权威清单；逐轮生成且与其完全相同的
版本化副本不进入仓库。依赖源码目录按实际版本保留，仍被历史或当前规格引用的快照不得删除或覆盖。

目录只包含被规格表直接引用的源码、包清单和许可证；校验入口为：

```bash
python3 tools/check_spec_refs.py --symbol --cfg-test

python3 tools/check_spec_refs.py \
  --spec docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md \
  --source-root local-analysis/sources/codex-cli-0.149.1 \
  --source-version 0.149.1 \
  --cargo-lock local-analysis/sources/codex-cli-0.149.1/codex-rs/Cargo.lock \
  --anchor-manifest tools/spec_ref_anchors.json \
  --dependency-manifest tools/spec_source_deps/manifest.json \
  --symbol --cfg-test
```
