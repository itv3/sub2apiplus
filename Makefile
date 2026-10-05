.PHONY: codex-p0-rehearsal build build-backend build-frontend test test-backend test-frontend test-frontend-critical test-capture-tools test-capture-tools-prerequisites test-capture-real-chains test-official-client-control test-upstream-merge-tools upstream-gate-full upstream-preflight upstream-baseline-seal upstream-baseline-validate upstream-revision-preflight upstream-source-transition upstream-source-transition-validate check-egress-spec check-egress-spec-ci egress-scanner-check check-egress-spec-local-source check-egress-bootstrap-replay check-egress-seal

EGRESS_BOOTSTRAP_COMMIT := 38a9929eac35a39c86de2f27de8f7a805d7dae52
EGRESS_BOOTSTRAP_BASELINE := $(CURDIR)/docs/egress/foundation/sink-baseline.json
EGRESS_BOOTSTRAP_SUPPLEMENTS := $(CURDIR)/docs/egress/lifecycle/pre-bootstrap-supplements.json
EGRESS_REMOVAL_RECEIPTS := $(CURDIR)/docs/egress/release/removal-receipts.json,$(CURDIR)/docs/egress/migration/removal-receipts.json,$(CURDIR)/docs/egress/consolidation/removal-receipts.json,$(CURDIR)/docs/egress/maintenance/removal-receipts.json,$(CURDIR)/backend/internal/officialegress/catalogdata/maintenance-removal-receipts.json
EGRESS_MIGRATION_RECEIPTS := $(CURDIR)/docs/egress/lifecycle/migration-receipts.json,$(CURDIR)/docs/egress/migration/migration-receipts.json
# bootstrap 回放使用完整追加式收据链：历史源码中的已退休候选仍须证明存在，
# 但分类结论由对应 RemovalReceipt 冻结，不能要求当前 scanner 永久保留旧规则。
EGRESS_BOOTSTRAP_REMOVAL_RECEIPTS := $(EGRESS_REMOVAL_RECEIPTS)
EGRESS_BOOTSTRAP_MIGRATION_RECEIPTS := $(EGRESS_MIGRATION_RECEIPTS)
EGRESS_CATALOG_AMENDMENTS := $(CURDIR)/docs/egress/lifecycle/catalog-amendments.json
EGRESS_BOOTSTRAP_INVENTORY_LOCK := $(CURDIR)/docs/egress/maintenance/bootstrap-inventory-lock.json
EGRESS_SCANNER_SOURCE_ROOT := $(CURDIR)/backend/cmd/egressscan
EGRESS_LEGACY_BASELINE := $(CURDIR)/docs/egress/lifecycle/legacy-baseline.json
EGRESS_LEGACY_CEILING := $(CURDIR)/docs/egress/lifecycle/legacy-ceiling.json
EGRESS_LEGACY_SEAL_RECEIPT := $(CURDIR)/docs/egress/lifecycle/legacy-seal-receipt.json
EGRESS_SEAL_BASE_REF ?=
CODEX_P0_CAMPAIGN_DIR ?=
CODEX_P0_ROOT ?=
UPSTREAM_MERGE_PLAN ?= $(CURDIR)/docs/egress/maintenance/upstream-v0.1.177-merge-plan.json
UPSTREAM_MERGE_REPOSITORY ?= $(CURDIR)
UPSTREAM_MERGE_REQUEST ?=
UPSTREAM_MERGE_PREFLIGHT_OUTPUT ?=
UPSTREAM_MERGE_BASELINE_INPUT ?=
UPSTREAM_MERGE_BASELINE_OUTPUT ?=
UPSTREAM_MERGE_BASELINE_RECEIPT ?=
UPSTREAM_MERGE_TRANSITIONS ?=
UPSTREAM_MERGE_BEFORE ?=
UPSTREAM_MERGE_AFTER ?=
UPSTREAM_MERGE_TRANSITION_OUTPUT ?=
UPSTREAM_MERGE_TRANSITION_PREDECESSOR ?=
UPSTREAM_MERGE_TRANSITION_REASON ?=
CODEX_0_149_1_SOURCE_ROOT ?= $(CURDIR)/local-analysis/sources/codex-cli-0.149.1
CAPTURE_TYPESCRIPT_MODULE ?= $(CURDIR)/frontend/node_modules/typescript/lib/typescript.js
CAPTURE_TYPESCRIPT_SHA256 := f316520790d4db220a10d890c5f85310e26a1bd3c104b8d3b5eb62ba0491651b

