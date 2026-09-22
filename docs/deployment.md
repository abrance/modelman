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

`compose.yaml` 默认只绑定 `127.0.0.1`，即只有云主机本机进程和同机的反向代理能访问。
这是刻意的默认值：OCR 服务本身没有账号体系，直接绑 `0.0.0.0` 等于把接口公开。

需要对外提供服务时，按以下顺序处理：

1. 设置 `AUTH_TOKEN`，把 token 放进云主机 `/opt/cops/secrets/model-ocr.env`，
   并在 `app.conf` 的 `SECRET_ENV` 中声明，同时在 cops 的 `deploy.yml`
   里把对应的 GitHub Actions secret 映射进去。
2. 把 compose 的端口绑定从 `127.0.0.1:9101` 改成 `9101`，或改为由
   同机 traefik 反向代理到该端口，由 traefik 提供 HTTPS。
3. 确认云安全组只放行必要端口。

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
