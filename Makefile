.PHONY: build build-backend build-frontend test test-backend test-frontend test-frontend-critical test-capture-tools test-official-client-control test-upstream-merge-tools check-egress-spec check-egress-spec-ci check-egress-spec-local-source check-egress-bootstrap-replay check-egress-seal

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
UPSTREAM_MERGE_PLAN ?= $(CURDIR)/docs/egress/maintenance/upstream-v0.1.177-merge-plan.json
CODEX_0_149_1_SOURCE_ROOT ?= $(CURDIR)/local-analysis/sources/codex-cli-0.149.1

FRONTEND_CRITICAL_VITEST := \
	src/api/__tests__/client.spec.ts \
	src/api/__tests__/tokenRefresh.spec.ts \
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

# 运行测试（后端 + 前端）
test: test-backend test-frontend test-capture-tools test-official-client-control check-egress-spec

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

check-egress-spec: check-egress-spec-local-source check-egress-spec-ci

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

check-egress-spec-ci: check-egress-bootstrap-replay check-egress-seal test-official-client-control test-upstream-merge-tools
	@python3 tools/check_version_leak.py --self-test
	@python3 tools/check_version_leak.py
	@python3 tools/check_changeset5_0145_symbols.py --self-test
	@python3 tools/check_changeset5_0145_symbols.py
	@python3 tools/changeset6_workspace_transition.py --self-test
	@python3 tools/changeset6_workspace_transition.py --frozen-only
	@python3 tools/maintenance_workspace_transition.py --self-test
	@python3 tools/maintenance_workspace_transition.py --frozen-only
	@python3 tools/multi_persona_control_workspace_transition.py --self-test
	@python3 tools/multi_persona_control_workspace_transition.py --frozen-only
	@python3 tools/fw_d_control_workspace_transition.py --self-test
	@python3 tools/fw_e_workspace_transition.py --self-test
	@python3 tools/fw_e_workspace_transition.py
	@python3 tools/fw_e_completeness_transition.py --self-test
	@python3 tools/fw_e_completeness_transition.py
	@python3 tools/fw_e_runtime_evidence_transition.py --self-test
	@python3 tools/fw_e_runtime_evidence_transition.py
	@python3 tools/fw_e_r_disposition_transition.py --self-test
	@python3 tools/fw_e_r_disposition_transition.py
	@python3 tools/changeset6_benchmark_evidence.py --self-test
	@python3 tools/changeset6_benchmark_evidence.py
	@python3 tools/check_ledger_completeness.py \
		--upstream-merge-plan "$(UPSTREAM_MERGE_PLAN)"
	@cd backend && go run ./cmd/egressscan -mode self-test
	@cd backend && go run ./cmd/egressscan -mode check \
		-baseline ../docs/egress/foundation/sink-baseline.json \
		-supplements ../docs/egress/lifecycle/pre-bootstrap-supplements.json \
		-removals "$(EGRESS_REMOVAL_RECEIPTS)" \
		-migration-receipts "$(EGRESS_MIGRATION_RECEIPTS)" \
		-catalog-amendments ../docs/egress/lifecycle/catalog-amendments.json \
		-inventory-lock "$(EGRESS_BOOTSTRAP_INVENTORY_LOCK)" \
		-scanner-source-root ./cmd/egressscan
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressscan -mode stats \
			-baseline ../docs/egress/foundation/sink-baseline.json -out $$d/sink-stats.md && \
		cmp -s $$d/sink-stats.md ../docs/egress/foundation/sink-stats.md || \
		{ echo "🔴 发送面统计已与基线漂移，请重新生成 sink-stats.md"; exit 1; }
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
	@python3 tools/changeset6_conflict_transition.py --self-test
	@python3 tools/changeset6_conflict_transition.py
	@python3 tools/maintenance_conflict_transition.py --self-test
	@python3 tools/maintenance_conflict_transition.py
	@# 36 文件冲突 inventory 是旧基线下的历史收缩证据，由上述 transition 固定原文与摘要。
	@# 当前源码闭集改由 §3.5 的 v0.1.177 路径复算覆盖，禁止再用旧基线重建并改写历史快照。
	@cd backend && go run -mod=mod github.com/google/wire/cmd/wire diff ./cmd/server
	@# 四类终端发送栈必须证明 Guard 接入前后发送事实与结果不变。
	@cd backend && go test ./internal/repository \
		-run '^(TestChromePersona|TestHTTPUpstreamGuardPreservesOutOfScopeWireAndResult|TestReqProfileGuardPreservesOutOfScopeWireAndResult)' -count=1
	@cd backend && go test ./internal/service \
		-run '^(TestPrivacyProductionFunctionsBuildAllThreeBrowserRequests|TestWebSocketHandshakeGuardPreservesWireAndResult|TestChangeset3RuntimeSinksEnterExecutorWithoutLegacyFinalizers|TestChangeset5LegacyAttachFinalizerDefinitionsAndCallsAreExtinct|TestChangeset5LegacyExtinctionGateRejectsDefinitionsAndWrappedCalls|TestChangeset5WebSocketExecutorOwnsFinalHandshakeHeaders|TestChangeset5OriginalPreFinalWireIsByteExactAndFrozen|TestChangeset5NormalizedPreAppliesOnlyExactOAuthNoiseTransition|TestChangeset5CurrentFinalWireMatchesFrozenWireFields|TestChangeset5CurrentFinalWireComparatorRejectsWireDrift|TestChangeset5NormalizationTransitionRejectsWrongOrExpandedApproval|TestChangeset5PostRefactorFinalWireIsFrozenAndMatchesPre)$$' -count=1
	@cd backend && go test ./internal/pkg/httpclient \
		-run '^(TestBuildTransportWithCustomDialKeepsHTTP2Disabled|TestSharedPoolGuardPreservesOutOfScopeWireAndResult)$$' -count=1
	@# 防快照与生成文件陈旧：临时导出后比对。
	@# 用 mktemp -d 而非固定 /tmp 路径，避免并行 CI 互相覆盖。
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressruntimedump -output $$d/runtime >/dev/null && \
		diff -qr $$d/runtime internal/officialegress/catalogdata/runtime >/dev/null || \
		{ echo "🔴 正式版本数据已漂移，请重跑 cmd/egressruntimedump 更新 catalogdata/runtime"; exit 1; }
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressprofiledump $$d/snap.json >/dev/null && \
		key=$$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["Version"] + "/" + d["Digest"])' $$d/snap.json) && \
		test -n "$$key" && \
		cmp -s $$d/snap.json internal/officialegress/profilecontract/testdata/snapshots/$$key.json || \
		{ echo "🔴 画像快照已变更，请重跑 cmd/egressprofiledump 更新 testdata"; exit 1; }
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressprofiledump -enums \
			internal/officialegress/profilecontract/testdata/snapshot-catalog.json $$d/enums.go >/dev/null && \
		gofmt $$d/enums.go > $$d/fmt.go && \
		cmp -s $$d/fmt.go internal/officialegress/profilecontract/enums_gen.go || \
		{ echo "🔴 枚举生成结果已变更，请重跑 -enums 更新 enums_gen.go"; exit 1; }
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressreleasegraphdump $$d/release-graph.json >/dev/null && \
		cmp -s $$d/release-graph.json internal/officialegress/releasecontract/testdata/release-graph.json || \
		{ echo "🔴 official-client 发布图已变更，请更新 release-graph.json"; exit 1; }
	@cd backend && d=$$(mktemp -d) && trap "rm -rf $$d" EXIT; \
		go run ./cmd/egressbindingdump \
			../docs/egress/foundation/sink-baseline.json $$d/release-bindings.json >/dev/null && \
		cmp -s $$d/release-bindings.json internal/officialegress/bindingcontract/testdata/release-bindings.json || \
		{ echo "🔴 ReleaseBinding 已与 sink 基线漂移，请更新 release-bindings.json"; exit 1; }
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
test-capture-tools:
	@test -f frontend/node_modules/typescript/lib/typescript.js || \
		{ echo "🔴 缺少锁定的 TypeScript AST 解析器，请先执行 pnpm --dir frontend install --frozen-lockfile"; exit 1; }
	@node --version >/dev/null
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
		-s tools/official_client_capture/tests -p 'test_*.py'

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