FRONTEND_CRITICAL_VITEST := \
	src/i18n/__tests__/localeKeyCompleteness.spec.ts \
	src/api/__tests__/client.spec.ts \
	src/api/__tests__/tokenRefresh.spec.ts \
	src/api/__tests__/keys.bulkUpdate.spec.ts \
	src/components/account/__tests__/OpenAIReferralCell.spec.ts \
	src/components/account/__tests__/OpenAIReferralCell.transport.spec.ts \
	src/components/account/__tests__/OpenAIQuotaResetCell.spark_shadow.spec.ts \
	src/components/keys/__tests__/BulkEditKeysModal.spec.ts \
	src/components/admin/user/__tests__/UserPlatformQuotaModal.spec.ts \
	src/views/user/__tests__/KeysView.spec.ts \
	src/api/__tests__/channelMonitorV2.spec.ts \
	src/views/auth/__tests__/LinuxDoCallbackView.spec.ts \
	src/views/auth/__tests__/WechatCallbackView.spec.ts \
	src/views/user/__tests__/PaymentView.spec.ts \
	src/views/user/__tests__/PaymentResultView.spec.ts \
	src/views/user/__tests__/ChannelStatusView.mode.spec.ts \
	src/components/user/profile/__tests__/ProfileInfoCard.spec.ts \
	src/views/admin/__tests__/SettingsView.spec.ts \
	src/features/channel-monitor-v2/__tests__/designSystem.structure.spec.ts \
	src/features/channel-monitor-v2/__tests__/monitorFormat.spec.ts \
	src/features/channel-monitor-v2/__tests__/monitorZoom.spec.ts

# 一键编译前后端
build: build-backend build-frontend

# 编译后端（复用 backend/Makefile）
build-backend:
	@$(MAKE) -C backend build

# 编译前端（需要已安装依赖）
build-frontend:
	@pnpm --dir frontend run build

# 运行测试（后端 + 前端）。test-official-client-control 是 check-egress-spec 的子检查之一（EGRESS_SPEC_CI_CHECKS），
# 由那里交给统一调度执行器执行；子检查跑在执行器另起的 make 进程里，不会和这里的先决目标去重，所以这里不再单列，
# 否则 make test 会把它执行两次。
test: test-backend test-frontend test-capture-tools check-egress-spec

# Codex 客户端仿真门禁，见 docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md §3.5 与 §5.1。
# 这些检查此前只能手工执行，因而无法阻止回归；--self-test 先校验判据本身是否
# 仍与版本号解耦，再用它扫描仓库。全部只读且不联网。
check-egress-seal:
	@cd backend && go run ./cmd/egressseal \
		-receipt "$(EGRESS_LEGACY_SEAL_RECEIPT)" \
		-ceiling "$(EGRESS_LEGACY_CEILING)" \
		-supplements "$(EGRESS_BOOTSTRAP_SUPPLEMENTS)" \
		-baseline "$(EGRESS_LEGACY_BASELINE)" \
		-protected-base-ref "$(EGRESS_SEAL_BASE_REF)"

# 本地完整门禁额外校验被 .gitignore 排除的 Codex CLI 源码引用；CI checkout
# 不包含 local-analysis，因此只执行下面的可复现提交态闭集。
check-egress-spec-local-source:
	@python3 tools/check_spec_refs.py \
		--source-root "$(CODEX_0_149_1_SOURCE_ROOT)" \
		--source-version 0.149.1 \
		--cargo-lock "$(CODEX_0_149_1_SOURCE_ROOT)/codex-rs/Cargo.lock" \
		--anchor-manifest tools/spec_ref_anchors.json \
		--dependency-manifest tools/spec_source_deps/manifest.json \
		--symbol --cfg-test

# 发送面扫描器基线检查：check-egress-spec-ci 的一部分；上游合并预检也在 -X ours 试扫描树上单独调用它，
# 在建 Plan 前列出尚未在主干预先登记的上游新增发送点（UM-13）。
egress-scanner-check:
	@cd backend && go run ./cmd/egressscan -mode check \
		-baseline ../docs/egress/foundation/sink-baseline.json \
		-supplements ../docs/egress/lifecycle/pre-bootstrap-supplements.json \
		-removals "$(EGRESS_REMOVAL_RECEIPTS)" \
		-migration-receipts "$(EGRESS_MIGRATION_RECEIPTS)" \
		-catalog-amendments ../docs/egress/lifecycle/catalog-amendments.json \
		-inventory-lock "$(EGRESS_BOOTSTRAP_INVENTORY_LOCK)" \
		-scanner-source-root ./cmd/egressscan

