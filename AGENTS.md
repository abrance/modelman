# AGENTS.md - modelman 仓库指南

## 仓库定位

存放自托管小模型服务（图像识别、时序预测、日志聚类等）的代码、模型与交付配置。
每个服务产出独立镜像，由 CI 构建并推送到 GHCR，再由 `cops` 仓库固定镜像 tag 部署到云主机。

本仓库**不负责部署**。不要在这里写 SSH、docker compose 或云主机相关的逻辑。

## 技术栈

| 组件 | 用途 |
|---|---|
| Rust 2021 + cargo workspace | 服务实现 |
| axum | HTTP 服务框架 |
| MNN（经 ocr-rs） | 小模型 CPU 推理 |
| Docker + GHCR | 交付 |
| GitHub Actions | 构建、测试、发布 |

Python 服务（日志聚类、时序预测一类）也放在 `services/<name>/` 下，与 Rust 服务同级，
各自带 `Dockerfile`，不加入 cargo workspace。参考实现见 `services/logcluster`。

## 目录约定

```
services/<name>/     单个服务：src/、tests/fixtures/、service.mk、smoke.sh、Dockerfile
registry/<name>.yaml 该服务运行的模型与版本（唯一事实来源）
docs/                设计、规划、架构约定、扩展与部署说明
```

`service.mk` 声明本服务的构建、测试、运行与冒烟命令，根 `Makefile` 与 CI 据此分派；
`smoke.sh` 是容器冒烟检查，CI 与发布流程共用同一份。

新增服务时先读 `docs/adding-a-service.md`，按其中的检查单逐项落实。

## 先读哪份文档

| 想了解 | 看哪里 |
|---|---|
| 为什么要这么设计、受什么约束、关键决策与代价 | `docs/design.md` |
| 已完成什么、下一步做什么、哪些明确不做 | `docs/roadmap.md` |
| 仓库内的目录职责、分层要求、推理侧硬约束 | `docs/architecture.md` |
| 怎么新增一个服务 | `docs/adding-a-service.md` |
| 镜像与部署链路 | `docs/deployment.md` |

## 服务接口约定

所有服务必须实现下列端点，字段名保持一致，便于部署链路与运维统一处理：

| 端点 | 要求 |
|---|---|
| `GET /healthz` | 200，包含已加载模型列表 |
| `GET /livez` | 200，仅表示进程存活 |
| `GET /readyz` | 默认模型常驻后返回 200，否则 503 |
| `GET /version` | 版本、git 提交、构建时间、生效配置 |
| `GET /models` | 可用模型清单，含加载状态与加载失败原因 |
| `GET /metrics` | Prometheus 文本格式 |
| 业务端点 | JSON 请求/响应，错误响应包含可读的 error 字段 |

`/ocr` 的响应结构属于对外契约，字段只能新增不能改名。

## 构建与测试

```bash
make build      # 构建该服务的 release 产物
make test       # 单元 + 契约 + HTTP 测试，会真实加载模型推理
make image      # 构建该服务的 docker 镜像
make fixtures   # 重新生成契约测试基线
```

`make` 目标是服务无关的分派器：具体命令写在 `services/<name>/service.mk`，
CI 按 `services/*/service.mk` 发现服务，所以新增服务不必改 Makefile 与 `ci.yml`。
切换服务用 `make <目标> SERVICE=<name>`。

要求：改动提交前 `make fmt-check`、`make clippy`、`make test` 三条全绿。

## 模型相关规则

1. **权重与代码同仓库、同镜像**。小模型（几十 MB 以内）直接随镜像交付，
   换模型不需要重建环境，也不需要运行时联网下载。
2. **构建期不得依赖 HuggingFace 拉取权重**。CI 需要访问外网下载 MNN 预编译归档，
   但模型文件本身必须来自仓库。
3. **`registry/<name>.yaml` 是"哪个模型在跑"的唯一事实来源**。改模型必须同步改它，
   并重新生成契约测试基线。
4. **模型变更必须重新生成并 review 契约测试基线**，差异要能解释。
5. **不要交付无法加载的模型**。若某个模型文件在推理后端加载失败，
   从目录中移除并在 `registry/<name>.yaml` 的 `excluded` 中记录原因。
6. **没有权重文件的服务也要登记**：在 `registry/<name>.yaml` 里显式写
   `weights: none` 并说明原因（例如日志聚类用的 Drain3 没有训练好的权重），
   同时登记它的参数组合与运行期状态的格式。运行期状态不属于镜像，
   回滚镜像不会回滚它，详见 `docs/deployment.md`。

## 推理相关规则

1. 推理必须放在阻塞线程上执行（`spawn_blocking`），不得阻塞异步运行时。
2. 按引擎粒度加锁，不要用一把全局锁串行化所有模型。
3. 并发必须由信号量设上界，默认值不超过宿主机可用核数；
   云主机 CPU 是与其它服务共享的，默认收敛比放开更安全。
4. 图像等大输入必须设体积与尺寸上限，解码走带限制的路径。
5. 阈值、并发、尺寸上限等参数走环境变量，不要硬编码。

## 安全与隐私

1. 服务默认不加鉴权；一旦部署到公网可达的位置，必须设置 `AUTH_TOKEN`，
   并保持 compose 只绑定 `127.0.0.1`，由反向代理对外提供 HTTPS。
2. 契约测试样本来自真实截图，含真实昵称等个人信息。本仓库是公开仓库，
   新增样本前先确认是否可以公开；如需替换，用等结构、等字体特征的重绘图。
3. 文档中不得出现真实 IP、账号、口令；用占位符或说明性文字代替。

## 提交约定

- 提交信息用中文，格式 `<类型>: <说明>`，例如 `feat: 新增 OCR 服务契约测试`。
- 不直接推 main，改动走分支 + PR。
