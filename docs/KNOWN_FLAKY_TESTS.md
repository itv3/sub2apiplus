# 已知不稳定测试清单

本清单登记在 CI 或 ARM64 上出现过、单独复跑可以通过、但还没有根治的偶发失败测试。

- 任何“复跑通过”的失败都要先登记在这里再合并，不能只靠复跑。登记内容：首次出现的提交与 CI 运行、失败现象、本机复现
  尝试、疑似根因、处置方式。
- CI 再次出现同一失败时，先对照“现象”：一致的可以复跑一次，并在该条“出现记录”里追加一行；不一致的按新缺陷处理。
- 根治后把条目移到“已修复”，写明修复提交。

## 未修复

### test_heartbeat_gap_is_watchdog_abort_without_owner_kill

- 位置：Python，`tools/official_client_capture/tests/test_codex_upgrade_supervisor.py`（`SupervisorTests`）。
- 现象：模拟心跳断档后，监督器状态应在用例的等待窗口内变为 `watchdog-aborted`，偶发仍是 `running`
  （`AssertionError: 'running' != 'watchdog-aborted'`）。
- 出现记录：
  - 2026-09-25，提交 001d8955a 的 CI（run 36109428010，capture-tools job，该 job 2267 条用例共跑 1685 秒）。
  - 此前 ARM64 上 `make test` 也出现过同一用例的时序偶发。
- 本机复现：本机单独复跑通过。
- 疑似根因：看门狗判定依赖真实时钟和心跳间隔，CI 高负载时看门狗循环的调度延迟超过了用例的等待窗口。
- 处置：现象一致可复跑一次。根治要把看门狗与心跳改用可注入的时钟，或把等待改为按状态轮询到上限，列入后续工具修复。

## 已修复

### TestPinnedOpenAIModelsListMixedAccountsShareColdCacheAcrossGroups

- 位置：Go，`backend/internal/service/openai_models_list_test.go`。
- 现象：两个分组并发拉取同一批账号的模型清单，用例断言 API Key 账号只打一次上游，偶发实际打了两次
  （`require.EqualValues(t, 1, apiCalls.Load())` 报 expected 1、actual 2）。
- 出现记录：
  - 2026-09-25，提交 001d8955a 的 CI（run 36109428010，test job「Unit tests」）；该提交没有改 backend。
  - 2026-10-27，ARM64 后台验证整包并行首跑红一次；本机整包并行时候选链一路也红过一次。
- 根因（生产代码缺陷，不是测试问题）：`fetchCachedOpenAIModels` 先查缓存、再进入 singleflight，两步之间没有锁；
  上一轮合并调用若恰好在这两步之间完成（结果已写入缓存、key 已释放），后来者仍按“未命中”进入新一轮合并，
  而领头者内部只取缓存里的 ETag、并不判断缓存是否已经新鲜，于是重复请求上游。线上并发冷读同一账号时同样会多打一次上游。
  （原先怀疑的“释放之后、写入之前”并不存在：写入缓存发生在合并调用返回之前。）
- 本机复现：固定写法下单独跑约 0.1% 概率红（2000 次红 4 次），多进程并发约 0.3%（9600 次红 31 次，全是上游被调 2 次）。
- 修复：`refreshCachedOpenAIModels` 的领头者内部在请求上游前再看一次缓存，已是新鲜状态就直接复用；
  stale 与未命中照常刷新。新增 `openai_models_cache_refresh_test.go`：一个确定性用例（旧代码必红）、一个 stale／过期仍刷新用例、
  一个 300 轮冷读并发用例（旧代码第 11 轮即红）。修复后同口径 19200 次 0 次红。
- 修复提交：`acbe032f697bf7d1cc64adb04f601a0665b6ed3c`（`fix(openai): 模型清单缓存合并刷新在领头者内复核缓存，避免并发冷读重复请求上游`）。
- 注意：该用例走真实 loopback HTTP，用 `-count` 很大并多进程同时跑时会耗尽本机临时端口（TIME_WAIT 过万），
  表现为 OAuth 账号清单缺项，与本条无关，压测时分批并等 TIME_WAIT 排空。
