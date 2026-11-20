# 时序预测服务的运行镜像。
#
# 构建上下文是仓库根目录。权重不在仓库里（model.safetensors 是 1.32 GB，
# 超出公开仓库的单文件上限），所以这一层在**构建期**从 HuggingFace 按不可变
# revision 取回、逐文件校验 sha256 后再放进镜像。运行期不联网，也不做任何
# 下载——运行主机不保证能访问外网，而且引擎在本地目录上显式声明了
# local_files_only，避免 hub 客户端在权重缺失时退化成去线上找一份。
#
# 不以非 root 用户运行是有意的：与另外三个服务保持同一套运维假设
# （没有需要写盘的共享卷、也没有需要降权的路径）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# torch 单独装 CPU 轮子。PyPI 上的 linux 轮子会连带拉进整套 CUDA 依赖，
# 体积从 184 MB 涨到 GB 级，而这里只用 CPU 前向。版本号写作 `torch==2.9.1+cpu`。
#
# 默认源是 PyTorch 官方 CPU 索引：它是 PEP 503 索引，所以用 --index-url，
# GitHub 托管 runner 拉它很快。国内网络下官方索引会报 hash 校验失败，
# 改用阿里云的目录列表镜像时参数换成 --find-links（两边的写法不同，
# 不是只换个域名）。
#
# 权重取回的源同理，HF_ENDPOINT 是可换的（本机直连 huggingface.co 常常不通）。
# 依赖安装不加 --quiet：CI 卡在安装上时，日志里就只剩一条命令行。
COPY services/forecast/requirements.txt /app/requirements.txt
ARG TORCH_VERSION=2.9.1+cpu
# 默认值必须带引号：Docker 的 ARG 解析器按空白切分默认值，
# 不写引号时 TORCH_SOURCE_ARGS 只会拿到 `--index-url`，URL 被丢掉，
# 构建期报 `--index-url option requires 1 argument`（CI 上真的踩到了）。
ARG TORCH_SOURCE_ARGS="--index-url https://download.pytorch.org/whl/cpu"
RUN pip install --no-cache-dir "torch==${TORCH_VERSION}" ${TORCH_SOURCE_ARGS} \
 && pip install --no-cache-dir -r /app/requirements.txt

COPY services/forecast/src /app/src
COPY services/forecast/tools /app/tools
COPY registry/forecast-digests.json /app/forecast-digests.json

# 权重取回。HF_ENDPOINT 可换成镜像源（国内网络直连 huggingface.co 常常不通）；
# 真正的版本由 digests 文件里的 revision 决定，--tier 只决定落地目录名。
ARG HF_ENDPOINT=https://huggingface.co
RUN python /app/tools/fetch_weights.py \
        --tier timesfm-3.0 \
        --digests /app/forecast-digests.json \
        --out /app/models \
        --endpoint "${HF_ENDPOINT}"

# 构建溯源。Python 没有 build.rs 那种编译期注入，所以在镜像里放一个文件，
# src/build_info.py 优先读它。
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
RUN printf '{"git_commit":"%s","build_time":"%s"}\n' \
        "${GIT_COMMIT}" "${BUILD_TIME}" > /app/build_info.json

ENV MODELS_DIR=/app/models \
    MODEL_TIER=timesfm-3.0 \
    DEVICE=cpu \
    LISTEN_ADDR=0.0.0.0:8080 \
    PRELOAD=true \
    VERIFY_WEIGHTS=true \
    MAX_CONTEXT=4096 \
    MAX_HORIZON=512 \
    MAX_SERIES=16 \
    MAX_CONCURRENCY=2 \
    QUEUE_TIMEOUT_SECS=60 \
    LIMIT_CONCURRENCY=8 \
    MAX_BYTES=2097152 \
    HEALTHCHECK_PATH=/readyz \
    LOG_LEVEL=info

# 部署主机是 16 核、与邻居共享；权重加载峰值 2.8 GB 常驻（实测），
# 线程数按 4 给，留出余量给同一节点上的其它服务
# （实测 512 点上下文、48 步长在 4 线程上约 1.3 s）。
ENV INFER_THREADS=4

EXPOSE 8080

# 自带探活，镜像里不需要 curl/wget。
# 探的是 /readyz：权重没加载完时进程在跑、/healthz 仍是 200，
# 只有 /readyz 会 503，把健康状态绑在它上面才不是假阳性。
# start-period 给到 300 秒：1.32 GB 权重的 sha256 校验加加载实测 20 秒上下，
# 但冷启动时磁盘缓存是空的，且 CI 机器更慢，留足余量。
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
