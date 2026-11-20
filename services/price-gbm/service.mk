# 广东现货小时电价 GBM 服务的构建入口（Python 技术栈，与 forecast 同款约定）。

PYTHON ?= python3

# 绝对路径：SERVICE_RUN 会先 cd 进服务目录，相对路径会失效
VENV := $(CURDIR)/$(SERVICE_DIR)/.venv
PY   := $(VENV)/bin/python

# Python 没有"编译产物"，构建这一步的含义是把钉死版本的依赖装进 venv。
# 权重（booster 文本）随仓库交付，不需要构建期取回。
SERVICE_BUILD = \
	$(PYTHON) -m venv $(VENV) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		-r $(SERVICE_DIR)/requirements-dev.txt

SERVICE_TEST = cd $(SERVICE_DIR) && $(PY) -m pytest

SERVICE_RUN = \
	cd $(SERVICE_DIR) && \
	MODELS_DIR=models \
	LISTEN_ADDR=0.0.0.0:$(PORT) \
	LOG_LEVEL=debug \
	$(PY) -m src.main

SERVICE_FIXTURES = cd $(SERVICE_DIR) && $(PY) tools/gen_fixtures.py

SERVICE_SMOKE = IMAGE=$(IMAGE):$(TAG) PORT=$(PORT) bash $(SERVICE_DIR)/smoke.sh

# 清掉 venv 与缓存。权重只有几 MB，一并清掉也无所谓，但保持与 forecast 同款口径。
SERVICE_CLEAN = rm -rf $(VENV) $(SERVICE_DIR)/.pytest_cache $(SERVICE_DIR)/__pycache__ $(SERVICE_DIR)/src/__pycache__ $(SERVICE_DIR)/tests/__pycache__

# 镜像里没有 .git，提交与构建时间只能构建期注入
SERVICE_IMAGE_ARGS = \
	--build-arg GIT_COMMIT=$$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown) \
	--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)
