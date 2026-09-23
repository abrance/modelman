# OCR 服务的构建入口。
#
# 根 Makefile 只做分派，这个文件声明本服务怎么构建、怎么测试、怎么运行、
# 怎么冒烟。新增服务时照这份复制改命令即可，根 Makefile 与 CI 都不用动。

# run 与 fixtures 使用的档位，可用 `make run MODEL=v6tiny` 覆盖
MODEL ?= v6small

SERVICE_BUILD = $(CARGO) build --release

# 只跑本服务的测试：workspace 里出现第二个 Rust 服务时不会顺带一起跑。
SERVICE_TEST = $(CARGO) test -p ocr-service --release

SERVICE_RUN = \
	MODELS_DIR=$(SERVICE_DIR)/models \
	DEFAULT_MODEL=$(MODEL) \
	PRELOAD_MODELS=$(MODEL) \
	LISTEN_ADDR=0.0.0.0:$(PORT) \
	RUST_LOG=ocr_service=info \
	./target/release/ocr-service

SERVICE_FIXTURES = cd $(SERVICE_DIR) && ../../target/release/gen-fixtures tests/fixtures $(MODEL)

SERVICE_SMOKE = IMAGE=$(IMAGE):$(TAG) PORT=$(PORT) bash $(SERVICE_DIR)/smoke.sh

SERVICE_CLEAN = $(CARGO) clean
