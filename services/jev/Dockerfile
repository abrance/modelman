# 判定服务的运行镜像。
#
# 构建上下文是仓库根目录。与日志聚类不同的一点：权重不在仓库里（678 MB，
# 超出公开仓库的单文件上限），所以这一层在**构建期**从 HuggingFace 按不可变
# revision 取回、逐文件校验 sha256 后再放进镜像。运行期不联网，也不做任何
# 下载——运行主机不保证能访问外网，这一点与门禁服务的定位一致。
#
# 不以非 root 用户运行是有意的：与另外两个服务保持同一套运维假设
# （没有需要写盘的共享卷、也没有需要降权的路径）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 默认用 PyTorch 官方 CPU 索引（标准 PEP 503，GitHub 托管 runner 拉它很快）。
# 国内网络下官方索引会报 hash 校验失败，改用阿里云的目录型镜像，或直接用
# 阿里云 pytorch-wheels 镜像，**不要**把镜像源写成唯一路径：
# CI 卡在依赖安装上要等满 30 分钟超时才报错。
#
# 权重取回的源同理，HF_ENDPOINT 是可换的（本机直连 huggingface.co 常常不通）。
COPY services/jev/requirements.txt /app/requirements.txt
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir "torch==2.9.1+cpu" -f "${TORCH_INDEX}" \
 && pip install --no-cache-dir -r /app/requirements.txt

COPY services/jev/src /app/src
COPY services/jev/tools /app/tools
COPY registry/jev-digests.json /app/jev-digests.json

# 权重取回。HF_ENDPOINT 可换成镜像源（国内网络直连 huggingface.co 常常不通）；
# MODEL_REVISION 只用于文档与自检，真正的版本由 digests 文件里的 revision 决定。
ARG HF_ENDPOINT=https://huggingface.co
ARG MODEL_TIER=multilingual
RUN python /app/tools/fetch_weights.py \
        --tier "${MODEL_TIER}" \
        --digests /app/jev-digests.json \
        --out /app/models \
        --endpoint "${HF_ENDPOINT}"

# 构建溯源。Python 没有 build.rs 那种编译期注入，所以在镜像里放一个文件，
# src/build_info.py 优先读它。
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
RUN printf '{"git_commit":"%s","build_time":"%s"}\n' \
        "${GIT_COMMIT}" "${BUILD_TIME}" > /app/build_info.json

ENV MODELS_DIR=/app/models \
    MODEL_TIER=multilingual \
    DEVICE=cpu \
    LISTEN_ADDR=0.0.0.0:8080 \
    PRELOAD=true \
    MAX_CONCURRENCY=2 \
    QUEUE_TIMEOUT_SECS=30 \
    LIMIT_CONCURRENCY=8 \
    MAX_LEN=1024 \
    HEAD_MAX_LEN=256 \
    MAX_STATE_CHARS=8000 \
    MAX_QUESTIONS=32 \
    MAX_OPTIONS=100 \
    MAX_BYTES=1048576 \
    HEALTHCHECK_PATH=/readyz \
    LOG_LEVEL=info

# 部署主机是 16 核、与邻居共享；模型加载要吃满 1.7 GB 常驻、峰值 2.3 GB，
# 线程数按 4 给，留出余量给同一节点上的其它服务（实测 4 线程足够把单问压进 100 ms）。
ENV INFER_THREADS=4

EXPOSE 8080

# 自带探活，镜像里不需要 curl/wget。
# 探的是 /readyz：权重没加载完时进程在跑、/healthz 仍是 200，
# 只有 /readyz 会 503，把健康状态绑在它上面才不是假阳性。
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
