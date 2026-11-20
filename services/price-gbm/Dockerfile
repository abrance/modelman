# 广东现货小时电价 GBM 服务的运行镜像。
#
# 构建上下文是仓库根目录。与 forecast 不同：booster 是几百 KB 的文本文件，
# 随仓库交付（D1：小模型直接同仓同镜像），构建期不需要取回任何权重，
# 运行期也不联网。不以非 root 用户运行，与其它服务保持同一套运维假设。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# LightGBM 链接 OpenMP 运行时，slim 基镜像不带
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY services/price-gbm/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/price-gbm/src /app/src
COPY services/price-gbm/models /app/models

# 构建溯源。与 forecast 同款：镜像里放 build_info.json，src/build_info.py 优先读它。
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
RUN printf '{"git_commit":"%s","build_time":"%s"}\n' \
        "${GIT_COMMIT}" "${BUILD_TIME}" > /app/build_info.json

ENV MODELS_DIR=/app/models \
    MODEL_TIER=gbm-hour-v1 \
    LISTEN_ADDR=0.0.0.0:8080 \
    VERIFY_WEIGHTS=true \
    MAX_DAYS=60 \
    MAX_HORIZON_DAYS=14 \
    MAX_BYTES=2097152 \
    HEALTHCHECK_PATH=/readyz \
    LOG_LEVEL=info

EXPOSE 8080

# booster 文本毫秒级加载，start-period 只需小余量。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
