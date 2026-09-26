# 已知不稳定测试清单

本清单登记在 CI 或 ARM64 上出现过、单独复跑可以通过、但还没有根治的偶发失败测试。

- 任何“复跑通过”的失败都要先登记在这里再合并，不能只靠复跑。登记内容：首次出现的提交与 CI 运行、失败现象、本机复现
  尝试、疑似根因、处置方式。
- CI 再次出现同一失败时，先对照“现象”：一致的可以复跑一次，并在该条“出现记录”里追加一行；不一致的按新缺陷处理。
- 根治后把条目移到“已修复”，写明修复提交。

## 未修复

### TestPinnedOpenAIModelsListMixedAccountsShareColdCacheAcrossGroups

- 位置：Go，`backend/internal/service/openai_models_list_test.go`。
- 现象：两个分组并发拉取同一批账号的模型清单，用例断言 API Key 账号只打一次上游，偶发实际打了两次
  （`require.EqualValues(t, 1, apiCalls.Load())` 报 expected 1、actual 2）。
- 出现记录：
  - 2026-09-25，提交 001d8955a 的 CI（run 36109428010，test job「Unit tests」）；该提交没有改 backend。
- 本机复现：`go test -run TestPinnedOpenAIModelsListMixedAccountsShareColdCacheAcrossGroups` 连跑 30 次均通过。
- 疑似根因：冷缓存并发去重的时间窗。第一个分组的上游调用结束、合并调用释放之后，结果写入缓存之前，第二个分组检查缓存
  未命中，又发了一次上游调用。与调度时序有关，CI 负载高时更容易出现。
- 处置：现象一致可复跑一次。根治要保证合并调用释放前结果已对后来者可见（或让后来者等待同一次调用的结果），列入后续
  backend 修复。

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

（暂无）