# check-egress-spec 由一组互不依赖的子检查组成（E2-04）：每项一个独立目标（下面的 egress-spec-* 与原有的
# check-egress-bootstrap-replay、egress-scanner-check 等），临时产物都在各自的 mktemp 目录里生成、比对、删除，彼此没有
# 数据依赖。两个目标交给统一调度执行器（tools/ci/entry_gates.py make-checks → tools/ci/unit_executor.py run-commands）
# 在整机额度内并行执行：一项失败其余照跑，全部跑完再汇总，失败项逐个列出并附日志尾部。入口门禁（驱动
# entry-gates.sh）按 print-egress-spec-checks 读同一份清单展开这些子检查，所以 P0 收据里的字面命令
# make check-egress-spec 与实际执行的子检查集合相同。CI checkout 不含 local-analysis（没有 0.149.1 源码树），只跑 _CI 那一份。
EGRESS_SPEC_CI_CHECKS := \
	check-egress-bootstrap-replay \
	check-egress-seal \
	test-official-client-control \
	test-upstream-merge-tools \
	egress-spec-version-leak-self-test \
	egress-spec-version-leak \
	egress-spec-changeset5-symbols-self-test \
	egress-spec-changeset5-symbols \
	egress-spec-changeset6-transition-self-test \
	egress-spec-changeset6-transition \
	egress-spec-maintenance-transition-self-test \
	egress-spec-maintenance-transition \
	egress-spec-multi-persona-transition-self-test \
	egress-spec-multi-persona-transition \
	egress-spec-fw-d-transition-self-test \
	egress-spec-fw-e-workspace-self-test \
	egress-spec-fw-e-workspace \
	egress-spec-fw-e-completeness-self-test \
	egress-spec-fw-e-completeness \
	egress-spec-fw-e-runtime-evidence-self-test \
	egress-spec-fw-e-runtime-evidence \
	egress-spec-fw-e-r-disposition-self-test \
	egress-spec-fw-e-r-disposition \
	egress-spec-changeset6-benchmark-self-test \
	egress-spec-changeset6-benchmark \
	egress-spec-audit-index-self-test \
	egress-spec-ledger-completeness \
	egress-spec-egressscan-self-test \
	egress-scanner-check \
	egress-spec-sink-stats \
	egress-spec-gofmt \
	egress-spec-go-vet \
	egress-spec-go-test \
	egress-spec-changeset6-conflict-self-test \
	egress-spec-changeset6-conflict \
	egress-spec-maintenance-conflict-self-test \
	egress-spec-maintenance-conflict \
	egress-spec-wire-diff \
	egress-spec-repository-guard \
	egress-spec-service-guard \
	egress-spec-httpclient-guard \
	egress-spec-runtime-catalog-self-test \
	egress-spec-runtime-catalog \
	egress-spec-profile-snapshot \
	egress-spec-profile-enums \
	egress-spec-release-graph \
	egress-spec-release-bindings \
	egress-spec-go-build
EGRESS_SPEC_CHECKS := check-egress-spec-local-source $(EGRESS_SPEC_CI_CHECKS)
.PHONY: print-egress-spec-checks $(EGRESS_SPEC_CHECKS)

check-egress-spec:
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/ci/entry_gates.py make-checks --name check-egress-spec $(EGRESS_SPEC_CHECKS)

check-egress-spec-ci:
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/ci/entry_gates.py make-checks --name check-egress-spec-ci $(EGRESS_SPEC_CI_CHECKS)

# 入口门禁读取子检查清单（与上面两个目标同一份）。
print-egress-spec-checks:
	@echo $(EGRESS_SPEC_CHECKS)

egress-spec-version-leak-self-test:
	@python3 tools/check_version_leak.py --self-test

egress-spec-version-leak:
	@python3 tools/check_version_leak.py

egress-spec-changeset5-symbols-self-test:
	@python3 tools/check_changeset5_0145_symbols.py --self-test

egress-spec-changeset5-symbols:
	@python3 tools/check_changeset5_0145_symbols.py

egress-spec-changeset6-transition-self-test:
	@python3 tools/changeset6_workspace_transition.py --self-test

egress-spec-changeset6-transition:
	@python3 tools/changeset6_workspace_transition.py --frozen-only

egress-spec-maintenance-transition-self-test:
	@python3 tools/maintenance_workspace_transition.py --self-test

egress-spec-maintenance-transition:
	@python3 tools/maintenance_workspace_transition.py --frozen-only

egress-spec-multi-persona-transition-self-test:
	@python3 tools/multi_persona_control_workspace_transition.py --self-test

egress-spec-multi-persona-transition:
	@python3 tools/multi_persona_control_workspace_transition.py --frozen-only

egress-spec-fw-d-transition-self-test:
	@python3 tools/fw_d_control_workspace_transition.py --self-test

egress-spec-fw-e-workspace-self-test:
	@python3 tools/fw_e_workspace_transition.py --self-test

