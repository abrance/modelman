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
| model-logcluster | 9103 |

## 有状态服务：日志聚类的额外要求

`model-ocr` 是无状态的：镜像里带着权重，重启不影响行为。`model-logcluster`
不是——它的“模型”是运行期累积出来的模板树，落在 `STATE_DIR`。这带来两件
部署侧必须知道的事：

1. **必须挂卷。** `cops` 侧的 `compose.yaml` 要给 `/app/state` 挂一个 volume，
   否则每次重建容器都从空树开始，历史模板全部丢失。
2. **回滚不是只改 tag。** 把 `MODEL_LOGCLUSTER_IMAGE_TAG` 改回上一版会一起带回旧的
   状态格式。状态里的 `schema_version` 与聚类参数（`SIM_TH` 等）与当前配置不一致时，
   服务会拒绝加载：`/readyz` 503、`/models` 给出原因、业务端点 503，
   并且**不覆盖**已有的状态文件。确认不再需要旧状态后手工删除状态文件再重启即可。
3. **状态卷不做自动备份。** 这是明确不做的项（`docs/design.md` D14）：模板可以从日志
   重新学出来，丢卷的代价是多跑一遍而不是丢数据。要备份就手工做，命令见
   `cops` 仓库 `apps/model-logcluster/.env` 末尾。

默认单副本：状态在本地卷里，横向扩容前必须先把状态外置（见 `docs/roadmap.md`）。

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

## 把 OCR 页面（`/ui`）对公网或手机开放

服务自带一个页面：`GET /ui`，点选/拖入/粘贴/手机拍照 → 文字。设计与交互见
`docs/ocr-ui.md`。它和 API 在同一个端口上，所以开放它就是把 9101 暴露出去，
必须按下面的顺序做 —— **顺序不能反**：

1. **先开 `AUTH_TOKEN`**（上面「暴露方式」的三步）。反过来做的后果是：在你挂上入口
   到开好 token 之间的那段时间里，`/ocr` 是任何人都能调的 CPU 密集接口。
2. **确认入口用 HTTPS。** 两个原因：HTTP 下 token 明文过网；非安全上下文里浏览器
   不给用 `navigator.clipboard`，「一键复制」会直接失效。
3. **入口必须落在域名根路径。** 页面用绝对路径调 `/ocr`，所以 traefik 要把
   `https://<域名>/` 转发到 `127.0.0.1:9101`；挂在子路径（如 `https://host/model-ocr/`）
   下页面能打开但无法识别。
4. 手机上访问 `https://<域名>/ui`，首次使用在页面里的「访问 token」处填一次，
   浏览器会记住。

反代与 TLS 证书在云主机上由 dockpanel/traefik 管，不在本仓库也不在 `cops` 仓库范围内，
这里只给清单。验收时确认三件事：未带 token 的 `curl https://<域名>/ocr` 返回 401、
页面里能识别出一张图、浏览器地址栏是 HTTPS。

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
