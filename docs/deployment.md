# 部署链路

本仓库只产出镜像，部署由 `cops` 仓库完成。两边职责的边界：

- **modelman（本仓库）**：代码与权重同仓，打 tag → CI 构建 + 契约测试 + 冒烟 → 推镜像到
  GHCR，并在 job summary 里给出需要写进 cops 的 tag。
- **cops（部署仓库）**：每个服务一个单元目录，`.env` 里锁定镜像 tag，PR 合入 main 后由
  CI 部署到目标主机并做健康门禁。

部署模式的权威在 `cops` 仓库（单元怎么写、主机怎么登记、部署脚本怎么跑）；
本页只写与"产镜像"耦合的部分：tag 命名、发布流程、暴露口径、回滚与验证。

```
modelman（本仓库）
  打 tag <服务名>/vX.Y.Z
    -> GitHub Actions 构建 + 契约测试 + 冒烟测试
    -> 推送 ghcr.io/abrance/modelman-<服务名>:vX.Y.Z-<短 sha>
    -> job summary 给出需要写进 cops 的 tag

cops（部署仓库）
  改 apps/model-<服务名>/.env 的 <服务名>_IMAGE_TAG
    -> PR 合入 main
    -> CI 渲染期望状态并部署到单元登记的目标主机（compose 或 k8s，见 cops README）
    -> 部署脚本等健康检查，健康门禁不过即失败
```

镜像 tag 是 `<版本>-<提交短 sha>`，永不覆盖。原因：有主机经镜像站拉取，
镜像站按 tag 缓存，可覆盖的 tag 会拿到陈旧层。

## 镜像命名

| 项 | 值 |
|---|---|
| 镜像 | `ghcr.io/abrance/modelman-<服务名>` |
| 经镜像站拉取 | `ghcr.chenby.cn/abrance/modelman-<服务名>`（cloud2 用；cloud3 直连 `ghcr.io`） |
| 平台 | `linux/amd64` |

两个服务现都在 `cops` 的 cloud3（k3s 单机）上运行：`model-ocr` 入口
`https://ocr.xiaoyxq.top`，`model-logcluster` 入口 `https://logcluster.xiaoyxq.top`。
主机的镜像源差异、端口与资源声明都在 cops 侧，本仓库不维护主机清单。

## 发布流程（一服务一份 workflow）

| 服务 | tag | 版本唯一来源 | workflow |
|---|---|---|---|
| ocr | `ocr/vX.Y.Z` | 根 `Cargo.toml` 的 `version` | `release-ocr.yml` |
| logcluster | `logcluster/vX.Y.Z` | `services/logcluster/src/build_info.py` 的 `VERSION` | `release-logcluster.yml` |

release workflow 会校验 **tag 与版本唯一来源一致**，不一致直接失败——否则镜像 tag
与 `/version` 报的版本对不上，运维就失去了判断依据。

## 有状态服务：日志聚类的额外要求

`model-ocr` 是无状态的：镜像里带着权重，重启不影响行为。`model-logcluster`
不是——它的"模型"是运行期累积出来的模板树，落在 `STATE_DIR`。这带来三件
部署侧必须知道的事（机制细节见 `services/logcluster/README.md`）：

1. **状态必须挂在卷上**（cops 侧现在是 PVC）。卷不挂，每次重建都从空树开始。
2. **回滚不是只改 tag。** 改回上一版会一起带回旧的状态格式：`schema_version` 与
   聚类参数与当前配置不一致时，服务拒绝加载（`/readyz` 503、`/models` 给出原因），
   并且**不覆盖**已有的状态文件。确认不再需要旧状态后手工删除状态文件再重启。
3. **状态卷不做自动备份**（`docs/design.md` D14）。要备份就手工做，命令见
   `cops` 仓库的迁移文档（不在本仓库）。

默认单副本：状态在本地卷里，横向扩容前必须先把状态外置。

## 暴露方式

**当前口径是：不带鉴权就开放**（`docs/design.md` D15–D17）。两个服务都实现了
`AUTH_TOKEN` 但未启用。代价两个服务不同，这是当时单独写 D16 的原因：

- **OCR**：`/ocr` 是纯 CPU 消耗型接口，被滥用的表现是并发额度被占满，
  上界（`MAX_CONCURRENCY`、`QUEUE_TIMEOUT_SECS`）保证对方拿到 503 而不是主机过载；
- **日志聚类**：`/cluster` 是**写**接口——被滥用是在模板树里埋数据，
  而模板污染不会自动恢复，只能清空状态卷重学（代价是丢掉已累积的模板）。

统一鉴权已定为方向（做在外层，`docs/roadmap.md` 中期），per-service 的
`AUTH_TOKEN` 是"要收敛时够用"的停手方案：启用只改 cops 三处（deploy.yml 密钥映射、
`app.conf` 的 `SECRET_ENV`/`REQUIRED_ENV`、主机上的密钥文件），**不需要改服务代码**；
两个页面的 token 输入与请求头都已实现并有测试覆盖。

## 页面

两个服务都自带页面，都在**根路径** `/`，随 API 同端口暴露：

- **OCR**：拍照/传图 → 文字。设计与交互见 `docs/ocr-ui.md`。
- **日志聚类**：贴日志 → 看模板。设计与交互见 `docs/logcluster-ui.md`。

入口必须落在**域名根路径**（页面用绝对路径调 `/ocr`、`/cluster`，子路径式入口会
打不开数据），并走 HTTPS（`navigator.clipboard` 只在安全上下文可用，OCR 页面的
「一键复制」依赖它）。

## 回滚

把 `cops` 里对应单元 `.env` 的镜像 tag 改回上一版，开 PR 合入 main。
不需要在目标主机上做任何手工操作。日志聚类多一步"先看状态格式是否兼容"，见上文。

## 部署后验证

部署由 cops 的健康门禁把守（readinessProbe / 部署脚本探活），发布侧只需确认
"线上跑的确实是这次发布的镜像"：

```bash
curl -s https://ocr.xiaoyxq.top/version
curl -s https://logcluster.xiaoyxq.top/version
```

`/version` 里的 `git_commit` 应与本次 cops 采用的镜像 tag 后缀一致。
进一步的人工验证（打一次真实请求、看资源占用）见 cops 仓库的部署文档。