egress-spec-fw-e-workspace:
	@python3 tools/fw_e_workspace_transition.py

egress-spec-fw-e-completeness-self-test:
	@python3 tools/fw_e_completeness_transition.py --self-test

egress-spec-fw-e-completeness:
	@python3 tools/fw_e_completeness_transition.py

egress-spec-fw-e-runtime-evidence-self-test:
	@python3 tools/fw_e_runtime_evidence_transition.py --self-test

egress-spec-fw-e-runtime-evidence:
	@python3 tools/fw_e_runtime_evidence_transition.py

egress-spec-fw-e-r-disposition-self-test:
	@python3 tools/fw_e_r_disposition_transition.py --self-test

egress-spec-fw-e-r-disposition:
	@python3 tools/fw_e_r_disposition_transition.py

egress-spec-changeset6-benchmark-self-test:
	@python3 tools/changeset6_benchmark_evidence.py --self-test

egress-spec-changeset6-benchmark:
	@python3 tools/changeset6_benchmark_evidence.py

egress-spec-audit-index-self-test:
	@python3 tools/codex_audit_index.py self-test

egress-spec-ledger-completeness:
	@python3 tools/check_ledger_completeness.py \
		--upstream-merge-plan "$(UPSTREAM_MERGE_PLAN)"

egress-spec-egressscan-self-test:
	@cd backend && go run ./cmd/egressscan -mode self-test

egress-spec-sink-stats:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressscan -mode stats \
			-baseline ../docs/egress/foundation/sink-baseline.json -out $$d/sink-stats.md && \
		cmp -s $$d/sink-stats.md ../docs/egress/foundation/sink-stats.md || \
		{ echo "🔴 发送面统计已与基线漂移，请重新生成 sink-stats.md"; exit 1; }

egress-spec-gofmt:
	@cd backend && test -z "$$(gofmt -l \
		./internal/officialegress/ \
		./cmd/egressruntimedump/ \
		./cmd/egresscatalogstage/ \
		./cmd/egressprofiledump/ \
		./cmd/egressreleasegraphdump/ \
		./cmd/egressbindingdump/ \
		./cmd/egressconflictinventory/ \
		./cmd/egressseal/ \
		./cmd/egressscan/)"

egress-spec-go-vet:
	@cd backend && go vet \
		./internal/officialegress/... \
		./cmd/egressruntimedump/ \
		./cmd/egresscatalogstage/ \
		./cmd/egressprofiledump/ \
		./cmd/egressreleasegraphdump/ \
		./cmd/egressbindingdump/ \
		./cmd/egressconflictinventory/ \
		./cmd/egressseal/ \
		./cmd/egressscan/

egress-spec-go-test:
	@cd backend && go test \
		./internal/officialegress/... \
		./cmd/egressruntimedump/ \
		./cmd/egresscatalogstage/ \
		./cmd/egressprofiledump/ \
		./cmd/egressreleasegraphdump/ \
		./cmd/egressbindingdump/ \
		./cmd/egressconflictinventory/ \
		./cmd/egressseal/ \
		./cmd/egressscan/ -count=1

# 36 文件冲突 inventory 是旧基线下的历史收缩证据，由下面四个 conflict transition 检查固定原文与摘要。
# 当前源码闭集改由 §3.5 的 v0.1.177 路径复算覆盖，禁止再用旧基线重建并改写历史快照。
egress-spec-changeset6-conflict-self-test:
	@python3 tools/changeset6_conflict_transition.py --self-test

egress-spec-changeset6-conflict:
	@python3 tools/changeset6_conflict_transition.py

egress-spec-maintenance-conflict-self-test:
	@python3 tools/maintenance_conflict_transition.py --self-test

egress-spec-maintenance-conflict:
	@python3 tools/maintenance_conflict_transition.py

egress-spec-wire-diff:
	@cd backend && go run -mod=mod github.com/google/wire/cmd/wire diff ./cmd/server

# 四类终端发送栈必须证明 Guard 接入前后发送事实与结果不变。
egress-spec-repository-guard:
	@cd backend && go test ./internal/repository \
		-run '^(TestChromePersona|TestHTTPUpstreamGuardPreservesOutOfScopeWireAndResult|TestReqProfileGuardPreservesOutOfScopeWireAndResult)' -count=1

