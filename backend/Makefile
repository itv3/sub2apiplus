.PHONY: build generate test test-gate test-unit test-integration test-e2e

VERSION ?= $(shell ./scripts/resolve-version.sh)
LDFLAGS ?= -s -w -X main.Version=$(VERSION)

build:
	CGO_ENABLED=0 go build -ldflags="$(LDFLAGS)" -trimpath -o bin/server ./cmd/server

generate:
	go generate ./ent
	go generate ./cmd/server

test:
	go test ./...
	golangci-lint run ./...

# 上游合并门禁的后端部分：默认、unit、integration 三组 go test 依次执行（默认组是唯一按
# 生产编译形态运行的一组，CI 没有它），再跑 golangci-lint 默认、unit、integration 三种标签；
# 固定 -count=1，遇错不停，全部结束后汇总（见 tools/upstream_merge/gate_runner.py）。
test-gate:
	@cd .. && PYTHONDONTWRITEBYTECODE=1 python3 -m tools.upstream_merge.gate_runner backend

test-unit:
	go test -tags=unit ./...

test-integration:
	go test -tags=integration ./...

test-e2e:
	./scripts/e2e-test.sh

test-e2e-local:
	go test -tags=e2e -v -timeout=300s ./internal/integration/...
