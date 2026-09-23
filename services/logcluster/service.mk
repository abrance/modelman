# 日志聚类服务的构建入口（Python 技术栈）。
#
# 这个文件的存在本身就是"根 Makefile 不认识任何具体服务"的证明：
# 同样的六个变量，这里换成 venv + pytest，根 Makefile 一行都不用改。

PYTHON ?= python3

# 绝对路径：SERVICE_RUN 会先 cd 进服务目录，相对路径会失效
VENV := $(CURDIR)/$(SERVICE_DIR)/.venv
PY   := $(VENV)/bin/python

# Python 没有"编译产物"，构建这一步的含义就是把钉死版本的依赖装进 venv。
# 幂等：venv 已存在时是空操作，pip 已满足时只花一两秒。
SERVICE_BUILD = \
	$(PYTHON) -m venv $(VENV) && \
	$(PY) -m pip install --quiet --disable-pip-version-check \
		-r $(SERVICE_DIR)/requirements-dev.txt

# 用 `python -m pytest` 而不是裸 pytest：前者会把当前目录放进 sys.path，
# 这样测试里可以直接 `import src`。
SERVICE_TEST = cd $(SERVICE_DIR) && $(PY) -m pytest

SERVICE_RUN = \
	cd $(SERVICE_DIR) && \
	STATE_DIR=.state \
	LISTEN_ADDR=0.0.0.0:$(PORT) \
	LOG_LEVEL=debug \
	$(PY) -m src.main

SERVICE_FIXTURES = cd $(SERVICE_DIR) && $(PY) tools/gen_fixtures.py tests/fixtures

SERVICE_SMOKE = IMAGE=$(IMAGE):$(TAG) PORT=$(PORT) bash $(SERVICE_DIR)/smoke.sh

# 清掉 venv 与缓存，**但不动 .state**：那是本地跑出来的模板树，
# 误删等于把累积的聚类结果清零。要重置就自己删那个目录。
SERVICE_CLEAN = rm -rf $(VENV) $(SERVICE_DIR)/.pytest_cache $(SERVICE_DIR)/__pycache__ $(SERVICE_DIR)/src/__pycache__ $(SERVICE_DIR)/tests/__pycache__

# 镜像里没有 .git，提交与构建时间只能构建期注入
SERVICE_IMAGE_ARGS = \
	--build-arg GIT_COMMIT=$$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown) \
	--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)