egress-spec-service-guard:
	@cd backend && go test ./internal/service \
		-run '^(TestPrivacyProductionFunctionsBuildAllThreeBrowserRequests|TestWebSocketHandshakeGuardPreservesWireAndResult|TestChangeset3RuntimeSinksEnterExecutorWithoutLegacyFinalizers|TestChangeset5LegacyAttachFinalizerDefinitionsAndCallsAreExtinct|TestChangeset5LegacyExtinctionGateRejectsDefinitionsAndWrappedCalls|TestChangeset5WebSocketExecutorOwnsFinalHandshakeHeaders|TestChangeset5OriginalPreFinalWireIsByteExactAndFrozen|TestChangeset5NormalizedPreAppliesOnlyExactOAuthNoiseTransition|TestChangeset5CurrentFinalWireMatchesFrozenWireFields|TestChangeset5CurrentFinalWireComparatorRejectsWireDrift|TestChangeset5NormalizationTransitionRejectsWrongOrExpandedApproval|TestChangeset5PostRefactorFinalWireIsFrozenAndMatchesPre)$$' -count=1

egress-spec-httpclient-guard:
	@cd backend && go test ./internal/pkg/httpclient \
		-run '^(TestBuildTransportWithCustomDialKeepsHTTP2Disabled|TestSharedPoolGuardPreservesOutOfScopeWireAndResult)$$' -count=1

# 防快照与生成文件陈旧：临时导出后比对。
# 用 mktemp -d 而非固定 /tmp 路径，避免并行 CI 互相覆盖。
# 比较不再是裸 diff：退休旧 Previous 时，被更早终态收据冻结为逐文件校验制品的画像
# 必须原地保留，会成为运行目录里不在 dump 闭集内的文件。排除项只能来自终态收据的
# retained_runtime_profiles，每份都与它自己绑定的 RemovalReceipt 逐条交叉验证（保留画像
# 跨轮延续，由退休当轮的终态收据持续批准）；排除之后其余文件仍必须与导出结果逐字节完全一致。
egress-spec-runtime-catalog-self-test:
	@python3 tools/check_runtime_catalog_projection.py --self-test

egress-spec-runtime-catalog:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressruntimedump -output $$d/runtime >/dev/null || \
		{ echo "🔴 正式版本数据导出失败，请检查 cmd/egressruntimedump"; exit 1; }; \
		python3 ../tools/check_runtime_catalog_projection.py \
			--root .. --dump $$d/runtime --repo internal/officialegress/catalogdata/runtime >/dev/null

egress-spec-profile-snapshot:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressprofiledump $$d/snap.json >/dev/null && \
		key=$$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["Version"] + "/" + d["Digest"])' $$d/snap.json) && \
		test -n "$$key" && \
		cmp -s $$d/snap.json internal/officialegress/profilecontract/testdata/snapshots/$$key.json || \
		{ echo "🔴 画像快照已变更，请重跑 cmd/egressprofiledump 更新 testdata"; exit 1; }

egress-spec-profile-enums:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressprofiledump -enums \
			internal/officialegress/profilecontract/testdata/snapshot-catalog.json $$d/enums.go >/dev/null && \
		gofmt $$d/enums.go > $$d/fmt.go && \
		cmp -s $$d/fmt.go internal/officialegress/profilecontract/enums_gen.go || \
		{ echo "🔴 枚举生成结果已变更，请重跑 -enums 更新 enums_gen.go"; exit 1; }

egress-spec-release-graph:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressreleasegraphdump $$d/release-graph.json >/dev/null && \
		cmp -s $$d/release-graph.json internal/officialegress/releasecontract/testdata/release-graph.json || \
		{ echo "🔴 official-client 发布图已变更，请更新 release-graph.json"; exit 1; }

egress-spec-release-bindings:
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressbindingdump \
			../docs/egress/foundation/sink-baseline.json $$d/release-bindings.json >/dev/null && \
		cmp -s $$d/release-bindings.json internal/officialegress/bindingcontract/testdata/release-bindings.json || \
		{ echo "🔴 ReleaseBinding 已与 sink 基线漂移，请更新 release-bindings.json"; exit 1; }

egress-spec-go-build:
	@cd backend && go build ./... >/dev/null

# 用当前受审扫描器回放 bootstrap commit 的干净源码归档。当前工作区即使有未提交
# 改动，也不会参与存在性证明；补录项必须在该历史源码中真实可扫描。
check-egress-bootstrap-replay:
	@d=$$(mktemp -d) && trap "rm -rf $$d" EXIT && \
		mkdir -p $$d/source && \
		cd backend && go build -o $$d/egressscan ./cmd/egressscan && \
		cd .. && git archive $(EGRESS_BOOTSTRAP_COMMIT) | tar -x -C $$d/source && \
		cd $$d/source/backend && $$d/egressscan -mode replay \
			-baseline $(EGRESS_BOOTSTRAP_BASELINE) \
			-supplements $(EGRESS_BOOTSTRAP_SUPPLEMENTS) \
			-removals $(EGRESS_BOOTSTRAP_REMOVAL_RECEIPTS) \
			-migration-receipts $(EGRESS_BOOTSTRAP_MIGRATION_RECEIPTS) \
			-catalog-amendments $(EGRESS_CATALOG_AMENDMENTS) \
			-inventory-lock $(EGRESS_BOOTSTRAP_INVENTORY_LOCK) \
			-scanner-source-root $(EGRESS_SCANNER_SOURCE_ROOT)

