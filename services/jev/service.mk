# 判定服务的构建入口（Python 技术栈）。
#
# 与日志聚类同款：同样的六个变量，换成 venv + pytest + 权重取回，
# 根 Makefile 一行都不用改。

PYTHON ?= python3

# 绝对路径：SERVICE_RUN 会先 cd 进服务目录，相对路径会失效
VENV := $(CURDIR)/$(SERVICE_DIR)/.venv
PY   := $(VENV)/bin/python

# torch 走 CPU 轮子镜像单独装：PyPI 上的 linux 轮子会连带拉进 CUDA 依赖，
# 镜像体积与构建时间都翻几倍。版本与 Dockerfile 保持一致。
TORCH_INDEX := https://mirrors.aliyun.com/pytorch-wheels/cpu/
TORCH_VERSION := 2.9.1+cpu

# Python 没有"编译产物"，构建这一步的含义是把钉死版本的依赖装进 venv，
# 并把权重取回本地——契约测试会真实加载模型，没有权重就只能整组跳过。
SERVICE_BUILD = \
	$(PYTHON) -m venv $(VENV) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		"torch==$(TORCH_VERSION)" -f $(TORCH_INDEX) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		-r $(SERVICE_DIR)/requirements-dev.txt && \
	$(PY) $(SERVICE_DIR)/tools/fetch_weights.py \
		--digests registry/jev-digests.json --out $(SERVICE_DIR)/models

SERVICE_TEST = cd $(SERVICE_DIR) && $(PY) -m pytest

SERVICE_RUN = \
	cd $(SERVICE_DIR) && \
	MODELS_DIR=models \
	LISTEN_ADDR=0.0.0.0:$(PORT) \
	LOG_LEVEL=debug \
	$(PY) -m src.main

SERVICE_FIXTURES = cd $(SERVICE_DIR) && $(PY) tools/gen_fixtures.py tests/fixtures

SERVICE_SMOKE = IMAGE=$(IMAGE):$(TAG) PORT=$(PORT) bash $(SERVICE_DIR)/smoke.sh

# 清掉 venv 与缓存。**不动 models/**：那是 678 MB 的权重，删掉要重新下载。
SERVICE_CLEAN = rm -rf $(VENV) $(SERVICE_DIR)/.pytest_cache $(SERVICE_DIR)/__pycache__ $(SERVICE_DIR)/src/__pycache__ $(SERVICE_DIR)/tests/__pycache__

# 镜像里没有 .git，提交与构建时间只能构建期注入
SERVICE_IMAGE_ARGS = \
	--build-arg GIT_COMMIT=$$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown) \
	--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)
