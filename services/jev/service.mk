# 判定服务的构建入口（Python 技术栈）。
#
# 与日志聚类同款：同样的六个变量，换成 venv + pytest + 权重取回，
# 根 Makefile 一行都不用改。

PYTHON ?= python3

# 绝对路径：SERVICE_RUN 会先 cd 进服务目录，相对路径会失效
VENV := $(CURDIR)/$(SERVICE_DIR)/.venv
PY   := $(VENV)/bin/python

# torch 单独装 CPU 轮子：PyPI 上的 linux 轮子会连带拉进整套 CUDA 依赖，
# 镜像体积与构建时间都翻几倍。
#
# 默认用 PyTorch 官方 CPU 索引：它是标准的 PEP 503 索引，GitHub 托管 runner
# 拉它很快。国内网络下官方索引会报 hash 校验失败，改阿里云的目录型镜像：
#   make build SERVICE=jev TORCH_INDEX=https://mirrors.aliyun.com/pytorch-wheels/cpu/
# 默认值必须选“CI 能过”的那个：卡在依赖安装上的失败要先在本地重现，而 CI
# 一旦卡住要等满 30 分钟才报错。
TORCH_INDEX ?= https://download.pytorch.org/whl/cpu
TORCH_VERSION := 2.9.1+cpu

# 权重取回的源，同样是环境可覆盖的（cloud3 直连 HF，本机多数时候要走镜像）
HF_ENDPOINT ?= https://huggingface.co

# Python 没有"编译产物"，构建这一步的含义是把钉死版本的依赖装进 venv，
# 并把权重取回本地——契约测试会真实加载模型，没有权重就只能整组跳过。
SERVICE_BUILD = \
	$(PYTHON) -m venv $(VENV) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		"torch==$(TORCH_VERSION)" -f $(TORCH_INDEX) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		-r $(SERVICE_DIR)/requirements-dev.txt && \
	$(PY) $(SERVICE_DIR)/tools/fetch_weights.py \
		--digests registry/jev-digests.json --out $(SERVICE_DIR)/models \
		--endpoint $(HF_ENDPOINT)

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