test-backend:
	@$(MAKE) -C backend test

test-frontend:
	@pnpm --dir frontend run lint:check
	@pnpm --dir frontend run typecheck
	@$(MAKE) test-frontend-critical

test-frontend-critical:
	@pnpm --dir frontend exec vitest run $(FRONTEND_CRITICAL_VITEST)

# 抓包工具提交态测试只使用合成数据，不联网、不读取真实凭据、不启动抓包进程；
# 依赖 local-analysis 的原始证据复算仅在本机证据存在时执行。Claude bundle AST 用
# frontend lockfile 中的 TypeScript 解析器，禁止临时下载或浮动版本。
# 分片并行（2026-09-28 老板拍板：保持全量门禁、把 test-capture-tools 拆成 N 片并行）。分片与全量共用同一 discover 语义，
# 分片器先做闭合自检（并集＝全量、互不相交、权重表无陈旧条目），任一片失败即失败。CI 按 matrix 逐片跑；本机门禁
# （upstream-gate-full）只跑闭合自检，全量交给统一调度执行器 make test-capture-tools（UM-20：静态 4 片片间不均，
# v0.2.13 合并时最慢一片 887 秒，执行器同机约 350 秒），原来的本机 4 片并行目标随之删除。
CAPTURE_TEST_SHARDS ?= 4
# CI 默认接入统一执行器；显式 legacy 恢复原单进程分片，失败不自动回退。
CAPTURE_TEST_EXECUTOR ?= unified
# 各 CI 作业内共享只读字节码；显式 off 恢复无共享缓存，身份记忆化独立推进。
CAPTURE_TEST_BYTECODE_CACHE ?= auto
CAPTURE_TEST_OUT_DIR ?=
define CAPTURE_TS_MODULE_CHECK
	python3 -c 'import hashlib, pathlib, sys; raw = pathlib.Path(sys.argv[1]); expected = sys.argv[2]; p = raw.resolve(strict=True); (raw.is_absolute() and raw.is_file() and not raw.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest() == expected) or sys.exit("CAPTURE_TYPESCRIPT_MODULE 非法或摘要不符")' \
		"$(CAPTURE_TYPESCRIPT_MODULE)" "$(CAPTURE_TYPESCRIPT_SHA256)"
	node --version >/dev/null
endef

test-capture-tools-shard-check:
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/ci/capture_test_shards.py check --count $(CAPTURE_TEST_SHARDS)

# 单片：SHARD_INDEX=1..$(CAPTURE_TEST_SHARDS)
test-capture-tools-shard:
	@$(CAPTURE_TS_MODULE_CHECK)
	@CLAUDE_AST_TYPESCRIPT_MODULE="$(CAPTURE_TYPESCRIPT_MODULE)" \
		PYTHONDONTWRITEBYTECODE=1 python3 tools/ci/capture_ci.py \
		--count $(CAPTURE_TEST_SHARDS) --index $(SHARD_INDEX) --executor "$(CAPTURE_TEST_EXECUTOR)" \
		--bytecode-cache "$(CAPTURE_TEST_BYTECODE_CACHE)" \
		--parallel $(CAPTURE_TEST_PARALLELISM) $(if $(CAPTURE_TEST_OUT_DIR),--out-dir "$(CAPTURE_TEST_OUT_DIR)",)

# 采集工具测试全量（E2-01）：统一调度执行器 tools/ci/unit_executor.py 与原单进程 discover 同一加载语义
# （起点 tools/official_client_capture/tests、模式 test_*.py），改为每个测试模块（重模块按方法拆块）一个独立进程，
# 在整机核数与内存额度内并行，跑完再一次汇总；最后两段输出与 unittest 相同，P0 收据照原样解析。
# CAPTURE_TEST_PARALLELISM=0 取 tools/ci/unit_executor.json 的默认并行度，设为 1 即逐个单元执行。
CAPTURE_TEST_PARALLELISM ?= 0
# 前置检查单独成目标（E2-04）：入口门禁把它与采集工具测试组放在同一个门禁项里，make test-capture-tools 照旧先跑它。
test-capture-tools-prerequisites:
	@python3 -c 'import hashlib, pathlib, sys; raw = pathlib.Path(sys.argv[1]); expected = sys.argv[2]; p = raw.resolve(strict=True); (raw.is_absolute() and raw.is_file() and not raw.is_symlink() and p.is_file()) or sys.exit("🔴 TypeScript AST 解析器必须是绝对路径下的普通文件（允许 pnpm 父目录符号链接）"); actual = hashlib.sha256(p.read_bytes()).hexdigest(); actual == expected or sys.exit(f"🔴 TypeScript AST 解析器摘要不一致：{actual}")' \
		"$(CAPTURE_TYPESCRIPT_MODULE)" "$(CAPTURE_TYPESCRIPT_SHA256)"
	@node --version >/dev/null

