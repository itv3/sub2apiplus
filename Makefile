.PHONY: build build-backend build-frontend test test-backend test-frontend test-frontend-critical test-capture-tools check-egress-spec

FRONTEND_CRITICAL_VITEST := \
	src/views/auth/__tests__/LinuxDoCallbackView.spec.ts \
	src/views/auth/__tests__/WechatCallbackView.spec.ts \
	src/views/user/__tests__/PaymentView.spec.ts \
	src/views/user/__tests__/PaymentResultView.spec.ts \
	src/components/user/profile/__tests__/ProfileInfoCard.spec.ts \
	src/views/admin/__tests__/SettingsView.spec.ts

# 一键编译前后端
build: build-backend build-frontend

# 编译后端（复用 backend/Makefile）
build-backend:
	@$(MAKE) -C backend build

# 编译前端（需要已安装依赖）
build-frontend:
	@pnpm --dir frontend run build

# 运行测试（后端 + 前端）
test: test-backend test-frontend test-capture-tools check-egress-spec

# Codex 出站规格门禁，见 docs/CODEX_CLI_0145_EGRESS_SPEC.md §3.5 与 §4.7。
# 这些检查此前只能手工执行，因而无法阻止回归；--self-test 先校验判据本身是否
# 仍与版本号解耦，再用它扫描仓库。全部只读标准库，不联网。
check-egress-spec:
	@python3 tools/check_version_leak.py --self-test
	@python3 tools/check_version_leak.py
	@python3 tools/check_ledger_completeness.py
	@python3 tools/check_spec_refs.py

test-backend:
	@$(MAKE) -C backend test

test-frontend:
	@pnpm --dir frontend run lint:check
	@pnpm --dir frontend run typecheck
	@$(MAKE) test-frontend-critical

test-frontend-critical:
	@pnpm --dir frontend exec vitest run $(FRONTEND_CRITICAL_VITEST)

# 抓包工具测试只使用标准库和合成数据，不联网、不读取真实凭据、不启动抓包进程。
test-capture-tools:
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
		-s tools/official_client_capture/tests -p 'test_*.py'
