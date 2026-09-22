# 构建与发布入口
#
# 常用目标：
#   make build     本地编译 release 二进制
#   make test      跑全部测试（含真实推理的契约测试）
#   make image     构建 docker 镜像
#   make run       前台启动服务
#   make fixtures  重新生成契约测试基线（会改动 tests/fixtures）

CARGO       ?= cargo
SERVICE     ?= ocr
IMAGE       ?= modelman-$(SERVICE)
TAG         ?= local
PORT        ?= 8080
MODEL       ?= v6small

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: build
build: ## 编译 release 二进制
	$(CARGO) build --release

.PHONY: test
test: ## 运行单元测试、契约测试与 HTTP 测试
	$(CARGO) test --release

.PHONY: fmt
fmt: ## 格式化
	$(CARGO) fmt --all

.PHONY: fmt-check
fmt-check: ## 检查格式
	$(CARGO) fmt --all -- --check

.PHONY: clippy
clippy: ## 静态检查
	$(CARGO) clippy --workspace --all-targets -- -D warnings

.PHONY: run
run: build ## 前台启动服务
	MODELS_DIR=services/$(SERVICE)/models \
	DEFAULT_MODEL=$(MODEL) \
	PRELOAD_MODELS=$(MODEL) \
	LISTEN_ADDR=0.0.0.0:$(PORT) \
	RUST_LOG=ocr_service=info \
	./target/release/ocr-service

.PHONY: image
image: build ## 构建镜像（需先编译二进制）
	docker build -f services/$(SERVICE)/Dockerfile -t $(IMAGE):$(TAG) .

.PHONY: image-run
image-run: ## 运行镜像并暴露 $(PORT)
	docker run --rm -p $(PORT):8080 --name $(IMAGE)-local $(IMAGE):$(TAG)

.PHONY: fixtures
fixtures: build ## 重新生成契约测试基线，提交前必须 review 差异
	cd services/$(SERVICE) && ../../target/release/gen-fixtures tests/fixtures $(MODEL)

.PHONY: clean
clean: ## 清理构建产物
	$(CARGO) clean
