# 构建与发布入口
#
# 这个文件只定义所有服务共用的目标，然后按下面的约定分派给具体服务：
#
#   services/<name>/service.mk   声明该服务怎么构建、测试、运行、冒烟
#
# 所以新增服务不需要改这个文件。可选的服务名就是含 service.mk 的目录名，
# 默认 ocr。
#
# 常用目标：
#   make build     本地编译 release 产物
#   make test      跑该服务的全部测试（OCR 会真实加载模型推理）
#   make image     构建该服务的 docker 镜像
#   make run       前台启动该服务
#   make smoke     起容器做冒烟检查（服务自带 smoke.sh）
#   make fixtures  重新生成契约测试基线（会改动 tests/fixtures）
#
# 切换服务：`make <目标> SERVICE=<name>`。fmt / fmt-check / clippy 是
# Rust workspace 级别的检查，与服务选择无关。

SERVICE ?= ocr
SERVICE_DIR := services/$(SERVICE)

IMAGE ?= modelman-$(SERVICE)
TAG   ?= local
PORT  ?= 8080
CARGO ?= cargo

.DEFAULT_GOAL := help

# 用 -include 而不是 include：写错服务名时让 help 仍然可用，
# 真正需要服务的目标由 need 逐个校验。
-include $(SERVICE_DIR)/service.mk

# 服务没声明某个入口时给出可读错误。make 会把空变量当成"没有命令要跑"
# 而静默成功，所以这里补一条会失败的语句。
#
# 判断放在 $(if) 里而不是 shell 的 test -n 里：入口命令本身可能带引号
# （例如 python -c "..."），一旦拼进 shell 命令行就会被再解释一次。
define need
$(if $(strip $($(1))),,@echo "$(SERVICE_DIR)/service.mk 未定义 $(1)" >&2; exit 2)
endef

.PHONY: help
help: ## 显示这个列表
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: service-check
service-check:
	@test -f $(SERVICE_DIR)/service.mk \
		|| { echo "未知服务 '$(SERVICE)'：$(SERVICE_DIR)/service.mk 不存在" >&2; exit 2; }

.PHONY: build
build: service-check ## 构建该服务的 release 产物
	$(call need,SERVICE_BUILD)
	$(SERVICE_BUILD)

.PHONY: test
test: build ## 运行单元测试、契约测试与 HTTP 测试
	$(call need,SERVICE_TEST)
	$(SERVICE_TEST)

.PHONY: run
run: build ## 前台启动服务
	$(call need,SERVICE_RUN)
	$(SERVICE_RUN)

.PHONY: image
image: build ## 构建该服务的 docker 镜像
	docker build -f $(SERVICE_DIR)/Dockerfile $(SERVICE_IMAGE_ARGS) -t $(IMAGE):$(TAG) .

.PHONY: image-run
image-run: ## 运行镜像并把 $(PORT) 映射到容器 8080
	docker run --rm -p $(PORT):8080 --name $(IMAGE)-local $(IMAGE):$(TAG)

.PHONY: smoke
smoke: image ## 起容器做冒烟检查（服务自带 smoke.sh）
	$(call need,SERVICE_SMOKE)
	$(SERVICE_SMOKE)

.PHONY: fixtures
fixtures: build ## 重新生成契约测试基线，提交前必须 review 差异
	$(call need,SERVICE_FIXTURES)
	$(SERVICE_FIXTURES)

.PHONY: fmt
fmt: ## 格式化 Rust 代码
	$(CARGO) fmt --all

.PHONY: fmt-check
fmt-check: ## 检查 Rust 代码格式
	$(CARGO) fmt --all -- --check

.PHONY: clippy
clippy: ## 静态检查 Rust workspace
	$(CARGO) clippy --workspace --all-targets -- -D warnings

.PHONY: clean
clean: service-check ## 清理该服务的构建产物
	$(call need,SERVICE_CLEAN)
	$(SERVICE_CLEAN)