test-capture-tools: test-capture-tools-prerequisites
	@CLAUDE_AST_TYPESCRIPT_MODULE="$(CAPTURE_TYPESCRIPT_MODULE)" \
		PYTHONDONTWRITEBYTECODE=1 python3 tools/ci/unit_executor.py run \
		--start tools/official_client_capture/tests --pattern 'test_*.py' --parallel $(CAPTURE_TEST_PARALLELISM)

# 升级控制工具的真实评估链（副本受管树 + 正式监督器子进程，每条链在 ARM64 上约 10 分钟）
# 属于工具发布／部署验证，不属于候选门禁的默认 `make test`：2026-09-21 v14r2 实测它们让
# 候选 target-platform 门禁在 ARM64 上跑到 5 小时、必然超出 VC-5 阶段预算。它们放在
# tests/real_chains/（无 __init__.py，unittest discover 不会递归进入），由本目标显式逐模块
# 执行；不删除、不 skip。防回流由 tests/test_capture_test_layering.py 保证。
# 缺少 Linux／root／Docker 等条件的链会被跳过：执行入口逐条列出，ARM64 自查设置
# CAPTURE_REAL_CHAINS_REQUIRE_EXECUTION=1 使任一跳过按失败处理，避免跳过被误读为通过。
CAPTURE_REAL_CHAIN_MODULES := \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_evaluation_real_chain \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_evaluation_attempt_recovery_real_chain \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_late_stage_faults \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_vc1_recorded_chain \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_fault_fixtures \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_deadline_extension \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_official_reuse_resume \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_build_revision \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_stage_recovery \
	tools.official_client_capture.tests.real_chains.test_codex_upgrade_segment_reuse
test-capture-real-chains:
	@CLAUDE_AST_TYPESCRIPT_MODULE="$(CAPTURE_TYPESCRIPT_MODULE)" \
		PYTHONDONTWRITEBYTECODE=1 python3 -m tools.official_client_capture.tests.real_chain_gate $(CAPTURE_REAL_CHAIN_MODULES)

# FW-D 通用受管工具链只使用当前 Codex 不可变制品和合成 Persona 数据，
# 不联网、不读取凭据、不查询或生成任何新 Persona 的官方画像。
test-official-client-control:
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
		-s tools/official_client_control/tests -p 'test_*.py'

# Sub2API 上游合并 U-0～U-6 状态机只使用合成 Git 图，
# 不联网、不触碰真实上游分支，也不推送或部署。
test-upstream-merge-tools:
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
		-s tools/upstream_merge/tests -p 'test_*.py'

# 上游合并 U-4 full-regression 组：出站规格、lint、前端、采集工具、go test 五条检查线按预计耗时从短到长
# 依次执行、遇错不停，每条线结束即输出该线日志与进度，全部结束后输出汇总；采集工具先做分片闭合自检、
# 再走统一调度执行器（make test-capture-tools）（见 tools/upstream_merge/gate_runner.py）。
# UPSTREAM_GATE_JOBS 可让检查线之间并行，默认 1：满载并行时计时敏感用例会误判。
upstream-gate-full:
	@PYTHONDONTWRITEBYTECODE=1 python3 -m tools.upstream_merge.gate_runner full \
		--codex-source-root "$(CODEX_0_149_1_SOURCE_ROOT)"

# 上游合并正式 plan-create 前的离线预检；输出报告必须放在主仓库之外，避免把
# 非权威报告误当成提交内容。所有参数均由调用方显式提供，不覆盖既有报告。
upstream-preflight:
	@test -n "$(UPSTREAM_MERGE_REQUEST)" || { echo "🔴 请设置 UPSTREAM_MERGE_REQUEST"; exit 2; }
	@python3 -m tools.upstream_merge preflight \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--request "$(UPSTREAM_MERGE_REQUEST)" \
		$(if $(UPSTREAM_MERGE_PREFLIGHT_OUTPUT),--output "$(UPSTREAM_MERGE_PREFLIGHT_OUTPUT)",)

