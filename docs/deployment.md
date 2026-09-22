# 部署链路

本仓库只产出镜像，部署由 `cops` 仓库完成。两边的边界是这样的：

```
modelman（本仓库，公开）
  打 tag ocr/v0.1.0
    -> GitHub Actions 编译 + 契约测试 + 冒烟测试
    -> 推送 ghcr.io/<owner>/modelman-ocr:v0.1.0-<短 sha>
    -> 在 job summary 里给出需要写进 cops 的 tag

cops（部署仓库）
  改 apps/model-ocr/.env 的 MODEL_OCR_IMAGE_TAG
    -> PR 合入 main
    -> deploy.yml 通过 SSH 在云主机执行 scripts/deploy.sh
    -> docker compose pull && up -d && 等健康检查
```

镜像 tag 是 `<版本>-<提交短 sha>`，永不覆盖。原因：境内主机通过镜像站拉取，
镜像站按 tag 缓存，可覆盖的 tag 会拿到陈旧层。

## 镜像命名

| 项 | 值 |
|---|---|
| 镜像 | `ghcr.io/<owner>/modelman-ocr` |
| 境内拉取路径 | `ghcr.chenby.cn/<owner>/modelman-ocr` |
| 备选镜像站 | `ghcr.nju.edu.cn/<owner>/modelman-ocr` |
| 平台 | `linux/amd64` |

若在云主机上 `docker pull` 长时间停在 `Pulling fs layer` 且不出现任何
`Pull complete`，说明 blob 回源 GitHub CDN 不通，换用备选镜像站拉取，
比对 digest 后 `docker tag` 回原路径即可，不要反复重试直连。

## cops 侧部署单元

```
apps/model-ocr/
  .env          期望状态：镜像、tag、端口、运行参数
  compose.yaml  资源上限、健康检查、日志轮转
  app.conf      部署脚本读取的健康检查元数据
```

端口分配：云主机上 11000 / 1502 / 4173 / 8081 / 9090 等已被占用，
模型服务统一使用 91xx 段。

| 服务 | 宿主机端口 |
|---|---|
| model-ocr | 9101 |
| model-forecast（预留） | 9102 |
| model-log-cluster（预留） | 9103 |

## 暴露方式

`compose.yaml` 只绑定 `127.0.0.1`，即只有云主机本机进程和同机的反向代理能访问。
这是刻意的默认值：OCR 服务本身没有账号体系，直接绑 `0.0.0.0` 等于把接口公开。

公网入口（TLS、域名反代）由宿主机上的 dockpanel/traefik 管理，不在本仓库也不在
`cops` 仓库的范围，`cops` 只保证服务在回环地址上监听。这与 `ptdoc-qdrant` 的处理方式一致。

**只要那个入口对公网开放，就必须先给服务加 token。** 未加 token 的 OCR 接口是
纯 CPU 消耗型接口，公开可达意味着任何人都能用你的算力。三步：

1. 在 `cops` 的 `deploy.yml` 密钥分发映射里加一行（GitHub Actions 不支持按变量名动态读 secret）。
2. 在 `apps/model-ocr/app.conf` 的 `SECRET_ENV` 与 `REQUIRED_ENV` 中声明 `AUTH_TOKEN`。
3. 把 token 写入云主机 `/opt/cops/secrets/model-ocr.env`（权限 600）。

设置后 `/ocr`、`/ocr/batch`、`/metrics` 需要带 `X-Auth-Token` 或
`Authorization: Bearer`，`/healthz`、`/readyz`、`/version`、`/models` 仍然免鉴权，
以便容器在拿到凭据之前就能通过健康检查。

## 资源上限

OCR 服务实测常驻内存约 65 MB（`v6small` 预热），compose 里给了较宽的上限
作为兜底而不是预期用量：

| 项 | 值 |
|---|---|
| `cpus` | 2.0 |
| `memory` | 1500M |
| `OMP_NUM_THREADS` | 2 |
| `MAX_CONCURRENCY` | 2 |

云主机只有 4 核且与 lems、ptcdoc、qdrant、postgres 共享，因此这两个并发参数
必须显式设置。PyTorch / MNN 默认按核数开线程，多个容器各开满线程会互相拖累。

## 回滚

把 `apps/model-ocr/.env` 的 `MODEL_OCR_IMAGE_TAG` 改回上一版本，再开一次 PR 合入 main。
不需要在云主机上做任何手工操作。

## 部署后验证

```bash
# 容器状态与健康
ssh <云主机> 'docker ps --filter name=model-ocr'
ssh <云主机> 'docker inspect -f "{{.State.Health.Status}}" model-ocr'

# 接口与版本
ssh <云主机> 'curl -s http://127.0.0.1:9101/version'
ssh <云主机> 'curl -s http://127.0.0.1:9101/models'

# 真实识别一次
ssh <云主机> 'curl -s -F image=@/tmp/case.png http://127.0.0.1:9101/ocr'

# 资源占用
ssh <云主机> 'docker stats --no-stream model-ocr'
```

`/version` 里的 `git_commit` 应与本次 cops 采用的镜像 tag 后缀一致，
这是确认"线上跑的确实是这次发布的镜像"的最快方式。
