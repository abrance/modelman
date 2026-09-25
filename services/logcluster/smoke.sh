#!/usr/bin/env bash
# 容器冒烟检查：起容器、等就绪、跑一次真实的聚类、报告健康状态。
#
# 输入（环境变量）：
#   IMAGE  镜像全名，含 tag
#   PORT   宿主机端口，映射到容器 8080，默认 8080
set -euo pipefail

image="${IMAGE:?需要设置 IMAGE}"
port="${PORT:-8080}"

name="modelman-smoke-$$"
cleanup() { docker rm -f "${name}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "${name}" -p "${port}:8080" --memory=512M "${image}" >/dev/null

ready=0
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
        ready=1
        break
    fi
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

# 自带页面随镜像交付（三份静态文件在启动时读进内存），确认真的带上去了、
# 且内容类型与安全头都对
curl -fsS -D /tmp/logcluster-ui-headers "http://127.0.0.1:${port}/" | grep -q '<title>'
curl -fsS "http://127.0.0.1:${port}/app.css" | grep -q '\-\-accent'
curl -fsS "http://127.0.0.1:${port}/app.js" | grep -q 'X-Auth-Token'
grep -qi 'content-type: text/html' /tmp/logcluster-ui-headers
grep -qi "content-security-policy: default-src 'none'" /tmp/logcluster-ui-headers
echo "smoke ui: 页面与静态资源就绪"

# /version 里的 git_commit 应与镜像 tag 的后缀一致
curl -fsS "http://127.0.0.1:${port}/version"
echo

curl -fsS "http://127.0.0.1:${port}/models" | python3 -c '
import json, sys
models = json.load(sys.stdin)
assert models, models
state = models[0]["state"]
assert state["loaded"] is False, state  # 新容器从空状态开始
print("smoke models:", models[0]["id"], "schema", state["schema_version"])
'

# 真实聚类一次：4 行应当收敛成 2 个模板。
# 注意新建簇的那一行返回的是它自己的原文，占位符只在发生合并后出现。
curl -fsS -X POST -H 'Content-Type: application/json' \
    -d '{"lines":["user alice logged in","user bob logged in",
                  "disk usage 95% on /dev/sda1","disk usage 12% on /dev/sdb2"]}' \
    "http://127.0.0.1:${port}/cluster" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["success"] is True, payload
assert payload["cluster_count"] == 2, payload
templates = [item["template"] for item in payload["results"]]
assert "user <*> logged in" in templates, templates
assert "disk usage <*> on <*>" in templates, templates
print("smoke cluster ok:", " | ".join(t for t in templates if "<*>" in t))
'

curl -fsS "http://127.0.0.1:${port}/clusters" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["total_size"] == 4, payload
print("smoke clusters ok:", len(payload["clusters"]), "templates")
'

# 只报告不改判：healthcheck 有 start-period，冒烟跑到这里通常还是 starting。
echo "smoke: health=$(docker inspect -f '{{.State.Health.Status}}' "${name}")"
