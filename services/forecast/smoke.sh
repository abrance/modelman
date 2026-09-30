#!/usr/bin/env bash
# 容器冒烟检查：起容器、等就绪、打真实请求、报告健康状态。
#
# 断言写在 tools/smoke_checks.py 里，用标准库直连 HTTP——镜像里没有 curl，
# 宿主机上也不假设装了 jq。这个脚本只负责容器的起停与日志兜底。
#
# 输入（环境变量）：
#   IMAGE  镜像全名，含 tag
#   PORT   宿主机端口，映射到容器 8080，默认 8080
set -euo pipefail

image="${IMAGE:?需要设置 IMAGE}"
port="${PORT:-8080}"

name="modelman-smoke-forecast-$$"
cleanup() { docker rm -f "${name}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 内存上界给到 4G：实测权重校验+加载的峰值 RSS 2.8 GB，低于这个值容器会被
# OOM 杀掉，而这正是要在这里暴露出来的问题。
docker run -d --name "${name}" -p "${port}:8080" --memory=4g "${image}" >/dev/null

ready=0
# 冷启动要读 1.32 GB 权重并逐文件核 sha256，给足 300 秒
for _ in $(seq 1 100); do
    if curl -fsS "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "${name}")" != "true" ]; then
        echo "smoke: 容器已退出" >&2
        docker logs "${name}" >&2 || true
        exit 1
    fi
    sleep 3
done
if [ "${ready}" -ne 1 ]; then
    echo "smoke: 容器在 300 秒内没有就绪" >&2
    docker logs "${name}" >&2 || true
    exit 1
fi

# 页面头部与安全头
curl -fsS -D /tmp/forecast-ui-headers "http://127.0.0.1:${port}/" | grep -q '<title>'
curl -fsS "http://127.0.0.1:${port}/app.css" | grep -q '\-\-accent'
grep -qi 'content-type: text/html' /tmp/forecast-ui-headers
grep -qi "content-security-policy: default-src 'none'" /tmp/forecast-ui-headers
echo "smoke ui: 页面与静态资源就绪"

python3 "$(dirname "$0")/tools/smoke_checks.py"

# 只报告不改判：healthcheck 有 start-period，冒烟跑到这里通常还是 starting。
echo "smoke: health=$(docker inspect -f '{{.State.Health.Status}}' "${name}")"
