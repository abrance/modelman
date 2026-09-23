# 日志聚类服务的运行镜像。
#
# 单阶段：依赖全部来自 PyPI，没有需要编译的本地代码，也不需要和宿主机
# 共享工具链。构建上下文是仓库根目录（模型/代码都在仓库里，镜像里不含
# 任何运行期要下载的东西）。
#
# 不以非 root 用户运行是有意的：STATE_DIR 在部署时挂的是 docker volume，
# 卷属主由 docker 决定，改成非 root 反而容易变成"状态写不进去"。
# 这是状态目录权限与镜像行为之间的取舍，写在 README 里。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY services/logcluster/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/logcluster/src /app/src

# 构建溯源。Python 没有 build.rs 那种编译期注入，所以在镜像里放一个文件，
# src/build_info.py 优先读它。
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
RUN printf '{"git_commit":"%s","build_time":"%s"}\n' \
        "${GIT_COMMIT}" "${BUILD_TIME}" > /app/build_info.json

ENV STATE_DIR=/app/state \
    LISTEN_ADDR=0.0.0.0:8080 \
    SIM_TH=0.4 \
    DEPTH=4 \
    MAX_CHILDREN=100 \
    PARAMETRIZE_NUMERIC=true \
    MASK_RULES= \
    SNAPSHOT_COMPRESS=true \
    SNAPSHOT_INTERVAL_MINUTES=5 \
    MAX_CONCURRENCY=2 \
    QUEUE_TIMEOUT_SECS=30 \
    LIMIT_CONCURRENCY=8 \
    MAX_LINES=2000 \
    MAX_LINE_CHARS=8192 \
    MAX_BYTES=8388608 \
    HEALTHCHECK_PATH=/readyz \
    LOG_LEVEL=info

RUN mkdir -p /app/state

EXPOSE 8080

# 自带探活，镜像里不需要 curl/wget。
# 探的是 /readyz：状态不可用时进程在跑、/healthz 仍是 200，
# 只有 /readyz 会 503，把健康状态绑在它上面才不是假阳性。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