# 将已执行的基线功能/证据检查绑定到当前干净提交；不会覆盖既有收据。
upstream-baseline-seal:
	@test -n "$(UPSTREAM_MERGE_BASELINE_INPUT)" || { echo "🔴 请设置 UPSTREAM_MERGE_BASELINE_INPUT"; exit 2; }
	@test -n "$(UPSTREAM_MERGE_BASELINE_OUTPUT)" || { echo "🔴 请设置 UPSTREAM_MERGE_BASELINE_OUTPUT"; exit 2; }
	@python3 -m tools.upstream_merge baseline-seal \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--input "$(UPSTREAM_MERGE_BASELINE_INPUT)" \
		--output "$(UPSTREAM_MERGE_BASELINE_OUTPUT)"

upstream-baseline-validate:
	@test -n "$(UPSTREAM_MERGE_BASELINE_RECEIPT)" || { echo "🔴 请设置 UPSTREAM_MERGE_BASELINE_RECEIPT"; exit 2; }
	@python3 -m tools.upstream_merge baseline-validate \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--receipt "$(UPSTREAM_MERGE_BASELINE_RECEIPT)"

upstream-revision-preflight:
	@test -n "$(UPSTREAM_MERGE_PLAN)" || { echo "🔴 请设置 UPSTREAM_MERGE_PLAN"; exit 2; }
	@python3 -m tools.upstream_merge revision-preflight \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--plan "$(UPSTREAM_MERGE_PLAN)" \
		$(foreach transition,$(UPSTREAM_MERGE_TRANSITIONS),--transition "$(transition)")

# 从两个已冻结提交追加生成 source-transition 链尾；不会改写已有节点。
upstream-source-transition:
	@test -n "$(UPSTREAM_MERGE_BEFORE)" || { echo "🔴 请设置 UPSTREAM_MERGE_BEFORE"; exit 2; }
	@test -n "$(UPSTREAM_MERGE_AFTER)" || { echo "🔴 请设置 UPSTREAM_MERGE_AFTER"; exit 2; }
	@test -n "$(UPSTREAM_MERGE_TRANSITION_OUTPUT)" || { echo "🔴 请设置 UPSTREAM_MERGE_TRANSITION_OUTPUT"; exit 2; }
	@python3 -m tools.upstream_merge source-transition \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--before "$(UPSTREAM_MERGE_BEFORE)" \
		--after "$(UPSTREAM_MERGE_AFTER)" \
		--output "$(UPSTREAM_MERGE_TRANSITION_OUTPUT)" \
		$(if $(UPSTREAM_MERGE_TRANSITION_PREDECESSOR),--predecessor-register "$(UPSTREAM_MERGE_TRANSITION_PREDECESSOR)",) \
		$(if $(UPSTREAM_MERGE_TRANSITION_REASON),--reason "$(UPSTREAM_MERGE_TRANSITION_REASON)",)

upstream-source-transition-validate:
	@test -n "$(UPSTREAM_MERGE_TRANSITION_OUTPUT)" || { echo "🔴 请设置 UPSTREAM_MERGE_TRANSITION_OUTPUT"; exit 2; }
	@python3 -m tools.upstream_merge source-transition-validate \
		--repository "$(UPSTREAM_MERGE_REPOSITORY)" \
		--transition "$(UPSTREAM_MERGE_TRANSITION_OUTPUT)"

# Codex 官方新版本发布后 24 小时内在 ARM64 离线演练全部 Job（不发官方请求），把工具与
# 环境就绪问题挡在升级窗口之外；输入是已用 plan --campaign-mode preflight_only 建好的目录。
codex-p0-rehearsal:
	@test -n "$(CODEX_P0_CAMPAIGN_DIR)" || { echo "🔴 请设置 CODEX_P0_CAMPAIGN_DIR（preflight_only Campaign 目录）"; exit 2; }
	@test -n "$(CODEX_P0_ROOT)" || { echo "🔴 请设置 CODEX_P0_ROOT（尚不存在的演练证据根）"; exit 2; }
	@test ! -e "$(CODEX_P0_ROOT)" || { echo "🔴 CODEX_P0_ROOT 已存在，演练根必须全新"; exit 2; }
	@mkdir -m 0700 "$(CODEX_P0_ROOT)"
	@python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt collect \
		--campaign-dir "$(CODEX_P0_CAMPAIGN_DIR)" --evidence-root "$(CODEX_P0_ROOT)" --output facts.json
	@python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt finalize \
		--evidence-root "$(CODEX_P0_ROOT)" --facts facts.json --output receipt.json
	@python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt replay \
		--evidence-root "$(CODEX_P0_ROOT)" --receipt receipt.json
	@echo "✅ P0 完整 Job 演练收据：$(CODEX_P0_ROOT)/receipt.json sha256=$$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$(CODEX_P0_ROOT)/receipt.json")"
