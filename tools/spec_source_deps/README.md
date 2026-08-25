# 规格表 L2 依赖源码

本目录保存第二部分 L2 规则所需的最小上游源码快照，使源码证据可以离线复核。
0.147.0 的历史基线继续由只读 `manifest.json` 记录；0.149.1 追加为
`manifest_0_149_1.json`。同名未变化依赖可以由两份清单共同引用，升级依赖使用新的版本目录，
不得删除或覆盖历史快照。

目录只包含被规格表直接引用的源码、包清单和许可证；校验入口为：

```bash
python3 tools/check_spec_refs.py --symbol --cfg-test

python3 tools/check_spec_refs.py \
  --spec docs/CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md \
  --source-root local-analysis/sources/codex-cli-0.149.1 \
  --source-version 0.149.1 \
  --cargo-lock local-analysis/sources/codex-cli-0.149.1/codex-rs/Cargo.lock \
  --anchor-manifest tools/spec_ref_anchors_0_149_1.json \
  --dependency-manifest tools/spec_source_deps/manifest_0_149_1.json \
  --symbol --cfg-test
```
