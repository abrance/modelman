#!/usr/bin/env bash
# 容器冒烟检查：起一个容器、等它就绪、打一次真实识别、报告健康状态。
#
# CI 与发布流程都调这个脚本，让"镜像能不能跑"只有一处定义。
# 输入（环境变量）：
#   IMAGE  镜像全名，含 tag
#   PORT   宿主机端口，映射到容器 8080，默认 8080
set -euo pipefail

image="${IMAGE:?需要设置 IMAGE}"
port="${PORT:-8080}"
here="$(cd "$(dirname "$0")" && pwd)"

# 容器名带 pid，避免并发跑同一台机器时互相顶掉
name="modelman-smoke-$$"
cleanup() { docker rm -f "${name}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "${name}" -p "${port}:8080" --memory=1500M "${image}" >/dev/null

ready=0
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
        ready=1
        break
    fi
    # 容器已经退出就不用再等满 60 秒了
    if [ "$(docker inspect -f '{{.State.Running}}' "${name}")" != "true" ]; then
        echo "smoke: 容器已退出" >&2
        docker logs "${name}" >&2 || true
        exit 1
    fi
    sleep 2
done
if [ "${ready}" -ne 1 ]; then
    echo "smoke: 容器在 60 秒内没有就绪" >&2
    docker logs "${name}" >&2 || true
    exit 1
fi

curl -fsS "http://127.0.0.1:${port}/healthz"
echo

# /version 里的 git_commit 应与镜像 tag 的后缀一致
curl -fsS "http://127.0.0.1:${port}/version"
echo

curl -fsS "http://127.0.0.1:${port}/models" | python3 -c '
import json, sys
models = json.load(sys.stdin)
assert models, models
print("smoke models:", ", ".join(item["id"] for item in models))
'

curl -fsS -F "image=@${here}/tests/fixtures/case_05.png" \
    "http://127.0.0.1:${port}/ocr" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["success"] is True, payload
assert payload["results"], payload
assert payload["results"][0]["text"], payload
print("smoke ok:", payload["results"][0]["text"])
'

# 只报告不改判：Dockerfile 里 healthcheck 有 20 秒 start-period，冒烟跑到这里
# 通常还是 starting。判失败会把正常的镜像拦下来。
echo "smoke: health=$(docker inspect -f '{{.State.Health.Status}}' "${name}")"
